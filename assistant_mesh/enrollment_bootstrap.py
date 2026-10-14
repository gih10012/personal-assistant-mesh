"""Thin owner-installed bootstrap tools over the existing enrollment contract.

The protocol is environment neutral. This CLI/journal is a POSIX reference;
node adapters may use any language and transport. Owner config selects an
already reviewed adapter, independent oracle and exact identity. Model tool
arguments cannot install commands, choose auth, widen grants or retry unknowns.
No Store, production node, network listener or credential provisioning occurs.
Native Shell/network/features are not intercepted. Adapter code is trusted,
not a sandbox or proof of remote execution/side-effect closure.
"""
import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

from .enrollment import (EnrollmentError, EnrollmentJournal, MAX_ARTIFACT_BYTES,
                         _encoded, _private_directory, document_sha256,
                         validate_adaptation_report, validate_enrollment)
from .provider_runtime import _owner_bytes, _unique_object


_FORMAT = 'mesh-bootstrap/1'
_MAX_REPLY = 16 * 1024 * 1024
_COMMAND = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,79}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')


class BootstrapError(ValueError):
    """Fixed error categories; no private paths, argv, stderr or credentials."""


def _require(condition, category):
    if not condition:
        raise BootstrapError(category)


def _json(raw):
    def invalid_number(value):
        raise ValueError('non_finite_json')
    try:
        return json.loads(raw.decode('utf8'), object_pairs_hook=_unique_object,
                          parse_constant=invalid_number)
    except Exception:
        raise BootstrapError('bootstrap_json_invalid') from None


def _private_json(path, maximum=128 * 1024):
    try:
        return _json(_owner_bytes(path, maximum, 'bootstrap_private_file_unavailable'))
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError('bootstrap_private_file_unavailable') from None


def _text(value, maximum=200):
    _require(isinstance(value, str) and 0 < len(value) <= maximum
             and all(ord(char) >= 32 and not 0xd800 <= ord(char) <= 0xdfff for char in value),
             'bootstrap_text_invalid')
    return value


def _path(value):
    _text(value, 4096)
    path = Path(value)
    _require(path.is_absolute() and '..' not in path.parts, 'bootstrap_path_invalid')
    return str(path)


def _integer(value, minimum, maximum):
    _require(type(value) is int and minimum <= value <= maximum, 'bootstrap_integer_invalid')
    return value


def _pinned_file(value):
    _require(isinstance(value, dict) and set(value) == {'path', 'sha256'},
             'bootstrap_pin_invalid')
    _path(value['path'])
    _require(isinstance(value['sha256'], str) and _HASH.fullmatch(value['sha256']) is not None,
             'bootstrap_pin_invalid')


