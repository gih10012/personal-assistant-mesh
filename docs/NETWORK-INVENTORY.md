# 网络能力发现：候选不是已可调用出口

`python3 -m assistant_mesh network-inventory --node-id local-node` 是可选的
本地只读入口，不需要 Mesh token，也不连接 authority。`node-id` 只是
声明的采样标签，不能为远端请求覆盖凭据绑定的身份。`--freshness-seconds`
默认 30，范围大于 0、不超过 300；这是观测 TTL，不是分配/授权租约。

Linux 内置适配器使用有时间/字节上限的 `ip -j link`、IPv4/IPv6 主表
默认路由和 `nmcli` device status。没有这些工具时返回明确缺项，不
安装工具、不使用 sudo、不更改 radio、DNS、路由、代理或服务。

输出包括接口类型/状态、路由 family/interface/gateway 分类、代理环境
变量**名称**、采样开始与完成时间、截止时间和新鲜度。不返回 SSID、
MAC、IP、网关地址、账户或代理值；不保存原始命令输出。两个主表路由
为空与工具失败分开，不从主表推断 policy routing/DNS 全貌。

每个有默认路由的接口生成稳定 `network.egress` 候选及本地 declared
观测。候选不自动 publish、grant、pool-bind、admit 或 dispatch。
本轮不主动访问互联网、不测试带宽/延迟、不宣称现有流量免费、真实
宿主身份或代理正在使用。`new_spend_minor=0` 只表示未申请新付费资源，
不是账单验收。过期观测不能变成当前可用性的证明。

## 如何由 agent 扩展

Python 入口为 `inspect_network(node_id, adapter=None, freshness_seconds=30)`。
Windows、手机或其他系统按实际环境实现显式 trusted adapter 的
`name` / `collect(runner)`，返回小型脱敏合同；库规范化字段并拒绝额外
敏感字段。没有适配器时明确 unsupported，不把 Linux 命令强塞给所有
节点，也不自动远端下载代码。

节点模型可继续用自己的原生 Shell/网络工具诊断、联网、恢复连接，
不受本收集器命令集合限制。这里的固定四条命令只限定这个被动采样器
的实现，**不是**原生 agent 的 allowlist。

下一步由模型和 owner 在真实身份/权限下选择候选并明确登记能力，
按实际观察配置共享瓶颈、预算、授权与执行器，再测具体请求。目录
存在、route present、一次 SSH 可达，都不能替代受管出口实际执行、
独立性能或去中心化调度验收。
