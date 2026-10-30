"""Offline diagnostic regressions: synthetic files and mocked transports only."""

from __future__ import annotations

import contextlib
import hashlib
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from diagnostic_test_support import (
    Evidence,
    OfflineCase,
    doctor,
    inspect_runtime,
    reconnect,
    reload_tool,
    source_fingerprint,
)
from diagnostic_evidence import _validate_memory_status


class RuntimeEvidenceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "main.py"
        self.source.write_bytes(b"VALUE = 1\n")
        self.log = self.root / "runtime.log"
        self.now = time.time()
        self.digest = source_fingerprint(self.source)

    def test_manual_memory_schema_accepts_legacy_and_fixed_new_fields(self):
        for value in (
            {},
            {"last_outcome": "saved"},
            {
                "manual_state": "running",
                "manual_requested_at": 1.0,
                "manual_finished_at": 0,
                "manual_batches": 5,
                "manual_saved": 2,
                "last_outcome": "pending_confirmation",
            },
        ):
            _validate_memory_status(value)

    def test_manual_memory_schema_rejects_private_text_and_invalid_values(self):
        for value in (
            {"manual_state": "private-data"},
            {"manual_state": []},
            {"last_outcome": []},
            {"manual_batches": True},
            {"manual_saved": -1},
            {"manual_saved": 2**53},
            {"manual_requested_at": float("inf")},
            {"manual_requested_at": 10**1000},
            {"manual_finished_at": 253402300800},
            {"manual_source": "private-data"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    _validate_memory_status(value)

    def write_marker(self, digest=None, instance="new", started=None, suffix=""):
        self.log.write_text(
            f"[12:00:00] [Core] [INFO] [workbuddy_bridge.main:123]: workbuddy_bridge: "
            f"[runtime] sha256={digest or self.digest} instance={instance} "
            f"started_at={self.now if started is None else started}\n" + suffix,
            encoding="utf-8",
        )

    def inspect(self, **kwargs):
        return inspect_runtime(self.source, self.log, now=self.now, **kwargs).state

    def test_missing_or_historical_line_number_is_unknown(self):
        self.assertIsNone(self.inspect())
        self.log.write_text(
            "Loading plugin workbuddy_bridge ...\n[workbuddy_bridge.main:123]: [回复]\n"
        )
        self.assertIsNone(self.inspect())

    def test_recent_exact_hash_is_confirmed(self):
        self.write_marker()
        self.assertIs(self.inspect(), True)

    def test_changed_content_at_same_line_count_is_not_accepted(self):
        self.write_marker()
        self.source.write_bytes(b"VALUE = 2\n")
        self.assertIs(self.inspect(), False)

    def test_fingerprint_includes_file_names_and_explicit_boundaries(self):
        transport = self.source.with_name("jimeng.py")
        self.source.write_bytes(b"AB")
        transport.write_bytes(b"C")
        expected = hashlib.sha256()
        for name, content in ((b"main.py", b"AB"), (b"jimeng.py", b"C")):
            expected.update(name + b"\0" + content + b"\0")
        first = source_fingerprint(self.source)
        self.assertEqual(first, expected.hexdigest())
        renamed = self.source.with_name("renamed.py")
        renamed.write_bytes(b"AB")
        self.assertNotEqual(first, source_fingerprint(renamed))
        # The raw concatenation remains ABC, but the source boundary changed.
        self.source.write_bytes(b"A")
        transport.write_bytes(b"BC")
        self.assertNotEqual(first, source_fingerprint(self.source))

    def test_image_module_change_invalidates_loaded_fingerprint(self):
        image_module = self.source.with_name("jimeng.py")
        image_module.write_bytes(b"VALUE = 1\n")
        self.digest = source_fingerprint(self.source)
        self.write_marker()
        self.assertIs(self.inspect(), True)
        image_module.write_bytes(b"VALUE = 2\n")
        self.assertIs(self.inspect(), False)

    def test_job_module_change_invalidates_loaded_fingerprint(self):
        job_module = self.source.with_name("image_jobs.py")
        job_module.write_bytes(b"VALUE = 1\n")
        self.digest = source_fingerprint(self.source)
        self.write_marker()
        self.assertIs(self.inspect(), True)
        job_module.write_bytes(b"VALUE = 2\n")
        self.assertIs(self.inspect(), False)

    def test_queue_module_change_invalidates_loaded_fingerprint(self):
        queue_module = self.source.with_name("request_queue.py")
        queue_module.write_bytes(b"VALUE = 1\n")
        self.digest = source_fingerprint(self.source)
        self.write_marker()
        self.assertIs(self.inspect(), True)
        queue_module.write_bytes(b"VALUE = 2\n")
        self.assertIs(self.inspect(), False)

    def test_split_modules_and_inventory_participate_in_version_evidence(self):
        for name in ("prompts.py", "reply_text.py", "source_files.py"):
            with self.subTest(source=name):
                for path in doctor.source_paths(self.source):
                    path.write_bytes(b"VALUE = 1\n")
                self.digest = source_fingerprint(self.source)
                self.write_marker()
                self.assertIs(self.inspect(), True)
                self.source.with_name(name).write_bytes(b"VALUE = 2\n")
                self.assertIs(self.inspect(), False)

    def test_missing_split_module_is_unknown_instead_of_partial_match(self):
        for path in doctor.source_paths(self.source):
            path.write_bytes(b"VALUE = 1\n")
        self.digest = source_fingerprint(self.source)
        self.write_marker()
        self.source.with_name("reply_text.py").unlink()
        self.assertIsNone(self.inspect())

    def test_unavailable_or_broken_inventory_does_not_crash_diagnostics(self):
        evidence_module = sys.modules[source_fingerprint.__module__]
        broken = self.root / "broken_inventory.py"
        for contents in (None, b"def invalid(:\n"):
            with self.subTest(contents=contents):
                if contents is not None:
                    broken.write_bytes(contents)
                with patch.object(evidence_module, "SOURCE_INVENTORY", broken):
                    self.assertIsNone(self.inspect())
                    doctor.results.clear()
                    doctor.check_compile()
                    self.assertIs(
                        doctor.results[-1][1], None if contents is None else False
                    )

    def test_stale_startup_unknown_even_if_log_recently_touched(self):
        self.write_marker(started=self.now - 3600)
        self.assertIsNone(self.inspect())

    def test_reload_needs_new_time_and_instance(self):
        self.write_marker(started=self.now - 1)
        self.assertIsNone(self.inspect(after_time=self.now))
        self.write_marker()
        self.assertIsNone(self.inspect(excluded_instances={"new"}))
        self.assertIs(
            self.inspect(after_time=self.now - 1, excluded_instances={"old"}), True
        )

    def test_later_failed_or_incomplete_initialization_invalidates_prior_marker(self):
        self.write_marker(suffix="Failed to import plugin workbuddy_bridge: broken\n")
        self.assertIs(self.inspect(), False)
        self.write_marker(suffix="Loading plugin workbuddy_bridge ...\n")
        self.assertIsNone(self.inspect())

    def test_unknown_hash_and_future_timestamp_are_unknown(self):
        self.write_marker(digest="unknown")
        self.assertIsNone(self.inspect())
        self.write_marker(started=self.now + 50)
        self.assertIsNone(self.inspect())

    def test_runtime_marker_inside_received_chat_is_not_initialization_evidence(self):
        self.log.write_text(
            f"[workbuddy_bridge.main:999]: workbuddy_bridge: [收到] 内容='"
            f"[runtime] sha256={self.digest} instance=fake started_at={self.now}'\n",
        )
        self.assertIsNone(self.inspect())


class ReloadTests(OfflineCase):
    def invoke(self, evidence, http_status=200, body='{"status":"ok"}'):
        with (
            patch.object(
                reload_tool,
                "_load_jwt_secret",
                return_value=("fake-user", "fake-secret"),
            ),
            patch.object(reload_tool, "_sign_token", return_value="fake-token"),
            patch.object(reload_tool, "runtime_instances", return_value={"old"}),
            patch.object(reload_tool, "_post", return_value=(http_status, body)),
            patch.object(reload_tool, "_probe_code_version", return_value=evidence),
            patch.object(sys, "argv", ["reload"]),
        ):
            return reload_tool.main()

    def test_api_success_without_version_evidence_is_unknown(self):
        self.assertEqual(self.invoke(Evidence(None, "not observed")), 2)
        self.assertNotIn("[VERIFIED]", self.output.getvalue())

    def test_old_code_failure_propagates(self):
        self.assertEqual(self.invoke(Evidence(False, "different hash")), 1)

    def test_new_confirmed_instance_is_success(self):
        self.assertEqual(self.invoke(Evidence(True, "new hash")), 0)

    def test_http_error_cannot_be_hidden_by_ok_json(self):
        self.assertEqual(self.invoke(Evidence(True, "new hash"), http_status=500), 1)

    def test_unexpected_json_shape_is_failure(self):
        self.assertEqual(self.invoke(Evidence(True, "new hash"), body="[]"), 1)


class ReconnectTests(OfflineCase):
    def invoke(self, child, *, missing_reload=False, changed_readback=False):
        env_path = self.root / "gateway.env"
        env_path.write_text(
            "CODEBUDDY_GATEWAY_PORT=12345\nCODEBUDDY_GATEWAY_PASSWORD=fake\n"
        )
        if not missing_reload:
            (self.root / "scripts").mkdir(exist_ok=True)
            (self.root / "scripts" / "reload-plugin.py").write_text("# synthetic child")
        with (
            patch.object(reconnect, "HERE", self.root),
            patch.object(reconnect, "ENV_FILE", env_path),
            patch.object(reconnect, "scan_ports", return_value=[]),
            patch.object(reconnect, "try_auth", return_value=True),
            patch.object(reconnect.subprocess, "run", return_value=child),
            patch.dict(os.environ, {}, clear=True),
        ):
            if changed_readback:
                with patch.object(
                    reconnect,
                    "load_env",
                    side_effect=[
                        {
                            "CODEBUDDY_GATEWAY_PORT": "12345",
                            "CODEBUDDY_GATEWAY_PASSWORD": "fake",
                        },
                        {
                            "CODEBUDDY_GATEWAY_PORT": "54321",
                            "CODEBUDDY_GATEWAY_PASSWORD": "different",
                        },
                    ],
                ):
                    return reconnect.main()
            return reconnect.main()

    def test_auth_only_is_unknown_and_child_warning_preserved(self):
        code = self.invoke(
            SimpleNamespace(
                returncode=2, stdout="[UNKNOWN] no runtime", stderr="warning detail"
            )
        )
        self.assertEqual(code, 2)
        self.assertIn("warning detail", self.output.getvalue())
        self.assertIn("仅网关认证通过", self.output.getvalue())

    def test_exit_zero_without_verification_marker_is_not_success(self):
        self.assertEqual(
            self.invoke(
                SimpleNamespace(returncode=0, stdout="[API_OK] accepted", stderr="")
            ),
            2,
        )

    def test_verified_reload_and_auth_are_success_but_not_qq_delivery(self):
        self.assertEqual(
            self.invoke(
                SimpleNamespace(
                    returncode=0, stdout="[VERIFIED] hash matches", stderr=""
                )
            ),
            0,
        )
        self.assertIn("尚未验证 QQ", self.output.getvalue())

    def test_missing_reload_and_changed_readback_do_not_succeed(self):
        child = SimpleNamespace(
            returncode=0, stdout="[VERIFIED] hash matches", stderr=""
        )
        self.assertEqual(self.invoke(child, missing_reload=True), 2)
        self.assertEqual(self.invoke(child, changed_readback=True), 1)


class DoctorTests(OfflineCase):
    def test_syntax_check_compiles_every_split_module_without_executing_it(self):
        entry = self.root / "main.py"
        for path in doctor.source_paths(entry):
            path.write_bytes(b"raise RuntimeError('must not execute')\n")
        with patch.object(doctor, "PLUGIN", entry):
            doctor.results.clear()
            doctor.check_compile()
            self.assertIs(doctor.results[-1][1], True)
            for name in ("prompts.py", "reply_text.py", "source_files.py"):
                with self.subTest(source=name):
                    path = entry.with_name(name)
                    path.write_bytes(b"def invalid(:\n")
                    doctor.check_compile()
                    self.assertIs(doctor.results[-1][1], False)
                    path.unlink()
                    doctor.check_compile()
                    self.assertIsNone(doctor.results[-1][1])
                    path.write_bytes(b"raise RuntimeError('must not execute')\n")

    def test_missing_runtime_is_unknown_not_pass(self):
        with (
            patch.object(doctor, "HERE", self.root),
            patch.object(doctor, "PLUGIN", self.root / "main.py"),
        ):
            doctor.results.clear()
            doctor.check_code_version()
            doctor.check_plugin_loaded()
        self.assertEqual([item[1] for item in doctor.results], [None, None])

    def test_unavailable_process_and_port_observations_are_unknown(self):
        with patch.object(
            doctor.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=1, stdout=""),
        ):
            doctor.results.clear()
            doctor.check_ports()
            doctor.check_processes()
        self.assertTrue(doctor.results)
        self.assertTrue(all(item[1] is None for item in doctor.results))

    def test_unknown_prevents_healthy_exit_and_results_reset_each_run(self):
        names = (
            "check_compile",
            "check_bridge_selftest",
            "check_plugin_loaded",
            "check_code_version",
            "check_gateway",
            "check_question_status",
            "check_ports",
            "check_processes",
        )
        with contextlib.ExitStack() as stack:
            for index, name in enumerate(names):
                stack.enter_context(
                    patch.object(
                        doctor,
                        name,
                        side_effect=lambda i=index: doctor.record(str(i), None),
                    )
                )
            self.assertEqual(doctor.main(), 2)
            self.assertEqual(len(doctor.results), 8)
            self.assertEqual(doctor.main(), 2)
            self.assertEqual(len(doctor.results), 8)
        self.assertNotIn("全部通过", self.output.getvalue())

    def test_selftest_failed_lines_cannot_be_hidden_by_all_pass(self):
        with patch.object(
            doctor.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="[FAIL] regression\nALL PASS\n",
                stderr="",
            ),
        ):
            doctor.results.clear()
            doctor.check_bridge_selftest()
        self.assertIs(doctor.results[-1][1], False)


if __name__ == "__main__":
    unittest.main()
