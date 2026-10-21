# Existing Chat final / Work Events deployment handoff

Owner-approved implementation leaf, base `f9e270f`, branch
`work/entrance-finish-20261021`. Reuses original final `629a1c2+a526f8f`,
Events `e342fa3+906be49`, and current-grant factory `395c94a+364eab2`.
The latter two are preserved as original cherry-picks, not newly invented OAuth.
Root owns production writes. School evaluation stays paused and out of this leaf.

## What changed, and what did not

- The old factory assembly never enabled the reviewed final-message read
  projection. The updated SAME-app template adds explicit owner-code
  `final_projection=True`, or `MESH_CONVERSATION_FINAL_PROJECTION=1` at its
  existing entrypoint. Default is off. It reuses the same backend/service/bridge,
  so receive can read durable agent messages without reading raw task results,
  rerunning work, draining replies, or requiring owner approval for every final.
- `conversation-final-ready-fragment.py.example` composes the EXISTING ready
  hook and checks the original worker has its exact final capability before
  attaching anything. It never adds a grant. It keeps original IPC/Claw setup,
  then attaches authority -> emits -> final -> optional private event feed on
  that SAME already-open API/Store. Project policies must be explicit subsets
  of the original reply audience. Input mappings are snapshotted at composition.
- CurrentOAuthGrants is the original source-derived checker, not a static
  token-presence or registered-client test. Independent source review and the
  actual pinned component tests cover real exchange/rotation/revoke hooks and
  authenticated adoption of an already-connected no-refresh grant. No concrete
  report for a remembered old finding count was available; this leaf does NOT
  claim an unspecified historical `1P1+2P2` set has been fixed.
- Module-level grant tests no longer accidentally collect the imported OAuth
  fixture class and rerun its unrelated tests. No original auth assertion was
  weakened. The old draft also imported optional JWT/Starlette unconditionally,
  breaking dependency-free core discovery when those sidecar packages are absent;
  it now has explicit optional-fixture guards, not a new core dependency. No
  final/feed/Events core, frozen Worker, Store patch or native
  model behavior changed in this continuation.

## Root's shortest controlled installation

1. Reuse the existing source-only frozen candidate and builder, not a new modern
   receiver. `deploy/entrance-finish-source.sha256` pins the 21 relevant source
   members; it is a MEMBER manifest, not a full image/runtime manifest. Verify
   it with `sha256sum -c` from the exact source root. The unchanged final builder
   still verifies its full actual base manifest, all ten pure-module hashes and
   the exact patch. Historical prepared full manifests were base
   `aff7d6e64d3092208a7683cd4db1ffa6259b555890732245701780e6360132df`
   and candidate
   `0f0c29c56a879f11a4666063fbeea70f2804a24e422ef11d6045c8add11f3b07`.
   They are NOT a claim about a launcher/config edited later: record the actual
   final private launcher hash and installed source manifest separately.
2. Keep the unchanged original worker/receiver identities and normal backups.
   Add `owner.conversation.final` ONLY to the existing original Leader worker
   peer; keep its existing node and capabilities. Install a bounded reply
   policy for the existing `chatgpt-chat` subject/node, not a wildcard, operator,
   Cloud or unverified new caller. Keep original `sources` exactly. Compose the
   existing private ready callable using the new pure template and explicit
   owner-approved project map, retaining `serve_entrance`'s passive constructor
   (`recover_inflight=False`). Do not launch a second receiver or poller.
3. Update only the existing plugin's active OAuth sidecar assembly, keeping its
   App, issuer, keys, encrypted OAuth state, client binding, eight tools,
   conversation directory, ingress credential reference and port. Enable final
   projection explicitly. For Work, additionally provide the owner-private
   `MESH_WORK_EVENTS_CONFIG` from the current-grant handoff: owned0700 directory,
   exact same source/subject/project map as the private receiver feed. The SAME
   original provider gets capture/revoke hooks BEFORE startup; the Events
   lifespan wraps it outside, closing the worker before detaching its auth loop.
   Run only ONE Events dispatcher. Keep a fallback IP-sidecar's Events disabled;
   this is not a multi-writer OAuth deployment or a second account/App install.
4. Unit-tested work may deploy now under Root's serialized operations. Leave
   `/v1/conversation/*` and `/v1/ingress/conversation/events` private. No new
   public nginx path, bearer export, model key, real task or OAuth login is
   required. A fresh authenticated request can adopt the existing no-refresh
   connection; an unknown old refresh grant waits for its NORMAL refresh hook,
   not a guessed JTI association or another login requested merely to test.
5. Mark separately: source merged, installed, final/projection enabled, Work
   dispatcher enabled, platform acceptance queued. Do not resubmit the old
   diagnostic or claim UI/model continuation from HTTP2xx, tools/discovery,
   a durable final, or a running PID. New ordinary Chat implicit use, original
   Chat follow-up, Work subscribe/continuation/cancel go to the Leader's weekly
   acceptance queue with the exact installed versions and original identities.

