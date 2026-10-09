import argparse
import json
import os
import signal
import sys
import threading
from pathlib import Path
from urllib.parse import urlencode

from .config import discover_codex_auth, private_json


_PROVIDER_DIAGNOSTICS = frozenset((
    'authority_unavailable', 'authority_rejected', 'authority_response_invalid',
    'owner_advertisement_mismatch', 'owner_entry_invalid', 'local_outcome_unresolved',
    'adapter_outcome_unknown', 'owner_adapter_missing', 'owner_adapter_contract_mismatch',
    'recorded_settlement_pending'))
_PROVIDER_ERRORS = frozenset((
    'provider_client_required', 'owner_config_unavailable', 'owner_config_invalid',
    'owner_module_unavailable', 'owner_module_integrity_failed', 'owner_module_compile_failed',
    'provider_journal_invalid', 'owner_advertisement_mismatch', 'owner_entry_invalid',
    'provider_journal_failed', 'provider_poll_already_active', 'provider_dispatch_failed',
    'provider_reconciliation_failed', 'invalid_provider_cycle_limit'))

_TOOL_ERRORS = frozenset((
    'tool_lifecycle_failed', 'lifecycle_private_directory_required', 'tool_publication_invalid',
    'lifecycle_provider_journal_collision', 'lifecycle_control_path_collision',
    'lifecycle_client_required', 'lifecycle_journal_invalid', 'lifecycle_journal_identity_mismatch',
    'lifecycle_lock_invalid', 'lifecycle_busy', 'owner_identity_changed',
    'lifecycle_operation_invalid', 'lifecycle_operation_not_found', 'retained_release_invalid',
    'lifecycle_operation_conflict', 'candidate_identity_invalid', 'candidate_integrity_failed',
    'publication_authority_mismatch', 'publication_query_failed', 'publication_response_invalid',
    'publication_directory_incomplete', 'publication_not_confirmed',
    'publication_predecessor_mismatch', 'activation_requires_publication',
    'activation_publication_changed', 'activation_manifest_invalid', 'activation_owner_changed',
    'activation_backup_conflict', 'activation_not_confirmed', 'rollback_requires_new_epoch'))


def _tool_error(category):
    print(json.dumps({'status': 'error', 'diagnostics': [category],
        'native_tools_intercepted': False, 'automatic_invocation_replay': False,
        'retry_with_new_id': False, 'execution_verified': False}), flush=True)


def _tool_command(args):
    # A local native-Shell entry point, not an HTTP source installer. It never
    # starts/restarts services, runs tests/modules, or resets execution journals.
    actions = ('stage', 'publish', 'activate', 'inspect', 'prepare-rollback')
    foreign = ('text', 'request_id', 'payload_file', 'kind', 'principal', 'limit',
               'after', 'id', 'task_id', 'call_id', 'max_cycles')
    valid = (args.action in actions and args.owner_config and args.state_dir and args.operation_id
             and not args.include_unavailable
             and not any(getattr(args, name) is not None for name in foreign))
    if args.action == 'stage':
        valid = valid and args.candidate_config and args.publication_config and args.retained_operation_id is None and args.epoch is None
    elif args.action == 'prepare-rollback':
        valid = valid and args.retained_operation_id and type(args.epoch) is int and args.epoch > 0 and args.candidate_config is None and args.publication_config is None
    else:
        valid = valid and all(getattr(args, name) is None for name in
                             ('candidate_config', 'publication_config', 'retained_operation_id', 'epoch'))
    if not valid:
        _tool_error('tool_arguments_invalid')
        raise SystemExit(2)
    try:
        from .worker import Client
        config = private_json(args.config)
        if not isinstance(config, dict):
            raise ValueError('invalid_client_config')
        client = Client(config)
    except Exception:
        _tool_error('tool_client_config_invalid')
        raise SystemExit(2) from None
    from .tool_lifecycle import ToolLifecycle, ToolLifecycleError
    try:
        controller = ToolLifecycle(client, args.owner_config, args.state_dir)
        if args.action == 'stage':
            value = controller.stage(args.operation_id, args.candidate_config, args.publication_config)
        elif args.action == 'prepare-rollback':
            value = controller.prepare_rollback(args.operation_id, args.retained_operation_id, args.epoch)
        else:
            value = getattr(controller, args.action)(args.operation_id)
        print(json.dumps(value, sort_keys=True), flush=True)
        if value.get('state') == 'unknown':
            raise SystemExit(2)
    except ToolLifecycleError as error:
        code = str(error)
        _tool_error(code if code in _TOOL_ERRORS else 'tool_lifecycle_failed')
        raise SystemExit(2) from None
    except Exception:
        _tool_error('tool_lifecycle_failed')
        raise SystemExit(2) from None
    return 0


