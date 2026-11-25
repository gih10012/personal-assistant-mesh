"""Dispatch and observe one owner-approved, finite phone screen share."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError
import uuid

from .config import private_json
from .enrollment import _private_directory
from .ingress import _identifier
from .phone_control import CONTRACT
from .phone_frame_client import read_frame
from .phone_owner import atomic_private
from .phone_screen_share_reader import LOOKUP_FIELDS, METRIC, VERIFIER
from . import phone_share_request as share_request
from .worker import Client
from .worker_claim_receipts import CLAIM, FORMAT as CLAIM_FORMAT, RECEIPT, confirm, exact_claim_id

FORMAT = 'amita-phone-share-acceptance/1'
DIMENSION = 'screen.read.concurrent'
CAPACITY = {'capacity': 1, 'unit': 'session'}
TASK_INPUT = 'Owner-operated acceptance of one finite read-only AMITA phone screen share.'
FINISH_RESULT = 'AMITA phone screen-share acceptance: verified a private frame and Source settlement.'
ERRORS = (OSError, ValueError, KeyError, TypeError)


class AcceptanceError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise AcceptanceError(code)


def action(client, namespace, name, **arguments):
    return client.request('/v1/' + namespace + '/action', {'action': name, 'arguments': arguments})


@contextmanager
def locked_directory(directory):
    directory = _private_directory(Path(directory).expanduser(), create=False)
    require(not any((parent / '.git').exists() for parent in (directory,) + tuple(directory.parents)),
            'private_directory_outside_worktree_required')
    fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        os.close(fd)


class Acceptance:
    def __init__(self, directory, operator, worker, node):
        self.directory, self.operator, self.worker = directory, operator, worker
        self.path = directory / 'acceptance.json'
        self.identity = {'worker': node, 'source_url': worker.url, 'source_socket': worker.unix_socket}
        self.state = None
        if self.path.exists() or self.path.is_symlink():
            self.state = private_json(self.path)
            require(self.state.get('format') == FORMAT and self.state.get('identity') == self.identity,
                    'original_local_journal_and_source_required')

    def public_state(self):
        if self.state is None:
            return {}
        keys = ('task_id', 'claim_id', 'request_id', 'grant_id', 'operation_id', 'capability_id',
                'capability_epoch')
        data = {key: self.state[key] for key in keys}
        data['task_epoch'] = self.done.get('claim')
        data['pending_write'] = self.state['pending']
        data['confirmed_steps'] = list(self.done)
        data['capture_verified'] = 'frame' in self.state
        return data

    @property
    def done(self):
        return self.state['confirmed']

    @property
    def worker_principal(self):
        return 'node:' + self.identity['worker']

    @property
    def required(self):
        return ['mesh.node:' + self.identity['worker']]

    def save(self):
        atomic_private(self.path, self.state)

    def checked_capability(self, capability, wait):
        until = time.monotonic() + wait
        while True:
            cap = action(self.worker, 'resource', 'describe', id=capability)
            spec = cap['spec']
            require(cap['id'] == capability and cap['kind'] == 'device.gui'
                    and cap['principal'] == 'node:' + capability.rsplit(':', 1)[0]
                    and spec.get('contract') == CONTRACT and 'inspect' in spec.get('actions', [])
                    and spec.get('screen_share', {}).get('command') == 'screen_share'
                    and spec.get('local_approval_required') is True,
                    'original_phone_screen_share_adapter_required')
            if cap['available'] is True:
                return cap
            remaining = until - time.monotonic()
            require(remaining > 0, 'waiting_for_authenticated_phone_control_presence')
            time.sleep(min(2, remaining))

    def initialize(self, cap, duration):
        task = 'phone-acceptance-' + uuid.uuid4().hex
        self.state = dict(format=FORMAT, identity=self.identity,
                          task_id=task, scope='manual:' + task, claim_id=exact_claim_id(task),
                          request_id='request-' + task, grant_id='grant-' + task,
                          operation_id='operation-' + task, capability_id=cap['id'],
                          capability_epoch=cap['epoch'], duration_ms=duration, pending=None, confirmed={})
        self.save()

    def source_pool(self):
        identity = self.state['capability_id'] + ':foreground'
        pool = next((pool for pool in action(self.operator, 'allocation', 'pools')['pools']
                     if pool['id'] == identity), None)
        require(pool is not None and pool['revoked'] is False, 'original_foreground_pool_required')
        spec = pool['dimensions'].get(DIMENSION, {})
        require(set(pool['dimensions']) == {DIMENSION}
                and all(spec.get(key) == value for key, value in CAPACITY.items()),
                'original_foreground_pool_contract_changed')
        return pool

    def source_binding(self):
        cap = self.state['capability_id']
        bindings = action(self.operator, 'allocation', 'bindings', capability_id=cap)['bindings']
        if not bindings:
            return None
        require(len(bindings) == 1 and bindings[0]['pool_id'] == cap + ':foreground'
                and bindings[0]['dimension'] == DIMENSION, 'original_foreground_binding_required')
        return bindings[0]

    def record(self, step, value):
        self.done[step] = value
        self.state['pending'] = None
        self.save()
        return value

    def write(self, step, attempt, validate=lambda response: True, **basis):
        if step in self.done:
            return self.done[step]
        self.state['pending'] = dict(step=step, **basis)
        self.save()
        return self.record(step, validate(attempt()))

    def pool_epoch(self, pool, expected):
        require(pool['id'] == self.state['capability_id'] + ':foreground'
                and pool['epoch'] == expected and pool['available'] is True,
                'pool_write_unconfirmed')
        return pool['epoch']

    def binding_epoch(self, binding, expected):
        cap = self.state['capability_id']
        require(binding is not None and binding['epoch'] == expected
                and binding['capability_id'] == cap and binding['capability_epoch'] == self.state['capability_epoch']
                and binding['pool_id'] == cap + ':foreground' and binding['pool_epoch'] == self.done['pool']
                and binding['dimension'] == DIMENSION and binding['quantity'] == 1
                and binding['unit'] == CAPACITY['unit'] and binding['usage_key'] == cap
                and binding['new_spend_minor'] == 0, 'pool_binding_unconfirmed')
        return binding['epoch']

    def configure_pool(self):
        pool = self.source_pool()
        require(pool['dimensions'][DIMENSION]['held'] == 0, 'foreground_pool_held')
        if 'pool' not in self.done:
            if pool['available']:
                self.record('pool', pool['epoch'])
            else:
                epoch = pool['epoch'] + 1
                self.write('pool', lambda: action(
                    self.operator, 'allocation', 'define_pool', id=pool['id'], expected_epoch=pool['epoch'],
                    dimensions={DIMENSION: CAPACITY}, lease_seconds=180),
                    lambda response: self.pool_epoch(response, epoch), expected_epoch=epoch)
        else:
            self.pool_epoch(pool, self.done['pool'])
        binding = self.source_binding()
        if 'binding' in self.done:
            self.binding_epoch(binding, self.done['binding'])
        elif (binding is not None and binding['pool_epoch'] == self.done['pool']
              and binding['capability_epoch'] == self.state['capability_epoch']):
            self.record('binding', self.binding_epoch(binding, binding['epoch']))
        else:
            cap = self.state['capability_id']
            old = binding['epoch'] if binding is not None else None
            epoch = old + 1 if old is not None else 1
            self.write('binding', lambda: action(
                self.operator, 'allocation', 'bind_pool', capability_id=cap,
                capability_epoch=self.state['capability_epoch'], pool_id=pool['id'],
                pool_epoch=self.done['pool'], dimension=DIMENSION, quantity=1, unit=CAPACITY['unit'],
                expected_epoch=old), lambda response: self.binding_epoch(response, epoch), expected_epoch=epoch)

    def claim_receipt(self, response):
        receipt = response.get('receipt')
        require(response.get('format') == CLAIM_FORMAT and response.get('task') is None,
                'exact_claim_receipt_route_required')
        if receipt is None and response.get('replayed') is False:
            return None
        require(response.get('replayed') is True and isinstance(receipt, dict)
                and receipt.get('claim_id') == self.state['claim_id'], 'original_claim_receipt_required')
        return receipt

    def claim_epoch(self, task, epoch):
        require(task is not None, 'original_claim_confirmed_empty')
        require(task == self.state['task_id'] and type(epoch) is int and epoch > 0,
                'original_task_claim_unconfirmed')
        return epoch

    def new_claim(self):
        state = self.state
        response = self.worker.request(CLAIM, {'claim_id': state['claim_id'], 'task_id': state['task_id']})
        task = confirm(response, state['claim_id'])
        require(task is not None and task.get('node') == self.identity['worker']
                and task.get('scope') == state['scope'], 'original_task_claim_unconfirmed')
        return task

    def grant_receipt(self, response, approved):
        state = self.state
        require(response['id'] == state['grant_id' if approved else 'request_id']
                and response['principal'] == self.worker_principal
                and response['capability_id'] == state['capability_id']
                and response['capability_epoch'] == state['capability_epoch']
                and response['action'] == 'inspect' and response['scope'] == self.selection()['scope'],
                'original_grant_scope_unconfirmed')
        if approved:
            require(response['request_id'] == state['request_id'] and response['available'] is True
                    and response['approved_by'] == 'operator', 'original_owner_grant_unconfirmed')
        else:
            require(response['status'] == 'pending', 'original_grant_request_unconfirmed')
        return True

    def selection(self):
        state = self.state
        return share_request.selection_for(state['task_id'], self.done['claim'], state['capability_id'],
                                           state['capability_epoch'], state['grant_id'], state['duration_ms'])

    def bindings(self):
        return {'task_id': self.state['task_id'], 'task_epoch': self.done['claim'],
                'operation_id': self.state['operation_id'], 'capability_id': self.state['capability_id']}

    def operation(self, reply):
        selection = self.selection()
        lookup = self.bindings()
        require(all(reply.get(key) == lookup[key] for key in LOOKUP_FIELDS - {'capability_id'})
                and reply.get('actor') == self.worker_principal
                and reply.get('plan') == {'target': lookup['capability_id'],
                                         'selections': [dict(selection, observations=[self.done['observation']])],
                                         'route': []}
                and len(reply.get('dispatch', [])) == 1,
                'original_screen_share_allocation_unconfirmed')
        dispatch = reply['dispatch'][0]
        require(dispatch['capability_id'] == lookup['capability_id']
                and dispatch['provider'] == 'node:' + lookup['capability_id'].rsplit(':', 1)[0],
                'original_phone_dispatch_unconfirmed')
        return {'state': reply['state'], 'dispatch_state': dispatch['state'], 'deadline': reply['deadline'],
                'quiescent': (dispatch['settlement'] or {}).get('evidence', {}).get('resource_quiescent') is True}

    def inspect_operation(self):
        return self.operation(action(self.worker, 'allocation', 'inspect',
                                     operation_id=self.state['operation_id']))

    def dispatch(self, phone_node, wait, duration):
        if self.state is not None:
            require(self.state['pending'] is None, 'reconcile_original_pending_write_first')
            require('reserve' not in self.done, 'original_operation_already_dispatched')
            require(self.state['duration_ms'] == duration, 'original_duration_required')
            capability = self.state['capability_id']
            require(phone_node is None or capability == phone_node + ':ui-control-v1', 'original_phone_required')
        else:
            capability = _identifier(phone_node, 'phone_node') + ':ui-control-v1'
        cap = self.checked_capability(capability, wait)
        if self.state is None:
            self.initialize(cap, duration)
        state = self.state
        require(cap['epoch'] == state['capability_epoch'], 'original_capability_epoch_changed')
        if 'claim' not in self.done:
            require(self.claim_receipt(self.worker.request(RECEIPT, {'claim_id': state['claim_id']})) is None,
                    'original_claim_receipt_already_exists')
        self.configure_pool()
        if 'heartbeat' not in self.done:
            before = self.operator.request('/v1/status')['time']
            self.write('heartbeat', lambda: self.worker.request('/v1/heartbeat', {'capabilities': self.required}),
                       seen_after=before)
        self.write('task', lambda: self.operator.request('/v1/tasks', {
            'id': state['task_id'], 'input': TASK_INPUT, 'required': self.required,
            'context': {'session_scope': state['scope']}}),
            lambda reply: self.verify_task(reply, created=True))
        self.write('claim', self.new_claim, lambda task: self.claim_epoch(task['id'], task['epoch']))
        scope = self.selection()['scope']
        self.write('request_grant', lambda: action(self.worker, 'resource', 'request_grant', id=capability,
                   action='inspect', scope=scope, request_id=state['request_id']),
                   lambda response: self.grant_receipt(response, approved=False))
        self.write('grant', lambda: action(self.operator, 'resource', 'grant', request_id=state['request_id'],
                   grant_id=state['grant_id'], expires_seconds=100),
                   lambda response: self.grant_receipt(response, approved=True))
        observation = self.write('observation', lambda: share_request.observe(self.worker, self.selection()),
                                 self.confirm_observation)
        return self.write('reserve', lambda: share_request.reserve(
            self.worker, state['operation_id'], self.selection(), observation), self.operation)

    @staticmethod
    def confirm_observation(observation):
        require(observation.get('metric') == METRIC and observation.get('unit') == 'observed'
                and type(observation.get('max_age_seconds')) in (int, float)
                and 1 <= observation['max_age_seconds'] <= 86400,
                'original_source_observation_unconfirmed')
        _identifier(observation['observation_id'], 'phone_observation')
        return observation

    def verify_task(self, response, created=False, finished=False):
        require(response['id'] == self.state['task_id']
                and (created or response['scope'] == self.state['scope']), 'original_task_write_unconfirmed')
        if finished:
            require(response['status'] == 'completed' and response['node'] == self.identity['worker']
                    and response['epoch'] == self.done['claim'] and response['result'] == FINISH_RESULT,
                    'original_task_finish_unconfirmed')
        return True

    def audit_event(self, principal, event_type, field, identity, after=0):
        for _ in range(4):
            page = action(self.operator, 'resource', 'audit', principal=principal, after=after, limit=250)
            for event in page['events']:
                if (event['operation'] == event_type and event['capability_id'] == self.state['capability_id']
                        and event['detail'].get(field) == identity):
                    return event
            if len(page['events']) < 250:
                break
            after = page['next']
        raise AcceptanceError('original_grant_audit_event_unconfirmed')

    def recover_observation(self):
        state = self.state
        cap = action(self.worker, 'resource', 'describe', id=state['capability_id'])
        selection = self.selection()
        expected = {'scope': selection['scope'], 'workload': selection['workload'], 'capture_verified': False}
        matches = [item for item in cap['metrics'] if item['actor'] == VERIFIER and item['metric'] == METRIC
                   and item['unit'] == 'observed' and item['capability_epoch'] == state['capability_epoch']
                   and item['verification'] == 'verified' and item['evidence'] == expected]
        require(len(matches) == 1, 'original_source_observation_unconfirmed')
        # Conservative bound for a recovered observation. Source still checks
        # live task, grant, control capability and the observation's sample age.
        return self.confirm_observation({'observation_id': matches[0]['id'], 'metric': METRIC,
                                         'unit': 'observed', 'max_age_seconds': 15})

    def reconcile(self):
        state = self.state
        pending = state['pending']
        if pending is None:
            return self.inspect_operation() if 'reserve' in self.done else {}
        step = pending['step']
        if step == 'pool':
            value = self.pool_epoch(self.source_pool(), pending['expected_epoch'])
        elif step == 'binding':
            value = self.binding_epoch(self.source_binding(), pending['expected_epoch'])
        elif step == 'heartbeat':
            nodes = self.operator.request('/v1/status')['nodes']
            require(any(node['id'] == self.identity['worker'] and node['online'] is True
                        and node['seen'] >= pending['seen_after'] and node['capabilities'] == self.required
                        for node in nodes), 'original_worker_heartbeat_unconfirmed')
            value = True
        elif step in ('task', 'finish'):
            response = self.operator.request('/v1/task/status', {'id': state['task_id']})
            value = self.verify_task(response, finished=step == 'finish')
        elif step == 'claim':
            receipt = self.claim_receipt(self.worker.request(RECEIPT, {'claim_id': state['claim_id']}))
            require(receipt is not None, 'original_task_claim_unconfirmed')
            value = self.claim_epoch(receipt['task_id'], receipt['task_epoch'])
        elif step in ('request_grant', 'grant'):
            event = self.audit_event(self.worker_principal, 'request_grant', 'request_id', state['request_id'])
            require(event['detail']['action'] == 'inspect'
                    and event['detail']['scope'] == self.selection()['scope'], 'original_grant_scope_unconfirmed')
            if step == 'grant':
                event = self.audit_event('operator', 'grant', 'grant_id', state['grant_id'], after=event['sequence'])
                require(event['detail']['request_id'] == state['request_id']
                        and event['detail']['principal'] == self.worker_principal, 'original_owner_grant_unconfirmed')
            value = True
        elif step == 'observation':
            value = self.recover_observation()
        elif step == 'reserve':
            value = self.inspect_operation()
        else:
            raise AcceptanceError('invalid_original_pending_write')
        self.record(step, value)
        return value if step == 'reserve' else {}

    def observe(self, wait):
        require('reserve' in self.done, 'confirmed_original_dispatch_required')
        if 'frame' in self.state:
            return self.state['frame']
        operation = self.inspect_operation()
        until = time.monotonic() + wait
        while operation['state'] in ('reserved', 'accepted', 'running'):
            try:
                result = read_frame(self.worker, self.bindings(), str(self.directory))
                metadata = {key: result[key] for key in ('path', 'width', 'height', 'revision', 'sha256',
                                                         'capture_timestamp')}
                self.state['frame'] = metadata
                self.save()
                return metadata
            except HTTPError as exc:
                if exc.code != 409:
                    raise
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))
            operation = self.inspect_operation()
        raise AcceptanceError('original_private_frame_unavailable')

    def finish(self):
        require(self.state['pending'] is None, 'reconcile_original_pending_write_first')
        require('frame' in self.state and 'reserve' in self.done, 'confirmed_original_frame_required')
        operation = self.inspect_operation()
        require(operation['state'] == 'completed' and operation['dispatch_state'] == 'completed'
                and operation['quiescent'], 'original_capture_settlement_unconfirmed')
        self.write('finish', self.finish_task)
        return operation

    def finish_task(self):
        task_id = self.state['task_id']
        receipt = self.worker.request('/v1/task/update', {'id': task_id, 'epoch': self.done['claim'],
            'status': 'completed', 'result': FINISH_RESULT})
        require(receipt.get('ok') is True, 'original_task_finish_unconfirmed')
        self.verify_task(self.operator.request('/v1/task/status', {'id': task_id}), finished=True)
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--operator-config', required=True)
    parser.add_argument('--worker-config', required=True)
    parser.add_argument('--directory', required=True, help='Existing absolute owner-only 0700 directory outside Git')
    commands = parser.add_subparsers(dest='command')
    dispatch = commands.add_parser('dispatch')
    dispatch.add_argument('--phone-node', help='Original enrolled phone node ID for a fresh acceptance')
    dispatch.add_argument('--wait-ready', type=int, default=0, choices=range(121), metavar='0..120')
    dispatch.add_argument('--duration-ms', type=int, metavar='1000..30000')
    observe = commands.add_parser('observe')
    observe.add_argument('--wait', type=int, default=30, choices=range(61), metavar='0..60')
    commands.add_parser('reconcile')
    commands.add_parser('finish')
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    if args.command == 'dispatch' and args.duration_ms is not None and not 1000 <= args.duration_ms <= 30000:
        parser.error('--duration-ms must be between 1000 and 30000')
    run = None
    try:
        operator = Client(private_json(args.operator_config))
        config = private_json(args.worker_config)
        worker = Client(config)
        node = _identifier(config['node_id'], 'manual_worker_node', maximum=100)
        require(config.get('capabilities') == ['mesh.node:' + node], 'dedicated_manual_worker_required')
        require((worker.url, worker.unix_socket) == (operator.url, operator.unix_socket),
                'original_common_source_required')
        with locked_directory(args.directory) as directory:
            run = Acceptance(directory, operator, worker, node)
            if args.command == 'dispatch':
                require(run.state is not None or args.phone_node is not None, 'original_phone_node_required')
                duration = args.duration_ms
                if duration is None:
                    duration = run.state['duration_ms'] if run.state is not None else 30000
                result = run.dispatch(args.phone_node, args.wait_ready, duration)
            else:
                require(run.state is not None, 'original_local_journal_required')
                result = run.observe(args.wait) if args.command == 'observe' else getattr(run, args.command)()
            print(json.dumps(dict(run.public_state(), result=result), ensure_ascii=False))
        return 0
    except ERRORS as exc:
        status = {'error': str(exc) if isinstance(exc, AcceptanceError) else 'original_source_or_local_io_unconfirmed'}
        if isinstance(exc, HTTPError):
            status['http_status'] = exc.code
        if run is not None:
            status.update(run.public_state())
        print(json.dumps(status), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
