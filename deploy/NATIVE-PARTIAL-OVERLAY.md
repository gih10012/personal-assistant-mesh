# S3 additive receiver / worker namespace

此 builder 只组装源码。不会 import 旧 launcher、打开 ledger/config/token、
连接服务或操作真实任务。构建与生产安装/复核/结果投递是不同步骤。
先交付无模型的 owner-reviewed completion，不等待 worker 升级。

## Receiver：保留实际旧 core 与 ready 链

`build-native-partial-overlay.py receiver` 接受本地捕获文件及其精确 SHA256，
另外要求每个文件在目标环境的显式绝对 installed 路径。可用
`--runtime-pins` 加入其余实际 namespace/launcher 依赖的 path → SHA256
JSON。构建期核捕获文件；生成 launcher 在 import 任何 incumbent 前再核
全部目标源文件以及本次 additive 模块 hash。路径/源/hash 不匹配就拒绝。

实际已捕获 source-v3 的入口属性为 `_entry`，不是猜测 package main：

- launcher 捕获：`/var/tmp/amita-business-replica-20261029/receiver-source-v3/launch_receiver.py`，
  SHA256 `a9b73900b491bb9169d48660aa31e7a9186ce3ce0ad144cad7f0b7f83631b008`。
- namespace init 捕获：同目录 `assistant_mesh/__init__.py`，SHA256
  `1dc0017348ee917cae4dcc5eaba2b0eb59c644c4098554e89f869f5d96363f6e`。
- 实际旧 Store 捕获：`snapshot/node/store.py`，SHA256
  `7d8f750e8dfe10045ca97b52ac0ff853d645ea66b4b01f9a9aaf60fb48df5440`。

目标 installed 路径由 Root 按本次实际进程来源填写，不把本地捕获目录
误当 VPS 安装路径：

```sh
python3 deploy/build-native-partial-overlay.py receiver \
  --destination /private/new-s3-receiver-source \
  --package-init /capture/assistant_mesh/__init__.py --init-sha256 EXACT_INIT_SHA256 \
  --installed-init /target/incumbent/assistant_mesh/__init__.py \
  --launcher /capture/launch_receiver.py --launcher-sha256 EXACT_LAUNCHER_SHA256 \
  --installed-launcher /target/incumbent/launch_receiver.py \
  --incumbent-store /capture/store.py --store-sha256 EXACT_STORE_SHA256 \
  --installed-store /target/original/assistant_mesh/store.py \
  --entry-attribute _entry --maintenance-config /target/private/native-maintenance.json
```

输出仅含 namespace init、S3 service/adapter、从固定源提取的精确 quiescent
helper 和新 launcher；不复制或替换旧 Store/server/Worker/entrance/sessions。
旧 Store 只安装该 helper，保留同一 class、constructor、transaction、
claim/update/continue_task 方法及旧 Conflict 类型。已有不同 helper 拒绝覆盖。

新 ready 顺序：创建三张**空** S3 业务表 → 调用原 ready（包含已安装的
replica、phone、入口/通道回调）→ 增加 S3 窄 dispatch → 返回原 ready 结果。
原 API principal 方法和所有普通 dispatch 原样沿用，不新增公开监听口。

专用私有配置只允许如下结构；token_file 由运行时既有私有文件读取规则
读取，builder 从不读它：

```json
{"reviewers":[{"reviewer_id":"OWNER_MAINTENANCE_ID","token_file":"/target/private/dedicated-maintenance.token"}]}
```

新维护凭据不得与任意旧 operator/worker/ingress 等角色复用；继承自旧 API
的跨角色同 token 也会拒绝。维护角色只能访问 S3 原四条窄 POST 入口；
全局 status/task/control/replica/phone 等其它管理入口均拒绝。原 ordinary
operator 标志、模型工具自传 reviewed/completed 不会因此取得维护权限。

## 复制覆盖：Root 显式维护，不能自动绕过

新增 global 表是 `native_partial_acceptances`、`native_partial_before_chunks`、
`native_partial_history_parts`；完整 live artifact 复用 `session_chunks`。
这些都是业务记录，不以内部 prefix 排除。adapter 安装表但**不 refresh**
Journal。Root 须把三表显式加入现有 source policy，在原维护窗口更新配置、
调用既有 `/v1/business-replica/refresh`，并让副本追上新 generation/manifest。
需要重新 baseline 时沿原完整复制流程办理，不覆盖运行中的 authority DB。

在 Journal 缺失、manifest 未覆盖三表以及 tasks/native_sessions/session_chunks、
或仍有 coverage_issues 时，S3 history/accept 拒绝。inspect/原 receipt 查回
仍可进行。其它原 API 不因本 adapter 被替换或获额外权限。

## Worker：独立版本，旧 namespace 缺口明确保留

`worker` 子命令另外要求实际受选 `--codex/--codex-sha256/--installed-codex`。
它保留原 launcher/namespace fallback 和配置，输出显式版本目录中的
**同一实际 incumbent Codex 源码加精确 Goal-read guard**；不换整包，不改
model/profile/governance/原会话身份，不声称 model/effort 已生效。

必须存在已知 `_read_goal_snapshot` seam，guard 位于真实 goal 读取后、处理
prior notifications/host 请求前；实际 Goal 必须匹配 owner 复核的原快照。
缺 seam 或已有不一致 guard 会拒绝构建且不产生 candidate 目录。

已读取的 laptop fallback `3198c877…/assistant_mesh/codex.py` 没有该方法。
**当前默认旧 worker 不在此 builder 的已支持部署范围。** receiver completion
可以先用；S2 remaining 应等 Root 选择合格且显式 pinned 的现代 Codex
namespace 并沿原 thread 接线。不能把旧 `thread/resume → turn/start` 冒充
已有完整 Goal controller，也不能只靠 prompt 声明 high reasoning。

## Cooperative retirement 接线

`native_partial_adapter.receipt_resolver(trusted_client)` 构造只向原 authority
receipt 入口取原 acceptance_id 的 resolver。可信部署代码用专用维护 Client
配置创建它，再赋给具备 S3 支持的 `supervisor.store.retirement_resolver`。
不能用模型提交的 receipt body 或普通 worker credential 替代。

本 builder 不替换本地 supervisor。其 retirement 默认为关闭；未完成单独
接线时沿原 stage/activate 接独立工作，原 work unknown、task/native guard
和全部原历史保留。具体验收仍按 [S3 contract](../docs/NATIVE-PARTIAL-CONTINUATION.md)
执行，任何真实旧任务均须 Root 逐原 operation 复核后才接受。
