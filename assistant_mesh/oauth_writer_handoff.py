"""Verify the retired OAuth source and staged target, and assemble their Raft transfer."""
import argparse
import subprocess

from .business_journal import canonical, digest
from .business_writer_grant import confirm_grant
from .business_writer_handoff import _fields, _hash, _plan, _public_key, _read, sign_witness, verify_witness
from .config import private_json
from .oauth_backup_control import RETIREMENT, identity, read_record
from .oauth_state_boundary import RETIREMENT_MARKER
from .oauth_state_bundle import _source_scan, binding, configured_key_paths, decode, inspect_staging, safe_path, verify_bundle


NAMESPACE = 'amita-oauth-writer-handoff'
EVIDENCE = 'amita-oauth-writer-witness/1'
HANDOFF = 'amita-oauth-writer-transfer/1'
SOURCE_FIELDS = {'binding_sha256', 'bundle_sha256', 'tree_sha256', 'source_retirement_sha256'}
TARGET_FIELDS = {'source_witness_sha256', 'target_state_sha256', 'target_directory_sha256'}
RETIREMENT_FIELDS = {'format', 'operation_id', 'instance_id', 'bundle_sha256', 'binding_sha256', 'tree_sha256',
                     'retired_at', 'local_restart_closed', 'cross_node_writer_fenced', 'activation_authorized', 'sha256'}


def _oauth_plan(plan):
    plan = _plan(plan, hash_fields=())
    identity(plan['operation_id'])
    return plan


def _body(plan, kind, evidence):
    holder = plan['source_holder' if kind == 'source' else 'next_owner']
    return dict(format=EVIDENCE, kind=kind, holder=holder, plan_sha256=digest(plan), evidence=evidence)


def _sign(plan, kind, evidence, key):
    body = _body(plan, kind, evidence)
    return dict(body=body, signature=sign_witness(body, key, NAMESPACE))


def _verify(plan, kind, witness, public_key, fields):
    if not _fields(witness, {'body', 'signature'}) or not isinstance(witness['body'], dict):
        raise ValueError('oauth_handoff_witness_invalid')
    body = witness['body']
    evidence = body.get('evidence')
    if (not _fields(evidence, fields) or not all(_hash(value) for value in evidence.values())
            or body != _body(plan, kind, evidence)):
        raise ValueError('oauth_handoff_witness_binding_conflict')
    verify_witness(body, witness['signature'], public_key, NAMESPACE)
    return evidence


def sign_source(plan, document, bundle_path, private_key, *, allow_loopback_for_tests=False):
    plan = _oauth_plan(plan)
    config, _ = binding(document, allow_loopback_for_tests)
    root = safe_path(config['state']['directory'])
    marker = root / RETIREMENT_MARKER
    retired = read_record(marker)
    if (not _fields(retired, RETIREMENT_FIELDS) or retired.get('format') != RETIREMENT
            or retired.get('operation_id') != plan['operation_id']
            or retired.get('local_restart_closed') is not True or retired.get('activation_authorized') is not False
            or retired.get('sha256') != digest({key: value for key, value in retired.items() if key != 'sha256'})
            or not _hash(retired.get('bundle_sha256'))):
        raise ValueError('oauth_handoff_original_retirement_required')
    bundle, exact, _ = verify_bundle(bundle_path, config, key_paths=configured_key_paths(config),
        expected_sha256=retired['bundle_sha256'], allow_loopback_for_tests=allow_loopback_for_tests)
    files, directories, _ = _source_scan(root, bundle['writer_observation'])
    raw = files.pop(RETIREMENT_MARKER, b'null')
    if (str(root) != bundle['source_root'] or retired['binding_sha256'] != bundle['binding_sha256']
            or retired['tree_sha256'] != bundle['source_stability']['tree_sha256'] or files != exact
            or directories != bundle['directories'] or decode(raw) != retired):
        raise ValueError('oauth_handoff_retired_state_changed')
    return _sign(plan, 'source', dict(binding_sha256=bundle['binding_sha256'], bundle_sha256=bundle['sha256'],
        tree_sha256=retired['tree_sha256'], source_retirement_sha256=retired['sha256']), private_key)


def target_directory_sha256(path):
    root = safe_path(path)
    info = root.stat()
    return digest([str(root), info.st_dev, info.st_ino])


def sign_target(plan, source, source_public_key, document, staging, private_key, *, allow_loopback_for_tests=False):
    plan = _oauth_plan(plan)
    evidence = _verify(plan, 'source', source, _public_key(source_public_key), SOURCE_FIELDS)
    config, bound = binding(document, allow_loopback_for_tests)
    if digest(bound) != evidence['binding_sha256']:
        raise ValueError('oauth_handoff_target_policy_conflict')
    state_hash = inspect_staging(staging, document, evidence['bundle_sha256'], allow_loopback_for_tests=allow_loopback_for_tests)
    return _sign(plan, 'target', dict(source_witness_sha256=digest(source['body']), target_state_sha256=state_hash,
        target_directory_sha256=target_directory_sha256(config['state']['directory'])), private_key)


def assemble_transfer(plan, source, target, source_public_key, target_public_key):
    plan = _oauth_plan(plan)
    source_key, target_key = _public_key(source_public_key), _public_key(target_public_key)
    if source_key == target_key:
        raise ValueError('oauth_handoff_independent_holder_keys_required')
    source_evidence = _verify(plan, 'source', source, source_key, SOURCE_FIELDS)
    target_evidence = _verify(plan, 'target', target, target_key, TARGET_FIELDS)
    if target_evidence['source_witness_sha256'] != digest(source['body']):
        raise ValueError('oauth_handoff_target_receipt_conflict')
    receipts = sorted((dict(holder=w['body']['holder'], sha256=digest(w['body'])) for w in (source, target)),
                      key=lambda item: item['holder'])
    checkpoint = dict(source_evidence, target_state_sha256=target_evidence['target_state_sha256'],
                      source_holder=plan['source_holder'], holder_receipts=receipts)
    return dict(format=HANDOFF, operation_id=plan['operation_id'], group=plan['group'], scope=plan['scope'],
                role='oauth_writer', previous=plan['previous'], next_owner=plan['next_owner'],
                next_epoch=plan['previous']['epoch'] + 1, checkpoint=checkpoint, checkpoint_sha256=digest(checkpoint))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    proofs = ('source', 'target', 'source-public-key', 'target-public-key')
    for action, fields in (('source', ('config', 'bundle', 'private-key')),
                           ('target', ('source', 'source-public-key', 'config', 'staging', 'private-key')),
                           ('assemble', proofs), ('confirm', proofs + ('voter-config',))):
        command = commands.add_parser(action)
        command.add_argument('--plan', required=True)
        for field in fields:
            command.add_argument('--' + field, required=True)
    args = parser.parse_args(argv)
    try:
        plan = _read(args.plan)
        if args.action == 'source':
            result = sign_source(plan, private_json(args.config), args.bundle, args.private_key)
        elif args.action == 'target':
            result = sign_target(plan, _read(args.source), args.source_public_key, private_json(args.config),
                                 args.staging, args.private_key)
        else:
            result = assemble_transfer(plan, _read(args.source), _read(args.target),
                                       args.source_public_key, args.target_public_key)
            if args.action == 'confirm':
                result = confirm_grant(result, private_json(args.voter_config))
        print(canonical(result))
        return 0
    except (OSError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
        print(canonical(dict(error=str(error))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
