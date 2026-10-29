"""Schedule proactive replies and collect current conversation material."""

import asyncio
import time
import uuid
from pathlib import Path

from . import attachments, meme_library
from .attachments import describe as _describe_saved_attachments

try:
    import aiohttp
except ImportError:
    aiohttp = None

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from .bridge_policy import (
    FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC,
    FOLLOW_HIFREQ_MAX_CHAIN,
    FOLLOW_HIFREQ_WINDOW_SEC,
    FOLLOW_MIN_GAP_SEC,
    GROUP_GAG_RULES,
    IDLE_ALLOW_IMAGE,
    IDLE_CHAT_ALL_GROUPS,
    IDLE_CHAT_BLACKLIST,
    IDLE_CHAT_ENABLED,
    IDLE_CHAT_GROUPS,
    IDLE_CHECK_INTERVAL,
    IDLE_COOLDOWN_SEC,
    IDLE_GLOBAL_GAP_SEC,
    IDLE_HIFREQ_COOLDOWN_SEC,
    IDLE_HIFREQ_GROUPS,
    IDLE_HIFREQ_TRIGGER_SEC,
    IDLE_HISTORY_SIZE,
    IDLE_LIVE_HISTORY,
    IDLE_MIN_SOURCE_LINES,
    IDLE_MIN_TRIGGER_SEC,
    IDLE_REQUIRE_TEXT,
    IDLE_SOURCE_TTL,
    IDLE_TRIGGER_SEC,
    KEYWORD_BUBBLE_COOLDOWN_SEC,
    KEYWORD_BUBBLE_ENABLED,
    KEYWORD_BUBBLE_GLOBAL_GAP_SEC,
    KEYWORD_BUBBLE_MAX_PER_DAY,
    KEYWORD_BUBBLE_MIN_SOURCE_LINES,
    KEYWORD_BUBBLE_WORDS,
    SPARK_FAIL_CIRCUIT_N,
    TEMP_ROOT,
    MessageChain,
    _conv_id,
    _has_textual_material,
    _hit_words,
    _is_at_me,
    _is_empty_reply,
    _is_gateway_error,
    _is_paused,
    _is_reply_to_me,
    _sanitize,
)


