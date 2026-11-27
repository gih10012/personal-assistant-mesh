import { exact, identifier, isOwnerTransfer, MEMBERS, need, OWNER_ROLES } from './codec.mjs';
import { originalReceipt } from './owner-fsm.mjs';
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
function checkManagement(config, value, authorization) {
  const operator = authorized(authorization, config.operator_key);
  const owner = authorized(authorization, config.owner_command_key);
  if (value?.action === 'propose' && isOwnerTransfer(value.proposal)) {
    need(owner, 'owner_command_key_required');
  } else if (['status', 'receipt', 'owner'].includes(value?.action)) {
    need(operator || owner, 'existing_operator_binding_required');
  } else need(operator, 'existing_operator_binding_required');
  need(value?.group === config.group && value?.scope === config.scope && value?.member === config.member,
    'management_group_binding');
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
    (path === '/raft/manage' && (authorized(authorization, config.operator_key) || authorized(authorization, config.owner_command_key))),
    'voter_ingress_not_authorized');
  const value = await boundedJSON(request);
  if (path === '/raft/peer') {
    await verify(config, value);
    need(peerMode(config, value.from) === 'push', 'peer_push_binding');
  } else if (path === '/raft/exchange') {
    await verifyExchange(config, value, clock());
  } else {
    checkManagement(config, value, authorization);
    need(['status', 'owner', 'receipt', 'disable'].includes(value.action) ||
      clock() < (config.qualification_until_ms ?? Infinity), 'qualification_expired');
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
  need(path === '/raft/manage', 'existing_operator_binding_required');
  checkManagement(driver.config, value, request.headers.get('authorization'));
  const keys = ['group', 'scope', 'member', 'action'];
  if (value.action === 'propose') {
    need(exact(value, [...keys, 'proposal']), 'management_fields'); return driver.propose(value.proposal);
  }
  if (value.action === 'receipt' || value.action === 'owner') {
    const readReceipt = value.action === 'receipt';
    const ownerRole = value.role ?? 'replica_writer';
    need(readReceipt ? exact(value, [...keys, 'operation_id']) && identifier(value.operation_id)
      : exact(value, keys) || (exact(value, [...keys, 'role']) && OWNER_ROLES.includes(value.role)),
      'management_fields');
    const state = await driver.inspect();
    const read = { group: state.group, scope: state.scope, member: state.member, applied: state.applied,
      consistency: state.owner_read_consistency, core_closed: state.core_closed, hold: state.hold, business_fencing: false };
    if (!readReceipt) return { ...read, role: ownerRole, current: state[ownerRole] };
    const receipt = originalReceipt(state.receipts, value.operation_id);
    return { ...read, operation_id: value.operation_id, status: receipt ? 'recorded' : 'unknown_here', receipt };
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
