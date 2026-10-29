"""Exercise production admission and HTTP handling with an offline transport."""

import asyncio
import json
import re
import tempfile
import threading
import time
import types
import unittest
import urllib.parse
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from bridge_test_support import PLUGIN, load_definitions, load_module

MEME_LIBRARY = load_module(
    PLUGIN.with_name("meme_library.py"), "test_question_scheduling_memes"
)
MEME_COLLECTION = load_module(
    PLUGIN.with_name("meme_collection.py"), "test_question_scheduling_collection"
)

QUEUE = load_module(PLUGIN.with_name("request_queue.py"), "question_queue_integration")
BASE = "http://127.0.0.1:19876"


class Response:
    """Provide an asynchronous HTTP response without opening a socket."""

    def __init__(
        self,
        *,
        status=200,
        body="",
        lines=(),
        gate=None,
        entered=None,
        enter_gate=None,
        enter_event=None,
    ):
        self.status = status
        self.body = body
        self.lines = list(lines)
        self.gate = gate
        self.entered = entered
        self.enter_gate = enter_gate
        self.enter_event = enter_event
        self.content = self

    async def __aenter__(self):
        if self.enter_event is not None:
            self.enter_event.set()
        if self.enter_gate is not None:
            await self.enter_gate.wait()
        if isinstance(self.body, Exception):
            raise self.body
        return self

    async def __aexit__(self, *_args):
        return None

    async def text(self):
        return self.body

    async def readline(self):
        if self.entered is not None:
            self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return self.lines.pop(0) if self.lines else b""


class Transport:
    """Record submissions and emulate remote lifecycle and SSE output."""

    def __init__(self):
        self.posts = []
        self.queries = []
        self.runs = {}
        self.statuses = {}
        self.run_errors = {}
        self.gateway_states = {}
        self.authenticated = {}
        self.status_queries = []
        self.status_hook = None
        self.live = set()
        self.max_live = 0
        self.first_gate = None
        self.stream_entered = asyncio.Event()
        self.post_error = None
        self.post_gate = None
        self.post_entered = asyncio.Event()
        self.post_missing_id = False
        self.reply_style = "normal"
        self.session_count = 0

    def client(self, **_kwargs):
        self.session_count += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def post(self, _url, *, json: dict, headers):
        self.posts.append(json)
        if self.post_gate is not None:
            return Response(enter_gate=self.post_gate, enter_event=self.post_entered)
        if self.post_error is not None:
            return Response(body=self.post_error)
        if self.post_missing_id:
            return Response(status=202, body='{"data": {}}')
        run_id = f"run-{len(self.posts)}"
        self.runs[run_id] = json
        self.live.add(run_id)
        self.max_live = max(self.max_live, len(self.live))
        return Response(status=202, body=f'{{"data": {{"runId": "{run_id}"}}}}')

    def get(self, url, **_kwargs):
        if url.endswith("/api/v1/auth/status"):
            base = url.removesuffix("/api/v1/auth/status")
            return Response(
                body=json.dumps({"authenticated": self.authenticated.get(base, True)})
            )
        if url.endswith("/api/v1/status"):
            base = url.removesuffix("/api/v1/status")
            self.status_queries.append(base)
            if self.status_hook is not None:
                self.status_hook()
            state = self.gateway_states.get(
                base, {"status": "ok", "busy": False, "runStatus": "idle"}
            )
            if state is None:
                return Response(status=503)
            return Response(body=json.dumps({"data": state}))
        if url.endswith("/stream"):
            run_id = url.rsplit("/", 2)[-2]
            body = self.runs[run_id]
            receipt = body["text"].splitlines()[0]
            origin = body["source"]["conversation"]["id"]
            reply = {
                "normal": receipt + "\nreply-" + origin,
                "silent": receipt,
                "wrong": "foreign output without this request's receipt",
            }[self.reply_style]
            event = {
                "status": "completed",
                "replyTo": body["id"],
                "content": {"markdown": reply},
            }
            return Response(
                lines=[("data: " + json.dumps(event) + "\n").encode(), b""],
                gate=self.first_gate if run_id == "run-1" else None,
                entered=self.stream_entered,
            )
        run_id = urllib.parse.unquote(url.rsplit("/", 1)[-1])
        self.queries.append(run_id)
        if run_id in self.run_errors:
            return Response(status=self.run_errors[run_id])
        active = self.statuses.get(run_id, False)
        if active is None:
            return Response(status=503, body="unavailable")
        if active is False:
            self.live.discard(run_id)
        return Response(body=json.dumps({"data": {"active": active}}))