Frozen core pins remain those in the unchanged builder:

| Member | Expected SHA256 |
| --- | --- |
| Input Store | `081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08` |
| Output Store (five-line opt-in hook) | `be2df48e03021f48c382efc4690fa7a8ee5e67da8bd93497e725566532e30c0b` |
| server | `cc5bb8b492d7e03f68a5f8720eb1399d9ab4ac8e303f46919713c0a6592fd19c` |
| worker | `816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0` |
| codex | `b32e2ba28c2c06ce1b1208a023de01ddd56081c012d9b5723e1158449bff1f98` |
| sessions | `37f5b647c5ddb11018907311bef1e60e512b0d5259d78ca188b3f93e3ecb33ec` |
| channel | `ed95ff4cfb33f385edc3f319085532adf543f581cdf8303864f6ac1bfc8c5261` |

The pure receiver closure remains the original ten modules, with no modern SDK
or Events network module imported by legacy3.6. Modern sidecar pins remain
MCP2.3.0 / FastMCP-slim4.1.0. Neither source parsing nor the local3.14 checks
below claim an actual production3.6/3.12 execution or deployment.

## Failure / recovery boundaries

- Final text and task completion commit in one original transaction. Before an
  unconfirmed completion, preserve original task/epoch/effect guard and review
  the SAME case. After commit, recover only the original receipt/projection;
  never repeat native work to regain a message.
- Feed cursor advances only after original refs are durable. Feed outages keep
  the cursor and permit delivery of already durable entries. Missing project
  is not guessed. No source sync or merge grants business execution rights.
- Events stays finite (default1h, maximum1day); current account/project access
  is rechecked. Local revoke blocks old JWT adoption durably. Upstream identity
  verification has at most30s positive-cache freshness. Account-check outages
  fail closed and may cancel pending delivery; restoring connectivity alone is
  NOT proof of reactivation: normal subscription refresh/re-subscribe is needed.
- Current GitHub factory has no public RFC7009 endpoint. Do not claim the
  ChatGPT Disconnect UI necessarily sends a revoke signal. Observable boundaries
  are unsubscribe, TTL/410, upstream revocation, and owner-local revoke. If
  local revoke metadata is unconfirmed, its durable pending intent keeps Events
  fail-closed across restart/capture/refresh. Owner-local same-subject block
  reconciles only metadata, never repeats the original provider revoke. If even
  the BEFORE-effect intent cannot persist, the provider is not called and the
  caller receives the explicit `intent_unconfirmed_not_attempted` failure.
- Roll back only owned source/config/lifespan, retaining all new tables,
  journals, refs and histories. Never restore a stale DB over the active one.
  Once new reply policies admitted tasks, arrange their original completion or
  handoff before removing the final hook; don't silently break pending replies.

## Bounded local evidence, not platform acceptance

Original grant module's first run accidentally included its imported fixture:
15 tests passed /1.657s, zero skip. All HTTP used synthetic ASGI/MockTransport,
not real accounts/network/models. The collection leak is now fixed. Four NEW
checks passed /0.218s, zero skip: default-off/shared context/projection, exact
original worker/ready ordering, audience/project refusal, and actual pinned
OAuth middleware capture of the existing no-refresh connection plus persistent
Events-only revocation. First new-fixture run had two setup errors (multi-subject
fixture and missing refresh key in a legitimate no-refresh response); fixed
the fixture only, not runtime checks. No old final/full-suite/model trial ran.
The three assembly checks also passed under the dependency-free system Python
/0.004s. At that checkpoint plain-core discovery loaded four optional grant checks
without JWT/Starlette/FastMCP; those environments are NOT claimed grant-verified.

Follow-up independent review reproduced a concrete P2: old no-refresh Events
access could return after local revoke metadata failed and the process restarted.
This continuation adds durable per-subject revoke intents and all authorization
fences above. The ONE new offline regression passed /0.009s: same failure seam,
one original provider call, rebuilt instance still denies, old JWT capture and
cache fast path cannot clear pending, owner-local reconciliation does not replay
the provider, later explicit new grant can work, and failed intent admission
calls the provider zero times. This is not a new account/model/platform trial;
independent re-review is pending until separately recorded by Root.

The current [official MCP Events contract](https://developers.openai.com/plugins/build/mcp-events)
was fetched in full. Work web / desktop Cloud / dots subscriptions are distinct
from ordinary Chat's native follow-up. Signed webhook2xx only acknowledges
receipt, not a resumed Chat/model or owner acceptance. Advanced-development
quota exhaustion retains this branch/checkpoint and waits; no free takeover.
