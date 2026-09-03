# PDF Toolkit

Generic external FolderBridge Extension for bounded PDF inspection and audit workflows.

## Why this exists

FolderBridge's bundled Microsoft Office Native extension intentionally renders Office documents, not arbitrary PDFs. PDF Toolkit fills that gap without changing the MCP tool catalog: it is installed as an external hot-load Extension and invoked through the normal `extension(list/info/run)` gateway.

The design is intentionally read-mostly. It does **not** edit PDFs, accept URLs, accept arbitrary executable paths, run OCR, or expose a general shell surface.

## Actions

- `status` — backend/version/readiness and capability report.
- `info` — source SHA-256, page count, metadata, bounded outline preview, sample page sizes, and a text-layer heuristic.
- `outline` — bounded PDF bookmark/TOC extraction.
- `read-pages` — bounded contiguous text-layer extraction (max 50 pages and max 500k returned chars).
- `search` — literal text-layer search with page provenance and bounded snippets.
- `render-pages` — Windows.Data.Pdf PNG rendering for a contiguous page range (max 100 pages, 72–400 nominal DPI plus pixel/byte budgets), optional ZIP, host-owned Job, scoped workspace mutation. The destination must be a fresh directory and `RENDER-COMPLETE.json` is written last as the success marker.

For audit work the intended sequence is:

`info -> search -> read-pages -> render-pages -> visual verification`

This mirrors FolderBridge's broader principle that structural/text extraction and native visual evidence are complementary rather than substitutes.

## Backend

PDF Toolkit v0.6 uses **PdfPig 0.1.16** for inspection through a fixed Windows PowerShell 5.1 process seam. Python does not import PdfPig, pypdf, PDFium, or any other third-party PDF parser. `pdf_inspect.ps1` receives exactly one bounded BOM-less UTF-8 JSON request on stdin, loads only the twelve provenance-locked DLLs from `_vendor-dotnet/` with `Assembly.LoadFrom`, verifies their SHA-256 and assembly identities, and permits only the two reviewed strong-name redirects frozen by the production contract. Missing, extra, globally substituted, or mismatched package-owned assemblies fail closed.

Human-readable page text uses PdfPig `ContentOrderTextExtractor`; page-size evidence uses MediaBox geometry. Literal case-insensitive search is independent of host locale/NLS: the installed tree contains the exact Unicode 14.0.0 `CaseFolding.txt`-derived full-fold map, with Unicode-scalar coordinates mapped back to original extracted text. Malformed UTF-16 evidence fails closed rather than being silently replaced.

Visual rendering is a separate backend. `pdf_render.ps1` uses Windows.Data.Pdf and owns its own page count, selected range, page geometry, pre-raster pixel budgets, and rasterization. `render-pages` never starts PdfPig merely to obtain page count or geometry; its result and schema-v3 `RENDER-COMPLETE.json` explicitly record `inspection_backend_invoked=false`.

The inspector process is owned and killable, but **process separation is not a parser-memory sandbox**. The v0.6 policy reports `parser_memory_sandbox=false`: PdfPig can allocate memory while opening or decoding a pathological PDF before returned-text limits take effect. Hostile PDFs remain a VM/container-grade isolation use case.

## Open-source design references

This extension is a new FolderBridge implementation; it does not copy an upstream MCP server wholesale. The research set is intentionally split by concern:

1. `jztan/pdf-mcp` — strongest reference for agent-oriented selective access (`info -> search -> read pages -> render pages`), context-budget discipline, content-trust warnings, and current migration to a permissive PDFium stack.
2. `AryanBV/pdf-toolkit-mcp` — reference for dependency containment, bounded/stable errors, and keeping PDF render usable without a native build toolchain.
3. `espresso3389/pdf-splitter-mcp` — reference for a small random-access surface: info, range extraction, search, outline, and page rendering.
4. `paradyno/pdf-mcp-server` — reference for PDFium-oriented architecture, path sandboxing, and cache/operation boundaries.
5. `nfsarch33/pdf-mcp-server` — cloned as a **research-only negative/feature reference** for OCR/forms/table-image/security taxonomy. Its runtime stack advertises PyMuPDF/AGPL, so no PyMuPDF-dependent implementation is copied into PDF Toolkit.

The fetcher follows each repository's current default branch rather than pinning guessed branch names, and writes the resolved branch + exact commit into `UPSTREAMS.lock.json`. Research clones are not packaged into the Extension.

