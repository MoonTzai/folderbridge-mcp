# FolderBridge MCP

[简体中文](README.zh-CN.md) | English

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

> [!TIP]
> **Windows users: [download FolderBridge.exe directly](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe).**
> The binary is under GitHub **Releases → latest release → Assets**; it does not appear in the repository's source-file list. You can also open the [full release page](https://github.com/MoonTzai/folderbridge-mcp/releases/latest) for the EXE and its SHA-256 checksum file.

**A safer, local-first bridge between AI clients and one folder you explicitly choose.**

FolderBridge MCP is a zero-dependency Python MCP server plus a desktop launcher. It lets ChatGPT on the web—or any client that supports local stdio MCP—inspect and carefully edit a bounded local workspace. It deliberately avoids a public HTTP server, arbitrary shell access, telemetry, and silent background services.

> [!IMPORTANT]
> This project is in an early public beta. It reduces the attack surface; it is not an operating-system sandbox. Only expose folders and repositories you trust.

## Why FolderBridge?

- **One-folder boundary:** each server process is confined to one canonical workspace.
- **Read-only by default:** switch to read/write explicitly in the launcher.
- **Conflict-safe edits:** existing files require the SHA-256 returned by the last read; stale edits fail instead of overwriting newer work.
- **No arbitrary shell:** optional tasks are named, locally reviewed, and approved by exact config hash.
- **No public listener:** the MCP server uses stdio only.
- **No telemetry:** FolderBridge itself makes no network requests.
- **Secret-aware launcher:** the OpenAI Runtime API key stays in memory and is removed before the local MCP process can use it.
- **Beginner-friendly desktop UI:** folder selection, access mode, Tunnel setup, diagnostics, start/stop, process monitoring, and redacted logs live in one window.
- **Windows scaling support:** fonts and window sizing follow the current display DPI and refresh when moving between monitors with different scale factors.

## Quick start

Requirements:

- Python 3.11 or newer;
- Windows for the tested double-click launcher experience (the stdio server is cross-platform);
- Git is optional and only used for bounded `status` and `diff` views.

### Windows executable — recommended

Download `FolderBridge.exe` and `FolderBridge.exe.sha256` from the [latest GitHub release Assets](https://github.com/MoonTzai/folderbridge-mcp/releases/latest), or [download the EXE directly](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe). The binary does not appear in the repository's source-file list. No Python installation is required. Verify the checksum, then double-click `FolderBridge.exe`.

The executable contains FolderBridge and its Python runtime, but it does **not** bundle OpenAI's `tunnel-client`; download the official client separately from OpenAI's release and select it in the launcher when using ChatGPT on the web.

> [!NOTE]
> The current community build is not code-signed, so Windows identifies its publisher as unknown. If you do not trust an unsigned binary, build it from the audited source instead.

### Run from source

Clone the repository and start the GUI:

```powershell
git clone https://github.com/MoonTzai/folderbridge-mcp.git
cd folderbridge-mcp
python .\folderbridge_launcher.py gui
```

Choose a workspace, keep **Read only** selected for the first run, and use the status panel to finish setup. Launcher preferences are stored outside the repository; the Runtime API key is never saved.

`folderbridge_gui.pyw` is kept as a source-only convenience for Python installations whose `.pyw` association points to an interpreter with Tkinter. The standalone `FolderBridge.exe` is the supported double-click entry point.

## Connect ChatGPT on the web

ChatGPT cannot connect directly to a local stdio process. FolderBridge uses OpenAI's official [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) for this path. You need:

> [!IMPORTANT]
> Secure MCP Tunnel requires only **outbound HTTPS (port 443 by default)** from this computer and no internet-facing inbound port. Never enable Windows Remote Desktop, router port forwarding, DMZ, UPnP mappings, or port `3389` for FolderBridge; none of them are part of Tunnel setup. If a firewall asks about inbound access, do not create a broad public inbound rule just to make the Tunnel work.

- a `tunnel_id` from [OpenAI Platform Tunnel settings](https://platform.openai.com/settings/organization/tunnels);
- a key for `tunnel-client` from the same organization's [Runtime API Keys](https://platform.openai.com/settings/organization/api-keys);
- the official [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest);
- ChatGPT developer-mode access appropriate for your account or workspace.

Click **网页端一键引导** (Web setup guide) in the launcher's upper-right corner for the same four steps and shortcuts below.

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

1. Select exactly one workspace folder and keep **Read only (recommended)** for the first run.
2. Select `tunnel-client.exe` from the complete archive, never a `tunnel-client-runtime-*` component; the default `folderbridge` profile is suitable.
3. Enter the same Tunnel ID and Runtime API key, and leave advanced named tasks disabled.
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

1. Select the workspace in FolderBridge and keep **Read only (recommended)**.
2. Open page 5 of **连接设置向导** and copy JSON, TOML, or the complete stdio command.
3. Paste it into the client's MCP Servers settings; follow that client's documentation for its exact field names.
4. Restart or refresh the client and verify that `server_info` and `workspace` appear.

A direct stdio connection needs no `tunnel-client`, Tunnel ID, Runtime API key, or running FolderBridge GUI. The client starts this command when needed:

```powershell
FolderBridge.exe serve --workspace C:\path\to\repo --read-only
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
      "args": ["serve", "--workspace", "C:\\work\\project", "--read-only"]
    }
  }
}
```

> [!IMPORTANT]
> The JSON/TOML top-level keys are common conventions, not universal client requirements. Compatibility ultimately means that the client launches command/args unchanged as an MCP stdio subprocess. If the client accepts only a URL, this release has no built-in HTTP/SSE listener; do not expose an ad-hoc local port directly to the public internet.

See the [client compatibility research](docs/client-compatibility-research.md) for protocol versions, process lifecycle, `cwd`/`env`, approval boundaries, and remote bridging details.

## Tools and editing workflow

The default server exposes:

- `server_info`: reports the active workspace and safety boundary;
- `workspace`: lists, reads, searches, and shows bounded Git status/diff output;
- `edit_file`: creates a UTF-8 file or performs atomic, unique exact replacements.

A safe edit loop is:

1. list or search for a file;
2. read it and retain the returned SHA-256;
3. submit an exact replacement with that hash;
4. inspect the Git diff.

The server cannot delete or move files. Absolute paths, `..`, symlinks, junctions/reparse points, VCS internals, common dependency/build folders, and credential-like names are denied.

## Optional named tasks

Arbitrary command text is never accepted through MCP. If you need a test-after-edit loop, create and review a local task policy:

```powershell
python .\folderbridge_launcher.py init --workspace C:\path\to\repo
# Review C:\path\to\repo\.folderbridge.json
python .\folderbridge_launcher.py approve --workspace C:\path\to\repo
```

Then enable approved tasks in the launcher or add `--allow-tasks` to the generated server command. The model can select only a task name; it cannot provide a command, arguments, environment variables, or working directory.

> [!WARNING]
> Tests, build tools, and package scripts execute repository code with your current OS user permissions. Use a disposable VM or container for untrusted repositories, or leave task execution disabled.

## Security model

FolderBridge treats MCP requests, repository text, and tool output as untrusted data. Enforcement lives in the tool implementation rather than MCP annotations alone. The main controls are:

- canonical path confinement and link rejection;
- bounded UTF-8 reads, searches, Git output, subprocess output, and protocol messages;
- atomic writes with a current-content hash precondition;
- protected local policy files;
- `shell=False`, fixed argument arrays, clean task environments, and timeouts;
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
