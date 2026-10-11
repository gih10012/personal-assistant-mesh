# Pi 实际免费端点验证（PAM-010d）

2026-10-11 17:46（Asia/Shanghai）终态：**一次真实请求被免费层客户端限制拒绝**。
固定 Pi 1.1.0 的原生 `opencode/longcat-2.5-preview-free` 经本人既有 VPS
SSH SOCKS5h 路径，实际收到 HTTP 403、`FreeTierError`；脱敏分类为
`FREE_TIER_CLIENT_RESTRICTION`。TLS 校验结果 0，curl 正常退出。Pi
自然 `agent_settled`，最终 assistant `stopReason=error`。

模型 POST 总数 **1**；工具执行 **0**；成功产物 **0**；第二轮工具任务
**未提交**。认证成功、推理、工具往返和成功会话续接均未验收。保留同一
synthetic trial 身份和原 Pi session，未再次请求、切模型/出口或采用付费路线。

## 版本、端点与成本声明

采用已发布的 `@earendil-works/pi-coding-agent@1.1.0`；实际包 metadata 和
自带 bundled CLI `--version` 都读回 1.1.0。官方包要求 Node ≥22.19，
本机 Node 26.8.2。该版本文档明确原生 OpenCode provider 使用
`OPENCODE_API_KEY`；本次仅在私有进程中引用已有 key，没有创建凭据。
[Pi v1.1.0 package](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/package.json)、
[Pi providers](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/providers.md)

物理请求选择是 `longcat-2.5-preview-free`，API 为 `openai-completions`，
实际 POST URL 是 `https://opencode.ai/zen/v1/chat/completions`。未使用
virtual router。Pi 原生目录 metadata 为 reasoning=true、contextWindow
1,000,000、maxTokens 131,072；本次 Pi thinking=`off`，实际请求未含
`reasoning_effort` 或 `enable_thinking`，输出上限 2,048。这不证明服务端
关闭内部 reasoning，也不等同于某个 AA 已测 effort。

