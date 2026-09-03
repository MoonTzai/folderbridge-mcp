# PDF Toolkit｜MATT 重设计与迭代审计｜2026-09-03

## 0. 审计目标与冻结规则

用户要求：先结合 MATT Skill 重新设计，确认与当前 v0.1.0 草案的差异；在**不执行实现修改**的前提下对设计迭代审计，直到连续两轮没有新增实质问题；只有设计门通过后才允许执行实现。

本轮采用的 FolderBridge Engineering Methods（当前磁盘真源）：

- `codebase-design`：深模块、小接口、显式 seam；用 deletion test 判断是否真的需要拆模块。
- `improve-codebase-architecture`：只找高杠杆 deepening opportunity；区分已证实缺陷和架构摩擦；禁止因为文件大就机械拆分。
- `tdd`：实现阶段按稳定公共 seam 做 vertical red → green → refactor。
- `code-review`：标准轴与 spec 轴独立审计，输出有证据、后果、修复的最小实质 finding 集。
- `implement`：只有设计 settled 后执行；小步实现、专项测试、全回归、最终 review。

同时沿用 FolderBridge ABI v1：外源扩展 exact-tree-hash approval、workspace confinement、explicit permissions、`mutation_scope`、worker snapshot、host-owned Job。

## 1. 当前 v0.1.0 草案固定点

当前公共动作：

`status / info / outline / read-pages / search / render-pages`

当前后端：`pypdfium2==4.30.0`，安装时 pip vendoring 到 `_vendor/`。

当前实现文件：`Plugins/extensions/pdf-toolkit/plugin.py`（单一 cohesive runtime 模块）。

当前重要正面设计：

- 没有 arbitrary URL / shell / executable / regex public surface；
- runtime 不申请 network/process permission；
- 只接受 workspace-relative PDF；
- `render-pages` 是 host-owned Job 并声明 `output_dir` tree mutation scope；
- 不提供 whole-document unbounded read；
- 文本层明确标为 untrusted，关键证据要求视觉核验；
- 专项测试已经覆盖基本路径 confinement、范围限制、literal search、PNG encoding、render artifact。

## 2. MATT Round 1｜架构＋spec 独立审计

结论：**未收敛。发现 10 个实质问题／deepening opportunity。**

### R1-01｜P1｜后端 pin 已显著过时

当前固定 `pypdfium2==4.30.0`（2024）不是 2026-09 当前稳定线。PyPI current stable 为 `5.13.0`（2026-08-13）。继续以 4.30.0 做新插件基线会主动背负已淘汰 API/bug surface。

处置：设计改为 pin current stable `pypdfium2 5.13.0`，并把 upstream/version/hash 写入可核 vendor lock；不跟随 floating latest。

### R1-02｜P1｜安装供应链不可重复，许可证闭环不够

当前 `pip install -r requirements-vendor.txt` 只锁版本，不锁 wheel SHA；安装结果虽然最终会被 FolderBridge exact-tree-hash approval 固定，但**下载阶段**仍不是可重复的 supply-chain gate。pypdfium2 二进制分发还要求随包保留 PDFium/third-party license payload。

处置：Windows x64 首版安装器固定 PyPI Trusted Publishing 的 `pypdfium2-5.13.0-py3-none-win_amd64.whl`，固定官方文件 SHA-256：

`47dcca2a8d507b5fd24f94c3c9d48fb379430f097bc20f01beff6c963ffbcedb`

安装器必须：下载/接收 wheel → SHA 对撞 → 解包 → 校验 package/version/license payload → 生成 `VENDOR-PROVENANCE.json` → 再 staged cutover。不得把许可证只写成一句“permissive”。

### R1-03｜P1｜render 只有页数/DPI限制，没有像素/磁盘预算

`100 pages × 600 DPI` 可能产生数十亿像素和多 GB 输出。页数上限并不等价于资源上限。

处置：加入：

- per-page pixel hard cap；
- per-call total pixel hard cap；
- post-render total artifact byte hard cap；
- render 前以 page geometry + nominal DPI 做 preflight；
- 超预算 fail-before-write；运行途中超 artifact bytes 则 fail + cleanup 本次新产物。

### R1-04｜P1 security｜直接覆盖输出存在 hardlink/partial-write 风险

当前 `Path.write_bytes()` 与 ZipFile 直接写目标；`overwrite=true` 时若已有 workspace 文件是 hardlink，直接打开会修改 hardlink 指向的内容；崩溃还可能留下 partial artifact。

处置：所有 PNG/ZIP 先写同目录随机临时文件，fsync/close 后 `os.replace()` 发布。对已有文件只替换目录项，不以写模式打开已有目标；失败清理临时文件和本次新建目标。

### R1-05｜P1 contract｜递归创建 output parent 与 mutation_scope 不完全对齐

manifest 只 claim `output_dir` tree，但 `_resolve_output_dir()` 会 `parent.mkdir(parents=True)`，可能创建 `output_dir` 祖先目录，扩大实际 mutation surface。

处置：要求 `output_dir.parent` 已存在且为 regular non-link directory；插件只允许创建 exact `output_dir` 本身。若用户要深层目录，先用 FolderBridge workspace action 建父目录或选择已有父目录。

### R1-06｜P2 correctness｜`read-pages.next_page` 对 partial page 不可真正续读

当前总字符预算在页中截断时返回 `next_page` 指向同一页，但 API 没有 `char_start`，下一调用会从该页开头重复，形成伪 continuation。

处置：分页规则改成：

- 已有完整页放入结果；
- 后续整页放不下时**不返回该页 partial**，`next_page` 指向它；
- 只有请求范围第一张页本身超过 `max_chars` 时允许 partial page，并明确 `partial_page`、`chars_returned`、`page_chars`，`next_page=null`；用户可单页提高预算；
- 单页仍超过 public max 时承认不可一次性完整读，不伪造可续游标。

### R1-07｜P2 correctness｜casefold 后的 char offset 不是原文 offset

`casefold()` 可能扩字符（如 `ß -> ss`），当前 `found` 是 folded string index，却被标成原文 `char_offset`，会产生错误 provenance。

处置：case-insensitive search 建立 folded-index → original-index 映射，所有 `char_offset` 必须回到原始提取文本坐标。

### R1-08｜P2 integrity｜缺少 source-change-during-call 防护

每个 action 会打开 PDF、读内容并独立 hash path；若文件在调用过程中被并发替换/修改，解析内容与返回 SHA 理论上可错位。

处置：引入 `SourceIdentity`：调用前记录 `size / mtime_ns / inode-like stat fields + SHA256`；调用结束再次 stat；若 signature 改变，整次结果失败 `SOURCE_CHANGED_DURING_CALL`。不为了极端 adversarial same-stat mutation 对 512 MiB 文件双 hash；FolderBridge audit 场景以 stable-workspace + stat fence 为边界并明确记录。

### R1-09｜P2 resource｜search/outline/text extraction 仍可能被恶意 PDF 放大

- search 默认可扫到物理 EOF，无 max scanned pages；
- outline 达到 max_items 后仍继续遍历全部 TOC 只为了精确 total；
- `_page_text()` 先提取整页再截总字符预算，单页文本可过大。

处置：

- `MAX_PDF_BYTES`；
- `MAX_SEARCH_PAGES_PER_CALL`；
- TOC 只扫描 `limit+1` 来判 truncated，不追求无界精确 total；
- PDFium textpage 先 `count_chars()`，再按 hard per-page extraction cap 调 `get_text_range(index,count)`；
- `info` text sample 也走 bounded extraction。

### R1-10｜Architecture｜不要因为 25 KiB 单文件而机械拆模块

MATT deletion test 结论：当前只有一个 PDFium adapter，`plugin.py` 内的 public action policy、path policy、PDFium lifecycle 强相关；仅因为文件长拆 `pdf_backend.py/path_policy.py/png.py` 会增加接口事实而不减少调用者知识。

处置：**v0.2 仍保留一个 deep `plugin.py`**，但把第三方 API 访问严格集中在 `_open_document/_page_text/_render_one/_toc_entries` 等少数内部 seam；测试优先跨 public `handle()` 或这些稳定 seam，不做纯文件数量重构。等未来出现第二 backend/OCR adapter 再重新应用 deletion test。

## 3. Round 1 后的 v0.2 设计

### 3.1 Public API 继续保持小而稳定

仍为：

`status / info / outline / read-pages / search / render-pages`

不新增 `run-all`、不新增任意命令、URL、regex、whole-document dump。

### 3.2 Backend / version

- runtime backend：`pypdfium2/PDFium 5.13.0`；
- Windows x64 installer 使用 PyPI official wheel + exact SHA；
- runtime 不联网、不 spawn subprocess；
- wheel license payload 必须原样随 `_vendor/` 分发；
- status 回报 pinned version、loaded version、vendor provenance presence。

### 3.3 Resource budgets

建议初始硬边界：

- PDF input max：512 MiB；
- read-pages：50 pages/call；
- search：最多 2000 pages/call；
- per-page extracted chars hard cap：1,000,000；
- public returned chars max：500,000；
- outline scan：requested limit + 1 only；
- render：100 pages schema ceiling仍保留，但另受 pixel budget；
- per-page render pixels：<= 50,000,000；
- total render pixels：<= 300,000,000；
- total rendered artifact bytes：<= 1 GiB；
- 所有预算值在 `status.policy` 暴露。

### 3.4 Evidence semantics

每个 `info/outline/read/search` 都要返回统一 `content_trust_note`：metadata/bookmarks/text 均属于 PDF document-supplied data；高风险、layout-sensitive、规则原文必须以 render 视觉核验。

`render-pages` 显式回报 `dpi_nominal`，并提示 PDFium 对 PDF 1.6+ `UserUnit` 的物理 DPI 计算存在 upstream 限制；视觉内容仍以实际 raster 输出为证据。

### 3.5 Atomic render transaction

调用内维护 `created_targets` 和临时文件集合：

preflight 全部目标/预算 → 逐页 temp render → atomic replace publish → 可选 ZIP temp → atomic replace publish。

发生错误：清理所有 temp；对本次调用原本不存在而已发布的 target 做 best-effort cleanup。已有文件仅在 `overwrite=true` 时通过 atomic replace 替换，不直接 truncate/write。

### 3.6 TDD execution order（设计门通过后）

1. backend pin/provenance status；
2. source size/stat fence；
3. bounded page text extraction；
4. bounded search + correct original offsets；
5. read-pages honest pagination；
6. bounded TOC；
7. render pixel preflight；
8. atomic PNG/ZIP publish + cleanup；
9. installer exact-wheel/hash/license gate；
10. focused PDF tests → full FolderBridge suite → implementation code-review。

## 4. MATT Round 2｜从修订 spec 重新独立审计

固定点：Round 1 后 v0.2 设计 + ABI v1 + current pypdfium2 5.13.0 文档。按 code-review 的 Standards / Spec 两轴重新检查，不继承“Round 1 已经找完”的假设。

结论：**仍未收敛。新增 4 个实质问题。**

### R2-01｜P1 installer security｜直接解包 wheel 需要 archive member confinement

Round 1 把安装器改成“exact wheel SHA 后直接解包”，但如果只调用通用 `Expand-Archive`，设计本身没有声明 zip member path gate。即使 wheel SHA 来自 PyPI Trusted Publishing，安装器仍应保持 FolderBridge 一贯的 archive traversal discipline。

修订：安装器用 `.NET ZipArchive` 枚举 wheel entry；拒绝绝对路径、drive/UNC、`..`、reparse 不适用的异常 member；只把安全相对 member 写入 staging `_vendor/`。解包前已经完成 wheel SHA 对撞，二者都必须通过。

### R2-02｜P1 transaction semantics｜“每文件 atomic”仍可能留下半套成功输出

Round 1 的 temp + `os.replace()` 可以防 partial file/hardlink overwrite，但若 20 张 PNG 中发布到第 10 张后第 11 张 publish 失败，调用整体失败却已经替换前 10 张，证据目录进入混合版本。

修订：render 分成严格三阶段：

1. **preflight**：source/范围/像素/所有 final target；
2. **stage**：所有 PNG 与可选 ZIP 全部写为 temp，预算与 hash 全完成，期间不触碰 final target；
3. **publish transaction**：对已有 final target 先 `os.replace(final, backup-temp)`，再逐个 `os.replace(staged, final)`；任何 publish 失败，best-effort 删除已发布新目标并从 backup 恢复旧目标；全部成功后才删除 backups。

因此成功响应才意味着整个 range 同一事务发布。

### R2-03｜P1 resource｜50M pixel/page + 非流式 PNG 编码仍过宽

当前 encoder 会同时持有 PDFium bitmap、逐行 payload 和压缩结果；50M RGB 像素可轻易逼近数百 MiB resident memory。页数预算不能替代单页峰值内存预算。

修订：

- public DPI 上限从 600 调整为 400；
- per-page pixel cap 调整为 30,000,000；
- total pixel cap 调整为 200,000,000；
- total artifact bytes 调整为 512 MiB；
- PNG encoder 改为 `zlib.compressobj()` 增量喂行，不再 `b"".join(rows)` 复制整幅 raw raster；
- 仍保留 100 pages schema ceiling，但绝大多数实际调用先由 total-pixel gate 限制。

这仍覆盖常见 A4/Letter 300–400 DPI audit render。

### R2-04｜P2 search evidence｜bounded extraction 必须显式报告 coverage gap

给单页 text extraction 加 hard cap 后，literal search 可能只搜到每页前 N 个字符。如果结果仍简单写 `truncated=false`，用户会误以为全范围无遗漏。

修订：每页 bounded extractor 返回 `page_chars / extracted_chars / text_truncated`。`search` 汇总：

- `text_truncated_pages`；
- `coverage_complete = (text_truncated_pages empty)`；
- `results_truncated` 仅描述结果列表长度，不混同 text coverage；
- search 页窗限制改为 **500 pages/call**，超出直接要求分窗，不偷偷只扫前 500 页。

## 5. Round 2 后 Final Candidate Spec v0.3

在 Round 1 v0.2 基础上冻结以下调整：

- backend：pypdfium2/PDFium `5.13.0`；
- Windows x64 wheel exact SHA gate + safe archive extraction + license/provenance gate；
- single deep runtime module，不做 size-driven split；
- input <=512 MiB；search <=500 pages/call；read <=50 pages/call；per-page text <=1,000,000 internal chars；
- search 明确区分 `results_truncated` 与 `coverage_complete`；
- read-pages continuation 只对**完整页边界**提供 `next_page`；
- TOC 只读 limit+1；
- render DPI 72–400；<=30M px/page；<=200M px/call；<=512 MiB staged artifacts；
- streaming PNG compression；
- render 是 stage-all → transactional publish → rollback-on-publish-failure；
- output parent 必须预先存在；scope 只覆盖 exact `output_dir` tree；
- source stat fence + source SHA provenance；
- 所有 metadata/bookmark/text 输出统一标 `document-supplied / untrusted`；
- visual audit 保持 `render-pages -> image_open`，OCR/semantic/cache/hidden-text advanced detection 继续 deferred，不暗示已经支持。

## 6. MATT Round 3｜clean-room review Final Candidate v0.3

重新只以 v0.3 spec、ABI v1 和 current pypdfium2 5.13.0 contract 为固定点，不沿用前两轮“已经足够安全”的判断。

结论：**仍未收敛。新增 3 个实质问题，其中 1 个促成明显的接口简化。**

### R3-01｜P1 architecture/security｜`overwrite` 让 render 事务复杂度远超当前真实需求

Round 2 为了支持 `overwrite=true` 引入“stage + backup + rollback”。但外部 worker 被 host cancel / timeout / OS hard-kill 时，Python finally/rollback 并没有必然执行，因此把它称为 transaction 会过度承诺；若再加入 journal/recovery，又会让首版接口承担远超“审计渲染”的恢复协议。

MATT deletion test：删除 `overwrite` 后，用户的核心工作流（把若干 PDF 页渲染到一个新的审计目录，再 `image_open`）完全不受损，反而删除大量 hardlink/backup/rollback/cancel-state 知识。

**修订：v0.4 删除 `overwrite` public parameter。**

`render-pages` 要求：

- `output_dir.parent` 必须预先存在；
- `output_dir` 必须不存在；插件只创建该 exact directory；
- 所有 stage temp 都只写在新建的 `output_dir` 内，mutation_scope 与真实写域完全一致；
- 所有 PNG/ZIP 先生成 plugin-owned temp，全部通过预算/hash 后再逐文件 `os.replace(temp, final)`；
- 最后写 `RENDER-COMPLETE.json`（同样 temp → atomic replace）作为**成功提交标记**；
- 正常 exception 时因为目录是本调用独占新建的，可安全 best-effort 删除整个 `output_dir`；
- hard cancel / OS crash 可能留下目录，但**只要缺少有效 completion marker 就明确是不完整输出，不得作为证据使用**；下一次调用拒绝复用同名目录，避免静默续写混合状态。

这里保证的是“final 单文件不 partial + 成功结果有 completion marker + 不覆盖既有证据”，不虚构跨进程 crash-atomic transaction。

### R3-02｜P1 trust｜runtime 不能回退导入宿主环境里的 pypdfium2

当前草案 `_import_pdfium()` 在 `_vendor/` 不存在或不完整时仍可能命中 host/global `pypdfium2`。这会绕过“backend bytes 属于 exact approved extension tree”的设计目标，也可能让 `status` 报 ready 却实际运行未审版本。

**修订：vendored-only import。**

- `_vendor/` 缺失直接 `PDF_BACKEND_UNAVAILABLE`；
- 把 `_vendor` 放到 `sys.path[0]` 后 import；
- 验证 `pypdfium2.__file__` 与 `pypdfium2_raw.__file__` 都 resolve 在 `_vendor/` 内；
- 读取 `VENDOR-PROVENANCE.json`，要求 pinned version=`5.13.0`、wheel SHA 与设计锁一致；
- status 从 vendored module version objects / provenance 返回版本，不用 `importlib.metadata.version()` 猜测 sys.path 上的其它 distribution；
- provenance 缺失/不匹配时 fail closed，不降级到全局 Python 包。

这让 FolderBridge exact-tree-hash approval 真正覆盖运行时 backend。

### R3-03｜P2 resource/lifecycle｜render Job 不应 `timeout_seconds:0`

v0.4 已有明确 page/pixel/byte 上限，因此 render 是一个**有界操作**，没有理由继续使用 host-level unlimited timeout。复杂/恶意 PDF 即使像素预算不大，也可能让 PDFium 在解析/渲染阶段耗时异常。

修订：manifest `render-pages.run_mode=job` 保留，但 host action timeout 固定 **7200 秒**。foreground `info/outline/read/search` 保持 extension 600 秒上限。Job cancel 仍由 FolderBridge worker ownership 处理。

## 7. Round 3 后 Candidate Spec v0.4

在 v0.3 基础上做三项冻结性简化/收紧：

1. **immutable render destination**：删除 overwrite；新目录一次生成；`RENDER-COMPLETE.json` 最后提交；缺 marker 的目录永不当成完整成功证据。
2. **vendored-only backend**：approved tree 内 `_vendor` + `VENDOR-PROVENANCE.json` 是唯一 runtime backend；不接受 host/global fallback。
3. **bounded job lifetime**：render host timeout 7200s，不再无限。

其余 v0.3 边界保持：input 512 MiB、search 500 pages、read 50 pages、1M internal chars/page、search coverage flags、TOC limit+1、render 72–400 DPI / 30M px/page / 200M px/call / 512 MiB artifacts、streaming PNG、source identity fence、untrusted text semantics、OCR/semantic/cache deferred。

## 8. MATT Round 4｜clean review v0.4

按 Standards / Spec 两轴重新审计：public surface、权限、mutation_scope、供应链、后端 provenance、source identity、text/search completeness、render resource/cancel semantics、evidence semantics、未来扩展 seam。

**结论：CLEAN。0 个新增实质问题。**

特别复核：

- 删除 overwrite 后，render 不再需要 backup/journal/恢复协议；用户核心 audit workflow 无损；
- completion marker 只声明“本次成功输出完整”，没有越权承诺 OS-crash 原子性；
- sibling/ancestor 路径不再被插件创建，`output_dir` tree mutation claim 与实际写域一致；
- vendored-only backend 与 exact-tree-hash trust model 对齐；
- 500-page search 窗口和 per-page text cap 都会显式暴露 coverage gap，不制造“零命中=全文不存在”的假证据；
- 一个 cohesive deep runtime module 仍比人为拆成多文件 adapter ceremony 更合适。

本轮没有发现需要改变 v0.4 spec 的 supported finding。

## 9. MATT Round 5｜第二次连续 clean review v0.4

再次从固定 v0.4 spec 做独立 review，并额外用反例检查：超大页面、500+页 PDF、无文本扫描件、host 已装另一版 pypdfium2、已有同名输出目录、hard cancel、TOC 极大、casefold 扩字符、输入调用中变化、wheel archive traversal、缺 license/provenance。

