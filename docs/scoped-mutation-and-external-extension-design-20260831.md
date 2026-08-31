# FolderBridge 0.8.21｜Scoped Mutation + External ComfyUI 设计

基线：`main@21baa7e2dc671dee676b84145a99ec797722a05c` / FolderBridge `0.8.20`

## 1. 目标

本轮只解决三个彼此相关的结构问题：

1. 同一 workspace 内，能够证明写入范围不冲突的动作允许真实并行；证明不了的动作继续保守串行。
2. 把当前 ComfyUI 从 bundled Extension + core workflow helper 剥离为标准、可热扫描、exact-hash 批准的外源 Extension；不再随 `FolderBridge.exe` 打包。
3. Launcher 的 Extensions UI 明确区分“内置插件”和“外源 · 可热加载插件”，但仍复用同一套 registry / enable / details 管理逻辑。

必须保留 0.8.20 已验证的安全与可靠性：bounded wait → `WORKSPACE_BUSY`、长执行原地转 Job、Job lease 持有到真实 worker 退出、shutdown close-and-wake、Flight Recorder blocker 诊断、路径不持久化到飞行记录、固定 MCP tool catalog、旧 Extension ABI 向后兼容。

## 2. 非目标

- 不通过取消 workspace mutation protection 来换取并发。
- 不让插件自行执行一段“scope resolver 代码”后再决定锁；scope 必须由 host 在启动 worker 前静态解析。
- 不把 Tunnel client deadline 后自动重连混进本轮；那属于 Tunnel client 自身状态机。
- 不在本轮把所有 Task / Capability 都强制改成精细 scope。未声明的 Task / Capability 继续 `workspace` opaque；协调器接口为以后扩展预留。
- 不把外源插件视为 OS sandbox。Scope 是并发协调契约，不扩大或替代权限边界。

## 3. 核心模块：MutationCoordinator

### 3.1 为什么替换两套锁

0.8.20 同时有：

- `WorkspaceMutationGate`：known file write = shared；opaque mutation = exclusive；
- `ResourceLockTable`：同文件再次串行。

两套锁维护相同的“谁与谁冲突”知识，容易产生旁路和双重等待。0.8.21 将冲突知识集中到一个深模块 `MutationCoordinator`。

### 3.2 Host 内部数据模型

```text
MutationScope
  kind = none | workspace | paths
  claims[]

MutationClaim
  kind = exact | tree
  path = canonical absolute path inside selected workspace
```

`none` 不申请 workspace lease；`workspace` 与该 workspace 内任何 mutation 冲突；`paths` 按 claim 交集判冲突。

冲突规则：

- workspace ↔ anything = conflict
- exact(A) ↔ exact(B) = A == B
- exact(A) ↔ tree(B) = A == B 或 A 位于 B 子树内
- tree(A) ↔ tree(B) = A == B，或 A/B 任一是另一方祖先

所有 path claim 都必须先经过 `Workspace.resolve(..., for_write=True)` 的既有 confinement / link / sensitive-path 规则；manifest 中不接受 glob。路径身份使用解析后的绝对路径并遵守平台 `normcase` 语义，Windows 上不能把仅大小写不同的同一目标当作两个独立 claim。

一个 action 的全部 claims 必须作为**一个原子 lease 请求**交给协调器；禁止逐 claim 获取多把锁，避免 A→B / B→A 的 ABBA 死锁。Manifest 每个 action 最多声明 32 个 claims，runtime 解析后的 claims 同样受 32 条硬上限约束，冲突检查始终有界。

### 3.3 公平性

协调器维护 active leases + ordered waiters。

一个 waiter 可以获得 lease，当且仅当：

1. 它与所有 active lease 不冲突；并且
2. 它与所有排在它前面的 waiter 都不冲突。

这样不同路径可以越过彼此并行，但一个已经排队的 `workspace` opaque writer 不会被后续零散文件写无限饿死。

等待仍使用 `WORKSPACE_MUTATION_WAIT_SECONDS = 2.0`；超时移除 waiter、notify_all，并返回结构化 `WORKSPACE_BUSY`。任何同步 foreground tools/call 都不能静默等到接近 transport response budget。

