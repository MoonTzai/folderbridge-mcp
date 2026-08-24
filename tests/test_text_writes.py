from __future__ import annotations

import hashlib
import os
import stat
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from folderbridge_mcp.security import ToolError, Workspace
import folderbridge_mcp.text_writes as text_writes
from folderbridge_mcp.text_writes import (
    MAX_TRANSACTION_CHUNK_BYTES,
    TRANSACTION_TTL_SECONDS,
    TextWriteManager,
)


class TextWriteManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "repo"
        self.root.mkdir()
        self.workspace = Workspace(self.root)
        self.staging = base / "staging"
        self.manager = TextWriteManager(self.staging)

    def tearDown(self) -> None:
        self.manager.close()
        self.temporary.cleanup()

    def _stage(self, path: str, text: str, *, mode: str = "create", baseline: str | None = None) -> tuple[str, bytes]:
        started = self.manager.begin(
            self.workspace,
            path,
            mode=mode,
            expected_target_sha256=baseline,
        )
        transaction_id = started["transaction_id"]
        payload = text.encode("utf-8")
        self.manager.append(self.workspace, transaction_id, offset=0, chunk=text)
        return transaction_id, payload

    def test_status_remains_responsive_while_commit_is_running(self) -> None:
        transaction_id, payload = self._stage("new.txt", "complete content")
        commit_entered = threading.Event()
        release_commit = threading.Event()

        def slow_commit(*args, **kwargs):
            commit_entered.set()
            release_commit.wait(timeout=0.5)

        with patch.object(text_writes, "_commit_staged_text", side_effect=slow_commit):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.manager.commit,
                    self.workspace,
                    transaction_id,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )
                self.assertTrue(commit_entered.wait(timeout=1))
                started = time.monotonic()
                status = self.manager.status(self.workspace, transaction_id)
                elapsed = time.monotonic() - started
                release_commit.set()
                future.result(timeout=1)
        self.assertLess(elapsed, 0.15)
        self.assertEqual(status["phase"], "committing")

    def test_abort_fails_fast_while_commit_is_running(self) -> None:
        transaction_id, payload = self._stage("new.txt", "complete content")
        commit_entered = threading.Event()
        release_commit = threading.Event()

        def slow_commit(*args, **kwargs):
            commit_entered.set()
            release_commit.wait(timeout=0.5)

        with patch.object(text_writes, "_commit_staged_text", side_effect=slow_commit):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.manager.commit,
                    self.workspace,
                    transaction_id,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )
                self.assertTrue(commit_entered.wait(timeout=1))
                started = time.monotonic()
                with self.assertRaises(ToolError) as raised:
                    self.manager.abort(self.workspace, transaction_id)
                elapsed = time.monotonic() - started
                release_commit.set()
                future.result(timeout=1)
        self.assertLess(elapsed, 0.15)
        self.assertEqual(raised.exception.code, "TRANSACTION_BUSY")

    def test_staging_permissions_are_private_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX mode bits are not authoritative on Windows")
        self.assertEqual(stat.S_IMODE(self.staging.stat().st_mode), 0o700)
        started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
        staging_file = next(self.staging.glob("*.part"))
        self.assertEqual(stat.S_IMODE(staging_file.stat().st_mode), 0o600)
        self.manager.abort(self.workspace, started["transaction_id"])

    def test_create_refuses_to_clobber_target_that_appears_after_begin(self) -> None:
        transaction_id, payload = self._stage("new.txt", "complete content")
        target = self.root / "new.txt"
        target.write_text("someone else", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "appeared"):
            self.manager.commit(
                self.workspace,
                transaction_id,
                expected_size=len(payload),
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "someone else")

    def test_replace_rechecks_target_sha_at_commit(self) -> None:
        target = self.root / "existing.txt"
        target.write_text("old", encoding="utf-8")
        baseline = hashlib.sha256(b"old").hexdigest()
        transaction_id, payload = self._stage("existing.txt", "new", mode="replace", baseline=baseline)
        target.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ToolError, "changed after begin"):
            self.manager.commit(
                self.workspace,
                transaction_id,
                expected_size=len(payload),
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "changed")

    def test_replace_successfully_commits_complete_text_and_preserves_baseline_lock(self) -> None:
        target = self.root / "existing.txt"
        target.write_text("old", encoding="utf-8")
        baseline = hashlib.sha256(b"old").hexdigest()
        transaction_id, payload = self._stage("existing.txt", "new content", mode="replace", baseline=baseline)
        committed = self.manager.commit(
            self.workspace,
            transaction_id,
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
        )
        self.assertFalse(committed["created"])
        self.assertEqual(target.read_bytes(), payload)
        with self.assertRaisesRegex(ToolError, "not found"):
            self.manager.status(self.workspace, transaction_id)

    def test_staging_mutation_after_precheck_is_detected_before_publish(self) -> None:
        transaction_id, payload = self._stage("new.txt", "complete content")
        staging_file = next(self.staging.glob("*.part"))
        original_hash = text_writes._hash_utf8_file
        first_staging_hash = True

        def hash_then_mutate(path: Path, limit: int):
            nonlocal first_staging_hash
            result = original_hash(path, limit)
            if path == staging_file and first_staging_hash:
                first_staging_hash = False
                staging_file.write_bytes(b"X" * len(payload))
            return result

        with patch.object(text_writes, "_hash_utf8_file", side_effect=hash_then_mutate):
            with self.assertRaisesRegex(ToolError, "staging|hash|SHA|changed"):
                self.manager.commit(
                    self.workspace,
                    transaction_id,
                    expected_size=len(payload),
                    expected_sha256=hashlib.sha256(payload).hexdigest(),
                )
        self.assertFalse((self.root / "new.txt").exists())

    def test_hash_mismatch_never_publishes_and_transaction_can_be_aborted(self) -> None:
        transaction_id, payload = self._stage("new.txt", "complete content")
        with self.assertRaisesRegex(ToolError, "does not match"):
            self.manager.commit(
                self.workspace,
                transaction_id,
                expected_size=len(payload),
                expected_sha256="0" * 64,
            )
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(self.manager.status(self.workspace, transaction_id)["received_bytes"], len(payload))
        self.manager.abort(self.workspace, transaction_id)
        self.assertFalse((self.root / "new.txt").exists())

    def test_hashes_are_case_insensitive_and_abort_removes_staging(self) -> None:
        target = self.root / "existing.txt"
        target.write_text("old", encoding="utf-8")
        baseline = hashlib.sha256(b"old").hexdigest().upper()
        started = self.manager.begin(
            self.workspace,
            "existing.txt",
            mode="replace",
            expected_target_sha256=baseline,
        )
        transaction_id = started["transaction_id"]
        self.manager.append(self.workspace, transaction_id, offset=0, chunk="new")
        status = self.manager.status(self.workspace, transaction_id)
        self.assertEqual(status["received_bytes"], 3)
        aborted = self.manager.abort(self.workspace, transaction_id)
        self.assertTrue(aborted["aborted"])
        self.assertEqual(list(self.staging.glob("*.part")), [])
        with self.assertRaisesRegex(ToolError, "not found"):
            self.manager.status(self.workspace, transaction_id)

    def test_invalid_utf8_surrogate_chunk_is_rejected_without_advancing(self) -> None:
        started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
        transaction_id = started["transaction_id"]
        with self.assertRaisesRegex(ToolError, "UTF-8") as raised:
            self.manager.append(self.workspace, transaction_id, offset=0, chunk="\ud800")
        self.assertEqual(raised.exception.code, "NOT_UTF8")
        self.assertEqual(self.manager.status(self.workspace, transaction_id)["received_bytes"], 0)

    def test_append_does_not_fsync_process_local_staging_chunks(self) -> None:
        with patch.object(text_writes.os, "fsync") as fsync:
            started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
            self.manager.append(self.workspace, started["transaction_id"], offset=0, chunk="payload")
        fsync.assert_not_called()

    def test_chunk_limit_and_offset_errors_do_not_advance_transaction(self) -> None:
        started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
        transaction_id = started["transaction_id"]
        with self.assertRaisesRegex(ToolError, "offset"):
            self.manager.append(self.workspace, transaction_id, offset=1, chunk="x")
        with self.assertRaisesRegex(ToolError, "chunk"):
            self.manager.append(
                self.workspace,
                transaction_id,
                offset=0,
                chunk="x" * (MAX_TRANSACTION_CHUNK_BYTES + 1),
            )
        self.assertEqual(self.manager.status(self.workspace, transaction_id)["received_bytes"], 0)

    def test_transaction_id_cannot_cross_workspace_boundary(self) -> None:
        started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
        other_root = Path(self.temporary.name) / "other"
        other_root.mkdir()
        other = Workspace(other_root)
        with self.assertRaisesRegex(ToolError, "different workspace") as raised:
            self.manager.status(other, started["transaction_id"])
        self.assertEqual(raised.exception.code, "TRANSACTION_WORKSPACE_MISMATCH")
        self.manager.abort(self.workspace, started["transaction_id"])

    def test_graceful_close_removes_active_staging(self) -> None:
        started = self.manager.begin(self.workspace, "new.txt", mode="create", expected_target_sha256=None)
        self.manager.append(self.workspace, started["transaction_id"], offset=0, chunk="payload")
        self.assertEqual(len(list(self.staging.glob("*.part"))), 1)
        self.manager.close()
        self.assertEqual(list(self.staging.glob("*.part")), [])
        with self.assertRaisesRegex(ToolError, "closed"):
            self.manager.status(self.workspace, started["transaction_id"])

    def test_stale_owned_staging_is_cleaned_on_manager_start(self) -> None:
        self.manager.close()
        self.staging.mkdir(parents=True, exist_ok=True)
        stale = self.staging / ("a" * 32 + ".part")
        stale.write_text("stale", encoding="utf-8")
        old = time.time() - TRANSACTION_TTL_SECONDS - 60
        os.utime(stale, (old, old))
        foreign = self.staging / "do-not-delete.part"
        foreign.write_text("keep", encoding="utf-8")
        self.manager = TextWriteManager(self.staging)
        self.assertFalse(stale.exists())
        self.assertTrue(foreign.exists())


if __name__ == "__main__":
    unittest.main()
