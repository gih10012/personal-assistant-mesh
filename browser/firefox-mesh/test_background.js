"use strict";
// Offline mocks only: this is not Firefox installation/account acceptance.
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { MeshFirefoxBridge, EXTENSION_ID, HOST_NAME, RECORD_KEY, MAX_REQUESTS } = require("./background.js");

const clone = (value) => JSON.parse(JSON.stringify(value));
const TASK = "ingress-" + "2".repeat(64);
let passed = 0;

function fixture() {
  const store = {};
  const calls = [];
  let tabs = [{ id: 9, active: true, url: "https://chatgpt.com/c/fixture" }];
  let selection = "用户明确选中的文字";
  let reply;
  const api = {
    runtime: {
      getURL: (path) => "moz-extension://fixture/" + path,
      async sendNativeMessage(host, message) {
        const durable = clone(store[RECORD_KEY]);
        calls.push({ host, message: clone(message), durable });
        if (reply) return reply(host, message, durable);
        return success(message);
      },
    },
    tabs: {
      async query(query) { assert.deepEqual(query, { active: true, currentWindow: true }); return clone(tabs); },
      async executeScript(tabID, spec) {
        calls.push({ selection: true, tabID, spec });
        return [selection];
      },
    },
    storage: { local: {
      async get(key) { return store[key] === undefined ? {} : { [key]: clone(store[key]) }; },
      async set(values) { for (const [key, value] of Object.entries(values)) store[key] = clone(value); },
    } },
  };
  const sender = { id: EXTENSION_ID, url: api.runtime.getURL("popup.html") };
  let bridge = new MeshFirefoxBridge(api, webcrypto);
  return { api, store, calls, sender,
    run: (message, from = sender) => bridge.handle(message, from),
    reopen() { bridge = new MeshFirefoxBridge(api, webcrypto); },
    tabs(value) { tabs = value; },
    reply(fn) { reply = fn; },
    selection(value) { selection = value; },
  };
}

function success(message, taskStatus = "pending") {
  const data = {
    format: "owner-ingress/1", request_id: message.payload.request_id, task_id: TASK,
    accepted: true, status: taskStatus, status_recognized: true, result_available: false,
    execution_verified: false, native_tools_intercepted: false,
  };
  if (message.action === "submit") data.task_created = false;
  if (message.action === "result") {
    Object.assign(data, { published: true, result: { summary: "经审查摘要", artifacts: [] } });
  }
  return { schema: 1, ok: true, action: message.action, data, account_verified: false, retry_with_new_id: false };
}

async function test(name, action) {
  await action();
  passed += 1;
}

