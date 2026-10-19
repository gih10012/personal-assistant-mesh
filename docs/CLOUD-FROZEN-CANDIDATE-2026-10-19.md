# Cloud runtime 的冻结源增量构建

叶分支 `work/cloud-frozen-candidate-20261019`，基线 `c46ef1e`。只补
`deploy/build-cloud-owner-candidate.py`，不安装/启动组件，不读生产状态，
不改变原 Goal/Plan/Tasks。已通过的22b现代SDK三项不重跑。

输入必须是 Root 从当前 sole receiver 取得的**只读源码副本**。不能把
现代整个 repo 当作宿主运行源，也不能重新从3198构建后覆盖现有入口。
辅助器复制副本中全部 `assistant_mesh/*.py` 和顶层 Python 启动 hook，
只叠加下列15个固定哈希纯库模块；不复制配置、SQLite、认证或 native
history。旧构建JSON不充当新清单；新完整清单为 `cloud-owner-candidate.json`。

| 闭包组 | 模块 |
| --- | --- |
| Cloud | cloud_owner_runtime、cloud_adoption、cloud_http、cloud_jobs、cloud_membership、cloud_ui |
| 会话/入口合同 | conversation、conversation_authority、conversation_runtime、ingress、ingress_results、enrollment |
| MCP纯库 | mcp_http、mcp_http_auth、native_messaging |

相对 import 递归只在这15个模块内进行，包括函数内的延迟 import。
`config/store/worker/entrance_runtime` 是明确边界，使用副本原模块；不沿
现代 Worker/Codex 的依赖继续复制。`mcp_http.create_server`、OAuth、SDK
和CLI入口都不在这里调用，因此无需安装现代依赖。

构建前核 Store/server/Worker/Codex/sessions/channel 六个已审核心哈希，
以及入口/Claw/owner-control模块存在。Root 还须确认整份当前源码副本的
manifest SHA；任意源字节或顶层Python hook变化都不静默接受。15个支持
模块另固定在 `c46ef1e/b348010` 的内容哈希，闭包出现新边也拒绝。
所有非闭包原文件逐字节保留；清单分别记录新增、实际变化和未变文件。
目标必须是副本外尚不存在的新路径，目录0700、源码/清单0600。

## Root 最短准备

下面路径均是 Root 私有路径占位符。先获取当前 receiver 源码副本，
不把数据库/配置/凭据混进输入；这些命令本身不切换运行服务。

```bash
python3 /PRIVATE/PINNED_SUPPORT/deploy/build-cloud-owner-candidate.py \
  --base-root /PRIVATE/CURRENT_RECEIVER_SOURCE_COPY --inspect
```

Root 核对副本来源与上述输出后，固定实际 `base_manifest_sha256`，再构建：

```bash
python3 /PRIVATE/PINNED_SUPPORT/deploy/build-cloud-owner-candidate.py \
  --base-root /PRIVATE/CURRENT_RECEIVER_SOURCE_COPY \
  --expected-base-manifest-sha256 CONFIRMED_BASE_SHA256 \
  --support-root /PRIVATE/PINNED_SUPPORT \
  --destination /PRIVATE/NEW_CLOUD_CANDIDATE
```

## 宿主3.6.8的两步小检查

Root 将**新候选源码**放到私有检查目录后运行；`/usr/bin/python3` 必须
实际是3.6.8，不使用现代SDK镜像解释器替代。第一步只导入并检查哈希：

