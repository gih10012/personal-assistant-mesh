# Actual screen-sharing session

This module is an in-process owner session, not a daemon or an authorization
issuer. Root supplies the existing trusted task/node adapter and native module.
It has no page entry, manifest edits, remote URL, arbitrary shell route, audio,
recording file, fake animation, LiveView or persistent permission switch.

```arkts
import { ScreenShareSession } from '../screenshare/ScreenShareSession';
// Retain one instance at UIAbility/application lifecycle level, not in a page
// destroyed on background. NativeScreenShare owns the real capture source.
const share = new ScreenShareSession();
// ONLY inside the real owner's explicit UI gesture, after existing adapter
// validates the original operation/approval and live authenticated receiver:
share.start(uiAbilityContext, originalScope, authorizationAdapter, frameSink);
// UIAbility onForeground/onBackground, after the adapter updates foreground:
share.foregroundChanged();
// Owner Stop / app shutdown:
await share.stop();
```

`originalScope` contains sessionId, taskId, mandatory positive-integer taskEpoch,
operationId, approvalId, nonce, revision and expiresAt (wall-clock ms). taskEpoch
must be read from the actual original SourceWorkRecord; never infer or generate
an epoch from task ID, current time, UI state or a local counter. The entire
binding including taskEpoch is copied, validated and compared, never renewed by this
module. These values must come from the original durable trusted task/approval
path; deserializing a remote flag or copying a previous successful receipt does
not authorize a new session. The adapter checks the exact original tuple,
current node credential/lease, owner revocation and dispatch state on every
valid() call. Root must prevent replay across app restarts; this in-memory
session also refuses to restart its consumed original scope.

The scope is strictly **internal to the app**. In particular approvalId and nonce
are local owner UI/action data and MUST NOT be serialized into a remote frame
request, source record, URL, log or remote acknowledgement. The sink receives
the internal scope only to validate against its captured original binding.
Root's transport sends an opaque session-binding digest plus original task ID,
operation ID, task epoch, frame hash and frame revision. After verifying the
remote receiver ACK for those public binding fields and actual frame bytes, the
adapter constructs the in-process `ack.scope` from its captured original local
scope. It must never require the source to receive or echo the local UI action
nonce, or copy arbitrary remote scope fields into that acknowledgement.

Required `ScreenShareAuthorization` methods:

- `isForeground(): boolean`: actual current UIAbility state.
- `valid(scope): boolean`: current trusted original authorization/lease; never a
  cached/persisted permit boolean. No async model call is needed.
- `onRevoked(listener): unsubscribe`: immediate revocation/lease loss event.
- `closeHdc(scope): void`: release only this session's own HDC coordination;
  never stop a different task or quietly reconnect HDC.

Required `ScreenShareFrameSink` methods:

- `isLive(scope): boolean`: true only while an actual authenticated receiving
  stream is attached to the exact original task/session. A metadata POST or
  provider-local queue is insufficient. Read existing transport state; no new
  connection is created here.
- `onDisconnect(listener): unsubscribe`: actual receiver loss event.
- `sendFrame(scope, frame): Promise<ScreenShareFrameAck>`: deliver the actual
  native JPEG bytes, <= 42000 bytes and <= 640 per edge, with native monotonic
  capture timestamp and increasing revision. Resolve only after the original
  receiver's acknowledgement is verified. Return the exact captured in-process
  original scope (including taskEpoch), frameRevision, byte count and
  acknowledged=true. This return object is local; it is not the remote wire ACK.
  A no-op/local success result,
  D1 metadata, a replaced task or an accepted outbound queue is not an ACK.
- `detach(scope): void`: detach/cancel only this session's frame route and
  in-flight delivery; preserve the shared Mesh node connection and original
  unknown receipt. Never automatically resend a frame with an unknown ACK.

Lifecycle and qualification:

1. Root has supplied a valid original scope and real live receiver; a real owner
   UI gesture calls start. Native callbacks are installed before capture starts.
2. `start_requested` only means the OS privacy dialog/start was requested.
   Only native `started` marks capture active; no frame is sent before that.
3. At least one actual frame must receive an exact receiver ACK. Only then does
   this module request `ContinuousTaskRequest` main
   `MODE_AV_PLAYBACK_AND_RECORD` (12), sub
   `SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION` (7), with the app's EntryAbility
   WantAgent and normal system notification. This is actual video screen
   capture, without audio or a substitute fake keepalive.
4. `backgroundActive` is true only after the OS returns a valid continuousTaskId.
   Foreground loss before that stops capture. An active source, valid scope,
   live receiver and real background ticket allow the UIAbility to background.
5. Stop on owner stop, scope/lease loss, sink disconnect/unknown ACK, native
   terminal/error/private/paused state, or cancellation/suspension of our OS
   continuous task. Timers only bound consent/ACK and check authorization; they
   never fabricate frames. Static scenes after the first ACK have no frame-idle
   deadline and may emit no further frames.

Stop closes native capture, unsubscribes local listeners/timers, detaches the
frame route, invokes the own-HDC hook, and releases only its exact background
task ID. A late startBackgroundRunning result is released without resuming the
session. Missing/failed task cleanup is reported `cleanupUnknown`, never masked
by cancelling every task in the UIAbility; that session cannot restart silently.
Native capture timestamps are monotonic ms. `lastAckObservedAt` is a separate
wall-clock receipt time; neither is replaced with invented frame timestamps.

Root-owned manifest/package prerequisites: normal
`ohos.permission.KEEP_BACKGROUND_RUNNING`, UIAbility avPlaybackAndRecord
background mode, the native ScreenCapture/ImageKit module and its package,
and the genuine system screen-capture privacy flow. No system_basic ACL,
special-scenario permission, audio playback or LiveView entitlement is used.
Root owns the real receiver binding, owner start/stop/status UI, full SDK build,
signing/deployment and actual-device acceptance. This source alone does not
establish phone stream delivery, background residency or unattended input safety.

Official API sources: installed commercial API26 SDK
`@ohos.resourceschedule.backgroundTaskManager.d.ts` (ContinuousTaskRequest,
start/stopBackgroundRunning, cancel/suspend events; main12/sub7 supported since
API22), and [Huawei continuous task API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-resourceschedule-backgroundtaskmanager/).
The native wrapper is supplied independently as `./NativeScreenShare` with
`start(options,onState,onFrame)` / `stop()`, state/frame interfaces. No third-party
application source was copied.
