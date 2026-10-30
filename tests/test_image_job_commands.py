"""Exercise image-job commands without QQ delivery, generation, or live records."""

import asyncio
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from bridge_test_support import PLUGIN, load_definitions, load_module

COMMANDS = load_module(PLUGIN.with_name("image_job_commands.py"), "image_commands_test")
JOBS = load_module(PLUGIN.with_name("image_jobs.py"), "image_command_jobs_test")


class ImageJobCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JOBS.ImageJobs(self.root / "jobs", Mock())
        self.output = self.root / "images"
        self.output.mkdir()
        self.image = self.output / "image.png"
        self.image.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.umo = "fixture:GroupMessage:group-a"
        self.send = AsyncMock(return_value=True)
        self.can_send = Mock(return_value=True)

    def job(self, *, sender="owner", umo=None, state="delivery_unknown", **changes):
        created = self.store.create(
            {
                "group_id": "group-a",
                "umo": umo or self.umo,
                "sender_id": sender,
                "nickname": "private-name",
                "prompt": "private-prompt",
                "ratio": "",
                "settings": {
                    "bin_path": "unused-cli",
                    "output_dir": str(self.output),
                    "model": "unused",
                    "ratio": "1:1",
                    "resolution": "2k",
                    "timeout": 30,
                },
                "deadline_at": time.time() + 30,
                "expires_at": time.time() + 86400,
            }
        )
        return self.store.update(
            created["id"], state=state, path=str(self.image), **changes
        )

    async def command(
        self, text, *, master=True, sender="admin", umo=None, message="1"
    ):
        return await COMMANDS.handle(
            text,
            store=self.store,
            sender_id=sender,
            umo=umo or self.umo,
            master=master,
            can_send=self.can_send,
            send_image=self.send,
            command_id=message,
        )

    def bridge(self):
        namespace = load_definitions(
            {
                "asyncio": asyncio,
                "image_job_commands": COMMANDS,
                "MessageChain": lambda **kwargs: types.SimpleNamespace(**kwargs),
                "Plain": lambda text: types.SimpleNamespace(text=text),
                "Image": lambda **kwargs: types.SimpleNamespace(**kwargs),
                "logger": Mock(),
            },
            methods={"_handle_image_job_cmd", "_spawn_background", "terminate"},
        )
        bridge = namespace["Bridge"]()
        bridge._draw_jobs = self.store
        bridge._stopped = False
        bridge._background_tasks = set()
        bridge._question_operations = set()
        bridge._idle_task = bridge._hot_task = None
        bridge._cleanup_stop = Mock()
        bridge._questions = Mock()
        bridge._write_runtime_status = Mock()
        bridge._can_send_active = lambda: not bridge._stopped
        bridge._send_chain_to_session = AsyncMock(return_value=True)
        self.addAsyncCleanup(bridge.terminate)
        return bridge

    async def test_bridge_wrapper_keeps_original_destination_and_transport_exceptions(
        self,
    ):
        job = self.job()
        bridge = self.bridge()
        event = types.SimpleNamespace(
            unified_msg_origin=self.umo,
            message_obj=types.SimpleNamespace(message_id="original-message"),
        )

        async def send(umo, chain, *, raise_on_error):
            self.assertEqual(umo, self.umo)
            self.assertEqual(chain.chain[1].file, str(self.image.resolve()))
            self.assertTrue(raise_on_error)
            self.assertIn(asyncio.current_task(), bridge._background_tasks)
            raise TimeoutError("fixture unknown delivery")

        bridge._send_chain_to_session.side_effect = send
        reply = await bridge._handle_image_job_cmd(
            event, f"/生图处理 {job['id']} 补发确认", "admin", True
        )
        self.assertIn("仍未确认", reply)
        self.assertEqual(self.store.get(job["id"])["state"], "delivery_unknown")
        await asyncio.sleep(0)
        self.assertEqual(bridge._background_tasks, set())
        with self.store.claim(job["id"]) as acquired:
            self.assertTrue(acquired)

    async def test_bridge_shutdown_cancels_tracked_send_and_releases_job_lock(self):
        job = self.job()
        bridge = self.bridge()
        event = types.SimpleNamespace(
            unified_msg_origin=self.umo,
            get_message_id=lambda: "method-message-id",
            message_obj=types.SimpleNamespace(message_id="fallback-id"),
        )
        entered = asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        bridge._send_chain_to_session.side_effect = send
        operation = asyncio.create_task(
            bridge._handle_image_job_cmd(
                event, f"/生图处理 {job['id']} 补发确认", "admin", True
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertEqual(len(bridge._background_tasks), 1)
            self.assertEqual(self.store.get(job["id"])["state"], "sending")
            await asyncio.wait_for(bridge.terminate(), 1)
            with self.assertRaises(asyncio.CancelledError):
                await operation
            self.assertEqual(self.store.get(job["id"])["state"], "sending")
            self.assertEqual(bridge._background_tasks, set())
            with self.store.claim(job["id"]) as acquired:
                self.assertTrue(acquired)
        finally:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    async def test_bridge_refuses_new_operation_after_shutdown(self):
        job = self.job()
        bridge = self.bridge()
        await bridge.terminate()
        event = types.SimpleNamespace(unified_msg_origin=self.umo)
        reply = await bridge._handle_image_job_cmd(
            event, f"/生图处理 {job['id']} 结束", "admin", True
        )
        self.assertIn("正在停止", reply)
        self.assertEqual(self.store.get(job["id"]), job)
        self.assertEqual(bridge._background_tasks, set())

    async def test_bridge_uses_original_identity_and_message_id_with_fallback(self):
        bridge = self.bridge()
        event = types.SimpleNamespace(
            unified_msg_origin=self.umo,
            get_message_id=lambda: "preferred-id",
            message_obj=types.SimpleNamespace(message_id="fallback-id"),
        )
        with patch.object(
            COMMANDS, "handle", new=AsyncMock(return_value="handled")
        ) as handle:
            self.assertEqual(
                await bridge._handle_image_job_cmd(event, "/生图任务", "owner", False),
                "handled",
            )
            args = handle.await_args.kwargs
            self.assertEqual(args["umo"], self.umo)
            self.assertEqual(args["sender_id"], "owner")
            self.assertFalse(args["master"])
            self.assertEqual(args["command_id"], "preferred-id")
            event.get_message_id = lambda: ""
            await bridge._handle_image_job_cmd(event, "/生图任务", "owner", False)
            self.assertEqual(handle.await_args.kwargs["command_id"], "fallback-id")

    async def test_unrelated_messages_are_not_consumed(self):
        self.assertIsNone(await self.command("hello"))
        self.assertIsNone(await self.command("/生图任务不是命令"))

    async def test_nonadmin_queries_are_scoped_to_both_owner_and_conversation(self):
        mine = self.job()
        another = self.job(sender="other")
        elsewhere = self.job(umo="fixture:GroupMessage:group-b")
        reply = await self.command("/生图任务", master=False, sender="owner")
        self.assertIn(mine["id"][:12], reply)
        self.assertNotIn(another["id"][:12], reply)
        self.assertNotIn(elsewhere["id"][:12], reply)
        for forbidden in ("private-name", "private-prompt", str(self.output), "owner"):
            self.assertNotIn(forbidden, reply)
        reply = await self.command(
            f"/生图任务 {another['id']}", master=False, sender="owner"
        )
        self.assertIn("找不到", reply)

    async def test_admin_queries_and_actions_cannot_cross_conversations(self):
        job = self.job(umo="fixture:GroupMessage:elsewhere")
        self.assertIn("没有", await self.command("/生图任务"))
        reply = await self.command(f"/生图处理 {job['id']} 补发确认")
        self.assertIn("找不到", reply)
        self.send.assert_not_awaited()

    async def test_list_is_bounded_to_ten_recent_records(self):
        jobs = [self.job() for _ in range(12)]
        reply = await self.command("/生图任务")
        self.assertEqual(sum(job["id"][:12] in reply for job in jobs), 10)
        self.assertIn(jobs[-1]["id"][:12], reply)

    async def test_queries_support_short_ids_and_hide_private_details(self):
        job = self.job()
        reply = await self.command(f"/生图任务 {job['id'][:12]}")
        self.assertIn(job["id"], reply)
        self.assertIn("发送结果待确认", reply)
        self.assertNotIn("private", reply)
        self.assertNotIn(str(self.output), reply)

    async def test_older_uncertain_job_is_not_hidden_by_recent_completed_jobs(self):
        waiting = self.job()
        for _ in range(12):
            self.job(state="delivered")
        reply = await self.command("/生图任务")
        self.assertIn(waiting["id"][:12], reply)
        self.assertEqual(reply.splitlines()[1].split()[0], waiting["id"][:12])

    async def test_invalid_or_ambiguous_ids_never_select_a_task(self):
        for identifier in ("../x", "abcd", "z" * 32):
            self.assertIn("格式", await self.command(f"/生图任务 {identifier}"))
        one, two = self.job(), self.job()
        one["id"], two["id"] = "a" * 32, "a" * 8 + "b" * 24
        with patch.object(self.store, "list_jobs", return_value=[one, two]):
            self.assertIn("多条", await self.command("/生图处理 aaaaaaaa 补发确认"))
        self.send.assert_not_awaited()

    async def test_nonadmin_actions_do_not_mutate_records(self):
        job = self.job()
        for action in ("结束", "确认已送达", "补发确认"):
            reply = await self.command(
                f"/生图处理 {job['id']} {action}", master=False, sender="owner"
            )
            self.assertIn("管理员", reply)
            self.assertEqual(self.store.get(job["id"]), job)
        self.send.assert_not_awaited()

    async def test_confirmation_is_idempotent_and_never_sends(self):
        job = self.job()
        self.assertIn(
            "记为送达", await self.command(f"/生图处理 {job['id']} 确认已送达")
        )
        self.assertIn("无需", await self.command(f"/生图处理 {job['id']} 确认已送达"))
        self.assertEqual(self.store.get(job["id"])["state"], "delivered")
        self.send.assert_not_awaited()

    async def test_submission_unknown_can_end_but_never_confirm_or_regenerate(self):
        job = self.job(state="submission_unknown")
        self.assertIn("不能补发", await self.command(f"/生图处理 {job['id']} 补发确认"))
        self.assertIn("只有", await self.command(f"/生图处理 {job['id']} 确认已送达"))
        self.assertIn("已结束", await self.command(f"/生图处理 {job['id']} 结束"))
        self.assertEqual(self.store.get(job["id"])["state"], "ended")
        self.assertTrue(self.image.exists())
        self.send.assert_not_awaited()

    async def test_active_or_locked_jobs_are_not_changed(self):
        for state in ("queued", "submitting", "sending"):
            job = self.job(state=state)
            self.assertIn("仍在运行", await self.command(f"/生图处理 {job['id']} 结束"))
            self.assertEqual(self.store.get(job["id"]), job)
        job = self.job()
        with self.store.claim(job["id"]) as claimed:
            self.assertTrue(claimed)
            self.assertIn(
                "正在处理", await self.command(f"/生图处理 {job['id']} 补发确认")
            )
        self.send.assert_not_awaited()

    async def test_resend_requires_literal_confirmation_and_stable_message_id(self):
        job = self.job()
        self.assertIn("补发确认", await self.command(f"/生图处理 {job['id']} 补发"))
        self.assertIn(
            "消息标识",
            await self.command(f"/生图处理 {job['id']} 补发确认", message=""),
        )
        self.assertEqual(self.store.get(job["id"]), job)
        self.send.assert_not_awaited()

    async def test_resend_saves_sending_before_callback_and_keeps_original_destination(
        self,
    ):
        job = self.job()

        async def send(umo, path):
            current = self.store.get(job["id"])
            self.assertEqual(current["state"], "sending")
            self.assertEqual(current["manual_send_attempts"], 1)
            self.assertEqual(len(current["admin_request_ids"]), 1)
            self.assertEqual((umo, path), (self.umo, str(self.image.resolve())))
            with self.store.claim(job["id"]) as claimed:
                self.assertFalse(claimed)
            return True

        self.send.side_effect = send
        self.assertIn(
            "已向原会话", await self.command(f"/生图处理 {job['id']} 补发确认")
        )
        self.assertEqual(self.store.get(job["id"])["state"], "delivered")
        await self.command(f"/生图处理 {job['id']} 补发确认")
        self.assertEqual(self.send.await_count, 1)

    async def test_transport_exception_is_quarantined_and_old_replays_remain_deduplicated(
        self,
    ):
        job = self.job()
        self.send.side_effect = TimeoutError("fixture transport uncertainty")
        command = f"/生图处理 {job['id']} 补发确认"
        self.assertIn("仍未确认", await self.command(command, message="first"))
        self.assertIn("仍未确认", await self.command(command, message="second"))
        self.assertIn("已经处理", await self.command(command, message="first"))
        current = self.store.get(job["id"])
        self.assertEqual(current["state"], "delivery_unknown")
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(len(current["admin_request_ids"]), 2)

    async def test_manual_resends_stop_after_three_explicit_attempts(self):
        job = self.job()
        self.send.return_value = False
        command = f"/生图处理 {job['id']} 补发确认"
        for index in range(3):
            self.assertIn("未被接收", await self.command(command, message=str(index)))
        self.assertIn("三次", await self.command(command, message="fourth"))
        self.assertEqual(self.send.await_count, 3)

    async def test_cancellation_preserves_sending_for_recovery(self):
        job = self.job()
        self.send.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.command(f"/生图处理 {job['id']} 补发确认")
        self.assertEqual(self.store.get(job["id"])["state"], "sending")

    async def test_missing_outside_and_nonimage_files_are_never_sent(self):
        outside = self.root / "outside.png"
        outside.write_bytes(self.image.read_bytes())
        invalid = self.output / "not-image.txt"
        invalid.write_text("private fixture", encoding="utf-8")
        for path in (outside, invalid, self.output / "missing.png"):
            job = self.job()
            self.store.update(job["id"], path=str(path))
            reply = await self.command(f"/生图处理 {job['id']} 补发确认")
            self.assertIn("未执行", reply)
        self.send.assert_not_awaited()

    async def test_shutdown_gate_prevents_any_mutation(self):
        job = self.job()
        self.can_send.return_value = False
        reply = await self.command(f"/生图处理 {job['id']} 补发确认")
        self.assertIn("不可用", reply)
        self.assertEqual(self.store.get(job["id"]), job)
        self.send.assert_not_awaited()

    async def test_saving_sending_failure_prevents_transport(self):
        job = self.job()
        with patch.object(self.store, "update", side_effect=OSError("fixture disk")):
            self.assertIn(
                "处理已停止", await self.command(f"/生图处理 {job['id']} 补发确认")
            )
        self.send.assert_not_awaited()
        self.assertEqual(self.store.get(job["id"]), job)

    async def test_saving_ack_failure_leaves_sending_not_ready_for_automatic_resend(
        self,
    ):
        job = self.job()
        real_update = self.store.update

        def update(job_id, **changes):
            if changes.get("state") == "delivered":
                raise OSError("fixture disk failure after send")
            return real_update(job_id, **changes)

        with patch.object(self.store, "update", side_effect=update):
            self.assertIn(
                "处理已停止", await self.command(f"/生图处理 {job['id']} 补发确认")
            )
        self.assertEqual(self.store.get(job["id"])["state"], "sending")
        self.assertEqual(self.send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
