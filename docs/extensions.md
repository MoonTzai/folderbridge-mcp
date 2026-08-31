# FolderBridge Extension ABI v1

FolderBridge 0.8.21 uses one stable MCP gateway, `extension`, for local integrations. Installing an extension changes the extension registry, not the MCP tool catalog. Clients can call `extension(action="list")`, inspect the returned action schemas, then call `extension(action="run", extension_id=..., extension_action=..., params=...)`. Actions declared with `run_mode="job"` return a host-owned `job_id`; use `extension(action="job_status", job_id=...)` or `extension(action="job_cancel", job_id=...)` without registering extra MCP tools. Each FolderBridge process admits at most 16 concurrently starting/running Extension jobs and retains only the newest 128 finished job records.

## Directory layout

```text
<extension-id>/
  folderbridge-extension.json
  plugin.py
  ... optional sibling modules/data ...
```

User extensions live under the per-user FolderBridge configuration directory, normally `%LOCALAPPDATA%\folderbridge-mcp\extensions` on Windows. The Windows launcher opens this directory from the default-collapsed **Extensions & Skills** sidebar.

## Skill Engine is a separate trust model

FolderBridge 0.8.21 also ships a local Skill Engine, exposed through the bundled read-only `skill-engine` Extension. Skill Packs contain methodology text rather than executable plugin code, so they do not use Extension permissions or the Extension code-approval ABI. External Skill Packs have their own exact-tree-hash approval and enable/disable state; unapproved or stale Pack metadata is not exposed to the model-facing `list`, `match`, `get`, or routing index. The `skill-engine` adapter itself stays thin and delegates parsing, trust, matching, and byte verification to the core Skill Engine.

The Launcher manages these two systems in the same sidebar for convenience, but their security semantics remain separate: Extensions may execute approved Python in workers, while Skill markdown is only returned as bounded UTF-8 methodology content and is never executed by the Skill Engine.

## Manifest

A v1 manifest uses this shape:

```json
{
  "schema_version": 1,
  "id": "example-tool",
  "name": "Example Tool",
  "version": "1.0.0",
  "description": "Example FolderBridge extension",
  "entrypoint": "plugin.py",
  "permissions": [
    "workspace.read",
    "extension.state"
  ],
  "execution": {
    "mode": "isolated-process",
    "timeout_seconds": 180
  },
  "workspace_adapter": {
    "mode": "dynamic",
    "state": "profile",
    "detect": {
      "any_of": ["pyproject.toml", "package.json"],
      "all_of": []
    }
  },
  "actions": {
    "status": {
      "read_only": true,
      "requires_workspace": false,
      "authorization": "global",
      "input_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": false
      }
    }
  }
}
```

`id` must match `[a-z0-9][a-z0-9._-]{0,63}`. The entrypoint must be a regular `.py` file inside the extension directory. Unknown manifest fields, links/reparse points, unknown permissions, path traversal, oversized extension trees, and unsupported execution modes are rejected before code runs.

## Permissions

ABI v1 accepts precise permission contracts only:

- `workspace.read`
- `workspace.write`
- `workspace.adapter`
- `extension.state`
- `git.commit-selected-files`
- `git.push-current-branch`
- `github.web-auth`
- `network.loopback:127.0.0.1:<port>` or `network.loopback:localhost:<port>`
- `network.outbound:https`
- `process.execute:<basename>`
- `environment.inherit:<UPPERCASE_NAME>`

There is no wildcard shell or wildcard network permission. `network.outbound:https` is an explicit authorization-contract declaration for plugins that need external HTTPS APIs; Extension permissions are not a kernel network sandbox. `environment.inherit:<UPPERCASE_NAME>` copies only that exact variable into the otherwise cleaned worker environment. `CONTROL_PLANE_API_KEY`, FolderBridge/control-plane variables, PATH/runtime bootstrap variables, and other reserved names cannot be inherited. Values inherited through variable names containing key/token/secret/password/passwd/auth are treated as secrets and recursively redacted from worker results, worker logs, surfaced stderr, and errors. A dynamic workspace adapter requires `workspace.adapter`; `workspace_adapter.state=profile` requires `extension.state`. Declaring `extension.state` is the authorization that makes FolderBridge provision `context.state_dir`; the adapter state field does not independently grant or withhold that directory.

