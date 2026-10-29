"""Collect explicitly marked QQ stickers into bounded, conversation-local storage."""

import asyncio
import errno
import hashlib
import logging
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

MAX_BYTES = 10 * 1024 * 1024
TOTAL_BYTES = 100 * 1024 * 1024
EXTENSIONS = {".jpg", ".png", ".gif", ".webp", ".bmp"}


def select_sticker(event, image_type):
    """Select the first direct image explicitly marked as a QQ sticker.

    Args:
        event: Original messaging event, including its raw OneBot payload.
        image_type: The framework's Image component class.

    Returns:
        A matching component, or None for ordinary pictures, private messages,
        forwarded content, inconsistent sources, or unsupported event metadata.
    """
    try:
        if event.is_private_chat():
            return None
        group = str(event.get_group_id() or "")
        sender = str(event.get_sender_id() or "")
        own = str(event.get_self_id() or "")
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if (
            not group
            or not sender
            or not own
            or sender == own
            or not isinstance(raw, dict)
            or raw.get("post_type") != "message"
            or raw.get("message_type") != "group"
            or str(raw.get("group_id", "")) != group
            or str(raw.get("user_id", "")) != sender
            or str(raw.get("self_id", "")) != own
            or not isinstance(raw.get("message"), list)
        ):
            return None
        raw_images = [
            segment.get("data")
            for segment in raw["message"]
            if isinstance(segment, dict) and segment.get("type") == "image"
        ]
        images = [part for part in event.get_messages() if isinstance(part, image_type)]
        if len(raw_images) != len(images):
            return None
        for data, image in zip(raw_images, images, strict=True):
            if not isinstance(data, dict):
                return None
            subtype = data.get("sub_type")
            if not (
                type(subtype) in (int, str)
                and subtype in (1, "1")
                or data.get("file") == "marketface"
            ):
                continue
            declared = data.get("file_size")
            if isinstance(declared, (str, int)) and str(declared).isdigit():
                if int(declared) > MAX_BYTES:
                    continue
            raw_url = str(data.get("url") or "")
            component_url = str(getattr(image, "url", "") or "")
            if raw_url and component_url and raw_url != component_url:
                continue
            return image
    except (AttributeError, TypeError, ValueError):
        return None
    return None


def _guard(path):
    """Reject redirected paths before inspecting or modifying managed storage.

    Args:
        path: Absolute candidate path and its existing ancestors.

    Raises:
        OSError: A symbolic link or Windows reparse point is present.
    """
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise OSError("Redirected sticker storage is not allowed")


