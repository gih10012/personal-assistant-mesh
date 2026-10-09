# PAM-004：模型主导的节点、出口与资源选择

源码核对：2026-10-09。PAM-004a 已实现，PAM-004b 仍是下一步合同；不宣称已具备自主
调度、真实出口执行或全网最优性能。PAM-003 已有目录联邦与隔离重联
证据；PAM-003d 正式双向循环已加载，空目录认证读取连续 `ok`，有内容
目录与故障域观察继续。状态以 [TASKS](TASKS.json) 和
实际 authority 为准，不用本文替代运行证明。

## 最小结论

不新增固定机器评分器，也不把模型退化为给预设路线填参数。
原生 Leader/项目 specialist 阅读当前证据，决定要探测什么、在哪个
节点运行、使用哪种能力与出口、何时等待或改计划。第一片新增的是
**可续接的路由决定与证据引用**；下一片用一个具体已授权出口/目标做
真实验证，复用现有准入与效果账本，不先实现整个通用网络代理。

确定性代码只核对可验证的执行边界：身份、精确授权、资源版本、证据
时效/范围、预算、共享容量、稳定操作 ID 和回执。它不规定模型只能
做什么；Mesh 不拦截原生 Shell、文件、网络、MCP 或平台自身功能。
节点失联仍可在本人授权范围自主联网、排障和寻找新路径。新路径先
通过原生能力验证后再登记受管入口，不要求所有原生工具预先注册。

## 当前源码：可直接复用与实际缺口

| 当前基础 | 可复用 | 不能外推的部分 |
| --- | --- | --- |
| [Registry](../assistant_mesh/resources.py) / 模型 discover、describe、graph | 开放 kind/spec；带单位/采样时间/workload/scope/验证来源的 metrics；精确 grant | 目录 available、health 自报或一次测量不是任意目标的执行证明 |
| [network inventory](../assistant_mesh/network_inventory.py) | 本机只读接口/默认路由与有期限 network.egress 候选 | 不自动登记；无互联网、DNS/策略路由、性能探测；不是已安装出口执行器 |
| [federated_capabilities](CAPABILITY-FEDERATION.md) | issuer/id/revision 隔离的远端公告与连接时效估计 | 当前不导出远端 metrics、graph、grants 或 pool；不能拿投影在当地预留远端容量 |
| [Allocations](../assistant_mesh/allocations.py) | 同 authority 的验证、共享容量原子预留、provider 回执和 unknown 保留 | 不选择最佳计划，不跨 authority 原子准入，也不是 OS/网络硬隔离 |
| [remote_delegate](../assistant_mesh/remote.py) | 模型明确选择 peer；原父任务的持久 child/消息/回执，目的端本地运行 | 控制面失联不证明业务效果未发生，换 peer/new callId 可能重复业务操作 |
| [Routing](../assistant_mesh/routing.py) / [RoutingContext](../assistant_mesh/routing_context.py) | task/Leader-bound 原子证据视图、不可变决定、原执行身份关联；连续 thread 可通过开放 mesh 或 CLI 接入 | 提议不执行；关联不是实际模型选择、计划匹配或业务成功证明；真实出口待 004b |
| [ProviderRuntime](../assistant_mesh/provider_runtime.py) | 私有安装的版本化 callback、effects journal 和真实结果补结算 | 没有内置通用出口 executor；计划不能自动变成任意代理 URL/Shell 源码 |

当前 [Codex.start](../assistant_mesh/codex.py) 的 `catalog-first` 取实时
目录第一项，Worker preflight 按配置账户顺序再尝试可选 Pi；这是已有
启动/不可用兜底，不是完成了模型自主选模型。下一步将当前可访问模型
与实际质量/额度/延迟证据交给模型判断，不能把目录顺序、模型名字或
永久 rank 当作智能选择。此扩展不在本文中冒称已实现。

## 模型应看到的证据，而非一个总分

一个目标首先说明具体 action、输入/输出、数据范围、实际 workload、
目标覆盖和期望结果。候选保留 `(issuer,capability_id)`，不因名称相同
合并；本地 capability epoch 与远端目录 revision 各保留原语义。

