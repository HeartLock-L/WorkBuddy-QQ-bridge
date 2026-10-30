"""Keep native QQ quote targets tied to each original request event."""

import asyncio
import re
import types
import unittest
from unittest.mock import Mock

from artifact_test_support import ArtifactFixture, Event
from bridge_test_support import load_definitions


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
    def __init__(self, *, file):
        self.file = file


class QuoteEvent(Event):
    def __init__(self, group="group-a", identifier="101", *, private=False):
        super().__init__(group)
        self.identifier = identifier
        self.private = private
        self.components = [At("fake-bot")]
        self.message_obj = types.SimpleNamespace(
            message_id=identifier, raw_message={"post_type": "message"}
        )
        self.plain_result = Mock(side_effect=lambda text: ("plain", text))
        self.chain_result = Mock(side_effect=lambda parts: ("chain", parts))

    def get_message_id(self):
        return self.identifier

    def get_messages(self):
        return self.components

    def is_private_chat(self):
        return self.private

    def get_message_str(self):
        return "question-" + self.group


class QuoteConstructionTests(unittest.TestCase):
    def setUp(self):
        self.namespace = {
            "re": re,
            "At": At,
            "Poke": Poke,
            "Reply": Reply,
            "Plain": Plain,
            "Image": Image,
            "logger": Mock(),
        }
        load_definitions(self.namespace, methods={"_reply_result"})
        self.bridge = self.namespace["Bridge"]()
        self.bridge._cfg_bool = lambda key, default: default

    def test_explicit_group_mention_builds_native_reply_before_text(self):
        for identifier in (101, "-102"):
            with self.subTest(identifier=identifier):
                event = QuoteEvent(identifier=identifier)
                kind, chain = self.bridge._reply_result(event, "answer")
                self.assertEqual(kind, "chain")
                self.assertEqual(len(chain), 2)
                self.assertIsInstance(chain[0], Reply)
                self.assertEqual(chain[0].id, str(identifier))
                self.assertEqual(chain[1].text, "answer")
                event.plain_result.assert_not_called()

    def test_private_empty_group_and_other_mentions_stay_plain(self):
        events = [QuoteEvent(private=True), QuoteEvent(group="")]
        for components in ([], [At("all")], [At("someone-else")], [Reply(id="999")]):
            event = QuoteEvent()
            event.components = components
            events.append(event)
        for event in events:
            with self.subTest(event=event.components, private=event.private):
                self.assertEqual(
                    self.bridge._reply_result(event, "answer"), ("plain", "answer")
                )
                event.chain_result.assert_not_called()

    def test_disabled_setting_preserves_plain_result(self):
        self.bridge._cfg_bool = lambda _key, _default: False
        event = QuoteEvent()
        self.assertEqual(
            self.bridge._reply_result(event, "answer"), ("plain", "answer")
        )
        event.chain_result.assert_not_called()

    def test_incoming_quote_never_replaces_current_question_identifier(self):
        event = QuoteEvent(identifier="123")
        event.components.insert(0, Reply(id="999"))
        result = self.bridge._reply_result(event, "answer")
        self.assertEqual(result[1][0].id, "123")

    def test_empty_or_unsupported_getter_uses_same_event_object_identifier(self):
        for getter in (
            None,
            lambda: "",
            Mock(side_effect=AttributeError("unsupported")),
        ):
            with self.subTest(getter=getter):
                event = QuoteEvent(identifier="123")
                event.get_message_id = getter
                self.assertEqual(
                    self.bridge._reply_result(event, "answer")[1][0].id, "123"
                )

    def test_invalid_nonempty_getter_does_not_fall_back_to_a_different_identifier(self):
        event = QuoteEvent(identifier="not-a-message")
        event.message_obj.message_id = "123"
        self.assertEqual(
            self.bridge._reply_result(event, "answer"), ("plain", "answer")
        )

    def test_invalid_identifiers_are_never_quoted(self):
        for identifier in (
            None,
            "",
            " ",
            0,
            "-0",
            False,
            True,
            "abc",
            "+1",
            "１２３",
            "1" * 128,
        ):
            with self.subTest(identifier=identifier):
                event = QuoteEvent(identifier=identifier)
                self.assertEqual(
                    self.bridge._reply_result(event, "answer"), ("plain", "answer")
                )
                event.chain_result.assert_not_called()

    def test_poke_and_raw_notifications_are_never_quoted(self):
        poke = QuoteEvent()
        poke.components.append(Poke())
        events = [poke]
        for post_type in ("notice", "meta_event", "request", "message_sent"):
            notice = QuoteEvent()
            notice.message_obj.raw_message = {"post_type": post_type}
            events.append(notice)
        for event in events:
            self.assertEqual(
                self.bridge._reply_result(event, "answer"), ("plain", "answer")
            )
            event.chain_result.assert_not_called()

    def test_missing_bot_identifier_never_uses_at_all(self):
        event = QuoteEvent()
        event.get_self_id = lambda: ""
        event.components = [At("")]
        self.assertEqual(
            self.bridge._reply_result(event, "answer"), ("plain", "answer")
        )

    def test_unsupported_result_factory_falls_back_without_sending(self):
        for factory in (None, Mock(side_effect=RuntimeError("unsupported"))):
            with self.subTest(factory=factory):
                event = QuoteEvent()
                event.chain_result = factory
                self.assertEqual(
                    self.bridge._reply_result(event, "answer"), ("plain", "answer")
                )
                event.plain_result.assert_called_once_with("answer")

    def test_component_construction_failure_keeps_the_filtered_text(self):
        self.namespace["Reply"] = Mock(side_effect=TypeError("unsupported"))
        event = QuoteEvent()
        self.assertEqual(
            self.bridge._reply_result(event, "filtered answer"),
            ("plain", "filtered answer"),
        )
        event.chain_result.assert_not_called()

    def test_quoted_meme_is_in_the_same_ordered_message(self):
        event = QuoteEvent(identifier="321")
        kind, parts = self.bridge._reply_result(
            event, "answer", image_path="verified-meme.gif"
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([type(part) for part in parts], [Reply, Plain, Image])
        self.assertEqual(parts[0].id, "321")
        self.assertEqual(parts[1].text, "answer")
        self.assertEqual(parts[2].file, "verified-meme.gif")
        event.plain_result.assert_not_called()
        event.chain_result.assert_called_once()

    def test_unquoted_meme_is_preserved_for_private_or_unaddressed_messages(self):
        private = QuoteEvent(private=True)
        unaddressed = QuoteEvent()
        unaddressed.components = []
        missing_id = QuoteEvent(identifier="")
        for event in (private, unaddressed, missing_id):
            with self.subTest(event=event):
                kind, parts = self.bridge._reply_result(
                    event, "answer", image_path="verified-meme.png"
                )
                self.assertEqual(kind, "chain")
                self.assertEqual([type(part) for part in parts], [Plain, Image])
                self.assertEqual(parts[1].file, "verified-meme.png")
                event.chain_result.assert_called_once()

    def test_pure_image_keeps_the_quote_without_an_empty_text_component(self):
        event = QuoteEvent(identifier="321")
        kind, parts = self.bridge._reply_result(
            event, "", image_path="verified-meme.gif"
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([type(part) for part in parts], [Reply, Image])
        self.assertEqual(parts[0].id, "321")
        self.assertEqual(parts[1].file, "verified-meme.gif")
        event.plain_result.assert_not_called()

    def test_pure_image_without_quote_contains_only_the_image(self):
        event = QuoteEvent(private=True)
        kind, parts = self.bridge._reply_result(
            event, "", image_path="verified-meme.gif"
        )
        self.assertEqual(kind, "chain")
        self.assertEqual([type(part) for part in parts], [Image])
        event.plain_result.assert_not_called()

    def test_failed_pure_image_construction_does_not_fall_back_to_blank_text(self):
        self.namespace["Image"] = Mock(side_effect=ValueError("unsupported image"))
        event = QuoteEvent()
        self.assertIsNone(self.bridge._reply_result(event, "", image_path="bad-image"))
        event.plain_result.assert_not_called()
        event.chain_result.assert_not_called()

    def test_disabling_quote_does_not_disable_the_optional_meme(self):
        self.bridge._cfg_bool = lambda _key, _default: False
        event = QuoteEvent()
        _, parts = self.bridge._reply_result(
            event, "answer", image_path="verified-meme.png"
        )
        self.assertEqual([type(part) for part in parts], [Plain, Image])

    def test_image_construction_failure_falls_back_to_plain_text_without_sending(self):
        self.namespace["Image"] = Mock(side_effect=ValueError("unsupported image"))
        event = QuoteEvent()
        self.assertEqual(
            self.bridge._reply_result(event, "answer", image_path="bad-path"),
            ("plain", "answer"),
        )
        event.chain_result.assert_not_called()
        event.plain_result.assert_called_once_with("answer")

    def test_quote_construction_failure_preserves_optional_unquoted_image(self):
        self.namespace["Reply"] = Mock(side_effect=TypeError("unsupported quote"))
        event = QuoteEvent()
        _, parts = self.bridge._reply_result(
            event, "answer", image_path="verified-meme.png"
        )
        self.assertEqual([type(part) for part in parts], [Plain, Image])
        event.chain_result.assert_called_once()


class QuotedQuestionRoutingTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        quote_namespace = {
            "re": re,
            "At": At,
            "Poke": Poke,
            "Reply": Reply,
            "Plain": Plain,
            "Image": Image,
            "logger": Mock(),
        }
        load_definitions(quote_namespace, methods={"_reply_result"})
        self.bridge._reply_result = types.MethodType(
            quote_namespace["Bridge"]._reply_result, self.bridge
        )
        self.globals.update(
            Image=Image,
            WEB_SEARCH_ENABLED=False,
            REDIRECT_ON_CONFIG_TOPIC=False,
            STRICT_FOR_OTHERS=False,
            REJECT_WORDS=(),
            _is_action_request=lambda text: None,
        )
        self.bridge._spark_said = {}
        self.bridge._follow_chain = {}
        self.bridge._group_context_block = lambda *args: ""
        self.bridge._current_persona = lambda event: "fixture persona"

    async def test_overlapping_questions_quote_their_own_event_after_reversed_completion(
        self,
    ):
        started = asyncio.Event()
        release = asyncio.Event()
        first = QuoteEvent("group-a", "111")
        second = QuoteEvent("group-b", "-222")

        async def ask(prompt, conversation, **kwargs):
            if conversation == "group-a":
                started.set()
                await release.wait()
            else:
                self.assertTrue(started.is_set())
            return "answer-" + conversation

        self.bridge._ask = ask

        async def dispatch(event):
            return [result async for result in self.bridge.on_message(event)]

        first_task = asyncio.create_task(dispatch(first))
        try:
            await asyncio.wait_for(started.wait(), 1)
            second_results = await dispatch(second)
        finally:
            release.set()
            first_results = await asyncio.wait_for(first_task, 1)
        for result, identifier, text in (
            (first_results[0], "111", "answer-group-a"),
            (second_results[0], "-222", "answer-group-b"),
        ):
            self.assertEqual(result[0], "chain")
            self.assertEqual(result[1][0].id, identifier)
            self.assertEqual(result[1][1].text, text)
        self.assertTrue(first.stopped)
        self.assertTrue(second.stopped)
        self.bridge._send_chain_to_session.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