**结论：CLEAN。0 个新增实质问题。**

没有发现新的 P1/P2 correctness/security/spec finding；所有反例都已经有 fail-closed、bounded 或显式 coverage/trust 语义。

### DESIGN CONVERGENCE GATE

- Round 1：10 findings → 修订；
- Round 2：4 new findings → 修订；
- Round 3：3 new findings → 修订；
- **Round 4：0 new material findings；**
- **Round 5：0 new material findings。**

达到用户要求的**连续两轮设计审计收敛**。从此允许进入 `implement` 阶段；实现若暴露 spec 不可行/不安全证据，必须显式 reopen design gate，不得静默偏离。

## 10. 实现阶段执行顺序（MATT implement + TDD）

按 public seam 逐个 vertical slice：

1. manifest v0.4 contract（remove overwrite / DPI 400 / job 7200）；
2. vendored-only backend + provenance status；
3. input size + source identity fence；
4. bounded text extractor + honest read-pages pagination；
5. mapped casefold literal search + 500-page window + coverage flags；
6. bounded TOC limit+1；
7. render geometry/pixel preflight；
8. streaming PNG + immutable output dir + completion marker + failure cleanup；
9. installer exact wheel SHA + safe wheel extraction + license/provenance gate；
10. focused PDF tests；
11. full FolderBridge regression；
12. post-implementation independent code-review against Standards + Final v0.4 spec。

## 11. Implementation Review I1｜首次实现后独立审计

固定点：已按 v0.4 spec 实现的 manifest/runtime/installer/tests。按 MATT `code-review` 把 Standards 与 Spec 两轴分开，不因为设计层已经收敛就默认实现正确。

结论：**未收敛。发现 11 个实现级 supported findings，均要求 red regression → 修复。**

1. source 在 render staging 后变化时，fresh output 目录需要清理，不能留下貌似可用的未提交证据目录；
2. `read-pages` 因 response budget 截断时，`coverage_complete` 不能仍为 true；
3. `info` 文本层采样异常不能静默伪装成“扫描件无文本”，必须显式返回 sample error / undetermined；
4. dense literal search 达到结果 cap 后不能继续扫描百万匹配尾部，且必须区分 result truncation 与 search-window completeness；
5. runtime path gate 需要拒绝 Windows ADS `:`、设备名、trailing dot/space 和保留字符；
6. wheel archive member gate 需要同样拒绝 Windows alias/ADS/device-name 路径，而不只防 `..`；
7. `-Force` 安装不能先删除旧插件；必须 staged cutover + rollback；
8. nominal render pixel preflight 之外还要校验 PDFium 实际输出的 total pixels；
9. document-supplied metadata/TOC 字符串需要 response bounds + explicit truncation；
10. Force-install backup 不能暂存在 hot-scan `extensions` 根里，避免并发 rescan 看见 duplicate extension id；backup 改到 sibling `extension-backups`；
11. upstream fetch 不应猜测固定 branch；应跟随各 repo default/current branch并记录 resolved branch+commit，同时把 nfsarch33 纳入 research-only 对照集而非实现依赖。

全部 finding 已用新增/修订回归测试锁定并修复。实现保持单一 deep runtime module；没有为了修 finding 做无收益的文件拆分。

## 12. Implementation Review I2｜修复后 clean review

重新从当前磁盘实现做 Standards-first review，范围包含：

- ABI v1 manifest / global authorization / mutation scope；
- runtime 是否存在 network/process/shell/URL/regex 意外能力；
- workspace path/link/reparse/Windows alias confinement；
- source identity、response budgets、metadata/TOC/text/search coverage semantics；
- render fresh-directory transaction、pixel/byte bounds、completion marker、cancel/crash evidence semantics；
- vendored-only backend、provenance、wheel/license gate、force-install rollback 和 hot-scan isolation；
- upstream research clone 与 packaged runtime 的许可证/边界分离。

**结论：CLEAN。0 个新增实质问题。**

本轮确认：公共接口仍只有六个 bounded actions；运行时没有网络/进程权限；所有 document-supplied 可返回文本都有上限或 coverage/truncation 语义；render 的实际 mutation 仍局限 exact fresh `output_dir` tree；安装更新失败可恢复旧插件；research clones 永不进入 installed extension tree。

## 13. Implementation Review I3｜第二次连续 clean review

再次按 Spec-first 独立复核 Final v0.4 的正向要求与 negative requirements，并以反例重新穿透：500+ 页搜索、单页超长文本、超大 metadata/TOC、casefold 扩字符、source 中途变化、400 DPI 大页、实际 raster 尺寸偏差、硬 cancel、同名 render destination、ADS/device-name 路径、恶意 wheel member、缺 provenance/license、host 全局装了另一版 pypdfium2、Force 更新切换失败、并发 hot rescan。

**结论：CLEAN。0 个新增实质问题。**

连续两轮 implementation review 无新增 supported finding，达到用户要求的实现审计收敛门。

### IMPLEMENTATION CONVERGENCE GATE

- I1：11 findings → TDD 修复；
- **I2：0 new material findings；**
- **I3：0 new material findings。**

实现现可进入“安装／exact-tree-hash approval／运行态验收”阶段。安装与审批不是代码审计的替代品；运行态 acceptance 必须在用户加载后，用实际 installed tree 与 Ottawa WUDC 2027 PDF 继续验证。

## 14. 运行态证据导致设计门重开｜v0.4.1 → v0.5.0

v0.4 设计／实现审计虽然连续收敛，但首次真实 frozen-host acceptance 暴露了一个设计阶段无法由源码单测证明的问题：`pypdfium2/PDFium` 在 FolderBridge 冻结 worker 中发生 native import hard failure。第一次表现为 frozen stdlib 中缺少 `ctypes.util`；补 compatibility shim 后仍出现 worker 直接退出／invalid-JSON transport failure。由于这是**真实宿主可运行性反证**，原 IMPLEMENTATION CONVERGENCE GATE 自动失效，必须显式重开，而不能用更多 shim 把 native loader 风险继续塞回 worker。

v0.5 的最小架构变更因此为：

- text/metadata/outline/page geometry：改为 exact-wheel-hash vendored pure-Python `pypdf==6.16.2`；
- visual page rendering：从 Python native PDF backend 剥离，改为固定 `pdf_render.ps1` 调用 Windows 平台 `Windows.Data.Pdf`；
- public audit workflow **不变**：`info -> search -> read-pages -> render-pages -> image_open`；
- public actions 仍只有六个 bounded actions，不新增 URL、任意 shell、任意 executable、OCR、semantic search 或 PDF mutation；
- runtime 仅新增最窄 `process.execute:powershell.exe` 权限，argv 固定围绕 approved script 构造；
- pypdf wheel 继续 exact SHA + safe archive extraction + license/provenance + vendored-only import；
- render 继续 fresh immutable output directory + last-write completion marker，不恢复 overwrite。

这不是功能扩张，而是把“文本解析”和“视觉渲染”拆到更符合 frozen-host 现实的两个稳定 seam。

## 15. v0.5 Implementation Hardening H1｜从真实宿主失败重新做 red-green review

固定 v0.5 架构后，按 MATT `tdd` + `code-review` 从外部进程 ownership、视觉证据真实性、资源预算与 control-plane 可核性重新穿透。该轮**未收敛**，最终共有 7 个 supported findings；均先以 regression/contract test 暴露，再修复：

1. **child-process ownership/cancel**：Windows renderer 不能用裸 `Popen/kill`。现改为 FolderBridge public `owned_process_group_kwargs` + `terminate_owned_process_tree`，并消费 host `job_cancel_path`；timeout/cancel 都终止 owned process tree。
2. **actual PNG evidence**：不能只信 pypdf nominal geometry。现读取每张实际 PNG 的 signature/IHDR width/height，以实际 raster 像素再次执行 per-page / total pixel budget，并在结果中分别保留 nominal 与 actual geometry。
3. **TOC depth coverage**：超过 `TOC_MAX_DEPTH` 不能静默停止。现返回 explicit `truncation_reasons=["max_depth"]`（可与 `max_items` 并存），`total=null` 表示总量未知。
4. **parser-memory truthfulness**：1M chars/page 只是返回文本 cap，不是 parser memory sandbox。README 现明确 `pypdf` 可能在截断前为 pathological page 分配更多内存；恶意 PDF 仍需隔离环境。
5. **renderer protocol binding**：worker 不能只相信 renderer 给出的文件列表。现严格核对 `source_units / selected_range / dpi_nominal`，并要求第 N 个输出名精确等于对应 `Pxxxx.png`，错页／错范围／错 DPI 一律 fail + fresh output cleanup。
6. **renderer-side pre-render budget**：Python/pypdf preflight 之外，Windows.Data.Pdf 必须依据**它自己看到的实际 page.Size** 在 raster 前再执行 30M px/page、200M px/call gate；避免两个 backend 对 geometry 理解不一致时先分配超预算 bitmap。
7. **research refresh default branch**：原 `fetch-upstreams.ps1 -Refresh` 会优先沿用本地当前 branch；若 upstream 后续迁移 default branch，文档中的“跟随 current default branch”会失真。现通过 `git ls-remote --symref origin HEAD` 每次 refresh 重新解析远端 current default，再 fetch + checkout `FETCH_HEAD`；普通非 Refresh 的 keep 模式仍不主动改 snapshot。

另外同步收紧了测试 fixture，使旧 v0.4 regression fake renderer 接受新增 cancel keyword；这只是测试适配，不计独立 finding。

## 16. v0.5 Implementation Review H2｜Standards-first clean review

在 H1 全部 red→green 后，重新以当前磁盘 v0.5 作为唯一固定点，不继承 v0.4 的 clean 结论。复核范围：

- ABI v1 manifest、六动作 public surface、global authorization、exact `output_dir` tree mutation scope；
- runtime permission 是否仍只有 workspace read/write + fixed PowerShell execute；
- pypdf vendored-only import、exact wheel SHA、provenance/license、host/global package bypass；
- path/ADS/device-name/trailing-dot-space/link/reparse confinement；
- source identity fence、metadata/TOC/text/search bounds 与 coverage semantics；
- child process ownership、cancel、7000s internal ceiling 与 7200s host Job ceiling；
- pypdf nominal geometry + Windows.Data.Pdf actual geometry 双 preflight；
- actual PNG IHDR、strict page filename/range/DPI binding、artifact byte budget、fresh destination、completion marker；
- installer staged cutover/rollback 与 research clone/runtime tree 分离；
- upstream default-branch refresh 语义与文档一致性。

**结论：CLEAN。0 个新增 material finding。**

本轮同时以 Microsoft 当前 Windows.Data.Pdf API 文档复核：`PdfPage.Size` 与 `PdfPageRenderOptions.DestinationWidth/Height` 都以 DIPs 表达，因此 renderer 的 DIP↔目标像素换算方向与 API contract 一致；最终 actual PNG IHDR 仍是像素预算的 authoritative postcondition。

## 17. v0.5 Implementation Review H3｜第二次独立 counterexample review

第二轮不沿用 H2 finding 集，改用反例重新攻击：frozen host 无 native PDF DLL、host 已装另一版 pypdf、缺 vendor provenance、恶意 wheel member、ADS/device path、source 中途变化、超长单页文本、超深/超大 outline、casefold 扩字符、dense search cap、扫描件 text sample error、pypdf 与 Windows renderer geometry 分歧、400 DPI 大页、renderer 回错页名/范围/DPI、PNG 假扩展名/无 IHDR、child timeout/cancel、hard-kill 留残目录、upstream default branch 改名、ForceInstall cutover 失败。

**结论：CLEAN。0 个新增 material finding。**

关键边界仍明确：

- pypdf 返回文本 cap **不等于** hostile-parser memory sandbox；
- hard cancel/OS crash 可能留下 fresh output directory，但没有有效 `RENDER-COMPLETE.json` 就不得当作成功证据；
- external Extension exact-hash approval 不是 OS sandbox；不受信任 PDF 仍应在 VM/container 级隔离中打开；
- research clone 仅用于设计研究，不进入 installed runtime tree。

### v0.5 IMPLEMENTATION CONVERGENCE GATE

- H1：7 findings → TDD 修复；
- **H2：0 new material findings；**
- **H3：0 new material findings。**

达到用户要求的**连续两轮实现审计收敛**。当前允许进入 `0.5.0 install -> exact-tree-hash reapproval -> installed-tree runtime acceptance -> Ottawa WUDC 2027 real-PDF acceptance`。

当前完整 FolderBridge suite 的 PDF Toolkit 相关测试已经全部 GREEN；全仓仍有一个与 PDF Toolkit 无关的既有失败：`test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body`，当前 runtime instructions 长度为 `6001 > 5000`。该失败不属于 PDF Toolkit diff，本轮不越界修改 core instruction surface，也不把“全仓 exit=1”误报成插件失败。

## 18. 第二次真实 frozen-host 反证｜v0.5.0 → v0.5.1

v0.5.0 通过源码测试与连续两轮 implementation review 后，真实 installed-tree acceptance 仍报告：Extension `trusted/enabled/loaded=true`，Windows.Data.Pdf renderer ready，但 vendored pypdf text backend `ready=false`、`loaded_pypdf_version=null`、`vendor_provenance=null`，错误为 `Could not import the approved vendored pypdf backend.`。磁盘与 frozen-host 诊断进一步定位到：pypdf reader 公共导入链会 eager import `XmpInformation`，而 XMP 模块需要 `xml.dom`；当前 FolderBridge.exe 冻结运行时没有打包这项对 PDF Toolkit 核心 reader/text workflow 非必需的可选 stdlib 子模块。

该运行态反证再次重开实现门。处置原则不是扩大 FolderBridge core bundle、不是回退宿主/global pypdf，也不是吞掉一般 import failure，而是保持 exact-tree trust model：

- pinned upstream wheel仍为 `pypdf==6.16.2`，wheel SHA 不变；
- installer 只在 exact wheel SHA 已通过后，对 `pypdf/_reader.py` 与 `pypdf/_doc_common.py` 中各唯一一处 `from .xmp import XmpInformation` 做 compatibility guard；
- 只有 `ModuleNotFoundError.name == "xml"` 或 `xml.*` 才降级成 XMP placeholder；任意其它缺失模块继续原样失败；
- PDF Toolkit 明确不暴露 XMP metadata capability；placeholder 若被实例化会明确报 XMP unavailable；
- installer 记录两文件 patch 前后 SHA；runtime 通过 `VENDOR-PROVENANCE.json` 复核 exact patch set 与 post-patch bytes；
- manifest 权限、六个 public actions、workspace/path/resource/render 边界均不扩大。

## 19. v0.5.1 Review R1｜red regression + 最小修复

第一次 fresh 全仓回归运行 `420 tests`。除既有 core `6001 > 5000` 外，新增一个 PDF Toolkit red：

`test_vendor_provenance_rejects_missing_patch_and_accepts_exact_patched_file_hashes`

supported finding：runtime 当时只要求“已知 compatibility patch id 恰好命中一次”，却没有拒绝 provenance 中额外的未审 patch record；这与“approved extension tree 中 compatibility patch set 必须精确等于已审集合”的 fail-closed 语义不一致。

最小修复：`_load_vendor_provenance()` 现在同时要求 `len(compatibility_patches) == 1` 且该唯一记录就是 `pypdf-reader-no-xmp-xml-v1`，否则 `PDF_VENDOR_PROVENANCE_MISMATCH`。不改变 public API 或权限。

同时补充行为型 regression，而非只检查源码字符串：直接取 installer 实际 compatibility replacement，构造临时包验证：

1. `.xmp` 因 `xml.dom` 缺失时报 `ModuleNotFoundError(name="xml.dom")` → reader module 可以 import，XMP placeholder 被调用时明确失败；
2. `.xmp` 因无关模块缺失时报 `ModuleNotFoundError(name="unrelated.module")` → compatibility guard 不得吞掉，必须继续抛原异常。

修复后 fresh 全仓回归为 `421 tests`，PDF Toolkit 全部 GREEN；唯一失败仍是既有、无关的 core runtime-instructions `6001 > 5000`，另有 2 个平台性 skip。

## 20. v0.5.1 Review R2｜Standards-first clean review

从修复后的 0.5.1 当前磁盘实现重新独立审查，不继承 R1 finding 集。检查范围：

- manifest/public surface/permissions 是否因 compatibility fix 扩张；
- exact wheel pin、safe extraction、单锚点 patch、两目标文件与 pre/post SHA provenance；
- runtime 是否要求 exact compatibility patch set 且逐文件复核 post-patch SHA；
- vendored-only import 与 global-host fallback rejection；
- compatibility guard 是否只覆盖 `xml`/`xml.*`；
- XMP capability 是否仍显式 disabled，且 reader/text/metadata/outline/search/render 既有语义无漂移；
- staged install cutover/rollback、README/NOTICE/manifest 版本与安全说明一致性。

**结论：CLEAN。0 个新增 material finding。**

特别确认：修复不需要把 `xml.dom` 加进 FolderBridge.exe，也不需要新增 runtime network/process 权限；因此 frozen-host 兼容性没有通过扩大 core dependency/trust surface 获得。

## 21. v0.5.1 Review R3｜第二次独立 counterexample review

第二轮从反例重新攻击：provenance 缺失、额外 patch record、patched bytes 被篡改、host 已有其它 pypdf、正常主机有 XML、frozen host 缺 `xml.dom`、缺无关模块、XMP 被误调用、wheel anchor 漂移、manifest/version/docs 漂移、Force install cutover 失败、renderer/原有 workspace confinement 被兼容改动意外扩大。

**结论：CLEAN。0 个新增 material finding。**

现有 guard 的边界保持为：只让 PDF Toolkit 不使用的 optional XMP/XML import 不再阻断 reader/text public workflow；任何未审模块缺失、provenance 漂移、patched-byte 漂移仍 fail closed。没有发现需要修改 FolderBridge core 或把 pypdf trust 迁出 approved extension tree 的理由。

### v0.5.1 IMPLEMENTATION CONVERGENCE GATE

- R1：1 material finding → regression → 最小修复；
- **R2：0 new material findings；**
- **R3：0 new material findings。**

达到连续两轮 clean implementation review。0.5.1 现允许进入：`install -Force -> rescan -> exact-tree-hash approval -> fresh installed status -> Ottawa real-PDF full-chain acceptance`。在真实 installed 0.5.1 的 `status -> info -> search -> read-pages -> render-pages -> image_open` 全链通过前，仍不得称 PDF Toolkit runtime PASS。

## 22. 第三次真实 frozen-host 反证｜installed v0.5.1 仍失败 → v0.6 设计门重开

用户完成 `0.5.1 install -Force -> rescan -> exact-tree-hash approval -> enable` 后，fresh `server_info / extension info / status` 给出新的 authoritative runtime evidence：

- installed version=`0.5.1`；
- installed exact-tree SHA-256=`41ad29d62fcd853a10548d52a2b13b8f774e50d4925038954a8369c558568b1e`；
- trusted=true / enabled=true / loaded=true / approval_stale=false；
- permissions 仍只有 `workspace.read / workspace.write / process.execute:powershell.exe`；
- Windows.Data.Pdf renderer 仍 ready；
- **text backend 仍 `ready=false`**；
- `loaded_pypdf_version=null`、`vendor_provenance=null`；
- error 仍为 `ExtensionError: Could not import the approved vendored pypdf backend.`。

这次反证使 v0.5.1 implementation convergence gate 对“可运行性”再次失效。关键结论不再是“再漏了一个 stdlib module”，而是：**把第三方 pure-Python package 放进 external Extension `_vendor` 并不能让它脱离 PyInstaller frozen interpreter 的 stdlib closure；包仍依赖 FolderBridge.exe 构建时是否冻结了其 import graph 所需模块。** 0.5.1 对已知 `xml.dom/XMP` 路径的窄 guard 只是修掉一个已知节点，不能证明其余标准库闭包完整。

因此明确禁止继续用“遇到一个 missing stdlib 就补一个 shim/guard”的方式推进。该路线会形成开放式 host-specific compatibility list，重复 v0.4 native shim 的失败模式，并把真实架构问题伪装成连续小补丁。

v0.6 设计目标改为：**第三方 PDF parser 完全离开 frozen Python interpreter**；`plugin.py` 只保留 policy/orchestration，第三方 reader 通过现有、已获批的 fixed `powershell.exe` process seam 运行；视觉层继续使用已经实机工作的 Windows.Data.Pdf。

候选 parser：`PdfPig 0.1.16` 的 .NET Framework 4.7.1 资产。NuGet 当前稳定 0.1.16 于 2026-08-22 发布，Apache-2.0；net471 直接依赖 `Microsoft.Bcl.HashCode >=6.0.0` 与 `System.Memory >=4.6.0`。PdfPig 官方文档直接提供 `PdfDocument.Open / NumberOfPages / page.Width / page.Height / document.Information / TryGetBookmarks`，并明确建议索引/RAG 文本使用 `ContentOrderTextExtractor.GetText(page)`，而不是直接相信 `page.Text` 的 PDF 内部内容顺序。

