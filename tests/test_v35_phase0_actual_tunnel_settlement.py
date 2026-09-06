from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from folderbridge_mcp.launcher_backend import LauncherSettings, build_init_argv


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/v35-phase0-remote-settlement-capability-matrix-20260905.json"
EVIDENCE_V0013 = ROOT / "docs/v35-phase0-actual-tunnel-settlement-v0013-red-20260905.md"
EVIDENCE_V0014 = ROOT / "docs/v35-phase0-actual-tunnel-settlement-v0014-source-contract-red-20260905.md"
RELIABILITY_V0014 = ROOT / "docs/v35-phase0-tunnel-v0014-shared-stdio-reliability-candidate-20260905.md"
HEALTH_EVIDENCE = ROOT / "docs/v35-phase0-end-to-end-data-plane-health-acceptance-20260905.md"


class V35ActualTunnelSettlementGateRedAcceptanceTests(unittest.TestCase):
    def test_actual_v0013_candidate_is_explicitly_fail_closed(self) -> None:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        transport = matrix["transport_evidence"]
        verdict = matrix["activation_verdict"]

        self.assertEqual(
            transport["production_candidate_tunnel_client_version_evaluated"],
            "v0.0.13",
        )
        self.assertEqual(transport["latest_official_source_contract_candidate_evaluated"], "v0.0.14")
        self.assertTrue(transport["latest_candidate_shared_stdio_source_contract_fix_present"])
        self.assertEqual(
            transport["latest_candidate_shared_stdio_dynamic_acceptance"],
            "GREEN",
        )
        self.assertFalse(transport["latest_candidate_settlement_seam_exposed_to_folderbridge"])
        self.assertFalse(
            transport["downstream_tunnel_request_identity_exposed_to_folderbridge"]
        )
        self.assertFalse(
            transport["per_command_response_post_ack_exposed_to_folderbridge"]
        )
        self.assertFalse(transport["structured_delivery_ack_proven"])
        self.assertEqual(transport["gate3_verdict"], "RED_BLOCKED")
        self.assertFalse(verdict["persistent_side_effecting_remote_cutover_allowed"])
        self.assertEqual(verdict["status"], "FORBIDDEN")

    def test_repo_candidate_gate2_is_recorded_without_promoting_live_runtime_rows(self) -> None:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        runtime = matrix["runtime_activation_evidence"]

        self.assertTrue(
            runtime[
                "repo_candidate_durable_operation_receipt_effect_boundary_wired_to_tool_invocations"
            ]
        )
        self.assertTrue(
            runtime[
                "repo_candidate_per_admission_journal_pin_key_capacity_wired_and_proven"
            ]
        )
        self.assertFalse(runtime["current_loaded_runtime_snapshot_revalidated_after_gate2"])
        self.assertTrue(matrix["rows"])
        self.assertTrue(
            all(
                row.get("persistent_tunnel_allowed") is False
                for row in matrix["rows"]
            )
        )

    def test_current_launcher_candidate_has_no_folderbridge_settlement_callback_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve()
            settings = LauncherSettings(
                workspaces=[str(workspace)],
                access_mode="read_write",
                profile="folderbridge",
                tunnel_id="tunnel_0123456789abcdef",
            )
            argv = build_init_argv(
                Path("tunnel-client.exe"),
                settings,
                (workspace,),
            )

        self.assertIn("--mcp-command", argv)
        self.assertNotIn("--mcp-server-url", argv)
        rendered = " ".join(argv).lower()
        self.assertNotIn("settlement-callback", rendered)
        self.assertNotIn("response-ack-callback", rendered)

    def test_red_evidence_names_the_exact_missing_pre_and_post_effect_seams(self) -> None:
        evidence = EVIDENCE_V0013.read_text(encoding="utf-8")

        self.assertIn("GATE 3: RED / BLOCKED", evidence)
        self.assertIn("request_id", evidence)
        self.assertIn("PostResponse", evidence)
        self.assertIn("before the side effect", evidence)
        self.assertIn("structured per-command callback/API", evidence)
        self.assertIn("side-effecting Tunnel activation: **FORBIDDEN**", evidence)

    def test_v0014_reliability_and_settlement_remain_separate_fail_closed_claims(self) -> None:
        settlement = EVIDENCE_V0014.read_text(encoding="utf-8")
        reliability = RELIABILITY_V0014.read_text(encoding="utf-8")

        self.assertIn("Gate 3 — Actual Tunnel Settlement E2E = RED / BLOCKED", settlement)
        self.assertIn("v0.0.14 settlement seam for current external-EXE FolderBridge integration = NOT FOUND", settlement)
        self.assertIn("shared-stdio reliability + truthful Launcher health integration = GREEN", reliability)
        self.assertIn("Windows v0.0.14 exact response-deadline retirement dynamic acceptance = GREEN", reliability)
        self.assertIn("Gate 3 settlement = RED / BLOCKED", reliability)

    def test_data_plane_health_evidence_refuses_process_only_false_green(self) -> None:
        health = HEALTH_EVIDENCE.read_text(encoding="utf-8")

        self.assertIn("end_to_end_data_plane_healthy authority seam = WIRED / authoritative provider NOT AVAILABLE", health)
        self.assertIn("current Launcher false-green prevention = GREEN", health)
        self.assertIn("process_alive", health)
        self.assertIn("control_plane_alive", health)
        self.assertIn("mcp_child_alive", health)
        self.assertIn("end_to_end_data_plane_healthy", health)
        self.assertIn("Do not label `UNKNOWN` as connected/healthy.", health)


if __name__ == "__main__":
    unittest.main()
