import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import worker, {ReverseRelay} from '../worker.mjs';
import {execute} from '../host.mjs';
import {PROTOCOL, sha256, policy, fingerprint, toBase64} from '../protocol.mjs';

async function fixture(t, overrides = {}) {
  const db = new DatabaseSync(':memory:'); t.after(() => db.close());
  const config = {format: PROTOCOL, client_token_sha256: await sha256('client-token-fixture-'.repeat(3)),
    members: [{id: 'fixture-member', token_sha256: await sha256('member-token-fixture-'.repeat(3))}],
    routes: [{method: 'POST', path: '/mcp', member_id: 'fixture-member'}],
    daily_limit: 10, max_receipts: 100, timeout_ms: 1000, ...overrides};
  const socket = {readyState: 1, sent: [], attachment: {member: 'fixture-member', connection: 'c'.repeat(64),
    token_hash: config.members[0].token_sha256}, deserializeAttachment() { return this.attachment; },
    send(raw) { this.sent.push(JSON.parse(raw)); }, close() { this.readyState = 3; }};
  const ctx = {storage: {sql: {exec(query, ...args) {
    const statement = db.prepare(query);
    const rows = /^SELECT\b/.test(query.trim()) ? statement.all(...args) : (statement.run(...args), []);
    return {toArray: () => rows};
  }}, async sync() {}}, getWebSockets: tag => !tag || tag === socket.attachment.member ? [socket] : [],
    setWebSocketAutoResponse() {}};
  const env = {PUBLIC_ORIGIN: 'https://relay.example.invalid', RELAY_POLICY: JSON.stringify(config)};
  const relay = new ReverseRelay(ctx, env);
  function request(id, body = '{"jsonrpc":"2.0","id":7,"method":"tools/list"}', route = '/mcp') {
    return new Request(env.PUBLIC_ORIGIN + route, {method: 'POST', body, headers: {
      'x-amita-relay-access': 'client-token-fixture-'.repeat(3), 'x-amita-relay-request-id': id,
      authorization: 'Bearer ORIGINAL-OAUTH-UNCHANGED', 'content-type': 'application/json'}});
  }
  return {db, ctx, config, env, socket, relay, request};
}

test('member tickets are independent, expiring, single-use; no admin route or origin broadening', async t => {
  const f = await fixture(t);
  const request = token => new Request(f.env.PUBLIC_ORIGIN + '/_mesh/relay/ticket', {method: 'POST',
    headers: {authorization: 'Bearer ' + token}, body: JSON.stringify({member_id: 'fixture-member'})});
  assert.equal((await f.relay.fetch(request('client-token-fixture-'.repeat(3)))).status, 401);
  const response = await f.relay.fetch(request('member-token-fixture-'.repeat(3)));
  assert.equal(response.status, 200); const ticket = await response.json();
  assert.equal((await f.relay.takeTicket(ticket.ticket, f.config)).id, 'fixture-member');
  assert.equal(await f.relay.takeTicket(ticket.ticket, f.config), null);
  f.relay.sql.exec('INSERT INTO tickets VALUES(?,?,?)', 'fixture-member', await sha256('expired'), Date.now() - 1);
  assert.equal(await f.relay.takeTicket('expired', f.config), null);
  assert.equal((await f.relay.fetch(f.request('route-test-01', '{}', '/v1/tasks'))).status, 404);
  assert.equal((await worker.fetch(new Request('https://wrong.invalid/mcp'), f.env)).status, 400);
  assert.throws(() => policy(JSON.stringify({...f.config,
    routes: [{method: 'POST', path: '/token', member_id: 'fixture-member'}]})), /relay_route_invalid/);
});

