/* Ordinary, visible Firefox DOM only. No fetch, cookies, auth, page-private API,
 * stealth, model switching, foreground activation or creation of a new chat.
 * Selectors must be reviewed against the owner's actual UI before installation.
 * This implementation is original; no third-party gateway code is vendored.
 */
(function (root) {
  "use strict";
  const EXTENSION_ID = "personal-assistant-mesh-chat@gih10012.local";
  const MAX_RESULT = 524288;
  const size = (text) => new TextEncoder().encode(text).length;
  const object = (value) => value && typeof value === "object" && !Array.isArray(value);
  const fail = (reason) => { throw new Error(reason); };
  function conversation(url) {
    const parsed = new URL(url);
    return parsed.origin === "https://chatgpt.com" && !parsed.username && !parsed.password
      && !parsed.search && !parsed.hash && /^(?:\/g\/[A-Za-z0-9_-]+)?\/c\/[A-Za-z0-9-]{10,}$/.test(parsed.pathname);
  }
  function validateJob(job) {
    if (!object(job) || !["request_id", "task_id", "project_id"].every((key) =>
      typeof job[key] === "string" && job[key].length > 0 && size(job[key]) <= 200)
      || typeof job.prompt !== "string" || size(job.prompt) > 20000
      || !/^[0-9a-f]{64}$/.test(job.content_sha256) || !/^[0-9a-f]{64}$/.test(job.account_sha256)
      || !conversation(job.conversation_url) || !object(job.dom)
      || !job.prompt.startsWith(`Mesh request: ${job.request_id}\nMesh content SHA-256: ${job.content_sha256}\n\n`)) fail("job_invalid");
    for (const key of ["account_selector", "account_attribute", "completion_selector", "composer_selector", "send_selector",
      "generating_selector", "message_selector", "role_attribute", "message_id_attribute"]) {
      if (typeof job.dom[key] !== "string" || !job.dom[key] || job.dom[key].length > 256) fail("job_invalid");
    }
    return job;
  }
  function binding(job) {
    return { request_id: job.request_id, task_id: job.task_id, project_id: job.project_id,
      content_sha256: job.content_sha256, conversation_url: job.conversation_url, account_sha256: job.account_sha256 };
  }
  function equalBinding(left, right) {
    return object(left) && object(right) && Object.keys(binding(left)).every((key) => left[key] === right[key]);
  }
  function text(element) {
    // Do not trim or concatenate a subset of Markdown blocks. These are the
    // complete rendered bytes of the selected message, not hidden reasoning.
    return typeof element.innerText === "string" ? element.innerText : null;
  }
  function visible(element) {
    return !!element && element.getAttribute("aria-hidden") !== "true"
      && element.getClientRects().length > 0;
  }
  function validateAccountMenu(value) {
    if (value === undefined || value === null) return null;
    const pairs = new Set(["aria-checked:true", "aria-selected:true", "aria-current:true", "aria-current:page",
      "data-selected:true", "data-state:checked", "data-state:selected", "data-state:active"]);
    if (!object(value) || Object.keys(value).sort().join() !== "current_selector,selected_attribute,selected_value,steps"
      || !Array.isArray(value.steps) || value.steps.length < 1 || value.steps.length > 3
      || !pairs.has(`${value.selected_attribute}:${value.selected_value}`)
      || ![value.current_selector, value.selected_attribute, value.selected_value].every((item) =>
        typeof item === "string" && item.length > 0 && item.length <= 256 && !/[\u0000-\u001f\u007f]/.test(item))
      || value.steps.some((step) => !object(step) || Object.keys(step).sort().join() !== "control_selector,menu_role,menu_selector"
        || !["menu", "listbox", "dialog"].includes(step.menu_role)
        || ![step.control_selector, step.menu_selector].every((item) => typeof item === "string" && item.length > 0 && item.length <= 256))) {
      fail("selected_account_contract_invalid");
    }
    return value;
  }
  function observation(job, state, reason, body = "", user = "", assistant = "", terminal = false) {
    return { state, reason, text: body, conversation_url: job.conversation_url,
      account_sha256: job.account_sha256, user_message_id: user, assistant_message_id: assistant,
      terminal_visible: terminal, scope: "visible_dom_text" };
  }
  class ChatBridgeDOM {
    constructor(document, location, crypto, events = {}, timers = root) {
      this.document = document; this.location = location; this.crypto = crypto;
      this.events = events; this.timers = timers; this.clicked = new Set(); this.watchers = new Map();
      this.accountReads = Promise.resolve();
    }
    account(job) {
      // Serialize read-only UI openings in this owned tab. No cached account
      // assertion or arbitrary page-email fallback on later turns.
      const run = () => this.readSelectedAccount(job);
      const result = this.accountReads.then(run, run);
      this.accountReads = result.catch(() => undefined); return result;
    }
    async waitFor(predicate) {
      const immediate = predicate(); if (immediate) return immediate;
      if (!this.events.MutationObserver) return null;
      return new Promise((resolve) => {
        const observer = new this.events.MutationObserver(() => {
          const value = predicate(); if (value) { observer.disconnect(); this.timers.clearTimeout(timer); resolve(value); }
        });
        const timer = this.timers.setTimeout(() => { observer.disconnect(); resolve(null); }, 1500);
        observer.observe(this.document.documentElement, { subtree: true, childList: true, attributes: true });
      });
    }
    outsideConversation(node, job) {
      return node && !node.closest(job.dom.message_selector) && !node.closest(job.dom.composer_selector);
    }
    async readSelectedAccount(job) {
      if (this.location.href !== job.conversation_url) return "conversation_changed";
      let config;
      try { config = validateAccountMenu(job.dom.account_menu); } catch (_) { return "selected_account_contract_invalid"; }
      if (!config) return "selected_account_contract_not_configured";
      let rootControl = null, answer = "selected_account_unavailable";
      const openedMenus = [];
      try {
        let menu;
        for (const step of config.steps) {
          // First control in normal UI, later controls only INSIDE the verified
          // parent menu. Never click an account row, chat or arbitrary link.
          const scope = menu || this.document, controls = scope.querySelectorAll(step.control_selector);
          if (controls.length !== 1 || !visible(controls[0]) || !this.outsideConversation(controls[0], job)) return "account_control_unavailable";
          const control = controls[0], role = control.getAttribute("role");
          if ((control.tagName !== "BUTTON" && !["button", "menuitem"].includes(role))
            || control.getAttribute("aria-haspopup") !== step.menu_role || control.getAttribute("aria-expanded") !== "false") return "account_menu_not_read_only_control";
          if (control.disabled || control.getAttribute("aria-disabled") === "true") return "account_control_unavailable";
          if (!rootControl) rootControl = control;
          control.click(); // explicitly configured normal menu/submenu only
          menu = await this.waitFor(() => {
            const menus = this.document.querySelectorAll(step.menu_selector);
            if (menus.length !== 1 || !visible(menus[0]) || !this.outsideConversation(menus[0], job)) return null;
            const id = menus[0].getAttribute("id");
            return id && menus[0].getAttribute("role") === step.menu_role
              && control.getAttribute("aria-expanded") === "true"
              && control.getAttribute("aria-controls") === id ? menus[0] : null;
          });
          if (!menu) return "account_menu_relation_unverified";
          openedMenus.push(menu);
          if (this.location.href !== job.conversation_url) return "conversation_changed";
        }
        const selected = menu.querySelectorAll(config.current_selector);
        if (selected.length !== 1 || !visible(selected[0]) || !this.outsideConversation(selected[0], job)
          || selected[0].getAttribute(config.selected_attribute) !== config.selected_value) return "current_account_selection_unverified";
        const current = selected[0], role = current.getAttribute("role");
        if (config.selected_attribute === "aria-checked" && !["menuitemradio", "radio"].includes(role)
          || config.selected_attribute === "aria-selected" && role !== "option") return "current_account_selection_unverified";
        const nodes = current.querySelectorAll(job.dom.account_selector);
        if (nodes.length !== 1 || !visible(nodes[0]) || !this.outsideConversation(nodes[0], job)) return "account_unavailable";
        const value = job.dom.account_attribute === "textContent" ? nodes[0].textContent : nodes[0].getAttribute(job.dom.account_attribute);
        if (typeof value !== "string" || !/^[^\s@]+@163\.com$/.test(value.trim().toLowerCase())) return "account_unavailable";
        const raw = await this.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value.trim().toLowerCase()));
        const hash = Array.from(new Uint8Array(raw), (byte) => byte.toString(16).padStart(2, "0")).join("");
        answer = hash === job.account_sha256 ? null : "account_changed";
      } catch (_) { answer = "selected_account_unavailable"; }
      finally {
        if (rootControl) {
          // Only undo our own closed->open menu. No Escape/focus tricks and no
          // account switching. A changed control leaves the gate unverified.
          if (rootControl.getAttribute("aria-expanded") !== "true") answer = "account_menu_restore_unverified";
          else {
            rootControl.click();
            const closed = await this.waitFor(() => rootControl.getAttribute("aria-expanded") === "false"
              && openedMenus.every((menu) => !visible(menu)));
            if (!closed) answer = "account_menu_restore_unverified";
          }
        }
      }
      return this.location.href === job.conversation_url ? answer : "conversation_changed";
    }
    generating(job) {
      return Array.from(this.document.querySelectorAll(job.dom.generating_selector)).some(visible);
    }
    async inspect(job) {
      validateJob(job);
      const mismatch = await this.account(job);
      if (mismatch) return observation(job, "blocked", mismatch);
      const messages = Array.from(this.document.querySelectorAll(job.dom.message_selector)).filter(visible);
      const matches = messages.map((node, index) => ({ node, index })).filter(({ node }) =>
        node.getAttribute(job.dom.role_attribute) === "user" && text(node) === job.prompt);
      if (!matches.length) return observation(job, "not_sent", "original_user_not_observed");
      if (matches.length !== 1) return observation(job, "unknown", "duplicate_original_user");
      const { node: user, index } = matches[0];
      const userId = user.getAttribute(job.dom.message_id_attribute) || "";
      const following = messages.slice(index + 1);
      if (following.some((node) => node.getAttribute(job.dom.role_attribute) === "user")) {
        return observation(job, "unknown", "concurrent_user_turn", "", userId);
      }
      const assistants = following.filter((node) => node.getAttribute(job.dom.role_attribute) === "assistant");
      if (assistants.length !== 1) return observation(job, "partial", "assistant_not_unambiguous", "", userId);
      const assistant = assistants[0], assistantId = assistant.getAttribute(job.dom.message_id_attribute) || "";
      const raw = text(assistant);
      if (raw === null) return observation(job, "partial", "rendered_text_unavailable", "", userId, assistantId);
      // Oversize is explicitly incomplete: never return a cropped success.
      if (size(raw) > MAX_RESULT) return observation(job, "partial", "visible_text_exceeds_capture_limit", "", userId, assistantId);
      const turn = assistant.closest('[data-testid^="conversation-turn"]') || assistant;
      const done = Array.from(turn.querySelectorAll(job.dom.completion_selector)).some(visible);
      const terminal = done && !this.generating(job) && assistant.getAttribute("aria-busy") !== "true"
        && assistant.getAttribute("data-is-streaming") !== "true" && !!userId && !!assistantId && !!raw;
      return observation(job, terminal ? "completed" : "partial", terminal ? "visible_terminal" : "terminal_not_observed",
        raw, userId, assistantId, terminal);
    }
    editorText(editor) {
      return typeof editor.value === "string" ? editor.value : editor.textContent;
    }
    async preflight(job) {
      const prior = await this.inspect(job);
      if (prior.state !== "not_sent") return { ready: false, observation: prior };
      const editor = this.document.querySelector(job.dom.composer_selector);
      const send = this.document.querySelector(job.dom.send_selector);
      if (!visible(editor) || !visible(send) || editor.disabled || editor.getAttribute("aria-disabled") === "true") {
        return { ready: false, observation: observation(job, "blocked", "composer_unavailable") };
      }
      if (this.editorText(editor) !== "") return { ready: false, observation: observation(job, "blocked", "composer_occupied") };
      if (this.generating(job)) return { ready: false, observation: observation(job, "blocked", "generation_in_progress") };
      return { ready: true };
    }
    async send(job) {
      validateJob(job);
      if (this.clicked.has(job.request_id)) return observation(job, "unknown", "local_send_intent_exists");
      const ready = await this.preflight(job);
      if (!ready.ready) return ready.observation;
      this.clicked.add(job.request_id); // grant already durable on both sides
      const editor = this.document.querySelector(job.dom.composer_selector);
      if (typeof editor.value === "string") {
        const prototype = editor.tagName === "TEXTAREA" ? this.events.HTMLTextAreaElement : this.events.HTMLInputElement;
        const setter = prototype && Object.getOwnPropertyDescriptor(prototype.prototype, "value");
        if (!setter || typeof setter.set !== "function") return observation(job, "unknown", "native_editor_setter_unavailable");
        setter.set.call(editor, job.prompt);
      } else {
        editor.textContent = job.prompt;
      }
      if (!this.events.InputEvent) return observation(job, "unknown", "native_input_event_unavailable");
      editor.dispatchEvent(new this.events.InputEvent("input", { bubbles: true, inputType: "insertText", data: job.prompt }));
      await Promise.resolve();
      const send = this.document.querySelector(job.dom.send_selector);
      if (await this.account(job) || this.generating(job) || this.editorText(editor) !== job.prompt
          || !visible(send) || send.disabled || send.getAttribute("aria-disabled") === "true") {
        return observation(job, "unknown", "send_precondition_changed");
      }
      send.click(); // one normal DOM action; never Enter fallback or resend
      return observation(job, "unknown", "send_intent_observed");
    }
    watch(job, publish, milliseconds = 120000) {
      validateJob(job);
      if (this.watchers.has(job.request_id)) return;
      const entry = { pending: false, overdue: false, candidate: null, observer: null, timer: null, settle: null };
      this.watchers.set(job.request_id, entry);
      const close = () => {
        entry.observer.disconnect(); this.timers.clearTimeout(entry.timer); this.timers.clearTimeout(entry.settle);
        this.watchers.delete(job.request_id);
      };
      const evaluate = async () => {
        if (entry.pending || !this.watchers.has(job.request_id)) return;
        entry.pending = true;
        try {
          let result = await this.inspect(job);
          if (result.state === "completed") {
            // Explicit terminal affordance + no generation + a second complete
            // snapshot. Mere text stability or deadline never proves completion.
            const signature = JSON.stringify(result);
            if (entry.candidate === signature) { await publish(result); close(); }
            else { entry.candidate = signature; entry.settle = this.timers.setTimeout(evaluate, 200); }
          } else {
            entry.candidate = null;
            if (result.state === "blocked") { await publish(observation(job, "unknown", result.reason)); close(); }
            else if (entry.overdue) {
              if (result.state === "not_sent") result = observation(job, "unknown", "original_user_not_observed");
              await publish(result); // partial remains watched; not success
            }
          }
        } finally { entry.pending = false; }
      };
      entry.observer = new this.events.MutationObserver(() => { evaluate().catch(() => undefined); });
      entry.observer.observe(this.document.documentElement, { subtree: true, childList: true, characterData: true, attributes: true });
      entry.timer = this.timers.setTimeout(() => { entry.overdue = true; evaluate().catch(() => undefined); }, milliseconds);
      evaluate().catch(() => undefined);
    }
  }
  const exported = { ChatBridgeDOM, validateJob, validateAccountMenu, binding, equalBinding, observation, EXTENSION_ID, MAX_RESULT };
  if (typeof module !== "undefined") module.exports = exported;
  if (!root.MeshChatDOM) root.MeshChatDOM = exported;
  if (typeof browser !== "undefined" && root.location && root.location.origin === "https://chatgpt.com" && !root.MeshChatDOMInstalled) {
    root.MeshChatDOMInstalled = true;
    const adapter = new ChatBridgeDOM(document, location, crypto,
      { InputEvent, HTMLTextAreaElement, HTMLInputElement, MutationObserver });
    browser.runtime.onMessage.addListener(async (message, sender) => {
      if (!sender || sender.id !== EXTENSION_ID || sender.tab || !object(message)
          || message.schema !== 1 || Object.keys(message).sort().join() !== "action,job,schema") fail("content_caller_not_allowed");
      const job = validateJob(message.job);
      if (message.action === "preflight") return adapter.preflight(job);
      if (message.action === "send") return adapter.send(job);
      if (message.action === "watch") {
        adapter.watch(job, (result) => browser.runtime.sendMessage({ schema: 1, action: "observation", binding: binding(job), observation: result }));
        return { watching: true };
      }
      fail("content_action_not_allowed");
    });
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
