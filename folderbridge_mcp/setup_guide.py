"""Stable values and small helpers used by the Windows setup guide."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


WINDOWS_X64_ASSET_PATTERN = "tunnel-client-v<版本>-windows-amd64.zip"
WINDOWS_X64_ASSET_GLOB = "tunnel-client-v*-windows-amd64.zip"
CHATGPT_INVOCATION_EXAMPLE = "请使用 FolderBridge 列出当前工作区根目录，并说明当前工作区与访问权限。"


def recommended_client_directory(
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> Path:
    """Return a non-admin, per-user folder for the portable tunnel client."""

    values = os.environ if environ is None else environ
    local_app_data = values.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "FolderBridge" / "bin"
    return (Path.home() if home is None else home) / ".folderbridge" / "bin"


def looks_like_tunnel_id(value: str) -> bool:
    """Accept the documented tunnel prefix without freezing the server's ID format."""

    candidate = value.strip()
    return candidate.startswith("tunnel_") and len(candidate) > len("tunnel_")
