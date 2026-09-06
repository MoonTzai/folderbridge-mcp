from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs/v35-phase0-remote-settlement-capability-matrix-20260905.json"
RELIABILITY = ROOT / "docs/v35-phase0-tunnel-v0014-shared-stdio-reliability-candidate-20260905.md"
SETTLEMENT = ROOT / "docs/v35-phase0-actual-tunnel-settlement-v0014-source-contract-red-20260905.md"
HEALTH = ROOT / "docs/v35-phase0-end-to-end-data-plane-health-acceptance-20260905.md"
WINDOWS_DYNAMIC_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-windows-dynamic-acceptance-raw-20260905.json"
REAL_FOLDERBRIDGE_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-real-folderbridge-devproxy-raw-20260905.json"
MULTISESSION_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-real-folderbridge-multisession-raw-20260905.json"
NORMAL_RUN_CHILD_DEATH_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-normal-run-child-death-raw-20260905.json"
DEV_PROXY_CHILD_DEATH_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-child-death-health-raw-20260905.json"
SLOW_OPERATION_RAW = ROOT / "docs/v35-phase0-tunnel-v0014-slow-real-folderbridge-raw-20260905.json"

PINNED_ARCHIVE_SHA256 = "7ba72d36560fd08cada793a8d569cb66b888b4fa87bb8709edf45eb4fc8fe170"
PINNED_TRANSPORT_SHA256 = "22107bba2664f27a439066a705b93943c7004cb981038b11985d0dfb3d95a70c"
PINNED_E2E_SHA256 = "616a42cd2089c3dc355120e7d96303c6e4a22b4831bd1181c693bbb976b38ad8"