### 3.4 生命周期

`MutationLease` 继续：

- 可跨线程释放；
- `update_owner(job_id, pid, ...)`；
- Job promotion 后由 host 持有到 worker 确认退出；
- timeout/cancel 进入 termination_pending 时绝不提前释放；
- shutdown 只关闭新 admission 并唤醒 waiter，不强行释放 live holder。

Flight Recorder 记录 mode/scope kind/wait/blocker/action/job_id/pid 等元数据，但持久化层继续剥离任何 `*_path` 字段。

## 4. Core 写工具接入

- `edit_file`：申请一个 `exact` claim；不再额外经过 ResourceLockTable。
- `write_file(commit)`：申请目标文件 `exact` claim。
- 不同文件可并行；同文件串行；与 `workspace` opaque Task/Capability/旧插件冲突。

`ResourceLockTable` 在所有调用方迁移后删除，避免保留第二套真实锁语义。

## 5. Extension ABI：mutation_scope

Extension schema_version 继续为 1，新增**可选** action 字段 `mutation_scope`，因此旧 manifest 无需改版本即可兼容。

### 5.1 兼容默认

若 action 未声明 `mutation_scope`：

- `read_only: true` → `none`
- `read_only: false` → `workspace`

这与 0.8.20 行为一致，因此现有 Godot、FFmpeg、Judge 等外源插件不改 manifest 仍安全运行。

### 5.2 显式格式

```json
"mutation_scope": {
  "mode": "none"
}
```

或：

```json
"mutation_scope": {
  "mode": "workspace"
}
```

或：

```json
"mutation_scope": {
  "mode": "paths",
  "claims": [
    {"path": "Output/report.json", "kind": "exact"},
    {"path": "ComfyUI/output", "kind": "tree"},
    {"param": "save_directory", "kind": "tree", "optional": true}
  ]
}
```

约束：

- 每个 action 最多 32 个 claims；每个 claim 恰好包含 `path` 或 `param` 之一；
- `param` 只能引用 action input_schema 顶层已声明的 string property；
- `optional=false` 时参数缺失直接 `INVALID_ARGUMENT`；
- `optional=true` 且参数缺失时忽略该 claim；
- runtime 解析后 `paths` 可以得到 0 个 claim，此时等价 `none`；
- 不接受 wildcard/glob、绝对路径、`..`、链接逃逸；
- **显式声明** `paths` / `workspace` 时，需要 action `requires_workspace:true`，且 manifest 声明 `workspace.write`；旧 manifest 通过兼容默认推导出的 `workspace` 不新增权限合法性要求，避免宿主升级把既有插件判成无效；
- `read_only` 不再被当成 mutation scope 的同义词；旧 manifest 未声明 scope 时仍按 `read_only:true → none / false → workspace` 推导以保持兼容；
- 显式声明后，`read_only:true` 可以具有**仅由可选参数触发**的 path claim，但 `authorization:none` 仍只能是静态 `none`；
- FolderBridge 处于 read-only 模式时，只要本次 effective scope 非 `none`，host 必须在 spawn worker 前拒绝；
- `read_only:false + scope=none` 合法，用于“会改变外部系统、但不修改 FolderBridge workspace”的动作。

Scope 参与 exact-hash approval；内容或 scope 改变都会使外源插件旧批准 stale。

`read_only` 与 `mutation_scope` 是两个不同维度：前者保留现有动作级授权/只读模式语义，后者只描述本次调用对 FolderBridge workspace 的协调范围。一个历史上 `read_only:true` 的动作可以声明**仅由可选参数触发**的 path claim；当该参数缺失时 effective scope=`none`，当参数存在时 host 在 worker 启动前得到非空 scope。FolderBridge 全局处于 read-only 模式时，任何 effective scope 非 `none` 的调用都必须由 host 拒绝，即使 action 静态 `read_only:true`。`authorization:none` 的 action 则只能声明静态 `none`，不得存在任何可能产生 workspace mutation 的 claim。

