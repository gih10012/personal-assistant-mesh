"""Owner operations on the incumbent API and original enrollment journal.

Only fixed filenames beneath the authority's owner-selected private root are
read. RPC callers select an enrollment ID, never a path, command or validator.
No business Store is constructed and no adapter is executed by this module.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

from .enrollment import EnrollmentError, _private_directory, document_sha256
from .enrollment_bootstrap import EnrollmentBootstrap, _private_json, _write_new_private
from .enrollment_relay import EnrollmentRelay, OWNER_FORMAT
from .phone_authority import LiveTaskGate, RegistryEnrollment, issue_node, verified_enrollment
from .ingress import _identifier

ACTIONS = ('plan', 'status', 'prepare-existing-binding', 'verify', 'register', 'issue')
PREFIX = '/v1/phone/owner/'
SCOPE = 'leader:project:amita-harmony-onboarding'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z')


def _require(value, category):
    if not value:
        raise ValueError(category)


def atomic_private(path, value):
    """Same-directory atomic replace; no overwrite of a symlink or loose file."""
    path = Path(path)
    _private_directory(path.parent, create=False)
    if path.exists() or path.is_symlink():
        _private_json(path)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
    try:
        _write_new_private(temporary, value)
        os.replace(str(temporary), str(path))
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary.exists():
            temporary.unlink()


class PhoneOwnerOperations:
    def __init__(self, service, *, origin):
        self.service, self.origin = service, origin
        # Fixed at installation, never selected or overridden by an RPC.
        self.root = Path(service.config_path).absolute().parent / 'phone-owner'

    def restore(self):
        # Owner-maintained index, observation-only restoration. No challenge,
        # verifier, registrar, issuance or business execution on startup.
        if not self.root.exists():
            return
        _private_directory(self.root, create=False)
        index = _private_json(self.root / 'bindings.json')
        _require(isinstance(index, dict) and set(index) == {'enrollments'}
                 and isinstance(index['enrollments'], list) and len(index['enrollments']) <= 32,
                 'phone_owner_binding_index_invalid')
        with self.service.lock:
            for enrollment_id in index['enrollments']:
                root, settings, bootstrap = self._load(enrollment_id)
                if (root / 'relay.json').exists():
                    self._attach(*self._relay(root, settings, bootstrap))

    def _load(self, enrollment_id):
        _require(isinstance(enrollment_id, str) and _ID.fullmatch(enrollment_id),
                 'phone_owner_enrollment_id_invalid')
        _private_directory(self.root, create=False)
        index = _private_json(self.root / 'bindings.json')
        _require(isinstance(index, dict) and set(index) == {'enrollments'}
                 and isinstance(index['enrollments'], list) and len(index['enrollments']) <= 32
                 and all(isinstance(i, str) and _ID.fullmatch(i) for i in index['enrollments'])
                 and len(set(index['enrollments'])) == len(index['enrollments'])
                 and enrollment_id in index['enrollments'], 'phone_owner_binding_not_configured')
        root = _private_directory(self.root / enrollment_id, create=False)
        settings = _private_json(root / 'settings.json')
        _require(isinstance(settings, dict) and set(settings) == {
            'format', 'node_id', 'subject', 'session_id', 'capabilities', 'visible_nodes',
            'task_id', 'task_epoch', 'entry_request_id'}, 'phone_owner_settings_invalid')
        _require(settings['format'] == 'amita-phone-owner-binding/1', 'phone_owner_settings_invalid')
        for key in ('node_id', 'task_id', 'entry_request_id'):
            _identifier(settings[key], 'phone_owner_' + key)
        _require(type(settings['task_epoch']) is int and settings['task_epoch'] > 0,
                 'phone_owner_task_epoch_invalid')
        bootstrap = EnrollmentBootstrap(root / 'bootstrap.json')
        _require(bootstrap.document['node_id'] == settings['node_id']
                 and bootstrap.document['mesh_id'] == self.service.sessions.mesh_id
                 and bootstrap.document['runtime']['kind'] == 'interactive'
                 and bootstrap.config['state_dir'] == str(root / 'state'),
                 'phone_owner_original_identity_required')
        # Metadata is validated before any challenge/token is created.
        self.service.validate_policy(settings['subject'], settings['session_id'],
                                     settings['capabilities'], settings['visible_nodes'])
        hooks = RegistryEnrollment(self.service.api.resources, bootstrap)
        bootstrap.registrar, bootstrap.registration_observer = hooks.register, hooks.observe
        return root, settings, bootstrap

    def _live(self, gate, settings):
        if gate(settings) is not True:
            raise PermissionError('phone_owner_original_task_not_live')
        row = gate.local.db.execute('SELECT context FROM tasks WHERE id=?', (settings['task_id'],)).fetchone()
        context = json.loads(row['context'])
        if context.get('session_scope') != SCOPE:
            raise PermissionError('phone_owner_original_scope_required')

    def _relay(self, root, settings, bootstrap):
        gate = LiveTaskGate(self.service.store)
        relay = EnrollmentRelay(bootstrap, root / 'relay.json', live_task_validator=gate,
                                clock=self.service.store.clock)
        _require(relay.config['resource_url'] == self.origin + '/enrollment/node'
                 and all(relay.binding[k] == settings[k] for k in
                         ('node_id', 'task_id', 'task_epoch', 'entry_request_id')),
                 'phone_owner_original_relay_required')
        return relay, gate

    def _relay_candidates(self, relay, gate):
        # Caller holds the same lock as principal, node and session requests.
        operation = relay.binding['operation_id']
        prior = [r for r, unused in self.service.relays if r.binding['operation_id'] == operation]
        _require(not prior or all(r.binding == relay.binding and r.config == relay.config for r in prior),
                 'phone_owner_original_relay_conflict')
        _require(prior or len(self.service.relays) < 32, 'phone_owner_relay_limit')
        return list(self.service.relays) if prior else self.service.relays + [(relay, gate)]

    def _attach(self, relay, gate):
        self.service.relays = self._relay_candidates(relay, gate)

    def handle(self, action, payload):
        _require(action in ACTIONS and isinstance(payload, dict), 'phone_owner_request_invalid')
        if not payload and action in ('plan', 'status'):
            with self.service.lock:
                return {'format': 'amita-phone-owner-result/1', 'action': action,
                    'configuration_only': action == 'plan', 'challenge_created': False,
                    'phone_enrolled': bool(self.service.installed['nodes']),
                    'loaded_node_ids': [n['node_id'] for n in self.service.installed['nodes']],
                    'loaded_operations': [r.binding['operation_id'] for r, g in self.service.relays],
                    'next_step': 'wait for real phone-generated node_id; install owner binding templates',
                    'automatic_replay': False, 'managed_use_authorized': False}
        _require(set(payload) == {'enrollment_id'}, 'phone_owner_request_invalid')
        with self.service.lock:
            root, settings, bootstrap = self._load(payload['enrollment_id'])
            operation = bootstrap.config['challenge']['operation_id']
            base = {'format': 'amita-phone-owner-result/1', 'action': action,
                    'enrollment_id': payload['enrollment_id'], 'node_id': settings['node_id'],
                    'operation_id': operation, 'automatic_replay': False,
                    'managed_use_authorized': False}
            if action == 'plan':
                plan = bootstrap.plan()
                return dict(base, initialized=plan['initialized'], configuration_only=True,
                            challenge_created=False, phone_enrolled=False, owner_enabled=plan['owner_enabled'])
            if action == 'status':
                plan = bootstrap.plan()
                status = None
                if plan['initialized']:
                    try:
                        status = bootstrap.status(operation)
                    except EnrollmentError as error:
                        if str(error) != 'enrollment_operation_unknown':
                            raise
                return dict(base, initialized=plan['initialized'], status=status,
                            relay_loaded=any(r.binding['operation_id'] == operation for r, g in self.service.relays),
                            node_loaded=any(n['node_id'] == settings['node_id'] for n in self.service.installed['nodes']),
                            private_export_available=(root / 'issued.json').is_file())
            self.service.validate_policy(settings['subject'], settings['session_id'],
                                         settings['capabilities'], settings['visible_nodes'], observe=True)
            gate = LiveTaskGate(self.service.store)
            with gate.admission():
                self._live(gate, settings)
                if action == 'prepare-existing-binding':
                    bootstrap.bootstrap()
                    challenge = bootstrap.challenge(operation)  # intent only; original nonce on retry
                    export_path = root / 'relay-export.json'
                    if not export_path.exists():
                        _write_new_private(export_path, {
                            'client': {'format': 'mesh-enrollment-relay-client/1',
                                       'resource_url': self.origin + '/enrollment/node',
                                       'binding': dict({k: challenge[k] for k in (
                                           'mesh_id', 'node_id', 'owner_binding', 'operation_id', 'nonce', 'expires_at')},
                                           enrollment_sha256=document_sha256(bootstrap.document),
                                           request_sha256=document_sha256(challenge),
                                           **{k: settings[k] for k in ('task_id', 'task_epoch', 'entry_request_id')})},
                            'credential': {'kind': 'bearer', 'token': secrets.token_urlsafe(48)}})
                    exported = _private_json(export_path)
                    client = exported['client']
                    expected = dict({k: challenge[k] for k in ('mesh_id', 'node_id', 'owner_binding',
                        'operation_id', 'nonce', 'expires_at')},
                        enrollment_sha256=document_sha256(bootstrap.document),
                        request_sha256=document_sha256(challenge),
                        **{k: settings[k] for k in ('task_id', 'task_epoch', 'entry_request_id')})
                    _require(client == {'format': 'mesh-enrollment-relay-client/1',
                        'resource_url': self.origin + '/enrollment/node', 'binding': expected},
                        'phone_owner_original_export_required')
                    config = {'format': OWNER_FORMAT, 'enabled': True, 'resource_url': client['resource_url'],
                        'binding': expected, 'max_request_bytes': 1024 * 1024,
                        'token_sha256': hashlib.sha256(exported['credential']['token'].encode()).hexdigest()}
                    if (root / 'relay.json').exists():
                        _require(_private_json(root / 'relay.json') == config,
                                 'phone_owner_relay_changed_or_revoked')
                    else:
                        _write_new_private(root / 'relay.json', config)
                    relay, relay_gate = self._relay(root, settings, bootstrap)
                    self._attach(relay, relay_gate)
                    return dict(base, status=bootstrap.status(operation), relay_loaded=True,
                                private_export='relay-export.json')
            # These call the incumbent Registry, which uses its own Store
            # transactions. Never hold a nested business writer transaction.
            relay, relay_gate = self._relay(root, settings, bootstrap)
            if action == 'verify':
                return dict(base, status=bootstrap.verify(operation))
            if action == 'register':
                state = bootstrap.status(operation)['registration_state']
                if state == 'not_registered':
                    bootstrap.register(operation)
                # attempted/unknown only queries the original publication.
                return dict(base, status=bootstrap.reconcile(operation))
            _require(set(settings['capabilities']) <= {bootstrap.config['challenge']['capability_id']},
                     'phone_owner_only_verified_capabilities')
            proposed_relays = self._relay_candidates(relay, relay_gate)
            issued_path = root / 'issued.json'
            if issued_path.exists():
                issued = _private_json(issued_path)
                # Revalidate fresh original oracle/registration/query without
                # rotating a token after a lost acknowledgement.
                enrollment = verified_enrollment(bootstrap, self.service.api.resources)
                checked = {'node_id': enrollment['node_id'], 'subject': settings['subject'],
                    'session_id': settings['session_id'], 'capabilities': settings['capabilities'],
                    'scope': ['phone:node', 'phone:updates'], 'enabled': True,
                    'token_sha256': hashlib.sha256(issued['bootstrap']['credential']['token'].encode()).hexdigest(),
                    'enrollment': enrollment, 'visible_nodes': settings['visible_nodes']}
                _require(checked == issued['node'] and issued['bootstrap']['enrollment'] == enrollment,
                         'phone_owner_issued_identity_changed')
            else:
                node, exported = issue_node(bootstrap, self.service.api.resources,
                    subject=settings['subject'], session_id=settings['session_id'],
                    capabilities=settings['capabilities'], resource_url=self.origin + '/phone/node')
                node['visible_nodes'] = settings['visible_nodes']
                if self.service.updates is not None:
                    exported['update_trust'] = self.service.updates.trust(settings['node_id'])
                issued = {'node': node, 'bootstrap': exported}
                _write_new_private(issued_path, issued)
            config = self.service._config()
            existing = [n for n in config['nodes'] if n['node_id'] == settings['node_id']]
            _require(not existing or existing == [issued['node']], 'phone_owner_node_changed_or_revoked')
            if not existing:
                config['nodes'].append(issued['node'])
            # Validate all candidates before the durable file and live maps are
            # changed; commit the three live views under the same owner lock.
            subjects, bindings = self.service.prepare_bindings(config)
            with gate.admission():
                self._live(gate, settings)
                atomic_private(self.service.config_path, config)
                self.service.sessions.subjects = subjects
                self.service.sessions.node_bindings = bindings
                self.service.installed = config
                self.service.relays = proposed_relays
            return dict(base, node_loaded=True, private_export='issued.json:bootstrap')


def install_dispatch(api, service, origin):
    """Wrap only exact operator routes; retain original dispatch for all others."""
    owner, previous = PhoneOwnerOperations(service, origin=origin), api.dispatch
    def dispatch(method, path, payload, peer):
        if path.split('?', 1)[0].startswith(PREFIX):
            action = path[len(PREFIX):]
            if method != 'POST' or action not in ACTIONS or peer.get('role') != 'operator':
                raise PermissionError('phone_owner_operator_only')
            return owner.handle(action, payload)
        return previous(method, path, payload, peer)
    owner.restore()
    api.dispatch = dispatch
    service.owner_operations = owner


def main(argv=None):
    parser = argparse.ArgumentParser(description='Thin private operator RPC; no local business Store')
    parser.add_argument('--config', required=True, help='existing private operator transport configuration')
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--enrollment-id')
    args = parser.parse_args(argv)
    try:
        _require((args.enrollment_id is None and args.action in ('plan', 'status')) or
                 (isinstance(args.enrollment_id, str) and bool(_ID.fullmatch(args.enrollment_id))),
                 'phone_owner_enrollment_id_invalid')
        from .worker import Client
        client = Client(_private_json(args.config))
        value = client.request(PREFIX + args.action,
                               {'enrollment_id': args.enrollment_id} if args.enrollment_id else {})
        print(json.dumps(value, sort_keys=True))
        return 0
    except Exception:
        # No transport exception text, private paths or tokens in stdout.
        print(json.dumps({'status': 'unconfirmed', 'automatic_replay': False,
                          'next_action': 'query status using the same enrollment ID; reconcile original operation'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
