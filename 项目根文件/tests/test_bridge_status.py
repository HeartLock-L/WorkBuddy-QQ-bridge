"""Verify private runtime evidence using fake services and temporary files."""

import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module


def load_status_code(root):
    """Compile only owned runtime methods with an isolated output location."""
    selected = {
        "_write_runtime_status",
        "_runtime_status_loop",
        "initialize",
        "terminate",
        "_spawn_background",
    }
    namespace = {
        "__file__": str(root / "main.py"),
        "Path": Path,
        "os": os,
        "json": json,
        "re": re,
        "tempfile": tempfile,
        "threading": threading,
        "time": time,
        "uuid": uuid,
        "asyncio": asyncio,
        "logger": Mock(),
        "runtime_status": load_module(
            PLUGIN.with_name("runtime_status.py"), "runtime_status_under_test"
        ),
        "CODE_SHA256": "a" * 64,
        "SPARK_FAIL_CIRCUIT_N": 3,
        "EMPTY_REPLY_MARKERS": {"[超时]", "[静默]", "[状态待确认]"},
        "_GW_CACHE": {
            "base": "http://127.0.0.1:23456",
            "state": "connected",
            "checked_at": time.time(),
            "token": "must-not-appear",
        },
        "_RUNTIME": {},
        "MAX_REPLY_CHARS": 1500,
        "OTHERS_MAX_CHARS": 1500,
        "HOT_RELOAD_ENABLED": False,
        "IDLE_CHAT_ENABLED": False,
        "RUNTIME_STATUS_INTERVAL_SEC": 0.01,
        "_is_paused": lambda: False,
    }
    load_definitions(namespace, methods=selected)
    bridge = namespace["Bridge"]()
    bridge._instance_id = "offline-instance"
    bridge._stopped = False
    bridge._runtime_started_at = time.time() - 3600
    bridge._status_task = None
    bridge._memory_task = None
    bridge._memory = types.SimpleNamespace(loop=AsyncMock(), close=AsyncMock())
    bridge._background_tasks = set()
    bridge._question_operations = set()
    bridge._questions = types.SimpleNamespace(
        close=Mock(),
        read_remote=Mock(return_value=None),
        waiting_count=2,
        max_waiters=5,
    )
    bridge._question_last_outcome = "reply"
    bridge._question_last_failure = "[超时]"
    bridge._question_last_failure_at = time.time() - 20
    bridge._spark_fail_streak = 3
    bridge._cleanup_stop = threading.Event()
    bridge._idle_task = bridge._hot_task = bridge._maintenance_task = None
    bridge._draw_recovery_task = None
    bridge._draw_recovery_loop = AsyncMock()
    bridge._maintenance_loop = AsyncMock()
    bridge._cfg_int = lambda _key, default: default
    bridge._cfg_bool = lambda _key, _default: False
    bridge._pending = {"private-group": [{"text": "private-chat-text"}]}
    return bridge, namespace


