import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import worker, {ReverseRelay} from '../worker.mjs';
import {execute} from '../host.mjs';
import {PROTOCOL, OAUTH_ROUTE, sha256, policy, fingerprint, toBase64} from '../protocol.mjs';

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

test('public OAuth preserves callback cookies and redirects through an explicit member handoff', async t => {
  const route = {method: 'GET', path: '/auth/callback', member_id: 'fixture-member'};
  const f = await fixture(t, {public_oauth: true, routes: [route]});
  const member = {id: 'backup-member', token_sha256: await sha256('backup-token-fixture-'.repeat(3))};
  const backup = {...f.socket, sent: [], attachment: {member: member.id, connection: 'b'.repeat(64),
    token_hash: member.token_sha256}};
  f.ctx.getWebSockets = tag => [f.socket, backup].filter(socket => socket.attachment.member === tag);
  f.config.members.push(member);
  const controller = 'fixture-route-control-'.repeat(3);
  f.config.oauth_control = {token_sha256: await sha256(controller), group: 'fixture-group', scope: 'fixture-scope',
    holders: [{owner: 'primary', member_id: 'fixture-member'}, {owner: 'backup', member_id: member.id}]};
  assert.throws(() => policy(JSON.stringify({...f.config, routes: [...f.config.routes,
    {method: 'POST', path: '/token', member_id: member.id}]})), /relay_oauth_single_active_member_required/);
  f.env.RELAY_POLICY = JSON.stringify(f.config);
  f.relay = new ReverseRelay(f.ctx, f.env);
  f.env.RELAY = {idFromName: name => name, get: () => f.relay};
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'amita-oauth-relay.'));
  t.after(() => fs.rm(dir, {recursive: true, force: true}));
  for (const name of ['primary', 'backup']) await fs.mkdir(path.join(dir, name));
  const hits = [];
  const server = http.createServer((request, response) => {
    hits.push(request.url);
    assert.equal(request.headers.host, 'relay.example.invalid');
    assert.equal(request.headers['x-forwarded-proto'], 'https');
    assert.equal(request.headers.cookie, 'private-consent=example');
    response.writeHead(302, {'location': 'https://chatgpt.com/connector_platform_oauth_redirect?state=original',
      'set-cookie': ['private-consent=; Max-Age=0; Secure', 'new-session=value; Secure; HttpOnly']});
    response.end();
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  let started, release;
  const firstStarted = new Promise(resolve => { started = resolve; });
  const firstCanFinish = new Promise(resolve => { release = resolve; });
  for (const [socket, name] of [[f.socket, 'primary'], [backup, 'backup']]) {
    const host = {state_directory: path.join(dir, name), upstream_origin: `http://127.0.0.1:${server.address().port}`,
      upstream_host: 'relay.example.invalid', public_oauth: true, routes: [route]};
    socket.send = async raw => {
      const frame = JSON.parse(raw); socket.sent.push(frame);
      if (socket === f.socket) { started(); await firstCanFinish; }
      const response = await execute(host, frame, socket.attachment.connection);
      await f.relay.webSocketMessage(socket, JSON.stringify(response));
    };
  }
  const callback = state => worker.fetch(new Request(f.env.PUBLIC_ORIGIN + '/auth/callback?code=private%2Bcode&state=' + state,
    {headers: {cookie: 'private-consent=example', 'x-amita-relay-request-id': 'attacker-selected'}}), f.env);
  const firstPending = callback('first');
  await firstStarted;
  const grant = {group: 'fixture-group', scope: 'fixture-scope', owner: 'backup', epoch: 1, index: 10,
    proposal_sha256: '1'.repeat(64), operation_id: 'real-fixture-handoff'};
  const command = {format: OAUTH_ROUTE, grant, previous: null, barrier_index: 11, observed_ms: Date.now()};
  const control = (body, token = controller) => worker.fetch(new Request(f.env.PUBLIC_ORIGIN + '/_mesh/relay/oauth-route',
    {method: body ? 'POST' : 'GET', body: body ? JSON.stringify(body) : undefined,
      headers: {'Authorization': 'Bearer ' + token}}), f.env);
  try {
    assert.equal((await control(command, 'client-token-fixture-'.repeat(3))).status, 401);
    assert.equal((await control({...command, observed_ms: Date.now() - 31000})).status, 409);
    backup.readyState = 3;
    assert.equal((await control(command)).status, 503);
    assert.equal((await (await control()).json()).current, null);
    backup.readyState = 1;
    const moved = await control(command);
    assert.equal(moved.status, 200);
    assert.deepEqual((await moved.json()).applied, grant);
    assert.equal((await (await control(command)).json()).duplicate, true);
    assert.equal((await control({...command, grant: {...grant, operation_id: 'older-conflict'}})).status, 409);
  } finally { release(); }
  const first = await firstPending;
  assert.equal(first.status, 302);
  assert.equal(first.headers.get('location'), 'https://chatgpt.com/connector_platform_oauth_redirect?state=original');
  assert.equal(first.headers.getSetCookie().length, 2);
  assert.notEqual(first.headers.get('x-amita-relay-request-id'), 'attacker-selected');
  f.relay = new ReverseRelay(f.ctx, f.env);
  assert.deepEqual((await (await control()).json()).current, grant);
  assert.equal((await callback('first')).status, 409);
  assert.equal(backup.sent.length, 0);
  assert.equal((await callback('second')).status, 302);
  assert.equal(f.socket.sent.length, 1);
  assert.equal(backup.sent.length, 1);
  assert.deepEqual(hits, ['/auth/callback?code=private%2Bcode&state=first', '/auth/callback?code=private%2Bcode&state=second']);
  const raw = await fs.readFile(path.join(dir, 'primary', f.socket.sent[0].request_id + '.json'), 'utf8');
  for (const secret of ['private-consent', 'private%2Bcode', 'new-session', 'state=first']) {
    assert(!raw.includes(secret));
    assert(!JSON.stringify(f.relay.one('SELECT * FROM attempts WHERE id=?', f.socket.sent[0].request_id)).includes(secret));
  }
  const admin = await worker.fetch(new Request(f.env.PUBLIC_ORIGIN + '/_mesh/relay/bootstrap'), f.env);
  assert.equal(admin.status, 401);
});
