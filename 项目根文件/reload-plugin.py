#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""重新加载 AstrBot 插件 —— 不走面板登录，直接调 Dashboard 的重载接口。

## 为什么需要这个脚本

`workbuddy_bridge` 自带热重载（每 2s 巡检 main.py，改了自动 reload）。
但那条链路有个**致命死角**：

    插件一旦加载失败（比如编辑器改出个 IndentationError），
    AstrBot 会把本插件卸载 → **热重载 watcher 也一起死掉**（它就住在这个插件里）
    → 此后文件改好了也没人管，插件永远醒不过来
    → @ 消息落到 AstrBot 默认流程 → provider 为空 → 群里报"未找到可用的对话模型"

2026-10-09 就这么翻了一次车。这个脚本就是那条逃生通道。

## 原理

Dashboard 的重载接口 `POST /api/v1/plugins/{id}/reload` 只要求一个 HS256 JWT，
而**签名密钥 `jwt_secret` 就明文躺在 `AstrBot/data/cmd_config.json` 里**。
所以我们不需要知道面板密码 —— 自己签一个 token 就能调。

服务端校验（`dashboard/server.py:295`）只看三件事：
    1. HS256 签名对
    2. 没过期
    3. payload["username"] 非空

## 为什么走 API 热重载而不是重启 AstrBot

重启 AstrBot 会打断 NapCat 与 QQ 的连接、丢掉运行中的会话上下文，
代价太大；走 Dashboard 的 reload 接口只换插件这一层，连接和上下文
都保得住 —— 只把死掉的插件拉起来，不动整栋楼。

## 用法

    双击 reload-plugin.bat                  # 日常用法（等同下面的默认重载）
    python reload-plugin.py                 # 重载 workbuddy_bridge（默认）
    python reload-plugin.py 某个插件目录名    # 重载指定插件

退出码：0 成功 / 1 失败。
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request

# ---- Windows 编码自防御（2026-10-16 修复） ------------------------------
# 血泪教训，不是装饰：本脚本会被 .bat 双击、被 reconnect.py 的 subprocess
# 拉起、被 AI 会话或 `> 文件` 重定向 —— 这些姿势下用户环境没有
# PYTHONUTF8 / PYTHONIOENCODING，stdout 编码回落系统 locale
# （中文 Windows = GBK/CP936）。而版本核对输出里有 ✔/✓/⚠/✗，
# GBK 编码不了 → UnicodeEncodeError 崩在"重载已经成功"之后，
# 害得调用方（reconnect.py）误报"重载调用失败（AstrBot 可能没在跑）"。
# 强制 UTF-8 + errors="replace"；reconnect.py 那侧按 utf-8 读，两边对齐。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录，所有路径的锚点

# 面板端口。与 start-astrbot.bat 里打印的一致；可用环境变量覆盖（调试用）。
DASHBOARD_PORT = os.environ.get("ASTRBOT_DASHBOARD_PORT", "6185")
API_BASE = f"http://127.0.0.1:{DASHBOARD_PORT}/api/v1"

# 默认要重载的插件（目录名）
DEFAULT_PLUGIN = "workbuddy_bridge"

# cmd_config.json 可能带 BOM（下面用 utf-8-sig 读来兼容）
CONFIG_PATH = os.path.join(HERE, "AstrBot", "data", "cmd_config.json")


def _die(msg: str, code: int = 1) -> "None":
    """打印错误并退出。致命前提不满足时用它快速失败，不拖着跑。"""
    print(f"[ERROR] {msg}")
    sys.exit(code)