Scope 是 host 并发调度契约，不宣称能够 OS 级阻止恶意插件绕过声明直接写文件；外部 Extension 仍遵守既有“不是真 OS sandbox”的安全说明。`PreparedExtensionRun` 应保存已经通过参数 schema 校验并由 host 解析完成的 effective scope，dispatcher 不得在加锁后重新从原始 params 推导第二次。

## 6. Task / Capability

本轮默认不扩 `.folderbridge.json` schema：

- approved Task：`workspace`
- project-code Capability：`workspace`
- builtin read-only/safe inspection：无 mutation lease

原因：Task/Capability 的真实输出范围通常由任意项目代码决定，贸然做静态 path 声明会制造虚假安全。`MutationScope` 是 host 通用 seam，后续有第二个可靠调用者时再为 Task config 增加声明式 scope。

## 7. ComfyUI 外源化

### 7.1 目标目录

建立：

`Plugins/extensions/comfyui/`

至少包含：

- `folderbridge-extension.json`
- `plugin.py`
- `README.md`
- `install.ps1`
- `tests/test_plugin.py`

运行代码只用 Python 标准库 + Extension context + 一个稳定的通用 Extension SDK 错误接口，不再 `import folderbridge_mcp.comfyui` 或其它产品专用 core helper。

新增极小公共 seam：`folderbridge_mcp.extension_api.ExtensionError`。它只承载 `code / message / details`，由 `extension_worker` 识别并序列化成既有错误 envelope。外源插件不应为了结构化错误再导入内部 `security.ToolError`。该 SDK 不提供文件/网络便利函数，避免把 host internals 泄漏成宽接口。

### 7.2 从 EXE 移除

- 删除 bundled `extensions/comfyui/`；
- 删除 `scripts/build_windows.ps1` 的 comfyui bundle allowlist 和 `folderbridge_mcp.comfyui` hidden import；
- `verify_windows_bundle.py` 明确断言 ComfyUI 不在 EXE bundled extension 列表；
- 原 `tests/test_comfyui.py` 的行为测试迁移为外源插件测试；
- 当 `folderbridge_mcp/comfyui.py` 不再有 core 引用后删除。

### 7.3 ComfyUI mutation scope

`status`：`read_only:true`, effective scope=`none`。

`run` 保持当前 `read_only:true`, authorization=`global` 的兼容语义；workspace scope 只覆盖 FolderBridge 明确控制的 workspace 写入：

```json
"mutation_scope": {
  "mode": "paths",
  "claims": [
    {"param": "save_directory", "kind": "tree", "optional": true}
  ]
}
```

`save_directory` 未提供时 effective scope=`none`，因此 FolderBridge 只读模式仍可运行普通生成；提供 `save_directory` 时 effective scope 为该 tree，若 FolderBridge 处于只读模式则 host 在启动 worker 前返回 `READ_ONLY`。ComfyUI 服务自己的 native output 目录属于外部服务状态；除非它被显式映射为 FolderBridge workspace 写入，否则不伪装成 host 可强制协调的范围。

取消仍使用 host `job_cancel_path` + prompt-scoped cancel endpoint；禁止退化到全局 `/interrupt`。

### 7.4 Managed Service

0.8.21 不把“启动/停止本机 ComfyUI 服务”的 Launcher convenience 一起重构成通用 service ABI。若现有 `managed_services.py` 只因 `folderbridge_mcp.comfyui` 的 status helper 产生 core import，则将 status probe 改成一个小型 loopback `/system_stats` probe，保持 managed service 可选体验。

ComfyUI Extension 即使不使用 Launcher 托管服务，也必须能在用户手工启动 ComfyUI 后独立热加载运行。

## 8. Launcher UI：内置 vs 外源热加载

Registry 继续输出现有 `bundled: bool`，UI 不增加第二套数据模型。

Extensions 区域分为两个有标题的连续 section：

1. `内置插件` / `Bundled Extensions`
2. `外源热加载插件` / `External Hot-load Extensions`

