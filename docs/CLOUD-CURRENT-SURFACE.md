# 当前官方 Cloud：正式任务身份与旧 CLI 的边界

PAM-006e 的独立分支实施片，2026-10-14。实现：
[cloud_ui.py](../assistant_mesh/cloud_ui.py)、
[CloudJobs](../assistant_mesh/cloud_jobs.py)，风险测试：
[test_cloud_current_ui.py](../tests/test_cloud_current_ui.py)。
未操作桌面、提交官方任务、重建环境、修改生产网络、登录或新增费用。

## 已核实的正式入口

当前官方说明把新版 published environment 与 Legacy Cloud 分开。
新任务应在 web/desktop 的 **Work in → Cloud** 选择已发布环境，或在
mobile 的 **Codex** 选择该环境；初始化、保存草稿和 Publish 都不是正式任务。
本人已发布的原环境应继续使用，不为 CLI 看不到它而重建。
[新版 Cloud environments](https://learn.chatgpt.com/docs/environments/cloud-environments)，
[Legacy guide](https://learn.chatgpt.com/docs/environments/cloud-environment)。

本机先审查 shell/npm launcher，再直接运行安装中的原生 `0.162.1`：
`--version`、`cloud --help`、`cloud exec --help`、`cloud list --help`、
`cloud status --help` 均 exit 0。仅 help，没有提交或账户推理试探。
当前正式 CLI 有 exec/list/status/diff；已有 CloudJobs 已封装一次 exec，
保留原 intent、stdin prompt 和 attempts=1，不需另造后台 OpenAI API。
[官方 CLI](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)。

同 tag 官方源码仍通过 `task_url` 生成 `/codex/tasks/<id>`，环境发现使用
repo/global environment 列表；exec 会先解析环境，再创建任务。不能把
新版个人环境在 UI 可选直接当作这套 CLI 已兼容，也不能通过 exec 试探。
本片没有新的环境列表/账号权限实测，历史空数组不是当前账号无权限证明。
[任务 URL 源码](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cloud-tasks/src/util.rs#L125)，
[环境发现](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cloud-tasks/src/env_detect.rs#L294)，
[exec/stdin](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/cloud-tasks/src/lib.rs)。

## 不猜新版 URL 的最小适配

官方文档没有给出新版持久任务的 URL/JSON 回执规范。本片新增的是
**Mesh 自己的 owner 证据 envelope**，不是声称找到新的 OpenAI API。
默认旧 `record_ui_receipt` 仍只接受原 `/codex/tasks/<id>`；没有可信 UI
verifier 的 current 分支拒绝记录，绝不把任意 `/local/*`、普通 Chat、
setup 或临时 `client-new-thread` 当作 Cloud job。

Trusted owner 配置 `CloudJobs(..., ui_receipt_verifier=normal_ui_verifier,
ui_environments=[原current环境绑定])`。CLI 配置必须用真实 provider ENV_ID；
current UI 未公开该 ID 时可用本人私有证据中的稳定环境引用，verifier 必须
核对应的原 published setup/个人身份域/仓库，而不是只匹配同名显示标签。
ui_environments 中的引用不允许 CLI exec，且拒绝发生在尝试边界消耗之前。
这是 Python 部署 hook，不是 Cloud/model 可填写的 JSON 权限参数。
它必须真正核对：已确认的本人账号、原已发布环境、原意图的唯一发送、
正式任务的 OpenAI Cloud 执行位置、保存后的同任务重开，以及精确 ID/URL。
不会打开邮箱菜单重复确认本人既有显示名绑定。

`current_ui_binding(job_id)` 仅读原 task/request/epoch/environment、owner
hash 和完整原 intent hash，不回传 prompt、token 或创建任务。
`record_current_ui_receipt(..., owner_confirmed=True)` 还必须通过 verifier
的严格 True，再原子记录以下 envelope：

```text
format = mesh-cloud-current-ui-receipt/1
原 job/task/request/epoch/environment + owner_binding_sha256 + intent_sha256
真实 provider_task_id + 原任务的 canonical ChatGPT URL
workflow = published_environment_task
execution_location = openai_cloud
launch = { evidence_sha256, observed_at }
reopened = { evidence_sha256, observed_at }
```

两份私有原始观察应留在 owner state，时间有序、不同 evidence hash；hash
仅标识这些观察，**不是独立执行证明**。URL 只作安全的同源引用，不能
代替 UI verifier。只接受原网页 once-boundary，不能认领 CLI unknown。
新增两张独立附加表不改原 namespace/intent/旧 unknown 行；重复原回执
只读原值，provider ID 不能用于另一个 Mesh task。

`record_current_ui_observation` 保留原 launch/身份，追加同保存任务的新
reopened 证据。verifier 的 binding 带 `expected_provider_status`，供其核对
实际显示状态；本地状态仅 pending/running/completed/error，未知显示状态
不推断完成。原观察完整保存，旧/同时间冲突状态不能覆盖更新观察。

current UI job 的普通 observe/status 返回需要原 UI 观察；fetch_diff
明确要求已有 CloudMembership artifact callback，**不**拿新版 ID 去调用
Legacy CLI。完整成果仍走原 task-bound `hello/artifact/report`，不是另造
diff 或把 VM 保留期当永久存储。execution_verified 保持 false。

## Root 一轮实际接线顺序

1. 保留本人原已发布环境与 private intent。仅针对新的明确 Mesh child
   工作保存一次原意图，prepared membership 先绑定 task/epoch/lease。
   原来 setup 或已发出的未知正式任务先读原状态，不再新发。
2. 在 **新任务** 选择原环境；完成真实能力/模型选择。若模型目录仍报重试，
   本步骤未提交，不归因“账号没登录”或虚构 quota。只暂停相关入口线。
3. 确认 `begin_ui_submission` 返回 submission_started=True 后才发送一次。
   保留正式任务 ID/URL、原 Cloud/environment 标记和第一份私有观察。
4. 从正常任务列表/原任务链接重开同一保存任务，核同环境与 saved state。
   若仍是临时页面/没有持久身份，不 record/不 resend，留原 unknown。
   正常 UI verifier 核两份证据后，才写 current receipt。
5. 使用已获本人授权的可达 HTTPS:443 gateway，环境联网规则允许目标；
   task-bound 网络秘密/独立节点凭据由正常 secret 管理注入，不拷贝本机
   OAuth、CODEX_HOME 或 operator bearer。真 VM 发 hello、获授能力调用、
   完整 artifact/report；VM 结束后从 Mesh 读回完整字节并独立核验。

新版环境 network secret 是目的地限定的 HTTPS placeholder，环境与 VPN
访问规则仍独立。域名/DNS/Tailscale/IP 公信证书的本人选择尚是独立部署
gate，本片不擅自申请证书、开放监听或创建收费 API key。
[窄 gateway](CLOUD-REACHABLE-GATEWAY.md)，
[官方 Cloud network secrets/VPN](https://learn.chatgpt.com/docs/environments/cloud-environments#configure-environment-variables-and-network-secrets)。

14 新测试和 Cloud 聚合 85 项通过：原 once/unknown、拒绝 setup/local/
临时身份、不安全 URL、原 intent/owner/env 冲突、严格 verifier、并发、
附加证据和原状态不可变、current job 不调用旧 CLI，以及合成 scoped-node
完整 153600B 离线恢复。synthetic verifier/provider/HTTP 不算真实 Cloud。
原 CLI help 是实机证据；正式 job、UI verifier 与公网回调还须上述实证。
