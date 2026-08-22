# Changelog

All notable changes to FolderBridge MCP are documented here.

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
