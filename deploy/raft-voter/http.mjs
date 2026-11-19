import { exact, need } from './codec.mjs';
import { verify } from './protocol.mjs';

export async function boundedJSON(request) {
  need(request.method === 'POST' && request.headers.get('content-type')?.split(';')[0] === 'application/json', 'json_post_required');
  const reader = request.body?.getReader(); need(reader, 'body_required');
  const chunks = []; let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      length += value.length; need(length <= 49152, 'http_body_budget'); chunks.push(value);
    }
  } finally { await reader.cancel().catch(() => {}); }
  const data = new Uint8Array(length); let offset = 0;
  for (const part of chunks) { data.set(part, offset); offset += part.length; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(data));
}
function authorized(header, expected) {
  if (typeof expected !== 'string' || typeof header !== 'string') return false;
  const wanted = `Bearer ${expected}`; let difference = header.length ^ wanted.length;
  for (let i = 0; i < wanted.length; i++) difference |= (header.charCodeAt(i) || 0) ^ wanted.charCodeAt(i);
  return difference === 0;
}

// Verify at the public ingress, before charging the separate DO quota for
// a request. Forward the parsed bounded body without teeing the input stream.
export async function authenticatedForward(config, request) {
  const path = new URL(request.url).pathname;
  const authorization = request.headers.get('authorization');
  need(path === '/raft/peer' ||
    (path === '/raft/manage' && authorized(authorization, config.operator_key)),
    'voter_ingress_not_authorized');
  const value = await boundedJSON(request);
  if (path === '/raft/peer') {
    await verify(config, value);
  } else {
    need(value?.group === config.group && value?.scope === config.scope &&
      value?.member === config.member, 'management_group_binding');
  }
  const headers = { 'content-type': 'application/json' };
  if (path === '/raft/manage') headers.authorization = authorization;
  return new Request(request.url, { method: 'POST', headers, body: JSON.stringify(value) });
}
export async function handle(driver, request) {
  const path = new URL(request.url).pathname, value = await boundedJSON(request);
  if (path === '/raft/peer') return driver.receive(value);
  need(path === '/raft/manage' && authorized(request.headers.get('authorization'), driver.config.operator_key), 'existing_operator_binding_required');
  need(value?.group === driver.config.group && value?.scope === driver.config.scope && value?.member === driver.config.member,
    'management_group_binding');
  const keys = ['group', 'scope', 'member', 'action'];
  if (value.action === 'propose') {
    need(exact(value, [...keys, 'proposal']), 'management_fields'); return driver.propose(value.proposal);
  }
  need(exact(value, keys), 'management_fields');
  if (value.action === 'status') return driver.inspect();
  if (value.action === 'enable' || value.action === 'disable') return driver.enable(value.action === 'enable');
  throw new Error('raft_voter_management_action');
}
export function transport(config) {
  return async outgoing => {
    const response = await fetch(config.peers[outgoing.to], { method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify(outgoing), redirect: 'error', signal: AbortSignal.timeout(3000) });
    need(response.ok, 'peer_transport_rejected');
    // ACKs are small and signed, and never carry an arbitrary response body.
    const text = await response.text(); need(text.length <= 2048, 'ack_budget'); return JSON.parse(text);
  };
}
