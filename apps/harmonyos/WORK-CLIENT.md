# 手机持续出站任务客户端

本片为源码实现。没有在此分支操作生产、账号、签名、手机或后台回调；
商业 SDK 编译、合法签名安装和真实手机第二次业务请求由 Root 集成验收。

`platform/WorkClient.ets` 由前台页面启动。首次随机 `node_id` 仍来自原
`NativeServices` 私有快照；不会因连接失败、unknown、凭据失效或更新而
替换。已有原 enrollment/relay 挑战与日志全部保留，持续任务使用独立
`mesh_work_v1` 私有 Preferences，更新状态使用维护模块独立数据库。

完成原 `/enrollment/node` 登记后，导入可信服务提供的私有
`mesh-phone-bootstrap/1`：

```json
{
  "format": "mesh-phone-bootstrap/1",
  "config": {
    "format": "mesh-phone-client/1",
    "resource_url": "https://mesh.example.test/phone/node",
    "node_id": "synthetic-phone",
    "session_id": "original-session",
    "mesh_id": "synthetic-mesh",
    "authority_id": "synthetic-authority"
  },
  "credential": {
    "kind": "bearer",
    "token": "SYNTHETIC-ONLY-DO-NOT-USE-AS-A-REAL-TOKEN",
    "scope": ["phone:node", "phone:updates"]
  },
  "enrollment": {
    "node_id": "synthetic-phone",
    "mesh_id": "synthetic-mesh",
    "authority_id": "synthetic-authority",
    "operation_id": "original-enrollment-op",
    "request_sha256": "64-lowercase-hex-from-original-enrollment",
    "registration_receipt": {
      "format": "mesh-enrollment-registration/1",
      "operation_id": "original-enrollment-op",
      "authority_id": "synthetic-authority",
      "publication_sha256": "64-lowercase-hex-from-original-publication",
      "registry_ref": "synthetic:original-registry-row"
    }
  }
}
```

上方是结构说明，占位 hash 不可直接导入。配置需匹配已绑定的原
enrollment、原操作及已保存的原完整结果。原登记回执由 authority 产生，
手机不自行宣布独立验证成功。`config` 严格只含非秘密字段；token 独立
保存至 `NodeCredentialStore`/Asset，导入文本立即从页面清空。替换可信
入口可以重导入同一节点/会话/mesh/authority 的配置，原日志不清空。

每次前台启动/重联执行 `mesh_connect`，之后约每10秒
`mesh_node_update`，约每30秒 `mesh_discover`。`mesh_work_pull` 返回原
Registry/Allocation 的 pending dispatch；不会调用旧 task pull 新建第二份
业务任务。每轮最多处理4个新 dispatch、核对4个旧日志，HTTP有8秒超时、
1MiB响应上限、禁止重定向且不切换未知入口。网络 presence 到期独立于
原任务 epoch/deadline；心跳不会延长或释放原任务的执行租约。

只支持原 `challenge.bytes.repeat` 和本人系统选择文件的
`file.read.selected`，均完整返回最多64KiB。字节 recipe 显式包含
`workload:{input:{bytes,sha256,base64},repeat}`；选文件包含
`workload:{max_bytes}`。实际 capability ID 是 `<node_id>:bytes-v1` 或
`<node_id>:selected-file-v1`。不解释远程 Shell、任意路径或GUI动作。

原 operation/task/task_epoch/selection/provider/receipt 始终绑定：
本地 prepared flush → inspect 原 allocation → accept → start 取得
`execute_once:true` → 本地 attempted flush → 原生执行/完整结果 flush →
provider `result` 保存完整字节 → provider `settle` 引用
`phone-result:<sha256>`。原调用方从同一 allocation 的 settlement/result
读取结果。所有 provider 调用复用
`mesh_work_report {session_id,report_id,provider:{action,arguments}}`。

冷启动没有结果的未决记录转为 unknown，后续仅 inspect/unknown 核对原
receipt；失去 start ACK 也不再调用原生执行。已保存完整结果可幂等报告
同一内容；失去 settle ACK 后核对已有结算，绝不换 epoch 修改旧报告。
结果上传与结算属于同一已执行操作的对账，不是重做能力调用。

本地关闭共享、切后台或页面关闭立即停止本机新执行，随后尽力发出窄
`mesh_node_update` 撤回能力，网络失败时依然本地停止。文件临时 URI 不
持久化、不公告；独立 grant token 绑定原调用。撤回/选新文件/冷启动不
允许旧缓存文件借用新授权上传。用户原生手机功能不受这个开关限制。

页面同时接入维护服务：可信配置、系统择机同步、前台同步、数据回退与
真实更新状态。WorkScheduler 不启动此任务执行器，应用不承诺后台常驻。

针对性状态机验证入口为 `test/work.test.mjs`。该 harness 运行发货的同一
`core/WorkProtocol.ts`，使用合成身份覆盖完整字节到结算、start/result/
settle ACK 丢失、durable 写失败、授权撤回和原身份拒绝；不等于 ArkTS
编译、真实服务部署、真机网络或实际文件权限验收。
