"""Owner-authorized, no-recording native Live probe on the existing Leader.

Stop the owned idle worker FIRST; this script refuses competing running tasks.
It obtains a real authority lease and restores ONLY the selected native rollout.
"""
import argparse
import json
import time
import uuid
from pathlib import Path

from assistant_mesh import sessions
from assistant_mesh.codex import Codex
from assistant_mesh.config import private_json
from assistant_mesh.live import CodexLiveTransport, LiveError, LiveSession
from assistant_mesh.worker import Client


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker-config', required=True)
    parser.add_argument('--operator-config', required=True)
    parser.add_argument('--communication-home', required=True)
    args = parser.parse_args()
    config = private_json(args.worker_config)
    operator, client = Client(private_json(args.operator_config)), Client(config)
    status = operator.request('/v1/status')
    if any(status['tasks'].get(key, 0) for key in ('running', 'pending', 'continuing', 'waiting_children')):
        raise ValueError('probe_requires_idle_authority')
    task_id = 'live-probe-' + uuid.uuid4().hex
    operator.request('/v1/tasks', {'id': task_id, 'input': '原生 Live 通信探测：仅合成文本，不录音、不运行工具。',
                                  'context': {'session_scope': 'leader:owner', 'source': 'synthetic-live-probe'}})
    client.request('/v1/heartbeat', {'capabilities': config['capabilities']})
    task = client.request('/v1/claim', {})['task']
    if not task or task['id'] != task_id:
        if task:
            client.request('/v1/task/update', {'id': task['id'], 'epoch': task['epoch'], 'status': 'waiting_backend'})
        raise ValueError('probe_did_not_acquire_own_task')
    native = (task.get('sessions') or {}).get('codex') or task.get('session')
    agent, live, alive, last_tick = None, None, True, [0]
    evidence = {'same_thread': False, 'primary_account_pinned': True, 'audio_capture_started': False}

    def tick(force=False):
        if force or time.monotonic() - last_tick[0] >= 10:
            client.request('/v1/heartbeat', {'capabilities': config['capabilities']})
            client.request('/v1/task/update', {'id': task_id, 'epoch': task['epoch']})
            last_tick[0] = time.monotonic()
        return True

    def lease(binding=None):
        try:
            return alive and tick()
        except Exception:
            return False

    code, completed = None, False
    try:
        if not native or not native.get('artifact') or not native['state'].get('thread_id'):
            raise ValueError('existing_native_leader_artifact_required')
        tick(force=True)
        evidence['phase'] = 'open-native'
        selected = dict(config['codex'], auth_home=args.communication_home, strict_auth_home=True,
                        sandbox='read-only', approval_policy='never')
        agent = Codex(selected)
        evidence['phase'] = 'restore-selected-rollout'
        restored = sessions.restore(client, task, str(Path(agent.auth_home) / 'mesh-live-imports'), tick)
        native_id = native['state']['thread_id']
        evidence['phase'] = 'resume-original-thread'
        thread = agent.rpc('thread/resume', {'threadId': native_id, 'path': restored,
                                           'cwd': selected['workspace'], 'sandbox': 'read-only',
                                           'approvalPolicy': 'never', 'excludeTurns': True})['thread']
        if thread['id'] != native_id:
            raise ValueError('native_leader_identity_changed')
        agent.thread_id, agent.rollout_path = native_id, thread.get('path')
        evidence['same_thread'] = True
        evidence['phase'] = 'start-native-live'
        transport = CodexLiveTransport(agent, native_id, lease, expected_auth_home=args.communication_home)
        allowed = {'connect', 'send_text', 'read_transcript'}
        def observe(event):
            if event.get('role') == 'assistant' and event.get('type') == 'transcript':
                evidence['assistant_transcript_events'] = evidence.get('assistant_transcript_events', 0) + 1
                if 'LIVE_TEXT_OK' in event.get('text', ''):
                    evidence['synthetic_response_marker_seen'] = True
        live = LiveSession(transport, {'thread_id': native_id, 'scope': 'leader:owner',
            'principal': 'operator', 'source': 'synthetic-text', 'peer': 'owner'},
            authorize=lambda binding, action: action in allowed, lease_check=lease, on_event=observe)
        live.start(output_modality='text', timeout=30)
        live.wait_started(timeout=30)
        live.append_text('这是本人授权的合成文本连通探测。只用一句话回答 LIVE_TEXT_OK，不调用工具，不创建或修改任何其它任务或目标。', task_id)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and live.state not in ('closed', 'error', 'detached'):
            live.step(timeout=1)
            if evidence.get('synthetic_response_marker_seen'):
                completed = True
                break
    except Exception as exc:
        code = str(exc) if isinstance(exc, LiveError) else 'native_live_probe_failed'
        if agent and getattr(agent, 'last_protocol_error', None):
            error = agent.last_protocol_error
            evidence['native_rpc_error_code'] = error.get('code') if isinstance(error, dict) else None
            message = str(error.get('message', '')).lower() if isinstance(error, dict) else ''
            evidence['diagnostic_class'] = next((label for keyword, label in (
                ('experimental', 'experimental_feature_gate'), ('not supported', 'unsupported'),
                ('not enabled', 'feature_not_enabled'), ('unauthorized', 'authentication_rejected'),
                ('not found', 'native_thread_or_route_missing'), ('thread not loaded', 'native_thread_not_loaded')) if keyword in message), 'protocol_rejected')
            evidence['diagnostic_flags'] = {word: word in message for word in ('rollout', 'path', 'thread', 'invalid', 'auth', 'unsupported', 'no such', 'not loaded')}
    finally:
        if live:
            try:
                live.stop(timeout=10)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not live.evidence['native_closed'] and live.state != 'detached':
                    live.step(timeout=1)
            except Exception:
                evidence['close_not_confirmed'] = True
            evidence['live'] = live.snapshot()
        if agent:
            try:
                state = dict(native['state'], thread_id=agent.thread_id, codex_node=config['node_id'],
                             codex_auth_home=agent.auth_home, side_effect_started=False)
                state['goal'] = agent.goal()
                sessions.save(client, task, config['node_id'], 'codex', state, agent.native_rollout(), tick)
            except Exception:
                evidence['native_snapshot_not_promoted'] = True
            agent.close()
        try:
            client.request('/v1/task/update', {'id': task_id, 'epoch': task['epoch'],
                'checkpoint': {'live_probe': evidence, 'side_effect_started': False},
                'status': 'completed' if completed else 'failed',
                'result': 'Live 合成文本探测：' + ('已观察到助手合成标记回复；音频未验收。' if completed else '未证明文本往返（' + (code or 'no_assistant_response_marker') + '）；未录音或拨号。')})
        finally:
            alive = False
    print(json.dumps({'task_id': task_id, 'completed': completed, 'error': code, 'evidence': evidence}, ensure_ascii=False))


if __name__ == '__main__':
    main()
