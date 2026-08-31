# FolderBridge Godot AI adapter

This external Extension connects FolderBridge to the local Godot AI MCP server at
`http://127.0.0.1:8000/mcp`. It is hot-loaded by FolderBridge; no core source edits
or MCP tool re-registration are required.

The public interface is deliberately narrower than Godot AI's full MCP catalog. It
offers bounded actions for status, editor/scene/node inspection, logs, screenshots,
scene opening/saving, basic node mutation, project run/stop, runtime tree inspection,
and input actions. Each call selects the Godot editor session whose project path
matches the selected FolderBridge workspace.

## Install

Copy this complete `godot-ai` directory to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\godot-ai\
```

The target directory must contain `folderbridge-extension.json` and `plugin.py`
directly at its first level. Open FolderBridge **Extensions & Skills**, rescan,
approve the exact directory hash and permissions, then enable the Extension. A
restart is unnecessary. Any later change to a hash-covered file requires approval
of the new hash.

## Prerequisites

1. Install and enable Godot AI in the target Godot project.
2. Open the project's `project.godot` in Godot.
3. Wait for the Godot AI dock to report Connected.
4. Add the project root, or a workspace containing `Godot/project.godot`, to FolderBridge.

## Web and desktop paths

- Codex desktop can connect directly to the local Godot AI MCP endpoint.
- ChatGPT web calls FolderBridge through Secure MCP Tunnel, then invokes this
  Extension through FolderBridge's stable `extension` gateway.

The adapter never accepts a caller-supplied URL, downstream MCP tool name, process,
or shell command.
