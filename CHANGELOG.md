# Changelog

All notable changes to FolderBridge MCP are documented here.

## 0.8.21 — 2026-08-31

- Replaced workspace-wide serialization for truthfully bounded Extension writes with a general scoped mutation coordinator. Extension actions may now declare `mutation_scope` as `none`, opaque `workspace`, or one or more `exact`/`tree` path claims resolved from fixed relative paths or validated top-level string parameters before the worker starts. Disjoint scopes can run concurrently, overlapping scopes remain serialized, writer fairness is preserved, and leases still remain held until the real worker/process exits.
- Preserved old Extension manifests without migration: omitted scope remains `none` for read-only actions and opaque `workspace` for non-read-only actions. A FolderBridge read-only runtime rejects any effective mutation scope before worker spawn, while scoped `edit_file` / transactional commit conflicts still fail within the bounded mutation-admission budget with blocker diagnostics rather than consuming the Tunnel response deadline.
- Added the deliberately small public `folderbridge_mcp.extension_api.ExtensionError` ABI. External Extensions can now preserve structured error `code`, message, and details across the isolated worker boundary without importing private `security.ToolError`; unknown exceptions remain fail-closed as `EXTENSION_WORKER_EXCEPTION`.
- Moved Local ComfyUI to `Plugins/extensions/comfyui` as a standard exact-hash-approved, hot-loadable external Extension. Its standalone standard-library runtime preserves API-workflow validation, dynamic-combo preflight, bounded artifact metadata, path-only behavior, prompt-scoped cancellation, timeout behavior, and workspace path protections without importing the retired core ComfyUI helper. `run` claims only the optional `save_directory` tree; without that parameter it takes no FolderBridge workspace mutation lease.
- Added a staged `install.ps1` for the external ComfyUI plugin plus repository and plugin-local tests. A hash-covered file change immediately makes approval stale; re-approval restores loading in the same running Registry process without a FolderBridge restart. Cancellation never falls back to ComfyUI's process-global `/interrupt` endpoint.
- Removed ComfyUI from the default bundled Extension allowlist and Windows packaging inputs/hidden imports. The package verifier now requires the bundled set to be only Git Publisher, Office, and Skill Engine and rejects any bundled ComfyUI self-test result. The optional Launcher-owned ComfyUI service controller remains available but now uses its own bounded loopback `/system_stats` probe instead of importing plugin/runtime code.
- Split the Launcher Extensions sidebar into explicit **Bundled Extensions** and **External Extensions · hot-loadable** sections, with per-card source labels and exact-hash reapproval guidance. Existing approval/revoke, managed-service controls, and Skill Pack handling continue through the same trust/runtime paths rather than a duplicated UI-specific mechanism.
- Updated bundled Git Publisher to 1.3.4 and fixed selective commits of externalized/moved files. Existing files are staged with `git add`, validated tracked deletions use `git update-index --force-remove`, and staged allowlist verification now uses `git diff --cached --no-renames` so Git's rename detection cannot hide the old side of a move and produce a false allowlist mismatch. Missing untracked paths remain rejected while traversal, sensitive-name, link/reparse, ignored-file, pre-staged-change, and explicit allowlist protections remain intact.
- Audited repository hygiene after the ComfyUI externalization: current public docs now consistently describe the 16-workspace limit, external ComfyUI boundary, and Git Publisher 1.3.4 deletion contract; public/bundled/private source roles are explicit; local-only Judge/discipline assets are protected from accidental publication; and repository-hygiene regression tests lock these cross-file contracts against future drift.
- Promoted the existing owned-subprocess helpers to the stable `folderbridge_mcp.extension_api` seam without changing their implementation, and moved the public FFmpeg, FTP, and GPT-SoVITS Extensions to that seam with a narrow ImportError fallback for already-built 0.8.21 hosts. Patch source versions are now FFmpeg Toolkit 0.1.2, FTP Toolkit 0.2.1, and GPT-SoVITS Local 0.1.2; existing action, permission, Job, timeout, process-tree cleanup, and output contracts remain unchanged. Godot and GPT-SoVITS are now covered by the repository-level full suite.

## 0.8.20 — 2026-08-31

- Prevented workspace mutation contention from consuming the Tunnel response deadline. `edit_file`, `write_file(commit)`, non-read-only Extension launches, approved Tasks, and project Task capabilities now use a bounded 2-second workspace mutation wait and return `WORKSPACE_BUSY` with blocker diagnostics instead of silently waiting behind a long exclusive Job for minutes.
- Applied the same bounded-wait rule to per-resource file locks, preserving normal short same-file serialization while preventing a second synchronous file mutation from waiting indefinitely behind an unusually long write.
- Added mutation-lease Flight Recorder diagnostics for wait start/acquire/timeout and exclusive acquire/update/release, including workspace, shared/exclusive/resource mode, wait time, blocking reason, holder/requester action, Job id, worker pid, Extension action, and capability/task. Workspace argument paths remain excluded from persisted Flight Recorder records; path detail is available only in the immediate `WORKSPACE_BUSY` response. Recorder writes are dispatched through a bounded single-worker lane so diagnostic I/O cannot hold the mutation scheduler lock.
- Preserved writer preference and true-process-lifetime lease protection: long Jobs still own their exclusive mutation lease until the worker actually exits; the new behavior changes only how competing synchronous MCP calls wait for admission. Timed-out exclusive waiters now wake blocked shared callers correctly.
- Added regression coverage for the original long-Job-versus-`edit_file` failure, holder metadata, waiter wake-up, non-blocking flight recording, preserved original blocker attribution, and same-file resource-lock fail-fast behavior.
- The external Tunnel client's post-deadline main-channel recovery is intentionally not emulated by restarting Tunnel from FolderBridge: a launcher-side restart would terminate process-local Jobs. FolderBridge 0.8.20 instead removes its known >response-budget lock-wait trigger; automatic main-channel reconstruction remains a Tunnel-client concern.

