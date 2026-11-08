import type { OwnerControlDecision } from './ControlSession';

export const CONTROL_BUNDLE: string = 'org.personalassistant.mesh';
export const CONTROL_DECISION_EVENT: string = 'org.personalassistant.mesh.control.DECISION';
export const CONTROL_DECISION_PAYLOAD: string = 'amita.control.decision_json';

// publisher is the OS CommonEventData.bundleName, never an extra in the body.
export function decodeBackgroundDecision(publisher: string, event: string, raw: string): OwnerControlDecision {
  try {
    if (publisher !== CONTROL_BUNDLE || event !== CONTROL_DECISION_EVENT || typeof raw !== 'string' ||
      raw.length === 0 || raw.length > 2048) { throw new Error('invalid'); }
    const value: OwnerControlDecision = JSON.parse(raw) as OwnerControlDecision;
    if (value === null || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).sort().join('|') !== ['approval_id', 'nonce', 'operation_id', 'task_id', 'revision',
        'expires_at', 'decision'].sort().join('|') ||
      ![value.approval_id, value.nonce, value.operation_id, value.task_id].every((id: string): boolean =>
        typeof id === 'string' && /^[A-Za-z0-9._:-]{1,180}$/.test(id)) ||
      !Number.isSafeInteger(value.revision) || value.revision <= 0 ||
      !Number.isSafeInteger(value.expires_at) || value.expires_at <= 0 ||
      (value.decision !== 'approve' && value.decision !== 'deny')) { throw new Error('invalid'); }
    return value;
  } catch (error) { throw new Error('phone_control_untrusted_background_decision'); }
}
