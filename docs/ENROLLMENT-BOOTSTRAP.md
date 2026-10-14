# 新设备：薄 bootstrap 工具与节点 recipe

`python3 -m assistant_mesh.enrollment_bootstrap` 复用原
[EnrollmentJournal](ENVIRONMENT-ADAPTIVE-ENROLLMENT.md)，不是另建一套 Mesh。
它把 plan、只读能力盘点、原 ID challenge、一次 adapter、独立全字节
verify 和完整 receipt 接成可执行工具；不自动注册生产节点、配置网络、
创建 credential 或访问模型。不要求所有设备安装 Python/systemd/SSH。

公开接口合同仍是 `mesh-enrollment/1`、`mesh-adaptation-report/1`、
`mesh-enrollment-challenge/1`、`mesh-enrollment-result/1`；同源重汇合保留
mesh/node/owner、continuation、nonce、原操作和成果，不能清 unknown 或
重开新 ID。此参考工具只管理自己额外提供的 Mesh 调用，原生 Shell、
文件、网络、浏览器和 MCP 全部保留。加入通道不自动授予业务出口。

## Agent 能直接用的工具

可信安装方使用 [owner 示例](../deploy/enrollment-bootstrap-owner.json.example)
填已有 owner/enrollment/授权、真实 pin、独立 input/oracle 文件、原操作
ID 和当前设备 adapter。全零 SHA/commit、`/PRIVATE` 和 `valid_until=1`
均为不可部署占位符。配置及数据留在私有状态，Linux 参考父目录0700/
文件0600；其他系统用其自身安全存储/ACL实现同合同，不照抄绝对路径。

部署后的固定 wrapper 绑定该配置；agent 参数只选 action 和**原** ID，
不能从 challenge/model JSON 选择命令、oracle、owner、credential 或
注册函数。普通 CLI 是可信本机入口，不是鉴权 HTTP 服务；同 UID 原生
agent 本来有文件/Shell权限，不能把私有 JSON 文件夸成不可信代码沙箱。

```sh
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json plan
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json inventory
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json bootstrap
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json challenge --operation-id ORIGINAL
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json run --operation-id ORIGINAL
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json verify --operation-id ORIGINAL
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json status --operation-id ORIGINAL
python3 -m assistant_mesh.enrollment_bootstrap --config /PRIVATE/owner.json receipt --operation-id ORIGINAL
```

- `plan` 不创建 journal/进程/网络，不把原配置说成当前 live 授权。
  原身份、运行型、挑战范围、自适配步骤、unknown/重汇合规则可直接
  交给原项目 specialist。`bootstrap` 仅绑定私有状态，不执行 challenge。
- `inventory` 用 PATH presence 查 owner 选定的现有命令，不运行 wrapper
  `help/--version`、扫描凭据或上报路径。harness存在不证明登录/额度/
  模型可用。可显式启用既有 `inspect_network` 的只读接口/默认路由元数据；
  Linux外无内建 OS adapter 时明确缺项，不转成强制Linux前提。
- capability候选保留 class/instance、scope、source、共享resource_pool、
  fault_domain和代理绑定，仍是 **declared**。接口存在不证明公网可达，
  默认路由不证明带宽，两个同Wi-Fi/同API key实例不能重复算独立容量。
  不自动扫描蓝牙、连接耳机、读取照片或为相似实例合并所有权/授权。
- `challenge` 固定 owner 配置的原操作 ID，input/oracle完整文件与pin一致
  才生成原 intent/nonce。不能在 unknown 后换 ID 再试这个入网挑战。
- `run` 复用现有一次 callback 边界。固定 argv/直接文件SHA、无Shell
  字符串、bounded stdin/stdout/stderr和timeout；不复制HOME/CODEX_HOME/
  auth/proxy环境给节点输送器。返回原状态和当前直系进程观察，不把PID/
  exit0当远端模型/工具/物理执行或外部副作用闭合证明。依赖库非完整pin，
  可信adapter可有本机权限；并不拦截宿主的任意Shell或网络。
- `verify` 只用单独 owner oracle 全字节对比，不能从节点结果生成oracle。
  `provider_reported`、`challenge_bytes_verified`、登记回执和真正业务授权
  是不同证据层。`native_execution_attested=false`及
  `managed_use_authorized=false`保持。仅正确hash、lease或ping不够。
- `status/receipt` 用新增 `EnrollmentJournal.open_readonly`，不初始化/
  recovery/迁移原Store。receipt含完整原challenge/result-base64/
  verification/registration，不只给摘要；过期、owner disable、adapter/
  oracle不可用后仍可读取原成果。完整字节属私有文件，不贴公开日志。

`enabled=false` 是此共享入口的实时停用，不限制native功能。持有旧
实例的调用也重新读它，不能用缓存 enable 绕过撤销。原持久证据不会
因此删除。改变owner/仓库/join/authorization/verifier/执行recipe
不能静默重绑旧journal或解放unknown；由可信外层先对账并决定下一授权。

## 一份小包，不带本机登录环境

[独立 JS recipe](../deploy/enrollment-stdio-adapter.mjs) 仅使用 Node.js
builtins，实现 `challenge.bytes.repeat`：stdin原challenge→实际全字节
转换→stdout原result envelope。不需 Python、常驻服务、SSH、模型或
依赖下载，也不读 owner 私有配置/凭据。它只是首个 adapter 参考，不
要求手机/Windows/Cloud未来都用Node；可换为当前环境的native实现。

