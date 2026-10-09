"use strict";
const get = (id) => document.getElementById(id);
const buttons = ["selection", "prepare", "submit", "status", "result", "new", "select"];
let busy = false;

function recordView(record) {
  if (record) {
    get("input").value = record.body.input;
    get("project").value = record.body.project_id || "";
    get("agent").value = record.body.agent_id || "";
    get("identity").textContent = `${record.request_id} · ${record.state} · ${record.last_status || "尚无回执"}`;
  } else {
    get("input").value = "";
    get("project").value = "";
    get("agent").value = "";
    get("identity").textContent = "尚未准备任务";
  }
  for (const id of ["input", "project", "agent"]) get(id).readOnly = !!record;
  get("confirm").checked = false;
}

function historyView(requests) {
  get("history").replaceChildren();
  const first = document.createElement("option");
  first.value = "";
  first.textContent = "选择保留的原 ID（不执行网络请求）";
  get("history").appendChild(first);
  for (const record of requests) {
    const option = document.createElement("option");
    option.value = record.request_id;
    option.textContent = `${record.request_id} · ${record.state} · ${record.last_status || "尚无回执"}`;
    get("history").appendChild(option);
  }
}

async function call(message) {
  if (busy) return;
  busy = true;
  for (const id of buttons) get(id).disabled = true;
  try {
    const response = await browser.runtime.sendMessage(message);
    // Never innerHTML, eval, automatic URL navigation, chat composer insertion
    // or executing a tool block from a result/page/streaming assistant answer.
    get("output").textContent = JSON.stringify(response, null, 2);
    if (response.ok && Object.prototype.hasOwnProperty.call(response, "record")) recordView(response.record);
    if (response.ok && Array.isArray(response.requests)) historyView(response.requests);
    if (response.ok && typeof response.selection === "string" && !get("input").readOnly) get("input").value = response.selection;
    if (["submit", "status", "result"].includes(message.action)) {
      const saved = await browser.runtime.sendMessage({ action: "load" });
      if (saved.ok) { recordView(saved.record); historyView(saved.requests); }
    }
  } catch (_) { get("output").textContent = "入口暂不可用；保留原 ID，不自动重投。"; }
  finally {
    busy = false;
    get("confirm").checked = false;
    for (const id of buttons) get(id).disabled = false;
  }
}

get("selection").addEventListener("click", () => call({ action: "selection" }));
get("prepare").addEventListener("click", () => call({ action: "prepare", input: get("input").value,
  project_id: get("project").value, agent_id: get("agent").value }));
get("submit").addEventListener("click", () => {
  if (!get("confirm").checked) { get("output").textContent = "请先明确审查并确认提交。"; return; }
  call({ action: "submit" });
});
get("status").addEventListener("click", () => call({ action: "status" }));
get("result").addEventListener("click", () => call({ action: "result" }));
get("new").addEventListener("click", () => {
  if (window.confirm("准备一个新的独立任务？这不是替换或重投旧任务：旧任务仍可运行，所有原 ID 均保留。相同未决正文会回到原 ID。")) call({ action: "new" });
});
get("select").addEventListener("click", () => call({ action: "select", request_id: get("history").value }));
call({ action: "load" });