def _load_jwt_secret(path: str) -> tuple[str, str]:
    """从 cmd_config.json 读出 (用户名, jwt_secret)。"""
    if not os.path.exists(path):
        _die(f"找不到配置文件：{path}")
    try:
        # utf-8-sig：兼容带 BOM 的 cmd_config.json（有的编辑器保存时会加）
        with open(path, encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception as e:  # noqa: BLE001
        _die(f"解析配置文件失败：{e}")

    dash = cfg.get("dashboard") or {}
    username = str(dash.get("username") or "").strip()
    secret = str(dash.get("jwt_secret") or "").strip()
    if not username:
        _die("配置里没有 dashboard.username")
    if not secret:
        _die("配置里没有 dashboard.jwt_secret")
    return username, secret


def _sign_token(username: str, secret: str) -> str:
    """用 jwt_secret 自签一个 7 天有效的 HS256 token。"""
    try:
        import jwt  # PyJWT
    except ImportError:
        _die(
            "缺少 PyJWT。请用项目自带的 venv 运行：\n"
            r"    .\.venv\Scripts\python.exe reload-plugin.py"
        )
    # payload 只需要 username + exp：服务端只校验签名/过期/username 非空，
    # 不查密码 —— 这就是能"自签"的根本原因
    payload = {
        "username": username,
        # 跟 AstrBot 源码 auth_service.generate_jwt 保持一致
        "exp": datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(days=7),
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    # PyJWT 2.x 返回 str；1.x 返回 bytes，兜一下
    return token.decode("utf-8") if isinstance(token, bytes) else token


def _post(path: str, token: str, timeout: int = 60) -> tuple[int, str]:
    """发一个带 Bearer token 的 POST，返回 (状态码, 响应体文本)。"""
    # reload 接口无请求体，但必须显式给空 body 才是合法的 POST
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    # 别让系统代理插一脚（本机回环不需要代理）
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # HTTP 层错误（401/404/500）不算致命：把状态码和响应体交回主流程判断
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        _die(
            f"连不上 AstrBot 面板（{API_BASE}）：{e.reason}\n"
            "        先确认 AstrBot 正在运行（6185 端口有监听）。"
        )


def _probe_code_version(plugin: str) -> None:
    """重载后探一次"到底加载的是哪一版代码"。

    ## 为什么需要这一步（2026-10-09 踩坑）
    AstrBot v4 的 `PluginManager.reload()` **不清 `sys.modules`**，
    而 `load()` 内部是 `__import__()` —— 命中缓存就返回**旧模块对象**，
    磁盘上的新代码根本没被读。日志照样打 `Loading plugin ...` 和
    `[热重载] 成功，新代码已生效`，看起来一切正常，实则跑的是旧代码。

    表现：群里回复的 `**` 怎么改都洗不掉。

    ## 探测法
    插件在若干关键位置打了带行号的日志（logger 会带上 `main:NNNN`）。
    拿日志里**最近一次**该锚点的行号，和磁盘上真实行号比：
      相等  → 加载的是当前磁盘版本
      不等  → 加载的是旧版本（差值就是"少了几行新代码"）

    这个方法不依赖插件自身是否已修复，纯外部观测，最可靠。
    """
    log_path = os.path.join(HERE, "AstrBot", "data", "logs", "astrbot.log")
    src_path = os.path.join(HERE, "AstrBot", "data", "plugins", plugin, "main.py")
    if not os.path.exists(log_path) or not os.path.exists(src_path):
        return

    # 磁盘上的真实行号。
    # 注意：logger 报的是**调用起始行**，而关键词往往在多行调用的字符串行上
    # （如 `logger.info(` 在 N，`f"…回复长度上限…"` 在 N+1）。
    # 所以要往上回退到最近的 `logger.info(`，否则会稳定差 1 行造成误报。
    try:
        with open(src_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    real = {}
    for i, line in enumerate(lines, 1):
        if "[回复] 机器人=" not in line and "回复长度上限" not in line:
            continue
        call_line = i
        for j in range(i - 1, max(0, i - 6), -1):
            if "logger.info(" in lines[j - 1] or "logger.warning(" in lines[j - 1]:
                call_line = j
                break
        if "[回复] 机器人=" in line:
            real["reply"] = call_line
        else:
            real["limit"] = call_line

    # 日志里最近的行号（logger 格式：[workbuddy_bridge.main:NNNN]）
    tail = ""
    try:
        size = os.path.getsize(log_path)
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(max(0, size - 300_000))
            tail = f.read()
    except OSError:
        return

    import re as _re

    def last_line_no(anchor: str) -> int | None:
        found = None
        for m in _re.finditer(
            r"\[workbuddy_bridge\.main:(\d+)\]:[^\n]*" + _re.escape(anchor),
            tail,
        ):
            found = int(m.group(1))
        return found

    checks = [
        ("[回复]", "reply"),
        ("回复长度上限", "limit"),
    ]
    verdicts = []
    for anchor, key in checks:
        if key not in real:
            continue
        got = last_line_no(anchor)
        if got is None:
            continue
        verdicts.append((anchor, got, real[key]))

    if not verdicts:
        print("     （日志里还没有可用的行号锚点，发一条群消息后再跑本脚本即可核对）")
        return

    # 全部锚点的日志行号都等于磁盘行号，才算"加载的是最新代码"
    all_ok = all(g == r for _, g, r in verdicts)
    print()
    if all_ok:
        print("     [版本核对] 加载的正是磁盘上的最新代码 ✔")
        for anchor, got, r in verdicts:
            print(f"       {anchor} 日志={got} 磁盘={r} ✓")
    else:
        print("     [版本核对] ⚠ 加载的是旧代码！日志行号与磁盘对不上：")
        for anchor, got, r in verdicts:
            flag = "✓" if got == r else f"✗ 差 {r - got} 行"
            print(f"       {anchor} 日志={got} 磁盘={r} {flag}")
        print("     说明框架的 reload() 命中了 sys.modules 缓存（见脚本头部注释）。")
        print("     对策：重启一次 AstrBot，让解释器全新加载；此后热重载自带缓存清理。")


def main() -> int:
    """主流程：读 jwt_secret → 自签 token → 调 reload 接口 → 版本核对探针。

    最后一步 _probe_code_version 很关键：框架的 reload() 可能命中
    sys.modules 缓存，"接口返回成功" ≠ "跑的是新代码"，必须用日志行号验收。
    """
    plugin = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLUGIN).strip()
    if not plugin:
        _die("插件名不能为空")

    username, secret = _load_jwt_secret(CONFIG_PATH)
    token = _sign_token(username, secret)

    print(f"[1/2] 已用本地配置自签令牌（用户 {username}）")
    print(f"[2/2] 请求重载插件：{plugin} ...")

    # 关键一步：走 API 热重载（而非重启 AstrBot），保住 NapCat 连接与上下文
    status, body = _post(f"/plugins/{plugin}/reload", token)

    # 服务端约定：{"status": "ok"|"error", "message": ..., "data": ...}
    try:
        parsed = json.loads(body)
    except Exception:  # noqa: BLE001
        parsed = None

    if parsed is None:
        print(f"[?] HTTP {status}，响应不是 JSON：\n{body[:500]}")
        return 1

    ok = parsed.get("status") == "ok"
    msg = parsed.get("message") or ""
    if ok:
        print(f"[OK] {msg or '重载成功'}")
        print()
        print("     日志末尾应有这几行：")
        print("       Loading plugin " + plugin + " ...")
        print("       热重载已开启（每 2s 巡检，改完 main.py 保存即自动生效）")
        _probe_code_version(plugin)
        return 0

    print(f"[FAIL] HTTP {status}：{msg}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
