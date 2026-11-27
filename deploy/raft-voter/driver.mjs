import { bytes, entry, exact, hardState, isOwnerTransfer, MEMBERS, message, need, OWNER_ROLES, parseProposal, proposeWire, safe, withCommit } from './codec.mjs';
import { acknowledgement, envelope, exchangeRequest, peerMode, validAck, validateConfig, verify, verifyExchange } from './protocol.mjs';
import { applyOwner, canonical, contentHash, originalReceipt, ownerRequestError, receiptMatches, recordReceipt } from './owner-fsm.mjs';

const MAX_ENTRIES = 2048, MAX_LOG_BYTES = 1024 * 1024, MAX_OUTBOX = 256, MAX_STATE_BYTES = 4 * 1024 * 1024;
const DAY_ROWS = 90000, DAY_EVENTS = 90000, REPLAY_WINDOW = 64;
const clone = value => structuredClone(value);
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
function binding(config) { return { group: config.group, scope: config.scope, member: config.member, voters: [...MEMBERS] }; }
function fresh(config) {
  return { format: 'amita-raft-host-state/1', binding: binding(config), enabled: config.start_enabled,
    stable: { hard_state: [], entries: [], snapshot: [], applied: 0 }, receipts: {}, outbox: [],
    next: { 1: 0, 2: 0, 3: 0 }, received: { 1: [], 2: [], 3: [] }, pending: null, hold: null,
    last_tick: 0, leader_hint: null, quota: { day: '', rows: 0, events: 0 } };
}
function validateStable(stable) {
  need(exact(stable, ['hard_state', 'entries', 'snapshot', 'applied']) && Array.isArray(stable.entries) &&
    stable.entries.length <= MAX_ENTRIES && safe(stable.applied) === stable.applied, 'stable_shape');
  bytes(stable.hard_state); bytes(stable.snapshot);
  // This artifact cannot export/install our digest FSM snapshots or compact.
  // Preserve a received snapshot in a hold; never guess its application state.
  need(stable.snapshot.length === 0, 'snapshot_requires_qualified_port_export');
  const hs = hardState(stable.hard_state);
  need(hs.vote === 0 || MEMBERS.includes(hs.vote), 'static_vote_member');
  let previous = 0, size = 0, appliedOwner = false;
  for (const wire of stable.entries) {
    const record = entry(wire); size += wire.length;
    need(record.index === previous + 1 && record.term <= hs.term, 'contiguous_bound_log');
    previous = record.index;
    if (record.index <= stable.applied && isOwnerTransfer(record.proposal)) appliedOwner = true;
  }
  need(size <= MAX_LOG_BYTES && stable.applied <= hs.commit && hs.commit <= previous, 'stable_commit_applied_bounds');
  return appliedOwner;
}

async function applyProposal(state, record) {
  const proposal = record.proposal;
  if (!proposal) return;
  if (isOwnerTransfer(proposal)) return applyOwner(state, proposal, record);
  const previous = originalReceipt(state.receipts, proposal.operation_id);
  if (previous) {
    // The original typed request already owns this ID. A later opaque entry
    // can neither overwrite that receipt nor stop unrelated typed transfers.
    if (previous.format === undefined && previous.digest !== proposal.digest) return 'original_operation_digest_conflict';
  } else recordReceipt(state.receipts, proposal.operation_id, { digest: proposal.digest, index: record.index, term: record.term });
}

async function verifyOwnerRestore(state, appliedOwner) {
  if (!appliedOwner && OWNER_ROLES.every(role => state[role] == null) && !Object.values(state.receipts).some(r => r.format)) return;
  // The pinned host retains the entire bounded log. A missing FSM row or
  // receipt at an already-applied index must never be treated as virgin state.
  const replay = { binding: state.binding, receipts: {} };
  for (const wire of state.stable.entries.slice(0, state.stable.applied)) {
    const reason = await applyProposal(replay, entry(wire));
    need(!reason, reason);
  }
  need(OWNER_ROLES.every(role => canonical(replay[role] ?? null) === canonical(state[role] ?? null)) &&
    canonical(replay.receipts) === canonical(state.receipts), 'original_applied_owner_state_required');
}

