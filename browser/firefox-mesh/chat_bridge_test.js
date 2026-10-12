"use strict";
// Synthetic controllable DOM/native port only; not a Chat account or Firefox.
const assert = require("node:assert/strict");
const { webcrypto, createHash } = require("node:crypto");
const dom = require("./chat_bridge_dom.js"), { ChatCollaborator, KEY, TAB_KEY } = require("./chat_bridge_background.js");
const clone = (value) => JSON.parse(JSON.stringify(value));
const hash = async (value) => Buffer.from(await webcrypto.subtle.digest("SHA-256", new TextEncoder().encode(value))).toString("hex");
class Node {
  constructor(body = "", attributes = {}) { this.innerText = body; this.textContent = body; this.attributes = attributes; this.nodes = {}; this.disabled = false; this.clicks = 0; }
  getAttribute(key) { return this.attributes[key] || null; }
  getClientRects() { return this.hidden ? [] : [1]; }
  querySelectorAll(selector) { return this.nodes[selector] || []; }
  closest() { return this.turn || null; }
  dispatchEvent(event) { this.lastEvent = event; }
  click() { this.clicks++; if (this.onClick) this.onClick(); }
}
class Timers {
  constructor() { this.actions = new Map(); this.sequence = 0; }
  setTimeout(action, time) { const id = ++this.sequence; this.actions.set(id, { action, time }); return id; }
  clearTimeout(id) { this.actions.delete(id); }
  async fire(time) { for (const [id, item] of Array.from(this.actions)) if (item.time === time) { this.actions.delete(id); await item.action(); } await new Promise((r) => setImmediate(r)); }
}
async function fixture() {
  const job = { request_id: "fixture-request", task_id: "fixture-child", project_id: "fixture-project", content_sha256: "1".repeat(64),
    account_sha256: await hash("fixture-owner@163.com"), conversation_url: "https://chatgpt.com/c/fixture-conversation-1", mode: "prepare",
    dom: { account_selector: "#account", account_attribute: "textContent", completion_selector: ".done", composer_selector: "#editor",
      send_selector: "#send", generating_selector: ".stop", message_selector: ".message", role_attribute: "role", message_id_attribute: "id",
      account_menu: { steps: [{ control_selector: "#profile-menu", menu_selector: "#account-menu", menu_role: "menu" }],
        current_selector: ".current-account", selected_attribute: "aria-checked", selected_value: "true" } } };
  job.prompt = `Mesh request: ${job.request_id}\nMesh content SHA-256: ${job.content_sha256}\n\n  原文 🐾\n\n`;
  const account = new Node("fixture-owner@163.com"), editor = new Node(), send = new Node(), nodes = {
    "#account": [account], "#editor": [editor], "#send": [send], ".stop": [], ".message": [] };
  const profile = new Node("", { role: "button", "aria-haspopup": "menu", "aria-expanded": "false", "aria-controls": "account-menu" });
  const menu = new Node("", { role: "menu", id: "account-menu" }); menu.hidden = true;
  const current = new Node("", { role: "menuitemradio", "aria-checked": "true" }); current.nodes["#account"] = [account];
  menu.nodes[".current-account"] = [current]; nodes["#profile-menu"] = [profile]; nodes["#account-menu"] = [menu];
  profile.onClick = () => { menu.hidden = !menu.hidden; profile.attributes["aria-expanded"] = menu.hidden ? "false" : "true"; };
  const document = { querySelectorAll: (selector) => nodes[selector] || [], querySelector: (selector) => (nodes[selector] || [])[0], documentElement: {} };
  const location = { href: job.conversation_url }, timers = new Timers();
  let observer;
  class MutationObserver { constructor(callback) { this.callback = callback; this.closed = false; observer = this; } observe() {} disconnect() { this.closed = true; } }
  const deterministicCrypto = { subtle: { digest: async (_type, bytes) => Uint8Array.from(createHash("sha256").update(bytes).digest()).buffer } };
  const adapter = new dom.ChatBridgeDOM(document, location, deterministicCrypto, { InputEvent: class { constructor(type, value) { this.type = type; Object.assign(this, value); } }, MutationObserver }, timers);
  return { job, nodes, account, profile, menu, current, editor, send, document, location, adapter, timers, observer: () => observer };
}
function messages(f, body = "  完整答案\n```text\n保留空白\n```\n\n", complete = true) {
  const user = new Node(f.job.prompt, { role: "user", id: "user-1" }), assistant = new Node(body, { role: "assistant", id: "assistant-1" });
  assistant.nodes[".done"] = complete ? [new Node()] : [];
  f.nodes[".message"] = [user, assistant]; return { user, assistant };
}
let count = 0;
async function test(name, action) { await action(); count++; }
function browserFixture() {
  let stored; const tabs = new Map(), markers = new Map(), created = [], operations = [], calls = [];
  const api = {
    storage: { local: { async get() { return stored === undefined ? {} : { [KEY]: clone(stored) }; }, async set(value) { stored = clone(value[KEY]); } } },
    sessions: { async getTabValue(id, key) { assert.equal(key, TAB_KEY); return markers.get(id); }, async setTabValue(id, key, marker) { markers.set(id, clone(marker)); } },
    tabs: {
      async create(value) { assert.equal(value.active, false); assert.ok(!("windowId" in value)); created.push(value); const tab = { id: 10 + created.length, url: value.url, active: false, status: "complete" }; tabs.set(tab.id, tab); return tab; },
      async get(id) { if (!tabs.has(id)) throw Error("tab closed"); return tabs.get(id); },
      async executeScript(id, value) { assert.deepEqual(value, { file: "chat_bridge_dom.js", frameId: 0 }); operations.push({ id, action: "inject" }); },
      async sendMessage(id, message) { operations.push({ id, action: message.action });
        if (message.action === "preflight") return { ready: true };
        if (message.action === "send") return dom.observation(message.job, "unknown", "send_intent_observed");
        if (message.action === "watch") return { watching: true };
        throw Error("unexpected"); }
    }, runtime: {} };
  const timers = new Timers(), bridge = new ChatCollaborator(api, webcrypto, timers); bridge.owner = "fixture-owner";
  bridge.rpc = async (action, job, observation) => {
    calls.push({ action, job: clone(job), observation: observation && clone(observation), stored: clone(stored) });
    return action === "begin_send" ? { send_granted: true } : {};
  };
  return { api, bridge, timers, tabs, markers, created, operations, calls, stored: () => stored };
}
(async () => {
  await test("strict project original conversation and prompt marker", async () => {
    const f = await fixture(); assert.equal(dom.validateJob(f.job), f.job);
    assert.throws(() => dom.validateJob({ ...f.job, conversation_url: "https://chatgpt.com/" }));
    assert.throws(() => dom.validateJob({ ...f.job, prompt: "replacement" }));
  });
  await test("wrong account never reads or fills a task", async () => {
    const f = await fixture(); f.account.textContent = "another@163.com";
    assert.equal((await f.adapter.preflight(f.job)).observation.reason, "account_changed");
    assert.equal((await f.adapter.send(f.job)).state, "blocked"); assert.equal(f.send.clicks, 0); assert.equal(f.editor.textContent, "");
  });
  await test("unknown account and changed conversation fail closed", async () => {
    const f = await fixture(); f.current.nodes["#account"] = []; assert.equal((await f.adapter.inspect(f.job)).reason, "account_unavailable");
    f.location.href = "https://chatgpt.com/c/another-conversation"; assert.equal((await f.adapter.inspect(f.job)).reason, "conversation_changed");
  });
  await test("hidden account metadata is not a visible identity check", async () => {
    const f = await fixture(); f.account.hidden = true;
    assert.equal((await f.adapter.preflight(f.job)).observation.reason, "account_unavailable");
    assert.equal(f.editor.textContent, ""); assert.equal(f.send.clicks, 0);
  });
  await test("arbitrary visible page email without selected-account contract is never evidence", async () => {
    const f = await fixture(); delete f.job.dom.account_menu;
    assert.equal((await f.adapter.preflight(f.job)).observation.reason, "selected_account_contract_not_configured");
    assert.equal(f.profile.clicks, 0); assert.equal(f.send.clicks, 0);
  });
  await test("only the current selected account row counts, not a matching nonselected row", async () => {
    const f = await fixture(); f.account.textContent = "other@163.com";
    const other = new Node("", { role: "menuitemradio", "aria-checked": "false" });
    other.nodes["#account"] = [new Node("fixture-owner@163.com")]; f.menu.nodes[".nonselected"] = [other];
    assert.equal((await f.adapter.account(f.job)), "account_changed");
    f.current.attributes["aria-checked"] = "false";
    assert.equal((await f.adapter.account(f.job)), "current_account_selection_unverified");
    assert.equal(f.send.clicks, 0);
  });
  await test("read-only linked menu opens and restores, never clicks selected account", async () => {
    const f = await fixture(); assert.equal(await f.adapter.account(f.job), null);
    assert.equal(f.profile.clicks, 2); assert.equal(f.menu.hidden, true); assert.equal(f.current.clicks, 0); assert.equal(f.account.clicks, 0);
  });
  await test("chat-contained account widget cannot open a menu or supply identity", async () => {
    const f = await fixture(); f.profile.turn = new Node("CHAT BODY");
    assert.equal(await f.adapter.account(f.job), "account_control_unavailable"); assert.equal(f.profile.clicks, 0);
  });
  await test("already-open user menu is not toggled or adopted by automatic check", async () => {
    const f = await fixture(); f.menu.hidden = false; f.profile.attributes["aria-expanded"] = "true";
    assert.equal(await f.adapter.account(f.job), "account_menu_not_read_only_control"); assert.equal(f.profile.clicks, 0);
  });
  await test("occupied composer and ongoing generation never replaced", async () => {
    const f = await fixture(); f.editor.textContent = "USER DRAFT";
    assert.equal((await f.adapter.send(f.job)).reason, "composer_occupied"); assert.equal(f.editor.textContent, "USER DRAFT");
    f.editor.textContent = ""; f.nodes[".stop"] = [new Node()]; assert.equal((await f.adapter.send(f.job)).reason, "generation_in_progress"); assert.equal(f.send.clicks, 0);
  });
  await test("one normal DOM send preserves complete prompt and no fallback", async () => {
    const f = await fixture(); assert.equal((await f.adapter.preflight(f.job)).ready, true);
    assert.equal((await f.adapter.send(f.job)).reason, "send_intent_observed"); assert.equal(f.editor.textContent, f.job.prompt); assert.equal(f.send.clicks, 1);
    assert.equal((await f.adapter.send(f.job)).reason, "local_send_intent_exists"); assert.equal(f.send.clicks, 1);
  });
  await test("disabled send after filling stays unknown and is never clicked", async () => {
    const f = await fixture(); f.send.disabled = true; assert.equal((await f.adapter.send(f.job)).state, "unknown"); assert.equal(f.send.clicks, 0);
  });
  await test("full visible terminal exact whitespaces and stable identities", async () => {
    const f = await fixture(); const { assistant } = messages(f); const result = await f.adapter.inspect(f.job);
    assert.equal(result.state, "completed"); assert.equal(result.text, assistant.innerText); assert.notEqual(result.text, result.text.trim()); assert.equal(result.assistant_message_id, "assistant-1");
  });
  await test("text stability alone, absent IDs or generation do not complete", async () => {
    const f = await fixture(); const { assistant } = messages(f, "stable", false); assert.equal((await f.adapter.inspect(f.job)).state, "partial");
    assistant.nodes[".done"] = [new Node()]; delete assistant.attributes.id; assert.equal((await f.adapter.inspect(f.job)).state, "partial");
    assistant.attributes.id = "assistant-1"; f.nodes[".stop"] = [new Node()]; assert.equal((await f.adapter.inspect(f.job)).state, "partial");
  });
  await test("duplicate request and intervening user turn stay unknown", async () => {
    const f = await fixture(); const { user } = messages(f); f.nodes[".message"].push(user); assert.equal((await f.adapter.inspect(f.job)).state, "unknown");
    messages(f); f.nodes[".message"].push(new Node("UNRELATED", { role: "user", id: "user-other" })); assert.equal((await f.adapter.inspect(f.job)).reason, "concurrent_user_turn");
  });
  await test("oversized visible answer never cropped into success", async () => {
    const f = await fixture(); messages(f, "x".repeat(dom.MAX_RESULT + 1)); const value = await f.adapter.inspect(f.job);
    assert.equal(value.state, "partial"); assert.equal(value.reason, "visible_text_exceeds_capture_limit"); assert.equal(value.text, "");
  });
  await test("MAX_WAIT yields only partial and later terminal is still watched", async () => {
    const f = await fixture(); const { assistant } = messages(f, "前半", false), outputs = [];
    f.adapter.watch(f.job, async (value) => { outputs.push(value); }, 1000);
    await new Promise((r) => setImmediate(r)); await f.timers.fire(1000);
    assert.equal(outputs[0].state, "partial"); assert.equal(f.observer().closed, false);
    assistant.innerText = "  后续完整 🐾\n\n"; assistant.nodes[".done"] = [new Node()]; f.observer().callback();
    await new Promise((r) => setImmediate(r)); await f.timers.fire(200);
    assert.equal(outputs[outputs.length - 1].state, "completed"); assert.equal(outputs[outputs.length - 1].text, assistant.innerText); assert.equal(f.observer().closed, true);
  });
  await test("owned inactive project tab reused, ordinary matching URL not adopted", async () => {
    const f = await fixture(), b = browserFixture(); b.tabs.set(99, { id: 99, url: f.job.conversation_url, active: true });
    const first = await b.bridge.ownedTab(f.job), second = await b.bridge.ownedTab(f.job);
    assert.equal(first.id, second.id); assert.equal(b.created.length, 1); assert.notEqual(first.id, 99); assert.equal(b.tabs.get(99).active, true);
  });
  await test("reused tab ID without extension marker is never controlled", async () => {
    const f = await fixture(), b = browserFixture(); const first = await b.bridge.ownedTab(f.job); b.markers.delete(first.id);
    const other = await b.bridge.ownedTab(f.job); assert.notEqual(first.id, other.id); assert.equal(b.created.length, 2);
  });
  await test("background persists intent before grant and before DOM action", async () => {
    const f = await fixture(), b = browserFixture(); await b.bridge.work(f.job);
    const grant = b.calls.find((call) => call.action === "begin_send"); assert.equal(grant.stored.requests[f.job.request_id].send_intent, true);
    assert.equal(b.operations.filter((item) => item.action === "send").length, 1);
    assert.equal(b.operations.some((item) => item.id === 99), false);
  });
  await test("lost grant ACK plus reopened background only observes original", async () => {
    const f = await fixture(), b = browserFixture(); b.bridge.rpc = async () => { throw Error("lost ACK"); };
    await b.bridge.work(f.job); assert.equal(b.stored().requests[f.job.request_id].send_intent, true);
    assert.equal(b.operations.some((item) => item.action === "send"), false);
    assert.equal(b.operations.some((item) => item.action === "watch"), true); // observation starts even before reconnect
    const reopened = new ChatCollaborator(b.api, webcrypto, b.timers); reopened.owner = "fixture-owner";
    const actions = []; reopened.rpc = async (action) => { actions.push(action); return {}; }; await reopened.work(f.job);
    assert.deepEqual(actions, ["report"]); assert.equal(b.operations.some((item) => item.action === "send"), false); assert.equal(b.operations.some((item) => item.action === "watch"), true);
  });
  await test("native observe mode cannot ask for another DOM send", async () => {
    const f = await fixture(), b = browserFixture(); await b.bridge.work({ ...f.job, mode: "observe" });
    assert.equal(b.calls.some((item) => item.action === "begin_send"), false); assert.equal(b.operations.some((item) => item.action === "send"), false);
  });
  await test("foreign tab/page/frame cannot return outcomes", async () => {
    const f = await fixture(), b = browserFixture(); await b.bridge.work(f.job);
    const message = { schema: 1, action: "observation", binding: dom.binding(f.job), observation: dom.observation(f.job, "partial", "terminal_not_observed", "visible") };
    for (const sender of [{ id: "evil", tab: { id: 11 }, frameId: 0, url: f.job.conversation_url },
      { id: dom.EXTENSION_ID, tab: { id: 99 }, frameId: 0, url: f.job.conversation_url },
      { id: dom.EXTENSION_ID, tab: { id: 11 }, frameId: 1, url: f.job.conversation_url }]) await assert.rejects(() => b.bridge.receiveObservation(message, sender));
  });
  await test("correct owned result persists before native reply and restores without browser resend", async () => {
    const f = await fixture(), b = browserFixture(); await b.bridge.work(f.job);
    const value = dom.observation(f.job, "completed", "visible_terminal", "  完整输出\n\n", "user-1", "assistant-1", true);
    await b.bridge.receiveObservation({ schema: 1, action: "observation", binding: dom.binding(f.job), observation: value },
      { id: dom.EXTENSION_ID, tab: { id: 11 }, frameId: 0, url: f.job.conversation_url });
    const reopened = new ChatCollaborator(b.api, webcrypto, b.timers); reopened.owner = "fixture-owner"; const calls = [];
    reopened.rpc = async (action, job, observation) => { calls.push({ action, observation }); return {}; };
    const before = b.operations.length; await reopened.work(f.job);
    assert.equal(b.operations.length, before); assert.equal(calls[0].observation.text, value.text);
  });
  console.log(`${count} controlled DOM/port cases passed; no actual browser/account/inference`);
})().catch((error) => { console.error(error); process.exitCode = 1; });
