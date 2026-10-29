"""一键重连：自动修复"网关凭据过期"（机器人不回消息的常见原因）。

## 背景

WorkBuddy 这侧的网关地址（端口）**会跟着活跃会话/进程变化**：
WorkBuddy 重开、或会话进程轮换之后，旧端口就不再监听。
而"密码"在同一个 WorkBuddy 生命周期内**通常不变**。

端口一变，插件（workbuddy_bridge）就找不到网关，表现为：
    - 群里 @ 机器人后长时间没反应
    - 偶尔回一句"没接住话"式的兜底话术

绝大多数情况下，只要能"扫一遍本机端口、找到还接受这套密码的那个"，
把端口更新到 workbuddy-gateway.env 就恢复了 —— 本脚本自动做这件事。

## 本脚本做的事

  1. 收集候选 (端口, 密码) 组合：
       a. 环境变量 SERVER__PORT / CODEBUDDY_GATEWAY_PASSWORD
          （由 WorkBuddy 会话内的 AI 运行时最准）
       b. 凭据文件里的旧值
       c. 本机 127.0.0.1 全部监听端口（分别配 a、b 两种密码）
  2. 逐个探测 /api/v1/auth/status，第一个 authenticated == true 的组合胜出
  3. 把胜者写回 workbuddy-gateway.env（端口或密码有变化时）
  4. 调用 reload-plugin.py，单独确认插件新实例与版本（未确认时保留警告）
  5. 读回文件做验收

## 为什么重载走 API 而不是重启 AstrBot

插件是通过 AstrBot 的 reload 接口热重载的，不重启整个 AstrBot ——
重启会打断 NapCat 与 QQ 的连接、丢掉运行中的会话上下文，动静太大；
热重载只换插件这一层，NapCat 连接和上下文都保得住。

## 用法

    双击 reconnect.bat
或：
    .venv\\Scripts\\python.exe scripts/reconnect.py

退出码：0 = 凭据认证与插件版本均确认；1 = 凭据修复失败；
2 = 凭据认证通过，但插件恢复未确认。所有结果均不代表已验证 QQ 收发。
"""

from __future__ import annotations

# 子进程诊断输出按 UTF-8 读取；本脚本被管道调用时也保持相同编码。
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parents[1]  # Project root.
ENV_FILE = HERE / "workbuddy-gateway.env"  # 插件启动时读的凭据文件，本脚本负责把它改对

# 本机回环不走系统代理（否则会 502）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

AUTH_TIMEOUT = 2  # 单次探测超时（秒）。连接被拒是瞬时的；半死端口才会拖满。


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