def _manifest(path):
    value = _private_json(path)
    required = {'schema', 'enabled', 'authority_kind', 'state_dir', 'enrollment',
                'authorization', 'verifier_id', 'adapter', 'challenge', 'inventory'}
    _require(isinstance(value, dict) and set(value) == required
             and type(value['schema']) is int and value['schema'] == 1,
             'bootstrap_owner_config_invalid')
    _require(type(value['enabled']) is bool and value['authority_kind'] in ('owner', 'synthetic'),
             'bootstrap_owner_config_invalid')
    _path(value['state_dir'])
    value['enrollment'] = validate_enrollment(value['enrollment'])
    _text(value['verifier_id'])
    adapter = value['adapter']
    _require(isinstance(adapter, dict) and set(adapter) == {'argv', 'pins', 'timeout_seconds'},
             'bootstrap_adapter_invalid')
    _require(isinstance(adapter['argv'], list) and 1 <= len(adapter['argv']) <= 16
             and isinstance(adapter['pins'], list) and 1 <= len(adapter['pins']) <= 16,
             'bootstrap_adapter_invalid')
    _path(adapter['argv'][0])
    for argument in adapter['argv']:
        _text(argument, 4096)
    for pin in adapter['pins']:
        _pinned_file(pin)
    pins = [pin['path'] for pin in adapter['pins']]
    _require(len(pins) == len(set(pins)) and adapter['argv'][0] in pins,
             'bootstrap_adapter_pins_invalid')
    # All file argv items are pinned; flags/numbers are fixed owner arguments.
    _require(all(not Path(arg).is_absolute() or arg in pins for arg in adapter['argv']),
             'bootstrap_adapter_unpinned_path')
    _integer(adapter['timeout_seconds'], 1, 60)
    challenge = value['challenge']
    _require(isinstance(challenge, dict) and set(challenge) == {'operation_id', 'capability_id', 'action',
             'revision', 'ttl_seconds', 'input', 'oracle', 'proxy'}, 'bootstrap_challenge_invalid')
    _text(challenge['operation_id'])
    _text(challenge['capability_id'])
    _text(challenge['action'])
    _integer(challenge['revision'], 1, 9007199254740991)
    _integer(challenge['ttl_seconds'], 1, 3600)
    _pinned_file(challenge['input'])
    _pinned_file(challenge['oracle'])
    inventory = value['inventory']
    _require(isinstance(inventory, dict) and set(inventory) == {'commands', 'network_metadata',
             'harness', 'candidates'}, 'bootstrap_inventory_invalid')
    _require(isinstance(inventory['commands'], list) and len(inventory['commands']) <= 64
             and all(isinstance(name, str) and _COMMAND.fullmatch(name) is not None
                     for name in inventory['commands'])
             and len(set(inventory['commands'])) == len(inventory['commands'])
             and type(inventory['network_metadata']) is bool, 'bootstrap_inventory_invalid')
    harness = inventory['harness']
    _require(isinstance(harness, dict) and set(harness) == {'name', 'version', 'command'},
             'bootstrap_harness_invalid')
    _text(harness['name'])
    _text(harness['version'])
    _require(harness['command'] is None or harness['command'] in inventory['commands'],
             'bootstrap_harness_invalid')
    _require(isinstance(inventory['candidates'], list) and len(inventory['candidates']) <= 256,
             'bootstrap_candidates_invalid')
    seen = set()
    for candidate in inventory['candidates']:
        _require(isinstance(candidate, dict) and set(candidate) == {'id', 'class', 'revision',
                 'actions', 'scope', 'resource_pool', 'fault_domain', 'source_device',
                 'proxy_binding'}, 'bootstrap_candidate_invalid')
        _text(candidate['id'])
        _text(candidate['class'])
        _integer(candidate['revision'], 1, 9007199254740991)
        _require(candidate['id'] not in seen, 'bootstrap_candidate_duplicate')
        seen.add(candidate['id'])
        _require(isinstance(candidate['scope'], dict), 'bootstrap_candidate_invalid')
        # Owner declarative metadata is private, never an authorization/evidence claim.
        _encoded(candidate['scope'], 32768)
        from .resources import _safe_metadata
        _, redacted = _safe_metadata(candidate['scope'])
        _require(not redacted, 'bootstrap_scope_contains_private_metadata')
        for key in ('resource_pool', 'fault_domain', 'source_device', 'proxy_binding'):
            if candidate[key] is not None:
                _text(candidate[key], 512)
        _require((candidate['source_device'] is None) == (candidate['proxy_binding'] is None),
                 'bootstrap_candidate_proxy_invalid')
    # Reuse the report validator for action/revision shape; no capability promotion.
    _report(value, system='not_observed', version='not_observed', harness_available=False)
    # Reuse exactly the original pure owner-binding validator, not its journal
    # constructor. Even plan on a missing installation must reject a different
    # enrollment/authorization SHA; no file or native feature is consulted here.
    checker = EnrollmentJournal.__new__(EnrollmentJournal)
    checker._configure(value['enrollment'], value['authorization'], value['verifier_id'], time.time)
    return value


def _report(config, system, version, harness_available):
    document, inventory = config['enrollment'], config['inventory']
    harness = inventory['harness']
    return validate_adaptation_report({'format': 'mesh-adaptation-report/1',
        'mesh_id': document['mesh_id'], 'node_id': document['node_id'],
        'system': {'name': system, 'version': version},
        'harness': {'name': harness['name'], 'version': harness['version'],
                    'available': harness_available},
        'storage': {'mode': 'mesh' if document['runtime']['kind'] == 'temporary' else 'both',
                    'state_ref': document['runtime']['state_ref']},
        'capabilities': [{key: item[key] for key in ('id', 'revision', 'actions')}
                         for item in inventory['candidates']],
        'gaps': ['Command presence is not invocation, model access or tool execution proof.',
                 'Join transport is not a business egress grant; native networking remains available.',
                 'No device-control, throughput, shared-capacity or failure-domain attestation.'],
        'native_features_restricted': False})


def _private_blob(pin, maximum):
    try:
        raw = _owner_bytes(pin['path'], maximum, 'bootstrap_private_file_unavailable')
        _require(hashlib.sha256(raw).hexdigest() == pin['sha256'], 'bootstrap_input_or_oracle_changed')
        return raw
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError('bootstrap_private_file_unavailable') from None


