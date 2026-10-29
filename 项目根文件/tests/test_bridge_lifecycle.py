"""Offline regression tests for bridge lifecycle, silence, and attachments.

The harness compiles only selected definitions from the owned plugin. It never
imports AstrBot, starts a gateway run, launches the image CLI, or sends to QQ.
"""

import asyncio
import os
import re
import shutil
import tempfile
import threading
import time
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module

MEME_LIBRARY = load_module(
    PLUGIN.with_name("meme_library.py"), "test_bridge_lifecycle_memes"
)
MEME_COLLECTION = load_module(
    PLUGIN.with_name("meme_collection.py"), "test_bridge_lifecycle_collection"
)

QUEUE = load_module(PLUGIN.with_name("request_queue.py"), "lifecycle_queue_under_test")
SOURCES = load_module(
    PLUGIN.with_name("source_files.py"), "lifecycle_sources_under_test"
)
ATTACHMENTS = load_module(
    PLUGIN.with_name("attachments.py"), "lifecycle_attachments_under_test"
)


class MessageSession:
    """Represent a destination without loading the messaging framework."""

    def __init__(self, platform_name="test", message_type="group", session_id="g"):
        self.platform_name = platform_name
        self.message_type = message_type
        self.session_id = session_id

    @classmethod
    def from_str(cls, umo):
        """Parse the test session format."""
        return cls(session_id=umo.rsplit(":", 1)[-1])


class MessageChain:
    """Hold components for the in-memory send recorder."""

    def __init__(self, chain=None):
        self.chain = chain or []


class Plain:
    """Hold a text component without any external behavior."""

    def __init__(self, text):
        self.text = text


def load_harness():
    """Compile selected production definitions with offline dependencies.

    Returns:
        The bridge class and its isolated global namespace.
    """
    methods = {
        "__init__",
        "_cfg",
        "_cfg_bool",
        "_cfg_int",
        "terminate",
        "_current_persona",
        "_ask",
        "_record_question_outcome",
        "_spawn_background",
        "_can_send_active",
        "_send_to_master",
        "_send_to_session",
        "_send_chain_to_session",
        "_speak_spark",
        "_spark_material",
        "_collect_attachments",
        "_idle_scan",
    }
    helpers = {"_is_meta_note", "_is_empty_reply", "_is_gateway_error"}
    constants = {
        "EMPTY_REPLY_MARKERS",
        "META_NOTE_PREFIXES",
        "META_NOTE_MARKERS",
        "_GATEWAY_ERROR_RE",
        "TIMEOUT_SEC",
    }

    def noop(*_args, **_kwargs):
        return None

    namespace = {
        "__file__": str(PLUGIN),
        "WORK_DIR": Path(tempfile.gettempdir()) / "unused-meme-fixture",
        "asyncio": asyncio,
        "memory_service": types.SimpleNamespace(
            MemoryService=lambda *_args, **_kwargs: types.SimpleNamespace(
                close=AsyncMock(return_value=True),
                note_dialogue=Mock(),
                context=AsyncMock(return_value=""),
                group_context=AsyncMock(return_value=""),
            )
        ),
        "meme_library": MEME_LIBRARY,
        "meme_collection": MEME_COLLECTION,
        "os": os,
        "time": time,
        "threading": threading,
        "uuid": uuid,
        "Path": Path,
        "re": re,
        "shutil": shutil,
        "star": types.SimpleNamespace(Context=object),
        "logger": types.SimpleNamespace(info=noop, warning=noop, error=noop),
        "request_queue": QUEUE,
        "source_files": SOURCES,
        "attachments": ATTACHMENTS,
        "image_jobs": types.SimpleNamespace(ImageJobs=lambda *a, **k: None),
        "jimeng": types.SimpleNamespace(
            JimengClient=lambda *args, **kwargs: types.SimpleNamespace(sessions={})
        ),
        "DRAW_SESSION_STATE_FILE": PLUGIN.with_name("draw_sessions.json"),
        "IMAGE_GEN_DIR": "unused-image-root",
        "DRAW_SELFIE_PROMPT": "selfie:",
        "EXPECTED_SELF_ID": "",
        "FOLLOW_MAX_CHAIN": 2,
        "ROLEPLAY_TIMEOUT_SEC": 60,
        "FOLLOW_CHAIN_COOLDOWN_SEC": 900,
        "SPARK_FAIL_CIRCUIT_N": 3,
        "_CAN_SEND_ACTIVE": True,
        "_is_paused": lambda: False,
        "_conv_id": lambda umo, gen: f"{umo}:{gen}",
        "_sanitize": lambda text: text,
        "MessageSession": MessageSession,
        "MessageChain": MessageChain,
        "Plain": Plain,
        "MessageType": types.SimpleNamespace(FRIEND_MESSAGE="friend"),
        "MASTER_ID": "test-master",
        "IDLE_HISTORY_SIZE": 15,
        "IDLE_LIVE_HISTORY": True,
        "IDLE_SOURCE_TTL": 1800,
        "IDLE_REQUIRE_TEXT": True,
        "IDLE_ALLOW_IMAGE": False,
        "_has_textual_material": lambda hist: any(h["text"] for h in hist),
        "READ_ATTACHMENTS": True,
        "GROUP_ATTACHMENT_REQUIRE_AT": True,
        "MAX_ATTACHMENTS_PER_MSG": 5,
        "_is_at_me": lambda event: True,
        "_attachment_components": lambda event: event.attachments,
        "_safe_attachment_dir": lambda name, sender: "same-user",
        "_mentions_image": lambda text: False,
    }
    load_definitions(namespace, functions=helpers, constants=constants, methods=methods)
    return namespace["Bridge"], namespace


class BridgeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Exercise production lifecycle methods without external services."""

    def setUp(self):
        self.Bridge, self.namespace = load_harness()
        self.context = types.SimpleNamespace(send_message=AsyncMock(return_value=True))
        self.bridge = self.Bridge(self.context)
        self.bridge._memes = None
        self.bridge._meme_collector = None
        self.bridge._write_runtime_status = Mock()
        # Production paths derive from the plugin file, so the admission lock
        # would be the live bot's own question_run.lock while it is running.
        # Keep every test on a private lock file instead of asserting the
        # service is stopped.
        self._queue_dir = tempfile.TemporaryDirectory()
        shared_queue = self.bridge._questions
        self.bridge._questions = QUEUE.RequestQueue(
            Path(self._queue_dir.name) / "question_run.json",
            max_waiters=shared_queue.max_waiters,
            wait_timeout=shared_queue.wait_timeout,
            logger=shared_queue.logger,
        )

    async def asyncTearDown(self):
        await self.bridge.terminate()
        self.bridge._questions.release()
        self._queue_dir.cleanup()

    async def test_terminate_cancels_and_awaits_all_owned_tasks(self):
        cleaned = []

        async def worker(label):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(label)

        tasks = [self.bridge._spawn_background(worker(str(i))) for i in range(4)]
        self.bridge._idle_task, self.bridge._hot_task = tasks[:2]
        await asyncio.sleep(0)
        await self.bridge.terminate()
        self.assertTrue(all(task.done() and task.cancelled() for task in tasks))
        self.assertEqual(set(cleaned), {"0", "1", "2", "3"})
        self.assertEqual(self.bridge._background_tasks, set())
        self.assertTrue(self.bridge._stopped)
        self.assertIsNone(self.bridge._idle_task)
        self.assertIsNone(self.bridge._hot_task)

    async def test_completed_tasks_are_removed_and_shutdown_rejects_new_work(self):
        task = self.bridge._spawn_background(asyncio.sleep(0))
        await task
        await asyncio.sleep(0)
        self.assertNotIn(task, self.bridge._background_tasks)
        await self.bridge.terminate()
        coro = asyncio.sleep(0)
        self.assertIsNone(self.bridge._spawn_background(coro))
        with self.assertRaises(RuntimeError):
            coro.send(None)

    async def test_cancel_resistant_task_does_not_make_shutdown_unbounded(self):
        release = asyncio.Event()
        entered = asyncio.Event()
        cancellations = []

        async def worker():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellations.append(True)

        task = self.bridge._spawn_background(worker())
        await entered.wait()
        actual_wait = asyncio.wait

        async def bounded_wait(tasks, *, timeout=None):
            return await actual_wait(tasks, timeout=min(timeout, 0.02))

        try:
            with patch.object(asyncio, "wait", side_effect=bounded_wait):
                await asyncio.wait_for(self.bridge.terminate(), 0.3)
                self.assertIn(task, self.bridge._background_tasks)
                self.assertFalse(task.done())
                await asyncio.wait_for(self.bridge.terminate(), 0.3)
                self.assertEqual(len(cancellations), 1)
        finally:
            release.set()
            await asyncio.wait_for(task, 1)
        await asyncio.sleep(0)
        self.assertNotIn(task, self.bridge._background_tasks)

    async def test_pending_memory_prevents_successful_unload_and_remains_shared(self):
        pending = asyncio.create_task(asyncio.Event().wait())
        self.bridge._memory._io_tasks = {pending}
        self.bridge._memory.close = AsyncMock(return_value=False)
        try:
            with self.assertRaisesRegex(RuntimeError, "Memory cleanup"):
                await self.bridge.terminate()
            self.assertIs(
                self.context._workbuddy_bridge_memory_drain, self.bridge._memory
            )
            self.assertFalse(pending.cancelled())
            self.bridge._memory.close.assert_awaited_once_with(timeout=5)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            self.bridge._memory._io_tasks.clear()
            self.bridge._memory.close.return_value = True
        await self.bridge.terminate()
        self.assertFalse(hasattr(self.context, "_workbuddy_bridge_memory_drain"))

    async def test_reading_persona_without_touch_does_not_extend_roleplay(self):
        event = types.SimpleNamespace(get_group_id=lambda: "g")
        self.bridge._roleplay["g"] = {"persona": "test persona", "ts": 100}
        with patch.object(time, "time", return_value=150):
            self.assertEqual(
                self.bridge._current_persona(event, touch=False), "test persona"
            )
        self.assertEqual(self.bridge._roleplay["g"]["ts"], 100)
        with patch.object(time, "time", return_value=161):
            self.assertEqual(self.bridge._current_persona(event, touch=False), "")
        self.assertNotIn("g", self.bridge._roleplay)
        self.bridge._roleplay["g"] = {"persona": "test persona", "ts": 200}
        with patch.object(time, "time", return_value=250):
            self.assertEqual(self.bridge._current_persona(event), "test persona")
        self.assertEqual(self.bridge._roleplay["g"]["ts"], 250)

    async def test_foreground_question_updates_dialogue_clock_but_background_does_not(
        self,
    ):
        self.bridge._ask_gateway = AsyncMock(return_value="answer")
        clock = self.bridge._memory.note_dialogue
        self.assertEqual(await self.bridge._ask("question", "local-scope"), "answer")
        self.assertEqual(clock.call_count, 2)
        clock.reset_mock()
        self.assertEqual(
            await self.bridge._ask("background", "local-scope", background=True),
            "answer",
        )
        clock.assert_not_called()

    async def test_foreground_queue_failure_also_marks_end_of_interaction(self):
        self.bridge._questions.acquire = AsyncMock(return_value="full")
        clock = self.bridge._memory.note_dialogue
        self.assertEqual(
            await self.bridge._ask("question", "local-scope"), "[队列已满]"
        )
        self.assertEqual(clock.call_count, 2)

    async def test_all_active_send_routes_respect_pause_stop_and_account(self):
        for blocked in ("paused", "stopped", "account"):
            self.namespace["_is_paused"] = lambda blocked=blocked: blocked == "paused"
            self.bridge._stopped = blocked == "stopped"
            self.bridge._account_ok = blocked != "account"
            self.assertFalse(await self.bridge._send_to_master("text", "test"))
            self.assertFalse(await self.bridge._send_to_session("test:group:g", "text"))
            self.assertFalse(
                await self.bridge._send_chain_to_session(
                    "test:group:g", MessageChain([Plain("text")])
                )
            )
        self.context.send_message.assert_not_awaited()
        self.namespace["_is_paused"] = lambda: False
        self.bridge._stopped = False
        self.bridge._account_ok = True
        self.assertTrue(await self.bridge._send_to_master("text", "test"))
        self.assertTrue(await self.bridge._send_to_session("test:group:g", "text"))
        self.assertTrue(
            await self.bridge._send_chain_to_session(
                "test:group:g", MessageChain([Plain("text")])
            )
        )
        self.assertEqual(self.context.send_message.await_count, 3)

    async def test_inflight_generation_cannot_send_after_pause(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def ask(*_args, **_kwargs):
            started.set()
            await release.wait()
            return "a late reply"

        self.bridge._ask = ask
        task = self.bridge._spawn_background(
            self.bridge._speak_spark("g", "test:group:g", "prompt", "idle")
        )
        await started.wait()
        self.namespace["_is_paused"] = lambda: True
        release.set()
        await task
        self.context.send_message.assert_not_awaited()
        self.assertEqual(self.bridge._spark_said, {})

    async def test_legitimate_silence_never_opens_the_circuit(self):
        self.bridge._spark_fail_streak = 2
        for reply in (
            "",
            "  ",
            "（不回复）",
            "（本轮不接，跳过。）",
            "[无输出]",
            "[无回复]",
        ):
            with self.subTest(reply=reply):
                self.bridge._ask = AsyncMock(return_value=reply)
                self.assertIsNone(
                    await self.bridge._speak_spark(
                        "g", "test:group:g", "prompt", "idle"
                    )
                )
                self.assertEqual(self.bridge._spark_fail_streak, 0)
        self.context.send_message.assert_not_awaited()

    async def test_queueing_does_not_increment_failures_but_real_errors_do(self):
        self.bridge._spark_fail_streak = 1
        self.bridge._ask = AsyncMock(return_value="[排队] waiting")
        for _ in range(4):
            await self.bridge._speak_spark("g", "test:group:g", "prompt", "idle")
        self.assertEqual(self.bridge._spark_fail_streak, 1)
        self.bridge._spark_fail_streak = 0
        for reply in (
            "[网关不可达] offline",
            "[凭据失效] rejected",
            "Error: 429 exhausted",
        ):
            self.bridge._ask = AsyncMock(return_value=reply)
            await self.bridge._speak_spark("g", "test:group:g", "prompt", "idle")
        self.assertEqual(self.bridge._spark_fail_streak, 3)
        self.bridge._spark_chat = AsyncMock()
        await self.bridge._idle_scan()
        self.bridge._spark_chat.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_live_history_uses_the_same_expiry_as_cached_history(self):
        now = time.time()
        old = {"text": "old topic", "name": "person", "ts": now - 3600}
        fresh = {"text": "fresh topic", "name": "person", "ts": now}
        self.bridge._fetch_recent_messages = AsyncMock(return_value=([old], ""))
        self.assertIsNone(await self.bridge._spark_material("g", 1, "idle"))
        self.bridge._fetch_recent_messages = AsyncMock(return_value=([old, fresh], ""))
        hist, text = await self.bridge._spark_material("g", 1, "idle")
        self.assertEqual(hist, [fresh])
        self.assertNotIn("old topic", text)
        self.assertIn("fresh topic", text)

    async def test_same_user_same_second_attachments_keep_distinct_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            one, two = root / "one.png", root / "two.png"
            one.write_bytes(b"first image")
            two.write_bytes(b"second image")
            self.namespace["ATTACHMENT_ROOT"] = root / "saved"
            self.namespace["_materialize_attachment"] = AsyncMock(
                side_effect=[str(one), str(two)]
            )
            self.bridge._referenced_image_comps = lambda event: []
            event = types.SimpleNamespace(
                attachments=[object()], is_private_chat=lambda: False
            )
            with patch.object(time, "strftime", return_value="20261022_140000"):
                first = await self.bridge._collect_attachments(event, "name", "user")
                second = await self.bridge._collect_attachments(event, "name", "user")
            self.assertNotEqual(first, second)
            self.assertEqual(Path(first[0]).read_bytes(), b"first image")
            self.assertEqual(Path(second[0]).read_bytes(), b"second image")


if __name__ == "__main__":
    unittest.main()
