export interface ScreenShareScope {
  sessionId: string;
  taskId: string;
  operationId: string;
  approvalId: string;
  nonce: string;
  revision: number;
  expiresAt: number;
}

export interface ScreenShareFrame {
  bytes: Uint8Array;
  timestamp: number; // Actual capture monotonic milliseconds, not wall clock.
  width: number;
  height: number;
  revision: number;
}

export interface ScreenShareFrameAck {
  scope: ScreenShareScope;
  frameRevision: number;
  bytes: number;
  acknowledged: boolean; // Verified original remote receiver acknowledgement.
}

function requireShare(value: boolean, code: string): void {
  if (!value) { throw new Error(code); }
}

export function copyScreenShareScope(value: ScreenShareScope): ScreenShareScope {
  return { sessionId: value.sessionId, taskId: value.taskId, operationId: value.operationId,
    approvalId: value.approvalId, nonce: value.nonce, revision: value.revision, expiresAt: value.expiresAt };
}

export function validScreenShareScope(value: ScreenShareScope, now: number): boolean {
  return [value.sessionId, value.taskId, value.operationId, value.approvalId, value.nonce].every(
    (part: string): boolean => typeof part === 'string' && part.length > 0 && part.length <= 512) &&
    Number.isSafeInteger(value.revision) && value.revision >= 0 &&
    Number.isSafeInteger(value.expiresAt) && value.expiresAt > now;
}

export function sameScreenShareScope(left: ScreenShareScope, right: ScreenShareScope): boolean {
  return left.sessionId === right.sessionId && left.taskId === right.taskId &&
    left.operationId === right.operationId && left.approvalId === right.approvalId && left.nonce === right.nonce &&
    left.revision === right.revision && left.expiresAt === right.expiresAt;
}

// Small in-memory lifecycle policy. It cannot issue authorization or renew a
// lease; the trusted adapter supplies current authorization and receiver state.
export class ScreenSharePolicy {
  readonly scope: ScreenShareScope;
  active: boolean = true;
  captureActive: boolean = false;
  backgroundTaskId: number = -1;
  acknowledgedFrames: number = 0;
  lastFrameRevision: number = 0;
  lastCaptureTimestamp: number = -1;
  lastAcknowledgedRevision: number = 0;
  lastAckObservedAt: number = 0;

  constructor(scope: ScreenShareScope, now: number) {
    requireShare(validScreenShareScope(scope, now), 'screen_share_original_scope_required');
    this.scope = copyScreenShareScope(scope);
  }

  healthy(authorised: boolean, sinkLive: boolean, now: number): boolean {
    return this.active && authorised && sinkLive && validScreenShareScope(this.scope, now);
  }

  started(): void {
    requireShare(this.active, 'screen_share_session_stopped');
    this.captureActive = true;
  }

  acceptFrame(frame: ScreenShareFrame, authorised: boolean, sinkLive: boolean, now: number): void {
    requireShare(this.captureActive && this.healthy(authorised, sinkLive, now), 'screen_share_capture_not_active');
    requireShare(frame.bytes instanceof Uint8Array && frame.bytes.length >= 4 && frame.bytes.length <= 42000 &&
      frame.bytes[0] === 255 && frame.bytes[1] === 216 && frame.bytes[2] === 255 &&
      frame.bytes[frame.bytes.length - 2] === 255 && frame.bytes[frame.bytes.length - 1] === 217 &&
      Number.isSafeInteger(frame.width) && frame.width > 0 && frame.width <= 640 &&
      Number.isSafeInteger(frame.height) && frame.height > 0 && frame.height <= 640 &&
      Number.isSafeInteger(frame.revision) && frame.revision > this.lastFrameRevision &&
      Number.isFinite(frame.timestamp) && frame.timestamp >= 0 && frame.timestamp >= this.lastCaptureTimestamp,
    'screen_share_frame_invalid');
    this.lastFrameRevision = frame.revision;
    this.lastCaptureTimestamp = frame.timestamp;
  }

  acknowledge(frame: ScreenShareFrame, ack: ScreenShareFrameAck, now: number): void {
    requireShare(this.active && this.captureActive && ack.acknowledged === true &&
      sameScreenShareScope(this.scope, ack.scope) && ack.frameRevision === frame.revision &&
      ack.bytes === frame.bytes.length && frame.revision > this.lastAcknowledgedRevision,
    'screen_share_frame_ack_unconfirmed');
    this.lastAcknowledgedRevision = frame.revision;
    this.acknowledgedFrames += 1;
    this.lastAckObservedAt = now;
  }

  canRequestBackground(authorised: boolean, sinkLive: boolean, now: number): boolean {
    return this.captureActive && this.acknowledgedFrames > 0 && this.backgroundTaskId < 0 &&
      this.healthy(authorised, sinkLive, now);
  }

  backgroundStarted(id: number): void {
    requireShare(this.active && this.captureActive && this.acknowledgedFrames > 0 &&
      Number.isSafeInteger(id) && id >= 0, 'screen_share_background_ticket_invalid');
    this.backgroundTaskId = id;
  }

  canRemainBackground(authorised: boolean, sinkLive: boolean, now: number): boolean {
    // No frame-idle deadline: a real static screen need not emit new frames.
    return this.backgroundTaskId >= 0 && this.captureActive && this.acknowledgedFrames > 0 &&
      this.healthy(authorised, sinkLive, now);
  }

  stop(): void { this.active = false; this.captureActive = false; }
  backgroundReleased(): void { this.backgroundTaskId = -1; }
}
