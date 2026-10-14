// Minimal independent node recipe: Node.js builtins only, no Python/daemon/SSH.
// It receives an admitted mesh-enrollment-challenge/1 on stdin and writes the
// original mesh-enrollment-result/1. This is a byte-transform challenge, NOT
// authority, egress permission, device control, a model or a production node.
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const MAX_INPUT = 128 * 1024;
const MAX_ARTIFACT = 8 * 1024 * 1024;
function requireValue(ok) { if (!ok) throw new Error('adapter_contract_invalid'); }
function objectKeys(value, keys) {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value));
  requireValue(Object.keys(value).sort().join('\0') === [...keys].sort().join('\0'));
}
function integer(value, minimum = 0, maximum = Number.MAX_SAFE_INTEGER) {
  requireValue(Number.isSafeInteger(value) && value >= minimum && value <= maximum);
}
function text(value) {
  requireValue(typeof value === 'string' && value.length > 0 && !/[\x00-\x1f]/.test(value));
}
function sha(value) { return createHash('sha256').update(value).digest('hex'); }
function keyCompare(a, b) {
  const x = Array.from(a, c => c.codePointAt(0)), y = Array.from(b, c => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] !== y[i]) return x[i] - y[i];
  }
  return x.length - y.length;
}
// Exactly Python's reference ensure_ascii=True / sorted-keys / compact JSON,
// not RFC 8785. Contract numbers must be safe integers, not arbitrary doubles.
export function canonical(value) {
  if (typeof value === 'string') {
    return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
      c => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
  }
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') { integer(value, -Number.MAX_SAFE_INTEGER); return String(value); }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  requireValue(value !== null && typeof value === 'object');
  return '{' + Object.keys(value).sort(keyCompare).map(key => canonical(key) + ':' + canonical(value[key])).join(',') + '}';
}

function validate(challenge) {
  objectKeys(challenge, ['format', 'operation_id', 'mesh_id', 'node_id', 'owner_binding',
    'enrollment_sha256', 'continuation', 'nonce', 'created_at', 'expires_at',
    'capability_id', 'action', 'revision', 'input', 'expected', 'ttl_seconds', 'proxy']);
  requireValue(challenge.format === 'mesh-enrollment-challenge/1');
  for (const key of ['operation_id', 'mesh_id', 'node_id', 'owner_binding', 'capability_id']) text(challenge[key]);
  requireValue(challenge.action === 'challenge.bytes.repeat' && challenge.proxy === null);
  requireValue(/^[0-9a-f]{64}$/.test(challenge.nonce) && /^[0-9a-f]{64}$/.test(challenge.enrollment_sha256));
  integer(challenge.revision, 1); integer(challenge.ttl_seconds, 1, 3600);
  integer(challenge.created_at); integer(challenge.expires_at, 1);
  const now = Date.now() / 1000;
  requireValue(challenge.created_at <= now && now < challenge.expires_at);
  objectKeys(challenge.continuation, ['project_id', 'session_ref', 'state_ref']);
  Object.values(challenge.continuation).forEach(text);
  objectKeys(challenge.input, ['bytes', 'sha256', 'base64']);
  integer(challenge.input.bytes, 0, 64 * 1024);
  requireValue(typeof challenge.input.base64 === 'string' && /^[0-9a-f]{64}$/.test(challenge.input.sha256));
  const input = Buffer.from(challenge.input.base64, 'base64');
  requireValue(input.length === challenge.input.bytes && input.toString('base64') === challenge.input.base64
    && sha(input) === challenge.input.sha256);
  objectKeys(challenge.expected, ['bytes', 'sha256']);
  integer(challenge.expected.bytes, 0, MAX_ARTIFACT);
  requireValue(/^[0-9a-f]{64}$/.test(challenge.expected.sha256));
  return input;
}

async function main() {
  requireValue(process.argv.length === 4 && process.argv[2] === '--repeat');
  const repeat = Number(process.argv[3]); integer(repeat, 1, 64);
  const chunks = []; let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length; requireValue(bytes <= MAX_INPUT); chunks.push(chunk);
  }
  const challenge = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  const input = validate(challenge);
  requireValue(input.length * repeat <= MAX_ARTIFACT);
  const output = Buffer.concat(Array.from({ length: repeat }, () => input));
  const envelope = { format: 'mesh-enrollment-result/1', operation_id: challenge.operation_id,
    request_sha256: sha(Buffer.from(canonical(challenge), 'ascii')), nonce: challenge.nonce,
    result: { bytes: output.length, sha256: sha(output), base64: output.toString('base64') },
    continuation: challenge.continuation, proxy: challenge.proxy };
  process.stdout.write(canonical(envelope) + '\n');
}

// Importing the canonical codec for conformance tests does not execute a job.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main().catch(() => { process.stderr.write('adapter_contract_invalid\n'); process.exitCode = 1; });
}