// One pure ESM state machine shared by Node and SQLite DO. Store.save must be
// atomic AND durable. A failed save poisons this core; only reconstruction may
// continue. No network I/O occurs while this serialized mutation queue is held.
export class VoterDriver {
  constructor({ Voter, store, config, clock = Date.now }) {
    this.Voter = Voter; this.store = store; this.config = validateConfig(config); this.clock = clock;
    this.state = null; this.durable = null; this.core = null; this.tail = Promise.resolve(); this.dead = false;
  }
  serial(action) {
    const result = this.tail.then(() => { need(!this.dead, 'core_closed_reconstruct_required'); return action(); });
    this.tail = result.catch(() => {}); return result;
  }
  async start() {
    return this.serial(async () => {
      this.state = await this.store.read() ?? fresh(this.config);
      need(this.state.format === 'amita-raft-host-state/1' && same(this.state.binding, binding(this.config)), 'original_group_member_binding');
      const appliedOwner = validateStable(this.state.stable);
      need(Array.isArray(this.state.outbox) && this.state.outbox.length <= MAX_OUTBOX &&
        typeof this.state.enabled === 'boolean' && Object.keys(this.state.receipts).length <= MAX_ENTRIES,
      'bounded_original_state');
      need(!this.state.hold, `retained_hold_${this.state.hold}`);
      await verifyOwnerRestore(this.state, appliedOwner);
      this.durable = clone(this.state);
      this.core = new this.Voter(this.config.member, JSON.stringify(this.state.stable));
      if (await this.stopExpired()) return this.status();
      const pending = clone(this.state.pending);
      // RawNode can expose already-committed, not-yet-applied entries after a
      // crash. Restore/apply those first, then retry the original journaled input.
      await this.drain(null);
      if (pending) await this.process(pending);
      return this.status();
    });
  }
  quota() {
    const day = new Date(this.clock()).toISOString().slice(0, 10);
    if (this.state.quota.day !== day) this.state.quota = { day, rows: 0, events: 0 };
    need(this.state.quota.rows < DAY_ROWS - 8 && this.state.quota.events < DAY_EVENTS, 'daily_free_budget_hold');
  }
  event() { this.quota(); this.state.quota.events++; }
  async save() {
    this.quota(); this.state.quota.rows++;
    need(JSON.stringify(this.state).length <= MAX_STATE_BYTES, 'state_budget_hold');
    try {
      const image = clone(this.state);
      await this.store.save(image); this.durable = image;
    }
    catch (error) { this.closeCore(); throw error; }
  }
  closeCore() { this.core?.free(); this.core = null; this.dead = true; }
  expired() { return this.clock() >= (this.config.qualification_until_ms ?? Infinity); }
  async stopExpired() {
    if (!this.expired()) return false;
    if (this.state.enabled) { this.state.enabled = false; await this.save(); }
    return true;
  }
  async hold(reason) {
    this.state.hold = reason;
    try { await this.save(); } finally { this.closeCore(); }
    throw new Error(`raft_voter_retained_hold_${reason}`);
  }
  status() {
    const state = this.durable;
    need(state !== null, 'not_started');
    const hs = hardState(state.stable.hard_state);
    return { format: 'amita-raft-status/1', ...binding(this.config), enabled: state.enabled && !this.expired(),
      ...(this.config.qualification_until_ms === undefined ? {} : { qualification_until_ms: this.config.qualification_until_ms }),
      term: hs.term, persisted_commit: hs.commit, applied: state.stable.applied,
      leader_hint: state.leader_hint, leader_hint_is_authority: false,
      replica_writer: clone(state.replica_writer ?? null), oauth_writer: clone(state.oauth_writer ?? null),
      owner_read_consistency: 'local_durable_applied',
      receipts: clone(state.receipts), queued_messages: state.outbox.length,
      pending_original_input: state.pending !== null, hold: state.hold, quota: clone(state.quota),
      core_closed: this.dead, business_fencing: false, ha_qualified: false };
  }
  async inspect() {
    await this.tail;
    if (this.expired() && this.state.enabled && !this.dead) return this.serial(async () => {
      await this.stopExpired(); return this.status();
    });
    return this.status();
  }
  async reserveAlarm() { return this.serial(async () => { this.quota(); this.state.quota.rows++; await this.save(); }); }
  finish(event) {
    if (event?.kind === 'peer') {
      const list = this.state.received[event.sender];
      if (!list.some(row => row.sequence === event.sequence)) {
        list.push({ sequence: event.sequence, hash: event.hash });
        if (list.length > REPLAY_WINDOW) list.shift();
      }
    }
    if (event) this.state.pending = null;
  }
  async drain(event) {
    let batches = 0;
    for (; batches < 32; batches++) {
      const ready = JSON.parse(this.core.ready());
      if (ready === null) return batches;
      need(Number.isSafeInteger(ready.sequence) && ready.sequence > 0 && Array.isArray(ready.entries), 'ready_shape');
      if (event) this.state.pending = clone(event);
      const before = JSON.stringify(this.state.stable);
      if (ready.hard_state !== null) this.state.stable.hard_state = [...bytes(ready.hard_state)];
      if (ready.snapshot !== null && bytes(ready.snapshot).length) {
        // A nonempty snapshot is retained durably before any acknowledgement,
        // but cannot release network messages without a qualified FSM restore.
        this.state.stable.snapshot = [...bytes(ready.snapshot)];
        return this.hold('snapshot_requires_qualified_port_export');
      }
      if (ready.entries.length) {
        const records = ready.entries.map(wire => ({ wire: [...bytes(wire)], parsed: entry(wire) }));
        const start = records[0].parsed.index;
        need(start > this.state.stable.applied, 'append_cannot_overwrite_applied');
        this.state.stable.entries = this.state.stable.entries.filter(wire => entry(wire).index < start)
          .concat(records.map(record => record.wire));
      }
      // Save actual HardState/log/snapshot before acknowledging Ready. Empty
      // Ready storage batches need no extra SQL write; messages still wait below.
      validateStable(this.state.stable);
      if (JSON.stringify(this.state.stable) !== before) await this.save();
      const applied = JSON.parse(this.core.persisted(BigInt(ready.sequence)));
      need(Array.isArray(applied.committed) && Array.isArray(applied.messages), 'applied_shape');
      if (applied.commit_index !== null) {
        const index = safe(applied.commit_index);
        this.state.stable.hard_state = withCommit(this.state.stable.hard_state, index);
      }
      let last = null;
      for (const wire of applied.committed) {
        const record = entry(wire);
        need(record.index === this.state.stable.applied + 1 &&
          same([...bytes(wire)], this.state.stable.entries[record.index - 1]), 'original_committed_entry');
        const reason = await applyProposal(this.state, record);
        if (reason) return this.hold(reason);
        // Empty leader/no-op entries advance applied too, atomically with FSM.
        this.state.stable.applied = record.index; last = record.index;
      }
      for (const wire of applied.messages) {
        const header = message(wire, { remote: true });
        need(header.from === this.config.member, 'original_outgoing_sender');
        const sequence = ++this.state.next[header.to]; safe(sequence);
        this.state.outbox.push(await envelope(this.config, sequence, bytes(wire)));
        if ([3, 8].includes(header.type)) this.state.leader_hint = this.config.member;
      }
      if (this.state.outbox.length > MAX_OUTBOX) return this.hold('durable_outbox_budget');
      this.finish(event); validateStable(this.state.stable);
      // Atomic durable state includes the complete outbox and applied receipts.
      // Only after this succeeds can a sender observe an ACK or flush messages.
      await this.save();
      if (last !== null) this.core.applied(BigInt(last));
    }
    return this.hold('ready_batch_budget');
  }
  async process(event) {
    const before = JSON.stringify(this.state);
    try {
      if (event.kind === 'tick') this.core.tick();
      else this.core.step(bytes(event.wire));
      const count = await this.drain(event);
      this.finish(event);
      if (!count || JSON.stringify(this.state) !== before && this.state.pending !== null) await this.save();
    } catch (error) {
      // A Ready/persist/apply boundary must never continue on a partially
      // advanced volatile RawNode. Stored pending input remains for recovery.
      this.closeCore(); throw error;
    }
  }
  // The caller holds serial() and has verified both signatures. Fold a matching
  // ACK deletion into the same durable save as receive (or the idle event).
  async deliver(from, packet, verified, ack) {
    need(this.state.enabled && !this.expired(), 'voter_disabled');
    const list = this.state.received[from], last = list.at(-1)?.sequence ?? 0;
    const head = this.state.outbox.find(row => row.to === from);
    if (ack) need(ack.sequence <= (head?.sequence ?? this.state.next[from]), 'ack_beyond_original_head');
    if (packet && packet.sequence <= last) {
      need(list.some(row => row.sequence === packet.sequence && row.hash === verified.hash), 'replay_content_or_window');
    } else if (packet) need(packet.sequence === last + 1, 'ordered_peer_sequence_required');
    this.event();
    if (ack && head?.sequence === ack.sequence) this.state.outbox.splice(this.state.outbox.indexOf(head), 1);
    if (packet && packet.sequence > last) {
      if ([3, 8].includes(verified.header.type)) this.state.leader_hint = from;
      await this.process({ kind: 'peer', sender: from, sequence: packet.sequence,
        hash: verified.hash, wire: [...verified.wire] });
    } else await this.save();
    return packet ? acknowledgement(this.config, packet) : null;
  }
  async receive(packet) {
    const verified = await verify(this.config, packet);
    return this.serial(() => this.deliver(packet.from, packet, verified, null));
  }
  async exchange(request) {
    const verified = await verifyExchange(this.config, request, this.clock());
    return this.serial(async () => {
      need(request.expires_ms > this.clock(), 'exchange_expiry');
      const ack = await this.deliver(request.from, request.packet, verified, request.ack);
      // The signed request can retrieve only this exact successor, even if an
      // old poll is replayed after another request has advanced the outbox.
      const sequence = (request.ack?.sequence ?? 0) + 1;
      const packet = this.state.outbox.find(row => row.to === request.from && row.sequence === sequence);
      return { format: 'amita-raft-exchange-result/1', ack, packet: packet ? clone(packet) : null };
    });
  }
  async prepareExchange(to) {
    return this.serial(async () => {
      need(peerMode(this.config, to) === 'poll', 'exchange_poll_binding');
      if (!this.state.enabled || this.expired()) return null;
      const packet = this.state.outbox.find(row => row.to === to) ?? null;
      const received = this.state.received[to].at(-1);
      const ack = received ? await acknowledgement(this.config, { from: to, sequence: received.sequence }) : null;
      return exchangeRequest(this.config, to, clone(packet), ack, this.clock());
    });
  }
  async finishExchange(request, result) {
    need(exact(result, ['format', 'packet', 'ack']) && result.format === 'amita-raft-exchange-result/1', 'exchange_result');
    if (request.packet) need(await validAck(this.config, request.packet, result.ack), 'authenticated_original_ack');
    else need(result.ack === null, 'unsolicited_ack');
    if (result.packet === null && result.ack === null) return;
    let verified = null;
    if (result.packet !== null) {
      need(result.packet?.from === request.to && result.packet.sequence === (request.ack?.sequence ?? 0) + 1,
        'exchange_result_binding');
      verified = await verify(this.config, result.packet);
    }
    return this.serial(() => this.deliver(request.to, result.packet, verified, result.ack));
  }
  async tick({ alarmWrite = false } = {}) {
    return this.serial(async () => {
      if (await this.stopExpired() || !this.state.enabled) return this.status();
      this.event();
      if (alarmWrite) this.state.quota.rows++; // setAlarm itself is a billed row.
      const now = this.clock();
      if (now - this.state.last_tick >= this.config.tick_ms) {
        this.state.last_tick = now; await this.process({ kind: 'tick' });
      } else await this.save(); // at-least-once alarm retry never accelerates ticks.
      return this.status();
    });
  }
  async propose(value) {
    const proposal = parseProposal(clone(value));
    return this.serial(async () => {
      need(this.state.enabled && !this.expired(), 'voter_disabled'); this.event();
      let requestHash = null;
      if (isOwnerTransfer(proposal)) {
        const reason = await ownerRequestError(proposal, this.state.binding);
        need(reason === null, reason);
        requestHash = await contentHash(proposal);
      }
      const old = originalReceipt(this.state.receipts, proposal.operation_id);
      if (old) { need(receiptMatches(old, proposal, requestHash), 'original_operation_digest_conflict'); return clone(old); }
      need(this.state.stable.entries.length < MAX_ENTRIES - 32, 'log_capacity_hold_no_compaction');
      await this.process({ kind: 'proposal', wire: [...proposeWire(this.config.member, proposal)] });
      return { status: 'submitted_not_commit_receipt', operation_id: proposal.operation_id };
    });
  }
  async enable(value) {
    return this.serial(async () => {
      need(typeof value === 'boolean', 'enabled_boolean'); need(!value || !this.expired(), 'qualification_expired');
      this.state.enabled = value; await this.save(); return this.status();
    });
  }
  async outbound() { return this.serial(() => this.state.enabled && !this.expired() ? clone(this.state.outbox) : []); }
  async acknowledge(pairs) {
    return this.serial(async () => {
      if (this.expired()) return;
      const remove = [];
      for (const { outgoing, ack } of pairs) {
        need(await validAck(this.config, outgoing, ack), 'authenticated_original_ack');
        const found = this.state.outbox.find(row => row.to === outgoing.to && row.sequence === outgoing.sequence);
        if (found) { need(same(found, outgoing), 'original_outbox_binding'); remove.push(found); }
      }
      if (remove.length) { this.state.outbox = this.state.outbox.filter(row => !remove.includes(row)); await this.save(); }
    });
  }
  async close() { await this.tail; this.closeCore(); await this.store.close?.(); }
}

