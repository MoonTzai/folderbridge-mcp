# FolderBridge Extension ABI v1

FolderBridge 0.7.0 uses one stable MCP gateway, `extension`, for local integrations. Installing an extension changes the extension registry, not the MCP tool catalog. Clients can call `extension(action="list")`, inspect the returned action schemas, then call `extension(action="run", extension_id=..., extension_action=..., params=...)`. Actions declared with `run_mode="job"` return a host-owned `job_id`; use `extension(action="job_status", job_id=...)` or `extension(action="job_cancel", job_id=...)` without registering extra MCP tools. Each FolderBridge process admits at most 16 concurrently starting/running Extension jobs and retains only the newest 128 finished job records.

## Directory layout

```text
<extension-id>/
  folderbridge-extension.json
  plugin.py
  ... optional sibling modules/data ...
```

User extensions live under the per-user FolderBridge configuration directory, normally `%LOCALAPPDATA%\folderbridge-mcp\extensions` on Windows. The Windows launcher opens this directory from the default-collapsed **Extensions & Skills** sidebar.

## Skill Engine is a separate trust model

FolderBridge 0.7.0 also ships a local Skill Engine, exposed through the bundled read-only `skill-engine` Extension. Skill Packs contain methodology text rather than executable plugin code, so they do not use Extension permissions or the Extension code-approval ABI. External Skill Packs have their own exact-tree-hash approval and enable/disable state; unapproved or stale Pack metadata is not exposed to the model-facing `list`, `match`, `get`, or routing index. The `skill-engine` adapter itself stays thin and delegates parsing, trust, matching, and byte verification to the core Skill Engine.

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

There is no wildcard shell or wildcard network permission. `network.outbound:https` is an explicit authorization-contract declaration for plugins that need external HTTPS APIs; Extension permissions are not a kernel network sandbox. `environment.inherit:<UPPERCASE_NAME>` copies only that exact variable into the otherwise cleaned worker environment. `CONTROL_PLANE_API_KEY`, FolderBridge/control-plane variables, PATH/runtime bootstrap variables, and other reserved names cannot be inherited. Values inherited through variable names containing key/token/secret/password/passwd/auth are treated as secrets and recursively redacted from worker results, worker logs, surfaced stderr, and errors. A dynamic workspace adapter requires `workspace.adapter`; profile state requires `extension.state`.

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
- `state_dir` or `null`
- `workspace_adapter`

The return value must be a strict JSON object. Non-standard numeric constants such as `NaN` and `Infinity` are rejected at manifest, request, response, and worker serialization boundaries. As with built-in tools, a result may contain `_content` with MCP content items; this is how the bundled ComfyUI extension returns images. A result may also contain `workspace_artifacts`, either workspace-relative strings or `{path,label,kind}` objects. FolderBridge re-resolves each declared artifact through workspace path policy and replaces it with trusted relative-path, byte-size, and SHA-256 metadata before exposing the result.

ABI v1 plugins should rely on FolderBridge-packaged modules and the Python standard library. A one-file EXE is not a general pip environment. Integrations that need external software should normally call a fixed local HTTP API or declare `process.execute:<basename>` and invoke that installed program.

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

FolderBridge evaluates these patterns every time the extension registry is queried or an action runs. A project can therefore gain a new build script months after the workspace was added and immediately become applicable. Plugin state belongs in `context.state_dir` by default, outside the repository.

## Authorization and hot loading

`action.authorization` is either `global` or `none`. Each action may additionally declare `run_mode` (`foreground`, the default, or `job`) and an optional `timeout_seconds` override. A Job action returns quickly with a `job_id`, allowing long pipelines to run without holding one foreground MCP request open.

- `global`: the user must approve the exact extension hash/permissions and enable it once in the Extensions sidebar.
- `none`: reserved for read-only actions of extensions bundled with FolderBridge, such as the ComfyUI `status` probe. External plugin code is never executed without hash approval.

The registry is scanned on demand. Adding, editing, approving, disabling, or removing plugins does not require a new MCP tool registration. The sidebar's **重新扫描** button only refreshes the GUI view; MCP `extension(list/info/run)` also sees the current filesystem/trust state on its next call.

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
- `render` accepts only `.pptx`, `.docx`, and `.xlsx`. PowerPoint uses native `Slide.Export`; Word/Excel use their native fixed-format engines and the Windows `Windows.Data.Pdf` renderer to produce PNG pages. It may also create a sibling ZIP and returns hashes/sizes for the source and every output.

The Office extension declares `process.execute:powershell.exe`, but the command surface is fixed: the plugin always invokes the bundled `office.ps1` with `shell=False`. There is no user-supplied command, script path, URL, or executable parameter. Workspace paths are relative-only, links/reparse points and dependency/VCS/build directories are rejected, macro-enabled Office formats are not accepted, Office automation security is forced to disable macros before opening, documents are opened read-only, and Excel link updates are disabled. Intermediate PDFs live under `context.state_dir` and are deleted after rendering.

Use the returned PNG paths with FolderBridge `image_open`; for audit workflows this keeps OOXML/Office originals as structural evidence and native page images as visual evidence instead of treating flattened text as a substitute for layout.

## Bundled Git Publisher extension

`extensions/git-publisher` adds a narrow GitHub publication workflow without exposing generic Git command execution. Its read-only `status` action can inspect the selected repository before approval; browser authorization and mutations require one-time global approval.

- `connect` invokes Git Credential Manager's GitHub `login --web` flow. GitHub opens in the user's browser and the resulting credential is stored by GCM in Windows Credential Manager. The action schema intentionally has no token/PAT/password parameter.
- `commit` accepts an explicit file allowlist plus a commit message. It rejects pre-existing staged content, paths outside the workspace, directories/deletions, credential/key-like files, generated/dependency/VCS directories, and files with content-transforming Git attributes; it verifies the staged set exactly before committing and disables Git hooks/signing.
- `push` accepts no ref/remote/URL argument. It reuses the current named branch and existing `origin`, requires a credential-free GitHub HTTPS origin, forces GCM as the credential helper, disables interactive prompts and pre-push hooks, and never force-pushes.

The Git Publisher permission declaration is deliberately explicit: `github.web-auth`, `git.commit-selected-files`, `git.push-current-branch`, and `process.execute:git.exe`. PAT authentication remains an out-of-band fallback through user-controlled Git/GCM configuration; FolderBridge does not ask the model to receive a secret token.

## Bundled ComfyUI example

`extensions/comfyui` is the reference implementation. Its manifest declares `network.loopback:127.0.0.1:8188`, workspace read/write permissions, a no-authorization read-only `status` action, and a globally authorized `run` action. The helper disables proxies and redirects, accepts only API-format workflow JSON from an allowed workspace, and returns generated PNG/JPEG/GIF/WebP images as MCP content.

### Launcher-owned managed service

ComfyUI process startup is deliberately **not** an Extension worker permission and is not exposed through the MCP `extension` action surface. FolderBridge 0.4.1 registers a Launcher-owned managed-service controller for the `comfyui` extension. This keeps the LLM/worker boundary separate from local process ownership.

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
