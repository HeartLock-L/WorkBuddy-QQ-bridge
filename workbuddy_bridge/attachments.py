"""Manage attachment files and scoped image context without the QQ framework."""

import asyncio
import io
import os
import threading
import time
import uuid
from pathlib import Path

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MATERIALIZE_TIMEOUT = 15.0
MESSAGE_TIMEOUT = 30.0
GIF_MAX_FRAMES = 200
GIF_FRAME_PIXELS = 4_000_000
GIF_TOTAL_PIXELS = 40_000_000
GIF_PREVIEW_BYTES = 1024 * 1024


class AttachmentBatch(list):
    """Keep saved paths and their metadata together for one request."""

    def __init__(self):
        super().__init__()
        self.notes: dict[str, str] = {}
        self.descriptions: dict[str, str] = {}
        self.notices: list[str] = []


class AttachmentRejected(ValueError):
    """Carry a safe user-facing reason without exposing filesystem details."""


def describe(batch):
    """Format only the paths and metadata owned by this attachment batch.

    Args:
        batch: AttachmentBatch, or legacy plain paths without metadata.

    Returns:
        One prompt line per resource, including available authors and GIF scope.
    """
    notes = getattr(batch, "notes", {})
    descriptions = getattr(batch, "descriptions", {})
    return "\n".join(
        f"- {path}"
        + (f"（发送者：{notes[path]}）" if notes.get(path) else "")
        + (f"；{descriptions[path]}" if descriptions.get(path) else "")
        for path in batch
    )


