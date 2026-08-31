# Local ComfyUI — external FolderBridge Extension

This Extension connects FolderBridge to a local ComfyUI server at `127.0.0.1:8188` without being bundled into `FolderBridge.exe`.

## Install / hot-load

From PowerShell, the repository copy can install/update itself with a staged directory cutover:

```powershell
.\install.ps1
```

If another PowerShell script invokes this installer with `&`, treat a thrown exception (or `$? -eq $false`) as failure. Do **not** inspect `$LASTEXITCODE` after invoking this `.ps1`: `$LASTEXITCODE` is a native-process status variable and can retain an unrelated earlier value even when this installer completed successfully.

By default it installs to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\comfyui\
```

You may instead copy the complete directory manually. The installer refuses to replace a reparse-point target, stages the exact runtime files beside the destination, swaps the directory only after staging succeeds, and restores the previous directory if cutover fails.

Open **Extensions & Skills**, click **重新扫描**, review the exact directory hash and declared permissions, then approve and enable the Extension. FolderBridge hot-scans the external Extension directory, so installing or updating the plugin does not require rebuilding FolderBridge or adding a new MCP tool. Any hash-covered file change makes the previous approval stale and requires a new approval.

## Actions

- `status`: checks local ComfyUI `/system_stats` through loopback only.
- `run`: executes one API-format workflow JSON from the selected FolderBridge workspace as a host-owned Job. It supports bounded overrides, dynamic-combo preflight, prompt-scoped cancellation, bounded artifact metadata, and optional image copying into a workspace `save_directory`.

`run` declares no workspace mutation when `save_directory` is omitted. When `save_directory` is supplied, FolderBridge resolves a tree mutation scope before the worker starts, so unrelated workspace writes can continue while overlapping writes are serialized.

## Safety boundary

The runtime uses Python standard library code plus the public `folderbridge_mcp.extension_api.ExtensionError` ABI only. It does not import `folderbridge_mcp.comfyui`, `folderbridge_mcp.security`, or other private FolderBridge internals.

Workspace workflow/save paths reject absolute paths, `..`, symlink/reparse traversal, ignored dependency/VCS directories, credential-like files, and writes to `.folderbridge.json`. ComfyUI network traffic is loopback-only, redirects/proxies are disabled, and cancellation never falls back to the process-global `/interrupt` endpoint.
