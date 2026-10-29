"""Coordinate image generation, recovery, and delivery."""

import asyncio
import threading
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from . import jimeng
from .bridge_policy import (
    DRAW_BLOCKED_WORDS,
    DRAW_PROMPT_MAX_LEN,
    DRAW_SELFIE_PROMPT,
    IMAGE_GEN_ADMIN_ONLY,
    IMAGE_GEN_BIN,
    IMAGE_GEN_COOLDOWN_SEC,
    IMAGE_GEN_DIR,
    IMAGE_GEN_ENABLED,
    IMAGE_GEN_GLOBAL_GAP_SEC,
    IMAGE_GEN_MAX_PER_DAY,
    IMAGE_GEN_MODEL,
    IMAGE_GEN_RATIO,
    IMAGE_GEN_RESOLUTION,
    IMAGE_GEN_TIMEOUT,
    MessageChain,
    _draw_block_reply,
    _draw_prompt_of,
    _draw_ratio_of,
    _draw_selfie_prompt_of,
    _hit_words,
    _is_master,
)


class DrawFlow:
    """Coordinate image generation, recovery, and delivery."""

    def _draw_allowed(self, sender_id: str, group_id: str) -> str:
        """能不能生图。返回空串=可以，否则返回拒绝原因（写日志用）。

        三道闸，顺序就是优先级：
        1. 总开关（面板 image_gen_enabled）
        2. 使用人（默认只管理员；花的是真额度，不是免费的）
        3. 节流（同群冷却 / 全局间隔 / 每日上限）
        """
        if not self._cfg_bool("image_gen_enabled", IMAGE_GEN_ENABLED):
            return "生图已关闭"
        if self._cfg_bool("image_gen_admin_only", IMAGE_GEN_ADMIN_ONLY):
            if not _is_master(sender_id):
                return "只有管理员能用生图"
        now = time.time()
        # 同群冷却：一次生成几十秒，冷却期内再来一条只会排更长的队
        cool = self._cfg_int("image_gen_cooldown_sec", IMAGE_GEN_COOLDOWN_SEC)
        if group_id and now - float(self._draw_last.get(group_id) or 0) < cool:
            return f"本群冷却中（<{cool}s）"
        gap = self._cfg_int("image_gen_global_gap_sec", IMAGE_GEN_GLOBAL_GAP_SEC)
        if now - float(self._draw_last_global or 0) < gap:
            return f"全局间隔未到（<{gap}s）"
        # 每日上限：CLI 额度是真金白银，必须有个天花板
        max_day = self._cfg_int("image_gen_max_per_day", IMAGE_GEN_MAX_PER_DAY)
        if max_day > 0 and group_id:
            today = time.strftime("%Y-%m-%d")
            day, n = self._draw_daily.get(group_id, (today, 0))
            if day != today:
                day, n = today, 0
            if n >= max_day:
                return f"今日已出图 {n} 张，达上限 {max_day}"
        return ""

    def _draw_bump(self, group_id: str) -> None:
        """占用一次生图配额。**必须在起任务之前占**（同 _kw_bump 的道理）：
        生成要跑几十秒，这期间进来的第二条如果还没记账，冷却检查会放行，
        结果是两条一起排队、白烧两次额度。"""
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if group_id:
            day, n = self._draw_daily.get(group_id, (today, 0))
            if day != today:
                day, n = today, 0
            self._draw_daily[group_id] = (day, n + 1)
            self._draw_last[group_id] = now
        self._draw_last_global = now

    async def _run_draw(
        self,
        group_id: str,
        umo: str,
        prompt: str,
        nickname: str,
        ratio: str = "",
        sender_id: str = "",
        *,
        retain_local: bool = False,
    ) -> None:
        """Persist an accepted image request before starting external work.

        Args:
            group_id: Original group identifier.
            umo: Complete destination from the original event.
            prompt: Requested image description.
            nickname: Requester's display name.
            ratio: Explicit aspect ratio, if supplied.
            sender_id: Owner of the reusable image session.
            retain_local: Preserve bot selfies locally after successful delivery.
        """
        if not self._can_send_active():
            return
        key = str(sender_id or "").strip()
        active = {"queued", "submitting", "submitted", "ready", "sending"}
        if key in self._draw_busy or any(
            job["sender_id"] == key and job["state"] in active
            for job in self._draw_jobs.list_jobs()
        ):
            await self._send_to_session(umo, "上一张还在处理，等它完成再发")
            return
        try:
            timeout = max(30, self._cfg_int("image_gen_timeout", IMAGE_GEN_TIMEOUT))
            # Each request downloads into its own directory, including after recovery.
            output = Path(str(self._cfg("image_gen_dir", "") or IMAGE_GEN_DIR))
            job = self._draw_jobs.create(
                {
                    "group_id": group_id,
                    "umo": umo,
                    "sender_id": key,
                    "nickname": nickname,
                    "prompt": prompt,
                    "retain_local": retain_local,
                    "ratio": ratio,
                    "deadline_at": time.time() + timeout,
                    "expires_at": time.time() + 86400,
                    "settings": {
                        "bin_path": str(
                            self._cfg("image_gen_bin", "") or IMAGE_GEN_BIN
                        ),
                        "output_dir": str((output / uuid.uuid4().hex).resolve()),
                        "model": str(
                            self._cfg("image_gen_model", "") or IMAGE_GEN_MODEL
                        ),
                        "ratio": str(
                            self._cfg("image_gen_ratio", "") or IMAGE_GEN_RATIO
                        ),
                        "resolution": str(
                            self._cfg("image_gen_resolution", "")
                            or IMAGE_GEN_RESOLUTION
                        ),
                        "timeout": timeout,
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("workbuddy_bridge: image request could not be saved: %s", exc)
            await self._send_to_session(umo, "生图任务没能保存，这次还没开始画")
            return
        await self._process_draw_job(job["id"])

    async def _draw_recovery_loop(self) -> None:
        """Resume recorded jobs independently of optional idle conversation."""
        while not self._stopped:
            try:
                await self._resume_draw_jobs()
            except Exception as exc:  # noqa: BLE001
                logger.error("workbuddy_bridge: image recovery scan failed: %s", exc)
            await asyncio.sleep(10)

    async def _resume_draw_jobs(self) -> None:
        """Schedule due jobs only after the original bot account is verified."""
        if not self._can_send_active() or not self._cfg_bool(
            "image_gen_enabled", IMAGE_GEN_ENABLED
        ):
            return
        for job in self._draw_jobs.list_jobs():
            if (
                (
                    job["state"]
                    in {"queued", "submitting", "submitted", "ready", "sending"}
                    or job["state"]
                    in {"failed", "expired", "submission_unknown", "delivery_unknown"}
                    and not job.get("notice_sent")
                )
                and job["id"] not in self._draw_running
                and job.get("next_retry_at", 0) <= time.time()
            ):
                self._spawn_background(self._process_draw_job(job["id"]))

    def _generate_draw_job(self, job: dict, stop: threading.Event) -> str:
        """Advance one saved job without ever resubmitting a known task.

        Args:
            job: Locked job snapshot with immutable origin and settings.
            stop: Cooperative shutdown signal shared with the async owner.

        Returns:
            Path downloaded for this exact submission.
        """
        settings = jimeng.JimengSettings(
            **{**job["settings"], "output_dir": Path(job["settings"]["output_dir"])}
        )
        deadline = time.monotonic() + max(0, job["deadline_at"] - time.time())
        session = job.get("session", "")
        if job["state"] == "submitted" and not job.get("submit_id"):
            raise RuntimeError("Submitted image task has no saved remote identifier")
        if not job.get("submit_id"):
            session = self._jimeng.ensure_session(
                job["nickname"],
                job["sender_id"],
                settings,
                deadline=deadline,
                cancel_event=stop,
            )
            if not session or str(session) == "0":
                raise RuntimeError("No independent image session")
            if stop.is_set():
                raise InterruptedError("Image worker stopped before submission")
            # A crash from this point until saving submit_id has an unknown outcome.
            self._draw_jobs.update(job["id"], state="submitting", session=session)

        def submitted(submit_id: str) -> None:
            """Save the remote identifier before attempting the first query.

            Args:
                submit_id: Identifier acknowledged by the generation service.
            """
            self._draw_jobs.update(job["id"], state="submitted", submit_id=submit_id)

        path = self._jimeng.generate(
            job["prompt"],
            session=session,
            settings=settings,
            ratio=job["ratio"],
            submit_id=job.get("submit_id", ""),
            on_submitted=submitted,
            deadline=deadline,
            cancel_event=stop,
        )
        result = Path(path).resolve()
        if not result.is_file() or not result.is_relative_to(
            settings.output_dir.resolve()
        ):
            raise RuntimeError("Image result is outside this task's download directory")
        self._draw_jobs.update(job["id"], state="ready", path=str(result), error="")
        return str(result)

    async def _process_draw_job(self, job_id: str) -> None:
        """Generate or deliver one claimed job and preserve uncertain outcomes.

        Args:
            job_id: Local identifier of an accepted request.
        """
        if job_id in self._draw_running:
            return
        self._draw_running.add(job_id)
        key = ""
        owns_owner = False
        try:
            async with self._draw_slots:
                with self._draw_jobs.claim(job_id) as claimed:
                    if (
                        not claimed
                        or not self._can_send_active()
                        or not self._cfg_bool("image_gen_enabled", IMAGE_GEN_ENABLED)
                    ):
                        return
                    job = self._draw_jobs.get(job_id)
                    if (
                        job["state"] == "delivered"
                        or job["state"]
                        in {
                            "failed",
                            "expired",
                            "submission_unknown",
                            "delivery_unknown",
                        }
                        and job.get("notice_sent")
                    ):
                        return
                    if job["state"] not in {
                        "queued",
                        "submitting",
                        "submitted",
                        "ready",
                        "sending",
                        "failed",
                        "expired",
                        "submission_unknown",
                        "delivery_unknown",
                    }:
                        return
                    if job.get("next_retry_at", 0) > time.time():
                        return
                    key = job["sender_id"]
                    if key in self._draw_busy:
                        return
                    self._draw_busy.add(key)
                    owns_owner = True
                    try:
                        if job["state"] == "sending":
                            job = self._draw_jobs.update(
                                job_id,
                                state="delivery_unknown",
                                error="Interrupted while sending",
                            )
                        elif job["state"] == "submitting":
                            job = self._draw_jobs.update(
                                job_id,
                                state="submission_unknown",
                                error="Interrupted before saving submission ID",
                            )
                        elif (
                            job["state"] in {"queued", "submitted", "ready"}
                            and time.time() >= job["expires_at"]
                        ):
                            job = self._draw_jobs.update(
                                job_id,
                                state="expired",
                                error="Delivery recovery window expired",
                            )
                        elif job["state"] in {"queued", "submitted"}:
                            if time.time() >= job["deadline_at"]:
                                raise TimeoutError("Image generation deadline expired")
                            stop = threading.Event()
                            worker = asyncio.create_task(
                                asyncio.to_thread(self._generate_draw_job, job, stop)
                            )
                            try:
                                await asyncio.shield(worker)
                            except asyncio.CancelledError:
                                stop.set()
                                # Keep the job lock until the CLI worker has really stopped.
                                try:
                                    await worker
                                except Exception as exc:  # noqa: BLE001
                                    logger.info(
                                        "workbuddy_bridge: image worker stopped during shutdown: %s",
                                        type(exc).__name__,
                                    )
                                raise
                            job = self._draw_jobs.get(job_id)
                        if job["state"] == "ready" and self._can_send_active():
                            path = Path(job["path"]).resolve()
                            output = Path(job["settings"]["output_dir"]).resolve()
                            if not path.is_file() or not path.is_relative_to(output):
                                raise RuntimeError(
                                    "Saved image is missing or outside its task directory"
                                )
                            text = (
                                "画好了，新照片出炉"
                                if job["prompt"].startswith(DRAW_SELFIE_PROMPT)
                                else f"画好了，{job['nickname']}说的「{job['prompt'][:30]}」"
                            )
                            chain = MessageChain(
                                chain=[Plain(text), Image(file=str(path))]
                            )
                            attempts = job.get("send_attempts", 0) + 1
                            self._draw_jobs.update(
                                job_id, state="sending", send_attempts=attempts
                            )
                            try:
                                sent = await asyncio.wait_for(
                                    self._send_chain_to_session(
                                        job["umo"], chain, raise_on_error=True
                                    ),
                                    20,
                                )
                            except asyncio.CancelledError:
                                # Recovery will quarantine this uncertain delivery, never resend blindly.
                                raise
                            except Exception as exc:  # noqa: BLE001
                                job = self._draw_jobs.update(
                                    job_id,
                                    state="delivery_unknown",
                                    error=type(exc).__name__,
                                )
                            else:
                                if sent:
                                    job = self._draw_jobs.update(
                                        job_id, state="delivered", error=""
                                    )
                                    self._draw_jobs.cleanup_artifact(job_id)
                                    logger.info(
                                        "workbuddy_bridge: image job %s delivered",
                                        job_id,
                                    )
                                    # Session cosmetics must not delay or invalidate delivery.
                                    settings = jimeng.JimengSettings(
                                        **{**job["settings"], "output_dir": output}
                                    )
                                    try:
                                        await asyncio.to_thread(
                                            self._jimeng.rename_session,
                                            job.get("session", ""),
                                            job["nickname"],
                                            job["sender_id"],
                                            settings,
                                            deadline=time.monotonic() + 5,
                                        )
                                    except Exception as exc:  # noqa: BLE001
                                        logger.warning(
                                            "workbuddy_bridge: post-delivery rename failed: %s",
                                            exc,
                                        )
                                elif not self._can_send_active():
                                    job = self._draw_jobs.update(
                                        job_id,
                                        state="ready",
                                        send_attempts=attempts - 1,
                                    )
                                elif attempts < 3:
                                    job = self._draw_jobs.update(
                                        job_id,
                                        state="ready",
                                        next_retry_at=time.time() + 10 * attempts,
                                        error="Delivery rejected; retry pending",
                                    )
                                else:
                                    job = self._draw_jobs.update(
                                        job_id,
                                        state="failed",
                                        error="Delivery rejected three times",
                                    )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        job = self._draw_jobs.get(job_id)
                        if isinstance(exc, jimeng.GenerationFailed):
                            state = "failed"
                        elif job["state"] == "submitting":
                            state = "submission_unknown"
                        elif job["state"] == "sending":
                            state = "delivery_unknown"
                        elif isinstance(exc, TimeoutError):
                            state = (
                                job["state"]
                                if job["state"] in {"queued", "submitted"}
                                and time.time() < job["deadline_at"]
                                else "expired"
                            )
                        elif (
                            job["state"] == "submitted"
                            and time.time() < job["deadline_at"]
                        ):
                            state = "submitted"
                        else:
                            state = "failed"
                        job = self._draw_jobs.update(
                            job_id,
                            state=state,
                            error=str(exc)[:300],
                            next_retry_at=time.time() + 10,
                        )
                        logger.warning(
                            "workbuddy_bridge: image job %s is %s: %s",
                            job_id,
                            state,
                            exc,
                        )
                    if (
                        self._can_send_active()
                        and job["state"]
                        in {
                            "failed",
                            "expired",
                            "submission_unknown",
                            "delivery_unknown",
                        }
                        and not job.get("notice_sent")
                    ):
                        notices = {
                            "failed": "这次生图或回图没完成，任务记录已经保留",
                            "expired": "这张图处理超时了，任务记录已经保留，没有重新提交",
                            "submission_unknown": "生图请求的结果暂时没确认，为免重复生成，这次不会自动重画",
                            "delivery_unknown": "图片已生成，但发送结果没确认，为免重复刷图，已暂停自动补发",
                        }
                        notified = await self._send_to_session(
                            job["umo"], notices[job["state"]]
                        )
                        self._draw_jobs.update(
                            job_id,
                            notice_sent=bool(notified)
                            or time.time() >= job["expires_at"],
                            next_retry_at=time.time() + 60,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "workbuddy_bridge: image job %s persistence or recovery failed: %s",
                job_id,
                exc,
            )
        finally:
            if owns_owner:
                self._draw_busy.discard(key)
            self._draw_running.discard(job_id)

    def _maybe_draw(self, event, text: str, sender_id: str) -> str:
        """生图入口。

        返回值三种：
        ""          —— 不是生图请求，继续走后面的流程
        "draw"      —— 已安排后台任务，调用方回一句"画着呢"
        其他字符串  —— 被闸拦下，调用方把这句话直接回给群友
                       （2026-10-19：cd 中不再沉默落回问答，先回"生图cd中"）
        """
        # 自画像优先（2026-10-20）：「生成一张你的照片」若先走普通解析，
        # prompt 会变成字面的"你的照片"——必须先判自画像类请求。
        selfie_prompt = _draw_selfie_prompt_of(text)
        prompt = selfie_prompt or _draw_prompt_of(text)
        if not prompt:
            return ""
        group_id = str(event.get_group_id() or "").strip()
        umo = str(event.unified_msg_origin or "")
        if not umo:
            return ""
        reason = self._draw_allowed(sender_id, group_id)
        if not reason:
            # 内容红线：涉及未成年人的性化请求直接拒，连模型都不送
            hit = _hit_words(prompt, DRAW_BLOCKED_WORDS)
            if hit:
                logger.warning(
                    f"workbuddy_bridge: [生图] {group_id} 命中红线词：{hit}，拒绝生成"
                )
                return "这个我不画"
        # 提示词长度检查（即梦 API 上限 1600 字符，实测 2673 被拒）
        if len(prompt) > DRAW_PROMPT_MAX_LEN:
            logger.info(
                f"workbuddy_bridge: [生图] {group_id} prompt 超长："
                f"{len(prompt)} > {DRAW_PROMPT_MAX_LEN}，拒绝生成"
            )
            return (
                f"提示词太长了，发了 {len(prompt)} 字，上限 {DRAW_PROMPT_MAX_LEN}，"
                f"精简一下再试"
            )
        if reason:
            logger.info(f"workbuddy_bridge: [生图] {group_id} 命中但不画：{reason}")
            return _draw_block_reply(reason)
        self._draw_bump(group_id)
        nickname = str(event.get_sender_name() or "").strip() or "群友"
        ratio = _draw_ratio_of(text)
        self._spawn_background(
            self._run_draw(
                group_id,
                umo,
                prompt,
                nickname,
                ratio,
                sender_id,
                retain_local=bool(selfie_prompt),
            )
        )
        return "draw"
