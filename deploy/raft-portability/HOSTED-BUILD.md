# Public standard-runner build slice, continued 2026-11-11

Prepared source only: Root owns review, public push, hosted execution and result
collection. No workflow has been pushed or run by this specialist. Native
Click runner errors and the earlier actual WASM E0514 remain recorded in
[BUILD-STATUS.md](BUILD-STATUS.md); this workflow does not convert them to PASS.
The actual GitHub run-list query for this original branch on 2026-11-11 returned
no runs. The continuation keeps the original Rust core, toolchain pins and native
case, and adds actual generated-WASM execution plus a runnable public entry.

## Official zero-new-fee basis

GitHub's current [billing and usage documentation](https://docs.github.com/en/actions/concepts/billing-and-usage)
states standard GitHub-hosted runner usage in public repositories is free.
The [current runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
explicitly lists `ubuntu-24.04` as a standard public runner. The
[billing reference](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
distinguishes it from larger runners, which are billed even for public repos.
These official sources were actually browsed on 2026-11-02. Account Actions
availability was not inspected; no registration, paid fallback or billing
configuration is requested or added.

The job is guarded by both `repository.private == false` and the exact original
research ref. It uses only `ubuntu-24.04`, `contents: read`, a 12-minute timeout,
one job with superseded runs cancelled, no cache, and artifact retention of two
days. Artifacts are capped at 20 MiB before upload. This is a small public source
build, not ongoing free computation unrelated to the repository.

## Fixed actions and official toolchain

Only official actions are used, pinned to their actual release commit URLs:

- [checkout v7.0.1](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)
  (`3d3c42e5aac5ba805825da76410c181273ba90b1`).
- [upload-artifact v7.0.2](https://github.com/actions/upload-artifact/commit/cf430e030ddbb5b0abf93d22962f4752f3646cd9)
  (`cf430e030ddbb5b0abf93d22962f4752f3646cd9`).

Their pinned action manifests were browsed; artifact retention/hidden-file
controls are supported by the pinned upload action. Checkout does not persist
credentials and sparsely fetches only this public Rust directory and LICENSE.

The hosted runner installs official Rust 1.98.1 with native +
`wasm32-unknown-unknown` std through rustup's official distribution server.
Every Cargo command explicitly uses `rustup run 1.98.1`; every build uses the
committed Cargo.lock with `--locked`. This pairs compiler and std provenance,
without changing or impersonating the local Arch compiler.

The exact existing `tests::durable_vote_survives_reconstruction` test runs once
on hosted Ubuntu with `--lib ... -- --exact`. Its captured output must also
confirm exactly one case ran and that exact case passed, avoiding a silent
zero-test match after renaming. Only then does a marker allow packaging. No
additional test framework or quorum test is
introduced. The workflow then builds WASM and uses the official wasm-bindgen
0.2.105 musl binary, checking the fixed release SHA256 already recorded in
ARTIFACTS.json, to generate `--target web` JS and typings.

Before packaging, `wasm-smoke.mjs` loads that generated JS, initializes its actual
WASM through `initSync({module: new WebAssembly.Module(...)})`, and repeats the
original native vote recovery case through exported `Voter`. Its small protobuf
fixtures use the pinned raft-proto 0.7.0 field/enum values. It writes and syncs
the vote, reconstructs the WASM voter and requires rejection of a second
candidate in the same term. It also requires Ready to withhold messages until
the matching persisted receipt. This is real WASM execution using the upstream
algorithm, not a mock consensus service. Its generated JSON receipt records the
exact executed WASM hash. No transport, credential, background loop or deployment
is part of this bounded test.

## Upload contract and evidence scope

`hosted_bundle.py` creates a new output directory under runner temp and copies
only explicit native library/WASM/JS/typing names, Cargo.lock, public dependency
licenses, Rust's COPYRIGHT/MIT/Apache files pinned to its compiler source commit,
and a generated `HOSTED-MANIFEST.json`. The explicit allowlist additionally copies
`wasm/wasm-smoke.mjs`, `wasm/package.json` and `WASM-RECOVERY.json`. The bundler
requires the successful WASM receipt's hash to match the actual generated module
before creating its output directory. No workspace directory,
target tree, Cargo registry, environment dump, private config, token, history,
HAP or personal file is uploaded. License-file paths cannot escape their public
crate directory. The manifest records source commit/ref/run URL, runner image,
official compiler/Cargo/installed targets, file hashes and package licenses.

Successful hosted logs + the matching manifest may establish this one native
test, generated-WASM vote recovery and the hosted compiler outputs. They do not
establish a local Click test receipt, Free DO cold start/CPU, three-voter consensus,
fencing of business effects, or production failover. Those boundaries remain.

## Trigger and Root continuation

Push trigger is only `research/free-quorum-portability-20261101`, and only Rust
`src/**`, Cargo.toml/Cargo.lock, this workflow, its packaging helper or the two
WASM runner source files change.
Documentation-only pushes do not run it. Existing `tests.yml` ignores push only
on this exact branch so pure Rust research does not run unrelated Python/chat
matrices; PR triggers, other branches and Python check definitions remain as
before. Restore the relevant checks if Python business changes enter this branch.

Root can review and push the original branch through its existing authorized
remote:

```text
git push origin research/free-quorum-portability-20261101
```

On the laptop prepend the command-local external proxy variables required by
the owner; do not set a repository/global proxy. After pushing, Root can find
the exact new run (no dispatch or second run is needed when the push triggered):

```sh
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890 gh run list --repo gih10012/personal-assistant-mesh --branch research/free-quorum-portability-20261101 --workflow free-raft-portability.yml --limit 5
```

That first source push is the expected trigger. GitHub's official
[workflow_dispatch rule](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
requires the workflow file to exist on the default branch before manual dispatch
is available. Keeping workflow_dispatch here does not bypass that rule; do not
create a fake source change merely to claim manual activation. Root can inspect
the real run for this exact branch/commit and download
`amita-raft-portability-<commit SHA>` within its two-day retention period.
If the workflow has already been installed on the default branch and a manual
run is needed instead of an existing run, the exact command is:

```sh
HTTP_PROXY=http://127.0.0.1:7890 HTTPS_PROXY=http://127.0.0.1:7890 http_proxy=http://127.0.0.1:7890 https_proxy=http://127.0.0.1:7890 gh workflow run free-raft-portability.yml --repo gih10012/personal-assistant-mesh --ref research/free-quorum-portability-20261101
```

The CI output directory is `$RUNNER_TEMP/amita-raft-public-bundle`. After Root
downloads the named artifact into a new private directory, its runnable entry
is `node /ABSOLUTE/DOWNLOADED-ARTIFACT/wasm/wasm-smoke.mjs`. Run it through the
normal Click check boundary. It needs only Node and the public generated bundle;
it performs no network access and creates/removes only a temporary vote file.
`WASM-RECOVERY.json` and `HOSTED-MANIFEST.json` retain the original hosted run
identity. Running the entry locally prints a new local receipt without changing
the original hosted receipt. There is no runnable WASM artifact until the actual
hosted build has succeeded; this source continuation is not that success.

The runner downloads public tools directly. The owner's laptop-local proxy is
not forwarded or configured on GitHub-hosted machines.
