# FolderBridge Extension ABI v1

FolderBridge 0.4.1 uses one stable MCP gateway, `extension`, for local integrations. Installing an extension changes the extension registry, not the MCP tool catalog. Clients can call `extension(action="list")`, inspect the returned action schemas, then call `extension(action="run", extension_id=..., extension_action=..., params=...)`.

## Directory layout

```text
<extension-id>/
  folderbridge-extension.json
  plugin.py
  ... optional sibling modules/data ...
```

User extensions live under the per-user FolderBridge configuration directory, normally `%LOCALAPPDATA%\folderbridge-mcp\extensions` on Windows. The Windows launcher opens this directory from the default-collapsed **Extensions** sidebar.

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
- `git.push-current-branch`
- `network.loopback:127.0.0.1:<port>` or `network.loopback:localhost:<port>`
- `process.execute:<basename>`

There is no wildcard shell or wildcard network permission. A dynamic workspace adapter requires `workspace.adapter`; profile state requires `extension.state`.

The permission list is part of the approval identity. External extensions are approved against the SHA-256 of the complete extension tree plus the exact permissions. If any file or permission changes, the old approval becomes stale and execution is blocked until the user approves the new hash.

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

The return value must be a JSON object. As with built-in tools, a result may contain `_content` with MCP content items; this is how the bundled ComfyUI extension returns images.

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

`action.authorization` is either `global` or `none`.

- `global`: the user must approve the exact extension hash/permissions and enable it once in the Extensions sidebar.
- `none`: reserved for read-only actions of extensions bundled with FolderBridge, such as the ComfyUI `status` probe. External plugin code is never executed without hash approval.

The registry is scanned on demand. Adding, editing, approving, disabling, or removing plugins does not require a new MCP tool registration. The sidebar's **重新扫描** button only refreshes the GUI view; MCP `extension(list/info/run)` also sees the current filesystem/trust state on its next call.

## Execution boundary

Plugins run in a separate FolderBridge worker process with:

- cleaned environment variables;
- no Runtime API key passed to plugin code;
- fixed working directory;
- manifest timeout up to 600 seconds;
- bounded request, response, stdout, and stderr;
- PyInstaller environment reset when running from the one-file Windows EXE.

This boundary prevents plugin prints/crashes from corrupting MCP stdio and limits accidental runaway output. It does not provide kernel-level filesystem/network isolation.

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
