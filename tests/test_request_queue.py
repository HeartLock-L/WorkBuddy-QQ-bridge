"""Offline scheduling and durable ownership regressions without a gateway."""

import asyncio
import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_test_support import PLUGIN

MODULE_PATH = PLUGIN.with_name("request_queue.py")
SPEC = importlib.util.spec_from_file_location("request_queue_under_test", MODULE_PATH)
QUEUE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = QUEUE
SPEC.loader.exec_module(QUEUE)


class RequestQueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "private" / "remote_run.json"
        self.instances = []
        self.tasks = []
        self.queue = self.make_queue()
        self.record = {
            "base": "http://127.0.0.1:12345",
            "request_id": "qq-request",
            "run_id": "remote-run",
            "started_at": time.time(),
        }

    def make_queue(self, **kwargs):
        queue = QUEUE.RequestQueue(self.path, **kwargs)
        self.instances.append(queue)
        return queue

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def wait_for_count(self, queue, count):
        for _ in range(100):
            if queue.waiting_count == count:
                return
            await asyncio.sleep(0)
        self.fail(f"Queue did not reach waiting count {count}")

    async def asyncTearDown(self):
        for queue in self.instances:
            queue.close()
            queue.release()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def test_construction_and_closed_admission_do_not_create_files(self):
        self.assertFalse(self.path.parent.exists())
        self.assertIsNone(self.queue.read_remote())
        self.queue.close()
        self.assertEqual(await self.queue.acquire(), "closed")
        self.assertFalse(self.path.parent.exists())

    async def test_humans_enter_in_fifo_order(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        order = []

        async def human(number):
            self.assertEqual(await self.queue.acquire(), "acquired")
            try:
                order.append(number)
                await asyncio.sleep(0)
            finally:
                self.queue.release()

        for number in range(4):
            self.spawn(human(number))
        await self.wait_for_count(self.queue, 4)
        self.queue.release()
        await asyncio.gather(*self.tasks)
        self.assertEqual(order, [0, 1, 2, 3])
        self.assertFalse(self.queue.busy)

    async def test_capacity_excludes_the_active_request(self):
        queue = self.make_queue(max_waiters=2)
        self.assertEqual(await queue.acquire(), "acquired")
        first = self.spawn(queue.acquire())
        second = self.spawn(queue.acquire())
        await self.wait_for_count(queue, 2)
        self.assertEqual(await queue.acquire(), "full")
        queue.release()
        self.assertEqual(await first, "acquired")
        self.assertEqual(queue.waiting_count, 1)
        queue.release()
        self.assertEqual(await second, "acquired")

    async def test_zero_waiters_allows_only_immediate_admission(self):
        queue = self.make_queue(max_waiters=0)
        self.assertEqual(await queue.acquire(), "acquired")
        self.assertEqual(await queue.acquire(), "full")
        self.assertEqual(await queue.acquire(background=True), "busy")
        queue.release()
        self.assertEqual(await queue.acquire(), "acquired")

    async def test_wait_timeout_removes_request_without_leaking_a_slot(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        started = time.monotonic()
        self.assertEqual(await self.queue.acquire(timeout=0.03), "expired")
        self.assertGreaterEqual(time.monotonic() - started, 0.02)
        self.assertEqual(self.queue.waiting_count, 0)
        self.queue.release()
        self.assertEqual(await self.queue.acquire(timeout=0), "acquired")

    async def test_cancelling_the_head_preserves_the_next_waiter(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        first = self.spawn(self.queue.acquire())
        second = self.spawn(self.queue.acquire())
        await self.wait_for_count(self.queue, 2)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.queue.waiting_count, 1)
        self.queue.release()
        self.assertEqual(await second, "acquired")

    async def test_expiring_head_does_not_reset_the_next_deadline(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        first = self.spawn(self.queue.acquire(timeout=0.02))
        second = self.spawn(self.queue.acquire(timeout=0.05))
        await self.wait_for_count(self.queue, 2)
        self.assertEqual(await first, "expired")
        self.assertEqual(await second, "expired")
        self.assertEqual(self.queue.waiting_count, 0)

    async def test_close_wakes_waiters_but_keeps_active_os_lock(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        waiting = self.spawn(self.queue.acquire())
        await self.wait_for_count(self.queue, 1)
        self.queue.close()
        self.assertEqual(await waiting, "closed")
        self.assertEqual(self.queue.waiting_count, 0)
        self.assertTrue(self.queue.busy)
        other = self.make_queue()
        self.assertEqual(await other.acquire(background=True), "busy")
        self.queue.release()
        self.assertEqual(await other.acquire(), "acquired")

    async def test_background_skips_active_or_waiting_humans(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        waiting = self.spawn(self.queue.acquire())
        await self.wait_for_count(self.queue, 1)
        self.assertEqual(await self.queue.acquire(background=True), "busy")
        self.assertEqual(self.queue.waiting_count, 1)
        self.queue.release()
        self.assertEqual(await self.queue.acquire(background=True), "busy")
        self.assertEqual(await waiting, "acquired")

    async def test_human_in_same_batch_precedes_background(self):
        background = self.spawn(self.queue.acquire(background=True))
        human = self.spawn(self.queue.acquire())
        self.assertEqual(await human, "acquired")
        self.assertEqual(await background, "busy")

    async def test_cross_instance_lock_wait_counts_toward_deadline(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        other = self.make_queue()
        self.assertEqual(await other.acquire(timeout=0.03), "expired")
        self.assertFalse(other.busy)
        waiting = self.spawn(other.acquire(timeout=1))
        await self.wait_for_count(other, 1)
        self.queue.release()
        self.assertEqual(await waiting, "acquired")

    async def test_os_lock_excludes_a_second_process(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        code = """
import asyncio, importlib.util, sys
spec = importlib.util.spec_from_file_location('queue_child', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
queue = module.RequestQueue(sys.argv[2])
print(asyncio.run(queue.acquire(timeout=0.03)))
queue.release()
"""
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-B", "-c", code, str(MODULE_PATH), str(self.path)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "expired")

    async def test_remote_state_persists_across_instances_without_age_expiry(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        record = {**self.record, "run_id": "", "started_at": 1.0}
        self.queue.save_remote(record)
        self.queue.release()
        other = self.make_queue()
        self.assertEqual(other.read_remote(), record)
        self.assertEqual(await other.acquire(), "acquired")
        self.assertEqual(other.read_remote(), record)
        other.clear_remote()
        other.clear_remote()
        self.assertIsNone(other.read_remote())

    async def test_saving_and_clearing_require_own_lock(self):
        for operation in (
            lambda: self.queue.save_remote(self.record),
            self.queue.clear_remote,
        ):
            with self.assertRaises(RuntimeError):
                operation()
        self.assertEqual(await self.queue.acquire(), "acquired")
        other = self.make_queue()
        with self.assertRaises(RuntimeError):
            other.save_remote(self.record)
        with self.assertRaises(RuntimeError):
            other.clear_remote()

    async def test_bad_remote_state_cannot_be_overwritten_silently(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        for broken in ("{broken", json.dumps({**self.record, "run_id": None}), "[]"):
            self.path.write_text(broken, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.queue.read_remote()
            with self.assertRaises(ValueError):
                self.queue.save_remote(self.record)
            self.assertEqual(self.path.read_text(encoding="utf-8"), broken)

    async def test_remote_schema_rejects_nonlocal_addresses_and_secrets(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        invalid = [
            {**self.record, "base": value}
            for value in (
                "https://127.0.0.1:12345",
                "http://example.com:12345",
                "http://user:pass@localhost:12345",
                "http://localhost:12345/path",
                "http://localhost:0",
                "http://localhost:65536",
            )
        ]
        invalid += [
            {**self.record, "token": "never-store-this"},
            {**self.record, "request_id": " "},
            {**self.record, "started_at": float("nan")},
            {**self.record, "started_at": True},
        ]
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(ValueError):
                self.queue.save_remote(record)
        self.assertFalse(self.path.exists())
        self.queue.save_remote({**self.record, "base": "http://localhost:12345"})

    async def test_failed_atomic_write_preserves_previous_remote_evidence(self):
        self.assertEqual(await self.queue.acquire(), "acquired")
        self.queue.save_remote(self.record)
        with (
            patch.object(QUEUE.os, "replace", side_effect=OSError("disk unavailable")),
            self.assertRaises(OSError),
        ):
            self.queue.save_remote({**self.record, "run_id": "new-id"})
        self.assertEqual(self.queue.read_remote(), self.record)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