// The transport runs outside the driver lock, so a response can be accepted
// while our own outbox is in flight. ACK deletion is batched into one write.
export function flusher(driver, send) {
  const peers = MEMBERS.filter(id => id !== driver.config.member && peerMode(driver.config, id) === 'push');
  let running = null;
  return () => {
    if (!peers.length) return Promise.resolve(0);
    if (running) return running;
    running = (async () => {
      const queue = await driver.outbound(), pairs = [];
      for (const member of peers) {
        const row = queue.find(value => value.to === member); if (!row) continue;
        try { pairs.push({ outgoing: row, ack: await send(row) }); } catch { /* durable original outbox retries later */ }
      }
      if (pairs.length) await driver.acknowledge(pairs);
      return pairs.length;
    })().finally(() => { running = null; });
    return running;
  };
}

// Node calls this once per timer tick. HTTP requests, startup and Raft Ready
// callbacks never trigger an extra poll. A lost response retains both heads.
export async function pollExchanges(driver, send) {
  for (const member of MEMBERS.filter(id => id !== driver.config.member && peerMode(driver.config, id) === 'poll')) {
    const request = await driver.prepareExchange(member);
    if (request) {
      let result;
      try { result = await send(request); } catch { continue; }
      await driver.finishExchange(request, result);
    }
  }
}
