import json
import os
import stat
from pathlib import Path


def private_json(path):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('private_config_requires_regular_0600_file')
    with path.open(encoding='utf8') as handle:
        return json.load(handle)


def read_secret(path):
    path = Path(path).expanduser()
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError('secret_requires_regular_0600_file')
    value = path.read_text().strip()
    if len(value) < 32:
        raise ValueError('weak_control_token')
    return value


def discover_codex_auth(preferred=None):
    """Known credential sources only. No credential values in return value/logs."""
    candidates = [Path(preferred).expanduser()] if preferred else []
    candidates += [Path.home() / '.codex-official', Path.home() / '.codex']
    for root in candidates:
        path = root / 'auth.json'
        if path.is_file() and not path.is_symlink() and not stat.S_IMODE(path.stat().st_mode) & 0o077:
            try:
                value = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            if value.get('auth_mode') == 'chatgpt' and value.get('tokens'):
                return str(root)
    raise ValueError('codex_auth_required')
