"""QQbot 一条龙体检（doctor）。

把散落的检查合成一个入口：
  1. 插件代码语法
  2. 插件逻辑自检（转发给 test-workbuddy-bridge.py）
  3. **插件是否真的加载进了 AstrBot**（2026-10-09 加）
  4. 网关凭据（读 workbuddy-gateway.env，严格判据 authenticated == true）
  5. 端口监听（AstrBot 面板 / 反向 WS / NapCat WebUI / 网关）
  6. 关键进程

适用场景：机器人突然不回消息、改过插件代码、WorkBuddy 重启过之后，
跑一遍就能定位断在哪一环；平时也可当日常巡检用。

用法：
    双击 doctor.bat（日常巡检 / 故障定位都用它）
或：
    .venv\\Scripts\\python.exe doctor.py

注意：本脚本**不能**替你刷新网关凭据 —— 新端口/新密码只注入给 WorkBuddy
派生的进程，外部脚本拿不到。凭据过期时按提示回 WorkBuddy 会话里让 AI 刷新。
"""

import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

# ---- Windows 编码自防御（2026-10-16 修复） ------------------------------
# 血泪教训，不是装饰：本脚本会被 .bat 双击、被管道/重定向调用，
# 而用户环境没有 PYTHONUTF8 / PYTHONIOENCODING。这些姿势下 stdout 编码
# 回落到系统 locale（中文 Windows = GBK/CP936），print 一个 emoji/✔✓⚠✗
# 就会 UnicodeEncodeError 崩在半路上（与自检子进程同源的坑）。
# 强制 UTF-8 + errors="replace"，保证任何调用姿势下都能完整跑完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

HERE = Path(__file__).resolve().parent  # 脚本所在目录，所有路径都以它为锚
ENV_FILE = HERE / "workbuddy-gateway.env"  # 网关凭据（端口+密码），reconnect.py 会改写它
PLUGIN = HERE / "AstrBot" / "data" / "plugins" / "workbuddy_bridge" / "main.py"  # 被体检的插件入口
BRIDGE_TEST = HERE / "test-workbuddy-bridge.py"  # 插件逻辑自检脚本，由本脚本拉起子进程执行
CMD_CONFIG = HERE / "AstrBot" / "data" / "cmd_config.json"  # AstrBot 配置（含面板账号与 jwt_secret）

DASHBOARD_PORT = "6185"
PLUGIN_NAME = "workbuddy_bridge"

# 本机回环必须绕过系统代理，否则 127.0.0.1 的请求会被代理截走返回 502
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# 三个固定端口：面板 / 插件与 AstrBot 的反向 WS / NapCat 的 WebUI。
# 注意网关端口不在此列 —— 它跟着会话进程走、经常变，由 check_gateway 单独验。
PORTS = {"6185": "AstrBot 面板", "6199": "AstrBot 反向 WS", "6099": "NapCat WebUI"}

# 体检结果台账：(项目名, 是否通过, 备注)，最后由 main() 汇总出结论
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    """登记一项体检结果并立即打印。

    所有 check_* 都通过它汇报，保证输出格式统一、末尾能汇总失败清单。
    """
    results.append((name, ok, note))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {note}" if note else ""))


