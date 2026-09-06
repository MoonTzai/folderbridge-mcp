# File Ops Toolkit

External FolderBridge Extension for safe **regular-file copy and move** operations inside one selected workspace.

## Actions

### `copy-file`

Copies one regular file to another workspace-relative path.

Safety contract:

- source and destination are both declared as exact mutation claims, so FolderBridge serializes conflicting workspace mutations while the worker is alive;
- binary streaming, no UTF-8 conversion;
- default `overwrite=false`;
- `overwrite=true` requires `expected_destination_sha256`;
- optional `expected_source_sha256` pins the intended source bytes and enables safe retry recognition when the destination already contains those exact bytes;
- same-directory temporary file + fsync + no-clobber publish / atomic replace;
- symlink and reparse-point traversal is rejected;
- FolderBridge ignored/generated/VCS and credential-like paths remain blocked;
- source changes detected during the copy fail before publish.

### `move-file`

Moves one regular file to another workspace-relative path.

Safety contract:

- exact mutation claims cover both source and destination;
- default no-clobber;
- overwrite requires `expected_destination_sha256`;
- optional `expected_source_sha256` pins the source and lets a retry recognize an already-completed move if the source is gone and the destination has the expected hash;
- same-filesystem atomic rename/replace is used on Windows;
- POSIX no-clobber move uses hard-link + source unlink with rollback on source-unlink failure;
- cross-filesystem moves fail closed instead of silently degrading to a partial copy/delete sequence.

## Parameters

Both actions accept:

- `source_path` — required workspace-relative regular-file path.
- `destination_path` — required workspace-relative regular-file path.
- `expected_source_sha256` — optional 64-hex SHA-256 of the source.
- `overwrite` — default `false`.
- `expected_destination_sha256` — required when `overwrite=true`.
- `create_parents` — default `false`; set `true` to create missing destination parent directories.
- `max_bytes` — default 8 GiB, hard maximum 64 GiB.

Only same-workspace regular-file operations are supported in v0.1.0. Directory trees and cross-workspace copy/move are deliberately outside this plugin's contract.

## Install

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Plugins\extensions\file-ops-toolkit\install.ps1
```

Then in FolderBridge:

1. open **Extensions & Skills**;
2. click **Rescan**;
3. approve the exact `file-ops-toolkit` hash and its `workspace.read` / `workspace.write` permissions;
4. enable it.

External Extension approval is exact-hash bound. Any source change requires reinstallation/rescan/reapproval.
