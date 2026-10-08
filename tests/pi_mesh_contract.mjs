// Execute the actual TypeScript extension with a registration-only Pi SDK shim.
// HTTP, fs/private-grant checks and route code are real; this is not a Pi model
// or installed-SDK/deployment acceptance test.
import fs from "node:fs";
import vm from "node:vm";
import { stripTypeScriptTypes } from "node:module";

const input = JSON.parse(fs.readFileSync(0, "utf8"));
const optional = Symbol("optional");
const Type = {
  String: () => ({ type: "string" }),
  Array: items => ({ type: "array", items }),
  Optional: value => ({ ...value, [optional]: true }),
  Object: (properties, options = {}) => ({ type: "object", properties,
    required: Object.keys(properties).filter(key => !properties[key][optional]), ...options }),
};
const tools = new Map();
const hooks = [];
const requests = [];
const nativeNames = ["bash", "read", "write", "network", "mcp_native"];
const pi = {
  registerTool(definition) {
    if (nativeNames.includes(definition.name)) throw new Error("native_tool_overwritten");
    if (tools.has(definition.name)) throw new Error("duplicate_mesh_tool");
    tools.set(definition.name, definition);
  },
  on(event, handler) { hooks.push({ event, handler }); },
};
const context = { fs, Type, defineTool: value => value, URL, AbortSignal,
  process: { env: { MESH_PI_GRANT_FILE: process.env.MESH_PI_GRANT_FILE }, getuid: process.getuid },
  fetch: async (url, options) => {
    requests.push({ path: new URL(url).pathname, method: options.method });
    return fetch(url, options);
  } };
let source = stripTypeScriptTypes(fs.readFileSync(input.extension, "utf8"), { mode: "strip" });
source = source.replace(/^import .+;\s*$/gm, "")
  .replace("export default function", "globalThis.meshExtension = function");
vm.runInNewContext(source, context, { filename: "pi-mesh.ts", timeout: 10000 });
const results = [];
let initializationError = null;
try {
  context.meshExtension(pi);
} catch (error) {
  initializationError = error.message;
}
if (!initializationError) {
  for (const call of input.calls || []) {
    try {
      if (nativeNames.includes(call.tool)) {
        // Simulate the native Pi event pipeline. A global extension hook would
        // make this path contact Mesh even though the native tool needs none.
        for (const hook of hooks.filter(item => item.event === "tool_call")) {
          await hook.handler({ toolName: call.tool, input: {}, toolCallId: call.callId });
        }
        results.push({ tool: call.tool, callId: call.callId, native: true, completed: true });
      } else {
        const definition = tools.get(call.tool);
        if (!definition) throw new Error("tool_not_registered");
        const result = await definition.execute(call.callId, call.arguments);
        results.push({ tool: call.tool, callId: call.callId, result });
      }
    } catch (error) {
      results.push({ tool: call.tool, callId: call.callId, thrown: true,
        error: error.message, status: error.status || null });
    }
  }
}
process.stdout.write(JSON.stringify({ initializationError, results, requests,
  hooks: hooks.map(item => item.event), nativeToolNames: nativeNames,
  tools: [...tools.values()].map(({ name, label, description, parameters }) =>
    ({ name, label, description, parameters })) }));
