# Changelog

All notable changes to FolderBridge MCP are documented here.

## 0.4.2 — 2026-08-23

- Made the main Windows launcher page vertically scrollable so high-DPI / low-height displays no longer make lower controls unreachable when the scaled content is taller than the available viewport.
- Kept per-monitor DPI recalculation while refreshing the scroll region after metric changes, so moving between displays does not strand content outside the usable page.
- Made first-run ComfyUI managed-service state explicit: when no install root has been configured, the launcher now reports that auto-start is waiting for configuration, opens the Extensions sidebar, logs the required action, and then prompts for a supported Portable or source root instead of appearing to fail silently.
- Improved ComfyUI startup diagnostics and tolerance: perform a bounded two-pass Launcher-start reconciliation instead of relying on a single 300 ms extension-state snapshot, show an in-progress service state, wait up to 120 seconds for heavy custom-node/CUDA initialization, launch with `--disable-auto-launch`, persist combined startup output to `launcher-comfyui.log`, and include that log path in early-exit/timeout errors instead of discarding stdout/stderr.
- Added a connection-guide appendix for optional toolchains: the standalone `FolderBridge.exe` needs neither Python nor Node.js; Python 3.11 x64 is recommended for source/development/repackaging, while Node.js LTS is only needed for Node/npm workspaces whose test/build commands require it.
- Clarified that global capabilities authorize bounded execution but do not install project runtimes, compilers, package managers, or dependencies, and corrected the guide so Local ComfyUI is documented under Extensions rather than as a global capability.
- Clarified single-file Windows delivery in both READMEs: FolderBridge itself is one EXE with its Python runtime bundled, while ChatGPT web usage still requires OpenAI's separately distributed `tunnel-client.exe`.
- Added 0.4.2 GUI/setup-guide regression coverage for scrollable high-DPI reachability, explicit ComfyUI first-run state, and optional Python/Node dependency guidance.

## 0.4.1 — 2026-08-23

- Fixed `--allow-tasks` startup semantics so workspaces without `.folderbridge.json`, approved-task workspaces, extension-only workspaces, and workspaces with unapproved configs can coexist; approval is enforced only when a named task is actually executed.
- Added Launcher-owned ComfyUI managed-service support: select a validated Portable / `.venv` / `venv` install once, optionally auto-start it on extension load, and reuse an already-running external `127.0.0.1:8188` instance without starting a duplicate.
- Hardened ComfyUI process ownership: only the `Popen` handle created by the current FolderBridge run is stoppable; no PID is persisted, no BAT/CMD or arbitrary shell command is launched, and FolderBridge never discovers/kills a process merely because it owns port 8188.
- Reworked application shutdown into a background orchestration that stops FolderBridge-owned managed services in loaded-extension order, waits for their expected ports, then stops Tunnel/MCP, clears the in-memory Runtime API key, and destroys Tk only on the main thread. External services remain untouched.
- Improved Windows Per-Monitor DPI V2 handling with a 400 ms fallback DPI poll, absolute `dpi / 96` metric recalculation, and refreshes for fixed Treeview/sidebar/canvas/status/log/button metrics when moving across differently scaled displays.
- Added a compact button style used only by the global-capability Select all / Clear controls.
- Added formal regression coverage for mixed `allow_tasks` workspaces, ComfyUI managed-service ownership/safety, shutdown ordering, and 96 → 144 → 96 DPI round trips.

## 0.4.0 — 2026-08-23

- Added FolderBridge Extension ABI v1 with hot scanning, exact directory-hash approval, declared permission contracts, bounded out-of-process execution, per-extension state directories, and dynamic workspace adapters that re-detect project features at call time instead of injecting workspace tasks.
- Replaced per-plugin MCP tool growth with one stable `extension` gateway (`list` / `info` / `run`), so installing future extensions does not change the Connector tool catalog.
- Migrated local ComfyUI into the first bundled extension while keeping the loopback-only API helper and generated-image MCP content path; the old 0.3.x `comfyui` global capability value is migrated out without resetting other launcher settings.
- Added a default-collapsed Extensions sidebar with hot rescan, exact-hash permission approval, enable/disable state, stale-approval detection, details, and direct access to the user extension directory.
- Added global capability “select all” and “clear” controls.
- Added an explicit Exit button and hardened shutdown so FolderBridge terminates its owned Tunnel/MCP process tree before the GUI exits; independently started local software such as ComfyUI is left running.
- Added an Extension ABI appendix to the connection guide, including one-click copy of the standard format and an LLM-ready plugin-development instruction that requires the LLM to request/upload missing API docs, scripts, workflows, sample files, or project structure instead of guessing.
- Added a bundled-extension build smoke test so Windows packaging fails if the new EXE cannot discover the bundled ComfyUI extension.

## 0.3.0 — 2026-08-23

- Promoted binary metadata, PPTX/SmartArt inspection, image opening, and local ComfyUI integration to built-in MCP tools that do not depend on per-workspace task configuration.
- Added persistent global pre-authorizations for test, build, Windows EXE packaging, Android APK packaging, constrained GitHub push, and ComfyUI workflow execution; current and future workspaces inherit the selected capabilities.
- Discover supported project entry points at call time so capabilities can become available after a workspace was originally added.
- Restricted global GitHub push to a GitHub HTTPS origin and the current branch, without force push or repository-local credential helpers / push-target rewrites.
- Restricted ComfyUI to loopback `127.0.0.1:8188`, disabled proxies and redirects, required API-format workflow JSON from an allowed workspace, and returned generated images as MCP image content.
- Preserved per-workspace exact-hash-approved named tasks for unusual custom commands rather than using them for common built-in/global capabilities.
- Fixed approved Windows task environments so `Path.home()` and PyInstaller work under the minimal environment, including a build-script recovery path for older packaged runners.

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
