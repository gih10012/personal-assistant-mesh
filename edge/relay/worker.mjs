import {PROTOCOL, MAX_REQUEST, MAX_RESPONSE, REQUEST_HEADERS, RESPONSE_HEADERS, identifier,
  sha256, random, fromBase64, toBase64, headers, fingerprint, policy, boundedBody, exact} from './protocol.mjs';

const json = (value, status = 200) => Response.json(value, {status, headers: {'cache-control': 'no-store'}});
const failure = (error, status, outcome = 'not_attempted', request_id = null) => json({
  protocol: PROTOCOL, error, outcome, request_id, automatic_retry: false, retry_with_new_id: false}, status);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.origin !== env.PUBLIC_ORIGIN || url.search || url.hash || /%|\\/.test(url.pathname)) {
      return failure('relay_exact_origin_and_path_required', 400);
    }
    try {
      policy(env.RELAY_POLICY); // Missing private configuration always fails closed.
      return await env.RELAY.get(env.RELAY.idFromName('authenticated-transport-v1')).fetch(request);
    } catch { return failure('relay_unavailable', 503, 'unknown'); }
  }
};

export class ReverseRelay {
  constructor(ctx, env) {
    this.ctx = ctx; this.env = env; this.sql = ctx.storage.sql; this.pending = new Map();
    this.sql.exec('CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,member TEXT NOT NULL,connection TEXT NOT NULL,state TEXT NOT NULL,deadline INTEGER NOT NULL,status INTEGER,response_sha256 TEXT)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS tickets(member TEXT PRIMARY KEY,digest TEXT NOT NULL,expires INTEGER NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS budgets(day TEXT PRIMARY KEY,requests INTEGER NOT NULL,tickets INTEGER NOT NULL)');
    // A cold start cannot prove whether an admitted original request executed.
    this.sql.exec("UPDATE attempts SET state='unknown' WHERE state='sent'");
    if (globalThis.WebSocketRequestResponsePair) {
      ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair('mesh-ping', 'mesh-pong'));
    }
  }
  one(query, ...args) { return this.sql.exec(query, ...args).toArray()[0]; }
  quota(kind, maximum) {
    const day = new Date().toISOString().slice(0, 10);
    this.sql.exec('DELETE FROM budgets WHERE day<>?', day);
    this.sql.exec('INSERT OR IGNORE INTO budgets VALUES(?,0,0)', day);
    const count = this.one(`SELECT ${kind} AS count FROM budgets WHERE day=?`, day).count;
    if (count >= maximum) return false;
    this.sql.exec(`UPDATE budgets SET ${kind}=${kind}+1 WHERE day=?`, day);
    return true;
  }
  socket(member) {
    return this.ctx.getWebSockets(member.id).find(ws => {
      const attachment = ws.deserializeAttachment();
      return ws.readyState === 1 && attachment?.token_hash === member.token_sha256;
    });
  }
  async clientAuthorized(request, config) {
    const token = request.headers.get('x-amita-relay-access');
    return typeof token === 'string' && token.length >= 32 && token.length <= 256
      && await sha256(token) === config.client_token_sha256;
  }
  async fetch(request) {
    const url = new URL(request.url); const config = policy(this.env.RELAY_POLICY);
    if (url.pathname === '/_mesh/relay/ticket' && request.method === 'POST') return this.ticket(request, config);
    if (url.pathname === '/_mesh/relay/connect' && request.method === 'GET') return this.connect(request, config);
    if (!await this.clientAuthorized(request, config)) return failure('relay_admission_denied', 401);
    if (url.pathname === '/_mesh/relay/bootstrap' && request.method === 'GET') return json({
      protocol: PROTOCOL, oauth_migration_complete: false, automatic_authority_switch: false,
      routes: config.routes.map(route => ({method: route.method, path: route.path,
        connected: Boolean(this.socket(config.members.find(member => member.id === route.member_id)))}))});
    const receipt = /^\/_mesh\/relay\/receipt\/([A-Za-z0-9_-]{8,128})$/.exec(url.pathname);
    if (receipt && request.method === 'GET') {
      const found = this.one('SELECT id,state,deadline,status,response_sha256 FROM attempts WHERE id=?', receipt[1]);
      return found ? json({...found, protocol: PROTOCOL, automatic_retry: false,
        execution_verified: false, response_body_retained: false}) : failure('relay_receipt_not_found', 404);
    }
    const route = config.routes.find(item => item.method === request.method && item.path === url.pathname);
    if (!route) return failure('relay_route_not_allowed', 404);
    return this.forward(request, route, config);
  }
  async ticket(request, config) {
    let input;
    try { input = JSON.parse(new TextDecoder().decode(await boundedBody(request, 1024))); }
    catch { return failure('relay_ticket_request_invalid', 400); }
    const token = /^Bearer ([A-Za-z0-9_-]{32,256})$/.exec(request.headers.get('authorization') || '')?.[1];
    const member = config.members.find(item => item.id === input?.member_id);
    if (!member || !exact(input, ['member_id']) || typeof token !== 'string'
        || token.length < 32 || token.length > 256 || await sha256(token) !== member.token_sha256) {
      return failure('relay_member_not_authorized', 401);
    }
    if (!this.quota('tickets', 1000)) return failure('relay_ticket_budget_exhausted', 429);
    const ticket = random();
    this.sql.exec('INSERT OR REPLACE INTO tickets VALUES(?,?,?)', member.id, await sha256(ticket), Date.now() + 20000);
    await this.ctx.storage.sync();
    return json({protocol: PROTOCOL, ticket, expires_in: 20});
  }
  async connect(request, config) {
    const protocols = (request.headers.get('sec-websocket-protocol') || '').split(',').map(x => x.trim());
    const ticket = protocols.find(value => /^ticket\.[0-9a-f]{64}$/.test(value))?.slice(7);
    if (request.headers.get('upgrade')?.toLowerCase() !== 'websocket' || !ticket
        || protocols.length !== 2 || !protocols.includes('mesh-relay-v1')) return failure('relay_upgrade_required', 400);
    const member = await this.takeTicket(ticket, config);
    if (!member) return failure('relay_ticket_expired_or_used', 401);
    for (const old of this.ctx.getWebSockets(member.id)) {
      this.disconnect(old); old.close(1012, 'transport connection replaced');
    }
    const pair = new WebSocketPair(); const [client, server] = Object.values(pair);
    const connection = random();
    this.ctx.acceptWebSocket(server, [member.id]);
    server.serializeAttachment({member: member.id, connection, token_hash: member.token_sha256});
    server.send(JSON.stringify({type: 'ready', protocol: PROTOCOL, member_id: member.id, connection_id: connection}));
    return new Response(null, {status: 101, webSocket: client, headers: {'sec-websocket-protocol': 'mesh-relay-v1'}});
  }
  async takeTicket(ticket, config) {
    const known = this.one('SELECT member FROM tickets WHERE digest=? AND expires>?', await sha256(ticket), Date.now());
    const member = config.members.find(item => item.id === known?.member);
    if (!member) return null;
    this.sql.exec('DELETE FROM tickets WHERE member=?', member.id);
    await this.ctx.storage.sync();
    return member;
  }
  async forward(request, route, config) {
    const id = request.headers.get('x-amita-relay-request-id');
    if (!identifier(id)) return failure('relay_original_request_id_required', 400);
    let raw, selected;
    try { raw = await boundedBody(request, MAX_REQUEST); selected = headers(request.headers, REQUEST_HEADERS); }
    catch { return failure('relay_bounded_request_required', 413, 'not_attempted', id); }
    const signature = await fingerprint(request.method, route.path, selected, raw);
    const known = this.one('SELECT fingerprint,state FROM attempts WHERE id=?', id);
    if (known) return failure(known.fingerprint === signature ? 'relay_reconcile_original' : 'relay_request_id_conflict',
      409, known.fingerprint === signature ? known.state : 'rejected', id);
    const member = config.members.find(item => item.id === route.member_id); const socket = this.socket(member);
    if (!socket) return failure('relay_member_offline', 503, 'not_attempted', id);
    if (this.one("SELECT COUNT(*) AS count FROM attempts WHERE state='sent'").count >= 4) {
      return failure('relay_inflight_capacity', 429, 'not_attempted', id);
    }
    if (this.one('SELECT COUNT(*) AS count FROM attempts').count >= config.max_receipts
        || !this.quota('requests', config.daily_limit)) return failure('relay_receipt_or_daily_capacity', 429, 'not_attempted', id);
    const attachment = socket.deserializeAttachment(); const deadline = Date.now() + config.timeout_ms;
    this.sql.exec("INSERT INTO attempts VALUES(?,?,?,?,'sent',?,NULL,NULL)", id, signature, member.id, attachment.connection, deadline);
    // Durable reservation/output gate precedes the one and only delivery.
    await this.ctx.storage.sync();
    return new Promise(resolve => {
      const timer = setTimeout(() => this.unknown(id, 'relay_deadline_unknown'), config.timeout_ms);
      this.pending.set(id, {resolve, timer});
      try { socket.send(JSON.stringify({type: 'request', protocol: PROTOCOL, request_id: id,
        connection_id: attachment.connection, fingerprint: signature, method: route.method, path: route.path,
        headers: selected, body_base64: toBase64(raw), deadline_ms: deadline})); }
      catch { this.unknown(id, 'relay_connection_unknown'); }
    });
  }
  async unknown(id, reason) {
    this.sql.exec("UPDATE attempts SET state='unknown' WHERE id=? AND state='sent'", id);
    await this.ctx.storage.sync();
    const pending = this.pending.get(id);
    if (pending) { clearTimeout(pending.timer); this.pending.delete(id); pending.resolve(failure(reason, 504, 'unknown', id)); }
  }
  async webSocketMessage(socket, message) {
    const attachment = socket.deserializeAttachment();
    try {
      const config = policy(this.env.RELAY_POLICY);
      if (!config.members.some(member => member.id === attachment?.member && member.token_sha256 === attachment.token_hash)
          || typeof message !== 'string' || message.length > 800000) throw new Error('relay_frame_invalid');
      const frame = JSON.parse(message);
      if (!exact(frame, ['type', 'protocol', 'connection_id', 'request_id', 'status', 'headers', 'body_base64', 'outcome'])
          || frame.type !== 'response' || frame.protocol !== PROTOCOL || frame.connection_id !== attachment.connection
          || !identifier(frame.request_id) || !Number.isInteger(frame.status) || frame.status < 200 || frame.status > 599
          || !['responded', 'unknown', 'not_attempted'].includes(frame.outcome)) throw new Error('relay_frame_invalid');
      const attempt = this.one('SELECT member,connection,state,status,response_sha256 FROM attempts WHERE id=?', frame.request_id);
      if (!attempt || attempt.member !== attachment.member || attempt.connection !== attachment.connection) throw new Error('relay_frame_unowned');
      const raw = fromBase64(frame.body_base64, MAX_RESPONSE); const selected = headers(frame.headers, RESPONSE_HEADERS);
      const bodyHash = await sha256(raw);
      if (attempt.state === 'completed') {
        if (frame.outcome !== 'responded' || attempt.status !== frame.status || attempt.response_sha256 !== bodyHash) {
          throw new Error('relay_original_response_conflict');
        }
        return;
      }
      this.sql.exec('UPDATE attempts SET state=?,status=?,response_sha256=? WHERE id=?',
        frame.outcome === 'responded' ? 'completed' : frame.outcome, frame.status, bodyHash, frame.request_id);
      await this.ctx.storage.sync();
      const pending = this.pending.get(frame.request_id);
      if (pending) {
        clearTimeout(pending.timer); this.pending.delete(frame.request_id);
        pending.resolve(new Response(raw.length ? raw : null, {status: frame.status,
          headers: {...selected, 'x-amita-relay-request-id': frame.request_id, 'cache-control': 'no-store'}}));
      }
    } catch { this.disconnect(socket); socket.close(1008, 'invalid transport response'); }
  }
  disconnect(socket) {
    const attachment = socket.deserializeAttachment();
    if (!attachment) return;
    for (const row of this.sql.exec("SELECT id FROM attempts WHERE connection=? AND state='sent'", attachment.connection).toArray()) {
      this.unknown(row.id, 'relay_disconnected_unknown');
    }
  }
  webSocketClose(socket) { this.disconnect(socket); }
  webSocketError(socket) { this.disconnect(socket); }
}
