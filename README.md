# Personal Assistant Mesh

独立、轻量的个人助理控制面：笔记本关机不停止微信入口，任务持久保存后交给可用的 Leader。

这是 OpenClaw 的配套扩展项目，不是 OpenAI Dots 的官方源码，也不声称完整实现 Dots。
首个版本使用 Codex app-server，复用本人已登录的 ChatGPT 认证；不需要新建 OpenAI API Key。

## 当前能力

- Python 标准库 + SQLite WAL，无 Redis、数据库服务器或常开桌面依赖。
- 独立微信 iLink 长轮询；整批消息、任务、回复上下文和游标在同一事务中持久保存。
- 消息去重、稳定出站请求 ID、发送未知结果不自动重试。
- 已绑定本人身份核对；第三人/群聊消息不触发任务。
- 设备能力目录、90 秒 Leader/任务租约、旧执行者提交隔离、任务检查点。
- Codex stdio app-server worker，自动发现 `.codex-official` / `.codex` 的已有认证。
- 本机和云端 worker；本机消失后云端接管只读工作，本机专属工作等待对应能力。
- 无模型的 `/status`、`/pause <完整任务ID>`、`/resume <完整任务ID>`。
- 每节点独立 token 与授权能力；RPC 仅监听 loopback，通过 SSH 隧道跨设备。
- 默认零支出；预算预留支持并发限制和请求防重，尚不执行采购或扣款。

## 运行

```bash
python3 -m unittest discover -s tests -v
python3 -m assistant_mesh --config /absolute/private/server.json serve
python3 -m assistant_mesh --config /absolute/private/worker.json worker
python3 -m assistant_mesh --config /absolute/private/operator.json status
```

配置必须为本人持有的普通 `0600` 文件；配置/认证/token/数据库存储在仓库外的 `0700` 私有目录。
配置生成器 `scripts/configure.py --help` 和 `deploy/` 提供本人 Linux/systemd 部署模板。
配置中的 `auth_home` 通过支持 `CODEX_HOME_OVERRIDE` 的 Codex wrapper 选择；原始 Codex 可执行文件使用自身默认认证目录。
不复制整个 Codex 主目录，不导出聊天历史，不把认证信息放到模型提示中。

## 接口

所有 `/v1/*` 需要 Bearer token；`/healthz` 仅返回服务存活状态。

| 接口 | 权限/行为 |
| --- | --- |
| `GET /v1/status` | 已认证状态查询，不返回消息正文或凭据 |
| `POST /v1/heartbeat` | node token，仅发布管理员授权能力，节点身份由 token 绑定 |
| `POST /v1/claim` | node token，原子领取匹配任务，返回执行 epoch |
| `POST /v1/task/update` | node token，必须持有未过期租约与当前 epoch |
| `POST /v1/tasks` | operator 创建任务，可指定父任务及能力 |
| `POST /v1/task/status` | operator 查看任务状态 |
| `POST /v1/inbox` | operator 按归档游标读取消息，不泄露上下文 token |
| `POST /v1/notify` | operator 向绑定本人入队通知，稳定 request_id |
| `POST /v1/notify/status` | operator 查询原发送结果，不重新发送 |
| `POST /v1/budget/reserve` | operator 按最小货币单位预留预算，未配置则拒绝 |

## 边界与后续里程碑

- 当前 worker 为只读，不是已开放采购、任意终端管理的全权限助理。
- 微信服务端可能拒绝长期空闲后的主动发送。明确 `-2` 仅在新回复上下文到来后尝试；网络未知结果不重发。
- 已受理不等于手机实际收件；保留 `delivery_verified=false`，真实收件单独验收。
- 附件私存但下载/理解尚未集成；现有 skill 的媒体功能不能自动算作云端验收通过。
- 初版单一控制账本，不声称控制入口本身无单点故障。网络分区不允许两个全局执行者同时提交。
- 跨主机不能直接恢复只存在另一台机器的 Codex thread；在新主机重新执行只读任务，不能重放可能产生外部效果的任务。
- 后续：OpenClaw 插件接入、模型兜底、技能版本发布与回滚、受限执行/审批、询价与采购适配、跨夜可靠性观察。

公开仓库只存代码与脱敏配置示例。运行证据记录必须去除账号、消息正文、IP 与凭据。