Run `fetch-upstreams.ps1` to create clean research checkouts under the ignored `local-private/pdf-toolkit-upstreams/` directory and record their exact local commit IDs in `UPSTREAMS.lock.json`. These research clones are not packaged into the Extension.

## Install

From PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Claude\Project\folderbridge-mcp\Plugins\extensions\pdf-toolkit\install.ps1"
```

That production installer:

1. acquires only the six Gate-B-locked NuGet packages plus the locked Unicode/license sources, verifies every package/data SHA-256 (and official NuGet SHA-512), selected dependency group/TFM, extracted runtime DLL hash and assembly identity, then builds schema-v3 provenance;
2. builds the complete candidate outside the hot-scan root on the destination volume, enforcing the FolderBridge 256-file / 64-MiB tree limits;
3. publishes with a destination-scoped `FileShare.None` installer lock and persistent `transaction.json` state. Forced replacement moves the old tree out of hot-scan as one directory, publishes the new tree as one directory move, revalidates the live tree, and on failure whole-tree-quarantines the failed new tree before restoring the old tree. It never performs file-by-file live overwrite or recursive deletion of the live destination.

`bootstrap.ps1` remains a repository research convenience that runs `fetch-upstreams.ps1` before invoking the installer; research snapshot acquisition is not required for production deployment. Installer/bootstrap networking is not a runtime capability. Runtime never downloads or repairs dependencies. A reviewed local acquisition cache may be used with `install.ps1 -ReviewedCacheRoot`; cached bytes are still required to match every frozen hash.

A successful filesystem publish is **not approval**. Open FolderBridge **Extensions & Skills**, rescan, review the newly computed exact tree hash and declared permissions, approve that exact v0.6 tree, and then enable **PDF Toolkit**. Trust from v0.5.x is never carried forward. Normal Extension hot loading does not require a FolderBridge rebuild or MCP re-registration.

Use `-RefreshUpstreams` to refresh clean research snapshots, and `-ForceInstall` only when intentionally replacing an existing installed PDF Toolkit tree.

## Security boundary

- Input and output paths are POSIX-style workspace-relative paths only.
- Parent traversal, links/reparse points, VCS/dependency/build directories, and credential/key-like paths are rejected.
- Input must be an existing regular `.pdf` file.
- No URL/network input is accepted by runtime actions.
- No PDF password parameter is accepted in v0.6; encrypted/password-required PDFs fail closed.
- PDFs are capped at 512 MiB; `read-pages` is capped at 50 pages/call; `search` at 500 pages/call; per-page returned extracted text is internally capped and coverage gaps are reported explicitly. These response caps are **not a parser-memory sandbox**; `status.policy.parser_memory_sandbox` is `false`, and hostile PDFs should still be opened only in appropriately isolated infrastructure.
- Document-supplied metadata values and TOC titles are output-bounded; malformed Unicode or extraction failure follows the locked fail-closed/uncertainty semantics rather than silent replacement.
- `search` is literal only; deterministic Unicode 14.0.0 full case-folding preserves original extracted-text Unicode-scalar offsets; there is no caller-supplied regular-expression surface.
- `render-pages` declares a tree mutation scope for `output_dir`; the parent must already exist and `output_dir` itself must be new. There is no overwrite mode.
- XMP metadata is intentionally not a PDF Toolkit capability. OCR, semantic search and PDF mutation are also disabled in v0.6.
- Render preflight caps 30M pixels/page and 200M pixels/call; PNG+ZIP bytes are capped at 512 MiB; the host-owned Job has a 7200-second ceiling. The only runtime process permission is `powershell.exe`; caller PDF/search data for inspection travels through bounded stdin JSON, and callers cannot supply command text, executable paths, script paths, or assembly paths.
- `RENDER-COMPLETE.json` is written last. A crash/cancel may leave a directory, but a directory without a valid completion marker is explicitly incomplete and must not be treated as committed evidence.
- Extracted text, bookmarks, and metadata are explicitly labeled untrusted document content. Critical claims and layout-sensitive evidence should be verified against rendered pages.

The Extension permission declaration is only an authorization contract, not a kernel sandbox. As with all external FolderBridge code, approve only reviewed bytes.

## Current non-goals

- OCR / Tesseract
- semantic/vector search
- table reconstruction
- embedded-image extraction
- PDF modification, merge/split/forms/encryption
- arbitrary URLs
- arbitrary commands/executables

These can be added later only if a real workflow requires them. The first target is reliable local evidence extraction and page visualization.
