"""Verify administrator-owned finite memory passes with offline services and SQLite."""

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from test_memory_service import Bridge, Event, MEMORY


class ManualMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "memory.sqlite3"
        self.service = MEMORY.MemoryService(self.path, logger=Mock())
        self.bridge = Bridge()
        self.event = Event()
        self.scope = MEMORY.scope_of(self.event)

    async def asyncTearDown(self):
        await self.service.close()
        self.temporary.cleanup()

    async def observe(
        self, text="咖啡不加糖，一直这么喝。", *, event=None, addressed=False
    ):
        event = event or self.event
        await self.service.observe(self.bridge, event, text, addressed=addressed)

    async def start(self):
        return await self.service.command(
            self.bridge, self.event, "/整理记忆 开始", master=True
        )

    async def next_batch(self):
        self.service._next_extract_at = 0
        return await self.service.run_once(self.bridge)

    def control_request(self, **changes):
        request = {
            "version": 1,
            "action": "start",
            "request_id": "a" * 32,
            "requested_at": time.time(),
        }
        request.update(changes)
        self.service._control_path.write_text(json.dumps(request), encoding="utf-8")
        return request

    async def test_collection_and_legacy_auto_flag_never_start_a_model_pass(self):
        self.bridge.settings["memory_auto_extract"] = True
        await self.observe()
        self.service._last_dialogue_at -= 600
        for _ in range(3):
            self.assertFalse(await self.service.run_once(self.bridge))
        self.bridge._ask.assert_not_awaited()
        self.bridge._memory_idle_status.assert_not_awaited()
        self.assertEqual(self.service.summary()["manual_state"], "idle")
        self.assertIsNotNone(self.service.store.prepare_batch(self.scope, "17"))

    async def test_only_verified_master_can_start_stop_or_inspect_manual_pass(self):
        await self.observe()
        for command in ("开始", "停止", "状态"):
            answer = await self.service.command(
                self.bridge, self.event, "/整理记忆 " + command, master=False
            )
            self.assertIn("管理员", answer)
        self.assertIsNone(self.service._manual_upper_id)
        self.bridge._memory_idle_status.assert_not_awaited()
        self.assertIn("已开启", await self.start())
        self.assertTrue(await self.next_batch())
        self.bridge._ask.assert_awaited_once()

    async def test_invalid_adapter_identity_cannot_start_even_with_master_flag(self):
        await self.observe()
        self.event.unified_msg_origin = "qq:GroupMessage:wrong"
        await self.start()
        self.assertIsNone(self.service._manual_upper_id)
        self.bridge._memory_idle_status.assert_not_awaited()

    async def test_start_captures_boundary_and_later_messages_wait_for_next_pass(self):
        await self.observe("我一直喜欢喝热咖啡。")
        await self.start()
        captured = self.service._manual_upper_id
        await self.observe("我也喜欢红茶。", event=Event(message="2"))
        await self.observe("我喜欢跑步。", event=Event(sender="18", message="3"))
        self.assertGreater(self.service.store.capture_upper_id(), captured)
        self.assertTrue(await self.next_batch())
        material = json.loads(self.bridge._ask.await_args.args[0].rsplit("\n", 1)[-1])
        self.assertEqual(
            [row["text"] for row in material["原话"]], ["我一直喜欢喝热咖啡。"]
        )
        self.assertEqual(material["上下文"], [])
        self.assertFalse(await self.next_batch())
        self.assertEqual(self.service.summary()["manual_state"], "completed")
        self.assertIsNone(self.service._manual_upper_id)
        self.assertFalse(await self.next_batch())
        self.assertEqual(self.bridge._ask.await_count, 1)
        await self.start()
        self.assertTrue(await self.next_batch())
        new_material = json.loads(
            self.bridge._ask.await_args.args[0].rsplit("\n", 1)[-1]
        )
        self.assertIn("我也喜欢红茶。", [row["text"] for row in new_material["原话"]])

    async def test_one_manual_pass_drains_multiple_batches_without_per_member_retry_wait(
        self,
    ):
        for index in range(1, 26):
            await self.observe("我喜欢每天喝热茶。", event=Event(message=str(index)))
        await self.start()
        self.assertTrue(await self.next_batch())
        first = json.loads(self.bridge._ask.await_args.args[0].rsplit("\n", 1)[-1])
        self.assertEqual(len(first["原话"]), 20)
        self.assertTrue(await self.next_batch())
        second = json.loads(self.bridge._ask.await_args.args[0].rsplit("\n", 1)[-1])
        self.assertEqual(len(second["原话"]), 5)
        self.assertFalse(await self.next_batch())
        self.assertEqual(self.service.summary()["manual_state"], "completed")
        self.assertEqual(self.service.summary()["manual_batches"], 2)

    async def test_unknown_or_busy_global_state_never_submits_or_automatically_resumes(
        self,
    ):
        await self.observe()
        for state in ("not_idle", "idle_unverified", "pending_confirmation"):
            with self.subTest(state=state):
                self.bridge._memory_idle_status.return_value = state
                await self.start()
                self.assertEqual(self.service.summary()["last_outcome"], state)
                self.assertEqual(self.service.summary()["manual_state"], "blocked")
                self.bridge._memory_idle_status.return_value = "ready"
                self.assertFalse(await self.next_batch())
        self.bridge._ask.assert_not_awaited()

    async def test_busy_state_after_start_ends_pass_before_model_submission(self):
        await self.observe()
        await self.start()
        self.bridge._memory_idle_status.return_value = "not_idle"
        self.assertFalse(await self.next_batch())
        self.assertIsNone(self.service._manual_upper_id)
        self.bridge._memory_idle_status.return_value = "ready"
        self.assertFalse(await self.next_batch())
        self.bridge._ask.assert_not_awaited()

    async def test_new_direct_chat_ends_pass_and_elapsed_time_does_not_rearm_it(self):
        await self.observe()
        await self.start()
        await self.observe("今天吃什么？", event=Event(message="2"), addressed=True)
        self.assertFalse(await self.next_batch())
        self.assertEqual(self.service.summary()["manual_state"], "blocked")
        self.service._last_dialogue_at -= 600
        self.assertFalse(await self.next_batch())
        self.bridge._ask.assert_not_awaited()

    async def test_status_command_does_not_interrupt_a_pass(self):
        await self.observe()
        await self.start()
        dialogue = self.service._last_dialogue_at
        await self.observe("/整理记忆 状态", event=Event(message="2"), addressed=True)
        answer = await self.service.command(
            self.bridge, self.event, "/整理记忆 状态", master=True
        )
        self.assertEqual(self.service._last_dialogue_at, dialogue)
        self.assertIn("整理状态", answer)
        self.assertTrue(await self.next_batch())

    async def test_stop_preserves_already_submitted_batch_but_never_submits_another(
        self,
    ):
        await self.observe()
        await self.observe("我一直喜欢喝红茶。", event=Event(sender="18", message="2"))
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()

        async def answer_later(prompt, *args, **kwargs):
            entered.set()
            await release.wait()
            return Bridge.extract(prompt)

        self.bridge._ask.side_effect = answer_later
        task = asyncio.create_task(self.next_batch())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertFalse(await self.next_batch())
            answer = await self.service.command(
                self.bridge, self.event, "/整理记忆 停止", master=True
            )
            self.assertIn("当前", answer)
            self.assertFalse(task.done())
            self.assertIsNone(self.service._manual_upper_id)
            self.assertIn("已有", await self.start())
            release.set()
            self.assertTrue(await task)
            self.assertEqual(self.service.summary()["manual_state"], "stopped")
            self.assertEqual(self.service.summary()["manual_saved"], 1)
            self.assertFalse(await self.next_batch())
            self.bridge._ask.assert_awaited_once()
            self.assertEqual(len(self.service.store.list_own(self.scope, "17")), 1)
        finally:
            release.set()
            if not task.done():
                await task

    async def test_failed_model_result_ends_pass_without_automatic_retry(self):
        await self.observe()
        await self.start()
        self.bridge._ask.side_effect = None
        self.bridge._ask.return_value = "[状态待确认]"
        self.assertFalse(await self.next_batch())
        self.assertEqual(self.service.summary()["last_outcome"], "pending_confirmation")
        self.assertEqual(self.service.summary()["manual_state"], "blocked")
        self.bridge._ask.side_effect = Bridge.extract
        self.assertFalse(await self.next_batch())
        self.bridge._ask.assert_awaited_once()
        self.assertIsNotNone(self.service.store.prepare_batch(self.scope, "17"))

    async def test_empty_pass_finishes_without_calling_the_model(self):
        await self.start()
        await self.next_batch()
        self.assertIsNone(self.service._manual_upper_id)
        self.assertEqual(self.service.summary()["manual_state"], "completed")
        self.bridge._ask.assert_not_awaited()

    async def test_local_start_without_a_trusted_runtime_identity_does_not_wait_forever(
        self,
    ):
        self.service.store.record(self.scope, "17", "测试成员", "1", "我喜欢喝茶。")
        self.control_request()
        await self.service.poll_control(self.bridge)
        self.assertIsNone(self.service._manual_upper_id)
        self.assertEqual(self.service.summary()["manual_state"], "blocked")
        self.assertFalse(await self.next_batch())
        self.bridge._ask.assert_not_awaited()

    async def test_concurrent_start_requests_cannot_open_two_passes(self):
        await self.observe()
        entered, release = asyncio.Event(), asyncio.Event()

        async def check_later():
            entered.set()
            await release.wait()
            return "ready"

        self.bridge._memory_idle_status.side_effect = check_later
        first = asyncio.create_task(self.start())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            second = await asyncio.wait_for(self.start(), 1)
            self.assertIn("已有", second)
            release.set()
            await first
            self.bridge._memory_idle_status.assert_awaited_once()
            self.assertEqual(self.service.summary()["manual_state"], "waiting")
        finally:
            release.set()
            if not first.done():
                await first

    async def test_stop_during_idle_check_revokes_pending_start(self):
        await self.observe()
        entered, release = asyncio.Event(), asyncio.Event()

        async def check_later():
            entered.set()
            await release.wait()
            return "ready"

        self.bridge._memory_idle_status.side_effect = check_later
        first = asyncio.create_task(self.start())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.service._stop_manual()
            release.set()
            await first
            self.assertIsNone(self.service._manual_upper_id)
            self.assertEqual(self.service.summary()["manual_state"], "stopped")
            self.assertFalse(await self.next_batch())
        finally:
            release.set()
            if not first.done():
                await first

    async def test_new_dialogue_during_start_prevents_opening_pass(self):
        await self.observe()

        async def check_with_dialogue():
            self.service.note_dialogue()
            return "ready"

        self.bridge._memory_idle_status.side_effect = check_with_dialogue
        await self.start()
        self.assertIsNone(self.service._manual_upper_id)
        self.assertEqual(self.service.summary()["manual_state"], "blocked")
        self.bridge._ask.assert_not_awaited()

    async def test_stop_while_preparing_blocks_restart_until_old_pass_has_exited(self):
        await self.observe()
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original_io = self.service._io

        async def prepare_later(function, *args, **kwargs):
            if function.__name__ == "prepare_batch":
                entered.set()
                await release.wait()
            return await original_io(function, *args, **kwargs)

        try:
            with patch.object(self.service, "_io", side_effect=prepare_later):
                task = asyncio.create_task(self.next_batch())
                await asyncio.wait_for(entered.wait(), 3)
                self.service._stop_manual()
                self.assertIn("已有", await self.start())
                release.set()
                self.assertFalse(await task)
            self.assertEqual(self.service.summary()["manual_state"], "stopped")
            self.bridge._ask.assert_not_awaited()
            self.assertIn("已开启", await self.start())
            self.assertTrue(await self.next_batch())
            self.bridge._ask.assert_awaited_once()
        finally:
            release.set()
            if "task" in locals() and not task.done():
                await task

    async def test_database_failure_before_submission_is_failed_not_completed(self):
        await self.observe()
        for method, fail_at in (
            ("known_scopes", 1),
            ("pending", 1),
            ("prepare_batch", 1),
            ("list_own", 1),
            ("pending", 2),
            ("defer", 1),
        ):
            with self.subTest(method=method, fail_at=fail_at):
                self.service._retry_at = 0
                await self.start()
                original = getattr(self.service.store, method)
                calls = 0

                def broken(*args, **kwargs):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise sqlite3.OperationalError("offline failure")
                    return original(*args, **kwargs)

                with patch.object(self.service.store, method, side_effect=broken):
                    self.assertFalse(await self.next_batch())
                self.assertIsNone(self.service._manual_upper_id)
                self.assertEqual(self.service.summary()["manual_state"], "failed")
                self.assertEqual(self.service.summary()["last_outcome"], "unavailable")
                self.assertIsNotNone(self.service.store.prepare_batch(self.scope, "17"))
        self.bridge._ask.assert_not_awaited()

    async def test_control_file_is_consumed_once_without_running_a_model_immediately(
        self,
    ):
        await self.observe()
        self.bridge._write_runtime_status = Mock()
        self.control_request()
        await self.service.poll_control(self.bridge)
        self.assertFalse(self.service._control_path.exists())
        self.assertFalse(self.service._control_processing.exists())
        self.assertIsNotNone(self.service._manual_upper_id)
        await self.service.poll_control(self.bridge)
        self.bridge._memory_idle_status.assert_awaited_once()
        self.bridge._ask.assert_not_awaited()
        self.bridge._write_runtime_status.assert_called_once_with("running")

    async def test_qq_stop_rejects_older_local_start_but_accepts_a_new_request(self):
        await self.observe()
        now = time.time() + 0.01
        with patch.object(MEMORY.time, "time", return_value=now) as clock:
            self.control_request()
            clock.return_value = now + 1
            await self.service.command(
                self.bridge, self.event, "/整理记忆 停止", master=True
            )
            await self.service.poll_control(self.bridge)
            self.assertIsNone(self.service._manual_upper_id)
            self.bridge._memory_idle_status.assert_not_awaited()
            clock.return_value = now + 2
            self.control_request(request_id="b" * 32)
            await self.service.poll_control(self.bridge)
            self.assertIsNotNone(self.service._manual_upper_id)
            self.bridge._memory_idle_status.assert_awaited_once()

    async def test_new_dialogue_rejects_older_local_start_but_accepts_a_new_request(
        self,
    ):
        await self.observe()
        now = time.time() + 0.01
        with patch.object(MEMORY.time, "time", return_value=now) as clock:
            self.control_request()
            clock.return_value = now + 1
            self.service.note_dialogue()
            await self.service.poll_control(self.bridge)
            self.assertIsNone(self.service._manual_upper_id)
            self.bridge._memory_idle_status.assert_not_awaited()
            clock.return_value = now + 2
            self.control_request(request_id="b" * 32)
            await self.service.poll_control(self.bridge)
            self.assertIsNotNone(self.service._manual_upper_id)
            self.bridge._memory_idle_status.assert_awaited_once()

    async def test_new_qq_start_rejects_older_local_stop_but_accepts_a_new_stop(self):
        await self.observe()
        now = time.time() + 0.01
        with patch.object(MEMORY.time, "time", return_value=now) as clock:
            self.control_request(action="stop")
            clock.return_value = now + 1
            await self.start()
            captured = self.service._manual_upper_id
            self.assertIsNotNone(captured)
            await self.service.poll_control(self.bridge)
            self.assertEqual(self.service._manual_upper_id, captured)
            self.assertEqual(self.service.summary()["manual_state"], "waiting")
            clock.return_value = now + 2
            self.control_request(action="stop", request_id="b" * 32)
            await self.service.poll_control(self.bridge)
            self.assertIsNone(self.service._manual_upper_id)
            self.assertEqual(self.service.summary()["manual_state"], "stopped")

    async def test_expired_future_malformed_or_foreign_control_never_starts(self):
        await self.observe()
        invalid = (
            {"requested_at": time.time() - 601},
            {"requested_at": time.time() + 6},
            {"requested_at": True},
            {"requested_at": float("nan")},
            {"requested_at": 10**400},
            {"action": "execute"},
            {"version": True},
            {"request_id": "wrong"},
            {"extra": "do not execute"},
        )
        for fields in invalid:
            with self.subTest(fields=fields):
                self.control_request(**fields)
                await self.service.poll_control(self.bridge)
                self.assertIsNone(self.service._manual_upper_id)
                self.assertFalse(self.service._control_path.exists())
                self.assertFalse(self.service._control_processing.exists())
        self.service._control_path.write_text("not JSON")
        await self.service.poll_control(self.bridge)
        self.bridge._memory_idle_status.assert_not_awaited()
        self.bridge._ask.assert_not_awaited()

    async def test_reload_does_not_replay_pending_or_already_claimed_requests(self):
        await self.observe()
        request = self.control_request()
        self.service._control_processing.write_text(
            json.dumps(request), encoding="utf-8"
        )
        await self.service.close()
        # Model a later reload explicitly rather than relying on the host timer resolution.
        with patch.object(
            MEMORY.time, "time", return_value=request["requested_at"] + 1
        ):
            self.service = MEMORY.MemoryService(self.path, logger=Mock())
        await self.service.poll_control(self.bridge)
        self.assertIsNone(self.service._manual_upper_id)
        self.assertFalse(self.service._control_path.exists())
        self.assertFalse(self.service._control_processing.exists())
        self.bridge._memory_idle_status.assert_not_awaited()

    async def test_receiving_messages_stores_local_classification_without_model(self):
        cases = (
            ("我喜欢喝茶。", "chat", "preference", True),
            ("我计划下个月学习Python。", "chat", "plan", True),
            ("今天吃什么？", "question", "other", True),
            ("/记忆", "command", "other", False),
            ("字" * 1001, "oversize", "other", False),
        )
        for index, (text, _kind, _hint, _eligible) in enumerate(cases, 1):
            await self.observe(text, event=Event(message=str(index)))
        picture = Event(message="6")
        picture.get_messages = lambda: [{"type": "image"}]
        await self.observe("", event=picture)
        self.bridge.persona = "角色"
        await self.observe("我是故事里的船长。", event=Event(message="7"))
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute(
                "SELECT message_kind,topic_hint,eligible FROM messages ORDER BY id"
            ).fetchall()
        self.assertEqual(
            rows,
            [(kind, hint, int(eligible)) for _, kind, hint, eligible in cases]
            + [("attachment", "other", 0), ("roleplay", "other", 0)],
        )
        self.bridge._ask.assert_not_awaited()
        self.bridge._memory_idle_status.assert_not_awaited()
