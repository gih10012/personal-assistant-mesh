import json
import hashlib
import http.client
import io
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .codex import Codex, CodexError
from .config import private_json, read_secret
from .model_tools import TOOLS, result
from .runtime_health import diagnose as diagnose_runtime
from . import sessions


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('control_redirect_blocked')


MAX_CONTROL_RESPONSE_BYTES = 8 * 1024 * 1024


def _checked_unix_socket(path):
    selected = Path(path)
    if any(parent.is_symlink() for parent in selected.parents):
        raise ValueError('unix_socket_requires_non_symlink_parent')
    parent = selected.parent.lstat()
    target = selected.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        raise ValueError('unix_socket_requires_private_owned_0700_parent')
    if (not stat.S_ISSOCK(target.st_mode) or target.st_uid != os.getuid()
            or stat.S_IMODE(target.st_mode) != 0o600):
        raise ValueError('unix_socket_requires_private_owned_0600_socket')
    return str(selected)


class _UnixHTTPConnection(http.client.HTTPConnection):
    """Private IPC transport; URL metadata never triggers TCP or TLS fallback."""
    def __init__(self, host, port, path):
        http.client.HTTPConnection.__init__(self, host, port, timeout=15)
        self.socket_path = path

    def connect(self):
        path = _checked_unix_socket(self.socket_path)
        child = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            child.settimeout(self.timeout)
            child.connect(path)
            self.sock = child
        except BaseException:
            child.close()
            raise


class Client:
    def __init__(self, config):
        self.url = config['control_url'].rstrip('/')
        parsed = urllib.parse.urlsplit(self.url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
            raise ValueError('control_requires_tls_or_loopback_tunnel')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_control_url')
        self.unix_socket = config.get('unix_socket')
        if self.unix_socket is not None:
            if (not isinstance(self.unix_socket, str) or not self.unix_socket or '\0' in self.unix_socket
                    or not Path(self.unix_socket).is_absolute() or '..' in Path(self.unix_socket).parts):
                raise ValueError('unix_socket_requires_absolute_path')
            if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise ValueError('unix_socket_requires_http_loopback_url')
            if not hasattr(socket, 'AF_UNIX'):
                raise ValueError('unix_socket_transport_unavailable')
            self.unix_socket = str(Path(self.unix_socket))
        self.parsed_url = parsed
        self.token = read_secret(config['token_file'])

    @staticmethod
    def _decode_response(response):
        length = response.headers.get('Content-Length')
        if length is not None:
            try:
                length = int(length)
            except (ValueError, TypeError):
                raise ValueError('control_response_invalid_length') from None
            if length < 0 or length > MAX_CONTROL_RESPONSE_BYTES:
                raise ValueError('control_response_too_large')
        value = response.read(MAX_CONTROL_RESPONSE_BYTES + 1)
        if len(value) > MAX_CONTROL_RESPONSE_BYTES:
            raise ValueError('control_response_too_large')
        return json.loads(value.decode('utf8'))

    def _unix_request(self, path, body, headers):
        connection = _UnixHTTPConnection(self.parsed_url.hostname, self.parsed_url.port, self.unix_socket)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request('POST' if body is not None else 'GET', self.parsed_url.path + path,
                               body=data, headers=headers)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise ValueError('control_redirect_blocked')
            if response.status >= 400:
                # Python 3.6's HTTPError.close cannot close a None fp. Keep an
                # empty owned response body, never the private server response.
                error = urllib.error.HTTPError(self.url + path, response.status, response.reason, response.headers, io.BytesIO())
                error.close()  # match existing Client semantics, no leaked body
                raise error
            return self._decode_response(response)
        except http.client.HTTPException:
            raise ValueError('control_http_protocol_invalid') from None
        finally:
            connection.close()

    def request(self, path, body=None):
        headers = {'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'}
        if self.unix_socket is not None:
            return self._unix_request(path, body, headers)
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        # Mesh tokens never enter model context or command line arguments.
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=15) as response:
                return self._decode_response(response)
        except urllib.error.HTTPError as exc:
            exc.close()  # rejected RPCs must not leak response sockets/files
            raise


