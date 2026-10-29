"""Exercise local manual-memory controls with temporary files and no model access."""

import concurrent.futures
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import patch

from bridge_test_support import PLUGIN, ROOT, load_module
from diagnostic_test_support import OfflineCase
from diagnostic_evidence import (
    MEMORY_MANUAL_STATES,
    MEMORY_OUTCOMES,
    source_fingerprint,
    source_paths,
)

control = load_module(ROOT / "scripts/memory-control.py", "memory_control_under_test")
runtime = load_module(PLUGIN.with_name("runtime_status.py"), "memory_control_runtime")


class MemoryControlTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.plugin = self.root / "AstrBot/data/plugins/workbuddy_bridge"
        self.plugin.mkdir(parents=True)
        for path in source_paths(self.plugin / "main.py"):
            path.write_text("# Offline source fixture.\n", encoding="utf-8")
        self.status = self.plugin / "bridge_status.json"
        self.request = self.plugin / "memory_control.json"
        patch.object(control, "ROOT", self.root).start()
        self.write_status()

    def test_display_labels_cover_all_valid_diagnostic_states(self):
        self.assertEqual(set(control.MANUAL_LABELS), MEMORY_MANUAL_STATES)
        self.assertEqual(set(control.OUTCOME_LABELS), MEMORY_OUTCOMES)

    def write_status(self, **changes):
        options = {
            "state": "running",
            "instance": "offline",
            "source_sha256": source_fingerprint(self.plugin / "main.py"),
            "started_at": time.time() - 1000,
            "question_active": False,
            "question_waiting": 0,
            "question_limit": 5,
            "read_remote": lambda: None,
            "last_outcome": "reply",
            "last_failure": "",
            "last_failure_at": 0.0,
            "proactive_failures": 0,
            "proactive_failure_limit": 3,
            "gateway_base": None,
            "gateway_state": "unknown",
            "gateway_checked_at": 0.0,
            "empty_reply_markers": set(),
            "memory_status": {
                "manual_state": "idle",
                "manual_requested_at": 0.0,
                "manual_finished_at": 0.0,
                "manual_batches": 0,
                "manual_saved": 0,
                "last_outcome": "waiting",
            },
        }
        options.update(changes)
        runtime.write_snapshot(self.status, **options)

    def test_start_publishes_one_complete_strict_request(self):
        original = os.link

        def check_atomic(source, target):
            self.assertFalse(target.exists())
            data = json.loads(Path(source).read_text(encoding="utf-8"))
            self.assertEqual(
                set(data), {"version", "action", "request_id", "requested_at"}
            )
            self.assertEqual(data["action"], "start")
            return original(source, target)

        with patch.object(control.os, "link", side_effect=check_atomic):
            self.assertEqual(control.main(["--start"]), 0)
        data = json.loads(self.request.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertRegex(data["request_id"], r"^[0-9a-f]{32}$")
        self.assertLess(abs(data["requested_at"] - time.time()), 3)
        self.assertEqual(list(self.plugin.glob(".memory-control-*.tmp")), [])
        self.assertIn("等待插件确认", self.output.getvalue())
        self.assertNotIn("已开始", self.output.getvalue())

    def test_stop_can_be_requested_during_an_active_task(self):
        self.write_status(question_active=True, question_waiting=2)
        self.assertEqual(control.main(["--stop"]), 0)
        self.assertEqual(json.loads(self.request.read_text())["action"], "stop")
        self.assertIn("当前批次结束后停止", self.output.getvalue())

    def test_existing_request_is_never_overwritten(self):
        original = b'{"owner":"other-request"}'
        self.request.write_bytes(original)
        self.assertEqual(control.main(["--start"]), 1)
        self.assertEqual(self.request.read_bytes(), original)
        self.assertEqual(list(self.plugin.glob(".memory-control-*.tmp")), [])

    def test_parallel_publish_has_only_one_winner(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: control.main(["--start"]), range(2)))
        self.assertEqual(sorted(results), [0, 1])
        self.assertTrue(
            re.fullmatch(
                r"[0-9a-f]{32}", json.loads(self.request.read_text())["request_id"]
            )
        )
        self.assertEqual(list(self.plugin.glob(".memory-control-*.tmp")), [])

    def test_publication_failure_leaves_no_partial_request(self):
        for operation in ("link", "fsync"):
            with self.subTest(operation=operation):
                with patch.object(control.os, operation, side_effect=PermissionError):
                    self.assertEqual(control.main(["--start"]), 1)
                self.assertFalse(self.request.exists())
                self.assertEqual(list(self.plugin.glob(".memory-control-*.tmp")), [])

    def test_stale_future_stopped_or_missing_runtime_cannot_submit(self):
        for case in ("stale", "future", "stopped", "missing"):
            with self.subTest(case=case):
                self.write_status()
                data = json.loads(self.status.read_text())
                if case == "stale":
                    data["updated_at"] = time.time() - 91
                elif case == "future":
                    data["updated_at"] = time.time() + 10
                elif case == "stopped":
                    data["state"] = "stopped"
                self.status.write_text(json.dumps(data), encoding="utf-8")
                if case == "missing":
                    self.status.unlink()
                self.assertEqual(control.main(["--start"]), 1)
                self.assertFalse(self.request.exists())

    def test_changed_source_cannot_be_claimed_as_loaded(self):
        (self.plugin / "main.py").write_text("# Different source\n")
        self.assertEqual(control.main(["--start"]), 1)
        self.assertFalse(self.request.exists())
        self.assertIn("版本", self.output.getvalue())

    def test_legacy_snapshot_is_readable_but_not_a_manual_control_target(self):
        self.write_status(memory_status={"last_outcome": "saved"})
        self.assertEqual(control.main(["--start"]), 1)
        self.assertFalse(self.request.exists())

    def test_paused_plugin_rejects_start_but_still_accepts_stop(self):
        (self.root / "bridge.paused").write_text("operator pause")
        self.assertEqual(control.main(["--start"]), 1)
        self.assertFalse(self.request.exists())
        self.assertEqual(control.main(["--stop"]), 0)
        self.assertEqual(json.loads(self.request.read_text())["action"], "stop")

    def test_default_status_and_explicit_status_are_read_only(self):
        before = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(control.main([]), 0)
        self.assertEqual(control.main(["--status"]), 0)
        after = {
            p.relative_to(self.root): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)
        self.assertIn("未开启", self.output.getvalue())

    def test_unknown_saved_count_is_not_presented_as_zero(self):
        self.write_status(
            memory_status={"manual_state": "stopped", "last_outcome": "cancelled"}
        )
        self.assertEqual(control.main([]), 0)
        self.assertIn("尚未确认", self.output.getvalue())

    def test_conflicting_and_unknown_arguments_are_rejected(self):
        for arguments in (["--start", "--stop"], ["--root", str(self.root)]):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as raised:
                    control.main(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertFalse(self.request.exists())
