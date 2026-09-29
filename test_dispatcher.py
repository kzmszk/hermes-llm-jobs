
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

ROOT = Path(__file__).resolve().parent


class BaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.private = Path(self.tmp.name) / 'state'
        self.addCleanup(mock.patch.stopall)
        mock.patch('runner.run_model', side_effect=AssertionError('production model forbidden')).start()

    def module(self):
        self.assertTrue((ROOT / 'dispatcher.py').exists(), 'durable dispatcher is missing')
        import dispatcher
        return dispatcher

    def job(self, jid='a', kind='document.summarize', expiry=None):
        return {'id': jid, 'type': kind, 'version': 1, 'payload': {'text': 'SECRET INPUT'},
                'leaseToken': 'opaque/SECRET+=', 'lease_until': expiry or time.time() + 60}


class DispatcherTests(BaseTest):
    def test_claim_survives_reopen_and_status_hides_private_data(self):
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            self.assertEqual(store.save_claim(self.job()), attempt)
        with d.Store(self.private) as store:
            self.assertEqual(store.status()[0]['state'], 'queued')
            self.assertNotIn('SECRET', json.dumps(store.status()))
            self.assertEqual(store.get(attempt)['job']['leaseToken'], 'opaque/SECRET+=')
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.private / 'jobs.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_durable_outcome_retries_same_body_even_after_expiry(self):
        d = self.module()
        self.assertTrue(hasattr(d.Store, 'record'), 'durable outbox is missing')
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job(expiry=time.time() - 1))
            store.record(attempt, 'complete', {'result': {'summary': 'SECRET RESULT'}})
        calls = []
        def api(path, body):
            calls.append((path, body))
            if len(calls) == 1:
                raise OSError('secret network detail')
            return {'ok': True, 'duplicate': True}
        with d.Store(self.private) as store:
            store.flush(api)
            self.assertEqual(store.get(attempt)['state'], 'outbox')
            store.flush(api)
            self.assertEqual(store.get(attempt)['state'], 'completed')
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(store.db.execute('SELECT state,tries FROM outbox').fetchone()[:], ('sent', 2))
            self.assertNotIn('SECRET', json.dumps(store.status()))

    def test_conflict_is_terminal_and_preserves_outcome(self):
        import urllib.error
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            store.record(attempt, 'fail', {'reason': 'model_timeout'})
            def api(path, body):
                raise urllib.error.HTTPError(path, 409, 'conflict', {}, None)
            store.flush(api)
            self.assertEqual(store.get(attempt)['state'], 'lease_lost')
            row = store.db.execute('SELECT state,body FROM outbox').fetchone()
            self.assertEqual(row['state'], 'lease_lost')
            self.assertIn('model_timeout', row['body'])
            store.flush(lambda *args: self.fail('terminal outcome retried'))

    def test_tick_claims_once_persists_before_spawn_and_excludes_busy_lane(self):
        import datetime
        d = self.module()
        self.assertTrue(hasattr(d, 'tick'), 'tick is missing')
        calls, spawned = [], []
        with d.Store(self.private) as store:
            def api(path, body):
                calls.append((path, body))
                return {'job': self.job('art', 'illustration.svg') if len(calls) == 1 else self.job('text') if len(calls) == 2 else None}
            def spawn(lane):
                self.assertTrue(any(r['lane'] == lane and r['state'] == 'queued' for r in store.status()))
                spawned.append(lane)
            day = datetime.datetime(2026, 1, 1, 12, tzinfo=d.JST)
            d.tick(store, api, spawn, now=day)
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(calls[0][1]['types']), {'quiz.grade', 'document.summarize', 'illustration.svg', 'video.generate'})
            d.tick(store, api, spawn, now=day)
            self.assertEqual(calls[1][1]['types'], ['quiz.grade', 'document.summarize', 'video.generate'])
            d.tick(store, api, spawn, now=day)
            self.assertEqual(calls[2][1]['types'], ['video.generate'], 'with grading and drawing busy only the video lane is still open')
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(store.status()), 2)

    def test_worker_records_result_before_send_with_pinned_metadata(self):
        d = self.module()
        self.assertTrue(hasattr(d, 'worker'), 'worker is missing')
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            def model(job, fd):
                self.assertEqual(store.get(attempt)['state'], 'running')
                return {'summary': 'done', 'keyPoints': []}
            def api(path, body):
                self.assertEqual(store.get(attempt)['state'], 'outbox')
                self.assertEqual(body['model'], 'gpt-6-luna / Hermes / openai-codex')
                self.assertEqual(body['promptVersion'], 'hermes-jobs-v1')
                self.assertEqual(body['leaseToken'], 'opaque/SECRET+=')
            d.worker(store, api, 'common', model=model)
            self.assertEqual(store.get(attempt)['state'], 'completed')
            self.assertEqual(store.get(attempt)['runs'], 1)

    def test_worker_expiry_crash_and_model_failure_lifecycle(self):
        d = self.module()
        for case in ('expired', 'crashed', 'invalid', 'timeout', 'runtime'):
            with self.subTest(case=case), d.Store(self.private / case) as store:
                attempt = store.save_claim(self.job(expiry=time.time() - 1 if case == 'expired' else None))
                if case == 'crashed':
                    with store.db:
                        store.db.execute("UPDATE jobs SET state='running',runs=1")
                def model(job, fd):
                    if case in ('expired', 'crashed'):
                        self.fail('expired or crashed work must not run again')
                    raise {'invalid': ValueError, 'timeout': TimeoutError, 'runtime': RuntimeError}[case]('SECRET')
                sent = []
                d.worker(store, lambda path, body: sent.append((path, body)), 'common', model=model)
                self.assertEqual(store.get(attempt)['state'], 'lease_lost' if case == 'expired' else 'failed')
                if case == 'expired':
                    self.assertEqual(sent, [])
                else:
                    self.assertTrue(sent[0][0].endswith('/fail'))
                    self.assertEqual(sent[0][1]['reason'], {'crashed': 'runner_error', 'invalid': 'invalid_result', 'timeout': 'model_timeout', 'runtime': 'hermes_failed'}[case])
                self.assertNotIn('SECRET', json.dumps(store.status()))

    def test_outbox_and_recovery_spawn_lock_boundaries(self):
        import datetime
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            store.record(attempt, 'fail', {'reason': 'runner_error'})
            with d.lock(self.private, 'outbox'):
                store.flush(lambda *args: self.fail('outbox lock ignored'))
            self.assertEqual(store.db.execute('SELECT tries FROM outbox').fetchone()[0], 0)
            store.flush(lambda *args: None)
            store.save_claim(self.job('next'))
            spawned = []
            def spawn(lane):
                with d.lock(self.private, lane) as fd:
                    self.assertIsNotNone(fd, 'probe lock must be released before spawning')
                spawned.append(lane)
            night = datetime.datetime(2026, 1, 1, 2, tzinfo=d.JST)
            d.tick(store, lambda *args: self.fail('night acquisition'), spawn, now=night)
            self.assertEqual(spawned, ['common'])
            with d.lock(self.private, 'tick'):
                d.tick(store, lambda *args: self.fail('duplicate tick'), lambda *args: self.fail('duplicate spawn'))

    def test_model_adapter_inherits_lane_lock_and_caps_lease_runtime(self):
        import signal
        d = self.module()
        self.assertTrue(hasattr(d, 'execute_model'), 'lease-aware model adapter is missing')
        child = None
        def fake(job):
            nonlocal child
            child = d.runner.subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'],
                stdout=subprocess.PIPE, start_new_session=True)
            try:
                child.communicate(timeout=600)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.communicate()
                raise TimeoutError('model_timeout')
        with d.Store(self.private), d.lock(self.private, 'common') as fd:
            with mock.patch.object(d.runner, 'run_model', side_effect=fake):
                start = time.monotonic()
                with self.assertRaises(TimeoutError):
                    d.execute_model(self.job(expiry=time.time() + 0.15), fd)
                self.assertLess(time.monotonic() - start, 1)
            self.assertIsNotNone(child)
            self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertIs(d.runner.subprocess, subprocess)

    def test_status_exposes_delivery_attempts_not_secrets_and_retains_new_leases(self):
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            store.record(attempt, 'complete', {'result': {'summary': 'SECRET'}})
            def offline(*args):
                raise OSError('SECRET credential')
            store.flush(offline)
            status = store.status()[0]
            self.assertEqual(status.get('delivery_tries'), 1)
            self.assertEqual(status.get('delivery_error'), 'transport_error')
            store.flush(lambda *args: None)
            job = self.job()
            job['leaseToken'] = 'another opaque token'
            store.save_claim(job)
            self.assertEqual([r['state'] for r in store.status()], ['completed', 'queued'])
            self.assertNotIn('SECRET', json.dumps(store.status()))

    def test_broker_rejected_result_is_retained_without_blocking_lane_forever(self):
        import datetime
        import urllib.error
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            result = {'summary': '\U0001f600' * 2100, 'keyPoints': []}
            store.record(attempt, 'complete', {'result': result})
            def rejected(path, body):
                raise urllib.error.HTTPError(path, 400, 'invalid result', None, None)
            store.flush(rejected)
            self.assertEqual(store.get(attempt)['state'], 'rejected')
            row = store.db.execute('SELECT state,body FROM outbox').fetchone()
            self.assertEqual(row['state'], 'rejected')
            self.assertEqual(json.loads(row['body'])['result'], result)
            claims = []
            def api(path, body):
                self.assertTrue(path.endswith('/claim'))
                claims.append(body)
                return {'job': None}
            d.tick(store, api, lambda *args: self.fail('unexpected model'),
                now=datetime.datetime(2026, 1, 1, 12, tzinfo=d.JST))
            self.assertIn('document.summarize', claims[0]['types'])

    def test_oversized_completion_is_rejected_preserving_history_and_freeing_lane(self):
        import datetime
        import urllib.error
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            result = {'summary': 'x\x01' * 2000, 'keyPoints': ['x\x01' * 1000] * 4}
            store.record(attempt, 'complete', {'result': result})
            body = store.db.execute('SELECT body FROM outbox').fetchone()['body']
            self.assertGreater(len(body.encode('utf-8')), 32768)
            def rejected(path, payload):
                self.assertTrue(path.endswith('/complete'))
                self.assertEqual(payload, json.loads(body))
                raise urllib.error.HTTPError(path, 413, 'body too large', None, None)
            store.flush(rejected)
            self.assertEqual(store.get(attempt)['state'], 'rejected')
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'rejected')
            row = store.db.execute('SELECT state,body,tries,last_error FROM outbox').fetchone()
            self.assertEqual(tuple(row), ('rejected', body, 1, 'http_413'))
            self.assertEqual(store.get(attempt)['job'], self.job(expiry=store.get(attempt)['job']['lease_until']))
            store.flush(lambda *args: self.fail('terminal outcome retried'))
            claims = []
            def api(path, payload):
                self.assertTrue(path.endswith('/claim'))
                claims.append(payload)
                return {'job': None}
            d.tick(store, api, lambda *args: self.fail('unexpected model'),
                now=datetime.datetime(2026, 1, 1, 12, tzinfo=d.JST))
            self.assertEqual(len(claims), 1)
            self.assertIn('document.summarize', claims[0]['types'])
            self.assertEqual(store.status()[0]['delivery_error'], 'http_413')
            self.assertEqual(store.status()[0]['delivery_tries'], 1)


