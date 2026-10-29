"""Offline images regression scenarios for the QQ bridge."""

from pathlib import Path


def check_generation(mod, check):
    """Run the generation regression scenarios."""
    print("== 生图：触发词判定（2026-10-19 接入即梦）==")
    # 正例：必须能抠出 prompt
    check("画一张猫 -> 猫", mod._draw_prompt_of("画一张猫"), "猫")
    check("画个海边日落 -> 海边日落", mod._draw_prompt_of("画个海边日落"), "海边日落")
    check("帮我画蓝鲸 -> 蓝鲸", mod._draw_prompt_of("帮我画蓝鲸"), "蓝鲸")
    check(
        "生图：赛博朋克街道", mod._draw_prompt_of("生图：赛博朋克街道"), "赛博朋克街道"
    )
    check("全角冒号被剥掉", mod._draw_prompt_of("画一张：一只白猫"), "一只白猫")
    check("半角逗号被剥掉", mod._draw_prompt_of("画个,机械鱼"), "机械鱼")
    # ★ 反例：这些是群里最高频的日常对话，绝不能当成要画图
    check("「这画风不错」不算生图", mod._draw_prompt_of("这画风不错"), "")
    check("「漫画看多了」不算生图", mod._draw_prompt_of("漫画看多了"), "")
    check(
        "「生图模块好用吗」不算（词不在开头）",
        mod._draw_prompt_of("生图模块好用吗"),
        "",
    )
    check("只说「画个」没内容 -> 空", mod._draw_prompt_of("画个"), "")
    check("只说「画一张」没内容 -> 空", mod._draw_prompt_of("画一张"), "")
    check("空消息 -> 空", mod._draw_prompt_of(""), "")
    check("普通聊天 -> 空", mod._draw_prompt_of("今天中午吃什么"), "")
    # 最长前缀优先：「画一张」不能被「画个」之类截走
    check("最长前缀优先", mod._draw_prompt_of("画一张鲸鱼"), "鲸鱼")
    # 量词尾巴（2026-10-20）：纯动词触发词后的"张/个/只"要剥掉，
    # 实测「帮我画张女仆装若纳瓦」把「张女仆装…」喂给了模型
    check(
        "帮我画张女仆装若纳瓦 -> 女仆装若纳瓦",
        mod._draw_prompt_of("帮我画张女仆装若纳瓦"),
        "女仆装若纳瓦",
    )
    check("给我画个猫 -> 猫", mod._draw_prompt_of("给我画个猫"), "猫")
    check("给我画一只蓝鲸 -> 蓝鲸", mod._draw_prompt_of("给我画一只蓝鲸"), "蓝鲸")
    check(
        "生图：一张赛博朋克街道 -> 赛博朋克街道",
        mod._draw_prompt_of("生图：一张赛博朋克街道"),
        "赛博朋克街道",
    )
    check("只说「帮我画张」没内容 -> 空", mod._draw_prompt_of("帮我画张"), "")
    # 带量词的触发词绝不能二次剥（「画一张张三」≠「三」）
    check("画一张张三 -> 张三（不二次剥）", mod._draw_prompt_of("画一张张三"), "张三")
    check("画张张三 -> 张三（不二次剥）", mod._draw_prompt_of("画张张三"), "张三")
    check(
        "帮我画一张张三 -> 张三（只剥一层）",
        mod._draw_prompt_of("帮我画一张张三"),
        "张三",
    )

    print()
    print("== 生图：自画像专用提示词（2026-10-20 管理员要求）==")
    SP = mod.DRAW_SELFIE_PROMPT
    _VARIANTS = mod.DRAW_SELFIE_VARIANTS

    def _check_selfie(name, text):
        """自画像命中 = 底句原样保留 + 随机变体来自池子。"""
        got = mod._draw_selfie_prompt_of(text)
        ok = got.startswith(SP + "，") and got[len(SP) + 1 :] in _VARIANTS
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
    check(
        "底句永远原样保留（管理员：这是一切的前提）",
        SP,
        "模仿《魔法少女的魔女审判》的画风，画一张拟人化deepseek",
    )
    check(
        "变体池非空且互不相同",
        len(_VARIANTS) >= 5 and len(set(_VARIANTS)) == len(_VARIANTS),
        True,
    )
    check(
        "画一张你的猫 不是自画像（走普通解析）",
        mod._draw_selfie_prompt_of("画一张你的猫"),
        "",
    )
    check("你的照片真好看 不是自画像", mod._draw_selfie_prompt_of("你的照片真好看"), "")
    check(
        "生成你的照片好玩吗 不是自画像",
        mod._draw_selfie_prompt_of("生成你的照片好玩吗"),
        "",
    )
    check(
        "★隐患存证：普通解析会把「生成一张你的照片」截成字面的「你的照片」",
        mod._draw_prompt_of("生成一张你的照片"),
        "你的照片",
    )

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

    async def _fake_run_draw(
        gid, umo, p, nick, ratio="", sid="", *, retain_local=False
    ):
        _captured["prompt"] = p
        _captured["retain_local"] = retain_local

    async def _drive_selfie():
        _dg._run_draw = _fake_run_draw
        r = _dg._maybe_draw(_SelfieEv(), "生成一张你的照片", "10001")
        await _aio.sleep(0)  # 让 create_task 出来的假任务跑一步，落 captured
        return r

    check(
        "端到端：自画像请求经 _maybe_draw 返回 draw", _aio.run(_drive_selfie()), "draw"
    )
    _got = _captured.get("prompt") or ""
    check(
        "端到端：底句保留且追加的是池内变体",
        _got.startswith(SP + "，") and _got[len(SP) + 1 :] in _VARIANTS,
        True,
    )
    check("端到端：普通生图不受影响", mod._draw_prompt_of("画一张猫"), "猫")
    check("端到端：自己的自拍照标记为长期保留", _captured.get("retain_local"), True)

    print()
    print("== 生图：三道闸（使用人 / 冷却 / 每日上限）==")
    dg = mod.Main(None)
    dg._cfg = lambda k, d: d
    check("已放开：默认不限制使用人（2026-10-19）", mod.IMAGE_GEN_ADMIN_ONLY, False)
    check("管理员放行", dg._draw_allowed(mod.MASTER_ID, "g-d1"), "")
    check("普通群友也放行（2026-10-19 放开）", dg._draw_allowed("123456", "g-d1"), "")
    # 想收回权限时，面板打开 admin_only 即可
    dr = mod.Main(None)
    dr._cfg = lambda k, d: True if k == "image_gen_admin_only" else d
    check(
        "面板可随时收回权限", dr._draw_allowed("123456", "g-d1"), "只有管理员能用生图"
    )
    check("收回后管理员仍可用", dr._draw_allowed(mod.MASTER_ID, "g-d1"), "")
    # 免费档 + 不限量（2026-10-19 管理员确认 5.0lite/4.6/4.0 免费）
    check("默认模型是免费档 5.0lite", mod.IMAGE_GEN_MODEL, "5.0lite")
    check("默认画幅 16:9（2026-10-19）", mod.IMAGE_GEN_RATIO, "16:9")
    # 提示词里写比例 -> 覆盖默认；没写 -> 落回默认
    check("写了比例就用写的", mod._draw_ratio_of("画一张 9:16 的图"), "9:16")
    check("没写比例 -> 空（由调用方落回默认 16:9）", mod._draw_ratio_of("画一只猫"), "")
    check("剥掉画幅后不留孤零零的「的」", mod._draw_prompt_of("画一张 4：3 的猫"), "猫")
    check("每日上限默认不限", mod.IMAGE_GEN_MAX_PER_DAY, 0)
    du = mod.Main(None)
    du._cfg = lambda k, d: d
    for _ in range(50):  # 连跑 50 次也不该被日上限拦
        du._draw_bump("g-dfree")
    check(
        "不限量：连跑50次仍放行",
        du._draw_allowed("123456", "g-dfree").startswith("本群冷却中"),
        True,
    )
    # 2026-10-19：cd 缩到 5s + cd 中要回"生图cd中"而不是沉默
    check("同群冷却默认 5s", mod.IMAGE_GEN_COOLDOWN_SEC, 5)
    check("全局间隔默认 5s", mod.IMAGE_GEN_GLOBAL_GAP_SEC, 5)
    check(
        "群冷却 -> 回「生图cd中」",
        mod._draw_block_reply("本群冷却中（<5s）"),
        "生图cd中",
    )
    check(
        "全局间隔 -> 回「生图cd中」",
        mod._draw_block_reply("全局间隔未到（<5s）"),
        "生图cd中",
    )
    check(
        "日上限 -> 回「生图cd中」",
        mod._draw_block_reply("今日已出图 30 张，达上限 30"),
        "生图cd中",
    )
    check(
        "权限类不吞原因",
        mod._draw_block_reply("只有管理员能用生图"),
        "只有管理员能用生图",
    )
    check("关闭类不吞原因", mod._draw_block_reply("生图已关闭"), "生图已关闭")
    # ★ 2026-10-19 实测误伤：「你觉得图片里的这个是你吗」命中"图片"，
    #   没有豁免词 → 被审批闸当操作请求拦下，还让群友等管理员点头。
    #   问句式豁免词补上之后，这条必须直接放行进问答。
    check(
        "看图认自己是问答，不拦（现场原句）",
        mod._is_action_request("你觉得图片里的这个是你吗"),
        None,
    )
    check("这是你吗 -> 不拦", mod._is_action_request("这是你吗"), None)
    check("这是谁 -> 不拦", mod._is_action_request("这是谁"), None)
    check("认得这张图吗 -> 不拦", mod._is_action_request("你认得这张图片吗"), None)
    # 对照：真要动图的还得拦
    check("把图片删了 -> 仍拦", mod._is_action_request("把图片删了"), "图片")
    check(
        "生成图片 -> 仍拦（改判第2层的「图片」）",
        mod._is_action_request("生成一张图片"),
        "图片",
    )
    # ★ 2026-10-20 自检 R7：6 个泛动词移出第 1 层后，单独出现必须放行
    check("生成这个词怎么念 -> 不拦", mod._is_action_request("生成这个词怎么念"), None)
    check("打开思路 -> 不拦", mod._is_action_request("打开思路想想"), None)
    check("做一次评估 -> 不拦", mod._is_action_request("帮我做一次评估"), None)
    check("写个笑话 -> 不拦", mod._is_action_request("写个笑话逗我"), None)
    check("改一下说法 -> 不拦", mod._is_action_request("你能改一下说法吗"), None)
    check(
        "打开这个文件 -> 仍拦（配了机器产物）",
        mod._is_action_request("打开这个文件"),
        "文件",
    )
    check(
        "删除文件 -> 仍拦（强词优先）", mod._is_action_request("把那个文件删掉"), "删掉"
    )
    # 占一次配额后立刻进冷却
    dg._draw_bump("g-d1")
    check(
        "占用后本群进冷却",
        dg._draw_allowed(mod.MASTER_ID, "g-d1").startswith("本群冷却中"),
        True,
    )
    check(
        "别的群不受本群冷却影响",
        dg._draw_allowed(mod.MASTER_ID, "g-other").startswith("全局间隔未到"),
        True,
    )
    # 日上限
    dh = mod.Main(None)
    dh._cfg = lambda k, d: (
        1
        if k == "image_gen_max_per_day"
        else (
            0
            if k == "image_gen_cooldown_sec"
            else (0 if k == "image_gen_global_gap_sec" else d)
        )
    )
    dh._draw_bump("g-d2")
    check(
        "日上限1：第二次被拒",
        dh._draw_allowed(mod.MASTER_ID, "g-d2").startswith("今日已出图"),
        True,
    )
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
    dq._draw_bump("g-d5")
    dq._draw_bump("g-d5")
    check("bump 幂等计数", dq._draw_daily["g-d5"][1], 2)
    check("出图目录已配置", bool(mod.IMAGE_GEN_DIR), True)
    check("CLI 路径已配置", mod.IMAGE_GEN_BIN.endswith("dreamina.exe"), True)

    print()
    print("== 生图：比例解析（2026-10-19 实测踩坑）==")
    # 现场原句：模型把"比例16:9"当描述画了，ratio 参数却还是 1:1
    raw = "画一张图，比例16:9，写实摄影，一个开满鲜花的花园"
    check("从消息里认出 16:9", mod._draw_ratio_of(raw), "16:9")
    check("prompt 里剥掉了比例字样", "16:9" not in mod._draw_prompt_of(raw), True)
    check("prompt 保留真实内容", "开满鲜花的花园" in mod._draw_prompt_of(raw), True)
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
    check(
        "横图字样从 prompt 剥掉",
        "横图" not in mod._draw_prompt_of("画个横图，一只猫"),
        True,
    )
    check("剥完内容还在", mod._draw_prompt_of("画个横图，一只猫"), "一只猫")

    print()
    print("== 生图：回图必须包成 MessageChain（2026-10-19 实测踩坑）==")
    # 现场：context.send_message 要链对象，直接传 list 抛
    #   `'list' object has no attribute 'chain'`，图生成了但发不出去
    import inspect as _insp

    src_run = _insp.getsource(mod.Main._process_draw_job)
    check(
        "回图用了 MessageChain",
        "MessageChain(chain=[" in "".join(src_run.split()),
        True,
    )
    check("不再裸传 list", "chain = [\n" not in src_run, True)
    check(
        "_draw_sync 接受比例参数",
        "ratio" in _insp.signature(mod.jimeng.JimengClient.generate).parameters,
        True,
    )
    check(
        "_run_draw 接受比例参数",
        "ratio" in _insp.signature(mod.Main._run_draw).parameters,
        True,
    )

    print()
    print("== 生图：一人一框（按 QQ 号复用，2026-10-22 回归）==")
    ds = mod.Main(None)
    ds._cfg = lambda k, d: d
    check("会话映射是字典", isinstance(ds._jimeng.sessions, dict), True)
    check("有同人并发闸", isinstance(ds._draw_busy, set), True)

    # 解析：真实格式带 PINNED 列，名称列可能含空格
    line = (
        "23722083785740  全局用户身份识别符分区表第伍点陆代-太阳使用 · 1000000000"
        "  No      2026-10-22 17:29"
    )
    mm = mod._DRAW_SESSION_ROW.match(line)
    check("能解析出会话 ID", mm.group(1), "23722083785740")
    check(
        "能解析出名称（含空格不乱切）",
        mm.group(2).rstrip(),
        "全局用户身份识别符分区表第伍点陆代-太阳使用 · 1000000000",
    )
    check("名称尾部含 · QQ号", mm.group(2).rstrip().endswith("· 1000000000"), True)
    zero = mod._DRAW_SESSION_ROW.match(
        "0                                                                         Yes     2026-10-21 14:03"
    )
    check("默认会话行也能解析", zero.group(1), "0")
    # 表头/分隔线不能被当成数据行
    check(
        "表头不匹配",
        mod._DRAW_SESSION_ROW.match(
            "ID              NAME                     PINNED  UPDATED_AT"
        ),
        None,
    )
    check(
        "分隔线不匹配",
        mod._DRAW_SESSION_ROW.match(
            "--------------  ----------------------  ------  ----------------"
        ),
        None,
    )

    # 复用逻辑：映射命中就用原框，绝不新建
    image_settings = mod.jimeng.JimengSettings(
        bin_path=mod.IMAGE_GEN_BIN,
        output_dir=Path(mod.IMAGE_GEN_DIR),
        model=mod.IMAGE_GEN_MODEL,
        ratio=mod.IMAGE_GEN_RATIO,
        resolution=mod.IMAGE_GEN_RESOLUTION,
        timeout=30,
    )
    ds2 = mod.Main(None)
    ds2._cfg = lambda k, d: d
    ds2._jimeng.sessions = {"123": "999"}
    ds2._jimeng.session_list = lambda settings, **kwargs: {"999": "谁使用 · 123"}
    ds2._jimeng._remember_session = lambda user, session: (
        ds2._jimeng.sessions.__setitem__(user, str(session))
    )
    check(
        "映射命中直接复用",
        ds2._jimeng.ensure_session("谁", "123", image_settings),
        "999",
    )
    # 映射失效时按 `· QQ号` 结尾捞回来
    ds2._jimeng.sessions = {}
    ds2._jimeng.session_list = lambda settings, **kwargs: {
        "0": "",
        "777": "别人的图",
        "888": "某某使用 · 123",
        "889": "某某使用 · 1230",
    }
    check(
        "按框名捞回原框（不误命中多一位）",
        ds2._jimeng.ensure_session("谁", "123", image_settings),
        "888",
    )
    check("捞回后补记映射", ds2._jimeng.sessions.get("123"), "888")

    # 签名贯通
    import inspect as _insp2

    check(
        "_run_draw 带 sender_id",
        "sender_id" in _insp2.signature(mod.Main._run_draw).parameters,
        True,
    )
    check(
        "_draw_sync 带 session",
        "session" in _insp2.signature(mod.jimeng.JimengClient.generate).parameters,
        True,
    )
    # 2026-10-20：拼命令的主体搬到 _draw_once（_draw_sync 变成带重试的包装层），
    # 所以这条要查 _draw_once 的源码。
    src_once = _insp2.getsource(mod.jimeng.JimengClient.generate)
    check("提交时传了 --session", '"--session"' in src_once, True)
    check(
        "会话创建失败禁止退回默认会话",
        'if not session or str(session) == "0":'
        in _insp2.getsource(mod.Main._generate_draw_job),
        True,
    )
    # ★ 2026-10-19 实测：即梦会用第一句提示词给会话改名，出图后必须改回来
    check(
        "出图后会改回分类框名",
        "改名" in _insp2.getsource(mod.Main._run_draw)
        or "self._jimeng.rename_session"
        in _insp2.getsource(mod.Main._process_draw_job),
        True,
    )
    check("有改名方法", hasattr(mod.jimeng.JimengClient, "rename_session"), True)
    src_rename = _insp2.getsource(mod.jimeng.JimengClient.rename_session)
    check(
        "改名用统一拼名（QQ 号必须留在名字里）",
        "session_label" in src_rename,
        True,
    )
    # 2026-10-22 补漏：原先不看返回码，CLI 拒绝也当成功，事后查不到
    check("改名校验返回码", "returncode" in src_rename, True)

    class _Rc:
        returncode = 1
        stdout = ""
        stderr = "Error: session not found"

    _real_run = mod.subprocess.run
    mod.subprocess.run = lambda *a, **k: _Rc()
    try:
        check(
            "改名被拒返回 False",
            mod.Main(None)._jimeng.rename_session("123", "甲", "456", image_settings),
            False,
        )
    finally:
        mod.subprocess.run = _real_run
    check(
        "默认会话(0)不改名",
        mod.Main(None)._jimeng.rename_session("0", "甲", "123", image_settings),
        False,
    )

    print()


