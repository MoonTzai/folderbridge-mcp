# PDF Toolkit external Extension design｜2026-09-03

> Current installed implementation: **v0.5.1, runtime FAILED**. **v0.6.0 feasibility PASS; Gate B Final Locked Spec convergence is in progress. Production v0.6 remains forbidden until Gate B records two consecutive clean reviews.** The MATT redesign/audit history and convergence gates are recorded in `docs/pdf-toolkit-matt-redesign-and-audit-20260903.md`. Real frozen-host acceptance invalidated both prior parser-placement assumptions: v0.4/0.4.1 placed native pypdfium2 inside the frozen Python worker and hard-failed; v0.5/0.5.1 placed vendored pure-Python pypdf inside that worker, but the package still depends on standard-library modules selected at FolderBridge.exe freeze time. v0.5.1's narrow XMP/xml compatibility guard removed one known missing-module path but fresh exact-tree runtime acceptance still failed during pypdf import. v0.6 therefore moves third-party PDF parsing completely out of the frozen Python interpreter while preserving the public audit workflow.

## Goal

Provide FolderBridge with a generic, hot-loadable, workspace-confined PDF evidence workflow without adding MCP tool names or expanding the bundled Office extension beyond its Office-document remit.

Primary audit workflow:

`info -> search -> read-pages -> render-pages -> image_open`

The design treats extracted text as untrusted document content and rendered pages as the visual verification layer for critical or layout-sensitive claims.

## Upstream research and selection

The implementation is new FolderBridge code. Upstreams are research references only and are cloned into ignored `local-private/pdf-toolkit-upstreams/` by `Plugins/extensions/pdf-toolkit/fetch-upstreams.ps1`.

| Upstream | Adopted design lesson | Not adopted |
| --- | --- | --- |
| `jztan/pdf-mcp` | Agent-first selective access, page provenance, bounded search/read/render flow, text-trust warning, permissive PDFium direction | semantic/vector stack, OCR, corpus cache, table/chart extraction in v0.1 |
| `AryanBV/pdf-toolkit-mcp` | dependency containment, predictable bounded tools, vision rendering | broad PDF write/edit/encrypt API |
| `espresso3389/pdf-splitter-mcp` | compact random-access operation surface: info/range/search/outline/render | Bun runtime and generic standalone-MCP packaging |
| `paradyno/pdf-mcp-server` | PDFium architecture and path/caching boundary ideas | Rust/C++ build/runtime and broad URL/manipulation surface |
| `nfsarch33/pdf-mcp-server` | research-only negative/feature reference for OCR/forms/table-image/security taxonomy | PyMuPDF-dependent implementation is rejected because that runtime dependency carries AGPL obligations |

## Historical failed v0.5 backend choice — NOT CURRENT SPEC

The following pypdf design is retained only as failure provenance for v0.5/v0.5.1. **Do not implement or reinstall it as the v0.6 backend.** The current normative backend contract begins at `## v0.6 design candidate` below.

The v0.5 text/structure backend started from the reviewed pure-Python `pypdf==6.16.2` universal wheel, SHA-256 `c8b09a59399062fb45a1b8156c18a787a10a3dae03ac9674397a226712c94604`, published through PyPI Trusted Publishing from `py-pdf/pypdf@1da09878621e5286e00510e4d378dfdf8940e541` under BSD-3-Clause. After exact wheel verification, the installer applies one narrowly scoped compatibility modification (`pypdf-reader-no-xmp-xml-v1`) to the two eager `XmpInformation` imports in `pypdf/_reader.py` and `pypdf/_doc_common.py`: normal hosts still import XMP; only a missing `xml`/`xml.*` module gets an XMP stub that fails if instantiated. PDF Toolkit exposes no XMP API. The installer records pre/post SHA-256 for both modified files, and runtime re-verifies the post-patch bytes from `VENDOR-PROVENANCE.json`. Runtime text inspection is vendored-only and never falls back to a host/global package.

Visual rendering is deliberately a separate fixed seam: `pdf_render.ps1` invokes Windows.Data.Pdf in an STA PowerShell process and returns only a bounded JSON file list. The caller cannot supply command text or executable paths; the manifest grants only `process.execute:powershell.exe`.

Reasons:

- the v0.4 pypdfium2/PDFium worker passed unit review but crashed the real frozen FolderBridge worker during import, first exposing a missing `ctypes.util` and then an invalid-JSON hard worker exit after the shim;
- a pure-Python text backend avoids native DLL loading inside the extension worker while still providing page count, document-info metadata, outline, text extraction and page geometry; the v0.5.1 compatibility patch deliberately disables only optional XMP metadata when the frozen host lacks `xml.dom`;
- Windows.Data.Pdf is already a locally available platform renderer and keeps visual verification out of the Python parser;
- permissive pypdf licensing avoids the PyMuPDF AGPL/commercial boundary;
- the dependency is installed into the extension's own `_vendor/` tree rather than assuming FolderBridge.exe is a pip environment.

## Public actions

- `status`: backend readiness/version and capability flags.
- `info`: SHA-256, bounded metadata, page count, bounded outline preview, sampled page geometry and text-layer heuristic/error state.
- `outline`: bounded bookmarks/TOC with bounded title strings and explicit truncation.
- `read-pages`: contiguous text extraction, max 50 pages/call and bounded total chars; continuation is only claimed at honest full-page boundaries.
- `search`: literal text-layer search, page provenance, original-text offsets and bounded snippets; max 500 pages/call; per-page text coverage gaps are explicit; no user regex surface.
- `render-pages`: immutable-destination Windows.Data.Pdf rendering to PNG plus optional ZIP, max 100 pages/call, 72–400 nominal DPI, pixel/byte budgets, 7200-second host-owned Job, and final `RENDER-COMPLETE.json` success marker.

No aggregate `run-all` action is exposed.

## Security / capability boundary

Runtime permissions are only:

- `workspace.read`
- `workspace.write`
- `process.execute:powershell.exe`

There is no runtime network permission. The PowerShell capability is not a generic command surface: v0.6 runtime constructs fixed argv only around approved `pdf_inspect.ps1` and `pdf_render.ps1`. Callers cannot provide script paths, assembly paths, executable paths or command text; public parameters remain bounded PDF/search/range/output/DPI data only.

All runtime file paths are workspace-relative POSIX paths. Parent traversal, absolute/backslash paths, links/reparse points, dependency/VCS/build directories, and credential/key-like names are rejected. Input must be a regular `.pdf` file. Password input, arbitrary URLs, arbitrary executable paths, and shell text are absent from schemas.

`render-pages` claims only the requested `output_dir` tree through ABI v1 `mutation_scope`, so unrelated workspace mutations need not serialize behind a PDF render. Its parent directory must already exist and the exact `output_dir` must be new; the current v0.6 contract inherits the no-overwrite rule.

The install/bootstrap scripts are **not** runtime actions. They are explicit user-run repository utilities used to fetch reviewed upstream snapshots and vendor the pinned backend before exact-hash approval.

## Context governance

The extension deliberately does not expose an unbounded whole-document read. Large PDF usage should be surgical:

1. `info` to understand page count/TOC/text-layer state;
2. `search` to locate candidate sections;
3. `read-pages` only around candidate pages;
4. `render-pages` only for visually important pages/ranges;
5. `image_open` on produced PNG files.

For scanned PDFs, `info.scan_candidate` is only a heuristic. OCR is a future optional layer and must not silently replace visual/source evidence.

## Historical failed v0.5 installation architecture — NOT CURRENT SPEC

This section records how the failed pypdf implementation was installed. v0.6 does **not** use this pypdf wheel/patch path; its normative candidate-fetch/locked NuGet installation contract is defined later.

Source tree:

`Plugins/extensions/pdf-toolkit/`

Research clones:

`local-private/pdf-toolkit-upstreams/` (gitignored)

Installed hot-load tree:

