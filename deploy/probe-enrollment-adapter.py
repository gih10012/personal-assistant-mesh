#!/usr/bin/env python3
"""Explicit isolated reference probe, never a production enrollment or model.

Runs the public bootstrap CLI and an independent JavaScript process with a
synthetic owner/authority. Private stage receipts are exclusive and resumable;
the original operation/journal is reused, unknown execution is not replayed.
"""
import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from assistant_mesh.enrollment import _private_directory, document_sha256
from assistant_mesh.enrollment_bootstrap import EnrollmentBootstrap, _private_json, _write_new_private


def blob(path, raw):
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}


def configuration(root, operation_id, repo_pin):
    owner = root / 'owner.json'
    if owner.exists():
        saved = _private_json(owner)
        if (saved['authority_kind'] != 'synthetic'
                or saved['challenge']['operation_id'] != operation_id
                or saved['enrollment']['repo_pin']['commit'] != repo_pin):
            raise ValueError('probe_original_identity_required')
        return owner
    node = shutil.which('node')
    if node is None:
        raise ValueError('probe_node_recipe_unavailable')
    node = Path(node).resolve()
    script = ROOT / 'deploy/enrollment-stdio-adapter.mjs'
    now = int(time.time())
    document = {'format': 'mesh-enrollment/1', 'mesh_id': 'isolated-bootstrap-mesh',
        'node_id': 'isolated-javascript-adapter', 'owner_binding': 'synthetic-owner-not-production',
        'repo_pin': {'repository': 'https://github.com/gih10012/personal-assistant-mesh', 'commit': repo_pin},
        'runtime': {'kind': 'temporary', 'expires_at': now + 1800,
                    'state_ref': 'private:isolated-persistent-authority-state'},
        'join': {'peer_id': 'isolated-reference-authority', 'authority_id': 'synthetic-authority',
                 'route_ref': 'private:owner-admitted-stdio', 'credential_ref': 'private:no-secret-fixture',
                 'trust_ref': 'private:synthetic-policy-not-owner-membership'},
        'continuation': {'project_id': '入网自适配 🧪', 'session_ref': 'native:original-isolated-specialist',
                         'state_ref': 'mesh:original-checkpoint'}}
    input_bytes = bytes((index * 19 + 31) % 256 for index in range(51200))
    config = {'schema': 1, 'enabled': True, 'authority_kind': 'synthetic',
        'state_dir': str(root / 'authority-state'), 'enrollment': document,
        'authorization': {'enrollment_sha256': document_sha256(document), 'valid_until': now + 1800,
                          'capabilities': [{'id': 'isolated-bytes', 'actions': ['challenge.bytes.repeat'],
                                            'source_device': None, 'proxy_binding': None}]},
        'verifier_id': 'separate-synthetic-full-byte-oracle',
        'adapter': {'argv': [str(node), str(script), '--repeat', '3'],
                    'pins': [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                             for path in (node, script)], 'timeout_seconds': 10},
        'challenge': {'operation_id': operation_id, 'capability_id': 'isolated-bytes',
                      'action': 'challenge.bytes.repeat', 'revision': 1, 'ttl_seconds': 300,
                      'input': blob(root / 'input.bin', input_bytes),
                      'oracle': blob(root / 'oracle.bin', input_bytes * 3), 'proxy': None},
        'inventory': {'commands': ['node', 'python3', 'git', 'fish', 'codex', 'pi'],
                      'network_metadata': False,
                      'harness': {'name': 'native-harness-not-invoked', 'version': 'unknown', 'command': None},
                      'candidates': [{'id': 'isolated-bytes', 'class': 'compute.bytes', 'revision': 1,
                          'actions': ['challenge.bytes.repeat'], 'scope': {'task': 'isolated-bootstrap-only'},
                          'resource_pool': 'one-owned-direct-process',
                          'fault_domain': 'same-physical-host-not-a-third-device',
                          'source_device': None, 'proxy_binding': None}]}}
    _write_new_private(owner, config)
    return owner


