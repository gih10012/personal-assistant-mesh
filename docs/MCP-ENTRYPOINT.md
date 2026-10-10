# 本地 Codex / MCP 薄入口（PAM-006c）

这是既有 owner-ingress 的 stdio 适配，不是新的 Leader、worker、host
沙箱或远程 ChatGPT 服务。三个工具为 `mesh_submit_task`、
`mesh_task_status`、`mesh_approved_result`。原生 Shell、文件、网络与其他
MCP 不拦截；权限仅约束此入口提供的 managed Mesh 能力。

## 实际协议与版本

2026-10-10 读取本机 `/usr/bin/codex --version` 为 `0.162.0`，并核对
[官方同 tag 协议模式源码](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/rmcp-client/src/protocol_mode.rs)：
默认 Legacy 使用 initialize，首选 `2025-06-18`；stdio modern 必须同时
启用 host 功能及 `CODEX_MCP_PROTOCOL_VERSION=2026-07-28` opt-in。
本插件不声明该环境标记，采用实际兼容路径。

本实现只支持 `2025-06-18`：initialize 总是返回此版本，其他提议得到
此兼容版本而非伪装支持；不能接受它的客户端须断开。支持 initialize、
`notifications/initialized`、ping、tools/list、tools/call；不支持
2026-07-28 discovery/现代请求生命周期、HTTP/SSE、sampling、elicitation、
resources、prompts、动态工具列表、进度、批量 JSON-RPC 或异步取消。
取消通知不会撤销已提交 Mesh 任务。stdio EOF 才结束此本地进程。

[MCP 2025-06-18 transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)
规定 UTF-8、换行分隔 JSON-RPC；stdout 仅协议消息。这里单行限 32768
bytes、回复限 65536 bytes、任务文本限 16384 UTF-8 bytes，字段/模式固定。
消息格式错误或超限终止该连接，不尝试重新同步再执行下一条。
[初始化与版本协商](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)。

## 权限和原任务身份

业务实现复用 `Ingress._submit_body`、现有 `Client`、
`native_messaging.dispatch/project_response/client_from_config`，与 Firefox
入口共享固定路由和 closed projection。模型参数不能选择 URL、配置路径、
bearer、role、subject、worker、租约或 native session；不提供 operator、
publish、任意 RPC/Shell、auth 扫描、注册或自动重试。project_id/agent_id
仅模型可读请求标签，不是授权。`_meta` 为有界协议元数据，忽略且不透传。

调用者先持久保存 request_id，再提交。同内容显式重查保持原 identity；
提交超时/失去回复/无效 authority reply 可能发生在 commit 之后，返回
unknown、`retry_with_new_id=false`。入口自身不重放、补偿、不生成新 ID；
应查原 request。新的独立任务允许显式新 ID，不因旧任务 unresolved
整体冻结。JSON-RPC id 只是 transport 回执 ID，不是业务 request_id。

状态仅 metadata。结果必须由已有 operator 在 authority 端审核并绑定原
task/epoch/result SHA 后发布；此插件只有读取能力。拒绝原始结果、
checkpoint、rollout、凭据、其他 subject 的 task、私有成果 URL，以及
错配 request/task。输出维持 `account_verified=false`、
`execution_verified=false`、`artifact_content_verified=false`；owner 审批
不是独立执行/内容核验。工具 annotations 是 hints，不是授权检查。

## 包与 owner 安装配置