```sh
python3 deploy/build-enrollment-adapter.py /PRIVATE/node-adapter.zip
```

包仅含 JS、协议示例、说明、许可证和完整文件hash manifest；目标必须
为新文件，现有包不覆盖。示例与说明不是授权。节点收到一次已准入的
原 challenge 后，用其实际 Node 可执行文件调用：

```sh
node enrollment-stdio-adapter.mjs --repeat 3
```

把原 JSON 写stdin并关闭stdin，保存**全部**stdout JSON给原authority。
已入网外层应先持久化发送intent，再执行至多一次；掉ACK只查原记录或
上报同nonce/result，不能重新执行。编码复现既有 Python参考的排序键/
紧凑ASCII/Unicode转义/安全整数JSON，不冒称RFC8785。没有包加载器、
远程源码自执行、下载付费依赖或把任意prompt变Shell字符串的功能。

## 真登记与外围设备由原 authority 接线

`EnrollmentBootstrap(..., registrar=..., registration_observer=...,
connection_observer=...)` 接受可信安装层注入的真实原authority callbacks。
它们不来自 JSON/HTTP参数。CLI没有配置这些钩子时明确
`bootstrap_registrar_not_configured`，不会拿operator bearer顶替。
`register`复用原publication/原准确回执；unknown只用`reconcile`读取
原publication。state/receipt不会授予Registry grant或造新的入网身份。

代理来源按原`source_device/proxy_binding/revision/ttl_seconds`保留。
验证还需可信observer**实际**连接挑战，不接受节点自写ping回执或
“扫描到耳机”当控制权。手机与电脑看到同耳机不代表两份独立资源；
代理迁移/多点连接保留原设备身份和本地最终权限。JS字节recipe本身
不驱动耳机/NAS/SSH-only设备；native agent按真实接口补adapter，按
本合同验收，不先搭完整设备框架。

临时和交互节点复用同enrollment；官方Cloud仍需其外层真实
task/epoch/lease/job及受限callback，见
[官方Cloud合同](OFFICIAL-CLOUD-NODE.md)。本工具不提交Cloud job、创建
token或把Alibaba的`cloud`当官方Cloud。VM退出后保存成果留在原Mesh。

## 当前验收边界

风险测试覆盖真实独立JS进程、完整153600B输出/Unicode请求hash、只读
冷恢复、原ID/unknown、当前停用、pin漂移、timeout、丢失原journal不
重新初始化、代理observer缺项和独立registrar注入。新增17项及原
enrollment/network/Cloud membership/HTTP合计106项在本分支通过
（8.317s，0skip）；不是Root全量/公开CI或生产部署验收。
合成authority/registry明确标记synthetic；这不是第三物理设备、
Windows/Harmony真机、模型推理、真实公网或生产注册。

2026-10-14 07:15:27 UTC，源commit
`4c81110b1cb5d6d66575510a62bb3c55a382c1e0`的非测试标准CLI实际链完成。
原`mesh-isolated-bootstrap-20261014-v1`仅一条journal operation，独立
JS直系process自然exit0/reaped，stdout205344B/stderr0，完整153600B
artifact与单独owner oracle逐字节一致。原request/nonce/continuation/
完整result envelope和SQLite `mode=ro`全行独立核对；再次运行probe仅读
实际原journal/全字节并返回`new_execution_attempted=false`。

| 对象 | 实际SHA-256 |
| --- | --- |
| 原request | `20a972cad0c98fbcdcd9e314878ed2a7e317c0dcc304ecb4a9e3fbb5414cefee` |
| 完整artifact | `a3217513b8a9255881606e02ed68242ebc17587f53879e05f4fbf8b93c25a4b7` |
| 完整receipt | `b365030acb9e774538e9041476bfaa57a4e90aba3fdbd613279ba0ceec8027d9` |
| 该冻结source小包（8456B） | `ed862d587defb7ff9373b6643097fb54f560ac1b03c2903cdf6d8721b676599b` |

包包含说明文件，因此后续文档/源版本重建得到不同包hash是正常的，
不能拿上表替代新包实际manifest/全文件核验。schema/authority为
synthetic，状态`verified/not_registered`；独立挑战字节通过仍不是
`native_execution_attested`、`managed_use_authorized`或全局入网。
没有生产注册/凭据/服务/网络/模型调用或修改原43个协调任务。

可用`deploy/probe-enrollment-adapter.py --state-dir /PRIVATE/isolated-new-root
--operation-id ORIGINAL --repo-pin FULL_COMMIT`复现实验。仅在明确
合成隔离空间使用；已有空间沿原ID/版本/阶段文件续接，原unknown不
重跑。完整私有stage/receipt/journal保留给可信外层核验，不公开内容。

Root下一接线：选择owner已授权设备/原challenge/原continuation，审查
由该设备agent按环境编写的adapter，配置独立oracle及真实原authority
callbacks，单次challenge/完整成果和登记回执；再验证断加入路径后的
native新工作/排障、授权替代重联与原身份/撤销/unknown保持。无常驻能力
就用temporary/interactive，缺某平台adapter不阻止其它已验证节点。
