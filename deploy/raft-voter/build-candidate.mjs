// Public files only. No Rust build, account operation, runtime configuration,
// credential, native history or running ledger is read/copied by this builder.
import { mkdirSync, writeFileSync, existsSync, lstatSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { digest, inspectArtifact, pins, regular } from './loader-node.mjs';
import { need } from './codec.mjs';

const source = dirname(fileURLToPath(import.meta.url));
const args = process.argv.slice(2);
need(args.length === 4 && args[0] === '--artifact-root' && args[2] === '--destination', 'explicit_builder_arguments');
const artifact = resolve(args[1]), destination = args[3];
need(isAbsolute(destination) && !existsSync(destination), 'new_private_destination');
const upstream = inspectArtifact(artifact);
const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { cwd: source, encoding: 'utf8' }).trim();
const commit = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: source, encoding: 'utf8' }).trim();
const sourceFiles = ['codec.mjs', 'protocol.mjs', 'owner-fsm.mjs', 'driver.mjs', 'storage-node.mjs', 'loader-node.mjs',
  'http.mjs', 'node.mjs', 'cloudflare-worker.mjs', 'artifact-pins.json', 'wrangler.candidate.json', 'smoke.mjs', 'exchange-smoke.mjs', 'owner-smoke.mjs', 'README.md'];
const outputs = [];
for (const name of sourceFiles) outputs.push([name, execFileSync('git', ['show', `${commit}:deploy/raft-voter/${name}`], { cwd: root })]);
const artifactFiles = [...Object.keys(pins.files), 'HOSTED-MANIFEST.json', 'WASM-RECOVERY.json'];
// Public package licenses are the ONLY permitted extra artifact tree. Every
// file must be listed with its exact size/hash by the original hosted manifest.
for (const row of upstream.files) {
  if (/^licenses\/[A-Za-z0-9_.+-]+\/[A-Za-z0-9_.+-]+$/.test(row.path)) artifactFiles.push(row.path);
}
for (const name of artifactFiles) {
  const data = regular(join(artifact, name));
  if (!['HOSTED-MANIFEST.json', 'WASM-RECOVERY.json'].includes(name)) {
    const row = upstream.files.find(row => row.path === name);
    need(row && row.bytes === data.length && row.sha256 === digest(data), 'allowlisted_artifact_hash');
  }
  outputs.push([`artifact/${name}`, data]);
}
need(outputs.reduce((sum, [, data]) => sum + data.length, 0) <= 8 * 1024 * 1024, 'candidate_public_size_budget');
mkdirSync(destination, { mode: 0o700, recursive: true });
need(lstatSync(destination).isDirectory() && !lstatSync(destination).isSymbolicLink() &&
  (lstatSync(destination).mode & 0o077) === 0, 'private_candidate_directory');
for (const [name, data] of outputs) {
  const path = join(destination, name); mkdirSync(dirname(path), { mode: 0o700, recursive: true });
  writeFileSync(path, data, { mode: 0o600, flag: 'wx' });
}
const manifest = { format: 'amita-raft-voter-candidate/1', source_commit: commit, upstream_commit: pins.source_commit,
  upstream_run_id: pins.run_id, upstream_manifest_sha256: digest(regular(join(artifact, 'HOSTED-MANIFEST.json'))),
  files: outputs.map(([path, data]) => ({ path, bytes: data.length, sha256: digest(data) })),
  wasm_loaded_from: 'artifact/wasm/amita_raft_portability_bg.wasm',
  native_build: false, cloudflare_deployed: false, credentials_copied: false, business_ledger_opened: false,
  node_entry: 'node node.mjs /ABSOLUTE/EXISTING_PRIVATE_CONFIG.json',
  cloudflare_entry: 'cloudflare-worker.mjs; wrangler.candidate.json is candidate-only',
  acceptance: 'upstream native/WASM single-vote evidence retained; local three-voter smoke is a separate receipt; DO CPU/account/network/business fencing unqualified' };
writeFileSync(join(destination, 'CANDIDATE-MANIFEST.json'), JSON.stringify(manifest, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
console.log(JSON.stringify({ destination, source_commit: commit, public_files: outputs.length,
  upstream_manifest_sha256: manifest.upstream_manifest_sha256, deployed: false }));
