from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import (
    SUPPORTED_EXTENSION_SCHEMA_VERSIONS,
    SUPPORTED_RUNTIME_ABI_VERSIONS,
    load_extension,
    resolve_effect_contract,
)


def write_extension(root: Path, manifest: dict) -> Path:
    extension = root / manifest["id"]
    extension.mkdir()
    (extension / "plugin.py").write_text(
        "def run(action, params, context):\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    (extension / "folderbridge-extension.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return extension


def base_manifest(*, schema_version: int = 1, runtime_abi: int | None = None) -> dict:
    manifest = {
        "schema_version": schema_version,
        "id": "v35-fixture",
        "name": "V35 fixture",
        "version": "1.0.0",
        "entrypoint": "plugin.py",
        "permissions": [],
        "workspace_adapter": {"mode": "none", "state": "none"},
        "actions": {
            "run": {
                "read_only": True,
                "requires_workspace": False,
                "authorization": "none",
                "run_mode": "foreground",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["probe", "mutate"],
                        }
                    },
                    "required": ["operation"],
                    "additionalProperties": False,
                },
            }
        },
    }
    if runtime_abi is not None:
        manifest["runtime_abi"] = runtime_abi
    return manifest


class V35EffectContractAcceptanceTests(unittest.TestCase):
    def test_manifest_schema_and_runtime_abi_are_independent_version_axes(self) -> None:
        self.assertEqual(SUPPORTED_EXTENSION_SCHEMA_VERSIONS, (1, 2))
        self.assertEqual(SUPPORTED_RUNTIME_ABI_VERSIONS, (1, 2))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            legacy = base_manifest(schema_version=1)
            legacy_record = load_extension(write_extension(root, legacy), bundled=True)
            self.assertEqual(legacy_record.manifest.schema_version, 1)
            self.assertEqual(legacy_record.manifest.runtime_abi, 1)

            schema2_abi1 = base_manifest(schema_version=2, runtime_abi=1)
            schema2_abi1["id"] = "schema2-abi1"
            schema2_abi1["actions"]["run"]["effect_contract"] = {
                "effect_semantics": "read_only",
                "lifetime": "foreground",
            }
            record = load_extension(write_extension(root, schema2_abi1), bundled=True)
            self.assertEqual(record.manifest.schema_version, 2)
            self.assertEqual(record.manifest.runtime_abi, 1)

            schema1_abi2 = base_manifest(schema_version=1, runtime_abi=2)
            schema1_abi2["id"] = "schema1-abi2"
            record = load_extension(write_extension(root, schema1_abi2), bundled=True)
            self.assertEqual(record.manifest.schema_version, 1)
            self.assertEqual(record.manifest.runtime_abi, 2)

    def test_schema_v1_read_only_never_becomes_effect_safety_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = base_manifest(schema_version=1)
            manifest["actions"]["run"]["read_only"] = True
            record = load_extension(write_extension(Path(directory), manifest), bundled=True)
            resolved = resolve_effect_contract(record.manifest.actions["run"], {"operation": "probe"})
            self.assertEqual(resolved.effect_semantics, "unclassified_unsafe")
            self.assertEqual(resolved.lifetime, "foreground")
            self.assertFalse(resolved.trusted)

    def test_schema_v2_direct_effect_contract_is_explicit_and_trusted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = base_manifest(schema_version=2, runtime_abi=2)
            manifest["actions"]["run"]["effect_contract"] = {
                "effect_semantics": "external_effect",
                "lifetime": "job_owned",
            }
            manifest["actions"]["run"]["run_mode"] = "job"
            record = load_extension(write_extension(Path(directory), manifest), bundled=True)
            resolved = resolve_effect_contract(record.manifest.actions["run"], {"operation": "mutate"})
            self.assertEqual(resolved.effect_semantics, "external_effect")
            self.assertEqual(resolved.lifetime, "job_owned")
            self.assertTrue(resolved.trusted)

    def test_schema_v2_selector_must_cover_exact_validated_enum_and_resolves_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = base_manifest(schema_version=2, runtime_abi=1)
            manifest["actions"]["run"]["effect_contract"] = {
                "selector": {
                    "param": "operation",
                    "cases": {
                        "probe": {
                            "effect_semantics": "read_only",
                            "lifetime": "foreground",
                        },
                        "mutate": {
                            "effect_semantics": "external_effect",
                            "lifetime": "job_owned",
                        },
                    },
                }
            }
            record = load_extension(write_extension(root, manifest), bundled=True)
            self.assertEqual(
                resolve_effect_contract(record.manifest.actions["run"], {"operation": "probe"}).effect_semantics,
                "read_only",
            )
            self.assertEqual(
                resolve_effect_contract(record.manifest.actions["run"], {"operation": "mutate"}).lifetime,
                "job_owned",
            )

            incomplete = base_manifest(schema_version=2, runtime_abi=1)
            incomplete["id"] = "incomplete-selector"
            incomplete["actions"]["run"]["effect_contract"] = {
                "selector": {
                    "param": "operation",
                    "cases": {
                        "probe": {
                            "effect_semantics": "read_only",
                            "lifetime": "foreground",
                        }
                    },
                }
            }
            with self.assertRaisesRegex(ValueError, "cover"):
                load_extension(write_extension(root, incomplete), bundled=True)

            unknown = base_manifest(schema_version=2, runtime_abi=1)
            unknown["id"] = "unknown-selector"
            unknown["actions"]["run"]["effect_contract"] = {
                "selector": {
                    "param": "operation",
                    "cases": {
                        "probe": {
                            "effect_semantics": "read_only",
                            "lifetime": "foreground",
                        },
                        "mutate": {
                            "effect_semantics": "external_effect",
                            "lifetime": "job_owned",
                        },
                        "other": {
                            "effect_semantics": "read_only",
                            "lifetime": "foreground",
                        },
                    },
                }
            }
            with self.assertRaisesRegex(ValueError, "cover"):
                load_extension(write_extension(root, unknown), bundled=True)

    def test_schema_v2_recovery_contract_is_bounded_and_rejects_raw_freeform_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = base_manifest(schema_version=2, runtime_abi=2)
            valid["actions"]["run"]["effect_contract"] = {
                "effect_semantics": "external_effect",
                "lifetime": "foreground",
            }
            valid["actions"]["run"]["recovery_contract"] = {
                "correlation": "host_operation_id",
                "recovery_mode_control": False,
            }
            record = load_extension(write_extension(root, valid), bundled=True)
            recovery = record.manifest.actions["run"].recovery_contract
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.correlation, "host_operation_id")

            invalid = base_manifest(schema_version=2, runtime_abi=2)
            invalid["id"] = "raw-capture"
            invalid["actions"]["run"]["effect_contract"] = {
                "effect_semantics": "external_effect",
                "lifetime": "foreground",
            }
            invalid["actions"]["run"]["recovery_contract"] = {
                "correlation": "deterministic_fields",
                "capsule": [
                    {"param": "operation", "kind": "raw_string"},
                ],
            }
            with self.assertRaisesRegex(ValueError, "capsule"):
                load_extension(write_extension(root, invalid), bundled=True)

    def test_schema_v2_effect_contract_is_required_for_every_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = base_manifest(schema_version=2, runtime_abi=1)
            with self.assertRaisesRegex(ValueError, "effect_contract"):
                load_extension(write_extension(Path(directory), manifest), bundled=True)


if __name__ == "__main__":
    unittest.main()
