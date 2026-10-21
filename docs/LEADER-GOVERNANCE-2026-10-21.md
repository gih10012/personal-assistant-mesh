# Leader任务治理与开发/验收节奏

版本：owner-governance-20261021-v1。本人已批准实施；此合同覆盖旧的
开发Goal暂停与逐片即时实测排程，不恢复评教。文件存在不等于运行
agent已核读、策略已安装或任务已真实交付。

## 创建任务先评级

每项业务任务和独立执行子任务记录难度、依据、风险、优先级、选择的
model/effort和quota策略。后三者不由难度标签替代。

- D1：常规或多步但约束明确的非开发工作，默认gpt-6-luna/max。
- D2：边界明确、可局部验收的一般开发，默认gpt-6-luna/max；发现
  架构、安全、状态一致性或跨模块不确定性时重新评级。
- D3：高级开发/架构/安全/复杂一致性，选择实际支持的合格强路线，
  当前优先gpt-6.1-sol/high，最深复杂性使用其支持的max。

这是本人授权的Mesh约定，不是伪称OpenAI有D1/D2/D3官方评分。
依据：[官方选模建议](https://learn.chatgpt.com/docs/model-selection)、
[实际推理等级](https://learn.chatgpt.com/docs/agent-configuration/subagents#reasoning-effort-model_reasoning_effort)。
保留3–5天前沿资料更新，按实际路线质量/现金价/额度/工具可靠性判断，
不设永久模型白名单；对D3不得仅因低价或免费跨过质量下限。

## 额度与续接

D3合格模型/已授权候选额度不足时等待恢复，绝不自动派给免费或较弱
模型接管。未开始native turn可选已有授权的合格账号，不新增key、付费
credits或消费reset。实际模型目录校验effort；不靠prompt自报生效。

保留真实quota窗口长度、usedPercent、resetsAt与观察时间。只有可靠
耗尽信息才判耗尽；unknown不等于无额度。按真正reset安排安全下一步，
不一分钟重建app-server累加epoch。明确paused不自动解除；turn已开始
而效果unknown时只核原回执/原case，不因quota恢复、模型或节点变更重跑。
既有side-effect guard和原任务/operation/thread身份保持。

## 日常开发和Leader自我debug

允许日常开发、静态检查、有限本地单测、编译及单测通过后的先部署。
Leader可查看受管组件状态/日志，分析故障、修改相关代码、单测、部署、
小范围重启或回退，并保存修复/版本/原因。不中断无关业务，不删除
记忆/恢复旧数据库，不以debug名义重放unknown或做重复模型对比。

## 集中实测与验收

Root、Leader和各agent均把集成实测、模型对比、故障演练和整体验收
放入Leader统一的持久队列。通常每周一个批次；优先真实周额度reset
之前24小时，缺可靠reset默认周日20:00 Asia/Shanghai。有待验项目及
资源才安排，不为消耗额度制造试验；同代码/证据不重复做无关全套。
日常真实本人业务、被动运行观察和必要debug不是合成验收。

批次保存项目/用例、代码版本、原操作ID、所需节点/模型/本人配合、
额度预算、预定时间、执行权、结果/证据与取消状态。分区时不得两边
重复执行同一批有外部效果的用例；无法证明独占就留待协调。暂缓验收
不阻挡日常开发/单测/安全部署，不冒称未部署功能已完成。

## 工作空间由Leader主导

本人指定真实网内Leader为工作空间项目owner：自己拆分任务、决定
分支负责人、委派开发、汇合改动和自我debug。Root提供合同/可用能力、
协调和共享生产串行写入；不代做Web后宣称Leader开发。工作空间需求见
[阶段合同](STAGE-WORKSPACE-2026-10-21.md)。原生Shell/网络/文件/MCP
保持可用；Mesh新增受管入口不形成宿主全局黑名单。

评教保持暂停，Root不再处理，等本人直接向Leader下达业务指令。
Claw负责自然沟通、准确时间、必要回执和媒体，不刷技术探测日志。
