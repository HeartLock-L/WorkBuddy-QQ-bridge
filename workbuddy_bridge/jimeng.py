"""Standalone Dreamina CLI client used by the QQ bridge.

This module uses only the standard library. The caller owns authorization,
request scheduling and message delivery; the CLI owns its existing login state.
Importing this module does not start a process or create any files.
"""

import json
import logging
import os
import re
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SESSION_NAME_MAX = 50
SESSION_ROW = re.compile(
    r"^(\d+)\s+(.*?)\s+(Yes|No)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$"
)


@dataclass(frozen=True)
class JimengSettings:
    """Resolved settings for one image request.

    Attributes:
        bin_path: Existing Dreamina executable, including its configured location.
        output_dir: Directory where the CLI downloads generated images.
        model: Explicit model selected by the caller.
        ratio: Default aspect ratio when the request does not specify one.
        resolution: Image resolution passed to the CLI.
        timeout: Total time allowed for one operation, in seconds.
    """

    bin_path: str
    output_dir: Path
    model: str
    ratio: str
    resolution: str
    timeout: int


class SubmissionUncertain(RuntimeError):
    """Represent a submission whose remote outcome cannot be safely inferred."""


class GenerationFailed(RuntimeError):
    """Represent an explicit terminal failure reported by the image service."""


def session_label(nickname: str, user_id: str) -> str:
    """Build a bounded session label while preserving the user suffix.

    Args:
        nickname: Display name used for the session label.
        user_id: Stable identifier used to recover the user's session.

    Returns:
        A label containing at most 50 characters.
    """
    tail = f"使用 · {user_id}"
    budget = SESSION_NAME_MAX - len(tail)
    name = (nickname or "").strip() or "群友"
    if budget < 1:
        return tail[-SESSION_NAME_MAX:]
    return f"{name[:budget]}{tail}"


