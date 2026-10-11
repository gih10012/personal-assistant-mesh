# 原生 OpenCode 匿名免费能力：一次隔离试验

2026-10-11 18:47 CST 实际结果：**当前这份配置收到 HTTP 403 /
`FreeTierError`，工具任务没有成功**。不是仅研究/目录检查；也不能
从这个结果推导所有匿名免费入口都失效。Codex/Pi 仍是 Mesh Leader
harness，本次 OpenCode 只是 owner 允许的临时外部执行能力候选。

## 实际边界

使用本机已安装的原生 `opencode` 1.18.29 ELF，不运行
`codex-opencode` wrapper、不改 UA/协议身份、不安装或升级、不登录、
不复制已有凭据、不启用订阅/充值/付费 fallback。二进制 SHA-256：
`0fb0f7db3cf8dc69543692bb328491fef294f8a129a2dc92eea75c313d3eaec1`。
本记录证明使用该本机文件，不冒称已核官方发布签名/供应链完整性。

新 owner-only 临时 prefix 内独立 XDG data/config/cache/state/home/tmp；
原生 `debug paths/config/skill` 读回通过，认证文件不存在，没有复制
任何 credential 环境变量。只有原生内置配置 skill，未加载个人 skills、
MCP、外部 plugins、项目配置或聊天/Leader 历史。仅给该 child 使用
既有 loopback HTTP 代理；本次**没有独立证明经过 VPS**，没有改全局网络。

实际解析配置只允许 `opencode/step-5-preview-free`，small model 也
指定同一免费模型；title/summary/compaction 和其他 agent 禁用，自动
更新/分享/自动压缩关闭。执行使用一个新的原生 session、自定义
`mesh-synthetic` primary、真实 `serve` + `run --attach`。第一 user turn
配置最多 3 steps、最多 4096 output tokens、150s CLI deadline。
只有首轮真正成功才允许同 session 的有界 follow-up；本次未触发。

合成输入仅四个整数 `7,11,13,3`，要求原生 read→write/edit 生成
`SUM=34\n` 并读取核对。配置保留 read/edit、拒绝其他 trial tools；
这只作用于此临时任务，不是 Mesh 或本机 native 功能的限制。**XDG/
prompt/工具权限不是 OS 沙箱**：实际 native worktree root 为 `/`，
所以不能把 external-directory 配置当作完整文件系统隔离证据。
模型被拒绝且实际工具事件为零，没有读取个人文件的执行证据。

## 目录及预检失败保留

空临时 cache 的首个原生目录查询返回 `Provider not found`。本机旧
公开目录 cache 不含 Step；严格免费 whitelist 因此为空，不能由此
判定匿名推理失败。原生 `models --refresh` 30s 超时；未压缩公共
目录 GET 的 25s/15s/130s 超时及部分文件均保留，均为 metadata GET，
不是模型 POST，也没有因观察 timeout 重启模型调用。

