import { createHash } from 'node:crypto';
import { lstatSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { need } from './codec.mjs';

export const pins = JSON.parse(readFileSync(new URL('./artifact-pins.json', import.meta.url), 'utf8'));
export function regular(path, maximum = 4 * 1024 * 1024) {
  const stat = lstatSync(path);
  need(stat.isFile() && !stat.isSymbolicLink() && stat.size <= maximum, 'regular_bounded_public_artifact');
  return readFileSync(path);
}
export function digest(value) { return createHash('sha256').update(value).digest('hex'); }
export function inspectArtifact(root) {
  root = resolve(root);
  const manifest = JSON.parse(regular(join(root, 'HOSTED-MANIFEST.json'), 256 * 1024));
  need(manifest.format === 'amita-raft-hosted-build/1' && manifest.source_commit === pins.source_commit &&
    manifest.run_id === pins.run_id && manifest.repository === 'gih10012/personal-assistant-mesh', 'pinned_hosted_provenance');
  for (const [name, expected] of Object.entries(pins.files)) {
    const data = regular(join(root, name));
    need(digest(data) === expected && manifest.files.some(row => row.path === name && row.sha256 === expected && row.bytes === data.length),
      'pinned_public_artifact_changed');
  }
  const recovery = JSON.parse(regular(join(root, 'WASM-RECOVERY.json'), 8192));
  need(recovery.status === 'passed' && recovery.wasm_sha256 === pins.files['wasm/amita_raft_portability_bg.wasm'], 'original_wasm_receipt');
  return manifest;
}
export async function loadNodeVoter(root = join(dirname(fileURLToPath(import.meta.url)), 'artifact')) {
  inspectArtifact(root);
  const generated = await import(pathToFileURL(join(resolve(root), 'wasm/amita_raft_portability.js')).href);
  generated.initSync({ module: new WebAssembly.Module(regular(join(root, 'wasm/amita_raft_portability_bg.wasm'))) });
  return generated.Voter;
}