`%LOCALAPPDATA%\folderbridge-mcp\extensions\pdf-toolkit\`

`bootstrap.ps1` runs two explicit stages:

1. `fetch-upstreams.ps1` follows each reference repository's current default branch, clones/refreshes the selected research set (including the explicitly research-only nfsarch33 reference), and records resolved branch + exact commit in `UPSTREAMS.lock.json`;
2. `install.ps1` creates a temporary staged extension tree, downloads or accepts the exact reviewed `pypdf 6.16.2` universal pure-Python wheel, verifies its SHA-256, safely validates/extracts wheel members, verifies metadata/license payload, applies the exact two-file reader/XMP compatibility patch with one-anchor guards, records pre/post file SHA-256 in `VENDOR-PROVENANCE.json`, copies the fixed `pdf_render.ps1`, then moves the complete tree into the user Extension directory. Forced replacement first moves the old tree to a sibling backup directory outside the hot-scan root and restores it if cutover fails.

FolderBridge then hot-scans the installed tree. The user reviews and approves its exact directory hash + declared permissions and enables it. No FolderBridge rebuild or MCP tool re-registration is required.

## Ottawa WUDC 2027 acceptance target

First real acceptance document:

`C:\Claude\Project\Debate-Universal-Grammar\Upload\分析资料原始文件\Ottawa WUDC Debating & Judging Manual - Final Version.pdf`

Known pre-plugin baseline:

- bytes: 1,447,609
- SHA-256: `929389446CBF07637DC0DF0629C6446ED6E900ADE17DEC02E4A278121E624A3E`

Acceptance sequence after installation/approval:

1. `status` must report the locked PdfPig/.NET inspection seam `inspection_ready=true` and the independent Windows.Data.Pdf `page_render_png=true`, with overall complete-workflow `ready=true`;
2. `info` must return the same source SHA, page count, metadata/TOC and text-layer samples;
3. `search` for `counter-proposition`, `definition`, `model`, `burden`, `ordinary intelligent voter`;
4. `read-pages` on matched sections with exact page provenance;
5. `render-pages` for cover/version/TOC and relevant rule pages;
6. open rendered PNGs through FolderBridge `image_open` and visually compare against extracted text;
7. use the inspected local PDF as current WUDC evidence only after its Ottawa 2027 identity is verified against official WUDC publication metadata/source.

## Deferred features

Only add when a concrete workflow justifies them:

- OCR/Tesseract adapter
- semantic/vector search
- table/image extraction
- hidden-text geometry comparison
- persistent cache
- PDF-to-PDF visual/content comparison

These should remain separate bounded actions rather than broadening v0.6 into a generic arbitrary PDF execution surface.

## v0.6 Final Locked Spec candidate｜external .NET reader behind the existing fixed PowerShell seam

**Normative precedence:** this section and its v0.6 subsections are the only current implementation contract. Earlier v0.5 backend/installation sections are explicitly historical failure records; any conflict is resolved in favor of v0.6.

### Why the design gate reopened

Fresh runtime evidence after exact install/reapproval of v0.5.1 is authoritative over source/unit confidence:

- installed version: `0.5.1`;
- installed exact-tree SHA-256: `41ad29d62fcd853a10548d52a2b13b8f774e50d4925038954a8369c558568b1e`;
- trusted/enabled/loaded and approval not stale;
- Windows.Data.Pdf rendering capability remains ready;
- text backend still reports `ready=false`, `loaded_pypdf_version=null`, `vendor_provenance=null`, with `Could not import the approved vendored pypdf backend`.

This proves that vendoring a pure-Python package into an Extension is not sufficient isolation from a PyInstaller-frozen interpreter: imports can still require stdlib modules that were not frozen into the host executable. Continuing to discover and shim missing stdlib modules one at a time would make runtime success depend on an open-ended, host-specific compatibility list and would repeat the failed v0.4 shim pattern. v0.6 therefore changes **parser placement**, not the public API.

### Selected candidate backend

Use stable `PdfPig 0.1.16` as a read-only parser inside a fixed Windows PowerShell 5.1/.NET Framework process.

Selection facts to lock during implementation:

- PdfPig `0.1.16`, released 2026-08-22, Apache-2.0;
- use the package's `.NET Framework 4.7.1` assets;
- direct runtime package requirements for that target are `Microsoft.Bcl.HashCode >= 6.0.0` and `System.Memory >= 4.6.0`;
- candidate exact-version set uses current compatible maintenance releases rather than mechanically selecting dependency minimums: `Microsoft.Bcl.HashCode 6.0.0`, `System.Memory 4.6.3`, `System.Buffers 4.6.1`, `System.Numerics.Vectors 4.6.1`, `System.Runtime.CompilerServices.Unsafe 6.1.2`;
- `System.Memory 4.6.3` formally targets .NET Framework 4.6.2 and declares `System.Buffers >=4.6.1`, `System.Numerics.Vectors >=4.6.1`, and `System.Runtime.CompilerServices.Unsafe >=6.1.2`; those three selected maintenance versions provide compatible .NET Framework assets, with no further package dependency for their net462 target;
- all six packages must be downloaded only during an explicit candidate-fetch/review/lock or locked install/bootstrap stage, safely extracted, license payload preserved, package hashes recorded and verified before approved-tree cutover;
- runtime has no network permission and may load only package-owned DLLs whose exact path, hash and assembly identity are declared by the installed vendor provenance.

NuGet package hashes are a **pre-implementation supply-chain gate**. The first candidate acquisition is not the production installer and must not be TOFU: obtain the exact package from the official NuGet source, capture package identity/integrity metadata, independently compute SHA-256, inspect the selected TFM assets/dependencies/license, and only then freeze the reviewed package SHA-256 values into installer/tests/provenance. Production install accepts only those locked bytes. If the current-maintenance candidate set fails the real PowerShell/assembly probe, choose a different exact set only from evidence produced by that probe; do not fall back to minimum versions by assumption.

### Runtime architecture

Public actions remain exactly:

`status / info / outline / read-pages / search / render-pages`

The v0.6 manifest is a **schema-preserving migration** of v0.5.1, not an opportunity to retune caller-visible bounds. `folderbridge-extension.json` therefore freezes `schema_version=1`, `id="pdf-toolkit"`, `name="PDF Toolkit"`, `version="0.6.0"`, `entrypoint="plugin.py"`, execution `mode="isolated-process"` with the normal 600-second action ceiling, `workspace_adapter={mode:"none", state:"none"}`, and exactly the three permissions listed below. Every action keeps `authorization="global"` and `additionalProperties=false`. Apart from the manifest version/description text and backend-readiness fields returned by `status`, the public input surface is unchanged from v0.5.1:

- `status`: `read_only=true`, `requires_workspace=false`; input object has no properties.
- `info`: `read_only=true`, `requires_workspace=true`; required `path` string length 1..1024; optional `max_outline_items` integer 0..200 default 40; optional `text_sample_pages` integer 0..20 default 8.
- `outline`: `read_only=true`, `requires_workspace=true`; required `path` string length 1..1024; optional `max_items` integer 1..500 default 500.
- `read-pages`: `read_only=true`, `requires_workspace=true`; required `path` string length 1..1024, `page_start` and `page_end` integers 1..100000; optional `max_chars` integer 1024..500000 default 120000. Runtime additionally preserves the existing maximum 50-page contiguous window.
- `search`: `read_only=true`, `requires_workspace=true`; required `path` string length 1..1024 and `query` string length 1..256; optional `page_start` integer 1..100000 default 1, optional `page_end` integer 1..100000, optional `case_sensitive` boolean default false, optional `max_results` integer 1..200 default 50, optional `snippet_chars` integer 80..2000 default 360. Runtime additionally preserves the maximum 500-page search window and whitespace-only `QUERY_EMPTY` rejection without trimming an otherwise valid literal query.
- `render-pages`: `read_only=false`, `requires_workspace=true`, `run_mode="job"`, action timeout 7200 seconds; mutation scope remains ABI-v1 path claims with exactly `{param:"output_dir", kind:"tree"}`. Required inputs are `path` string 1..1024, `page_start` / `page_end` integers 1..100000, and fresh `output_dir` string 1..1024; optional `dpi` integer 72..400 default 180 and `make_zip` boolean default true. Runtime preserves the maximum 100-page range and immutable-destination/output transaction rules.

No alias action, password field, URL, regex mode, executable/script/assembly path, generic options object, aggregate `run-all`, or new caller-controlled process parameter is added. Any deliberate future change to these schemas/defaults/authorization/run mode/mutation scope is a separate public-API review rather than an incidental v0.6 backend implementation detail.

### Public response compatibility / explicit v0.6 deltas

Backend migration also preserves caller-visible **result shapes** by default. Production tests must compare v0.6 against the current v0.5.1 contract and allow only the backend-specific deltas explicitly listed here; a field must not be silently renamed merely because its producer moved from Python/pypdf to PowerShell/PdfPig.

Common inspection source identity remains exactly `path` (workspace-relative POSIX), `bytes`, and lowercase SHA-256 `sha256`. The following public response keys/nesting remain stable:

- `info`: `path/bytes/sha256/page_count/metadata/outline/outline_total/outline_items_seen_at_least/outline_truncated/outline_truncation_reasons/outline_max_depth/sample_page_sizes/text_layer_sample/text_sample_complete/text_sample_errors/scan_candidate/scan_candidate_note/content_trust_note`. `metadata` keeps `title/author/subject/keywords/creator/producer/creation_date/modification_date/format/truncated_fields`; `format` remains invariant `PDF-<major.minor>` when known. Outline preview entries keep `level/title/title_truncated/page`; page-size entries keep `page/width_points/height_points`; text-sample entries keep `page/page_chars/sample_text_chars/sample_truncated/error`. Sampling failure keeps the existing uncertainty behavior rather than deleting keys or turning it into a scanned-PDF claim.
- `outline`: `path/bytes/sha256/page_count/total_items/items_seen_at_least/truncated/truncation_reasons/max_depth/items/content_trust_note`; each item remains `level/title/title_truncated/page`, with exact total nullable when traversal is truncated.
- `read-pages`: `path/bytes/sha256/page_count/page_start/page_end/returned_pages/max_chars/response_truncated/text_truncated_pages/coverage_complete/total_truncated/next_page/pages/content_trust_note`. Each returned page remains `page/text/chars/extracted_chars/text_truncated/partial`; **the legacy public field name is `chars`**, whose value is the full normalized page code-point count, even though internal protocol fixtures may call the same concept `page_chars`. Do not rename it to `page_chars` in the public action result. `next_page`/partial semantics remain as locked below.
- `search`: `path/bytes/sha256/page_count/query/case_sensitive/page_start/page_end/pages_scanned/results/max_results/results_truncated/truncated/matches_total_in_extracted_text/matches_seen_at_least/search_window_complete/text_truncated_pages/text_coverage_complete/coverage_complete/search_mode/content_trust_note`. `truncated` remains the compatibility alias of `results_truncated`; `text_coverage_complete` remains the compatibility complement of `text_truncated_pages`, while `coverage_complete` additionally requires a complete search window. Each result remains `page/match_on_page/char_offset/char_end/snippet`, with original extracted-text Unicode code-point offsets.

`status` is the one intentionally backend-diagnostic response delta. It keeps stable general keys `ready`, `backend`, `text_backend`, `renderer`, `vendor_provenance`, `vendor_dir_present`, `powershell`, `capabilities`, `policy`, `error`, and `install_hint`, but replaces pypdf-specific pin/patch fields with the exact PdfPig seam: `inspection_ready` boolean; `pinned_pdfpig_version="0.1.16"`; `loaded_pdfpig_version` string-or-null; `pdf_inspect_script_present` boolean; `pdf_render_script_present` boolean; `casefold_unicode_version="14.0.0"`. `ready = inspection_ready && capabilities.page_render_png`. `text_backend` remains the compatibility key name and identifies `PdfPig via Windows PowerShell 5.1`; it does not imply the frozen Python worker imports PdfPig. Capability keys continue to include `metadata/outline/text_layer/literal_search/xmp_metadata/page_render_png/ocr/semantic_search/pdf_mutation`, with XMP/OCR/semantic-search/mutation false in v0.6. `policy` preserves the existing path/network/password/render and numerical limit fields and adds the explicit `parser_memory_sandbox=false` plus deterministic-casefold policy/version. `error` keeps separate `text_backend` and `renderer` readiness diagnostics so inspection failure does not erase renderer readiness. Old `pinned_pypdf_version`, `loaded_pypdf_version`, `pinned_wheel_sha256`, and `compatibility_patch` fields are removed only as this explicitly reviewed v0.6 diagnostic delta; tests must reject any other unlisted status-shape drift.

`render-pages` remains parser-independent while preserving the existing artifact-oriented shape. The Windows renderer is now the source of page count/range/nominal geometry, so top-level `page_count` is retained for compatibility but is explicitly **renderer-owned** and must equal a new explicit `source_units`; the result also exposes renderer `selected_range={start,end,unit:"page"}`. It keeps `path/bytes/sha256/page_start/page_end/rendered_pages/dpi_nominal/renderer/text_backend/render_note/total_pixels_nominal/total_pixels_actual/rendered/zip/completion_marker/workspace_artifacts`; `text_backend` is declarative configured-backend identity (`PdfPig 0.1.16`) only, and a new `inspection_backend_invoked=false` makes clear that render did not start PdfPig. Each `rendered` entry keeps `page/path/bytes/sha256/width_pixels/height_pixels/pixels/width_pixels_nominal/height_pixels_nominal`; `zip` remains null or `path/bytes/sha256`.

The final `RENDER-COMPLETE.json` marker remains last-write commit evidence but moves to **schema_version 3** because backend ownership changes: it records `complete=true`, `renderer="Windows.Data.Pdf"`, declarative `text_backend="PdfPig 0.1.16"`, `inspection_backend_invoked=false`, source identity, renderer `source_units` + `selected_range`, page_start/page_end, dpi_nominal, total_pixels_nominal/actual, artifact records, and the existing incomplete-directory warning note. A v0.6 render must not start the inspector merely to populate `page_count` or `text_backend`.

Consequently, the legacy `PDF_RENDER_SOURCE_MISMATCH` code remains reserved for compatibility but is **not emitted from a PdfPig-vs-Windows page-count comparison in v0.6**, because no inspection backend runs on the render path. Cross-backend page-count disagreement is instead the already-locked workflow state `page alignment = unresolved` when separately obtained inspection and render evidence are compared. Renderer self-inconsistency (wrong `source_units`, selected range, file count/name, DPI or JSON envelope) remains `PDF_RENDER_PROTOCOL_ERROR`; source stat-fence mutation remains `SOURCE_CHANGED_DURING_CALL`.

Runtime seams become:

1. `plugin.py` — deep policy/orchestration module only. It keeps workspace-relative path validation, link/reparse denial, source identity/SHA fences, public range/response budgets, result validation, renderer artifact validation, and host-owned process cancellation. It must have **no third-party PDF import**.
2. `pdf_inspect.ps1` — new fixed, non-user-selectable inspection script. It loads only approved vendored .NET assemblies from the Extension tree and exposes an internal bounded JSON protocol for status/info/outline/page-text/search/geometry needs. The `powershell.exe` argv contains only fixed host-owned flags plus the fixed approved script path; **no caller document/query data is placed on the Windows command line**. Python sends exactly one bounded BOM-less UTF-8 JSON request object on stdin containing protocol version + action + resolved PDF path + bounded action/range/query data. This preserves values such as leading `-`, quotes, control characters and embedded NUL in literal queries without PowerShell parameter-binder ambiguity. No command text, caller-supplied script path, assembly path or executable path is accepted.
3. `pdf_render.ps1` — existing proven Windows.Data.Pdf visual-render seam remains separate and **must not depend on PdfPig/pdf_inspect.ps1**. It is authoritative for its own PDF page count/page geometry/range validation and pre-raster pixel budgets. Python owns source confinement/SHA-stat fencing, fresh output transaction and post-render protocol/artifact validation, but no third-party parser is allowed on the render call path.

The manifest permission set remains unchanged:

- `workspace.read`
- `workspace.write`
- `process.execute:powershell.exe`

No `dotnet.exe`, `node.exe`, custom EXE, generic shell, network, URL, OCR or PDF-mutation capability is added.

`status` reports inspection and visual readiness separately. `inspection_ready` requires the locked PdfPig/PowerShell reader seam; `page_render_png` requires the independent Windows.Data.Pdf seam. Overall `ready` denotes the complete intended audit workflow and therefore requires both, but a true `page_render_png` flag must always correspond to a render path that can actually run even when the inspection backend is unavailable.

### Assembly loading and provenance contract

Installed tree adds a dedicated read-only vendor directory, e.g. `_vendor-dotnet/`, plus `VENDOR-PROVENANCE.json` schema v3. Candidate/research staging may retain full nupkg files for review, but the installed approved tree contains only the locked runtime DLL set, required package license/NOTICE payload, fixed scripts/manifest/docs/provenance, and the fixed generated Unicode case-fold runtime data + Unicode License notice. It must not copy complete nupkg archives, symbols, unrelated target frameworks, analyzers/build/ref/source assets or other development-only payload.

Before cutover, installer staging must also enforce the current FolderBridge verified Extension hard limits: at most 256 files and at most 64 MiB total tree bytes. Exceeding either limit is an install failure rather than a later rescan surprise. Tests lock these host-compatibility budgets; if FolderBridge core changes them in the future, that is an explicit compatibility review point.

### v0.6 installer transaction / hot-scan cutover contract

The v0.6 installer is a **create-then-publish transaction**, not an in-place updater. Historical v0.5 installer behavior is not relied upon implicitly; the following transaction is part of the current normative contract.

Before observing `had_previous`, creating staging, moving a live tree or downloading package bytes, the installer must acquire a **destination-scoped exclusive installer lock** outside the hot-scan root. Canonicalize the full live destination path, normalize it for Windows case-insensitive identity, derive a deterministic SHA-256 lock key from that canonical destination, and place the lock file under an installer-owned non-link directory on the destination's parent volume (for example `extension-install-locks/pdf-toolkit-<destination-key>.lock`). Open/hold the lock with an OS file handle that denies sharing (`FileShare.None`) for the entire transaction until a terminal success/failure state and any recovery paths have been recorded. Inability to acquire the handle is an immediate install-busy failure **before any staging/live-tree mutation**. The lock file itself may persist across runs; exclusivity comes from the live OS handle, so a crashed process releases ownership without PID-based stale-lock stealing. Installers targeting different canonical destinations use different lock keys and need not serialize globally.

The same destination key also owns one persistent **transaction-state directory** outside the hot-scan root and on the same volume (for example `extension-install-transactions/pdf-toolkit-<destination-key>/`). After acquiring the lock and **before inferring a new `had_previous` state**, the installer checks this directory. A **nonterminal** (`prepared` / `old_backed_up` / `new_published`) journal from an earlier process is `INSTALL_RECOVERY_REQUIRED`: the new invocation must not create a new staging tree, reinterpret an absent live destination as a fresh install, move/delete any recorded recovery tree, or otherwise mutate the live destination. Terminal `aborted` / `committed` residue is handled only by the explicit terminal-residue rules below. The installer reports the canonical destination plus recorded staging/backup/quarantine paths and their current existence for explicit recovery whenever recovery is required. It never treats PID age, missing process ownership or elapsed time as permission to discard an unfinished transaction.

For a new transaction, create the state directory and atomically write a bounded BOM-less-UTF-8 `transaction.json` (temp file + same-directory rename) containing at least schema version, destination key/canonical destination, transaction id, `had_previous`, phase, and the exact staging/backup/quarantine paths as they become known. Phase updates are atomic and occur at transaction boundaries. Nonterminal phases are exactly `prepared`, `old_backed_up`, and `new_published`; terminal phases are `aborted` and `committed`. The filesystem topology remains the source of truth if a crash occurs between a namespace move and the following journal update, so a mismatched/torn/unknown journal is recovery-required rather than guessed through. `committed` may be written only after all live v0.6 postconditions pass. `aborted` may be written only after a handled failure has proven that the live namespace is back in the transaction's initial safe shape: when `had_previous=true` the old tree has been restored to the live destination and the backup name is no longer occupied; when `had_previous=false` the live destination is absent. A rollback/quarantine failure never writes `aborted`.

A later invocation distinguishes terminal residue from incomplete recovery. A valid `committed` residue first revalidates the live v0.6 manifest/provenance/inventory/hash postconditions; only if those still pass may it clean outside-root staging/backup/quarantine residue and remove the journal, then begin a new transaction. A valid `aborted` residue may be retired only if its recorded initial-state topology still holds (`had_previous=true` => live destination exists and recorded backup path is absent; `had_previous=false` => live destination absent); then outside-root staging/quarantine residue is cleaned best-effort and the journal may be removed before a new transaction. Any terminal-state topology mismatch, committed-state revalidation failure, nonterminal journal, torn/unknown journal, or unexpected recorded recovery path is `INSTALL_RECOVERY_REQUIRED`. This journal is installer recovery state only and is never copied into the approved Extension tree.

1. Build the complete candidate Extension tree in the transaction's unique staging path, **outside the FolderBridge hot-scan Extension root but on the same filesystem volume as the destination**, so the final publish can be one directory rename/move rather than a recursive copy into the live tree. The staging path must itself pass the same link/reparse/path-safety rules used for installer-controlled files.
2. Before the live destination is touched, staging must contain the final `0.6.0` manifest/runtime/scripts, exactly the Gate-B-locked NuGet DLL inventory, Unicode fold asset, licenses/NOTICE and schema-v3 provenance. The installer must verify all locked nupkg hashes, selected TFM/dependency groups, extracted DLL SHA-256 + assembly identities, generated semantic-data hashes, expected-file inventory, no unknown runtime DLL/data asset, and the actual staged 256-file / 64-MiB host limits. Any handled failure here leaves the live Extension untouched, atomically records terminal `aborted`, cleans staging best-effort outside hot-scan, removes the terminal journal/state directory when cleanup reaches a safe end state, and then returns the install failure. If journal/state cleanup itself is interrupted, the next invocation processes the valid `aborted` residue under the rule above rather than calling it an incomplete rollback.
3. The `had_previous = live pdf-toolkit destination exists` value captured **after prior-journal handling and before the new journal/staging are created** is immutable for the transaction and is the value recorded in `transaction.json`. A normal install with `had_previous=true` fails before live-tree mutation and records/cleans a terminal `aborted` transaction; with `-Force`, an existing live tree may be replaced only after that tree is moved as a **whole directory** to a unique sibling backup directory outside the hot-scan root on the same volume. `-Force` with `had_previous=false` is still a fresh-install transaction and creates no synthetic/empty backup. The installer must never re-infer `had_previous` after staging, backup or publish begins, and must never delete or overwrite individual files in the live tree as its update mechanism.
4. Publish the fully verified staging tree by one directory move/rename to the exact live `pdf-toolkit` destination. FolderBridge may therefore observe either no `pdf-toolkit` tree or one complete old/new tree during cutover, but never an intentionally file-by-file half-built v0.6 tree.
5. After publish, perform bounded postconditions against the live tree before declaring success: re-enumerate the expected installed inventory, recheck the staged/live file-count and byte budgets, and reverify every locked runtime DLL/data hash plus manifest/provenance identity. A publish or postcondition failure uses a two-branch state machine and **namespace moves only**. If any failed/uncertain new live destination exists, first move that whole directory to a unique same-volume quarantine directory outside the hot-scan root; the installer must not recursively delete or file-by-file mutate it in place. If that quarantine rename fails, stop immediately; when `had_previous=true` preserve the old backup, and when `had_previous=false` report that the fresh-install live destination could not be evacuated. In neither branch may the installer start partial deletion. Once the live destination name is free: (a) when `had_previous=true`, move the preserved old backup back as one whole-tree restore; if restore fails, stop and preserve both recovery trees; (b) when `had_previous=false`, leave the live destination **absent**—there is no rollback tree to synthesize. Only after one of those safe initial-state topologies is established does the installer atomically write terminal `aborted`. A quarantined failed-new tree from that successfully completed rollback/fresh-install evacuation is then deleted only outside the hot-scan root on a best-effort basis; cleanup failure does not convert the failed install into success and its quarantine path is reported for manual cleanup. The terminal journal/state directory is removed after safe cleanup; if process death interrupts that cleanup, the next invocation recognizes `aborted` residue. All incomplete rollback states surface an explicit install/rollback failure, retain a nonterminal journal, and perform no further live-tree mutation.
6. On success, atomically write terminal `committed` only after all live postconditions pass. A successful replacement may then delete the old backup; a successful fresh install has no backup. Temporary downloads/staging and any no-longer-needed outside-root quarantine are cleaned best-effort, after which the committed journal/state directory is removed. If process death leaves a committed residue, the next invocation follows committed revalidation rather than treating it as an unfinished rollback. Installer failure must not silently leave recovery material inside the hot-scan root or report success while rollback/revalidation is unresolved.
7. Successful filesystem publish is **not trust approval**. The installer must instruct FolderBridge to rescan, show the newly computed exact Extension tree hash + permissions, require local review/reapproval of that exact tree, and only then enable/use v0.6. A prior v0.5 approval, old hash, or temporarily preserved enabled state must not be treated as approval of the replacement bytes.
8. Network acquisition is installer/bootstrap-only. The installed runtime must not download NuGet/Unicode/license content, repair missing vendor files from the network, fall back to another package version/TFM, or modify machine-wide CLR/GAC/config state. Missing/mismatched runtime assets remain fail-closed readiness/errors until an explicit reviewed reinstall produces a newly approved tree.

Tests for production TDD must attack at least: two installers targeting the same canonical destination cannot both enter the transaction and the loser fails before mutation, while distinct destination lock keys do not create an accidental global lock; simulated process death after journal creation, after old-tree backup, after new publish, after `aborted`, and after `committed` leaves deterministic outside-root state; a subsequent installer with a nonterminal journal refuses to infer a new `had_previous` or mutate live state; valid committed residue is cleaned only after live postconditions revalidate; valid aborted residue is retired only when its initial-state topology still holds; torn/unknown/mismatched terminal state fails recovery-required; a handled pre-cutover failure records `aborted` and does not poison the next install as recovery-required; a successfully completed rollback/fresh evacuation records `aborted`, while rollback failure leaves a nonterminal journal; pre-cutover hash/inventory failure leaves the old tree untouched or, for a fresh install, leaves the destination absent; forced replacement moves the old tree outside hot-scan before publish; `-Force` with no previous tree follows the fresh-install branch and creates no backup; injected replacement publish failure restores the exact old tree; injected fresh-install publish/postcondition failure evacuates any new tree whole and ends with the live destination absent; injected postcondition failure with a previous tree first whole-tree-quarantines the failed new tree and then rolls back; quarantine-rename failure performs no recursive live deletion and preserves/reports any old backup; old-tree restore failure preserves/reports both recovery trees; no file-by-file live overwrite or recursive-live-delete rollback path exists; and install success still requires rescan/exact-hash reapproval before runtime acceptance.

The provenance must identify, at minimum, for every selected NuGet package:

- package id and exact version;
- official package URL/source;
- package integrity hash used by the installer;
- selected target framework (`net471` where applicable);
- every runtime DLL copied into `_vendor-dotnet/`, including SHA-256 and assembly identity;
- preserved license/notice files and their source package;
- Unicode casefold contract: Unicode release/version, exact `CaseFolding.txt` source URL/hash, generated runtime asset path/hash, and Unicode License v3 notice;
- exact expected DLL/data set: unknown additional runtime DLLs or semantic data assets fail closed.

CLR default resolution/GAC behavior is not itself a trust proof, so `AssemblyResolve` is only a secondary mechanism. Before parser use, `pdf_inspect.ps1` must:

1. distinguish platform/.NET Framework assemblies from every package-owned vendored assembly declared in provenance;
2. detect already-loaded assemblies with the same package-owned identity and fail if their verifiable location is not inside approved `_vendor-dotnet/`;
3. recompute SHA-256 of every expected vendored DLL and reject unknown/missing/mismatched DLLs;
4. preload package-owned assemblies from provenance-declared approved paths in the exact deterministic order frozen by the Gate B concrete lock below, using **`Assembly.LoadFrom` only**. Gate A's earlier loader-strategy flexibility is now resolved; `Assembly.Load(bytes)` / no-context loading may remain research evidence but is not a production fallback;
5. immediately verify each loaded assembly's `FullName` and `Location`; `Location` must resolve inside approved `_vendor-dotnet/` and correspond to the already hash-verified provenance path. Any empty/outside/global/GAC package-owned location or identity mismatch fails closed;
6. after all twelve approved DLLs have been verified and loaded, register at most one bounded `AssemblyResolve` handler using only the two exact FullName redirects frozen by Gate B below; every other unresolved or unexpected package-owned dependency fails closed.

The script must never use workspace/current-directory/PATH/global-package directories as dependency search roots and must never accept a GAC/global copy for a package-owned assembly. Platform/.NET Framework assemblies may come from the OS runtime and are outside Extension package provenance by design.

The PowerShell process therefore has an explicit platform dependency: Windows PowerShell 5.1 plus a .NET Framework runtime capable of the selected `net471` PdfPig asset. `status` and the feasibility probe must detect/report whether the host meets at least the .NET Framework 4.7.1 baseline before third-party assembly loading. Unsupported hosts fail with a clear platform-runtime capability error rather than falling through to opaque assembly-load exceptions.

### Text/evidence semantics

For human-readable extracted text, require PdfPig's documented `ContentOrderTextExtractor.GetText(page)` rather than raw `page.Text` because PdfPig explicitly warns that internal content order is often not reading order. Documentation is not enough to prove packaging: candidate-fetch must inspect the stable nupkg asset list/assembly metadata to identify the exact DLL containing `UglyToad.PdfPig.DocumentLayoutAnalysis.TextExtractor.ContentOrderTextExtractor`, and the same-PowerShell feasibility probe must resolve and call that exact type from the locked vendor set. If the stable package payload does not expose it without an undeclared dependency, that is a design/feasibility failure to resolve explicitly; v0.6 must not silently downgrade to raw ordering.

Public text-coordinate semantics remain backend-independent: `page_chars`, `extracted_chars`, `char_offset`, and `char_end` are Unicode scalar/code-point counts/indices, matching the prior Python-string contract rather than .NET UTF-16 code-unit indices. PowerShell must use surrogate-aware counting/mapping wherever internal APIs return UTF-16 positions. Internal budget checks may be conservatively stricter in UTF-16 units, but reported evidence coordinates cannot silently change units.

Before scalar counting, case folding, snippet construction or JSON serialization, the inspector must validate that every evidence string it is about to expose contains only well-formed UTF-16 sequences. An unpaired high/low surrogate must never be left to the UTF-8 encoder's replacement fallback because that would silently change text and invalidate offsets. Backend migration does not introduce a new 'skip bad page and continue' policy: in `read-pages` or `search`, malformed UTF-16 (like any selected-page extraction failure in v0.5.1) fails the whole action with an explicit bounded text-extraction/invalid-Unicode error; no partial search evidence is returned. `info` retains its existing sampled-page uncertainty mechanism, so a malformed sampled page is recorded in `text_sample_errors`, makes `text_sample_complete=false`, and leaves `scan_candidate=null`. Malformed metadata or TOC document strings fail the corresponding `info`/`outline` action explicitly rather than silently replacing the code unit or inventing a new field-level output shape. Feasibility/protocol fixtures include both valid supplementary-plane characters and deliberately unpaired surrogate cases.
Existing evidence rules remain:

- metadata/bookmarks/extracted text are document-supplied and untrusted;
- critical or layout-sensitive claims require `render-pages -> image_open` visual verification;
- no OCR claim is inferred from a text miss;
- search results distinguish result-list truncation from text-coverage gaps;
- case-insensitive literal search uses an **Extension-owned deterministic Unicode case-fold table**, not host NLS/`.NET CompareInfo` as its semantic source. Gate A feasibility first records the Unicode-data baseline underlying the current v0.5.1 Python `str.casefold()` behavior and selects the released Unicode `CaseFolding.txt` version that reproduces that baseline; the candidate is **not** pre-fixed to the newest Unicode release. Candidate lock freezes that exact source version/SHA-256, generates only the locale-independent default full-fold mapping required for caseless matching, and records the generated runtime asset SHA/version in the approved tree/provenance. A newer Unicode fold version is a deliberate public semantic migration requiring explicit spec review, not an incidental backend change. Unicode Data Files are distributed under the Unicode License v3 (`Unicode-3.0`), whose notice/license must accompany the derived mapping asset;
- PowerShell folds Unicode scalars through that fixed table and builds folded-index -> original-code-point mapping, preserving expansion cases such as `Straße` / `STRASSE`; unmapped scalars map to themselves. Case-sensitive search bypasses the table;
- `status` reports the fixed `casefold_unicode_version`; changing the Unicode fold dataset is an explicit Extension version/tree change and reapproval event, never an OS-driven semantic drift;
- all document strings and text responses remain bounded.

### Backend-independent behavioral compatibility contract

The v0.6 backend migration must preserve the observable v0.5.1 **policy/coordinate/control semantics** unless this normative section explicitly changes them. This does not promise byte-for-byte equality of parser-derived page text across pypdf and PdfPig: whitespace, line breaks and reading order can legitimately differ because v0.6 deliberately uses PdfPig `ContentOrderTextExtractor`. Extracted text remains untrusted document-derived evidence. Tests therefore lock the public algorithms/shape/bounds/provenance semantics below, while real-PDF acceptance separately validates substantive extracted text against rendered pages. A parser difference that changes a rule sentence, term location or page alignment is evidence requiring visual/source review, not something the compatibility layer may silently rewrite merely to match old pypdf golden text.

Tests must lock at least the following behavior at the public `handle()` seam and again across the PowerShell protocol fixture:

- `info.page_count` and `outline.page_count` are document page counts using the public 1-based page-number space. `info.sample_page_sizes` samples at most 6 evenly distributed pages using the existing first/last-inclusive sampling rule; each item reports 1-based `page`, `width_points`, and `height_points`, rounded to 3 decimals. To preserve the v0.5.1 pypdf `mediabox` contract, PdfPig implementation must derive these dimensions from `Page.MediaBox` rather than `Page.Width/Height` or CropBox-visible bounds; PdfPig explicitly treats `Width/Height` as rotation/CropBox-visible geometry. A backend migration must not silently relabel pixels/DIPs/cropped-visible geometry as MediaBox points.
- `info.metadata` preserves the existing bounded field set: `title`, `author`, `subject`, `keywords`, `creator`, `producer`, `creation_date`, `modification_date`, `format`, plus `truncated_fields`. Each normal metadata value remains capped at 4,096 Unicode code points; missing values remain empty rather than causing field-shape drift. `format` remains culture-invariant `PDF-<major.minor>` (for example `PDF-1.7`), never a locale-sensitive numeric rendering such as `PDF-1,7`. Malformed-Unicode metadata follows the action-level fail-closed rule above rather than silent replacement or a new field shape.
- outline entries remain `{level,title,title_truncated,page}` with 1-based nesting `level`, 1-based destination `page` or `null`, a 512-code-point title cap, and a maximum traversal depth of 15. If max depth or max items prevents complete traversal, exact total is not fabricated: `total_items`/`outline_total` is `null`, `items_seen_at_least` records observed items, and truncation reasons distinguish `max_depth` from `max_items`.
- `info.text_layer_sample` uses the existing evenly distributed sample selection up to `text_sample_pages`; an extraction error yields an explicit sampled-page error and makes `text_sample_complete=false` and `scan_candidate=null`. A failed sample must not be reclassified as evidence that the document is scanned. `scan_candidate=true` remains only a heuristic when every selected sample completed and all selected extracted text was empty/whitespace.
- `read-pages` validates a contiguous range of at most 50 pages. Whole pages are returned while they fit the response budget. A later page that would overflow the remaining budget is **not** partially returned; `next_page` points to that page. Only when the first requested page itself exceeds the response budget may that first page be partial, in which case `partial=true` and no fake resumable `next_page` is advertised for the remainder of that same page.
- `read-pages.coverage_complete` is true only when there was no response truncation, no parser text-cap truncation and no partial returned page. `response_truncated`, `text_truncated_pages`, `total_truncated` and `next_page` keep their existing distinct meanings.
- `search` is literal, not regex. Validation rejects a query whose `.strip()`/whitespace-only check is empty, but it does **not** trim or normalize an otherwise valid query before matching; leading/trailing spaces in a non-empty query remain part of the literal needle. Case-sensitive search uses exact extracted-text code points. Case-insensitive search folds **both** the original validated query and extracted page text through the same locked full case-fold mapping and maps match coordinates back to original extracted-text Unicode code-point indices.
- `search` uses non-overlapping match progression: after one match, scanning resumes after the full folded needle rather than one code point later. `match_on_page` is 1-based within each page.
- `search` stops as soon as one match beyond `max_results` is observed. At that point `results_truncated=true`, `search_window_complete=false`, `matches_total_in_extracted_text=null`, and `matches_seen_at_least` reports the number actually observed. It does not scan a dense tail merely to compute an exact total.
- `search.text_truncated_pages` describes parser text-cap coverage gaps independently of result-list truncation. `coverage_complete` is true only when both the requested search window completed and no selected page had text-cap truncation.
- `snippet_chars` remains an approximate context-width control around original extracted-text code-point coordinates; snippets normalize embedded newlines/whitespace for compact evidence display and must never be used as the authoritative offset source.
- `info`, `outline`, `read-pages` and `search` continue to return source SHA/size identity and fail the whole call if the source stat fence changes before completion; no partial evidence is accepted after a source-change signal.

Backend-native UTF-16 indices, alternate overlapping-search defaults, different continuation schemes, or approximate case-insensitive comparison are implementation details that must not leak into this public contract.

### Process/output boundary

All inspection actions run in an owned PowerShell child process with the same process-tree termination/cancel discipline already used by rendering. Public inspection actions retain the existing 600-second host ceiling; the inner inspector runner uses a 570-second business timeout so controlled timeout/error/source-fence handling completes before the outer host fail-safe. **Before spawning `powershell.exe`, Python must first check `context.job_cancel_path`; an already-requested cancel returns `PDF_INSPECT_CANCELLED` without creating a child process.** After spawn, Python supervises the owned process rather than waiting in one unbounded blocking call: every supervision interval checks `context.job_cancel_path`, and active cancel or 570-second expiry terminates the owned child process tree and returns explicit `PDF_INSPECT_CANCELLED` / `PDF_INSPECT_TIMEOUT` semantics. The FolderBridge outer timeout remains the final ownership backstop.

Inspector stdout/stderr must be **concurrently drained while the child is running** using plugin-local bounded capture; the implementation must not wait for child exit before reading pipes. Inspection uses its own caps rather than reusing the renderer's historical 1 MiB stdout cap: `INSPECT_STDOUT_LIMIT = 8 MiB` and `INSPECT_STDERR_LIMIT = 256 KiB`. The 8 MiB stdout ceiling is deliberately above the worst-case legal 500,000-code-point read/search JSON envelope while remaining well below FolderBridge's 32 MiB Extension worker response ceiling. Once either ceiling is exceeded, the runner stops retaining additional bytes, terminates the owned PowerShell process tree promptly, drains/joins the capture path only within a bounded cleanup interval, and returns `PDF_INSPECT_PROTOCOL_TOO_LARGE`. This prevents Windows pipe backpressure deadlock and prevents a pathological parser/script from inflating plugin memory before a post-exit size check. The feasibility suite includes (a) maximum-legal responses containing multibyte/supplementary/control-character JSON expansion that must remain below 8 MiB and round-trip successfully, and (b) intentional over-limit stdout/stderr fixtures that would deadlock a non-draining supervisor.

Inspector stdin is a separate bounded protocol surface: Python serializes one request object as BOM-less UTF-8, enforces a 64 KiB request-byte ceiling, writes it to the child's stdin, then closes stdin. PowerShell reads raw standard-input bytes through strict UTF-8 decoding (invalid sequences fail), parses exactly one JSON object, rejects unknown fields/protocol versions/type/range mismatches, and never interprets request values as PowerShell source. The fixed argv contains no user data.

`pdf_inspect.ps1` explicitly sets console output to BOM-less UTF-8 before emitting any evidence. The internal protocol has explicit stdout/stderr byte ceilings and exactly one UTF-8 JSON result envelope. Python decodes retained inspector stdout/stderr with strict UTF-8; invalid byte sequences are protocol failures and are never silently replaced. Any extra stdout, invalid JSON, unexpected fields, non-finite values, page-count/range mismatch, or protocol version mismatch fails closed.

### Public error taxonomy and controlled inspector envelope

The backend migration must not make callers guess whether the same public failure became a different error merely because parsing moved to PowerShell/.NET. All unchanged v0.5.1 validation/workspace/path/source/render error codes remain part of the compatibility contract. In particular, inspection actions preserve `PAGE_RANGE_INVALID`, `PAGE_RANGE_TOO_LARGE`, `QUERY_EMPTY`, `SOURCE_CHANGED_DURING_CALL`, `PDF_OPEN_FAILED`, `PDF_PASSWORD_REQUIRED`, `PDF_TEXT_EXTRACT_FAILED`, and `PDF_PAGE_GEOMETRY_FAILED`. Render preserves its existing `PDF_RENDER_*`, output-path and DPI **code namespace** except for the explicitly reviewed ownership delta already frozen above: `PDF_RENDER_SOURCE_MISMATCH` remains a reserved compatibility code but v0.6 does not emit it from a PdfPig-vs-Windows page-count comparison because the inspector is never started on the render path; renderer self-inconsistency is `PDF_RENDER_PROTOCOL_ERROR`, and later cross-backend disagreement is workflow-level `page alignment=unresolved`. All other unchanged render error triggers/codes remain stable. Malformed UTF-16 in any document-derived string that the action would expose is classified as `PDF_TEXT_EXTRACT_FAILED` with bounded details identifying the affected surface (`page_text`, `metadata`, or `outline`) rather than inventing a backend-specific replacement-character result or leaking a raw CLR exception.

Backend/provenance failures are also frozen: absent provenance is `PDF_VENDOR_PROVENANCE_MISSING`; oversized/malformed provenance JSON/schema is `PDF_VENDOR_PROVENANCE_INVALID`; declared package/DLL/data-set/hash/identity mismatch, missing or unknown package-owned runtime asset is `PDF_VENDOR_PROVENANCE_MISMATCH`; an already-loaded package-owned identity outside the approved vendor root or any attempted global/GAC substitution is `PDF_BACKEND_UNTRUSTED`; unsupported/missing PowerShell/.NET runtime, missing fixed inspector script, or inability to load an otherwise provenance-valid approved assembly set is `PDF_BACKEND_UNAVAILABLE`; a loaded approved-path assembly whose verified `FullName` does not match the locked identity is `PDF_BACKEND_VERSION_MISMATCH`.

The v0.6-owned process/protocol codes are exactly `PDF_INSPECT_CANCELLED`, `PDF_INSPECT_TIMEOUT`, `PDF_INSPECT_PROTOCOL_TOO_LARGE`, and `PDF_INSPECT_PROTOCOL_ERROR`. `PDF_INSPECT_PROTOCOL_ERROR` covers a child crash/non-zero exit without a valid controlled envelope, invalid UTF-8, extra stdout, malformed/unexpected JSON, protocol/schema/range/count mismatch, or other internal transport result that cannot be trusted. Raw PowerShell stderr is diagnostic-only, bounded by the inspector stderr cap, and is never forwarded verbatim as the public error message.

Every **controlled** inspector completion writes exactly one stdout JSON object and no stderr. Its top-level shape is one of:

- success: `{ "protocol": 1, "ok": true, "result": { ... } }`;
- controlled failure: `{ "protocol": 1, "ok": false, "error": { "code": "...", "message": "...", "details": { ... } } }`.

Controlled success and controlled domain/backend failures exit the PowerShell process with code `0`; Python validates the envelope first and then raises the declared public `ExtensionError` for `ok=false`. `error.code` is restricted to the frozen public inspection/backend taxonomy above, `message` is bounded human-readable text, and `details` is a bounded JSON object with no arbitrary exception/stack dump. Unknown error codes, missing/extra envelope fields, non-zero exit, or an envelope inconsistent with the requested action fail as `PDF_INSPECT_PROTOCOL_ERROR`. Pre-start cancel is handled before process creation and therefore has no inspector envelope.

`status` is the one non-throwing readiness surface: an unavailable/untrusted inspection backend yields `inspection_ready=false` and bounded inspection error code/message data while independently probing/reporting `page_render_png`; `ready` remains the conjunction of both capabilities. A broken inspection backend must not force `page_render_png=false` or make an otherwise available `render-pages` path invoke the inspector.

The parser process itself is the first **response/CPU-time** resource-boundary enforcement point; it must never emit whole-document or otherwise unbounded text for Python to trim later. `pdf_inspect.ps1` must enforce the existing public semantics internally: bounded metadata fields; bounded outline item/depth/title traversal; info text samples only; read-pages page window/per-page text cap/total response-char cap with honest full-page continuation; search maximum 500-page window, per-page text cap, literal-only matching, bounded snippets, result cap + one-match early stop, and explicit text-coverage/result-truncation flags. The Python wrapper independently revalidates the returned counts/ranges/string lengths/schema but must not be the first place those limits take effect.

These limits are **not a parser-memory sandbox**. PdfPig may allocate substantial memory while opening, decoding object streams, fonts or page content before the script can apply returned-text caps; the separate PowerShell process is owned/killable but is not placed in a Windows Job Object with a committed-memory limit, AppContainer, low-integrity token, VM or container. The existing 512 MiB input cap and 570-second inspector deadline bound file size/time, not peak memory or parser exploit impact. `status.policy` must expose the exact boolean field `parser_memory_sandbox=false`, and published README/security text must state the same limitation unambiguously; hostile/untrusted PDFs remain a VM/container-grade isolation use case. v0.6 must not claim that process separation itself makes malicious PDFs safe.

A parser call does not write workspace artifacts. It does not accept a password parameter and must fail closed on encrypted/password-required documents rather than guessing credentials. XMP remains outside the public capability even though PdfPig can expose it; v0.6 uses bounded document information metadata only. `render-pages` remains the only public PDF Toolkit action that mutates workspace files.

`render-pages` bypasses the inspection backend completely. `pdf_render.ps1` must obtain `PageCount` and each selected `PdfPage.Size` from Windows.Data.Pdf itself, reject invalid requested ranges and 30M-per-page / 200M-total pixel budgets before raster allocation, then return the already-defined bounded renderer protocol. Python may create the fresh output leaf before invoking the renderer; any range/open/preflight failure is followed by normal best-effort removal of that call-owned directory. This is preferable to coupling visual rendering to a parser solely to validate page count first.

Parser independence does **not** imply automatic page-number equivalence across PdfPig and Windows.Data.Pdf. Inspection results (`info/search/read-pages`) must expose the inspected source SHA/bytes and inspection `page_count`; render results must expose the same Python-captured source SHA/bytes plus renderer-owned `source_units`/selected range. When a render is used as visual verification of an extracted page, the audit workflow must compare both source identity and page count first. If the source identity differs, or if PdfPig `page_count != Windows.Data.Pdf source_units`, page alignment is `unresolved` and the rendered page must not be claimed as verification of the extracted page number. This comparison belongs to workflow/evidence validation, not to render availability: `render-pages` remains callable when the inspection backend is unavailable, and it must not start PdfPig merely to establish this cross-backend check.

### Feasibility gate before implementation

Before changing the production runtime, TDD must first prove the environment seam with a deterministic test/spike that runs the **same Windows PowerShell executable used by FolderBridge** and loads the exact selected net471 assemblies from a temporary vendored fixture. The probe must establish:

1. PowerShell/.NET platform preflight reports a runtime satisfying the selected net471 baseline;
2. candidate-fetch has identified and locked the exact nupkg hashes, selected TFM assets, runtime DLL set, assembly identities and license payloads;
3. at least the candidate loader strategy (initially exact-path loading; an official no-context/byte-load route may be compared if needed) can load the complete locked package-owned assembly set, call across the required types without casting/type-identity failures, and produce an inventory proving no global/GAC package-owned substitution;
4. PdfPig can open deterministic fixtures covering the complete public inspection primitive set: page count, page geometry, bounded document-information metadata, bookmark/outline traversal and page text. Geometry fixtures include a cropped/rotated page and explicitly prove that public `width_points/height_points` come from `Page.MediaBox`, not PdfPig CropBox-visible `Page.Width/Height`; metadata fixtures also prove culture-invariant `PDF-<major.minor>` formatting. The fixture with bookmarks/metadata must prove the exact public APIs/types needed by `info`/`outline`, not merely infer them from documentation;
5. `ContentOrderTextExtractor` is proven present in the actual stable package payload and callable from the selected vendor set, and the text fixture confirms it is the path used for public page text rather than raw `page.Text`;
6. a deterministic encrypted/password-required fixture proves the no-password public contract fails closed with an explicit bounded error and does not accidentally open with an empty/default password;
7. removing or byte-tampering one required vendored DLL fails closed before parser API use;
8. preloading an outside-vendor assembly with a colliding package-owned identity, or making a vendored dependency absent while a global/GAC candidate exists, does not satisfy the dependency;
9. multilingual protocol fixtures round-trip Chinese, accents, `ß`, JSON control characters and supplementary-plane characters through BOM-less UTF-8 with strict decoding;
10. the feasibility runner records the current v0.5.1 Python Unicode-data/casefold baseline, the locked Unicode fold asset is generated reproducibly from the matching released `CaseFolding.txt`, and an equivalence corpus derived from that file confirms the generated PowerShell fold mapping matches current Python `str.casefold()` behavior for the relevant default full-fold mappings; license/provenance is present, and search fixtures preserve `Straße -> STRASSE`, Greek sigma/default folding and original code-point offsets including a supplementary character before the match;
11. the same fold fixtures produce the same results independently of current culture/OS comparison settings because host ignore-case primitives are not the semantic source;
12. bounded read/search fixtures prove the PowerShell side itself caps output and reports coverage/truncation before stdout serialization;
13. stdin protocol fixtures round-trip leading-dash queries, quotes, JSON control characters, embedded NUL and supplementary characters without putting any user value on argv; malformed/oversized/>64KiB/extra-field requests fail before parser use;
14. pre-start cancel, active cancel and a deterministic hanging inspector fixture prove the 570-second-equivalent internal deadline/cancel path terminates the owned PowerShell tree and surfaces bounded explicit errors before the outer host ceiling.

The feasibility result must freeze the concrete assembly-loader strategy in the production spec. If no Windows PowerShell 5.1 strategy simultaneously satisfies exact approved-byte provenance, dependency resolution and PdfPig type usability, v0.6 is rejected; do not modify machine-wide GAC state, `powershell.exe.config`, or add another compatibility shim to force it through.

If this probe fails, v0.6 is rejected before production implementation; do not add another compatibility shim layer.

### Gate B concrete feasibility lock｜2026-09-03

D32 and D33 exposed that the earlier probe did not execute every Gate A fixture, so the temporary/research feasibility probe was extended and rerun **fresh from official NuGet acquisition through the complete closure suite in one invocation** after each material gap. The authoritative evidence is now `local-private/pdf-toolkit-v06-feasibility/result.json`, SHA-256 `53dd3db18bfefd828dfafe3ea7a19eb347d0327cc61e6dea05b61d83d1c5c7f1`, with `overall_pass=true`. In addition to the earlier strict stdin/read/casefold/cancel fixtures, this result proves the PowerShell-side `search` window rejects a 501-page request before parser use and independently reports parser text-cap coverage (`text_truncated_pages=[1]`, `search_window_complete=true`, `coverage_complete=false`) without conflating it with result-list truncation. The prior `e254b8bfad5040789a6aa0c877283adbe38da1c802402664140b665087bd308d` and `59ebbad6d384d36e17f2979d88c89aa8cd6bcbdd01e47faa418e6a211107c59d` results remain historical evidence only and are not sufficient for the complete Gate A claim. The probe itself is local-private/non-production and is **not** part of the approved Extension tree. These facts are now frozen for Gate B review; changing any package version/hash/TFM, DLL set/hash/identity, Unicode dataset, loader strategy or approved redirect below reopens the locked-spec review.

The same authoritative fresh run now also closes the Gate A behavioral/protocol fixtures that were previously missing:

- strict stdin validation rejects extra fields, malformed JSON, invalid UTF-8, >64 KiB input, invalid bounded fields/ranges and whitespace-only search **before parser use**; the closure harness records `parser_touched=false` for those failures;
- a leading-dash/quoted/NUL/supplementary query round-trips as data, not argv/source text;
- parser-side bounded read semantics are executed before serialization: a later whole page that cannot fit is omitted with `next_page` pointing to it; only an oversized first requested page may be partial with `next_page=null`; the 1,000,000-code-point page-text cap independently marks `text_truncated_pages`/incomplete coverage;
- case-insensitive literal search executes the locked Unicode fold map on a fixture with a supplementary-plane prefix and `Straße`; query `STRASSE` maps the first match back to original code-point coordinates `char_offset=1`, `char_end=7`, observes the cap+one second match, sets `results_truncated=true`, `search_window_complete=false`, `matches_total_in_extracted_text=null`, and `matches_seen_at_least=2`;
- a non-empty literal query with leading/trailing spaces is preserved rather than trimmed; the fixture `" definition "` matches exactly at original code-point range `1..13`;
- a deliberately unpaired high surrogate fails explicitly as `invalid-unicode:unpaired-high-surrogate` rather than passing through UTF-8 replacement; and
- process supervision executes all three cancel/deadline states: pre-start cancel returns `reason=cancel, spawned=false`, active cancel terminates the owned process, and the deterministic hang fixture reaches the bounded timeout path.

The full repository test that hosted this fresh probe reported the probe test itself `ok`; the repository still had only the pre-existing unrelated `6001 > 5000` runtime-instructions-length failure. The temporary test bridge was restored after true Job completion to SHA-256 `4f90886fa84974f105fa4e8287f2fda02021725133dcff87d7913c4d555db77b`.

#### Locked NuGet package set

All packages were reacquired from official NuGet V3 registration/package-content endpoints in the same full probe. The registration-declared SHA-512 was compared byte-for-byte with a locally computed SHA-512 before the local SHA-256 lock was recorded.

| Package | Version | Selected TFM | Selected TFM dependency group | NuGet official SHA-512 (base64) | nupkg SHA-256 | License expression |
| --- | --- | --- | --- | --- | --- | --- |
| PdfPig | 0.1.16 | `net471` | `Microsoft.Bcl.HashCode 6.0.0`; `System.Memory 4.6.0` | `clPAR660u7oGMGO0+I4JAq3olrsbIcKO2m3JtAuOow+IbS62Pyfh00GFnG10Ngl2nizTS5+DsCSlndW6sLvNBQ==` | `d67171846ea8c28f50359137065fec4514266d7a32b23eae6c5f2ebed8ffcfc4` | Apache-2.0 |
| Microsoft.Bcl.HashCode | 6.0.0 | `net462` | none | `k0mXL9QMC7IN4nRuAHuLWykH8H/ng04IWmGSj72aqlaHztvyA6NNc90Nipuavy1iqByVwiJVoIDkNH/IlcnfLQ==` | `f3b9b2bab0bf8cc717d5fdf6d7aee3ec54e36d9e85bd41347acae161319cbd6b` | MIT |
| System.Memory | 4.6.3 | `net462` | `System.Buffers 4.6.1`; `System.Numerics.Vectors 4.6.1`; `System.Runtime.CompilerServices.Unsafe 6.1.2` | `NXcNYlWoXe5cz9sb8Huo6x2dCZVYkhwKtgE00n/MoI8V4ZI/7/t+EI5bOhQFlZfFjjqM8+U6prjU/aARt7H/tA==` | `26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955` | MIT |
| System.Buffers | 4.6.1 | `net462` | none | `qve/dFwECwehSWlZmpkrrlIeATCvo/Hw2koyMrUVcDBy5gXAQrnwX8pHEoqgj8DgkrWuWW1DrQbFqoMbo+Fvrg==` | `b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79` | MIT |
| System.Numerics.Vectors | 4.6.1 | `net462` | none | `/rkvpUeUPlCY/2qYVQKiUsj5IKaXZcy2+SQAGAfemAdyEF5AgIgYOFNSTMWDXo09JWFX9HB+wV1yCyi2Mwi3TA==` | `2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7` | MIT |
| System.Runtime.CompilerServices.Unsafe | 6.1.2 | `net462` | none | `t2aXWJZBkAkRrTOnw31OBELKEVSDD5YvC3O5dXaHFsR66/nRTKm1y3Iq6NwFI5u5IlKrWYfdan66V+GKKkY8hQ==` | `5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937` | MIT |

The earlier flattened nuspec view that showed `System.ValueTuple 4.5.0` is explicitly **not** the selected dependency contract: the enhanced probe records dependency groups by TFM and proves `System.ValueTuple` belongs to PdfPig's `.NETFramework4.6.2` group, while the selected `.NETFramework4.7.1` group contains only `Microsoft.Bcl.HashCode` and `System.Memory`. `System.ValueTuple` is therefore not vendored for this v0.6 lock.

#### Locked runtime DLL set and assembly identities

The approved `_vendor-dotnet/` runtime set is exactly the following twelve DLLs; unknown additional package DLLs fail closed.

| DLL | SHA-256 | Assembly identity |
| --- | --- | --- |
| `UglyToad.PdfPig.Core.dll` | `894bf5e8daac5e4f6fbd7e2eb26c6b2f39e42b3122e35fa69c6fa30469a43bb0` | `UglyToad.PdfPig.Core, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `UglyToad.PdfPig.DocumentLayoutAnalysis.dll` | `aa79f1774b74e5bd6939e089bb7770fd62b8e8c22d444f5f736a7171c243e16c` | `UglyToad.PdfPig.DocumentLayoutAnalysis, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `UglyToad.PdfPig.Fonts.dll` | `b066e7440e7d76d2b8229e9274e300dcfe7dcec65dd578106e8a1bf2473bb911` | `UglyToad.PdfPig.Fonts, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `UglyToad.PdfPig.Package.dll` | `ec4a85d737582d93917a4dff811267723092e60f891916dd56dc94630417b5ee` | `UglyToad.PdfPig.Package, Version=0.1.16.0, Culture=neutral, PublicKeyToken=null` |
| `UglyToad.PdfPig.Tokenization.dll` | `84315ce24887373ed9019442edfcb1b7777e7782ed0c4bf69be63e84941b43e0` | `UglyToad.PdfPig.Tokenization, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `UglyToad.PdfPig.Tokens.dll` | `d91a3f93ca27728709875ef71425d4c8e7165d5d3a7b13094ec976cfc22d305c` | `UglyToad.PdfPig.Tokens, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `UglyToad.PdfPig.dll` | `cd712f405cbd4400903d18f2855e0b2458acb76d75019765f9faaa2f3ba0717e` | `UglyToad.PdfPig, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123` |
| `Microsoft.Bcl.HashCode.dll` | `3a4e851ee5fc0f6182aa5a3d65dc56fcd6979b65334b5c3b92fbdc791457c0ab` | `Microsoft.Bcl.HashCode, Version=6.0.0.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51` |
| `System.Memory.dll` | `d5e8e4866f9cfa66f7765660f84b210198893e55335487afe5ebda342c0e913d` | `System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51` |
| `System.Buffers.dll` | `2d78d770c9cb997199154ae8c018b9f1d1efbc86729f7264dde6dbad2a12cac3` | `System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51` |
| `System.Numerics.Vectors.dll` | `20c2fa81b8c70d651099d762954f285fd4f942e63b2d7217c145dab8d4b2f4c9` | `System.Numerics.Vectors, Version=4.1.6.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a` |
| `System.Runtime.CompilerServices.Unsafe.dll` | `08cbd7278b66f1e68425a82d4b97181a4130d93e3dd91831407aba7212ccdacf` | `System.Runtime.CompilerServices.Unsafe, Version=6.0.3.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a` |

