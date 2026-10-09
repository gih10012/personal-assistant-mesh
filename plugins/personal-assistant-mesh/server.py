#!/usr/bin/env python3
"""Owner-installed launcher, not a model-controlled configuration interface."""
import json
import os
import stat
import sys
from pathlib import Path


def private_bytes(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('settings')
    for parent in path.parents:
        item = parent.lstat()
        if (not stat.S_ISDIR(item.st_mode) or item.st_uid not in (0, os.getuid())
                or item.st_mode & 0o022 and not item.st_mode & stat.S_ISVTX):
            raise ValueError('settings')
    parent, named = path.parent.lstat(), path.lstat()
    if (parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISREG(named.st_mode) or named.st_uid != os.getuid()
            or stat.S_IMODE(named.st_mode) != 0o600 or named.st_nlink != 1):
        raise ValueError('settings')
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        opened = os.fstat(source.fileno())
        identity = lambda item: (item.st_dev, item.st_ino, item.st_uid, item.st_mode,
                                 item.st_nlink, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if identity(opened) != identity(named):
            raise ValueError('settings')
        value = source.read(32769)
        if len(value) > 32768 or identity(os.fstat(source.fileno())) != identity(opened):
            raise ValueError('settings')
        return value


def unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('settings')
        result[key] = value
    return result


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError('settings')
        default_root = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
        default = str(Path(default_root) / 'personal-assistant-mesh' / 'mcp-launch.json')
        settings = json.loads(private_bytes(os.environ.get('PERSONAL_ASSISTANT_MESH_PLUGIN_SETTINGS', default)).decode('utf8'),
                              object_pairs_hook=unique)
        if not isinstance(settings, dict) or set(settings) != {'runtime_root', 'client_config'}:
            raise ValueError('settings')
        root = Path(settings['runtime_root'])
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError('settings')
        if root.lstat().st_mode & 0o022:
            # A sticky /tmp ancestor can contain a private runtime, but the
            # import root itself must never be a shared writable directory.
            raise ValueError('settings')
        for parent in (root,) + tuple(root.parents):
            item = parent.lstat()
            if (not stat.S_ISDIR(item.st_mode) or item.st_uid not in (0, os.getuid())
                    or item.st_mode & 0o022 and not item.st_mode & stat.S_ISVTX):
                raise ValueError('settings')
        # Installation fixes these locations in a private owner file. Neither
        # tool names/arguments nor Chat content can change them.
        private_bytes(settings['client_config'])
        sys.path.insert(0, str(root))
        from assistant_mesh.mcp_entrypoint import main as serve
        return serve(config_path=settings['client_config'])
    except Exception:
        sys.stderr.write('Mesh MCP launcher unavailable\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
