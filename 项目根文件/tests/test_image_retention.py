"""Verify selfie retention and bounded ordinary-image cache removal offline."""

import json
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from bridge_test_support import PLUGIN, load_module

JOBS = load_module(PLUGIN.with_name("image_jobs.py"), "retention_jobs_under_test")
PNG = b"\x89PNG\r\n\x1a\nfixture image"


class ImageRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "images"
        self.store = JOBS.ImageJobs(
            self.root / "jobs",
            Mock(),
            output_root=self.output,
            selfie_prefix="bot-selfie:",
        )

    def job(self, *, state="delivered", retain=False, prompt="a blue whale", **extra):
        directory = self.output / uuid.uuid4().hex
        directory.mkdir(parents=True)
        image = directory / "image.png"
        image.write_bytes(PNG)
        fields = {
            "group_id": "g",
            "umo": "fixture:GroupMessage:g",
            "sender_id": "owner",
            "nickname": "name",
            "prompt": prompt,
            "ratio": "",
            "settings": {
                "bin_path": "unused.exe",
                "output_dir": str(directory),
                "model": "fixture",
                "ratio": "1:1",
                "resolution": "fixture",
                "timeout": 30,
            },
            "deadline_at": 130,
            "expires_at": 1000,
        }
        if retain is not None:
            fields["retain_local"] = retain
        with patch.object(JOBS.time, "time", return_value=100):
            job = self.store.create(fields)
            job = self.store.update(job["id"], state=state, path=str(image), **extra)
        return job, image

    def clean(self, job, **kwargs):
        with self.store.claim(job["id"]) as owned:
            self.assertTrue(owned)
            return self.store.cleanup_artifact(job["id"], **kwargs)

    def test_delivered_ordinary_image_removed_but_record_and_remote_id_remain(self):
        job, image = self.job(session="existing-session", submit_id="existing-submit")
        sentinel = image.with_name("unrelated.txt")
        sentinel.write_text("keep", encoding="utf-8")
        self.assertEqual(self.clean(job, now=200), 1)
        saved = self.store.get(job["id"])
        self.assertFalse(image.exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue(saved["local_deleted"])
        self.assertEqual(saved["path"], str(image))
        self.assertEqual(saved["updated_at"], job["updated_at"])
        for field in ("state", "session", "submit_id", "umo", "settings"):
            self.assertEqual(saved[field], job[field])
        self.assertEqual(self.clean(job, now=200), 0)

    def test_selfies_and_unclassified_legacy_jobs_remain(self):
        for state in JOBS.STATES:
            extra = (
                {"session": "s", "submit_id": "remote"} if state == "submitted" else {}
            )
            job, image = self.job(state=state, retain=True, **extra)
            with self.subTest(state=state):
                self.assertEqual(self.clean(job, now=2000), 0)
                self.assertTrue(image.exists())
        job, image = self.job(retain=None)
        self.store.selfie_prefix = ""
        self.assertEqual(self.clean(job, now=2000), 0)
        self.assertTrue(image.exists())

    def test_exact_legacy_prefix_keeps_selfie_and_cleans_other_known_prompt(self):
        ordinary, disposable = self.job(retain=None)
        selfie, retained = self.job(retain=None, prompt="bot-selfie: walking")
        self.assertEqual(self.clean(ordinary, now=200), 1)
        self.assertEqual(self.clean(selfie, now=2000), 0)
        self.assertFalse(disposable.exists())
        self.assertTrue(retained.exists())

    def test_explicit_retention_flag_is_immutable_and_validated(self):
        for value in ("false", 0, None):
            job, _ = self.job()
            raw = self.store.directory / (job["id"] + ".json")
            content = json.loads(raw.read_text(encoding="utf-8"))
            content["retain_local"] = value
            raw.write_text(json.dumps(content), encoding="utf-8")
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.get(job["id"])
        job, _ = self.job(retain=True)
        with self.assertRaises(ValueError):
            self.store.update(job["id"], retain_local=False)
        with self.assertRaises(ValueError):
            self.store.update(job["id"], local_deleted="yes")

    def test_unfinished_ordinary_jobs_keep_image_until_original_expiry(self):
        for state in JOBS.STATES - {"delivered", "ended"}:
            extra = (
                {"session": "s", "submit_id": "remote"} if state == "submitted" else {}
            )
            job, image = self.job(state=state, **extra)
            with self.subTest(state=state):
                self.assertEqual(self.clean(job, now=999), 0)
                self.assertTrue(image.exists())
                self.assertEqual(self.clean(job, now=1000), 1)
                self.assertFalse(image.exists())
                self.assertEqual(self.store.get(job["id"])["state"], state)

    def test_admin_ended_job_cleans_immediately_and_empty_task_directory_removed(self):
        job, image = self.job(state="ended")
        self.assertEqual(self.clean(job, now=200), 1)
        self.assertFalse(image.parent.exists())

    def test_no_trusted_root_never_deletes(self):
        job, image = self.job()
        self.store.output_root = None
        self.assertEqual(self.clean(job, now=2000), 0)
        self.assertEqual(self.store.cleanup_artifacts(now=2000), 0)
        self.assertTrue(image.exists())

    def test_wrong_root_shared_directory_and_path_escape_are_preserved(self):
        for variant in ("wrong-root", "shared-directory", "escape", "non-image"):
            job, image = self.job()
            raw = self.store.directory / (job["id"] + ".json")
            if variant == "wrong-root":
                self.store.output_root = self.root / "elsewhere"
            elif variant == "shared-directory":
                job["settings"]["output_dir"] = str(self.output)
            elif variant == "escape":
                outside = self.root / "keep.png"
                outside.write_bytes(PNG)
                job["path"] = str(outside)
            else:
                image.write_bytes(b"not an image")
            raw.write_text(json.dumps(job), encoding="utf-8")
            with self.subTest(variant=variant):
                self.assertEqual(self.clean(job, now=2000), 0)
                self.assertTrue(image.exists())
                self.assertFalse(self.store.get(job["id"]).get("local_deleted", False))
            self.store.output_root = self.output

    def test_reparse_component_never_deleted(self):
        job, image = self.job()
        original = Path.lstat

        def redirected(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == image.parent:
                return types.SimpleNamespace(
                    st_mode=result.st_mode,
                    st_file_attributes=JOBS.stat.FILE_ATTRIBUTE_REPARSE_POINT,
                )
            return result

        with patch.object(Path, "lstat", redirected):
            self.assertEqual(self.clean(job, now=2000), 0)
        self.assertTrue(image.exists())

    def test_unlink_failure_keeps_progress_and_later_retry_succeeds(self):
        job, image = self.job()
        original = Path.unlink

        def rejected(path, *args, **kwargs):
            if path == image:
                raise PermissionError("fixture image is open")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", rejected):
            self.assertEqual(self.clean(job, now=200), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertTrue(image.exists())
        self.assertEqual(self.clean(job, now=200), 1)

    def test_recorded_output_link_is_rejected_when_saved_path_uses_real_directory(self):
        job, image = self.job()
        alias = self.output / uuid.uuid4().hex
        try:
            alias.symlink_to(image.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        job["settings"]["output_dir"] = str(alias)
        record = self.store.directory / (job["id"] + ".json")
        record.write_text(json.dumps(job), encoding="utf-8")
        self.assertEqual(self.clean(job, now=2000), 0)
        self.assertEqual(image.read_bytes(), PNG)
        self.assertFalse(self.store.get(job["id"]).get("local_deleted", False))

    def test_missing_file_after_interrupted_marker_save_is_reconciled(self):
        job, image = self.job()
        with patch.object(self.store, "update", side_effect=OSError("fixture disk")):
            self.assertEqual(self.clean(job, now=200), 0)
        self.assertFalse(image.exists())
        self.assertFalse(self.store.get(job["id"]).get("local_deleted", False))
        self.assertEqual(self.clean(job, now=201), 0)
        self.assertTrue(self.store.get(job["id"])["local_deleted"])

    def test_cleanup_pass_honors_locks_limit_cursor_and_cancellation(self):
        jobs = [self.job() for _ in range(3)]
        jobs.sort(key=lambda pair: pair[0]["id"], reverse=True)
        with self.store.claim(jobs[0][0]["id"]) as owned:
            self.assertTrue(owned)
            self.assertEqual(self.store.cleanup_artifacts(now=2000), 2)
            self.assertTrue(jobs[0][1].exists())
        stopped = threading.Event()
        stopped.set()
        self.assertEqual(self.store.cleanup_artifacts(stop_event=stopped, now=2000), 0)
        self.assertTrue(jobs[0][1].exists())
        self.assertEqual(self.store.cleanup_artifacts(limit=1, now=2000), 0)
        total = sum(self.store.cleanup_artifacts(limit=1, now=2000) for _ in range(3))
        self.assertEqual(total, 1)

    def test_archive_cleans_first_and_preserves_selfies(self):
        ordinary, disposable = self.job()
        selfie, kept = self.job(retain=True)
        now = 100 + 31 * 86400
        self.assertEqual(self.store.archive_terminal(now=now), 2)
        self.assertFalse(disposable.exists())
        self.assertTrue(kept.exists())
        archived = list((self.store.directory / "archive").rglob("*.json"))
        records = [json.loads(path.read_text(encoding="utf-8")) for path in archived]
        self.assertTrue(
            next(j for j in records if j["id"] == ordinary["id"])["local_deleted"]
        )
        self.assertNotIn(
            "local_deleted", next(j for j in records if j["id"] == selfie["id"])
        )

    def test_archive_keeps_live_reference_when_cleanup_cannot_remove_file(self):
        job, image = self.job()
        original = Path.unlink

        def rejected(path, *args, **kwargs):
            if path == image:
                raise PermissionError("fixture image is open")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", rejected):
            self.assertEqual(self.store.archive_terminal(now=100 + 31 * 86400), 0)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertTrue(image.exists())

    def test_invalid_cleanup_limits_fail_before_io(self):
        for kwargs in (
            {"limit": 0},
            {"limit": True},
            {"now": float("nan")},
            {"now": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.store.cleanup_artifacts(**kwargs)
        self.assertFalse(self.store.directory.exists())


if __name__ == "__main__":
    unittest.main()