## 0.8.19 — 2026-08-30

- Added transport-safe adaptive execution for long FolderBridge-owned work. Extension foreground actions and fixed-argv Task/project-capability executions can now keep their original worker/process and promote in place to a host-owned Job after the shared 60-second synchronous response budget, without restarting or repeating paid/long work; business timeouts remain independent and Extension `timeout=0` remains unlimited.
- Added recoverable Job control surfaces: Extension `job_list/job_status/job_cancel` plus Task/Capability `list/status/cancel`. All Job discovery/status/cancel operations run on the MCP control lane so they remain available while data workers are saturated, while actual execution stays on the data lane.
- Hardened process ownership and workspace mutation leases across promotion, cancellation, timeout, and shutdown. Shutdown now wins foreground-to-Job boundary races, promoted Task/Capability Jobs expose `termination_pending` instead of pretending an unkillable process has exited, and mutation leases are released only after the owned process is confirmed dead.
- Centralized the transport response budget in `process_control.py` so Extension and Task/Capability execution cannot drift onto different lifecycle thresholds. Runtime health distinguishes active output from quiet-but-alive work and never auto-cancels a task merely because it is quiet; Extension stall suspicion still requires an explicit stale heartbeat contract.
- Added a launcher button immediately to the right of **Official docs** to export the complete recent 15-minute built-in Flight Recorder window as redacted JSONL. Export stays local, honors the recorder's 20 MiB bound, tolerates malformed individual records, and does not route through the Tunnel being diagnosed.

## 0.8.18 — 2026-08-30

- Added a built-in reliability Flight Recorder for diagnosing MCP/Tunnel disconnects without storing workspace payload bodies. The MCP server records compact request ingress/completion metadata, lane saturation, parse/EOF/shutdown events, dispatch exceptions, response byte counts, and stdout write failures; the Launcher records Tunnel process lifecycle plus only warning/error-classified `tunnel-client` output after existing secret redaction.
- Flight data is local-only under the FolderBridge user configuration root, split into independent per-process/per-minute JSONL shards so Launcher and MCP writers never need a cross-process file lock. The recorder keeps only the latest 15 minutes, enforces a 20 MiB total cap, bounds diagnostic text to 16 KiB, asynchronously prunes old shards, and treats every recorder I/O failure as best-effort telemetry loss rather than an MCP/Tunnel failure.
- Added the read-only control-lane `flight_recorder` tool with `status`, `recent`, and `errors` actions. Results are bounded and expose only compact metadata; file paths, search queries, write chunks, request/response bodies, API keys, Runtime keys, and other credential-like values are not recorded. This lets a post-reconnect diagnosis distinguish failures before local MCP ingress, during local dispatch, at stdout/BrokenPipe, or in diagnostics surfaced by the local `tunnel-client`, while correctly leaving remote Tunnel service internals outside FolderBridge's visibility.

## 0.8.17 — 2026-08-27

- Fixed the Office PDF rasterizer `width` contract on scaled Windows desktops. `Windows.Data.Pdf.PdfPageRenderOptions.DestinationWidth` / `DestinationHeight` are DIPs rather than physical pixels, so Word/Excel output could be enlarged by the system DPI scale (for example, a requested 1600-wide page rendered as 3600 physical pixels at 225% scaling).
- Word and Excel PDF rasterization now resolve the actual system DPI from a system-aware thread context, convert the requested physical pixel dimensions to DIPs before WinRT rendering, then apply a bounded final `System.Drawing` correction only when DIP quantization leaves the bitmap off the exact requested pixel dimensions. PowerPoint `Slide.Export` remains unchanged because its width contract is already physical pixels.
- Strengthened the real Word end-to-end regression: `width=800` must now produce an actual 800-pixel PNG while still proving no render-owned `WINWORD.EXE` survives. Bundled Office Native is now 1.1.4.

## 0.8.16 — 2026-08-27

