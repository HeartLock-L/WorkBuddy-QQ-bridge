"""QQbot 一条龙体检（doctor）。

把散落的检查合成一个入口：
  1. 插件代码语法
  2. 插件逻辑自检（转发给 tests/bridge_selftest.py）
  3. **插件是否真的加载进了 AstrBot**（2026-10-09 加）
  4. 网关凭据（读 workbuddy-gateway.env，严格判据 authenticated == true）
  5. 端口监听（AstrBot 面板 / 反向 WS / NapCat WebUI / 网关）
  6. 关键进程

适用场景：机器人突然不回消息、改过插件代码、WorkBuddy 重启过之后，
跑一遍就能定位断在哪一环；平时也可当日常巡检用。

用法：
    双击 doctor.bat（日常巡检 / 故障定位都用它）
或：
    .venv\\Scripts\\python.exe scripts/doctor.py

本脚本会动态发现本机网关端口并验证现有凭据，但只读检查，不写回凭据。
若 WorkBuddy 重启时密码也变化，需要从当前 WorkBuddy 环境取得新凭据；
探测端口不能凭空获得新密码。按诊断提示在 WorkBuddy 会话内运行 reconnect。
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import reconnect as gateway_probe
from diagnostic_evidence import inspect_runtime, source_paths

# ---- Windows 编码自防御（2026-10-16 修复） ------------------------------
# 血泪教训，不是装饰：本脚本会被 .bat 双击、被管道/重定向调用，
# 而用户环境没有 PYTHONUTF8 / PYTHONIOENCODING。这些姿势下 stdout 编码
# 回落到系统 locale（中文 Windows = GBK/CP936），print 一个 emoji/✔✓⚠✗
# 就会 UnicodeEncodeError 崩在半路上（与自检子进程同源的坑）。
# 强制 UTF-8 + errors="replace"，保证任何调用姿势下都能完整跑完。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):  # Optional stream encoding support.
    pass

HERE = (
    Path(__file__).resolve().parents[1]
)  # Project root, independent of the caller working directory.
ENV_FILE = (
    HERE / "workbuddy-gateway.env"
)  # 网关凭据（端口+密码），reconnect.py 会改写它
PLUGIN = (
    HERE / "AstrBot" / "data" / "plugins" / "workbuddy_bridge" / "main.py"
)  # 被体检的插件入口
BRIDGE_TEST = (
    HERE / "tests" / "bridge_selftest.py"
)  # 插件逻辑自检脚本，由本脚本拉起子进程执行
CMD_CONFIG = (
    HERE / "AstrBot" / "data" / "cmd_config.json"
)  # AstrBot 配置（含面板账号与 jwt_secret）

DASHBOARD_PORT = "6185"
PLUGIN_NAME = "workbuddy_bridge"

# 三个固定端口：面板 / 插件与 AstrBot 的反向 WS / NapCat 的 WebUI。
# 注意网关端口不在此列 —— 它跟着会话进程走、经常变，由 check_gateway 单独验。
PORTS = {"6185": "AstrBot 面板", "6199": "AstrBot 反向 WS", "6099": "NapCat WebUI"}

# 体检结果台账：(项目名, 是否通过, 备注)，最后由 main() 汇总出结论
results: list[tuple[str, bool | None, str]] = []


def record(name: str, ok: bool | None, note: str = "") -> None:
    """登记一项体检结果并立即打印。

    所有 check_* 都通过它汇报，保证输出格式统一、末尾能汇总失败清单。
    """
    results.append((name, ok, note))
    mark = "UNKNOWN" if ok is None else ("PASS" if ok else "FAIL")
    print(f"[{mark}] {name}" + (f" —— {note}" if note else ""))


def check_plugin_loaded() -> None:
    """A Loading line is an attempt, not evidence of completed initialization."""
    evidence = inspect_runtime(
        PLUGIN,
        HERE / "AstrBot" / "data" / "logs" / "astrbot.log",
        require_heartbeat=True,
    )
    record("插件加载状态", evidence.state, evidence.note)


def check_code_version() -> None:
    """Use the initialized import-time source hash, never historical line numbers."""
    evidence = inspect_runtime(
        PLUGIN,
        HERE / "AstrBot" / "data" / "logs" / "astrbot.log",
        version_only=True,
    )
    record("代码版本核对", evidence.state, evidence.note)


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
    """Compile in memory: do not import or create plugin __pycache__ files."""
    try:
        for source in source_paths(PLUGIN):
            compile(source.read_bytes(), str(source), "exec")
    except SyntaxError as exc:
        record("插件语法检查", False, f"SyntaxError: {exc.msg}")
    except OSError as exc:
        record("插件语法检查", None, f"源码不可读：{type(exc).__name__}")
    else:
        record("插件语法检查", True, "仅验证语法，不代表插件已运行")


def check_bridge_selftest() -> None:
    """插件逻辑自检：把 tests/bridge_selftest.py 拉成子进程跑，认 stdout 里的 ALL PASS。

    用子进程而不是 import，是为了故障隔离 —— 自检脚本崩了只影响这一项，
    不会把 doctor 本体一起带走。
    """
    # 子进程显式带上 UTF-8 环境（双保险）：自检脚本自己也做了 reconfigure，
    # 但环境变量能兜住"将来它又被改回依赖默认编码"的情况。
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(BRIDGE_TEST)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
    )
    stdout = proc.stdout or ""
    ok = "ALL PASS" in stdout and "[FAIL]" not in stdout and proc.returncode == 0
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
            fails = [line for line in tail if line.startswith("[FAIL]")]
            if fails:
                verdict = fails[0]
    record("插件逻辑自检", ok, verdict)


def check_gateway() -> None:
    """Authenticate known candidates before a bounded read-only loopback scan."""
    cfg = load_env(ENV_FILE)
    file_token = str(cfg.get("CODEBUDDY_GATEWAY_PASSWORD") or "").strip().strip("\"'")
    env_token = os.environ.get("CODEBUDDY_GATEWAY_PASSWORD", "").strip()
    primary_token = file_token or env_token
    if not primary_token:
        record(
            "网关凭据", False, "缺少可验证的凭据，请在 WorkBuddy 会话内运行 reconnect"
        )
        return
    known = []
    for raw, token in (
        (
            cfg.get("CODEBUDDY_GATEWAY_PORT") or os.environ.get("SERVER__PORT"),
            primary_token,
        ),
        (os.environ.get("SERVER__PORT"), env_token),
    ):
        try:
            port = int(str(raw).strip().strip("\"'"))
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535 and token:
            known.append((port, token))
    tokens = list(dict.fromkeys(token for token in (primary_token, env_token) if token))
    deadline = time.monotonic() + 30
    tried = set()
    winner = None
    timed_out = False
    for phase in ("known", "scan"):
        if phase == "known":
            candidates = known
        else:
            ports = gateway_probe.scan_ports()
            candidates = [(port, token) for token in tokens for port in ports]
        for port, token in candidates:
            if (port, token) in tried:
                continue
            if time.monotonic() >= deadline:
                timed_out = True
                break
            tried.add((port, token))
            if gateway_probe.try_auth(port, token):
                winner = (port, token)
                break
        if winner is not None or timed_out:
            break
    if winner is not None:
        port, token = winner
        if token != file_token:
            record(
                "网关凭据",
                None,
                f"当前环境凭据可在端口 {port} 认证，但磁盘凭据未同步；请在此 WorkBuddy 会话运行 reconnect",
            )
        else:
            record(
                "网关凭据",
                True,
                f"动态发现端口 {port}，磁盘凭据认证通过；未修改配置，未验证 QQ 收发",
            )
    elif timed_out:
        record("网关凭据", None, "动态探测达到时间上限，尚未确认可用网关")
    else:
        record(
            "网关凭据",
            False,
            "已验证候选未通过认证；请确认 WorkBuddy 已启动，密码变化时在其会话内运行 reconnect",
        )


def check_question_status() -> None:
    """Explain recent queue ownership without cancelling or changing any work."""
    evidence = inspect_runtime(
        PLUGIN,
        HERE / "AstrBot" / "data" / "logs" / "astrbot.log",
        require_heartbeat=True,
    )
    if evidence.state is not True:
        record("问答调度", None, "没有可信的新鲜运行快照，排队与占用状态未知")
        return
    snapshot = evidence.snapshot
    question = snapshot["question"]
    remote = question["remote_state"]
    reasons = {
        "none": "没有遗留任务记录",
        "recorded": "保留着上一轮任务记录，是否结束需由网关确认",
        "submission_unknown": "上一轮提交结果不明，不能自动重提",
        "gateway_changed": "网关已换地址，旧任务是否结束仍待确认",
        "unreadable": "旧任务记录不可读，需要管理员检查",
    }
    blocked = remote in {"submission_unknown", "gateway_changed", "unreadable"}
    note = f"处理中 {int(question['active'])} 条，等待 {question['waiting']}/{question['limit']} 条；{reasons[remote]}"
    if blocked:
        note += "；先确认 WorkBuddy 旧任务已结束，再用 /恢复问答 确认上一轮已结束"
    record("问答调度", None if blocked else True, note)
    proactive = snapshot["proactive"]
    print(
        "[INFO] 主动闲聊："
        + (
            "因连续失败暂停，成功的普通问答后可恢复"
            if proactive["paused"]
            else "未因连续失败暂停"
        )
    )
    last = question["last_failure"]
    labels = {
        "[超时]": "任务超时",
        "[排队超时]": "排队超时",
        "[队列已满]": "等待队列已满",
        "[状态待确认]": "旧任务状态待确认",
        "[回复校验失败]": "回复回执不匹配",
        "[凭据失效]": "凭据认证失败",
        "[未配置]": "缺少凭据",
        "[网关不可达]": "未找到可认证网关",
        "[网关错误]": "网关返回错误",
        "[连接失败]": "连接中断",
        "[环境错误]": "运行环境异常",
        "[内部错误]": "插件内部异常",
        "cancelled": "本地请求取消",
    }
    if last:
        print(
            "[INFO] 最近失败："
            + labels.get(last, "其他内部结果")
            + "（历史记录，不等同于当前仍故障）"
        )


def listening_ports() -> set[str] | None:
    """当前处于 LISTENING 的端口集合（字符串形式，与 PORTS 的键对齐）。

    netstat 不可用时返回 None，由调用方报告 UNKNOWN，不能冒充未监听证据。
    """
    try:
        proc = subprocess.run(
            ["netstat", "-ano"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    out = proc.stdout
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
        if live is None:
            record(
                f"端口 {port}（{label}）", None, "无法观察端口，不能据此判定服务状态"
            )
        else:
            record(
                f"端口 {port}（{label}）",
                port in live,
                "观察到监听" if port in live else "本次观测未见监听",
            )


def check_processes() -> None:
    """关键进程清点：QQ（NapCat 宿主）必须在跑；Python 进程数作参考（AstrBot/插件都是 python）。"""
    try:
        proc = subprocess.run(
            ["tasklist"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        )
    except Exception:  # noqa: BLE001
        record("关键进程", None, "tasklist 不可用，运行状态未知")
        return
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        record("关键进程", None, "没有有效进程观测结果")
        return
    out = proc.stdout.lower()
    record(
        "QQ 进程",
        "qq.exe" in out,
        "在跑" if "qq.exe" in out else "未运行（需双击 start-napcat.bat）",
    )
    record("Python 进程", out.count("python.exe") >= 1, f"{out.count('python.exe')} 个")


def main() -> int:
    """按依赖顺序跑完全部检查并汇总结论。

    顺序有讲究：先验"文件本身没问题"（语法/逻辑自检），再验"运行态"
    （加载状态/版本/网关/端口/进程）—— 前面挂掉往往就是后面的根因。
    退出码：0 所列检查通过 / 1 有失败 / 2 无失败但证据不足（UNKNOWN）。
    """
    print("=" * 62)
    results.clear()
    print(" QQbot 一条龙体检")
    print("=" * 62)
    for check in (
        check_compile,
        check_bridge_selftest,
        check_plugin_loaded,
        check_code_version,
        check_gateway,
        check_question_status,
        check_ports,
        check_processes,
    ):
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - each failed probe must become UNKNOWN
            record(check.__name__, None, f"检查未完成：{type(exc).__name__}")

    print("-" * 62)
    # 汇总失败清单，并针对常见失败给出下一步指引
    failed = [name for name, ok, _ in results if ok is False]
    unknown = [name for name, ok, _ in results if ok is None]
    if not failed and not unknown:
        print("结论：所列检查通过；尚未验证 QQ 登录和消息端到端收发。")
        return 0
    print(
        f"结论：{len(failed)} 项失败、{len(unknown)} 项 UNKNOWN，不能断言机器人健康。"
    )
    if failed:
        print("失败：" + "；".join(failed))
    if unknown:
        print("未确认：" + "；".join(unknown))
    if "插件加载状态" in failed:
        print(
            "提示：插件没加载上时，双击 reload-plugin.bat 可以就地重载，不必重启 AstrBot。"
        )
    print("提示：凭据问题可先用 reconnect.bat；拿不到新密码时再回 WorkBuddy 会话刷新。")
    return 1 if failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
