"""把 .bat 批量转成 cmd 能吃的样子（一次性的，跑完可删）。

背景（2026-10-21 实测）：
    pause-bridge.bat 双击后一堆 "'cho' 不是内部或外部命令"、标记文件根本没生成。
    原因是文件存成了 UTF-8，而 cmd 读 .bat 用的是系统 OEM 代码页（中文机 = 936/GBK）。
    UTF-8 中文是 3 字节、GBK 按 2 字节配对，末尾多出的 1 个字节会把下一行的首字符
    （哪怕是 e、t 这种 ASCII）一起吞掉 —— 于是 echo 变 cho、title 变 tle、整行错位。
    文件里再写 `chcp 65001` 也救不回来：那行之前的内容早就按 936 解析过了。

做法：
    1. 原文件按 UTF-8 读入（能读就是 UTF-8；读不了说明本来就是 GBK，跳过）
    2. 去掉 `chcp 65001`（改成 GBK 之后它会反过来把输出弄乱）
    3. 统一写成 GBK + CRLF（cmd 唯一无条件认的组合）
    4. 原文件先备份到 备份-bat-<日期>/
"""

from __future__ import annotations

import shutil
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
BACKUP = HERE / f"备份-bat-{date.today():%Y%m%d}"


def convert(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "本来就是 GBK，跳过"

    lines = []
    dropped_chcp = False
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line.strip().lower().startswith("chcp 65001"):
            dropped_chcp = True
            continue
        lines.append(line)

    out = "\r\n".join(lines)
    if not out.endswith("\r\n"):
        out += "\r\n"

    BACKUP.mkdir(exist_ok=True)
    shutil.copy2(path, BACKUP / path.name)
    path.write_bytes(out.encode("gbk", errors="replace"))
    tail = "（已去掉 chcp 65001）" if dropped_chcp else ""
    return f"转成 GBK + CRLF{tail}"


def main() -> int:
    bats = sorted(HERE.glob("*.bat"))
    if not bats:
        print("没找到 .bat")
        return 1
    for bat in bats:
        print(f"{bat.name:<28} {convert(bat)}")
    print(f"\n原文件备份在：{BACKUP.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
