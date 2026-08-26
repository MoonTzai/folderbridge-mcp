from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from folderbridge_mcp.process_control import owned_process_group_kwargs, terminate_owned_process_tree


ALLOWED_OPERATIONS = {
    "probe",
    "bootstrap",
    "prepare-dataset",
    "asr",
    "train",
    "infer",
    "launch-webui",
    "stop",
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


def _status(context: dict[str, Any]) -> dict[str, Any]:
    root = _workspace_root(context)
    runner = root / "GPT-SoVITS-Bridge" / "runner.ps1"
    gpt_root = root / "GPT-SoVITS"
    runtime_python = gpt_root / "runtime" / "python.exe"
    webui = gpt_root / "go-webui.bat"
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
    return {
        "workspace_root": str(root),
        "runner_ready": runner.is_file(),
        "package_root": str(gpt_root),
        "runtime_ready": runtime_python.is_file(),
        "runtime_python": str(runtime_python) if runtime_python.is_file() else None,
        "webui_ready": webui.is_file(),
        "archive_ready": archive.is_file(),
        "archive_path": str(archive),
        "powershell": powershell,
        "seven_zip": seven_zip,
        "ready": bool(runner.is_file() and runtime_python.is_file() and powershell),
    }


def _run(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if bool(context.get("workspace_read_only")):
        raise RuntimeError("FolderBridge is read-only; GPT-SoVITS operations may write workspace files")

    operation = params.get("operation")
    if operation not in ALLOWED_OPERATIONS:
        raise RuntimeError(f"unsupported operation: {operation}")

    root = _workspace_root(context)
    runner = _runner(root)
    powershell = shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("powershell.exe was not found on the trusted PATH")

    state_dir_raw = context.get("state_dir")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise RuntimeError("extension state_dir is unavailable")
    state_dir = Path(state_dir_raw).resolve(strict=True)

    payload = params.get("params") or {}
    if not isinstance(payload, dict):
        raise RuntimeError("params must be an object")

    timeout = int(params.get("timeout_seconds", 7200))
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
        if not stdout:
            return {"ok": True, "operation": operation, "stdout": "", "stderr": stderr[-4000:]}
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            result = {"ok": True, "operation": operation, "stdout": stdout[-12000:]}
        if stderr:
            result["stderr_tail"] = stderr[-4000:]
        return result
    finally:
        if params_path is not None:
            try:
                params_path.unlink()
            except FileNotFoundError:
                pass


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _status(context)
    if action == "run":
        return _run(params, context)
    raise RuntimeError(f"unsupported action: {action}")
