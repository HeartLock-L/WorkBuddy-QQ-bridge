"""Exercise bounded archival without reading live image jobs or chat data."""

import json
import threading
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import test_image_jobs as fixtures

IMAGE_JOBS = fixtures.IMAGE_JOBS


class ImageJobArchiveTests(unittest.TestCase):
    def setUp(self):
        fixtures.ImageJobTests.setUp(self)
        self.old_time = 100
        self.now = self.old_time + 30 * 86400

    def old_job(self, state="delivered", **changes):
        with patch.object(IMAGE_JOBS.time, "time", return_value=self.old_time):
            job = self.store.create(self.fields)
            return self.store.update(job["id"], state=state, **changes)

    def archive_path(self, job):
        month = time.strftime("%Y-%m", time.gmtime(job["updated_at"]))
        return self.store.directory / "archive" / month / f"{job['id']}.json"

    def test_empty_archive_does_not_create_directories(self):
        self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertFalse(self.store.directory.exists())

    def test_terminal_bytes_images_and_lock_identity_are_preserved(self):
        image = self.root / "images" / "picture.png"
        image.parent.mkdir()
        image.write_bytes(b"original generated image")
        job = self.old_job(path=str(image))
        live = self.store.directory / f"{job['id']}.json"
        before = live.read_bytes()
        with self.store.claim(job["id"]):
            lock = self.store.directory / f"{job['id']}.lock"
            lock_before = lock.stat().st_ino
        self.assertEqual(self.store.archive_terminal(now=self.now), 1)
        self.assertEqual(self.archive_path(job).read_bytes(), before)
        self.assertEqual(image.read_bytes(), b"original generated image")
        self.assertEqual(lock.stat().st_ino, lock_before)
        self.assertFalse(live.exists())
        self.assertEqual(self.store.list_jobs(), [])

    def test_only_old_terminal_states_are_archived(self):
        jobs = {}
        for state in IMAGE_JOBS.STATES:
            changes = (
                {"session": "123", "submit_id": "original"}
                if state == "submitted"
                else {}
            )
            jobs[state] = self.old_job(state, **changes)
        with patch.object(IMAGE_JOBS.time, "time", return_value=self.old_time + 1):
            recent = self.store.create(self.fields)
            self.store.update(recent["id"], state="delivered")
        self.assertEqual(self.store.archive_terminal(now=self.now), 4)
        terminal = {"delivered", "failed", "expired", "ended"}
        live = {job["id"] for job in self.store.list_jobs()}
        self.assertEqual(
            live,
            {job["id"] for state, job in jobs.items() if state not in terminal}
            | {recent["id"]},
        )
        for state in terminal:
            self.assertTrue(self.archive_path(jobs[state]).exists())

    def test_bounded_rotation_does_not_starve_behind_active_jobs(self):
        for index, state in enumerate(("queued", "queued", "failed", "ended"), 1):
            with patch.object(
                IMAGE_JOBS.uuid,
                "uuid4",
                return_value=types.SimpleNamespace(hex=f"{index:032x}"),
            ):
                self.old_job(state)
        self.assertEqual(self.store.archive_terminal(now=self.now, limit=2), 0)
        self.assertEqual(self.store.archive_terminal(now=self.now, limit=2), 2)
        self.assertEqual(self.store.archive_terminal(now=self.now, limit=2), 0)
        self.assertEqual(len(self.store.list_jobs()), 2)

    def test_held_claim_is_skipped_until_the_next_pass(self):
        job = self.old_job()
        with self.store.claim(job["id"]) as owned:
            self.assertTrue(owned)
            self.assertEqual(self.store.archive_terminal(now=self.now), 0)
            self.assertFalse(self.archive_path(job).exists())
        self.assertEqual(self.store.archive_terminal(now=self.now), 1)

    def test_record_is_rechecked_after_claim(self):
        job = self.old_job()
        original_claim = self.store.claim

        @contextmanager
        def changed_before_claim(job_id):
            with original_claim(job_id) as owned:
                self.store.update(job_id, state="delivery_unknown")
                yield owned

        with patch.object(self.store, "claim", changed_before_claim):
            self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertEqual(self.store.get(job["id"])["state"], "delivery_unknown")
        self.assertFalse(self.archive_path(job).exists())

    def test_publish_failure_keeps_original_and_cleans_temporary_file(self):
        job = self.old_job()
        with patch.object(
            IMAGE_JOBS.os, "link", side_effect=OSError("archive unavailable")
        ):
            self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertFalse(self.archive_path(job).exists())
        self.assertEqual(list(self.store.directory.rglob("*.tmp")), [])

    def test_conflicting_archive_never_overwrites_either_record(self):
        job = self.old_job()
        destination = self.archive_path(job)
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"existing archive evidence")
        self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual(destination.read_bytes(), b"existing archive evidence")

    def test_retry_finishes_interruption_after_archive_publication(self):
        job = self.old_job()
        live = self.store.directory / f"{job['id']}.json"
        original_unlink = Path.unlink

        def fail_live_unlink(path, *args, **kwargs):
            if path == live:
                raise OSError("interrupted before removing live record")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_live_unlink):
            self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertEqual(self.archive_path(job).read_bytes(), live.read_bytes())
        self.assertEqual(self.store.archive_terminal(now=self.now), 1)
        self.assertEqual(
            json.loads(self.archive_path(job).read_text(encoding="utf-8")), job
        )
        self.assertFalse(live.exists())

    def test_cancelled_pass_preserves_records(self):
        job = self.old_job()
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.store.archive_terminal(now=self.now, stop_event=stop), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertFalse(self.archive_path(job).exists())

    def test_invalid_limits_fail_before_any_io(self):
        for arguments in (
            {"now": float("nan")},
            {"now": True},
            {"retention_days": 0},
            {"retention_days": 1.5},
            {"limit": 0},
            {"limit": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                self.store.archive_terminal(**arguments)
        self.assertFalse(self.store.directory.exists())

    def test_invalid_record_is_retained_without_hiding_valid_history(self):
        job = self.old_job()
        corrupt = self.store.directory / ("f" * 32 + ".json")
        corrupt.write_bytes(b"invalid json")
        self.assertEqual(self.store.archive_terminal(now=self.now), 1)
        self.assertTrue(self.archive_path(job).exists())
        self.assertEqual(corrupt.read_bytes(), b"invalid json")

    def test_archive_reparse_marker_is_rejected(self):
        job = self.old_job()
        archive = self.store.directory / "archive"
        archive.mkdir()
        original_lstat = Path.lstat

        def redirected_lstat(path, *args, **kwargs):
            value = original_lstat(path, *args, **kwargs)
            if path == archive:
                return types.SimpleNamespace(
                    st_mode=value.st_mode,
                    st_file_attributes=IMAGE_JOBS.stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return value

        with patch.object(Path, "lstat", redirected_lstat):
            self.assertEqual(self.store.archive_terminal(now=self.now), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual(list(archive.iterdir()), [])

    def test_optional_manual_progress_validates_and_survives_archive(self):
        job = self.old_job(
            "ended", admin_request_ids=["a" * 64], manual_send_attempts=1
        )
        for changes in (
            {"admin_request_ids": 123},
            {"admin_request_ids": ["a" * 64] * 2},
            {"admin_request_ids": ["invalid"]},
            {"admin_request_ids": ["a" * 64, "b" * 64, "c" * 64, "d" * 64]},
            {"manual_send_attempts": -1},
            {"manual_send_attempts": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(job["id"], **changes)
        self.assertEqual(self.store.archive_terminal(now=self.now), 1)
        self.assertEqual(
            json.loads(self.archive_path(job).read_text(encoding="utf-8")), job
        )


if __name__ == "__main__":
    unittest.main()