- Fixed the remaining Word native-render hang by separating DOCX rendering across process boundaries: `word_export.ps1` now performs only Word COM `ExportAsFixedFormat` and exits before the independent `pdf_render.ps1` process initializes `Windows.Data.Pdf`. Real-machine diagnosis proved Word COM, COM cleanup, and WinRT PDF rasterization each completed normally in isolation while the combined lifetime could stall even for a one-line DOCX.
- Added fail-closed ownership for COM-launched `WINWORD.EXE`: the export stage snapshots pre-existing Word PIDs, accepts exactly one newly created Word process, publishes that PID privately, and the plugin verifies/holds a Windows process handle so only the render-owned Word instance can be reaped on completion, cancellation, or timeout. Existing user Word instances are never targeted by process-name-wide termination.
- Removed the obsolete single-process Word path from the shared Office stage script, preserved the existing `P0001.png` page naming contract, and added a real Windows end-to-end regression proving a minimal DOCX renders to PNG without changing the pre/post WINWORD PID set. Bundled Office Native is now 1.1.3.

## 0.8.15 — 2026-08-27

- Fixed the second Word-native rendering bottleneck found by real DOCX regression: the renderer no longer calls `Document.ComputeStatistics(wdStatisticPages)` before `ExportAsFixedFormat`. That call forced a redundant full-document pagination pass before Word immediately paginated again for PDF export and could leave text/table-heavy documents apparently hung before the first PNG was produced.
- Word now performs one native full-document PDF export, then uses the PDF's authoritative page count to validate `page_start`/`page_end` and rasterizes only the requested pages. This preserves Word-native layout while avoiding duplicate pagination work and simplifies source-page numbering.
- Added a Windows regression proving the existing STA `Windows.Data.Pdf` async bridge completes successfully, ruling out the PDF rasterizer as the incident cause, and locked the no-`ComputeStatistics` Word path in tests. Bundled Office Native is now 1.1.2.

## 0.8.14 — 2026-08-27

- Moved bundled Microsoft Office Native `render` from a foreground MCP request to a host-owned Extension Job. Native PowerPoint/Word/Excel rendering can legitimately take much longer than ordinary control-plane calls, so the caller now receives a `job_id` immediately and polls `job_status`; slow or failed Office automation can no longer keep one MCP request open until the client/tunnel tears down with a TaskGroup transport failure.
- Updated bundled Office Native to 1.1.1 and locked the Job-mode contract in regression tests and Windows bundle verification while preserving the existing PowerPoint `Slide.Export` and Word/Excel fixed-format + `Windows.Data.Pdf` rendering backends.
- Updated the Extension documentation to describe the Job lifecycle and completed-result contract for native Office rendering.

## 0.8.13 — 2026-08-26

- Fixed Extension ABI state provisioning so declaring `extension.state` is sufficient for FolderBridge to create and inject `context.state_dir`, even when `workspace_adapter.mode=none` and `workspace_adapter.state=none`; plugins no longer need a fake profile adapter just to obtain private persistent state.
- Preserved the permission boundary and isolation contract: extensions without `extension.state` receive `state_dir=null`, workspace-bound state remains isolated by extension/workspace ID, workspace-less actions use the extension's `global` state directory, and foreground/Job actions share the same context builder.
- Clarified the public Extension specification and LLM authoring guidance, and added regression coverage for permission gating, real directory creation, workspace isolation, global state, profile-permission validation, and Job-mode parity.
- Rebuilt the single-file Windows package and re-ran the packaged smoke test successfully as `folderbridge-mcp 0.8.13` before release.

## 0.8.12 — 2026-08-26

- Added globally authorized `release-sync` as a repository-declared delivery handoff seam. Node-based workspaces may expose a fixed `release:sync` script in `package.json`; FolderBridge discovers it dynamically and executes only `npm run release:sync`, without exposing arbitrary command text or shell arguments through MCP.
- Kept release semantics inside each repository rather than teaching FolderBridge project-specific paths. Development-to-release-repository bridges, staging-folder handoffs, and similar delivery syncs can now reuse one capability while retaining repository-owned validation and safety rules.
- Added regression coverage for canonical capability ordering, dynamic discovery, fixed npm argv, GUI/i18n exposure, and preservation of existing test/build/package/git-push behavior.
- Rebuilt the single-file Windows package and re-ran the packaged smoke test successfully as `folderbridge-mcp 0.8.12` before release.

## 0.8.11 — 2026-08-26

- Added a repository-declared packaging seam for globally authorized `package-windows` and `package-android`: Node-based workspaces may expose fixed `package:windows` / `package:android` scripts in `package.json`, which FolderBridge discovers dynamically and executes only through fixed `npm run <script>` argv.
- Kept existing Windows PowerShell/PyInstaller and Android Gradle/Flutter discovery as compatibility fallbacks, so current projects continue to work while nonstandard repository layouts no longer need FolderBridge-specific directory hardcoding.
- Unified npm-script capability discovery behind one helper shared by `test`, `build`, Windows packaging, and Android packaging, and added regression coverage proving both packaging capabilities are discoverable and that package script bodies are never copied into a shell command.
- Rebuilt the single-file Windows package and re-ran the packaged smoke test successfully as `folderbridge-mcp 0.8.11` before release.

## 0.8.10 — 2026-08-25