每张卡增加来源标签，并明确区分“代码来源信任”与“高权限动作授权”：

- `内置 · 随 FolderBridge 发布 · 代码由 release 信任`
- `外源 · 可热加载 · 代码需精确 hash 批准`

外源 section 顶部保留“插件目录 / 重新扫描”说明；外源内容改变后继续显示 approval stale。Bundled action 若声明 `authorization=global`，仍保持现有的显式启用、hash/permission 绑定和 stale/revoke 行为；不能因为代码来源是 bundled 就取消用户撤销高权限动作授权的能力。

启停、批准/撤销、详情、managed-service controls 继续走同一函数与 trust store，不复制第二套逻辑；UI 只增加来源分组和更准确的语义标签。

ComfyUI 外源化后应自动从第一 section 移入第二 section；Godot / FFmpeg 等现有外源插件也归第二 section。

## 9. TDD 垂直切片

按以下顺序，每个 slice 必须先红后绿：

1. `MutationScope` 冲突判定：exact/exact、exact/tree、tree/tree、workspace。
2. coordinator：两个不冲突 path scope 同时获得；冲突 scope 串行。
3. fairness：早到 workspace waiter 阻止后续与其冲突的新 path writer 插队；无关 scope 可继续并行。
4. timeout：冲突 waiter 约 2s → `WORKSPACE_BUSY`；移除后唤醒其它 waiter。
5. Job lifecycle：scoped Extension Job promotion 后 lease 持有到真实 exit；termination_pending 不提前释放。
6. core `edit_file/write_file(commit)`：同文件串行、不同文件并行、opaque task 阻塞 exact write。
7. Extension manifest parser：旧 manifest 默认行为不变；新 scope 合法/非法矩阵。
8. Extension dispatcher：params → host-resolved scope，并在 worker spawn 前获取 lease。
9. Godot existing manifest regression：不修改 Godot 仍按旧默认运行。
10. Extension public error ABI：`ExtensionError` 能跨 worker 保留 code/details；普通未知异常仍归 `EXTENSION_WORKER_EXCEPTION`。
11. external ComfyUI behavior parity：status、workflow validation、dynamic combo、artifact metadata、prompt-scoped cancel、timeout及原有结构化错误码。
12. ComfyUI scope：无 save_directory 时不占 workspace lease；有 save_directory 时只与该 tree 冲突。
13. Windows bundle：EXE 不含 ComfyUI bundled plugin/helper；新增 `tests/test_external_comfyui.py` 作为仓库全量测试入口，并保留 `Plugins/extensions/comfyui/tests/test_plugin.py` 供插件自身独立测试。
14. Launcher UI：同一 catalog 被稳定分成 bundled/external 两区；ComfyUI/Godot 归 external；来源标签与授权按钮语义正确。
15. full suite + packaged smoke。

## 10. 运行态验收

至少做以下真实场景：

A. 同一 workspace：长 GPT/Comfy scoped Job + `edit_file` 修改无关文件 → 两者并行，不出现 `WORKSPACE_BUSY`。

B. scoped Job 正在写 `Output/**` + `edit_file Output/x.md` → 约 2s `WORKSPACE_BUSY`，原 Job 不受影响。

C. opaque Task/Capability 长 Job + 任意 `edit_file` → 约 2s `WORKSPACE_BUSY`，Tunnel 不掉线。

D. 两个不同 tree scope Extension Job → 真并行；相同/祖先重叠 tree → 串行。

E. ComfyUI 外源目录内容改一个字节 → approval stale；恢复/重新批准后无需重启 FolderBridge 即可热加载。

F. UI 同时出现 bundled 与 external section，ComfyUI/Godot/FFmpeg 来源显示正确。

## 11. 提交边界

不得把工作副本、probe、bak、EXE copy 混入提交。

建议分三个 commit：

1. `refactor: add scoped workspace mutation coordinator`
2. `refactor: move ComfyUI to external hot-load extension`
3. `ui: distinguish bundled and external extensions`

每个 commit 都应独立可测试；最终再 full test + package-windows + 运行态验收。
