from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.security import ToolError
from folderbridge_mcp.skills import SkillEngine


def write_pack(
    root: Path,
    pack_id: str,
    *,
    bundled: bool = False,
    skills: list[dict] | None = None,
    source: dict | None = None,
) -> Path:
    pack = root / pack_id
    pack.mkdir(parents=True)
    skill_specs = skills or [
        {
            "id": "architecture",
            "name": "Architecture",
            "description": "Design deep modules and interfaces.",
            "routing_terms": ["architecture", "module", "interface", "架构", "模块", "接口"],
            "body": "# Architecture\n\nUse deep modules and explicit interfaces.\n",
            "resources": {},
        }
    ]
    rendered = []
    for spec in skill_specs:
        skill_dir = pack / "skills" / spec["id"]
        skill_dir.mkdir(parents=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(spec["body"], encoding="utf-8")
        resources = []
        for resource_name, resource_body in spec.get("resources", {}).items():
            resource_path = skill_dir / resource_name
            resource_path.write_text(resource_body, encoding="utf-8")
            resources.append(f"skills/{spec['id']}/{resource_name}")
        rendered.append(
            {
                "id": spec["id"],
                "name": spec["name"],
                "path": f"skills/{spec['id']}/SKILL.md",
                "description": spec["description"],
                "routing_terms": spec["routing_terms"],
                "resources": resources,
            }
        )
    manifest = {
        "schema_version": 1,
        "id": pack_id,
        "name": f"Pack {pack_id}",
        "version": "1.0.0",
        "description": "Test pack",
        "source": source or {
            "repository": "https://example.invalid/skills",
            "ref": "v1",
            "commit": "abc123",
            "license": "MIT",
        },
        "skills": rendered,
    }
    (pack / "folderbridge-skill-pack.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pack


class SkillEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundled = self.root / "bundled"
        self.user = self.root / "user"
        self.bundled.mkdir()
        self.user.mkdir()
        self.trust_path = self.root / "skill-trust.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def engine(self) -> SkillEngine:
        return SkillEngine(
            bundled_root=self.bundled,
            user_root=self.user,
            trust_path=self.trust_path,
        )

    def test_repository_bundled_engineering_pack_has_expected_core_methods(self) -> None:
        engine = SkillEngine(user_root=self.user, trust_path=self.trust_path)
        packs = engine.describe()["packs"]
        engineering = next(item for item in packs if item["id"] == "folderbridge-engineering")
        self.assertTrue(engineering["bundled"])
        self.assertEqual(engineering["version"], "1.0.0")
        self.assertEqual(
            {item["id"] for item in engineering["skills"]},
            {"codebase-design", "improve-codebase-architecture", "diagnosing-bugs", "tdd", "code-review", "implement"},
        )
        routed = engine.match("请先做架构审计，再调试这个竞态 bug", limit=5)["matches"]
        refs = {item["skill_ref"] for item in routed}
        self.assertIn("folderbridge-engineering/improve-codebase-architecture", refs)
        self.assertIn("folderbridge-engineering/diagnosing-bugs", refs)

    def test_bundled_engineering_skill_bodies_have_no_personal_branding(self) -> None:
        pack_root = Path(__file__).resolve().parents[1] / "skill_packs" / "matt-pocock-engineering"
        for path in (pack_root / "skills").rglob("SKILL.md"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("Matt Pocock", text, path.as_posix())
            self.assertNotIn("mattpocock", text.lower(), path.as_posix())

    def test_bundled_engineering_pack_preserves_upstream_mit_attribution(self) -> None:
        pack_root = Path(__file__).resolve().parents[1] / "skill_packs" / "matt-pocock-engineering"
        manifest = json.loads((pack_root / "folderbridge-skill-pack.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"]["repository"], "https://github.com/mattpocock/skills")
        self.assertEqual(manifest["source"]["license"], "MIT")

        notice = (pack_root / "NOTICE.md").read_text(encoding="utf-8")
        upstream_license = (pack_root / "LICENSE.upstream-MIT.txt").read_text(encoding="utf-8")
        self.assertIn("Matt Pocock", notice)
        self.assertIn("https://github.com/mattpocock/skills", notice)
        self.assertIn("adapted", notice.lower())
        self.assertIn("MIT", notice)
        self.assertIn("Copyright (c) 2026 Matt Pocock", upstream_license)
        self.assertIn("Permission is hereby granted", upstream_license)

        project_root = Path(__file__).resolve().parents[1]
        readme = (project_root / "README.md").read_text(encoding="utf-8")
        readme_zh = (project_root / "README.zh-CN.md").read_text(encoding="utf-8")
        for text in (readme, readme_zh):
            self.assertIn("Matt Pocock", text)
            self.assertIn("mattpocock/skills", text)
            self.assertIn("MIT", text)
            self.assertIn("LICENSE.upstream-MIT.txt", text)

    def test_bundled_pack_is_trusted_enabled_and_get_verifies_returned_bytes(self) -> None:
        write_pack(self.bundled, "bundled-pack", bundled=True)
        engine = self.engine()
        description = engine.describe()
        self.assertEqual([item["id"] for item in description["packs"]], ["bundled-pack"])
        pack = description["packs"][0]
        self.assertTrue(pack["bundled"])
        self.assertTrue(pack["trusted"])
        self.assertTrue(pack["enabled"])
        match = engine.match("Please review the module architecture", limit=3)
        self.assertEqual(match["matches"][0]["skill_ref"], "bundled-pack/architecture")
        selected = match["matches"][0]
        loaded = engine.get(selected["skill_ref"], selected["sha256"])
        self.assertIn("deep modules", loaded["text"])
        self.assertEqual(loaded["sha256"], selected["sha256"])

    def test_external_pack_is_hidden_until_exact_hash_approval(self) -> None:
        write_pack(self.user, "external-pack")
        engine = self.engine()
        self.assertEqual(engine.describe()["packs"], [])
        self.assertEqual(engine.match("architecture")["matches"], [])

        admin = engine.describe(include_untrusted=True)
        self.assertEqual([item["id"] for item in admin["packs"]], ["external-pack"])
        external = admin["packs"][0]
        self.assertFalse(external["trusted"])
        engine.approve_pack("external-pack", external["sha256"])

        trusted = engine.describe()["packs"][0]
        self.assertTrue(trusted["trusted"])
        self.assertTrue(trusted["enabled"])
        self.assertEqual(engine.match("架构模块")["matches"][0]["skill_ref"], "external-pack/architecture")

    def test_approval_rejects_pack_changed_after_displayed_hash(self) -> None:
        pack = write_pack(self.user, "external-pack")
        engine = self.engine()
        displayed = engine.describe(include_untrusted=True)["packs"][0]["sha256"]
        (pack / "skills" / "architecture" / "SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            engine.approve_pack("external-pack", displayed)
        self.assertEqual(raised.exception.code, "SKILL_PACK_CHANGED")
        self.assertEqual(engine.describe()["packs"], [])

    def test_manifest_cannot_change_between_parse_and_tree_hash(self) -> None:
        pack = write_pack(self.user, "unstable-manifest")
        target = pack / "folderbridge-skill-pack.json"
        original_read_bytes = Path.read_bytes
        mutated = False

        def read_and_mutate(path: Path) -> bytes:
            nonlocal mutated
            data = original_read_bytes(path)
            if path == target and not mutated:
                mutated = True
                raw = json.loads(data)
                raw["skills"][0]["routing_terms"] = ["different-route"]
                target.write_text(json.dumps(raw), encoding="utf-8")
            return data

        with patch.object(Path, "read_bytes", read_and_mutate):
            admin = self.engine().describe(include_untrusted=True)

        self.assertTrue(mutated)
        self.assertEqual(admin["packs"], [])
        self.assertTrue(any(error["code"] == "SKILL_PACK_INVALID" for error in admin["errors"]))

    def test_pack_changed_while_hashing_is_rejected(self) -> None:
        pack = write_pack(self.user, "unstable-pack")
        target = pack / "skills" / "architecture" / "SKILL.md"
        original_read_bytes = Path.read_bytes
        mutated = False

        def read_and_mutate(path: Path) -> bytes:
            nonlocal mutated
            data = original_read_bytes(path)
            if path == target and not mutated:
                mutated = True
                target.write_text("mutated during hash", encoding="utf-8")
            return data

        with patch.object(Path, "read_bytes", read_and_mutate):
            admin = self.engine().describe(include_untrusted=True)

        self.assertTrue(mutated)
        self.assertEqual(admin["packs"], [])
        self.assertTrue(any(error["code"] == "SKILL_PACK_INVALID" for error in admin["errors"]))

    def test_external_change_makes_approval_stale_and_model_view_hides_it(self) -> None:
        pack = write_pack(self.user, "external-pack")
        engine = self.engine()
        displayed = engine.describe(include_untrusted=True)["packs"][0]
        engine.approve_pack("external-pack", displayed["sha256"])
        (pack / "notes.txt").write_text("new undeclared content", encoding="utf-8")
        self.assertEqual(engine.describe()["packs"], [])
        admin = engine.describe(include_untrusted=True)["packs"][0]
        self.assertTrue(admin["approval_stale"])
        self.assertFalse(admin["trusted"])

    def test_bundled_disable_override_survives_content_change(self) -> None:
        pack = write_pack(self.bundled, "bundled-pack")
        engine = self.engine()
        engine.set_enabled("bundled-pack", False)
        self.assertEqual(engine.match("architecture")["matches"], [])
        (pack / "skills" / "architecture" / "SKILL.md").write_text("new bundled body", encoding="utf-8")
        reloaded = self.engine()
        admin = reloaded.describe(include_untrusted=True)["packs"][0]
        self.assertTrue(admin["trusted"])
        self.assertFalse(admin["enabled"])

    def test_get_fails_if_body_changes_after_match(self) -> None:
        pack = write_pack(self.bundled, "bundled-pack")
        engine = self.engine()
        selected = engine.match("architecture")["matches"][0]
        (pack / "skills" / "architecture" / "SKILL.md").write_text("mutated", encoding="utf-8")
        with self.assertRaises(ToolError) as raised:
            engine.get(selected["skill_ref"], selected["sha256"])
        self.assertEqual(raised.exception.code, "SKILL_CHANGED")

    def test_declared_resource_is_bounded_and_hash_verified(self) -> None:
        write_pack(
            self.bundled,
            "bundled-pack",
            skills=[
                {
                    "id": "design",
                    "name": "Design",
                    "description": "Design method",
                    "routing_terms": ["design"],
                    "body": "main body",
                    "resources": {"DEEPENING.md": "resource body"},
                }
            ],
        )
        engine = self.engine()
        selected = engine.match("design")["matches"][0]
        resource = selected["resources"][0]
        loaded = engine.get(selected["skill_ref"], resource["sha256"], resource=resource["path"])
        self.assertEqual(loaded["text"], "resource body")
        with self.assertRaises(ToolError) as raised:
            engine.get(selected["skill_ref"], resource["sha256"], resource="skills/design/OTHER.md")
        self.assertEqual(raised.exception.code, "SKILL_RESOURCE_NOT_DECLARED")

    def test_routing_terms_are_deduplicated_by_normalized_text(self) -> None:
        write_pack(
            self.bundled,
            "dedupe-pack",
            skills=[
                {
                    "id": "debug",
                    "name": "Debug",
                    "description": "Debug method",
                    "routing_terms": ["bug", "BUG", "  bug  ", "调试", " 调试 "],
                    "body": "debug",
                    "resources": {},
                }
            ],
        )
        match = self.engine().match("bug 调试")["matches"][0]
        self.assertEqual(match["matched_terms"], ["bug", "调试"])
        self.assertEqual(len(match["matched_terms"]), 2)

    def test_match_is_deterministic_and_unrelated_text_has_no_match(self) -> None:
        write_pack(
            self.bundled,
            "matt",
            skills=[
                {
                    "id": "diagnosing-bugs",
                    "name": "Diagnosing Bugs",
                    "description": "Debug reproducibly",
                    "routing_terms": ["bug", "debug", "crash", "错误", "调试"],
                    "body": "diagnose",
                    "resources": {},
                },
                {
                    "id": "tdd",
                    "name": "TDD",
                    "description": "Test first",
                    "routing_terms": ["tdd", "test first", "red green", "测试先行"],
                    "body": "tdd",
                    "resources": {},
                },
            ],
        )
        engine = self.engine()
        self.assertEqual(engine.match("debug this crash")["matches"][0]["skill_ref"], "matt/diagnosing-bugs")
        self.assertEqual(engine.match("请用测试先行的 TDD 做")["matches"][0]["skill_ref"], "matt/tdd")
        self.assertEqual(engine.match("what is the weather tomorrow")["matches"], [])

    def test_routing_index_is_bounded_and_does_not_embed_skill_body(self) -> None:
        write_pack(self.bundled, "bundled-pack")
        index = self.engine().routing_index(max_chars=500)
        self.assertLessEqual(len(index), 500)
        self.assertIn("bundled-pack/architecture", index)
        self.assertNotIn("Use deep modules and explicit interfaces", index)

    def test_user_pack_cannot_shadow_bundled_pack_id(self) -> None:
        write_pack(self.bundled, "same-id")
        external = write_pack(self.user, "same-id")
        (external / "skills" / "architecture" / "SKILL.md").write_text(
            "external override must never replace bundled content\n",
            encoding="utf-8",
        )
        admin = self.engine().describe(include_untrusted=True)
        self.assertEqual([item["id"] for item in admin["packs"]], ["same-id"])
        self.assertTrue(admin["packs"][0]["bundled"])
        self.assertEqual(admin["error_count"], 0)
        selected = self.engine().match("architecture")["matches"][0]
        loaded = self.engine().get(selected["skill_ref"], selected["sha256"])
        self.assertIn("Use deep modules", loaded["text"])
        self.assertNotIn("external override", loaded["text"])

    def test_external_approval_survives_engine_recreation(self) -> None:
        write_pack(self.user, "external-pack")
        first = self.engine()
        displayed = first.describe(include_untrusted=True)["packs"][0]
        first.approve_pack("external-pack", displayed["sha256"])

        reloaded = self.engine()
        trusted = reloaded.describe()["packs"]
        self.assertEqual([item["id"] for item in trusted], ["external-pack"])
        self.assertTrue(trusted[0]["trusted"])
        self.assertTrue(trusted[0]["enabled"])

    def test_manifest_schema_version_rejects_json_boolean(self) -> None:
        pack = write_pack(self.user, "bool-schema")
        manifest_path = pack / "folderbridge-skill-pack.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["schema_version"] = True
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        admin = self.engine().describe(include_untrusted=True)
        self.assertEqual(admin["packs"], [])
        self.assertTrue(any(error["code"] == "SKILL_PACK_INVALID" for error in admin["errors"]))

    def test_manifest_is_strict_json_and_paths_cannot_escape_pack(self) -> None:
        pack = write_pack(self.user, "bad-pack")
        manifest_path = pack / "folderbridge-skill-pack.json"
        manifest_path.write_text('{"schema_version":1,"id":"bad-pack","name":"x","version":"1","description":"x","source":{},"skills":[],"bad":NaN}', encoding="utf-8")
        admin = self.engine().describe(include_untrusted=True)
        self.assertEqual(admin["packs"], [])
        self.assertGreaterEqual(admin["error_count"], 1)

        self.tearDown()
        self.setUp()
        pack = write_pack(self.user, "bad-path")
        raw = json.loads((pack / "folderbridge-skill-pack.json").read_text(encoding="utf-8"))
        raw["skills"][0]["path"] = "../outside.md"
        (pack / "folderbridge-skill-pack.json").write_text(json.dumps(raw), encoding="utf-8")
        admin = self.engine().describe(include_untrusted=True)
        self.assertEqual(admin["packs"], [])
        self.assertGreaterEqual(admin["error_count"], 1)


if __name__ == "__main__":
    unittest.main()