请求前再次核对的 Zen 官方页面将该端点列为 Chat Completions，输入、
输出、缓存读取均 Free，并单独声明 LongCat 2.5 Preview Free 限时免费、
零留存、不用于训练。Pi 原生目录 input/output/cacheRead/cacheWrite
声明都为 0。上述是当前 provider 声明；本次没有实际 token generation
或独立账单结算证据。[Zen endpoints/pricing/privacy](https://opencode.ai/docs/zen/)

## 原失败记录与实际执行

| 阶段 | 实际结果 | 模型 POST / 工具 |
| --- | --- | --- |
| 第一次私有 prefix 安装 | 命令句柄失效、PID 消失，目录仅部分下载；没有安装成功/退出回执 | 0 / 0 |
| 同 prefix 固定版本有界补完 | `ignore-scripts`、无 audit/fund，240s 加 5s 回收边界；最终 exit 137，package-lock 和正常 bin link 缺失 | 0 / 0 |
| SDK 本地 bootstrap | native model/read/write 初始化；本地 `Agent.setTools` 调用不兼容（`LOCAL_AGENT_TOOL_GUARD_API_MISMATCH`），在 `prompt` 前失败；无持久 session 文件 | 0 / 0 |
| 原生 unbundled SDK 合成 prompt | deferred OpenAI provider 因缺失 `openai/index.mjs` 终结（`NATIVE_DEPENDENCY_MISSING`）；保留实际 session/error | 0 / 0 |
| 同原 session 的新明确 synthetic fixture 步骤 | 已发布包自带 bundled SDK 和 OpenAI protocol chunk 实际 import 成功；一次请求返回 HTTP 403 / FreeTierError | 1 / 0 |

同一已持久 session 共接纳两次合成 prompt：前一次在本地 dependency
import 失败，后一次是明确的新 fixture 步骤、唯一一次模型 POST。整体
npm 安装仍记为 **incomplete**，没有第三次 installer。可执行 bundle
来自同一已下载 1.1.0 包，没有改写 package 或正式 Mesh 源码。SDK import
及协议 chunk import 成功是有限 runtime 证据，不能写成推理成功。

真实 fixture 只涉及三个私有 scratch 文件：读取合成整数、保存排序和求和
结果、读回；计划的后续任务从原 user message 恢复 synthetic marker，
保存第二个结果并读回。Loader 显式为空，没有项目/Leader 的 AGENTS、
skills、历史、goal 或聊天输入；只有 Pi 原生 read/write 工具，并对本次
synthetic scratch 路径增加局部执行前检查。agent/provider retry、自动
compaction 和 cache warming 均关闭。免费端点拒绝后未提交后续任务。

## 路由、终态与连续性边界

原 owner-private egress 配置经 UID/mode 校验及 `ssh -G` 检查，无继承
forward；解析的 host/user 与既有文档中的本人 VPS 一致。每次 runtime
仅持有自己新开的短期 SSH 进程。实际模型请求由私有、显式 fetch adapter
交给 curl 的 SOCKS5h transport；curl 返回 HTTP/TLS 元数据后交由 **Pi
原生 OpenAI streaming parser 和 agent loop** 处理。key 只在私有内存和
curl stdin 配置管道中，未进入 argv、提示词、仓库或报告。

因此可核验的是本次明确选择的 owner VPS SSH 路径和真实 TLS/HTTP
结果，验收范围是 **transport-only POST 403**；不能写成 working model、
harness/tool loop 验收。没有把“设置 proxy 环境变量”当作 SDK 已遵循路由，也没有独立测定
远端公网源 IP。原生 Pi 无此 adapter 时的代理行为尚未验证。没有全局
网络修改、生产 service/receiver 切换、账户/账单/auto-reload 修改。

403 的正文为 JSON，安全 `error.type=FreeTierError`，没有单独 error.code；
私有只读分类匹配客户端使用限制，未匹配额度/余额/账号/key/地区原因。
没有 Cloudflare challenge HTML。响应 content-type header 未单独持久化，
不补发请求求取它。官方仓库 collaborator 的说明也明确免费层限制其他
harness，这与本次实测一致；该政策说明不能反推本 key 的认证或余额已经
成功验证。[OpenCode collaborator clarification](https://github.com/anomalyco/opencode/issues/49621)

原 Pi session 标识公开脱敏为 `01a12a58…5ba4`，完整标识和文件只留私有
记录。bundled fixture 实际打开原文件并恢复 3 条消息；原 3,563B 完整
前缀 hash 与当前相同长度前缀独立核对一致，终态 session 为 4,886B、
owner-only 0600、单硬链接。
这证明 **失败历史的原 session 保持与重开**；第二个模型工具任务未执行，
不能称工具/产物续接完成。所有 owned SSH/Pi/curl 已终结并回收；最后
核验对应 PID 均不存在。既有 key 文件字节未变，28 个本次私有非-package
文件扫描中 key 出现数为 0。

| 私有实际记录 | 字节 | SHA-256 |
| --- | ---: | --- |
| 原 session 前缀 | 3,563 | `b0478c65e9fcf8f17ab4ed26d672689c6b8c59c20051efa84ab6a037ec8137d9` |
| 原 session 终态 | 4,886 | `ac91556003ba08ee0e06ba33dc9c748b04e668ab359475ff77c5227de243914e` |
| 唯一模型请求 body（合成内容） | 2,913 | `abed33d6a2c2ef80e31a48a6b36dec984bbd2062221eb45fc4059a0d6ab82a48` |
| 唯一 HTTP 403 body | 120 | `557188636950dd0930896ff8ebf863167d0c8977a049fd3785fe818e41a03578` |
| bundled 原生 event journal | 4,130 | `ce0ebce1372dfa360bef7d6c6dbf500764d12e08d9882acc6f7bd67607b0ec22` |

报告不公开私有路径、凭据、body/error 片段或完整 native journal。

## 后续候选的判断

LongCat 的本次 exact variant/effort 尚无匹配 AA Intelligence Index
证据，分数记 **unknown**，不能借 Step、其他 LongCat 或相似名字代填。
owner 要求手动比较 AA 原始成本点与本人的实际免费/订阅增量成本；若
variant 和 effort 相同且实际路线可用，可把真实费用 0 独立纳入判断。
本次没有把免费模型一概标为 paid-frontier 以下，也没有证明它已可用。

`step-5-preview-free` 在当前 Zen 页面有免费/零留存声明，但 Pi 1.1.0
原生 catalog 实际未包含该 ID。未来可按官方 models.json 合同添加一个
明确模型；这仍需要独立 exact variant/effort 质量匹配及实际访问验收。
当前 FreeTierError 与上游免费层 harness 限制已明确，单换 Step 不能被
当成认证/账单修复或未知请求重试。此次没有 Step POST、header 身份伪装、
OpenCode agent、充值或 paid fallback。新可用授权路线由后续明确任务
继续，保留本次终态和长期 owner goal。
[Pi models.json](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/models.md)
