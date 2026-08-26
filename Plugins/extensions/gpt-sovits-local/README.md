# Local GPT-SoVITS — FolderBridge External Extension

An optional external FolderBridge Extension for a workspace that already contains a dedicated `GPT-SoVITS-Bridge` and local GPT-SoVITS runtime.

The Extension itself is intentionally thin. It does not download models, accept arbitrary commands, or embed GPT-SoVITS binaries. It only invokes the fixed workspace entrypoint:

```text
GPT-SoVITS-Bridge/runner.ps1
```

## Install

Copy this entire `gpt-sovits-local` directory to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\gpt-sovits-local\
```

Then open FolderBridge **Extensions & Skills**, rescan if needed, approve the exact directory hash + declared permissions, and enable **Local GPT-SoVITS**.

Normal external Extension hot loading does not require rebuilding FolderBridge or re-registering MCP tools.

## Expected workspace layout

The selected workspace is expected to provide the bridge/runtime files used by the fixed runner, including:

```text
GPT-SoVITS-Bridge/runner.ps1
GPT-SoVITS/
```

`status` reports whether the fixed runner, runtime Python, WebUI launcher, package archive, PowerShell, and optional 7-Zip executable are present.

## Actions

- `status` — inspect local bridge/runtime readiness.
- `run` — host-owned Job action with one fixed `operation` enum:
  - `probe`
  - `bootstrap`
  - `prepare-dataset`
  - `asr`
  - `train`
  - `infer`
  - `launch-webui`
  - `stop`

The optional `params` object is serialized to a temporary JSON file under FolderBridge's private Extension state directory and passed to the fixed runner. No arbitrary script/executable parameter is exposed.

## Permissions and trust boundary

The manifest declares:

- `workspace.read`
- `workspace.write`
- `extension.state`
- `process.execute:powershell.exe`

The worker uses `shell=False` and FolderBridge-owned process-tree cleanup. External Extension approval is a trust contract rather than an OS sandbox: the fixed workspace runner and the GPT-SoVITS code it launches still execute with the current OS user's permissions, so review the workspace/runtime before use.