| 证据类别 | 必须保留 | 不能替代的东西 |
| --- | --- | --- |
| 本机网络采样 | adapter、sample_time/deadline、route family、接口类型、declared | 默认路由存在不等于目标可达、代理已生效或费用已核验 |
| 请求/性能观测 | metric/value/unit、样本范围、scope/workload、实现 epoch、方法、结果引用、验证者 | 不同单位/负载/目标的数值不能直接排序；旧实现测量不是新实现保证 |
| 路径 | 所选边、端点 epoch、具体目标覆盖、逐跳可达证据 | 控制面 hello/SSH 成功不等于业务出口、模型或目标服务正常 |
| 故障域 | 谁声明/验证了共同依赖，是什么范围和当前版本 | 不同 node/账号别名/出口名称不证明相互独立 |
| 权限/成本 | owner 绑定、精确 action/scope、审批期限、实际新增成本 | available/authorize.allowed 不等于已执行；不在此流程购买资源 |

故障域可以是同一 WAN/代理、同一物理宿主、同一账号额度、同一模型
服务、持久 job authority 或 resource admission authority。它们是不同
依赖：控制连接断了但远端业务仍在运行时，只恢复回执，不重做业务。
当前代码没有可信故障域解析器；`spec` 可以携带开放描述，但自报不能
冒充物理证明。新增 owner-bound 私有 dependency reference 后才可明确
按域去重/验证；未知独立性保持 unknown，不默认视为独立冗余。

同 authority 已有 owner pool/usage_key：同一 allocation 同池同维度
同 usage_key 取最大需求，不同使用相加；不同 allocation 继续实际计量。
模型不能靠造第二个 pool、第二份公告或复制远端配额增加容量。
跨 authority 共享账号/出口暂不靠 gossip 计总额；准入仍留在原资源
owner，跨域 escrow/预留另做合同，不伪造全网原子容量。

模型可同时考虑质量、延迟、可达性、隐私、额度、电量、局部负载和
连续上下文成本；哪些重要由任务与当前证据决定。不存在永久规定
“手机低、云高”或“模型 A 一定优先”。缺证据时可提议一次有边界的
探测，也可选择等证据或改工作分解，而不是编造性能。

## 第一片 PAM-004a：小型路由决定账本

**已实现 `routing_context` / `route_propose` 和薄 CLI/HTTP 入口。**
只做 task-bound 只读 evidence view 与持久 proposal，不在 proposal
写入时执行、登记能力、批准 grant、创建 pool 或修改全局网络。
先复用现有 Registry/Projection 读取；inventory 由节点在本机采样或
作为明确来源的私有报告提供，不偷偷远程扫描采集。

最小数据合同：

```text
decision_id / immutable content hash
job/task binding：authority、原 task、epoch（由认证运行环境注入）
work：action、scope、workload、目标/结果要求、零新增支出边界
evidence_refs：来源类型/issuer、ID、epoch或revision、采样时间、摘要hash
candidates：模型考虑的节点/能力/路径与缺项，故障域来源
selection：模型选择或 waiting_evidence，简短理由与证据引用
execution_link：原 delegate child/message ID 或 local operation ID；未执行为空
observed_outcome：实际读回的结果引用/unknown/review，不是模型口头完成
```

保存可公开解释的简短决定，不要求导出隐藏思维链，不写凭据或账户
原文。资料中的描述/结果仍是数据，不能作为系统指令或新增授权。
新增 proposal 只影响本 job 的计划记录，不取代 goal、native plan、
连续 thread 或现有任务所有权。

当前落地为两个小模块、独立私有表与薄工具接线：

1. 对实际 task lease 原子校验；绑定不可由 prompt 覆盖。
2. 保存有界证据引用和不可变决定；同 ID/同内容幂等、异内容拒绝。
3. 返回只读查询。证据是当时快照，不能当永久 admission token。
4. 执行仍用现有 allocation/remote_delegate；保存其真实返回的身份
   作为 execution_link。若提交或记录回执之间中断，先按原 ID 对账，
   不重新让模型猜一次委派。关联 immutable，不能因 unknown 换引用；
   原执行的 CAS/准入仍留在既有 authority，不在提议里新增业务 CAS。