class BridgeStatusFileTests(unittest.TestCase):
    """Check snapshot content and replacement without touching live evidence."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bridge, self.namespace = load_status_code(self.root)
        self.path = self.root / "bridge_status.json"

    def read_status(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_manual_memory_status_roundtrips_through_diagnostic_reader(self):
        reader = load_module(
            Path(__file__).resolve().parents[1] / "scripts/diagnostic_evidence.py",
            "manual_memory_diagnostic_reader",
        )
        expected = {
            "manual_state": "waiting",
            "manual_requested_at": time.time() - 2,
            "manual_finished_at": 0.0,
            "manual_batches": 3,
            "manual_saved": 1,
            "last_outcome": "pending_confirmation",
            "last_reasons": {},
        }
        self.bridge._memory.summary = Mock(return_value=expected)
        self.bridge._write_runtime_status()
        self.assertEqual(reader.read_runtime_status(self.path)["memory"], expected)
        for outcome in ("not_idle", "idle_unverified"):
            expected["last_outcome"] = outcome
            self.bridge._write_runtime_status()
            self.assertEqual(reader.read_runtime_status(self.path)["memory"], expected)

    def test_invalid_manual_memory_fields_are_filtered_before_publication(self):
        self.bridge._memory.summary = Mock(
            return_value={
                "manual_state": "private-chat-text",
                "manual_requested_at": float("nan"),
                "manual_finished_at": -1,
                "manual_batches": True,
                "manual_saved": "secret",
                "manual_source": "private-user-id",
            }
        )
        self.bridge._write_runtime_status()
        self.assertEqual(self.read_status()["memory"], {})
        self.assertNotIn("private", self.path.read_text())

    def test_snapshot_contains_only_operational_evidence(self):
        self.bridge._question_operations.add(object())
        self.bridge._write_runtime_status()
        status = self.read_status()
        self.assertEqual(status["version"], 1)
        self.assertEqual(status["instance"], "offline-instance")
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["source_sha256"], "a" * 64)
        self.assertEqual(status["state"], "running")
        self.assertLess(status["started_at"], status["updated_at"])
        self.assertEqual(status["question"]["waiting"], 2)
        self.assertEqual(status["question"]["limit"], 5)
        self.assertTrue(status["question"]["active"])
        self.assertEqual(status["question"]["last_failure"], "[超时]")
        self.assertTrue(status["proactive"]["paused"])
        self.assertEqual(status["proactive"]["failures"], 3)
        raw = self.path.read_text(encoding="utf-8")
        for secret in ("must-not-appear", "private-chat-text", "private-group"):
            self.assertNotIn(secret, raw)
        self.assertEqual(
            set(status),
            {
                "version",
                "instance",
                "pid",
                "source_sha256",
                "started_at",
                "updated_at",
                "state",
                "question",
                "proactive",
                "gateway",
            },
        )

    def test_remote_record_reasons_are_distinct(self):
        record = {
            "base": "http://127.0.0.1:23456",
            "request_id": "private-request",
            "run_id": "private-run",
            "started_at": time.time(),
        }
        for value, reason in (
            (None, "none"),
            (record, "recorded"),
            ({**record, "run_id": ""}, "submission_unknown"),
            ({**record, "base": "http://127.0.0.1:34567"}, "gateway_changed"),
        ):
            with self.subTest(reason=reason):
                self.bridge._questions.read_remote.return_value = value
                self.bridge._write_runtime_status()
                self.assertEqual(self.read_status()["question"]["remote_state"], reason)
                self.assertNotIn("private-run", self.path.read_text(encoding="utf-8"))

    def test_memory_summary_is_wired_and_only_fixed_fields_are_published(self):
        expected = {
            "last_attempt_at": time.time(),
            "last_success_at": 0.0,
            "attempts": 3,
            "saved_total": 0,
            "last_selected": 2,
            "last_saved": 0,
            "last_duration_ms": 14,
            "last_outcome": "filtered",
            "last_reasons": {"question": 1, "reported": 1},
        }
        self.bridge._memory.summary = Mock(
            return_value={
                **expected,
                "text": "private-memory-text",
                "source": "private-memory-source",
                "group_id": "private-memory-group",
                "proof": "private-memory-proof",
                "last_reasons": {**expected["last_reasons"], "private-reason": 1},
            }
        )
        self.bridge._write_runtime_status()
        self.bridge._memory.summary.assert_called_once_with()
        self.assertEqual(self.read_status()["memory"], expected)
        self.assertNotIn("private-", self.path.read_text(encoding="utf-8"))

    def test_unavailable_memory_summary_preserves_legacy_snapshot(self):
        for memory in (
            None,
            types.SimpleNamespace(summary=None),
            types.SimpleNamespace(summary={"text": "private-text"}),
        ):
            with self.subTest(memory=memory):
                self.bridge._memory = memory
                self.bridge._write_runtime_status()
                self.assertNotIn("memory", self.read_status())
        del self.bridge._memory
        self.bridge._write_runtime_status()
        self.assertNotIn("memory", self.read_status())

    def test_invalid_memory_values_do_not_prevent_heartbeat_or_leak_text(self):
        for value in (
            False,
            True,
            -1,
            float("nan"),
            float("inf"),
            10**400,
            "private-value",
            [],
            {},
        ):
            with self.subTest(value=value):
                self.bridge._memory.summary = Mock(
                    return_value={
                        "last_attempt_at": value,
                        "last_success_at": value,
                        "attempts": value,
                        "saved_total": value,
                        "last_selected": value,
                        "last_saved": value,
                        "last_duration_ms": value,
                        "last_outcome": value,
                        "last_reasons": {"question": value, "private-reason": 1},
                    }
                )
                self.bridge._write_runtime_status()
                self.assertEqual(self.read_status()["memory"], {"last_reasons": {}})
                self.assertNotIn("private-", self.path.read_text(encoding="utf-8"))

    def test_fractional_counts_and_unknown_outcomes_are_omitted(self):
        self.bridge._memory.summary = Mock(
            return_value={
                "last_attempt_at": 1.5,
                "last_success_at": 0,
                "attempts": 1.5,
                "last_outcome": "private-reply-body",
                "last_reasons": {"question": 1.5, "reported": 0},
            }
        )
        self.bridge._write_runtime_status()
        self.assertEqual(
            self.read_status()["memory"],
            {
                "last_attempt_at": 1.5,
                "last_success_at": 0,
                "last_reasons": {"reported": 0},
            },
        )

    def test_malformed_memory_summary_is_not_published(self):
        for value in (None, "private-summary", ["private-summary"], False):
            with self.subTest(value=value):
                self.bridge._memory.summary = Mock(return_value=value)
                self.bridge._write_runtime_status()
                self.assertNotIn("memory", self.read_status())
                self.assertNotIn("private-", self.path.read_text(encoding="utf-8"))

    def test_unreadable_remote_record_does_not_prevent_heartbeat(self):
        self.bridge._questions.read_remote.side_effect = ValueError("private-secret")
        self.bridge._write_runtime_status()
        self.assertEqual(self.read_status()["question"]["remote_state"], "unreadable")
        self.assertNotIn("private-secret", self.path.read_text(encoding="utf-8"))

    def test_failed_atomic_replace_preserves_previous_snapshot(self):
        self.bridge._write_runtime_status()
        before = self.path.read_bytes()
        with patch.object(
            self.namespace["runtime_status"].os,
            "replace",
            side_effect=OSError("fixture"),
        ):
            try:
                self.bridge._write_runtime_status("stopped")
            except OSError:
                pass
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["bridge_status.json"])

    def test_stopped_status_cannot_look_like_running(self):
        self.bridge._stopped = True
        self.bridge._write_runtime_status("stopped")
        self.assertEqual(self.read_status()["state"], "stopped")

    def test_invalid_state_preserves_the_previous_snapshot(self):
        self.bridge._write_runtime_status()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.bridge._write_runtime_status("unrecognized")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["bridge_status.json"])

    def test_invalid_numeric_value_removes_temporary_file_without_replacement(self):
        self.bridge._write_runtime_status()
        before = self.path.read_bytes()
        self.bridge._question_last_failure_at = float("nan")
        with self.assertRaises(ValueError):
            self.bridge._write_runtime_status()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.iterdir()], ["bridge_status.json"])

    def test_unrecognized_outcomes_and_nonlocal_addresses_are_not_published(self):
        self.bridge._question_last_outcome = "private-reply-body"
        self.bridge._question_last_failure = "private-failure-details"
        self.namespace["_GW_CACHE"].update(
            state="private-state", base="https://outside.invalid/private-token"
        )
        self.bridge._write_runtime_status()
        status = self.read_status()
        self.assertEqual(status["question"]["last_outcome"], "")
        self.assertEqual(status["question"]["last_failure"], "")
        self.assertEqual(status["gateway"]["state"], "unknown")
        self.assertIsNone(status["gateway"]["base"])
        self.assertNotIn("private-", self.path.read_text(encoding="utf-8"))

    def test_generated_heartbeat_is_accepted_by_the_diagnostic_reader(self):
        source = self.root / "main.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        evidence_path = PLUGIN.parents[4] / "scripts/diagnostic_evidence.py"
        name = "bridge_status_contract_evidence"
        spec = importlib.util.spec_from_file_location(name, evidence_path)
        evidence = importlib.util.module_from_spec(spec)
        sys.modules[name] = evidence
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(evidence)
        self.namespace["CODE_SHA256"] = evidence.source_fingerprint(source)
        self.bridge._runtime_started_at = time.time() - 3600
        self.bridge._write_runtime_status()
        log = self.root / "unused.log"
        live = evidence.inspect_runtime(source, log, require_heartbeat=True)
        version = evidence.inspect_runtime(source, log, version_only=True)
        self.assertIs(live.state, True, live.note)
        self.assertIs(version.state, True, version.note)
        self.bridge._write_runtime_status("stopped")
        stopped = evidence.inspect_runtime(source, log, require_heartbeat=True)
        self.assertIs(stopped.state, False, stopped.note)


class BridgeStatusLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Verify initialization and shutdown own the heartbeat lifecycle."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bridge, self.namespace = load_status_code(self.root)
        self.path = self.root / "bridge_status.json"

    async def asyncTearDown(self):
        await asyncio.wait_for(self.bridge.terminate(), 2)

    async def test_initialization_writes_status_with_idle_chat_disabled(self):
        await self.bridge.initialize()
        self.assertEqual(json.loads(self.path.read_text())["state"], "running")
        self.assertIsNone(self.bridge._idle_task)
        self.assertIsNotNone(self.bridge._status_task)

    async def test_repeated_initialize_does_not_duplicate_heartbeat(self):
        await self.bridge.initialize()
        first = self.bridge._status_task
        await self.bridge.initialize()
        self.assertIs(first, self.bridge._status_task)

    async def test_heartbeat_refreshes_without_any_chat_messages(self):
        refreshed = asyncio.Event()
        writes = 0
        original = self.bridge._write_runtime_status

        def save(state="running"):
            nonlocal writes
            original(state)
            writes += 1
            if writes >= 2:
                loop.call_soon_threadsafe(refreshed.set)

        loop = asyncio.get_running_loop()
        self.bridge._write_runtime_status = save
        await self.bridge.initialize()
        await asyncio.wait_for(refreshed.wait(), 1)
        self.assertGreaterEqual(writes, 2)
        self.assertEqual(json.loads(self.path.read_text())["state"], "running")

    async def test_heartbeat_recovers_from_a_failed_status_write(self):
        recovered = asyncio.Event()
        calls = 0
        original = self.bridge._write_runtime_status
        loop = asyncio.get_running_loop()

        def save(state="running"):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary status destination error")
            original(state)
            loop.call_soon_threadsafe(recovered.set)

        self.bridge._write_runtime_status = save
        self.bridge._status_task = self.bridge._spawn_background(
            self.bridge._runtime_status_loop()
        )
        await asyncio.wait_for(recovered.wait(), 1)
        self.assertGreaterEqual(calls, 2)
        self.assertEqual(json.loads(self.path.read_text())["state"], "running")

    async def test_terminate_records_stopped_after_owned_tasks_exit(self):
        await self.bridge.initialize()
        heartbeat = self.bridge._status_task
        observed = []

        async def worker():
            try:
                await asyncio.Event().wait()
            finally:
                observed.append(json.loads(self.path.read_text())["state"])

        self.bridge._spawn_background(worker())
        await asyncio.sleep(0)
        await self.bridge.terminate()
        self.assertEqual(observed, ["running"])
        self.assertTrue(heartbeat.done())
        self.assertEqual(self.bridge._background_tasks, set())
        self.assertEqual(json.loads(self.path.read_text())["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
