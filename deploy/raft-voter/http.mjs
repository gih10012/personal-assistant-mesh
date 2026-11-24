import { exact, MEMBERS, need } from './codec.mjs';
import { peerMode, verify, verifyExchange } from './protocol.mjs';

export async function boundedJSON(request) {
  need(request.method === 'POST' && request.headers.get('content-type')?.split(';')[0] === 'application/json', 'json_post_required');
  return readJSON(request, 49152);
}
async function readJSON(message, maximum) {
  const reader = message.body?.getReader(); need(reader, 'body_required');
  const chunks = []; let length = 0;
  try {
    while (true) {
      const { done, value } = await reader.read(); if (done) break;
      length += value.length; need(length <= maximum, 'http_body_budget'); chunks.push(value);
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
export async function authenticatedForward(config, request, clock = Date.now) {
  const url = new URL(request.url), path = url.pathname;
  const authorization = request.headers.get('authorization');
  const peers = MEMBERS.filter(id => id !== config.member);
  need(path === '/raft/manage' || clock() < (config.qualification_until_ms ?? Infinity), 'qualification_expired');
  need((path === '/raft/peer' && peers.some(id => peerMode(config, id) === 'push')) ||
    (path === '/raft/exchange' && !url.search && peers.some(id => peerMode(config, id) === 'accept')) ||
    (path === '/raft/manage' && authorized(authorization, config.operator_key)),
    'voter_ingress_not_authorized');
  const value = await boundedJSON(request);
  if (path === '/raft/peer') {
    await verify(config, value);
    need(peerMode(config, value.from) === 'push', 'peer_push_binding');
  } else if (path === '/raft/exchange') {
    await verifyExchange(config, value, clock());
  } else {
    need(value?.group === config.group && value?.scope === config.scope &&
      value?.member === config.member, 'management_group_binding');
    need(['status', 'disable'].includes(value.action) || clock() < (config.qualification_until_ms ?? Infinity), 'qualification_expired');
  }
  const headers = { 'content-type': 'application/json' };
  if (path === '/raft/manage') headers.authorization = authorization;
  return new Request(request.url, { method: 'POST', headers, body: JSON.stringify(value) });
}
export async function handle(driver, request) {
  const path = new URL(request.url).pathname, value = await boundedJSON(request);
  if (path === '/raft/peer') {
    need(MEMBERS.includes(value?.from) && value.from !== driver.config.member &&
      peerMode(driver.config, value.from) === 'push', 'peer_push_binding');
    return driver.receive(value);
  }
  if (path === '/raft/exchange') return driver.exchange(value);
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
    const timeout = Math.min(3000, (config.qualification_until_ms ?? Infinity) - Date.now());
    need(timeout > 0, 'qualification_expired');
    const response = await fetch(config.peers[outgoing.to], { method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': 'AMITA-Raft/1.0' },
      body: JSON.stringify(outgoing), redirect: 'error', signal: AbortSignal.timeout(timeout) });
    need(response.ok, 'peer_transport_rejected');
    return readJSON(response, peerMode(config, outgoing.to) === 'poll' ? 49152 : 2048);
  };
}
