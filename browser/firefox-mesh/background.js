"use strict";

const EXTENSION_ID = "personal-assistant-mesh@gih10012.local";
const HOST_NAME = "personal_assistant_mesh";
const RECORD_KEY = "mesh_firefox_state_v1";
const MAX_REQUESTS = 64;
const MAX_TEXT = 16384;
const STATUSES = ["pending", "running", "completed", "failed", "waiting_auth", "waiting_backend",
  "waiting_children", "continuing", "needs_review", "paused", "waiting_remote", "unrecognized"];
const own = (value) => value && typeof value === "object" && !Array.isArray(value);
const bytes = (value) => new TextEncoder().encode(value).length;
const fail = (category) => { throw new Error(category); };

function validUnicode(value) {
  for (let index = 0; index < value.length; index++) {
    const code = value.charCodeAt(index);
    if (code >= 0xD800 && code <= 0xDBFF) {
      const next = value.charCodeAt(++index);
      if (!(next >= 0xDC00 && next <= 0xDFFF)) return false;
    } else if (code >= 0xDC00 && code <= 0xDFFF) return false;
  }
  return true;
}

function taskBody(message) {
  if (typeof message.input !== "string" || !message.input.trim() || !validUnicode(message.input) || bytes(message.input) > MAX_TEXT) fail("invalid_task_text");
  const body = { input: message.input };
  for (const field of ["project_id", "agent_id"]) {
    if (message[field] !== undefined && message[field] !== "") {
      if (typeof message[field] !== "string" || !message[field].trim() || !validUnicode(message[field])
          || /[\u0000-\u001F\u007F]/.test(message[field]) || message[field].trim() !== message[field] || bytes(message[field]) > 200) fail("invalid_task_label");
      body[field] = message[field];
    }
  }
  return body;
}

function canonicalBody(body) {
  return JSON.stringify(Object.fromEntries(Object.keys(body).sort().map((key) => [key, body[key]])));
}

class MeshFirefoxBridge {
  constructor(api, cryptoAPI) {
    this.api = api;
    this.crypto = cryptoAPI;
    this.queue = Promise.resolve();
  }

