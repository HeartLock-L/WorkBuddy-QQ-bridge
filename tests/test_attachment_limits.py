"""Exercise bounded attachment processing and actual GIF preview decoding."""

import asyncio
import builtins
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_module

ATTACHMENTS = load_module(
    PLUGIN.with_name("attachments.py"), "attachment_limits_under_test"
)


class AttachmentLimitsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "saved"

    async def save(self, sources, **kwargs):
        materialize = kwargs.pop(
            "materialize", AsyncMock(side_effect=lambda item: str(item))
        )
        return await ATTACHMENTS.save_attachments(
            sources,
            out_dir=self.output,
            sender_name="Alice",
            sender_id="a",
            context_component=None,
            context_meta={},
            max_files=5,
            materialize=materialize,
            logger=Mock(),
            **kwargs,
        )

    def source(self, name, size):
        path = self.root / name
        with path.open("wb") as stream:
            stream.truncate(size)
        return path

    async def test_exact_ten_mib_is_accepted_and_larger_is_rejected(self):
        maximum = ATTACHMENTS.MAX_ATTACHMENT_BYTES
        valid = self.source("exact.bin", maximum)
        result = await self.save([valid])
        self.assertEqual(len(result), 1)
        self.assertEqual(Path(result[0]).stat().st_size, maximum)
        self.assertEqual(result.notes[result[0]], "Alice（a）")
        self.assertFalse(result.notices)
        invalid = self.source("large.bin", maximum + 1)
        result = await self.save([invalid])
        self.assertFalse(result)
        self.assertIn("单个附件", result.notices[0])
        self.assertEqual(len(list(self.output.iterdir())), 1)

    async def test_aggregate_accepts_boundary_then_rejects_later_source(self):
        first = self.source("first.bin", 6 * 1024 * 1024)
        second = self.source("second.bin", 4 * 1024 * 1024)
        third = self.source("third.bin", 1)
        result = await self.save([first, second, third])
        self.assertEqual(len(result), 2)
        self.assertEqual(
            sum(Path(p).stat().st_size for p in result),
            ATTACHMENTS.MAX_ATTACHMENT_BYTES,
        )
        self.assertIn("合计", result.notices[0])
        self.assertEqual(len(list(self.output.iterdir())), 2)

    async def test_metadata_rejects_large_file_before_framework_download(self):
        component = Mock(size=ATTACHMENTS.MAX_ATTACHMENT_BYTES + 1)
        materialize = AsyncMock()
        result = await self.save([component], materialize=materialize)
        materialize.assert_not_awaited()
        self.assertFalse(result)
        self.assertFalse(self.output.exists())

    async def test_materialization_timeout_cancels_download_and_reports_safe_notice(
        self,
    ):
        cancelled = asyncio.Event()

        async def blocked(_):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()

        result = await self.save(
            [object()], materialize=blocked, materialize_timeout=0.01
        )
        self.assertTrue(cancelled.is_set())
        self.assertFalse(result)
        self.assertIn("超时", result.notices[0])
        self.assertNotIn(str(self.root), result.notices[0])
        self.assertFalse(self.output.exists())

    async def test_timeout_cleans_a_worker_result_that_finishes_after_abandonment(self):
        source = self.source("source.bin", 100)
        original_save = ATTACHMENTS._save_one
        copied = threading.Event()
        release = threading.Event()

        def late_result(*args):
            result = original_save(*args)
            copied.set()
            release.wait(2)
            return result

        with patch.object(ATTACHMENTS, "_save_one", side_effect=late_result):
            task = asyncio.create_task(self.save([source], message_timeout=0.1))
            for _ in range(100):
                if copied.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(copied.is_set())
            result = await task
            self.assertFalse(result)
            self.assertIn("超时", result.notices[0])
            release.set()
            for _ in range(100):
                if not list(self.output.iterdir()):
                    break
                await asyncio.sleep(0.005)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertTrue(source.exists())

    async def test_cancellation_cleans_completed_worker_result(self):
        source = self.source("source.bin", 100)
        original_save = ATTACHMENTS._save_one
        copied = threading.Event()
        release = threading.Event()

        def late_result(*args):
            result = original_save(*args)
            copied.set()
            release.wait(2)
            return result

        with patch.object(ATTACHMENTS, "_save_one", side_effect=late_result):
            task = asyncio.create_task(self.save([source]))
            for _ in range(100):
                if copied.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(copied.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            release.set()
            for _ in range(100):
                if not list(self.output.iterdir()):
                    break
                await asyncio.sleep(0.005)
        self.assertEqual(list(self.output.iterdir()), [])

    def gif_source(self, name="animation.dat", count=3):
        from PIL import Image

        frames = [
            Image.new(
                "RGB", (24, 20), (index * 41 % 256, index * 87 % 256, index * 131 % 256)
            )
            for index in range(count)
        ]
        path = self.root / name
        frames[0].save(
            path,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        return path

    async def test_gif_is_sniffed_without_extension_and_original_is_preserved(self):
        from PIL import Image

        source = self.gif_source()
        result = await self.save([source])
        self.assertEqual(len(result), 2)
        original, preview = map(Path, result)
        self.assertEqual(original.suffix, ".gif")
        self.assertEqual(original.read_bytes(), source.read_bytes())
        self.assertEqual(preview.suffix, ".jpg")
        self.assertLessEqual(preview.stat().st_size, ATTACHMENTS.GIF_PREVIEW_BYTES)
        with Image.open(preview) as image:
            self.assertEqual(image.size, (960, 344))
            self.assertNotEqual(image.getpixel((160, 32)), image.getpixel((480, 32)))
        self.assertIn("共解析 3 帧", result.descriptions[str(preview)])
        self.assertIn("不是完整播放", result.descriptions[str(preview)])
        self.assertEqual(result.notes[str(original)], result.notes[str(preview)])
        self.assertIn("GIF", ATTACHMENTS.describe(result))

    async def test_long_gif_preview_has_bounded_sample_count_and_explicit_partial_scope(
        self,
    ):
        source = self.gif_source(count=12)
        with patch.object(ATTACHMENTS, "GIF_MAX_FRAMES", 10):
            result = await self.save([source])
        self.assertEqual(len(result), 2)
        self.assertIn("仅解析前 10 帧", result.descriptions[result[1]])
        from PIL import Image

        with Image.open(result[1]) as image:
            self.assertLessEqual(image.width, 1280)
            self.assertLessEqual(image.height, 688)

    async def test_corrupt_gif_is_rejected_without_paths_or_partial_copy(self):
        source = self.root / "broken.gif"
        for content in (b"not a GIF", b"GIF89a" + b"\0" * 20):
            source.write_bytes(content)
            result = await self.save([source])
            self.assertFalse(result)
            self.assertTrue(result.notices)
            self.assertEqual(list(self.output.glob("*")), [])
            self.assertEqual(source.read_bytes(), content)

    async def test_missing_pillow_is_explicit_and_does_not_break_other_attachments(
        self,
    ):
        source = self.gif_source()
        original_import = builtins.__import__

        def without_pillow(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("fixture")
            return original_import(name, *args, **kwargs)

        other = self.source("other.bin", 20)
        with patch("builtins.__import__", side_effect=without_pillow):
            result = await self.save([source, other])
        self.assertEqual(len(result), 1)
        self.assertIn("缺少动图读取组件", result.notices[0])
        self.assertEqual(len(list(self.output.iterdir())), 1)

    async def test_gif_pixel_budget_rejects_oversized_canvas(self):
        source = self.gif_source()
        with patch.object(ATTACHMENTS, "GIF_FRAME_PIXELS", 100):
            result = await self.save([source])
        self.assertFalse(result)
        self.assertIn("画面太大", result.notices[0])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_describe_plain_paths_remains_compatible(self):
        self.assertEqual(ATTACHMENTS.describe(["first", "second"]), "- first\n- second")


if __name__ == "__main__":
    unittest.main()
