from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.extensions import load_extension


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "Plugins" / "extensions" / "chatgpt-web-llm-adapter"
PLUGIN_TEST_PATH = PLUGIN_ROOT / "tests" / "test_plugin.py"

_PLUGIN_TEST_SPEC = importlib.util.spec_from_file_location("folderbridge_external_chatgpt_web_llm_adapter_selftests", PLUGIN_TEST_PATH)
if _PLUGIN_TEST_SPEC is None or _PLUGIN_TEST_SPEC.loader is None:
    raise RuntimeError("Could not load ChatGPT Web LLM Test Adapter self-tests")
_PLUGIN_TEST_MODULE = importlib.util.module_from_spec(_PLUGIN_TEST_SPEC)
_old_path = list(sys.path)
try:
    sys.path.insert(0, str(PLUGIN_ROOT))
    _PLUGIN_TEST_SPEC.loader.exec_module(_PLUGIN_TEST_MODULE)
finally:
    sys.path[:] = _old_path
for _test_name in dir(_PLUGIN_TEST_MODULE):
    _test_type = getattr(_PLUGIN_TEST_MODULE, _test_name)
    if isinstance(_test_type, type) and issubclass(_test_type, unittest.TestCase) and _test_type is not unittest.TestCase:
        globals()[f"PluginSelfTest_{_test_name}"] = _test_type


