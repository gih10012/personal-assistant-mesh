# Codex/Pi 多模型接入研究

## 本次续接：现有认证线索与官方计划内推理候选

Root再次官方docs搜索→实际fetch Other models及SIWC，读取Pi固定
`ea448f454af474f3e2fe9733b331e17b0662f5d6` models/providers/virtual-models
原文与Zen当前端点/价格/隐私。Codex custom仍需要兼容Responses；
Pi可原生接Chat Completions/Anthropic Messages，OpenCode只是API
provider，不换Leader harness。[Other models](https://learn.chatgpt.com/docs/models#other-models)、
[Pi models](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/docs/models.md)

实际本机已有OpenCode provider私有认证记录，仅读provider/type/引用
存在性，未输出或复制key，未调用模型；ambient key未设，Pi目录和
binary未安装。因此下一步优先复用既有私有引用，不先要求新key，
但不能把存在性叫有效认证。先一个已授权免费物理模型合成无秘密
工具/原session续接/完整产物，然后再接原生virtual routing；免费
alias不能授权实际paid dispatch，原Leader长期历史不外传。Zen有
限时免费与训练用途例外，且auto-reload可能充值，不能宣称无限免费。
[Zen端点/价格/隐私](https://opencode.ai/docs/zen/)、
[Pi providers](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/docs/providers.md)

SIWC官方已有合格Plus/Pro的计划内推理路径，app请求独立consent。
消耗既有Codex/Work usage，app weekly cap不是新增额度，且不给
Chat conversations/memories访问。这是候选“计划内Responses推理
能力”，与Chat模型调用Mesh MCP的入口方向不同；不是任意第三方
key、普通Chat无限API或已经安装成功。本次只读研究，无新认证/开支。
[Sign in with ChatGPT](https://learn.chatgpt.com/docs/sign-in-with-chatgpt)

## 11:42 补充：复用原生机制，不先造第二套harness

两名并行专员分别核对协议/路由与Mesh最小衔接，Root再次实际fetch
官方配置/Other models/Bedrock并读取Pi固定commit和Zen原文。只读
研究，没有安装升级、创建key、开账单、调用第三方模型或上传长记忆。

最新Bedrock专页区分`amazon-bedrock-runtime`（Runtime/CRIS）与
`amazon-bedrock`（Mantle）；它们是AWS认证的支持OpenAI模型路线，
不是任意Claude/非Responses协议例外。Runtime不支持hosted web
search。这些是当前上游文档，不证明现部署0.162.1/0.159.2已支持
新内置provider，也不证明官方Codex Cloud可使用该本地配置。
[官方Bedrock](https://learn.chatgpt.com/docs/amazon-bedrock)

Pi固定HEAD `ea448f454af474f3e2fe9733b331e17b0662f5d6`已经有
OpenCode原生provider/`OPENCODE_API_KEY`，可先复用，不必运行OpenCode
agent或先写协议extension。该版本还有`registerVirtualModel`：每次
请求选物理模型、记录实际provider/model，route state随原session
分支持久保存且跨compaction；tool continuation/retry可sticky到
previous/failed物理模型。这贴合能力化/动态选模方向，不能从读到
上游功能推断现有Mesh已装好或已跑通。
[providers](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/docs/providers.md)、
[virtual models](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/docs/virtual-models.md)

现Mesh Pi adapter只在同provider内set_model，费用合同核对selected
provider/model；将virtual alias声明免费不能授权其实际paid physical
dispatch。下一片先隔离已有授权的单个免费物理模型，完整tool结果/
原session续接/产物验收；之后把实际dispatch/auth/cost/privacy/时效
接到原生router，不永久锁模型，也不构造另一套transcript替代Pi。
Mesh费用合同不改变用户直接运行原生Pi/Shell的能力。

Codex gateway仍需实际alias/catalog及stream/tool/harness续接验收；
不把HTTP文本或模型自报当上游路由证明。认证、transport和部署版本
分别核对，不能把所有HTTP续接都强制成WebSocket协议。免费Zen
Chat Completions候选优先Pi；Claude Messages中转也优先Pi原生协议。
Muse Contributor训练用途仍排除默认私有长记忆传送，关闭自动充值
并核对实际账户/key/限流。没有凭据时只报告缺项，不生成新key。
[gateway](https://learn.chatgpt.com/docs/enterprise/gateway-compatibility)、
[Zen](https://opencode.ai/docs/zen/)

## 2026-10-11 primary 复核与实施结论

11:25再次按官方docs搜索→实际fetch核对custom provider/Other models，
并读取Zen当前endpoint/pricing/privacy与Pi固定HEAD原文。当前Codex
官方明确custom gateway需Responses，不支持`wire_api="chat"`或
Chat-Completions-only端点；built-in AWS Bedrock路由单列，不能泛化成
任意Claude中转已经兼容。结论与09:05研究一致，本次只读核对未创建
第三方key、充值或模型turn。[Other models](https://learn.chatgpt.com/docs/models#other-models)

Leader继续原生Codex；Pi作为可选多协议工作harness。接OpenCode Zen的
模型API不要求运行OpenCode agent。现有custom route/已实测配置版本
0.162.1和0.159.2只验了Responses；不能从最新网页倒推旧binary能力。
当前官方配置另有built-in `amazon-bedrock`，专页另列`amazon-bedrock-runtime`；
这是独立AWS认证的支持OpenAI模型路线，不是现有任意Claude中转已经
兼容，也不能从文档推断部署binary或未来所有协议能力。
[官方配置](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)、
[Bedrock](https://learn.chatgpt.com/docs/amazon-bedrock)

| 服务协议 | 本项目下一实际路径 |
| --- | --- |
| Responses | 原生Codex custom provider；验证完整stream/tool IDs/result/原thread |
| Chat Completions | Pi `openai-completions`，Codex需真实兼容gateway |
| Anthropic Messages / Claude中转 | Pi `anthropic-messages`，按真实base URL/auth核对 |
| 非标准协议 | Pi provider extension或合格gateway，不能只改model alias |

Zen primary 页10月10日更新，允许其他coding harness使用；当前
Space Bunny/LongCat/Step免费路由为Chat Completions，页面声明限时
免费、zero-retention/不训练。Muse Contributor为Responses，但隐私条款
明确允许训练；它不是默认传送Leader记忆的安全兜底。免费表/名称仍需
现有账户key、期限、限流与硬零新增支出实核，不开auto-reload。
[Endpoints/pricing/privacy](https://opencode.ai/docs/zen/)

Pi新核对上游HEAD `ea448f454af474f3e2fe9733b331e17b0662f5d6`；现有
研究pin继续保留，未升级/安装。该HEAD和原pin的models.json显式env值
使用`$NAME`/`${NAME}`，裸`NAME`是literal，不把旧示例的裸key-name
照搬；provider ambient env auth另核。Mesh adapter不解析/输出secret，
由native配置读取。[models](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/docs/models.md)、
[resolver](https://github.com/earendil-works/pi/blob/ea448f454af474f3e2fe9733b331e17b0662f5d6/packages/coding-agent/src/core/resolve-config-value.ts)

最小落地：已有授权route/私有认证引用→provider/model/protocol动态候选→
已授权的VPS路径→合成无秘密tool往返→原session续接→产物读回。模型
按当前可用性/任务/性能选择，不固定排行；上下文/工具/SSE不兼容的
模型不能仅因提供HTTP文本就称完整harness兼容。Codex与Pi各保留原
历史；跨provider/harness无损记忆另证，不复制完整Codex JSONL发给
免费服务。当前源码候选及零模型配置验收不升级为第三方推理成功；
`paid_api`尚未开放，未来付费Claude中转需预算/结算接入，非永久禁用。

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
