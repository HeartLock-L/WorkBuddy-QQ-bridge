"""Keep live filtering settings effective across the pure-text module boundary."""

import unittest

from bridge_test_support import PLUGIN, load_definitions, load_module


class ReplyTextConfigurationTests(unittest.TestCase):
    def setUp(self):
        reply_text = load_module(
            PLUGIN.with_name("reply_text.py"), "reply_text_under_test"
        )
        self.namespace = load_definitions(
            {
                "reply_text": reply_text,
                "meme_library": load_module(
                    PLUGIN.with_name("meme_library.py"), "reply_text_memes"
                ),
                "FILTER_INTERNAL": True,
                "_RUNTIME": {"max_chars": 1500, "others_max_chars": 1500},
                "REFUSE_TEXT": "refused",
                "STRICT_STRIP_WORDS": reply_text.STRICT_STRIP_WORDS,
            },
            functions={"_sanitize", "_strict_filter"},
        )

    def test_disabled_filter_preserves_text_without_reading_limits(self):
        self.namespace["FILTER_INTERNAL"] = False
        self.namespace["_RUNTIME"] = {}
        text = "[INFO] internal detail\n**" + "原样文字" * 400 + "**"
        self.assertEqual(self.namespace["_sanitize"](text), text)
        self.namespace["FILTER_INTERNAL"] = True
        self.assertEqual(self.namespace["_sanitize"](""), "")
        self.assertEqual(self.namespace["_strict_filter"](""), "")

    def test_changed_limits_apply_on_the_next_call(self):
        text = "这是一条普通群聊消息。" * 20
        sanitize = self.namespace["_sanitize"]
        strict_filter = self.namespace["_strict_filter"]
        self.assertEqual(sanitize(text), text)
        self.assertEqual(strict_filter(text), text)

        self.namespace["_RUNTIME"]["max_chars"] = 40
        self.assertLess(len(sanitize(text)), len(text))
        self.assertEqual(strict_filter(text), text)
        self.namespace["_RUNTIME"]["others_max_chars"] = 40
        self.assertLess(len(strict_filter(text)), len(text))
        self.namespace["_RUNTIME"] = {"max_chars": 0, "others_max_chars": 0}
        self.assertEqual(sanitize(text), text)
        self.assertEqual(strict_filter(text), text)

    def test_meme_control_markers_never_reach_other_reply_paths(self):
        for enabled in (True, False):
            self.namespace["FILTER_INTERNAL"] = enabled
            for marker in (
                "[MEME:m0123456789abcdef]",
                "[MEME:unknown]",
                "[MEME:broken",
                "[MEME_NAME:m0123456789abcdef|猫猫疑惑|疑惑,没听懂]",
                "[MEME_NAME:unknown|私有名字|标签]",
                "[MEME_NAME:broken",
                "【MEME_NAME:wrong】",
            ):
                self.assertEqual(
                    self.namespace["_sanitize"]("normal reply " + marker),
                    "normal reply",
                )

    def test_changed_terms_and_refusal_apply_on_the_next_call(self):
        strict_filter = self.namespace["_strict_filter"]
        text = "普通聊天内容\n特定用词这一行"
        self.assertEqual(strict_filter(text), text)
        self.namespace["STRICT_STRIP_WORDS"] = ("特定用词",)
        self.assertEqual(strict_filter(text), "普通聊天内容")
        self.namespace["REFUSE_TEXT"] = "本轮配置的拒绝文案"
        self.assertEqual(strict_filter("特定用词这一行"), "本轮配置的拒绝文案")


if __name__ == "__main__":
    unittest.main()
