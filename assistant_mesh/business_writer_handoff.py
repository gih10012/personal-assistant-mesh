"""Build a Raft writer transfer from signed old-writer and target disk evidence."""
import argparse
import base64
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

from .business_journal import canonical, digest
from .business_recovery import _checkpoint, inspect_recovery
from .business_writer_seal import _identifier, _integer, sealed_checkpoint
from .worker_claim_receipts import FORMAT as CLAIM_PROTOCOL


NAMESPACE = 'amita-business-writer-handoff'
EVIDENCE = 'amita-business-writer-witness/1'
TRANSFER = 'amita-replica-writer-transfer/1'
PLAN_FIELDS = {'operation_id', 'group', 'scope', 'source_holder', 'next_owner', 'previous'}
BUSINESS_HASH_FIELDS = ('external_dependencies_manifest_sha256', 'retained_effects_sha256')


def _need(condition, reason):
    if not condition:
        raise ValueError('business_handoff_' + reason)


def _hash(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _fields(value, keys):
    return isinstance(value, dict) and set(value) == keys


def _plan(value, hash_fields=BUSINESS_HASH_FIELDS):
    _need(_fields(value, PLAN_FIELDS | set(hash_fields)) and all(_identifier(value[k]) for k in
          ('operation_id', 'group', 'scope', 'source_holder', 'next_owner'))
          and value['source_holder'] != value['next_owner']
          and all(_hash(value[k]) for k in hash_fields),
          'plan_invalid')
    previous = value['previous']
    _need(_fields(previous, {'owner', 'epoch', 'head'}) and _integer(previous['epoch'])
          and _integer(previous['epoch'] + 1) and _fields(previous['head'], {'index', 'sha256'})
          and _integer(previous['head']['index']) and _hash(previous['head']['sha256']), 'previous_invalid')
    if previous['epoch'] == 0:
        _need(previous == {'owner': None, 'epoch': 0, 'head': {'index': 0, 'sha256': '0' * 64}}, 'genesis_invalid')
    else:
        _need(previous['owner'] == value['source_holder'] and previous['head']['index'] > 0, 'previous_invalid')
    return value


def _ssh(arguments, body):
    result = subprocess.run(['ssh-keygen', '-Y'] + arguments, input=canonical(body).encode('ascii'),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    _need(result.returncode == 0, 'witness_signature_failed')
    return result.stdout.decode('ascii')


def _public_key(path):
    key = Path(path).read_text(encoding='utf8').split()[:2]
    _need(len(key) == 2 and key[0] == 'ssh-ed25519' and re.fullmatch(r'[A-Za-z0-9+/]{68}', key[1]),
          'trusted_ed25519_key_required')
    blob = base64.b64decode(key[1], validate=True)
    _need(blob.startswith(b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20'), 'trusted_ed25519_key_required')
    return ' '.join(key)


def _body(plan, kind, evidence):
    holder = plan['source_holder' if kind == 'source' else 'next_owner']
    return dict(format=EVIDENCE, kind=kind, holder=holder, plan_sha256=digest(plan), evidence=evidence)


def _sign(plan, kind, evidence, key):
    body = _body(plan, kind, evidence)
    signature = sign_witness(body, key, NAMESPACE)
    return dict(body=body, signature=signature)


def sign_witness(body, key, namespace):
    return _ssh(['sign', '-f', str(Path(key).absolute()), '-n', namespace], body)


def verify_witness(body, signature, public_key, namespace):
    _need(_identifier(body['holder']) and isinstance(signature, str) and signature.isascii()
          and 0 < len(signature) <= 8192, 'witness_invalid')
    with tempfile.TemporaryDirectory(prefix='amita-handoff-') as directory:
        signers = Path(directory) / 'allowed_signers'
        signers.write_text(body['holder'] + ' ' + public_key + '\n', encoding='ascii')
        path = Path(directory) / 'signature'
        path.write_text(signature, encoding='ascii')
        _ssh(['verify', '-f', str(signers), '-I', body['holder'], '-n', namespace, '-s', str(path)], body)


def _verify(plan, kind, envelope, public_key):
    _need(_fields(envelope, {'body', 'signature'}) and isinstance(envelope['body'], dict), 'witness_invalid')
    body = envelope['body']
    evidence = body.get('evidence')
    _need(isinstance(evidence, dict) and body == _body(plan, kind, evidence), 'witness_plan_conflict')
    verify_witness(body, envelope['signature'], public_key, NAMESPACE)
    return evidence


def _source_evidence(plan, envelope, public_key):
    evidence = _verify(plan, 'source', envelope, public_key)
    _need(_fields(evidence, {'checkpoint', 'source_seal_sha256'}) and _hash(evidence['source_seal_sha256']),
          'source_evidence_invalid')
    position = _checkpoint(evidence['checkpoint'])
    _need(_identifier(position.authority) and _identifier(position.incarnation)
          and _integer(position.generation, 1) and _integer(position.head), 'source_position_invalid')
    return evidence


def sign_source(plan, database, private_key):
    plan = _plan(plan)
    checkpoint, seal_hash = sealed_checkpoint(database, plan['operation_id'], plan['source_holder'])
    return _sign(plan, 'source', dict(checkpoint=checkpoint, source_seal_sha256=seal_hash), private_key)


def sign_target(plan, source, source_public_key, replica, template, candidate, private_key):
    plan = _plan(plan)
    original = _source_evidence(plan, source, _public_key(source_public_key))
    inspected = inspect_recovery(replica, template, candidate, original['checkpoint'])
    evidence = dict(source_witness_sha256=digest(source['body']), candidate_sha256=inspected['candidate_sha256'],
                    schema_sql_sha256=inspected['schema_sql_sha256'])
    return _sign(plan, 'target', evidence, private_key)


def assemble_transfer(plan, source, target, source_public_key, target_public_key):
    plan = _plan(plan)
    source_key, target_key = _public_key(source_public_key), _public_key(target_public_key)
    _need(source_key != target_key, 'independent_holder_keys_required')
    original = _source_evidence(plan, source, source_key)
    prepared = _verify(plan, 'target', target, target_key)
    _need(_fields(prepared, {'source_witness_sha256', 'candidate_sha256', 'schema_sql_sha256'})
          and prepared['source_witness_sha256'] == digest(source['body'])
          and _hash(prepared['candidate_sha256']) and _hash(prepared['schema_sql_sha256']),
          'target_evidence_conflict')
    receipts = [dict(holder=record['body']['holder'], sha256=digest(record['body'])) for record in (source, target)]
    checkpoint = dict(**original['checkpoint'], source_holder=plan['source_holder'],
        source_seal_sha256=original['source_seal_sha256'], holder_receipts=sorted(receipts, key=lambda r: r['holder']),
        external_dependencies_manifest_sha256=plan['external_dependencies_manifest_sha256'],
        retained_effects_sha256=plan['retained_effects_sha256'], worker_claim_protocol=CLAIM_PROTOCOL)
    return dict(format=TRANSFER, operation_id=plan['operation_id'], group=plan['group'], role='replica_writer',
                scope=plan['scope'], previous=plan['previous'], next_owner=plan['next_owner'],
                next_epoch=plan['previous']['epoch'] + 1, checkpoint=checkpoint, checkpoint_sha256=digest(checkpoint))


def _read(path):
    with open(path, 'rb') as handle:
        raw = handle.read(65537)
    _need(len(raw) <= 65536, 'input_too_large')
    return json.loads(raw)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action, fields in (
            ('source', ('database', 'private-key')),
            ('target', ('source', 'source-public-key', 'replica', 'template', 'candidate', 'private-key')),
            ('assemble', ('source', 'target', 'source-public-key', 'target-public-key'))):
        command = commands.add_parser(action)
        command.add_argument('--plan', required=True)
        for field in fields:
            command.add_argument('--' + field, required=True)
    args = parser.parse_args(argv)
    try:
        plan = _read(args.plan)
        if args.action == 'source':
            result = sign_source(plan, args.database, args.private_key)
        elif args.action == 'target':
            result = sign_target(plan, _read(args.source), args.source_public_key, args.replica,
                                 args.template, args.candidate, args.private_key)
        else:
            result = assemble_transfer(plan, _read(args.source), _read(args.target),
                                       args.source_public_key, args.target_public_key)
        print(canonical(result))
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(canonical(dict(error=str(error))))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