def check_plugin_loaded() -> None:
    """插件是否真的活着（而不仅仅是磁盘上的文件语法正确）。

    2026-10-09 翻车教训：main.py 因为一个缩进错误导致加载失败，
    AstrBot 把插件连同它的热重载 watcher 一起卸载了。之后文件改好了，
    但没有任何机制再把它拉起来，@ 消息全落到 AstrBot 默认流程，
    群里一直报"未找到任何可用的对话模型"。

    纯语法检查（check_compile）发现不了这种情况 —— 文件是好的，
    坏的是"AstrBot 内存里的状态"。

    ## 为什么读日志而不是问面板

    面板的 `/plugins` 返回的 `message` 是**首次加载失败时缓存下来的**，
    即使后来重载成功了，那段老报错文案也不会刷新（实测过，会误报）。
    日志才是真相 —— 每次加载 / 卸载都会留一行，按顺序读最后一个状态即可。

    判据：最后一条 `Loading plugin <name>` 之后，是否紧跟
    `Failed to import plugin <name>`。有 → 死了；没有 → 活着。
    """
    log_path = HERE / "AstrBot" / "data" / "logs" / "astrbot.log"
    if not log_path.exists():
        record("插件加载状态", False, "找不到 astrbot.log")
        return

    try:
        # 日志可能很大，只读尾部一段就够 —— 状态只看最近一次
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read()
    except Exception as exc:  # noqa: BLE001
        record("插件加载状态", False, f"读日志失败：{exc.__class__.__name__}")
        return

    mark_load = f"Loading plugin {PLUGIN_NAME} ..."
    mark_fail = f"Failed to import plugin {PLUGIN_NAME}"

    # rfind = 只看"最后一次"加载；更早的历史状态不代表现在
    idx_load = tail.rfind(mark_load)
    if idx_load < 0:
        record("插件加载状态", False, "日志里没有加载记录 —— AstrBot 可能没在跑")
        return

    # 最后一次"加载"之后若还出现过"导入失败"，说明当前处于死的状态
    idx_fail = tail.rfind(mark_fail)
    if idx_fail > idx_load:
        # 取失败原因（冒号后面的部分）
        line = tail[idx_fail:].splitlines()[0]
        reason = line.split(":", 1)[-1].strip()
        record(
            "插件加载状态", False,
            f"{reason} —— 双击 reload-plugin.bat 重载，或先修语法",
        )
        return

    record("插件加载状态", True, "已加载，无报错")


def check_code_version() -> None:
    """内存里跑的代码，是不是磁盘上的最新版？

    2026-10-09 第二课：插件"加载成功"不等于"跑的是新代码"。

    AstrBot v4 的 `PluginManager.reload()` 只做 terminate / unbind / load，
    **不清 `sys.modules`**；而 `load()` 内部是 `__import__()` ——
    Python 在 `sys.modules` 里命中同名模块就直接返回**旧对象**，
    根本不会重新读磁盘上的 .py。

    于是出现一种极隐蔽的故障：
        改完 main.py → 日志照打 `Loading plugin ...` + `[热重载] 成功`
        → 但插件跑的还是旧代码 → 新功能一点没生效
    本次实测就是 `_strip_markdown` 写了却洗不掉群里的 `**`。

    ## 判据：拿日志行号与磁盘行号对表
    插件在关键位置打的日志自带行号（logger 格式 `main:NNNN`）。
    取日志里最近一次的该行号，跟磁盘上的真实行号比：
        相等 → 跑的是当前磁盘版本
        不等 → 跑的是旧版本（差值 ≈ 少了多少行新代码）
    纯外部观测，不依赖插件自身是否已修好。
    """
    import re

    src_path = HERE / "AstrBot" / "data" / "plugins" / PLUGIN_NAME / "main.py"
    log_path = HERE / "AstrBot" / "data" / "logs" / "astrbot.log"
    if not src_path.exists():
        record("代码版本核对", False, f"找不到 {src_path}")
        return

    try:
        lines = src_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        record("代码版本核对", False, f"读源文件失败：{exc.__class__.__name__}")
        return

    real: dict[str, int] = {}
    for i, line in enumerate(lines, 1):
        if "[回复] 机器人=" not in line and "回复长度上限" not in line:
            continue
        # logger 记的是**调用起始行**；关键词通常在多行调用的字符串行上
        # （`logger.info(` 在 N，`f"…关键词…"` 在 N+1）。往上回退到
        # 最近的 logger 调用行，否则会稳定差 1 行、造成误报。
        call_line = i
        for j in range(i - 1, max(0, i - 6), -1):
            src_line = lines[j - 1]
            if "logger.info(" in src_line or "logger.warning(" in src_line:
                call_line = j
                break
        if "[回复] 机器人=" in line:
            real["reply"] = call_line
        else:
            real["limit"] = call_line

    if not log_path.exists():
        record("代码版本核对", False, "找不到 astrbot.log")
        return

    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 300_000))
            tail = f.read()
    except OSError as exc:
        record("代码版本核对", False, f"读日志失败：{exc.__class__.__name__}")
        return

    def last_log_line_no(anchor: str) -> int | None:
        """在日志尾部找锚点最后一次出现时的源码行号（logger 自带 main:NNNN）。"""
        got = None
        for m in re.finditer(
            r"\[workbuddy_bridge\.main:(\d+)\]:[^\n]*" + re.escape(anchor),
            tail,
        ):
            got = int(m.group(1))
        return got

    pairs: list[tuple[str, int, int]] = []
    for anchor, key in (("[回复]", "reply"), ("回复长度上限", "limit")):
        if key not in real:
            continue
        got = last_log_line_no(anchor)
        if got is None:
            continue
        pairs.append((anchor, got, real[key]))

    if not pairs:
        record(
            "代码版本核对", True,
            "日志里暂无行号锚点（发一条群消息后即可核对）",
        )
        return

    # 日志行号 ≠ 磁盘行号 → 内存里跑的是旧代码（sys.modules 缓存坑，见 docstring）
    bad = [p for p in pairs if p[1] != p[2]]
    if bad:
        detail = "；".join(
            f"{a} 日志={g} 磁盘={r}（差 {r - g} 行）" for a, g, r in bad
        )
        record(
            "代码版本核对", False,
            f"跑的是旧代码：{detail} —— 重启一次 AstrBot 让解释器全新加载",
        )
        return

    record("代码版本核对", True, "内存代码与磁盘一致")


