"""Publish content-free runtime evidence without depending on the bot framework."""

import json
import math
import os
import re
import tempfile
import time
from collections.abc import Callable, Collection
from pathlib import Path


def write_snapshot(
    target: Path,
    *,
    state: str,
    instance: str,
    source_sha256: str,
    started_at: float,
    question_active: bool,
    question_waiting: int,
    question_limit: int,
    read_remote: Callable[[], dict | None],
    last_outcome: str,
    last_failure: str,
    last_failure_at: float,
    proactive_failures: int,
    proactive_failure_limit: int,
    gateway_base: str | None,
    gateway_state: str,
    gateway_checked_at: float,
    empty_reply_markers: Collection[str],
    memory_status: dict | None = None,
) -> None:
    """Filter explicit operational fields and atomically replace the heartbeat.

    Args:
        target: Local heartbeat destination supplied by the plugin.
        state: running after initialization or stopped after task cleanup.
        instance: Opaque identifier of the current plugin instance.
        source_sha256: Fingerprint of the imported plugin sources.
        started_at: Instance start time.
        question_active: Whether the plugin owns an active question operation.
        question_waiting: Number of local waiters.
        question_limit: Maximum number of local waiters.
        read_remote: Read-only callback for the saved remote task record.
        last_outcome: Latest fixed outcome category, never reply content.
        last_failure: Latest fixed failure category.
        last_failure_at: Time of the latest failure.
        proactive_failures: Consecutive background gateway failures.
        proactive_failure_limit: Threshold that pauses proactive replies.
        gateway_base: Candidate loopback gateway address, validated here.
        gateway_state: Candidate connection category, validated here.
        gateway_checked_at: Time of the latest connection check.
        empty_reply_markers: Allowed fixed result categories from the plugin.
        memory_status: Optional memory counters, filtered to fixed public fields.

    Raises:
        OSError: The snapshot cannot be replaced; the prior copy is preserved.
        ValueError: The lifecycle state or a JSON numeric value is unsupported.
    """
    if state not in {"running", "stopped"}:
        raise ValueError("Invalid runtime state")
    base = gateway_base
    if not isinstance(base, str) or not re.fullmatch(
        r"http://127\.0\.0\.1:[0-9]{1,5}", base
    ):
        base = None
    remote_state = "none"
    try:
        pending = read_remote()
        if pending:
            if not pending["run_id"]:
                remote_state = "submission_unknown"
            elif base is not None and pending["base"] != base:
                remote_state = "gateway_changed"
            else:
                remote_state = "recorded"
    except (OSError, ValueError):
        remote_state = "unreadable"
    outcomes = set(empty_reply_markers) | {"", "reply", "cancelled"}
    if gateway_state not in {
        "unknown",
        "connected",
        "not_configured",
        "unreachable",
    }:
        gateway_state = "unknown"
    snapshot = {
        "version": 1,
        "instance": instance,
        "pid": os.getpid(),
        "source_sha256": source_sha256,
        "started_at": started_at,
        "updated_at": time.time(),
        "state": state,
        "question": {
            "active": question_active,
            "waiting": question_waiting,
            "limit": question_limit,
            "remote_state": remote_state,
            "last_outcome": last_outcome if last_outcome in outcomes else "",
            "last_failure": last_failure if last_failure in outcomes else "",
            "last_failure_at": last_failure_at,
        },
        "proactive": {
            "paused": proactive_failures >= proactive_failure_limit,
            "failures": proactive_failures,
        },
        "gateway": {
            "state": gateway_state,
            "checked_at": gateway_checked_at,
            "base": base,
        },
    }
    if isinstance(memory_status, dict):
        memory = {}
        for field in (
            "last_attempt_at",
            "last_success_at",
            "manual_requested_at",
            "manual_finished_at",
        ):
            value = memory_status.get(field)
            if (
                type(value) in (int, float)
                and 0 <= value <= 253402300799
                and math.isfinite(value)
            ):
                memory[field] = value
        for field in (
            "attempts",
            "saved_total",
            "last_selected",
            "last_saved",
            "last_duration_ms",
            "manual_batches",
            "manual_saved",
        ):
            value = memory_status.get(field)
            if type(value) is int and 0 <= value <= 2**53 - 1:
                memory[field] = value
        outcome = memory_status.get("last_outcome")
        if isinstance(outcome, str) and outcome in {
            "waiting",
            "running",
            "saved",
            "no_candidates",
            "filtered",
            "unchanged",
            "stale",
            "invalid_json",
            "invalid_schema",
            "proof_mismatch",
            "invalid_source",
            "gateway_unreachable",
            "gateway_error",
            "busy",
            "pending_confirmation",
            "not_idle",
            "idle_unverified",
            "expired",
            "silent",
            "unavailable",
            "cancelled",
            "paused",
            "disabled",
            "exception",
            "permission_denied",
        }:
            memory["last_outcome"] = outcome
        manual_state = memory_status.get("manual_state")
        if isinstance(manual_state, str) and manual_state in {
            "idle",
            "waiting",
            "running",
            "completed",
            "stopped",
            "blocked",
            "failed",
        }:
            memory["manual_state"] = manual_state
        reasons = memory_status.get("last_reasons")
        if isinstance(reasons, dict):
            memory["last_reasons"] = {
                key: value
                for key in (
                    "question",
                    "hypothetical",
                    "reported",
                    "other_subject",
                    "instruction",
                    "context_missing",
                    "group_policy",
                    "duplicate",
                    "explicit_protected",
                    "forgotten_key",
                    "capacity",
                    "stale_cursor",
                    "stale_epoch",
                    "stale_source",
                )
                if type(value := reasons.get(key)) is int and 0 <= value <= 2**53 - 1
            }
        snapshot["memory"] = memory
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".bridge-status-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
