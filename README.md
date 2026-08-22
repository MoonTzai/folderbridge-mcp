# FolderBridge MCP

[简体中文](README.zh-CN.md) | English

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

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

Download `FolderBridge.exe` and `FolderBridge.exe.sha256` from the [latest GitHub release](https://github.com/MoonTzai/folderbridge-mcp/releases/latest). No Python installation is required. Verify the checksum, then double-click `FolderBridge.exe`.

The executable contains FolderBridge and its Python runtime, but it does **not** bundle OpenAI's `tunnel-client`; select the separately downloaded official client in the launcher when using ChatGPT on the web.

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

- a `tunnel_id` and Runtime API key from [OpenAI Platform Tunnel settings](https://platform.openai.com/settings/organization/tunnels);
- the official [`tunnel-client`](https://github.com/openai/tunnel-client/releases/latest);
- ChatGPT developer-mode access appropriate for your account or workspace.

In the launcher:

1. Click **网页端一键引导** (Web setup guide) to open the official Tunnel and ChatGPT pages.
2. Select the downloaded `tunnel-client`, then enter the Tunnel ID and Runtime API key.
3. Click **Start connection**. The launcher runs the official `init`, `doctor`, and `run` flow.
4. Open [ChatGPT Plugins](https://chatgpt.com/plugins), create a developer-mode app, choose **Tunnel** as the connection, and select or paste the same Tunnel ID.

Keep the launcher running while ChatGPT uses the workspace. Closing it stops the Tunnel process. Account-level developer-mode and app installation confirmations remain in ChatGPT; FolderBridge does not automate your browser session or security settings.

No browser extension is required. FolderBridge intentionally does not silently download or execute network-delivered binaries.

## Connect another local MCP client

Print a ready-to-use configuration:

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format toml
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format json
```

Generate a read-only command:

```powershell
python .\folderbridge_launcher.py client-config --workspace C:\path\to\repo --format tunnel --read-only
```

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
