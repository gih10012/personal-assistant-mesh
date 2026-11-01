# Free 第三票：成熟 Raft 核心移植决策

2026-11-01，独立研究分支，基线 `75ffd06`。本轮没有生产、账号、GUI或部署操作。
已读 S2 `7764517` 的 `control_quorum.py` 和交接文档；原任务、业务、native线程、
unknown、43任务图和独立暂停的评教均不变。

## 决策

**值得做一段受限构建/恢复切片，尚不能宣布 Free 三票可部署。** 推荐统一使用
TiKV `raft-rs =0.7.0`（Apache-2.0）的 `RawNode`：PC、VPS为 native 宿主，
Cloudflare SQLite DO为 WASM/JavaScript 宿主。三个成员都运行同一核心、保存完整
控制日志和状态机；DO不是只存一个CAS的见证。共识核心不 fork，不自己实现Raft。

这是一条可解释的工程推断，不是上游已认证的 DO 产品。`RawNode`由宿主主动驱动
tick/step/Ready，没有必须保活的 Tokio 任务。版本的默认日志功能会带终端依赖，
应关闭；随机选举使用rand，WASM需要显式启用getrandom 0.2的js后端。
[版本/依赖/许可证](https://github.com/tikv/raft-rs/blob/v0.7.0/Cargo.toml)、
[选举随机源](https://github.com/tikv/raft-rs/blob/v0.7.0/src/raft.rs)、
[getrandom 0.2的WASM契约](https://github.com/rust-random/getrandom/blob/v0.2.16/src/lib.rs)。

`etcd-io/raft`也将存储和传输留给宿主，但Go/WASM需要额外Go运行时接线；本次不为它
另做平行实现。OpenRaft现行主线已进入0.10 alpha和可替换异步运行时，DO休眠仍需自建
生命周期适配。维护优先时先验证一个手动事件核心，避免同时维护两个路线。
[etcd核心边界](https://github.com/etcd-io/raft)、
[OpenRaft主线依赖](https://github.com/databendlabs/openraft/blob/main/openraft/Cargo.toml)。

## 现有 S2 的复用边界

现有适配器要求真正的三成员etcd。**raft-rs成员不能加入etcd集群**，已有Free relay
也不因加一个DO类而成为第三票。移植若成立，三个控制成员须统一换为新宿主，另写
窄`RaftControlGateway`，在一个已提交状态机命令内执行compare、受保护值、receipt、
新head等变更，再复用现有应用角色/epoch/unknown规则；不伪装成etcd member。

业务完整账本仍由PC/VPS的S1副本承担，DO只复制小型控制状态；DO失效时PC与VPS通过
独立直连路径互通，以原固定3票的2/3推进。若两者的全部通信也绕同一Cloudflare relay，
Cloudflare故障仍会隔断2/3，不能称已消除该单点。Raft Leader可以变化，且不等于
业务coordinator/native/OAuth/communication的owner。

## Free 额度与生命周期：现行官方资料

以下页面本次实际浏览；公开文档不能证明当前账号的剩余额度或已部署资格。

| 项目 | 官方事实与实现约束 |
| --- | --- |
| Free可用存储 | SQLite DO可用于Free；账户总5GB，读500万行/日、写10万行/日。删除、索引更新及setAlarm也消耗写行。日志压缩和状态更新需计入。 |
| 请求与时长 | DO 10万请求/日、13000 GB-s/日；超额失败，不自动升级付费。每天UTC零点重置。按128MB整日活跃约11059 GB-s，单DO已占大部预算。 |
| Worker/WASM | 当前Workers limits主表为未压缩64MiB、128MB内存、启动1秒，Free入口CPU10ms；WASM算在bundle内。实物必须量大小与冷启动，不能沿用旧3MiB说法。 |
| DO CPU矛盾 | DO limits写默认30秒，且同页说适用Workers计划限制；Workers Free表是10ms。不能据30秒当Free承诺。首切片保守要求每次有界step/恢复满足10ms，真实Free验证由Root决定。 |
| 对象存储矛盾 | 同页通用表10GB/对象，但FAQ说Free写满1GB报SQLITE_FULL；首版按每对象1GB保守设计，不依赖扩额。 |

[DO价格/配额](https://developers.cloudflare.com/durable-objects/platform/pricing/)、
[Workers限制](https://developers.cloudflare.com/workers/platform/limits/)、
[DO限制及FAQ](https://developers.cloudflare.com/durable-objects/platform/limits/)。

DO允许被休眠/驱逐，内存丢失；constructor要从稳定存储重建。普通定时器妨碍休眠，
不能当永驻保障；alarm为至少一次执行并有限重试，不能把alarm视为可靠周期时钟。
可由仍在线的native成员以5秒一个有界批次唤醒DO，合并往返消息；两native各一批
约34560次/日，尚需为选举、业务、恢复、既有relay和重试留额。不要对每次heartbeat
写新业务日志或无条件重写HardState。接近额度时可将DO视为不可用，PC/VPS继续2/3；
若只剩一native加DO而DO额度耗尽，唯一权工作必须等待。
[生命周期](https://developers.cloudflare.com/durable-objects/concepts/durable-object-lifecycle/)、
[alarm语义](https://developers.cloudflare.com/durable-objects/api/alarms/)。

上述5秒/预算是设计计算，不是延时SLA。延迟、暂停、乱序、重复消息只能影响活性，
不能凭tick次数授予提交权；醒来先载入term/vote/log再处理任何消息。core可自行选举，
静态优先级不把某节点指定为永久leader；不要用固定DO leader绕过宿主设计。

## 两种宿主，一个持久化契约

共同crate仅包含固定版本Raft、protobuf编解码、受限输入输出及控制状态机。
native用本机SQLite事务和耐久提交；DO用JS的同步SQL接口，WASM导出有界step/Ready
数据，JS执行SQLite事务并等待`storage.sync()`后才确认给核心/发送依赖落盘的响应。
不需要WASI文件系统、线程、TCP监听或在WASM内运行SQLite。
[Cloudflare WASM限制](https://developers.cloudflare.com/workers/runtime-apis/webassembly/)、
[SQLite transactionSync与sync](https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/)。

按0.7.0的API实施：同一Ready的snapshot、entries、HardState先耐久；之后才能发
persisted_messages并advance_append。再处理LightReady的commit/已提交entries，
事务化应用状态、幂等receipt和applied索引后advance_apply；成功回执只在应用耐久后
产生。保守宿主可连允许提前发的messages也延后。LightReady的commit_index上游不强制
落盘，但本宿主可在应用事务中保存，用于清楚重建进度。
[精确Ready/LightReady契约](https://github.com/tikv/raft-rs/blob/v0.7.0/src/raw_node.rs)。

必须串行化一个RawNode的整个Ready周期，包括await同步点，避免JS请求交错。存储失败
或CPU终止时丢弃未确认内存实例，从耐久状态重建；不得继续使用比磁盘超前的内存。
snapshot安装须原子替换FSM、ConfState、snapshot metadata、applied与可保留日志后缀；
禁止先删旧日志再落新snapshot。小日志/小快照有界恢复，否则10ms/128MB可能不成立。

term/vote必须先耐久再回投票响应；日志先耐久再回Append成功，不能先发送再后台保存。
不要恢复旧备份/PITR到同一投票ID：这会回退term/vote并破坏一次一票。丢失投票状态
是成员更换事件，不是自动空库重启；需受控重配，v1直接拒绝而不自建新集群。

配置固定三个非learner voter、独立cluster UUID和成员公钥，bootstrap只一次；每端
pin完整membership digest，禁运行期ConfChange/自动缩容/新空集群冒充原集群。peer消息
认证并绑定from/to/cluster，保留Raft处理乱序重复的语义，不自行凑两个ACK就提交。

线性读使用`ReadOnlyOption::Safe`与唯一context的ReadIndex，收到ReadState且本地
applied达到该index才答；旧leader缓存、heartbeat和租约不能替代多数派确认。显式
`check_quorum=true`、`pre_vote=true`，限制未提交字节和每次Ready大小。
[配置契约](https://github.com/tikv/raft-rs/blob/v0.7.0/src/config.rs)。

这些是宿主正确性要求，不声称一张架构图已经证明实现正确。原S2对native/外部效果
的约束仍成立：控制日志提交不会自动fence现有SQLite、Shell、iLink或OAuth提交点。

## 最小交付切片及停点

Root已协调：先在同一独立分支做下面的局部构建/恢复，暂不部署Cloudflare。

1. 固定`raft = =0.7.0`、`default-features=false`、`protobuf-codec`；WASM目标显式
   `getrandom = =0.2.16`加js。依赖锁定Cargo.lock，native和WASM共用核心；提供
   wasm-bindgen导出及最小JS宿主接口样例，不造网络/集群管理框架。
2. 实际产出native及wasm32-unknown-unknown release artifact，记录工具版本、大小、
   SHA256与直接依赖许可证；运行一次真正RawNode的消息step及term/vote/log恢复用例。
   构建/检查均走Click Evidence；若宿主不识别，不改包装规避。
3. 交付后由Root串行决定Free隔离DO启动：实际冷启动/CPU、持久响应顺序、杀死重建及
   PC/VPS保持2/3。只有这轮通过后再接控制FSM/原S2接口和生产提交点。

截至本文初稿：只完成官方来源调研，没有编译、部署或真三票验收。后续实物证据追加
在本文件独立小节；cargo check不能冒充DO启动或部署证明。

### 构建切片停点

Root随后授权准备同核最小构建/宿主样例，源码在`deploy/raft-portability`，没有接入
任何已有入口。真实RawNode投票/耐久恢复用例和DO持久屏障样例已写，但全部未编译。
Click拒绝`cargo build --release` batch：`neither read-only nor a recognized check`，
未启动任何build/test。带局部代理前缀的先前调用未被host改写，报`click-gate: command
not found`，同样没有Cargo执行。未包装绕过；exact argv和限制见该目录README。
设计文档首提交`464c631`可独立采用，未编译切片需要宿主开放正式Cargo检查能力后继续。

若该切片需要fork共识核心/改写安全算法，或有界恢复无法满足Free限制，则停止此
路线。零新支出替代是继续现有S1双完整副本和受控交接，等待现有合法独立计算节点
承载第三个etcd member；没有该节点时保持单票故障不自动授唯一权，不冒称自动HA。
