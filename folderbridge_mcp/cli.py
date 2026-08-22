from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import (
    CONFIG_NAME,
    ConfigError,
    approve_config,
    canonical_workspace,
    canonical_workspaces,
    config_is_trusted,
    load_config,
    trust_path,
    write_default_config,
)
from .mcp import McpServer
from .launcher_backend import render_client_config
from .tools import ToolRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="folderbridge-mcp",
        description="Small local-only MCP file tools with exact-edit and opt-in named tasks.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help=f"Create {CONFIG_NAME} with detected task suggestions.")
    _workspace_argument(init)
    init.add_argument("--trust", action="store_true", help="Approve the generated task list on this machine.")
    init.add_argument("--force", action="store_true", help="Replace an existing config.")

    approve = subparsers.add_parser("approve", help="Approve the exact current task config on this machine.")
    _workspace_argument(approve)

    doctor = subparsers.add_parser("doctor", help="Show local safety and setup status.")
    _workspace_argument(doctor)

    config = subparsers.add_parser("client-config", help="Print a ready-to-use local client or tunnel command.")
    _workspace_arguments(config)
    config.add_argument("--format", choices=("tunnel", "json", "toml"), default="tunnel")
    config.add_argument("--read-only", action="store_true")
    config.add_argument("--allow-tasks", action="store_true")

    serve = subparsers.add_parser("serve", help="Run the MCP server over stdio.")
    _workspace_arguments(serve)
    serve.add_argument("--read-only", action="store_true", help="Do not advertise the edit tool.")
    serve.add_argument(
        "--allow-tasks",
        action="store_true",
        help="Advertise named task execution; the task config must also be locally approved.",
    )

    subparsers.add_parser("gui", help="Open the local graphical launcher.")
    return parser


def _workspace_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".", help="The one directory this process can access.")


def _workspace_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--workspace",
        dest="workspaces",
        action="append",
        help="An allowed directory. Repeat for up to eight separate workspaces.",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "gui":
        from .gui import main as gui_main

        return gui_main()
    try:
        if args.command in {"client-config", "serve"}:
            workspaces = canonical_workspaces(args.workspaces or ["."])
            if args.command == "client-config":
                return _client_config(
                    workspaces,
                    output_format=args.format,
                    read_only=args.read_only,
                    allow_tasks=args.allow_tasks,
                )
            # tunnel-client needs this secret, but the local MCP subprocess does not.
            # Drop it before any tool or approved repository task can run.
            os.environ.pop("CONTROL_PLANE_API_KEY", None)
            runtime = ToolRuntime.from_roots(
                workspaces,
                read_only=args.read_only,
                allow_tasks=args.allow_tasks,
            )
            names = ",".join(workspace.name for workspace in workspaces)
            print(
                f"folderbridge-mcp {__version__}: stdio workspaces={len(workspaces)} ({names}) "
                f"mode={'read-only' if args.read_only else 'read/write'} tasks={args.allow_tasks}",
                file=sys.stderr,
                flush=True,
            )
            McpServer(runtime).serve()
            return 0
        workspace = canonical_workspace(args.workspace)
        if args.command == "init":
            return _init(workspace, force=args.force, trust=args.trust)
        if args.command == "approve":
            return _approve(workspace)
        if args.command == "doctor":
            return _doctor(workspace)
    except ConfigError as exc:
        print(f"folderbridge-mcp: {exc}", file=sys.stderr)
        return 2
    return 2


def _init(workspace: Path, *, force: bool, trust: bool) -> int:
    config = write_default_config(workspace, force=force)
    print(f"Created {config.path}")
    _print_tasks(config)
    if trust:
        path = approve_config(workspace, config)
        print(f"Approved this exact config on this machine: {path}")
    elif config.tasks:
        print(f"Tasks remain disabled until you inspect {CONFIG_NAME} and run: {_local_command('approve', workspace)}")
    print(f"MCP command: {_server_command(workspace, read_only=False, allow_tasks=False)}")
    return 0


def _approve(workspace: Path) -> int:
    config = load_config(workspace, required=True)
    _print_tasks(config)
    path = approve_config(workspace, config)
    print(f"Approved SHA-256 {config.sha256}")
    print(f"Trust record: {path}")
    print("Warning: named tasks run repository code with your current OS user permissions.")
    return 0


def _doctor(workspace: Path) -> int:
    config = load_config(workspace)
    trusted = config_is_trusted(workspace, config)
    git = shutil.which("git")
    print(f"FolderBridge MCP {__version__}")
    print(f"Workspace: {workspace}")
    print(f"Python: {sys.executable} ({sys.version.split()[0]})")
    print(f"Git read-only views: {'ready' if git else 'unavailable'}")
    print(f"Config: {config.path if config.sha256 else 'not created (file tools still work)'}")
    print(f"Task config trusted: {'yes' if trusted else 'no'}")
    _print_tasks(config)
    print("Network listener: none")
    print("Telemetry: none")
    print("Arbitrary shell: none")
    print(f"Local MCP command: {_server_command(workspace, read_only=False, allow_tasks=False)}")
    print("For ChatGPT web, pass that command to OpenAI tunnel-client as --mcp-command.")
    if config.tasks and trusted:
        print(f"Task-enabled command: {_server_command(workspace, read_only=False, allow_tasks=True)}")
    return 0


def _client_config(workspaces: tuple[Path, ...], *, output_format: str, read_only: bool, allow_tasks: bool) -> int:
    if allow_tasks:
        for workspace in workspaces:
            config = load_config(workspace, required=True)
            if not config_is_trusted(workspace, config):
                raise ConfigError(f"{CONFIG_NAME} is not approved for {workspace} on this machine")
    access_mode = "read_only" if read_only else "read_write"
    print(render_client_config(workspaces, access_mode, allow_tasks, output_format))
    return 0


def _print_tasks(config: object) -> None:
    tasks = getattr(config, "tasks", {})
    if not tasks:
        print("Approved task candidates: none (file-only workflow)")
        return
    print("Task candidates:")
    for task in tasks.values():
        print(f"  {task.name}: {_join_command(list(task.argv))}  [timeout {task.timeout_seconds}s]")


def _launcher() -> Path:
    return Path(__file__).resolve().parents[1] / "folderbridge_launcher.py"


def _server_argv(workspace: Path, *, read_only: bool, allow_tasks: bool) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        command = str(Path(sys.executable).resolve())
        args = ["serve", "--workspace", str(workspace)]
    else:
        command = sys.executable
        args = [str(_launcher()), "serve", "--workspace", str(workspace)]
    if read_only:
        args.append("--read-only")
    if allow_tasks:
        args.append("--allow-tasks")
    return command, args


def _server_command(workspace: Path, *, read_only: bool, allow_tasks: bool) -> str:
    command, args = _server_argv(workspace, read_only=read_only, allow_tasks=allow_tasks)
    return _join_command([command, *args])


def _local_command(command: str, workspace: Path) -> str:
    if getattr(sys, "frozen", False):
        argv = [str(Path(sys.executable).resolve()), command, "--workspace", str(workspace)]
    else:
        argv = [sys.executable, str(_launcher()), command, "--workspace", str(workspace)]
    return _join_command(argv)


def _join_command(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


if __name__ == "__main__":
    raise SystemExit(main())
