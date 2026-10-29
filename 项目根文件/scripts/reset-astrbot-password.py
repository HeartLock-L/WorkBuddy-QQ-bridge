"""Safely stop this project's dashboard before the BAT resets its password.

Only an exact listener on port 6185 whose interpreter and absolute script
belong to this project may be stopped. Windows venv redirectors additionally
require a verified launcher parent and the base interpreter from pyvenv.cfg.
CIM failures, relative scripts, and unknown ownership fail closed. This module does not
start services; reset-astrbot-password.bat starts the password reset only
after this preflight succeeds.
"""

from __future__ import annotations

import base64
import json
import ntpath
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DASHBOARD_PORT = 6185
FIRST_ARGUMENTS = re.compile(r'^\s*(?:"([^\"]+)"|(\S+))\s+(?:"([^\"]+)"|(\S+))(?:\s|$)')
CREATED_AT = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})$"
)


class ResetRefused(RuntimeError):
    pass


def _run_powershell(script: str) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-EncodedCommand",
            encoded,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode:
        raise ResetRefused(
            "Windows could not verify or stop the dashboard process; nothing else will be stopped."
        )
    return proc.stdout.strip()


def listener_processes() -> list[dict]:
    # Exact numeric LocalPort comparison also covers IPv4 and IPv6 listeners.
    output = _run_powershell(f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$connections = @(Get-NetTCPConnection -ErrorAction Stop |
    Where-Object {{ $_.State -eq 'Listen' -and $_.LocalPort -eq {DASHBOARD_PORT} }})
$records = @($connections | ForEach-Object {{
    $connection = $_
    $item = Get-CimInstance Win32_Process -Filter "ProcessId = $($connection.OwningProcess)" -ErrorAction Stop
    if ($null -eq $item -or $null -eq $item.CreationDate) {{ throw 'Unknown listener identity' }}
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId = $($item.ParentProcessId)" -ErrorAction Stop
    $parentRecord = $null
    if ($null -ne $parent -and $null -ne $parent.CreationDate) {{
        $parentRecord = [PSCustomObject]@{{ ProcessId = [int]$parent.ProcessId;
            ExecutablePath = $parent.ExecutablePath; CommandLine = $parent.CommandLine;
            CreationDate = $parent.CreationDate.ToUniversalTime().ToString('o') }}
    }}
    [PSCustomObject]@{{ LocalPort = [int]$connection.LocalPort;
        ProcessId = [int]$item.ProcessId; ExecutablePath = $item.ExecutablePath;
        CommandLine = $item.CommandLine; CreationDate = $item.CreationDate.ToUniversalTime().ToString('o');
        ParentProcessId = [int]$item.ParentProcessId; Parent = $parentRecord }}
}})
ConvertTo-Json -InputObject $records -Depth 4 -Compress
""")
    try:
        records = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ResetRefused("Windows returned no reliable listener identity.") from exc
    if not isinstance(records, list) or any(
        not isinstance(item, dict) for item in records
    ):
        raise ResetRefused("Windows returned an unexpected listener identity.")
    selected = {}
    for item in records:
        if type(item.get("LocalPort")) is not int:
            raise ResetRefused("Windows returned an unknown listener port.")
        if item["LocalPort"] != DASHBOARD_PORT:
            continue
        process_id = item.get("ProcessId")
        if type(process_id) is not int or process_id <= 0:
            raise ResetRefused("Windows returned an unknown listener process.")
        if process_id in selected and selected[process_id] != item:
            raise ResetRefused("Listener identity changed while being inspected.")
        selected[process_id] = item
    return list(selected.values())


def _same_absolute_path(actual: str, expected: Path | str) -> bool:
    return (
        isinstance(actual, str)
        and ntpath.isabs(actual)
        and ntpath.normcase(ntpath.normpath(actual))
        == ntpath.normcase(ntpath.normpath(str(expected)))
    )


def _creation_time(record: dict) -> datetime:
    value = record.get("CreationDate")
    match = CREATED_AT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ResetRefused("Process creation time cannot be verified.")
    # CIM 'o' uses seven fractional digits; Python 3.10 accepts six. Normalize
    # before parsing rather than accepting a naive/local-time timestamp.
    fraction = (match.group(2) or "").ljust(6, "0")[:6]
    offset = "+00:00" if match.group(3) == "Z" else match.group(3)
    try:
        return datetime.fromisoformat(
            f"{match.group(1)}.{fraction}{offset}"
        ).astimezone(timezone.utc)  # noqa: UP017 - Python 3.10 has no datetime.UTC.
    except ValueError as exc:
        raise ResetRefused("Process creation time cannot be verified.") from exc


def _script_interpreter(record: dict, main_file: Path) -> str:
    command = record.get("CommandLine")
    match = FIRST_ARGUMENTS.match(command) if isinstance(command, str) else None
    if match is None or not _same_absolute_path(
        match.group(3) or match.group(4), main_file
    ):
        raise ResetRefused(
            "Cannot prove the listener runs this project's absolute AstrBot/main.py. "
            "Close the correct AstrBot window manually, then retry."
        )
    return match.group(1) or match.group(2)


def _base_interpreter(root: Path) -> str:
    try:
        lines = (
            (root / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8-sig").splitlines()
        )
    except (OSError, UnicodeError) as exc:
        raise ResetRefused(
            "Cannot verify this project's venv base interpreter."
        ) from exc
    homes = [
        value.strip()
        for line in lines
        if (parts := line.partition("="))[1] and parts[0].strip().lower() == "home"
        for value in [parts[2]]
    ]
    if len(homes) != 1 or not ntpath.isabs(homes[0]):
        raise ResetRefused("This project's venv home is missing or ambiguous.")
    return ntpath.join(homes[0], "python.exe")


def verify_identity(record: dict, root: Path) -> bool:
    """Verify identity; return whether the launcher parent must be rechecked."""
    executable = root / ".venv" / "Scripts" / "python.exe"
    main_file = root / "AstrBot" / "main.py"
    process_id = record.get("ProcessId")
    if type(process_id) is not int or process_id <= 0:
        raise ResetRefused(
            "Port 6185 belongs to an unverified process; refusing to stop it."
        )
    created = _creation_time(record)
    interpreter = _script_interpreter(record, main_file)
    if _same_absolute_path(record.get("ExecutablePath"), executable):
        if not _same_absolute_path(interpreter, executable):
            raise ResetRefused("Executable and command-line interpreter disagree.")
        return False

    # CPython's Windows venv launcher runs base Python as a child. Its child
    # command line uses the base path in 3.10 and may retain the venv argv[0]
    # in later releases. Neither form is trusted without the live parent chain.
    base = _base_interpreter(root)
    if not _same_absolute_path(record.get("ExecutablePath"), base) or not (
        _same_absolute_path(interpreter, base)
        or _same_absolute_path(interpreter, executable)
    ):
        raise ResetRefused("Listener is not this project's venv interpreter.")
    parent = record.get("Parent")
    if not isinstance(parent, dict) or (
        type(parent.get("ProcessId")) is not int
        or parent["ProcessId"] <= 0
        or parent["ProcessId"] == process_id
        or type(record.get("ParentProcessId")) is not int
        or record["ParentProcessId"] != parent["ProcessId"]
        or not _same_absolute_path(parent.get("ExecutablePath"), executable)
        or not _same_absolute_path(_script_interpreter(parent, main_file), executable)
        or _creation_time(parent) > created
    ):
        raise ResetRefused(
            "Cannot verify the project's venv launcher parent; refusing to stop the listener."
        )
    return True


def stop_verified_process(record: dict, *, verify_parent: bool = False) -> None:
    payload = base64.b64encode(json.dumps(record).encode("utf-8")).decode("ascii")
    # Bind a process handle before rechecking CIM identity, so PID reuse cannot
    # redirect termination to a newly created process. No taskkill by port/PID.
    _run_powershell(f"""
$ErrorActionPreference = 'Stop'
$expected = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json
$target = Get-Process -Id $expected.ProcessId -ErrorAction Stop
$null = $target.Handle
$live = Get-CimInstance Win32_Process -Filter "ProcessId = $($expected.ProcessId)" -ErrorAction Stop
if ($null -eq $live -or $target.HasExited -or
    $live.ExecutablePath -cne $expected.ExecutablePath -or
    $live.CommandLine -cne $expected.CommandLine -or
    $live.CreationDate.ToUniversalTime().ToString('o') -cne $expected.CreationDate) {{
    throw 'Process identity changed; refusing to stop'
}}
if ([Math]::Abs(($target.StartTime.ToUniversalTime() - $live.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1) {{
    throw 'Process handle identity mismatch'
}}
if ({"$true" if verify_parent else "$false"}) {{
    $parentTarget = Get-Process -Id $expected.Parent.ProcessId -ErrorAction Stop
    $null = $parentTarget.Handle
    $liveParent = Get-CimInstance Win32_Process -Filter "ProcessId = $($expected.Parent.ProcessId)" -ErrorAction Stop
    if ($null -eq $liveParent -or $parentTarget.HasExited -or
        $live.ParentProcessId -ne $expected.Parent.ProcessId -or
        $liveParent.ExecutablePath -cne $expected.Parent.ExecutablePath -or
        $liveParent.CommandLine -cne $expected.Parent.CommandLine -or
        $liveParent.CreationDate.ToUniversalTime().ToString('o') -cne $expected.Parent.CreationDate -or
        $liveParent.CreationDate -gt $live.CreationDate) {{
        throw 'Venv launcher parent identity changed'
    }}
    if ([Math]::Abs(($parentTarget.StartTime.ToUniversalTime() - $liveParent.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1) {{
        throw 'Venv launcher handle identity mismatch'
    }}
}}
$target.Kill()
if (-not $target.WaitForExit(10000)) {{ throw 'Dashboard did not exit' }}
""")


def prepare_reset(root: Path = HERE) -> None:
    if (
        not (root / ".venv" / "Scripts" / "python.exe").is_file()
        or not (root / "start-astrbot.bat").is_file()
    ):
        raise ResetRefused("Project Python or start-astrbot.bat is missing.")
    records = listener_processes()
    # Verify every owner before stopping any process.
    proofs = [verify_identity(record, root) for record in records]
    for record, verify_parent in zip(records, proofs):
        stop_verified_process(record, verify_parent=verify_parent)
    if listener_processes():
        raise ResetRefused(
            "Port 6185 is still occupied; refusing to start a password reset."
        )


def main() -> int:
    try:
        prepare_reset()
    except (ResetRefused, OSError, subprocess.SubprocessError) as exc:
        print(f"[ERROR] {exc}")
        return 1
    print("[OK] Port 6185 is free. The launcher can now reset the dashboard password.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
