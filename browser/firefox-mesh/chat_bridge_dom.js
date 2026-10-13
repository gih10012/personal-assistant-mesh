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
    const layout = validateMessageLayout(job.dom.message_layout);
    for (const key of ["completion_selector", "composer_selector", "send_selector", "generating_selector"
      ].concat(layout ? [] : ["message_selector", "role_attribute", "message_id_attribute"])) {
      if (typeof job.dom[key] !== "string" || !job.dom[key] || job.dom[key].length > 256) fail("job_invalid");
    }
    if (job.dom.owner_confirmed_profile === undefined || job.dom.owner_confirmed_profile === null || job.dom.account_menu) {
      for (const key of ["account_selector", "account_attribute"]) {
        if (typeof job.dom[key] !== "string" || !job.dom[key] || job.dom[key].length > 256) fail("job_invalid");
      }
    }
    if (job.observed_identity !== undefined) validateObservedIdentity(job.observed_identity);
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
  function validateOwnerProfile(value, accountSha256) {
    if (value === undefined || value === null) return null;
    const required = ["account_sha256", "control_selector", "expected_label", "label_selector"];
    if (!object(value) || required.some((key) => !(key in value))
      || Object.keys(value).some((key) => !required.concat(["label_scope", "parent_selector"]).includes(key))
      || ![value.control_selector, value.label_selector, value.expected_label].every((item) =>
        typeof item === "string" && size(item) > 0 && size(item) <= 256 && !/[\u0000-\u001f\u007f]/.test(item))
      || value.expected_label !== value.expected_label.trim()
      || typeof value.account_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(value.account_sha256)) {
      fail("owner_profile_contract_invalid");
    }
    if (value.account_sha256 !== accountSha256) fail("owner_profile_namespace_conflict");
    const scope = value.label_scope === undefined ? "control" : value.label_scope;
    if (!["control", "direct_parent"].includes(scope) || scope === "control" && "parent_selector" in value
      || scope === "direct_parent" && (typeof value.parent_selector !== "string" || !value.parent_selector
        || value.parent_selector.length > 256 || /[\u0000-\u001f\u007f]/.test(value.parent_selector))) {
      fail("owner_profile_contract_invalid");
    }
    return value;
  }
  function validateMessageLayout(value) {
    if (value === undefined || value === null) return null;
    const keys = ["unit_selector", "unit_key_attribute", "user_role_suffix", "assistant_role_suffix",
      "user_text_selector", "assistant_text_selector", "assistant_id_attribute", "terminal_container_selector",
      "terminal_state_attribute", "terminal_state_value"];
    if (!object(value) || Object.keys(value).sort().join() !== keys.slice().sort().join()
      || keys.some((key) => typeof value[key] !== "string" || !value[key] || value[key].length > 256
        || /[\u0000-\u001f\u007f]/.test(value[key]))
      || ["unit_key_attribute", "assistant_id_attribute", "terminal_state_attribute"].some((key) =>
        !/^[a-z][a-z0-9-]{0,63}$/.test(value[key]))
      || value.user_role_suffix.endsWith(value.assistant_role_suffix)
      || value.assistant_role_suffix.endsWith(value.user_role_suffix)
      || !["complete", "completed"].includes(value.terminal_state_value)) fail("message_layout_invalid");
    return value;
  }
  function validateObservedIdentity(value) {
    const fields = object(value) && Object.keys(value).sort().join();
    const reference = value && value.user_reference;
    if (!object(value) || !["assistant_message_id,user_message_id", "assistant_message_id,user_reference"].includes(fields)
      || typeof value.assistant_message_id !== "string" || value.assistant_message_id.length > 200
      || fields === "assistant_message_id,user_message_id" && (typeof value.user_message_id !== "string" || !value.user_message_id || value.user_message_id.length > 200)
      || fields === "assistant_message_id,user_reference" && (!object(reference)
        || Object.keys(reference).sort().join() !== "kind,prompt_sha256,reference" || reference.kind !== "rendered_user_anchor"
        || typeof reference.reference !== "string" || !reference.reference || reference.reference.length > 200
        || reference.reference !== reference.reference.trim() || /[\u0000-\u001f\u007f]/.test(reference.reference)
        || typeof reference.prompt_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(reference.prompt_sha256))) fail("observed_identity_invalid");
    return value;
  }
  function observedIdentity(value) {
    if (!object(value)) return null;
    let identity;
    if (value.user_reference) identity = { user_reference: value.user_reference, assistant_message_id: value.assistant_message_id || "" };
    else if (value.user_message_id) identity = { user_message_id: value.user_message_id, assistant_message_id: value.assistant_message_id || "" };
    else return null;
    return validateObservedIdentity(identity);
  }
  function identityConflict(prior, current) {
    if (!prior || !current) return false;
    const userChanged = prior.user_message_id !== current.user_message_id
      || !!prior.user_reference !== !!current.user_reference
      || prior.user_reference && ["kind", "reference", "prompt_sha256"].some((key) => prior.user_reference[key] !== current.user_reference[key]);
    return !!userChanged || !!(prior.assistant_message_id && current.assistant_message_id && prior.assistant_message_id !== current.assistant_message_id);
  }
  function observation(job, state, reason, body = "", user = "", assistant = "", terminal = false, reference = null) {
    const value = { state, reason, text: body, conversation_url: job.conversation_url,
      account_sha256: job.account_sha256, user_message_id: user, assistant_message_id: assistant,
      terminal_visible: terminal, scope: "visible_dom_text" };
    if (reference) value.user_reference = reference;
    return value;
  }
  class ChatBridgeDOM {
    constructor(document, location, crypto, events = {}, timers = root) {
      this.document = document; this.location = location; this.crypto = crypto;
      this.events = events; this.timers = timers; this.clicked = new Set(); this.watchers = new Map();
      this.accountReads = Promise.resolve(); this.identities = new Map();
    }
    account(job) {
      // Serialize checks in this owned tab. A trusted owner-confirmed profile
      // binding is re-read without menus; arbitrary Chat names never count.
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
      const selector = job.dom.message_layout ? job.dom.message_layout.unit_selector : job.dom.message_selector;
      return node && !node.closest(selector) && !node.closest(job.dom.composer_selector);
    }
    readOwnerProfile(job, config) {
      try {
        const controls = Array.from(this.document.querySelectorAll(config.control_selector)).filter(visible);
        if (controls.length !== 1 || !this.outsideConversation(controls[0], job)) return "owner_profile_unavailable";
        const control = controls[0];
        if ((control.tagName !== "BUTTON" && control.getAttribute("role") !== "button")
          || control.disabled || control.getAttribute("aria-disabled") === "true"
          || control.getAttribute("aria-busy") === "true") return "owner_profile_unavailable";
        // Some normal profile buttons are empty overlays. The only optional
        // wider scope is their exact direct parent, never an arbitrary ancestor.
        let scope = control;
        if (config.label_scope === "direct_parent") {
          scope = control.parentElement;
          if (!visible(scope) || !this.outsideConversation(scope, job) || !scope.matches(config.parent_selector)) return "owner_profile_parent_unavailable";
          const siblings = Array.from(scope.querySelectorAll(config.control_selector)).filter(visible);
          if (siblings.length !== 1 || siblings[0] !== control) return "owner_profile_parent_unavailable";
        }
        const labels = Array.from(scope.querySelectorAll(config.label_selector)).filter(visible);
        if (labels.length !== 1 || !this.outsideConversation(labels[0], job)) return "owner_profile_label_unavailable";
        const label = text(labels[0]);
        if (typeof label !== "string" || !label.trim()) return "owner_profile_label_unavailable";
        if (this.location.href !== job.conversation_url) return "conversation_changed";
        return label.trim() === config.expected_label ? null : "account_changed";
      } catch (_) { return "owner_profile_unavailable"; }
    }
    async readSelectedAccount(job) {
      if (this.location.href !== job.conversation_url) return "conversation_changed";
      let profile;
      try { profile = validateOwnerProfile(job.dom.owner_confirmed_profile, job.account_sha256); }
      catch (error) { return error.message; }
      // Explicit installation binding takes priority. A changed/missing label
      // never triggers account-menu clicks or adoption of another identity.
      if (profile) return this.readOwnerProfile(job, profile);
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
    rememberIdentity(job, reference, assistantId) {
      const prior = this.identities.get(job.request_id) || job.observed_identity;
      if (identityConflict(prior, { user_reference: reference, assistant_message_id: assistantId })) return false;
      this.identities.set(job.request_id, { user_reference: reference,
        assistant_message_id: assistantId || prior && prior.assistant_message_id || "" });
      return true;
    }
    async inspectLayout(job, layout) {
      const units = Array.from(this.document.querySelectorAll(layout.unit_selector)).filter(visible).map((node) => {
        const key = node.getAttribute(layout.unit_key_attribute) || "";
        const role = key.endsWith(layout.user_role_suffix) ? "user" : key.endsWith(layout.assistant_role_suffix) ? "assistant" : "";
        const bodies = role ? Array.from(node.querySelectorAll(role === "user" ? layout.user_text_selector : layout.assistant_text_selector)).filter(visible) : [];
        return { node, key, role, body: bodies.length === 1 ? bodies[0] : null };
      }).filter((unit) => unit.role);
      if (units.some((unit) => unit.role === "user" && (!unit.body || text(unit.body) === null))) {
        return observation(job, "unknown", "user_body_not_unambiguous");
      }
      const matches = units.map((unit, index) => ({ unit, index })).filter(({ unit }) => unit.role === "user" && text(unit.body) === job.prompt);
      if (!matches.length) return observation(job, "not_sent", "original_user_not_observed");
      if (matches.length !== 1) return observation(job, "unknown", "duplicate_original_user");
      const { unit: user, index } = matches[0];
      if (!user.key || user.key.length > 200 || user.key !== user.key.trim() || /[\u0000-\u001f\u007f]/.test(user.key)) {
        return observation(job, "unknown", "rendered_user_anchor_unavailable");
      }
      const rawHash = await this.crypto.subtle.digest("SHA-256", new TextEncoder().encode(job.prompt));
      const reference = { kind: "rendered_user_anchor", reference: user.key,
        prompt_sha256: Array.from(new Uint8Array(rawHash), (byte) => byte.toString(16).padStart(2, "0")).join("") };
      if (this.location.href !== job.conversation_url || text(user.body) !== job.prompt
        || user.node.getAttribute(layout.unit_key_attribute) !== user.key) return observation(job, "unknown", "user_identity_changed");
      const following = units.slice(index + 1);
      if (following.some((unit) => unit.role === "user")) return observation(job, "unknown", "concurrent_user_turn", "", "", "", false, reference);
      const assistants = following.filter((unit) => unit.role === "assistant");
      const assistant = assistants.length === 1 ? assistants[0] : null;
      const rawId = assistant && assistant.body ? assistant.body.getAttribute(layout.assistant_id_attribute) || "" : "";
      const assistantId = rawId.length <= 200 && rawId === rawId.trim() && !/[\u0000-\u001f\u007f]/.test(rawId) ? rawId : "";
      if (!this.rememberIdentity(job, reference, assistantId)) return observation(job, "unknown", "message_identity_conflict", "", "", assistantId, false, reference);
      if (!assistant || !assistant.body) return observation(job, "partial", "assistant_not_unambiguous", "", "", "", false, reference);
      const raw = text(assistant.body);
      if (raw === null) return observation(job, "partial", "rendered_text_unavailable", "", "", assistantId, false, reference);
      if (size(raw) > MAX_RESULT) return observation(job, "partial", "visible_text_exceeds_capture_limit", "", "", assistantId, false, reference);
      const turn = assistant.body.closest(layout.terminal_container_selector);
      const uniqueBody = turn && Array.from(turn.querySelectorAll(layout.assistant_text_selector)).filter(visible);
      const done = turn && visible(turn) && uniqueBody.length === 1 && uniqueBody[0] === assistant.body
        && turn.getAttribute(layout.terminal_state_attribute) === layout.terminal_state_value
        && Array.from(turn.querySelectorAll(job.dom.completion_selector)).some(visible);
      const idle = [assistant.node, assistant.body, turn].filter(Boolean).every((node) =>
        node.getAttribute("aria-busy") !== "true" && node.getAttribute("data-is-streaming") !== "true");
      const terminal = !!done && idle && !this.generating(job) && !!raw && !!assistantId
        && assistantId.length <= 200 && assistantId === assistantId.trim();
      return observation(job, terminal ? "completed" : "partial", terminal ? "visible_terminal" : "terminal_not_observed",
        raw, "", assistantId, terminal, reference);
    }
    async inspect(job) {
      validateJob(job);
      const mismatch = await this.account(job);
      if (mismatch) return observation(job, "blocked", mismatch);
      const layout = validateMessageLayout(job.dom.message_layout);
      if (layout) return this.inspectLayout(job, layout);
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
    editor(job) {
      const editors = Array.from(this.document.querySelectorAll(job.dom.composer_selector)).filter(visible);
      return editors.length === 1 ? editors[0] : null;
    }
    sendButton(job) {
      const buttons = Array.from(this.document.querySelectorAll(job.dom.send_selector)).filter(visible);
      return buttons.length === 1 ? buttons[0] : null;
    }
    async preflight(job) {
      const prior = await this.inspect(job);
      if (job.dom.message_layout && prior.state === "completed") {
        // An already-rendered terminal is still only one snapshot. The
        // observation-only watcher must take two full matching snapshots.
        return { ready: false, observation: { ...prior, state: "partial", reason: "terminal_snapshot_unconfirmed", terminal_visible: false } };
      }
      if (prior.state !== "not_sent") return { ready: false, observation: prior };
      const editor = this.editor(job);
      if (!editor || editor.disabled || editor.readOnly || editor.getAttribute("aria-disabled") === "true") {
        return { ready: false, observation: observation(job, "blocked", "composer_unavailable") };
      }
      if (this.editorText(editor) !== "") return { ready: false, observation: observation(job, "blocked", "composer_occupied") };
      if (this.generating(job)) return { ready: false, observation: observation(job, "blocked", "generation_in_progress") };
      // Chat may display a voice control, not a send button, while its editor
      // is empty. Readiness grants only one fill attempt; the real, unique,
      // enabled send control must appear and be rechecked AFTER that fill.
      return { ready: true };
    }
    async send(job) {
      validateJob(job);
      if (this.clicked.has(job.request_id)) return observation(job, "unknown", "local_send_intent_exists");
      const ready = await this.preflight(job);
      if (!ready.ready) return ready.observation;
      this.clicked.add(job.request_id); // grant already durable on both sides
      const editor = this.editor(job);
      if (!editor) return observation(job, "unknown", "composer_changed");
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
      const send = await this.waitFor(() => this.sendButton(job));
      if (await this.account(job) || this.generating(job) || this.editor(job) !== editor
          || this.editorText(editor) !== job.prompt || this.sendButton(job) !== send
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
            else if (job.dom.message_layout && result.state === "unknown") { await publish(result); close(); }
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
  const exported = { ChatBridgeDOM, validateJob, validateAccountMenu, validateOwnerProfile, validateMessageLayout,
    validateObservedIdentity, observedIdentity, identityConflict,
    binding, equalBinding, observation, EXTENSION_ID, MAX_RESULT };
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