The permission list is part of the approval identity. External extensions are approved against the SHA-256 of the complete extension tree plus the exact permissions. If any file or permission changes, the old approval becomes stale and execution is blocked until the user approves the new hash. At execution time the worker copies the hash-covered tree into a private temporary snapshot, verifies that snapshot against the host-pinned hash, and imports/runs only from the verified snapshot so the checked bytes and executed bytes stay aligned.

Permission declarations are an authorization contract, not an operating-system sandbox. Python code approved by the user still runs with that user's OS permissions. Use a VM/container for untrusted plugin code.

## Plugin entrypoint

`plugin.py` must expose:

```python
def handle(action, params, context):
    if action == "status":
        return {"ready": True}
    raise RuntimeError("unsupported action")
```

`params` has already been checked against the action's declared `input_schema`. `context` contains:

- `extension_id`
- `extension_version`
- `permissions`
- `workspace_root` or `null`
- `workspace_read_only`
- `state_dir` or `null` — non-null whenever `extension.state` is declared; FolderBridge creates `<extension-state-root>/<extension_id>/<workspace_id>/` for workspace-bound calls and `<extension-state-root>/<extension_id>/global/` when no workspace is selected
- `workspace_adapter`

The return value must be a strict JSON object. Non-standard numeric constants such as `NaN` and `Infinity` are rejected at manifest, request, response, and worker serialization boundaries. As with built-in tools, a result may contain `_content` with bounded MCP content items when an action intentionally surfaces content. A result may also contain `workspace_artifacts`, either workspace-relative strings or `{path,label,kind}` objects. FolderBridge re-resolves each declared artifact through workspace path policy and replaces it with trusted relative-path, byte-size, and SHA-256 metadata before exposing the result.

External Extensions that need a structured failure code should use the deliberately small public error ABI instead of importing FolderBridge internals:

```python
from folderbridge_mcp.extension_api import ExtensionError

raise ExtensionError("EXAMPLE_FAILED", "The operation failed.", retryable=False)
```

The worker preserves `code`, message, and JSON-safe `details` in the normal Extension error envelope. Unknown exceptions still become `EXTENSION_WORKER_EXCEPTION`. `ExtensionError` is intentionally the only public helper here: it does not expose FolderBridge filesystem, network, process, trust-store, or workspace internals.

ABI v1 plugins should otherwise rely on the Python standard library or their own sibling modules/data. A one-file EXE is not a general pip environment. Integrations that need external software should normally call a fixed local HTTP API or declare `process.execute:<basename>` and invoke that installed program.

## Workspace adapters instead of task injection

Extensions must not require installation-time edits to every workspace's `.folderbridge.json`. When project structure matters, use:

```json
"workspace_adapter": {
  "mode": "dynamic",
  "state": "profile",
  "detect": {
    "any_of": ["scripts/build_windows.ps1", "*.spec"],
    "all_of": []
  }
}
```

FolderBridge evaluates these patterns every time the extension registry is queried or an action runs. A project can therefore gain a new build script months after the workspace was added and immediately become applicable. Plugin persistent state belongs in `context.state_dir` by default, outside the repository. Request `extension.state` whenever that directory is needed, even when `workspace_adapter.mode=none` and `workspace_adapter.state=none`; `state=profile` remains an adapter declaration that requires the same permission, not the switch that provisions storage.

## Action granularity: avoid aggregate public actions

Public actions should be small, fixed, and semantically bounded. Do not expose `run-all`, `verification-suite`, `do-everything`, or similar aggregate entry points that bundle many independent tests/checks or a multi-stage pipeline into one foreground MCP request. Large aggregate calls are harder to diagnose, produce less predictable output/timeout behavior, and can interact poorly with client-side safety or request limits even when every underlying step is individually safe.

Prefer a fixed allowlist of actions whose names describe one bounded operation. If a workflow needs several of them, let the client invoke the actions sequentially and return each result independently. A read-only `verification-plan` action may return the recommended sequence as data, but it should not itself spawn subprocesses or execute the workflow. Do not expose arbitrary test-file, command, executable, URL, or shell parameters merely to reduce the number of declared actions.

A genuinely atomic long-running operation may use host-owned `run_mode="job"` when the connected client can reliably query `job_status` and issue `job_cancel`. Job mode is not a substitute for decomposition: an action that is conceptually many independent checks should still be split even if it could technically run as one Job. Every action should retain explicit timeout/output bounds and return compact diagnostics.

