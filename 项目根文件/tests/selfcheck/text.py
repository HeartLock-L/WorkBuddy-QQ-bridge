"""Offline text regression scenarios for the QQ bridge."""

import time

from .support import TEST_BOT_ID, make_event_type


def check_text(mod, check):
    """Run the text regression scenarios."""
    import asyncio as _aio

    _Ev = make_event_type(mod)
    me = TEST_BOT_ID
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
    check(
        "成对加粗被洗掉",
        strip("这个**很关键**，要**认真**看。") == "这个很关键，要认真看。",
        True,
    )
    check(
        "连续加粗被洗掉",
        strip("**搜索不丢人**，**答错才丢人**。") == "搜索不丢人，答错才丢人。",
        True,
    )
    check("落单加粗标记被清掉", "**" not in strip("**开头有星星没结尾"), True)
    check("无序列表符号被去掉", strip("- 甲\n- 乙") == "甲\n乙", True)
    check("星号列表被去掉", strip("* 甲\n* 乙") == "甲\n乙", True)
    check("有序列表编号被去掉", strip("1. 先热身\n2. 再跑步") == "先热身\n再跑步", True)
    check("标题井号被去掉", strip("# 大标题\n正文") == "大标题\n正文", True)
    # 分隔线删掉后留下一个空行 —— 那是自然分段，不压（只有 3+ 换行才压）
    check("分隔线被去掉", "---" not in strip("上面\n---\n下面"), True)
    check("引用符号被去掉", strip("> 他说过") == "他说过", True)
    check(
        "行内代码反引号被去掉",
        strip("用 `pip install` 装") == "用 pip install 装",
        True,
    )
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
    check(
        "执行模式提示词含循环节规则",
        "循环节" in mod._prompt_prefix(True, mode="execute"),
        True,
    )

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
        "本会话标识"
        in mod._prompt_prefix(False, "chat", None, "", channel="qq-abc123"),
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
    check(
        "「（本轮……不发言。）」拦下",
        mod._is_meta_note("（本轮无合适接话点：话题已结束，不发言。）"),
        True,
    )
    check("「（这条跳过）」拦下", mod._is_meta_note("（这条跳过）"), True)
    check("「（素材……不接）」拦下", mod._is_meta_note("（素材不足，先不接）"), True)
    check("「（无合适……跳过）」拦下", mod._is_meta_note("（无合适接话点，跳过）"), True)
    check(
        "光有前导无关键词 -> 保守放行（设计如此）",
        mod._is_meta_note("（无合适接话点）"),
        False,
    )
    check("半角括号同样拦", mod._is_meta_note("(本轮不宜参与)"), True)
    # 不误伤（宁可漏拦、不可误伤）
    check(
        "正文含'不接'但开头不对 -> 不拦",
        mod._is_meta_note("这活我不接，找别人吧"),
        False,
    )
    check(
        "「（本轮比赛真精彩）」不拦（无关键词）",
        mod._is_meta_note("（本轮比赛真精彩）"),
        False,
    )
    check(
        "超 200 字不拦（保守策略）",
        mod._is_meta_note("（本轮" + "啊" * 210 + "不发言）"),
        False,
    )
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

    r_open = rp._handle_roleplay_cmd(
        _Ev(me, [], "g-rp1"), "扮演 一个话少的剑客", "10001", False
    )
    check("开场有回执", bool(r_open) and "入戏" in r_open, True)
    check(
        "人设写入本群",
        (rp._roleplay.get("g-rp1") or {}).get("persona"),
        "一个话少的剑客",
    )
    check("取用返回人设", rp._current_persona(_Ev(me, [], "g-rp1")), "一个话少的剑客")
    check("未开演的群取不到", rp._current_persona(_Ev(me, [], "g-rp2")), "")
    r_nop = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp2"), "扮演", "10001", False)
    check("未带设定给出提示", bool(r_nop) and "给个设定" in r_nop, True)
    check(
        "日常说法不触发",
        rp._handle_roleplay_cmd(_Ev(me, [], "g-rp1"), "我想扮演一下", "10001", False),
        None,
    )
    r_stop = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp1"), "结束扮演", "10001", False)
    check("收场有回执", bool(r_stop) and "出戏" in r_stop, True)
    check("收场后已清除", "g-rp1" in rp._roleplay, False)
    r_none = rp._handle_roleplay_cmd(_Ev(me, [], "g-rp3"), "结束扮演", "10001", False)
    check("没在演时收场", bool(r_none) and "没在演" in r_none, True)

    rp._roleplay["g-rp9"] = {
        "persona": "旧人设",
        "owner": "1",
        "ts": time.time() - 10**6,
    }
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
    check(
        "非管理员被拒",
        _aio.run(rp._handle_relay_cmd(ev0, "代发 123456 内容", False)),
        "这条只有号主能用。",
    )
    check(
        "缺内容给格式提示",
        _aio.run(rp._handle_relay_cmd(ev0, "代发 123", True)),
        "格式是这样：代发 群号 内容",
    )
    check(
        "群号非数字给格式提示",
        _aio.run(rp._handle_relay_cmd(ev0, "代发 abc 内容", True)),
        "格式是这样：代发 群号 内容",
    )
    check(
        "普通文本不触发",
        _aio.run(rp._handle_relay_cmd(ev0, "今天天气不错", True)),
        None,
    )

    print()
    print("== 出口清洗：_sanitize（所有人）+ _strict_filter（非管理员加严） ==")
    mixed = (
        "第一句正文。\n[2026-10-14 10:00:00] [Core] [INFO] [x]: 内部日志\n第二句正文。"
    )
    out_mixed = mod._sanitize(mixed)
    check("日志行被整行删除", "内部日志" not in out_mixed, True)
    check(
        "正常正文保留",
        ("第一句正文" in out_mixed) and ("第二句正文" in out_mixed),
        True,
    )
    check(
        "本机 URL 变占位",
        "[本机地址]" in mod._sanitize("打开 http://127.0.0.1:6185 看看"),
        True,
    )
    check(
        "localhost:端口 变占位",
        "[本机端口]" in mod._sanitize("连的是 localhost:6099"),
        True,
    )
    check(
        "「端口 6185」被隐藏", mod._sanitize("端口 6185 在监听"), "端口[已隐藏] 在监听"
    )
    check(
        "绝对路径变占位",
        "[路径]" in mod._sanitize(r"文件在 F:\workbuddy\QQbot\test.txt 里"),
        True,
    )
    check(
        "长数字（QQ号）变占位",
        "[号码]" in mod._sanitize("他的号 123456789 记一下"),
        True,
    )
    check(
        "全内部信息 -> 兜底话术",
        mod._sanitize("[INFO]"),
        "（这条回复只包含内部信息，已过滤）",
    )

    check("非管理员：邮箱脱敏", "[邮箱]" in mod._strict_filter("联系我 a@b.com"), True)
    check(
        "非管理员：手机号脱敏",
        "[手机号]" in mod._strict_filter("打 13812345678 找我"),
        True,
    )
    check(
        "非管理员：IP 脱敏",
        "[IP]" in mod._strict_filter("IP 是 192.168.1.1 记得"),
        True,
    )
    check(
        "非管理员：含内部词整行删除",
        "端口" not in mod._strict_filter("第一句。\n端口 6185 在跑\n第三句。"),
        True,
    )
    check(
        "非管理员：删空后换拒绝话术", mod._strict_filter("端口 6185"), mod.REFUSE_TEXT
    )

    print()
    print("== 审批判定三层（强操作 / 机器产物 / 咨询豁免） ==")
    check(
        "兼容别名 = 强操作+机器产物",
        mod.ACTION_WORDS == mod.STRONG_ACTION_WORDS + mod.MACHINE_OBJECTS,
        True,
    )
    check("纯机器产物 -> 拦", bool(mod._is_action_request("那份表格在哪")), True)
    check("咨询词豁免机器产物", mod._is_action_request("推荐一本讲表格的书"), None)
    check(
        "强操作词不豁免", bool(mod._is_action_request("帮我删掉这几个文件再分析")), True
    )

    print()
    print("== 面板配置回退（无配置时用代码默认，不至于失灵） ==")
    check("_cfg 回退默认", rp._cfg("不存在的键", "x"), "x")
    check("_cfg_bool 回退默认", rp._cfg_bool("不存在的键", True), True)
    check("_cfg_int 回退默认", rp._cfg_int("不存在的键", 900), 900)

    check("_safe_name 清洁非法字符", mod._safe_name("a/b:c"), "abc")
    check("_safe_name 空名兜底", mod._safe_name(""), "用户")

    print()