- Updated bundled Git Publisher to 1.3.1 with a generic `release-assets` action while preserving the existing parameterless FolderBridge-only `release` compatibility path. The generic action publishes only explicit regular files from the selected GitHub HTTPS workspace repository, uses GitHub-stable ASCII download names with optional user-facing display labels, requires the current branch to be fully pushed and tracked content clean, rejects tag movement/force push and credential-like paths, snapshots and SHA-256 verifies assets before remote mutation, verifies Release asset name/size/label plus Latest/tag postconditions, and runs long uploads as a host-owned two-hour Job.
- Updated the Windows bundle verifier and public Extension documentation to require/report Git Publisher 1.3.1 and its generic Release/display-label contract.
- Added bounded ComfyUI API-workflow preflight for supported dynamic inputs. The incident case `SaveVideo.codec={"codec":"auto"}` is now rejected before `/prompt`, while the correct API form `codec="auto"` remains accepted and submitted.
- Completed host-owned ComfyUI job cancellation: FolderBridge passes a private cancellation token into the isolated worker, the bundled plugin watches it independently of history polling, and cancellation/timeout targets only the submitted ComfyUI `prompt_id` through `/api/jobs/{id}/cancel` before any bounded worker force-kill fallback.
- Removed the legacy `/interrupt` cancellation fallback because it cannot guarantee prompt-scoped cancellation and could interrupt an unrelated ComfyUI GUI/job workload. If targeted cancellation is unavailable, FolderBridge fails closed and only reaps its own worker.
- Hardened early-cancel, blocked-history, monitor-start failure, shutdown, timeout, and control-file cleanup paths while preserving the invariant that workspace mutation protection is not released until the extension worker is confirmed exited.
- Added regression coverage for valid/invalid DynamicCombo API shapes, prompt submission suppression, prompt-scoped cancellation, blocked-history cancellation, pre-submit cancellation, and the no-global-interrupt safety rule. Bundled Local ComfyUI is now 1.2.0.

## 0.8.9 — 2026-08-25

- Hardened bundled ComfyUI workflow execution for long-running image/video generation: `run` now uses a host-owned Extension Job instead of a foreground MCP request, with a 2-hour workflow timeout by default, explicit `0` for no automatic workflow timeout, and the existing job status/cancel control lane remaining responsive while generation runs.
- Fixed ComfyUI video outputs being misclassified as images when nodes expose MP4/WebM/MOV/MKV descriptors through legacy `images` fields. Generated media is no longer fetched or embedded into MCP responses; the bundled extension returns bounded artifact references, verified local paths when resolvable, workspace-relative paths when applicable, and file sizes instead.
- Bounded ComfyUI artifact metadata to 64 returned entries with an explicit truncation flag, preserved explicit image `save_directory` copying without inline Base64 in job results, and fail-closed when a relative ComfyUI `main.py` cannot be safely resolved instead of fabricating an absolute output path.
- Added regression coverage for path-only image results, explicit image saving without inline media, video path reporting without `/view` binary fetches, bounded large output sets, relative-main path safety, and the bundled ComfyUI 1.1.0 job/timeout contract.

## 0.8.8 — 2026-08-25

- Fixed two false positives found by real-workspace verification of the new built-in `test`/`build` providers: UTF-8 JSON files with a BOM are now decoded as `utf-8-sig`, and conventional diagnostic output files (`err.txt`, `error.txt`, `stderr.txt`, `stdout.txt`) are excluded from source/content validation.
- Added regression coverage reproducing the Debate-Exam BOM calibration file and Debate-Judge diagnostic-output cases while preserving failure on genuinely invalid JSON and non-diagnostic non-UTF-8 project text.

## 0.8.7 — 2026-08-25

- Made globally pre-authorized `test` and `build` capabilities usable in every selected workspace, including single-file/static HTML, documentation, content, collection, and source-only folders that do not define npm/Python build scripts. Explicit project test/build entry points still take precedence.
- Added a FolderBridge-owned bounded workspace smoke provider that reuses the core workspace safety policy, skips credential-like/VCS/dependency/hidden paths, validates common UTF-8 text plus JSON/HTML structure, and uses trusted system Node `--check` for bounded JavaScript syntax checks when Node is available. Workspace JavaScript is parsed, not executed.
- Added a non-mutating safe build fallback: static/content/no-build workspaces use an identity build, while source workspaces without a build entry point use validation-only mode. Both report their provider/mode explicitly and never pretend to generate compiled artifacts.
- `server_info` now reports the selected capability provider, so globally authorized `test`/`build` show available across workspaces while remaining auditable as `project-task`, `builtin-workspace-smoke`, or `builtin-safe-build`.

## 0.8.6 — 2026-08-25

- Fixed Git Publisher Release authentication when Git Credential Manager is already connected but GitHub CLI has no separate `gh auth login` session. Git Publisher 1.2.1 now retrieves the existing GitHub credential from GCM inside the isolated worker and passes it to `gh.exe` only through the child-process `GH_TOKEN` environment for the duration of the Release operation.
- The credential is never accepted as an MCP parameter, written to FolderBridge settings/remotes/logs, placed on the `gh` command line, or returned to the model. Release publishing therefore uses the same browser-authorized GCM account as commit/push without requiring a second GitHub login.
- Added regression coverage requiring the GCM `credential fill` bridge, forbidding the old `gh auth status` dependency, and preserving the zero-parameter project-locked Release surface.

## 0.8.5 — 2026-08-25