## 23. v0.6 Design Review D1｜from-zero architecture attack

固定输入：第三次真实 runtime failure、FolderBridge ABI v1、现有六动作 public surface、现有 PowerShell-only process permission、Windows.Data.Pdf 已实机工作，以及 `docs/pdf-toolkit-external-extension-design-20260903.md` 中首版 v0.6 candidate。按 deep-module / runtime-verification / Standards+Spec 两轴重新审查，不继承 v0.5.x clean 结论。

**结论：未收敛。6 个 material findings。**

### D1-01｜P1 trust｜`AssemblyResolve` 不能单独证明使用的是 approved vendored DLL

CLR 正常 assembly resolution 会先尝试已加载/default load context、平台/GAC 等路径；只有解析失败后才会触发 `AssemblyResolve`。因此“给 `_vendor-dotnet/` 注册 AssemblyResolve”不足以证明 package-owned assembly 一定来自 exact approved tree。若机器上恰有同名强名称程序集，第三方 bytes 可能绕过 Extension provenance。

修订：

- 把 **platform/.NET Framework assemblies** 与 **package-owned vendored assemblies** 明确分层；
- 对所有 package-owned assembly，`pdf_inspect.ps1` 在 parser 使用前按 provenance 明确的依赖顺序调用 exact-path `Assembly.LoadFrom()` 预加载；
- 每次 load 后立即验证 `Assembly.FullName` 与 provenance-declared identity，并读取 `Assembly.Location`，要求 resolve 后路径严格位于 approved `_vendor-dotnet/`；
- 对 package-owned assembly，任何已加载同名 assembly 若 location 不在 vendor tree，直接 fail closed，而不是接受 GAC/global copy；
- `AssemblyResolve` 仅作为后续依赖解析的补充，返回值也必须来自已验证 exact-path map；不得搜索 workspace/PATH/current-directory/global package folders；
- feasibility probe 必须刻意制造 outside-vendor 同名 assembly / missing vendored dependency 反例，证明不会被全局/GAC 偷偷满足。

### D1-02｜P1 compatibility｜不能把依赖“最低版本”机械当成 v0.6 lock

首版 candidate 把 `System.Memory 4.6.0 / Buffers 4.6.0 / Numerics.Vectors 4.6.0 / Unsafe 6.1.0` 直接当 exact pin，只因为它们满足最小约束。这不是 current-compatible lock 的充分理由。NuGet 当前 `System.Memory 4.6.3` 的 net462 依赖已经前移到 `System.Buffers >=4.6.1 / System.Numerics.Vectors >=4.6.1 / System.Runtime.CompilerServices.Unsafe >=6.1.2`；这些维护版本均提供 .NET Framework 4.6.2 资产。

修订候选 lock 为：

- `PdfPig 0.1.16`；
- `Microsoft.Bcl.HashCode 6.0.0`；
- `System.Memory 4.6.3`；
- `System.Buffers 4.6.1`；
- `System.Numerics.Vectors 4.6.1`；
- `System.Runtime.CompilerServices.Unsafe 6.1.2`。

这仍不是最终供应链锁；必须经实际 nupkg asset/assembly probe 后才能 freeze exact package hashes 和 DLL set。若 feasibility 证明新维护版本与 PdfPig net471 组合不兼容，允许回退到经实测的较低版本，但必须有明确证据，不得按最低约束拍脑袋。

### D1-03｜P1 platform｜“有 Windows PowerShell 5.1”不等于满足 PdfPig net471 runtime

PowerShell executable 存在只能证明 process seam 可用，不能证明目标机 .NET Framework 足以运行 `net471` asset。

修订：`status` 与 feasibility probe 必须显式核查可运行的 .NET Framework baseline（至少满足 4.7.1），并返回清晰的 platform-runtime state。低于 baseline 时 parser capability fail closed；不得在加载 DLL 后才用晦涩 `BadImageFormat/FileLoad/TypeLoad` 异常让用户猜原因。该检查不增加 manifest permission，只读取 OS/.NET runtime 自身状态。

### D1-04｜P1 resource/evidence｜bounded protocol 不能把全文先吐给 Python 再截断

如果 `pdf_inspect.ps1` 对一页/全范围先生成无界字符串，然后通过 stdout 返回，Python 端再执行 1M chars/page、500k response、500 pages/search 等限制，资源边界已经被突破；stdout/memory 也可在 wrapper 验证前被恶意 PDF 放大。

修订：**资源限制必须在 parser 进程内部第一现场执行。** 固定脚本本身必须：

- info sample 只提取 bounded sample；
- read-pages 在 PowerShell 内执行 page-range + per-page text cap + response-char cap，并保持诚实 continuation 语义；
- search 在 PowerShell 内做 literal search、500-page window、per-page cap、max-results+1 early stop、bounded snippet；
- outline 在 PowerShell 内限制 items/depth/title length；
- metadata 在 PowerShell 内限制字段和值长度；
- 返回 coverage/truncation flags；
- Python 再做一层 schema/range/count/size revalidation，但不能依赖收到 unbounded payload 后才安全。

### D1-05｜P1 supply chain｜package hash 获取本身必须成为正式 gate

设计已写“不能虚构 NuGet hash”，但仍缺少执行门定义。外部 nupkg 是新的供应链根；FolderBridge exact-tree approval 只能固定安装后的结果，不能代替下载前的官方 package identity lock。

修订：实现前必须先完成 **candidate-fetch/review/lock**：从 NuGet 官方 package source 取得 exact nupkg，记录官方 package identity/integrity metadata，并本地计算 SHA-256；只有二者与 package id/version 一致且 package contents/TFM/license 通过人工/测试审查后，才把 SHA-256 固化进 installer/tests/provenance contract。安装器正式路径只接受 locked bytes，不做 TOFU，不允许“第一次下载后顺手写 hash”。

### D1-06｜P1 feasibility/text quality｜不能从 README 示例推断 stable nupkg 一定包含 `ContentOrderTextExtractor` 所需 runtime asset

PdfPig 0.1.16 NuGet 页面确实直接示例 `ContentOrderTextExtractor.GetText(page)`，并警告 `page.Text` 常不是可读顺序；但 production design 仍需要对**实际下载的 stable nupkg**确认 namespace/type 位于哪一个 DLL、该 DLL 是否属于 package runtime payload、是否有额外 package dependency。不能只凭 repo solution/project layout猜测。

修订：candidate-fetch 后必须 inspect nupkg asset list + assembly metadata，并在 same-PowerShell feasibility probe 中直接 resolve/call `UglyToad.PdfPig.DocumentLayoutAnalysis.TextExtractor.ContentOrderTextExtractor`。若稳定 package payload不能在 locked dependency set 中提供它，v0.6 设计必须重新选择明确的 text-order strategy；**禁止静默退化为 raw `page.Text`**。

### D1 修订后冻结项

- public actions 不变；
- permissions 不变；
- `plugin.py` 不再 import 第三方 PDF parser；
- fixed `pdf_inspect.ps1` + existing `pdf_render.ps1` 形成解析/视觉两个 process seam；
- package-owned DLL 必须 exact-path preload + location/identity/hash 验证；
- candidate dependency set 改为 current maintenance-compatible versions，但最终 hash/DLL lock 必须等 candidate-fetch + feasibility probe；
- net471 baseline 显式 preflight；
- text/search/outline/metadata bounds 在 parser process 内执行，Python 双重验证；
- ContentOrderTextExtractor availability 必须由真实 package + runtime probe证明；
- design 未连续两轮 clean 前禁止 production implementation。

## 24. v0.6 Design Review D2｜revised-spec independence attack

从 D1 修订后的 v0.6 设计重新开始，不沿用 D1 finding 集。重点攻击 backend coupling、真实 capability 语义、Extension host limits、snapshot/approved-tree 边界和 failure isolation。

**结论：仍未收敛。新增 2 个 material findings。**

### D2-01｜P1 architecture/capability｜`render-pages` 必须真正独立于 inspection parser

0.5.1 已经暴露一个设计/实现错位：`status.capabilities.page_render_png=true` 仅表示 Windows.Data.Pdf seam ready，但实际 `_render_pages()` 先调用 pypdf 打开文档、读取页数/geometry，因此 text backend import failure 会让 render-pages 同样不可调用。也就是说视觉层虽然被描述为独立，执行路径仍被 parser 强耦合。

v0.6 不应把同一耦合迁移为 `PdfPig preflight -> Windows.Data.Pdf render`。修订为：

- `render-pages` **不依赖 PdfPig / pdf_inspect.ps1**；
- Python 仍负责 workspace-relative source confinement、source identity SHA/stat fence、fresh output-dir transaction、owned child process、artifact/result validation；
- `pdf_render.ps1` 作为 Windows.Data.Pdf authoritative visual seam，自行打开 PDF、取得 `PageCount` / `PdfPage.Size`、在任何 raster allocation 前验证 requested range、per-page/total pixel budget，再渲染；
- renderer 返回 `source_units / selected_range / dpi / per-page actual geometry/files`，Python 对协议和 source fence 再校验；
- 无效页范围可以在 fresh output_dir 已创建后由 renderer fail，Python 正常异常路径删除该目录；不需要为了“先知道页数”重新引入第三方 parser；
- `status` 应分别报告 `inspection_ready` 与 `page_render_png`; overall `ready` 仍可定义为完整 audit workflow 两层都 ready，但单项 capability 必须对应真实可调用路径。

这样真正实现 text evidence layer / visual evidence layer failure isolation，也使 Ottawa 视觉核验 seam 不被第三方 parser 可用性连坐。

### D2-02｜P1 host-compatibility｜新 vendor payload 必须显式满足 FolderBridge Extension snapshot hard limits

FolderBridge core 当前对每个 Extension 的 verified tree 有硬上限：`MAX_EXTENSION_FILES=256`、`MAX_EXTENSION_BYTES=64 MiB`。PdfPig 0.1.16 的 NuGet package 本身约 15.94 MiB，且包含多 TFM/开发资产；若 installer 粗暴保存完整 nupkg 展开树或多余 TFM，很容易无谓扩大 approved snapshot，并可能触碰 host limit。即使当前候选预计能装下，设计也不能把它留到 rescan 才发现。

修订：

- candidate-fetch 可以在 research/temp staging 保存完整 nupkg 供审查，但 **installed Extension tree 只复制 locked runtime DLL set + required license/NOTICE + fixed scripts/manifest/docs/provenance**；
- 不把 nupkg、symbols、其它 TFM、build/analyzer/ref/source assets装进运行树；
- installer staged validation 在 cutover 前计算 exact file count/total bytes，并以 FolderBridge 当前 256 files / 64 MiB 作为兼容 gate；超限直接失败；
- 测试锁住该 budget，并要求未来若 core limit变化必须显式重新审计，而不是 silently rely on larger host；
- provenance 仍记录 package-level hash/来源与所选 runtime asset，完整 nupkg 不需要留在 installed tree 才能保持供应链可追溯。

### D2 修订后候选

v0.6 现在有三个真正独立的层次：

1. `plugin.py` policy/orchestration，不含第三方 PDF import；
2. `pdf_inspect.ps1 + locked PdfPig net471 DLL set` 负责 bounded structure/text evidence；
3. `pdf_render.ps1 + Windows.Data.Pdf` 负责独立 visual evidence。

inspection failure 不得虚假标绿，但也不得阻断单独 render capability；installed vendor tree 必须在 FolderBridge 现有 snapshot budget 内。设计门继续关闭，进入下一轮 clean-room review。

## 25. v0.6 Design Review D3｜Unicode + loader semantics attack

第三轮从 evidence fidelity 与 CLR 官方 loading semantics 重新攻击，不继承 D1/D2 的 finding 集。对照现有 v0.5 regression contract，特别检查中文/Unicode stdout、case-insensitive literal search、original-text offsets、LoadFrom context 和 package-owned assembly identity。

**结论：仍未收敛。新增 3 个 material findings。**

### D3-01｜P1 evidence integrity｜Windows PowerShell 5.1 stdout encoding 必须显式固定 UTF-8

现有 renderer 只返回 ASCII-ish JSON，因此 Python 用 UTF-8 decode 未暴露问题；新 inspector 会把 PDF 原文（中文、重音字符、非 BMP 字符）写进 JSON。Windows PowerShell 5.1 的 console encoding 不能被默认假定为 UTF-8。若脚本直接 `[Console]::Out.WriteLine(ConvertTo-Json ...)`，Python 端固定 UTF-8 decode 可能产生 silent replacement/corruption，恰好破坏本插件最核心的“精确文本证据”。

修订：

- `pdf_inspect.ps1` 在任何输出前显式把 console output encoding 设为 BOM-less UTF-8；
- stdout 只允许一个 UTF-8 JSON envelope；diagnostic stderr 同样按明确 UTF-8 contract 输出；
- Python inspector decoder 使用 strict UTF-8，invalid byte sequence 直接 protocol failure，**禁止 `errors="replace"` 把证据乱码后继续当成功**；
- regression 必须覆盖中文、`é/ß`、emoji/非 BMP 字符、换行与 JSON escape；round-trip 后文本逐 code point 相等。

### D3-02｜P1 semantic compatibility｜.NET ignore-case 与 UTF-16 offset 不能静默替换现有 Python casefold/offset contract

现有 regression 明确锁住：`"Straße X".casefold() == "strasse x"`，并要求扩展映射后的 `ss` 两个 folded positions 都回指原文 `ß` 的同一个 original-text index。迁移到 PowerShell/.NET 后如果直接使用 `StringComparison.OrdinalIgnoreCase` / `.IndexOf()`：

- full Unicode casefold 语义不等价（扩字符尤其危险）；
- .NET string index 是 UTF-16 code-unit offset，而现有 Python `len/index` 是 Unicode code-point index，emoji 等 supplementary characters 前后的 `char_offset` 会无声漂移。

修订：冻结 public evidence semantics，而不是冻结某个 .NET convenience API：

- case-insensitive literal search 必须继续满足 full-casefold-equivalent regression，至少锁住 expansion mapping（`ß -> ss`）和原文 origin map；
- `page_chars / extracted_chars / char_offset / char_end` 的“character”统一定义为 Unicode scalar/code-point index，不是 UTF-16 code units；
- PowerShell 内部如果使用 UTF-16 index，必须通过 surrogate-aware origin mapping 转回 code-point coordinates；
- bounded char budgets 可在内部更保守地按 UTF-16 units提前截断，但对外 reported counts/offsets 必须符合上述 code-point contract；
- feasibility probe 先测试 .NET Framework built-in comparison 是否满足既有 full-casefold regressions；若不满足，production implementation 必须使用 Extension-owned deterministic casefold mapping（可由固定 Unicode case-fold data生成）并维护 folded-index -> original-code-point map，不能降低语义；
- 新 regression 加入 supplementary-plane 字符位于 match 前方，确认 offset 不被 surrogate pair 多算 1。

### D3-03｜P1 runtime reliability｜不要在 feasibility 之前把 `Assembly.LoadFrom` 本身冻结成规范

Microsoft .NET Framework 文档明确区分 default/load-from/no-context，并提示 LoadFrom context 可能产生 dependency resolution、serialization/casting/type-identity 等意外；官方 best-practices 建议不要在没有验证时把 LoadFrom 当成默认优选。当前 v0.6 设计的真正安全目标是“package-owned assembly 只能由 approved bytes 满足”，不是“必须调用某一个 loader API”。

修订：

- 设计把 loader **API** 从冻结项降级为 feasibility decision；冻结的是 loader invariants；
- candidate probe 至少验证 exact-path LoadFrom 路线的完整依赖绑定、type identity、ContentOrderTextExtractor 调用、loaded assembly inventory；若 LoadFrom context 出现冲突，允许测试受控 `Assembly.Load(byte[]) + AssemblyResolve` 等官方支持路线；
- 无论最终选哪条，parser API 调用前必须能够枚举/证明所有 package-owned assembly 的 FullName 与来源状态符合 provenance，且不存在 outside-vendor/global/GAC package-owned substitute；
- 若 loader 路线无法在 Windows PowerShell 5.1 中同时满足可调用性与 exact provenance，**v0.6 设计失败**，不能靠 binding shim / machine-wide install / 修改 powershell.exe.config 解围；
- production spec 只有在 feasibility probe 后才能冻结具体 loader strategy。

### D3 修订后门禁

此时 v0.6 已不只是“换 PdfPig”：它明确冻结了证据编码、Unicode search/offset contract 与 loader trust invariants。下一轮必须从该修订候选重新独立检查；D3 本身不计 clean。

## 26. v0.6 Design Review D4｜cross-host determinism attack

从 D3 修订候选攻击“同一 PDF 在不同 Windows/.NET 机器上是否仍得到同一 case-insensitive search 语义”。Unicode 官方明确区分大小写转换与 case folding；`CaseFolding.txt` 是 locale-independent caseless matching 的正式数据来源，并包含 `ß` 等不能靠单字符 lowercase 处理的映射。Unicode 17.0 文档仍以该文件定义 default case folding；Unicode Data Files 当前受 Unicode License v3（SPDX `Unicode-3.0`）覆盖。

**结论：仍未收敛。新增 1 个 material finding。**

### D4-01｜P1 determinism/licensing｜production casefold 不能依赖宿主 NLS/CompareInfo 版本

D3 仍保留“如果 .NET built-in comparison 通过 feasibility regression 就可以使用”的空间。但即使当前机器样例通过，Windows/.NET Framework 的文化/NLS 实现与版本仍属于 host platform 行为；这不能证明跨目标机拥有与原 Python full casefold 一致且稳定的全部 Unicode 映射。对于 evidence search，跨机器 semantic drift 不应被接受。

修订：production v0.6 **不再以宿主 ignore-case primitive 作为 casefold 真源**。改为 Extension-owned deterministic Unicode fold asset：

- candidate lock 阶段选择并固定一个具体 Unicode CaseFolding data version；当前候选以已发布稳定 Unicode 17.0 为起点，最终仍需记录 exact source URL/file SHA-256；
- 只使用 default locale-independent full case folding 所需 mapping，生成 compact runtime table；生成器和生成结果都要有 deterministic regression；
- runtime asset（例如 `unicode-casefold.json`/紧凑等价格式）进入 exact approved Extension tree，记录 Unicode version、source hash、generated asset hash；
- 保留并随插件分发适用的 Unicode License v3 notice/license；
- PowerShell 按 Unicode scalar 枚举原文，使用固定表生成 folded text 与 folded-index -> original-code-point map；无 mapping 的 code point 原样保留；
- case-sensitive search 继续 exact literal，不经过 fold；
- Python/PowerShell shared regression 从同一固定 mapping contract 测 `Straße/STRASSE`、Greek sigma、扩字符、supplementary-plane offset；
- runtime `status` 暴露 `casefold_unicode_version`，使证据语义可追溯；
- Unicode data更新属于显式版本升级与 exact-tree reapproval，不随 OS 自动漂移。

这个小数据资产比依赖宿主 NLS 更符合 v0.6 的核心原则：第三方/语义关键依赖都由 approved bytes定义。它同样计入 256 files / 64 MiB Extension budget。

### D4 修订后门禁

D4 仍有 finding，不计 clean。下一轮开始，固定 v0.6 candidate 已包含：独立 render、external PdfPig inspection、exact package provenance、runtime platform preflight、bounded parser-side output、strict UTF-8、Unicode scalar coordinates、Extension-owned fixed casefold data、loader feasibility gate与 host snapshot budget。

## 27. v0.6 Design Review D5｜process ownership / timeout attack

本轮专门对照 FolderBridge 0.8.21 的真实 extension worker/job ownership：foreground action timeout >60s 时会预建 cancel control，原 worker 可在 transport budget 后原地 promotion；host timeout/shutdown 持续拥有 worker process tree 并在必要时终止。v0.6 inspection 在 worker 内再启动 PowerShell child，因此必须保证两层 lifecycle contract 不打架。

**结论：仍未收敛。新增 1 个 material finding。**

### D5-01｜P1 reliability｜inspection child 需要 host ceiling 以内的独立 timeout/cancel contract

若 `pdf_inspect.ps1` 只由 `subprocess.communicate()` 无限等到外层 600s action timeout，正常 parser hang 会退化成 host 强杀 worker；这会失去稳定的 `PDF_INSPECT_TIMEOUT/CANCELLED` 错误语义，也缩短 source-fence/协议清理窗口。虽然 host 最终会保住 process ownership，但插件不应把第一层可控超时全部外包给最后的硬终止。

修订：

