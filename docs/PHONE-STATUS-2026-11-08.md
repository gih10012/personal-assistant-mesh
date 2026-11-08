# 手机本轮状态与交付条件

本人已恢复原生 Goal。当前先完成手机自治与无线维护，随后继续 VPS 单点治理；
学校评教仍独立暂停。原43项协调任务、私有业务身份和未知回执保持。

## 已有真实设备证据

- 原商业 API26 SDK 构建及原合法签名，覆盖安装保留应用身份与数据。
- 手机自己领取、执行登记挑战并完整回传；独立 oracle 核对49,152字节通过。
- 原 Registry 注册及独立查回完成，成员凭据存入应用 Asset。
- 真实手机经自己的网络建立出站会话，应用显示 online；这不是后台常驻证据。

实际安装发现并修正两处适配：字符串摘要直接使用 TextEncoder 的精确 UTF8
字节，避免 native Buffer 内存池视图差异；普通应用不设置需要受限权限的
Asset IS_PERSISTENT 属性，即使赋 false 也可能触发权限校验。

第二个独立任务已由真实网内 Worker准备新的输入、预期完整结果及精确授权
请求。分配后发现手机 WorkProtocol 将 authority 的 `node:<id>` provider
误当裸 node ID；修正应保留原 wire principal，不能让服务器伪造设备执行。
原分配和未执行/到期状态私有保留，新的实际验收需有明确后续授权。

## 本人新增要求及分工

1. 手机操控：基于实际公开 SDK 的无障碍服务与截图授权做适配，提供有界
   inspect、带 revision 的对象引用、短屏幕租约、动作及人输入礼让。
   弃用 API 不等于已移除；必须由本人在设置启用后取得真实 callback/root
   证据。没有权限的动作明确不可用，不把合成测试当跨应用控制成功。
2. 实况窗审批：请求绑定原 task/operation/revision/nonce/截止时间。拒绝、
   到期和未知不得自动放行。LiveView 权益未开通时准确报告；系统通知可以
   打开审批页，但不能称为无需切应用的后台批准。
3. 后台：区分正常 UI、合法系统服务、系统有界维护 callback 和 suspended。
   用户无需一直展示 Mesh 页。没有实际常驻资格就排队，不伪造音频、传输、
   前台状态或无线 HDC；callback 开始、停止、完成、未知分别持久记录。
4. 自维护：确定性监督负责已有授权的签名数据同步及原回执核对。需要认知
   时进入原 Mesh 任务队列，设备隔离记忆、D1/低难度和合格低成本路线。
   维护 worker/model 尚未接通时保持 queued/not_configured，不冒称手机常驻 LLM。
5. 无线升级：配置、Markdown Skill、文件走可信签名、版本/hash/大小校验、
   完整下载后原子启用及回退。原生 HAP/权限变更走合法同签名升级；核 AGC
   指定设备发布的实际资格、appIdentifier、签名连续性与确认安装，保留原
   node、Asset、业务 journal。成功前不承诺今天永久是最后一次 USB。

Root 负责共享入口、生产、账号、签名与最后真机安装；开发专员分别负责
control/LiveView、maintenance/native upgrade、phone 后端与隔离维护任务。
独立 worktree 并行，合入后统一真正 import 新 ETS 的商业 SDK 构建。

## 本轮真实验收

安装前：代码、固定配置迁移、可信更新包、可见状态与回退准备完成，再经
Claw 请求接线。安装时保留同一身份及 Asset，实际启用/探测无障碍与通知，
让另一非敏感应用处于前台，验证可观察和审批后的动作；权限不足按实际
回执收敛，不声称开通全部权限。

之后验证独立 Mesh 任务完整结果、合法系统后台机会与维护记录、实际维护
任务原路由，以及同签名无线递增版本覆盖安装。每一项完成即可报告，不重复
大套件。源码、合入、部署、设备可见结果和已验收分别记录。

## 官方依据

- [AccessibilityExtensionAbility](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/js-apis-application-accessibilityextensionability/)：与实际商业 SDK 一起核公开且已弃用的接口，不据旧文档承诺新机权限。
- [系统 Agent GUI 服务](https://developer.huawei.com/consumer/cn/doc/service/agent-gui-0000002680521240/)：伙伴开放与申请要求，不能替代任意应用的通用 GUI 授权。
- [Live View Kit](https://developer.huawei.com/consumer/cn/doc/harmonyos-references/liveview-liveviewmanager/)：实际权益及 WantAgent 交互。
- [应用内更新](https://developer.huawei.com/consumer/cn/doc/HarmonyOS-Guides/store-update/)：在架应用的官方更新路径，不能冒充当前 debug 包已支持无线升级。
- [指定设备发布 FAQ](https://developer.huawei.com/consumer/cn/doc/App/agc-help-internal-test-faq-0000002295372101/)：合法测试分发与其实际限制。
