"""Offline messages regression scenarios for the QQ bridge."""

import hashlib
import tempfile
import time
from pathlib import Path

from .support import TEST_BOT_ID, make_event_type


def check_messages(mod, check):
    """Run the messages regression scenarios."""
    p_chat = mod._prompt_prefix(False)
    _Ev = make_event_type(mod)
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
    inst2._remember_group_message(
        _GEv([mod.Image(file="a.jpg"), mod.Record(file="v.mp3")]), ""
    )
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
                {
                    "user_id": 10001,
                    "time": 1,
                    "sender": {"card": "甲", "nickname": "甲"},
                    "message": [{"type": "text", "data": {"text": "今晚开黑"}}],
                },
                {
                    "user_id": 9999,
                    "time": 2,
                    "sender": {"nickname": "机器人"},
                    "message": [{"type": "text", "data": {"text": "机器人的话"}}],
                },
                {
                    "user_id": 10002,
                    "time": 3,
                    "sender": {"nickname": "乙"},
                    "message": [{"type": "image", "data": {"file": "a.jpg"}}],
                },
                {
                    "user_id": 10003,
                    "time": 4,
                    "sender": {"nickname": "丙"},
                    "message": [{"type": "image"}, {"type": "image"}],
                },
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
    check(
        "调用的是历史接口",
        bot.calls and bot.calls[0][0] == "get_group_msg_history",
        True,
    )

    inst3._group_activity["654321"] = {
        "umo": "x",
        "bot": _FakeBot(boom=True),
        "self_id": "9999",
    }
    boom_hist, boom_why = _aio.run(inst3._fetch_recent_messages("654321", 15))
    check("接口异常 -> 空表（回退缓存）", boom_hist == [], True)
    check("接口异常 -> 原因是『接口异常』", "接口异常" in boom_why, True)

    # 接口回空 messages 是最隐蔽的一种失败（不抛异常、状态还 ok）。
    # 2026-10-18 实测线上 434 次全走这条，日志里却看不出来。
    inst3._group_activity["777"] = {
        "umo": "x",
        "bot": _FakeBot({"status": "ok", "retcode": 0, "data": {"messages": []}}),
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
            {
                "user_id": 20001,
                "time": 5,
                "sender": {"nickname": "丁"},
                "message": [{"type": "text", "data": {"text": "拆包后的素材"}}],
            },
        ]
    }
    inst3._group_activity["888"] = {
        "umo": "x",
        "bot": _FakeBot(unpacked),
        "self_id": "9999",
    }
    got_u, _why_u = _aio.run(inst3._fetch_recent_messages("888", 15))
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
    check(
        "'今天天气真好' 不误伤",
        mod._hit_words("今天天气真好", mod.CONFIG_TOPIC_WORDS),
        None,
    )
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

    me = TEST_BOT_ID
    check("@ 机器人 -> 读", mod._is_at_me(_Ev(me, [mod.At(qq=me)])), True)
    check("@ 别人 -> 不读", mod._is_at_me(_Ev(me, [mod.At(qq="3000000004")])), False)
    check("@全体 -> 读", mod._is_at_me(_Ev(me, [mod.At(qq="all")])), True)
    check(
        "只发图没 @ -> 不读", mod._is_at_me(_Ev(me, [mod.Image(file="a.jpg")])), False
    )
    check(
        "引用机器人 -> 算叫到",
        mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id=me)])),
        True,
    )
    check(
        "引用别人 -> 不算叫到",
        mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id="3000000006")])),
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
            mod._is_at_me(_Ev(me, [mod.At(qq="3000000006")]))
            or mod._is_reply_to_me(_Ev(me, [mod.Reply(sender_id="3000000006")]))
            or any("在调".startswith(p) for p in mod.EXTRA_WAKE_PREFIXES)
        ),
        False,
    )
    check(
        "附件数量统计",
        len(
            mod._attachment_components(
                _Ev(
                    me,
                    [mod.Plain(text="看这个"), mod.Image(file="a.jpg"), mod.At(qq=me)],
                )
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
    print("== 原消息引用与本群表情候选 ==")
    quoted = mod.Main(None)
    original = _Ev(
        TEST_BOT_ID,
        [mod.Reply(id="101"), mod.At(qq=TEST_BOT_ID), mod.Plain("问题")],
        "selfcheck-group-a",
        message_id="202",
    )
    result = quoted._reply_result(original, "答复")
    check("群内明确@使用原生回复", result[0], "chain")
    parts = result[1] if result[0] == "chain" else []
    check(
        "引用当前问题而非被引用的旧消息",
        getattr(parts[0], "id", None) if parts else None,
        "202",
    )
    check(
        "引用后保持答复文字",
        getattr(parts[1], "text", None) if len(parts) > 1 else None,
        "答复",
    )
    image_result = quoted._reply_result(original, "", image_path="fixture.png")
    image_parts = image_result[1]
    check("纯表情保持原消息引用", getattr(image_parts[0], "id", None), "202")
    check(
        "纯表情不附带空文字",
        [type(part) for part in image_parts],
        [mod.Reply, mod.Image],
    )
    original.message_id = ""
    check(
        "缺原消息ID不借用旧引用",
        quoted._reply_result(original, "答复"),
        ("plain", "答复"),
    )
    original.message_id = "202"
    quoted.config = {"quote_reply_enabled": False}
    check(
        "关闭引用功能仍发普通答复",
        quoted._reply_result(original, "答复"),
        ("plain", "答复"),
    )
    quoted.config = {}
    for label, event in (
        (
            "私聊不附加引用",
            _Ev(TEST_BOT_ID, [mod.At(qq=TEST_BOT_ID)], message_id="303"),
        ),
        (
            "全体@不等同明确@机器人",
            _Ev(TEST_BOT_ID, [mod.At(qq="all")], "selfcheck-group-a", "303"),
        ),
    ):
        check(label, quoted._reply_result(event, "答复"), ("plain", "答复"))

    with tempfile.TemporaryDirectory(prefix="qqbot-meme-selfcheck-") as temporary:
        root = Path(temporary)
        scope_a = "selfcheck:GroupMessage:group-a"
        scope_b = "selfcheck:GroupMessage:group-b"
        png = b"\x89PNG\r\n\x1a\nselfcheck-image"
        fingerprint = hashlib.sha256(png).hexdigest()
        group_dir = (
            root / "自动收集" / hashlib.sha256(scope_a.encode()).hexdigest()[:16]
        )
        group_dir.mkdir(parents=True)
        asset = group_dir / f"{fingerprint}.png"
        asset.write_bytes(png)
        library = mod.meme_library.MemeLibrary(root)
        offer = library.offer(scope_a, chance_percent=100, now=1000)
        check("本群自收表情成为候选", bool(offer and len(offer.candidates) == 1), True)
        check(
            "其他群看不到本群自收表情",
            library.offer(scope_b, chance_percent=100, now=1000),
            None,
        )
        if offer:
            token = offer.candidates[0].token
            marker = f"[MEME:{token}]"
            clean, selected = mod.meme_library.strip_selection(f"接住了{marker}", offer)
            check("表情选择标记从聊天文字移除", clean, "接住了")
            check("允许当前候选的唯一选择", selected, token)
            check(
                "他群不能消费本群候选",
                library.take(scope_b, offer, selected, now=1001),
                None,
            )
            check(
                "本群可以消费原候选",
                library.take(scope_a, offer, selected, now=1001),
                str(asset),
            )
            check(
                "重复发送受冷却限制",
                library.take(scope_a, offer, selected, now=1002),
                None,
            )
            check(
                "重复控制标记清理且不选图",
                mod.meme_library.strip_selection(f"答复{marker}{marker}", offer),
                ("答复", None),
            )
            check(
                "无本轮候选时标记仍清除",
                mod.meme_library.strip_selection(f"答复{marker}"),
                ("答复", None),
            )
        check(
            "伪造素材路径不能选择表情",
            mod.meme_library.strip_selection("答复[MEME:../other.png]", offer),
            ("答复", None),
        )
        check(
            "不完整表情标记不会泄露",
            mod.meme_library.strip_selection("答复[MEME:broken", offer),
            ("答复", None),
        )
        naming = mod.meme_library.MemeLibrary(root)
        naming_offer = naming.offer(scope_a, context="有点疑惑")
        check("默认候选不再随机丢弃", bool(naming_offer), True)
        if naming_offer:
            chosen = naming_offer.candidates[0]
            name_marker = f"[MEME_NAME:{chosen.token}|疑惑猫猫|疑惑,没听懂]"
            raw = name_marker + f"[MEME:{chosen.token}]"
            check(
                "命名标记不影响纯表情选择",
                mod.meme_library.strip_selection(raw, naming_offer),
                ("", chosen.token),
            )
            check("名称只能学习到原会话", naming.learn(scope_b, naming_offer, raw), 0)
            check("看图命名可以保存", naming.learn(scope_a, naming_offer, raw), 1)
            restored = mod.meme_library.MemeLibrary(root).offer(scope_a, context="疑惑")
            check(
                "重建实例后名称仍能召回",
                restored.candidates[0].title if restored else None,
                "疑惑猫猫",
            )
            check(
                "全局回复清理命名标记",
                mod.meme_library.strip_selection("答复" + name_marker),
                ("答复", None),
            )

    print()


def check_image_context(mod, check):
    """Run the image context regression scenarios."""
    print("== 读图：图必须跟人对应 ==")

    class _GEv:
        def __init__(self, gid):
            self._gid = gid

        def get_group_id(self):
            return self._gid

    class _PEv:
        def __init__(self, gid, uid):
            self._gid, self._uid = gid, uid

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return self._uid

    ic = mod.Main(None)
    t0 = time.time()
    ic._recent_images["900001"] = [
        {"url": "http://x/a.jpg", "sender": "111", "name": "甲", "ts": t0},
        {"url": "http://x/b.jpg", "sender": "222", "name": "乙", "ts": t0 + 1},
    ]
    got = ic._recent_context_image_comp(_GEv("900001"), "111")
    check("甲提问 -> 只拿甲自己的图", got[0].file if got else None, "http://x/a.jpg")
    check(
        "丙没发过图 -> 不借别人的",
        ic._recent_context_image_comp(_GEv("900001"), "333"),
        None,
    )
    ic._recent_images["900002"] = [
        {
            "url": "http://x/old.jpg",
            "sender": "111",
            "name": "甲",
            "ts": t0 - mod.IMAGE_CONTEXT_TTL - 10,
        }
    ]
    check(
        "过期的图不捎",
        ic._recent_context_image_comp(_GEv("900002"), "111"),
        None,
    )

    ic._img_wait.clear()
    ic._wait_image(_PEv("g1", "111"), "看看这张")
    check("等图：别人补发不触发", ic._take_image_wait(_PEv("g1", "222")), None)
    check("等图：本人补上才算", bool(ic._take_image_wait(_PEv("g1", "111"))), True)
    check("等图：消费后不重复触发", ic._take_image_wait(_PEv("g1", "111")), None)
    ic._wait_image(_PEv("g2", "111"), "看图")
    ic._img_wait["g2:111"]["ts"] = time.time() - mod.IMAGE_FOLLOWUP_TTL - 1
    check("等图：过期作废", ic._take_image_wait(_PEv("g2", "111")), None)

    # ---- 看不懂的梗：先搜，搜不明白就闭嘴（2026-10-25 管理员要求）----
    print()
    print("== 静默规则：看不懂的梗别硬接 ==")
    check(
        "提示词含 1.9 规则",
        "1.9【看不懂的梗别硬接】" in mod._prompt_prefix(False),
        True,
    )
    check("[静默] 视为空回复（不发进群）", mod._is_empty_reply("[静默]"), True)
    check("[静默] 带空格同理", mod._is_empty_reply(" [静默] "), True)
