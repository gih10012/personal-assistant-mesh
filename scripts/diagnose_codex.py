"""Bounded redacted protocol diagnosis; never dump account/token payloads."""
import argparse
import json
import re

from assistant_mesh.codex import Codex, CodexError
from assistant_mesh.config import private_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    with Codex(private_json(args.config)['codex']) as runtime:
        try:
            print(json.dumps(runtime.account()))
        except CodexError:
            error = getattr(runtime, 'last_protocol_error', {})
            message = error.get('message', '')[:500]
            message = re.sub(r'(?i)(Bearer\s+|sk-)[A-Za-z0-9_.-]+', '[REDACTED]', message)
            message = re.sub(r'eyJ[A-Za-z0-9_.-]+', '[REDACTED]', message)
            print(json.dumps({'code': error.get('code'), 'message': message}))


if __name__ == '__main__':
    main()
