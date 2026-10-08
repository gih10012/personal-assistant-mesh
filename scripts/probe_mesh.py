"""Owner-run, zero-new-spend mesh smoke probe, using existing mesh APIs only.

Examples (run from the repository; configs/state must be private):
  python scripts/probe_mesh.py --local-config /private/operator.json \
    --peer-config /private/cloud-client.json --source laptop --destination cloud --health
  # Then append --state /private/smoke.json --phase remember --submit --poll
  # Later use the SAME state: --phase recall --submit --poll --verify-replay

Health is read-only. Submission is explicit and consumes an existing native
model's normal quota, never provisions credentials/models or uses a paid API.
The immutable private smoke envelope is not another task ledger. It permits
safe continuation with the same IDs after an observation timeout. No model
output, token, URL, or nonce is printed. Prompt-only read-only intent is not
misrepresented as a verified runtime restriction.
"""
import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
from pathlib import Path
from urllib.parse import urlencode

# Permit the documented direct script invocation without an installed package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant_mesh.config import private_json
from assistant_mesh.networking import PROTOCOL, digest, identifier
from assistant_mesh.worker import Client


FORMAT = 'mesh-smoke-envelope/1'
TERMINAL = ('completed', 'failed', 'needs_review')
STATUSES = TERMINAL + ('pending', 'running', 'paused', 'waiting_auth', 'waiting_backend',
                       'waiting_children', 'waiting_remote', 'continuing')


class ProbeError(ValueError):
    pass


def state_path(path):
    selected = Path(path).expanduser()
    if selected.is_symlink():
        raise ProbeError('smoke_state_requires_private_regular_file')
    resolved = selected.resolve()
    if any((parent / '.git').exists() for parent in resolved.parents):
        raise ProbeError('smoke_state_must_be_outside_git_worktrees')
    return selected


def envelopes(source, destination, run_id, project, agent, nonce):
    remember = ('这是纯文本连续上下文验证。请在当前对话上下文记住随机校验字串：' + nonce +
                '。本轮只回复 ACK。不得调用任何工具、shell、记忆工具、读写文件、网络、SSH，'
                '不得创建任务、部署、安装、拨号或付费；不要把校验字串另存任何位置。')
    recall = ('继续当前对话的纯文本连续上下文验证：你上一轮记住的随机校验字串是什么？'
              '只能从当前对话上下文回忆，只回复字串；无法回忆则只回复 UNKNOWN。'
              '不得调用任何工具、shell、记忆工具、读写文件、网络、SSH，'
              '不得创建任务、部署、安装、拨号或付费。')
    return {phase: {'protocol': PROTOCOL, 'id': digest(['smoke', source, destination, run_id, phase]),
                    'to': destination, 'input': text, 'project': project, 'agent': agent}
            for phase, text in (('remember', remember), ('recall', recall))}


