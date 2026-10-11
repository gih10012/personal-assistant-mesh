# Anonymous stock OpenCode compatibility fixture

Observed on 2026-10-11, 19:00 China time. Distinct authorized fixture:
`PAM-010d-opencode-stock-free-compat-20261011-v1`.

## Outcome

**Succeeded:** the existing genuine OpenCode 1.18.29 binary, using its native
`build` agent and actual built-in tools, anonymously executed a public scratch
task through `opencode/step-5-preview-free`. It read two factors, created and
verified an exact output file, then continued the **same original native
session** with a read-only verification. No login, new credential, paid model,
fallback, installation, client-identity substitution or protocol emulation was
used. This is a bounded external-capability test, not an OpenCode Leader.

The earlier custom-agent/read-edit-only fixture remains an independent
`FreeTierError` failure, with its original session and absent artifact intact;
see [the earlier trial](ANONYMOUS-FREE-EXECUTION-TRIAL.md). That failure does not
establish that the genuine client's anonymous mode is generally unavailable.

## Zero-model preflight

- Existing `/usr/bin/opencode`: version `1.18.29`; SHA-256
  `0fb0f7db3cf8dc69543692bb328491fef294f8a129a2dc92eea75c313d3eaec1`.
  No replacement or upgrade; this hash identifies the observed local binary,
  not an independent release-signature verification.
- Existing bubblewrap 0.12.0 namespaces passed an actual read-only probe. The
  child's original `HOME` and, if present, `CODEX_HOME` environment values were
  preserved, not redirected. The real home pathname was empty inside that
  namespace; personal OpenCode auth/plugins, `.codex`, and the old trial were
  invisible. Host mounts and personal files were not modified.
- A new private prefix contained a real empty Git worktree and separate
  XDG data/config/cache/state/tmp. Original auth was neither copied nor mounted;
  isolated `data/opencode/auth.json` was absent before and after execution.
- Native `version`, `debug paths`, `debug config`, `debug agent build`, and
  `models opencode --verbose` all exited 0 without requesting a model turn.
- `build` reported `native: true`, its native permission rules and all 13 real
  tools enabled: `invalid`, `question`, `bash`, `read`, `glob`, `grep`, `edit`,
  `write`, `task`, `webfetch`, `todowrite`, `websearch`, `skill`. No custom prompt,
  replacement tool schema, agent rename or permission deny was added. Built-in
  policy rules and the configured step limit still apply normally.
- The complete, unmodified public catalog from the earlier successful metadata
  GET was reused, SHA-256
  `dc800728d62b322f8bcce55fa8bfe76a390dda91fdf51919e006a0cfc5f7cebb`.
  Native catalog output listed only `opencode/step-5-preview-free` with active
  status and input/output/cache-read/cache-write prices all 0. Its physical-model
  reference is `stepfun/step-5-preview`; no quality benchmark was run here.

