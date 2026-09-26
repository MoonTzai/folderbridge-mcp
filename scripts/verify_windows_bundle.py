from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import sys
import threading
import time
import tempfile
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BUNDLED_EXTENSIONS = {"git-publisher", "office", "skill-engine"}
EXPECTED_BUNDLED_SKILL_PACKS = {"folderbridge-engineering"}
EXPECTED_ENGINEERING_SKILLS = {
    "codebase-design",
    "improve-codebase-architecture",
    "diagnosing-bugs",
    "tdd",
    "code-review",
    "implement",
}
EXPECTED_EXTENSION_VERSIONS = {"git-publisher": "1.5.0", "office": "1.1.4"}


def _project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return str(tomllib.load(stream)["project"]["version"])


def _run(executable: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        [str(executable), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"{' '.join(args)} failed with exit code {completed.returncode}: {stderr}")
    return completed


def _json(executable: Path, *args: str) -> dict[str, Any]:
    completed = _run(executable, *args)
    try:
        payload = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        preview = completed.stdout[:240].decode("utf-8", errors="replace")
        raise RuntimeError(f"{' '.join(args)} did not return valid UTF-8 JSON: {preview!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{' '.join(args)} returned a non-object JSON payload")
    return payload


def _mcp_lifecycle_smoke(executable: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="folderbridge-bundle-smoke-") as temporary:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "bundle-verifier", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "server_info", "arguments": {}},
            },
        ]
        process = subprocess.Popen(
            [
                str(executable),
                "serve",
                "--workspace",
                temporary,
                "--allow-tasks",
                "--capability",
                "test",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            process.wait()
            raise RuntimeError("packaged MCP lifecycle smoke could not open stdio pipes")

        response_lines: queue.Queue[bytes | None] = queue.Queue()

        def read_stdout() -> None:
            try:
                for raw in iter(process.stdout.readline, b""):
                    response_lines.put(raw)
            finally:
                response_lines.put(None)

        reader = threading.Thread(target=read_stdout, name="bundle-mcp-stdout", daemon=True)
        reader.start()

        responses: dict[int, dict[str, Any]] = {}
        try:
            for request in requests:
                request_id = int(request["id"])
                encoded = json.dumps(request, separators=(",", ":")).encode("utf-8") + b"\n"
                process.stdin.write(encoded)
                process.stdin.flush()

                deadline = time.monotonic() + 30.0
                while request_id not in responses:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(
                            f"packaged MCP lifecycle smoke timed out waiting for response {request_id}"
                        )
                    try:
                        raw = response_lines.get(timeout=remaining)
                    except queue.Empty as exc:
                        raise RuntimeError(
                            f"packaged MCP lifecycle smoke timed out waiting for response {request_id}"
                        ) from exc
                    if raw is None:
                        diagnostics = process.stderr.read().decode("utf-8", errors="replace")[-2000:]
                        raise RuntimeError(
                            f"packaged MCP lifecycle smoke closed before response {request_id}: {diagnostics}"
                        )
                    try:
                        response = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                        continue
                    if isinstance(response, dict) and isinstance(response.get("id"), int):
                        responses[int(response["id"])] = response

            # Only retire stdin after all three active-runtime observations have
            # been received. Closing it earlier races EOF shutdown against queued
            # control requests and can falsely report runtime_state=shutting_down.
            process.stdin.close()
            process.wait(timeout=15)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired, RuntimeError):
            if process.poll() is None:
                process.kill()
                process.wait()
            raise
        finally:
            reader.join(timeout=1)

        if process.returncode != 0:
            diagnostics = process.stderr.read().decode("utf-8", errors="replace")[-2000:]
            raise RuntimeError(
                f"packaged MCP lifecycle smoke failed with exit code {process.returncode}: {diagnostics}"
            )

        catalog_response = responses.get(2)
        tools = catalog_response.get("result", {}).get("tools") if isinstance(catalog_response, dict) else None
        if not isinstance(tools, list) or not all(isinstance(tool, dict) for tool in tools):
            raise RuntimeError("packaged MCP tools/list did not return a valid tool catalog")

        info_response = responses.get(3)
        info = (
            info_response.get("result", {}).get("structuredContent")
            if isinstance(info_response, dict)
            else None
        )
        if not isinstance(info, dict):
            raise RuntimeError("packaged MCP server_info did not return structuredContent")
        return tools, info


def _action_enum(tool: dict[str, Any]) -> set[str]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return set()
    properties = schema.get("properties")
    action = properties.get("action") if isinstance(properties, dict) else None
    values = action.get("enum") if isinstance(action, dict) else None
    return {str(value) for value in values} if isinstance(values, list) else set()


def verify(executable: Path) -> dict[str, Any]:
    executable = executable.resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError("built executable is not a regular file")

    version = _run(executable, "--version").stdout.decode("utf-8", errors="strict").strip()
    expected_version = f"folderbridge-mcp {_project_version()}"
    if version != expected_version:
        raise RuntimeError(f"version smoke mismatch: expected {expected_version!r}, got {version!r}")

    mcp_tool_list, mcp_info = _mcp_lifecycle_smoke(executable)
    mcp_tools = {
        str(tool.get("name")): tool
        for tool in mcp_tool_list
        if isinstance(tool.get("name"), str)
    }
    required_tools = {"flight_recorder", "extension", "file_ops", "run_task", "run_capability"}
    missing_tools = sorted(required_tools.difference(mcp_tools))
    if missing_tools:
        raise RuntimeError(f"packaged MCP catalog is missing required tools: {missing_tools}")
    if _action_enum(mcp_tools["extension"]) != {"list", "info", "run", "job_list", "job_status", "job_cancel"}:
        raise RuntimeError("packaged Extension Job-management action contract mismatch")
    expected_task_actions = {"run", "list", "status", "cancel"}
    for tool_name in ("run_task", "run_capability"):
        if _action_enum(mcp_tools[tool_name]) != expected_task_actions:
            raise RuntimeError(f"packaged {tool_name} Job-management action contract mismatch")

    continuity = mcp_info.get("continuity")
    if not isinstance(continuity, dict):
        raise RuntimeError("packaged server_info is missing continuity metadata")
    if continuity.get("runtime_state") != "ready":
        raise RuntimeError(f"packaged runtime_state mismatch: {continuity.get('runtime_state')!r}")
    catalog_revision = continuity.get("catalog_revision")
    if isinstance(catalog_revision, bool) or not isinstance(catalog_revision, int) or catalog_revision < 1:
        raise RuntimeError(f"packaged catalog_revision is invalid: {catalog_revision!r}")
    if continuity.get("diagnostics_available") is not True:
        raise RuntimeError("packaged diagnostics_available must be true")

    expected_catalog_sha256 = hashlib.sha256(
        json.dumps(
            mcp_tool_list,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if continuity.get("catalog_sha256") != expected_catalog_sha256:
        raise RuntimeError("packaged continuity catalog_sha256 does not match tools/list")

    tool_surface = mcp_info.get("tool_surface")
    if not isinstance(tool_surface, dict):
        raise RuntimeError("packaged server_info is missing tool_surface metadata")
    expected_tool_names = [str(tool["name"]) for tool in mcp_tool_list]
    if tool_surface.get("tool_names") != expected_tool_names:
        raise RuntimeError("packaged tool_surface names do not match tools/list")
    if tool_surface.get("tool_count") != len(expected_tool_names):
        raise RuntimeError("packaged tool_surface count does not match tools/list")
    if tool_surface.get("catalog_sha256") != expected_catalog_sha256:
        raise RuntimeError("packaged tool_surface catalog_sha256 does not match tools/list")
    if tool_surface.get("run_capability_exposed") is not True or tool_surface.get("run_task_exposed") is not True:
        raise RuntimeError("packaged execution tool surface is incomplete")
    if tool_surface.get("binding_consistent") is not True or tool_surface.get("binding_issues") != []:
        raise RuntimeError(
            f"packaged execution tool binding is inconsistent: {tool_surface.get('binding_issues')!r}"
        )
    if tool_surface.get("run_capability_names") != ["test"]:
        raise RuntimeError("packaged run_capability enum does not match requested test capability")
    if mcp_info.get("global_capabilities_enabled") != ["test"]:
        raise RuntimeError("packaged server_info global capabilities do not match tools/list")
    if mcp_info.get("task_execution_enabled") is not True:
        raise RuntimeError("packaged server_info task execution flag does not match run_task exposure")

    extension_catalog = _json(executable, "extensions", "--json")
    extensions = extension_catalog.get("extensions")
    if not isinstance(extensions, list):
        raise RuntimeError("extension catalog has no extensions list")
    bundled_extensions = {
        str(item.get("id"))
        for item in extensions
        if isinstance(item, dict) and item.get("bundled") is True
    }
    if bundled_extensions != EXPECTED_BUNDLED_EXTENSIONS:
        raise RuntimeError(
            f"bundled Extension set mismatch: expected {sorted(EXPECTED_BUNDLED_EXTENSIONS)}, "
            f"got {sorted(bundled_extensions)}"
        )
    extension_by_id = {
        str(item.get("id")): item
        for item in extensions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for extension_id, expected_version_value in EXPECTED_EXTENSION_VERSIONS.items():
        actual = extension_by_id.get(extension_id, {}).get("version")
        if actual != expected_version_value:
            raise RuntimeError(
                f"{extension_id} version mismatch: expected {expected_version_value}, got {actual}"
            )

    publisher = extension_by_id.get("git-publisher")
    publisher_actions = publisher.get("actions") if isinstance(publisher, dict) else None
    if not isinstance(publisher_actions, list):
        raise RuntimeError("git-publisher bundled action metadata is missing")
    publisher_action_by_name = {
        str(item.get("name")): item
        for item in publisher_actions
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if "release" in publisher_action_by_name:
        raise RuntimeError("git-publisher must not expose a project-specific legacy release action")
    for action_name in ("status", "connect", "commit", "push", "release-assets"):
        action_meta = publisher_action_by_name.get(action_name)
        schema = action_meta.get("input_schema") if isinstance(action_meta, dict) else None
        repo_path = schema.get("properties", {}).get("repo_path") if isinstance(schema, dict) else None
        if not isinstance(repo_path, dict) or repo_path.get("default") != "." or repo_path.get("type") != "string":
            raise RuntimeError(f"git-publisher {action_name} nested repo_path contract is missing")
    generic_release = publisher_action_by_name.get("release-assets")
    if not isinstance(generic_release, dict):
        raise RuntimeError("git-publisher generic release-assets action is missing")
    if generic_release.get("run_mode") != "job" or generic_release.get("timeout_seconds") != 7200:
        raise RuntimeError("git-publisher release-assets must be a two-hour host-owned Job")
    generic_schema = generic_release.get("input_schema")
    asset_properties = (
        generic_schema.get("properties", {}).get("assets", {}).get("items", {}).get("properties", {})
        if isinstance(generic_schema, dict)
        else {}
    )
    if "label" not in asset_properties:
        raise RuntimeError("git-publisher release-assets display-label contract is missing")

    skill_catalog = _json(executable, "skills", "--json")
    packs = skill_catalog.get("packs")
    if not isinstance(packs, list):
        raise RuntimeError("Skill catalog has no packs list")
    bundled_skill_packs = {
        str(item.get("id"))
        for item in packs
        if isinstance(item, dict) and item.get("bundled") is True
    }
    if bundled_skill_packs != EXPECTED_BUNDLED_SKILL_PACKS:
        raise RuntimeError(
            f"bundled Skill Pack set mismatch: expected {sorted(EXPECTED_BUNDLED_SKILL_PACKS)}, "
            f"got {sorted(bundled_skill_packs)}"
        )
    engineering = next(
        (item for item in packs if isinstance(item, dict) and item.get("id") == "folderbridge-engineering"),
        None,
    )
    if not isinstance(engineering, dict):
        raise RuntimeError("folderbridge-engineering is missing from Skill catalog")
    skills = engineering.get("skills")
    if not isinstance(skills, list):
        raise RuntimeError("folderbridge-engineering has no skills list")
    actual_skills = {
        str(item.get("id"))
        for item in skills
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if actual_skills != EXPECTED_ENGINEERING_SKILLS:
        raise RuntimeError(
            f"engineering Skill set mismatch: expected {sorted(EXPECTED_ENGINEERING_SKILLS)}, "
            f"got {sorted(actual_skills)}"
        )
    source = engineering.get("source")
    if not isinstance(source, dict):
        raise RuntimeError("folderbridge-engineering has no source attribution")
    if source.get("repository") != "https://github.com/mattpocock/skills" or source.get("license") != "MIT":
        raise RuntimeError("folderbridge-engineering upstream attribution/license mismatch")

    self_test = _json(executable, "extensions", "--self-test")
    if "comfyui" in self_test:
        raise RuntimeError("external ComfyUI must not participate in bundled Extension self-test")
    skill_engine = self_test.get("skill_engine")
    if not isinstance(skill_engine, dict):
        raise RuntimeError("bundled Skill Engine worker self-test failed")
    worker_packs = skill_engine.get("packs")
    if not isinstance(worker_packs, list) or not any(
        isinstance(item, dict) and item.get("id") == "folderbridge-engineering"
        for item in worker_packs
    ):
        raise RuntimeError("Skill Engine worker did not discover folderbridge-engineering")

    return {
        "version": version,
        "bundled_extensions": sorted(bundled_extensions),
        "bundled_skill_packs": sorted(bundled_skill_packs),
        "engineering_skills": sorted(actual_skills),
        "mcp_job_management": True,
        "mcp_continuity": continuity,
        "mcp_tool_surface": tool_surface,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: verify_windows_bundle.py <FolderBridge.exe>", file=sys.stderr)
        return 2
    try:
        result = verify(Path(args[0]))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"bundle verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