def _adapter_bytes(pin):
    """Validate current direct executable/script bytes, not its dependency closure."""
    path = Path(pin['path'])
    try:
        for parent in path.parents:
            item = parent.lstat()
            _require(stat.S_ISDIR(item.st_mode) and item.st_uid in (0, os.getuid())
                     and (not item.st_mode & 0o022 or item.st_mode & stat.S_ISVTX),
                     'bootstrap_adapter_path_unsafe')
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            before = os.fstat(source.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_uid in (0, os.getuid())
                     and not before.st_mode & 0o022 and before.st_size <= 64 * 1024 * 1024,
                     'bootstrap_adapter_path_unsafe')
            raw = source.read(64 * 1024 * 1024 + 1)
            after = os.fstat(source.fileno())
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        _require(all(getattr(before, key) == getattr(after, key) == getattr(path.lstat(), key)
                     for key in fields) and hashlib.sha256(raw).hexdigest() == pin['sha256'],
                 'bootstrap_adapter_changed')
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError('bootstrap_adapter_unavailable') from None


def _write_new_private(path, value):
    raw = _encoded(value)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())
    parent = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


class StdioAdapter:
    """One fixed argv process; bounded bytes/time, no automatic retries."""
    def __init__(self, adapter, cwd):
        self.config, self.cwd, self.last_process = adapter, cwd, None

    def check(self):
        for pin in self.config['pins']:
            _adapter_bytes(pin)
        _require(os.access(self.config['argv'][0], os.X_OK), 'bootstrap_adapter_not_executable')

    def __call__(self, challenge):
        self.check()
        request = _encoded(challenge, 128 * 1024) + b'\n'
        # Do not copy auth/proxy/HOME/CODEX_HOME values to a node transport.
        # This is child-only configuration, not a restriction on native agents.
        environment = {key: os.environ[key] for key in
                       ('LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR', 'TMPDIR', 'TMP', 'TEMP')
                       if key in os.environ}
        process, selector = None, selectors.DefaultSelector()
        output, errors, sent = bytearray(), bytearray(), 0
        started = time.monotonic()
        deadline = started + min(self.config['timeout_seconds'],
                                 max(0, challenge['expires_at'] - time.time()))
        try:
            _require(deadline > started, 'bootstrap_challenge_expired_before_process')
            process = subprocess.Popen(self.config['argv'], cwd=str(self.cwd),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=environment, shell=False, start_new_session=True)
            for pipe in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
            selector.register(process.stdout, selectors.EVENT_READ, 'output')
            selector.register(process.stderr, selectors.EVENT_READ, 'error')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                _require(remaining > 0, 'bootstrap_adapter_timeout_unknown')
                for key, _ in selector.select(remaining):
                    if key.data == 'input':
                        try:
                            sent += os.write(key.fd, request[sent:sent + 65536])
                        except BlockingIOError:
                            continue
                        if sent == len(request):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        limit = _MAX_REPLY if key.data == 'output' else 32768
                        target = output if key.data == 'output' else errors
                        try:
                            chunk = os.read(key.fd, min(65536, limit + 1 - len(target)))
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        else:
                            target.extend(chunk)
                            _require(len(target) <= limit, 'bootstrap_adapter_output_too_large_unknown')
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
            _require(process.returncode == 0, 'bootstrap_adapter_failed_unknown')
            return _json(bytes(output))
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    # Only this owned process group; termination is not remote
                    # side-effect closure and the journal still remains unknown.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if not pipe.closed:
                        pipe.close()
                self.last_process = {'direct_process_pid': process.pid,
                    'direct_process_returncode': process.returncode, 'direct_process_reaped': True,
                    'stdout_bytes': len(output), 'stderr_bytes': len(errors),
                    'elapsed_seconds': time.monotonic() - started,
                    'remote_effect_closure_verified': False, 'native_execution_attested': False}


