"""Pure QQ reply formatting and privacy filters, independent of bot state."""

import re

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

REPEAT_MAX_UNIT = 12  # 循环节最长多少字符（142857 六个，够用）
REPEAT_MIN_REPEATS = 4  # 至少重复几次才认定为循环
REPEAT_MIN_SPAN = 40  # 累计多少字符才动手（太短的巧合不误伤）


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


# ---- 出口过滤用的正则（全部在 _sanitize 里使用）----
# 每条正则对应一类"绝不该出现在 QQ 群里"的内容；误伤容忍度极低，
# 匹配模式都尽量收紧（带行首锚点 / 特定前缀），宁可漏不可错删。
_LOG_TAG_LINE = re.compile(  # 带 [Core]/[INFO] 等方括号标签的整行日志
    r"^\s*\[(?:Core|INFO|ERRO|ERROR|WARN|DEBUG|OneBot|AdapterManager"
    r"|workbuddy_bridge|napcat-qq|pipeline|respond\.stage)[^\]]*\]\s*.*$",
    re.M | re.I,
)
_LOG_TIME_LINE = re.compile(  # 以时间戳开头的整行日志（可选带前置标签）
    r"^\s*(?:\[[^\]]{0,40}\]\s*)?(?:\[\d{4}-\d{2}-\d{2}[^\]]*\]|\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s*.*$",
    re.M,
)
_LEADIN_LINE = re.compile(
    r"^[^\n]{0,60}(?:日志|如下|印证|输出|结果)[^\n]{0,20}[：:]\s*$", re.M
)  # 「日志如下：」这类引导行
_LOCAL_URL = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?\S*", re.I
)  # 本机 URL 整体
_LOCAL_HOST = re.compile(
    r"\b(?:127\.0\.0\.1|localhost|0\.0\.0\.0):\d{2,5}\b", re.I
)  # 主机:端口
_PORT_MENTION = re.compile(
    r"(?:端口|port)[^\d\n]{0,10}\d{2,5}", re.I
)  # 「端口 62022」字样
_ABS_PATH = re.compile(r"[A-Za-z]:\\[^\s，。；、）)】\]\"'`]+")  # Windows 绝对路径
_QQ_NUM = re.compile(r"(?<!\d)\d{9,11}(?!\d)")  # 9-11 位纯数字当 QQ 号
_EMPTY_FENCE = re.compile(r"```[^\n`]*\n[ \t\r\n]*```")  # 空代码块（洗完正文剩下的壳）
_BLANK_RUN = re.compile(r"\n{3,}")  # 三连以上空行压成两连

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
_MD_BOLD_LEFT = re.compile(r"\*\*(.+)$", re.MULTILINE)  # 落单的开标记
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
    out = out.replace("**", "")  # 处理完配对后，残留的一律清掉

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


def sanitize(text: str, *, max_chars: int) -> str:
    """Apply the general reply filter using caller-provided settings.

    Args:
        text: Model response before formatting and internal detail filtering.
        max_chars: Current response length limit; nonpositive disables trimming.

    Returns:
        The filtered response, or the original text when it is empty.
    """
    if not text:
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
    out, _cut = _smart_trim(out, max_chars)
    return out


# 非管理员回复中被整行清除的内部用词（注意：不含"管理员/主人"，
# 否则连"只有管理员可以授权"这种拒绝语都会被删掉）
STRICT_STRIP_WORDS = (
    "密码",
    "密钥",
    "token",
    "apikey",
    "api key",
    "api_key",
    "secret",
    "localhost",
    "127.0.0.1",
    "端口",
    "日志",
    "配置文件",
    "源码",
    "安装目录",
    "文件路径",
    "部署",
    "服务器地址",
)

# 非管理员回复的隐私脱敏正则（在 _strict_filter 里使用）：
# 邮箱 / 手机号 / IPv4 一律打码——模型可能从某处学到真实联系方式，不能外发。
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def strict_filter(
    text: str, *, max_chars: int, refuse_text: str, strip_words: tuple[str, ...]
) -> str:
    """Apply the additional privacy filter for non-admin replies.

    Args:
        text: Response after the general reply filter.
        max_chars: Current non-admin response length limit.
        refuse_text: Reply used when filtering leaves almost no content.
        strip_words: Current internal terms whose containing lines are removed.

    Returns:
        The filtered response or the supplied refusal text.
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
        if not any(w.lower() in line.lower() for w in strip_words)
    ]
    out = _BLANK_RUN.sub("\n\n", "\n".join(kept)).strip()
    # 删完几乎不剩字 → 说明整条都在讲内部信息，换成标准拒绝语
    if len(out) < 4:
        return refuse_text
    # The caller supplies the current non-admin response limit.
    out, _cut = _smart_trim(out, max_chars)
    return out
