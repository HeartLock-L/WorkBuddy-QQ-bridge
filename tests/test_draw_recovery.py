"""Exercise saved image recovery using temporary files and mocked transports."""

import asyncio
import json
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import artifact_test_support as fixtures
from bridge_test_support import load_definitions


class DrawRecoveryTests(fixtures.ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    """Verify image progress survives interrupted workers without resubmission."""

    def setUp(self):
        super().setUp()
        self.bridge._jimeng.generate.side_effect = self._download
        self.extra_bridges = []

    async def asyncTearDown(self):
        for bridge in [self.bridge, *self.extra_bridges]:
            bridge._stopped = True
            tasks = list(bridge._background_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _download(self, prompt, *, settings, submit_id="", on_submitted=None, **kwargs):
        """Return a local fixture for exactly the supplied task settings.

        Args:
            prompt: Text identifying the fixture.
            settings: Per-task output directory and CLI parameters.
            submit_id: Existing remote task when resuming.
            on_submitted: Callback used only for a newly submitted task.
            **kwargs: Other production generation options.

        Returns:
            Path to the fixture image inside the task's directory.
        """
        if not submit_id:
            on_submitted("remote-" + uuid.uuid4().hex)
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        image = settings.output_dir / "result.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + prompt.encode("utf-8"))
        return str(image)

    def _job(self, state="queued", owner="101", group="original-group"):
        """Create durable progress at a selected stage.

        Args:
            state: Initial execution or delivery stage.
            owner: Original requester's session owner.
            group: Original destination group.

        Returns:
            The saved task record.
        """
        output = self.root / "images" / uuid.uuid4().hex
        job = self.bridge._draw_jobs.create(
            {
                "group_id": group,
                "umo": f"fake:GroupMessage:{group}",
                "sender_id": owner,
                "nickname": "requester",
                "prompt": f"image-for-{group}",
                "ratio": "1:1",
                "deadline_at": time.time() + 60,
                "expires_at": time.time() + 3600,
                "settings": {
                    "bin_path": "fake-cli.exe",
                    "output_dir": str(output),
                    "model": "fake-model",
                    "ratio": "1:1",
                    "resolution": "fake-resolution",
                    "timeout": 60,
                },
            }
        )
        changes = {"state": state}
        if state in {"submitted", "ready", "sending", "delivered"}:
            changes.update(session="123", submit_id="remote-existing")
        if state in {"ready", "sending", "delivered"}:
            output.mkdir(parents=True)
            image = output / "result.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + group.encode("utf-8"))
            changes["path"] = str(image)
        return self.bridge._draw_jobs.update(job["id"], **changes)

    def _new_instance(self):
        """Create independent in-memory state over the same on-disk jobs.

        Returns:
            A fresh bridge with mocked generation and message transports.
        """
        bridge = type(self.bridge)()
        bridge.__dict__.update(self.bridge.__dict__)
        bridge._draw_jobs = type(self.bridge._draw_jobs)(
            self.bridge._draw_jobs.directory,
            output_root=self.root / "images",
            selfie_prefix=self.globals["DRAW_SELFIE_PROMPT"],
        )
        bridge._draw_running = set()
        bridge._draw_busy = set()
        bridge._draw_slots = asyncio.Semaphore(2)
        bridge._background_tasks = set()
        bridge._jimeng = types.SimpleNamespace(
            ensure_session=Mock(return_value="123"),
            generate=Mock(side_effect=self._download),
            rename_session=Mock(return_value=False),
        )
        bridge._send_chain_to_session = AsyncMock(return_value=True)
        bridge._send_to_session = AsyncMock(return_value=True)
        self.extra_bridges.append(bridge)
        return bridge

    async def test_submitted_job_recovers_using_existing_remote_identifier(self):
        job = self._job("submitted")
        bridge = self._new_instance()
        await bridge._process_draw_job(job["id"])
        bridge._jimeng.ensure_session.assert_not_called()
        bridge._jimeng.generate.assert_called_once()
        self.assertEqual(
            bridge._jimeng.generate.call_args.kwargs["submit_id"],
            "remote-existing",
        )
        self.assertEqual(bridge._draw_jobs.get(job["id"])["state"], "delivered")
        self.assertEqual(bridge._send_chain_to_session.await_args.args[0], job["umo"])

    async def test_explicit_delivery_rejection_retries_saved_image_only(self):
        job = self._job("ready")
        self.bridge._send_chain_to_session.return_value = False
        await self.bridge._process_draw_job(job["id"])
        pending = self.bridge._draw_jobs.get(job["id"])
        self.assertEqual(pending["state"], "ready")
        self.assertEqual(pending["send_attempts"], 1)
        self.assertGreater(pending["next_retry_at"], time.time())
        self.bridge._draw_jobs.update(job["id"], next_retry_at=0)
        bridge = self._new_instance()
        await bridge._process_draw_job(job["id"])
        bridge._jimeng.generate.assert_not_called()
        self.bridge._jimeng.generate.assert_not_called()
        self.assertEqual(bridge._draw_jobs.get(job["id"])["state"], "delivered")
        sent_image = bridge._send_chain_to_session.await_args.args[1].chain[-1]
        self.assertEqual(Path(sent_image.file).resolve(), Path(job["path"]).resolve())

    async def test_uncertain_send_is_not_automatically_repeated(self):
        job = self._job("ready")
        self.bridge._send_chain_to_session.side_effect = OSError("ack lost")
        await self.bridge._process_draw_job(job["id"])
        self.assertEqual(
            self.bridge._draw_jobs.get(job["id"])["state"], "delivery_unknown"
        )
        bridge = self._new_instance()
        await bridge._process_draw_job(job["id"])
        await bridge._resume_draw_jobs()
        bridge._send_chain_to_session.assert_not_awaited()
        bridge._jimeng.generate.assert_not_called()
        self.assertFalse(bridge._background_tasks)

    async def test_recovery_keeps_each_saved_origin_and_image(self):
        jobs = [
            self._job("ready", owner="101", group="group-a"),
            self._job("ready", owner="102", group="group-b"),
        ]
        bridge = self._new_instance()
        delivered = {}

        async def send(origin, chain, **kwargs):
            path = Path(chain.chain[-1].file)
            delivered[origin] = path.read_bytes()
            return True

        bridge._send_chain_to_session.side_effect = send
        await asyncio.gather(*(bridge._process_draw_job(j["id"]) for j in jobs))
        expected = {job["umo"]: job for job in jobs}
        self.assertEqual(bridge._send_chain_to_session.await_count, 2)
        for call in bridge._send_chain_to_session.await_args_list:
            origin, chain = call.args
            job = expected[origin]
            self.assertEqual(
                Path(chain.chain[-1].file).resolve(), Path(job["path"]).resolve()
            )
            self.assertEqual(
                delivered[origin],
                b"\x89PNG\r\n\x1a\n" + job["group_id"].encode(),
            )
            self.assertFalse(Path(chain.chain[-1].file).exists())

    async def test_delivered_record_never_sends_again_after_restart(self):
        job = self._job("delivered")
        bridge = self._new_instance()
        await bridge._process_draw_job(job["id"])
        await bridge._resume_draw_jobs()
        bridge._send_chain_to_session.assert_not_awaited()
        bridge._jimeng.generate.assert_not_called()
        self.assertFalse(bridge._background_tasks)

    async def test_pause_or_unverified_account_keeps_ready_job_untouched(self):
        job = self._job("ready")
        for reason in ("paused", "unverified account"):
            with self.subTest(reason=reason):
                bridge = self._new_instance()
                bridge._can_send_active = lambda: False
                await bridge._resume_draw_jobs()
                await bridge._process_draw_job(job["id"])
                bridge._send_chain_to_session.assert_not_awaited()
                self.assertFalse(bridge._background_tasks)
                saved = bridge._draw_jobs.get(job["id"])
                self.assertEqual(saved["state"], "ready")
                self.assertEqual(saved["send_attempts"], 0)

    async def test_two_instances_cannot_deliver_a_claimed_job(self):
        job = self._job("ready")
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return True

        self.bridge._send_chain_to_session.side_effect = send
        first = asyncio.create_task(self.bridge._process_draw_job(job["id"]))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            bridge = self._new_instance()
            await bridge._process_draw_job(job["id"])
            bridge._send_chain_to_session.assert_not_awaited()
            bridge._jimeng.generate.assert_not_called()
        finally:
            release.set()
            await first
        self.assertEqual(self.bridge._send_chain_to_session.await_count, 1)
        self.assertEqual(self.bridge._draw_jobs.get(job["id"])["state"], "delivered")

    async def test_cancellation_holds_claim_until_cli_thread_has_stopped(self):
        job = self._job()
        entered, saw_stop, release = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def running_cli(*args, on_submitted, cancel_event, **kwargs):
            on_submitted("accepted-before-shutdown")
            entered.set()
            if not cancel_event.wait(3):
                raise AssertionError("Worker did not receive shutdown signal")
            saw_stop.set()
            if not release.wait(3):
                raise AssertionError("Test did not release the CLI worker")
            raise InterruptedError("CLI worker stopped")

        self.bridge._jimeng.generate.side_effect = running_cli
        first = asyncio.create_task(self.bridge._process_draw_job(job["id"]))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            first.cancel()
            self.assertTrue(await asyncio.to_thread(saw_stop.wait, 2))
            self.assertFalse(first.done())
            bridge = self._new_instance()
            with bridge._draw_jobs.claim(job["id"]) as claimed:
                self.assertFalse(claimed)
            await bridge._process_draw_job(job["id"])
            bridge._jimeng.generate.assert_not_called()
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
        with self.bridge._draw_jobs.claim(job["id"]) as claimed:
            self.assertTrue(claimed)
        self.assertEqual(self.bridge._draw_jobs.get(job["id"])["state"], "submitted")

    async def test_rename_failure_cannot_undo_successful_delivery(self):
        job = self._job("ready")
        self.bridge._jimeng.rename_session.side_effect = RuntimeError("rename failed")
        await self.bridge._process_draw_job(job["id"])
        self.assertEqual(self.bridge._draw_jobs.get(job["id"])["state"], "delivered")
        self.bridge._send_chain_to_session.assert_awaited_once()
        self.bridge._send_to_session.assert_not_awaited()

    async def test_crashed_submission_or_delivery_is_quarantined(self):
        for original, expected in (
            ("submitting", "submission_unknown"),
            ("sending", "delivery_unknown"),
        ):
            with self.subTest(original=original):
                job = self._job(original)
                bridge = self._new_instance()
                await bridge._process_draw_job(job["id"])
                await bridge._process_draw_job(job["id"])
                self.assertEqual(bridge._draw_jobs.get(job["id"])["state"], expected)
                bridge._jimeng.generate.assert_not_called()
                bridge._send_chain_to_session.assert_not_awaited()
                bridge._send_to_session.assert_awaited_once()

    async def test_initialize_starts_recovery_when_idle_chat_is_disabled(self):
        namespace = {
            "asyncio": asyncio,
            "time": time,
            "logger": types.SimpleNamespace(info=Mock()),
            "CODE_SHA256": "offline-test",
            "_RUNTIME": {},
            "MAX_REPLY_CHARS": 1800,
            "OTHERS_MAX_CHARS": 900,
            "HOT_RELOAD_ENABLED": False,
            "IDLE_CHAT_ENABLED": False,
        }
        load_definitions(namespace, methods={"initialize"})
        marker = object()

        def capture(coroutine):
            coroutine.close()
            return marker

        bridge = types.SimpleNamespace(
            _stopped=True,
            _instance_id="offline-instance",
            _runtime_started_at=0,
            _status_task=None,
            _memory_task=None,
            _memory=types.SimpleNamespace(loop=AsyncMock()),
            _write_runtime_status=Mock(),
            _runtime_status_loop=AsyncMock(),
            _cfg_int=lambda _key, default: default,
            _cfg_bool=Mock(return_value=False),
            _draw_recovery_task=None,
            _draw_recovery_loop=AsyncMock(),
            _maintenance_task=None,
            _cleanup_stop=types.SimpleNamespace(clear=Mock()),
            _maintenance_loop=AsyncMock(),
            _spawn_background=Mock(side_effect=capture),
            _idle_loop=Mock(side_effect=AssertionError("Idle loop must stay disabled")),
        )
        await namespace["Bridge"].initialize(bridge)
        bridge._draw_recovery_loop.assert_called_once()
        self.assertEqual(bridge._spawn_background.call_count, 4)
        bridge._idle_loop.assert_not_called()
        self.assertIs(bridge._draw_recovery_task, marker)
        self.assertFalse(bridge._stopped)

    async def test_explicit_submission_failure_is_final_without_an_unknown_state(self):
        job = self._job()
        self.bridge._jimeng.generate.side_effect = fixtures.JIMENG.GenerationFailed(
            "Remote service rejected this generation"
        )
        await self.bridge._process_draw_job(job["id"])
        saved = self.bridge._draw_jobs.get(job["id"])
        self.assertEqual(saved["state"], "failed")
        self.assertFalse(saved.get("submit_id"))
        self.assertTrue(saved["notice_sent"])
        await self.bridge._process_draw_job(job["id"])
        self.bridge._jimeng.generate.assert_called_once()
        self.bridge._send_chain_to_session.assert_not_awaited()

    async def test_one_query_timeout_preserves_identifier_and_total_deadline(self):
        job = self._job("submitted")
        self.bridge._jimeng.generate.side_effect = TimeoutError(
            "One CLI call timed out"
        )
        await self.bridge._process_draw_job(job["id"])
        saved = self.bridge._draw_jobs.get(job["id"])
        self.assertEqual(saved["state"], "submitted")
        self.assertEqual(saved["submit_id"], "remote-existing")
        self.assertEqual(saved["deadline_at"], job["deadline_at"])
        self.bridge._send_to_session.assert_not_awaited()
        self.bridge._draw_jobs.update(job["id"], next_retry_at=0)
        self.bridge._jimeng.generate.side_effect = self._download
        await self.bridge._process_draw_job(job["id"])
        self.assertEqual(self.bridge._draw_jobs.get(job["id"])["state"], "delivered")
        self.bridge._jimeng.ensure_session.assert_not_called()
        self.assertEqual(self.bridge._jimeng.generate.call_count, 2)
        for call in self.bridge._jimeng.generate.call_args_list:
            self.assertEqual(call.kwargs["submit_id"], "remote-existing")

    async def test_submission_identifier_save_failure_prevents_query_and_resubmit(self):
        job = self._job()
        client = fixtures.JIMENG.JimengClient(self.root / "sessions.json")
        client.ensure_session = Mock(return_value="123")
        self.bridge._jimeng = client
        runner = Mock(
            return_value=types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"submit_id": "accepted-but-not-saved"}),
                stderr="",
            )
        )
        original_update = self.bridge._draw_jobs.update

        def unreliable_save(job_id, **changes):
            if changes.get("state") == "submitted":
                raise OSError("Disk failed while saving the remote identifier")
            return original_update(job_id, **changes)

        with (
            patch.object(fixtures.JIMENG.subprocess, "run", runner),
            patch.object(self.bridge._draw_jobs, "update", side_effect=unreliable_save),
        ):
            await self.bridge._process_draw_job(job["id"])
        runner.assert_called_once()
        self.assertEqual(runner.call_args.args[0][1], "text2image")
        saved = self.bridge._draw_jobs.get(job["id"])
        self.assertEqual(saved["state"], "submission_unknown")
        self.assertFalse(saved.get("submit_id"))
        bridge = self._new_instance()
        await bridge._process_draw_job(job["id"])
        await bridge._resume_draw_jobs()
        bridge._jimeng.generate.assert_not_called()
        bridge._send_chain_to_session.assert_not_awaited()
        self.assertFalse(bridge._background_tasks)

    async def test_failed_notice_is_retried_and_marked_only_after_delivery(self):
        job = self._job("failed")
        self.bridge._send_to_session.return_value = False
        await self.bridge._process_draw_job(job["id"])
        saved = self.bridge._draw_jobs.get(job["id"])
        self.assertFalse(saved["notice_sent"])
        self.assertGreater(saved["next_retry_at"], time.time())
        self.assertEqual(saved["state"], "failed")
        self.bridge._draw_jobs.update(job["id"], next_retry_at=0)
        self.bridge._send_to_session.return_value = True
        await self.bridge._resume_draw_jobs()
        await asyncio.gather(*list(self.bridge._background_tasks))
        saved = self.bridge._draw_jobs.get(job["id"])
        self.assertTrue(saved["notice_sent"])
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(self.bridge._send_to_session.await_count, 2)
        self.bridge._jimeng.generate.assert_not_called()
        self.bridge._send_chain_to_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
