// Transport admission is separate from the unchanged member application's OAuth.
export const PROTOCOL = 'amita-reverse-relay/1';
export const MAX_REQUEST = 262144;
export const MAX_RESPONSE = 524288;
export const REQUEST_HEADERS = new Set(['authorization', 'content-type', 'accept',
  'mcp-protocol-version', 'mcp-session-id', 'last-event-id', 'origin']);
export const RESPONSE_HEADERS = new Set(['content-type', 'www-authenticate', 'cache-control',
  'mcp-session-id', 'vary', 'allow', 'access-control-allow-origin', 'access-control-allow-methods',
  'access-control-allow-headers', 'access-control-expose-headers']);
export const PUBLIC_ROUTES = new Set(['POST /mcp', 'GET /mcp', 'OPTIONS /mcp',
  'GET /.well-known/oauth-protected-resource', 'GET /.well-known/oauth-protected-resource/mcp']);
export const OAUTH_ROUTES = new Set([...PUBLIC_ROUTES, 'GET /.well-known/oauth-authorization-server',
  'GET /authorize', 'GET /consent', 'POST /consent', 'GET /auth/callback', 'POST /token', 'OPTIONS /token',
  'POST /register', 'OPTIONS /register', 'POST /revoke', 'OPTIONS /revoke']);
export const OAUTH_REQUEST_HEADERS = new Set([...REQUEST_HEADERS, 'cookie', 'access-control-request-method',
  'access-control-request-headers']);
export const OAUTH_RESPONSE_HEADERS = new Set([...RESPONSE_HEADERS, 'location', 'pragma', 'expires',
  'access-control-allow-credentials', 'access-control-max-age', 'content-security-policy',
  'content-security-policy-report-only', 'x-frame-options', 'x-content-type-options', 'referrer-policy',
  'permissions-policy', 'cross-origin-resource-policy', 'strict-transport-security']);
const QUERY_ROUTES = new Set(['GET /authorize', 'GET /consent', 'GET /auth/callback']);
export const allowedQuery = (method, path, query, oauth) => query === '' || oauth === true &&
  typeof query === 'string' && query.length <= 8192 && query.startsWith('?') && !/[\x00-\x20\x7f#]/.test(query) &&
  QUERY_ROUTES.has(`${method} ${path}`);
export function responseCookies(value) {
  if (!Array.isArray(value) || value.some(item => typeof item !== 'string' || /[\r\n\0]/.test(item)) ||
      JSON.stringify(value).length > 16384) throw new Error('relay_cookies_invalid');
  return value;
}
export const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_-]{8,128}$/.test(value);
export const exact = (value, keys) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
export const sha256 = async raw => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
  typeof raw === 'string' ? new TextEncoder().encode(raw) : raw)), n => n.toString(16).padStart(2, '0')).join('');
export const random = () => Array.from(crypto.getRandomValues(new Uint8Array(32)), n => n.toString(16).padStart(2, '0')).join('');
export const toBase64 = raw => { let text = ''; for (const value of raw) text += String.fromCharCode(value); return btoa(text); };
export function fromBase64(value, maximum) {
  if (typeof value !== 'string' || value.length > Math.ceil(maximum / 3) * 4) throw new Error('relay_body_invalid');
  const raw = Uint8Array.from(atob(value), c => c.charCodeAt(0));
  if (raw.byteLength > maximum || toBase64(raw) !== value) throw new Error('relay_body_invalid');
  return raw;
}
export function headers(value, allowed) {
  const entries = value instanceof Headers ? [...value.entries()] : Object.entries(value || {});
  const result = {};
  for (const [key, content] of entries) {
    if (!allowed.has(key) || typeof content !== 'string' || /[\r\n\0]/.test(content)) {
      if (!(value instanceof Headers)) throw new Error('relay_headers_invalid');
      continue;
    }
    result[key] = content;
  }
  if (JSON.stringify(result).length > 16384) throw new Error('relay_headers_oversized');
  return Object.fromEntries(Object.entries(result).sort(([a], [b]) => a.localeCompare(b)));
}
export const fingerprint = (method, path, selectedHeaders, body, query) => sha256(JSON.stringify([
  method, path, Object.entries(selectedHeaders).sort(([a], [b]) => a.localeCompare(b)), toBase64(body),
  ...(query === undefined ? [] : [query])]));
export function policy(raw) {
  const value = JSON.parse(raw);
  const hex = x => typeof x === 'string' && /^[0-9a-f]{64}$/.test(x);
  const routes = value.public_oauth === true ? OAUTH_ROUTES : PUBLIC_ROUTES;
  if (value.format !== PROTOCOL || !hex(value.client_token_sha256) || !Array.isArray(value.members)
      || (value.public_oauth !== undefined && typeof value.public_oauth !== 'boolean')
      || value.members.length < 1 || value.members.length > 8 || !Array.isArray(value.routes)
      || value.routes.length < 1 || value.routes.length > routes.size) throw new Error('relay_policy_invalid');
  const ids = new Set(), hashes = new Set([value.client_token_sha256]);
  for (const member of value.members) {
    if (!exact(member, ['id', 'token_sha256']) || !identifier(member.id) || !hex(member.token_sha256)
        || ids.has(member.id) || hashes.has(member.token_sha256)) throw new Error('relay_member_invalid');
    ids.add(member.id); hashes.add(member.token_sha256);
  }
  const seen = new Set();
  for (const route of value.routes) {
    const key = `${route.method} ${route.path}`;
    if (!exact(route, ['method', 'path', 'member_id']) || !routes.has(key)
        || seen.has(key) || !ids.has(route.member_id)) throw new Error('relay_route_invalid');
    seen.add(key);
  }
  if (value.public_oauth && new Set(value.routes.map(route => route.member_id)).size !== 1) {
    throw new Error('relay_oauth_single_active_member_required');
  }
  value.daily_limit ??= 2000;
  value.max_receipts ??= 100000;
  value.timeout_ms ??= 25000;
  if (!Number.isInteger(value.daily_limit) || value.daily_limit < 1 || value.daily_limit > 10000
      || !Number.isInteger(value.max_receipts) || value.max_receipts < 1 || value.max_receipts > 1000000
      || !Number.isInteger(value.timeout_ms) || value.timeout_ms < 1000 || value.timeout_ms > 25000) {
    throw new Error('relay_bounds_invalid');
  }
  return value;
}
export async function boundedBody(request, maximum) {
  if (!request.body) return new Uint8Array();
  const reader = request.body.getReader(); const chunks = []; let size = 0;
  try {
    while (true) {
      const {done, value} = await reader.read(); if (done) break;
      size += value.length;
      if (size > maximum) throw new Error('relay_body_oversized');
      chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  const output = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.length; }
  return output;
}
