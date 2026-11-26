import { Want, WantAgent } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { ApprovalPresentation, ApprovalSurface, SurfaceAction } from './ApprovalSurface';
import { buildControlEntryWantAgent } from './ApprovalWantAgents';
import { CONTROL_BUNDLE } from './BackgroundDecisionProtocol';

const SURFACE_ID: number = 731902;
const WANT_PREFIX: string = CONTROL_BUNDLE + '.computer-use.';
const OWNER_VIEW: string = 'amitaComputerUseOwnerView';

export interface ComputerUseScope {
  operationId: string;
  taskId: string;
  expiresAt: number;
}

export class ComputerUseOwnerView {
  nonce: string = '';
  phase: string = '';
  surface: string = '';
  liveviewError: string = '';
  notificationError: string = '';
  deciding: boolean = false;
  decisionUnconfirmed: boolean = false;
  canStop: boolean = false;
}

interface ComputerUseConflict {
  id: string;
  expiresAt: number;
  decide: (decision: string) => Promise<boolean>;
}

class ActiveComputerUse {
  scope: ComputerUseScope;
  surface: ApprovalSurface;
  stop: (() => Promise<boolean>) | undefined;
  nonce: string = util.generateRandomUUID();
  phase: string = 'operating';
  conflict: ComputerUseConflict | null = null;
  lastConflictId: string = '';
  deciding: boolean = false;
  decisionUnconfirmed: boolean = false;
  notificationAttempted: boolean = false;
  publication: Promise<ApprovalPresentation> = Promise.resolve(inApp(false));
  presentation: ApprovalPresentation = inApp(false);

  constructor(scope: ComputerUseScope, entitledEvent: string, stop?: () => Promise<boolean>) {
    this.scope = { operationId: scope.operationId, taskId: scope.taskId, expiresAt: scope.expiresAt };
    this.surface = new ApprovalSurface(SURFACE_ID, entitledEvent === 'PROGRESS' ? entitledEvent : '');
    this.stop = stop;
  }
}

function inApp(pending: boolean): ApprovalPresentation {
  return { surface: pending ? 'in_app_pending' : 'in_app_status', presented: false, pending: pending,
    liveview_error: '', notification_error: '' };
}

function validId(value: string): boolean {
  return /^[A-Za-z0-9._:-]{1,160}$/.test(value);
}

function sameOperation(left: ComputerUseScope, right: ComputerUseScope): boolean {
  return left.operationId === right.operationId && left.taskId === right.taskId && left.expiresAt === right.expiresAt;
}

// UIAbility-local presentation for the provider's already-admitted original operation.
// Providers own the lease, durable receipts, conflict evidence and effect boundary.
export class ComputerUseSurface {
  private static active: ActiveComputerUse | null = null;
  private static timer: number = -1;
  private static platform: Promise<void> = Promise.resolve();

  static operating(scope: ComputerUseScope, entitledEvent: string, stop?: () => Promise<boolean>): Promise<ApprovalPresentation> {
    if (!validId(scope.operationId) || !validId(scope.taskId) || !Number.isSafeInteger(scope.expiresAt) ||
      scope.expiresAt <= Date.now() || scope.expiresAt - Date.now() > 28800000) {
      throw new Error('phone_computer_use_surface_scope_invalid');
    }
    let current: ActiveComputerUse | null = ComputerUseSurface.active;
    if (current !== null && !sameOperation(current.scope, scope)) {
      throw new Error('phone_computer_use_surface_operation_mismatch');
    }
    if (current === null) {
      current = new ActiveComputerUse(scope, entitledEvent, stop);
      ComputerUseSurface.active = current;
    } else if (current.phase === 'conflict' || current.phase === 'operating') {
      return current.publication;
    }
    current.conflict = null;
    return ComputerUseSurface.transition(current, 'operating');
  }

