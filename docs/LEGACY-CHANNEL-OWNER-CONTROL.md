# Legacy carrier 的私有 owner 控制层

这是 PAM-009c 的 Linux 部署适配器**源码**，不是已部署的生产接管、
bootstrap witness 或 VPS 分区 HA。其合同也不要求 Windows、手机、
官方 Cloud 节点照搬 Unix socket。现有原生工具、网络和权限保持原样。

`assistant_mesh.legacy_channel_owner` 用现有 `serve(ready=...)` 回调拿到
**当前 receiver 已经拥有的原 `ChannelLoopLifecycle` 对象**，保留原 poll/
send Thread 与完整 callback admitted/returned 观察。它不从 PID、
线程名字、静态 flag 或 checkpoint 猜测停止，更不会重建丢失的句柄。

## 接口与作用域

只提供两个 owner-private IPC 动作：

- `status`：返回 protocol、随机 instance ID、实际 socket peer PID/UID
  和完整 lifecycle snapshot；没有账号、凭据、消息、配置路径或历史正文。
- `stop`：要求同 instance ID，以 0–60 秒**共享截止时间**调用这一组
  现有线程的 `stop()`。没有 start/reset、导入、重放、grant、Leader/
  Worker/HTTP server 停止、模块加载或任意命令执行接口。

超时仍是同一组活线程/未返回 callback。后续只观察或继续有界 join
同组，不生成新实例、不取消已准入 callback、不发第二次业务请求。
`close()` 仅关闭本适配器 IPC，不停止 carrier。

结果保留 `loop_calls` 原始计数、线程入口/返回/失败、running loops 和
local closure。以下字段即使本地停止闭合也始终是 **false**：

```
external_operation_quiescence_verified
canonical_channel_state_closed_verified
handoff_authorized
native_features_restricted
```

原 outbox unknown/原 client ID 保留；callback 返回不等于外部消息送达。
真正的 canonical 状态核验、旧承载失权、一次受控交接与独立故障域
authority/state/arbitration 仍需后续真实合同和验收。

## 私有边界

固定协议为 `mesh-legacy-channel-owner-v1`，一连接一个有上限的 newline
JSON 请求。只在 Linux 有实际 `SO_PEERCRED` 时启用：server 在读请求前
核 client UID；client 在发动作前核实际 peer UID、可选固定 PID，并核
response 的 PID/UID 与该 socket peer 一致。stop 必须携带当前 instance
ID，CLI 还要求固定 `--expected-pid`。

父目录须**已经存在**、由当前 owner 拥有且恰为 0700，不允许祖先
symlink。socket 必须新建，随后设 0600；现有 socket/file 从不自动
删除、接管或替换。清理仅删除本实例原 inode 的 socket，不能误删
同路径新文件。实例 ID 用于拒绝发错进程实例，不是 fence token 或
任期授权。same UID owner 仍保有本机原生能力；这不是 host sandbox。

IPC 最多同时处理 8 个连接；输入/输出帧最多 4096B，读取有期限。
所有 public 错误固定分类，不输出原 exception/message/账号状态。
没有 TCP、不核 peer 的后备路径，也没有自动重试业务动作。

## 启动与源码隔离

Owner 应先在已核对的唯一 receiver、完整备份/原在途状态边界安装。
以下只是接口示意，不是本次已执行的部署指令：

```sh
python3 -m assistant_mesh.legacy_channel_owner \
  --socket /private/owner/channel/control.sock \
  serve --config /private/owner/server.json
python3 -m assistant_mesh.legacy_channel_owner \
  --socket /private/owner/channel/control.sock \
  status --expected-pid RECEIVER_PID
python3 -m assistant_mesh.legacy_channel_owner \
  --socket /private/owner/channel/control.sock \
  stop --expected-pid RECEIVER_PID --instance-id STATUS_INSTANCE_ID --timeout 5
```

Python 入口为 `serve_with_owner_control(config, socket_path)`；只接受
legacy channel 配置，不接受 `channel_carrier` 或 private notification
模式。它先预留 owner IPC，再调用现有 server；ready hook 只接已经
启动、拥有实际 Thread handles 的 legacy group，拒绝 fenced/supervised
group、缺失句柄或重复 attach。

**不能直接在生产用新完整 Store 替换旧 Store。**当前正式旧核心
3198c87 的 Store 与新候选有 native-goal settlement 差异；receiver 与
Worker 原来共用代码目录。要冻结独立 receiver 安装，保留原 3198c87
业务模块/Store/原 Worker，只最小 backport 697b98c 的 legacy Channel
及其依赖、真实句柄启动与 ready 失败/finally 清理。不能覆盖二者
共同 cwd；不能把新 goal/drain 行为随 receiver 升级带入 Worker。

原 3198c87 `serve.ready` 丢弃了 thread handles，而且回调位于其
`try/finally` 外；**未经上述最小 backport，本模块不能补出原句柄**。
旧 Store 还没有 fenced `channel_commit_poll` 需要的 `_ingest_in_db`，
所以不允许顺便启动 fenced poller 或声称 legacy bootstrap 已完成。

服务启动临时将 **server 所引用的原 Store factory**限定为
`recover_inflight=False`，仅用于本次 API 构造；ready 时、HTTP 开始
处理前立即恢复，异常退出也恢复。这样不触发构造器把所有 outbox/
**无关 native steering** 的 submitting 改为 unknown。它不替换 Store
实现、事务、业务方法、Worker、原生 goal 或历史；旧未决状态保留，
不是自动 replay 许可。该 scoped factory 是 owner 自己启动的单一
服务适配器，不供请求加载；同进程第二次启动拒绝。

## 验证与未完成事项

新增 tests 使用真实 Linux Unix socket/SO_PEERCRED、真实 Thread、
真实 SQLite 与 loopback HTTP，但 **iLink/account/消息均 synthetic**。
覆盖实例误配、UID/PID、0600/0700、现有路径/同路径替换保护、
有界停止/完整 finish callback、unknown 不重放、状态闭合不授予
handoff、无非法动作、startup Store recovery 不改 unrelated steering，
以及 carrier stop 后 HTTP/无关原 task 行继续保留。环境 proxy 不
用于 synthetic loopback 检查。

本机 focused **20 项通过（3.428s）**，合并 lifecycle/supervisor/server/
Channel/checkpoint 近邻 **93 项通过（3.809s）**，compile/diff 检查通过。
首轮一个 synthetic loopback HTTP 检查受环境 proxy 影响报 502，修正
fixture 仅显式禁用该次 loopback proxy 后通过；没有改生产网络。
Python 3.6 语法解析通过不代替 VPS 运行验证；双端冻结/CI由 Root 集成。

这些测试不验证生产 iLink 在途请求、原通道 canonical closure、真实
旧 receiver instrumentation、carrier 换到 laptop 或 VPS 分区 HA。
安装/受控停止/外部状态证明由 Root 后续在唯一收发器边界实测；在此
之前保留“source-only，production pending”的状态。
