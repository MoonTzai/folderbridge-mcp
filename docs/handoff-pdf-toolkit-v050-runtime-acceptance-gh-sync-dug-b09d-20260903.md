# 新会话交接｜PDF Toolkit v0.5.0 运行态验收 → GH 外源插件同步 → DUG B09D｜2026-09-03

## 0. 基本纪律

新会话不要依赖旧会话记忆、旧 Job ID、旧口头结论或旧插件运行态。首先以 current FolderBridge `server_info`、`folderbridge-mcp` / `Debate-Universal-Grammar` 磁盘现场和当前 Extension catalog 为准重建基线。

明确用户约束：

- PDF Toolkit 必须完成真实运行态验收后才算 PASS；静态测试/加载成功不能替代 runtime acceptance。
- GH 同步只处理“GitHub 已存在且本地确有更新”的外源插件/相关公开资产；先 fresh 检查本地/远端关系、许可证、变更范围，再选择性提交/推送。
- **Debate-Judge 不推送。** 不允许因为 Judge workspace 可见或 adapter 存在而顺带发布 Debate-Judge 项目。
- 不夹带并行无关改动；`folderbridge_mcp/gui.py`、`tests/test_gui_041_regressions.py` 等当前已有并行修改不能未经审计混入 PDF Toolkit / 外源插件发布。
- 不 force push。

## 1. FolderBridge 当前现场

2026-09-03 收口前 fresh `server_info`：

- FolderBridge version: `0.8.21`
- workspace `folderbridge-mcp`: `126f215203a0`
- workspace `Debate-Universal-Grammar`: `cdc7fb5f69fa`
- workspace `Debate-Judge`: `3c7b944da9d6`
- PDF Toolkit catalog：`0.5.0`，external，trusted=true，enabled=true，loaded=true，approval_stale=false
- installed exact extension tree SHA-256：`c81dc21e3fce6897c3487e903397e9faa4dc6637b549d0519ea5ad70a542af08`
- permissions：`workspace.read`, `workspace.write`, `process.execute:powershell.exe`
- public actions：`status / info / outline / read-pages / search / render-pages`

`folderbridge-mcp` 当前 git status 仍有并行工作：

- modified: `Plugins/extensions/README.md`
- modified: `folderbridge_mcp/gui.py`
- modified: `tests/test_gui_041_regressions.py`
- untracked: `Plugins/extensions/pdf-toolkit/*`
- untracked: `Plugins/skill-packs/video-storyboard-production/*`
- untracked: `Plugins/skill-packs/install-video-storyboard-production.ps1`
- untracked: `scripts/install_mattpocock_full_skill_pack.ps1`
- untracked: PDF Toolkit design/audit docs + tests

因此任何 commit/push 前必须 fresh `workspace status/diff`，按功能边界挑文件，不得 `git add -A` 式混推。

## 2. PDF Toolkit v0.5.0 设计/实现状态

源码位置：

