from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from folderbridge_mcp.config import load_config
from folderbridge_mcp.tools import ToolRuntime


class LifecycleResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.runtimes: list[ToolRuntime] = []

    def tearDown(self) -> None:
        for runtime in reversed(self.runtimes):
            runtime.close()
        self.temporary.cleanup()

    def make_runtime(
        self,
        *,
        capabilities: tuple[str, ...] | list[str] = (),
        allow_tasks: bool = False,
    ) -> ToolRuntime:
        runtime = ToolRuntime(
            self.root,
            load_config(self.root),
            capabilities=capabilities,
            allow_tasks=allow_tasks,
        )
        self.runtimes.append(runtime)
        return runtime

    def test_server_info_exposes_canonical_continuity_contract(self) -> None:
        runtime = self.make_runtime(capabilities=["test"], allow_tasks=True)

        info = runtime.call("server_info", {})["structuredContent"]
        continuity = info["continuity"]

        self.assertEqual(continuity["runtime_state"], "ready")
        self.assertIsInstance(continuity["catalog_revision"], int)
        self.assertGreaterEqual(continuity["catalog_revision"], 1)
        self.assertTrue(continuity["diagnostics_available"])
        self.assertRegex(continuity["catalog_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("state", continuity)
        self.assertNotIn("recovery_available", continuity)

    def test_tool_surface_is_derived_from_actual_list_tools(self) -> None:
        runtime = self.make_runtime(capabilities=["test", "package-windows"], allow_tasks=True)

        listed = runtime.list_tools()
        info = runtime.call("server_info", {})["structuredContent"]
        surface = info["tool_surface"]

        expected_names = [tool["name"] for tool in listed]
        self.assertEqual(surface["tool_names"], expected_names)
        self.assertEqual(surface["tool_count"], len(expected_names))
        self.assertTrue(surface["run_capability_exposed"])
        self.assertTrue(surface["run_task_exposed"])

        run_capability = next(tool for tool in listed if tool["name"] == "run_capability")
        self.assertEqual(
            surface["run_capability_names"],
            run_capability["inputSchema"]["properties"]["name"]["enum"],
        )
        self.assertEqual(surface["run_capability_names"], info["global_capabilities_enabled"])
        self.assertEqual(surface["run_task_exposed"], info["task_execution_enabled"])

    def test_tool_surface_reports_disabled_execution_interfaces_truthfully(self) -> None:
        runtime = self.make_runtime()

        info = runtime.call("server_info", {})["structuredContent"]
        surface = info["tool_surface"]

        self.assertFalse(surface["run_capability_exposed"])
        self.assertFalse(surface["run_task_exposed"])
        self.assertEqual(surface["run_capability_names"], [])
        self.assertEqual(info["global_capabilities_enabled"], [])
        self.assertFalse(info["task_execution_enabled"])

    def test_catalog_identity_is_stable_across_repeated_calls(self) -> None:
        runtime = self.make_runtime(capabilities=["test"])

        first = runtime.call("server_info", {})["structuredContent"]
        second = runtime.call("server_info", {})["structuredContent"]

        self.assertEqual(first["continuity"]["catalog_revision"], second["continuity"]["catalog_revision"])
        self.assertEqual(first["continuity"]["catalog_sha256"], second["continuity"]["catalog_sha256"])
        self.assertEqual(first["tool_surface"], second["tool_surface"])

    def test_server_info_self_diagnoses_tool_surface_binding_failure(self) -> None:
        runtime = self.make_runtime(capabilities=["test"], allow_tasks=True)
        filtered = [
            tool for tool in runtime.list_tools()
            if tool["name"] != "run_capability"
        ]

        with mock.patch.object(runtime, "list_tools", return_value=filtered):
            info = runtime.call("server_info", {})["structuredContent"]

        self.assertFalse(info["healthy"])
        self.assertFalse(info["tool_surface"]["binding_consistent"])
        self.assertIn("run_capability_missing", info["tool_surface"]["binding_issues"])
        self.assertTrue(
            any(issue.get("code") == "TOOL_SURFACE_BINDING_FAILURE" for issue in info["issues"])
        )

    def test_listed_tools_have_dispatch_handlers(self) -> None:
        runtime = self.make_runtime(capabilities=["test"], allow_tasks=True)

        for tool in runtime.list_tools():
            result = runtime.call(tool["name"], {})
            if result.get("isError"):
                self.assertNotEqual(
                    result["structuredContent"]["error"]["code"],
                    "UNKNOWN_TOOL",
                    tool["name"],
                )

    def test_runtime_state_reports_shutdown_without_losing_diagnostics(self) -> None:
        runtime = self.make_runtime(capabilities=["test"])
        runtime.begin_shutdown()

        info = runtime.call("server_info", {})["structuredContent"]

        self.assertEqual(info["continuity"]["runtime_state"], "shutting_down")
        self.assertTrue(info["continuity"]["diagnostics_available"])
        self.assertIn("flight_recorder", info["tool_surface"]["tool_names"])


if __name__ == "__main__":
    unittest.main()