  // Call only after the provider has paused the original operation for this confirmed conflict.
  static conflict(scope: ComputerUseScope, conflictId: string, expiresAt: number,
    decide: (decision: string) => Promise<boolean>): Promise<ApprovalPresentation> {
    const current: ActiveComputerUse | null = ComputerUseSurface.active;
    if (current === null || !sameOperation(current.scope, scope) || !validId(conflictId) ||
      !Number.isSafeInteger(expiresAt) || expiresAt > scope.expiresAt || expiresAt <= Date.now()) {
      throw new Error('phone_computer_use_surface_conflict_invalid');
    }
    if (current.lastConflictId === conflictId) { return current.publication; }
    if (current.phase !== 'operating' || current.deciding) {
      throw new Error('phone_computer_use_surface_conflict_already_pending');
    }
    current.conflict = { id: conflictId, expiresAt: expiresAt, decide: decide };
    current.lastConflictId = conflictId;
    return ComputerUseSurface.transition(current, 'conflict');
  }

  static async close(scope: ComputerUseScope): Promise<string> {
    const current: ActiveComputerUse | null = ComputerUseSurface.active;
    if (current === null || !sameOperation(current.scope, scope)) { return ''; }
    ComputerUseSurface.active = null;
    clearTimeout(ComputerUseSurface.timer);
    ComputerUseSurface.timer = -1;
    ComputerUseSurface.updateOwnerView();
    const ending: Promise<string> = ComputerUseSurface.platform.then(async (): Promise<string> => {
      await ComputerUseSurface.clearNotification(current);
      return current.surface.clearLiveView();
    });
    ComputerUseSurface.platform = ending.then((): void => {}, (): void => {});
    return ending;
  }

  static async onNewWant(want: Want): Promise<boolean> {
    if (want.action === undefined || !want.action.startsWith(WANT_PREFIX)) { return false; }
    const nonce: Object | undefined = want.parameters?.['amita.computer_use.nonce'];
    const decision: Object | undefined = want.parameters?.['amita.computer_use.decision'];
    if (typeof nonce === 'string' && typeof decision === 'string' &&
      want.action === WANT_PREFIX + nonce + '.' + decision && decision !== 'open') {
      await ComputerUseSurface.decide(nonce, decision);
    }
    return true;
  }

  static async decide(nonce: string, decision: string): Promise<boolean> {
    const current: ActiveComputerUse | null = ComputerUseSurface.active;
    if (current === null || current.nonce !== nonce || current.deciding ||
      ComputerUseSurface.deadline(current) <= Date.now()) { return false; }
    const conflict: ComputerUseConflict | null = current.conflict;
    const review: boolean = current.phase === 'conflict' && conflict !== null && ['continue', 'stop'].includes(decision);
    const stop: (() => Promise<boolean>) | undefined = current.stop;
    if (!review && !(current.phase === 'operating' && decision === 'stop' && stop !== undefined)) { return false; }
    current.deciding = true;
    current.decisionUnconfirmed = false;
    ComputerUseSurface.updateOwnerView();
    let recorded: boolean = false;
    try {
      if (review && conflict !== null) { recorded = await conflict.decide(decision); }
      else if (stop !== undefined) { recorded = await stop(); }
      if (!recorded || ComputerUseSurface.active !== current) { return recorded; }
      if (decision === 'stop') { await ComputerUseSurface.close(current.scope); }
      else if (ComputerUseSurface.deadline(current) > Date.now()) {
        current.conflict = null;
        // Wait for the provider to report that operation is actually running again.
        ComputerUseSurface.transition(current, 'resuming');
      }
      return true;
    } catch (_) { return false; }
    finally {
      current.deciding = false;
      current.decisionUnconfirmed = !recorded;
      ComputerUseSurface.updateOwnerView();
    }
  }

  private static deadline(current: ActiveComputerUse): number {
    return current.conflict?.expiresAt ?? current.scope.expiresAt;
  }

