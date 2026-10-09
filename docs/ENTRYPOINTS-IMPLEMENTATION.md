# PAM-006b：Chat / 原生 Codex / Cloud 的最小真实接通路线

初版核对日期：2026-10-09；Cloud 只读 gate 续接：2026-10-10 北京时间。
本文是实施交接，不是入网证明。已核对官方文档、本机 `codex-cli 0.162.0`
帮助、公开 GitHub 源码、本项目代码及既有认证下本项目只读 Cloud 环境
查询；本研究/adapter 工作没有操作浏览器账号、提交 Cloud job、部署
服务、发放凭据或新增支出。Root 的正式部署独立记在任务记录。
原生 Codex 继续担任 Leader；入口不能重建其 thread、替换原生记忆或
阻断 Shell、文件、网络、MCP。补充研究见
[ENTRYPOINTS-RESEARCH.md](ENTRYPOINTS-RESEARCH.md)。

## 先接哪一条

| 入口 | 已核实的基础 | 最小下一步 | 不应宣称 |
| --- | --- | --- | --- |
| 普通 Chat，Firefox | 可通过浏览器扩展发送本人指定文本；官方普通 Chat 的自定义 MCP 可用性尚未核实 | 窄域扩展 → Native Messaging → 既有 owner-ingress；先人工确认发送 | 已获得官方 Chat 原生工具调用、常驻 agent 或无限额度 |
| ChatGPT Work plugin | 官方 quickstart 明确从 Chat 切换到 Work 后测试 plugin | 同一 ingress 的薄 MCP sidecar；实际主账号安装/授权/调用验收 | Work 示例等于普通 Chat 支持，或 localhost Inspector 等于实际上线 |
| 原生 Codex | 本项目已有持续 native session 与额外 Mesh tools | 保留当前会话，补入口工具/成果读取，不改 native runner | 新入口必须另起一个失去记忆的 Leader |
| Codex Cloud | 官方 CLI 可提交、列出任务；本机还有 status/diff/apply | 原生 CLI job adapter，先接一个实际 job 的限定结果回传 | job 提交成功等于节点入网，或 published environment 等于常驻进程 |

