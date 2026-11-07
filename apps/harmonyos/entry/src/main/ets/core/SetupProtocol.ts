// Owner-local transport envelope only. Existing enrollment/work/update validators
// remain responsible for their original identities, receipts and permissions.
export const MAX_SETUP_BYTES: number = 65536;
export interface OwnerSetup {
  format: string;
  node_id: string;
  phase: string;
  enrollment_json: string;
  relay_json: string;
  relay_token: string;
  work_bootstrap_json: string;
  update_trust_json: string;
}
function requireSetup(value: boolean): void {
  if (!value) { throw new Error('mesh_owner_setup_invalid'); }
}
export function parseOwnerSetup(raw: string, nodeId: string, byteLength: number): OwnerSetup {
  try {
    requireSetup(typeof raw === 'string' && raw.length > 0 && raw.length <= MAX_SETUP_BYTES &&
      Number.isSafeInteger(byteLength) && byteLength > 0 && byteLength <= MAX_SETUP_BYTES);
    const value: OwnerSetup = JSON.parse(raw) as OwnerSetup;
    requireSetup(value !== null && typeof value === 'object' && !Array.isArray(value));
    requireSetup(Object.keys(value).sort().join('|') === ['format', 'node_id', 'phase', 'enrollment_json',
      'relay_json', 'relay_token', 'work_bootstrap_json', 'update_trust_json'].sort().join('|'));
    requireSetup(value.format === 'amita-phone-owner-setup/1' && typeof nodeId === 'string' && nodeId.length > 0 &&
      typeof value.node_id === 'string' && value.node_id === nodeId &&
      typeof value.phase === 'string' && typeof value.enrollment_json === 'string' &&
      typeof value.relay_json === 'string' && typeof value.relay_token === 'string' &&
      typeof value.work_bootstrap_json === 'string' && typeof value.update_trust_json === 'string');
    if (value.phase === 'enrollment') {
      requireSetup(value.enrollment_json.length > 0 && value.relay_json.length > 0 &&
        /^[\x21-\x7e]{32,4096}$/.test(value.relay_token) &&
        value.work_bootstrap_json === '' && value.update_trust_json === '');
    } else {
      requireSetup(value.phase === 'member' && value.enrollment_json === '' && value.relay_json === '' &&
        value.relay_token === '' && value.work_bootstrap_json.length > 0);
    }
    return value;
  } catch (error) {
    // JSON parser errors can contain private input; never propagate them.
    throw new Error('mesh_owner_setup_invalid');
  }
}
