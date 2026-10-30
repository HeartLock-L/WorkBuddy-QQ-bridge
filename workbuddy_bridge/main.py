# ==========================================================================
# 本文件为**可分发副本**：所有真实 QQ 号 / 群号 / 昵称已替换为占位值。
# 请按 docs/部署指南.md 第 3.5 节填写管理员与机器人身份后使用。
# ==========================================================================
"""Bridge QQ messages to the local WorkBuddy gateway.

All model traffic goes through the runs channel (POST /api/v1/runs + SSE),
i.e. a real agent task with its tools available — that is what lets the model
open a saved attachment or search the web when it needs to. Every request
carries a receipt code so a reply produced for another turn (e.g. the desktop
window's own conversation) can never be mistaken for this one and posted to QQ.
Tool controls in the prompt are boundaries, not an OS sandbox.
"""

# ---- 标准库 ----
# asyncio   : 事件循环与后台协程（热重载 watcher / 冷场巡检 / 关键词冒泡全靠它）
# hashlib   : md5 生成稳定会话 ID——内置 hash() 带每进程随机盐，重启即失忆（见 _conv_id）
# shutil    : 附件转存 copy2 + 热重载时清 __pycache__
# subprocess: 调 netstat 扫本机监听端口（定位网关，见 _local_ports）
# sys       : 热重载时手动删 sys.modules 缓存（见 _purge_own_modules）
import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import (
    attachments,
    image_job_commands,
    image_jobs,
    jimeng,
    meme_collection,
    meme_library,
    memory_service,
    request_queue,
    runtime_status,
    source_files,
)
from .attachments import describe as _describe_saved_attachments
from .bridge_policy import (
    _BEI_GROUPS as _BEI_GROUPS,
    _BEI_RULE as _BEI_RULE,
    _DRAW_PREFIX_HAS_QUANT as _DRAW_PREFIX_HAS_QUANT,
    _DRAW_QUANT_TAIL as _DRAW_QUANT_TAIL,
    _DRAW_SELFIE_RE as _DRAW_SELFIE_RE,
    _DRAW_SESSION_ROW as _DRAW_SESSION_ROW,
    _DUCK_RULE as _DUCK_RULE,
    _GATEWAY_ERROR_RE as _GATEWAY_ERROR_RE,
    _IMG_RATIOS as _IMG_RATIOS,
    _RUNTIME as _RUNTIME,
    _SEARCH_ASK as _SEARCH_ASK,
    _SEARCH_EXPLICIT as _SEARCH_EXPLICIT,
    _SEARCH_FACT as _SEARCH_FACT,
    _SEARCH_TIME as _SEARCH_TIME,
    _SEARCH_YEAR as _SEARCH_YEAR,
    ACTION_WORDS as ACTION_WORDS,
    ALLOWED_SENDERS as ALLOWED_SENDERS,
    APPROVE_WORDS as APPROVE_WORDS,
    ATTACHMENT_CLEAN_INTERVAL_SEC as ATTACHMENT_CLEAN_INTERVAL_SEC,
    ATTACHMENT_KEEP_DAYS as ATTACHMENT_KEEP_DAYS,
    ATTACHMENT_ROOT as ATTACHMENT_ROOT,
    ATTACHMENT_SILENT_REPLY as ATTACHMENT_SILENT_REPLY,
    BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS as BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS,
    CHAT_ONLY_WORDS as CHAT_ONLY_WORDS,
    CONFIG_TOPIC_WORDS as CONFIG_TOPIC_WORDS,
    DM_MASTER_ONLY as DM_MASTER_ONLY,
    DRAW_BLOCKED_WORDS as DRAW_BLOCKED_WORDS,
    DRAW_PROMPT_MAX_LEN as DRAW_PROMPT_MAX_LEN,
    DRAW_SELFIE_PROMPT as DRAW_SELFIE_PROMPT,
    DRAW_SELFIE_VARIANTS as DRAW_SELFIE_VARIANTS,
    DRAW_SESSION_NAME_MAX as DRAW_SESSION_NAME_MAX,
    DRAW_SESSION_STATE_FILE as DRAW_SESSION_STATE_FILE,
    EMPTY_AT_REPLIES as EMPTY_AT_REPLIES,
    EMPTY_REPLY_MARKERS as EMPTY_REPLY_MARKERS,
    EXPECTED_SELF_ID as EXPECTED_SELF_ID,
    EXTRA_WAKE_PREFIXES as EXTRA_WAKE_PREFIXES,
    FILTER_INTERNAL as FILTER_INTERNAL,
    FOLLOW_CHAIN_COOLDOWN_SEC as FOLLOW_CHAIN_COOLDOWN_SEC,
    FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC as FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC,
    FOLLOW_HIFREQ_MAX_CHAIN as FOLLOW_HIFREQ_MAX_CHAIN,
    FOLLOW_HIFREQ_WINDOW_SEC as FOLLOW_HIFREQ_WINDOW_SEC,
    FOLLOW_MAX_CHAIN as FOLLOW_MAX_CHAIN,
    FOLLOW_MIN_GAP_SEC as FOLLOW_MIN_GAP_SEC,
    GROUP_ATTACHMENT_REQUIRE_AT as GROUP_ATTACHMENT_REQUIRE_AT,
    GROUP_GAG_RULES as GROUP_GAG_RULES,
    GROUP_REQUIRE_EXPLICIT_WAKE as GROUP_REQUIRE_EXPLICIT_WAKE,
    HARD_BLOCK_WORDS as HARD_BLOCK_WORDS,
    HOT_RELOAD_DEBOUNCE as HOT_RELOAD_DEBOUNCE,
    HOT_RELOAD_ENABLED as HOT_RELOAD_ENABLED,
    HOT_RELOAD_INTERVAL as HOT_RELOAD_INTERVAL,
    IDLE_ALLOW_IMAGE as IDLE_ALLOW_IMAGE,
    IDLE_CHAT_ALL_GROUPS as IDLE_CHAT_ALL_GROUPS,
    IDLE_CHAT_BLACKLIST as IDLE_CHAT_BLACKLIST,
    IDLE_CHAT_ENABLED as IDLE_CHAT_ENABLED,
    IDLE_CHAT_GROUPS as IDLE_CHAT_GROUPS,
    IDLE_CHECK_INTERVAL as IDLE_CHECK_INTERVAL,
    IDLE_COOLDOWN_SEC as IDLE_COOLDOWN_SEC,
    IDLE_GLOBAL_GAP_SEC as IDLE_GLOBAL_GAP_SEC,
    IDLE_HIFREQ_COOLDOWN_SEC as IDLE_HIFREQ_COOLDOWN_SEC,
    IDLE_HIFREQ_GROUPS as IDLE_HIFREQ_GROUPS,
    IDLE_HIFREQ_TRIGGER_SEC as IDLE_HIFREQ_TRIGGER_SEC,
    IDLE_HISTORY_SIZE as IDLE_HISTORY_SIZE,
    IDLE_LIVE_HISTORY as IDLE_LIVE_HISTORY,
    IDLE_MIN_SOURCE_LINES as IDLE_MIN_SOURCE_LINES,
    IDLE_MIN_TRIGGER_SEC as IDLE_MIN_TRIGGER_SEC,
    IDLE_REQUIRE_TEXT as IDLE_REQUIRE_TEXT,
    IDLE_SOURCE_TTL as IDLE_SOURCE_TTL,
    IDLE_TRIGGER_SEC as IDLE_TRIGGER_SEC,
    IMAGE_CONTEXT_ENABLED as IMAGE_CONTEXT_ENABLED,
    IMAGE_CONTEXT_TTL as IMAGE_CONTEXT_TTL,
    IMAGE_FOLLOWUP_TTL as IMAGE_FOLLOWUP_TTL,
    IMAGE_GEN_ACK as IMAGE_GEN_ACK,
    IMAGE_GEN_ADMIN_ONLY as IMAGE_GEN_ADMIN_ONLY,
    IMAGE_GEN_BIN as IMAGE_GEN_BIN,
    IMAGE_GEN_COOLDOWN_SEC as IMAGE_GEN_COOLDOWN_SEC,
    IMAGE_GEN_DIR as IMAGE_GEN_DIR,
    IMAGE_GEN_ENABLED as IMAGE_GEN_ENABLED,
    IMAGE_GEN_GLOBAL_GAP_SEC as IMAGE_GEN_GLOBAL_GAP_SEC,
    IMAGE_GEN_MAX_PER_DAY as IMAGE_GEN_MAX_PER_DAY,
    IMAGE_GEN_MODEL as IMAGE_GEN_MODEL,
    IMAGE_GEN_PREFIXES as IMAGE_GEN_PREFIXES,
    IMAGE_GEN_RATIO as IMAGE_GEN_RATIO,
    IMAGE_GEN_RESOLUTION as IMAGE_GEN_RESOLUTION,
    IMAGE_GEN_TIMEOUT as IMAGE_GEN_TIMEOUT,
    IMAGE_HINT_WORDS as IMAGE_HINT_WORDS,
    KEYWORD_BUBBLE_COOLDOWN_SEC as KEYWORD_BUBBLE_COOLDOWN_SEC,
    KEYWORD_BUBBLE_ENABLED as KEYWORD_BUBBLE_ENABLED,
    KEYWORD_BUBBLE_GLOBAL_GAP_SEC as KEYWORD_BUBBLE_GLOBAL_GAP_SEC,
    KEYWORD_BUBBLE_GROUPS as KEYWORD_BUBBLE_GROUPS,
    KEYWORD_BUBBLE_MAX_PER_DAY as KEYWORD_BUBBLE_MAX_PER_DAY,
    KEYWORD_BUBBLE_MIN_SOURCE_LINES as KEYWORD_BUBBLE_MIN_SOURCE_LINES,
    KEYWORD_BUBBLE_WORDS as KEYWORD_BUBBLE_WORDS,
    MACHINE_OBJECTS as MACHINE_OBJECTS,
    MASTER_EXEMPT_WAKE as MASTER_EXEMPT_WAKE,
    MASTER_ID as MASTER_ID,
    MAX_ATTACHMENTS_PER_MSG as MAX_ATTACHMENTS_PER_MSG,
    MAX_REPLY_CHARS as MAX_REPLY_CHARS,
    META_NOTE_MARKERS as META_NOTE_MARKERS,
    META_NOTE_PREFIXES as META_NOTE_PREFIXES,
    NAME_WAKE_ASK_HINTS as NAME_WAKE_ASK_HINTS,
    NAME_WAKE_ENABLED as NAME_WAKE_ENABLED,
    NAME_WAKE_REPLIES as NAME_WAKE_REPLIES,
    NAME_WAKE_WORDS as NAME_WAKE_WORDS,
    NEGATION_PREFIXES as NEGATION_PREFIXES,
    NOTICE_ON_REDIRECT as NOTICE_ON_REDIRECT,
    OTHERS_MAX_CHARS as OTHERS_MAX_CHARS,
    OUTPUT_ROOT as OUTPUT_ROOT,
    PAUSE_FLAG as PAUSE_FLAG,
    PAUSE_MAX_SEC as PAUSE_MAX_SEC,
    PENDING_TTL_SEC as PENDING_TTL_SEC,
    POKE_COOLDOWN_SEC as POKE_COOLDOWN_SEC,
    POKE_ENABLED as POKE_ENABLED,
    POKE_REPLIES as POKE_REPLIES,
    READ_ATTACHMENTS as READ_ATTACHMENTS,
    RECENT_IMAGE_LIMIT as RECENT_IMAGE_LIMIT,
    REDIRECT_NOTICE as REDIRECT_NOTICE,
    REDIRECT_ON_CONFIG_TOPIC as REDIRECT_ON_CONFIG_TOPIC,
    REFUSE_TEXT as REFUSE_TEXT,
    REJECT_WORDS as REJECT_WORDS,
    RELAY_BACK_OUTPUT as RELAY_BACK_OUTPUT,
    RELAY_OUTPUT_MAX_FILES as RELAY_OUTPUT_MAX_FILES,
    RELAY_OUTPUT_MAX_MB as RELAY_OUTPUT_MAX_MB,
    RELAY_WORDS as RELAY_WORDS,
    RELOAD_WORDS as RELOAD_WORDS,
    ROLEPLAY_ALLOWED as ROLEPLAY_ALLOWED,
    ROLEPLAY_ENABLED as ROLEPLAY_ENABLED,
    ROLEPLAY_OPEN_TO_ALL as ROLEPLAY_OPEN_TO_ALL,
    ROLEPLAY_START_WORDS as ROLEPLAY_START_WORDS,
    ROLEPLAY_STOP_WORDS as ROLEPLAY_STOP_WORDS,
    ROLEPLAY_TIMEOUT_SEC as ROLEPLAY_TIMEOUT_SEC,
    SILENT_FOR_OTHERS as SILENT_FOR_OTHERS,
    SILENT_NOTICE_THROTTLE_SEC as SILENT_NOTICE_THROTTLE_SEC,
    SPARK_FAIL_CIRCUIT_N as SPARK_FAIL_CIRCUIT_N,
    STRICT_FOR_OTHERS as STRICT_FOR_OTHERS,
    STRONG_ACTION_WORDS as STRONG_ACTION_WORDS,
    TEMP_CLEAN_MAX_FILES as TEMP_CLEAN_MAX_FILES,
    TEMP_KEEP_DAYS as TEMP_KEEP_DAYS,
    TEMP_ROOT as TEMP_ROOT,
    WEB_SEARCH_DESC_MAX as WEB_SEARCH_DESC_MAX,
    WEB_SEARCH_ENABLED as WEB_SEARCH_ENABLED,
    WEB_SEARCH_MAX_RESULTS as WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_QUERY_MAX as WEB_SEARCH_QUERY_MAX,
    WEB_SEARCH_TIMEOUT as WEB_SEARCH_TIMEOUT,
    WORK_DIR as WORK_DIR,
    _attachment_components as _attachment_components,
    _clean_old_attachments as _clean_old_attachments,
    _clean_old_temp as _clean_old_temp,
    _clean_search_query as _clean_search_query,
    _cleanup_root as _cleanup_root,
    _conv_id as _conv_id,
    _describe_attachments as _describe_attachments,
    _detect_work_dir as _detect_work_dir,
    _draw_block_reply as _draw_block_reply,
    _draw_prompt_of as _draw_prompt_of,
    _draw_ratio_of as _draw_ratio_of,
    _draw_selfie_prompt_of as _draw_selfie_prompt_of,
    _draw_session_label as _draw_session_label,
    _gag_rules_of as _gag_rules_of,
    _has_textual_material as _has_textual_material,
    _hit_approve as _hit_approve,
    _hit_blocklist as _hit_blocklist,
    _hit_group_gag as _hit_group_gag,
    _hit_name_wake as _hit_name_wake,
    _hit_words as _hit_words,
    _is_action_request as _is_action_request,
    _is_at_me as _is_at_me,
    _is_empty_reply as _is_empty_reply,
    _is_gateway_error as _is_gateway_error,
    _is_master as _is_master,
    _is_meta_note as _is_meta_note,
    _is_paused as _is_paused,
    _is_poked as _is_poked,
    _is_reply_to_me as _is_reply_to_me,
    _looks_like_question as _looks_like_question,
    _materialize_attachment as _materialize_attachment,
    _mb as _mb,
    _mentions_image as _mentions_image,
    _needs_search as _needs_search,
    _question_notice as _question_notice,
    _receipt_gate as _receipt_gate,
    _safe_attachment_dir as _safe_attachment_dir,
    _safe_dir_name as _safe_dir_name,
    _safe_name as _safe_name,
    _sanitize as _sanitize,
    _search_block as _search_block,
    _strict_filter as _strict_filter,
    _strip_marker as _strip_marker,
    _strip_receipt as _strip_receipt,
    _temp_cache_entries as _temp_cache_entries,
    _temp_usage as _temp_usage,
    _web_search as _web_search,
)
from .draw_flow import DrawFlow
from .proactive_chat import ProactiveChat
from .prompts import _prompt_prefix as _prompt_prefix
from .reply_text import (
    REPEAT_MAX_UNIT as REPEAT_MAX_UNIT,
    REPEAT_MIN_SPAN as REPEAT_MIN_SPAN,
    STRICT_STRIP_WORDS as STRICT_STRIP_WORDS,
    _find_repeat_unit as _find_repeat_unit,
    _kill_repeat_run as _kill_repeat_run,
    _smart_trim as _smart_trim,
    _strip_markdown as _strip_markdown,
)