class Collector:
    """Limit background collection and retain only owned sticker files.

    Args:
        root: Existing manual sticker library; collection uses its own subfolder.
        logger: Optional bridge logger.
        per_group: Maximum collected stickers retained per conversation.
        total_files: Maximum collected stickers across conversations.
        total_bytes: Maximum bytes retained in owned collected files.
        keep_days: Days since last appearance before a sticker expires.
        cooldown: Minimum seconds between accepted collection events per group.
    """

    def __init__(
        self,
        root,
        *,
        logger=None,
        per_group=100,
        total_files=500,
        total_bytes=TOTAL_BYTES,
        keep_days=30,
        cooldown=5,
    ):
        self.root = Path(root).absolute() / "自动收集"
        self.logger = logger or logging.getLogger(__name__)
        self.per_group = per_group
        self.total_files = total_files
        self.total_bytes = total_bytes
        self.keep_days = keep_days
        self.cooldown = cooldown
        if any(
            type(n) is not int or n <= 0
            for n in (per_group, total_files, total_bytes, keep_days)
        ):
            raise ValueError("Invalid sticker retention limits")
        self.active = 0
        self.last_accepted = {}

    def schedule(self, umo, component, *, materialize, spawn):
        """Reserve bounded background work without slowing normal QQ handling.

        Args:
            umo: Complete original conversation identifier.
            component: Direct image selected by select_sticker.
            materialize: Existing async component-to-local-file adapter.
            spawn: Bridge task tracker; returns a task or None during shutdown.

        Returns:
            Whether a background collection task was accepted.
        """
        now = time.monotonic()
        if not isinstance(umo, str) or not umo or component is None or self.active >= 2:
            return False
        previous = self.last_accepted.get(umo)
        if previous is not None and now - previous < self.cooldown:
            return False
        self.last_accepted = {
            key: stamp
            for key, stamp in self.last_accepted.items()
            if now - stamp < self.cooldown
        }
        self.last_accepted[umo] = now
        self.active += 1
        coroutine = self._collect(umo, component, materialize)
        try:
            task = spawn(coroutine)
        except Exception:
            coroutine.close()
            self.active -= 1
            raise
        if task is None:
            coroutine.close()
            self.active -= 1
            return False
        task.add_done_callback(lambda _task: setattr(self, "active", self.active - 1))
        return True

    async def _collect(self, umo, component, materialize):
        """Materialize one sticker and await its bounded storage worker.

        Args:
            umo: Complete original conversation identifier.
            component: Verified direct sticker image component.
            materialize: Async framework adapter returning the source path.
        """
        stop = threading.Event()
        worker = None
        deadline = time.monotonic() + 20
        try:
            local = await asyncio.wait_for(materialize(component), timeout=15)
            if not local:
                return
            worker = asyncio.create_task(
                asyncio.to_thread(self._store, umo, local, stop, deadline)
            )
            await asyncio.wait_for(
                asyncio.shield(worker), max(0, deadline - time.monotonic())
            )
        except asyncio.CancelledError:
            stop.set()
            if worker is not None:
                # The bounded local worker must release its collection lock.
                await asyncio.gather(worker, return_exceptions=True)
            raise
        except Exception as exc:  # noqa: BLE001 - optional collection never breaks a reply.
            stop.set()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)
            self.logger.warning(
                "meme_collection: skipped sticker: %s", type(exc).__name__
            )

    @contextmanager
    def _claim(self):
        """Hold the collection lock across quota checks and filesystem changes.

        Yields:
            True while this process owns collection, or False during contention.

        Raises:
            OSError: Storage or locking fails for reasons other than contention.
        """
        _guard(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".collection.lock"
        _guard(lock_path)
        with lock_path.open("a+b") as lock:
            if not lock.tell():
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            acquired = False
            try:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                yield acquired
            finally:
                if acquired:
                    lock.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _remove_collected(self, path):
        """Remove one owned image and its optional same-fingerprint name record.

        Args:
            path: Image already selected for expiration or quota eviction.

        Raises:
            OSError: The image is outside managed storage or cannot be deleted.
        """
        if (
            path.parent.parent != self.root
            or re.fullmatch(r"[a-f0-9]{16}", path.parent.name) is None
            or re.fullmatch(r"[a-f0-9]{64}", path.stem) is None
            or path.suffix not in EXTENSIONS
        ):
            raise OSError("Sticker is outside managed storage")
        _guard(path)
        path.unlink()
        names = path.parent / ".names"
        record = names / (path.stem + ".json")
        try:
            _guard(record)
            if record.is_file():
                record.unlink()
            if names.is_dir():
                names.rmdir()
        except OSError:
            # Retry leftover metadata on maintenance without failing image removal.
            pass
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def _prune_names(self, group, stop, deadline, budget):
        """Remove bounded orphan name records inside one collected group.

        Args:
            group: Validated collection directory, never the manual library.
            stop: Optional cooperative cancellation event.
            deadline: Shared monotonic maintenance deadline.
            budget: Remaining entries allowed by the shared inventory limit.

        Returns:
            Entries examined and whether this directory was fully inspected.
            Metadata failures never fail image collection or expiration.
        """
        examined = 0
        names = group / ".names"
        try:
            _guard(names)
            if not names.is_dir():
                return 0, True
            for record in names.iterdir():
                if (
                    (stop is not None and stop.is_set())
                    or time.monotonic() >= deadline
                    or examined >= budget
                ):
                    return examined, False
                examined += 1
                if (
                    record.suffix != ".json"
                    or re.fullmatch(r"[a-f0-9]{64}", record.stem) is None
                ):
                    continue
                try:
                    _guard(record)
                    if not record.is_file():
                        continue
                    if any(
                        (group / (record.stem + extension)).exists()
                        for extension in EXTENSIONS
                    ):
                        continue
                    record.unlink()
                except OSError:
                    continue
            try:
                names.rmdir()
                group.rmdir()
            except OSError:
                pass
        except OSError:
            pass
        return examined, True

    def _scan_prune(self, stop, deadline):
        """Inspect bounded owned storage and remove expired collected stickers.

        Args:
            stop: Optional cancellation event.
            deadline: Monotonic deadline for inventory work.

        Returns:
            Remaining file records, number deleted, and inventory completeness.
            The caller holds the collection lock throughout this operation.
        """
        entries = []
        examined = 0
        complete = True
        for group in self.root.iterdir():
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                return entries, 0, False
            examined += 1
            if examined > 2048:
                complete = False
                break
            if not re.fullmatch(r"[a-f0-9]{16}", group.name):
                continue
            try:
                _guard(group)
            except OSError:
                continue
            if not group.is_dir():
                continue
            for file in group.iterdir():
                if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                    return entries, 0, False
                examined += 1
                if examined > 2048:
                    complete = False
                    break
                if file.suffix not in EXTENSIONS or not re.fullmatch(
                    r"[a-f0-9]{64}", file.stem
                ):
                    continue
                if len(entries) >= self.total_files:
                    complete = False
                    break
                info = file.lstat()
                if (
                    not stat.S_ISREG(info.st_mode)
                    or getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    continue
                entries.append((file, info.st_mtime, info.st_size))
            if not complete:
                break
            name_count, names_complete = self._prune_names(
                group, stop, deadline, 2048 - examined
            )
            examined += name_count
            if not names_complete:
                complete = False
                break
        entries.sort(key=lambda row: row[1])
        cutoff = time.time() - self.keep_days * 86400
        removed = 0
        for entry in list(entries):
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                return entries, removed, False
            if entry[1] < cutoff:
                self._remove_collected(entry[0])
                entries.remove(entry)
                removed += 1
        return entries, removed, complete

    def prune(self, stop_event=None):
        """Expire collected stickers without touching the manual library.

        Args:
            stop_event: Optional event controlled by bridge maintenance.

        Returns:
            Number of owned expired sticker files deleted in this bounded pass.
        """
        if not self.root.exists() or (stop_event is not None and stop_event.is_set()):
            return 0
        try:
            with self._claim() as acquired:
                if not acquired:
                    return 0
                _entries, removed, _complete = self._scan_prune(
                    stop_event, time.monotonic() + 5
                )
                return removed
        except OSError as exc:
            self.logger.warning(
                "meme_collection: cleanup skipped: %s", type(exc).__name__
            )
            return 0

    def _store(self, umo, local, stop, deadline):
        """Atomically deduplicate and retain a validated sticker under an OS lock.

        Args:
            umo: Original conversation identifier, stored only as a hash.
            local: Framework-owned source file; never modified or deleted here.
            stop: Cooperative cancellation flag for this copy.
            deadline: Monotonic time after which work stops.

        Returns:
            The stored path, or None if another process owns collection.

        Raises:
            OSError: Storage or source validation fails.
            ValueError: The file is oversized or not a supported image.
            TimeoutError: Cancellation or the deadline stops the copy.
        """
        source = Path(local)
        if not source.is_file() or source.stat().st_size > MAX_BYTES:
            raise ValueError("Invalid sticker size")
        scope = hashlib.sha256(umo.encode("utf-8")).hexdigest()[:16]
        directory = self.root / scope
        temporary = None
        with self._claim() as acquired:
            if not acquired:
                return None
            try:
                _guard(directory)
                directory.mkdir(exist_ok=True)
                temporary = directory / f".incoming-{uuid.uuid4().hex}.tmp"
                digest = hashlib.sha256()
                copied = 0
                header = b""
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    while True:
                        if stop.is_set() or time.monotonic() >= deadline:
                            raise TimeoutError
                        block = incoming.read(min(64 * 1024, MAX_BYTES - copied + 1))
                        if not block:
                            break
                        copied += len(block)
                        if copied > MAX_BYTES:
                            raise ValueError("Sticker exceeds attachment limit")
                        header = (header + block)[:12]
                        digest.update(block)
                        outgoing.write(block)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if header.startswith(b"\x89PNG\r\n\x1a\n"):
                    extension = ".png"
                elif header.startswith(b"\xff\xd8\xff"):
                    extension = ".jpg"
                elif header.startswith((b"GIF87a", b"GIF89a")):
                    extension = ".gif"
                elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
                    extension = ".webp"
                elif header.startswith(b"BM"):
                    extension = ".bmp"
                else:
                    raise ValueError("Unsupported sticker image")
                if copied > self.total_bytes:
                    raise ValueError("Sticker exceeds collection quota")
                target = directory / (digest.hexdigest() + extension)
                _guard(target)
                duplicate = target.exists()
                if duplicate:
                    if not target.is_file() or target.stat().st_size != copied:
                        raise OSError("Conflicting collected sticker")
                    existing_digest = hashlib.sha256()
                    existing_size = 0
                    with target.open("rb") as existing:
                        while True:
                            if stop.is_set() or time.monotonic() >= deadline:
                                raise TimeoutError
                            block = existing.read(
                                min(64 * 1024, MAX_BYTES - existing_size + 1)
                            )
                            if not block:
                                break
                            existing_size += len(block)
                            if existing_size > MAX_BYTES:
                                raise OSError(
                                    "Collected sticker changed during verification"
                                )
                            existing_digest.update(block)
                    if (
                        existing_size != copied
                        or existing_digest.digest() != digest.digest()
                    ):
                        raise OSError(
                            "Collected sticker content does not match its identifier"
                        )
                    os.utime(target, None)
                entries, _removed, complete = self._scan_prune(stop, deadline)
                if duplicate:
                    return str(target)
                if not complete:
                    raise OSError("Collection inventory exceeds scan budget")
                while (
                    sum(path.parent == directory for path, _, _ in entries)
                    >= self.per_group
                    or len(entries) >= self.total_files
                    or sum(size for _, _, size in entries) + copied > self.total_bytes
                ):
                    if stop.is_set() or time.monotonic() >= deadline:
                        raise TimeoutError
                    own = [entry for entry in entries if entry[0].parent == directory]
                    victim = own[0] if len(own) >= self.per_group else entries[0]
                    self._remove_collected(victim[0])
                    entries.remove(victim)
                if stop.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError
                _guard(target)
                # Exclusive publication never replaces an unexpected file.
                os.link(temporary, target)
                return str(target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
