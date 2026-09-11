# Local ComfyUI — external FolderBridge Extension

This Extension connects FolderBridge to a local ComfyUI server at `127.0.0.1:8188` without being bundled into `FolderBridge.exe`.

## Install / hot-load

From PowerShell, the repository copy can install/update itself with a staged directory cutover:

```powershell
.\install.ps1
```

If another PowerShell script invokes this installer with `&`, treat a thrown exception (or `$? -eq $false`) as failure. Do **not** inspect `$LASTEXITCODE` after invoking this `.ps1`: `$LASTEXITCODE` is a native-process status variable and can retain an unrelated earlier value even when this installer completed successfully.

By default it installs to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\comfyui\
```

You may instead copy the complete directory manually. The installer refuses to replace a reparse-point target, stages the exact runtime files beside the destination, swaps the directory only after staging succeeds, and restores the previous directory if cutover fails.

Open **Extensions & Skills**, click **重新扫描**, review the exact directory hash and declared permissions, then approve and enable the Extension. FolderBridge hot-scans the external Extension directory, so installing or updating the plugin does not require rebuilding FolderBridge or adding a new MCP tool. Any hash-covered file change makes the previous approval stale and requires a new approval.

## Actions

- `status`: checks local ComfyUI `/system_stats` through loopback only.
- `jobs` / `job-status`: inspect real ComfyUI prompt state and bounded output metadata.
- `progress`: explicit advanced diagnostic for one exact FolderBridge-submitted MiniMax Director prompt/node. It observes a bounded client-specific WebSocket interval and reports unavailable instead of inventing a percentage. It is not invoked implicitly by `health-check`.
- `health-check`: reusable cross-project read-only snapshot for one exact prompt. It aggregates compact job state, output/artifact count, exact queue presence, numeric RAM/VRAM headroom, ComfyUI launch arguments (including `--fast-disk` detection), and advertises supported optional progress capability without opening a WebSocket. A single snapshot never declares a stall.
- `preflight`: validates one API-format workflow without submitting it. It applies bounded overrides, dynamic-combo checks, and the same reusable production-profile gate used by `run`.
- `run`: executes one API-format workflow JSON from the selected FolderBridge workspace as a host-owned Job. It supports bounded overrides, dynamic-combo preflight, prompt-scoped cancellation, bounded artifact metadata, optional image copying into a workspace `save_directory`, and production-profile validation before `/prompt` submission.

### Production profiles

`preflight` and `run` accept `production_profile`. The default is `auto`; `none` is an explicit expert opt-out. `auto` currently recognizes native **MiniMax H3 V2V at 1344×768** and applies the built-in `minimax_h3_v2v_16gb_1344x768` profile before any prompt is submitted.

That profile is the reusable FolderBridge-side form of the production path accepted on 2026-09-11. It deliberately contains **no project-specific frame numbers, seed, prompt text, or output names**. Instead it fail-closes on the portable execution contract:

- one `MiniMaxH3Director` V2V node at 1344×768 / 24 fps with `ref_max_size=1344`;
- production sampling `cfg=1`, `8` steps, `dpmpp_2m` + `simple`, video/audio shifts `12/3`;
- model chain `MiniMaxChunkFeedForward(chunks=2, seq_threshold=4096)` → `MiniMaxLowVRAMAttention(head_chunks=4)` → Director;
- `clear_vram_between_segments=true`;
- live ComfyUI must be running with `--fast-disk`;
- live `MiniMaxH3Director` schema must expose `guard_long_v2v_segments` with default `true`, and a workflow may not explicitly disable it;
- every V2V segment must stay within the 100,000 target+source packed-video-row budget. At 1344×768 this yields a hard recommended maximum of 158 aligned frames; **124f is the conservative baseline**, while the formally accepted 141f production segment also remains inside the same budget. A 260f single segment is rejected before `/prompt`.

Use `preflight` when preparing or auditing a production workflow. `run` repeats the same validation immediately before submission, so passing an earlier preflight cannot become stale authority if the workflow or live Director/runtime changes later.

### Standard long-run check

For long ComfyUI jobs, prefer `health-check(prompt_id)` as the standard generic snapshot instead of manually composing `status` + `job-status` + queue inspection on every project. If the workflow was launched by FolderBridge and a host Job ID is known, also inspect that Job through FolderBridge's host-level `extension job_status`; host worker liveness deliberately remains outside this external plugin so the plugin never imports private FolderBridge internals.

If `health-check` advertises a supported progress provider and capability-specific detail is actually needed, call `progress(prompt_id, node_id)` explicitly. Current MiniMax Director progress is client-targeted; ComfyUI replaces the socket mapping when the same client ID reconnects, so generic health deliberately does not open `/ws` or create concurrent observer takeover risk.

Treat `running_exact_queue_bound` as evidence that ComfyUI's job and current queue agree that the exact prompt is running, not as proof of sampler movement. Escalating to a stall diagnosis requires longitudinal evidence such as repeated unchanged snapshots plus an independent runtime/log/output signal. Numeric RAM/VRAM ratios are observations, not generic safety grades. The health action never frees memory, restarts ComfyUI, cancels prompts, or submits work.

`run` declares no workspace mutation when `save_directory` is omitted. When `save_directory` is supplied, FolderBridge resolves a tree mutation scope before the worker starts, so unrelated workspace writes can continue while overlapping writes are serialized.

## Safety boundary

The runtime uses Python standard library code plus the public `folderbridge_mcp.extension_api.ExtensionError` ABI only. It does not import `folderbridge_mcp.comfyui`, `folderbridge_mcp.security`, or other private FolderBridge internals.

Workspace workflow/save paths reject absolute paths, `..`, symlink/reparse traversal, ignored dependency/VCS directories, credential-like files, and writes to `.folderbridge.json`. ComfyUI network traffic is loopback-only, redirects/proxies are disabled, and cancellation never falls back to the process-global `/interrupt` endpoint.