class EnrollmentBootstrap:
    """Trusted owner facade; hooks are never loaded from model/node JSON."""
    def __init__(self, config_path, *, connection_observer=None, registrar=None,
                 registration_observer=None):
        self.config_path = config_path
        self.config = _manifest(config_path)
        self.document = self.config['enrollment']
        self.root = Path(self.config['state_dir'])
        self.connection_observer = connection_observer
        self.registrar, self.registration_observer = registrar, registration_observer
        self.adapter = StdioAdapter(self.config['adapter'], self.root)
        self.binding = {'format': _FORMAT,
            'enrollment_sha256': document_sha256(self.document),
            'authorization_sha256': document_sha256(self.config['authorization']),
            'verifier_id': self.config['verifier_id'], 'authority_kind': self.config['authority_kind'],
            'recipe_sha256': document_sha256({'adapter': self.config['adapter'],
                                            'challenge': self.config['challenge']})}

    def _binding(self):
        saved = _private_json(self.root / 'bootstrap-binding.json')
        _require(saved == self.binding, 'bootstrap_original_binding_changed')

    def _journal(self, readonly=False, initialize=False):
        self._binding()
        if readonly:
            return EnrollmentJournal.open_readonly(self.root, self.document,
                self.config['authorization'], self.config['verifier_id'])
        return EnrollmentJournal(self.root, self.document, self.config['authorization'],
                                 self.config['verifier_id'], initialize=initialize)

    def _owner_enabled(self):
        # A long-lived local facade must observe owner disable, not retain a
        # cached enable flag. Other execution bindings cannot silently change.
        current = _manifest(self.config_path)
        _require(all(current[key] == self.config[key] for key in
                     ('authority_kind', 'state_dir', 'enrollment', 'authorization',
                      'verifier_id', 'adapter', 'challenge')), 'bootstrap_original_binding_changed')
        return current['enabled']

    def _enabled(self):
        _require(self._owner_enabled(), 'bootstrap_owner_disabled')

    def _operation(self, operation_id):
        _require(operation_id == self.config['challenge']['operation_id'],
                 'bootstrap_original_operation_required')

    def bootstrap(self):
        # Explicit initialization only. No challenge, adapter invocation or registration.
        _private_directory(self.root)
        path = self.root / 'bootstrap-binding.json'
        created = False
        try:
            _write_new_private(path, self.binding)
            created = True
        except FileExistsError:
            self._binding()
        self._journal(initialize=created)
        return dict(self.plan(), initialized=True, new_execution_attempted=False)

    def plan(self):
        exists = (self.root / 'bootstrap-binding.json').exists()
        if exists:
            self._binding()
            self._journal(readonly=True)
        return {'format': 'mesh-bootstrap-plan/1', 'authority_kind': self.config['authority_kind'],
            'plan_configuration_only': True, 'live_task_or_device_authority_verified': False,
            'initialized': exists, 'owner_enabled': self.config['enabled'],
            'enrollment': self.document, 'binding': self.binding,
            'runtime_requirement': 'implement the same JSON contract using available native tools',
            'required_platform_or_language': None,
            'this_control_plane_reference': {'language': 'Python', 'storage': 'POSIX-private-files/SQLite'},
            'authorized_challenge_actions': self.config['authorization']['capabilities'],
            'candidate_instances': [dict(item, verification='declared', available_verified=False)
                                    for item in self.config['inventory']['candidates']],
            'steps': ['inventory: inspect metadata, do not execute unknown wrappers',
                      'bootstrap: bind existing owner identity and durable state',
                      'challenge: preserve one explicit original operation ID',
                      'run: one owner-pinned adapter attempt; unknown only observes',
                      'verify: compare complete bytes with the separate owner oracle',
                      'register: separate trusted registrar, never auto-grant business use',
                      'receipt/status: read original full evidence after expiry or disconnection'],
            'join_transport_is_business_egress_grant': False,
            'native_features_restricted': False, 'managed_use_authorized': False,
            'production_node_registered': False, 'automatic_retry': False,
            'rejoin': {'preserve': ['mesh_id', 'node_id', 'owner_binding', 'continuation',
                                   'operation_id', 'nonce', 'original_result', 'revocations', 'unknowns'],
                      'native_repair_and_alternative_routes_allowed': True,
                      'new_route_or_peer_trust_automatically_granted': False}}

    def inventory(self):
        inventory = self.config['inventory']
        present = {name: shutil.which(name) is not None for name in inventory['commands']}
        command = inventory['harness']['command']
        report = _report(self.config, platform.system() or 'unknown', platform.release() or 'unknown',
                         bool(command and present[command]))
        network = None
        if inventory['network_metadata']:
            from .network_inventory import inspect_network
            network = inspect_network(self.document['node_id'])
        return {'format': 'mesh-bootstrap-inventory/1', 'adaptation_report': report,
            'commands': [{'name': name, 'present': present[name],
                          'evidence': 'path_presence_only', 'invocation_performed': False}
                         for name in inventory['commands']],
            'network_metadata': network, 'candidate_instances': self.plan()['candidate_instances'],
            'physical_neighborhood_scanned': False, 'device_connections_verified': False,
            'registered_capabilities': False, 'native_features_restricted': False}

    def record_adaptation(self, report):
        self._enabled()
        return self._journal().record_adaptation(report)

    def challenge(self, operation_id):
        self._operation(operation_id)
        self._enabled()
        recipe = self.config['challenge']
        return self._journal().intent(operation_id, recipe['capability_id'], recipe['action'],
            _private_blob(recipe['input'], 64 * 1024), _private_blob(recipe['oracle'], MAX_ARTIFACT_BYTES),
            revision=recipe['revision'], ttl_seconds=recipe['ttl_seconds'], proxy=recipe['proxy'])

    def run(self, operation_id):
        self._operation(operation_id)
        self._enabled()
        journal = self._journal()
        status = journal.inspect(operation_id)
        if status['state'] == 'intent':
            # A bad installation is pre-admission, not an unknown execution.
            self.adapter.check()
        result = journal.execute(operation_id, self.adapter)
        return {'status': result, 'process_observation': self.adapter.last_process,
                'authority_kind': self.config['authority_kind']}

    def verify(self, operation_id):
        self._operation(operation_id)
        self._enabled()
        journal = self._journal()
        status = journal.inspect(operation_id)
        if status['verification'] == 'challenge_bytes_verified':
            # Original verification is an observation. Do not run a second
            # device connection challenge or imply its expiry was renewed.
            return status
        return journal.verify(operation_id,
            _private_blob(self.config['challenge']['oracle'], MAX_ARTIFACT_BYTES),
            connection_observer=self.connection_observer)

    def record_result(self, operation_id, envelope):
        self._operation(operation_id)
        # Original late result is observation, not a new execution/grant.
        return self._journal().record_result(operation_id, envelope)

    def register(self, operation_id):
        self._operation(operation_id)
        self._enabled()
        _require(callable(self.registrar), 'bootstrap_registrar_not_configured')
        return self._journal().register(operation_id, self.registrar)

    def reconcile(self, operation_id):
        self._operation(operation_id)
        _require(callable(self.registration_observer), 'bootstrap_registration_observer_not_configured')
        return self._journal().reconcile_registration(operation_id, self.registration_observer)

    def status(self, operation_id):
        self._operation(operation_id)
        status = self._journal(readonly=True).inspect(operation_id)
        enabled = self._owner_enabled()
        return dict(status, owner_enabled=enabled,
                    registered_candidate_fresh=status['registered_candidate_fresh'] and enabled)

    def receipt(self, operation_id):
        self._operation(operation_id)
        receipt = self._journal(readonly=True).receipt(operation_id)
        enabled = self._owner_enabled()
        receipt['status'] = dict(receipt['status'], owner_enabled=enabled,
            registered_candidate_fresh=receipt['status']['registered_candidate_fresh'] and enabled)
        return dict(receipt, bootstrap_binding=self.binding, owner_enabled=enabled)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise BootstrapError('bootstrap_arguments_invalid')


