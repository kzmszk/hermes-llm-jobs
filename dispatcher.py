#!/usr/bin/env python3
"""Durable PC-side queue. Does not change the broker or the legacy runner."""
import json
import argparse
import contextlib
import datetime
import fcntl
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import runner

LANES = {'common': tuple(runner.TYPES), 'illustration': (runner.ILLUSTRATION,)}
JST = ZoneInfo('Asia/Tokyo')
API_TIMEOUT = 5


@contextlib.contextmanager
def api_deadline():
    # Socket timeouts alone allow an indefinitely slow body or DNS resolution.
    def expired(*args):
        raise TimeoutError('api_timeout')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, API_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def lock(private, name):
    fd = os.open(Path(private) / (name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            yield fd
    finally:
        # close rather than LOCK_UN: a model child may still own this description.
        os.close(fd)


def tick(store, api, spawn, now=None):
    with lock(store.private, 'tick') as held:
        if held is None:
            return
        store.flush(api)
        busy = {r['lane'] for r in store.status() if r['state'] in ('queued', 'running', 'outbox')}
        for lane in LANES:
            recover = False
            with lock(store.private, lane) as available:
                if available is None:
                    busy.add(lane)
                elif any(r['lane'] == lane and r['state'] in ('queued', 'running') for r in store.status()):
                    recover = True
            if recover:
                spawn(lane)
        now = now or datetime.datetime.now(JST)
        types = [kind for lane, kinds in LANES.items() if lane not in busy for kind in kinds]
        if not types or not 8 <= now.astimezone(JST).hour < 24:
            return
        job = api('/api/jobs/runner/claim', {'types': types})['job']
        if job:
            if job['type'] not in types:
                raise ValueError('unexpected_claim_type')
            store.save_claim(job)
            spawn(lane_for(job['type']))


def execute_model(job, lane_fd):
    """Keep legacy prompts/backends intact; inherit lane ownership into the CLI child.

    This is process-local: each worker is single-threaded. The legacy executable
    continues using its unchanged subprocess module and timeouts.
    """
    deadline = time.monotonic() + max(0, job['lease_until'] - time.time())
    class LeaseProcess(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            budget = min(600 if job['type'] == runner.ILLUSTRATION else 150,
                         deadline - time.monotonic())
            if budget <= 0:
                raise TimeoutError('lease_expired')
            kwargs['pass_fds'] = (*kwargs.get('pass_fds', ()), lane_fd)
            # An independent process enforces the bound even if this worker dies.
            # No --foreground: timeout must own/kill the complete model group.
            command = ['/usr/bin/timeout', '--signal=KILL', str(budget) + 's', *args[0]]
            super().__init__(command, *args[1:], **kwargs)

        def communicate(self, input=None, timeout=None):
            # runner calls communicate() without timeout only after killing a child.
            if timeout is not None:
                timeout = min(timeout, max(0, deadline - time.monotonic()))
            return super().communicate(input=input, timeout=timeout)

    original = runner.subprocess
    runner.subprocess = SimpleNamespace(Popen=LeaseProcess, PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired)
    try:
        if time.monotonic() >= deadline:
            raise TimeoutError('lease_expired')
        return runner.run_model(job)
    finally:
        runner.subprocess = original


def worker(store, api, lane, model=None):
    model = model or execute_model
    with lock(store.private, lane) as held:
        if held is None:
            return
        row = store.db.execute("SELECT attempt FROM jobs WHERE lane=? AND state IN ('queued','running') ORDER BY attempt LIMIT 1", (lane,)).fetchone()
        if row:
            attempt = row['attempt']
            entry = store.get(attempt)
            job = entry['job']
            if entry['lease_until'] <= time.time():
                with store.db:
                    store.db.execute("UPDATE jobs SET state='lease_lost',error='lease_expired',updated=? WHERE attempt=?", (time.time(), attempt))
            elif entry['state'] == 'running':
                # Never repeat an ambiguous model invocation locally. Let the broker's
                # bounded retry policy issue a fresh lease after its 300-second delay.
                store.record(attempt, 'fail', {'reason': 'runner_error'})
            else:
                with store.db:
                    store.db.execute("UPDATE jobs SET state='running',runs=runs+1,updated=? WHERE attempt=?", (time.time(), attempt))
                try:
                    result = model(job, held)
                except Exception as exc:
                    reason = 'model_timeout' if isinstance(exc, TimeoutError) else 'invalid_result' if isinstance(exc, (ValueError, KeyError)) else 'hermes_failed'
                    store.record(attempt, 'fail', {'reason': reason})
                else:
                    illustration = lane == 'illustration'
                    store.record(attempt, 'complete', {
                        'result': result,
                        'model': runner.ILLUSTRATION_MODEL + ' / Claude Code' if illustration else runner.MODEL + ' / Hermes / openai-codex',
                        'promptVersion': runner.ILLUSTRATION_VERSION if illustration else runner.VERSION,
                    })
        store.flush(api, lane)


def lane_for(kind):
    return next(lane for lane, types in LANES.items() if kind in types)


class Store:
    def __init__(self, private):
        os.umask(0o077)
        self.private = Path(private).absolute()
        self.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.private.is_symlink() or self.private.stat().st_uid != os.getuid():
            raise ValueError('unsafe_state_directory')
        self.private.chmod(0o700)
        path = self.private / 'jobs.sqlite3'
        if path.is_symlink():
            raise ValueError('unsafe_database')
        self.db = sqlite3.connect(path, timeout=10)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS jobs (
                attempt INTEGER PRIMARY KEY, id TEXT NOT NULL, type TEXT NOT NULL,
                lane TEXT NOT NULL, token TEXT NOT NULL, job TEXT NOT NULL,
                lease_until REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                runs INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
                updated REAL NOT NULL, error TEXT, UNIQUE(id, token)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                attempt INTEGER PRIMARY KEY REFERENCES jobs(attempt),
                action TEXT NOT NULL, body TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', tries INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
        ''')

    def record(self, attempt, action, body):
        if action not in ('complete', 'fail'):
            raise ValueError('invalid_action')
        body = dict(body, leaseToken=self.get(attempt)['token'])
        with self.db:
            self.db.execute('INSERT INTO outbox(attempt,action,body) VALUES (?,?,?)',
                            (attempt, action, json.dumps(body, ensure_ascii=False)))
            self.db.execute("UPDATE jobs SET state='outbox',updated=? WHERE attempt=?",
                            (time.time(), attempt))

    def flush(self, api, lane=None):
        with lock(self.private, 'outbox') as held:
            if held is not None:
                self._flush_locked(api, lane)

    def _flush_locked(self, api, lane):
        # At most two sends per tick, one active outcome per lane. No model work here.
        rows = self.db.execute('''SELECT o.*,j.id FROM outbox o JOIN jobs j USING(attempt)
            WHERE o.state='pending' AND (? IS NULL OR j.lane=?)
            ORDER BY o.attempt LIMIT 2''', (lane, lane)).fetchall()
        for row in rows:
            error = None
            try:
                api('/api/jobs/runner/' + row['id'] + '/' + row['action'], json.loads(row['body']))
            except Exception as exc:
                code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
                error = 'http_' + str(code) if code else 'transport_error'
                state = 'lease_lost' if code == 409 else 'rejected' if code in (400, 413) and row['action'] == 'complete' else 'pending'
            else:
                state = 'sent'
            with self.db:
                self.db.execute('UPDATE outbox SET state=?,tries=tries+1,last_error=? WHERE attempt=?',
                                (state, error, row['attempt']))
                if state != 'pending':
                    final = ('completed' if row['action'] == 'complete' else 'failed') if state == 'sent' else state
                    self.db.execute('UPDATE jobs SET state=?,updated=?,error=? WHERE attempt=?',
                                    (final, time.time(), error, row['attempt']))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    def save_claim(self, job):
        if not re.fullmatch('[a-zA-Z0-9-]{1,100}', job['id']):
            raise ValueError('invalid_id')
        lane = lane_for(job['type'])
        if not isinstance(job['leaseToken'], str) or not job['leaseToken']:
            raise ValueError('invalid_lease')
        now = time.time()
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO jobs
                (id,type,lane,token,job,lease_until,created,updated)
                VALUES (?,?,?,?,?,?,?,?)''',
                (job['id'], job['type'], lane, job['leaseToken'], json.dumps(job),
                 float(job['lease_until']), now, now))
        return self.db.execute('SELECT attempt FROM jobs WHERE id=? AND token=?',
                               (job['id'], job['leaseToken'])).fetchone()[0]

    def get(self, attempt):
        row = dict(self.db.execute('SELECT * FROM jobs WHERE attempt=?', (attempt,)).fetchone())
        row['job'] = json.loads(row['job'])
        return row

    def status(self):
        return [dict(row) for row in self.db.execute('''SELECT j.attempt,id,type,lane,
            j.state,runs,lease_until,created,updated,error,
            o.state AS delivery_state,COALESCE(o.tries,0) AS delivery_tries,
            o.last_error AS delivery_error FROM jobs j LEFT JOIN outbox o USING(attempt)
            ORDER BY j.attempt''')]


def load_api(config_path, allow_loopback=False):
    config = json.loads(Path(config_path).read_text())
    url = config['url']
    parsed = urllib.parse.urlsplit(url)
    local = (allow_loopback and parsed.scheme == 'http' and parsed.hostname == '127.0.0.1'
             and not parsed.username and not parsed.password and parsed.path in ('', '/')
             and not parsed.query and not parsed.fragment)
    if url != 'https://hermes-llm-jobs.kazumasa.workers.dev' and not local:
        raise ValueError('unexpected_endpoint')
    def api(path, data):
        request = urllib.request.Request(url.rstrip('/') + path,
            data=json.dumps(data, ensure_ascii=False).encode(), headers={
                'Authorization': 'Bearer ' + config['runnerKey'],
                'Content-Type': 'application/json', 'User-Agent': 'Hermes-Job-Dispatcher/1.0'})
        with api_deadline():
            with urllib.request.build_opener(runner.NoRedirect()).open(request, timeout=API_TIMEOUT) as response:
                return json.load(response)
    return api


def spawn_worker(private, config, lane, allow_loopback=False, script=None):
    command = [sys.executable, str(script or Path(__file__).resolve()), 'worker',
               '--lane', lane, '--state-dir', str(private), '--config', str(config)]
    if allow_loopback:
        command.append('--allow-loopback')
    fd = os.open(Path(private) / (lane + '.log'),
                 os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'ab') as log:
        os.fchmod(log.fileno(), 0o600)
        subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True, cwd=runner.ROOT)


def main(argv=None, *, worker_script=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('tick', 'worker', 'status'):
        sub = commands.add_parser(name)
        sub.add_argument('--state-dir', type=Path, default=runner.PRIVATE / 'dispatcher')
        sub.add_argument('--config', type=Path, default=runner.PRIVATE / 'config.json')
        sub.add_argument('--allow-loopback', action='store_true', help='allow an explicit localhost test API')
        if name == 'worker':
            sub.add_argument('--lane', choices=LANES, required=True)
    args = parser.parse_args(argv)
    try:
        with Store(args.state_dir) as store:
            if args.command == 'status':
                print(json.dumps({'jobs': store.status()}, ensure_ascii=False))
                return 0
            api = load_api(args.config, args.allow_loopback)
            if args.command == 'worker':
                worker(store, api, args.lane)
            else:
                tick(store, api, lambda lane: spawn_worker(store.private, args.config.absolute(), lane,
                     args.allow_loopback, worker_script), now=now)
        return 0
    except Exception as exc:
        # Never print API bodies, model exceptions, config contents or job payloads.
        error = {'error': 'dispatcher_failure', 'kind': type(exc).__name__}
        if isinstance(exc, urllib.error.HTTPError):
            error['status'] = exc.code
        print(json.dumps(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