def _provider_error(category):
    print(json.dumps({'status': 'error', 'diagnostics': [category], 'recoverable': False,
        'dispatch_count': None, 'reconciled_count': None, 'local_unsettled': None, 'cycles': None,
        'automatic_invocation_replay': False, 'retry_with_new_id': False,
        'native_tools_intercepted': False}), flush=True)


def _network_error(category):
    print(json.dumps({'status': 'error', 'diagnostics': [category],
        'network_configuration_changed': False, 'native_features_restricted': False,
        'capabilities_advertised': False, 'allocation_authorized': False}), flush=True)


def _network_command(args):
    # Local metadata needs no Mesh credential. This command does not enroll a
    # candidate, send a request, probe the Internet, or control native recovery.
    foreign = ('config', 'text', 'request_id', 'action', 'payload_file', 'kind',
               'principal', 'limit', 'after', 'id', 'task_id', 'epoch', 'call_id',
               'owner_config', 'max_cycles', 'state_dir', 'operation_id',
               'candidate_config', 'publication_config', 'retained_operation_id')
    if (not args.node_id or args.include_unavailable
            or any(getattr(args, name) is not None for name in foreign)):
        _network_error('network_arguments_invalid')
        raise SystemExit(2)
    from .network_inventory import inspect_network, NetworkInventoryError
    try:
        report = inspect_network(args.node_id, freshness_seconds=
                                 30 if args.freshness_seconds is None else args.freshness_seconds)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    except NetworkInventoryError:
        _network_error('network_inventory_invalid')
        raise SystemExit(2) from None
    except Exception:
        _network_error('network_inventory_failed')
        raise SystemExit(2) from None
    return 0


class _CLIParser(argparse.ArgumentParser):
    def error(self, message):
        # This is only error-output redaction, never command selection. A text
        # value equal to "provider" can also select this safer error renderer;
        # it cannot activate a runtime for worker/node or any other command.
        if 'tool-release' in sys.argv[1:]:
            _tool_error('tool_arguments_invalid')
            self.exit(2)
        if 'provider' in sys.argv[1:]:
            _provider_error('provider_arguments_invalid')
            self.exit(2)
        if 'network-inventory' in sys.argv[1:]:
            _network_error('network_arguments_invalid')
            self.exit(2)
        argparse.ArgumentParser.error(self, message)


def _provider_summary(report, cycles=None):
    if not isinstance(report, dict) or report.get('status') not in ('ok', 'degraded'):
        raise ValueError('invalid_provider_report')
    if not isinstance(report.get('diagnostics'), list) or type(report.get('recoverable')) is not bool:
        raise ValueError('invalid_provider_report')
    diagnostics = sorted({value if isinstance(value, str) and value in _PROVIDER_DIAGNOSTICS
                          else 'provider_diagnostic_unknown' for value in report['diagnostics']})
    dispatch, reconciled = report.get('dispatch'), report.get('reconciled')
    unsettled = report.get('local_unsettled')
    if (not isinstance(dispatch, list) or not isinstance(reconciled, list)
            or type(unsettled) is not int or unsettled < 0):
        raise ValueError('invalid_provider_report')
    result = {'status': report['status'], 'diagnostics': diagnostics, 'recoverable': report['recoverable'],
        'dispatch_count': len(dispatch), 'reconciled_count': len(reconciled), 'local_unsettled': unsettled,
        'automatic_invocation_replay': False, 'retry_with_new_id': False,
        'native_tools_intercepted': False}
    if cycles is not None:
        if type(cycles) is not int or cycles < 0:
            raise ValueError('invalid_provider_report')
        result['cycles'] = cycles
    return result