  async digest(body) {
    const hash = await this.crypto.subtle.digest("SHA-256", new TextEncoder().encode(canonicalBody(body)));
    return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  async activeChat(sender) {
    if (!sender || sender.id !== EXTENSION_ID || sender.tab || sender.url !== this.api.runtime.getURL("popup.html")) fail("caller_not_allowed");
    const tabs = await this.api.tabs.query({ active: true, currentWindow: true });
    if (!Array.isArray(tabs) || tabs.length !== 1 || !Number.isInteger(tabs[0].id) || tabs[0].active !== true) fail("active_chat_required");
    try {
      const url = new URL(tabs[0].url);
      if (url.origin !== "https://chatgpt.com" || url.username || url.password) fail("active_chat_required");
    } catch (_) { fail("active_chat_required"); }
    return tabs[0];
  }

  async checkedRecord(saved) {
    if (!own(saved) || Object.keys(saved).sort().join() !== "body,content_sha256,last_status,request_id,schema,state"
        || saved.schema !== 1 || !/^mesh-firefox-[0-9a-f]{32}$/.test(saved.request_id) || !own(saved.body)
        || !(saved.last_status === null || STATUSES.includes(saved.last_status))
        || !["prepared", "unknown", "received"].includes(saved.state) || !/^[0-9a-f]{64}$/.test(saved.content_sha256)) fail("saved_request_invalid");
    if (Object.keys(saved.body).some((key) => !["input", "project_id", "agent_id"].includes(key))) fail("saved_request_invalid");
    const body = taskBody(saved.body);
    if (await this.digest(body) !== saved.content_sha256) fail("saved_request_invalid");
    return saved;
  }

  async state() {
    const saved = (await this.api.storage.local.get(RECORD_KEY))[RECORD_KEY];
    if (saved === undefined) return { schema: 1, active_request_id: null, requests: [] };
    if (!own(saved) || Object.keys(saved).sort().join() !== "active_request_id,requests,schema"
        || saved.schema !== 1 || !Array.isArray(saved.requests) || saved.requests.length > MAX_REQUESTS) fail("saved_request_invalid");
    const ids = new Set();
    for (const record of saved.requests) {
      await this.checkedRecord(record);
      if (ids.has(record.request_id)) fail("saved_request_invalid");
      ids.add(record.request_id);
    }
    if (saved.active_request_id !== null && !ids.has(saved.active_request_id)) fail("saved_request_invalid");
    return saved;
  }

  view(state) {
    return { ok: true, record: state.requests.find((record) => record.request_id === state.active_request_id) || null,
      requests: state.requests.map(({ request_id, state, last_status }) => ({ request_id, state, last_status })), account_verified: false };
  }

  async save(state) {
    // One bounded state value includes every original ID; selecting a new
    // independent task cannot delete/evict an unresolved intent.
    await this.api.storage.local.set({ [RECORD_KEY]: state });
  }

  async prepare(message) {
    const body = taskBody(message);
    const fingerprint = await this.digest(body);
    const state = await this.state();
    const old = state.requests.find((record) => record.request_id === state.active_request_id);
    if (old) {
      if (old.content_sha256 !== fingerprint) fail("original_request_locked");
      return this.view(state);
    }
    const unresolved = state.requests.find((record) => record.content_sha256 === fingerprint && record.last_status !== "completed");
    if (unresolved) {
      state.active_request_id = unresolved.request_id;
      await this.save(state);
      return { ...this.view(state), reused_original_request: true };
    }
    if (state.requests.length >= MAX_REQUESTS) fail("saved_request_limit");
    const random = this.crypto.getRandomValues(new Uint8Array(16));
    const id = "mesh-firefox-" + Array.from(random, (byte) => byte.toString(16).padStart(2, "0")).join("");
    const record = { schema: 1, request_id: id, body, content_sha256: fingerprint, state: "prepared", last_status: null };
    // Intent, exact text and stable identity are durable BEFORE any native RPC.
    state.requests.push(record);
    state.active_request_id = id;
    await this.save(state);
    return this.view(state);
  }

  async invoke(action) {
    const state = await this.state();
    const record = state.requests.find((record) => record.request_id === state.active_request_id);
    if (!record) fail("prepare_request_first");
    if (action === "submit") {
      record.state = "unknown";
      record.last_status = null;
      await this.save(state);
    }
    const payload = action === "submit" ? { ...record.body, request_id: record.request_id } : { request_id: record.request_id };
    let response;
    try {
      response = await this.api.runtime.sendNativeMessage(HOST_NAME, { schema: 1, action, payload });
    } catch (_) {
      return { ok: false, error: "native_reply_unknown", outcome: "unknown", request_id: record.request_id, retry_with_new_id: false };
    }
    if (!own(response) || response.schema !== 1 || typeof response.ok !== "boolean" || response.retry_with_new_id !== false
        || bytes(JSON.stringify(response)) > 65536) fail("native_reply_invalid");
    if (response.ok) {
      if (response.action !== action || response.account_verified !== false || !own(response.data)
          || response.data.request_id !== record.request_id || response.data.execution_verified !== false
          || response.data.native_tools_intercepted !== false || !/^ingress-[0-9a-f]{64}$/.test(response.data.task_id)) fail("native_reply_invalid");
      if (action !== "result" && (!STATUSES.includes(response.data.status)
          || response.data.status_recognized !== (response.data.status !== "unrecognized"))) fail("native_reply_invalid");
      record.state = "received";
      if (action !== "result") record.last_status = response.data.status;
      await this.save(state);
    }
    return response;
  }

  async newTask() {
    const state = await this.state();
    state.active_request_id = null;
    await this.save(state);
    return this.view(state);
  }

  async select(requestID) {
    const state = await this.state();
    if (typeof requestID !== "string" || !state.requests.some((record) => record.request_id === requestID)) fail("saved_request_not_found");
    state.active_request_id = requestID;
    await this.save(state);
    return this.view(state);
  }

  async selection(tab) {
    // A single user-requested selection read; no DOM history/cookies/account
    // scraping, MutationObserver, page message listener or generic script input.
    const values = await this.api.tabs.executeScript(tab.id, { code: "window.getSelection().toString()", frameId: 0 });
    if (!Array.isArray(values) || typeof values[0] !== "string" || bytes(values[0]) > MAX_TEXT) fail("selection_unavailable");
    return { ok: true, selection: values[0], account_verified: false };
  }

  async process(message, sender) {
    if (!own(message) || typeof message.action !== "string") fail("invalid_bridge_message");
    const fields = message.action === "prepare" ? ["action", "input", "project_id", "agent_id"]
      : message.action === "select" ? ["action", "request_id"] : ["action"];
    if (Object.keys(message).some((field) => !fields.includes(field))) fail("invalid_bridge_message");
    const tab = await this.activeChat(sender);
    switch (message.action) {
      case "load": return this.view(await this.state());
      case "selection": return this.selection(tab);
      case "prepare": return this.prepare(message);
      case "submit": case "status": case "result": return this.invoke(message.action);
      case "new": return this.newTask();
      case "select": return this.select(message.request_id);
      default: fail("invalid_bridge_message");
    }
  }

  handle(message, sender) {
    const run = async () => {
      try { return await this.process(message, sender); }
      catch (error) {
        const known = new Set(["invalid_task_text", "invalid_task_label", "caller_not_allowed", "active_chat_required",
          "saved_request_invalid", "original_request_locked", "prepare_request_first", "native_reply_invalid",
          "saved_request_limit", "saved_request_not_found", "selection_unavailable", "invalid_bridge_message"]);
        return { ok: false, error: known.has(error.message) ? error.message : "bridge_unavailable", retry_with_new_id: false };
      }
    };
    const result = this.queue.then(run, run);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }
}

if (typeof browser !== "undefined") {
  const bridge = new MeshFirefoxBridge(browser, crypto);
  browser.runtime.onMessage.addListener((message, sender) => bridge.handle(message, sender));
}
if (typeof module !== "undefined") module.exports = { MeshFirefoxBridge, EXTENSION_ID, HOST_NAME, RECORD_KEY, MAX_REQUESTS };