#### Locked loader strategy

Production v0.6 uses **`Assembly.LoadFrom` after exact path/hash/identity precheck**. `Assembly.Load(bytes)` also passed the research comparison but is not the production strategy and must not become an undocumented fallback.

The probe established and Gate B freezes the following exact deterministic DLL load order: `System.Runtime.CompilerServices.Unsafe.dll -> System.Buffers.dll -> System.Numerics.Vectors.dll -> System.Memory.dll -> Microsoft.Bcl.HashCode.dll -> UglyToad.PdfPig.Core.dll -> UglyToad.PdfPig.DocumentLayoutAnalysis.dll -> UglyToad.PdfPig.Fonts.dll -> UglyToad.PdfPig.Package.dll -> UglyToad.PdfPig.Tokenization.dll -> UglyToad.PdfPig.Tokens.dll -> UglyToad.PdfPig.dll`. Production must not reorder this sequence without reopening Gate B. Because PdfPig's compiled assembly references use older strong-name assembly versions than the selected compatible NuGet maintenance packages, production **must register exactly one bounded `AssemblyResolve` handler after all twelve approved DLLs have been hash/identity verified and loaded**. That handler contains only the two exact FullName redirects frozen below; it has no simple-name/version-range/global search fallback, and every other package-owned unresolved request fails closed. The redirect set is derived from `GetReferencedAssemblies()` of those exact approved assemblies and is frozen to:

