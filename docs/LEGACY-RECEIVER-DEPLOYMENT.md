# 旧版 receiver 的独立部署边界

18:54 Root 已**实际加载唯一 receiver**，读回当前 PID/UID/socket peer、
实例与原生 poll/send Thread handles。仅独立 receiver cwd/入口和
systemd runtime 目录变更，原 Worker/Store/共用源码、账本 inode 保留。
原任务/steering/session/native-session/chunk/action/interaction 全行
指纹前后相同；一致 SQLite、原配置/账户/unit 完整私有备份保留，
没有回灌备份或开启第二接收器。

18:56 实际 carrier-only stop 已闭合：同 PID/instance，两个原线程均
退出，完整 callback poll **8/8**、send **275/275** admitted/returned，
active=false。send 是 loop callback（包括空队列），**不是275次消息
发送**。HTTP health200、原 Worker 未重启、上述完整 native 行指纹不变；
原 outbox102accepted、context 和原 ledger inode 保留，没有 pending/
submitting/unknown 或原请求重放。18:58唯一receiver收发已受控恢复：
新PID/instance/socket inode、真实poll/send活线程均核验，原Worker及
native全行hash不变。两次受控receiver重启（加载/恢复），自动NRestarts0；
不是原Worker重启或两个竞争收发器。
IPC 的 external/canonical/handoff proof 仍为false；没有跨节点交接。
这不是正式核心整体升级、真实 carrier 交接或分区 HA，后续停止及
恢复的实际结果另记，不能由下面的测试推导出来。
原生 Shell、网络、文件、MCP、Leader 和 Worker 权限不由此适配器限制。

## 冻结范围与真实验证

候选以正式核心 `3198c877234a8fd0c6912fdbe218f47185effedc` 为底：

- 原 189 个文件中的 187 个逐字节不变，仅替换 `channel.py` 并最小
  修改 `server.py` 的启动、ready hook 与 finally 清理。
- Channel 及其 carrier/supervisor/owner 依赖取自已发布源码
  `4860c170ea367e4676ac62a2ef58193b5ab7eb3f`；新增三个依赖模块和
  三个测试文件，共 195 个 regular source file。
- 原 Store、Worker、Codex、业务 API 构造器、配置、networking 和
  remote-notifications 实现保留。没有顺带升级原生 goal/settlement。
- 同一归档的风险相关 **117 项**：本机 Python 3.14 通过（9.424s），
  目标 Python 3.6.8 通过（12.807s）。提取后的 195 个文件完整哈希一致；
  原生产 37 个模块的基线哈希及两个服务的 PID/重启次数另行只读核对。

冻结归档 SHA-256：
`116bacc2a164cf167a44758726d7758232782ef5193c2ce7538f40ac8a74da01`。
完整 manifest、最小 server patch、测试收据在 owner 私有交接材料中，
不把机器路径、账号、消息、配置或 rollout 放进公开仓库。
无机器信息的 [最小 server patch](../deploy/legacy-owner-server.patch)
公开保留；只适用于上述3198 base，须配合同一4860c17的Channel及三个
依赖模块，不对当前已演进server或不同环境盲套。原私有patch SHA为
`52c2805a61a65f4da2f278b29fbc830897dc0e8a05ae7ad2b398cf8373e9d8c1`。

117 项使用实际旧 API/Store、Thread、Unix peer credentials、SQLite 和
loopback HTTP；iLink/account/消息是 **synthetic**，无真实模型或 iLink
网络调用。覆盖 carrier-only stop 后 HTTP 继续、完整 callback 归还、
原 submitting/unknown/私有 client ID 保持、无关 task/steering/session/
chunk 不变以及 ready 失败清理。它们不是生产通道闭合证据。

最初的私有 smoke 夹具有三处错误：未进入 HTTP serving loop 就调用
shutdown 导致等待；从公开状态读取故意不公开的 client ID；假设两个
独立线程的固定调用先后。失败/自有测试进程中止记录保留；修正仅涉及
夹具，没有放宽实现或原断言的身份/次数要求。

## 构造与版本隔离

使用 `legacy_channel_owner.serve_with_owner_control`，不能直接用新完整
Store 替代旧 Store。适配器仅在本次原 API 构造期间把 **server 引用的
原 Store factory** 调用限定为 `recover_inflight=False`，并在 ready
开始处理 HTTP 前恢复；异常 finally 也恢复。实际混合版本测试已核对
这一点。它不是恢复旧 DB，也不是把旧 submitting 自动改成可重放。

