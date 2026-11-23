"""EXE Packager Toolkit — FolderBridge ABI v1 / schema v2 external extension.

One selected workspace and one selected application entry per call.
No arbitrary command/argv/shell selection; no workspace-wide automatic execution.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

from folderbridge_mcp.extension_api import (
    owned_process_group_kwargs,
    terminate_owned_process_tree,
)

VERSION = "0.1.1"
BACKENDS = {
    ".py": "python",
    ".pyw": "python",
    ".go": "go",
    ".csproj": "dotnet",
}
DENIED_COMPONENTS = {
    ".git", ".svn", ".hg", ".venv", "venv", "node_modules", "__pycache__",
    ".folderbridge", "credentials", "secrets", ".ssh", ".aws", ".build",
}
SENSITIVE_NAMES = {".env", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials.json"}
SENSITIVE_EXTS = {".pem", ".p12", ".pfx", ".key"}
OUTPUT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
BACKEND_EXE_NAMES = {
    "python": {"python.exe", "py.exe"},
    "go": {"go.exe"},
    "dotnet": {"dotnet.exe"},
}
# Optional, previously inspected, pre-existing local PyInstaller environment.
# This is only a toolchain candidate, never an output directory or a workspace selector.
FOLDERBRIDGE_BUILD_PY = Path(__file__).resolve().parents[3] / ".build-venv" / "Scripts" / "python.exe"


def _is_link(path: Path) -> bool:
    try:
        entry = path.lstat()
    except OSError:
        return False
    return path.is_symlink() or bool(
        getattr(entry, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _root(context: dict[str, Any]) -> Path:
    raw = context.get("workspace_root")
    if not isinstance(raw, str) or not raw:
        raise ValueError("Select an existing FolderBridge workspace first.")
    root = Path(raw).resolve(strict=True)
    if not root.is_dir() or _is_link(root):
        raise ValueError("Workspace root is not a regular directory.")
    return root


def _relative(raw: Any) -> PurePosixPath:
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 1024
        or "\x00" in raw
        or "\\" in raw
    ):
        raise ValueError("Expected a POSIX-style workspace-relative path.")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or raw.startswith("./")
        or raw.endswith("/")
        or ".." in path.parts
        or str(path) == "."
        or any(
            not part
            or part in {".", ".."}
            or ":" in part
            or part.lower() in DENIED_COMPONENTS
            or part.lower() in SENSITIVE_NAMES
            or part.endswith((" ", "."))
            for part in path.parts
        )
        or path.suffix.lower() in SENSITIVE_EXTS
    ):
        raise ValueError("Unsafe workspace-relative path.")
    return path


def _check_components(root: Path, path: Path) -> None:
    parts = path.relative_to(root).parts
    current = root
    for part in parts:
        current = current / part
        if _is_link(current):
            raise ValueError("Linked/junction paths are not allowed: " + part)


def _source(root: Path, raw: str) -> tuple[Path, PurePosixPath, str]:
    rel = _relative(raw)
    expected_backend = BACKENDS.get(rel.suffix.lower())
    if expected_backend is None:
        raise ValueError("Supported sources: .py, .pyw, .go and .csproj.")
    candidate = root.joinpath(*rel.parts)
    _check_components(root, candidate)
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file() or _is_link(resolved):
        raise ValueError("The source must be a regular file.")
    length = resolved.stat().st_size
    if not 1 <= length <= 32 * 1024 * 1024:
        raise ValueError("Source must be nonempty and no larger than 32 MiB.")
    return resolved, rel, expected_backend


def _output(root: Path, raw: str, *, create: bool) -> tuple[Path, PurePosixPath]:
    rel = _relative(raw)
    candidate = root.joinpath(*rel.parts)
    _check_components(root, candidate)
    if create:
        candidate.mkdir(parents=True, exist_ok=True)
    if candidate.exists():
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        if not resolved.is_dir() or _is_link(resolved):
            raise ValueError("Output directory is not a regular directory.")
    else:
        if not candidate.parent.exists():
            # For preflight only; do not write anything.
            _check_components(root, candidate.parent)
        resolved = candidate
    return resolved, rel


def _output_name(name: Any) -> str:
    if not isinstance(name, str) or not OUTPUT_NAME.fullmatch(name) or name.upper() in {
        "CON", "PRN", "NUL", "AUX", "COM1", "LPT1"
    }:
        raise ValueError("Output name must be 1–64 safe ASCII characters, starting with a letter.")
    if name.lower().endswith(".exe"):
        raise ValueError("Output name must not include .exe.")
    return name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _pe_info(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size < 1024:
        raise ValueError("EXE is too small to be a valid Windows PE program.")
    with path.open("rb") as f:
        mz = f.read(64)
        if len(mz) != 64 or mz[:2] != b"MZ":
            raise ValueError("Output is missing the Windows MZ header.")
        pointer = struct.unpack_from("<I", mz, 0x3C)[0]
        if pointer > min(size - 24, 1048576):
            raise ValueError("Invalid Windows PE header offset.")
        f.seek(pointer)
        pe = f.read(24)
        if len(pe) < 24 or pe[:4] != b"PE\x00\x00":
            raise ValueError("Output is missing the Windows PE signature.")
        machine = struct.unpack_from("<H", pe, 4)[0]
        if machine not in (0x8664, 0x14C, 0xAA64):
            raise ValueError("Unsupported Windows PE machine type.")
    return {"size": size, "pe_machine_hex": "%04x" % machine, "sha256": _sha256(path)}


def _select_backend(source_backend: str, requested: str) -> str:
    if requested not in ("auto", "python", "go", "dotnet"):
        raise ValueError("Unsupported packaging backend.")
    if requested != "auto" and requested != source_backend:
        raise ValueError("Backend does not match source extension.")
    return source_backend


def _tool_candidates(backend: str) -> list[tuple[Path, list[str]]]:
    candidates = []
    if backend == "python":
        raw = [str(FOLDERBRIDGE_BUILD_PY), shutil.which("python.exe"), shutil.which("py.exe")]
    elif backend == "go":
        raw = [shutil.which("go.exe")]
    else:
        raw = [shutil.which("dotnet.exe")]
    seen: set[str] = set()
    for value in raw:
        if not value:
            continue
        path = Path(value)
        if _is_link(path):
            continue
        try:
            path = path.resolve(strict=True)
        except OSError:
            continue
        if (
            str(path).casefold() in seen
            or not path.is_file()
            or _is_link(path)
            or path.name.lower() not in BACKEND_EXE_NAMES[backend]
        ):
            continue
        seen.add(str(path).casefold())
        prefix = [str(path), "-3"] if path.name.lower() == "py.exe" else [str(path)]
        candidates.append((path, prefix))
    return candidates


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST", "HTTP_PROXY", "HTTPS_PROXY",
        "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
    ):
        env.pop(name, None)
    env.update({
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "GOOS": "windows",
        "GOARCH": "amd64",
        "CGO_ENABLED": "0",
        "GOPROXY": "off",
        "GOSUMDB": "off",
        "GOTOOLCHAIN": "local",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_NOLOGO": "1",
    })
    if os.name == "nt" and not env.get("USERPROFILE"):
        env["USERPROFILE"] = str(Path.home())
    return env


def _probe_prefix(backend: str, prefix: list[str]) -> list[str]:
    if backend == "python":
        return prefix + ["-m", "PyInstaller", "--version"]
    if backend == "go":
        return prefix + ["version"]
    return prefix + ["--version"]


def _find_toolchain(backend: str) -> tuple[list[str] | None, str | None, list[str]]:
    errors = []
    for path, prefix in _tool_candidates(backend):
        try:
            r = subprocess.run(
                _probe_prefix(backend, prefix),
                cwd=str(path.parent),
                env=_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                shell=False,
                check=False,
                **owned_process_group_kwargs(hide_window=True),
            )
            response = r.stdout.decode("utf-8", "replace")[-2000:].strip()
            if r.returncode == 0:
                return prefix, response.splitlines()[-1][:180] if response else "available", errors
            errors.append(path.name + ": code=" + str(r.returncode))
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(path.name + ": " + type(exc).__name__)
    return None, None, errors


def _plan(params: dict[str, Any], context: dict[str, Any], *, ensure_outdir=False):
    root = _root(context)
    source, source_rel, detected = _source(root, params["source_path"])
    backend = _select_backend(detected, params.get("backend", "auto"))
    output, output_rel = _output(root, params["output_dir"], create=ensure_outdir)
    name = _output_name(params["output_name"])
    target = output / (name + ".exe")
    _check_components(root, target)
    if target.exists() and (not target.is_file() or _is_link(target)):
        raise ValueError("Output is not a regular file.")
    return root, source, source_rel, backend, output, output_rel, target


def _status() -> dict[str, Any]:
    return {
        "extension": "exe-packager-toolkit",
        "version": VERSION,
        "supported": {
            "python": [".py", ".pyw"],
            "go": [".go"],
            "dotnet": [".csproj"],
        },
        "installation_required": "One approval in FolderBridge Extensions & Skills, not per workspace.",
        "workspace_selection": "Use workspace_id of the existing FolderBridge workspace for every call.",
        "network": "No remote packaging service or project server.",
        "windows_host": os.name == "nt",
        "tools_found": {
            name: [p.name for p, _ in _tool_candidates(name)]
            for name in ("python", "go", "dotnet")
        },
        "limitations": [
            "Only the listed source types are supported.",
            "No arbitrary shell, command line, spec file or executable input.",
            "Local build toolchains are required; PyInstaller is not bundled.",
            "Packing a source project executes local build-tool/plugin code under the Windows user; this is not an OS sandbox.",
        ],
    }


def _preflight(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root, source, source_rel, backend, output, output_rel, target = _plan(params, context)
    tool, version, errors = _find_toolchain(backend) if os.name == "nt" else (None, None, ["Windows host required"])
    return {
        "ok": True,
        "ready": bool(tool) and not target.exists(),
        "backend": backend,
        "source_path": source_rel.as_posix(),
        "source_bytes": source.stat().st_size,
        "source_sha256": _sha256(source),
        "output_path": (output_rel / target.name).as_posix(),
        "output_exists": target.exists(),
        "existing_output_sha256": _sha256(target) if target.is_file() else None,
        "toolchain": tool[0] if tool else None,
        "toolchain_version": version,
        "toolchain_probe_errors": errors,
        "next_action": "package-onefile if ready (supply expected_source_sha256), else install a toolchain or choose a new output name.",
        "no_files_written": True,
    }


def _build_argv(
    backend: str, executable: list[str], source: Path, scratch: Path,
    name: str, gui: bool
) -> tuple[list[str], Path]:
    output = scratch / "dist"
    output.mkdir(exist_ok=True)
    target = output / (name + ".exe")
    if backend == "python":
        return (
            executable + [
                "-m", "PyInstaller",
                "--onefile", "--noconfirm", "--clean", "--noupx",
                "--windowed" if gui else "--console",
                "--name", name,
                "--distpath", str(output),
                "--workpath", str(scratch / "work"),
                "--specpath", str(scratch / "spec"),
                str(source),
            ],
            target,
        )
    if backend == "go":
        return (
            executable + ["build", "-trimpath", "-buildvcs=false", "-o", str(target), str(source)],
            target,
        )
    if backend == "dotnet":
        return (
            executable + [
                "publish", str(source), "-c", "Release", "-r", "win-x64",
                "--self-contained", "true", "--no-restore",
                "-p:PublishSingleFile=true", "-p:PublishTrimmed=false",
                "-p:DebugType=None", "-p:DebugSymbols=false",
                "-o", str(output), "--nologo",
            ],
            output / (source.stem + ".exe"),
        )
    raise ValueError("Unsupported backend.")


def _build_once(argv: list[str], source: Path, log_path: Path, timeout: int):
    started = time.monotonic()
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            argv,
            cwd=str(source.parent),
            env=_environment(),
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **owned_process_group_kwargs(hide_window=True),
        )
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            terminate_owned_process_tree(proc, hide_window=True)
            raise RuntimeError("Packaging exceeded %d seconds and was terminated." % timeout)
    with log_path.open("rb") as stream:
        stream.seek(max(0, log_path.stat().st_size - 12000))
        tail = stream.read(12000).decode("utf-8", errors="replace")
    # Avoid exporting complete environments, long traces or unrelated binary bytes.
    if code != 0:
        raise RuntimeError("Packaging failed (exit %d). Last log:\n%s" % (code, tail[-6000:]))
    return round(time.monotonic() - started, 3), tail[-3500:]


def _copy_exclusive(source: Path, destination: Path) -> None:
    """Copy to an exclusive destination, never remove a preexisting file."""
    created = False
    try:
        with destination.open("xb") as target:
            created = True
            with source.open("rb") as stream:
                shutil.copyfileobj(stream, target, 2 * 1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise


def _package(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if bool(context.get("workspace_read_only")):
        raise ValueError("Selected FolderBridge workspace is read-only.")
    if os.name != "nt":
        raise ValueError("Windows packaging requires a Windows host.")
    root, source, source_rel, backend, output, out_rel, destination = _plan(params, context)
    expected = params["expected_source_sha256"]
    if not HASH_RE.fullmatch(expected) or _sha256(source) != expected:
        raise ValueError("Source SHA-256 differs from the reviewed input.")
    overwrite = bool(params.get("overwrite", False))
    old_expected = params.get("expected_output_sha256")
    if destination.exists():
        if not overwrite or not HASH_RE.fullmatch(old_expected or ""):
            raise ValueError("Output already exists; pass overwrite plus expected_output_sha256.")
        if _sha256(destination) != old_expected:
            raise ValueError("Existing EXE changed since it was inspected.")
    elif overwrite:
        raise ValueError("overwrite=true but the destination EXE does not exist.")
    executable, version, errors = _find_toolchain(backend)
    if not executable:
        raise ValueError("No supported local build toolchain found: " + ", ".join(errors)[:600])
    timeout = params.get("timeout_seconds", 3600)
    if type(timeout) is not int or not 60 <= timeout <= 7200:
        raise ValueError("timeout_seconds must be in 60..7200")
    name = _output_name(params["output_name"])
    gui = bool(params.get("gui", True))
    # Compiler output and all intermediate files live outside selected workspace.
    # The ONLY intentional workspace write is the final EXE into output_dir.
    with tempfile.TemporaryDirectory(prefix="fb-exe-packager-") as temp:
        scratch = Path(temp)
        command, built = _build_argv(backend, executable, source, scratch, name, gui)
        duration, log_tail = _build_once(command, source, scratch / "build.log", timeout)
        if not built.is_file():
            raise RuntimeError("The toolchain reported success but did not create an EXE.")
        pe_info = _pe_info(built)
        if backend == "dotnet":
            # A dotnet publish can create runtime sidecars. Refuse to claim one-file
            # delivery when auxiliary DLL/JSON/native runtime files are still present.
            extra = [f.name for f in built.parent.iterdir()
                     if f.is_file() and f != built and f.suffix.lower() not in {".pdb"}]
            if extra:
                raise RuntimeError("dotnet publish is not truly one-file: " + ", ".join(extra[:12]))
        if _sha256(source) != expected:
            raise ValueError("Source changed during packaging; rejecting the EXE.")
        _output(root, params["output_dir"], create=True)
        if destination.exists():
            if _sha256(destination) != old_expected:
                raise ValueError("Output changed before commit; refusing to overwrite.")
            stage = destination.with_name(destination.name + ".new-partial")
            if stage.exists():
                raise ValueError("An old .new-partial file is present; refusing to overwrite.")
            try:
                _copy_exclusive(built, stage)
                if _sha256(stage) != pe_info["sha256"]:
                    raise ValueError("Output changed during copy.")
                if _sha256(destination) != old_expected:
                    raise ValueError("Output changed before atomic replacement.")
                os.replace(stage, destination)
            finally:
                stage.unlink(missing_ok=True)
        else:
            _copy_exclusive(built, destination)
        confirmed = _pe_info(destination)
        if confirmed["sha256"] != pe_info["sha256"]:
            raise RuntimeError("Copied EXE SHA-256 mismatch.")
    rel = (out_rel / destination.name).as_posix()
    return {
        "ok": True,
        "status": "BUILT_SINGLE_FILE_PE_NOT_RUNTIME_TESTED",
        "backend": backend,
        "toolchain": executable[0],
        "toolchain_version": version,
        "source": source_rel.as_posix(),
        "source_sha256": expected,
        "output": rel,
        "output_bytes": confirmed["size"],
        "output_sha256": confirmed["sha256"],
        "pe_machine_hex": confirmed["pe_machine_hex"],
        "duration_seconds": duration,
        "log_tail": log_tail,
        "workspace_artifacts": [{"path": rel, "label": "Windows one-file EXE", "kind": "file"}],
        "caveat": "PE format/hash checked; real launch and application behavior require Windows device testing.",
    }


def _verify(params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    root = _root(context)
    rel = _relative(params["path"])
    if rel.suffix.lower() != ".exe":
        raise ValueError("Select a .exe file.")
    path = root.joinpath(*rel.parts)
    _check_components(root, path)
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file() or _is_link(resolved):
        raise ValueError("EXE must be a regular non-linked file.")
    info = _pe_info(resolved)
    expected = params.get("expected_sha256")
    if expected and (not HASH_RE.fullmatch(expected) or info["sha256"] != expected):
        raise ValueError("EXE SHA-256 does not match expected hash.")
    return {"ok": True, "path": rel.as_posix(), "verified": info,
            "runtime_status": "NOT_EXECUTED", "workspace_artifacts": [
                {"path": rel.as_posix(), "label": "Verified EXE", "kind": "file"}
            ]}


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action == "status":
        return _status()
    if action == "preflight":
        return _preflight(params, context)
    if action == "package-onefile":
        return _package(params, context)
    if action == "verify-exe":
        return _verify(params, context)
    raise ValueError("Unsupported action: " + str(action))
