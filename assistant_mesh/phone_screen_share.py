"""Private latest screen frame on the incumbent phone/Allocation authority.

No listener, Store, schema, task, grant, frame journal or import-time hook.
Opaque wire bindings correlate an already running read-only dispatch; they
never authorize it. Local owner UI approval IDs/nonces are not wire fields.
"""
import base64
from dataclasses import dataclass, field
import hashlib
import json
import math

from .ingress import _identifier
from .store import Conflict


ACTIONS = frozenset('mesh_screen_share_' + name for name in ('open', 'frame', 'status', 'close'))
BINDING_FIELDS = frozenset(('primary_session_id', 'session_id', 'stream_id', 'task_id', 'task_epoch',
                            'operation_id', 'capability_id', 'binding_digest', 'expires_at'))
FRAME_FIELDS = frozenset(('revision', 'capture_timestamp', 'width', 'height', 'bytes', 'sha256', 'base64'))
MAX_JSON = 65536
MAX_FRAME = 42000
IDLE_TTL = 30
MAX_SCOPE_TTL = 300


def _encoded(value):
    try:
        raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('screen_share_json_required') from None
    if len(raw) > MAX_JSON:
        raise ValueError('screen_share_json_budget')
    return raw


def _digest(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def stream_policy(body):
    if (not isinstance(body, dict) or set(body) != {'format', 'command', 'duration_ms', 'max_fps'}
            or body['format'] != 'amita-phone-screen-share/1' or body['command'] != 'screen_share'
            or type(body['duration_ms']) is not int or not 1000 <= body['duration_ms'] <= 300000
            or type(body['max_fps']) is not int or not 1 <= body['max_fps'] <= 2):
        raise PermissionError('screen_share_dedicated_stream_workload_required')
    return body


@dataclass(repr=False)
class _Slot:
    binding: dict = field(repr=False)
    idle_until: float
    max_fps: int
    active: bool = True
    last_frame_at: float = 0
    frame: dict = field(default_factory=dict, repr=False)
    body: bytes = field(default=b'', repr=False)


class PhoneScreenShare:
    def __init__(self, service):
        self.service = service
        # At most one bounded frame per configured node (configuration <=32).
        # A closed slot retains only its public binding until its short expiry.
        self._slots = {}

    def _binding(self, arguments, *, live=True):
        binding = {key: arguments[key] for key in BINDING_FIELDS}
        for key in BINDING_FIELDS - {'task_epoch', 'expires_at', 'binding_digest'}:
            _identifier(binding[key], 'screen_share_' + key)
        now = self.service.store.clock()
        if (type(binding['task_epoch']) is not int or not 0 < binding['task_epoch'] < 2 ** 53
                or not _digest(binding['binding_digest']) or not _finite(binding['expires_at'])
                or binding['expires_at'] <= 0
                or live and not now < binding['expires_at'] <= now + MAX_SCOPE_TTL):
            raise ValueError('screen_share_original_binding_required')
        return binding

    def _current_node(self, node_id):
        current = self.service._config()
        installed = next((node for node in self.service.installed['nodes'] if node['node_id'] == node_id), None)
        node = next((node for node in current['nodes'] if node['node_id'] == node_id), None)
        if (installed is None or node is None or not current['enabled'] or not node['enabled']
                or any(node[key] != installed[key] for key in node if key != 'enabled')):
            raise PermissionError('screen_share_owner_binding_required')
        return node

    def _guard(self, db, node_id, binding, reader=None):
        if binding['expires_at'] <= self.service.store.clock():
            raise Conflict('screen_share_scope_expired')
        node = self._current_node(node_id)
        adapter = node.get('installed_control_adapter')
        if (adapter is None or not adapter['enabled'] or not adapter.get('screen_share_enabled', False)
                or 'inspect' not in adapter['actions']
                or binding['primary_session_id'] != node['session_id']
                or binding['session_id'] != node.get('control_session_id')
                or binding['capability_id'] != adapter['capability_id']):
            raise PermissionError('screen_share_installed_read_only_adapter_required')
        selected = self.service.control.select(node, binding['session_id'])
        self.service._presence(db, selected, live=True, capability=binding['capability_id'])
        allocations = self.service.api.allocations
        allocation = allocations._allocation(db, binding['operation_id'])
        # The original requesting worker alone reads pixels. Operator/provider
        # roles are not a substitute for this exact authenticated allocation actor.
        if reader is not None and reader != allocation['actor']:
            raise PermissionError('screen_share_original_reader_required')
        dispatch = allocations._dispatch(db, binding['operation_id'], binding['capability_id'], 'node:' + node_id)
        if (allocation['task_id'] != binding['task_id'] or allocation['task_epoch'] != binding['task_epoch']
                or allocation['state'] != 'running' or dispatch['state'] != 'running'
                or dispatch['receipt_id'] is None or binding['expires_at'] > allocation['deadline']):
            raise Conflict('screen_share_original_running_dispatch_required')
        # Reuses current task owner/epoch/lease, exact approved Registry grant,
        # capability epoch/revocation, pools and original resolution in this DB lock.
        allocations._admission(db, allocation, self.service.store.clock())
        selection = next((item for item in json.loads(allocation['plan'])['selections']
                          if item['capability_id'] == binding['capability_id']), None)
        if selection is None or selection['action'] != 'inspect':
            raise PermissionError('screen_share_read_only_selection_required')
        self.service.control.validate_selection(selected, selection)
        body = json.loads(base64.b64decode(selection['workload']['input']['base64'], validate=True))
        return stream_policy(body)

    def _expire(self):
        now = self.service.store.clock()
        for node_id, slot in list(self._slots.items()):
            if slot.binding['expires_at'] <= now:
                del self._slots[node_id]
            elif slot.idle_until <= now:
                slot.active, slot.body, slot.frame = False, b'', {}

    @staticmethod
    def _public(binding):
        # All fields are public correlation data; no owner approval/nonce/grant.
        return {key: binding[key] for key in ('stream_id', 'task_id', 'task_epoch', 'operation_id',
                                             'capability_id', 'binding_digest', 'expires_at')}

    def _reply(self, slot):
        return dict(self._public(slot.binding), format='amita-phone-screen-share-ack/1',
                    active=slot.active, acknowledged=bool(slot.frame),
                    frame_revision=slot.frame.get('revision', 0), frame_sha256=slot.frame.get('sha256', ''),
                    bytes=slot.frame.get('bytes', 0))

    def _close(self, db, node_id, binding):
        """Privacy cleanup only, not operation admission or a frame reader.

        Authenticated producer and immutable historical allocation identity
        suffice to erase its exact old slot after task/grant/presence expiry.
        Never erase a replacement binding or create a slot, lease or receipt.
        """
        node = self._current_node(node_id)
        adapter = node.get('installed_control_adapter')
        if (adapter is None or binding['primary_session_id'] != node['session_id']
                or binding['session_id'] != node.get('control_session_id')
                or binding['capability_id'] != adapter['capability_id']):
            raise PermissionError('screen_share_original_producer_required')
        allocations = self.service.api.allocations
        allocation = allocations._allocation(db, binding['operation_id'])
        allocations._dispatch(db, binding['operation_id'], binding['capability_id'], 'node:' + node_id)
        if allocation['task_id'] != binding['task_id'] or allocation['task_epoch'] != binding['task_epoch']:
            raise PermissionError('screen_share_original_producer_required')
        slot = self._slots.get(node_id)
        if slot is not None and slot.binding == binding:
            slot.active, slot.body, slot.frame = False, b'', {}
            return self._reply(slot)
        # Exact old binding is already absent. Report only its absence without
        # exposing or deleting another currently active stream's public state.
        return self._reply(_Slot(binding, self.service.store.clock(), 1, active=False))

    def request(self, node, envelope):
        _encoded(envelope)
        if (not isinstance(envelope, dict) or set(envelope) != {'action', 'arguments'}
                or envelope['action'] not in ACTIONS or not isinstance(envelope['arguments'], dict)):
            raise ValueError('screen_share_request_required')
        action, args = envelope['action'], envelope['arguments']
        if set(args) != BINDING_FIELDS | ({'frame'} if action == 'mesh_screen_share_frame' else set()):
            raise ValueError('screen_share_arguments_required')
        with self.service.lock:
            self._expire()
            if action == 'mesh_screen_share_close':
                binding = self._binding(args, live=False)
                with self.service.store.transaction() as db:
                    return self._close(db, node['node_id'], binding)
            try:
                binding = self._binding(args)
                with self.service.store.transaction() as db:
                    policy = self._guard(db, node['node_id'], binding)
                    slot = self._slots.get(node['node_id'])
                    if action == 'mesh_screen_share_open':
                        if slot is not None and slot.binding != binding and slot.active:
                            try:
                                self._guard(db, node['node_id'], slot.binding)
                            except (PermissionError, Conflict, ValueError):
                                del self._slots[node['node_id']]
                                slot = None
                            else:
                                raise Conflict('screen_share_node_slot_busy')
                        if slot is None or slot.binding != binding:
                            if binding['expires_at'] > self.service.store.clock() + policy['duration_ms'] / 1000:
                                raise ValueError('screen_share_duration_exceeded')
                            slot = _Slot(binding, min(binding['expires_at'], self.service.store.clock() + IDLE_TTL),
                                         policy['max_fps'])
                            self._slots[node['node_id']] = slot
                    if slot is None or slot.binding != binding or not slot.active:
                        raise Conflict('screen_share_original_stream_not_live')
                    if action == 'mesh_screen_share_frame':
                        frame, raw = self._frame(args['frame'])
                        if slot.frame and frame['revision'] <= slot.frame['revision']:
                            if frame != slot.frame or raw != slot.body:
                                raise Conflict('screen_share_frame_revision_conflict')
                        else:
                            if slot.frame and frame['capture_timestamp'] < slot.frame['capture_timestamp']:
                                raise Conflict('screen_share_capture_timestamp_reversed')
                            if slot.frame and self.service.store.clock() - slot.last_frame_at < 1 / slot.max_fps:
                                raise Conflict('screen_share_frame_rate_exceeded')
                            slot.frame, slot.body = frame, raw
                            slot.last_frame_at = self.service.store.clock()
                    slot.idle_until = min(binding['expires_at'], self.service.store.clock() + IDLE_TTL)
                    return self._reply(slot)
            except (PermissionError, Conflict):
                # Invalid original fences never leave readable pixels behind.
                # Reader authorization failures below must not evict another
                # caller's legitimate active stream.
                existing = self._slots.get(node['node_id'])
                if existing is not None:
                    try:
                        with self.service.store.transaction() as db:
                            self._guard(db, node['node_id'], existing.binding)
                    except (PermissionError, Conflict, ValueError):
                        del self._slots[node['node_id']]
                raise

    @staticmethod
    def _frame(value):
        if (not isinstance(value, dict) or set(value) != FRAME_FIELDS
                or type(value['revision']) is not int or not 0 < value['revision'] < 2 ** 53
                or not _finite(value['capture_timestamp']) or value['capture_timestamp'] < 0
                or any(type(value[key]) is not int or not 0 < value[key] <= 640 for key in ('width', 'height'))
                or type(value['bytes']) is not int or not 4 <= value['bytes'] <= MAX_FRAME
                or not _digest(value['sha256']) or not isinstance(value['base64'], str)
                or len(value['base64']) > 56000):
            raise ValueError('screen_share_frame_required')
        try:
            raw = base64.b64decode(value['base64'], validate=True)
        except ValueError:
            raise ValueError('screen_share_frame_encoding_required') from None
        if (len(raw) != value['bytes'] or hashlib.sha256(raw).hexdigest() != value['sha256']
                or base64.b64encode(raw).decode('ascii') != value['base64']
                or not raw.startswith(b'\xff\xd8\xff') or not raw.endswith(b'\xff\xd9')):
            raise ValueError('screen_share_frame_hash_or_jpeg_required')
        return {key: value[key] for key in FRAME_FIELDS - {'base64'}}, raw

    def read(self, actor, arguments):
        """Called only by the original API's authenticated operator/worker seam."""
        _encoded(arguments)
        if not isinstance(arguments, dict) or set(arguments) != BINDING_FIELDS:
            raise ValueError('screen_share_reader_arguments_required')
        with self.service.lock:
            self._expire()
            binding = self._binding(arguments)
            node = next((item for item in self.service.installed['nodes']
                         if item.get('installed_control_adapter', {}).get('capability_id') == binding['capability_id']), None)
            if node is None:
                raise PermissionError('screen_share_installed_read_only_adapter_required')
            slot = self._slots.get(node['node_id'])
            with self.service.store.transaction() as db:
                try:
                    self._guard(db, node['node_id'], binding)
                except (PermissionError, Conflict, ValueError):
                    self._slots.pop(node['node_id'], None)
                    raise
                self._guard(db, node['node_id'], binding, reader=actor)
                if slot is None or slot.binding != binding or not slot.active or not slot.frame:
                    raise Conflict('screen_share_frame_not_available')
                # No provider/owner UI credentials, prior images, history or URLs.
                result = dict(self._public(binding), format='amita-phone-screen-share-frame/1',
                              frame=dict(slot.frame, base64=base64.b64encode(slot.body).decode('ascii')))
                _encoded(result)
                return result
