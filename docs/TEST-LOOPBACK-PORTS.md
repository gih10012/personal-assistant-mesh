# 真实 loopback Node 测试端口与清理（2026-10-15）

窄叶 `work/a2a-loopback-fixture-20261015`，base `16205ef`；只改测试夹具
和本记录，不修改生产Node/Server、等待时间、授权/断网/连续会话断言。

原CI `38713049911`（head `51a47ac`，Python3.12）在
`test_real_a2a_disconnect_local_work_and_reconnect_same_scope` 的
`pair → alice.start()` 返回 `local_node_server_start_failed`。原日志没有
底层errno，**不能声称已证明该CI一定是端口碰撞**，失败记录继续保留。

## 可确定重现的原夹具缺陷

原 `free_port()` 使用临时socket `bind(127.0.0.1, 0)` 取端口，然后关闭。
pair先为Alice、Bob两次取号；随后先真正启动Bob，再启动Alice。
关闭的端口不再被保留，OS合法返回同一个号就会让Alice撞到Bob。

在修改前，用真实临时loopback socket取得一个已释放的端口，仅把两次
`free_port()` 的返回值固定为该号（没有替换HTTPServer/socket绑定）调用
原pair，确定性得到同一个 `local_node_server_start_failed`，实际
`alice.server_error` 是 `OSError/errno98(EADDRINUSE)`，Bob真实监听该号，
Alice自有server线程已停。这证明真实失败路径存在，不是重跑直到绿。

## 最小修复及原边界

- fixture默认 `port=0`，由**真实服务器** bind后持续持有端口，不先取号
  再关闭；先启动Bob，从真实bound callback保存实际worker/peer地址，
  再启动Alice，最后用Alice实际地址构造Bob companion。
- 初次Bob绑定后的真实端口写回其私有fixture配置；原断网测试重启Bob
  仍使用该原地址，不把“发现新端口/换目标”冒充原peer重连。
- `start_external` 捕获线程的实际启动异常，pair的`start_owned`也读取
  Node保留的实际server_error，报告异常类型及errno，不
  打印完整异常体/URL/凭据；ready仍等原3秒。生产Node仍等原5秒，不
  自动换端口/重试/修改生产可用性判断。
- 真实bound callback在ready前登记服务器/线程句柄；teardown额外断言
  Node和外部fixture自有HTTP线程确已停止，继续真实shutdown/join/close。
- 新回归核两次实际serve都用0、socket实际地址不同且peer/client地址
  全一致、双向真实认证probe；另用实际占用的loopback端口分别核Node
  原异常/锁释放和外部fixture类型/errno诊断。不是mock网络成功或skip。

只涉及合成private fixture ledger/凭据，无模型调用、外部主机、生产运维
或新费用。本机修复验证不能顶替后续精确head的CI结果。

## 受限验证

5项针对性真实loopback回归通过（3.217s，0跳过），含3项新回归和原
断网/同scope重连、原ephemeral/private worker配置。最终相关Node/networking/
remote组121项全部通过（17.519s，0跳过），保留独立原日志。原子组有
包含关系，不相加拼成全网自治/HA证明；未重跑完整仓库或CI到变绿。