def _provider_command(args):
    # An explicit command/action/owner config is the only installation entry;
    # no model payload, startup scan, Node/Worker default or service is changed.
    foreign = ('text', 'request_id', 'payload_file', 'kind', 'principal', 'limit', 'after',
               'id', 'task_id', 'epoch', 'call_id')
    if (args.action not in ('describe', 'poll', 'serve') or not args.owner_config
            or args.include_unavailable or any(getattr(args, name) is not None for name in foreign)
            or (args.max_cycles is not None and args.action != 'serve')):
        _provider_error('provider_arguments_invalid')
        raise SystemExit(2)
    cycles = None
    if args.max_cycles is not None:
        try:
            cycles = int(args.max_cycles)
            if str(cycles) != args.max_cycles or not 1 <= cycles <= 1000000:
                raise ValueError('invalid_cycle_limit')
        except (ValueError, TypeError):
            _provider_error('provider_arguments_invalid')
            raise SystemExit(2)
    try:
        from .worker import Client
        config = private_json(args.config)
        if not isinstance(config, dict):
            raise ValueError('invalid_client_config')
        client = Client(config)
    except Exception:
        _provider_error('provider_client_config_invalid')
        raise SystemExit(2) from None
    from .provider_runtime import ProviderRuntime, ProviderRuntimeError
    stop, handlers = threading.Event(), {}
    previous_summary = [None]

    def emit(summary):
        encoded = json.dumps(summary, sort_keys=True)
        if encoded != previous_summary[0]:
            print(encoded, flush=True)
            previous_summary[0] = encoded

    outcome, failure = 0, None
    try:
        if args.action == 'serve' and threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT):
                handlers[number] = signal.signal(number, lambda signum, frame: stop.set())
        runtime = ProviderRuntime(client, args.owner_config)
        if args.action == 'describe':
            bindings = runtime.describe_bindings()
            emit({'status': 'ok', 'diagnostics': [], 'recoverable': True, 'bindings': bindings,
                'binding_count': len(bindings), 'journal_initialized': True, 'module_execution_started': False,
                'advertised': False, 'automatic_invocation_replay': False, 'retry_with_new_id': False,
                'native_tools_intercepted': False})
        elif args.action == 'poll':
            summary = _provider_summary(runtime.poll_once(), cycles=1)
            emit(summary)
            outcome = 2 if summary['status'] == 'degraded' or not summary['recoverable'] else 0
        else:
            value = runtime.serve(stop=stop, on_report=lambda report: emit(_provider_summary(report)), max_cycles=cycles)
            last = value['last_report']
            if last is None and stop.is_set() and value['cycles'] == 0:
                summary = {'status': 'stopped', 'diagnostics': [], 'recoverable': True,
                    'dispatch_count': 0, 'reconciled_count': 0, 'local_unsettled': None, 'cycles': 0,
                    'automatic_invocation_replay': False, 'retry_with_new_id': False, 'native_tools_intercepted': False}
            else:
                summary = _provider_summary(last, cycles=value['cycles'])
            if stop.is_set():
                summary['stop_requested'] = True
            emit(summary)
            outcome = 0 if stop.is_set() else (2 if summary['status'] == 'degraded' or not summary['recoverable'] else 0)
    except ProviderRuntimeError as error:
        code = str(error)
        failure = code if code in _PROVIDER_ERRORS else 'provider_runtime_failed'
    except BaseException:
        # Trusted modules can raise SystemExit/KeyboardInterrupt too. A durable
        # intent without a result is not success or permission to replay it.
        failure = 'provider_runtime_failed'
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
    if failure is not None:
        _provider_error(failure)
        raise SystemExit(2) from None
    if outcome:
        # __main__ deliberately ignores main's return value today: a real
        # process exit is required for service failure/degraded semantics.
        raise SystemExit(outcome)
    return 0


