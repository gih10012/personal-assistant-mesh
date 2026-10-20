# Native agent lifecycle entry

This owner-local thin adapter gives an existing Mesh node a simple native
agent-control entry. It does not make every process a new node/member, replace
the continuous Leader, install another network service, or resume paused work.
It reuses the existing `codex-leader-control` spawn/resume/status/stop pattern,
adding explicit reasoning selection, durable request IDs and owned cancellation.

## One interface

Run from the installed frozen source, or expose this module behind a local
`mesh agent` wrapper. The wrapper is not installed by this source-only leaf.

```sh
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json spawn \
  --request-id original-spawn-id --agent specialist-one --cwd /WORKSPACE \
  --model MODEL_FROM_THIS_NODE_CATALOG --effort SUPPORTED_EFFORT --prompt-file /PRIVATE/task.txt
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json status --agent specialist-one
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json configure \
  --request-id original-config-id --agent specialist-one --effort SUPPORTED_EFFORT
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json resume \
  --request-id new-input-id --agent specialist-one --prompt-file /PRIVATE/followup.txt
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json stop --agent specialist-one
python3 -m assistant_mesh.agent_manager --config /PRIVATE/agents-owner.json status --request-id original-spawn-id
```

`status` without an agent lists the local managed and explicitly bound external
agents. Prompts may alternatively arrive on stdin (`--prompt-file -`). Paths,
auth references and runtime selection come only from the private owner config,
not executable text supplied as agent arguments.

Every launch first validates the exact model/effort against this native
binary's zero-model `model/list`. Unsupported values fail before a turn; there
is no permanent effort/model whitelist or silent downgrade. `configure` writes
a revision for the **next explicit turn**. A running turn is unchanged. Native
autonomous Goal continuations are not secretly interrupted or restarted.

The recorded CLI selection and catalog receipt are not proof of inference
settings. `effective_verified` remains false. Real acceptance compares the
original native `turn_context` model/effort/collaboration settings, using
`reasoning.observed_turn`; an agent's textual self-report is insufficient.

## Private binding

Use an owner-only 0600 JSON config outside Git, with 0700 state/runtime paths:

```json
{
  "format": "mesh-agent-manager/v1",
  "state_dir": "/PRIVATE/mesh-agents",
  "runtime_dir": "/run/user/UID/mesh-agents",
  "runtime": {
    "executable": "/PINNED/NATIVE/codex",
    "auth_home": "/EXISTING/OWNER/CODEX/PROFILE",
    "network_env_file": "/PRIVATE/OPTIONAL-PROXY-ENV.json"
  },
  "external_agents": {
    "leader": {"model": "CURRENT_MODEL", "reasoning_effort": "CURRENT_EFFORT"},
    "claw": {"runtime": {"auth_home": "/EXISTING/CLAW/PROFILE"}}
  }
}
```

Omit `network_env_file` if not needed. The initial adapter is Linux/native ELF
Codex + the owner's existing ChatGPT/OpenAI subscription profile; it rejects
unreviewed executable wrappers and never creates keys, copies auth, enables
paid fallback or edits global config. Other providers/platforms can supply a
thin adapter; unsupported here does not restrict any native node functionality.
Native Shell/files/network/MCP remain present. Managed execution uses the
owner's already approved full native tools posture, not a tool allowlist.

The durable `state_dir/profiles.json` has format `mesh-agent-profiles/v1`,
`agents` and `requests`. `load_profile(path, agent_id)` returns only
`{revision, model, reasoning_effort}` or None. Root binds this loader to the
actual original Leader/Claw worker's `reasoning.make_codex` factory; do not
guess identity from user prompts. The same CLI configures those external
agents. Their status is `externally_managed`, not fabricated liveness, and
`stop` cannot kill their arbitrary recorded PIDs. An existing authority-specific
external stop adapter remains separate; this leaf must not stop the Leader.

## Durability and cancellation

Launch intent, private prompt, model selection and source config hash persist
before spawning a short-lived owner-local supervisor. Reusing the same ID
returns the original request only; differing input conflicts. Launch/turn
unknown never launches again. Only a successfully completed original native
turn can receive a new explicit input through `resume`, retaining its session.

`stop` talks to the original owner-only Unix socket with nonce and peer PID/UID
verification. The live supervisor signals only its exact child process group,
keeps the child PID reserved with Linux `WNOWAIT` until signalling finishes,
then reaps it and same-group orphaned tool children. Missing/ambiguous control
does not fall back to killing a PID from disk. A malformed status client does
not abort the native turn. Per-turn local sockets are not public Mesh APIs.

Stopping preserves prompts, JSONL, native session/history, original request and
unknown effects. It is not rollback, permission to replay, or proof of remote /
deliberately detached side-effect closure. Do not delete histories on destroy.

Four targeted checks cover next-turn/external bindings, original-ID launch
durability, a real synthetic supervisor/group cancellation, and original-session
completion/new-input binding. They make zero Codex/model requests. Real native
catalog, model effort and original Leader next-turn acceptance are Root-owned
operations and must be recorded separately from these checks.
