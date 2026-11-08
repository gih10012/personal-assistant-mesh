import { Want, WantAgent, wantAgent } from '@kit.AbilityKit';
import { ApprovalTicket, ControlSession, OwnerControlDecision } from './ControlSession';
import { ApprovalActions } from './ApprovalSurface';
import { CONTROL_BUNDLE, CONTROL_DECISION_EVENT, CONTROL_DECISION_PAYLOAD } from './BackgroundDecisionProtocol';

// Public fallback: opens the app's own existing UIAbility. This does not pretend
// to be a background callback. Root must route onCreate/onNewWant to the same
// private pending approval journal and verify the exact ticket before deciding.
// No remote-supplied bundle, ability, URL or command is accepted.
export async function buildApprovalWantAgents(ticket: ApprovalTicket, background: boolean = false): Promise<ApprovalActions> {
  const review: WantAgent = await decisionAgent(ticket, 'review', 0);
  const approve: WantAgent = background ? await backgroundAgent(ticket, 'approve', 1) : await decisionAgent(ticket, 'approve', 1);
  const deny: WantAgent = background ? await backgroundAgent(ticket, 'deny', 2) : await decisionAgent(ticket, 'deny', 2);
  if (background && (await wantAgent.getBundleName(review) !== CONTROL_BUNDLE ||
    await wantAgent.getBundleName(approve) !== CONTROL_BUNDLE || await wantAgent.getBundleName(deny) !== CONTROL_BUNDLE)) {
    throw new Error('phone_control_background_agent_creator_mismatch');
  }
  return { review: review, approve: approve, deny: deny };
}
async function backgroundAgent(ticket: ApprovalTicket, decision: string, offset: number): Promise<WantAgent> {
  const value: OwnerControlDecision = { approval_id: ticket.approval_id, nonce: ticket.nonce,
    operation_id: ticket.operation_id, task_id: ticket.task_id, revision: ticket.revision,
    expires_at: ticket.expires_at, decision: decision };
  const parameters: Record<string, Object> = {};
  parameters[CONTROL_DECISION_PAYLOAD] = JSON.stringify(value);
  const want: Want = { bundleName: CONTROL_BUNDLE, action: CONTROL_DECISION_EVENT,
    uri: 'amita-control://decision/' + ticket.approval_id + '/' + decision, parameters: parameters };
  return wantAgent.getWantAgent({ wants: [want], requestCode: offset,
    actionType: wantAgent.OperationType.SEND_COMMON_EVENT,
    actionFlags: [wantAgent.WantAgentFlags.ONE_TIME_FLAG, wantAgent.WantAgentFlags.CONSTANT_FLAG] });
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
