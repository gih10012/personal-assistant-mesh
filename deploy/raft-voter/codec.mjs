// Pinned raft-proto 0.7.0/proto/proto/eraftpb.proto wire boundary. No Raft algorithm here.
export const MEMBERS = Object.freeze([1, 2, 3]);
export const MAX_WIRE = 32768;
export function need(value, reason) { if (!value) throw new Error(`raft_voter_${reason}`); }
export function exact(value, keys) {
  return value !== null && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}
export function bytes(value, maximum = MAX_WIRE) {
  need((Array.isArray(value) || value instanceof Uint8Array) && value.length <= maximum &&
    value.every(v => Number.isInteger(v) && v >= 0 && v <= 255), 'bounded_bytes_required');
  return Uint8Array.from(value);
}
export function safe(value) {
  need(typeof value === 'bigint' ? value >= 0n && value <= BigInt(Number.MAX_SAFE_INTEGER) :
    Number.isSafeInteger(value) && value >= 0, 'u64_exceeds_supported_safe_range');
  return Number(value);
}
export function fields(input) {
  const wire = bytes(input), result = []; let offset = 0;
  const variable = () => {
    let value = 0n, shift = 0n;
    for (let count = 0; count < 10 && offset < wire.length; count++) {
      const part = wire[offset++]; value |= BigInt(part & 127) << shift;
      if (!(part & 128)) { need(value <= 0xffffffffffffffffn, 'invalid_varint'); return value; }
      shift += 7n;
    }
    throw new Error('raft_voter_invalid_varint');
  };
  while (offset < wire.length) {
    need(result.length < 512, 'protobuf_fields_budget');
    const tag = safe(variable()), number = Math.floor(tag / 8), kind = tag & 7;
    need(number > 0, 'protobuf_tag');
    let value;
    if (kind === 0) value = variable();
    else if (kind === 2) {
      const length = safe(variable()); need(offset + length <= wire.length, 'protobuf_length');
      value = wire.slice(offset, offset + length); offset += length;
    } else if (kind === 1 || kind === 5) {
      const length = kind === 1 ? 8 : 4; need(offset + length <= wire.length, 'protobuf_length');
      value = wire.slice(offset, offset + length); offset += length;
    } else throw new Error('raft_voter_unsupported_wire_type');
    result.push({ number, kind, value });
  }
  return result;
}
export function uint(rows, number) {
  const matches = rows.filter(row => row.number === number);
  need(matches.length <= 1 && matches.every(row => row.kind === 0), 'duplicate_scalar');
  return matches.length ? safe(matches[0].value) : 0;
}
export function blob(rows, number) {
  const matches = rows.filter(row => row.number === number);
  need(matches.length <= 1 && matches.every(row => row.kind === 2), 'duplicate_blob');
  return matches.length ? matches[0].value : new Uint8Array();
}
function variable(value) {
  let current = BigInt(safe(value)), result = [];
  do { let part = Number(current & 127n); current >>= 7n; if (current) part |= 128; result.push(part); } while (current);
  return result;
}
function scalar(number, value) { return value ? [...variable(number * 8), ...variable(value)] : []; }
function binary(number, value) { return value.length ? [...variable(number * 8 + 2), ...variable(value.length), ...value] : []; }
export function entry(wire) {
  const rows = fields(wire);
  need(rows.every(row => [1, 2, 3, 4, 5, 6].includes(row.number)) &&
    uint(rows, 1) === 0 && uint(rows, 5) <= 1 && blob(rows, 6).length === 0, 'static_normal_entries_only');
  const value = { term: uint(rows, 2), index: uint(rows, 3), data: blob(rows, 4) };
  if (value.data.length) value.proposal = parseProposal(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(value.data)));
  return value;
}
export function message(wire, { remote = false } = {}) {
  const rows = fields(wire), type = uint(rows, 1), to = uint(rows, 2), from = uint(rows, 3);
  need(rows.every(row => row.number >= 1 && row.number <= 16), 'unknown_message_fields');
  need(MEMBERS.includes(to) && MEMBERS.includes(from), 'static_message_members');
  need([2, 3, 4, 5, 6, 8, 9, 17, 18].includes(type), 'unsupported_message_snapshot_or_local_type');
  if (remote) need(from !== to, 'remote_loopback_message');
  for (const number of [4, 5, 6, 8, 13, 14, 15]) uint(rows, number);
  need(blob(rows, 9).length === 0 && blob(rows, 12).length === 0, 'snapshot_or_context_not_supported');
  for (const row of rows.filter(row => row.number === 7)) {
    need(row.kind === 2, 'entry_wire_type'); entry(row.value);
  }
  return { type, to, from, term: uint(rows, 4) };
}
export function hardState(wire) {
  const rows = fields(wire);
  need(rows.every(row => [1, 2, 3].includes(row.number)), 'unknown_hard_state_fields');
  return { term: uint(rows, 1), vote: uint(rows, 2), commit: uint(rows, 3) };
}
export function withCommit(wire, commit) {
  const state = hardState(wire); need(commit >= state.commit, 'commit_regression');
  return [...scalar(1, state.term), ...scalar(2, state.vote), ...scalar(3, commit)];
}
export function digestProposal(value) {
  need(exact(value, ['format', 'operation_id', 'digest']) && value.format === 'amita-quorum-digest/1' &&
    typeof value.operation_id === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value.operation_id) &&
    typeof value.digest === 'string' && /^[0-9a-f]{64}$/.test(value.digest), 'digest_only_proposal');
  return { format: value.format, operation_id: value.operation_id, digest: value.digest };
}
export const OWNER_TRANSFER = 'amita-replica-writer-transfer/1';
export const identifier = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const hash = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const integer = value => Number.isSafeInteger(value) && value >= 0;
export function parseProposal(value) {
  if (value?.format !== OWNER_TRANSFER) return digestProposal(value);
  need(exact(value, ['format', 'operation_id', 'group', 'role', 'scope', 'previous', 'next_owner',
    'next_epoch', 'checkpoint', 'checkpoint_sha256']) && identifier(value.operation_id) &&
    identifier(value.group) && value.role === 'replica_writer' && identifier(value.scope) &&
    identifier(value.next_owner) && integer(value.next_epoch) && value.next_epoch > 0 &&
    hash(value.checkpoint_sha256), 'owner_transfer_fields');
  const p = value.previous;
  need(exact(p, ['owner', 'epoch', 'head']) && integer(p.epoch) &&
    (p.epoch === 0 ? p.owner === null : identifier(p.owner)) &&
    exact(p.head, ['index', 'sha256']) && integer(p.head.index) && hash(p.head.sha256) &&
    (p.epoch === 0 ? p.head.index === 0 && p.head.sha256 === '0'.repeat(64) : p.head.index > 0) &&
    value.next_epoch === p.epoch + 1 && value.next_owner !== p.owner, 'owner_predecessor_fields');
  const c = value.checkpoint;
  need(exact(c, ['authority', 'incarnation', 'generation', 'manifest_sha256', 'sealed_journal_head',
    'typed_rows_sha256', 'external_dependencies_manifest_sha256', 'worker_claim_protocol',
    'source_holder', 'source_seal_sha256', 'holder_receipts', 'retained_effects_sha256']) &&
    identifier(c.authority) && identifier(c.incarnation) && integer(c.generation) && c.generation > 0 &&
    integer(c.sealed_journal_head) && hash(c.manifest_sha256) && hash(c.typed_rows_sha256) &&
    hash(c.external_dependencies_manifest_sha256) && c.worker_claim_protocol === 'amita-worker-claim/1' &&
    identifier(c.source_holder) && hash(c.source_seal_sha256) && hash(c.retained_effects_sha256) &&
    (p.owner === null || c.source_holder === p.owner) && c.source_holder !== value.next_owner, 'sealed_checkpoint_fields');
  const receipts = c.holder_receipts;
  need(Array.isArray(receipts) && receipts.length === 2 &&
    receipts.every(r => exact(r, ['holder', 'sha256']) && identifier(r.holder) && hash(r.sha256)) &&
    receipts[0].holder < receipts[1].holder && receipts[0].sha256 !== receipts[1].sha256 &&
    receipts.some(r => r.holder === c.source_holder) && receipts.some(r => r.holder === value.next_owner),
  'two_distinct_sorted_holder_receipts');
  return value;
}
export function proposeWire(id, value) {
  const data = new TextEncoder().encode(JSON.stringify(parseProposal(value)));
  return Uint8Array.from([...scalar(1, 2), ...scalar(2, id), ...scalar(3, id), ...binary(7, binary(4, data))]);
}
export function encode64(value) {
  let text = ''; for (const byte of value) text += String.fromCharCode(byte);
  return btoa(text).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
}
export function decode64(value) {
  need(typeof value === 'string' && value.length <= Math.ceil(MAX_WIRE * 4 / 3) && /^[A-Za-z0-9_-]*$/.test(value), 'wire_base64');
  const text = atob(value.replaceAll('-', '+').replaceAll('_', '/'));
  const result = bytes(Uint8Array.from(text, char => char.charCodeAt(0)));
  need(encode64(result) === value, 'noncanonical_base64'); return result;
}