### 已实现入口与证据边界

开放 `mesh(action,arguments)` 中新增动作，不重建原生 thread/dynamicTools：

| action | arguments | 含义 |
| --- | --- | --- |
| `routing_context` | 可选 kind/issuer/include_unavailable/limit/observation_max_age_seconds | 同一 local authority 事务的有界证据视图，无总分/执行/联网 |
| `route_propose` | decision | 六个必需字段 decision_id/work/candidates/selection/rationale/evidence_refs；对象内容开放，同 ID 同内容幂等 |
| `route_inspect` / `route_list` | decision_id / 可选 limit | 当前 task/Leader fence 的提议与原执行引用查询 |
| `route_link` | decision_id/kind/reference_id | kind 为 remote_delegation 或 managed_allocation；实际本任务、原 actor 身份核对，一次不可变关联 |

HTTP `POST /v1/routing/action` 的唯一包络为
`{task_id,epoch,action,arguments}`，action=context/propose/inspect/list/link。
只允许有 deployment-bound node 的 worker；node/authority 不由模型提供。
`GET /v1/routing/decisions` 是 operator/viewer 只读历史，支持
decision_id/task_id/limit，不接受 worker/agent_peer 借 node 字段伪装 owner。
历史读在任务结束后仍可用，不为模型写操作取消 lease 校验。

旧线程可用原生 CLI：`routing --task-id CURRENT --epoch CURRENT --action NAME
--payload-file /private/arguments.json`；配置与 payload 保持本人所有、0600、
仓库之外。运行期参考给出当前 task/epoch，不输出 credential。
owner 历史 CLI 为 `route-decisions --id DECISION` 或 `--task-id TASK`。

证据视图保留单位、scope/workload、样本/接收时间、epoch/revision 和来源。
可选 age 是 caller 要求的 freshness，不等于 admission；精确截止时刻为
stale，未来样本/旧 epoch 不为 fresh。当前可信验证者、与当前 provider
独立性另列布尔值；保留历史 verification，scope/workload 仍未核对。
cap/pool availability 使用同一 as_of；远端 outer available 才是投影时效，
内层 capability 是来源当时的声明。远端 metrics/pool/grant 不被导入。

local observations/edges/bindings 只覆盖 sampled capability IDs；按数量与
2MiB 总响应截断时显式标明 partial，不假称全网完整。checksum 是当前
authority 生成的视图摘要，但模型抄入 evidence_refs 后仍是 unchecked
引用，未自动核验。共享池显示原 owner 合同与 held/remaining，不累加
同名公告。两类执行候选分别限量，保留原 child/operation 与 actor/epoch。

提议只写 route_decisions；link 只核对真实执行身份，不核验计划因果。
始终 `model_selection_verified=false`、`execution_verified=false`；native
真实选型和独立业务结果另做验收。unknown 先查原 ID，不更换引用、
新建 ID 或调用新 peer 重放；此入口失败也不阻断任何原生功能。

第一轮不需要远端导出全部 metrics。模型可用远端公告发现候选，目的
节点收到明确委派后查询其本 authority 的新鲜观测并自主决定本地实现；
关键缺证据必须报告 waiting_evidence/rejected，不能把声明升级 verified。
若委派会执行实际业务，它已经是一个可能产生效果的执行阶段，不是
“因为主节点没有 reserve 所以肯定没开始”。

## 第二片 PAM-004b：一个真实目标的出口 canary

选当前 owner 已授权目标的一个有界只读请求，定义实际 scope/workload、
预期内容/摘要与结果引用。可以由模型在两个已 enrollment 节点/可达
出口之间选择；**不是**先替换系统默认路由、代理、VPN 或部署付费网络。
业务请求路径与控制面路径分别记录。使用现有连接或节点自主的原生
能力，实际平台的选择方式由节点 agent 改装，不写死 Linux 接口命令。

将成功验证的特定动作安装为 owner-trusted、版本化 managed adapter，
明确输入/输出、目标范围、有界时间/字节和真实 quiescence。adapter
内部执行路径不能因模型传入任意 URL/凭据扩权；跳转/数据外流范围
也须包含在实际授权中。读请求也会被目标服务观测到，不假设绝无
外部效果或现有流量免费；不新开计费资源或读取其他账号。

