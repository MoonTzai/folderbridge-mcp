from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree
except ImportError:  # FolderBridge 0.8.21 compatibility before the public process-helper re-export.
    from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


MAX_ARGS = 512
MAX_ARG_CHARS = 8192
MAX_PATH_REFS = 128
MAX_CAPTURE_CHARS = 262_144
MAX_LOG_CHARS = 65_536
MAX_ARTIFACTS = 256
TOKEN_RE = re.compile(r"\{\{(?:(filter):)?([A-Za-z0-9_.-]{1,64})\}\}")
DRIVE_PATH_RE = re.compile(r"(?i)(?:^|[^A-Za-z0-9_])(?:[A-Za-z]:[\\/])")
UNC_RE = re.compile(r"(?:^|[^\\])\\\\")
PARENT_SEGMENT_RE = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
URL_SCHEME_RE = re.compile(r"(?i)(?:^|[=,;\s])(?:https?|ftp|ftps|tcp|udp|rtmp|rtmps|rtsp|srt|ssh|smb|gopher|file|pipe|concat|subfile|cache|crypto|data):")
DANGEROUS_FILTER_RE = re.compile(r"(?i)(?:^|[,;\[])(?:a?zmq|frei0r|ladspa|lv2|vst)(?:=|[,;\]]|$)")
PATTERN_RE = re.compile(r"%[-+0#]*\d*[A-Za-z]")
PATH_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DEVICE_NAMES_RE = re.compile(r"(?i)^(?:NUL|CON|PRN|AUX|COM[1-9]|LPT[1-9])(?::.*)?$")
DEVICE_FORMATS = {
    "alsa", "avfoundation", "decklink", "dshow", "fbdev", "gdigrab", "jack",
    "kmsgrab", "openal", "oss", "pulse", "sndio", "v4l2", "video4linux2", "x11grab",
}
BANNED_OPTIONS = {
    "-protocol_whitelist",
    "-protocol_blacklist",
    "-filter_script",
    "-filter_complex_script",
    "-report",
    "-y",
    "-n",
}
DENIED_PARTS = {".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__"}
SENSITIVE_BASENAMES = {
    ".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", "credentials.json",
}
SENSITIVE_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
WORKSPACE_TOOL_DIRS = ("GPT-SoVITS/runtime", "tools/ffmpeg/bin", "ffmpeg/bin")
CAPABILITY_FLAGS = {
    "encoders": "-encoders",
    "decoders": "-decoders",
    "codecs": "-codecs",
    "formats": "-formats",
    "muxers": "-muxers",
    "demuxers": "-demuxers",
    "filters": "-filters",
    "protocols": "-protocols",
    "hwaccels": "-hwaccels",
    "devices": "-devices",
    "bsfs": "-bsfs",
    "pix_fmts": "-pix_fmts",
    "sample_fmts": "-sample_fmts",
    "layouts": "-layouts",
    "colors": "-colors",
}


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    if action == "status":
        return _status(root, params.get("tool_dir"))
    if action == "capabilities":
        return _capabilities(root, params)
    if action == "probe":
        return _probe(root, params, context)
    if action == "run":
        if bool(context.get("workspace_read_only")):
            raise RuntimeError("FolderBridge is read-only; FFmpeg run may write workspace outputs.")
        return _run_ffmpeg(root, params, context)
    raise RuntimeError(f"unsupported action: {action}")


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _is_reparse(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


def _reject_links(root: Path, candidate: Path) -> None:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError as exc:
        raise RuntimeError("path escapes workspace") from exc
    current = root
    for part in parts:
        current = current / part
        if not current.exists() and not current.is_symlink():
            break
        if current.is_symlink() or _is_reparse(current):
            raise RuntimeError(f"linked/reparse path component is not allowed: {part}")


def _clean_relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise RuntimeError("paths must be non-empty POSIX-style workspace-relative strings")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise RuntimeError("path must stay inside the selected workspace")
    lowered = [part.lower() for part in rel.parts]
    if any(part in DENIED_PARTS for part in lowered):
        raise RuntimeError("path targets a denied dependency/VCS directory")
    base = lowered[-1]
    suffix = PurePosixPath(base).suffix.lower()
    if base in SENSITIVE_BASENAMES or suffix in SENSITIVE_SUFFIXES:
        raise RuntimeError("credential/key-like paths are not allowed")
    return rel


def _resolve_existing(root: Path, raw: str, *, directory: bool = False) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("input path must exist inside the selected workspace") from exc
    if resolved.is_symlink() or _is_reparse(resolved):
        raise RuntimeError("linked/reparse inputs are not allowed")
    if directory:
        if not resolved.is_dir():
            raise RuntimeError("input_dir must be a directory")
    elif not resolved.is_file():
        raise RuntimeError("input must be a regular file")
    return resolved


def _pattern_parent(root: Path, raw: str, *, create: bool) -> tuple[Path, PurePosixPath]:
    rel = _clean_relative(raw)
    if len(rel.parts) < 1:
        raise RuntimeError("pattern path is invalid")
    if not (PATTERN_RE.search(rel.name) or any(ch in rel.name for ch in "*?[]")):
        raise RuntimeError("pattern path must contain an FFmpeg % pattern or glob metacharacter")
    if any(PATTERN_RE.search(part) or any(ch in part for ch in "*?[]") for part in rel.parts[:-1]):
        raise RuntimeError("pattern metacharacters are allowed only in the final path component")
    parent_rel = PurePosixPath(*rel.parts[:-1]) if len(rel.parts) > 1 else PurePosixPath(".")
    parent = root if str(parent_rel) == "." else root.joinpath(*parent_rel.parts)
    _reject_links(root, parent)
    if create:
        parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("pattern parent must stay inside the selected workspace") from exc
    if not resolved_parent.is_dir() or resolved_parent.is_symlink() or _is_reparse(resolved_parent):
        raise RuntimeError("pattern parent must be a regular directory")
    return resolved_parent, rel


def _resolve_output(root: Path, raw: str, *, directory: bool, create_parents: bool, overwrite: bool) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    parent = candidate.parent
    _reject_links(root, parent)
    if create_parents:
        parent.mkdir(parents=True, exist_ok=True)
    try:
        resolved_parent = parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("output parent must exist inside the selected workspace") from exc
    if candidate.exists() or candidate.is_symlink():
        _reject_links(root, candidate)
        if candidate.is_symlink() or _is_reparse(candidate):
            raise RuntimeError("linked/reparse outputs are not allowed")
        if directory:
            if not candidate.is_dir():
                raise RuntimeError("output_dir must be a directory")
        elif not candidate.is_file():
            raise RuntimeError("output must be a regular file")
        if not overwrite and not directory:
            raise RuntimeError("output already exists; set overwrite=true to replace it")
    elif directory:
        candidate.mkdir(parents=create_parents, exist_ok=False)
    return candidate


def _resolve_path_specs(root: Path, raw_specs: Any, *, create_parents: bool, overwrite: bool, read_only: bool) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    if raw_specs is None:
        return {}, {}
    if not isinstance(raw_specs, dict) or len(raw_specs) > MAX_PATH_REFS:
        raise RuntimeError(f"paths must be an object with at most {MAX_PATH_REFS} entries")
    values: dict[str, str] = {}
    normalized: dict[str, dict[str, Any]] = {}
    for name, spec in raw_specs.items():
        if not isinstance(name, str) or not PATH_NAME_RE.fullmatch(name):
            raise RuntimeError("path reference names must match [A-Za-z0-9_.-]{1,64}")
        if not isinstance(spec, dict) or set(spec) != {"path", "mode"}:
            raise RuntimeError(f"path reference {name!r} must contain exactly path and mode")
        raw_path = spec.get("path")
        mode = spec.get("mode")
        if not isinstance(raw_path, str) or not isinstance(mode, str):
            raise RuntimeError(f"path reference {name!r} has invalid path/mode")
        if read_only and mode.startswith("output"):
            raise RuntimeError("read-only probe paths may not declare outputs")
        if mode == "input":
            resolved = _resolve_existing(root, raw_path)
            value = str(resolved)
        elif mode == "input_dir":
            resolved = _resolve_existing(root, raw_path, directory=True)
            value = str(resolved)
        elif mode == "input_pattern":
            parent, rel = _pattern_parent(root, raw_path, create=False)
            value = str(parent / rel.name)
        elif mode == "output":
            resolved = _resolve_output(root, raw_path, directory=False, create_parents=create_parents, overwrite=overwrite)
            value = str(resolved)
        elif mode == "output_dir":
            resolved = _resolve_output(root, raw_path, directory=True, create_parents=create_parents, overwrite=True)
            value = str(resolved)
        elif mode == "output_pattern":
            parent, rel = _pattern_parent(root, raw_path, create=create_parents)
            existing = list(parent.glob(_pattern_glob(rel.name)))
            if existing and not overwrite:
                raise RuntimeError(f"output pattern {name!r} already matches existing files; set overwrite=true")
            value = str(parent / rel.name)
        else:
            raise RuntimeError(f"unsupported path mode for {name!r}: {mode}")
        values[name] = value
        normalized[name] = {"path": _clean_relative(raw_path).as_posix(), "mode": mode}
    return values, normalized


def _pattern_glob(name: str) -> str:
    return PATTERN_RE.sub("*", name)


def _filter_escape_path(value: str) -> str:
    text = value.replace("\\", "/")
    for source, replacement in (("\\", "\\\\"), (":", "\\:"), ("'", "\\'"), (",", "\\,"), (";", "\\;"), ("[", "\\["), ("]", "\\]")):
        text = text.replace(source, replacement)
    return text


def _validate_raw_args(raw_args: Any) -> list[str]:
    if not isinstance(raw_args, list) or len(raw_args) > MAX_ARGS:
        raise RuntimeError(f"args must be an array with at most {MAX_ARGS} strings")
    args: list[str] = []
    for raw in raw_args:
        if not isinstance(raw, str) or not raw or len(raw) > MAX_ARG_CHARS or "\x00" in raw:
            raise RuntimeError("every FFmpeg argument must be a non-empty bounded string")
        scrubbed = TOKEN_RE.sub("", raw).replace("{{null}}", "")
        policy_text = scrubbed.replace("\\:", ":").replace("\\/", "/")
        if raw in BANNED_OPTIONS:
            raise RuntimeError(f"FFmpeg option is managed or denied by the plugin: {raw}")
        if scrubbed == "-" or DEVICE_NAMES_RE.fullmatch(scrubbed):
            raise RuntimeError("stdin/stdout and Windows device paths are not allowed; use declared workspace paths or {{null}}")
        if DRIVE_PATH_RE.search(policy_text) or UNC_RE.search(policy_text) or PARENT_SEGMENT_RE.search(policy_text):
            raise RuntimeError("raw arguments may not contain filesystem paths; declare paths and use placeholders")
        if policy_text.startswith("/") or URL_SCHEME_RE.search(policy_text):
            raise RuntimeError("raw arguments may not contain absolute paths, URLs, or explicit protocols")
        if DANGEROUS_FILTER_RE.search(scrubbed):
            raise RuntimeError("network/plugin-loading filters are not allowed")
        args.append(raw)
    for index, arg in enumerate(args[:-1]):
        if arg == "-f" and args[index + 1].lower() in DEVICE_FORMATS:
            raise RuntimeError(f"device capture format is not allowed: {args[index + 1]}")
    return args


def _expand_args(args: list[str], path_values: dict[str, str]) -> tuple[list[str], set[str]]:
    used: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        filter_mode, name = match.groups()
        if name not in path_values:
            raise RuntimeError(f"unknown path placeholder: {name}")
        used.add(name)
        value = path_values[name]
        return _filter_escape_path(value) if filter_mode else value

    expanded: list[str] = []
    for raw in args:
        if "{{null}}" in raw:
            raw = raw.replace("{{null}}", "NUL" if os.name == "nt" else "/dev/null")
        value = TOKEN_RE.sub(replace, raw)
        if "{{" in value or "}}" in value:
            raise RuntimeError("invalid or unsupported placeholder syntax")
        expanded.append(value)
    return expanded, used


def _resolve_tool_dir(root: Path, raw: str) -> Path:
    rel = _clean_relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links(root, candidate)
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("tool_dir must exist inside the selected workspace") from exc
    if not resolved.is_dir() or resolved.is_symlink() or _is_reparse(resolved):
        raise RuntimeError("tool_dir must be a regular non-link directory")
    return resolved


def _fixed_tool_pair(directory: Path) -> tuple[Path, Path] | None:
    ffmpeg = directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    ffprobe = directory / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if all(path.is_file() and not path.is_symlink() and not _is_reparse(path) for path in (ffmpeg, ffprobe)):
        return ffmpeg, ffprobe
    return None


def _resolve_tools(root: Path, tool_dir: str | None, *, required: bool) -> tuple[Path | None, Path | None, str | None]:
    if tool_dir is not None:
        directory = _resolve_tool_dir(root, tool_dir)
        pair = _fixed_tool_pair(directory)
        if pair is None:
            if required:
                raise RuntimeError("tool_dir must contain regular ffmpeg and ffprobe executables")
            return None, None, f"workspace:{_clean_relative(tool_dir).as_posix()}"
        return pair[0], pair[1], f"workspace:{_clean_relative(tool_dir).as_posix()}"

    ffmpeg_raw = shutil.which("ffmpeg.exe") or shutil.which("ffmpeg")
    ffprobe_raw = shutil.which("ffprobe.exe") or shutil.which("ffprobe")
    if ffmpeg_raw and ffprobe_raw:
        ffmpeg = Path(ffmpeg_raw)
        ffprobe = Path(ffprobe_raw)
        if ffmpeg.is_file() and ffprobe.is_file():
            return ffmpeg, ffprobe, "trusted-path"

    for candidate in WORKSPACE_TOOL_DIRS:
        try:
            directory = _resolve_tool_dir(root, candidate)
        except RuntimeError:
            continue
        pair = _fixed_tool_pair(directory)
        if pair is not None:
            return pair[0], pair[1], f"workspace:{candidate}"
    if required:
        raise RuntimeError("ffmpeg/ffprobe were not found on the trusted PATH or in a supported workspace tool directory; pass tool_dir containing fixed ffmpeg/ffprobe filenames")
    return None, None, None


def _bounded_text(data: bytes, limit: int) -> tuple[str, bool]:
    text = data.decode("utf-8-sig", errors="replace")
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _tail_file(path: Path, max_chars: int) -> tuple[str, bool]:
    max_bytes = max(4096, min(max_chars * 4, 4 * 1024 * 1024))
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(-max_bytes, os.SEEK_END)
            data = handle.read(max_bytes)
    except OSError:
        return "", False
    text = data.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[-max_chars:], True
    return text, size > len(data)


def _short_command(tool: Path, args: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            [str(tool), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            timeout=timeout,
            check=False,
            close_fds=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not execute {tool.name}: {exc}") from exc
    stdout, _ = _bounded_text(completed.stdout, MAX_CAPTURE_CHARS)
    stderr, _ = _bounded_text(completed.stderr, MAX_CAPTURE_CHARS)
    return completed.returncode, stdout, stderr


def _tool_metadata(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    code, stdout, stderr = _short_command(path, ["-version"], timeout=15)
    text = stdout.strip() or stderr.strip()
    first = text.splitlines()[0] if text else ""
    return {"path": str(path), "ready": code == 0, "version": first[:1000], "exit_code": code}


def _status(root: Path, tool_dir: str | None) -> dict[str, Any]:
    ffmpeg, ffprobe, source = _resolve_tools(root, tool_dir, required=False)
    return {
        "ready": bool(ffmpeg and ffprobe),
        "source": source,
        "ffmpeg": _tool_metadata(ffmpeg),
        "ffprobe": _tool_metadata(ffprobe),
        "workspace_candidates": list(WORKSPACE_TOOL_DIRS),
        "policy": {
            "workspace_paths_only": True,
            "shell": False,
            "network_protocols": False,
            "device_capture": False,
            "stdio_media": False,
            "protocol_whitelist": ["file", "crypto", "data"],
        },
    }


def _capabilities(root: Path, params: dict[str, Any]) -> dict[str, Any]:
    kind = params["kind"]
    if kind not in CAPABILITY_FLAGS:
        raise RuntimeError(f"unsupported capability kind: {kind}")
    ffmpeg, _ffprobe, source = _resolve_tools(root, params.get("tool_dir"), required=True)
    assert ffmpeg is not None
    code, stdout, stderr = _short_command(ffmpeg, ["-hide_banner", CAPABILITY_FLAGS[kind]], timeout=30)
    text = stdout if stdout.strip() else stderr
    lines = text.splitlines()
    contains = params.get("contains")
    if isinstance(contains, str) and contains:
        needle = contains.casefold()
        lines = [line for line in lines if needle in line.casefold()]
    total = len(lines)
    max_items = int(params.get("max_items", 500))
    return {
        "kind": kind,
        "source": source,
        "exit_code": code,
        "total_matching_lines": total,
        "truncated": total > max_items,
        "lines": lines[:max_items],
    }


def _state_temp_dir(context: dict[str, Any], prefix: str) -> Path:
    raw = context.get("state_dir")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("extension state_dir is unavailable")
    state_dir = Path(raw).resolve(strict=True)
    if not state_dir.is_dir():
        raise RuntimeError("extension state_dir is not a directory")
    return Path(tempfile.mkdtemp(prefix=prefix, dir=state_dir))


def _execute_long(argv: list[str], context: dict[str, Any], *, timeout_seconds: int, prefix: str) -> tuple[int, str, str, bool, bool, float]:
    temp_dir = _state_temp_dir(context, prefix)
    stdout_path = temp_dir / "stdout.log"
    stderr_path = temp_dir / "stderr.log"
    started = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=temp_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    close_fds=True,
                    **owned_process_group_kwargs(hide_window=True),
                )
            except OSError as exc:
                raise RuntimeError(f"could not start {Path(argv[0]).name}: {exc}") from exc
            try:
                process.wait(timeout=None if timeout_seconds == 0 else timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                terminate_owned_process_tree(process, hide_window=True)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise RuntimeError(f"{Path(argv[0]).name} exceeded {timeout_seconds} seconds") from exc
        stdout, stdout_truncated = _tail_file(stdout_path, MAX_CAPTURE_CHARS)
        stderr, stderr_truncated = _tail_file(stderr_path, MAX_LOG_CHARS)
        elapsed = time.monotonic() - started
        return process.returncode if process is not None else 1, stdout, stderr, stdout_truncated, stderr_truncated, elapsed
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _prepare_invocation(root: Path, params: dict[str, Any], *, read_only: bool) -> tuple[list[str], dict[str, dict[str, Any]], set[str]]:
    raw_args = _validate_raw_args(params.get("args", []))
    overwrite = bool(params.get("overwrite", False))
    create_parents = bool(params.get("create_output_parents", True))
    values, normalized = _resolve_path_specs(
        root,
        params.get("paths"),
        create_parents=create_parents,
        overwrite=overwrite,
        read_only=read_only,
    )
    expanded, used = _expand_args(raw_args, values)
    unused = set(values) - used
    if unused:
        raise RuntimeError("declared path references were not used in args: " + ", ".join(sorted(unused)))
    return expanded, normalized, used


def _prepare_probe_params(params: dict[str, Any]) -> dict[str, Any]:
    effective = dict(params)
    raw_args = list(params.get("args") or [])
    path = params.get("path")
    if path is not None:
        paths = dict(params.get("paths") or {})
        if "source" in paths:
            raise RuntimeError("probe.path conflicts with paths.source; use either the convenience path or the explicit paths.source entry")
        paths["source"] = {"path": path, "mode": "input"}
        effective["paths"] = paths
        if not raw_args:
            raw_args = [
                "-v", "error",
                "-show_format", "-show_streams", "-show_chapters", "-show_programs",
                "-of", "json", "{{source}}",
            ]
        elif not any("{{source}}" in arg for arg in raw_args):
            raw_args.append("{{source}}")
        effective["args"] = raw_args
    if not raw_args:
        raise RuntimeError("probe requires path or args")
    return effective


def _build_ffprobe_argv(ffprobe: Path, expanded: list[str]) -> list[str]:
    return [
        str(ffprobe),
        "-hide_banner",
        "-protocol_whitelist", "file,crypto,data",
        *expanded,
    ]


def _build_ffmpeg_argv(ffmpeg: Path, expanded: list[str], *, overwrite: bool) -> list[str]:
    return [
        str(ffmpeg),
        "-hide_banner",
        "-nostdin",
        "-protocol_whitelist", "file,crypto,data",
        "-y" if overwrite else "-n",
        *expanded,
    ]


def _probe(root: Path, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    _ffmpeg, ffprobe, source = _resolve_tools(root, params.get("tool_dir"), required=True)
    assert ffprobe is not None
    effective = _prepare_probe_params(params)
    expanded, normalized, _used = _prepare_invocation(root, effective, read_only=True)
    argv = _build_ffprobe_argv(ffprobe, expanded)
    timeout_seconds = int(params.get("timeout_seconds", 120))
    code, stdout, stderr, stdout_truncated, stderr_truncated, elapsed = _execute_long(
        argv, context, timeout_seconds=timeout_seconds, prefix="ffprobe-"
    )
    max_output_chars = int(params.get("max_output_chars", 131072))
    if len(stdout) > max_output_chars:
        stdout = stdout[:max_output_chars]
        stdout_truncated = True
    result: dict[str, Any] = {
        "tool": "ffprobe",
        "source": source,
        "exit_code": code,
        "elapsed_seconds": round(elapsed, 3),
        "stdout": stdout,
        "stdout_truncated": stdout_truncated,
        "stderr": stderr,
        "stderr_truncated": stderr_truncated,
        "paths": normalized,
    }
    if code == 0 and not stdout_truncated:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            result["json"] = parsed
    if code != 0:
        detail = (stderr or stdout or f"exit code {code}")[-8000:]
        raise RuntimeError(f"ffprobe failed: {detail}")
    return result


def _artifact_paths(root: Path, normalized: dict[str, dict[str, Any]], max_artifacts: int) -> tuple[list[str], bool]:
    artifacts: list[str] = []
    for spec in normalized.values():
        mode = spec["mode"]
        raw = spec["path"]
        if mode == "output":
            candidate = root.joinpath(*PurePosixPath(raw).parts)
            if candidate.is_file() and not candidate.is_symlink() and not _is_reparse(candidate):
                artifacts.append(PurePosixPath(raw).as_posix())
        elif mode == "output_pattern":
            rel = PurePosixPath(raw)
            parent = root.joinpath(*rel.parts[:-1]) if len(rel.parts) > 1 else root
            for candidate in sorted(parent.glob(_pattern_glob(rel.name))):
                if candidate.is_file() and not candidate.is_symlink() and not _is_reparse(candidate):
                    artifacts.append(candidate.relative_to(root).as_posix())
                    if len(artifacts) > max_artifacts:
                        return artifacts[:max_artifacts], True
        elif mode == "output_dir":
            directory = root.joinpath(*PurePosixPath(raw).parts)
            if directory.is_dir():
                for candidate in sorted(directory.rglob("*")):
                    if candidate.is_file() and not candidate.is_symlink() and not _is_reparse(candidate):
                        artifacts.append(candidate.relative_to(root).as_posix())
                        if len(artifacts) > max_artifacts:
                            return artifacts[:max_artifacts], True
        if len(artifacts) > max_artifacts:
            return artifacts[:max_artifacts], True
    return artifacts[:max_artifacts], len(artifacts) > max_artifacts


def _run_ffmpeg(root: Path, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    ffmpeg, _ffprobe, source = _resolve_tools(root, params.get("tool_dir"), required=True)
    assert ffmpeg is not None
    expanded, normalized, _used = _prepare_invocation(root, params, read_only=False)
    overwrite = bool(params.get("overwrite", False))
    argv = _build_ffmpeg_argv(ffmpeg, expanded, overwrite=overwrite)
    timeout_seconds = int(params.get("timeout_seconds", 7200))
    code, stdout, stderr, stdout_truncated, stderr_truncated, elapsed = _execute_long(
        argv, context, timeout_seconds=timeout_seconds, prefix="ffmpeg-"
    )
    if code != 0:
        detail = (stderr or stdout or f"exit code {code}")[-8000:]
        raise RuntimeError(f"ffmpeg failed: {detail}")
    max_artifacts = int(params.get("max_artifacts", 100))
    artifacts, artifacts_truncated = _artifact_paths(root, normalized, max_artifacts)
    return {
        "tool": "ffmpeg",
        "source": source,
        "exit_code": code,
        "elapsed_seconds": round(elapsed, 3),
        "stdout_tail": stdout[-MAX_LOG_CHARS:],
        "stdout_truncated": stdout_truncated or len(stdout) > MAX_LOG_CHARS,
        "stderr_tail": stderr,
        "stderr_truncated": stderr_truncated,
        "paths": normalized,
        "artifacts_truncated": artifacts_truncated,
        "workspace_artifacts": artifacts,
    }