- `System.Memory, Version=4.0.2.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51` -> approved `System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`;
- `System.Buffers, Version=4.0.4.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51` -> approved `System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51`.

Any other package-owned resolution request fails closed. The probe also passed: outside-vendor preloaded collision rejection, one-byte DLL tamper rejection, and a missing-vendored-DLL case where an identical external copy was preloaded but the declared vendored path was absent (`missing_vendor_pass=true`). No GAC mutation, machine-wide binding config, PATH/current-directory probing or generic dependency search is permitted.

#### Locked platform/API evidence

The feasibility host used Windows PowerShell 5.1 and reported .NET Framework release `533509`, satisfying the locked minimum `.NET Framework >= 4.7.1` platform requirement. Both load strategies could open the deterministic PdfPig fixture, but the production loader remains `LoadFrom` as above. Gate B's targeted culture/casefold closure evidence is `local-private/pdf-toolkit-v06-feasibility/gate-b-semantics.json`, SHA-256 `8e37c7275573d44a8c3aec97b31d3205fdae14841a51387d146c4e84f8afb103`. The exact locked package set proved:

- `UglyToad.PdfPig.PdfDocument` usable;
- `UglyToad.PdfPig.DocumentLayoutAnalysis.TextExtractor.ContentOrderTextExtractor.GetText(Page, bool addDoubleNewline = false)` callable via reflection with the optional second parameter supplied explicitly;
- page count 1; PDF version 1.7; while `CurrentCulture=de-DE`, explicit invariant formatting produces exactly `PDF-1.7` (never `PDF-1,7`); metadata title `Probe Title`, author `Probe Author`;
- bookmark API returns `Probe Bookmark`;
- MediaBox `612 x 792` while PdfPig visible `Page.Width/Height` is `380 x 290`, proving the public geometry seam must use `Page.MediaBox`;
- encrypted fixture fails without a password and opens only with the probe-only explicit password; the public v0.6 action surface still accepts no password and therefore fails closed.

