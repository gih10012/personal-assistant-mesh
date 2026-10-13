/* Separate owned Firefox collaborator. The original manual bridge is intact. */
(function (root) {
  "use strict";
  const dom = typeof module !== "undefined" ? require("./chat_bridge_dom.js") : root.MeshChatDOM;
  const HOST = "personal_assistant_mesh_chat", KEY = "mesh_chat_collaborator_v1", TAB_KEY = "mesh_chat_owned_v1";
  const object = (value) => value && typeof value === "object" && !Array.isArray(value);
  const fail = (reason) => { throw new Error(reason); };
  class ChatCollaborator {
    constructor(api, crypto, timers = root) {
      this.api = api; this.crypto = crypto; this.timers = timers;
      this.port = null; this.owner = null; this.pending = new Map(); this.active = new Map();
      this.serial = Promise.resolve(); this.sequence = 0; this.backoff = 1000;
    }
    async storage() {
      const saved = (await this.api.storage.local.get(KEY))[KEY];
      if (saved === undefined) return { schema: 1, owner_id: this.owner, projects: {}, requests: {} };
      if (!object(saved) || saved.schema !== 1 || saved.owner_id !== this.owner
          || !object(saved.projects) || !object(saved.requests)) fail("owner_storage_conflict");
      return saved;
    }
    async change(mutator) {
      const run = async () => { const state = await this.storage(); const result = await mutator(state);
        await this.api.storage.local.set({ [KEY]: state }); return result; };
      const result = this.serial.then(run, run); this.serial = result.catch(() => undefined); return result;
    }
    async ownedTab(job) {
      let found;
      await this.change(async (state) => {
        let project = state.projects[job.project_id];
        if (project && project.url !== job.conversation_url) fail("project_conversation_conflict");
        if (project) {
          try {
            const tab = await this.api.tabs.get(project.tab_id);
            const marker = await this.api.sessions.getTabValue(tab.id, TAB_KEY);
            if (tab.url === project.url && object(marker) && marker.owner_id === this.owner
                && marker.project_id === job.project_id && marker.nonce === project.nonce) found = tab;
          } catch (_) { /* never adopt an ordinary tab by matching its URL */ }
        }
        if (!found) {
          const nonce = Array.from(this.crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
          const tab = await this.api.tabs.create({ url: job.conversation_url, active: false });
          await this.api.sessions.setTabValue(tab.id, TAB_KEY, { owner_id: this.owner, project_id: job.project_id, nonce });
          project = { url: job.conversation_url, tab_id: tab.id, nonce };
          state.projects[job.project_id] = project; found = tab;
        }
      });
      if (found.status === "loading") {
        await new Promise((resolve, reject) => {
          const done = (id, change, tab) => {
            if (id !== found.id || change.status !== "complete") return;
            this.api.tabs.onUpdated.removeListener(done); this.timers.clearTimeout(timer);
            if (tab.url !== job.conversation_url) reject(new Error("conversation_changed")); else resolve();
          };
          const timer = this.timers.setTimeout(() => { this.api.tabs.onUpdated.removeListener(done);
            reject(new Error("owned_tab_not_ready")); }, 30000);
          this.api.tabs.onUpdated.addListener(done);
          // The tab could have finished between create/get and listener setup.
          this.api.tabs.get(found.id).then((tab) => {
            if (tab.status === "complete") done(tab.id, { status: "complete" }, tab);
          }).catch(() => undefined);
        });
      }
      return found;
    }
    async content(tab, action, job) {
      // A fixed local file, never model-supplied JavaScript or page eval.
      await this.api.tabs.executeScript(tab.id, { file: "chat_bridge_dom.js", frameId: 0 });
      return this.api.tabs.sendMessage(tab.id, { schema: 1, action, job }, { frameId: 0 });
    }
    rpc(action, job, observation) {
      const port = this.port;
      if (!port) return Promise.reject(new Error("native_port_unavailable"));
      const call = `chat-call-${++this.sequence}`;
      const message = { schema: 1, action, call_id: call, request_id: job.request_id, content_sha256: job.content_sha256 };
      if (observation !== undefined) message.observation = observation;
      return new Promise((resolve, reject) => {
        const timer = this.timers.setTimeout(() => { this.pending.delete(call); reject(new Error("native_reply_unknown")); }, 15000);
        this.pending.set(call, { resolve, reject, timer, job });
        try { port.postMessage(message); } catch (_) {
          this.timers.clearTimeout(timer); this.pending.delete(call); reject(new Error("native_reply_unknown"));
        }
      });
    }
    async report(job, value) {
      if (!object(value) || value.conversation_url !== job.conversation_url || value.account_sha256 !== job.account_sha256) fail("observation_binding_conflict");
      await this.change((state) => {
        const prior = state.requests[job.request_id];
        if (!prior || !dom.equalBinding(prior.binding, job)) fail("original_request_conflict");
        if (prior.observation && prior.observation.state === "completed"
            && JSON.stringify(prior.observation) !== JSON.stringify(value)) fail("completed_result_changed");
        const current = dom.observedIdentity(value), remembered = prior.observed_identity || dom.observedIdentity(prior.observation);
        if (current && dom.identityConflict(remembered, current)) {
          value = { ...value, state: "unknown", reason: "message_identity_conflict", text: "", terminal_visible: false };
        }
        if (remembered) prior.observed_identity = remembered;
        else if (current) prior.observed_identity = current;
        if (current && !dom.identityConflict(remembered, current) && remembered && !remembered.assistant_message_id && current.assistant_message_id) {
          prior.observed_identity = { ...remembered, assistant_message_id: current.assistant_message_id };
        }
        prior.observation = value;
      });
      return this.rpc("report", job, value);
    }
    async work(job) {
      dom.validateJob(job);
      if (!this.owner || !["prepare", "observe"].includes(job.mode)) fail("native_job_invalid");
      if (this.active.has(job.request_id)) return;
      this.active.set(job.request_id, true);
      let retained = false, tab = null, observationOnly = job.mode === "observe";
      try {
        const original = await this.change((state) => {
          const prior = state.requests[job.request_id];
          if (prior && !dom.equalBinding(prior.binding, job)) fail("original_request_conflict");
          if (!prior) state.requests[job.request_id] = { binding: dom.binding(job), send_intent: false, observation: null };
          return JSON.parse(JSON.stringify(state.requests[job.request_id]));
        });
        if (original.observation && (original.observation.conversation_url !== job.conversation_url
            || original.observation.account_sha256 !== job.account_sha256)) fail("observation_binding_conflict");
        const localIdentity = original.observed_identity || dom.observedIdentity(original.observation);
        if (localIdentity) dom.validateObservedIdentity(localIdentity);
        if (dom.identityConflict(localIdentity, job.observed_identity)) {
          // Neither a lost host ACK nor a reload may rewrite either first
          // checkpoint or grant another send. Report conflict, then observe.
          await this.report(job, dom.observation(job, "unknown", "identity_checkpoint_conflict"));
          observationOnly = true;
        }
        let identity = localIdentity || job.observed_identity;
        if (identity && !identity.assistant_message_id && job.observed_identity && !dom.identityConflict(identity, job.observed_identity)) {
          identity = { ...identity, assistant_message_id: job.observed_identity.assistant_message_id };
        }
        if (identity) { job = { ...job, observed_identity: identity }; observationOnly = true; }
        if (original.observation && original.observation.state === "completed") {
          await this.rpc("report", job, original.observation); return;
        }
        observationOnly = observationOnly || original.send_intent;
        tab = await this.ownedTab(job);
        if (!observationOnly && !original.send_intent && job.mode === "prepare") {
          const ready = await this.content(tab, "preflight", job);
          if (!object(ready) || ready.ready !== true) {
            if (!object(ready) || !object(ready.observation)) fail("preflight_unavailable");
            const value = ready.observation;
            if (value.state === "not_sent") value.state = "blocked";
            await this.report(job, value);
            if (["partial", "unknown"].includes(value.state)) {
              await this.content(tab, "watch", job); retained = true;
            }
            return;
          }
          // Durable BEFORE begin-send RPC and before any editor modification.
          await this.change((state) => { state.requests[job.request_id].send_intent = true; });
          observationOnly = true;
          const grant = await this.rpc("begin_send", job);
          if (grant.send_granted === true) {
            const value = await this.content(tab, "send", job);
            if (!object(value)) fail("send_outcome_unknown");
            // A failed post-grant preflight cannot authorize a fresh attempt.
            if (value.state === "blocked" || value.state === "not_sent") value.state = "unknown";
            await this.report(job, value);
          }
        } else if (original.send_intent && job.mode === "prepare") {
          // Native grant ACK may have vanished before its durable counterpart:
          // never request another grant; converge the original identity only.
          await this.report(job, dom.observation(job, "unknown", "local_send_intent_exists"));
        }
        await this.content(tab, "watch", job); retained = true;
      } catch (_) {
        // Never fill/reclick on failure. Host grant, local intent and original
        // ID survive a lost transport; a reconnect asks for observation only.
        if (observationOnly && tab) {
          try { await this.content(tab, "watch", job); retained = true; } catch (_) { /* original durable unknown remains */ }
        }
      } finally {
        if (!retained) this.active.delete(job.request_id);
      }
    }
    async receiveObservation(message, sender) {
      if (!this.owner || !object(message) || Object.keys(message).sort().join() !== "action,binding,observation,schema"
          || message.schema !== 1 || message.action !== "observation" || !sender || sender.id !== dom.EXTENSION_ID
          || !sender.tab || sender.frameId !== 0) fail("observation_caller_not_allowed");
      const state = await this.storage(), saved = state.requests[message.binding && message.binding.request_id];
      if (!saved || !dom.equalBinding(saved.binding, message.binding)) fail("observation_binding_conflict");
      const project = state.projects[saved.binding.project_id];
      if (!project || project.tab_id !== sender.tab.id || sender.url !== saved.binding.conversation_url) fail("observation_tab_not_owned");
      const marker = await this.api.sessions.getTabValue(sender.tab.id, TAB_KEY);
      if (!object(marker) || marker.nonce !== project.nonce || marker.owner_id !== this.owner
          || marker.project_id !== saved.binding.project_id) fail("observation_tab_not_owned");
      const value = message.observation;
      if (!object(value) || value.conversation_url !== saved.binding.conversation_url || value.account_sha256 !== saved.binding.account_sha256
          || !["completed", "partial", "unknown"].includes(value.state)) fail("observation_invalid");
      await this.report(saved.binding, value);
      if (value.state === "completed" || value.state === "unknown") this.active.delete(saved.binding.request_id);
      return { accepted: true, retry_with_new_id: false };
    }
    async nativeMessage(message) {
      if (!object(message) || message.schema !== 1) fail("native_message_invalid");
      if (message.event === "hello") {
        if (message.format !== "mesh-chat-browser/1" || typeof message.owner_id !== "string" || !message.owner_id) fail("native_owner_invalid");
        if (this.owner && this.owner !== message.owner_id) fail("native_owner_changed");
        this.owner = message.owner_id; await this.change(() => undefined); this.backoff = 1000;
      } else if (message.event === "reply") {
        const pending = this.pending.get(message.call_id);
        const fields = ["format", "request_id", "task_id", "project_id", "content_sha256", "state", "send_intent",
          "result_available", "retry_with_new_id", "execution_verified", "account_verified", "native_tools_intercepted"];
        if (!pending || !object(message.data) || Object.keys(message.data).some((key) => !fields.includes(key) && key !== "send_granted")
          || message.data.format !== "mesh-chat-browser/1"
          || ["request_id", "task_id", "project_id", "content_sha256"].some((key) => pending.job[key] !== message.data[key])
          || message.data.retry_with_new_id !== false || message.data.execution_verified !== false
          || message.data.account_verified !== false || message.data.native_tools_intercepted !== false
          || typeof message.data.send_intent !== "boolean" || typeof message.data.result_available !== "boolean"
          || !["queued", "unknown", "partial", "blocked", "completed"].includes(message.data.state)
          || ("send_granted" in message.data && typeof message.data.send_granted !== "boolean")) fail("native_reply_invalid");
        this.pending.delete(message.call_id); this.timers.clearTimeout(pending.timer); pending.resolve(message.data);
      } else if (message.event === "work") {
        this.work(message.job).catch(() => undefined);
      } else fail("native_message_invalid");
    }
    connect() {
      if (this.port) return;
      const port = this.api.runtime.connectNative(HOST); this.port = port;
      port.onMessage.addListener((message) => { this.nativeMessage(message).catch(() => { port.disconnect(); }); });
      port.onDisconnect.addListener(() => {
        if (this.port !== port) return;
        this.port = null; this.active.clear();
        for (const pending of this.pending.values()) { this.timers.clearTimeout(pending.timer); pending.reject(new Error("native_reply_unknown")); }
        this.pending.clear();
        const delay = this.backoff; this.backoff = Math.min(this.backoff * 2, 60000);
        this.timers.setTimeout(() => this.connect(), delay); // only reconnect, no DOM replay
      });
    }
  }
  const exported = { ChatCollaborator, HOST, KEY, TAB_KEY };
  if (typeof module !== "undefined") module.exports = exported;
  else {
    const bridge = new ChatCollaborator(browser, crypto);
    browser.runtime.onMessage.addListener((message, sender) => bridge.receiveObservation(message, sender));
    bridge.connect();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
