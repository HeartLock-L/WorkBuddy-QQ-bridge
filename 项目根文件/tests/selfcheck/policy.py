"""Offline policy regression scenarios for the QQ bridge."""

from .support import TEST_BOT_ID, make_event_type


def check_core(mod, check):
    """Run the core regression scenarios."""
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
    check(
        "重载指令：普通文本不误触", inst._handle_reload_cmd("今天天气不错", True), None
    )

    print()


def check_group_rules(mod, check):
    """Run the group rules regression scenarios."""
    _Ev = make_event_type(mod)
    me = TEST_BOT_ID
    print("== 群专属梗规则（示例梗 / 示例称号） ==")
    gag_group = "2000000001"
    other_group = "2000000006"

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
        bool(mod._hit_group_gag(_Ev(me, [mod.At(qq="1000000002")], gag_group), "快看")),
        True,
    )
    check(
        "别的群不触发",
        bool(
            mod._hit_group_gag(_Ev(me, [mod.Plain(text="示例梗")], other_group), "示例梗")
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
            mod._hit_group_gag(_Ev(me, [mod.At(qq="1000000002")], gag_group), "看这个")
            or {}
        ).get("target_id"),
        "1000000002",
    )
    check(
        "示例称号规则在第二个群同样生效",
        bool(
            mod._hit_group_gag(_Ev(me, [mod.Plain(text="示例词")], "2000000002"), "示例词")
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
            mod._hit_group_gag(_Ev(me, [mod.Plain(text="示例词")], other_group), "示例词")
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
    check("任意群都判定为开启", mod.Main(None)._idle_enabled_for("999999999"), True)
    # Exercise the production method with panel settings and constant fallbacks.
    for label, config, group_id, expected in (
        ("面板关闲聊优先于所有群模式", {"idle_chat_enabled": False}, "g-idle", False),
        ("字符串关闭配置生效", {"idle_chat_enabled": "false"}, "g-idle", False),
        (
            "面板黑名单优先于所有群模式",
            {"idle_chat_blacklist": "g-idle"},
            "g-idle",
            False,
        ),
        ("默认黑名单优先于所有群模式", {}, mod.IDLE_CHAT_BLACKLIST[0], False),
        (
            "面板白名单允许指定群",
            {"idle_chat_all_groups": False, "idle_chat_groups": " g-idle, g-other "},
            "g-idle",
            True,
        ),
        (
            "面板白名单排除其他群",
            {"idle_chat_all_groups": False, "idle_chat_groups": "g-other"},
            "g-idle",
            False,
        ),
        (
            "空白名单回退代码默认",
            {"idle_chat_all_groups": False, "idle_chat_groups": " "},
            mod.IDLE_CHAT_GROUPS[0],
            True,
        ),
        ("默认白名单排除其他群", {"idle_chat_all_groups": False}, "g-idle", False),
        (
            "黑名单优先于面板白名单",
            {
                "idle_chat_all_groups": False,
                "idle_chat_groups": "g-idle",
                "idle_chat_blacklist": "g-idle",
            },
            "g-idle",
            False,
        ),
    ):
        check(
            label, mod.Main(None, config=config)._idle_enabled_for(group_id), expected
        )

    print()
    print("== 角色扮演（按群隔离的人设） ==")
    check("RP 总开关默认开", mod.ROLEPLAY_ENABLED, True)
    check("RP 白名单默认空（谁都不行）", len(mod.ROLEPLAY_ALLOWED), 0)
    check("RP 默认对所有群友开放（无需白名单）", mod.ROLEPLAY_OPEN_TO_ALL, True)
    check(
        "未扮演时不加人设说明",
        "这个群现在正在角色扮演" not in mod._prompt_prefix(False, "chat"),
        True,
    )
    check(
        "有人设时写进提示词",
        "这个群现在正在角色扮演"
        in mod._prompt_prefix(False, "chat", None, "一个话少的剑客"),
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
        any(mod._is_empty_reply(s) for s in mod.ATTACHMENT_SILENT_REPLY),
        False,
    )
    check(
        "兜底节流为正数（防止刷屏）",
        mod.SILENT_NOTICE_THROTTLE_SEC > 0,
        True,
    )
    check(
        "Main 有节流用的字典",
        "_silent_notice"
        in (__import__("pathlib").Path(mod.__file__).parent / "main.py").read_text(
            encoding="utf-8",
        ),
        True,
    )

    print()
    print("== 现拉群历史参数 ==")
    src = __import__("inspect").getsource(mod.Main._fetch_recent_messages)
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
