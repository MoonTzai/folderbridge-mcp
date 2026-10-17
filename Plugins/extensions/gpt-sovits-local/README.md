# Local GPT-SoVITS — FolderBridge External Extension

Version 0.2.2.

0.2.2 completes the live schema-compatibility pass: action schemas now use only FolderBridge's supported JSON-schema subset, including the reference/stitch positive-number fields that previously used unsupported `exclusiveMinimum`. Strict greater-than-zero checks remain in the runtime. It retains the 0.2.1 removal of `pattern` and the noisy-stdout final-JSON recovery.

A workspace-confined GPT-SoVITS v2Pro toolkit built on the fixed workspace bridge:

`GPT-SoVITS-Bridge/runner.ps1`

The extension does not expose arbitrary command text, script paths, executables, URLs, or unrestricted filesystem access. Long operations are host-owned FolderBridge jobs; local services use fixed loopback endpoints.

## Verified runtime authority

The current automation targets the installed and verified GPT-SoVITS **v2Pro** package layout. It does not pretend that unverified v1/v2/v4/v2ProPlus training paths have the same contract.

Expected workspace roots:

`GPT-SoVITS/`
`GPT-SoVITS-Bridge/`
`GPT-SoVITS-Work/`

The bridge uses the packaged runtime Python, official slicer/ASR/dataset scripts, official GPT/SoVITS trainers, and the official TTS pipeline.

## Actions

### Read-only inspection

- `verification-plan`
- `status` — bridge/runtime/WebUI/API readiness plus managed-service PID state.
- `list-models` — enumerate pretrained and trained GPT/SoVITS weights without loading them.
- `dataset-status` — inspect one experiment's prepared text/BERT/Hubert/WAV/semantic assets and compact checkpoints.
- `service-status` — inspect WebUI/API PID records and live-process status.
- `probe-runtime` — fixed runner probe.

### Audio preparation and transcription

- `analyze-audio`
- `analyze-audio-batch`
- `prepare-reference` — bounded trim, silence trim, mono/stereo handling, resample, peak normalization, fades, and sidecar metadata.
- `stitch-audio` — resample/concatenate reference or generated clips with bounded pauses and optional normalization.
- `slice-audio` — official GPT-SoVITS slicer with bounded thresholds/timing controls.
- `asr` — optional slicing followed by the configured local FunASR route; produces a .list plus acoustic manifest and bounded reference shortlist.

### Fine-tuning workflow

- `prepare-dataset` — official v2Pro 1A/1B/1C preparation pipeline.
- `train` — creates retained v2Pro configs, trains SoVITS + GPT, and returns compact checkpoint paths.

Generated work stays under `GPT-SoVITS-Work`:

- `slices/<job>/`
- `asr/<job>/`
- `manifests/<exp>/`
- `experiments/<exp>/`
- `configs/<exp>/`
- `checkpoints/<exp>/gpt/`
- `checkpoints/<exp>/sovits/`
- `inference/<exp>/`
- `logs/`
- `state/`

### Inference

- `infer`
- `infer-batch`

Inference supports a primary reference plus auxiliary references, prompt/target language, GPT/SoVITS checkpoint selection, top-k/top-p/temperature, split method, batching, speed, fragment interval, seed, parallel inference, repetition penalty, CPU override, and metadata sidecars.

Batch inference reuses one loaded TTS pipeline/reference context instead of spawning one operation per line.

### Local services

- `launch-webui` / `stop-webui` — GPT-SoVITS UI lifecycle.
- `launch-api` / `stop-api` — official API v2 fixed to `127.0.0.1:9880`.
- `service-status` — inspect both managed services.

The WebUI uses the package's local port `9874`. API port is deliberately not caller-controlled.

### Compatibility

- `run` remains for old callers with a fixed operation enum, but it is deprecated. New workflows should use the dedicated actions so FolderBridge can validate parameters and effect semantics before a worker starts.

## Artifacts and auditability

Dedicated bridge operations return known output paths to FolderBridge as `workspace_artifacts`; the host revalidates workspace confinement and supplies size/SHA-256 receipts where applicable.

No arbitrary script/executable field is exposed. The bridge's own path resolver rejects escapes outside the selected workspace.

## Installation/update

Run `install.ps1` from this directory, or install the release ZIP through FolderBridge. The installer atomically publishes to:

`%LOCALAPPDATA%\folderbridge-mcp\extensions\gpt-sovits-local`

Then rescan **Extensions & Skills**, approve the exact new hash/permissions, and enable the extension. External hot-load updates do not require rebuilding FolderBridge.

## Permissions

The manifest declares:

- `workspace.read`
- `workspace.write`
- `extension.state`
- `process.execute:powershell.exe`
- loopback `127.0.0.1:9874` and `127.0.0.1:9880`
- outbound HTTPS because supported ASR backends may use their own model fallback when a required local asset is absent

The extension remains ordinary approved code running with the current OS user's privileges; FolderBridge permissions are an authorization/audit contract, not a kernel sandbox.
