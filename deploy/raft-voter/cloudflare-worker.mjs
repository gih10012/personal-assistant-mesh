// Wrangler binds this static import as a precompiled WebAssembly.Module.
// Never compile bytes or fetch a WASM URL inside a Worker.
import compiled from './artifact/wasm/amita_raft_portability_bg.wasm';
import { initSync, Voter } from './artifact/wasm/amita_raft_portability.js';
import { MEMBERS, need } from './codec.mjs';
import { VoterDriver, flusher } from './driver.mjs';
import { authenticatedForward, handle, transport } from './http.mjs';
import { peerMode, validateConfig } from './protocol.mjs';

function configuration(env) {
  // This is a fail-closed deployment assertion, not an account/billing API.
  // Root must independently verify the actual account remains Workers Free.
  need(env.DEPLOYMENT_PLAN === 'free' && typeof env.VOTER_CONFIG_JSON === 'string', 'verified_free_plan_and_private_binding_required');
  const config = validateConfig(JSON.parse(env.VOTER_CONFIG_JSON));
  need(MEMBERS.every(id => id === config.member || peerMode(config, id) !== 'poll'), 'hosted_voter_cannot_poll');
  return config;
}
class SQLStore {
  constructor(storage) {
    this.storage = storage;
    storage.sql.exec('CREATE TABLE IF NOT EXISTS raft_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)');
  }
  async read() {
    const rows = [...this.storage.sql.exec('SELECT payload FROM raft_state WHERE id=1')];
    return rows.length ? JSON.parse(rows[0].payload) : null;
  }
  async save(value) {
    this.storage.transactionSync(() => {
      const result = this.storage.sql.exec('INSERT INTO raft_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', JSON.stringify(value));
      // The 5s profile budgets a single INTEGER PRIMARY KEY row per save.
      // If platform accounting differs, retain the old state and stop the
      // candidate, rather than silently spending an assumed write allocation.
      need(result.rowsWritten === 1, `sql_rows_per_save_changed_${result.rowsWritten}`);
    });
    await this.storage.sync();
  }
}
export class RaftVoter {
  constructor(ctx, env) {
    this.ctx = ctx; this.env = env;
    this.ready = ctx.blockConcurrencyWhile(async () => {
      const config = configuration(env);
      initSync({ module: compiled });
      this.driver = new VoterDriver({ Voter, store: new SQLStore(ctx.storage), config });
      await this.driver.start(); this.flush = flusher(this.driver, transport(config));
    });
  }
  async schedule(accounted) {
    const status = await this.driver.inspect();
    if (!status.enabled || status.core_closed || status.hold) return;
    const alarm = await this.ctx.storage.getAlarm(), cutoff = this.driver.config.qualification_until_ms ?? Infinity;
    if (alarm === null || alarm > cutoff) {
      if (!accounted) await this.driver.reserveAlarm();
      await this.ctx.storage.setAlarm(Math.min(cutoff, Date.now() + this.driver.config.tick_ms));
    }
  }
  async fetch(request) {
    try {
      await this.ready;
      const result = await handle(this.driver, request);
      const status = await this.driver.inspect();
      if (!status.enabled && await this.ctx.storage.getAlarm() !== null) {
        await this.driver.reserveAlarm(); await this.ctx.storage.deleteAlarm();
      }
      else await this.schedule(false);
      this.ctx.waitUntil(this.flush());
      return Response.json(result, { headers: { 'cache-control': 'no-store' } });
    } catch (error) {
      return Response.json({ ok: false, error: String(error), business_fencing: false }, { status: 409 });
    }
  }
  async alarm() {
    await this.ready;
    await this.driver.tick({ alarmWrite: true });
    await this.flush();
    await this.schedule(true);
  }
}
export default {
  async fetch(request, env) {
    try {
      const config = configuration(env);
      const forwarded = await authenticatedForward(config, request);
      // Exactly one configured original voter; request data cannot select a new
      // class, arbitrary group/member, free namespace or management credential.
      const id = env.RAFT_VOTER.idFromName(`${config.group}/${config.scope}/${config.member}`);
      return env.RAFT_VOTER.get(id).fetch(forwarded);
    } catch (_) {
      return Response.json({ ok: false, error: 'voter_ingress_rejected' },
        { status: 403, headers: { 'cache-control': 'no-store' } });
    }
  }
};
