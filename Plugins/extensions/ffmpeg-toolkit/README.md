# FFmpeg Toolkit — FolderBridge External Extension

Version 0.1.1.

A reusable external FolderBridge ABI v1 Extension for workspace-confined local FFmpeg/FFprobe work.

## Install

Copy this entire `ffmpeg-toolkit` directory to:

```text
%LOCALAPPDATA%\folderbridge-mcp\extensions\ffmpeg-toolkit\
```

The destination directory must directly contain `folderbridge-extension.json`, `plugin.py`, `README.md`, and `tests/`. Then open FolderBridge **Extensions & Skills**, rescan if needed, approve the exact directory hash + declared permissions, and enable **FFmpeg Toolkit**.

No installer script, FolderBridge rebuild, Tunnel restart, or MCP tool re-registration is required for normal hot loading.

## Actions

- `status` — locate and verify `ffmpeg` + `ffprobe`.
- `capabilities` — inspect encoders, decoders, codecs, formats, muxers, demuxers, filters, protocols, hardware accelerators, devices, bitstream filters, pixel/sample formats, channel layouts and colors.
- `probe` — run FFprobe with either a convenient single `path` or an explicit placeholder-based FFprobe argv.
- `run` — run a local-media FFmpeg argv as a host-owned FolderBridge Job.

`run` is intentionally deep rather than command-specific: normal FFmpeg codecs, filters, `filter_complex`, mapping, multiple declared inputs/outputs, subtitle burn-in, frame sequences and hardware encoders can be used without exposing a shell.

## Probe convenience contract

`path` is the simple single-input API:

```json
{
  "path": "media/input.wav"
}
```

It expands to a standard JSON metadata/stream/chapter/program probe.

When `path` is combined with custom `args`, version 0.1.1 automatically appends the internal `{{source}}` placeholder if the caller did not place it explicitly:

```json
{
  "path": "media/input.wav",
  "args": ["-v", "error", "-show_format", "-of", "json"]
}
```

Advanced callers can instead use `paths` directly and place their own placeholders.

FFmpeg and FFprobe now use separate argv builders. FFmpeg keeps the managed `-nostdin` policy; FFprobe does not receive the FFmpeg-only `-nostdin` switch. Both retain the local protocol whitelist.

## Path declarations

File-system paths must enter argv through declared workspace-relative references:

```json
{
  "src": {"path": "media/input.mp4", "mode": "input"},
  "subs": {"path": "media/subtitles.srt", "mode": "input"},
  "dst": {"path": "output/final.mp4", "mode": "output"},
  "frames": {"path": "output/frame-%04d.png", "mode": "output_pattern"}
}
```

Use `{{src}}`, `{{dst}}`, etc. in argv. `{{filter:subs}}` emits a filter-escaped workspace path for filters such as `subtitles=`.

Supported modes are `input`, `input_pattern`, `input_dir`, `output`, `output_pattern`, and `output_dir` (read-only `probe` accepts input modes only).

## Security boundary

The executable is fixed to `ffmpeg(.exe)` / `ffprobe(.exe)`; there is no arbitrary executable or shell parameter.

The Extension rejects raw absolute paths, UNC paths, `..`, explicit network/protocol URLs, stdin/stdout media pipes, direct capture-device formats, external filter scripts, and network/plugin-loading filters. Subprocesses use `shell=False`, bounded logs, a fixed `file,crypto,data` protocol whitelist, and FolderBridge-owned process-tree cleanup.

FolderBridge external Extensions are an authorization contract rather than a kernel sandbox. FFmpeg itself parses complex media/container formats, so untrusted media and playlists should still be treated as untrusted input.

## FFmpeg discovery

Resolution order:

1. trusted process PATH;
2. fixed workspace candidates `GPT-SoVITS/runtime`, `tools/ffmpeg/bin`, or `ffmpeg/bin`;
3. an explicit workspace-relative `tool_dir` containing fixed FFmpeg/FFprobe filenames.

FFmpeg binaries are deliberately not bundled with this Extension.

## Development tests

`tests/test_plugin.py` covers path confinement, URL/device/plugin-filter rejection, complex argv/placeholder handling, output patterns, sensitive paths, the separate FFmpeg/FFprobe argv contracts, and the 0.1.1 `probe.path + custom args` convenience behavior.
