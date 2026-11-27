import { isOwnerTransfer } from './codec.mjs';
import { sha256 } from './protocol.mjs';

// All accepted strings are ASCII identifiers or hashes, and numbers are
// nonnegative safe integers. Arrays retain their order (including holder IDs).
export function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value !== null && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  }
  return JSON.stringify(value);
}
export const contentHash = value => sha256(new TextEncoder().encode(canonical(value)));
const genesis = { owner: null, epoch: 0, head: { index: 0, sha256: '0'.repeat(64) } };

export function originalReceipt(receipts, id) {
  return Object.hasOwn(receipts, id) ? receipts[id] : null;
}

export function receiptMatches(receipt, proposal, requestHash) {
  return isOwnerTransfer(proposal)
    ? receipt.format === receiptFormat(proposal.role) && receipt.request_sha256 === requestHash
    : receipt.format === undefined && receipt.digest === proposal.digest;
}

function receiptFormat(role) {
  return role === 'oauth_writer' ? 'amita-oauth-writer-receipt/1' : 'amita-replica-writer-receipt/1';
}

export function recordReceipt(receipts, id, receipt) {
  // Operation IDs are arbitrary strings, including names on Object.prototype.
  Object.defineProperty(receipts, id, { value: receipt, enumerable: true, writable: true, configurable: true });
}

export async function ownerRequestError(command, binding) {
  if (command.group !== binding.group || command.scope !== binding.scope) return 'owner_group_scope_binding';
  if (await contentHash(command.checkpoint) !== command.checkpoint_sha256) return 'sealed_checkpoint_digest';
  return null;
}

// The driver calls this for committed entries only, in log order, before the
// same durable save as its applied index. The FSM opens no business database.
export async function applyOwner(state, command, record) {
  if (originalReceipt(state.receipts, command.operation_id)) return;
  const requestHash = await contentHash(command);
  const current = state[command.role] ?? genesis;
  let reason = await ownerRequestError(command, state.binding);
  if (reason === null && (current.owner !== command.previous.owner || current.epoch !== command.previous.epoch ||
    current.head.index !== command.previous.head.index || current.head.sha256 !== command.previous.head.sha256)) {
    reason = 'owner_predecessor_conflict';
  }
  const receipt = { format: receiptFormat(command.role), operation_id: command.operation_id,
    group: command.group, role: command.role, scope: command.scope, request_sha256: requestHash,
    index: record.index, term: record.term, checkpoint_sha256: command.checkpoint_sha256,
    status: reason === null ? 'applied' : 'rejected' };
  if (command.role === 'replica_writer') receipt.retained_effects_sha256 = command.checkpoint.retained_effects_sha256;
  if (reason) receipt.reason = reason;
  else {
    const head = { index: record.index, sha256: requestHash };
    receipt.grant = { owner: command.next_owner, epoch: command.next_epoch, head };
    state[command.role] = { ...receipt.grant, group: command.group, role: command.role, scope: command.scope,
      operation_id: command.operation_id, checkpoint_sha256: command.checkpoint_sha256, checkpoint: command.checkpoint };
  }
  recordReceipt(state.receipts, command.operation_id, receipt);
}
