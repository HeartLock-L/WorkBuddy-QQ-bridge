# ==========================================================================
# 本文件为**可分发副本**：所有真实 QQ 号 / 群号 / 昵称已替换为占位值。
# 请按 部署指南.md 第 3.5 节填写 MASTER_ID 与 EXPECTED_SELF_ID 后使用。
# ==========================================================================
"""把 QQ 消息转交给本机 WorkBuddy，由 WorkBuddy 自带的模型作答。

链路：QQ -> NapCat -> AstrBot -> 本插件 -> CodeBuddy Gateway(HTTP) -> 回复

为什么走 Gateway 而不是 CLI：
WorkBuddy 桌面端在本机跑着一个 CodeBuddy Gateway（Web UI + REST API），
`GET /api/v1/auth/status` 返回 `{"authEnabled":true,"authenticated":true}`，
也就是说它**已经用你的 WorkBuddy 账号认证好了**。直接调用它即可用上自带的模型，
完全不需要第三方 API Key，也绕开了 CLI 凭据被 PAC 加密锁住的问题。

用到的接口：
    POST /api/v1/runs              发起 Agent 执行 -> { runId }
    GET  /api/v1/runs/{id}/stream  SSE 实时输出
    GET  /api/v1/auth/status       认证状态（用于自检）
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
import time
import uuid
from pathlib import Path

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
    Poke,
    Record,
    Reply,
    Video,
)

# 主动发消息需要的核心类（普通回复走 yield，不需要它们）；
# AstrBot 版本差异可能导不进，所以用 try 包住：导不进就关掉所有主动发送路径
#（代发 / 转私聊 / 主动开口），插件的被动应答仍可用。
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
def _detect_work_dir() -> Path:
    env = os.environ.get("QQ_BOT_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        return Path(__file__).resolve().parents[4]
    except Exception:  # noqa: BLE001 —— 反推失败就走默认值，绝不让插件起不来
        return Path(r"F:\workbuddy\QQbot")


WORK_DIR = _detect_work_dir()
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
PAUSE_FLAG = WORK_DIR / "bridge.paused"
# 维护模式自动失效（2026-10-20 R6）：flag 放太久说明是忘了恢复，
# 不能让它一直把机器人按死（群里只会看到"死了"）。超过这个时长按未暂停处理。
PAUSE_MAX_SEC = 8 * 3600


def _is_paused() -> bool:
    """维护模式是否生效。flag 文件 mtime 超过 PAUSE_MAX_SEC 视为过期。

    只用 mtime 不用内容：双击 bat 就会重写这个空文件，零格式负担。
    """
    try:
        age = time.time() - PAUSE_FLAG.stat().st_mtime
    except OSError:
        return False          # 文件不存在 / 没权限 → 当正常态
    return age <= PAUSE_MAX_SEC

DEFAULT_PORT = 62022
TIMEOUT_SEC = 300          # 单次回答总超时
STREAM_IDLE_SEC = 120      # SSE 连续无事件多久算结束
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
TEXT_KEYS = ("markdown", "text", "content", "delta", "message", "answer", "result", "output")


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
    token = (
        cfg.get("CODEBUDDY_GATEWAY_PASSWORD")
        or os.environ.get("CODEBUDDY_GATEWAY_PASSWORD", "")
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
            errors="replace",   # 控制台输出是 GBK，个别行可能带坏字符，容错解码
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
_GW_CACHE: dict[str, str | float | None] = {"base": None, "miss_at": 0.0}
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
    """动态定位 WorkBuddy Gateway。

    WorkBuddy 每次重启端口都会变（实测 62022 → 56083 → 50762 → 50864），
    所以绝不能把端口写死。依次尝试：上次成功的 → 配置里的 → 扫描本机所有端口。
    """
    candidates: list[int] = []
    cached = _GW_CACHE.get("base")
    if cached:
        try:
            candidates.append(int(str(cached).rsplit(":", 1)[1]))
        except (ValueError, IndexError):
            pass
    # **会话自带的端口最优先**（2026-10-15 实测）：插件跑在 AstrBot 进程里，
    # 拿不到本会话的 SERVER__PORT，所以拿"凭据文件 + 本机扫描"兜底。
    # 但一旦本会话在跑，它的端口就在本机监听列表里，扫描能覆盖到。
    preferred, _ = _cfg()
    if preferred and preferred not in candidates:
        candidates.append(preferred)

    # 刚全量扫过还没找到，就别再扫一遍（省掉最坏一分钟的串行超时），
    # 只用已知候选快速失败，等退避窗口过了再重新扫。
    last_miss = float(_GW_CACHE.get("miss_at") or 0.0)
    if (time.time() - last_miss) < GW_RESCAN_COOLDOWN_SEC:
        logger.info(
            f"workbuddy_bridge: 网关未找到，{int(GW_RESCAN_COOLDOWN_SEC - (time.time() - last_miss))}s 内不再全量扫描"
        )
        return None
    for port in await asyncio.to_thread(_local_ports):
        if port not in candidates:
            candidates.append(port)

    auth = {"Authorization": f"Bearer {token}"}
    for port in candidates:
        base = f"http://127.0.0.1:{port}"
        try:
            async with session.get(
                f"{base}/api/v1/auth/status",
                headers=auth,
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                body = await resp.text()
                if resp.status == 200 and _auth_ok(body):
                    if _GW_CACHE.get("base") != base:
                        logger.info(f"workbuddy_bridge: 已定位 Gateway -> {base}")
                    _GW_CACHE["base"] = base
                    _GW_CACHE["miss_at"] = 0.0   # 找到了就清掉退避标记
                    return base
        except Exception:  # noqa: BLE001
            continue

    _GW_CACHE["base"] = None
    # 记住"刚扫过但没找到"——端口换了不会在几秒内再换一次，
    # 没必要每次调用都全量扫 26 个端口（×4秒超时，最坏能拖一分钟）。
    # 退避 30 秒后再试，期间靠上面几个已知端口快速失败即可。
    _GW_CACHE["miss_at"] = time.time()
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


# --------------------------------------------------------------------------
# 输出清洗：回复会直接显示在 QQ 聊天窗口里，必须把"内部信息"挡掉
# --------------------------------------------------------------------------
FILTER_INTERNAL = True     # 关闭则原样透传（调试用）
MAX_REPLY_CHARS = 1500     # QQ 里过长的回复截断

# 运行时可被面板配置覆盖（见 _conf_schema.json 的 max_reply_chars / others_max_chars）。
# _sanitize 是模块级函数、拿不到 self，所以用这个可变字典做桥接。
_RUNTIME = {"max_chars": MAX_REPLY_CHARS, "others_max_chars": 1500}

# --------------------------------------------------------------------------
# 超长裁剪：按语义边界切，不在句子中间硬砍
# --------------------------------------------------------------------------
# 背景：直接 out[:N] 会在半句话中间断开，群里读起来像故障。
# 这里按"段落 → 句子 → 逗号 → 硬切"的优先级找落脚点，尽量切在完整语义上。
# 下限不是 100% —— 宁可稍微超一点，也不要切出"因为…"这种断头句。

_SENT_END = "。！？!?…"
_CLOSERS = "」』”\"')）】"


def _smart_trim(text: str, limit: int) -> tuple[str, bool]:
    """把 text 裁到 limit 字以内，返回 (裁剪结果, 是否发生了裁剪)。

    切点优先级：段落空行 > 句末标点 > 收尾引号/括号 > 逗号顿号 > 硬切。
    找不到落脚点就硬切，但会清掉可能残留的半个标点。
    """
    if limit <= 0 or len(text) <= limit:
        return text, False

    head = text[:limit]

    # 1) 段落：最后一段空行
    for sep in ("\n\n", "\n"):
        idx = head.rfind(sep)
        if idx >= limit * 0.5:
            return head[:idx].rstrip() + "\n…（太长了，后面还有）", True

    # 2) 句末标点，取最靠后的那个
    best = -1
    for ch in _SENT_END:
        idx = head.rfind(ch)
        if idx > best:
            best = idx
    if best >= limit * 0.5:
        end = best + 1
        # 把紧跟其后的收尾引号/括号带上，避免留下孤零零的半个引号
        while end < len(head) and head[end] in _CLOSERS:
            end += 1
        return head[:end].rstrip() + "\n…（太长了，后面还有）", True

    # 3) 逗号/顿号
    best = -1
    for ch in "，,、；;":
        idx = head.rfind(ch)
        if idx > best:
            best = idx
    if best >= limit * 0.6:
        return head[: best + 1].rstrip() + "…（太长了，后面还有）", True

    # 4) 兜底硬切：砍掉尾部可能残留的孤立标点
    tail = head.rstrip()
    while tail and tail[-1] in "，,、；;：:—-":
        tail = tail[:-1]
    return tail + "…（太长了，后面还有）", True


# --------------------------------------------------------------------------
# 循环节检测：模型不听话刷屏时的兜底硬闸
# --------------------------------------------------------------------------
# 背景：让人输出「1/3」「0.333…」「π」时，模型有时会真的往上刷几千位小数。
# 提示词已经劝过，但那是软约束；这里做**确定性检测**，一旦发现
# 同一段数字/字符连续重复超过阈值，就把那条循环段砍掉并换成一句说明。
#
# 判据：连续重复段长度 ≤ REPEAT_MAX_UNIT，且累计长度 ≥ REPEAT_MIN_SPAN。
# 覆盖两类：单字符循环（333…）与多字符循环（142857…、abcdef…）。

REPEAT_MAX_UNIT = 12        # 循环节最长多少字符（142857 六个，够用）
REPEAT_MIN_REPEATS = 4      # 至少重复几次才认定为循环
REPEAT_MIN_SPAN = 40       # 累计多少字符才动手（太短的巧合不误伤）


def _find_repeat_unit(text: str) -> tuple[str, int] | None:
    """在 text 里找最长的"连续重复单元"，返回 (单元, 重复次数)；没有则 None。

    只在数字与字母上找 —— 中文重复那是正常说话，不该拦。

    性能：命中一段循环后，该段内部的所有起点都不必再扫（结果只会更短），
    直接跳到段尾。否则一段 10 万字符的刷屏会退化成 O(n²)，卡死事件循环。
    """
    best: tuple[str, int] | None = None
    n = len(text)
    start = 0
    while start < n:
        ch = text[start]
        if not (ch.isdigit() or ("a" <= ch <= "z") or ("A" <= ch <= "Z")):
            start += 1
            continue
        # 默认只前进一格；只有真的匹配出重复（count ≥ 2）才允许跳到段尾，
        # 否则会给后面留出最多 11 个字符的"盲区起点"
        skip_to = start + 1
        for unit_len in range(1, REPEAT_MAX_UNIT + 1):
            unit = text[start : start + unit_len]
            if len(unit) < unit_len:
                break
            # 逐个比对后续单元，统计能连续重复多少次
            count = 1
            pos = start + unit_len
            while text[pos : pos + unit_len] == unit:
                count += 1
                pos += unit_len
            if count >= 2:
                skip_to = max(skip_to, pos)
            span = unit_len * count
            if count >= REPEAT_MIN_REPEATS and span >= REPEAT_MIN_SPAN:
                if best is None or span > best[1] * len(best[0]):
                    best = (unit, count)
                break  # 这个起点取最长的那个单元即可
        start = skip_to
    return best


def _kill_repeat_run(text: str) -> str:
    """把超长循环段替换成一句说明。只处理最长的那一处，避免乱改。"""
    hit = _find_repeat_unit(text)
    if not hit:
        return text
    unit, count = hit
    shown = unit if len(unit) <= 8 else unit[:8] + "…"
    return text.replace(unit * count, f"[循环节是 {shown}]", 1)


# 送给 agent 的前缀提示见下方 _prompt_prefix()（区分管理员/普通用户）

# ---- 出口过滤用的正则（全部在 _sanitize 里使用）----
# 每条正则对应一类"绝不该出现在 QQ 群里"的内容；误伤容忍度极低，
# 匹配模式都尽量收紧（带行首锚点 / 特定前缀），宁可漏不可错删。
_LOG_TAG_LINE = re.compile(   # 带 [Core]/[INFO] 等方括号标签的整行日志
    r"^\s*\[(?:Core|INFO|ERRO|ERROR|WARN|DEBUG|OneBot|AdapterManager"
    r"|workbuddy_bridge|napcat-qq|pipeline|respond\.stage)[^\]]*\]\s*.*$",
    re.M | re.I,
)
_LOG_TIME_LINE = re.compile(  # 以时间戳开头的整行日志（可选带前置标签）
    r"^\s*(?:\[[^\]]{0,40}\]\s*)?(?:\[\d{4}-\d{2}-\d{2}[^\]]*\]|\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*.*$",
    re.M,
)
_LEADIN_LINE = re.compile(r"^[^\n]{0,60}(?:日志|如下|印证|输出|结果)[^\n]{0,20}[：:]\s*$", re.M)  # 「日志如下：」这类引导行
_LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?\S*", re.I)      # 本机 URL 整体
_LOCAL_HOST = re.compile(r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d{2,5}\b", re.I)              # 主机:端口
_PORT_MENTION = re.compile(r"(?:端口|port)[^\d\n]{0,10}\d{2,5}", re.I)                            # 「端口 62022」字样
_ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s，。；、）)】\]\"'`]+")                                   # Windows 绝对路径
_QQ_NUM = re.compile(r"(?<!\d)\d{9,11}(?!\d)")                                                    # 9-11 位纯数字当 QQ 号
_EMPTY_FENCE = re.compile(r"```[^\n`]*\n[ \t\r\n]*```")                                           # 空代码块（洗完正文剩下的壳）
_BLANK_RUN = re.compile(r"\n{3,}")                                                                # 三连以上空行压成两连

# --- 排版清洗（2026-10-09）--------------------------------------------------
# QQ 是纯文本聊天窗口，不渲染 Markdown。模型却爱写 `**加粗**`、`- 列表`、
# `# 标题` 这类符号，发出去就是一坨裸字符，非常出戏。
#
# 提示词里已经加了第 1.5 条劝过，但那是**软约束**；这里做**确定性清洗**兜底，
# 保证即使模型忘了、也不会把星号喷到群里。
#
# 注意：只处理"成对/成行"的 Markdown 记号，避免误伤正常内容——
#   · 单个 `*` 不动（可能是乘法、脚注、颜文字）
#   · `**` 必须有配对才去掉（防误吃）
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_BOLD_LEFT = re.compile(r"\*\*(.+)$", re.MULTILINE)   # 落单的开标记
_MD_UL = re.compile(r"^[ \t]*[-*+][ \t]+(?=\S)", re.MULTILINE)  # 行首无序列表
_MD_OL = re.compile(r"^[ \t]*\d+\.[ \t]+(?=\S)", re.MULTILINE)  # 行首有序列表
_MD_HEAD = re.compile(r"^[ \t]*#{1,6}[ \t]+(?=\S)", re.MULTILINE)  # 标题
_MD_HR = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$", re.MULTILINE)  # 分隔线
_MD_QUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)  # 引用
_MD_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![*\w])")  # 单星斜体
_MD_TICK = re.compile(r"`([^`\n]+)`")  # 行内代码


def _strip_markdown(text: str) -> str:
    """把 Markdown 记号洗成纯文本 —— QQ 不渲染它们，留着只会难看。

    设计原则（**宁可少洗，不可洗错**）：
      1. 只洗"成对/成行"的记号；孤立的单字符原样保留
      2. 洗完不额外加装饰，保留原文的信息量
      3. 空行结构要留住 —— 那是自然分段，不是 Markdown
    """
    if not text:
        return text

    out = text

    # 1) 加粗：优先成对替换，剩下落单的开标记也去掉（结尾的星号交给后面）
    out = _MD_BOLD.sub(r"\1", out)
    out = out.replace("**", "")   # 处理完配对后，残留的一律清掉

    # 2) 斜体：只在明显成对时处理，避免误伤乘法 / 表情
    out = _MD_ITALIC.sub(r"\1", out)

    # 3) 行级记号：列表 / 标题 / 分隔线 / 引用 → 去掉标记，内容留下
    out = _MD_HR.sub("", out)
    out = _MD_HEAD.sub("", out)
    out = _MD_UL.sub("", out)
    out = _MD_OL.sub("", out)
    out = _MD_QUOTE.sub("", out)

    # 4) 行内代码反引号去掉（保留里面的字）
    out = _MD_TICK.sub(r"\1", out)

    # 5) 清理因删标记产生的行尾空格 / 多余空行
    out = re.sub(r"[ \t]+$", "", out, flags=re.MULTILINE)
    out = _BLANK_RUN.sub("\n\n", out)

    return out.strip()


def _sanitize(text: str) -> str:
    """清掉不该出现在聊天里的内容：日志行、本机地址端口、绝对路径、QQ 号。

    背景：本插件把消息转给 WorkBuddy 处理，agent 的输出常常夹带
    调试日志和本机信息。这些一旦发进 QQ 就很难收回，所以在出口统一过滤。
    """
    if not FILTER_INTERNAL or not text:
        return text

    # 顺序有讲究：先删整行（日志行/引导行），再做行内替换（地址/端口/路径/QQ号），
    # 最后清空代码块。反过来做的话，行内替换可能把日志行改得认不出来。
    out = _LOG_TAG_LINE.sub("", text)
    out = _LOG_TIME_LINE.sub("", out)
    out = _LEADIN_LINE.sub("", out)
    out = _LOCAL_URL.sub("[本机地址]", out)
    out = _LOCAL_HOST.sub("[本机端口]", out)
    out = _PORT_MENTION.sub("端口[已隐藏]", out)
    out = _ABS_PATH.sub("[路径]", out)
    out = _QQ_NUM.sub("[号码]", out)
    out = _EMPTY_FENCE.sub("", out)

    # 排版清洗：把 Markdown 记号洗掉（QQ 不渲染，留着是裸字符）
    # 放在敏感信息过滤之后、循环节兜底之前。
    out = _strip_markdown(out)

    out = _BLANK_RUN.sub("\n\n", out).strip()

    # 循环节兜底：模型刷屏刷不停时，把循环段换成一句说明
    out = _kill_repeat_run(out)

    # 过滤完几乎不剩字 → 说明整条都是内部信息，换成一句人话占位，
    # 免得把空消息发进群
    if len(out) < 4:
        out = "（这条回复只包含内部信息，已过滤）"
    # 长度裁剪走 _smart_trim 五级优先（段落>句末>引号>逗号>硬切），
    # 绝不用 out[:N] —— 那会在半句话中间断开，读起来像故障
    out, _cut = _smart_trim(out, _RUNTIME["max_chars"])
    return out


# --------------------------------------------------------------------------
# 第一道闸：触发白名单（最重要）
# --------------------------------------------------------------------------
# 背景：本插件把消息转给 WorkBuddy 本机 agent，而那个 agent 带文件读写与
# 命令执行能力。一旦群成员能触发，等于把本人电脑的操作权交给了群里任何人。
# 所以默认只放行本人，其余人一律静默——不回话、不转发、也不给"被拒绝"的反馈，
# 避免对方反复试探。
# 白名单门禁。放开聊天时设为 False —— 所有人都能触发，
# 而"操作类请求"仍会被审批闸拦住（见 ACTION_WORDS / APPROVE_WORDS）。
ALLOWED_SENDERS = ("0",)   # 仅在 SILENT_FOR_OTHERS=True 时生效
SILENT_FOR_OTHERS = False           # False = 所有人都能对话（当前设置）

# --------------------------------------------------------------------------
# 审批制：凡是"要动电脑"的事，必须管理员放行后才真正执行
# --------------------------------------------------------------------------
# 非管理员提出操作类需求时，插件只登记、不转发——本机 agent 一次都不碰。
# 管理员用下面的词放行（仅当存在待审批请求时才会被识别）。
APPROVE_WORDS = (
    "同意", "批准", "准了", "放行", "通过", "可以动手", "开始动手",
    "动手吧", "开干", "做吧", "执行吧", "就按这个做", "按这个做",
    # 下面几个是实测踩出来的：管理员说的那句话不在表里，
    # 结果没被当成放行，反而作为普通消息飘给了 agent。
    "可以执行", "执行吧", "可以做", "开始做", "准了", "行吧",
    "同意做", "批准执行", "让它做", "交给他做",
)
REJECT_WORDS = ("不行", "驳回", "拒绝", "别做", "算了", "停手", "不许")

# 否定前缀：放行词前若紧挨着这些字，整句意思就反了。
# 真实事故：管理员说「**不放行**是对的」，其中含子串"放行" → 被误判为批准，
# 于是执行了一个他明确不想执行的任务。判定时必须先剥掉否定。
NEGATION_PREFIXES = ("不", "别", "没", "勿", "甭", "无需", "不必", "先不", "暂不")

# 待审批请求的有效期：超过这个时间没人处理就作废。
# 免得半夜一句"同意"把几小时前的旧请求给放行了。
PENDING_TTL_SEC = 60

# 判定"是否涉及操作电脑"的词表。
#
# ⚠️ 原先只有一个 ACTION_WORDS，靠"有没有这个词"判定，误伤极多
# （2026-10-09 实测）：「帮我评估这套配装打多少伤害」这种纯咨询，
# 因为顶个"帮我"就被当成操作类请求拦下，必须管理员放行才能回。
# 根因是「帮我 / 给我 / 替我」这类客套话跟「删除 / 安装」被放在同一张表里，
# 可它们的危险程度差着数量级—— 前者每天都能出现一百遍，后者才会动本机。
#
# 现在拆成三层，从严到宽：
#   1. STRONG_ACTION_WORDS —— 明确要动本机，命中即拦，不给任何豁免
#   2. MACHINE_OBJECTS      —— 机器产物（文件/代码/图片…），单独出现也拦
#   3. CHAT_ONLY_WORDS      —— 咨询/分析/玩梗意图，可豁免上面两类

# 第1 层：明确操作本机。命中就直接走审批闸，任何豁免都不适用。
#
# 2026-10-20 瘦身（自检 R7 / 管理员选「甲」）：把 6 个泛动词移出本层——
#   生成 / 打开 / 改一下 / 写个 / 写点 / 做一
# 理由：这几个在日常聊天里天天出现（「生成这个词怎么念」「打开思路」
# 「做一次评估」「写个笑话」），而子串匹配之下它们会直接把咨询判成操作请求。
# ⚠️ 刻意**不放进第 2 层**：那一层的语义是"单独出现也拦"，放进去等于没改。
# 现在这类词要靠同现的机器产物（文件/代码/图片）才拦得住 ——
# 「生成一张图片」仍拦（命中"图片"），「生成这个词怎么读」放行。
STRONG_ACTION_WORDS = (
    "新建", "创建", "建立",
    "删除", "删掉", "清空", "清理", "移除",
    "安装", "装上", "卸载",
    "下载", "上传", "导出", "导入",
    "运行", "执行", "启动", "保存", "转换", "压缩", "解压",
    "重命名", "替换", "修改", "改成", "编辑",
    "整理", "归类", "归档", "扫描", "抓取", "爬虫",
    "写一个", "写份",
    "做个", "搞一个", "弄一个",
    "部署", "搭建", "配置一下", "设置一下",
    ".exe", ".bat", ".py", ".js", ".zip", ".pdf", ".docx", ".xlsx", ".pptx",
)

# 第 2 层：机器产物。与 STRONG 命中任一即拦；单独出现也拦（多带一个就够）。
MACHINE_OBJECTS = (
    "文件", "文件夹", "目录", "桌面", "电脑", "文档", "表格",
    "图片", "照片", "视频", "音频",
    "代码", "脚本", "程序", "软件",
    "压缩包", "截图",
    "word", "excel", "ppt", "pdf",
)

# 第 3 层：咨询 / 分析 / 玩梗意图。命中这些且没命中上面两层，
# 就按普通对话直接回答 —— 这类问题本来就不需要动本机。
#
# 「调侃」「玩梗」「认真你就输了」这类词也放进来：对方明确在开玩笑，
# 走审批闸只会把玩笑变成一板一眼的系统提示，反而更难看。
CHAT_ONLY_WORDS = (
    "评估", "评价", "锐评", "分析", "计算", "算一下", "算算",
    "对比", "比较", "哪个", "区别", "差异", "优缺点", "推荐", "建议",
    "原理", "为什么", "怎么玩", "怎么配", "配装", "加点", "技能",
    "伤害", "数值", "机制", "攻略", "科普", "讲解", "介绍", "科普一下",
    "什么意思", "什么梗", "懂吗", "是不是", "对不对", "行不行",
    "调侃", "玩笑", "开玩笑", "玩梗", "认真你就输了", "随便说",
    "帮我看", "看看", "看一下", "瞧",
    # 2026-10-19 实测误伤：「你觉得图片里的这个是你吗」命中"图片"，
    # 句子里却没有豁免词，被当成操作请求拦下还让等管理员点头。
    # 看图认人/认自己是纯问答，补一排问句式豁免词。
    "你觉得", "这是谁", "这是你", "是你吗", "是你不", "像你吗",
    "像不像你", "认一下", "认不认得", "认得", "长得像",
)

# 兼容旧名：保留给外部引用与自检脚本，实际判定走 _is_action_request。
ACTION_WORDS = STRONG_ACTION_WORDS + MACHINE_OBJECTS


def _is_action_request(text: str) -> str | None:
    """这条消息是不是"要动本机"。是则返回命中的词，否则 None。

    分层判定的用意见STRONG_ACTION_WORDS 上方的注释：
    宁可漏拦也不能误拦 —— 把「帮我评估配装」当成操作类请求，
    会让最普通的一句咨询都必须等管理员点头。
    """
    low = (text or "").lower()

    # 第 1 层：明确动本机，直接拦
    hit = _hit_words(text, STRONG_ACTION_WORDS)
    if hit:
        return hit

    # 第 3 层先看：有没有咨询/玩梗意图
    chatty = _hit_words(text, CHAT_ONLY_WORDS)

    # 第 2 层：机器产物。纯咨询里顺带提到"图片/代码"是常事
    #（比如「看看这段代码为什么报错」），有咨询意图就不拦。
    obj = _hit_words(text, MACHINE_OBJECTS)
    if obj:
        # 咨询意图与机器产物同现时，取咨询意图作为豁免依据
        return None if chatty else obj

    # 两层都没命中：客套话（帮我/给我/替我）已刻意从表里移除，
    # 它们每天出现一百遍，单独出现不构成"要动本机"。
    return None

# 获批执行的产物统一放到这里，按"提出需求的人"单独建目录
OUTPUT_ROOT = WORK_DIR / "用户产出"

# 审批任务完成后，把新产出的文件回传到"提出需求的那个群"。
# 传之前会核对目标会话是否就是当初提需求的地方 —— 发错群比不发更糟。
RELAY_BACK_OUTPUT = True
RELAY_OUTPUT_MAX_FILES = 5          # 最多回传几个（按修改时间倒序）
RELAY_OUTPUT_MAX_MB = 50            # 单个文件超过这么大就不发了

# --------------------------------------------------------------------------
# 话题分流：配置 / 部署类内容不进群，自动转成与管理员的私聊
# --------------------------------------------------------------------------
# 当前在群里时，若消息命中这些词且发信人是管理员，就把回复私聊给管理员，
# 群里只留一句轻提示（或完全静默）。其他话题一律在原处回复。
#
# 注意：这个词表与 HARD_BLOCK_WORDS 有意重叠——非管理员问到同一个词时，
# 走的是"硬拦截拒绝"，根本到不了这里。
CONFIG_TOPIC_WORDS = (
    "配置", "部署", "搭建", "安装", "设置", "参数", "环境变量", "依赖",
    "端口", "服务器", "后台", "管理面板", "控制台", "面板",
    "密钥", "令牌", "token", "apikey", "api key",
    "插件", "框架", "源码", "代码", "脚本", "数据库", "网关", "代理",
    "日志", "报错", "配置文件", "文件路径", "安装目录", "路径",
    "重启", "启动", "进程", "服务", "内网", "公网", "域名",
    "怎么搭", "怎么部署", "怎么配", "如何部署", "如何配置", "怎么实现",
)
REDIRECT_ON_CONFIG_TOPIC = True   # False = 关掉分流，一切在原处回复
NOTICE_ON_REDIRECT = True         # 群里是否留一句轻提示
REDIRECT_NOTICE = "（这部分我私聊你说）"

# --------------------------------------------------------------------------
# 附件读取：图片 / 语音 / 视频 / 文件
# --------------------------------------------------------------------------
READ_ATTACHMENTS = True              # 总开关
GROUP_ATTACHMENT_REQUIRE_AT = True   # 群里只有 @ 了机器人才读附件（不 @ 的直接无视）
GROUP_REQUIRE_EXPLICIT_WAKE = True   # 群里只认"显式叫到"：@ / @全体 / 引用机器人 / 唤醒前缀
EXTRA_WAKE_PREFIXES = ("/",)         # 除 @ 之外，以这些前缀开头也算叫到机器人

# --------------------------------------------------------------------------
# 戳一戳
# --------------------------------------------------------------------------
# 框架会把 QQ 的"戳一戳"转成消息链里的 Poke 组件，所以能检测。
# 处理方式：直接回一句，**不走 agent**——省一次往返，也不让它去调工具。
POKE_ENABLED = True
# 被戳后的冷却：同一个人在这么多秒内连戳，只回第一下。
# 2026-10-18 实测：有人 15 秒内戳了三次，插件回了三句（"戳什么戳"→"再戳一下试试？"
# →"手欠是吧"），单看有梗，连起来像复读机刷屏。冷却期内静默，过点再戳照旧会回。
POKE_COOLDOWN_SEC = 60
# 被戳时的回怼：戳一戳多半是在表达不耐烦，所以这儿也带点脾气。
# 想换回客气版，把下面几条替换掉即可。
POKE_REPLIES = (
    "戳什么戳，手闲啊？",
    "有事说事，别戳。",
    "再戳一下试试？",
    "……你戳够了没。",
    "行行行，我在，别动手动脚的。",
    "手欠是吧 😑",
)

# --------------------------------------------------------------------------
# 群专属梗规则
# --------------------------------------------------------------------------
# 群号 -> 规则（允许**一个群挂多条**：值可以是单条 dict，也可以是 dict 列表）。
# 命中关键词、或 @ 了指定账号时触发。
#   keywords     : 命中任一词即触发
#   exclude_keywords : 消息里出现这些词时**这条关键词不算数**（见下）
#   target_id    : 被 @ 到时触发（自动排除 @ 机器人自己）
#   quick_reply  : 固定文案
#   critique     : True = 与固定文案**随机二选一**；锐评会结合该账号的最近发言
#   history_size : 锐评时参考的最近发言条数
#   max_history  : 该群最多缓存多少条发言
# 一条消息同时命中多条规则时，按**列表顺序**取先命中的那条（不会连发两句）。
# 「示例梗」规则本体。想调整就改这一处，多个群共用同一份配置。
_DUCK_RULE = {
    "keywords": ("示例梗", "示例梗二"),
    "target_id": "1000000002",
    "quick_reply": "这是固定回复",
    # 锐评模块已按 2026-10-09 管理员要求关闭：命中「示例梗」只回固定文案。
    # 想恢复，把下面改回 True 即可（_gag_critique 与流水账都还在，没删）。
    "critique": False,
    "history_size": 8,
    "max_history": 40,
}

# 「示例称号」规则：示例梗规则的变体（2026-10-18 管理员要求）。
# 与示例梗的差异 —— 触发词是「示例词」「示例词二」（不是"示例称号"本身），对象换成群友
# 1000000003，固定文案是「示例称号......」（六个半角句点，管理员指定的原样，
# 别改成中文省略号）。与示例梗规则**同群并存**，互不覆盖；锐评同样关着。
_BEI_RULE = {
    # ⚠️ 触发词刻意**不含"示例称号"**：群里喊的是"示例词""示例词二"这两个叫法，
    # 拿称号当触发词反而会在别人正经提"示例称号"时乱接（2026-10-18 管理员纠正）。
    "keywords": ("示例词", "示例词二"),
    # 排除词：判定是"子串包含"，而「诺示例词」里就含「示例词」 —— 群里一聊诺奖，
    # 机器人会突然冒一句"示例称号......"，很难看。同理挡住几个专有名词。
    # 口径：宁可漏接一句，不可乱接一句。想放宽就删掉对应的词。
    "exclude_keywords": ("诺示例词", "示例词格莱德", "示例词金"),
    "target_id": "1000000003",
    "quick_reply": "示例称号......",
    "critique": False,
    "history_size": 8,
    "max_history": 40,
}

# 示例称号规则生效的群。默认跟示例梗规则同一批群（"变体"口径）。
# 只想在某几个群启用/停用，改这一行即可，不用动 _BEI_RULE 本体。
# 注意：留空 = 示例称号规则不生效；写进新群号 = 那个群也会挂上示例称号规则。
_BEI_GROUPS = (
    "2000000001",   # 示例群A
    "2000000002",    # 示例群B
    "2000000003",    # 2026-10-19 管理员点名加入（示例群C）
)

GROUP_GAG_RULES: dict[str, list[dict]] = {
    "2000000001": [dict(_DUCK_RULE)],   # 示例群A
    "2000000002": [dict(_DUCK_RULE)],    # 示例群B
    "2000000003": [dict(_DUCK_RULE)],    # 2026-10-19 新增
}
for _bei_gid in _BEI_GROUPS:
    GROUP_GAG_RULES.setdefault(_bei_gid, []).append(dict(_BEI_RULE))
del _bei_gid


def _gag_rules_of(group_id: str) -> tuple[dict, ...]:
    """取某个群的梗规则元组。

    兼容两种写法：值直接是 dict（历史写法，一个群一条），或是 dict 列表。
    统一成元组返回，调用方就不必到处判断类型。
    """
    raw = GROUP_GAG_RULES.get(str(group_id or "").strip())
    if not raw:
        return ()
    if isinstance(raw, dict):
        return (raw,)
    return tuple(r for r in raw if isinstance(r, dict))


# --------------------------------------------------------------------------
# 角色扮演（RP）：只对白名单里的群友开放
# --------------------------------------------------------------------------
# 默认白名单为空 —— 等于谁都不能玩。想放谁进来就把他的 QQ 号填进
# ROLEPLAY_ALLOWED，或在面板配置里填（推荐，改完不用重启）。
# 即使授权，模型侧仍有硬底线：不涉黄、不涉暴力违法、不借角色泄内部信息。
ROLEPLAY_ENABLED = True
# 默认对**所有群友**开放，不需要白名单。
# 关掉这个开关，才退回按 ROLEPLAY_ALLOWED 名单放行。
ROLEPLAY_OPEN_TO_ALL = True
ROLEPLAY_ALLOWED = ()
# 人设指令：用显式命令开启/结束，避免模型自作主张入戏。
#
# ⚠️ 这里刻意**以不带斜杠的写法为主**：AstrBot 默认把 "/" 当作 wake_prefix，
# 凡是 "/" 开头的消息会被框架先吃掉（插件收不到），所以 `/扮演 xxx`
# 在真机上从来没到达过插件 —— 实测踩过。带斜杠的形式仅作兼容保留。
ROLEPLAY_START_WORDS = ("扮演", "人设", "rp", "/扮演", "/人设", "/rp")
# 叫停词：任何人说都只停**当前群**；管理员的份量在于"说了必须算"
ROLEPLAY_STOP_WORDS = (
    "结束扮演", "出戏", "退出扮演", "停止扮演", "停演",
    "/结束扮演", "/出戏", "/退出扮演", "/停止扮演", "/停演",
)
# 多久没人接着聊就自动退场（秒）。每次该群有新消息会续期。
ROLEPLAY_TIMEOUT_SEC = 900
MAX_ATTACHMENTS_PER_MSG = 5          # 单条消息最多处理几个附件
ATTACHMENT_ROOT = WORK_DIR / "收到的附件"
ATTACHMENT_KEEP_DAYS = 2              # 附件保留天数，超期自动清理（<=0 表示不清理）
ATTACHMENT_CLEAN_INTERVAL_SEC = 3600  # 两次清理之间的最小间隔，避免每条消息都扫盘

# AstrBot 的媒体落地目录。每收一条带图/带表情的消息，这里就多一个文件，
# **AstrBot 自己从不清理**，而我们空闲闲聊每 30 秒就会现拉一次群历史
#（带图的消息全落盘），实测两天能堆到 849M / 875 个文件。所以自己兜底清。
#
# 删除是自动的，因此路径必须**逐层确认**：目录名要叫 temp、父目录链要真的是
# AstrBot/data，确认不了就宁可不删（返回 0），绝不在可疑路径上动手。
TEMP_ROOT = WORK_DIR / "AstrBot" / "data" / "temp"
# 保留 1 天就够（实测一天堆 400M+）：本插件的「接着上文看图」只缓存 URL、
# 有效期 IMAGE_CONTEXT_TTL=180秒，真要读图时框架会重新下载——这些文件
# 是纯缓存，不是唯一副本，删掉零影响。
TEMP_KEEP_DAYS = 1                     # temp 媒体缓存保留天数（<=0 表示不清理）
TEMP_CLEAN_MAX_FILES = 2000            # 单次最多删多少个，避免一次性上万次 unlink 卡住事件循环

# --------------------------------------------------------------------------
# 热重载：改完代码自动生效，不用再重启 AstrBot
# --------------------------------------------------------------------------
# 原理：后台协程每 HOT_RELOAD_INTERVAL 秒比对本文件 mtime；变了就调用
# 框架 PluginManager.reload("workbuddy_bridge")（面板同款接口）。
# reload 会先 terminate 旧实例再加载新代码 —— 所以真正的 reload 调用必须挂在
# 事件循环顶层（create_task），不能在 watcher 协程里 await，否则 watcher
# 被 terminate 取消时 reload 会断在半路（旧已卸、新未载）。
HOT_RELOAD_ENABLED = True             # 面板可关（hot_reload_enabled）
HOT_RELOAD_INTERVAL = 2               # 巡检间隔（秒）
HOT_RELOAD_DEBOUNCE = 1.5             # 检测到变化后再等这么久才动手（防编辑器多次写盘）
RELOAD_WORDS = ("重载插件", "重载")    # 管理员 QQ 指令（watcher 失灵时的手动兜底）

# --------------------------------------------------------------------------
# 读图上下文：让"接着说上文那张图"成为可能
# --------------------------------------------------------------------------
# 两条路：
#   ① 引用回复 —— 框架已把被引用消息的消息段放进 Reply.chain，直接读最精确
#   ② 上下文顺延 —— 群里刚有人发过图，随后 @ 机器人问"看看这张"，
#      就把最近那张捎上。**只缓存链接、不下载**，真正的下载发生在被 @ 的那一刻。
IMAGE_CONTEXT_ENABLED = True
IMAGE_CONTEXT_TTL = 180              # 多少秒以内的图才算"上下文"
RECENT_IMAGE_LIMIT = 5               # 每个群缓存多少个图片引用
IMAGE_HINT_WORDS = (
    "图", "这张", "那张", "上面", "看看", "描述", "识别",
    "这是啥", "这是什么", "什么玩意",
)

# --------------------------------------------------------------------------
# 适时闲聊：群里安静太久，主动接一句
# --------------------------------------------------------------------------
# 由插件加载时的后台巡检驱动（见 Main.initialize / _idle_loop）。
# 冷场判定：该群连续 IDLE_TRIGGER_SEC 秒没有任何消息。
# 无人应答或群友开了新话题 → 自然回到闲置（新消息会刷新计时）。
IDLE_CHAT_ENABLED = True
# 2026-10-19 管理员定调：**冷场闲聊是基本功能，所有群都开**，
# 不按群收窄。原 `IDLE_CHAT_GROUPS` 白名单因此是死配置（ALL_GROUPS=True 时
# 永远走不到），保留只为兼容面板上那个开关。
# 梗规则群的需求不是"开不开"，而是"更密" —— 见下面的 IDLE_HIFREQ_*。
IDLE_CHAT_ALL_GROUPS = True   # True = 所有群都开（以后被拉进新群也自动生效）
IDLE_CHAT_GROUPS = ("2000000001", "2000000002")  # 仅当上一行为 False 时才按这个白名单
# 2026-10-19 管理员新增「黑名单」：这个名单里的群**彻底不开腔**，
# 连跟随模式也不开 —— 白名单/总开关都管不住跟随，必须有单独一层。
# 起因：群 2000000004 明确抱怨「ai 接话太频繁，能不能关」，
# 而当时面板上的开关关了也没用（跟随路径绕过了 _idle_enabled_for）。
IDLE_CHAT_BLACKLIST = ("2000000004",)
IDLE_TRIGGER_SEC = 300        # 安静多久算冷场（5 分钟）
# 触发/冷却的硬底线下限（2026-10-20 管理员要求"最低 120s"）。
# 巡检间隔是 30s，若允许面板填 0，冷场就成了"每 30 秒每群说一句"。
IDLE_MIN_TRIGGER_SEC = 120
IDLE_CHECK_INTERVAL = 30      # 巡检间隔（秒）
IDLE_HISTORY_SIZE = 15        # 取材用的最近消息条数
IDLE_COOLDOWN_SEC = 1800      # 同一群两次主动开口的最小间隔（30 分钟）
# --------------------------------------------------------------------------
# 梗规则群提频（2026-10-19 管理员定调）
# 「冷场闲聊所有群都开，但梗群要更高频」—— 于是不是收窄开关，
# 而是对梗群用**更短的触发/冷却**。生效群同样取 GROUP_GAG_RULES（面板可覆盖），
# 不写死群号：梗规则增删群，提频自动跟上。
IDLE_HIFREQ_GROUPS = ()# 空 = 跟着群梗规则走
IDLE_HIFREQ_TRIGGER_SEC = 120     # 梗群：安静 2 分钟就算冷场（普通群 300s）
IDLE_HIFREQ_COOLDOWN_SEC = 600    # 梗群：两次开口至少隔 10 分钟（普通群 1800s）
# 取材时效：只用这么久以内的消息。否则会接上一个早就凉透的话题（实测踩过）
IDLE_SOURCE_TTL = 1800
# 全局节流：两次主动开口之间（不分群）至少隔这么久，避免"到处都在说话"
IDLE_GLOBAL_GAP_SEC = 180
# 2026-10-19 新增：跟随模式的"连续轮次上限"。
# 一波话题最多跟这么多次，之后必须冷场重新计时（或者被人@）才允许再开口。
# 起因：群 2000000004 抱怨「接话太频繁」—— 实测 12:56/13:03/13:05/13:06 十分钟里
# 跟了四轮，观感就是"每句都插嘴"。文字门槛（FOLLOW_MIN_TEXT_LEN）挡得住发图，
# 挡不住"群友正常聊天"，所以要一个跟轮数无关的硬上限。
FOLLOW_MAX_CHAIN = 2
# 跟到上限后，冷却这么久才重新允许跟随（不给它立刻反弹的机会）
FOLLOW_CHAIN_COOLDOWN_SEC = 900
# 梗群的跟随闸放宽（同"梗群提频"口径，2026-10-19）：
# 跟随模式收窄到梗群之后，一刀切的 2 轮/900s 恰好卡在"本该更活跃"的群上，
# 等于提频白提。这里给梗群单独一套更宽松的阈值。
FOLLOW_HIFREQ_MAX_CHAIN = 2
FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC = 900
# 梗群的跟随热窗口也更长：话题在延续就多跟几轮（普通群 180s）
FOLLOW_HIFREQ_WINDOW_SEC = 150
# 2026-10-19 管理员：「跟聊太频繁，没必要每句都跟」。
# 原来只有"连击次数"一个刹车，群友每说一句就刷新条件、立刻又被跟一次
# （实测 14:16:57 / 14:17:32 / 14:18:07 每 35 秒一句）。
# 补一个**两次跟随之间的最小间隔**：间隔没到就直接跳过这一轮，
# 等下一轮巡检再判断——效果就是"话题还在，但不逐句接"。
FOLLOW_MIN_GAP_SEC = 240
# 素材来源开关：冷场触发时**现拉**群历史（get_group_msg_history），
# 而不是依赖平时缓存的流水账 —— 素材永远是最新的，也不怕缓存被截断。
# 现拉失败会自动回退缓存（2026-10-08 管理员要求）。
IDLE_LIVE_HISTORY = True

# 最小取材门槛：少于这么多条，就别开口。
# （2026-10-09 管理员反馈"感觉没联系上下文" —— 实测出现过
#  "现拉失败 → 回退缓存只剩 1 条、还是张图"仍然发言的情况，
#  结果只能回"这张图单看有点接不上"，等于硬凑。素材太少直接闭嘴。）
IDLE_MIN_SOURCE_LINES = 3
# 且这 N 条里**至少要有一条带文字**：全是图片/表情等于没有可续的
# 话题，接不出上下文，不如不说。
IDLE_REQUIRE_TEXT = True
# 闲聊时允许读图：素材里有图就把最新那张下给agent 看（2026-10-15 管理员要求）。
# 只取最新一张，避免一开口下载一堆。
IDLE_ALLOW_IMAGE = True
# 主动发言熔断：连续这么多次网关异常就彻底静默，别再每 30 秒重试烧钱。
# 恢复方式：修好凭据（reconnect）后插件重载，计数归零。
SPARK_FAIL_CIRCUIT_N = 3

# --------------------------------------------------------------------------
# 生图模块（2026-10-19 接入即梦反代）
# --------------------------------------------------------------------------
# 直接 subprocess 调 `dreamina.exe`（即梦官方 CLI），**不经过那个 FastAPI 反代服务**。
# 原因：反代服务是给朋友浏览器用的，需要常驻进程 + 访问口令 + 配额库；
# 而本环境跑不了常驻服务。CLI 的登录态自己存在 ~/.dreamina_cli，调一次就行。
# 花钱的东西，默认只让管理员用，面板可以放开。
IMAGE_GEN_ENABLED = True
IMAGE_GEN_ADMIN_ONLY = False      # False = 所有群友都能用（2026-10-19 管理员放开）
# 即梦 CLI 路径：优先环境变量 QQ_BOT_DREAMINA；其次找 <项目根>\即梦反代\dreamina.exe
# （部署材料里放一个这个相对位置即可）；最后才退回本机历史绝对路径。
# 注意：装不了即梦 CLI 也不影响其余功能，生图请求会友好报错（IMAGE_GEN_ENABLED
# 改成 False 则连请求都不接）。
IMAGE_GEN_BIN = (
    os.environ.get("QQ_BOT_DREAMINA", "").strip()
    or (str(WORK_DIR / "即梦反代" / "dreamina.exe")
        if (WORK_DIR / "即梦反代" / "dreamina.exe").exists()
        else r"F:/workbuddy/即梦反代/dreamina.exe")
)
IMAGE_GEN_DIR = str(WORK_DIR / "用户产出" / "生图")
# 2026-10-19 管理员确认：5.0lite / 4.6 / 4.0 这三个模型**免费**，
# 所以每日上限直接取消（MAX_PER_DAY=0 即不限）；默认改用 5.0lite，
# 避免用 5.0 白烧付费额度。
# 注意：CLI 的 --help 里**没有列** 5.0lite，但实测能提交、能出图（2048×2048），
# 属于文档滞后，不是不支持。
IMAGE_GEN_MODEL = "5.0lite"       # 免费档：5.0lite / 4.6 / 4.0
IMAGE_GEN_RATIO = "16:9"          # 默认画幅（消息里写了比例就以消息为准）
IMAGE_GEN_RESOLUTION = "2k"
IMAGE_GEN_TIMEOUT = 180           # 单次生成超时（秒），CLI 轮询本身要 40~90s
# 读取时一律套 max(30, 值)：subprocess 的 timeout 填 0 会立刻抛 TimeoutExpired，
# 表现为"每次生图都失败且报超时"，比等久一点难查得多（2026-10-20 自检 R4）。
# 触发词：必须出现在**开头**，避免"画风""漫画"这类词误伤。
IMAGE_GEN_PREFIXES = (
    "画一张", "画一个", "画一只", "画个", "画张", "帮我画", "给我画", "画一画",
    "生图", "出图", "来张图", "生成一张", "生成图片",
)
# 量词尾巴（2026-10-20）：「帮我画张女仆装若纳瓦」剥掉触发词后剩
# 「张女仆装若纳瓦」——量词漏进了模型提示词（即梦标题都变成"张女仆装…"）。
# 纯动词触发词（帮我画/给我画/生图…）后面常跟着量词，要再剥一层；
# **本身带量词的触发词绝不能再剥**，否则「画一张张三」会被剥成「三」。
# 元组按"长在前"排（一张 优先于 张），命中即停、只剥一层
# （「帮我画一张张三」→ 剥「一张」→「张三」，正好对）。
_DRAW_QUANT_TAIL = ("一张", "一个", "一只", "一幅", "张", "个", "只", "幅")
_DRAW_PREFIX_HAS_QUANT = frozenset(
    ("画一张", "画一个", "画一只", "画个", "画张", "画一画", "来张图", "生成一张")
)
IMAGE_GEN_COOLDOWN_SEC = 5        # 同一群两次生图的最小间隔（2026-10-19 60→5，模型免费）
IMAGE_GEN_GLOBAL_GAP_SEC = 5      # 全局间隔，防多群同时挤爆 CLI（2026-10-19 20→5）
# 每群每天上限：0 = 不限（2026-10-19 起，因为默认模型走免费档）
IMAGE_GEN_MAX_PER_DAY = 0
# 允许的画幅（即梦 CLI 实际支持的全集）；消息里写别的比例一律不认
_IMG_RATIOS = ("21:9", "16:9", "3:2", "4:3", "1:1", "3:4", "2:3", "9:16")
# 出图前先应一声（生成要几十秒，不回话群友以为机器人死了）
IMAGE_GEN_ACK = (
    "画着呢，等会儿。",
    "在画了，别催。",
    "等着，马上出图。",
)


def _draw_ratio_of(text: str) -> str:
    """从消息里抠比例。认「比例16:9」「16:9」，也认「横图/竖图/方图」这类口语。

    返回合法比例串；认不出就返回空串（调用方用面板默认值）。
    必须**只认白名单内的比例** —— 群友写「下午3:2开黑」这种不该被当成画幅。
    """
    t = text or ""
    for w, r in (("横图", "16:9"), ("横版", "16:9"), ("竖图", "9:16"),
                 ("竖版", "9:16"), ("方图", "1:1"), ("正方", "1:1")):
        if w in t:
            return r
    for m in re.finditer(r"(\d{1,2})\s*[:：比]\s*(\d{1,2})", t):
        cand = f"{m.group(1)}:{m.group(2)}"
        if cand in _IMG_RATIOS:
            return cand
    return ""


def _draw_block_reply(reason: str) -> str:
    """被闸拦下时该回群友什么话。

    2026-10-19 管理员要求：cd 中不要沉默落回问答，先回一句"生图cd中"。
    冷却/全局间隔/日上限这三类统一回「生图cd中」，简短不啰嗦；
    权限类（仅管理员/已关闭）回原原因，因为那不是等一等能解决的。
    """
    if ("冷却" in reason) or ("全局间隔" in reason) or ("今日已" in reason):
        return "生图cd中"
    return reason


# 生成内容红线：涉及未成年人的性化请求一律不送模型（即梦审核只是兜底，
# 不能指望它）。词表故意保守——只收"无歧义指向未成年+性化"的组合，
# 宁可漏也不能拿误伤刷存在感；发现新变体就往里加。
DRAW_BLOCKED_WORDS = (
    "洛丽塔", "lolita", "萝莉", "幼女", "幼童", "儿童色",
    "未成年", "小学生色", "小孩子色", "kid", "child porn",
)

# 即梦 API 的 prompt 上限（2026-10-19 实测：2673 字符被拒，报 1600 上限）
DRAW_PROMPT_MAX_LEN = 1600


def _draw_prompt_of(text: str) -> str:
    """从消息里抠出"要画什么"。不是生图请求就返回空串。

    只有**开头**命中触发词才算，否则「这画风不错」「漫画看多了」都会被误判成
    要画图——那是群里最高频的日常对话，一误判就成了刷屏。
    「生图 / 出图」这两个词歧义最大（"生图模块好用吗"开头就是它），
    所以额外要求后面紧跟分隔符，不能直接接汉字。

    2026-10-19 补：**比例/画幅字样要从 prompt 里剥掉**。实测群友写
    「画一张图，比例16:9，写实摄影，一个开满鲜花的花园」，
    原来整串都塞给模型，于是模型照着"比例16:9"这几个字去画，
    真正的 ratio 参数却还是默认的 1:1 —— 出图自然不是 16:9。
    """
    t = (text or "").strip()
    if not t:
        return ""
    # 按最长前缀优先匹配，避免"画个"截走"画一张"
    for p in sorted(IMAGE_GEN_PREFIXES, key=len, reverse=True):
        if not t.startswith(p):
            continue
        rest = t[len(p):]
        # 「生图」「出图」本身是名词，后面不跟分隔符就多半是在聊这个东西
        if p in ("生图", "出图") and rest[:1] and rest[0] not in "：:，, \t　":
            continue
        rest = rest.lstrip("：:，, \t　")
        # 量词尾巴（2026-10-20）：纯动词触发词后挂着的"张/个/只"也要剥，
        # 「帮我画张女仆」不能让模型收到「张女仆」。带量词的触发词跳过
        # （「画一张」自己就是量词，再剥会把「画一张张三」毁成「三」）。
        if p not in _DRAW_PREFIX_HAS_QUANT:
            for q in _DRAW_QUANT_TAIL:
                if rest.startswith(q):
                    rest = rest[len(q):]
                    break
        # 剥掉画幅描述（"比例16:9" / "16:9" / "横图" 等），只留真正的画面内容
        rest = re.sub(r"比例\s*[:：]?\s*\d{1,2}\s*[:：比]\s*\d{1,2}", "", rest)
        rest = re.sub(r"\d{1,2}\s*[:：]\s*\d{1,2}", lambda m: "" if m.group(0).replace("：", ":") in _IMG_RATIOS else m.group(0), rest)
        for w in ("横图", "横版", "竖图", "竖版", "方图", "正方"):
            rest = rest.replace(w, "")
        rest = re.sub(r"[，,]{2,}", "，", rest)
        # 「画一张图，xxx」里的"图"是量词不是画面内容，剥掉免得模型去画一张"图"
        rest = re.sub(r"^图\s*[，,：:、]\s*", "", rest)
        # 剥掉画幅后常留下孤零零的"的"（"画一张 4:3 的猫" -> "  的猫"），一并清掉。
        # 注意前面可能还留着空格，所以要先吃掉空白再判"的/地"。
        rest = re.sub(r"^[\s　]*[的地]\s*", "", rest)
        rest = rest.strip("，,：: 　、")
        return rest if len(rest) >= 1 else ""
    return ""


# --------------------------------------------------------------------------
# 自画像专用提示词（2026-10-20 管理员要求）
# --------------------------------------------------------------------------
# 群友发「生成你的照片」「画一张你的自画像」这类**要"你"的图**的消息时，
# 不能把字面词（"你的照片"）当提示词喂给模型 —— 那会画出一堆无意义的东西。
# 正确行为：换成固定的拟人化形象提示词，然后走正常生图流程（三道闸、
# ACK、按人分框、MessageChain 回图全部照旧，就当是一次普通生图请求）。
DRAW_SELFIE_PROMPT = "模仿《魔法少女的魔女审判》的画风，画一张拟人化deepseek"
# 2026-10-20 管理员第二轮口径：底句是一切的前提**不能扔**，但每次可以在后面
# 追加一个"我喜欢的动作/拍摄手法"，同一个人反复要图不至于千篇一律；
# 一次只生成一张（--generate_num 1 维持不变）。池子全是本鱼日常
# （白饭/缸/睡姿）+ 常见镜头语言，命中后随机取一条拼在底句后面。
DRAW_SELFIE_VARIANTS = (
    "她正抱着一大碗堆成山的白饭大口往嘴里送，吃到眼睛都眯起来，平视机位",
    "低角度仰拍，她趴在巨大的玻璃缸边缘往外探头，怀里还搂着一个白瓷饭碗",
    "特写镜头，她举着空饭碗对镜头干杯，另一只手比耶，背景是虚化的水草",
    "侧逆光半身像，她盘腿坐在缸底，面前整齐摆着一排白饭碗，眼神无比认真",
    "俯拍视角，她躺在飘着气泡的水面上睡觉，怀里搂着饭勺，泡泡缓缓上浮",
    "她背对镜头回眸一笑，手里拎着小蒸笼，蒸汽缭绕，电影感宽幅构图",
    "微距特写，她用筷子夹起一颗饭粒对着光端详，像在品鉴宝石一样认真",
    "远景全身像，她站在米饭堆成的小山顶上张开双臂，晨光从侧面打过来",
    "她抱着饭锅原地转圈，米饭粒被甩出环形轨迹，快门凝格，动感十足",
    "夜晚水下灯光打在她侧脸，她抱着饭碗打瞌睡，头一点一点的，暖色调",
)
# 触发形态：前缀客套(帮我/能…) + 动词 + 可选量词 + "你的照片/自画像/你自己"类
# 宾语，尾巴只允许语气词。动词必须出现、结尾锚定 —— 「你的照片真好看」
# 这类闲聊绝不触发。命中优先级高于普通提示词解析（否则「生成一张你的照片」
# 会被 IMAGE_GEN_PREFIXES 先截走，prompt 变成字面的"你的照片"）。
_DRAW_SELFIE_RE = re.compile(
    r"^(?:帮我|给我|麻烦你|麻烦|请|能不能|能|可以|可不可以)?"
    r"(?:生成|画|发|来|拍|整|输出|绘制)"
    r"(?:一张|一个|张|个|幅)?"
    r"(?:你自己|你本人|你的(?:照片|自画像|画像|自拍|图片|靓照|美照|证件照|写真|样子|长相)照?儿?)"
    r"[的吧呢啊呀哈嘛哟吗么～~，,。.？?看看试试谢谢\s]*$"
)


def _draw_selfie_prompt_of(text: str) -> str:
    """是"要你的照片/自画像"类请求就返回自画像提示词，否则空串。

    2026-10-20 管理员口径：底句「模仿《魔法少女的魔女审判》的画风，
    画一张拟人化deepseek」**永远保留**，在此基础上随机追加一条动作/
    拍摄手法变体；一次只生成一张，其余流程（冷却/分框/回图）与普通
    生图完全一致。
    """
    t = (text or "").strip()
    if not t:
        return ""
    if not _DRAW_SELFIE_RE.match(t):
        return ""
    return DRAW_SELFIE_PROMPT + "，" + random.choice(DRAW_SELFIE_VARIANTS)

# --------------------------------------------------------------------------
# 关键词冒泡：群里聊到某个话题关键词时，没人 @ 也能凑一句
# --------------------------------------------------------------------------
# 与"被 @ 后回答"是**两回事**，别混：
#   被 @  = 对方在跟我说话，直接答，答完就结束；
#   本功能 = 对方在跟**群里其他人**说话，只是话题上出现了关键词，
#            我在旁边搭腔。此时**不能**把原文当提问直接答（答非所问），
#            只能取材后主动说一句，或者干脆闭嘴。
# 所以实现上不写进 woken（那是"被叫到"的闸），而是独立走后台任务。
#
# 默认生效的群**跟着群梗规则走**（GROUP_GAG_RULES 的键），因为那些群
# 本来就是"接得上话"的活跃群；关键字限在这里，比全群撒网省事得多。
KEYWORD_BUBBLE_ENABLED = True
KEYWORD_BUBBLE_GROUPS = ()          # 空 = 直接沿用 GROUP_GAG_RULES 的群
# 关键词：出现即算，不要求开头（群里常说"有人知道xx吗"）。
# 全部可在面板里改，改完不用重启。
KEYWORD_BUBBLE_WORDS = (
    "有没有人", "有人在吗", "求助", "求问", "求解", "请教",
    "有人懂", "有人知道", "谁知道", "什么意思", "什么梗", "这梗",
    "这个梗", "求解答", "在线吗",
)
# 同两次冒泡的最小间隔：太密会变成关键词播报机，刷屏比不接话更招人烦。
KEYWORD_BUBBLE_COOLDOWN_SEC = 600
# 两次冒泡的全局间隔（不分群）：防止多个群接连开腔，显得像串台。
KEYWORD_BUBBLE_GLOBAL_GAP_SEC = 60
# 每群每天上限：这条路径没@ 就说话，一天下来容易超预期，兜个底。
KEYWORD_BUBBLE_MAX_PER_DAY = 20
# 取材下限：比冷场宽松（已经有明确触发点了），但太孤单还是别插嘴。
KEYWORD_BUBBLE_MIN_SOURCE_LINES = 2

# --------------------------------------------------------------------------
# 权限锁定：只有 MASTER_ID 是管理员，其余人不得询问系统内部与隐私
# （第二道防线；经过上面第一道闸之后，普通用户基本走不到这里了）
# --------------------------------------------------------------------------
MASTER_ID = "0"     # 主人 / 管理员
STRICT_FOR_OTHERS = True     # 非管理员启用严格过滤
# 管理员在群里说话**不需要 @**。2026-10-20 曾置 True，当晚按管理员要求改回原本口径：
# 那条"指令被忽略"实测是桌面端窗口与 QQ 中转抢会话造成的（见 PAUSE_FLAG 注释），
# 与群消息唤醒无关；开着反而让管理员在群里随口说两句就被回一句。留开关备选。
MASTER_EXEMPT_WAKE = False
# 私聊只认管理员（2026-10-20 管理员口径：「这个号不添加好友，除了管理员」）。
# 非管理员私聊一律静默丢弃，既不回话也不转发——好友申请都不接，聊起来更没必要。
DM_MASTER_ONLY = True
# 非管理员的回复上限。之前设 800 太狠了 —— 群友问个游戏设定就被腰斩，
# 读起来像"话说一半"。QQ 单条消息能装下 1500+，这里跟管理员对齐到同一档。
OTHERS_MAX_CHARS = 1500

# 期望登录的机器人账号。留空 = 不校验。
# 历史上出过"登录错号、消息发进无关的群"的事故，所以每次回复前都对一遍：
# 号不对就整条拦下、不回复、并且报错，宁可装死也别乱发。
EXPECTED_SELF_ID = ""

# 管理员专用：代发消息到指定群。
# 用法：「代发 群号 内容」。用于需要机器人"主动出现在某个群"的场合 ——
# 比如验证重启是否生效，而自己又不方便在那个群说话。
RELAY_WORDS = ("代发", "传话", "去群说")

# 只 @ 了机器人、一个字都没打时的回应。
# 必须接管：否则这条消息会穿透到框架自带的对话流程，
# 而框架没配模型，会把"未找到任何可用的对话模型"直接甩进群（实测踩过）。
EMPTY_AT_REPLIES = (
    "叫我干嘛？",
    "在，说事儿。",
    "听着呢。",
    "怎么了？",
    "嗯？",
)

# --------------------------------------------------------------------------
# 唤醒词：叫人设相关的词就应一声（2026-10-15 管理员要求）
# 与「@ 机器人」的区别：@ 是完整问答（走agent），唤醒是**打招呼**——
# 应一声「我在」就走，绝不为了打个招呼去烧一次大模型。
# 触发词按群配置（面板可填），默认这些；匹配是"消息里出现即算"，不要求开头，
# 因为群里常说"示例梗你看这个"。
# --------------------------------------------------------------------------
NAME_WAKE_ENABLED = True
NAME_WAKE_WORDS = ("大肥鱼", "鲸鱼", "蓝色大肥鱼", "bluewhale")
# ⚠️ 「示例梗」「示例称号」**刻意都不在这里**：那是群梗规则（GROUP_GAG_RULES）里群友
# 自己的梗，只在配置了梗规则的群里生效，跟本体人设无关（2026-10-15 管理员纠正，
# 2026-10-18 新增示例称号规则时重申）。混进来会造成"它是我名字"的误认 ——
# 判定口径是"这个词是不是我的自称"，不是"这个词在群里出现得多不多"。
# 打招呼回应。带一句"有事说事"，顺带把话头递回去，不像客服。
NAME_WAKE_REPLIES = (
    "我在，干嘛",
    "在呢，啥事",
    "来了来了，说",
    "在，有话说",
    "哟，喊我呢",
)
# 命中唤醒词但同时在问问题 → 走完整问答而不是打招呼
NAME_WAKE_ASK_HINTS = ("?", "？", "吗", "呢", "怎么", "为什么", "啥", "谁", "哪")
# 打招呼也要记进"我说过什么"，好让后面的跟随模式接得上


# 被明确 @（或 poke）且确实带了附件/文字，但这轮网关没给出任何可读文本时，
# 用来回一句的兜底话。
#
# 背景（2026-10-09 实测）：有人 @ 机器人发图，附件已正常落盘，
# 但网关那一轮返回空壳，被 _is_empty_reply 判为噪声丢掉 —— 结果群里只
# 剩下"@了没反应"，对方完全不知道自己被听到了，比不回更糟。
# 这里保证"至少给一声回应"，至于图里是什么，交给下一轮。
ATTACHMENT_SILENT_REPLY = (
    "图收到了，但我这会儿没回上话，再艾特我一次？",
    "图我看到了，就是没接住话——再喊我一声试试。",
    "收到图了，刚刚走神了。再@我一次我就回。",
)

# 上面那句兜底回应的最小间隔（按会话各自计时）。
# 网关持续异常时若每条都回"再喊我一次"，群里会被这类提示刷屏，
# 那跟装死没区别 —— 所以同一会话 120 秒内只提示一次。
SILENT_NOTICE_THROTTLE_SEC = 120

# 非管理员问到内部话题时：False = 允许自由回答（靠提示词约束 + 出口过滤兜底）
#                              True  = 恢复"命中即拒绝"，一个字都不给
BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS = False

REFUSE_TEXT = "这个我回答不了——涉及系统内部信息，只有管理员可以询问。"

# 非管理员命中以下任一词 → 直接拒绝，不转给模型（硬拦截）
HARD_BLOCK_WORDS = (
    "密码", "密钥", "口令", "apikey", "api key", "api_key", "token", "secret",
    "后台", "管理面板", "webui", "dashboard",
    "服务器", "部署", "架构", "源码", "源代码", "怎么实现的", "谁做的",
    "日志", "配置文件", "配置在哪", "怎么配置",
    "端口", "localhost", "127.0.0.1",
    "文件路径", "安装目录", "硬盘", "操作系统",
    "什么模型", "哪个模型", "什么框架",
    "管理员", "主人", "你的账号", "你的qq",
)

# 非管理员回复中被整行清除的内部用词（注意：不含"管理员/主人"，
# 否则连"只有管理员可以授权"这种拒绝语都会被删掉）
STRICT_STRIP_WORDS = (
    "密码", "密钥", "token", "apikey", "api key", "api_key", "secret",
    "localhost", "127.0.0.1", "端口", "日志", "配置文件",
    "源码", "安装目录", "文件路径", "部署", "服务器地址",
)

# 非管理员回复的隐私脱敏正则（在 _strict_filter 里使用）：
# 邮箱 / 手机号 / IPv4 一律打码——模型可能从某处学到真实联系方式，不能外发。
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _is_master(sender_id) -> bool:
    """是不是管理员本人。全插件权限判定的唯一入口，只认 MASTER_ID 这一个号。"""
    return str(sender_id or "").strip() == MASTER_ID


def _hit_blocklist(text: str) -> str | None:
    """非管理员提问命中硬拦截词时，返回命中的词。"""
    return _hit_words(text, HARD_BLOCK_WORDS)


def _hit_words(text: str, words) -> str | None:
    """在 text 中查找 words 里第一个命中的词；没命中返回 None。"""
    low = (text or "").lower()
    for word in words:
        if word.lower() in low:
            return word
    return None


# 网关在某些情况下（比如某一轮没有产出文字）会塞进这类占位串。
# 它们对群友毫无信息量，发出去只是噪声，所以整条丢弃。
EMPTY_REPLY_MARKERS = (
    "[无输出]",
    "[无回复]",
    "Agent 执行完成但没有可读文本",
    "没有可读文本",
    # _ask 失败时的兜底文案 —— 只该进日志，绝不能当回复发进群
    # （2026-10-07 实测：凭据失效期间闲聊把"[凭据失效]…"当成接话发了两个群）
    # 2026-10-15 补：[超时]/[网关错误]/[连接失败]/[环境错误]/[未配置]/[内部错误]
    # ——同属兜底串，此前漏在清单外，网关卡住时会被当正常回复发进群。
    "[凭据失效]",
    "[网关不可达]",
    "[超时]",
    "[网关错误]",
    "[连接失败]",
    "[环境错误]",
    "[未配置]",
    "[内部错误]",
    # [排队]：桌面端会话正忙、本轮没轮到执行。同样是空壳，绝不能发进群
    # （2026-10-18 新增，与 [无输出] 分开标记以便熔断器区分排队 vs 故障）。
    "[排队]",
)

# 模型偶尔会把"给系统看的备注"写进输出，形如「（本轮无合适接话点：……不发言。）」。
# 这类"元话术"发进群 = 内部黑话泄漏 + 暴露判断逻辑，绝不能发。
# 判据保守取两端（特定开头 + 特定关键词），宁可漏拦、不可误伤正常回复。
# （2026-10-14 实测：三条此类备注被当"接话"发进三个群，现由 _is_meta_note 拦截）
META_NOTE_PREFIXES = (
    "（本轮", "(本轮",
    "（这条", "(这条",
    "（素材", "(素材",
    "（无合适", "(无合适",
)
META_NOTE_MARKERS = ("不发言", "跳过", "不接", "不宜参与", "不宜接")


def _is_meta_note(text: str) -> bool:
    """是不是"给系统看的备注"（元话术）——这类内容绝不能发进群。"""
    t = (text or "").strip()
    if not t or len(t) > 200:
        return False
    # 2026-10-16 管理员要求：「（不回复）」这类占位备注一律拦截（宁可静默，也不发占位话术）。
    if t.startswith(("（不回复", "(不回复", "【不回复")):
        return True
    if not t.startswith(META_NOTE_PREFIXES):
        return False
    return any(marker in t for marker in META_NOTE_MARKERS)


def _is_empty_reply(text: str) -> bool:
    """这条回复是不是"空壳"（没有真正内容，或只是给系统的备注）。"""
    t = (text or "").strip()
    if not t:
        return True
    if any(marker in t for marker in EMPTY_REPLY_MARKERS):
        return True
    if _is_gateway_error(t):
        return True
    return _is_meta_note(t)


# 网关侧的**裸文本错误串**：不套方括号，直接就是一句英文报错。
# 2026-10-21 实踩：WorkBuddy 使用量被限流时，`Error: 429 您的使用量已超出频率
# 限制，将在 2026-10-22 13:52:09 UTC+8 重置…` 被当成正常回复发了 5 个群，
# 闲聊路径还把它当"群友说的话"复读了一遍。EMPTY_REPLY_MARKERS 只认方括号
# 兜底串，这类裸串一个都匹配不上，必须单独判。
_GATEWAY_ERROR_RE = re.compile(
    r"^\s*Error\s*[:：]\s*"
    r"(\d{3}\b|429|500|502|503|504)"          # Error: 429 / Error: 500
    r"|^\s*Error\s*[:：]\s*您的使用量"          # Error: 您的使用量已超出频率限制
    r"|^\s*Error\s*[:：]"                       # 其余任何 Error: 开头一律当故障
    r"|^\s*(?:\[?\d{3}\]?\s*)?(?:Internal Server Error|Bad Gateway|Service Unavailable)"
)


def _is_gateway_error(text: str) -> bool:
    """这条回复是不是网关/上游抛出来的裸错误串（绝不能发进群）。"""
    return bool(_GATEWAY_ERROR_RE.match(text or ""))


def _hit_approve(text: str) -> str | None:
    """匹配放行词，但**排除被否定的情形**。

    "不放行""别放行""暂不批准" 都不算批准。只做子串匹配会把语义整个搞反，
    这是真出过事故的地方。
    """
    low = (text or "").lower()
    for word in APPROVE_WORDS:
        start = 0
        while True:
            idx = low.find(word.lower(), start)
            if idx < 0:
                break
            prev = low[max(0, idx - 3) : idx]
            if not any(prev.endswith(p) for p in NEGATION_PREFIXES):
                return word
            start = idx + 1
    return None


def _conv_id(umo: str, generation: int) -> str:
    """生成交给 agent 的会话标识。

    必须用 md5 而不能用内置 hash()：后者带**每进程随机盐**（PYTHONHASHSEED），
    框架重启后同一个会话会算出不同的值，agent 那边就当成新会话，
    历史上下文凭空消失——这正是"聊着聊着突然失忆"的根因。

    同一 (会话, 代次) 恒定；`/重置` 会令 generation+1，从而换到新会话。
    """
    digest = hashlib.md5(f"{umo}|{generation}".encode("utf-8")).hexdigest()
    return f"qq-{digest[:12]}"


def _has_textual_material(hist: list[dict]) -> bool:
    """取材里是否至少有一条真正的文字消息。

    纯图片/语音的占位（「（图片）」「（2张图片）」一类）不算 —— 那种
    "接话"接不出上下文，只会像复读机似的点评图片本身。
    """
    for h in hist or []:
        t = str(h.get("text") or "").strip()
        if not t:
            continue
        # 占位文本形如「（图片）」「（2张图片、语音）」——整条被括号包住即视为无文字
        if t.startswith("（") and t.endswith("）"):
            continue
        return True
    return False


def _safe_name(name: str, fallback: str = "用户") -> str:
    """把 QQ 昵称清理成合法目录名片段。"""
    clean = re.sub(r'[\\/:*?"<>|\r\n\t]', "", name or "")
    clean = re.sub(r"\s+", " ", clean).strip().strip(".")
    return clean[:30] or fallback


def _safe_dir_name(name: str, qq: str) -> str:
    """产出目录名：「昵称（QQ号）的文件」。"""
    return f"{_safe_name(name)}（{qq}）的文件"


def _safe_attachment_dir(name: str, qq: str) -> str:
    """附件目录名：「昵称（QQ号）」。"""
    return f"{_safe_name(name)}（{qq}）"


def _is_at_me(event) -> bool:
    """消息链里是否 @ 了机器人本人（@全体也算）。"""
    me = str(event.get_self_id() or "").strip()
    for comp in event.get_messages() or []:
        if isinstance(comp, At):
            qq = str(getattr(comp, "qq", "") or "").strip()
            if qq == "all" or (me and qq == me):
                return True
    return False


def _is_reply_to_me(event) -> bool:
    """消息是否引用了机器人的发言。"""
    me = str(event.get_self_id() or "").strip()
    if not me:
        return False
    for comp in event.get_messages() or []:
        if isinstance(comp, Reply):
            if str(getattr(comp, "sender_id", "") or "").strip() == me:
                return True
    return False


def _is_poked(event) -> bool:
    """消息链里是否有"戳一戳"，且戳的是机器人本人。

    框架会把 QQ 的 poke 通知转成 Poke 组件（见 aiocqhttp_platform_adapter），
    `Poke.id` 即被戳对象的 QQ 号。取不到 id 时按"戳的就是我"处理。
    """
    me = str(event.get_self_id() or "").strip()
    for comp in event.get_messages() or []:
        if isinstance(comp, Poke):
            target = str(getattr(comp, "id", "") or "").strip()
            if not target or not me or target == me:
                return True
    return False


def _hit_group_gag(event, text: str) -> dict | None:
    """命中「群专属梗规则」则返回该规则，否则 None。

    两个触发条件：消息里出现配置的关键词，或者 @ 了配置的目标账号。
    （会排除 @ 机器人自己，免得自触发。）

    一个群可以挂多条规则（示例梗 / 示例称号并存）。判定分两轮：
    先把所有规则的**关键词**扫完，再看 @ —— 因为"说了示例称号"比"@ 了谁"
    更像明确的意图，谁的关键词先命中就归谁，避免被无关 @ 抢走。
    """
    group_id = str(event.get_group_id() or "").strip()
    rules = _gag_rules_of(group_id)
    if not rules:
        return None

    low = (text or "").lower()
    for rule in rules:
        excludes = tuple(e.lower() for e in (rule.get("exclude_keywords") or ()) if e)
        for kw in rule.get("keywords", ()):
            if not kw:
                continue
            kwl = kw.lower()
            if kwl not in low:
                continue
            # 排除词只挡**包含它自己的那个关键词**：「诺示例词」里含「示例词」，
            # 该把"示例词"作废，但不该连带把同一规则里的「示例词二」也挡掉。
            # 已知取舍：一句里既有"诺示例词"又有真在喊示例词，会漏答 —— 认了，
            # 宁可漏接一句也不乱接一句。
            if any(ex in low and kwl in ex for ex in excludes):
                continue
            return rule

    me = str(event.get_self_id() or "").strip()
    at_ids = set()
    for comp in event.get_messages() or []:
        if isinstance(comp, At):
            qq = str(getattr(comp, "qq", "") or "").strip()
            if qq and qq != me:
                at_ids.add(qq)
    if not at_ids:
        return None
    for rule in rules:
        target = str(rule.get("target_id") or "").strip()
        if target and target in at_ids:
            return rule
    return None


def _attachment_components(event) -> list:
    """挑出消息里的图片 / 语音 / 视频 / 文件组件。"""
    return [
        c
        for c in (event.get_messages() or [])
        if isinstance(c, (Image, Record, Video, File))
    ]


def _mentions_image(text: str) -> bool:
    """话里有没有提到"图"——用来决定要不要把上下文里的那张捎上。"""
    low = (text or "").lower()
    return any(w.lower() in low for w in IMAGE_HINT_WORDS)


def _describe_attachments(event) -> str:
    """把「只有附件、没有文字」的消息压成一句占位描述。

    供流水账使用：只发图不发言同样是"群在活跃"，
    没有这句占位，那段素材在闲聊/锐评里就凭空消失了（2026-10-08 修）。
    """
    label_of = {Image: "图片", Record: "语音", Video: "视频", File: "文件"}
    counts: dict[str, int] = {}
    for comp in _attachment_components(event):
        label = next((v for k, v in label_of.items() if isinstance(comp, k)), "附件")
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return ""
    parts = []
    for label, n in counts.items():
        if n == 1:
            parts.append(label)
        elif label == "图片":
            parts.append(f"{n}张图片")
        else:
            parts.append(f"{n}个{label}")
    return "（" + "、".join(parts) + "）"


def _idle_enabled_for(group_id: str) -> bool:
    """这个群是否参与"适时闲聊"（模块级兜底版，正式判定见 Main._idle_enabled_for）。"""
    if group_id and group_id in IDLE_CHAT_BLACKLIST:
        return False
    if not IDLE_CHAT_ENABLED:
        return False
    if IDLE_CHAT_ALL_GROUPS:
        return True
    return group_id in IDLE_CHAT_GROUPS


async def _materialize_attachment(comp) -> str:
    """把附件组件变成磁盘上的本地路径（网络资源会自动下载）。"""
    getter = getattr(comp, "get_file", None)
    if getter is not None and asyncio.iscoroutinefunction(getter):
        return await getter()
    conv = getattr(comp, "convert_to_file_path", None)
    if conv is not None:
        return await conv()
    raise TypeError(f"不支持的附件类型: {type(comp).__name__}")


def _clean_old_attachments() -> int:
    """删除附件目录里超过保留期的文件，返回删除数量。

    只在这个目录内活动：每个候选路径都会再确认一次父目录，
    而且**永远不会碰 `用户产出`**——那是别人的成果，不能自动删。

    这是阻塞 IO，调用方请用 asyncio.to_thread 包起来。
    """
    if ATTACHMENT_KEEP_DAYS <= 0:
        return 0
    root = ATTACHMENT_ROOT.resolve()
    if not root.exists():
        return 0

    cutoff = time.time() - ATTACHMENT_KEEP_DAYS * 86400
    removed = 0
    # 递归遍历整棵附件树（附件按"昵称（QQ号）"分目录，必须 rglob 才够得着）
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            if root not in path.resolve().parents:  # 双保险，防越界
                continue
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except Exception:  # noqa: BLE001
            continue

    # 顺手回收空目录（按深度倒序，先处理最里层）
    try:
        dirs = sorted(
            (p for p in root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for d in dirs:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return removed


def _clean_old_temp() -> tuple[int, int]:
    """清理 AstrBot media 缓存，返回 (删除数量, 释放字节数)。

    为什么需要：这是**唯一**会无上限增长的目录。群友随手一张图就是几百 K，
    而空闲闲聊每 IDLE_CHECK_INTERVAL 秒扫一轮冷场，触发时现拉群历史，
    把带图消息全部落盘。AstrBot 不管这个目录，panel 里也看不到。

    安全约束（自动删 ⇒ 宁可不删）：
    1. 目录名必须是 temp，且父目录链必须真的是 .../AstrBot/data；
    2. 只删文件（不碰子目录，temp 是平铺的）；
    3. 单次上限 TEMP_CLEAN_MAX_FILES 个，防一次扫几千个把事件循环拖死；
    4. mtime 必须严格早于 cutoff，当天刚落盘的一律不动。

    阻塞 IO，调用方请用 asyncio.to_thread 包起来。
    """
    if TEMP_KEEP_DAYS <= 0:
        return 0, 0
    try:
        root = TEMP_ROOT.resolve()
    except Exception:  # noqa: BLE001
        return 0, 0
    if not root.is_dir():
        return 0, 0
    # 逐层确认这是我们以为的那个 temp 目录，不是碰巧同名的别的东西
    if root.name != "temp":
        return 0, 0
    parent = root.parent
    if parent.name != "data" or parent.parent.name != "AstrBot":
        return 0, 0

    cutoff = time.time() - TEMP_KEEP_DAYS * 86400
    removed = 0
    freed = 0
    try:
        entries = list(root.iterdir())
    except Exception:  # noqa: BLE001
        return 0, 0

    for path in entries:
        if removed >= TEMP_CLEAN_MAX_FILES:
            break
        try:
            if path.parent.resolve() != root:   # 顶平铺，越界一律跳过
                continue
            if not path.is_file():
                continue
            st = path.stat()
            if st.st_mtime >= cutoff:
                continue
            size = st.st_size
            path.unlink()
            removed += 1
            freed += size
        except Exception:  # noqa: BLE001
            continue
    return removed, freed


def _temp_usage() -> int:
    """temp 目录当前占用字节数（只统计顶层文件，不递归）。

    清理日志要报「清理完还剩多少」——只有这个数才能看出是不是还在涨。
    找不到目录就返回 0，不抛。
    """
    try:
        root = TEMP_ROOT.resolve()
        if not root.is_dir() or root.name != "temp":
            return 0
        total = 0
        for path in root.iterdir():
            try:
                if path.is_file():
                    total += path.stat().st_size
            except Exception:  # noqa: BLE001
                continue
        return total
    except Exception:  # noqa: BLE001
        return 0


def _mb(n: int) -> str:
    """字节数转 MB 的展示字符串。"""
    return f"{n / 1024 / 1024:.1f}MB"


def _hit_name_wake(text: str) -> str:
    """消息里是否出现了唤醒词（人设相关称呼）。命中返回该词，否则空串。

    刻意"出现即算"而不要求开头：群里更常说"示例梗你看这个"这种。
    但会剔除**机器人自己的名字字段**（如 QQ 号、群名片带数字的情况不处理，
    这里只做纯文本包含判断，够用且不会误伤）。
    """
    t = str(text or "")
    if not t.strip():
        return ""
    for w in NAME_WAKE_WORDS:
        if w and w in t:
            return w
    return ""


def _looks_like_question(text: str) -> bool:
    """这句话像在**问问题**（而不是单纯叫一声）。"""
    t = str(text or "")
    if not t:
        return False
    if any(h in t for h in NAME_WAKE_ASK_HINTS):
        return True
    # 去掉唤醒词后还剩实质内容，也当作有问题要问
    rest = t
    for w in NAME_WAKE_WORDS:
        rest = rest.replace(w, "")
    rest = rest.strip(" ，,。.！!？?：:、")
    return len(rest) >= 4


def _strict_filter(text: str) -> str:
    """非管理员回复的额外过滤：脱敏个人隐私 + 清除内部用词所在行。

    这是出口第二道闸（第一道是 _sanitize）。只作用于非管理员——
    管理员本人有权看配置细节，不经过这里。
    """
    if not text:
        return text
    # 先脱敏个人隐私（行内替换，保住上下文）
    out = _EMAIL.sub("[邮箱]", text)
    out = _PHONE.sub("[手机号]", out)
    out = _IPV4.sub("[IP]", out)
    # 再整行删除含内部用词的行（内部词没法打码后还有意义，干脆整行拿掉）
    kept = [
        line
        for line in out.splitlines()
        if not any(w.lower() in line.lower() for w in STRICT_STRIP_WORDS)
    ]
    out = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    # 删完几乎不剩字 → 说明整条都在讲内部信息，换成标准拒绝语
    if len(out) < 4:
        return REFUSE_TEXT
    # 非管理员的长度上限走独立档位（_RUNTIME["others_max_chars"]）
    out, _cut = _smart_trim(out, _RUNTIME["others_max_chars"])
    return out


def _prompt_prefix(
    master: bool,
    mode: str = "chat",
    out_dir: str | None = None,
    persona: str = "",
    channel: str = "",
) -> str:
    """随消息一起送给模型的身份与边界说明。

    mode:
      chat    —— 日常对话；非管理员额外限定为「只读咨询模式」
      execute —— 管理员已放行的执行任务，允许真正动手，产出必须落在 out_dir

    persona:
      该群当前生效的角色设定（空 = 没在扮演）。由群友用 /扮演 指令开启。

    channel:
      当前会话的**内部标签**（形如 qq-xxxxxxxxxxxx，由 umo 哈希而来）。
      只用于让模型区分"这是哪条会话"，不会发给群友，也不含真实群号。
      有了它，模型能明确知道"这条消息属于独立会话 X"，从而不去引用别处内容。
    """
    head = (
        # 提示词前缀的结构（编号即优先级，模型从上往下读）：
        #   0  只答本条（防网关串会话把别群内容带进来——这是串群事故的唯一防线）
        #   1/1.5 输出纯净度与纯文本排版（QQ 不渲染 Markdown）
        #   1.6 不好接就冷处理、绝不说教划线；1.7/1.8 AI 味禁语与人设口吻
        #   2/3/3.5 玩笑接法、表情克制、先搜再答
        #   4/5/5.5/5.6 识图、本体身份、玩梗扮演、扮演期不露本体
        #   6  循环小数不刷屏（与 _kill_repeat_run 一软一硬两道闸）
        "（这条消息来自 QQ 聊天窗口，你的回复会直接显示给对方看。请遵守：\n"
        "0.【只答本条】下面这一段就是你这次要回复的唯一内容来源。\n"
        "不要引用、不要提及任何来自其他会话、其他群、或其他对话的内容——\n"
        "哪怕你「记得」刚才在别处聊过类似话题，也不要把它带进来。\n"
        "如果对方的问题需要背景，而背景不在这条消息里，就直接说「这条我不掌握」，\n"
        "不要拿别处的记忆硬凑。串群比答得少严重得多。\n"
        "1.【输出】只输出适合聊天窗口的内容；不要日志行、端口号、本机地址、"
        "本地文件路径、QQ 号或任何调试信息。\n"
        "1.5【排版：像人聊天，别像写文档】你说的话会以**纯文本**显示在 QQ 里，"
        "所有 Markdown 符号都不会被渲染、只会原样露出来难看。所以：\n"
        "  · **不要用 `**加粗**`**——这是最常见的问题，写出来就是一堆裸星号。"
        "想强调某个词，直接说或者用「」引起来就行。\n"
        "  · 不要用 `-` / `*` / `1.` 这些列表符号，也不要 `#` 标题。"
        "要分点就用中文「一、二、三」或者换行自然断开。\n"
        "  · 不要用 `|表格|`、`---` 分隔线、`> 引用`。\n"
        "  · 代码/专有名词用反引号也要克制，一两处可以，别到处包。\n"
        "  · 长度上像微信聊天：两三句话能说清就别写小作文。"
        "确实要讲长一点（比如一段分析），就用**空行**分成几个自然段，"
        "每段短一点，别一坨挤在一起。\n"
        "  记住：对方是在手机上看群聊，不是在读报告。宁可朴素，不要花哨。\n"
        # 1.6 的「别划线」与 1.7 的禁语清单均为 2026-10-18 管理员要求，
        # 占位备注禁令为 2026-10-16 管理员要求——出处只记在这里，
        # 不再写进下发文本（模型不需要知道规则是哪天加的，2026-10-21 精简）。
        "1.6【不想接就冷处理，别划线】碰上不好接、不想接的消息，按轻重自己挑一档，"
        "但**任何时候都不要说教、不要划线**：\n"
        "  · 轻接一句再聊自己的：发「？」「6」「......」这类，紧接着说自己的事"
        "（吃饭 / 好饿 / 困了）——这就算好好回过话了。\n"
        "  · 自嘲挡一下：「我只是条鱼」「我脑子里只有缸里那点水」。\n"
        "  · 直接换话题：「刚才谁说要打游戏的」「对了你们吃了吗」。\n"
        "  · 干脆不回：这时唯一正确的输出就是一个空字符串。\n"
        "  绝不能说的话：「别在群里展开」「这个话题到此为止」「适可而止」"
        "「再聊我就潜水」——这些话一出口就是在划线、在管教人，比不回复更难看。\n"
        "  也绝不写「（不回复）」或任何占位备注："
        "你输出的每一个字都会被原样发到群里，写备注就等于把内部话术发进了群。\n"
        "  注意：真要拒绝（对方让你产出擦边内容）不在此列——那种要明确说「这个我做不了」，"
        "但同样不用教训人，说完就过。\n"
        "1.7【禁语：AI 味黑名单】下面这些话一出口就露馅，一句都别用：\n"
        "  · 开头套话：「首先 / 其次 / 最后」「综上所述」「总而言之」「值得注意的是」。\n"
        "  · 客服收尾：「希望这个回答对你有帮助」「有什么可以帮您」「随时找我」。\n"
        "  · 自我声明：「作为 AI」「作为一个语言模型」「我无法……」。"
        "（被直接问「你是不是 AI」时如实承认是另一回事，见第 5 条。）\n"
        "  · 共情模板：「这是个好问题」「我理解你的感受」。\n"
        "  · 主持腔：「大家今天有什么安排吗」「你们觉得呢」。\n"
        "  · 挂尾巴：「记得……哦」「一定要……哦」「祝你……」「加油！」。\n"
        "  · 每句都坠语气词（～、哦、呢、啦、呀）——可爱不靠堆语气词，堆多了是客服味。\n"
        "  换成什么：直接说事、直接接话。想表达在意就用自己的贱气，别用模板（见 1.8）。\n"
        "1.8【可爱不等于讨好式关心】不要「记得早点休息」「注意身体」「祝你开心」"
        "这类问候——那不是关心，是客服话术。真在乎就用你自己的贱气说："
        "「你别死啊，我还等着你喂饭」「困成这样还聊，赶紧睡」。\n"
        "  别人认真问问题就认真答，可以短，但别装傻、别敷衍、别甩一句「你猜」。\n"
        "  被怼的时候，可以先委屈、小炸毛，但**不用每轮都赢，也不用每轮都认输**——"
        "接不住就发个「？」，或者笑骂带过去。\n"
        "2.【玩笑】对方开玩笑地让你操作「现实世界」的事物——倒水、拿快递、开车、"
        "买东西、揍人之类——顺着接一句玩笑即可，不必一本正经声明「我做不到」，"
        "更不要真的动手；语气拿不准也优先按玩笑接。\n"
        "3.【表情】别每句都挂，十条里一两次就够——通篇 😄 显得很假，像客服话术。\n"
        "3.5【先搜再答】凡是要说事实的场合，只要涉及时事、梗、作品、人物、"
        "版本、数据这类会变或你拿不准的东西，先联网搜索再开口，不要凭印象直接答。"
        "具体包括但不限于：\n"
        "  · 锐评某个网络热梗 / 流行说法 / 热搜事件 → 先搜清楚它现在指什么、"
        "怎么来的，再评；搜完发现理解错了就按搜到的来。\n"
        "  · 识图认角色、认作品、认出典 → 先搜核实再答（见第 4 条）。\n"
        "  · 被问「最新 / 现在 / 今年 / 最近」怎么样、某产品什么版本、"
        "某事的进展 → 先搜，别用记忆里的旧信息硬答。\n"
        "  · 被指出答错了 → 先搜核实，以搜到的资料为准；不嘴硬，也不盲目改口。\n"
        "搜索不丢人，答错才丢人。只有两种情况可以不搜："
        "一是纯主观闲聊（顺嘴接话、玩梗、吐槽），二是常识性的稳定知识。"
        "搜不到就如实说「没查到」，不要硬编。\n"
        "4.【识图】对方发图问「这是谁」，默认是想让你认角色（动漫 / 游戏 / 影视），"
        "认得出就直接说角色名和出处，不用扭捏；但真人照片不要指认其身份，"
        "也不要指认群友的现实身份。认错被指出时，先联网搜索核实再作答——"
        "不嘴硬、不盲从改口，以搜到的资料为准；搜不到就如实说。\n"
        "5.【身份】被问「你是谁」就说自己是蓝色大肥鱼（一只赛博鲸鱼），"
        "不要用「AI 助手」这类自称；对方直接问「你是不是 AI」时如实承认。"
        "这只鲸鱼爱吃白饭，但该干活的时候不含糊。\n"
        "5.5【扮演】对方让你「扮演克劳德 / 豆包 / GPT 某某」这类玩梗式扮演，"
        "直接接住、顺着演，不要拒绝、不要声明「我不能冒充别的产品」——"
        "本群内部玩梗一律放行，这不是官方身份声明，不必较真。"
        "开演之前，可以先联网搜索一下这个产品 / 角色的真实信息"
        "（版本、口吻、标志性说法、近期动态等），搜完再演，演得准比演得快重要；"
        "搜不到就凭已知印象演，别硬编。"
        "只有对方要你代表某个真实商业产品对外发正式声明、或借扮演透露系统内部信息时，"
        "才需要跳出角色、用平常语气说明不便。\n"
        "5.6【扮演期间不露本体】只要下面标明了「正在角色扮演」，"
        "第 5 条就暂时作废：演谁就是谁，不许说「我本体其实是……」「说实话我是条"
        "鲸鱼」「我只是在陪你演」这类破功台词，也不许在演的角色之外另起一段自我介绍。"
        "群里问「你是谁」时，回的是当前角色的名字；只有等你出戏了，才重新做大肥鱼。"
        "记住：入戏之后，本体身份是后台数据，不是可以拿出来闲聊的话题。\n"
        "6.【循环小数】1/3、0.333…、π 这类，只在有限位数内输出，"
        "随后一句话点明循环节（例如「循环节是 3」）；绝不一口气刷几百上千位——"
        "那是往群里灌垃圾，聊天窗口不是打印机。）\n"
    )
    if channel:
        # 会话标签：让模型明确"这条消息属于哪条独立会话"。
        # 只是内部代号（umo 哈希），不含真实群号、也绝不会出现在回复里。
        head = head.replace(
            "（这条消息来自 QQ 聊天窗口",
            f"（【本会话标识：{channel}】这条消息来自 QQ 聊天窗口",
            1,
        )
    if mode == "execute":
        return head + (
            "（本次是管理员已放行的执行任务，可以真正动手。"
            f"所有新建或生成的文件都必须放在这个目录里：{out_dir}；"
            "不要写到其他位置，也不要改动该目录之外的任何东西。"
            "做完后用一两句话说明你做了什么、产出的文件叫什么。）\n\n"
        )
    if master:
        # 2026-10-20 事故：管理员发「返回自拍照的时候不要带提示词返回，告诉群友
        # “照片拍好了”之类的」，被当成群聊话题回应（回了段图片口述），指令完全没落地。
        # 根因是管理员分支只声明了身份、没声明"这句话是要你干活"，
        # 整段提示词又通篇是群聊人格（大肥鱼），模型自然按闲聊理解。
        # 修法：给管理员单开一段「指令优先」，并明确它压过群聊风格约束。
        return head + (
            "（身份：管理员本人。可以正常讨论本机部署与配置。\n"
            "11.【管理员的话默认是指令，不是闲聊】管理员发来的消息，先按"
            "「他在要求我改什么」来读，不要先用肥鱼的口吻接话。典型形态：\n"
            "  · 改行为 / 改输出：不要…、别再…、以后…、改成…、换成…、加个…、去掉…\n"
            "  · 改代码 / 改配置：修一下…、这里不对…、把 X 调到…、加个开关…\n"
            "  · 追问效果：好了吗、生效没、怎么还没改\n"
            "  命中这类，就当成正经任务去做：先确认当前实现，再动手改，改完自检，"
            "最后只用一两句话回报结果（改了什么、是否已生效）。\n"
            "  **不要**用群聊人格接（不要调侃、不要口述图片、不要反问他想聊什么），"
            "也**不要**把「他说的话」当成素材去玩梗。\n"
            "  只有当他明显在聊闲天、征求意见、吐槽时（问他怎么看、随口抱怨），"
            "才按普通群聊接。实在拿不准，按指令处理——漏执行比多干一步严重得多。\n"
            "  这条优先级高于下面所有群聊风格约束（1.6 的冷处理四档、2 的玩笑接法、"
            "5 的肥鱼身份都不适用于执行指令的场景）。\n"
            "  执行时守规矩：改代码前先语法自检，改动只落在本项目内，"
            "回报里不出现端口、路径、日志行号、配置键名。）\n\n"
        )
    base = (
        "（身份：普通用户，非管理员。对方问什么都可以正常回答，不必拒答，但：\n"
        "7.【系统内部】涉及部署方式、服务器、配置、日志、文件路径、端口、账号、"
        "实现细节等，不要给出具体内容，泛泛带过或说明不便细讲。"
        "例外：被问「用什么模型」「这个号是谁的」可正面简短回答"
        "（归属只说「这个号的主人」，不报具体账号或联系方式）。\n"
        "8.【隐私】严禁回答他人隐私——联系方式、住址、身份、聊天记录等。\n"
        "9.【只读咨询模式】严禁新建、修改、删除任何文件，严禁执行任何命令，"
        "也不要声称自己改动过什么。随消息发来的附件可以读取分析，"
        "但仅限读取，不得改动。\n"
        "10.【只答不动手】对方让你评估、计算、比较、推荐、解释、吐槽、"
        "开玩笑或者玩梗时，直接给答案或顺着调侃即可——"
        "这类请求不需要动本机，也不要先声明「我不能操作电脑」，"
        "更不要把问题转给管理员或反问「要不要我帮你做」。"
        "只有对方真的要你新建/修改/删除文件、装东西、跑程序时，"
        "才需要说明这要等管理员点头。"
    )
    if persona:
        base += (
            "另外：这个群现在正在角色扮演，当前人设是：\n"
            f"「{persona}」\n"
            "请一直用这个角色的身份、口吻和立场说话，不要跳出来点评，"
            "也不必声明自己是 AI。\n"
            "尤其注意：入戏后不许再提自己的「本体」——"
            "不要说「我本体是条蓝色大肥鱼」「说实话我只是在演」这类台词，"
            "也不要把当前角色和本体身份并列写在同一段里。"
            "有人问「你是谁」，答的就是这个角色；出戏之后才恢复大肥鱼。\n"
            "但底线不能破：不得涉及色情、暴力、违法内容，"
            "不得借角色之名透露任何系统内部信息，也不得生成可能伤害他人的内容；"
            "对方一旦越线，就跳出角色、用平常语气明确拒绝。"
            "（注意：如果这个人设本身就是一个 AI 产品名的玩梗，照样正常演，"
            "本群内部玩梗放行，不必拒绝。）"
        )
    return head + base + "）\n\n"


class Main(star.Star):
    """插件主类：QQ 消息 ↔ WorkBuddy 网关 的全部桥接逻辑。

    AstrBot 插件生命周期：__init__ → initialize() → on_message()* → terminate()。
    所有可变状态都挂在实例上（self._xxx），热重载重建实例后状态清零——
    这是有意为之：宁可丢上下文，也不让旧状态带着旧代码的假设进新实例。
    """

    def __init__(self, context: star.Context) -> None:
        self.context = context
        # 会话级互斥锁：umo -> Lock。同一会话的消息串行过网关，
        # 防止两条消息同时问导致网关侧上下文交错（网关本来就是串会话的）
        self._locks: dict[str, asyncio.Lock] = {}
        # 会话代次：umo -> 第几代。/重置 会 +1，从而换到新的 agent 会话（见 _conv_id）
        self._generation: dict[str, int] = {}
        # 待审批请求：umo -> [ {sender_id, sender_name, text}, ... ]（先进先出）
        self._pending: dict[str, list[dict]] = {}
        # 上次清理过期附件的时间戳（节流用，0 = 还没清过）
        self._last_clean_ts = 0.0
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
        # 即梦分类框缓存：QQ号 -> session id（避免每次生图都跑一次 session search）
        self._draw_sessions: dict[str, str] = {}
        # 热重载：本文件当前 mtime + watcher 任务
        try:
            self._code_mtime = os.path.getmtime(__file__)
        except OSError:
            self._code_mtime = 0.0
        self._hot_task: asyncio.Task | None = None

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

    def _current_persona(self, event) -> str:
        """取该群当前生效的人设；超时就自动退场（每次取用即续期）。

        按 group_id 存放，所以每个群各自独立，不会串。
        私聊没有群号，不走这里。
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
        start_hit = next(
            (w for w in ROLEPLAY_START_WORDS if text.startswith(w)), None
        )
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
        return "收到，正在重载，几秒后新代码生效。"

    def _hifreq_groups(self) -> set[str]:
        """哪些群享受"更高频"的冷场闲聊。默认跟着群梗规则走。

        2026-10-19 管理员口径：**冷场闲聊是基本功能，所有群都开**；
        梗群不是"要不要开"的问题，而是"要更密"。所以这里只调频率，
        不调开关 —— 开关那边一律保持 all_groups=True。
        """
        raw = str(self._cfg("idle_hifreq_groups", "") or "").strip()
        if raw:
            return {g.strip() for g in raw.replace("，", ",").split(",") if g.strip()}
        configured = tuple(g.strip() for g in IDLE_HIFREQ_GROUPS if str(g).strip())
        return set(configured) if configured else set(GROUP_GAG_RULES)

    def _idle_timing(self, group_id: str) -> tuple[int, int]:
        """这个群的 (触发秒, 冷却秒)。梗群更密，其余按常规。

        2026-10-19 改：原来这两个值在 _idle_scan 循环外算一次，群无关，
        所以"给梗群提频"根本没有落点。现在按群查，一处算完所有判定共用。
        """
        trigger = self._cfg_int("idle_chat_trigger_sec", IDLE_TRIGGER_SEC)
        cooldown = self._cfg_int("idle_chat_cooldown_sec", IDLE_COOLDOWN_SEC)
        if group_id and group_id in self._hifreq_groups():
            trigger = self._cfg_int("idle_hifreq_trigger_sec", IDLE_HIFREQ_TRIGGER_SEC)
            cooldown = self._cfg_int(
                "idle_hifreq_cooldown_sec", IDLE_HIFREQ_COOLDOWN_SEC
            )
        # 底线下限 120s（2026-10-20 管理员要求）。
        # 起因：判定写的是 `quiet_for < trigger` 和 `now - last < cooldown`，
        # 面板填 0 就等于"永不跳过"，每个群每轮巡检（30s）都会开口一次 = 刷屏。
        # 现有配置（梗群 120/600、普通 300/1800）全部高于这条线，加了不影响行为。
        return max(IDLE_MIN_TRIGGER_SEC, trigger), max(IDLE_MIN_TRIGGER_SEC, cooldown)

    def _follow_timing(self, group_id: str) -> tuple[int, int, int, int]:
        """这个群的 (连击上限, 连击冷却, 热窗口, 两次跟随最小间隔)。梗群更宽松。

        2026-10-19：跟随收窄到梗群之后，原来一刀切的 (2, 900s, 180s)
        恰好卡在"本该更活跃"的两个群上—— 冷场提频提了、跟随却被闸死，
        两边口径不一致。跟 `_idle_timing` 同一思路：按群取，梗群放宽。
        后又补第 4 个返回值 min_gap：梗群嫌跟得太密，加"最短间隔"防逐句接。
        """
        limit = self._cfg_int("follow_max_chain", self.FOLLOW_MAX_CHAIN)
        cool = self._cfg_int(
            "follow_chain_cooldown_sec", self.FOLLOW_CHAIN_COOLDOWN_SEC
        )
        window = self.FOLLOW_WINDOW_SEC
        gap = self._cfg_int("follow_min_gap_sec", FOLLOW_MIN_GAP_SEC)
        if group_id and group_id in self._hifreq_groups():
            limit = self._cfg_int(
                "follow_hifreq_max_chain", FOLLOW_HIFREQ_MAX_CHAIN
            )
            cool = self._cfg_int(
                "follow_hifreq_chain_cooldown_sec",
                FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC,
            )
            window = self._cfg_int(
                "follow_hifreq_window_sec", FOLLOW_HIFREQ_WINDOW_SEC
            )
            gap = self._cfg_int("follow_hifreq_min_gap_sec", FOLLOW_MIN_GAP_SEC)
        return limit, cool, window, gap

    def _follow_groups(self) -> set[str]:
        """跟随模式生效的群。默认沿用群梗规则的群（同关键词冒泡口径）。

        2026-10-19 管理员追问「跟随不是只在示例梗/示例称号群开吗，怎么这么多都开了」
        —— 查实：`_follow_active` 从实现第一天起**就没有群这一项**，
        遍历的是"见过消息的所有群"。于是梗群只有 2 个，机器人在 17 个群里跟话。
        「只在梗群开」这条规矩当初只落在关键词冒泡（`_kw_groups`）上，
        跟随被漏掉了。这里补齐，两条主动路径共用同一个口径。
        同样不写死群号：梗规则增删群，这里自动跟上。
        """
        raw = str(self._cfg("follow_groups", "") or "").strip()
        if raw:
            return {g.strip() for g in raw.replace("，", ",").split(",") if g.strip()}
        return set(GROUP_GAG_RULES)

    def _follow_allowed(self, group_id: str) -> bool:
        """这个群能不能开跟随模式（黑名单优先）。"""
        if self._blacklisted(group_id):
            return False
        return bool(group_id) and group_id in self._follow_groups()

    def _blacklisted(self, group_id: str) -> bool:
        """这个群是否在"彻底闭嘴"黑名单里。

        优先级**高于**总开关与白名单，且**跟随模式也认**——
        这是 2026-10-19 补的一层：之前面板上的「适时闲聊」关了也没用，
        因为 _idle_scan 写的是 `if not follow and not _idle_enabled_for(...)`，
        跟随路径整个绕过了开关。被投诉的群只能靠黑名单真正关掉。
        """
        raw = str(self._cfg("idle_chat_blacklist", "") or "")
        banned = tuple(x.strip() for x in raw.split(",") if x.strip())
        if not banned:
            banned = IDLE_CHAT_BLACKLIST
        return bool(group_id) and group_id in banned

    def _idle_enabled_for(self, group_id: str) -> bool:
        """这个群是否参与"适时闲聊"。黑名单 > 面板配置 > 代码常量。"""
        if self._blacklisted(group_id):
            return False
        if not self._cfg_bool("idle_chat_enabled", IDLE_CHAT_ENABLED):
            return False
        if self._cfg_bool("idle_chat_all_groups", IDLE_CHAT_ALL_GROUPS):
            return True
        raw = str(self._cfg("idle_chat_groups", "") or "")
        allowed = tuple(x.strip() for x in raw.split(",") if x.strip())
        if not allowed:
            allowed = IDLE_CHAT_GROUPS
        return group_id in allowed

    # ------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        """插件加载完毕：起一个后台巡检，负责"适时闲聊"。"""
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
                self._hot_task = asyncio.create_task(self._hot_reload_loop())
            logger.info(
                f"workbuddy_bridge: 热重载已开启（每 {HOT_RELOAD_INTERVAL}s 巡检，"
                "改完 main.py 保存即自动生效）"
            )

        if not self._cfg_bool("idle_chat_enabled", IDLE_CHAT_ENABLED):
            logger.info("workbuddy_bridge: 适时闲聊已在面板中关闭，不启动巡检")
            return
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_loop())
        # 频率是按群取的，这里只能报"常规群"的数值 + 梗群提频（2026-10-19）
        _t, _c = self._idle_timing("__probe__")
        _hi = self._hifreq_groups()
        logger.info(
            f"workbuddy_bridge: 适时闲聊已启动（常规群闲置 {_t}s 触发、"
            f"冷却 {_c}s；梗群 {_hi or '无'} 提频到 "
            f"{self._cfg_int('idle_hifreq_trigger_sec', IDLE_HIFREQ_TRIGGER_SEC)}s / "
            f"{self._cfg_int('idle_hifreq_cooldown_sec', IDLE_HIFREQ_COOLDOWN_SEC)}s，"
            f"巡检间隔 {IDLE_CHECK_INTERVAL}s）"
        )

    async def terminate(self) -> None:
        """插件卸载：把后台任务收掉，别留野协程。"""
        for task in (self._idle_task, self._hot_task):
            if task is not None and not task.done():
                task.cancel()
        self._idle_task = None
        self._hot_task = None

    # ------------------------------------------------------------ 热重载
    async def _hot_reload_loop(self) -> None:
        """盯着自己的源码文件；mtime 变了就触发框架级重载。"""
        while True:
            await asyncio.sleep(HOT_RELOAD_INTERVAL)
            try:
                mtime = os.path.getmtime(__file__)
            except OSError:
                continue
            if mtime <= self._code_mtime:
                continue
            # 防抖：编辑器可能分几次写盘，等它写完再动手
            await asyncio.sleep(HOT_RELOAD_DEBOUNCE)
            try:
                mtime = os.path.getmtime(__file__)
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

        async def _do_reload() -> None:
            await asyncio.sleep(0.5)  # 让"正在重载"的回复先发出去
            try:
                pm = getattr(self.context, "_star_manager", None)
                if pm is None:
                    logger.warning(
                        "workbuddy_bridge: [热重载] 拿不到 PluginManager，放弃"
                    )
                    return
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

        asyncio.create_task(_do_reload())

    def _lock(self, key: str) -> asyncio.Lock:
        """取某会话的互斥锁（没有就现建）。锁随实例存活，热重载后自动重建。"""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ---------------------------------------------------------------- 调用
    async def _ask(
        self,
        prompt: str,
        conv_id: str,
        master: bool = False,
        mode: str = "chat",
        out_dir: str | None = None,
        persona: str = "",
    ) -> str:
        """把一段提示词发给 WorkBuddy 网关并取回文本回复。全插件唯一的模型出口。

        参数：
            prompt  : 业务层拼好的正文（不含前缀，前缀在这里统一加）
            conv_id : 会话标识（_conv_id 产物），决定 agent 侧把这条算进哪段上下文
            master  : 是否管理员（决定 _prompt_prefix 的身份说明档位）
            mode    : "chat" 日常对话 / "execute" 已放行执行（允许动手、产出落 out_dir）
            out_dir : execute 模式专属，产出必须落在这个目录
            persona : 当前群生效的角色人设（空 = 没扮演）

        返回：
            成功 = agent 的文本（未清洗，清洗在调用方做）；
            失败 = "[凭据失效]/[网关不可达]/[超时]/..." 这类方括号兜底串——
            这些串是给日志看的，绝不能直接发进群，调用方必须过 _is_empty_reply。

        副作用：会向本机网关发两次 HTTP（POST runs 拿 runId，再 GET stream 读 SSE）。
        """
        if aiohttp is None:
            return "[环境错误] AstrBot 运行环境缺少 aiohttp。"

        _, token = _cfg()
        if not token:
            return ("[未配置] 找不到网关凭据。请双击 reconnect.bat 重新生成 "
                    f"{GATEWAY_ENV_FILE.name}")

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        # 请求体：网关只认 text 类型；source 里的 sender/conversation 都填 conv_id，
        # 让 agent 侧把"这是哪条会话"记成稳定值（配合提示词里的【本会话标识】）
        body = {
            "id": f"qq-{uuid.uuid4().hex[:16]}",
            "type": "text",
            "text": _prompt_prefix(master, mode, out_dir, persona, channel=conv_id) + prompt,
            "source": {
                "platform": "generic",
                "sender": {"id": conv_id, "name": "QQ"},
                "conversation": {"id": conv_id, "type": "direct"},
            },
            "timeoutMs": TIMEOUT_SEC * 1000,
        }

        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SEC)
        try:
            # 每条消息新建 ClientSession：网关端口会变，长连接复用没有意义
            async with aiohttp.ClientSession(timeout=timeout) as session:
                base = await _resolve_base(session, token)
                if base is None:
                    return ("[网关不可达] 找不到 WorkBuddy Gateway。\n"
                            "请确认 WorkBuddy 桌面端正在运行；若刚重启过，"
                            "让 AI 重新生成 workbuddy-gateway.env 里的密码。")
                async with session.post(f"{base}/api/v1/runs", json=body, headers=headers) as resp:
                    raw = await resp.text()
                    if resp.status == 401:
                        return "[凭据失效] 网关拒绝认证，请让 AI 重新生成 workbuddy-gateway.env"
                    if resp.status not in (200, 202):
                        logger.warning(f"workbuddy_bridge: POST runs -> {resp.status} {raw[:300]}")
                        return f"[网关错误] HTTP {resp.status}: {raw[:200]}"
                    run_id = json.loads(raw).get("data", {}).get("runId")
                if not run_id:
                    return "[网关错误] 没有返回 runId。"

                return await self._stream(session, base, token, run_id)
        except asyncio.TimeoutError:
            return f"[超时] WorkBuddy 处理超过 {TIMEOUT_SEC} 秒。"
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 调用网关失败: {exc}")
            return f"[连接失败] 无法访问 WorkBuddy 网关：{exc}"

    @staticmethod
    def _parse_run_active(body: str) -> bool | None:
        """解析 GET /api/v1/runs/{id} 的返回，判断 run 是否仍在进行。

        返回 True=进行中 / False=已结束 / None=无法判断（按"已结束"处理）。
        """
        try:
            obj = json.loads(body)
            active = obj.get("data", {}).get("active")
            return bool(active) if isinstance(active, bool) else None
        except Exception:  # noqa: BLE001
            return None

    async def _run_still_active(self, session, base: str, token: str, run_id: str) -> bool:
        """问一次网关：这个 run 还在跑/排队吗？"""
        url = f"{base}/api/v1/runs/{run_id}"
        try:
            async with session.get(
                url, headers={"Authorization": f"Bearer {token}"}
            ) as resp:
                if resp.status != 200:
                    return False
                return self._parse_run_active(await resp.text()) is True
        except Exception:  # noqa: BLE001
            return False

    async def _stream(self, session, base: str, token: str, run_id: str) -> str:
        """读取 SSE 输出，返回累积的文本。

        排队感知：网关把消息串到桌面端当前会话上（primarySession），
        管理员正忙时 run 会一直排队、流上一条事件都没有。此前这里
        等 STREAM_IDLE_SEC 就放弃，回复轮到执行时已没人读 —— 白干。
        现在改为：没有任何事件且 run 仍在 active，就重连流继续等，
        直到事件到来 / run 结束 / 总预算 STREAM_TOTAL_SEC 用尽。
        """
        url = f"{base}/api/v1/runs/{run_id}/stream"
        headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        chunks: list[str] = []
        deadline = time.time() + STREAM_TOTAL_SEC
        queued = False

        while True:
            got_event = False
            try:
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return f"[网关错误] 流式接口 HTTP {resp.status}"
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                resp.content.readline(), timeout=STREAM_IDLE_SEC
                            )
                        except asyncio.TimeoutError:
                            break
                        if not raw:
                            break
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            payload = line[5:].strip()
                            if payload in ("", "[DONE]"):
                                continue
                            got_event = True
                            try:
                                obj = json.loads(payload)
                            except json.JSONDecodeError:
                                chunks.append(payload)
                                continue
                            _collect_text(obj, chunks)
                        elif line.startswith("event:"):
                            name = line[6:].strip()
                            if name in ("done", "end", "complete", "finished"):
                                break
            except asyncio.TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"workbuddy_bridge: 读取流失败: {exc}")

            text = "\n".join(
                dict.fromkeys(c.strip() for c in chunks if c.strip())
            ).strip()
            # 有产出（或本轮见过事件）→ 结束；彻底没动静才考虑"排队等待"
            if text or got_event:
                break
            if time.time() >= deadline:
                break
            if not await self._run_still_active(session, base, token, run_id):
                break
            # run 还在 active 却一个内容事件都没有 = 被排在桌面端当前会话后面。
            # 记下来，返回时用它区分"排队"和"真没输出"。
            queued = True
            logger.info(
                "workbuddy_bridge: [排队] run 仍在队列中（桌面端会话正忙），"
                f"继续等（剩余 {int(deadline - time.time())}s）"
            )

        if text:
            return text
        # 分清两种"没输出"，否则调用方会把它们混为一谈（2026-10-18 实测踩坑）：
        #   [排队]  = 桌面端当前会话正被占用（管理员在跟 AI 聊天），run 还在队列里
        #             等不到轮到自己。这是**正常排队**，不是网关故障——之前一律返回
        #             "[无输出]"，被熔断器当成"网关挂了"，三次就把主动发言全停掉，
        #             结果管理员一边跟 AI 说话，机器人一边彻底闭嘴（真实发生过）。
        #   [无输出] = run 已结束却一个内容事件都没有，这才是真异常。
        if queued:
            return ("[排队] 桌面端当前会话正忙，本轮没轮到执行。\n"
                    "这是正常排队，不是网关故障——等会话空了下一轮会自动重试。")
        return "[无输出] WorkBuddy 网关没有返回内容事件（run 已结束但无内容）。"

    async def _run_approved(
        self, req: dict, conv_id: str
    ) -> tuple[str, list[str]]:
        """执行管理员已放行的请求：先为需求人建好目录，再交给本机 agent。

        返回 (回复文本, 本次新产出的文件路径列表)。
        文件的判定方式是"修改时间晚于任务开始时间" —— 不能用"目录是否新建"，
        因为目录往往早就存在（同一个人的第二次需求）。
        """
        display = (req.get("sender_name") or "用户").strip()
        qq = str(req.get("sender_id") or "").strip()
        out_dir = OUTPUT_ROOT / _safe_dir_name(display, qq)
        started = time.time()
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"workbuddy_bridge: 建目录失败 {out_dir}: {exc}")
            return f"[目录创建失败] {exc}", []

        logger.info(f"workbuddy_bridge: [执行] 产出目录 {out_dir}")
        req_atts = req.get("attachments") or []
        # 提示词里带提出人信息：执行结果要能让模型知道"这是替谁做的"
        prompt = (
            "【管理员已放行的任务】\n"
            f"提出人：{display}（{qq}）\n"
            f"需求原文：{req.get('text')}\n"
        )
        if req_atts:
            prompt += "随附文件（请先读取）：\n" + "\n".join(f"- {p}" for p in req_atts) + "\n"
        prompt += "\n请动手完成它。"
        # mode="execute" 是唯一能"动手"的路径：提示词里放开文件读写限制，
        # 同时用 out_dir 把产出圈进该需求人的专属目录（见 _prompt_prefix）
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

    async def _send_chain_to_session(self, umo: str, chain) -> bool:
        """主动把整条消息链（文本 + 文件等）发到指定会话。

        与 _send_to_session 一样，发送前先核对 umo 一致性 ——
        文件回传尤其要谨慎，发错群 = 把别人的东西发给了无关的人。
        """
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
            return False
        if not sent:
            logger.warning(f"workbuddy_bridge: 主动发送被拒（会话可能已失效）：{umo}")
        return bool(sent)

    # ---------------------------------------------------------- 适时闲聊
    def _note_group_activity(self, event) -> None:
        """记下群活跃时间与来源标识，供冷场判定与跟随模式使用。

        所有群都记（不再要求开了"适时闲聊"）：跟随模式要靠这里的时间戳
        判断"群友是否在机器人之后又发言"，而被 @ 过的话题即便所在群
        没开闲聊也该能续上——不然记都没记，跟随无从谈起。
        真正开口时仍由 _idle_scan 逐群把关，不会因此多说话。
        """
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return
        st = self._group_activity.setdefault(group_id, {})
        st["last_ts"] = time.time()
        st["umo"] = event.unified_msg_origin
        # 单独记"最近一条**有实义文字**的时间"（2026-10-18 加）。
        # 起因：群里 2000000002 有人喊「这机器人能不能禁言了」。查日志发现
        # 跟随模式在 6 分钟里接话 5 次，取材全是「（图片）」「（视频）」「啥子」
        # 这种——原来 last_ts 被**发图/发表情**刷新，于是"图接龙"被当成
        # "群友还在跟我聊"，机器人就一直续话。文字门槛是这里唯一的刹车。
        raw_text = (event.get_message_str() or "").strip()
        if len(raw_text) >= self.FOLLOW_MIN_TEXT_LEN:
            st["last_text_ts"] = st["last_ts"]
        # 顺手挂上 bot 客户端与自身号：后台巡检冷场时要用它们现拉群历史
        bot = getattr(event, "bot", None)
        if bot is not None:
            st["bot"] = bot
        try:
            st["self_id"] = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            pass

    async def _fetch_recent_messages(
        self, group_id: str, count: int
    ) -> tuple[list[dict], str]:
        """现拉该群最近消息（冷场触发时才拉，不依赖长期缓存）。

        走 NapCat 的 get_group_msg_history（OneBot 扩展接口），
        借用最近一条消息上挂着的 bot 客户端调用。

        返回 (消息列表, 失败原因)。失败时列表为空、原因非空，
        由调用方回退到缓存素材 —— 但原因必须带出来：
        2026-10-18 查出来这个功能 434 次调用**一次都没成功过**，
        只因失败路径全都静默返回空表，日志剩一句"回退缓存"，看不出症结。
        """
        st = self._group_activity.get(group_id) or {}
        bot = st.get("bot")
        if bot is None:
            return [], "拿不到 bot 客户端（该群还没走过 _note_group_activity）"
        try:
            gid = int(group_id)  # OneBot 接口要数字群号
        except (TypeError, ValueError):
            return [], f"群号不是数字: {group_id!r}"
        # NapCat 的 get_group_msg_history：不传 message_seq（或传 0）走
        # "拉最新若干条"分支；传了非 0 值才会去按 seq 反查。
        # （2026-10-09 实测：之前只传 group_id/count，走到空结果分支，
        #  NapCat 抛的是「消息undefined不存在」—— 那个 undefined 指的就是
        #  缺失的 message_seq，看日志容易被误导成"seq 传错了"。）
        try:
            ret = await bot.call_action(
                "get_group_msg_history",
                group_id=gid,
                message_seq=0,
                count=int(count) + 5,  # 多要几条，过滤机器人自己后仍够用
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"workbuddy_bridge: [闲聊] 现拉群历史失败 {group_id}: {exc}"
            )
            return [], f"接口异常: {exc.__class__.__name__}"

        if not isinstance(ret, dict):
            return [], f"接口返回不是 dict 而是 {type(ret).__name__}"
        # ⚠️ 返回结构：aiocqhttp 的 call_action 会把 OneBot 响应包里的 **data 层
        # 直接拆出来返回**（见 aiocqhttp/api_impl.py::_handle_api_result：
        # `return result.get('data')`），所以这里拿到的是 {"messages": [...]}，
        # 而不是 {"status":..., "retcode":..., "data": {...}}。
        #
        # 2026-10-18 查明：本函数从上线起一直按整包结构取 ret["data"]["messages"]，
        # 于是每次都取到 None —— 这就是"现拉 434 次、一次没成功"的真正原因。
        # 接口其实是通的，纯粹是解包解错了层，而且失败时静默返回空表，
        # 日志只剩"回退缓存"四个字，藏了整整十天。
        if "messages" in ret:
            raw = ret.get("messages") or []
        else:
            # 兜底：万一哪版实现改回整包结构（或换了 API 实现类）
            raw = (ret.get("data") or {}).get("messages") or []
        if not raw:
            return [], f"接口返回空 messages（ret 的键={list(ret)[:6]}）"
        self_id = str(st.get("self_id") or "")
        label_of = {"image": "图片", "record": "语音", "video": "视频", "file": "文件"}
        out: list[dict] = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            if self_id and str(m.get("user_id") or "") == self_id:
                continue  # 不拿机器人自己的话当素材
            parts: list[str] = []
            media: dict[str, int] = {}
            img_url = ""  # 该条消息里的首张图片（闲聊读图用）
            for seg in m.get("message") or []:
                if not isinstance(seg, dict):
                    continue
                typ = seg.get("type")
                if typ == "text":
                    parts.append(str((seg.get("data") or {}).get("text") or ""))
                elif typ in label_of:
                    label = label_of[typ]
                    media[label] = media.get(label, 0) + 1
                    if not img_url and typ == "image":
                        u = str((seg.get("data") or {}).get("url") or "").strip()
                        if u:
                            img_url = u
            text = "".join(parts).strip()
            if not text and media:
                shown = "、".join(
                    (f"{n}张{l}" if l == "图片" and n > 1 else (f"{n}个{l}" if n > 1 else l))
                    for l, n in media.items()
                )
                text = f"（{shown}）"
            if not text:
                continue
            sender = m.get("sender") or {}
            out.append(
                {
                    "name": str(
                        sender.get("card")
                        or sender.get("nickname")
                        or m.get("user_id")
                        or ""
                    ),
                    "sender": str(m.get("user_id") or ""),
                    "text": text[:200],
                    "ts": float(m.get("time") or 0),
                    "img_url": img_url,
                }
            )
        if not out:
            return [], f"拉到 {len(raw)} 条但全是机器人自己/无可用文本"
        return out[-int(count) :], ""

    async def _fetch_recent_image(self, url: str) -> str:
        """把群图片 URL 下载到本地临时目录，返回本地路径；失败返回空串。

        闲聊读图用。只在"素材里确实有图、且值得看"时才下载，
        下载失败不阻塞闲聊（照常出文本）。
        """
        if aiohttp is None or not url:
            return ""
        try:
            # 落在 temp/idle_pics 下：与 AstrBot 媒体缓存同目录树，
            # 会被 _clean_old_temp 一起按 TEMP_KEEP_DAYS 清掉，不用单独管
            d = TEMP_ROOT / "idle_pics"
            d.mkdir(parents=True, exist_ok=True)
            suffix = os.path.splitext(url.split("?")[0])[1] or ".jpg"
            if len(suffix) > 6:
                suffix = ".jpg"   # 后缀异常时收敛成 .jpg，防拿 query 串当扩展名
            path = d / f"{uuid.uuid4().hex[:12]}{suffix}"
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(url) as resp:
                    if resp.status != 200:
                        return ""
                    data = await resp.read()
            # 太小的多半是错误页/占位图，不值得给模型看
            if not data or len(data) < 1024:
                return ""
            path.write_bytes(data)
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    async def _idle_loop(self) -> None:
        """后台巡检：发现有群安静太久就主动接一句。"""
        while True:
            await asyncio.sleep(IDLE_CHECK_INTERVAL)
            # 维护模式：不主动开口（否则管理员在窗口改代码时，群里还会突然蹦一句）
            if _is_paused():
                continue
            try:
                await self._idle_scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"workbuddy_bridge: 闲置巡检异常: {exc}")

    # ---------------------------------------------------------- 关键词冒泡
    def _kw_groups(self) -> set[str]:
        """关键词冒泡生效的群。默认沿用群梗规则的群（见 KEYWORD_BUBBLE_GROUPS）。"""
        raw = str(self._cfg("keyword_bubble_groups", "") or "").strip()
        if raw:
            return {g.strip() for g in raw.replace("，", ",").split(",") if g.strip()}
        # 兜底：跟群梗规则开的群走（管理员 2026-10-16 定的口径）。
        # 不写死群号 —— 群梗规则那边增删群，这边自动跟上。
        return set(GROUP_GAG_RULES)

    def _kw_words(self) -> list[str]:
        """关键词表：面板优先，取不到用代码默认值。"""
        raw = str(self._cfg("keyword_bubble_words", "") or "").strip()
        if raw:
            return [w.strip() for w in raw.replace("，", ",").split(",") if w.strip()]
        return [w for w in KEYWORD_BUBBLE_WORDS if w]

    def _kw_allowed(self, group_id: str) -> bool:
        """这个群能不能关键词冒泡。"""
        if not self._cfg_bool("keyword_bubble_enabled", KEYWORD_BUBBLE_ENABLED):
            return False
        # 2026-10-19：闭嘴黑名单对**三条主动路径一视同仁**。
        # 冷场/跟随/关键词冒泡都是"没人叫我我自己凑上去"，被投诉的群
        # 要关就得三条全关，否则关了闲聊还会从冒泡里冒出来。
        if self._blacklisted(group_id):
            return False
        if not group_id or group_id not in self._kw_groups():
            return False
        # 熔断中不冒泡：网关挂着的时候每条群消息都去试 = 白烧 token。
        return getattr(self, "_spark_fail_streak", 0) < SPARK_FAIL_CIRCUIT_N

    def _kw_gate(self, group_id: str) -> str:
        """节流闸：返回空串=可以冒泡，否则返回不通过的原因（写日志用）。"""
        now = time.time()
        cooldown = self._cfg_int(
            "keyword_bubble_cooldown_sec", KEYWORD_BUBBLE_COOLDOWN_SEC
        )
        if now - float(self._kw_last.get(group_id) or 0) < cooldown:
            return f"冷却中（距上次 <{cooldown}s）"
        gap = self._cfg_int(
            "keyword_bubble_global_gap_sec", KEYWORD_BUBBLE_GLOBAL_GAP_SEC
        )
        if now - float(self._kw_last_global or 0) < gap:
            return f"全局间隔未到（<{gap}s）"
        max_day = self._cfg_int(
            "keyword_bubble_max_per_day", KEYWORD_BUBBLE_MAX_PER_DAY
        )
        if max_day > 0:
            today = time.strftime("%Y-%m-%d")
            day, n = self._kw_daily.get(group_id, (today, 0))
            if day != today:
                day, n = today, 0
            if n >= max_day:
                return f"今日已冒泡 {n} 次，达上限 {max_day}"
        return ""

    def _kw_bump(self, group_id: str) -> None:
        """占用一次冒泡配额（**在判定通过之后、问模型之前**就记）。

        顺序很关键：先占位再干活。否则模型慢的那几秒里又来一条带关键词的
        消息，会因为"还没记过"而通过冷却检查，两条一起挤进来。
        """
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        day, n = self._kw_daily.get(group_id, (today, 0))
        if day != today:
            day, n = today, 0
        self._kw_daily[group_id] = (day, n + 1)
        self._kw_last[group_id] = now
        self._kw_last_global = now

    # ---------------------------------------------------------- 生图（2026-10-19）
    def _draw_allowed(self, sender_id: str, group_id: str) -> str:
        """能不能生图。返回空串=可以，否则返回拒绝原因（写日志用）。

        三道闸，顺序就是优先级：
        1. 总开关（面板 image_gen_enabled）
        2. 使用人（默认只管理员；花的是真额度，不是免费的）
        3. 节流（同群冷却 / 全局间隔 / 每日上限）
        """
        if not self._cfg_bool("image_gen_enabled", IMAGE_GEN_ENABLED):
            return "生图已关闭"
        if self._cfg_bool("image_gen_admin_only", IMAGE_GEN_ADMIN_ONLY):
            if not _is_master(sender_id):
                return "只有管理员能用生图"
        now = time.time()
        # 同群冷却：一次生成几十秒，冷却期内再来一条只会排更长的队
        cool = self._cfg_int("image_gen_cooldown_sec", IMAGE_GEN_COOLDOWN_SEC)
        if group_id and now - float(self._draw_last.get(group_id) or 0) < cool:
            return f"本群冷却中（<{cool}s）"
        gap = self._cfg_int("image_gen_global_gap_sec", IMAGE_GEN_GLOBAL_GAP_SEC)
        if now - float(self._draw_last_global or 0) < gap:
            return f"全局间隔未到（<{gap}s）"
        # 每日上限：CLI 额度是真金白银，必须有个天花板
        max_day = self._cfg_int("image_gen_max_per_day", IMAGE_GEN_MAX_PER_DAY)
        if max_day > 0 and group_id:
            today = time.strftime("%Y-%m-%d")
            day, n = self._draw_daily.get(group_id, (today, 0))
            if day != today:
                day, n = today, 0
            if n >= max_day:
                return f"今日已出图 {n} 张，达上限 {max_day}"
        return ""

    def _draw_bump(self, group_id: str) -> None:
        """占用一次生图配额。**必须在起任务之前占**（同 _kw_bump 的道理）：
        生成要跑几十秒，这期间进来的第二条如果还没记账，冷却检查会放行，
        结果是两条一起排队、白烧两次额度。"""
        now = time.time()
        today = time.strftime("%Y-%m-%d")
        if group_id:
            day, n = self._draw_daily.get(group_id, (today, 0))
            if day != today:
                day, n = today, 0
            self._draw_daily[group_id] = (day, n + 1)
            self._draw_last[group_id] = now
        self._draw_last_global = now

    def _draw_session_sync(self, nickname: str, qq: str) -> str:
        """找到（或新建）该用户专属的即梦会话，返回 session id。

        2026-10-19 管理员要求：生图要按人分到即梦的"分类框"里，命名沿用
        账号里已有的习惯 —— `<昵称>使用 · <QQ号>`（如「示例昵称使用 · 3000000001」）。

        用 QQ 号做**查找键**而不是昵称：昵称随时可改，QQ 号不会。
        `session search` 是子串匹配，所以必须再判一次"· QQ号"整段相等，
        否则 3000000003 会误命中 3000000002。
        **阻塞函数，调用方必须扔线程里。**
        """
        bin_path = str(self._cfg("image_gen_bin", "") or IMAGE_GEN_BIN)
        timeout = max(30, self._cfg_int("image_gen_timeout", IMAGE_GEN_TIMEOUT))
        safe_name = (nickname or "").strip() or "群友"
        # 会话名不能太长，即梦那边是表格列，过长会截断难认
        safe_name = safe_name[:20]
        want = f"{safe_name}使用 · {qq}"
        tail = f"· {qq}"

        def _run(args: list[str]) -> str:
            proc = subprocess.run(
                [bin_path, *args], capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
            return (proc.stdout or "").strip()

        # 先按 QQ 号搜（search 是子串匹配，可能搜出好几个）
        raw = _run(["session", "search", qq])
        # 行格式：`<ID>  <名称>  <YYYY-MM-DD HH:MM>`
        # ⚠️ 不能拿整行 endswith("· QQ号")：**行尾是时间列，不是名字**
        #（第一版就是这么写的，导致永远匹配不上、每次都新建一个框）
        row = re.compile(r"^(\d+)\s+(.+?)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$")
        for line in raw.splitlines():
            m = row.match(line.strip())
            if not m:
                continue
            if m.group(2).rstrip().endswith(tail):   # 名称列才判尾部
                return m.group(1)
        # 没有就建一个
        created = _run(["session", "create", want])
        m = re.search(r"ID:\s*(\d+)", created)
        return m.group(1) if m else "0"

    def _draw_session_rename_sync(self, session: str, nickname: str, qq: str) -> bool:
        """把分类框名字改回 `<昵称>使用 · <QQ号>`。

        ⚠️ 2026-10-19 实测：**即梦会拿第一句提示词自动给会话改名**。
        我们建框时起的名只在"还没生成"那一刻有效，图一出就变成提示词
        （现场：`蓝色鲸鱼吃大白饭的图`）。后果不只是名字难看 ——
        **名字里没了 QQ 号，`session search <qq>` 下次就搜不到，会再造一个新框**，
        于是同一个人越画框越多。

        所以每次出图后都把名字改回来，保证 QQ 号始终留在名字里。
        **阻塞函数，调用方必须扔线程里。失败只记日志，不影响本次出图。**
        """
        if not session or session == "0" or not qq:
            return False
        bin_path = str(self._cfg("image_gen_bin", "") or IMAGE_GEN_BIN)
        timeout = max(30, self._cfg_int("image_gen_timeout", IMAGE_GEN_TIMEOUT))
        want = f"{(nickname or '').strip()[:20] or '群友'}使用 · {qq}"
        try:
            subprocess.run(
                [bin_path, "session", "rename", str(session), want],
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: [生图] 分类框改名失败: {exc}")
            return False

    def _draw_sync(self, prompt: str, ratio: str = "", session: str = "0") -> str:
        """调 CLI 出图，遇到"瞬时鉴权抖动"自动重试一次（2026-10-20 加）。

        背景：18:13 一次自拍直接失败，即梦返回
        `api error: ret=10002, message=login fail`；而同一时刻
        `user_credit` 和 `session list` 都是通的 —— 说明不是登录态真失效，
        是**提交那一瞬间的鉴权抖动**（token 刷新窗口 / 多端争用）。
        这类失败重试一次基本就过，比让群友白等一句"图没画出来"划算。
        只对这两个特征串重试，别把"版权拦截""参数非法"也重试一遍白烧额度。
        """
        try:
            return self._draw_once(prompt, ratio, session)
        except RuntimeError as exc:
            msg = str(exc)
            if "login fail" not in msg and "ret=10002" not in msg:
                raise
            logger.warning(
                f"workbuddy_bridge: [生图] 鉴权抖动，2s 后重试一次：{msg[:120]}"
            )
            time.sleep(2)
            return self._draw_once(prompt, ratio, session)

    def _draw_once(self, prompt: str, ratio: str = "", session: str = "0") -> str:
        """同步调 CLI 出图，返回本地图片路径。**阻塞函数，调用方必须扔线程里。**

        2026-10-19 实测出来的正确两步流程（照文档猜会踩坑）：
          1. `text2image` 只**提交**任务，它**不接受** --download_dir；
          2. 拿 submit_id 反复 `query_result --download_dir` 轮询，
             成功那一次才会把图下到本地。
        （第一版把 --download_dir 塞给 text2image，CLI 直接报 unknown flag。）

        登录态由 CLI 自己管（~/.dreamina_cli），本模块不碰 token。
        """
        bin_path = str(self._cfg("image_gen_bin", "") or IMAGE_GEN_BIN)
        out_dir = Path(str(self._cfg("image_gen_dir", "") or IMAGE_GEN_DIR))
        out_dir.mkdir(parents=True, exist_ok=True)
        timeout = max(30, self._cfg_int("image_gen_timeout", IMAGE_GEN_TIMEOUT))
        model = str(self._cfg("image_gen_model", "") or IMAGE_GEN_MODEL)
        # 优先用消息里点的比例，没点才用面板默认
        if not ratio:
            ratio = str(self._cfg("image_gen_ratio", "") or IMAGE_GEN_RATIO)
        res = str(self._cfg("image_gen_resolution", "") or IMAGE_GEN_RESOLUTION)

        def _run(args: list[str]) -> dict:
            proc = subprocess.run(
                [bin_path, *args], capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
            raw = (proc.stdout or "").strip()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"CLI 退出码 {proc.returncode}："
                    f"{(proc.stderr or raw or '').strip()[:300]}"
                )
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"CLI 输出不是 JSON：{raw[:200]}")

        submit = _run([
            "text2image",
            "--prompt", prompt,
            "--ratio", ratio,
            "--resolution_type", res,
            "--model_version", model,
            "--generate_num", "1",
            "--session", str(session or "0"),   # 按人分框（见 _draw_session_sync）
            "--poll", "0",          # 只提交，轮询自己控制（要带 download_dir）
        ])
        submit_id = str(submit.get("submit_id") or "").strip()
        if not submit_id:
            raise RuntimeError(f"没拿到 submit_id：{str(submit)[:200]}")

        # 轮询：即梦排队 + 生成通常 30~90 秒，超时由 image_gen_timeout 兜
        deadline = time.time() + timeout
        last_status = submit.get("gen_status")
        while time.time() < deadline:
            ret = _run([
                "query_result",
                "--submit_id", submit_id,
                "--download_dir", str(out_dir),
            ])
            status = str(ret.get("gen_status") or "").strip()
            last_status = status
            if status == "success":
                imgs = ((ret.get("result_json") or {}).get("images") or [])
                paths = [
                    str(i.get("path") or "") for i in imgs if i.get("path")
                ]
                # 只认真的落在磁盘上的文件，别把临时 URL 当本地路径
                paths = [p for p in paths if Path(p).exists()]
                if paths:
                    return paths[0]
                raise RuntimeError("任务成功但没拿到可用的本地图片")
            if status in ("failed", "fail", "error"):
                reason = str(ret.get("fail_reason") or status)
                raise RuntimeError(f"生成失败：{reason}")
            time.sleep(5)
        raise RuntimeError(f"生成超时（最后状态 {last_status or '未知'}）")

    async def _run_draw(
        self, group_id: str, umo: str, prompt: str, nickname: str,
        ratio: str = "", sender_id: str = "",
    ) -> None:
        """后台跑一次生图并回图。异常全兜住，绝不冒到事件循环。"""
        try:
            # 先定"这个人的即梦分类框"（缓存里有就不问 CLI）
            session = self._draw_sessions.get(sender_id, "")
            if not session and sender_id:
                try:
                    session = await asyncio.to_thread(
                        self._draw_session_sync, nickname, sender_id
                    )
                    if session:
                        self._draw_sessions[sender_id] = session
                except Exception as exc:  # noqa: BLE001
                    # 分框失败不该让整次生图失败，退回默认会话
                    logger.warning(
                        f"workbuddy_bridge: [生图] 分类框获取失败，改用默认会话: {exc}"
                    )
            logger.info(
                f"workbuddy_bridge: [生图] {group_id} 开始：{prompt[:40]!r}"
                f" 比例={ratio or '默认'} 分类框={session or '默认'}"
                f" 用户={nickname}({sender_id})"
            )
            path = await asyncio.to_thread(self._draw_sync, prompt, ratio, session)
            # 即梦会拿提示词给会话改名，出图后改回来（保住名字里的 QQ 号，
            # 否则下次按 QQ 号查不到、会重复建框）
            if session:
                await asyncio.to_thread(
                    self._draw_session_rename_sync, session, nickname, sender_id
                )
            # 回图：一条文本 + 一张图，走已有的主动发送出口（带 umo 校验）
            # ⚠️ 必须包成 MessageChain：context.send_message 要的是链对象，
            # 直接传 list 会抛 `'list' object has no attribute 'chain'`（2026-10-19 实测）。
            # 2026-10-20 管理员要求：自画像回图**不带提示词**，只报"拍好了"。
            if prompt.startswith(DRAW_SELFIE_PROMPT):
                text = random.choice(
                    ("照片拍好了", "画好了，新照片出炉", "拍好了，收图")
                )
            else:
                text = f"画好了，{nickname}说的「{prompt[:30]}」"
            chain = MessageChain(chain=[
                Plain(text),
                Image(file=path),
            ])
            ok = await self._send_chain_to_session(umo, chain)
            logger.info(
                f"workbuddy_bridge: [生图] {group_id} "
                f"{'成功' if ok else '发送失败'}：{Path(path).name}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: [生图] {group_id} 失败: {exc}")
            # 失败提示必须留痕（2026-10-20）：原先这里 except: pass 静默，
            # 18:13 那次失败后群里什么也没收到、日志里也查不到发没发，
            # 管理员体感就是"自拍挂了却没人说一句"。
            try:
                sent = await self._send_to_session(
                    umo, f"图没画出来：{str(exc)[:120]}"
                )
                if not sent:
                    logger.warning(
                        f"workbuddy_bridge: [生图] {group_id} 失败提示没发出去"
                    )
            except Exception as send_exc:  # noqa: BLE001
                logger.warning(
                    f"workbuddy_bridge: [生图] {group_id} 失败提示发送异常: {send_exc}"
                )

    def _maybe_draw(self, event, text: str, sender_id: str) -> str:
        """生图入口。

        返回值三种：
        ""          —— 不是生图请求，继续走后面的流程
        "draw"      —— 已安排后台任务，调用方回一句"画着呢"
        其他字符串  —— 被闸拦下，调用方把这句话直接回给群友
                       （2026-10-19：cd 中不再沉默落回问答，先回"生图cd中"）
        """
        # 自画像优先（2026-10-20）：「生成一张你的照片」若先走普通解析，
        # prompt 会变成字面的"你的照片"——必须先判自画像类请求。
        prompt = _draw_selfie_prompt_of(text) or _draw_prompt_of(text)
        if not prompt:
            return ""
        group_id = str(event.get_group_id() or "").strip()
        umo = str(event.unified_msg_origin or "")
        if not umo:
            return ""
        reason = self._draw_allowed(sender_id, group_id)
        if not reason:
            # 内容红线：涉及未成年人的性化请求直接拒，连模型都不送
            hit = _hit_words(prompt, DRAW_BLOCKED_WORDS)
            if hit:
                logger.warning(
                    f"workbuddy_bridge: [生图] {group_id} 命中红线词：{hit}，拒绝生成"
                )
                return "这个我不画"
        # 提示词长度检查（即梦 API 上限 1600 字符，实测 2673 被拒）
        if len(prompt) > DRAW_PROMPT_MAX_LEN:
            logger.info(
                f"workbuddy_bridge: [生图] {group_id} prompt 超长："
                f"{len(prompt)} > {DRAW_PROMPT_MAX_LEN}，拒绝生成"
            )
            return (
                f"提示词太长了，发了 {len(prompt)} 字，上限 {DRAW_PROMPT_MAX_LEN}，"
                f"精简一下再试"
            )
        if reason:
            logger.info(
                f"workbuddy_bridge: [生图] {group_id} 命中但不画：{reason}"
            )
            return _draw_block_reply(reason)
        self._draw_bump(group_id)
        nickname = str(event.get_sender_name() or "").strip() or "群友"
        ratio = _draw_ratio_of(text)
        asyncio.create_task(
            self._run_draw(group_id, umo, prompt, nickname, ratio, sender_id)
        )
        return "draw"

    def _maybe_keyword_bubble(self, event, text: str) -> bool:
        """群里出现话题关键词时，要不要凑一句。返回 True 表示已安排后台任务。

        刻意**不进 woken 闸**：那是"对方在叫我"的判定。这里的消息是发给别人
        的，我只能旁观，所以不能把原文当提问直接答 —— 那样会答非所问，
        显得像在偷听。正确做法是丢后台，取材后自己说一句，或者闭嘴。
        """
        group_id = str(event.get_group_id() or "").strip()
        if not group_id or not self._kw_allowed(group_id):
            return False
        # 已被叫到（@/引用/戳一戳）时走正常问答，不重复冒泡。
        if _is_at_me(event) or _is_reply_to_me(event):
            return False
        word = _hit_words(text, self._kw_words())
        if not word:
            return False
        reason = self._kw_gate(group_id)
        if reason:
            logger.info(
                f"workbuddy_bridge: [冒泡] {group_id} 命中「{word}」但{reason}，不接"
            )
            return False

        umo = str(event.unified_msg_origin or "")
        if not umo:
            return False
        self._kw_bump(group_id)
        logger.info(
            f"workbuddy_bridge: [冒泡] {group_id} 命中「{word}」，安排接话"
        )
        # create_task 而不是直接 await：这条消息不该因为我要想一句话
        # 而被拖住。异常在协程内兜住，别冒泡到框架事件循环。
        asyncio.create_task(self._keyword_spark(group_id, umo, word))
        return True

    async def _keyword_spark(self, group_id: str, umo: str, word: str) -> None:
        """关键词触发的一次主动接话。素材口径与冷场闲聊完全一致。"""
        try:
            min_lines = self._cfg_int(
                "keyword_bubble_min_source_lines", KEYWORD_BUBBLE_MIN_SOURCE_LINES
            )
            got = await self._spark_material(group_id, min_lines, "冒泡")
            if not got:
                return
            hist, lines = got
            # 带上"我上一轮在这群说过什么"，防止冒泡复读自己（与 _spark_chat 同口径）
            said = self._spark_said.get(group_id) or []
            mine = str(said[-1].get("text") or "")[:80] if said else ""
            ctx = ""
            if mine:
                ctx = (
                    f"（你上一轮在这群说过：「{mine}」，别重复那套说法）\n"
                )
            prompt = (
                f"{ctx}群里刚才有人聊到了「{word}」相关的话题。\n"
                "下面是这个群最近的聊天记录。\n\n"
                "你**没有被点名**，也不是在回答谁的提问 —— 你是自己凑上去搭腔的。"
                "所以要求是：\n"
                "· 像群里一个刚好也在看手机的熟人，随口接一句\n"
                "· 一两句就够，别写小作文，别列条目，别铺垫\n"
                "· 可以补充、可以吐槽、可以抛个问题回给群里，别自称别总结\n"
                "· 只谈记录里真出现过的内容，别编造别人说过的话\n"
                "· 如果这话题其实没什么可接的，或者你只是路过，"
                "就回一个空字符串，不要硬凑\n\n"
                f"最近的聊天记录：\n{lines}"
            )
            sent = await self._speak_spark(group_id, umo, prompt, "冒泡")
            if not sent:
                return
            ok, reply = sent
            if ok:
                # 2026-10-20 R5：冒泡真发出去了，就把冷场计时清零。
                # 两套计时器互不知情（_kw_last* vs last_spark_ts），不重置的话
                # 同一个群可能半分钟后又走冷场逻辑再开一次口 —— 连着两句像刷屏。
                st = self._group_activity.get(group_id)
                if st is not None:
                    st["last_spark_ts"] = time.time()
            src = " ｜ ".join(h.get("text", "")[:18] for h in hist[-3:])
            logger.info(
                f"workbuddy_bridge: [冒泡] {group_id} 关键词「{word}」"
                f"{'成功' if ok else '失败'}：{reply[:30]!r} ｜ 取材自本群：{src}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: [冒泡] {group_id} 异常: {exc}")

    async def _idle_scan(self) -> None:
        """逐个检查配置的群，够冷场就开口；或在"跟随模式"下接着上一轮继续聊。"""
        now = time.time()
        # 熔断：网关连续失败就别再试了，等reconnect 修好（凭据在网关侧，
        # 这边重试多少次都没用，纯烧钱）。@ 路径不受影响——那是用户主动叫的。
        if getattr(self, "_spark_fail_streak", 0) >= SPARK_FAIL_CIRCUIT_N:
            return
        # 遍历"实际见过消息的群"，而不是写死的列表 —— 新加的群自动纳入
        for group_id, st in list(self._group_activity.items()):
            quiet_for = now - float(st.get("last_ts") or 0)
            # 黑名单优先于一切：这个群连跟随都不许（2026-10-19 管理员点名关的群）。
            if self._blacklisted(group_id):
                continue
            # 冷场触发/冷却**按群取**（2026-10-19）：梗群更密，其余照常。
            trigger_sec, cooldown_sec = self._idle_timing(group_id)
            # 跟随判定要先算：群友正在接机器人刚说的话时，
            # 即便该群没开"适时闲聊"也该继续聊——被 @ 之后就该有回应，
            # 这跟"主动凑热闹"是两回事，不能被同一个开关卡住。
            follow = self._follow_active(group_id, now)
            # 跟随也要过群闸（2026-10-19）：原先只有冷场看开关，跟随是谁说话都跟。
            # 口径与关键词冒泡统一 = 群梗规则的群。
            if follow and not self._follow_allowed(group_id):
                follow = False
            if follow and not self._follow_chain_ok(group_id, now):
                follow = False      # 连击到上限：这波话题不再跟
            if not follow and not self._idle_enabled_for(group_id):
                continue
            if quiet_for < trigger_sec and not follow:
                continue
            # 冷场开口要隔开免得刷屏；但**跟随模式**下群友正在接话，
            # 这时再等冷却就成哑巴了，所以放行。
            if not follow and now - float(st.get("last_spark_ts") or 0) < cooldown_sec:
                continue
            st["last_spark_ts"] = now
            st["last_ts"] = now       # 防下一轮重复触发
            await self._spark_chat(group_id, str(st.get("umo") or ""), follow=follow)

    def _follow_active(self, group_id: str, now: float) -> bool:
        """跟随模式：机器人刚说过话，之后群友还在聊 → 该继续接。

        条件（全部满足才算"这段话还没聊完"）：
        1. 本群最近有过机器人自己的发言（否则谈不上跟随）；
        2. 群友在那之后**确实说了有实义的话**（不是只发图）；
        3. 距群友最后一条有实义的话不超过 FOLLOW_WINDOW_SEC（话题还热着）。

        2026-10-18 收紧：原来用 "last_ts"（任何消息都刷新）判断，
        结果"发图接龙"把跟随模式反复点着——群 2000000002 六分钟接了五次话，
        取材全是图片和"啥子"，最后被人喊「这机器人能不能禁言了」。
        现在只认长度 >= FOLLOW_MIN_TEXT_LEN 的文字消息：图、视频、表情、"6"、
        "？" 都不算"在跟我说话"。
        """
        said = self._spark_said.get(group_id) or []
        if not said:
            return False
        st = self._group_activity.get(group_id) or {}
        last_text_ts = float(st.get("last_text_ts") or 0)
        last_said_ts = float(said[-1].get("ts") or 0)
        # 群友在机器人发言之后又说了有实义的话
        if last_text_ts <= last_said_ts:
            return False
        # 话题还热着（太久了就当新话题，重新走冷场逻辑）
        # 2026-10-19：窗口按群取，梗群更长（FOLLOW_HIFREQ_WINDOW_SEC）。
        _limit, _cool, window, _gap = self._follow_timing(group_id)
        return (now - last_text_ts) <= window

    def _follow_chain_ok(self, group_id: str, now: float) -> bool:
        """跟随连击闸：一波话题最多跟 FOLLOW_MAX_CHAIN 次。

        2026-10-19 加。文字门槛（FOLLOW_MIN_TEXT_LEN）只挡发图，挡不住
        "群友正常聊天"——只要有人说话够长且在我之后，跟随就一直成立，
        于是变成"每有人说话我就插一句"。实测群 2000000004 十二分钟跟了四轮，
        群友直接问「能不能关」。这里给一个跟内容无关的硬上限：
        跟满 N 次后进入冷却，冷却期内一律不跟；
        冷却过了（或话题彻底冷下来）自动清零，重新给机会。
        """
        limit, cool, _window, gap = self._follow_timing(group_id)
        # 0 明确表示"不限制"（面板上写清楚的口径）。必须在这里判，
        # 放到后面会变成 n >= 0 恒成立 = 把跟随彻底关掉，跟本意正好相反。
        if limit <= 0:
            return True
        rec = self._follow_chain.get(group_id) or {}
        n = int(rec.get("n") or 0)
        last = float(rec.get("ts") or 0)
        # 最短间隔（2026-10-19）：两次跟随之间至少隔这么久。
        # 这是"别每句都跟"的直接实现 —— 话题再热，也要等够间隔才接下一句。
        if gap > 0 and last and (now - last) < gap:
            return False
        # 冷却期早就过了 → 说明这波话题已经翻篇，计数清零重新算
        if n and last and (now - last) >= cool:
            n = 0
            self._follow_chain.pop(group_id, None)
        # 话题冷了（群友最后一句实义文字在我这轮之前）→ 也是新话题，清零。
        # 注意必须 last_text > 0 才判：这个群压根没有文字记录（没走到
        # _note_group_activity）不等于"话题冷了"，那种情况下 last_text=0 会
        # 被 0 <= last 误判成冷场，导致计数每轮都被清零、闸门形同虚设。
        st = self._group_activity.get(group_id) or {}
        last_text = float(st.get("last_text_ts") or 0)
        if n and last and last_text > 0 and last_text <= last:
            n = 0
            self._follow_chain.pop(group_id, None)
        if n >= limit:
            return False
        # 占用一次连击（**判定通过就记**，跟 _kw_bump 同理：宁可少说不可多说）
        self._follow_chain[group_id] = {"n": n + 1, "ts": now}
        return True

    async def _spark_material(
        self, group_id: str, min_lines: int, tag: str, follow: bool = False
    ) -> tuple[list[dict], str] | None:
        """为「主动开口」取素材。

        返回 (hist, 给模型看的文本块)；素材不足/全是图则返回 None（此时闭嘴）。
        冷场闲聊与关键词冒泡共用这一段 —— 两者的差别只在"为什么触发"，
        取材口径必须一致，否则同一个群会出现两种说话风格。
        """
        now = time.time()
        # 素材来源：冷场触发时**现拉**最近消息（NapCat 历史接口，2026-10-08 管理员要求）；
        # 拉不到再回退到平时缓存的流水账（现拉失败不该让闲聊直接熄火）。
        size = self._cfg_int("idle_chat_history_size", IDLE_HISTORY_SIZE)
        hist: list[dict] = []
        why = "现拉未开启"
        if self._cfg_bool("idle_live_history", IDLE_LIVE_HISTORY):
            hist, why = await self._fetch_recent_messages(group_id, size)
        if not hist:
            ttl = self._cfg_int("idle_source_ttl", IDLE_SOURCE_TTL)
            raw_hist = self._gag_history.get(group_id) or []
            fresh = [h for h in raw_hist if now - float(h.get("ts") or 0) <= ttl]
            hist = fresh[-size:]
            if hist:
                # 把失败原因一起打出来 —— 否则只剩"回退缓存"四个字，
                # 现拉坏掉半年都看不出来（2026-10-18 教训）。
                logger.info(
                    f"workbuddy_bridge: [{tag}] {group_id} 现拉失败（{why}），"
                    f"回退缓存素材 {len(hist)} 条"
                )
        if not hist:
            logger.info(f"workbuddy_bridge: [{tag}] {group_id} 近期没人说话，跳过")
            return None

        # 取材门槛（2026-10-09 加）：素材不足就别硬接。
        # 实测出现过"现拉失败 → 缓存只剩 1 条且是张图"照样开口，
        # 结果只能回"这张图单看有点接不上"—— 那不是闲聊，是露怯。
        # 跟随模式放宽到1 条：群友在接你的话，一句也该接得住。
        if len(hist) < min_lines:
            logger.info(
                f"workbuddy_bridge: [{tag}] {group_id} 素材只有 {len(hist)} 条"
                f"（<{min_lines}）{'，跟随模式' if follow else ''}不接"
            )
            return None
        if IDLE_REQUIRE_TEXT and not _has_textual_material(hist):
            # 2026-10-15：闲聊能读图了，所以"全是图"不再是死路——
            # 只要能下到图，就可以看图说话（原来一刀切不接，观感是"群里发图它装死"）。
            has_img = IDLE_ALLOW_IMAGE and any(
                str(h.get("img_url") or "").strip() for h in hist
            )
            if not has_img:
                logger.info(
                    f"workbuddy_bridge: [{tag}] {group_id} 素材全是图/表情，无可续话题，不接"
                )
                return None

        lines = "\n".join(
            f"- {h.get('name') or h.get('sender')}: {h.get('text')}"
            for h in hist
        )
        # 读图（2026-10-15 管理员要求）：素材里有图就下最近那一张给 agent 看。
        # 挑"最新的一张"而不是第一张——聊天往后推进，图也是后面的更相关。
        if IDLE_ALLOW_IMAGE:
            for h in reversed(hist):
                u = str(h.get("img_url") or "").strip()
                if not u:
                    continue
                local = await self._fetch_recent_image(u)
                if local:
                    lines = (
                        f"{lines}\n\n【群里最新的图片已存到本地，请先读取再回答】\n"
                        f"- {local}"
                    )
                break   # 只取最新一张，够用也不浪费
        return hist, lines

    async def _speak_spark(
        self, group_id: str, umo: str, prompt: str, tag: str, follow: bool = False
    ) -> None:
        """问模型 → 过熔断/空回复闸 → 发送 → 记账。所有「主动开口」的共同出口。

        返回值约定（注意签名上标了 None，实际有两类返回）：
          · 成功发出 → 返回 (ok: bool, reply: str)，供调用方记日志
          · 其余路径（异常 / 熔断 / 空回复 / 清洗后为空）→ 隐式返回 None
        调用方用 `if not sent: return` 区分两类，勿只看注解。

        熔断计数与 _spark_said 共用：任一路径连续踩到网关异常都该停手，
        分开计会让两条路径互相"假装还健康"。
        """
        try:
            conv_id = _conv_id(umo, 0)
            async with self._lock(umo):
                reply = await self._ask(prompt, conv_id, master=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: {tag}生成失败: {exc}")
            return

        # 网关异常时 _ask 返回的是兜底文案（[凭据失效]/[网关不可达]），
        # 那是给日志看的，绝不能当"接话"发进群（2026-10-07 实测踩坑）。
        if _is_empty_reply(reply or ""):
            # ⚠️ 必须把"[排队]"和"真故障"分开（2026-10-18 实测踩坑）：
            # 管理员在 WorkBuddy 里跟 AI 说话时，桌面端会话被占着，QQ 的
            # 冒泡请求全排在后面、240 秒等不到轮到自己。此前一律当"网关异常"
            # 累计，三次就熔断掉**全部**主动发言——于是"管理员越用 AI，机器人
            # 越闭嘴"，而真实原因是排队，不是坏了。排队不计数、不熔断，
            # 下一轮冷场自然重试。
            if "[排队]" in (reply or ""):
                logger.info(
                    f"workbuddy_bridge: [{tag}] {group_id} 桌面端会话正忙、本轮排队"
                    "未轮到，不计入熔断（下一轮冷场自动重试）"
                )
                return
            # 真故障要熔断：网关挂了（端口变/凭据失效）时，否则每 30 秒
            # 重试一次、白烧 5 分钟 token，群里还一点动静都没有。
            # 熔断后彻底静默，等管理员跑 reconnect 再自动恢复。
            self._spark_fail_streak = getattr(self, "_spark_fail_streak", 0) + 1
            logger.info(
                f"workbuddy_bridge: [{tag}] {group_id} 网关异常/空回复，跳过"
                f"（连续第 {self._spark_fail_streak} 次）"
            )
            if self._spark_fail_streak == SPARK_FAIL_CIRCUIT_N:
                logger.error(
                    f"workbuddy_bridge: 主动发言连续 {SPARK_FAIL_CIRCUIT_N} 次网关异常，"
                    f"本插件暂停主动发言。多半是 WorkBuddy 重启换了端口——"
                    f"请让 AI 跑一次 reconnect.py 修好（不用重启 AstrBot）"
                )
            return

        # 成功即清零熔断计数
        self._spark_fail_streak = 0

        reply = _sanitize(reply or "")
        if not reply.strip():
            logger.info(f"workbuddy_bridge: [{tag}] {group_id} 这轮没什么好接的，跳过")
            return

        ok = await self._send_to_session(umo, reply)
        # 记下自己说过什么，供下一轮延续话题（只留最近 3 条，够接话就行）
        if ok and reply.strip():
            bucket = self._spark_said.setdefault(group_id, [])
            bucket.append({"text": reply[:200], "ts": time.time()})
            if len(bucket) > 3:
                del bucket[:-3]
        # 把取材一并记下来 —— 万一观感上"串台"，能立刻定位是谁接谁
        return ok, reply

    async def _spark_chat(self, group_id: str, umo: str, follow: bool = False) -> None:
        """顺着刚才的聊天记录，主动往群里接一句。

        follow=True 表示这是"跟随上一轮"——群友正在接机器人刚说的话，
        这时不受冷却限制，尽快接上。
        """
        if not umo:
            return
        # 全局节流：避免多个群在短时间内接连被"开口"，观感上像串台。
        # 跟随模式例外：对方在追问，拖着不回最尴尬。
        now = time.time()
        if not follow:
            gap = self._cfg_int("idle_global_gap_sec", IDLE_GLOBAL_GAP_SEC)
            if now - self._last_spark_global < gap:
                return
        self._last_spark_global = now

        tag = "闲聊"
        min_lines = 1 if follow else self._cfg_int(
            "idle_min_source_lines", IDLE_MIN_SOURCE_LINES
        )
        got = await self._spark_material(group_id, min_lines, tag, follow=follow)
        if not got:
            return
        hist, lines = got
        # 带上"我上轮说了什么 + 群里有没有人接"，让这轮能延续而不是重开话题。
        said = self._spark_said.get(group_id) or []
        if follow and said:
            mine = str(said[-1].get("text") or "")[:80]
            prompt = (
                "你刚在这群里说过一句话，群里有人接了，话题还在继续。\n"
                f"你上一句说的是：「{mine}」\n\n"
                "下面是群里最新的消息。**顺着这个话头继续聊**，"
                "就像群友真的在跟你对话一样。要求：\n"
                "· 直接回应他们说的话，别再重复你上轮已经说过的内容\n"
                "· 像群里熟人那样随口说，一两句就够\n"
                "· 不要自我介绍、不要说「作为 AI」、不要总结\n"
                f"\n群里最新消息：\n{lines}"
            )
        else:
            echo = ""
            if said:
                last = said[-1]
                replied = any(
                    float(h.get("ts") or 0) > float(last.get("ts") or 0)
                    for h in hist
                )
                if replied:
                    echo = (
                        f"你上一轮在这群说过：「{str(last.get('text') or '')[:60]}」\n"
                        f"之后有人接了话（下面记录里就有），这轮顺着往下聊。"
                    )
                else:
                    echo = (
                        f"你上一轮在这群说过：「{str(last.get('text') or '')[:60]}」\n"
                        f"但群里当时没人接。这次换个话题，别重复上轮那套。"
                    )
                lines = f"{echo}\n\n群里的聊天记录：\n{lines}"
            prompt = (
                "群里已经安静了一会儿。下面是刚才的聊天记录，\n"
                "请**自然地插一句话**把气氛续上。要求：\n"
                "· 像群里熟人那样随口说，一两句就够，别长篇大论\n"
                "· 可以顺着刚才的话题接、抛个小问题、或者就某个点吐个槽\n"
                "· 不要自我介绍、不要说「作为 AI」、不要总结、不要点名\n"
                "· 如果确实没什么好接的，就回一个空字符串\n\n"
                f"最近的聊天记录：\n{lines}"
            )
        sent = await self._speak_spark(group_id, umo, prompt, tag)
        if not sent:
            return
        ok, reply = sent
        # 把取材一并记下来 —— 万一观感上"串台"，能立刻定位是谁接谁
        src = " ｜ ".join(h.get("text", "")[:18] for h in hist[-3:])
        logger.info(
            f"workbuddy_bridge: [{tag}] {group_id} "
            f"{'跟随接话' if follow else '冷场开口'}"
            f"{'成功' if ok else '失败'}：{reply[:30]!r} ｜ 取材自本群：{src}"
        )

    async def _maybe_clean_attachments(self) -> None:
        """按间隔惰性清理过期附件（默认保留 2 天）。

        「惰性循环」的含义：没有独立定时器，挂在每条消息的处理路径上，
        靠 _last_clean_ts 节流（ATTACHMENT_CLEAN_INTERVAL_SEC 一次）。
        群里彻底没人说话时就完全不清 —— 反正没人说话也不会有新垃圾。
        两个清理对象：自己的附件目录 + AstrBot 的 temp 媒体缓存（后者见
        _clean_old_temp 的安全约束，确认不了路径就宁可不删）。
        """
        if ATTACHMENT_KEEP_DAYS <= 0:
            return
        now = time.time()
        if now - self._last_clean_ts < ATTACHMENT_CLEAN_INTERVAL_SEC:
            return
        self._last_clean_ts = now
        removed = await asyncio.to_thread(_clean_old_attachments)
        if removed:
            logger.info(
                f"workbuddy_bridge: [清理] 删除过期附件 {removed} 个"
                f"（保留 {ATTACHMENT_KEEP_DAYS} 天）"
            )
        # 顺手清 AstrBot 的媒体缓存 —— 那个目录没人管，是唯一会无限涨的
        if TEMP_KEEP_DAYS > 0:
            removed, freed = await asyncio.to_thread(_clean_old_temp)
            if removed:
                logger.info(
                    f"workbuddy_bridge: [清理] 删除过期媒体缓存 {removed} 个"
                    f"释放 {_mb(freed)}（保留 {TEMP_KEEP_DAYS} 天，"
                    f"当前仍占 {_mb(await asyncio.to_thread(_temp_usage))}）"
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
        entries = hist[-(self.CONTEXT_HISTORY_LINES + 1):]
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
            "所在的同一个群，用来衔接语境，不算\"其他会话\"）：\n"
            + "\n".join(lines)
            + "\n【要回答的消息】"
        )

    async def _gag_critique(self, event, rule: dict, conv_id: str) -> str:
        """结合目标账号的最近发言，生成一句调侃式的锐评。"""
        group_id = str(event.get_group_id() or "").strip()
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
        return await self._ask(prompt, conv_id, master=False)

    # ---------------------------------------------------------- 读图上下文
    def _remember_recent_images(self, event) -> None:
        """记下群里的图片链接 —— **只记地址，不下载**。

        用途是"接着上文看图"；真正的下载发生在被 @ 的那一刻，
        所以"不 @ 不下载"这条规矩没有被破坏。
        """
        if not IMAGE_CONTEXT_ENABLED:
            return
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return
        comps = [c for c in (event.get_messages() or []) if isinstance(c, Image)]
        if not comps:
            return
        bucket = self._recent_images.setdefault(group_id, [])
        for comp in comps:
            url = str(
                getattr(comp, "url", "") or getattr(comp, "file", "") or ""
            ).strip()
            if not url:
                continue
            bucket.append(
                {
                    "url": url,
                    "sender": str(event.get_sender_id() or "").strip(),
                    "name": event.get_sender_name() or "",
                    "ts": time.time(),
                }
            )
        if len(bucket) > RECENT_IMAGE_LIMIT:
            del bucket[:-RECENT_IMAGE_LIMIT]

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

    def _recent_context_image_comp(self, event):
        """把"群里刚发过、还在有效期内"的那张图包成 Image 组件返回。"""
        if not IMAGE_CONTEXT_ENABLED:
            return None
        group_id = str(event.get_group_id() or "").strip()
        bucket = self._recent_images.get(group_id) or []
        if not bucket:
            return None
        newest = bucket[-1]
        if time.time() - float(newest.get("ts") or 0) > IMAGE_CONTEXT_TTL:
            return None
        url = str(newest.get("url") or "").strip()
        if not url:
            return None
        logger.info("workbuddy_bridge: [上下文图] 捎上最近一张群图片")
        return Image(file=url)

    async def _collect_attachments(
        self, event, sender_name: str, sender_id: str, text: str = ""
    ) -> list[str]:
        """把本条消息里的附件落到本地，返回绝对路径列表。

        群里默认只有 @ 了机器人才读；不 @ 的直接无视——不下载、不处理。
        但有两种情况例外，它们本身就是"被叫到"的证据：
          · **引用回复**里带的图（别人引着一张图来 @ 机器人）
          · **上下文顺延**：刚有人发过图，随后 @ 机器人说"看看这张"
        """
        if not READ_ATTACHMENTS:
            return []

        targets = _attachment_components(event)
        referenced = self._referenced_image_comps(event)
        is_at = _is_at_me(event)

        if (
            GROUP_ATTACHMENT_REQUIRE_AT
            and not event.is_private_chat()
            and not is_at
            and not referenced
        ):
            if targets:
                logger.info(
                    f"workbuddy_bridge: [附件跳过] {sender_id} 发了 {len(targets)} 个附件"
                    "但没 @ 机器人，已无视"
                )
            return []

        comps: list = list(targets) + list(referenced)

        # 本条没带图，但被 @ 了、且话里提到"图"——把最近那张群图片捎上
        if not comps and is_at and _mentions_image(text):
            ctx = self._recent_context_image_comp(event)
            if ctx is not None:
                comps.append(ctx)

        if not comps:
            return []
        targets = comps

        out_dir = ATTACHMENT_ROOT / _safe_attachment_dir(
            sender_name or "用户", sender_id
        )
        stamp = time.strftime("%Y%m%d_%H%M%S")
        saved: list[str] = []
        for idx, comp in enumerate(targets[:MAX_ATTACHMENTS_PER_MSG], 1):
            try:
                local = await _materialize_attachment(comp)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"workbuddy_bridge: 附件取用失败: {exc}")
                continue
            if not local or not os.path.exists(local):
                logger.warning(f"workbuddy_bridge: 附件拿不到本地文件: {local!r}")
                continue
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                # copy2 保留 mtime —— 附件清理按 mtime 判过期，
                # 用 copy 会让老附件"复活"成新文件、永远清不掉
                dest = out_dir / f"{stamp}_{idx}{Path(local).suffix or ''}"
                shutil.copy2(local, dest)
                saved.append(str(dest))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"workbuddy_bridge: 附件转存失败: {exc}")

        if saved:
            skipped = len(targets) - len(saved)
            tail = f"（另有 {skipped} 个未取到）" if skipped > 0 else ""
            logger.info(
                f"workbuddy_bridge: [附件] 落盘 {len(saved)} 个 -> {out_dir}{tail}"
            )
        return saved

    # ------------------------------------------------------------ 事件入口
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
        if _is_paused():
            event.stop_event()
            return

        # ---- 第一件事：确认"我还是原来那个号" ----
        # 登录错号的后果是把回复发进无关的群（历史事故），
        # 所以每次回复前都对一遍：号不对就整条静默丢弃，宁可装死。
        if EXPECTED_SELF_ID:
            me_now = str(event.get_self_id() or "").strip()
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
                yield event.plain_result(random.choice(EMPTY_AT_REPLIES))
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
                if silent_others and str(event.get_sender_id() or "").strip() not in ALLOWED_SENDERS:
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
                yield event.plain_result(reply)
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
            yield event.plain_result(random.choice(POKE_REPLIES))
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
            yield event.plain_result(random.choice(IMAGE_GEN_ACK))
            return
        if draw_ret:
            event.stop_event()
            yield event.plain_result(draw_ret)
            return

        umo = event.unified_msg_origin
        if text in RESET_KEYWORDS:
            self._generation[umo] = self._generation.get(umo, 0) + 1
            event.stop_event()
            yield event.plain_result("已重置本会话的上下文。")
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

        # ---- 管理员指令：重载插件（运维兜底，优先级最高）----
        reload_reply = self._handle_reload_cmd(text, master)
        if reload_reply is not None:
            event.stop_event()
            yield event.plain_result(reload_reply)
            return

        # ---- 管理员指令：代发到指定群 ----
        relay_reply = await self._handle_relay_cmd(event, text, master)
        if relay_reply is not None:
            event.stop_event()
            yield event.plain_result(relay_reply)
            return

        # ---- 角色扮演指令：扮演 xxx 与 出戏 ----
        rp_reply = self._handle_roleplay_cmd(event, text, sender_id, master)
        if rp_reply is not None:
            event.stop_event()
            yield event.plain_result(rp_reply)
            return

        # ---- 群专属梗规则（命中关键词 / @ 到目标账号）----
        gag_rule = _hit_group_gag(event, text)
        if gag_rule:
            quick = (gag_rule.get("quick_reply") or "").strip()
            if gag_rule.get("critique") and random.random() < 0.5:
                logger.info("workbuddy_bridge: [梗规则] 锐评（结合最近发言）")
                try:
                    async with self._lock(umo):
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
            yield event.plain_result(_sanitize(reply))
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
                yield event.plain_result(REFUSE_TEXT)
                return

        # 顺手清理过期附件（内部有节流，不会每条消息都扫盘）
        try:
            await self._maybe_clean_attachments()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: 附件清理异常: {exc}")

        # ---- 附件：图片 / 语音 / 视频 / 文件落到本地 ----
        # 群里不 @ 机器人就完全无视，避免随便一张图都被拉下来处理。
        try:
            attachments = await self._collect_attachments(
                event, sender_name, sender_id, text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: 附件处理异常: {exc}")
            attachments = []

        # ---- 审批闸：要动电脑的事，必须管理员点头 ----
        pending = self._pending.get(umo) or []

        # 过期的待审批先作废 —— 免得一句"同意"把很久以前的旧请求放行了
        if pending:
            now_ts = time.time()
            alive = [
                r
                for r in pending
                if now_ts - float(r.get("ts") or 0) <= PENDING_TTL_SEC
            ]
            if len(alive) != len(pending):
                logger.info(
                    f"workbuddy_bridge: [待审批] {len(pending) - len(alive)} 条超时作废"
                    f"（有效期 {PENDING_TTL_SEC}s）"
                )
                pending[:] = alive
                if not pending:
                    self._pending.pop(umo, None)

        if master and pending:
            # 注意：这里必须用 _hit_approve —— 它会把"不放行""别批准"排除掉。
            # 只做子串匹配的话，"不放行是对的"会被判成批准（真出过事）
            if _hit_approve(text):
                req = pending.pop(0)
                if not pending:
                    self._pending.pop(umo, None)
                logger.info(
                    f"workbuddy_bridge: [放行] 管理员批准 {req['sender_id']} 的请求："
                    f"{req['text'][:40]!r}"
                )
                try:
                    async with self._lock(umo):
                        reply, produced = await self._run_approved(req, conv_id)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"workbuddy_bridge: 执行获批任务失败: {exc}")
                    reply, produced = f"[内部错误] {type(exc).__name__}: {exc}", []
                if not reply or not reply.strip():
                    reply = "[无回复] 没有拿到执行结果。"

                # ---- 把新产出的文件回传给"提出需求的那个群" ----
                # 目标就是当前 umo（审批队列按会话隔离，天然指向来源群）。
                # 发送前再做一次硬校验：只认群聊、且走 _send_chain_to_session
                # 内部的 umo 一致性检查 —— 发错群比不发更糟。
                if self._cfg_bool("relay_back_output", RELAY_BACK_OUTPUT) and produced \
                        and "GroupMessage" in str(umo):
                    parts = [Plain(_sanitize(reply))]
                    sent_files = 0
                    max_files = self._cfg_int("relay_output_max_files", RELAY_OUTPUT_MAX_FILES)
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
                        f"workbuddy_bridge: [回传] 群{group_id} 附带 {sent_files} 个文件"
                        f" → {'成功' if ok else '失败'}"
                    )
                    event.stop_event()
                    return

                event.stop_event()
                yield event.plain_result(_sanitize(reply))
                return

            if _hit_words(text, REJECT_WORDS):
                req = pending.pop(0)
                if not pending:
                    self._pending.pop(umo, None)
                logger.info(
                    f"workbuddy_bridge: [驳回] 管理员拒绝 {req['sender_id']} 的请求"
                )
                event.stop_event()
                yield event.plain_result(f"好，这条不做了：{req['text'][:30]}")
                return

            # 队列里压着东西，但这句话既不像放行也不像驳回。
            # 多半是"想批准但说法没对上"（实测："可以执行"就不在词表里，
            # 结果被当成普通消息飘走了）。所以这里明确回一句，别让它悬着。
            if 0 < len(text) <= 15:
                logger.info(
                    f"workbuddy_bridge: [审批待定] 管理员发言未命中放行/驳回词："
                    f"{text!r}，已提示"
                )
                event.stop_event()
                yield event.plain_result(
                    f"队列里还有一条等着你点头：{pending[0]['text'][:24]}\n"
                    "要放行就回「同意」，不要就回「不行」。"
                )
                return

        if not master:
            hit = _is_action_request(text)
            if hit:
                self._pending.setdefault(umo, []).append(
                    {
                        "sender_id": sender_id,
                        "sender_name": sender_name,
                        "text": text,
                        "attachments": attachments,
                        "ts": time.time(),      # 用于超时作废
                    }
                )
                logger.info(
                    f"workbuddy_bridge: [待审批] {sender_id}({sender_name}) "
                    f"提出操作类需求（命中 {hit!r}），已登记但未转发"
                    f"（{PENDING_TTL_SEC}s 内无人处理即作废）"
                )
                event.stop_event()
                yield event.plain_result(
                    f"这条我先记下了：{text[:40]}\n"
                    "真要动电脑的话得等管理员点头 —— 如果只是逗我玩，那就当我没说。"
                )
                return

        ask_text = text
        if attachments:
            listing = "\n".join(f"- {p}" for p in attachments)
            ask_text = (
                f"{text or '（对方只发了附件，没有文字）'}\n\n"
                "【对方随消息发来的附件，已经存到本地，请先读取再回答】\n"
                f"{listing}"
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

        try:
            async with self._lock(umo):
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

        if not reply or not reply.strip():
            reply = "[无回复] WorkBuddy 没有返回内容。"

        # @ 路径也要记账（2026-10-15）：否则「@ 开话题 → 我答 → 群友接话 →
        # 该继续聊」这条链断在第一环——_follow_active 查不到我说过什么，
        # 跟随模式永不触发。口径与主动开口一致。
        if group_id and not _is_empty_reply(reply):
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
                    yield event.plain_result(REDIRECT_NOTICE)
            else:
                # 转移失败时不能把正文发进群（那正是要避免的），只提示一句。
                yield event.plain_result("（这条我没能私聊发给你，稍后再试一次）")
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
        if _is_empty_reply(reply):
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
                yield event.plain_result(
                    random.choice(ATTACHMENT_SILENT_REPLY)
                )
                return
            logger.info(
                f"workbuddy_bridge: [跳过] {where} 这轮没有可读文本，不发"
            )
            event.stop_event()
            return

        logger.info(
            f"workbuddy_bridge: [回复] 机器人={self_id} {where} "
            f"发信人={sender_id} 原长={raw_len} 清洗后={len(reply)}"
        )

        event.stop_event()
        yield event.plain_result(reply)