## Workspace mutation scopes

An action may declare `mutation_scope` so FolderBridge can coordinate only the workspace paths that the action can actually mutate. The host resolves the effective scope from the already schema-validated parameters **before the worker starts** and acquires the corresponding mutation lease before execution or Job registration.

Supported modes are:

- `none`: no FolderBridge-workspace mutation is claimed.
- `workspace`: the action is opaque and may mutate anywhere in the selected workspace.
- `paths`: one or more `exact` or `tree` claims, each using either a fixed POSIX-relative `path` or a top-level string `param`. Parameter claims may be `optional:true`.

Example:

```json
"mutation_scope": {
  "mode": "paths",
  "claims": [
    {"param": "save_directory", "kind": "tree", "optional": true}
  ]
}
```

Path scopes require `requires_workspace:true` and `workspace.write`; `authorization:none` actions may not claim mutations. Scope paths must be clean non-glob relative paths and are resolved through the selected workspace boundary before the worker is spawned. Exact/exact, exact/tree, tree/tree, and workspace-opaque conflicts are serialized, while disjoint path scopes may run concurrently. The lease remains held until the real worker/process exit, including Job timeout or termination-pending states.

For backward compatibility, an action that omits `mutation_scope` keeps the old semantics: `read_only:true` resolves to `none`, while a non-read-only action resolves to opaque `workspace`. New write-capable integrations should prefer an explicit scope when they can truthfully bound their workspace writes. If FolderBridge itself is running read-only and the effective scope is not `none`, the host rejects the call with `READ_ONLY` before worker startup.

## Authorization and hot loading

`action.authorization` is either `global` or `none`. Each action may additionally declare `run_mode` (`foreground`, the default, or `job`) and an optional `timeout_seconds` override. A Job action returns quickly with a `job_id`, allowing long operations to run without holding one foreground MCP request open.

- `global`: the user must approve the exact extension hash/permissions and enable it once in the Extensions sidebar.
- `none`: reserved for read-only actions of extensions bundled with FolderBridge, such as the bundled Skill Engine's read-only actions. External plugin code is never executed without hash approval.

The registry is scanned on demand. Adding, editing, approving, disabling, or removing plugins does not require a new MCP tool registration or FolderBridge restart. The sidebar's **重新扫描** button only refreshes the GUI view; MCP `extension(list/info/run)` also sees the current filesystem/trust state on its next call. If any hash-covered external Extension file changes, the old approval becomes stale immediately; re-approving the new hash restores loading in the same running Registry process.

## Execution boundary

Plugins run in a separate FolderBridge worker process with:

- a cleaned environment; only exact `environment.inherit:NAME` declarations are copied from the host;
- no Tunnel/control-plane Runtime API key passed to plugin code, and that variable is explicitly non-inheritable;
- a private verified execution snapshot used as both import root and working directory;
- default foreground execution plus optional host-owned `run_mode="job"` for long tasks;
- per-action or extension-default `timeout_seconds` from 0 through 86,400 seconds (24 hours), where `0` disables automatic timeout termination;
- complete owned process-tree termination on non-zero timeout, explicit `job_cancel`, or FolderBridge shutdown;
- bounded request, response, stdout, and stderr;
- PyInstaller environment reset when running from the one-file Windows EXE.

Even with `timeout_seconds=0`, an explicit cancel or FolderBridge shutdown still cleans up the owned worker tree. Job records are owned by the running FolderBridge MCP process rather than persisted as detached background daemons. This boundary prevents plugin prints/crashes from corrupting MCP stdio and limits accidental runaway output. It does not provide kernel-level filesystem/network isolation.

## Bundled Microsoft Office Native extension

`extensions/office` adds a Windows-native document pipeline without changing the MCP tool catalog. Its bundled read-only actions can inspect Word/Excel OOXML directly, while the write-capable `render` action requires one-time global approval because it launches locally installed Microsoft Office and writes PNG/ZIP output into the selected workspace.

