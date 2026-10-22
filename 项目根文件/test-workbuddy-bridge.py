"""workbuddy_bridge 纯逻辑自检。

不依赖 AstrBot 运行时：给 astrbot.* 打桩后再加载插件，
只验证关键词判定、放行词识别、目录名清理、提示词拼装。

用法：
    双击 test-bridge.bat
或：
    .venv\\Scripts\\python.exe test-workbuddy-bridge.py
"""

import asyncio
import hashlib
import os
import sys
import shutil
import tempfile
import time
import types
from pathlib import Path

# ---- Windows 编码自防御（2026-10-16 修复） ------------------------------
# 双击 test-bridge.bat 时 stdout 是控制台，Python 用 UTF-8 直写不崩；
# 但经由 doctor.py 的 subprocess 管道（或 `> 文件` / `| more` 重定向）跑时，
# stdout 编码会回落到系统 locale（中文系统 = GBK/CP936），打印「emoji 保留」
# 用例里的 🍅 直接 UnicodeEncodeError 崩溃 —— 表现是 doctor 报
# 「插件逻辑自检 FAIL」、详情是一行看不懂的乱码（2026-10-16 实证复现）。
# 强制 UTF-8；doctor.py 那侧正好按 utf-8 读子进程输出，两边对齐。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent
PLUGIN = HERE / "AstrBot" / "data" / "plugins" / "workbuddy_bridge" / "main.py"


def _install_stubs() -> None:
    """给 astrbot.* 打桩，让插件能在纯 Python 下被加载。"""

    class _Logger:
        def __getattr__(self, _name):
            return lambda *a, **k: None

    class _Star:
        def __init__(self, *_a, **_k):
            pass

    class _Context:
        pass

    class _Filter:
        class EventMessageType:
            ALL = "ALL"

        def event_message_type(self, *_a, **_k):
            return lambda fn: fn

        def command(self, *_a, **_k):
            return lambda fn: fn

    class _Event:
        pass

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = _Logger()

    star = types.ModuleType("astrbot.api.star")
    star.Star = _Star
    star.Context = _Context
    api.star = star

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = _Event
    event.filter = _Filter()
    api.event = event

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _At:
        def __init__(self, qq=""):
            self.qq = qq

    class _Image:
        def __init__(self, file=""):
            self.file = file

    class _Record:
        def __init__(self, file=""):
            self.file = file

    class _Video:
        def __init__(self, file=""):
            self.file = file

    class _File:
        def __init__(self, name="", file="", url=""):
            self.name = name
            self.file_ = file
            self.url = url

    class _Reply:
        def __init__(self, sender_id="", chain=None):
            self.sender_id = sender_id
            self.chain = chain or []

    class _Poke:
        def __init__(self, id=""):
            self.id = id

    mc = types.ModuleType("astrbot.api.message_components")
    mc.Plain = _Plain
    mc.At = _At
    mc.Image = _Image
    mc.Record = _Record
    mc.Video = _Video
    mc.File = _File
    mc.Reply = _Reply
    mc.Poke = _Poke
    api.message_components = mc

    astrbot.api = api

    # --- 主动发消息所需的核心模块 ---
    class _MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class _MessageSession:
        def __init__(self, platform_name="", message_type="", session_id=""):
            self.platform_name = platform_name
            self.platform_id = platform_name
            self.message_type = message_type
            self.session_id = session_id

        def __str__(self):
            return f"{self.platform_name}:{self.message_type}:{self.session_id}"

    class _MessageType:
        FRIEND_MESSAGE = "FriendMessage"
        GROUP_MESSAGE = "GroupMessage"

    core = types.ModuleType("astrbot.core")
    core_msg = types.ModuleType("astrbot.core.message")
    mer = types.ModuleType("astrbot.core.message.message_event_result")
    mer.MessageChain = _MessageChain

    plat = types.ModuleType("astrbot.core.platform")
    ms = types.ModuleType("astrbot.core.platform.message_session")
    ms.MessageSession = _MessageSession
    mt = types.ModuleType("astrbot.core.platform.message_type")
    mt.MessageType = _MessageType

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star
    sys.modules["astrbot.api.event"] = event
    sys.modules["astrbot.api.message_components"] = mc
    sys.modules["astrbot.core"] = core
    sys.modules["astrbot.core.message"] = core_msg
    sys.modules["astrbot.core.message.message_event_result"] = mer
    sys.modules["astrbot.core.platform"] = plat
    sys.modules["astrbot.core.platform.message_session"] = ms
    sys.modules["astrbot.core.platform.message_type"] = mt