`C:\Claude\Project\folderbridge-mcp\Plugins\extensions\pdf-toolkit\`

核心设计：

- text/metadata/outline/page geometry：vendored-only `pypdf==6.16.2`
- pinned wheel SHA-256：`c8b09a59399062fb45a1b8156c18a787a10a3dae03ac9674397a226712c94604`
- visual render：固定 `pdf_render.ps1` → Windows `Windows.Data.Pdf`
- 运行时无 network permission
- PowerShell permission 仅固定 renderer seam，不向调用者开放任意命令/executable path
- render 只写 fresh `output_dir`，无 overwrite；最后写 `RENDER-COMPLETE.json`
- 72–400 DPI；30M pixels/page；200M pixels/call；512 MiB artifact cap
- text layer 明确是 document-supplied/untrusted；关键规则/布局必须 render → `image_open` 视觉核验

设计审计文档：

- `docs/pdf-toolkit-external-extension-design-20260903.md`
- `docs/pdf-toolkit-matt-redesign-and-audit-20260903.md`

MATT：设计层曾完成连续两轮 clean convergence；v0.4 真实 frozen-host 运行失败后显式 reopen design gate，改成 v0.5 pure-Python text + Windows renderer；v0.5 implementation hardening 已补 owned process tree/cancel、PNG IHDR actual-pixel verification、outline truncation、renderer pre-render actual-geometry pixel budget、default-branch upstream refresh safety等。

最近全仓回归：`417 tests`，PDF Toolkit 专项全部 PASS；只剩已有、与 PDF Toolkit 无关的 core failure：

`test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body`

原因：runtime instructions `6001 > 5000`。不要为了 PDF Toolkit 擅自修改该并行 core 问题。

## 3. **最新真实运行态结果：PDF Toolkit 尚未 PASS**

本会话收口前 fresh 运行：

`extension(pdf-toolkit).status`

结果：Extension 本身已加载，但：

- `ready=false`
- backend=`pypdf + Windows.Data.Pdf`
- `vendor_dir_present=true`
- `pdf_render_script_present=true`
- `powershell=C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe`
- renderer capability `page_render_png=true`
- **text backend capabilities 均 false**：metadata/outline/text_layer/literal_search=false
- `loaded_pypdf_version=null`
- `vendor_provenance=null`
- error: `ExtensionError: Could not import the approved vendored pypdf backend.`
- status install hint 仍建议重新 `install.ps1 -Force` 后 rescan/re-approve

因此当前最优先任务不是 DUG，也不是 GH：**先定位为什么 installed v0.5 tree 的 `_vendor` 存在但 pypdf/provenance 运行态不可用。**

不要从“trusted/enabled/loaded=true”推断 plugin ready。

推荐 fresh 排查顺序：

1. `server_info` / `extension info pdf-toolkit` / `extension status` 复现。
2. 对照 source `install.ps1`, `plugin.py` 中 `VENDOR-PROVENANCE.json` 位置/字段、wheel extraction、vendored import path、frozen worker sys.path/clean-env 约束。
3. 如需修复，继续遵守 TDD：先 red regression → 最小修复 → PDF 专项 → 全仓回归 → implementation review 连续两轮 clean。
4. 给用户一次新的 `install.ps1 -Force` 命令；用户 rescan/re-approve 后 fresh runtime acceptance。
5. runtime acceptance 必须完整跑通后才能称 PASS：
   `status → Ottawa info → search → read-pages → render-pages → image_open`。

## 4. Ottawa WUDC 2027 首个验收 PDF

DUG workspace 文件：

`Upload/分析资料原始文件/Ottawa WUDC Debating & Judging Manual - Final Version.pdf`

fresh file_info：

- bytes: `1,447,609`
- SHA-256: `929389446cbf07637dc0df0629c6446ed6e900ade17dec02e4a278121e624a3e`

验收至少做：

1. `info`：页数、metadata、outline/text-layer状态、same SHA。
2. `search`：至少 `counter-proposition`, `definition`, `model`, `burden`, `ordinary intelligent voter`。
3. `read-pages`：命中页及必要前后页，记录 exact page provenance。
4. `render-pages`：封面、版本页/版权页、目录、关键条款页到 fresh output dir。
5. `image_open`：逐页目视核对 PDF rendered page 与 extracted text。
6. 再 fresh 对照 WUDC official manual landing/publication metadata；能取得官方原 PDF bytes 时才做 byte-for-byte SHA 对撞。未完成同字节证明前，准确写 high-confidence official candidate，不虚构 identical bytes。

## 5. GH 同步待办（PDF runtime PASS 后）

用户要求：**“看一下 GH，GH 有但是本地更新的外源插件都更新一下，Judge 不要推送。”**

执行原则：

1. fresh 检查 `folderbridge-mcp` origin/current branch/remote head/status/diff；必要时用 Git Publisher / approved `git-push`，禁止 force push。
2. 识别哪些 external plugins / Skill assets 已经在 GitHub 公开仓库存在，且本地相对远端确有更新。
3. 只发布这些已存在的公开组件；新建但用户没有要求公开的新组件，不能因为它在工作区就自动推。
4. PDF Toolkit 若属于本轮新公开资产，先确认用户原意/当前 repo 发布结构，再决定是否作为 folderbridge-mcp 的新 external extension 纳入；不要假设“GH 已存在”涵盖它。
5. 严格排除 Debate-Judge 项目发布；也不要把 Judge 本地代码、报告或工作支线混入任何 commit。
6. 发布前确保对应 tests/README/version/NOTICE/license 一致。

## 6. DUG 当前正式控制面

fresh DUG rolling state：

- `latest_completed_batch = B09C`
- `next_batch = B09D`
- closure: `B09C_double_smoke_formally_closed_next_B09D`
- cumulative independent issues: `113`
- S1=0 / S2=54 / S3=59 / S4=0
- B09 issues=3（S2×2，S3×1）
- next issue=`B09-004`
- master `mutation_authorized=false`

master：

`《辩论筑基知识体系》.md`

- bytes: `376,188`
- SHA-256: `2960c2beaadacb0827fac0810b6cd926d37a3751bb3d8decc51bcf7641b77e2e`

B09D prompt：

`03-提炼笔记/下一批提示词-B09D-跨LLM-20260903.md`

- SHA-256: `a7018b0dfaa219d92f01b9358d9b08d80c85c81bb1b2d31b36eb2d19ba26fc58`

B09D 唯一起点：

- 第9.1讲 PPTX P1
- SRT cue1 / `00:00:02,480`
- 不跳麦克风测试、版权、前言、讲师自我限权

B09D 必须继续执行：

- current Oregon / BP / WUDC / 国际论证学只用切实可核权威出处，禁止模型记忆和虚构页码；
- 真实理论/制度分歧写 `DISPUTE NOTE`：争议对象、DUG操作口径、制度/学派A/B、双方具体来源、证据层级、适用边界、DUG处理；
- 制度规则与同行评议学术理论分栏；
- current WUDC 条文应优先从 Ottawa 2027 official/manual local PDF 精确核证，Panama 2025 只能标历史见证；
- 涉及胜负/比分/裁判职责/举证责任/现役判准时，current `C:\Claude\Project\Debate-Judge\web\judge.html` 是 Judge-first 唯一现役正式权威，根 `Skill-Judge.md` 只导航；
- master 继续只审不改。

## 7. 新会话建议执行顺序

`fresh FolderBridge self-check`
→ `PDF Toolkit v0.5 status 复现`
→ `修 vendored pypdf/provenance runtime blocker`
→ `MATT/TDD implementation review convergence`
→ `用户重新 install/rescan/approve（如需要）`
→ `Ottawa PDF 全链 runtime acceptance`
→ `GH existing external-plugin selective sync，Judge excluded`
→ `根据实际 PDF 能力必要时更新/补充 B09D prompt/交接，但不要破坏 B09C freeze`
→ `正式进入 DUG B09D`。