`plugins/personal-assistant-mesh/` 是 Agent Plugins 1.0 portable 包：
root plugin.json 和 mcp.json；command 为 `python3`，实际 launcher
`${PLUGIN_ROOT}/server.py`，cwd `${PLUGIN_ROOT}`。没有新 Sites/hosting、
OpenAI API key、付费资源、hook、全局 native feature 配置或远程 app ID。
[官方 plugin 格式](https://developers.openai.com/plugins/build/plugins)；
同 tag 的 [portable manifest loader](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/core-plugins/src/agent_plugin_manifest.rs)
明确从 root `mcp.json` 加载服务器，并从 `extensions.com.openai` 读取展示
字段，因此当前无需另造 `.codex-plugin` / `.mcp.json` 兼容覆盖。
这是本地源码包，未上传公共插件目录；public GitHub 源码发布与插件目录
发布不是同一件事。

Launcher 默认读取 owner 的
`$XDG_CONFIG_HOME/personal-assistant-mesh/mcp-launch.json`（未设 XDG 时
`~/.config/personal-assistant-mesh/mcp-launch.json`）。owner 在安装期可通过
`PERSONAL_ASSISTANT_MESH_PLUGIN_SETTINGS` 指向同样私有的文件；这不是
tool 参数。示例仅说明形状，不含可用机器标识、密码或 grant：

```json
{
  "runtime_root": "/absolute/owner-installed/personal-assistant-mesh",
  "client_config": "/absolute/private-owner-directory/ingress-client.json"
}
```

私有 client 为既有 `{ "control_url": "http://127.0.0.1:PORT",
"token_file": "/absolute/private-owner-directory/ingress.token" }`，可按既有
Client 合同配置 Unix socket 或 TLS authority。只用此入口专用的 ingress
bearer，不复用 operator/worker/OpenAI credential。server 必须已把它绑定
owner/source/subject/scopes；本 helper 不能仅凭本地文件证明 server role。

所有私有设置/client/token 文件须 owner-owned、0600、普通单链接文件，
直接 parent owner-owned 0700；拒绝路径 symlink、不可信可写 ancestor、
相对路径、`..`、超限、重复键。不自动 chmod、创建或修复。运行期 root
及 ancestors 为可信 owner/root-owned 非共享可写目录，不能是 symlink。
owner 安装选择实际 repo/runtime 位置，缓存复制包不靠 `../../` 猜仓库。
插件源码不包含机器配置或秘密，也不会扫描 `.codex` 等账号目录。

本地插件需 host 支持本地进程和 python3，当前实际测试为本机 Python 3.14；
代码保留 Python 3.6 语法兼容但不是 VPS 上实际插件安装证据。POSIX 私有
文件权限/`O_NOFOLLOW` 是此 launcher 的实际环境要求，Windows/手机由
节点 agent 按稳定 ingress 合同适配，不伪称这份 Linux launcher 已兼容。
离线 child initialize/tools/list 不发网络请求，不修改 Codex auth/config。

## 验收边界

`python3 -m unittest tests.test_mcp_entrypoint` 覆盖协议、字段、权限投影、
unknown 不重放、无凭据反射、实际 stdio child 与 portable manifest。
child 使用临时 dummy ingress config，只有 initialize/tools/list/ping；
不能将其称为真实 Mesh task 回路或 Codex 插件已安装。

2026-10-10 已按 owner 授权在本机一个既有 Codex profile 中通过官方
`codex mcp add/get` 配置 stdio server；未上传或安装 portable 插件目录包。
实际 native app-server 发现三个工具，随后真实调用提交、状态及批准成果，
不是 mock，也不是另建 Leader。稳定请求
`PAM-006c-codex-local-handoff-20261010-v1` 的任务 completed epoch 1；
业务仍在原 Leader thread，Root 审查摘要以
`PAM-006c-codex-local-result-20261010-v1` 发布，实际原生 MCP 读回
task/request/publication 精确绑定的成果。没有请求验证客户端的模型 turn；
因此也不把这个验收说成“模型已自主选择调用这三个 MCP 工具”。

初次空验证线程退出后，实际 `thread/resume` 返回 no rollout found；
没有模型 turn 的空线程尚无可恢复的 rollout。保留旧 ID/回执，并确认
业务 submit 意图尚不存在后，显式改用一个在同一 app-server 连接中保持
存活的 ephemeral transport，业务 request 不变；它不是 Leader、记忆迁移
或未知效果重放。验证客户端随后正常关闭，持久业务与成果仍在 Mesh。
[官方 app-server](https://learn.chatgpt.com/docs/app-server) 区分 stored session
resume、in-memory ephemeral thread 和 `mcpServer/tool/call`。

普通 Chat/Work/@163 account、Codex Cloud enrollment 或
“无限推理”仍须分别实际核验，不能从本地 stdio 包推导。
