"""Submit local memory controls without contacting a model or modifying its database."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

from diagnostic_evidence import read_runtime_status, source_fingerprint

ROOT = Path(__file__).resolve().parents[1]
MANUAL_LABELS = {
    "idle": "未开启",
    "waiting": "等待空闲",
    "running": "整理中",
    "completed": "本轮已完成",
    "stopped": "已停止",
    "blocked": "暂时受阻",
    "failed": "本轮未完成",
}
OUTCOME_LABELS = {
    "waiting": "尚未开始",
    "running": "正在处理当前批次",
    "saved": "已保存",
    "no_candidates": "本批没有需要长期保存的内容",
    "filtered": "本批候选未通过规则筛选",
    "unchanged": "本批没有新增变化",
    "stale": "本批来源已变化，未采用旧结果",
    "invalid_json": "返回格式不符合要求",
    "invalid_schema": "返回字段不符合要求",
    "proof_mismatch": "返回批次不匹配",
    "invalid_source": "返回来源未通过校验",
    "gateway_unreachable": "连接暂时不可用",
    "gateway_error": "连接返回错误",
    "busy": "已有任务占用",
    "pending_confirmation": "上一轮任务状态待确认",
    "not_idle": "还有其他任务，尚未空闲",
    "idle_unverified": "暂时不能确认是否完全空闲",
    "expired": "本次等待已到期",
    "silent": "没有返回整理结果",
    "unavailable": "整理服务暂时不可用",
    "cancelled": "本次等待已取消",
    "paused": "维护暂停中",
    "disabled": "记忆功能已关闭",
    "exception": "整理遇到错误",
    "permission_denied": "未获执行许可",
}


def main(argv: list[str] | None = None) -> int:
    """Validate runtime evidence and publish one complete control request.

    Args:
        argv: Optional command-line arguments, used by offline tests too.

    Returns:
        Zero after a read or accepted local submission; one when no action was taken.
    """
    parser = argparse.ArgumentParser(description="手动开启、停止或查看记忆整理。")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--start", action="store_true", help="提交本轮整理请求")
    actions.add_argument("--stop", action="store_true", help="当前批次结束后停止")
    actions.add_argument(
        "--status", action="store_true", help="只读查看整理状态（默认）"
    )
    arguments = parser.parse_args(argv)
    plugin = ROOT / "AstrBot/data/plugins/workbuddy_bridge"
    try:
        status = read_runtime_status(plugin / "bridge_status.json")
        now = time.time()
        if status["state"] != "running" or not -5 <= now - status["updated_at"] <= 90:
            print("插件心跳已停止或过期，暂不能确认当前状态；未提交请求。")
            return 1
        if status["source_sha256"] != source_fingerprint(plugin / "main.py"):
            print("插件加载版本与当前文件不一致，请等更新完成后再操作；未提交请求。")
            return 1
        memory = status.get("memory", {})
        if memory.get("manual_state") not in MANUAL_LABELS:
            print("当前插件还没有提供手动整理状态；未提交请求。")
            return 1
        if not arguments.start and not arguments.stop:
            print("记忆整理：" + MANUAL_LABELS[memory["manual_state"]])
            if "last_outcome" in memory:
                print("最近结果：" + OUTCOME_LABELS[memory["last_outcome"]])
            print(
                "本轮已处理批次："
                + str(memory.get("manual_batches", "未知"))
                + "；已保存条数："
                + str(memory.get("manual_saved", "尚未确认"))
            )
            print("状态来自最近一次心跳，可能略有延迟。")
            return 0
        if arguments.start and (ROOT / "bridge.paused").exists():
            print("机器人正在维护暂停，暂不提交整理请求。")
            return 1
        request = {
            "version": 1,
            "action": "start" if arguments.start else "stop",
            "request_id": uuid.uuid4().hex,
            "requested_at": now,
        }
        target = plugin / "memory_control.json"
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=plugin,
                prefix=".memory-control-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(request, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            # A hard link publishes complete bytes atomically and cannot replace a request.
            os.link(temporary, target)
        except FileExistsError:
            print("已有待处理的整理请求，本次没有覆盖；请稍后查看状态。")
            return 1
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        if arguments.start:
            print("已提交整理请求，等待插件确认完全空闲后开始；请求十分钟内有效。")
        else:
            print("已提交停止请求；已开始的当前批次结束后停止，请稍后查看状态。")
        return 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        print("暂时不能读取状态或提交请求（" + type(exc).__name__ + "）。")
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