class VideoTests(BaseTest):
    """video.generate: its own lane, its own budget and labels, and a runner that hands a visitor's text over as a file."""
    FAKE = r"""
import fs from 'node:fs';
const a = process.argv.slice(2);
const req = JSON.parse(fs.readFileSync(a[a.indexOf('--input') + 1], 'utf8'));
const id = a[a.indexOf('--id') + 1];
fs.writeFileSync(new URL('./argv.txt', import.meta.url), a.join(' '));
fs.writeFileSync(new URL('./request.txt', import.meta.url), JSON.stringify(req));
fs.writeFileSync(new URL('./env.txt', import.meta.url), JSON.stringify(process.env));
const good = { title: 'てすと', subtitle: 'さぶ', seconds: 120, width: 1920, height: 1080, style: 'podcast-duo',
  chapters: [{ t: 0, title: 'イントロ' }, { t: 5, title: 'ひとつめ' }], video: `movies/${id}/video.mp4`, poster: `movies/${id}/poster.webp`, bytes: 1000,
  qa: { status: 'PASS', checks: 18, passed: 18, measuredSec: 120, overlaps: 0, minGapSec: 0.3, lufs: -16, secret: 'dropped' } };
console.log('render 50%');
const mode = req.theme.split(' ')[0];
if (mode === 'ok') console.log('AUTO_MOVIE_RESULT ' + JSON.stringify(good));
else if (mode === 'reject') { console.error('theme_rejected'); process.exit(3); }
else if (mode === 'crash') process.exit(1);
else if (mode === 'hang') setTimeout(() => {}, 60000);
else if (mode === 'badjson') console.log('AUTO_MOVIE_RESULT {nope');
else if (mode === 'foreign') console.log('AUTO_MOVIE_RESULT ' + JSON.stringify({ ...good, video: 'movies/someone-else/video.mp4' }));
else if (mode === 'silent') console.log('done, but no result line');
"""
    JID = 'abcd1234-ef56-4789-a012-345678901234'

    def fake_movie(self, name='fake'):
        root = Path(self.tmp.name) / name
        (root / 'bin').mkdir(parents=True)
        (root / 'bin' / 'auto-movie.mjs').write_text(self.FAKE)
        import runner
        mock.patch.object(runner, 'AUTO_MOVIE', root).start()
        mock.patch.object(runner, 'PRIVATE', Path(self.tmp.name) / 'private').start()
        mock.patch.object(runner, 'VIDEO_LOGS', Path(self.tmp.name) / 'private' / 'video-logs').start()
        return root

    def job(self, theme='ok computer', **extra):
        return {'id': self.JID, 'type': 'video.generate', 'version': 1, 'payload': {'theme': theme, 'minutes': 2, **extra},
                'leaseToken': 'opaque', 'lease_until': time.time() + 3000}

    def test_video_has_its_own_lane_budget_and_labels(self):
        d = self.module()
        self.assertEqual(d.lane_for('video.generate'), 'video')
        self.assertIn('video.generate', d.LANES['video'])
        self.assertNotIn('video.generate', d.LANES['common'])
        self.assertEqual(d.BUDGET['video.generate'], d.runner.VIDEO_TIMEOUT)
        self.assertLess(d.runner.VIDEO_TIMEOUT, 2700, 'shorter than the broker lease')
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
            sent = []
            d.worker(store, lambda path, body: sent.append((path, body)), 'video', model=lambda job, fd: {'ok': True})
            self.assertEqual(store.get(attempt)['state'], 'completed')
            self.assertEqual(sent[0][1]['model'], 'claude-opus-5-5 / VOICEVOX / HyperFrames')
            self.assertEqual(sent[0][1]['promptVersion'], 'auto-movie-v1')

    def test_a_running_video_does_not_hold_up_grading_or_drawing(self):
        import datetime
        d = self.module()
        with d.Store(self.private) as store, d.lock(self.private, 'video') as held:
            self.assertIsNotNone(held)
            calls = []
            def api(path, body):
                calls.append(body)
                return {'job': None}
            d.tick(store, api, lambda lane: None, now=datetime.datetime(2026, 1, 1, 12, tzinfo=d.JST))
            self.assertEqual(set(calls[0]['types']), {'quiz.grade', 'document.summarize', 'illustration.svg'})

    def test_make_video_hands_text_over_as_a_file_and_returns_only_known_fields(self):
        root = self.fake_movie()
        import runner
        theme = "ok ' ; rm -rf ~ $(reboot) \u3042"
        result = runner.make_video(self.job(theme, notes='数字は\nそのまま使う'))
        self.assertEqual(result['video'], 'movies/%s/video.mp4' % self.JID)
        self.assertEqual(result['poster'], 'movies/%s/poster.webp' % self.JID)
        self.assertEqual(result['qa'], {'status': 'PASS', 'checks': 18, 'passed': 18, 'measuredSec': 120, 'overlaps': 0, 'minGapSec': 0.3, 'lufs': -16})
        self.assertEqual(result['chapters'], [{'t': 0, 'title': 'イントロ'}, {'t': 5, 'title': 'ひとつめ'}])
        argv = (root / 'bin' / 'argv.txt').read_text()
        self.assertNotIn('rm -rf', argv, 'the visitor\'s text never appears in the command line')
        self.assertEqual(json.loads((root / 'bin' / 'request.txt').read_text()), {'theme': theme, 'minutes': 2, 'notes': '数字は\nそのまま使う'})
        log = Path(self.tmp.name) / 'private' / 'video-logs' / (self.JID + '.log')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertIn('render 50%', log.read_text())

    def test_make_video_gets_a_minimal_environment_with_a_usable_path(self):
        root = self.fake_movie()
        import runner
        bare = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'AWS_SECRET_ACCESS_KEY': 'must-not-leak', 'CLOUDFLARE_API_TOKEN': 'must-not-leak', 'LANG': 'C.UTF-8'}
        with mock.patch.dict(os.environ, bare, clear=True):
            runner.make_video(self.job())
        seen = json.loads((root / 'bin' / 'env.txt').read_text())
        self.assertNotIn('AWS_SECRET_ACCESS_KEY', seen)
        self.assertNotIn('CLOUDFLARE_API_TOKEN', seen)
        self.assertEqual(seen['LANG'], 'C.UTF-8')
        path = seen['PATH'].split(os.pathsep)
        self.assertIn(str(Path.home() / '.local/bin'), path, 'claude and node live there')
        self.assertIn('/usr/bin', path)

    def test_make_video_failures_map_to_the_brokers_reasons(self):
        self.fake_movie()
        import runner
        with self.assertRaises(ValueError):                # the model declined the theme: retrying will not help
            runner.make_video(self.job('reject this'))
        with self.assertRaises(RuntimeError):
            runner.make_video(self.job('crash now'))
        with self.assertRaises(RuntimeError):
            runner.make_video(self.job('silent run'))
        with self.assertRaises(ValueError):
            runner.make_video(self.job('badjson please'))
        with self.assertRaises(ValueError):                # pointing at somebody else's files is refused
            runner.make_video(self.job('foreign files'))

    def test_make_video_stops_a_hung_run(self):
        self.fake_movie()
        import runner
        mock.patch.object(runner, 'VIDEO_TIMEOUT', 1).start()
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            runner.make_video(self.job('hang forever'))
        self.assertLess(time.monotonic() - start, 6)

    def test_make_video_rejects_bad_requests_before_starting_anything(self):
        root = self.fake_movie()
        import runner
        for bad in (dict(theme=''), dict(theme='x' * 61), dict(theme='a<b'), dict(theme='ok', minutes=9), dict(theme='ok', notes=5), dict(theme='ok', notes='x' * 1501)):
            job = self.job()
            job['payload'].update(bad)
            with self.assertRaises(ValueError):
                runner.make_video(job)
        job = self.job()
        job['id'] = '../../etc'
        with self.assertRaises(ValueError):
            runner.make_video(job)
        self.assertFalse((root / 'bin' / 'argv.txt').exists(), 'nothing was started')

    def test_video_result_rules(self):
        import runner
        good = {'title': 't', 'subtitle': 's', 'seconds': 100, 'width': 1920, 'height': 1080, 'style': 'monologue', 'bytes': 5,
                'video': 'movies/j/video.mp4', 'poster': 'movies/j/poster.webp', 'chapters': [{'t': 0, 'title': 'a'}, {'t': 3.5, 'title': 'b'}]}
        self.assertEqual(runner.validate_video(good, 'j')['seconds'], 100)
        for key, value in [('title', ''), ('seconds', 5), ('seconds', True), ('width', 1920.5), ('style', 'other'), ('bytes', 0), ('video', 'movies/x/video.mp4'),
                           ('poster', 'https://evil.example/p.webp'), ('chapters', [{'t': 0, 'title': 'a'}]), ('chapters', [{'t': 5, 'title': 'a'}, {'t': 5, 'title': 'b'}]),
                           ('chapters', [{'t': i, 'title': 'x'} for i in range(17)]), ('qa', {'status': 'FAIL', 'checks': 1, 'passed': 0})]:
            with self.assertRaises(ValueError, msg=(key, value)):
                runner.validate_video(dict(good, **{key: value}), 'j')


