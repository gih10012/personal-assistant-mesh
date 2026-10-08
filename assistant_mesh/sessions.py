"""Transport ONLY a selected native rollout. Codex owns context and compaction."""
import base64
import gzip
import os
import tempfile
from pathlib import Path


def request(client, task, action, payload):
    return client.request('/v1/session', {'task_id': task['id'], 'epoch': task['epoch'],
                                         'action': action, 'payload': payload})


def save(client, task, node, harness, state, rollout=None, tick=None):
    parts = 0
    if rollout:
        # Streaming compression and bounded chunks avoid holding a long native
        # conversation in RAM or truncating it to an application-written summary.
        with tempfile.TemporaryFile() as packed:
            with gzip.GzipFile(fileobj=packed, mode='wb') as archive, Path(rollout).open('rb') as source:
                while True:
                    block = source.read(65536)
                    if not block:
                        break
                    archive.write(block)
                    if tick:
                        tick()
            packed.seek(0)
            while True:
                block = packed.read(65536)
                if not block:
                    break
                request(client, task, 'upload', {'harness': harness, 'part': parts,
                                                'data': base64.b64encode(block).decode()})
                parts += 1
                if tick:
                    tick()
    return request(client, task, 'commit', {'harness': harness, 'state': dict(state, codex_node=node), 'parts': parts})


def restore(client, task, folder, tick=None, harness='codex'):
    folder = Path(folder)
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.TemporaryFile() as packed:
        part = 0
        while True:
            value = request(client, task, 'download', {'part': part, 'harness': harness})['data']
            if value is None:
                break
            packed.write(base64.b64decode(value, validate=True))
            part += 1
            if tick:
                tick()
        if not part:
            raise ValueError('native_session_artifact_empty')
        packed.seek(0)
        with tempfile.NamedTemporaryFile(dir=str(folder), prefix='native-rollout-', suffix='.jsonl', delete=False) as output:
            path = output.name
            try:
                with gzip.GzipFile(fileobj=packed, mode='rb') as archive:
                    while True:
                        block = archive.read(65536)
                        if not block:
                            break
                        output.write(block)
                        if tick:
                            tick()
            except BaseException:
                os.unlink(path)
                raise
    return path
