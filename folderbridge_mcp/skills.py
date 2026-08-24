from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .security import ToolError
from .user_paths import user_config_root


SKILL_PACK_SCHEMA_VERSION = 1
SKILL_TRUST_VERSION = 1
SKILL_PACK_MANIFEST = "folderbridge-skill-pack.json"
MAX_SKILL_MANIFEST_BYTES = 1024 * 1024
MAX_SKILL_TEXT_BYTES = 128 * 1024
MAX_SKILL_PACK_FILES = 512
MAX_SKILL_PACK_BYTES = 4 * 1024 * 1024
MAX_SKILL_PACKS_PER_ROOT = 128
MAX_SKILLS_PER_PACK = 128
MAX_ROUTING_TERMS = 64
MAX_ROUTING_TERM_CHARS = 120
MAX_MATCH_LIMIT = 5
PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ASCII_WORD_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$", re.IGNORECASE)
IGNORED_TREE_PARTS = {"__pycache__", ".git", ".svn"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")


@dataclass(frozen=True)
class SkillSource:
    repository: str
    ref: str
    commit: str
    license: str


@dataclass(frozen=True)
class SkillDocument:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SkillRecord:
    skill_id: str
    name: str
    description: str
    document: SkillDocument
    routing_terms: tuple[str, ...]
    resources: tuple[SkillDocument, ...]

    @property
    def resource_by_path(self) -> dict[str, SkillDocument]:
        return {item.path: item for item in self.resources}


@dataclass(frozen=True)
class SkillPackRecord:
    path: Path
    pack_id: str
    name: str
    version: str
    description: str
    source: SkillSource
    skills: tuple[SkillRecord, ...]
    sha256: str
    bundled: bool

    @property
    def skill_by_id(self) -> dict[str, SkillRecord]:
        return {item.skill_id: item for item in self.skills}


class _SkillTrustStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _empty(self) -> dict[str, Any]:
        return {"version": SKILL_TRUST_VERSION, "external": {}, "bundled_disabled": []}

    def _load(self) -> dict[str, Any]:
        try:
            if self.path.is_symlink() or _is_reparse_point(self.path):
                return self._empty()
            with self.path.open("rb") as handle:
                data = handle.read(MAX_SKILL_MANIFEST_BYTES + 1)
            if len(data) > MAX_SKILL_MANIFEST_BYTES:
                return self._empty()
            raw = json.loads(data, parse_constant=_reject_json_constant)
        except (OSError, UnicodeDecodeError, ValueError):
            return self._empty()
        version = raw.get("version") if isinstance(raw, dict) else None
        if not isinstance(version, int) or isinstance(version, bool) or version != SKILL_TRUST_VERSION:
            return self._empty()
        if set(raw).difference({"version", "external", "bundled_disabled"}):
            return self._empty()
        external = raw.get("external")
        disabled = raw.get("bundled_disabled")
        if not isinstance(external, dict) or not isinstance(disabled, list):
            return self._empty()
        cleaned_external: dict[str, dict[str, Any]] = {}
        for pack_id, value in external.items():
            if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id) or not isinstance(value, dict):
                continue
            sha256 = value.get("sha256")
            enabled = value.get("enabled")
            if (
                set(value) == {"sha256", "enabled"}
                and isinstance(sha256, str)
                and re.fullmatch(r"[0-9a-f]{64}", sha256)
                and isinstance(enabled, bool)
            ):
                cleaned_external[pack_id] = {"sha256": sha256, "enabled": enabled}
        cleaned_disabled = sorted({
            item for item in disabled
            if isinstance(item, str) and PACK_ID_RE.fullmatch(item)
        })
        return {
            "version": SKILL_TRUST_VERSION,
            "external": cleaned_external,
            "bundled_disabled": cleaned_disabled,
        }

    def _save(self, raw: dict[str, Any]) -> None:
        data = json.dumps(
            raw,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as handle:
                temporary = handle.name
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
        finally:
            if temporary:
                try:
                    Path(temporary).unlink()
                except FileNotFoundError:
                    pass

    def status(self, record: SkillPackRecord) -> dict[str, bool]:
        with self._lock:
            raw = self._load()
            if record.bundled:
                enabled = record.pack_id not in set(raw["bundled_disabled"])
                return {"trusted": True, "enabled": enabled, "approval_stale": False}
            saved = raw["external"].get(record.pack_id)
            trusted = bool(saved and saved.get("sha256") == record.sha256)
            return {
                "trusted": trusted,
                "enabled": bool(trusted and saved and saved.get("enabled")),
                "approval_stale": bool(saved and not trusted),
            }

    def approve_external(self, record: SkillPackRecord) -> None:
        if record.bundled:
            raise ValueError("bundled Skill Packs do not use external approval records")
        with self._lock:
            raw = self._load()
            raw["external"][record.pack_id] = {"sha256": record.sha256, "enabled": True}
            self._save(raw)

    def set_enabled(self, record: SkillPackRecord, enabled: bool) -> None:
        with self._lock:
            raw = self._load()
            if record.bundled:
                disabled = set(raw["bundled_disabled"])
                if enabled:
                    disabled.discard(record.pack_id)
                else:
                    disabled.add(record.pack_id)
                raw["bundled_disabled"] = sorted(disabled)
                self._save(raw)
                return
            saved = raw["external"].get(record.pack_id)
            if saved is None or saved.get("sha256") != record.sha256:
                raise ToolError("SKILL_PACK_NOT_TRUSTED", "Skill Pack must be approved at its current hash before it can be enabled.")
            saved["enabled"] = bool(enabled)
            raw["external"][record.pack_id] = saved
            self._save(raw)

    def revoke_external(self, pack_id: str) -> None:
        with self._lock:
            raw = self._load()
            raw["external"].pop(pack_id, None)
            self._save(raw)


class SkillEngine:
    """Deep module for bounded Skill Pack discovery, trust, matching, and text loading."""

    def __init__(
        self,
        *,
        bundled_root: Path | None = None,
        user_root: Path | None = None,
        trust_path: Path | None = None,
    ) -> None:
        self.bundled_root = bundled_root or bundled_skill_pack_root()
        self.user_root = user_root or skill_pack_root_path()
        self._trust = _SkillTrustStore(trust_path or skill_trust_path())

    def _scan(self) -> tuple[dict[str, SkillPackRecord], list[dict[str, str]]]:
        records: dict[str, SkillPackRecord] = {}
        errors: list[dict[str, str]] = []
        for root, bundled in ((self.bundled_root, True), (self.user_root, False)):
            try:
                if not root.is_dir():
                    children: list[Path] = []
                else:
                    children = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold())
            except OSError as exc:
                errors.append({"code": "SKILL_ROOT_SCAN_FAILED", "path": str(root), "error": str(exc)})
                continue
            if len(children) > MAX_SKILL_PACKS_PER_ROOT:
                errors.append({
                    "code": "SKILL_PACK_ROOT_LIMIT",
                    "path": str(root),
                    "error": f"root exceeds {MAX_SKILL_PACKS_PER_ROOT} Skill Packs",
                })
                children = children[:MAX_SKILL_PACKS_PER_ROOT]
            for child in children:
                try:
                    record = _load_pack(child, bundled=bundled)
                except (OSError, ValueError) as exc:
                    errors.append({"code": "SKILL_PACK_INVALID", "path": str(child), "error": str(exc)})
                    continue
                if record.pack_id in records:
                    errors.append({
                        "code": "SKILL_PACK_DUPLICATE",
                        "path": str(child),
                        "error": f"duplicate Skill Pack id: {record.pack_id}",
                    })
                    continue
                records[record.pack_id] = record
        return records, errors

    def describe(self, *, include_untrusted: bool = False) -> dict[str, Any]:
        records, errors = self._scan()
        rendered: list[dict[str, Any]] = []
        for pack_id in sorted(records):
            record = records[pack_id]
            status = self._trust.status(record)
            if not include_untrusted and not (status["trusted"] and status["enabled"]):
                continue
            rendered.append(self._render_pack(record, status, include_skills=True))
        if include_untrusted:
            rendered_errors = errors
        else:
            counts: dict[str, int] = {}
            for item in errors:
                code = item["code"]
                counts[code] = counts.get(code, 0) + 1
            rendered_errors = [{"code": code, "count": counts[code]} for code in sorted(counts)]
        return {
            "packs": rendered,
            "error_count": len(errors),
            "errors": rendered_errors,
        }

    def match(self, task: str, *, limit: int = 3) -> dict[str, Any]:
        if not isinstance(task, str) or not task.strip() or len(task) > 16_000:
            raise ToolError("INVALID_ARGUMENT", "task must be a non-empty string <= 16000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_MATCH_LIMIT:
            raise ToolError("INVALID_ARGUMENT", f"limit must be 1..{MAX_MATCH_LIMIT}")
        normalized = _normalize_text(task)
        records, _errors = self._scan()
        matches: list[dict[str, Any]] = []
        for pack_id in sorted(records):
            pack = records[pack_id]
            status = self._trust.status(pack)
            if not (status["trusted"] and status["enabled"]):
                continue
            for skill in pack.skills:
                matched_terms: list[str] = []
                score = 0
                for term in skill.routing_terms:
                    if _term_matches(normalized, term):
                        matched_terms.append(term)
                        score += _term_score(term)
                if score <= 0:
                    continue
                matches.append(self._render_match(pack, skill, score=score, matched_terms=matched_terms))
        matches.sort(key=lambda item: (-item["score"], item["skill_ref"]))
        return {"task": task, "matches": matches[:limit]}

    def get(self, skill_ref: str, expected_sha256: str, *, resource: str | None = None) -> dict[str, Any]:
        if not isinstance(skill_ref, str) or "/" not in skill_ref:
            raise ToolError("INVALID_ARGUMENT", "skill_ref must be <pack-id>/<skill-id>")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ToolError("INVALID_ARGUMENT", "expected_sha256 must be a lowercase SHA-256 hex digest")
        pack_id, skill_id = skill_ref.split("/", 1)
        records, _errors = self._scan()
        pack = records.get(pack_id)
        if pack is None:
            raise ToolError("SKILL_NOT_FOUND", "Skill is not installed.", skill_ref=skill_ref)
        status = self._trust.status(pack)
        if not (status["trusted"] and status["enabled"]):
            raise ToolError("SKILL_PACK_NOT_TRUSTED", "Skill Pack is not trusted and enabled.", skill_ref=skill_ref)
        skill = pack.skill_by_id.get(skill_id)
        if skill is None:
            raise ToolError("SKILL_NOT_FOUND", "Skill is not installed.", skill_ref=skill_ref)
        if resource is None:
            document = skill.document
            kind = "skill"
        else:
            if not isinstance(resource, str) or not resource:
                raise ToolError("INVALID_ARGUMENT", "resource must be a declared resource path")
            document = skill.resource_by_path.get(resource)
            if document is None:
                raise ToolError("SKILL_RESOURCE_NOT_DECLARED", "Resource is not declared for this Skill.", resource=resource)
            kind = "resource"
        path = _safe_pack_file(pack.path, document.path, max_bytes=MAX_SKILL_TEXT_BYTES)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_SKILL_TEXT_BYTES + 1)
        except OSError as exc:
            raise ToolError("SKILL_READ_FAILED", f"Could not read Skill content: {exc}") from exc
        if len(data) > MAX_SKILL_TEXT_BYTES:
            raise ToolError(
                "SKILL_TEXT_TOO_LARGE",
                f"Skill content exceeds {MAX_SKILL_TEXT_BYTES} bytes.",
                skill_ref=skill_ref,
                path=document.path,
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != document.sha256 or digest != expected_sha256:
            raise ToolError(
                "SKILL_CHANGED",
                "Skill content changed after it was selected; reload routing metadata before using it.",
                skill_ref=skill_ref,
                expected_sha256=expected_sha256,
                actual_sha256=digest,
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError("SKILL_INVALID_TEXT", "Skill content must be UTF-8 text.") from exc
        return {
            "skill_ref": skill_ref,
            "kind": kind,
            "path": document.path,
            "text": text,
            "size": len(data),
            "sha256": digest,
            "pack": self._render_pack(pack, status, include_skills=False),
        }

    def routing_index(self, *, max_chars: int = 4096) -> str:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not 128 <= max_chars <= 262_144:
            raise ValueError("max_chars must be 128..262144")
        header = (
            "Local Skill Engine: use extension id 'skill-engine' action 'match' for methodology routing, "
            "then action 'get' with the returned skill_ref and sha256. The compact index below is only a routing hint; "
            "for any non-trivial task not clearly covered, call 'match' anyway so installed Skills omitted from this index remain discoverable.\n"
        )
        description = self.describe()
        packs = [list(pack["skills"]) for pack in description["packs"]]
        total_skills = sum(len(skills) for skills in packs)
        lines = [header]
        current_chars = len(header)
        included = 0
        footer_reserve = 180
        depth = 0
        while any(depth < len(skills) for skills in packs):
            for skills in packs:
                if depth >= len(skills):
                    continue
                skill = skills[depth]
                terms = ", ".join(skill["routing_terms"][:8])
                line = f"- {skill['skill_ref']} [terms: {terms}]\n"
                remaining_after = total_skills - (included + 1)
                reserve = footer_reserve if remaining_after > 0 else 0
                if current_chars + len(line) + reserve > max_chars:
                    continue
                lines.append(line)
                current_chars += len(line)
                included += 1
            depth += 1
        omitted = total_skills - included
        if omitted:
            footer = (
                f"- routing index truncated: {omitted} of {total_skills} enabled Skills omitted from initialization; "
                "use skill-engine match for task-specific discovery.\n"
            )
            if current_chars + len(footer) <= max_chars:
                lines.append(footer)
            else:
                short_footer = f"- routing index truncated: {omitted} Skills omitted; use skill-engine match.\n"
                room = max_chars - current_chars
                if room > 0:
                    lines.append(short_footer[:room])
        return "".join(lines)[:max_chars]

    def approve_pack(self, pack_id: str, expected_sha256: str) -> dict[str, Any]:
        if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id):
            raise ToolError("INVALID_ARGUMENT", "pack_id is invalid")
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ToolError("INVALID_ARGUMENT", "expected_sha256 must be a lowercase SHA-256 hex digest")
        records, _errors = self._scan()
        record = records.get(pack_id)
        if record is None:
            raise ToolError("SKILL_PACK_NOT_FOUND", "Skill Pack is not installed.", pack_id=pack_id)
        if record.bundled:
            raise ToolError("SKILL_PACK_BUNDLED", "Bundled Skill Packs do not require hash approval.", pack_id=pack_id)
        if record.sha256 != expected_sha256:
            raise ToolError(
                "SKILL_PACK_CHANGED",
                "Skill Pack changed after the displayed hash; review the new hash before approving.",
                expected_sha256=expected_sha256,
                actual_sha256=record.sha256,
            )
        self._trust.approve_external(record)
        return self._render_pack(record, self._trust.status(record), include_skills=True)

    def set_enabled(self, pack_id: str, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ToolError("INVALID_ARGUMENT", "enabled must be boolean")
        records, _errors = self._scan()
        record = records.get(pack_id)
        if record is None:
            raise ToolError("SKILL_PACK_NOT_FOUND", "Skill Pack is not installed.", pack_id=pack_id)
        self._trust.set_enabled(record, enabled)
        return self._render_pack(record, self._trust.status(record), include_skills=True)

    def revoke_pack(self, pack_id: str) -> None:
        records, _errors = self._scan()
        record = records.get(pack_id)
        if record is not None and record.bundled:
            raise ToolError("SKILL_PACK_BUNDLED", "Bundled Skill Packs cannot be revoked; disable them instead.")
        self._trust.revoke_external(pack_id)

    @staticmethod
    def _render_pack(record: SkillPackRecord, status: dict[str, bool], *, include_skills: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": record.pack_id,
            "name": record.name,
            "version": record.version,
            "description": record.description,
            "sha256": record.sha256,
            "bundled": record.bundled,
            **status,
            "source": {
                "repository": record.source.repository,
                "ref": record.source.ref,
                "commit": record.source.commit,
                "license": record.source.license,
            },
            "skill_count": len(record.skills),
        }
        if include_skills:
            result["skills"] = [SkillEngine._render_skill(record, item) for item in record.skills]
        return result

    @staticmethod
    def _render_skill(pack: SkillPackRecord, skill: SkillRecord) -> dict[str, Any]:
        return {
            "skill_ref": f"{pack.pack_id}/{skill.skill_id}",
            "id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "path": skill.document.path,
            "size": skill.document.size,
            "sha256": skill.document.sha256,
            "routing_terms": list(skill.routing_terms),
            "resources": [
                {"path": item.path, "size": item.size, "sha256": item.sha256}
                for item in skill.resources
            ],
        }

    @staticmethod
    def _render_match(pack: SkillPackRecord, skill: SkillRecord, *, score: int, matched_terms: list[str]) -> dict[str, Any]:
        return {
            **SkillEngine._render_skill(pack, skill),
            "pack_id": pack.pack_id,
            "pack_version": pack.version,
            "pack_sha256": pack.sha256,
            "source": {
                "repository": pack.source.repository,
                "ref": pack.source.ref,
                "commit": pack.source.commit,
                "license": pack.source.license,
            },
            "score": score,
            "matched_terms": matched_terms,
        }


def skill_pack_root_path() -> Path:
    return user_config_root() / "skill-packs"


def skill_trust_path() -> Path:
    return user_config_root() / "skill-packs-trust.json"


def bundled_skill_pack_root() -> Path:
    if getattr(sys, "frozen", False):
        frozen_root = getattr(sys, "_MEIPASS", None)
        if frozen_root:
            return Path(frozen_root) / "skill_packs"
    return Path(__file__).resolve().parents[1] / "skill_packs"


def _load_pack(path: Path, *, bundled: bool) -> SkillPackRecord:
    if path.is_symlink() or _is_reparse_point(path):
        raise ValueError("Skill Pack directory may not be a link or reparse point")
    root = path.resolve(strict=True)
    manifest_path = root / SKILL_PACK_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink() or _is_reparse_point(manifest_path):
        raise ValueError(f"missing regular {SKILL_PACK_MANIFEST}")
    sha256, manifest_data, snapshot = _hash_pack(root)
    if len(manifest_data) > MAX_SKILL_MANIFEST_BYTES:
        raise ValueError("Skill Pack manifest is too large")
    try:
        raw = json.loads(manifest_data, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Skill Pack manifest must be strict UTF-8 JSON") from exc
    return _parse_pack_manifest(raw, root=root, bundled=bundled, sha256=sha256, snapshot=snapshot)


def _parse_pack_manifest(
    raw: Any,
    *,
    root: Path,
    bundled: bool,
    sha256: str,
    snapshot: dict[str, bytes],
) -> SkillPackRecord:
    if not isinstance(raw, dict):
        raise ValueError("Skill Pack manifest must be an object")
    allowed = {"schema_version", "id", "name", "version", "description", "source", "skills"}
    unknown = sorted(set(raw).difference(allowed))
    if unknown:
        raise ValueError(f"unknown Skill Pack fields: {', '.join(unknown)}")
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SKILL_PACK_SCHEMA_VERSION:
        raise ValueError(f"schema_version must be integer {SKILL_PACK_SCHEMA_VERSION}")
    pack_id = raw.get("id")
    name = raw.get("name")
    version = raw.get("version")
    description = raw.get("description", "")
    if not isinstance(pack_id, str) or not PACK_ID_RE.fullmatch(pack_id):
        raise ValueError("invalid Skill Pack id")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("Skill Pack name must be 1..120 characters")
    if not isinstance(version, str) or not version.strip() or len(version) > 64:
        raise ValueError("Skill Pack version must be 1..64 characters")
    if not isinstance(description, str) or len(description) > 1000:
        raise ValueError("Skill Pack description must be <= 1000 characters")
    source = _parse_source(raw.get("source", {}))
    raw_skills = raw.get("skills")
    if not isinstance(raw_skills, list) or not raw_skills or len(raw_skills) > MAX_SKILLS_PER_PACK:
        raise ValueError(f"skills must contain 1..{MAX_SKILLS_PER_PACK} entries")
    seen: set[str] = set()
    skills: list[SkillRecord] = []
    for item in raw_skills:
        if not isinstance(item, dict):
            raise ValueError("Skill entries must be objects")
        allowed_skill = {"id", "name", "path", "description", "routing_terms", "resources"}
        unknown_skill = sorted(set(item).difference(allowed_skill))
        if unknown_skill:
            raise ValueError(f"unknown Skill fields: {', '.join(unknown_skill)}")
        skill_id = item.get("id")
        skill_name = item.get("name")
        skill_path = item.get("path")
        skill_description = item.get("description", "")
        routing_terms = item.get("routing_terms", [])
        resources = item.get("resources", [])
        if not isinstance(skill_id, str) or not SKILL_ID_RE.fullmatch(skill_id) or skill_id in seen:
            raise ValueError(f"invalid or duplicate Skill id: {skill_id!r}")
        seen.add(skill_id)
        if not isinstance(skill_name, str) or not skill_name.strip() or len(skill_name) > 120:
            raise ValueError(f"Skill {skill_id} name must be 1..120 characters")
        if not isinstance(skill_description, str) or len(skill_description) > 1000:
            raise ValueError(f"Skill {skill_id} description must be <= 1000 characters")
        if (
            not isinstance(routing_terms, list)
            or not routing_terms
            or len(routing_terms) > MAX_ROUTING_TERMS
            or not all(isinstance(term, str) and 1 <= len(term.strip()) <= MAX_ROUTING_TERM_CHARS for term in routing_terms)
        ):
            raise ValueError(f"Skill {skill_id} routing_terms are invalid")
        deduplicated_terms: list[str] = []
        seen_term_keys: set[str] = set()
        for term in routing_terms:
            cleaned_term = term.strip()
            term_key = _normalize_text(cleaned_term)
            if term_key in seen_term_keys:
                continue
            seen_term_keys.add(term_key)
            deduplicated_terms.append(cleaned_term)
        normalized_terms = tuple(deduplicated_terms)
        document = _document(snapshot, skill_path)
        if not isinstance(resources, list) or len(resources) > MAX_SKILL_PACK_FILES:
            raise ValueError(f"Skill {skill_id} resources must be a bounded list")
        resource_documents = tuple(_document(snapshot, resource) for resource in resources)
        if len({item.path for item in resource_documents}) != len(resource_documents):
            raise ValueError(f"Skill {skill_id} resources contain duplicates")
        skills.append(SkillRecord(
            skill_id=skill_id,
            name=skill_name.strip(),
            description=skill_description.strip(),
            document=document,
            routing_terms=normalized_terms,
            resources=resource_documents,
        ))
    return SkillPackRecord(
        path=root,
        pack_id=pack_id,
        name=name.strip(),
        version=version.strip(),
        description=description.strip(),
        source=source,
        skills=tuple(skills),
        sha256=sha256,
        bundled=bundled,
    )


def _parse_source(raw: Any) -> SkillSource:
    if not isinstance(raw, dict) or set(raw).difference({"repository", "ref", "commit", "license"}):
        raise ValueError("source supports only repository/ref/commit/license")
    values: list[str] = []
    for key in ("repository", "ref", "commit", "license"):
        value = raw.get(key, "")
        if not isinstance(value, str) or len(value) > 500 or "\x00" in value:
            raise ValueError(f"source.{key} must be a string <= 500 characters")
        values.append(value.strip())
    return SkillSource(*values)


def _document(snapshot: dict[str, bytes], raw: Any) -> SkillDocument:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Skill paths must be clean POSIX relative strings")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Skill path may not escape the pack")
    normalized_path = relative.as_posix()
    data = snapshot.get(normalized_path)
    if data is None:
        raise ValueError("Skill path must name a regular non-link file from the verified pack snapshot")
    if len(data) > MAX_SKILL_TEXT_BYTES:
        raise ValueError(f"Skill text exceeds {MAX_SKILL_TEXT_BYTES} bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Skill text must be UTF-8: {raw}") from exc
    return SkillDocument(
        path=normalized_path,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def _safe_pack_file(root: Path, raw: Any, *, max_bytes: int) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise ValueError("Skill paths must be clean POSIX relative strings")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Skill path may not escape the pack")
    path = root.joinpath(*relative.parts)
    if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
        raise ValueError("Skill path must name a regular non-link file")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        size = resolved.stat().st_size
    except (OSError, ValueError) as exc:
        raise ValueError("Skill path escapes the pack") from exc
    if size > max_bytes:
        raise ValueError(f"Skill text exceeds {max_bytes} bytes")
    return resolved


def _hash_pack(root: Path) -> tuple[str, bytes, dict[str, bytes]]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    manifest_data: bytes | None = None
    snapshot: dict[str, bytes] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        relative_path = path.relative_to(root)
        if any(part in IGNORED_TREE_PARTS for part in relative_path.parts):
            continue
        if path.is_symlink() or _is_reparse_point(path):
            raise ValueError("Skill Pack trees may not contain links or reparse points")
        if not path.is_file() or path.suffix.lower() in IGNORED_SUFFIXES:
            continue
        count += 1
        if count > MAX_SKILL_PACK_FILES:
            raise ValueError(f"Skill Pack exceeds {MAX_SKILL_PACK_FILES} files")
        try:
            before = path.stat()
            if before.st_size > MAX_SKILL_PACK_BYTES - total:
                raise ValueError(f"Skill Pack exceeds {MAX_SKILL_PACK_BYTES} bytes")
            data = path.read_bytes()
            after = path.stat()
        except OSError as exc:
            raise ValueError(f"could not read Skill Pack file: {relative_path.as_posix()}") from exc
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if len(data) != before.st_size or before_identity != after_identity:
            raise ValueError(f"Skill Pack file changed while hashing: {relative_path.as_posix()}")
        normalized_path = relative_path.as_posix()
        snapshot[normalized_path] = data
        if normalized_path == SKILL_PACK_MANIFEST:
            manifest_data = data
        total += len(data)
        if total > MAX_SKILL_PACK_BYTES:
            raise ValueError(f"Skill Pack exceeds {MAX_SKILL_PACK_BYTES} bytes")
        relative = relative_path.as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    if manifest_data is None:
        raise ValueError(f"missing regular {SKILL_PACK_MANIFEST}")
    return digest.hexdigest(), manifest_data, snapshot


def _normalize_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _term_matches(normalized_task: str, term: str) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    if " " not in normalized_term and ASCII_WORD_RE.fullmatch(normalized_term):
        pattern = rf"(?<![a-z0-9_]){re.escape(normalized_term)}(?![a-z0-9_])"
        return re.search(pattern, normalized_task, flags=re.IGNORECASE) is not None
    return normalized_term in normalized_task


def _term_score(term: str) -> int:
    normalized = _normalize_text(term)
    token_bonus = 8 if " " in normalized else 0
    return 10 + token_bonus + min(len(normalized), 40)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
