from __future__ import annotations

import json
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

from .config import Task
from .security import ToolError, Workspace, resolve_task_executable
from .task_runner import run_task


MAX_SCANNED_FILES = 5_000
MAX_READ_BYTES = 64 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 16 * 1024 * 1024
MAX_JS_SYNTAX_CHECKS = 25
SCAN_SECONDS = 20.0
MAX_ISSUES = 50
MAX_DELIVERABLES = 50

TEXT_SUFFIXES = {
    ".bcc", ".css", ".csv", ".htm", ".html", ".js", ".json", ".md", ".mjs",
    ".cjs", ".py", ".srt", ".toml", ".txt", ".xml", ".yaml", ".yml",
}
CODE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cs", ".go", ".java", ".js", ".jsx", ".mjs",
    ".cjs", ".php", ".py", ".rb", ".rs", ".swift", ".ts", ".tsx",
}
CONTENT_SUFFIXES = {
    ".bcc", ".csv", ".docx", ".md", ".pdf", ".pptx", ".srt", ".txt", ".xlsx",
}
DELIVERABLE_SUFFIXES = CONTENT_SUFFIXES | {".htm", ".html"}


class _HtmlProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._script = False
        self._script_is_js = False
        self._script_is_module = False
        self._parts: list[str] = []
        self.inline_scripts: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script":
            return
        attr = {key.lower(): (value or "") for key, value in attrs}
        if attr.get("src"):
            return
        script_type = attr.get("type", "").strip().lower()
        self._script = True
        self._script_is_js = script_type in {"", "text/javascript", "application/javascript", "module"}
        self._script_is_module = script_type == "module"
        self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or not self._script:
            return
        if self._script_is_js:
            self.inline_scripts.append(("".join(self._parts), self._script_is_module))
        self._script = False
        self._script_is_js = False
        self._script_is_module = False
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._script and self._script_is_js:
            self._parts.append(data)


def _skip_validation_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part.startswith(".") for part in relative.parts):
        return True
    lowered = path.name.lower()
    return lowered.endswith((".bak", ".tmp")) or ".bak." in lowered or lowered.startswith("tmp-")


def _node_executable(root: Path) -> str | None:
    try:
        return resolve_task_executable("node", root)
    except ToolError:
        return None


def _node_check(root: Path, node: str, target: Path, *, timeout: int = 10) -> str | None:
    result = run_task(root, Task("builtin-js-syntax-check", (node, "--check", str(target)), timeout))
    if result.get("exit_code") == 0 and not result.get("timed_out"):
        return None
    stderr = str(result.get("stderr") or "").strip()
    if result.get("timed_out"):
        return "JavaScript syntax check timed out"
    return stderr[:1000] or "JavaScript syntax check failed"


def _profile(*, files: int, html_files: int, code_files: int, content_files: int) -> str:
    if files == 0:
        return "empty"
    if code_files:
        return "source"
    if html_files and content_files <= max(4, html_files * 4):
        return "static-web"
    if html_files or content_files:
        return "content"
    return "collection"


