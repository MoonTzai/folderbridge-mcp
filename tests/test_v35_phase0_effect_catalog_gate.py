from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRST_PARTY_MANIFESTS = (
    "extensions/git-publisher/folderbridge-extension.json",
    "extensions/office/folderbridge-extension.json",
    "extensions/skill-engine/folderbridge-extension.json",
    "Plugins/extensions/comfyui/folderbridge-extension.json",
    "Plugins/extensions/download-toolkit/folderbridge-extension.json",
    "Plugins/extensions/ffmpeg-toolkit/folderbridge-extension.json",
    "Plugins/extensions/ftp-toolkit/folderbridge-extension.json",
    "Plugins/extensions/godot-ai/folderbridge-extension.json",
    "Plugins/extensions/gpt-sovits-local/folderbridge-extension.json",
    "Plugins/extensions/pdf-toolkit/folderbridge-extension.json",
)
EXPECTED_EFFECTS = {
    "git-publisher": {
        "status": [
            "read_only",
            "foreground"
        ],
        "connect": [
            "external_effect",
            "foreground"
        ],
        "commit": [
            "external_effect",
            "foreground"
        ],
        "push": [
            "external_effect",
            "foreground"
        ],
        "release-assets": [
            "external_effect",
            "job_owned"
        ]
    },
    "office": {
        "status": [
            "read_only",
            "foreground"
        ],
        "inspect_docx": [
            "read_only",
            "foreground"
        ],
        "inspect_xlsx": [
            "read_only",
            "foreground"
        ],
        "render": [
            "external_effect",
            "job_owned"
        ]
    },
    "skill-engine": {
        "list": [
            "read_only",
            "foreground"
        ],
        "match": [
            "read_only",
            "foreground"
        ],
        "get": [
            "read_only",
            "foreground"
        ]
    },
    "comfyui": {
        "status": [
            "read_only",
            "foreground"
        ],
        "free": [
            "external_effect",
            "foreground"
        ],
        "jobs": [
            "read_only",
            "foreground"
        ],
        "job-status": [
            "read_only",
            "foreground"
        ],
        "progress": [
            "read_only",
            "foreground"
        ],
        "health-check": [
            "read_only",
            "foreground"
        ],
        "cancel-prompt": [
            "external_effect",
            "foreground"
        ],
        "node-info": [
            "read_only",
            "foreground"
        ],
        "models": [
            "read_only",
            "foreground"
        ],
        "features": [
            "read_only",
            "foreground"
        ],
        "preflight": [
            "read_only",
            "foreground"
        ],
        "run": [
            "external_effect",
            "job_owned"
        ]
    },
    "download-toolkit": {
        "download": [
            "external_effect",
            "job_owned"
        ],
        "github-snapshot": [
            "external_effect",
            "job_owned"
        ]
    },
    "ffmpeg-toolkit": {
        "status": [
            "read_only",
            "foreground"
        ],
        "capabilities": [
            "read_only",
            "foreground"
        ],
        "probe": [
            "read_only",
            "foreground"
        ],
        "run": [
            "external_effect",
            "job_owned"
        ]
    },
    "ftp-toolkit": {
        "status": [
            "read_only",
            "foreground"
        ],
        "configure": [
            "external_effect",
            "foreground"
        ],
        "forget": [
            "external_effect",
            "foreground"
        ],
        "check": [
            "read_only",
            "foreground"
        ],
        "list": [
            "read_only",
            "foreground"
        ],
        "stat": [
            "read_only",
            "foreground"
        ],
        "mkdir": [
            "external_effect",
            "foreground"
        ],
        "rename": [
            "external_effect",
            "foreground"
        ],
        "delete": [
            "external_effect",
            "foreground"
        ],
        "upload": [
            "external_effect",
            "job_owned"
        ],
        "upload-tree": [
            "external_effect",
            "job_owned"
        ],
        "download": [
            "external_effect",
            "job_owned"
        ]
    },
    "godot-ai": {
        "status": [
            "read_only",
            "foreground"
        ],
        "inspect-editor": [
            "read_only",
            "foreground"
        ],
        "inspect-scene": [
            "read_only",
            "foreground"
        ],
        "inspect-node": [
            "read_only",
            "foreground"
        ],
        "read-logs": [
            "read_only",
            "foreground"
        ],
        "screenshot": [
            "read_only",
            "foreground"
        ],
        "open-scene": [
            "external_effect",
            "foreground"
        ],
        "save-scene": [
            "external_effect",
            "foreground"
        ],
        "create-node": [
            "external_effect",
            "foreground"
        ],
        "set-node-property": [
            "external_effect",
            "foreground"
        ],
        "delete-node": [
            "external_effect",
            "foreground"
        ],
        "run-project": [
            "external_effect",
            "managed_service"
        ],
        "stop-project": [
            "external_effect",
            "managed_service"
        ],
        "inspect-runtime-tree": [
            "read_only",
            "foreground"
        ],
        "send-action": [
            "external_effect",
            "foreground"
        ]
    },
    "gpt-sovits-local": {
        "status": [
            "read_only",
            "foreground"
        ]
    },
    "pdf-toolkit": {
        "status": [
            "read_only",
            "foreground"
        ],
        "info": [
            "read_only",
            "foreground"
        ],
        "outline": [
            "read_only",
            "foreground"
        ],
        "read-pages": [
            "read_only",
            "foreground"
        ],
        "search": [
            "read_only",
            "foreground"
        ],
        "render-pages": [
            "external_effect",
            "job_owned"
        ]
    }
}

