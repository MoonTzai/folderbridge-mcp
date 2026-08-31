# Long-running Extension Runtime｜结构性修复设计（2026-08-30）

## 背景与实测证据

2026-08-30 W-T1 现场出现：`debate-judge-adapter/web-unit` 作为 foreground MCP 调用持续约 300.6s；Tunnel 在约 120s 先达到 response deadline，随后新工具调用连续 502，而本地 FolderBridge MCP/worker 仍继续运行并最终正常完成。

这说明问题不是 Judge 单点，而是两个时间域耦合错误：

1. **业务执行时限**：可能持续数分钟到数小时，甚至允许 `0`（无自动超时）。
2. **MCP/Tunnel 同步响应窗口**：必须短且有安全余量；不能用它承载长业务生命周期。

目标是把二者彻底解耦，而不是单纯把某几个 Judge action 改成 `run_mode=job`，也不是把 Tunnel 等待时间硬拉到数小时。

## 结构性目标

### A. Adaptive Foreground → Job

- FolderBridge 内置 `INLINE_FOREGROUND_RESPONSE_BUDGET_SECONDS = 60`（业务 timeout 之外的**传输安全预算**）。
- manifest 仍允许 action 声明 `run_mode=foreground`；短动作保持同步返回兼容性。
- foreground worker 若在 60s 内完成：按现有同步语义返回。
- 若业务 timeout > 60s 或为 0，且 worker 到 60s 仍在运行：
  - **不得终止、不得重启、不得重复执行 action**；
  - 原 worker、原 stdout/stderr capture、原 workspace mutation lease、原业务 timeout 原地移交给 host-owned Job；
  - MCP 调用立即返回 `job_id`、`status=running`、`auto_promoted=true`；
  - 原 action timeout 按 worker 首次启动时间计算，转换 Job 后不得重新计时；`0` 继续表示无自动 timeout。
- 如果 action 自己的业务 timeout <= 60s，则仍按现有 foreground timeout 语义执行，不做 auto-promotion。

### B. Promotion capacity / ownership

- 可 auto-promote 的 foreground 在启动时预留一个 Job capacity，避免到 60s 时 Job 池已满而无法安全脱离 Tunnel。
- 同步完成则释放预留；promotion 时把预留转成正式 Job，不增加第二个 worker。
- workspace mutation lease 只能在**真实 worker 退出并完成 host 清理后**释放；promotion 不得释放/重取 lease。
- shutdown / timeout / cancel 仍统一使用 host-owned process-tree termination 与 `termination_pending` 语义。

### C. Job rediscovery

- 扩展稳定网关新增只读 control action：`extension(action=job_list)`。
- 目的：如果 `run`/auto-promotion 返回恰好在网络故障中丢失，重连后仍能按 workspace 找回 active/recent Job，不形成“孤儿任务”。
- `job_list` 只返回 bounded metadata，不返回未完成 stdout/stderr/业务正文；继续使用 `job_status` 读取单个 Job 的结果。

### D. Built-in liveness / progress health

FolderBridge 不把“进程存在”误判成“业务正在推进”，采用分层证据：

1. `process_alive`：弱信号，只证明 worker 尚未退出。
2. host-observed output activity：中等信号，记录 capture 的最近字节活动时间。
3. optional plugin progress：强信号。host 为 worker 提供 `context.job_progress_path`，插件可原子写入 bounded strict JSON：
   - `seq`：非负整数（可选）
   - `phase`：短字符串（可选）
   - `message`：短字符串（可选）
   - `current` / `total`：数字（可选）
   - `heartbeat_interval_seconds`：插件主动承诺的更新周期（可选，5..3600）

Host 以文件 mtime 作为可信观测时间，不信任插件自报 wall-clock。

`job_status` / `job_list` 返回 `runtime_health`：

- `progressing`：存在最近有效 progress/heartbeat；高置信度。
- `active_output`：近期有 worker output activity；中等置信度。
- `alive_quiet`：进程仍活但无强进度证据；低置信度，**不是失败**。
- `stalled_suspected`：仅当插件曾明确声明 heartbeat cadence，且超过 `max(3×interval, 300s)` 未更新时才进入；只是诊断提示，**绝不自动 kill/cancel**。
- terminal job：`finished`。

