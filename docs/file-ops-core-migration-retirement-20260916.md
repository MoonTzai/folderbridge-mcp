# File Ops Toolkit → FolderBridge Core `file_ops` 迁移 / 退役 SOP

日期：2026-09-16
目标版本：FolderBridge 0.8.34

## 目标

FolderBridge 0.8.34 将普通文件 copy / move 收编为 Core `file_ops` 工具，并原生支持：

- 同 workspace copy
- 同 workspace move
- 跨 workspace copy
- 跨 workspace move
- 默认 no-clobber
- overwrite + `expected_destination_sha256`
- 可选 `expected_source_sha256`
- `create_parents`
- 源/目标 workspace 双侧 confinement
- 跨 workspace 双侧 mutation lease
- 跨 workspace move 的 verified copy → destination size/SHA verify → source stability recheck → source delete

外源 `file-ops-toolkit` 不再作为 0.8.34 之后的公开发布能力，但**已有本地安装不会由新版本自动停用、屏蔽或删除**。这是一个显式、可回滚的迁移。

当前本机已安装路径：

`C:\Users\Moon\AppData\Local\folderbridge-mcp\extensions\file-ops-toolkit`

当前 live 0.8.32 进程仍可继续使用该外源插件。不要在仍有平行进程依赖它时修改或删除这个目录。

---

## Phase A｜0.8.34 更新前

1. 保持现有 `file-ops-toolkit` 安装目录原样。
2. 保持现有批准状态原样。
3. 不删除、不重命名、不覆盖本地安装目录。
4. 不通过 trust store 手工改 JSON。
5. 允许当前仍在运行的旧 FolderBridge 平行进程继续使用旧插件。

仓库侧可以停止发布 File Ops Toolkit，但这不等于删除当前用户插件目录。

---

## Phase B｜更新到 FolderBridge 0.8.34

更新 / 启动 0.8.34 后，先 fresh 核对：

1. `server_info.version == 0.8.34`
2. `server_info.builtin_tools` 包含 `file_ops`
3. `server_info.security.file_ops_cross_workspace == true`
4. 本地 `file-ops-toolkit` 仍可在 Extension 列表中看到（如果尚未手动停用/删除）
5. 此阶段不要把“旧插件仍存在”判为失败；它是预期的迁移中间态。

注意：Extension 启用/停用状态位于共享的 FolderBridge 用户信任状态中。**如果还有其它正在运行的平行进程需要旧 File Ops Toolkit，不要先停用它。**

---

## Phase C｜人工停用旧外源插件（不删除）

在确认平行进程已经不再需要旧 File Ops Toolkit 后：

1. 打开 FolderBridge 0.8.34 Launcher。
2. 打开右侧 `Extensions & Skills`。
3. 找到 `File Ops Toolkit 0.1.0`。
4. 取消启用 / 停用它。
5. **不要点击“撤销批准”。**
6. **不要删除插件目录。**
7. 重新扫描后，应看到该插件仍安装，但不再 loaded/enabled。

这样如果 Core 验收失败，可以立即重新启用旧插件回滚。

---

## Phase D｜Core `file_ops` 正式验收

旧插件停用后，只允许通过 Core `file_ops` 做以下 live smoke。测试必须使用可删除的临时样本，不要拿真实生产文件作为第一次验收样本。

### D1｜同 workspace copy

- 创建源样本
- `file_ops(action=copy)` 到同 workspace 新路径
- 核对 size + SHA-256 + 原源文件仍存在

### D2｜同 workspace move

- `file_ops(action=move)`
- 核对目标内容/SHA
- 核对源已消失
- 核对 no-clobber 默认生效

### D3｜跨 workspace copy

必须使用两个当前 FolderBridge 已配置的 workspace：

- `source_workspace_id = A`
- `destination_workspace_id = B`
- `source_path` / `destination_path` 都保持 workspace-relative

验收：

- A 中源仍存在
- B 中目标出现
- bytes / size / SHA 一致
- 返回的 source/destination workspace ID 正确

### D4｜跨 workspace move

- A → B move
- 必须返回 verified-copy-delete 路径语义
- B 目标 size/SHA 与源一致
- 目标验证完成后 A 源才允许被删除
- 若最后删除源失败，应报告 partial，而不是伪装成功

### D5｜overwrite guard

- 目标已存在时，不带 `overwrite=true` 必须拒绝
- `overwrite=true` 但不带 `expected_destination_sha256` 必须拒绝
- stale SHA 必须拒绝
- 当前正确 SHA 才允许覆盖

### D6｜边界安全

必须确认拒绝：

- `..`
- 绝对路径
- symlink / junction / reparse path
- `.git`
- dependency/generated 路径
- credential-like 文件
- `.folderbridge.json` 目标写入
- 未配置的 destination workspace ID

### D7｜双 workspace mutation protection

跨 workspace 操作进行期间，A 源路径和 B 目标路径不能被冲突 mutation 并行写入；测试层必须证明两边 lease 都真实持有，而不是只锁源 workspace。

### D8｜确认旧插件零调用

本轮验收期间不得调用：

- `extension/file-ops-toolkit/copy-file`
- `extension/file-ops-toolkit/move-file`

只允许 Core `file_ops`。

只有 D1–D8 全部 GREEN，才进入 Phase E。

---

## Phase E｜最终撤销批准 + 删除旧本地插件

**只有在用户明确确认 Core 全功能验收 GREEN 后执行。**

### E1｜先撤销旧插件批准

在 Launcher 中对 `File Ops Toolkit` 点击“撤销批准”。

重新扫描，确认旧插件不再 trusted / enabled / loaded。

### E2｜再删除本地插件目录

PowerShell：

```powershell
$target = Join-Path $env:LOCALAPPDATA 'folderbridge-mcp\extensions\file-ops-toolkit'

if (Test-Path -LiteralPath $target -PathType Container) {
    Remove-Item -LiteralPath $target -Recurse -Force
}

if (Test-Path -LiteralPath $target) {
    throw "File Ops Toolkit directory still exists: $target"
}

Write-Host "File Ops Toolkit local installation removed: $target"
```

### E3｜最终 fresh 验收

重启 / 重新扫描后：

1. `file-ops-toolkit` 不再出现在 Extension 列表
2. Core `file_ops` 仍存在
3. 再做一次最小同 workspace copy + 跨 workspace copy
4. 不出现旧插件 missing / stale approval / duplicate implementation 错误

完成后，File Ops Toolkit 才算正式退役。

---

## 回滚

Phase C–D 期间如果 Core 验收出现 blocker：

1. 停止 Core smoke
2. 不删除旧插件目录
3. 在 Launcher 中重新启用已批准的 `File Ops Toolkit 0.1.0`
4. 保留失败证据和测试样本
5. 修复 Core 后重新从 Phase C 开始

Phase E 删除之后若极端情况下需要历史回滚，本机源码快照保存在 Git 忽略目录：

`C:\Claude\Project\folderbridge-mcp\local-private\retired\file-ops-toolkit-0.1.0`

该快照不属于公开插件发布面，不应重新上传 GitHub Release。
