/*
 * SPDX-License-Identifier: Apache-2.0
 * Independent TypeScript implementation of the OpenHarmony HDC wire format.
 * Schema/command reference: OpenHarmony developtools_hdc, Copyright (c) 2021
 * Huawei Device Co., Ltd., Apache License 2.0; pinned source and license in README.
 */

export const HDC_HANDSHAKE = 1;
export const HDC_CLOSE = 2;
export const HDC_ECHO = 9;
export const HDC_RAW = 10;
export const HDC_EXECUTE = 1001;
export const HDC_MAX_PACKET = 2 * 1024 * 1024;

export interface SelfHdcPacket { channel: number; command: number; data: Uint8Array; }
export interface SelfHdcHandshake {
  auth: number; session: number; target: string; data: Uint8Array; version: string;
}
interface WireField { tag: number; wire: number; value: number; data: Uint8Array; }

export function hdcRequire(condition: boolean, error: string): void {
  if (!condition) { throw new Error(error); }
}

export function hdcJoin(parts: Uint8Array[]): Uint8Array {
  let length = 0;
  for (const part of parts) { length += part.length; }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const part of parts) { result.set(part, offset); offset += part.length; }
  return result;
}

// Avoid host-only TextEncoder globals: this module also runs in ArkTS.
export function hdcUtf8(value: string): Uint8Array {
  const bytes: number[] = [];
  for (let index = 0; index < value.length; index++) {
    let point = value.charCodeAt(index);
    if (point >= 0xd800 && point <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next >= 0xdc00 && next <= 0xdfff) {
        point = 0x10000 + (point - 0xd800) * 1024 + next - 0xdc00; index++;
      } else { point = 0xfffd; }
    } else if (point >= 0xdc00 && point <= 0xdfff) { point = 0xfffd; }
    if (point < 0x80) { bytes.push(point); }
    else if (point < 0x800) { bytes.push(0xc0 | (point >> 6), 0x80 | (point & 63)); }
    else if (point < 0x10000) {
      bytes.push(0xe0 | (point >> 12), 0x80 | ((point >> 6) & 63), 0x80 | (point & 63));
    } else {
      bytes.push(0xf0 | (point >> 18), 0x80 | ((point >> 12) & 63),
        0x80 | ((point >> 6) & 63), 0x80 | (point & 63));
    }
  }
  return new Uint8Array(bytes);
}

export function hdcText(bytes: Uint8Array): string {
  let result = '';
  for (let index = 0; index < bytes.length;) {
    const first = bytes[index++];
    if (first < 128) { result += String.fromCharCode(first); continue; }
    const count = first >= 0xc2 && first <= 0xdf ? 1 : first >= 0xe0 && first <= 0xef ? 2 :
      first >= 0xf0 && first <= 0xf4 ? 3 : -1;
    if (count < 0 || index + count > bytes.length) { result += '\ufffd'; continue; }
    let point = first & (count === 1 ? 31 : count === 2 ? 15 : 7);
    let valid = true;
    for (let part = 0; part < count; part++) {
      if ((bytes[index + part] & 0xc0) !== 0x80) { valid = false; break; }
      point = point * 64 + (bytes[index + part] & 63);
    }
    if (!valid || point < (count === 1 ? 128 : count === 2 ? 2048 : 65536) ||
      point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) { result += '\ufffd'; continue; }
    index += count;
    if (point < 65536) { result += String.fromCharCode(point); }
    else { point -= 65536; result += String.fromCharCode(0xd800 + (point >> 10), 0xdc00 + (point & 1023)); }
  }
  return result;
}

function varint(value: number): Uint8Array {
  hdcRequire(Number.isInteger(value) && value >= 0 && value <= 0xffffffff, 'selfhdc_wire_integer');
  const bytes: number[] = [];
  do {
    const next = value % 128;
    value = Math.floor(value / 128);
    bytes.push(next + (value > 0 ? 128 : 0));
  } while (value > 0);
  return new Uint8Array(bytes);
}
function scalar(tag: number, value: number): Uint8Array { return hdcJoin([varint(tag * 8), varint(value)]); }
function blob(tag: number, value: Uint8Array): Uint8Array {
  return hdcJoin([varint(tag * 8 + 2), varint(value.length), value]);
}

