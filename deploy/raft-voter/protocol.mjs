import { bytes, decode64, encode64, exact, MEMBERS, message, need } from './codec.mjs';
export const FORMAT = 'amita-raft-peer/1';
export function validateConfig(value) {
  need(exact(value, ['group', 'scope', 'member', 'links', 'operator_key', 'tick_ms', 'start_enabled', 'peers']) &&
    /^[A-Za-z0-9_.:-]{1,96}$/.test(value.group) && /^[A-Za-z0-9_.:-]{1,96}$/.test(value.scope) &&
    MEMBERS.includes(value.member) && Number.isSafeInteger(value.tick_ms) && value.tick_ms >= 5000 && value.tick_ms <= 60000 &&
    typeof value.start_enabled === 'boolean' &&
    (value.operator_key === null || /^[0-9a-f]{64}$/.test(value.operator_key)), 'explicit_private_configuration');
  const others = MEMBERS.filter(id => id !== value.member).map(String);
  need(exact(value.links, others) && exact(value.peers, others), 'exact_link_bindings');
  for (const id of others) {
    need(typeof value.links[id] === 'string' && /^[0-9a-f]{64}$/.test(value.links[id]), 'existing_link_key_required');
    const url = new URL(value.peers[id]);
    need(url.protocol === 'https:' && !url.username && !url.password && !url.search && !url.hash && url.pathname === '/raft/peer',
      'explicit_https_peer_endpoint');
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
