// Execute the EXACT phone Protocol.ts against the original authority's JSON.
// This is protocol conformance, not Harmony SDK/device/network execution.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import {
  acceptRelayChallenge, beginRelayChallenge, beginRelayResult, canonicalChallenge, canonicalEnrollment,
  canonicalRelayBinding, canonicalResult, newRelayRecord, observeRelayStatus,
  validateEnrollment, validateRelayClientConfig, validateResultEnvelope
} from '../entry/src/main/ets/core/Protocol.ts';
const hash = v => createHash('sha256').update(v).digest('hex');
const input = JSON.parse(readFileSync(0, 'utf8'));
validateEnrollment(input.enrollment, input.enrollment.node_id);
const enrollmentHash = hash(canonicalEnrollment(input.enrollment));
validateRelayClientConfig(input.config, input.enrollment, enrollmentHash);
const row = newRelayRecord(input.config);
beginRelayChallenge(row);
acceptRelayChallenge(row, input.challenge_response, input.enrollment, enrollmentHash,
  hash(canonicalChallenge(input.challenge_response.challenge)), input.now);
validateResultEnvelope(input.result, row.binding, input.enrollment);
const bytes = Buffer.from(input.result.result.base64, 'base64');
if (bytes.length !== input.result.result.bytes || bytes.toString('base64') !== input.result.result.base64 ||
    hash(bytes) !== input.result.result.sha256) { throw new Error('complete_result_bytes_changed'); }
beginRelayResult(row, hash(canonicalResult(input.result)));
observeRelayStatus(row, input.result_response, 'result');
process.stdout.write(JSON.stringify({ canonical_binding: canonicalRelayBinding(row.binding),
  canonical_result: canonicalResult(input.result), result_state: row.result_state,
  request_sha256: row.binding.request_sha256, operation_id: row.binding.operation_id }));