class ProactiveChat:
    """Schedule proactive replies and collect current conversation material."""

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
            limit = self._cfg_int("follow_hifreq_max_chain", FOLLOW_HIFREQ_MAX_CHAIN)
            cool = self._cfg_int(
                "follow_hifreq_chain_cooldown_sec",
                FOLLOW_HIFREQ_CHAIN_COOLDOWN_SEC,
            )
            window = self._cfg_int("follow_hifreq_window_sec", FOLLOW_HIFREQ_WINDOW_SEC)
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
        st["message_version"] = int(st.get("message_version", 0)) + 1
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
            logger.warning(f"workbuddy_bridge: [闲聊] 现拉群历史失败 {group_id}: {exc}")
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
                    (
                        f"{n}张{label}"
                        if label == "图片" and n > 1
                        else (f"{n}个{label}" if n > 1 else label)
                    )
                    for label, n in media.items()
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

    async def _fetch_recent_image(self, url: str):
        """Fetch one recent image under the same limits as direct attachments.

        Args:
            url: Image address selected from the current group's recent messages.

        Returns:
            Request-local attachment paths and GIF descriptions, or an empty batch.
            Idle failures stay silent; cancellation still propagates.
        """
        empty = attachments.AttachmentBatch()
        if aiohttp is None or not url:
            return empty
        downloaded = None
        try:
            directory = TEMP_ROOT / "idle_pics"
            directory.mkdir(parents=True, exist_ok=True)
            suffix = Path(url.split("?")[0]).suffix
            if suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
                suffix = ".img"
            downloaded = directory / f".download-{uuid.uuid4().hex}{suffix}"

            async def materialize(_component):
                timeout = aiohttp.ClientTimeout(total=attachments.MATERIALIZE_TIMEOUT)
                async with aiohttp.ClientSession(
                    timeout=timeout, auto_decompress=True
                ) as session:
                    async with session.get(url) as response:
                        if response.status != 200:
                            return ""
                        advertised = response.headers.get("Content-Length", "")
                        if (
                            str(advertised).isdigit()
                            and int(advertised) > attachments.MAX_ATTACHMENT_BYTES
                        ):
                            raise attachments.AttachmentRejected(
                                "Image download exceeds attachment limit"
                            )
                        received = 0
                        signature = b""
                        with downloaded.open("xb") as stream:
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                if len(signature) < 12:
                                    signature += chunk[: 12 - len(signature)]
                                received += len(chunk)
                                if received > attachments.MAX_ATTACHMENT_BYTES:
                                    raise attachments.AttachmentRejected(
                                        "Decoded image download exceeds attachment limit"
                                    )
                                stream.write(chunk)
                        image_header = (
                            signature.startswith(b"\x89PNG\r\n\x1a\n")
                            or signature.startswith(b"\xff\xd8\xff")
                            or signature.startswith((b"GIF87a", b"GIF89a", b"BM"))
                            or (signature[:4] == b"RIFF" and signature[8:12] == b"WEBP")
                        )
                        return str(downloaded) if received and image_header else ""

            batch = await attachments.save_attachments(
                [url],
                out_dir=directory,
                sender_name="群友",
                sender_id="",
                context_component=None,
                context_meta={},
                max_files=1,
                materialize=materialize,
                logger=logger,
            )
            # The caller already labels the historical author from the same message.
            batch.notes.clear()
            batch.notices.clear()
            return batch
        except Exception:  # noqa: BLE001
            logger.warning("workbuddy_bridge: recent image download failed")
            return empty
        finally:
            if downloaded is not None:
                try:
                    downloaded.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "workbuddy_bridge: recent image temporary cleanup failed"
                    )

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
        logger.info(f"workbuddy_bridge: [冒泡] {group_id} 命中「{word}」，安排接话")
        # create_task 而不是直接 await：这条消息不该因为我要想一句话
        # 而被拖住。异常在协程内兜住，别冒泡到框架事件循环。
        self._spawn_background(self._keyword_spark(group_id, umo, word))
        return True

    async def _keyword_spark(self, group_id: str, umo: str, word: str) -> None:
        """关键词触发的一次主动接话。素材口径与冷场闲聊完全一致。"""
        created_at = time.monotonic()
        source_version = int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        )
        if self._questions.busy:
            return
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
                ctx = f"（你上一轮在这群说过：「{mine}」，别重复那套说法）\n"
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
            sent = await self._speak_spark(
                group_id,
                umo,
                prompt,
                "冒泡",
                source_version=source_version,
                created_at=created_at,
                meme_context="\n".join(
                    str(item.get("text") or "") for item in hist[-5:]
                ),
            )
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
                follow = False  # 连击到上限：这波话题不再跟
            if not follow and not self._idle_enabled_for(group_id):
                continue
            if quiet_for < trigger_sec and not follow:
                continue
            # 冷场开口要隔开免得刷屏；但**跟随模式**下群友正在接话，
            # 这时再等冷却就成哑巴了，所以放行。
            if not follow and now - float(st.get("last_spark_ts") or 0) < cooldown_sec:
                continue
            st["last_spark_ts"] = now
            st["last_ts"] = now  # 防下一轮重复触发
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
        ttl = self._cfg_int("idle_source_ttl", IDLE_SOURCE_TTL)
        hist = [h for h in hist if now - float(h.get("ts") or 0) <= ttl]
        if not hist:
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
            f"- {h.get('name') or h.get('sender')}: {h.get('text')}" for h in hist
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
                    # 标注是谁发的：主动接话时更容易把图和人对上
                    who = str(h.get("name") or h.get("sender") or "群友").strip()
                    lines = (
                        f"{lines}\n\n【群里最新的图片是 {who} 发的，已存到本地，"
                        "请先读取再回答；回复里别出现 QQ 号】\n"
                        + _describe_saved_attachments(local)
                    )
                break  # 只取最新一张，够用也不浪费
        return hist, lines

    async def _speak_spark(
        self,
        group_id: str,
        umo: str,
        prompt: str,
        tag: str,
        follow: bool = False,
        *,
        source_version: int | None = None,
        created_at: float | None = None,
        meme_context: str = "",
    ) -> tuple[bool, str] | None:
        """Generate an optional group reply and count only gateway failures.

        Args:
            group_id: Source group identifier.
            umo: Validated destination session.
            prompt: Material and instructions for this reply.
            tag: Label used for diagnostic messages.
            follow: Whether this reply continues an active topic.
            source_version: Group message revision captured before collecting context.
            created_at: Monotonic time at which context collection started.
            meme_context: Recent messages from this group, excluding prompt templates.

        Returns:
            The send result and reply, or None when staying silent.
        """
        if getattr(self, "_stopped", False) or _is_paused():
            return None
        created_at = time.monotonic() if created_at is None else created_at
        version = int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        )
        source_version = version if source_version is None else source_version
        not_after = created_at + max(
            5, min(180, self._cfg_int("proactive_reply_ttl_sec", 30))
        )
        if (
            version != source_version
            or time.monotonic() >= not_after
            or self._questions.busy
        ):
            logger.info(
                "workbuddy_bridge: [question] proactive skipped before submission"
            )
            return None
        memory = getattr(self, "_memory", None)
        if memory is not None and self._cfg_bool("memory_enabled", True):
            memory_context = await memory.group_context(
                self, group_id, umo, meme_context
            )
            if memory_context:
                prompt = memory_context + prompt

        memes = getattr(self, "_memes", None)
        meme_offer = None
        meme_cooldown = max(60, min(3600, self._cfg_int("meme_cooldown_sec", 120)))
        if (
            memes is not None
            and self._cfg_bool("meme_enabled", True)
            and not self._blacklisted(group_id)
        ):
            meme_offer = await asyncio.to_thread(
                memes.offer,
                umo,
                context=meme_context[-800:],
                cooldown_sec=meme_cooldown,
            )
            if meme_offer is not None:
                prompt += meme_offer.prompt
        try:
            conv_id = _conv_id(umo, 0)
            if getattr(self, "_stopped", False) or _is_paused():
                return None
            reply = await self._ask(
                prompt, conv_id, master=False, background=True, not_after=not_after
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"workbuddy_bridge: [{tag}] generation failed: {exc}")
            return

        raw_meme_reply = reply
        reply, meme_token = meme_library.strip_selection(reply, meme_offer)
        meme_only = bool(meme_token and not reply.strip())

        if _is_empty_reply(reply or "") and not meme_only:
            if any(
                m in (reply or "")
                for m in (
                    "[排队]",
                    "[忙碌跳过]",
                    "[过期跳过]",
                    "[排队超时]",
                    "[队列已满]",
                    "[维护暂停]",
                )
            ):
                logger.info(
                    f"workbuddy_bridge: [{tag}] {group_id} queued; "
                    "circuit failure count unchanged"
                )
                return
            failure_markers = (
                "[凭据失效]",
                "[网关不可达]",
                "[超时]",
                "[网关错误]",
                "[连接失败]",
                "[环境错误]",
                "[未配置]",
                "[内部错误]",
                "[回复校验失败]",
                "[状态待确认]",
            )
            failed = _is_gateway_error(reply or "") or any(
                marker in (reply or "") for marker in failure_markers
            )
            if not failed:
                self._spark_fail_streak = 0
                logger.info(
                    f"workbuddy_bridge: [{tag}] {group_id} no reply needed; "
                    "circuit failure count reset"
                )
                return
            self._spark_fail_streak = getattr(self, "_spark_fail_streak", 0) + 1
            logger.info(
                f"workbuddy_bridge: [{tag}] {group_id} gateway failure; "
                f"consecutive failures={self._spark_fail_streak}"
            )
            if self._spark_fail_streak == SPARK_FAIL_CIRCUIT_N:
                logger.error(
                    "workbuddy_bridge: proactive replies paused after "
                    f"{SPARK_FAIL_CIRCUIT_N} gateway failures; run reconnect.py"
                )
            return

        self._spark_fail_streak = 0

        reply = _sanitize(reply or "")
        if not reply.strip() and not meme_only:
            logger.info(f"workbuddy_bridge: [{tag}] {group_id} empty after filtering")
            return

        current_version = int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        )
        if (
            current_version != source_version
            or time.monotonic() >= not_after
            or self._questions.busy
        ):
            logger.info(
                "workbuddy_bridge: [question] proactive reply expired before delivery"
            )
            return None
        image_path = None
        if not self._can_send_active():
            return None
        if memes is not None and self._cfg_bool("meme_enabled", True):
            if meme_offer is not None:
                try:
                    await asyncio.to_thread(
                        memes.learn, umo, meme_offer, raw_meme_reply
                    )
                except Exception as exc:  # noqa: BLE001 - optional names never block chat.
                    logger.warning(
                        "workbuddy_bridge: meme naming skipped: %s", type(exc).__name__
                    )
            # Naming involves local IO; recheck the original delivery deadline.
            if (
                not self._can_send_active()
                or time.monotonic() >= not_after
                or self._questions.busy
                or int(
                    (self._group_activity.get(group_id) or {}).get("message_version", 0)
                )
                != source_version
            ):
                return None
            image_path = memes.take(
                umo, meme_offer, meme_token, cooldown_sec=meme_cooldown
            )
        if image_path:
            try:
                chain = MessageChain(
                    chain=([Plain(reply)] if reply.strip() else [])
                    + [Image(file=image_path)]
                )
            except Exception as exc:  # noqa: BLE001 - no transport has started yet.
                logger.warning(
                    "workbuddy_bridge: meme result unavailable: %s", type(exc).__name__
                )
                if meme_only:
                    return None
                ok = await self._send_to_session(umo, reply)
            else:
                ok = await self._send_chain_to_session(umo, chain)
        elif meme_only:
            return None
        else:
            ok = await self._send_to_session(umo, reply)
        if ok and (reply.strip() or image_path):
            bucket = self._spark_said.setdefault(group_id, [])
            bucket.append(
                {
                    "text": reply[:200] if reply.strip() else "（发了一张表情包）",
                    "ts": time.time(),
                }
            )
            if len(bucket) > 3:
                del bucket[:-3]
        return ok, reply

    async def _spark_chat(self, group_id: str, umo: str, follow: bool = False) -> None:
        """顺着刚才的聊天记录，主动往群里接一句。

        follow=True 表示这是"跟随上一轮"——群友正在接机器人刚说的话，
        这时不受冷却限制，尽快接上。
        """
        created_at = time.monotonic()
        source_version = int(
            (self._group_activity.get(group_id) or {}).get("message_version", 0)
        )
        if self._questions.busy:
            return
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
        min_lines = (
            1
            if follow
            else self._cfg_int("idle_min_source_lines", IDLE_MIN_SOURCE_LINES)
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
                    float(h.get("ts") or 0) > float(last.get("ts") or 0) for h in hist
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
        sent = await self._speak_spark(
            group_id,
            umo,
            prompt,
            tag,
            follow=follow,
            source_version=source_version,
            created_at=created_at,
            meme_context="\n".join(str(item.get("text") or "") for item in hist[-5:]),
        )
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
