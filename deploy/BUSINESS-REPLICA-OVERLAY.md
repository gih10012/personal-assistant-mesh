# Source overlay assembly

`build-business-replica-overlay.py` builds two explicit components without
importing their runtime code, reading a DB/private runtime configuration or
starting services. Root collects actual sources/pins and owns deployment,
private policy/credentials and the coordinated window for **all DB writers**.
See [the replication contract](BUSINESS-REPLICATION.md) for activation order.

Use the support root containing the final three business modules, including
`business_journal.hook_store_type`. Local snapshot paths and installed source
paths are separate: only installed paths appear in executable output. The
destination must be a new absolute directory whose parent already exists.
The assembled directory can be transferred to its final release location;
the launcher selects its own directory as `sys.path[0]`.

```sh
python3 deploy/build-business-replica-overlay.py receiver \
  --support-root /LOCAL/FINAL-SOURCE \
  --package-init /LOCAL/ACTUAL-SNAPSHOT/assistant_mesh/__init__.py \
  --init-sha256 INIT_SHA256 \
  --installed-init /REMOTE/ACTUAL-NAMESPACE/assistant_mesh/__init__.py \
  --launcher /LOCAL/ACTUAL-SNAPSHOT/launch_receiver.py \
  --launcher-sha256 LAUNCHER_SHA256 \
  --installed-launcher /REMOTE/amita-phone-recovery-20261029.1/launch_receiver.py \
  --runtime-pins /LOCAL/actual-runtime-pins.json \
  --policy-file /REMOTE/PRIVATE/business-replication-policy.json \
  --snapshot-ttl 600 \
  --destination /LOCAL/NEW-RECEIVER-RELEASE

python3 deploy/build-business-replica-overlay.py node \
  --support-root /LOCAL/FINAL-SOURCE \
  --package-init /LOCAL/ACTUAL-NODE-SNAPSHOT/assistant_mesh/__init__.py \
  --init-sha256 NODE_INIT_SHA256 \
  --installed-init /home/ai_agent/.local/share/personal-assistant-mesh/assistant_mesh/__init__.py \
  --runtime-pins /LOCAL/actual-node-runtime-pins.json \
  --destination /LOCAL/NEW-NODE-RELEASE
```

The optional pins file is a JSON object mapping explicit **installed absolute
source file paths** to their SHA256 values. The builder records these pins;
the generated launcher verifies them before importing the namespace. It also
verifies the explicit original namespace init, original receiver launcher and
all four generated namespace files. Build does not open the additional pinned
paths, so remote source pins can be supplied from Root's collected evidence.
`business-replica-overlay.json` records runtime pins and module hashes.

Receiver composition is intentionally fixed to the current inspected recovery
chain: loaded launcher `_incumbent._recovery_incumbent._incumbent.previous`.
The new launcher hooks the original Store type before loading that launcher,
then wraps the entry's current `ready`: hook its Store instance, attach the
private policy, set the bounded snapshot TTL (10–600 seconds), and call the
original `ready`. Finally it calls the original entry's `main`. It preserves
the phone ready chain and original Store constructor/business implementation.

Launch the node with its existing argument tail and working directory:

```sh
python3 /REMOTE/NEW-NODE-RELEASE/launch_node.py --config /PRIVATE/node.json node
```

Keep `start_server=false` and `start_worker=false` in that existing private
config. The builder does not verify or change it; the manifest marks these
flags as required, not observed. This Node still writes the shared source DB,
so it belongs in Root's writer coordination window. The launcher hooks the
original Store type before importing the original `assistant_mesh.cli.main`
and preserves `sys.argv[1:]`. Neither overlay replaces Store, Worker, server,
Node, receiver modules, media or native history, and neither authorizes failover.

One synthetic entrypoint case is provided for Root's Click Evidence execution:

```sh
python3 -m unittest tests.test_business_replica_overlay.BusinessReplicaOverlayTests
```

Run it with the absolute worktree as workdir. It checks source relocation,
pre-constructor hooks, receiver ready ordering/TTL, node argument preservation,
runtime pin rejection and refusal to overwrite an existing release. It opens
no production DB and starts no actual runtime services.