- inspection public actions 保持现有 600s foreground ceiling；
- Python inspector runner 使用 **570s internal business ceiling**，明显短于 host action timeout；
- runner 采用与 renderer 相同的 `Popen + owned_process_group_kwargs + poll` 模式，而不是单次无界 communicate；
- 每轮 poll 检查 `context.job_cancel_path`；cancel 时调用 `terminate_owned_process_tree(child)`，回收 stdout/stderr 后返回明确 `PDF_INSPECT_CANCELLED`；
- 570s 到期时同样终止 child tree并返回 `PDF_INSPECT_TIMEOUT`；
- child stdout/stderr 必须由 bounded capture/等价机制控制，终止后仍执行 strict UTF-8/protocol handling；
- 外层 FolderBridge 600s timeout 继续作为 fail-safe ownership ceiling，而不是正常业务 timeout；
- tests 覆盖 pre-start cancel、running cancel、internal timeout、child refusal/termination fallback，以及 source/output无 workspace mutation。

570s 不是“PDF 理论最长耗时”，只是当前 600s public action contract 内的安全业务 deadline；若未来确有合法 inspection 需要更长，应一起版本化调整 action timeout与 internal margin，而不是单独把 child改成无限。

### D5 修订后门禁

D5 有 finding，不计 clean。从下一轮开始才重新计连续 clean。固定候选现已覆盖 backend placement、供应链、loader、Unicode、资源、视觉独立性以及双层 process ownership。

## 28. v0.6 Design Review D6｜control-plane coherence attack

从“另一个实现者只读正式设计文档、是否会得到唯一现行规范”角度审查。发现 `docs/pdf-toolkit-external-extension-design-20260903.md` 虽然顶部和后半段已声明 v0.6，但前半段仍把 v0.5 pypdf backend/installer 当作现在时描述，Ottawa acceptance 甚至仍要求 `vendored pypdf` ready，security seam 也只写 `pdf_render.ps1`。这形成同一文件内的双真源。

**结论：仍未收敛。新增 1 个 material finding。**

### D6-01｜P1 spec governance｜必须消除 v0.5 历史段落与 v0.6 现行 contract 的可执行冲突

修订：

- 原 `Backend choice` 改名并醒目标记为 `Historical failed v0.5 backend choice — NOT CURRENT SPEC`；保留它只用于解释为何迁移，不得再被实现阶段视为当前要求；
- 原 pypdf `Installation architecture` 同样标成 v0.5 historical failed implementation；v0.6 install 以 candidate-fetch/locked NuGet + `_vendor-dotnet` + Unicode fold asset 为唯一现行路线；
- current Security/capability 说明改为固定 `pdf_inspect.ps1` **和** `pdf_render.ps1` 两个 PowerShell seam，仍无 caller-supplied script/command/assembly path；
- overwrite/fresh destination 文案改成当前 v0.6 继承的 contract，而不是“v0.5 deliberately”；
- Ottawa acceptance 第一步改为 `inspection_ready (locked PdfPig/.NET seam) + independent Windows.Data.Pdf page_render_png ready`，不再引用 pypdf；
- deferred feature 文字引用 v0.6；
- 在 v0.6 candidate 段明确其 normative precedence：若历史段与 v0.6 冲突，以 v0.6 为唯一现行规范；但更优做法仍是给历史段标题直接去歧义，而不是只靠 precedence 口号。

设计文档可以保留失败历史，但**历史必须不可误执行**。本轮有 finding，不计 clean；修订后才重新开始连续 clean 计数。

## 29. v0.6 Design Review D7｜phase-boundary / unresolved-lock attack

从“当前设计是否已经足以直接 production implementation”重新审查。当前 candidate 有意把三个事实留给真实 feasibility 才能确定：NuGet official integrity + local SHA lock、实际 stable nupkg runtime DLL/ContentOrderTextExtractor asset set、以及 PowerShell 5.1 下最终 CLR loader strategy。这些不是可以在代码阶段边做边定的小细节，而是供应链/运行边界事实。

**结论：仍未收敛。新增 1 个 material finding。**

### D7-01｜P1 process governance｜必须把 architecture convergence 与 final locked-spec convergence 分成两级门

如果当前设计连续两轮 clean 后就直接允许 production code 修改，那么 candidate-fetch/feasibility 后落下的 exact hashes/DLL set/loader strategy 会成为未经两轮设计审计的新规范，违反用户明确的“方案连续两轮收敛后再执行”纪律。

修订为两级 gate：

#### Gate A｜v0.6 Architecture Design Convergence

当前阶段审计的是架构不变量：public API、权限、process seams、供应链规则、host limits、Unicode contract、render isolation、resource/cancel/evidence semantics、feasibility acceptance criteria。

达到连续两轮 0 new material findings 后，**只释放以下非 production mutation 工作**：

1. candidate-fetch/review/lock 到 temporary/research staging；
2. 取得官方 NuGet integrity metadata并计算 local SHA-256；
3. inspect exact nupkg TFM/DLL/license/assembly references；
4. 生成/锁定 Unicode CaseFolding asset；
5. throwaway/sandbox PowerShell feasibility probe，比较 loader strategy并验证 tiny PDF/text/ContentOrder/collision/tamper/Unicode/cancel bounds。

Gate A **不允许**修改 production `folderbridge-extension.json / plugin.py / install.ps1 / pdf_inspect.ps1 / published README contract` 来“先试试看”。临时 probe 必须位于 ignored research/temp area或 tests fixture，不进入 approved production tree。

#### Gate B｜Final Locked v0.6 Spec Convergence

Gate A probe 完成后，把以下 concrete facts 写回正式设计/审计：

- exact package versions + official integrity + SHA-256；
- exact selected TFM/files/DLL SHA/assembly identities/license set；
- exact Unicode CaseFolding version/source SHA/generated asset SHA；
- **唯一最终 loader strategy** 与 probe evidence；
- measured installed-tree file/byte budget；
- probe outputs/known constraints。

然后从该 locked spec 再做独立 review，必须重新取得**连续两轮 0 new material findings**。只有 Gate B 通过，才释放 production TDD implementation：red regression -> manifest/installer/runtime最小实现 -> focused PDF tests -> full repo tests -> implementation review连续两轮 clean -> user reinstall/reapprove -> Ottawa full runtime acceptance。

任何 feasibility 结果如果迫使改变架构不变量（新增 executable/permission、放宽 trust、丢弃 ContentOrder/full-casefold、需要 machine-wide config/GAC install等），Gate A 自动失效，回到 architecture design review，而不是直接写进 Gate B。

### D7 修订后门禁

当前仍在 **Gate A architecture design**。D7 有 finding，不计 clean；必须先对修订后的 Gate A candidate取得连续两轮 clean，才能做 candidate-fetch/feasibility probe。

## 30. v0.6 Design Review D8｜backend-migration behavioral-contract attack

在 Gate A 两级门确定后，从“即使新的 PdfPig/PowerShell backend 技术上可行，是否会悄悄改变六个 public actions 的 observable semantics”重新审查，并与当前 0.5.1 `read-pages/search` 实现逐项对照。

**结论：仍未收敛。新增 1 个 material finding。**

### D8-01｜P1 correctness/contract｜必须冻结 backend-independent read/search 行为，而不能只写“语义不变”

现行实现存在多个容易在 .NET/PowerShell 重写时无声漂移的细节：

- `read-pages` 只允许“第一张请求页本身超 response budget”时返回 partial；后续页若放不下必须整页不返回并以 `next_page` 指向该页；
- `coverage_complete` 同时依赖 response truncation、parser text cap 与 partial-page 状态；
- search 是 literal、**非重叠**推进；
- ignore-case 同时 fold query 与 page text，并把 folded match 映回原始 Unicode code-point 坐标；
- 达到 `max_results+1` 后立即停止 dense tail，`matches_total_in_extracted_text=null`，只保留 `matches_seen_at_least`；
- result-list truncation 与 text-cap coverage gap 是两条独立证据轴；
- snippet 是经 whitespace normalization 的显示上下文，不是 authoritative offset source；
- source stat fence 失败要使整个 call 失败，不能保留部分已提取证据。

这些都属于 public evidence contract，不是 pypdf 私有实现细节。若仅要求“bounded read/search”，PowerShell 版完全可能采用 overlapping search、UTF-16 offset、精确 total 全扫、partial continuation 或不同 case-insensitive API 而仍看似满足功能表。

修订：在 v0.6 normative design 中新增 `Backend-independent behavioral compatibility contract`，逐项冻结上述 observable behavior，并要求 public `handle()` 与 PowerShell protocol fixture 双层测试。只有规范明确改变的行为才允许版本化迁移；backend-native UTF-16、overlap default、alternate continuation 或 host ignore-case 都不得漏到 public API。

### D8 修订后门禁

D8 有 finding，不计 clean。Gate A clean 计数再次从下一轮开始。

## 31. v0.6 Design Review D9｜parser-isolation/resource-boundary attack

从“把 parser 移出 frozen Python 后，安全/资源语义是否被过度承诺”重新审查 PowerShell/PdfPig seam。

**结论：仍未收敛。新增 1 个 material finding。**

### D9-01｜P1 security/resource truthfulness｜独立 PowerShell parser 仍不是内存或恶意文档 sandbox

v0.6 能解决的是 parser placement、可终止进程 ownership、stdout/time bounds 与 frozen stdlib closure；它没有给 PdfPig 进程加 Windows Job committed-memory limit、AppContainer、low-integrity token、VM/container 隔离。PdfPig 也可能在脚本有机会执行 returned-text cap 之前，为对象流、字体、页面内容等分配大量内存。因此：

- 512 MiB input cap 不是 peak-memory cap；
- 570 秒 deadline 不是内存限制；
- owned PowerShell child 可被 kill，不等于 hostile parser 已 sandbox；
- external Extension exact-tree approval 只覆盖代码/依赖信任，不覆盖恶意 PDF 的解析风险。

修订：v0.6 normative `Process/output boundary` 明确把这些限制称为 response/CPU-time bounds，增加 `parser_memory_sandbox=false`（或等价明确状态）要求，并要求 README/security text 继续声明 hostile/untrusted PDFs 应在 VM/container 级隔离环境打开。不得因为 parser 已移到独立进程就宣称恶意 PDF 安全。

### D9 修订后门禁

D9 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 32. v0.6 Design Review D10｜cross-backend page-provenance attack

从“render 已真正独立后，`search/read -> render -> image_open` 是否仍能保证同一页 provenance”重新攻击。

**结论：仍未收敛。新增 1 个 material finding。**

### D10-01｜P1 evidence correctness｜独立 renderer 不能默认与 PdfPig 页码天然等价

v0.6 正确地删除了 render 对 inspection backend 的硬依赖，但这同时删除了旧 0.5.1 在 render 调用中比较 text-backend page count 与 Windows renderer page count 的防线。对正常 PDF 两者通常一致；对病理/边界 PDF，不同 parser 对 page tree 的解释可能分歧。若 inspection 的 page N 与 renderer 的 page N 并非同一逻辑页，视觉核验会形成错误 provenance。

修订原则：不能为了恢复 check 又让 `render-pages` 必须启动 PdfPig，否则 inspection backend 一挂 visual capability 又会被连坐。改为证据工作流层的显式 alignment gate：

- `info/search/read-pages` 返回 Python capture 的 source SHA/bytes + PdfPig inspection `page_count`；
- `render-pages` 返回同一 Python capture 的 source SHA/bytes + Windows.Data.Pdf `source_units` + selected range；
- 把 render 用作 extracted page 的视觉验证前，必须比较 source identity 和 page count；
- source identity 不同或 `inspection page_count != renderer source_units` 时，标记 `page-alignment unresolved`，不得宣称该 render 已验证 extracted page；
- render 本身仍可在 inspection backend unavailable 时独立运行，不为这个 check 启动第三方 parser。

这保留 capability isolation，同时恢复证据 provenance 的 fail-closed 语义。

### D10 修订后门禁

D10 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 33. v0.6 Design Review D11｜malformed-Unicode evidence attack

从 .NET string/UTF-8 protocol 与 public Unicode code-point contract 的异常路径重新攻击。

**结论：仍未收敛。新增 1 个 material finding。**

### D11-01｜P1 evidence integrity｜未配对 UTF-16 surrogate 不能由 encoder 静默替换

.NET `string` 可以持有未配对 high/low surrogate；病理 PDF/字体映射理论上可能让 parser 暴露这种序列。若直接交给 UTF-8 encoder/JSON 输出，默认 fallback 可能变成 U+FFFD，从而发生未声明的文本变更，并使 `page_chars/char_offset/char_end` 与 search provenance 失真。

修订：在 scalar counting、casefold、snippet、JSON serialization 前验证所有即将公开的 evidence string 的 UTF-16 well-formedness：

- 正文页若存在 unpaired surrogate：该页以显式 extraction error/coverage gap 处理，不产生权威 read/search 文本；
- metadata/TOC 中受影响字段/标题：显式标 unavailable/error，并给 bounded diagnostic，不做静默 replacement；
- 合法 supplementary-plane surrogate pair 必须按单个 Unicode code point 计数；
- feasibility/protocol tests 同时覆盖有效 supplementary character 和故意构造的 unpaired surrogate。

这与 strict BOM-less UTF-8 protocol 一起确保“编码层不会偷偷改证据”。

### D11 修订后门禁

D11 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 34. v0.6 Design Review D12｜complete-backend-feasibility attack

从“Gate A probe 是否已经覆盖六动作真正需要的全部 parser primitive，而不是只证明正文能读”重新审查。

**结论：仍未收敛。新增 1 个 material finding。**

### D12-01｜P1 feasibility/spec completeness｜必须在 probe 阶段证明 metadata + outline + encrypted-fail-closed，而不是留到 production implementation

现有 feasibility 条目只明确要求 page count/geometry/text 与 `ContentOrderTextExtractor`。但 public API 还依赖：

- `info` 的 bounded document-information metadata；
- `outline`/`info` 的 bookmark/outline traversal；
- no-password contract 下 encrypted/password-required PDF 的明确失败。

如果这些能力直到 production runtime 重写时才首次被真实调用，一旦 PdfPig 0.1.16 的实际 API/asset/dependency 形态与预期不同，就会在 Gate A 之后才改变 backend/loader/spec，破坏两级门禁。

修订：feasibility probe 必须使用 deterministic fixtures/assembly metadata 实证完整 primitive set：page count、geometry、document information、bookmark/outline、ContentOrder text；另用 encrypted fixture 证明无 password 参数时 fail closed，不能意外以 empty/default password 打开。只有这些都通过，才能把 PdfPig 0.1.16 作为 Gate B 的 concrete backend。

### D12 修订后门禁

D12 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 35. v0.6 Design Review D13｜info/outline observable-contract attack

继续按 backend migration 的 public seam 逐项对照，检查 `info/outline` 是否像 read/search 一样已经冻结可观察语义。

**结论：仍未收敛。新增 1 个 material finding。**

### D13-01｜P1 correctness/compatibility｜`info/outline` 的单位、字段和 uncertainty semantics 也必须 backend-independent

现有 v0.5.1 已经形成稳定外部行为，但 v0.6 candidate 之前只概括为“metadata/outline/page geometry bounded”。迁移到 PdfPig 时容易出现以下静默漂移：

- `width_points/height_points` 被换成 backend-native 单位、DIPs 或 pixels；
- page/outline level 从 1-based 变成 0-based；
- metadata 字段集、missing-value 形状、4,096-char cap 与 `truncated_fields` 漂移；
- TOC title 512-char cap、max depth 15、`total=null` on incomplete traversal、`max_depth/max_items` truncation reasons 漂移；
- `info` sampled text 某页 extraction error 被错误当成“扫描件无文本”，把 `scan_candidate` 从 unknown 变成 true/false。

修订：扩充 `Backend-independent behavioral compatibility contract`，冻结：

- `info.sample_page_sizes` 最多 6 个按既有 first/last-inclusive evenly-distributed 规则采样，1-based page，points/user-space contract，3-decimal rounding；
- metadata 现有八个 document-info fields + `format/truncated_fields`，正常值 4,096 code-point cap；
- outline `{level,title,title_truncated,page}`、1-based level/page、512 title cap、depth 15、incomplete total=null 与分离 truncation reasons；
- text sample error => `text_sample_complete=false` + `scan_candidate=null`，不得把 parser failure 伪装成 scanned heuristic。

这些属于 public evidence contract，不能由 PdfPig 自己的默认 shape/units决定。

### D13 修订后门禁

D13 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 36. v0.6 Design Review D14｜Gate A Standards-first clean review

固定当前 v0.6 normative section，不继承此前 finding 集，按 Standards-first 重新检查：

- 六动作 public surface 与现有 input schema 是否保持；
- runtime permission 是否仍仅 `workspace.read / workspace.write / process.execute:powershell.exe`；
- parser placement 是否彻底脱离 frozen Python third-party import；
- NuGet/Unicode candidate-lock 与 Gate A/Gate B phase boundary；
- FolderBridge 256 files / 64 MiB approved-tree hard limit；
- package-owned assembly collision/GAC/global substitution、platform runtime边界；
- metadata/outline/geometry/read/search 的 backend-independent output semantics；
- Unicode full casefold、code-point offsets、malformed surrogate、UTF-8 protocol；
- 600s host / 570s inspector cancel-timeout ownership；
- response/time bounds 与 parser-memory non-sandbox truthfulness；
- Windows.Data.Pdf render independence、fresh output/completion-marker语义与 cross-backend page alignment；
- source SHA/stat fence、document-supplied trust note、OCR/non-goal boundary；
- feasibility criteria 是否覆盖 metadata/bookmarks/ContentOrder/encryption/collision/tamper/Unicode/bounds/cancel。

同时 fresh 对 PdfPig 官方资料复核：`DocumentInformation` 公开 metadata 字段为 string/nullable；`PdfDocument.TryGetBookmarks(out Bookmarks)` 是正式 bookmarks API；`ContentOrderTextExtractor` 仍是官方推荐 reading-order text 路径。没有发现设计要求依赖不存在的 public primitive。

**结论：CLEAN。0 个新增 material finding。**

历史 v0.5 段已明确标记为 failed/non-normative，Ottawa acceptance 与 current status contract 不再引用 pypdf。没有发现需要新增 executable、permission、network、machine-wide config/GAC mutation、OCR 或 public action 的理由。

D14 计为 Gate A 第 1 个连续 clean review。

## 37. v0.6 Design Review D15｜Unicode-version drift counterexample review

独立反例轮重点攻击：不同 Windows culture、supplementary characters、Unicode casefold 版本变化、global assembly collision、lower .NET host、encrypted PDF、parser unavailable but renderer available、source/page-count mismatch。

**结论：未收敛。新增 1 个 material finding，D14 的 clean 连续计数失效。**

### D15-01｜P1 compatibility｜不能一边承诺 v0.5.1 casefold parity，一边预先升级到 Unicode 17.0

当前 normative contract 已要求 backend migration 保持 v0.5.1 的 Python `str.casefold()` observable semantics；但之前又把 Extension-owned fold dataset 的“current candidate”写死成 Unicode 17.0。若当前 FolderBridge Python runtime 使用较早 Unicode database，则极少数字符的 casefold 映射可能随 backend migration 被无意升级，违反“不因换 backend 改 public semantics”。

修订：

- Gate A feasibility 先记录当前 v0.5.1 Python Unicode-data/casefold baseline；
- 从正式 Unicode release 中选择与该 baseline 匹配的 `CaseFolding.txt`，不预先追 newest；
- 从该文件生成 fixed default full-fold table，并用由 CaseFolding 数据派生的 equivalence corpus 对当前 Python `str.casefold()` 与 PowerShell table做行为对撞；
- source version/SHA、generated asset SHA、Unicode License 写入 Gate B lock/provenance；
- 未来若主动升级 Unicode fold version，视为 public semantic migration，需要独立 spec review，而不是 backend replacement 的附带变化。

### D15 修订后门禁

D15 有 finding；Gate A clean 计数重新归零。下一轮起必须重新取得连续两轮 CLEAN。

## 38. v0.6 Design Review D16｜geometry/unit/locale counterexample review

继续针对 backend-native value semantics 攻击 `info`，并用 PdfPig 当前官方源码/发布说明核对页面几何 API。

**结论：仍未收敛。新增 1 个 material finding。**

### D16-01｜P1 compatibility/evidence｜不能把 PdfPig `Page.Width/Height` 当成现行 pypdf MediaBox geometry；format 也不能 locale drift

当前 0.5.1 `_page_size()` 读取 pypdf `page.mediabox.width/height`。PdfPig 官方当前 `Page` 同时暴露 `MediaBox` 与 `CropBox`，而 `Page.Width/Height` 明确从 CropBox visible bounds + rotation 计算；0.1.15 还专门调整过该行为。因此对 cropped/rotated PDF，直接迁移到 `Page.Width/Height` 会改变 `sample_page_sizes` 的语义。

另一个同类风险是 `document.Version` 是数值；若 PowerShell/CLR 直接使用当前 culture `ToString()`，部分 locale 可能产生 `1,7` 而现行 `metadata.format` 是 `PDF-1.7` 形状。

修订：