def run_workspace_smoke(root: Path) -> dict[str, object]:
    workspace = Workspace(root)
    deadline = time.monotonic() + SCAN_SECONDS
    issues: list[str] = []
    deliverables: list[str] = []
    scanned = 0
    checked_text = 0
    read_bytes = 0
    html_files = 0
    json_files = 0
    code_files = 0
    content_files = 0
    js_checks = 0
    js_skipped = 0
    truncated = False
    node = _node_executable(root)

    def issue(message: str) -> None:
        if len(issues) < MAX_ISSUES:
            issues.append(message)

    for path in workspace.iter_files("."):
        if scanned >= MAX_SCANNED_FILES or time.monotonic() > deadline or read_bytes >= MAX_READ_BYTES:
            truncated = True
            break
        if _skip_validation_file(path, root):
            continue
        scanned += 1
        relative = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix in CODE_SUFFIXES:
            code_files += 1
        if suffix in CONTENT_SUFFIXES:
            content_files += 1
        if suffix in DELIVERABLE_SUFFIXES and len(deliverables) < MAX_DELIVERABLES:
            deliverables.append(relative)
        if suffix == ".html" or suffix == ".htm":
            html_files += 1
        if suffix == ".json":
            json_files += 1
        if suffix not in TEXT_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            issue(f"{relative}: could not stat file ({type(exc).__name__})")
            continue
        if size > MAX_TEXT_FILE_BYTES:
            continue
        if read_bytes + size > MAX_READ_BYTES:
            truncated = True
            break
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            issue(f"{relative}: text file is not valid UTF-8")
            continue
        except OSError as exc:
            issue(f"{relative}: could not read file ({type(exc).__name__})")
            continue
        read_bytes += len(data)
        checked_text += 1

        if suffix == ".json":
            try:
                json.loads(text)
            except (json.JSONDecodeError, ValueError) as exc:
                issue(f"{relative}: invalid JSON ({exc})")

        inline_scripts: list[tuple[str, bool]] = []
        if suffix in {".html", ".htm"}:
            try:
                parser = _HtmlProbe()
                parser.feed(text)
                parser.close()
                inline_scripts = parser.inline_scripts
            except Exception as exc:
                issue(f"{relative}: HTML parse failed ({type(exc).__name__})")

        if node and suffix in {".js", ".mjs", ".cjs"} and js_checks < MAX_JS_SYNTAX_CHECKS:
            failure = _node_check(root, node, path)
            js_checks += 1
            if failure:
                issue(f"{relative}: {failure}")
        elif suffix in {".js", ".mjs", ".cjs"}:
            js_skipped += 1

        for index, (script, is_module) in enumerate(inline_scripts, start=1):
            if not script.strip():
                continue
            if not node or js_checks >= MAX_JS_SYNTAX_CHECKS or time.monotonic() > deadline:
                js_skipped += 1
                continue
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", suffix=".mjs" if is_module else ".js", delete=False
                ) as handle:
                    temporary = handle.name
                    handle.write(script)
                failure = _node_check(root, node, Path(temporary))
                js_checks += 1
                if failure:
                    issue(f"{relative} inline script #{index}: {failure}")
            finally:
                if temporary:
                    try:
                        Path(temporary).unlink()
                    except OSError:
                        pass

    profile = _profile(files=scanned, html_files=html_files, code_files=code_files, content_files=content_files)
    summary = (
        f"FolderBridge workspace smoke: profile={profile}; scanned={scanned}; "
        f"text={checked_text}; html={html_files}; json={json_files}; js_checks={js_checks}; "
        f"issues={len(issues)}; truncated={str(truncated).lower()}"
    )
    return {
        "task": "capability-test",
        "provider": "builtin-workspace-smoke",
        "provider_kind": "builtin",
        "profile": profile,
        "exit_code": 1 if issues else 0,
        "timed_out": False,
        "stdout": summary,
        "stderr": "\n".join(issues),
        "stdout_total_bytes": len(summary.encode("utf-8")),
        "stderr_total_bytes": len("\n".join(issues).encode("utf-8")),
        "truncated": truncated,
        "issues": issues,
        "deliverables": deliverables,
        "checks": {
            "scanned_files": scanned,
            "checked_text_files": checked_text,
            "html_files": html_files,
            "json_files": json_files,
            "js_syntax_checks": js_checks,
            "js_syntax_skipped": js_skipped,
            "node_available": bool(node),
            "read_bytes": read_bytes,
            "max_scanned_files": MAX_SCANNED_FILES,
            "max_read_bytes": MAX_READ_BYTES,
        },
    }


def run_safe_build(root: Path) -> dict[str, object]:
    smoke = run_workspace_smoke(root)
    profile = str(smoke["profile"])
    build_mode = "validation-only" if profile == "source" else "identity"
    summary = (
        f"FolderBridge safe build: mode={build_mode}; profile={profile}; "
        "no project build entry point was detected; no artifacts were generated. "
        f"Underlying smoke exit={smoke['exit_code']}."
    )
    stdout = summary + "\n" + str(smoke.get("stdout") or "")
    return {
        **smoke,
        "task": "capability-build",
        "provider": "builtin-safe-build",
        "build_mode": build_mode,
        "generated_artifacts": False,
        "stdout": stdout,
        "stdout_total_bytes": len(stdout.encode("utf-8")),
    }
