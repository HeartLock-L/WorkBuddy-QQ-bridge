"""Exercise scoped meme choices without starting QQ, models, or downloads."""

import concurrent.futures
import dataclasses
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge_test_support import PLUGIN, load_module

MEMES = load_module(PLUGIN.with_name("meme_library.py"), "meme_library_under_test")
PNG = b"\x89PNG\r\n\x1a\n"


class MemeLibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = MEMES.MemeLibrary(self.root / "memes")

    def manual(self, name="happy.png", data=PNG + b"happy"):
        target = self.library.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def collected(self, scope, data=PNG + b"collected"):
        target = (
            self.library.root
            / "自动收集"
            / hashlib.sha256(scope.encode()).hexdigest()[:16]
            / (hashlib.sha256(data).hexdigest() + ".png")
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def offer(self, scope="group-a", now=100, **kwargs):
        return self.library.offer(scope, chance_percent=100, now=now, **kwargs)

    def named(self, path, name="开心猫猫", tags=("开心",)):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        directory = path.parent / ".names"
        directory.mkdir(exist_ok=True)
        target = directory / (digest + ".json")
        target.write_text(
            json.dumps(
                {
                    "version": 1,
                    "digest": digest,
                    "file": path.name,
                    "name": name,
                    "tags": tags,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return target

    def test_missing_library_does_not_create_files(self):
        self.assertIsNone(self.offer())
        self.assertFalse(self.library.root.exists())

    def test_manual_and_collected_choices_are_scoped_to_original_conversation(self):
        manual = self.manual()
        same = self.collected("group-a")
        self.named(manual)
        other = self.collected("group-b", PNG + b"other-group")
        nested = self.manual("nested/unlisted.png", PNG + b"nested")
        offer = self.offer()
        self.assertEqual(
            {item.path for item in offer.candidates}, {str(manual), str(same)}
        )
        self.assertNotIn(str(other), offer.prompt)
        self.assertNotIn(str(nested), offer.prompt)
        self.assertIn("不能仅凭文件名猜画面", offer.prompt)
        self.assertIn("先正常回答问题", offer.prompt)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            offer.scope = "group-b"

    def test_expired_or_misnamed_collected_files_are_not_offered(self):
        old = self.collected("group-a")
        age = time.time() - MEMES.COLLECTED_KEEP_SEC - 10
        os.utime(old, (age, age))
        bad = old.with_name("0" * 64 + ".png")
        bad.write_bytes(PNG + b"name does not match its content")
        self.assertIsNone(self.offer())

    def test_no_duplicates_and_at_most_three_images(self):
        for index in range(5):
            path = self.manual(f"image-{index}.png", PNG + bytes([index]))
            if index < 3:
                self.named(path)
        self.manual("duplicate.png", PNG + b"\0")
        offer = self.offer()
        self.assertEqual(len(offer.candidates), 3)
        self.assertEqual(len({item.digest for item in offer.candidates}), 3)
        self.assertLessEqual(sum(not item.named for item in offer.candidates), 1)

    def test_per_file_and_aggregate_candidate_bytes_are_bounded(self):
        self.manual("oversize.png", PNG + b"x" * 100)
        self.named(self.manual("first.png", PNG + b"a" * 42))
        self.named(self.manual("second.png", PNG + b"b" * 42))
        with patch.object(MEMES, "MAX_BYTES", 80):
            offer = self.offer()
        self.assertEqual(len(offer.candidates), 1)
        self.assertTrue(offer.candidates[0].path.endswith("first.png"))

    def test_nonimage_file_is_ignored_and_gif_is_supported(self):
        self.manual("error.png", b"<html>failed</html>")
        animated = self.manual("reaction.gif", b"GIF89a" + b"fixture")
        offer = self.offer()
        self.assertEqual([item.path for item in offer.candidates], [str(animated)])

    def test_symbolic_link_file_or_library_is_not_followed(self):
        outside = self.root / "outside.png"
        outside.write_bytes(PNG + b"outside")
        self.library.root.mkdir()
        try:
            (self.library.root / "link.png").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Directory fixture cannot create symbolic links: {exc}")
        self.assertIsNone(self.offer())
        real_library = self.root / "actual"
        real_library.mkdir()
        (real_library / "plain.png").write_bytes(PNG + b"normal")
        redirected = self.root / "redirected"
        redirected.symlink_to(real_library, target_is_directory=True)
        self.assertIsNone(
            MEMES.MemeLibrary(redirected).offer("group-a", chance_percent=100)
        )

    def test_strip_one_valid_marker_without_reserving_a_send(self):
        self.manual()
        offer = self.offer()
        token = offer.candidates[0].token
        text, selected = MEMES.strip_selection(f"有点乐 [MEME:{token}]", offer)
        self.assertEqual((text, selected), ("有点乐", token))
        self.assertEqual(self.library._group_last, {})
        self.assertEqual(MEMES.strip_selection(f"[MEME:{token}]", offer), ("", token))

    def test_text_without_control_marker_preserves_whitespace_exactly(self):
        for text in ("", "   ", "\n 答复  \n", "\tfirst\r\nsecond\t", "a [other] b"):
            with self.subTest(text=text):
                self.assertEqual(MEMES.strip_selection(text), (text, None))

    def test_unknown_malformed_and_multiple_controls_never_select_paths(self):
        self.manual()
        offer = self.offer()
        token = offer.candidates[0].token
        cases = [
            "正文 [MEME:../../secret.png]",
            "正文 [MEME:unknown]",
            f"正文 [MEME:{token}] [MEME:{token}]",
            f"正文 【MEME:{token}】",
            "正文 [MEME:\nsecret.png\n]",
            "正文 [MEME:incomplete",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                clean, selected = MEMES.strip_selection(raw, offer)
                self.assertEqual(clean, "正文")
                self.assertIsNone(selected)
        self.assertEqual(MEMES.strip_selection(f"正文 [MEME:{token}]"), ("正文", None))

    def test_offer_tokens_cannot_be_reused_for_another_request_or_group(self):
        self.manual()
        first, second = self.offer(), self.offer("group-b")
        token = first.candidates[0].token
        self.assertIsNone(MEMES.strip_selection(f"答复 [MEME:{token}]", second)[1])
        self.assertIsNone(self.library.take("group-b", first, token, now=101))
        self.assertEqual(self.library._group_last, {})

    def test_changed_content_is_rejected_even_when_size_and_mtime_match(self):
        path = self.manual()
        offer = self.offer()
        original = path.stat()
        path.write_bytes(PNG + b"other")
        os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertIsNone(
            self.library.take("group-a", offer, offer.candidates[0].token, now=101)
        )
        self.assertEqual(self.library._group_last, {})

    def test_deleted_or_redirected_choice_does_not_reserve_cooldown(self):
        path = self.manual()
        offer = self.offer()
        path.unlink()
        self.assertIsNone(
            self.library.take("group-a", offer, offer.candidates[0].token, now=101)
        )
        self.manual()
        fresh = self.offer(now=102)
        self.assertIsNotNone(
            self.library.take("group-a", fresh, fresh.candidates[0].token, now=103)
        )

    def test_file_replaced_by_link_after_offer_is_rejected(self):
        path = self.manual()
        offer = self.offer()
        outside = self.root / "outside.png"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Fixture cannot create symbolic links: {exc}")
        self.assertIsNone(
            self.library.take("group-a", offer, offer.candidates[0].token, now=101)
        )
        self.assertEqual(self.library._group_last, {})
        self.assertTrue(outside.exists())

    def test_parallel_choices_reserve_only_one_send(self):
        self.manual()
        offers = [self.offer(), self.offer()]
        barrier = threading.Barrier(2)

        def select(offer):
            barrier.wait(timeout=2)
            return self.library.take(
                "group-a", offer, offer.candidates[0].token, now=101
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(select, offers))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_global_gap_blocks_another_group_even_with_another_asset(self):
        self.named(self.manual("a.png", PNG + b"a"))
        self.named(self.manual("b.png", PNG + b"b"))
        first, second = self.offer(), self.offer("group-b")
        self.assertIsNotNone(
            self.library.take("group-a", first, first.candidates[0].token, now=100)
        )
        self.assertIsNone(
            self.library.take("group-b", second, second.candidates[1].token, now=119)
        )
        self.assertIsNotNone(
            self.library.take("group-b", second, second.candidates[1].token, now=120)
        )

    def test_same_asset_is_not_repeated_after_group_cooldown_elapses(self):
        self.manual()
        offer = self.offer()
        self.assertIsNotNone(
            self.library.take("group-a", offer, offer.candidates[0].token, now=100)
        )
        self.assertIsNone(self.offer(now=219))
        self.assertIsNone(self.offer(now=220))
        self.assertIsNone(self.offer("group-b", now=699))
        self.assertIsNotNone(self.offer(now=700))

    def test_stale_and_invalid_time_choices_do_not_send(self):
        self.manual()
        offer = self.offer()
        for now in (99, 401, float("nan"), float("inf")):
            with self.subTest(now=now):
                self.assertIsNone(
                    self.library.take(
                        "group-a", offer, offer.candidates[0].token, now=now
                    )
                )
        self.assertEqual(self.library._group_last, {})

    def test_probability_gate_leaves_cooldown_and_files_unchanged(self):
        path = self.manual()
        with patch.object(MEMES.random, "random", return_value=0.5):
            self.assertIsNone(self.library.offer("group-a", chance_percent=20, now=100))
            self.assertIsNotNone(
                self.library.offer("group-a", chance_percent=100, now=100)
            )
        self.assertEqual(self.library._group_last, {})
        self.assertTrue(path.exists())

    def test_default_offer_never_uses_random_admission_or_random_order(self):
        self.manual()
        with (
            patch.object(
                MEMES.random, "random", side_effect=AssertionError("No lottery")
            ),
            patch.object(
                MEMES.random, "shuffle", side_effect=AssertionError("No shuffle")
            ),
        ):
            self.assertIsNotNone(
                self.library.offer("group-a", context="笑死了", now=100)
            )

    def test_named_recall_matches_emotion_synonyms_and_ignores_unrelated_names(self):
        happy = self.manual("first.png", PNG + b"laugh")
        puzzled = self.manual("second.png", PNG + b"puzzle")
        self.named(happy, "猫猫大笑", ("开心", "大笑"))
        self.named(puzzled, "猫猫疑惑", ("疑惑", "没听懂"))
        for context, path in (("笑死", happy), ("看不懂", puzzled), ("啊？", puzzled)):
            with self.subTest(context=context):
                offer = self.offer(context=context)
                self.assertEqual([item.path for item in offer.candidates], [str(path)])
        self.assertIsNone(self.offer(context="明天天气怎么样"))

    def test_negative_happiness_does_not_recall_happy_picture(self):
        happy = self.manual("happy.png", PNG + b"happy")
        sad = self.manual("sad.png", PNG + b"sad")
        self.named(happy, "猫猫大笑", ("开心", "快乐"))
        self.named(sad, "猫猫委屈", ("难过", "委屈"))
        for context in ("笑不出来", "不开心", "不太开心"):
            offer = self.offer(context=context)
            self.assertEqual([item.path for item in offer.candidates], [str(sad)])

    def test_only_one_unknown_image_is_offered_and_unlearned_choices_rotate(self):
        paths = [self.manual(f"image-{i}.png", PNG + bytes([i])) for i in range(4)]
        choices = [self.offer().candidates for _ in range(4)]
        self.assertTrue(
            all(len(items) == 1 and not items[0].named for items in choices)
        )
        self.assertEqual(
            {items[0].path for items in choices}, {str(path) for path in paths}
        )
        self.assertEqual(self.offer("another-group").candidates[0].path, str(paths[0]))

    def test_learning_survives_reopening_and_confirmed_names_need_no_image_read_prompt(
        self,
    ):
        path = self.collected("group-a")
        offer = self.offer()
        token = offer.candidates[0].token
        raw = f"没听懂 [MEME_NAME:{token}|猫猫疑惑|疑惑,没听懂] [MEME:{token}]"
        with patch.object(MEMES.time, "monotonic", return_value=101):
            self.assertEqual(self.library.learn("group-a", offer, raw), 1)
            self.assertEqual(self.library.learn("group-a", offer, raw), 0)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        record = json.loads(
            (path.parent / ".names" / (digest + ".json")).read_text(encoding="utf-8")
        )
        self.assertEqual(record["name"], "猫猫疑惑")
        self.assertEqual(record["tags"], ["疑惑", "没听懂"])
        self.assertNotIn("group-a", json.dumps(record))
        reopened = MEMES.MemeLibrary(self.library.root)
        choice = reopened.offer("group-a", context="我不懂", now=102)
        self.assertEqual(choice.candidates[0].title, "猫猫疑惑")
        self.assertTrue(choice.candidates[0].named)
        self.assertNotIn(str(path), choice.prompt)
        self.assertIn("不必再次读图", choice.prompt)
        self.assertIsNone(reopened.offer("group-b", context="我不懂", now=102))

    def test_naming_markers_are_removed_without_interfering_with_one_selection(self):
        self.manual()
        offer = self.offer()
        token = offer.candidates[0].token
        naming = f"[MEME_NAME:{token}|开心猫猫|开心,赞同]"
        clean, selected = MEMES.strip_selection(f"嘿嘿 {naming} [MEME:{token}]", offer)
        self.assertEqual((clean, selected), ("嘿嘿", token))
        self.assertEqual(MEMES.strip_selection(naming), ("", None))
        for marker in (
            "[MEME_NAME:broken",
            "【MEME_NAME:nope】",
            "[MEME_META:unknown]",
            "[MEME_NAME1:bad]",
            "[MEME_BAD_NAME:bad]",
        ):
            self.assertEqual(MEMES.strip_selection("正文 " + marker), ("正文", None))

    def test_unknown_duplicate_cross_group_and_stale_naming_cannot_write(self):
        path = self.collected("group-a")
        offer = self.offer()
        token = offer.candidates[0].token
        valid = f"[MEME_NAME:{token}|猫猫疑惑|疑惑]"
        with patch.object(MEMES.time, "monotonic", return_value=101):
            self.assertEqual(self.library.learn("group-b", offer, valid), 0)
            self.assertEqual(self.library.learn("group-a", offer, valid + valid), 0)
            self.assertEqual(
                self.library.learn(
                    "group-a", offer, "[MEME_NAME:m0000000000000000|猫猫疑惑|疑惑]"
                ),
                0,
            )
            foreign = dataclasses.replace(offer, scope="group-b")
            self.assertEqual(self.library.learn("group-b", foreign, valid), 0)
        with patch.object(MEMES.time, "monotonic", return_value=401):
            self.assertEqual(self.library.learn("group-a", offer, valid), 0)
        self.assertFalse((path.parent / ".names").exists())

    def test_changed_or_missing_images_cannot_gain_names(self):
        path = self.manual()
        offer = self.offer()
        token = offer.candidates[0].token
        raw = f"[MEME_NAME:{token}|猫猫疑惑|疑惑]"
        info = path.stat()
        path.write_bytes(PNG + b"other")
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        with patch.object(MEMES.time, "monotonic", return_value=101):
            self.assertEqual(self.library.learn("group-a", offer, raw), 0)
            path.unlink()
            self.assertEqual(self.library.learn("group-a", offer, raw), 0)
        self.assertFalse((path.parent / ".names").exists())

    def test_invalid_names_and_tags_never_become_persisted_data(self):
        self.manual()
        offer = self.offer()
        token = offer.candidates[0].token
        invalid = [
            ("../secret", "开心"),
            ("C:\\secret", "开心"),
            ("账号12345678", "开心"),
            ("名字" * 13, "开心"),
            ("猫猫", "一,二,三,四,五,六"),
            ("猫猫", "标签" * 7),
            ("猫猫", "user@example.com"),
        ]
        with patch.object(MEMES.time, "monotonic", return_value=101):
            for name, tags in invalid:
                raw = f"[MEME_NAME:{token}|{name}|{tags}]"
                self.assertEqual(self.library.learn("group-a", offer, raw), 0)
        self.assertFalse((self.library.root / ".names").exists())

    def test_metadata_write_failure_keeps_selection_and_original_image_intact(self):
        path = self.manual()
        original = path.read_bytes()
        offer = self.offer()
        token = offer.candidates[0].token
        raw = f"正文 [MEME_NAME:{token}|猫猫疑惑|疑惑] [MEME:{token}]"
        with (
            patch.object(MEMES.time, "monotonic", return_value=101),
            patch.object(MEMES.os, "replace", side_effect=PermissionError),
        ):
            self.assertEqual(self.library.learn("group-a", offer, raw), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list((path.parent / ".names").iterdir()), [])
        self.assertEqual(MEMES.strip_selection(raw, offer), ("正文", token))
        self.assertEqual(self.library.take("group-a", offer, token, now=102), str(path))

    def test_redirected_metadata_directory_is_neither_read_nor_written(self):
        path = self.manual()
        offer = self.offer()
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (path.parent / ".names").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Fixture cannot create symbolic links: {exc}")
        token = offer.candidates[0].token
        with patch.object(MEMES.time, "monotonic", return_value=101):
            self.assertEqual(
                self.library.learn(
                    "group-a", offer, f"[MEME_NAME:{token}|猫猫疑惑|疑惑]"
                ),
                0,
            )
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.offer().candidates[0].named)

    def test_invalid_oversized_and_stale_digest_metadata_does_not_supply_confirmed_name(
        self,
    ):
        path = self.manual()
        record_path = self.named(path)
        record = json.loads(record_path.read_text(encoding="utf-8"))
        for payload in (
            "x" * (MEMES.MAX_NAME_BYTES + 1),
            "{broken",
            json.dumps(dict(record, digest="0" * 64)),
            json.dumps(dict(record, file="../happy.png")),
        ):
            record_path.write_text(payload, encoding="utf-8")
            self.assertFalse(self.offer().candidates[0].named)


if __name__ == "__main__":
    unittest.main()
