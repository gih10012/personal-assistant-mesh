# Pi 多协议模型入口（PAM-010c）

这是源码候选，未部署、未创建 key/账单、未调用第三方模型。Pi 是可选
harness，不替换 Codex Leader，也不把 Codex JSONL 转成 Pi session。

## 私有配置

保留原 `workspace/session_dir/provider/model/cost_policy`，新增可选
`agent_dir` 与 `models_file`。`agent_dir` 必须是本人持有、无组/其他用户
权限的真实目录；内有 `models.json/auth.json` 时文件也必须本人持有且
无组/其他用户权限、非 symlink/硬链接。路径祖先不能被其他用户写入
（系统 sticky 临时目录例外）。这些是当前 POSIX host 的配置保护，
不是所有 Windows/手机节点必须照抄的协议要求。

adapter 仅给自己的 Pi 子进程设置 `PI_CODING_AGENT_DIR`，不更改当前
Leader 的环境、系统 HOME、全局 Pi profile 或 native 工具权限。
`models_file` 如指定，必须正好是 `<agent_dir>/models.json`；Pi 没有
单独的任意 models 文件 CLI flag，所以不假造参数，不复制/覆盖配置。
adapter 只检查其私有文件属性，不解析/打印认证；实际协议与 key 的
解析仍由 Pi 原生处理。

例如，仓库外的私有 worker JSON：

```json
{
  "pi": {
    "executable": "/private/installed-pi",
    "workspace": "/private/project",
    "session_dir": "/private/pi-sessions",
    "agent_dir": "/private/pi-profile",
    "models_file": "/private/pi-profile/models.json",
    "provider": "owner-gateway",
    "model": "actual-model-id",
    "cost_policy": "free_api",
    "cost_contract": "/private/free-api-contract.json"
  }
}
```

Pi 原生 models.json 支持 `baseUrl/api/models` 和私有 key 环境引用。
Responses 用 `openai-responses`，Chat Completions 用
`openai-completions`，Claude Messages 用 `anthropic-messages`。不以模型
名/alias 猜协议，不自动借用其他 harness 的 OAuth 文件。
[上游 model 配置](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/models.md)、
[配置目录](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/configuration.md)

## 成本合同

- `existing_subscription/local` 继续兼容既有 owner 配置；这只是 owner
  声明，不能把 API 路由包装为订阅或证明 live 成本。
- `free_api` 必须隔离 `agent_dir`，并引用仓库外 0600 JSON 的
  `cost_contract`。共享 validator 核对 provider、明确 models、未来
  `expires_at`、`zero_cost=true`、`paid_fallback=false`、
  `auto_reload=false`。不是看到 `-free` 后缀就自动授权。
- `paid_api` 当前拒绝；需要已有明确预算/用量结算合同后另实现，
  一个布尔值或自行改成 `existing_subscription` 不是授权。

启动前、native `set_model` 前/回执后、原 session 恢复读取实际 model
后，以及每次 `prompt` 前都重新核对。合同声明不是上游账单保证；
服务限时/隐私用途另核实，Root/调用者不应把完整私人记忆发送给训练
用途服务，也不能将此校验当内容脱敏器或上游 auto-reload 管理器。

## 目录、选择和连续性

`Pi.models()` 使用原生 `get_available_models`，只投影模型 ID/provider、
API、context、reasoning/input、**declared_cost** 等 metadata；不输出
endpoint/headers/key/auth。结果标注 `pi_configured_catalog`、
`live_auth_verified=false`、`inference_verified=false`。目录可见、零价
自报、认证/真实推理/工具能力分别核验。

`Pi.select_model(provider, model_id)` 使用原生 `set_model`，不 prompt、
不新建/fork session、不覆盖历史。只在本 adapter 未启动工作或收到
自然 `agent_settled` 后允许；native streaming/compaction/排队中拒绝。
set_model 发送后成本失效、回执不同或不明确即保留不确定性，不用另一次 set/prompt
重试来“修复”。一个 Pi 实例绑定一个 owner provider route：显式
`provider` 不能跨 provider；legacy 未配置时从实际 `get_state.model`
锁定 provider。需要另一个服务时配置其已授权 route，不把旧长期
session 自动搬过去。调用者仍负责隐私/成本范围，目录可见不等于
所有 provider 的执行许可。

`start(text, checkpoint)` 继续用原 `pi_session_file` 的 `switch_session`；
恢复时检查实际 native model，不偷偷换回 CLI 默认模型。不受当前
合同覆盖时停在 prompt 前，由 owner/Leader 按原身份协调。prompt 在
发送前先标记尝试；未知 ACK/错误/超时不能在这个 adapter 上再 prompt
或切模型重做。跨进程 task/unknown 仍由原 Worker authority 保持，
不能靠重新实例化 adapter 绕过它。

[原生 RPC model/session 合同](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/rpc-commands.md)、
[settled 生命周期](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/rpc.md)

## 验证范围

`tests/test_pi_provider.py` 用合成 profile/合同及 mock RPC 覆盖私有
路径、子进程环境隔离、成本失效/付费拒绝、安全目录投影、真实 RPC
字段、原 session 连续性及 unknown 不重发。它不是 Pi 安装、账户
认证、第三方实际模型调用或端到端工具续接验收；PAM-010d 另验。
