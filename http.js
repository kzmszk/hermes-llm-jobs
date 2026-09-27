export class HttpError extends Error { constructor(status, message, code = 'request_error') { super(message); this.status = status; this.code = code; } }
export const fail = (status, message, code) => { throw new HttpError(status, message, code); };
export const now = () => Math.floor(Date.now() / 1000);
export const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', ...headers } });
export const randomToken = () => { const bytes = crypto.getRandomValues(new Uint8Array(32)); return [...bytes].map(b => b.toString(16).padStart(2, '0')).join(''); };
export async function hash(value) { return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, '0')).join(''); }
export async function hmac(secret, value) { const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']); return [...new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(value)))].map(b => b.toString(16).padStart(2, '0')).join(''); }
export function equal(a, b) { if (typeof a !== 'string' || typeof b !== 'string') return false; let diff = a.length ^ b.length; for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0); return diff === 0; }
export async function body(request) {
  if (!request.headers.get('content-type')?.startsWith('application/json')) fail(415, 'JSON形式で送信してください。');
  if (Number(request.headers.get('content-length')) > 32768) fail(413, '送信内容が長すぎます。');
  const reader = request.body?.getReader(); if (!reader) fail(400, '本文が必要です。');
  const decoder = new TextDecoder(); let text = '', size = 0;
  while (true) { const { done, value } = await reader.read(); if (done) break; size += value.length; if (size > 32768) { await reader.cancel(); fail(413, '送信内容が長すぎます。'); } text += decoder.decode(value, { stream: true }); }
  text += decoder.decode(); let value; try { value = JSON.parse(text); } catch { fail(400, 'JSONを読み取れません。'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(400, 'JSONオブジェクトが必要です。'); return value;
}
export function sameOrigin(request) { if (request.headers.get('origin') !== new URL(request.url).origin) fail(403, 'このページから操作してください。', 'origin_rejected'); }
export async function limit(env, key, max, seconds) {
  const time = now(), window = Math.floor(time / seconds);
  const row = await env.DB.prepare('INSERT INTO rate_limits(bucket,window,hits,expires_at) VALUES(?,?,1,?) ON CONFLICT(bucket,window) DO UPDATE SET hits=hits+1 RETURNING hits').bind(key, window, (window + 2) * seconds).first();
  if (row.hits > max) fail(429, '操作が多いため、しばらく待ってから試してください。', 'rate_limited');
}
export function secure(response, request) {
  const headers = new Headers(response.headers);
  headers.set('X-Content-Type-Options', 'nosniff'); headers.set('Referrer-Policy', 'same-origin');
  headers.set('X-Frame-Options', 'DENY'); headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
  headers.set('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'");
  if (new URL(request.url).protocol === 'https:') headers.set('Strict-Transport-Security', 'max-age=31536000');
  return new Response(response.body, { status: response.status, headers });
}
