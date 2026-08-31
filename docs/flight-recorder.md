# FolderBridge Flight Recorder

FolderBridge 0.8.18 includes a local, built-in reliability Flight Recorder for diagnosing MCP/Tunnel disconnects such as 502, TaskGroup, EOF, BrokenPipe, timeout, or unexpected Tunnel exit incidents.

## Retention and overhead

- Time window: latest 15 minutes.
- Total storage cap: 20 MiB.
- Diagnostic text cap: 16 KiB per event.
- Storage: `<FolderBridge user config root>/flight-recorder/`; on a normal Windows install this is `%LOCALAPPDATA%\folderbridge-mcp\flight-recorder`.
- Writers use independent `role-pid-UTCminute.jsonl` shards. Launcher and MCP never contend on a cross-process recorder lock.
- Normal recording is append-only and best-effort. Cleanup is scheduled asynchronously. Recorder I/O failures are swallowed and counted; they must never become an MCP/Tunnel failure source.

## What is recorded

The MCP process records compact metadata such as request ID, method, tool/action, workspace ID, lane, request/response byte counts, duration, Server busy, parse error, EOF/shutdown, dispatch exceptions, and stdout write failures.

The Launcher records Tunnel process lifecycle and only warning/error-classified `tunnel-client` output. Normal Tunnel chatter is not persisted.

## What is deliberately not recorded

The recorder does not persist full MCP request or response bodies. It does not record workspace file content, file paths supplied as tool arguments, search queries, write chunks, API request bodies, or credentials. Credential-like diagnostic text and keyed fields are redacted before persistence.

The recorder can observe the local FolderBridge MCP process and diagnostics surfaced by the local `tunnel-client`. It cannot observe private internals of the remote Tunnel service that were never surfaced locally.

## Read-only MCP interface

The built-in `flight_recorder` tool runs on the control lane and supports:

- `status`: storage health and retention configuration.
- `recent`: a bounded merged timeline for the latest 1–15 minutes.
- `errors`: warning/error events only.

Both `recent` and `errors` accept a result limit of 1–200 and an optional role filter: `all`, `mcp`, or `launcher`.

## How to interpret a disconnect

After reconnecting, inspect `flight_recorder(errors)` first, then a small `flight_recorder(recent)` window around the incident.

- No matching `mcp.request`: the failed operation did not reach the local MCP server; investigate the client/Tunnel path before local dispatch.
- `mcp.request` without `mcp.complete`, followed by EOF/shutdown or Tunnel diagnostics: the local operation was interrupted before a normal MCP completion boundary.
- `mcp.write_error`: FolderBridge produced a response but the local stdout pipe was no longer writable; inspect nearby Tunnel exit/TaskGroup/BrokenPipe events.
- `mcp.complete` with response bytes and no local error, while the client still saw 502: the local MCP request completed and wrote successfully, so the failure is more likely downstream of the local MCP write boundary.
- `mcp.busy`: the bounded lane was saturated; this should fail one request with Server busy rather than crash the MCP process.
- `tunnel.output` containing TaskGroup/ExceptionGroup/502/BrokenPipe/connection reset or `tunnel.unexpected_exit`: correlate the timestamp with the nearest MCP request/complete pair.

Use the recorder as evidence, not as an automatic root-cause verdict: a remote service failure may leave only the locally visible boundary symptoms.
