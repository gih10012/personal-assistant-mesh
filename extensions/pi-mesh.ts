import fs from "node:fs";
import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

class MeshAuthorityError extends Error {
  status: number;
  constructor(status: number) {
    super(`mesh_authority_${status}`);
    this.status = status;
  }
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function result(data: unknown, success = true) {
  return { content: [{ type: "text" as const, text: JSON.stringify(data) }],
    details: data, ...(success ? {} : { isError: true }) };
}

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
  if (target.username || target.password || target.search || target.hash)
    throw new Error("invalid_mesh_control_url");
  const tokenInfo = fs.lstatSync(grant.token_file);
  if (!tokenInfo.isFile() || tokenInfo.isSymbolicLink() || (tokenInfo.mode & 0o077) || tokenInfo.uid !== process.getuid?.())
    throw new Error("private_mesh_token_required");
  const token = fs.readFileSync(grant.token_file, "utf8").trim();
  if (token.length < 32) throw new Error("weak_mesh_control_token");
  async function request(path: string, body: unknown) {
    const response = await fetch(new URL(target.pathname.replace(/\/$/, "") + path, target.origin), {
      method: "POST", redirect: "error", signal: AbortSignal.timeout(15000),
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new MeshAuthorityError(response.status);
    return response.json();
  }
  // Fence only this extension's extra mesh entrance. Never subscribe to the
  // global tool_call event: native shell/files/network/MCP do not depend on
  // mesh reachability, enrolment or a mesh grant to run.
  async function dispatch(action: string, arguments_: Record<string, unknown>, callId: string) {
    await request("/v1/task/update", { id: grant.task_id, epoch: grant.epoch });
    let path = "/v1/agent/action";
    let body: unknown = { task_id: grant.task_id, epoch: grant.epoch,
      call_id: `${grant.task_id}:${callId}`, action, arguments: arguments_ };
    let rejected = "mesh_coordination_rejected";
    let resource = false;
    if (action === "remote_delegate") {
      const { peer, ...nested } = arguments_;
      if (typeof peer !== "string" || !peer)
        return result({ success: false, error: "remote_delegation_peer_required" }, false);
      path = "/v1/mesh/delegate";
      body = { task_id: grant.task_id, epoch: grant.epoch,
        call_id: `${grant.task_id}:${callId}`, peer, arguments: nested };
      rejected = "remote_delegation_rejected";
    } else if (!["delegate", "children", "wait_children", "remember", "recall", "notify"].includes(action)) {
      path = "/v1/resource/action";
      body = action === "resource" ? arguments_ : { action, arguments: arguments_ };
      rejected = "resource_authority_rejected";
      resource = true;
    }
    try {
      return result(await request(path, body));
    } catch (error) {
      if (!(error instanceof MeshAuthorityError)
          || !(resource ? [400, 403, 404, 409] : [400, 403]).includes(error.status))
        throw error; // stale task/remote lease and uncertain transport never become success
      return result({ success: false,
        error: resource && error.status === 404 ? "resource_authority_upgrade_required" : rejected,
        http_status: error.status }, false);
    }
  }
  const openArguments = Type.Object({}, { additionalProperties: true });
  pi.registerTool(defineTool({
    name: "mesh", label: "Mesh capability entrance",
    description: "Additional mesh capability/coordination entrance, not a native tool gate or network proxy. "
      + "Never intercepts native shell, files, network or MCP; unregistered native tools remain available. "
      + "discover/describe/graph/audit and resource API actions use the open capability directory; "
      + "resource accepts {action:resourceAction,arguments:{...}}. Capability kinds are unrestricted. "
      + "remote_delegate accepts {peer,input,project_id,agent_id,role} for an owner-enrolled mesh peer; "
      + "delegate/children/wait_children use the existing fenced child-task ledger; yield after wait_children. "
      + "remember/recall/notify retain their original contracts. Identity and task lease are host-bound; "
      + "directory metadata, authorize.allowed and queued are not completed execution. Capabilities without "
      + "an execution adapter are not automatically invoked. Mesh failure never gates other authorized native routes.",
    parameters: Type.Object({ action: Type.String(), arguments: Type.Optional(openArguments) },
      { additionalProperties: false }),
    async execute(callId, arguments_) {
      const input: unknown = arguments_;
      if (!object(input) || Object.keys(input).some(key => !["action", "arguments"].includes(key))
          || typeof input.action !== "string" || !input.action
          || (input.arguments !== undefined && !object(input.arguments)))
        return result({ success: false, error: "invalid_mesh_gateway_arguments" }, false);
      return dispatch(input.action, (input.arguments || {}) as Record<string, unknown>, callId);
    },
  }));
  const definitions = [
    ["remember", "Save verified private owner preferences; no credentials.", Type.Object({ text: Type.String() })],
    ["recall", "Recall durable cross-device memory.", Type.Object({ query: Type.Optional(Type.String()) })],
    ["delegate", "Create a durable child task; reuse agent_id within project_id for native session continuity.", Type.Object({ input: Type.String(), role: Type.Optional(Type.String()), project_id: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String()), required: Type.Optional(Type.Array(Type.String())) })],
    ["children", "Inspect actual child task results.", Type.Object({})],
    ["wait_children", "Yield this turn; the ledger wakes the parent after children settle.", Type.Object({})],
    ["notify", "Queue an owner WeChat message. Queued is not verified delivery.", Type.Object({ text: Type.String() })],
    ["remote_delegate", "Delegate to an owner-enrolled autonomous mesh peer; queued is not completion. Native SSH/network routes are not gated by this tool.", Type.Object({ peer: Type.String(), input: Type.String(), role: Type.Optional(Type.String()), project_id: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String()) })],
    ["resource", "Open capability directory and exact mesh-only resource grants; not a native toolbox allowlist or execution adapter.", Type.Object({ action: Type.String(), arguments: Type.Optional(openArguments) })],
  ] as const;
  for (const [action, description, parameters] of definitions) {
    pi.registerTool(defineTool({
      name: `mesh_${action}`, label: `Mesh ${action}`, description, parameters,
      async execute(callId, arguments_) {
        return dispatch(action, arguments_ as Record<string, unknown>, callId);
      },
    }));
  }
}
