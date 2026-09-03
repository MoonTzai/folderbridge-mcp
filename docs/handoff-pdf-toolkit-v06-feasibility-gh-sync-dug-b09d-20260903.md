# PDF Toolkit v0.6 feasibility → runtime acceptance → GH 外源插件同步 → DUG B09D｜新会话正式交接｜2026-09-03

> 本文件是新会话唯一正式交接入口之一。新会话不得依赖旧会话记忆、旧 Job ID、旧插件运行态、旧口头结论或未落盘推理；必须首先以 current FolderBridge 与磁盘现场重新自检。

## 0. 工作区与硬门禁

- `folderbridge-mcp`
  - workspace_id: `126f215203a0`
- `Debate-Universal-Grammar`
  - workspace_id: `cdc7fb5f69fa`
- `Debate-Judge`
  - workspace_id: `3c7b944da9d6`
  - **本轮明确禁止推送 Debate-Judge。**
  - fresh `workspace status` 返回 `fatal: not a git repository`，因此不要把“不是 Git 仓库”误解成允许对其做任何发布动作；禁推是用户级硬门禁。

执行总顺序仍然是：

`fresh self-check → v0.6 feasibility → Gate B → production implementation/TDD/review → 用户 install/rescan/exact-hash approve → Ottawa 全链 runtime acceptance → GH 已有外源插件选择性同步 → DUG B09D`

在 PDF runtime 真正 PASS 之前，不进入 GH 与 DUG B09D 实质审计。

---

## 1. fresh FolderBridge 现场（本次收口前）

`server_info`：

- FolderBridge version = `0.8.21`
- mode = read/write
- extension `pdf-toolkit`：
  - version = `0.5.1`
  - external / bundled=false
  - trusted=true
  - enabled=true
  - loaded=true
  - approval_stale=false
  - installed exact-tree SHA-256 = `41ad29d62fcd853a10548d52a2b13b8f774e50d4925038954a8369c558568b1e`
  - permissions:
    - `workspace.read`
    - `workspace.write`
    - `process.execute:powershell.exe`

fresh `pdf-toolkit status` 仍为 **FAIL**：

- `ready=false`
- backend = `pypdf + Windows.Data.Pdf`
- text_backend = `pypdf`
- pinned_pypdf_version = `6.16.2`
- loaded_pypdf_version = `null`
- vendor_dir_present = `true`
- pdf_render_script_present = `true`
- page_render_png = `true`
- metadata = `false`
- outline = `false`
- text_layer = `false`
- literal_search = `false`
- vendor_provenance = `null`
- error.text_backend = `ExtensionError: Could not import the approved vendored pypdf backend.`

因此 **0.5.1 仍不能宣称 runtime PASS**。

---

## 2. folderbridge-mcp fresh Git 现场

branch/status：

`## main...origin/main`

tracked drift：

- `M Plugins/extensions/README.md`
- `M folderbridge_mcp/gui.py`
- `M tests/test_gui_041_regressions.py`

untracked 主要包括：

- 整个 `Plugins/extensions/pdf-toolkit/` source tree
- PDF Toolkit docs/tests
- `video-storyboard-production` Skill Pack
- `scripts/install_mattpocock_full_skill_pack.ps1`
- 旧交接文件

tracked diff 中：

- `Plugins/extensions/README.md` 增加 PDF Toolkit 0.5.1 表项；
- `gui.py` / `test_gui_041_regressions.py` 是 managed service controls UI 并行改动；
- 不能 `git add -A`，必须逐项归属、选择性提交。

**禁止为了 PDF Toolkit 或 GH 数字变绿顺手修改并行 core/UI 漂移。**

---

## 3. v0.6 架构状态：Gate A 已正式 PASS

此前对 v0.6 进行了多轮 from-zero / failure-matrix 设计审计。

最终 Gate A：

- D25 = `0 new material findings`
- D26 = `0 new material findings`
- **Architecture Design Convergence = PASS**

Gate A 只授权：

- temporary candidate fetch
- feasibility probe
- `local-private/` 下的临时验证资产

Gate A **没有授权直接修改 production v0.6 manifest/plugin/install/runtime**。

Production PDF Toolkit source 仍处于 0.5.1 体系；`Plugins/extensions/pdf-toolkit/folderbridge-extension.json` fresh SHA-256：

