# 从 DeepSeek Super-agent 吸收的编排原则

来源是本人提出的 [Issue #1](https://github.com/gih10012/personal-assistant-mesh/issues/1)。
本轮阅读官方原型 README、000/001/002/003/007 文档，以及 Engine、Intake、
Effects、DshExecutor 的相关源码。以下是设计采纳与现有系统映射，未部署新的
SuperAgent 编排器。当前交付顺序仍为鸿蒙真机入网、消除 VPS 单点。

## 产品定位与职责

AMITA 面向本人提供持续的通用助理。入口/Router 维持对话、识别任务关系、
处理简单完整工作并协调其它 Worker；研发、研究、校园/文件业务、自运维均
属于可委派工作。自运维 Worker 负责获授权的修复/升级，不代表整个顶层助手。
复杂规划选择合格模型，简单沟通优先低成本路线，沿现有模型治理动态选择。

Router 是智能编排职责；Mesh Leader/coordinator 是有任期和作用域的协调角色；
authority、原生会话 owner、OAuth writer 和通道执行权分别维护。实现中可以
由同一进程承担多个职责，但不能因承担 Router 就自动获得其它角色的权利。
模型不可用时，确定性的通信、心跳、发现、队列、租约及基础恢复继续运行。

采用原型[哲学文档](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/000-philosophy.md)
提出的人类注意力成本视角：关注本人为了完成一件事需要补几次上下文、催问
几次、手工恢复几次，而非只统计 agent 数、工具次数或日志量。Claw 日常只在
确有交付、必要决策或实质阻塞时通知；避免模板回执、重试计数和过时答复。
工作台首页优先呈现待本人处理事项，仍保留本人要求的完整任务树、子任务、
文档、计划和 agent 活动视图。

## 值得直接吸收的机制

1. **已有任务的后续消息直接到原 Worker。**
   原型 [Intake](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/src/core/intake.ts)
   先按事件身份去重，再按订阅关系投递。已经有归属的补充说明不必反复调用
   Router；Worker 发现超出原任务范围时再交回协调。相关上下文可以扇出，
   任务修改和外部操作仍依据原授权，不能把订阅当授权。
2. **长期任务与执行尝试分开。**
   [领域模型](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/001-domain.md)
   区分 Ticket、Assignment、Session。AMITA 继续使用原 task_id、epoch、
   原生 thread 和 operation 回执表达这些关系，不复制一套 Ticket 真相源。
   模型一轮结束、进程退出、业务完成和结果投递是不同事实。
3. **状态提交后才发起外部动作。**
   [Engine](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/src/core/engine.ts)
   在事务内更新领域状态；[Effects](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/src/app/effects.ts)
   持久记录后再执行。其 seq/generation 还避免旧执行结果删除后来更新的队列项。
   AMITA 将这一原则落在已有 inbox/outbox、原消息/操作ID和任务epoch上。
4. **异步工作保留可追踪句柄，人类输入优先。**
   [执行文档](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/003-execution.md)
   将长工具和后台任务与前台对话解耦，并为人的补充输入预留调度机会。
   AMITA 依据真实工具/远端作业句柄跟进，观察超时不等于执行停止；既有任务
   的补充、取消和提问不能长期排在批量新任务之后。
5. **已送达、已进入会话、已被处理分别记录。**
   原型区分 Inputs 的投递与进入 Session 历史后的处理。AMITA 也应保留来源
   时间、接收时间和投递/消费回执，沿原 message/request_id 对账。来源时钟
   不作为跨设备最后写入获胜规则，摘要更不能覆盖原始历史。
6. **主动性来自已授权规则，监督关注真实进展。**
   [授权文档](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/002-authorization.md)
   区分明确请求与推断候选，支持 observe/auto。AMITA 可先观察新主动规则的
   效果，再在授权范围启用；本人已批准的当前 Goal 无需重新降为逐次确认。
   语义监督按任务、回执、产物和阻塞变化触发，基础保活不消耗推理。

## 映射到现有 AMITA

| 参考概念 | 在 AMITA 的位置 | 增量方向 |
| --- | --- | --- |
| Source / Trigger | 已有入口、inbox、conversation/message/request 身份 | 统一带来源、时间、去重键的事件输入与游标 |
| Thread / Subscription | 原 conversation、task 关系与会话消息 | 持久绑定及直达 Worker；context-only 与任务控制分开 |
| Ticket | 原业务 task 与父子依赖/结果 | 继续作为用户目标的权威记录 |
| Assignment / Executor | task epoch、节点、runner、原生 thread 与执行回执 | 完成/等待/未知分别表达，沿原身份恢复 |
| Mandate | 原 owner 指令、scope、受管能力 grant 和操作权限 | 让编排读到原依据；接收新上下文不扩权 |
| Report / Attention | 原结果、interaction、outbox 和工作台 | 以结构化事实呈现待人、待设备、待模型、运行与交付 |

这些是复用映射，不代表全部订阅、调度和监督行为已经实现。特别是本次
真实 S2 案例：业务 task 已完成，但本地 runner 因终态标记缺失留在 unknown。
已针对这个缺口修复生命周期输出，并通过原 authority 追加关闭复核、协作退出
旧 runner；没有把完成的业务重新执行。该案例说明多个生命周期需要明确对账。

## 需要按 AMITA 边界调整的原型行为

- 官方 [README](https://github.com/deepseek-ai/dsh-superagent-prototype) 将其定位为
  单 GitHub 仓库的研究原型，并说明公开版本与内部版本/文章设计有差别。
  [架构文档](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/main/docs/007-architecture.md)
  的单 SQLite writer 和目录锁可用于一个宿主，不证明跨设备多数派、完整副本
  或 VPS 退出后仍可运行。AMITA 的共识、数据和通道单点治理仍须独立交付。
- 原型在一定背景运行期限后停止 shell jobs，并在部分失败后重启 Assignment。
  AMITA 的真实外部操作必须沿原句柄、operation 和回执核对。活跃 native 或
  unknown 不能仅凭超时、429/5xx、进程心跳失败就杀掉重做。
- 原型的 Effects/Channel 重试需要结合目标系统语义采用。AMITA 的 iLink
  unknown send 仍只查原 client ID；没有幂等或失权证据不自动换ID补发。
- 原型跨 Source 身份关联可以由提示描述；AMITA 的跨设备成员认证、受管
  资源授权和远端身份绑定继续使用实际验证，不从自然语言昵称推导权限。
- 原型的 Policy 更改通知及人类消息自动恢复规则，需要服从 AMITA 已有
  owner pause/revoke。停止的任务不会被 CI、新消息或后台监督自动解禁。
- Inline & Spill、Chain of Recall 和 Watchdog 可作为上下文/监督设计候选；
  本轮阅读不能证明公开原型已实现文章讨论的完整版本。AMITA 先复用完整
  native 历史、产物索引、任务关系与回执，摘要用于定位，原始证据继续保留。

## 后续最小实施切片

在当前手机/HA阶段基础上优先补齐：**同会话多任务的事件归属与持久订阅，
已归属补充直达原 Worker，Router 保持可响应，按原入口低打扰交付。**
沿已有任务、会话和效果接口实现；无需新增一个独立任务数据库或更换 Codex。
自运维与研发/研究等走同一任务生命周期，各自获得所需作用域的能力。

判断它是否有用的实际场景：本人同时布置两件事，再补充其中一件，另一件
继续工作；模型等待时查询和排队仍正常；待设备任务在真实接回后续接；本人
停止某一任务时，其余独立任务不受牵连。这里只记录未来验收方向，本轮不
启动新模型试验、重建原43项协调任务或改变它们的依赖。

阅读定位（GitHub 返回的文件 blob SHA）：README `39e1c4af122f11dd72ac0d2b34b79d6f8ae259ea`；
001 `fd5300132e85f69ab794bbd3685db2c4fd87a5e0`；003 `6cb33a88c5d728dd4c9e72cae5f8d5c22fd82d02`；
Engine `c2198adf9c7a10837bec5f4789129bf576e39395`；Effects `9255cda8f7a63ed0e7ab70bb023a5927fe5218d5`。