class SubprocessTests(BaseTest):
    def setUp(self):
        super().setUp()
        self.requests = []
        self.jobs = []
        self.response_codes = []
        self.claim_delay = 0
        self.drip = 0
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, data))
                if self.path.endswith('/claim'):
                    time.sleep(outer.claim_delay)
                    job = next((j for j in outer.jobs if j['type'] in data['types']), None)
                    if job:
                        outer.jobs.remove(job)
                    body = {'job': job}
                else:
                    body = {'ok': True}
                encoded = json.dumps(body).encode()
                code = outer.response_codes.pop(0) if outer.response_codes and not self.path.endswith('/claim') else 200
                self.send_response(code)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                try:
                    for byte in encoded:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        if outer.drip:
                            time.sleep(outer.drip)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config = Path(self.tmp.name) / 'config.json'
        self.config.write_text(json.dumps({'url': 'http://127.0.0.1:' + str(self.server.server_port), 'runnerKey': 'TEST ONLY'}))
        self.harness = Path(self.tmp.name) / 'harness.py'
        self.harness.write_text('''import datetime, json, os, sys, time
from pathlib import Path
sys.path.insert(0, %r)
import dispatcher as d
def fake(job):
    marker = Path(%r) / (job['id'] + '.started')
    marker.write_text(json.dumps({'pid': os.getpid(), 'sid': os.getsid(0)}))
    if job['payload'].get('child'):
        delay = 60 if job['payload'].get('hang') else 1.2
        p = d.runner.subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(' + str(delay) + ')'], start_new_session=True, stdout=d.runner.subprocess.DEVNULL, stderr=d.runner.subprocess.DEVNULL)
        marker.with_suffix('.child').write_text(str(p.pid))
        p.communicate(timeout=10)
    else:
        time.sleep(job['payload'].get('delay', 0.01))
    return {'svg': '<svg></svg>'} if job['type'] == 'illustration.svg' else {'summary': 'test', 'keyPoints': []}
d.runner.run_model = fake
original_api = d.load_api
def api_factory(*args, **kwargs):
    api = original_api(*args, **kwargs)
    def call(path, data):
        result = api(path, data)
        if os.environ.get('FAKE_CRASH_AFTER_ACK') and path.endswith('/complete'):
            os._exit(19)
        return result
    return call
d.load_api = api_factory
sys.exit(d.main(worker_script=__file__, now=datetime.datetime(2026, 1, 1, 12, tzinfo=d.JST)))
''' % (str(ROOT), self.tmp.name))

    def command(self, command, *args, real=False):
        return [sys.executable, str(ROOT / 'dispatcher.py' if real else self.harness), command,
                '--state-dir', str(self.private), '--config', str(self.config), '--allow-loopback', *args]

    def cli(self, command, *args, real=False):
        return subprocess.run(self.command(command, *args, real=real), capture_output=True, text=True, timeout=10)

    def wait_for(self, predicate, timeout=8):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(0.02)
        self.fail('condition not reached')

    def test_tick_detaches_long_illustration_and_quick_common(self):
        d = self.module()
        self.assertTrue(hasattr(d, 'main'), 'CLI is missing')
        art = self.job('art', 'illustration.svg')
        art['payload'] = {'delay': 2}
        self.jobs = [art, self.job('text')]
        start = time.monotonic()
        result = self.cli('tick')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertLess(time.monotonic() - start, 1)
        self.wait_for(lambda: (Path(self.tmp.name) / 'art.started').exists())
        pid = json.loads((Path(self.tmp.name) / 'art.started').read_text())
        self.assertEqual(pid['pid'], pid['sid'])
        result = self.cli('tick')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.wait_for(lambda: any(path.endswith('/text/complete') for path, body in self.requests))
        self.assertFalse(any(path.endswith('/art/complete') for path, body in self.requests))
        self.wait_for(lambda: any(path.endswith('/art/complete') for path, body in self.requests))
        status = self.cli('status', real=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn('SECRET', status.stdout)
        self.assertEqual([r['state'] for r in json.loads(status.stdout)['jobs']], ['completed', 'completed'])
        self.assertEqual(len([r for r in self.requests if r[0].endswith('/claim')]), 2)
        for name in ('common.log', 'illustration.log'):
            self.assertEqual((self.private / name).stat().st_mode & 0o777, 0o600)


    def test_empty_noop_and_overlapping_ticks(self):
        self.claim_delay = 0.3
        first = subprocess.Popen(self.command('tick'), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: first.poll() is None and first.kill())
        self.wait_for(lambda: len(self.requests) == 1)
        second = self.cli('tick')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.communicate(timeout=5), ('', ''))
        self.assertEqual(first.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(list(self.private.glob('*.log')))
        self.assertEqual(json.loads(self.cli('status', real=True).stdout), {'jobs': []})

    def test_api_has_total_deadline_not_just_socket_idle_timeout(self):
        d = self.module()
        self.assertTrue(hasattr(d, 'API_TIMEOUT'), 'bounded API deadline is missing')
        self.drip = 0.03
        api = d.load_api(self.config, allow_loopback=True)
        with mock.patch.object(d, 'API_TIMEOUT', 0.12):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                api('/api/jobs/runner/claim', {'types': ['quiz.grade']})
            self.assertLess(time.monotonic() - start, 0.3)

    def test_worker_crash_leaves_child_lock_and_recovers_without_rerun(self):
        import signal
        d = self.module()
        job = self.job('orphan')
        job['payload'] = {'child': True}
        with d.Store(self.private) as store:
            attempt = store.save_claim(job)
        worker = subprocess.Popen(self.command('worker', '--lane', 'common'), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        marker = Path(self.tmp.name) / 'orphan.child'
        self.wait_for(marker.exists)
        worker.kill()
        worker.communicate(timeout=5)
        child_pid = int(marker.read_text())
        self.addCleanup(lambda: self.kill_if_alive(child_pid))
        self.assertEqual(self.cli('worker', '--lane', 'common').returncode, 0)
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'running')
            self.assertEqual(store.get(attempt)['runs'], 1)
        self.assertEqual(self.requests, [])
        def released():
            with d.lock(self.private, 'common') as fd:
                return fd is not None
        self.wait_for(released)
        self.assertEqual(self.cli('worker', '--lane', 'common').returncode, 0)
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'failed')
            self.assertEqual(store.get(attempt)['runs'], 1)
        self.assertEqual(self.requests[0][1]['reason'], 'runner_error')

    def test_orphaned_hung_model_has_independent_lease_watchdog(self):
        d = self.module()
        job = self.job('hung', expiry=time.time() + 0.7)
        job['payload'] = {'child': True, 'hang': True}
        with d.Store(self.private) as store:
            attempt = store.save_claim(job)
        worker = subprocess.Popen(self.command('worker', '--lane', 'common'), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: worker.poll() is None and worker.kill())
        marker = Path(self.tmp.name) / 'hung.child'
        self.wait_for(marker.exists)
        child_pid = int(marker.read_text())
        self.addCleanup(lambda: self.kill_if_alive(child_pid))
        worker.kill()
        worker.wait(timeout=5)
        def released():
            with d.lock(self.private, 'common') as fd:
                return fd is not None
        self.wait_for(released, timeout=1.5)
        self.assertEqual(self.cli('worker', '--lane', 'common').returncode, 0)
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'lease_lost')
            self.assertEqual(store.get(attempt)['runs'], 1)

    @staticmethod
    def kill_if_alive(pid):
        import signal
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_worker_http_retry_does_not_repeat_model(self):
        d = self.module()
        self.response_codes = [503, 200]
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
        self.assertEqual(self.cli('worker', '--lane', 'common').returncode, 0)
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'outbox')
        self.assertEqual(self.cli('tick').returncode, 0)
        sends = [r for r in self.requests if r[0].endswith('/complete')]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0], sends[1])
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'completed')
            self.assertEqual(store.get(attempt)['runs'], 1)


    def test_crash_after_remote_ack_resends_recorded_result_after_expiry(self):
        d = self.module()
        with d.Store(self.private) as store:
            attempt = store.save_claim(self.job())
        result = subprocess.run(self.command('worker', '--lane', 'common'),
            env=dict(os.environ, FAKE_CRASH_AFTER_ACK='1'), capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 19)
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'outbox')
            with store.db:
                store.db.execute('UPDATE jobs SET lease_until=?', (time.time() - 1,))
        self.assertEqual(self.cli('tick').returncode, 0)
        sends = [r for r in self.requests if r[0].endswith('/complete')]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0], sends[1])
        with d.Store(self.private) as store:
            self.assertEqual(store.get(attempt)['state'], 'completed')
            self.assertEqual(store.get(attempt)['runs'], 1)


if __name__ == '__main__':
    unittest.main()