def check_receipts_search(mod, check):
    """Run the receipts search regression scenarios."""
    print("== 回执码闸：串台产物必须被丢弃（2026-10-22） ==")
    nonce = "abc12345"
    receipt = f"确认-{nonce}"
    body, hit = mod._strip_receipt(f"{receipt}\n今天群里挺热闹", nonce)
    check("正常回复 -> 正文+命中", (body, hit), ("今天群里挺热闹", True))
    body, hit = mod._strip_receipt(f"窗口里的内部汇报\n{receipt}\n接话正文", nonce)
    check("回执前的外来文本被切掉", (body, hit), ("接话正文", True))
    body, hit = mod._strip_receipt("只有窗口里的内部汇报，没有回执码", nonce)
    check("无回执码 -> 不命中", hit, False)
    check(
        "无回执码 -> 整段丢弃",
        mod._receipt_gate("只有窗口里的内部汇报", nonce),
        "[回复校验失败]",
    )
    check(
        "网关兜底串原样放行",
        mod._receipt_gate("[排队] 桌面端当前会话正忙", nonce),
        "[排队] 桌面端当前会话正忙",
    )
    check("回执后无正文 -> 按空回复", mod._receipt_gate(f"{receipt}", nonce), "[静默]")

    print()
    print("== 联网检索：触发判定与查询串清洗（2026-10-22） ==")
    for _t, _want in [
        ("搜一下今日美元汇率", True),
        ("今天美元汇率多少", True),
        ("现在金价多少钱", True),
        ("查一下最近的新闻", True),
        ("这价格真香", False),
        ("今天真开心", False),
        ("铁墓是什么", False),
        ("", False),
        # 2026-10-22 放宽后：没有「今天/搜一下」的事实问句也该查
        ("DeepSeek V4 发布了吗", True),
        ("2026 年诺示例词文学奖是谁", True),
        ("库里今天得分多少", True),
        ("最近有什么新番", True),
        ("新版本上线了吗", True),
        # 要观点/闲聊的照旧不查
        ("这个显卡怎么样", False),
        ("马斯克现在在干嘛", False),
        ("现在几点", False),
        ("你吃饭了吗", False),
        ("帮我想个名字", False),
    ]:
        check(f"「{_t}」需要搜索={_want}", mod._needs_search(_t), _want)
    check(
        "@ 与链接被清掉、指令词被剥掉",
        mod._clean_search_query("[At:1000000001] 搜一下今日美元汇率 https://x.com/a"),
        "今日美元汇率",
    )
    check(
        "搜索块带免责说明",
        "不是群友说的话" in mod._search_block("- 标题：摘要"),
        True,
    )

    print()