随后普通 `curl --compressed` 完整取得 [models.dev 公共目录](https://models.dev/api.json)，
5389867 B，SHA-256：
`dc800728d62b322f8bcce55fa8bfe76a390dda91fdf51919e006a0cfc5f7cebb`。
原样交给原生支持的 `OPENCODE_MODELS_PATH`，不造模型或 cost 字段。
真实 CLI `models opencode --verbose` 随后退出 0，唯一模型 active：

- input/output/cache-read/cache-write cost 全部为 0；
- canonical model 为 `stepfun/step-5-preview`，原生 API 为 Zen
  Chat Completions；不是 OpenCode Go 的已认证订阅 endpoint；
- 声明 tool-call、reasoning、1M context、low/medium/high variants。

声明能力不是推理/工具成功；本次实际 variant 为 `default`，没有测
AA 分数，不能把截图中未知 effort 的分数冒充本次测量。免费渠道按
实际增量现金 0 比较，不由此次拒绝永久排除同型号的其他合法渠道。

## 实际终态与计数

SSE observer 已建立后才一次提交 user turn，原生错误事件记录
`statusCode=403`、`isRetryable=false`、`FreeTierError`，随后原 session
idle。观察到 1 个 session error、1 个失败 assistant message、0 retry
events、0 input/output/reasoning tokens、0 tool parts、0 成功产物。
第一 CLI **exit 0 但收到模型错误**，不能以 exit code 宣布成功。

没有 wire HTTP 计数器，**模型 POST 总数 unknown**；一次 user turn
和一次错误事件不是“独立证明恰好一次 POST”。上游 AI SDK 路径
默认 `maxRetries=0`，session 层另有最多 5 次 retry，未找到可关闭的
配置。私有 controller 订阅原 session 的 retry 状态，若出现会 abort
同 session；本次没有出现，不宣称严密零重试保证。
[LLM 源码](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/llm.ts)、
[session retry 源码](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/retry.ts)

未提交 follow-up、未 fork/import/重放原 session、未转付费。owned
server 被 TERM 并 wait/reap，退出 -15；controller 自然退出 0。随后
原生 `export` 只读重开同 session，完整两条 messages 与终态 API
读回逐字段一致，canonical SHA-256：
`6f83003ae8aa0c8e56506707b624dd4fd538f91255c8b5152381075abaac6ff8`。
这证明失败记录保留，不是成功工具续接或跨 harness 连续记忆。

## 客户端身份与候选原因：不能仅凭错误文字下结论

固定 v1.18.29 源码在 request preparation 中直接构造原生
`User-Agent` 和 `x-opencode-session/request/client/project`；这段不是
外部 plugin。`--pure` 仅跳过外部 plugins，禁用 default plugins 跳过
内置 auth integrations，其列表没有 Zen identity plugin。本次未抓
实际出站 request headers，所以“哪些 header 真正到达服务端”仍
unknown；源码期望不能冒充 wire measurement。
[request preparation](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/session/llm/request.ts)、
[plugin loader](https://github.com/anomalyco/opencode/blob/v1.18.29/packages/opencode/src/plugin/index.ts)

上游同仓 issue 报告真实客户端也会因禁用 bash/read/task、裁剪
tools 或自定义 agent 配置触发此错误；其中 #51315 明列 1.18.29，
#53347 的最新回复修正先前“agent 名称/UA”猜测，指出 bash-deny
相关。它们与本次 read/edit-only 配置相符，**是有根据的候选原因，
不是本机已完成的因果实验或服务端权威说明**。不能据此宣称“必须
登录/购买”或“原生 OpenCode 全部不能免费用”。
[1.18.29 tool 配置报告](https://github.com/anomalyco/opencode/issues/51315)、
[custom agent / body 与 header 区分](https://github.com/anomalyco/opencode/issues/53347)、
[server 模式及后续更正](https://github.com/anomalyco/opencode/issues/49756)

后续若继续正常客户端兼容性验收，应先核是否保留真实内置 agent/
tool 合同，并用独立环境保护私有数据，而不是造 UA、fake tool
schema 或把另一 harness 伪装成 OpenCode。当前失败身份和全部原始
私有证据保留；新的实际请求另由 Root 协调，本记录没有启动它。
此候选未登记为 available 的生产能力，未切 Leader 或正式服务。

## 下一独立 stock fixture 的最小方案（尚无新模型请求）

已只读核本机现有 bubblewrap 0.12.0。一个零网络/零模型 namespace
probe 实际退出 0：只挂载只读系统 binary/libs，建立独立 proc/dev/tmp，
在 **原 HOME 路径**挂空 tmpfs，确认原 auth、OpenCode config、Codex
目录及上一 trial 目录均不可见，HOME 环境值原样保留。没有重设
HOME/CODEX_HOME、改 host mount/权限/配置或清原 session。这个 probe
尚未运行 OpenCode，只证明该最小 namespace 在本机可创建。

供 Root 审查的下一步是：

1. 创建另一个私有 scratch/data/config/cache/state prefix，仅把公开
   合成 fixture 和公开 catalog 原样暴露进新 namespace；给 scratch
   初始化空 Git worktree，避免 native root `/`。系统 binary/libs、
   必需 DNS/TLS 配置只读；真实 HOME 路径保持空，新增 XDG 路径仅
   指新 prefix。保留 native shell/files/network 在该 fixture 内正常使用。
2. 原生内置 `build` agent/prompt 和全部真实内置 tools 不裁剪；不
   补 fake 工具，不改 UA/身份/SDK。仍只免费 physical model，关闭
   分享/升级、隐藏 title/summary/自动压缩，不携私人 auth、MCP、
   skills、plugins 或原 Leader history。网络只给 child 复用既有出口。
3. **先零模型 preflight**：同 namespace 内 debug paths/config/agent/
   skill、空 auth、唯一定价 0 目录及原 HOME 不可见逐项实际核验；
   检查 native shell、scratch 写入与 TLS 证书可用，不上传私有数据。
4. Root 明确续接后才做一次新的正常内置执行合同；SSE 先订阅，
   deadlines/step/output 边界及 first-retry abort 保留，未知不重放。
   旧自定义 agent 的已知拒绝与原 session 保持，不能抹去或当作
   stock fixture 成功。此处没有提交下一 POST、安装或购买。

这不是绕过免费层规则：下一片若获准，仅使用真正客户端的正常
内置功能来核兼容性，并以独立机器环境保护私有数据；也不是让
Mesh 对日常 agent 的原生 shell/工具施加这种临时任务限制。