class V35TunnelV0014SharedStdioCandidateEvidenceTests(unittest.TestCase):
    def test_matrix_keeps_live_v0013_separate_from_latest_v0014_candidate(self) -> None:
        matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
        transport = matrix["transport_evidence"]

        self.assertEqual(
            transport["production_candidate_tunnel_client_version_evaluated"],
            "v0.0.13",
        )
        self.assertEqual(
            transport["latest_official_source_contract_candidate_evaluated"],
            "v0.0.14",
        )
        self.assertTrue(transport["latest_candidate_shared_stdio_source_contract_fix_present"])
        self.assertEqual(
            transport["latest_candidate_shared_stdio_dynamic_acceptance"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_broader_shared_stdio_signing"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_windows_exact_response_deadline_retirement"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_real_folderbridge_readonly_data_plane"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_multiple_logical_initializations_same_id_concurrency"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_normal_run_child_death_fail_closed"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_dev_proxy_child_death_health"],
            "RED_FALSE_GREEN_LAB_WRAPPER_ONLY",
        )
        self.assertEqual(
            transport["latest_candidate_slow_18_to_30_second_real_folderbridge_operation"],
            "GREEN",
        )
        self.assertEqual(
            transport["latest_candidate_launcher_end_to_end_health_wiring"],
            "GREEN_TRUTHFUL_NO_AUTHORITATIVE_PROVIDER",
        )
        self.assertFalse(transport["latest_candidate_authoritative_end_to_end_health_provider_available"])
        self.assertFalse(transport["latest_candidate_settlement_seam_exposed_to_folderbridge"])
        self.assertEqual(transport["gate3_verdict"], "RED_BLOCKED")

    def test_reliability_evidence_pins_exact_official_source_without_claiming_dynamic_green(self) -> None:
        evidence = RELIABILITY.read_text(encoding="utf-8")

        self.assertIn(PINNED_ARCHIVE_SHA256, evidence)
        self.assertIn(PINNED_TRANSPORT_SHA256, evidence)
        self.assertIn(PINNED_E2E_SHA256, evidence)
        self.assertIn("source-contract fix for incident failure family = GREEN", evidence)
        self.assertIn("Windows v0.0.14 exact response-deadline retirement dynamic acceptance = GREEN", evidence)
        self.assertIn("real FolderBridge v0.0.14 read-only data plane = GREEN", evidence)
        self.assertIn("shared-stdio dynamic transport acceptance = GREEN", evidence)
        self.assertIn("shared-stdio reliability + truthful Launcher health integration = GREEN", evidence)
        self.assertIn("Ran 608 tests in 127.560s", evidence)
        self.assertIn("go.dev -> 198.18.0.14", evidence)
        self.assertIn("dl.google.com -> 198.18.0.15", evidence)

    def test_settlement_evidence_refuses_to_promote_reliability_to_gate3(self) -> None:
        evidence = SETTLEMENT.read_text(encoding="utf-8")

        self.assertIn("Gate 3 — Actual Tunnel Settlement E2E = RED / BLOCKED", evidence)
        self.assertIn(
            "v0.0.14 settlement seam for current external-EXE FolderBridge integration = NOT FOUND",
            evidence,
        )
        self.assertIn("PostResponse", evidence)
        self.assertIn("process-internal", evidence)
        self.assertIn("bare `rpc_request_id`", evidence)

    def test_health_evidence_keeps_end_to_end_layer_unproven(self) -> None:
        evidence = HEALTH.read_text(encoding="utf-8")

        self.assertIn("end_to_end_data_plane_healthy authority seam = WIRED / authoritative provider NOT AVAILABLE", evidence)
        self.assertIn("current Launcher false-green prevention = GREEN", evidence)
        self.assertIn("Do not label `UNKNOWN` as connected/healthy.", evidence)
        self.assertIn("dev proxy", evidence)
        self.assertIn("not** by itself an exact response-deadline-retirement harness", evidence)
        self.assertIn("authority seam = WIRED / authoritative provider NOT AVAILABLE", evidence)
        self.assertIn("RED_FALSE_GREEN", evidence)
        self.assertIn("fail-closed GREEN", evidence)

    def test_dynamic_raw_evidence_keeps_reliability_health_and_settlement_scopes_separate(self) -> None:
        windows = json.loads(WINDOWS_DYNAMIC_RAW.read_text(encoding="utf-8"))
        real_fb = json.loads(REAL_FOLDERBRIDGE_RAW.read_text(encoding="utf-8"))
        multi = json.loads(MULTISESSION_RAW.read_text(encoding="utf-8"))
        normal_child = json.loads(NORMAL_RUN_CHILD_DEATH_RAW.read_text(encoding="utf-8"))
        dev_child = json.loads(DEV_PROXY_CHILD_DEATH_RAW.read_text(encoding="utf-8"))
        slow = json.loads(SLOW_OPERATION_RAW.read_text(encoding="utf-8"))

        self.assertEqual(
            windows["verdict"]["windows_v0014_exact_response_deadline_retirement"],
            "GREEN",
        )
        self.assertFalse(windows["control_plane"]["timeout_response_posted"])
        self.assertTrue(windows["shared_stdio"]["private_alias_used_for_reused_retired_id"])
        self.assertEqual(windows["control_plane"]["recovery_upstream_rpc_id"], 0)
        self.assertEqual(windows["verdict"]["gate3_settlement"], "UNCHANGED_RED_BLOCKED")

        self.assertEqual(real_fb["verdict"]["real_folderbridge_v0014_readonly_data_plane"], "GREEN")
        self.assertTrue(real_fb["data_plane"]["concurrent_independent_requests_succeeded"])
        self.assertFalse(real_fb["scope_limit"]["exact_response_deadline_retirement_proven_by_this_lane"])

        self.assertEqual(multi["logical_ingress_initializations"], 2)
        self.assertEqual(multi["concurrent_same_id_results"], [0, 0])
        self.assertEqual(multi["verdict"]["concurrent_same_caller_id_real_folderbridge"], "GREEN")

        self.assertEqual(normal_child["verdict"]["normal_run_child_death_fail_closed"], "GREEN")
        self.assertFalse(normal_child["verdict"]["normal_run_health_remained_false_green"])
        self.assertEqual(dev_child["verdict"]["candidate_child_death_health"], "RED_FALSE_GREEN")
        self.assertTrue(dev_child["false_green_confirmed"])

        self.assertEqual(slow["verdict"]["slow_18_to_30_second_real_folderbridge_operation"], "GREEN")
        self.assertEqual(slow["verdict"]["independent_later_request_not_globally_wedged"], "GREEN")
        self.assertEqual(slow["independent_request"]["latency_seconds"], 0.046)
        self.assertEqual(slow["slow_operation"]["seconds_until_job_observation"], 21.0)
        self.assertFalse(slow["slow_operation"]["upstream_terminal_response_posted"])
        self.assertTrue(slow["late_response"]["retired_response_not_posted_upstream"])
        self.assertEqual(slow["verdict"]["gate3_settlement"], "UNCHANGED_RED_BLOCKED")


if __name__ == "__main__":
    unittest.main()