test('real loopback HTTP preserves MCP bytes/auth/challenge; durable duplicate never redelivers', async t => {
  const f = await fixture(t); const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'amita-relay.'));
  t.after(() => fs.rm(directory, {recursive: true, force: true})); let hits = 0;
  const server = http.createServer(async (request, response) => {
    hits += 1; let body = ''; for await (const chunk of request) body += chunk;
    assert.equal(request.headers.authorization, 'Bearer ORIGINAL-OAUTH-UNCHANGED');
    assert.equal(request.headers.host, 'original-resource.invalid');
    assert.equal(body, '{"jsonrpc":"2.0","id":7,"method":"tools/list"}');
    response.writeHead(401, {'content-type': 'application/json',
      'www-authenticate': 'Bearer resource_metadata="https://original-resource.invalid/.well-known/oauth-protected-resource/mcp"'});
    response.end('{"error":"original-application-challenge"}');
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const host = {state_directory: directory, upstream_origin: `http://127.0.0.1:${server.address().port}`,
    upstream_host: 'original-resource.invalid', routes: [{method: 'POST', path: '/mcp'}]};
  let original;
  f.socket.send = raw => {
    const frame = JSON.parse(raw); f.socket.sent.push(frame);
    execute(host, frame, f.socket.attachment.connection).then(response => {
      original = response; return f.relay.webSocketMessage(f.socket, JSON.stringify(response));
    });
  };
  const response = await f.relay.fetch(f.request('original-request-01'));
  assert.equal(response.status, 401);
  assert.equal(response.headers.get('www-authenticate'), 'Bearer resource_metadata="https://original-resource.invalid/.well-known/oauth-protected-resource/mcp"');
  assert.equal(await response.text(), '{"error":"original-application-challenge"}');
  assert.equal((await f.relay.fetch(f.request('original-request-01'))).status, 409);
  assert.equal((await f.relay.fetch(f.request('original-request-01', '{"changed":true}'))).status, 409);
  assert.equal(hits, 1); assert.equal(f.socket.sent.length, 1);
  const receipt = JSON.stringify(f.relay.one('SELECT * FROM attempts WHERE id=?', 'original-request-01'));
  assert(!receipt.includes('ORIGINAL-OAUTH') && !receipt.includes('tools/list'));
  const local = await fs.readFile(path.join(directory, 'original-request-01.json'), 'utf8');
  assert(!local.includes('ORIGINAL-OAUTH') && !local.includes('tools/list'));
  const previous = f.relay.one('SELECT response_sha256 FROM attempts WHERE id=?', 'original-request-01').response_sha256;
  await f.relay.webSocketMessage(f.socket, JSON.stringify({...original, body_base64: toBase64(new TextEncoder().encode('changed'))}));
  assert.equal(f.relay.one('SELECT response_sha256 FROM attempts WHERE id=?', 'original-request-01').response_sha256, previous);
});

test('deadline/cold reconstruction preserve unknown ID; offline and capacities never silently switch members', async t => {
  const f = await fixture(t);
  const first = await f.relay.fetch(f.request('unknown-request-01'));
  assert.equal(first.status, 504); assert.equal((await first.json()).outcome, 'unknown');
  assert.equal(f.socket.sent.length, 1);
  const restored = new ReverseRelay(f.ctx, f.env);
  assert.equal((await restored.fetch(f.request('unknown-request-01'))).status, 409);
  assert.equal(f.socket.sent.length, 1);
  f.socket.readyState = 3;
  const offline = await restored.fetch(f.request('independent-request-02'));
  assert.equal(offline.status, 503); assert.equal((await offline.json()).outcome, 'not_attempted');
  assert.equal(restored.one('SELECT COUNT(*) AS count FROM attempts').count, 1);
  f.socket.readyState = 1;
  f.env.RELAY_POLICY = JSON.stringify({...f.config, max_receipts: 1});
  assert.equal((await restored.fetch(f.request('capacity-request-03'))).status, 429);
  assert.equal((await restored.fetch(f.request('oversized-request-04', 'x'.repeat(262145)))).status, 413);
  assert.equal(f.socket.sent.length, 1);
});

test('host fsynced exclusive admission blocks overlapping delivery; expired/disallowed requests never reach upstream', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'amita-relay-host.'));
  t.after(() => fs.rm(directory, {recursive: true, force: true}));
  const config = {state_directory: directory, upstream_origin: 'http://127.0.0.1:12345',
    upstream_host: 'original.invalid', routes: [{method: 'POST', path: '/mcp'}]};
  const body = new TextEncoder().encode('{"original":"message"}'); const selected = {'content-type': 'application/json'};
  const frame = {type: 'request', protocol: PROTOCOL, connection_id: 'c'.repeat(64), request_id: 'host-original-01',
    method: 'POST', path: '/mcp', headers: selected, body_base64: toBase64(body), deadline_ms: Date.now() + 1000,
    fingerprint: await fingerprint('POST', '/mcp', selected, body)};
  let hits = 0;
  const perform = async () => { hits += 1; return {status: 200, headers: {}, body: new Uint8Array()}; };
  const results = await Promise.allSettled([execute(config, frame, frame.connection_id, perform),
    execute(config, frame, frame.connection_id, perform)]);
  assert.equal(results.filter(value => value.status === 'fulfilled').length, 1); assert.equal(hits, 1);
  await assert.rejects(execute(config, {...frame, request_id: 'expired-request-02', deadline_ms: Date.now() - 1}, frame.connection_id, perform));
  await assert.rejects(execute(config, {...frame, request_id: 'admin-request-03', path: '/v1/tasks'}, frame.connection_id, perform));
  assert.equal(hits, 1);
  assert.equal((await fs.stat(path.join(directory, frame.request_id + '.json'))).mode & 0o777, 0o600);
});