def load_bridge(transport, paused):
    """Compile the owned request methods with exclusively offline dependencies.

    Args:
        transport: HTTP simulation for this test.
        paused: Callable representing the maintenance switch.

    Returns:
        The bridge class and the globals used by its production methods.
    """
    methods = {
        "_ask",
        "_ask_gateway",
        "_run_still_active",
        "_gateway_idle",
        "_memory_local_idle",
        "_memory_idle_status",
        "_parse_run_active",
        "_stream",
        "terminate",
        "_spawn_background",
        "_speak_spark",
        "_note_group_activity",
        "_can_send_active",
        "_handle_question_recovery",
        "_gag_critique",
        "_record_question_outcome",
    }
    functions = {
        "_strip_receipt",
        "_receipt_gate",
        "_is_empty_reply",
        "_is_gateway_error",
        "_is_meta_note",
        "_question_notice",
        "_auth_ok",
    }
    constants = {
        "EMPTY_REPLY_MARKERS",
        "META_NOTE_PREFIXES",
        "META_NOTE_MARKERS",
        "_GATEWAY_ERROR_RE",
        "PENDING_TTL_SEC",
    }
    namespace = {
        "asyncio": asyncio,
        "meme_library": MEME_LIBRARY,
        "meme_collection": MEME_COLLECTION,
        "time": time,
        "uuid": uuid,
        "urllib": urllib,
        "json": json,
        "re": re,
        "Path": Path,
        "logger": Mock(),
        "_GW_CACHE": {"base": None, "state": "unknown", "checked_at": 0.0},
        "_is_paused": paused,
        "_cfg": lambda: (19876, "offline-credential"),
        "_resolve_base": AsyncMock(return_value=BASE),
        "_prompt_prefix": lambda *_args, receipt="", **_kwargs: receipt + "\n",
        "aiohttp": types.SimpleNamespace(
            ClientSession=transport.client,
            ClientTimeout=lambda **kwargs: kwargs,
        ),
        "TIMEOUT_SEC": 2,
        "STREAM_TOTAL_SEC": 1,
        "STREAM_IDLE_SEC": 1,
        "SPARK_FAIL_CIRCUIT_N": 3,
        "_conv_id": lambda origin, generation: origin,
        "_sanitize": lambda text: text,
    }
    load_definitions(
        namespace, functions=functions, constants=constants, methods=methods
    )
    return namespace["Bridge"], namespace


class QuestionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Cover the real scheduler, transport path and correlation checks together."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.transport = Transport()
        self.paused = False
        self.Bridge, self.globals = load_bridge(self.transport, lambda: self.paused)
        self.instances = []
        self.bridge = self.new_bridge()

    def new_bridge(self, *, wait=1, limit=5):
        bridge = self.Bridge()
        bridge._stopped = False
        bridge._questions = QUEUE.RequestQueue(
            self.root / "remote.json", max_waiters=limit, wait_timeout=wait
        )
        bridge._background_tasks = set()
        bridge._question_operations = set()
        bridge._cleanup_stop = threading.Event()
        bridge._write_runtime_status = Mock()
        bridge._idle_task = bridge._hot_task = None
        bridge._account_ok = True
        bridge._spark_fail_streak = 0
        bridge._group_activity = {}
        bridge._spark_said = {}
        bridge._draw_busy = set()
        bridge._draw_running = set()
        bridge._pending = {}
        bridge._draw_jobs = types.SimpleNamespace(
            directory=self.root / "draw_jobs", list_jobs=lambda: []
        )
        bridge._cfg_int = lambda key, default: default
        bridge._send_to_session = AsyncMock(return_value=True)
        self.instances.append(bridge)
        return bridge

    async def asyncTearDown(self):
        for bridge in self.instances:
            await bridge.terminate()
            bridge._questions.release()

    async def seed_remote(self, run_id="old-run"):
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        self.bridge._questions.save_remote(
            {
                "base": BASE,
                "request_id": "old-request",
                "run_id": run_id,
                "started_at": time.time(),
            }
        )
        self.bridge._questions.release()

    async def wait_for_waiters(self, count):
        for _ in range(100):
            if self.bridge._questions.waiting_count == count:
                return
            await asyncio.sleep(0)
        self.fail(f"Expected {count} queued requests")

    async def test_cross_group_fifo_one_remote_run_and_busy_background_skips(self):
        self.transport.first_gate = asyncio.Event()
        first = asyncio.create_task(self.bridge._ask("one", "group-a"))
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        second = asyncio.create_task(self.bridge._ask("two", "group-b"))
        third = asyncio.create_task(self.bridge._ask("three", "group-c"))
        await self.wait_for_waiters(2)
        self.assertEqual(
            await self.bridge._ask("idle", "group-d", background=True), "[忙碌跳过]"
        )
        self.assertEqual(len(self.transport.posts), 1)
        self.transport.first_gate.set()
        self.assertEqual(
            await asyncio.gather(first, second, third),
            ["reply-group-a", "reply-group-b", "reply-group-c"],
        )
        self.assertEqual(
            [p["source"]["conversation"]["id"] for p in self.transport.posts],
            ["group-a", "group-b", "group-c"],
        )
        self.assertEqual(self.transport.max_live, 1)

    async def test_full_queue_cancelled_waiter_and_wait_timeout_never_post(self):
        self.bridge._questions.max_waiters = 1
        self.bridge._questions.wait_timeout = 0.04
        self.transport.first_gate = asyncio.Event()
        first = asyncio.create_task(self.bridge._ask("one", "a"))
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        cancelled = asyncio.create_task(self.bridge._ask("cancel", "b"))
        await self.wait_for_waiters(1)
        self.assertEqual(await self.bridge._ask("full", "c"), "[队列已满]")
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.assertEqual(await self.bridge._ask("expire", "d"), "[排队超时]")
        self.transport.first_gate.set()
        await first
        self.assertEqual(len(self.transport.posts), 1)
        self.assertFalse(self.bridge._questions.busy)

    async def test_close_wakes_human_waiters_and_rejects_new_requests(self):
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        waiting = asyncio.create_task(self.bridge._ask("queued", "g"))
        await self.wait_for_waiters(1)
        await self.bridge.terminate()
        self.assertEqual(await waiting, "[维护暂停]")
        self.assertEqual(await self.bridge._ask("late", "g"), "[维护暂停]")
        self.assertEqual(self.transport.session_count, 0)

    async def test_correct_empty_receipt_is_silence_but_wrong_receipt_is_failure(self):
        self.transport.reply_style = "silent"
        self.assertEqual(await self.bridge._ask("silent", "g"), "[静默]")
        self.transport.reply_style = "wrong"
        self.assertEqual(await self.bridge._ask("wrong", "g"), "[回复校验失败]")
        self.assertIsNone(self.bridge._questions.read_remote())

    async def test_verified_foreground_reply_recovers_proactive_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.assertEqual(await self.bridge._ask("recover", "g"), "reply-g")
        self.assertIsNone(self.bridge._questions.read_remote())
        self.assertEqual(self.bridge._spark_fail_streak, 0)

    async def test_verified_foreground_silence_recovers_proactive_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.transport.reply_style = "silent"
        self.assertEqual(await self.bridge._ask("recover", "g"), "[静默]")
        self.assertIsNone(self.bridge._questions.read_remote())
        self.assertEqual(self.bridge._spark_fail_streak, 0)

    async def test_wrong_receipt_does_not_recover_proactive_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.transport.reply_style = "wrong"
        self.assertEqual(await self.bridge._ask("recover", "g"), "[回复校验失败]")
        self.assertEqual(self.bridge._spark_fail_streak, 3)

    async def test_approved_execution_does_not_recover_proactive_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.assertEqual(
            await self.bridge._ask(
                "execute", "g", master=True, mode="execute", out_dir=str(self.root)
            ),
            "reply-g",
        )
        self.assertEqual(self.bridge._spark_fail_streak, 3)

    async def test_reply_with_remote_work_still_active_does_not_recover_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.transport.statuses["run-1"] = True
        self.assertEqual(await self.bridge._ask("recover", "g"), "reply-g")
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.assertEqual(self.bridge._spark_fail_streak, 3)

    async def test_reply_with_unknown_remote_status_does_not_recover_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.transport.statuses["run-1"] = None
        self.assertEqual(await self.bridge._ask("recover", "g"), "reply-g")
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.assertEqual(self.bridge._spark_fail_streak, 3)

    async def test_background_transport_reply_does_not_recover_foreground_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.assertEqual(
            await self.bridge._ask("background", "g", background=True), "reply-g"
        )
        self.assertEqual(self.bridge._spark_fail_streak, 3)

    async def test_admission_failure_does_not_recover_proactive_circuit(self):
        self.bridge._spark_fail_streak = 3
        self.bridge._questions.max_waiters = 0
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        try:
            self.assertEqual(await self.bridge._ask("full", "g"), "[队列已满]")
            self.assertEqual(self.bridge._spark_fail_streak, 3)
            self.assertEqual(self.transport.posts, [])
        finally:
            self.bridge._questions.release()

    async def test_missing_credentials_record_status_without_opening_transport(self):
        self.globals["_cfg"] = lambda: (None, "")
        reply = await self.bridge._ask("private question", "g")
        self.assertTrue(reply.startswith("[未配置]"))
        self.assertEqual(self.bridge._question_last_outcome, "[未配置]")
        self.assertEqual(self.bridge._question_last_failure, "[未配置]")
        self.assertEqual(self.globals["_GW_CACHE"]["state"], "not_configured")
        self.assertEqual(self.transport.session_count, 0)
        self.globals["_resolve_base"].assert_not_awaited()

    async def test_outcome_evidence_contains_categories_and_preserves_last_failure(
        self,
    ):
        self.bridge._record_question_outcome("[连接失败] private error detail")
        failed_at = self.bridge._question_last_failure_at
        self.assertEqual(self.bridge._question_last_outcome, "[连接失败]")
        self.assertEqual(self.bridge._question_last_failure, "[连接失败]")
        self.bridge._record_question_outcome("private successful reply")
        self.assertEqual(self.bridge._question_last_outcome, "reply")
        self.assertEqual(self.bridge._question_last_failure, "[连接失败]")
        self.assertEqual(self.bridge._question_last_failure_at, failed_at)

    async def test_prior_active_or_unknown_run_never_allows_new_submission(self):
        await self.seed_remote()
        self.bridge._questions.wait_timeout = 0.03
        self.transport.statuses["old-run"] = True
        self.assertEqual(await self.bridge._ask("new", "g"), "[排队超时]")
        self.transport.statuses["old-run"] = None
        self.assertEqual(await self.bridge._ask("new", "g"), "[状态待确认]")
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.bridge._questions.read_remote()["run_id"], "old-run")

    async def test_explicit_remote_completion_allows_next_submission(self):
        await self.seed_remote()
        self.transport.statuses["old-run"] = False
        self.assertEqual(await self.bridge._ask("next", "g"), "reply-g")
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(self.transport.queries[0], "old-run")
        self.assertIsNone(self.bridge._questions.read_remote())

    async def test_missing_run_with_busy_underlying_session_retains_ownership(self):
        await self.seed_remote()
        self.transport.statuses["old-run"] = False
        self.transport.gateway_states[BASE] = {
            "status": "ok",
            "busy": True,
            "runStatus": "running",
        }
        self.assertEqual(
            await self.bridge._ask("later", "g", background=True), "[忙碌跳过]"
        )
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.bridge._questions.read_remote()["run_id"], "old-run")

    async def test_run_404_is_not_completion_even_when_session_is_idle(self):
        await self.seed_remote()
        self.transport.run_errors["old-run"] = 404
        self.assertEqual(await self.bridge._ask("later", "g"), "[状态待确认]")
        self.assertEqual(self.transport.posts, [])
        self.assertIsNotNone(self.bridge._questions.read_remote())

    async def test_moved_gateway_recovers_only_from_authenticated_old_idle_endpoint(
        self,
    ):
        await self.seed_remote()
        moved = "http://127.0.0.1:19877"
        self.globals["_resolve_base"].return_value = moved
        self.transport.authenticated[BASE] = False
        self.assertEqual(await self.bridge._ask("later", "g"), "[状态待确认]")
        self.assertEqual(self.transport.queries, [])
        self.assertEqual(self.transport.posts, [])
        self.assertEqual(self.bridge._questions.read_remote()["base"], BASE)
        self.transport.authenticated[BASE] = True
        self.transport.gateway_states[BASE] = None
        self.assertEqual(await self.bridge._ask("later", "g"), "[状态待确认]")
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": False}
        self.assertEqual(await self.bridge._ask("later", "g"), "reply-g")
        self.assertEqual(len(self.transport.posts), 1)
        self.assertIsNone(self.bridge._questions.read_remote())

    async def test_gateway_idle_requires_explicit_boolean_and_consistent_status(self):
        cases = [
            ({"status": "ok", "busy": False}, True),
            ({"status": "ok", "busy": False, "runStatus": "idle"}, True),
            ({"status": "ok", "busy": True}, False),
            ({"status": "ok", "busy": False, "runStatus": "running"}, None),
            ({"status": "ok", "busy": 0}, None),
            ({"status": "ok", "busy": "false"}, None),
            ({"busy": False}, None),
            ([], None),
            (None, None),
        ]
        for state, expected in cases:
            with self.subTest(state=state):
                self.transport.gateway_states[BASE] = state
                self.assertIs(
                    await self.bridge._gateway_idle(self.transport, BASE, "offline"),
                    expected,
                )

    async def test_memory_idle_preflight_checks_local_work_without_model_submissions(
        self,
    ):
        self.assertEqual(await self.bridge._memory_idle_status(), "ready")
        self.assertFalse(self.bridge._questions.busy)
        self.bridge._draw_busy.add("fixture")
        self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        self.bridge._draw_busy.clear()
        self.bridge._draw_running.add("fixture")
        self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        self.bridge._draw_running.clear()
        self.bridge._pending = {"fixture": [{"ts": time.time()}]}
        self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        self.bridge._pending = {"fixture": [{"ts": time.time() - 3600}]}
        self.assertEqual(await self.bridge._memory_idle_status(), "ready")
        self.assertEqual(self.transport.posts, [])

    async def test_memory_idle_checks_durable_image_jobs_and_damaged_records(self):
        jobs = self.bridge._draw_jobs
        jobs.directory.mkdir()
        record = jobs.directory / "fixture.json"
        record.write_text("{}", encoding="utf-8")
        self.assertEqual(await self.bridge._memory_idle_status(), "idle_unverified")
        for state in (
            "queued",
            "submitting",
            "submitted",
            "ready",
            "sending",
            "submission_unknown",
            "delivery_unknown",
        ):
            with self.subTest(state=state):
                jobs.list_jobs = lambda: [{"id": "fixture", "state": state}]
                self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        jobs.list_jobs = lambda: [{"id": "fixture", "state": "delivered"}]
        self.assertEqual(await self.bridge._memory_idle_status(), "ready")
        jobs.list_jobs = Mock(side_effect=OSError("unavailable"))
        self.assertEqual(await self.bridge._memory_idle_status(), "idle_unverified")
        self.assertEqual(self.transport.posts, [])

    async def test_memory_idle_keeps_unknown_run_and_recovers_only_completed_idle(self):
        await self.seed_remote()
        self.transport.statuses["old-run"] = True
        self.assertEqual(
            await self.bridge._memory_idle_status(), "pending_confirmation"
        )
        self.transport.statuses["old-run"] = False
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": True}
        self.assertEqual(
            await self.bridge._memory_idle_status(), "pending_confirmation"
        )
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": False}
        self.assertEqual(await self.bridge._memory_idle_status(), "ready")
        self.assertIsNone(self.bridge._questions.read_remote())
        self.assertEqual(self.transport.posts, [])

    async def test_memory_idle_gateway_missing_or_unknown_cannot_admit(self):
        self.globals["_resolve_base"].return_value = None
        self.assertEqual(await self.bridge._memory_idle_status(), "gateway_unreachable")
        self.globals["_resolve_base"].return_value = BASE
        self.transport.gateway_states[BASE] = None
        self.assertEqual(await self.bridge._memory_idle_status(), "idle_unverified")
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": True}
        self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        self.assertFalse(self.bridge._questions.busy)
        self.assertEqual(self.transport.posts, [])

    async def test_memory_idle_checks_share_human_lock_and_release_on_cancellation(
        self,
    ):
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        self.assertEqual(await self.bridge._memory_idle_status(), "not_idle")
        self.bridge._questions.release()
        entered, release = asyncio.Event(), asyncio.Event()

        async def resolving(*_args):
            entered.set()
            await release.wait()
            return BASE

        self.globals["_resolve_base"] = resolving
        checking = asyncio.create_task(self.bridge._memory_idle_status())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertTrue(self.bridge._questions.busy)
        checking.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await checking
        self.assertFalse(self.bridge._questions.busy)
        self.assertEqual(self.transport.posts, [])

    async def test_memory_idle_moved_endpoint_unknown_keeps_prior_record(self):
        await self.seed_remote()
        self.globals["_resolve_base"].return_value = "http://127.0.0.1:19877"
        before = self.bridge._questions.read_remote()
        self.transport.authenticated[BASE] = False
        self.assertEqual(
            await self.bridge._memory_idle_status(), "pending_confirmation"
        )
        self.assertEqual(self.bridge._questions.read_remote(), before)
        self.transport.authenticated[BASE] = True
        self.transport.run_errors["old-run"] = 404
        self.assertEqual(
            await self.bridge._memory_idle_status(), "pending_confirmation"
        )
        self.assertEqual(self.bridge._questions.read_remote(), before)
        self.transport.run_errors.clear()
        self.assertEqual(await self.bridge._memory_idle_status(), "ready")
        self.assertIsNone(self.bridge._questions.read_remote())
        self.assertEqual(self.transport.posts, [])

    async def test_wrapper_timeout_with_busy_session_blocks_next_submission(self):
        self.transport.first_gate = asyncio.Event()
        self.globals["STREAM_TOTAL_SEC"] = 0.03
        self.globals["STREAM_IDLE_SEC"] = 0.03
        self.transport.statuses["run-1"] = False
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": True}
        self.assertIn(await self.bridge._ask("first", "g"), ("[超时]", "[排队]"))
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.assertEqual(
            await self.bridge._ask("later", "g", background=True), "[忙碌跳过]"
        )
        self.assertEqual(len(self.transport.posts), 1)
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": False}
        self.transport.first_gate.set()
        self.assertEqual(await self.bridge._ask("later", "g"), "reply-g")
        self.assertEqual(len(self.transport.posts), 2)

    async def test_memory_submission_rechecks_idle_instead_of_remote_queueing(self):
        label = "qq-memory-0123456789ab"
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": True}
        self.assertEqual(
            await self.bridge._ask("memory", label, background=True), "[任务忙碌]"
        )
        self.transport.gateway_states[BASE] = None
        self.assertEqual(
            await self.bridge._ask("memory", label, background=True), "[空闲未确认]"
        )
        self.assertEqual(self.transport.posts, [])
        self.assertIsNone(self.bridge._questions.read_remote())
        self.transport.gateway_states[BASE] = {"status": "ok", "busy": False}
        self.transport.status_hook = lambda: self.bridge._draw_busy.add("new-job")
        self.assertEqual(
            await self.bridge._ask("memory", label, background=True), "[任务忙碌]"
        )
        self.assertEqual(self.transport.posts, [])
        self.transport.status_hook = None
        self.bridge._draw_busy.clear()
        self.assertEqual(
            await self.bridge._ask("memory", label, background=True), "reply-" + label
        )
        self.assertEqual(len(self.transport.posts), 1)

    async def test_cancel_after_submission_preserves_run_for_new_instance(self):
        self.transport.first_gate = asyncio.Event()
        self.transport.statuses["run-1"] = True
        first = asyncio.create_task(self.bridge._ask("first", "g"))
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        other = self.new_bridge(wait=0.03)
        self.assertEqual(other._questions.read_remote()["run_id"], "run-1")
        self.assertEqual(await other._ask("next", "other"), "[排队超时]")
        self.assertEqual(len(self.transport.posts), 1)

    async def test_local_stream_timeout_does_not_clear_remote_occupancy(self):
        self.transport.first_gate = asyncio.Event()
        self.transport.statuses["run-1"] = True
        self.globals["STREAM_TOTAL_SEC"] = 0.03
        self.globals["STREAM_IDLE_SEC"] = 0.03
        result = await self.bridge._ask("first", "g")
        self.assertIn(result, ("[超时]", "[排队]"))
        self.assertEqual(self.bridge._questions.read_remote()["run_id"], "run-1")
        self.assertEqual(
            await self.bridge._ask("idle", "x", background=True), "[忙碌跳过]"
        )
        self.assertEqual(len(self.transport.posts), 1)

    async def test_lost_submission_response_without_id_blocks_replay(self):
        self.transport.post_error = OSError("response lost after submission")
        self.assertEqual(await self.bridge._ask("first", "g"), "[连接失败]")
        self.assertEqual(self.bridge._questions.read_remote()["run_id"], "")
        other = self.new_bridge()
        self.assertEqual(await other._ask("again", "g"), "[状态待确认]")
        self.assertEqual(len(self.transport.posts), 1)

    async def test_shutdown_cancels_unacknowledged_post_and_preserves_remote_guard(
        self,
    ):
        self.transport.post_gate = asyncio.Event()
        task = asyncio.create_task(self.bridge._ask("first", "g"))
        await asyncio.wait_for(self.transport.post_entered.wait(), 1)
        self.assertTrue(self.bridge._question_operations)
        await asyncio.wait_for(self.bridge.terminate(), 0.5)
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.bridge._question_operations)
        self.assertEqual(self.bridge._questions.read_remote()["run_id"], "")
        other = self.new_bridge()
        self.assertEqual(await other._ask("next", "g"), "[状态待确认]")
        self.assertFalse(other._questions.busy)
        self.assertEqual(len(self.transport.posts), 1)

    async def test_missing_acknowledged_id_is_not_treated_as_idle(self):
        self.transport.post_missing_id = True
        self.assertEqual(await self.bridge._ask("first", "g"), "[状态待确认]")
        self.assertEqual(await self.bridge._ask("again", "g"), "[状态待确认]")
        self.assertEqual(len(self.transport.posts), 1)

    async def test_authorization_pause_and_expired_material_never_enter_transport(self):
        self.assertEqual(
            await self.bridge._ask("task", "g", mode="execute", out_dir="output"),
            "[权限拒绝]",
        )
        self.assertEqual(
            await self.bridge._ask("task", "g", mode="execute", master=True),
            "[权限拒绝]",
        )
        self.assertEqual(
            await self.bridge._ask(
                "idle", "g", background=True, not_after=time.monotonic() - 1
            ),
            "[过期跳过]",
        )
        self.paused = True
        self.assertEqual(await self.bridge._ask("task", "g"), "[维护暂停]")
        self.assertEqual(self.transport.session_count, 0)
        self.assertFalse(self.bridge._questions.state_path.exists())

    async def test_proactive_busy_or_stale_material_skips_without_failure_count(self):
        self.bridge._spark_fail_streak = 2
        self.bridge._group_activity["g"] = {"message_version": 3}
        self.assertIsNone(
            await self.bridge._speak_spark(
                "g",
                "fake:group:g",
                "old",
                "idle",
                source_version=2,
            )
        )
        self.assertIsNone(
            await self.bridge._speak_spark(
                "g",
                "fake:group:g",
                "old",
                "idle",
                created_at=time.monotonic() - 31,
            )
        )
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        try:
            self.assertIsNone(
                await self.bridge._speak_spark("g", "fake:group:g", "busy", "idle")
            )
        finally:
            self.bridge._questions.release()
        self.assertEqual(self.bridge._spark_fail_streak, 2)
        self.assertEqual(self.transport.posts, [])
        self.bridge._send_to_session.assert_not_awaited()

    async def test_expired_background_resolution_releases_slot_before_any_post(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        attempts = 0

        async def resolve(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return BASE

        self.globals["_resolve_base"] = resolve
        proactive = asyncio.create_task(
            self.bridge._ask(
                "idle",
                "background-group",
                background=True,
                not_after=time.monotonic() + 0.04,
            )
        )
        await asyncio.wait_for(entered.wait(), 1)
        human = asyncio.create_task(self.bridge._ask("human", "human-group"))
        await self.wait_for_waiters(1)
        self.assertEqual(await asyncio.wait_for(proactive, 0.5), "[过期跳过]")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(await asyncio.wait_for(human, 0.5), "reply-human-group")
        self.assertEqual(len(self.transport.posts), 1)
        self.assertEqual(
            self.transport.posts[0]["source"]["conversation"]["id"], "human-group"
        )
        self.assertFalse(self.bridge._questions.busy)
        self.assertIsNone(self.bridge._questions.read_remote())

    async def test_new_group_message_drops_inflight_proactive_reply(self):
        self.bridge._group_activity["g"] = {"message_version": 3}
        self.transport.first_gate = asyncio.Event()
        task = asyncio.create_task(
            self.bridge._speak_spark("g", "fake:group:g", "idle", "idle")
        )
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        self.bridge._group_activity["g"]["message_version"] = 4
        self.transport.first_gate.set()
        self.assertIsNone(await task)
        self.bridge._send_to_session.assert_not_awaited()
        self.assertEqual(self.bridge._spark_said, {})

    async def test_new_group_message_drops_inflight_critique(self):
        self.bridge._group_activity["g"] = {"message_version": 3}
        self.bridge._gag_history = {"g": [{"sender": "target", "text": "recent"}]}
        event = types.SimpleNamespace(get_group_id=lambda: "g")
        self.transport.first_gate = asyncio.Event()
        task = asyncio.create_task(
            self.bridge._gag_critique(event, {"target_id": "target"}, "fake:group:g")
        )
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        self.bridge._group_activity["g"]["message_version"] = 4
        self.transport.first_gate.set()
        self.assertEqual(await task, "[过期跳过]")
        self.bridge._send_to_session.assert_not_awaited()

    async def test_human_waiting_prevents_delivery_of_inflight_proactive_reply(self):
        self.transport.first_gate = asyncio.Event()
        proactive = asyncio.create_task(
            self.bridge._speak_spark("g", "fake:group:g", "idle", "idle")
        )
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        human = asyncio.create_task(self.bridge._ask("human", "other-group"))
        await self.wait_for_waiters(1)
        self.transport.first_gate.set()
        self.assertIsNone(await proactive)
        self.assertEqual(await human, "reply-other-group")
        self.bridge._send_to_session.assert_not_awaited()
        self.assertEqual(self.transport.max_live, 1)

    async def test_expiry_during_generation_is_not_delivered_or_counted_as_failure(
        self,
    ):
        clock = [time.monotonic()]
        self.globals["time"] = types.SimpleNamespace(
            monotonic=lambda: clock[0], time=time.time
        )
        self.bridge._spark_fail_streak = 2
        self.transport.first_gate = asyncio.Event()
        task = asyncio.create_task(
            self.bridge._speak_spark("g", "fake:group:g", "idle", "idle")
        )
        await asyncio.wait_for(self.transport.stream_entered.wait(), 1)
        clock[0] += 31
        self.transport.first_gate.set()
        self.assertIsNone(await task)
        self.assertEqual(self.bridge._spark_fail_streak, 2)
        self.bridge._send_to_session.assert_not_awaited()

    async def test_current_proactive_reply_returns_only_to_its_original_group(self):
        self.bridge._group_activity["g"] = {
            "message_version": 7,
            "last_ts": time.time() + 100,
        }
        sent = await self.bridge._speak_spark(
            "g",
            "fake:group:g",
            "idle",
            "idle",
            source_version=7,
        )
        self.assertEqual(sent, (True, "reply-fake:group:g"))
        self.bridge._send_to_session.assert_awaited_once_with(
            "fake:group:g", "reply-fake:group:g"
        )
        self.assertEqual(self.bridge._spark_said["g"][0]["text"], "reply-fake:group:g")

    async def test_real_message_advances_version_independently_of_idle_timestamp(self):
        self.bridge.FOLLOW_MIN_TEXT_LEN = 4
        event = types.SimpleNamespace(
            get_group_id=lambda: "g",
            get_message_str=lambda: "new message",
            get_self_id=lambda: "bot",
            unified_msg_origin="fake:group:g",
        )
        self.bridge._note_group_activity(event)
        self.assertEqual(self.bridge._group_activity["g"]["message_version"], 1)
        self.bridge._group_activity["g"]["last_ts"] += 100
        self.bridge._note_group_activity(event)
        self.assertEqual(self.bridge._group_activity["g"]["message_version"], 2)

    async def test_only_explicit_idle_operator_acknowledgement_clears_remote_guard(
        self,
    ):
        await self.seed_remote("")
        exact = "/恢复问答 确认上一轮已结束"
        await self.bridge._handle_question_recovery(exact, False)
        self.assertIsNotNone(self.bridge._questions.read_remote())
        await self.bridge._handle_question_recovery("/恢复问答", True)
        self.assertIsNotNone(self.bridge._questions.read_remote())
        self.assertEqual(await self.bridge._questions.acquire(), "acquired")
        try:
            await self.bridge._handle_question_recovery(exact, True)
            self.assertIsNotNone(self.bridge._questions.read_remote())
        finally:
            self.bridge._questions.release()
        await self.bridge._handle_question_recovery(exact, True)
        self.assertIsNone(self.bridge._questions.read_remote())
        self.assertEqual(self.transport.posts, [])


if __name__ == "__main__":
    unittest.main()
