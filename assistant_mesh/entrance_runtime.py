"""Owner-only launch adapters for the frozen Claw API and native Worker.

No daemon is started by import/construction, no second receiver is created,
and no source/credential/service is installed by these helpers.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

from .config import private_json
from .codex import Codex
from .entrance import (CONTRACT_SHA256, PROTOCOL, ROLE_REVISION,
                       entrance_store_class, validate_policy)
from .model_tools import result
from .worker import Client, Worker


ENTRANCE_INSTRUCTIONS = (
    '你是本人独立的低成本连续沟通入口，不是Mesh Leader，也不是机械转发器。'
    '先使用原生工具完整读取workspace/AGENTS.md的入口职责合同；持有文档不等于已读。'
    '每条新消息先核当前日期、双方最近聊天、最新未处理消息及上次实际回复，'
    'UTC存储、Asia/Shanghai理解，跨天要说清；平台原始时间未知就明确未知，不能拿处理时间代替。'
    '使用原生Codex会话/压缩保持记忆，不因每条消息新开会话或用摘要替换原历史。'
    '普通消息先由你理解：可自然回复、静默、做基础操作，必要才用entrance handoff转给Leader。'
    '已有完整输入且无需其它节点的简单搜索、表格整理或分析可直接完成并回复，不必机械转交。'
    '数据源在电脑/其它节点、要求保存到指定设备或已有集中业务文件时，即使任务简单也交Leader协调，'
    '不要另建脱离原事实源的副本；入口自身收发、媒体、回执和通道管理仍自行处理。'
    'handoff排队/受理不代表Leader正在执行，更不代表完成；先读回原任务状态再如实告诉本人。'
    '消息附件要通过entrance media取到私有文件，并实际用view_image/原生文件工具阅读。'
    '下载/文件名/SHA不是理解；未能处理某格式说明具体缺项，不泛称只能文字，不假称已看。'
    '内部故障/任务完成事件也先核当前任务和时间，通常静默或合并成有用更新，'
    '不要回复“在吗”时播昨天的任务，不要发重试/探针/任务ID模板或旧错误刷屏。'
    '最终文本默认留私有；真正回复本人要明确调用entrance reply，静默用silent。'
    'reply前读history取得latest_inbound_event_id，原回复unknown必须原ID查status，不能换ID重发。'
    '另有owner配置的Claw CLI可读history/context/status、按原ID发文字媒体和下载；能力不止reply。'
    '常规对话仍用reply防陈旧回复；恢复由原唯一authority/refresher处理，不自己开iLink轮询或复制账号。'
    '入口是推荐能力，不拦截任何原生Shell、文件、网络、MCP；Leader也保留直接Claw权限。'
    '在已有本人授权和零新增费用内自主使用工具；不新建付费key、不新增花费、不扩大第三人通讯。')


class EntranceWorker(Worker):
    """Original run_once/session/artifact/guard lifecycle, one distinct scope."""
    def __init__(self, config, **options):
        policy = validate_policy(config.get('entrance'))
        capabilities = config.get('capabilities')
        if (not isinstance(capabilities, list) or policy['capability'] not in capabilities
                or set(capabilities) & {'leader', 'agent', 'native.wechat.refresh'}):
            raise ValueError('entrance_worker_capabilities_required')
        child = copy.deepcopy(config)
        if not isinstance(child.get('codex'), dict):
            raise ValueError('entrance_native_codex_configuration_required')
        # This is child-local role composition, not a global/native permission
        # change or edits to the incumbent Leader's configuration/auth files.
        codex = child['codex']
        if (not isinstance(codex.get('auth_home'), str)
                or not Path(codex['auth_home']).is_absolute()
                or codex.get('strict_auth_home') is not True):
            raise ValueError('entrance_explicit_native_auth_home_required')
        prefix = codex.get('instructions', '')
        if not isinstance(prefix, str):
            raise ValueError('invalid_developer_instructions')
        codex['instructions'] = prefix + ('\n\n' if prefix else '') + ENTRANCE_INSTRUCTIONS
        # An entrance notification is bounded conversation work, not permission
        # to inherit or create a permanent native Goal from another worker.
        if codex.get('goal') is not None:
            raise ValueError('entrance_worker_must_not_inherit_leader_goal')
        accounts = child.get('codex_accounts')
        if accounts and (not isinstance(accounts, list) or any(
                not isinstance(account, dict) or not isinstance(account.get('auth_home'), str)
                or not Path(account['auth_home']).is_absolute() for account in accounts)):
            raise ValueError('entrance_explicit_native_auth_home_required')
        if accounts and any(account.get('goal') is not None for account in accounts):
            raise ValueError('entrance_worker_must_not_inherit_leader_goal')
        self.entrance_policy = policy
        from .entrance_failure import closed_failure_backend
        backend = options.pop('backend', Codex)
        wrapped = closed_failure_backend(backend, self._save_closed_failure, self._failure_prefixes)
        super().__init__(child, backend=wrapped, **options)

    def _failure_prefixes(self):
        return (self.current or {}).get('context', {}).get('unsettled_previous_operations', [])

    def on_activity(self, kind, value):
        output = super().on_activity(kind, value)
        if kind == 'session_ready' and self.current is not None:
            # Frozen3198's read-only sandbox branch predates its stricter
            # native-start guard. Network/MCP effects are possible even there.
            # This callback precedes actual turn/start; no original task/native
            # tool is replayed or intercepted by this additional managed guard.
            self.current['checkpoint']['side_effect_started'] = True
            self.client.request('/v1/task/update', {'id': self.current['id'],
                'epoch': self.current['epoch'], 'checkpoint': self.current['checkpoint']})
        return output

    def _save_closed_failure(self, agent, rollout, proof):
        """Called only after actual owned native close, before old Worker catch.

        Save all raw failed-history bytes through the incumbent artifact upload;
        neither task status nor side_effect_started is relaxed. Lost ACK stops.
        """
        from . import sessions
        from .entrance_failure import history_snapshot
        task = self.current
        if task is None or task.get('scope') != self.entrance_policy['scope']:
            return
        state = dict(task['checkpoint'], thread_id=agent.thread_id, turn_id=agent.turn_id,
            harness='codex', codex_node=self.config['node_id'], codex_auth_home=agent.auth_home,
            native_rollout_path=str(rollout), side_effect_started=True,
            entrance_closed_failure=proof)

        def lease_tick():
            self.client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch']})

        saved = sessions.save(self.client, task, self.config['node_id'], 'codex', state,
                              rollout, lease_tick)
        if (not isinstance(saved, dict) or ('ok' in saved and saved['ok'] is not True)
                or saved.get('saved') is not True
                or saved.get('artifact_saved') is not True
                or saved.get('scope') != task['scope']
                or history_snapshot(rollout, agent.thread_id) != proof['history']):
            return
        self.client.request('/v1/agent/action', {'task_id': task['id'], 'epoch': task['epoch'],
            'call_id': task['id'] + ':closed-failure:' + str(task['epoch']),
            'action': 'entrance_record_failure', 'arguments': {'proof': proof}})

    def resource_reference(self):
        reference = super().resource_reference()
        reference['entrance'] = {
            'protocol': PROTOCOL, 'role_revision': ROLE_REVISION,
            'role_contract_sha256': CONTRACT_SHA256,
            'role_contract_path': str(Path(self.config['codex']['workspace']) / 'AGENTS.md'),
            'tool': 'mesh(action="entrance",arguments={action,arguments})',
            'actions': {
                'history': {'source': 'events/inbox/outbox', 'after': 0, 'limit': 50},
                'reply': {'text': 'natural owner reply', 'observed_latest_message_id':
                    'history.latest_inbound_event_id', 'reply_key': 'primary'},
                'silent': {'reason': 'private decision; not sent to owner'},
                'handoff': {'input': 'model-understood task for current Leader'},
                'status': {'event_id': 'original event ID'},
                'media': {'attachment_id': 'opaque staged attachment ID'}},
            'rules': ENTRANCE_INSTRUCTIONS,
            'history': '分页可读全部已授权原inbox/outbox与新入口事件；不把本地归档称完整云端历史。'
                '原生wechat-personal native messages也可读取已同步双向史/原消息时间，'
                '不为读取另开poller、不重登或消费iLink游标。',
            'media': 'materialize返回私有文件引用，不等于已理解；实际阅读文件再回复。'
                '路径属于入口执行节点，不可假称别的节点已收到文件。',
            'reply': '原生final不会自动发送；显式reply/silent/handoff。'
                '原IDunknown仅status；新消息到来先刷新上下文再决定，避免跨天/错话。',
            'native_tools_intercepted': False}
        reference['claw_cli'] = {'module': 'assistant_mesh.claw_cli',
            'config_path': getattr(self, 'config_path', None), 'task_id': (self.current or {}).get('id'),
            'epoch': (self.current or {}).get('epoch'),
            'arguments': '--config CONFIG --task-id TASK --epoch EPOCH --call-id ORIGINAL_CALL COMMAND',
            'commands': ['status', 'history', 'context', 'send-status', 'send-text', 'send-media',
                         'download', 'recover-context'],
            'backend': 'owner-enabled original authority; no operator bearer, new poller or local fallback',
            'same_execution_node_files': True, 'native_features_restricted': False}
        return reference

    def on_tool(self, call):
        arguments = call.get('arguments', {})
        name = call.get('tool')
        action, nested = None, None
        if name == 'mesh' and isinstance(arguments, dict) and arguments.get('action') == 'entrance':
            entry = arguments.get('arguments', {})
            if (set(arguments) - {'action', 'arguments'} or not isinstance(entry, dict)
                    or set(entry) - {'action', 'arguments'}
                    or not isinstance(entry.get('action'), str)
                    or not isinstance(entry.get('arguments', {}), dict)):
                return result({'error': 'entrance_tool_arguments_invalid'}, False)
            action, nested = entry['action'], entry.get('arguments', {})
        elif (name == 'mesh_notify' or (name == 'mesh' and isinstance(arguments, dict)
                                      and arguments.get('action') == 'notify')):
            # Keep the entrance's own legacy notify tool inside its associated
            # timeline. Other Workers and all native communication are untouched.
            action = 'reply'
            nested = arguments if name == 'mesh_notify' else arguments.get('arguments', {})
            if not isinstance(nested, dict) or set(nested) != {'text'}:
                return result({'error': 'entrance_tool_arguments_invalid'}, False)
            return result({'error': 'entrance_reply_requires_fresh_context',
                'instruction': '使用entrance history再reply，附observed_latest_message_id；不换ID重发unknown。',
                'native_tools_intercepted': False}, False)
        if action is None:
            return super().on_tool(call)
        if action not in ('history', 'reply', 'silent', 'handoff', 'status', 'media') or not self.current:
            return result({'error': 'entrance_tool_action_invalid'}, False)
        self.tick(force=True)
        try:
            output = self.client.request('/v1/agent/action', {
                'task_id': self.current['id'], 'epoch': self.current['epoch'],
                'call_id': self.current['id'] + ':' + call['callId'],
                'action': 'entrance_' + action, 'arguments': nested})
        except (OSError, ValueError) as error:
            return self._mesh_tool_failure(error, 'entrance', True)
        return result(output)


def media_from_config(config):
    options = config.get('entrance_media')
    if options is None:
        return None
    if not isinstance(options, dict) or set(options) - {
            'root', 'media_helper_file', 'media_helper_sha256', 'ilink_sha256', 'python'}:
        raise ValueError('entrance_media_owner_configuration_invalid')
    from .entrance_media import EntranceMedia, SkillMediaDownloader
    supplied = set(options) - {'root'}
    downloader = None
    if supplied:
        required = {'media_helper_file', 'media_helper_sha256', 'ilink_sha256'}
        if not required <= supplied:
            raise ValueError('entrance_media_owner_configuration_invalid')
        # Exact constructor is checked in the sibling's independently reviewed
        # adapter; no dynamic module/class comes from a message or task payload.
        downloader = SkillMediaDownloader(options['media_helper_file'],
            options['media_helper_sha256'], options['ilink_sha256'],
            **({'python': options['python']} if 'python' in options else {}))
    return EntranceMedia(options['root'], downloader=downloader)


def serve_entrance(config, socket_path, ready=None):
    """Root's deliberate replacement of its ONE receiver; no deploy/handoff.

    Use only an independently built frozen-source candidate. Retain the exact
    legacy owner IPC/lifecycle; no account duplication, new carrier or grant.
    """
    from . import server
    from .legacy_channel_owner import LegacyChannelOwnerControl, _SERVE_LOCK
    from .legacy_fenced_receiver import _check_frozen_source
    validate_policy(config.get('entrance'))
    if (not config.get('ilink_account') or 'channel_carrier' in config
            or config.get('notification_policy', {}).get('mode', 'channel') != 'channel'
            or (ready is not None and not callable(ready))):
        raise ValueError('entrance_original_legacy_receiver_required')
    _check_frozen_source(server)
    media = media_from_config(config)
    original, control = server.Store, None
    selected = entrance_store_class(original, config['entrance'], media)
    if config.get('claw_cli') is not None:
        options = config['claw_cli']
        if (not isinstance(options, dict) or set(options) - {'enabled', 'max_bytes'}
                or type(options.get('enabled')) is not bool):
            raise ValueError('claw_cli_owner_configuration_required')
        if options['enabled']:
            from .claw_cli import claw_store_class
            from .claw_upload import uploader_from_config
            selected = claw_store_class(selected, uploader_from_config(config),
                options.get('max_bytes', 32 * 1024 * 1024), any(
                    'native.wechat.refresh' in peer.get('capabilities', []) for peer in config.get('peers', [])))
    if not _SERVE_LOCK.acquire(False):
        raise ValueError('owner_service_already_running')

    def passive(path, **options):
        options['recover_inflight'] = False
        return selected(path, **options)

    def connected(http_server, api, channel):
        if server.Store is not passive:
            raise ValueError('entrance_source_factory_changed')
        server.Store = original
        control.attach(http_server, api, channel)
        if ready:
            ready(http_server, api, channel)

    try:
        control = LegacyChannelOwnerControl(socket_path)
        server.Store = passive
        return server.serve(config, ready=connected)
    finally:
        if server.Store is passive:
            server.Store = original
        if control:
            control.close()
        _SERVE_LOCK.release()


def run_entrance_worker(config, config_path=None):
    # This leaf's EOF/start/save reasoning was audited against these actual
    # legacy implementations, not whatever future modules happen to be on PATH.
    # A future native version needs its own adaptation, never a wider host gate.
    pins = {'codex.py': 'b32e2ba28c2c06ce1b1208a023de01ddd56081c012d9b5723e1158449bff1f98',
        'worker.py': '816a8eb2f6bc5a24a22ae345b1b7b94b0dd5d0ce52f99a6658688e683ce9cbf0',
        'sessions.py': '37f5b647c5ddb11018907311bef1e60e512b0d5259d78ca188b3f93e3ecb33ec'}
    package = Path(__file__).resolve().parent
    if any(hashlib.sha256((package / name).read_bytes()).hexdigest() != expected
           for name, expected in pins.items()):
        raise ValueError('entrance_frozen_native_adapter_required')
    return EntranceWorker(config, config_path=config_path).run()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Opt-in continuous native Claw entrance; no installer')
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='command')
    serve = sub.add_parser('serve')
    serve.add_argument('--socket', required=True)
    sub.add_parser('worker')
    action = sub.add_parser('action')
    action.add_argument('--task-id', required=True)
    action.add_argument('--epoch', type=int, required=True)
    action.add_argument('--call-id', required=True)
    action.add_argument('--action', required=True,
        choices=('history', 'reply', 'silent', 'handoff', 'status', 'media', 'notice'))
    action.add_argument('--payload-file', required=True)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error('command required')
    try:
        config = private_json(args.config)
        if args.command == 'serve':
            serve_entrance(config, args.socket)
        elif args.command == 'worker':
            run_entrance_worker(config, config_path=args.config)
        else:
            output = Client(config).request('/v1/agent/action', {
                'task_id': args.task_id, 'epoch': args.epoch, 'call_id': args.call_id,
                'action': 'entrance_' + args.action, 'arguments': private_json(args.payload_file)})
            print(json.dumps(output, ensure_ascii=False))
    except Exception:
        # No private paths/account/errors accidentally enter daemon/owner logs.
        print(json.dumps({'ok': False, 'error': 'entrance_operation_unconfirmed',
            'automatic_retry': False, 'native_tools_intercepted': False}), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
