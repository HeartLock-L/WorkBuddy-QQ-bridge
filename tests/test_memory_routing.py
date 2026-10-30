"""Exercise memory wiring through the real plugin with offline adapter stubs."""

import asyncio
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN
from selfcheck.support import load_plugin, make_event_type


class MemoryRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Use real services and SQLite while replacing only external transports."""

    @classmethod
    def setUpClass(cls):
        cls.module = load_plugin(PLUGIN)
        cls.Event = make_event_type(cls.module)

    @classmethod
    def tearDownClass(cls):
        cls.module._selfcheck_meme_temp.cleanup()

    def setUp(self):
        # A live bot may own an existing database; all test operations use a separate path.
        self._real_db_preexisting = PLUGIN.with_name("memory.sqlite3").exists()
        self.temporary = tempfile.TemporaryDirectory(prefix="qqbot-memory-routing-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = {
            "image_gen_enabled": False,
            "web_search_enabled": False,
            "meme_enabled": False,
            "keyword_bubble_enabled": False,
            "idle_chat_enabled": False,
            "hot_reload_enabled": False,
            "memory_enabled": True,
            "memory_auto_extract": True,
        }
        self.bridge = self.module.Main(None, config=self.config)
        self.bridge._questions = self.module.request_queue.RequestQueue(
            self.root / "question_run.json", logger=Mock()
        )
        self.bridge._memory = self.module.memory_service.MemoryService(
            self.root / "memory.sqlite3", logger=Mock()
        )
        self.bridge._write_runtime_status = Mock()
        self.bridge._ask_gateway = AsyncMock(return_value="茶已经泡好了")
        self.bridge._send_to_session = AsyncMock(return_value=True)
        self.bot_id = self.module.EXPECTED_SELF_ID or "900001"
        self.bridge._account_ok = True
        self.sequence = 1000
        self.assertEqual(
            PLUGIN.with_name("memory.sqlite3").exists(),
            self._real_db_preexisting,
            "初始化改动了真实记忆库的存在状态",
        )

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.assertEqual(
            PLUGIN.with_name("memory.sqlite3").exists(),
            self._real_db_preexisting,
            "测试收尾改动了真实记忆库的存在状态",
        )

    def event(self, text, *, group="routing-group", sender="10001"):
        self.sequence += 1
        event = self.Event(
            self.bot_id,
            [self.module.At(qq=self.bot_id), self.module.Plain(text)],
            group_id=group,
            message_id=str(self.sequence),
        )
        event.sender = sender
        return event

    def remember(self, event, text, *, key="饮茶", group_name="饮食偏好"):
        return self.bridge._memory.store.remember(
            self.module.memory_service.scope_of(event),
            event.get_sender_id(),
            text,
            key=key,
            name=event.get_sender_name(),
            group_name=group_name,
        )

    async def deliver(self, event):
        return [item async for item in self.bridge.on_message(event)]

    def reply_text(self, replies):
        return "\n".join(
            item[1]
            if item[0] == "plain"
            else "".join(getattr(component, "text", "") for component in item[1])
            for item in replies
        )

    async def test_regular_question_uses_current_member_and_group_before_normal_queue(
        self,
    ):
        event = self.event("我喜欢喝什么茶")
        self.remember(event, "我喜欢喝普洱茶")
        self.remember(self.event("", sender="10002"), "我喜欢喝乌龙茶")
        self.remember(self.event("", group="other-group"), "我喜欢喝龙井茶")
        replies = await self.deliver(event)
        self.assertEqual(self.reply_text(replies), "茶已经泡好了")
        self.assertEqual(replies[0][1][0].id, event.message_id)
        self.bridge._ask_gateway.assert_awaited_once()
        call = self.bridge._ask_gateway.call_args
        self.assertIn("我喜欢喝普洱茶", call.args[0])
        self.assertNotIn("我喜欢喝乌龙茶", call.args[0])
        self.assertNotIn("我喜欢喝龙井茶", call.args[0])
        self.assertFalse(call.kwargs["master"])
        self.assertEqual(call.kwargs["mode"], "chat")
        self.assertFalse(self.bridge._questions.busy)

    async def test_unmentioned_chat_is_collected_then_only_manually_consolidated(
        self,
    ):
        self.config["silent_for_others"] = False
        self.config["group_require_at"] = True
        text = "我平时喜欢喝不加糖的热咖啡。"
        event = self.Event(
            self.bot_id,
            [self.module.Plain(text)],
            group_id="routing-group",
            message_id="3001",
        )
        event.message_obj = types.SimpleNamespace(
            raw_message={"post_type": "message", "time": time.time() - 121}
        )
        self.assertEqual([], await self.deliver(event))
        self.bridge._ask_gateway.assert_not_awaited()

        def extract(prompt, *_args, **_kwargs):
            material = json.loads(prompt.rsplit("\n", 1)[-1])
            source = material["原话"][0]
            return json.dumps(
                {
                    "proof": material["proof"],
                    "items": [
                        {
                            "source": source["ref"],
                            "quote": source["text"],
                            "category": "preference",
                            "key": "咖啡偏好",
                            "content": source["text"],
                            "group": "饮食偏好",
                        }
                    ],
                },
                ensure_ascii=False,
            )

        self.bridge._ask = AsyncMock(side_effect=extract)
        self.assertFalse(await self.bridge._memory.run_once(self.bridge))
        self.bridge._ask.assert_not_awaited()
        self.bridge._memory_idle_status = AsyncMock(return_value="ready")
        denied = await self.bridge._process_commands(
            event, "/整理记忆 开始", event.get_sender_id(), False
        )
        self.assertTrue(denied[0])
        self.assertIsNone(self.bridge._memory._manual_upper_id)
        started = await self.bridge._process_commands(
            event, "/整理记忆 开始", event.get_sender_id(), True
        )
        self.assertTrue(started[0])
        self.assertTrue(await self.bridge._memory.run_once(self.bridge))
        scope = self.module.memory_service.scope_of(event)
        memories = self.bridge._memory.store.list_own(scope, event.get_sender_id())
        self.assertEqual([text], [item.content for item in memories])
        self.bridge._send_to_session.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_explicit_memory_command_never_enters_computer_approval_or_gateway(
        self,
    ):
        self.bridge._process_approval = AsyncMock(
            side_effect=AssertionError("approval")
        )
        replies = await self.deliver(self.event("记住我不吃香菜"))
        self.assertIn("记住了", self.reply_text(replies))
        self.bridge._process_approval.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()
        self.assertEqual(
            len(
                self.bridge._memory.store.list_own(
                    self.module.memory_service.scope_of(self.event("")), "10001"
                )
            ),
            1,
        )

    async def test_group_catalog_and_details_route_without_model_or_computer_approval(
        self,
    ):
        event = self.event("")
        saved = self.remember(event, "我喜欢喝普洱茶")
        self.remember(event, "我不吃香菜", key="香菜")
        self.remember(
            self.event("", sender="10002"),
            "我喜欢游泳",
            key="游泳",
            group_name="运动偏好",
        )
        self.remember(self.event("", group="other-group"), "我喜欢喝龙井茶")
        self.bridge._process_approval = AsyncMock(
            side_effect=AssertionError("approval")
        )
        groups = self.bridge._memory.store.list_groups(
            self.module.memory_service.scope_of(event), event.get_sender_id()
        )
        catalog = self.reply_text(await self.deliver(self.event("/记忆")))
        self.assertIn("饮食偏好", catalog)
        self.assertIn(groups[0].intro, catalog)
        self.assertNotIn("运动偏好", catalog)
        self.assertNotIn("龙井茶", catalog)
        detail = self.reply_text(await self.deliver(self.event("/记忆 饮食偏好")))
        self.assertIn(str(saved.id), detail)
        self.assertIn("普洱茶", detail)
        self.assertIn("香菜", detail)
        self.assertNotIn("游泳", detail)
        self.assertNotIn("龙井茶", detail)
        self.bridge._process_approval.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_group_intro_drops_forgotten_fact_through_real_command_route(self):
        event = self.event("")
        saved = self.remember(event, "我喜欢喝普洱茶")
        await self.deliver(self.event(f"/更正记忆 {saved.id} 我现在只喝红茶"))
        catalog = self.reply_text(await self.deliver(self.event("/记忆")))
        detail = self.reply_text(await self.deliver(self.event("/记忆 饮食偏好")))
        self.assertNotIn("普洱茶", catalog + detail)
        self.assertIn("红茶", detail)
        await self.deliver(self.event(f"/忘记记忆 {saved.id}"))
        scope = self.module.memory_service.scope_of(event)
        self.assertEqual(
            [], self.bridge._memory.store.list_groups(scope, event.get_sender_id())
        )
        catalog = self.reply_text(await self.deliver(self.event("/记忆")))
        detail = self.reply_text(await self.deliver(self.event("/记忆 饮食偏好")))
        self.assertNotIn("红茶", catalog + detail)
        self.assertNotIn("普洱茶", catalog + detail)
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_catalog_and_detail_page_commands_expose_older_ids_without_gateway(
        self,
    ):
        event = self.event("")
        oldest = None
        for index in range(4):
            item = self.remember(event, f"我喜欢第{index}种茶。", key=f"饮茶{index}")
            oldest = oldest or item
        for index in range(13):
            self.remember(
                event,
                f"我喜欢研究第{index}种植物。",
                key=f"植物{index}",
                group_name=f"观察专题{index:02d}",
            )
        self.remember(self.event("", sender="10002"), "我喜欢喝其他成员的茶。")
        self.bridge._process_approval = AsyncMock(
            side_effect=AssertionError("approval")
        )
        first = self.reply_text(await self.deliver(self.event("/记忆")))
        self.assertIn("下一页：/记忆 目录 2", first)
        catalog = self.reply_text(await self.deliver(self.event("/记忆 目录 2")))
        self.assertIn("第 2/2 页", catalog)
        self.assertIn("饮食偏好", catalog)
        detail = self.reply_text(await self.deliver(self.event("/记忆 饮食偏好 2")))
        all_records = self.reply_text(await self.deliver(self.event("/记忆 全部 6")))
        self.assertIn(f"{oldest.id}：{oldest.content}", detail)
        self.assertIn(f"{oldest.id}：{oldest.content}", all_records)
        self.assertNotIn("其他成员的茶", catalog + detail + all_records)
        forgotten = await self.deliver(self.event(f"/忘记记忆 {oldest.id}"))
        self.assertIn("已忘记", self.reply_text(forgotten))
        after = self.reply_text(await self.deliver(self.event("/记忆 饮食偏好 2")))
        self.assertIn("页码应在 1 到 1 之间", after)
        self.assertNotIn(oldest.content, after)
        self.bridge._process_approval.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_correction_and_forgetting_commands_take_effect_in_next_question(
        self,
    ):
        event = self.event("")
        memory = self.remember(event, "我喜欢喝普洱茶")
        corrected = await self.deliver(
            self.event(f"/更正记忆 {memory.id} 我喜欢喝红茶")
        )
        self.assertIn("已更正", self.reply_text(corrected))
        await self.deliver(self.event("我喜欢喝什么茶"))
        prompt = self.bridge._ask_gateway.call_args.args[0]
        self.assertIn("我喜欢喝红茶", prompt)
        self.assertNotIn("我喜欢喝普洱茶", prompt)
        forgotten = await self.deliver(self.event(f"/忘记记忆 {memory.id}"))
        self.assertIn("已忘记", self.reply_text(forgotten))
        await self.deliver(self.event("我喜欢喝什么茶"))
        # Recent chat may contain commands, but deleted memory is never injected.
        prompt = self.bridge._ask_gateway.call_args.args[0]
        self.assertNotIn("【当前会话的长期记忆资料】", prompt)

    async def test_unattributed_origin_cannot_inject_stored_memory(self):
        event = self.event("我喜欢喝什么茶")
        self.remember(event, "我喜欢喝普洱茶")
        event.unified_msg_origin = "aiocqhttp:GroupMessage:other-group"
        await self.deliver(event)
        self.assertNotIn("我喜欢喝普洱茶", self.bridge._ask_gateway.call_args.args[0])

    async def test_disabled_memory_keeps_normal_question_working_without_retrieval(
        self,
    ):
        event = self.event("我喜欢喝什么茶")
        self.remember(event, "我喜欢喝普洱茶")
        self.config["memory_enabled"] = False
        self.bridge._memory.context = AsyncMock(side_effect=AssertionError("retrieval"))
        self.bridge._memory.observe = AsyncMock(side_effect=AssertionError("recording"))
        replies = await self.deliver(event)
        self.assertEqual(self.reply_text(replies), "茶已经泡好了")
        self.bridge._memory.context.assert_not_awaited()
        self.bridge._memory.observe.assert_not_awaited()

    async def test_database_failure_does_not_prevent_normal_answer(self):
        # A directory cannot be opened as SQLite; all IO is still inside this fixture.
        self.bridge._memory = self.module.memory_service.MemoryService(
            self.root, logger=Mock()
        )
        replies = await self.deliver(self.event("我喜欢喝什么茶"))
        self.assertEqual(self.reply_text(replies), "茶已经泡好了")
        self.bridge._ask_gateway.assert_awaited_once()

    async def test_recalled_memory_does_not_bypass_existing_question_queue(self):
        event = self.event("我喜欢喝什么茶")
        self.remember(event, "我喜欢喝普洱茶")
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        pending = asyncio.create_task(self.deliver(event))

        async def wait_until_queued():
            while self.bridge._questions.waiting_count == 0:
                await asyncio.sleep(0.001)

        try:
            await asyncio.wait_for(wait_until_queued(), 2)
            self.bridge._ask_gateway.assert_not_awaited()
            self.assertFalse(pending.done())
        finally:
            self.bridge._questions.release()
        replies = await asyncio.wait_for(pending, 2)
        self.assertEqual(self.reply_text(replies), "茶已经泡好了")
        self.bridge._ask_gateway.assert_awaited_once()

    async def test_proactive_reply_recalls_only_relevant_current_group_memory(self):
        event = self.event("")
        self.remember(event, "我喜欢喝普洱茶")
        self.remember(self.event("", group="other-group"), "我喜欢喝普洱茶和龙井茶")
        self.bridge._note_group_activity(event)
        result = await self.bridge._speak_spark(
            "routing-group",
            event.unified_msg_origin,
            "接一句",
            "test",
            meme_context="聊聊普洱茶",
        )
        self.assertEqual(result, (True, "茶已经泡好了"))
        call = self.bridge._ask_gateway.call_args
        self.assertIn("我喜欢喝普洱茶", call.args[0])
        self.assertNotIn("我喜欢喝龙井茶", call.args[0])
        self.assertTrue(call.kwargs["background"])
        self.assertGreater(call.kwargs["not_after"], time.monotonic())
        self.bridge._send_to_session.assert_awaited_once_with(
            event.unified_msg_origin, "茶已经泡好了"
        )

    async def test_expired_or_busy_proactive_request_does_not_read_memory_or_generate(
        self,
    ):
        self.bridge._memory.group_context = AsyncMock(
            side_effect=AssertionError("read")
        )
        result = await self.bridge._speak_spark(
            "routing-group",
            "aiocqhttp:GroupMessage:routing-group",
            "接一句",
            "test",
            created_at=time.monotonic() - 60,
        )
        self.assertIsNone(result)
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        try:
            result = await self.bridge._speak_spark(
                "routing-group",
                "aiocqhttp:GroupMessage:routing-group",
                "接一句",
                "test",
            )
            self.assertIsNone(result)
        finally:
            self.bridge._questions.release()
        self.bridge._memory.group_context.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_new_group_message_during_memory_read_prevents_stale_proactive_delivery(
        self,
    ):
        event = self.event("")
        self.bridge._note_group_activity(event)

        async def slow_read(*_args):
            self.bridge._note_group_activity(self.event("新消息"))
            return "一条旧记忆"

        self.bridge._memory.group_context = AsyncMock(side_effect=slow_read)
        result = await self.bridge._speak_spark(
            "routing-group", event.unified_msg_origin, "接一句", "test"
        )
        self.assertIsNone(result)
        self.bridge._send_to_session.assert_not_awaited()

    async def test_plain_group_chat_records_without_renewing_roleplay_or_dialogue(self):
        self.config.update(
            silent_for_others=False, group_require_at=True, roleplay_timeout_sec=60
        )
        started = time.time() - 30
        self.bridge._roleplay["routing-group"] = {"persona": "虚构角色", "ts": started}
        self.bridge._memory._last_dialogue_at -= 121
        before = self.bridge._memory._last_dialogue_at
        event = self.Event(
            self.bot_id,
            [self.module.Plain("我在演一个角色。")],
            group_id="routing-group",
            message_id="7001",
        )
        self.assertEqual([], await self.deliver(event))
        self.assertEqual(self.bridge._roleplay["routing-group"]["ts"], started)
        self.assertEqual(self.bridge._memory._last_dialogue_at, before)
        scope = self.module.memory_service.scope_of(event)
        self.assertIsNone(
            self.bridge._memory.store.prepare_batch(scope, event.get_sender_id())
        )
        self.assertEqual(
            self.bridge._memory.store.group_overview(scope).member_count, 1
        )
        with patch.object(self.module.time, "time", return_value=started + 61):
            self.assertEqual(self.bridge._current_persona(event, touch=False), "")
        self.assertNotIn("routing-group", self.bridge._roleplay)
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_foreground_question_resets_idle_window_but_background_request_does_not(
        self,
    ):
        self.bridge._memory._last_dialogue_at -= 121
        old = self.bridge._memory._last_dialogue_at
        await self.deliver(self.event("今天想聊聊咖啡口味"))
        self.assertGreater(self.bridge._memory._last_dialogue_at, old + 100)
        foreground_end = self.bridge._memory._last_dialogue_at
        self.assertEqual(
            await self.bridge._ask("offline background", "offline", background=True),
            "茶已经泡好了",
        )
        self.assertEqual(self.bridge._memory._last_dialogue_at, foreground_end)
        self.bridge._send_to_session.assert_not_awaited()

    async def test_group_parent_directory_routes_without_exposing_another_member_child(
        self,
    ):
        event = self.event("")
        other = self.event("", sender="10002")
        self.remember(other, "我个人喜欢喝乌龙茶。")
        store = self.bridge._memory.store
        scope = self.module.memory_service.scope_of(event)
        first, second = (
            "我们群每周六一起打羽毛球。",
            "我们群每周六一起打羽毛球，我同意。",
        )
        store.record(scope, event.sender, "甲", "7001", first, now=time.time() - 10)
        store.record(scope, other.sender, "乙", "7002", second, now=time.time() - 9)
        batch = store.prepare_batch(scope, event.sender)
        a, b = batch.messages[0], batch.context[0]
        self.assertEqual(
            store.commit_batch(
                batch,
                {
                    "proof": batch.proof,
                    "items": [],
                    "group_items": [
                        {
                            "sources": [a.ref, b.ref],
                            "quotes": [a.text, b.text],
                            "category": "fact",
                            "key": "周末羽毛球",
                            "content": first,
                            "group": "共同活动",
                        }
                    ],
                },
            ),
            1,
        )
        self.bridge._process_approval = AsyncMock(
            side_effect=AssertionError("approval")
        )
        catalog = self.reply_text(await self.deliver(self.event("/群记忆")))
        details = self.reply_text(await self.deliver(self.event("/群记忆 共同活动")))
        self.assertIn("共同活动", catalog)
        self.assertIn("成员独立记忆", catalog)
        self.assertNotIn("乌龙茶", catalog + details)
        self.assertIn(first, details)
        common = store.list_own(scope, self.module.memory_service.GROUP_SUBJECT)[0]
        denied = self.reply_text(
            await self.deliver(self.event(f"/忘记记忆 {common.id}"))
        )
        self.assertIn("没有找到", denied)
        self.assertEqual(store.group_overview(scope).group_count, 1)
        self.bridge._process_approval.assert_not_awaited()
        self.bridge._ask_gateway.assert_not_awaited()

    async def test_initialization_owns_one_memory_worker_and_unload_joins_it(self):
        async def parked():
            await asyncio.Event().wait()

        self.bridge._draw_recovery_loop = parked
        self.bridge._maintenance_loop = parked
        self.bridge._runtime_status_loop = parked
        self.assertFalse((self.root / "memory.sqlite3").exists())
        await self.bridge.initialize()
        first = self.bridge._memory_task
        await self.bridge.initialize()
        self.assertIs(first, self.bridge._memory_task)
        self.assertIn(first, self.bridge._background_tasks)
        await asyncio.sleep(0)
        await self.bridge.terminate()
        self.assertTrue(first.done())
        self.assertTrue(self.bridge._memory._closed)
        self.assertFalse(self.bridge._memory._io_tasks)
        self.assertFalse(self.bridge._background_tasks)
        self.assertIsNone(self.bridge._memory_task)
        self.assertFalse((self.root / "memory.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
