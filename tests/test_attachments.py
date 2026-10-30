"""Check scoped image selection and attachment writes with temporary files."""

import asyncio
import os
import shutil
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module

ATTACHMENTS = load_module(PLUGIN.with_name("attachments.py"), "attachments_under_test")


class Image:
    def __init__(self, file="", url=""):
        self.file = file
        self.url = url


class Reply:
    def __init__(self, chain):
        self.chain = chain


class ImageContextTests(unittest.TestCase):
    def test_recent_images_keep_group_author_expiry_and_order(self):
        history = {}
        with patch.object(ATTACHMENTS.time, "time", return_value=100):
            ATTACHMENTS.remember_images(
                history, "g1", "a", "A", ["old", "mine"], limit=2
            )
            ATTACHMENTS.remember_images(history, "g1", "b", "B", ["other"], limit=2)
            ATTACHMENTS.remember_images(history, "g2", "a", "A", ["elsewhere"], limit=2)
        self.assertEqual([row["url"] for row in history["g1"]], ["mine", "other"])
        self.assertEqual(
            ATTACHMENTS.recent_image(history, "g1", "a", ttl=30, now=130)["url"], "mine"
        )
        self.assertIsNone(ATTACHMENTS.recent_image(history, "g1", "a", ttl=30, now=131))
        self.assertIsNone(
            ATTACHMENTS.recent_image(history, "g1", "missing", ttl=30, now=100)
        )
        self.assertEqual(
            ATTACHMENTS.recent_image(history, "g2", "a", ttl=30, now=100)["url"],
            "elsewhere",
        )

    def test_blank_latest_address_does_not_fall_back_to_an_older_image(self):
        history = {
            "g": [
                {"url": "old", "sender": "a", "ts": 100},
                {"url": " ", "sender": "a", "ts": 100},
            ]
        }
        self.assertIsNone(ATTACHMENTS.recent_image(history, "g", "a", ttl=30, now=100))

    def test_waits_are_single_use_scoped_and_expire_strictly(self):
        waiting = {"old:user": {"ts": 69, "text": "expired"}}
        ATTACHMENTS.remember_image_wait(waiting, "g1", "a", "x" * 130, ttl=30, now=100)
        self.assertNotIn("old:user", waiting)
        self.assertEqual(len(waiting["g1:a"]["text"]), 120)
        self.assertIsNone(
            ATTACHMENTS.take_image_wait(waiting, "g1", "b", ttl=30, now=100)
        )
        self.assertIsNone(
            ATTACHMENTS.take_image_wait(waiting, "g2", "a", ttl=30, now=100)
        )
        self.assertIsNotNone(
            ATTACHMENTS.take_image_wait(waiting, "g1", "a", ttl=30, now=130)
        )
        self.assertIsNone(
            ATTACHMENTS.take_image_wait(waiting, "g1", "a", ttl=30, now=130)
        )
        ATTACHMENTS.remember_image_wait(waiting, "g1", "a", "again", ttl=30, now=100)
        self.assertIsNone(
            ATTACHMENTS.take_image_wait(waiting, "g1", "a", ttl=30, now=131)
        )
        self.assertNotIn("g1:a", waiting)


class AttachmentEntryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original = self.root / "source.png"
        self.original.write_bytes(b"image fixture")
        os.utime(self.original, (100, 100))
        self.materialize = AsyncMock(return_value=str(self.original))
        self.namespace = {
            "attachments": ATTACHMENTS,
            "Image": Image,
            "Reply": Reply,
            "IMAGE_CONTEXT_ENABLED": True,
            "IMAGE_CONTEXT_TTL": 30,
            "IMAGE_FOLLOWUP_TTL": 30,
            "RECENT_IMAGE_LIMIT": 5,
            "READ_ATTACHMENTS": True,
            "GROUP_ATTACHMENT_REQUIRE_AT": True,
            "ATTACHMENT_ROOT": self.root / "saved",
            "MAX_ATTACHMENTS_PER_MSG": 5,
            "_attachment_components": lambda event: [
                c for c in event.get_messages() if isinstance(c, Image)
            ],
            "_is_at_me": lambda event: event.addressed,
            "_mentions_image": lambda text: "image" in text,
            "_safe_attachment_dir": lambda name, sender: name + "-" + sender,
            "_materialize_attachment": self.materialize,
            "logger": Mock(),
            "time": time,
            "os": os,
            "Path": Path,
            "shutil": shutil,
            "uuid": uuid,
        }
        load_definitions(
            self.namespace,
            methods={
                "_remember_recent_images",
                "_referenced_image_comps",
                "_recent_context_image_comp",
                "_collect_attachments",
                "_wait_image",
                "_take_image_wait",
            },
        )
        self.bridge = self.namespace["Bridge"]()
        self.bridge._recent_images = {}
        self.bridge._img_wait = {}

    def event(self, *components, group="g", sender="a", addressed=False, private=False):
        return types.SimpleNamespace(
            get_messages=lambda: components,
            get_group_id=lambda: group,
            get_sender_id=lambda: sender,
            get_sender_name=lambda: sender.upper(),
            is_private_chat=lambda: private,
            addressed=addressed,
        )

    async def test_unaddressed_group_images_are_recorded_but_never_downloaded(self):
        event = self.event(Image(url="own-image"))
        self.bridge._remember_recent_images(event)
        self.assertEqual(self.bridge._recent_images["g"][0]["url"], "own-image")
        self.assertEqual(await self.bridge._collect_attachments(event, "A", "a"), [])
        self.materialize.assert_not_awaited()
        self.assertFalse((self.root / "saved").exists())

    async def test_explicit_reply_image_wins_over_cached_context(self):
        self.bridge._recent_images["g"] = [
            {"url": "cached", "sender": "a", "name": "A", "ts": time.time()}
        ]
        quoted = Image(url="quoted")
        event = self.event(Reply([quoted]))
        saved = await self.bridge._collect_attachments(event, "A", "a", "image")
        self.materialize.assert_awaited_once_with(quoted)
        self.assertEqual(saved.notes[saved[0]], "A（a）")
        self.assertEqual(Path(saved[0]).stat().st_mtime, 100)

    async def test_same_author_context_is_used_and_other_authors_create_waits(self):
        self.bridge._recent_images["g"] = [
            {"url": "mine", "sender": "a", "name": "Original A", "ts": time.time()},
            {"url": "other", "sender": "b", "name": "B", "ts": time.time()},
        ]
        saved = await self.bridge._collect_attachments(
            self.event(addressed=True), "A", "a", "image"
        )
        self.assertEqual(self.materialize.await_args.args[0].file, "mine")
        self.assertEqual(saved.notes[saved[0]], "Original A（a）")
        self.materialize.reset_mock()
        self.assertEqual(
            await self.bridge._collect_attachments(
                self.event(sender="c", addressed=True), "C", "c", "image"
            ),
            [],
        )
        self.materialize.assert_not_awaited()
        self.assertIn("g:c", self.bridge._img_wait)
        self.assertNotIn("g:a", self.bridge._img_wait)

    async def test_force_allows_verified_followup_and_attachment_cap_is_preserved(self):
        components = [Image(url="one"), Image(url="two")]
        self.namespace["MAX_ATTACHMENTS_PER_MSG"] = 1
        saved = await self.bridge._collect_attachments(
            self.event(*components), "A", "a", force=True
        )
        self.assertEqual(len(saved), 1)
        self.materialize.assert_awaited_once_with(components[0])

    async def test_disabled_reading_preserves_early_return_without_shared_notes(self):
        self.namespace["READ_ATTACHMENTS"] = False
        self.assertEqual(
            await self.bridge._collect_attachments(
                self.event(Image()), "A", "a", force=True
            ),
            [],
        )
        self.materialize.assert_not_awaited()
        self.assertFalse(hasattr(self.bridge, "_att_notes"))

    async def test_one_materialization_failure_does_not_drop_later_attachments(self):
        self.materialize.side_effect = [
            OSError("fixture unavailable"),
            str(self.original),
        ]
        saved = await self.bridge._collect_attachments(
            self.event(Image(), Image(), addressed=True), "A", "a"
        )
        self.assertEqual(len(saved), 1)
        self.assertEqual(Path(saved[0]).read_bytes(), b"image fixture")

    async def test_concurrent_groups_keep_authors_with_their_own_paths(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        first_component = Image(url="first")
        second_component = Image(url="second")

        async def materialize(component):
            if component is first_component:
                first_started.set()
                await release_first.wait()
            return str(self.original)

        self.namespace["_materialize_attachment"] = materialize
        first = asyncio.create_task(
            self.bridge._collect_attachments(
                self.event(first_component, group="g1", sender="a", addressed=True),
                "Alice",
                "a",
            )
        )
        await first_started.wait()
        second = await self.bridge._collect_attachments(
            self.event(second_component, group="g2", sender="b", addressed=True),
            "Bob",
            "b",
        )
        release_first.set()
        first = await first
        self.assertEqual(first.notes, {first[0]: "Alice（a）"})
        self.assertEqual(second.notes, {second[0]: "Bob（b）"})
        self.assertIsNot(first.notes, second.notes)
        self.assertNotEqual(first[0], second[0])
        self.assertFalse(hasattr(self.bridge, "_att_notes"))

    async def test_cancellation_propagates_without_creating_output(self):
        self.materialize.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.bridge._collect_attachments(
                self.event(Image(), addressed=True), "A", "a"
            )
        self.assertFalse((self.root / "saved").exists())


if __name__ == "__main__":
    unittest.main()