def main(argv=None):
    parser = _Parser(description='Owner-installed enrollment bootstrap; no native restrictions')
    parser.add_argument('--config', required=True)
    parser.add_argument('action', choices=('bootstrap', 'plan', 'inventory', 'record-adaptation',
                                         'challenge', 'run', 'verify', 'status', 'receipt',
                                         'record-result', 'register', 'reconcile'))
    parser.add_argument('--operation-id')
    parser.add_argument('--document-file')
    try:
        args = parser.parse_args(argv)
        operation_actions = {'challenge', 'run', 'verify', 'status', 'receipt',
                             'record-result', 'register', 'reconcile'}
        _require((args.action in operation_actions) == (args.operation_id is not None),
                 'bootstrap_operation_argument_invalid')
        _require((args.action in ('record-adaptation', 'record-result')) == (args.document_file is not None),
                 'bootstrap_document_argument_invalid')
        tool = EnrollmentBootstrap(args.config)
        if args.action in ('record-adaptation', 'record-result'):
            document = _private_json(args.document_file, _MAX_REPLY)
            output = (tool.record_result(args.operation_id, document) if args.action == 'record-result'
                      else tool.record_adaptation(document))
        else:
            method = getattr(tool, args.action)
            output = method(args.operation_id) if args.action in operation_actions else method()
        print(json.dumps(output, ensure_ascii=True, sort_keys=True, allow_nan=False))
        return 0
    except (BootstrapError, EnrollmentError) as error:
        output = {'status': 'error', 'category': str(error), 'automatic_retry': False,
                  'native_features_restricted': False, 'production_node_registered': False}
    except Exception:
        output = {'status': 'error', 'category': 'bootstrap_reference_failed', 'automatic_retry': False,
                  'native_features_restricted': False, 'production_node_registered': False}
    print(json.dumps(output, sort_keys=True))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