- v0.6 明确 `sample_page_sizes.width_points/height_points` 使用 `Page.MediaBox`，保持 pypdf mediabox contract，1-based page + 3-decimal rounding；
- 不允许用 PdfPig CropBox-visible `Width/Height` 替代；
- `metadata.format` 固定 culture-invariant `PDF-<major.minor>`；
- feasibility fixture 加 cropped/rotated page，对撞 MediaBox 与 Width/Height，确保 production seam选的是前者。

### D16 修订后门禁

D16 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 39. v0.6 Design Review D17｜Gate A architecture-invariant clean review

从“probe 后是否仍可能被迫改变架构不变量”而不是逐字段补丁的角度，重新独立审查当前 normative v0.6：

- public surface/schema 保持六动作；
- inspection third-party parser 完全移出 frozen Python；
- runtime direct process permission仍只允许 fixed `powershell.exe` seam；
- candidate dependency版本可在 probe 依据证据调整，但不得新增 executable/permission/network/machine-wide config；
- package source/integrity/DLL set/loader strategy/Unicode fold version都被明确推迟到 Gate A feasibility，并必须在 Gate B lock 后再次两轮审计；
- loader trust要求 package-owned collision/GAC/global substitution fail closed；
- platform .NET Framework assembly trust与 vendored package trust分离；
- public geometry/metadata/outline/read/search/Unicode/coverage semantics已 backend-independent；
- parser-memory non-sandbox truthfulness、source stat fence、encrypted fail-closed均显式；
- render capability独立且 visual alignment另有 evidence gate，不会因 inspection unavailable 被连坐；
- host extension 256-file/64-MiB hard limit 与 600s/7200s action ceilings已有对应设计边界；
- feasibility criteria已覆盖完整 parser primitive、ContentOrder、cropped/rotated MediaBox、metadata culture、encryption、assembly collision/tamper、Unicode parity/malformed surrogate、bounds/cancel。

复核当前 PdfPig 官方资料，没有发现这些架构要求依赖不存在的 public API：`PdfDocument.Information`、`TryGetBookmarks`、`Page.MediaBox`、`ContentOrderTextExtractor`均有正式公开 surface；其具体 0.1.16 nupkg asset/loader feasibility仍按 Gate A probe 实证，不在本轮假定为已通过。

**结论：CLEAN。0 个新增 material finding。**

D17 计为 Gate A 第 1 个连续 clean review。

## 40. v0.6 Design Review D18｜pipe-backpressure/process-liveness counterexample review

第二轮反例改从 owned child I/O 生命周期攻击：大正文响应、过量 stdout/stderr、PowerShell child 在 pipe 满后阻塞、host cancel/timeout同时发生。

**结论：未收敛。新增 1 个 material finding，D17 的 clean 连续计数失效。**

### D18-01｜P1 reliability/resource｜stdout/stderr cap 必须运行中并发 drain，不能等 child exit 后才检查

inspection response 可合法达到数十万字符；Windows anonymous pipe 容量远小于 public response ceiling。如果 Python supervisor 只 `poll()` 等 PowerShell 结束而不持续读取 stdout/stderr，child 可能在写满 pipe 后阻塞，形成 parent 等 exit / child 等 reader 的死锁。即使最终做 `len(stdout) > limit` 检查，也无法避免该死锁；若使用无界 `communicate()` 缓存，又可能先在 Python 内存中吸收过量输出才发现 cap。

修订：

- inspector runner 必须在 child 运行期间并发 drain stdout/stderr；
- plugin-local capture 各自有 hard byte ceiling，不依赖 FolderBridge core private helper；
- 任一流超 cap 立即停止保留更多 bytes、终止 owned PowerShell process tree、bounded join/drain并返回 `PDF_INSPECT_PROTOCOL_TOO_LARGE`；
- cancel/570s timeout 与 output-overflow共用同一 ownership/termination路径，避免双重 release；
- feasibility 加故意产生超过 pipe capacity/协议 cap 的 stdout 与 stderr fixture，证明 supervisor 不死锁且内存捕获保持 bounded。

### D18 修订后门禁

D18 有 finding；Gate A clean 计数重新归零。下一轮起重新取得连续两轮 CLEAN。

## 41. v0.6 Design Review D19｜legal-max-response/cap compatibility attack

在并发 bounded capture 修订后，继续检查“防超量输出”的 cap 是否会误杀合法 public maximum。

**结论：仍未收敛。新增 1 个 material finding。**

### D19-01｜P1 reliability/contract｜不能复用 renderer 1 MiB stdout cap 处理 500k-code-point inspection response

当前 0.5.1 `POWERSHELL_STDOUT_LIMIT` 为 1 MiB，适合只返回小 JSON 文件列表的 renderer；v0.6 inspection 的合法 `read-pages.max_chars` 上限是 500,000 Unicode code points。500k CJK UTF-8 已超过 1 MiB，supplementary/control-character JSON encoding 还可能更大。如果实现者机械复用现有常量，会让 public schema 允许的合法请求稳定失败。

修订：

- inspection 与 renderer 使用分离的 I/O cap；
- `INSPECT_STDOUT_LIMIT = 8 MiB`，`INSPECT_STDERR_LIMIT = 256 KiB`；
- 8 MiB 仍显著低于 FolderBridge `MAX_WORKER_RESPONSE_BYTES = 32 MiB`；
- feasibility 必须序列化 maximum-legal 500k-code-point fixtures（多字节、supplementary、JSON control escaping），证明合法 envelope低于 8 MiB并完整 round-trip；
- 另用 >8 MiB stdout / >256 KiB stderr fixture证明 concurrent capture及时 kill并返回 protocol-too-large。

### D19 修订后门禁

D19 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 42. v0.6 Design Review D20｜Windows command-line/input-protocol counterexample review

从 arbitrary literal query / Unicode / Windows argv-PowerShell binder 边界攻击 inspector request seam。

**结论：仍未收敛。新增 1 个 material finding。**

### D20-01｜P1 correctness/security｜caller data 不应穿过 PowerShell command-line parameter binder

若 `query/path/range` 作为 `powershell.exe -File pdf_inspect.ps1 -Query <value>` 等 argv 传递，会额外引入 Windows command-line 与 PowerShell parameter-binding 语义：leading `-`、引号、控制字符、supplementary text 都需要额外 quoting 证明；尤其 NUL 无法作为 Windows command-line argument表示，而现有 Python literal query 并没有“禁止 NUL”这一 public contract。即使 shell=false，也会造成 backend migration 的数据语义漂移。

修订为单一 data protocol：

- PowerShell argv 只含 fixed executable flags + approved `pdf_inspect.ps1` path；
- action/path/query/range 全部作为一个 BOM-less UTF-8 JSON object 从 stdin发送；
- request hard cap 64 KiB，Python write 后立即 close stdin；
- PowerShell用 strict UTF-8 读取 raw standard input，exactly-one-object parse，拒绝 invalid UTF-8、unknown fields、protocol/type/range mismatch；
- request values永不 `Invoke-Expression`、scriptblock parse或进入 assembly/script/executable选择；
- feasibility 覆盖 leading dash、quotes、control chars、embedded NUL、supplementary characters以及 malformed/oversized request。

这同时减少 injection surface 并保持现有 literal-search 数据域。

### D20 修订后门禁

D20 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 43. v0.6 Design Review D21｜Gate A full-surface clean review

从当前唯一 normative v0.6 重新做 Standards + Spec 双轴 review，并先做文档一致性扫描：旧 pypdf backend只存在于明确 historical failure record；current security contract 已指向 fixed inspector+renderer；inspector caller data只走 stdin JSON；不存在 Unicode 17.0 预锁、renderer 1MiB cap复用或 PdfPig Width/Height 误用的 current 指令。

重新穿透：public schema、path confinement、permissions、NuGet/Unicode candidate lock、extension tree limits、platform/assembly trust、complete parser primitive、metadata/outline/geometry semantics、read/search casefold/offset/continuation、malformed Unicode、stdin/stdout strict UTF-8、concurrent bounded capture、legal max response、timeout/cancel、parser memory truthfulness、encrypted fail-closed、render isolation、cross-backend page alignment、source identity与 Gate A/B phase boundary。

**结论：CLEAN。0 个新增 material finding。**

没有发现需要扩大权限/可执行文件、降低 provenance、修改 public action surface、恢复 parser-to-render coupling 或在 Gate A 前预锁未经 probe 的 loader/package事实。

D21 计为 Gate A 第 1 个连续 clean review。

## 44. v0.6 Design Review D22｜error-policy compatibility counterexample review

第二轮 clean-room 反例专门比较异常路径，而不是正常输出：selected-page text extraction failure、malformed surrogate、metadata/TOC invalid string 与 info sample uncertainty。

**结论：未收敛。新增 1 个 material finding，D21 clean 连续计数失效。**

### D22-01｜P1 compatibility/evidence｜不能在 backend migration 中把 read/search failure改成“跳过坏页继续”

前一版 v0.6 malformed-Unicode 文案写成“正文页 per-page extraction error/coverage gap”，这会暗示 `read-pages/search` 可以跳过坏页继续返回部分证据。但现行 0.5.1 行为是：

- `read-pages/search` 的 selected-page extraction exception 会传播并使整个 action失败；
- 只有 `info` text sample 已有 per-page error collection，sample failure => `text_sample_complete=false`、`scan_candidate=null`；
- metadata/TOC 没有现成的 field-level error shape。

如果新 backend 在 search 中跳过失败页，即使标 `coverage_complete=false`，也会把原本 fail-closed 的证据调用改成 partial-success，属于未授权语义变化。

修订：

- `read-pages/search` 遇到 malformed UTF-16 或 selected-page extraction failure 整次 action明确失败，不返回部分 matches/pages；
- `info` 仅在既有 text sample seam保留 per-page uncertainty；
- malformed metadata/TOC string使对应 `info/outline` action显式失败，不新增临时 field-level output schema；
- UTF-8 encoder仍不得静默 U+FFFD replacement。

### D22 修订后门禁

D22 有 finding；Gate A clean 计数重新归零。下一轮起重新取得连续两轮 CLEAN。

## 45. v0.6 Design Review D23｜parser-output-equivalence scope attack

从“backend-independent compatibility”这一措辞本身重新攻击，区分可冻结的算法/协议语义与无法合理承诺的 parser-derived text bytes。

**结论：仍未收敛。新增 1 个 material finding。**

### D23-01｜P1 spec truthfulness｜不能承诺 pypdf 与 PdfPig/ContentOrder 的抽取正文 byte-for-byte 等价

v0.6 有意更换 parser，且明确要求 PdfPig `ContentOrderTextExtractor` 而不是 raw content order；因此空白、换行、reading order、甚至某些 glyph mapping 的提取结果可能与 pypdf 不同。若“observable semantics preserved”被解读为旧 pypdf extracted-text golden bytes必须完全复刻，实现者可能为了过测试而做危险的字符串后处理，反而污染真实 PDF 证据。

修订：

- compatibility 明确限定为 policy/coordinate/control semantics：schema、page provenance、bounds、continuation、literal search algorithm、casefold/offset units、coverage/truncation、error/source-fence semantics；
- 不承诺 parser-derived正文 byte-for-byte相同；
- v0.6 实际正文来自 locked PdfPig `ContentOrderTextExtractor`，仍标 document-supplied/untrusted；
- real-PDF acceptance负责验证 substantive text；若 backend difference改变规则句、术语定位或 page alignment，必须 `render-pages -> image_open` 核对，而不是 compatibility layer把新文本静默改成旧 pypdf输出。

### D23 修订后门禁

D23 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 46. v0.6 Design Review D24｜literal-query validation compatibility attack

在 parser text equivalence边界明确后，重新对照 0.5.1 literal-query validation。

**结论：仍未收敛。新增 1 个 P2 material compatibility finding。**

### D24-01｜P2 compatibility｜whitespace-only query 要拒绝，但不能把合法 query trim 后再搜

现行 `_search()` 使用 `if not query.strip(): reject` 仅做 pure-whitespace validation；随后 needle仍来自原始 `query`。因此：

- `"   "` 必须拒绝；
- `" definition "` 是合法 literal needle，前后空格属于匹配内容；
- backend migration若在 PowerShell端对所有 query `.Trim()` 后搜索，会改变 observable literal-search semantics。

修订：backend-independent contract明确“whitespace-only validation without normalization”：只用于判空，不改变原始 query；casefold也作用于原始已验证 query。stdin JSON protocol保留 leading/trailing spaces/NUL/control data。

### D24 修订后门禁

D24 有 finding，不计 clean。Gate A clean 计数从下一轮重新开始。

## 47. v0.6 Design Review D25｜Gate A final Standards-first clean review

固定 D24 后的 normative v0.6，从零按 Standards-first + phase-boundary review：public action/input contract、workspace/path confinement、process permission、parser placement、candidate supply chain、assembly trust、host extension limits、full parser primitive、all public evidence fields/units/error semantics、Unicode/casefold/query semantics、strict stdin/stdout protocols、bounded concurrent capture、legal-max envelope、timeout/cancel ownership、memory non-sandbox disclosure、encrypted fail-closed、independent renderer + alignment gate、source identity、Gate A/B release conditions。

特别复核了此前反复出现的“新 backend 顺手改语义”风险：当前 spec 已明确区分 parser-derived text content（允许因 ContentOrder backend不同而改变）与必须保持的 policy/coordinate/control semantics；whitespace-only query validation、MediaBox geometry、culture-invariant format、read/search whole-call error behavior均已显式冻结。

**结论：CLEAN。0 个新增 material finding。**

没有剩余架构事实需要在 production implementation 中临时决定；尚未锁定的 exact package SHA/DLL set/Unicode baseline/loader strategy都明确属于 Gate A feasibility输出，并受 Gate B 二次两轮审计约束。

D25 计为 Gate A 第 1 个连续 clean review。

## 48. v0.6 Design Review D26｜Gate A final independent counterexample review

第二轮不沿用 D25 的 standards 顺序，改用 failure matrix 独立攻击：

- host 只有较低 .NET Framework / PowerShell runtime；
- official candidate package hash或identity不匹配、NuGet bytes变化；
- vendored DLL缺失/篡改/未知额外 DLL、package-owned identity 已从 GAC/global预载；
- candidate exact version不含预期 TFM/ContentOrder type、metadata/bookmark API不可用；
- encrypted/password-required PDF；
- cropped/rotated MediaBox与 visible Width/Height分歧；
- current Python Unicode baseline不是最新 Unicode release、Turkish/culture变化、ß/Greek sigma/supplementary mapping、unpaired surrogate；
- whitespace-only / leading-dash / quoted / NUL query；
- 500k legal response、>8MiB stdout、>256KiB stderr、pipe backpressure；
- pre-start cancel、active cancel、hung parser、570s inner timeout、outer host shutdown；
- parser超时/不可用但 Windows.Data.Pdf renderer仍可独立运行；
- PdfPig与Windows.Data.Pdf page count不一致；
- source在调用中变化；
- render output dir已存在、mid-render failure、hard crash留下无 completion marker目录；
- pathological PDF memory膨胀与 external Extension非 OS sandbox边界。

逐项结果都已有 Gate A fail-closed、bounded、explicit uncertainty 或 Gate B/probe acceptance路径；没有案例要求新增 executable/permission、放宽 provenance、machine-wide GAC/config、恢复 pypdf/frozen import 或改变 public action surface。

**结论：CLEAN。0 个新增 material finding。**

### v0.6 GATE A｜ARCHITECTURE DESIGN CONVERGENCE PASS

- D24：1 finding -> 修订；
- **D25：0 new material findings；**
- **D26：0 new material findings。**

连续两轮 Gate A clean 已达到。现在**仅允许**进入 temporary/research candidate-fetch + feasibility probe；仍禁止 production `folderbridge-extension.json / plugin.py / install.ps1 / pdf_inspect.ps1` 实现修改。Probe 完成并写回 exact hashes/DLL set/Unicode baseline/loader evidence后，必须通过 Gate B Final Locked v0.6 Spec 的连续两轮 clean，才允许 production TDD implementation。

## 49. v0.6 Feasibility execution｜PASS

Gate A 后仅在 `local-private/pdf-toolkit-v06-feasibility/` 施工 temporary/research probe；未修改 production PDF Toolkit runtime/manifest/installer。本轮最终 full probe fresh reacquire official NuGet registration/package bytes、Unicode baseline、CLR loader、PdfPig primitives、protocol/process supervision、collision/tamper/missing-vendor fail-closed 与 tree budget，`progress.json` 到达 `probe_pass`。

最终 full feasibility evidence：

- `result.json` SHA-256 `59ebbad6d384d36e17f2979d88c89aa8cd6bcbdd01e47faa418e6a211107c59d`；
- `overall_pass=true`；
- Windows PowerShell 5.1；.NET Framework release `533509`；
- `Assembly.LoadFrom` 与 byte-load research comparison 均可执行 PdfPig，但生产策略锁定 `LoadFrom`；
- PdfPig 0.1.16 net471 + exact transitive package set加载成功；
- `ContentOrderTextExtractor.GetText(Page,bool)`、metadata、bookmarks、PDF version、MediaBox、encrypted fail-closed全部通过；
- package-owned outside-vendor collision、tamper、missing vendored DLL + outside identical preload全部 fail-closed；
- exact metadata-derived redirects只出现 `System.Memory 4.0.2.0 -> 4.0.5.0` 与 `System.Buffers 4.0.4.0 -> 4.0.5.0`；
- strict UTF-8 stdin/stdout 的 Chinese/accent/ß/control/NUL/supplementary round-trip通过；
- stdout/stderr overflow、timeout、cancel/owned process tree通过；
- Unicode baseline锁为 14.0.0，full C/F generated mapping 1,530 entries；
- measured payload 17 files / 6,143,201 bytes；加 production reserve 后 projection 49 files / 6,667,489 bytes，低于 host 256 files / 67,108,864 bytes。

全仓测试中 temporary feasibility test 自身 `ok`；唯一失败仍是既有无关 `test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body`：`6001 > 5000`，未修改 core。每次 Job 真正结束后 temporary test bridge 均恢复，最终 `tests/test_allow_tasks_regression.py` 回到原 SHA `4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`。

**结论：v0.6 feasibility PASS。** 仅授权写 Final Locked Spec + Gate B review；仍不授权 production implementation、用户重装、runtime PASS、GH 或 DUG。

## 50. v0.6 Gate B Review D27｜license-attribution + implementation-freedom attack

第一轮 Gate B 不复用 Gate A failure matrix，而从“另一个实现者只拿 Final Locked Spec 是否能无猜测实现 exact candidate”反向审查 package/TFM/DLL/license/loader lock。

**结论：未收敛。新增 1 个 material finding，并同步消除 1 个实现歧义；clean 计数=0。**

### D27-01｜P1 redistribution provenance｜SPDX MIT template 不能单独替代 package copyright notice

增强 feasibility evidence 已锁定 MIT SPDX text SHA，但直接读取该文本发现其版权行仍是 `Copyright (c) <year> <copyright holders>` 模板。若仅把该共享文本当作五个 Microsoft MIT 包的完整 license payload，会丢失 MIT 要求保留的实际 copyright notice。

修订方法没有重新信任网络：从**已通过 full feasibility nupkg SHA-256 lock 的本地 nupkg bytes**直接解析 nuspec metadata，生成 `local-private/pdf-toolkit-v06-feasibility/package-metadata.json`，SHA-256 `2a0b1f502210009eb6474bee2ed3720d5aaf25c136154fd67db8e1487ccf26f4`。证据为：

- 五个 MIT dependency packages 均 `authors=Microsoft`，copyright 精确为 `© Microsoft Corporation. All rights reserved.`；repository 都是 `https://github.com/dotnet/maintenance-packages`，各 package commit 已写入 Final Locked Spec；
- PdfPig 0.1.16 `authors=UglyToad`、Apache-2.0、repo commit `a7bb35662bbbf405efddad50aedc9bcdcf515afc`，nuspec无 copyright 字段；
- 所有 metadata 都来自与 full result 中 nupkg SHA 完全一致的 cached package；metadata probe test `ok`；全仓仍只有既有 6001>5000 failure；temporary test bridge再次恢复原 SHA。

Final Locked Spec 已修订：共享 license text只负责固定 permission/license body；NOTICE/provenance还必须保存 exact package attribution，五个 MIT packages必须带精确 Microsoft copyright notice，不能原样把 SPDX placeholder 当作 package notice。

### D27-02｜non-material clarity fix｜PdfPig 内部 DLL load order 也必须唯一

原锁只写到 `... -> PdfPig DLL set`，虽然 probe 使用排序后的 deterministic set，但实现者仍可能自行重排七颗 PdfPig DLL。为消除实现自由度，Final Locked Spec 已把十二颗 DLL 的完整顺序逐颗冻结。此项本身未形成新的 architecture/security counterexample，但与 D27-01 一并修订。

### D27 修订后门禁

D27 有 material finding，因此 Gate B clean 计数保持 0。后续必须基于修订后的 `docs/pdf-toolkit-external-extension-design-20260903.md` 重新取得两轮连续 `0 new material findings`；D27 不计 clean。

