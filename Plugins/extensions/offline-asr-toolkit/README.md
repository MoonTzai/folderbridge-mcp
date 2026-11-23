# Offline ASR Toolkit — FolderBridge external extension

Version 0.1.0. General-purpose, **offline-first** transcription from a selected FolderBridge workspace. This plugin is not tied to any particular course, repository, audio manifest, language, or model installation.

## Interface

The stable FolderBridge `extension` gateway discovers these actions after exact-hash approval:

- `status`: optionally verify local model/runtime file SHA-256. No ASR execution.
- `list-profiles`: list configured local provider profiles.
- `transcribe-one`: host-owned Job to transcribe one workspace-relative audio file, placing all intentional outputs under a caller-selected workspace-relative output directory. Receipts contain the true input/model/runtime/script SHA, worker PID, exit code, timings, engine version, log, text and output SHA.

`transcribe-one` takes `input_path`, `output_dir`, `profile_id`, plus optional `language`, `device` and `max_audio_bytes`. It does **not** accept a shell command, script path, URL, or arbitrary executable. A recorded transcript is not a human listening receipt or semantic-audit PASS.

## Provider profiles

Local model configuration goes in `profiles.json` alongside the installed extension. The generic source tree intentionally does **not** contain a user-specific profile or the model files themselves. Example structure:

```json
{
  "schema": "offline-asr-profiles-v1",
  "profiles": [
    {
      "id": "example-zh",
      "engine": "funasr",
      "language": "zh",
      "python_exe": "C:\\path\\to\\python.exe",
      "python_sha256": "<SHA256-of-the-interpreter>",
      "model": {
        "directory": "C:\\path\\to\\paraformer",
        "files": {
          "model.pt": "<SHA256>",
          "config.yaml": "<SHA256>"
        }
      }
    }
  ]
}
```

All referenced model files and Python binary must be present locally and SHA-pinned. `funasr` accepts optional `vad` and `punctuation` model entries with the same directory/files shape. The optional `faster-whisper` engine expects a pinned local Whisper model directory. Model/runtime code and language combinations must be validated on the actual machine before production use.

For the same interpreter and model collection, this profile can be used with different workspaces by changing only `workspace_id`, `input_path` and `output_dir`. Changing the profile data changes the extension hash and requires user reapproval.

## Install / hot update

Recommended: execute the provided `install.ps1` locally (it stages a new extension directory, can preserve an existing local profile, and requires **separate GUI approval**). For a first installation with a model profile:

```powershell
& '.\Plugins\extensions\offline-asr-toolkit\install.ps1' -ProfilePath '.\local-private\offline-asr-toolkit\profiles.json'
```

On a different machine, supply its own `profiles.json`; the plugin code remains the same. The extension installs beneath `%LOCALAPPDATA%\folderbridge-mcp\extensions\offline-asr-toolkit\`. After that, open FolderBridge **Extensions & Skills**, rescan and **approve the exact current directory hash and declared permissions** before enabling. Subsequent edits to any hash-covered file automatically stale the old approval; approve again. No FolderBridge core rebuild/restart or new MCP tools are necessary.

Declared permissions: `workspace.read`, `workspace.write`, `process.execute:python.exe`. The actual Python binary is selected **only** through a trusted hash-covered local profile and is rehashed before execution.

## Security and limitations

- Offline: no action performs model downloading. The child ASR interpreter sets offline environment flags, redirects common cache/temp folders under the output Job, and rejects normal Python socket connections. No HTTPS outbound permission is requested.
- Input: regular, non-linked selected-workspace audio files with path/size/extension checks; output: a dedicated unique run subtree under the selected workspace; mutations are leased against the caller's `output_dir`.
- Source audio and pinned model binaries are rehashed before/after the model call. External libraries still run with the user's OS rights; **FolderBridge permissions are not an OS-enforced sandbox**. Proving *no writes anywhere outside the output root* requires OS filesystem restrictions or further independent verification.
- Supported engine code paths: local FunASR and local faster-whisper; the latter and individual codec/language combinations remain **unverified until real smoke testing**.
- Present version uses one-file Jobs to preserve transparent receipts. A bounded batch/multi-file action can be added later after the first verified live Job, without changing FolderBridge's MCP tool catalog.
- User must explicitly install, approve and enable the plugin. Simply checking the source does not count as a live hot-load or ASR success.
