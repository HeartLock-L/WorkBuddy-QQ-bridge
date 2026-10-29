"""Normalize BAT line endings without removing intentional code-page commands.

ASCII scripts stay ASCII. Scripts that select UTF-8 before any non-ASCII
content stay UTF-8; other Chinese scripts use GBK. Unsupported or ambiguous
code-page changes and lossy conversions are refused before changing the file.
Original bytes are backed up without overwriting an earlier backup.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

HERE = Path(__file__).resolve().parents[1]
BACKUP = HERE / "backups" / f"bat-encoding-{date.today():%Y%m%d}"
CODE_PAGE = re.compile(r"^\s*@?chcp(?:\.com)?\s+(\d+)\b", re.IGNORECASE)


def normalized_bytes(raw: bytes) -> tuple[bytes, str]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    encoding = "ascii"
    if not text.isascii():
        switches = [
            (i, int(match.group(1)))
            for i, line in enumerate(lines)
            if (match := CODE_PAGE.match(line))
        ]
        pages = {page for _, page in switches}
        if len(pages) > 1 or pages - {936, 65001}:
            raise ValueError(
                "non-ASCII script has unsupported/mixed code pages; left unchanged"
            )
        encoding = "gbk"
        if pages == {65001}:
            first_unicode = next(
                i for i, line in enumerate(lines) if not line.isascii()
            )
            if switches[0][0] >= first_unicode:
                raise ValueError(
                    "chcp 65001 must precede non-ASCII content; left unchanged"
                )
            encoding = "utf-8"
    out = "\r\n".join(lines)
    if not out.endswith("\r\n"):
        out += "\r\n"
    return out.encode(encoding), encoding


def convert(path: Path) -> str:
    raw = path.read_bytes()
    out, encoding = normalized_bytes(raw)
    if out == raw:
        return f"unchanged ({encoding} + CRLF)"

    BACKUP.mkdir(parents=True, exist_ok=True)
    backup = BACKUP / path.name
    index = 0
    while backup.exists() and backup.read_bytes() != raw:
        index += 1
        backup = BACKUP / f"{path.name}.{index}.bak"
    if not backup.exists():
        shutil.copy2(path, backup)

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=path.name + ".", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(out)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return f"converted to {encoding} + CRLF (chcp preserved)"


def main() -> int:
    bats = sorted([*HERE.glob("*.bat"), *(HERE / "scripts").glob("*.bat")])
    if not bats:
        print("No .bat files found.")
        return 1
    failed = False
    for bat in bats:
        try:
            result = convert(bat)
        except (OSError, UnicodeError, ValueError) as exc:
            result = f"ERROR: {exc}"
            failed = True
        print(f"{bat.name:<28} {result}")
    print(f"\nOriginals: {BACKUP.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
