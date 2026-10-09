# 原生 Codex Cloud job adapter

PAM-006b 的本地实施片；实现是
[cloud_jobs.py](../assistant_mesh/cloud_jobs.py)，离线测试是
[test_cloud_jobs.py](../tests/test_cloud_jobs.py)。不接替 native Leader、
不修改 Store/native history、不自动 enroll node、不安装服务，不使用
Platform API key 或第三方代理。当前没有提交真实 Cloud job。

## 官方接口与真实边界

本机核对版本为 `codex-cli 0.162.0`。官方 exec/list 文档：
[Cloud CLI](https://learn.chatgpt.com/docs/developer-commands#codex-cloud)。
接口细节还核对了相同 tag 的
[CLI args](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/cli.rs)
和 [implementation](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/lib.rs)。

已实际阅读 `resolve_query_input`：非 `-` 的 QUERY 原样作为 prompt；
`-` 设置 `force_stdin`，调用 stdin `read_to_string`，空输入拒绝。
所以 adapter 的最后一个 `-` **不是字面任务内容**。prompt 走 PIPE，
不放在 argv、shell command 或公开日志。没有通过提交 job 来试探。
[Pinned stdin source](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/lib.rs#L249)。

`cloud status` 只有 Ready 退出 0；Pending/Applied/Error 都退出 1。
adapter 用 `list --json` 精确观察 provider ID，status 仅辅助分类。
Pending exit 1、页面未列出任务、网络失败都不能触发重新提交。
官方 CLI 未暴露 caller 幂等键；本模块保证每个 intent 最多启动一次
CLI exec，不声称 provider HTTP 层 exactly-once。

不能只依赖 `command -v codex`。本机只读诊断发现一个 shell launcher
通过自己的 override 变量重新设置 CODEX_HOME，覆盖调用方仅设置的
CODEX_HOME；此前 list 因而读到另一种默认认证。修复配置应选择已经
安装、已核实的原生 CLI 绝对路径及原授权 home，不改 auth/wrapper、
不新登录或换 Leader。adapter 要求显式绝对 executable；调用方仍须
核对所选 launcher 是否尊重 home。路径是选择记录，不是账号证明。

## Trusted library 接口

以下全是虚构路径/ID，且**不是执行脚本**。Root 集成到受鉴权控制面
之前，必须核对实际 owner、原 Mesh task/epoch、当前 task authority、
Cloud 额度、已有 published environment 和允许的 branch。

```python
from assistant_mesh.cloud_jobs import CloudJobs

jobs = CloudJobs(
    '/PRIVATE/cloud-job-journal', 'verified-owner-profile-binding',
    {'ACTUAL_ENV_ID': ['main']},
    executable='/VERIFIED/absolute/native-codex',
    codex_home='/PRIVATE/existing-owner-native-home',
)
receipt = jobs.intent({
    'mesh_task_id': 'original-mesh-child-task', 'mesh_epoch': 1,
    'request_id': 'original-stable-request',
    'environment_id': 'ACTUAL_ENV_ID', 'branch': 'main',
    'prompt': 'The explicitly authorized project work; no local credentials.',
})
# Root checks actual authorization here; never map model-supplied True to this.
# jobs.submit(receipt['job_id'], authorized=True)
```

| Method | 行为 |
| --- | --- |
| `intent(payload)` | 持久不可变 task/epoch/request/env/branch/prompt；不联网 |
| `get(job_id)` | 原本地 receipt、状态和有界元数据；不返回 prompt/auth |
| `submit(job_id, authorized=True)` | 唯一 effectful 提交入口；当前配置复核，持久记录开始后单次 CLI exec，固定 attempts=1 |
| `observe(job_id, max_pages=5)` | 同 provider ID/env 的 list JSON 分页观察；最多 20 pages，不凭标题找因果绑定 |
| `status_diagnostic(job_id)` | `[PENDING/READY/APPLIED/ERROR]` 与实际退出码联合分类；不返回原 title/stderr，不更新权威状态或重投 |
| `reconcile(job_id, url, owner_confirmed=True)` | 丢回执后本人独立核对原 job；再检查同 native profile 列表中的精确 ID/env，记录手工来源；不 reset attempt |
| `fetch_diff(job_id)` | 已观察 Ready/Applied 时取 attempt 1 的 exact stdout，私有内容寻址保存并持久记引用；不 apply |
| `saved_diffs(job_id)` | 不联网恢复已存成果引用，并核对本地原字节 SHA/大小 |

`authorized=True`/`owner_confirmed=True` 是 trusted integration 的门槛，
不是 OAuth/lease 检查的替代。不能直接暴露成模型可写 HTTP 参数。
本模块不查询 Store、不检查 parent lease，Root 必须在受鉴权 handler
核对并持久授权后调用。观测结果不直接结算 Store task/释放资源。

## 持久身份与 unknown

一个独立私有 journal 绑定 owner-profile namespace、CLI path/home
selection；更换 owner/home/executable 时拒绝认领旧 journal。正常
同身份 OAuth 更新不需要复制 auth 到 journal；本模块根本不读 token。
native home 内账号被换掉或同 UID 任意文件写入不在此模块隔离范围内，
需由 Root 的实际身份检查保护。升级 CLI 的新选择不能默默搬迁旧 job。

原 mesh task 和 request 都 UNIQUE。相同内容 retry 返回同 job；相同
task 换 request、换 epoch、prompt、环境/branch 变更都冲突。并行
Cloud 工作应先分配独立 Mesh child task，而不是同一 task 下绕过 unknown。

状态序列为 intent → submitting → submitted，或 submission_unknown。
开始标志在 FULL synchronous SQLite transaction 中提交，然后才启动
CLI。收到一个严格的官方 task URL 才绑定唯一 provider ID。超时、
丢回复、非零、无效回执、甚至开始记录后尚未启动进程就崩溃，都保守
视为 unknown；没有自动 reset、cancel、resume、换 ID 或 resubmit API。
重新打开 journal 不清除开始标志，两个实例竞态也只能启动一次 exec。

未知 provider ID 时 `observe` 返回 identity_unknown，不按模型自报
nonce/title 自动认领候选 job；本人须在授权账号中独立对账，再调用
reconcile。list membership 是补充环境核对，不是 job 因果关系证明。
provider ID 已绑定后不能替换/绑定另一 task。未找到列表项或限页数
结束保留原 ID/最后已知状态；不能以列表短期缺失证明“没有提交”。

## 私有成果与退出后回收

journal/root/runs/artifacts 为 owner 0700，DB/成果为常规单链接 0600；
拒绝不安全 mode、symlink 目录、硬链接 DB，不对现有文件自动 chmod。
当前私有 ACL 安装例在 POSIX 验证；Windows 必须提供对应 owner ACL
适配，不伪称 Linux mode 位足以保护 Windows。协议无需随 OS 重写。

实际 CLI cwd 在私有 per-job 目录。原生 CLI 可能写包含账号标识的
`error.log`；绝不在公开仓库 cwd 运行，不发布 raw log。stdout/stderr
在私有 temporary files 中捕获；返回内容有大小限制，诊断仅固定类别
或 digest。**该读取上限不是整个 CLI 临时文件/原生日志的磁盘配额**；
正常运行时文件仍可能增长，部署资源调度须另设已授权的磁盘预算。
超时只终止本次 owned CLI 进程，不证明远端 job 被取消。

diff 原字节先 fsync，再 atomic create（不覆盖），目录 fsync 后把
引用写入 journal。VM/浏览器退出后可用 saved_diffs 本地恢复，不依赖
7 天 Cloud VM 保留。结果可重复读取，但从不调用 cloud apply，不执行
patch、附件或模型返回的指令；diff 并不覆盖所有非代码成果类型。

`artifact_content_verified=False` 表示只有本地字节完整性核对，还没有
独立验证成果质量。`execution_verified=False` 和
`cloud_node_enrolled=False` 始终保留，不能由 Ready、task URL 或保存
diff 改成 True。后续真实握手、任务限定 callback、ephemeral lease、
成果发布均由单独 Mesh 合同承接，不发永久 worker/operator token。

## 已验证 / 下一步

32 项离线测试通过：正常 intent/单次 exec/pending→ready/diff/reopen、
并发、超时/崩溃/丢回复、稳定 ID 与内容冲突、namespace/runtime 选择、
只读观察/分页异常、status exit 1、人工对账、私有文件/原字节回收和
不 autoapply。默认 runner 仅用本地 Python subprocess 测 stdin/超时/
输出上限，没有模型调用或真实 Cloud job。它们不是实际账号验收。

2026-10-09 实际只读 gate：在仓库外 0700 cwd、umask 077，用既有
原生 CLI 和正确授权 home 执行 `cloud list --limit 1 --json`，exit 0、
stderr 为空、tasks 为空，auth 文件前后 SHA 不变。没有提交 job；空列表
不能确认/否定本项目已发布 environment、branch、network 或模型额度。

随后在 2026-10-10 北京时间的续接中，32 项测试独立复跑全部通过。
只读环境查询沿用同一原生主账号 home，局部核对现有 ID token 的邮箱
suffix 与 owner 指定主账号相符（未公开邮箱、account ID 或 token）。
按相同 tag 官方
[environment discovery](https://github.com/openai/codex/blob/rust-v0.162.0/codex-rs/cloud-tasks/src/env_detect.rs#L310)
对 **仅本项目** 的 repo-scoped environment URL 发一个 GET，HTTP 200
返回空数组。没有全账号环境/聊天遍历，没有 refresh 或提交试探。
该次查询的 auth 文件原字节前后 SHA 不变；生成的证据在仓库外 0700
目录、0600 文件，仅保留查询类别/数量/时间，不保存 token 或原账号值。

独立 `git ls-remote --heads origin main` 已观察到公开 `main` 指向
`cf308b32057327d4e87106456ff639f0bcbb6151`；这只证明 GitHub branch，
不证明该 branch 已被 Cloud environment 发布/允许。目前 **该版本 CLI
的 repo-specific lookup 未返回本项目环境**，不能据此声称全账号无
Cloud entitlement、新旧环境接口通用、网络/额度不足，或已有 Cloud
node。未创建环境、GitHub connection、secret、新 key 或 job。

Root 下一步：以本人同一账号核对/连接并发布本项目 environment（须在
该环境操作的实际授权成立后），确认 branch/HTTPS 网络及 owner 权限；集成
原 Mesh child task/epoch 的授权检查与私有 journal；获得明确 job
授权后单次提交；实际 provider ID 观察、取回成果、独立验收，再做
临时节点握手。没有 environment、账号权限或额度时停在对应 gate，
不要通过提交试探、改 auth、重放或新收费资源绕过。
