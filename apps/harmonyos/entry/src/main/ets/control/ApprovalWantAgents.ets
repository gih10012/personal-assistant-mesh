import { Want, WantAgent, wantAgent } from '@kit.AbilityKit';
import { ApprovalTicket, ControlSession, OwnerControlDecision } from './ControlSession';
import { ApprovalActions } from './ApprovalSurface';

// Public fallback: opens the app's own existing UIAbility. This does not pretend
// to be a background callback. Root must route onCreate/onNewWant to the same
// private pending approval journal and verify the exact ticket before deciding.
// No remote-supplied bundle, ability, URL or command is accepted.
export async function buildApprovalWantAgents(ticket: ApprovalTicket): Promise<ApprovalActions> {
  const review: WantAgent = await decisionAgent(ticket, 'review', 0);
  const approve: WantAgent = await decisionAgent(ticket, 'approve', 1);
  const deny: WantAgent = await decisionAgent(ticket, 'deny', 2);
  return { review: review, approve: approve, deny: deny };
}
// Only call from the app's own platform callback, never a network request.
// false means review/no decision; it must open the actual private review UI.
export async function handleApprovalDecisionWant(want: Want, session: ControlSession): Promise<boolean> {
  const p: Record<string, Object> | undefined = want.parameters;
  if (p === undefined || typeof p['amita.control.approval_id'] !== 'string' ||
    typeof p['amita.control.nonce'] !== 'string' || typeof p['amita.control.operation_id'] !== 'string' ||
    typeof p['amita.control.task_id'] !== 'string' || typeof p['amita.control.revision'] !== 'number' ||
    typeof p['amita.control.expires_at'] !== 'number' || typeof p['amita.control.decision'] !== 'string') {
    throw new Error('phone_control_approval_callback_invalid');
  }
  const value: OwnerControlDecision = { approval_id: p['amita.control.approval_id'] as string,
    nonce: p['amita.control.nonce'] as string, operation_id: p['amita.control.operation_id'] as string,
    task_id: p['amita.control.task_id'] as string, revision: p['amita.control.revision'] as number,
    expires_at: p['amita.control.expires_at'] as number, decision: p['amita.control.decision'] as string };
  if (want.action !== 'org.personalassistant.mesh.control.' + value.approval_id + '.' + value.decision) {
    throw new Error('phone_control_approval_callback_invalid');
  }
  if (value.decision === 'review') { return false; }
  await session.decideBound(value);
  return true; // A local decision was recorded, not proof that any input ran.
}
async function decisionAgent(ticket: ApprovalTicket, decision: string, offset: number): Promise<WantAgent> {
  let code: number = 0;
  for (let i: number = 0; i < ticket.approval_id.length; i++) {
    code = (code * 31 + ticket.approval_id.charCodeAt(i)) % 536870911;
  }
  const want: Want = { bundleName: 'org.personalassistant.mesh', abilityName: 'EntryAbility',
    action: 'org.personalassistant.mesh.control.' + ticket.approval_id + '.' + decision,
    parameters: { 'amita.control.approval_id': ticket.approval_id, 'amita.control.nonce': ticket.nonce,
      'amita.control.operation_id': ticket.operation_id, 'amita.control.task_id': ticket.task_id,
      'amita.control.revision': ticket.revision, 'amita.control.expires_at': ticket.expires_at,
      'amita.control.decision': decision } };
  return wantAgent.getWantAgent({ wants: [want], requestCode: code * 3 + offset,
    operationType: wantAgent.OperationType.START_ABILITY,
    wantAgentFlags: [wantAgent.WantAgentFlags.ONE_TIME_FLAG, wantAgent.WantAgentFlags.CONSTANT_FLAG] });
}
