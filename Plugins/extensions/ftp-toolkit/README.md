# FTP Toolkit 0.2.0

Generic external FolderBridge Extension for workspace-confined FTP / explicit-FTPS transfers.

This Extension is intentionally **not** tied to InfinityFree, Debate-Coach, a particular repository layout, or a fixed remote directory. InfinityFree deployment is one workflow that can be composed from these generic actions.

## Install

Copy the whole directory:

```text
Plugins/extensions/ftp-toolkit
```

to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\ftp-toolkit
```

Then approve the exact Extension hash + permissions and enable it in **Extensions & Skills**.

Any later file change makes the approval stale, as required by the FolderBridge Extension trust model.

## Profiles and credentials

`configure` opens a local Windows dialog. The model never receives FTP username or password.

A profile contains:

- FTP host
- port
- connection mode: explicit FTPS or plain FTP
- remote root
- username
- password
- optional TLS certificate-verification override
- optional local HTTP CONNECT proxy host + port (for example a Clash/Mihomo mixed-port)

The complete profile is stored as a Windows Generic Credential under a `FolderBridge/ftp-toolkit/...` target. Nothing is written into the selected workspace.

Profiles are reusable across workspaces. For example:

```text
profile: infinityfree
remote root: /htdocs
```

or:

```text
profile: another-site
remote root: /public_html
```

All action `remote_path` / `remote_dir` values are **relative to the configured remote root**, so a configured `/htdocs` profile cannot be redirected to an unrelated server path by a later tool call.

## Actions

- `status` — local tool availability and optional profile status; never returns credentials.
- `configure` — open the local profile/credential dialog.
- `forget` — remove one saved profile from Windows Credential Manager.
- `check` — authenticate and list the configured remote root.
- `list` — bounded one-directory FTP listing.
- `stat` — query one remote file's existence/size metadata when the server exposes it.
- `mkdir` — create one remote directory under the configured root.
- `rename` — FTP RNFR/RNTO within the configured root.
- `delete` — delete exactly one named remote file under the configured root; no wildcard or recursive directory deletion.
- `upload` — upload one workspace-relative regular file; optional `none`, `size`, or `sha256` verification. Missing parent directories are created level-by-level with idempotent FTP `MKD` commands before upload.
- `upload-tree` — recursively upload regular files from one workspace-relative directory while preserving relative structure; bounded by `max_files`. Deep parent directories are created automatically level-by-level.
- `download` — download one remote file into one workspace-relative destination and return it as a FolderBridge-validated workspace artifact.

Long transfers use FolderBridge host-owned Jobs and can be polled/cancelled through the stable Extension gateway.

## Local path safety

The Extension accepts workspace-relative POSIX paths only. It rejects:

- absolute paths
- `..` traversal
- links/reparse points
- VCS/dependency locations such as `.git` and `node_modules`
- common secret/key file names and extensions

`upload-tree` skips common sensitive/dependency names and rejects links/reparse points instead of following them.

## Network/process boundary

FolderBridge ABI v1 currently has no dedicated `network.outbound:ftp` permission. This Extension therefore declares fixed executable permissions:

```text
process.execute:curl.exe
process.execute:powershell.exe
```

`powershell.exe` is used only for the bundled local credential dialog. `curl.exe` performs FTP/FTPS operations with `shell=False`.

When a profile enables the optional local proxy, curl uses an HTTP proxy plus CONNECT tunneling. This preserves native FTP/FTPS commands while handing destination connection and DNS handling to the local proxy listener. A Clash/Mihomo `mixed-port` can therefore be used by entering only its local host (normally `127.0.0.1`) and port, without changing the FTP server profile or passing a proxy URL through MCP.

The action schema exposes no raw URL, executable, command, curl argument, token, password, or arbitrary shell parameter.

Credentials are supplied to curl through its stdin config (`--config -`), not embedded in the FTP URL or command line. Error text is redacted before returning to FolderBridge.

## Verification

For uploads:

- `none`: upload only.
- `size`: compare remote size when the FTP server exposes size metadata.
- `sha256`: compare size, download the uploaded remote bytes, and calculate SHA-256 locally.

`sha256` is strongest but costs one extra download of the remote file.

## InfinityFree example

InfinityFree is now an orchestration use case, not plugin code.

Create a profile such as `infinityfree` with remote root `/htdocs`. A versioned static release can then be published with generic operations:

1. `upload` immutable chunk files.
2. `upload` the version manifest.
3. `upload`/verify the stable loader when needed.
4. `upload` the new pointer to a temporary remote name.
5. `rename` the temporary pointer to the live pointer last.

That ordering belongs to the Debate-Coach release workflow. FTP Toolkit itself remains reusable for unrelated FTP projects.

## Deliberate exclusions

Version 0.2.0 does not expose:

- wildcard or recursive remote delete
- directory removal
- arbitrary FTP quote commands
- arbitrary URLs
- raw curl arguments
- local absolute paths
- SFTP/SSH
- automatic mirror deletion/synchronization

These exclusions keep the interface useful for normal publishing without turning the Extension into an unrestricted remote shell/file manager.
