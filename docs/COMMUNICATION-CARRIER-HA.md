# 沟通能力多节点承载（PAM-009）

2026-10-10 设计与审计，**尚未实现/部署接管**。ClawBot 为 owner↔Leader
沟通能力，本机/VPS/未来节点可承载，与 Leader 工作区分离。当前生产
仍 VPS 单收发，节点通知走原 relay。

server.py 在单 authority 启动 poll/send；store.py 的游标、inbox/
context/task 与 outbox 防重只在一份 DB。notification_relay.py 的
原 route/unknown 不能换目标重发。新建本机 DB/复制 token 不是接管。

Leader 单 authority 租约不是跨节点 channel 租约。iLink 没有 Mesh
epoch fencing；ping 失败不证明旧发送器停止。仅两互相不可达节点
不能兼得任意故障接管和唯一账号副作用，需要可证明旧操作停止/到期
或独立仲裁/外部排他。不能用测试 quorum 冒充现网 HA，不新增付费。

## 最小合同

| 对象 | 保留/核验 |
| --- | --- |
| 候选 | owner/account、私有认证引用、实际 runtime/可达性；候选不等于 holder |
| 通道任期 | account/holder/epoch/deadline/checkpoint revision，独立于 Leader lease |
| checkpoint | 游标、消息 ID、context 版本、原 task 映射、outbox/client ID、intent/unknown/回执 |
| 操作权 | poll 前、返回持久化前、send intent 前核验任期；失权不发新请求 |
| 在途效果 | 观察终止/原 ID 对账；新任期不授予 unknown 重发权 |
| 路由 | 保留原消息/canonical task/原 authority 映射和效果账本；迁移须有 handoff 证明，恢复逐条对账 |

先受控交接：准备候选→冻结新通道操作→在途收敛→封存 checkpoint→
新任期→加载→同 ID 续接。再做自动观察和有安全条件的故障接管。
若旧 holder 无法确认停止且无外部排他，备用仍可用其他入口接受
任务、自主工作/寻网，不能谎称同一微信通道已无脑安全接管。

收到过的旧消息不能在另一 authority 重建可执行 task；只有消息 ID
相同不够。未决消息可分区暂存/核对，不能当新授权重做旧业务；已有
迁移合同或明确的新本地工作仍可执行。这不禁止节点独立处理新任务。

任期控制只管 Mesh 共享账号能力，不封禁原生 Shell/网络/其他入口，
不加主机白名单。既有授权一次配置后日常自动使用，不逐条人工审批。

## Tasks 与有界验收

- 009a：候选承载合同、本机/VPS 探测；凭据在私有部署层，不启动
  第二 receiver、不公开聊天。
- 009b：版本化 checkpoint 和持久交接/回执；旧快照 pending 不当
  新消息，应用摘要不替代连续原生历史。
- 009c：单账号承载任期与失权排他；选择可验证的最小仲裁，明确
  分区可用性，不预先写完所有平台/故障。
- 009d：隔离 ACK 丢失/崩溃/分区/重入网后，实际受控 handoff；一条
  合成消息只建一个原 task，发送按原 ID 核对，独立读回。再启符合
  相同合同的自动接管，不为测 HA 搞坏当前正常入口。

安装、发送成功、租约到期各自不等于容灾验收；还要旧 holder 失权、
账本/上下文连续和未知不重放。Chat/终端可为独立兜底，不冒称手机
已收到微信。[当前通知合同](OWNER-NOTIFICATIONS.md)继续有效。
