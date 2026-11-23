"""Final artifact privacy gate for public FolderBridge Windows and plugin ZIPs.

Only release/windows-x64/FolderBridge.exe and release/external-extensions/
are scanned. Error messages report rule IDs and artifact names, not matched secrets.
This is a publication check, not a claim that software is a security sandbox.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "release"
PUBLIC_PLUGINS = (
    "blender-toolkit",
    "chatgpt-web-llm-adapter",
    "comfyui",
    "download-toolkit",
    "exe-packager-toolkit",
    "ffmpeg-toolkit",
    "ftp-toolkit",
    "godot-ai",
    "git-publisher",
    "gpt-sovits-local",
    "offline-asr-toolkit",
    "pdf-toolkit",
    "remotion-toolkit",
    "storyboard-chatgpt-web",
    "testonline-host",
    "windows-capture-toolkit",
)
PRIVATE_NAME = re.compile(r"(?:^|/)(?:local-private|secrets|credentials|profile[s]?\.json|memory|certificates|upload)(?:/|$)", re.I)
DENY_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
RULES = {
    "GitHub credential": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "API credential": re.compile(rb"(?:sk-(?:proj-)?[A-Za-z0-9_-]{30,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[0-9A-Za-z-]{30,})"),
    "Private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Developer path": re.compile(rb"[A-Za-z]:[\\/]+(?:Claude[\\/]+Project|MiniMax[\\/]+|Tools[\\/]+AI-Recorder)", re.I),
    "Private user path": re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]+[A-Za-z0-9_.-]+[\\/]+(?:Desktop|Documents|AppData|Downloads|OneDrive|\\.ssh)", re.I),
    "LAN host literal": re.compile(rb"(?<![0-9])192\.168\.(?!0\.0\b)[0-9]{1,3}\.[0-9]{1,3}(?![0-9])"),
}
TEXT = {".py", ".pyw", ".md", ".json", ".txt", ".ps1", ".cjs", ".js", ".ts", ".tsx", ".yml", ".yaml", ".xml", ".html", ".css"}
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def scan_bytes(name: str, data: bytes) -> None:
    for rule_id, rule in RULES.items():
        if rule.search(data):
            raise RuntimeError("Blocked release artifact: " + name + " violates " + rule_id)


def inspect() -> dict:
    exe_path = RELEASE / "windows-x64" / "FolderBridge.exe"
    if not exe_path.is_file():
        raise RuntimeError("Missing Windows EXE candidate")
    binary = exe_path.read_bytes()
    if len(binary) < 1_000_000 or binary[:2] != b"MZ":
        raise RuntimeError("Invalid Windows EXE candidate")
    scan_bytes("FolderBridge.exe", binary)
    plugin_dir = RELEASE / "external-extensions"
    actual = sorted(plugin_dir.glob("*.zip"))
    expected_names = []
    checked = []
    for slug in PUBLIC_PLUGINS:
        folder = ROOT / "Plugins" / "extensions" / slug
        m = json.loads((folder / "folderbridge-extension.json").read_text(encoding="utf-8"))
        if m["id"] != slug:
            raise RuntimeError("Unexpected extension ID: " + slug)
        fname = "FolderBridge-Plugin-" + slug + "-v" + m["version"] + ".zip"
        expected_names.append(fname)
        archive = plugin_dir / fname
        if not archive.is_file():
            raise RuntimeError("Missing public extension ZIP: " + fname)
        with zipfile.ZipFile(archive) as z:
            names = [x.filename.replace("\\", "/") for x in z.infolist() if not x.is_dir()]
            if len(names) != len(set(names)):
                raise RuntimeError("Duplicate ZIP entries: " + fname)
            for minimum in ("folderbridge-extension.json", "plugin.py", "README.md"):
                if minimum not in names:
                    raise RuntimeError("Missing required ZIP entry: " + fname + " / " + minimum)
            for zinfo in z.infolist():
                if zinfo.is_dir():
                    continue
                name = zinfo.filename.replace("\\", "/")
                if name.startswith("/") or name.startswith("../") or "/../" in name or PRIVATE_NAME.search(name):
                    raise RuntimeError("Forbidden release ZIP member: " + fname)
                if name.lower().endswith(DENY_SUFFIXES):
                    raise RuntimeError("Sensitive extension in ZIP: " + fname)
                if zinfo.file_size > 10_000_000:
                    raise RuntimeError("Oversize ZIP member: " + fname)
                if Path(name).suffix.lower() in TEXT:
                    body = z.read(zinfo)
                    scan_bytes(fname + "/" + name, body)
        checksum = (plugin_dir / (fname + ".sha256")).read_text(encoding="ascii").split()[0].lower()
        if digest(archive.read_bytes()) != checksum:
            raise RuntimeError("ZIP SHA sidecar mismatch: " + fname)
        checked.append({"name": fname, "sha256": checksum})
    if sorted(p.name for p in actual) != sorted(expected_names):
        raise RuntimeError("Public ZIP asset inventory differs from the explicit 16-plugin allowlist")
    receipt = {
        "status": "ARTIFACT_PRIVACY_PASS",
        "version": VERSION,
        "core": {"name": "FolderBridge.exe", "sha256": digest(binary), "bytes": len(binary)},
        "publicPluginCount": len(checked),
        "checked": checked,
        "notPackaged": ["local-private", "credentials", "profiles", "certificates", "private Judge adapter"],
    }
    out = RELEASE / "privacy"
    out.mkdir(parents=True, exist_ok=True)
    (out / "public-artifact-check.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return receipt


if __name__ == "__main__":
    record = inspect()
    print("PUBLIC_ARTIFACT_PRIVACY_PASS plugins=" + str(record["publicPluginCount"]) +
          " core_sha256=" + record["core"]["sha256"])
