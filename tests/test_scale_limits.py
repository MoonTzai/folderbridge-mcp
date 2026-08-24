from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.binary_tools import MAX_XML_MEMBER_BYTES
from folderbridge_mcp.config import load_config
from folderbridge_mcp.skills import SkillEngine
from folderbridge_mcp.tools import ToolRuntime


ROOT = Path(__file__).resolve().parents[1]
OFFICE_PLUGIN = ROOT / "extensions" / "office" / "plugin.py"


def write_pack(root: Path, pack_id: str, skills: list[dict[str, object]]) -> None:
    pack = root / pack_id
    pack.mkdir(parents=True)
    rendered: list[dict[str, object]] = []
    for spec in skills:
        skill_id = str(spec["id"])
        skill_dir = pack / "skills" / skill_id
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(str(spec.get("body", "body\n")), encoding="utf-8")
        rendered.append(
            {
                "id": skill_id,
                "name": str(spec.get("name", skill_id)),
                "path": f"skills/{skill_id}/SKILL.md",
                "description": str(spec.get("description", "bounded method")),
                "routing_terms": list(spec.get("routing_terms", [skill_id])),
                "resources": [],
            }
        )
    manifest = {
        "schema_version": 1,
        "id": pack_id,
        "name": f"Pack {pack_id}",
        "version": "1.0.0",
        "description": "scale test",
        "source": {},
        "skills": rendered,
    }
    (pack / "folderbridge-skill-pack.json").write_text(json.dumps(manifest), encoding="utf-8")


class ScaleLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bundled = self.root / "bundled"
        self.user = self.root / "user"
        self.workspace = self.root / "workspace"
        self.bundled.mkdir()
        self.user.mkdir()
        self.workspace.mkdir()
        self.trust = self.root / "trust.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def engine(self) -> SkillEngine:
        return SkillEngine(bundled_root=self.bundled, user_root=self.user, trust_path=self.trust)

    def test_skill_pack_capacity_supports_declared_128_skills(self) -> None:
        write_pack(
            self.bundled,
            "full-pack",
            [
                {
                    "id": f"skill-{index:03d}",
                    "routing_terms": [f"term-{index}"],
                }
                for index in range(128)
            ],
        )
        description = self.engine().describe()
        self.assertEqual(len(description["packs"]), 1)
        self.assertEqual(description["packs"][0]["skill_count"], 128)

    def test_skill_engine_scans_more_than_32_small_packs(self) -> None:
        for index in range(40):
            write_pack(self.bundled, f"pack-{index:03d}", [{"id": "method", "routing_terms": [f"term-{index}"]}])
        description = self.engine().describe()
        self.assertEqual(len(description["packs"]), 40)
        self.assertFalse(any(item["code"] == "SKILL_PACK_ROOT_LIMIT" for item in description["errors"]))

    def test_routing_index_round_robins_packs_and_reports_omissions(self) -> None:
        for pack_index in range(10):
            write_pack(
                self.bundled,
                f"pack-{pack_index:02d}",
                [
                    {
                        "id": f"skill-{skill_index:02d}",
                        "routing_terms": [f"term-{pack_index}-{skill_index}", f"alias-{pack_index}-{skill_index}"],
                        "description": "verbose description should not dominate the compact routing index",
                    }
                    for skill_index in range(10)
                ],
            )
        index = self.engine().routing_index(max_chars=2000)
        self.assertLessEqual(len(index), 2000)
        self.assertIn("pack-00/skill-00", index)
        self.assertIn("pack-09/skill-00", index)
        self.assertIn("omitted", index.lower())

    def test_runtime_initialization_budget_can_surface_hundreds_of_compact_skills(self) -> None:
        write_pack(
            self.bundled,
            "many-skills",
            [
                {"id": f"skill-{index:03d}", "routing_terms": [f"route-{index}"]}
                for index in range(120)
            ],
        )
        runtime = ToolRuntime(self.workspace, load_config(self.workspace))
        runtime.skills = self.engine()
        instructions = runtime.instructions
        self.assertIn("many-skills/skill-000", instructions)
        self.assertIn("many-skills/skill-119", instructions)
        self.assertGreater(len(instructions), 3500)
        self.assertLessEqual(len(instructions), 70 * 1024)

    def test_ooxml_single_part_limits_are_not_legacy_small_file_limits(self) -> None:
        self.assertGreaterEqual(MAX_XML_MEMBER_BYTES, 128 * 1024 * 1024)
        spec = importlib.util.spec_from_file_location("folderbridge_scale_office", OFFICE_PLUGIN)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertGreaterEqual(module.MAX_XML_BYTES, 128 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