#### Locked Unicode/protocol evidence

The v0.5.1 Python baseline is Unicode `14.0.0`. The locked default-full-fold source is Unicode `14.0.0` `CaseFolding.txt`, SHA-256 `a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f`. The generated 1,530-entry mapping asset SHA-256 is `77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504`. Unicode license source `https://www.unicode.org/license.txt` has SHA-256 `e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96`.

Gate B additionally executed the generated map in Windows PowerShell 5.1 over a 1,544-code-point corpus containing every locked C/F mapping key plus supplementary/default-fold stress fixtures. Under both `en-US` and `tr-TR`, the output was 1,666 code points and SHA-256 `fa5b0d68bd03308001ce5aba87a14d21988c3ba7d989463b3cbc5e283a321711`, exactly matching the Python/Unicode expected corpus hash. This proves the PowerShell fold implementation is driven by the locked Unicode map rather than host `ToLower`/ignore-case culture behavior.

Strict BOM-less UTF-8 stdin/stdout passed a request containing leading `-`, quotes, embedded NUL and supplementary `😀`; output round-tripped Chinese `中文`, `café`, `Straße`, newline control data and `😀`. A PowerShell 5.1 environment constraint was also proven: non-ASCII literals in a UTF-8-no-BOM `.ps1` source file may be decoded through the legacy script code page. Production `pdf_inspect.ps1` is therefore **ASCII-only at the script-source level for semantic literals**; any required non-ASCII constant/test datum is constructed from numeric Unicode code points at runtime. Do not switch to a different script-file encoding as an implementation alternative without reopening Gate B. **This does not change the protocol**, which remains strict BOM-less UTF-8 bytes on stdin/stdout.

