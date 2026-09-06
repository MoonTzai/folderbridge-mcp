from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sys
from collections.abc import Mapping
from pathlib import Path


INTERNAL_CONFIG_ROOT_ENV = "FOLDERBRIDGE_CONFIG_ROOT"
INTERNAL_CONFIG_ROOT_MARKER_ENV = "FOLDERBRIDGE_CONFIG_ROOT_MARKER"
_INTERNAL_CONFIG_ROOT_MARKER_VERSION = "v1"
_INTERNAL_BOOT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_PROCESS_CHILD_BOOT_ID = secrets.token_hex(16)


def _canonical_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _os_user_config_root(
    values: Mapping[str, str],
    *,
    platform: str,
    home: Path | None,
) -> Path:
    if platform == "win32":
        local_app_data = values.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            base = Path(local_app_data)
        else:
            user_profile = values.get("USERPROFILE", "").strip()
            if user_profile:
                base = Path(user_profile) / "AppData" / "Local"
            else:
                resolved_home = Path.home() if home is None else home
                base = resolved_home / "AppData" / "Local"
        return _canonical_path(base / "folderbridge-mcp")

    xdg_config = values.get("XDG_CONFIG_HOME", "").strip()
    if xdg_config:
        base = Path(xdg_config)
    else:
        resolved_home = Path.home() if home is None else home
        base = resolved_home / ".config"
    return _canonical_path(base / "folderbridge-mcp")


def _internal_marker(root: Path, boot_id: str) -> str:
    canonical = str(_canonical_path(root))
    digest = hashlib.sha256(
        (
            "folderbridge-internal-config-root\\0"
            + _INTERNAL_CONFIG_ROOT_MARKER_VERSION
            + "\\0"
            + boot_id
            + "\\0"
            + canonical
        ).encode("utf-8", errors="strict")
    ).hexdigest()
    return f"{_INTERNAL_CONFIG_ROOT_MARKER_VERSION}:{boot_id}:{digest}"


def internal_child_config_root_environment(
    root: Path,
    *,
    boot_id: str | None = None,
) -> dict[str, str]:
    """Build the reserved parent-to-child config-root propagation contract."""

    resolved_boot_id = _PROCESS_CHILD_BOOT_ID if boot_id is None else boot_id
    if not isinstance(resolved_boot_id, str) or not _INTERNAL_BOOT_ID_RE.fullmatch(resolved_boot_id):
        raise ValueError("internal config-root boot_id must be 32 lowercase hex characters")
    canonical = _canonical_path(Path(root))
    return {
        INTERNAL_CONFIG_ROOT_ENV: str(canonical),
        INTERNAL_CONFIG_ROOT_MARKER_ENV: _internal_marker(canonical, resolved_boot_id),
    }


def _validated_internal_config_root(values: Mapping[str, str]) -> Path | None:
    raw_root = values.get(INTERNAL_CONFIG_ROOT_ENV, "").strip()
    marker = values.get(INTERNAL_CONFIG_ROOT_MARKER_ENV, "").strip()
    if not raw_root or not marker:
        return None
    parts = marker.split(":")
    if len(parts) != 3:
        return None
    version, boot_id, digest = parts
    if (
        version != _INTERNAL_CONFIG_ROOT_MARKER_VERSION
        or not _INTERNAL_BOOT_ID_RE.fullmatch(boot_id)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        return None
    canonical = _canonical_path(Path(raw_root))
    if not hmac.compare_digest(marker, _internal_marker(canonical, boot_id)):
        return None
    return canonical


def require_internal_child_config_root(
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Fail closed when an internal child lacks a valid parent root marker."""

    values = os.environ if environ is None else environ
    root = _validated_internal_config_root(values)
    if root is None:
        raise ValueError("missing or invalid internal FolderBridge config-root marker")
    return root


def clear_internal_config_root_environment(environ: dict[str, str] | None = None) -> None:
    """Remove reserved child-only propagation variables from a top-level process."""

    values = os.environ if environ is None else environ
    values.pop(INTERNAL_CONFIG_ROOT_ENV, None)
    values.pop(INTERNAL_CONFIG_ROOT_MARKER_ENV, None)


def user_config_root(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
    test_root: Path | None = None,
) -> Path:
    """Return FolderBridge's canonical per-user configuration root.

    Extension workers receive ``FOLDERBRIDGE_CONFIG_ROOT`` from the host so a
    cleaned worker environment cannot accidentally choose a different profile
    directory from the launcher/MCP process.
    """

    if test_root is not None:
        return _canonical_path(Path(test_root))

    values = os.environ if environ is None else environ
    internal = _validated_internal_config_root(values)
    if internal is not None:
        return internal
    current_platform = sys.platform if platform is None else platform
    return _os_user_config_root(values, platform=current_platform, home=home)
