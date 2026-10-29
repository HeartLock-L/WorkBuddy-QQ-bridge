"""Bounded model admission and durable remote-run ownership without network I/O."""

import asyncio
import errno
import json
import logging
import math
import os
import re
import tempfile
from collections import deque
from pathlib import Path


class RequestQueue:
    """Serialize model requests and preserve remote-run evidence across reloads.

    Args:
        state_path: Private JSON file holding the current remote run.
        max_waiters: Maximum waiting human requests, excluding the active owner.
        wait_timeout: Default admission deadline in seconds.
        logger: Optional application logger.
    """

    def __init__(self, state_path: Path, max_waiters=5, wait_timeout=60, logger=None):
        if type(max_waiters) is not int or max_waiters < 0:
            raise ValueError("max_waiters must be a nonnegative integer")
        if (
            isinstance(wait_timeout, bool)
            or not isinstance(wait_timeout, (int, float))
            or not math.isfinite(wait_timeout)
            or wait_timeout < 0
        ):
            raise ValueError("wait_timeout must be finite and nonnegative")
        self.state_path = Path(state_path)
        self.max_waiters = max_waiters
        self.wait_timeout = float(wait_timeout)
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self._waiters: deque[asyncio.Event] = deque()
        self._active = False
        self._closed = False
        self._lock_stream = None

    @property
    def busy(self) -> bool:
        """Return whether this instance has an owner or queued human requests."""
        return self._active or bool(self._waiters)

    @property
    def waiting_count(self) -> int:
        """Return the number of human requests waiting for admission."""
        return len(self._waiters)

    def _try_claim(self) -> bool:
        """Try to claim the shared lock without waiting or touching remote state.

        Returns:
            True if this instance now owns the OS lock.

        Raises:
            OSError: The lock cannot be opened for a reason other than contention.
        """
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.state_path.with_suffix(".lock").open("a+b")
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                return False
            self._lock_stream = stream
            self._active = True
            return True
        finally:
            if self._lock_stream is not stream:
                stream.close()

    async def acquire(self, *, background=False, timeout=None) -> str:
        """Admit a human in FIFO order, or skip optional work when busy.

        Args:
            background: Optional work that must never join the human queue.
            timeout: Optional admission budget overriding the default.

        Returns:
            acquired, busy, full, expired, or closed.

        Raises:
            asyncio.CancelledError: Admission was cancelled; no slot is retained.
            ValueError: The requested timeout is invalid.
            OSError: The shared lock cannot be opened safely.
        """
        budget = self.wait_timeout if timeout is None else timeout
        if (
            isinstance(budget, bool)
            or not isinstance(budget, (int, float))
            or not math.isfinite(budget)
            or budget < 0
        ):
            raise ValueError("timeout must be finite and nonnegative")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget
        if self._closed:
            return "closed"
        if background:
            # Humans admitted in the same event-loop turn get the first chance.
            await asyncio.sleep(0)
            if self._closed:
                return "closed"
            if self.busy:
                return "busy"
            return "acquired" if self._try_claim() else "busy"
        if not self.busy and self._try_claim():
            return "acquired"
        if len(self._waiters) >= self.max_waiters:
            return "full"
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            while True:
                waiter.clear()
                if self._closed:
                    return "closed"
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return "expired"
                is_head = bool(self._waiters) and self._waiters[0] is waiter
                if is_head and not self._active:
                    if self._try_claim():
                        self._waiters.popleft()
                        return "acquired"
                    # Other plugin instances cannot signal this process's event.
                    remaining = min(remaining, 0.05)
                try:
                    await asyncio.wait_for(waiter.wait(), remaining)
                except asyncio.TimeoutError:
                    continue
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            if self._waiters and not self._active:
                self._waiters[0].set()

    def release(self) -> None:
        """Release the local and OS slots and wake the next human waiter."""
        stream = self._lock_stream
        if stream is None:
            return
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()
            self._lock_stream = None
            self._active = False
            if self._waiters:
                self._waiters[0].set()

    def close(self) -> None:
        """Reject new work and wake waiters without releasing an active owner."""
        self._closed = True
        waiting = list(self._waiters)
        self._waiters.clear()
        for waiter in waiting:
            waiter.set()

    @staticmethod
    def _validate_remote(record: dict) -> None:
        """Validate the small allowlisted remote-run record.

        Args:
            record: Decoded JSON object containing no prompts or credentials.

        Raises:
            ValueError: The record is malformed or contains unexpected fields.
        """
        keys = {"base", "request_id", "run_id", "started_at"}
        if not isinstance(record, dict) or set(record) != keys:
            raise ValueError("Invalid remote-run record fields")
        base = record["base"]
        match = (
            re.fullmatch(r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})", base)
            if isinstance(base, str)
            else None
        )
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("Remote-run base must be a loopback HTTP endpoint")
        if (
            not isinstance(record["request_id"], str)
            or not record["request_id"].strip()
        ):
            raise ValueError("Missing remote request identifier")
        if not isinstance(record["run_id"], str):
            raise ValueError("Invalid remote run identifier")
        started = record["started_at"]
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
            or started <= 0
        ):
            raise ValueError("Invalid remote-run start time")

    def read_remote(self) -> dict | None:
        """Read remote ownership evidence without clearing malformed state.

        Returns:
            A validated record, or None only when the file is absent.

        Raises:
            ValueError: Stored JSON or its schema is invalid.
            OSError: The record cannot be read.
        """
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        record = json.loads(raw)
        self._validate_remote(record)
        return record

    def save_remote(self, record: dict) -> None:
        """Atomically save ownership evidence while holding the admission lock.

        Args:
            record: Validated loopback address and identifiers for the remote run.

        Raises:
            RuntimeError: This instance does not own the admission lock.
            ValueError: New or existing state is malformed.
            OSError: Durable storage fails, preserving the previous record.
        """
        if not self._active or self._lock_stream is None:
            raise RuntimeError("Remote state requires the active request lock")
        self._validate_remote(record)
        self.read_remote()  # A damaged existing record must never be overwritten silently.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.state_path.parent,
                prefix=f".{self.state_path.name}-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def clear_remote(self) -> None:
        """Remove remote evidence only while this instance owns admission.

        Raises:
            RuntimeError: This instance does not own the request lock.
            OSError: The stored record cannot be removed.
        """
        if not self._active or self._lock_stream is None:
            raise RuntimeError("Remote state requires the active request lock")
        self.state_path.unlink(missing_ok=True)
