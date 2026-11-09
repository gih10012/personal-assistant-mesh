# Phone autonomy receiver overlay

Build source only; no Store, account, private configuration or incumbent runtime
is imported. The installed receiver's captured public parent is the input.
The source commit applies to the four new phone modules. The parent launcher,
namespace initializer, three native-partial modules and original manifest remain
byte-identical; `parent_launch_receiver.py` retains the original launcher.
The new `launch_receiver.py` checks every inherited runtime pin plus actual
parent-release pins and candidate module hashes, then calls the unchanged
parent's `_entry.main()`. It adds no ready callback and forwards the original
argv. Existing namespace fallback supplies protected core and phone_http.

Reproduce the 2026-11-09 candidate into a **new** destination:

```sh
python3 deploy/build-phone-autonomy-overlay.py \
  --parent-snapshot /home/gihggiy/.local/state/amita-phone-deployment/20261109/parent \
  --parent-manifest-sha256 8598e064358300bf0a559da1d6c898199d9bb0367bc6303045719c06fa9c009a \
  --source-commit 4d69f302daa3533d1fa92258caf77ebbd0d8e319 \
  --destination /PRIVATE/new-candidate-directory \
  --installed-parent /home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-native-partial-receiver-20261101.1 \
  --installed-target /home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-phone-autonomy-receiver-20261109.1
```

The actual local output is
`/home/gihggiy/.local/state/amita-phone-deployment/20261109/candidate`.
Its `phone-autonomy-overlay.json` SHA-256 is
`f5aa14bf6e19c1e9b0212525a281cc2885528e434d8a5aec2a14ba49794c4c0f`.
It has 12 files and is marked `production_installed=false`,
`end_to_end_verified=false`, `ledger_opened=false`. A pin mismatch is a stop;
never update an old expected hash merely to make the launcher pass.

Root alone performs deployment, after its original backup and active-task=0
window. Keep the original unit, configuration, socket, DB and DB inode. Copy
the candidate as a new immutable release; retain the parent release and every
inherited pinned path for rollback:

```sh
scp -pr /home/gihggiy/.local/state/amita-phone-deployment/20261109/candidate \
  ai-server:/home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-phone-autonomy-receiver-20261109.1
```

On Source, use the **existing** receiver user unit and its unchanged interpreter
from `systemctl --user show ... -p ExecStart`. The source snapshot contains no
unit name; do not infer one. Root supplies these two already-inspected values:

```sh
receiver_unit='EXISTING_RECEIVER.service'
receiver_python='/usr/bin/python3'
systemctl --user show "$receiver_unit" -p ExecStart
mkdir -p "/home/ai_agent/.config/systemd/user/$receiver_unit.d"
cat > "/home/ai_agent/.config/systemd/user/$receiver_unit.d/99-phone-autonomy-20261109.conf" <<EOF
[Service]
ExecStart=
ExecStart=$receiver_python /home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-phone-autonomy-receiver-20261109.1/launch_receiver.py --config /home/ai_agent/.local/state/personal-assistant-mesh/server.json --socket /run/user/1003/assistant-mesh-channel-owner/control.sock
EOF
systemctl --user daemon-reload
systemctl --user restart "$receiver_unit"
```

Preserve any additional real original argv; the two shown config/socket arguments
are the confirmed current contract. Add this task's new drop-in only if that
filename does not already exist and it sorts after the current ExecStart
override. This document does not authorize replacing an unrelated drop-in.

One upper-level acceptance uses the existing private operator Client and fixed
phone owner configuration. Root supplies their real existing paths; no new
onboarding task, enrollment operation or bearer is required:

```sh
phone_owner_config='/PRIVATE/EXISTING_FIXED_PHONE_CONFIG.json'
operator_config='/PRIVATE/EXISTING_OPERATOR_CLIENT_CONFIG.json'
phone_digest=$(sha256sum "$phone_owner_config" | cut -d ' ' -f 1)
PYTHONPATH=/home/ai_agent/.local/share/personal-assistant-mesh-tools/amita-phone-autonomy-receiver-20261109.1 \
  "$receiver_python" -m assistant_mesh.phone_owner --config "$operator_config" \
  reload-installed --expected-sha256 "$phone_digest"
```

`installed_sha256` must match. Repeating the same digest is read/idempotent
installation, preserving node/token/session/enrollment, original operations and
revocation. A listening service or this source build is not phone end-to-end
control, background delivery, local cognition or Live permission evidence.

Rollback in Root's safe idle window removes **only this task's new drop-in**,
then restarts the original unit. No SQLite restore, identity rotation, original
file overwrite or unknown-operation replay is part of rollback:

```sh
rm -- "/home/ai_agent/.config/systemd/user/$receiver_unit.d/99-phone-autonomy-20261109.conf"
systemctl --user daemon-reload
systemctl --user restart "$receiver_unit"
```

If new optional private config fields were staged, Root retains its pre-change
private configuration backup and selects a config understood by the old release
before rollback. Never export that private file in this source package.
