# Changelog

All notable changes to FolderBridge MCP are documented here.

## 0.2.0 — 2026-08-23

- Added an add/remove workspace list in the Windows launcher, with up to eight independent roots per connection.
- Added stable `workspace_id` selection to MCP tools; multi-workspace calls cannot silently fall back to a different folder.
- Kept single-workspace tool calls and commands compatible while automatically migrating version 1 launcher settings.
- Reject duplicate and parent/child-overlapping roots before startup, and keep the global access mode read-only by default.
- Added repeated `--workspace` support to `serve` and `client-config` commands.

## 0.1.7 — 2026-08-22

- Made every setup-guide instruction selectable and copyable, including filenames and configuration values.
- Placed each warning directly beneath the setup step that it qualifies instead of collecting warnings at the bottom of a page.
- Kept the connection-guide button available while the Tunnel is starting or running.
- Added the post-setup ChatGPT conversation flow and a copyable example invocation prompt.

## 0.1.6 — 2026-08-22

- Fixed “configuration failed” when applying settings after the `folderbridge` Tunnel profile already exists.
- Re-applying the launcher form now intentionally updates its validated, launcher-managed profile instead of treating it as a first-time-only creation.

## 0.1.5 — 2026-08-22

- Fixed the packaged one-file server failing to start through `tunnel-client` under PyInstaller 6.22.1+ parent-process validation.
- Launch the stdio server as an independent PyInstaller instance using the documented public environment reset switch.
- Clarified that Secure MCP Tunnel needs outbound HTTPS only and never requires RDP, router port forwarding, DMZ, or inbound port 3389.

## 0.1.4 — 2026-08-22

- Fixed Windows Tunnel profile creation when the FolderBridge executable or workspace path contains spaces: generated `--mcp-command` paths now survive the official client's POSIX-style parsing.
- Reject `tunnel-client-runtime-*` internal components instead of treating them as the full client.
- Added prominent in-app and bilingual README warnings to download the complete Windows amd64 archive and select only `tunnel-client.exe`.

## 0.1.3 — 2026-08-22

- Added a fifth setup-guide page for non-ChatGPT MCP clients.
- Added one-click JSON, TOML, and complete stdio command copying from the GUI.
- Documented the compatibility requirements and routes for local stdio, URL-only, web, mobile, and cloud clients.
- Clarified that direct stdio clients start and stop FolderBridge themselves and need no Tunnel credentials or running GUI.
- Centralized generated client configurations so CLI and GUI preserve identical command/argument boundaries.

## 0.1.2 — 2026-08-22

- Replaced the short web setup popup with a DPI-aware four-step connection guide.
- Added exact Windows x64 official `tunnel-client` download, checksum, extraction, and executable-selection guidance.
- Documented every current Platform Tunnel field and the least-privilege Tunnel permissions.
- Added a prominent ChatGPT configuration warning: choose Tunnel plus No authentication, not OAuth or Server URL.
- Added a safe per-user extraction folder shortcut without silently downloading or running network binaries.

## 0.1.1 — 2026-08-22

- Made the launcher follow Windows display scaling instead of forcing a fixed Tk scale.
- Added Per-Monitor V2 DPI awareness so fonts refresh when the window moves between monitors with different Scale settings.
- Scaled the initial window and high-DPI UI measurements while keeping the window within the visible screen.

## 0.1.0 — 2026-08-22

Initial public release.

- Added a bounded, stdio-only MCP workspace server with read, search, Git review, and conflict-safe exact editing.
- Added locally reviewed, exact-hash-approved named tasks as an optional capability.
- Added a Chinese desktop launcher for workspace permissions, OpenAI Secure MCP Tunnel setup, diagnostics, process monitoring, and redacted logs.
- Added English and Simplified Chinese documentation, an Apache-2.0 license, and a public security model.
- Added a standalone Windows x64 `FolderBridge.exe`; the same binary opens the GUI when double-clicked and retains stdio for its `serve` subcommand.

The Windows community build is not code-signed and does not bundle OpenAI's `tunnel-client`. Verify the accompanying SHA-256 file or build from source if you do not trust unsigned binaries.