def stage(root, owner, action, operation_id, number):
    saved = root / ('%02d-%s.json' % (number, action))
    if saved.exists():
        return _private_json(saved, 16 * 1024 * 1024)
    argv = [sys.executable, '-m', 'assistant_mesh.enrollment_bootstrap', '--config', str(owner), action]
    if action in ('challenge', 'run', 'verify', 'status', 'receipt'):
        argv.extend(['--operation-id', operation_id])
    process = subprocess.Popen(argv, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = process.communicate(timeout=20)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    if process.returncode != 0 or stderr or len(stdout) > 16 * 1024 * 1024:
        raise ValueError('probe_cli_stage_failed')
    value = json.loads(stdout.decode('utf8'))
    _write_new_private(saved, value)
    return value


def probe(state_dir, operation_id, repo_pin):
    root = _private_directory(state_dir)
    owner = configuration(root, operation_id, repo_pin)
    final = root / 'probe-summary.json'
    if final.exists():
        # Reconcile original actual evidence; no new attempt on repeat probe.
        summary = _private_json(final)
        saved = _private_json(root / '08-receipt.json', 16 * 1024 * 1024)
        current = EnrollmentBootstrap(owner).receipt(operation_id)
        if any(current[key] != saved[key] for key in ('challenge', 'result', 'verification', 'registration')):
            raise ValueError('probe_original_journal_changed')
        full = base64.b64decode(current['result']['result']['base64'], validate=True)
        if (full != (root / 'oracle.bin').read_bytes()
                or hashlib.sha256(full).hexdigest() != summary['full_artifact_sha256']):
            raise ValueError('probe_original_full_artifact_changed')
        return dict(summary, original_full_journal_reconciled=True, new_execution_attempted=False,
                    current_state=current['status']['state'], owner_enabled=current['owner_enabled'])
    stage(root, owner, 'plan', operation_id, 1)
    inventory = stage(root, owner, 'inventory', operation_id, 2)
    stage(root, owner, 'bootstrap', operation_id, 3)
    challenge = stage(root, owner, 'challenge', operation_id, 4)
    run = stage(root, owner, 'run', operation_id, 5)
    if run['status']['state'] != 'result_reported':
        return {'status': run['status']['state'], 'operation_id': operation_id,
                'synthetic_authority': True, 'automatic_retry': False,
                'production_node_registered': False}
    verified = stage(root, owner, 'verify', operation_id, 6)
    stage(root, owner, 'status', operation_id, 7)
    receipt = stage(root, owner, 'receipt', operation_id, 8)
    full = base64.b64decode(receipt['result']['result']['base64'], validate=True)
    oracle = (root / 'oracle.bin').read_bytes()
    if full != oracle or challenge != receipt['challenge']:
        raise ValueError('probe_original_receipt_or_full_bytes_mismatch')
    summary = {'format': 'mesh-bootstrap-probe/1', 'status': 'verified',
        'operation_id': operation_id, 'repo_pin': repo_pin, 'synthetic_authority': True,
        'actual_node_adapter_language': 'javascript',
        'process_observation': run['process_observation'], 'full_artifact_bytes': len(full),
        'full_artifact_sha256': hashlib.sha256(full).hexdigest(),
        'original_request_sha256': document_sha256(challenge),
        'full_receipt_sha256': document_sha256(receipt),
        'inventory_sha256': document_sha256(inventory),
        'verification': verified['verification'], 'registration_state': verified['registration_state'],
        'production_node_registered': False, 'third_physical_device_verified': False,
        'official_cloud_job_verified': False, 'native_execution_attested': False,
        'managed_use_authorized': False, 'native_features_restricted': False,
        'automatic_retry': False, 'verified_at': int(time.time())}
    _write_new_private(final, summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description='Explicit isolated bootstrap CLI/JS reference probe')
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--operation-id', required=True)
    parser.add_argument('--repo-pin', required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(probe(args.state_dir, args.operation_id, args.repo_pin), sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({'status': 'error', 'category': 'isolated_bootstrap_probe_failed',
                          'automatic_retry': False, 'production_node_registered': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