- Fixed the English connection-guide close button: the action label `关闭` now renders as `Close` instead of the state word `disabled`. Auto-start/service state messages continue to use `disabled` where appropriate.
- Added a regression test that distinguishes the close action from disabled-state translations.

## 0.8.4 — 2026-08-25

- Updated bundled Git Publisher to 1.2.0 with a parameterless, project-locked `release` action. It publishes only the stable version declared in `pyproject.toml`, only from `main`, only when tracked files are clean and `origin/main` matches the current HEAD, and only with tag `v<version>` plus the fixed `release/windows-x64/FolderBridge.exe` and `.sha256` assets.
- Release publishing uses the existing credential-free GitHub HTTPS origin validation and Git Credential Manager for Git operations, plus the locally installed GitHub CLI (`gh.exe`) for GitHub Release creation/upload. No arbitrary tag, version, asset path, repository, or `gh` argument is exposed to the model.
- Existing version tags are accepted only when they resolve to the current release commit; existing Releases are repaired with asset clobber and re-marked Latest, while new Releases are created only after the matching tag exists remotely.

## 0.8.3 — 2026-08-25

- Added a persistent Chinese/English launcher UI switch (`中文 / EN`) immediately to the left of the connection guide. Main-window labels, live connection/service status, Extension/Skill controls, setup-guide content, file dialogs, confirmation/error dialogs, and launcher logs share one localization layer; language changes do not alter the Tunnel configuration fingerprint or require a reconnect.
- Added localization regression gates: launcher settings migrate to v4 with `language=zh` by default, invalid language values fail closed, and AST coverage rejects newly added Chinese launcher/backend UI text unless an English rendering exists.
- Fixed the repository publishing lifecycle that allowed `main` to advance while GitHub Releases remained stale. A release commit whose first line is exactly `Release FolderBridge <pyproject version>` now triggers a Windows GitHub Actions job that re-validates the version, runs the full tests, builds and verifies the EXE, creates/validates the matching version tag, and creates or repairs the GitHub Release with `FolderBridge.exe` plus `FolderBridge.exe.sha256`. Ordinary pushes do not publish a Release, and no manual tag/version input is exposed.
- Added explicit third-party attribution for the bundled `folderbridge-engineering` Skill Pack. README documentation now states that it is a FolderBridge-specific condensed/adapted selection derived from Matt Pocock's `mattpocock/skills`, not the official upstream plugin.
- Preserved the upstream MIT license and copyright notice inside the bundled Pack via `NOTICE.md` and `LICENSE.upstream-MIT.txt`, while keeping FolderBridge's project-authored code/documentation under Apache-2.0.
- Added machine-readable upstream repository/license metadata to the bundled Pack and extended source/package regression gates so missing Matt Pocock/MIT attribution fails tests and the packaged EXE verifier checks that the attribution survives bundling.

## 0.8.2 — 2026-08-25

- Fixed upgrade compatibility for external Skill Packs and Extensions. Exact-hash approvals remain in the user-level trust stores across executable upgrades when external content/permissions are unchanged; regression coverage now locks this behavior across fresh Engine/Registry instances.
- Made bundled-vs-external ID collisions upgrade-safe without weakening trust: a release-trusted bundled component always wins, while an older external installation with the same ID is treated as superseded instead of producing a persistent duplicate-ID load error. External components still cannot override bundled components, and true same-tier duplicate IDs remain errors.
- Removed build-directory contamination from Windows releases. Packaging now uses explicit bundled allowlists for the four shipped Extensions and the engineering Skill Pack instead of recursively embedding the whole repository `extensions` / `skill_packs` directories, so untracked or locally staged third-party components cannot be accidentally promoted into `FolderBridge.exe`.
- Strengthened packaged-product smoke tests to compare the EXE's actual bundled Extension/Skill sets against those allowlists. This closes the 0.8.1 failure mode that could package a local `folderbridge-discipline` source folder as bundled and then collide with the user's separately installed copy.

## 0.8.1 — 2026-08-25

- Removed the remaining small-file search bottleneck: literal UTF-8 search now streams files up to 512 MiB instead of skipping everything above 256 KiB, keeps bounded file/time budgets, reports skip reasons explicitly, and supports result pagination. File listing is pageable as well.
- Made Git inspection reviewable at scale: `workspace(status/diff)` supports byte pagination and optional workspace-relative path scoping, so a large diff no longer becomes an irrecoverably truncated 64 KiB preview.
- Scaled model discovery without inflating the fixed MCP catalog: Extension `list` is now compact and pageable while `info` returns one full action schema; Skill initialization uses a 64 KiB compact round-robin routing index, explicitly reports omitted Skills, and keeps task-specific `skill-engine match` as the complete discovery path. Skill Pack capacity now consistently supports the declared 128 Skills and up to 128 packs per root.
- Raised legacy OOXML single-part ceilings to 128 MiB for core PPTX and the bundled Office inspector while parsing ZIP members as streams; aggregate package/XML/member-count limits remain in place.
- Updated Git Publisher to 1.1.0: status is pageable, explicit commits accept up to 128 paths while staying bounded by the unchanged 1 MiB MCP envelope, and the per-file regular-Git ceiling now matches GitHub's 100 MiB limit instead of stopping at 64 MiB. The bundled Office Extension is also 1.1.0.

