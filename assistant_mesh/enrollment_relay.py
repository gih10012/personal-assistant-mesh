"""Narrow external interactive enrollment over the original bootstrap journal.

Not a Cloud membership, node/authorization database, listener or credential
issuer. A trusted owner supplies one existing Bootstrap and an independently
implemented live-task validator. Device JSON can only observe/admit its exact
original challenge and report its complete original result; it cannot verify,
register, grant, create a task, select adapters or replay an unknown execution.
"""
import hashlib
import hmac
import math
import time

from .cloud_http import make_http_handler as _json_http_handler, _resource
from .enrollment import (EnrollmentError, _encoded, _hash, _integer, _object, _text,
                         document_sha256)
from .enrollment_bootstrap import EnrollmentBootstrap, _private_json

OWNER_FORMAT = 'mesh-enrollment-relay-owner/1'
REQUEST_FORMAT = 'mesh-enrollment-relay-request/1'
RESPONSE_FORMAT = 'mesh-enrollment-relay-response/1'
RESOURCE_PATH = '/enrollment/node'
_BINDINGS = ('mesh_id', 'node_id', 'owner_binding', 'enrollment_sha256',
             'operation_id', 'request_sha256', 'nonce', 'expires_at',
             'task_id', 'task_epoch', 'entry_request_id')


def _require(value, code):
    if not value:
        raise EnrollmentError(code)


def _binding(value):
    binding = _object(value, _BINDINGS)
    for key in ('mesh_id', 'node_id', 'owner_binding', 'operation_id', 'task_id', 'entry_request_id'):
        _text(binding[key], 200)
    for key in ('enrollment_sha256', 'request_sha256', 'nonce'):
        _hash(binding[key])
    _integer(binding['expires_at'], 1)
    _integer(binding['task_epoch'], 1)
    return binding


def _owner(path):
    value = _private_json(path)
    _object(value, ('format', 'enabled', 'resource_url', 'token_sha256', 'binding', 'max_request_bytes'))
    _require(value['format'] == OWNER_FORMAT and type(value['enabled']) is bool,
             'enrollment_relay_owner_invalid')
    resource, unused = _resource(value['resource_url'])
    _require(resource == RESOURCE_PATH, 'enrollment_relay_exact_resource_required')
    _hash(value['token_sha256'])
    _integer(value['max_request_bytes'], 128 * 1024, 16 * 1024 * 1024)
    _binding(value['binding'])
    return value


