"""Read-only runtime evidence shared by the diagnostic tools (standard library only).

A matching historical line number is not a version identifier. The plugin emits
an import-time source hash and a fresh instance ID after initialization instead.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path

RUNTIME_RE = re.compile(
    # Only the logger's own runtime message, not a marker quoted in chat text.
    r"^(?:\[[^\]\r\n]*\]\s*)*\[workbuddy_bridge\.main:\d+\]:\s*"
    r"(?:workbuddy_bridge:\s*)?\[runtime\]\s+sha256=(?P<sha256>[a-fA-F0-9]{64}|unknown)"
    r"\s+instance=(?P<instance>[a-zA-Z0-9_-]+)"
    r"\s+started_at=(?P<started_at>[0-9.]+)",
    re.MULTILINE,
)


MEMORY_TIMES = (
    "last_attempt_at",
    "last_success_at",
    "manual_requested_at",
    "manual_finished_at",
)
MEMORY_COUNTERS = (
    "attempts",
    "saved_total",
    "last_selected",
    "last_saved",
    "last_duration_ms",
    "manual_batches",
    "manual_saved",
)
MEMORY_OUTCOMES = {
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
}
MEMORY_MANUAL_STATES = {
    "idle",
    "waiting",
    "running",
    "completed",
    "stopped",
    "blocked",
    "failed",
}
MEMORY_REASONS = {
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
}


@dataclass(frozen=True)
class Evidence:
    state: bool | None
    note: str
    snapshot: dict | None = None


def _validate_memory_status(memory: object) -> None:
    """Reject a memory block that cannot be trusted as runtime evidence.

    Args:
        memory: Optional ``memory`` section of the heartbeat snapshot.

    Raises:
        ValueError: The block carries unknown fields or impossible values.
    """
    allowed = (
        set(MEMORY_TIMES)
        | set(MEMORY_COUNTERS)
        | {
            "last_outcome",
            "last_reasons",
            "manual_state",
        }
    )
    if not isinstance(memory, dict) or not set(memory) <= allowed:
        raise ValueError("Invalid memory status")
    for field in MEMORY_TIMES:
        if field not in memory:
            continue
        value = memory[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 253402300799
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid memory timestamp")
    for field in MEMORY_COUNTERS:
        if field in memory and (
            type(memory[field]) is not int or not 0 <= memory[field] <= 2**53 - 1
        ):
            raise ValueError("Invalid memory counter")
    if "last_outcome" in memory and (
        not isinstance(memory["last_outcome"], str)
        or memory["last_outcome"] not in MEMORY_OUTCOMES
    ):
        raise ValueError("Invalid memory outcome")
    if "manual_state" in memory and (
        not isinstance(memory["manual_state"], str)
        or memory["manual_state"] not in MEMORY_MANUAL_STATES
    ):
        raise ValueError("Invalid manual memory state")
    if "last_reasons" in memory:
        reasons = memory["last_reasons"]
        if not isinstance(reasons, dict) or not set(reasons) <= MEMORY_REASONS:
            raise ValueError("Invalid memory reasons")
        for value in reasons.values():
            if type(value) is not int or not 0 <= value <= 2**53 - 1:
                raise ValueError("Invalid memory reason count")


def read_runtime_status(path: Path) -> dict:
    """Read the small, credential-free heartbeat schema without side effects.

    Args:
        path: Plugin-owned bridge_status.json snapshot.

    Returns:
        The validated snapshot. Reading it does not establish freshness.

    Raises:
        OSError: The snapshot cannot be read.
        ValueError: Its structure or values cannot be trusted as runtime evidence.
    """
    raw = Path(path).read_bytes()
    if len(raw) > 16_384:
        raise ValueError("Runtime snapshot is too large")
    data = json.loads(raw.decode("utf-8"))
    expected = {
        "version",
        "instance",
        "pid",
        "source_sha256",
        "started_at",
        "updated_at",
        "state",
        "question",
        "gateway",
        "proactive",
    }
    if (
        not isinstance(data, dict)
        or set(data) - (expected | {"memory"})
        or not expected <= set(data)
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        raise ValueError("Invalid runtime snapshot fields")
    if not isinstance(data["instance"], str) or not re.fullmatch(
        r"[a-zA-Z0-9_-]{1,128}", data["instance"]
    ):
        raise ValueError("Invalid instance")
    if type(data["pid"]) is not int or data["pid"] <= 0:
        raise ValueError("Invalid process identifier")
    if not isinstance(data["source_sha256"], str) or not re.fullmatch(
        r"(?:[a-fA-F0-9]{64}|unknown)", data["source_sha256"]
    ):
        raise ValueError("Invalid source fingerprint")
    if data["state"] not in ("running", "stopped"):
        raise ValueError("Invalid runtime state")
    question, gateway, proactive = data["question"], data["gateway"], data["proactive"]
    if not isinstance(question, dict) or set(question) != {
        "active",
        "waiting",
        "limit",
        "remote_state",
        "last_outcome",
        "last_failure",
        "last_failure_at",
    }:
        raise ValueError("Invalid question status")
    if not isinstance(gateway, dict) or set(gateway) != {"state", "base", "checked_at"}:
        raise ValueError("Invalid gateway status")
    if not isinstance(proactive, dict) or set(proactive) != {"paused", "failures"}:
        raise ValueError("Invalid proactive status")
    if "memory" in data:
        _validate_memory_status(data["memory"])
    if type(question["active"]) is not bool or type(proactive["paused"]) is not bool:
        raise ValueError("Invalid activity flag")
    for value in (question["waiting"], question["limit"], proactive["failures"]):
        if type(value) is not int or value < 0:
            raise ValueError("Invalid status count")
    if question["remote_state"] not in {
        "none",
        "recorded",
        "submission_unknown",
        "gateway_changed",
        "unreadable",
    }:
        raise ValueError("Invalid remote ownership status")
    for value in (question["last_outcome"], question["last_failure"]):
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:|reply|cancelled|\[[^\]\r\n]{1,32}\])", value
        ):
            raise ValueError("Invalid question outcome")
    if gateway["state"] not in {
        "unknown",
        "connected",
        "not_configured",
        "unreachable",
    }:
        raise ValueError("Invalid gateway state")
    if gateway["base"] is not None:
        match = (
            re.fullmatch(
                r"http://(?:127\.0\.0\.1|localhost):([0-9]{1,5})", gateway["base"]
            )
            if isinstance(gateway["base"], str)
            else None
        )
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("Invalid gateway address")
    for value in (
        data["started_at"],
        data["updated_at"],
        question["last_failure_at"],
        gateway["checked_at"],
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError("Invalid runtime timestamp")
    if data["started_at"] <= 0 or data["updated_at"] < data["started_at"]:
        raise ValueError("Invalid heartbeat chronology")
    if (
        question["last_failure_at"] > data["updated_at"]
        or gateway["checked_at"] > data["updated_at"]
    ):
        raise ValueError("Status event is newer than its heartbeat")
    return data


def _inventory_path() -> Path:
    """Locate source_files.py in a deployment tree or a repository checkout."""
    root = Path(__file__).resolve().parents[1]
    for candidate in (
        root / "AstrBot/data/plugins/workbuddy_bridge/source_files.py",
        root / "workbuddy_bridge/source_files.py",
    ):
        if candidate.is_file():
            return candidate
    return root / "AstrBot/data/plugins/workbuddy_bridge/source_files.py"


SOURCE_INVENTORY = _inventory_path()


def _source_inventory():
    """Load only the pure inventory; import failures remain diagnosable.

    Returns:
        The module defining owned source paths and their fingerprint.

    Raises:
        OSError: The inventory file is absent or unreadable.
        SyntaxError: The inventory itself contains invalid Python.
    """
    spec = importlib.util.spec_from_file_location(
        "bridge_source_inventory", SOURCE_INVENTORY
    )
    module = importlib.util.module_from_spec(spec)
    # Compile directly to avoid writing diagnostic __pycache__ files.
    exec(
        compile(SOURCE_INVENTORY.read_bytes(), str(SOURCE_INVENTORY), "exec"),
        module.__dict__,
    )
    return module


def source_paths(source_path: Path) -> tuple[Path, ...]:
    """Return the complete inventory for syntax checks, including absent files."""
    return _source_inventory().source_paths(source_path)


def source_fingerprint(source_path: Path) -> str:
    """Hash installed plugins strictly, retaining partial standalone fixtures.

    Args:
        source_path: Maintained entry point or isolated legacy test fixture.

    Returns:
        Fingerprint with stable filename and NUL boundaries.

    Raises:
        OSError: Installed inventory or declared source files cannot be read.
        SyntaxError: The inventory module is invalid.
    """
    source_path = Path(source_path)
    inventory = _source_inventory()
    partial = (
        source_path.resolve().parent != SOURCE_INVENTORY.resolve().parent
        and not source_path.with_name("source_files.py").exists()
    )
    return inventory.source_fingerprint(source_path, missing_ok=partial)


def read_log_tail(path: Path, max_bytes: int = 400_000) -> str:
    """Read bytes first, so a tail seek cannot split a text decoder's state."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell() - max_bytes))
        return stream.read().decode("utf-8", "replace")