## 0.8.0 — 2026-08-24

- Reworked the UTF-8 write layer so existing files up to 128 MiB can use SHA-locked exact replacement; files above 1 MiB obtain their whole-file SHA through `file_info` while the MCP single-message ceiling remains 1 MiB. Release regression coverage mechanically exercises 100 MiB exact edits plus 100 MiB transactional create and replace paths.
- Added the fixed `write_file` transaction (`begin` / `append` / `status` / `commit` / `abort`) for whole-file UTF-8 creates or replacements up to 512 MiB. Chunks are limited to 128 KiB so even worst-case JSON escaping fits under the unchanged MCP envelope.
- Transaction staging is host-owned outside the workspace, bounded to 16 active transactions, process-local, cleaned on graceful shutdown, and stale after 24 hours. Commit revalidates UTF-8, size, new SHA, workspace/link policy, and replacement baseline, then publishes from a fully written same-directory temporary file.
- Hardened both old and new write paths against clobber/TOCTOU cases: create publication is no-clobber, existing exact edits recheck SHA immediately before publication, transactional copy recomputes staged size/SHA, and POSIX staging uses private permissions with parent-directory fsync after publication where supported.
- Added bounded concurrent MCP request dispatch with independent control/data lanes (2/6 workers, 8/12 in-flight defaults), fail-fast `-32001 Server busy` admission, and serialized complete JSONL response writes so long data operations do not block control/status calls or create unbounded queues.
- Added resource-aware mutation coordination: same-file core writes serialize while different files may overlap; opaque task/capability/non-read-only Extension mutations exclude core writes for that workspace. Both foreground non-read-only Extension actions and non-read-only Jobs retain the workspace mutation lease until the host confirms process exit; failed termination enters `termination_pending` with host-owned reapers instead of releasing protection early. Job and foreground lifecycle budgets are independently bounded at 16 each.
- Removed an Extension hot-reload TOCTOU by preparing one immutable `record + action + SHA-256` execution contract and using that same contract for mutation-lock policy, authorization, and worker launch; the worker still verifies a private execution snapshot against the prepared hash.
- Made stdio shutdown concurrency-safe: close workspace-mutation admission and wake queued mutation waiters first, retry termination of owned Extension workers without pretending a still-live process released its lease, then drain bounded MCP workers and clean process-local transactional staging.

## 0.7.4 — 2026-08-24

- Fixed the four OpenAI Secure MCP Tunnel text fields (`tunnel-client`, Profile, Tunnel ID, Runtime API Key) on Windows Per-Monitor DPI changes. They no longer depend on the native Vista `ttk.Entry` element, whose runtime geometry can remain at the old monitor scale; FolderBridge now uses one explicit DPI-managed text-entry seam and recalculates font, focus border, caret width, and grid internal padding whenever monitor DPI changes.
- Added a Tk geometry regression proving a Tunnel text field grows when runtime DPI changes from 96 to 144, in addition to the static action-surface regression.

## 0.7.3 — 2026-08-24

- Updated the Extension ABI documentation, English/Chinese READMEs, Launcher appendix, and copyable LLM authoring prompt to discourage aggregate public actions such as `run-all` / `verification-suite`. Extensions should expose small fixed allowlisted operations, optionally publish a non-executing `verification-plan`, and reserve Job mode for genuinely atomic long-running work.
- Made the MCP connection-guide dialog content-aware and monitor-aware. Initial and final geometry now use the current monitor work area, respect DPI-scaled minimums, clamp the dialog on-screen, and derive guide text width from the available dialog width instead of a fixed 720-pixel request.

## 0.7.2 — 2026-08-24

- Fixed the Launcher status for dynamic workspace Extensions: an approved/enabled adapter that only loads for matching workspaces is now shown as `已启用 · 工作区匹配时加载` instead of the misleading global `未加载` state.
- Widened the Extensions & Skills sidebar and unified its frame, canvas, wrap, DPI-refresh, and window-fit width budgets so dense plugin/Skill metadata has more usable horizontal space.
- Removed redundant sidebar rebuilds from simple show/hide toggles, reducing the visible jump during expansion while keeping explicit rescan and state-change refresh paths intact.
- Batched the three main-panel `全部折叠/全部展开` layout work so all visibility changes complete before one final window reflow; individual panel toggles keep their single reflow path.

## 0.7.1 — 2026-08-23

- Fixed Launcher mouse-wheel routing so blank/main-page areas scroll the page while native scroll areas such as logs, workspace lists, comboboxes, and independently scrollable canvases keep their own wheel behavior; the Extensions & Skills canvas scrolls itself.
- Launcher sizing now uses the current Windows monitor work area (`rcWork`) instead of full-screen bounds. Startup is centered inside that work area, and content-driven resize after panel expand/collapse is capped at 90% of the available height and repositioned inside the monitor, with the existing page scrollbar handling overflow.

## 0.7.0 — 2026-08-23

