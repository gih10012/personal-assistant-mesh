# PAM-009b：私有通道一致 checkpoint

2026-10-10 源码有限切片，**未部署、未做跨承载接管**。
`assistant_mesh.channel_checkpoint` 提供只读导出、严格验证和新私有文件
发布；没有 import、lease、poll、send、任务领取或原生 history 改写。
`migration_ready=false` 是格式强制条件，不是调用者可打开的选项。

## 保存什么

| 范围 | 保存与验证 |
| --- | --- |
| 通道绑定 | 原 account binding、receiver incarnation、由两者计算的原 route_id；不读认证文件、不复制 bot token |
| 选定 meta | cursor、owner_context 原 token/version/created、last_poll、activation cutoff、channel error 与原通道绑定；不导出任意其他 meta |
| inbox | 完整原私有消息行与 Store.ingest 消息哈希身份；普通 owner 消息必须指向原 canonical task，历史 cutoff/control/media 明确无 task |
| tasks | **全部原行**：parent、scope、context、status、epoch、node、原 checkpoint/effect guard/result 等；parent 引用必须完整且无环；绑定原 ledger authority 和行 SHA |
| outbox | 原 body/fingerprint、status、client_id、context version、media JSON reference、retry count、delivery route/detail；unknown/submitting/waiting_auth 原样保存 |
| relay | 原 source/peer/authority、request_id、post_attempted、attempt、lease、receipt/error；核对原 outbox 和固定通知身份，不改投 |
| notify receipts | 若原表存在，保存原 source/request→outbox 映射，核对原文字 fingerprint 与通知身份；不存在时不凭空制造 |
| 其他 authority 表 | 同一读视图逐行生成 schema/content SHA 和行数的 source anchor；BLOB 只计长度/哈希，不把原生 artifact 字节复制到 checkpoint |

这是**私有数据**：包含聊天、正文、context token、媒体引用、task 输入/
结果等。只写入 owner 的 0700 目录中新 0600 文件，不能进公仓、公告、
普通 capabilities 目录或无授权第三方模型。媒体字段是原 JSON 引用，
不会读取引用路径、下载附件或递归复制文件系统。

## 一致读取与完整性

源 DB 用 SQLite URI `mode=ro` 和 `query_only` 打开，不构造 `Store`，
因此不会初始化 schema、recover inflight、把 submitting 改成 unknown，
或修改日志模式。单一 `BEGIN` 内首个 schema 读建立 snapshot，全部行
及 dependency anchor 均从该视图读取；其他 WAL writer 可继续前进。
**一致读不证明旧 poller、sender、worker 已停机**，也不冻结外部副作用。

v1 精确核对当前实际 `PRAGMA table_info` 的字段顺序、SQLite type、
NOT NULL、default、primary key。新增/不认识的通道字段必须明确更新
版本/合同，不能悄悄遗漏新的 intent 列。其他关系表仅留下 anchor，
不是可导入 SQL，也不是完整执行关系已闭合的证明。

`authority` 是调用者从受保护部署配置取得的原 ledger 身份；当前 DB
没有独立持久 authority 身份列，本模块不会把传入标签当认证证明。
核验者须分别持有原 authority、account binding 和**外部保留的 SHA**。
自身 artifact 内的 SHA 不能给自身背书；攻击者改 unknown→pending 后
重算 artifact SHA，仍应被外部原 SHA 拒绝。SHA 保证被绑定内容一致，
不是签名、仲裁、反回滚证明、迁移授权或手机送达证据。

默认总上限：100,000 行、64 MiB。计量包含导出行和 dependency anchor
扫描的原 BLOB 字节；超过即失败，不截断/写部分 checkpoint。单个
SQLite 值和数据库读取本身仍使用 SQLite/Python 的内存，不能将这些
上限当作任意超大 DB 的严格峰值内存保证。最薄实现面向当前 Linux/
POSIX 私有文件部署，其他平台须适配权限与持久发布，**不阻断原生功能**。

## 离线调用

```python
from assistant_mesh.channel_checkpoint import (
    export_checkpoint, validate_checkpoint, write_checkpoint,
)

# 原配置身份/绑定与独立记录来自私有部署层，不从聊天文字授权。
checkpoint = export_checkpoint(
    private_database, original_authority, original_account_binding,
    checkpoint_id="owner-controlled-checkpoint-1",
)
expected_sha = checkpoint["sha256"]  # 独立保留，经认证交接给核验者。
validate_checkpoint(checkpoint, original_authority, original_account_binding, expected_sha)
write_checkpoint(checkpoint, new_private_path,
                 original_authority, original_account_binding, expected_sha)
```

发布使用新临时文件、fsync 和不覆盖的原子 link；已有目标（包括原快照）
拒绝覆盖。不自动创建目标 authority、不启动 receiver。若持久发布末尾
失败，目标可能已经存在；先按原 SHA 核对，不用覆盖/删除原目标处理。
异常只含固定诊断码，不把 DB 路径、账号、私有行或 SQLite 原错误输出。

## 尚缺什么

完整 tasks 行和 parent 树避免“只比 ID 就声称已迁移”，但**不等于
完整可运行任务迁移**。原生 session/artifact、interaction/steering、
continuation、agent action、remote delegation、allocation/provider
副作用等仍依赖原 authority 或原 provider 的持久状态；dependency
anchor 只供后续对账，不能代替实际关系行、原始 native 历史或在途证明。
未选择的 meta 也不在完整性覆盖中。

格式明确 `task_execution_dependencies_complete=false`、
`native_history_complete=false`、`source_quiescence_verified=false`、
`carrier_lease_verified=false`、`import_or_send_authorized=false`。
009c/d 需要旧 holder 失权/在途收敛、完整任务执行/原历史引用闭合、
受控接管和独立真实消息验收；不能导入新 SQLite、改 authority、重置
unknown 或开启第二 poller 就宣称完成。当前生产 VPS 单收发保持。
见 [沟通承载 HA](COMMUNICATION-CARRIER-HA.md)。
