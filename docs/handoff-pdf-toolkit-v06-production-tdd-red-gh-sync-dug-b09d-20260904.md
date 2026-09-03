# PDF Toolkit v0.6 production TDD RED → runtime acceptance → GH sync → DUG B09D｜新会话交接｜2026-09-04

> 本文件是新会话唯一正式交接锚点之一。新会话不得依赖旧会话运行态、旧 Job ID、旧口头结论或未落盘 reasoning；必须先以 FolderBridge 当前 server_info / git status / Extension catalog / 本文件及下列 SHA 重新自检，再继续。

## 1. 工作区与硬门禁

工作区：

1. `folderbridge-mcp`
   - workspace_id: `126f215203a0`
2. `Debate-Universal-Grammar`
   - workspace_id: `cdc7fb5f69fa`
3. `Debate-Judge`
   - workspace_id: `3c7b944da9d6`
   - **本轮明确禁止推送 Debate-Judge。**

当前总阶段：

`PDF Toolkit v0.6 Gate B COMPLETE -> production TDD RED -> GREEN -> user reinstall/reapproval -> Ottawa runtime acceptance -> GH selective sync -> DUG B09D`

当前断点严格是：

**修复后的 v0.6 已完成 filesystem reinstall + exact-hash reapproval；live exact tree SHA-256 为 `412307ff7b8776e60d8515040ca3e8086ac9b1298764d73a49ae8d1f69c4d80f`，trusted / enabled / loaded / approval current。Ottawa 官方 PDF 已完成 `status -> info -> search -> read-pages -> render-pages -> image_open` 全链 runtime acceptance，并正式 PASS。当前唯一正式下一步是 folderbridge-mcp GH selective sync；GH sync 完成后才进入 DUG B09D。**

因此新会话启动时：

- production code 已针对本次 live counterexample完成 gate-preserving 修订并重新收敛；
- 修复后 live exact tree 已完成 reapproval / enable；
- Ottawa runtime acceptance 已完整通过，**PDF runtime PASS 已可正式签署**；
- GH selective sync gate 已打开；
- **不得**在 GH sync 完成前进入 DUG B09D；
- **不得**为 PDF 工作修 FolderBridge core 的既有无关 `6001 > 5000` failure；
- **不得**推送 Debate-Judge。

## 2. 启动后必须首先 fresh 读取 / 核验

按顺序：

1. `server_info`
2. 本交接全文：
   - `docs/handoff-pdf-toolkit-v06-production-tdd-red-gh-sync-dug-b09d-20260904.md`
3. Final Locked Spec：
   - `docs/pdf-toolkit-external-extension-design-20260903.md`
   - 当前 SHA-256：`a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6`
4. MATT redesign/audit：
   - `docs/pdf-toolkit-matt-redesign-and-audit-20260903.md`
   - 当前 SHA-256：`6fbb7e79411382e9e87a52e7a32b1dfba338a4faa159a24dfb859b2f2d092294`
5. v0.6 production tests：
   - `tests/test_pdf_toolkit_v06.py`
   - 当前 SHA-256：`806a8d2fa6c3784d111aad17af257de970cab4ff74d3bf1d6851195b9d475a51`
6. authoritative fresh feasibility result：
   - `local-private/pdf-toolkit-v06-feasibility/result.json`
   - SHA-256：`53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1`
7. fresh `folderbridge-mcp git status/diff`
8. fresh Extension catalog + `pdf-toolkit info/status`

启动时必须再次确认 installed Extension 仍是现场实际值，而不是沿用本交接口头值。

本交接写入前 fresh 现场：

- FolderBridge `0.8.21`
- installed `pdf-toolkit 0.6.0`
- installed exact tree SHA-256：`412307ff7b8776e60d8515040ca3e8086ac9b1298764d73a49ae8d1f69c4d80f`
- trusted / enabled / loaded / approval not stale
- `status.ready=true` / `inspection_ready=true` / loaded PdfPig `0.1.16` / Unicode casefold `14.0.0`
- source `pdf_inspect.ps1` SHA-256：`a33ce880075f5f9b6331b8d517dd9284585604dc091cab4922c303c1271c407d`。
- Ottawa runtime acceptance = **PASS**。

