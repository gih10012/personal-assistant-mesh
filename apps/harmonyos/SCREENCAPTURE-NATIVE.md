# Native video-only screen sharing

`NativeScreenShare` in `entry/src/main/ets/screenshare/NativeScreenShare.ets` wraps
`libamita_screenshare.so`. It captures real screen buffers through the public
`OH_AVScreenCapture` C API and delivers bounded JPEGs in memory. It does not
record to disk, log images, open network connections, inject input, start a
daemon, manipulate signing, or request privacy-dialog exemptions.

## API shared with the session controller

```ts
const capture = new NativeScreenShare();
capture.start({ maxWidth: 640, maxHeight: 640, maxBytes: 42000 }, onState, onFrame);
// Later, including on owner stop/revocation or sink disconnect:
capture.stop();
```

`start` and `stop` return `void`. Callers must install their actual authenticated
frame consumer before `start`. `onState` receives `{ state, errorCode }`; only
`started` means the system's `OH_SCREEN_CAPTURE_STATE_STARTED` callback fired.
`start_requested` is not authorization or capture success. Other states are
`stopped`, `canceled`, `interrupted`, `error`, `private_scene`,
`private_scene_exit`, `paused`, and `resumed`. Capture/error codes are preserved;
normal transitions use zero. The controller must end its session on terminal
states and must not qualify background activity while paused/private.

Frames are `{ bytes: Uint8Array, timestamp, width, height, revision }`.
`timestamp` is the actual capture nanosecond timestamp divided by 1e6, i.e.
source monotonic milliseconds. It must not be compared with `Date.now()`;
record wall-clock receipt time separately. Revision increases only when an
actual buffer is copied; dropped revisions are expected under backpressure.
Dimensions describe the delivered JPEG, preserve aspect ratio, and never
exceed the requested bounds or 640 pixels. The JPEG is at most `maxBytes`
(hard limit 42000); encoding uses finite quality/size attempts and fails
explicitly if no valid JPEG fits. No placeholder/stale frame is generated.

Native callbacks copy/downsample before returning. The `OH_AVBuffer` is never
retained outside its callback. The separately referenced `OH_NativeBuffer` is
mapped, unmapped and unreferenced on every path. JPEG encoding uses public
ImageKit Pixelmap/ImagePacker APIs; their options, pixelmaps and packers are
released on every path. One worker encodes at a time, with exactly one pending
copied frame (latest wins). The JS delivery bridge also keeps one latest JPEG
and one thread-safe notification; it does not accumulate a frame queue.

Supported source buffers are RGBA8888/RGBX8888, BGRA8888/BGRX8888, and NV12/NV21
with valid public `MapPlanes` metadata. Width/height, strides, per-plane offsets
and accesses are checked against the positive capacity reported by AVBuffer,
with a 128 MiB input ceiling and 8192 dimension ceiling. Unsupported pixel
formats, inaccessible mappings, invalid/absent capacity or invalid planes
produce an error instead of an invented image. NV12/NV21 uses BT.601/BT.709
and full/limited range metadata; missing color-space metadata defaults to
BT.601 limited range. HDR/P010 and unsupported declared YUV color spaces are
rejected. This module is not a color-managed HDR-to-SDR renderer.

Capture requests the real default display size and RGBA source. Both audio
source configurations are zero (ignored), with the microphone explicitly
disabled. The state callback is registered before capture start, which enables
the normal system privacy-dialog flow. No `CAPTURE_SCREEN`,
`CUSTOM_SCREEN_RECORDING` or `EXEMPT_CAPTURE_SCREEN_AUTHORIZE` workaround is
used. The worker requests at most 5 FPS and copies at most one real frame per
200 ms; a static display naturally produces no new frames. No artificial
animation, periodic dummy image or idle data is sent.

Stop invalidates wrapper generations immediately, drops pending frames, waits
for the worker, stops and releases AVScreenCapture, then releases both NAPI
thread-safe callbacks. Late callbacks cannot enter a restarted wrapper session.
Native error/cancel callbacks signal worker cleanup, avoiding release from
inside the platform's own capture callback. Setup completion is synchronized
before native cleanup so asynchronous startup errors cannot free an object
still being initialized. Environment cleanup also stops capture.

If native Stop or Release returns a failure, `stop()` throws a fixed-message
error with the native code so the session controller can record cleanup as
unknown. A failed Release quarantines its callback userdata and blocks new
capture instead of freeing memory that the platform may still reference. At
environment teardown, unreleased userdata is intentionally left to process
cleanup; that condition is not reported as a clean stop. Wrapper instances
share an ownership fence so a second instance cannot stop another capture.

## Integration owned by Root

Root must add `buildOption.externalNativeOptions` pointing to
`./src/main/cpp/CMakeLists.txt` in the entry build profile and the file dependency
`"libamita_screenshare.so": "file:./src/main/cpp/types/libamita_screenshare"` to
the entry package. No common package/profile/manifest/EntryAbility was edited
in this change. The native target links only public SDK libraries.

The actual session controller separately owns normal `KEEP_BACKGROUND_RUNNING`
and `INTERNET`, `backgroundModes: ["avPlaybackAndRecord"]`, real user consent,
and `MODE_AV_PLAYBACK_AND_RECORD` + `SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION`.
This module does not start that task itself and does not turn an unused capture
into background residency. Cross-application input remains a separate selfHDC
capability. SelfHDC does not survive the owner closing Wireless Debugging just
because capture is running.

Custom error codes: `-1001` invalid options, `-1002` display information,
`-1003` source format/color space, `-1004` mapping, `-1005` buffer bounds,
`-1006` encoding, `-1007` JPEG budget, `-1008` callback delivery, `-1009`
allocation, `-1010` another native session is still active. Caller exceptions
use fixed messages, never image/credential contents.

## Evidence boundary and sources

Actual declarations were read from the commercial API26 SDK's public headers
for MediaKit, NativeBuffer, NativeDisplayManager and ImageKit. Implementation
is original; it does not copy unlicensed projects. Public reference guidance:

- [AVScreenCapture basic process](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/avscreencapture-c-basic-process/)
- [AVScreenCapture C API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/capi-native-avscreen-capture-h/)
- [NativeBuffer C API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/capi-native-buffer-h/)
- [ImagePacker C API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/capi-image-packer-native-h/)
- [Background task API](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-resourceschedule-backgroundtaskmanager/)

No build, test, device, desktop, signing or production operation was performed
for this change. Root owns the full commercial SDK build and one real phone
acceptance: consent dialog, changed frames from another app while AMITA is in
the background, authenticated consumer acknowledgement, and immediate stop.
API/header availability is not a device success receipt.