def scan_ports() -> list[int]:
    """本机 127.0.0.1 上处于 LISTENING 的端口（升序）。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            errors="replace",
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    ports = set()
    for line in out.splitlines():
        if "LISTENING" not in line:
            continue
        # 只认 127.0.0.1 回环地址 —— 网关只监听本机；扫 0.0.0.0 会引入无关服务
        m = re.search(r"127\.0\.0\.1:(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def try_auth(port: int, token: str) -> bool:
    """判据必须认 authenticated is True —— 旧密码也可能返回 200 + false。"""
    if not token:
        return False
    url = f"http://127.0.0.1:{port}/api/v1/auth/status"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with OPENER.open(req, timeout=AUTH_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode("utf-8", "replace")
        # 严格判据：HTTP 200 不够 —— 旧密码打在新网关上也可能是
        # 200 + {"authenticated": false}。必须显式认 true。
        parsed = json.loads(body)
        return isinstance(parsed, dict) and parsed.get("authenticated") is True
    except Exception:  # noqa: BLE001
        # 连接被拒/超时/返回非 JSON 都算"这个组合不行"，继续试下一个
        return False


def main() -> int:
    """主流程：收集候选 → 逐个探测 → 写回凭据 → 重载插件 → 读回验收。

    核心思路来自网关凭据机制：端口跟着会话进程走、经常变，
    但密码在同一 WorkBuddy 生命周期内通常不变 —— 所以"拿旧密码扫端口"
    命中率很高，绝大多数断连都能自愈；扫不到才说明密码也变了。
    """
    print("=" * 62)
    print(" QQbot 一键重连（自动修复网关凭据）")
    print("=" * 62)

    cfg = load_env(ENV_FILE)
    file_port_raw = (cfg.get("CODEBUDDY_GATEWAY_PORT") or "").strip()
    file_token = (cfg.get("CODEBUDDY_GATEWAY_PASSWORD") or "").strip()
    env_port_raw = (os.environ.get("SERVER__PORT") or "").strip()
    env_token = (os.environ.get("CODEBUDDY_GATEWAY_PASSWORD") or "").strip()

    def as_int(v: str):
        try:
            return int(v) if v else None
        except ValueError:
            return None

    file_port = as_int(file_port_raw)
    env_port = as_int(env_port_raw)

    print(
        f"[1/4] 收集候选 …（文件端口 {file_port_raw or '缺失'}，环境端口 {env_port_raw or '无'}）"
    )

    open_ports = scan_ports()

    # 候选按"优先顺序"排列：环境组合 → 文件组合 → 用两套密码扫全部端口
    ordered: list[tuple[int, str, str]] = []
    if env_port and env_token:
        ordered.append((env_port, env_token, "环境变量"))
    if file_port and file_token:
        ordered.append((file_port, file_token, "凭据文件"))
    for token, label in ((file_token, "文件密码"), (env_token, "环境密码")):
        if not token:
            continue
        for port in open_ports:
            ordered.append((port, token, f"{label}×扫端口"))

    # 去重：环境端口/文件端口很可能也在监听清单里，避免同一组合重复探测
    seen: set[tuple[int, str]] = set()
    candidates: list[tuple[int, str, str]] = []
    for item in ordered:
        key = (item[0], item[1])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)

    print(
        f"      待探测 {len(candidates)} 个组合（本机监听端口 {len(open_ports)} 个）…"
    )

    # 按优先级逐个探测，第一个认证通过的即胜出（单次 2s 超时，扫起来不慢）
    winner: tuple[int, str, str] | None = None
    for port, token, source in candidates:
        if try_auth(port, token):
            winner = (port, token, source)
            break

    if winner is None:
        print()
        print("[FAIL] 没有找到可用的网关。")
        print("       最可能的情况：WorkBuddy 重启过，密码也变了。")
        print("       -> 回 WorkBuddy 会话里说一句「重连」，让 AI 刷新凭据。")
        return 1

    win_port, win_token, win_source = winner
    print(f"      命中：端口 {win_port}（来源：{win_source}），认证通过")

    # --- 写回凭据文件 -----------------------------------------------------
    changed = not (win_port == file_port and win_token == file_token)
    print("[2/4] 更新凭据文件 " + ENV_FILE.name + " …")
    if changed:
        ENV_FILE.write_text(
            f"CODEBUDDY_GATEWAY_PORT={win_port}\n"
            f"CODEBUDDY_GATEWAY_PASSWORD={win_token}\n",
            encoding="utf-8",
        )
        if file_port and win_port != file_port:
            print(f"      端口已更新：{file_port} -> {win_port}")
        else:
            print("      密码已更新（端口未变）")
    else:
        print("      本来就是可用状态，无需更改")

    # --- 重载插件，立即生效 ----------------------------------------------
    print("[3/4] 重载插件（让新凭据立即生效）…")
    reload_verified = False
    reload_script = HERE / "scripts" / "reload-plugin.py"
    if reload_script.exists():
        try:
            proc = subprocess.run(
                [sys.executable, str(reload_script)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            output = (proc.stdout or "").strip()
            errors = (proc.stderr or "").strip()
            # Preserve the child's warning/UNKNOWN evidence, not just one OK line.
            for line in (output + ("\n" + errors if errors else "")).splitlines():
                print("      " + line)
            reload_verified = proc.returncode == 0 and any(
                line.startswith("[VERIFIED]") for line in output.splitlines()
            )
            if not reload_verified:
                print(
                    f"      [WARN] 插件新实例/版本尚未确认（exit={proc.returncode}）；仅凭据已更新。"
                )
        except Exception as exc:  # noqa: BLE001
            print(
                f"      [WARN] 重载调用异常（{exc.__class__.__name__}）；插件恢复未确认。"
            )
    else:
        print("      [WARN] 找不到 reload-plugin.py；插件恢复未确认。")

    # --- 写后验收 ---------------------------------------------------------
    # 不信任"刚刚写进去的值"，重新从磁盘读一遍再真打一次认证 ——
    # 防止写文件编码/内容出错却报成功。
    print("[4/4] 读回文件验收 …")
    cfg2 = load_env(ENV_FILE)
    port2 = as_int((cfg2.get("CODEBUDDY_GATEWAY_PORT") or "").strip())
    token2 = (cfg2.get("CODEBUDDY_GATEWAY_PASSWORD") or "").strip()
    if port2 == win_port and token2 == win_token and try_auth(port2, token2):
        print(f"      [AUTH_OK] 凭据文件读写一致，认证通过（端口 {port2}）")
        print("-" * 62)
        if reload_verified:
            print("结论：凭据认证与插件版本已确认；尚未验证 QQ 登录和消息收发。")
            return 0
        print("[UNKNOWN] 仅网关认证通过；插件恢复未确认，不能宣布机器人恢复。")
        return 2

    print("      [FAIL] 写回后验收没通过（少见）—— 回 WorkBuddy 会话让 AI 看看。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