def load_env(path: Path) -> dict:
    """把 KEY=VALUE 凭据文件读成 dict；容忍空行、注释与首尾空白。"""
    cfg = {}
    if not path.exists():
        return cfg
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        cfg[k.strip()] = v.strip()
    return cfg


def check_compile() -> None:
    """插件语法检查：子进程跑 py_compile，不 import —— 只验语法，不碰运行态。"""
    proc = subprocess.run(
        [sys.executable, "-m", "py_compile", str(PLUGIN)],
        capture_output=True, text=True,
    )
    record("插件语法检查", proc.returncode == 0, (proc.stderr or "").strip()[:80])


def check_bridge_selftest() -> None:
    """插件逻辑自检：把 test-workbuddy-bridge.py 拉成子进程跑，认 stdout 里的 ALL PASS。

    用子进程而不是 import，是为了故障隔离 —— 自检脚本崩了只影响这一项，
    不会把 doctor 本体一起带走。
    """
    # 子进程显式带上 UTF-8 环境（双保险）：自检脚本自己也做了 reconfigure，
    # 但环境变量能兜住"将来它又被改回依赖默认编码"的情况。
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(BRIDGE_TEST)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    stdout = proc.stdout or ""
    ok = "ALL PASS" in stdout and proc.returncode == 0
    tail = stdout.strip().splitlines()
    verdict = tail[-1] if tail else ""
    if not ok:
        if proc.returncode != 0:
            # 崩溃退出：把 stderr 最后一行亮出来（通常是 traceback 的异常行），
            # 比给用户看一行截断的乱码有用地多（2026-10-16 教训）。
            err_lines = (proc.stderr or "").strip().splitlines()
            err_line = err_lines[-1] if err_lines else ""
            verdict = f"子进程崩溃退出（code={proc.returncode}）：{err_line or '无 stderr 输出'}"
        else:
            fails = [l for l in tail if l.startswith("[FAIL]")]
            if fails:
                verdict = fails[0]
    record("插件逻辑自检", ok, verdict)


