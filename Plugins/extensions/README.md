# FolderBridge External Extensions

This directory contains **external / non-bundled FolderBridge Extensions** that are distributed with the repository for optional installation but are **not built into FolderBridge.exe**.

Do not confuse this directory with the repository-root `extensions/` directory:

- `extensions/` = bundled extensions shipped as part of FolderBridge.
- `Plugins/extensions/` = optional **public** external extensions that users copy into their per-user Extension directory and approve explicitly.
- `Plugins/skill-packs/` = optional **public** Skill Packs; these are methodology text and use a separate trust model.
- `local-private/` = preferred ignored repository-local source for new private/local-only integrations. Existing explicitly ignored local-only integrations may remain where operationally convenient; their presence under the workspace does not make them public or eligible for synchronization.

## Install an external Extension

Copy one complete child directory, for example `ffmpeg-toolkit`, to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\ffmpeg-toolkit\
```

The target directory must contain `folderbridge-extension.json` and the declared entrypoint (`plugin.py`) directly at its first level. Do not add an extra nesting layer.

Then open FolderBridge **Extensions & Skills**, rescan if needed, approve the exact directory hash + declared permissions, and enable the Extension. The runtime registry is hot-scanned, so a normal external Extension install does not require rebuilding FolderBridge or re-registering MCP tools.

Any later change to a hash-covered Extension file makes the old approval stale and requires approval of the new hash.

## Currently published external Extensions

| Extension | Version | Scope |
| --- | --- | --- |
| `download-toolkit` | 0.1.0 | Public HTTPS file download and safe GitHub source snapshots with streamed size/SHA verification, SSRF/redirect defenses, no-clobber atomic publish, and no repository-code execution. |
| `file-ops-toolkit` | 0.1.0 | Same-workspace regular-file copy/move with exact source+destination mutation claims, streamed hashing, no-clobber defaults, SHA-guarded overwrite, atomic publish/replace, and link/reparse denial. |
| `comfyui` | 1.4.0 | Hot-load bridge to local ComfyUI with host-owned workflow Jobs, targeted prompt cancellation, bounded real-job/model/node inspection, explicit memory release, transient history retry, and optional scoped workspace output. |
| `gpt-sovits-local` | 0.1.2 | Workspace adapter for the fixed `GPT-SoVITS-Bridge/runner.ps1` workflow. |
| `ffmpeg-toolkit` | 0.1.2 | Workspace-confined FFmpeg/FFprobe probe, capability discovery, and long-running media jobs. |
| `pdf-toolkit` | 0.6.0 | Workspace-confined PDF inspection through an exact-provenance PdfPig 0.1.16 / Windows PowerShell 5.1 process seam with deterministic Unicode 14.0.0 literal search, plus parser-independent Windows.Data.Pdf page rendering and transactional exact-hash installation. |
| `ftp-toolkit` | 0.2.1 | Generic workspace-confined FTP/FTPS profiles with optional local HTTP CONNECT proxy, listing/stat, upload/download, recursive upload, automatic parent mkdir, rename and exact-file delete. |
| `godot-ai` | 0.1.0 | Hot-loadable workspace adapter for a local Godot AI MCP server, with bounded editor, scene, run, log, screenshot, and runtime-input actions. |
