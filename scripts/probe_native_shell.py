"""Verify native Shell with an undisclosed challenge, not a model's assertion.

Uses the owner's existing Codex profile. No Mesh policy or native tool filter is
installed. The private probe directory is retained for bounded diagnosis.
"""
import argparse
import hashlib
import json
import os
import secrets
import shlex
import tempfile
from pathlib import Path

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.config import private_json


def evidence(path, expected):
    """Only examine the probe's own native rollout, never unrelated sessions."""
    calls, outputs = {}, {}
    with Path(path).open(encoding='utf8') as handle:
        for line in handle:
            record = json.loads(line)
            if record.get('type') != 'response_item':
                continue
            item = record.get('payload', {})
            identity = item.get('call_id')
            if item.get('type') in ('function_call', 'custom_tool_call') and identity:
                calls[identity] = item
            if item.get('type') in ('function_call_output', 'custom_tool_call_output') and identity:
                outputs[identity] = json.dumps(item.get('output', ''), ensure_ascii=False)
    shell_calls = [identity for identity, item in calls.items()
                   if 'sha256sum' in str(item.get('arguments', item.get('input', '')))
                   and any(word in str(item.get('name', '')).lower()
                           for word in ('exec', 'shell', 'terminal'))]
    return {'native_tool_calls': len(calls), 'shell_challenge_calls': len(shell_calls),
            'challenge_seen_in_native_output': any(expected in outputs.get(identity, '') for identity in shell_calls)}


def run(config):
    workspace = Path(tempfile.mkdtemp(prefix='mesh-native-shell-probe.'))
    workspace.chmod(0o700)
    challenge = workspace / 'challenge.bin'
    payload = secrets.token_bytes(64)
    descriptor = os.open(str(challenge), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(payload)
    expected = hashlib.sha256(payload).hexdigest()
    candidates = config.get('codex_accounts') or [None]
    # These restrict the test's requested work, not the native tool surface.
    prompt = ('这是本人授权的原生 Shell 验收。使用原生终端执行这一条命令：sha256sum -- ' +
              shlex.quote(str(challenge)) + '。文件内容和预期哈希未提供给你。不要读其它文件，'
              '不要联网或写入文件，不调用 Mesh。最终只回复实际命令输出里的 SHA256，不要猜测。')
    runtime = None
    for profile, candidate in enumerate(candidates):
        native = dict(config['codex'], workspace=str(workspace))
        if candidate is not None:
            native.update(dict(candidate, strict_auth_home=True))
        runtime = Codex(native)
        try:
            quota = runtime.rate_limits()
        except (OSError, ValueError):
            quota = {'status': 'unknown'}
        if quota.get('status') != 'exhausted':
            break
        runtime.close()
        runtime = None
    if runtime is None:
        raise CodexError('codex_usage_limit_exceeded')
    # Fallback only selected BEFORE starting. Never replay an attempted turn.
    with runtime:
        runtime.start(prompt)
        reply = runtime.finish(timeout=180)
        rollout = runtime.native_rollout()
        result = evidence(rollout, expected)
    result.update({'reply_matches': reply.strip() == expected,
                   'probe_directory': str(workspace), 'mesh_tool_interception': False,
                   'authorized_profile_index': profile, 'started_turn_replayed': False})
    result['execution_verified'] = result['challenge_seen_in_native_output'] and result['reply_matches']
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    result = run(private_json(args.config))
    print(json.dumps(result, ensure_ascii=False))
    if not result['execution_verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
