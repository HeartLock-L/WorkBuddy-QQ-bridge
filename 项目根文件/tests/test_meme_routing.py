"""Exercise optional stickers through real question and proactive reply paths."""

import asyncio
import base64
import hashlib
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from artifact_test_support import (
    MEME_COLLECTION,
    MEME_LIBRARY,
    ArtifactFixture,
    Event,
)
from bridge_test_support import load_definitions

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+aGZ0AAAAASUVORK5CYII="
)


class At:
    def __init__(self, qq):
        self.qq = qq


class Poke:
    pass


class Reply:
    def __init__(self, *, id):
        self.id = id


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    def __init__(self, *, file="", url=""):
        self.file = file
        self.url = url


class MemeEvent(Event):
    """Keep source metadata and outgoing chain construction local to one event."""

    def __init__(self, group="group-a", identifier="101", *, sticker=False):
        super().__init__(group)
        self.identifier = identifier
        self.private = False
        self.components = [At("fake-bot")]
        raw = {
            "post_type": "message",
            "message_type": "group",
            "group_id": group,
            "user_id": "same-user",
            "self_id": "fake-bot",
            "message": [],
        }
        if sticker:
            url = "https://example.invalid/sticker.png"
            self.components = [Image(url=url)]
            raw["message"] = [{"type": "image", "data": {"sub_type": 1, "url": url}}]
        self.message_obj = types.SimpleNamespace(message_id=identifier, raw_message=raw)
        self.chain_result = Mock(side_effect=lambda parts: parts)
        self.plain_result = Mock(side_effect=lambda text: [Plain(text)])

    def get_message_id(self):
        return self.identifier

    def get_messages(self):
        return self.components

    def get_message_str(self):
        return "message-" + self.group

    def is_private_chat(self):
        return self.private


class MemeRoutingTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.globals.update(
            At=At,
            Poke=Poke,
            Reply=Reply,
            Plain=Plain,
            Image=Image,
            WEB_SEARCH_ENABLED=False,
            REDIRECT_ON_CONFIG_TOPIC=False,
            STRICT_FOR_OTHERS=False,
            SILENT_NOTICE_THROTTLE_SEC=0,
            SPARK_FAIL_CIRCUIT_N=3,
            EXTRA_WAKE_PREFIXES=(),
            MASTER_EXEMPT_WAKE=False,
            _is_at_me=lambda event: any(
                isinstance(item, At) and item.qq == event.get_self_id()
                for item in event.get_messages()
            ),
            _is_reply_to_me=lambda event: False,
            _materialize_attachment=AsyncMock(
                side_effect=AssertionError("Attachment downloads are forbidden")
            ),
        )
        load_definitions(
            self.globals,
            methods={"_speak_spark"},
            functions={"_is_empty_reply", "_is_gateway_error", "_is_meta_note"},
            constants={
                "EMPTY_REPLY_MARKERS",
                "META_NOTE_PREFIXES",
                "META_NOTE_MARKERS",
                "_GATEWAY_ERROR_RE",
            },
        )
        self.bridge._speak_spark = types.MethodType(
            self.globals["Bridge"]._speak_spark, self.bridge
        )
        self.bridge._spark_said = {}
        self.bridge._follow_chain = {}
        self.bridge._silent_notice = {}
        self.bridge._group_activity = {}
        self.bridge._questions = types.SimpleNamespace(busy=False)
        self.bridge._group_context_block = lambda *args: ""
        self.bridge._current_persona = lambda event: ""
        self.bridge._take_image_wait = lambda event: None
        self.bridge._maybe_keyword_bubble = Mock()
        self.options = {}
        self.bridge._cfg_bool = lambda key, default: self.options.get(key, default)
        self.library_root = self.root / "memes"
        self.library_root.mkdir()
        self.clock = 1000.0
        clock_patch = patch.object(
            MEME_LIBRARY,
            "time",
            types.SimpleNamespace(monotonic=lambda: self.clock, time=time.time),
        )
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.offers = {}
        self.bridge._memes = MEME_LIBRARY.MemeLibrary(self.library_root)
        original_offer = self.bridge._memes.offer
        original_take = self.bridge._memes.take
        original_learn = self.bridge._memes.learn

        def offer(scope, **kwargs):
            result = original_offer(scope, chance_percent=100, now=self.clock, **kwargs)
            self.offers[scope] = result
            return result

        self.bridge._memes.offer = Mock(side_effect=offer)
        self.bridge._memes.take = Mock(
            side_effect=lambda *args, **kwargs: original_take(
                *args, now=self.clock, **kwargs
            )
        )
        self.bridge._memes.learn = Mock(wraps=original_learn)

    def seed(self, event):
        """Write one real, uniquely fingerprinted image owned by this group."""
        scope = event.unified_msg_origin
        data = PNG + scope.encode()
        folder = (
            self.library_root
            / "自动收集"
            / hashlib.sha256(scope.encode()).hexdigest()[:16]
        )
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (hashlib.sha256(data).hexdigest() + ".png")
        path.write_bytes(data)
        return str(path)

    def selected_reply(self, event, text="answer"):
        token = self.offers[event.unified_msg_origin].candidates[0].token
        return text + " [MEME:" + token + "]"

    def named_reply(self, event, text="answer", *, select=False):
        token = self.offers[event.unified_msg_origin].candidates[0].token
        naming = f"[MEME_NAME:{token}|猫猫笑哭|开心,笑哭]"
        return (self.selected_reply(event, text) if select else text) + " " + naming

    async def answer(self, event, *, config_topic=None):
        return [
            result
            async for result in self.bridge._answer_question(
                event,
                event.get_message_str(),
                attachments=[],
                follow_img=None,
                conv_id=event.group,
                master=True,
                sender_id=event.get_sender_id(),
                self_id=event.get_self_id(),
                where=event.group,
                config_topic=config_topic,
                umo=event.unified_msg_origin,
            )
        ]

    async def spark(self, event, **kwargs):
        kwargs.setdefault("meme_context", "本群正在聊猫猫笑哭")
        return await self.bridge._speak_spark(
            event.group,
            event.unified_msg_origin,
            "continue the conversation",
            "test",
            **kwargs,
        )

    def assert_unconsumed(self):
        self.assertEqual(self.bridge._memes._group_last, {})
        self.assertEqual(self.bridge._memes._asset_last, {})
        self.assertIsNone(self.bridge._memes._last_global)

    async def test_question_uses_one_model_call_and_one_quoted_image_chain(self):
        event = MemeEvent()
        path = self.seed(event)
        self.bridge._group_context_block = lambda *args: "本群正在聊猫猫笑哭"
        self.bridge._gag_history = {
            event.group: [{"text": "本群正在聊猫猫笑哭"}],
            "other-group": [{"text": "别群素材不应进入召回"}],
        }
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event)
        )
        results = await self.answer(event)
        self.bridge._ask.assert_awaited_once()
        prompt, conv_id = self.bridge._ask.call_args.args
        self.assertEqual(conv_id, event.group)
        self.assertIn(self.offers[event.unified_msg_origin].prompt, prompt)
        retrieval_context = self.bridge._memes.offer.call_args.kwargs["context"]
        self.assertIn(event.get_message_str(), retrieval_context)
        self.assertIn("本群正在聊猫猫笑哭", retrieval_context)
        self.assertNotIn("别群素材不应进入召回", retrieval_context)
        self.assertEqual(len(results), 1)
        chain = results[0]
        self.assertEqual([type(item) for item in chain], [Reply, Plain, Image])
        self.assertEqual(chain[0].id, event.identifier)
        self.assertEqual(chain[1].text, "answer")
        self.assertEqual(chain[2].file, path)
        self.assertEqual(self.bridge._spark_said[event.group][0]["text"], "answer")
        self.bridge._send_to_session.assert_not_awaited()
        self.bridge._send_chain_to_session.assert_not_awaited()

    async def test_concurrent_groups_keep_their_offers_reply_ids_and_images(self):
        first = MemeEvent("group-a", "101")
        second = MemeEvent("group-b", "202")
        paths = {event.group: self.seed(event) for event in (first, second)}
        entered = asyncio.Event()
        release = asyncio.Event()

        async def ask(_prompt, conv_id, **_kwargs):
            event = first if conv_id == first.group else second
            if event is first:
                entered.set()
                await release.wait()
            return self.selected_reply(event, "answer-" + event.group)

        self.bridge._ask = AsyncMock(side_effect=ask)
        pending = asyncio.create_task(self.answer(first))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            second_results = await self.answer(second)
            self.clock += MEME_LIBRARY.GLOBAL_GAP_SEC + 1
        finally:
            release.set()
            first_results = await pending
        for event, results in ((first, first_results), (second, second_results)):
            with self.subTest(group=event.group):
                chain = results[0]
                self.assertEqual(chain[0].id, event.identifier)
                self.assertEqual(chain[1].text, "answer-" + event.group)
                self.assertEqual(chain[2].file, paths[event.group])
        self.assertEqual(self.bridge._ask.await_count, 2)
        self.assertEqual(
            {call.args[0] for call in self.bridge._memes.take.call_args_list},
            {first.unified_msg_origin, second.unified_msg_origin},
        )

    async def test_unknown_malformed_and_duplicate_choices_never_attach_images(self):
        event = MemeEvent()
        self.seed(event)
        for control in (
            "[MEME:m0000000000000000]",
            "[MEME:C:/outside/secret.png]",
            "【MEME:broken】",
            "[MEME:broken",
            "[MEME:m0000000000000000] [MEME:m1111111111111111]",
        ):
            with self.subTest(control=control):
                self.bridge._ask = AsyncMock(return_value="answer " + control)
                results = await self.answer(event)
                self.assertEqual([type(item) for item in results[0]], [Reply, Plain])
                self.assertEqual(results[0][1].text, "answer")
                self.bridge._ask.assert_awaited_once()
                self.assert_unconsumed()

    async def test_foreign_group_token_is_removed_without_consuming_image(self):
        event = MemeEvent()
        foreign = MemeEvent("other-group")
        self.seed(event)
        self.seed(foreign)
        other_offer = self.bridge._memes.offer(foreign.unified_msg_origin)
        token = other_offer.candidates[0].token
        self.bridge._ask = AsyncMock(return_value=f"answer [MEME:{token}]")
        results = await self.answer(event)
        self.assertEqual([type(item) for item in results[0]], [Reply, Plain])
        self.assertEqual(results[0][1].text, "answer")
        self.assert_unconsumed()

    async def test_question_failure_silence_and_empty_outputs_do_not_reserve_images(
        self,
    ):
        event = MemeEvent()
        self.seed(event)
        for text in ("[静默]", "[网关不可达]", "[过期跳过]", "[回复校验失败]"):
            with self.subTest(text=text):
                self.bridge._ask = AsyncMock(
                    side_effect=lambda *args, reply=text, **kwargs: self.named_reply(
                        event, reply, select=True
                    )
                )
                results = await self.answer(event)
                self.bridge._ask.assert_awaited_once()
                self.bridge._memes.take.assert_not_called()
                self.bridge._memes.learn.assert_not_called()
                self.assert_unconsumed()
                for chain in results:
                    self.assertFalse(any(isinstance(item, Image) for item in chain))
                    for item in chain:
                        if isinstance(item, Plain):
                            self.assertNotIn("MEME", item.text)
                if text == "[静默]":
                    self.assertEqual(results, [])

    async def test_question_filtered_empty_or_blocked_delivery_keeps_image_unused(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event)
        )
        self.globals["_sanitize"] = lambda text: ""
        await self.answer(event)
        self.bridge._memes.take.assert_not_called()
        self.globals["_sanitize"] = lambda text: text
        self.bridge._can_send_active = lambda: False
        self.assertEqual(await self.answer(event), [])
        self.bridge._memes.take.assert_not_called()
        self.assert_unconsumed()

    async def test_expired_question_offer_preserves_text_without_consuming_image(self):
        event = MemeEvent()
        self.seed(event)

        def ask(*args, **kwargs):
            result = self.selected_reply(event)
            self.clock += MEME_LIBRARY.OFFER_TTL_SEC + 1
            return result

        self.bridge._ask = AsyncMock(side_effect=ask)
        results = await self.answer(event)
        self.assertEqual([type(item) for item in results[0]], [Reply, Plain])
        self.assertEqual(results[0][1].text, "answer")
        self.assert_unconsumed()

    async def test_question_disabled_or_private_does_not_offer_images(self):
        self.bridge._ask = AsyncMock(return_value="answer [MEME:m0000000000000000]")
        event = MemeEvent()
        self.seed(event)
        self.options["meme_enabled"] = False
        results = await self.answer(event)
        self.bridge._memes.offer.assert_not_called()
        self.assertEqual(results[0][-1].text, "answer")
        self.options.clear()
        event = MemeEvent("")
        event.private = True
        await self.answer(event)
        self.bridge._memes.offer.assert_not_called()
        self.assert_unconsumed()

    async def test_proactive_image_is_sent_once_to_source_with_same_model_call(self):
        event = MemeEvent()
        path = self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event)
        )
        self.assertEqual(await self.spark(event), (True, "answer"))
        self.bridge._ask.assert_awaited_once()
        self.assertTrue(self.bridge._ask.call_args.kwargs["background"])
        self.assertIn(
            self.offers[event.unified_msg_origin].prompt,
            self.bridge._ask.call_args.args[0],
        )
        self.assertIn(
            "本群正在聊猫猫笑哭",
            self.bridge._memes.offer.call_args.kwargs["context"],
        )
        self.assertNotIn(
            "continue the conversation",
            self.bridge._memes.offer.call_args.kwargs["context"],
        )
        self.bridge._send_chain_to_session.assert_awaited_once()
        destination, chain = self.bridge._send_chain_to_session.call_args.args
        self.assertEqual(destination, event.unified_msg_origin)
        self.assertEqual([type(item) for item in chain.chain], [Plain, Image])
        self.assertEqual(chain.chain[0].text, "answer")
        self.assertEqual(chain.chain[1].file, path)
        self.bridge._send_to_session.assert_not_awaited()

    async def test_proactive_ambiguous_image_delivery_does_not_resend_text(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event)
        )
        self.bridge._send_chain_to_session.return_value = False
        self.assertEqual(await self.spark(event), (False, "answer"))
        self.bridge._send_chain_to_session.assert_awaited_once()
        self.bridge._send_to_session.assert_not_awaited()
        self.assertIn(event.unified_msg_origin, self.bridge._memes._group_last)
        self.assertEqual(self.bridge._spark_said, {})

    async def test_proactive_failures_and_silence_do_not_consume_images(self):
        event = MemeEvent()
        self.seed(event)
        for text in ("[静默]", "[网关不可达]", "[过期跳过]"):
            with self.subTest(text=text):
                self.bridge._ask = AsyncMock(
                    side_effect=lambda *args, reply=text, **kwargs: self.named_reply(
                        event, reply, select=True
                    )
                )
                self.assertIsNone(await self.spark(event))
                self.bridge._ask.assert_awaited_once()
                self.bridge._memes.take.assert_not_called()
                self.bridge._memes.learn.assert_not_called()
                self.assert_unconsumed()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()

    async def test_proactive_stale_revision_busy_or_stopped_send_does_not_take_image(
        self,
    ):
        event = MemeEvent()
        self.seed(event)
        for reason in ("new-message", "busy", "stopped"):
            with self.subTest(reason=reason):
                self.bridge._group_activity = {}
                self.bridge._questions.busy = False
                self.bridge._can_send_active = lambda: True

                def ask(*args, cause=reason, **kwargs):
                    if cause == "new-message":
                        self.bridge._group_activity[event.group] = {
                            "message_version": 1
                        }
                    elif cause == "busy":
                        self.bridge._questions.busy = True
                    else:
                        self.bridge._can_send_active = lambda: False
                    return self.named_reply(event, select=True)

                self.bridge._ask = AsyncMock(side_effect=ask)
                self.assertIsNone(await self.spark(event))
                self.bridge._memes.take.assert_not_called()
                self.bridge._memes.learn.assert_not_called()
                self.assert_unconsumed()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()

    async def test_proactive_expired_context_does_not_offer_or_call_model(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(side_effect=AssertionError("Expired context"))
        self.assertIsNone(await self.spark(event, created_at=time.monotonic() - 181))
        self.bridge._ask.assert_not_awaited()
        self.bridge._memes.offer.assert_not_called()
        self.bridge._memes.take.assert_not_called()

    async def test_proactive_context_expiring_during_model_call_does_not_consume_image(
        self,
    ):
        event = MemeEvent()
        self.seed(event)
        stamp = [1000.0]
        self.globals["time"] = types.SimpleNamespace(
            monotonic=lambda: stamp[0], time=time.time
        )

        def expired_reply(*args, **kwargs):
            stamp[0] = 1031.0
            return self.named_reply(event, select=True)

        self.bridge._ask = AsyncMock(side_effect=expired_reply)
        self.assertIsNone(await self.spark(event))
        self.bridge._ask.assert_awaited_once()
        self.bridge._memes.take.assert_not_called()
        self.bridge._memes.learn.assert_not_called()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()
        self.assert_unconsumed()

    async def test_question_learns_a_name_without_an_extra_model_call_or_image_send(
        self,
    ):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event)
        )
        results = await self.answer(event)
        self.bridge._ask.assert_awaited_once()
        self.assertEqual([type(item) for item in results[0]], [Reply, Plain])
        self.assertEqual(results[0][1].text, "answer")
        self.bridge._memes.learn.assert_called_once()
        scope, offered, raw = self.bridge._memes.learn.call_args.args
        self.assertEqual(scope, event.unified_msg_origin)
        self.assertIs(offered, self.offers[scope])
        self.assertIn("MEME_NAME", raw)
        self.assert_unconsumed()
        reopened = MEME_LIBRARY.MemeLibrary(self.library_root)
        recalled = reopened.offer(scope, context="笑哭", now=self.clock)
        self.assertEqual(recalled.candidates[0].title, "猫猫笑哭")
        self.assertTrue(recalled.candidates[0].named)
        self.assertIn("笑哭", recalled.candidates[0].tags)

    async def test_question_names_and_selects_the_same_picture_without_leaking_controls(
        self,
    ):
        event = MemeEvent()
        path = self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event, select=True)
        )
        results = await self.answer(event)
        self.bridge._ask.assert_awaited_once()
        self.assertEqual([type(item) for item in results[0]], [Reply, Plain, Image])
        self.assertEqual(results[0][1].text, "answer")
        self.assertEqual(results[0][2].file, path)
        self.bridge._memes.learn.assert_called_once()

    async def test_naming_storage_failure_preserves_the_answer_and_selected_image(self):
        event = MemeEvent()
        path = self.seed(event)
        self.bridge._memes.learn = Mock(side_effect=OSError("Read-only catalogue"))
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event, select=True)
        )
        results = await self.answer(event)
        self.assertEqual([type(item) for item in results[0]], [Reply, Plain, Image])
        self.assertEqual(results[0][1].text, "answer")
        self.assertEqual(results[0][2].file, path)
        self.bridge._ask.assert_awaited_once()

    async def test_question_can_reply_with_only_its_own_quoted_image(self):
        event = MemeEvent(identifier="404")
        path = self.seed(event)
        self.bridge._follow_chain[event.group] = {"count": 2}
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event, "")
        )
        results = await self.answer(event)
        self.assertEqual(len(results), 1)
        self.assertEqual([type(item) for item in results[0]], [Reply, Image])
        self.assertEqual(results[0][0].id, "404")
        self.assertEqual(results[0][1].file, path)
        self.bridge._ask.assert_awaited_once()
        self.assertEqual(
            self.bridge._spark_said[event.group][-1]["text"], "（发了一张表情包）"
        )
        self.assertNotIn(event.group, self.bridge._follow_chain)

    async def test_empty_output_without_a_valid_choice_cannot_become_an_image(self):
        event = MemeEvent()
        self.seed(event)
        for raw in ("", "[MEME:broken]", "[MEME:m0000000000000000]"):
            with self.subTest(raw=raw):
                self.bridge._ask = AsyncMock(return_value=raw)
                for chain in await self.answer(event):
                    self.assertFalse(any(isinstance(item, Image) for item in chain))
                self.assert_unconsumed()
                self.assertIsNone(await self.spark(event))
                self.assert_unconsumed()
        self.bridge._memes.learn.assert_not_called()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()

    async def test_pure_image_cannot_use_a_foreign_group_token_or_name(self):
        event = MemeEvent()
        foreign = MemeEvent("other-group")
        self.seed(event)
        self.seed(foreign)
        other_offer = self.bridge._memes.offer(foreign.unified_msg_origin)
        token = other_offer.candidates[0].token
        self.bridge._ask = AsyncMock(
            return_value=f"[MEME:{token}] [MEME_NAME:{token}|别群猫猫|开心]"
        )
        for chain in await self.answer(event):
            self.assertFalse(any(isinstance(item, Image) for item in chain))
        self.assertIsNone(await self.spark(event))
        self.assert_unconsumed()
        for scope in (event.unified_msg_origin, foreign.unified_msg_origin):
            offered = self.bridge._memes.offer(scope)
            self.assertTrue(
                all(item.title != "别群猫猫" for item in offered.candidates)
            )

    async def test_invalidated_pure_image_never_sends_a_blank_or_unverified_result(
        self,
    ):
        event = MemeEvent()
        for reason in ("deleted", "changed", "expired", "cooldown"):
            with self.subTest(reason=reason):
                self.bridge._memes._group_last.clear()
                path = Path(self.seed(event))

                def invalidate(*args, cause=reason, **kwargs):
                    raw = self.selected_reply(event, "")
                    if cause == "deleted":
                        path.unlink()
                    elif cause == "changed":
                        path.write_bytes(PNG + b"different content")
                    elif cause == "expired":
                        self.clock += MEME_LIBRARY.OFFER_TTL_SEC + 1
                    else:
                        self.bridge._memes._group_last[event.unified_msg_origin] = (
                            self.clock
                        )
                    return raw

                self.bridge._ask = AsyncMock(side_effect=invalidate)
                self.assertEqual(await self.answer(event), [])
                self.assertEqual(self.bridge._memes._asset_last, {})
                self.assertIsNone(self.bridge._memes._last_global)
                self.assertEqual(self.bridge._spark_said, {})

    async def test_proactive_pure_image_has_no_blank_text_component(self):
        event = MemeEvent()
        path = self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event, "", select=True)
        )
        outcome = await self.spark(event)
        self.assertTrue(outcome and outcome[0])
        self.bridge._ask.assert_awaited_once()
        self.bridge._memes.learn.assert_called_once()
        destination, result = self.bridge._send_chain_to_session.call_args.args
        self.assertEqual(destination, event.unified_msg_origin)
        self.assertEqual([type(item) for item in result.chain], [Image])
        self.assertEqual(result.chain[0].file, path)
        self.bridge._send_to_session.assert_not_awaited()
        self.assertEqual(
            self.bridge._spark_said[event.group][-1]["text"], "（发了一张表情包）"
        )

    async def test_question_cannot_record_an_image_that_could_not_be_constructed(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event, "")
        )
        self.globals["Image"] = Mock(side_effect=ValueError("Unsupported image"))
        self.assertEqual(await self.answer(event), [])
        self.assertEqual(self.bridge._spark_said, {})

    async def test_proactive_image_construction_failure_preserves_available_text(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event)
        )
        self.globals["Image"] = Mock(side_effect=ValueError("Unsupported image"))
        self.assertEqual(await self.spark(event), (True, "answer"))
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_awaited_once_with(
            event.unified_msg_origin, "answer"
        )

    async def test_failed_proactive_pure_image_construction_never_sends_blank_text(
        self,
    ):
        event = MemeEvent()
        self.seed(event)
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.selected_reply(event, "")
        )
        self.globals["Image"] = Mock(side_effect=ValueError("Unsupported image"))
        self.assertIsNone(await self.spark(event))
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()
        self.assertEqual(self.bridge._spark_said, {})

    async def test_proactive_filtered_text_cannot_be_replaced_by_an_image(self):
        event = MemeEvent()
        self.seed(event)
        self.globals["_sanitize"] = lambda text: ""
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event, select=True)
        )
        self.assertIsNone(await self.spark(event))
        self.bridge._memes.learn.assert_not_called()
        self.bridge._memes.take.assert_not_called()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.assert_unconsumed()

    async def test_proactive_naming_failure_does_not_drop_the_text_reply(self):
        event = MemeEvent()
        self.seed(event)
        self.bridge._memes.learn = Mock(side_effect=OSError("Read-only catalogue"))
        self.bridge._ask = AsyncMock(
            side_effect=lambda *args, **kwargs: self.named_reply(event)
        )
        self.assertEqual(await self.spark(event), (True, "answer"))
        self.bridge._send_to_session.assert_awaited_once_with(
            event.unified_msg_origin, "answer"
        )
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._ask.assert_awaited_once()
        self.assert_unconsumed()

    async def test_proactive_rechecks_activity_and_deadline_after_naming(self):
        event = MemeEvent()
        self.seed(event)
        for reason in ("new-message", "paused", "expired"):
            with self.subTest(reason=reason):
                self.bridge._group_activity = {}
                self.bridge._can_send_active = lambda: True
                stamp = [1000.0]
                self.globals["time"] = types.SimpleNamespace(
                    monotonic=lambda: stamp[0], time=time.time
                )

                def changed_during_naming(*args, cause=reason):
                    if cause == "new-message":
                        self.bridge._group_activity[event.group] = {
                            "message_version": 1
                        }
                    elif cause == "paused":
                        self.bridge._can_send_active = lambda: False
                    else:
                        stamp[0] = 1031.0
                    return 0

                self.bridge._memes.learn = Mock(side_effect=changed_during_naming)
                self.bridge._ask = AsyncMock(
                    side_effect=lambda *args, **kwargs: self.named_reply(
                        event, select=True
                    )
                )
                self.assertIsNone(await self.spark(event))
                self.bridge._memes.learn.assert_called_once()
                self.bridge._memes.take.assert_not_called()
                self.assert_unconsumed()
        self.bridge._send_chain_to_session.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()

    async def test_nonwake_sticker_is_collected_into_original_group_without_model_call(
        self,
    ):
        event = MemeEvent(sticker=True)
        source = self.root / "received.png"
        source.write_bytes(PNG)
        materialize = AsyncMock(return_value=str(source))
        self.globals["_materialize_attachment"] = materialize
        self.options["group_require_at"] = True
        self.bridge._meme_collector = MEME_COLLECTION.Collector(self.library_root)
        self.bridge._ask = AsyncMock(side_effect=AssertionError("Unsolicited question"))
        results = [result async for result in self.bridge.on_message(event)]
        self.assertEqual(results, [])
        pending = list(self.bridge._background_tasks)
        self.assertEqual(len(pending), 1)
        await asyncio.gather(*pending)
        materialize.assert_awaited_once_with(event.components[0])
        self.bridge._ask.assert_not_awaited()
        self.bridge._send_to_session.assert_not_awaited()
        own = self.bridge._memes.offer(event.unified_msg_origin)
        self.assertIsNotNone(own)
        self.assertIsNone(self.bridge._memes.offer("fake:GroupMessage:other-group"))
        self.assertEqual(len(own.candidates), 1)
        self.assertTrue(source.exists())

    async def test_collection_is_disabled_by_switches_blacklist_pause_and_identity(
        self,
    ):
        collector = types.SimpleNamespace(schedule=Mock())
        self.bridge._meme_collector = collector
        self.bridge._ask = AsyncMock(side_effect=AssertionError("Unsolicited question"))
        for reason in (
            "meme-disabled",
            "collection-disabled",
            "blacklist",
            "paused",
            "stopped",
            "wrong-bot",
            "cannot-send",
            "raw-wrong-bot",
            "raw-wrong-group",
            "raw-wrong-sender",
            "private",
            "ordinary-image",
        ):
            with self.subTest(reason=reason):
                event = MemeEvent(sticker=True)
                self.options = {"group_require_at": True}
                self.globals["_is_paused"] = lambda: False
                self.globals["EXPECTED_SELF_ID"] = ""
                self.bridge._stopped = False
                self.bridge._can_send_active = lambda: True
                self.bridge._blacklisted = lambda group: False
                if reason == "meme-disabled":
                    self.options["meme_enabled"] = False
                elif reason == "collection-disabled":
                    self.options["meme_collect_enabled"] = False
                elif reason == "blacklist":
                    self.bridge._blacklisted = lambda group: True
                elif reason == "paused":
                    self.globals["_is_paused"] = lambda: True
                elif reason == "stopped":
                    self.bridge._stopped = True
                elif reason == "wrong-bot":
                    self.globals["EXPECTED_SELF_ID"] = "another-bot"
                elif reason == "cannot-send":
                    self.bridge._can_send_active = lambda: False
                elif reason == "raw-wrong-bot":
                    event.message_obj.raw_message["self_id"] = "another-bot"
                elif reason == "raw-wrong-group":
                    event.message_obj.raw_message["group_id"] = "other-group"
                elif reason == "raw-wrong-sender":
                    event.message_obj.raw_message["user_id"] = "other-sender"
                elif reason == "private":
                    event.private = True
                    self.globals["_is_master"] = lambda sender: False
                else:
                    event.message_obj.raw_message["message"][0]["data"]["sub_type"] = 0
                self.assertEqual(
                    [result async for result in self.bridge.on_message(event)], []
                )
                collector.schedule.assert_not_called()
                self.bridge._ask.assert_not_awaited()
        self.globals["_materialize_attachment"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