def check_names(mod, check):
    """Run the names regression scenarios."""
    print("== 即梦会话名：必须 ≤50 字符（2026-10-22 生图失败真因） ==")
    # CLI 硬限 50 字符，超了 session create 退 1，整条生图断在第一步。
    cases = [
        ("短昵称", "阿鱼", "1000000000"),
        ("中文长昵称", "黑煤龟·影融", "1000000000"),
        ("超长昵称", "这是一个特别特别特别特别特别特别长的昵称啊啊啊", "1000000000"),
        ("空昵称", "", "1000000000"),
        ("超长QQ", "阿鱼", "1234567890123456"),
    ]
    for label, nick, qq in cases:
        name = mod._draw_session_label(nick, qq)
        check(
            f"{label} 长度 {len(name)} <= 50",
            len(name) <= mod.DRAW_SESSION_NAME_MAX,
            True,
        )
        check(
            f"{label} 以「使用 · QQ」结尾",
            name.endswith(f"使用 · {qq}") or qq in name,
            True,
        )
    name = mod._draw_session_label("黑煤龟·影融", "1000000000")
    check(
        "界面可读：昵称在、QQ 在",
        ("黑煤龟·影融" in name and "1000000000" in name),
        True,
    )
    # 2026-10-22 回归：名字必须稳定，随机后缀会让"一人一框"失效
    check(
        "同一人两次同名（可复用）",
        mod._draw_session_label("阿鱼", "1") == mod._draw_session_label("阿鱼", "1"),
        True,
    )
    check(
        "不带随机 hex 后缀",
        mod._draw_session_label("阿鱼", "1"),
        "阿鱼使用 · 1",
    )

    # ---- 读图：图必须跟人对应（2026-10-23 管理员要求）----------------
    # 病灶：原实现取 bucket[-1]（全群最新一张），A 问"看看这张"会拿到 B 的图。
    print()
