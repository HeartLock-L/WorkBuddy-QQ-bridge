"""Exercise durable image progress without CLI, network, or real chat data."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "AstrBot/data/plugins/workbuddy_bridge/image_jobs.py"
)
SPEC = importlib.util.spec_from_file_location("image_jobs_under_test", MODULE_PATH)
IMAGE_JOBS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMAGE_JOBS)


class ImageJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.logger = Mock()
        self.store = IMAGE_JOBS.ImageJobs(self.root / "jobs", self.logger)
        self.fields = {
            "group_id": "original-group",
            "umo": "test:GroupMessage:original-group",
            "sender_id": "owner",
            "nickname": "name",
            "prompt": "a blue whale",
            "ratio": "",
            "settings": {
                "bin_path": "fake-cli.exe",
                "output_dir": str(self.root / "images"),
                "model": "test-model",
                "ratio": "1:1",
                "resolution": "2k",
                "timeout": 30,
            },
            "deadline_at": time.time() + 30,
            "expires_at": time.time() + 86400,
        }

    def test_constructor_and_empty_listing_do_not_create_files(self):
        self.assertEqual(self.store.list_jobs(), [])
        self.assertFalse(self.store.directory.exists())

    def test_record_reopens_with_immutable_source_and_detached_input(self):
        job = self.store.create(self.fields)
        self.fields["settings"]["model"] = "changed-input"
        reopened = IMAGE_JOBS.ImageJobs(self.store.directory)
        saved = reopened.get(job["id"])
        self.assertEqual(saved["state"], "queued")
        self.assertEqual(saved["umo"], "test:GroupMessage:original-group")
        self.assertEqual(saved["settings"]["model"], "test-model")
        self.assertEqual(saved["id"], job["id"])

    def test_atomic_replace_failure_preserves_previous_record(self):
        job = self.store.create(self.fields)
        original = self.store.get(job["id"])
        with patch.object(
            IMAGE_JOBS.os, "replace", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                self.store.update(
                    job["id"], state="submitted", session="123", submit_id="remote-task"
                )
        self.assertEqual(self.store.get(job["id"]), original)
        self.assertEqual(list(self.store.directory.glob("*.tmp")), [])
        self.assertEqual(list(self.store.directory.glob(".*.tmp")), [])

    def test_create_failure_never_exposes_a_job(self):
        with patch.object(
            IMAGE_JOBS.os, "replace", side_effect=OSError("disk unavailable")
        ):
            with self.assertRaises(OSError):
                self.store.create(self.fields)
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_origin_parameters_and_deadlines_cannot_be_changed(self):
        job = self.store.create(self.fields)
        for field, value in {
            "umo": "test:GroupMessage:other-group",
            "sender_id": "other-user",
            "settings": dict(self.fields["settings"], model="another-model"),
            "deadline_at": time.time() + 99999,
            "id": "a" * 32,
            "prompt": "different request",
        }.items():
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.update(job["id"], **{field: value})
        self.assertEqual(self.store.get(job["id"]), job)

    def test_cross_instance_lock_and_release_allow_one_worker(self):
        job = self.store.create(self.fields)
        other = IMAGE_JOBS.ImageJobs(self.store.directory)
        with self.store.claim(job["id"]) as first:
            self.assertTrue(first)
            with other.claim(job["id"]) as second:
                self.assertFalse(second)
            self.store.update(
                job["id"], state="submitted", session="123", submit_id="remote-task"
            )
        with other.claim(job["id"]) as recovered:
            self.assertTrue(recovered)
            self.assertEqual(other.get(job["id"])["submit_id"], "remote-task")

    def test_another_process_cannot_claim_a_held_job(self):
        job = self.store.create(self.fields)
        script = """
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('job_lock_probe', sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
store = mod.ImageJobs(Path(sys.argv[2]))
with store.claim(sys.argv[3]) as owned:
    print('owned' if owned else 'busy')
"""
        with self.store.claim(job["id"]) as owned:
            self.assertTrue(owned)
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    script,
                    str(MODULE_PATH),
                    str(self.store.directory),
                    job["id"],
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
        self.assertEqual(result.stdout.strip(), "busy")

    def test_bad_record_is_preserved_and_does_not_hide_valid_records(self):
        job = self.store.create(self.fields)
        corrupt = self.store.directory / ("b" * 32 + ".json")
        corrupt.write_text('{"broken"', encoding="utf-8")
        self.assertEqual(
            [record["id"] for record in self.store.list_jobs()], [job["id"]]
        )
        self.assertEqual(corrupt.read_text(encoding="utf-8"), '{"broken"')
        self.logger.warning.assert_called_once()

    def test_terminal_state_survives_reload_and_claim_exception(self):
        job = self.store.create(self.fields)
        with self.assertRaises(RuntimeError):
            with self.store.claim(job["id"]) as owned:
                self.assertTrue(owned)
                self.store.update(job["id"], state="delivered", send_attempts=1)
                raise RuntimeError("worker interrupted after checkpoint")
        other = IMAGE_JOBS.ImageJobs(self.store.directory)
        with other.claim(job["id"]) as owned:
            self.assertTrue(owned)
            self.assertEqual(other.get(job["id"])["state"], "delivered")

    def test_submitted_without_original_task_id_cannot_be_recovered_as_new(self):
        job = self.store.create(self.fields)
        with self.assertRaises(ValueError):
            self.store.update(job["id"], state="submitted", session="123")
        path = self.store.directory / f"{job['id']}.json"
        job.update(state="submitted", session="123", submit_id="")
        path.write_text(json.dumps(job), encoding="utf-8")
        self.assertEqual(self.store.list_jobs(), [])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), job)

    def test_invalid_identifiers_states_and_settings_are_rejected(self):
        job = self.store.create(self.fields)
        for value in ("../outside", "A" * 32, "", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.get(value)
        for changes in (
            {"state": "unrecognized"},
            {"send_attempts": -1},
            {"next_retry_at": float("nan")},
            {"notice_sent": "yes"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(job["id"], **changes)
        data = json.loads((self.store.directory / f"{job['id']}.json").read_text())
        data["settings"]["timeout"] = -30
        (self.store.directory / f"{job['id']}.json").write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.store.get(job["id"])


if __name__ == "__main__":
    unittest.main()