## 51. v0.6 Gate B Review D28｜culture-format + runtime-casefold evidence attack

D27 修订后，从实现者视角重新核对 Gate A 明文要求与现有 feasibility 输出，不沿用“设计上应该如此”的推断，只接受已执行的 Windows PowerShell 5.1 evidence。

**结论：仍未收敛。新增 1 个 material evidence finding。**

### D28-01｜P1 evidence completeness｜culture-invariant PDF version 与 PowerShell casefold independence 尚未实际执行证明

原 feasibility 已证明 PdfPig fixture 的 `doc.Version == 1.7`，也已从 Unicode 14.0.0 `CaseFolding.txt` 生成 deterministic C/F mapping；但这两项仍缺少 Gate A 明文要求的运行态反例证明：

- 没有在非英文 / decimal-comma culture 下实际执行 `PDF-<major.minor>` formatter，因此 `PDF-1.7` 仍只是实现要求，不是 probe evidence；
- 没有让 Windows PowerShell 真正用生成映射在不同 culture 下跑同一 fold corpus，因此“host ignore-case / CurrentCulture 不参与语义”仍未被运行态证明。

修订／补证据：

- `local-private/pdf-toolkit-v06-feasibility/gate-b-semantics.json` 固化 targeted closure evidence，SHA-256 `8e37c7275573d44a8c3aec97b31d3205fdae14841a51387d146c4e84f8afb103`；
- 同一 Windows PowerShell 5.1 / locked DLL set 下把 `CurrentCulture` 设为 `de-DE`，明确以 invariant culture 格式化 PdfPig `doc.Version`，结果严格为 `PDF-1.7`，从而排除 `PDF-1,7` 漂移；
- Windows PowerShell 使用 locked Unicode 14.0.0 generated map，在包含全部 1,530 个 C/F mapping key + supplementary/default stress fixtures 的 1,544-code-point corpus 上实际执行 fold；
- `en-US` 与 `tr-TR` 两个 culture 均输出 1,666 code points，UTF-8 folded bytes SHA-256 均为 `fa5b0d68bd03308001ce5aba87a14d21988c3ba7d989463b3cbc5e283a321711`，并与 Python/Unicode expected corpus hash完全相同；
- 临时 test bridge 对该 targeted evidence 为 `ok`，全仓仍只有既有无关 `6001 > 5000` failure；桥已恢复原 SHA `4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`；
- Final Locked Spec 已写回上述运行态事实，不再把 culture/casefold independence 仅作为尚待实现的声明。

### D28 修订后门禁

D28 有 material finding，因此 Gate B clean 计数继续为 0。下一轮必须从修订后的 Final Locked Spec 重新独立审查；D28 不计 clean。

## 52. v0.6 Gate B Review D29｜locked supply-chain / CLR / provenance consistency review

基于 D28 修订后的 Final Locked Spec，从另一个实现者视角独立核对 package/TFM dependency、vendored DLL set、assembly identity、load order、redirect whitelist、license attribution、Unicode/culture runtime evidence、tree budget 与 production installer obligations 是否存在互相冲突或仍需临时决策的空白。

**结论：CLEAN。0 个新增 material finding。**

重点复核结果：

- `PdfPig 0.1.16 net471` 的 selected dependency group 只含 `Microsoft.Bcl.HashCode 6.0.0` 与 `System.Memory 4.6.0`；`System.ValueTuple 4.5.0` 只属于 net462 group，已明确不进入 v0.6 vendored set；
- 十二颗 DLL 的 SHA-256、assembly identity 与逐颗 deterministic load order均已唯一冻结；production 不存在“自行选择 PdfPig DLL 顺序”的自由度；
- `Assembly.LoadFrom` 是唯一 production loader；研究用 `Assembly.Load(bytes)` 不构成 fallback；
- 两个强名版本 redirect 以 exact requested FullName -> exact approved FullName 冻结；其他 package-owned resolution request 全部 fail closed；
- outside-vendor collision、one-byte tamper、missing-vendor + identical external copy 三类反例均已有 probe PASS，且 spec 不允许 GAC/PATH/current-directory/global package lookup；
- D27 license finding 已闭合：五个 MIT 包的 exact Microsoft copyright notice 与 canonical MIT permission text必须成对保留；PdfPig author/repository/commit/Apache-2.0 attribution亦已锁；
- D28 evidence finding 已闭合：`de-DE` 下 invariant `PDF-1.7` 与 `en-US`/`tr-TR` PowerShell full-fold corpus同 hash 已写入锁定事实；
- measured feasibility payload 与 production reserve被明确区分；installer仍必须对真实 staged tree做最终 256-file / 64-MiB gate，不能把 reserve 当成免检额度；
- 上述具体 Gate B lock 与前文 candidate wording不存在可改变 package/loader/Unicode事实的冲突；任何 lock 变动均明确要求 reopen Gate B。

D29 计为 Gate B 第 1 个连续 clean review。仍禁止 production TDD，必须再取得一轮独立 clean。

## 53. v0.6 Gate B Review D30｜failure-matrix + normative-precedence attack

第二轮不复用 D29 的 supply-chain 顺序，而从 unsupported runtime、preloaded assembly、binding miss、malformed Unicode、dense search、oversized protocol、cancel/timeout、parser/render independence、page-count mismatch、source mutation、render transaction、encrypted PDF、tree-budget 与 redistribution provenance逐项攻击，并专门检查 Gate A 时期的“candidate flexibility”是否仍残留在 Final Locked normative text 中。

**结论：未收敛。新增 1 个 material implementation-ambiguity finding。**

### D30-01｜P1 Final-Lock contradiction｜前置 Assembly contract 仍允许 loader strategy 自由度，与 Gate B `LoadFrom only` 冲突

Final Locked Spec 后部已经明确冻结：production loader 为 `Assembly.LoadFrom`，research `Assembly.Load(bytes)` 不得成为 undocumented fallback；但同一 v0.6 normative section 的前置 Assembly-loading contract 仍保留 Gate A 阶段句子：

`the design does not pre-freeze Assembly.LoadFrom as the only acceptable API`

并继续描述 byte-loaded/no-context strategy 的等价 invariant。即使后部 concrete lock 足以让审阅者推断最终答案，这种同一 normative section 内的相反措辞仍允许实现者保留 byte-load fallback，违背 Final Locked Spec“无需临时设计决策”的目标。

修订：

- 前置 loader contract 现在直接写死 `Assembly.LoadFrom only`；
- 十二 DLL 必须按 Gate B frozen order 从 provenance-declared approved path逐颗加载；
- 每颗加载后必须验证 `FullName + Location`，Location 必须位于 `_vendor-dotnet/` 并对应已 hash-verified path；
- `Assembly.Load(bytes)` / no-context 仅保留为 research evidence，不得进入 production fallback；
- bounded `AssemblyResolve` 只能在十二 DLL 全部 verify/load 后注册，且只允许 Gate B 已冻结的两个 exact FullName redirect；其余 package-owned dependency request fail closed。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `8a50c7afdb35b3d71a1569b296fbf8d72b6a2a57b3c19468c05bd649ca0d4a6f`。

### D30 修订后门禁

D30 有 material finding，因此 D29 的单个 clean 不再形成连续收敛；Gate B clean 计数重置为 0。必须从 D30 修订后的 Final Locked Spec 再取得连续两轮 clean，production TDD 仍禁止。

## 54. v0.6 Gate B Review D31｜pre-Gate-B wording residue sweep

从 D30 修订后的完整 v0.6 normative section 搜索 `candidate / fallback / strategy / choose / may` 等可能让 production 实现者误以为 package/version/loader/Unicode/permission仍可自行选择的残留措辞，并逐项按后部 concrete lock 的确定性复核。

**结论：CLEAN。0 个新增 material finding。**

复核结果：

- loader 路径现在只有 `Assembly.LoadFrom`；`Assembly.Load(bytes)` 仅被明确标注为 research evidence 且不得成为 fallback；
- 仍存在的 candidate wording均属于 Gate A feasibility历史条件、候选页/scan heuristic或 Ottawa official-identity uncertainty，不再赋予 production 改变已锁 package/hash/TFM/DLL/Unicode/redirect 的权力；
- package exact versions、NuGet hashes、selected TFM、dependency groups与十二 DLL inventory均由 Gate B concrete lock 唯一化；任何变化都明确 reopen Gate B；
- Unicode 14.0.0 mapping/version/hash与 PowerShell runtime corpus evidence已固定，不允许因 OS/culture/newer Unicode版本漂移；
- manifest permission set仍唯一为 `workspace.read / workspace.write / process.execute:powershell.exe`；无第二 executable/network/OCR路径；
- license/NOTICE wording虽引用“required payload”，但 Gate B 后部已具体解析为 two shared canonical license texts + package-level attribution + Unicode license，且禁止 silently substitute/omit；不存在许可实现分叉。

D31 计为 Gate B 第 1 个连续 clean review。production TDD 仍禁止，必须再取得一轮独立 clean。

## 55. v0.6 Gate B Review D32｜Gate-A checklist-to-executed-evidence attack

D31 后不再只审“规范是否自洽”，而把 Gate A feasibility section 的 14 项逐条对照 `local-private/pdf-toolkit-v06-feasibility/probe.py` 与当时 `result.json`，要求每个明列 fixture 都必须在同一次权威 `overall_pass` 中实际执行；仅有设计声明、旧 Python 单测或分散 targeted evidence均不算完成。

**结论：未收敛。新增 1 个 material feasibility-completeness finding。**

### D32-01｜P0 gate truthfulness｜旧 `overall_pass=true` 没有覆盖 Gate A 明列的完整 fixture 集

逐项对照发现，旧权威 result 虽已证明 package integrity、loader、PdfPig primitive、encrypted fail-closed、collision/tamper/missing-vendor、Unicode dataset/culture、UTF-8 roundtrip、stdout/stderr overflow、active cancel/timeout 等，但当时 probe 源码没有实际执行以下 Gate A 明列项：

- parser-side bounded `read-pages/search` response/coverage/result-cap semantics；
- malformed JSON / invalid UTF-8 / >64 KiB / extra-field / invalid bounded-field stdin 在 parser use 前 fail closed；
- pre-start cancel（旧 probe 只有 active cancel + timeout）；
- deliberately unpaired surrogate fail-closed；
- `Straße -> STRASSE` expansion 下原始 Unicode code-point offset mapping，且 match 前有 supplementary-plane scalar；
- non-empty literal query 前后空格不得 trim 的同 PowerShell protocol evidence。

因此旧 `result.json` SHA `59ebbad6d384d36e17f2979d88c89aa8cd6bcbdd01e47faa418e6a211107c59d` 不能继续作为“Gate A 14 项完整 feasibility PASS”的权威证据；D31 的一个 clean 也不能继续累计。

### D32 closure implementation｜temporary probe only

只增强 `local-private/pdf-toolkit-v06-feasibility/probe.py`，没有修改 production `folderbridge-extension.json / plugin.py / install.ps1 / pdf_inspect.ps1`：

- `run_ps_bounded` 新增 raw-payload test route 与 `pre_cancel` fixture；pre-start cancel在 Popen 前返回 `reason=cancel, spawned=false`；
- 新增 strict Windows PowerShell 5.1 Gate-A contract harness，request parser使用 strict BOM-less UTF-8、exact field whitelist、protocol/type/range validation，并记录 invalid request 是否已触及 semantic/parser stage；
- 新增 read whole-page boundary、first-page partial、1,000,000-code-point page cap；
- 新增 locked Unicode map casefold search：`😀Straße Straße` + `STRASSE`，`max_results=1`，首个原文坐标严格为 `char_offset=1 / char_end=7`，第二匹配只用于 cap+one，得到 `results_truncated=true / search_window_complete=false / matches_total_in_extracted_text=null / matches_seen_at_least=2`；
- 新增 literal `" definition "` 前后空格保留 fixture，原文坐标 `1..13`；
- 新增 deliberately unpaired high surrogate -> `invalid-unicode:unpaired-high-surrogate`；
- invalid/oversized/extra-field request全部记录 `parser_touched=false`。

closure harness 第一次运行暴露 temporary PowerShell probe 自身数组封装 bug：`Scalar-Items` 的 `return ,$out.ToArray()` 把 scalar objects 包成 nested `Object[]`，导致 fold fixture `System.Object[] -> System.Int32`。该问题只存在 local-private harness；去掉 unary comma 后同 closure test PASS，不构成 production architecture finding。

随后不接受“单独 closure PASS + 旧 full result”的拼接结论，而重新运行一次完整 fresh `run_probe()`：从 official NuGet registration/package reacquisition 开始，依次经过 Unicode、license、loader、protocol/process、Gate-A closure、tree budget，在**同一次 invocation**到达 `probe_pass`。

新的唯一权威 feasibility evidence：

- `local-private/pdf-toolkit-v06-feasibility/result.json`
- SHA-256 `e254b8bfad5040789a6aa0c877283adbe38da1c802402664140b665087bd308d`
- `overall_pass=true`
- `gate_a_contract_closure` 包含上述 strict request/read/search/surrogate fixtures；
- `protocol_process.prestart_cancel = {reason: cancel, spawned: false}`；active cancel与 timeout同轮继续 PASS；
- package/TFM/DLL/hash、Unicode 14.0.0、loader `loadfrom`、license、tree-budget facts未发生漂移。

承载 fresh probe 的全仓测试中 `test_000_pdf_toolkit_v06_full_fresh_feasibility ... ok`；全仓仍只有既有无关 `6001 > 5000` runtime-instructions-length failure。Job 真正结束后，临时 `tests/test_allow_tasks_regression.py` 已恢复原 SHA `4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`。

Final Locked Spec 同步修订：

- 权威 result hash改为 `e254...308d`，旧 `59eb...c59d` 降为 historical evidence；
- 明确 pre-start cancel必须在 spawning `powershell.exe` 前检查 `context.job_cancel_path`；
- 固化上述 strict stdin、bounded read/search、casefold original-offset、literal-space、unpaired-surrogate 与三态 cancel/timeout evidence。

### D32 修订后门禁

D32 是 material finding，故 D31 的单个 clean 作废；Gate B clean 计数重置为 0。现在 feasibility 才重新具备完整 Gate-A PASS 资格。必须基于 SHA `652327ac2da8d2d8f94b67f3e4035c82fbd395c7c55d71a5ddd5a909460be5d9` 的 Final Locked Spec 再取得连续两轮 `0 new material findings`，之后才允许 production TDD。

## 56. v0.6 Gate B Review D33｜Gate-A #12 search-bound/coverage evidence attack

D33 从 D32 修订后的 14 项 checklist 重新攻击“已经有 bounded read/search fixture”这一结论，不接受 `max_results+1` 等同于完整 search-side bounded evidence。

**结论：未收敛。新增 1 个 material feasibility-completeness finding。**

### D33-01｜P0 gate truthfulness｜search window 与 parser text-cap coverage 尚未被同 PowerShell closure 实际执行

D32 已证明 dense result-list 在 `max_results+1` 立即停止，但 Gate A #12 还明确要求：

- `search` 自身最多 500 页的 window bound 必须在 parser use 前 fail closed；
- parser-side page-text cap 导致的 coverage gap 必须与 result-list truncation 独立报告；
- `text_truncated_pages / search_window_complete / coverage_complete / matches_total_in_extracted_text` 的组合必须由 PowerShell 侧先形成，再进入 stdout serialization。

逐项检查发现 D32 closure 的 `Run-Search` 当时没有 `page_start/page_end`，也没有 `text_truncated_pages/coverage_complete`，因此旧权威 result `e254b8bf...308d` 仍不足以覆盖 Gate A #12 全义。

### D33 closure implementation｜temporary probe only

只修改 `local-private/pdf-toolkit-v06-feasibility/probe.py`：

- search request schema 增加严格整数 `page_start/page_end`；
- 1-based range必须满足 `page_start >= 1`、`page_end >= page_start`、窗口 `<=500`；501-page fixture 在 semantic/parser stage 前以 `search-window-invalid;parser_touched=false` 拒绝；
- search result补回 page provenance；
- 新增 parser text-cap fixture：构造 1,000,001-code-point ASCII page，PowerShell 先截为 1,000,000 extracted chars 再搜索/序列化，返回 `page_chars=1000001 / extracted_chars=1000000 / text_truncated_pages=[1] / search_window_complete=true / coverage_complete=false`，且无结果时 exact extracted-text match count仍为 0；
- dense `Straße` cap+one、literal-space query、original code-point offsets继续同轮验证。

第一次 targeted 运行曾因 probe 把 1,000,000 chars 全部展开成一百万个 `PSCustomObject` scalar entries 导致 temporary child异常退出；这不是生产设计反例。harness 改为先真实截断 1,000,001 -> 1,000,000，再对该 ASCII text-cap fixture使用 bounded ordinal search，不再制造百万对象。随后 temporary unittest 只剩两次断言字段名/新增 `page` provenance 的陈旧预期，修正临时断言后 closure test为 `ok`。这些都没有修改 production 文件或 Final Spec semantics。

随后再次执行完整 fresh `run_probe()`，拒绝把 targeted closure 与旧 full result拼接：

- official NuGet registration/package reacquisition重新执行；
- Unicode/license、loader、protocol/process、Gate-A closure、tree budget全部在同一次 invocation完成；
- `progress.json` 最终到达 `probe_pass`；
- `test_000_pdf_toolkit_v06_full_fresh_feasibility ... ok`；
- 全仓仍只有既有无关 `6001 > 5000` runtime-instructions-length failure；
- Job 真正结束后 temporary `tests/test_allow_tasks_regression.py` 再次恢复原 SHA `4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`。

新的唯一权威 feasibility evidence：

- `local-private/pdf-toolkit-v06-feasibility/result.json`
- SHA-256 `53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1`
- `overall_pass=true`
- `gate_a_contract_closure.strict_request_fail_before_parser.search_window_501` 明确包含 `parser_touched=false`；
- `gate_a_contract_closure.search_text_cap` 明确区分 parser text-cap coverage gap 与 result-list truncation；
- package/TFM/DLL/hash、Unicode 14.0.0、loader `Assembly.LoadFrom`、exact redirects、license/tree-budget facts未漂移。

Final Locked Spec 已同步新的权威 hash与 D33 evidence，当前 SHA-256 `07039a2891ce54bfe08fc43c30ab81b6eda5d2b71654535682fd48ec47eb2154`。

### D33 修订后门禁

D33 是 material finding，因此 Gate B clean 计数仍为 0。必须从 SHA `07039a2891ce54bfe08fc43c30ab81b6eda5d2b71654535682fd48ec47eb2154` 的 Final Locked Spec 重新取得**连续两轮** `0 new material findings`；production TDD 仍禁止。

## 57. v0.6 Gate B Review D34｜fresh evidence ↔ Final Locked Spec equality review

D34 不新增 probe、不补 fixture，而从最新权威 fresh result `53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1` 反向核对 Final Locked Spec，攻击“证据已经 fresh，但规范仍残留旧锁/错锁/推断锁”的风险。

**结论：CLEAN。0 个新增 material finding。**

逐项等值结果：

- package/version/selected TFM/dependency group/nupkg SHA-256 与 official SHA-512均与 concrete package lock一致；`System.ValueTuple`仍只属于非选中 TFM，不进入 runtime set；
- fresh result 的十二 DLL SHA/assembly identity与 Final Locked Spec inventory一致，无第十三个 package-owned runtime DLL；
- `selected_strategy=loadfrom` 与 production `Assembly.LoadFrom only`一致；research `byte` attempt仍只作为对比 evidence，规范明确禁止 production fallback；
- fresh loadfrom evidence中需要的 exact redirects仍只有 `System.Memory 4.0.2.0 -> 4.0.5.0` 与 `System.Buffers 4.0.4.0 -> 4.0.5.0`，与规范 whitelist一致；
- `collision_pass=true / tamper_pass=true / missing_vendor_pass=true` 与 fail-closed provenance contract一致；
- Unicode baseline仍为 `14.0.0`，`CaseFolding.txt` SHA `a566...9f0f`、generated map SHA `77db...3504`、Unicode license SHA `e7a9...3d96`、en-US/tr-TR corpus SHA `fa5b...1711`均未漂移；
- package metadata evidence仍为 SHA `2a0b1f502210009eb6474bee2ed3720d5aaf25c136154fd67db8e1487ccf26f4`，Apache/MIT/Unicode attribution contract与规范一致；
- tree budget fresh result仍为 measured `17 files / 6,143,201 bytes`、reserved `32 / 524,288`、projected `49 / 6,667,489`，低于 host `256 / 67,108,864`，与规范完全一致；
- protocol/process evidence仍为 legal JSON envelope `1,500,013 / 1,000,013 / 2,000,013` bytes、stdout overflow 9,000,000、stderr overflow 400,000、pre-start cancel `spawned=false`、active cancel与 timeout PASS；
- D33 新增的 501-page search rejection与 parser text-cap coverage fields均已出现在同一 authoritative result中，Final Locked Spec也明确记录，未被旧 D32 evidence覆盖或淡化；
- 旧 `e254...` / `59eb...` 只作为 historical evidence出现，没有被任何 current lock继续引用为权威依据。

