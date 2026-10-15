import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { canonicalChallenge, canonicalEnrollment, report, validateChallenge, validateEnrollment } from '../entry/src/main/ets/core/Protocol.ts';
const input = JSON.parse(readFileSync(0, 'utf8'));
validateEnrollment(input.enrollment, input.enrollment.node_id);
const enrollmentHash = createHash('sha256').update(canonicalEnrollment(input.enrollment)).digest('hex');
validateChallenge(input.challenge, input.enrollment, enrollmentHash, input.now);
process.stdout.write(JSON.stringify({ canonical_enrollment: canonicalEnrollment(input.enrollment),
  canonical_challenge: canonicalChallenge(input.challenge), adaptation: report(input.enrollment, false) }));
