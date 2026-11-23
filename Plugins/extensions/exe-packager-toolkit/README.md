# EXE Packager Toolkit (FolderBridge external extension)

Version 0.1.1 · independent, hot-loaded, cross-workspace Windows EXE packager.

### 0.1.1 compatibility fix (2026-11-18)
The initial 0.1.0 `package-onefile` action used JSON Schema `pattern` for hashes, but current FolderBridge input validation does not implement that schema keyword. It rejected packaging before the worker could run (`EXTENSION_SCHEMA_UNSUPPORTED`). 0.1.1 uses supported 64-character length constraints in the manifest while preserving exact 64-character lowercase hexadecimal SHA validation inside the Python worker. `verify-exe` is fixed as well. Existing installed 0.1.0 needs an explicit reinstall and approval of the new hash in the FolderBridge UI.

## Why this exists

The built-in FolderBridge global capability "package-windows" invokes each repository's own fixed build script. It is **not** a generic source-file packager. This external extension adds a reusable, globally approved action that accepts a chosen FolderBridge workspace, one source entry and one output directory. There is **no new Node task registration for each project**.

## Supported inputs (v0.1.0)

| Backend | Source | Toolchain | Produced |
| --- | --- | --- | --- |
| python | .py / .pyw | Python + PyInstaller | --onefile Windows EXE |
| go | .go | Go toolchain | windows/amd64 EXE |
| dotnet | .csproj | .NET SDK and restored dependencies | self-contained single-file Windows EXE, only if no required sidecar files remain |

Not supported: Electron builds, raw HTML direct packaging, arbitrary .spec files, arbitrary command strings or user-chosen executables. Future backends would require source review and a new extension version/hash approval; this is **not** an unrestricted packaging shell.

The toolchain itself is **not bundled** in this extension. The Python selector checks an optional repository-local .build-venv/Scripts/python.exe when its source resides in a developer checkout, then system python.exe / py.exe if discoverable. Go/dotnet are selected from installed system toolchains. PyInstaller must be available in the chosen Python environment. No automatic package downloads or pip installs occur.

## One-time install and authorization (manual/user action)

Open PowerShell and run:

powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\Plugins\extensions\exe-packager-toolkit\install.ps1"

The installer copies the hash-covered files to:

%LOCALAPPDATA%\folderbridge-mcp\extensions\exe-packager-toolkit\

Then open FolderBridge > **Extensions & Skills** > **Rescan**, find **EXE Packager Toolkit**, inspect its declared permissions, approve the exact current hash, and enable it. Do not treat copying the directory as authorizing it. The extension will then be usable from **any currently configured FolderBridge workspace**, selected by workspace_id on each call; future workspaces are usable once registered with FolderBridge.

No modification of a project's .folderbridge.json, no FolderBridge binary rebuild, no Tunnel restart, and no new per-project Node task is required.

If any extension source, manifest or permission changes later, the previous approval becomes stale and the changed version must be approved again. One-time authorization means **one approval per reviewed version**, not a permanent bypass of code review.

## How to use

1. Select the relevant workspace_id from FolderBridge server_info; e.g. Tools = bc0054019b82.
2. Call extension(info, extension_id="exe-packager-toolkit") to see the exact action schema.
3. Call preflight with source_path, output_dir, output_name, backend="auto" to get the toolchain result and current source SHA.
4. If preflight is ready, invoke package-onefile with the same paths and **expected_source_sha256** from preflight. Run as a managed Job and check job_status until it finishes.
5. Call verify-exe to confirm PE format and hash. Running the resulting EXE on Windows and testing actual app behavior is a separate acceptance gate.

Generic standalone Python application example (not evidence that a build ran):

- Workspace: a registered workspace_id returned by server_info
- source_path: src/sample_app.pyw
- output_dir: Release
- output_name: SampleApp
- backend: auto
- gui: true
- expected_source_sha256: exact source SHA reported by preflight
- overwrite: false

Expected output: workspace-relative Release/SampleApp.exe

The file is a native Windows executable distributing one file. PyInstaller's one-file bootloader may extract to the operating system temporary directory while running; this does not mean it requires a server or a separate installed Python runtime.

## Safety controls

- No arbitrary shell command, environment variable, URL, network endpoint, script flags or arbitrary executable path parameters.
- Workspace-relative input/output paths only; symlinks, Windows junctions, secrets, dependencies and VCS directories denied.
- Build action requires an exact SHA-256 match of the source and returns output SHA-256, byte size and PE header check.
- Existing EXE is **never** overwritten unless overwrite=true and the exact previous output SHA-256 is provided. Scratch intermediates are temporary.
- Python/Go/.NET build tools are fixed to listed names, with shell=False and host-owned process-tree timeout/cancellation.
- The host declares an opaque workspace mutation lease because some build systems may create intermediate object/cache files near the source, even though the intended final artifact is a single EXE.
- Go module remote fetching is disabled; .NET uses --no-restore; no automatic Python package download.
- Installed build tools and source project hooks are still executable code under the current Windows account: **external Extensions are not OS sandboxes**. Do not package untrusted repositories outside a VM.
- EXE PE/header/hash validation is not the same as antivirus attestation or a successful Windows GUI launch.

## This version status

- Source implementation and offline unit tests are included under Plugins/extensions/exe-packager-toolkit.
- Installing and approving the extension requires explicit action in the FolderBridge Windows UI.
- No Windows EXE is claimed until the package-onefile Job completes and its SHA/PE checks pass.
- Project files are not modified by installation. Running a packaging Job may create compiler intermediate files in the selected workspace, as described above.