## 3. Gate B 已正式收敛，不得重新误判为待 feasibility

v0.6 feasibility 已由完整 fresh probe 闭合；authoritative result：

`local-private/pdf-toolkit-v06-feasibility/result.json`

SHA-256：

`53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1`

其 `overall_pass=true`，且同一次 fresh run 已覆盖最终 Gate-A closure，包括：

- official NuGet reacquisition / integrity；
- exact package / TFM / DLL / assembly identity；
- Windows PowerShell 5.1 + .NET Framework >= 4.7.1；
- `Assembly.LoadFrom`；
- exact strong-name redirects；
- collision / tamper / missing-vendor fail-closed；
- PdfPig page count / ContentOrderTextExtractor / MediaBox / metadata / bookmarks；
- encrypted no-password fail-closed；
- Unicode 14.0.0 deterministic full casefold；
- culture-invariant `PDF-1.7`；
- strict UTF-8 / NUL / supplementary-plane protocol；
- stdout/stderr caps、timeout、active cancel、pre-start cancel；
- read/search bounded semantics；
- 501-page search rejection before parser use；
- parser text-cap coverage flags；
- tree-budget projection。

Gate B 后续 MATT audit经历多轮 material findings，最终在同一 Final Locked Spec SHA：

`a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6`

上取得：

- **D48 CLEAN #1**：fresh feasibility evidence ↔ Final Locked Spec equality matrix；
- **D49 CLEAN #2**：external Extension security / permission / runtime-boundary review。

因此：

**Gate B = PASS / CONVERGED；production TDD 可以继续。**

不要重新把当前阶段误置为“feasibility 未通过”或“production 禁止”。

## 4. Gate B 最终冻结的关键 production contract

### 4.1 Public Extension / manifest

Public actions必须保持 exactly：

`status / info / outline / read-pages / search / render-pages`

Runtime permissions exactly：

- `workspace.read`
- `workspace.write`
- `process.execute:powershell.exe`

无 runtime network permission、URL、generic shell、dotnet.exe、自定义 EXE、OCR、PDF mutation。

Manifest public input schema、defaults、limits、authorization、requires_workspace、read_only、600s inspection ceiling、7200s render Job、`output_dir` tree mutation scope均已在 Final Locked Spec逐字段冻结；不要随 parser migration漂移。

### 4.2 Runtime architecture

1. `plugin.py`
   - Python policy/orchestration only；
   - **不得 import任何第三方 PDF parser**；
   - 保留 workspace confinement、link/reparse denial、source SHA/stat fence、public bounds、response validation、renderer artifact validation、host-owned cancel/process ownership。
2. `pdf_inspect.ps1`
   - 新 fixed approved script；
   - Windows PowerShell 5.1；
   - fixed argv，不接受 caller script/assembly/executable/command text；
   - request只从 strict BOM-less UTF-8 stdin JSON进入；
   - production script source对 semantic non-ASCII literal采用 **ASCII-only source** + numeric Unicode code point构造；不得自行改选另一 script encoding。
3. `pdf_render.ps1`
   - 保持独立 Windows.Data.Pdf seam；
   - render path **不得启动 PdfPig / pdf_inspect.ps1**；
   - renderer自身取得 PageCount / selected range / geometry / pixel preflight。

### 4.3 Exact PdfPig package / assembly lock

Packages：

- PdfPig `0.1.16`
- Microsoft.Bcl.HashCode `6.0.0`
- System.Memory `4.6.3`
- System.Buffers `4.6.1`
- System.Numerics.Vectors `4.6.1`
- System.Runtime.CompilerServices.Unsafe `6.1.2`

PdfPig selected TFM = `net471`。

`PdfPig net471` actual dependency group只包含：

- `Microsoft.Bcl.HashCode 6.0.0`
- `System.Memory 4.6.0`

`System.ValueTuple 4.5.0`只属于 net462 group，**不得进入 v0.6 runtime lock**。

Production DLL load order exact：

