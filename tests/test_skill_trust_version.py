from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import folderbridge_mcp.skills as skills_module
from folderbridge_mcp.security import ToolError
from folderbridge_mcp.skills import SkillEngine


class SkillTrustVersionTests(unittest.TestCase):
    def test_trust_store_version_rejects_json_boolean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundled = root / "bundled"
            user = root / "user"
            bundled.mkdir()
            user.mkdir()
            pack = user / "external-pack"
            skill_dir = pack / "skills" / "architecture"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Architecture\n", encoding="utf-8")
            (pack / "folderbridge-skill-pack.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "id": "external-pack",
                    "name": "External Pack",
                    "version": "1.0.0",
                    "description": "test",
                    "source": {},
                    "skills": [{
                        "id": "architecture",
                        "name": "Architecture",
                        "path": "skills/architecture/SKILL.md",
                        "description": "architecture",
                        "routing_terms": ["architecture"],
                        "resources": [],
                    }],
                }),
                encoding="utf-8",
            )
            trust_path = root / "skill-trust.json"
            engine = SkillEngine(bundled_root=bundled, user_root=user, trust_path=trust_path)
            external = engine.describe(include_untrusted=True)["packs"][0]
            trust_path.write_text(
                json.dumps({
                    "version": True,
                    "external": {
                        "external-pack": {
                            "sha256": external["sha256"],
                            "enabled": True,
                        }
                    },
                    "bundled_disabled": [],
                }),
                encoding="utf-8",
            )
            self.assertEqual(engine.describe()["packs"], [])

    def test_oversized_trust_store_is_rejected_before_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trust_path = Path(temporary) / "skill-trust.json"
            with trust_path.open("wb") as stream:
                stream.truncate(skills_module.MAX_SKILL_MANIFEST_BYTES + 1)
            store = skills_module._SkillTrustStore(trust_path)
            original_read_bytes = Path.read_bytes

            def guarded_read_bytes(path: Path) -> bytes:
                if path == trust_path:
                    self.fail("oversized Skill trust-store contents must not be read before the byte limit is checked")
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
                self.assertEqual(store._load(), store._empty())

    def test_document_cannot_change_after_tree_hash_before_record_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundled = root / "bundled"
            user = root / "user"
            bundled.mkdir()
            user.mkdir()
            pack = user / "external-pack"
            skill_dir = pack / "skills" / "architecture"
            skill_dir.mkdir(parents=True)
            document = skill_dir / "SKILL.md"
            document.write_text("old body", encoding="utf-8")
            (pack / "folderbridge-skill-pack.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "id": "external-pack",
                    "name": "External Pack",
                    "version": "1.0.0",
                    "description": "test",
                    "source": {},
                    "skills": [{
                        "id": "architecture",
                        "name": "Architecture",
                        "path": "skills/architecture/SKILL.md",
                        "description": "architecture",
                        "routing_terms": ["architecture"],
                        "resources": [],
                    }],
                }),
                encoding="utf-8",
            )
            original_hash_pack = skills_module._hash_pack

            def hash_then_mutate(path: Path):
                result = original_hash_pack(path)
                document.write_text("new body", encoding="utf-8")
                return result

            engine = SkillEngine(
                bundled_root=bundled,
                user_root=user,
                trust_path=root / "skill-trust.json",
            )
            with mock.patch.object(skills_module, "_hash_pack", hash_then_mutate):
                admin = engine.describe(include_untrusted=True)

            self.assertEqual(document.read_text(encoding="utf-8"), "new body")
            self.assertEqual(len(admin["packs"]), 1)
            rendered_document = admin["packs"][0]["skills"][0]
            self.assertEqual(rendered_document["size"], len(b"old body"))
            self.assertEqual(rendered_document["sha256"], hashlib.sha256(b"old body").hexdigest())

    def test_oversized_skill_pack_file_is_rejected_before_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pack = root / "external-pack"
            skill_dir = pack / "skills" / "architecture"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("# Architecture\n", encoding="utf-8")
            (pack / "folderbridge-skill-pack.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "id": "external-pack",
                    "name": "External Pack",
                    "version": "1.0.0",
                    "description": "test",
                    "source": {},
                    "skills": [{
                        "id": "architecture",
                        "name": "Architecture",
                        "path": "skills/architecture/SKILL.md",
                        "description": "architecture",
                        "routing_terms": ["architecture"],
                        "resources": [],
                    }],
                }),
                encoding="utf-8",
            )
            target = pack / "huge.bin"
            with target.open("wb") as stream:
                stream.truncate(skills_module.MAX_SKILL_PACK_BYTES + 1)
            original_read_bytes = Path.read_bytes

            def guarded_read_bytes(path: Path) -> bytes:
                if path == target:
                    self.fail("oversized Skill Pack file contents must not be read before the byte limit is checked")
                return original_read_bytes(path)

            with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
                with self.assertRaisesRegex(ValueError, "exceeds"):
                    skills_module._load_pack(pack, bundled=False)

    def test_get_bounds_read_if_skill_grows_after_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundled = root / "bundled"
            user = root / "user"
            bundled.mkdir()
            user.mkdir()
            pack = user / "external-pack"
            skill_dir = pack / "skills" / "architecture"
            skill_dir.mkdir(parents=True)
            document = skill_dir / "SKILL.md"
            document.write_text("approved body", encoding="utf-8")
            (pack / "folderbridge-skill-pack.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "id": "external-pack",
                    "name": "External Pack",
                    "version": "1.0.0",
                    "description": "test",
                    "source": {},
                    "skills": [{
                        "id": "architecture",
                        "name": "Architecture",
                        "path": "skills/architecture/SKILL.md",
                        "description": "architecture",
                        "routing_terms": ["architecture"],
                        "resources": [],
                    }],
                }),
                encoding="utf-8",
            )
            engine = SkillEngine(
                bundled_root=bundled,
                user_root=user,
                trust_path=root / "skill-trust.json",
            )
            admin = engine.describe(include_untrusted=True)["packs"][0]
            engine.approve_pack("external-pack", admin["sha256"])
            match = engine.match("architecture")["matches"][0]
            original_safe_pack_file = skills_module._safe_pack_file

            def validate_then_grow(pack_root: Path, raw: object, *, max_bytes: int) -> Path:
                path = original_safe_pack_file(pack_root, raw, max_bytes=max_bytes)
                with path.open("wb") as stream:
                    stream.truncate(skills_module.MAX_SKILL_TEXT_BYTES + 1)
                return path

            with mock.patch.object(skills_module, "_safe_pack_file", side_effect=validate_then_grow):
                with self.assertRaises(ToolError) as raised:
                    engine.get(match["skill_ref"], match["sha256"])
            self.assertEqual(raised.exception.code, "SKILL_TEXT_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
