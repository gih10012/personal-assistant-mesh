"""Task-bound phone computer use through existing worker RPCs and call journal.

No operator/provider identity, device credential, HDC, model invocation, task
creation or second operation ledger. Every GUI effect remains phone-approved.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import stat

from .capability_call import CapabilityCallError, TaskCapabilityCalls
from .config import private_json
from .phone_control import CONTRACT, KIND
from .resources import _name

COMMANDS = frozenset(('inspect_ui', 'perform_action', 'screenshot'))
FORMAT = 'mesh-phone-computer-use/1'
MAX_STDOUT_BYTES = 8192


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def encode_blob(value):
    raw = canonical(value).encode('utf8')
    if len(raw) > 65536:
        raise ValueError('phone_computer_use_command_too_large')
    return {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'base64': base64.b64encode(raw).decode('ascii')}


def decode_blob(body):
    if (not isinstance(body, dict) or set(body) != {'bytes', 'sha256', 'base64'}
            or type(body['bytes']) is not int or not 0 <= body['bytes'] <= 65536
            or not isinstance(body['base64'], str) or len(body['base64']) > 87384):
        raise ValueError('phone_computer_use_full_result_invalid')
    raw = base64.b64decode(body['base64'], validate=True)
    if len(raw) != body['bytes'] or hashlib.sha256(raw).hexdigest() != body['sha256']:
        raise ValueError('phone_computer_use_full_result_invalid')
    return raw


def private_output(path, raw):
    """Explicit caller destination: create 0600 or reuse identical private bytes."""
    absolute = os.path.abspath(os.path.expanduser(path))
    try:
        fd = os.open(absolute, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        fd = os.open(absolute, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or info.st_size != len(raw) or handle.read(len(raw) + 1) != raw):
                raise ValueError('phone_computer_use_output_exists_or_not_private')
        reused = True
    else:
        info = os.fstat(fd)
        try:
            with os.fdopen(fd, 'wb') as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        except Exception:
            current = os.lstat(absolute)
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                os.unlink(absolute)
            raise
        reused = False
    return {'path': absolute, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'reused': reused}


def result_output(receipt, image_out=None, output_file=None):
    """Bounded text for agents; binary/full JSON stays in explicit local files."""
    output = {k: receipt[k] for k in ('format', 'operation_id', 'decision_id', 'task_id', 'original_task_epoch',
        'operation_state', 'phase', 'reason', 'verification', 'result_available',
        'execution_verified', 'automatic_invocation_replay', 'retry_with_new_id') if k in receipt}
    dispatch = receipt.get('provider_receipt')
    if dispatch:
        output['provider_receipt'] = {k: dispatch[k] for k in ('operation_id', 'capability_id', 'provider', 'state',
                                                              'epoch', 'receipt_id') if k in dispatch}
    if not receipt.get('result_available'):
        return output
    raw = decode_blob(receipt['result'])
    output.update(result_bytes=len(raw), result_sha256=hashlib.sha256(raw).hexdigest())
    try:
        body = json.loads(raw.decode('utf8'))
    except (ValueError, UnicodeError):
        body = None
    is_control = (isinstance(body, dict) and body.get('format') == 'amita-phone-ui-control-result/1'
        and body.get('contract') == CONTRACT and body.get('operation_id') == receipt['operation_id']
        and body.get('task_id') == receipt['task_id'] and body.get('task_epoch') == receipt['original_task_epoch']
        and dispatch is not None and body.get('principal') == dispatch.get('provider'))
    image, image_raw = None, None
    if is_control and body.get('command') == 'screenshot':
        # Current PhoneControlRuntime payload is screenshot.*, not image.*.
        image = body.get('screenshot')
        fields = {'format', 'width', 'height', 'display_id', 'crop_left', 'crop_top', 'scale_x', 'scale_y',
                  'user_selected', 'bytes', 'sha256', 'base64'}
        if (not isinstance(image, dict) or set(image) != fields or image['format'] != 'image/jpeg'
                or any(type(image[k]) is not int or not 1 <= image[k] <= 16384 for k in ('width', 'height'))
                or any(type(image[k]) is not int or image[k] < 0 for k in ('display_id', 'crop_left', 'crop_top'))
                or any(type(image[k]) not in (int, float) or not math.isfinite(image[k])
                       or not 0 < image[k] <= 1 for k in ('scale_x', 'scale_y'))
                or type(image['user_selected']) is not bool):
            raise ValueError('phone_computer_use_original_screenshot_invalid')
        image_raw = decode_blob({k: image[k] for k in ('bytes', 'sha256', 'base64')})
        if not image_raw.startswith(b'\xff\xd8\xff') or not image_raw.endswith(b'\xff\xd9'):
            raise ValueError('phone_computer_use_original_screenshot_not_jpeg')
        output['screenshot'] = {k: image[k] for k in fields if k != 'base64'}
        output['coordinate_mapping'] = {'screen_x': 'crop_left + image_x / scale_x',
            'screen_y': 'crop_top + image_y / scale_y', 'verification': 'provider_reported'}
    elif is_control and body.get('command') == 'inspect_ui' and isinstance(body.get('view'), dict):
        view = body['view']
        output['view'] = {k: view[k] for k in ('revision', 'bundle_name', 'window_id', 'view_ref', 'width', 'height', 'truncated') if k in view}
        elements = []
        for element in view.get('elements', [])[:16]:
            if not isinstance(element, dict):
                continue
            item = {k: element[k] for k in ('element_ref', 'sensitive', 'enabled', 'actions', 'bounds') if k in element}
            item.update(role=str(element.get('role', ''))[:80], label=str(element.get('label', ''))[:80])
            elements.append(item)
        output['view']['elements'] = elements
        output['view']['truncated'] = bool(view.get('truncated') or len(view.get('elements', [])) > len(elements))
        while elements and len(canonical(output).encode('utf8')) > MAX_STDOUT_BYTES - 1024:
            elements.pop(); output['view']['truncated'] = True
    elif is_control and body.get('command') == 'perform_action' and isinstance(body.get('receipt'), dict):
        output['receipt'] = {k: body['receipt'][k] for k in ('operation_id', 'state', 'input_acknowledged',
                                                          'effect_verified', 'automatic_replay') if k in body['receipt']}
    if image_out:
        if image_raw is None:
            raise ValueError('phone_computer_use_original_screenshot_required')
        output['image_file'] = private_output(image_out, image_raw)
    if output_file:
        output['output_file'] = private_output(output_file, raw)
    if len(canonical(output).encode('utf8')) > MAX_STDOUT_BYTES:
        raise ValueError('phone_computer_use_metadata_too_large')
    return output


def command_body(command, arguments):
    if command not in COMMANDS or not isinstance(arguments, dict):
        raise ValueError('phone_computer_use_command_invalid')
    body = {'format': CONTRACT, 'command': command}
    if command == 'inspect_ui':
        if set(arguments) - {'max_elements'}:
            raise ValueError('phone_computer_use_inspect_arguments_invalid')
        maximum = arguments.get('max_elements', 64)
        if type(maximum) is not int or not 1 <= maximum <= 128:
            raise ValueError('phone_computer_use_inspect_arguments_invalid')
        body['max_elements'] = maximum
    elif command == 'perform_action':
        if (set(arguments) != {'action', 'element_ref', 'text'}
                or arguments['action'] not in ('tap', 'set_text', 'scroll_forward', 'scroll_backward')
                or not isinstance(arguments['text'], str) or len(arguments['text']) > 4096
                or arguments['action'] != 'set_text' and arguments['text'] != ''):
            raise ValueError('phone_computer_use_action_arguments_invalid')
        _name(arguments['element_ref'], 'phone_element_ref', 180)
        # Task/operation/epoch come solely from the phone's real dispatch.
        # The wire command cannot supply or override those execution bindings.
        body.update(arguments)
    elif arguments:
        raise ValueError('phone_computer_use_screenshot_arguments_invalid')
    return body


class PhoneComputerUse:
    def __init__(self, calls, phone_node_id, commands=()):
        if not isinstance(calls, TaskCapabilityCalls):
            raise ValueError('phone_computer_use_existing_task_calls_required')
        self.calls, self.client = calls, calls.client
        self.phone = _name(phone_node_id, 'phone_node_id', 160)
        if not isinstance(commands, list) or set(commands) - COMMANDS:
            raise ValueError('phone_computer_use_owner_commands_required')
        self.commands, self.capability = list(commands), self.phone + ':ui-control-v1'

    def prepare(self, command, operation_id, arguments=None):
        _name(operation_id, 'operation_id', 180)
        _name(self.calls.task_id, 'phone_control_task_id', 180)
        body = command_body(command, arguments or {})
        return {'action': 'action' if command == 'perform_action' else 'inspect',
                'scope': {'task_id': self.calls.task_id, 'phone_node_id': self.phone,
                          'operation_id': operation_id, 'command': command},
                'workload': {'input': encode_blob(body)}}

    def capability_status(self, command=None, operation_id=None, arguments=None, observations=()):
        # The existing worker-only routing endpoint authenticates CURRENT task,
        # node and epoch before any new reservation or grant inspection.
        self.calls._hello()
        self.calls._context()
        try:
            cap = self.client.request('/v1/resource/action', {'action': 'describe',
                                                            'arguments': {'id': self.capability}})
        except (ValueError, OSError):
            return {'format': FORMAT, 'phone_node_id': self.phone, 'capability_id': self.capability,
                    'operation_id': operation_id, 'available': False, 'ready_to_reserve': False,
                    'reason': 'device_service_not_installed_or_unreachable', 'execution_verified': False,
                    'automatic_invocation_replay': False, 'retry_with_new_id': False}
        contract = (cap.get('id') == self.capability and cap.get('principal') == 'node:' + self.phone
                    and cap.get('kind') == KIND and cap.get('spec', {}).get('contract') == CONTRACT
                    and cap.get('spec', {}).get('local_approval_required') is True
                    and cap.get('spec', {}).get('human_input_fence_required') is True)
        result = {'format': FORMAT, 'phone_node_id': self.phone, 'capability_id': self.capability,
                  'task_id': self.calls.task_id, 'task_epoch': self.calls.task_epoch,
                  'available': bool(contract and cap.get('available')), 'contract_valid': contract,
                  'commands': self.commands, 'epoch': cap.get('epoch'),
                  'ready_to_reserve': False, 'execution_verified': False,
                  'automatic_invocation_replay': False, 'retry_with_new_id': False,
                  'reason': 'request_scope_required' if contract and cap.get('available') else 'device_service_unavailable'}
        if command is None:
            return result
        work = self.prepare(command, operation_id, arguments)
        result.update(operation_id=operation_id, command=command, prepared=work,
                      service_available=result['available'], available=False)
        if command not in self.commands:
            result['reason'] = 'command_not_owner_enabled_or_probed'
            return result
        if not result['service_available']:
            return result
        authorization = self.client.request('/v1/resource/action', {'action': 'authorize', 'arguments': {
            'id': self.capability, 'action': work['action'], 'scope': work['scope']}})
        result['authorization'] = authorization
        if authorization.get('allowed') is not True:
            result['reason'] = 'exact_grant_required'
            return result
        # Query the existing atomic task evidence snapshot with an explicit GUI
        # filter; never invent measurements or relabel provider declarations.
        context = self.calls._routing('context', {'kind': KIND, 'limit': 100, 'include_unavailable': True})
        sections, qualified = context.get('sections', {}), []
        metrics = sections.get('local_observations', {}).get('records', [])
        for requirement in observations:
            if (not isinstance(requirement, dict)
                    or set(requirement) != {'observation_id', 'metric', 'unit', 'max_age_seconds'}
                    or type(requirement['max_age_seconds']) not in (int, float)
                    or not 1 <= requirement['max_age_seconds'] <= 86400):
                raise ValueError('phone_computer_use_observation_invalid')
            matches = [m for m in metrics if m.get('id') == requirement['observation_id']
                and m.get('capability_id') == self.capability and m.get('capability_epoch') == cap['epoch']
                and m.get('verification') == 'verified' and m.get('trusted_verifier_current') is True
                and m.get('independent_of_current_provider') is True
                and m.get('metric') == requirement['metric'] and m.get('unit') == requirement['unit']
                and not m.get('sample_time_in_future') and m.get('sample_age_seconds', 1e99) < requirement['max_age_seconds']
                and isinstance(m.get('evidence'), dict)
                and m.get('evidence', {}).get('scope') == work['scope']
                and m.get('evidence', {}).get('workload') == work['workload']]
            if not matches:
                result['reason'] = 'qualified_scope_workload_observation_required'
                return result
            qualified.append(dict(requirement))
        if not qualified:
            result['reason'] = 'qualified_scope_workload_observation_required'
            return result
        bindings = sections.get('local_pool_bindings', {}).get('records', [])
        pools = sections.get('local_pools', {}).get('records', [])
        selected = [b for b in bindings if b.get('capability_id') == self.capability and b.get('capability_epoch') == cap['epoch']]
        if not selected or not all(b.get('new_spend_minor', 0) == 0 and any(
            p.get('id') == b.get('pool_id') and p.get('epoch') == b.get('pool_epoch') and p.get('available') is True
            and p.get('dimensions', {}).get(b['dimension'], {}).get('unit') == b['unit']
            and p.get('dimensions', {}).get(b['dimension'], {}).get('remaining', 0) >= b['quantity']
            for p in pools) for b in selected):
            result['reason'] = 'managed_phone_resource_pool_unavailable'
            return result
        result.update(available=True, ready_to_reserve=True, reason='original_allocation_will_recheck', observations=qualified)
        return result

    def invoke(self, command, operation_id, decision_id, arguments=None, observations=(), lease_seconds=60):
        work = self.prepare(command, operation_id, arguments)
        # An existing local intent can only be queried. No write is repeated,
        # including after lost ACK, lease loss, changed capacity or phone restart.
        try:
            previous = self.calls._row(operation_id)
        except CapabilityCallError as error:
            if str(error) != 'capability_call_not_found':
                raise
        else:
            original = json.loads(previous['request'])
            selection = original['plan']['selections'][0]
            if (previous['decision_id'] != decision_id or selection['scope'] != work['scope']
                    or selection['workload'] != work['workload'] or selection['action'] != work['action']
                    or selection['capability_id'] != self.capability):
                raise ValueError('phone_computer_use_original_operation_changed')
            return self.status(operation_id)
        status = self.capability_status(command, operation_id, arguments, observations)
        if not status['ready_to_reserve']:
            return status
        selection = dict(work, capability_id=self.capability, epoch=status['epoch'], observations=list(observations))
        receipt = self.calls.call(operation_id, decision_id, selection,
            [{'capability_id': self.capability, 'epoch': status['epoch']}],
            'Owner-enabled phone contract with exact scope and independent observations; local approval remains mandatory.',
            lease_seconds)
        return self._project(receipt)

    def _project(self, receipt):
        allocation = receipt.get('allocation')
        dispatch = allocation['dispatch'][0] if allocation else None
        if allocation and (allocation['plan']['target'] != self.capability
                           or dispatch.get('provider') != 'node:' + self.phone):
            raise ValueError('phone_computer_use_original_provider_changed')
        return {'format': FORMAT, 'operation_id': receipt['operation_id'], 'decision_id': receipt['decision_id'],
                'task_id': receipt['task_id'], 'original_task_epoch': receipt['original_task_epoch'],
                'operation_state': receipt['operation_state'], 'phase': receipt['phase'],
                'provider_receipt': dispatch, 'diagnostics': receipt['diagnostics'],
                'reason': 'awaiting_device_provider_or_local_approval' if dispatch and dispatch['state'] in ('pending', 'accepted', 'running') else receipt['phase'],
                'automatic_invocation_replay': False, 'retry_with_new_id': False,
                'execution_verified': False}

    def status(self, operation_id):
        return self._project(self.calls.status(operation_id))

    def result(self, operation_id):
        status = self.status(operation_id)
        dispatch = status['provider_receipt']
        if dispatch is None or dispatch['state'] not in ('running', 'unknown', 'completed', 'stopped'):
            return status
        try:
            body = self.client.request('/v1/phone/result', {'operation_id': operation_id, 'capability_id': self.capability})
        except (OSError, ValueError):
            return dict(status, result_available=False, reason='original_phone_result_unconfirmed')
        raw = decode_blob(body)
        settlement = dispatch.get('settlement')
        if settlement:
            evidence = settlement.get('evidence', {})
            if (evidence.get('sha256') != body['sha256'] or evidence.get('bytes') != body['bytes']
                    or evidence.get('result_reference') != 'phone-result:' + body['sha256']):
                raise ValueError('phone_computer_use_original_result_anchor_changed')
        status.update(result_available=True, result=body, verification='original_full_bytes_sha256_only')
        try:
            status['json'] = json.loads(raw.decode('utf8'))
        except (ValueError, UnicodeError):
            pass  # Image/binary bytes remain complete, with no fabricated JSON.
        return status


def load_entry(path):
    binding = private_json(path)
    if (not isinstance(binding, dict) or set(binding) != {'format', 'config_path', 'task_id', 'task_epoch', 'phone_node_id', 'commands'}
            or binding['format'] != 'mesh-phone-computer-use-binding/1'
            or type(binding['task_epoch']) is not int or binding['task_epoch'] < 1):
        raise ValueError('phone_computer_use_private_binding_required')
    config = private_json(binding['config_path'])
    capability_calls = config.get('capability_calls')
    if (not isinstance(capability_calls, dict) or set(capability_calls) != {'authority', 'journal_path'}
            or config.get('role', 'worker') != 'worker'):
        raise ValueError('phone_computer_use_existing_worker_configuration_required')
    from .worker import Client
    calls = TaskCapabilityCalls(Client(config), capability_calls['journal_path'], capability_calls['authority'],
                                config['node_id'], binding['task_id'], binding['task_epoch'])
    return PhoneComputerUse(calls, binding['phone_node_id'], binding['commands'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Existing task-bound phone computer use; no operator or HDC fallback')
    parser.add_argument('--binding', required=True, help='owner-installed private current-task binding')
    parser.add_argument('action', choices=sorted(COMMANDS) + ['capability_status', 'status', 'result', 'prepare'])
    parser.add_argument('--operation-id'); parser.add_argument('--decision-id')
    parser.add_argument('--request-file', help='JSON arguments/observations; contains no credential or task binding')
    parser.add_argument('--command', choices=sorted(COMMANDS), help='command for prepare/capability_status')
    parser.add_argument('--image-out', help='result only: explicit private JPEG destination, created once or same-bytes reuse')
    parser.add_argument('--output-file', help='result only: explicit private destination for the complete original Blob')
    args = parser.parse_args(argv)
    try:
        entry = load_entry(args.binding)
        if (args.image_out or args.output_file) and args.action != 'result':
            raise ValueError('phone_computer_use_result_output_only')
        value = {}
        if args.request_file:
            with open(args.request_file, encoding='utf8') as handle:
                raw = handle.read(131073)
            if len(raw.encode('utf8')) > 131072:
                raise ValueError('phone_computer_use_request_too_large')
            value = json.loads(raw)
            if (not isinstance(value, dict) or set(value) - {'arguments', 'observations', 'lease_seconds'}):
                raise ValueError('phone_computer_use_request_invalid')
        if args.action in ('status', 'result'):
            output = getattr(entry, args.action)(args.operation_id)
            if args.action == 'result':
                output = result_output(output, args.image_out, args.output_file)
        elif args.action == 'prepare':
            output = entry.prepare(args.command, args.operation_id, value.get('arguments', {}))
        elif args.action == 'capability_status':
            output = entry.capability_status(args.command, args.operation_id, value.get('arguments', {}), value.get('observations', []))
        else:
            output = entry.invoke(args.action, args.operation_id, args.decision_id, value.get('arguments', {}),
                                  value.get('observations', []), value.get('lease_seconds', 60))
        print(canonical(output)); return 0
    except Exception:
        print(canonical({'format': FORMAT, 'operation_id': args.operation_id, 'outcome': 'unconfirmed',
            'available': False,
            'reason': 'configured_binding_or_original_receipt_unavailable', 'automatic_invocation_replay': False,
            'retry_with_new_id': False, 'next_action': 'query status/result using the same original operation ID'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
