"""Offline chat regression scenarios for the QQ bridge."""

import asyncio
import time

from .support import make_event_type


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


async def _stub_material(group_id, min_lines, tag, follow=False):
    """恒定返回素材，隔离"素材够不够"对本次验证的干扰。"""
    return ([{"name": "甲", "text": "随便聊聊", "ts": time.time()}], "随便聊聊")


def _stub_speak(sink):
    async def _inner(group_id, umo, prompt, tag, **kwargs):
        sink.append((group_id, tag))
        return True, "我随便说一句"

    return _inner


def check_follow(mod, check):
    """Run the follow regression scenarios."""
    print("== 同群上下文随行（2026-10-15 网关串会话治理）==")

    ctx_inst = mod.Main(None)
    # 新行为：没配梗规则、没开闲聊的群也记流水账（网关串会话后，续聊靠它）
    ctx_inst._idle_enabled_for = lambda gid: False
    ctx_inst._remember_group_message(
        _GEv2([mod.Plain(text="甲说了句甲的话")]), "甲说了句甲的话"
    )
    ctx_inst._remember_group_message(
        _GEv2([mod.Plain(text="乙回了句乙的话")], sid="10002", name="乙"),
        "乙回了句乙的话",
    )
    check(
        "无梗/无闲聊的群也记流水账",
        len(ctx_inst._gag_history.get("g-ctx") or []) == 2,
        True,
    )

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
    check(
        "群友只发图 -> 不跟随（2026-10-18）", f_inst._follow_active("g-f4", now), False
    )
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
    check(
        "间隔没到 -> 第2轮被拦（2026-10-19 降频）",
        ch_inst._follow_chain_ok("g-c1", nowc + 10),
        False,
    )
    check("间隔到了 -> 第2轮放行", ch_inst._follow_chain_ok("g-c1", nowc + 300), True)
    check("连击满2轮 -> 第3轮被拦", ch_inst._follow_chain_ok("g-c1", nowc + 700), False)
    # 不同群互不影响
    check("别的群不受本群连击影响", ch_inst._follow_chain_ok("g-c2", nowc + 20), True)
    # 冷却期过了 -> 重新给机会
    check("冷却期后恢复", ch_inst._follow_chain_ok("g-c1", nowc + 1500), True)
    # 话题冷了（群友最后一句在我这轮之前）-> 清零重新算
    ch_inst._group_activity["g-c3"] = {"last_ts": nowc, "last_text_ts": nowc - 500}
    check("连击第1轮放行(g3)", ch_inst._follow_chain_ok("g-c3", nowc), True)
    ch_inst._group_activity["g-c3"]["last_text_ts"] = (
        nowc - 1
    )  # 群友在我这轮之前就没再说话
    check(
        "话题已冷 -> 计数清零（但仍受最小间隔约束）",
        ch_inst._follow_chain_ok("g-c3", nowc + 5),
        False,
    )
    check(
        "话题已冷 + 间隔够 -> 重放", ch_inst._follow_chain_ok("g-c3", nowc + 400), True
    )
    # 面板可覆盖上限（0=不限制）
    un_inst = mod.Main(None)
    un_inst._cfg = lambda k, d: 0 if k == "follow_max_chain" else d
    for i in range(8):
        un_inst._follow_chain_ok("g-u1", nowc + i * 400)
    check(
        "上限设0=不限制（2026-10-19）",
        un_inst._follow_chain_ok("g-u1", nowc + 4000),
        True,
    )
    # ★ 无文字记录时，计数不能被误清零（否则闸门形同虚设，这是踩过的坑）
    nr_inst = mod.Main(None)
    nr_inst._cfg = lambda k, d: d
    check("无活动记录-第1轮放行", nr_inst._follow_chain_ok("g-nr", nowc), True)
    check(
        "无活动记录-第2轮也要等间隔", nr_inst._follow_chain_ok("g-nr", nowc + 5), False
    )
    check("无活动记录-间隔够则放行", nr_inst._follow_chain_ok("g-nr", nowc + 300), True)
    check(
        "无活动记录-第3轮仍被拦（连击满）",
        nr_inst._follow_chain_ok("g-nr", nowc + 700),
        False,
    )
    # 间隔设 0 = 关闭该限制（只靠连击数）
    zg = mod.Main(None)
    zg._cfg = lambda k, d: 0 if k == "follow_min_gap_sec" else d
    check(
        "间隔设0 -> 连续两次都放行",
        [zg._follow_chain_ok("g-z", nowc), zg._follow_chain_ok("g-z", nowc + 1)],
        [True, True],
    )

    print()
    print("== 跟随模式的群闸（2026-10-19：只在梗群开）==")

    fg = mod.Main(None)
    fg._cfg = lambda k, d: d
    gag_groups = set(mod.GROUP_GAG_RULES)
    check("梗群至少有两个（示例梗/示例称号群）", len(gag_groups) >= 2, True)
    check("跟随默认只认梗群", fg._follow_groups(), gag_groups)
    check("梗群允许跟随", fg._follow_allowed(min(gag_groups)), True)
    check("非梗群禁止跟随（2026-10-19）", fg._follow_allowed("555555555"), False)
    # 黑名单优先于群闸
    check(
        "黑名单群即使在梗群也不跟随",
        fg._follow_allowed("2000000004") if "2000000004" in gag_groups else True,
        True,
    )
    # 面板可覆盖群列表
    fg._cfg = lambda k, d: "123,456" if k == "follow_groups" else d
    check("面板可覆盖跟随群列表", fg._follow_allowed("123"), True)
    check("面板覆盖后梗群失效", fg._follow_allowed(min(gag_groups)), False)
    # ★ 端到端：非梗群即使冷场+群友接话，也绝不开口
    ng = mod.Main(None)
    ng._cfg = lambda k, d: d
    ng._last_spark_global = 0.0
    ng._spark_fail_streak = 0
    ng_called = []
    ng._spark_material = _stub_material
    ng._speak_spark = _stub_speak(ng_called)
    ng._group_activity["555555555"] = {
        "last_ts": time.time() - 9999,
        "last_text_ts": time.time() - 5,
        "umo": "u-ng",
    }
    ng._spark_said["555555555"] = [{"text": "我刚说的", "ts": time.time() - 30}]
    # 关掉冷场闲聊，确保"唯一可能开口的路径"就是跟随 —— 跟随被闸住就该全静
    ng._cfg = lambda k, d: False if k == "idle_chat_enabled" else d
    asyncio.run(ng._idle_scan())
    check("非梗群跟随被拦-完全静默（2026-10-19）", len(ng_called), 0)
    # 对照：同样配置但在梗群里 -> 会开口
    ok_gid = min(gag_groups)
    og = mod.Main(None)
    og._cfg = lambda k, d: False if k == "idle_chat_enabled" else d
    og._last_spark_global = 0.0
    og._spark_fail_streak = 0
    og_called = []
    og._spark_material = _stub_material
    og._speak_spark = _stub_speak(og_called)
    og._group_activity[ok_gid] = {
        "last_ts": time.time() - 10,
        "last_text_ts": time.time() - 5,
        "umo": "u-ok",
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
    gid = min(gag2)
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
            "last_ts": time.time() - quiet,
            "umo": "u-x",
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
    gid3 = min(gag3)
    check(
        "梗群跟随：连击上限=2", fq._follow_timing(gid3)[0], mod.FOLLOW_HIFREQ_MAX_CHAIN
    )
    check(
        "梗群跟随：冷却=900s",
        fq._follow_timing(gid3)[1],
        mod.FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC,
    )
    check(
        "梗群跟随：热窗口=150s",
        fq._follow_timing(gid3)[2],
        mod.FOLLOW_HIFREQ_WINDOW_SEC,
    )
    check("梗群跟随：最小间隔=240s", fq._follow_timing(gid3)[3], mod.FOLLOW_MIN_GAP_SEC)
    check(
        "非梗群跟随：2/900/180/240",
        fq._follow_timing("777777777"),
        (
            mod.FOLLOW_MAX_CHAIN,
            mod.FOLLOW_CHAIN_COOLDOWN_SEC,
            180,
            mod.FOLLOW_MIN_GAP_SEC,
        ),
    )
    check("梗群热窗口确实更短（降频）", fq._follow_timing(gid3)[2] < 180, True)
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
    check(
        "新群享受提频阈值",
        n3._idle_timing(g3),
        (mod.IDLE_HIFREQ_TRIGGER_SEC, mod.IDLE_HIFREQ_COOLDOWN_SEC),
    )
    # 结构性护栏：所有梗群在三处口径里必须一致，别只加了一处
    check(
        "三处口径完全一致",
        n3._hifreq_groups() == n3._follow_groups() == set(mod.GROUP_GAG_RULES),
        True,
    )

    print()


def check_sparks(mod, check):
    """Run the sparks regression scenarios."""
    _Ev = make_event_type(mod)
    now = time.time()
    print("== 闭嘴黑名单：整条巡检路径都拦（2026-10-19）==")
    # 现场：群 2000000004 抱怨太频繁，但面板开关关了没用——因为跟随路径
    # 绕过了 _idle_enabled_for。这里直接驱动 _idle_scan 验证它真的不开口。

    scan = mod.Main(None)
    scan._cfg = lambda k, d: d
    scan._last_spark_global = 0.0
    scan._spark_fail_streak = 0
    called = []
    scan._spark_material = _stub_material
    scan._speak_spark = _stub_speak(called)
    scan._group_activity["2000000004"] = {
        "last_ts": time.time() - 9999,  # 早就冷场，理论上必开口
        "last_text_ts": time.time() - 5,  # 且群友刚说过话，跟随也成立
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
    check(
        "无图时挑不到", [h for h in no_img if str(h.get("img_url") or "").strip()], []
    )
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
    check(
        "叫名字+问句 -> 算提问", mod._looks_like_question("大肥鱼你知道这个梗吗"), True
    )
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
    check(
        "网关不可达->空壳",
        mod._is_empty_reply("[网关不可达] 找不到 WorkBuddy Gateway。"),
        True,
    )
    check(
        "凭据失效->空壳",
        mod._is_empty_reply("[凭据失效] 网关拒绝认证，请让AI 重新生成"),
        True,
    )
    check(
        "无输出->空壳",
        mod._is_empty_reply("[无输出] WorkBuddy 网关没有返回内容事件。"),
        True,
    )
    check("超时->空壳", mod._is_empty_reply("[超时] WorkBuddy 处理超过 300 秒。"), True)
    check("网关错误->空壳", mod._is_empty_reply("[网关错误] 没有返回 runId。"), True)
    check(
        "连接失败->空壳",
        mod._is_empty_reply("[连接失败] 无法访问 WorkBuddy 网关"),
        True,
    )
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
    cb2._account_ok = True
    sent2 = []
    cb2._ask = _stub_ask("[排队] 桌面端当前会话正忙，本轮没轮到执行。")
    cb2._send_to_session = _stub_send(sent2)
    for _ in range(5):
        _run(cb2._speak_spark("g_q", "u_q", "聊聊", "闲聊"))
    check("连续 5 次排队都不熔断", cb2._spark_fail_streak, 0)
    check("排队时一条都不发", len(sent2), 0)

    # 真故障仍要熔断（别把熔断一起改没了）
    cb3 = mod.Main(None)
    cb3._account_ok = True
    sent3 = []
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
    check(
        "命中「有没有人」",
        mod._hit_words("有没有人知道这个梗什么意思", kb._kw_words()),
        "有没有人",
    )
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
    kt2._kw_last[kg] = time.time() - 99999  # 本群冷却已过
    kt2._kw_last_global = time.time()  # 但别的群刚开过腔
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
        def __init__(
            self,
            comps,
            gid,
            sid="10001",
            name="甲",
            umo="qq:GroupMessage:x",
            me="999999",
        ):
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
    ev_at_me = _KWBEv(
        [mod.At(qq="999999")], kg, umo=f"qq:GroupMessage:{kg}", me="999999"
    )

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
    ev_out = _KWBEv(
        [mod.Plain(text="有没有人啊")], "g-not-in-rules", umo="qq:GroupMessage:out"
    )

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
    ev_in = _KWBEv(
        [mod.Plain(text="有没有人啊这什么意思")], kg, umo=f"qq:GroupMessage:{kg}"
    )

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
    check(
        "刚失败-> 处于退避期",
        (time.time() - mod._GW_CACHE["miss_at"]) < mod.GW_RESCAN_COOLDOWN_SEC,
        True,
    )
    # 退避期过后=> 允许重新扫
    mod._GW_CACHE["miss_at"] = time.time() - 99
    check(
        "退避期过-> 可重扫",
        (time.time() - mod._GW_CACHE["miss_at"]) < mod.GW_RESCAN_COOLDOWN_SEC,
        False,
    )
    mod._GW_CACHE["miss_at"] = 0.0

    print()
    print("== @ 路径也能续上（记账覆盖两条路径）==")
    at_inst = mod.Main(None)
    at_inst._idle_enabled_for = lambda gid: False  # 该群没开"适时闲聊"
    # 模拟 @ 回复后的记账（这条路径以前不记，是断链的根因）
    at_bucket = at_inst._spark_said.setdefault("g-at", [])
    at_bucket.append({"text": "我回答@的内容", "ts": now - 20})
    at_inst._group_activity["g-at"] = {
        "last_ts": now - 5,
        "last_text_ts": now - 5,
        "umo": "u1",
    }
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
    check(
        "active=true -> 进行中",
        mod.Main._parse_run_active('{"data":{"active":true}}'),
        True,
    )
    check(
        "active=false -> 已结束",
        mod.Main._parse_run_active('{"data":{"active":false}}'),
        False,
    )
    check("非法JSON -> None", mod.Main._parse_run_active("not-json"), None)
    check("缺active字段 -> None", mod.Main._parse_run_active('{"data":{}}'), None)

    print()
