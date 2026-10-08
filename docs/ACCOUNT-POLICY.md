# 通信身份与推理账号池

本人明确指定：Chat / Live 固定主账号；Codex 推理可使用两个已授权账号。
这两种权限分开处理，不能为了推理可用性而切换对话身份。

- 通信适配器必须传入确定的 `expected_auth_home`，与原生进程实际 home
  一致才允许绑定。缺失或身份不符即拒绝，不回退到其他账号。
- 部署的主账号使用私有 `163.com` profile；账号值、认证内容和私有配置
  均不进公开仓库。启动器的改动只影响未来启动，不证明已有 GUI 登录态。
- Worker 的 `codex_accounts` 是单独的明确授权列表。只在尚未启动模型
  turn 的初始化失败时尝试下一个 profile；不把已经开始的外部效果换号重跑。
- profile 变更仍保持原 native thread。只运输所选 thread 的原生 rollout，
  不编写摘要替代上下文，也不 silently 新建 Leader。无法恢复时报告错误。
- 推理池可用不证明 Live 已开放；成功登录和 Plus 元数据也不是音频验收。
- 不创建 API key，不启用付费 Realtime 或购买资源作为隐式兜底。

实际部署需再次核对每个 profile 的原生登录状态，过期认证不能冒充后备。
当前 Live 的真实连接验收仍待原 Leader rollout 恢复问题排除后进行。
