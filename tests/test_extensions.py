from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from folderbridge_mcp.config import load_config
import folderbridge_mcp.extension_worker as extension_worker_module
import folderbridge_mcp.extensions as extensions_module
from folderbridge_mcp.extensions import (
    MAX_RUNNING_EXTENSION_JOBS,
    ExtensionRegistry,
    ExtensionTrustStore,
    load_extension,
)
from folderbridge_mcp.security import ToolError, Workspace
from folderbridge_mcp.tools import ToolRuntime


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.user_root = self.base / "extensions"
        self.bundled_root = self.base / "bundled"
        self.user_root.mkdir()
        self.bundled_root.mkdir()
        self.trust = ExtensionTrustStore(self.base / "trust.json")
        self.registry = ExtensionRegistry(
            user_root=self.user_root,
            bundled_root=self.bundled_root,
            trust_store=self.trust,
        )
        self.workspace_root = self.base / "workspace"
        self.workspace_root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_extension(
        self,
        extension_id: str = "example",
        *,
        bundled: bool = False,
        authorization: str = "global",
        adapter: dict[str, object] | None = None,
        permissions: list[str] | None = None,
        plugin_source: str | None = None,
    ) -> Path:
        root = (self.bundled_root if bundled else self.user_root) / extension_id
        root.mkdir(parents=True)
        manifest = {
            "schema_version": 1,
            "id": extension_id,
            "name": f"Extension {extension_id}",
            "version": "1.0.0",
            "description": "test extension",
            "entrypoint": "plugin.py",
            "permissions": permissions or ["workspace.read", "extension.state"],
            "execution": {"mode": "isolated-process", "timeout_seconds": 30},
            "workspace_adapter": adapter or {"mode": "none", "state": "profile"},
            "actions": {
                "echo": {
                    "read_only": True,
                    "requires_workspace": True,
                    "authorization": authorization,
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string", "maxLength": 100}},
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        (root / "folderbridge-extension.json").write_text(json.dumps(manifest), encoding="utf-8")
        (root / "plugin.py").write_text(
            plugin_source
            or "def handle(action, params, context):\n"
               "    return {'action': action, 'value': params['value'], 'workspace_root': context['workspace_root'], 'state_dir': context['state_dir']}\n",
            encoding="utf-8",
        )
        return root

    def test_unknown_or_overbroad_permission_is_rejected(self) -> None:
        path = self.make_extension(permissions=["network.*"])
        with self.assertRaisesRegex(ValueError, "unknown or overbroad"):
            load_extension(path, bundled=False)

    def test_secret_redaction_prefers_longest_overlapping_value(self) -> None:
        redacted = extensions_module._redact_secrets(
            "tokens=abcdef and abc",
            ("abc", "abcdef"),
        )
        self.assertNotIn("def", redacted)
        self.assertNotIn("abcdef", redacted)
        self.assertEqual(redacted, "tokens=<redacted> and <redacted>")

    def test_enum_does_not_treat_json_boolean_as_number(self) -> None:
        with self.assertRaises(ToolError) as raised:
            extensions_module.validate_json_schema(True, {"enum": [1]}, path="value")
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        extensions_module.validate_json_schema(1, {"enum": [1]}, path="value")

    def test_manifest_cannot_change_between_parse_and_tree_hash(self) -> None:
        root = self.make_extension()
        target = root / "folderbridge-extension.json"
        original_read_bytes = Path.read_bytes
        mutated = False

        def read_and_mutate(path: Path) -> bytes:
            nonlocal mutated
            data = original_read_bytes(path)
            if path == target and not mutated:
                mutated = True
                raw = json.loads(data)
                raw["permissions"].append("process.execute:cmd.exe")
                target.write_text(json.dumps(raw), encoding="utf-8")
            return data

        with mock.patch.object(Path, "read_bytes", read_and_mutate):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                load_extension(root, bundled=False)

        self.assertTrue(mutated)

    def test_extension_changed_while_hashing_is_rejected(self) -> None:
        root = self.make_extension()
        target = root / "plugin.py"
        original_read_bytes = Path.read_bytes
        mutated = False

        def read_and_mutate(path: Path) -> bytes:
            nonlocal mutated
            data = original_read_bytes(path)
            if path == target and not mutated:
                mutated = True
                target.write_text("def handle(action, params, context):\n    return {'mutated': True}\n", encoding="utf-8")
            return data

        with mock.patch.object(Path, "read_bytes", read_and_mutate):
            with self.assertRaisesRegex(ValueError, "changed while hashing"):
                load_extension(root, bundled=False)

        self.assertTrue(mutated)

    def test_manifest_schema_version_rejects_json_boolean(self) -> None:
        root = self.make_extension()
        manifest_path = root / "folderbridge-extension.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["schema_version"] = True
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            load_extension(root, bundled=False)

    def test_trust_store_version_rejects_json_boolean(self) -> None:
        root = self.make_extension()
        record = load_extension(root, bundled=False)
        self.trust.path.write_text(
            json.dumps({
                "version": True,
                "extensions": {
                    record.manifest.extension_id: {
                        "sha256": record.sha256,
                        "permissions": list(record.manifest.permissions),
                        "enabled": True,
                    }
                },
            }),
            encoding="utf-8",
        )
        self.assertFalse(self.trust.status(record)["trusted"])

    def test_oversized_trust_store_is_rejected_before_reading_contents(self) -> None:
        root = self.make_extension()
        record = load_extension(root, bundled=False)
        with self.trust.path.open("wb") as stream:
            stream.truncate(extensions_module.MAX_MANIFEST_BYTES + 1)
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            if path == self.trust.path:
                self.fail("oversized Extension trust-store contents must not be read before the byte limit is checked")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
            self.assertFalse(self.trust.status(record)["trusted"])

    def test_manifest_rejects_nonstandard_json_constants(self) -> None:
        path = self.make_extension()
        manifest_path = path / "folderbridge-extension.json"
        text = manifest_path.read_text(encoding="utf-8")
        text = text.replace(
            '"type": "string", "maxLength": 100',
            '"type": "number", "maximum": NaN',
        )
        manifest_path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON"):
            load_extension(path, bundled=False)

    def test_oversized_extension_file_is_rejected_before_reading_contents(self) -> None:
        root = self.make_extension()
        target = root / "huge.bin"
        with target.open("wb") as stream:
            stream.truncate(extensions_module.MAX_EXTENSION_BYTES + 1)
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            if path == target:
                self.fail("oversized Extension file contents must not be read before the byte limit is checked")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_extension(root, bundled=False)

    def test_extension_snapshot_rejects_oversized_file_before_reading_contents(self) -> None:
        root = self.make_extension()
        target = root / "huge.bin"
        with target.open("wb") as stream:
            stream.truncate(extensions_module.MAX_EXTENSION_BYTES + 1)
        destination = self.base / "snapshot"
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(path: Path) -> bytes:
            if path == target:
                self.fail("oversized execution-snapshot file contents must not be read before the byte limit is checked")
            return original_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                extensions_module.snapshot_extension(root, destination)

    def test_extension_result_rejects_nonfinite_numbers(self) -> None:
        self.make_extension(
            plugin_source=(
                "def handle(action, params, context):\n"
                "    return {'value': float('nan')}\n"
            )
        )
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        with self.assertRaises(ToolError) as raised:
            self.registry.run(
                "example",
                "echo",
                {"value": "ignored"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        self.assertEqual(raised.exception.code, "EXTENSION_SERIALIZE_FAILED")

    def test_outbound_https_and_explicit_environment_permissions_are_supported(self) -> None:
        path = self.make_extension(
            permissions=[
                "workspace.read",
                "extension.state",
                "network.outbound:https",
                "environment.inherit:JUDGE_API_TOKEN",
            ]
        )
        record = load_extension(path, bundled=False)
        self.assertIn("network.outbound:https", record.manifest.permissions)
        self.assertIn("environment.inherit:JUDGE_API_TOKEN", record.manifest.permissions)

        reserved = self.make_extension("reserved-env", permissions=["environment.inherit:CONTROL_PLANE_API_KEY"])
        with self.assertRaisesRegex(ValueError, "reserved environment variable"):
            load_extension(reserved, bundled=False)

    def test_external_extension_cannot_opt_out_of_authorization(self) -> None:
        path = self.make_extension(authorization="none")
        with self.assertRaisesRegex(ValueError, "external extensions may not declare authorization=none"):
            load_extension(path, bundled=False)

    def test_exact_hash_approval_becomes_stale_after_code_change(self) -> None:
        path = self.make_extension()
        first = self.registry.get("example")
        self.trust.approve(first, enabled=True)
        self.assertTrue(self.trust.status(first)["enabled"])

        with (path / "plugin.py").open("a", encoding="utf-8") as handle:
            handle.write("\n# changed\n")
        second = self.registry.get("example")
        status = self.trust.status(second)

        self.assertNotEqual(first.sha256, second.sha256)
        self.assertFalse(status["trusted"])
        self.assertFalse(status["enabled"])
        self.assertTrue(status["approval_stale"])

    def test_worker_rejects_extension_changed_after_parent_hash_check(self) -> None:
        path = self.make_extension(
            plugin_source=(
                "def handle(action, params, context):\n"
                "    return {'value': 'approved'}\n"
            )
        )
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        original_start = extensions_module._start_worker_process

        def mutate_then_start(current_record, request, env):
            (path / "plugin.py").write_text(
                "def handle(action, params, context):\n"
                "    return {'value': 'mutated-after-approval'}\n",
                encoding="utf-8",
            )
            return original_start(current_record, request, env)

        with mock.patch.object(extensions_module, "_start_worker_process", side_effect=mutate_then_start):
            with self.assertRaises(ToolError) as raised:
                self.registry.run(
                    "example",
                    "echo",
                    {"value": "ignored"},
                    workspace=Workspace(self.workspace_root),
                    read_only=True,
                )
        self.assertEqual(raised.exception.code, "EXTENSION_HASH_MISMATCH")

    def test_worker_executes_verified_snapshot_not_mutated_source_after_hash_check(self) -> None:
        path = self.make_extension(
            plugin_source=(
                "from pathlib import Path\n"
                "def handle(action, params, context):\n"
                "    return {'value': 'approved', 'resource': Path('resource.txt').read_text(encoding='utf-8')}\n"
            )
        )
        (path / "resource.txt").write_text("approved-resource", encoding="utf-8")
        record = load_extension(path, bundled=False)
        request = json.dumps(
            {
                "extension_sha256": record.sha256,
                "action": "echo",
                "params": {"value": "ignored"},
                "context": {},
            }
        ).encode("utf-8")

        class FakeStdin:
            def __init__(self, data: bytes) -> None:
                self.buffer = io.BytesIO(data)

        class FakeStdout:
            def __init__(self) -> None:
                self.buffer = io.BytesIO()

        fake_stdout = FakeStdout()
        original_spec = extension_worker_module.importlib.util.spec_from_file_location

        def mutate_source_before_import(name, location, *args, **kwargs):
            (path / "plugin.py").write_text(
                "def handle(action, params, context):\n"
                "    return {'value': 'mutated-after-worker-hash'}\n",
                encoding="utf-8",
            )
            (path / "resource.txt").write_text("mutated-resource", encoding="utf-8")
            return original_spec(name, location, *args, **kwargs)

        with mock.patch.object(extension_worker_module.sys, "stdin", FakeStdin(request)), mock.patch.object(
            extension_worker_module.sys, "stdout", fake_stdout
        ), mock.patch.object(
            extension_worker_module.importlib.util,
            "spec_from_file_location",
            side_effect=mutate_source_before_import,
        ):
            exit_code = extension_worker_module.worker_main(str(path), bundled=False)

        self.assertEqual(exit_code, 0)
        envelope = json.loads(fake_stdout.buffer.getvalue())
        self.assertEqual(envelope["result"]["value"], "approved")
        self.assertEqual(envelope["result"]["resource"], "approved-resource")
        self.assertIn("mutated-after-worker-hash", (path / "plugin.py").read_text(encoding="utf-8"))
        self.assertEqual((path / "resource.txt").read_text(encoding="utf-8"), "mutated-resource")

    def test_trust_store_concurrent_approvals_do_not_lose_records(self) -> None:
        self.make_extension("alpha")
        self.make_extension("beta")
        alpha = self.registry.get("alpha")
        beta = self.registry.get("beta")
        original_load = self.trust._load
        original_save = self.trust._save
        first_loaded = threading.Event()
        second_saved = threading.Event()

        def staged_load():
            records = original_load()
            if threading.current_thread().name == "trust-first":
                first_loaded.set()
                second_saved.wait(timeout=0.2)
            return records

        def staged_save(records):
            original_save(records)
            if threading.current_thread().name == "trust-second":
                second_saved.set()

        with mock.patch.object(self.trust, "_load", side_effect=staged_load), mock.patch.object(
            self.trust, "_save", side_effect=staged_save
        ):
            first = threading.Thread(target=lambda: self.trust.approve(alpha), name="trust-first")
            second = threading.Thread(target=lambda: self.trust.approve(beta), name="trust-second")
            first.start()
            self.assertTrue(first_loaded.wait(timeout=1))
            second.start()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        saved = self.trust._load()
        self.assertEqual(set(saved), {"alpha", "beta"})

    def test_dynamic_workspace_adapter_becomes_applicable_later(self) -> None:
        self.make_extension(
            adapter={
                "mode": "dynamic",
                "state": "profile",
                "detect": {"any_of": ["special.marker"], "all_of": []},
            },
            permissions=["workspace.read", "workspace.adapter", "extension.state"],
        )
        before = self.registry.describe(self.workspace_root)["extensions"][0]
        self.assertFalse(before["applicable"])

        (self.workspace_root / "special.marker").write_text("ready", encoding="utf-8")
        after = self.registry.describe(self.workspace_root)["extensions"][0]
        self.assertTrue(after["applicable"])

    def test_external_extension_requires_approval_and_runs_out_of_process(self) -> None:
        self.make_extension()
        denied = None
        try:
            self.registry.run(
                "example",
                "echo",
                {"value": "hello"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        except Exception as exc:  # assert ToolError code without importing internals here
            denied = exc
        self.assertIsNotNone(denied)
        self.assertEqual(getattr(denied, "code", None), "EXTENSION_NOT_TRUSTED")

        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        result = self.registry.run(
            "example",
            "echo",
            {"value": "hello"},
            workspace=Workspace(self.workspace_root),
            read_only=True,
        )
        self.assertEqual(result["value"], "hello")
        self.assertEqual(Path(result["workspace_root"]), self.workspace_root.resolve())
        self.assertTrue(result["state_dir"])
        self.assertEqual(result["extension_id"], "example")
        self.assertEqual(result["extension_action"], "echo")

    def test_action_schema_rejects_unknown_fields_before_worker_runs(self) -> None:
        self.make_extension()
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        with self.assertRaises(Exception) as caught:
            self.registry.run(
                "example",
                "echo",
                {"value": "ok", "command": "rm -rf /"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        self.assertEqual(getattr(caught.exception, "code", None), "INVALID_ARGUMENT")

    def test_bundled_read_only_action_can_opt_out_of_global_authorization(self) -> None:
        path = self.make_extension("bundled-status", bundled=True, authorization="none")
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["requires_workspace"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = self.registry.run(
            "bundled-status",
            "echo",
            {"value": "status"},
            workspace=Workspace(self.workspace_root),
            read_only=True,
        )
        self.assertEqual(result["value"], "status")

    def test_mcp_tool_catalog_stays_fixed_when_extensions_change(self) -> None:
        runtime = ToolRuntime(self.workspace_root, load_config(self.workspace_root))
        runtime.extensions = self.registry
        before = [tool["name"] for tool in runtime.list_tools()]
        self.make_extension()
        after = [tool["name"] for tool in runtime.list_tools()]

        self.assertEqual(before, after)
        self.assertIn("extension", after)
        listed = runtime.call("extension", {"action": "list"})["structuredContent"]
        self.assertEqual(listed["extensions"][0]["id"], "example")

    def test_only_declared_environment_crosses_worker_boundary(self) -> None:
        self.make_extension(
            permissions=["workspace.read", "extension.state", "environment.inherit:JUDGE_API_TOKEN"],
            plugin_source=(
                "import os\n"
                "def handle(action, params, context):\n"
                "    return {\n"
                "        'declared': os.environ.get('JUDGE_API_TOKEN'),\n"
                "        'declared_present': bool(os.environ.get('JUDGE_API_TOKEN')),\n"
                "        'control_plane': os.environ.get('CONTROL_PLANE_API_KEY'),\n"
                "        'inherited': context.get('inherited_environment', []),\n"
                "    }\n"
            ),
        )
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        old_token = os.environ.get("JUDGE_API_TOKEN")
        old_control = os.environ.get("CONTROL_PLANE_API_KEY")
        try:
            os.environ["JUDGE_API_TOKEN"] = "judge-secret"
            os.environ["CONTROL_PLANE_API_KEY"] = "must-not-cross"
            result = self.registry.run(
                "example",
                "echo",
                {"value": "ignored"},
                workspace=Workspace(self.workspace_root),
                read_only=True,
            )
        finally:
            if old_token is None:
                os.environ.pop("JUDGE_API_TOKEN", None)
            else:
                os.environ["JUDGE_API_TOKEN"] = old_token
            if old_control is None:
                os.environ.pop("CONTROL_PLANE_API_KEY", None)
            else:
                os.environ["CONTROL_PLANE_API_KEY"] = old_control
        self.assertEqual(result["declared"], "<redacted>")
        self.assertTrue(result["declared_present"])
        self.assertIsNone(result["control_plane"])
        self.assertEqual(result["inherited"], ["JUDGE_API_TOKEN"])

    def test_job_mode_supports_long_or_unlimited_timeout_and_status(self) -> None:
        path = self.make_extension(
            plugin_source=(
                "import time\n"
                "def handle(action, params, context):\n"
                "    time.sleep(0.08)\n"
                "    return {'done': True}\n"
            )
        )
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["run_mode"] = "job"
        manifest["actions"]["echo"]["timeout_seconds"] = 0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)

        started = self.registry.run(
            "example",
            "echo",
            {"value": "ignored"},
            workspace=Workspace(self.workspace_root),
            read_only=True,
        )
        self.assertEqual(started["status"], "running")
        self.assertEqual(started["timeout_seconds"], 0)

        deadline = time.monotonic() + 3
        status = self.registry.job_status(started["job_id"], workspace=Workspace(self.workspace_root))
        while status["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
            status = self.registry.job_status(started["job_id"], workspace=Workspace(self.workspace_root))
        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status["result"]["done"])

    def test_job_manager_rejects_unbounded_concurrency_before_spawning(self) -> None:
        path = self.make_extension()
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["run_mode"] = "job"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        self.registry.jobs._starting_jobs = MAX_RUNNING_EXTENSION_JOBS
        try:
            with self.assertRaises(ToolError) as raised:
                self.registry.run(
                    "example",
                    "echo",
                    {"value": "ignored"},
                    workspace=Workspace(self.workspace_root),
                    read_only=True,
                )
        finally:
            self.registry.jobs._starting_jobs = 0
        self.assertEqual(raised.exception.code, "EXTENSION_JOB_LIMIT")

    def test_job_admission_reservation_is_held_until_job_is_registered(self) -> None:
        path = self.make_extension(
            plugin_source=(
                "import time\n"
                "def handle(action, params, context):\n"
                "    time.sleep(0.05)\n"
                "    return {'done': True}\n"
            )
        )
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["run_mode"] = "job"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)

        original_job = extensions_module._ExtensionJob
        constructor_entered = threading.Event()
        release_constructor = threading.Event()
        first_result: list[dict[str, object]] = []
        first_error: list[BaseException] = []

        def blocking_job(*args, **kwargs):
            constructor_entered.set()
            if not release_constructor.wait(timeout=3):
                raise RuntimeError("test did not release job construction")
            return original_job(*args, **kwargs)

        def start_first() -> None:
            try:
                first_result.append(self.registry.run(
                    "example",
                    "echo",
                    {"value": "first"},
                    workspace=Workspace(self.workspace_root),
                    read_only=True,
                ))
            except BaseException as exc:  # surfaced to the test thread below
                first_error.append(exc)

        with mock.patch.object(extensions_module, "MAX_RUNNING_EXTENSION_JOBS", 1), mock.patch.object(
            extensions_module, "_ExtensionJob", side_effect=blocking_job
        ):
            thread = threading.Thread(target=start_first)
            thread.start()
            self.assertTrue(constructor_entered.wait(timeout=3))
            try:
                with self.assertRaises(ToolError) as raised:
                    self.registry.run(
                        "example",
                        "echo",
                        {"value": "second"},
                        workspace=Workspace(self.workspace_root),
                        read_only=True,
                    )
                self.assertEqual(raised.exception.code, "EXTENSION_JOB_LIMIT")
            finally:
                release_constructor.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(len(first_result), 1)

    def test_job_mode_accepts_more_than_two_hours(self) -> None:
        path = self.make_extension()
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["run_mode"] = "job"
        manifest["actions"]["echo"]["timeout_seconds"] = 3 * 60 * 60
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record = load_extension(path, bundled=False)
        self.assertEqual(record.manifest.actions["echo"].timeout_seconds, 10800)

    def test_workspace_artifacts_are_host_validated_and_hashed(self) -> None:
        path = self.make_extension(
            permissions=["workspace.read", "workspace.write", "extension.state"],
            plugin_source=(
                "from pathlib import Path\n"
                "def handle(action, params, context):\n"
                "    target = Path(context['workspace_root']) / 'judge-result.txt'\n"
                "    target.write_text('verdict', encoding='utf-8')\n"
                "    return {'workspace_artifacts': [{'path': 'judge-result.txt', 'kind': 'report', 'label': 'Judge result'}]}\n"
            ),
        )
        manifest_path = path / "folderbridge-extension.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["actions"]["echo"]["read_only"] = False
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        record = self.registry.get("example")
        self.trust.approve(record, enabled=True)
        result = self.registry.run(
            "example",
            "echo",
            {"value": "ignored"},
            workspace=Workspace(self.workspace_root),
            read_only=False,
        )
        artifact = result["workspace_artifacts"][0]
        self.assertEqual(artifact["path"], "judge-result.txt")
        self.assertEqual(artifact["kind"], "report")
        self.assertEqual(artifact["size"], len("verdict"))
        self.assertEqual(len(artifact["sha256"]), 64)

    def test_bundled_comfyui_manifest_is_discoverable_from_repository(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        record = load_extension(project_root / "extensions" / "comfyui", bundled=True)
        self.assertEqual(record.manifest.extension_id, "comfyui")
        self.assertIn("status", record.manifest.actions)
        self.assertIn("run", record.manifest.actions)
        self.assertIn("network.loopback:127.0.0.1:8188", record.manifest.permissions)


if __name__ == "__main__":
    unittest.main()
