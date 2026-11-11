# Owner SelfHDC panel and bounded driver

Root integration contract (not bound by this change):

```arkts
import { SelfHdcPanel } from '../selfhdc/SelfHdcPanel';
// In the parent builder, pass the actual UIAbility foreground storage value:
SelfHdcPanel({ foreground: this.foreground })
```

`SelfHdcPanel` obtains the host `common.Context`; `foreground` defaults to false.
No initialization, appearance, network event or timer starts HDC. A local switch,
manual port entry and Connect gesture are required. The controller closes on
foreground loss/disappearance, clears local screenshot and displayed data, and
does not reconnect on resume. Disconnect closes the application connection; it
does **not** switch off system Wireless debugging or imply LAN isolation.

Protocol dependency supplied independently in the same directory:

```arkts
new SelfHdcClient(context: common.Context)
connect(port: number, budgetMs?: number): Promise<SelfHdcStatus>
shell(command: string, maxBytes?: number, timeoutMs?: number): Promise<SelfHdcShellResult>
close(): void
status(): SelfHdcStatus
// status: { state: string, error: string, authenticated: boolean }
// result: { stdout: Uint8Array, completed: boolean, dispatched: boolean, exitCode?: number }
```

Only an authenticated connection permits the following manual probes:

- `id`: complete UTF-8 identity response, <= 4096 bytes / 5 seconds.
- `uitest dumpLayout -p <our unique /data/local/tmp/*.json>`: complete JSON,
  <= 256 KiB / 15 seconds. No whole-device accessibility root is fabricated.
- `snapshot_display -f <our unique /data/local/tmp/*.jpeg>`: one actual JPEG,
  <= 1 MiB / 15 seconds, read by bounded base64 only when `base64` is present.
  The data is validated and saved in private app cache for local preview only.

The remote scripts use only generated UUID paths and fixed commands, check file
size before reading, and install an EXIT trap to remove their exact temporary
file. A daemon crash/forced process kill can leave that temporary file behind;
unknown completion does not claim successful cleanup. No wildcard cleanup,
unbounded dump, silent capture fallback, screenshot upload, new daemon,
continuous recording or execution retry is included. The screenshot is cleared
on disconnect and foreground loss; an app process crash can leave its private
cache file until normal cache cleanup. Credentials stay in the protocol layer.

Shell stream completion alone is insufficient: a verified exitCode of 0 is
required. A dispatched operation with missing completion is unknown. Failure
messages never expose raw transport exceptions or raw shell output. All returned
data remains local; stale results are discarded after lifecycle cancellation.

The finite `SelfHdcDriver.inputApproved()` API has no panel or Mesh caller.
Coordinates must fit explicit display bounds, swipe velocity is 200–40000 px/s,
text is <= 1024 UTF-8 bytes with shell quoting and no control characters, and
keys are limited to Back/Home. It requires an operation ID, live approval gate
and `humanInputObservable`. Root must bind that gate to existing original
task/operation/revision/nonce/deadline validation, durable dispatch receipts and
real human-input monitoring **before** exposing remote GUI actions. Handshake,
identity/layout/screenshot success does not establish human-input monitoring,
unattended authorization, background residency or GUI capability publication.
This stage supplies no remote arbitrary-shell endpoint.

Official references, independently implemented (no ClawMate source copied):

- [HDC and user-enabled wireless debugging, trust UI](https://developer.huawei.com/consumer/cn/doc/harmonyos-guides/hdc/)
- [UITest shell dumpLayout, uiInput coordinates/text/keys](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/uitest-guidelines/)
- [snapshot_display screenshot command](https://developer.huawei.com/consumer/cn/doc/doccenter-deveco-studio/ide-hmos-screenshot)
- [OpenHarmony daemon listens on all IPv4 interfaces](https://github.com/openharmony/developtools_hdc/blob/8efffcf2fa2f5d4f997f2de8eb287432959417c8/src/daemon/daemon_tcp.cpp#L136)

Remaining actual-device acceptance: application sandbox loopback access, system
trust prompt and independent identity, tool availability/exit status, complete
layout and actual JPEG, cancellation/disconnect on background, and commercial
system listener exposure. No device operations, HDC executions, builds, signing
or deployment were performed for this isolated delivery. Root owns compilation,
integration, existing approval wiring and any explicitly authorized acceptance.
