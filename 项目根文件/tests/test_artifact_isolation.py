"""Concurrent artifact and image regressions without AstrBot, CLI, QQ, or AI."""

import asyncio
import os
import threading
import time
import unittest
from pathlib import Path

from artifact_test_support import ArtifactFixture, Event


class ArtifactIsolationTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    """Exercise overlapping same-user requests from separate source groups."""

    async def test_concurrent_approved_tasks_return_only_own_files_to_original_group(
        self,
    ):
        ready = asyncio.Event()
        directories = {}

        async def ask(prompt, conversation, *, master, mode, out_dir):
            self.assertTrue(master)
            self.assertEqual(mode, "execute")
            directory = Path(out_dir)
            directories[conversation] = directory
            artifact = directory / f"{conversation}.txt"
            artifact.write_text(conversation, encoding="utf-8")
            future = time.time() + 1
            os.utime(artifact, (future, future))
            if len(directories) == 2:
                ready.set()
            await ready.wait()  # Both tasks write before either scans outputs.
            return f"reply-{conversation}"

        self.bridge._ask = ask
        events = [Event("group-a"), Event("group-b")]

        async def dispatch(event):
            return [reply async for reply in self.bridge.on_message(event)]

        await asyncio.wait_for(
            asyncio.gather(*(dispatch(event) for event in events)), 2
        )
        self.assertEqual(len(set(directories.values())), 2)
        for directory in directories.values():
            self.assertEqual(directory.parent, self.root / "same-user")
            self.assertRegex(directory.name, r"^[a-f0-9]{32}$")
        deliveries = self.bridge._send_chain_to_session.await_args_list
        self.assertEqual(len(deliveries), 2)
        for call in deliveries:
            origin, chain = call.args
            group = origin.rsplit(":", 1)[-1]
            files = [Path(part.file) for part in chain.chain if hasattr(part, "file")]
            self.assertEqual(files, [directories[group] / f"{group}.txt"])
            self.assertEqual(files[0].read_text(encoding="utf-8"), group)
        self.assertTrue(all(event.stopped for event in events))

    async def test_concurrent_different_users_images_keep_original_destinations(self):
        barrier = threading.Barrier(2)
        delivered = {}

        async def send(origin, chain, **kwargs):
            group = origin.rsplit(":", 1)[-1]
            path = Path(chain.chain[-1].file)
            self.assertEqual(path.read_bytes(), b"\x89PNG\r\n\x1a\n" + group.encode())
            delivered[origin] = path
            return True

        self.bridge._send_chain_to_session.side_effect = send

        def generate(prompt, *, ratio, session, settings, **kwargs):
            kwargs["on_submitted"]("remote-" + prompt)
            barrier.wait(timeout=2)
            image = settings.output_dir / f"{prompt}.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + prompt.encode())
            return str(image)

        self.bridge._jimeng.ensure_session.side_effect = (
            lambda name, owner, settings, **kwargs: owner
        )
        self.bridge._jimeng.generate.side_effect = generate
        await asyncio.wait_for(
            asyncio.gather(
                self.bridge._run_draw(
                    "group-a",
                    "fake:GroupMessage:group-a",
                    "group-a",
                    "same-name",
                    sender_id="101",
                ),
                self.bridge._run_draw(
                    "group-b",
                    "fake:GroupMessage:group-b",
                    "group-b",
                    "same-name",
                    sender_id="102",
                ),
            ),
            3,
        )
        deliveries = self.bridge._send_chain_to_session.await_args_list
        self.assertEqual(len(deliveries), 2)
        for call in deliveries:
            origin, chain = call.args
            group = origin.rsplit(":", 1)[-1]
            images = [Path(part.file) for part in chain.chain if hasattr(part, "file")]
            self.assertEqual(images[0].name, f"{group}.png")
            self.assertEqual(images[0], delivered[origin])
            self.assertFalse(images[0].exists())
            job = next(
                j for j in self.bridge._draw_jobs.list_jobs() if j["umo"] == origin
            )
            self.assertEqual(images, [Path(job["path"])])
        for call in self.bridge._jimeng.generate.call_args_list:
            self.assertEqual(call.kwargs["settings"].bin_path, "fake-cli.exe")
            self.assertEqual(call.kwargs["settings"].model, "fake-model")

    async def test_same_user_busy_request_never_submits_a_second_image(self):
        started = threading.Event()
        release = threading.Event()

        def generate(*args, **kwargs):
            started.set()
            if not release.wait(timeout=3):
                raise AssertionError("Generation was not released")
            kwargs["on_submitted"]("remote-one")
            image = kwargs["settings"].output_dir / "one.png"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            return str(image)

        self.bridge._jimeng.generate.side_effect = generate
        first = asyncio.create_task(
            self.bridge._run_draw(
                "group-a", "fake:GroupMessage:group-a", "cat", "name", sender_id="101"
            )
        )
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            await self.bridge._run_draw(
                "group-b", "fake:GroupMessage:group-b", "dog", "name", sender_id="101"
            )
            self.assertEqual(self.bridge._jimeng.generate.call_count, 1)
            self.assertEqual(
                self.bridge._send_to_session.await_args.args[0],
                "fake:GroupMessage:group-b",
            )
        finally:
            release.set()
            await first
        self.assertEqual(
            self.bridge._send_chain_to_session.await_args.args[0],
            "fake:GroupMessage:group-a",
        )
        self.assertFalse(self.bridge._draw_busy)

    async def test_session_failure_never_submits_generation(self):
        for result in (RuntimeError("session unavailable"), "", "0"):
            with self.subTest(result=result):
                self.bridge._jimeng.ensure_session.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.bridge._jimeng.ensure_session.return_value = result
                self.bridge._send_to_session.reset_mock()
                await self.bridge._run_draw(
                    "group-a",
                    "fake:GroupMessage:group-a",
                    "cat",
                    "name",
                    sender_id="101",
                )
                self.bridge._jimeng.generate.assert_not_called()
                self.bridge._send_chain_to_session.assert_not_awaited()
                self.assertEqual(
                    self.bridge._send_to_session.await_args.args[0],
                    "fake:GroupMessage:group-a",
                )
                self.assertFalse(self.bridge._draw_busy)


if __name__ == "__main__":
    unittest.main()