```bash
cd /PRIVATE/NEW_CLOUD_CANDIDATE
PYTHONPATH=/PRIVATE/NEW_CLOUD_CANDIDATE PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -S -B - <<'PY'
import hashlib, json, sqlite3, sys
from pathlib import Path
assert sys.version_info[:3] == (3, 6, 8), sys.version
root = Path.cwd()
manifest = json.loads((root / 'cloud-owner-candidate.json').read_text())
for name, expected in manifest['source_sha256'].items():
    assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
from assistant_mesh import server
from assistant_mesh.legacy_fenced_receiver import _check_frozen_source
assert Path(server.__file__).resolve().parent == root / 'assistant_mesh'
_check_frozen_source(server)
original = sqlite3.connect
def no_database(*args, **kwargs):
    raise AssertionError('import opened SQLite state')
sqlite3.connect = no_database
try:
    from assistant_mesh.cloud_owner_runtime import CloudOwnerRuntime, install_owner
    from assistant_mesh.entrance_runtime import serve_entrance, EntranceWorker
    from assistant_mesh.claw_cli import claw_store_class
    from assistant_mesh.claw_upload import uploader_from_config
finally:
    sqlite3.connect = original
assert not any(n == 'mcp' or n.startswith(('mcp.', 'fastmcp')) for n in sys.modules)
print('frozen hashes and host3.6.8 source imports passed; no database/SDK/receiver started')
PY
```

第二步仅跑已有8项 owner-runtime 检查：其 Store/HTTP/任务全部位于临时
隔离 fixture，导入的 Store/Worker 等来自上面的冻结候选，而非现代 repo。
不传生产配置、数据库或 account path，不启动iLink/模型/Cloud任务。

```bash
cd /PRIVATE/NEW_CLOUD_CANDIDATE
PYTHONPATH=/PRIVATE/NEW_CLOUD_CANDIDATE PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -S -B /PRIVATE/PINNED_SUPPORT/tests/test_cloud_owner_runtime.py
```

本机已跑3项 builder检查（1.518s、0skip）；其中子进程在历史冻结40模块
加已审 entrance 的候选上，实际完成上述8项runtime检查、0skip，并验证
导入无SQLite连接/无SDK载入、入口/Claw保留、原核心字节不变。解释器为
本机Python3.14.7；这些最初检查不是宿主3.6.8或实际50模块副本的证据。

随后 Root 提供了实际50模块只读源码副本。`runtime-manifest.json` 文件
SHA256为 `830bdbf65d7a679aea66289386c243c92577226cd12b8a696c5acf3d7102f781`，
其50个模块哈希与副本逐一匹配。原 `4a8dd1c` builder无修复直接构建成功：
base源码manifest `aff7d6e64d3092208a7683cd4db1ffa6259b555890732245701780e6360132df`；
候选源码manifest `1505d2cf105899d5e50afa4015967ee3268e7c3b952df20ecb2e864c77f2ae8a`；
候选JSON清单文件SHA256 `3aed7c4f4df9bfccb604fb31f71f7acc1ecd6910a1fe19e2142d348e2afd8b37`。
候选共60模块：原48个字节不变，只更新 `cloud_jobs.py` 和
`conversation_authority.py`，新增10个。闭包外45个原模块与六个核心均独立
逐字节核对相等。该实际候选在本机3.14.7的 `-S -B` 子进程再次完成无
SQLite连接/无SDK导入与原8项runtime检查、0skip。未读其他私有配置/DB，
作者未执行生产或远端操作。其后Root已将同一归档传到VPS私有检查
目录，在实际宿主Python3.6.8以`-S -B`完成上述两步：所有清单/核心
hash、无SQLite连接/无SDK导入均通过，8项隔离runtime检查1.204s、
零skip。没有安装生产grant、启动实际listener或调用模型/Cloud任务。

## 使用范围纠正

这片候选保留为绑定真实task/claim的受管执行profile，不直接作为普通
Cloud的一般入口：Leader空闲/零任务时，客户端也必须能用独立owner
有限期身份hello、读状态和提交新任务。该入口与执行租约分离的薄
适配另行实现；真正执行/steering仍保留原task/epoch校验，不造keeper。
因此宿主检查通过不触发自动切换唯一receiver，更不冒称官方Cloud
入网。未来确需该执行profile时再由Root独占接入，保留原ready hook、
owner IPC、入口/Claw及native pins，真实claim/grant与Cloud双向分别验。
