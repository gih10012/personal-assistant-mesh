import fs from "node:fs";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Credentials stay in a private host grant, never a tool parameter or prompt.
function privateJson(file: string) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) || info.uid !== process.getuid?.())
    throw new Error("private_mesh_grant_required");
  return JSON.parse(fs.readFileSync(file, "utf8"));
}

export default function (pi: ExtensionAPI) {
  const grant = privateJson(process.env.MESH_PI_GRANT_FILE!);
  const target = new URL(grant.control_url);
  if (target.protocol !== "https:" && !(target.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)))
    throw new Error("mesh_control_requires_tls_or_loopback");
  const tokenInfo = fs.lstatSync(grant.token_file);
  if (!tokenInfo.isFile() || tokenInfo.isSymbolicLink() || (tokenInfo.mode & 0o077) || tokenInfo.uid !== process.getuid?.())
    throw new Error("private_mesh_token_required");
  const token = fs.readFileSync(grant.token_file, "utf8").trim();
  async function request(path: string, body: unknown) {
    const response = await fetch(new URL(path, target), {
      method: "POST", redirect: "error", signal: AbortSignal.timeout(15000),
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`mesh_authority_${response.status}`);
    return response.json();
  }
  // A lease gate, not a static command/task allowlist. A disconnected former
  // worker cannot start another native tool merely because its model is alive.
  pi.on("tool_call", async () => {
    await request("/v1/task/update", { id: grant.task_id, epoch: grant.epoch });
  });
  const definitions = [
    ["remember", "Save verified private owner preferences; no credentials.", Type.Object({ text: Type.String() })],
    ["recall", "Recall durable cross-device memory.", Type.Object({ query: Type.Optional(Type.String()) })],
    ["delegate", "Create a durable child task; reuse agent_id within project_id for native session continuity.", Type.Object({ input: Type.String(), role: Type.Optional(Type.String()), project_id: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String()), required: Type.Optional(Type.Array(Type.String())) })],
    ["children", "Inspect actual child task results.", Type.Object({})],
    ["wait_children", "Yield this turn; the ledger wakes the parent after children settle.", Type.Object({})],
    ["notify", "Queue an owner WeChat message. Queued is not verified delivery.", Type.Object({ text: Type.String() })],
  ] as const;
  for (const [action, description, parameters] of definitions) {
    pi.registerTool(defineTool({
      name: `mesh_${action}`, label: `Mesh ${action}`, description, parameters,
      async execute(callId, arguments_) {
        const data = await request("/v1/agent/action", {
          task_id: grant.task_id, epoch: grant.epoch,
          call_id: `${grant.task_id}:${callId}`, action, arguments: arguments_,
        });
        return { content: [{ type: "text", text: JSON.stringify(data) }], details: data };
      },
    }));
  }
}
