// Run only the generated, real raft-rs WASM. No service, credentials or network.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { closeSync, fsyncSync, mkdtempSync, openSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const moduleDirectory = resolve(process.argv[2] ?? dirname(fileURLToPath(import.meta.url)));
const bytes = readFileSync(join(moduleDirectory, 'amita_raft_portability_bg.wasm'));
const { initSync, Voter } = await import(pathToFileURL(join(moduleDirectory, 'amita_raft_portability.js')).href);
// Workers hosts supply a precompiled module too; this tests that initSync entry,
// not fetch(), URL loading, a mock implementation or a native .so substitute.
initSync({ module: new WebAssembly.Module(bytes) });
const fresh = { hard_state: [], entries: [], snapshot: [], applied: 0 };
const directory = mkdtempSync(join(tmpdir(), 'amita-raft-wasm-vote-'));
let first, restored;
try {
  first = new Voter(3, JSON.stringify(fresh));
  // raft-proto 0.7.0/proto/eraftpb.proto: Message fields type=1,to=2,
  // from=3,term=4,reject=10; MsgRequestVote=5,Response=6. Small fixed fixtures
  // match the existing Rust recovery case, and never encode arbitrary u64s.
  const request = candidate => Uint8Array.from([8, 5, 16, 3, 24, candidate, 32, 7]);
  const response = (candidate, reject) => [8, 6, 16, candidate, 24, 3, 32, 7, ...(reject ? [80, 1] : [])];
  const hasResponse = (batch, candidate, reject) => batch.messages.some(message =>
    Buffer.from(message).equals(Buffer.from(response(candidate, reject))));
  first.step(request(1));
  const ready = JSON.parse(first.ready());
  assert.ok(ready !== null && Number.isSafeInteger(ready.sequence) && ready.sequence > 0);
  assert.ok(Array.isArray(ready.hard_state) && ready.hard_state.length > 0);
  assert.equal('messages' in ready, false, 'Ready must withhold outgoing messages');
  assert.throws(() => first.step(request(1)), /finish outstanding Ready/);
  assert.throws(() => first.persisted(BigInt(ready.sequence + 1)), /wrong Ready receipt/);
  const stable = { ...fresh, hard_state: ready.hard_state };
  const pending = join(directory, 'vote.tmp'), durable = join(directory, 'vote.json');
  writeFileSync(pending, JSON.stringify(stable));
  let descriptor = openSync(pending, 'r');
  try { fsyncSync(descriptor); } finally { closeSync(descriptor); }
  renameSync(pending, durable);
  descriptor = openSync(directory, 'r');
  try { fsyncSync(descriptor); } finally { closeSync(descriptor); }
  assert.ok(hasResponse(JSON.parse(first.persisted(BigInt(ready.sequence))), 1, false));
  first.free(); first = undefined;
  restored = new Voter(3, readFileSync(durable, 'utf8'));
  restored.step(request(2));
  const next = JSON.parse(restored.ready());
  assert.ok(hasResponse(JSON.parse(restored.persisted(BigInt(next.sequence))), 2, true),
    'restored voter must reject another candidate in the same term');
  const evidence = {
    format: 'amita-raft-wasm-recovery/1',
    case: 'durable_vote_survives_reconstruction', status: 'passed',
    wasm_sha256: createHash('sha256').update(bytes).digest('hex'),
    runtime: process.version,
    scope: 'Generated WASM initSync and one durable vote recovery; no DO, transport, FSM or three-voter acceptance'
  };
  if (process.argv[3]) writeFileSync(resolve(process.argv[3]), JSON.stringify(evidence, null, 2) + '\n', { flag: 'wx' });
  console.log(JSON.stringify(evidence));
} finally {
  first?.free(); restored?.free();
  rmSync(directory, { recursive: true, force: true });
}
