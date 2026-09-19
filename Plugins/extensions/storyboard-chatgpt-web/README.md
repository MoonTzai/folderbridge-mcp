# Storyboard ChatGPT Web

Independent FolderBridge Extension for Storyboard Forge. It uses ChatGPT Web itself for image generation/editing and, when enabled, a separate ChatGPT Web conversation for native Visual Judge review.

`storyboard-chatgpt-web 0.2.0` is the current accepted Storyboard Forge provider. It completed the GENERATE_ONLY and GENERATE_AND_JUDGE live smoke chains, including native Judge PASS-only canonical acceptance, and the retired `storyboard-generate-review 0.2.5` live Extension has been removed.

## Proven implementation reused

The Generator transport follows the previously proven FolderBridge intro workflow from `chatgpt-storyboard/live/m4-orchestrator-e2e.cjs`:

- separate Generator and Judge ChatGPT conversations;
- exact `operation_key` per state/attempt;
- upload the real predecessor / failed candidate image for edits;
- wait for ChatGPT Web native image generation;
- recover the exact generated image through the conversation's `image_asset_pointer` and ChatGPT file download endpoint;
- persist exact bytes, SHA-256 and image dimensions locally;
- upload that exact local artifact to Judge;
- PASS advances; FAIL creates minimum repair from the immediately failed candidate.

The copied historical source is kept under `legacy_reference/` for audit only. Runtime code does not depend on Puppeteer or the old project.

## Execution modes

### GENERATE_AND_JUDGE

Full two-conversation loop:

`Generator -> exact artifact -> Judge -> PASS accept / FAIL repair -> next frame`

Canonical frame acceptance remains PASS-only.

### GENERATE_ONLY

Judge is bypassed completely. The Extension reads the same Prompt Pack JSON and automatically generates images in order.

Outputs are recorded as `generated_unreviewed`, not PASS/accepted. They may be used as the next frame's sequence predecessor so continuity still works, but they never enter the PASS-only canonical registry.

A run can switch from `GENERATE_AND_JUDGE` to `GENERATE_ONLY`. Switching back to Judge mode after unreviewed sequence artifacts already exist requires a fresh run so reviewed and unreviewed authority cannot be mixed.

## Main actions

- `run-init` — freeze Prompt Pack and choose `execution_mode`.
- `run-mode-set` — change the mode when authority rules allow it.
- `generator-bind` — bind/reuse a dedicated ChatGPT Web Generator conversation.
- `generator-run` — upload exact references, send one compiled task, recover exact ChatGPT image bytes and persist them.
- `judge-bind` / `judge-run` — independent native eight-axis Judge.
- `candidate-accept` — PASS-only canonical promotion.
- `repair-preview` — immediate-failed-candidate repair contract.
- `auto-run` — durable resumable loop; in GENERATE_ONLY it never invokes Judge.

## Provider safety

Generator and Judge share the same account-level local safety state:

- fresh ChatGPT starts at least 20 seconds apart;
- at most 8 fresh starts in a rolling 300-second window;
- history-sensitive navigation cadence;
- known history/rate-limit warning detection;
- 120 / 240 / 480 / 600 second cooldown;
- 1800-second quiet strike reset;
- browser starts at `about:blank`;
- no conversation sidebar/history scanning loop.

Conversation IDs and operation keys assist recovery; they are not semantic authority. Exact Prompt Pack bytes, exact uploaded image SHA provenance, native Judge verdicts and PASS-only canonical state remain the authority in reviewed mode.
