#!/usr/bin/env python3
"""Build only the existing public remote plugin; never install or authenticate."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NAME = 'personal-assistant-mesh-remote'
FILES = ('plugin.json', 'mcp.json', 'skills/mesh-assistant/SKILL.md',
         'skills/mesh-assistant/agents/openai.yaml',
         'skills/mesh-assistant/references/async-followup.md')


def build(output):
    source = ROOT / 'plugins' / NAME
    output = Path(output).absolute()
    if source in output.parents or output == source:
        raise ValueError('remote_plugin_archive_must_be_outside_source')
    manifest = json.loads((source / 'plugin.json').read_text(encoding='utf8'))
    mcp = json.loads((source / 'mcp.json').read_text(encoding='utf8'))
    if (manifest['name'] != NAME or manifest['$schema'] != 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
            or len(manifest['extensions']['com.openai']['interface']['shortDescription']) > 30
            or mcp['$schema'] != 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json'):
        raise ValueError('remote_plugin_manifest_invalid')
    with zipfile.ZipFile(str(output), 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('remote_plugin_component_invalid')
            archive.writestr(NAME + '/' + name, path.read_bytes())
    with zipfile.ZipFile(str(output)) as archive:
        if sorted(archive.namelist()) != sorted(NAME + '/' + name for name in FILES):
            raise ValueError('remote_plugin_archive_invalid')
        for name in ('plugin.json', 'mcp.json'):
            json.loads(archive.read(NAME + '/' + name).decode('utf8'))
    raw = output.read_bytes()
    return {'archive': str(output), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'files': list(FILES), 'saved_to_account': False, 'connected': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    print(json.dumps(build(parser.parse_args().output), sort_keys=True))
