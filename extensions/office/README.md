# Microsoft Office Native extension

Bundled FolderBridge extension for two complementary jobs:

1. **Portable OOXML inspection without launching Office**
   - `inspect_docx`: paragraphs, styles/numbering, tables, sections/page setup, headers/footers, media, hyperlinks, footnotes/endnotes/comments.
   - `inspect_xlsx`: workbook/sheet structure, selected cell values, formulas and cached values, merged ranges, hidden rows/columns, defined names, calculation settings and external-link parts.
2. **Native Windows visual rendering with locally installed Microsoft Office**
   - PPTX: PowerPoint `Slide.Export` -> PNG.
   - DOCX: a dedicated Word-only PowerShell stage performs one native full-document fixed-format export and exits completely; a second WinRT-only stage then validates the PDF page count and renders only the requested PNG pages. The renderer deliberately avoids a separate `ComputeStatistics` pagination pass and never mixes live Word COM automation with `Windows.Data.Pdf` in one process.
   - XLSX: Excel native fixed-format print layout per worksheet -> PDF -> Windows `Windows.Data.Pdf` -> one PNG per print page.
   - `width` always means final physical PNG pixels for PPTX/DOCX/XLSX. Word/Excel internally convert the pixel target to WinRT DIPs according to the current system DPI and correct any final one-pixel DIP rounding drift.

The native `render` action is write-capable and therefore requires one-time global approval in FolderBridge's Extensions sidebar. It runs as a host-owned Extension Job so Office pagination/rasterization never keeps one MCP request open for the full render duration; the initial call returns a `job_id`, and `job_status` returns the eventual render result or bounded failure. The read-only `status`, `inspect_docx`, and `inspect_xlsx` actions are bundled read-only actions and do not require global authorization.

## Security boundaries

- Only workspace-relative POSIX-style paths are accepted.
- `..`, absolute paths, links/reparse points, VCS/dependency/build directories and unsupported extensions are rejected.
- Only `.pptx`, `.docx`, and `.xlsx` are accepted by native rendering; macro-enabled Office formats are intentionally excluded.
- Office documents/workbooks/presentations are opened read-only.
- Office `AutomationSecurity` is set to `msoAutomationSecurityForceDisable` before opening files.
- Excel link updates are disabled.
- PowerShell execution is restricted to the bundled `office.ps1`, `word_export.ps1`, and `pdf_render.ps1`; no user-supplied command/script/URL parameter exists and subprocess execution uses `shell=False`.
- Word rendering snapshots the pre-existing `WINWORD.EXE` PID set before COM startup, requires exactly one new Word process for ownership, opens a verified OS process handle to that instance, and reaps only that owned instance on completion, cancellation, or timeout. Existing user Word processes are never targeted by name-wide termination.
- Intermediate PDFs are stored under the per-user FolderBridge extension state directory, not in the workspace, and are removed after the run.
- Final PNGs and optional ZIP are written only beneath the explicitly selected workspace.

## Typical calls

First discover the extension/actions with `extension(action="list")` or `extension(action="info", extension_id="office")`.

Read a Word document structurally:

```json
{
  "extension_id": "office",
  "extension_action": "inspect_docx",
  "params": {"path": "docs/example.docx", "max_items": 2000}
}
```

Read a bounded Excel region while preserving formulas:

```json
{
  "extension_id": "office",
  "extension_action": "inspect_xlsx",
  "params": {
    "path": "data/example.xlsx",
    "sheet": "Data",
    "cell_range": "A1:Z200",
    "max_items": 5000
  }
}
```

Render slides/pages/worksheet print pages and create a sibling ZIP:

```json
{
  "extension_id": "office",
  "extension_action": "render",
  "params": {
    "path": "sources/example.pptx",
    "output_dir": "renders/example",
    "page_start": 1,
    "page_end": 66,
    "width": 1920,
    "make_zip": true
  }
}
```

For Excel, optional `sheets` is an array of exact worksheet names. `page_start`/`page_end` then apply independently to each selected worksheet's native print-page PDF. If omitted, every worksheet is exported.

The `render` invocation first returns a Job record. Poll `extension(action="job_status", job_id="...")`; when the Job succeeds, its result includes source SHA-256, rendered PNG paths/sizes/SHA-256 values, selected range metadata, and the optional ZIP path/size/SHA-256. Use FolderBridge `image_open` to inspect individual generated PNGs, including PNG members inside the ZIP.
