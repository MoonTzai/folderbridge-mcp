from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    from folderbridge_mcp.extension_api import owned_process_group_kwargs, terminate_owned_process_tree
except ImportError:  # FolderBridge 0.8.21 compatibility before the public process-helper re-export.
    from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


ALLOWED_OPERATIONS = {
    "probe",
    "bootstrap",
    "analyze-audio",
    "prepare-reference",
    "stitch-audio",
    "slice-audio",
    "prepare-dataset",
    "asr",
    "train",
    "infer",
    "launch-webui",
    "stop",
    "launch-api",
    "stop-api",
}

DIRECT_ACTIONS = {
    "probe-runtime": "probe",
    "bootstrap": "bootstrap",
    "analyze-audio": "analyze-audio",
    "analyze-audio-batch": "analyze-audio",
    "prepare-reference": "prepare-reference",
    "stitch-audio": "stitch-audio",
    "slice-audio": "slice-audio",
    "asr": "asr",
    "prepare-dataset": "prepare-dataset",
    "train": "train",
    "infer": "infer",
    "infer-batch": "infer",
    "launch-webui": "launch-webui",
    "stop-webui": "stop",
    "launch-api": "launch-api",
    "stop-api": "stop-api",
}

MODEL_SUFFIXES = {".ckpt", ".pth"}
ARTIFACT_KEYS = {
    "output",
    "params_sidecar",
    "list_file",
    "segments_manifest",
    "shortlist",
    "manifest_copy",
    "text_features",
    "semantic_tokens",
    "sovits_config",
    "gpt_config",
    "sovits_checkpoint",
    "gpt_checkpoint",
}


def _workspace_root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise RuntimeError("workspace_root is required")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError("workspace_root is not a directory")
    return root


def _runner(root: Path) -> Path:
    runner = root / "GPT-SoVITS-Bridge" / "runner.ps1"
    if not runner.is_file():
        raise RuntimeError("GPT-SoVITS-Bridge/runner.ps1 is missing from the selected workspace")
    resolved = runner.resolve(strict=True)
    resolved.relative_to(root)
    return resolved


def _safe_exp_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name:
        raise RuntimeError("exp_name must be non-empty")
    if any(ch in name for ch in '\\/:*?"<>|') or name in {".", ".."}:
        raise RuntimeError("exp_name contains invalid path characters")
    return name


def _relative_if_file(root: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return str(resolved.relative_to(root)).replace("\\", "/")


def _collect_artifacts(root: Path, result: Any, *, limit: int = 256) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if len(found) >= limit:
            return
        rel = _relative_if_file(root, value)
        if rel and rel not in seen:
            seen.add(rel)
            found.append(rel)

    def walk(value: Any, key: str | None = None) -> None:
        if len(found) >= limit:
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key in ARTIFACT_KEYS:
                    add(child)
                elif child_key == "outputs" and isinstance(child, list):
                    walk(child, child_key)
                elif child_key == "item" and isinstance(child, dict):
                    walk(child, child_key)
                elif child_key == "items" and isinstance(child, list):
                    walk(child, child_key)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)

    walk(result)
    return found


def _process_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    if os.name != "nt":
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    WAIT_TIMEOUT = 0x00000102
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        wait = kernel32.WaitForSingleObject(handle, 0)
        return wait == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _service_record(root: Path, name: str, host: str, port: int) -> dict[str, Any]:
    state = root / "GPT-SoVITS-Work" / "state"
    pid_file = state / f"{name}.pid"
    pid = None
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="ascii").strip())
        except Exception:
            pid = None
    return {
        "name": name,
        "pid_file": str(pid_file.relative_to(root)).replace("\\", "/"),
        "pid": pid,
        "process_alive": None if pid is None else _process_alive(pid),
        "host": host,
        "port": port,
    }