def check_gateway() -> None:
    """网关凭据体检：env 里记的 (端口, 密码) 现在还能不能通过认证。

    ## 网关凭据机制（为什么这项最容易挂）
    - **端口跟着会话进程走、频繁变**：WorkBuddy 重开或会话进程轮换后，
      旧端口就不再监听，env 里记的值即刻作废。
    - **密码在同一 WorkBuddy 生命周期内通常不变** —— 所以 reconnect.py
      能拿旧密码扫到新端口自愈。
    - **验收判据必须认 authenticated == true**：旧密码打在新网关上也可能
      返回 HTTP 200，但 body 里 authenticated 是 false。只看状态码会误判通过。
    """
    cfg = load_env(ENV_FILE)
    port = (cfg.get("CODEBUDDY_GATEWAY_PORT") or "").strip()
    token = (cfg.get("CODEBUDDY_GATEWAY_PASSWORD") or "").strip()
    if not port or not token:
        record("网关凭据", False, "env 缺失端口或密码，回 WorkBuddy 会话让 AI 刷新")
        return
    url = f"http://127.0.0.1:{port}/api/v1/auth/status"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with OPENER.open(req, timeout=6) as resp:
            body = resp.read().decode("utf-8", "replace")
        # 严格判据：必须是 JSON 里的 authenticated == true（200+false 是陷阱）
        authed = json.loads(body).get("authenticated") is True
        if authed:
            record("网关凭据", True, f"端口 {port}，认证通过")
        else:
            record("网关凭据", False, f"端口 {port} 可连但拒绝认证 —— 回 WorkBuddy 会话让 AI 刷新")
    except Exception as exc:  # noqa: BLE001
        record("网关凭据", False, f"连不上 127.0.0.1:{port}（{exc.__class__.__name__}）—— WorkBuddy 可能重启过，让 AI 刷新")


def listening_ports() -> set[str]:
    """当前处于 LISTENING 的端口集合（字符串形式，与 PORTS 的键对齐）。

    netstat 不可用时返回空集 —— 宁可报"未监听"也不让体检中断。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            timeout=20, errors="replace",
        ).stdout
    except Exception:  # noqa: BLE001
        return set()
    ports = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        m = re.search(r":(\d+)\s", line)
        if m:
            ports.add(m.group(1))
    return ports


def check_ports() -> None:
    """三个固定端口是否在监听：面板 / 反向 WS / NapCat WebUI（网关端口见 check_gateway）。"""
    live = listening_ports()
    for port, label in PORTS.items():
        record(f"端口 {port}（{label}）", port in live, "监听中" if port in live else "未监听")


def check_processes() -> None:
    """关键进程清点：QQ（NapCat 宿主）必须在跑；Python 进程数作参考（AstrBot/插件都是 python）。"""
    try:
        out = subprocess.run(
            ["tasklist"], capture_output=True, text=True, timeout=20, errors="replace",
        ).stdout.lower()
    except Exception:  # noqa: BLE001
        record("关键进程", False, "tasklist 不可用")
        return
    record("QQ 进程", "qq.exe" in out, "在跑" if "qq.exe" in out else "未运行（需双击 start-napcat.bat）")
    record("Python 进程", out.count("python.exe") >= 1, f"{out.count('python.exe')} 个")


def main() -> int:
    """按依赖顺序跑完全部检查并汇总结论。

    顺序有讲究：先验"文件本身没问题"（语法/逻辑自检），再验"运行态"
    （加载状态/版本/网关/端口/进程）—— 前面挂掉往往就是后面的根因。
    退出码：0 全部通过 / 1 有未通过项（供 .bat 或调用方判断）。
    """
    print("=" * 62)
    print(" QQbot 一条龙体检")
    print("=" * 62)
    check_compile()
    check_bridge_selftest()
    check_plugin_loaded()
    check_code_version()
    check_gateway()
    check_ports()
    check_processes()

    print("-" * 62)
    # 汇总失败清单，并针对常见失败给出下一步指引
    failed = [name for name, ok, _ in results if not ok]
    if not failed:
        print("结论：全部通过，启动顺序：start-astrbot.bat -> start-napcat.bat -> 扫码")
        return 0
    print(f"结论：{len(failed)} 项未通过 —— {'；'.join(failed)}")
    if "插件加载状态" in failed:
        print("提示：插件没加载上时，双击 reload-plugin.bat 可以就地重载，不必重启 AstrBot。")
    print("提示：网关凭据类问题必须回 WorkBuddy 会话里让 AI 刷新（外部脚本拿不到新密码）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
