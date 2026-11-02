# Actual build delivery, 2026-11-02

Native release exists; recovery test has not executed; WASM build ran and
failed on a concrete compiler/std mismatch. No generated WASM/JS or Cloudflare
deployment exists. All code remains isolated from production entrypoints.

## Native artifact and resolved packages

Root generated release through Click's official implementation mutation:
`cargo build --release --manifest-path <absolute Cargo.toml> --config
http.proxy="http://127.0.0.1:7890"`. This corrected the initial misuse of
`verify` for a requested artifact build; it is not a verification-PASS claim.
Existing Root output was retained. Cargo's artifact metadata records Arch
rustc 1.98.1 (48a229cea, 2026-09-01), package `1:1.98.1-1`, LLVM 22.1.8.

| Artifact | Bytes | SHA256 |
| --- | ---: | --- |
| `target/release/libamita_raft_portability.rlib` | 1061802 | `35b1085e4cf7237076fd33d8578c41769daec4d07c35d2509e5c4b11590c5d77` |
| `target/release/libamita_raft_portability.so` | 414984 | `f7d9b38bd4bcc7955f66cf2276b59eec99afd305468faec80d022db7269743d3` |
| `Cargo.lock` | 11419 | `887cd1bcd6e2405c481cef688ffbc124a0705e6139ac972e011710678776206b` |

The rlib contains the Rust host interface; no public native C ABI/daemon is
provided. Build outputs stay local and ignored. The lock and `ARTIFACTS.json`
are committed. `record_inventory.py`, executed through Click mutation,
generates requested delivery metadata, not test results or validation receipts.
The fetched manifests' license declarations/checksums cover all 49 locked
packages, including this crate. Examples: raft/raft-proto/protobuf Apache-2.0;
serde and wasm-bindgen MIT OR Apache-2.0; slog MPL-2.0 OR MIT OR Apache-2.0;
zerocopy BSD-2-Clause OR Apache-2.0 OR MIT. These are package declarations.

## Actual WASM compilation and sustainable toolchain fix

The official Rust 1.98.1 manifest and wasm-bindgen 0.2.105 release metadata
were downloaded with command-local proxy settings. Actual file hashes recorded
in `ARTIFACTS.json` equal their published source values:

- Official wasm std: 22423420 bytes,
  `cf1fcf68880d8b2b90128cede7416cfb82ad3584357ce9fb668f2b6244c26e55`.
- wasm-bindgen musl archive: 8815954 bytes,
  `b391448c4926ac4b11425a6752484d85164e72489d97804461d5e868c643b88a`.

Both were extracted only under this directory's `.tools`. The exact CLI is
`.tools/wasm-bindgen-0.2.105-x86_64-unknown-linux-musl/wasm-bindgen`; its archive
contains MIT and Apache licenses. No global toolchain/proxy was modified.

A real Click mutation ran `cargo build --release --locked --target
wasm32-unknown-unknown --manifest-path <absolute Cargo.toml>` with command-local
`http.proxy`, target rustflags pointing to the extracted official std sysroot,
and target linker `/usr/bin/wasm-ld`. It fetched the WASM dependencies, invoked
rustc, and exited 101:

```text
error[E0514]: found crate `core` compiled by an incompatible version of rustc
crate `core` compiled by rustc 1.98.1 (48a229cea 2026-09-01)
help: recompile that crate using this compiler:
rustc 1.98.1 (48a229cea 2026-09-01) (Arch Linux rust 1:1.98.1-1)
```

`std` and `compiler_builtins` show the same mismatch. This is a distribution
metadata incompatibility, not a raft-rs source failure. Repeating cargo clean,
forking consensus, or patching compiler version checks is not the fix.

Use a complete matching compiler/std pair: either extract exact Arch
`rust-wasm 1:1.98.1-1` for this compiler into a private sysroot, or prepare a
fully pinned official compiler + native/wasm std under a private prefix. Pin
toolchain provenance as well as Cargo.lock, and keep the wasm-bindgen CLI
version exactly equal to the crate. Numeric version/commit matching alone is
insufficient when mixing distribution builds.

The [Arch package page](https://archlinux.org/packages/extra/x86_64/rust-wasm/)
was actually browsed. Its exact historical package is
`https://archive.archlinux.org/packages/r/rust-wasm/rust-wasm-1%3A1.98.1-1-x86_64.pkg.tar.zst`,
92588347 bytes. Archive transfer timed out after 180 seconds at 1523082 bytes;
the current official geo mirror returned 404 for that historical version. The
partial file remains ignored in `.tools/downloads`, not installed or usable.
No further large retries are running.

After obtaining a WASM release, the prepared CLI can generate `--target web
--out-dir pkg` glue. The DO host must pass a precompiled module to generated
`initSync`, as Workers does not accept arbitrary runtime WASM byte compilation.
Root still owns the separate isolated Free DO startup decision.

## Native recovery check: runner failure, not test failure

The real RawNode recovery test was submitted directly through Click verify.
Absolute workdir/manifest forms failed before Cargo started: `workdir did not
match the prepared capability`, then `context binding was malformed`. Final
plain form, actual exec cwd `/home/gihggiy`:

```text
click-gate verify -- cargo test --release --offline --manifest-path /home/gihggiy/Github/personal-assistant-mesh-worktrees/free-quorum-portability-20261101/deploy/raft-portability/Cargo.toml
```

Result: context-binding error, exit 2. No recovery test ran; there is no PASS.
No test was wrapped in Python/Node or executed under mutation. Resume this same
direct test once the verification runner binding works. Building deliverables,
fetching dependencies and generating their inventory used official mutation
and make no test-success claim.

## Required host decisions

The current SQL/Ready sample is not a complete deployable host. Before one:

1. Serialize every u64 JSON field as a decimal string, including sequence and
   applied index, or retain protobuf bytes and expose u64 as BigInt. Converting
   an already rounded JSON Number to BigInt cannot recover lost precision.
2. Bind restore to cluster/member identity and pinned three-voter ConfState;
   validate snapshot metadata, log suffix, HardState and applied together. The
   spike's `Stable` is not a state compatibility contract.
3. Atomically advance applied for empty leader/no-op entries even without a
   business mutation. Snapshot installation needs its own applied barrier;
   current awaiting-apply logic tracks only returned committed entries.
4. Serialize RawNode through Ready persistence/apply, discard memory after
   storage failure, and implement the deterministic FSM/receipt/ReadIndex
   boundary before adapting S2. Control commits cannot fence existing Shell,
   iLink, OAuth or SQLite effects by themselves.

These are host changes, not consensus algorithm rewrites. Native recovery,
actual WASM startup, Free CPU/cold start and real three-node failover remain
separate pending acceptance steps.

Official sources actually accessed:
[Rust installation](https://rust-lang.github.io/rustup/installation/other.html),
[pinned distribution manifest](https://static.rust-lang.org/dist/channel-rust-1.98.1.toml),
[wasm-bindgen CLI](https://wasm-bindgen.github.io/wasm-bindgen/reference/cli.html),
[pinned release](https://github.com/wasm-bindgen/wasm-bindgen/releases/tag/0.2.105).
