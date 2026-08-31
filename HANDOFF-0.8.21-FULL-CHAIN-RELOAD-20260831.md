# FolderBridge 0.8.21｜全链路重载最终验收与收口交接｜2026-08-31

> 本文件是全链路重载后的正式恢复入口。新会话不得依赖旧 MCP tool state、旧 Job ID、旧进程快照或口头记忆；必须先以当前磁盘现场、`server_info`、Extension catalog 与本文件为准自检。

## 1. 工作区

- 项目：`C:\Claude\Project\folderbridge-mcp`
- workspace：`folderbridge-mcp`
- 旧会话 workspace_id：`126f215203a0`
- 分支：`main`
- 当前版本目标：`0.8.21`

注意：workspace_id 在重载后通常应保持，但仍必须以新的 `server_info` 返回值为准，不可硬编码旧值。

## 2. 本轮目标

本轮是 `0.8.20 -> 0.8.21` 的结构性升级，核心目标：

1. 把旧 workspace-wide mutation lock 升级为 scoped mutation coordinator，使互不冲突的路径写入能够并行，同时保留 opaque workspace mutation 的保守兼容语义。
2. 保持 long foreground -> Job 原地 promotion、真实 worker 退出前不释放 lease、bounded wait -> `WORKSPACE_BUSY`、shutdown close-and-wake 等 0.8.20 安全性质。
3. 将 ComfyUI 从 FolderBridge core/bundled Extension 完整外源化：
   - 标准外源目录：`Plugins/extensions/comfyui/`
   - 用户热加载目录：`%LOCALAPPDATA%\folderbridge-mcp\extensions\comfyui`
   - 外源插件版本：`1.3.0`
   - 不再依赖 `folderbridge_mcp.comfyui`
   - 使用公共最小 ABI：`folderbridge_mcp.extension_api.ExtensionError`
4. ComfyUI `run` 使用动态 mutation scope：
   - 无 `save_directory` -> effective scope `none`
   - 有 `save_directory` -> 仅占该目录 tree scope
5. Windows EXE 不再 bundled ComfyUI，也不再 hidden-import `folderbridge_mcp.comfyui`。
6. Launcher Extensions sidebar 分为：
   - `内置插件 / Bundled Extensions`
   - `外源热加载插件 / External Hot-load Extensions`
7. 保留 Launcher-owned ComfyUI managed-service convenience，但它与 external Extension runtime 解耦；用户手工启动 ComfyUI 时 external Extension 仍应独立工作。

正式设计文件：

- `docs/scoped-mutation-and-external-extension-design-20260831.md`

正式 ABI 文档：

- `docs/extensions.md`

## 3. 已完成的核心实现

已完成并纳入测试的主要内容：

- scoped mutation conflict model：`none / workspace / exact / tree`
- scoped mutation coordinator、fairness、timeout 与 blocker metadata
- `edit_file` / transactional write commit 的 per-path coordination
- Extension manifest 新增声明式 `mutation_scope`
- Extension params -> host-resolved effective scope，在 worker spawn 前完成
- scoped Extension Job 在 promotion / timeout / cancellation / `termination_pending` 下保持 lease 到真实 worker exit
- Task/Capability 保持旧 opaque workspace mutation 兼容行为
- `folderbridge_mcp.extension_api.ExtensionError` 公共最小结构化错误 ABI
- external ComfyUI 独立 runtime、plugin、manifest、README、tests
- external ComfyUI prompt-scoped cancel 保留，禁止回退全局 `/interrupt`
- external ComfyUI `install.ps1`：同卷 staging -> directory cutover -> failure rollback，并拒绝 reparse-point target
- Launcher bundled/external 两区显示与中英文文案
- Windows bundle allowlist 改为仅 release-trusted bundled Extensions；ComfyUI 不再打入 EXE
- `verify_windows_bundle.py` 增加 ComfyUI exclusion 验证
- `docs/extensions.md` 已同步 external ComfyUI / mutation_scope / ExtensionError / managed-service 新语义
- `CHANGELOG.md` 已新增 `0.8.21`
- 项目版本锚点已同步到 `0.8.21`：
  - `pyproject.toml`
  - `folderbridge_mcp/__init__.py`
  - `packaging/windows_version_info.txt`
  - `packaging/windows_dpi_manifest.xml`
  - `tests/test_version.py`

## 4. 测试与实包状态

最后一次版本化后的全量测试：

- `Ran 384 tests`
- `OK (skipped=2)`

两个 skip 均为既有平台条件：

- symlink unavailable
- POSIX mode bits are not authoritative on Windows

Windows 0.8.21 实包已经成功构建并通过构建脚本内置 verifier：

- `C:\Claude\Project\folderbridge-mcp\release\windows-x64\FolderBridge.exe`
- size：`12137205` bytes
- SHA-256：`05d8a4086bbe1e070616e55d8d8a59f4c41dcfad448db267021ba2ca5df22a00`
- smoke：`folderbridge-mcp 0.8.21`

