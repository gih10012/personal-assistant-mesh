# Codex/Pi 多模型接入研究

2026-10-10 核验官方文档和 Pi 上游
`c5f5b3282d5e4203c085e59837ba17aeaf2829b5`。未创建 key、开账单或调用
第三方模型；协议可接不代表当前 Mesh/账户已接通。

| 服务实际协议 | Codex harness | Pi harness |
| --- | --- | --- |
| Responses | custom provider，须完整兼容 | 原生 openai-responses |
| Chat Completions | 当前不直接支持，需合格 Responses gateway | 原生 openai-completions |
| Claude/中转 Anthropic Messages | 不可直接当 Responses；需独立桥接 | 原生 anthropic-messages |
| 自定义协议/认证 | gateway/runtime 适配需实测 | provider extension |

Codex 使用 model_provider/provider/base_url/私有 credential 引用及
wire_api=responses；核对部署 CLI 版本，不拿最新文档当旧版本验收。
仅返回文字不够：SSE 完成事件、function call ID/工具结果往返、历史
续接和真实 context/tools/reasoning metadata 都需正确。
[官方 provider](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)、
[模型协议](https://learn.chatgpt.com/docs/models#other-models)、
[gateway 合同](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)

Pi 私有 models.json 可指定 provider URL、key 引用、api 与模型；
extension 可注册自定义流/认证。上游已迁至 earendil-works/pi。Pi 可
同 session 换模型，不意味着无损读取 Codex JSONL；原 Leader 历史留在
原 harness。[models](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/models.md)、
[extensions](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/custom-provider.md)、
[sessions](https://github.com/earendil-works/pi/blob/c5f5b3282d5e4203c085e59837ba17aeaf2829b5/packages/coding-agent/docs/sessions.md)

## OpenCode 服务可用，不更换 Leader

Zen 官方允许其他 coding agent 使用 API。多数免费项走 Chat
Completions；`muse-spark-1.3-contributor-free` 列为 Responses，是 Codex
候选但仍需工具/续接实测；Claude 项走 Messages，Pi 接入路径较短。
免费有时效，账户/key/限流/费用单独确认。部分免费服务会使用输入/
输出改进或训练；Muse Contributor 明确涉及训练用途。不默认传送
完整 Leader 记忆、个人信息或秘密。自动充值可能新增费用，本次不开启。
[Zen endpoints、pricing、privacy](https://opencode.ai/docs/zen/)

使用 OpenCode 模型服务不等于改用其 agent harness，不伪造客户端/
session 来源，不把模型 alias 当兼容性。未来 Claude 中转看真实
协议、数据和费用条款，不凭“兼容”宣传。

## 初始源码差距与 PAM-010

以下是本次实施前的审计，保留为起点而非最新部署状态。现在已有
[Codex custom provider](CODEX-PROVIDERS.md)、[Pi 隔离 provider](PI-PROVIDERS.md)
源码候选与成本声明/续接保护；未部署、未验第三方推理。统一实时
目录与多候选模型路由仍待 010a，完整工具往返仍待 010d。

- codex.py 写死 model_provider=openai，认证 discovery 只针对 ChatGPT。
- pi.py 已传 provider/model，缺隔离 agent_dir/endpoint 与真实认证
  合同；cost_policy 只有 existing_subscription/local，不能把免费
  API/付费中转谎报成既有订阅。目录可见不等于认证成功。
- Worker 目前只有 Codex 账户链后一个 Pi 候选，尚无多 provider
  模型自主选择入口。已起效/unknown turn 不跨模型/harness 重放。

实施：登记 harness/protocol/model/context/tools/cost/privacy/availability
与时效，credential 引用仓库外；Codex 放开显式 Responses provider，
Pi 加隔离配置/目录/原 session 续接。先合成无秘密任务验证 stream→
工具调用→真实结果→原 session 续接→产物，再做中断/429/认证拒绝
等风险相称验证。无固定模型排行榜；新授权/费用另确认，免费也核对
数据用途，不默认上传个人长记忆。

模型 gateway 不代理 Shell/MCP/浏览器全部网络，VPS 通用出口另做。
ChatGPT 登录不是万能 API 凭据；参与第三方应用使用计划也不意味着
新增无限额度或可读取聊天记忆。
[官方 Sign in with ChatGPT](https://learn.chatgpt.com/docs/sign-in-with-chatgpt)