class EnrollmentRelay:
    """Trusted, one-operation adapter; no new state or remote approval hooks.

    The live_task_validator(binding) is installed by owner CODE, not this JSON.
    It must independently observe current original task/epoch/request authority.
    Missing/rejected authority fails closed for new admission or first result.
    Original status and identical terminal-result readback remain observable.
    """
    def __init__(self, bootstrap, config_path, *, live_task_validator=None, clock=time.time):
        _require(isinstance(bootstrap, EnrollmentBootstrap), 'enrollment_relay_existing_bootstrap_required')
        _require(live_task_validator is None or callable(live_task_validator),
                 'enrollment_relay_validator_invalid')
        _require(callable(clock), 'enrollment_relay_clock_invalid')
        self.bootstrap, self.config_path = bootstrap, config_path
        self.config = _owner(config_path)
        self.binding = self.config['binding']
        self.validate_task, self.clock = live_task_validator, clock
        _require(bootstrap.document['runtime']['kind'] == 'interactive',
                 'enrollment_relay_interactive_node_required')
        self._receipt()  # Existing original intent must already be owner-created.

    def _current(self):
        current = _owner(self.config_path)
        _require(all(current[k] == self.config[k] for k in current if k != 'enabled'),
                 'enrollment_relay_original_owner_binding_changed')
        return current

    def _receipt(self):
        receipt = self.bootstrap.receipt(self.binding['operation_id'])
        challenge = receipt['challenge']
        _require(all(challenge[k] == self.binding[k] for k in (
            'mesh_id', 'node_id', 'owner_binding', 'operation_id', 'nonce', 'expires_at'))
            and receipt['enrollment_sha256'] == self.binding['enrollment_sha256']
            and document_sha256(challenge) == self.binding['request_sha256'],
            'enrollment_relay_original_challenge_changed')
        return receipt

    def _principal(self, token):
        current = self._current()
        try:
            valid = (isinstance(token, str) and 32 <= len(token.encode('utf8')) <= 4096
                     and all(33 <= ord(c) < 127 for c in token))
        except UnicodeError:
            valid = False
        if not valid or not hmac.compare_digest(hashlib.sha256(token.encode('utf8')).hexdigest(),
                                               current['token_sha256']):
            raise PermissionError('enrollment_relay_auth_required')
        return current

    def _write(self, receipt):
        _require(self._current()['enabled'] and receipt['owner_enabled'],
                 'enrollment_relay_owner_disabled')
        now = self.clock()
        _require(type(now) in (int, float) and math.isfinite(now)
                 and receipt['challenge']['created_at'] <= now < self.binding['expires_at'],
                 'enrollment_relay_original_challenge_expired')
        # Bootstrap observes disable/binding changes itself; journal admission
        # also checks the original authorization/runtime/challenge deadlines.
        self.bootstrap._enabled()
        if self.validate_task is None:
            raise PermissionError('enrollment_relay_live_task_validator_required')
        binding = {k: self.binding[k] for k in _BINDINGS}
        if self.validate_task(binding) is not True:
            raise PermissionError('enrollment_relay_original_task_not_live')

    def _response(self, action, receipt, **extra):
        return dict({'format': RESPONSE_FORMAT, 'binding': dict(self.binding),
            'action': action, 'status': receipt['status'],
            'may_execute_original_challenge': False,
            'automatic_retry': False, 'automatic_replay': False,
            'relay_performed_verification': False, 'relay_performed_registration': False,
            'managed_use_authorized': False, 'native_execution_attested': False,
            'native_features_restricted': False}, **extra)

    def handle(self, token, envelope):
        self._principal(token)
        _object(envelope, ('format', 'binding', 'action', 'arguments'))
        _require(envelope['format'] == REQUEST_FORMAT, 'enrollment_relay_request_format_invalid')
        _require(_binding(envelope['binding']) == self.binding,
                 'enrollment_relay_request_binding_mismatch')
        action = envelope['action']
        _require(action in ('challenge', 'result', 'status'), 'enrollment_relay_action_not_allowed')
        receipt = self._receipt()
        if action in ('challenge', 'status'):
            _object(envelope['arguments'], ())
        if action == 'status':
            return self._response(action, receipt)
        if action == 'challenge':
            if receipt['status']['state'] == 'intent':
                self._write(receipt)
                # Commit attempted BEFORE any challenge is returned. A lost
                # ACK means the next fetch is non-executable observation.
                admitted = self.bootstrap._journal().admit_external(
                    self.binding['operation_id'], self.binding['request_sha256'])
                receipt = self._receipt()
                return self._response(action, receipt, challenge=admitted['challenge'],
                    may_execute_original_challenge=admitted['fresh'])
            return self._response(action, receipt, challenge=receipt['challenge'])
        # The result body IS the existing result envelope, never a short summary
        # or a remotely selected verification/registration instruction.
        result = envelope['arguments']
        if receipt['result'] is not None:
            _require(_encoded(result) == _encoded(receipt['result']),
                     'enrollment_relay_original_result_conflict')
            return self._response(action, receipt, identical_existing_result=True)
        _require(receipt['status']['state'] in ('attempted', 'unknown'),
                 'enrollment_relay_original_admission_required')
        self._write(receipt)
        # Validate all original nonce/request/continuation/proxy and complete
        # result bytes with the existing Journal. No verifier/registrar runs.
        self.bootstrap._journal().record_result(self.binding['operation_id'], result, require_live=True)
        return self._response(action, self._receipt(), original_result_retained=True)


def make_http_handler(relay, *, trusted_proxy_peers=(), timeout=15):
    """Reuse existing strict JSON/TLS transport, not Cloud membership logic.

    Returns a handler only; no socket, listener, TLS certificate or key is made.
    Root may later expose this ONE route through its authenticated TLS gateway.
    """
    _require(isinstance(relay, EnrollmentRelay), 'enrollment_relay_facade_required')
    return _json_http_handler(relay, relay.config['resource_url'],
        trusted_proxy_peers=trusted_proxy_peers,
        max_request_bytes=relay.config['max_request_bytes'], timeout=timeout)
