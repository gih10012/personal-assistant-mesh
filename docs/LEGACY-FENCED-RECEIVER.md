# 最小旧 receiver fenced 启动适配器

PAM-009c 源码与隔离部署候选，2026-10-13。**本片没有生产安装、停止、
grant、iLink 收发或第二个生产 poller**。它补实际旧接收器缺失的 carrier
角色/authority/启动/停止接线，不用整份 HEAD Store 或 Worker 替换旧核心。

## 精确的源码范围

[构建器](../deploy/build-legacy-fenced-receiver.py) 只接受已冻结的 40 模块
旧 receiver 组合（原3198、已部署 legacy 生命周期、两项只读 routing 指纹投影）。
逐模块 SHA 组成的规范摘要固定为
`f68f548cf160bb2083ad67ab7204d382c990fcd6ee0af8fb2ef999d5a1b2e29a`。
不同 shell 的通配符排序不一定相同；核对摘要时以文件名 ASCII 排序。

38 个旧模块字节不变，包括 Worker、Codex/native 会话、Channel、已有
witness/supervisor、资源/路由/任务逻辑。仅修改：

| 原模块 | 变更与实际边界 |
| --- | --- |
| `store.py` | 已有 [ingestion extraction patch](../deploy/legacy-store-carrier.patch)；所有非 ingestion AST/原 schema/constructor/recovery 语义不变 |
| `server.py` | 新 [HTTP budget patch](../deploy/legacy-fenced-http-budget.patch)；只允许已认证 carrier 的两个原结果提交接口接收 8 MiB＋envelope，其他接口仍128 KiB |

Store 原 SHA `7d8f750e…5440` → `08163399…5f08`；server 原 SHA
`23ae66d8…526b` → `cc5bb8b4…d19c`。完整值由构建器和运行入口固定。
新增已有 `channel_state.py`、`channel_authority.py`，及静态
[legacy_fenced_receiver.py](../assistant_mesh/legacy_fenced_receiver.py)。
构建器不复制认证、配置、聊天、账本、历史或缓存；不创建 Store，不
启动 Channel，不修改输入目录，不复用已有输出目录。新私有目录中的
`candidate.json` 记录所有实际源码 SHA，明确安装/IO/grant 均 false。

```sh
python3 deploy/build-legacy-fenced-receiver.py \
  --base-root /private/exact-frozen-receiver \
  --destination /private/new-fenced-source-candidate
```

这是源码准备，不是部署指令或交接凭据。其他平台不要求使用此 Python/
Unix IPC 示例，Mesh 协议和原生功能不因这个适配器受限。

## 原 API 保持；carrier 额外隔离

适配器在独占 owner 启动锁内临时提供原 API 的静态 subclass：先核
carrier peer 的 node/account/owner/canonical-authority 和凭据角色不重叠，
再构造原 API。前后两次凭据读取若变化会明确拒绝，不保留模糊 principal。
carrier 角色在 global status/task/resource/worker/ingress 路由之前隔离，
不能凭 `node` 继承 Worker 权限。非 carrier 请求仍调用原 API 方法。

显式 opt-in 会创建已有的五张 channel 表及 send identity 索引，仍在
**同一个原 canonical authority** 中，不建立另一份 inbox/task/native DB。
constructor 保留 `recover_inflight=False` 的旧 receiver 边界，不重分类
原 submitting outbox 或无关 native steering。没有改 Worker、settlement、
goal、history、原 sender client ID 或 unknown。

API/witness/Channel 构造及源码构建不调用外部 poll/send，也不导入
account.pending。只有明确 fenced、原 term 已授予并进入 group.start
才启动真实收发线程。完成 construction/ready 后、HTTP 开始处理前
恢复原 `Store/API/_start_channel` factories；
失败也恢复，不能由请求、JSON 或模块名重新注入。

## 两种明确运行方式；默认不授予任期

`channel_carrier.transport_mode="disabled"`：保留原 iLink 账户和通知 route，
只提供原 authority 的受限 RPC，无本地 poll/send/maintenance。owner IPC
明确报告 disabled、未启动、无实际线程、本地 closure false；这不是证明
其他旧收发器已经停止。

`transport_mode="fenced"`：必须已经具备原 authority 授予的本机 term 与
原 cursor。缺任期/身份不匹配/缺 checkpoint 会在业务 IO 前拒绝。任期
过期或 authority 不可达也不回退到裸 iLink，不重复原 operation。

可信 Python 入口是：

```python
serve_with_fenced_owner_control(
    private_config, private_socket,
    handoff_verifier=trusted_actual_verifier,       # 缺省 None，grant 关闭
    operation_verifier=trusted_original_effects,   # 缺省 None，reconcile 关闭
    before_channel=owner_prepared_state_hook,      # 可选，不默认安装任期
)
```

这三个参数只接受实际 callable。配置 JSON 没有选择函数、导入模块、
posted booleans、freeze/grant/reconcile 的接口；CLI 的 `serve` 也不
安装 verifier。正常 bootstrap 必须由独立核验通过的 owner 代码提供
真实 proof，不能复制本测试中的 synthetic hook。