`System.Runtime.CompilerServices.Unsafe.dll -> System.Buffers.dll -> System.Numerics.Vectors.dll -> System.Memory.dll -> Microsoft.Bcl.HashCode.dll -> UglyToad.PdfPig.Core.dll -> UglyToad.PdfPig.DocumentLayoutAnalysis.dll -> UglyToad.PdfPig.Fonts.dll -> UglyToad.PdfPig.Package.dll -> UglyToad.PdfPig.Tokenization.dll -> UglyToad.PdfPig.Tokens.dll -> UglyToad.PdfPig.dll`

Production loader = **`Assembly.LoadFrom` only** after exact path/hash/identity precheck。

Production **必须**注册一个 bounded `AssemblyResolve` handler，且只能允许两个 exact FullName redirects：

1. `System.Memory, Version=4.0.2.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`
   -> `System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`
2. `System.Buffers, Version=4.0.4.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`
   -> `System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`

没有 simple-name wildcard / version range / GAC/global fallback / byte-load fallback。

### 4.4 Unicode lock

- Python/v0.5.1 semantic baseline = Unicode `14.0.0`
- `CaseFolding.txt` SHA-256：`a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f`
- generated 1530-entry full-fold map SHA-256：`77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504`
- Unicode license SHA-256：`e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96`
- `status.casefold_unicode_version = "14.0.0"`
- `status.policy.parser_memory_sandbox = false` exact boolean key。

### 4.5 Process / protocol

Inspection child：

- internal business timeout 570s；outer host ceiling 600s；
- stdout cap 8 MiB；stderr cap 256 KiB；
- concurrent draining while child lives；
- stdin request cap 64 KiB；
- pre-start cancel检查发生在 spawn之前；
- active cancel / timeout终止 owned process tree；
- controlled completion：exactly one stdout protocol-v1 JSON object、stderr empty、exit code 0；
- controlled domain/backend error也在 JSON envelope中表达；
- crash/nonzero/invalid UTF-8/extra stdout/malformed envelope等 -> `PDF_INSPECT_PROTOCOL_ERROR`。

### 4.6 Public response / error compatibility

`info / outline / read-pages / search` public response keys/nesting默认保持 v0.5.1 contract；允许的 v0.6 parser-derived text差异不等于允许字段漂移。

`status`是明确 backend-diagnostic delta；使用 PdfPig readiness字段，不再保留 pypdf-specific pin/patch字段。

`render-pages`保持 artifact-oriented shape，但：

- `page_count`改为 renderer-owned，且等于新 `source_units`；
- 新 `selected_range`；
- 新 `inspection_backend_invoked=false`；
- `text_backend="PdfPig 0.1.16"`仅为 declarative configured identity；
- `RENDER-COMPLETE.json`升级 schema_version 3；
- 不再通过 PdfPig-vs-Windows page-count对比触发 `PDF_RENDER_SOURCE_MISMATCH`；该 code保留为 compatibility-reserved，不在 v0.6 render path用此触发条件。

既有 validation/workspace/path/source错误 taxonomy保持；inspection backend/protocol新增 code 已在 Final Locked Spec冻结。

### 4.7 Installer transaction

v0.6 `install.ps1` 是 create-then-publish transaction，不允许直接覆盖 live hot-scan tree。

必须实现：

- destination-scoped exclusive OS file-handle lock (`FileShare.None`)；
- persistent outside-hot-scan same-volume transaction journal；
- phases：`prepared / old_backed_up / new_published / aborted / committed`；
- fresh install / replacement 两分支；
- staging + backup + failed-new quarantine 均 outside hot-scan且 same volume；
- live mutation只允许 whole-tree namespace rename/move；
- rollback时 failed new live tree先 whole-tree quarantine，再 restore old backup；
- 不允许 recursive live delete作为 rollback；
- crash residue / terminal residue必须按 journal + topology fail-closed；
- normal handled failure不能留下会永久 poison下一次安装的假 recovery-required状态；
- success filesystem publish不等于 trust approval；必须 rescan / exact tree hash review / reapprove / enable；旧 v0.5 approval不得沿用。

## 5. 当前 production TDD GREEN 现场

v0.6 production tests：