EXPECTED_GPT_SOVITS_RUN_CASES = {
    "probe": [
        "read_only",
        "job_owned"
    ],
    "bootstrap": [
        "external_effect",
        "job_owned"
    ],
    "prepare-dataset": [
        "external_effect",
        "job_owned"
    ],
    "asr": [
        "external_effect",
        "job_owned"
    ],
    "train": [
        "external_effect",
        "job_owned"
    ],
    "infer": [
        "external_effect",
        "job_owned"
    ],
    "launch-webui": [
        "external_effect",
        "managed_service"
    ],
    "stop": [
        "external_effect",
        "managed_service"
    ]
}


class V35EffectContractCatalogGateAcceptanceTests(unittest.TestCase):
    def test_visible_first_party_manifests_are_schema_v2_with_complete_effect_contracts(self) -> None:
        violations: list[str] = []
        manifests: dict[str, dict] = {}
        for relative in FIRST_PARTY_MANIFESTS:
            path = ROOT / relative
            manifest = json.loads(path.read_text(encoding="utf-8"))
            extension_id = manifest.get("id")
            manifests[str(extension_id)] = manifest
            if manifest.get("schema_version") != 2:
                violations.append(
                    f"{extension_id}: manifest schema must be v2, got {manifest.get('schema_version')!r}"
                )
            actions = manifest.get("actions")
            if not isinstance(actions, dict) or not actions:
                violations.append(f"{extension_id}: actions must be a non-empty object")
                continue
            for action_name, action in actions.items():
                if not isinstance(action, dict) or "effect_contract" not in action:
                    violations.append(
                        f"{extension_id}/{action_name}: missing trusted effect_contract"
                    )

        # The multiplexed GPT-SoVITS owner is explicitly called out by V35:
        # effect semantics must be resolved from the already schema-validated
        # operation discriminator rather than blanket action-name inference.
        sovits = manifests.get("gpt-sovits-local")
        if isinstance(sovits, dict):
            run = sovits.get("actions", {}).get("run", {})
            operation = run.get("input_schema", {}).get("properties", {}).get("operation", {})
            enum = operation.get("enum")
            effect = run.get("effect_contract")
            selector = effect.get("selector") if isinstance(effect, dict) else None
            if not isinstance(selector, dict):
                violations.append(
                    "gpt-sovits-local/run: effect_contract must use the operation selector"
                )
            else:
                if selector.get("param") != "operation":
                    violations.append(
                        "gpt-sovits-local/run: effect selector param must be operation"
                    )
                cases = selector.get("cases")
                if not isinstance(enum, list) or not isinstance(cases, dict) or set(cases) != set(enum):
                    violations.append(
                        "gpt-sovits-local/run: effect selector cases must cover the exact operation enum"
                    )

        self.assertEqual([], violations, "\n" + "\n".join(violations))


    def test_effect_contract_catalog_matches_reviewed_runtime_semantics(self) -> None:
        manifests = {}
        for relative in FIRST_PARTY_MANIFESTS:
            manifest = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            manifests[manifest["id"]] = manifest

        for extension_id, actions in EXPECTED_EFFECTS.items():
            manifest = manifests[extension_id]
            self.assertEqual(manifest.get("runtime_abi"), 1, extension_id)
            self.assertEqual(set(manifest["actions"]), set(actions) | ({"run"} if extension_id == "gpt-sovits-local" else set()))
            for action_name, (semantics, lifetime) in actions.items():
                self.assertEqual(
                    manifest["actions"][action_name].get("effect_contract"),
                    {"effect_semantics": semantics, "lifetime": lifetime},
                    f"{extension_id}/{action_name}",
                )

        run = manifests["gpt-sovits-local"]["actions"]["run"]
        selector = run["effect_contract"]["selector"]
        self.assertEqual(selector["param"], "operation")
        self.assertEqual(set(selector["cases"]), set(EXPECTED_GPT_SOVITS_RUN_CASES))
        for operation, (semantics, lifetime) in EXPECTED_GPT_SOVITS_RUN_CASES.items():
            self.assertEqual(
                selector["cases"][operation],
                {"effect_semantics": semantics, "lifetime": lifetime},
                f"gpt-sovits-local/run({operation})",
            )

    def test_private_debate_judge_owner_remains_explicitly_blocked_until_its_source_contract_is_audited(self) -> None:
        matrix = json.loads(
            (ROOT / "docs/v35-phase0-remote-settlement-capability-matrix-20260905.json").read_text(
                encoding="utf-8"
            )
        )
        rows = [
            row
            for row in matrix.get("rows", [])
            if row.get("owner") == "extension:debate-judge-adapter"
        ]
        self.assertTrue(rows)
        self.assertTrue(all(row.get("persistent_tunnel_allowed") is False for row in rows))
        self.assertTrue(
            all(
                row.get("blocking_reason") == "trusted_effect_contract_evidence_unavailable"
                for row in rows
            )
        )


if __name__ == "__main__":
    unittest.main()
