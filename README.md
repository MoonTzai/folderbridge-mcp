# FolderBridge MCP

[简体中文](README.zh-CN.md) | English

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Source Python 3.11+](https://img.shields.io/badge/Source%20Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

> [!TIP]
> **FolderBridge itself is a single-file Windows app: [download `FolderBridge.exe` directly](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe) and double-click it. No Python or Node.js installation is required.**
> The binary is under GitHub **Releases → latest release → Assets**; it does not appear in the repository's source-file list. For ChatGPT on the web, FolderBridge still uses OpenAI's separately distributed official `tunnel-client.exe`; the launcher guides you through selecting it. You can also open the [full release page](https://github.com/MoonTzai/folderbridge-mcp/releases/latest) for the EXE and its optional SHA-256 checksum file.

**A safer, local-first bridge between AI clients and a small set of folders you explicitly choose.**

FolderBridge MCP is a zero-dependency Python MCP server plus a desktop launcher. It lets ChatGPT on the web—or any client that supports local stdio MCP—inspect and carefully edit a bounded local workspace. It deliberately avoids a public HTTP server, arbitrary shell access, telemetry, and silent background services.

> [!IMPORTANT]
> This project is in an early public beta. It reduces the attack surface; it is not an operating-system sandbox. Only expose folders and repositories you trust.

## 0.4.2 highlights

- **Reachable high-DPI UI:** the main launcher page now has a vertical viewport scrollbar, so lower controls remain reachable when Windows scaling makes the content taller than the available screen.
- **Clear ComfyUI first run and startup diagnostics:** if no ComfyUI install root has been saved yet, the launcher explicitly says auto-start is waiting for configuration, opens the Extensions sidebar, and prompts for a supported Portable / `.venv` / `venv` root instead of appearing to fail silently. Launcher startup performs a bounded second reconciliation pass instead of relying on one early extension snapshot; managed startup then waits up to 120 seconds, shows an in-progress state, uses `--disable-auto-launch`, and keeps combined startup output in `launcher-comfyui.log` so early exits/timeouts are diagnosable.
- **Optional toolchains are labeled correctly:** `FolderBridge.exe` itself needs neither Python nor Node.js. Python 3.11 x64 is recommended for source/development/repackaging; Node.js LTS is only needed when a Node/npm workspace's own test/build flow needs it.
- **Capabilities are not installers:** enabling test/build/package authorizes bounded execution; it does not install Python, Node, Gradle, compilers, package managers, or project dependencies.
- **Single-file delivery clarified:** the Windows FolderBridge application is one EXE with its Python runtime bundled. ChatGPT web users still obtain OpenAI's official `tunnel-client.exe` separately.

See [CHANGELOG.md](CHANGELOG.md) for the full release history and [Extension ABI v1](docs/extensions.md) for managed-service and plugin boundaries.

## Why FolderBridge?

- **Independent folder boundaries:** one connection can contain up to eight canonical workspaces; every multi-workspace tool call selects a `workspace_id` instead of merging roots.
- **Read-only by default:** switch to read/write explicitly in the launcher.
- **Conflict-safe edits:** existing files require the SHA-256 returned by the last read; stale edits fail instead of overwriting newer work.
- **No arbitrary shell:** optional tasks are named, locally reviewed, and approved by exact config hash.
- **No public listener:** the MCP server uses stdio only.
- **No telemetry:** FolderBridge itself makes no network requests.
- **Secret-aware launcher:** the OpenAI Runtime API key stays in memory and is removed before the local MCP process can use it.
- **Beginner-friendly desktop UI:** an add/remove folder list, global access mode, Tunnel setup, diagnostics, start/stop, process monitoring, and redacted logs live in one window.
- **Windows scaling support:** fonts and window sizing follow the current display DPI and refresh when moving between monitors with different scale factors.

## Quick start

Requirements depend on how you use FolderBridge:

- **Standalone Windows EXE:** no separate Python or Node.js installation is required; Windows is the tested double-click launcher environment.
- **Run/develop from source:** Python 3.11 or newer is required; Python 3.11 x64 is the recommended Windows build baseline.
- **Project capabilities:** install only the toolchain required by the workspace itself (for example Node.js LTS for a Node/npm project's test/build scripts). Enabling a capability does not install that toolchain.
- Git is optional and only used for bounded `status` and `diff` views.

### Windows executable — recommended

Download the single `FolderBridge.exe` from the [latest GitHub release Assets](https://github.com/MoonTzai/folderbridge-mcp/releases/latest), or [download the EXE directly](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe). The binary does not appear in the repository's source-file list. `FolderBridge.exe` is the complete FolderBridge application and already contains its Python runtime, so normal EXE users do not install Python or Node.js. The adjacent `FolderBridge.exe.sha256` file is optional and exists only for integrity verification.

The one-file executable deliberately does **not** rebundle OpenAI's independently released `tunnel-client`; download the official client separately from OpenAI's release and select `tunnel-client.exe` in the launcher only when using ChatGPT on the web.

> [!NOTE]
> The current community build is not code-signed, so Windows identifies its publisher as unknown. If you do not trust an unsigned binary, build it from the audited source instead.

### Run from source

Clone the repository and start the GUI:

```powershell
git clone https://github.com/MoonTzai/folderbridge-mcp.git
cd folderbridge-mcp
python .\folderbridge_launcher.py gui
```

Add the workspaces you need, keep **Read only** selected for the first run, and use the status panel to finish setup. Launcher preferences are stored outside the repository; the Runtime API key is never saved. A legacy single-workspace setting is migrated into the first list entry automatically.

`folderbridge_gui.pyw` is kept as a source-only convenience for Python installations whose `.pyw` association points to an interpreter with Tkinter. The standalone `FolderBridge.exe` is the supported double-click entry point.

### Optional development and project toolchains

These are **not** prerequisites for normal `FolderBridge.exe` use:

- For source development or rebuilding the Windows EXE, install **Python 3.11 x64** from the official [Python Windows downloads](https://www.python.org/downloads/windows/) and verify `python --version`. The packaging flow uses an isolated `.build-venv` and `requirements-build.txt`.
- Install **Node.js LTS** from the official [Node.js downloads](https://nodejs.org/en/download) only when a target Node/npm workspace needs `node`/`npm` for its own test or build commands; verify with `node --version` and `npm --version`.
- Other projects may require their own runtimes or compilers. FolderBridge capabilities provide authorization and bounded discovery/execution, not dependency installation.

## Connect ChatGPT on the web

ChatGPT cannot connect directly to a local stdio process. FolderBridge uses OpenAI's official [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) for this path. You need:

> [!IMPORTANT]
> Secure MCP Tunnel requires only **outbound HTTPS (port 443 by default)** from this computer and no internet-facing inbound port. Never enable Windows Remote Desktop, router port forwarding, DMZ, UPnP mappings, or port `3389` for FolderBridge; none of them are part of Tunnel setup. If a firewall asks about inbound access, do not create a broad public inbound rule just to make the Tunnel work.

- a `tunnel_id` from [OpenAI Platform Tunnel settings](https://platform.openai.com/settings/organization/tunnels);
- a key for `tunnel-client` from the same organization's [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys);
- the official [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest);
- ChatGPT developer-mode access appropriate for your account or workspace.

Click **连接设置向导** (Connection setup guide) in the launcher's upper-right corner for the same steps and shortcuts below, including the optional Python/Node appendix.

### 1. Download the official Windows x64 client

> [!WARNING]
> **Download only the complete `tunnel-client-v<version>-windows-amd64.zip` archive. Never download or select a `tunnel-client-runtime-*` file.** Runtime files are internal components and do not implement the `init` command needed for setup; their similar names can still lead to “configuration failed.” After extracting the complete archive, select only the main program named exactly `tunnel-client.exe`.

1. Open the latest [`openai/tunnel-client` release](https://github.com/openai/tunnel-client/releases/latest) and expand **Assets**.
2. Download the complete archive named `tunnel-client-v<version>-windows-amd64.zip`. In release filenames, `amd64` means Windows x64 and is correct for most Intel/AMD PCs.
3. Optionally download `SHA256SUMS.txt` for verification. Do not choose `tunnel-client-runtime-*`, `windows-arm64`, `all.zip`, source-code archives, or license files; a Runtime archive cannot replace the complete client.
4. Extract the entire ZIP to a stable folder; there is no installer, and you should not run it from inside the archive. The guide can create and open `%LOCALAPPDATA%\FolderBridge\bin` as a recommended location.
5. Return to FolderBridge, click **选择已解压的 EXE**, and select only the file named exactly `tunnel-client.exe`.

### 2. Create the Platform Tunnel

Open [OpenAI Platform Tunnel settings](https://platform.openai.com/settings/organization/tunnels), click **Create tunnel**, and use these current choices:

| Field | Recommended value | Notes |
| --- | --- | --- |
| Name | `FolderBridge` | Customizable display name |
| Description | `Local FolderBridge MCP for private workspace access` | Required; customizable |
| Organizations | `Personal` for a personal account | For teams, select the Platform organization that manages this Tunnel |
| ChatGPT workspaces | The workspace where you will create the app | Do not leave it blank; a personal account usually has one workspace ID |

After creation, copy the Tunnel ID beginning with `tunnel_`. Running and selecting it requires **Tunnels Read + Use**; creating or editing it also requires **Manage**. Associate only the organizations and ChatGPT workspaces that need access to this local workspace.

Create the key for `tunnel-client` under the same Platform organization's [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys); its creator needs **Tunnels Read + Use**. Paste it only into FolderBridge, where it remains in process memory. Do **not** use an Admin API key, and do not put the Runtime key into the ChatGPT app's Authentication field.

### 3. Start FolderBridge

1. Add one or more explicit workspace folders (up to eight) and keep **Read only (recommended)** for the first run. The global access mode applies to every listed folder.
2. Select `tunnel-client.exe` from the complete archive, never a `tunnel-client-runtime-*` component; the default `folderbridge` profile is suitable.
3. Enter the same Tunnel ID and Runtime API key. Optionally enable the global capabilities you want once; leave per-workspace advanced named tasks disabled unless you need a custom command.
4. Click **Start connection**. The launcher runs the official `init`, `doctor`, and `run` flow. Continue only after the top status reads **运行中** (Running). When changing the folder or access mode later, click **应用配置** (Apply configuration) again; the launcher updates the same profile that it manages.

### 4. Create the ChatGPT developer-mode app

Open [ChatGPT Plugins](https://chatgpt.com/plugins), click **+**, and use these critical choices:

| Field | Required choice |
| --- | --- |
| Connection | **Tunnel** |
| Available Tunnel | Select the same Tunnel, or paste its `tunnel_...` ID |
| Authentication | **No authentication** |

> [!WARNING]
> Do not leave the form on its default **OAuth** choice, and do not paste a `https://tunnel-service...` address into Server URL. FolderBridge does not implement user OAuth, so that configuration produces a `does not implement OAuth` error.

#### Invoke the app in a ChatGPT conversation

1. Keep the FolderBridge status at **运行中** (Running), then start a new ChatGPT conversation.
2. Select the **+** beside the composer, open **More**, and choose the FolderBridge app that you just created.
3. Send a task such as: “First use FolderBridge `server_info` to list the available workspaces, then read the root of the workspace I name.” Review the `workspace_id` and target file in any tool confirmation before approving it.

This entry point follows OpenAI's official [connect and test guidance](https://developers.openai.com/apps-sdk/deploy/connect-chatgpt). The launcher's connection guide includes the same steps and a one-click copyable example prompt.

Keep the launcher running while ChatGPT uses the workspace. Closing it stops the Tunnel process. Account-level developer-mode and app installation confirmations remain in ChatGPT; FolderBridge does not automate your browser session or security settings.

No browser extension is required. FolderBridge intentionally does not silently download or execute network-delivered binaries.

## Connect another local MCP client

Yes. FolderBridge's compatibility seam is its standard MCP `stdio` server. The ChatGPT Tunnel is only a bridge for a web client that cannot launch a local process. The MCP `stdio` transport likewise specifies that the client launches the server subprocess and exchanges JSON-RPC over standard input/output. [MCP stdio specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)

### Client requirements

- It can configure a local **command + args** pair and keep the subprocess running.
- It supports MCP over `stdio`, keeping stdout reserved for protocol messages.
- It can discover and call MCP tools (`initialize`, `tools/list`, and `tools/call`, or the modern 2026-07-28 discovery flow).
- It closes stdin or terminates the subprocess when the session ends.
- Prefer a client with tool approval UX, but do not treat its confirmation dialog as the only security boundary.

FolderBridge currently handles the `2024-11-05`, `2025-03-26`, `2025-06-18`, `2025-11-25`, and `2026-07-28` protocol paths. It returns standard tool schemas, text content, and `structuredContent`; whether a confirmation prompt appears is client-specific. [MCP Tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)

| Client type | Direct connection? | Route |
| --- | --- | --- |
| Local desktop app or IDE with stdio MCP | Yes | Use `FolderBridge.exe` as command and `serve ...` as args |
| Local app with one combined command field | Usually | Paste the full stdio command and preserve path quoting |
| Client accepting only a remote HTTP/SSE URL | No | Use that client's official stdio gateway/proxy, or deploy a separately protected MCP bridge |
| Web-only, mobile, or cloud sandbox | Usually not to the local PC | Use the vendor's official Tunnel/remote mechanism on a trusted host that can reach the workspace |

### Shortest local setup

1. Add one or more workspaces in FolderBridge and keep **Read only (recommended)**.
2. Open page 5 of **连接设置向导** and copy JSON, TOML, or the complete stdio command.
3. Paste it into the client's MCP Servers settings; follow that client's documentation for its exact field names.
4. Restart or refresh the client and verify that `server_info` and `workspace` appear.

A direct stdio connection needs no `tunnel-client`, Tunnel ID, Runtime API key, or running FolderBridge GUI. The client starts this command when needed:

```powershell
FolderBridge.exe serve --workspace C:\path\to\repo --read-only
```

Repeat `--workspace` to add folders while keeping each root independent:

```powershell
FolderBridge.exe serve --workspace C:\work\frontend --workspace C:\work\backend --read-only
```

Generate ready-to-paste configurations from source:

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format json --read-only
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format toml --read-only
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format tunnel --read-only
```

The JSON output follows the common `mcpServers` convention used by many desktop clients:

```json
{
  "mcpServers": {
    "folderbridge": {
      "command": "C:\\Tools\\FolderBridge.exe",
      "args": ["serve", "--workspace", "C:\\work\\frontend", "--workspace", "C:\\work\\backend", "--read-only"]
    }
  }
}
```

> [!IMPORTANT]
> The JSON/TOML top-level keys are common conventions, not universal client requirements. Compatibility ultimately means that the client launches command/args unchanged as an MCP stdio subprocess. If the client accepts only a URL, this release has no built-in HTTP/SSE listener; do not expose an ad-hoc local port directly to the public internet.

See the [client compatibility research](docs/client-compatibility-research.md) for protocol versions, process lifecycle, `cwd`/`env`, approval boundaries, and remote bridging details.

## Tools and editing workflow

The built-in server tools do not depend on per-workspace task configuration:

- `server_info`: reports each available workspace name, stable `workspace_id`, built-in/global capabilities, and safety boundary;
- `workspace`: lists, reads, searches, and shows bounded Git status/diff output inside the selected `workspace_id`;
- `file_info`: returns bounded metadata and SHA-256 for binary files;
- `pptx_inspect`: inspects PPTX text, OOXML diagram relationships, and SmartArt data without executing Office content;
- `image_open`: returns bounded PNG/JPEG/GIF/WebP image content to the MCP client, including exact image members inside ZIP files;
- `extension`: the stable `list` / `info` / `run` gateway for hot-loaded extensions; installing more plugins does not add MCP tool names;
- `edit_file`: in read/write mode, creates or exactly edits a UTF-8 file inside the selected workspace.

With one workspace, existing clients may continue omitting `workspace_id`. With multiple workspaces, workspace-scoped tools require a `workspace_id` returned by `server_info`; missing or unknown IDs are rejected. Duplicate roots, parent/child overlaps, and lists beyond eight entries are rejected before startup.

A safe edit loop is:

1. list or search for a file;
2. read it and retain the returned SHA-256;
3. submit an exact replacement with that hash;
4. inspect the Git diff.

The server cannot delete or move files. Absolute paths, `..`, symlinks, junctions/reparse points, VCS internals, common dependency/build folders, and credential-like names are denied.

## Global pre-authorized capabilities

The launcher can authorize common capabilities once for all current and future workspaces: `test`, `build`, `package-windows`, `package-android`, and `git-push`. These permissions live in the launcher settings rather than `.folderbridge.json`, so a workspace can gain a supported build script months after it was added and FolderBridge will detect it at call time. The launcher also provides **Select all** and **Clear** controls for this group.

`git-push` is intentionally constrained to a GitHub HTTPS `origin`, the current branch, no force push, and no repository-local credential helper / push-target rewrite configuration. Build/package capabilities may execute local project code, so enable only the global capabilities you want.

For direct stdio clients, repeat `--capability <name>` on the `serve` or `client-config` command. The Windows launcher exposes the same choices as persistent checkboxes.

## Extensions

FolderBridge Extension ABI v1 is the preferred way to add future global integrations such as ComfyUI, FFmpeg, Blender, Ollama, ADB, or other local tools without changing the MCP tool catalog. The Windows launcher has a default-collapsed **Extensions** sidebar that hot-scans the user extension directory, shows installed/approved/loaded state, and lets you approve or disable plugins while the Tunnel remains connected.

Each extension is a directory containing at least `folderbridge-extension.json` and `plugin.py`. External plugins are approved against the exact SHA-256 of the complete plugin directory plus the declared permission list; changing any file or permission makes the approval stale. Extensions execute in a separate subprocess with a cleaned environment, bounded protocol I/O, and a declared timeout. This isolates crashes and protocol pollution, but it is **not an OS security sandbox**: use a VM/container for untrusted plugin code.

Workspace-specific adaptation should use `workspace_adapter.mode=dynamic` with `detect.any_of` / `detect.all_of`. FolderBridge re-evaluates those patterns at call time, so extensions do not need to inject `.folderbridge.json` tasks when installed and can become applicable after a project changes later. Persistent plugin state should use the provided profile state directory rather than polluting repositories.

ComfyUI is the first bundled extension. Its status action checks the fixed loopback endpoint `127.0.0.1:8188`; workflow execution requires one-time approval in the Extensions sidebar. In FolderBridge 0.4.1 the Windows launcher can also manage the local ComfyUI process itself: choose a supported Portable root (`python_embeded\\python.exe` + `ComfyUI\\main.py`) or source root (`main.py` + `.venv\\Scripts\\python.exe` / `venv\\Scripts\\python.exe`) once, and the launcher can auto-start it with explicit Python/main.py arguments, `shell=False`, and fixed `127.0.0.1:8188` binding. If that endpoint is already online before FolderBridge starts it, the service is marked external and reused rather than duplicated.

Managed-service ownership is intentionally stricter than Extension permissions. FolderBridge stores only `install_root` and `auto_start`, never a PID or arbitrary command. Only the in-memory `Popen` handle created by the current launcher run is considered owned and stoppable. FolderBridge never runs a user-selected BAT/CMD for ComfyUI and never looks up or kills an unknown process merely because it occupies port 8188. On exit it stops owned managed services before Tunnel/MCP; external ComfyUI remains running.

On Windows, the launcher requests Per-Monitor DPI V2 awareness and also polls the current window DPI as a lightweight fallback. Fixed GUI metrics are recalculated from their original logical sizes (`dpi / 96`) only when DPI actually changes, so moving 96 → 144 → 96 DPI does not accumulate scale drift.

ABI v1 plugins should use FolderBridge-packaged modules and the Python standard library rather than assuming arbitrary pip dependencies inside the one-file EXE. Extra software should normally be reached through a precise loopback API or a declared `process.execute:<basename>` dependency. Run `FolderBridge.exe extensions --json` to inspect what the packaged executable can discover. The connection guide appendix contains the complete format plus a one-click **LLM plugin-development prompt** that tells the model to request/upload missing API docs, scripts, workflows, sample files, or project structure instead of guessing. See [Extension ABI v1](docs/extensions.md).

## Optional per-workspace named tasks

Arbitrary command text is never accepted through MCP. For unusual repository-specific commands that are not covered by global capabilities, create and review a local task policy:

```powershell
python .\folderbridge_launcher.py init --workspace C:\path\to\repo
# Review C:\path\to\repo\.folderbridge.json
python .\folderbridge_launcher.py approve --workspace C:\path\to\repo
```

Then enable approved tasks in the launcher or add `--allow-tasks` to the generated server command. Enabling this switch does **not** require every configured workspace to contain `.folderbridge.json`: no-config workspaces, approved-task workspaces, extension-only workspaces, and workspaces whose config has not yet been approved can coexist in one connection. Approval is checked when that workspace's named task is actually invoked. The model can select only a task name; it cannot provide a command, arguments, environment variables, or working directory.

> [!WARNING]
> Tests, build tools, and package scripts execute repository code with your current OS user permissions. Use a disposable VM or container for untrusted repositories, or leave task execution disabled.

## Security model

FolderBridge treats MCP requests, repository text, and tool output as untrusted data. Enforcement lives in the tool implementation rather than MCP annotations alone. The main controls are:

- canonical path confinement and link rejection per workspace, with overlapping roots denied;
- bounded UTF-8 reads, searches, Git output, subprocess output, and protocol messages;
- atomic writes with a current-content hash precondition;
- protected local policy files;
- `shell=False`, fixed argument arrays, clean task environments, and timeouts;
- extension manifests reject unknown/overbroad permission names, extension approvals bind exact code hashes and permissions, and plugin execution is moved out of the MCP process;
- no inbound network listener and no FolderBridge telemetry.

Read [the complete security model](docs/security-model.md) and [the upstream design research](docs/upstream-research.md). To report a vulnerability, follow [SECURITY.md](SECURITY.md).

## Development

There are no third-party runtime dependencies.

```powershell
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

### Build the Windows executable

Builds use a pinned, build-time-only PyInstaller dependency. The released EXE uses the console bootloader with `hide-console=hide-early`: double-clicking opens only the GUI, while the same executable retains stdio when Tunnel starts its `serve` subcommand.

```powershell
python -m venv .build-venv
.\.build-venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\scripts\build_windows.ps1 -Python .\.build-venv\Scripts\python.exe
```

Artifacts and a SHA-256 file are written to `release\windows-x64`.

## License

Licensed under the [Apache License 2.0](LICENSE).

FolderBridge MCP is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by OpenAI. ChatGPT, OpenAI, MCP, and other product names belong to their respective owners.
