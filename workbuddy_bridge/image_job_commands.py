"""Inspect and explicitly resolve saved image jobs without generating new images."""

import asyncio
import hashlib
import re
import stat
from datetime import datetime
from pathlib import Path

LABELS = {
    "queued": "等待处理",
    "submitting": "正在提交",
    "submitted": "正在生成",
    "ready": "等待发送",
    "sending": "正在发送",
    "delivered": "已送达",
    "failed": "未完成",
    "expired": "已超时",
    "submission_unknown": "提交结果待确认",
    "delivery_unknown": "发送结果待确认",
    "ended": "已人工结束",
}


async def handle(
    text,
    *,
    store,
    sender_id,
    umo,
    master,
    can_send,
    send_image,
    command_id="",
):
    """Process scoped status queries or administrator recovery commands.

    Args:
        text: Original explicit command text.
        store: ImageJobs store containing immutable request destinations.
        sender_id: Verified author of this command.
        umo: Complete current conversation identifier from the event.
        master: Whether the bridge recognizes the sender as its administrator.
        can_send: Callback checking current lifecycle and account readiness.
        send_image: Async callback accepting the saved destination and image path.
        command_id: Stable original message identifier, required for a resend.

    Returns:
        A plain-text reply, or None when this is not an image-job command.
    """
    parts = str(text or "").strip().split()
    if not parts or parts[0] not in {"/生图任务", "/生图处理"}:
        return None
    managing = parts[0] == "/生图处理"
    if managing and not master:
        return "处理生图任务需要管理员确认。你可以用 /生图任务 查看自己的任务。"
    if (managing and len(parts) != 3) or (not managing and len(parts) > 2):
        return (
            "用法：/生图处理 任务号 确认已送达｜补发确认｜结束"
            if managing
            else "用法：/生图任务 或 /生图任务 任务号"
        )
    if not umo or not sender_id:
        return "这条消息的来源没有确认，暂时不能查看或处理任务。"
    action = parts[2] if managing else ""
    if managing and action not in {"确认已送达", "补发确认", "结束"}:
        return "可用操作：确认已送达、补发确认、结束。补发前请先核对原会话是否已经收到图片。"
    try:
        visible = [
            job
            for job in store.list_jobs()
            if job["umo"] == umo and (master or job["sender_id"] == sender_id)
        ]
        if len(parts) == 1:
            priority = {
                "submission_unknown": 0,
                "delivery_unknown": 0,
                "queued": 1,
                "submitting": 1,
                "submitted": 1,
                "ready": 1,
                "sending": 1,
                "failed": 2,
                "expired": 2,
            }
            visible.sort(
                key=lambda job: (
                    priority.get(job["state"], 3),
                    -job["updated_at"],
                    job["id"],
                )
            )
            if not visible:
                return "当前会话没有可查看的生图任务。较早的已结束任务可能已经归档。"
            lines = ["当前会话生图任务（待确认和进行中的优先）："]
            for job in visible[:10]:
                stamp = datetime.fromtimestamp(job["created_at"]).strftime(
                    "%m-%d %H:%M"
                )
                lines.append(f"{job['id'][:12]}  {LABELS[job['state']]}  {stamp}")
            if len(visible) > 10:
                lines.append(f"共 {len(visible)} 条记录，显示前 10 条。")
            lines.append("查看详情：/生图任务 任务号")
            return "\n".join(lines)
        identifier = parts[1].lower()
        if re.fullmatch(r"[0-9a-f]{8,32}", identifier) is None:
            return "任务号格式不对，请从 /生图任务 的结果里复制。"
        matches = [job for job in visible if job["id"].startswith(identifier)]
        if not matches:
            return "当前会话找不到这个任务，或你没有查看它的权限。较早的已结束任务可能已经归档。"
        if len(matches) > 1:
            return "这个任务号对应多条记录，请使用更长的任务号。"
        job_id = matches[0]["id"]
        if not managing:
            job = matches[0]
            reply = f"任务：{job_id}\n状态：{LABELS[job['state']]}"
            reply += f"\n人工补发次数：{job.get('manual_send_attempts', 0)}/3"
            if job.get("local_deleted"):
                reply += "\n本地副本已按保留规则清理，原图请到即梦网页查看。"
            if job["state"] == "submission_unknown":
                reply += (
                    "\n提交结果尚未确认，不会自动重新生成；管理员核实后可以结束此记录。"
                )
            elif job["state"] == "delivery_unknown":
                reply += (
                    "\n请先核对本会话是否收到图片，再由管理员确认已送达或补发确认。"
                )
            if master:
                reply += "\n处理：/生图处理 任务号 确认已送达｜补发确认｜结束"
            return reply
        if not can_send():
            return "机器人暂时不可用，这次没有处理任务。"
        with store.claim(job_id) as claimed:
            if not claimed:
                return "这个任务正在处理，等当前操作结束后再试。"
            job = store.get(job_id)
            if job["umo"] != umo:
                return "任务来源与当前会话不一致，已停止处理。"
            state = job["state"]
            if state in {"queued", "submitting", "submitted", "sending"}:
                return "这个任务仍在运行或等待恢复，请等状态确认后再处理。"
            if action == "确认已送达":
                if state == "delivered":
                    return "这个任务已经记为已送达，无需重复确认。"
                if state != "delivery_unknown":
                    return "只有“发送结果待确认”的任务需要确认已送达。"
                store.update(job_id, state="delivered", notice_sent=True, error="")
                store.cleanup_artifact(job_id)
                return "已记为送达，不会再次自动补发。"
            if action == "结束":
                if state in {"delivered", "ended"}:
                    return "这个任务已经结束，无需重复处理。"
                store.update(
                    job_id,
                    state="ended",
                    notice_sent=True,
                    error="Closed by administrator after verification",
                )
                store.cleanup_artifact(job_id)
                return "已结束这条任务记录；自拍照保留，普通图片会清理本地副本，不会重新生成或补发。"
            return await _resend(
                job,
                store=store,
                sender_id=sender_id,
                command_id=command_id,
                can_send=can_send,
                send_image=send_image,
            )
    except (OSError, ValueError, TypeError, OverflowError):
        return "任务记录暂时无法读取或保存，处理已停止；没有重新生成图片。"