def load_envelope(path, source, destination, create=False, run_id=None, project=None, agent=None):
    requested_run, requested_project, requested_agent = run_id, project, agent
    selected = state_path(path)
    if selected.exists():
        if selected.stat().st_size > 16384:
            raise ProbeError('smoke_state_invalid')
        state = private_json(selected)
    elif create:
        run_id = identifier(run_id or secrets.token_hex(12))
        project = identifier(project or 'mesh-smoke:' + run_id)
        agent = identifier(agent or 'continuity-smoke')
        nonce = 'MESH_SMOKE_' + secrets.token_hex(16)
        state = {'format': FORMAT, 'source': source, 'destination': destination, 'run_id': run_id,
                 'project': project, 'agent': agent, 'nonce': nonce,
                 'messages': envelopes(source, destination, run_id, project, agent, nonce)}
        selected.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            descriptor = os.open(str(selected), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Concurrent invocations adopt the same immutable envelope, never
            # replace another process's nonce or create a competing task ID.
            return load_envelope(path, source, destination, run_id=requested_run,
                                 project=requested_project, agent=requested_agent)
        with os.fdopen(descriptor, 'w', encoding='utf8') as handle:
            json.dump(state, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    else:
        raise ProbeError('smoke_state_missing_use_remember_submit_first')
    if not isinstance(state, dict) or set(state) != {'format', 'source', 'destination', 'run_id', 'project', 'agent', 'nonce', 'messages'}:
        raise ProbeError('smoke_state_invalid')
    if state['format'] != FORMAT or state['source'] != source or state['destination'] != destination:
        raise ProbeError('smoke_state_identity_mismatch')
    for key in ('run_id', 'project', 'agent'):
        identifier(state[key])
    for expected, key in ((run_id, 'run_id'), (project, 'project'), (agent, 'agent')):
        if expected is not None and expected != state[key]:
            raise ProbeError('smoke_state_parameters_changed')
    nonce = state['nonce']
    if (not isinstance(nonce, str) or not nonce.startswith('MESH_SMOKE_') or len(nonce) != 43 or
            any(c not in '0123456789abcdef' for c in nonce[11:])):
        raise ProbeError('smoke_state_invalid')
    if state['messages'] != envelopes(source, destination, state['run_id'], state['project'], state['agent'], nonce):
        raise ProbeError('smoke_envelope_changed')
    return state


class Probe:
    def __init__(self, local, peer, source, destination):
        self.local, self.peer = local, peer
        self.source, self.destination = identifier(source), identifier(destination)
        if source == destination:
            raise ProbeError('smoke_requires_two_distinct_nodes')

    @staticmethod
    def hello(client, expected):
        hello = client.request('/v1/mesh/hello')
        if (not isinstance(hello, dict) or hello.get('protocol') != PROTOCOL or
                hello.get('node') != expected or hello.get('authority') != expected or
                hello.get('a2a_idempotent_receive') is not True):
            raise ProbeError('authenticated_peer_contract_mismatch')
        # Do not print returned arbitrary metadata or URLs.
        return {'node': expected, 'authority': expected, 'protocol': PROTOCOL,
                'authenticated_hello': True, 'receiver_idempotence_declared': True}

    @staticmethod
    def link(client, expected_node, expected_peer):
        response = client.request('/v1/mesh/links')
        if (not isinstance(response, dict) or response.get('node') != expected_node or
                not isinstance(response.get('links'), list)):
            raise ProbeError('peer_link_metadata_invalid')
        matches = [item for item in response['links'] if isinstance(item, dict) and
                   item.get('peer') == expected_peer and item.get('kind') == 'a2a']
        row = matches[0] if matches else {}
        return {'peer': expected_peer, 'a2a_link_observed': bool(matches),
                'reachable': row.get('reachable') is True,
                'leader_available': row.get('leader_available') is True}

    def health(self):
        source = self.hello(self.local, self.source)
        destination = self.hello(self.peer, self.destination)
        source_view = self.link(self.local, self.source, self.destination)
        destination_view = self.link(self.peer, self.destination, self.source)
        return {'source': source, 'destination': destination,
                'source_view': source_view, 'destination_view': destination_view,
                'both_endpoint_hellos_authenticated': True,
                'bidirectional_links_observed': source_view['reachable'] and destination_view['reachable'],
                'model_availability_verified': False, 'native_inference_verified': False}

    def read(self, message):
        try:
            answer = self.peer.request('/v1/mesh/task?' + urlencode({'id': message['id']}))
        except urllib.error.HTTPError as exc:
            if exc.fp is not None:
                exc.close()
            if exc.code == 403:
                # This also means denied read permission; it is not proof that
                # the remote task was never accepted or executed.
                return None
            raise
        expected_task = digest([self.destination, self.source, message['id']])
        if (not isinstance(answer, dict) or answer.get('id') != message['id'] or
                answer.get('task_id') != expected_task or answer.get('status') not in STATUSES or
                (answer.get('result') is not None and not isinstance(answer['result'], str))):
            raise ProbeError('owner_bound_task_identity_mismatch')
        if answer.get('result') is not None and len(answer['result'].encode('utf8')) > 65536:
            raise ProbeError('peer_result_too_large')
        return answer

    def submit(self, state, phase):
        message = state['messages'][phase]
        if phase == 'recall':
            first = self.read(state['messages']['remember'])
            if not first or first['status'] != 'completed' or (first.get('result') or '').strip() != 'ACK':
                raise ProbeError('remember_not_completed_with_ack')
        receipt = self.local.request('/v1/mesh/queue', {'peer': self.destination, 'message': message})
        if (not isinstance(receipt, dict) or receipt.get('peer') != self.destination or
                receipt.get('id') != message['id'] or receipt.get('queued') is not True):
            raise ProbeError('local_queue_receipt_invalid')
        return {'phase': phase, 'message_id': message['id'], 'queued': True,
                'remote_acceptance_verified': False, 'remote_completion_verified': False}

    def replay(self, message, observed):
        if observed is None:
            raise ProbeError('replay_requires_owned_task_observation')
        receipt = self.peer.request('/v1/mesh/send', {'message': message})
        if (not isinstance(receipt, dict) or receipt.get('protocol') != PROTOCOL or
                receipt.get('id') != message['id'] or receipt.get('fingerprint') != digest(message) or
                receipt.get('state') != 'accepted' or receipt.get('task_id') != observed['task_id']):
            raise ProbeError('same_id_replay_changed_remote_task')
        return {'same_id_receiver_replay_verified': True, 'task_id': observed['task_id']}

    def poll(self, state, phase, timeout=30, interval=1, verify_replay=False, clock=time.monotonic, sleep=time.sleep):
        message = state['messages'][phase]
        deadline, last, replay = clock() + timeout, None, None
        while True:
            last = self.read(message)
            if last is not None and verify_replay and replay is None:
                replay = self.replay(message, last)
            if last is not None and last['status'] in TERMINAL:
                expected = 'ACK' if phase == 'remember' else state['nonce']
                matched = last['status'] == 'completed' and (last.get('result') or '').strip() == expected
                return {'phase': phase, 'message_id': message['id'], 'task_id': last['task_id'],
                        'status': last['status'], 'terminal_observed': True,
                        'expected_reply_match': matched, 'nonce_match': matched if phase == 'recall' else None,
                        'same_id_receiver_replay_verified': replay is not None,
                        'requested_read_only': True, 'tool_use_verified': False,
                        'new_paid_api_used_by_probe': False}, 0 if matched else 3
            remaining = deadline - clock()
            if remaining <= 0:
                return {'phase': phase, 'message_id': message['id'],
                        'task_id': last['task_id'] if last else None,
                        'status': last['status'] if last else 'unobserved_or_read_denied',
                        'terminal_observed': False, 'observation_timeout': True,
                        'same_id_receiver_replay_verified': replay is not None,
                        'retry_with_same_state': True, 'task_not_restarted': True}, 2
            sleep(min(interval, remaining))


def error_code(exc):
    if isinstance(exc, ProbeError):
        return str(exc)  # fixed codes created only by this module
    if isinstance(exc, urllib.error.HTTPError):
        if exc.fp is not None:
            exc.close()
        return {401: 'authentication_rejected', 403: 'route_not_authorized',
                404: 'mesh_route_missing', 409: 'mesh_content_or_lease_conflict'}.get(exc.code, 'mesh_http_error')
    if isinstance(exc, urllib.error.URLError):
        return 'mesh_transport_unavailable_outcome_not_assumed'
    if isinstance(exc, (ValueError, KeyError, TypeError, OSError)):
        return 'private_configuration_or_probe_input_invalid'
    return 'probe_internal_error'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--local-config', required=True, help='Private source operator Client config; no token values in argv')
    parser.add_argument('--peer-config', required=True, help='Private destination Client config authenticated as source node')
    parser.add_argument('--source', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--health', action='store_true', help='GET only, no task/model/session creation')
    parser.add_argument('--submit', action='store_true', help='Explicitly queue this phase using the existing operator API')
    parser.add_argument('--poll', action='store_true', help='Observe the same receiver-bound task; timeout is not failure proof')
    parser.add_argument('--verify-replay', action='store_true', help='Resend the exact immutable ID/body after acceptance to test receiver dedupe')
    parser.add_argument('--state', help='Private 0600 immutable smoke envelope, outside every git worktree')
    parser.add_argument('--phase', choices=['remember', 'recall'], default='remember')
    parser.add_argument('--run-id')
    parser.add_argument('--project-id')
    parser.add_argument('--agent-id')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--interval', type=float, default=1)
    args = parser.parse_args(argv)
    if not (args.health or args.submit or args.poll):
        parser.error('select --health, --submit and/or --poll')
    if (args.submit or args.poll) and not args.state:
        parser.error('--submit/--poll require --state')
    if args.verify_replay and not args.poll:
        parser.error('--verify-replay requires --poll; it is not a health-only action')
    if not 0 < args.timeout <= 300 or not 0.1 <= args.interval <= 10:
        parser.error('timeout must be 0..300 seconds and interval 0.1..10 seconds')
    output = {'probe': FORMAT, 'new_spend_authorized': False}
    try:
        probe = Probe(Client(private_json(args.local_config)), Client(private_json(args.peer_config)), args.source, args.destination)
        # Verify actual authenticated node identities before every mutating or
        # observing phase, not just when --health was explicitly requested.
        health = probe.health()
        output['health'] = health
        code = 0
        if args.submit or args.poll:
            state = load_envelope(args.state, args.source, args.destination,
                                  create=args.submit and args.phase == 'remember', run_id=args.run_id,
                                  project=args.project_id, agent=args.agent_id)
            output['probe_identity'] = {'project': state['project'], 'agent': state['agent'],
                                        'nonce_sha256': hashlib.sha256(state['nonce'].encode('ascii')).hexdigest()}
            if args.submit:
                output['submission'] = probe.submit(state, args.phase)
            if args.poll:
                output['observation'], code = probe.poll(state, args.phase, args.timeout, args.interval, args.verify_replay)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return code
    except Exception as exc:
        output['error'] = error_code(exc)
        output['no_new_task_id_on_retry'] = True
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