PyInstaller 新构建产物中已进一步检查：

- `.build/FolderBridge.spec` 无 ComfyUI bundled data/hidden import
- `.build/pyinstaller/FolderBridge/PYZ-00.toc` 无 `folderbridge_mcp.comfyui`

因此当前已证明：ComfyUI 不只是“不在 UI bundled catalog”，而是旧 core helper 也未进入 0.8.21 EXE Python archive。

## 5. 最终真实运行态验收事实

2026-08-31 已在新的真实进程中完成最终验收：

- `server_info.version = 0.8.21`
- workspace：`folderbridge-mcp` / `126f215203a0`
- `comfyui 1.3.0`
- `bundled=false`
- 最终 `trusted=true / enabled=true / approval_stale=false / loaded=true`
- 最终已批准 external tree SHA-256：`63d443a169a3c08284681191d2c168a844612e3fb4124fe93080fd3eff817a02`
- external `status` 真连通 `http://127.0.0.1:8188`
- 返回本机 ComfyUI `0.30.1` 与 `NVIDIA GeForce RTX 4070 Ti SUPER`

真实 hot-reload stale 闭环已完成：

1. 在用户 external ComfyUI tree 制造无执行作用的 hash-only 变化；
2. Rescan 后真实观察到 `approval_stale=true / trusted=false / enabled=false / loaded=false`；
3. 恢复标准 plugin tree；
4. 在**不重启 FolderBridge**的情况下重新批准并启用；
5. 同一 0.8.21 进程恢复 `trusted/enabled/loaded=true`。

真实 scoped mutation 运行态也已完成：

- 无 `save_directory`：ComfyUI run 不占 workspace mutation lease；
- 有 `save_directory=tests`：只持有对应 tree lease；
- 对重叠 `tests/test_version.py` 的 edit 等待约 2 秒后返回 `WORKSPACE_BUSY`；
- 同一 Job 运行期间，对无关根目录文件的 edit 可立即成功；
- flight recorder 记录了 tree lease acquire / bounded wait / timeout / true worker exit 后 release；
- opaque Task/Capability 继续使用 workspace scope，回归测试验证 promotion / timeout / termination_pending 下 lease 持续到真实 worker exit。

## 6. 重载前用户需要执行的 PowerShell

在 PowerShell 中执行：

```powershell
Set-Location 'C:\Claude\Project\folderbridge-mcp'

# 1. 安装/更新 external ComfyUI 到 FolderBridge 用户热加载目录
& '.\Plugins\extensions\comfyui\install.ps1'
# 注意：若这段被外层 PowerShell 脚本包装，失败判断应依赖 throw/catch（或 $?），
# 不得在调用 .ps1 后检查 $LASTEXITCODE；它可能残留此前 native process 的旧退出码，
# 从而出现已经打印 Installed 但外层仍误报失败的假失败。

# 2. 可选：核对新 EXE SHA-256
(Get-FileHash '.\release\windows-x64\FolderBridge.exe' -Algorithm SHA256).Hash

# 3. 关闭当前正在运行的旧 FolderBridge 窗口。
#    推荐用 GUI 的“退出”，让 managed services / Tunnel / MCP 走安全 shutdown。
#    不建议直接 Stop-Process 强杀。

# 4. 旧窗口完全退出后启动 0.8.21
Start-Process -FilePath '.\release\windows-x64\FolderBridge.exe'
```

SHA 正确值应为：

`05D8A4086BBE1E070616E55D8D8A59F4C41DCFAD448DB267021BA2CA5DF22A00`

启动 0.8.21 后，在 Launcher 右侧 `Extensions & Skills`：

1. 点 `重新扫描 / Rescan`。
2. 确认出现 `外源热加载插件 / External Hot-load Extensions` 区。
3. `Local ComfyUI` 应在外源区而不是内置区。
4. 第一次 external hash 需要重新批准；批准并启用。
5. 然后重新启动 Tunnel / MCP 连接。

## 7. 新会话启动后的强制自检顺序

新会话必须先做，不得跳过：

1. `server_info`
   - 必须确认 `version = 0.8.21`
   - 如果仍是 0.8.19/0.8.20，立即停止后续验收，先诊断重载未生效。
2. `workspace status` / `git status`
   - 以磁盘现场为准。
3. `extension(list)` / `extension(info, comfyui)`
   - 目标：`comfyui 1.3.0`
   - `bundled: false`
   - `trusted/enabled/loaded: true`（批准后）
   - `run` 应含 optional `save_directory` tree mutation scope
4. `extension(run, comfyui, status)`
   - 验证 external plugin 实际可访问本地 `127.0.0.1:8188`；若 ComfyUI 未启动，应只报告服务离线，不应变回 bundled/core 路径。