class ExternalChatGptWebLlmAdapterTests(unittest.TestCase):
    def test_manifest_is_generic_test_only_loopback_provider(self) -> None:
        record = load_extension(PLUGIN_ROOT, bundled=False)
        self.assertEqual(record.manifest.extension_id, "chatgpt-web-llm-adapter")
        self.assertFalse(record.bundled)
        self.assertEqual(record.manifest.version, "0.3.2")
        self.assertIn("extension.state", record.manifest.permissions)
        self.assertIn("network.loopback:127.0.0.1:8767", record.manifest.permissions)
        self.assertIn("network.loopback:127.0.0.1:8768", record.manifest.permissions)
        self.assertIn("network.outbound:https", record.manifest.permissions)
        self.assertIn("process.execute:chrome.exe", record.manifest.permissions)
        self.assertIn("process.execute:msedge.exe", record.manifest.permissions)
        self.assertEqual(set(record.manifest.actions), {"status", "connection", "serve", "verification-plan"})
        serve = record.manifest.actions["serve"]
        self.assertEqual(serve.authorization, "global")
        self.assertEqual(serve.run_mode, "job")
        self.assertEqual(serve.timeout_seconds, 0)
        self.assertEqual(serve.mutation_scope.mode, "none")
        self.assertEqual(serve.effect_contract.direct.effect_semantics, "external_effect")
        self.assertEqual(serve.effect_contract.direct.lifetime, "job_owned")
        for name in ["status", "connection", "verification-plan"]:
            action = record.manifest.actions[name]
            self.assertTrue(action.read_only)
            self.assertEqual(action.mutation_scope.mode, "none")
            self.assertEqual(action.authorization, "global")

    def test_source_uses_only_public_extension_api_and_no_arbitrary_command_or_url_parameter(self) -> None:
        plugin = (PLUGIN_ROOT / "plugin.py").read_text(encoding="utf-8")
        runtime = (PLUGIN_ROOT / "browser_runtime.py").read_text(encoding="utf-8")
        manifest = (PLUGIN_ROOT / "folderbridge-extension.json").read_text(encoding="utf-8")
        self.assertIn("folderbridge_mcp.extension_api", plugin)
        self.assertIn("folderbridge_mcp.extension_api", runtime)
        self.assertIn("except ModuleNotFoundError", runtime)
        self.assertNotIn("folderbridge_mcp.security", plugin + runtime)
        self.assertNotIn("folderbridge_mcp.extensions", plugin + runtime)
        self.assertNotIn('"url"', manifest)
        self.assertNotIn('"command"', manifest)
        self.assertNotIn('"executable"', manifest)
        self.assertIn('"max_parallel"', manifest)
        self.assertIn('"maximum": 4, "default": 2', manifest)
        self.assertIn('"response_timeout_seconds"', manifest)
        self.assertIn('CHATGPT_WEB_ADAPTER_PORT", 8767', runtime)
        self.assertIn('CHATGPT_WEB_ADAPTER_DEVTOOLS_PORT", 8768', runtime)
        self.assertIn('f"--remote-debugging-port={DEVTOOLS_PORT}"', runtime)
        self.assertIn('"--disable-extensions"', runtime)
        self.assertIn('def provider_state(self) -> str:', runtime)

    def test_install_script_is_staged_and_targets_per_user_hot_load_root(self) -> None:
        source = (PLUGIN_ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("folderbridge-mcp\\extensions", source)
        self.assertIn("chatgpt-web-llm-adapter", source)
        self.assertIn("browser_runtime.py", source)
        self.assertIn("standalone.py", source)
        self.assertIn("launch-standalone.ps1", source)
        self.assertIn("ReparsePoint", source)
        self.assertIn("Move-Item", source)
        self.assertNotIn("Invoke-Expression", source)
        self.assertNotIn("iex ", source.lower())

    def test_readme_explicitly_supports_multiple_clients_without_claiming_api_equivalence(self) -> None:
        source = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("认知星图", source)
        self.assertIn("Debate-Judge", source)
        self.assertIn("fresh chat per request", source)
        self.assertIn("prompt_wrapped", source)
        self.assertIn("不能", source)
        self.assertIn("OpenAI", source)
        self.assertIn("Standalone 0.3.2", source)
        self.assertIn("5 分钟最多启动 8 个", source)
        self.assertIn("没有宣称自动启用 Temporary Chat", source)
        self.assertIn("127.0.0.1:8769", source)
        self.assertIn("LIVE_PROBE_PASS", source)

    def test_standalone_launcher_is_independent_and_has_bounded_lifecycle(self) -> None:
        standalone = (PLUGIN_ROOT / "standalone.py").read_text(encoding="utf-8")
        launcher = (PLUGIN_ROOT / "launch-standalone.ps1").read_text(encoding="utf-8")
        self.assertIn('CHATGPT_WEB_ADAPTER_PORT", "8769"', standalone)
        self.assertIn('CHATGPT_WEB_ADAPTER_DEVTOOLS_PORT", "8770"', standalone)
        self.assertIn("run_service", standalone)
        self.assertIn("--probe", standalone)
        self.assertIn("--stop", standalone)
        self.assertIn("LIVE_PROBE_PASS", standalone)
        self.assertIn("WAITING_PAGE", standalone)
        self.assertIn("WAITING_LOGIN_OR_CHALLENGE", standalone)
        self.assertIn("fresh ChatGPT conversation", standalone)
        self.assertIn("/control/open-login", standalone)
        self.assertIn("/control/max-parallel", standalone)
        self.assertIn("网页 ChatGPT 并发上限", standalone)
        self.assertIn("default=2", standalone)
        self.assertIn("打开 ChatGPT 登录 / 验证页", standalone)
        self.assertIn("'Authorization':'Bearer '+", standalone)
        self.assertNotIn("shell=True", standalone)
        self.assertIn(".build-venv\\Scripts\\python.exe", launcher)
        self.assertNotIn("Invoke-Expression", launcher)
        # Windows PowerShell 5.1 may decode BOM-less UTF-8 scripts through the
        # active ANSI code page. Keep this bootstrap ASCII-only so localized
        # output can never corrupt quoting or braces at parse time.
        launcher.encode("ascii")
        for name in [
            "启动-ChatGPT-Web-LLM-Standalone.cmd",
            "验证-ChatGPT-Web-LLM-Standalone.cmd",
            "状态-ChatGPT-Web-LLM-Standalone.cmd",
            "停止-ChatGPT-Web-LLM-Standalone.cmd",
        ]:
            path = ROOT / name
            self.assertTrue(path.is_file(), name)
            source = path.read_text(encoding="utf-8")
            self.assertIn("launch-standalone.ps1", source)

    def test_powershell_51_can_parse_standalone_launcher(self) -> None:
        launcher = str(PLUGIN_ROOT / "launch-standalone.ps1")
        escaped = launcher.replace("'", "''")
        command = (
            f"$path='{escaped}';$tokens=$null;$errors=$null;"
            "[System.Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_standalone_status_runs_without_folderbridge_package_on_script_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(PLUGIN_ROOT / "standalone.py"), "--status", "--state-dir", temporary],
                cwd=PLUGIN_ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15,
                check=False,
                env={**os.environ, "CHATGPT_WEB_ADAPTER_PORT": "39769", "CHATGPT_WEB_ADAPTER_DEVTOOLS_PORT": "39770"},
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        payload = result.stdout
        self.assertIn('"standalone_version": "0.3.2"', payload)
        self.assertIn('"base_url": "http://127.0.0.1:39769/v1"', payload)
        self.assertIn('"running": false', payload)


if __name__ == "__main__":
    unittest.main()
