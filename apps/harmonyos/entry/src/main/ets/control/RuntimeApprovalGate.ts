import type { ApprovalTicket, ControlFence, OwnerControlDecision } from './ControlSession';

export interface RuntimePermit {
  ticket: ApprovalTicket;
  dispatch_json: string;
  input_json: string;
  input_hash: string;
  owner_grant: string;
  permission_context: string;
  action_kind: string;
  fence: ControlFence;
  state: string;
  hardware_armed: boolean;
}

// One live local decision slot, not another task/operation ledger. Source
// identity/outcome remain solely in WorkEngine; action approval uses the
// existing ControlSession journal. No timeout or presentation can approve.
export class RuntimeApprovalGate {
  private permit: RuntimePermit | null = null;
  private resolve: ((approved: boolean) => void) | null = null;
  private timer: number = -1;
  private clock: () => number;
  constructor(clock: () => number = (): number => Date.now()) { this.clock = clock; }
  current(): RuntimePermit | null { return this.permit; }
  begin(permit: RuntimePermit): Promise<boolean> {
    if (this.permit !== null || permit.ticket.expires_at <= this.clock() || permit.state !== 'awaiting_approval') {
      throw new Error('phone_control_approval_slot_unavailable');
    }
    this.permit = permit;
    return new Promise<boolean>((resolve: (approved: boolean) => void): void => {
      this.resolve = resolve;
      this.timer = setTimeout((): void => { this.cancel(); }, Math.max(1, permit.ticket.expires_at - this.clock()));
    });
  }
  armHardware(): void {
    if (this.permit !== null && this.permit.state === 'awaiting_approval') { this.permit.hardware_armed = true; }
  }
  matches(value: OwnerControlDecision): boolean {
    const p: RuntimePermit | null = this.permit;
    return p !== null && p.state === 'awaiting_approval' && p.ticket.expires_at > this.clock() &&
      value.approval_id === p.ticket.approval_id && value.nonce === p.ticket.nonce &&
      value.operation_id === p.ticket.operation_id && value.task_id === p.ticket.task_id &&
      value.revision === p.ticket.revision && value.expires_at === p.ticket.expires_at &&
      (value.decision === 'approve' || value.decision === 'deny');
  }
  complete(approved: boolean): void {
    const p: RuntimePermit | null = this.permit;
    if (p === null || p.state !== 'awaiting_approval') { throw new Error('phone_control_no_pending_approval'); }
    if (p.ticket.expires_at <= this.clock()) { this.cancel(); return; }
    if (this.timer !== -1) { clearTimeout(this.timer); this.timer = -1; }
    p.state = approved ? 'approved' : 'denied';
    p.hardware_armed = false;
    const resolve: ((approved: boolean) => void) | null = this.resolve;
    this.resolve = null;
    if (!approved) { p.fence.valid = false; this.permit = null; }
    if (resolve !== null) { resolve(approved); }
  }
  allowed(dispatchJson: string, ownerGrant: string, context?: string): boolean {
    const p: RuntimePermit | null = this.permit;
    return p !== null && p.state === 'approved' && p.fence.valid && p.ticket.expires_at > this.clock() &&
      p.dispatch_json === dispatchJson && p.owner_grant === ownerGrant &&
      (context === undefined || context === p.permission_context);
  }
  consume(dispatchJson: string, ownerGrant: string): RuntimePermit {
    if (!this.allowed(dispatchJson, ownerGrant)) { throw new Error('phone_control_same_operation_approval_required'); }
    const p: RuntimePermit = this.permit as RuntimePermit;
    p.state = 'consumed'; return p;
  }
  cancel(): void {
    if (this.timer !== -1) { clearTimeout(this.timer); this.timer = -1; }
    if (this.permit !== null) { this.permit.fence.valid = false; this.permit.state = 'cancelled'; }
    this.permit = null;
    const resolve: ((approved: boolean) => void) | null = this.resolve;
    this.resolve = null;
    if (resolve !== null) { resolve(false); }
  }
}
