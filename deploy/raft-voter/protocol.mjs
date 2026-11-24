import { bytes, decode64, encode64, exact, MEMBERS, message, need } from './codec.mjs';
export const FORMAT = 'amita-raft-peer/1';
const EXCHANGE = 'amita-raft-exchange/1', EXCHANGE_TTL_MS = 30000;
export function peerMode(config, id) {
  const route = config.peers[id];
  if (route === 'accept_exchange') return 'accept';
  return new URL(route).pathname === '/raft/exchange' ? 'poll' : 'push';
}
export function validateConfig(value) {
  const fields = ['group', 'scope', 'member', 'links', 'operator_key', 'tick_ms', 'start_enabled', 'peers'];
  if (value && Object.hasOwn(value, 'qualification_until_ms')) {
    fields.push('qualification_until_ms');
    need(Number.isSafeInteger(value.qualification_until_ms) && value.qualification_until_ms > 0, 'absolute_qualification_deadline');
  }
  need(exact(value, fields) &&
    typeof value.group === 'string' && /^[A-Za-z0-9_.:-]{1,96}$/.test(value.group) &&
    typeof value.scope === 'string' && /^[A-Za-z0-9_.:-]{1,96}$/.test(value.scope) &&
    MEMBERS.includes(value.member) && Number.isSafeInteger(value.tick_ms) && value.tick_ms >= 5000 && value.tick_ms <= 60000 &&
    typeof value.start_enabled === 'boolean' &&
    (value.operator_key === null || /^[0-9a-f]{64}$/.test(value.operator_key)), 'explicit_private_configuration');
  const others = MEMBERS.filter(id => id !== value.member).map(String);
  need(exact(value.links, others) && exact(value.peers, others), 'exact_link_bindings');
  for (const id of others) {
    need(typeof value.links[id] === 'string' && /^[0-9a-f]{64}$/.test(value.links[id]), 'existing_link_key_required');
    if (value.peers[id] !== 'accept_exchange') {
      const url = new URL(value.peers[id]);
      need(url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash &&
        ['/raft/peer', '/raft/exchange'].includes(url.pathname), 'explicit_https_peer_endpoint');
    }
    if (peerMode(value, id) !== 'push') need(!value.start_enabled, 'exchange_requires_disabled_initial_state');
  }
  return structuredClone(value);
}
export async function sha256(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes(value, 4 * 1024 * 1024)))]
    .map(v => v.toString(16).padStart(2, '0')).join('');
}
async function key(hex, usage) {
  return crypto.subtle.importKey('raw', Uint8Array.from(hex.match(/../g), value => parseInt(value, 16)),
    { name: 'HMAC', hash: 'SHA-256' }, false, [usage]);
}
function authenticated(value) {
  return new TextEncoder().encode(JSON.stringify([value.format, value.group, value.scope, value.from, value.to, value.sequence, value.wire]));
}
export async function envelope(config, sequence, wire) {
  const header = message(wire, { remote: true }); need(header.from === config.member, 'outgoing_sender');
  const value = { format: FORMAT, group: config.group, scope: config.scope, from: header.from, to: header.to,
    sequence, wire: encode64(wire) };
  value.mac = encode64(new Uint8Array(await crypto.subtle.sign('HMAC', await key(config.links[header.to], 'sign'), authenticated(value))));
  return value;
}
export async function verify(config, value) {
  need(exact(value, ['format', 'group', 'scope', 'from', 'to', 'sequence', 'wire', 'mac']) &&
    value.format === FORMAT && value.group === config.group && value.scope === config.scope &&
    MEMBERS.includes(value.from) && value.from !== config.member && value.to === config.member &&
    Number.isSafeInteger(value.sequence) && value.sequence > 0, 'peer_binding');
  const wire = decode64(value.wire), mac = decode64(value.mac);
  need(mac.length === 32 && await crypto.subtle.verify('HMAC', await key(config.links[value.from], 'verify'), mac, authenticated(value)), 'peer_authentication');
  const header = message(wire, { remote: true });
  need(header.from === value.from && header.to === value.to, 'wire_sender_recipient_binding');
  return { wire, header, hash: await sha256(wire) };
}
export async function acknowledgement(config, value) {
  const ack = { format: 'amita-raft-ack/1', group: config.group, scope: config.scope, from: config.member,
    to: value.from, sequence: value.sequence };
  ack.mac = encode64(new Uint8Array(await crypto.subtle.sign('HMAC', await key(config.links[ack.to], 'sign'),
    new TextEncoder().encode(JSON.stringify([ack.format, ack.group, ack.scope, ack.from, ack.to, ack.sequence])))));
  return ack;
}
export async function validAck(config, outgoing, ack) {
  if (!(exact(ack, ['format', 'group', 'scope', 'from', 'to', 'sequence', 'mac']) &&
    ack.format === 'amita-raft-ack/1' && ack.group === config.group && ack.scope === config.scope &&
    ack.from === outgoing.to && ack.to === config.member && ack.sequence === outgoing.sequence)) return false;
  const signature = decode64(ack.mac);
  return signature.length === 32 && crypto.subtle.verify('HMAC', await key(config.links[ack.from], 'verify'), signature,
    new TextEncoder().encode(JSON.stringify([ack.format, ack.group, ack.scope, ack.from, ack.to, ack.sequence])));
}

function exchangeBytes(value) {
  return new TextEncoder().encode(JSON.stringify([value.format, value.group, value.scope, value.from,
    value.to, value.expires_ms, value.packet, value.ack]));
}
export async function exchangeRequest(config, to, packet, ack, now) {
  const value = { format: EXCHANGE, group: config.group, scope: config.scope, from: config.member,
    to, expires_ms: Math.min(now + EXCHANGE_TTL_MS / 2, config.qualification_until_ms ?? Infinity), packet, ack };
  value.mac = encode64(new Uint8Array(await crypto.subtle.sign('HMAC', await key(config.links[to], 'sign'), exchangeBytes(value))));
  return value;
}
export async function verifyExchange(config, value, now) {
  need(exact(value, ['format', 'group', 'scope', 'from', 'to', 'expires_ms', 'packet', 'ack', 'mac']) &&
    value.format === EXCHANGE && value.group === config.group && value.scope === config.scope &&
    MEMBERS.includes(value.from) && value.from !== config.member && value.to === config.member &&
    peerMode(config, value.from) === 'accept', 'exchange_binding');
  need(Number.isSafeInteger(value.expires_ms) && value.expires_ms > now &&
    value.expires_ms <= now + EXCHANGE_TTL_MS, 'exchange_expiry');
  const mac = decode64(value.mac);
  need(mac.length === 32 && await crypto.subtle.verify('HMAC', await key(config.links[value.from], 'verify'),
    mac, exchangeBytes(value)), 'exchange_authentication');
  if (value.ack !== null) {
    need(Number.isSafeInteger(value.ack?.sequence) && value.ack.sequence > 0 &&
      await validAck(config, { to: value.from, sequence: value.ack.sequence }, value.ack), 'exchange_ack_binding');
  }
  if (value.packet === null) return null;
  need(value.packet?.from === value.from, 'exchange_packet_binding');
  return verify(config, value.packet);
}
