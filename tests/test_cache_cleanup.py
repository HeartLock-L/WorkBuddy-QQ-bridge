"""Exercise cache retention and maintenance using temporary files and fake tasks."""

import asyncio
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module


def load_cache_code(root):
    """Load production cache code and wrappers without importing the framework."""
    functions = {
        "_clean_old_temp",
        "_temp_usage",
        "_clean_old_attachments",
        "_mb",
        "_cleanup_root",
        "_temp_cache_entries",
    }
    methods = {
        "initialize",
        "terminate",
        "_spawn_background",
        "_maintenance_loop",
        "_maybe_clean_attachments",
    }
    namespace = {
        "Path": Path,
        "os": os,
        "cache_maintenance": load_module(
            PLUGIN.with_name("cache_maintenance.py"), "cache_maintenance_under_test"
        ),
        "time": time,
        "threading": threading,
        "asyncio": asyncio,
        "logger": Mock(),
        "WORK_DIR": root,
        "TEMP_ROOT": root / "AstrBot/data/temp",
        "TEMP_KEEP_DAYS": 1,
        "TEMP_CLEAN_MAX_FILES": 2000,
        "ATTACHMENT_ROOT": root / "收到的附件",
        "ATTACHMENT_KEEP_DAYS": 2,
        "ATTACHMENT_CLEAN_INTERVAL_SEC": 3600,
        "CODE_SHA256": "offline-fingerprint",
        "_RUNTIME": {},
        "MAX_REPLY_CHARS": 1500,
        "OTHERS_MAX_CHARS": 1500,
        "HOT_RELOAD_ENABLED": False,
        "HOT_RELOAD_INTERVAL": 2,
        "IDLE_CHAT_ENABLED": False,
        "_is_paused": lambda: False,
    }
    load_definitions(namespace, functions=functions, methods=methods)
    return namespace["Bridge"], namespace