async def _resend(job, *, store, sender_id, command_id, can_send, send_image):
    """Send a verified saved artifact while holding the caller's durable claim.

    Args:
        job: Fresh locked job with its immutable original destination.
        store: ImageJobs store whose claim remains held throughout this call.
        sender_id: Verified administrator requesting the manual resend.
        command_id: Stable original message identifier used for replay protection.
        can_send: Callback checking current lifecycle and account readiness.
        send_image: Async callback accepting the saved destination and image path.

    Returns:
        A plain-text outcome; uncertain delivery is preserved for manual review.
    """
    if job["state"] not in {"ready", "failed", "expired", "delivery_unknown"}:
        return "这个任务不能补发。提交结果待确认的任务不会重新生成，已结束的任务也不会重发。"
    if job.get("local_deleted"):
        return "本地副本已按保留规则清理，原图请到即梦网页查看。"
    if not str(command_id or "").strip():
        return "这条指令缺少可核对的消息标识，未执行补发。"
    request_id = hashlib.sha256(
        f"{job['umo']}\0{sender_id}\0{command_id}".encode()
    ).hexdigest()
    request_ids = job.get("admin_request_ids", [])
    if request_id in request_ids:
        return "这条补发指令已经处理过，不会重复发送；请先查看任务状态。"
    attempts = job.get("manual_send_attempts", 0)
    if attempts >= 3:
        return "这个任务已尝试人工补发三次，请核对消息后确认送达或结束任务。"
    raw_path = Path(job.get("path", ""))
    raw_output = Path(job["settings"]["output_dir"])
    if not raw_path.is_absolute() or not raw_output.is_absolute():
        return "这个任务没有可核对的本地图片，不会重新生成。"
    path, output = raw_path.resolve(), raw_output.resolve()
    if not path.is_file() or not path.is_relative_to(output):
        return "原任务的图片已不存在或位置不符，未执行补发。"
    for item in (raw_path, *raw_path.parents):
        info = item.lstat()
        if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            return "图片位置存在跳转，未执行补发。"
    if not stat.S_ISREG(path.stat().st_mode):
        return "原任务的图片不是普通文件，未执行补发。"
    with path.open("rb") as stream:
        header = stream.read(12)
    if not (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith((b"GIF87a", b"GIF89a"))
        or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
    ):
        return "原任务文件无法确认为图片，未执行补发。"
    if not can_send():
        return "机器人暂时不可用，这次没有补发。"
    store.update(
        job["id"],
        state="sending",
        send_attempts=job.get("send_attempts", 0) + 1,
        manual_send_attempts=attempts + 1,
        admin_request_ids=[*request_ids, request_id],
        notice_sent=True,
        error="",
    )
    try:
        sent = await asyncio.wait_for(send_image(job["umo"], str(path)), timeout=20)
    except asyncio.CancelledError:
        # Keep "sending" durable; recovery will quarantine the uncertain result.
        raise
    except Exception as exc:  # noqa: BLE001 - transport failure has an unknown result.
        store.update(job["id"], state="delivery_unknown", error=type(exc).__name__)
        return "补发结果仍未确认，请先查看本会话是否收到图片；不会自动再发。"
    if sent:
        store.update(job["id"], state="delivered", error="")
        store.cleanup_artifact(job["id"])
        return "已向原会话补发图片，并记为已送达。"
    store.update(job["id"], state="failed", error="Manual delivery rejected")
    return "这次补发未被接收，图片和任务记录已保留，没有重新生成。"