def _status(context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    runner = root / "GPT-SoVITS-Bridge" / "runner.ps1"
    bridge_driver = root / "GPT-SoVITS-Bridge" / "ops" / "bridge_ops.py"
    gpt_root = root / "GPT-SoVITS"
    runtime_python = gpt_root / "runtime" / "python.exe"
    if not runtime_python.is_file():
        runtime_python = gpt_root / "runtime" / "Scripts" / "python.exe"
    webui = gpt_root / "go-webui.bat"
    api = gpt_root / "api_v2.py"
    archive = root / "GPT-SoVITS-Bridge" / "downloads" / "GPT-SoVITS-v2pro-20250604.7z"
    powershell = shutil.which("powershell.exe")
    seven_zip = shutil.which("7z.exe")
    if not seven_zip:
        for candidate in (
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "7-Zip" / "7z.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "7-Zip" / "7z.exe",
        ):
            if candidate.is_file():
                seven_zip = str(candidate)
                break
    services = {
        "webui": _service_record(root, "webui", "127.0.0.1", 9874),
        "api": _service_record(root, "api", "127.0.0.1", 9880),
    }
    return {
        "ok": True,
        "workspace_root": str(root),
        "runner_ready": runner.is_file(),
        "bridge_driver_ready": bridge_driver.is_file(),
        "package_root": str(gpt_root),
        "runtime_ready": runtime_python.is_file(),
        "runtime_python": str(runtime_python) if runtime_python.is_file() else None,
        "webui_ready": webui.is_file(),
        "api_ready": api.is_file(),
        "archive_ready": archive.is_file(),
        "archive_path": str(archive),
        "powershell": powershell,
        "seven_zip": seven_zip,
        "services": services,
        "verified_training_family": "v2Pro",
        "ready": bool(runner.is_file() and bridge_driver.is_file() and runtime_python.is_file() and powershell),
    }


def _list_models(context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    gpt_root = root / "GPT-SoVITS"
    pretrained = gpt_root / "GPT_SoVITS" / "pretrained_models"
    work = root / "GPT-SoVITS-Work" / "checkpoints"
    rows: list[dict[str, Any]] = []
    for source_name, source_root in (("pretrained", pretrained), ("trained", work)):
        if not source_root.is_dir():
            continue
        for file in source_root.rglob("*"):
            if len(rows) >= 1000:
                break
            if not file.is_file() or file.suffix.lower() not in MODEL_SUFFIXES:
                continue
            try:
                rel = file.resolve(strict=True).relative_to(root)
            except ValueError:
                continue
            rows.append({
                "name": file.name,
                "path": str(rel).replace("\\", "/"),
                "kind": "gpt" if file.suffix.lower() == ".ckpt" else "sovits",
                "source": source_name,
                "size_bytes": file.stat().st_size,
                "modified_ns": file.stat().st_mtime_ns,
            })
    rows.sort(key=lambda x: (x["source"], x["kind"], x["path"].lower()))
    return {
        "ok": True,
        "count": len(rows),
        "models": rows,
        "truncated": len(rows) >= 1000,
    }


def _dataset_status(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    exp_name = _safe_exp_name(params.get("exp_name"))
    work = root / "GPT-SoVITS-Work"
    exp = work / "experiments" / exp_name
    required = {
        "text_features": exp / "2-name2text.txt",
        "bert_features": exp / "3-bert",
        "hubert_features": exp / "4-cnhubert",
        "wav32k": exp / "5-wav32k",
        "semantic_tokens": exp / "6-name2semantic.tsv",
    }
    checkpoints = work / "checkpoints" / exp_name
    gpt = checkpoints / "gpt"
    sovits = checkpoints / "sovits"
    status = {}
    for key, path in required.items():
        status[key] = {
            "ready": path.is_file() if path.suffix else path.is_dir(),
            "path": str(path.relative_to(root)).replace("\\", "/"),
        }
    gpt_files = sorted(
        (p for p in gpt.glob("*.ckpt") if p.is_file()),
        key=lambda p: p.stat().st_mtime_ns,
    ) if gpt.is_dir() else []
    sovits_files = sorted(
        (p for p in sovits.glob("*.pth") if p.is_file()),
        key=lambda p: p.stat().st_mtime_ns,
    ) if sovits.is_dir() else []
    return {
        "ok": True,
        "exp_name": exp_name,
        "experiment_exists": exp.is_dir(),
        "prepared": all(row["ready"] for row in status.values()),
        "components": status,
        "gpt_checkpoint_count": len(gpt_files),
        "sovits_checkpoint_count": len(sovits_files),
        "latest_gpt_checkpoint": str(gpt_files[-1].relative_to(root)).replace("\\", "/") if gpt_files else None,
        "latest_sovits_checkpoint": str(sovits_files[-1].relative_to(root)).replace("\\", "/") if sovits_files else None,
    }


def _service_status(context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    return {
        "ok": True,
        "services": {
            "webui": _service_record(root, "webui", "127.0.0.1", 9874),
            "api": _service_record(root, "api", "127.0.0.1", 9880),
        },
    }


def _parse_bridge_stdout(stdout: str, operation: str) -> dict[str, Any]:
    if not stdout:
        return {"ok": True, "operation": operation, "stdout": ""}

    try:
        parsed: Any | None = json.loads(stdout)
        prefix = ""
    except json.JSONDecodeError:
        parsed = None
        prefix = stdout
        for line in reversed(stdout.splitlines()):
            candidate = line.strip()
            if not candidate or not candidate.startswith(("{", "[")):
                continue
            try:
                parsed = json.loads(candidate)
                index = stdout.rfind(line)
                prefix = stdout[:index] if index >= 0 else stdout
                break
            except json.JSONDecodeError:
                continue

    if parsed is None:
        return {"ok": True, "operation": operation, "stdout": stdout[-12000:]}
    if isinstance(parsed, dict):
        result = dict(parsed)
        if prefix.strip():
            result["stdout_tail"] = prefix[-4000:]
        return result
    return {"ok": True, "operation": operation, "result": parsed}


def _run_operation(operation: str, payload: dict[str, Any], timeout: int, context: dict[str, Any]) -> dict[str, Any]:
    if operation not in ALLOWED_OPERATIONS:
        raise RuntimeError(f"unsupported operation: {operation}")
    if bool(context.get("workspace_read_only")) and operation != "probe":
        raise RuntimeError("FolderBridge is read-only; this GPT-SoVITS operation may mutate runtime/workspace state")

    root = _workspace_root(context)
    runner = _runner(root)
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("powershell.exe was not found on the trusted PATH")

    state_dir_raw = context.get("state_dir")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise RuntimeError("extension state_dir is unavailable")
    state_dir = Path(state_dir_raw).resolve(strict=True)

    params_path: Path | None = None
    try:
        fd, raw_path = tempfile.mkstemp(prefix="gpt-sovits-", suffix=".json", dir=state_dir)
        os.close(fd)
        params_path = Path(raw_path)
        params_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        argv = [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            "-WorkspaceRoot",
            str(root),
            "-Operation",
            operation,
            "-ParamsPath",
            str(params_path),
        ]
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                **owned_process_group_kwargs(hide_window=True),
            )
        except OSError as exc:
            raise RuntimeError(f"could not start GPT-SoVITS runner: {exc}") from exc

        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=None if timeout == 0 else timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_owned_process_tree(process, hide_window=True)
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout_bytes, stderr_bytes = process.communicate(timeout=5)
            raise RuntimeError(f"GPT-SoVITS operation exceeded {timeout} seconds") from exc

        stdout = stdout_bytes.decode("utf-8-sig", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8-sig", errors="replace").strip()
        if process.returncode != 0:
            detail = stderr or stdout or f"exit code {process.returncode}"
            raise RuntimeError(f"GPT-SoVITS operation failed: {detail[-6000:]}")
        result = _parse_bridge_stdout(stdout, operation)
        if stderr:
            result["stderr_tail"] = stderr[-4000:]
        artifacts = _collect_artifacts(root, result)
        if artifacts:
            result["workspace_artifacts"] = artifacts
        return result
    finally:
        if params_path is not None:
            try:
                params_path.unlink()
            except FileNotFoundError:
                pass


def _legacy_run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    operation = params.get("operation")
    if operation not in ALLOWED_OPERATIONS:
        raise RuntimeError(f"unsupported operation: {operation}")
    payload = params.get("params") or {}
    if not isinstance(payload, dict):
        raise RuntimeError("params must be an object")
    timeout = int(params.get("timeout_seconds", 7200))
    return _run_operation(operation, payload, timeout, context)


def _direct_action(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    operation = DIRECT_ACTIONS[action]
    payload = dict(params)
    timeout = int(payload.pop("timeout_seconds", 7200))

    if action == "infer-batch":
        items = payload.get("items") or []
        if not items:
            raise RuntimeError("items must not be empty")
        payload["text"] = items[0]["text"]

    if action == "analyze-audio-batch":
        payload["audio_paths"] = payload.pop("audio_paths")

    return _run_operation(operation, payload, timeout, context)


def _verification_plan() -> dict[str, Any]:
    return {
        "ok": True,
        "verified_training_family": "v2Pro",
        "recommended_sequence": [
            "status",
            "probe-runtime",
            "list-models",
            "analyze-audio / prepare-reference / stitch-audio / slice-audio / asr",
            "prepare-dataset",
            "dataset-status",
            "train when fine-tuning is required",
            "infer or infer-batch",
            "launch-webui or launch-api only when an interactive/local service is required",
        ],
        "notes": [
            "Dedicated actions are preferred over the deprecated compatibility run action.",
            "All bridge paths are ultimately confined to the selected workspace by GPT-SoVITS-Bridge.",
            "ASR can optionally run official slicing first and emits acoustic manifests/shortlists.",
            "Current training automation is intentionally authority-locked to the verified v2Pro package.",
            "WebUI binds to the package default local interface; API is fixed to 127.0.0.1:9880.",
        ],
    }


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "verification-plan":
        return _verification_plan()
    if action == "status":
        return _status(context)
    if action == "list-models":
        return _list_models(context)
    if action == "dataset-status":
        return _dataset_status(params, context)
    if action == "service-status":
        return _service_status(context)
    if action == "run":
        return _legacy_run(params, context)
    if action in DIRECT_ACTIONS:
        return _direct_action(action, params, context)
    raise RuntimeError(f"unsupported action: {action}")