D34 计为 Gate B **第 1 个连续 clean review**。production TDD 仍禁止；必须再取得一轮独立、不同攻击面的 `0 new material findings`。

## 58. v0.6 Gate B Review D35｜implementation-handoff failure-path / public-error attack

D35 假设实现者没有参与前述审计，只拿 Final Locked Spec 与现有 v0.5.1 public surface 开工，逐条攻击 `status` 降级、encrypted/malformed text、source mutation、provenance/assembly drift、child crash/overflow/cancel、read/search uncertainty 与 renderer independence 是否仍需临场决定可观察语义。

**结论：未收敛。新增 1 个 material public-contract finding。**

### D35-01｜P1 public compatibility｜fail-closed 已锁，但 public error taxonomy / controlled inspector envelope 未锁完整

现有 v0.5.1 已公开使用稳定可观察 error codes，例如 `PDF_PASSWORD_REQUIRED / PDF_OPEN_FAILED / PDF_TEXT_EXTRACT_FAILED / PDF_PAGE_GEOMETRY_FAILED / PAGE_RANGE_INVALID / PAGE_RANGE_TOO_LARGE / QUERY_EMPTY / SOURCE_CHANGED_DURING_CALL`，而 D34 时的 v0.6 spec只精确命名了 `PDF_INSPECT_CANCELLED / PDF_INSPECT_TIMEOUT / PDF_INSPECT_PROTOCOL_TOO_LARGE` 等少量新 process code。对于 encrypted PDF、malformed Unicode、provenance mismatch、preloaded outside-vendor assembly、PowerShell child non-zero、invalid inspector JSON等，规范虽要求 fail closed，却仍允许不同实现者选择不同 public code，甚至可能把 raw stderr/CLR exception直接上浮。

同时原文只说“exactly one UTF-8 JSON result envelope”，没有冻结 controlled success 与 controlled domain/backend error 的 envelope shape、exit-code约定、stderr角色与 unknown-code处理。这样 production TDD仍需做新的公共协议设计，违背 Final Locked Spec“不留关键临时决策”的目标。

### D35 closure｜Final Locked Spec only；不修改 feasibility facts

Final Locked Spec新增 `Public error taxonomy and controlled inspector envelope`，冻结：

- unchanged v0.5.1 inspection validation/domain codes继续保留：`PAGE_RANGE_INVALID / PAGE_RANGE_TOO_LARGE / QUERY_EMPTY / SOURCE_CHANGED_DURING_CALL / PDF_OPEN_FAILED / PDF_PASSWORD_REQUIRED / PDF_TEXT_EXTRACT_FAILED / PDF_PAGE_GEOMETRY_FAILED`；render/output/DPI taxonomy保持现有行为；
- malformed UTF-16/document string统一以 `PDF_TEXT_EXTRACT_FAILED` fail closed，并用 bounded details标识 `page_text / metadata / outline`，不使用 replacement character；
- provenance/backend exact mapping：`PDF_VENDOR_PROVENANCE_MISSING / INVALID / MISMATCH`、`PDF_BACKEND_UNTRUSTED / UNAVAILABLE / VERSION_MISMATCH`；
- v0.6 process exact codes：`PDF_INSPECT_CANCELLED / TIMEOUT / PROTOCOL_TOO_LARGE / PROTOCOL_ERROR`；child crash/non-zero、invalid UTF-8/JSON/schema、extra stdout、count/range/protocol mismatch等统一为 `PDF_INSPECT_PROTOCOL_ERROR`；raw stderr只作 bounded internal diagnostic，不得直接作为 public message；
- controlled stdout envelope唯一为 protocol v1：success `{protocol:1,ok:true,result:{...}}`；controlled failure `{protocol:1,ok:false,error:{code,message,details}}`；两者 process exit code均为 0 且 controlled completion stderr为空；未知 code、额外/缺失字段或非零退出均 fail as `PDF_INSPECT_PROTOCOL_ERROR`；
- pre-start cancel仍在 spawn 前返回，不产生 inspector envelope；
- `status` 明确是 non-throwing readiness surface：inspection坏时 `inspection_ready=false` + bounded inspection error code/message，renderer仍独立探测并可保持 `page_render_png=true`，`render-pages`不得因此调用 PdfPig。

该修订只冻结 Gate B public/error/transport semantics，不改变已通过 fresh feasibility 的 package/TFM/DLL/Unicode/loader/process ownership能力，也不引入新 executable、permission或 parser primitive，因此不重跑 Gate A candidate acquisition；production TDD必须对这套 exact envelope/error mapping写测试。

修订后 Final Locked Spec SHA-256：`84f23cd2ee37ca5c976766d9169a6aedd2b23af46e54f4abb5bd4ef7f6d02d60`。

### D35 修订后门禁

D35 是 material finding，因此 D34 的单个 clean作废；Gate B clean重新归零。必须从 SHA `84f23cd2ee37ca5c976766d9169a6aedd2b23af46e54f4abb5bd4ef7f6d02d60` 的 Final Locked Spec 再取得连续两轮 clean，production TDD仍禁止。

## 59. v0.6 Gate B Review D36｜error/envelope contradiction sweep

从 D35 修订后的完整 v0.6 normative section 搜索 `stderr / non-zero / error code / status / page_render_png / encrypted` 等所有可能形成第二套失败协议的措辞，检查 controlled completion、host-owned process failure、domain error与 readiness降级是否互相矛盾。

**结论：CLEAN。0 个新增 material finding。**

复核结果：

- controlled inspector completion唯一为 stdout protocol-v1 envelope + exit code 0 + empty stderr；没有另一处允许受控 domain/backend error通过 stderr或非零退出表达；
- child crash/non-zero、invalid UTF-8/JSON/schema、extra stdout等均唯一归入 `PDF_INSPECT_PROTOCOL_ERROR`，stdout/stderr overflow唯一归入 `PDF_INSPECT_PROTOCOL_TOO_LARGE`；
- pre-start cancel仍明确发生在 spawn前，active cancel/timeout属于 host supervision，不与 controlled inspector envelope混用；
- encrypted/password-required public failure现在唯一冻结为 `PDF_PASSWORD_REQUIRED`，open/text/geometry/range/query/source-change等既有 code也已明确继承；
- malformed document-derived UTF-16统一走 `PDF_TEXT_EXTRACT_FAILED` + bounded surface details，没有 replacement fallback或第二个 Unicode public code；
- provenance missing/invalid/mismatch、outside-vendor/untrusted、backend unavailable/version mismatch边界已经各自唯一；
- `status` 在 backend异常时明确 non-throwing，`inspection_ready=false`并报告 bounded inspection error；`page_render_png`仍独立探测，且 `render-pages`不得为此调用 inspector；
- earlier `status`/renderer-independence wording与 D35 新 contract一致，没有把整体 `ready=false`误写成 renderer unavailable。

D36 计为 Gate B **第 1 个连续 clean review**。production TDD仍禁止，必须再取得一轮不同攻击面的 clean。

## 60. v0.6 Gate B Review D37｜production handoff / installer transaction attack

本轮不再复查 D36 的 error envelope，而是假设一个未参与前序审计的实现者只拿当前 Final Locked Spec 开始写 `install.ps1`：从 locked nupkg acquisition / staged tree / provenance / host tree budget，一直走到 hot-scan cutover、rollback、rescan/reapproval，检查是否仍需要自行决定关键事务语义。

**结论：未收敛。新增 1 个 material installer-transaction finding。**

### D37-01｜P0 hot-scan integrity｜v0.6 normative section没有冻结 create-then-publish / rollback transaction

现有 v0.6 normative text虽然已经要求：

- production install只接受 Gate-B-locked package bytes；
- safe extraction + exact inventory/provenance；
- cutover前实测 256 files / 64 MiB；
- runtime不得网络修复/fallback；

但完整的“staging -> old-tree backup -> whole-directory publish -> rollback”语义此前只存在于明确标记为 `v0.5 historical / NOT CURRENT SPEC` 的历史段落。v0.6 自身仅写 `before cutover / approved-tree cutover`，没有规定：

- staging 必须位于 hot-scan root 外；
- publish 是否允许对 live tree 逐文件覆盖；
- `-Force` 时旧树何时、移到哪里；
- publish/postcondition 失败时如何恢复；
- rollback 自身失败时如何保全旧树；
- filesystem success 与 FolderBridge exact-tree-hash reapproval 的边界。

这违反 Final Locked Spec“production implementation不再临时选择关键行为”的目标，也允许 FolderBridge hot-scan 在更新过程中观察一个半构造 tree。D36 的一个 clean 因此不能继续累计。

### D37 closure｜Final Locked Spec normative install transaction

已在当前 v0.6 normative section新增 `v0.6 installer transaction / hot-scan cutover contract`，明确冻结：

1. 完整 v0.6 candidate先在 **hot-scan root外、与 destination同 volume** 的 unique staging tree建立；最终 publish必须能用 whole-directory rename/move，不允许递归复制进 live tree；
2. touching live destination前必须完成 exact nupkg/hash/TFM/dependency-group、12 DLL SHA/assembly identity、Unicode semantic asset、licenses/NOTICE、schema-v3 provenance、expected inventory以及 staged 256-file/64-MiB实测；pre-cutover failure只清 staging，旧 live tree不变；
3. 非 Force遇到已存在 destination直接失败；Force必须先把旧 `pdf-toolkit` whole tree移到 hot-scan root外的 same-volume sibling backup，禁止逐文件删除/覆盖作为更新机制；
4. verified staging以一次 directory move/rename发布到 live destination，使 scanner最多观察 complete old / absent / complete new三态，而非有意 half-built tree；
5. publish后在删除 backup前重新验证 live inventory、file/byte budget、locked runtime DLL/data hashes、manifest/provenance identity；publish或 postcondition失败必须移除失败新树并 whole-tree rollback旧树；rollback自身失败则停止、保留 backup路径并显式报错，禁止继续变异；
6. backup只有 live postconditions 全通过后才删除；staging/download再 best-effort cleanup；
7. filesystem publish成功不等于 trust approval：必须 FolderBridge rescan -> 展示新 exact tree hash + permissions -> 本地 reapprove -> enable；旧 v0.5 approval/hash不能继承；
8. installed runtime不得网络自修、换 package/TFM、改 GAC/CLR config；缺失/漂移保持 fail closed，直到显式 reviewed reinstall。

同时把 production TDD必须覆盖的 transaction attacks写入规范：pre-cutover failure不碰旧树、Force先移旧树、publish failure rollback、postcondition failure rollback、rollback failure保全 backup、禁止 file-by-file overwrite、成功后仍需 exact-hash reapproval。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `6a6d020c9b300df96d52a79deff6dd0a7b1e4686a4f5bab17f39aba70c04dc00`。

### D37 修订后门禁

D37 是 material finding，因此 D36 的单个 clean作废；Gate B clean重新归零。必须从 SHA `6a6d020c9b300df96d52a79deff6dd0a7b1e4686a4f5bab17f39aba70c04dc00` 的 Final Locked Spec 再取得连续两轮、不同攻击面的 `0 new material findings`。production v0.6 TDD仍禁止。

## 61. v0.6 Gate B Review D38｜rollback namespace-integrity attack

本轮只攻击 D37 新增的 transaction contract，不复查 package/evidence/error taxonomy。目标是验证在 Windows hot-scan 语义下，rollback 本身是否也保持 whole-tree namespace transaction，而不是只保证正常 publish 不逐文件覆盖。

**结论：未收敛。新增 1 个 material rollback-integrity finding。**

### D38-01｜P0 rollback half-tree window｜“递归删除失败新树后再恢复旧树”违背 whole-tree 原则

D37 修订后的第 5 步原文要求：publish/postcondition失败时先 `remove the failed new destination best-effort`，然后把旧 backup whole-tree移回 live destination。

这个顺序仍允许实现者对 hot-scan root 内已经发布的新 tree执行 recursive delete。即使正常 cutover是 whole-directory rename，递归删除期间 FolderBridge scanner仍可能观察一个逐文件消失的 half-tree；而且删除中途失败还会留下占用 live destination name 的残树，使旧 backup无法恢复。它与 D37 自己冻结的“不把 live tree当 file-by-file update surface”原则冲突。

### D38 closure｜rollback也只允许 namespace move

Final Locked Spec 已将 rollback改为：

- failed new live tree若存在，必须先 **whole-directory rename/move** 到 hot-scan root外、same-volume unique quarantine；
- 只有 live destination name完全释放后，才允许把旧 backup whole-tree restore回来；
- 禁止把 recursive delete / file-by-file mutation作为 rollback前置步骤；
- 如果 failed-new -> quarantine rename失败，立即停止，保留旧 backup，且不得开始部分删除 live tree；
- 如果 old backup -> live restore失败，立即停止并同时保留 backup与 quarantined failed-new tree；
- incomplete rollback必须显式报 install/rollback failure，之后不再继续变异 tree；
- production TDD新增 quarantine-rename failure、restore failure、禁止 recursive-live-delete rollback path 等攻击。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `0172f5fadd73805eaa83eff7c52dc4a5dfd0400474f461a013746beeec35528b`。

### D38 修订后门禁

D38 是 material finding，Gate B clean继续为 0。必须从 SHA `0172f5fadd73805eaa83eff7c52dc4a5dfd0400474f461a013746beeec35528b` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 62. v0.6 Gate B Review D39｜fresh-install transaction-state attack

本轮继续只攻击 installer state machine，但与 D38 不同：不再看 rollback 是否 whole-tree，而专门检查 **没有旧版本时** transaction 的失败语义是否唯一。按规范模拟 `had_previous=false` 的 normal install / `-Force` fresh install / publish failure / postcondition failure。

**结论：未收敛。新增 1 个 material state-machine finding。**

### D39-01｜P0 fresh-install rollback ambiguity｜规范默认“old backup”存在

D38 修订后的 rollback语言仍围绕“preserved old backup”描述。对于真正的第一次安装：

- live `pdf-toolkit` 在 transaction开始时不存在；
- `-Force` 也可能在不存在旧树时被传入；
- publish成功后若 postcondition失败，没有旧 tree可 restore。

原规范没有冻结此时最终 live destination应该 absent 还是保留失败新 tree，也没有说明是否创建 synthetic backup、quarantine如何处理。实现者仍需临时设计失败状态，因此不能算 implementation-complete。

### D39 closure｜显式 `had_previous` 双分支状态机

Final Locked Spec 已进一步冻结：

- transaction开始显式捕获 `had_previous = live destination exists`；
- normal install + `had_previous=true` 直接失败；
- `-Force + had_previous=false` 仍走 fresh-install branch，不创建空/synthetic backup；
- 任何 failed/uncertain new live tree仍先 whole-directory quarantine到 hot-scan外；quarantine失败时不得递归删 live tree；
- `had_previous=true`：live name释放后 whole-tree restore old backup；restore失败保留 backup + quarantine；
- `had_previous=false`：live name释放后最终状态必须是 **destination absent**，没有 restore动作，也不得伪造 backup；
- 成功完成的 failure evacuation/rollback 后，failed-new quarantine只允许在 hot-scan外 best-effort cleanup；cleanup失败报告路径但安装仍失败；
- successful replacement只有在 live postconditions通过后删 old backup；successful fresh install本来就没有 backup。

production TDD attack list同步增加：fresh pre-cutover failure保持 destination absent、`-Force` fresh不建 backup、fresh publish/postcondition failure最终 destination absent，以及 fresh quarantine failure不得退化为递归删除。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `0a1814a24ce8081401f22192bdb232240a02f2d807ba226852bece2f1a297f0e`。

### D39 修订后门禁

D39 是 material finding，Gate B clean继续为 0。必须从 SHA `0a1814a24ce8081401f22192bdb232240a02f2d807ba226852bece2f1a297f0e` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 63. v0.6 Gate B Review D40｜concurrent-installer serialization attack

本轮只攻击两个 installer process 同时针对同一 live destination 的竞态。即使每个 process 内部都遵守 whole-tree staging/backup/quarantine，如果二者可以同时读取 `had_previous` 并交错 rename，transaction仍不具备串行语义。

**结论：未收敛。新增 1 个 material concurrency finding。**

### D40-01｜P0 concurrent cutover race｜缺少 destination-scoped installer lock

旧规范没有要求 `install.ps1` 在观察 live state前取得 exclusive lock。因此两个 `-Force` installer可能：

- 都基于同一旧 live tree做出 `had_previous=true` 判断；
- 一个把 old tree移到 backup后，另一个重新看到 absent/new state；
- 各自持有 staging/backup/quarantine并交错 publish/rollback；
- 最终虽然单次操作都是 directory rename，整体状态机仍不 serializable。

### D40 closure｜OS-handle destination lock

Final Locked Spec 已冻结：

- 在读取 `had_previous`、创建 staging、下载 package或触碰 live tree **之前**，先 canonicalize live destination；
- 按 Windows case-insensitive identity规范化 canonical path，并由该 path确定性 SHA-256出 destination lock key；
- lock file位于 hot-scan root外、destination parent volume上的 installer-owned non-link目录；
- 用 OS file handle + `FileShare.None` 持有整个 transaction，直到 terminal success/failure 与 recovery path记录完成；
- 获取失败立即 install-busy，且必须发生在任何 mutation前；
- lock file可以持久存在，所有权来自 live handle；process crash后由 OS释放，不允许用 PID/time heuristic“偷锁”；
- 不同 canonical destinations使用不同 keys，不扩大成 global serialization。

production TDD attack list同步增加 same-destination双 installer竞争与 different-destination非全局阻塞测试。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `2d2b84d37da8e643a2f4b634412620f50d8dda26312b102aee972acc0488cee6`。

### D40 修订后门禁

D40 是 material finding，Gate B clean继续为 0。必须从 SHA `2d2b84d37da8e643a2f4b634412620f50d8dda26312b102aee972acc0488cee6` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 64. v0.6 Gate B Review D41｜process-crash / transaction-residue attack

本轮只攻击 installer process在 transaction中途被强杀/断电后，下一次 installer 是否会把残留 filesystem topology误解释成一个新初始状态。D40 的 OS file lock只能阻止**同时**运行；process死后 handle会正确释放，但不会自动解释已经发生的 backup/publish namespace move。

**结论：未收敛。新增 1 个 material crash-recovery finding。**

### D41-01｜P0 crash-state ambiguity｜lock释放后缺少持久 transaction identity/journal

如果 process在 `old tree -> backup` 后、`new staging -> live` 前死亡：

- exclusive lock由 OS释放，这是正确行为；
- live destination此时 absent；
- old tree仍安全存在于 hot-scan外 backup；
- 但旧规范没有持久 transaction identity/state。

下一次 installer若只重新计算 `had_previous = live exists`，会得到 `false`，从而把一个**中断的 replacement**误当成 fresh install。这会绕过旧树恢复语义。类似歧义也存在于 crash-after-publish / crash-after-postconditions-before-cleanup。

### D41 closure｜destination-keyed persistent transaction journal

Final Locked Spec 已冻结：

- same destination key在 hot-scan root外、same volume拥有唯一 persistent transaction-state directory；
- acquire exclusive lock后、**在重新推断 `had_previous` 之前**先检查该 state；
- non-committed prior journal一律 `INSTALL_RECOVERY_REQUIRED`：新 invocation不得建新 staging、不得把 absent live解释为 fresh、不得自动删除/move recorded recovery trees；必须报告 canonical destination及 staging/backup/quarantine记录路径与当前存在状态；
- 不允许基于 PID不存在、时间过久等 heuristic丢弃 unfinished state；
- 新 transaction使用 atomic temp+same-directory rename写 bounded BOM-less UTF-8 `transaction.json`，至少记录 schema、destination key/canonical path、transaction id、had_previous、phase与 known recovery paths；
- phase固定为 `prepared / old_backed_up / new_published / committed`，boundary update必须 atomic；namespace topology与 journal不一致/torn/unknown时 fail recovery-required，不猜测；
- `committed` 只能在 live postconditions全通过后写；
- 后续 invocation遇到 valid committed residue时，必须先重新验证 live v0.6 manifest/provenance/inventory/hash postconditions，只有仍通过才允许清 hot-scan外 residue/journal并开始新 transaction；否则 recovery-required；
- transaction journal永不进入 approved Extension tree。

production TDD同步增加 process death at journal-created / old-backed-up / new-published / committed 等 crash fixtures，以及下一次 invocation不得误推 `had_previous`、torn/unknown journal fail closed、committed residue revalidation等测试。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `5e94aff09794323284164b3b36c2a6d693551f74697c88b3683e4f58b1ce82be`。

### D41 修订后门禁