5. 核对 GUI：
   - bundled 与 external 两区同时存在
   - ComfyUI / Godot / FFmpeg 均属于 external 区
   - Office / Git Publisher / Skill Engine 属于 bundled 区
6. 真实 hot-reload stale 验收：
   - 在用户安装目录为 external ComfyUI tree 制造一个无害的 hash-only 内容变化（建议增加临时无执行作用文本文件，而不是改 `plugin.py`）
   - Rescan / `extension(list)` 后确认 `approval_stale=true`、加载被阻止
   - 删除临时文件或重新运行标准 `install.ps1` 恢复标准插件 tree
   - 对恢复后的精确 hash 重新批准
   - **无需重启 FolderBridge** 即恢复 `trusted/enabled/loaded`
7. scoped mutation 真实运行态验收：
   - 无 `save_directory` 的 ComfyUI run 不应占 workspace mutation lease
   - 有 `save_directory=Output/...` 时只应阻塞重叠路径；无关文件 edit 应可并行
   - opaque Task/Capability 仍保持 workspace-wide 保守阻塞
8. 若上述全部通过，再处理旧源码物理清理与最终全量测试/打包复验。

## 8. 最终闭合状态

本轮必须项已全部闭合：

- 0.8.21 真运行态确认：完成
- external `comfyui 1.3.0` catalog / approval / enable / live status：完成
- bundled vs external 分区结构与回归：完成
- live hash-stale -> reapprove without restart：完成
- scoped mutation live scenario：完成
- `extensions/comfyui/` 旧 bundled 目录：已物理删除
- `folderbridge_mcp/comfyui.py` 旧 core helper：已物理删除
- `tests/test_comfyui.py`：已删除，完整行为覆盖已迁移到 `Plugins/extensions/comfyui/tests/test_plugin.py`，并由 `tests/test_external_comfyui.py` 主套件显式加载
- 当前 `scripts/` 无 `folderbridge_mcp.comfyui` 引用
- 当前 PyInstaller `PYZ-00.toc` 无 `folderbridge_mcp.comfyui`
- final full suite：`384 tests / OK / skipped=2`
- final Windows build + verifier：通过
- final EXE SHA-256：`05d8a4086bbe1e070616e55d8d8a59f4c41dcfad448db267021ba2ca5df22a00`

仓库内仍可能在 `.build/staged-snapshot-*` 历史快照、文档描述和负向回归断言中看到字符串 `folderbridge_mcp.comfyui`；它们不是当前运行源码依赖，不应为了“搜索零结果”而扩大清理范围。

## 9. 当前工作树纪律

当前工作树本来就存在大量本轮及此前未提交文件。特别包括但不限于：

- `FolderBridge - Copy.exe`
- `_probe.txt`
- `*.bak-taskfix`
- `tools.py`
- `tools_plus_compat_ready.py`
- `write-probe-chatgpt.txt`
- 多个 Plugins / external-extensions / skill-packs 工作副本

**不得为了“干净”而批量删除、reset、clean 或覆盖这些文件。**

必须逐项判断来源。禁止：

- `git reset --hard`
- `git clean -fd/-fdx`
- force push
- 用旧 release / 旧 EXE 覆盖 0.8.21 产物

本轮最终提交前应只纳入经过确认的 0.8.21 目标文件，避免把 probe、copy、bak、无关外源插件工作副本混入提交。

## 10. 后续新会话入口

本轮 0.8.21 最终验收已经闭合，后续新会话**不需要无条件重跑本文件记录的整套 hot-reload / scoped-mutation 实验**。启动时应：

1. 读取本文件与 `docs/scoped-mutation-and-external-extension-design-20260831.md`；
2. 以新的 `server_info` 确认当前运行版本与 workspace_id；
3. 读取 `workspace status`，保留现有非本轮脏文件，不做 broad clean/reset；
4. 若 ComfyUI source/hash、mutation coordinator、Extension ABI、打包 allowlist 或 Windows EXE 自本次收口后发生变化，再按受影响范围重做对应验收；
5. 若只是继续其它 FolderBridge 功能开发，以本次 0.8.21 状态作为已验证基线即可；
6. 未经用户明确要求，不自动 push/release。

## 11. 新会话原则

- 以磁盘现场为准，不依赖本文件之外的旧运行时状态。
- 不复用旧 Job ID。
- 不因为旧 catalog 里曾显示 bundled ComfyUI 就回滚架构。
- 不用 patch 恢复旧 core helper；发现问题优先修 external Extension / generic ABI / launcher 通用机制。
- scoped mutation 不能为了追求并行而虚构 Task/Capability 的精确写范围；没有可靠声明时继续 opaque workspace scope。
- ComfyUI native output 不是 FolderBridge workspace mutation，只有显式 `save_directory` 才进入 host scope。
- 不允许 prompt cancellation 回退到 ComfyUI 全局 `/interrupt`。
- 用户要求的是结构性解决，不是临时兼容补丁。
