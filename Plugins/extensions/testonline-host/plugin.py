from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError

_ENDPOINT = "http://127.0.0.1:18889/status"
_MAX_BYTES = 131_072


def _status() -> dict[str, Any]:
    try:
        request = urllib.request.Request(_ENDPOINT, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=2.0) as response:
            if response.status != 200:
                raise ValueError("control endpoint returned HTTP " + str(response.status))
            data = response.read(_MAX_BYTES + 1)
            if len(data) > _MAX_BYTES:
                raise ValueError("oversized status")
            result = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "online": False,
            "service_id": "testonline-host",
            "status": "OFFLINE",
            "detail": str(exc)[:240],
            "sites": [],
        }
    if not isinstance(result, dict) or result.get("service_id") != "testonline-host":
        raise ExtensionError("TESTONLINE_INVALID_STATUS", "Unexpected local service identity")
    return result


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if action not in {"status", "sites"}:
        raise ExtensionError("EXTENSION_ACTION_NOT_FOUND", "Unsupported TestOnline action")
    status = _status()
    if action == "sites":
        return {
            "service_id": "testonline-host",
            "online": bool(status.get("online")),
            "sites": status.get("sites", []),
            "errors": status.get("errors", []),
            "scanned_at": status.get("scanned_at"),
        }
    return status
