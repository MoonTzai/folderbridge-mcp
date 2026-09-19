from __future__ import annotations

from pathlib import Path
from typing import Any

from folderbridge_mcp.extension_api import ExtensionError

from browser_runtime import connection_info, run_service


def _state_dir(context: dict[str, Any]) -> Path:
    value = context.get("state_dir")
    if not isinstance(value, str) or not value:
        raise ExtensionError("STATE_UNAVAILABLE", "This adapter requires FolderBridge extension.state storage.", retryable=False)
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True)
    return path


def handle(action: str, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    state_dir = _state_dir(context)
    if action == "status":
        return connection_info(state_dir, include_token=False)
    if action == "connection":
        # 0.3.2 keeps the one-run bearer token on the local human control plane.
        # MCP/Extension callers receive connection metadata but never the secret.
        return connection_info(state_dir, include_token=False)
    if action == "verification-plan":
        return {
            "ok": True,
            "steps": [
                "Primary path: use FolderBridge Extensions & Skills to manage Standalone 0.3.2 on 127.0.0.1:8769 with dedicated 8770 DevTools; repo CMD launchers remain recovery fallbacks.",
                "Optional Extension path: run serve as a host-owned Job on 127.0.0.1:8767/8768.",
                "Complete ChatGPT login/security checks in the dedicated visible browser if ready is false.",
                "Configure the client with the displayed base_url, one-run api_key and model.",
                "Run low-sample semantic acceptance. Every HTTP completion uses a fresh ChatGPT conversation.",
            ],
            "contract": {
                "test_only": True,
                "openai_compatible_paths": ["GET /v1/models", "POST /v1/chat/completions"],
                "streaming": "SSE compatibility response after the web turn completes; not token-live streaming",
                "system_semantics": "prompt_wrapped",
                "json_mode": "response_format=json_object is strictly validated as one JSON object; only non-semantic code-block display wrappers are normalized",
                "fresh_chat_per_request": True,
                "allowed_browser_origins": ["null (file://)", "localhost", "127.0.0.1"],
                "official_api_equivalence": False,
                "standalone_primary": True,
                "standalone_base_url": "http://127.0.0.1:8769/v1",
                "default_max_parallel": 2,
                "hard_max_parallel": 4,
                "fresh_chat_min_interval_seconds": 20,
                "fresh_chat_rolling_window": "8 starts per 300 seconds",
                "history_rate_limit_cooldown": "120/240/480/600 seconds exponential cooldown",
                "login_page_reuse": True,
                "temporary_chat_enforced": False,
            },
        }
    if action == "serve":
        return run_service(
            state_dir,
            context.get("job_cancel_path"),
            max_parallel=params.get("max_parallel", 2),
            response_timeout_seconds=params.get("response_timeout_seconds", 600),
        )
    raise ExtensionError("EXTENSION_ACTION_NOT_FOUND", f"Unsupported ChatGPT Web LLM Adapter action: {action}", retryable=False)
