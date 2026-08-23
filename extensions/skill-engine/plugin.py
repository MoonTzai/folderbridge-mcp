from __future__ import annotations

from typing import Any

from folderbridge_mcp.skills import SkillEngine


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    del context
    engine = SkillEngine()
    if action == "list":
        return engine.describe()
    if action == "match":
        return engine.match(params["task"], limit=int(params.get("limit", 3)))
    if action == "get":
        loaded = engine.get(
            params["skill_ref"],
            params["expected_sha256"],
            resource=params.get("resource"),
        )
        text = loaded.pop("text")
        loaded["_content"] = [{"type": "text", "text": text}]
        return loaded
    raise RuntimeError(f"unsupported action: {action}")