- Added a general local Skill Engine for trusted methodology text. Skills are discovered from bounded manifests, matched deterministically, and loaded on demand; Skill markdown is never executed as local code.
- Added the bundled read-only `skill-engine` Extension with `list`, `match`, and `get` actions behind the existing stable `extension` MCP gateway, so installing additional Skill Packs does not add MCP tool names or require per-Skill connector schemas.
- Added initialization-time Skill routing guidance and a bounded routing index. FolderBridge can make relevant Skills discoverable to the model, but reports the behavior honestly as model-routed rather than claiming it can force invocation.
- Added exact-hash trust for external Skill Packs. Unapproved or stale Packs are hidden from the model-facing view; approval uses the hash displayed by the Launcher, and `get` re-hashes the exact bytes it returns so `match`/`get` changes fail closed.
- Added the bundled `folderbridge-engineering` Pack with six focused engineering methods: codebase design, architecture improvement, bug diagnosis, test-driven development, code review, and implementation.
- Expanded the Launcher sidebar to **Extensions & Skills**, with separate Skill Pack enable/disable, external exact-hash approval, revoke controls, details, and a user Skill Pack directory. The warning explicitly distinguishes prompt/methodology influence from executable plugin code.
- Added `skills --json` diagnostics with deterministic UTF-8 output on Windows, and expanded `extensions --self-test` so packaged smoke tests exercise both the ComfyUI worker and the Skill Engine worker.
- Windows packaging now includes `skill_packs`, verifies discovery of the bundled `skill-engine` Extension and engineering Pack, and runs worker smoke tests before generating the EXE checksum.
- Unified the per-user FolderBridge configuration-root calculation so the launcher, MCP host, and cleaned Extension workers resolve the same Skill/Extension state location.
- Added PPTX inspection observability for aggregate XML use: normal results now expose `xml_uncompressed_bytes`, `xml_limit_bytes`, and `xml_limit_usage_ratio` in addition to the existing 256 MiB pre-parse guard.

## 0.6.4 — 2026-08-23

- Closed the Extension exact-hash execution race end to end. Workers now create a private temporary execution snapshot, verify it against the host-pinned SHA-256, and import/run only from that verified snapshot; relative plugin file access is rooted there as well.
- Made Extension trust-store read/modify/write operations process-locally atomic so concurrent approval and enable/disable operations do not overwrite one another.
- Enforced strict JSON at Extension manifest, request, response, and serialization boundaries; non-standard `NaN` and `Infinity` values are rejected.
- Fixed overlapping secret redaction by replacing longer values first, and fixed JSON-schema enum handling so booleans are distinct from numeric values.
- Fixed a managed ComfyUI lifecycle race by keeping a stable local process handle throughout one startup operation.
- Consolidated remaining first-party subprocess paths: core Git inspection, bundled Git Publisher Git/GCM calls, and bundled Office PowerShell rendering now use bounded owned-process semantics with child-tree timeout cleanup.
- Git safety inspection now fails closed if its bounded output is truncated instead of treating a partial result as complete.
- Added a 256 MiB aggregate uncompressed XML/relationship budget to core PPTX inspection before XML parsing.
- Restored explicit `server_info` observability that Extension jobs are process-local and do not survive a FolderBridge server restart, with regression coverage for these failure modes.

## 0.6.3 — 2026-08-23

- Fixed cross-monitor DPI font resizing on Windows. FolderBridge now manages explicit DPI-aware named fonts and recalculates their pixel sizes whenever `GetDpiForWindow` changes, instead of relying on Tk's undefined runtime behavior for resizing existing widgets after `tk scaling` changes.
- Applied the managed font set to ttk styles, entries, Treeview text/headings, the runtime log, the primary start/stop button, and setup-guide text tags; guide spacing/margins are refreshed with the same DPI transition.
- Added regression coverage for deterministic 96→144→96 font sizing and for removing fixed tuple fonts from the live GUI path.

## 0.6.2 — 2026-08-23

- Performed a deep-module architecture audit focused on module depth, interfaces, seams, locality, and deletion leverage rather than splitting files by size alone.
- Added a single `process_control` module for FolderBridge-owned subprocess groups and tree termination. Extension workers/jobs, Tunnel commands, managed ComfyUI, approved tasks, and bounded Git inspection now share the same Windows/POSIX ownership seam instead of maintaining separate `taskkill` / process-group implementations.
- Bounded Extension Job lifecycle resources: at most 16 jobs may be starting/running concurrently, and only the newest 128 finished job records are retained in the current FolderBridge process.
- Added runtime observability to `server_info`: the running FolderBridge version and Job resource policy are now reported directly, making stale EXE versus client-cached MCP tool schema easier to diagnose after upgrades.
- Refined live ComfyUI status polling so the Extensions sidebar does not rebuild every two seconds when nothing changed. Background probes use a separate pending state, unchanged results update only the status label, and structural changes still trigger a full card refresh.
- Expanded regression coverage around the new process-control seam, Job concurrency bounds, runtime version reporting, and live-service UI behavior.

## 0.6.1 — 2026-08-23

