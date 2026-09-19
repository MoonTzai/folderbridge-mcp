# Windows Capture Toolkit

A bounded external FolderBridge Extension for **one-shot Windows screenshots plus exact-window left-click automation**.

## What it captures

- exact top-level window surfaces selected from `list-windows`
- the current foreground window
- a selected monitor
- an explicit desktop rectangle
- browser, WebView2, media-player and streaming-player **visible pixels** when Windows exposes them normally

`capture-window` and `capture-active-window` support two explicit modes:

- `window_surface` (default): asks Windows to render that exact window surface with `PrintWindow`, so ordinary application UI is not replaced by pixels from an overlapping window.
- `visible_pixels`: copies the actual desktop pixels inside the window rectangle. This is intentionally suited to Chromium/WebView2/video playback surfaces that may not render through `PrintWindow`, but the target must be visible and unobstructed because overlapping windows are part of those pixels.

For ordinary software UI, use `window_surface`. For a playing browser/video/streaming surface, use `visible_pixels` and keep the player unobstructed.

## Bounded mouse input

`click-window` performs exactly one **left click** inside one exact top-level window returned by `list-windows`.

- coordinates are relative to the full window surface shown by `capture-window`; there is no absolute desktop click API
- the requested point must be inside the current target-window bounds or the action fails closed
- a minimized target may be temporarily restored, foregrounded for the click, and returned to minimized state afterward
- the pointer position is restored after the click
- there is no keyboard input, drag input, wheel input, double-click action, arbitrary macro language or background click loop

This is enough for an assistant to inspect a screenshot, choose a specific FolderBridge control, click it, wait for the UI to settle, then call `capture-window` again.

## What it does not do

- no keyboard input or text entry
- no drag/drop, scrolling, repeated macro loop or absolute desktop click
- no continuous/background recording
- no arbitrary process execution; the only declared executable permission is the fixed current `FolderBridge.exe`, used solely for the isolated demo instance action
- no network access
- **no DRM/HDCP/protected-surface bypass**

If protected playback renders as black to normal desktop capture, the Extension preserves that result and reports visual statistics. A near-black warning is only diagnostic because a genuinely dark video frame can look the same.

## Public actions

- `status`
- `list-windows`
- `list-monitors`
- `capture-window`
- `capture-active-window`
- `capture-monitor`
- `capture-region`
- `click-window`
- `launch-demo-folderbridge`

Every capture action accepts an optional `delay_ms` from 0 to 30000. This is useful for pausing a video at a desired point or revealing player controls before a one-shot capture. Window capture also accepts `restore_minimized` (default `true`): when the target is minimized, the Extension temporarily shows it without activation, captures it, and minimizes it again in a `finally` path.

Capture actions write exactly one `.png` inside the selected FolderBridge Workspace and return the PNG path, byte size, SHA-256, dimensions, source identity, capture rectangle and bounded visual statistics.

`click-window` accepts the exact `window_handle`, window-relative `x` / `y`, optional `restore_minimized` and a bounded `after_click_ms` settle delay (0–5000 ms). It does not accept executable, command, script, keyboard or arbitrary desktop-coordinate parameters.

`launch-demo-folderbridge` is a host-owned Job. It launches the same frozen `FolderBridge.exe` with fresh temporary `LOCALAPPDATA` and `APPDATA` roots, so the demo process does not share the primary instance's FolderBridge config, Tunnel profile, workspace list, Extension trust store, or screenshot profile. Closing/cancelling the Job tears down the child process tree and the temporary profile is deleted afterward.

## Safe workflow for a player UI

1. Make the browser/player window visible and unobstructed.
2. If needed, pause at the desired frame and reveal the playback controls.
3. Call `list-windows` and choose the exact `window_handle`.
4. Call `capture-window` with that handle, `capture_mode=visible_pixels`, and a workspace-relative PNG output path.
5. Inspect the returned `visual_stats.warning`. If a protected surface is black, do not try to circumvent it; use an allowed still/recording source instead.

## Install

From PowerShell:

```powershell
& .\install.ps1
```

The installer copies the complete Extension into:

`%LOCALAPPDATA%\folderbridge-mcp\extensions\windows-capture-toolkit`

Then open **FolderBridge → Extensions & Skills**, rescan, review the exact hash and the two bounded permissions (`workspace.write` and `process.execute:FolderBridge.exe`), approve it, and enable it.

Any source update changes the exact tree hash and requires approval again.