def runtime_instances(path: Path) -> set[str]:
    try:
        return {match["instance"] for match in RUNTIME_RE.finditer(read_log_tail(path))}
    except OSError:
        return set()


def inspect_runtime(
    source_path: Path,
    log_path: Path,
    *,
    after_time: float | None = None,
    excluded_instances: set[str] | None = None,
    max_age_sec: float = 300,
    now: float | None = None,
    require_heartbeat: bool = False,
    version_only: bool = False,
) -> Evidence:
    """Inspect heartbeat evidence or explicitly labelled legacy version evidence.

    Args:
        source_path: Maintained plugin entry point.
        log_path: Legacy initialization log, used only without a snapshot.
        after_time: Earliest acceptable initialization for a reload.
        excluded_instances: Instances that existed before a reload.
        max_age_sec: Legacy evidence age limit; heartbeats are capped at 90 seconds.
        now: Optional deterministic test clock.
        require_heartbeat: Require a fresh running heartbeat for liveness only,
            without comparing the source fingerprint or using startup logs.
        version_only: Report a recorded version without asserting current liveness;
            this takes precedence if both flags are set. With neither flag set,
            runtime/reload evidence requires both freshness and a matching version.

    Returns:
        Tri-state evidence. Neither a heartbeat nor a fingerprint proves QQ delivery.
    """
    now = time.time() if now is None else now
    expected = None
    if not require_heartbeat or version_only:
        try:
            expected = source_fingerprint(source_path)
        except (OSError, SyntaxError) as exc:
            return Evidence(None, f"源码指纹不可读（{type(exc).__name__}）")
    status_path = Path(source_path).with_name("bridge_status.json")
    try:
        snapshot = read_runtime_status(status_path)
    except FileNotFoundError:
        snapshot = None
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return Evidence(
            None, f"运行快照不可用（{type(exc).__name__}），未使用旧日志兜底"
        )
    if snapshot is not None:
        started, updated = snapshot["started_at"], snapshot["updated_at"]
        if started > now + 5 or updated > now + 5:
            return Evidence(None, "运行快照时间异常")
        if after_time is not None and started < after_time:
            return Evidence(None, "只有重载请求之前的运行快照")
        if snapshot["instance"] in (excluded_instances or set()):
            return Evidence(None, "尚未观察到重载后的新插件实例")
        if not version_only:
            if snapshot["state"] == "stopped":
                return Evidence(False, "最近插件实例已明确停止")
            if now - updated > min(max_age_sec, 90):
                return Evidence(None, "运行心跳已过期，不能确认插件当前存活")
            if require_heartbeat:
                return Evidence(
                    True, "插件运行心跳新鲜；版本另行核对，未验证 QQ 收发", snapshot
                )
        if snapshot["source_sha256"] == "unknown":
            return Evidence(None, "插件未能计算加载版本指纹")
        if snapshot["source_sha256"].lower() != expected:
            return Evidence(False, "快照中的已加载版本与当前磁盘源码不一致")
        if version_only:
            return Evidence(True, "快照记录的加载版本与磁盘一致；当前存活另看心跳")
        return Evidence(True, "插件运行心跳新鲜且版本一致；未验证 QQ 收发", snapshot)
    if require_heartbeat and not version_only:
        return Evidence(None, "没有运行心跳；启动日志不能证明插件当前存活")
    try:
        tail = read_log_tail(log_path)
        log_age = now - log_path.stat().st_mtime
    except OSError as exc:
        return Evidence(None, f"运行指纹证据不可读（{type(exc).__name__}）")

    matches = list(RUNTIME_RE.finditer(tail))
    if not matches:
        return Evidence(None, "没有运行指纹；旧行号和 Loading 记录不能确认版本")
    latest = matches[-1]
    # A failed/new load after the last completed initialization invalidates it.
    later = tail[latest.end() :]
    if "Failed to import plugin workbuddy_bridge" in later:
        return Evidence(False, "最近一次运行指纹之后发生插件导入失败")
    if "Loading plugin workbuddy_bridge" in later or re.search(
        r"Removed handler[^\n]*from plugin workbuddy_bridge", later
    ):
        return Evidence(None, "运行指纹之后有加载或卸载记录，尚无新的初始化证据")
    try:
        started = float(latest["started_at"])
    except ValueError:
        return Evidence(None, "运行指纹时间无效")
    if not math.isfinite(started) or started > now + 5 or log_age < -5:
        return Evidence(None, "运行指纹或日志时间异常")
    if after_time is not None and started < after_time:
        return Evidence(None, "只有重载请求之前的运行指纹")
    if latest["instance"] in (excluded_instances or set()):
        return Evidence(None, "尚未观察到重载后的新插件实例")
    if not version_only and (now - started > max_age_sec or log_age > max_age_sec):
        return Evidence(None, "运行指纹或日志过旧，无法据此断言当前运行状态")
    if latest["sha256"] == "unknown":
        return Evidence(None, "插件未能计算加载版本指纹")
    if latest["sha256"].lower() != expected:
        return Evidence(False, "已加载的源文件指纹与当前磁盘文件不一致")
    if version_only:
        return Evidence(True, "历史初始化版本与磁盘一致；不能据此确认当前存活")
    return Evidence(True, "近期初始化版本与磁盘一致；此兼容证据不代替运行心跳")