class WireReader {
  private bytes: Uint8Array;
  offset: number = 0;
  constructor(bytes: Uint8Array) { this.bytes = bytes; }
  integer(): number {
    let value = 0;
    for (let count = 0; count < 5; count++) {
      hdcRequire(this.offset < this.bytes.length, 'selfhdc_wire_truncated');
      const next = this.bytes[this.offset++];
      value += (next & 127) * Math.pow(128, count);
      hdcRequire(value <= 0xffffffff, 'selfhdc_wire_overflow');
      if (next < 128) { return value; }
    }
    throw new Error('selfhdc_wire_varint');
  }
  fields(): WireField[] {
    const fields: WireField[] = [];
    while (this.offset < this.bytes.length) {
      hdcRequire(fields.length < 64, 'selfhdc_wire_fields');
      const key = this.integer();
      const field: WireField = { tag: Math.floor(key / 8), wire: key % 8, value: 0, data: new Uint8Array(0) };
      hdcRequire(field.tag > 0, 'selfhdc_wire_tag');
      if (field.wire === 0) { field.value = this.integer(); }
      else {
        const length = field.wire === 2 ? this.integer() : field.wire === 1 ? 8 : field.wire === 5 ? 4 : -1;
        hdcRequire(length >= 0 && this.offset + length <= this.bytes.length, 'selfhdc_wire_length');
        field.data = this.bytes.slice(this.offset, this.offset + length); this.offset += length;
      }
      fields.push(field);
    }
    return fields;
  }
}

export function hdcEncodeHandshake(value: SelfHdcHandshake): Uint8Array {
  return hdcJoin([blob(1, hdcUtf8('OHOS HDC')), scalar(2, value.auth), scalar(3, value.session),
    blob(4, hdcUtf8(value.target)), blob(5, value.data), blob(6, hdcUtf8(value.version))]);
}
export function hdcDecodeHandshake(bytes: Uint8Array): SelfHdcHandshake {
  hdcRequire(bytes.length <= 65536, 'selfhdc_handshake_size');
  const value: SelfHdcHandshake = { auth: 0, session: 0, target: '', data: new Uint8Array(0), version: '' };
  let banner = '';
  const seen = new Set<number>();
  for (const field of new WireReader(bytes).fields()) {
    if (field.tag > 6) { continue; }
    hdcRequire(!seen.has(field.tag), 'selfhdc_handshake_duplicate'); seen.add(field.tag);
    hdcRequire(field.wire === (field.tag === 2 || field.tag === 3 ? 0 : 2), 'selfhdc_handshake_field');
    if (field.tag === 1) { banner = hdcText(field.data); }
    else if (field.tag === 2) { value.auth = field.value; }
    else if (field.tag === 3) { value.session = field.value; }
    else if (field.tag === 4) { value.target = hdcText(field.data); }
    else if (field.tag === 5) { value.data = field.data; }
    else { value.version = hdcText(field.data); }
  }
  hdcRequire(banner === 'OHOS HDC' && seen.has(2), 'selfhdc_handshake_rejected');
  return value;
}

export function hdcEncodeTlv(tag: string, value: string): Uint8Array {
  hdcRequire(/^[A-Za-z0-9_]{1,16}$/.test(tag), 'selfhdc_tlv_tag');
  const bytes = hdcUtf8(value);
  return hdcJoin([hdcUtf8(tag.padEnd(16, ' ') + String(bytes.length).padEnd(16, ' ')), bytes]);
}
export function hdcDecodeTlv(bytes: Uint8Array): Map<string, string> {
  const result = new Map<string, string>();
  let offset = 0;
  while (offset < bytes.length) {
    hdcRequire(offset + 32 <= bytes.length && result.size < 64, 'selfhdc_tlv_truncated');
    const tag = hdcText(bytes.slice(offset, offset + 16)).trim();
    const text = hdcText(bytes.slice(offset + 16, offset + 32)).trim();
    hdcRequire(/^[A-Za-z0-9_]{1,16}$/.test(tag) && /^\d{1,10}$/.test(text) && !result.has(tag), 'selfhdc_tlv_invalid');
    const length = Number(text); offset += 32;
    hdcRequire(length <= 65536 && offset + length <= bytes.length, 'selfhdc_tlv_length');
    result.set(tag, hdcText(bytes.slice(offset, offset + length))); offset += length;
  }
  return result;
}

