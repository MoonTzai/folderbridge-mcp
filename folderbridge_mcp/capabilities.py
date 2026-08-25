from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Iterable

from .config import Task
from .security import ToolError, clean_environment, resolve_task_executable
from .task_runner import run_task
from .workspace_validation import run_safe_build, run_workspace_smoke


EXECUTION_CAPABILITY_NAMES = (
    "test",
    "build",
    "package-windows",
    "package-android",
    "release-sync",
    "git-push",
)

CAPABILITY_NAMES = EXECUTION_CAPABILITY_NAMES

CAPABILITY_LABELS = {
    "test": "测试",
    "build": "项目构建",
    "package-windows": "封装 Windows EXE",
    "package-android": "封装 Android APK",
    "release-sync": "同步发布交付",
    "git-push": "推送 GitHub",
}

PACKAGE_JSON_LIMIT = 512 * 1024
GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?$",
    re.IGNORECASE,
)


def normalize_capability_names(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    requested = list(values)
    if not all(isinstance(value, str) for value in requested):
        raise ValueError("Capability names must be strings")
    unknown = sorted(set(requested).difference(CAPABILITY_NAMES))
    if unknown:
        raise ValueError(f"Unknown capabilities: {', '.join(unknown)}")
    if len(set(requested)) != len(requested):
        raise ValueError("Capability names cannot be duplicated")
    requested_set = set(requested)
    return tuple(name for name in CAPABILITY_NAMES if name in requested_set)


def discover_capabilities(workspace: Path) -> dict[str, dict[str, str]]:
    root = workspace.resolve(strict=True)
    discovered: dict[str, dict[str, str]] = {}
    builders = {
        "test": _test_task,
        "build": _build_task,
        "package-windows": _windows_package_task,
        "package-android": _android_package_task,
        "release-sync": _release_sync_task,
    }
    for name, builder in builders.items():
        task = builder(root)
        if task is not None:
            discovered[name] = {
                "label": CAPABILITY_LABELS[name],
                "source": _task_source(task),
                "provider": "project-task",
            }
            continue
        if name == "test":
            discovered[name] = {
                "label": CAPABILITY_LABELS[name],
                "source": "FolderBridge built-in bounded workspace smoke",
                "provider": "builtin-workspace-smoke",
            }
        elif name == "build":
            discovered[name] = {
                "label": CAPABILITY_LABELS[name],
                "source": "FolderBridge built-in safe build fallback (identity or validation-only)",
                "provider": "builtin-safe-build",
            }
    if _safe_directory(root, ".git"):
        discovered["git-push"] = {
            "label": CAPABILITY_LABELS["git-push"],
            "source": "local .git repository; GitHub HTTPS origin is validated at execution time",
        }
    return discovered


def run_capability(workspace: Path, name: str) -> dict[str, object]:
    if name not in EXECUTION_CAPABILITY_NAMES:
        raise ToolError(
            "UNKNOWN_CAPABILITY",
            "This global authorization uses a dedicated built-in tool rather than run_capability.",
            available=list(EXECUTION_CAPABILITY_NAMES),
        )
    root = workspace.resolve(strict=True)
    if name == "git-push":
        return _run_github_push(root)

    builders = {
        "test": _test_task,
        "build": _build_task,
        "package-windows": _windows_package_task,
        "package-android": _android_package_task,
        "release-sync": _release_sync_task,
    }
    task = builders[name](root)
    if task is None:
        if name == "test":
            result = run_workspace_smoke(root)
        elif name == "build":
            result = run_safe_build(root)
        else:
            raise ToolError(
                "CAPABILITY_UNAVAILABLE",
                f"{CAPABILITY_LABELS[name]} is globally pre-authorized but no supported project entry point is currently present.",
                capability=name,
            )
    else:
        result = run_task(root, task)
        result["provider"] = "project-task"
        result["provider_kind"] = "workspace-code"
    result["capability"] = name
    result["label"] = CAPABILITY_LABELS[name]
    return result


def _npm_script_task(root: Path, script_name: str, task_name: str, timeout_seconds: int) -> Task | None:
    scripts = _package_json_scripts(root)
    if script_name not in scripts:
        return None
    return Task(task_name, ("npm", "run", script_name), timeout_seconds)


def _test_task(root: Path) -> Task | None:
    declared = _npm_script_task(root, "test", "capability-test", 300)
    if declared is not None:
        return declared
    tests = root / "tests"
    if _safe_directory(root, "tests") and any(
        path.is_file() and not path.is_symlink() for path in tests.glob("test_*.py")
    ):
        return Task(
            "capability-test",
            ("python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"),
            300,
        )
    if any(_safe_file(root, name) for name in ("pytest.ini", "tox.ini")):
        return Task("capability-test", ("python", "-m", "pytest", "-q"), 300)
    return None


def _build_task(root: Path) -> Task | None:
    return _npm_script_task(root, "build", "capability-build", 600)


def _windows_package_task(root: Path) -> Task | None:
    declared = _npm_script_task(root, "package:windows", "capability-package-windows", 600)
    if declared is not None:
        return declared
    candidates = (
        "scripts/build_windows.ps1",
        "scripts/package_windows.ps1",
        "build_windows.ps1",
        "package_windows.ps1",
    )
    for relative in candidates:
        if not _safe_file(root, relative):
            continue
        argv: list[str] = (
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", relative]
            if os.name == "nt"
            else ["pwsh", "-NoProfile", "-File", relative]
        )
        if relative.endswith("build_windows.ps1"):
            for python_relative in (
                ".build-venv/Scripts/python.exe",
                ".venv/Scripts/python.exe",
                ".venv/bin/python",
            ):
                if _safe_file(root, python_relative):
                    argv.extend(("-Python", python_relative))
                    break
        return Task("capability-package-windows", tuple(argv), 600)

    specs = [path for path in root.glob("*.spec") if path.is_file() and not path.is_symlink()]
    if len(specs) == 1:
        return Task(
            "capability-package-windows",
            ("python", "-m", "PyInstaller", "--noconfirm", specs[0].name),
            600,
        )
    return None


def _release_sync_task(root: Path) -> Task | None:
    return _npm_script_task(root, "release:sync", "capability-release-sync", 900)


def _android_package_task(root: Path) -> Task | None:
    declared = _npm_script_task(root, "package:android", "capability-package-android", 900)
    if declared is not None:
        return declared

    wrapper_candidates = ("gradlew.bat", "android/gradlew.bat") if os.name == "nt" else ("gradlew", "android/gradlew")
    for relative in wrapper_candidates:
        if not _safe_file(root, relative):
            continue
        if os.name == "nt":
            return Task(
                "capability-package-android",
                ("cmd.exe", "/d", "/c", relative, "assembleRelease"),
                900,
            )
        return Task(
            "capability-package-android",
            ("sh", relative, "assembleRelease"),
            900,
        )
    if _safe_file(root, "pubspec.yaml"):
        return Task(
            "capability-package-android",
            ("flutter", "build", "apk", "--release"),
            900,
        )
    return None


def _run_github_push(root: Path) -> dict[str, object]:
    top = Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if top != root:
        raise ToolError(
            "CAPABILITY_UNAVAILABLE",
            "GitHub push requires the FolderBridge workspace itself to be the Git repository root.",
        )
    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch or branch == "HEAD":
        raise ToolError("DETACHED_HEAD", "GitHub push requires a named current branch.")
    _reject_unsafe_local_git_config(root)
    remote = _git_text(root, "remote", "get-url", "--push", "origin")
    if not GITHUB_HTTPS_RE.fullmatch(remote):
        raise ToolError(
            "GITHUB_ORIGIN_REQUIRED",
            "Global git-push only permits a credential-free https://github.com/<owner>/<repo>[.git] origin URL.",
            origin=remote,
        )
    git = resolve_task_executable("git", root)
    task = Task(
        "capability-git-push",
        (
            git,
            "push",
            "--porcelain",
            "--no-verify",
            "origin",
            f"HEAD:refs/heads/{branch}",
        ),
        300,
    )
    result = run_task(root, task)
    result["capability"] = "git-push"
    result["label"] = CAPABILITY_LABELS["git-push"]
    result["branch"] = branch
    result["origin"] = remote
    return result


def _reject_unsafe_local_git_config(root: Path) -> None:
    rendered = _git_text(root, "config", "--local", "--list")
    unsafe: list[str] = []
    for line in rendered.splitlines():
        key = line.split("=", 1)[0].strip().lower()
        if (
            key == "credential.helper"
            or (key.startswith("credential.") and key.endswith(".helper"))
            or key in {"core.sshcommand", "remote.origin.pushurl", "remote.origin.receivepack"}
            or (key.startswith("url.") and (key.endswith(".insteadof") or key.endswith(".pushinsteadof")))
        ):
            unsafe.append(key)
    if unsafe:
        raise ToolError(
            "UNSAFE_GIT_CONFIG",
            "Global git-push refuses repository-local Git settings that can execute helpers or rewrite the push target.",
            keys=sorted(set(unsafe)),
        )


def _git_text(root: Path, *args: str) -> str:
    git = resolve_task_executable("git", root)
    result = run_task(root, Task("capability-git-inspect", (git, *args), 15))
    if bool(result.get("timed_out")):
        raise ToolError("GIT_FAILED", f"git {' '.join(args)} exceeded 15 seconds")
    if bool(result.get("truncated")):
        raise ToolError("GIT_FAILED", "Git inspection output exceeded the bounded capture limit; refusing a partial result.")
    exit_code = result.get("exit_code")
    if exit_code != 0:
        message = str(result.get("stderr") or "").strip()
        raise ToolError("GIT_FAILED", message[:2000] or f"git {' '.join(args)} failed")
    return str(result.get("stdout") or "").strip()


def _package_json_scripts(root: Path) -> dict[str, str]:
    path = root / "package.json"
    if not _safe_file(root, "package.json"):
        return {}
    try:
        data = path.read_bytes()
        if len(data) > PACKAGE_JSON_LIMIT:
            return {}
        parsed = json.loads(data)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    scripts = parsed.get("scripts") if isinstance(parsed, dict) else None
    if not isinstance(scripts, dict):
        return {}
    return {key: value for key, value in scripts.items() if isinstance(key, str) and isinstance(value, str)}


def _safe_file(root: Path, relative: str) -> bool:
    path = root / Path(relative)
    return _safe_path(root, path, require_directory=False)


def _safe_directory(root: Path, relative: str) -> bool:
    path = root / Path(relative)
    return _safe_path(root, path, require_directory=True)


def _safe_path(root: Path, path: Path, *, require_directory: bool) -> bool:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        if path.is_symlink() or _is_reparse_point(path):
            return False
        return resolved.is_dir() if require_directory else resolved.is_file()
    except (OSError, ValueError):
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _task_source(task: Task) -> str:
    return subprocess.list2cmdline(list(task.argv)) if os.name == "nt" else " ".join(task.argv)
