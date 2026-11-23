"""Generic, hot-loadable FolderBridge offline ASR extension.

No project-specific paths, commands, model downloads or user-supplied scripts.
Profiles are installed as exact-hash-approved local configuration data.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree

PROFILE_FILE = Path(__file__).with_name("profiles.json")
WORKER_FILE = Path(__file__).with_name("asr_worker.py")
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
ENGINES = {"funasr", "faster-whisper"}
LANGUAGE_RE = re.compile(r"^(auto|[a-z]{2,3}(?:-[a-z0-9]{2,8}){0,2})$")
DENIED_PARTS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".folderbridge"}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "credentials.json", "id_rsa", "id_ed25519"}
SENSITIVE_SUFFIXES = {".key", ".pem", ".pfx", ".p12"}
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    dig = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(2 * 1024 * 1024), b""):
            dig.update(block)
    return dig.hexdigest()


def _atomic_json(path: Path, obj: dict[str, Any]) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as fp:
        json.dump(obj, fp, ensure_ascii=False, indent=2, allow_nan=False)
        fp.write("\n")
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(temp, path)


def _is_link(path: Path) -> bool:
    try:
        stat_result = path.lstat()
        return path.is_symlink() or bool(getattr(stat_result, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def _reject_links_from_root(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    current = root
    for component in relative.parts:
        current = current / component
        if _is_link(current):
            raise ValueError("Linked/reparse workspace path denied")


def _relative(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Expected a nonempty POSIX workspace-relative path")
    rel = PurePosixPath(raw)
    if rel.is_absolute() or str(rel) in {".", ".."} or ".." in rel.parts or raw.startswith("./"):
        raise ValueError("Workspace path must remain relative and cannot contain ..")
    if any(":" in p or p.lower() in DENIED_PARTS or p.lower() in SENSITIVE_NAMES for p in rel.parts):
        raise ValueError("Workspace path contains a denied component")
    if rel.suffix.lower() in SENSITIVE_SUFFIXES:
        raise ValueError("Key or credential-like path denied")
    return rel


def _root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise ValueError("A selected workspace is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Selected workspace is not a directory")
    return root


def _input(root: Path, raw: str, max_bytes: int) -> tuple[Path, str]:
    rel = _relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links_from_root(root, candidate)
    path = candidate.resolve(strict=True)
    path.relative_to(root)
    if not path.is_file() or _is_link(path) or path.suffix.lower() not in AUDIO_EXTS:
        raise ValueError("Unsupported input: expected a regular local audio file")
    if path.stat().st_size > max_bytes or path.stat().st_size < 100:
        raise ValueError("Audio file is empty, invalid, or too large")
    return path, _sha256(path)


def _output(root: Path, raw: str) -> tuple[Path, PurePosixPath]:
    rel = _relative(raw)
    candidate = root.joinpath(*rel.parts)
    _reject_links_from_root(root, candidate)
    candidate.mkdir(parents=True, exist_ok=True)
    _reject_links_from_root(root, candidate)
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_dir() or _is_link(resolved):
        raise ValueError("Output must be a real workspace directory")
    return resolved, rel


def _absolute_dir(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("Profile model path is missing")
    path = Path(raw)
    if not path.is_absolute() or not path.is_dir() or _is_link(path):
        raise ValueError("Profile requires a local, regular absolute model directory")
    return path.resolve(strict=True)


def _validate_profile(obj: dict[str, Any]) -> None:
    if not isinstance(obj, dict):
        raise ValueError("Profile must be an object")
    if not PROFILE_ID_RE.fullmatch(str(obj.get("id", ""))):
        raise ValueError("Invalid profile id")
    if obj.get("engine") not in ENGINES:
        raise ValueError("Unsupported ASR engine")
    if not LANGUAGE_RE.fullmatch(str(obj.get("language", ""))):
        raise ValueError("Invalid model language")
    raw_python = obj.get("python_exe")
    if not isinstance(raw_python, str) or not Path(raw_python).is_absolute() or Path(raw_python).name.lower() != "python.exe":
        raise ValueError("Python interpreter must be an approved absolute python.exe")
    if not SHA_RE.fullmatch(str(obj.get("python_sha256", ""))):
        raise ValueError("Python executable SHA is required")
    model = obj.get("model")
    if not isinstance(model, dict) or not model.get("directory"):
        raise ValueError("Primary model directory required")
    for part in (obj.get("model"), obj.get("vad"), obj.get("punctuation")):
        if part is None:
            continue
        if not isinstance(part, dict) or not isinstance(part.get("directory"), str):
            raise ValueError("Model entry must have a directory")
        files = part.get("files")
        if not isinstance(files, dict) or not 1 <= len(files) <= 32:
            raise ValueError("Model entry must pin 1..32 assets")
        for file_name, digest in files.items():
            if not isinstance(file_name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", file_name):
                raise ValueError("Unsafe asset filename in model profile")
            if not SHA_RE.fullmatch(str(digest)):
                raise ValueError("Missing SHA-256 for model asset")
        if "model.pt" not in files and "model.bin" not in files:
            raise ValueError("Model primary weight must be pinned")
    if obj["engine"] == "funasr" and "model.pt" not in model["files"]:
        raise ValueError("FunASR model.pt required")
    if obj["engine"] == "faster-whisper" and (obj.get("vad") or obj.get("punctuation")):
        raise ValueError("Faster-whisper cannot receive FunASR auxiliaries")


def _load_profiles() -> tuple[list[dict[str, Any]], str | None]:
    if not PROFILE_FILE.exists():
        return [], None
    if PROFILE_FILE.stat().st_size > 128 * 1024:
        raise ValueError("profiles.json too large")
    profiles = json.loads(PROFILE_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(profiles, dict) or profiles.get("schema") != "offline-asr-profiles-v1":
        raise ValueError("Unsupported profiles.json schema")
    values = profiles.get("profiles")
    if not isinstance(values, list) or len(values) > 16:
        raise ValueError("Expected at most 16 profiles")
    ids = set()
    for profile in values:
        _validate_profile(profile)
        if profile["id"] in ids:
            raise ValueError("Duplicate profile id")
        ids.add(profile["id"])
    return values, _sha256(PROFILE_FILE)


def _verify_profile(profile: dict[str, Any], *, verify_hashes: bool) -> dict[str, Any]:
    python = Path(profile["python_exe"])
    if not python.is_file() or _is_link(python):
        raise ValueError("Pinned Python executable not found or linked")
    python_hash = _sha256(python) if verify_hashes else None
    if verify_hashes and python_hash != profile["python_sha256"]:
        raise ValueError("Pinned Python executable SHA mismatch")
    records: list[dict[str, Any]] = []
    for role in ("model", "vad", "punctuation"):
        part = profile.get(role)
        if part is None:
            continue
        directory = _absolute_dir(part["directory"])
        for filename, expected in part["files"].items():
            candidate = directory / filename
            if not candidate.is_file() or _is_link(candidate):
                raise ValueError("Missing or linked model asset: " + role + "/" + filename)
            actual = _sha256(candidate) if verify_hashes else None
            if verify_hashes and actual != expected:
                raise ValueError("Model SHA mismatch: " + role + "/" + filename)
            records.append({"role": role, "file": filename, "size": candidate.stat().st_size, "expected_sha256": expected,
                            "verified_sha256": actual})
    return {"python_sha256": python_hash, "assets": records, "asset_hashes_verified": verify_hashes}


def _sanitized_stem(stem: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:50].strip("._-")
    return slug or "audio"


def _env_for(output: Path) -> dict[str, str]:
    cache = output / "private-cache"
    tmp = output / "tmp"
    for path in (cache, tmp, cache / "hf", cache / "torch", cache / "modelscope"):
        path.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()  # FolderBridge already provides a cleaned worker environment.
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8", "PYTHONPYCACHEPREFIX": str(cache / "pycache"),
        "HF_HOME": str(cache / "hf"), "HUGGINGFACE_HUB_CACHE": str(cache / "hf"),
        "HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "MODELSCOPE_CACHE": str(cache / "modelscope"),
        "MODELSCOPE_OFFLINE": "1", "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache), "MPLCONFIGDIR": str(cache / "matplotlib"),
        "TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp),
        "HOME": str(cache), "USERPROFILE": str(cache), "DO_NOT_TRACK": "1",
    })
    return env


def _transcribe(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if context.get("workspace_read_only"):
        raise RuntimeError("Selected FolderBridge workspace is read-only")
    root = _root(context)
    profiles, config_sha = _load_profiles()
    selected = next((p for p in profiles if p["id"] == params["profile_id"]), None)
    if not selected:
        raise ValueError("Unknown or unconfigured profile_id")
    input_path, input_sha = _input(root, params["input_path"], int(params.get("max_audio_bytes", 536870912)))
    profile_receipt = _verify_profile(selected, verify_hashes=True)
    language = params.get("language", "auto")
    if not LANGUAGE_RE.fullmatch(language):
        raise ValueError("Invalid language identifier")
    if language == "auto":
        language = selected["language"]
    if selected["engine"] == "funasr" and language != selected["language"]:
        raise ValueError("This FunASR profile has a fixed language; select auto or its configured language")
    if not WORKER_FILE.is_file() or _is_link(WORKER_FILE):
        raise RuntimeError("Extension ASR worker is missing")
    output_dir, output_rel = _output(root, params["output_dir"])
    if shutil.disk_usage(output_dir).free < 128 * 1024 * 1024:
        raise RuntimeError("Insufficient disk free space (<128 MiB)")
    run_name = _sanitized_stem(input_path.stem) + "-" + input_sha[:12] + "-" + uuid.uuid4().hex[:12]
    run_dir = output_dir / run_name
    run_dir.mkdir(exist_ok=False)
    paths = {
        "result": run_dir / "asr-result.json",
        "transcript": run_dir / "transcript.txt",
        "log": run_dir / "worker.log",
        "receipt": run_dir / "receipt.json",
        "spec": run_dir / "job.json",
    }
    device = params.get("device", "auto")
    spec = {"engine": selected["engine"], "input_file": str(input_path),
            "model_dir": selected["model"]["directory"],
            "vad_dir": selected.get("vad", {}).get("directory"),
            "punctuation_dir": selected.get("punctuation", {}).get("directory"),
            "language": language, "device": device, "result_file": str(paths["result"])}
    _atomic_json(paths["spec"], spec)
    started = _now()
    pid = None
    exit_code = None
    timed_out = False
    with paths["log"].open("wb") as log:
        proc = subprocess.Popen(
            [selected["python_exe"], "-B", "-I", str(WORKER_FILE), str(paths["spec"])],
            cwd=str(run_dir), env=_env_for(run_dir), shell=False,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
        pid = proc.pid
        try:
            exit_code = proc.wait(timeout=6900)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_owned_process_tree(proc, hide_window=True)
            exit_code = proc.poll()
    finished = _now()
    error = None
    try:
        post_profile = _verify_profile(selected, verify_hashes=True)
        if post_profile != profile_receipt:
            error = "Profile source changed while ASR was running"
        if _sha256(input_path) != input_sha:
            error = "Input audio changed while ASR was running"
    except Exception as exc:
        error = "Post-run provenance check failed: " + str(exc)
    worker = {}
    if paths["result"].exists() and paths["result"].stat().st_size < 10 * 1024 * 1024:
        try:
            worker = json.loads(paths["result"].read_text(encoding="utf-8"))
        except Exception as exc:
            error = error or ("Invalid ASR result: " + str(exc))
    text = worker.get("text", "") if isinstance(worker, dict) else ""
    if not isinstance(text, str) or not text.strip():
        error = error or "No nonempty real ASR transcript"
    if exit_code != 0 or timed_out:
        error = error or ("Worker failed or timed out, exit_code=" + str(exit_code))
    if error is None:
        paths["transcript"].write_text(text.strip() + "\n", encoding="utf-8")
    root_prefix = output_rel.as_posix() + "/" + run_name + "/"
    outputs = {
        name: {"path": root_prefix + p.name, "sha256": _sha256(p), "bytes": p.stat().st_size}
        for name, p in paths.items() if p.exists() and name != "receipt"
    }
    receipt = {
        "schema": "offline-asr-one-v1", "status": "PASS_TECHNICAL_ASR_ONLY" if error is None else "FAIL",
        "error": error, "started_utc": started, "finished_utc": finished, "pid": pid,
        "exit_code": exit_code, "timed_out": timed_out, "selected_workspace": str(root),
        "input_path": params["input_path"], "input_sha256": input_sha,
        "input_bytes": input_path.stat().st_size, "profile_id": selected["id"],
        "profile_engine": selected["engine"], "model_profile_sha256": config_sha,
        "model_assets": profile_receipt, "language": language, "device": device,
        "worker_script_sha256": _sha256(WORKER_FILE),
        "worker_metadata": {k: worker.get(k) for k in ("backend_version", "torch_version", "modelscope_version", "duration_seconds") if k in worker} if isinstance(worker, dict) else {},
        "produced": outputs,
        "meaning_status": "NOT_A_HUMAN_VERIFICATION",
        "network_policy": "python socket connections refused; offline env flags set; NO KERNEL NETWORK SANDBOX",
        "write_policy": "all intentional output/cache/tmp under run_dir; external Python libraries not OS-sandboxed",
    }
    _atomic_json(paths["receipt"], receipt)
    if error is not None:
        raise RuntimeError("Offline ASR failed; inspect " + root_prefix + "receipt.json: " + error)
    return {
        "ok": True, "status": receipt["status"], "input_sha256": input_sha,
        "profile_id": selected["id"], "pid": pid, "exit_code": exit_code,
        "text": text[:30000], "text_truncated": len(text) > 30000,
        "receipt_sha256": _sha256(paths["receipt"]),
        "workspace_artifacts": [
            {"path": root_prefix + paths[n].name, "label": n, "kind": "file"}
            for n in ("receipt", "result", "transcript", "log")
        ],
        "notes": ["ASR transcript requires listening/semantic review", "No claim of OS sandboxing or human speech confirmation"],
    }


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    _root(context)
    profiles, profile_sha = _load_profiles()
    if action in {"status", "list-profiles"}:
        check = bool(params.get("verify_assets", False)) if action == "status" else False
        records = []
        for profile in profiles:
            item = {"id": profile["id"], "engine": profile["engine"], "language": profile["language"],
                    "python_exe": profile["python_exe"], "configured": True}
            if check:
                try:
                    item["verification"] = _verify_profile(profile, verify_hashes=True)
                    item["ready"] = True
                except Exception as exc:
                    item["ready"] = False
                    item["error"] = str(exc)
            else:
                item["ready"] = "UNVERIFIED"
            records.append(item)
        return {"ok": True, "plugin_version": "0.1.0", "profiles_sha256": profile_sha,
                "profiles": records, "supported_engines": sorted(ENGINES),
                "supported_audio_extensions": sorted(AUDIO_EXTS),
                "all_workspaces": True, "read_only_external_model_profiles": True,
                "no_download_or_command_parameter": True, "hot_reload_requires_exact_hash_approval": True}
    if action == "transcribe-one":
        return _transcribe(params, context)
    raise ValueError("Unsupported action")