class JimengClient:
    """Manage reusable sessions and image generation through the installed CLI.

    Args:
        state_file: Existing user-to-session mapping, without a schema migration.
        logger: Optional application logger; otherwise use a standard logger.
    """

    def __init__(self, state_file: Path, logger=None) -> None:
        self.state_file = Path(state_file)
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self._state_lock = threading.Lock()
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("jimeng: cannot read session mapping: %s", exc)
            data = {}
        self.sessions = (
            {
                str(key): str(value)
                for key, value in data.items()
                if str(key).isdigit() and str(value).isdigit() and str(value) != "0"
            }
            if isinstance(data, dict)
            else {}
        )

    @staticmethod
    def _remaining(deadline: float, cancel_event: threading.Event | None) -> float:
        """Check cancellation and return the remaining operation budget.

        Args:
            deadline: Absolute monotonic deadline.
            cancel_event: Optional cooperative shutdown signal.

        Returns:
            Positive seconds remaining.

        Raises:
            InterruptedError: The caller requested cancellation.
            TimeoutError: The operation exhausted its total budget.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Image operation cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Image operation timed out")
        return remaining

    def _run(
        self,
        args: list[str],
        settings: JimengSettings,
        deadline: float,
        cancel_event: threading.Event | None,
    ):
        """Run a bounded CLI command inside the shared operation budget.

        Args:
            args: CLI operation and arguments after the executable.
            settings: Executable selected by the caller.
            deadline: Absolute monotonic deadline shared by all commands.
            cancel_event: Optional cooperative shutdown signal.

        Returns:
            The completed process, including a nonzero exit code.

        Raises:
            TimeoutError: This command exceeded its bounded wait.
            InterruptedError: Cancellation was requested before starting it.
        """
        timeout = min(30.0, self._remaining(deadline, cancel_event))
        try:
            return subprocess.run(
                [settings.bin_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Image CLI command timed out") from exc

    def session_list(
        self,
        settings: JimengSettings,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, str]:
        """Read and parse the CLI's recent session table.

        Args:
            settings: Executable and timeout for this request.
            deadline: Optional absolute monotonic deadline.
            cancel_event: Optional cooperative shutdown signal.

        Returns:
            Session identifiers mapped to their display names.

        Raises:
            RuntimeError: The command fails or its table cannot be parsed.
        """
        if deadline is None:
            deadline = time.monotonic() + settings.timeout
        proc = self._run(
            ["session", "list", "-n", "100"], settings, deadline, cancel_event
        )
        self._remaining(deadline, cancel_event)
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())[:160]
            raise RuntimeError("Image session list failed: " + (detail or "no output"))
        rows = {}
        for line in (proc.stdout or "").splitlines():
            match = SESSION_ROW.match(line.strip())
            if match:
                rows[match.group(1)] = match.group(2).strip()
        if not rows:
            detail = " ".join((proc.stdout or "").split())[:120]
            raise RuntimeError("Image session list unparsable: " + (detail or "empty"))
        return rows

    def _remember_session(self, user_id: str, session: str) -> None:
        """Persist a session binding without concurrent temporary-file writes.

        Args:
            user_id: Owner of the reusable session.
            session: Non-default session identifier returned by the CLI.
        """
        if not user_id or not session or str(session) == "0":
            return
        with self._state_lock:
            if self.sessions.get(user_id) == str(session):
                return
            self.sessions[user_id] = str(session)
            try:
                temporary = self.state_file.with_name(self.state_file.name + ".tmp")
                temporary.write_text(
                    json.dumps(self.sessions, ensure_ascii=False, indent=0),
                    encoding="utf-8",
                )
                os.replace(temporary, self.state_file)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("jimeng: cannot save session mapping: %s", exc)

    def ensure_session(
        self,
        nickname: str,
        user_id: str,
        settings: JimengSettings,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Reuse the owner's session or create one using the existing policy.

        Args:
            nickname: Display name for a newly created session.
            user_id: Stable owner identifier used in the local mapping.
            settings: Executable and timeout for this request.
            deadline: Optional absolute monotonic deadline shared with generation.
            cancel_event: Optional cooperative shutdown signal.

        Returns:
            A non-default session identifier.

        Raises:
            RuntimeError: The CLI fails or returns an invalid session identifier.
        """
        if deadline is None:
            deadline = time.monotonic() + settings.timeout
        self._remaining(deadline, cancel_event)
        user_id = str(user_id or "").strip()
        known = str(self.sessions.get(user_id) or "")
        # The CLI only lists its 100 most recent sessions. Absence from that
        # page is not evidence that an older saved binding was deleted.
        if known.isdigit() and known != "0":
            return known
        rows = self.session_list(settings, deadline=deadline, cancel_event=cancel_event)
        if user_id:
            tail = f"· {user_id}"
            for session, name in rows.items():
                if session != "0" and name.rstrip().endswith(tail):
                    self._remember_session(user_id, session)
                    self.logger.info(
                        "jimeng: recovered session %s by owner suffix", session
                    )
                    return session
        proc = self._run(
            ["session", "create", session_label(nickname, user_id)],
            settings,
            deadline,
            cancel_event,
        )
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())[:160]
            raise RuntimeError(
                "Image session creation failed: " + (detail or "no output")
            )
        matches = re.findall(r"\bID:\s*([1-9]\d*)\b", proc.stdout or "", re.IGNORECASE)
        if len(matches) != 1:
            raise RuntimeError(
                "Image session creation returned no unique non-default ID"
            )
        self._remember_session(user_id, matches[0])
        self._remaining(deadline, cancel_event)
        self.logger.info("jimeng: created session %s", matches[0])
        return matches[0]

    def rename_session(
        self,
        session: str,
        nickname: str,
        user_id: str,
        settings: JimengSettings,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Restore the owner's label after the CLI names a session from its prompt.

        Args:
            session: Reusable session identifier.
            nickname: Display name used in the label.
            user_id: Stable owner suffix.
            settings: Executable and timeout for this request.
            deadline: Optional absolute monotonic deadline.
            cancel_event: Optional cooperative shutdown signal.

        Returns:
            Whether the rename completed successfully.
        """
        if deadline is None:
            deadline = time.monotonic() + settings.timeout
        self._remaining(deadline, cancel_event)
        if not session or session == "0" or not user_id:
            return False
        try:
            proc = self._run(
                [
                    "session",
                    "rename",
                    str(session),
                    session_label(nickname, user_id),
                ],
                settings,
                deadline,
                cancel_event,
            )
            self._remaining(deadline, cancel_event)
        except (TimeoutError, InterruptedError):
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("jimeng: session rename failed: %s", exc)
            return False
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or proc.stdout or "").split())[:160]
            self.logger.warning(
                "jimeng: session rename rejected: %s", detail or "no output"
            )
            return False
        return True

    def generate(
        self,
        prompt: str,
        *,
        session: str,
        settings: JimengSettings,
        ratio: str = "",
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
        submit_id: str = "",
        on_submitted=None,
    ) -> str:
        """Submit at most once, or resume querying an existing image task.

        Args:
            prompt: Requested image description; unused when resuming a task.
            session: Owner's non-default session identifier.
            settings: Executable, output directory and generation parameters.
            ratio: Optional aspect ratio overriding the configured default.
            deadline: Optional absolute monotonic deadline shared with setup.
            cancel_event: Optional cooperative shutdown signal.
            submit_id: Existing task identifier; prevents any new submission.
            on_submitted: Callback that persists the identifier before querying.

        Returns:
            An existing regular image file inside this request's output directory.

        Raises:
            SubmissionUncertain: Submission has no trustworthy task identifier.
            GenerationFailed: The service explicitly reports a terminal failure.
            TimeoutError: The total budget or a bounded CLI wait is exhausted.
            InterruptedError: The caller requested cancellation.
            RuntimeError: Repeated query errors or invalid image results.
        """
        if deadline is None:
            deadline = time.monotonic() + settings.timeout
        self._remaining(deadline, cancel_event)
        if not session or str(session) == "0":
            raise RuntimeError(
                "An independent image session is required before generation"
            )
        out_dir = Path(settings.output_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        submit_id = str(submit_id or "").strip()
        failed_statuses = {"failed", "fail", "error", "not_pass"}
        if not submit_id:
            # A lost response does not prove that the remote submission failed.
            # Never replay this command, even for authentication-looking errors.
            try:
                proc = self._run(
                    [
                        "text2image",
                        "--prompt",
                        prompt,
                        "--ratio",
                        ratio or settings.ratio,
                        "--resolution_type",
                        settings.resolution,
                        "--model_version",
                        settings.model,
                        "--generate_num",
                        "1",
                        "--session",
                        str(session),
                        "--poll",
                        "0",
                    ],
                    settings,
                    deadline,
                    cancel_event,
                )
                raw = (proc.stdout or "").strip()
                if proc.returncode != 0:
                    raise RuntimeError(f"CLI submission exited with {proc.returncode}")
                submit = json.loads(raw)
                if not isinstance(submit, dict):
                    raise ValueError("Submission response is not an object")  # noqa: TRY004 - invalid CLI JSON.
            except (TimeoutError, OSError, RuntimeError, ValueError) as exc:
                # InterruptedError is an OSError, but a pre-command stop proves
                # that no command was launched and must stay a cancellation.
                if isinstance(exc, InterruptedError):
                    raise
                raise SubmissionUncertain(
                    "Image submission outcome is unknown; do not resubmit"
                ) from exc
            raw_id = submit.get("submit_id")
            submit_id = (
                str(raw_id).strip()
                if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool)
                else ""
            )
            if not submit_id:
                status = str(submit.get("gen_status") or "").strip().lower()
                if status in failed_statuses:
                    raise GenerationFailed(
                        f"Generation failed: {submit.get('fail_reason') or status}"
                    )
                raise SubmissionUncertain(
                    "Image submission returned no task identifier; do not resubmit"
                )
            # Persist a known ID even if cancellation or expiry arrived while
            # the subprocess was completing. Recovery must query this same ID.
            if on_submitted is not None:
                on_submitted(submit_id)
            status = str(submit.get("gen_status") or "").strip().lower()
            if status in failed_statuses:
                raise GenerationFailed(
                    f"Generation failed: {submit.get('fail_reason') or status}"
                )

        errors = 0
        while True:
            self._remaining(deadline, cancel_event)
            delay = 5.0
            try:
                proc = self._run(
                    [
                        "query_result",
                        "--submit_id",
                        submit_id,
                        "--download_dir",
                        str(out_dir),
                    ],
                    settings,
                    deadline,
                    cancel_event,
                )
                self._remaining(deadline, cancel_event)
                if proc.returncode != 0:
                    raise RuntimeError(f"CLI query exited with {proc.returncode}")
                result = json.loads((proc.stdout or "").strip())
                if not isinstance(result, dict):
                    raise ValueError("Query response is not an object")  # noqa: TRY004 - invalid CLI JSON.
                status = str(result.get("gen_status") or "").strip().lower()
                if not status:
                    raise RuntimeError("Image query returned no generation status")
            except (TimeoutError, InterruptedError):
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                errors += 1
                if errors >= 3:
                    raise RuntimeError(
                        "Image query failed three times; the submitted task is retained"
                    ) from exc
                self.logger.warning(
                    "jimeng: retrying query for existing task %s", submit_id
                )
                delay = 2.0
            else:
                errors = 0
                if status in failed_statuses:
                    raise GenerationFailed(
                        f"Generation failed: {result.get('fail_reason') or status}"
                    )
                if status == "success":
                    body = result.get("result_json")
                    images = body.get("images", []) if isinstance(body, dict) else []
                    if isinstance(images, list):
                        for item in images:
                            raw_path = (
                                item.get("path") if isinstance(item, dict) else None
                            )
                            if not isinstance(raw_path, str) or not raw_path.strip():
                                continue
                            candidate = Path(raw_path)
                            if not candidate.is_absolute():
                                candidate = out_dir / candidate
                            try:
                                resolved = candidate.resolve(strict=True)
                                resolved.relative_to(out_dir)
                                if candidate.is_symlink() or not stat.S_ISREG(
                                    resolved.stat().st_mode
                                ):
                                    continue
                            except (OSError, ValueError, RuntimeError):
                                continue
                            self._remaining(deadline, cancel_event)
                            return str(resolved)
                    raise RuntimeError(
                        "Generation succeeded without a usable local image in its output directory"
                    )
            delay = min(delay, self._remaining(deadline, cancel_event))
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                raise InterruptedError("Image operation cancelled")
