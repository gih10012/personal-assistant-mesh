# Bounded raft-rs portability spike

**Current 2026-11-02 status:** native release and Cargo.lock exist. Recovery
test has not executed because of Click verify runner context binding. Actual
WASM compilation found an Arch compiler/official std metadata mismatch; WASM
and generated JS do not yet exist. See [BUILD-STATUS.md](BUILD-STATUS.md) and
the generated [artifact/package inventory](ARTIFACTS.json) for current evidence
and the exact continuation. Nothing here is deployed or a control service.

## Historical 2026-11-01 checkpoint

This directory is isolated from all production Python and Workers entrypoints.
It is an uncompiled checkpoint because Click rejected the actual Cargo build
argv before execution. No artifact, Cargo.lock, WASM startup or recovery test
receipt exists. Do not integrate it as an available control service.

`Core` uses the upstream `raft =0.7.0` algorithm, static voters 1/2/3 and bounded
messages. It exposes a Ready storage batch while withholding outbound messages;
the host acknowledges durable storage before `persisted()` releases them. The
WASM wrapper and native recovery test call the same code. The test prepares a
real RequestVote, saves HardState through a synced file, reconstructs RawNode,
and asks a second candidate for a vote in the same term. This is a targeted
native-host recovery check, not a three-node deployment or a SQLite DO test.

`do-host-boundary.mjs` sketches the required synchronous SQL transaction and
async durable barrier. It does not implement database migrations, restore,
transport, tick scheduling, FSM, read API, credentials or bootstrap. These are
not replaced by a fake CAS witness. The Rust `Stable` format is internal to
this spike, not an adjacent-version compatibility contract. u64 JSON roundtrip
above JavaScript's safe integer range remains a required schema decision before
shipping, as do input snapshots/ConfState validation and explicit empty-entry
apply handling. This draft must not be presented as deployment-ready.

Pinned direct dependencies: raft 0.7.0, protobuf 2.28.0, serde 1.0.228,
serde_json 1.0.149, getrandom 0.2.16, slog 2.8.2, wasm-bindgen 0.2.105.
The raft 0.7.0 / raft-proto 0.7.0 upstream manifests were browsed and declare
Apache-2.0. The other exact crates and transitive dependencies have not been
resolved or their license metadata fetched; do not report a complete license
inventory before producing Cargo.lock and inspecting those actual packages.
The WASM target uses getrandom's 0.2 `js` feature; no private core fork.

Pending exact checks (absolute workdir is this directory):

```json
{"version":2,"workdir":"/home/gihggiy/Github/personal-assistant-mesh-worktrees/free-quorum-portability-20261101/deploy/raft-portability","checks":[{"evidence_id":"free-raft-native-release","argv":["cargo","build","--release"],"class":"targeted"},{"evidence_id":"free-raft-native-recovery","argv":["cargo","test","--release"],"class":"targeted"},{"evidence_id":"free-raft-wasm-release","argv":["cargo","build","--release","--target","wasm32-unknown-unknown"],"class":"targeted"}]}
```

On 2026-11-01 Click rejected check 1 as `neither read-only nor a recognized
check`. None of the batch ran. An earlier invocation with command-local proxy
variables was not rewritten by the host and reported `click-gate: command not
found`; Cargo also never ran. `rustc --version` was separately rejected and
was not rerun through another wrapper. Stop until the host exposes the actual
Cargo capabilities. Do not use a Python/Node wrapper to evade this rejection.
External dependency downloads must use command-local localhost:7890 proxy,
with a host-supported Evidence environment binding; do not set global proxy.

Once allowed, the required deliverable is native release + WASM release,
matching wasm-bindgen-generated JS, actual size/hash, and the recovery receipt.
Then Root can decide the isolated Free DO startup. No build here counts as a
Cloudflare deployment or as actual quorum/failover acceptance.
