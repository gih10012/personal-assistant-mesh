import { common } from '@kit.AbilityKit';
import { ScreenShareSession, ScreenShareAuthorization, ScreenShareFrameSink,
  ScreenShareSessionStatus } from './ScreenShareSession';
import { ScreenShareScope, ScreenShareFrame, ScreenShareFrameAck } from './ScreenSharePolicy';
import { WorkConfig, WorkRecord } from '../core/WorkProtocol';
import { PhoneScreenShareSink, PhoneScreenShareGate } from './PhoneScreenShareSink';

// One original session owner outlives a particular page. Installation and
// foreground changes never start capture, request permission or open a sink.
const session: ScreenShareSession = new ScreenShareSession();
let context: common.UIAbilityContext | null = null;
let managedSink: PhoneScreenShareSink | null = null;
let sourceClosing: Promise<void> | null = null;
let sourceCloseUnknown: boolean = false;
interface AcknowledgedFrame { revision: number; sha256: string; }
let latestFrame: AcknowledgedFrame | null = null;
let previousFrame: AcknowledgedFrame | null = null;

export interface ScreenShareOwnerStatus extends ScreenShareSessionStatus {
  lastAcknowledgedSha256: string;
}

function closeManagedSource(sink: PhoneScreenShareSink): Promise<void> {
  if (sourceClosing !== null) { return sourceClosing; }
  sourceClosing = sink.close().catch((): void => {
    sourceCloseUnknown = true;
    throw new Error('screen_share_source_close_unconfirmed');
  }).finally((): void => {
    if (managedSink === sink) { managedSink = null; }
    sourceClosing = null;
  });
  return sourceClosing;
}

export class ScreenShareOwner {
  static attach(value: common.UIAbilityContext): void {
    if (context !== null && context !== value) { throw new Error('screen_share_owner_context_changed'); }
    context = value;
  }

  // The original task and allocation supply an exact live binding from either
  // the owner's gesture or the configured standing authorization.
  static start(scope: ScreenShareScope, authorization: ScreenShareAuthorization,
    sink: ScreenShareFrameSink): void {
    if (context === null || AppStorage.get<boolean>('meshForeground') !== true ||
      !authorization.isForeground() || !authorization.valid(scope) || !sink.isLive(scope)) {
      throw new Error('screen_share_original_authorized_foreground_required');
    }
    session.start(context as common.UIAbilityContext, scope, authorization, sink);
  }

  static foregroundChanged(): void { session.foregroundChanged(); }
  static liveFrameRevision(scope: ScreenShareScope): number { return session.liveFrameRevision(scope); }
  static status(): ScreenShareOwnerStatus {
    const value: ScreenShareSessionStatus = session.status();
    const ack = latestFrame?.revision === value.lastFrameRevision ? latestFrame : previousFrame;
    return { state: value.state, captureActive: value.captureActive, backgroundActive: value.backgroundActive,
      acknowledgedFrames: value.acknowledgedFrames, lastFrameRevision: value.lastFrameRevision,
      lastCaptureTimestamp: value.lastCaptureTimestamp, lastAckObservedAt: value.lastAckObservedAt,
      error: value.error, nativeCode: value.nativeCode, cleanupUnknown: value.cleanupUnknown,
      lastAcknowledgedSha256: ack !== null && ack.revision === value.lastFrameRevision ? ack.sha256 : '' };
  }
  static sourceCloseUnconfirmed(): boolean { return sourceCloseUnknown; }

  // The original streaming executor supplies its durable WorkRecord. This
  // opens no new task and the first frame never settles the original record.
  static async startManaged(primary: WorkConfig, controlSessionId: string, record: WorkRecord,
    scope: ScreenShareScope, authorization: ScreenShareAuthorization, gate: PhoneScreenShareGate): Promise<void> {
    if (context === null || managedSink !== null || sourceClosing !== null ||
      AppStorage.get<boolean>('meshForeground') !== true || !authorization.isForeground() ||
      !authorization.valid(scope)) { throw new Error('screen_share_original_authorized_foreground_required'); }
    const sink: PhoneScreenShareSink = new PhoneScreenShareSink(primary, controlSessionId, record, scope, gate);
    managedSink = sink; sourceCloseUnknown = false; latestFrame = null; previousFrame = null;
    const bound: ScreenShareFrameSink = {
      isLive: (value: ScreenShareScope): boolean => sink.isLive(value),
      onDisconnect: (listener: () => void): (() => void) => sink.onDisconnect(listener),
      sendFrame: async (value: ScreenShareScope, frame: ScreenShareFrame): Promise<ScreenShareFrameAck> => {
        const ack: ScreenShareFrameAck = await sink.sendFrame(value, frame);
        if (managedSink === sink) {
          previousFrame = latestFrame;
          latestFrame = { revision: ack.frameRevision, sha256: sink.status().lastAcknowledgedSha256 };
        }
        return ack;
      },
      detach: (value: ScreenShareScope): void => {
        sink.detach(value);
        closeManagedSource(sink).catch((): void => {});
      }
    };
    try {
      await sink.open();
      ScreenShareOwner.start(scope, authorization, bound);
    } catch (_) {
      sink.detach(scope);
      try { await closeManagedSource(sink); } catch (_) {}
      throw new Error('screen_share_managed_start_unconfirmed');
    }
  }

  static async stop(): Promise<void> {
    await session.stop('screen_share_owner_stopped');
    if (managedSink !== null) { await closeManagedSource(managedSink); }
    else if (sourceClosing !== null) { await sourceClosing; }
    if (sourceCloseUnknown) { throw new Error('screen_share_source_close_unconfirmed'); }
  }
  static async destroyed(): Promise<void> {
    try { await ScreenShareOwner.stop(); }
    finally { context = null; }
  }
}