`tests/test_pdf_toolkit_v06.py`

当前 SHA-256：

`806a8d2fa6c3784d111aad17af257de970cab4ff74d3bf1d6851195b9d475a51`

P1–P5 已按 locked spec 逐层实现并经真实测试收敛：

- **P1 inspection seam GREEN**：manifest 0.6.0；fixed ASCII-only `pdf_inspect.ps1`；12 DLL exact `Assembly.LoadFrom` + exact redirects；Python 无第三方 PDF parser；bounded/owned inspector supervisor。
- **P2 public inspection GREEN**：`info / outline / read-pages / search` 全部仅经 fixed PdfPig PowerShell seam；ContentOrderTextExtractor / MediaBox / Unicode-scalar offsets / deterministic Unicode 14.0.0 casefold 均由 production inspector 执行并由 Python 二次 validate。
- **P3 render decoupling GREEN**：`render-pages` 不启动 PdfPig/inspector；Windows.Data.Pdf own source page count/range/geometry/nominal pixel preflight；completion marker schema v3；`inspection_backend_invoked=false`。
- **P4 installer/provenance GREEN**：exact six NuGet locks + 12 DLL hashes/identities + Unicode/license locks + schema-v3 provenance + 256 files/64 MiB tree gate + destination-scoped `FileShare.None` lock + persistent journal + same-volume outside-hot-scan staging/backup/quarantine + whole-tree publish/rollback。
- **P5 source hygiene GREEN**：公开 extension table 已锁 `pdf-toolkit 0.6.0`；`requirements-vendor.txt` 明确 0.6 无 Python vendor requirements；README/NOTICE 已迁移到 PdfPig/NuGet/Unicode/transaction installer 正式口径；README 正式部署入口为 `install.ps1`，bootstrap 仅是 research convenience。

真实 offline temporary-directory installer acceptance（使用 authoritative fresh feasibility cache，不访问网络）已 PASS：

1. fresh publish；
2. existing live + no `-Force` => fail closed 且 live 不变；
3. `-Force` whole-tree replacement；
4. nonterminal recovery journal => `INSTALL_RECOVERY_REQUIRED`，且 live 不变；
5. concurrent destination lock => `INSTALL_BUSY`，且 live 不变。

该 acceptance 实际抓到并修了两个 production bug，而不是放宽测试：

- clean FolderBridge environment 可能没有 `$env:OS`，旧 Windows 判定会在真实 Windows PowerShell 5.1误拒绝；现改为 CLR `[Environment]::OSVersion.Platform == Win32NT`；
- .NET Framework `File.Replace(temp,path,$null,$true)` 不接受 null backup path；journal atomic replace 现使用同目录唯一 backup path，成功后删除，失败则保留 recovery bytes，不退化为 delete+move。

当前 `install.ps1` SHA-256：

`fd560492ed0e94468486ca91225bd186dc0dd6940eb15767ff9075008411adbf`

首次 production GREEN 后，live Ottawa acceptance 发现：`status` GREEN，但 `info / outline / read-pages` 均因 inspector unknown crash 返回 `PDF_INSPECT_PROTOCOL_ERROR`；独立 `render-pages` 对同一 Ottawa PDF PASS，确认文件/路径/renderer 正常。进一步用 599-byte ASCII 最小 PDF 复现同一 inspector failure，排除 Ottawa/中文路径特异性。

TDD 临时完整安装树诊断锁定真实根因：Windows PowerShell 5.1 对泛型集合直接 `@($genericList)` 输出存在 `System.ArgumentException: Argument types do not match`；最初 stack 位于 `Get-OutlineData` 的 items array materialization。production 现把所有直接泛型 List JSON-array出口改为 `.ToArray()`，并新增永久实装回归：安装完整临时 v0.6 tree 后依次真实执行 `info / outline / read-pages / search`，包括 Unicode 14.0.0 casefold map 实际加载。

最后 fresh全仓 test：

