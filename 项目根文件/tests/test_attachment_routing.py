"""Check attachment metadata and failures through actual message-routing methods."""

import asyncio
import unittest
from unittest.mock import AsyncMock

from artifact_test_support import ATTACHMENTS, ArtifactFixture, Event


class AttachmentEvent(Event):
    def __init__(self, group, text, sender="owner"):
        super().__init__(group)
        self.text = text
        self.sender = sender

    def get_message_str(self):
        return self.text

    def get_sender_id(self):
        return self.sender

    def get_sender_name(self):
        return "operator" if self.sender == "same-user" else "requester"


class AttachmentRoutingTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.globals.update(
            WEB_SEARCH_ENABLED=False,
            REDIRECT_ON_CONFIG_TOPIC=False,
            STRICT_FOR_OTHERS=False,
            REJECT_WORDS=(),
            _is_action_request=lambda text: (
                "new file" if text.startswith("新建") else None
            ),
        )
        self.bridge._spark_said = {}
        self.bridge._follow_chain = {}
        self.bridge._group_context_block = lambda *args: ""
        self.bridge._current_persona = lambda event: "fixture persona"
        self.bridge._ask = AsyncMock(return_value="fixture answer")

    def batch(self, group, *, failed=False, partial=False):
        batch = ATTACHMENTS.AttachmentBatch()
        if not failed:
            image = self.root / f"{group}.gif"
            preview = self.root / f"{group}-frames.jpg"
            image.write_bytes(b"GIF89a fixture")
            preview.write_bytes(b"preview fixture")
            batch.extend([str(image), str(preview)])
            batch.notes.update(
                {str(image): f"author-{group}", str(preview): f"author-{group}"}
            )
            batch.descriptions[str(image)] = f"original-{group} animation"
            batch.descriptions[str(preview)] = (
                f"sampled-{group}: frames 1, 5, 9; not every frame"
            )
        if failed or partial:
            batch.notices.append("单个附件不能超过 10 MB，请压缩后重发。")
        return batch

    async def dispatch(self, event):
        return [reply async for reply in self.bridge.on_message(event)]

    def assert_own_metadata(self, prompt, batch, own, other):
        for path in batch:
            self.assertIn(path, prompt)
        self.assertIn(f"author-{own}", prompt)
        self.assertIn(f"original-{own} animation", prompt)
        self.assertIn(f"sampled-{own}: frames 1, 5, 9; not every frame", prompt)
        self.assertNotIn(f"author-{other}", prompt)
        self.assertNotIn(f"sampled-{other}", prompt)
        self.assertNotIn(str(self.root / f"{other}.gif"), prompt)

    async def test_partial_failure_notifies_then_answers_using_remaining_resources(
        self,
    ):
        batch = self.batch("group-a", partial=True)
        self.bridge._collect_attachments.return_value = batch
        event = AttachmentEvent("group-a", "看看附件里的动作")
        replies = await self.dispatch(event)
        self.assertEqual(replies, [batch.notices[0], "fixture answer"])
        self.assertTrue(event.stopped)
        self.bridge._ask.assert_awaited_once()
        prompt, conversation = self.bridge._ask.await_args.args
        self.assertEqual(conversation, "group-a")
        self.assert_own_metadata(prompt, batch, "group-a", "group-b")
        self.assertEqual(self.bridge._pending, {})

    async def test_all_failed_attachments_stop_before_question_or_approval(self):
        batch = self.batch("group-a", failed=True)
        self.bridge._collect_attachments.return_value = batch
        process_approval = AsyncMock()
        self.bridge._process_approval = process_approval
        for text, sender in (
            ("看看附件", "owner"),
            ("新建这个附件的副本", "owner"),
            ("/执行 创建附件副本", "same-user"),
        ):
            with self.subTest(text=text):
                event = AttachmentEvent("group-a", text, sender)
                self.assertEqual(await self.dispatch(event), batch.notices)
                self.assertTrue(event.stopped)
                self.assertEqual(self.bridge._pending, {})
        process_approval.assert_not_awaited()
        self.bridge._ask.assert_not_awaited()

    async def test_unexpected_collector_failure_stops_without_internal_details(self):
        self.bridge._collect_attachments.side_effect = RuntimeError(
            "fixture internal path C:/private/attachment and debug details"
        )
        process_approval = AsyncMock()
        self.bridge._process_approval = process_approval
        for text, sender in (
            ("看看附件", "owner"),
            ("新建这个附件的副本", "owner"),
            ("/执行 创建附件副本", "same-user"),
        ):
            with self.subTest(text=text):
                event = AttachmentEvent("group-a", text, sender)
                replies = await self.dispatch(event)
                self.assertEqual(len(replies), 1)
                self.assertIn("附件", replies[0])
                self.assertNotIn("fixture", replies[0])
                self.assertNotIn("private", replies[0])
                self.assertNotIn("RuntimeError", replies[0])
                self.assertTrue(event.stopped)
                self.assertEqual(self.bridge._pending, {})
        process_approval.assert_not_awaited()
        self.bridge._ask.assert_not_awaited()

    async def test_concurrent_questions_keep_each_batch_in_its_actual_prompt(self):
        batches = {group: self.batch(group) for group in ("group-a", "group-b")}
        first_collecting = asyncio.Event()
        second_collected = asyncio.Event()
        both_asking = asyncio.Event()
        captured = {}

        async def collect(event, *args, **kwargs):
            if event.group == "group-a":
                first_collecting.set()
                await second_collected.wait()
            else:
                await first_collecting.wait()
                second_collected.set()
            return batches[event.group]

        async def ask(prompt, conversation, **kwargs):
            captured[conversation] = prompt
            if len(captured) == 2:
                both_asking.set()
            await both_asking.wait()
            return f"answer-{conversation}"

        self.bridge._collect_attachments.side_effect = collect
        self.bridge._ask.side_effect = ask
        events = [AttachmentEvent(group, "看看这张动图") for group in batches]
        replies = await asyncio.wait_for(
            asyncio.gather(*(self.dispatch(event) for event in events)), 2
        )
        self.assertEqual(replies, [["answer-group-a"], ["answer-group-b"]])
        self.assert_own_metadata(
            captured["group-a"], batches["group-a"], "group-a", "group-b"
        )
        self.assert_own_metadata(
            captured["group-b"], batches["group-b"], "group-b", "group-a"
        )
        self.assertIn("本条来自群 group-a", captured["group-a"])
        self.assertIn("本条来自群 group-b", captured["group-b"])

    async def test_pending_approvals_keep_authors_and_gif_scope_across_groups(self):
        batches = {
            group: self.batch(group, partial=group == "group-a")
            for group in ("group-a", "group-b")
        }

        async def collect(event, *args, **kwargs):
            return (
                batches[event.group]
                if event.sender == "owner"
                else ATTACHMENTS.AttachmentBatch()
            )

        self.bridge._collect_attachments.side_effect = collect
        requests = [AttachmentEvent(group, "新建附件分析结果") for group in batches]
        replies = await asyncio.gather(*(self.dispatch(event) for event in requests))
        self.assertEqual(replies[0][0], batches["group-a"].notices[0])
        for event in requests:
            pending = self.bridge._pending[event.unified_msg_origin]
            self.assertEqual(len(pending), 1)
            self.assertIs(pending[0]["attachments"], batches[event.group])
        self.bridge._ask.assert_not_awaited()
        captured = {}
        both_executing = asyncio.Event()

        async def ask(prompt, conversation, *, master, mode, out_dir):
            self.assertTrue(master)
            self.assertEqual(mode, "execute")
            captured[conversation] = prompt
            if len(captured) == 2:
                both_executing.set()
            await both_executing.wait()
            return f"approved-{conversation}"

        self.bridge._ask.side_effect = ask
        approvals = [AttachmentEvent(group, "同意", "same-user") for group in batches]
        results = await asyncio.wait_for(
            asyncio.gather(*(self.dispatch(event) for event in approvals)), 2
        )
        self.assertEqual(results, [["approved-group-a"], ["approved-group-b"]])
        self.assertEqual(self.bridge._pending, {})
        self.assert_own_metadata(
            captured["group-a"], batches["group-a"], "group-a", "group-b"
        )
        self.assert_own_metadata(
            captured["group-b"], batches["group-b"], "group-b", "group-a"
        )
        self.bridge._send_chain_to_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
