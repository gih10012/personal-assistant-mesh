import {PROTOCOL, MAX_REQUEST, MAX_RESPONSE, REQUEST_HEADERS, RESPONSE_HEADERS, OAUTH_REQUEST_HEADERS,
  OAUTH_RESPONSE_HEADERS, OAUTH_ROUTE, allowedQuery, responseCookies, identifier, writerId, sha256, random, fromBase64, toBase64,
  headers, fingerprint, policy, boundedBody, exact} from './protocol.mjs';

const json = (value, status = 200) => Response.json(value, {status, headers: {'cache-control': 'no-store'}});
const failure = (error, status, outcome = 'not_attempted', request_id = null) => json({
  protocol: PROTOCOL, error, outcome, request_id, automatic_retry: false, retry_with_new_id: false}, status);
const OAUTH_SINGLE_USE = new Set(['POST /token', 'POST /consent', 'GET /auth/callback', 'POST /revoke']);
const OAUTH_ROUTE_PATH = '/_mesh/relay/oauth-route';
const isSha = value => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value);
const positive = value => Number.isSafeInteger(value) && value > 0;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.origin !== env.PUBLIC_ORIGIN || url.hash || /%|\\/.test(url.pathname)) {
      return failure('relay_exact_origin_and_path_required', 400);
    }
    try {
      const config = policy(env.RELAY_POLICY); // Missing private configuration always fails closed.
      if (!allowedQuery(request.method, url.pathname, url.search, config.public_oauth)) {
        return failure('relay_exact_origin_and_path_required', 400);
      }
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
    if (policy(env.RELAY_POLICY).oauth_control) {
      this.sql.exec('CREATE TABLE IF NOT EXISTS oauth_writer_routes(operation_id TEXT PRIMARY KEY,epoch INTEGER UNIQUE NOT NULL,head_index INTEGER NOT NULL,grant TEXT NOT NULL,member TEXT NOT NULL)');
    }
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
    if (!allowedQuery(request.method, url.pathname, url.search, config.public_oauth)) {
      return failure('relay_exact_origin_and_path_required', 400);
    }
    if (url.pathname === '/_mesh/relay/ticket' && request.method === 'POST') return this.ticket(request, config);
    if (url.pathname === '/_mesh/relay/connect' && request.method === 'GET') return this.connect(request, config);
    if (url.pathname === OAUTH_ROUTE_PATH && config.oauth_control) return this.controlRoute(request, config);
    const route = config.routes.find(item => item.method === request.method && item.path === url.pathname);
    if (route && config.public_oauth) {
      // OAuth and MCP identity remain the original application's decisions.
      // Public callers cannot choose transport IDs, backends or control routes.
      const original = await this.clientAuthorized(request, config);
      return this.forward(request, route, config, original, url.search);
    }
    if (!await this.clientAuthorized(request, config)) return failure('relay_admission_denied', 401);
    if (url.pathname === '/_mesh/relay/bootstrap' && request.method === 'GET') {
      const current = this.writerRoute(config);
      return json({protocol: PROTOCOL, oauth_migration_complete: false, automatic_authority_switch: false,
        routes: config.routes.map(route => ({method: route.method, path: route.path,
          connected: Boolean(this.socket(this.activeMember(route, config, current)))}))});
    }
    const receipt = /^\/_mesh\/relay\/receipt\/([A-Za-z0-9_-]{8,128})$/.exec(url.pathname);
    if (receipt && request.method === 'GET') {
      const found = this.one('SELECT id,state,deadline,status,response_sha256 FROM attempts WHERE id=?', receipt[1]);
      return found ? json({...found, protocol: PROTOCOL, automatic_retry: false,
        execution_verified: false, response_body_retained: false}) : failure('relay_receipt_not_found', 404);
    }
    if (!route) return failure('relay_route_not_allowed', 404);
    return this.forward(request, route, config, true);
  }
  writerRoute(config) {
    if (!config.oauth_control) return null;
    const row = this.one('SELECT grant,member FROM oauth_writer_routes ORDER BY epoch DESC LIMIT 1');
    if (!row) return null;
    const grant = JSON.parse(row.grant);
    if (grant.group !== config.oauth_control.group || grant.scope !== config.oauth_control.scope ||
        !config.oauth_control.holders.some(item => item.owner === grant.owner && item.member_id === row.member)) {
      throw new Error('relay_oauth_route_policy_changed');
    }
    return grant;
  }
  activeMember(route, config, active = this.writerRoute(config)) {
    const member = active ? config.oauth_control.holders.find(item => item.owner === active.owner).member_id : route.member_id;
    return config.members.find(item => item.id === member);
  }
  async controlRoute(request, config) {
    const control = config.oauth_control;
    const token = /^Bearer ([A-Za-z0-9_-]{32,256})$/.exec(request.headers.get('authorization') || '')?.[1];
    if (!token || await sha256(token) !== control.token_sha256) return failure('relay_oauth_controller_required', 401);
    if (request.method === 'GET') return json({format: OAUTH_ROUTE, current: this.writerRoute(config)});
    if (request.method !== 'POST') return failure('relay_oauth_route_method', 405);
    let command;
    try { command = JSON.parse(new TextDecoder().decode(await boundedBody(request, 4096))); }
    catch { return failure('relay_oauth_route_command_invalid', 400); }
    if (!exact(command, ['format', 'grant', 'previous', 'barrier_index', 'observed_ms']) || command.format !== OAUTH_ROUTE) {
      return failure('relay_oauth_route_command_invalid', 400);
    }
    const grant = command.grant;
    if (!exact(grant, ['group', 'scope', 'owner', 'epoch', 'index', 'proposal_sha256', 'operation_id'])
        || grant.group !== control.group || grant.scope !== control.scope || !writerId(grant.operation_id)
        || !positive(grant.epoch) || !positive(grant.index) || !isSha(grant.proposal_sha256)) {
      return failure('relay_oauth_grant_binding_invalid', 400);
    }
    const holder = control.holders.find(item => item.owner === grant.owner);
    if (!holder) return failure('relay_oauth_target_not_registered', 400);
    const ordered = Object.fromEntries(Object.keys(grant).sort().map(key => [key, grant[key]]));
    const original = this.one('SELECT grant FROM oauth_writer_routes WHERE operation_id=?', grant.operation_id);
    if (original) return original.grant === JSON.stringify(ordered)
      ? json({format: OAUTH_ROUTE, applied: JSON.parse(original.grant), duplicate: true})
      : failure('relay_oauth_original_grant_conflict', 409);
    const active = this.writerRoute(config); // Re-read after all asynchronous parsing.
    if (!positive(command.barrier_index) || command.barrier_index <= grant.index || !positive(command.observed_ms)
        || Date.now() - command.observed_ms > 30000 || command.observed_ms - Date.now() > 5000) {
      return failure('relay_oauth_current_confirmation_required', 409);
    }
    if (command.previous !== (active?.proposal_sha256 ?? null) || grant.epoch <= (active?.epoch ?? 0)
        || grant.index <= (active?.index ?? 0)) return failure('relay_oauth_route_predecessor_changed', 409);
    if (!this.socket(config.members.find(member => member.id === holder.member_id))) {
      return failure('relay_oauth_target_disconnected', 503);
    }
    this.sql.exec('INSERT INTO oauth_writer_routes VALUES(?,?,?,?,?)', grant.operation_id, grant.epoch,
      grant.index, JSON.stringify(ordered), holder.member_id);
    await this.ctx.storage.sync();
    return json({format: OAUTH_ROUTE, applied: ordered, duplicate: false});
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
  async forward(request, route, config, original, query) {
    const oauth = query !== undefined;
    let id = original ? request.headers.get('x-amita-relay-request-id') : null;
    if (original && !identifier(id)) return failure('relay_original_request_id_required', 400);
    let raw, selected;
    try { raw = await boundedBody(request, MAX_REQUEST); selected = headers(request.headers, oauth ? OAUTH_REQUEST_HEADERS : REQUEST_HEADERS); }
    catch { return failure('relay_bounded_request_required', 413, 'not_attempted', id); }
    const signature = await fingerprint(request.method, route.path, selected, raw, query);
    // Guard exact repeats of the single-use OAuth operations at the transport
    // as well. Ordinary MCP calls keep their existing application idempotency.
    if (!original) id = OAUTH_SINGLE_USE.has(`${route.method} ${route.path}`)
      ? 'oauth-' + signature : 'public-' + random();
    const known = this.one('SELECT fingerprint,state FROM attempts WHERE id=?', id);
    if (known) return failure(known.fingerprint === signature ? 'relay_reconcile_original' : 'relay_request_id_conflict',
      409, known.fingerprint === signature ? known.state : 'rejected', id);
    const member = this.activeMember(route, config); const socket = this.socket(member);
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
      this.pending.set(id, {resolve, timer, oauth});
      try { socket.send(JSON.stringify({type: 'request', protocol: PROTOCOL, request_id: id,
        connection_id: attachment.connection, fingerprint: signature, method: route.method, path: route.path,
        headers: selected, body_base64: toBase64(raw), deadline_ms: deadline,
        ...(oauth ? {query} : {})})); }
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
      const fields = ['type', 'protocol', 'connection_id', 'request_id', 'status', 'headers', 'body_base64', 'outcome'];
      const pending = this.pending.get(frame.request_id);
      const oauth = Object.hasOwn(frame, 'set_cookies');
      if (!exact(frame, oauth ? [...fields, 'set_cookies'] : fields) || (oauth && !config.public_oauth && !pending?.oauth)
          || (oauth && pending && !pending.oauth)
          || (!oauth && pending?.oauth && frame.outcome === 'responded')
          || frame.type !== 'response' || frame.protocol !== PROTOCOL || frame.connection_id !== attachment.connection
          || !identifier(frame.request_id) || !Number.isInteger(frame.status) || frame.status < 200 || frame.status > 599
          || !['responded', 'unknown', 'not_attempted'].includes(frame.outcome)) throw new Error('relay_frame_invalid');
      const attempt = this.one('SELECT member,connection,state,status,response_sha256 FROM attempts WHERE id=?', frame.request_id);
      if (!attempt || attempt.member !== attachment.member || attempt.connection !== attachment.connection) throw new Error('relay_frame_unowned');
      const raw = fromBase64(frame.body_base64, MAX_RESPONSE);
      const selected = headers(frame.headers, oauth ? OAUTH_RESPONSE_HEADERS : RESPONSE_HEADERS);
      const cookies = oauth ? responseCookies(frame.set_cookies) : [];
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
      if (pending) {
        clearTimeout(pending.timer); this.pending.delete(frame.request_id);
        const output = new Headers({...selected, 'x-amita-relay-request-id': frame.request_id, 'cache-control': 'no-store'});
        for (const cookie of cookies) output.append('set-cookie', cookie);
        pending.resolve(new Response(raw.length ? raw : null, {status: frame.status, headers: output}));
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
