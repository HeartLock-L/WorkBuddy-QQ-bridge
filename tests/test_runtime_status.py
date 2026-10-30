"""Offline heartbeat and dynamic diagnostic discovery regressions."""

import json
import os
import time
import unittest
from unittest.mock import Mock, patch

from diagnostic_test_support import (
    OfflineCase,
    doctor,
    inspect_runtime,
    source_fingerprint,
)


class RuntimeStatusTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "main.py"
        self.source.write_text("VALUE = 1\n", encoding="utf-8")
        self.log = self.root / "runtime.log"
        self.now = time.time()
        self.status = {
            "version": 1,
            "instance": "test-instance",
            "pid": 123,
            "source_sha256": source_fingerprint(self.source),
            "started_at": self.now - 3600,
            "updated_at": self.now,
            "state": "running",
            "question": {
                "active": False,
                "waiting": 0,
                "limit": 5,
                "remote_state": "none",
                "last_outcome": "reply",
                "last_failure": "",
                "last_failure_at": 0,
            },
            "gateway": {
                "state": "connected",
                "base": "http://127.0.0.1:34567",
                "checked_at": self.now,
            },
            "proactive": {"paused": False, "failures": 0},
        }
        self.snapshot_path = self.source.with_name("bridge_status.json")

    def save(self):
        self.snapshot_path.write_text(json.dumps(self.status), encoding="utf-8")

    def inspect(self, **kwargs):
        return inspect_runtime(self.source, self.log, now=self.now, **kwargs)

    def test_old_startup_with_fresh_heartbeat_confirms_running_instance(self):
        self.save()
        evidence = self.inspect(require_heartbeat=True)
        self.assertIs(evidence.state, True)
        self.assertEqual(evidence.snapshot, self.status)

    def test_expired_snapshot_cannot_fall_back_to_recent_log(self):
        self.status["updated_at"] = self.now - 91
        self.status["gateway"]["checked_at"] = self.now - 100
        self.save()
        self.log.write_text(
            f"[workbuddy_bridge.main:1]: [runtime] sha256={self.status['source_sha256']} instance=fake started_at={self.now}\n",
            encoding="utf-8",
        )
        self.assertIsNone(self.inspect().state)
        self.assertIsNone(self.inspect(max_age_sec=300).state)

    def test_stopped_instance_cannot_be_reported_alive(self):
        self.status["state"] = "stopped"
        self.save()
        self.assertIs(self.inspect().state, False)

    def test_future_and_nonfinite_timestamps_are_unknown(self):
        for value in (self.now + 20, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.status["updated_at"] = value
                self.save()
                self.assertIsNone(self.inspect().state)

    def test_bad_schema_or_external_gateway_is_not_runtime_evidence(self):
        for mutation in (
            lambda: self.status.update(prompt="not permitted"),
            lambda: self.status["gateway"].update(base="https://external.invalid"),
            lambda: self.status["question"].update(active="yes"),
            lambda: self.status["question"].update(waiting=-1),
        ):
            original = json.loads(json.dumps(self.status))
            mutation()
            self.save()
            self.assertIsNone(self.inspect().state)
            self.status = original

    def test_corrupt_snapshot_does_not_use_legacy_log(self):
        self.snapshot_path.write_text("broken", encoding="utf-8")
        self.assertIsNone(self.inspect().state)

    def test_reload_requires_new_start_and_new_instance(self):
        self.save()
        self.assertIsNone(self.inspect(after_time=self.now).state)
        self.assertIsNone(self.inspect(excluded_instances={"test-instance"}).state)
        self.status["started_at"] = self.now
        self.status["instance"] = "reloaded"
        self.save()
        self.assertIs(
            self.inspect(
                after_time=self.now, excluded_instances={"test-instance"}
            ).state,
            True,
        )

    def test_source_change_or_unknown_fingerprint_is_not_accepted(self):
        self.save()
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        self.assertIs(self.inspect().state, False)
        self.status["source_sha256"] = "unknown"
        self.save()
        self.assertIsNone(self.inspect().state)

    def test_fresh_old_version_is_alive_but_version_check_fails(self):
        self.save()
        self.source.write_text("VALUE = 2\n", encoding="utf-8")
        with (
            patch.object(doctor, "PLUGIN", self.source),
            patch.object(doctor, "HERE", self.root),
        ):
            doctor.results.clear()
            doctor.check_plugin_loaded()
            doctor.check_code_version()
        self.assertEqual([item[1] for item in doctor.results], [True, False])
        self.assertIs(self.inspect().state, False)

    def test_fresh_unknown_version_is_alive_but_version_check_is_unknown(self):
        self.status["source_sha256"] = "unknown"
        self.save()
        with (
            patch.object(doctor, "PLUGIN", self.source),
            patch.object(doctor, "HERE", self.root),
        ):
            doctor.results.clear()
            doctor.check_plugin_loaded()
            doctor.check_code_version()
        self.assertEqual([item[1] for item in doctor.results], [True, None])
        self.assertIsNone(self.inspect().state)

    def test_version_evidence_is_separate_from_current_liveness(self):
        self.log.write_text(
            f"[workbuddy_bridge.main:1]: [runtime] sha256={self.status['source_sha256']} instance=old started_at={self.now - 3600}\n",
            encoding="utf-8",
        )
        self.assertIs(self.inspect(version_only=True).state, True)
        self.assertIsNone(self.inspect(require_heartbeat=True).state)
        self.status["state"] = "stopped"
        self.save()
        self.assertIs(self.inspect(version_only=True).state, True)
        self.assertIs(self.inspect(require_heartbeat=True).state, False)

    def test_doctor_reports_queue_and_recovery_reason_without_mutation(self):
        self.status["question"].update(
            active=True,
            waiting=2,
            remote_state="gateway_changed",
            last_failure="[状态待确认]",
            last_failure_at=self.now,
        )
        self.status["proactive"].update(paused=True, failures=3)
        self.save()
        before = self.snapshot_path.read_bytes()
        with (
            patch.object(doctor, "PLUGIN", self.source),
            patch.object(doctor, "HERE", self.root),
        ):
            doctor.results.clear()
            doctor.check_question_status()
        self.assertIsNone(doctor.results[-1][1])
        self.assertIn("等待 2/5", self.output.getvalue())
        self.assertIn("网关已换地址", self.output.getvalue())
        self.assertIn("/恢复问答", self.output.getvalue())
        self.assertIn("历史记录", self.output.getvalue())
        self.assertEqual(before, self.snapshot_path.read_bytes())

    def test_record_exists_is_not_claimed_to_be_a_running_remote_task(self):
        self.status["question"]["remote_state"] = "recorded"
        self.save()
        with (
            patch.object(doctor, "PLUGIN", self.source),
            patch.object(doctor, "HERE", self.root),
        ):
            doctor.results.clear()
            doctor.check_question_status()
        self.assertIs(doctor.results[-1][1], True)
        self.assertIn("是否结束需由网关确认", self.output.getvalue())

    def test_unknown_outcome_text_is_never_echoed(self):
        self.status["question"]["last_failure"] = "[private-fixture]"
        self.save()
        with (
            patch.object(doctor, "PLUGIN", self.source),
            patch.object(doctor, "HERE", self.root),
        ):
            doctor.check_question_status()
        self.assertNotIn("private-fixture", self.output.getvalue())
        self.assertIn("其他内部结果", self.output.getvalue())


class GatewayDiagnosticTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.env_file = self.root / "gateway.env"
        self.env_file.write_text(
            "CODEBUDDY_GATEWAY_PORT=12345\nCODEBUDDY_GATEWAY_PASSWORD=fake-token\n",
            encoding="utf-8",
        )
        patch.object(doctor, "ENV_FILE", self.env_file).start()
        patch.dict(os.environ, {}, clear=True).start()
        doctor.results.clear()

    def test_valid_known_candidate_avoids_port_scan(self):
        with (
            patch.object(doctor.gateway_probe, "try_auth", return_value=True) as auth,
            patch.object(doctor.gateway_probe, "scan_ports") as scan,
        ):
            doctor.check_gateway()
        self.assertIs(doctor.results[-1][1], True)
        auth.assert_called_once_with(12345, "fake-token")
        scan.assert_not_called()

    def test_port_drift_is_found_without_changing_credentials(self):
        before = self.env_file.read_bytes()
        with (
            patch.object(
                doctor.gateway_probe,
                "try_auth",
                side_effect=lambda port, token: port == 34567 and token == "fake-token",
            ) as auth,
            patch.object(
                doctor.gateway_probe, "scan_ports", return_value=[12345, 34567]
            ) as scan,
        ):
            doctor.check_gateway()
        self.assertIs(doctor.results[-1][1], True)
        self.assertIn("34567", doctor.results[-1][2])
        self.assertEqual(auth.call_count, 2)
        scan.assert_called_once()
        self.assertEqual(before, self.env_file.read_bytes())

    def test_new_environment_password_is_reported_as_not_yet_synchronized(self):
        before = self.env_file.read_bytes()
        with (
            patch.dict(
                os.environ,
                {"SERVER__PORT": "34567", "CODEBUDDY_GATEWAY_PASSWORD": "new-secret"},
            ),
            patch.object(
                doctor.gateway_probe,
                "try_auth",
                side_effect=lambda port, token: token == "new-secret",
            ),
            patch.object(doctor.gateway_probe, "scan_ports") as scan,
        ):
            doctor.check_gateway()
        self.assertIsNone(doctor.results[-1][1])
        self.assertIn("磁盘凭据未同步", doctor.results[-1][2])
        self.assertNotIn("new-secret", self.output.getvalue())
        scan.assert_not_called()
        self.assertEqual(before, self.env_file.read_bytes())

    def test_missing_credentials_do_not_scan_or_authenticate(self):
        self.env_file.write_text("", encoding="utf-8")
        with (
            patch.object(doctor.gateway_probe, "try_auth") as auth,
            patch.object(doctor.gateway_probe, "scan_ports") as scan,
        ):
            doctor.check_gateway()
        self.assertIs(doctor.results[-1][1], False)
        auth.assert_not_called()
        scan.assert_not_called()

    def test_http_200_with_authenticated_false_never_passes(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"authenticated": false}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        with (
            patch.object(doctor.gateway_probe.OPENER, "open", return_value=response),
            patch.object(doctor.gateway_probe, "scan_ports", return_value=[]),
        ):
            doctor.check_gateway()
        self.assertIs(doctor.results[-1][1], False)

    def test_scan_budget_exhaustion_is_unknown_not_authentication_failure(self):
        with (
            patch.object(doctor.time, "monotonic", side_effect=[100, 131]),
            patch.object(doctor.gateway_probe, "try_auth") as auth,
            patch.object(doctor.gateway_probe, "scan_ports") as scan,
        ):
            doctor.check_gateway()
        self.assertIsNone(doctor.results[-1][1])
        auth.assert_not_called()
        scan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