export function hdcEncodePacket(packet: SelfHdcPacket): Uint8Array {
  hdcRequire(packet.data.length <= HDC_MAX_PACKET, 'selfhdc_packet_size');
  const protect = hdcJoin([scalar(1, packet.channel), scalar(2, packet.command), scalar(3, 0), scalar(4, 9)]);
  const header = new Uint8Array(11);
  header[0] = 72; header[1] = 87; header[4] = 1;
  const view = new DataView(header.buffer);
  view.setUint16(5, protect.length, false); view.setUint32(7, packet.data.length, false);
  return hdcJoin([header, protect, packet.data]);
}

export class SelfHdcPacketStream {
  private pending: Uint8Array = new Uint8Array(0);
  feed(chunk: Uint8Array): SelfHdcPacket[] {
    hdcRequire(this.pending.length + chunk.length <= HDC_MAX_PACKET + 65547, 'selfhdc_receive_limit');
    this.pending = hdcJoin([this.pending, chunk]);
    const packets: SelfHdcPacket[] = [];
    let offset = 0;
    while (this.pending.length - offset >= 11) {
      const header = this.pending.subarray(offset, offset + 11);
      hdcRequire(header[0] === 72 && header[1] === 87 && header[2] === 0 && header[3] === 0 &&
        header[4] === 1, 'selfhdc_packet_header');
      const view = new DataView(header.buffer, header.byteOffset, 11);
      const protectLength = view.getUint16(5, false), dataLength = view.getUint32(7, false);
      hdcRequire(protectLength > 0 && protectLength <= 1024 && dataLength <= HDC_MAX_PACKET, 'selfhdc_packet_size');
      const length = 11 + protectLength + dataLength;
      if (this.pending.length - offset < length) { break; }
      const fields = new WireReader(this.pending.slice(offset + 11, offset + 11 + protectLength)).fields();
      let channel = 0, command = 0, checksum = 0, code = -1;
      const seen = new Set<number>();
      for (const field of fields) {
        if (field.tag > 4) { continue; }
        hdcRequire(field.wire === 0 && !seen.has(field.tag), 'selfhdc_packet_protect'); seen.add(field.tag);
        if (field.tag === 1) { channel = field.value; }
        else if (field.tag === 2) { command = field.value; }
        else if (field.tag === 3) { checksum = field.value; }
        else { code = field.value; }
      }
      hdcRequire(code === 9 && seen.has(2) && checksum <= 255, 'selfhdc_packet_protect');
      const data = this.pending.slice(offset + 11 + protectLength, offset + length);
      if (checksum !== 0) {
        let sum = 0; for (const byte of data) { sum = (sum + byte) & 255; }
        hdcRequire(sum === checksum, 'selfhdc_packet_checksum');
      }
      packets.push({ channel: channel, command: command, data: data }); offset += length;
    }
    this.pending = this.pending.slice(offset);
    return packets;
  }
}

export function hdcShellCommand(command: string, marker: string): string {
  hdcRequire(command.length > 0 && hdcUtf8(command).length <= 32768 && command.indexOf('\0') < 0,
    'selfhdc_command_invalid');
  hdcRequire(/^AMITA_HDC_[A-Fa-f0-9]{32}$/.test(marker), 'selfhdc_marker_invalid');
  // The subshell contains exit/exec and trailing comments; stdout remains byte-preserving.
  return '(\n' + command + '\n)\n_amita_hdc_rc=$?\nprintf "\\n' + marker + ':%s\\n" "$_amita_hdc_rc"\n';
}
export interface SelfHdcShellTail { stdout: Uint8Array; completed: boolean; exitCode?: number; }
export function hdcShellTail(bytes: Uint8Array, marker: string): SelfHdcShellTail {
  const result: SelfHdcShellTail = { stdout: bytes, completed: false };
  const prefix = hdcUtf8('\n' + marker + ':');
  // Only inspect the ASCII suffix, preserving arbitrary binary output before it.
  const start = Math.max(0, bytes.length - prefix.length - 5);
  for (let offset = start; offset + prefix.length < bytes.length; offset++) {
    let matched = true;
    for (let index = 0; index < prefix.length; index++) {
      if (bytes[offset + index] !== prefix[index]) { matched = false; break; }
    }
    if (!matched) { continue; }
    const tail = hdcText(bytes.slice(offset + prefix.length));
    if (!/^\d{1,3}\n$/.test(tail)) { continue; }
    const exitCode = Number(tail.trim());
    if (exitCode > 255) { continue; }
    return { stdout: bytes.slice(0, offset), completed: true, exitCode: exitCode };
  }
  return result;
}
