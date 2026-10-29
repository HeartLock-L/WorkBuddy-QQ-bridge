"""Durable image progress and process-safe claims, without external services."""

import errno
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

STATES = {
    "queued",
    "submitting",
    "submitted",
    "ready",
    "sending",
    "delivered",
    "failed",
    "expired",
    "ended",
    "submission_unknown",
    "delivery_unknown",
}
MUTABLE = {
    "state",
    "session",
    "submit_id",
    "path",
    "error",
    "notice_sent",
    "next_retry_at",
    "send_attempts",
    "admin_request_ids",
    "manual_send_attempts",
    "local_deleted",
}


class ImageJobs:
    """Store one atomic JSON record per accepted request.

    Args:
        directory: Private local directory for job records and lock files.
        logger: Optional application logger.
        output_root: Trusted generation directory; absent disables local deletion.
        selfie_prefix: Exact legacy selfie prompt prefix; absent keeps old images.
    """

    def __init__(
        self, directory: Path, logger=None, *, output_root=None, selfie_prefix=""
    ) -> None:
        self.directory = Path(directory)
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.output_root = Path(output_root) if output_root is not None else None
        self.selfie_prefix = str(selfie_prefix or "")
        self._archive_cursor = ""
        self._cleanup_cursor = ""

    @staticmethod
    def _validate(job: dict, job_id: str) -> None:
        """Reject malformed progress before creating, reading, or updating it.

        Args:
            job: Complete persisted record.
            job_id: Identifier expected from the filename.

        Raises:
            ValueError: The record cannot safely be used for recovery.
        """
        if not isinstance(job, dict) or job.get("id") != job_id:
            raise ValueError("Image job identifier mismatch")
        if (
            type(job.get("version")) is not int
            or job["version"] != 1
            or not isinstance(job.get("state"), str)
            or job["state"] not in STATES
        ):
            raise ValueError("Invalid image job version or state")
        for name in ("umo", "sender_id", "nickname", "prompt"):
            if not isinstance(job.get(name), str) or not job[name].strip():
                raise ValueError(f"Image job requires {name}")
        for name in ("group_id", "ratio"):
            if not isinstance(job.get(name), str):
                raise ValueError(f"Invalid image job {name}")  # noqa: TRY004 - decoded record validation.
        for name in (
            "created_at",
            "updated_at",
            "deadline_at",
            "expires_at",
            "next_retry_at",
        ):
            value = job.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"Invalid image job {name}")  # noqa: TRY004 - decoded record validation.
        attempts = job.get("send_attempts")
        if type(attempts) is not int or attempts < 0:
            raise ValueError("Invalid image delivery attempt count")
        manual_attempts = job.get("manual_send_attempts", 0)
        if type(manual_attempts) is not int or manual_attempts < 0:
            raise ValueError("Invalid manual image delivery attempt count")
        request_ids = job.get("admin_request_ids", [])
        if (
            not isinstance(request_ids, list)
            or len(request_ids) > 3
            or any(
                not isinstance(value, str)
                or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in request_ids
            )
            or len(set(request_ids)) != len(request_ids)
        ):
            raise ValueError("Invalid manual image delivery request identifiers")
        settings = job.get("settings")
        if not isinstance(settings, dict):
            raise ValueError("Missing image settings")  # noqa: TRY004 - decoded record validation.
        for name in ("bin_path", "output_dir", "model", "ratio", "resolution"):
            if not isinstance(settings.get(name), str) or not settings[name].strip():
                raise ValueError(f"Invalid image setting {name}")
        timeout = settings.get("timeout")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Invalid image timeout")
        for name in ("session", "submit_id", "path", "error"):
            if name in job and not isinstance(job[name], str):
                raise ValueError(f"Invalid image job {name}")  # noqa: TRY004 - decoded record validation.
        if job["state"] == "submitted" and (
            not job.get("submit_id", "").strip()
            or not job.get("session", "").strip()
            or job["session"] == "0"
        ):
            raise ValueError(
                "Submitted image job requires its original remote identifier"
            )
        if "notice_sent" in job and type(job["notice_sent"]) is not bool:
            raise ValueError("Invalid image job notice state")
        for field in ("retain_local", "local_deleted"):
            if field in job and type(job[field]) is not bool:
                raise ValueError(f"Invalid image job {field}")

    def create(self, fields: dict) -> dict:
        """Persist a new request before any generation attempt.

        Args:
            fields: Source, request parameters, settings, and wall-clock deadlines.

        Returns:
            The newly saved record.

        Raises:
            ValueError: Required fields are malformed.
            OSError: Durable storage fails; the caller must not submit an image.
        """
        job = json.loads(json.dumps(fields, allow_nan=False))
        job_id = uuid.uuid4().hex
        now = time.time()
        job.update(
            id=job_id,
            version=1,
            state="queued",
            created_at=now,
            updated_at=now,
            next_retry_at=0,
            send_attempts=0,
        )
        self._validate(job, job_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{job_id}-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(job, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f"{job_id}.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return job

    def get(self, job_id: str) -> dict:
        """Read a record without mutating it.

        Args:
            job_id: Lowercase UUID hex identifier.

        Returns:
            A validated copy of the record.

        Raises:
            ValueError: The identifier or record is malformed.
            OSError: The record cannot be read.
        """
        if not isinstance(job_id, str) or re.fullmatch(r"[a-f0-9]{32}", job_id) is None:
            raise ValueError("Invalid image job identifier")
        job = json.loads(
            (self.directory / f"{job_id}.json").read_text(encoding="utf-8")
        )
        self._validate(job, job_id)
        return job

    def update(self, job_id: str, **changes) -> dict:
        """Atomically replace progress while preserving its original destination.

        Args:
            job_id: Identifier already claimed by the caller.
            **changes: Mutable execution and delivery progress only.

        Returns:
            The updated saved record.

        Raises:
            ValueError: A change alters immutable metadata or is malformed.
            OSError: Persistence fails, leaving the previous record intact.
        """
        if set(changes) - MUTABLE:
            raise ValueError("Image job origin and generation settings are immutable")
        job = self.get(job_id)
        # Cache housekeeping must not restart the business-history retention clock.
        job.update(
            changes,
            updated_at=job["updated_at"]
            if set(changes) == {"local_deleted"}
            else time.time(),
        )
        self._validate(job, job_id)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{job_id}-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(job, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / f"{job_id}.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return job

    def list_jobs(self) -> list[dict]:
        """List valid progress, preserving corrupt records for inspection.

        Returns:
            Records in creation order, or an empty list before the first request.
        """
        jobs = []
        for path in self.directory.glob("*.json"):
            try:
                jobs.append(self.get(path.stem))
            except (
                OSError,
                ValueError,
                TypeError,
                OverflowError,
                RecursionError,
            ) as exc:
                self.logger.warning(
                    "image_jobs: skipped invalid record %s: %s", path.name, exc
                )
        return sorted(jobs, key=lambda job: (job["created_at"], job["id"]))

    def cleanup_artifact(self, job_id: str, *, now=None) -> int:
        """Delete one eligible non-selfie cache while the caller holds its claim.

        Args:
            job_id: Identifier already claimed by the caller.
            now: Wall-clock timestamp, defaulting to the current time.

        Returns:
            Number of image files deleted. Unsafe locations and failed cleanup
            are retained for inspection or a later maintenance pass. No remote
            operation occurs; the saved task identifier and path remain intact.

        Raises:
            ValueError: The supplied wall-clock timestamp is invalid.
        """
        now = time.time() if now is None else now
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise ValueError("Invalid image cleanup time")
        if self.output_root is None:
            return 0
        try:
            job = self.get(job_id)
            retain = job.get("retain_local")
            if retain is None and self.selfie_prefix:
                retain = job["prompt"].startswith(self.selfie_prefix)
            if retain is not False or job.get("local_deleted") or not job.get("path"):
                return 0
            if job["state"] not in {"delivered", "ended"} and now < job["expires_at"]:
                return 0
            root = self.output_root
            output = Path(job["settings"]["output_dir"])
            path = Path(job["path"])
            if (
                not root.is_absolute()
                or not output.is_absolute()
                or not path.is_absolute()
                or re.fullmatch(r"[a-f0-9]{32}", output.name) is None
                or output.parent.resolve() != root.resolve()
                or not path.resolve().is_relative_to(output.resolve())
                or path == output
            ):
                raise OSError("Image cache path is outside the trusted task directory")
            for node in dict.fromkeys(
                (root, *root.parents, output, *output.parents, path, *path.parents)
            ):
                try:
                    info = node.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(info.st_mode) or (
                    getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    raise OSError("Image cache path contains a redirected component")
            removed = 0
            if path.exists():
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("Image cache path is not a regular file")
                with path.open("rb") as stream:
                    signature = stream.read(12)
                if not (
                    signature.startswith(b"\x89PNG\r\n\x1a\n")
                    or signature.startswith(b"\xff\xd8\xff")
                    or signature.startswith((b"GIF87a", b"GIF89a", b"BM"))
                    or signature.startswith(b"RIFF")
                    and signature[8:12] == b"WEBP"
                ):
                    raise OSError(
                        "Image cache does not have a supported image signature"
                    )
                path.unlink()
                removed = 1
            self.update(job_id, local_deleted=True)
            try:
                output.rmdir()
            except OSError:
                # Remove only an empty task directory, never other task artifacts.
                pass
            return removed
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            self.logger.warning("image_jobs: local image cleanup deferred: %s", exc)
            return 0

    def cleanup_artifacts(self, *, stop_event=None, limit=100, now=None) -> int:
        """Clean bounded live image caches under their existing process locks.

        Args:
            stop_event: Optional cooperative cancellation event.
            limit: Maximum records inspected during this maintenance pass.
            now: Wall-clock timestamp, defaulting to the current time.

        Returns:
            Number of image files deleted; unknown and busy tasks are preserved.

        Raises:
            ValueError: The pass limit or timestamp is invalid.
        """
        now = time.time() if now is None else now
        if (
            type(limit) is not int
            or limit < 1
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            raise ValueError("Invalid image cleanup limits")
        if self.output_root is None:
            return 0
        paths = sorted(self.directory.glob("*.json"), key=lambda path: path.name)
        after = [path for path in paths if path.name > self._cleanup_cursor]
        before = [path for path in paths if path.name <= self._cleanup_cursor]
        removed = 0
        for path in (after + before)[:limit]:
            if stop_event is not None and stop_event.is_set():
                break
            self._cleanup_cursor = path.name
            try:
                with self.claim(path.stem) as owned:
                    if owned and not (stop_event is not None and stop_event.is_set()):
                        removed += self.cleanup_artifact(path.stem, now=now)
            except (
                OSError,
                ValueError,
                TypeError,
                OverflowError,
                RecursionError,
            ) as exc:
                self.logger.warning(
                    "image_jobs: local image scan skipped record: %s", exc
                )
        return removed

    def archive_terminal(
        self, *, now=None, retention_days=30, limit=100, stop_event=None
    ) -> int:
        """Archive old terminal records after applying local cache retention.

        Args:
            now: Wall-clock timestamp, defaulting to the current time.
            retention_days: Days retained after the last progress update.
            limit: Maximum JSON records inspected during this maintenance pass.
            stop_event: Optional cooperative cancellation event.

        Returns:
            Number of records removed from the live recovery directory after
            their complete original bytes were durably published in the archive.

        Raises:
            ValueError: Time, retention, or pass limits are invalid.
        """
        now = time.time() if now is None else now
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
            or type(retention_days) is not int
            or retention_days < 1
            or type(limit) is not int
            or limit < 1
        ):
            raise ValueError("Invalid image archive limits")
        cutoff = now - retention_days * 86400
        terminal = {"delivered", "failed", "expired", "ended"}
        root = self.directory.absolute()
        if not root.exists():
            return 0
        if any(
            node.exists()
            and (
                node.is_symlink()
                or getattr(node.lstat(), "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            for node in (root, *root.parents)
        ):
            self.logger.warning("image_jobs: unsafe archive source directory")
            return 0
        paths = sorted(root.glob("*.json"), key=lambda path: path.name)
        # Rotate bounded parsing so retained active records cannot starve history.
        after = [path for path in paths if path.name > self._archive_cursor]
        before = [path for path in paths if path.name <= self._archive_cursor]
        archived = 0
        for path in (after + before)[:limit]:
            if stop_event is not None and stop_event.is_set():
                break
            self._archive_cursor = path.name
            temporary = None
            try:
                source_stat = path.lstat()
                if not stat.S_ISREG(source_stat.st_mode) or (
                    getattr(source_stat, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT
                ):
                    self.logger.warning("image_jobs: skipped redirected archive source")
                    continue
                job = self.get(path.stem)
                if job["state"] not in terminal or job["updated_at"] > cutoff:
                    continue
                with self.claim(job["id"]) as owned:
                    if not owned:
                        continue
                    # Re-read after locking: an administrator may have changed it.
                    job = self.get(job["id"])
                    if job["state"] not in terminal or job["updated_at"] > cutoff:
                        continue
                    if stop_event is not None and stop_event.is_set():
                        break
                    self.cleanup_artifact(job["id"], now=now)
                    job = self.get(job["id"])
                    retain = job.get("retain_local")
                    if retain is None and self.selfie_prefix:
                        retain = job["prompt"].startswith(self.selfie_prefix)
                    if (
                        self.output_root is not None
                        and retain is False
                        and job.get("path")
                        and not job.get("local_deleted")
                    ):
                        # Keep the live reference until local cleanup succeeds.
                        continue
                    archive_root = root / "archive"
                    month = time.strftime("%Y-%m", time.gmtime(job["updated_at"]))
                    directory = archive_root / month
                    for node in (archive_root, directory):
                        if node.exists() and (
                            not node.is_dir()
                            or node.is_symlink()
                            or getattr(node.lstat(), "st_file_attributes", 0)
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        ):
                            raise OSError("Unsafe image archive directory")
                        if not node.resolve().is_relative_to(root.resolve()):
                            raise OSError("Image archive escapes its source directory")
                    directory.mkdir(parents=True, exist_ok=True)
                    destination = directory / path.name
                    raw = path.read_bytes()
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=directory,
                        prefix=f".{job['id']}-",
                        suffix=".tmp",
                        delete=False,
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    try:
                        # Publish complete bytes exclusively; never replace history.
                        os.link(temporary, destination)
                    except FileExistsError:
                        previous = destination.lstat()
                        if (
                            not stat.S_ISREG(previous.st_mode)
                            or getattr(previous, "st_file_attributes", 0)
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT
                            or destination.read_bytes() != raw
                        ):
                            raise OSError("Conflicting image archive record") from None
                    # A crash before unlink leaves identical copies for the next pass.
                    path.unlink()
                    archived += 1
            except (
                OSError,
                ValueError,
                TypeError,
                OverflowError,
                RecursionError,
            ) as exc:
                self.logger.warning(
                    "image_jobs: archive skipped record %s: %s", path.name, exc
                )
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return archived

    @contextmanager
    def claim(self, job_id: str):
        """Hold a nonblocking OS lock across generation and delivery.

        Args:
            job_id: Existing job's lowercase UUID hex identifier.

        Yields:
            True if this process owns the job; False while another worker owns it.

        Raises:
            ValueError: The identifier or stored record is invalid.
            OSError: The lock cannot be opened for a reason other than contention.
        """
        self.get(job_id)
        with (self.directory / f"{job_id}.lock").open("a+b") as stream:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            acquired = False
            try:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                yield acquired
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
