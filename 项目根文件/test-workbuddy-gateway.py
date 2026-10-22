"""WorkBuddy Gateway self-test.

Usage:
    double-click test-gateway.bat
or:
    .venv\\Scripts\\python.exe test-workbuddy-gateway.py
    .venv\\Scripts\\python.exe test-workbuddy-gateway.py "your question"

It does three things and prints raw responses:
  1. auto-discover the Gateway port (it changes when WorkBuddy restarts) and
     confirm the auth state
  2. POST /api/v1/runs            -- start an agent run
  3. GET  /api/v1/runs/{id}/stream -- print raw SSE events

Note: the script explicitly bypasses system proxies (127.0.0.1 must not be
proxied, otherwise you get HTTP 502).
"""

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / "workbuddy-gateway.env"

# Bypass proxies: localhost requests must never go through a system proxy
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_env(path: Path) -> dict:
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


def local_ports() -> list[int]:
    """Listening ports on 127.0.0.1."""
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
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
        m = re.search(r"127\.0\.0\.1:(\d+)\s", line)
        if m:
            ports.add(int(m.group(1)))
    return sorted(ports)


def get(url: str, token: str, timeout: int = 8):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with OPENER.open(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def auth_ok(body: str) -> bool:
    """必须 authenticated: true —— 旧密码也返回 200+false，别被骗。"""
    try:
        return json.loads(body).get("authenticated") is True
    except Exception:  # noqa: BLE001
        return False


def discover(token: str, preferred):
    """Return (port, auth_status_body). Try configured port first, then scan."""
    candidates = []
    if preferred:
        candidates.append(preferred)
    candidates += [p for p in local_ports() if p != preferred]

    for port in candidates:
        try:
            status, body = get(f"http://127.0.0.1:{port}/api/v1/auth/status", token, 4)
        except Exception:  # noqa: BLE001
            continue
        if status == 200 and auth_ok(body):
            return port, body
    return None, ""


def main() -> int:
    cfg = load_env(ENV_FILE)
    token = cfg.get("CODEBUDDY_GATEWAY_PASSWORD", "")
    preferred = cfg.get("CODEBUDDY_GATEWAY_PORT")
    try:
        preferred = int(preferred) if preferred else None
    except (TypeError, ValueError):
        preferred = None

    print(f"credentials file : {ENV_FILE.name}")
    if token:
        print(f"password         : loaded ({len(token)} chars)")
    else:
        print("password         : MISSING -> ask the AI to regenerate it")
        return 1

    print("-" * 62)
    port, status = discover(token, preferred)
    if port is None:
        print("[1] FAILED: no WorkBuddy Gateway accepts this password.")
        print("    WorkBuddy was probably restarted (password changed),")
        print("    or WorkBuddy is not running.")
        print("    -> ask the AI to regenerate workbuddy-gateway.env")
        return 1

    if preferred and port != preferred:
        print(f"[1] note: configured port {preferred} is stale, using {port}")
    print(f"[1] gateway port {port}  auth: {status}")

    question = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "\u53ea\u56de\u7b54\u56db\u4e2a\u5b57\uff0c\u4e0d\u8981\u4f7f\u7528\u4efb\u4f55\u5de5\u5177\uff1a\u6d4b\u8bd5\u6210\u529f"
    )
    conv = f"selftest-{uuid.uuid4().hex[:8]}"
    body = {
        "id": f"selftest-{uuid.uuid4().hex[:12]}",
        "type": "text",
        "text": question,
        "source": {
            "platform": "generic",
            "sender": {"id": conv, "name": "selftest"},
            "conversation": {"id": conv, "type": "direct"},
        },
        "timeoutMs": 180000,
    }

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/runs",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with OPENER.open(req, timeout=30) as resp:
            started = resp.read().decode("utf-8", "replace")
        print(f"[2] POST runs    : {started}")
        run_id = json.loads(started)["data"]["runId"]
    except urllib.error.HTTPError as exc:
        print("[2] POST runs FAILED :", exc.code, exc.read().decode("utf-8", "replace"))
        return 1
    except Exception as exc:  # noqa: BLE001
        print("[2] POST runs FAILED :", exc)
        return 1

    print("[3] SSE raw events (max 150s):")
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/v1/runs/{run_id}/stream",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
            },
        )
        deadline = time.time() + 150
        with OPENER.open(req, timeout=150) as resp:
            while time.time() < deadline:
                line = resp.readline()
                if not line:
                    print("    <stream closed>")
                    break
                print("   ", line.decode("utf-8", "replace").rstrip())
    except Exception as exc:  # noqa: BLE001
        print("[3] stream FAILED :", exc)

    print("-" * 62)
    print("If section [3] printed nothing at all:")
    print("  the WorkBuddy desktop session is probably busy (runs get queued).")
    print("  Close any running conversation in WorkBuddy and retry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