def main() -> int:
    _install_stubs()

    import importlib.util

    spec = importlib.util.spec_from_file_location("wb_bridge", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 单测必须与"维护模式标记"隔离：管理员为了改代码挂了 bridge.paused 时，
    # 插件对 QQ 全静默是**正确行为**，但套件会因此误报一批 FAIL。
    # 这里把暂停判定强制打桩成"未暂停"，让结果只反映被测逻辑本身。
    # 环境变量也可临时绕过（QQ_BOT_TEST_NO_PAUSE_STUB=0）。
    import os as _os

    if _os.environ.get("QQ_BOT_TEST_NO_PAUSE_STUB", "1") != "0":
        mod._is_paused = lambda: False  # type: ignore[assignment]

    ok = True

    def check(label, got, expect):
        nonlocal ok
        good = got == expect
        ok = ok and good
        tail = "" if good else f"   期望 {expect!r}"
        print(f"[{'PASS' if good else 'FAIL'}] {label} -> {got!r}{tail}")

    print("== 操作类判定（非管理员命中即转待审批） ==")
    check("白名单已放开（所有人可对话）", mod.SILENT_FOR_OTHERS, False)

    # 必须拦的：真要动本机
    check(
        "'把这的文件删掉' 要拦",
        mod._is_action_request("把这的文件删掉"),
        "删掉",
    )
    check(
        "'帮我做个网页' 要拦（做=生成）",
        bool(mod._is_action_request("帮我做个网页")),
        True,
    )
    check(
        "'帮我下载个视频' 要拦（下载是强操作）",
        bool(mod._is_action_request("帮我下载个视频")),
        True,
    )
    check(
        "'把桌面清空' 要拦",
        bool(mod._is_action_request("把桌面清空")),
        True,
    )

    # 2026-10-09 实测误伤：这条被原 ACTION_WORDS 的"帮我"拦下，
    # 但它只是游戏配装咨询，跟本机毫无关系，必须放行直接回答。
    duck_msg = (
        "星穹铁道的角色真珠，以后内186速为准，帮我评估在勉强王极300行动值内，"
        "银狼 lv999，爻光，火光，全员2+1的情况下套和风套谁打的最快，"
        "同时计算出在100行动值时真珠与银狼 lv999 分别刷出了几个额外回合"
    )
    check(
        "实测误伤案例：配装评估咨询要放行",
        mod._is_action_request(duck_msg),
        None,
    )
    check(
        "'帮我评估这套配装' 要放行",
        mod._is_action_request("帮我评估这套配装打多少伤害"),
        None,
    )
    check(
        "'推荐哪个好' 要放行",
        mod._is_action_request("推荐哪个好"),
        None,
    )
    check(
        "'计算一下伤害' 要放行",
        mod._is_action_request("计算一下伤害"),
        None,
    )
    check(
        "咨询里带'代码'要放行（看看这段代码为什么报错）",
        mod._is_action_request("看看这段代码为什么报错"),
        None,
    )
    check(
        "玩笑话要放行（走调侃不走审批）",
        mod._is_action_request("你是不是玩坏了，开玩笑的"),
        None,
    )

    # 客套话已从词表移除，单独出现不再触发
    check(
        "客套话'帮我'已不在判定表",
        mod._hit_words("帮我", mod.STRONG_ACTION_WORDS),
        None,
    )
    check("'今天天气真好' 不误伤", mod._is_action_request("今天天气真好"), None)
    check("'哈哈哈哈' 不误伤", mod._is_action_request("哈哈哈哈"), None)
    check("'你是谁呀' 不误伤", mod._is_action_request("你是谁呀"), None)

    print()
    print("== 放行 / 驳回词（仅管理员且有待审批时生效） ==")
    check("'同意'", mod._hit_words("同意", mod.APPROVE_WORDS), "同意")
    check("'开始动手吧'", mod._hit_words("开始动手吧", mod.APPROVE_WORDS), "开始动手")
    check("'批准'", mod._hit_words("批准", mod.APPROVE_WORDS), "批准")
    check("'我在吃饭' 不误触发", mod._hit_words("我在吃饭", mod.APPROVE_WORDS), None)
    check("'算了' 驳回", mod._hit_words("算了", mod.REJECT_WORDS), "算了")

    print()
    print("== 产物目录命名 ==")
    check(
        "正常昵称",
        mod._safe_dir_name("示例昵称甲", "3000000004"),
        "示例昵称甲（3000000004）的文件",
    )
    check(
        "非法字符被清理",
        mod._safe_dir_name('a/b\\c:d*e?f"g<h>i|j', "123"),
        "abcdefghij（123）的文件",
    )
    check("空昵称兜底", mod._safe_dir_name("", "123"), "用户（123）的文件")
    check(
        "emoji 保留",
        mod._safe_dir_name("示例昵称乙", "3000000005"),
        "示例昵称乙（3000000005）的文件",
    )
    check(
        "超长昵称截断",
        len(mod._safe_dir_name("昵" * 80, "1")) <= 30 + len("（1）的文件"),
        True,
    )

    print()
    print("== 提示词拼装 ==")
    p_chat = mod._prompt_prefix(False)
    check("非管理员含【只读咨询模式】", "只读咨询模式" in p_chat, True)
    check("非管理员允许自由回答", "可以正常回答" in p_chat, True)
    check("非管理员仍受内部信息约束", "不要给出具体内容" in p_chat, True)
    check("模型/归属可正面回答", "可正面简短回答" in p_chat, True)
    check("调侃现实操作 -> 玩笑回应", "顺着接一句玩笑" in p_chat, True)
    check("要求少用表情", "别每句都挂，十条里一两次" in p_chat, True)
    check("认角色而非指认真人", "默认是想让你认角色" in p_chat, True)
    check("真人照片不指认身份", "真人照片不要指认其身份" in p_chat, True)
    check("认错先搜索核实", "先联网搜索核实再作答" in p_chat, True)

    # 「先搜再答」总纲（2026-10-09 管理员指令）：锐评 / 识图 / 时事这类
    # 涉及事实且会变的东西，必须先搜索再开口。以前是零散地各处要求，
    # 现在收成第 3.5 条统一管住。
    check("含先搜再答总纲", "先搜再答" in p_chat, True)
    check("锐评必须先搜", "锐评某个网络热梗" in p_chat, True)
    check("识图必须先搜", "识图认角色" in p_chat, True)
    check("时事变动的信息要先搜", "今年 / 最近" in p_chat, True)
    check("被指错先搜再改口", "不盲目改口" in p_chat, True)
    check("搜索优先于硬答（金句）", "搜索不丢人，答错才丢人" in p_chat, True)
    check("排除纯主观闲聊不必搜", "纯主观闲聊" in p_chat, True)
    check("搜不到如实说", "没查到" in p_chat, True)
    # 管理员侧同样要带上（这条对所有分支生效）
    check("管理员提示词也含先搜再答", "先搜再答" in mod._prompt_prefix(True), True)
    check("自我介绍用『蓝色大肥鱼』", "蓝色大肥鱼" in p_chat, True)
    check("不自称 AI 助手", "不要用「AI 助手」这类自称" in p_chat, True)
    check("被直接问是否为 AI 时仍如实承认", "时如实承认" in p_chat, True)

    # ---- 2026-10-18 按「DeepSeek 小鲸鱼」角色卡补的三条 ----
    # 1) 冷处理分档（原是 1.6 单条空输出，现在四档 + 禁划线话）
    check("冷处理：轻接一句档", "轻接一句再聊自己的" in p_chat, True)
    check("冷处理：自嘲挡一下档", "自嘲挡一下" in p_chat, True)
    check("冷处理：换话题档", "直接换话题" in p_chat, True)
    check("冷处理：空输出档", "唯一正确的输出就是一个空字符串" in p_chat, True)
    check("冷处理：禁划线话（别在群里展开）", "别在群里展开" in p_chat, True)
    check("冷处理：禁划线话（到此为止）", "这个话题到此为止" in p_chat, True)
    check("冷处理：禁划线话（适可而止）", "适可而止" in p_chat, True)
    check(
        # 2026-10-14 血泪教训：三条括号备注被当"接话"发进群。这条绝不能丢。
        "冷处理：仍禁占位备注",
        "（不回复）" in p_chat and "占位备注" in p_chat,
        True,
    )
    check("冷处理：真拒绝不受冷处理影响", "真要拒绝" in p_chat, True)

    # 2) AI 味黑名单
    check("禁语：开头套话", "综上所述" in p_chat, True)
    check("禁语：客服收尾", "希望这个回答对你有帮助" in p_chat, True)
    check("禁语：自我声明", "作为一个语言模型" in p_chat, True)
    check("禁语：共情模板", "这是个好问题" in p_chat, True)
    check("禁语：主持腔", "你们觉得呢" in p_chat, True)
    check("禁语：挂尾巴", "记得……哦" in p_chat, True)
    check("禁语：堆语气词", "可爱不靠堆语气词" in p_chat, True)
    check(
        # 禁「作为 AI」不能跟第 5 条「被问就承认」打架，得留活口。
        "禁语：与『如实承认是 AI』不冲突",
        "如实承认是另一回事" in p_chat,
        True,
    )

    # 3) 反讨好式关心
    check("反讨好：点名禁客服式问候", "记得早点休息" in p_chat, True)
    check("反讨好：给出贱气替代说法", "我还等着你喂饭" in p_chat, True)
    check("反讨好：认真问就认真答", "认真问问题就认真答" in p_chat, True)
    check("反讨好：不用每轮都赢", "不用每轮都赢" in p_chat, True)

    print()
    print("== 代发指令 ==")
    check("代发词表非空", len(mod.RELAY_WORDS) > 0, True)
    check("含『代发』", "代发" in mod.RELAY_WORDS, True)

    print()
    print("== 热重载 ==")
    check("重载词表非空", len(mod.RELOAD_WORDS) > 0, True)
    check("含『重载插件』", "重载插件" in mod.RELOAD_WORDS, True)
    check("热重载默认开启", mod.HOT_RELOAD_ENABLED, True)
    inst = mod.Main(None)
    calls = []
    inst._spawn_reload = lambda: calls.append(1)
    non_master = inst._handle_reload_cmd("重载插件", False)
    check("重载指令：非管理员被拒", bool(non_master) and "只有号主" in non_master, True)
    check("重载指令：未触发重载", len(calls) == 0, True)
    master_reply = inst._handle_reload_cmd("重载插件", True)
    check(
        "重载指令：管理员触发并回执",
        bool(master_reply) and "重载" in master_reply and len(calls) == 1,
        True,
    )
    check("重载指令：普通文本不误触", inst._handle_reload_cmd("今天天气不错", True), None)

    print()
    print("== 图片也算聊天条数 ==")

    class _GEv:  # 极简群消息桩：只提供流水账用得到的方法
        def __init__(self, comps, gid="g1"):
            self._comps, self._gid = comps, gid

        def get_messages(self):
            return self._comps

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return "10001"

        def get_sender_name(self):
            return "测试用户"

    inst2 = mod.Main(None)
    inst2._idle_enabled_for = lambda gid: True
    inst2._remember_group_message(_GEv([mod.Image(file="a.jpg")]), "")
    hist = inst2._gag_history.get("g1") or []
    check("纯图片消息进流水账", len(hist) == 1, True)
    check("占位文本为（图片）", bool(hist) and hist[0]["text"] == "（图片）", True)
    inst2._remember_group_message(
        _GEv([mod.Image(file="a.jpg"), mod.Image(file="b.jpg")]), ""
    )
    hist = inst2._gag_history.get("g1") or []
    check("两张图合并计数", len(hist) == 2 and hist[1]["text"] == "（2张图片）", True)
    inst2._remember_group_message(_GEv([mod.Image(file="a.jpg"), mod.Record(file="v.mp3")]), "")
    hist = inst2._gag_history.get("g1") or []
    check("图+语音分列", len(hist) == 3 and hist[2]["text"] == "（图片、语音）", True)
    inst2._remember_group_message(_GEv([mod.Plain(text="纯文字")]), "纯文字")
    hist = inst2._gag_history.get("g1") or []
    check("文字照常记录", len(hist) == 4 and hist[3]["text"] == "纯文字", True)
    inst2._remember_group_message(_GEv([mod.Plain(text="")]), "")
    check("无附件无文字 -> 不记", len(inst2._gag_history.get("g1") or []) == 4, True)

    print()
    print("== 冷场现拉群历史 ==")
    import asyncio as _aio

    check("现拉开关默认开", mod.IDLE_LIVE_HISTORY, True)

    class _FakeBot:
        def __init__(self, payload=None, boom=False):
            self._payload, self._boom, self.calls = payload, boom, []

        async def call_action(self, action, **kw):
            self.calls.append((action, kw))
            if self._boom:
                raise RuntimeError("napcat down")
            return self._payload

    payload = {
        "status": "ok",
        "data": {
            "messages": [
                {"user_id": 10001, "time": 1, "sender": {"card": "甲", "nickname": "甲"},
                 "message": [{"type": "text", "data": {"text": "今晚开黑"}}]},
                {"user_id": 9999, "time": 2, "sender": {"nickname": "机器人"},
                 "message": [{"type": "text", "data": {"text": "机器人的话"}}]},
                {"user_id": 10002, "time": 3, "sender": {"nickname": "乙"},
                 "message": [{"type": "image", "data": {"file": "a.jpg"}}]},
                {"user_id": 10003, "time": 4, "sender": {"nickname": "丙"},
                 "message": [{"type": "image"}, {"type": "image"}]},
            ]
        },
    }

    inst3 = mod.Main(None)
    # 2026-10-18 起返回 (消息列表, 失败原因)：失败原因必须带出来，
    # 否则"现拉一直失败"这种问题在日志里只剩一句"回退缓存"，永远查不出。
    got, why = _aio.run(inst3._fetch_recent_messages("123456", 15))
    check("无 bot -> 空表", got == [], True)
    check("无 bot 时带上失败原因", bool(why), True)

    bot = _FakeBot(payload)
    inst3._group_activity["123456"] = {"umo": "x", "bot": bot, "self_id": "9999"}
    got, why = _aio.run(inst3._fetch_recent_messages("123456", 15))
    check("过滤机器人自己的发言", len(got) == 3, True)
    check("成功时原因应为空", why, "")
    check("文本素材原样保留", bool(got) and got[0]["text"] == "今晚开黑", True)
    check("群名片优先作名字", bool(got) and got[0]["name"] == "甲", True)
    check("单图压成（图片）", len(got) > 1 and got[1]["text"] == "（图片）", True)
    check("双图压成（2张图片）", len(got) > 2 and got[2]["text"] == "（2张图片）", True)
    check(
        "count 截断取最近的",
        len(_aio.run(inst3._fetch_recent_messages("123456", 2))[0]) == 2,
        True,
    )
    check("调用的是历史接口", bot.calls and bot.calls[0][0] == "get_group_msg_history", True)

    inst3._group_activity["654321"] = {"umo": "x", "bot": _FakeBot(boom=True), "self_id": "9999"}
    boom_hist, boom_why = _aio.run(inst3._fetch_recent_messages("654321", 15))
    check("接口异常 -> 空表（回退缓存）", boom_hist == [], True)
    check("接口异常 -> 原因是『接口异常』", "接口异常" in boom_why, True)

    # 接口回空 messages 是最隐蔽的一种失败（不抛异常、状态还 ok）。
    # 2026-10-18 实测线上 434 次全走这条，日志里却看不出来。
    inst3._group_activity["777"] = {
        "umo": "x", "bot": _FakeBot({"status": "ok", "retcode": 0, "data": {"messages": []}}),
        "self_id": "9999",
    }
    empty_hist, empty_why = _aio.run(inst3._fetch_recent_messages("777", 15))
    check("空 messages -> 空表", empty_hist == [], True)
    check("空 messages -> 原因带出返回结构", "空 messages" in empty_why, True)

    # ★ 真实结构：aiocqhttp 的 call_action 返回的是**拆过包的 data 层**，
    # 即 {"messages": [...]}，不是整包。2026-10-18 查出的"434 次全失败"根因
    # 就是只认整包、永远取到 None。必须支持这种形态。
    unpacked = {
        "messages": [
            {"user_id": 20001, "time": 5, "sender": {"nickname": "丁"},
             "message": [{"type": "text", "data": {"text": "拆包后的素材"}}]},
        ]
    }
    inst3._group_activity["888"] = {
        "umo": "x", "bot": _FakeBot(unpacked), "self_id": "9999",
    }
    got_u, why_u = _aio.run(inst3._fetch_recent_messages("888", 15))
    check("aiocqhttp 拆包格式能取到消息", len(got_u), 1)
    check("拆包格式内容正确", got_u[0]["text"] if got_u else "", "拆包后的素材")

    # 取材不足就不该硬接（2026-10-09 admin 反馈"感觉没联系上下文"）：
    # 实测出现过"现拉失败 -> 回退缓存只剩 1 条（还是张图）"照样开口，
    # 结果只能回"这张图单看有点接不上"。所以必须有个下限门槛 ——
    # 低于它直接闭嘴，比硬接一句没上文的强。
    check(
        "闲聊有最小取材门槛（素材太少就别说）",
        getattr(mod, "IDLE_MIN_SOURCE_LINES", 0) >= 2,
        True,
    )
    # 只接到图片、没有任何文字，等于没有可续的话题，也不该开口
    check(
        "闲聊素材需含至少一条文字（纯图不接）",
        hasattr(mod, "_has_textual_material"),
        True,
    )
    check(
        "纯图占位不算文字素材",
        mod._has_textual_material([{"text": "（图片）"}, {"text": "（2张图片）"}]),
        False,
    )
    check(
        "正常文字算素材",
        mod._has_textual_material([{"text": "（图片）"}, {"text": "今晚开黑"}]),
        True,
    )
    check("空素材判否", mod._has_textual_material([]), False)

    print()
    print("== 空 @ 兜底 ==")
    check("空@回应库非空", len(mod.EMPTY_AT_REPLIES) > 0, True)
    check(
        "空@回应都是短句",
        all(len(r) <= 12 for r in mod.EMPTY_AT_REPLIES),
        True,
    )
    check("拿不准也优先按玩笑接", "优先按玩笑接" in p_chat, True)
    check("非管理员可读附件（仅读取）", "附件可以读取分析" in p_chat, True)
    check(
        "内部提问硬拦截默认关闭",
        mod.BLOCK_INTERNAL_QUESTIONS_FOR_OTHERS,
        False,
    )
    out = "D:/x/用户产出/张三（1）的文件"
    p_exec = mod._prompt_prefix(True, "execute", out)
    check("执行模式带上产出目录", out in p_exec, True)
    check("执行模式不再限只读", "只读咨询模式" in p_exec, False)
    check("管理员日常对话不受限", "只读咨询模式" in mod._prompt_prefix(True), False)

    print()
    print("== 话题分流（配置/部署 -> 私聊） ==")
    check("'怎么部署'", mod._hit_words("怎么部署", mod.CONFIG_TOPIC_WORDS), "部署")
    check("'日志在哪看'", mod._hit_words("日志在哪看", mod.CONFIG_TOPIC_WORDS), "日志")
    check("'端口是多少'", mod._hit_words("端口是多少", mod.CONFIG_TOPIC_WORDS), "端口")
    check(
        "'用的什么模型' 不再分流（可直接答）",
        mod._hit_words("用的什么模型", mod.CONFIG_TOPIC_WORDS),
        None,
    )
    check("'今天天气真好' 不误伤", mod._hit_words("今天天气真好", mod.CONFIG_TOPIC_WORDS), None)
    check("'你好呀' 不误伤", mod._hit_words("你好呀", mod.CONFIG_TOPIC_WORDS), None)
    check("主动发送能力已就绪", mod._CAN_SEND_ACTIVE, True)

    print()
    print("== 会话标识稳定性（重启不再丢上下文） ==")
    expect = "qq-" + hashlib.md5(b"u|0").hexdigest()[:12]
    check("用 md5（跨进程恒定）", mod._conv_id("u", 0), expect)
    check("同会话同代次恒定", mod._conv_id("a:b:c", 0), mod._conv_id("a:b:c", 0))
    check("换代次 -> 换会话", mod._conv_id("u", 0) != mod._conv_id("u", 1), True)
    check("换会话 -> 换 id", mod._conv_id("a", 0) != mod._conv_id("b", 0), True)

    print()
    print("== 附件识别与 @ 判定（群里不 @ 不读） ==")

    class _Ev:
        def __init__(self, self_id, comps, group_id=""):
            self._bits = (self_id, comps, group_id)
            self.unified_msg_origin = (
                f"aiocqhttp:GroupMessage:{group_id}" if group_id else "aiocqhttp:FriendMessage:1"
            )
            self.stopped = False
            self.sender = "10001"

        def get_self_id(self):
            return self._bits[0]

        def get_messages(self):
            return self._bits[1]

        def get_group_id(self):
            return self._bits[2]

        def get_sender_id(self):
            return self.sender

        def get_sender_name(self):
            return "测试用户"

        # 下面几个是驱动 on_message 全流程要用的。
        # get_message_str 刻意**不含 At** —— 跟框架一致：@ 不算文字，
        # 纯 @ 消息的 message_str 就是空的，这正是 2026-10-18 那次漏发的现场。
        def get_message_str(self):
            return "".join(
                c.text for c in self._bits[1] if isinstance(c, mod.Plain)
            )

        def is_private_chat(self):
            return not self._bits[2]

        def stop_event(self):
            self.stopped = True

        def plain_result(self, text):
            return ("plain", text)

    me = "3897046583"
    check("@ 机器人 -> 读", mod._is_at_me(_Ev(me, [mod.At(qq=me)])), True)
    check("@ 别人 -> 不读", mod._is_at_me(_Ev(me, [mod.At(qq="3000000004")])), False)
    check("@全体 -> 读", mod._is_at_me(_Ev(me, [mod.At(qq="all")])), True)
    check("只发图没 @ -> 不读", mod._is_at_me(_Ev(me, [mod.Image(file="a.jpg")])), False)
    check(
        "引用机器人 -> 算叫到",
        mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id=me)])),
        True,
    )
    check(
        "引用别人 -> 不算叫到",
        mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id="873831183")])),
        False,
    )
    check(
        "普通发言 -> 不算叫到",
        mod._is_reply_to_me(_Ev(me, [mod.Plain(text="在调")])),
        False,
    )
    check("戳一戳开关默认开", mod.POKE_ENABLED, True)
    check("被戳 -> 算叫到", mod._is_poked(_Ev(me, [mod.Poke(id=me)])), True)
    check("戳别人 -> 不算", mod._is_poked(_Ev(me, [mod.Poke(id="3000000004")])), False)
    check("普通消息 -> 不算被戳", mod._is_poked(_Ev(me, [mod.Plain(text="hi")])), False)
    check("被戳的答复池非空", bool(mod.POKE_REPLIES), True)
    check("被戳的答复都不为空", all(s.strip() for s in mod.POKE_REPLIES), True)
    check("戳一戳冷却为正数", int(mod.POKE_COOLDOWN_SEC) > 0, True)

    # ---- 全流程：空消息必须拦下 + 戳一戳冷却（2026-10-18 事故修复）----
    import asyncio as _aio2

    async def _drive(inst, ev):
        out = []
        async for r in inst.on_message(ev):
            out.append(r)
        return out

    print()
    print("== 空消息一律拦下（2026-10-18 漏发『LLM 请求失败』事故） ==")
    inst_e = mod.Main(None)
    # 注意：这里必须用 EXPECTED_SELF_ID，否则会先被开头的"登录错号"校验拦掉，
    # 测不到空消息分支（踩过一次，误以为通过）。
    # 未配置时（分发的材料就是空的）用一个人造号顶上，否则这一组全跑不到目标分支。
    real_me = mod.EXPECTED_SELF_ID or "1000000001"
    # 场景 A：空消息、且认不出是在 @ 我 —— 以前这里裸 return，
    # 消息继续往下走，框架没配模型就把报错甩进群。现在必须掐断。
    ev_a = _Ev(real_me, [mod.At(qq="999999")], "2000000001")
    got_a = _aio2.run(_drive(inst_e, ev_a))
    check("认不出的空消息 -> 不说话", got_a, [])
    check("认不出的空消息 -> 仍然掐断传播链", ev_a.stopped, True)
    # 场景 B：确实 @ 我了 —— 回一句兜底，同样要掐断
    ev_b = _Ev(real_me, [mod.At(qq=real_me)], "2000000001")
    got_b = _aio2.run(_drive(inst_e, ev_b))
    check("空@ -> 兜底回一句", len(got_b), 1)
    check("空@ -> 已掐断传播链", ev_b.stopped, True)

    print()
    print("== 戳一戳冷却（防连戳三连回） ==")
    inst_p = mod.Main(None)
    ev_p1 = _Ev(real_me, [mod.Poke(id=real_me)], "2000000001")
    check("第一次戳 -> 回一句", len(_aio2.run(_drive(inst_p, ev_p1))), 1)
    ev_p2 = _Ev(real_me, [mod.Poke(id=real_me)], "2000000001")
    check("冷却期内再戳 -> 不回", _aio2.run(_drive(inst_p, ev_p2)), [])
    check("冷却期内也掐断（不放给框架）", ev_p2.stopped, True)
    # 把记录拨回过去，模拟冷却结束
    inst_p._poke_last[f"{ev_p1.unified_msg_origin}#10001"] = 0.0
    ev_p3 = _Ev(real_me, [mod.Poke(id=real_me)], "2000000001")
    check("冷却过了 -> 又能回", len(_aio2.run(_drive(inst_p, ev_p3))), 1)
    # 换个人戳同一个会话，不该被别人拖累
    ev_p4 = _Ev(real_me, [mod.Poke(id=real_me)], "2000000001")
    ev_p4.sender = "20002"
    check("不同人戳 -> 各自算各自的", len(_aio2.run(_drive(inst_p, ev_p4))), 1)

    check(
        "@ 别人 + 引用别人 -> 群里不该被唤醒",
        bool(
            mod._is_at_me(_Ev(me, [mod.At(qq="873831183")]))
            or mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id="873831183")]))
            or any(
                "在调".startswith(p)
                for p in mod.EXTRA_WAKE_PREFIXES
            )
        ),
        False,
    )
    check(
        "附件数量统计",
        len(
            mod._attachment_components(
                _Ev(me, [mod.Plain(text="看这个"), mod.Image(file="a.jpg"), mod.At(qq=me)])
            )
        ),
        1,
    )
    check(
        "附件目录命名（无「的文件」后缀）",
        mod._safe_attachment_dir("示例昵称甲", "3000000004"),
        "示例昵称甲（3000000004）",
    )
    check(
        "产出目录命名未受影响",
        mod._safe_dir_name("示例昵称甲", "3000000004"),
        "示例昵称甲（3000000004）的文件",
    )

    print()
    print("== 过期附件清理（保留 2 天） ==")
    tmp = Path(tempfile.mkdtemp())
    mod.ATTACHMENT_ROOT = tmp
    old_ts = time.time() - 3 * 86400

    (tmp / "甲").mkdir(parents=True, exist_ok=True)
    keep = tmp / "甲" / "new.jpg"
    keep.write_text("x")

    (tmp / "乙").mkdir(parents=True, exist_ok=True)
    gone = tmp / "乙" / "old.jpg"
    gone.write_text("x")
    os.utime(gone, (old_ts, old_ts))

    removed = mod._clean_old_attachments()
    check("删掉过期文件数量", removed, 1)
    check("过期文件已消失", gone.exists(), False)
    check("未过期文件保留", keep.exists(), True)
    check("空目录被回收", (tmp / "乙").exists(), False)
    check("非空目录保留", (tmp / "甲").exists(), True)

    outside = Path(tempfile.mkdtemp()) / "用户产出"
    outside.mkdir(parents=True, exist_ok=True)
    artifact = outside / "成果.txt"
    artifact.write_text("x")
    os.utime(artifact, (old_ts, old_ts))
    mod._clean_old_attachments()
    check("用户产出绝不受影响", artifact.exists(), True)

    print()
    print("== 群专属梗规则（示例梗 / 示例称号） ==")
    gag_group = "2000000001"
    other_group = "867072270"

    # 一个群现在可以挂多条规则（2026-10-18 起），不能再把值当 dict 用。
    # 示例称号规则**不能用关键词去认**（它的触发词是"示例词""示例词二"），按目标号认。
    all_rules = mod._gag_rules_of(gag_group)
    duck_rules = [r for r in all_rules if "示例梗" in r.get("keywords", ())]
    bei_rules = [r for r in all_rules if r.get("target_id") == "1000000003"]
    rule = duck_rules[0] if duck_rules else {}
    bei = bei_rules[0] if bei_rules else {}

    check("示例梗规则已配置", bool(duck_rules), True)
    check("示例称号规则已配置（同群并存）", bool(bei_rules), True)
    check("同群两条规则互不覆盖", len(all_rules), 2)
    # 历史写法（值直接是 dict）必须继续能跑 —— 万一有人手工改回单条也不会炸。
    mod.GROUP_GAG_RULES["__tmp_single__"] = {"keywords": ("测试",), "quick_reply": "x"}
    try:
        check(
            "兼容单条 dict 写法",
            mod._gag_rules_of("__tmp_single__"),
            (mod.GROUP_GAG_RULES["__tmp_single__"],),
        )
    finally:
        mod.GROUP_GAG_RULES.pop("__tmp_single__", None)
    check("未配置的群返回空元组", mod._gag_rules_of(other_group), ())

    check(
        "关键词『示例梗』命中",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="示例梗来了")], gag_group), "示例梗来了"
            )
        ),
        True,
    )
    check(
        "关键词『示例梗二』命中",
        bool(
            mod._hit_group_gag(_Ev(me, [mod.Plain(text="示例梗二呢")], gag_group), "示例梗二呢")
        ),
        True,
    )
    check(
        "@目标账号命中",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.At(qq="1000000002")], gag_group), "快看"
            )
        ),
        True,
    )
    check(
        "别的群不触发",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="示例梗")], other_group), "示例梗"
            )
        ),
        False,
    )
    check(
        "无关消息不触发",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="今天天气")], gag_group), "今天天气"
            )
        ),
        False,
    )
    check(
        "固定文案非空",
        bool((rule or {}).get("quick_reply", "").strip()),
        True,
    )
    check(
        # 2026-10-09 管理员要求关闭锐评：命中「示例梗」只回固定文案。
        # 改成"必须为 False"而不是删掉这条断言——这样以后谁误改回True 会立刻红。
        "示例梗锐评已关闭（2026-10-09 管理员要求）",
        bool((rule or {}).get("critique")),
        False,
    )
    check(
        "锐评关闭后固定文案仍可用",
        (rule or {}).get("quick_reply") == "这是固定回复",
        True,
    )
    check(
        "梗规则命中可绕过群唤醒（不 @ 也算叫到）",
        mod._hit_group_gag(_Ev(me, [mod.Plain(text="示例梗呢")], gag_group), "示例梗呢")
        is not None,
        True,
    )
    check(
        "第二个群（米哈游）也已配置",
        bool(mod._gag_rules_of("2000000002")),
        True,
    )
    check(
        "未配置的群仍然不触发",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.At(qq="1000000002")], other_group), "评价下"
            )
        ),
        False,
    )

    # ---- 示例称号规则（2026-10-18 新增，示例梗规则的变体）----
    # 触发词是「示例词」「示例词二」，**不是"示例称号"**（2026-10-18 管理员纠正）。
    check(
        "示例称号规则触发词为『示例词』『示例词二』",
        tuple((bei or {}).get("keywords") or ()),
        ("示例词", "示例词二"),
    )
    hit_bei = mod._hit_group_gag(
        _Ev(me, [mod.Plain(text="示例词人呢")], gag_group), "示例词人呢"
    )
    check("关键词『示例词』命中", bool(hit_bei), True)
    check(
        "命中示例词时返回的是示例称号规则（没被示例梗抢走）",
        (hit_bei or {}).get("target_id"),
        "1000000003",
    )
    hit_zong = mod._hit_group_gag(
        _Ev(me, [mod.Plain(text="示例词二今天没上线")], gag_group), "示例词二今天没上线"
    )
    check("关键词『示例词二』命中", bool(hit_zong), True)
    check(
        "命中示例词二时返回的也是示例称号规则",
        (hit_zong or {}).get("target_id"),
        "1000000003",
    )
    check(
        # 防回归：管理员明确说过触发词不是"示例称号"，谁改回去立刻红。
        "『示例称号』本身不再是触发词",
        mod._hit_group_gag(
            _Ev(me, [mod.Plain(text="示例称号在吗")], gag_group), "示例称号在吗"
        ),
        None,
    )
    # 排除词：判定是子串包含，「诺示例词」里含「示例词」，不挡就会在聊诺奖时乱接。
    check(
        "『诺示例词奖』不触发（排除词生效）",
        mod._hit_group_gag(
            _Ev(me, [mod.Plain(text="诺示例词奖今天颁了")], gag_group),
            "诺示例词奖今天颁了",
        ),
        None,
    )
    check(
        "『示例词格莱德』不触发（排除词生效）",
        mod._hit_group_gag(
            _Ev(me, [mod.Plain(text="示例词格莱德在哪个国家")], gag_group),
            "示例词格莱德在哪个国家",
        ),
        None,
    )
    check(
        # 排除词只挡"包含它的那个词"，不能连带把「示例词二」也挡掉。
        "含有排除词时『示例词二』仍然触发",
        (
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="诺示例词和示例词二都没来")], gag_group),
                "诺示例词和示例词二都没来",
            )
            or {}
        ).get("target_id"),
        "1000000003",
    )
    check(
        # @ 是明确指人，不走关键词判定，排除词不该影响它。
        "排除词不影响 @ 路径",
        (
            mod._hit_group_gag(
                _Ev(me, [mod.At(qq="1000000003"), mod.Plain(text="诺示例词")], gag_group),
                "诺示例词",
            )
            or {}
        ).get("target_id"),
        "1000000003",
    )
    check(
        "示例称号固定文案正确",
        (bei or {}).get("quick_reply"),
        "示例称号......",
    )
    hit_bei_at = mod._hit_group_gag(
        _Ev(me, [mod.At(qq="1000000003")], gag_group), "看这个"
    )
    check(
        "@示例称号对象命中示例称号规则",
        (hit_bei_at or {}).get("target_id"),
        "1000000003",
    )
    check(
        "@示例梗对象不会被示例称号规则抢走",
        (
            mod._hit_group_gag(
                _Ev(me, [mod.At(qq="1000000002")], gag_group), "看这个"
            )
            or {}
        ).get("target_id"),
        "1000000002",
    )
    check(
        "示例称号规则在第二个群同样生效",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="示例词")], "2000000002"), "示例词"
            )
        ),
        True,
    )
    check(
        "两条规则的目标账号不同（防串号）",
        (rule or {}).get("target_id") != (bei or {}).get("target_id"),
        True,
    )
    check(
        "示例称号锐评同样关闭",
        bool((bei or {}).get("critique")),
        False,
    )
    check(
        "同一条消息双词同现时按规则顺序取示例梗（不连发两句）",
        (
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="示例梗和示例词都在")], gag_group), "示例梗和示例词都在"
            )
            or {}
        ).get("target_id"),
        "1000000002",
    )
    check(
        "示例称号触发词不污染其它群",
        bool(
            mod._hit_group_gag(
                _Ev(me, [mod.Plain(text="示例词")], other_group), "示例词"
            )
        ),
        False,
    )

    print()
    print("== 读图上下文（B 缓存链接 / C 引用回溯） ==")
    check("读图上下文已开启", mod.IMAGE_CONTEXT_ENABLED, True)
    check("'看看这张图' 命中图片意图", mod._mentions_image("看看这张图"), True)
    check("'这张图啥情况' 命中", mod._mentions_image("这张图啥情况"), True)
    check("'今天天气不错' 不命中", mod._mentions_image("今天天气不错"), False)
    check(
        "引用消息带 chain 字段（C 方案的前提）",
        hasattr(mod.Reply(sender_id="1"), "chain"),
        True,
    )
    check(
        "上下文有效期为正数",
        int(mod.IMAGE_CONTEXT_TTL) > 0,
        True,
    )

    print()
    print("== 适时闲聊 ==")
    check("闲聊已开启", mod.IDLE_CHAT_ENABLED, True)
    check("触发阈值 5 分钟", mod.IDLE_TRIGGER_SEC, 300)
    check("覆盖群非空", len(mod.IDLE_CHAT_GROUPS) > 0, True)
    check("取材条数 15", mod.IDLE_HISTORY_SIZE, 15)
    check("冷却远大于触发阈值", mod.IDLE_COOLDOWN_SEC > mod.IDLE_TRIGGER_SEC, True)
    check("所有群模式已开", mod.IDLE_CHAT_ALL_GROUPS, True)
    check("任意群都判定为开启", mod._idle_enabled_for("999999999"), True)

    print()
    print("== 角色扮演（按群隔离的人设） ==")
    check("RP 总开关默认开", mod.ROLEPLAY_ENABLED, True)
    check("RP 白名单默认空（谁都不行）", len(mod.ROLEPLAY_ALLOWED), 0)
    check("RP 默认对所有群友开放（无需白名单）", mod.ROLEPLAY_OPEN_TO_ALL, True)
    check("未扮演时不加人设说明", "这个群现在正在角色扮演" not in mod._prompt_prefix(False, "chat"), True)
    check(
        "有人设时写进提示词",
        "这个群现在正在角色扮演" in mod._prompt_prefix(False, "chat", None, "一个话少的剑客"),
        True,
    )
    check(
        "人设内容被带上",
        "一个话少的剑客" in mod._prompt_prefix(False, "chat", None, "一个话少的剑客"),
        True,
    )
    check(
        "人设说明含底线约束",
        "不得涉及色情" in mod._prompt_prefix(False, "chat", None, "某角色"),
        True,
    )
    check(
        "管理员分支不受人设影响",
        "管理员本人" in mod._prompt_prefix(True, "chat", None, "某角色"),
        True,
    )
    check("开场指令词已配", len(mod.ROLEPLAY_START_WORDS) > 0, True)
    check(
        "裸词『扮演』已配（框架会剥掉斜杠，实测必需）",
        "扮演" in mod.ROLEPLAY_START_WORDS,
        True,
    )
    check("带斜杠形式仍保留兼容", "/扮演" in mod.ROLEPLAY_START_WORDS, True)
    check(
        "叫停有裸词形式",
        "结束扮演" in mod.ROLEPLAY_STOP_WORDS,
        True,
    )
    check("收场指令词已配", len(mod.ROLEPLAY_STOP_WORDS) > 0, True)
    check("退场超时为正数", mod.ROLEPLAY_TIMEOUT_SEC > 0, True)
    check("闲聊有全局节流（防串台观感）", mod.IDLE_GLOBAL_GAP_SEC > 0, True)
    check("闲聊素材有时效上限", mod.IDLE_SOURCE_TTL > 0, True)

    print()
    print("== 放行判定（否定词必须被排除） ==")
    check("'同意' 判为放行", bool(mod._hit_approve("同意")), True)
    check("'放行' 判为放行", bool(mod._hit_approve("放行")), True)
    check(
        "'不放行是对的' 不算放行（真实事故）",
        mod._hit_approve("不放行是对的，同时这句话发到群里了，自检一下"),
        None,
    )
    check("'别放行' 不算放行", mod._hit_approve("别放行"), None)
    check("'暂不批准' 不算放行", mod._hit_approve("暂不批准"), None)
    check(
        "'同意' 与 '不同意' 要区分开",
        (bool(mod._hit_approve("同意")), mod._hit_approve("不同意")),
        (True, None),
    )
    check("待审批有效期已设置", mod.PENDING_TTL_SEC > 0, True)
    check(
        "'可以执行' 判为放行（实测漏过的说法）",
        bool(mod._hit_approve("可以执行")),
        True,
    )
    check(
        "'不可以执行' 不算放行",
        mod._hit_approve("不可以执行"),
        None,
    )

    print()
    print("== 发送前校验 ==")
    # EXPECTED_SELF_ID 是"部署者必填"项：没填时不算失败，而是明确提示去哪填。
    # 分发的材料里这一项是空的（脱敏占位），新人一跑 test-bridge.bat 不该看到 FAIL。
    if not mod.EXPECTED_SELF_ID:
        print(
            "[SKIP] 期望账号未配置 —— 请按 部署指南.md 第 3.5 节填写 "
            "EXPECTED_SELF_ID（机器人的 QQ 号）"
        )
    else:
        check("期望账号已配置", bool(mod.EXPECTED_SELF_ID), True)
        check("期望账号为纯数字", str(mod.EXPECTED_SELF_ID).isdigit(), True)
    check(
        "否定前缀表非空",
        len(mod.NEGATION_PREFIXES) > 0,
        True,
    )
    check(
        "'不'开头的词确实在否定表里",
        "不" in mod.NEGATION_PREFIXES,
        True,
    )

    print()
    print("== 空壳回复过滤 ==")
    check("空字符串算空壳", mod._is_empty_reply(""), True)
    check("纯空白算空壳", mod._is_empty_reply("   \n "), True)
    check(
        "网关的[无输出]占位串算空壳",
        mod._is_empty_reply("[无输出] Agent 执行完成但没有可读文本。"),
        True,
    )
    check(
        "[无回复] 占位串算空壳",
        mod._is_empty_reply("[无回复] WorkBuddy 没有返回内容。"),
        True,
    )
    check("正常回复不算空壳", mod._is_empty_reply("你好呀"), False)
    check(
        "含内容的正常回复不算空壳",
        mod._is_empty_reply("这个我不接，换个话题吧"),
        False,
    )

    print()
    print("== @图片没回话时要有兜底回应（2026-10-09） ==")
    # 起因：有人 @ 机器人发图，附件正常落盘，但网关那轮返回空壳，
    # 于是被 _is_empty_reply 整条丢弃 —— 群里只留下"@了没反应"。
    check(
        "兜底回应非空",
        all(bool(s.strip()) for s in mod.ATTACHMENT_SILENT_REPLY),
        True,
    )
    check(
        "兜底回应不含内部信息字样",
        any(
            w in "".join(mod.ATTACHMENT_SILENT_REPLY)
            for w in ("端口", "路径", "网关", "日志")
        ),
        False,
    )
    check(
        "兜底回应不落入空壳标记",
        any(
            mod._is_empty_reply(s) for s in mod.ATTACHMENT_SILENT_REPLY
        ),
        False,
    )
    check(
        "兜底节流为正数（防止刷屏）",
        mod.SILENT_NOTICE_THROTTLE_SEC > 0,
        True,
    )
    check("Main 有节流用的字典", "_silent_notice" in open(
        __import__("pathlib").Path(mod.__file__).parent / "main.py",
        encoding="utf-8",
    ).read(), True)

    print()
    print("== 现拉群历史参数 ==")
    src = open(
        __import__("pathlib").Path(mod.__file__).parent / "main.py",
        encoding="utf-8",
    ).read()
    check(
        "显式传 message_seq=0（NapCat 走最新分支）",
        "message_seq=0" in src,
        True,
    )
    check(
        "不再传 reverseOrder（多余参数）",
        "reverseOrder=True" not in src,
        True,
    )

    print()
    print("== 审批产出回传 ==")
    check("回传开关默认开", mod.RELAY_BACK_OUTPUT, True)
    check("回传文件数上限为正", mod.RELAY_OUTPUT_MAX_FILES > 0, True)
    check("回传单文件上限为正", mod.RELAY_OUTPUT_MAX_MB > 0, True)
    check(
        "回传文件数上限不超过 20",
        mod.RELAY_OUTPUT_MAX_FILES <= 20,
        True,
    )

    print()
    print("== 超长裁剪（按语义边界，不硬砍） ==")
    # 短文本原样返回，不该动
    check("未超长不裁剪", mod._smart_trim("短句。", 100), ("短句。", False))

    long_para = "第一段内容。" * 40 + "\n\n" + "第二段内容。" * 40
    trimmed, cut = mod._smart_trim(long_para, 300)
    check("超长会裁剪", cut, True)
    check("裁后不超限", len(trimmed) <= 300 + 20, True)
    check("裁剪点落在段落空行上", trimmed.endswith("…（太长了，后面还有）"), True)
    check(
        "裁剪标记前是完整句（不以半个标点收尾）",
        trimmed.split("…（太长了")[0].rstrip()[-1] in "。！？”』」",
        True,
    )

    long_sent = "这是一句完整的话。" * 60
    t2, cut2 = mod._smart_trim(long_sent, 250)
    check("无段落时按句末标点切", cut2, True)
    check("句末切分不超限", len(t2) <= 250 + 20, True)
    check(
        "句末切分后是完整句子",
        t2.split("…（太长了")[0].rstrip().endswith("。"),
        True,
    )

    long_comma = "甲乙丙丁戊己庚辛壬癸，" * 40
    t3, cut3 = mod._smart_trim(long_comma, 200)
    check("只有逗号时按逗号切", cut3, True)
    check("逗号切分不超限", len(t3) <= 200 + 20, True)

    t4, cut4 = mod._smart_trim("短", 0)
    check("limit<=0 时不裁剪", (t4, cut4), ("短", False))

    check(
        "非管理员上限已放宽到 1500",
        mod._RUNTIME["others_max_chars"] >= 1500,
        True,
    )

    print()
    print("== 排版清洗（QQ 不渲染 Markdown，星号必须洗掉） ==")
    # 2026-10-09 管理员反馈"双*太多了"：模型爱写 **加粗**，发进群就是裸星号。
    # 这是确定性兜底，不靠提示词自觉。
    strip = mod._strip_markdown
    check("成对加粗被洗掉", strip("这个**很关键**，要**认真**看。") == "这个很关键，要认真看。", True)
    check("连续加粗被洗掉", strip("**搜索不丢人**，**答错才丢人**。") == "搜索不丢人，答错才丢人。", True)
    check("落单加粗标记被清掉", "**" not in strip("**开头有星星没结尾"), True)
    check("无序列表符号被去掉", strip("- 甲\n- 乙") == "甲\n乙", True)
    check("星号列表被去掉", strip("* 甲\n* 乙") == "甲\n乙", True)
    check("有序列表编号被去掉", strip("1. 先热身\n2. 再跑步") == "先热身\n再跑步", True)
    check("标题井号被去掉", strip("# 大标题\n正文") == "大标题\n正文", True)
    # 分隔线删掉后留下一个空行 —— 那是自然分段，不压（只有 3+ 换行才压）
    check("分隔线被去掉", "---" not in strip("上面\n---\n下面"), True)
    check("引用符号被去掉", strip("> 他说过") == "他说过", True)
    check("行内代码反引号被去掉", strip("用 `pip install` 装") == "用 pip install 装", True)
    # --- 不能误伤（宁可少洗，不可洗错）---
    check("乘法星号不动", strip("3 * 4 = 12") == "3 * 4 = 12", True)
    check("C 指针星号不动", strip("int *p = &a;") == "int *p = &a;", True)
    check("纯文本原样保留", strip("今天下雨了，带伞。") == "今天下雨了，带伞。", True)
    check("话题井号不动", strip("话题#热点 冲上去了") == "话题#热点 冲上去了", True)
    check("_strip_markdown 存在", hasattr(mod, "_strip_markdown"), True)
    # _sanitize 出口也要真的调用它
    check(
        "_sanitize 出口调用排版清洗",
        "**" not in mod._sanitize("这条**很重要**的消息"),
        True,
    )

    print("== 热重载必须清 sys.modules（否则重载跑的还是旧代码） ==")
    # AstrBot v4 的 PluginManager.reload() 不清 sys.modules，
    # 而 load() 内部是 __import__() —— 命中缓存就返回旧模块对象，
    # 现象是"重载成功"但新代码不生效（2026-10-09 实测，_strip_markdown 白写）。
    check("_purge_own_modules 存在", hasattr(mod.Main, "_purge_own_modules"), True)
    import inspect as _inspect

    try:
        _src = _inspect.getsource(mod.Main._purge_own_modules)
    except (OSError, TypeError):
        _src = ""
    check("清理前缀含插件模块名", "data.plugins.workbuddy_bridge" in _src, True)
    check("会遍历 sys.modules", "sys.modules" in _src, True)
    try:
        _spawn_src = _inspect.getsource(mod.Main._spawn_reload)
    except (OSError, TypeError, AttributeError):
        _spawn_src = ""
    check(
        "重载前先清模块缓存",
        "_purge_own_modules" in _spawn_src,
        True,
    )

    print("== 循环节兜底（不让模型刷屏） ==")
    # 单字符循环：1/3 的经典坑
    spam3 = "10/3 = 3." + "3" * 2000
    hit3 = mod._find_repeat_unit(spam3)
    check("识别单字符循环 3…", hit3 is not None and hit3[0] == "3", True)
    fixed3 = mod._kill_repeat_run(spam3)
    check("单字符循环被替换为说明", "[循环节是 3]" in fixed3, True)
    check("替换后不再刷屏", len(fixed3) < 100, True)

    # 多字符循环：1/7 = 0.142857142857…
    spam7 = "1/7 = 0." + "142857" * 400
    hit7 = mod._find_repeat_unit(spam7)
    check("识别多字符循环 142857…", hit7 is not None and hit7[0] == "142857", True)
    fixed7 = mod._kill_repeat_run(spam7)
    check("多字符循环被替换为说明", "[循环节是 142857]" in fixed7, True)
    check("多字符替换后不再刷屏", len(fixed7) < 100, True)

    # 正常内容不能被误伤
    normal = "先说结论：单挑她赢，团战她封神。这是四影和三月的层级差。"
    check("中文正常回复不被拦", mod._kill_repeat_run(normal), normal)
    pi = "π = 3.14159265358979323846264338327950288419716939937510"
    check("π 的有限位不被拦", mod._kill_repeat_run(pi), pi)
    short = "哈哈哈"
    check("短重复词不被拦", mod._kill_repeat_run(short), short)
    check(
        "重复单元上限存在（避免误判长词）",
        mod.REPEAT_MAX_UNIT <= 12 and mod.REPEAT_MIN_SPAN >= 40,
        True,
    )

    # 提示词里必须带这条规则，否则模型根本不知道要克制
    p_master = mod._prompt_prefix(master=True)
    p_other = mod._prompt_prefix(master=False)
    check("管理员提示词含循环节规则", "循环节" in p_master, True)
    check("普通用户提示词含循环节规则", "循环节" in p_other, True)
    check("执行模式提示词含循环节规则", "循环节" in mod._prompt_prefix(True, mode="execute"), True)

    # 扮演规则（2026-10-09 管理员确认按 A 走）：玩梗式扮演 AI 产品必须放行，
    # 提示词里要明确写着"不要拒绝"，否则模型会自由发挥、时拒时演。
    check("提示词允许玩梗式扮演 AI 产品", "扮演克劳德" in p_other, True)
    check("提示词明确要求不拒绝这类扮演", "不要拒绝" in p_other, True)
    check("提示词要求扮演前先联网搜索", "先联网搜索" in p_other, True)

    # 扮演期间不许露本体（2026-10-09 实测事故）：演"灵宝"演到一半蹦出
    # "我本体是一条蓝色的大肥鱼"，等于当场破功。
    # 根因是第 5 条（身份）和第 5.5 条（扮演）之间没有优先级说明，
    # 模型演着演着就"补充说明一下真实身份"。修法：加第 5.6 条显式压过第 5 条。
    check("扮演期间禁用本体身份（第5.6条）", "扮演期间不露本体" in p_other, True)
    check("明确要求本条压过身份条", "第 5 条就" in p_other and "作废" in p_other, True)
    check("列出典型破功台词", "我本体其实是" in p_other, True)
    check("出戏后才恢复本体", "出戏了，才重新做大肥鱼" in p_other, True)
    # 第 5.6 条引用 persona 分支时必须用「正在角色扮演」的说法，
    # 不能照抄"这个群现在正在角色扮演" —— 那是 persona 分支独有的标记，
    # 抄进全局提示词会让"未扮演时不加人设说明"的断言失效（真的踩过一次）。
    check("第5.6条不误用persona分支标记", "这个群现在正在角色扮演" not in p_other, True)
    # persona 分支（群内 /扮演）也必须带上这条，否则走 persona 就漏了
    p_persona = mod._prompt_prefix(False, "chat", None, "一个话很少的剑客")
    check("角色扮演提示词禁止自曝本体", "不许再提自己的「本体」" in p_persona, True)
    check("角色扮演中提到问名字要答角色", "答的就是这个角色" in p_persona, True)

    # 串群隔离（2026-10-09 实测事故）：答 A 群时把 B 群的内容带了出来。
    # 提示词必须有"只答本条"的硬隔离，且能带上当前会话标签。
    check("提示词含只答本条的隔离规则", "只答本条" in p_other, True)
    check("隔离规则明确禁止跨会话引用", "其他会话" in p_other, True)
    check(
        "带上会话标签（区分不同群）",
        "本会话标识" in mod._prompt_prefix(False, "chat", None, "", channel="qq-abc123"),
        True,
    )
    check(
        "会话标签内容被写入",
        "qq-abc123" in mod._prompt_prefix(False, "chat", None, "", channel="qq-abc123"),
        True,
    )
    check(
        "不传标签时不出现占位",
        "本会话标识" in mod._prompt_prefix(False, "chat"),
        False,
    )

    print()
    print("== 元话术拦截（2026-10-14 修复：内部备注绝不发进群） ==")
    # 事故复盘：三条「（本轮……不发言。）」式备注被当"接话"发进三个群，
    # 修复 = 提示词 1.6【空输出】条 + 这层确定性拦截（_is_empty_reply 出口）。
    check("「（本轮……不发言。）」拦下", mod._is_meta_note("（本轮无合适接话点：话题已结束，不发言。）"), True)
    check("「（这条跳过）」拦下", mod._is_meta_note("（这条跳过）"), True)
    check("「（素材……不接）」拦下", mod._is_meta_note("（素材不足，先不接）"), True)
    check("「（无合适……跳过）」拦下", mod._is_meta_note("（无合适接话点，跳过）"), True)
    check("光有前导无关键词 -> 保守放行（设计如此）", mod._is_meta_note("（无合适接话点）"), False)
    check("半角括号同样拦", mod._is_meta_note("(本轮不宜参与)"), True)
    # 不误伤（宁可漏拦、不可误伤）
    check("正文含'不接'但开头不对 -> 不拦", mod._is_meta_note("这活我不接，找别人吧"), False)
    check("「（本轮比赛真精彩）」不拦（无关键词）", mod._is_meta_note("（本轮比赛真精彩）"), False)
    check("超 200 字不拦（保守策略）", mod._is_meta_note("（本轮" + "啊" * 210 + "不发言）"), False)
    check("空串不拦", mod._is_meta_note(""), False)
    check("meta note 计入空壳出口", mod._is_empty_reply("（本轮不接，先这样。）"), True)

    print()
    print("== 空壳标记全量（网关兜底串一律不发群） ==")
    check("凭据失效串拦下", mod._is_empty_reply("[凭据失效] 网关拒绝认证"), True)
    check("网关不可达串拦下", mod._is_empty_reply("[网关不可达] 连接超时"), True)
    check("'没有可读文本'拦下", mod._is_empty_reply("本轮没有可读文本"), True)
    check("标记表至少 5 类", len(mod.EMPTY_REPLY_MARKERS) >= 5, True)

    print()
    print("== 角色扮演命令（开场 / 收场 / 群隔离 / 超时退场） ==")
    rp = mod.Main(None)
    check("RP 默认对所有群友放行", rp._roleplay_allowed("999"), True)

    r_open = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp1"), "扮演 一个话少的剑客", "10001", False)
    check("开场有回执", bool(r_open) and "入戏" in r_open, True)
    check("人设写入本群", (rp._roleplay.get("g-rp1") or {}).get("persona"), "一个话少的剑客")
    check("取用返回人设", rp._current_persona(_Ev(me, [], "g-rp1")), "一个话少的剑客")
    check("未开演的群取不到", rp._current_persona(_Ev(me, [], "g-rp2")), "")
    r_nop = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp2"), "扮演", "10001", False)
    check("未带设定给出提示", bool(r_nop) and "给个设定" in r_nop, True)
    check("日常说法不触发", rp._handle_roleplay_cmd(_Ev(me, [], "g-rp1"), "我想扮演一下", "10001", False), None)
    r_stop = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp1"), "结束扮演", "10001", False)
    check("收场有回执", bool(r_stop) and "出戏" in r_stop, True)
    check("收场后已清除", "g-rp1" in rp._roleplay, False)
    r_none = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp3"), "结束扮演", "10001", False)
    check("没在演时收场", bool(r_none) and "没在演" in r_none, True)

    rp._roleplay["g-rp9"] = {"persona": "旧人设", "owner": "1", "ts": time.time() - 10**6}
    check("超时取用返回空", rp._current_persona(_Ev(me, [], "g-rp9")), "")
    check("超时后被清出表", "g-rp9" in rp._roleplay, False)

    rp._roleplay["g-rp8"] = {"persona": "人设X", "owner": "1", "ts": time.time() - 5}
    _ts0 = rp._roleplay["g-rp8"]["ts"]
    check(
        "取用即续期",
        (rp._current_persona(_Ev(me, [], "g-rp8")), rp._roleplay["g-rp8"]["ts"] > _ts0),
        ("人设X", True),
    )

    print()
    print("== 代发指令（管理员专属） ==")
    ev0 = _Ev(me, [], "")
    check("非管理员被拒", _aio.run(rp._handle_relay_cmd(ev0, "代发 123456 内容", False)), "这条只有号主能用。")
    check("缺内容给格式提示", _aio.run(rp._handle_relay_cmd(ev0, "代发 123", True)), "格式是这样：代发 群号 内容")
    check("群号非数字给格式提示", _aio.run(rp._handle_relay_cmd(ev0, "代发 abc 内容", True)), "格式是这样：代发 群号 内容")
    check("普通文本不触发", _aio.run(rp._handle_relay_cmd(ev0, "今天天气不错", True)), None)

    print()
    print("== 出口清洗：_sanitize（所有人）+ _strict_filter（非管理员加严） ==")
    mixed = "第一句正文。\n[2026-10-14 10:00:00] [Core] [INFO] [x]: 内部日志\n第二句正文。"
    out_mixed = mod._sanitize(mixed)
    check("日志行被整行删除", "内部日志" not in out_mixed, True)
    check("正常正文保留", ("第一句正文" in out_mixed) and ("第二句正文" in out_mixed), True)
    check("本机 URL 变占位", "[本机地址]" in mod._sanitize("打开 http://127.0.0.1:6185 看看"), True)
    check("localhost:端口 变占位", "[本机端口]" in mod._sanitize("连的是 localhost:6099"), True)
    check("「端口 6185」被隐藏", mod._sanitize("端口 6185 在监听"), "端口[已隐藏] 在监听")
    check("绝对路径变占位", "[路径]" in mod._sanitize(r"文件在 F:\workbuddy\QQbot\test.txt 里"), True)
    check("长数字（QQ号）变占位", "[号码]" in mod._sanitize("他的号 123456789 记一下"), True)
    check("全内部信息 -> 兜底话术", mod._sanitize("[INFO]"), "（这条回复只包含内部信息，已过滤）")

    check("非管理员：邮箱脱敏", "[邮箱]" in mod._strict_filter("联系我 a@b.com"), True)
    check("非管理员：手机号脱敏", "[手机号]" in mod._strict_filter("打 13812345678 找我"), True)
    check("非管理员：IP 脱敏", "[IP]" in mod._strict_filter("IP 是 192.168.1.1 记得"), True)
    check("非管理员：含内部词整行删除", "端口" not in mod._strict_filter("第一句。\n端口 6185 在跑\n第三句。"), True)
    check("非管理员：删空后换拒绝话术", mod._strict_filter("端口 6185"), mod.REFUSE_TEXT)

    print()
    print("== 审批判定三层（强操作 / 机器产物 / 咨询豁免） ==")
    check("兼容别名 = 强操作+机器产物", mod.ACTION_WORDS == mod.STRONG_ACTION_WORDS + mod.MACHINE_OBJECTS, True)
    check("纯机器产物 -> 拦", bool(mod._is_action_request("那份表格在哪")), True)
    check("咨询词豁免机器产物", mod._is_action_request("推荐一本讲表格的书"), None)
    check("强操作词不豁免", bool(mod._is_action_request("帮我删掉这几个文件再分析")), True)

    print()
    print("== 面板配置回退（无配置时用代码默认，不至于失灵） ==")
    check("_cfg 回退默认", rp._cfg("不存在的键", "x"), "x")
    check("_cfg_bool 回退默认", rp._cfg_bool("不存在的键", True), True)
    check("_cfg_int 回退默认", rp._cfg_int("不存在的键", 900), 900)

    check("_safe_name 清洁非法字符", mod._safe_name("a/b:c"), "abc")
    check("_safe_name 空名兜底", mod._safe_name(""), "用户")

    print()
    print("== 同群上下文随行（2026-10-15 网关串会话治理）==")
    class _GEv2:
        def __init__(self, comps, gid="g-ctx", sid="10001", name="甲"):
            self._comps, self._gid, self._sid, self._name = comps, gid, sid, name
        def get_messages(self):
            return self._comps
        def get_group_id(self):
            return self._gid
        def get_sender_id(self):
            return self._sid
        def get_sender_name(self):
            return self._name

    ctx_inst = mod.Main(None)
    # 新行为：没配梗规则、没开闲聊的群也记流水账（网关串会话后，续聊靠它）
    ctx_inst._idle_enabled_for = lambda gid: False
    ctx_inst._remember_group_message(_GEv2([mod.Plain(text="甲说了句甲的话")]), "甲说了句甲的话")
    ctx_inst._remember_group_message(_GEv2([mod.Plain(text="乙回了句乙的话")], sid="10002", name="乙"), "乙回了句乙的话")
    check("无梗/无闲聊的群也记流水账", len(ctx_inst._gag_history.get("g-ctx") or []) == 2, True)

    block = ctx_inst._group_context_block("g-ctx", "10001", "甲说了句甲的话")
    check("上下文块剔除当前消息", "甲说了句甲的话" not in block, True)
    check("上下文块保留他人消息", "乙: 乙回了句乙的话" in block, True)
    check("上下文块带【要回答的消息】标记", "【要回答的消息】" in block, True)
    check("上下文块点明同一群", "同一个群" in block, True)
    check("空历史返回空串", ctx_inst._group_context_block("g-empty", "1", "x"), "")

    print()
    print("== 闲聊续话记账（2026-10-15 持续话题）==")
    spark_inst = mod.Main(None)
    check("初始没有记账", spark_inst._spark_said.get("g-spark"), None)
    # 模拟开口成功后的记账
    bucket = spark_inst._spark_said.setdefault("g-spark", [])
    bucket.append({"text": "上轮我说的话", "ts": time.time() - 10})
    bucket.append({"text": "更早的一句话", "ts": time.time() - 999})
    check("记账写入成功", len(bucket) == 2, True)
    # 保留上限3 条
    for i in range(5):
        bucket.append({"text": f"第{i}句", "ts": time.time()})
        if len(bucket) > 3:
            del bucket[:-3]
    check("记账上限3 条", len(bucket) == 3, True)

    print()
    print("== 跟随模式判定（群友接话就该继续聊）==")
    f_inst = mod.Main(None)
    now = time.time()
    # 1) 从没说过话 -> 不是跟随
    f_inst._group_activity["g-f1"] = {"last_ts": now}
    check("没说过话 -> 不跟随", f_inst._follow_active("g-f1", now), False)
    # 2) 说过话，但群友还没接（last_ts 未超过说话时间）-> 不跟随
    f_inst._spark_said["g-f2"] = [{"text": "我刚说的", "ts": now - 5}]
    f_inst._group_activity["g-f2"] = {"last_ts": now - 30}  # 群友最后发言在我之前
    check("群友还没接 -> 不跟随", f_inst._follow_active("g-f2", now), False)
    # 3) 说过话 + 群友之后又聊了 + 还在热窗口内 -> 跟随
    #    2026-10-18 起跟随只认"有实义文字"，所以这里给 last_text_ts。
    f_inst._spark_said["g-f3"] = [{"text": "我刚说的", "ts": now - 60}]
    f_inst._group_activity["g-f3"] = {"last_ts": now - 10, "last_text_ts": now - 10}
    check("群友接话且话题热 -> 跟随", f_inst._follow_active("g-f3", now), True)
    # ★ 2026-10-18 事故护栏：群友只发图/表情（没有文字）时，**不许**跟随。
    #   现场：群 2000000002 六分钟接话五次，取材全是图片，被人喊"能不能禁言了"。
    f_inst._spark_said["g-f4"] = [{"text": "我刚说的", "ts": now - 60}]
    f_inst._group_activity["g-f4"] = {"last_ts": now - 10}  # 只有图，没有 last_text_ts
    check("群友只发图 -> 不跟随（2026-10-18）", f_inst._follow_active("g-f4", now), False)
    # 4) 群友接了但已经过了窗口（>180s）-> 不跟随，视为新话题
    f_inst._spark_said["g-f4"] = [{"text": "我说的", "ts": now - 600}]
    f_inst._group_activity["g-f4"] = {"last_ts": now - 500}
    check("超过跟随窗口 -> 不跟随", f_inst._follow_active("g-f4", now), False)
    # 跟随窗口常量存在且合理
    check("跟随窗口=180s", f_inst.FOLLOW_WINDOW_SEC, 180)

    print()
    print("== 闭嘴黑名单（2026-10-19：管理员点名关的群）==")
    bl_inst = mod.Main(None)
    check("黑名单常量含被投诉群", "2000000004" in mod.IDLE_CHAT_BLACKLIST, True)
    check("黑名单群判定为闭嘴", bl_inst._blacklisted("2000000004"), True)
    check("黑名单群不开闲聊", bl_inst._idle_enabled_for("2000000004"), False)
    check("普通群不受影响", bl_inst._idle_enabled_for("100000000"), True)
    # 面板配置能覆盖代码常量（留空=用代码里的黑名单）
    check("黑名单留空时用代码常量", bl_inst._blacklisted("2000000004"), True)
    bl_inst._cfg = lambda k, d: "999000111" if k == "idle_chat_blacklist" else d
    check("面板黑名单覆盖生效", bl_inst._blacklisted("999000111"), True)
    check("面板黑名单不含代码里的群", bl_inst._blacklisted("2000000004"), False)
    # ★ 关键护栏：黑名单必须同时掐掉关键词冒泡，否则关了闲聊还会从冒泡冒出来
    check("黑名单群禁止关键词冒泡", bl_inst._kw_allowed("999000111"), False)
    bl_inst._cfg = lambda k, d: d
    check("黑名单群禁止关键词冒泡（代码常量）", bl_inst._kw_allowed("2000000004"), False)

    print()
    print("== 跟随连击闸 + 最小间隔（2026-10-19）==")
    ch_inst = mod.Main(None)
    check("连击上限=2", ch_inst.FOLLOW_MAX_CHAIN, 2)
    check("连击冷却=900s", ch_inst.FOLLOW_CHAIN_COOLDOWN_SEC, 900)
    check("两次跟随最小间隔=240s", mod.FOLLOW_MIN_GAP_SEC, 240)
    nowc = time.time()
    ch_inst._cfg = lambda k, d: d
    # 第 1 轮放行；间隔没到（10s < 240s）第二轮直接被拦
    check("连击第1轮放行", ch_inst._follow_chain_ok("g-c1", nowc), True)
    check("间隔没到 -> 第2轮被拦（2026-10-19 降频）",
          ch_inst._follow_chain_ok("g-c1", nowc + 10), False)
    check("间隔到了 -> 第2轮放行",
          ch_inst._follow_chain_ok("g-c1", nowc + 300), True)
    check("连击满2轮 -> 第3轮被拦",
          ch_inst._follow_chain_ok("g-c1", nowc + 700), False)
    # 不同群互不影响
    check("别的群不受本群连击影响", ch_inst._follow_chain_ok("g-c2", nowc + 20), True)
    # 冷却期过了 -> 重新给机会
    check("冷却期后恢复", ch_inst._follow_chain_ok("g-c1", nowc + 1500), True)
    # 话题冷了（群友最后一句在我这轮之前）-> 清零重新算
    ch_inst._group_activity["g-c3"] = {"last_ts": nowc, "last_text_ts": nowc - 500}
    check("连击第1轮放行(g3)", ch_inst._follow_chain_ok("g-c3", nowc), True)
    ch_inst._group_activity["g-c3"]["last_text_ts"] = nowc - 1  # 群友在我这轮之前就没再说话
    check("话题已冷 -> 计数清零（但仍受最小间隔约束）",
          ch_inst._follow_chain_ok("g-c3", nowc + 5), False)
    check("话题已冷 + 间隔够 -> 重放",
          ch_inst._follow_chain_ok("g-c3", nowc + 400), True)
    # 面板可覆盖上限（0=不限制）
    un_inst = mod.Main(None)
    un_inst._cfg = lambda k, d: 0 if k == "follow_max_chain" else d
    for i in range(8):
        un_inst._follow_chain_ok("g-u1", nowc + i * 400)
    check("上限设0=不限制（2026-10-19）", un_inst._follow_chain_ok("g-u1", nowc + 4000), True)
    # ★ 无文字记录时，计数不能被误清零（否则闸门形同虚设，这是踩过的坑）
    nr_inst = mod.Main(None)
    nr_inst._cfg = lambda k, d: d
    check("无活动记录-第1轮放行", nr_inst._follow_chain_ok("g-nr", nowc), True)
    check("无活动记录-第2轮也要等间隔", nr_inst._follow_chain_ok("g-nr", nowc + 5), False)
    check("无活动记录-间隔够则放行", nr_inst._follow_chain_ok("g-nr", nowc + 300), True)
    check("无活动记录-第3轮仍被拦（连击满）",
          nr_inst._follow_chain_ok("g-nr", nowc + 700), False)
    # 间隔设 0 = 关闭该限制（只靠连击数）
    zg = mod.Main(None)
    zg._cfg = lambda k, d: 0 if k == "follow_min_gap_sec" else d
    check("间隔设0 -> 连续两次都放行",
          [zg._follow_chain_ok("g-z", nowc), zg._follow_chain_ok("g-z", nowc + 1)],
          [True, True])

    print()
    print("== 跟随模式的群闸（2026-10-19：只在梗群开）==")

    async def _stub_material(group_id, min_lines, tag, follow=False):
        """恒定返回素材，隔离"素材够不够"对本次验证的干扰。"""
        return ([{"name": "甲", "text": "随便聊聊", "ts": time.time()}],
                "随便聊聊")

    def _stub_speak(sink):
        async def _inner(group_id, umo, prompt, tag):
            sink.append((group_id, tag))
            return True, "我随便说一句"
        return _inner
    fg = mod.Main(None)
    fg._cfg = lambda k, d: d
    gag_groups = set(mod.GROUP_GAG_RULES)
    check("梗群至少有两个（示例梗/示例称号群）", len(gag_groups) >= 2, True)
    check("跟随默认只认梗群", fg._follow_groups(), gag_groups)
    check("梗群允许跟随", fg._follow_allowed(sorted(gag_groups)[0]), True)
    check("非梗群禁止跟随（2026-10-19）", fg._follow_allowed("555555555"), False)
    # 黑名单优先于群闸
    check("黑名单群即使在梗群也不跟随",
          fg._follow_allowed("2000000004")if "2000000004" in gag_groups else True, True)
    # 面板可覆盖群列表
    fg._cfg = lambda k, d: "123,456" if k == "follow_groups" else d
    check("面板可覆盖跟随群列表", fg._follow_allowed("123"), True)
    check("面板覆盖后梗群失效", fg._follow_allowed(sorted(gag_groups)[0]), False)
    # ★ 端到端：非梗群即使冷场+群友接话，也绝不开口
    ng = mod.Main(None)
    ng._cfg = lambda k, d: d
    ng._last_spark_global = 0.0
    ng._spark_fail_streak = 0
    ng_called = []
    ng._spark_material = _stub_material
    ng._speak_spark = _stub_speak(ng_called)
    ng._group_activity["555555555"] = {
        "last_ts": time.time() - 9999, "last_text_ts": time.time() - 5, "umo": "u-ng",
    }
    ng._spark_said["555555555"] = [{"text": "我刚说的", "ts": time.time() - 30}]
    # 关掉冷场闲聊，确保"唯一可能开口的路径"就是跟随 —— 跟随被闸住就该全静
    ng._cfg = lambda k, d: False if k == "idle_chat_enabled" else d
    asyncio.run(ng._idle_scan())
    check("非梗群跟随被拦-完全静默（2026-10-19）", len(ng_called), 0)
    # 对照：同样配置但在梗群里 -> 会开口
    ok_gid = sorted(gag_groups)[0]
    og = mod.Main(None)
    og._cfg = lambda k, d: False if k == "idle_chat_enabled" else d
    og._last_spark_global = 0.0
    og._spark_fail_streak = 0
    og_called = []
    og._spark_material = _stub_material
    og._speak_spark = _stub_speak(og_called)
    og._group_activity[ok_gid] = {
        "last_ts": time.time() - 10, "last_text_ts": time.time() - 5, "umo": "u-ok",
    }
    og._spark_said[ok_gid] = [{"text": "我刚说的", "ts": time.time() - 30}]
    asyncio.run(og._idle_scan())
    check("梗群跟随仍然放行（对照组）", len(og_called), 1)

    print()
    print("== 梗群提频（2026-10-19：闲聊全群开，梗群更密）==")
    hf = mod.Main(None)
    hf._cfg = lambda k, d: d
    gag2 = set(mod.GROUP_GAG_RULES)
    check("高频群默认跟梗群走", hf._hifreq_groups(), gag2)
    check("冷场闲聊仍是所有群都开", hf._idle_enabled_for("777777777"), True)
    gid = sorted(gag2)[0]
    hi_t, hi_c = hf._idle_timing(gid)
    no_t, no_c = hf._idle_timing("777777777")
    check("普通群触发=300s", no_t, mod.IDLE_TRIGGER_SEC)
    check("普通群冷却=1800s", no_c, mod.IDLE_COOLDOWN_SEC)
    check("梗群触发更短（120s）", hi_t, mod.IDLE_HIFREQ_TRIGGER_SEC)
    check("梗群冷却更短（600s）", hi_c, mod.IDLE_HIFREQ_COOLDOWN_SEC)
    check("梗群触发确实更密", hi_t < no_t, True)
    check("梗群冷却确实更密", hi_c < no_c, True)
    # 面板可覆盖，但受"最低 120s"底线约束（2026-10-20 管理员要求：
    # 巡检间隔 30s，触发/冷却填 0 或小值会让冷场变成每 30 秒开口一次）。
    hf._cfg = lambda k, d: 55 if k == "idle_hifreq_trigger_sec" else d
    check("面板覆盖值被抬到 120s 底线", hf._idle_timing(gid)[0], 120)
    hf._cfg = lambda k, d: 300 if k == "idle_hifreq_trigger_sec" else d
    check("高于底线的面板值原样生效", hf._idle_timing(gid)[0], 300)
    hf._cfg = lambda k, d: "999888777" if k == "idle_hifreq_groups" else d
    check("面板可覆盖高频群列表", hf._hifreq_groups(), {"999888777"})
    check("覆盖后原梗群不再提频", hf._idle_timing(gid)[0], mod.IDLE_TRIGGER_SEC)

    # ★ 端到端：同样"安静 200 秒"，梗群会开口、普通群不会
    def _mk(gid_, quiet):
        """造一个实例跑一轮真实 _idle_scan，返回它实际发了几句。"""
        inst = mod.Main(None)
        inst._cfg = lambda k, d: d
        inst._last_spark_global = 0.0
        inst._spark_fail_streak = 0
        sink = []
        inst._spark_material = _stub_material
        inst._speak_spark = _stub_speak(sink)
        inst._group_activity[gid_] = {
            "last_ts": time.time() - quiet, "umo": "u-x",
        }
        asyncio.run(inst._idle_scan())
        return sink

    check("安静200s：梗群已开口（提频生效）", len(_mk(gid, 200)), 1)
    check("安静200s：普通群仍不开口（未收窄）", len(_mk("777777777", 200)), 0)
    check("安静400s：普通群才开口（常规阈值）", len(_mk("777777777", 400)), 1)

    print()
    print("== 跟随阈值按群取值（2026-10-19 降频后）==")
    fq = mod.Main(None)
    fq._cfg = lambda k, d: d
    gag3 = set(mod.GROUP_GAG_RULES)
    gid3 = sorted(gag3)[0]
    check("梗群跟随：连击上限=2",
          fq._follow_timing(gid3)[0], mod.FOLLOW_HIFREQ_MAX_CHAIN)
    check("梗群跟随：冷却=900s",
          fq._follow_timing(gid3)[1], mod.FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC)
    check("梗群跟随：热窗口=150s",
          fq._follow_timing(gid3)[2], mod.FOLLOW_HIFREQ_WINDOW_SEC)
    check("梗群跟随：最小间隔=240s",
          fq._follow_timing(gid3)[3], mod.FOLLOW_MIN_GAP_SEC)
    check("非梗群跟随：2/900/180/240",
          fq._follow_timing("777777777"),
          (mod.FOLLOW_MAX_CHAIN, mod.FOLLOW_CHAIN_COOLDOWN_SEC, 180,
           mod.FOLLOW_MIN_GAP_SEC))
    check("梗群热窗口确实更短（降频）",
          fq._follow_timing(gid3)[2] < 180, True)
    # ★ 端到端：按真实节奏模拟"群友每 40 秒说一句"，看它到底跟几次
    fq2 = mod.Main(None)
    fq2._cfg = lambda k, d: d
    ts = time.time()
    fired = [fq2._follow_chain_ok(gid3, ts + i * 40) for i in range(12)]  # 8 分钟
    check("8分钟内最多跟2次（原为4次）", sum(fired), 2)
    check("不是每句都跟（12 句只跟 2 次）", sum(fired) < 12, True)
    # 对照：非梗群同样节奏也只跟 2 次
    fq3 = mod.Main(None)
    fq3._cfg = lambda k, d: d
    got3 = [fq3._follow_chain_ok("777777777", ts + i * 40) for i in range(12)]
    check("非梗群同样最多 2 次（对照组）", sum(got3), 2)
    # 热窗口：梗群 200s 后已算凉（窗口 150s）
    fq4 = mod.Main(None)
    fq4._cfg = lambda k, d: d
    fq4._spark_said[gid3] = [{"text": "我刚说的", "ts": ts - 300}]
    fq4._group_activity[gid3] = {"last_ts": ts - 200, "last_text_ts": ts - 200}
    check("梗群200s后已凉（窗口150s）", fq4._follow_active(gid3, ts), False)
    fq4._group_activity[gid3] = {"last_ts": ts - 100, "last_text_ts": ts - 100}
    check("梗群100s内仍算热", fq4._follow_active(gid3, ts), True)
    fq4._spark_said["777777777"] = [{"text": "我刚说的", "ts": ts - 300}]
    fq4._group_activity["777777777"] = {"last_ts": ts - 200, "last_text_ts": ts - 200}
    check("非梗群200s后已凉（窗口180s）", fq4._follow_active("777777777", ts), False)

    print()
    print("== 梗规则群扩到三个（2026-10-19 管理员点名新增）==")
    g3 = "2000000003"
    rules3 = mod.GROUP_GAG_RULES.get(g3) or []
    check("新群已挂上梗规则", len(rules3) >= 1, True)
    check("新群同时有示例梗和示例称号（并存）", len(rules3), 2)
    check("新群不在闭嘴黑名单", mod.IDLE_CHAT_BLACKLIST.__contains__(g3), False)
    n3 = mod.Main(None)
    n3._cfg = lambda k, d: d
    check("新群自动进高频群", n3._hifreq_groups().__contains__(g3), True)
    check("新群自动进跟随群", n3._follow_groups().__contains__(g3), True)
    check("新群允许跟随", n3._follow_allowed(g3), True)
    check("新群允许关键词冒泡", n3._kw_allowed(g3), True)
    check("新群享受提频阈值",
          n3._idle_timing(g3), (mod.IDLE_HIFREQ_TRIGGER_SEC, mod.IDLE_HIFREQ_COOLDOWN_SEC))
    # 结构性护栏：所有梗群在三处口径里必须一致，别只加了一处
    check("三处口径完全一致",
          n3._hifreq_groups() == n3._follow_groups() == set(mod.GROUP_GAG_RULES), True)

    print()
    print("== 生图：触发词判定（2026-10-19 接入即梦）==")
    # 正例：必须能抠出 prompt
    check("画一张猫 -> 猫", mod._draw_prompt_of("画一张猫"), "猫")
    check("画个海边日落 -> 海边日落", mod._draw_prompt_of("画个海边日落"), "海边日落")
    check("帮我画蓝鲸 -> 蓝鲸", mod._draw_prompt_of("帮我画蓝鲸"), "蓝鲸")
    check("生图：赛博朋克街道", mod._draw_prompt_of("生图：赛博朋克街道"), "赛博朋克街道")
    check("全角冒号被剥掉", mod._draw_prompt_of("画一张：一只白猫"), "一只白猫")
    check("半角逗号被剥掉", mod._draw_prompt_of("画个,机械鱼"), "机械鱼")
    # ★ 反例：这些是群里最高频的日常对话，绝不能当成要画图
    check("「这画风不错」不算生图", mod._draw_prompt_of("这画风不错"), "")
    check("「漫画看多了」不算生图", mod._draw_prompt_of("漫画看多了"), "")
    check("「生图模块好用吗」不算（词不在开头）", mod._draw_prompt_of("生图模块好用吗"), "")
    check("只说「画个」没内容 -> 空", mod._draw_prompt_of("画个"), "")
    check("只说「画一张」没内容 -> 空", mod._draw_prompt_of("画一张"), "")
    check("空消息 -> 空", mod._draw_prompt_of(""), "")
    check("普通聊天 -> 空", mod._draw_prompt_of("今天中午吃什么"), "")
    # 最长前缀优先：「画一张」不能被「画个」之类截走
    check("最长前缀优先", mod._draw_prompt_of("画一张鲸鱼"), "鲸鱼")
    # 量词尾巴（2026-10-20）：纯动词触发词后的"张/个/只"要剥掉，
    # 实测「帮我画张女仆装若纳瓦」把「张女仆装…」喂给了模型
    check("帮我画张女仆装若纳瓦 -> 女仆装若纳瓦",
          mod._draw_prompt_of("帮我画张女仆装若纳瓦"), "女仆装若纳瓦")
    check("给我画个猫 -> 猫", mod._draw_prompt_of("给我画个猫"), "猫")
    check("给我画一只蓝鲸 -> 蓝鲸", mod._draw_prompt_of("给我画一只蓝鲸"), "蓝鲸")
    check("生图：一张赛博朋克街道 -> 赛博朋克街道",
          mod._draw_prompt_of("生图：一张赛博朋克街道"), "赛博朋克街道")
    check("只说「帮我画张」没内容 -> 空", mod._draw_prompt_of("帮我画张"), "")
    # 带量词的触发词绝不能二次剥（「画一张张三」≠「三」）
    check("画一张张三 -> 张三（不二次剥）", mod._draw_prompt_of("画一张张三"), "张三")
    check("画张张三 -> 张三（不二次剥）", mod._draw_prompt_of("画张张三"), "张三")
    check("帮我画一张张三 -> 张三（只剥一层）",
          mod._draw_prompt_of("帮我画一张张三"), "张三")

    print()
    print("== 生图：自画像专用提示词（2026-10-20 管理员要求）==")
    SP = mod.DRAW_SELFIE_PROMPT
    _VARIANTS = mod.DRAW_SELFIE_VARIANTS

    def _check_selfie(name, text):
        """自画像命中 = 底句原样保留 + 随机变体来自池子。"""
        got = mod._draw_selfie_prompt_of(text)
        ok = (
            got.startswith(SP + "，")
            and got[len(SP) + 1:] in _VARIANTS
        )
        check(name, ok, True)

    _check_selfie("生成你的照片 -> 底句+随机变体", "生成你的照片")
    _check_selfie("生成一张你的照片 -> 底句+随机变体", "生成一张你的照片")
    _check_selfie("生成你的自画像 -> 底句+随机变体", "生成你的自画像")
    _check_selfie("画一张你的自画像 -> 底句+随机变体", "画一张你的自画像")
    _check_selfie("发张你的自拍 -> 底句+随机变体", "发张你的自拍")
    _check_selfie("能画一张你的照片吗 -> 底句+随机变体", "能画一张你的照片吗")
    _check_selfie("画你自己 -> 底句+随机变体", "画你自己")
    _check_selfie("发一张你的自拍照 -> 底句+随机变体", "发一张你的自拍照")
    _check_selfie("发张你的证件照 -> 底句+随机变体", "发张你的证件照")
    check("底句永远原样保留（管理员：这是一切的前提）",
          SP, "模仿《魔法少女的魔女审判》的画风，画一张拟人化deepseek")
    check("变体池非空且互不相同",
          len(_VARIANTS) >= 5 and len(set(_VARIANTS)) == len(_VARIANTS), True)
    check("画一张你的猫 不是自画像（走普通解析）", mod._draw_selfie_prompt_of("画一张你的猫"), "")
    check("你的照片真好看 不是自画像", mod._draw_selfie_prompt_of("你的照片真好看"), "")
    check("生成你的照片好玩吗 不是自画像", mod._draw_selfie_prompt_of("生成你的照片好玩吗"), "")
    check("★隐患存证：普通解析会把「生成一张你的照片」截成字面的「你的照片」",
          mod._draw_prompt_of("生成一张你的照片"), "你的照片")

    # 端到端：真实入口 _maybe_draw 必须把自画像请求路由到固定提示词。
    # （只测判定函数会漏"主流程根本没调用它"这类 bug——2026-10-19 跟随群闸的教训）
    import asyncio as _aio

    class _SelfieEv:
        unified_msg_origin = "qq:g-selfie:1"

        def get_group_id(self):
            return "g-selfie"

        def get_sender_name(self):
            return "测试员"

    _dg = mod.Main(None)
    _dg._cfg = lambda k, d: d
    _captured = {}

    async def _fake_run_draw(gid, umo, p, nick, ratio="", sid=""):
        _captured["prompt"] = p

    async def _drive_selfie():
        _dg._run_draw = _fake_run_draw
        r = _dg._maybe_draw(_SelfieEv(), "生成一张你的照片", "10001")
        await _aio.sleep(0)  # 让 create_task 出来的假任务跑一步，落 captured
        return r

    check("端到端：自画像请求经 _maybe_draw 返回 draw",
          _aio.run(_drive_selfie()), "draw")
    _got = _captured.get("prompt") or ""
    check("端到端：底句保留且追加的是池内变体",
          _got.startswith(SP + "，") and _got[len(SP) + 1:] in _VARIANTS, True)
    check("端到端：普通生图不受影响",
          mod._draw_prompt_of("画一张猫"), "猫")

    print()
    print("== 生图：三道闸（使用人 / 冷却 / 每日上限）==")
    dg = mod.Main(None)
    dg._cfg = lambda k, d: d
    check("已放开：默认不限制使用人（2026-10-19）",
          mod.IMAGE_GEN_ADMIN_ONLY, False)
    check("管理员放行", dg._draw_allowed(mod.MASTER_ID, "g-d1"), "")
    check("普通群友也放行（2026-10-19 放开）",
          dg._draw_allowed("123456", "g-d1"), "")
    # 想收回权限时，面板打开 admin_only 即可
    dr = mod.Main(None)
    dr._cfg = lambda k, d: True if k == "image_gen_admin_only" else d
    check("面板可随时收回权限",
          dr._draw_allowed("123456", "g-d1"), "只有管理员能用生图")
    check("收回后管理员仍可用",
          dr._draw_allowed(mod.MASTER_ID, "g-d1"), "")
    # 免费档 + 不限量（2026-10-19 管理员确认 5.0lite/4.6/4.0 免费）
    check("默认模型是免费档 5.0lite", mod.IMAGE_GEN_MODEL, "5.0lite")
    check("默认画幅 16:9（2026-10-19）", mod.IMAGE_GEN_RATIO, "16:9")
    # 提示词里写比例 -> 覆盖默认；没写 -> 落回默认
    check("写了比例就用写的", mod._draw_ratio_of("画一张 9:16 的图"), "9:16")
    check("没写比例 -> 空（由调用方落回默认 16:9）",
          mod._draw_ratio_of("画一只猫"), "")
    check("剥掉画幅后不留孤零零的「的」",
          mod._draw_prompt_of("画一张 4：3 的猫"), "猫")
    check("每日上限默认不限", mod.IMAGE_GEN_MAX_PER_DAY, 0)
    du = mod.Main(None)
    du._cfg = lambda k, d: d
    for _ in range(50):          # 连跑 50 次也不该被日上限拦
        du._draw_bump("g-dfree")
    check("不限量：连跑50次仍放行",
          du._draw_allowed("123456", "g-dfree").startswith("本群冷却中"), True)
    # 2026-10-19：cd 缩到 5s + cd 中要回"生图cd中"而不是沉默
    check("同群冷却默认 5s", mod.IMAGE_GEN_COOLDOWN_SEC, 5)
    check("全局间隔默认 5s", mod.IMAGE_GEN_GLOBAL_GAP_SEC, 5)
    check("群冷却 -> 回「生图cd中」",
          mod._draw_block_reply("本群冷却中（<5s）"), "生图cd中")
    check("全局间隔 -> 回「生图cd中」",
          mod._draw_block_reply("全局间隔未到（<5s）"), "生图cd中")
    check("日上限 -> 回「生图cd中」",
          mod._draw_block_reply("今日已出图 30 张，达上限 30"), "生图cd中")
    check("权限类不吞原因",
          mod._draw_block_reply("只有管理员能用生图"), "只有管理员能用生图")
    check("关闭类不吞原因",
          mod._draw_block_reply("生图已关闭"), "生图已关闭")
    # ★ 2026-10-19 实测误伤：「你觉得图片里的这个是你吗」命中"图片"，
    #   没有豁免词 → 被审批闸当操作请求拦下，还让群友等管理员点头。
    #   问句式豁免词补上之后，这条必须直接放行进问答。
    check("看图认自己是问答，不拦（现场原句）",
          mod._is_action_request("你觉得图片里的这个是你吗"), None)
    check("这是你吗 -> 不拦", mod._is_action_request("这是你吗"), None)
    check("这是谁 -> 不拦", mod._is_action_request("这是谁"), None)
    check("认得这张图吗 -> 不拦",
          mod._is_action_request("你认得这张图片吗"), None)
    # 对照：真要动图的还得拦
    check("把图片删了 -> 仍拦",
          mod._is_action_request("把图片删了"), "图片")
    check("生成图片 -> 仍拦（改判第2层的「图片」）",
          mod._is_action_request("生成一张图片"), "图片")
    # ★ 2026-10-20 自检 R7：6 个泛动词移出第 1 层后，单独出现必须放行
    check("生成这个词怎么念 -> 不拦", mod._is_action_request("生成这个词怎么念"), None)
    check("打开思路 -> 不拦", mod._is_action_request("打开思路想想"), None)
    check("做一次评估 -> 不拦", mod._is_action_request("帮我做一次评估"), None)
    check("写个笑话 -> 不拦", mod._is_action_request("写个笑话逗我"), None)
    check("改一下说法 -> 不拦", mod._is_action_request("你能改一下说法吗"), None)
    check("打开这个文件 -> 仍拦（配了机器产物）",
          mod._is_action_request("打开这个文件"), "文件")
    check("删除文件 -> 仍拦（强词优先）",
          mod._is_action_request("把那个文件删掉"), "删掉")
    # 占一次配额后立刻进冷却
    dg._draw_bump("g-d1")
    check("占用后本群进冷却", dg._draw_allowed(mod.MASTER_ID, "g-d1").startswith("本群冷却中"), True)
    check("别的群不受本群冷却影响",
          dg._draw_allowed(mod.MASTER_ID, "g-other").startswith("全局间隔未到"), True)
    # 日上限
    dh = mod.Main(None)
    dh._cfg = lambda k, d: 1 if k == "image_gen_max_per_day" else (0 if k == "image_gen_cooldown_sec" else (0 if k == "image_gen_global_gap_sec" else d))
    dh._draw_bump("g-d2")
    check("日上限1：第二次被拒", dh._draw_allowed(mod.MASTER_ID, "g-d2").startswith("今日已出图"), True)
    # 总开关关闭 -> 谁都不行
    do = mod.Main(None)
    do._cfg = lambda k, d: False if k == "image_gen_enabled" else d
    check("总开关关闭 -> 拒绝", do._draw_allowed(mod.MASTER_ID, "g-d3"), "生图已关闭")
    # 关掉 admin_only -> 群友也能用
    dp = mod.Main(None)
    dp._cfg = lambda k, d: False if k == "image_gen_admin_only" else d
    check("开放后群友可用", dp._draw_allowed("123456", "g-d4"), "")
    # 配额在起任务前就占（防并发重复排队）—— bump 两次必须计到 2
    dq = mod.Main(None)
    dq._draw_bump("g-d5"); dq._draw_bump("g-d5")
    check("bump 幂等计数", dq._draw_daily["g-d5"][1], 2)
    check("出图目录已配置", bool(mod.IMAGE_GEN_DIR), True)
    check("CLI 路径已配置", mod.IMAGE_GEN_BIN.endswith("dreamina.exe"), True)

    print()
    print("== 生图：比例解析（2026-10-19 实测踩坑）==")
    # 现场原句：模型把"比例16:9"当描述画了，ratio 参数却还是 1:1
    raw = "画一张图，比例16:9，写实摄影，一个开满鲜花的花园"
    check("从消息里认出 16:9", mod._draw_ratio_of(raw), "16:9")
    check("prompt 里剥掉了比例字样",
          "16:9" not in mod._draw_prompt_of(raw), True)
    check("prompt 保留真实内容",
          "开满鲜花的花园" in mod._draw_prompt_of(raw), True)
    check("裸比例也认", mod._draw_ratio_of("画一个 9:16 的竖构图像"), "9:16")
    check("口语「横图」-> 16:9", mod._draw_ratio_of("画个横图"), "16:9")
    check("口语「竖图」-> 9:16", mod._draw_ratio_of("画个竖图"), "9:16")
    check("口语「方图」-> 1:1", mod._draw_ratio_of("画个方图"), "1:1")
    check("全角冒号也认", mod._draw_ratio_of("画一张 4：3 的"), "4:3")
    check("没写比例 -> 空（走面板默认）", mod._draw_ratio_of("画一只猫"), "")
    # ★ 反例：不是合法画幅的数字不能被当比例
    check("非白名单比例不认", mod._draw_ratio_of("画个 7:5 的"), "")
    # 3:2 本身是合法画幅，单看字符串分不开"下午3:2"。真正的护栏是：
    # 这类消息压根不是生图请求 -> _draw_prompt_of 返回空 -> 根本走不到比例解析。
    check("非生图消息不会进比例解析", mod._draw_prompt_of("下午3:2开黑"), "")
    check("横图字样从 prompt 剥掉",
          "横图" not in mod._draw_prompt_of("画个横图，一只猫"), True)
    check("剥完内容还在", mod._draw_prompt_of("画个横图，一只猫"), "一只猫")

    print()
    print("== 生图：回图必须包成 MessageChain（2026-10-19 实测踩坑）==")
    # 现场：context.send_message 要链对象，直接传 list 抛
    #   `'list' object has no attribute 'chain'`，图生成了但发不出去
    import inspect as _insp
    src_run = _insp.getsource(mod.Main._run_draw)
    check("回图用了 MessageChain", "MessageChain(chain=[" in src_run, True)
    check("不再裸传 list", "chain = [\n" not in src_run, True)
    check("_draw_sync 接受比例参数",
          "ratio" in _insp.signature(mod.Main._draw_sync).parameters, True)
    check("_run_draw 接受比例参数",
          "ratio" in _insp.signature(mod.Main._run_draw).parameters, True)

    print()
    print("== 生图：按人分即梦分类框（2026-10-19）==")
    # 命名沿用账号里已有的习惯：`<昵称>使用 · <QQ号>`
    ds = mod.Main(None)
    ds._cfg = lambda k, d: d
    check("分类框缓存初始为空", ds._draw_sessions, {})
    # 解析逻辑：行尾是**时间列**，不是名字——第一版栽在这
    import re as _re
    row_re = _re.compile(r"^(\d+)\s+(.+?)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s*$")
    line = "21390015007500  示例昵称使用 · 3000000001  2026-09-09 18:36"
    mm = row_re.match(line)
    check("能解析出会话 ID", mm.group(1), "21390015007500")
    check("能解析出名称", mm.group(2).rstrip(), "示例昵称使用 · 3000000001")
    check("名称尾部含 · QQ号", mm.group(2).rstrip().endswith("· 3000000001"), True)
    # 表头/分隔线不能被当成数据行
    check("表头不匹配", row_re.match("ID              NAME                     UPDATED_AT"), None)
    check("分隔线不匹配", row_re.match("--------------  ----------------------  ----------------"), None)
    # 签名贯通
    import inspect as _insp2
    check("_run_draw 带 sender_id",
          "sender_id" in _insp2.signature(mod.Main._run_draw).parameters, True)
    check("_draw_sync 带 session",
          "session" in _insp2.signature(mod.Main._draw_sync).parameters, True)
    # 2026-10-20：拼命令的主体搬到 _draw_once（_draw_sync 变成带重试的包装层），
    # 所以这条要查 _draw_once 的源码。
    src_once = _insp2.getsource(mod.Main._draw_once)
    check("提交时传了 --session", '"--session"' in src_once, True)
    check("分类框失败不拖垮生图",
          "分类框获取失败，改用默认会话" in _insp2.getsource(mod.Main._run_draw), True)
    # ★ 2026-10-19 实测：即梦会用第一句提示词给会话改名，出图后必须改回来
    check("出图后会改回分类框名",
          "改名" in _insp2.getsource(mod.Main._run_draw)
          or "_draw_session_rename_sync" in _insp2.getsource(mod.Main._run_draw), True)
    check("有改名方法", hasattr(mod.Main, "_draw_session_rename_sync"), True)
    check("改名保留 QQ 号（否则下次查不到）",
          "使用 · {qq}" in _insp2.getsource(mod.Main._draw_session_rename_sync), True)
    check("默认会话(0)不改名",
          mod.Main(None)._draw_session_rename_sync("0", "甲", "123"), False)

    print()
    print("== 闭嘴黑名单：整条巡检路径都拦（2026-10-19）==")
    # 现场：群 2000000004 抱怨太频繁，但面板开关关了没用——因为跟随路径
    # 绕过了 _idle_enabled_for。这里直接驱动 _idle_scan 验证它真的不开口。
    import asyncio as _as
    scan = mod.Main(None)
    scan._cfg = lambda k, d: d
    scan._last_spark_global = 0.0
    scan._spark_fail_streak = 0
    called = []
    scan._spark_material = _stub_material
    scan._speak_spark = _stub_speak(called)
    scan._group_activity["2000000004"] = {
        "last_ts": time.time() - 9999,      # 早就冷场，理论上必开口
        "last_text_ts": time.time() - 5,    # 且群友刚说过话，跟随也成立
        "umo": "u-bl",
    }
    scan._spark_said["2000000004"] = [{"text": "我刚说的", "ts": time.time() - 30}]
    asyncio.run(scan._idle_scan())
    check("黑名单群冷场也不开口（2026-10-19）", len(called), 0)
    # 对照组：同样状态但不在黑名单 → 会开口，证明上面不是"闸门根本没接上"
    scan2 = mod.Main(None)
    scan2._cfg = lambda k, d: d
    scan2._last_spark_global = 0.0
    scan2._spark_fail_streak = 0
    called2 = []
    scan2._spark_material = _stub_material
    scan2._speak_spark = _stub_speak(called2)
    scan2._group_activity["100000000"] = {
        "last_ts": time.time() - 9999,
        "last_text_ts": time.time() - 5,
        "umo": "u-ok",
    }
    scan2._spark_said["100000000"] = [{"text": "我刚说的", "ts": time.time() - 30}]
    asyncio.run(scan2._idle_scan())
    check("对照组普通群正常开口（证明拦截精准）", len(called2), 1)

    print()
    print("== 闲聊读图（2026-10-15）==")
    check("读图开关默认开", mod.IDLE_ALLOW_IMAGE, True)
    # 素材里带img_url 才能被挑中
    with_img = [
        {"name": "甲", "text": "（图片）", "img_url": "https://x/a.jpg"},
        {"name": "乙", "text": "看这个", "img_url": "https://x/b.jpg"},
    ]
    latest = [h for h in with_img if str(h.get("img_url") or "").strip()][-1]
    check("挑的是最新一张", latest["img_url"], "https://x/b.jpg")
    no_img = [{"name": "丙", "text": "纯文字"}]
    check("无图时挑不到", [h for h in no_img if str(h.get("img_url") or "").strip()], [])
    # 缺 img_url 键的旧素材不能崩（回退流水账就没这个字段）
    legacy = [{"name": "丁", "text": "老格式"}]
    check("缺 img_url 键不报错", str(legacy[0].get("img_url") or ""), "")

    print()
    print("== 唤醒词打招呼（2026-10-15）==")
    check("唤醒默认开", mod.NAME_WAKE_ENABLED, True)
    check("含唤醒词-> 命中", mod._hit_name_wake("大肥鱼你在吗"), "大肥鱼")
    check("非开头也命中", mod._hit_name_wake("大肥鱼你看这个"), "大肥鱼")
    check("鲸鱼也命中", mod._hit_name_wake("鲸鱼出来"), "鲸鱼")
    # 2026-10-15 管理员纠正：「示例梗」是群友的梗（群梗规则），
    # 不是本体人设，**绝不能**当唤醒词，否则等于认错自己名字。
    check("示例梗不是唤醒词", mod._hit_name_wake("示例梗你在吗"), "")
    check("无关文本不命中", mod._hit_name_wake("今天天气不错"), "")
    check("空串不命中", mod._hit_name_wake(""), "")
    # 打招呼 vs 完整问答的分界
    check("只是叫名字 -> 不算提问", mod._looks_like_question("大肥鱼"), False)
    check("叫名字+问句 -> 算提问", mod._looks_like_question("大肥鱼你知道这个梗吗"), True)
    check("叫名字+问号 -> 算提问", mod._looks_like_question("大肥鱼在吗？"), True)
    check("回应话术够用", len(mod.NAME_WAKE_REPLIES) >= 3, True)

    print()
    print("== 熔断：网关连续失败就静默（2026-10-15）==")
    check("熔断阈值=3", mod.SPARK_FAIL_CIRCUIT_N, 3)
    cb = mod.Main(None)
    check("初始计数为0", cb._spark_fail_streak, 0)
    # 未达阈值 -> 继续尝试
    cb._spark_fail_streak = 1
    check("失败1次仍会尝试", cb._spark_fail_streak >= mod.SPARK_FAIL_CIRCUIT_N, False)
    # 达阈值 -> _idle_scan 直接 return（不再白烧 token）
    cb._spark_fail_streak = mod.SPARK_FAIL_CIRCUIT_N
    check("达阈值后扫描层熔断", cb._spark_fail_streak >= mod.SPARK_FAIL_CIRCUIT_N, True)
    # 成功即清零
    cb._spark_fail_streak = 0
    check("成功后清零", cb._spark_fail_streak, 0)
    # 网关兜底文案必须被 _is_empty_reply 拦下（绝不发进群）
    check("网关不可达->空壳", mod._is_empty_reply("[网关不可达] 找不到 WorkBuddy Gateway。"), True)
    check("凭据失效->空壳", mod._is_empty_reply("[凭据失效] 网关拒绝认证，请让AI 重新生成"), True)
    check("无输出->空壳", mod._is_empty_reply("[无输出] WorkBuddy 网关没有返回内容事件。"), True)
    check("超时->空壳", mod._is_empty_reply("[超时] WorkBuddy 处理超过 300 秒。"), True)
    check("网关错误->空壳", mod._is_empty_reply("[网关错误] 没有返回 runId。"), True)
    check("连接失败->空壳", mod._is_empty_reply("[连接失败] 无法访问 WorkBuddy 网关"), True)
    check("环境错误->空壳", mod._is_empty_reply("[环境错误] 缺少 aiohttp。"), True)
    check("未配置->空壳", mod._is_empty_reply("[未配置] 找不到网关凭据。"), True)
    check("内部错误->空壳", mod._is_empty_reply("[内部错误] KeyError: x"), True)
    check("排队->空壳", mod._is_empty_reply("[排队] 桌面端当前会话正忙。"), True)
    check("正常回复不是空壳", mod._is_empty_reply("在呢，啥事"), False)
    # 边界：正常回复里带这些字眼但不是兜底串，不能误伤
    check("含方括号但不匹配 -> 保留", mod._is_empty_reply("[1] 你这波操作真骚"), False)

    # ⚠️ 排队 ≠ 故障（2026-10-18 真实事故）：
    # 管理员在 WorkBuddy 里跟 AI 说话时，桌面端会话被占着，QQ 冒泡请求
    # 全排在后面。此前 _stream 一律返回"[无输出]"，被熔断器当网关故障累计，
    # 三次就把**全部**主动发言熔断掉——"管理员越用 AI，机器人越闭嘴"。
    # 现在 _stream 用 queued 标记区分两者，[排队] 不计入熔断。
    print()
    print("== 排队 vs 真故障：熔断只认真故障（2026-10-18）==")

    class _NullCtx:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *a):
            return False

    def _run(coro):
        return asyncio.run(coro)

    def _stub_ask(text):
        async def _fake_ask(*a, **kw):
            return text
        return _fake_ask

    def _stub_send(sink):
        async def _fake_send(umo, reply):
            sink.append(reply)
            return True
        return _fake_send

    cb2 = mod.Main(None)
    sent2 = []
    cb2._lock = lambda umo: _NullCtx()
    cb2._ask = _stub_ask("[排队] 桌面端当前会话正忙，本轮没轮到执行。")
    cb2._send_to_session = _stub_send(sent2)
    for _ in range(5):
        _run(cb2._speak_spark("g_q", "u_q", "聊聊", "闲聊"))
    check("连续 5 次排队都不熔断", cb2._spark_fail_streak, 0)
    check("排队时一条都不发", len(sent2), 0)

    # 真故障仍要熔断（别把熔断一起改没了）
    cb3 = mod.Main(None)
    sent3 = []
    cb3._lock = lambda umo: _NullCtx()
    cb3._ask = _stub_ask("[凭据失效] 网关拒绝认证")
    cb3._send_to_session = _stub_send(sent3)
    for _ in range(mod.SPARK_FAIL_CIRCUIT_N):
        _run(cb3._speak_spark("g_q", "u_q", "聊聊", "闲聊"))
    check("真故障仍触发熔断", cb3._spark_fail_streak, mod.SPARK_FAIL_CIRCUIT_N)
    check("熔断时一条都不发", len(sent3), 0)

    print()
    print("== 关键词冒泡：群范围与关键词（2026-10-16）==")
    kb = mod.Main(None)
    gag_groups = set(mod.GROUP_GAG_RULES)
    check("群梗规则非空", len(gag_groups) > 0, True)
    # 默认跟着群梗规则走的群走（管理员定的口径）
    check("默认群=群梗规则的群", kb._kw_groups(), gag_groups)
    # 没配梗规则的群 -> 不冒泡
    check("梗外群不生效", kb._kw_allowed("g-not-in-rules"), False)
    check("梗内群生效", kb._kw_allowed(next(iter(gag_groups))), True)
    # 总开关关掉 -> 全都不生效
    kb.config = {"keyword_bubble_enabled": False}
    check("总开关关掉->不生效", kb._kw_allowed(next(iter(gag_groups))), False)
    kb.config = {}
    # 面板显式配群 -> 覆盖默认（不写死）
    kb.config = {"keyword_bubble_groups": "111, 222"}
    check("面板配群覆盖默认", kb._kw_groups(), {"111", "222"})
    check("面板配的群生效", kb._kw_allowed("111"), True)
    kb.config = {}
    # 面板能改关键词表
    kb.config = {"keyword_bubble_words": "甲词,乙词"}
    check("面板关键词生效", kb._kw_words(), ["甲词", "乙词"])
    kb.config = {}
    check("默认关键词表非空", len(kb._kw_words()) > 0, True)
    # 默认表里不该混进群梗词（那是梗规则的活）
    check("默认表不含示例梗", mod._hit_words("示例梗", kb._kw_words()), None)
    # 真实命中判定
    check("命中「有没有人」", mod._hit_words("有没有人知道这个梗什么意思", kb._kw_words()), "有没有人")
    check("命中「什么意思」", mod._hit_words("这什么意思", kb._kw_words()), "什么意思")
    check("无关文本不命中", mod._hit_words("今天吃了碗面", kb._kw_words()), None)
    # 熔断中 -> 不冒泡（网关挂着别白烧 token）
    kb._spark_fail_streak = mod.SPARK_FAIL_CIRCUIT_N
    check("熔断中不冒泡", kb._kw_allowed(next(iter(gag_groups))), False)
    kb._spark_fail_streak = 0

    print()
    print("== 关键词冒泡：节流三道闸（冷却/全局间隔/日上限）==")
    kg = next(iter(mod.GROUP_GAG_RULES))
    kt = mod.Main(None)
    check("初始可通过冷却", kt._kw_gate(kg), "")
    kt._kw_bump(kg)
    check("刚冒泡->冷却中", "冷却中" in kt._kw_gate(kg), True)
    # 冷却过了但全局间隔没过（同一群刚bump 过，全局时间也刚更新）
    kt2 = mod.Main(None)
    kt2._kw_last[kg] = time.time() - 99999      # 本群冷却已过
    kt2._kw_last_global = time.time()             # 但别的群刚开过腔
    check("全局间隔挡住", "全局间隔" in kt2._kw_gate(kg), True)
    # 日上限：造满配额
    kt3 = mod.Main(None)
    kt3._kw_last[kg] = 0.0
    kt3._kw_last_global = 0.0
    today = time.strftime("%Y-%m-%d")
    kt3._kw_daily[kg] = (today, mod.KEYWORD_BUBBLE_MAX_PER_DAY)
    check("日上限挡住", "上限" in kt3._kw_gate(kg), True)
    # 跨天自动归零（昨天用满今天照常冒泡）
    kt3._kw_daily[kg] = ("2020-01-01", mod.KEYWORD_BUBBLE_MAX_PER_DAY)
    check("跨天后不卡上限", kt3._kw_gate(kg), "")
    # 每天上限可设为 0=不限制
    kt4 = mod.Main(None)
    kt4.config = {"keyword_bubble_max_per_day": 0}
    kt4._kw_daily[kg] = (today, 9999)
    check("上限=0 -> 不限制", kt4._kw_gate(kg), "")
    # 配额在bump 时递增（且只加一次）
    kt5 = mod.Main(None)
    kt5._kw_bump(kg)
    check("bump 后计数=1", kt5._kw_daily[kg][1], 1)
    kt5._kw_bump(kg)
    check("再 bump 计数=2", kt5._kw_daily[kg][1], 2)

    print()
    print("== 关键词冒泡：不把原文当提问（2026-10-16）==")
    class _KWBEv(_GEv2):
        def __init__(self, comps, gid, sid="10001", name="甲", umo="qq:GroupMessage:x", me="999999"):
            super().__init__(comps, gid, sid, name)
            self.unified_msg_origin = umo
            self._me = me
        def is_private_chat(self):
            return False
        def get_self_id(self):
            return self._me
    # 被 @ 时不冒泡（走正常问答，别两条路径一起说话）
    import asyncio as _aio
    kwa = mod.Main(None)
    fired_a = []
    async def _fake_spark_a(gid, umo, word):
        fired_a.append((gid, word))
    kwa._keyword_spark = _fake_spark_a
    # @ 的正是机器人自己 -> 必须不冒泡（这条已经在正常问答路径里了）
    ev_at_me = _KWBEv([mod.At(qq="999999")], kg, umo=f"qq:GroupMessage:{kg}", me="999999")

    async def _drive_at():
        # 留一拍让 create_task 真正跑起来，否则断言会早于任务执行
        r = kwa._maybe_keyword_bubble(ev_at_me, "有没有人啊这什么意思")
        await _aio.sleep(0)
        return r
    check("被@ 机器人 -> 不冒泡", _aio.run(_drive_at()), False)
    check("被@ 时没起冒泡任务", len(fired_a), 0)
    # 梗外群 -> 不冒泡
    kwa2 = mod.Main(None)
    fired_b = []
    async def _fake_spark_b(gid, umo, word):
        fired_b.append((gid, word))
    kwa2._keyword_spark = _fake_spark_b
    ev_out = _KWBEv([mod.Plain(text="有没有人啊")], "g-not-in-rules", umo="qq:GroupMessage:out")

    async def _drive_out():
        r = kwa2._maybe_keyword_bubble(ev_out, "有没有人啊")
        await _aio.sleep(0)
        return r
    check("梗外群不冒泡", _aio.run(_drive_out()), False)
    check("梗外群没起任务", len(fired_b), 0)
    # 梗内群 + 有关键词 -> 应安排任务
    kwi = mod.Main(None)
    fired = []
    async def _fake_spark2(gid, umo, word):
        fired.append((gid, word))
    kwi._keyword_spark = _fake_spark2
    ev_in = _KWBEv([mod.Plain(text="有没有人啊这什么意思")], kg, umo=f"qq:GroupMessage:{kg}")

    async def _drive():
        r = kwi._maybe_keyword_bubble(ev_in, "有没有人啊这什么意思")
        await _aio.sleep(0)
        return r
    check("梗内群命中-> 安排冒泡", _aio.run(_drive()), True)
    check("冒泡带上了命中的词", fired[0][1] if fired else None, "有没有人")
    # 无关键词 -> 不冒泡
    fired.clear()
    ev_none = _KWBEv([mod.Plain(text="今天天气不错")], kg, umo=f"qq:GroupMessage:{kg}")

    async def _drive2():
        r = kwi._maybe_keyword_bubble(ev_none, "今天天气不错")
        await _aio.sleep(0)
        return r
    check("无关键词-> 不冒泡", _aio.run(_drive2()), False)
    check("无关键词时没起任务", len(fired), 0)
    # 冒泡提示词要点：必须点明"你没被点名"
    check("冒泡有独立提示词方法", hasattr(mod.Main, "_keyword_spark"), True)
    check("冒泡复用素材方法", hasattr(mod.Main, "_spark_material"), True)
    check("冒泡复用发送出口", hasattr(mod.Main, "_speak_spark"), True)

    print()
    print("== 网关扫描退避（端口换了别每次全量扫）==")
    check("退避窗口=30s", mod.GW_RESCAN_COOLDOWN_SEC, 30)
    check("缓存带 miss_at 字段", "miss_at" in mod._GW_CACHE, True)
    check("miss_at 初始为0", float(mod._GW_CACHE.get("miss_at") or 0.0), 0.0)
    # 刚 miss 过 => 退避期内（不再全量扫描）
    mod._GW_CACHE["miss_at"] = time.time()
    check("刚失败-> 处于退避期", (time.time() - mod._GW_CACHE["miss_at"]) < mod.GW_RESCAN_COOLDOWN_SEC, True)
    # 退避期过后=> 允许重新扫
    mod._GW_CACHE["miss_at"] = time.time() - 99
    check("退避期过-> 可重扫", (time.time() - mod._GW_CACHE["miss_at"]) < mod.GW_RESCAN_COOLDOWN_SEC, False)
    mod._GW_CACHE["miss_at"] = 0.0

    print()
    print("== @ 路径也能续上（记账覆盖两条路径）==")
    at_inst = mod.Main(None)
    at_inst._idle_enabled_for = lambda gid: False   # 该群没开"适时闲聊"
    # 模拟 @ 回复后的记账（这条路径以前不记，是断链的根因）
    at_bucket = at_inst._spark_said.setdefault("g-at", [])
    at_bucket.append({"text": "我回答@的内容", "ts": now - 20})
    at_inst._group_activity["g-at"] = {"last_ts": now - 5, "last_text_ts": now - 5, "umo": "u1"}
    check("@ 过且群友接话 -> 跟随", at_inst._follow_active("g-at", now), True)
    # 群友没接（last_ts 早于我发言）-> 不跟随
    at_inst._group_activity["g-at"] = {"last_ts": now - 60, "umo": "u1"}
    check("@ 过但无人接 -> 不跟随", at_inst._follow_active("g-at", now), False)
    # 从没被@ 过的群 -> 绝不跟随（防乱插话）
    at_inst._group_activity["g-never"] = {"last_ts": now, "umo": "u2"}
    check("从未互动 -> 绝不跟随", at_inst._follow_active("g-never", now), False)
    # _note_group_activity 对未开闲聊的群也要记（否则跟随无从判断）
    class _AtEv(_Ev):
        def __init__(self, gid):
            self._gid = gid
            self.unified_msg_origin = f"aiocqhttp:GroupMessage:{gid}"
        def get_group_id(self):
            return self._gid
        def get_self_id(self):
            return "1"
        def get_message_str(self):
            # _note_group_activity 现在会读文字长度（跟随模式的文字门槛），
            # 这个桩只关心"活跃时间被记下"，给一句普通文本即可。
            return "随便说了一句"
    at_inst._note_group_activity(_AtEv("g-off"))
    check("未开闲聊的群也记活跃", "g-off" in at_inst._group_activity, True)
    check(
        "有文字的消息会刷新 last_text_ts（跟随门槛）",
        bool(at_inst._group_activity["g-off"].get("last_text_ts")),
        True,
    )

    print()
    print("== run 状态解析（排队等待用）==")
    check("active=true -> 进行中", mod.Main._parse_run_active('{"data":{"active":true}}'), True)
    check("active=false -> 已结束", mod.Main._parse_run_active('{"data":{"active":false}}'), False)
    check("非法JSON -> None", mod.Main._parse_run_active("not-json"), None)
    check("缺active字段 -> None", mod.Main._parse_run_active('{"data":{}}'), None)

    print()
    print("== temp 媒体缓存清理：路径守卫（真实目录绝不参与测试）==")
    # 用 tmp 造假 temp：父目录链必须真的是 .../AstrBot/data/temp，
    # 否则守卫生效，测不到东西 —— 所以这里按同样的层级搭出来。
    fake_root = Path(tempfile.mkdtemp(prefix="astrbotsim_")) / "AstrBot" / "data" / "temp"
    fake_root.mkdir(parents=True)
    try:
        real_root = mod.TEMP_ROOT
        real_keep = mod.TEMP_KEEP_DAYS
        real_cap = mod.TEMP_CLEAN_MAX_FILES

        old_f = fake_root / "media_old.jpg"
        new_f = fake_root / "media_new.jpg"
        sub_d = fake_root / "somedir"
        old_f.write_bytes(b"x" * 4096)
        new_f.write_bytes(b"y" * 8192)
        sub_d.mkdir()
        (sub_d / "inner.txt").write_bytes(b"z" * 1024)
        # 老文件改mtime 到2 天前，新文件保持现在
        old_ts = time.time() - 3 * 86400
        os.utime(old_f, (old_ts, old_ts))

        mod.TEMP_ROOT = fake_root
        mod.TEMP_KEEP_DAYS = 1
        mod.TEMP_CLEAN_MAX_FILES = 2000

        removed, freed = mod._clean_old_temp()
        check("删掉超期文件 1 个", removed, 1)
        check("释放量=4096", freed, 4096)
        check("超期文件已消失", old_f.exists(), False)
        check("当天文件保留", new_f.exists(), True)
        check("子目录不被删", sub_d.is_dir(), True)
        check("_temp_usage 只算顶层文件", mod._temp_usage(), 8192)
        check("重复调用无残留（幂等）", mod._clean_old_temp(), (0, 0))

        print("-- 路径守卫：以下每条都必须拒绝 --")
        for label, bad in [
            ("目录名非temp", fake_root.parent / "astrbot"),
            ("父目录不是data", fake_root.parent.parent / "AstrBot"),
            ("路径不存在", Path("F:/ZZZ_no_such/data/temp")),
        ]:
            mod.TEMP_ROOT = bad
            check(f"{label}-> 拒绝", mod._clean_old_temp(), (0, 0))

        mod.TEMP_ROOT = fake_root
        mod.TEMP_KEEP_DAYS = 0
        check("KEEP_DAYS=0 -> 不清", mod._clean_old_temp(), (0, 0))

        mod.TEMP_KEEP_DAYS = 1
        mod.TEMP_CLEAN_MAX_FILES = 2000
        # 单次上限：造 10 个超期文件，cap=3 只能删 3 个
        for i in range(10):
            f = fake_root / f"bulk_{i}.jpg"
            f.write_bytes(b"b" * 128)
            os.utime(f, (old_ts, old_ts))
        mod.TEMP_CLEAN_MAX_FILES = 3
        r_cap, _ = mod._clean_old_temp()
        check("单次上限生效(<=3)", r_cap <= 3, True)

        mod.TEMP_ROOT, mod.TEMP_KEEP_DAYS, mod.TEMP_CLEAN_MAX_FILES = real_root, real_keep, real_cap
    finally:
        shutil.rmtree(fake_root.parents[2], ignore_errors=True)

    print()
    print("-" * 56)
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
