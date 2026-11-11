# Native phone self-HDC transport

`SelfHdcClient.ets` connects **only** from `127.0.0.1` to `127.0.0.1` at a port
entered by the owner. It implements HDC directly over `NetworkKit` TCP. The
phone's owner must enable system Wireless Debugging and approve the system's
normal client trust dialog. There is no USB bootstrap, PC daemon, external
listener, copied PC key or automatic authorization in this implementation.

```ts
const client = new SelfHdcClient(context);
const state = await client.connect(ownerEnteredPort, 90000);
if (state.authenticated) {
  const result = await client.shell('id', 4096, 10000);
  // completed && exitCode === 0 proves command completion, not its business effect.
}
client.close();
```

Exports from `SelfHdcClient.ets`:

- `SelfHdcClient(context: common.Context)`
- `connect(port: number, budgetMs = 90000): Promise<SelfHdcStatus>`
- `shell(command: string, maxBytes = 1048576, timeoutMs = 15000): Promise<SelfHdcShellResult>`
- `status(): SelfHdcStatus`, `close(): void` (both synchronous)
- `SelfHdcStatus { state: string; error: string; authenticated: boolean }`
- `SelfHdcShellResult { stdout: Uint8Array; completed: boolean; dispatched: boolean; exitCode?: number }`

States are `idle`, `connecting`, `awaiting_trust`, `ready`, `error`, `closed`.
Only `ready` allows Shell. No credentials, challenges, commands, shell output,
remote notices or private identities are logged. Error strings are fixed codes.
`stdout` is the raw HDC output stream; HDC may merge remote stderr into it.

The app generates an independent RSA3072 identity using CryptoArchitectureKit,
atomically persists it in its own `filesDir/amita-selfhdc-v1/identity.json`, and
publishes the public key only after persistence. A corrupt key is an explicit
failure; it is never silently replaced. App data removal loses this identity
and requires a new user trust decision. Concurrent clients share key loading.

Wire behavior: packed 11-byte `HW` header, big-endian lengths, protobuf unsigned
varints, protect vcode 9, 16-byte-tag/16-byte-length ASCII TLVs, RSA/PSS/SHA512
with digest-sized salt, and legacy PKCS1 raw signature when negotiated by the
daemon. It waits for successful authentication **and the corresponding channel
0 close acknowledgement** before dispatching any shell command. It does not
advertise encryption or pretend to be an attested SDK binary. Required TLS/PSK
is an explicit `selfhdc_tls_unsupported`; there is no downgrade or retry.

Each client has one session and one command in flight. Authorization is bounded
to 1–90 seconds including key generation; TCP connect is bounded to 8 seconds.
An authenticated session expires after five minutes; a new session requires an
explicit caller connection, never an automatic reconnection.
Shell timeout is 0.1–60 seconds, stdout budget 1 byte–8 MiB, command at most
32 KiB UTF-8. Packets are capped at 2 MiB, queued input at 4 MiB, control fields
and TLVs are bounded. The raw-shell protocol has no exit code, so commands run
in a subshell and append a random 128-bit completion marker. Only a complete
marker at the exact stream suffix plus channel close sets `completed=true`.
Explicit exit/exec and EXIT cleanup remain inside the subshell. Timeout,
disconnect, cancellation or output overflow returns partial bounded bytes with
`completed=false`; `dispatched=true` means the outcome is unknown and must not
be replayed automatically. Even a send error may have dispatched bytes.

This transport does not grant background residency. Callers must separately
respect OS execution windows. Closing it only closes the app's connection; it
does **not** close or reconfigure system Wireless Debugging. A loopback client
does not prove system hdcd is bound only to loopback: the public daemon binds
`0.0.0.0`. Commercial-device loopback reachability, trust, API26 negotiation,
post-reboot recovery, and commands remain **unverified on the owner's phone**.
No HDC/device operations or full application build were performed for this change.

## Protocol provenance and verification

This implementation was independently written from the Apache-2.0 official
[OpenHarmony developtools_hdc source](https://github.com/openharmony/developtools_hdc/tree/8efffcf2fa2f5d4f997f2de8eb287432959417c8),
not from the unlicensed ClawMate implementation. Reference files:
`src/common/session.h`, `session.cpp`, `serial_struct.h`,
`serial_struct_define.h`, `define_enum.h`, `base.cpp`, `auth.cpp`,
`src/host/server.cpp`, `src/daemon/daemon.cpp`.
Reference copyright: Copyright (c) 2021 Huawei Device Co., Ltd.; licensed under
the [Apache License, Version 2.0](https://github.com/openharmony/developtools_hdc/blob/8efffcf2fa2f5d4f997f2de8eb287432959417c8/LICENSE).
No official implementation source was copied; wire constants and message
layouts are reimplemented here with source attribution.

Focused regression uses literal schema-derived wire fixtures, fragmented and
coalesced packets, malformed lengths/protect fields, and an actual local POSIX
shell to verify binary stdout, explicit exit and EXIT trap handling. It never
contacts a phone or network and is not a simulated device acceptance.

```sh
node --test apps/harmonyos/entry/src/main/ets/selfhdc/protocol.test.mjs
```

As elsewhere in this repository, the `.ts` import uses Node's built-in TypeScript
support (Node 22.18+ or newer). No test framework dependency is added.