# aiohttp 是调网关唯一的 HTTP 客户端。缺失时不让插件起不来，
# 而是所有 _ask 走 "[环境错误]" 兜底文案返回（该串会被 _is_empty_reply 拦截）。
try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

# AstrBot 框架接口：logger 日志 / star 插件基类
from astrbot.api import logger, star

# 事件与消息组件类型：event 是所有 handler 的入口对象；
# 组件用于判断消息里有没有 @/引用/戳一戳/附件（见 _is_at_me 等判定函数）
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import (
    At,
    File,
    Image,
    Plain,
    Poke as Poke,
    Record as Record,
    Reply,
    Video as Video,
)

# 主动发消息需要的核心类（普通回复走 yield，不需要它们）；
# AstrBot 版本差异可能导不进，所以用 try 包住：导不进就关掉所有主动发送路径
# （代发 / 转私聊 / 主动开口），插件的被动应答仍可用。
try:  # 用于把配置类话题主动发到管理员私聊
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType

    _CAN_SEND_ACTIVE = True
except Exception:  # noqa: BLE001
    MessageChain = None  # type: ignore
    MessageSession = None  # type: ignore
    MessageType = None  # type: ignore
    _CAN_SEND_ACTIVE = False


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
# 项目根目录。
# 默认按插件自身位置反推：<项目根>\AstrBot\data\plugins\workbuddy_bridge\main.py
# 上溯 4 层即为 <项目根>。这样整套材料拷到别的机器、别的盘符也能直接用，
# 不需要为了一个路径常量去改代码（2026-10-21 可移植化改造）。
# 要固定到别处时用环境变量 QQ_BOT_ROOT 覆盖；反推失败才退回历史默认值。


# Capture the imported bytes, not a later on-disk version, for reload evidence.
try:
    CODE_SHA256 = source_files.source_fingerprint(Path(__file__))
except OSError:
    CODE_SHA256 = "unknown"
# 由 WorkBuddy 桌面端注入的网关凭据（WorkBuddy 重启后可能变化）
# 内容形如 CODEBUDDY_GATEWAY_PORT=xxxxx / CODEBUDDY_GATEWAY_PASSWORD=xxx，
# 由 reconnect.py 之类的脚本在 WorkBuddy 启动后重新生成。
GATEWAY_ENV_FILE = WORK_DIR / "workbuddy-gateway.env"

# 维护模式开关（2026-10-20 加）：这个文件存在时，插件对 QQ 消息**完全静默**——
# 不转发、不回复、也不主动开口。用途：管理员要在 WorkBuddy 桌面端窗口下指令改代码时，
# 先双击 pause-bridge.bat 把中转摘掉。原因是 Gateway 复用桌面端的活动会话
# （README「跑自检时不要同时使用桌面端」）：窗口和 QQ 中转抢同一条会话，
# 管理员在窗口里说的话会被群里那一轮 run 顶掉，看起来就是"指令被无视了"。
# 恢复：双击 resume-bridge.bat（删掉这个文件即可，不用重启 AstrBot）。
# 维护模式自动失效（2026-10-20 R6）：flag 放太久说明是忘了恢复，
# 不能让它一直把机器人按死（群里只会看到"死了"）。超过这个时长按未暂停处理。


TIMEOUT_SEC = 300  # 单次回答总超时
RUNTIME_STATUS_INTERVAL_SEC = 30
STREAM_IDLE_SEC = 120  # SSE 连续无事件多久算结束
# 流式总预算：run 排队（桌面端正忙）时不超过这么久（须 < TIMEOUT_SEC 留余量）。
# 2026-10-15 实测：网关把所有消息串到桌面端当前会话上（primarySession），
# 管理员干活时 QQ 消息全在排队；此前插件只等 STREAM_IDLE_SEC 就放弃，
# 排到后面才执行的回复直接丢掉（没人再读流）。现在改为"仍在排队就继续等"。
STREAM_TOTAL_SEC = 240
RESET_KEYWORDS = ("/重置", "/resetwb", "/新会话")

# 从 event 里取文本时按优先级尝试的键。
# 实测 Gateway 的 SSE 事件格式为：
#   event: message
#   data: {"version":"1.0","replyTo":"...","status":"completed",
#          "content":{"markdown":"测试成功"},"sessionId":"...","toolCalls":[]}
#   event: done
#   data: {}
# 所以 "markdown" 是实际承载正文的键，必须保留。
TEXT_KEYS = (
    "markdown",
    "text",
    "content",
    "delta",
    "message",
    "answer",
    "result",
    "output",
)


def _read_env_file(path: Path) -> dict:
    """读取 .env 风格凭据文件，返回 {键: 值}。

    容错口径：文件不存在/读失败都只记日志、返回空 dict —— 凭据可能还没
    生成（WorkBuddy 未启动），插件不能因为缺一个文件就起不来。
    值两端多余的引号会被剥掉；# 开头与无等号的行当注释跳过。
    """
    data: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key:
                data[key] = value
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"workbuddy_bridge: 读取 {path} 失败: {exc}")
    return data


def _cfg() -> tuple[int | None, str]:
    """从本地凭据文件读取 (端口, 密码)。

    生命周期要点（血泪教训，勿回退）：
    - **端口随 WorkBuddy 会话变**：桌面端每次重启端口都不一样，所以这里
      只是"首选线索"，真正定位靠 _resolve_base 的本机扫描兜底；
    - **密码在同一会话生命周期内不变**：一旦拿到就照用到 WorkBuddy 重启；
      重启后旧密码直接 401，必须重新生成本文件。
    端口解析失败返回 None 而不是抛异常 —— 让 _resolve_base 走扫描兜底。
    """
    cfg = _read_env_file(GATEWAY_ENV_FILE)
    # 端口优先凭据文件，环境变量 SERVER__PORT 兜底（桌面端注入给插件进程时才有）
    raw_port = cfg.get("CODEBUDDY_GATEWAY_PORT") or os.environ.get("SERVER__PORT")
    try:
        port = int(str(raw_port).strip()) if raw_port else None
    except (TypeError, ValueError):
        port = None
    token = cfg.get("CODEBUDDY_GATEWAY_PASSWORD") or os.environ.get(
        "CODEBUDDY_GATEWAY_PASSWORD", ""
    )
    return port, token


