# 原3198 Store的最小通道事务 backport

2026-10-11，PAM-009c 源码交付；**未部署**。此片仅补当前独立
legacy receiver 缺少的原账本事务组合能力，不替换正式 Worker，
不升级 native goal/claim/update/session 行为，不 grant、启动接收器
或修改生产 SQLite。上一片真实唯一 receiver stop/resume 事实见
[receiver 部署](LEGACY-RECEIVER-DEPLOYMENT.md)，不由本 fixture 外推。

## 变更范围

[最小 patch](../deploy/legacy-store-carrier.patch) 精确针对
`3198c877234a8fd0c6912fdbe218f47185effedc` 的 `assistant_mesh/store.py`。
只有一个 ingestion hunk：原 `ingest` 打开原 transaction 后调用新
`_ingest_in_db`；后者是原来 ingestion 的完整 body 去掉外层 transaction。
原过滤、原 inbox/message ID、上下文、activation cutoff、命令控制、
goal/plan 输入处理、task/outbox 写入、cursor/last_poll 都保持。
无新 Store schema、构造器 recovery、更换 clock 或新 native settlement。

- 原 Store SHA-256：
  `7d8f750e8dfe10045ca97b52ac0ff853d645ea66b4b01f9a9aaf60fb48df5440`
- patch SHA-256：
  `901e588c7d3bc59c808e565efff17ba5913bca50073d1aabd87cd6f71c78ea61`
- patch 后 Store SHA-256：
  `081633994459ffd2fb253e5514ea398d011b0eb8adcf059b251bbde10a705f08`

已有 `ChannelState` 才是 prepare/mark/record/commit facade；无需把这套
方法塞进 Store 或复制整个新版 Store。其 receipt 与 `_ingest_in_db`
共同使用原 `Store.transaction()` 的 `BEGIN IMMEDIATE` writer；receipt
写入失败会一起回滚 inbox/context/task/outbox/cursor，不出现“先提交
新任务，后丢失 canonical receipt”的两事务窗口。

## 原有 facade 依赖与加法 schema

本片不更改这些已有实现。下一 receiver candidate 需显式带上与当前
源码一致的模块，而不是假设旧 receiver 已全部具备：

| 模块 | 本片需要的接口/行为 | 依赖 |
| --- | --- | --- |
| `channel_state.py` | canonical prepare/mark/record/commit/receipt | `channel_carrier`, `networking.canonical/digest`，原 Store 的 `_meta/_set/transaction/clock/_ingest_in_db` |
| `channel_carrier.py` | 指定 witness、term/ticket/once-only intent | 原 `Store.Conflict`、`networking.canonical/digest`、标准库 |
| `channel_authority.py` | 原 authority 的 actor/binding 校验及完整结果 RPC/spool | 上述两模块、标准库；启用可选 supervisor 时再依赖 `channel_supervisor` |

原3198 networking 已有相同 `canonical/digest`；没有为了本片覆盖它。
本地私有 candidate 从完整原189个文件构建，只有 Store 原文件变更，
另带上述3个现有 facade 模块。保留原 source 的独立副本用于显式
base-root 复核；这不是可直接启用 fenced 配置的 server 发布包。
实际 `API/serve` 接线、peer/config 验证、fenced transport 和 owner
verifier 仍须 Root 对独立 receiver overlay 做最小集成/审查。

Facade 显式构造后加5表和1个 send-identity unique index：
`channel_carrier_witness/accounts/candidates/intents` 及
`channel_canonical_intents`。原 Store 单独打开没有这些表。原 native/
task/steering/session/chunk/action/interaction 行不因建这些表变化。
没有 verifier 时账户仍 frozen/unowned，不能因 schema 创建签 grant。

私有 candidate 中现有3模块 SHA-256：

- `channel_carrier.py`：`df8713965b429e07251bed9c1e77cf3465133ae3bfacf0b73dc457c577737426`
- `channel_state.py`：`e1b3642f1c9c8a736474e888e70f845e6b750c98413da2f49d7602eddf857568`
- `channel_authority.py`：`a9978f8dff13b4b4ae6409fab3a12d43b7e7a53087dda423e2f48abb09a0934c`

## 已运行的兼容验收

[测试](../tests/test_legacy_store_carrier_backport.py) 从精确 git ancestor
或 `PAM_LEGACY_BASE_ROOT/assistant_mesh/store.py` 读取原 Store，先核
原 SHA，再按 public patch 的所有行/offset/context 精确应用并核
patch 后 SHA。**不拿当前新版 Store 替代**。测试中的 source 只编译
成隔离 fixture 类，不写当前 `assistant_mesh/store.py`。

本机：原 git object **43项通过/0 skip，0.384s**；显式私有原 base
root **43项通过/0 skip，0.334s**。本片未在 VPS/Python3.6/CI
运行，不能将上一 receiver overlay 的117项或新版2002项替代它。

30项直接复用已有 canonical-state 全断言但把 Store 构造/重开换成
精确旧 Store + 此 patch，不改断言；覆盖 frozen/expired/旧term、
原 wire snapshot、原 client/body/media/context/route、完整结果 spool、
提交授权、receipt 原子回滚、原 ACK-loss只读回执、unknown 不重放、
独立新发送不被旧unknown全部阻断、原 native历史/steering 保留。
13项新增验证包括：

- 所有非 ingestion 方法/Store其余AST全等；提取body与原body AST全等。
- ordinary/goal/plan/status/control输入的完整账本与原行为一致。
- caller事务失败/半批无效/重复message均不提前提交。
- 8个并发原 public ingest只创建1个原message/task。
- 2个并发poll prepare只保留1条原lane；2个原commit回执查询只 ingest1次。
- legacy public ingest 与原 fenced commit 竞争时，变化cursor被拒绝，
  原完整outcome和未决guard保留，不用新ID修复或启动第二poller。
- 显式原 `recover_inflight=False` 重开保留提交中outbox/steering；
  facade仅加schema，原native全行/历史bytes保持。

复跑：

```sh
python3 -m unittest tests.test_legacy_store_carrier_backport -v
PAM_LEGACY_BASE_ROOT=/explicit/frozen/original3198/source \
  python3 -m unittest tests.test_legacy_store_carrier_backport -v
```

缺精确 git object且未提供base的第三方source archive会明确报告
class-level skip；错误显式base/hash不匹配则失败。正式Root兼容验收
必须核实 **43 pass/0 skip**，不能把skip当过关。CI应提供原 git history
或明确base源；没有修改生产source/账本以方便测试。

## 保留的真实部署边界

原构造器默认 `recover_inflight=True` 仍会将提交中outbox/steering
保守标为unknown；**本片没有改掉原行为**。receiver加载边界必须沿用
已有原账本静止核验和receiver专用 no-recovery 构造策略，不能重启
Worker/丢原steering，也不能回灌旧DB消去新状态。

上述 proof/verifier 和所有 transport outcome 都是 synthetic；没有
iLink/model请求、生产grant、第二poller、实际handoff或分区HA证据。
无法由patch、fixture、local stop 或单个PID签真实external quiescence。
下一步 Root 集成此最小patch/现有facade进独立receiver candidate，
双端兼容复核，再以原通道真实停止/原在途闭合/独立witnessproof推进
一次受控carrier交接。communication authority/state/arbitration 若仍
只在VPS，换carrier仍不等于VPS失联HA。
