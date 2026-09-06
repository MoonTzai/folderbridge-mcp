from __future__ import annotations

import threading
import time
import unittest


class V35TransportNeutralSchedulerAcceptanceTests(unittest.TestCase):
    def test_data_lane_is_fail_fast_bounded_independent_of_transport(self) -> None:
        from folderbridge_mcp.mcp import McpRequestScheduler

        entered = threading.Event()
        release = threading.Event()

        def dispatch(request):
            entered.set()
            release.wait(5)
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"ok": True}}

        scheduler = McpRequestScheduler(
            dispatch,
            control_workers=1,
            control_max_inflight=1,
            data_workers=1,
            data_max_inflight=1,
        )
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace", "arguments": {"action": "list"}},
        }
        first_result: list[dict | None] = []
        thread = threading.Thread(
            target=lambda: first_result.append(scheduler.dispatch_sync(request)),
            daemon=True,
        )
        thread.start()
        self.assertTrue(entered.wait(2))
        started = time.monotonic()
        busy = scheduler.dispatch_sync({**request, "id": 2})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5)
        self.assertEqual(busy["error"]["code"], -32001)
        self.assertEqual(busy["error"]["message"], "Server busy")

        release.set()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result[0]["result"]["ok"], True)
        scheduler.close()

    def test_control_lane_remains_available_while_data_lane_is_saturated(self) -> None:
        from folderbridge_mcp.mcp import McpRequestScheduler

        entered = threading.Event()
        release = threading.Event()

        def dispatch(request):
            if request.get("method") == "tools/call":
                entered.set()
                release.wait(5)
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

        scheduler = McpRequestScheduler(
            dispatch,
            control_workers=1,
            control_max_inflight=1,
            data_workers=1,
            data_max_inflight=1,
        )
        data_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "workspace", "arguments": {"action": "list"}},
        }
        worker = threading.Thread(target=lambda: scheduler.dispatch_sync(data_request), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(2))

        response = scheduler.dispatch_sync(
            {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}
        )
        self.assertEqual(response, {"jsonrpc": "2.0", "id": 2, "result": {}})

        release.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        scheduler.close()

    def test_scheduler_close_rejects_new_work_without_owning_runtime_lifecycle(self) -> None:
        from folderbridge_mcp.mcp import McpRequestScheduler

        calls: list[int] = []
        scheduler = McpRequestScheduler(
            lambda request: calls.append(request["id"]) or {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {},
            },
            control_workers=1,
            control_max_inflight=1,
            data_workers=1,
            data_max_inflight=1,
        )
        scheduler.close()
        response = scheduler.dispatch_sync(
            {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}}
        )
        self.assertEqual(response["error"]["code"], -32001)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
