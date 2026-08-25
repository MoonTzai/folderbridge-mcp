from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_BUNDLED_EXTENSIONS = {"comfyui", "git-publisher", "office", "skill-engine"}
EXPECTED_BUNDLED_SKILL_PACKS = {"folderbridge-engineering"}
EXPECTED_ENGINEERING_SKILLS = {
    "codebase-design",
    "improve-codebase-architecture",
    "diagnosing-bugs",
    "tdd",
    "code-review",
    "implement",
}
EXPECTED_EXTENSION_VERSIONS = {"git-publisher": "1.3.0", "office": "1.1.0"}


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


def verify(executable: Path) -> dict[str, Any]:
    executable = executable.resolve(strict=True)
    if not executable.is_file():
        raise RuntimeError("built executable is not a regular file")

    version = _run(executable, "--version").stdout.decode("utf-8", errors="strict").strip()
    expected_version = f"folderbridge-mcp {_project_version()}"
    if version != expected_version:
        raise RuntimeError(f"version smoke mismatch: expected {expected_version!r}, got {version!r}")

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
    legacy_release = publisher_action_by_name.get("release")
    generic_release = publisher_action_by_name.get("release-assets")
    if not isinstance(legacy_release, dict):
        raise RuntimeError("git-publisher compatibility release action is missing")
    legacy_schema = legacy_release.get("input_schema")
    if not isinstance(legacy_schema, dict) or legacy_schema.get("properties") != {}:
        raise RuntimeError("git-publisher compatibility release action is no longer parameterless")
    if not isinstance(generic_release, dict):
        raise RuntimeError("git-publisher generic release-assets action is missing")
    if generic_release.get("run_mode") != "job" or generic_release.get("timeout_seconds") != 7200:
        raise RuntimeError("git-publisher release-assets must be a two-hour host-owned Job")

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
    if self_test.get("comfyui", {}).get("extension_id") != "comfyui":
        raise RuntimeError("bundled ComfyUI worker self-test failed")
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
