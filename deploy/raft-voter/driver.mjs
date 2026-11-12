import { bytes, digestProposal, entry, exact, hardState, MEMBERS, message, need, proposeWire, safe, withCommit } from './codec.mjs';
import { acknowledgement, envelope, validAck, validateConfig, verify } from './protocol.mjs';

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
  let previous = 0, size = 0;
  for (const wire of stable.entries) {
    const record = entry(wire); size += wire.length;
    need(record.index === previous + 1 && record.term <= hs.term, 'contiguous_bound_log');
    previous = record.index;
  }
  need(size <= MAX_LOG_BYTES && stable.applied <= hs.commit && hs.commit <= previous, 'stable_commit_applied_bounds');
}

// One pure ESM state machine shared by Node and SQLite DO. Store.save must be
// atomic AND durable. A failed save poisons this core; only reconstruction may
// continue. No network I/O occurs while this serialized mutation queue is held.
export class VoterDriver {
  constructor({ Voter, store, config, clock = Date.now }) {
    this.Voter = Voter; this.store = store; this.config = validateConfig(config); this.clock = clock;
    this.state = null; this.core = null; this.tail = Promise.resolve(); this.dead = false;
  }
  serial(action) {
    const result = this.tail.then(() => { need(!this.dead, 'core_closed_reconstruct_required'); return action(); });
    this.tail = result.catch(() => {}); return result;
  }
  async start() {
    return this.serial(async () => {
      this.state = await this.store.read() ?? fresh(this.config);
      need(this.state.format === 'amita-raft-host-state/1' && same(this.state.binding, binding(this.config)), 'original_group_member_binding');
      validateStable(this.state.stable);
      need(Array.isArray(this.state.outbox) && this.state.outbox.length <= MAX_OUTBOX &&
        typeof this.state.enabled === 'boolean' && Object.keys(this.state.receipts).length <= MAX_ENTRIES,
      'bounded_original_state');
      need(!this.state.hold, `retained_hold_${this.state.hold}`);
      this.core = new this.Voter(this.config.member, JSON.stringify(this.state.stable));
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
    try { await this.store.save(clone(this.state)); }
    catch (error) { this.closeCore(); throw error; }
  }
  closeCore() { this.core?.free(); this.core = null; this.dead = true; }
  async hold(reason) {
    this.state.hold = reason;
    try { await this.save(); } finally { this.closeCore(); }
    throw new Error(`raft_voter_retained_hold_${reason}`);
  }
  status() {
    need(this.state !== null, 'not_started');
    const hs = hardState(this.state.stable.hard_state);
    return { format: 'amita-raft-status/1', ...binding(this.config), enabled: this.state.enabled,
      term: hs.term, persisted_commit: hs.commit, applied: this.state.stable.applied,
      leader_hint: this.state.leader_hint, leader_hint_is_authority: false,
      receipts: clone(this.state.receipts), queued_messages: this.state.outbox.length,
      pending_original_input: this.state.pending !== null, hold: this.state.hold, quota: clone(this.state.quota),
      core_closed: this.dead, business_fencing: false, ha_qualified: false };
  }
  async inspect() { await this.tail; return this.status(); }
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
        if (record.data.length) {
          const proposal = digestProposal(JSON.parse(new TextDecoder().decode(record.data)));
          const previous = this.state.receipts[proposal.operation_id];
          if (previous && previous.digest !== proposal.digest) return this.hold('original_operation_digest_conflict');
          if (!previous) this.state.receipts[proposal.operation_id] = {
            digest: proposal.digest, index: record.index, term: record.term
          };
        }
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
  async receive(value) {
    const verified = await verify(this.config, value);
    return this.serial(async () => {
      need(this.state.enabled, 'voter_disabled'); this.event();
      const list = this.state.received[value.from], last = list.at(-1)?.sequence ?? 0;
      if (value.sequence <= last) {
        need(list.some(row => row.sequence === value.sequence && row.hash === verified.hash), 'replay_content_or_window');
        await this.save(); return acknowledgement(this.config, value);
      }
      need(value.sequence === last + 1, 'ordered_peer_sequence_required');
      if ([3, 8].includes(verified.header.type)) this.state.leader_hint = value.from;
      await this.process({ kind: 'peer', sender: value.from, sequence: value.sequence,
        hash: verified.hash, wire: [...verified.wire] });
      return acknowledgement(this.config, value);
    });
  }
  async tick({ alarmWrite = false } = {}) {
    return this.serial(async () => {
      if (!this.state.enabled) return this.status();
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
    const proposal = digestProposal(value);
    return this.serial(async () => {
      need(this.state.enabled, 'voter_disabled'); this.event();
      const old = this.state.receipts[proposal.operation_id];
      if (old) { need(old.digest === proposal.digest, 'original_operation_digest_conflict'); return clone(old); }
      need(this.state.stable.entries.length < MAX_ENTRIES - 32, 'log_capacity_hold_no_compaction');
      await this.process({ kind: 'proposal', wire: [...proposeWire(this.config.member, proposal)] });
      return { status: 'submitted_not_commit_receipt', operation_id: proposal.operation_id };
    });
  }
  async enable(value) {
    return this.serial(async () => {
      need(typeof value === 'boolean', 'enabled_boolean'); this.state.enabled = value; await this.save(); return this.status();
    });
  }
  async outbound() { return this.serial(() => this.state.enabled ? clone(this.state.outbox) : []); }
  async acknowledge(pairs) {
    return this.serial(async () => {
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
  let running = null;
  return () => {
    if (running) return running;
    running = (async () => {
      const queue = await driver.outbound(), pairs = [];
      for (const member of MEMBERS.filter(id => id !== driver.config.member)) {
        const row = queue.find(value => value.to === member); if (!row) continue;
        try { pairs.push({ outgoing: row, ack: await send(row) }); } catch { /* durable original outbox retries later */ }
      }
      if (pairs.length) await driver.acknowledge(pairs);
      return pairs.length;
    })().finally(() => { running = null; });
    return running;
  };
}