class CacheFileTests(unittest.TestCase):
    """Limit destructive cleanup to old files in the explicitly owned locations."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        _, self.namespace = load_cache_code(self.root)
        self.media = self.namespace["TEMP_ROOT"]
        self.media.mkdir(parents=True)
        self.now = float(int(time.time()))

    def make_file(self, relative, *, size=3, age=2 * 86400, root=None):
        path = (root or self.media) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_old_top_level_and_idle_images_are_removed_together(self):
        top = self.make_file("old.jpg", size=5)
        idle = self.make_file("idle_pics/old.jpg", size=7)
        fresh = self.make_file("fresh.jpg", age=60)
        fresh_idle = self.make_file("idle_pics/fresh.jpg", age=60)
        other = self.make_file("other/old.jpg")
        nested = self.make_file("idle_pics/nested/old.jpg")
        self.assertEqual(self.namespace["_clean_old_temp"](), (2, 12))
        self.assertFalse(top.exists())
        self.assertFalse(idle.exists())
        for kept in (fresh, fresh_idle, other, nested):
            self.assertTrue(kept.exists(), kept)
        self.assertTrue((self.media / "idle_pics").is_dir())

    def test_usage_counts_top_level_and_idle_images_only(self):
        self.make_file("old.jpg", size=5)
        self.make_file("idle_pics/new.jpg", size=7, age=1)
        self.make_file("other/old.jpg", size=11)
        self.make_file("idle_pics/nested/old.jpg", size=13)
        self.assertEqual(self.namespace["_temp_usage"](), 12)

    def test_retention_boundary_is_strict_and_recent_files_survive(self):
        before = self.make_file("before.jpg", age=86401)
        boundary = self.make_file("boundary.jpg", age=86400)
        after = self.make_file("after.jpg", age=86399)
        with patch.object(time, "time", return_value=self.now):
            self.assertEqual(self.namespace["_clean_old_temp"]()[0], 1)
        self.assertFalse(before.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue(after.exists())

    def test_delete_limit_is_shared_by_both_cache_locations(self):
        files = [self.make_file(f"top-{i}.jpg") for i in range(3)]
        files += [self.make_file(f"idle_pics/idle-{i}.jpg") for i in range(3)]
        self.namespace["TEMP_CLEAN_MAX_FILES"] = 4
        self.assertEqual(self.namespace["_clean_old_temp"](), (4, 12))
        self.assertEqual(sum(path.exists() for path in files), 2)

    def test_disabled_cleanup_preserves_cache(self):
        path = self.make_file("old.jpg")
        self.namespace["TEMP_KEEP_DAYS"] = 0
        self.assertEqual(self.namespace["_clean_old_temp"](), (0, 0))
        self.assertTrue(path.exists())

    def test_cancelled_cleanup_does_not_delete_files(self):
        path = self.make_file("idle_pics/old.jpg")
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.namespace["_clean_old_temp"](stop), (0, 0))
        self.assertTrue(path.exists())

    def test_cancellation_during_cleanup_stops_after_current_file(self):
        files = [self.make_file(f"old-{i}.jpg") for i in range(3)]
        stop = threading.Event()
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            stop.set()
            return result

        with patch.object(Path, "unlink", unlink):
            self.assertEqual(self.namespace["_clean_old_temp"](stop), (1, 3))
        self.assertEqual(sum(path.exists() for path in files), 2)

    def test_same_named_directory_outside_expected_root_is_rejected(self):
        outside = self.root / "unrelated/AstrBot/data/temp"
        path = self.make_file("old.jpg", root=outside)
        self.namespace["TEMP_ROOT"] = outside
        self.assertEqual(self.namespace["_clean_old_temp"](), (0, 0))
        self.assertEqual(self.namespace["_temp_usage"](), 0)
        self.assertTrue(path.exists())

    def test_missing_temp_directory_is_harmless(self):
        self.media.rmdir()
        self.assertEqual(self.namespace["_clean_old_temp"](), (0, 0))
        self.assertEqual(self.namespace["_temp_usage"](), 0)

    def test_unlink_failure_does_not_abort_other_candidates(self):
        blocked = self.make_file("blocked.jpg", size=5)
        removable = self.make_file("idle_pics/removable.jpg", size=7)
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("fixture file is busy")
            return original(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            self.assertEqual(self.namespace["_clean_old_temp"](), (1, 7))
        self.assertTrue(blocked.exists())
        self.assertFalse(removable.exists())

    def test_reparse_file_is_neither_deleted_nor_counted(self):
        blocked = self.make_file("linked.jpg", size=11)
        self.make_file("ordinary.jpg", size=5)
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == blocked:
                values = {
                    name: getattr(info, name)
                    for name in dir(info)
                    if name.startswith("st_")
                }
                values["st_file_attributes"] = 1024
                return types.SimpleNamespace(**values)
            return info

        with patch.object(Path, "lstat", lstat):
            self.assertEqual(self.namespace["_temp_usage"](), 5)
            self.assertEqual(self.namespace["_clean_old_temp"](), (1, 5))
        self.assertTrue(blocked.exists())

    def test_reparse_parent_is_rejected_before_scanning(self):
        blocked = self.make_file("old.jpg")
        parent = self.media.parent
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == parent:
                values = {
                    name: getattr(info, name)
                    for name in dir(info)
                    if name.startswith("st_")
                }
                values["st_file_attributes"] = 1024
                return types.SimpleNamespace(**values)
            return info

        with patch.object(Path, "lstat", lstat):
            self.assertEqual(self.namespace["_temp_usage"](), 0)
            self.assertEqual(self.namespace["_clean_old_temp"](), (0, 0))
        self.assertTrue(blocked.exists())

    def test_symlinked_idle_directory_does_not_clean_external_files(self):
        external = self.root / "external"
        outside = self.make_file("old.jpg", root=external)
        link = self.media / "idle_pics"
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"This host cannot create an unprivileged symlink: {exc}")
        self.addCleanup(link.unlink)
        self.assertEqual(self.namespace["_temp_usage"](), 0)
        self.assertEqual(self.namespace["_clean_old_temp"](), (0, 0))
        self.assertTrue(outside.exists())

    def test_attachment_retention_leaves_outputs_and_recent_files_untouched(self):
        attachments = self.namespace["ATTACHMENT_ROOT"]
        old = self.make_file("user/old.txt", age=3 * 86400, root=attachments)
        fresh = self.make_file("user/fresh.txt", age=86400, root=attachments)
        output = self.make_file(
            "result.txt", age=3 * 86400, root=self.root / "用户产出"
        )
        self.assertEqual(self.namespace["_clean_old_attachments"](), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(output.exists())

    def test_attachment_cancellation_preserves_files(self):
        path = self.make_file(
            "user/old.txt", age=3 * 86400, root=self.namespace["ATTACHMENT_ROOT"]
        )
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.namespace["_clean_old_attachments"](stop), 0)
        self.assertTrue(path.exists())

    def test_changed_attachment_retention_applies_on_the_next_call(self):
        path = self.make_file(
            "user/old.txt", age=3 * 86400, root=self.namespace["ATTACHMENT_ROOT"]
        )
        self.namespace["ATTACHMENT_KEEP_DAYS"] = 0
        self.assertEqual(self.namespace["_clean_old_attachments"](), 0)
        self.assertTrue(path.exists())
        self.namespace["ATTACHMENT_KEEP_DAYS"] = 2
        self.assertEqual(self.namespace["_clean_old_attachments"](), 1)
        self.assertFalse(path.exists())


class CacheMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    """Verify maintenance runs independently of chats and stops with the plugin."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.Bridge, self.namespace = load_cache_code(
            Path(self.temporary.name).resolve()
        )
        self.bridge = self.Bridge()
        self.bridge.context = types.SimpleNamespace()
        self.bridge._instance_id = "offline-instance"
        self.bridge._runtime_started_at = 0
        self.bridge._status_task = None
        self.bridge._memory_task = None
        self.bridge._memory = types.SimpleNamespace(
            loop=AsyncMock(), close=AsyncMock(return_value=True)
        )
        self.bridge._write_runtime_status = Mock()
        self.bridge._runtime_status_loop = AsyncMock()
        self.bridge._stopped = False
        self.bridge._background_tasks = set()
        self.bridge._question_operations = set()
        self.bridge._questions = types.SimpleNamespace(close=Mock())
        self.bridge._idle_task = None
        self.bridge._hot_task = None
        self.bridge._maintenance_task = None
        self.bridge._draw_recovery_task = None
        self.bridge._draw_jobs = types.SimpleNamespace(
            archive_terminal=Mock(return_value=0),
            cleanup_artifacts=Mock(return_value=0),
        )
        self.bridge._meme_collector = types.SimpleNamespace(prune=Mock(return_value=0))
        self.bridge._cleanup_stop = threading.Event()
        self.bridge._last_clean_ts = 0.0
        self.bridge._cfg_int = lambda _key, default: default
        self.bridge._cfg_bool = lambda _key, _default: False
        self.bridge._draw_recovery_loop = AsyncMock()
        self.namespace["_clean_old_attachments"] = Mock(return_value=0)
        self.namespace["_clean_old_temp"] = Mock(return_value=(0, 0))
        self.namespace["_temp_usage"] = Mock(return_value=0)

    async def asyncTearDown(self):
        self.bridge._cleanup_stop.set()
        await asyncio.wait_for(self.bridge.terminate(), 2)

    async def test_initialize_starts_cleanup_with_idle_chat_disabled(self):
        ran = asyncio.Event()

        async def clean():
            ran.set()

        self.bridge._maybe_clean_attachments = AsyncMock(side_effect=clean)
        await self.bridge.initialize()
        await asyncio.wait_for(ran.wait(), 1)
        self.assertIsNone(self.bridge._idle_task)
        self.assertIsNotNone(self.bridge._maintenance_task)
        self.assertFalse(self.bridge._maintenance_task.done())

    async def test_initialize_rejects_live_io_from_previous_instance(self):
        previous = types.SimpleNamespace(close=AsyncMock(return_value=False))
        self.bridge.context._workbuddy_bridge_memory_drain = previous
        with self.assertRaisesRegex(RuntimeError, "Previous memory operations"):
            await self.bridge.initialize()
        previous.close.assert_awaited_once_with(timeout=5)
        self.assertIsNone(self.bridge._maintenance_task)
        self.assertIsNone(self.bridge._memory_task)
        self.assertTrue(self.bridge._stopped)
        self.assertIs(self.bridge.context._workbuddy_bridge_memory_drain, previous)
        previous.close.return_value = True
        self.bridge._maybe_clean_attachments = AsyncMock()
        await self.bridge.initialize()
        self.assertFalse(hasattr(self.bridge.context, "_workbuddy_bridge_memory_drain"))
        self.assertIsNotNone(self.bridge._maintenance_task)

    async def test_repeated_initialize_keeps_one_maintenance_task(self):
        self.bridge._maybe_clean_attachments = AsyncMock()
        await self.bridge.initialize()
        first = self.bridge._maintenance_task
        await self.bridge.initialize()
        self.assertIs(self.bridge._maintenance_task, first)
        self.assertIn(first, self.bridge._background_tasks)

    async def test_maintenance_repeats_without_any_message(self):
        self.namespace["ATTACHMENT_CLEAN_INTERVAL_SEC"] = 0.01
        repeated = asyncio.Event()
        count = 0

        async def clean():
            nonlocal count
            count += 1
            if count >= 2:
                repeated.set()

        self.bridge._maybe_clean_attachments = AsyncMock(side_effect=clean)
        await self.bridge.initialize()
        await asyncio.wait_for(repeated.wait(), 1)
        self.assertGreaterEqual(count, 2)

    async def test_disabled_attachment_retention_does_not_disable_media_cleanup(self):
        self.namespace["ATTACHMENT_KEEP_DAYS"] = 0
        await self.bridge._maybe_clean_attachments()
        self.namespace["_clean_old_attachments"].assert_not_called()
        self.namespace["_clean_old_temp"].assert_called_once()

    async def test_disabled_media_retention_does_not_disable_attachment_cleanup(self):
        self.namespace["TEMP_KEEP_DAYS"] = 0
        await self.bridge._maybe_clean_attachments()
        self.namespace["_clean_old_attachments"].assert_called_once()
        self.namespace["_clean_old_temp"].assert_not_called()

    async def test_attachment_failure_does_not_skip_media_cleanup(self):
        self.namespace["_clean_old_attachments"].side_effect = OSError("fixture")
        await self.bridge._maybe_clean_attachments()
        self.namespace["_clean_old_temp"].assert_called_once()

    async def test_disabled_cache_retention_still_archives_image_history(self):
        self.namespace["ATTACHMENT_KEEP_DAYS"] = 0
        self.namespace["TEMP_KEEP_DAYS"] = 0
        await self.bridge._maybe_clean_attachments()
        self.bridge._draw_jobs.archive_terminal.assert_called_once_with(
            stop_event=self.bridge._cleanup_stop
        )
        self.bridge._draw_jobs.cleanup_artifacts.assert_called_once_with(
            stop_event=self.bridge._cleanup_stop
        )

    async def test_image_cleanup_failure_does_not_skip_other_maintenance(self):
        self.bridge._draw_jobs.cleanup_artifacts.side_effect = OSError("fixture")
        await self.bridge._maybe_clean_attachments()
        self.namespace["_clean_old_attachments"].assert_called_once()
        self.namespace["_clean_old_temp"].assert_called_once()
        self.bridge._draw_jobs.archive_terminal.assert_called_once()

    async def test_failed_media_pass_still_archives_image_history(self):
        self.namespace["_clean_old_temp"].side_effect = OSError("fixture")
        await self.bridge._maybe_clean_attachments()
        self.bridge._draw_jobs.archive_terminal.assert_called_once()

    async def test_terminate_cancels_waiting_maintenance(self):
        self.bridge._maybe_clean_attachments = AsyncMock()
        await self.bridge.initialize()
        task = self.bridge._maintenance_task
        await asyncio.sleep(0)
        await asyncio.wait_for(self.bridge.terminate(), 1)
        self.assertTrue(task.done())
        self.assertTrue(self.bridge._cleanup_stop.is_set())
        self.assertEqual(self.bridge._background_tasks, set())

    async def test_terminate_waits_for_running_cleanup_thread(self):
        entered = threading.Event()
        finish = threading.Event()
        exited = threading.Event()

        def cleanup(stop_event=None):
            entered.set()
            try:
                if not finish.wait(2):
                    raise TimeoutError("fixture cleanup was not released")
                return 0
            finally:
                exited.set()

        self.namespace["_clean_old_attachments"] = cleanup
        self.addCleanup(finish.set)
        await self.bridge.initialize()
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        stopped = asyncio.create_task(self.bridge.terminate())
        try:
            await asyncio.sleep(0.03)
            self.assertTrue(self.bridge._cleanup_stop.is_set())
            self.assertFalse(stopped.done())
            finish.set()
            await asyncio.wait_for(stopped, 1)
            self.assertTrue(exited.is_set())
            self.namespace["_clean_old_temp"].assert_not_called()
        finally:
            finish.set()
            await asyncio.wait_for(stopped, 2)


if __name__ == "__main__":
    unittest.main()