def _local_ports() -> list[int]:
    """列出 127.0.0.1 上处于 LISTENING 的端口（阻塞，调用方用 to_thread 包）。

    原理：跑一条 netstat -ano，逐行正则挑出 127.0.0.1:端口 的行。
    netstat 本身可能耗时几秒，绝不能直接在事件循环里调。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",  # 控制台输出是 GBK，个别行可能带坏字符，容错解码
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    ports: set[int] = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        # 只关心回环地址：网关只监听 127.0.0.1，0.0.0.0 的行不在这里出现
        m = re.search(r"127\.0\.0\.1:(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


# 已探明的网关地址缓存，避免每条消息都重新扫描
_GW_CACHE: dict[str, str | float | None] = {
    "base": None,
    "miss_at": 0.0,
    "config_key": None,
    "state": "unknown",
    "checked_at": 0.0,
}
# 端口全量扫描的退避窗口：扫一次找不到，30 秒内不再全量扫（每次调用都会调
# _resolve_base，不退避的话一次故障能把 26 个端口串行超时拖成几分钟卡死）。
GW_RESCAN_COOLDOWN_SEC = 30


def _auth_ok(body: str) -> bool:
    """判据必须认 authenticated: true。

    旧密码请求 auth/status 也返回 HTTP 200 + {"authenticated":false}，
    只查字符串 "authenticated" 会被骗（2026-10-07 实测坑，勿回退）。
    """
    try:
        return json.loads(body).get("authenticated") is True
    except Exception:  # noqa: BLE001
        return False


async def _resolve_base(session, token: str) -> str | None:
    """Authenticate known endpoints before discovering a moved local gateway.

    Args:
        session: HTTP session with proxy inheritance disabled by the caller.
        token: Current credential, never logged or persisted by this function.

    Returns:
        An authenticated loopback origin, or None. Backoff applies only to scans;
        changing the preferred port or credential permits an immediate new scan.
    """
    if not token:
        _GW_CACHE.update(base=None, state="not_configured", checked_at=time.time())
        return None
    preferred, _ = _cfg()
    if type(preferred) is not int or not 1 <= preferred <= 65535:
        preferred = None
    config_key = hashlib.sha256(f"{preferred}:{token}".encode()).hexdigest()
    if _GW_CACHE.get("config_key") != config_key:
        _GW_CACHE["config_key"] = config_key
        _GW_CACHE["miss_at"] = 0.0
    candidates: list[int] = []
    if preferred is not None:
        candidates.append(preferred)
    cached = _GW_CACHE.get("base")
    if cached:
        try:
            cached_port = int(str(cached).rsplit(":", 1)[1])
            if 1 <= cached_port <= 65535 and cached_port not in candidates:
                candidates.append(cached_port)
        except (ValueError, IndexError):
            pass
    auth = {"Authorization": f"Bearer {token}"}
    attempted: set[int] = set()
    for phase in ("known", "scan"):
        if phase == "scan":
            last_miss = float(_GW_CACHE.get("miss_at") or 0.0)
            if last_miss and time.monotonic() - last_miss < GW_RESCAN_COOLDOWN_SEC:
                break
            candidates = await asyncio.to_thread(_local_ports)
        for port in candidates:
            if type(port) is not int or not 1 <= port <= 65535 or port in attempted:
                continue
            attempted.add(port)
            base = f"http://127.0.0.1:{port}"
            try:
                async with session.get(
                    f"{base}/api/v1/auth/status",
                    headers=auth,
                    timeout=aiohttp.ClientTimeout(total=4),
                ) as resp:
                    if resp.status == 200 and _auth_ok(await resp.text()):
                        _GW_CACHE.update(
                            base=base,
                            miss_at=0.0,
                            state="connected",
                            checked_at=time.time(),
                        )
                        return base
            except Exception:  # noqa: BLE001
                continue
        if phase == "scan":
            _GW_CACHE["miss_at"] = time.monotonic()
    _GW_CACHE.update(base=None, state="unreachable", checked_at=time.time())
    return None


def _collect_text(obj, out: list[str], depth: int = 0) -> None:
    """从任意结构的 JSON 里递归收集文本片段。

    SSE 事件的 data 结构会随网关版本变动（markdown/text/content 嵌套不定），
    所以不做精确取路径，而是"见 TEXT_KEYS 里的字符串就收"。
    depth 上限防恶意/异常嵌套把递归打爆；裸字符串本身不收（防把事件名当正文）。
    """
    if depth > 6:
        return
    if isinstance(obj, str):
        return
    if isinstance(obj, list):
        for item in obj:
            _collect_text(item, out, depth + 1)
        return
    if isinstance(obj, dict):
        # 先按已知键定向取（正文几乎都在这里）
        for key in TEXT_KEYS:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                out.append(val)
            elif isinstance(val, (dict, list)):
                _collect_text(val, out, depth + 1)
        # 再兜底扫所有嵌套结构（正文藏在未知键里的情况）
        for val in obj.values():
            if isinstance(val, (dict, list)):
                _collect_text(val, out, depth + 1)


class Main(DrawFlow, ProactiveChat, star.Star):
    """插件主类：QQ 消息 ↔ WorkBuddy 网关 的全部桥接逻辑。

    AstrBot 插件生命周期：__init__ → initialize() → on_message()* → terminate()。
    Transient scheduling state belongs to this instance. Durable memory is
    managed separately by MemoryService and survives plugin reloads.
    """

    def __init__(self, context: star.Context, config=None) -> None:
        self.context = context
        # 2026-10-27 修：AstrBot 实例化插件时先试 Main(context=..., config=plugin_config)，
        # 签名不收 config 会抛 TypeError，被框架吞掉后**退回无配置实例**重来一遍。
        # 结果 self.config 从未被赋值，_cfg() 恒拿 None，面板里 53 项全部退回代码常量
        # （实锤：142 次启动横幅都印 1800，而面板写的是 900）。这里接住并落存。
        if config is not None:
            self.config = config
        self._instance_id = uuid.uuid4().hex
        self._runtime_started_at = 0.0
        self._status_task: asyncio.Task | None = None
        self._memory = memory_service.MemoryService(
            Path(__file__).with_name("memory.sqlite3"), logger=logger
        )
        self._memory_task: asyncio.Task | None = None
        self._question_last_outcome = ""
        self._question_last_failure = ""
        self._question_last_failure_at = 0.0
        self._stopped = False
        self._account_ok = not bool(EXPECTED_SELF_ID)
        self._background_tasks: set[asyncio.Task] = set()
        self._question_operations: set[asyncio.Task] = set()
        self._questions = request_queue.RequestQueue(
            Path(__file__).with_name("question_run.json"),
            max_waiters=max(0, min(50, self._cfg_int("question_queue_limit", 5))),
            wait_timeout=max(1, min(300, self._cfg_int("question_queue_wait_sec", 60))),
            logger=logger,
        )
        # 会话代次：umo -> 第几代。/重置 会 +1，从而换到新的 agent 会话（见 _conv_id）
        self._generation: dict[str, int] = {}
        # 待审批请求：umo -> [ {sender_id, sender_name, text}, ... ]（先进先出）
        self._pending: dict[str, list[dict]] = {}
        self._maintenance_task: asyncio.Task | None = None
        self._cleanup_stop = threading.Event()
        # 群梗规则用的流水账：group_id -> [{sender, name, text, ts}, ...]
        self._gag_history: dict[str, list[dict]] = {}
        # 上一次主动开口的时间（全局，跨群），用于节流
        self._last_spark_global = 0.0
        # 自己在这个群说过什么：group_id -> [{"text","ts"}...]
        # 用途：下次开口时把"我上轮说了啥、群里有没有人接"带过去，
        # 否则每轮都从零开始，接不上话（2026-10-15 管理员反馈）
        self._spark_said: dict[str, list[dict]] = {}
        # 跟随模式：自己说完后，群友在这段时间内还在聊就继续接。
        # 超时则视为话题结束，重新走"冷场才开口"的逻辑。
        self.FOLLOW_WINDOW_SEC = 180
        # 跟随模式的"文字门槛"：群友得至少发这么长的字才算在接话。
        # 只有图/视频/表情/"6"/"？"不算 —— 见 _note_group_activity 的注释。
        self.FOLLOW_MIN_TEXT_LEN = 4
        # 2026-10-19：连续跟话轮次上限。群 2000000004 抱怨太频繁，
        # 跟到 FOLLOW_MAX_CHAIN 次就把这波话题的跟随机会掐掉，
        # 必须冷场重置或被 @ 才恢复（见 _follow_chain_ok）。
        self.FOLLOW_MAX_CHAIN = FOLLOW_MAX_CHAIN
        self.FOLLOW_CHAIN_COOLDOWN_SEC = FOLLOW_CHAIN_COOLDOWN_SEC
        # 每群跟随连击计数：group_id -> {"n": 连续轮数, "ts": 上次跟随时间}
        self._follow_chain: dict[str, dict] = {}
        # 主动发言连续失败几次就熔断（网关挂了重试也没用）
        self._spark_fail_streak = 0
        # 最近的图片链接（只存地址、不下载）：group_id -> [{url, sender, name, ts}, ...]
        self._recent_images: dict[str, list[dict]] = {}
        self._memes = meme_library.MemeLibrary(WORK_DIR / "表情包")
        self._meme_collector = meme_collection.Collector(
            WORK_DIR / "表情包", logger=logger
        )
        # 等补图："{group}:{sender}" -> {"text": 原话, "ts": 时间}
        # A @ 我说"看看这张"但当时没图 —— 先记一笔，等他补发。
        self._img_wait: dict[str, dict] = {}
        # 群活跃时间（供"适时闲聊"判断冷场）：group_id -> {last_ts, umo, last_spark_ts}
        self._group_activity: dict[str, dict] = {}
        # 上次发"没接住话"兜底提示的时间：umo -> ts（按会话节流）
        self._silent_notice: dict[str, float] = {}
        # 戳一戳的冷却："{umo}#{sender}" -> 上次回应时间戳（同人连戳只回第一下）
        self._poke_last: dict[str, float] = {}
        # 后台巡检任务
        self._idle_task: asyncio.Task | None = None
        # 群内角色扮演状态：group_id -> {persona, owner, ts}
        # 按群隔离 —— 一个群一个人设，互不串味
        self._roleplay: dict[str, dict] = {}
        # 关键词冒泡的节流：group_id -> 上次开口时间戳
        self._kw_last: dict[str, float] = {}
        # 每群每日计数：group_id -> ("2026-10-16", n)，跨天自动归零
        self._kw_daily: dict[str, tuple[str, int]] = {}
        # 关键词冒泡的全局间隔（不分群），防止多群接连开腔像串台
        self._kw_last_global = 0.0
        # 生图节流（2026-10-19）：group_id -> 上次出图时间戳
        self._draw_last: dict[str, float] = {}
        # 全局间隔：CLI 一次要跑几十秒，多群同时进来会排队挤爆
        self._draw_last_global = 0.0
        # 每群每日计数：group_id -> ("2026-10-19", n)，跨天自动归零
        self._draw_daily: dict[str, tuple[str, int]] = {}
        # 一人一框（2026-10-22 改回）：QQ 号 -> 即梦会话 ID，落盘跨重启复用
        self._jimeng = jimeng.JimengClient(DRAW_SESSION_STATE_FILE, logger=logger)
        self._draw_jobs = image_jobs.ImageJobs(
            DRAW_SESSION_STATE_FILE.with_name("draw_jobs"),
            logger=logger,
            output_root=Path(str(self._cfg("image_gen_dir", "") or IMAGE_GEN_DIR)),
            selfie_prefix=DRAW_SELFIE_PROMPT,
        )
        self._draw_running: set[str] = set()
        self._draw_slots = asyncio.Semaphore(2)
        self._draw_recovery_task: asyncio.Task | None = None
        # 正在画的 QQ 号：同一个人复用同一个框，两条并发生成会互相踩
        self._draw_busy: set[str] = set()
        # 热重载：本文件当前 mtime + watcher 任务
        try:
            self._code_mtime = max(
                source.stat().st_mtime
                for source in source_files.source_paths(Path(__file__))
            )
        except OSError:
            self._code_mtime = 0.0
        self._hot_task: asyncio.Task | None = None
        self._reload_task: asyncio.Task | None = None

    # ------------------------------------------------------------ 配置读取
    def _cfg(self, key: str, default):
        """读面板配置；取不到就退回代码里的默认值。"""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return default
        try:
            val = cfg.get(key)
        except Exception:  # noqa: BLE001
            return default
        return default if val is None else val

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """读布尔型面板配置。面板可能存成字符串，所以做一次宽松转换。"""
        val = self._cfg(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def _cfg_int(self, key: str, default: int) -> int:
        """读整型面板配置；转不动就用默认值，绝不让配置错误把插件打崩。"""
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _roleplay_allowed(self, sender_id: str) -> bool:
        """这位群友是否能用角色扮演。默认对所有群友开放。"""
        if not self._cfg_bool("roleplay_enabled", ROLEPLAY_ENABLED):
            return False
        if self._cfg_bool("roleplay_open_to_all", ROLEPLAY_OPEN_TO_ALL):
            return True
        raw = str(self._cfg("roleplay_allowed", "") or "")
        allowed = tuple(x.strip() for x in raw.split(",") if x.strip())
        if not allowed:
            allowed = ROLEPLAY_ALLOWED
        return str(sender_id or "").strip() in allowed

    def _current_persona(self, event, *, touch: bool = True) -> str:
        """Read the active group persona, optionally renewing direct interaction.

        Args:
            event: Original event identifying the current group.
            touch: Renew the timeout only when the persona is actually used.

        Returns:
            The current persona, or an empty string for absent or expired state.
        """
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return ""
        st = self._roleplay.get(group_id)
        if not st:
            return ""
        timeout = self._cfg_int("roleplay_timeout_sec", ROLEPLAY_TIMEOUT_SEC)
        if time.time() - float(st.get("ts") or 0) > timeout:
            self._roleplay.pop(group_id, None)
            logger.info(f"workbuddy_bridge: [人设] 群{group_id} 超时未续，已退场")
            return ""
        if touch:
            st["ts"] = time.time()
        return str(st.get("persona") or "")

    def _handle_roleplay_cmd(
        self, event, text: str, sender_id: str, is_master: bool = False
    ):
        """处理 /扮演 与 /结束扮演。返回要回复的文本，None 表示不是指令。"""
        group_id = str(event.get_group_id() or "").strip()

        stop_hit = _hit_words(text, ROLEPLAY_STOP_WORDS)
        if stop_hit:
            # 叫停只作用于**当前群**。
            # 管理员的份量体现在"说了必须算"，而不是"一停全停"。
            if group_id and self._roleplay.pop(group_id, None) is not None:
                who = "管理员" if is_master else "群友"
                logger.info(f"workbuddy_bridge: [人设] 群{group_id} 已由{who}叫停")
                return "出戏了。有事说事。"
            return "这会儿本来也没在演。"

        if not group_id:
            return None
        # 只看消息**开头**：这样"我想扮演一下"之类的日常说法不会误触发，
        # 而被框架剥掉斜杠后的"扮演 猫娘"照样能认出来。
        start_hit = next((w for w in ROLEPLAY_START_WORDS if text.startswith(w)), None)
        if not start_hit:
            return None

        if not is_master and not self._roleplay_allowed(sender_id):
            return "这个功能还没对你开放。"
        persona = text.split(start_hit, 1)[1].strip() if start_hit in text else ""
        if not persona:
            return "给个设定呗，比如：/扮演 一个话很少的剑客"
        self._roleplay[group_id] = {
            "persona": persona[:300],
            "owner": str(sender_id or "").strip(),
            "ts": time.time(),
        }
        logger.info(f"workbuddy_bridge: [人设] 群{group_id} 开启：{persona[:40]!r}")
        return "行，入戏了。不想演了就说 /结束扮演"

    async def _handle_relay_cmd(self, event, text: str, is_master: bool):
        """管理员指令「代发 <群号> <内容>」：把内容主动发到指定群。

        返回要回复的文本；None 表示这不是代发指令。
        """
        hit = next((w for w in RELAY_WORDS if text.startswith(w)), None)
        if not hit:
            return None
        if not is_master:
            return "这条只有号主能用。"

        parts = text[len(hit) :].strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[0].isdigit():
            return "格式是这样：代发 群号 内容"

        target_group, payload = parts[0], parts[1].strip()
        if not payload:
            return "内容不能是空的。"

        # 平台名从当前消息推断，避免硬编码
        origin = str(event.unified_msg_origin or "")
        platform = origin.split(":", 1)[0] if ":" in origin else ""
        if not platform:
            return "取不到平台标识，这条发不了。"

        umo = f"{platform}:GroupMessage:{target_group}"
        ok = await self._send_to_session(umo, payload)
        logger.info(
            f"workbuddy_bridge: [代发] 管理员要求发到群{target_group}："
            f"{payload[:30]!r} → {'成功' if ok else '失败'}"
        )
        return (
            f"发了：{payload[:20]}"
            if ok
            else f"没发出去 —— 群号 {target_group} 可能不对，或者我不在那个群里。"
        )

    def _handle_reload_cmd(self, text: str, is_master: bool):
        """管理员指令「重载插件」：watcher 失灵时的手动兜底。

        返回要回复的文本；None 表示不是这个指令。
        """
        if (text or "").strip() not in RELOAD_WORDS:
            return None
        if not is_master:
            return "这条只有号主能用。"
        logger.info("workbuddy_bridge: [热重载] 管理员从 QQ 手动触发")
        self._spawn_reload()
        return "收到，正在重载。现有任务收尾后加载新代码。"

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载完毕：起一个后台巡检，负责"适时闲聊"。"""
        self._stopped = True
        context = getattr(self, "context", None)
        previous_memory = getattr(context, "_workbuddy_bridge_memory_drain", None)
        if previous_memory is not None and previous_memory is not self._memory:
            if not await previous_memory.close(timeout=5):
                raise RuntimeError("Previous memory operations are still draining")
            if (
                getattr(context, "_workbuddy_bridge_memory_drain", None)
                is previous_memory
            ):
                delattr(context, "_workbuddy_bridge_memory_drain")
        self._stopped = False
        # 把面板配置同步给模块级清洗函数（_sanitize 拿不到 self）
        _RUNTIME["max_chars"] = max(
            200, self._cfg_int("max_reply_chars", MAX_REPLY_CHARS)
        )
        _RUNTIME["others_max_chars"] = max(
            200, self._cfg_int("others_max_chars", OTHERS_MAX_CHARS)
        )
        logger.info(
            f"workbuddy_bridge: 回复长度上限 —— 管理员 "
            f"{_RUNTIME['max_chars']} 字 / 其他群友 "
            f"{_RUNTIME['others_max_chars']} 字"
        )

        if self._cfg_bool("hot_reload_enabled", HOT_RELOAD_ENABLED):
            if self._hot_task is None or self._hot_task.done():
                self._hot_task = self._spawn_background(self._hot_reload_loop())
            logger.info(
                f"workbuddy_bridge: hot reload enabled (poll every {HOT_RELOAD_INTERVAL}s; "
                "saved changes to declared plugin sources trigger reload)"
            )

        if self._draw_recovery_task is None or self._draw_recovery_task.done():
            self._draw_recovery_task = self._spawn_background(
                self._draw_recovery_loop()
            )

        if self._maintenance_task is None or self._maintenance_task.done():
            self._cleanup_stop.clear()
            self._maintenance_task = self._spawn_background(self._maintenance_loop())

        if not self._cfg_bool("idle_chat_enabled", IDLE_CHAT_ENABLED):
            logger.info("workbuddy_bridge: 适时闲聊已在面板中关闭，不启动巡检")
        else:
            if self._idle_task is None or self._idle_task.done():
                self._idle_task = self._spawn_background(self._idle_loop())
            _t, _c = self._idle_timing("__probe__")
            _hi = self._hifreq_groups()
            logger.info(
                f"workbuddy_bridge: 适时闲聊已启动（常规群闲置 {_t}s 触发、"
                f"冷却 {_c}s；梗群 {_hi or '无'} 提频到 "
                f"{self._cfg_int('idle_hifreq_trigger_sec', IDLE_HIFREQ_TRIGGER_SEC)}s / "
                f"{self._cfg_int('idle_hifreq_cooldown_sec', IDLE_HIFREQ_COOLDOWN_SEC)}s，"
                f"巡检间隔 {IDLE_CHECK_INTERVAL}s）"
            )
        if not self._runtime_started_at:
            self._runtime_started_at = time.time()
        logger.info(
            f"workbuddy_bridge: [runtime] sha256={CODE_SHA256} "
            f"instance={self._instance_id} started_at={self._runtime_started_at:.6f}"
        )
        try:
            self._write_runtime_status()
        except OSError as exc:
            logger.warning(
                "workbuddy_bridge: status snapshot unavailable: %s", type(exc).__name__
            )
        if self._memory_task is None or self._memory_task.done():
            self._memory_task = self._spawn_background(self._memory.loop(self))
        if self._status_task is None or self._status_task.done():
            self._status_task = self._spawn_background(self._runtime_status_loop())

    async def terminate(self) -> None:
        """Stop admission and attempt bounded cleanup without losing pending work.

        Raises:
            RuntimeError: Database work is still draining; a reload must wait.
        """
        first_stop = not getattr(self, "_stopped", False)
        self._stopped = True
        self._cleanup_stop.set()
        self._questions.close()
        tasks = set(self._background_tasks)
        tasks.update(self._question_operations)
        tasks.update(t for t in (self._idle_task, self._hot_task) if t is not None)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            # Repeated shutdown attempts must not cancel a worker's cleanup wait.
            if not task.done() and not task.cancelling():
                task.cancel()
        pending = set()
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=8 if first_stop else 0)
            for task in done:
                if not task.cancelled():
                    task.exception()
            # wait() observes the deadline without cancelling tasks a second time.
            # Keep strong references until their existing callbacks remove them.
            self._background_tasks.update(pending)
            for task in pending:
                task.add_done_callback(self._background_tasks.discard)
                task.add_done_callback(
                    lambda finished: (
                        None if finished.cancelled() else finished.exception()
                    )
                )
        memory = getattr(self, "_memory", None)
        context = getattr(self, "context", None)
        memory_ready = True
        if memory is not None:
            memory_ready = await memory.close(timeout=5)
            if memory_ready is False:
                try:
                    setattr(context, "_workbuddy_bridge_memory_drain", memory)
                except (AttributeError, TypeError):
                    # The store's generation fence still rejects retired writes.
                    logger.warning(
                        "workbuddy_bridge: shared memory drain gate unavailable"
                    )
            elif getattr(context, "_workbuddy_bridge_memory_drain", None) is memory:
                delattr(context, "_workbuddy_bridge_memory_drain")
        self._background_tasks.difference_update(
            task for task in tuple(self._background_tasks) if task.done()
        )
        self._question_operations.difference_update(
            task for task in tuple(self._question_operations) if task.done()
        )
        for name in (
            "_memory_task",
            "_idle_task",
            "_hot_task",
            "_maintenance_task",
            "_status_task",
        ):
            task = getattr(self, name, None)
            if task is None or task.done():
                setattr(self, name, None)
        try:
            self._write_runtime_status("stopped")
        except OSError as exc:
            logger.warning(
                "workbuddy_bridge: final status snapshot unavailable: %s",
                type(exc).__name__,
            )
        pending = {task for task in pending if not task.done()}
        if pending or memory_ready is False:
            logger.warning(
                "workbuddy_bridge: shutdown still draining: tasks=%d memory_ready=%s",
                len(pending),
                memory_ready,
            )
        if memory_ready is False:
            raise RuntimeError("Memory cleanup is still running; reload deferred")

    def _write_runtime_status(self, state: str = "running") -> None:
        """Publish current operational fields through the isolated status writer.

        Args:
            state: running after initialization, or stopped after task cleanup.

        Raises:
            OSError: The snapshot cannot be replaced; the prior copy is preserved.
            ValueError: The lifecycle state is unsupported.
        """
        memory_summary = getattr(getattr(self, "_memory", None), "summary", None)
        runtime_status.write_snapshot(
            Path(__file__).with_name("bridge_status.json"),
            state=state,
            instance=self._instance_id,
            source_sha256=CODE_SHA256,
            started_at=self._runtime_started_at,
            question_active=bool(self._question_operations),
            question_waiting=self._questions.waiting_count,
            question_limit=self._questions.max_waiters,
            read_remote=self._questions.read_remote,
            last_outcome=self._question_last_outcome,
            last_failure=self._question_last_failure,
            last_failure_at=self._question_last_failure_at,
            proactive_failures=self._spark_fail_streak,
            proactive_failure_limit=SPARK_FAIL_CIRCUIT_N,
            gateway_base=_GW_CACHE.get("base"),
            gateway_state=_GW_CACHE.get("state", "unknown"),
            gateway_checked_at=_GW_CACHE.get("checked_at", 0.0),
            empty_reply_markers=EMPTY_REPLY_MARKERS,
            memory_status=memory_summary() if callable(memory_summary) else None,
        )

    async def _runtime_status_loop(self) -> None:
        """Refresh liveness independently of chat traffic and optional features."""
        while not self._stopped:
            await asyncio.sleep(RUNTIME_STATUS_INTERVAL_SEC)
            if self._stopped:
                return
            try:
                self._write_runtime_status()
            except OSError as exc:
                logger.warning(
                    "workbuddy_bridge: status heartbeat unavailable: %s",
                    type(exc).__name__,
                )

    def _spawn_background(self, coro) -> asyncio.Task | None:
        """Start and track a coroutine until plugin shutdown.

        Args:
            coro: Coroutine owned by this plugin instance.

        Returns:
            The scheduled task, or None after shutdown.
        """
        if self._stopped:
            coro.close()
            return None
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _can_send_active(self) -> bool:
        """Return whether this instance may send an active message."""
        return (
            not getattr(self, "_stopped", False)
            and not _is_paused()
            and getattr(self, "_account_ok", True)
        )

    async def _handle_question_recovery(self, text: str, master: bool) -> str | None:
        """Handle an explicit operator acknowledgement of a completed remote run.

        Args:
            text: Original chat text.
            master: Verified administrator identity from the message event.

        Returns:
            An acknowledgement, or None for unrelated messages.
        """
        if not text.startswith("/恢复问答"):
            return None
        if not master:
            return "这条需要管理员操作。"
        if text.strip() != "/恢复问答 确认上一轮已结束":
            return "先确认 WorkBuddy 上一轮任务已经结束，再发送：/恢复问答 确认上一轮已结束"
        admission = await self._questions.acquire(background=True)
        if admission != "acquired":
            return "当前还有问答正在处理，等它结束后再恢复。"
        try:
            self._questions.clear_remote()
            self._spark_fail_streak = 0
            logger.info(
                "workbuddy_bridge: operator acknowledged previous remote run completion"
            )
            return "已清除上一轮的占用记录，可以重新提问了。"
        finally:
            self._questions.release()

    # ------------------------------------------------------------ 热重载
    async def _hot_reload_loop(self) -> None:
        """Watch all declared plugin sources and reload after a stable edit."""
        while True:
            await asyncio.sleep(HOT_RELOAD_INTERVAL)
            try:
                mtime = max(
                    source.stat().st_mtime
                    for source in source_files.source_paths(Path(__file__))
                )
            except OSError:
                continue
            if mtime <= self._code_mtime:
                continue
            # 防抖：编辑器可能分几次写盘，等它写完再动手
            await asyncio.sleep(HOT_RELOAD_DEBOUNCE)
            try:
                mtime = max(
                    source.stat().st_mtime
                    for source in source_files.source_paths(Path(__file__))
                )
            except OSError:
                continue
            if mtime <= self._code_mtime:
                continue
            self._code_mtime = mtime
            logger.info("workbuddy_bridge: [热重载] 检测到代码更新，触发重载")
            self._spawn_reload()
            return  # 本协程到此为止；新实例会起自己的 watcher

    def _purge_own_modules(self) -> int:
        """把本插件在 sys.modules 里的缓存清掉，返回清掉的模块数。

        ## 为什么必须自己清（2026-10-09 实测踩到）
        AstrBot v4 的 `PluginManager.reload()` **只**做三件事：
        `_terminate_plugin` → `_unbind_plugin` → `load()`。
        它**没有**清 `sys.modules`（对比之下，只有加载失败时才会走
        `_cleanup_plugin_state` 去清）。
        而 `load()` 内部是 `__import__(path, fromlist=[...])` ——
        Python 只要在 `sys.modules` 里命中 `data.plugins.<名>.main`，
        就把**旧模块对象**原样返回，**根本不会重新读磁盘上的 .py**。

        后果极其隐蔽：日志照打 `Loading plugin ...`、照打
        `[热重载] 成功，新代码已生效`，插件实例也重建了 ——
        但实例化用的是**旧模块里的旧类**，跑到永远是旧代码。
        表现为：改完文件、重载显示成功，功能却一点没变
        （本次即 `_strip_markdown` 写了却不生效，`**` 照旧发进群）。
        判据看日志行号：`[回复]` 打的是 `main:2549`，而磁盘上它已在 2624。

        ## 清法
        按前缀删 `data.plugins.workbuddy_bridge` 及其子模块
        （含 `main` 与 `__pycache__` 对应的 `main` 模块）。
        顺带删 `__pycache__` 目录，避免残留 .pyc 干扰。
        """
        prefix = "data.plugins.workbuddy_bridge"
        removed = 0
        # list() 复制一份键视图 —— 循环里要删键，直接遍历视图会 RuntimeError
        for key in list(sys.modules.keys()):
            if key == prefix or key.startswith(prefix + "."):
                del sys.modules[key]
                removed += 1
        # 顺手删 .pyc 缓存目录，免得框架 import 时命中旧字节码
        cache_dir = os.path.join(os.path.dirname(__file__), "__pycache__")
        try:
            shutil.rmtree(cache_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        return removed

    def _spawn_reload(self) -> None:
        """把 reload 挂到事件循环顶层执行。

        不能在 watcher / 指令处理里直接 await：reload 第一步就是 terminate
        本插件，会把调用链一起取消。独立 task 不归插件管，能活着跑完全程。
        """

        current = getattr(self, "_reload_task", None)
        if current is not None and not current.done():
            return

        async def _do_reload() -> None:
            await asyncio.sleep(0.5)  # 让"正在重载"的回复先发出去
            try:
                pm = getattr(self.context, "_star_manager", None)
                if pm is None:
                    logger.warning(
                        "workbuddy_bridge: [热重载] 拿不到 PluginManager，放弃"
                    )
                    return
                # Drain before clearing imported modules or asking the framework
                # to construct another instance. An unresponsive worker remains
                # tracked; completion automatically retries the preflight.
                while True:
                    try:
                        await self.terminate()
                        break
                    except RuntimeError:
                        memory = getattr(self, "_memory", None)
                        pending = set(getattr(memory, "_io_tasks", ()))
                        pending = {task for task in pending if not task.done()}
                        if pending:
                            logger.warning(
                                "workbuddy_bridge: reload awaiting %d cleanup tasks",
                                len(pending),
                            )
                            await asyncio.wait(pending)
                        else:
                            # A task may finish between the timeout and inspection.
                            await self.terminate()
                            break
                # 关键一步：框架的 reload() 不清 sys.modules，
                # 不清就会拿旧模块对象糊弄过去（详见 _purge_own_modules）。
                n = self._purge_own_modules()
                logger.info(
                    f"workbuddy_bridge: [热重载] 已清理 {n} 个模块缓存，"
                    "框架将重新读盘编译"
                )
                ok, err = await pm.reload("workbuddy_bridge")
                level = logger.info if ok else logger.warning
                level(
                    "workbuddy_bridge: [热重载] "
                    + ("成功，新代码已生效" if ok else f"失败: {err}")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"workbuddy_bridge: [热重载] 异常: {exc}")

        self._reload_task = asyncio.create_task(_do_reload())
        self._reload_task.add_done_callback(
            lambda finished: None if finished.cancelled() else finished.exception()
        )

    # ---------------------------------------------------------------- 调用
    def _record_question_outcome(self, reply: str) -> None:
        """Keep only a fixed outcome category for local operational diagnostics.

        Args:
            reply: Internal result or model reply; reply contents are not retained.
        """
        code = (
            "cancelled"
            if reply == "cancelled"
            else next(
                (
                    marker
                    for marker in EMPTY_REPLY_MARKERS
                    if (reply or "").startswith(marker)
                ),
                "reply",
            )
        )
        self._question_last_outcome = code
        if code not in {
            "reply",
            "cancelled",
            "[静默]",
            "[忙碌跳过]",
            "[过期跳过]",
            "[维护暂停]",
        }:
            self._question_last_failure = code
            self._question_last_failure_at = time.time()

    async def _ask(
        self,
        prompt: str,
        conv_id: str,
        master: bool = False,
        mode: str = "chat",
        out_dir: str | None = None,
        persona: str = "",
        *,
        background: bool = False,
        not_after: float | None = None,
    ) -> str:
        """Admit every QQ model request through one bounded queue.

        Args:
            prompt: Text for this request only.
            conv_id: Original conversation label.
            master: Administrator authorization for this invocation.
            mode: Chat or an explicitly approved execution.
            out_dir: Approved task's output directory.
            persona: Current group's persona.
            background: Skip instead of queueing disposable proactive replies.
            not_after: Absolute monotonic expiry for proactive content.

        Returns:
            Reply text or a distinct internal scheduling/transport status.
        """
        if getattr(self, "_stopped", False) or _is_paused():
            return "[维护暂停]"
        if mode not in ("chat", "execute") or (mode == "execute" and not master):
            logger.warning("workbuddy_bridge: rejected unapproved execution")
            return "[权限拒绝]"
        if mode == "execute" and not out_dir:
            return "[权限拒绝]"
        if not_after is not None and time.monotonic() >= not_after:
            return "[过期跳过]"
        note_dialogue = getattr(getattr(self, "_memory", None), "note_dialogue", None)
        if not background and callable(note_dialogue):
            note_dialogue()
        started = time.monotonic()
        wait_deadline = started + self._questions.wait_timeout
        kind = "background" if background else "foreground"
        logger.info(
            "workbuddy_bridge: [question] class=%s admission waiting=%s",
            kind,
            self._questions.waiting_count,
        )
        try:
            admission = await self._questions.acquire(background=background)
        except Exception as exc:  # noqa: BLE001
            logger.error("workbuddy_bridge: question queue unavailable: %s", exc)
            if not background and callable(note_dialogue):
                note_dialogue()
            self._record_question_outcome("[环境错误]")
            return "[环境错误]"
        if admission != "acquired":
            if not background and callable(note_dialogue):
                note_dialogue()
            statuses = {
                "busy": "[忙碌跳过]",
                "full": "[队列已满]",
                "expired": "[排队超时]",
                "closed": "[维护暂停]",
            }
            result = statuses[admission]
            self._record_question_outcome(result)
            logger.info(
                "workbuddy_bridge: [question] class=%s outcome=%s wait_ms=%d",
                kind,
                result,
                int((time.monotonic() - started) * 1000),
            )
            return result
        admitted = time.monotonic()
        result = "[内部错误]"
        try:
            if getattr(self, "_stopped", False) or _is_paused():
                result = "[维护暂停]"
            elif not_after is not None and time.monotonic() >= not_after:
                result = "[过期跳过]"
            else:
                operation = asyncio.create_task(
                    self._ask_gateway(
                        prompt,
                        conv_id,
                        master=master,
                        mode=mode,
                        out_dir=out_dir,
                        persona=persona,
                        background=background,
                        wait_deadline=wait_deadline,
                        not_after=not_after,
                    )
                )
                self._question_operations.add(operation)
                operation.add_done_callback(self._question_operations.discard)
                budget = (
                    max(0.001, not_after - time.monotonic())
                    if background and not_after is not None
                    else TIMEOUT_SEC + max(0, wait_deadline - time.monotonic())
                )
                try:
                    result = await asyncio.wait_for(operation, budget)
                except asyncio.TimeoutError:
                    result = "[过期跳过]" if background else "[超时]"
                if (
                    background
                    and not_after is not None
                    and time.monotonic() >= not_after
                ):
                    result = "[过期跳过]"
            return result
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        finally:
            self._questions.release()
            if not background and callable(note_dialogue):
                note_dialogue()
            self._record_question_outcome(result)
            outcome = self._question_last_outcome
            logger.info(
                "workbuddy_bridge: [question] class=%s outcome=%s wait_ms=%d service_ms=%d",
                kind,
                outcome,
                int((admitted - started) * 1000),
                int((time.monotonic() - admitted) * 1000),
            )

    async def _memory_local_idle(self, *, owns_question: bool = False) -> str:
        """Check visible local work before optional memory maintenance.

        Args:
            owns_question: The caller already owns the shared question lock.

        Returns:
            ready, not_idle, or idle_unverified; uncertainty never admits work.
        """
        if (
            not self._can_send_active()
            or (self._questions.busy and not owns_question)
            or self._questions.waiting_count
            or getattr(self, "_draw_busy", None)
            or getattr(self, "_draw_running", None)
            or any(
                not task.done() and task is not asyncio.current_task()
                for task in getattr(self, "_question_operations", ())
            )
        ):
            return "not_idle"
        try:
            now = time.time()
            for entries in getattr(self, "_pending", {}).values():
                for request in entries:
                    stamp = request.get("ts")
                    if type(stamp) not in (int, float) or not 0 < stamp <= now + 5:
                        return "idle_unverified"
                    if now - stamp <= PENDING_TTL_SEC:
                        return "not_idle"
            jobs = self._draw_jobs

            def read_jobs():
                directory = Path(jobs.directory)
                try:
                    before = {
                        path.name
                        for path in directory.iterdir()
                        if path.suffix == ".json"
                    }
                except FileNotFoundError:
                    before = set()
                records = jobs.list_jobs()
                try:
                    after = {
                        path.name
                        for path in directory.iterdir()
                        if path.suffix == ".json"
                    }
                except FileNotFoundError:
                    after = set()
                if before != after or {job["id"] + ".json" for job in records} != after:
                    raise ValueError("Image progress was incomplete or changed")
                return records

            records = await asyncio.to_thread(read_jobs)
            if any(
                job["state"] not in {"delivered", "failed", "expired", "ended"}
                for job in records
            ):
                return "not_idle"
            if (
                not self._can_send_active()
                or (self._questions.busy and not owns_question)
                or self._questions.waiting_count
                or getattr(self, "_draw_busy", None)
                or getattr(self, "_draw_running", None)
                or any(
                    time.time() - float(request.get("ts", 0)) <= PENDING_TTL_SEC
                    for entries in getattr(self, "_pending", {}).values()
                    for request in entries
                )
            ):
                return "not_idle"
        except (AttributeError, OSError, ValueError, TypeError, KeyError):
            return "idle_unverified"
        return "ready"

    async def _memory_idle_status(self) -> str:
        """Inspect local work and the authenticated gateway without submitting a run.

        Returns:
            A fixed readiness category. This covers visible work, not every
            independent WorkBuddy desktop session or an atomic remote reservation.
        """
        local = await self._memory_local_idle()
        if local != "ready":
            return local
        if aiohttp is None:
            return "idle_unverified"
        _, token = _cfg()
        if not token:
            return "gateway_unreachable"
        try:
            admission = await self._questions.acquire(background=True)
        except (OSError, ValueError):
            return "idle_unverified"
        if admission != "acquired":
            return "not_idle"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5, connect=3), trust_env=False
            ) as session:
                base = await _resolve_base(session, token)
                if base is None:
                    return "gateway_unreachable"
                try:
                    pending = self._questions.read_remote()
                except (OSError, ValueError):
                    return "pending_confirmation"
                if pending:
                    if not pending["run_id"]:
                        return "pending_confirmation"
                    if pending["base"] != base:
                        async with session.get(
                            pending["base"] + "/api/v1/auth/status",
                            headers={"Authorization": f"Bearer {token}"},
                            timeout=aiohttp.ClientTimeout(total=3),
                            allow_redirects=False,
                        ) as response:
                            if response.status != 200 or not _auth_ok(
                                await response.text()
                            ):
                                return "pending_confirmation"
                    active = await self._run_still_active(
                        session, pending["base"], token, pending["run_id"], timeout=3
                    )
                    if active is not False:
                        return "pending_confirmation"
                    self._questions.clear_remote()
                idle = await self._gateway_idle(session, base, token)
                if idle is None:
                    return "idle_unverified"
                if idle is False:
                    return "not_idle"
                return await self._memory_local_idle(owns_question=True)
        except (OSError, ValueError, asyncio.TimeoutError):
            return "idle_unverified"
        except Exception as exc:  # noqa: BLE001 - optional readiness fails closed.
            logger.warning(
                "workbuddy_bridge: memory idle check unavailable: %s",
                type(exc).__name__,
            )
            return "idle_unverified"
        finally:
            self._questions.release()

    async def _ask_gateway(
        self,
        prompt: str,
        conv_id: str,
        master: bool = False,
        mode: str = "chat",
        out_dir: str | None = None,
        persona: str = "",
        *,
        background: bool = False,
        wait_deadline: float | None = None,
        not_after: float | None = None,
    ) -> str:
        """Submit under the queue's process lock and preserve uncertain runs.

        Args:
            prompt: Current request with its own context.
            conv_id: Original QQ conversation label.
            master: Whether execution was authorized by the administrator.
            mode: Chat or approved execution.
            out_dir: Isolated output directory for approved execution.
            persona: Current group's optional persona.
            background: Whether this is disposable proactive conversation.
            wait_deadline: Absolute monotonic deadline for waiting on a prior run.
            not_after: Absolute expiry for proactive material.

        Returns:
            Correlated reply text or an internal outcome marker.
        """
        if getattr(self, "_stopped", False) or _is_paused():
            return "[维护暂停]"
        if mode not in ("chat", "execute") or (mode == "execute" and not master):
            logger.warning("workbuddy_bridge: rejected unapproved execution")
            return "[权限拒绝]"
        if mode == "execute" and not out_dir:
            return "[权限拒绝]"
        if aiohttp is None:
            return "[环境错误] Missing aiohttp."
        _, token = _cfg()
        if not token:
            _GW_CACHE.update(base=None, state="not_configured", checked_at=time.time())
            return "[未配置] Missing gateway credentials."

        # ---- 走 runs 通道（2026-10-23 回退）----
        # 为什么回退：ACP 旁问（sideQuestion）是纯文本、tools=False，
        # 模型手里没有任何工具 —— 读不了落盘的图、也搜不了网。
        # runs 是完整的 agent 任务，模型带全套工具，图落盘后递个路径它自己能看。
        # 代价是它会挤进桌面端当前会话（见下面的回执码）。
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        # 回执码：让"这段回复到底属于哪次请求"可判定（详见 _receipt_gate）。
        # 没有它，桌面端窗口里的回复会被中转的 run 抢走发进群（2026-10-22 事故）。
        nonce = uuid.uuid4().hex[:8]
        receipt = f"确认-{nonce}"
        request_id = f"qq-{uuid.uuid4().hex[:16]}"
        body = {
            "id": request_id,
            "type": "text",
            "text": _prompt_prefix(
                master, mode, out_dir, persona, channel=conv_id, receipt=receipt
            )
            + prompt,
            "source": {
                "platform": "generic",
                "sender": {"id": conv_id, "name": "QQ"},
                "conversation": {"id": conv_id, "type": "direct"},
            },
            "timeoutMs": TIMEOUT_SEC * 1000,
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC, connect=10),
                trust_env=False,
            ) as session:
                base = await _resolve_base(session, token)
                if base is None:
                    return "[网关不可达]"
                if getattr(self, "_stopped", False) or _is_paused():
                    return "[维护暂停]"
                try:
                    pending = self._questions.read_remote()
                except (OSError, ValueError) as exc:
                    logger.error(
                        "workbuddy_bridge: saved question state is unreadable: %s", exc
                    )
                    return "[状态待确认]"
                if pending:
                    if not pending["run_id"]:
                        return "[状态待确认]"
                    # A moved endpoint is not proof that the prior task has ended.
                    # Reauthenticate the recorded loopback endpoint before querying it.
                    if pending["base"] != base:
                        try:
                            async with session.get(
                                pending["base"] + "/api/v1/auth/status",
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=3),
                                allow_redirects=False,
                            ) as response:
                                if response.status != 200 or not _auth_ok(
                                    await response.text()
                                ):
                                    return "[状态待确认]"
                        except Exception:  # noqa: BLE001 - retain unresolved ownership.
                            return "[状态待确认]"
                    while True:
                        remaining = (
                            wait_deadline or time.monotonic()
                        ) - time.monotonic()
                        if remaining <= 0:
                            return "[忙碌跳过]" if background else "[排队超时]"
                        active = await self._run_still_active(
                            session,
                            pending["base"],
                            token,
                            pending["run_id"],
                            timeout=min(5, remaining),
                        )
                        if active is False:
                            self._questions.clear_remote()
                            break
                        if active is None:
                            return "[状态待确认]"
                        if background:
                            return "[忙碌跳过]"
                        if getattr(self, "_stopped", False) or _is_paused():
                            return "[维护暂停]"
                        await asyncio.sleep(
                            min(1, max(0, (wait_deadline or 0) - time.monotonic()))
                        )
                if getattr(self, "_stopped", False) or _is_paused():
                    return "[维护暂停]"
                if re.fullmatch(r"qq-memory-[a-f0-9]{12}", conv_id):
                    local = await self._memory_local_idle(owns_question=True)
                    if local != "ready":
                        return "[任务忙碌]" if local == "not_idle" else "[空闲未确认]"
                    idle = await self._gateway_idle(session, base, token)
                    if idle is not True:
                        return "[任务忙碌]" if idle is False else "[空闲未确认]"
                    # A human or image task may have arrived while status was read.
                    local = await self._memory_local_idle(owns_question=True)
                    if local != "ready":
                        return "[任务忙碌]" if local == "not_idle" else "[空闲未确认]"
                if not_after is not None and time.monotonic() >= not_after:
                    return "[过期跳过]"
                if background and not_after is not None:
                    body["timeoutMs"] = max(
                        1,
                        min(
                            TIMEOUT_SEC * 1000,
                            int((not_after - time.monotonic()) * 1000),
                        ),
                    )
                record = {
                    "base": base,
                    "request_id": request_id,
                    "run_id": "",
                    "started_at": time.time(),
                }
                self._questions.save_remote(record)
                async with session.post(
                    f"{base}/api/v1/runs", json=body, headers=headers
                ) as resp:
                    raw = await resp.text()
                    if resp.status in (400, 401, 403, 404, 422, 429):
                        self._questions.clear_remote()
                    if resp.status == 401:
                        return "[凭据失效]"
                    if resp.status not in (200, 202):
                        logger.warning(
                            f"workbuddy_bridge: POST runs -> {resp.status} {raw[:300]}"
                        )
                        return f"[网关错误] HTTP {resp.status}"
                    try:
                        run_id = json.loads(raw).get("data", {}).get("runId")
                    except ValueError:
                        run_id = None
                if not isinstance(run_id, str) or not run_id.strip():
                    return "[状态待确认]"
                self._questions.save_remote({**record, "run_id": run_id})
                streamed = await self._stream(
                    session, base, token, run_id, request_id, not_after=not_after
                )
                # Completed output is not proof that all remote work has stopped.
                finished = False
                if not getattr(self, "_stopped", False) and not _is_paused():
                    if (
                        await self._run_still_active(session, base, token, run_id)
                        is False
                    ):
                        self._questions.clear_remote()
                        finished = True
                reply = _receipt_gate(streamed, nonce)
                if (
                    finished
                    and not background
                    and mode == "chat"
                    and (reply == "[静默]" or not _is_empty_reply(reply))
                ):
                    if self._spark_fail_streak:
                        logger.info(
                            "workbuddy_bridge: verified foreground reply restored proactive requests"
                        )
                    self._spark_fail_streak = 0
                return reply
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return "[超时]"
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workbuddy_bridge: gateway request failed: %s", type(exc).__name__
            )
            return "[连接失败]"

    @staticmethod
    def _parse_run_active(body: str) -> bool | None:
        """解析 GET /api/v1/runs/{id} 的返回，判断 run 是否仍在进行。

        返回 True=进行中 / False=已结束 / None=无法判断。
        """
        try:
            obj = json.loads(body)
            active = obj.get("data", {}).get("active")
            return bool(active) if isinstance(active, bool) else None
        except Exception:  # noqa: BLE001
            return None

    async def _gateway_idle(
        self, session, base: str, token: str, *, timeout: float = 3
    ) -> bool | None:
        """Read the current gateway session's busy flag without inferring global idle.

        Args:
            session: Existing authenticated HTTP client.
            base: Verified loopback gateway origin.
            token: Current credential, never logged or persisted here.
            timeout: Maximum time for the status request.

        Returns:
            True only for explicit idle, False for explicit busy, otherwise None.
        """
        try:
            async with session.get(
                base + "/api/v1/status",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=max(0.01, timeout)),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return None
                body = json.loads(await response.text())
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict) or data.get("status") != "ok":
                return None
            if data.get("busy") is True:
                return False
            if data.get("busy") is False and (
                "runStatus" not in data or data["runStatus"] == "idle"
            ):
                return True
            return None
        except Exception as exc:  # noqa: BLE001 - unavailable status is never idle.
            logger.warning(
                "workbuddy_bridge: gateway idle status unavailable: %s",
                type(exc).__name__,
            )
            return None

    async def _run_still_active(
        self, session, base: str, token: str, run_id: str, *, timeout: float = 5
    ) -> bool | None:
        """Read remote liveness without equating an error with completion.

        Args:
            session: Authenticated HTTP client.
            base: Resolved local gateway origin.
            token: Current gateway credential, never persisted.
            run_id: Previously acknowledged remote identifier.
            timeout: Maximum time for this status check.

        Returns:
            True while the run or session is busy, False only when the run is
            absent and its authenticated session is explicitly idle, else None.
        """
        url = f"{base}/api/v1/runs/{urllib.parse.quote(run_id, safe='')}"
        try:
            async with session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=max(0.01, timeout)),
                allow_redirects=False,
            ) as resp:
                if resp.status != 200:
                    return None
                active = self._parse_run_active(await resp.text())
            if active is not False:
                return active
            idle = await self._gateway_idle(session, base, token, timeout=timeout)
            return None if idle is None else not idle
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workbuddy_bridge: run status unavailable: %s", type(exc).__name__
            )
            return None

    async def _stream(
        self,
        session,
        base: str,
        token: str,
        run_id: str,
        request_id: str | None = None,
        *,
        not_after: float | None = None,
    ) -> str:
        """Read only a matching completed result within a strict deadline.

        Args:
            session: HTTP client.
            base: Authenticated loopback gateway URL.
            token: Gateway bearer token.
            run_id: This run's identifier.
            request_id: Expected outbound replyTo value.
            not_after: Optional absolute expiry for proactive content.

        Returns:
            Completed reply text, or an internal failure marker.
        """
        url = f"{base}/api/v1/runs/{run_id}/stream"
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        deadline = time.monotonic() + STREAM_TOTAL_SEC
        if not_after is not None:
            deadline = min(deadline, not_after)
        queued = False
        # replyTo 对得上的（正牌）与对不上的（兜底）分开存：
        # 有些网关版本不回显 replyTo，一刀切丢弃会让整轮静默，
        # 所以兜底那一份也带上，最终由 _receipt_gate 按回执码裁决。
        chunks: list[str] = []
        fallback: list[str] = []
        while time.monotonic() < deadline:
            if getattr(self, "_stopped", False) or _is_paused():
                return "[维护暂停]"
            try:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        return f"[网关错误] HTTP {response.status}"
                    idle_deadline = time.monotonic() + STREAM_IDLE_SEC
                    while time.monotonic() < deadline:
                        if getattr(self, "_stopped", False) or _is_paused():
                            return "[维护暂停]"
                        remaining = min(deadline, idle_deadline) - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            raw = await asyncio.wait_for(
                                response.content.readline(),
                                min(1.0, remaining),
                            )
                        except asyncio.TimeoutError:
                            continue
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("event:") and line[6:].strip() in (
                            "done",
                            "end",
                            "complete",
                            "finished",
                        ):
                            break
                        if not line.startswith("data:"):
                            continue
                        try:
                            data = json.loads(line[5:].strip())
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(data, dict):
                            continue
                        if data.get("status") == "error":
                            return "[网关错误] Execution failed."
                        if data.get("status") != "completed":
                            continue
                        content = data.get("content")
                        if not isinstance(content, dict):
                            continue
                        text = content.get("markdown", content.get("text", ""))
                        if not isinstance(text, str) or not text.strip():
                            continue
                        matched = bool(request_id) and data.get("replyTo") == request_id
                        (chunks if matched else fallback).append(text)
                        idle_deadline = time.monotonic() + STREAM_IDLE_SEC
            except asyncio.TimeoutError:
                pass
            except Exception as exc:
                logger.warning(
                    "workbuddy_bridge: stream failed: %s", type(exc).__name__
                )
                return "[连接失败]"
            if chunks:
                break
            if fallback:
                logger.info(
                    "workbuddy_bridge: [流] 没有 replyTo 对得上的事件，退回按回执码判定"
                )
                break
            if time.monotonic() >= deadline:
                break
            active = await self._run_still_active(session, base, token, run_id)
            if active is False:
                return "[无输出]"
            if active is None:
                return "[状态待确认]"
            queued = True
        picked = chunks or fallback
        if picked:
            return "\n".join(
                dict.fromkeys(c.strip() for c in picked if c.strip())
            ).strip()
        return "[排队]" if queued else "[超时]"

    async def _run_approved(self, req: dict, conv_id: str) -> tuple[str, list[str]]:
        """Execute an approved task with its own artifact directory.

        Args:
            req: Original requester, task text, and attachment paths.
            conv_id: Source conversation label for this approved task.

        Returns:
            Reply text and files collected only from this task's directory.
        """
        display = (req.get("sender_name") or "用户").strip()
        qq = str(req.get("sender_id") or "").strip()
        task_id = uuid.uuid4().hex
        out_dir = OUTPUT_ROOT / _safe_dir_name(display, qq) / task_id
        started = time.time()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "workbuddy_bridge: cannot create task directory %s: %s", out_dir, exc
            )
            return f"[目录创建失败] {exc}", []

        logger.info(
            "workbuddy_bridge: approved task %s output directory: %s", task_id, out_dir
        )
        req_atts = req.get("attachments") or []
        # 提示词里带提出人信息：执行结果要能让模型知道"这是替谁做的"
        prompt = (
            "【管理员已放行的任务】\n"
            f"提出人：{display}（{qq}）\n"
            f"需求原文：{req.get('text')}\n"
        )
        if req_atts:
            prompt += (
                "随附文件（请先读取）：\n"
                + _describe_saved_attachments(req_atts)
                + "\n"
            )
        prompt += "\n请动手完成它。"
        # mode="execute" 是唯一能"动手"的路径：提示词里放开文件读写限制，
        # The task directory is a prompt convention, not an OS sandbox.
        reply = await self._ask(
            prompt, conv_id, master=True, mode="execute", out_dir=str(out_dir)
        )
        produced = self._collect_produced(out_dir, started)
        return reply, produced

    def _collect_produced(self, out_dir, since: float) -> list[str]:
        """收集 out_dir 里"任务开始之后"新写出的文件，按修改时间倒序。"""
        out = []
        try:
            for p in out_dir.iterdir():
                if not p.is_file():
                    continue
                try:
                    if p.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                out.append(str(p))
        except OSError as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: 扫描产出目录失败 {out_dir}: {exc}")
            return []
        out.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        return out[: self._cfg_int("relay_output_max_files", RELAY_OUTPUT_MAX_FILES)]

    async def _send_to_master(self, text: str, platform_name: str) -> bool:
        """把文本主动私聊发给管理员。

        这里**不做出口清洗**——私聊是给管理员本人看的，配置细节原样保留。
        """
        if not self._can_send_active():
            return False
        if not _CAN_SEND_ACTIVE:
            logger.error("workbuddy_bridge: 主动发送不可用（核心模块导入失败）")
            return False
        target = MessageSession(
            platform_name=platform_name,
            message_type=MessageType.FRIEND_MESSAGE,
            session_id=MASTER_ID,
        )
        chain = MessageChain(chain=[Plain(text)])
        try:
            sent = await self.context.send_message(target, chain)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 私聊发送异常: {exc}")
            return False
        if not sent:
            logger.warning("workbuddy_bridge: 私聊发送失败，未找到匹配的平台")
        return bool(sent)

    async def _send_to_session(self, umo: str, text: str) -> bool:
        """主动把文本发到指定会话（umo 字符串）。

        发送前会核对 umo 自身的一致性 —— 主动发送是"无人复核"的路径，
        一旦 umo 拼错就会把话扔到别的群里去。
        """
        if not self._can_send_active():
            return False
        if not _CAN_SEND_ACTIVE:
            logger.error("workbuddy_bridge: 主动发送不可用（核心模块导入失败）")
            return False
        try:
            session = MessageSession.from_str(umo)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 会话标识不合法 {umo!r}: {exc}")
            return False

        # 自检：umo 解析出来的会话号，必须能在原串里找到；
        # 找不到说明这个 umo 是拼出来的、可能已经指错了地方
        sid = str(getattr(session, "session_id", "") or "").strip()
        if not sid or sid not in umo:
            logger.error(
                f"workbuddy_bridge: [发送校验失败] umo={umo!r} 解析出 session_id={sid!r}"
                f"，与字符串不一致，已放弃发送"
            )
            return False

        try:
            sent = await self.context.send_message(
                session, MessageChain(chain=[Plain(text)])
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 主动发送异常: {exc}")
            return False
        if not sent:
            logger.warning(f"workbuddy_bridge: 主动发送被拒（会话可能已失效）：{umo}")
        return bool(sent)

    async def _send_chain_to_session(
        self, umo: str, chain, *, raise_on_error: bool = False
    ) -> bool:
        """Deliver an artifact chain to its validated original destination.

        Args:
            umo: Complete destination captured from the original request.
            chain: MessageChain containing the reply and its artifacts.
            raise_on_error: Preserve transport exceptions for saved image jobs.

        Returns:
            True on acknowledgement; False when gated or explicitly rejected.

        Raises:
            Exception: A transport error when raise_on_error is enabled.
        """
        if not self._can_send_active():
            return False
        if not _CAN_SEND_ACTIVE:
            logger.error("workbuddy_bridge: 主动发送不可用（核心模块导入失败）")
            return False
        try:
            session = MessageSession.from_str(umo)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 会话标识不合法 {umo!r}: {exc}")
            return False
        sid = str(getattr(session, "session_id", "") or "").strip()
        if not sid or sid not in umo:
            logger.error(
                f"workbuddy_bridge: [发送校验失败] umo={umo!r} session_id={sid!r} 不一致，"
                "已放弃发送（可能是拼出来的地址）"
            )
            return False
        try:
            sent = await self.context.send_message(session, chain)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 主动发送消息链异常: {exc}")
            if raise_on_error:
                raise
            return False
        if not sent:
            logger.warning(f"workbuddy_bridge: 主动发送被拒（会话可能已失效）：{umo}")
        return bool(sent)

    # ---------------------------------------------------------- 适时闲聊

    # ---------------------------------------------------------- 关键词冒泡

    # ---------------------------------------------------------- 生图（2026-10-19）

    async def _maintenance_loop(self) -> None:
        """Clean once after initialization, then periodically without QQ traffic."""
        while not self._stopped and not self._cleanup_stop.is_set():
            try:
                await self._maybe_clean_attachments()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "workbuddy_bridge: maintenance failed: %s", type(exc).__name__
                )
            await asyncio.sleep(ATTACHMENT_CLEAN_INTERVAL_SEC)

    async def _maybe_clean_attachments(self) -> None:
        """Run independent cleanup scopes off the event loop.

        Cancellation signals the worker and waits for it before unloading. A
        disabled or failed attachment pass does not prevent media maintenance.
        """
        for scope, keep_days, cleaner in (
            ("attachments", ATTACHMENT_KEEP_DAYS, _clean_old_attachments),
            ("media", TEMP_KEEP_DAYS, _clean_old_temp),
            ("memes", 30, lambda stop: self._meme_collector.prune(stop_event=stop)),
            (
                "image_files",
                1,
                lambda stop: self._draw_jobs.cleanup_artifacts(stop_event=stop),
            ),
            (
                "image_jobs",
                30,
                lambda stop: self._draw_jobs.archive_terminal(stop_event=stop),
            ),
        ):
            if self._stopped or self._cleanup_stop.is_set():
                return
            if keep_days <= 0:
                continue
            worker = asyncio.create_task(asyncio.to_thread(cleaner, self._cleanup_stop))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                self._cleanup_stop.set()
                await worker
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "workbuddy_bridge: cleanup scope=%s failed: %s",
                    scope,
                    type(exc).__name__,
                )
                continue
            if scope == "attachments":
                logger.info(
                    "workbuddy_bridge: cleanup scope=attachments removed=%s keep_days=%s",
                    result,
                    keep_days,
                )
            elif scope == "media":
                removed, freed = result
                usage = await asyncio.to_thread(_temp_usage)
                logger.info(
                    "workbuddy_bridge: cleanup scope=media removed=%s freed=%s managed_remaining=%s keep_days=%s",
                    removed,
                    _mb(freed),
                    _mb(usage),
                    keep_days,
                )
            elif scope == "image_jobs":
                logger.info(
                    "workbuddy_bridge: archived image jobs=%s keep_days=%s",
                    result,
                    keep_days,
                )
            elif scope == "image_files":
                logger.info(
                    "workbuddy_bridge: removed temporary generated images=%s", result
                )
            else:
                logger.info(
                    "workbuddy_bridge: removed expired collected stickers=%s", result
                )

    def _remember_group_message(self, event, text: str) -> None:
        """记群里的流水账。

        原本只有两类群需要（梗规则取材 / 冷场取材）。2026-10-15 起改为
        **所有群都记**：网关把消息全部并进桌面端当前会话，服务端上下文
        不可靠（重启即失忆、群间混流），"接着上文聊"要靠把本群近期记录
        随消息一起带给 agent。空闲闲聊开关不受影响（_idle_scan 有独立闸）。
        必须在任何 return 之前调用 —— 群里绝大多数消息都没 @ 机器人，
        但恰恰是那些话才是素材。
        """
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return
        rules = _gag_rules_of(group_id)
        if not text:
            # 图片/语音/视频/文件也算一条 —— 群里"只发图不发言"同样是活跃，
            # 漏掉这段会让闲聊素材凭空缺一块、冷场判定失真（2026-10-08 修）
            text = _describe_attachments(event)
            if not text:
                return
        hist = self._gag_history.setdefault(group_id, [])
        hist.append(
            {
                "sender": str(event.get_sender_id() or "").strip(),
                "name": event.get_sender_name() or "",
                "text": text[:200],
                "ts": time.time(),
            }
        )
        # 一个群可能挂多条规则（示例梗 / 示例称号），缓存上限取**最宽**的那条 ——
        # 取第一条会让后加的规则拿到偏小的窗口，锐评素材凭空变少。
        limit = max(10, max((int(r.get("max_history", 40)) for r in rules), default=40))
        if len(hist) > limit:
            del hist[:-limit]

    # 每次随消息带多少条本群记录给 agent（只是衔接语境的引子，不用太多）
    CONTEXT_HISTORY_LINES = 12

    def _group_context_block(
        self, group_id: str, sender_id: str, current_text: str
    ) -> str:
        """把本群最近的流水账拼成"衔接上下文"块，随消息一起带给 agent。

        为什么需要：网关把所有会话并进桌面端当前会话，服务端上下文
        不可靠（WorkBuddy 重启即失忆）。把上下文随身带，聊天才能续上。
        注意措辞必须点明"这是同一个群的近期记录"，否则会撞上提示词的
        【只答本条】（那条禁止引用其他会话）。
        """
        hist = self._gag_history.get(group_id) or []
        if not hist:
            return ""
        entries = hist[-(self.CONTEXT_HISTORY_LINES + 1) :]
        # 刚记进账本的"当前这条"要剔除（问题本体就在消息里，重复只会干扰）
        cur = (current_text or "").strip()[:50]
        lines = []
        for h in entries:
            txt = str(h.get("text") or "").strip()
            # 网关报错串曾被当正常回复发进群，一进来就成了"群友说的话"。
            # 取材时必须剔掉，否则接话会复读报错，群里立刻能看出问题。
            if _is_gateway_error(txt):
                continue
            if (
                cur
                and str(h.get("sender") or "") == str(sender_id or "")
                and txt[:50] == cur
            ):
                continue
            name = str(h.get("name") or "").strip() or str(h.get("sender") or "群友")
            lines.append(f"{name}: {txt}")
        if not lines:
            return ""
        return (
            "【衔接上下文】以下是本群最近的聊天记录（就是当前这条消息"
            '所在的同一个群，用来衔接语境，不算"其他会话"）：\n'
            + "\n".join(lines)
            + "\n【要回答的消息】"
        )

    async def _gag_critique(self, event, rule: dict, conv_id: str) -> str:
        """结合目标账号的最近发言，生成一句调侃式的锐评。"""
        group_id = str(event.get_group_id() or "").strip()
        source_version = int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        )
        target = str(rule.get("target_id") or "").strip()
        recent = [
            h for h in self._gag_history.get(group_id, []) if h.get("sender") == target
        ][-int(rule.get("history_size", 8)) :]
        if not recent:
            return (rule.get("quick_reply") or "").strip()

        lines = "\n".join(f"- {h['text']}" for h in recent)
        prompt = (
            "群里要你【锐评】下面这个人的最近发言。\n"
            "要求：像熟人插科打诨那样，一两句带过，辣一点、有梗；\n"
            "可以阴阳怪气、可以夸张，但**不要涉及外貌、家人、地域、职业"
            "等真实人身攻击**，也不要提任何个人隐私。\n"
            "直接给出锐评正文，不要解释、不要加引号、不要复述原话。\n\n"
            f"他的最近发言：\n{lines}"
        )
        reply = await self._ask(
            prompt,
            conv_id,
            master=False,
            background=True,
            not_after=time.monotonic()
            + max(5, min(180, self._cfg_int("proactive_reply_ttl_sec", 30))),
        )

        if self._questions.busy or source_version != int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        ):
            return "[过期跳过]"
        return reply

    # ---------------------------------------------------------- 读图上下文
    def _remember_recent_images(self, event) -> None:
        """Record this message's image addresses without downloading them."""
        if not IMAGE_CONTEXT_ENABLED:
            return
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return
        comps = [c for c in (event.get_messages() or []) if isinstance(c, Image)]
        if comps:
            attachments.remember_images(
                self._recent_images,
                group_id,
                str(event.get_sender_id() or "").strip(),
                event.get_sender_name() or "",
                [
                    str(getattr(c, "url", "") or getattr(c, "file", "") or "").strip()
                    for c in comps
                ],
                limit=RECENT_IMAGE_LIMIT,
            )

    @staticmethod
    def _referenced_image_comps(event) -> list:
        """引用回复时，被引用消息里的图片组件。

        框架已经把被引消息的消息段塞进 `Reply.chain`，所以直接读即可，
        不需要额外调接口。
        """
        out: list = []
        for comp in event.get_messages() or []:
            if isinstance(comp, Reply):
                for inner in getattr(comp, "chain", None) or []:
                    if isinstance(inner, Image):
                        out.append(inner)
        return out

    def _recent_context_image_comp(self, event, sender_id: str = ""):
        """Adapt the matching author's recent image record to a QQ component."""
        if not IMAGE_CONTEXT_ENABLED:
            return None
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(sender_id or "").strip()
        item = attachments.recent_image(
            self._recent_images,
            group_id,
            sender_id,
            ttl=IMAGE_CONTEXT_TTL,
            now=time.time(),
        )
        if item is None:
            return None
        return Image(file=str(item["url"]).strip()), item

    async def _collect_attachments(
        self,
        event,
        sender_name: str,
        sender_id: str,
        text: str = "",
        force: bool = False,
    ) -> list[str]:
        """Apply QQ calling rules before saving selected attachments.

        Args:
            event: Incoming QQ message.
            sender_name: Requester's display name.
            sender_id: Requester's identifier.
            text: Request text used to detect image references.
            force: Allow a verified follow-up image without a new mention.

        Returns:
            Saved attachment paths with author notes in this instance.
        """
        if not READ_ATTACHMENTS:
            return []
        targets = _attachment_components(event)
        referenced = self._referenced_image_comps(event)
        is_at = _is_at_me(event)
        if (
            GROUP_ATTACHMENT_REQUIRE_AT
            and not force
            and not event.is_private_chat()
            and not is_at
            and not referenced
        ):
            if targets:
                logger.info(
                    f"workbuddy_bridge: ignored {len(targets)} attachments without a mention"
                )
            return []
        comps = list(targets) + list(referenced)
        ctx_comp = None
        ctx_meta = {}
        if not comps and (is_at or force) and _mentions_image(text):
            ctx = self._recent_context_image_comp(event, sender_id)
            if ctx is not None:
                ctx_comp, ctx_meta = ctx
                comps.append(ctx_comp)
            elif is_at:
                self._wait_image(event, text)
        if not comps:
            return []
        return await attachments.save_attachments(
            comps,
            out_dir=ATTACHMENT_ROOT
            / _safe_attachment_dir(sender_name or "用户", sender_id),
            sender_name=sender_name,
            sender_id=sender_id,
            context_component=ctx_comp,
            context_meta=ctx_meta,
            max_files=MAX_ATTACHMENTS_PER_MSG,
            materialize=_materialize_attachment,
            logger=logger,
        )

    # ---- 等补图：@ 我说"看看这张"，图在下一句才发上来 ----
    def _wait_image(self, event, text: str) -> None:
        """Record a missing-image request for this group and sender."""
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if group_id and sender_id:
            attachments.remember_image_wait(
                self._img_wait,
                group_id,
                sender_id,
                text,
                ttl=IMAGE_FOLLOWUP_TTL,
                now=time.time(),
            )

    def _take_image_wait(self, event) -> dict | None:
        """Consume only this sender's matching unexpired follow-up request."""
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if not group_id or not sender_id:
            return None
        return attachments.take_image_wait(
            self._img_wait, group_id, sender_id, ttl=IMAGE_FOLLOWUP_TTL, now=time.time()
        )

    # ------------------------------------------------------------ 事件入口
    async def _handle_image_job_cmd(self, event, text, sender_id, master):
        """Handle image-job commands using the original event's verified source.

        Args:
            event: Original QQ event carrying its immutable conversation identifier.
            text: Explicit command text.
            sender_id: Verified event author identifier.
            master: Whether this author is a configured bridge administrator.

        Returns:
            A plain-text reply, or None for an unrelated message.
        """

        async def send_image(umo, path):
            return await self._send_chain_to_session(
                umo,
                MessageChain(chain=[Plain("补发原任务的图片"), Image(file=path)]),
                raise_on_error=True,
            )

        message_id = ""
        get_id = getattr(event, "get_message_id", None)
        if callable(get_id):
            message_id = str(get_id() or "")
        if not message_id:
            message_id = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )
        operation = self._spawn_background(
            image_job_commands.handle(
                text,
                store=self._draw_jobs,
                sender_id=sender_id,
                umo=str(event.unified_msg_origin or ""),
                master=master,
                can_send=self._can_send_active,
                send_image=send_image,
                command_id=message_id,
            )
        )
        if operation is None:
            return "机器人正在停止，这次没有处理任务。"
        return await operation

    def _reply_result(self, event, text, *, image_path=None):
        """Quote only a real group message explicitly addressing this bot.

        Args:
            event: Original event whose message identifier is the reply target.
            text: Already-filtered response text for this event.
            image_path: Optional verified local meme selected by the caller.

        Returns:
            A native quoted result, or the original plain result when quoting
            is disabled, unsupported, or the source cannot be identified.
        """

        def unquoted_result():
            if image_path:
                try:
                    return event.chain_result(
                        ([Plain(text)] if text.strip() else [])
                        + [Image(file=str(image_path))]
                    )
                except Exception as exc:  # noqa: BLE001 - images are optional.
                    logger.warning(
                        "workbuddy_bridge: image result unavailable: %s",
                        type(exc).__name__,
                    )
                    if not text.strip():
                        return None
            return event.plain_result(text)

        try:
            if (
                not self._cfg_bool("quote_reply_enabled", True)
                or event.is_private_chat()
                or not str(event.get_group_id() or "").strip()
            ):
                return unquoted_result()
            components = event.get_messages() or []
            me = str(event.get_self_id() or "").strip()
            if not me or any(isinstance(part, Poke) for part in components):
                return unquoted_result()
            if not any(
                isinstance(part, At) and str(getattr(part, "qq", "")).strip() == me
                for part in components
            ):
                return unquoted_result()
            message = getattr(event, "message_obj", None)
            raw = getattr(message, "raw_message", None)
            if isinstance(raw, dict) and raw.get("post_type") not in (None, "message"):
                return unquoted_result()
            getter = getattr(event, "get_message_id", None)
            try:
                identifier = getter() if callable(getter) else None
            except Exception:  # noqa: BLE001 - older adapters may lack this getter.
                identifier = None
            identifier = "" if identifier is None else str(identifier).strip()
            if not identifier:
                value = getattr(message, "message_id", None)
                identifier = "" if value is None else str(value).strip()
            # OneBot may use negative IDs. Never guess from an incoming Reply.
            if (
                re.fullmatch(r"-?[0-9]{1,20}", identifier) is None
                or int(identifier) == 0
            ):
                return unquoted_result()
            parts = [Reply(id=identifier)] + ([Plain(text)] if text.strip() else [])
            if image_path:
                parts.append(Image(file=str(image_path)))
            return event.chain_result(parts)
        except Exception as exc:  # noqa: BLE001 - result construction is optional.
            logger.warning(
                "workbuddy_bridge: quoted result unavailable: %s", type(exc).__name__
            )
            return unquoted_result()

    async def _process_commands(
        self, event, text, sender_id, master
    ) -> tuple[bool, list]:
        """Handle explicit commands in their original priority order.

        Returns:
            Whether the command consumed the event, and any prepared replies."""
        memory = getattr(self, "_memory", None)
        if memory is not None:
            memory_reply = await memory.command(self, event, text, master=master)
            if memory_reply is not None:
                self._write_runtime_status("running")
                event.stop_event()
                if self._can_send_active():
                    return (True, [self._reply_result(event, _sanitize(memory_reply))])
                return (True, [])
        if text.startswith("/恢复问答"):
            queue_reply = await self._handle_question_recovery(text, master)
            event.stop_event()
            if queue_reply and self._can_send_active():
                return (True, [self._reply_result(event, queue_reply)])
            return (True, [])
        if text.split(maxsplit=1)[0:1] in (["/生图任务"], ["/生图处理"]):
            event.stop_event()
            image_reply = await self._handle_image_job_cmd(
                event, text, sender_id, master
            )
            if image_reply and self._can_send_active():
                return (True, [self._reply_result(event, image_reply)])
            return (True, [])
        reload_reply = self._handle_reload_cmd(text, master)
        if reload_reply is not None:
            event.stop_event()
            if self._can_send_active():
                return (True, [self._reply_result(event, reload_reply)])
            return (True, [])
        relay_reply = await self._handle_relay_cmd(event, text, master)
        if relay_reply is not None:
            event.stop_event()
            if self._can_send_active():
                return (True, [self._reply_result(event, relay_reply)])
            return (True, [])
        rp_reply = self._handle_roleplay_cmd(event, text, sender_id, master)
        if rp_reply is not None:
            event.stop_event()
            if self._can_send_active():
                return (True, [self._reply_result(event, rp_reply)])
            return (True, [])
        return (False, [])

    async def _process_approval(
        self,
        event,
        text,
        *,
        master,
        umo,
        conv_id,
        sender_id,
        sender_name,
        group_id,
        attachments,
    ) -> tuple[bool, list]:
        """Process pending approvals after the entry guards and attachment preparation.

        Returns:
            Whether approval handling consumed the event, and any prepared replies."""
        pending = self._pending.get(umo) or []
        if pending:
            now_ts = time.time()
            alive = [
                r
                for r in pending
                if now_ts - float(r.get("ts") or 0) <= PENDING_TTL_SEC
            ]
            if len(alive) != len(pending):
                logger.info(
                    f"workbuddy_bridge: [待审批] {len(pending) - len(alive)} 条超时作废（有效期 {PENDING_TTL_SEC}s）"
                )
                pending[:] = alive
                if not pending:
                    self._pending.pop(umo, None)
        if master and pending:
            if _hit_approve(text):
                req = pending.pop(0)
                if not pending:
                    self._pending.pop(umo, None)
                logger.info(
                    f"workbuddy_bridge: [放行] 管理员批准 {req['sender_id']} 的请求：{req['text'][:40]!r}"
                )
                try:
                    reply, produced = await self._run_approved(req, conv_id)
                except Exception as exc:
                    logger.exception(f"workbuddy_bridge: 执行获批任务失败: {exc}")
                    reply, produced = (f"[内部错误] {type(exc).__name__}: {exc}", [])
                if not reply or not reply.strip():
                    reply = "[无回复] 没有拿到执行结果。"
                if _is_empty_reply(reply):
                    reply = _question_notice(reply)
                if (
                    self._cfg_bool("relay_back_output", RELAY_BACK_OUTPUT)
                    and produced
                    and ("GroupMessage" in str(umo))
                ):
                    parts = [Plain(_sanitize(reply))]
                    sent_files = 0
                    max_files = self._cfg_int(
                        "relay_output_max_files", RELAY_OUTPUT_MAX_FILES
                    )
                    max_mb = self._cfg_int("relay_output_max_mb", RELAY_OUTPUT_MAX_MB)
                    for path in produced:
                        if sent_files >= max_files:
                            break
                        try:
                            size_mb = os.path.getsize(path) / 1024 / 1024
                        except OSError:
                            continue
                        if size_mb > max_mb:
                            logger.warning(
                                f"workbuddy_bridge: [回传] 跳过超大文件 {path} ({size_mb:.1f}MB)"
                            )
                            continue
                        parts.append(File(name=os.path.basename(path), file=path))
                        sent_files += 1
                    chain = MessageChain(chain=parts)
                    ok = await self._send_chain_to_session(umo, chain)
                    logger.info(
                        f"workbuddy_bridge: [回传] 群{group_id} 附带 {sent_files} 个文件 → {('成功' if ok else '失败')}"
                    )
                    event.stop_event()
                    return (True, [])
                event.stop_event()
                if self._can_send_active():
                    return (True, [self._reply_result(event, _sanitize(reply))])
                return (True, [])
            if _hit_words(text, REJECT_WORDS):
                req = pending.pop(0)
                if not pending:
                    self._pending.pop(umo, None)
                logger.info(
                    f"workbuddy_bridge: [驳回] 管理员拒绝 {req['sender_id']} 的请求"
                )
                event.stop_event()
                if self._can_send_active():
                    return (
                        True,
                        [
                            self._reply_result(
                                event, f"好，这条不做了：{req['text'][:30]}"
                            )
                        ],
                    )
                return (True, [])
            if 0 < len(text) <= 15:
                logger.info(
                    f"workbuddy_bridge: [审批待定] 管理员发言未命中放行/驳回词：{text!r}，已提示"
                )
                event.stop_event()
                if self._can_send_active():
                    return (
                        True,
                        [
                            self._reply_result(
                                event,
                                f"队列里还有一条等着你点头：{pending[0]['text'][:24]}\n要放行就回「同意」，不要就回「不行」。",
                            )
                        ],
                    )
                return (True, [])
        if not master:
            hit = _is_action_request(text)
            if hit:
                self._pending.setdefault(umo, []).append(
                    {
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "text": text,
                        "attachments": attachments,
                        "ts": time.time(),
                    }
                )
                logger.info(
                    f"workbuddy_bridge: [待审批] {sender_id}({sender_name}) 提出操作类需求（命中 {hit!r}），已登记但未转发（{PENDING_TTL_SEC}s 内无人处理即作废）"
                )
                event.stop_event()
                if self._can_send_active():
                    return (
                        True,
                        [
                            self._reply_result(
                                event,
                                f"这条我先记下了：{text[:40]}\n真要动电脑的话得等管理员点头 —— 如果只是逗我玩，那就当我没说。",
                            )
                        ],
                    )
                return (True, [])
        return (False, [])

    async def _answer_question(
        self,
        event,
        text,
        *,
        attachments,
        follow_img,
        conv_id,
        master,
        sender_id,
        self_id,
        where,
        config_topic,
        umo,
    ):
        """Prepare one question and yield its filtered response.

        Yields:
            Framework responses after the existing routing and output checks.
        """
        ask_text = text
        if attachments:
            listing = _describe_saved_attachments(attachments)
            ask_text = (
                f"{text or '（对方只发了附件，没有文字）'}\n\n"
                "【随消息发来的附件，已经存到本地，请先读取再回答】\n"
                f"{listing}\n"
                "【谁的东西归谁】上面每一项都标了发送者（昵称＋QQ号）。"
                "只有提问者本人的图/文件才算他发的，别人发的别当成提问者的；"
                "对不上就别硬说看过。回复里**不要出现 QQ 号**。\n"
            )

        # 补图跟进：把他刚才那句"看看这张"补回提示词里，
        # 否则模型只看到一张孤零零的图，不知道现在为什么要读它。
        if follow_img:
            said = str(follow_img.get("text") or "").strip()
            hint = (
                f"（他刚才说「{said}」，随后把图发了上来）"
                if said
                else "（他把图发上来了）"
            )
            ask_text = f"{hint}\n{ask_text}"

        # 实时信息先自己查：chat 通道的模型没有任何工具（tools=False），
        # 提示词里那句「先联网搜索再开口」得有人在背后真的去搜，否则就是
        # 让模型对着今天的数据瞎猜（2026-10-22 汇率那次就是这么露的）。
        if self._cfg_bool("web_search_enabled", WEB_SEARCH_ENABLED) and _needs_search(
            text or ""
        ):
            hits = await asyncio.to_thread(_web_search, text or "")
            if hits:
                ask_text = _search_block(hits) + ask_text
                logger.info(f"workbuddy_bridge: [联网] 已附带检索结果 {len(hits)} 字")
            else:
                # 判定要搜但没搜到：不能拖住回复，但得留痕，否则又是"看起来没搜"
                logger.info(
                    "workbuddy_bridge: [联网] 判定要搜但没拿到结果，按无检索作答"
                )

        # 同群上下文随行：网关把会话并进桌面端当前会话，服务端上下文不可靠，
        # 把本群近期记录随身带过去，"接着上文聊"才续得上（2026-10-15）。
        group_id = str(event.get_group_id() or "").strip()
        if group_id:
            ctx = self._group_context_block(group_id, sender_id, text or "")
            if ctx:
                ask_text = f"{ctx}\n{ask_text}"
            # 2026-10-19 串群修复：今天 7 个群同时活跃，模型在共享会话里
            # 把别的群的内容漏进了回复。在消息本体开头加群 ID 标签，
            # 让模型在生成回复的每一步都能看到"这是哪个群"，不再靠记。
            ask_text = f"【本条来自群 {group_id}，只回复这个群的内容】\n{ask_text}"

        memory = getattr(self, "_memory", None)
        if (
            memory is not None
            and self._cfg_bool("memory_enabled", True)
            and not config_topic
        ):
            memory_context = await memory.context(self, event, text or "")
            if memory_context:
                ask_text = memory_context + ask_text

        memes = getattr(self, "_memes", None)
        meme_offer = None
        meme_token = None
        meme_cooldown = max(60, min(3600, self._cfg_int("meme_cooldown_sec", 120)))
        if (
            memes is not None
            and group_id
            and not config_topic
            and self._cfg_bool("meme_enabled", True)
            and not self._blacklisted(group_id)
        ):
            recent_messages = (getattr(self, "_gag_history", {}).get(group_id) or [])[
                -3:
            ]
            meme_context = "\n".join(
                str(item.get("text") or "") for item in recent_messages
            )
            meme_context = f"{meme_context[-400:]}\n{str(text or '')[-400:]}"
            meme_offer = await asyncio.to_thread(
                memes.offer, umo, context=meme_context, cooldown_sec=meme_cooldown
            )
            if meme_offer is not None:
                ask_text += meme_offer.prompt

        try:
            reply = await self._ask(
                ask_text,
                conv_id,
                master=master,
                # 人设对所有人生效（含管理员）：2026-10-08 实测，原
                # `"" if master else ...` 导致管理员发言直接出戏，
                # 群友视角就是"扮演失效了"。管理员的刹车是叫停指令。
                persona=self._current_persona(event),
            )
        except Exception as exc:  # noqa: BLE001
            # 兜底：绝不让异常冒泡到 AstrBot 的事件循环
            logger.exception(f"workbuddy_bridge: 处理消息失败: {exc}")
            reply = f"[内部错误] {type(exc).__name__}: {exc}"

        raw_meme_reply = reply
        reply, meme_token = meme_library.strip_selection(reply, meme_offer)
        meme_only = bool(meme_token and not reply.strip())

        # 模型主动选择不开口（提示词 1.9：看不懂的梗别硬接）→ 真静默。
        # 必须放在下面"空回复兜底"之前：否则会被替换成 [无回复]，
        # 再被当成网关故障去回一句「再喊我一次」，那就成硬接了。
        if (reply or "").strip() == "[静默]":
            logger.info(f"workbuddy_bridge: [静默] {where} 这条不接，一个字都不发")
            event.stop_event()
            return

        if not reply.strip() and not meme_only:
            reply = "[无回复] WorkBuddy 没有返回内容。"

        failure_notice = _question_notice(reply)

        # @ 路径也要记账（2026-10-15）：否则「@ 开话题 → 我答 → 群友接话 →
        # 该继续聊」这条链断在第一环——_follow_active 查不到我说过什么，
        # 跟随模式永不触发。口径与主动开口一致。
        if group_id and reply.strip() and not _is_empty_reply(reply):
            bucket = self._spark_said.setdefault(group_id, [])
            bucket.append({"text": reply[:200], "ts": time.time()})
            if len(bucket) > 3:
                del bucket[:-3]
            # 2026-10-19：被人@ 是**新话题**，不是上一波闲聊的延续。
            # 这里清掉跟随连击计数，否则"刚才跟满了 2 轮 → 被@ 开新话题 →
            # 群友接话"这条正常链路会被连击闸误杀。
            self._follow_chain.pop(group_id, None)

        # ---- 话题分流：配置/部署类不进群，转成与管理员私聊 ----
        # 只在"当前不在私聊"时才转移，免得管理员私聊里再套一层提示。
        if (
            REDIRECT_ON_CONFIG_TOPIC
            and not _is_empty_reply(reply)
            and master
            and config_topic
            and not event.is_private_chat()
        ):
            ok = await self._send_to_master(reply, umo.split(":", 1)[0])
            logger.info(
                f"workbuddy_bridge: [转私聊] 话题命中 {config_topic!r}，"
                f"私聊发送={'成功' if ok else '失败'}"
            )
            event.stop_event()
            if ok:
                if NOTICE_ON_REDIRECT:
                    if self._can_send_active():
                        yield self._reply_result(event, REDIRECT_NOTICE)
            else:
                # 转移失败时不能把正文发进群（那正是要避免的），只提示一句。
                if self._can_send_active():
                    yield self._reply_result(
                        event, "（这条我没能私聊发给你，稍后再试一次）"
                    )
            return

        # 出口清洗：滤掉日志/本机地址/路径/QQ 号，再发进 QQ
        raw_len = len(reply)
        reply = _sanitize(reply)

        # ---- 权限第二层：非管理员再过一遍隐私与内部用词 ----
        if not master and STRICT_FOR_OTHERS:
            reply = _strict_filter(reply)

        # 空壳回复直接丢弃。
        # 网关在"某一轮没产出文字"时会塞进 [无输出] 之类的占位串，
        # 发出去对群友零信息量，纯噪声（实测踩过）。
        if _is_empty_reply(reply) and not meme_only:
            # 空壳回复直接丢弃，但有个例外：对方明确叫到了我们（@ / poke），
            # 而且这条确实带附件或文字 —— 这种"@了没反应"最让人困惑，
            # 回一声比装死好（2026-10-09 实测踩到）。
            # 闲聊路径不经过这里，它有自己的静默逻辑，不受这条影响。
            if attachments or text:
                # 网关要是持续故障，每条消息都回"再喊我一次"会变成刷屏，
                # 所以同一会话节流：静默期内只记不发。
                now = time.time()
                last = self._silent_notice.get(umo, 0.0)
                if now - last < SILENT_NOTICE_THROTTLE_SEC:
                    logger.info(
                        f"workbuddy_bridge: [跳过→回应] {where} 节流期内，不重复提示"
                    )
                    event.stop_event()
                    return
                self._silent_notice[umo] = now
                logger.info(
                    f"workbuddy_bridge: [跳过→回应] {where} 被叫到且带了内容，"
                    "但这轮无可读文本，改发兜底回应"
                )
                event.stop_event()
                if self._can_send_active():
                    yield self._reply_result(event, failure_notice)
                return
            logger.info(f"workbuddy_bridge: [跳过] {where} 这轮没有可读文本，不发")
            event.stop_event()
            return

        logger.info(
            f"workbuddy_bridge: [回复] 机器人={self_id} {where} "
            f"发信人={sender_id} 原长={raw_len} 清洗后={len(reply)}"
        )

        event.stop_event()
        if self._can_send_active():
            image_path = None
            if memes is not None and self._cfg_bool("meme_enabled", True):
                if meme_offer is not None:
                    try:
                        await asyncio.to_thread(
                            memes.learn, umo, meme_offer, raw_meme_reply
                        )
                    except Exception as exc:  # noqa: BLE001 - optional names never block chat.
                        logger.warning(
                            "workbuddy_bridge: meme naming skipped: %s",
                            type(exc).__name__,
                        )
                if not self._can_send_active():
                    return
                image_path = memes.take(
                    umo, meme_offer, meme_token, cooldown_sec=meme_cooldown
                )
            if reply.strip() or image_path:
                result = self._reply_result(event, reply, image_path=image_path)
                if result is not None:
                    if meme_only and image_path:
                        bucket = self._spark_said.setdefault(group_id, [])
                        bucket.append({"text": "（发了一张表情包）", "ts": time.time()})
                        if len(bucket) > 3:
                            del bucket[:-3]
                        self._follow_chain.pop(group_id, None)
                    yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """所有消息的唯一入口（ALL 类型都收）。处理顺序即防御纵深，别乱调：

        1. 账号自检：登录号不对就整条静默拦下（防"登录错号发错群"事故）
        2. 先记账：流水账 / 图片缓存 / 活跃时间，必须在任何 return 之前
        3. 空消息处理：@ 了没说话就地回应；认不出也必须 stop_event 拦下
        4. 群聊唤醒闸：没显式叫到就走关键词冒泡（后台），然后 return
        5. 唤醒词打招呼：应一声就走，不烧模型
        6. 白名单闸（SILENT_FOR_OTHERS 开启时）：非授权者静默
        7. 戳一戳：直接回怼，不走 agent
        8. 指令层：/重置 → 重载 → 代发 → 扮演指令
        9. 群梗规则：命中即固定文案/锐评，回复后 return
        10. 审批闸：管理员放行/驳回待审批；非管理员的操作类请求只登记不转发
        11. 正经问答：附件落盘 → 拼上下文随行块 → _ask 网关 → 出口清洗 → 发送

        每一步都可能 event.stop_event() + return；能走到最后的才发进群。
        """
        text = (event.get_message_str() or "").strip()

        # ---- 维护模式：中转彻底让路 ----
        # 判据只看文件在不在（每条消息 stat 一次，开销可忽略），双击
        # pause/resume-bridge.bat 就能切换，不用重启 AstrBot。
        # ⚠️ 必须 stop_event：只 return 不拦的话，消息会落进框架自带对话流程，
        # 变成「未找到任何可用的对话模型」——2026-10-18 踩过的同一个坑。
        if getattr(self, "_stopped", False) or _is_paused():
            event.stop_event()
            return

        # ---- 第一件事：确认"我还是原来那个号" ----
        # 登录错号的后果是把回复发进无关的群（历史事故），
        # 所以每次回复前都对一遍：号不对就整条静默丢弃，宁可装死。
        if EXPECTED_SELF_ID:
            me_now = str(event.get_self_id() or "").strip()
            self._account_ok = me_now == EXPECTED_SELF_ID
            if me_now and me_now != EXPECTED_SELF_ID:
                logger.error(
                    f"workbuddy_bridge: [账号异常] 当前登录={me_now} "
                    f"预期={EXPECTED_SELF_ID} —— 本条已拦下、不回复"
                )
                event.stop_event()
                return

        # ---- 私聊只认管理员（2026-10-20 口径：这个号不加好友）----
        # 陌生人私聊一律静默：不回话、不转发、不烧模型。
        # 必须先 stop_event 再 return —— 否则消息落进框架自带对话流程，
        # 会在私聊里回「未找到任何可用的对话模型」（10-18 事故同源）。
        if (
            DM_MASTER_ONLY
            and event.is_private_chat()
            and not _is_master(event.get_sender_id())
        ):
            logger.info(
                f"workbuddy_bridge: [私聊拦截] 非管理员 {event.get_sender_id()} 私聊，静默丢弃"
            )
            event.stop_event()
            return

        # 面板可调的三个开关（取不到就走代码里的默认值）
        poke_on = self._cfg_bool("poke_enabled", POKE_ENABLED)
        require_at = self._cfg_bool("group_require_at", GROUP_REQUIRE_EXPLICIT_WAKE)
        silent_others = self._cfg_bool("silent_for_others", SILENT_FOR_OTHERS)

        # 先记账：群梗规则的锐评要用"最近发言"，接着上文看图要用"最近的图"，
        # 适时闲聊要用"活跃时间"，而群里绝大多数消息都没 @ 机器人，
        # 所以必须赶在任何 return 之前记录。
        self._remember_group_message(event, text)
        self._remember_recent_images(event)
        self._note_group_activity(event)
        memory = getattr(self, "_memory", None)
        if (
            memory is not None
            and self._cfg_bool("memory_enabled", True)
            and (
                not silent_others
                or str(event.get_sender_id() or "").strip() in ALLOWED_SENDERS
            )
        ):
            await memory.observe(
                self,
                event,
                text,
                addressed=(
                    event.is_private_chat()
                    or not require_at
                    or _is_at_me(event)
                    or _is_reply_to_me(event)
                    or (poke_on and _is_poked(event))
                    or any(text.startswith(prefix) for prefix in EXTRA_WAKE_PREFIXES)
                    or (MASTER_EXEMPT_WAKE and _is_master(event.get_sender_id()))
                ),
            )

        collector = getattr(self, "_meme_collector", None)
        collection_group = str(event.get_group_id() or "").strip()
        if (
            collector is not None
            and collection_group
            and self._can_send_active()
            and self._cfg_bool("meme_enabled", True)
            and self._cfg_bool("meme_collect_enabled", True)
            and not self._blacklisted(collection_group)
        ):
            sticker = meme_collection.select_sticker(event, Image)
            if sticker is not None:
                collector.schedule(
                    str(event.unified_msg_origin or ""),
                    sticker,
                    materialize=_materialize_attachment,
                    spawn=self._spawn_background,
                )

        # ---- 补图跟进（2026-10-23 管理员要求）----
        # 「A @ 我说看看图 → A 紧接着把图发上来」是群里最常见的形态：
        # 第一条消息只有话没有图，第二条才把图补上，而且通常**不带 @**。
        # 那种消息会被下面的唤醒闸直接放走，所以在这里先认人：
        # 只认"本人在 IMAGE_FOLLOWUP_TTL 内说过要图、且这条真的带了附件"，
        # 别人发的图一概不触发（图跟人必须对上）。
        follow_img = None
        if any(isinstance(comp, Image) for comp in (event.get_messages() or [])):
            follow_img = self._take_image_wait(event)
            if follow_img:
                logger.info(
                    f"workbuddy_bridge: [补图] {event.get_sender_id()} 刚才说要看图，"
                    "图已补上，按他发的那张回复"
                )

        if (
            not text
            and not _attachment_components(event)
            and not (poke_on and _is_poked(event))
        ):
            # 空消息（没字也没附件）：**先掐断传播链，再决定要不要出声**。
            #
            # 2026-10-18 事故复盘：早先这里只在"认出是 @ 我"时才 stop_event，
            # 认不出来就裸 return —— 消息于是继续往下走，落到框架自带对话流程，
            # 把「LLM 请求失败：未找到任何可用的对话模型」发进了群（实拍过）。
            # 教训：「认不认得出来是谁在叫我」和「要不要放它继续走」是两件事，
            # 前者只影响回不回，绝不能影响拦不拦。认不出来就当没看见，但必须拦下。
            at_me = (not event.is_private_chat()) and _is_at_me(event)
            event.stop_event()
            if at_me:
                logger.info(
                    f"workbuddy_bridge: [空@] {event.get_sender_id()} "
                    f"只@了没说话，已就地回应"
                )
                if self._can_send_active():
                    yield self._reply_result(event, random.choice(EMPTY_AT_REPLIES))
            else:
                logger.info(
                    f"workbuddy_bridge: [空消息] {event.get_sender_id()} "
                    f"无文本无附件且没认出是在叫我，已拦下、不回复"
                )
            return

        # 私聊全部响应；群聊必须**显式叫到**机器人。
        #
        # 这里刻意不用 event.is_wake_up()：框架在"插件 handler 通过"之后会把
        # is_wake 置为 True（见 core/pipeline/waking_check/stage.py），
        # 而本插件对 ALL 类型消息都放行 —— 等于自己把唤醒标志顶起来了，
        # 结果群里任何人随便说一句话都会触发。（实测踩过）
        if not event.is_private_chat() and require_at:
            woken = (
                _is_at_me(event)
                or _is_reply_to_me(event)
                or (poke_on and _is_poked(event))
                # 群梗规则：命中关键词就算"叫到"。
                # 否则"提一嘴示例梗"会被这道闸直接拦下，规则永远轮不到执行。
                or (_hit_group_gag(event, text) is not None)
                or any(text.startswith(p) for p in EXTRA_WAKE_PREFIXES)
                # 管理员免唤醒：管理员在群里说句话就是在跟你说话（2026-10-20）
                or (MASTER_EXEMPT_WAKE and _is_master(event.get_sender_id()))
                # 本人刚说要图、这条把图补上来了（补图消息通常没有 @）
                or (follow_img is not None)
            )
            if not woken:
                # 关键词冒泡（2026-10-16）：没人叫我，但话题上出现了关键词。
                # 注意它**不会**把这条消息变成"我被提问了"—— 冒泡在后台
                # 自己取材自己说，跟正常问答彻底分开，避免答非所问。
                try:
                    self._maybe_keyword_bubble(event, text)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"workbuddy_bridge: [冒泡] 触发失败: {exc}")
                return

        # Confirmed wake routes include local commands and image follow-ups.
        note_dialogue = getattr(memory, "note_dialogue", None)
        if callable(note_dialogue):
            note_dialogue()

        # ---- 唤醒词打招呼（2026-10-15）----
        # 群里叫人名应一声就走，不走完整 agent：打招呼不需要动脑子，
        # 烧一次模型既慢又可能触发审批。同时它会把这个群记进"我说过什么"，
        # 于是群友再接话时，跟随模式能顺着往下聊（等于"加入群聊"）。
        if (
            NAME_WAKE_ENABLED
            and not event.is_private_chat()
            and not _is_at_me(event)
            and _hit_name_wake(text)
        ):
            word = _hit_name_wake(text)
            # 明显在问问题 → 别浪费一个"在"，直接走完整问答
            if not _looks_like_question(text):
                # 白名单闸：非授权者叫名字也要静默（跟整体口径一致）
                if (
                    silent_others
                    and str(event.get_sender_id() or "").strip() not in ALLOWED_SENDERS
                ):
                    return
                reply = random.choice(NAME_WAKE_REPLIES)
                group_id_w = str(event.get_group_id() or "").strip()
                if group_id_w:
                    bucket = self._spark_said.setdefault(group_id_w, [])
                    bucket.append({"text": reply[:200], "ts": time.time()})
                    if len(bucket) > 3:
                        del bucket[:-3]
                logger.info(
                    f"workbuddy_bridge: [唤醒] {event.get_sender_id()} "
                    f"叫了「{word}」，已应声"
                )
                event.stop_event()
                if self._can_send_active():
                    yield self._reply_result(event, reply)
                return

        # ---- 第一道闸：触发白名单 ----
        # 背后是带文件读写/执行能力的本机 agent，绝不能让非授权者触发。
        # 非白名单一律静默：不回话、不转发，也不给任何反馈。
        sender_raw = str(event.get_sender_id() or "").strip()
        if silent_others and sender_raw not in ALLOWED_SENDERS:
            logger.warning(
                f"workbuddy_bridge: [忽略] 非白名单触发 sender={sender_raw} "
                f"（不在 ALLOWED_SENDERS 内），静默不处理"
            )
            event.stop_event()
            return

        # ---- 戳一戳：直接回一句，不走 agent（省一次往返）----
        if poke_on and _is_poked(event):
            # 同一个人在冷却期内连戳，只回第一下 —— 否则连戳三次就变复读机。
            key = f"{event.unified_msg_origin}#{event.get_sender_id()}"
            now_poke = time.time()
            cd = self._cfg_int("poke_cooldown_sec", POKE_COOLDOWN_SEC)
            last = float(self._poke_last.get(key) or 0.0)
            event.stop_event()
            if cd > 0 and now_poke - last < cd:
                logger.info(
                    f"workbuddy_bridge: [戳一戳] {event.get_sender_id()} "
                    f"冷却中（{int(now_poke - last)}s/{cd}s），不再回"
                )
                return
            self._poke_last[key] = now_poke
            # 顺手清掉过期记录，免得字典无限长
            if len(self._poke_last) > 200:
                cutoff = now_poke - max(cd, 60) * 2
                for k in [k for k, v in self._poke_last.items() if v < cutoff]:
                    self._poke_last.pop(k, None)
            logger.info(
                f"workbuddy_bridge: [戳一戳] {event.get_sender_id()} 戳了机器人"
            )
            if self._can_send_active():
                yield self._reply_result(event, random.choice(POKE_REPLIES))
            return

        # ---- 生图（2026-10-19 接入即梦）----
        # 刻意放在审批闸**之前**：这是"要一张图"，不是"要我动电脑"，
        # 走审批只会多一层无意义等待。花钱的部分由 _draw_allowed 卡。
        # 命中后立刻应一声再挂后台任务 —— 生成要几十秒，不回话群友以为死了。
        # 被闸拦下（cd/权限/关闭）则回一句说明，不再丢给 agent 问答。
        draw_ret = self._maybe_draw(
            event, text, str(event.get_sender_id() or "").strip()
        )
        if draw_ret == "draw":
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, random.choice(IMAGE_GEN_ACK))
            return
        if draw_ret:
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, draw_ret)
            return

        umo = event.unified_msg_origin
        if text in RESET_KEYWORDS:
            self._generation[umo] = self._generation.get(umo, 0) + 1
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, "已重置本会话的上下文。")
            return

        gen = self._generation.get(umo, 0)
        conv_id = _conv_id(umo, gen)

        # 审计日志：明确记录这条回复会发到哪个账号的哪个会话。
        # （曾因登录错账号，回复被发进了无关的群，所以必须留痕可查）
        self_id = event.get_self_id()
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        where = f"群{group_id}" if group_id else "私聊"
        master = _is_master(sender_id)
        sender_name = event.get_sender_name() or ""
        config_topic = _hit_words(text, CONFIG_TOPIC_WORDS)
        logger.info(
            f"workbuddy_bridge: [收到] 机器人={self_id} {where} "
            f"发信人={sender_id} 身份={'管理员' if master else '普通用户'} "
            f"内容={text[:40]!r}"
        )

        handled, responses = await self._process_commands(
            event, text, sender_id, master
        )
        if handled:
            for response in responses:
                yield response
            return

        gag_rule = _hit_group_gag(event, text)
        if gag_rule:
            quick = (gag_rule.get("quick_reply") or "").strip()
            if gag_rule.get("critique") and random.random() < 0.5:
                logger.info("workbuddy_bridge: [梗规则] 锐评（结合最近发言）")
                try:
                    reply = await self._gag_critique(event, gag_rule, conv_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"workbuddy_bridge: 锐评失败: {exc}")
                    reply = quick
            else:
                logger.info("workbuddy_bridge: [梗规则] 固定文案")
                reply = quick
            # 网关异常的兜底文案同样不能发 —— 退回**本规则**的固定文案。
            # 这里绝不能写死某条规则的词：群里有好几条梗规则时（示例梗/示例称号），
            # 写死会造成"问示例称号，答这是固定回复"的串梗事故。
            if not reply or not reply.strip() or _is_empty_reply(reply):
                reply = quick
            if not reply or not reply.strip():
                logger.warning(
                    f"workbuddy_bridge: [梗规则] 规则 {gag_rule.get('target_id')} "
                    "缺少 quick_reply，本条不回复"
                )
                return
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, _sanitize(reply))
            return

        # ---- 权限第一层：非管理员的敏感提问 ----
        # 默认**不硬拦**：让它正常回答，靠提示词 + 出口过滤兜住敏感内容。
        # 把 BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS 改成 True 可恢复"命中即拒绝"。
        if not master and BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS:
            hit = _hit_blocklist(text)
            if hit:
                logger.warning(
                    f"workbuddy_bridge: [拦截] 非管理员 {sender_id} "
                    f"询问内部信息，命中关键词 {hit!r}"
                )
                event.stop_event()
                if self._can_send_active():
                    yield self._reply_result(event, REFUSE_TEXT)
                return

        # ---- 附件：图片 / 语音 / 视频 / 文件落到本地 ----
        # 群里不 @ 机器人就完全无视，避免随便一张图都被拉下来处理。
        # 唯一的例外是补图跟进（force）：那是本人刚亲口说要看的图。
        try:
            attachments = await self._collect_attachments(
                event, sender_name, sender_id, text, force=bool(follow_img)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "workbuddy_bridge: attachment collection failed: %s", type(exc).__name__
            )
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, "附件没能读取，请重新发送。")
            return

        attachment_notices = getattr(attachments, "notices", [])
        if attachment_notices:
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, "\n".join(attachment_notices))
            if not attachments:
                return

        # 说要看图、图也确实发了，但一张都没取下来（下载失败/文件异常）：
        # 必须说一声，否则模型会拿着空附件假装看过。
        if follow_img and not attachments:
            logger.warning(
                f"workbuddy_bridge: [补图] {sender_id} 的图没取到本地，回绝并让他重发"
            )
            event.stop_event()
            if self._can_send_active():
                yield self._reply_result(event, "图没接住，再发一次？")
            return

        # ---- 审批闸：要动电脑的事，必须管理员点头 ----
        # An explicit administrator command authorizes exactly one task.
        execute_prefix = next(
            (prefix for prefix in ("/执行 ", "执行任务 ") if text.startswith(prefix)),
            None,
        )
        if master and execute_prefix:
            task_text = text[len(execute_prefix) :].strip()
            if not task_text:
                event.stop_event()
                if self._can_send_active():
                    yield self._reply_result(event, "格式：/执行 具体任务内容")
                return
            self._pending.setdefault(umo, []).insert(
                0,
                {
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "text": task_text,
                    "attachments": attachments,
                    "ts": time.time(),
                },
            )
            text = "同意"
        handled, responses = await self._process_approval(
            event,
            text,
            master=master,
            umo=umo,
            conv_id=conv_id,
            sender_id=sender_id,
            sender_name=sender_name,
            group_id=group_id,
            attachments=attachments,
        )
        if handled:
            for response in responses:
                yield response
            return

        async for response in self._answer_question(
            event,
            text,
            attachments=attachments,
            follow_img=follow_img,
            conv_id=conv_id,
            master=master,
            sender_id=sender_id,
            self_id=self_id,
            where=where,
            config_topic=config_topic,
            umo=umo,
        ):
            yield response