没有插件 heartbeat 契约时，即使长时间安静也最多是 `alive_quiet`，避免把 DeepSeek/Browser/编译等长静默阶段误杀。

Progress 文件：host-owned 临时控制目录；最大 64KiB；拒绝 symlink/reparse；解析错误只体现在 health diagnostics，不影响业务 Job。

### E. Transport / control-plane invariant

- 任何 auto-promotable foreground action 最迟应在 60s 左右返回同步 MCP 响应或 Job handle，不再触碰实测约 120s Tunnel response deadline。
- `job_list/job_status/job_cancel/server_info/flight_recorder` 属 control work，应继续独立于 data lane 的长业务生命周期。
- 不修改 Tunnel 本身的业务 timeout；业务任务可持续数小时，显式 Job `timeout_seconds=0` 仍允许无限时限。

## 兼容性与非目标

- 不把所有 foreground action 一刀切为 Job；<60s 正常动作完全保留同步接口。
- 不改变插件 action 的输入 schema/业务语义。
- 不自动拆分插件内部多阶段业务；是否应该拆 action 仍遵守 Extension 设计纪律。
- 不因 quiet/stall suspicion 自动终止任务。
- 不要求现有插件立刻实现 progress；未实现时 health 降级为 process/output 证据。
- 不让 auto-promotion 重新执行 action。
- 不延长 Tunnel 同步等待到小时级。

## 回归测试矩阵

1. foreground < budget：仍同步返回；无 Job 遗留。
2. foreground > budget：同一个 PID/worker 自动 promotion；返回 Job handle；无第二次 handler 执行。
3. promotion race：worker 在 budget 边界退出时只能出现一次终态/一次 lease release。
4. promoted Job timeout：按原始 started time 计算，不额外多 60s。
5. `timeout=0` promotion：无限业务 timeout，Job 正常运行/取消。
6. non-read-only promotion：workspace lease 在 worker 真退出前不释放。
7. Job capacity reservation：不会运行到 promotion 点才因 Job 池满失去运输安全。
8. cancel：promotion 后 cancel token 可用且 process-tree fallback 仍有效。
9. `job_list`：workspace 隔离、active/recent 可恢复、bounded。
10. health：fresh progress→`progressing`；recent output→`active_output`；无强信号→`alive_quiet`；声明 cadence 且超阈→`stalled_suspected`；不自动 cancel。
11. progress path：oversize/invalid JSON/symlink 只降级诊断，不破坏 Job。
12. shutdown / `termination_pending` 既有测试全部保持。
13. server_info 暴露 adaptive policy，不暴露 payload。
14. 全量 tests + Windows package build + packaged smoke。

## MATT 审计轮次

### Round 1｜架构/故障模型审计

初稿候选曾考虑：
- 只改 Judge `web-unit` 为 Job；
- foreground 超时后杀 worker 再以 Job 重启；
- 仅用后台 heartbeat 线程判断“没卡”；

均否决：
- 单点改 manifest 不能修复通用契约缺口；
- 重启会导致副作用重复执行；
- 独立 heartbeat 线程可能在业务 deadlock 时继续跳动，形成假健康。

Round 1 修订后形成本文 A-D：原 worker 原地 promotion、Job rediscovery、分层 health，并明确 quiet 不等于 stalled。

### Round 2｜兼容/并发/恢复审计

发现并补入：
- promotion 点若 Job 池已满，会再次造成无法安全返回，因此必须启动时 reservation；
- promotion 后 timeout 如果重新从零计算，会偷偷放宽业务时限，因此必须 end-to-end 计时；
- auto-promotion handle 可能在网络故障中丢失，因此必须 `job_list` 可恢复；
- heartbeat cadence 必须由插件显式声明，否则 host 不应武断判 stall；
- health 只能诊断，不能自动 kill。

