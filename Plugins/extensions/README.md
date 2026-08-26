# FolderBridge External Extensions

This directory contains **external / non-bundled FolderBridge Extensions** that are distributed with the repository for optional installation but are **not built into FolderBridge.exe**.

Do not confuse this directory with the repository-root `extensions/` directory:

- `extensions/` = bundled extensions shipped as part of FolderBridge.
- `Plugins/extensions/` = optional external extensions that users copy into their per-user Extension directory and approve explicitly.
- `Plugins/skill-packs/` = optional Skill Packs; these are methodology text and use a separate trust model.

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
| `gpt-sovits-local` | 0.1.1 | Workspace adapter for the fixed `GPT-SoVITS-Bridge/runner.ps1` workflow. |
| `ffmpeg-toolkit` | 0.1.1 | Workspace-confined FFmpeg/FFprobe probe, capability discovery, and long-running media jobs. |
| `ftp-toolkit` | 0.2.0 | Generic workspace-confined FTP/FTPS profiles with optional local HTTP CONNECT proxy, listing/stat, upload/download, recursive upload, automatic parent mkdir, rename and exact-file delete. |
