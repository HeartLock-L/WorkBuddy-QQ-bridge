"""Check generated-image retention at real delivery and command boundaries."""

import asyncio
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from artifact_test_support import ArtifactFixture, Event
from bridge_test_support import PLUGIN, load_definitions, load_module

COMMANDS = load_module(
    PLUGIN.with_name("image_job_commands.py"), "retention_command_routes"
)
PNG = b"\x89PNG\r\n\x1a\nfixture"


class ImageRetentionRoutingTests(ArtifactFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.paths = []

        def generate(prompt, *, settings, on_submitted, **kwargs):
            on_submitted("remote-fixture")
            settings.output_dir.mkdir(parents=True, exist_ok=True)
            path = settings.output_dir / "result.png"
            path.write_bytes(PNG)
            self.paths.append(path)
            return str(path)

        self.bridge._jimeng.generate.side_effect = generate

    async def asyncTearDown(self):
        for task in list(self.bridge._background_tasks):
            task.cancel()
        await asyncio.gather(*self.bridge._background_tasks, return_exceptions=True)

    def job(self, *, retain=False, state="delivery_unknown"):
        output = self.root / "images" / uuid.uuid4().hex
        output.mkdir(parents=True)
        image = output / "saved.png"
        image.write_bytes(PNG)
        job = self.bridge._draw_jobs.create(
            {
                "group_id": "original-group",
                "umo": "fake:GroupMessage:original-group",
                "sender_id": "original-owner",
                "nickname": "requester",
                "prompt": "selfie:fixture" if retain else "landscape",
                "retain_local": retain,
                "ratio": "1:1",
                "deadline_at": time.time() + 30,
                "expires_at": time.time() + 86400,
                "settings": {
                    "bin_path": "unused-cli.exe",
                    "output_dir": str(output),
                    "model": "fixture",
                    "ratio": "1:1",
                    "resolution": "fixture",
                    "timeout": 30,
                },
            }
        )
        return self.bridge._draw_jobs.update(job["id"], state=state, path=str(image))

    async def command(self, job, action, send):
        return await COMMANDS.handle(
            f"/生图处理 {job['id']} {action}",
            store=self.bridge._draw_jobs,
            sender_id="admin",
            umo=job["umo"],
            master=True,
            can_send=lambda: True,
            send_image=send,
            command_id=uuid.uuid4().hex,
        )

    async def test_normal_image_is_readable_during_send_then_removed_after_delivery(
        self,
    ):
        async def send(umo, chain, **kwargs):
            path = Path(chain.chain[-1].file)
            self.assertEqual(path.read_bytes(), PNG)
            self.assertEqual(umo, "fake:GroupMessage:original-group")
            self.assertEqual(self.bridge._draw_jobs.list_jobs()[0]["state"], "sending")
            return True

        self.bridge._send_chain_to_session.side_effect = send
        await self.bridge._run_draw(
            "original-group",
            "fake:GroupMessage:original-group",
            "landscape",
            "name",
            sender_id="owner",
        )
        job = self.bridge._draw_jobs.list_jobs()[0]
        self.assertEqual(job["state"], "delivered")
        self.assertIs(job["retain_local"], False)
        self.assertIs(job["local_deleted"], True)
        self.assertFalse(self.paths[0].exists())
        self.assertEqual(job["submit_id"], "remote-fixture")
        self.bridge._jimeng.generate.assert_called_once()

    async def test_only_the_dedicated_selfie_branch_sets_long_term_retention(self):
        namespace = {
            "_draw_selfie_prompt_of": lambda text: (
                "selfie:portrait" if text == "selfie request" else ""
            ),
            "_draw_prompt_of": lambda text: "selfie:ordinary user description",
            "_draw_ratio_of": lambda text: "",
            "_hit_words": lambda *args: None,
            "DRAW_BLOCKED_WORDS": (),
            "DRAW_PROMPT_MAX_LEN": 1600,
            "logger": Mock(),
        }
        handler = load_definitions(namespace, methods={"_maybe_draw"})[
            "Bridge"
        ]._maybe_draw
        self.bridge._draw_allowed = Mock(return_value="")
        self.bridge._draw_bump = Mock()
        for text, retained in (("selfie request", True), ("ordinary request", False)):
            with self.subTest(text=text):
                result = handler(self.bridge, Event("original-group"), text, text)
                self.assertEqual(result, "draw")
                await asyncio.gather(*self.bridge._background_tasks)
                job = next(
                    j
                    for j in self.bridge._draw_jobs.list_jobs()
                    if j["sender_id"] == text
                )
                self.assertIs(job["retain_local"], retained)
                self.assertEqual(Path(job["path"]).exists(), retained)

    async def test_failed_or_uncertain_delivery_keeps_local_image_for_recovery(self):
        for result in (False, TimeoutError("unconfirmed transport")):
            with self.subTest(result=type(result).__name__):
                self.bridge._send_chain_to_session.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.bridge._send_chain_to_session.return_value = result
                await self.bridge._run_draw(
                    "original-group",
                    "fake:GroupMessage:original-group",
                    "landscape",
                    "name",
                    sender_id=uuid.uuid4().hex,
                )
                self.assertTrue(self.paths[-1].exists())
        states = {j["state"] for j in self.bridge._draw_jobs.list_jobs()}
        self.assertEqual(states, {"ready", "delivery_unknown"})

    async def test_admin_confirmation_and_end_remove_only_non_selfie_copies(self):
        send = AsyncMock(side_effect=AssertionError("No image should be sent"))
        for action in ("确认已送达", "结束"):
            for retained in (False, True):
                with self.subTest(action=action, retained=retained):
                    job = self.job(retain=retained)
                    await self.command(job, action, send)
                    saved = self.bridge._draw_jobs.get(job["id"])
                    self.assertEqual(
                        saved["state"],
                        "delivered" if action == "确认已送达" else "ended",
                    )
                    self.assertEqual(Path(job["path"]).exists(), retained)
        send.assert_not_awaited()
        self.bridge._jimeng.generate.assert_not_called()

    async def test_manual_resend_cleans_after_original_destination_receives_image(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                job = self.job(retain=retained)

                async def send(umo, path):
                    self.assertEqual(umo, job["umo"])
                    self.assertEqual(Path(path).read_bytes(), PNG)
                    return True

                await self.command(job, "补发确认", send)
                self.assertEqual(
                    self.bridge._draw_jobs.get(job["id"])["state"], "delivered"
                )
                self.assertEqual(Path(job["path"]).exists(), retained)
        self.bridge._jimeng.generate.assert_not_called()

    async def test_manual_resend_failure_preserves_recoverable_copy(self):
        for result in (False, TimeoutError("unknown")):
            with self.subTest(result=type(result).__name__):
                job = self.job()
                send = AsyncMock()
                if isinstance(result, Exception):
                    send.side_effect = result
                else:
                    send.return_value = result
                await self.command(job, "补发确认", send)
                self.assertTrue(Path(job["path"]).exists())
                self.assertEqual(
                    self.bridge._draw_jobs.get(job["id"])["state"],
                    "delivery_unknown" if isinstance(result, Exception) else "failed",
                )
        self.bridge._jimeng.generate.assert_not_called()

    async def test_expired_local_copy_does_not_trigger_regeneration_on_manual_resend(
        self,
    ):
        job = self.job()
        with self.bridge._draw_jobs.claim(job["id"]) as claimed:
            self.assertTrue(claimed)
            self.bridge._draw_jobs.cleanup_artifact(
                job["id"], now=job["expires_at"] + 1
            )
        send = AsyncMock(
            side_effect=AssertionError("Expired local copy must not be sent")
        )
        await self.command(job, "补发确认", send)
        self.assertFalse(Path(job["path"]).exists())
        self.assertEqual(
            self.bridge._draw_jobs.get(job["id"])["state"], "delivery_unknown"
        )
        send.assert_not_awaited()
        self.bridge._jimeng.generate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