复核后未再发现需要改变接口/所有权模型的实质性问题；**连续两轮收敛**。

## 0.8.19｜实施收口（2026-08-30）

实际实施阶段在不改变业务语义的前提下，把同一个 transport-lifecycle 问题进一步收敛到所有**确实可能跨越同步响应安全窗口、且由 FolderBridge 自己持有外部进程**的入口：

- Extension foreground：保留本文原 worker 原地 promotion 机制；短 foreground 不变。
- `run_task` / project-task `run_capability`：新增独立的 `TaskJobManager`，固定 argv 任务超过共享 transport budget 后原进程转 host-owned Job；短任务继续保持旧同步返回。
- Git inspection（15s）、JS syntax check（10s）、Launcher doctor、TunnelSupervisor、GUI managed service 等有界控制/持久服务路径不纳入 Task Job，避免把“长任务修复”扩张成所有子进程统一异步化。
- Extension Job 与 Task Job 不强行合并为一个大 manager；二者只共享真正共同的进程策略 `TRANSPORT_RESPONSE_BUDGET_SECONDS = 60.0` 与 process-tree termination seam。

实施期 MATT / Code Review 额外发现并修复了以下实质问题；每发现一项即重置最终收敛计数：

1. Task promotion 与 shutdown 边界竞态：shutdown 可能已经开始，另一线程仍登记新 Job；改为 shutdown 在 ownership-transfer 边界获胜。
2. Extension promotion 存在同构 shutdown race；同步补上 manager `_closed` 原子复核。
3. Extension 与 Task 各自维护 60s transport budget 会形成双真源；统一下沉到 `process_control.py`。
4. promoted Task/Capability 在 terminate/kill 失败时曾可能提前发布终态并释放 workspace lease；新增 `termination_pending`，真实进程退出前继续持有 host ownership / lease。
5. 新增 Job 恢复接口最初未全部进入 MCP control lane；现已保证 Extension `job_list/job_status/job_cancel` 与 Task/Capability `list/status/cancel` 均不受 data lane 饱和阻塞，实际 `run` 仍属于 data lane。
6. Flight Recorder JSONL 导出对单条合法 JSON 但异常 `pid` 字段不够容错；现改为坏记录排序字段降级，不再让一条损坏记录拖垮完整 15 分钟导出。

Launcher 同时新增 **“导出最近15min飞行日志”** 按钮，紧邻“官方文档”右侧。导出直接读取内置 Flight Recorder 的最近 15 分钟本地 JSONL，不经过待诊断的 Tunnel；继续执行现有脱敏规则与 20 MiB 总量边界，并可跨 launcher/mcp role 汇总完整窗口，而不是 MCP 预览接口的最多 200 条。

### 最终验证证据

- 实施完成后的 MATT 最终复核：**连续 2/2 收敛**；未再发现需要扩大接口或改变所有权模型的实质问题。
- 源码全量回归：**356 tests OK / 2 skipped / 0 failures / 0 errors**。
- 发布版本：**FolderBridge 0.8.19**。
- Windows 产物：`release/windows-x64/FolderBridge.exe`。
- packaged verifier 已增强为实际启动构建后的 EXE，通过 stdio MCP 发送 `initialize` + `tools/list`，确认 `flight_recorder`、Extension Job 管理动作，以及 Task/Capability `run/list/status/cancel` 均真实存在于打包产物；随后 bundled Extensions / Skills / worker self-test 与 `--version` smoke 全部通过。
- 最终 EXE SHA-256：`d7b40630ecfbf0686a1c912858e93be39877b53f7b54c739195f2de9f707cd95`。
- Windows 版本资源：`FileVersion/ProductVersion = 0.8.19`；manifest identity = `0.8.19.0`。

> 说明：完成本节时，当前正在承载本会话 MCP/Tunnel 的仍是旧 **0.8.18** 进程；0.8.19 已完成构建与 packaged 验收，但必须在后续全链路重载后才成为实际运行态。重载后第一步应以 `server_info` 核验运行版本和新 `task_jobs` / adaptive policy，而不是依赖本节记录。
