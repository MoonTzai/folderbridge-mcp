from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, ContextManager, Any
from urllib.request import Request, urlopen


GITHUB_REPOSITORY = "MoonTzai/folderbridge-mcp"
LATEST_RELEASE_API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
LATEST_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases/latest"
MAX_RELEASE_RESPONSE_BYTES = 64 * 1024
DEFAULT_UPDATE_TIMEOUT_SECONDS = 4.0
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class UpdateCheckResult:
    status: str
    current_version: str
    latest_version: str | None
    releases_url: str
    detail: str | None = None

    @property
    def update_available(self) -> bool:
        return self.status == "update_available"


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def check_for_updates(
    current_version: str,
    *,
    opener: Callable[..., ContextManager[Any]] = urlopen,
    timeout_seconds: float = DEFAULT_UPDATE_TIMEOUT_SECONDS,
) -> UpdateCheckResult:
    current = _version_tuple(current_version)
    if current is None:
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            "current_version_invalid",
        )
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0.5 <= float(timeout_seconds) <= 15.0:
        raise ValueError("timeout_seconds must be between 0.5 and 15 seconds")

    request = Request(
        LATEST_RELEASE_API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"FolderBridge/{current_version} update-check",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with opener(request, timeout=float(timeout_seconds)) as response:
            payload = response.read(MAX_RELEASE_RESPONSE_BYTES + 1)
    except Exception as exc:
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            type(exc).__name__,
        )
    if len(payload) > MAX_RELEASE_RESPONSE_BYTES:
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            "response_too_large",
        )
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            "response_invalid_json",
        )
    if not isinstance(decoded, dict):
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            "response_not_object",
        )
    tag_name = decoded.get("tag_name")
    if not isinstance(tag_name, str):
        return UpdateCheckResult(
            "unavailable",
            current_version,
            None,
            LATEST_RELEASES_URL,
            "tag_missing",
        )
    latest = _version_tuple(tag_name)
    if latest is None:
        return UpdateCheckResult(
            "unavailable",
            current_version,
            tag_name,
            LATEST_RELEASES_URL,
            "tag_invalid",
        )
    normalized_latest = ".".join(str(part) for part in latest)
    if latest > current:
        return UpdateCheckResult(
            "update_available",
            current_version,
            normalized_latest,
            LATEST_RELEASES_URL,
        )
    return UpdateCheckResult(
        "current",
        current_version,
        normalized_latest,
        LATEST_RELEASES_URL,
    )
