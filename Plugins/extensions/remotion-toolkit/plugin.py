from __future__ import annotations

"""FolderBridge Remotion Toolkit 0.1.0 — external, hot-loadable, no FB core changes.

ABI: handle(action, params, context) -> dict
Only executes the extension-bundled bridge.cjs with the locally installed Node.
Never runs caller-supplied commands, URLs, shell scripts, npm or remote MCP.
"""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

ENTRY = Path(__file__).resolve().with_name("bridge.cjs")
VERSION = "0.1.2"
HEX = re.compile(r"^[a-fA-F0-9]{64}$")
JOB = re.compile(r"^[a-z][a-z0-9_-]{2,62}$")
BAD_PARTS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".npm-cache", ".ssh", "credentials", "secrets", ".folderbridge.json",
}
BAD_EXT = {".key", ".p12", ".pfx", ".pem"}
CODECS = {"h264", "h265", "vp8", "vp9", "av1", "prores", "gif", "mp3", "wav", "aac", "h264-ts"}
FORMATS = {"png", "jpeg", "webp", "pdf"}
ALLOWED_OPTS = {
    "composition_id", "input_props", "frame", "frames", "seconds", "frame_ranges",
    "codec", "image_format", "jpeg_quality", "scale", "concurrency",
    "audio_codec", "audio_bitrate", "video_bitrate", "crf", "hardware_acceleration",
    "pixel_format", "color_space", "sample_rate", "muted", "enforce_audio_track",
    "prores_profile", "x264_preset", "gop_size", "every_nth_frame", "gif_loops",
    "max_rate", "buffer_size", "log_level", "timeout_ms", "noise_db",
    "min_silence_seconds",
}
RENDER_ACTIONS = {"bundle", "render-still", "render-stills", "render-frames", "render-video"}
READ_ACTIONS = {"status", "verification-plan", "project-inspect", "preflight",
                "compositions", "media-probe", "silence-scan", "studio-guide"}


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _obj(value: Any) -> bool:
    return isinstance(value, dict)


