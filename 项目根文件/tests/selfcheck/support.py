"""Framework stubs, explicit event fixtures, and self-check result reporting."""

import importlib.util
import os
import sys
import tempfile
import types
from functools import wraps
from pathlib import Path

TEST_BOT_ID = "1000000004"


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
        def __init__(self, sender_id="", chain=None, id=""):
            self.sender_id = sender_id
            self.chain = chain or []
            self.id = id

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


def load_plugin(plugin):
    """Load the owned bridge with offline framework dependencies.

    Args:
        plugin: Absolute path to the maintained plugin entry point.

    Returns:
        The loaded plugin with its maintenance switch isolated for self-checks.
    """
    _install_stubs()
    spec = importlib.util.spec_from_file_location(
        "wb_bridge", plugin, submodule_search_locations=[str(plugin.parent)]
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    if os.environ.get("QQ_BOT_TEST_NO_PAUSE_STUB", "1") != "0":
        mod._is_paused = lambda: False
        sys.modules[mod.ProactiveChat.__module__]._is_paused = lambda: False
    # Keep every legacy fixture independent of the operator's real meme library.
    mod._selfcheck_meme_temp = tempfile.TemporaryDirectory(prefix="qqbot-selfcheck-")
    isolated_meme_root = Path(mod._selfcheck_meme_temp.name) / "empty-memes"
    original_init = mod.Main.__init__

    @wraps(original_init)
    def offline_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._memory = mod.memory_service.MemoryService(
            Path(mod._selfcheck_meme_temp.name) / self._instance_id / "memory.sqlite3",
            logger=mod.logger,
        )
        self._memes = mod.meme_library.MemeLibrary(isolated_meme_root)
        self._meme_collector = mod.meme_collection.Collector(isolated_meme_root)

    mod.Main.__init__ = offline_init
    return mod


class CheckResults:
    """Keep the established PASS labels, final result, and exit-code contract."""

    def __init__(self):
        self.ok = True
        self.count = 0

    def __call__(self, label, got, expect):
        good = got == expect
        self.ok = self.ok and good
        self.count += 1
        tail = "" if good else f"   期望 {expect!r}"
        print(f"[{'PASS' if good else 'FAIL'}] {label} -> {got!r}{tail}")


def make_event_type(mod):
    """Build a fresh event fixture bound to this offline plugin's components.

    Args:
        mod: Plugin whose message component classes are used by the fixture.

    Returns:
        An event class suitable for the production message entry point.
    """

    class _Ev:
        def __init__(self, self_id, comps, group_id="", message_id=""):
            self._bits = (self_id, comps, group_id)
            self.message_id = message_id
            self.unified_msg_origin = (
                f"aiocqhttp:GroupMessage:{group_id}"
                if group_id
                else "aiocqhttp:FriendMessage:1"
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

        def get_message_id(self):
            return self.message_id

        # 下面几个是驱动 on_message 全流程要用的。
        # get_message_str 刻意**不含 At** —— 跟框架一致：@ 不算文字，
        # 纯 @ 消息的 message_str 就是空的，这正是 2026-10-18 那次漏发的现场。
        def get_message_str(self):
            return "".join(c.text for c in self._bits[1] if isinstance(c, mod.Plain))

        def is_private_chat(self):
            return not self._bits[2]

        def stop_event(self):
            self.stopped = True

        def plain_result(self, text):
            return ("plain", text)

        def chain_result(self, chain):
            return ("chain", chain)

    return _Ev