The bounded process supervisor passed: legal 500,000-code-point JSON envelopes measured 1,500,013 bytes (CJK), 1,000,013 bytes (control expansion) and 2,000,013 bytes (supplementary), all below the 8 MiB inspector stdout cap; intentional 9,000,000-byte stdout and 400,000-byte stderr runs triggered overflow handling; deterministic timeout and cancel runs both terminated through the owned process-tree path.

#### Locked license set and installed-tree budget

The selected nupkgs expose license expressions but no standalone license/NOTICE members. The installed Extension therefore carries a shared canonical license-text set **plus the package-level attribution/copyright metadata extracted from the exact locked nupkg bytes**; it must not treat an SPDX template with placeholders as a complete package notice. Gate B package-metadata evidence is `local-private/pdf-toolkit-v06-feasibility/package-metadata.json`, SHA-256 `2a0b1f502210009eb6474bee2ed3720d5aaf25c136154fd67db8e1487ccf26f4`. Gate B locks:

- PdfPig 0.1.16: author `UglyToad`, Apache-2.0; project/repository `https://github.com/UglyToad/PdfPig`, repository commit `a7bb35662bbbf405efddad50aedc9bcdcf515afc`; nuspec has no copyright field and no packaged LICENSE/NOTICE member. Canonical Apache-2.0 text: `https://spdx.org/licenses/Apache-2.0.txt`, SHA-256 `c274f80372d90c012937370f0e1f15087d22e308ef98b27cea5dc0d2d088366c`, 10,279 bytes.
- Microsoft.Bcl.HashCode 6.0.0, System.Buffers 4.6.1, System.Memory 4.6.3, System.Numerics.Vectors 4.6.1 and System.Runtime.CompilerServices.Unsafe 6.1.2: author `Microsoft`, nuspec copyright **`© Microsoft Corporation. All rights reserved.`**, MIT. Their exact repository commits are respectively `d0c2a5a83211e271826172a6b0510c25a52dbd53`, `6b84308c9ad012f53240d72c1d716d7e42546483`, `f62ca0009b038cab4725a720f386623a969d73ad`, `6b84308c9ad012f53240d72c1d716d7e42546483`, `f62ca0009b038cab4725a720f386623a969d73ad`, all under `https://github.com/dotnet/maintenance-packages`. Canonical MIT text: `https://spdx.org/licenses/MIT.txt`, SHA-256 `c3b1b78bc8bd3ea13aa4bc9778442d16560270afa235006d816e5e88cef24db4`, 1,077 bytes. Because that canonical text contains `<year> <copyright holders>` placeholders, production must pair the permission text with the exact Microsoft copyright notice above in `NOTICE.md`/license attribution; it must never ship the placeholder as though it were the package's copyright notice.
- Unicode License text/hash as locked in the Unicode subsection.

