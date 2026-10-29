"""Exercise source-scoped sticker collection without QQ or network access."""

import asyncio
import hashlib
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_module

COLLECTION = load_module(PLUGIN.with_name("meme_collection.py"), "meme_collection_test")


class Image:
    def __init__(self, url="https://example.invalid/sticker"):
        self.url = url


class SourceSelectionTests(unittest.TestCase):
    def event(self, subtype=1):
        component = Image()
        raw = {
            "post_type": "message",
            "message_type": "group",
            "group_id": "g",
            "user_id": "sender",
            "self_id": "bot",
            "message": [
                {"type": "image", "data": {"sub_type": subtype, "url": component.url}}
            ],
        }
        event = types.SimpleNamespace(
            is_private_chat=lambda: False,
            get_group_id=lambda: "g",
            get_sender_id=lambda: "sender",
            get_self_id=lambda: "bot",
            get_messages=lambda: [component],
            message_obj=types.SimpleNamespace(raw_message=raw),
        )
        return event, component, raw

    def test_only_explicit_custom_sticker_or_marketface_is_selected(self):
        for subtype in (1, "1"):
            event, component, _ = self.event(subtype)
            self.assertIs(COLLECTION.select_sticker(event, Image), component)
        for subtype in (None, 0, "0", True, 11, "image", {}):
            event, _, _ = self.event(subtype)
            self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, component, raw = self.event(None)
        raw["message"][0]["data"]["file"] = "marketface"
        self.assertIs(COLLECTION.select_sticker(event, Image), component)

    def test_private_self_notification_and_mismatched_sources_are_ignored(self):
        for key, value in (
            ("post_type", "notice"),
            ("message_type", "private"),
            ("group_id", "other"),
            ("user_id", "other"),
            ("self_id", "other"),
        ):
            event, _, raw = self.event()
            raw[key] = value
            self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, _, raw = self.event()
        event.is_private_chat = lambda: True
        self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, _, raw = self.event()
        event.get_sender_id = lambda: "bot"
        raw["user_id"] = "bot"
        self.assertIsNone(COLLECTION.select_sticker(event, Image))

    def test_missing_bot_identity_never_collects_even_when_raw_identity_is_empty(self):
        event, _, raw = self.event()
        event.get_self_id = lambda: ""
        raw["self_id"] = ""
        self.assertIsNone(COLLECTION.select_sticker(event, Image))

    def test_quoted_images_unknown_mapping_and_large_declared_size_are_ignored(self):
        event, _, raw = self.event()
        raw["message"][0]["type"] = "reply"
        self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, _, raw = self.event()
        raw["message"].append(raw["message"][0])
        self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, _, raw = self.event()
        raw["message"][0]["data"]["file_size"] = COLLECTION.MAX_BYTES + 1
        self.assertIsNone(COLLECTION.select_sticker(event, Image))
        event, _, raw = self.event()
        raw["message"][0]["data"]["url"] = "https://example.invalid/other"
        self.assertIsNone(COLLECTION.select_sticker(event, Image))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / "library"
        self.collector = COLLECTION.Collector(self.library, logger=Mock())
        self.counter = 0

    def source(self, data=None, size=None):
        self.counter += 1
        data = data or b"\x89PNG\r\n\x1a\n" + str(self.counter).encode() * 12
        if size is not None:
            data = data[:8] + b"x" * (size - 8)
        source = self.root / f"source-{self.counter}.bin"
        source.write_bytes(data)
        return source

    def store(self, scope, source, collector=None):
        return (collector or self.collector)._store(
            scope, source, threading.Event(), time.monotonic() + 10
        )

    def managed(self):
        return [
            p
            for p in self.collector.root.glob("*/*")
            if p.is_file() and p.suffix in COLLECTION.EXTENSIONS
        ]

    def name_record(self, image):
        record = image.parent / ".names" / (image.stem + ".json")
        record.parent.mkdir(exist_ok=True)
        record.write_text('{"name":"猫猫疑惑"}', encoding="utf-8")
        return record

    def test_content_deduplication_is_per_complete_conversation(self):
        source = self.source()
        first = Path(self.store("qq:group:one", source))
        again = Path(self.store("qq:group:one", source))
        second = Path(self.store("other:group:one", source))
        self.assertEqual(first, again)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual(
            first.name, hashlib.sha256(source.read_bytes()).hexdigest() + ".png"
        )
        self.assertEqual(
            first.parent.name, hashlib.sha256(b"qq:group:one").hexdigest()[:16]
        )
        self.assertEqual(first.read_bytes(), source.read_bytes())
        self.assertEqual(len(self.managed()), 2)

    def test_gif_bytes_are_preserved_with_verified_extension(self):
        source = self.source(b"GIF89a" + b"fixture" * 20)
        target = Path(self.store("group", source))
        self.assertEqual(target.suffix, ".gif")
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_same_size_corrupt_duplicate_is_not_accepted_or_refreshed(self):
        source = self.source()
        target = Path(self.store("one", source))
        corrupt = b"x" * source.stat().st_size
        target.write_bytes(corrupt)
        old = time.time() - 3600
        os.utime(target, (old, old))
        with self.assertRaisesRegex(OSError, "does not match"):
            self.store("one", source)
        self.assertEqual(target.read_bytes(), corrupt)
        self.assertAlmostEqual(target.stat().st_mtime, old, places=4)
        self.assertFalse(list(self.collector.root.glob("*/.incoming-*")))

    def test_ten_mib_boundary_and_oversized_invalid_files(self):
        source = self.source(size=COLLECTION.MAX_BYTES)
        self.assertEqual(
            Path(self.store("group", source)).stat().st_size, COLLECTION.MAX_BYTES
        )
        oversized = self.source(size=COLLECTION.MAX_BYTES + 1)
        with self.assertRaises(ValueError):
            self.store("group", oversized)
        invalid = self.source(b"<html>not a sticker</html>")
        with self.assertRaises(ValueError):
            self.store("group", invalid)
        self.assertFalse(list(self.collector.root.glob("*/.incoming-*")))

    def test_per_group_global_count_and_byte_quotas_evict_oldest_only(self):
        self.collector = COLLECTION.Collector(
            self.library, per_group=2, total_files=3, total_bytes=60
        )
        first = Path(self.store("one", self.source(size=20)))
        os.utime(first, (time.time() - 30, time.time() - 30))
        second = Path(self.store("one", self.source(b"\x89PNG\r\n\x1a\n" + b"b" * 12)))
        third = Path(self.store("one", self.source(b"\x89PNG\r\n\x1a\n" + b"c" * 12)))
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(third.exists())
        fourth = Path(self.store("two", self.source(b"\x89PNG\r\n\x1a\n" + b"d" * 22)))
        self.assertTrue(fourth.exists())
        self.assertLessEqual(sum(p.stat().st_size for p in self.managed()), 60)
        self.assertLessEqual(len(self.managed()), 3)

    def test_prune_removes_only_expired_owned_collected_files(self):
        old = Path(self.store("one", self.source()))
        fresh = Path(self.store("two", self.source()))
        old_time = time.time() - 31 * 86400
        os.utime(old, (old_time, old_time))
        manual = self.library / "manual.png"
        manual.write_bytes(b"user file")
        unknown = old.parent / "hand-made.png"
        unknown.write_bytes(b"user file")
        os.utime(unknown, (old_time, old_time))
        self.assertEqual(self.collector.prune(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(manual.exists())
        self.assertTrue(unknown.exists())
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.collector.prune(stop), 0)

    def test_expiration_removes_only_matching_name_in_the_original_group(self):
        source = self.source()
        old = Path(self.store("one", source))
        same_in_other_group = Path(self.store("two", source))
        fresh = Path(self.store("one", self.source()))
        old_name = self.name_record(old)
        other_name = self.name_record(same_in_other_group)
        fresh_name = self.name_record(fresh)
        manual = self.library / "manual.png"
        manual.write_bytes(b"manual")
        manual_name = self.name_record(manual)
        old_time = time.time() - 31 * 86400
        os.utime(old, (old_time, old_time))

        self.assertEqual(self.collector.prune(), 1)
        self.assertFalse(old.exists())
        self.assertFalse(old_name.exists())
        self.assertTrue(fresh_name.exists())
        self.assertTrue(other_name.exists())
        self.assertTrue(manual_name.exists())

    def test_name_directory_does_not_consume_image_quota_and_eviction_cleans_name(self):
        self.collector = COLLECTION.Collector(self.library, per_group=2, total_files=2)
        first = Path(self.store("one", self.source()))
        first_name = self.name_record(first)
        os.utime(first, (time.time() - 30, time.time() - 30))
        second = Path(self.store("one", self.source()))
        second_name = self.name_record(second)
        third = Path(self.store("one", self.source()))

        self.assertFalse(first.exists())
        self.assertFalse(first_name.exists())
        self.assertTrue(second.exists())
        self.assertTrue(second_name.exists())
        self.assertTrue(third.exists())
        self.assertEqual(len(self.managed()), 2)

    def test_orphan_names_are_cleaned_without_deleting_unknown_or_manual_files(self):
        image = Path(self.store("one", self.source()))
        orphan = self.name_record(image)
        image.unlink()
        unknown = orphan.with_name("hand-written.json")
        unknown.write_text("user notes", encoding="utf-8")
        other_extension = orphan.with_suffix(".txt")
        other_extension.write_text("user notes", encoding="utf-8")
        self.assertEqual(self.collector.prune(), 0)
        self.assertFalse(orphan.exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(other_extension.exists())
        unknown.unlink()
        other_extension.unlink()
        self.assertEqual(self.collector.prune(), 0)
        self.assertFalse(image.parent.exists())

    def test_failed_name_deletion_does_not_fail_image_removal_and_is_retried(self):
        image = Path(self.store("one", self.source()))
        record = self.name_record(image)
        old_time = time.time() - 31 * 86400
        os.utime(image, (old_time, old_time))
        original_unlink = Path.unlink

        def denied_name(path, *args, **kwargs):
            if path == record:
                raise PermissionError("Fixture denies only name removal")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", denied_name):
            self.assertEqual(self.collector.prune(), 1)
        self.assertFalse(image.exists())
        self.assertTrue(record.exists())
        self.assertEqual(self.collector.prune(), 0)
        self.assertFalse(record.exists())

    def test_redirected_name_directory_or_record_never_deletes_external_file(self):
        for redirect_directory in (False, True):
            image = Path(self.store(str(redirect_directory), self.source()))
            names = image.parent / ".names"
            outside = self.root / f"outside-{redirect_directory}"
            outside.mkdir()
            external = outside / (image.stem + ".json")
            external.write_text("external content", encoding="utf-8")
            try:
                if redirect_directory:
                    names.symlink_to(outside, target_is_directory=True)
                else:
                    names.mkdir()
                    (names / external.name).symlink_to(external)
            except OSError as exc:
                self.skipTest(f"Directory symlinks unavailable: {exc}")
            old_time = time.time() - 31 * 86400
            os.utime(image, (old_time, old_time))
            self.assertEqual(self.collector.prune(), 1)
            self.assertFalse(image.exists())
            self.assertEqual(external.read_text(encoding="utf-8"), "external content")

    def test_orphan_name_scan_obeys_shared_entry_budget_deadline_and_stop(self):
        image = Path(self.store("one", self.source()))
        names = image.parent / ".names"
        names.mkdir()
        for index in range(3):
            (names / (f"{index:064x}.json")).write_text("{}", encoding="utf-8")
        stop = threading.Event()
        stop.set()
        self.assertEqual(
            self.collector._prune_names(image.parent, stop, time.monotonic() + 5, 2),
            (0, False),
        )
        self.assertEqual(
            self.collector._prune_names(image.parent, None, time.monotonic() - 1, 2),
            (0, False),
        )
        self.assertEqual(
            self.collector._prune_names(image.parent, None, time.monotonic() + 5, 2),
            (2, False),
        )
        self.assertEqual(len(list(names.iterdir())), 1)

    def test_redirected_root_is_rejected_without_touching_external_file(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.library.mkdir()
        try:
            self.collector.root.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        with self.assertRaises(OSError):
            self.store("one", self.source())
        self.assertEqual(list(outside.iterdir()), [])

    def test_cross_instance_lock_skips_writes_and_pruning(self):
        other = COLLECTION.Collector(self.library, logger=Mock())
        source = self.source()
        with self.collector._claim() as held:
            self.assertTrue(held)
            self.assertIsNone(self.store("one", source, other))
            self.assertEqual(other.prune(), 0)
        self.assertIsNotNone(self.store("one", source, other))

    def test_cancelled_copy_has_no_owned_output_or_partial_file(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(TimeoutError):
            self.collector._store("one", self.source(), stop, time.monotonic() + 10)
        self.assertEqual(self.managed(), [])
        self.assertFalse(list(self.collector.root.glob("*/.incoming-*")))


class SchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.collector = COLLECTION.Collector(Path(self.temp.name), logger=Mock())
        self.tasks = []

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def test_two_inflight_limit_and_per_group_cooldown(self):
        wait = asyncio.Event()

        async def blocked(_component):
            await wait.wait()
            return ""

        self.assertTrue(
            self.collector.schedule(
                "one", object(), materialize=blocked, spawn=self.spawn
            )
        )
        self.assertFalse(
            self.collector.schedule(
                "one", object(), materialize=blocked, spawn=self.spawn
            )
        )
        self.assertTrue(
            self.collector.schedule(
                "two", object(), materialize=blocked, spawn=self.spawn
            )
        )
        self.assertFalse(
            self.collector.schedule(
                "three", object(), materialize=blocked, spawn=self.spawn
            )
        )
        wait.set()
        await asyncio.gather(*self.tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.collector.active, 0)

    async def test_stopped_spawn_or_early_cancellation_releases_admission(self):
        materialize = AsyncMock(return_value="")
        self.assertFalse(
            self.collector.schedule(
                "one", object(), materialize=materialize, spawn=lambda coroutine: None
            )
        )
        self.assertEqual(self.collector.active, 0)
        self.assertTrue(
            self.collector.schedule(
                "two", object(), materialize=materialize, spawn=self.spawn
            )
        )
        self.tasks[0].cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(self.collector.active, 0)
        materialize.assert_not_awaited()

    async def test_download_failure_does_not_create_library_or_escape(self):
        materialize = AsyncMock(side_effect=TimeoutError)
        self.assertTrue(
            self.collector.schedule(
                "one", object(), materialize=materialize, spawn=self.spawn
            )
        )
        await asyncio.gather(*self.tasks)
        self.assertFalse(self.collector.root.exists())


if __name__ == "__main__":
    unittest.main()
