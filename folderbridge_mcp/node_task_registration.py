from __future__ import annotations

"""Local-only convenience for registering named Node tasks.

This module NEVER executes Node or approves a config from an MCP call. It produces
an exact fixed-argv task for the existing approved run_task execution boundary.
"""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .config import (
    CONFIG_NAME,
    MAX_TASK_TIMEOUT_SECONDS,
    ConfigError,
    ProjectConfig,
    Task,
    _atomic_write,
    _canonical_json,
    _is_reparse_point,
    _validate_task,
    canonical_workspace,
    config_is_trusted,
    config_path,
    load_config,
)

SCRIPT_SUFFIXES = frozenset({".js", ".cjs", ".mjs"})
EXCLUDED_PATH_PARTS = frozenset({
    ".git", ".venv", "venv", ".build", ".build-venv",
    "node_modules", "__pycache__", ".tox", "secrets", "credentials",
})


class NodeTaskRegistrationError(ConfigError):
    """Node task cannot be registered under the current local policy."""


@dataclass(frozen=True)
class NodeTaskPlan:
    workspace: Path
    script_path: str
    task: Task
    old_config_sha256: str
    new_config_sha256: str
    new_config_bytes: bytes
    update_timeout: bool = False

    @property
    def config_path(self) -> Path:
        return config_path(self.workspace)


def _script_relative_to_workspace(workspace: Path, raw_script: str | Path) -> str:
    if not isinstance(raw_script, (str, Path)) or not str(raw_script).strip():
        raise NodeTaskRegistrationError("Select an existing Node script inside the selected workspace.")
    raw = Path(raw_script)
    if ".." in raw.parts:
        raise NodeTaskRegistrationError("Parent-directory traversal is not permitted in a registered Node path.")
    candidate = raw if raw.is_absolute() else workspace / raw
    try:
        real = candidate.resolve(strict=True)
        relative = real.relative_to(workspace)
    except (OSError, ValueError, RuntimeError) as exc:
        raise NodeTaskRegistrationError("Node script must resolve inside the selected workspace.") from exc
    if not relative.parts or relative.suffix.lower() not in SCRIPT_SUFFIXES:
        raise NodeTaskRegistrationError("Only existing .js, .cjs or .mjs entry files are supported.")
    if any(part.lower() in EXCLUDED_PATH_PARTS for part in relative.parts):
        raise NodeTaskRegistrationError("Node script is in a prohibited dependency, credential or build directory.")
    probe = workspace
    for part in relative.parts:
        probe = probe / part
        try:
            if probe.is_symlink() or _is_reparse_point(probe):
                raise NodeTaskRegistrationError("Linked/reparse-point scripts and parent directories are prohibited.")
        except OSError as exc:
            raise NodeTaskRegistrationError("Cannot inspect Node script path.") from exc
    # A symlink originally in candidate's path can normalize out during resolve.
    raw_relative = candidate.relative_to(workspace) if candidate.is_relative_to(workspace) else relative
    probe = workspace
    for part in raw_relative.parts:
        probe = probe / part
        if probe.is_symlink() or _is_reparse_point(probe):
            raise NodeTaskRegistrationError("Linked/reparse-point scripts and parent directories are prohibited.")
    if not real.is_file():
        raise NodeTaskRegistrationError("Selected Node entry point is not a regular file.")
    rel = relative.as_posix()
    if len(rel) > 950:
        raise NodeTaskRegistrationError("Node entry path exceeds task argv safety limits.")
    return rel


def plan_node_task(
    workspace: str | Path,
    name: str,
    script_path: str | Path,
    timeout_seconds: int = MAX_TASK_TIMEOUT_SECONDS,
    *,
    update_timeout: bool = False,
) -> NodeTaskPlan:
    """Pure preview: no writes, no approval and no code execution."""
    if not isinstance(update_timeout, bool):
        raise NodeTaskRegistrationError("update_timeout must be an explicit boolean.")
    try:
        root = canonical_workspace(workspace)
        config = load_config(root)
        rel = _script_relative_to_workspace(root, script_path)
        task = _validate_task(name, {
            "argv": ["node", rel],
            "timeout_seconds": timeout_seconds,
        })
    except (ConfigError, OSError, ValueError) as exc:
        raise NodeTaskRegistrationError(str(exc)) from exc
    previous = config.tasks.get(name)
    if update_timeout:
        if previous is None or previous.argv != task.argv:
            raise NodeTaskRegistrationError("Timeout-only update requires an existing task with the exact same Node argv.")
    elif previous is not None:
        raise NodeTaskRegistrationError("Task name already exists; existing tasks are never overwritten.")
    if previous is None and len(config.tasks) >= 20:
        raise NodeTaskRegistrationError("This workspace already has the maximum 20 task entries.")
    tasks = {
        key: {"argv": list(value.argv), "timeout_seconds": value.timeout_seconds}
        for key, value in config.tasks.items()
    }
    tasks[name] = {"argv": list(task.argv), "timeout_seconds": task.timeout_seconds}
    blob = _canonical_json({"version": 1, "tasks": tasks})
    if len(blob) > 256 * 1024:
        raise NodeTaskRegistrationError("Resulting task config exceeds the maximum size.")
    return NodeTaskPlan(
        workspace=root,
        script_path=rel,
        task=task,
        old_config_sha256=config.sha256,
        new_config_sha256=hashlib.sha256(blob).hexdigest(),
        new_config_bytes=blob,
        update_timeout=update_timeout,
    )


def install_node_task(plan: NodeTaskPlan) -> ProjectConfig:
    """Local write with optimistic SHA guard. Deliberately does not auto-approve."""
    if not isinstance(plan, NodeTaskPlan):
        raise NodeTaskRegistrationError("Missing a reviewed Node task registration plan.")
    root = canonical_workspace(plan.workspace)
    if root != plan.workspace:
        raise NodeTaskRegistrationError("Workspace identity changed after task planning.")
    if hashlib.sha256(plan.new_config_bytes).hexdigest() != plan.new_config_sha256:
        raise NodeTaskRegistrationError("Planned config payload no longer matches its review hash.")
    old = load_config(root)
    if old.sha256 != plan.old_config_sha256:
        raise NodeTaskRegistrationError("Current task config changed; replan and review without overwriting.")
    # Recompute the entire local plan: a forged payload must not change another
    # task or its argv, even when the attacker supplies its matching SHA.
    checked = plan_node_task(root, plan.task.name, plan.script_path,
                             plan.task.timeout_seconds, update_timeout=plan.update_timeout)
    if checked.old_config_sha256 != plan.old_config_sha256 or checked.task != plan.task or checked.new_config_bytes != plan.new_config_bytes:
        raise NodeTaskRegistrationError("Planned update changes more than its permitted task fields.")
    if _script_relative_to_workspace(root, plan.script_path) != plan.script_path:
        raise NodeTaskRegistrationError("Registered script changed path during review.")
    _atomic_write(plan.config_path, plan.new_config_bytes)
    current = load_config(root, required=True)
    if current.sha256 != plan.new_config_sha256:
        raise NodeTaskRegistrationError("Installed task config failed SHA verification.")
    if config_is_trusted(root, current) and old.sha256 != current.sha256:
        raise NodeTaskRegistrationError("Unexpected inherited approval; no automatic trust may carry forward.")
    return current