Production NOTICE/provenance must identify package id/version, author, license expression, locked nupkg SHA-256, repository URL/commit when supplied by the nuspec, and the exact shared license text used. The five MIT packages must include the exact Microsoft copyright notice. If any later production acquisition of the same locked nupkg bytes reveals an additional package-specific NOTICE/license payload, the discrepancy is a Gate B/provenance failure rather than permission to silently substitute or omit it.

The measured feasibility payload is 17 files / 6,143,201 bytes (twelve runtime DLLs + three Unicode files + two shared package-license texts). Because production scripts/manifest/provenance are not implemented yet, they are **not** mislabeled as measured bytes: Gate B reserves a conservative additional 32 files / 524,288 bytes. The locked pre-implementation projection is therefore 49 files / 6,667,489 bytes, comfortably below FolderBridge's 256-file / 67,108,864-byte approved-tree ceilings. Production install/tests must still measure the real staged tree and fail if either host limit is exceeded; the reserve is a Gate B feasibility budget, not permission to skip exact final measurement.

#### Feasibility verdict and remaining gate

`v0.6 feasibility = PASS`. This PASS does **not** mean runtime acceptance and does not authorize user reinstall yet. It establishes that the Gate A architecture can be realized without a new executable/permission, machine-wide CLR mutation, trust relaxation, ContentOrder downgrade, Unicode semantic change, or renderer coupling.

The Final Locked Spec above must now receive two consecutive independent Gate B reviews with `0 new material findings`. Only after those two clean reviews may production TDD begin. The known unrelated repository failure `test_runtime_instructions_advertise_skill_routing_without_embedding_skill_body` (`6001 > 5000`) remains outside PDF Toolkit scope and must not be changed as part of this work.

### Runtime acceptance remains unchanged

No design/static test can replace real acceptance. After install/reapproval, PASS still requires the same Ottawa chain:

`status -> info -> search -> read-pages -> render-pages -> image_open`

The local Ottawa file must still match the known `1,447,609` bytes and SHA-256 `929389446cbf07637dc0df0629c6446ed6e900ade17dec02e4a278121e624a3e`. Official identity remains only `high-confidence official Ottawa 2027 candidate` until an official source byte-for-byte comparison is completed.
