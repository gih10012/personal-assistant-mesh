export interface NativeScreenShareOptions {
  maxWidth: number;
  maxHeight: number;
  maxBytes: number;
}

export interface NativeScreenShareState {
  state: string;
  errorCode: number;
}

export interface NativeScreenShareFrame {
  bytes: Uint8Array;
  /** Actual capture timestamp, monotonic milliseconds, not wall-clock Date.now(). */
  timestamp: number;
  width: number;
  height: number;
  revision: number;
}

declare const nativeScreenShare: {
  start(options: NativeScreenShareOptions, onState: (state: NativeScreenShareState) => void,
    onFrame: (frame: NativeScreenShareFrame) => void): void;
  stop(): void;
};
export default nativeScreenShare;
