from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from folderbridge_mcp.launcher_backend import (
    MAX_COMMAND_OUTPUT,
    LauncherError,
    LauncherSettings,
    LauncherSettingsStore,
    build_doctor_argv,
    build_init_argv,
    build_run_argv,
    control_plane_environment,
    find_tunnel_client,
    mcp_argv,
    mcp_command,
    redact_text,
    render_client_config,
    run_short_command,
)


class LauncherBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace with spaces"
        self.root.mkdir()
        self.client = Path(self.temp.name) / ("tunnel-client.exe" if os.name == "nt" else "tunnel-client")
        self.client.write_bytes(b"not executed")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def settings(self) -> LauncherSettings:
        return LauncherSettings(
            workspace=str(self.root),
            access_mode="read_only",
            profile="folderbridge_1",
            tunnel_id="tunnel_0123456789abcdef0123456789abcdef",
            tunnel_client_path=str(self.client),
        )

    def test_settings_store_never_has_an_api_key_field(self) -> None:
        path = Path(self.temp.name) / "settings" / "launcher.json"
        store = LauncherSettingsStore(path)
        settings = self.settings()
        secret = "runtime-test-never-save-this-value"
        environment = control_plane_environment(secret)

        store.save(settings)

        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertNotIn(secret, raw)
        self.assertNotIn("api_key", parsed)
        self.assertEqual(environment["CONTROL_PLANE_API_KEY"], secret)
        self.assertEqual(store.load(), settings)

    def test_settings_store_rejects_unknown_or_wrong_typed_fields(self) -> None:
        path = Path(self.temp.name) / "launcher.json"
        path.write_text('{"workspace": ".", "unexpected": "value"}', encoding="utf-8")
        self.assertEqual(LauncherSettingsStore(path).load(), LauncherSettings())

        path.write_text('{"version": 1, "workspace": 7}', encoding="utf-8")
        self.assertEqual(LauncherSettingsStore(path).load(), LauncherSettings())

    def test_settings_validate_profile_and_tunnel_id_before_command_building(self) -> None:
        settings = self.settings()
        settings.profile = "name; run-something"
        with self.assertRaises(LauncherError):
            settings.validate(require_tunnel_id=True)

        settings = self.settings()
        settings.tunnel_id = "tunnel_bad value"
        with self.assertRaises(LauncherError):
            settings.validate(require_tunnel_id=True)

    def test_tunnel_commands_are_argument_arrays(self) -> None:
        settings = self.settings()
        workspace = settings.validate(require_tunnel_id=True)
        init = build_init_argv(self.client, settings, workspace)

        self.assertEqual(init[:5], [str(self.client), "init", "--sample", "sample_mcp_stdio_local", "--profile"])
        self.assertEqual(init[5], settings.profile)
        self.assertEqual(init[6:8], ["--tunnel-id", settings.tunnel_id])
        self.assertEqual(init[8], "--mcp-command")
        expected_workspace = str(self.root).replace("\\", "/") if os.name == "nt" else str(self.root)
        self.assertIn(expected_workspace, init[9])
        self.assertEqual(build_doctor_argv(self.client, settings.profile), [str(self.client), "doctor", "--profile", settings.profile, "--explain"])
        self.assertEqual(build_run_argv(self.client, settings.profile), [str(self.client), "run", "--profile", settings.profile])

    def test_frozen_build_uses_the_executable_as_the_stdio_server(self) -> None:
        settings = self.settings()
        workspace = settings.validate(require_tunnel_id=True)
        with mock.patch.object(sys, "frozen", True, create=True):
            argv = mcp_argv(workspace, "read_only", False)
        self.assertEqual(argv[0], str(Path(sys.executable).resolve()))
        self.assertEqual(argv[1:4], ["serve", "--workspace", str(workspace)])
        self.assertEqual(argv[-1], "--read-only")

    def test_client_configs_preserve_command_and_argument_boundaries(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            rendered_json = render_client_config(self.root, "read_only", False, "json")
            rendered_toml = render_client_config(self.root, "read_only", False, "toml")
            rendered_command = render_client_config(self.root, "read_only", False, "tunnel")

        parsed = json.loads(rendered_json)
        server = parsed["mcpServers"]["folderbridge"]
        self.assertEqual(server["command"], str(Path(sys.executable).resolve()))
        self.assertEqual(server["args"][:3], ["serve", "--workspace", str(self.root)])
        self.assertEqual(server["args"][-1], "--read-only")
        self.assertIn("[mcp_servers.folderbridge]", rendered_toml)
        self.assertIn(str(self.root).replace("\\", "\\\\"), rendered_toml)
        self.assertIn("--read-only", rendered_command)

    def test_client_config_rejects_unknown_output_format(self) -> None:
        with self.assertRaises(LauncherError):
            render_client_config(self.root, "read_only", False, "yaml")

    @unittest.skipUnless(os.name == "nt", "Windows tunnel command quoting regression")
    def test_tunnel_command_preserves_windows_paths_for_posix_style_parser(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            command = mcp_command(self.root, "read_only", False)

        executable = str(Path(sys.executable).resolve()).replace("\\", "/")
        workspace = str(self.root).replace("\\", "/")
        self.assertIn(executable, command)
        self.assertIn(workspace, command)
        self.assertNotIn("\\", command)

    def test_runtime_component_is_not_accepted_as_tunnel_client(self) -> None:
        suffix = ".exe" if os.name == "nt" else ""
        runtime = Path(self.temp.name) / f"tunnel-client-runtime-cloudflared{suffix}"
        runtime.write_bytes(b"not a tunnel client")

        self.assertEqual(find_tunnel_client(str(self.client)), self.client.resolve())
        self.assertIsNone(find_tunnel_client(str(runtime)))

    def test_fingerprint_changes_when_workspace_access_changes(self) -> None:
        settings = self.settings()
        first = settings.fingerprint()
        settings.access_mode = "read_write"
        second = settings.fingerprint()
        self.assertNotEqual(first, second)

    def test_control_plane_environment_is_a_copy(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            env = control_plane_environment("sk-memory-only")
            self.assertEqual(env["CONTROL_PLANE_API_KEY"], "sk-memory-only")
            self.assertNotIn("CONTROL_PLANE_API_KEY", os.environ)
            with self.assertRaises(LauncherError):
                control_plane_environment("")

    def test_redaction_covers_explicit_and_bearer_secrets(self) -> None:
        secret = "runtime-secret-value"
        prefixed_key = "sk-" + "example12345"
        text = f"value={secret} Authorization: Bearer abcdef {prefixed_key}"
        redacted = redact_text(text, (secret,))
        self.assertNotIn(secret, redacted)
        self.assertNotIn("abcdef", redacted)
        self.assertNotIn(prefixed_key, redacted)

    def test_short_command_captures_output_without_a_shell(self) -> None:
        result = run_short_command(
            [sys.executable, "-c", "print('launcher-ok')"],
            env=dict(os.environ),
            timeout_seconds=10,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertIn("launcher-ok", result.output)
        self.assertFalse(result.timed_out)

    def test_short_command_output_is_memory_bounded(self) -> None:
        result = run_short_command(
            [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {MAX_COMMAND_OUTPUT + 65536})"],
            env=dict(os.environ),
            timeout_seconds=10,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.output), MAX_COMMAND_OUTPUT + 64)


if __name__ == "__main__":
    unittest.main()