`7dfcad09daee1ec2d2b72879ed2ad14bb9313433d2916c1384b1853d449eba80`

---

## 4. v0.6 feasibility probe 当前现场

临时 probe：

`local-private/pdf-toolkit-v06-feasibility/probe.py`

fresh SHA-256：

`c0b01f645c02062e649765a0ba6e92ad395a0f9d28dba2f9914c7b7d382b57ed`

它位于 ignored/local-private 区域，不属于 production Extension tree。

临时测试桥已经全部恢复；fresh：

`tests/test_allow_tasks_regression.py`

SHA-256：

`4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`

不要继承任何旧临时桥；新会话如要再触发 probe，必须 fresh 检查并按同样纪律“临时挂载 → 执行 → 等 workspace lease 释放 → 精确恢复原 SHA”。

### 4.1 已取得的真实 feasibility 证据

已经真实完成过：

1. NuGet V3 official Catalog integrity 路线核验：
   - registration 内嵌 `catalogEntry` 可能没有 `packageHash`；
   - 正确路线是先按 registration 中的 version 筛选唯一目标 leaf，再解引用 official Catalog item；
   - official Catalog item 的 `packageHash` / `packageHashAlgorithm` 用于 package integrity；nuget.org 为 SHA512；
   - 禁止 TOFU。
2. 6 个候选包曾在同一完整运行中越过 official SHA512 对撞并进入 Unicode 阶段：
   - PdfPig 0.1.16
   - Microsoft.Bcl.HashCode 6.0.0
   - System.Memory 4.6.3
   - System.Buffers 4.6.1
   - System.Numerics.Vectors 4.6.1
   - System.Runtime.CompilerServices.Unsafe 6.1.2
3. 6 个 `.nupkg` 当前均已落在：
   - `local-private/pdf-toolkit-v06-feasibility/downloads/`
4. Unicode baseline probe 曾完整通过并到达 `loader_start`。
5. PowerShell 5.1 generated loader parser 曾发现 `$simple:$loc` interpolation 错误，已改成 `${simple}:...`；同类 `$simple:` hazard 已搜索清空。
6. 网络 acquisition 与 runtime feasibility 已被拆开；新增 cached/local runtime probe，避免每次 loader 重跑依赖 NuGet/Unicode 网络。
7. cached runtime probe 已能从本地 nupkg 离线、安全重建 vendor DLL 集合，而不是继承可能被失败运行覆盖的半成品 `vendor/`。

### 4.2 已确认不是架构反证的 probe bug / 环境故障

历史红灯：

- 把 registration 内嵌 entry 当完整 Catalog item → probe bug，已修；
- 遍历所有历史版本并逐个 Catalog fetch → probe bug，导致 300s timeout，已修为先筛 version；
- PowerShell `$simple:$loc` parser error → probe bug，已修；
- 一轮 `URLError: [Errno 2] No such file or directory` → 与前后 NuGet 成功运行不一致，归类为 environmental/network failure；
- `run_probe()` 重建 vendor 导致失败运行留下半成品 → probe cache lifecycle bug，已改 cached runtime 从 6 个 nupkg 离线重建。

不要把上述历史红灯写成“PdfPig 不可行”。

---

## 5. 当前唯一精确断点：PowerShell `Inside()` 路径分隔符 bug

latest `progress.json`：

- stage = `cached_runtime_failed`
- error 表面为：
  - loadfrom → `dll-outside-vendor:...\vendor\System.Runtime.CompilerServices.Unsafe\System.Runtime.CompilerServices.Unsafe.dll`
  - byte → 同样 `dll-outside-vendor`

但该 DLL 明明位于 vendor subtree，因此还没有执行到真正的 CLR Assembly load。

根因已定位在 `write_loader_script()` 生成的 PowerShell `Inside()`：

```powershell
function Inside([string]$child, [string]$root) {
    $c = [IO.Path]::GetFullPath($child).TrimEnd('\\')
    $r = [IO.Path]::GetFullPath($root).TrimEnd('\\') + '\\'
    return $c.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)
}
```

