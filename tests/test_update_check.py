from __future__ import annotations

import json
import unittest
from contextlib import contextmanager

from folderbridge_mcp.update_check import (
    LATEST_RELEASES_URL,
    UpdateCheckResult,
    check_for_updates,
)


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


class UpdateCheckTests(unittest.TestCase):
    @staticmethod
    def opener(payload: object):
        @contextmanager
        def _open(_request, *, timeout):
            assert 0.5 <= timeout <= 15
            yield _Response(json.dumps(payload).encode("utf-8"))
        return _open

    def test_newer_latest_release_is_reported_with_stable_latest_link(self) -> None:
        result = check_for_updates("0.8.34", opener=self.opener({"tag_name": "v0.8.35"}))
        self.assertIsInstance(result, UpdateCheckResult)
        self.assertTrue(result.update_available)
        self.assertEqual(result.latest_version, "0.8.35")
        self.assertEqual(result.releases_url, LATEST_RELEASES_URL)
        self.assertEqual(LATEST_RELEASES_URL, "https://github.com/MoonTzai/folderbridge-mcp/releases/latest")

    def test_equal_or_older_release_is_current(self) -> None:
        equal = check_for_updates("0.8.34", opener=self.opener({"tag_name": "0.8.34"}))
        older = check_for_updates("0.8.34", opener=self.opener({"tag_name": "v0.8.33"}))
        self.assertEqual(equal.status, "current")
        self.assertEqual(older.status, "current")

    def test_network_and_invalid_payload_fail_non_fatally(self) -> None:
        def failing(_request, *, timeout):
            raise OSError("offline")

        offline = check_for_updates("0.8.34", opener=failing)
        self.assertEqual(offline.status, "unavailable")
        self.assertEqual(offline.detail, "OSError")

        @contextmanager
        def invalid(_request, *, timeout):
            yield _Response(b"not-json")

        malformed = check_for_updates("0.8.34", opener=invalid)
        self.assertEqual(malformed.status, "unavailable")
        self.assertEqual(malformed.detail, "response_invalid_json")

    def test_invalid_current_version_never_attempts_network(self) -> None:
        called = False

        def opener(_request, *, timeout):
            nonlocal called
            called = True
            raise AssertionError("must not be called")

        result = check_for_updates("dev", opener=opener)
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(called)


if __name__ == "__main__":
    unittest.main()
