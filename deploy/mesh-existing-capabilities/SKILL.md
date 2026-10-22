---
name: mesh-existing-capabilities
description: Use the owner's installed campus knowledge subset and Xiumi JSON compose/validate helpers, or delegate existing laptop browser, clipboard and niri desktop actions through Mesh. Applies to these existing devices and action bindings, not new-device enrollment.
---

Read `references/binding.json` and this entry's `installation.json`. They bind
the existing private source installation, selected Python and original provider.
They contain no transferred account, browser session or MCP backend.

Before using a bound action, run the selected `python_executable` from the
binding with `-B scripts/verify.py` (use the absolute path of this skill).
It checks the original receipt, every source pin and Python 3.9 minimum without
importing skill code. A failure means a concrete dependency/integrity gap;
report it without changing pins or selecting an unreviewed installation.
A successful check establishes source/dependency presence, not business success.

For a portable action, read its original `skill` and relevant `references` from
`binding.json`, then use its `script`/`module` on this node:

| Action | Bound use |
| --- | --- |
| `ncut.knowledge` | Run `python -B script knowledge --query QUERY [--details]`. This searches only the packaged knowledge subset; no school login/request is included. |
| `xiumi.compose` | Import the bound `module` using the selected Python with `-B` and build components per the bound format reference. Save task artifacts outside the source installation. |
| `xiumi.validate` | Run `python -B script validate AUTHORIZED_JSON [--save-ready]`. Validation does not upload images, paste into a browser or save an article. |

Use owner-authorized inputs and output locations. The original skill can mention
unbundled workflows; that does not make them installed. Do not invoke its other
school/business, `serve`, browser, clipboard or account branches on this target.
Do not add image/account/native-history files to this private source package.

For school authentication/network, Xiumi image localization/paste/save, and niri
desktop actions, use the existing authenticated A2A path to `source_provider`:

`mesh(action="remote_delegate", arguments={peer,input,project_id,agent_id,role})`

The peer is the binding's original provider. Include the requested action,
original operation ID and exact task scope in `input`; let the provider helper
read its ORIGINAL locally installed skill and current native MCP dependencies.
Use `agent_id="existing-device-helper"` and `role="existing-device-helper"`.
An old thread can use its existing `mesh_remote_delegate` or task-bound resume
CLI from the Mesh working contract; do not install credentials to add a tool.
Keep the original child/delivery receipt after submission. Unknown outcomes are
resolved by reading that receipt, never by another task or operation ID.

Niri instructions in a catalog are not its backend. The laptop helper checks
its current profile's actual niri MCP tools and Wayland/DBus session. Coordinate
the sole GUI operator; return busy rather than take over. Follow original
begin/wake/end and power/brightness restoration rules. Read-only window listing
does not need input or wake. School evaluation remains independently paused.

Catalog registration from this package starts `health=unavailable`, with
`runtime_verified=false` and no managed provider grant. Native skill discovery,
actual action results and independent evidence are separate. Preserve all native
Shell/files/network/MCP tools; these bindings only add a convenient Mesh route.