- `inspect_docx` parses paragraphs, styles/numbering, tables, sections/page settings, headers/footers, media, hyperlinks, footnotes/endnotes and comments without launching Word.
- `inspect_xlsx` parses workbook/sheet structure, bounded cell ranges, formulas plus cached values, shared/inline strings, merged ranges, hidden rows/columns, defined names, calculation properties and external-link parts without launching Excel.
- `render` accepts only `.pptx`, `.docx`, and `.xlsx`. PowerPoint uses native `Slide.Export`; Excel uses its native fixed-format engine plus `Windows.Data.Pdf`. Word is deliberately split across two child PowerShell processes: a Word-only `ExportAsFixedFormat` stage exits and releases its COM server before a separate WinRT-only PDF rasterization stage begins. The public `width` parameter is always the final physical PNG width in pixels; because WinRT PDF render dimensions are expressed in DIPs, Word/Excel convert the pixel target through the current system DPI and correct any final DIP-quantization drift before returning. It runs as a host-owned `run_mode="job"` action so native Office work cannot hold one foreground MCP request open for its full duration; poll `job_status` for completion. It may also create a sibling ZIP and the successful Job result returns hashes/sizes for the source and every output.

The Office extension declares `process.execute:powershell.exe`, but the command surface is fixed to bundled `office.ps1`, `word_export.ps1`, and `pdf_render.ps1`, always with `shell=False`. There is no user-supplied command, script path, URL, or executable parameter. Word ownership is fail-closed: the export stage records the `WINWORD.EXE` PID set before COM startup, accepts exactly one newly created Word PID, and the plugin opens a verified Windows process handle to that instance so completion/cancel/timeout can reap only the render-owned Word process rather than issuing a name-wide kill. Workspace paths are relative-only, links/reparse points and dependency/VCS/build directories are rejected, macro-enabled Office formats are not accepted, Office automation security is forced to disable macros before opening, documents are opened read-only, and Excel link updates are disabled. Intermediate PDFs live under `context.state_dir` and are deleted after rendering.

When `render` succeeds, use the PNG paths from the completed Job result with FolderBridge `image_open`; for audit workflows this keeps OOXML/Office originals as structural evidence and native page images as visual evidence instead of treating flattened text as a substitute for layout.

## Bundled Git Publisher extension

`extensions/git-publisher` adds a narrow GitHub publication workflow without exposing generic Git command execution. Its read-only `status` action can inspect the selected repository before approval; browser authorization and mutations require one-time global approval.

- `connect` invokes Git Credential Manager's GitHub `login --web` flow. GitHub opens in the user's browser and the resulting credential is stored by GCM in Windows Credential Manager. The action schema intentionally has no token/PAT/password parameter.
- `commit` accepts an explicit file allowlist plus a commit message. It rejects pre-existing staged content, paths outside the workspace, directories/deletions, credential/key-like files, generated/dependency/VCS directories, and files with content-transforming Git attributes; it verifies the staged set exactly before committing and disables Git hooks/signing.
- `push` accepts no ref/remote/URL argument. It reuses the current named branch and existing `origin`, requires a credential-free GitHub HTTPS origin, forces GCM as the credential helper, disables interactive prompts and pre-push hooks, and never force-pushes.
- `release` remains compatibility-locked to FolderBridge's own versioned Windows release and accepts no parameters.
- `release-assets` is the generic Release path. It accepts only a bounded tag/title, an explicit 1..64 workspace-file allowlist with optional GitHub-stable ASCII download filenames and optional user-facing display labels, and a boolean Latest request. The tracked worktree must be clean, the current branch must already match `origin`, existing tags may not move, explicit untracked build artifacts are allowed, and every asset is SHA-256 checked and copied to a temporary snapshot before any remote mutation. The action is host-owned `run_mode="job"` with a two-hour timeout so long uploads can be polled or cancelled.

The Git Publisher permission declaration is deliberately explicit: `github.web-auth`, `git.commit-selected-files`, `git.push-current-branch`, `process.execute:git.exe`, and `process.execute:gh.exe`. PAT authentication remains an out-of-band fallback through user-controlled Git/GCM configuration; FolderBridge does not ask the model to receive a secret token.

## External hot-load ComfyUI reference

`Plugins/extensions/comfyui` is the reference external Extension. It is **not** part of the default bundled Extension set and is not embedded into `FolderBridge.exe`. Install it into the per-user Extension directory (normally `%LOCALAPPDATA%\\folderbridge-mcp\\extensions\\comfyui`) either by copying the complete directory or by running its `install.ps1`, then rescan, review the exact directory hash + permissions, approve, and enable it. Updating any hash-covered plugin file makes the old approval stale; the next registry scan sees the new bytes without restarting FolderBridge.