D41 是 material finding，Gate B clean继续为 0。必须从 SHA `5e94aff09794323284164b3b36c2a6d693551f74697c88b3683e4f58b1ce82be` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 65. v0.6 Gate B Review D42｜journal terminal-state poisoning attack

本轮不攻击 process crash本身，而攻击**正常、已妥善处理的失败**：如果 pre-cutover hash failure或成功 rollback仍留下 D41 定义的 non-committed journal，下一次 installer会被错误地判为 recovery-required，形成自我投毒。

**结论：未收敛。新增 1 个 material journal-lifecycle finding。**

### D42-01｜P0 handled-failure poisoning｜没有 terminal abort 状态

D41 只定义了 `prepared / old_backed_up / new_published / committed`。因此：

- package/hash/inventory在 touching live前失败；
- replacement publish失败但旧 tree已经完整 restore；
- fresh postcondition失败且 new tree已 whole-tree evacuate、live重新 absent；

这些都已经回到明确安全初始 topology，却仍只能留下“non-committed” journal。下一次运行按照 D41规则会 `INSTALL_RECOVERY_REQUIRED`，无法区分真正 crash/incomplete rollback 与正常 handled failure。

### D42 closure｜`aborted` terminal state

Final Locked Spec 已把 journal phase分成：

- nonterminal：`prepared / old_backed_up / new_published`；
- terminal：`aborted / committed`。

并冻结：

- `aborted` 只有在 handled failure已经证明恢复 initial safe namespace后才能写：had_previous=true时 old tree已回 live且 backup name释放；had_previous=false时 live destination absent；
- quarantine/restore失败绝不写 aborted，继续保留 nonterminal journal；
- pre-cutover handled failure在旧 live未变/ fresh live仍 absent时写 aborted，再清 hot-scan外 staging；
- successful rollback/fresh evacuation写 aborted，再清 outside-root quarantine/state；process若在 cleanup中死掉，下一次识别 terminal aborted residue而不是 incomplete rollback；
- next invocation只在 aborted记录的 initial topology仍成立时退休该 residue；topology mismatch仍 recovery-required；
- success在 live postconditions全通过后写 committed，再清 backup/staging；committed residue仍须重新验证 live v0.6 postconditions；
- torn/unknown/mismatched terminal journal一律 recovery-required。

production TDD同步加入 handled pre-cutover failure不得 poison next install、rollback成功写 aborted、rollback失败保持 nonterminal、crash-after-aborted/committed residue处理等测试。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `0330b8ca6befa97f284aa5e23b4a2644b7200e95bd36dd83a7c2ed3f65239766`。

### D42 修订后门禁

D42 是 material finding，Gate B clean继续为 0。必须从 SHA `0330b8ca6befa97f284aa5e23b4a2644b7200e95bd36dd83a7c2ed3f65239766` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 66. v0.6 Gate B Review D43｜installer state-machine contradiction sweep

本轮不再引入新功能，而把 D37–D42 叠加后的完整 transaction section按文字顺序从头执行，专门寻找同一 normative section 内会让实现者得到不同状态机的相反措辞。

**结论：未收敛。新增 2 个同源 material ordering/wording finding。**

### D43-01｜terminal-abort wording contradiction

D42 已把 `aborted` 定义为 terminal phase，但前置 recovery paragraph仍写“A non-committed journal ... is INSTALL_RECOVERY_REQUIRED”。`aborted` 显然也是 non-committed，因此同一规范同时要求它“可安全退休”和“必须 recovery-required”。

修订为：只有 **nonterminal `prepared / old_backed_up / new_published`** prior journal直接 recovery-required；terminal `aborted / committed` 只走后文明确的 terminal-residue规则。

### D43-02｜`had_previous` capture顺序自相矛盾

journal段已要求新 transaction在 staging前写入包含 `had_previous` 的 `transaction.json`；但编号第 3 步仍写到 staging验证之后才 `Capture had_previous`。这会导致实现者无法确定 initial state究竟在何时冻结，甚至可能在 package staging期间 live tree变化后重新推断。

修订为：

- acquire lock；
- handle prior journal；
- **此时唯一一次**捕获 `had_previous = live destination exists`；
- 创建/写 new journal并把该值记录进去；
- staging/backup/publish过程中不得重新推断。

normal install + had_previous=true 也按这个 immutable initial value在 live mutation前进入 terminal aborted cleanup；Force/fresh分支均沿用同一值。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `9b2c1c599d24ad5338c2516eff735dc14ab90727151d03248f18328d7f9eba8d`。

### D43 修订后门禁

D43 是 material finding，Gate B clean继续为 0。必须从 SHA `9b2c1c599d24ad5338c2516eff735dc14ab90727151d03248f18328d7f9eba8d` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 67. v0.6 Gate B Review D44｜public manifest/action-surface parity attack

本轮完全离开 installer，检查“v0.6只改变 parser placement、不改变 public API”是否已经落实到 machine-readable manifest surface，而不是只保留 action 名称。

**结论：未收敛。新增 1 个 material public-contract finding。**

### D44-01｜P0 API drift ambiguity｜只锁 action names，没有逐字段锁 manifest schema/defaults/job/mutation scope

v0.6 旧文本写了 public actions仍是 `status/info/outline/read-pages/search/render-pages`，也零散锁了 read/search/render runtime caps，但没有明确冻结当前 v0.5.1 `folderbridge-extension.json` 的完整 schema。因此实现者仍可能在“action 名没变”的前提下改变：

- `requires_workspace` / `read_only` / global authorization；
- `path/query` 长度；
- `max_outline_items/text_sample_pages/max_items/max_chars/max_results/snippet_chars` 上下界或 defaults；
- `page_start/page_end` schema bounds；
- render `run_mode=job` / 7200s action timeout；
- `mutation_scope` 是否仍只 claim `output_dir` tree；
- `dpi/make_zip` default；
- 甚至新增 password/URL/regex/options 等 caller-controlled field。

这会把 backend migration变成隐性 public API migration，违背 v0.6 目标。

### D44 closure｜v0.5.1 manifest schema-preserving migration

Final Locked Spec 已逐字段冻结 v0.6 manifest：

- `schema_version=1 / id=pdf-toolkit / name=PDF Toolkit / version=0.6.0 / entrypoint=plugin.py`；
- isolated-process normal ceiling 600s；workspace_adapter none/none；exact三项 permission；
- 六个 actions全部保持 `authorization=global`、`additionalProperties=false`；
- status/info/outline/read-pages/search/render-pages 的 read_only、requires_workspace、required fields、每个 integer/string bound/default全部按当前 v0.5.1 manifest冻结；
- read-pages runtime 50-page、search 500-page、render 100-page上限继续保持；
- render仍 `run_mode=job`、7200s、ABI-v1 `output_dir` tree mutation claim、72..400 dpi default180、make_zip default true；
- 明确禁止 alias/password/URL/regex/executable/script/assembly path/generic options/run-all 等新增 caller surface。

任何未来 schema/default/auth/run-mode/mutation-scope调整必须单独 public-API review，不能夹带在 v0.6 parser implementation中。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `891b6e661c13052cc295f91cd4d6518c06b27ebdeb6c9c17d73aa17bb2c162cf`。

### D44 修订后门禁

D44 是 material finding，Gate B clean继续为 0。必须从 SHA `891b6e661c13052cc295f91cd4d6518c06b27ebdeb6c9c17d73aa17bb2c162cf` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 68. v0.6 Gate B Review D45｜public response-shape parity / explicit-delta attack

本轮继续 public API 审计，但与 D44 的 input manifest 不同：逐项对照当前 v0.5.1 `plugin.py` 实际返回对象，检查 v0.6 是否明确规定哪些 result keys/nested fields必须保持、哪些 backend-specific field允许改变。

**结论：未收敛。新增 1 个 material response-contract finding，并暴露 1 个 render error-semantics contradiction。**

### D45-01｜P0 output drift ambiguity｜规范只锁行为，没有锁 caller-visible result shape

现有 v0.6 已冻结 read/search coverage semantics、source identity、status readiness、render parser independence，但没有明确要求保持当前 top-level/nested key names。因此实现者仍可能：

- 把 `read-pages.pages[].chars` 改名为 internal harness 使用的 `page_chars`；
- 删除 search兼容字段 `truncated` 或 `text_coverage_complete`；
- 改 `info` metadata/outline/text-sample nested shape；
- 把 render `page_count`直接删除而不说明 replacement ownership；
- 任意清理 status 的旧 backend diagnostics，导致调用方不可预测。

此外 D35 曾笼统写“render keeps existing PDF_RENDER_* taxonomy unchanged”，但 v0.6 又明确禁止 render启动 PdfPig，因此旧 `PDF_RENDER_SOURCE_MISMATCH` 的“text backend page count vs renderer page count”触发条件已经不存在；若不明确处理，implementation会被两条要求夹住。

### D45 closure｜preserve-by-default + enumerated v0.6 output delta

Final Locked Spec 已冻结：

- common inspection `path/bytes/sha256` source identity不变；
- `info` top-level、metadata、outline preview、page-size、text-sample keys全部按 v0.5.1 保持；
- `outline` result/items shape不变；
- `read-pages` 保留全部 top-level fields与 `pages[].page/text/chars/extracted_chars/text_truncated/partial`，并特别注明 public legacy field名必须继续是 `chars`；
- `search` 保留 `results_truncated` + `truncated` alias、`text_coverage_complete` + `coverage_complete`、exact/at-least match count与 result entry shape；
- status只有 backend diagnostic是明确允许的 v0.6 delta：保留 general readiness/policy/capability/error keys，把 pypdf-only pin/patch fields换成 `inspection_ready / pinned_pdfpig_version / loaded_pdfpig_version / pdf_inspect_script_present / casefold_unicode_version` 等 exact PdfPig seam fields；其它未列 status shape drift禁止；
- render继续 parser-independent，但保留既有 artifact-oriented keys；`page_count`继续存在并明确改为 renderer-owned，同时新增等值 `source_units` + `selected_range`；`text_backend="PdfPig 0.1.16"`只是 declarative identity，并新增 `inspection_backend_invoked=false` 防止误读为 parser执行证据；
- `RENDER-COMPLETE.json` 因 ownership改变明确升级 schema_version 3，加入 source_units/selected_range/inspection_backend_invoked，同时继续最后写入作为 commit marker；
- `PDF_RENDER_SOURCE_MISMATCH` 保留为 reserved compatibility code，但 v0.6 不再因 PdfPig-vs-Windows page count比较而触发；cross-backend disagreement在 separately obtained evidence比较时进入 `page alignment=unresolved`；renderer自身 envelope/range/count/DPI/file不一致仍为 `PDF_RENDER_PROTOCOL_ERROR`，source stat fence仍 `SOURCE_CHANGED_DURING_CALL`。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `158960bfa10556fadb9af1cfa634198e6ce51ebe636c98d77d7582144f827ac3`。

### D45 修订后门禁

D45 是 material finding，Gate B clean继续为 0。必须从 SHA `158960bfa10556fadb9af1cfa634198e6ce51ebe636c98d77d7582144f827ac3` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 69. v0.6 Gate B Review D46｜response/error ownership contradiction sweep

本轮不新增 output field，只搜索 D45 修订后 normative v0.6 内是否还存在与 parser-independent render ownership相反的旧 error wording。

**结论：未收敛。新增 1 个 material wording contradiction。**

### D46-01｜`PDF_RENDER_SOURCE_MISMATCH` trigger wording仍双义

D45 已明确：v0.6 render不启动 PdfPig，因此旧的“text backend page count != Windows renderer page count”不再是 render-time trigger；code只保留为 reserved compatibility name，cross-backend disagreement改为 later workflow `page alignment=unresolved`。

但后部 error taxonomy仍写“render keeps its existing `PDF_RENDER_*` ... taxonomy unchanged”。若按字面理解，implementation又必须保留旧 source-mismatch trigger，这与 parser-independent contract冲突。

### D46 closure

已把后部 taxonomy同步为同一唯一口径：

- 保留既有 `PDF_RENDER_*` **code namespace** 与其它未变化的 triggers；
- 明确 carve out `PDF_RENDER_SOURCE_MISMATCH`：reserved但不再由 PdfPig-vs-Windows comparison触发；
- renderer自身 source_units/range/file-count/name/DPI/envelope不一致 -> `PDF_RENDER_PROTOCOL_ERROR`；
- source stat fence -> `SOURCE_CHANGED_DURING_CALL`；
- separately obtained inspection/render page-count不一致 -> workflow `page alignment=unresolved`。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `110bb4803d7c2a5a3ea02b685a3e635719b8045f268320c95e49f921f32561d3`。

### D46 修订后门禁

D46 是 material finding，Gate B clean继续为 0。必须从 SHA `110bb4803d7c2a5a3ea02b685a3e635719b8045f268320c95e49f921f32561d3` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 70. v0.6 Gate B Review D47｜from-zero implementation-choice residue sweep

本轮用 `candidate / may / fallback / choose / optional / equivalent` 等措辞重新扫完整 normative v0.6，目标不是找历史描述，而是找仍会让 production coder临时选择关键行为的残留。

**结论：未收敛。新增 3 个 material uniqueness findings。**

### D47-01｜loader redirect仍写 `may register`

Gate B fresh evidence已经证明当前 locked assembly set需要两个 exact strong-name redirects，但 concrete loader lock仍写 production `may register one bounded AssemblyResolve handler`。这允许实现者尝试不注册、按需注册或换策略，与“loader strategy唯一化”目标冲突。

修订：production **must register exactly one** bounded handler，且必须在 12 DLL全部 hash/identity verify + LoadFrom后注册；内容只有冻结的两个 exact FullName mapping，其余 package-owned unresolved request fail closed。

### D47-02｜PowerShell source encoding仍有二选一

规范仍允许：`pdf_inspect.ps1` semantic literals “ASCII-only ... **or** use an explicitly reviewed PowerShell-5.1-safe script-file encoding”。这把一个 implementation choice留到了 production。

修订：production `pdf_inspect.ps1` script-source层固定 **ASCII-only semantic literals**；需要非 ASCII fixture/constant时用 numeric Unicode code points运行时构造。未来改 script-file encoding必须 reopen Gate B。stdin/stdout protocol仍 strict BOM-less UTF-8 bytes。

### D47-03｜status parser-memory policy key仍允许 equivalent wording

D45 已把 public status result锁为 exact `policy.parser_memory_sandbox=false`，但后部 safety段仍写该 key“or equivalent unambiguous wording”。这允许 implementation不返回 D45 已冻结的字段。

修订：`status.policy` 必须返回 exact boolean `parser_memory_sandbox=false`；README/security prose再用自然语言解释，但不能替代该字段。

修订后 `docs/pdf-toolkit-external-extension-design-20260903.md` SHA-256 为 `a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6`。

### D47 修订后门禁

D47 是 material finding，Gate B clean继续为 0。必须从 SHA `a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6` 的 Final Locked Spec 再取得连续两轮不同攻击面的 clean；production v0.6 TDD仍禁止。

## 71. v0.6 Gate B Review D48｜Final Spec ↔ authoritative fresh feasibility equality matrix

本轮换成 evidence-equality 攻击面，不复查 installer/public-API wording。以当前 Final Locked Spec SHA `a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6` 对照唯一权威 fresh feasibility result 与两个 companion evidence artifacts，逐项检查 concrete lock是否在 D35–D47 文档修订中发生漂移。

权威 evidence仍为：

- `local-private/pdf-toolkit-v06-feasibility/result.json` SHA-256 `53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1`；
- `package-metadata.json` SHA-256 `2a0b1f502210009eb6474bee2ed3720d5aaf25c136154fd67db8e1487ccf26f4`；
- `gate-b-semantics.json` SHA-256 `8e37c7275573d44a8c3aec97b31d3205fdae14841a51387d146c4e84f8afb103`。

**结论：CLEAN。0 个新增 material finding。**

复核矩阵：

- package/version/selected TFM/dependency groups及 nupkg hashes未变；
- 12 DLL inventory/hash/assembly identities未变；
- selected loader仍 `loadfrom`；collision/missing-vendor/tamper负例仍全部 `true`；
- exact strong-name redirects仍只有 `System.Memory 4.0.2.0 -> 4.0.5.0` 与 `System.Buffers 4.0.4.0 -> 4.0.5.0`；D47 只是把 handler从 `may` 唯一化为 `must exactly one`，没有改变 evidence内容；
- Unicode仍 Python baseline 14.0.0，CaseFolding source SHA `a566...9f0f`、generated map SHA `77db...3504`、Unicode license SHA `e7a9...3d96`、culture corpus hash均未漂移；
- protocol/process仍包含 pre-start `spawned=false`, active cancel, timeout, bounded overflow；
- D33 search 501-page pre-parser rejection与 parser text-cap coverage evidence仍在同一 authoritative result；
- measured payload 17 files / 6,143,201 bytes、projected 49 files / 6,667,489 bytes仍与 Final Spec一致；
- package license/attribution metadata hash与 Final Spec license section一致；
- D35–D47 新增的 installer transaction、manifest/output compatibility、error ownership与 exact source-encoding/status-key约束均是 production contract强化，不要求改变 Gate A package/parser feasibility facts，因此不需要重新生成 feasibility result。

D48 计为 Gate B **第 1 个连续 clean review**。production v0.6 TDD仍禁止；必须从同一 Final Locked Spec SHA 再取得一轮独立、不同攻击面的 clean。

## 72. v0.6 Gate B Review D49｜FolderBridge external-extension security/capability boundary attack

本轮用与 D48 完全不同的攻击面：把 v0.6 当作一个 FolderBridge 外源热加载 Extension，从 host security model反向检查 permission、authorization、workspace confinement、fixed-process surface、runtime network、mutation scope、installer/runtime边界与 parser-isolation honesty。对照当前已安装 v0.5.1 `extension info` 的真实 host-visible manifest contract，而不是只看设计文档自述。

**结论：CLEAN。0 个新增 material finding。**

复核结果：

- 当前 host-visible baseline与 v0.6 locked manifest一致保持 exact三项 runtime permission：`workspace.read / workspace.write / process.execute:powershell.exe`；没有 network、generic process、OCR、URL或 PDF mutation capability；
- `status`仍 `read_only=true / requires_workspace=false`；`info/outline/read-pages/search`均 workspace-bound + read-only；`render-pages`仍是唯一 mutating action，且 host-visible mutation scope只 claim caller指定的 `output_dir` tree；没有因为 PdfPig 或 installer recovery引入 opaque workspace mutation；
- 六个 actions仍 global authorization + strict schema/additionalProperties=false；v0.6 没有新增 command/script/executable/assembly path、password、URL、regex或 arbitrary options入口；
- runtime `powershell.exe` surface只围绕 approved fixed `pdf_inspect.ps1` / `pdf_render.ps1`；inspection caller data走 bounded strict UTF-8 stdin JSON，不上 argv；render仍固定 renderer contract；
- 所有 caller runtime file input仍由 Python host policy限定为 workspace-relative POSIX path，拒绝 traversal/absolute/backslash/link/reparse/dependency/VCS/build/sensitive paths；PowerShell收到的是 host-resolved path，不成为新的 public arbitrary-filesystem surface；
- installer/bootstrap 的 network acquisition、destination lock、transaction journal、backup/quarantine都属于显式 user-run repository utility，不是 Extension action，也不进入 approved Extension tree；installed runtime明确禁止网络 repair、package/TFM fallback、GAC/CLR config mutation；
- exact-tree approval lifecycle仍存在：filesystem install success必须 rescan -> exact tree hash + permissions review -> reapprove -> enable；旧 v0.5 approval不能继承到新 bytes；
- parser child process虽然 owned/cancellable，但规范明确不宣称 OS sandbox，并要求 exact `status.policy.parser_memory_sandbox=false`；这与 FolderBridge 外源 Extension本身“isolated process != OS sandbox”的安全边界一致；hostile/untrusted PDF仍明确属于 VM/container-grade isolation场景；
- D37–D47 新增 installer transaction/journal与 public response contracts没有要求扩大 Extension permissions，也没有把 installer恢复状态暴露成 runtime action。

当前 Final Locked Spec SHA-256 仍为 `a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6`，与 D48 审核基准完全相同。

### Gate B convergence

D48 + D49 已在**同一 Final Locked Spec SHA**上连续取得两轮独立 `0 new material findings`：

1. D48：authoritative fresh feasibility equality matrix — CLEAN；
2. D49：FolderBridge external-extension security/capability boundary — CLEAN。

因此 **Gate B = CONVERGED / PASS**。此前 `production v0.6 TDD forbidden` 门禁现在解除；允许从 Final Locked Spec SHA `a7846385a8d1a61b12ddbae2de52a5a03f9a7e7682bc9669d6c5c5ebcdc570e6` 开始 production TDD。任何后续 production 实现若要求改变 package/version/hash/TFM、12-DLL set/load order/redirect、Unicode data、permission/action schema、public response/error semantics、installer transaction state machine或 parser/render ownership，必须 reopen Gate B，而不能在实现中临时改设计。