- `Ran 427 tests`
- `FAILED (failures=1, skipped=2)`
- **所有 PDF Toolkit tests = GREEN，包括 offline installer acceptance 与新 file-backed 四动作真实安装树回归**；
- 唯一 failure仍是既有无关 FolderBridge core：`test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body`，`6001 > 5000`；
- 2 skips 为既有平台相关 skips；
- 没有 PDF 新回归。

因此 production TDD gate = **PASS**。不要重新把阶段退回 RED，也不要为追求“全仓 0 failure”去修改无关 FolderBridge core。

### 5.1 修复后 Ottawa live runtime acceptance = PASS

修复后 live tree：

- version `0.6.0`
- exact tree SHA-256 `412307ff7b8776e60d8515040ca3e8086ac9b1298764d73a49ae8d1f69c4d80f`
- trusted / enabled / loaded / approval current
- `status.ready=true`
- `inspection_ready=true`
- loaded PdfPig `0.1.16`
- renderer `Windows.Data.Pdf`

Ottawa source identity：

- bytes `1,447,609`
- SHA-256 `929389446cbf07637dc0df0629c6446ed6e900ade17dec02e4a278121e624a3e`
- page count `71`
- metadata title `Ottawa WUDC Debating & Judging Manual - Final Version`
- format `PDF-1.4`
- sampled text layer complete；`scan_candidate=false`

全文 literal search：

- `counter-proposition`：14 hits / 71 pages complete
- `definition`：87 hits / 71 pages complete（以 `max_results=200` 重跑闭合）
- `model`：47 hits / 71 pages complete
- `burden`：31 hits / 71 pages complete
- `ordinary intelligent voter`：26 hits / 71 pages complete

所有以上 search 均最终 `search_window_complete=true / text_coverage_complete=true / coverage_complete=true`。

连续 `read-pages` acceptance：

- P19–21：OIV / winning-a-debate，完整，无截断
- P28–31：burdens / frameworks / policy motions / counter-proposition，完整，无截断
- P38–46：definitions / models / squirrelling / definition challenge / counter-propping，完整，无截断

三个独立 renderer jobs 均 PASS，且 `inspection_backend_invoked=false`：

- P1–5：front/version/TOC
- P19–31：OIV / burdens / motion types
- P38–46：definitions / counter-propping

`image_open` 视觉核验已实际打开并确认：P1 cover、P2 authorship、P4 TOC、P19 OIV、P28 Burdens、P38 Definitions and Models、P44 Counter-propping。渲染版面、页码、章节标题和抽取正文一致，无错页/乱码/明显布局异常。

因此：**PDF Toolkit v0.6 Ottawa runtime acceptance = PASS；GH selective sync gate OPEN。**

## 6. 当前唯一允许推进的下一阶段

production counterexample、reinstall/reapproval、Ottawa runtime acceptance 均已完成并 PASS。当前顺序严格变为：

1. fresh `folderbridge-mcp git status/diff`；
2. 仅选择 PDF Toolkit 正式范围与必要 docs/tests；
3. selective commit，禁止 `git add -A`；
4. push 当前 `main`，no force；
5. post-push fresh status / remote sync verification；
6. GH sync 完成后才进入 DUG B09D。

`gui.py`、GUI regression、video storyboard Skill Pack、MATT installer等并发漂移继续排除；Debate-Judge继续禁止推送。

## 7. 用户 reinstall / runtime acceptance 门禁

production测试现已 GREEN，可以向用户提供 exact install/reinstall指令：

