"""Test idle image downloads offline with bounded simulated HTTP streams."""

import asyncio
import io
import tempfile
import threading
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module

ATTACHMENTS = load_module(
    PLUGIN.with_name("attachments.py"), "idle_attachments_under_test"
)


class Response:
    def __init__(self, chunks=(), *, length=None, status=200, block=False, fail=False):
        self.chunks = chunks
        self.status = status
        self.headers = {} if length is None else {"Content-Length": length}
        self.content = self
        self.started = asyncio.Event()
        self.block = block
        self.fail = fail
        self.chunk_size = None
        self.iterated = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        self.chunk_size = size
        self.iterated = True
        for chunk in self.chunks:
            yield chunk
        self.started.set()
        if self.fail:
            raise asyncio.TimeoutError
        if self.block:
            await asyncio.sleep(60)


class Session:
    def __init__(self, response, **kwargs):
        self.response = response
        self.kwargs = kwargs
        self.url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def get(self, url):
        self.url = url
        return self.response


class IdleAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sessions = []
        self.response = Response()

        def session(**kwargs):
            current = Session(self.response, **kwargs)
            self.sessions.append(current)
            return current

        self.namespace = {
            "attachments": ATTACHMENTS,
            "aiohttp": types.SimpleNamespace(
                ClientSession=session,
                ClientTimeout=lambda **kwargs: types.SimpleNamespace(**kwargs),
            ),
            "TEMP_ROOT": self.root,
            "Path": Path,
            "uuid": uuid,
            "logger": Mock(),
        }
        load_definitions(self.namespace, methods={"_fetch_recent_image"})
        self.bridge = self.namespace["Bridge"]()

    def outputs(self):
        return list((self.root / "idle_pics").glob("*"))

    async def test_advertised_oversize_is_rejected_without_reading_body(self):
        self.response = Response(
            [b"unused"], length=str(ATTACHMENTS.MAX_ATTACHMENT_BYTES + 1)
        )
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertFalse(result)
        self.assertFalse(result.notices)
        self.assertFalse(self.response.iterated)
        self.assertEqual(self.outputs(), [])

    async def test_missing_length_accepts_exact_ten_mib_with_no_duplicate_download(
        self,
    ):
        self.response = Response(
            [b"\xff\xd8\xff" + b"x" * (65536 - 3)] + [b"x" * 65536] * 159
        )
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(
            Path(result[0]).stat().st_size, ATTACHMENTS.MAX_ATTACHMENT_BYTES
        )
        self.assertEqual(self.response.chunk_size, 64 * 1024)
        self.assertEqual(self.sessions[0].kwargs["timeout"].total, 15)
        self.assertTrue(self.sessions[0].kwargs["auto_decompress"])
        self.assertEqual(self.outputs(), [Path(result[0])])
        self.assertEqual(result.notes, {})

    async def test_actual_decompressed_stream_is_capped_despite_small_content_length(
        self,
    ):
        self.response = Response([b"x" * 65536] * 160 + [b"x"], length="20")
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertFalse(result)
        self.assertFalse(result.notices)
        self.assertEqual(self.outputs(), [])

    async def test_timeout_removes_partial_download_without_idle_notice(self):
        self.response = Response([b"first chunk"], fail=True)
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertFalse(result)
        self.assertFalse(result.notices)
        self.assertEqual(self.outputs(), [])

    async def test_download_cancellation_removes_partial_file_and_propagates(self):
        self.response = Response([b"first chunk"], block=True)
        task = asyncio.create_task(
            self.bridge._fetch_recent_image("https://fixture.invalid/image.jpg")
        )
        await self.response.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.outputs(), [])

    async def test_copy_cancellation_releases_source_before_download_cleanup(self):
        self.response = Response([b"\xff\xd8\xff" + b"fixture"])
        opened = threading.Event()

        def blocked(local, _dest, _remaining, _deadline, stop):
            with Path(local).open("rb"):
                opened.set()
                stop.wait(2)
            raise TimeoutError

        with patch.object(ATTACHMENTS, "_save_one", side_effect=blocked):
            task = asyncio.create_task(
                self.bridge._fetch_recent_image("https://fixture.invalid/image.jpg")
            )
            for _ in range(100):
                if opened.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(opened.is_set())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.outputs(), [])

    async def test_small_gif_is_preserved_and_has_temporal_preview(self):
        from PIL import Image

        first = Image.new("RGB", (20, 20), "red")
        second = Image.new("RGB", (20, 20), "blue")
        buffer = io.BytesIO()
        first.save(
            buffer, format="GIF", save_all=True, append_images=[second], duration=100
        )
        payload = buffer.getvalue()
        self.assertLess(len(payload), 1024)
        self.response = Response([payload], length=str(len(payload)))
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(Path(result[0]).suffix, ".gif")
        self.assertEqual(Path(result[0]).read_bytes(), payload)
        self.assertIn("共解析 2 帧", result.descriptions[result[1]])
        self.assertEqual(set(self.outputs()), {Path(p) for p in result})
        self.assertFalse(result.notices)

    async def test_http_200_html_is_ignored_and_valid_small_png_is_accepted(self):
        from PIL import Image

        self.response = Response([b"<html>temporary gateway error</html>"])
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.jpg"
        )
        self.assertFalse(result)
        self.assertEqual(self.outputs(), [])
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(buffer, format="PNG")
        payload = buffer.getvalue()
        self.assertLess(len(payload), 1024)
        self.response = Response([payload])
        result = await self.bridge._fetch_recent_image(
            "https://fixture.invalid/image.png"
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(Path(result[0]).read_bytes(), payload)

    async def test_unavailable_empty_and_non_ok_responses_stay_silent(self):
        self.assertFalse(await self.bridge._fetch_recent_image(""))
        for response in (Response(status=403), Response([])):
            self.response = response
            result = await self.bridge._fetch_recent_image(
                "https://fixture.invalid/image.jpg"
            )
            self.assertFalse(result)
            self.assertFalse(result.notices)
            self.assertEqual(self.outputs(), [])


if __name__ == "__main__":
    unittest.main()