Its manifest declares `network.loopback:127.0.0.1:8188`, `workspace.read`, and `workspace.write`. Because it is external, both `status` and `run` use `authorization:"global"`. `run` is a host-owned Job with no host auto-timeout (`timeout_seconds:0`) while its own workflow timeout remains bounded to 24 hours unless the caller explicitly chooses `0`. The runtime uses only Python standard library modules, sibling plugin code, and the public `folderbridge_mcp.extension_api.ExtensionError` ABI; it does not import `folderbridge_mcp.comfyui`, `folderbridge_mcp.security`, or other product-private helpers.

The helper disables proxies and redirects, accepts only API-format workflow JSON from the selected workspace, preflights supported dynamic-combo inputs, returns bounded artifact metadata, and does not fetch generated media bytes unless needed for an explicit workspace `save_directory`. Workspace path handling preserves traversal/link/reparse/sensitive-path protections. Cancellation uses the host Job cancel token plus ComfyUI's prompt-scoped `/api/jobs/<prompt_id>/cancel` endpoint and never falls back to the process-global `/interrupt` endpoint.

The ComfyUI `run` action declares an optional tree mutation claim on `save_directory`. With no `save_directory`, the effective workspace mutation scope is `none`; with one, only that resolved tree is leased. ComfyUI's own native output directory is external service state and is not falsely represented as a FolderBridge-controlled workspace mutation unless the user explicitly requests a workspace copy.

### Launcher-owned managed service

ComfyUI process startup is deliberately **not** an Extension worker permission and is not exposed through the MCP `extension` action surface. FolderBridge keeps an optional Launcher-owned managed-service controller for `comfyui`, but that controller has its own bounded loopback `/system_stats` probe and does not import the external plugin runtime or the retired core ComfyUI helper. This keeps the LLM/worker boundary separate from local process ownership and also means the external Extension works normally when the user starts ComfyUI manually.

The launcher accepts a **ComfyUI directory**, never an arbitrary executable or command. Supported layouts are:

```text
# Portable
python_embeded\\python.exe
ComfyUI\\main.py

# Source
main.py
.venv\\Scripts\\python.exe
# or
venv\\Scripts\\python.exe
```

The saved local application-state file is normally `%LOCALAPPDATA%\\folderbridge-mcp\\extension-state\\comfyui\\launcher-service.json` and contains only `version`, `install_root`, and `auto_start`. It never persists a PID, process handle, BAT/CMD path, or arbitrary command.

Before every launch the directory is revalidated. FolderBridge invokes the explicit Python executable and `main.py` with `shell=False`, the appropriate ComfyUI working directory, and fixed `--listen 127.0.0.1 --port 8188` arguments; Portable builds also receive the standalone-build flag. A BAT/CMD file is never used as the launcher entry point.

Ownership is runtime-only. If `127.0.0.1:8188` is already healthy, the launcher marks it as an **external service**, does not start another instance, and never stops it. Only the `Popen` handle that this FolderBridge run itself created is considered **owned**. Disable/revoke and application shutdown may stop that owned process tree through the saved in-memory handle. FolderBridge never discovers a PID from port 8188, never kills an unknown port owner, and never uses a historical PID. If the owned process has been stopped but port 8188 remains online, the launcher reports that another process owns the port and leaves it untouched.

Application shutdown is orchestrated outside the Tk main thread: loaded extensions are handled in deterministic order, owned managed services stop and wait for their expected ports first, then Tunnel/MCP stops, in-memory Runtime API key data is cleared, and `root.destroy()` runs back on the Tk thread. A managed-service startup failure is logged in the GUI and does not block Tunnel startup.

## Prompt for an LLM plugin author

The Windows connection guide contains a **复制给 LLM 的插件开发指令** button. That prompt deliberately tells the LLM not to guess undocumented local APIs. Before writing a plugin, the LLM must decide whether it has enough information and, when needed, actively ask the user to upload or provide the minimum relevant materials, such as API/CLI documentation, `--help` output, existing scripts/config, workflow JSON, sample requests/responses, a representative project tree, or a non-sensitive sample proprietary file.

The LLM should prefer file uploads over asking the user to paste long or binary contents. Once the necessary evidence is present, it should generate the manifest, entrypoint, tests, and README directly and self-check the declared permissions, action schemas, adapter rules, path handling, timeouts, and absence of arbitrary command/URL parameters.