def _save_one(local, dest, remaining, deadline, stop):
    """Copy a bounded input and prepare a bounded temporal GIF preview.

    Args:
        local: Framework-materialized local source.
        dest: Unique destination selected for this request.
        remaining: Remaining incoming byte budget for the message.
        deadline: Monotonic deadline shared with attachment materialization.
        stop: Cooperative cancellation flag owned by this request.

    Returns:
        Original path, optional GIF preview, description, and incoming bytes.

    Raises:
        AttachmentRejected: The source or preview exceeds a supported limit.
        TimeoutError: The message budget expired or the request was cancelled.
        OSError: A local file could not be read or saved.
    """
    created = []
    complete = False
    try:
        local = Path(local)
        source_stat = local.stat()
        if not local.is_file():
            raise AttachmentRejected("附件暂时读不到，请重新发送。")
        if source_stat.st_size > MAX_ATTACHMENT_BYTES:
            raise AttachmentRejected("单个附件不能超过 10 MB，请压缩后重发。")
        if source_stat.st_size > remaining:
            raise AttachmentRejected("这条消息的附件合计不能超过 10 MB，请分开发送。")
        if stop.is_set() or time.monotonic() >= deadline:
            raise TimeoutError
        with local.open("rb") as source:
            signature = source.read(6)
            source.seek(0)
            is_gif = signature in (b"GIF87a", b"GIF89a")
            if local.suffix.lower() == ".gif" and not is_gif:
                raise AttachmentRejected("这张动图格式不完整，请换一份文件重发。")
            if is_gif:
                dest = dest.with_suffix(".gif")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as target:
                created.append(dest)
                copied = 0
                while True:
                    if stop.is_set() or time.monotonic() >= deadline:
                        raise TimeoutError
                    chunk = source.read(min(64 * 1024, remaining - copied + 1))
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_ATTACHMENT_BYTES or copied > remaining:
                        raise AttachmentRejected(
                            "附件大小超过 10 MB 限制，请压缩或分开发送。"
                        )
                    target.write(chunk)
            # Cache retention follows the incoming file's original age.
            os.utime(dest, (source_stat.st_atime, source_stat.st_mtime))
        preview = None
        description = ""
        if is_gif:
            try:
                from PIL import Image, ImageDraw, UnidentifiedImageError
            except ImportError as exc:
                raise AttachmentRejected(
                    "当前环境缺少动图读取组件，请管理员检查后再试。"
                ) from exc
            samples = []
            stride = 1
            decoded = 0
            truncated = False
            last = None
            try:
                with Image.open(dest) as animation:
                    width, height = animation.size
                    pixels = width * height
                    if pixels > GIF_FRAME_PIXELS:
                        raise AttachmentRejected("动图画面太大，请缩小分辨率后重发。")
                    frame_budget = min(
                        GIF_MAX_FRAMES, GIF_TOTAL_PIXELS // max(pixels, 1)
                    )
                    decoded_pixels = 0
                    for index in range(frame_budget + 1):
                        if stop.is_set() or time.monotonic() >= deadline:
                            raise TimeoutError
                        try:
                            animation.seek(index)
                        except EOFError:
                            break
                        if index == frame_budget:
                            truncated = True
                            break
                        current_pixels = animation.width * animation.height
                        if current_pixels > GIF_FRAME_PIXELS:
                            raise AttachmentRejected(
                                "动图画面太大，请缩小分辨率后重发。"
                            )
                        if decoded_pixels + current_pixels > GIF_TOTAL_PIXELS:
                            truncated = True
                            break
                        decoded_pixels += current_pixels
                        frame = animation.convert("RGB")
                        frame.thumbnail((320, 320))
                        last = (index, frame)
                        decoded += 1
                        if index % stride == 0:
                            samples.append(last)
                            if len(samples) > 8:
                                samples = samples[::2]
                                stride *= 2
                    if last is not None and samples[-1][0] != last[0]:
                        if len(samples) >= 8:
                            samples.pop()
                        samples.append(last)
            except (UnidentifiedImageError, EOFError, OSError, ValueError) as exc:
                if isinstance(exc, AttachmentRejected):
                    raise
                raise AttachmentRejected(
                    "这张动图没能完整解析，请换一份文件重发。"
                ) from exc
            if not samples:
                raise AttachmentRejected("这张动图没有可读取的画面。")
            columns = min(4, len(samples))
            rows = (len(samples) + columns - 1) // columns
            sheet = Image.new("RGB", (columns * 320, rows * 344), "white")
            canvas = ImageDraw.Draw(sheet)
            for position, (index, frame) in enumerate(samples):
                x, y = (position % columns) * 320, (position // columns) * 344
                sheet.paste(frame, (x + (320 - frame.width) // 2, y + 24))
                canvas.text((x + 6, y + 5), f"Frame {index + 1}", fill="black")
            buffer = io.BytesIO()
            sheet.save(buffer, format="JPEG", quality=75)
            encoded = buffer.getvalue()
            if len(encoded) > GIF_PREVIEW_BYTES:
                raise AttachmentRejected("动图预览超过处理限制，请缩小画面后重发。")
            if stop.is_set() or time.monotonic() >= deadline:
                raise TimeoutError
            preview = dest.with_name(dest.stem + "_frames.jpg")
            with preview.open("xb") as stream:
                created.append(preview)
                stream.write(encoded)
            os.utime(preview, (source_stat.st_atime, source_stat.st_mtime))
            scope = f"仅解析前 {decoded} 帧" if truncated else f"共解析 {decoded} 帧"
            description = (
                f"GIF 动图的抽帧预览：{scope}，展示 {len(samples)} 帧，"
                "从左到右、从上到下按时间顺序排列；不是完整播放，不能据此判断精确速度、时长或未展示的动作。"
            )
        if stop.is_set() or time.monotonic() >= deadline:
            raise TimeoutError
        complete = True
        return str(dest), str(preview) if preview else None, description, copied
    finally:
        if not complete or stop.is_set():
            for path in created:
                path.unlink(missing_ok=True)


def remember_images(history, group_id, sender_id, sender_name, urls, *, limit):
    """Retain image addresses without fetching their contents.

    Args:
        history: Per-group image records owned by the current bridge instance.
        group_id: Normalized group identifier.
        sender_id: Normalized author identifier.
        sender_name: Display name recorded with the image.
        urls: Addresses extracted from this message's image components.
        limit: Maximum records retained for the group.
    """
    bucket = history.setdefault(group_id, [])
    for url in urls:
        if not url:
            continue
        bucket.append(
            {
                "url": url,
                "sender": sender_id,
                "name": sender_name,
                "ts": time.time(),
            }
        )
    if len(bucket) > limit:
        del bucket[:-limit]


def recent_image(history, group_id, sender_id, *, ttl, now):
    """Find the latest unexpired image for the requested group and author.

    Args:
        history: Per-group image records.
        group_id: Normalized group identifier.
        sender_id: Normalized author identifier, or empty for the legacy lookup.
        ttl: Maximum image age in seconds.
        now: Current wall-clock timestamp.

    Returns:
        The original record, or None when no eligible address is available.
    """
    bucket = history.get(group_id) or []
    mine = [
        item
        for item in bucket
        if now - float(item.get("ts") or 0) <= ttl
        and (not sender_id or str(item.get("sender") or "") == sender_id)
    ]
    if not mine:
        return None
    newest = mine[-1]
    return newest if str(newest.get("url") or "").strip() else None


def remember_image_wait(waiting, group_id, sender_id, text, *, ttl, now):
    """Record one follow-up request and expire old requests.

    Args:
        waiting: Per-group and per-author waiting records owned by the bridge.
        group_id: Normalized group identifier.
        sender_id: Normalized author identifier.
        text: Original request, capped at the existing 120-character limit.
        ttl: Waiting-record lifetime in seconds.
        now: Current wall-clock timestamp.
    """
    for key in [
        key for key, item in waiting.items() if now - float(item.get("ts") or 0) > ttl
    ]:
        waiting.pop(key, None)
    waiting[f"{group_id}:{sender_id}"] = {"text": (text or "")[:120], "ts": now}


def take_image_wait(waiting, group_id, sender_id, *, ttl, now):
    """Consume only the matching author's unexpired follow-up request.

    Args:
        waiting: Per-group and per-author waiting records owned by the bridge.
        group_id: Normalized group identifier.
        sender_id: Normalized author identifier.
        ttl: Waiting-record lifetime in seconds.
        now: Current wall-clock timestamp.

    Returns:
        The matching record, or None for missing or expired records.
    """
    item = waiting.pop(f"{group_id}:{sender_id}", None)
    if not item or now - float(item.get("ts") or 0) > ttl:
        return None
    return item


async def save_attachments(
    targets,
    *,
    out_dir: Path,
    sender_name,
    sender_id,
    context_component,
    context_meta,
    max_files,
    materialize,
    logger,
    materialize_timeout=MATERIALIZE_TIMEOUT,
    message_timeout=MESSAGE_TIMEOUT,
):
    """Save bounded attachments with metadata isolated to the current request.

    Args:
        targets: Components already selected by the QQ entry point.
        out_dir: Per-sender destination chosen by the entry point.
        sender_name: Requester's display name.
        sender_id: Requester's identifier.
        context_component: Selected previous image component, if any.
        context_meta: Author metadata for that previous image.
        max_files: Existing per-message attachment cap.
        materialize: Async framework component-to-local-path adapter. Its own
            temporary download cache is not controlled by this copy boundary.
        logger: Existing bridge logger.
        materialize_timeout: Maximum seconds for each framework materialization.
        message_timeout: Overall time budget for one attachment batch.

    Returns:
        Saved paths and request-local authors, descriptions, and safe notices.
        GIF preview bytes have a separate one-MiB cap per source attachment.
        Cancellation propagates and asks any copy worker to remove its output.
    """
    batch = AttachmentBatch()

    def discard_worker(task):
        # Cancellation may race with the worker's successful final return.
        if task.cancelled():
            return
        try:
            original, preview, _, _ = task.result()
        except Exception:  # noqa: BLE001
            return
        for filename in (original, preview):
            if filename:
                try:
                    Path(filename).unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "workbuddy_bridge: cancelled attachment cleanup failed"
                    )

    deadline = time.monotonic() + message_timeout
    remaining = MAX_ATTACHMENT_BYTES
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    for idx, comp in enumerate(targets[:max_files], 1):
        worker = None
        stop = threading.Event()
        try:
            available = deadline - time.monotonic()
            if available <= 0:
                raise TimeoutError
            # Trust metadata only to reject oversized content before download.
            declared = getattr(comp, "size", None)
            if declared is None:
                declared = getattr(comp, "file_size", None)
            if isinstance(declared, (str, int)) and str(declared).isdigit():
                declared = int(declared)
                if declared > MAX_ATTACHMENT_BYTES:
                    raise AttachmentRejected("单个附件不能超过 10 MB，请压缩后重发。")
                if declared > remaining:
                    raise AttachmentRejected(
                        "这条消息的附件合计不能超过 10 MB，请分开发送。"
                    )
            local = await asyncio.wait_for(
                materialize(comp), min(materialize_timeout, available)
            )
            if not local:
                raise AttachmentRejected("附件暂时读不到，请重新发送。")
            dest = out_dir / f"{stamp}_{idx}{Path(local).suffix}"
            worker = asyncio.create_task(
                asyncio.to_thread(_save_one, local, dest, remaining, deadline, stop)
            )
            original, preview, description, size = await asyncio.wait_for(
                asyncio.shield(worker), max(0, deadline - time.monotonic())
            )
            remaining -= size
            if (
                context_component is not None
                and comp is context_component
                and context_meta
            ):
                author = (
                    f"{context_meta.get('name') or sender_name}"
                    f"（{context_meta.get('sender') or sender_id}）"
                )
            else:
                author = f"{sender_name or '用户'}（{sender_id}）"
            batch.append(original)
            batch.notes[original] = author
            if preview:
                batch.append(preview)
                batch.notes[preview] = author
                batch.descriptions[original] = (
                    "GIF 原文件；请结合随后同源的抽帧预览理解。"
                )
                batch.descriptions[preview] = description
        except asyncio.CancelledError:
            stop.set()
            if worker is not None:
                # Retrieve a late worker exception without delaying shutdown.
                worker.add_done_callback(discard_worker)
                try:
                    await asyncio.wait_for(asyncio.shield(worker), 0.25)
                except (Exception, asyncio.CancelledError):  # noqa: BLE001
                    pass
            raise
        except TimeoutError:
            stop.set()
            if worker is not None:
                worker.add_done_callback(discard_worker)
                try:
                    await asyncio.wait_for(asyncio.shield(worker), 0.25)
                except (Exception, asyncio.CancelledError):  # noqa: BLE001
                    pass
            batch.notices.append("附件读取超时，请稍后重发；较大的文件可以先压缩。")
            logger.warning("workbuddy_bridge: attachment processing timed out")
            if time.monotonic() >= deadline:
                break
        except AttachmentRejected as exc:
            batch.notices.append(str(exc))
            logger.warning(
                "workbuddy_bridge: attachment rejected by resource or decoding limits"
            )
        except Exception as exc:  # noqa: BLE001
            batch.notices.append("有个附件没能读取，请重新发送。")
            logger.warning(
                "workbuddy_bridge: attachment processing failed: %s", type(exc).__name__
            )
    if len(targets) > max_files:
        batch.notices.append(f"一条消息最多读取 {max_files} 个附件，多出的请分开发送。")
    batch.notices = list(dict.fromkeys(batch.notices))
    if batch:
        logger.info("workbuddy_bridge: saved %s attachment resources", len(batch))
    return batch