Receiver 与 Worker 原来共用源码目录；不能在该目录覆盖候选。Receiver
须使用独立冻结安装目录，Worker 的原 cwd/模块/配置保持。所有实际
state/credential 配置引用须逐项核为原位置的绝对路径；有相对路径时
先解决定位，不把新 cwd 当新的私有状态目录。不要顺便修改账号或认证。

生产 unit 若启用 `PrivateTmp`，不能直接使用位于临时命名空间中的
验证目录作为 cwd。应先把同一归档提升到 owner 可持久访问的独立目录，
再逐文件核对 manifest，保留旧 unit 与恢复路径。

## Socket 崩溃恢复的实际边界

真实目标是 **systemd 239**，不是先前假定的 219。本机 man 和一个
唯一临时 user unit 实测了：

- `RuntimeDirectoryMode=0700`、`UMask=0077`；实际 socket 为 0600。
- `RuntimeDirectoryPreserve=no`；正常 stop 后 PID 消失、目录/原 socket
  自动删除。再次启动是新 inode，不复用旧实例。
- 仅对这个 synthetic unit 的已核实际 argv/UID 的主进程发送 SIGKILL；
  实际终态 `Result=signal`、`ExecMainCode=2`、`ExecMainStatus=9`，
  PID 消失、目录自动删除，不依赖 Python finally。
- 同时使用 `PrivateTmp`、`NoNewPrivileges` 和原部署量级的内存配置。
  试验 `Restart=no`，没有宣称验证了完整自动重启、iLink 或 HA。

自有 unit 已卸载，运行目录和测试 PID 已清理；测试代码/收据可恢复保留。
候选源码未因此变化；这个 canary 阶段没有修改正式服务，随后 Root
的唯一 receiver 加载见页首，不能把两个阶段混为未部署或 HA 已完成。

部署模板仅展示字段关系，占位符必须由当前节点按实际位置替换；这些
Linux 示例不是 Windows、手机或其他 Mesh 节点的必需环境：

```ini
[Service]
WorkingDirectory=<独立冻结 receiver 源码目录>
RuntimeDirectory=mesh-channel-owner
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=no
ExecStart=
ExecStart=/usr/bin/python3 -m assistant_mesh.legacy_channel_owner --socket %t/mesh-channel-owner/control.sock serve --config <原私有配置的绝对路径>
```

必须选择没有被其他服务使用的 runtime 目录。不要自动 unlink 旧 state
socket，也不要用 `RuntimeDirectoryPreserve=yes/restart` 保留本次 socket。
目录清理只解决本机 IPC 文件寿命，**不证明外部请求已取消或闭合**。

## Root 的加载、观察与回退顺序

1. 保存完整 effective unit/drop-ins、原配置与一致 DB 备份及基线。
   此前旧 Worker 遇 authority 请求丢失会中断 native turn，因此加载前
   必须重新核真实 idle/no native in-flight，而不是用旧状态或 PID 判断。
2. 只切唯一 receiver 的独立 cwd/入口；不启第二 poller，不动 Worker
   共用源码，不清历史、guard、unknown 或 client ID。
3. 读回实际新 receiver PID/UID、socket peer 和本实例 ID，再用私有
   `status --expected-pid` 核原 owned handles。
4. 如做 carrier-only 验收，`stop` 必须同 PID/instance ID、共享有界期限；
   超时仍观察原句柄，不建替代实例。核完整 callback 归还、HTTP 保持和
   原通道账本，不能把 IPC ok 当 handoff 许可。
5. 回退只恢复 receiver 的原 unit/cwd/入口，停用本次独立覆盖层。
   不回灌旧 DB/rollout。旧 Store 默认构造恢复可能把 submitting
   保守改为 unknown（包括 steering），需保留前后收据并明确此风险，
   不能为了“还原成功”改回 pending 或清效果记录。

旧 Store 仍缺 `_ingest_in_db` 及 fenced channel prepare/mark/record/commit
事务接口；本候选明确拒绝 `channel_carrier`，没有 ChannelAuthority 接线。
这部分最小事务 backport、真实 canonical/external 闭合、一次受控换
carrier 与 communication authority/state/arbitration 独立故障域都另验。
本地停止结果的 handoff/external/canonical proof 字段仍为 false。

详见 [owner IPC 合同](LEGACY-CHANNEL-OWNER-CONTROL.md) 与
[沟通容灾合同](COMMUNICATION-CARRIER-HA.md)。