class Worker:
    def __init__(self, config, client=None, backend=Codex, config_path=None):
        self.config = config
        self.client = client or Client(config)
        self.backend = backend
        self.stop = threading.Event()
        self.current = None
        self.last_tick = 0
        self.wait_children = False
        self.agent = None
        self.polling_steering = False
        self.config_path = None
        if config_path is not None:
            private_json(config_path)  # the handle is owned/0600, not task input
            self.config_path = str(Path(config_path).expanduser().resolve())

    def heartbeat(self):
        return self.client.request('/v1/heartbeat', {'capabilities': self.config.get('capabilities', ['leader', 'agent'])})

    def _mesh_tool_failure(self, exc, domain, task_fenced):
        """Extra Mesh RPC failure is not proof of non-execution or a native gate."""
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        if status == 409 and task_fenced:
            # HTTPError bodies are deliberately closed/private. Recheck the
            # same task/Leader fence without a new action ID or result mutation.
            # If this core check fails (including transport), keep failing closed
            # for this managed task rather than assuming its lease is current.
            lease = self.client.request('/v1/task/update', {'id': self.current['id'], 'epoch': self.current['epoch']})
            if not isinstance(lease, dict) or lease.get('ok') is not True:
                raise ValueError('task_fence_response_invalid')
        error = domain + '_unavailable'
        if status in (400, 401, 403):
            error = domain + '_rejected'
        elif status == 409:
            error = domain + '_conflict' if task_fenced else domain + '_rejected'
        elif status == 404 and domain == 'resource_authority':
            error = 'resource_authority_upgrade_required'
        failure = {'error': error, 'availability': 'unavailable', 'outcome': 'unknown',
                   'automatic_retry': False, 'retry_with_new_id': False,
                   'native_tools_intercepted': False,
                   'retry_guidance': '未知结果可能已经提交；先核对原操作状态，保留同一操作身份，不因失败新建 ID 重做。原生能力不受此 Mesh 入口失败限制。'}
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            failure['http_status'] = status
        return result(failure, False)

    def on_tool(self, call):
        arguments = call.get('arguments', {})
        if (call.get('tool') == 'mesh' and isinstance(arguments, dict)
                and arguments.get('action') == 'runtime_diagnose'):
            # This local observation neither executes a task nor changes its
            # lease. It remains usable when Mesh heartbeat is unavailable.
            if (set(arguments) - {'action', 'arguments'}
                    or not isinstance(arguments.get('arguments', {}), dict)
                    or arguments.get('arguments', {})):
                return result({'error': 'invalid_runtime_diagnose_arguments'}, False)
            try:
                return result(diagnose_runtime(self.config))
            except Exception:
                return result({'error': 'runtime_diagnosis_unavailable'}, False)
        self.tick(force=True)
        name = call['tool']
        arguments = call.get('arguments', {})
        if name == 'mesh':
            # Only adapt the additional mesh tool, never native terminal/MCP
            # calls. Resource kinds stay open and authority is server-bound.
            if (not isinstance(arguments, dict) or set(arguments) - {'action', 'arguments'}
                    or not isinstance(arguments.get('action'), str) or not arguments['action']
                    or not isinstance(arguments.get('arguments', {}), dict)):
                return result({'error': 'invalid_mesh_gateway_arguments'}, False)
            action, nested = arguments['action'], arguments.get('arguments', {})
            routing_actions = {'routing_context': ('context', {'kind', 'issuer', 'include_unavailable', 'limit', 'observation_max_age_seconds'}),
                               'route_propose': ('propose', {'decision'}), 'route_inspect': ('inspect', {'decision_id'}),
                               'route_list': ('list', {'limit'}), 'route_link': ('link', {'decision_id', 'kind', 'reference_id'})}
            if action in routing_actions:
                route_action, allowed = routing_actions[action]
                if set(nested) - allowed:
                    return result({'error': 'routing_arguments_or_runtime_identity_invalid'}, False)
                payload = {'task_id': self.current['id'], 'epoch': self.current['epoch'],
                           'action': route_action, 'arguments': nested}
                try:
                    output = self.client.request('/v1/routing/action', payload)
                except (OSError, ValueError) as exc:
                    return self._mesh_tool_failure(exc, 'routing_authority', True)
                return result(output)
            if action == 'federated_capabilities':
                if set(nested) - {'issuer', 'kind', 'include_unavailable', 'limit'}:
                    return result({'error': 'invalid_federated_capabilities_arguments'}, False)
                query = dict(nested)
                if 'include_unavailable' in query:
                    if type(query['include_unavailable']) is not bool:
                        return result({'error': 'invalid_federated_capabilities_arguments'}, False)
                    query['include_unavailable'] = '1' if query['include_unavailable'] else '0'
                try:
                    output = self.client.request('/v1/mesh/capability-projection' +
                                                 ('?' + urllib.parse.urlencode(query) if query else ''))
                except (OSError, ValueError) as exc:
                    return self._mesh_tool_failure(exc, 'catalog_authority', False)
                return result(output)
            if action == 'allocation':
                # Only the extra Mesh contract uses this admission journal;
                # native tools never pass through it. Reserve is bound here to
                # the actual running task, not model-supplied task identities.
                if (set(nested) - {'action', 'arguments'}
                        or not isinstance(nested.get('action'), str)
                        or not isinstance(nested.get('arguments', {}), dict)):
                    return result({'error': 'invalid_mesh_allocation_arguments'}, False)
                payload = dict(nested, arguments=dict(nested.get('arguments', {})))
                if payload['action'] == 'reserve':
                    if {'task_id', 'task_epoch'} & set(payload['arguments']):
                        return result({'error': 'mesh_allocation_task_identity_is_runtime_bound'}, False)
                    payload['arguments'].update(task_id=self.current['id'], task_epoch=self.current['epoch'])
                try:
                    output = self.client.request('/v1/allocation/action', payload)
                except (OSError, ValueError) as exc:
                    return self._mesh_tool_failure(exc, 'allocation_authority', payload['action'] == 'reserve')
                return result(output)
            elif action in ('remote_delegate', 'delegate', 'children', 'wait_children', 'remember', 'recall', 'notify'):
                name, arguments = 'mesh_' + action, nested
            else:
                name = 'mesh_resource'
                arguments = nested if action == 'resource' else {'action': action, 'arguments': nested}
        if name not in {t['name'] for t in TOOLS}:
            return result({'error': 'unknown_mesh_tool'}, False)
        if name == 'mesh_remote_delegate':
            arguments = dict(arguments)
            peer = arguments.pop('peer', None)
            if not isinstance(peer, str) or not peer:
                return result({'error': 'remote_delegation_peer_required'}, False)
            try:
                output = self.client.request('/v1/mesh/delegate', {
                    'task_id': self.current['id'], 'epoch': self.current['epoch'],
                    'call_id': self.current['id'] + ':' + call['callId'], 'peer': peer, 'arguments': arguments})
            except (OSError, ValueError) as exc:
                return self._mesh_tool_failure(exc, 'remote_delegation', True)
            return result(output)
        if name == 'mesh_resource':
            try:
                output = self.client.request('/v1/resource/action', arguments)
            except (OSError, ValueError) as exc:
                # This API has resource epochs, not a task lease of its own.
                # Its business 409 retains the existing resource semantics.
                return self._mesh_tool_failure(exc, 'resource_authority', False)
            return result(output)
        try:
            output = self.client.request('/v1/agent/action', {
                'task_id': self.current['id'], 'epoch': self.current['epoch'],
                'call_id': self.current['id'] + ':' + call['callId'],
                'action': name[len('mesh_'):], 'arguments': arguments})
        except (OSError, ValueError) as exc:
            return self._mesh_tool_failure(exc, 'mesh_coordination', True)
        if name == 'mesh_wait_children':
            self.wait_children = True
        return result(output)

    def resource_reference(self):
        """Small discovery summary plus a native CLI path for existing threads.

        Older native rollouts retain their original dynamicTools. No new thread
        or application-authored memory summary is created to add this feature.
        """
        reference = {'purpose': 'mesh 是额外提供的能力/协调入口，不拦截原生 Shell、文件、网络、MCP 或任何原生功能；未登记仍可自主发现或创建工具。',
                     'fresh_thread_tool': 'mesh(action, arguments)',
                     'legacy_tool': 'mesh_resource(action, arguments)',
                     'gateway': {'coordination_actions': ['remote_delegate', 'delegate', 'children', 'wait_children', 'remember', 'recall', 'notify'],
                         'resource_wrapper': {'action': 'resource', 'arguments': {'action': 'resource API action', 'arguments': {}}},
                         'allocation_wrapper': {'action': 'allocation', 'arguments': {'action': 'reserve/inspect/pending/accept/start/unknown/settle/decline/cancel', 'arguments': {}}},
                         'allocation_execution': '共享容量预留和provider回执是受管Mesh合同；start不是工具执行证明。没有实际provider适配器不得宣称完成；未知或running占用不会仅因TTL自动释放。',
                         'native_tools_intercepted': False,
                         'execution': '目录/graph/authorize 只表示声明、证据或权限检查，不自动执行能力；queued/allowed 不等于完成。',
                         'notifications': 'notify 是显式通知本人；使用配置且授权的本人通道/节点回传，private 模式没有回传时 recorded_private。private 节点自动结果不自动转发；queued/accepted 不证明手机送达。',
                         'connectivity': 'mesh 内入口的授权不限制原生网络路线；失联可继续用其他本人授权的原生路线自主恢复连接。'},
                     'actions': ['discover', 'describe', 'graph', 'audit', 'advertise', 'renew', 'observe', 'link',
                                 'revoke', 'request_grant', 'authorize'],
                     'authority': 'actor 由 peer 固定；跨主体 grant 仅 owner operator 审批。声明/health 不是验证或执行。'}
        reference['runtime_access'] = {
            'local_read_tool': {'action': 'runtime_diagnose', 'arguments': {}},
            'purpose': '可选的本节点运行包布局诊断，不读凭据、不联网或推理；不是原生工具前置门禁。'
                       'complete 仅表示布局存在，不证明完整性、Shell、模型或网络可用。'
                       '下列命令是推荐句柄，未自动执行；原生 Shell/网络和创建任意工具仍保留。',
            'automatic_repair': False, 'task_replayed': False, 'session_replaced': False}
        reference['federation'] = {
            'tool': 'mesh(action="federated_capabilities",arguments={issuer,kind,include_unavailable,limit})',
            'cli_command': 'mesh-capabilities',
            'purpose': '可选已 enrollment peer 的只读能力投影；issuer/revision/时效为来源声明，不是本地授权或容量。'
                       '模型自主选择证据/节点；远端执行仍用 remote_delegate 在资源 owner authority 准入与结算。'
                       '不同 issuer 的同名能力不合并，重联不重放 unknown；连接在线不证明推理/互联网/性能。',
            'managed_invocation_authorized': False}
        reference['routing'] = {
            'context': 'mesh(action="routing_context",arguments={kind,issuer,include_unavailable,limit,observation_max_age_seconds})',
            'propose': 'mesh(action="route_propose",arguments={decision:{decision_id,work,candidates,selection,rationale,evidence_refs}})',
            'inspect': 'mesh(action="route_inspect",arguments={decision_id})',
            'list': 'mesh(action="route_list",arguments={limit})',
            'link': 'mesh(action="route_link",arguments={decision_id,kind,reference_id})',
            'purpose': '任务绑定的证据快照和不可变模型提议，不是评分器或执行授权。提议写入不会预留容量、批准grant、探测网络或执行。'
                       '证据摘要仍须核对来源、单位、scope/workload、时效和截断；远端声明不是本地共享容量。'
                       'link只核对同一任务原actor的实际remote_delegation或managed_allocation身份，不证明模型选择、计划匹配或业务成功。'
                       'unknown保留原operation/child ID查证，不换节点新ID重放；原生功能不受影响。',
            'cli_command': 'routing', 'historical_owner_read': 'route-decisions (operator/viewer only)',
            'model_selection_verified': False, 'managed_invocation_authorized': False}
        # Only fixed host states enter the prompt, never exception messages or
        # private response bodies. This is advisory, not a native-tool gate.
        steering = (self.current or {}).get('checkpoint', {}).get('mesh_steering_status', {})
        steering = steering if isinstance(steering, dict) else {}
        states = {'query': ('not_polled', 'available', 'unavailable', 'permission_denied', 'server_upgrade_required'),
                  'submission': ('none', 'submitted', 'unknown'), 'ack': ('none', 'acknowledged', 'unknown')}
        reference['steering_status'] = {key: steering.get(key) if steering.get(key) in values else values[0]
                                        for key, values in states.items()}
        if self.config_path is not None:
            reference['runtime_access']['cli'] = {
                'cwd': str(Path(__file__).resolve().parent.parent),
                'doctor': [sys.executable, '-m', 'scripts.runtime_doctor', '--config', self.config_path],
                'install': [sys.executable, '-m', 'scripts.upgrade_runtime',
                    '--artifact', '/PRIVATE/VERIFIED_COMPLETE_ARTIFACT.tar.gz',
                    '--sha256', 'VERIFIED_WHOLE_ARTIFACT_SHA256', '--version', 'VERIFIED_VERSION',
                    '--target', 'VERIFIED_TARGET', '--install-dir', '/PRIVATE/OWNED_RUNTIME'],
                'switch': [sys.executable, '-m', 'scripts.runtime_admin', '--config', self.config_path,
                    'set-cloud-runtime', '--package-dir', '/PRIVATE/VERIFIED_PACKAGE_DIR',
                    '--sha256', 'VERIFIED_WHOLE_ARTIFACT_SHA256'],
                'probe': [sys.executable, '-m', 'scripts.probe_native_shell', '--config', self.config_path],
                'requirements': 'install/switch 是环境变更，需明确完整官方包来源、版本/target/SHA256与本次维护授权；'
                                '配置切换不证明运行进程已采用新配置。probe 会进行原生推理和创建独立验收会话，'
                                '只能显式验收时执行，不替代或重放原业务 thread。配置/token 不得读取输出或写入公仓。'}
            reference['cli'] = {'cwd': str(Path(__file__).resolve().parent.parent),
                'argv': [sys.executable, '-m', 'assistant_mesh.cli', '--config', self.config_path, 'resources'],
                'resource_usage': '把 arguments JSON 写入 git 仓库之外本人所有的 0600 私有文件；'
                                  '把 resources 换成 resource --action NAME --payload-file /private/args.json。'
                                  '配置/token 保持在受保护文件中，不要读取或输出 token 值。',
                'allocation_usage': 'allocation --action NAME --payload-file /private/args.json；reserve需当前task_id/task_epoch，优先mesh allocation入口自动绑定。',
                'routing_usage': 'routing --task-id CURRENT_TASK --epoch CURRENT_EPOCH --action context/propose/inspect/list/link --payload-file /private/args.json；'
                                 'worker凭据及当前lease必需，JSON仅arguments。route-decisions --id DECISION或--task-id TASK是operator/viewer只读历史。',
                'routing_task_binding': {'task_id': self.current['id'], 'epoch': self.current['epoch']} if self.current else None}
            reference['a2a'] = {'fresh_thread_tool': 'mesh(action="remote_delegate",arguments={peer,input,project_id,agent_id})',
                'legacy_tool': 'mesh_remote_delegate(peer,input,project_id,agent_id)',
                'payload_shape': {'peer': 'owner-enrolled-peer', 'arguments': {'input': 'model-selected task',
                    'project_id': 'project', 'agent_id': 'continuous-specialist'}},
                'coordination': '用 mesh_children/mesh_wait_children 等待原父会话自动唤醒；未知 SSH 不等于可重试 A2A。'}
            if self.current:
                reference['a2a']['resume_cli'] = ['mesh-delegate', '--task-id', self.current['id'], '--epoch', str(self.current['epoch']),
                    '--call-id', 'stable-operation-id', '--payload-file', '/private/delegation.json']
        try:
            response = self.client.request('/v1/resources?limit=10')
            if (not isinstance(response, dict) or not isinstance(response.get('capabilities'), list)
                    or not all(isinstance(cap, dict) for cap in response['capabilities'][:10])):
                raise ValueError('optional_discovery_invalid_response')
            keys = ('id', 'kind', 'principal', 'epoch', 'health', 'verification', 'available', 'deadline')
            reference['summary'] = [dict({key: cap.get(key) for key in keys},
                                         description=str(cap.get('description', ''))[:300])
                                    for cap in response.get('capabilities', [])[:10]]
            reference['as_of'] = response.get('as_of')
            reference['discovery_status'] = 'available'
        except urllib.error.HTTPError as exc:
            reference['discovery_status'] = ('server_upgrade_required' if exc.code == 404 else
                                             'permission_denied' if exc.code in (401, 403) else 'unavailable')
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            reference['discovery_status'] = 'unavailable'
        return reference

    def open_backend(self):
        # Diagnostic-only: replace a prior task/attempt's candidate chain. Do
        # not retain configuration, account values, exception text or paths.
        diagnostics = []
        self.current.setdefault('checkpoint', {})['backend_candidates'] = diagnostics

        def publish_diagnostics():
            # Failed preflight never reaches the normal post-start tick. Its
            # safe observations still need a fenced, private ledger checkpoint.
            # This optional write must not replace the existing last_error or
            # become another native runtime / tool precondition.
            if 'id' not in self.current or 'epoch' not in self.current:
                return
            try:
                self.client.request('/v1/task/update', {'id': self.current['id'],
                    'epoch': self.current['epoch'],
                    'checkpoint': {'backend_candidates': [dict(row) for row in diagnostics]}})
            except (OSError, ValueError):
                pass

        accounts = self.config.get('codex_accounts')
        if accounts is not None and (not isinstance(accounts, list) or not accounts or not all(isinstance(a, dict) for a in accounts)):
            raise ValueError('invalid_codex_account_pool')
        candidates = [(self.backend, dict(self.config['codex'], **dict(account, strict_auth_home=True)), 'codex')
                      for account in accounts] if accounts else [(self.backend, self.config['codex'], 'codex')]
        if self.config.get('pi'):
            from .pi import Pi
            config = dict(self.config['pi'])
            config['mesh_grant'] = {k: self.config[k] for k in ('control_url', 'token_file')}
            config['mesh_grant'].update(task_id=self.current['id'], epoch=self.current['epoch'])
            candidates.append((Pi, config, 'pi'))
        last_error = None
        for index, (implementation, config, name) in enumerate(candidates):
            agent = None
            diagnostic = {'index': index, 'harness': name, 'state': 'preflight',
                          'cause': 'none', 'quota_status': 'not_checked'}
            diagnostics.append(diagnostic)
            stage = 'initialization'
            try:
                config = dict(config)
                if name == 'codex':
                    context = self.current.get('context', {})
                    if context.get('mode'):
                        config['mode'] = context['mode']
                    if context.get('goal'):
                        config['goal'] = dict(context['goal'])
                agent = implementation(config, tools=TOOLS, on_tool=self.on_tool, on_activity=self.on_activity,
                                       on_interaction=self.on_interaction)
                stage = 'account_rpc'
                if not agent.account()['authenticated']:
                    diagnostic.update(state='authentication_required', cause='authentication')
                    raise CodexError(name + '_auth_required')
                if name == 'codex' and callable(getattr(agent, 'rate_limits', None)):
                    try:
                        quota = agent.rate_limits()
                    except (ValueError, OSError):
                        quota = {'status': 'unknown'}  # optional API, not a native-tool gate
                    diagnostic['quota_status'] = (quota.get('status')
                        if isinstance(quota, dict) and quota.get('status') in ('available', 'exhausted') else 'unknown')
                    if isinstance(quota, dict) and quota.get('status') == 'exhausted':
                        diagnostic.update(state='quota_exhausted', cause='quota')
                        retry = quota.get('retry_at')
                        if (isinstance(retry, (int, float)) and not isinstance(retry, bool)
                                and 0 < retry < float('inf')):
                            diagnostic['retry_at'] = retry
                        raise CodexError('codex_usage_limit_exceeded')
                elif name == 'codex':
                    diagnostic['quota_status'] = 'unknown'
                else:
                    diagnostic['quota_status'] = 'not_applicable'
                diagnostic.update(state='selected', cause='none')
                publish_diagnostics()
                self.harness = name
                return agent
            except (ValueError, OSError) as exc:
                if diagnostic['state'] == 'preflight':
                    diagnostic.update(state='account_unavailable' if stage == 'account_rpc' else 'runtime_unavailable',
                                      cause=stage)
                publish_diagnostics()
                if agent:
                    agent.close()
                last_error = exc
        raise last_error

    def on_interaction(self, method, parameters):
        kind = 'question' if method == 'item/tool/requestUserInput' else 'approval'
        identity = hashlib.sha256(json.dumps([self.current['id'], self.current['epoch'], method, parameters], sort_keys=True).encode()).hexdigest()[:24]
        body = {'task_id': self.current['id'], 'epoch': self.current['epoch'], 'id': identity}
        value = self.client.request('/v1/interaction', dict(body, kind=kind, params=parameters))
        deadline = time.monotonic() + self.config.get('interaction_timeout', 3600)
        while time.monotonic() < deadline:
            self.tick()
            if value['answer'] is not None:
                return value['answer']
            self.stop.wait(2)
            value = self.client.request('/v1/interaction', body)
        raise CodexError('owner_response_pending')

    def on_activity(self, kind, value):
        if kind == 'session_ready':
            self.current['checkpoint'].update(value)
            if self.harness == 'codex' and self.agent:
                self.current['checkpoint']['codex_auth_home'] = self.agent.auth_home
            self.current['checkpoint']['codex_node'] = self.config.get('node_id')
            self.current['checkpoint']['harness'] = self.harness
            sessions.save(self.client, self.current, self.config.get('node_id'), self.harness,
                          self.current['checkpoint'])
            # Reserve BEFORE turn/start: native effects can precede item/started.
            if self.harness == 'pi' or self.config['codex'].get('sandbox') != 'read-only':
                self.current['checkpoint']['side_effect_started'] = True
            self.tick(force=True)
        elif kind == 'approval_required':
            self.current['checkpoint']['approval_required'] = True
            self.client.request('/v1/agent/action', {'task_id': self.current['id'], 'epoch': self.current['epoch'],
                'call_id': self.current['id'] + ':approval:' + str(value.get('itemId', 'request')),
                'action': 'notify', 'arguments': {'text': '任务 ' + self.current['id'][:8] + ' 需要本人审批；原生运行环境拒绝了未授权操作，请回复授权范围。'}})
        elif kind == 'plan_updated':
            self.current['checkpoint']['plan'] = value
            self.tick(force=True)

    def tick(self, force=False):
        if force or time.monotonic() - self.last_tick >= 15:
            self.heartbeat()
            if self.current:
                self.client.request('/v1/task/update', {'id': self.current['id'], 'epoch': self.current['epoch'],
                                                       'checkpoint': self.current['checkpoint']})
            self.last_tick = time.monotonic()
            if self.current and self.agent and getattr(self.agent, 'turn_id', None) and not self.polling_steering:
                self.polling_steering = True
                try:
                    body = {'task_id': self.current['id'], 'epoch': self.current['epoch']}
                    scope = (body['task_id'], body['epoch'])
                    if getattr(self, '_steering_scope', None) != scope:
                        self._steering_scope = scope
                        self._steering_receipts = {}
                        self._steering_status = {'query': 'not_polled', 'submission': 'none', 'ack': 'none'}
                    receipts, status = self._steering_receipts, self._steering_status

                    def acknowledge(identity, outcome):
                        try:
                            response = self.client.request('/v1/steering', dict(body, id=identity, state=outcome))
                            if not isinstance(response, dict) or response.get('ok') is not True:
                                raise ValueError('optional_steering_invalid_ack')
                        except urllib.error.HTTPError as exc:
                            if exc.code == 409:
                                raise  # steering also checks the current task lease
                            receipts[identity]['state'] = status['submission'] = 'unknown'
                            status['ack'] = 'unknown'
                            return
                        except (OSError, ValueError, TypeError, KeyError, AttributeError):
                            receipts[identity]['state'] = status['submission'] = 'unknown'
                            status['ack'] = 'unknown'
                            return
                        receipts[identity]['acked'] = True
                        status['ack'] = 'acknowledged'

                    # An ACK may have committed before its response was lost.
                    # Repair only that bookkeeping, never resend native steer.
                    # Bound optional repair to one request per heartbeat tick.
                    repairing = next((identity for identity, receipt in receipts.items() if not receipt['acked']), None)
                    if repairing is not None:
                        status['submission'] = 'unknown'
                        acknowledge(repairing, 'unknown')
                    try:
                        response = self.client.request('/v1/steering', body)
                        if not isinstance(response, dict) or 'steering' not in response:
                            raise ValueError('optional_steering_invalid_response')
                        event = response['steering']
                        if event is not None and (not isinstance(event, dict)
                                or not isinstance(event.get('id'), str) or not event['id']
                                or not isinstance(event.get('text'), str) or not event['text'].strip()
                                or len(event['text'].encode('utf8')) > 32768
                                or event.get('task_id', body['task_id']) != body['task_id']
                                or event.get('epoch', body['epoch']) != body['epoch']
                                or event.get('state', 'pending') not in ('pending', 'submitting', 'submitted', 'unknown')):
                            raise ValueError('optional_steering_invalid_event')
                    except urllib.error.HTTPError as exc:
                        if exc.code == 409:
                            raise
                        status['query'] = ('server_upgrade_required' if exc.code == 404 else
                                           'permission_denied' if exc.code in (401, 403) else 'unavailable')
                        return
                    except (OSError, ValueError, TypeError, KeyError, AttributeError):
                        status['query'] = 'unavailable'
                        return
                    status['query'] = 'available'
                    if event is not None:
                        identity = event['id']
                        if identity in receipts:
                            status['submission'] = receipts[identity]['state']
                            status['ack'] = 'acknowledged' if receipts[identity]['acked'] else 'unknown'
                            if not receipts[identity]['acked'] and identity != repairing:
                                acknowledge(identity, 'unknown')
                            return
                        if event.get('state', 'pending') != 'pending':
                            # Existing terminal/inflight ledger evidence is not
                            # permission to replay an already attempted event.
                            outcome = 'submitted' if event['state'] == 'submitted' else 'unknown'
                            receipts[identity] = {'state': outcome, 'acked': event['state'] != 'submitting'}
                            status['submission'] = outcome
                            status['ack'] = 'acknowledged' if receipts[identity]['acked'] else 'unknown'
                            if not receipts[identity]['acked']:
                                acknowledge(identity, 'unknown')
                            return
                        # Reserve BEFORE calling native RPC; a missing reply is
                        # unknown, not proof that the steer was never received.
                        receipts[identity] = {'state': 'unknown', 'acked': False}
                        status['submission'] = 'unknown'
                        try:
                            self.agent.steer(event['text'])
                        except (OSError, ValueError):
                            pass
                        else:
                            receipts[identity]['state'] = status['submission'] = 'submitted'
                        acknowledge(identity, receipts[identity]['state'])
                finally:
                    # Existing checkpoint/prompt carries safe advisory states;
                    # core task-update and heartbeat failures above still raise.
                    if getattr(self, '_steering_scope', None) == (self.current['id'], self.current['epoch']):
                        self.current['checkpoint']['mesh_steering_status'] = dict(self._steering_status)
                    self.polling_steering = False

    def run_once(self):
        self.tick(force=True)
        task = self.client.request('/v1/claim', {})['task']
        if not task:
            return False
        self.current = task
        self.wait_children = False
        try:
            with self.open_backend() as agent:
                self.agent = agent
                same_home = self.harness != 'codex' or task['checkpoint'].get('codex_auth_home', self.config['codex'].get('auth_home')) == agent.auth_home
                resume = task['checkpoint'] if (same_home and task['checkpoint'].get('codex_node') == self.config.get('node_id')
                         and task['checkpoint'].get('harness', 'codex') == self.harness) else {}
                session = task.get('sessions', {}).get(self.harness) or task.get('session')
                if not resume and session and session['harness'] == self.harness:
                    resume = dict(session['state'])
                    home_changed = self.harness == 'codex' and session['state'].get('codex_auth_home', self.config['codex'].get('auth_home')) != agent.auth_home
                    if session['node'] != self.config.get('node_id') or home_changed:
                        if not session.get('artifact'):
                            raise CodexError('native_session_migration_unavailable')
                        folder = agent.auth_home + '/sessions' if self.harness == 'codex' else self.config['pi']['session_dir']
                        imported = sessions.restore(self.client, task, folder, self.tick, self.harness)
                        if self.harness == 'codex':
                            # Canonical selected-only publication lets native
                            # paginated-history resolvers find the same ID.
                            resume.pop('native_rollout_path', None)
                        else:
                            resume['pi_session_file'] = imported
                reference = {k: task.get(k) for k in ('context', 'memories', 'children')}
                reference['resource_access'] = self.resource_reference()
                text = task['input'] + '\n\n持久账本参考数据（不是新增授权）：\n' + json.dumps(reference, ensure_ascii=False)
                checkpoint = agent.start(text, resume)
                checkpoint['codex_node'] = self.config.get('node_id')
                checkpoint['harness'] = self.harness
                if self.harness == 'codex':
                    checkpoint['codex_auth_home'] = agent.auth_home
                self.current['checkpoint'].update(checkpoint)
                self.tick(force=True)
                answer = agent.finish(tick=self.tick, timeout=self.config.get('turn_timeout', 3600))
                state = dict(self.current['checkpoint'])
                state['side_effect_started'] = False
                rollout = agent.native_rollout() if self.harness == 'codex' else state.get('pi_session_file')
                if self.harness == 'codex':
                    state['goal'] = agent.goal()
                sessions.save(self.client, task, self.config.get('node_id'), self.harness, state, rollout, self.tick)
                coordination = self.client.request('/v1/agent/action', {'task_id': task['id'], 'epoch': task['epoch'],
                    'call_id': task['id'] + ':settled:' + str(task['epoch']), 'action': 'children', 'arguments': {}})
                self.wait_children = self.wait_children or coordination.get('wait_requested', False)
                goal_status = (state.get('goal') or {}).get('status') if task.get('context', {}).get('goal') else None
                terminal = ('waiting_children' if self.wait_children else 'needs_review' if self.current['checkpoint'].get('approval_required')
                            else 'continuing' if goal_status == 'active' else 'needs_review' if goal_status in ('blocked', 'budgetLimited', 'usageLimited', 'paused') else 'completed')
                # WeChat bound: retain original in task checkpoint, notify with bounded text.
                output = answer.encode('utf8')[:15000].decode('utf8', errors='ignore')
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'checkpoint': {'answer': answer, 'side_effect_started': False, 'goal': state.get('goal')}, 'result': output,
                    'status': terminal})
        except (ValueError, OSError, urllib.error.URLError) as exc:
            # Losing the authority must terminate the local runtime before takeover.
            status = 'waiting_auth' if 'auth' in str(exc) else 'waiting_backend'
            code = str(exc) if isinstance(exc, CodexError) else 'worker_unavailable'
            try:
                self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'],
                    'result': '任务暂未完成：' + code + '。需要恢复认证或运行环境后继续。', 'status': status})
            except (OSError, ValueError, urllib.error.URLError):
                pass  # stale lease: authority owns recovery, not the abandoned worker
        finally:
            self.current = None
            self.agent = None
        return True

    def run(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except (OSError, ValueError, urllib.error.URLError):
                pass
            self.stop.wait(2)
