from __future__ import annotations

from typing import Any

from .process_control import owned_process_group_kwargs, terminate_owned_process_tree


__all__ = (
    "ExtensionError",
    "owned_process_group_kwargs",
    "terminate_owned_process_tree",
)


class ExtensionError(Exception):
    """Stable public error ABI for out-of-process FolderBridge extensions."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("ExtensionError code must be a non-empty string")
        if not isinstance(message, str) or not message:
            raise ValueError("ExtensionError message must be a non-empty string")
        self.code = code.strip()
        self.message = message
        self.details = dict(details)
        super().__init__(message)