官方 custom-MCP 文档介绍 ChatGPT web 的读写工具，但没有在该页面保证
所有普通 Chat surface/账号均可用。更具体的 plugin quickstart 明确要求
切换到 **Work**。所以旧研究表中 Chat 的 MCP 路线必须按条件方案理解，
不能据文档替本人主账号确认 entitlement。
[Custom MCP](https://developers.openai.com/api/docs/guides/custom-mcp-server)，
[Plugin quickstart](https://developers.openai.com/plugins/quickstart)。

本轮优先完成一条实际往返，不等待所有入口齐备：本人从入口发一条
稳定 ID 的任务 → 原 authority 收账 → 原 native Leader 处理 → 入口
读取经批准的成果。之后再让 Chat 模型调用工具、注册 Cloud 临时节点。

## 可直接复用的本项目合同

[ingress.py](../assistant_mesh/ingress.py) 已有独立 owner-bound principal：

- `POST /v1/ingress/tasks`：`request_id`、`input`，可选 project/agent 标签。
- `POST /v1/ingress/task/status`：仅原 `request_id`，返回有限状态。
- 身份来自部署绑定的 owner/source/subject/scopes，不接受 prompt、
  email、调用方 actor、native session、node 或 task lease 覆写。
- 原 request/content 在同一 transaction 内确定普通 Store task；重试
  同 ID/同内容只返回原任务，变更内容冲突，不因掉线/模型变化重放。

具体权限和 CLI 形状见 [INGRESS-CONTRACT.md](INGRESS-CONTRACT.md)。
`result_available` 不是原结果读权限。`ingress_results.py` 的独立批准
成果实现已进入公开代码 `cf308b3`：source/subject/request 绑定、
完成任务 epoch/result digest 的 CAS、有界摘要与引用，并明确不证明
实际执行/引用内容；HTTP/CLI 已在同一源码接线。正式部署与实际原生
任务往返仍由 Root 逐项验收，源码发布或局部测试不能当作账号入网。

第一版所有入口只包装提交、状态、批准成果这三项。不把 operator、
worker 或 SSH bearer 给浏览器/ChatGPT，也不以通用公共 `shell()`
代替身份接入。节点自己的原生 Shell 不受此入口 schema 限制。

## 普通 Chat：先做窄权限 Firefox 桥接

这是项目设计方案，不是官方 Chat MCP 能力。第一版应让本人点击
“送到 Mesh”，不自动观察全部聊天，不读浏览器 cookies、密码库或
历史会话。收到成果时先在扩展面板展示，可由本人插入当前 Chat。
这样即使普通 Chat 没有 plugin 按钮，仍能真正成为本人沟通入口。

推荐最小组件：

1. Firefox WebExtension：仅 `https://chatgpt.com/*`，明确固定扩展 ID，
   本人主动选定的 tab/conversation 才能发送。背景脚本核对 sender
   tab、精确 origin 和激活状态；不接受网页任意 `postMessage` 指令。
2. 扩展 background → Native Messaging host。Mozilla 的 native host
   manifest 使用 `allowed_extensions`；不能照抄 Chrome 的
   `allowed_origins`。content script 不能直接调用 Native Messaging。
   [Mozilla Native Messaging](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_messaging)。
3. Native host：只解码有界 JSON framing，限定 submit/status/批准成果
   操作。stdout 仅协议；日志去私有 stderr。不得接受任意 shell、文件
   路径、URL、credential path 或 HTTP method 参数。它读取仓库外的
   专用入口配置，通过已有 authority 网络路径调用上述合同。
4. Linux 可用本人级 manifest；Windows/macOS 用自己的注册位置/启动
   包装，不把 Linux 路径变成协议依赖。适配宿主安装，而不是重写 Mesh。
   [Native manifest locations](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests)。

专用 ingress 凭据留在本人控制的 native host 私有文件，不能注入页面
或放在 URL/query、扩展可见工具输出中。首次由本人绑定 owner/source/
subject；账号 UI 显示的邮箱只能辅助核对，不能成为服务端认证证据。
同机同 UID 的完全控制不属于这个浏览器入口能隔离的信任边界。

每次发送先保存 request ID、内容 fingerprint 和绑定信息，再实际
发送。tab 刷新、扩展重启、native host 退出后查询原请求，不重造 ID。
响应先按结构和大小校验，以不可信成果文本显示，不执行其中代码。
Native Messaging host 随连接关闭可以退出；任务/回执/成果由 Mesh
持久保留，不把浏览器进程生存期当作任务生存期。

第二片才加入模型调用回路：向当前已武装会话提供有限工具 schema，
只解析完整、严格匹配 schema 的新 assistant tool block。调用必须带
本轮 issued nonce/稳定 call ID，首次写操作仍经本人确认。不得因为
打开旧会话/DOM 重新渲染而重执行旧代码块，也不得执行 streaming
未完成参数、网页引用中的 JSON 或模型自报权限。页面是输入源，不是
授权源；授权在 host/authority 中。自动继续对话要有本人开启的会话级
开关、预算/终止手段和可见状态，不以 UI 自动点击冒充官方 API。

## 第三方基准：复用解析/DOM，不直接安装宽权限成品

实际读取 `srbhptl39/MCP-SuperAssistant` 的许可证、manifest、ChatGPT
配置/适配器的关键处理、JSON parser、Streamable HTTP transport、
analytics listener/service 和 Firebase 配置代码。核对 commit：
`c26168ee2c5708a3a65ef5afd88cda1a97c81734`。
它是浏览器侧给网页对话添加工具回路的候选组件，不是另一套 Leader。

- [MIT LICENSE](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/LICENSE)：复制代码应保留许可证和署名。
- [ChatGPT adapter](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/pages/content/src/plugins/adapters/chatgpt.adapter.ts)：有 ProseMirror 文本插入、send-button 点击、SPA/DOM 观察。可借选择器/事件思路；本人真实 Firefox 上仍需验证，不能用 optimistic UI success 当任务完成。
- [JSON parser](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/pages/content/src/render_prescript/src/parser/jsonFunctionParser.ts)：识别 `function_call_start/end` 和参数，也容忍 streaming/partial JSON。可借格式兼容；Mesh 执行层必须改为完整严格解析、nonce/ID 去重，不采用 partial 参数 regex 作为可执行请求。
- [Manifest](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/chrome-extension/manifest.ts)：多站点 host permissions、analytics host、宽 web-accessible resources，且使用 Chrome MV3 background service worker。虽有 gecko ID，也不是 Firefox 运行验收。fork 时只留本项目必要域/资源，按 Firefox 实际支持改背景实现。
- [Streamable HTTP plugin](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/chrome-extension/src/mcpclient/plugins/streamable-http/StreamableHttpPlugin.ts)：该类直接构造 URL transport，不能据它推定已有完整 owner OAuth/撤销处理；不复用其 debug URI 日志来输出私有 URL/凭据。
- [Analytics service](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/chrome-extension/utils/analytics-service.ts)：有工具名、连接、错误栈及设备属性采集/发送调用；[sender](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/chrome-extension/utils/analytics.ts) 仅在构建时配置了有效 GA 值才发送，未核实发行包是否配置。Mesh fork 去掉此依赖和 listener，不把遥测当必需能力。
- [Firebase config](https://github.com/srbhptl39/MCP-SuperAssistant/blob/c26168ee2c5708a3a65ef5afd88cda1a97c81734/chrome-extension/src/background/firebase-remote-config-api.ts)：检查点中 `REMOTE_CONFIG_ENABLED=false`；存在远程配置实现不等于当前已启用。Mesh fork 不需要第三方远程 selector/config 更新，移除后使用经审核版本。

这是定向代码阅读，不是完整安全审计。建议先写小桥接，按需摘取
有许可证的 adapter/parser；若完整 fork，则先裁剪权限、依赖、日志、
telemetry、自动执行和远程配置，再测试。第一片不依赖它的大侧栏、
多站点功能或通用 MCP URL 配置，减少实际接通的前置工作。

## 官方 Work / 条件性 Chat MCP

相同业务入口可以另接 MCP sidecar：Streamable HTTP `/mcp`，三个薄
tools；submit 如实标记 write，状态/批准成果标记 readonly。sidecar
先完成 OAuth 2.1 authorization-code + PKCE，验证 issuer/audience/
expiry/scopes/revocation，并映射到专用 owner ingress。annotations
和客户端“记住批准”不代替服务端鉴权。
[Official auth](https://developers.openai.com/plugins/build/auth)，
[confirmation rules](https://developers.openai.com/api/docs/guides/custom-mcp-server#how-to-use)。

沿用已有本人 VPS 的受控 HTTPS origin/443（如果已具备）；localhost
不是远端 ChatGPT 可达地址。若还没有稳定 HTTPS/DNS/OAuth owner 登录
路径，先保留本地桥接，不开放匿名 Mesh。使用独立现代 Python sidecar，
不为 MCP 库升级正式 VPS 全局 Python。Secure MCP Tunnel 需要额外
Platform tunnel/runtime key，排除出本轮零新 key 路线。
[Tunnel prerequisites](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels#before-you-start)。

Root 下一步在本人主账号中核对实际 surface：安装/授权一条个人 plugin，
以原 request ID 做真实 submit/status/result；记录发生在普通 Chat 还是
Work。普通 Chat 不可用时使用已实现的 Firefox 入口，不更换账号来
伪造主账号验收；没有安装/真实调用证据保持 pending。

## Codex Cloud：原生 CLI 包装，而不是私有 API 猜测

初版研究仅运行本机 CLI help；续接另做了下文明确列出的只读 gate。
下列 exec/status/diff 仍为**未执行的命令形状**。prompt
通过 stdin，避免私人任务正文进入 argv/进程清单：

```sh
codex cloud exec --env ACTUAL_ENV_ID --attempts 1 - < /PRIVATE/job-prompt.txt
codex cloud list --env ACTUAL_ENV_ID --limit 20 --json
codex cloud status ACTUAL_TASK_ID
codex cloud diff ACTUAL_TASK_ID --attempt 1
```

本机另有 `cloud apply`，会修改本地 worktree，第一版不自动调用。
官方文档确认 exec/list；额外命令和边界来自实际本机 help 及相同版本
[CLI source](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/cli.rs)、
[implementation](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/lib.rs)。
该检查点的关键行为：

- Exec 的 attempts 范围 1–4，默认 1；提交成功输出 task URL，不提供
  exec JSON 或 caller 幂等键参数。list 有 JSON 和分页 cursor。
- **`cloud status` 仅 Ready 退出 0；Pending/Applied/Error 退出 1。**
  非零不等于网络错误，不能由退出码推导重提 job。
- status 是人类文本。用 `list --json` 精确匹配已记录 task ID 观察，
  保留未知新 status，不把未知当成功/可重做。status 用于辅助诊断。
- 未核实 CLI 有 cloud cancel/resume 或完整任意 artifact 下载。
  diff 能取回代码改动，不等于非代码成果已经回到 Mesh。
- CLI 使用自己的现有认证；实现有包含账号标识的私有 error log。
  工作目录/日志隔离在仓库外，公开证据只保留类别与脱敏元数据。

推荐 adapter 状态序列：持久 `job_intent` → 单次 `submitting` → 实际
回执绑定 `provider_task_id` → 原 ID 观察 → 取回 diff/限定回传成果 →
独立验收。提交 timeout/进程退出/非零且不能证明未发送时，保持
`submission_unknown`；不能因 list 暂未出现就自动重投。prompt 中的
nonce/title 只能帮助找候选 job，不是 provider ID/身份的可靠证明。
不能靠新的 Mesh request ID、账号切换或 attempts 增加来“修复”未知
外部效果。CLI 本身没有给 wrapper 提供可据以保证 exactly-once 的
服务端幂等合同。[Cloud CLI](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)。

先实际核对本人已有、已发布 environment ID 是否被当前 CLI 接受，
不要假定 legacy/new Cloud environment ID 完全通用。job adapter 可
先导入本人已有 job 并只读观察，随后由 Root 在任务授权和余量成立
时单次提交一个真实 job；本轮研究没有提交。当前 adapter 不提供
任意既有 job 导入接口，只有已持久 intent 的实际回执绑定/本人明确
对账；新增导入也须独立核对因果身份，不凭标题或 nonce 自动认领。

### 实际只读 gate，而非入网

当前 [Cloud job adapter](CODEX-CLOUD-JOBS.md) 的 32 项离线测试已独立
复跑通过。用原授权 native home 和原生 binary 执行
`cloud list --limit 1 --json`：exit 0、stderr 为空、tasks 为空、auth
文件原字节 SHA 前后相同。shell launcher 曾覆盖 caller 设置的
CODEX_HOME，故选择已核实的 native absolute binary，未改 wrapper/
auth；不把 launcher 路径或 CLI 成功当主账号身份的替代。

随后只读核对原 native 主账号 suffix，并按官方同版本
[repo-specific lookup](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/env_detect.rs#L310)
查询公开本项目，HTTP 200 返回 `[]`，auth 原字节仍不变。证据保存于
仓库外私有目录，不遍历其它 Cloud 环境/聊天，不发布 account/token。
GitHub `main` 独立可见，但尚无该 CLI lookup 返回的本项目 Cloud
environment，所以未执行 exec、未做 callback，也没有临时 node。
这是指定版本和指定 repo 的观察，不否定当前账号的全部 Cloud 能力。
下一步需要在同一本人账号核对项目环境连接/发布及实际授权，不能
自动创建环境、授权第三方或改 secret 来把这一 gate 假装成已接通。

## 临时身份、网络与退出后回收

当前 Cloud 文档中的 published filesystem 供新任务复用；每个 task
有独立 workspace。保存 VM 默认最多可恢复到末次 turn/resume 后
7 天，不替代 Mesh 的持久任务/成果/记忆。当前 Cloud 不提供 browser/
computer-use；本机个人 skills 不自动同步，repo skills 可用。
[Current Cloud environment](https://learn.chatgpt.com/docs/environments/cloud-environments)。

Cloud → Mesh 必须真实可达：已有公网 HTTPS 443 origin，或另外明确
授权的私有 VPN。不能将 laptop localhost/SSH tunnel 地址直接发给
远端 VM。domain allowlist 只管网络，不授予 Mesh 权限。当前 Cloud
network secrets 在 setup **及 task** 中，通过匹配的 HTTPS/443
代理替换占位值，不要求将真实 secret 写到 VM 文件。不要混用 legacy
singular `cloud-environment` 页面“setup 后移除秘密”的旧合同。
[Network secrets](https://learn.chatgpt.com/docs/environments/cloud-environments#configure-environment-variables-and-network-secrets)。

身份实施分两片，不能把 ingress role 当 worker：

1. 第一个 job 只用任务限定 callback/report principal 回传特定成果。
   原 owner/mesh task/provider job ID、expires/revoked、允许的操作和
   request nonce 在 authority 配置/持久账本中绑定。Cloud 自报 job ID
   不算 provider 身份证明；须由 adapter 回执和挑战绑定核对。
2. 需要并行 worker 时再做 ephemeral enrollment：单独 node/lease、
   任务范围、config revision、心跳和凭据撤销。owner ingress 的
   submit/read 不升级为 execute/lease/operator 权限。

不复制本机 native auth、SSH key、全权 operator 或永久 worker token
到 Cloud。先评估当前 environment 的既有 secret 配置是否能表达
单 job 凭据；如必须更新环境、发放临时 token 或新增第三方授权，
由 Root 作为单独实施步骤核对，不能在源码里写默认万能 secret。
临时 token 是 Mesh 授权设计，不是新增 OpenAI API key；当前尚未部署。

job 断线/VM 退出只令受管 lease 失效，不限制其原生 Shell。持久账本
保留 provider ID、operation IDs、未知效果、结果引用与验收记录；
没有心跳不自动释放未知外部效果/重执行任务。凭据过期或撤销后拒绝
新的 managed effect，仍可由 owner 核对既有 job 的状态/批准成果。
不能依赖临时 VM 保存恢复密钥、原始 Leader 记忆或唯一成果副本。

## Root 的实施顺序和停止边界

1. 集成/发布 owner ingress 与批准成果合同；在原 authority 验证同
   subject 的一个真实任务往返，其他 subject 和原结果读被拒绝。
2. 做窄 Firefox/native-host 小桥接，首版只人工发送和查看批准成果。
   测一次断开/刷新/重启后原 request 不重复；取得本人主账号页面的
   实际证据。它优先于完整 MCP OAuth 或大型第三方扩展改装。
3. 原生 Cloud adapter 先完成私有 durable intent、单次提交、精确 ID
   观察和 diff 取回，重点测试 Pending exit 1/unknown 提交不重投。
   使用固定版本 native CLI，不用 OpenClaw/opencode 替代运行器。
4. 用已有本人 environment/网络路径做一个真实 Cloud job 的限定
   callback；确认 job→authority→成果持久落账后再登记 ephemeral node。
5. 并行补 localhost MCP + OAuth 边界测试；已有 HTTPS/owner 登录路径
   具备时接实际 Work/条件性普通 Chat，最后接 schedule 与 Live。

可立即实现的本地代码/测试不需要新 API key或收费资源。以下不是本轮
研究授权内的动作：创建 Cloud job、GitHub connection/OAuth consent、
环境发布/secret 更新、公开 HTTPS/DNS/端口、安装浏览器扩展、Tailscale
新 key、Platform tunnel、自动 cloud apply。Root 需要依据原任务的
实际授权和当前账号/环境核对后实施；欠缺权限时请求本人，不换身份。
订阅内 Cloud/Work 也会占用现有 allowance，不等于无限推理。
[Usage](https://learn.chatgpt.com/docs/pricing)。

验收记录分别写“合同测试、localhost 实测、实际账号入口、真实 job
callback、临时节点上线/失效、独立成果验证”。未发生的项不打完成；
能初始化 MCP、看见 CLI help、收到提交 URL 或绿色 fixture 都不是
真实入网。只需要先验证一条可用往返，再让 Mesh 自主扩展环境适配。
