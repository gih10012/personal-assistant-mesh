# 开发源码与正式运行的边界

Mesh 不限制 agent 的原生工具。这里隔离的是部署版本，不是 Shell、
网络、文件或 MCP 权限；节点仍可在 owner 授权内发现能力、自行改装。

正式运行应将四种东西分开：开发源码、已验证版本、私有配置与持久
任务/原生历史/效果账本。启动入口固定到可追溯的验证版本；切版本不
复制旧 DB 覆盖新状态，不重新创建 Leader，不清 unknown/未决效果。
冻结来源、字节哈希、测试证据、切换前状态和实际进程路径分别核验。
版本可恢复，已经可能起效的工作不可因回退自动重放。

## 当前实际范围（2026-10-10）

两端正式核心仍为 `3198c877234a8fd0c6912fdbe218f47185effedc`。
公开 `ef647c8` goal 控制器通过两端 1328 项与 CI，但没有正式加载。

laptop 的 worker/node 原先以开发仓库为 WorkingDirectory。已在空闲
边界保留单位原配置、私有配置、一致账本及切换 intent，固定两个核心
入口到私有冻结 `3198c87` 版本目录；实际进程 WorkingDirectory 和源码
字节均核对。原业务/原生/效果行、配置字节、两条尚未领取的 guarded
维护 task 保留；未恢复 DB/history、未激活 native goal、未重启其他
服务。候选代码没有借本次切换进入正式核心。

VPS 核心本来就在私有发布目录；独立 managed provider 进程在本次
操作前后保持相同 PID、active、NRestarts=0，没有新增受管执行。

随后也保留 Firefox host、Codex MCP settings 与既有 profile 原配置，
只改三处源码路径：host import root、MCP launcher、私有 runtime_root。
新启动的两个入口已使用同一冻结正式版本；真实读取原 request 的
status/result，task、publication、完整批准投影与切换前一致。client、
grant/token、Firefox manifest 字节保留；没有提交新 task、调用模型、
重启既有进程或改变权限。验证是独立协议客户端，不是 Chat 模型 tools
或实际浏览器 UI 新一轮调用。

**未完成：** laptop native-recovery 保持旧进程且仍指向开发目录；已
存活的 MCP 进程没有重启，公开单位模板仍使用仓库目录。其他入口
需分别完成切换及实际进程核验。不能把已核对的启动入口固定说成所有
进程、动态导入或整机都已经版本隔离。续接 task 为 PAM-007f；逐项
选择静止边界、保留原状态和恢复配置，再核对实际运行来源。

## 环境自适配

Linux 的版本目录和 systemd drop-in 只是本机部署例子。Windows、手机
及临时 Cloud 节点可采用自己的包/进程/存储机制；必须保留的是源码
身份、私有状态隔离、同任务/线程身份、未知效果与切换证据，而不是
特定路径、命令或常驻方式。发布目录也不能替代模型自主环境排障。
