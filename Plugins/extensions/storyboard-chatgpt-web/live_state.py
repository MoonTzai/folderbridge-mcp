from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


HEX64 = frozenset("0123456789abcdef")


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in HEX64 for ch in value)


def candidate_state_path(run_dir: Path, frame_id: str, attempt: int) -> Path:
    safe = hashlib.sha256(frame_id.encode("utf-8")).hexdigest()[:20]
    return run_dir / "candidates" / f"{safe}-{attempt}.json"


def judge_state_path(run_dir: Path, frame_id: str, attempt: int) -> Path:
    safe = hashlib.sha256(frame_id.encode("utf-8")).hexdigest()[:20]
    return run_dir / "judges" / f"{safe}-{attempt}.json"


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_candidate(run_dir: Path, frame_id: str, attempt: int) -> dict[str, Any] | None:
    return read_json(candidate_state_path(run_dir, frame_id, attempt))


def read_judge(run_dir: Path, frame_id: str, attempt: int) -> dict[str, Any] | None:
    return read_json(judge_state_path(run_dir, frame_id, attempt))


def immediate_failed_substrate(run_dir: Path, frame_id: str, attempt: int) -> dict[str, Any] | None:
    if attempt <= 1:
        return None
    prior = read_candidate(run_dir, frame_id, attempt - 1)
    if not prior or prior.get("status") != "failed" or not is_sha256(prior.get("sha256")):
        return None
    return prior