优先使用本轮已通过 offline acceptance 的 authoritative reviewed cache：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Claude\Project\folderbridge-mcp\Plugins\extensions\pdf-toolkit\install.ps1" -Force -ReviewedCacheRoot "C:\Claude\Project\folderbridge-mcp\local-private\pdf-toolkit-v06-feasibility"
```

该命令只完成 filesystem publish；缓存不会绕过校验，installer仍会重新验证所有 frozen SHA/TFM/metadata/DLL identity。**不会也不应继承 v0.5.x trust**。执行后用户必须打开 FolderBridge `Extensions & Skills`：rescan -> 核对 `PDF Toolkit 0.6.0` 新 exact tree hash + permissions（必须不同于修复前 `7f98e8ce55aa2c1f86bc7f28cc7bbe257971548d9efb28e077321d1ba0928442`）-> approve -> enable，然后再通知继续。

用户完成安装/reapproval并通知后，必须 fresh：

1. Extension catalog / info
2. 确认 installed version `0.6.0`
3. exact-tree approval current / not stale / enabled / loaded
4. `status`

真实 runtime PASS仍必须跑 Ottawa chain：

`status -> info -> search -> read-pages -> render-pages -> image_open`

Ottawa PDF：

`C:\Claude\Project\Debate-Universal-Grammar\Upload\分析资料原始文件\Ottawa WUDC Debating & Judging Manual - Final Version.pdf`

known local identity：

- bytes `1,447,609`
- SHA-256 `929389446cbf07637dc0df0629c6446ed6e900ade17dec02e4a278121e624a3e`

search至少：

- `counter-proposition`
- `definition`
- `model`
- `burden`
- `ordinary intelligent voter`

随后 read matched sections；render cover/version/TOC与关键 rule pages；必须经 `image_open`视觉核对。

只有完整 runtime acceptance通过，才可称 **PDF runtime PASS**。

## 8. GH sync 门禁

Ottawa runtime PASS 已完成，因此 **GH selective sync gate = OPEN**。

纪律：

- fresh status/diff；
- selective staging/commit；
- **禁止 `git add -A`**；
- no force push；
- 不混入 GUI / video skill pack /其它并发漂移；
- Debate-Judge **禁止推送**；
- folderbridge-mcp只提交本 PDF Toolkit正式范围与必要 docs/tests；
- 需要 release动作时按当前 Git Publisher / release纪律执行，不得靠旧 Job ID。

## 9. DUG B09D 门禁

只有 GH sync完成后才进入 DUG B09D。

DUG mother expected SHA：

`2960c2be...`

B09D prompt expected SHA：

`a7018b0d...`

进入时必须 fresh核验完整 SHA，不得只靠前缀。

B09D起点严格：

- PPTX P1
- SRT cue 1
- `00:00:02,480`

必须包含：

- mic test
- copyright
- preface
- lecturer self-limitation

不得跳过开场原始材料。

## 10. 当前 git 并发漂移提醒

本交接本轮更新前 fresh status显示除 PDF Toolkit外还存在：

- `folderbridge_mcp/gui.py` modified（无关并发漂移）
- `tests/test_gui_041_regressions.py` modified（无关并发漂移）
- `Plugins/extensions/README.md` modified，其中 **仅 `pdf-toolkit 0.6.0` published-table 行属于本 PDF 工作**；其它并行内容不得吸收
- video-storyboard-production Skill Pack相关未跟踪文件
- Matt skill installer等文件

这些不是本次 production TDD可以随意吸收的范围。

PDF Toolkit当前正式/相关 untracked包含：

- `Plugins/extensions/pdf-toolkit/*`
- `docs/pdf-toolkit-external-extension-design-20260903.md`
- `docs/pdf-toolkit-matt-redesign-and-audit-20260903.md`
- 本交接及旧 handoff
- `tests/test_pdf_toolkit_extension.py`
- `tests/test_pdf_toolkit_v04.py`
- `tests/test_pdf_toolkit_v05.py`
- `tests/test_pdf_toolkit_v06.py`

新会话必须继续 selective，不能用“清理工作树”为理由碰并发成果。

## 11. 新会话第一动作

fresh自检后优先确认 GH selective sync 是否已经完成：

- 若 PDF Toolkit 正式范围仍未 commit/push：继续 §8 selective sync，不重跑已 PASS 的 production TDD/Ottawa runtime，除非磁盘现场显示 PDF source 又发生变化；
- 若已 commit 但未 push：验证 commit exact scope 后 push 当前分支；
- 若 GH 已同步并且 post-push clean/sync verification PASS：进入 DUG B09D，先 fresh 核验 mother 与 B09D prompt 的完整 SHA，再从 PPTX P1 / SRT cue 1 / `00:00:02,480` 开始。

只有新的 runtime/source counterexample才重新打开相应 P 层；不得为了追求形式全绿修改无关 FolderBridge core，也不得推送 Debate-Judge。