- Added true collapsible main-page sections for Local Workspaces/Permissions, OpenAI Secure MCP Tunnel, and Runtime Log, each with its own expand/collapse control plus a global Expand all / Collapse all button.
- Collapsing or expanding a section re-runs the existing content-fit logic so the Launcher recomputes its ideal height and only falls back to scrolling when the display cannot fit the visible sections.
- The managed ComfyUI service card now refreshes its actual status every 2 seconds while the Extensions sidebar is open. Online states are shown in green, offline/unconfigured states in red, and transient detection/startup states in a neutral color.
- Re-verified the 0.6.0 long-job implementation after restart. The running backend exposes the new job-aware security policy and source schema, but an already-loaded ChatGPT connector can keep its older cached `extension` tool enum until the connector/conversation tool schema is reloaded.

## 0.6.0 — 2026-08-23

- Extended Extension ABI v1 with host-owned long-running jobs. Actions may declare `run_mode=job`; `extension(action="run")` returns a `job_id`, while `job_status` and `job_cancel` inspect or terminate the owned worker process tree without adding MCP tool names.
- Extension timeouts now support `0` (no automatic timeout termination) through 86,400 seconds (24 hours). Per-action timeout overrides take precedence over the extension default; zero still allows explicit cancel and FolderBridge shutdown cleanup.
- Added exact environment inheritance permissions such as `environment.inherit:OPENAI_API_KEY`; only explicitly declared variables cross the cleaned worker boundary. `CONTROL_PLANE_API_KEY` and other FolderBridge/control-plane variables are reserved and can never be inherited by an extension. Values inherited through key/token/secret/password/auth-like variable names are recursively redacted from worker results, logs, and surfaced errors.
- Added `network.outbound:https` as an explicit authorization-contract permission for integrations that must call external HTTPS APIs. As with other Extension permissions, this is approval metadata rather than a kernel network sandbox.
- Added host validation for plugin-declared `workspace_artifacts`: returned workspace-relative files are re-resolved through FolderBridge path policy and returned with byte size and SHA-256 metadata.
- Extension timeout/cancel paths now terminate the complete FolderBridge-owned worker process tree instead of only the Python worker shell, so child runtimes such as Node.js do not remain orphaned.
- Added `.api-config.json` / `api-config.json` to credential-like workspace names hidden from normal MCP file tools, preventing Judge-style API configuration files from being surfaced as ordinary project text.
- The Windows launcher now grows to the page's actual requested content size after layout, capped at 94% of the display. The main vertical scrollbar is hidden when the complete page fits and appears only when content exceeds the available viewport; DPI changes and Extension sidebar toggles re-run the same fit logic.

## 0.5.1 — 2026-08-23

- Added the bundled **Git Publisher** extension behind the stable `extension` gateway, with explicit `github.web-auth`, `git.commit-selected-files`, and `git.push-current-branch` permissions.
- Added browser-based GitHub authorization through Git Credential Manager (`github login --web`); OAuth credentials remain in Windows Credential Manager and the MCP action schema intentionally exposes no token/PAT/password field.
- Added bounded `status`, `commit`, and `push` actions: repository root/current branch/origin are revalidated on every mutation and only credential-free `https://github.com/<owner>/<repo>[.git]` origins are accepted.
- `commit` only stages explicitly named regular files, rejects pre-existing staged changes, credential/key-like files, dependency/generated/VCS paths and content-transforming Git attributes, verifies the staged set exactly, and disables hooks plus commit signing. It never runs `git add .`.
- `push` is current-branch-only, never force-pushes, disables pre-push hooks and terminal prompts, and forces Git Credential Manager rather than repository-local credential helpers.
- Extended Extension ABI v1 permission vocabulary and Windows packaging smoke tests so the single-file EXE must contain ComfyUI, Microsoft Office Native, and Git Publisher.

## 0.5.0 — 2026-08-23

- Added the bundled **Microsoft Office Native** extension behind the stable `extension` gateway; no new MCP tool names are introduced.
- Added portable `inspect_docx` OOXML reading for Word paragraphs, paragraph styles/numbering, tables, section/page settings, headers/footers, media relationships, hyperlinks, footnotes, endnotes and comments without launching Word.
- Added portable `inspect_xlsx` OOXML reading for Excel workbook/sheet structure, bounded cell ranges, formulas plus cached values, shared/inline strings, merged ranges, hidden rows/columns, defined names, calculation properties and external-link parts without launching Excel.
- Added globally authorized native `render` for `.pptx`, `.docx` and `.xlsx`: PowerPoint uses `Slide.Export`; Word and Excel use their native fixed-format engines followed by the Windows `Windows.Data.Pdf` page renderer to produce PNGs that `image_open` can inspect.
- Native rendering can emit a deterministic sibling ZIP and returns source/output byte counts and SHA-256 hashes, enabling audit workflows to use Office originals for structure and generated PNGs for full-page visual evidence.
- Hardened Office automation: only workspace-relative non-link paths are accepted; macro-enabled formats are excluded; documents open read-only with Office automation macros force-disabled; Excel link updates are disabled; intermediate PDFs stay in FolderBridge profile state; the PowerShell entrypoint is fixed and invoked with `shell=False`.
- Added Office extension regression coverage for manifest permissions, DOCX/XLSX structure extraction, path traversal rejection and PowerShell syntax validation.
- Windows packaging now verifies that both bundled ComfyUI and Microsoft Office extensions are present in the single-file EXE.

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