def _sha(file: Path) -> str:
    h = hashlib.sha256()
    with file.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _workspace(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        _fail("A FolderBridge workspace is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        _fail("Selected workspace is not a directory")
    return root


def _is_reparse(file: Path) -> bool:
    try:
        attr = file.lstat().st_file_attributes
        return bool(attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except (OSError, AttributeError):
        return False


def _clean(raw: Any) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or len(raw) >= 950 or "\\" in raw or "\0" in raw:
        _fail("Paths must be bounded workspace-relative POSIX strings")
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts or not p.parts or any(":" in x for x in p.parts):
        _fail("Path traversal, absolute paths and drives are forbidden")
    for part in p.parts:
        if part in (".", "") or part.casefold() in BAD_PARTS or Path(part).suffix.casefold() in BAD_EXT:
            _fail("Protected workspace path component")
    return p


def _existing(root: Path, raw: Any, *, directory: bool = False) -> tuple[str, Path]:
    p = _clean(raw)
    candidate = root.joinpath(*p.parts)
    curr = root
    for part in p.parts:
        curr /= part
        if curr.exists() or curr.is_symlink():
            if curr.is_symlink() or _is_reparse(curr):
                _fail("Symlink/junction traversal is forbidden")
    try:
        real = candidate.resolve(strict=True)
        real.relative_to(root)
    except (ValueError, OSError) as exc:
        raise RuntimeError("Existing workspace file not found or escaped workspace") from exc
    if directory and not real.is_dir():
        _fail("Expected directory")
    if not directory and not real.is_file():
        _fail("Expected file")
    return p.as_posix(), real


def _project(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    rel, directory = _existing(root, params.get("project_path"), directory=True)
    if not any("vibe" in segment.casefold() for segment in PurePosixPath(rel).parts):
        _fail("A Vibe-named project directory is required")
    _, marker = _existing(root, rel + "/remotion-toolkit.project.json")
    if marker.stat().st_size > 32768:
        _fail("Project marker is too large")
    meta = json.loads(marker.read_text(encoding="utf-8"))
    if not _obj(meta) or meta.get("schema") != 1 or meta.get("contract") != "folderbridge-remotion-toolkit-v1":
        _fail("Invalid project marker; expected Remotion Toolkit contract v1")
    entry_rel = meta.get("entry")
    if not isinstance(entry_rel, str) or not entry_rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs")):
        _fail("Invalid Remotion source entry")
    _, entry = _existing(root, rel + "/" + entry_rel)
    _, package = _existing(root, rel + "/package.json")
    pub = None
    public_input_checks = []
    if meta.get("public_dir") and meta.get("shared_public_dir"):
        _fail("Choose project public_dir or shared_public_dir, not both")
    if meta.get("public_dir"):
        _, pub = _existing(root, rel + "/" + meta["public_dir"], directory=True)
    if meta.get("shared_public_dir"):
        shared_rel, pub = _existing(root, meta["shared_public_dir"], directory=True)
        if not any("vibe" in segment.casefold() for segment in PurePosixPath(shared_rel).parts):
            _fail("Shared media must remain inside a Vibe-named FolderBridge workspace directory")
        asset_hashes = meta.get("shared_public_sha256_map")
        if not _obj(asset_hashes) or not 1 <= len(asset_hashes) <= 100:
            _fail("A shared public directory requires SHA pins for 1..100 representative media files")
        for relative_asset, expected in asset_hashes.items():
            if not isinstance(expected, str) or not HEX.fullmatch(expected):
                _fail("Invalid shared public asset SHA")
            try:
                _, file = _existing(root, shared_rel + "/" + relative_asset)
                actual = _sha(file)
            except RuntimeError:
                actual = None
            public_input_checks.append({
                "path": shared_rel + "/" + relative_asset,
                "sha256": actual,
                "matches": actual is not None and actual.casefold() == expected.casefold(),
            })
    browser = None
    if meta.get("browser"):
        browser_raw = meta["browser"]
        if not isinstance(browser_raw, str) or PurePosixPath(browser_raw).name.casefold() not in (
            "chrome-headless-shell.exe", "chrome.exe", "chrome-headless-shell", "chrome"
        ):
            _fail("Browser executable must be a Chrome/Chrome Headless Shell binary")
        try:
            _, browser = _existing(root, browser_raw)
        except RuntimeError:
            browser = None
    if not isinstance(meta.get("entry_sha256"), str) or not HEX.fullmatch(meta["entry_sha256"]):
        _fail("No valid expected entry SHA-256")
    if not isinstance(meta.get("package_sha256"), str) or not HEX.fullmatch(meta["package_sha256"]):
        _fail("No valid expected package SHA-256")
    version = meta.get("remotion_version")
    if not isinstance(version, str) or not re.fullmatch(r"4\.\d+\.\d+", version):
        _fail("An exact Remotion 4.x version must be pinned")
    modules = meta.get("required_modules", [])
    if not isinstance(modules, list) or len(modules) > 20 or any(
        not isinstance(x, str) or not re.fullmatch(r"(?:@[a-z0-9-]+/)?[a-z0-9-]+", x) for x in modules
    ):
        _fail("Invalid required modules")
    operations = meta.get("allowed_operations", [])
    if not isinstance(operations, list) or not all(
        isinstance(x, str) and x in RENDER_ACTIONS | READ_ACTIONS for x in operations
    ):
        _fail("Invalid allowed operations")
    actual_entry = _sha(entry)
    actual_package = _sha(package)
    extra_sources = meta.get("source_sha256_map", {})
    if not isinstance(extra_sources, dict) or len(extra_sources) > 200:
        _fail("source_sha256_map must be a map with no more than 200 entries")
    source_audit = []
    for relative_source, expected_source_sha in extra_sources.items():
        if not isinstance(expected_source_sha, str) or not HEX.fullmatch(expected_source_sha):
            _fail("Invalid SHA-256 value in source map")
        try:
            _, source_file = _existing(root, rel + "/" + relative_source)
            actual_source_sha = _sha(source_file)
            source_audit.append({"path": relative_source, "sha256": actual_source_sha,
                                 "matches": actual_source_sha.casefold() == expected_source_sha.casefold()})
        except RuntimeError:
            source_audit.append({"path": relative_source, "sha256": None, "matches": False})
    source_audit.extend(public_input_checks)
    source_inputs_match = all(row["matches"] for row in source_audit)
    dependency_provider = meta.get("shared_dependency_provider")
    runtime_package = package
    runtime_root = directory
    provider_audit = {"mode": "project_local", "pass": True}
    if dependency_provider is not None:
        if not isinstance(dependency_provider, dict) or set(dependency_provider) != {
            "project_path", "package_sha256", "package_lock_sha256"
        }:
            _fail("Shared dependency provider must pin exact workspace project, package and lockfile hashes")
        for key in ("package_sha256", "package_lock_sha256"):
            if not isinstance(dependency_provider[key], str) or not HEX.fullmatch(dependency_provider[key]):
                _fail("Shared dependency provider SHA is malformed")
        provider_rel, provider_root = _existing(
            root, dependency_provider["project_path"], directory=True
        )
        is_legacy_vibe_provider = any(
            "vibe" in segment.casefold() for segment in PurePosixPath(provider_rel).parts
        )
        expected_central_provider = f"Remotion-Shared/remotion-{version}"
        is_approved_central_provider = provider_rel.casefold() == expected_central_provider.casefold()
        if not (is_legacy_vibe_provider or is_approved_central_provider):
            _fail(
                "Shared engine must be a Vibe-named project or the exact "
                "MiniMax Remotion-Shared/remotion-<pinned version> directory"
            )
        if is_approved_central_provider:
            _, receipt = _existing(root, provider_rel + "/INSTALL-RECEIPT.json")
            if receipt.stat().st_size > 32768:
                _fail("Central Remotion engine receipt too large")
            checked_install = json.loads(receipt.read_text(encoding="utf-8"))
            if checked_install.get("status") != "INDEPENDENT_ENGINE_INSTALLED__NO_CONSUMERS_SWITCHED":
                _fail("Central Remotion engine has no successful installation receipt")
        if provider_root == directory:
            _fail("Shared dependency provider must be a separate preinstalled project")
        _, provider_pkg = _existing(root, provider_rel + "/package.json")
        _, provider_lock = _existing(root, provider_rel + "/package-lock.json")
        observed_pkg = _sha(provider_pkg)
        observed_lock = _sha(provider_lock)
        source_spec = json.loads(package.read_text(encoding="utf-8"))
        provider_spec = json.loads(provider_pkg.read_text(encoding="utf-8"))
        if not _obj(source_spec) or not _obj(provider_spec):
            _fail("Project or shared dependency package.json invalid")
        primary = ("remotion", "react", "react-dom", "@remotion/cli")
        source_deps = source_spec.get("dependencies", {})
        provider_deps = provider_spec.get("dependencies", {})
        if not _obj(source_deps) or not _obj(provider_deps):
            _fail("Package dependency mapping invalid")
        allow_inherited = meta.get("allow_engine_inherited_dependencies", False) is True
        expected_react = meta.get("expected_react_version", "19.2.0")
        if allow_inherited and (
            not isinstance(expected_react, str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_react)
        ):
            _fail("Inherited dependencies require a precise React version")
        versions = []
        for name in primary:
            declared = source_deps.get(name)
            if declared is None and allow_inherited:
                declared = version if name in ("remotion", "@remotion/cli") else expected_react
            versions.append({
                "package": name, "expected": declared, "provider": provider_deps.get(name),
                "inherited": name not in source_deps,
                "matches": isinstance(declared, str) and declared == provider_deps.get(name),
            })
        versions_ok = all(item["matches"] for item in versions)
        provider_hashes_match = (
            observed_pkg.casefold() == dependency_provider["package_sha256"].casefold()
            and observed_lock.casefold() == dependency_provider["package_lock_sha256"].casefold()
        )
        provider_audit = {
            "mode": "shared_read_only_project",
            "project_path": provider_rel,
            "package_sha256": observed_pkg,
            "package_lock_sha256": observed_lock,
            "hashes_match": provider_hashes_match,
            "core_package_versions": versions,
            "pass": provider_hashes_match and versions_ok,
        }
        runtime_package = provider_pkg
        runtime_root = provider_root
    return {
        "rel": rel,
        "dir": directory,
        "marker": marker,
        "meta": meta,
        "entry": entry,
        "package": package,
        "runtime_package": runtime_package,
        "runtime_root": runtime_root,
        "dependency_provider": provider_audit,
        "public": pub,
        "browser": browser,
        "entry_sha": actual_entry,
        "package_sha": actual_package,
        "source_pass": actual_entry.casefold() == meta["entry_sha256"].casefold()
        and actual_package.casefold() == meta["package_sha256"].casefold()
        and source_inputs_match
        and provider_audit["pass"],
        "source_audit": source_audit,
        "source_coverage": "PINNED_INPUT_MAP" if (extra_sources or public_input_checks) else "ENTRY_AND_PACKAGE_ONLY",
        "modules": modules,
        "allowed": operations,
        "version": version,
    }


def _node() -> str | None:
    found = shutil.which("node.exe") or shutil.which("node")
    if found:
        return found
    if os.name == "nt":
        for candidate in (
            Path("C:/Program Files/nodejs/node.exe"),
            Path("C:/Program Files (x86)/nodejs/node.exe"),
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _options(action: str, params: dict[str, Any]) -> dict[str, Any]:
    raw = params.get("options", {})
    if not _obj(raw):
        _fail("options must be a JSON object")
    unknown = set(raw) - ALLOWED_OPTS
    if unknown:
        _fail("Unknown Remotion options: " + ", ".join(sorted(unknown)))
    if len(json.dumps(raw, ensure_ascii=False)) > 220000:
        _fail("Options exceed 220 KiB")
    for key in ("input_props",):
        if key in raw and not _obj(raw[key]):
            _fail("input_props must be a JSON object")
    for key, maximum in (("frames", 6000), ("seconds", 6000), ("frame_ranges", 120)):
        if key in raw:
            if not isinstance(raw[key], list) or not 1 <= len(raw[key]) <= maximum:
                _fail(f"{key} must be a nonempty bounded array")
    for k in ("frame", "scale", "concurrency", "crf", "jpeg_quality", "timeout_ms",
              "noise_db", "min_silence_seconds", "gop_size", "every_nth_frame", "gif_loops"):
        if k in raw:
            value = raw[k]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not -100 <= value <= 600000:
                _fail(f"Option {k} must be a finite bounded number")
    if "frame" in raw and (raw["frame"] < 0 or int(raw["frame"]) != raw["frame"]):
        _fail("frame must be a non-negative integer")
    for k in ("frames", "seconds"):
        for value in raw.get(k, []):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 2000000:
                _fail(f"Invalid {k} value")
            if k == "frames" and int(value) != value:
                _fail("Frames must be integers")
    if "frames" in raw and "seconds" in raw:
        _fail("frames and seconds are mutually exclusive")
    if "frame_ranges" in raw:
        last = -1
        for rr in raw["frame_ranges"]:
            if not isinstance(rr, list) or len(rr) != 2 or not all(
                isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in rr
            ):
                _fail("frame_ranges entries must be [non-negative int start, end]")
            if rr[0] > rr[1] or rr[0] <= last:
                _fail("frame_ranges must be monotone, non-overlapping and closed")
            last = rr[1]
    if "codec" in raw and raw["codec"] not in CODECS:
        _fail("Unknown output codec")
    if "image_format" in raw and raw["image_format"] not in FORMATS:
        _fail("Unknown still format")
    if "composition_id" in raw and (not isinstance(raw["composition_id"], str) or
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", raw["composition_id"])):
        _fail("Invalid composition_id")
    for k in ("codec", "audio_codec", "prores_profile", "x264_preset", "hardware_acceleration",
              "pixel_format", "color_space", "log_level", "image_format"):
        if k in raw and not isinstance(raw[k], str):
            _fail(f"Invalid {k}")
    if "hardware_acceleration" in raw and raw["hardware_acceleration"] not in (
        "disable", "if-possible", "required"
    ):
        _fail("Unsupported hardware acceleration policy")
    if "log_level" in raw and raw["log_level"] not in ("warn", "error", "info", "verbose", "trace"):
        _fail("Unsupported log level")
    for k in ("muted", "enforce_audio_track"):
        if k in raw and not isinstance(raw[k], bool):
            _fail(f"{k} must be boolean")
    for k in ("audio_bitrate", "video_bitrate", "max_rate", "buffer_size"):
        if k in raw and (not isinstance(raw[k], str) or not re.fullmatch(r"[1-9][0-9]{0,8}[kKmM]?", raw[k])):
            _fail("Invalid bitrate")
    if action == "render-video" and raw.get("codec") not in CODECS:
        _fail("Explicit valid codec required for video/audio render")
    if action in ("render-still", "render-stills", "render-frames", "render-video"):
        if "composition_id" not in raw:
            _fail("composition_id is required")
    if action == "render-still" and ("frames" in raw or "seconds" in raw):
        _fail("render-still uses a single frame; use render-stills for batches")
    if action == "render-stills" and not ("frames" in raw or "seconds" in raw):
        _fail("render-stills requires frames or seconds")
    if action == "render-frames" and not ("frames" in raw or "seconds" in raw or "frame_ranges" in raw):
        _fail("render-frames must explicitly bound selected frames")
    return raw


def _exec_bridge(p: dict[str, Any], action: str, opts: dict[str, Any],
                 output: Path, media: Path | None, timeout: int) -> dict[str, Any]:
    node = _node()
    if not node:
        return {"status": "NODE_NOT_INSTALLED", "blockers": ["node.exe not found on this machine"]}
    if not ENTRY.is_file():
        _fail("Approved Remotion Node bridge is missing")
    payload = {
        "action": action, "project": str(p["dir"]), "entry": str(p["entry"]),
        "package": str(p["package"]),
        "runtime_package": str(p["runtime_package"]),
        "runtime_root": str(p["runtime_root"]),
        "public_dir": str(p["public"]) if p["public"] else None,
        "browser": str(p["browser"]) if p["browser"] else "__LOCAL_BROWSER_MISSING__",
        "version": p["version"], "required_modules": p["modules"],
        "output_dir": str(output), "media_input": str(media) if media else None, "options": opts,
    }
    env_keys = ("PATH", "Path", "PATHEXT", "SystemRoot", "SYSTEMROOT", "WINDIR",
                "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramFiles")
    env = {k: os.environ[k] for k in env_keys if k in os.environ}
    proc = subprocess.run(
        [node, str(ENTRY)], input=json.dumps(payload, ensure_ascii=False),
        capture_output=True, encoding="utf-8", errors="replace", cwd=p["dir"],
        env=env, shell=False, timeout=timeout, check=False,
    )
    marker = "FBR_RESULT_JSON="
    lines = [l[len(marker):] for l in proc.stdout.splitlines() if l.startswith(marker)]
    if not lines:
        _fail("Remotion Node bridge returned no valid response; exit " + str(proc.returncode)
              + "; stderr=" + (proc.stderr or "")[-1500:])
    try:
        response = json.loads(lines[-1])
    except ValueError as exc:
        raise RuntimeError("Remotion bridge response is malformed") from exc
    if not response.get("ok") or proc.returncode != 0:
        error = response.get("error", {})
        _fail("Remotion bridge error: " + str(error.get("message", "unclassified error"))[:1300])
    result = response.get("result")
    if not _obj(result):
        _fail("Bridge must return a JSON result object")
    return result


def _output(root: Path, p: dict[str, Any], job_id: str) -> Path:
    parent = p["dir"] / "remotion-toolkit-output"
    if parent.exists():
        _existing(root, p["rel"] + "/remotion-toolkit-output", directory=True)
    else:
        parent.mkdir()
    dest = parent / job_id
    if dest.exists() or dest.is_symlink():
        _fail("Output already exists; choose a new job_id; no overwrite permitted")
    dest.mkdir()
    return dest


def _write_receipt(target: Path, record: dict[str, Any]) -> Path:
    receipt = target / "receipt.json"
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return receipt


def _artifacts(root: Path, project: dict[str, Any], output: Path, result: dict[str, Any]) -> list[str]:
    paths = []
    for name in ("receipt.json", "progress.json", "render.mp4", "render.mov", "render.webm", "render.gif",
                 "render.mp3", "render.wav", "render.aac", "render.ts"):
        file = output / name
        if file.is_file():
            paths.append(file.relative_to(root).as_posix())
    # Bounded still references; no listing thousands of frames as artifacts
    for file in sorted(output.glob("still-*"))[:35]:
        if file.is_file():
            paths.append(file.relative_to(root).as_posix())
    bundle = output / "bundle" / "index.html"
    if bundle.is_file():
        paths.append(bundle.relative_to(root).as_posix())
    return paths[:64]


def _status(root: Path) -> dict[str, Any]:
    return {
        "ok": True, "plugin_version": VERSION, "workspace": str(root),
        "node_available": bool(_node()), "node_path": _node(),
        "remotion_version_policy": "exact 4.x project marker; all remotion packages must match",
        "shared_dependency_support": "same FolderBridge workspace, explicit SHA-locked provider project; no node_modules copying or junctions",
        "official_deprecated_mcp": "https://www.remotion.dev/docs/ai/mcp",
        "official_agent_skills": "https://www.remotion.dev/docs/ai/skills",
        "official_studio_webmcp": "https://www.remotion.dev/docs/ai/webmcp",
        "render_requires_project_profile": True, "publishing": "NOT_SUPPORTED",
        "external_plugin_approval": "Exact directory hash and permissions; GUI approval required",
    }


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace(context)
    if action == "status":
        return _status(root)
    if action == "verification-plan":
        return {
            "steps": [
                "project-inspect: source/entry SHA and public assets",
                "preflight: versions, Node, local Chromium (no downloads)",
                "compositions: inspect exact metadata/input props",
                "render-stills: representative real frames before lengthy rendering",
                "render-frames / render-video: specific codec, bounded frames/ranges",
                "media-probe and silence-scan: inspect actual output",
                "Review rendered frames, semantic correctness and user signoff separately",
            ],
            "studio_integration": "Studio WebMCP is a browser-agent integration, not the retired hosted Remotion MCP",
            "hosted_remotion_mcp": "Shut down 2026-10-26",
        }
    if not _obj(params):
        _fail("Expected params object")
    p = _project(root, params)
    base = {
        "project": p["rel"], "entry_sha256": p["entry_sha"],
        "package_sha256": p["package_sha"], "project_profile_sha256": _sha(p["marker"]),
        "source_integrity_pass": p["source_pass"], "remotion_version": p["version"],
        "source_input_coverage": p["source_coverage"],
        "source_input_checks": p["source_audit"][:200],
        "dependency_provider": p["dependency_provider"],
        "browser_declared_and_present": bool(p["browser"]),
        "required_modules": p["modules"], "allowed_operations": p["allowed"],
    }
    if action == "project-inspect":
        return {"ok": True, **base}
    if action == "studio-guide":
        return {
            "ok": True, **base, "started": False,
            "studio": "Open project in local Remotion Studio, then use WebMCP through a supporting browser agent (e.g. Codex)",
            "local_command": "npx --no-install remotion studio " + p["meta"]["entry"],
            "official_webmcp": "https://www.remotion.dev/docs/ai/webmcp",
            "agent_skills": "https://www.remotion.dev/docs/ai/skills",
            "warning": "This plugin deliberately does not launch a persistent Studio HTTP server or install packages",
        }
    if action not in RENDER_ACTIONS | {"preflight", "compositions", "media-probe", "silence-scan"}:
        _fail("Unsupported Remotion action")
    if action not in p["allowed"] and action not in ("preflight",):
        _fail("Action not allowed by project marker")
    opts = _options(action, params)
    if not p["source_pass"]:
        return {"ok": False, "status": "SOURCE_SHA_MISMATCH", **base}
    if action in RENDER_ACTIONS and context.get("workspace_read_only"):
        _fail("Workspace is read-only; rendering is not permitted")
    media = None
    if action in ("media-probe", "silence-scan"):
        _, media = _existing(root, params.get("media_path"))
        if media.suffix.casefold() not in {".mp4", ".mov", ".mkv", ".webm", ".wav", ".mp3",
                                            ".aac", ".m4a", ".ts", ".hevc", ".avi", ".gif"}:
            _fail("media_path must have a supported audio/video extension")
    # Node bundling for read-only actions writes only into extension private state.
    if action in RENDER_ACTIONS:
        job_id = params.get("job_id")
        if not isinstance(job_id, str) or not JOB.fullmatch(job_id):
            _fail("job_id must be a unique safe slug, 3..63 characters")
        output = _output(root, p, job_id)
    else:
        state = context.get("state_dir")
        if not isinstance(state, str) or not state:
            _fail("extension.state is required for private bundle output")
        output = Path(tempfile.mkdtemp(prefix="remotion-", dir=state))
    max_wait = int(params.get("timeout_seconds", 7200 if action in RENDER_ACTIONS else 600))
    if not 10 <= max_wait <= 86400:
        _fail("timeout_seconds must be 10..86400")
    record = {"schema": 1, "plugin": VERSION, "action": action, "project": p["rel"],
              "source": base, "time_utc": datetime.now(timezone.utc).isoformat()}
    try:
        result = _exec_bridge(p, action, opts, output, media, max_wait)
        record.update({"status": result.get("status"), "result": result})
        if action in RENDER_ACTIONS:
            receipt = _write_receipt(output, record)
            return {"ok": True, "status": result.get("status"), **base,
                    "result": result, "workspace_artifacts": _artifacts(root, p, output, result),
                    "receipt": receipt.relative_to(root).as_posix()}
        return {"ok": True, **base, "result": result}
    except BaseException as exc:
        record["status"] = "FAILED_NOT_ACCEPTED"
        record["error"] = str(exc)[:3000]
        if action in RENDER_ACTIONS:
            _write_receipt(output, record)
        raise
    finally:
        if action not in RENDER_ACTIONS:
            shutil.rmtree(output, ignore_errors=True)
