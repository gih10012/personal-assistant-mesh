import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { HDC_EXECUTE, HDC_MAX_PACKET, SelfHdcPacketStream, hdcDecodeHandshake, hdcDecodeTlv,
  hdcEncodeHandshake, hdcEncodePacket, hdcEncodeTlv, hdcJoin, hdcShellCommand, hdcShellTail,
  hdcText, hdcUtf8 } from './SelfHdcProtocol.ts';

// Literal independent fixtures from the official packed C++ header + protobuf schema,
// not output produced by the encoder under test; no real device/key/account material.
const rawExecute = Uint8Array.from(Buffer.from(
  '4857000001000900000001080710e9071800200978', 'hex'));
const hello = Uint8Array.from(Buffer.from(
  '0a084f484f5320484443100018012201312a00320176', 'hex'));

test('official HW header/protobuf fixture survives arbitrary TCP segmentation and coalescing', () => {
  assert.deepEqual(hdcEncodePacket({ channel: 7, command: HDC_EXECUTE, data: hdcUtf8('x') }), rawExecute);
  const stream = new SelfHdcPacketStream();
  const packets = [];
  for (const byte of rawExecute) packets.push(...stream.feed(new Uint8Array([byte])));
  assert.deepEqual(packets, [{ channel: 7, command: 1001, data: hdcUtf8('x') }]);
  assert.equal(new SelfHdcPacketStream().feed(hdcJoin([rawExecute, rawExecute])).length, 2);
  const value = hdcDecodeHandshake(hello);
  assert.deepEqual(value, { auth: 0, session: 1, target: '1', data: new Uint8Array(0), version: 'v' });
  assert.deepEqual(hdcEncodeHandshake(value), hello);
});

test('parser rejects lengths, duplicate fields, unsupported encryption header and corrupt checksum', () => {
  const oversized = rawExecute.slice();
  new DataView(oversized.buffer).setUint32(7, HDC_MAX_PACKET + 1, false);
  assert.throws(() => new SelfHdcPacketStream().feed(oversized), /packet_size/);
  const encrypted = rawExecute.slice(); encrypted[2] = 1;
  assert.throws(() => new SelfHdcPacketStream().feed(encrypted), /packet_header/);
  const corrupt = rawExecute.slice(); corrupt[17] = 1;
  assert.throws(() => new SelfHdcPacketStream().feed(corrupt), /packet_checksum/);
  assert.throws(() => hdcDecodeHandshake(hdcJoin([hello, new Uint8Array([16, 4])])), /duplicate/);
  const invalidTlv = hdcUtf8('authtype        9               1');
  assert.throws(() => hdcDecodeTlv(invalidTlv), /tlv_length/);
  assert.deepEqual(hdcDecodeTlv(hdcEncodeTlv('authtype', '1')), new Map([['authtype', '1']]));
});

test('shell completion preserves binary stdout and contains exit/EXIT trap in a subshell', () => {
  const marker = 'AMITA_HDC_00112233445566778899aabbccddeeff';
  const script = hdcShellCommand("trap 'printf cleanup' EXIT\nprintf '\\001\\377'\nexit 43 # trailing comment", marker);
  const process = spawnSync('/bin/sh', ['-c', script]);
  assert.equal(process.status, 0);
  const decoded = hdcShellTail(new Uint8Array(process.stdout), marker);
  assert.equal(decoded.completed, true); assert.equal(decoded.exitCode, 43);
  assert.deepEqual(decoded.stdout, new Uint8Array([1, 255, ...Buffer.from('cleanup')]));
  assert.equal(hdcShellTail(new Uint8Array(process.stdout).slice(0, -1), marker).completed, false);
  assert.equal(hdcShellTail(new Uint8Array(process.stdout), marker.replace('0011', '9911')).completed, false);
  assert.throws(() => hdcShellCommand('id\0rm', marker), /command_invalid/);
  const unicode = '手机 \u{1f642}'; assert.equal(hdcText(hdcUtf8(unicode)), unicode);
});
