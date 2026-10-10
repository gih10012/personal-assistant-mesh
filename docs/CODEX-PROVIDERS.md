# Codex 显式 Responses provider（PAM-010b）

2026-10-10 源码候选。默认仍用 `model_provider=openai` 与既有本人
ChatGPT 认证；**没有更换 Leader、复制其长记忆、创建 key、开账单或
调用第三方模型**。新配置不改变宿主 Shell、网络、文件或 MCP 权限。

## 最薄配置

worker 的 `codex` 或 `codex_accounts` 某一项可显式给出：

```json
{
  "model_provider": "owner-responses",
  "model": "actual-model-id",
  "auth_home": "/private/isolated-codex-profile",
  "provider_file": "/private/responses-provider.json",
  "provider_env_file": "/private/responses-env.json",
  "cost_policy": "free_api",
  "cost_contract": "/private/free-api-contract.json"
}
```

这些文件不进公仓。`auth_home` 必须显式指定本人持有的私有真实目录；
metadata/env/成本文件由既有 `private_json` 校验本人持有、私有普通
文件、非 symlink。provider JSON 只允许下列 metadata：

```json
{
  "name": "Owner Responses route",
  "base_url": "https://models.example/v1",
  "wire_api": "responses",
  "env_key": "MESH_PROVIDER_KEY",
  "env_http_headers": {"X-Owner": "MESH_PROVIDER_OWNER"},
  "requires_openai_auth": false
}
```

`provider_env_file` 只可赋值这些显式引用的变量，也可使用已授权进程
环境里的同名变量。secret 仅进入该子进程环境，不进入 CLI、公开诊断
或 provider identity。metadata 可进入 native 配置，故 URL/名称不要
包含 secret。endpoint 不接受 userinfo/query/fragment；HTTP 仅 loopback。
不接受 inline bearer/header 值、特殊内建 provider 或 Messages/chat
协议假名；command-backed auth/new upstream flags 留待实际需求适配。

非 OpenAI-auth route 不扫描/借用 ChatGPT 认证，不继承未被显式引用的
`OPENAI_API_KEY/CODEX_API_KEY`，不调用订阅额度接口。此时 preflight
`authenticated=true` 只是“显式配置准入”，同时
`live_auth_verified=false`，不是 API key 已可用的证明。

若既有合规 gateway 使用 ChatGPT auth，须显式
`requires_openai_auth=true`，与 `env_key` 互斥，并强制显式私有
`auth_home` 的 strict 认证；缺少认证不能扫描/fallback 到其他账号。
把普通 API route 标成订阅不是授权；不向任意第三方借出
OAuth。builtin `openai` 不接受本接口覆盖 provider URL/secret。
[官方 custom provider 合同](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)

## 成本与连续性

`local` 只接受 loopback endpoint；`existing_subscription` custom route
要求 OpenAI auth。`free_api` 与 Pi 共用私有成本声明：精确 provider、
`models`、未来 `expires_at`、`zero_cost=true`、`paid_fallback=false`、
`auto_reload=false`。`paid_api` 暂不接入，需预算/结算合同另做。

启动和每次可产生工作的 native RPC **实际发送前**重核声明，不自动
重试或降为付费。此声明不是上游账单保证、数据用途审查器或在途
generation/原生自动 goal 的逐 turn 费用 watchdog；实际免费/隐私/
硬消费上限由服务账户和下一片 PAM-010d 独立核验。

新 checkpoint 保存 provider ID+metadata 的 SHA identity。原 thread
只续接相同 provider identity；旧没有 identity 的 OpenAI thread 继续
兼容，但不得悄悄改到第三方。原生 memory/压缩与完整历史行为不变。
改变 endpoint/协议/认证引用不是切模型方便按钮；须单独授权与验证，
不能借换 provider、账户或 harness 重放 unknown。

## 实际验证与剩余

本机 `codex-cli 0.162.1` 已在全新私有空 profile、loopback 不监听
endpoint 下实际 `initialize → config/read → thread/loaded/list`：
custom provider 读回一致，loaded threads=0，退出 0、reader 回收。
只做这三个方法，**没有 thread/turn/goal/model 调用**。这证明真实
native 配置可加载，不证明 Responses stream、认证、tool roundtrip
或旧 VPS native 版本兼容；两端最终验证另记计划/任务证据。

`tests/test_codex_provider.py` 覆盖配置/凭据隔离、费用失效的发送边界
和同 provider 续接。PAM-010a 的统一实时目录/模型自主多候选路由、
010d 的无秘密真实工具/续接，以及 production 配置仍待接入。
Chat Completions/Claude Messages 优先由可选 [Pi](PI-PROVIDERS.md) 原生
协议承担；Responses gateway 须真正兼容，而非仅改 endpoint 名称。
