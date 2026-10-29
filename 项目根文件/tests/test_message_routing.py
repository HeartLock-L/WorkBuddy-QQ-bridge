"""Preserve command precedence and consumed-event semantics after extraction."""

import types
import unittest
from unittest.mock import AsyncMock, Mock

from artifact_test_support import ArtifactFixture, Event
from bridge_test_support import load_definitions


class MessageCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        namespace = load_definitions(
            {"logger": Mock()}, methods={"_process_commands", "_reply_result"}
        )
        self.bridge = namespace["Bridge"]()
        self.bridge._handle_question_recovery = AsyncMock(return_value=None)
        self.bridge._handle_image_job_cmd = AsyncMock(return_value="image status")
        self.bridge._handle_reload_cmd = Mock(return_value=None)
        self.bridge._handle_relay_cmd = AsyncMock(return_value=None)
        self.bridge._handle_roleplay_cmd = Mock(return_value=None)
        self.bridge._can_send_active = Mock(return_value=True)
        self.event = types.SimpleNamespace(
            stop_event=Mock(), plain_result=Mock(side_effect=lambda text: text)
        )

    async def test_recovery_consumes_even_without_reply_and_precedes_other_commands(
        self,
    ):
        result = await self.bridge._process_commands(
            self.event, "/恢复问答", "admin", True
        )
        self.assertEqual(result, (True, []))
        self.event.stop_event.assert_called_once_with()
        self.bridge._handle_reload_cmd.assert_not_called()
        self.bridge._handle_relay_cmd.assert_not_awaited()
        self.bridge._handle_roleplay_cmd.assert_not_called()

    async def test_empty_reload_reply_is_handled_and_does_not_fall_through(self):
        self.bridge._handle_reload_cmd.return_value = ""
        result = await self.bridge._process_commands(self.event, "/重载", "admin", True)
        self.assertEqual(result, (True, [""]))
        self.event.stop_event.assert_called_once_with()
        self.bridge._handle_relay_cmd.assert_not_awaited()
        self.bridge._handle_roleplay_cmd.assert_not_called()

    async def test_suppressed_send_still_consumes_relay_command(self):
        self.bridge._can_send_active.return_value = False
        self.bridge._handle_relay_cmd.return_value = "handled"
        result = await self.bridge._process_commands(self.event, "/代发", "admin", True)
        self.assertEqual(result, (True, []))
        self.event.stop_event.assert_called_once_with()
        self.event.plain_result.assert_not_called()
        self.bridge._handle_roleplay_cmd.assert_not_called()

    async def test_regular_text_falls_through_without_stopping_event(self):
        result = await self.bridge._process_commands(
            self.event, "普通提问", "member", False
        )
        self.assertEqual(result, (False, []))
        self.event.stop_event.assert_not_called()
        self.bridge._handle_image_job_cmd.assert_not_awaited()
        self.bridge._handle_roleplay_cmd.assert_called_once_with(
            self.event, "普通提问", "member", False
        )

    async def test_image_commands_are_consumed_before_unrelated_handlers(self):
        for text in ("/生图任务", "/生图处理 abcdef12 结束"):
            with self.subTest(text=text):
                result = await self.bridge._process_commands(
                    self.event, text, "admin", True
                )
                self.assertEqual(result, (True, ["image status"]))
                self.bridge._handle_image_job_cmd.assert_awaited_with(
                    self.event, text, "admin", True
                )
        self.bridge._handle_reload_cmd.assert_not_called()
        self.bridge._handle_relay_cmd.assert_not_awaited()
        self.bridge._handle_roleplay_cmd.assert_not_called()

    async def test_image_lookalikes_fall_through(self):
        for text in ("/生图任务列表", "/生图处理一下", "解释 /生图任务", ""):
            with self.subTest(text=text):
                self.assertEqual(
                    await self.bridge._process_commands(
                        self.event, text, "member", False
                    ),
                    (False, []),
                )
        self.bridge._handle_image_job_cmd.assert_not_awaited()
        self.event.stop_event.assert_not_called()

    async def test_suppressed_image_reply_still_consumes_command(self):
        self.bridge._can_send_active.return_value = False
        result = await self.bridge._process_commands(
            self.event, "/生图任务", "member", False
        )
        self.assertEqual(result, (True, []))
        self.event.stop_event.assert_called_once_with()
        self.event.plain_result.assert_not_called()
        self.bridge._handle_reload_cmd.assert_not_called()


class MessageEntryImageCommandTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    async def test_image_query_never_falls_through_to_attachments_approval_or_model(
        self,
    ):
        event = Event("original-group")
        event.get_message_str = lambda: "/生图任务"
        self.bridge._handle_image_job_cmd = AsyncMock(return_value="saved task status")
        self.bridge._process_approval = AsyncMock(
            side_effect=AssertionError("approval forbidden")
        )
        self.bridge._ask = AsyncMock(side_effect=AssertionError("model forbidden"))
        replies = [reply async for reply in self.bridge.on_message(event)]
        self.assertEqual(replies, ["saved task status"])
        self.assertTrue(event.stopped)
        self.bridge._handle_image_job_cmd.assert_awaited_once_with(
            event, "/生图任务", "same-user", True
        )
        self.bridge._collect_attachments.assert_not_awaited()
        self.bridge._process_approval.assert_not_awaited()
        self.bridge._ask.assert_not_awaited()

    async def test_suppressed_image_command_remains_consumed_at_message_entry(self):
        event = Event("original-group")
        event.get_message_str = lambda: "/生图处理 abcdef12 结束"
        self.bridge._handle_image_job_cmd = AsyncMock(return_value="ended")
        self.bridge._can_send_active = lambda: False
        self.bridge._ask = AsyncMock(side_effect=AssertionError("model forbidden"))
        self.assertEqual([reply async for reply in self.bridge.on_message(event)], [])
        self.assertTrue(event.stopped)
        self.bridge._collect_attachments.assert_not_awaited()
        self.bridge._ask.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