  private static transition(current: ActiveComputerUse, phase: string): Promise<ApprovalPresentation> {
    current.phase = phase;
    current.nonce = util.generateRandomUUID();
    current.decisionUnconfirmed = false;
    const nonce: string = current.nonce;
    const pending: boolean = phase === 'conflict';
    current.presentation = inApp(pending);
    ComputerUseSurface.updateOwnerView();
    const expiresAt: number = ComputerUseSurface.deadline(current);
    clearTimeout(ComputerUseSurface.timer);
    ComputerUseSurface.timer = setTimeout((): void => {
      ComputerUseSurface.close(current.scope).catch((): void => {});
    }, expiresAt - Date.now());
    const isCurrent = (): boolean => ComputerUseSurface.active === current && current.nonce === nonce && expiresAt > Date.now();
    const publication: Promise<ApprovalPresentation> = ComputerUseSurface.platform.then(async (): Promise<ApprovalPresentation> => {
      if (!isCurrent()) { return inApp(pending); }
      const presentation: ApprovalPresentation = await ComputerUseSurface.present(current, phase, nonce, expiresAt, isCurrent);
      if (isCurrent()) {
        current.presentation = presentation;
        ComputerUseSurface.updateOwnerView();
      }
      return presentation;
    });
    current.publication = publication;
    ComputerUseSurface.platform = publication.then((): void => {}, (): void => {});
    return publication;
  }

  private static async present(current: ActiveComputerUse, phase: string, nonce: string, expiresAt: number,
    isCurrent: () => boolean): Promise<ApprovalPresentation> {
    const pending: boolean = phase === 'conflict';
    try {
      await ComputerUseSurface.clearNotification(current);
      if (!isCurrent()) { return inApp(pending); }
      const open: WantAgent = await ComputerUseSurface.agent(nonce, 'open', 0);
      const buttons: SurfaceAction[] = [];
      if (pending) {
        buttons.push({ title: '继续原任务', agent: await ComputerUseSurface.agent(nonce, 'continue', 1) });
      }
      if (pending || phase === 'operating' && current.stop !== undefined) {
        buttons.push({ title: '停止', agent: await ComputerUseSurface.agent(nonce, 'stop', 2) });
      }
      const capsule: string = pending ? '等待确认' : phase === 'operating' ? '屏幕读取中' : '等待恢复';
      current.notificationAttempted = current.notificationAttempted || pending;
      const presentation: ApprovalPresentation = await current.surface.presentCard({
        title: 'AMITA · ' + capsule,
        text: pending ? '检测到本人操作，已暂停手机操控' : phase === 'operating' ?
          '正在读取已授权共享的屏幕' : '已确认继续，正在核对当前状态',
        detail: pending ? '继续原任务或停止' : '查看手机操控状态',
        expiresAt: expiresAt, pending: pending, silent: !pending, notificationFallback: pending,
        open: open, buttons: buttons, capsuleTitle: capsule
      }, isCurrent);
      if (presentation.surface !== 'liveview') { await current.surface.clearLiveView(); }
      return presentation;
    } catch (_) {
      const fallback: ApprovalPresentation = inApp(pending);
      fallback.liveview_error = 'liveview_presentation_unconfirmed';
      await current.surface.clearLiveView();
      return fallback;
    }
  }

  private static async clearNotification(current: ActiveComputerUse): Promise<void> {
    if (!current.notificationAttempted) { return; }
    try {
      await current.surface.clearNotification();
      current.notificationAttempted = false;
    } catch (_) {}
  }

  private static agent(nonce: string, decision: string, offset: number): Promise<WantAgent> {
    let code: number = 0;
    for (let i: number = 0; i < nonce.length; i++) { code = (code * 31 + nonce.charCodeAt(i)) % 536870911; }
    const want: Want = { bundleName: CONTROL_BUNDLE, abilityName: 'EntryAbility',
      action: WANT_PREFIX + nonce + '.' + decision,
      parameters: { 'amita.computer_use.nonce': nonce, 'amita.computer_use.decision': decision } };
    return buildControlEntryWantAgent(want, code * 3 + offset, decision !== 'open');
  }

  private static updateOwnerView(): void {
    const current: ActiveComputerUse | null = ComputerUseSurface.active;
    const view: ComputerUseOwnerView = new ComputerUseOwnerView();
    if (current !== null) {
      view.nonce = current.nonce;
      view.phase = current.phase;
      view.surface = current.presentation.surface;
      view.liveviewError = current.presentation.liveview_error;
      view.notificationError = current.presentation.notification_error;
      view.deciding = current.deciding;
      view.decisionUnconfirmed = current.decisionUnconfirmed;
      view.canStop = current.phase === 'conflict' || current.phase === 'operating' && current.stop !== undefined;
    }
    AppStorage.setOrCreate<ComputerUseOwnerView>(OWNER_VIEW, view);
  }
}
