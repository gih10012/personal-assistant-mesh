# Pi → Go 的独立免费工具试验（PAM-010d）

2026-10-11 18:30:59（Asia/Shanghai）终态：**一次实际 POST 收到 HTTP
403，要求有效 Go 订阅**。这与此前 Zen 的 `FreeTierError` 是不同端点、
不同拒绝原因，不据此推断所有免费模型或 Pi 都不可用。

采用同一已下载 Pi 1.1.0 bundled native SDK，显式 provider
`opencode-go`、物理 ID `step-5-preview-free`，端点为
`https://opencode.ai/zen/go/v1/chat/completions`。引用本人已有 Go
认证，没有新登录、key、订阅、充值、Use Balance 或付费 fallback。
Go 官方明确支持 Codex/Pi，并将 Step Free 列为限时零价；**价格为 0
不等于该接口无需有效订阅**。[Go 官方文档](https://opencode.ai/docs/go/)

## 实际结果

- 模型 POST **1**，curl exit **0**，TLS verify **0**，HTTP **403**。
- JSON `error.type=server_error`，私有正文只作脱敏分类
  `GO_ACTIVE_SUBSCRIPTION_REQUIRED`，不公开正文或凭据。
- Pi 自然 `agent_settled`，最终 `stopReason=error`；工具 **0**，成功
  产物 **0**；成功后的原 session follow-up **未提交**。
- 实际 UA 为 Pi 自身身份，`x-opencode-session` 等于本次真实 native
  session ID；没有伪装 OpenCode 客户端或使用旧 trial/session 身份。
- controller 的 SSH/Pi/curl 均已回收；已有认证文件字节未变，本次
  19 个私有记录扫描中 Go key 出现数 **0**。没有改正式服务或全局网络。

这只是真实 transport/terminal rejection 和失败 session 持久化证据，
不是认证、模型推理、工具往返、成功续接或 Mesh 模型能力可用性验收。
现有 key 的存在不证明 Go 订阅当前有效；未查询或改变账单。

## 有界隔离与连续性

明确 trial 为 `PAM-010d-pi-go-step5-free-tool-session-20261011-v1`。
启动前核对 prepared prefix 中没有请求计数、trial summary 或 session，
也没有存活进程；此前专员中断不是一次模型请求，更没有 unknown 重放。
本次仅启动该 controller 一次，保留实际原进程终态及完整失败文件。

输入只含合成整数、marker 和三个 scratch 文件的排序/求和/读回任务。
native loader 为空；没有 Leader 记忆、skill、私人文件或聊天输入。
原生工具仅 read/write，局部 trial guard 只约束本合成任务，**不限制
节点原生 shell/文件/网络**。retry、compaction/cache warming 关闭。

实际 provider 使用显式 zero-cost models.json 配置，thinking=`off`，
max output=4096，context 配置32768；这些是本试验的保守客户端配置，
不声明模型完整窗口或某个 AA effort 已验证。实际请求没有
`reasoning_effort` / `enable_thinking`；截图 Step Preview 约44分只能在
物理型号、版本和推理配置匹配的明确假设下作参考，不能反推本次能力。
[Pi models.json 合同](https://github.com/earendil-works/pi/blob/v1.1.0/packages/coding-agent/docs/models.md)

请求经本次 owned SSH SOCKS5h/curl fetch adapter，送入 Pi 原生 OpenAI
stream parser/agent loop。认证只在内存和 curl stdin 配置管道中，没有
进入 argv、prompt 或记录。没有证明 ambient SDK 代理行为或独立源 IP。

| 私有原始记录 | 字节 | SHA-256 |
| --- | ---: | --- |
| 唯一合成 request body | 2,534 | `f3fc3519ba2264ed60cc4e1adacce84107f9e8e9d88d98d7015f1b4df1d12a63` |
| 唯一 HTTP 403 body | 135 | `4e39de6b6b1cfdcc89ddce05dadbf22ecb4b08d27caaa32289e2345896e5d88c` |
| 本次失败 native session | 3,842 | `a40ac835d23537b88a1aa8134e7e011220e42a88b476bf0baf4848447ce09d10` |

Root 独立读取 summary/计数、请求与 session hash、原生终态及实际
PID 回收状态；完整 session/错误正文/私有路径不上传公共仓库。

## 下一合法路线

保留 [Zen 原失败](PI-ACTUAL-FREE-TRIAL.md)，不在同一拒绝接口上换
alias、伪装客户端或切收费方案。Owner“看着办”允许真实 OpenCode
匿名会话仅作为**临时外部执行能力**隔离验收：Leader 仍为 Codex/Pi，
不携带原生长期记忆，不改现有包装器或账户配置。

真实 OpenCode 免登录目录、Zen API 免费层和 Go 订阅接口必须分开。
本机 `codex-opencode` 最终运行 Codex，但会回写历史，且 relay 有
静态 UA/重试及推理转换限制；不盲跑它接管连续 Leader。原生匿名
客户端实测、工具产物/原 session follow-up 成功另报，不能提前算完成。
