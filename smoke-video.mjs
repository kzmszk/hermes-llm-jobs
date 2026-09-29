// Checks the broker's job types (video.generate in detail, illustration.svg as a regression) against a THROW-AWAY local broker:
// it applies the migrations to a temporary database, registers two test apps, starts `wrangler dev` on a free port and removes everything
// afterwards. It never touches the deployed service or the real database.
//
//   node smoke-video.mjs
import { spawn, execFileSync } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const ROOT = path.dirname(new URL(import.meta.url).pathname);
const WRANGLER = path.join(ROOT, 'node_modules', 'wrangler', 'bin', 'wrangler.js');
const state = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-jobs-smoke-'));
const wr = (...args) => execFileSync(process.execPath, [WRANGLER, ...args], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
const sha = (k) => crypto.createHash('sha256').update(k).digest('hex');
const freePort = () => new Promise((res) => { const s = net.createServer().listen(0, '127.0.0.1', () => { const { port } = s.address(); s.close(() => res(port)); }); });

wr('d1', 'migrations', 'apply', 'DB', '--local', '--persist-to', state);
wr('d1', 'execute', 'DB', '--local', '--persist-to', state, '--command',
  `INSERT INTO llm_apps(id,key_hash,allowed_types) VALUES('auto-movie','${sha('test-app-key-movie')}','["video.generate"]'),('sketch','${sha('test-app-key-sketch')}','["illustration.svg"]')`);
const port = await freePort();
const dev = spawn(process.execPath, [WRANGLER, 'dev', '-c', 'wrangler.jsonc', '--port', String(port), '--ip', '127.0.0.1', '--persist-to', state, '--var', 'JOB_RUNNER_KEY:test-runner-key'],
  { cwd: ROOT, stdio: 'ignore', detached: true });
const stop = () => { try { process.kill(-dev.pid, 'SIGKILL'); } catch { /* already gone */ } fs.rmSync(state, { recursive: true, force: true }); };
process.on('exit', stop);
const BASE = `http://127.0.0.1:${port}`;
for (let i = 0; ; i++) {
  try { if ((await fetch(BASE + '/health')).ok) break; } catch { /* not up yet */ }
  if (i > 120) throw new Error('the local broker did not start');
  await new Promise((r) => setTimeout(r, 500));
}

const H = (key) => ({ Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' });
const APP = H('test-app-key-movie'), SKETCH = H('test-app-key-sketch'), RUN = H('test-runner-key');
const call = async (path, headers, body) => {
  const r = await fetch(BASE + path, { method: body === undefined ? 'GET' : 'POST', headers, body: body === undefined ? undefined : JSON.stringify(body) });
  let j = null;
  try { j = await r.json(); } catch { /* not JSON */ }
  return { status: r.status, j };
};
const check = (name, cond, extra) => { console.log(`${cond ? 'ok  ' : 'FAIL'} ${name}${cond ? '' : '  ' + JSON.stringify(extra)}`); if (!cond) process.exitCode = 1; };
const job = (key, payload) => ({ type: 'video.generate', version: 1, idempotencyKey: key, payload });
const now = () => Math.floor(Date.now() / 1000);

/* ---- registration ---- */
const expected = {};   // job id → the payload the broker should hand to the runner (trimmed, notes only when given)
let r = await call('/api/jobs', APP, job('t1', { theme: '  三日坊主をやめる工夫 ', minutes: 2 }));
check('register → 201 pending', r.status === 201 && r.j.status === 'pending', r);
const first = r.j.id;
expected[first] = { theme: '三日坊主をやめる工夫', minutes: 2 };
r = await call('/api/jobs', APP, job('t1', { theme: '三日坊主をやめる工夫', minutes: 2 }));
check('the same request again → 200, same id', r.status === 200 && r.j.id === first, r);
r = await call('/api/jobs', APP, job('t1', { theme: '別の題材', minutes: 2 }));
check('same key, different input → 409', r.status === 409, r);
for (const [name, payload] of [
  ['theme too long', { theme: 'x'.repeat(61), minutes: 2 }], ['theme with <', { theme: 'a<b', minutes: 2 }], ['no theme', { minutes: 2 }],
  ['minutes 5', { theme: 'ok theme', minutes: 5 }], ['minutes as text', { theme: 'ok theme', minutes: '2' }],
  ['notes as number', { theme: 'ok theme', minutes: 2, notes: 5 }], ['notes too long', { theme: 'ok theme', minutes: 2, notes: 'x'.repeat(1501) }], ['notes with NUL', { theme: 'ok theme', minutes: 2, notes: 'a\u0000b' }],
  ['no payload', undefined],
]) {
  r = await call('/api/jobs', APP, job('bad-' + name, payload));
  check(`refused: ${name}`, r.status === 400, r);
}
r = await call('/api/jobs', SKETCH, job('t2', { theme: 'ok theme', minutes: 2 }));
check('an app without the type is refused', r.status === 400, r);

/* ---- the daily allowance is 3 videos per app; a resent request costs nothing ---- */
for (const k of ['t3', 't4']) { r = await call('/api/jobs', APP, job(k, { theme: 'テーマ ' + k, minutes: 3, notes: '使ってほしい事実' })); check(`register ${k}`, r.status === 201, r); expected[r.j.id] = { theme: 'テーマ ' + k, minutes: 3, notes: '使ってほしい事実' }; }
r = await call('/api/jobs', APP, job('t5', { theme: 'テーマ t5', minutes: 2 }));
check('the 4th new video today → 429 daily_limit', r.status === 429 && r.j?.error?.code === 'daily_limit', r);
r = await call('/api/jobs', APP, job('t3', { theme: 'テーマ t3', minutes: 3, notes: '使ってほしい事実' }));
check('…but resending an accepted one still works', r.status === 200, r);

/* ---- the runner claims it with a long lease ---- */
r = await call('/api/jobs/runner/claim', RUN, { types: ['video.generate'] });
check('claim → a video job', r.status === 200 && r.j.job?.type === 'video.generate' && expected[r.j.job.id], r);
const id = r.j.job.id;   // jobs created in the same second are claimed in no particular order
check('the payload is normalized (trimmed, notes only when given)', JSON.stringify(r.j.job.payload) === JSON.stringify(expected[id]), r.j.job.payload);
const lease = r.j.job?.lease_until - now();
check(`lease is 45 minutes (${lease}s)`, lease > 2690 && lease <= 2700, lease);
const token = r.j.job.leaseToken;

/* ---- results: only this job's own files, small numbers ---- */
const good = {
  title: '三日坊主をやめる工夫', subtitle: '小さく始める', seconds: 119.6, width: 1920, height: 1080, style: 'podcast-duo', bytes: 25000000,
  chapters: [{ t: 0, title: 'イントロ' }, { t: 4, title: 'はじめに' }, { t: 100, title: 'エンディング' }],
  video: `movies/${id}/video.mp4`, poster: `movies/${id}/poster.webp`,
  qa: { status: 'PASS', checks: 18, passed: 18, measuredSec: 120, overlaps: 0, minGapSec: 0.34, lufs: -16, secret: 'dropped' }, extra: 'dropped',
};
const complete = (result, t = token) => call(`/api/jobs/runner/${id}/complete`, RUN, { leaseToken: t, result, model: 'claude-opus-5-5 / VOICEVOX / HyperFrames', promptVersion: 'auto-movie-v1' });
const bads = {
  'another job\'s video': { video: 'movies/someone-else/video.mp4' }, 'another job\'s poster': { poster: `movies/${id}/../x.webp` }, 'a URL as the video': { video: 'https://evil.example/v.mp4' },
  'one chapter only': { chapters: [{ t: 0, title: 'a' }] }, 'chapters not ascending': { chapters: [{ t: 5, title: 'a' }, { t: 5, title: 'b' }] },
  'too many chapters': { chapters: Array.from({ length: 17 }, (_, i) => ({ t: i, title: 'x' })) }, 'unknown style': { style: 'movie' },
  'bytes 0': { bytes: 0 }, 'bytes 300MB': { bytes: 300 * 1024 * 1024 }, 'seconds 5': { seconds: 5 }, 'qa FAIL': { qa: { status: 'FAIL', checks: 18, passed: 3 } }, 'no title': { title: '' },
};
for (const [name, patch] of Object.entries(bads)) { r = await complete({ ...good, ...patch }); check(`result refused: ${name}`, r.status === 400, r); }
r = await complete(good, 'wrong-token');
check('a wrong lease token is refused', r.status === 409, r);
r = await complete(good);
check('a good result is accepted', r.status === 200 && r.j.ok, r);
r = await complete(good);
check('sending it again is harmless', r.status === 200 && r.j.duplicate === true, r);

/* ---- what the app reads back ---- */
r = await call(`/api/jobs/${id}`, APP);
const res = r.j?.result;
check('status completed', r.status === 200 && r.j.status === 'completed', r);
check('only the known fields survive', res && !('extra' in res) && !('secret' in res.qa) && res.qa.status === 'PASS' && res.video === good.video && res.chapters.length === 3, res);
check('the model and the prompt version are recorded', res?.model === 'claude-opus-5-5 / VOICEVOX / HyperFrames' && res?.promptVersion === 'auto-movie-v1', res);
r = await call(`/api/jobs/${id}`, SKETCH);
check('another app cannot read it', r.status === 404, r);

/* ---- a failed attempt goes back to the queue ---- */
r = await call('/api/jobs/runner/claim', RUN, { types: ['video.generate'] });
check('the next job can be claimed', r.status === 200 && r.j.job && r.j.job.id !== id && expected[r.j.job.id], r);
const id2 = r.j.job.id;
r = await call(`/api/jobs/runner/${id2}/fail`, RUN, { leaseToken: r.j.job.leaseToken, reason: 'invalid_result' });
check('fail → ok', r.status === 200, r);
r = await call(`/api/jobs/${id2}`, APP);
check('…and the job is pending again with its reason', r.j?.status === 'pending' && r.j.last_error === 'invalid_result', r);

/* ---- the older types still behave (illustration.svg shares the generalized daily allowance code) ---- */
r = await call('/api/jobs', SKETCH, { type: 'illustration.svg', version: 1, idempotencyKey: 'i1', payload: { subject: '縁側の柴犬' } });
check('illustration.svg: register → 201', r.status === 201, r);
const iid = r.j.id;
r = await call('/api/jobs', SKETCH, { type: 'illustration.svg', version: 1, idempotencyKey: 'i1', payload: { subject: '縁側の柴犬' } });
check('illustration.svg: resend → same id', r.status === 200 && r.j.id === iid, r);
r = await call('/api/jobs/runner/claim', RUN, { types: ['illustration.svg'] });
check('illustration.svg: claimed with the 20-minute lease', r.status === 200 && r.j.job?.id === iid && r.j.job.lease_until - now() > 1190 && r.j.job.lease_until - now() <= 1200, r);
r = await call(`/api/jobs/runner/${iid}/complete`, RUN, { leaseToken: r.j.job.leaseToken, result: { svg: '<svg viewBox="0 0 10 10"><path d="M0 0L5 5"/></svg>' }, model: 'm', promptVersion: 'v' });
check('illustration.svg: result accepted', r.status === 200, r);
r = await call('/api/jobs', APP, { type: 'nope.nothing', version: 1, idempotencyKey: 'n1', payload: {} });
check('an unknown type is refused', r.status === 400, r);

/* ---- the PC side, for real: dispatcher tick → a detached video worker → runner.make_video (a stand-in for auto_movie) → the result reaches the broker ---- */
const rig = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-video-rig-'));
fs.mkdirSync(path.join(rig, 'movie', 'bin'), { recursive: true });
fs.writeFileSync(path.join(rig, 'movie', 'bin', 'auto-movie.mjs'), `
import fs from 'node:fs';
const a = process.argv.slice(2);
const id = a[a.indexOf('--id') + 1];
const req = JSON.parse(fs.readFileSync(a[a.indexOf('--input') + 1], 'utf8'));
console.log('stand-in for auto_movie: ' + req.theme);
console.log('AUTO_MOVIE_RESULT ' + JSON.stringify({ title: 'スタンドイン', subtitle: 'テスト', seconds: 60, width: 1920, height: 1080, style: 'podcast-duo', bytes: 1234,
  chapters: [{ t: 0, title: 'イントロ' }, { t: 5, title: '本編' }], video: 'movies/' + id + '/video.mp4', poster: 'movies/' + id + '/poster.webp' }));
`);
fs.writeFileSync(path.join(rig, 'config.json'), JSON.stringify({ url: BASE, runnerKey: 'test-runner-key' }), { mode: 0o600 });
const pyenv = { ...process.env, AUTO_MOVIE_DIR: path.join(rig, 'movie'), HERMES_VIDEO_LOG_DIR: path.join(rig, 'logs') };
const py = (code) => execFileSync('python3', ['-c', code], { cwd: ROOT, env: pyenv, encoding: 'utf8' });
const dargs = `['--state-dir', ${JSON.stringify(path.join(rig, 'state'))}, '--config', ${JSON.stringify(path.join(rig, 'config.json'))}, '--allow-loopback']`;
py(`import dispatcher, datetime, sys; sys.exit(dispatcher.main(['tick'] + ${dargs}, now=datetime.datetime(2026, 1, 1, 12, tzinfo=dispatcher.JST)))`);
let row;
for (let i = 0; i < 60; i++) {
  await new Promise((r) => setTimeout(r, 1000));
  row = JSON.parse(py(`import dispatcher, sys; sys.exit(dispatcher.main(['status'] + ${dargs}))`)).jobs.find((j) => j.type === 'video.generate');
  if (row && ['completed', 'failed', 'rejected', 'lease_lost'].includes(row.state)) break;
}
check('the dispatcher claimed a video job and its worker finished it', row?.state === 'completed' && row.lane === 'video' && row.delivery_state === 'sent', row);
r = await call(`/api/jobs/${row.id}`, APP);
check('the broker has the result, labelled with the video model', r.j?.status === 'completed' && r.j.result?.title === 'スタンドイン' && r.j.result.model === 'claude-opus-5-5 / VOICEVOX / HyperFrames' && r.j.result.promptVersion === 'auto-movie-v1', r);
const logFile = path.join(rig, 'logs', row.id + '.log');
check('the run left a private log', fs.existsSync(logFile) && (fs.statSync(logFile).mode & 0o777) === 0o600 && /stand-in for auto_movie/.test(fs.readFileSync(logFile, 'utf8')), logFile);
fs.rmSync(rig, { recursive: true, force: true });

console.log(process.exitCode ? '\nSOME CHECKS FAILED' : '\nall checks passed');
stop();
process.exit(process.exitCode || 0);