关键：PowerShell **单引号字符串不把反斜杠当转义字符**。probe 按 Python/regex 习惯写了过量 `\`，导致 `$r` 实际追加多个反斜杠，正常 child 路径不会 StartsWith 该 root。

### 新会话的第一个唯一实现动作

只修改：

`local-private/pdf-toolkit-v06-feasibility/probe.py`

把 generated PowerShell `Inside()` 改为 PowerShell 原生的**单个反斜杠路径分隔符语义**；同时确认 `TrimEnd` 也只表达一个 `\` char，而不是多个 literal backslashes。

在修复前先 fresh read 对应片段和 file SHA；不要凭本交接直接盲改。

修完后：

1. 搜索同类 generated PowerShell backslash-overescape；
2. 临时把 cached-runtime probe 挂到最早测试模块；
3. 跑 `test` capability；
4. 观察 `progress.json`；
5. Job 完成并 lease 释放后恢复临时测试桥原 SHA。

**只有越过 `Inside()` 并真正执行 `Assembly.LoadFrom` / `Assembly.Load(bytes)` 后，后续失败才开始构成 CLR/PdfPig loader feasibility 证据。**

---

## 6. v0.6 feasibility 尚待真实验证的核心项

下轮必须继续得到真实证据：

- PowerShell 5.1 / .NET Framework >= 4.7.1
- exact DLL hash precheck
- assembly identity
- `LoadFrom` vs byte-load strategy
- global/GAC/outside-vendor collision fail-closed
- tampered DLL fail-closed
- `UglyToad.PdfPig.PdfDocument`
- `ContentOrderTextExtractor.GetText`
- page count / PDF version
- metadata
- bookmarks
- **MediaBox bounds**（不是 CropBox，也不把 `Page.Width/Height` 当 MediaBox）
- encrypted PDF：无密码必须失败；probe 专用密码可开，并报告 IsEncrypted
- UTF-8 / NUL / supplementary Unicode stdin JSON roundtrip
- stdout 8 MiB / stderr 256 KiB bounded concurrent drain
- timeout / cancel / owned process tree
- Extension projected tree <= 256 files / 64 MiB

另外历史设计已冻结：

- pypdf → PdfPig **不承诺正文逐字节等价**；
- search query 只用 `strip()` 判断是否纯空白，真正 literal search 使用原始 query，不自动 trim；
- caller data 走 stdin JSON，不走 argv；
- read/search 异常保持 whole-call fail-closed；
- 最终重要文本仍需 render + `image_open` 视觉核验。

---

## 7. Gate B 与 production 修改门禁

只有 feasibility probe 真正 PASS 后：

1. 把 exact package hashes / DLL set / TFM / license evidence / Unicode baseline / loader strategy 写进 Final Locked v0.6 Spec；
2. 再做 Gate B 独立 clean review；
3. 必须连续两轮 `0 new material findings`；
4. 才允许 production TDD：
   - red regression
   - 最小实现
   - PDF 专项测试
   - 全仓测试
   - implementation review
   - 连续两轮 clean
5. 才允许用户重新安装 / rescan / exact-hash approve。

不要跳过 Gate B 直接改 production。

全仓已有一个**明确无关的既有失败**：

`test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body`

原因：runtime instructions `6001 > 5000`。

这是 FolderBridge core 并行问题；**不要为了 PDF Toolkit 数字变绿顺手修它。**

---

## 8. PDF runtime 真正 PASS 的验收定义

用户重新 install / rescan / exact-hash approve 后，必须 fresh：

`status → Ottawa info → search → read-pages → render-pages → image_open`

任何一步失败都不能称 runtime PASS。

Ottawa 测试文件（DUG workspace）：

`Upload/分析资料原始文件/Ottawa WUDC Debating & Judging Manual - Final Version.pdf`

已知 baseline：

- bytes = `1,447,609`
- SHA-256 = `929389446cbf07637dc0df0629c6446ed6e900ade17dec02e4a278121e624a3e`

至少 search：

- counter-proposition
- definition
- model
- burden
- ordinary intelligent voter

然后精确 read 命中页及必要上下页。

render 至少：

- 封面
- version/copyright page
- contents
- definition/model 条款
- counter-proposition
- burdens
- adjudication / judging 关键页

必须 `image_open` 目视核对，不能仅凭 extracted text 宣称视觉真值。

若可取得 WUDC 官网原 PDF bytes，再做 SHA-256 byte-for-byte 对撞；没有对撞前只能称 `high-confidence official Ottawa 2027 candidate`。

---

## 9. GH 同步门禁

PDF runtime PASS 后才处理用户原始要求：

“GH有但是本地更新的外源插件都更新一下，Judge不要推送。”

要求：

- fresh 检查 folderbridge-mcp origin / branch / status / diff / remote；
- 只同步 GH 已存在且本地确有更新的 external plugins / skill assets；
- 不 `git add -A`；
- 并行 GUI / video-storyboard-production / PDF Toolkit / MATT installer 等逐项归属；
- 不 force push；
- **Debate-Judge 禁止推送**；
- `debate-judge-adapter` 位于 folderbridge-mcp 并不授权推 Debate-Judge 项目；
- PDF Toolkit 若此前 GH 不存在，先按 repo 正式发布结构判断是否应作为“本次新增公开插件”，不能机械套用“GH 有但本地更新”。

---

## 10. DUG B09D 控制面仍未变

fresh SHA：

`《辩论筑基知识体系》.md`

`2960c2beaadacb0827fac0810b6cd926d37a3751bb3d8decc51bcf7641b77e2e`

`03-提炼笔记/下一批提示词-B09D-跨LLM-20260903.md`

`a7018b0dfaa219d92f01b9358d9b08d80c85c81bb1b2d31b36eb2d19ba26fc58`

进入 DUG 前仍需 fresh 读取：

1. `03-提炼笔记/全量原始资料审计-跨LLM自举协议-20260822.md`
2. `03-提炼笔记/全量原始资料审计-跨LLM批次文件总表-20260822.md`
3. `03-提炼笔记/全量原始资料审计-跨LLM状态-20260823.json`
4. `03-提炼笔记/下一批提示词-B09D-跨LLM-20260903.md`
5. `《辩论筑基知识体系》.md`
6. 总账
7. B09C report
8. 第9.1讲 PPTX
9. 第9.1讲 SRT
10. Ottawa WUDC 2027 本地 PDF

应确认：

- latest_completed_batch = B09C
- next_batch = B09D
- B09C double-smoke formally closed
- independent issues = 113
- S2 = 54
- S3 = 59
- B09 = 3
- next issue = B09-004
- mutation_authorized = false

B09D 唯一起点：

- 第9.1讲 PPTX P1
- SRT cue1
- `00:00:02,480`
- 不跳麦克风测试、版权、前言、讲师自我限权

当前 WUDC/BP 校对在 PDF Toolkit runtime PASS 后优先走：

`official Ottawa local PDF → precise search/read → page provenance → render → visual verification`

真实分歧继续强制 `DISPUTE NOTE｜分歧性概念／制度落位备注`，制度规则与同行评议学术理论必须分栏；Oregon/BP/WUDC/国际学理必须有切实权威出处，禁止模型记忆、虚构页码、虚构规则。

涉及胜负、比分、裁判职责、举证责任、判准或现役裁决时，fresh 使用：

`C:\Claude\Project\Debate-Judge\web\judge.html`

作为 current Judge-first 唯一现役正式权威；根 `Skill-Judge.md` 只导航。

---

## 11. 新会话最短正确执行路径

1. 完整读本交接。
2. fresh `server_info`、folderbridge-mcp status/diff、extension catalog、pdf-toolkit info/status。
3. fresh read `local-private/pdf-toolkit-v06-feasibility/probe.py` 的 `write_loader_script()` / `Inside()` 片段并取 SHA。
4. **只修 local-private probe 的 PowerShell single-backslash containment bug。**
5. 扫 generated PowerShell 同类 overescape。
6. 临时挂最早测试桥，跑 cached-runtime feasibility。
7. 等 Job 完成、workspace lease 释放，精确恢复测试桥。
8. 若真正进入 CLR/PdfPig 后出现新失败，按“probe bug / candidate dependency failure / Gate A architecture counterexample”三分法判断；不乱补丁。
9. feasibility PASS 后锁 Final Spec → Gate B 两轮 clean → production TDD/review。
10. 用户重新安装批准后做 Ottawa 全链 runtime acceptance。
11. PASS 后才 GH 选择性同步；Judge 永不推送。
12. 最后正式推进 DUG B09D。

**当前不要做：** GH push、Debate-Judge push、DUG mutation、production v0.6 修改、宣称 PDF runtime PASS。