只移动载体时，`run_remote_with_owner_control(channel, socket, ...)` 接受
已经构造的 `Channel(RemoteChannelState(original_client, private_spool), …)`。
不构造业务 Store、不导入 pending、不复制 canonical DB；状态和完整结果
继续通过同一已绑定的 client 到原 authority。原状态恢复/未知对账依赖
仍保留，原 authority 不可达时停止新受管 IO，不称 VPS 失联 HA。

## owner IPC 观察真正的 fenced 线程

旧 `legacy_channel_owner` 仍拒绝 fenced group；没有降低它的守卫。
新的 `FencedChannelOwnerControl` 复用同 UID/PID、0700/0600、同实例、
帧/连接限制和 status/stop-only 的 IPC wire。没有 start/reset、任意命令、
credential/history/state 读出、模块加载或 grant 动作。

`OwnedFencedSupervisor` 复用已有 same-term inspect/candidate/renew 行为，
显式 start 先尝试同 term 的 initial readiness（不 grant/收发），成功后
第一次 maintenance 等待原 interval，避免立即清 ready 与第一轮收发竞态；
若初次核验失败保持 gated，由唯一 maintenance 原身份核对/续租，非重发 IO。
同时保留三个实际 Thread（maintenance/poll/send）的入口/返回及每次
**完整 Channel callback** 的 admitted/returned/active；不只看 transport
返回。stop 与原 Channel callback admission 共用锁，并按一个0–60s
monotonic deadline 有界 join，不取消已准入 IO 或原 persistence。

未启动、缺句柄、部分启动、仍活着/未返回的 callback 都不能称 closure。
部分启动保留原句柄，不能以新 group 重试。独立 PID 或 JSON 快照也不
替代这些对象。新 snapshot 即使本地闭合，以下始终 false：

```text
external_operation_quiescence_verified
canonical_channel_state_closed_verified
handoff_authorized
native_features_restricted
```

仅证明这一组 owner-held callbacks/线程停止。直接在这些 owned loops
之外调用的 native 程序不在本观察范围，不能拿 local closure 给它们背书。

## 隔离验证与仍待真实部署的 gate

[测试](../tests/test_legacy_fenced_receiver.py) 在单独 package namespace 内
构建精确旧组合，不使用当前 HEAD Store/server。可从完整 git history
复原，或显式提供 `PAM_FENCED_RECEIVER_BASE_ROOT`；缺精确源会 skip，
错误源/hash 则失败。正式兼容验收须确认无 skip。

本机新12项包含真实 loopback HTTP、Unix SO_PEERCRED、三线程和一个
**独立子进程**远端载体：disabled/no-term 不收发、不导入 pending；
角色隔离/凭据变化拒绝；原 submitting 行保持；含200,000字符 padding
及一条原消息的完整 batch 跨 HTTP/spool/canonical 提交，仅创建一个
原 authority task；held transport/commit 的零期限 stop 不
闭合，原 callback 返回后闭合且所有 grant/proof claims 保持 false；
部分启动不能假闭合，旧 IPC 仍拒绝 fenced。所有账户/消息/iLink/proof
均为 synthetic，不是手机送达或真实 handoff。

最终源码本机12项 **5.653s、0 skip**；VPS Python3.6.8 私有隔离 fixture
12项 **6.417s、0 skip** 通过。本机新片与既有 canonical/authority/
supervisor/owner IPC 聚合 **106项16.773s** 通过（已有旧 Store fixture
出现一次非失败 ResourceWarning，记录保留，不误称无 warning）。
以上不替代整个仓库冻结 CI，也不声称生产载入。

保留先前证据：初版12项本机8.655s/VPS9.373s通过，但随后同版本
VPS复核12.018s有两个首轮callback等待失败；不是全部版本稳定成功。
没有加长3s等待掩盖，新增上述显式 initial-readiness barrier、延后首轮
maintenance 清 ready，最终两端复核通过。原失败/旧候选保留，未部署。

下一 Root 需要在唯一旧 receiver 空闲/备份边界独立审查 candidate，并
核原 carrier 真正停止、原全部在途/完整结果与 canonical 账本闭合。
`CHANNEL-CHECKPOINT` 的 audit artifact 仍 `migration_ready=false`；不能
自动变为 grant proof，也不能由 PID 消失、socket timeout、TTL 或
“布尔检查全绿”替代独立外部操作/admitted-caller quiescence。

原 canonical authority/state/witness 当前仍在 VPS 同一故障域；本片
没有解决它们的独立可用性、仲裁或可恢复状态，更没有任意分区 HA。
接收器改到 laptop 也不能消除这个依赖。保留
[CHANNEL-CARRIER](CHANNEL-CARRIER.md)、[CHANNEL-HANDOFF](CHANNEL-HANDOFF.md)
及 [COMMUNICATION-CARRIER-HA](COMMUNICATION-CARRIER-HA.md) 的真实验收要求。
