# Download Toolkit 0.1.0

A public external FolderBridge Extension for **workspace-confined public HTTPS downloads**.

## Actions

- `download` — stream one public HTTPS resource to an exact workspace-relative file. The action is a host-owned Job, defaults to a 256 MiB limit, supports a caller-supplied download SHA-256, defaults to no-clobber, requires the current destination SHA-256 for overwrite and rechecks it immediately before publish, writes to a same-directory temporary file, fsyncs, publishes atomically, and never echoes URL query strings in results.
- `github-snapshot` — download one GitHub source ZIP from `codeload.github.com`, reject unsafe ZIP members, strip the GitHub top-level archive directory, and publish the source tree without `.git`, hooks, filters, submodules, dependency installation, or repository-code execution.

## Network boundary

The plugin accepts HTTPS only, port 443 only. It rejects URL userinfo, fragments, localhost/private/link-local/reserved destinations, validates every redirect again, and preserves TLS SNI/certificate verification for the original hostname.

Clash Verge is a first-class compatibility target:

- **TUN / Fake-IP** — direct connections remain supported. The RFC 2544 `198.18.0.0/15` Fake-IP range is still rejected for generic downloads; only the fixed `github-snapshot` target `codeload.github.com` may use that range, so TUN compatibility does not turn into a generic SSRF bypass.
- **Windows System Proxy** — when WinINET System Proxy is enabled and its HTTP/HTTPS proxy endpoint is loopback-only (`127.0.0.0/8`, `::1`, or `localhost`), downloads automatically use an HTTP CONNECT tunnel through that local proxy. Clash/Mihomo mixed-port configurations such as `127.0.0.1:7897` are supported.
- Remote proxies, proxy credentials, SOCKS-only proxy settings, and non-loopback proxy endpoints are not auto-trusted. If no approved local System Proxy is active, the downloader uses the validated direct/TUN path.

This is still an approved Extension running with the current OS user's permissions; `network.outbound:https` is an authorization contract rather than a kernel network sandbox.

## Workspace boundary

All destinations are relative to the selected FolderBridge workspace. Traversal, absolute paths, link/reparse destinations, VCS/dependency/credential-like path segments, and accidental overwrite are rejected. The host additionally validates declared exact/tree mutation scopes before the worker starts.

## Why this is separate from FTP Toolkit and Git Publisher

- FTP Toolkit owns FTP/FTPS transfer/profile semantics.
- Git Publisher owns commit/push/release publication of an existing local Git repository.
- Download Toolkit owns inbound public HTTPS acquisition.

Keeping them separate prevents action/permission overlap and avoids turning Git Publisher into an arbitrary network/process surface.