def resource_payload(path, action=None):
    """Owned private JSON outside git worktrees; secrets never enter argv."""
    arguments = {} if path is None else private_json(path)
    if path is not None:
        selected = Path(path).expanduser().resolve()
        if any((parent / '.git').exists() for parent in selected.parents):
            raise ValueError('payload_file_must_be_outside_git_worktrees')
    if not isinstance(arguments, dict):
        raise ValueError('resource_payload_requires_object')
    if 'action' in arguments:
        if action is not None and action != arguments['action']:
            raise ValueError('resource_action_conflict')
        payload = arguments
    else:
        if not action:
            raise ValueError('resource_requires_action')
        payload = {'action': action, 'arguments': arguments}
    if set(payload) - {'action', 'arguments'} or not isinstance(payload.get('action'), str) or not isinstance(payload.get('arguments', {}), dict):
        raise ValueError('invalid_resource_payload')
    return payload


def main():
    os.umask(0o077)
    parser = _CLIParser(description='Durable personal-assistant mesh')
    parser.add_argument('--config', help='Required private configuration except for local network-inventory')
    parser.add_argument('command', choices=['serve', 'worker', 'recovery', 'status', 'submit', 'notify', 'doctor', 'probe-codex',
                                          'resources', 'resource', 'allocation', 'capability-events', 'resource-graph', 'node',
                                          'mesh-hello', 'mesh-links', 'mesh-local', 'mesh-queue', 'mesh-task', 'mesh-delegate', 'provider', 'tool-release', 'network-inventory'])
    parser.add_argument('--text')
    parser.add_argument('--request-id')
    parser.add_argument('--action', help='Resource API method, not a native terminal restriction')
    parser.add_argument('--payload-file', help='Owned 0600 JSON outside git worktrees; no credential values in argv')
    parser.add_argument('--kind')
    parser.add_argument('--principal', help='Discovery filter only, never an actor override')
    parser.add_argument('--include-unavailable', action='store_true')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--after', type=int)
    parser.add_argument('--id', help='A2A message ID for an owner-bound task read')
    parser.add_argument('--task-id')
    parser.add_argument('--epoch', type=int)
    parser.add_argument('--call-id')
    parser.add_argument('--owner-config', help='Explicit owned private provider manifest; never model source/payload')
    parser.add_argument('--max-cycles', help='Optional positive finite serve count; provider serve only')
    parser.add_argument('--state-dir', help='Existing owned 0700 local tool-release state directory')
    parser.add_argument('--operation-id', help='Stable deployment identity; unknown outcomes are query-only')
    parser.add_argument('--candidate-config', help='Private single-adapter candidate manifest; stage only')
    parser.add_argument('--publication-config', help='Private metadata and actual self-test reference; stage only')
    parser.add_argument('--retained-operation-id', help='Retained release identity; prepare-rollback only')
    parser.add_argument('--node-id', help='Local inventory label only, never an authenticated actor override')
    parser.add_argument('--freshness-seconds', type=float, help='Local inventory observation TTL; default 30, maximum 300')
    args = parser.parse_args()
    if args.command == 'network-inventory':
        return _network_command(args)
    if args.node_id is not None or args.freshness_seconds is not None:
        parser.error('network_options_require_network_inventory_command')
    if not args.config:
        parser.error('the following arguments are required: --config')
    if args.command == 'tool-release':
        return _tool_command(args)
    if any(getattr(args, name) is not None for name in ('state_dir', 'operation_id', 'candidate_config',
                                                     'publication_config', 'retained_operation_id')):
        parser.error('tool_options_require_tool_release_command')
    if args.command == 'provider':
        return _provider_command(args)
    if args.owner_config is not None or args.max_cycles is not None:
        parser.error('provider_options_require_provider_command')
    config = private_json(args.config)
    if args.command == 'serve':
        from .server import serve
        return serve(config)
    if args.command == 'worker':
        from .worker import Worker
        return Worker(config, config_path=str(Path(args.config).expanduser().resolve())).run()
    if args.command == 'node':
        from .node import run
        return run(config, str(Path(args.config).expanduser().resolve()))
    if args.command == 'recovery':
        from .recovery import run
        return run(config)
    if args.command in ('status', 'submit', 'notify', 'resources', 'resource', 'allocation', 'capability-events', 'resource-graph',
                        'mesh-hello', 'mesh-links', 'mesh-local', 'mesh-queue', 'mesh-task', 'mesh-delegate'):
        from .worker import Client
        client = Client(config)
        if args.command in ('mesh-hello', 'mesh-links'):
            value = client.request('/v1/mesh/' + args.command[5:])
        elif args.command == 'mesh-task':
            if not args.id:
                parser.error('mesh-task requires --id')
            value = client.request('/v1/mesh/task?' + urlencode({'id': args.id}))
        elif args.command in ('mesh-local', 'mesh-queue', 'mesh-delegate'):
            if not args.payload_file:
                parser.error(args.command + ' requires --payload-file')
            # Reuse the same regular/owned/0600 and outside-worktree gate,
            # without interpreting model text as sender identity or permission.
            wrapper = resource_payload(args.payload_file, 'mesh-payload')
            payload = wrapper['arguments']
            if args.command == 'mesh-delegate':
                if not args.task_id or args.epoch is None or not args.call_id:
                    parser.error('mesh-delegate requires task-id, epoch, call-id')
                if set(payload) != {'peer', 'arguments'}:
                    parser.error('mesh-delegate payload requires peer and arguments')
                payload = dict(payload, task_id=args.task_id, epoch=args.epoch,
                               call_id=args.task_id + ':' + args.call_id)
            value = client.request('/v1/mesh/' + args.command[5:], payload)
        elif args.command == 'status':
            value = client.request('/v1/status')
        elif args.command == 'submit':
            value = client.request('/v1/tasks', {'input': args.text})
        elif args.command == 'notify':
            if not args.request_id:
                parser.error('notify requires --request-id')
            value = client.request('/v1/notify', {'text': args.text, 'request_id': args.request_id})
        elif args.command in ('resource', 'allocation'):
            if args.principal is not None:
                parser.error('--principal is a discovery filter, not a resource actor override')
            try:
                payload = resource_payload(args.payload_file, args.action)
            except ValueError as exc:
                parser.error(str(exc))
            value = client.request('/v1/' + args.command + '/action', payload)
        else:
            routes = {'resources': '/v1/resources', 'capability-events': '/v1/capability-events',
                      'resource-graph': '/v1/resource/graph'}
            query = {k: getattr(args, k) for k in ('limit', 'after', 'kind', 'principal') if getattr(args, k) is not None}
            if args.include_unavailable:
                query['include_unavailable'] = '1'
            value = client.request(routes[args.command] + ('?' + urlencode(query) if query else ''))
    elif args.command == 'doctor':
        try:
            root = discover_codex_auth(config.get('codex', {}).get('auth_home'))
            value = {'codex_auth_found': True, 'source': root, 'auth_live_verified': False}
        except ValueError:
            value = {'codex_auth_found': False, 'action': 'Ask owner to authenticate Codex; never request plaintext tokens.'}
    else:
        from .codex import Codex
        with Codex(config['codex']) as agent:
            value = {'account': agent.account(), 'models': [m.get('id') for m in agent.models()]}
    print(json.dumps(value, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
