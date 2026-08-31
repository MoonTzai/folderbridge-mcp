# FolderBridge MCP

[简体中文](README.zh-CN.md) | English

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
![Source Python 3.11+](https://img.shields.io/badge/Source%20Python-3.11%2B-3776AB)
![Transport: stdio](https://img.shields.io/badge/MCP-stdio-6B5CE7)

> [!TIP]
> **FolderBridge itself is a single-file Windows app: [download `FolderBridge.exe` directly](https://github.com/MoonTzai/folderbridge-mcp/releases/latest/download/FolderBridge.exe) and double-click it. No Python or Node.js installation is required.**
> The binary is under GitHub **Releases → latest release → Assets**; it does not appear in the repository's source-file list. For ChatGPT on the web, FolderBridge still uses OpenAI's separately distributed official `tunnel-client.exe`; the launcher guides you through selecting it. You can also open the [full release page](https://github.com/MoonTzai/folderbridge-mcp/releases/latest) for the EXE and its optional SHA-256 checksum file.

**A safer, local-first bridge between AI clients and a small set of folders you explicitly choose.**

FolderBridge MCP is a zero-dependency Python MCP server plus a desktop launcher. It lets ChatGPT on the web—or any client that supports local stdio MCP—inspect and carefully edit a bounded local workspace. It deliberately avoids a public HTTP server, arbitrary shell access, telemetry, and silent background services. The Windows launcher can switch the complete user-facing interface between Chinese and English with the persistent `中文 / EN` button immediately to the left of the connection guide; changing language does not reconfigure or reconnect the Tunnel.

> [!IMPORTANT]
> This project is in an early public beta. It reduces the attack surface; it is not an operating-system sandbox. Only expose folders and repositories you trust.

## 0.7.0 highlights

- **Local Skill Engine without MCP schema churn:** trusted methodology Skills are discovered, matched, and loaded on demand through the bundled read-only `skill-engine` Extension behind the existing stable `extension` gateway. Adding Skill Packs does not add MCP tool names.
- **Model-routed, not falsely forced:** MCP initialization includes a bounded routing index for architecture, debugging, TDD, review, and implementation work. The model can call `match` and then `get` relevant methods; `server_info` reports this honestly as model-routed rather than guaranteed invocation.
- **Exact-hash Skill trust:** external Skill Packs remain invisible to the model until the exact displayed hash is approved. Any later file change makes approval stale, and `get` re-hashes the exact bytes it returns so a changed Skill fails closed between selection and use.
- **Bundled engineering methods:** `folderbridge-engineering` provides six focused methods for codebase design, architecture improvement, bug diagnosis, test-driven development, code review, and implementation. This FolderBridge-specific condensed/adapted Pack is derived from selected Skills in Matt Pocock's open-source [`mattpocock/skills`](https://github.com/mattpocock/skills) project under the MIT License; it is not the official Matt Pocock plugin. Skills are methodology text only and are never executed as local code.
- **Extensions & Skills launcher:** the existing right sidebar now manages Extensions and Skill Packs separately, including user Skill directory access, enable/disable state, exact-hash approval, revoke, provenance/details, and a clear warning that Skill text can influence model behavior without receiving executable permissions.
- **Packaged Skill verification:** `skills --json` emits stable UTF-8 diagnostics on Windows; `extensions --self-test` exercises both the ComfyUI and Skill Engine workers; the Windows build embeds `skill_packs` and verifies both bundled Skill discovery and worker execution before writing the EXE checksum.
- **PPTX XML usage visibility:** `pptx_inspect` now reports actual aggregate XML/relationship uncompressed bytes, configured limit, and usage ratio, while keeping the existing 256 MiB pre-parse guard.
- **Shared user-state root:** launcher, MCP host, and cleaned Extension workers now resolve one canonical FolderBridge user configuration root, avoiding split Extension/Skill state under different environment views.
- **Verified Extension execution snapshots:** exact-hash approval now stays bound to the bytes that actually execute. Each worker copies the hash-covered Extension tree to a private temporary snapshot, verifies that snapshot against the host-pinned SHA-256, and imports/runs only from the verified copy, including relative plugin file access.
- **Concurrency and protocol hardening:** Extension trust-store mutations are process-locally serialized, strict JSON rejects non-standard `NaN`/`Infinity` values, overlapping secrets are redacted longest-first, and JSON-schema booleans no longer compare equal to numeric enum values.
- **Bounded first-party subprocess ownership:** core Git inspection plus bundled Git Publisher and Office external-process calls now share owned-process timeout cleanup; partial/truncated Git safety inspection fails closed.
- **Additional resource/lifecycle guards:** managed ComfyUI startup keeps a stable process handle across shutdown races, and core PPTX inspection rejects aggregate XML/relationship expansion above 256 MiB before parsing.
- **Cross-monitor DPI font resizing:** on Windows Per-Monitor V2 displays, FolderBridge now explicitly recalculates named-font pixel sizes whenever the launcher moves between monitors with different scaling. Existing ttk labels/buttons/entries/Treeview text, the runtime log, the primary start/stop button, and setup-guide text all resize with the new monitor instead of relying on Tk's undefined runtime `tk scaling` behavior for existing widgets.
- **Long-running Extension jobs:** actions may use `run_mode=job`, return immediately with a FolderBridge-owned `job_id`, and be inspected or cancelled through the same stable `extension` gateway. Timeouts may be configured from 1 second through 24 hours; `0` means no automatic timeout termination. Job ownership is bounded to 16 concurrently active jobs (including `termination_pending`) and 128 retained finished records per FolderBridge process; foreground Extension workers have a separate 16-worker lifecycle budget.
- **Explicit secret/environment boundary:** extensions still start from a cleaned environment. Only variables named by exact `environment.inherit:NAME` permissions are copied; FolderBridge's `CONTROL_PLANE_API_KEY` and internal/control-plane names are permanently reserved. Values inherited through key/token/secret/password/auth-like names are recursively redacted from surfaced results/logs/errors. This makes API-backed integrations possible without exposing the Tunnel control key.
- **External HTTPS declaration:** API-backed plugins can declare `network.outbound:https` so the user sees the network requirement during exact-hash approval. Permission declarations remain authorization contracts rather than a kernel sandbox.
- **Host-validated artifacts:** plugins can return `workspace_artifacts`; FolderBridge revalidates each workspace-relative path and attaches size/SHA-256 metadata before exposing the result.
- **One deep process-ownership module:** Extension workers/jobs, Tunnel commands, managed ComfyUI, approved tasks, and bounded Git inspection share one Windows/POSIX process-group implementation. Timeout, cancel, and shutdown terminate the complete FolderBridge-owned process tree, including child runtimes such as Node.js.
- **Content-sized, collapsible launcher:** after Tk layout, the launcher grows to the actual requested page height/width up to 94% of the screen. Local Workspace/Permissions, Tunnel settings, and Runtime Log can each collapse independently, with a global Expand all / Collapse all control. The page scrollbar is hidden when everything fits and appears only as a fallback when visible content is taller than the available viewport; DPI changes, section toggles, and the Extension sidebar trigger a recalculation.
- **Live managed-service status:** while the Extensions sidebar is open, managed ComfyUI status is refreshed every 2 seconds. Online states are green, offline/unconfigured states red, and detection/startup states neutral. Stable probes update the existing status label instead of rebuilding the whole sidebar.
- **Judge-style API config files stay hidden:** `.api-config.json` / `api-config.json` are now treated as credential-like workspace files and are not exposed through normal MCP file tools.
- **Browser-authorized GitHub publishing:** the bundled `git-publisher` extension can open GitHub authorization through Git Credential Manager, keep OAuth credentials in Windows Credential Manager, commit only an explicit file allowlist, push only the current branch to the existing GitHub HTTPS origin, and publish explicit workspace files as GitHub Release assets with bounded tag/title/filename inputs. Long generic Release uploads run as host-owned Jobs. No token/PAT/password field is exposed to the model.
- **Git publication remains constrained:** Publisher rejects pre-existing staged changes, credential/key-like files, content-transforming Git attributes and unsafe repository-local Git settings; it never runs `git add .`, never accepts an arbitrary remote/ref, disables hooks/signing for its bounded commit, and never force-pushes.
- **Native Microsoft Office visual pipeline:** the bundled `office` extension can render `.pptx`, `.docx`, and `.xlsx` with locally installed Microsoft Office. PowerPoint exports slides directly to PNG; Word/Excel use their native fixed-format layout engines and the Windows PDF renderer to produce page PNGs.
- **Word structure reading without launching Word:** `inspect_docx` exposes paragraphs, styles/numbering, tables, sections/page setup, headers/footers, media, hyperlinks, footnotes/endnotes and comments from OOXML.
- **Excel structure/formula reading without launching Excel:** `inspect_xlsx` exposes sheets, bounded cell ranges, formulas plus cached values, merged ranges, hidden rows/columns, defined names, calculation settings and external-link parts.
- **Audit-ready output:** native rendering can write a deterministic PNG directory plus sibling ZIP and returns SHA-256/size metadata for both the Office source and generated outputs; the existing `image_open` tool can then inspect each page or ZIP member visually.
- **Office automation is constrained:** only workspace-relative non-link paths are accepted; macro-enabled formats are excluded; Office automation macros are force-disabled; files open read-only; Excel link updates are disabled; the bundled PowerShell script is fixed and never accepts arbitrary commands.
- **Existing 0.4.x safety/UI improvements remain:** high-DPI scrolling, ComfyUI managed-service diagnostics, stable extension gateway, bounded global capabilities, and single-file Windows delivery are unchanged.

See [CHANGELOG.md](CHANGELOG.md) for the full release history and [Extension ABI v1](docs/extensions.md) for managed-service and plugin boundaries.

## Why FolderBridge?

- **Independent folder boundaries:** one connection can contain up to 16 canonical workspaces; every multi-workspace tool call selects a `workspace_id` instead of merging roots.
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
- Git is optional. Bounded `status`/`diff` views use Git when available; the Git Publisher extension additionally requires Git for Windows with Git Credential Manager for browser-authorized GitHub commits/pushes.

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

1. Add one or more explicit workspace folders (up to 16) and keep **Read only (recommended)** for the first run. The global access mode applies to every listed folder.
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
- `flight_recorder`: reads the built-in reliability flight recorder through `status`, `recent`, or `errors`; it keeps only compact MCP/Tunnel metadata from the latest 15 minutes under a 20 MiB cap and never records workspace request/response bodies;
- `workspace`: lists and reads files, streams literal search across UTF-8 files up to 512 MiB, and exposes pageable/path-scoped Git status/diff output inside the selected `workspace_id`;
- `file_info`: returns bounded metadata and a whole-file SHA-256 for regular files; use it to obtain the current SHA before editing text above 1 MiB;
- `pptx_inspect`: inspects PPTX text, OOXML diagram relationships, and SmartArt data without executing Office content;
- `image_open`: returns bounded PNG/JPEG/GIF/WebP image content to the MCP client, including exact image members inside ZIP files;
- `extension`: the stable gateway for hot-loaded extensions; `list` returns a compact pageable catalog, `info` returns one extension's full action schemas, and `run` invokes the selected action without adding MCP tool names;
- `edit_file`: in read/write mode, creates small inline UTF-8 files and exactly edits existing UTF-8 text up to 128 MiB; existing-file edits require a current whole-file SHA-256, while large whole-file creation uses `write_file`;
- `write_file`: in read/write mode, provides `begin`, `append`, `status`, `commit`, and `abort` for transactional whole-file UTF-8 creates or replacements up to 512 MiB while the MCP single-message limit remains 1 MiB.

With one workspace, existing clients may continue omitting `workspace_id`. With multiple workspaces, workspace-scoped tools require a `workspace_id` returned by `server_info`; missing or unknown IDs are rejected. Duplicate roots, parent/child overlaps, and lists beyond eight entries are rejected before startup.

For local exact replacements, list/search the file, inspect the relevant range, retain the current whole-file SHA-256, call `edit_file`, then inspect the Git diff. `workspace(read)` returns the whole-file SHA for files up to 1 MiB; use `file_info` for larger files. New-file publication is no-clobber, and existing-file edits recheck the expected SHA immediately before atomic publication. If the underlying filesystem cannot provide safe atomic no-clobber publication, creation fails safely instead of falling back to an overwrite.

For a large whole-file create or replacement, use `write_file`: call `begin` (`replace` also requires the old SHA from `file_info`), send `append` chunks at the exact next UTF-8 byte offset, optionally call `status`, then `commit` with the complete new byte size and SHA-256 or `abort`. Each chunk is capped at 128 KiB so worst-case JSON escaping stays below the unchanged 1 MiB MCP message ceiling. Transactions are process-local, stage outside the workspace, support files up to 512 MiB, and clean stale staging older than 24 hours on a later startup. Commit revalidates UTF-8, size, new SHA, workspace/link policy, and replacement baseline before a fully written same-directory temporary file is atomically published; create mode never overwrites a path that appeared after `begin`.

Large-file inspection no longer falls back to a small-file search ceiling. Literal search scans UTF-8 content as a stream up to 512 MiB per file and reports binary/non-UTF-8/oversized/I/O skips separately; list and search results use result offsets, while Git status/diff use byte offsets and may be scoped to one workspace-relative path. These are bounded pages, not whole-response expansion: the 1 MiB MCP envelope remains unchanged.

Skill routing follows the same scaling rule. Initialization carries a compact 64 KiB round-robin routing index rather than embedding Skill bodies; if enabled Skills do not fit, the index says how many were omitted and `skill-engine match` remains the complete task-specific discovery path. Extension `list` similarly stays compact and pageable, with full schemas deferred to `extension(info)`.

External Skill Packs and Extensions are user installations, not release payload. Their exact-hash approval records live under the user-level FolderBridge configuration and survive an EXE upgrade as long as the external bytes/declared permissions do not change. The repository `skill_packs` and `extensions` trees are release-owned source locations; the Windows build packages only an explicit bundled allowlist, so unrelated local/untracked folders there are not promoted into the EXE. If a later release intentionally ships a bundled component with the same ID as an older external installation, the bundled copy safely takes precedence and the old external copy is ignored rather than causing a duplicate-ID startup error; external code can never override a bundled ID.

### Bounded MCP concurrency

FolderBridge 0.8.0 uses separate bounded request lanes so a long data-plane operation does not make control/status calls unresponsive. The current defaults are 2 control workers with at most 8 in-flight control requests and 6 data workers with at most 12 in-flight data requests. Saturation never creates an unbounded queue: excess requests fail fast with JSON-RPC `-32001` / `Server busy`. Concurrent responses may complete out of request order, as JSON-RPC permits, but FolderBridge serializes complete JSONL writes so response bytes never interleave.

Control work includes initialization/ping/catalog calls, `server_info`, `flight_recorder`, extension list/info/job status/cancel, and transactional write status/abort. Reads and independent data work can proceed while other data operations are running. Core writes to the same target file are serialized; different target files can overlap. Tasks, build/package/capability execution, and non-read-only Extension actions are treated as opaque workspace mutations and cannot overlap core file writes in that workspace. Both foreground non-read-only Extension actions and non-read-only Extension Jobs keep the workspace mutation lease until the host has confirmed that the worker process exited. If termination cannot be confirmed immediately, the worker enters host-owned `termination_pending` handling and the lease stays held; bounded daemon reapers reconcile it automatically when the process later exits. The 16-Job and 16-foreground-worker lifecycle budgets remain separate from MCP request-worker limits. On stdio shutdown FolderBridge first closes mutation admission and wakes queued mutation waiters, then retries termination of owned Extension workers, drains bounded request workers, and finally cleans transactional staging.

The server cannot delete or move files. Absolute paths, `..`, symlinks, junctions/reparse points, VCS internals, common dependency/build folders, and credential-like names are denied.

## Global pre-authorized capabilities

The launcher can authorize common capabilities once for all current and future workspaces: `test`, `build`, `package-windows`, `package-android`, `release-sync`, and `git-push`. These permissions live in the launcher settings rather than `.folderbridge.json`. The launcher also provides **Select all** and **Clear** controls for this group.

For Node-based repositories, packaging and delivery handoff entry points can be declared explicitly in `package.json` as `package:windows`, `package:android`, and `release:sync`. FolderBridge discovers those fixed script names at call time and executes them only as `npm run package:windows`, `npm run package:android`, or `npm run release:sync`; it never copies the script body into a shell command. Existing bounded Windows PowerShell/PyInstaller and Android Gradle/Flutter discovery remains available as a compatibility fallback. `release-sync` intentionally has no implicit fallback: the repository owns the exact release/staging destinations and validation rules.

For globally authorized `test` and `build`, FolderBridge always exposes a provider for every selected workspace. A recognized project entry point such as `npm run test`, `npm run build`, Python unittest/pytest, and similar supported commands wins first. If no project entry point exists, `test` falls back to a FolderBridge-owned bounded workspace smoke that reuses the normal credential/VCS/dependency/link-denial policy, checks common UTF-8 text plus JSON/HTML structure, and optionally uses trusted system Node with `--check` for bounded JavaScript syntax parsing without executing workspace JavaScript. `build` falls back to a non-mutating safe build: static/content/no-build folders use explicit `identity` mode, while source folders without a build entry point use explicit `validation-only` mode. Neither fallback invents compiled artifacts, and `server_info` reports the selected provider.

`git-push` remains a low-level push-only capability constrained to a GitHub HTTPS `origin`, the current branch, no force push, and no repository-local credential helper / push-target rewrite configuration. For browser authorization plus explicit-file commit + push, use the bundled **Git Publisher** extension instead. Explicit project build/package capabilities may execute local project code, so enable only the global capabilities you want.

For direct stdio clients, repeat `--capability <name>` on the `serve` or `client-config` command. The Windows launcher exposes the same choices as persistent checkboxes.

## Extensions

FolderBridge Extension ABI v1 is the preferred way to add future global integrations such as ComfyUI, FFmpeg, Blender, Ollama, ADB, or other local tools without changing the MCP tool catalog. The Windows launcher has a default-collapsed **Extensions** sidebar that hot-scans the user extension directory, shows installed/approved/loaded state, and lets you approve or disable plugins while the Tunnel remains connected.

Each extension is a directory containing at least `folderbridge-extension.json` and `plugin.py`. External plugins are approved against the exact SHA-256 of the complete plugin directory plus the declared permission list; changing any file or permission makes the approval stale. Extensions execute in a separate subprocess with a cleaned environment, bounded protocol I/O, and a declared timeout. This isolates crashes and protocol pollution, but it is **not an OS security sandbox**: use a VM/container for untrusted plugin code.

Workspace-specific adaptation should use `workspace_adapter.mode=dynamic` with `detect.any_of` / `detect.all_of`. FolderBridge re-evaluates those patterns at call time, so extensions do not need to inject `.folderbridge.json` tasks when installed and can become applicable after a project changes later. Persistent plugin state should use the provided profile state directory rather than polluting repositories.

Keep public Extension actions **small, fixed, and semantically bounded**. Do not expose aggregate `run-all`, `verification-suite`, or `do-everything` actions that bundle dozens of independent tests/checks or a multi-stage pipeline into one foreground MCP call. Split them into fixed allowlisted actions with predictable timeout/output bounds and let the client invoke those steps in order. A read-only `verification-plan` may return the recommended sequence without executing it. A genuinely atomic long-running operation may use host-owned Job mode when the connected client can reliably query/cancel jobs, but Job mode should not be used to hide an action that should have been decomposed.

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

### Publish a GitHub Release

Repository pushes and GitHub Releases are intentionally distinct. Git Publisher 1.3.1 keeps the parameterless `release` action compatibility-locked to FolderBridge itself: it reads the stable `x.y.z` version only from `pyproject.toml`, requires branch `main`, a clean tracked working tree, and `origin/main` at the current HEAD, then creates or validates only tag `v<version>` and uploads only `release/windows-x64/FolderBridge.exe` plus `FolderBridge.exe.sha256`. The separate generic `release-assets` action works only on the selected workspace repository and accepts a bounded tag/title plus an explicit allowlist of regular workspace files; tracked content must be clean, the current branch must already match origin, tag movement/force push are rejected, and explicit untracked build artifacts may be uploaded under GitHub-stable ASCII Release filenames with optional user-facing display labels. Assets are SHA-256 checked and snapshotted before remote mutation, and long uploads run as host-owned Jobs. Release authentication reuses the existing browser-authorized Git Credential Manager account: the isolated worker obtains the credential from GCM and passes it only in the child `gh.exe` process environment for the duration of the operation, so no separate `gh auth login` is required. No token/PAT/password or arbitrary Git/`gh` command input is accepted from the model, and the credential is not persisted by FolderBridge or returned through MCP.

The repository-side `.github/workflows/release-windows.yml` remains a second publication path for commits titled exactly `Release FolderBridge <version>`. It independently re-reads the version, runs the full Windows tests, builds and verifies the EXE, and creates or repairs the matching Release. Existing tags are accepted only when they resolve to the same release commit, and existing Releases can be repaired by re-uploading the two fixed assets and marking that version Latest.

## License

FolderBridge's project-authored code and documentation are licensed under the [Apache License 2.0](LICENSE).

The bundled `folderbridge-engineering` Skill Pack contains condensed/adapted methodology text derived from selected Skills in Matt Pocock's [`mattpocock/skills`](https://github.com/mattpocock/skills) project. Upstream copyright is held by Matt Pocock and the upstream material is licensed under MIT. FolderBridge preserves the attribution and full MIT permission notice in [`skill_packs/matt-pocock-engineering/NOTICE.md`](skill_packs/matt-pocock-engineering/NOTICE.md) and [`LICENSE.upstream-MIT.txt`](skill_packs/matt-pocock-engineering/LICENSE.upstream-MIT.txt). The Pack is a FolderBridge adaptation, not the official Matt Pocock plugin, and no affiliation or endorsement is implied.

FolderBridge MCP is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by OpenAI. ChatGPT, OpenAI, MCP, and other product names belong to their respective owners.