(async () => {
  await test("only own popup sender, never a page/content-script/external message", async () => {
    const f = fixture();
    for (const sender of [{}, { id: EXTENSION_ID, url: "https://chatgpt.com" },
                          { ...f.sender, tab: { id: 9 } }, { ...f.sender, id: "another@extension" }]) {
      assert.equal((await f.run({ action: "submit" }, sender)).error, "caller_not_allowed");
    }
    assert.equal(f.calls.length, 0);
  });
  await test("exact active chatgpt.com origin only, not spoofed suffix/http", async () => {
    const f = fixture();
    for (const url of ["https://chatgpt.com.evil.invalid/c/1", "http://chatgpt.com/c/1",
                       "https://chat.openai.com", "https://user:secret@chatgpt.com", "about:blank"]) {
      f.tabs([{ id: 9, active: true, url }]);
      assert.equal((await f.run({ action: "load" })).error, "active_chat_required");
    }
    f.tabs([]);
    assert.equal((await f.run({ action: "load" })).error, "active_chat_required");
    assert.equal(f.calls.length, 0);
  });
  await test("explicit selection is one fixed read and never submits", async () => {
    const f = fixture();
    const value = await f.run({ action: "selection" });
    assert.equal(value.selection, "用户明确选中的文字");
    assert.deepEqual(f.calls, [{ selection: true, tabID: 9,
      spec: { code: "window.getSelection().toString()", frameId: 0 } }]);
    assert.equal(f.store[RECORD_KEY], undefined);
    f.selection("中".repeat(5500));
    assert.equal((await f.run({ action: "selection" })).error, "selection_unavailable");
  });
  await test("load/prepare/select/new are local and do not call a native host", async () => {
    const f = fixture();
    assert.equal((await f.run({ action: "load" })).record, null);
    const a = await f.run({ action: "prepare", input: "开放任务一" });
    assert.match(a.record.request_id, /^mesh-firefox-[0-9a-f]{32}$/);
    await f.run({ action: "new" });
    await f.run({ action: "select", request_id: a.record.request_id });
    assert.equal(f.calls.length, 0);
  });
  await test("intent exact text id and hash are persisted before submission", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一", project_id: "项目甲", agent_id: "连续子任务" });
    f.reply(async (host, message, durable) => {
      assert.equal(host, HOST_NAME);
      const stored = durable.requests.find((record) => record.request_id === message.payload.request_id);
      assert.equal(stored.state, "unknown");
      assert.deepEqual(stored.body, { input: "任务一", project_id: "项目甲", agent_id: "连续子任务" });
      assert.equal(stored.request_id, a.record.request_id);
      assert.match(stored.content_sha256, /^[0-9a-f]{64}$/);
      return success(message);
    });
    assert.equal((await f.run({ action: "submit" })).ok, true);
  });
  await test("lost native reply retains original id across reopened background", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一" });
    f.reply(() => { throw new Error("PRIVATE_TOKEN network error"); });
    const lost = await f.run({ action: "submit" });
    assert.equal(lost.outcome, "unknown");
    assert.equal(lost.request_id, a.record.request_id);
    assert.equal(lost.retry_with_new_id, false);
    assert.equal(JSON.stringify(lost).includes("PRIVATE"), false);
    f.reopen();
    assert.equal((await f.run({ action: "load" })).record.request_id, a.record.request_id);
    f.reply((_, message) => success(message, "running"));
    await f.run({ action: "status" });
    assert.equal(f.calls[1].message.payload.request_id, a.record.request_id);
    assert.deepEqual(Object.keys(f.calls[1].message.payload), ["request_id"]);
  });
  await test("new independent parallel task preserves unresolved original and can select it", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一" });
    f.reply(() => { throw new Error("lost"); });
    await f.run({ action: "submit" });
    const independent = await f.run({ action: "new" });
    assert.equal(independent.record, null);
    assert.equal(independent.requests[0].request_id, a.record.request_id);
    const b = await f.run({ action: "prepare", input: "不同的独立项目二" });
    assert.notEqual(a.record.request_id, b.record.request_id);
    assert.equal(b.requests.length, 2);
    f.reopen();
    const selected = await f.run({ action: "select", request_id: a.record.request_id });
    assert.equal(selected.record.state, "unknown");
    f.reply((_, message) => success(message, "running"));
    await f.run({ action: "status" });
    assert.equal(f.calls[1].message.payload.request_id, a.record.request_id);
  });
  await test("same unresolved exact body reuses original id even after explicit new", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一", project_id: "项目" });
    f.reply(() => { throw new Error("lost"); });
    await f.run({ action: "submit" });
    await f.run({ action: "new" });
    const restored = await f.run({ action: "prepare", project_id: "项目", input: "任务一" });
    assert.equal(restored.record.request_id, a.record.request_id);
    assert.equal(restored.reused_original_request, true);
    assert.equal(restored.requests.length, 1);
    assert.equal(f.calls.length, 1);
  });
  await test("completed task may have explicit independent repeated text", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "相同周期任务" });
    f.reply((_, message) => success(message, "completed"));
    await f.run({ action: "submit" });
    await f.run({ action: "new" });
    const b = await f.run({ action: "prepare", input: "相同周期任务" });
    assert.notEqual(a.record.request_id, b.record.request_id);
    assert.equal(b.requests.length, 2);
  });
  await test("draft change requires explicit independent intent and cannot overwrite old body", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一" });
    assert.equal((await f.run({ action: "prepare", input: "任务二" })).error, "original_request_locked");
    assert.equal((await f.run({ action: "load" })).record.content_sha256, a.record.content_sha256);
    assert.equal(f.calls.length, 0);
  });
  await test("closed popup methods and saved-only ID selection block generic commands", async () => {
    const f = fixture();
    for (const message of [{ action: "submit", shell: "fish" }, { action: "prepare", input: "任务", url: "evil" },
                           { action: "selection", code: "cookies" }, { action: "operator" },
                           { action: "load", config_path: "/private/config" }, { action: "status", request_id: "global" }]) {
      assert.equal((await f.run(message)).error, "invalid_bridge_message");
    }
    assert.equal((await f.run({ action: "select", request_id: "global-task" })).error, "saved_request_not_found");
    assert.equal(f.calls.length, 0);
  });
  await test("concurrent double prepares serialize to one stable id", async () => {
    const f = fixture();
    const [a, b] = await Promise.all([f.run({ action: "prepare", input: "任务一" }), f.run({ action: "prepare", input: "任务一" })]);
    assert.equal(a.record.request_id, b.record.request_id);
    assert.equal(f.store[RECORD_KEY].requests.length, 1);
  });
  await test("stored fingerprint corruption or body authority fields never submitted", async () => {
    for (const mutate of [
      (record) => { record.body.input = "暗改"; },
      (record) => { record.body.role = "operator"; },
      (record) => { record.extra_secret = "private"; },
    ]) {
      const f = fixture();
      await f.run({ action: "prepare", input: "任务一" });
      mutate(f.store[RECORD_KEY].requests[0]);
      assert.equal((await f.run({ action: "submit" })).error, "saved_request_invalid");
      assert.equal(f.calls.length, 0);
    }
  });
  await test("invalid host response remains unknown and never automatically retries", async () => {
    const f = fixture();
    await f.run({ action: "prepare", input: "任务一" });
    f.reply((_, message) => ({ ...success(message), account_verified: true }));
    assert.equal((await f.run({ action: "submit" })).error, "native_reply_invalid");
    assert.equal((await f.run({ action: "load" })).record.state, "unknown");
    assert.equal(f.calls.length, 1);
  });
  await test("unknown status never recorded as recognized completion", async () => {
    const f = fixture();
    await f.run({ action: "prepare", input: "任务一" });
    f.reply((_, message) => success(message, "unrecognized"));
    assert.equal((await f.run({ action: "submit" })).error, "native_reply_invalid");
    assert.equal((await f.run({ action: "load" })).record.last_status, null);
  });
  await test("bounded history does not evict IDs to create new task", async () => {
    const f = fixture();
    for (let index = 0; index < MAX_REQUESTS; index++) {
      assert.equal((await f.run({ action: "prepare", input: "独立任务" + index })).ok, true);
      await f.run({ action: "new" });
    }
    const before = clone(f.store[RECORD_KEY]);
    assert.equal((await f.run({ action: "prepare", input: "超出本地上限的新任务" })).error, "saved_request_limit");
    assert.deepEqual(f.store[RECORD_KEY], before);
    assert.equal(f.calls.length, 0);
  });
  await test("manual submit failure does not reuse stale completed status", async () => {
    const f = fixture();
    const a = await f.run({ action: "prepare", input: "任务一" });
    f.reply((_, message) => success(message, "completed"));
    await f.run({ action: "submit" });
    f.reply(() => { throw new Error("lost"); });
    await f.run({ action: "submit" });
    await f.run({ action: "new" });
    const restored = await f.run({ action: "prepare", input: "任务一" });
    assert.equal(restored.record.request_id, a.record.request_id);
    assert.equal(restored.record.last_status, null);
  });
  await test("failed durable intent write never sends a native submission", async () => {
    const f = fixture();
    await f.run({ action: "prepare", input: "任务一" });
    f.api.storage.local.set = async () => { throw new Error("PRIVATE_DISK_ERROR"); };
    const response = await f.run({ action: "submit" });
    assert.equal(response.error, "bridge_unavailable");
    assert.equal(JSON.stringify(response).includes("PRIVATE"), false);
    assert.equal(f.calls.length, 0);
  });
  await test("invalid Unicode or native selector label rejected before saved intent", async () => {
    const f = fixture();
    assert.equal((await f.run({ action: "prepare", input: "\uD800" })).error, "invalid_task_text");
    assert.equal((await f.run({ action: "prepare", input: "任务", agent_id: "label\nrole" })).error, "invalid_task_label");
    assert.equal(f.store[RECORD_KEY], undefined);
  });
  process.stdout.write(`${passed} offline contracts passed\n`);
})().catch(() => {
  // A test stack contains code locations only, but keep run output fixed too.
  process.stderr.write("offline contract assertion failed\n");
  process.exitCode = 1;
});
