import nativeScreenShare from 'libamita_screenshare.so';

export interface NativeScreenShareOptions {
  maxWidth: number;
  maxHeight: number;
  maxBytes: number;
}
export interface NativeScreenShareState { state: string; errorCode: number; }
export interface NativeScreenShareFrame {
  bytes: Uint8Array;
  // Source capture nanoseconds / 1e6: monotonic milliseconds, not wall-clock Date.now().
  timestamp: number;
  width: number;
  height: number;
  revision: number;
}

export class NativeScreenShare {
  private static owner: NativeScreenShare | undefined = undefined;
  private generation: number = 0;
  private running: boolean = false;
  private started: boolean = false;
  private pendingFrame: NativeScreenShareFrame | undefined = undefined;

  start(options: NativeScreenShareOptions, onState: (state: NativeScreenShareState) => void,
    onFrame: (frame: NativeScreenShareFrame) => void): void {
    if (this.running || (NativeScreenShare.owner !== undefined && NativeScreenShare.owner !== this)) {
      throw new Error('screen_share_already_started');
    }
    if (!Number.isInteger(options.maxWidth) || options.maxWidth < 32 || options.maxWidth > 640 ||
      !Number.isInteger(options.maxHeight) || options.maxHeight < 32 || options.maxHeight > 640 ||
      !Number.isInteger(options.maxBytes) || options.maxBytes < 1024 || options.maxBytes > 42000) {
      throw new Error('screen_share_options_invalid');
    }
    const generation = ++this.generation;
    this.running = true; this.started = false; this.pendingFrame = undefined;
    NativeScreenShare.owner = this;
    // Both sinks are installed in the native call before user consent/capture startup.
    try {
      nativeScreenShare.start(options, (state: NativeScreenShareState): void => {
        if (generation !== this.generation) { return; }
        if (state.state === 'started') { this.started = true; }
        if (state.state === 'paused') { this.started = false; }
        if (state.state === 'resumed' && this.running) { this.started = true; }
        if (state.state === 'error' || state.state === 'stopped' || state.state === 'canceled' ||
          state.state === 'interrupted') {
          this.running = false; this.started = false;
          this.pendingFrame = undefined;
        }
        onState(state);
        // The independent native TSFN queues may reach JS in either order.
        // Native frames already require real STARTED; hold only the latest one
        // until its state notification has reached the session controller.
        if (state.state === 'started' && generation === this.generation && this.running &&
          this.started && this.pendingFrame !== undefined) {
          const pending = this.pendingFrame; this.pendingFrame = undefined;
          onFrame(pending);
        }
      }, (frame: NativeScreenShareFrame): void => {
        if (generation !== this.generation || !this.running) { return; }
        if (!this.started) { this.pendingFrame = frame; return; }
        onFrame(frame);
      });
    } catch (error) {
      this.running = false; this.started = false; this.pendingFrame = undefined; this.generation++;
      if ((error as Error).message === 'screen_share_already_started') {
        NativeScreenShare.owner = undefined;
        throw new Error('screen_share_already_started');
      }
      try { nativeScreenShare.stop(); }
      catch (_cleanupError) { throw new Error('screen_share_native_start_cleanup_unknown'); }
      NativeScreenShare.owner = undefined;
      throw new Error('screen_share_native_start_failed');
    }
  }

  stop(): void {
    if (NativeScreenShare.owner !== this) { return; }
    // Revoke this instance's callbacks before waiting for native worker/capture cleanup.
    this.generation++; this.running = false; this.started = false; this.pendingFrame = undefined;
    nativeScreenShare.stop();
    // On an actual native cleanup failure, keep ownership and propagate the error;
    // another wrapper must not replace a capture whose release is unknown.
    NativeScreenShare.owner = undefined;
  }
}