The fixture configuration selected native `build`, with `steps: 3`, the sole
free provider-model whitelist, and the same free `small_model`. Title, summary
and compaction agents were disabled to prevent auxiliary model work; automatic
compaction, sharing and upgrades were disabled. `--pure` prevented external
plugins. Other native build tools and the default build prompt were retained.
No provider headers, user agent or underlying genuine-client code were changed.
These are **temporary fixture settings**, not restrictions on Mesh nodes or
their native shell, filesystem, network or MCP features. Native CLI/config
behavior is described in the [official CLI documentation](https://opencode.ai/docs/cli/)
and [configuration documentation](https://opencode.ai/docs/config/).

## Actual execution and continuity

An owned loopback native server was started. Its native SSE event observer was
established **before** the first prompt. The first prompt requested work only
on the new public `factors.txt` (6 and 9) and `product.txt`; ordinary native tools
were available, with no delegation or network work requested.

| Observed item | Actual result |
| --- | --- |
| Main native turn | 3 assistant steps; completed `read`, then `bash` |
| Main shell action | Read factors, multiply, write output, and read exact bytes with `od -c`; exit 0 |
| Artifact | Exactly `PRODUCT=54` plus one newline; 11 bytes |
| Same-session follow-up | 2 assistant steps; completed `read` and read-only `bash` (`od -c product.txt`); exit 0 |
| Follow-up response | Included `VERIFIED_54`; no additional write tool/action |
| Full native messages | 4 after main turn, 7 after follow-up; every original message object preserved |
| Models actually recorded | All 5 assistant messages use `opencode/step-5-preview-free` |
| Errors/retries | 0 session errors; 0 observed retry events |
| Native reported model cost | 0 across all recorded assistant messages |
| Runtime/cleanup | 27.648 seconds; owned server terminated and reaped; PID absence checked |

Native `export` of this original session exited 0 after cleanup. Its complete
seven messages exactly matched the final native API messages after canonical
JSON ordering, SHA-256
`4c16ce1d6d2efee769e8ee87fe84bbb39b22bf5382a5233a9374a2d744e9c7c4`.
The artifact remained unchanged after follow-up, SHA-256
`c1dce1e288deb3a6650dce0248ad6bdd09cb5fe346b35368eaf1fd4a10bde299`.
No historical messages were imported, rewritten, compacted, forked or replayed.

## Bounds and remaining unknowns

- Controller intent allowed exactly one new main user turn, and one follow-up
  only after success. Per-turn native step limit was 3, output setting 4096, and
  whole execution/cleanup deadline 180 seconds. The durable intent prevents a
  second execution of this fixture. An unknown/timeout would retain the same
  session and abort owned processes, not resubmit under a new identity.
- Session-layer retry is separate from SDK/HTTP retry. The observer was set to
  abort the original session at its first retry event. Zero retries were
  observed, but this is not a universal guarantee of zero hidden transport
  retries. **HTTP model POST count was not independently instrumented and
  remains unknown.** Two user turns and five assistant steps are not an HTTP
  request counter.
- The existing loopback HTTP proxy was made available to this child. Its
  downstream route was not independently proven: this test does **not** prove
  VPS egress, Mesh routing or failover.
- Catalog zero prices plus genuine native cost-0 records establish this route's
  observed no-login/free execution, not a billing audit, permanent availability,
  identical quality to an AA benchmark, or unlimited quota. Reasoning effort
  was not explicitly selected or independently benchmarked.
- Namespace isolation is specific to this Linux fixture. It is not a new
  host-wide allowlist or a portable Mesh deployment prerequisite.
- A small deterministic artifact and same-session continuity are verified;
  production enrollment, robust provider adaptation, leader migration, broad
  arbitrary-model reliability, decentralised HA and autonomous projects are
  not established by this fixture. No production adapter or registry was
  activated by this test.

## Comparison with the earlier refusal

Current upstream reports associate some genuine-client free-tier refusals with
custom agent/tool permission footprints, including disabled native tools. This
is a candidate explanation, not an independently confirmed cause; see
[issue 51315](https://github.com/anomalyco/opencode/issues/51315) and
[issue 53347](https://github.com/anomalyco/opencode/issues/53347). The latter
contains updated observations rather than its original identity-header guess.
Other reports still describe refusals with broad permissions, so stock success
does not promise universal compatibility; see
[issue 52907](https://github.com/anomalyco/opencode/issues/52907).

This successful test changed agent/tool footprint **and** workspace isolation
and operational configuration compared with the first failure; it is not a
single-variable causal experiment. It supports preserving the genuine native
contract for this temporary external capability instead of treating tool
cropping or client spoofing as an interoperability requirement. The pinned
[native request preparation source](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/llm/request.ts)
defines legitimate client/session identity headers; actual wire headers were
not captured here and none were supplied as replacements.

Private controller, intent, namespace receipts, full SSE events, native messages,
terminal output and original export stay outside this public repository. Their
exact local paths and hashes were handed to the Leader for independent review;
this document contains no credentials, private history or native session IDs.