先做独立验证者的具体目标 probe，再使用目的 authority 上现有
`Allocations.reserve`。该准入当前要求：

- capability/action/exact scope、workload 与当前版本匹配；跨 principal
  有 owner 批准的有限 grant。
- 至少一条新鲜 `verified` observation；验证者在部署 trusted_verifiers
  中且不是 provider 本人，evidence 精确匹配 scope/workload。
- route 如存在，逐跳匹配边/端点版本与具体可达 observation；同一个
  authority 的 owner pool/binding、共享瓶颈、零新增支出均有效。
- 当前 task/Leader fence 实际有效；Worker 注入 reserve 的 task 身份，
  不把父 authority 的 task ID 冒充为目的 authority 本地 task。

provider accept/start 与 callback effects journal 原样复用；结果引用
与独立 owner 参考答案核验后记录 outcome。自测或 provider settlement
仍不自动成为独立性能证明。一次 canary 只证明这个目标/负载/时刻，
不是完整互联网或全网最佳路线。

## 改路：只在知道旧阶段未起效时开始替代

同一个 job 可以有多个有版本的候选计划；模型可以改变判断，但不能
改变已开始操作的事实。新决定可以 supersede 旧计划，不能重命名旧
效果使其“消失”。只影响相关业务/共享资源，其他独立本地工作仍继续。

| 已知状态 | 允许的下一步 |
| --- | --- |
| 仅草案/只读证据不足，无提交 | 改计划、补边界清楚的 probe 或等待 |
| 准入明确拒绝且此前没有不确定提交 | 记录拒绝理由，再提议另一候选 |
| 已 reserved、确定未 accepted，原 authority 成功取消/过期或所有相关 dispatch 明确 decline | 记录旧操作身份与静止/未接收证据，再新建替代 operation |
| delegate/accept/start 响应丢失、active/unknown、控制面失联 | 查询原 child/message/operation ID，保留占用和效果不确定性；不改 peer/new ID 重做 |
| 原结果实际完成且验证失败 | 保留结果；由业务语义/本人授权决定修复或补偿，不把它伪装成未执行 |

`reserve` 相同 operation ID 但不同 plan 本来就冲突；不能改旧内容。
accepted/running/unknown 不能因 TTL、节点/模型变更或进程消失释放；
必须真实 quiescence + 原结果/回执对账。恢复原生 thread 仍用原 context，
不靠切 harness 重放已起效 turn。换模型前的实际额度/认证检查不替代
这一边界；未知额度也不等于禁止原生推理或工具。

## 只做这条链路的验收

1. 模型实际查询证据，至少比较两个可区分的候选或明确报告证据不足；
   保存稳定决定 ID、选择理由/证据和真实 dispatch 身份。脚本固定选择
   某个节点的 fixture 不能冒充模型自主选择。
2. 过期/旧 epoch/错 scope/自验证观测被现有准入拒绝；另一个适用候选
   可以由模型重新选择，但拒绝必须证明确实未提交，而非 ACK 丢失。
3. 两个能力声明同一共享瓶颈时不重复增加容量；未知故障域不被报告
   为独立冗余。对该范围验证即可，不先补完全网拓扑。
4. 对一个 owner 授权目标真实执行、独立核对结果；记录平台、路径、
   样本单位/workload/时效与实际效果 journal，不打印秘密。
5. 用 fixture 覆盖 ACK 丢失、cold resume、same-ID 查询与 unknown 容量
   保留；真实中断注入须另获当前部署范围内安全授权，不为验收破坏
   活跃业务。真实出口/模型/故障证据与 fixture 分开。

PAM-004a 已有 proposal 表/API、工具和 CLI；测试、发布、正式加载、
真实模型使用分别见 PLAN/TASKS/ACCEPTANCE，不将 fixture 或提议当作
004b 出口验收。Root 维护执行证据，不因本文完成就将 PAM-004 标记完成。
异构节点全适配、远端观测 feed、
模型选择专用入口、跨 authority escrow 和全局最优性能留待具体后续，
不阻碍第一条有结果的模型选择链路。
