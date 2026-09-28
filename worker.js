// Standalone job broker. No quiz database or application-specific side effects.
import { body, equal, fail, hash, json, now, randomToken, limit } from './http.js';
const TYPES = ['quiz.grade', 'document.summarize', 'illustration.svg'];
const ILLUSTRATIONS_PER_DAY = 30;     // per app, counted per UTC day (resets 09:00 JST)
const LEASE = { 'illustration.svg': 1200 };   // seconds; everything else keeps 600
const str = (x, n) => typeof x === 'string' && x.trim().length > 0 && x.length <= n;
// The runner sanitizes the SVG against an allow-list; this is the broker's own last check.
const UNSAFE_SVG = /<script|<foreignObject|<iframe|<image|<a[\s>]|<!|<\?|\son[a-z]+\s*=|javascript:|data:|url\((?!#)/i;
function input(type, p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) fail(400,'payloadが必要です。');
  if (type === 'quiz.grade') {
    if (!str(p.question,4000) || !str(p.modelAnswer,8000) || !str(p.answer,4000) || !Array.isArray(p.rubric) || p.rubric.length !== 3 || p.rubric.some(x=>!str(x,1000))) fail(400,'採点の入力形式が不正です。');
    return { question:p.question,modelAnswer:p.modelAnswer,rubric:p.rubric,answer:p.answer };
  }
  if (type === 'illustration.svg') {
    if (!str(p.subject,60) || /[\u0000-\u001f\u007f<>{}`\\]/.test(p.subject)) fail(400,'subject（60文字まで、記号 <>{}`\\ なし）が必要です。');
    return { subject:p.subject.trim() };
  }
  if (!str(p.text,12000)) fail(400,'要約するtextが必要です（最大12,000文字）。');
  return { text:p.text };
}
function output(type, r) {
  if (!r || typeof r !== 'object' || Array.isArray(r)) fail(400,'resultが必要です。');
  if (type === 'quiz.grade') {
    if (!Array.isArray(r.criteria) || r.criteria.length!==3 || r.criteria.some((x,i)=>x.index!==i || !Number.isInteger(x.points) || x.points<0 || x.points>2 || !str(x.feedback,1000)) || !Number.isFinite(r.confidence) || r.confidence<0 || r.confidence>1 || !str(r.feedback,2500)) fail(400,'採点結果の形式が不正です。');
    return { criteria:r.criteria.map(({index,points,feedback})=>({index,points,feedback})),confidence:r.confidence,feedback:r.feedback };
  }
  if (type === 'illustration.svg') {
    if (!str(r.svg,24000) || !/^<svg[\s>]/.test(r.svg) || !/<\/svg>\s*$/.test(r.svg) || UNSAFE_SVG.test(r.svg)) fail(400,'SVGの形式が不正です。');
    if (r.memo != null && !str(r.memo,1000)) fail(400,'メモの形式が不正です。');
    return r.memo ? { svg:r.svg,memo:r.memo } : { svg:r.svg };
  }
  if (!str(r.summary,4000) || !Array.isArray(r.keyPoints) || r.keyPoints.length>8 || r.keyPoints.some(x=>!str(x,500))) fail(400,'要約結果の形式が不正です。');
  return { summary:r.summary,keyPoints:r.keyPoints };
}
async function auth(request, env, runner) {
  const raw=request.headers.get('authorization')||'';
  if (!raw.startsWith('Bearer ') || raw.length>300) fail(401,'APIキーが必要です。');
  const digest=await hash(raw.slice(7));
  if (runner) {
    if (!env.JOB_RUNNER_KEY || !equal(digest,await hash(env.JOB_RUNNER_KEY))) fail(401,'実行用キーが必要です。');
    return null;
  }
  const app=await env.DB.prepare('SELECT id,allowed_types FROM llm_apps WHERE key_hash=? AND enabled=1').bind(digest).first();
  if (!app) fail(401,'アプリ用キーが必要です。');
  return app;
}
export async function jobRoute(request, env) {
  const path=new URL(request.url).pathname, method=request.method, runner=path.startsWith('/api/jobs/runner/');
  const app=await auth(request,env,runner), time=now();
  if (!runner && method==='POST' && path==='/api/jobs') {
    await limit(env,'jobs:'+app.id,60,60);
    const d=await body(request);
    if (!TYPES.includes(d.type) || !JSON.parse(app.allowed_types).includes(d.type) || d.version!==1 || !str(d.idempotencyKey,160)) fail(400,'イベント種別・バージョン・重複防止キーを確認してください。');
    const payload=input(d.type,d.payload), fingerprint=await hash(JSON.stringify({type:d.type,version:1,payload})), id=crypto.randomUUID();
    if (d.type==='illustration.svg') {
      // A resent request (same key and input) returns its job without spending the daily allowance.
      const seen=await env.DB.prepare('SELECT id,input_hash,status FROM llm_jobs WHERE app_id=? AND idempotency_key=?').bind(app.id,d.idempotencyKey).first();
      if (seen) { if(seen.input_hash!==fingerprint)fail(409,'同じキーで異なる内容は登録できません。'); return json({id:seen.id,status:seen.status}); }
      try { await limit(env,'daily:'+app.id+':illustration.svg',ILLUSTRATIONS_PER_DAY,86400); }
      catch (e) { if (e.status===429) fail(429,'今日のイラストの受付は終わりました。','daily_limit'); throw e; }
    }
    await env.DB.prepare("INSERT INTO llm_jobs(id,app_id,type,version,idempotency_key,input_hash,payload_json,available_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(app_id,idempotency_key) DO NOTHING").bind(id,app.id,d.type,1,d.idempotencyKey,fingerprint,JSON.stringify(payload),time,time,time).run();
    const row=await env.DB.prepare('SELECT id,input_hash,status FROM llm_jobs WHERE app_id=? AND idempotency_key=?').bind(app.id,d.idempotencyKey).first();
    if(row.input_hash!==fingerprint)fail(409,'同じキーで異なる内容は登録できません。');
    return json({id:row.id,status:row.status},row.id===id?201:200);
  }
  if (!runner && method==='GET' && /^\/api\/jobs\/[a-zA-Z0-9-]+$/.test(path)) {
    const row=await env.DB.prepare('SELECT id,type,version,status,result_json,last_error,created_at,updated_at FROM llm_jobs WHERE id=? AND app_id=?').bind(path.split('/').at(-1),app.id).first();
    if(!row)fail(404,'ジョブが見つかりません。');
    const {result_json,...rest}=row; return json({...rest,result:result_json?JSON.parse(result_json):null});
  }
  if(runner && method==='POST' && path==='/api/jobs/runner/claim') {
    const d=await body(request);
    if (!Array.isArray(d.types) || !d.types.length || d.types.some(t=>!TYPES.includes(t))) fail(400,'処理可能なtypeを指定してください。');
    await env.DB.prepare("UPDATE llm_jobs SET status='failed',last_error='retry_exhausted',updated_at=? WHERE attempts>=3 AND (status='pending' OR (status='running' AND lease_until<=?))").bind(time,time).run();
    const token=randomToken();
    const leaseCase=`CASE type ${Object.entries(LEASE).map(([t,s])=>`WHEN '${t}' THEN ${s}`).join(' ')} ELSE 600 END`;
    const row=await env.DB.prepare(`UPDATE llm_jobs SET status='running',attempts=attempts+1,lease_hash=?,lease_until=?+${leaseCase},updated_at=? WHERE id=(SELECT id FROM llm_jobs WHERE version=1 AND type IN (${d.types.map(()=>'?').join(',')}) AND attempts<3 AND available_at<=? AND (status='pending' OR (status='running' AND lease_until<=?)) ORDER BY created_at,id LIMIT 1) RETURNING id,app_id,type,version,payload_json,lease_until`).bind(await hash(token),time,time,...d.types,time,time).first();
    if(!row)return json({job:null});
    const {payload_json,...rest}=row;return json({job:{...rest,payload:JSON.parse(payload_json),leaseToken:token}});
  }
  const m=path.match(/^\/api\/jobs\/runner\/([a-zA-Z0-9-]+)\/(complete|fail)$/);
  if(runner && method==='POST' && m) {
    const d=await body(request), row=await env.DB.prepare('SELECT * FROM llm_jobs WHERE id=?').bind(m[1]).first();
    if(!row)fail(404,'ジョブが見つかりません。');
    if(!str(d.leaseToken,100) || !equal(row.lease_hash,await hash(d.leaseToken))) fail(409,'処理権が無効です。');
    if(m[2]==='fail') {
      if(row.status!=='running' || row.lease_until<=time)fail(409,'処理権が失効しました。');
      const reason=['model_timeout','invalid_result','hermes_failed'].includes(d.reason)?d.reason:'runner_error';
      await env.DB.prepare("UPDATE llm_jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'pending' END,available_at=?,lease_until=NULL,lease_hash=NULL,last_error=?,updated_at=? WHERE id=? AND status='running' AND lease_hash=? AND lease_until>?").bind(time+300,reason,time,row.id,row.lease_hash,time).run();
      return json({ok:true});
    }
    if(!str(d.model,200) || !str(d.promptVersion,100))fail(400,'モデル名と指示の版が必要です。');
    const result={...output(row.type,d.result),model:d.model,promptVersion:d.promptVersion}, resultJSON=JSON.stringify(result), digest=await hash(resultJSON);
    if(row.status==='completed') {if(row.result_hash!==digest)fail(409,'保存済みの結果と異なります。');return json({ok:true,duplicate:true});}
    if(row.status!=='running'||row.lease_until<=time)fail(409,'処理権が失効しました。');
    const saved=await env.DB.batch([
      env.DB.prepare("UPDATE llm_jobs SET status='completed',result_json=?,result_hash=?,updated_at=?,last_error=NULL WHERE id=? AND status='running' AND lease_hash=? AND lease_until>?").bind(resultJSON,digest,time,row.id,row.lease_hash,time)
    ]);
    if(!saved[0].meta.changes)fail(409,'処理権が更新されました。');
    return json({ok:true});
  }
  fail(404,'APIが見つかりません。');
}

export default {
  async fetch(request, env) {
    try {
      const path = new URL(request.url).pathname;
      if (path === '/health' && request.method === 'GET') return json({ok:true,service:'hermes-llm-jobs'});
      if (path !== '/api/jobs' && !path.startsWith('/api/jobs/')) return json({error:'not_found'},404);
      return await jobRoute(request, env);
    } catch (e) {
      return json({error:{code:e.code||'internal_error',message:e.status?e.message:'処理できませんでした。'}},e.status||500);
    }
  },
  async scheduled(event, env) {
    await env.DB.prepare('DELETE FROM rate_limits WHERE expires_at<?').bind(now()).run();
  }
};
