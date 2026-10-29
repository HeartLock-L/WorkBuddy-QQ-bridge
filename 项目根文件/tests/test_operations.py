"""Offline regression tests: no real Windows process or service is touched."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


encoding_tool = load_module("operations_encoding", "fix-bat-encoding.py")
reset_tool = load_module("operations_password_reset", "reset-astrbot-password.py")


class EncodingRegressionTests(unittest.TestCase):
    def test_ascii_launcher_keeps_utf8_code_page(self):
        raw = b"@echo off\nchcp 65001 >nul\necho launch\n"
        output, encoding = encoding_tool.normalized_bytes(raw)
        self.assertEqual(output, raw.replace(b"\n", b"\r\n"))
        self.assertEqual(encoding, "ascii")

    def test_utf8_body_and_chcp_remain_compatible(self):
        text = "@echo off\nchcp 65001 >nul\necho 测试🍅\n"
        output, encoding = encoding_tool.normalized_bytes(text.encode("utf-8"))
        self.assertEqual(output.decode("utf-8"), text.replace("\n", "\r\n"))
        self.assertEqual(encoding, "utf-8")

    def test_chinese_without_code_page_uses_lossless_gbk(self):
        text = "@echo off\necho 测试\n"
        output, encoding = encoding_tool.normalized_bytes(text.encode("utf-8"))
        self.assertEqual(output.decode("gbk"), text.replace("\n", "\r\n"))
        self.assertEqual(encoding, "gbk")

    def test_ambiguous_or_lossy_conversion_does_not_modify_original(self):
        cases = [
            "@echo off\necho 🍅\n",
            "@echo off\nrem 中文\nchcp 65001 >nul\n",
            "@echo off\nchcp 936\nchcp 65001\necho 中文\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.bat"
            with patch.object(encoding_tool, "BACKUP", Path(directory) / "backups"):
                for text in cases:
                    with self.subTest(text=text):
                        raw = text.encode("utf-8")
                        path.write_bytes(raw)
                        with self.assertRaises((UnicodeError, ValueError)):
                            encoding_tool.convert(path)
                        self.assertEqual(path.read_bytes(), raw)
                        self.assertFalse(encoding_tool.BACKUP.exists())

    def test_repeated_repairs_preserve_each_original_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.bat"
            backup = Path(directory) / "backups"
            first = b"@echo off\nchcp 65001 >nul\n"
            second = b"@echo off\necho second edit\n"
            with patch.object(encoding_tool, "BACKUP", backup):
                path.write_bytes(first)
                encoding_tool.convert(path)
                encoding_tool.convert(path)  # Already normalized: no new backup.
                path.write_bytes(second)
                encoding_tool.convert(path)
            self.assertEqual((backup / path.name).read_bytes(), first)
            self.assertEqual((backup / (path.name + ".1.bak")).read_bytes(), second)
            self.assertEqual(len(list(backup.iterdir())), 2)


class PasswordResetRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "project with spaces"
        executable = self.root / ".venv" / "Scripts" / "python.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"mock")
        (self.root / "start-astrbot.bat").write_text("mock")
        self.record = {
            "LocalPort": 6185,
            "ProcessId": 1234,
            "ExecutablePath": str(executable),
            "CommandLine": f'"{executable}" "{self.root / "AstrBot" / "main.py"}" --webui-dir "mock"',
            "CreationDate": "2026-10-22T00:00:00.0000000Z",
        }

    def test_exact_port_excludes_61850_and_deduplicates_ipv4_ipv6(self):
        other_port = {**self.record, "LocalPort": 61850, "ProcessId": 5678}
        snapshot = [other_port, self.record, self.record.copy()]
        with patch.object(
            reset_tool, "_run_powershell", return_value=json.dumps(snapshot)
        ):
            self.assertEqual(reset_tool.listener_processes(), [self.record])

    def test_unknown_port_or_cim_payload_is_refused(self):
        for snapshot in ["", "null", "{}", '[{"ProcessId":1234}]']:
            with (
                self.subTest(snapshot=snapshot),
                patch.object(reset_tool, "_run_powershell", return_value=snapshot),
                self.assertRaises(reset_tool.ResetRefused),
            ):
                reset_tool.listener_processes()

    def test_verified_project_process_can_be_stopped_then_port_rechecked(self):
        with (
            patch.object(
                reset_tool, "listener_processes", side_effect=[[self.record], []]
            ) as listeners,
            patch.object(reset_tool, "stop_verified_process") as stop,
        ):
            reset_tool.prepare_reset(self.root)
        stop.assert_called_once_with(self.record, verify_parent=False)
        self.assertEqual(listeners.call_count, 2)

    def test_all_identities_are_checked_before_any_termination(self):
        unknown = {
            **self.record,
            "ProcessId": 5678,
            "ExecutablePath": r"C:\Other\python.exe",
        }
        with (
            patch.object(
                reset_tool, "listener_processes", return_value=[self.record, unknown]
            ),
            patch.object(reset_tool, "stop_verified_process") as stop,
            self.assertRaises(reset_tool.ResetRefused),
        ):
            reset_tool.prepare_reset(self.root)
        stop.assert_not_called()

    def test_relative_or_decoy_script_arguments_are_refused(self):
        executable = self.record["ExecutablePath"]
        wrong_commands = [
            f'"{executable}" main.py',
            f'"{executable}" "{self.root / "other.py"}" "{self.root / "AstrBot" / "main.py"}"',
            f'"C:\\Other\\python.exe" "{self.root / "AstrBot" / "main.py"}"',
        ]
        for command in wrong_commands:
            with self.subTest(command=command):
                record = {**self.record, "CommandLine": command}
                with (
                    patch.object(
                        reset_tool, "listener_processes", return_value=[record]
                    ),
                    patch.object(reset_tool, "stop_verified_process") as stop,
                ):
                    with self.assertRaises(reset_tool.ResetRefused):
                        reset_tool.prepare_reset(self.root)
                    stop.assert_not_called()

    def test_no_listener_does_not_stop_any_process(self):
        with (
            patch.object(reset_tool, "listener_processes", return_value=[]),
            patch.object(reset_tool, "stop_verified_process") as stop,
        ):
            reset_tool.prepare_reset(self.root)
        stop.assert_not_called()

    def test_still_occupied_port_prevents_reset_start(self):
        with (
            patch.object(
                reset_tool,
                "listener_processes",
                side_effect=[[self.record], [self.record]],
            ),
            patch.object(reset_tool, "stop_verified_process"),
            self.assertRaises(reset_tool.ResetRefused),
        ):
            reset_tool.prepare_reset(self.root)

    def test_windows_failure_becomes_failed_preflight(self):
        with (
            patch.object(
                reset_tool,
                "prepare_reset",
                side_effect=reset_tool.ResetRefused("CIM unavailable"),
            ),
            patch("builtins.print"),
        ):
            self.assertEqual(reset_tool.main(), 1)

    def venv_child(self):
        base = self.root / "base-runtime" / "python.exe"
        (self.root / ".venv" / "pyvenv.cfg").write_text(
            f"home = {base.parent}\n", encoding="utf-8"
        )
        parent = {**self.record, "ProcessId": 5678}
        main_file = self.root / "AstrBot" / "main.py"
        return {
            **self.record,
            "ExecutablePath": str(base),
            "CommandLine": f'"{base}" "{main_file}" --webui-dir "mock"',
            "CreationDate": "2026-10-22T00:00:00.0010000Z",
            "ParentProcessId": parent["ProcessId"],
            "Parent": parent,
        }

    def test_venv_base_child_requires_both_launcher_and_absolute_script(self):
        child = self.venv_child()
        main_file = self.root / "AstrBot" / "main.py"
        for command_exe in (child["ExecutablePath"], self.record["ExecutablePath"]):
            with self.subTest(command_exe=command_exe):
                candidate = {**child, "CommandLine": f'"{command_exe}" "{main_file}"'}
                with (
                    patch.object(
                        reset_tool, "listener_processes", side_effect=[[candidate], []]
                    ),
                    patch.object(reset_tool, "stop_verified_process") as stop,
                ):
                    reset_tool.prepare_reset(self.root)
                stop.assert_called_once_with(candidate, verify_parent=True)

    def test_unknown_or_reused_venv_parent_never_reaches_termination(self):
        child = self.venv_child()
        parent = child["Parent"]
        cases = [
            {**child, "Parent": None},
            {**child, "ParentProcessId": 9999},
            {**child, "Parent": {**parent, "ProcessId": child["ProcessId"]}},
            {**child, "Parent": {**parent, "ExecutablePath": r"C:\Other\python.exe"}},
            {
                **child,
                "Parent": {
                    **parent,
                    "CommandLine": f'"{parent["ExecutablePath"]}" main.py',
                },
            },
            {
                **child,
                "Parent": {**parent, "CreationDate": "2026-10-22T00:00:00.0020000Z"},
            },
            {**child, "Parent": {**parent, "CreationDate": "unknown"}},
            {**child, "ExecutablePath": r"C:\Other\python.exe"},
        ]
        for candidate in cases:
            with (
                self.subTest(candidate=candidate),
                patch.object(
                    reset_tool, "listener_processes", return_value=[candidate]
                ),
                patch.object(reset_tool, "stop_verified_process") as stop,
                self.assertRaises(reset_tool.ResetRefused),
            ):
                reset_tool.prepare_reset(self.root)
            stop.assert_not_called()

    def test_venv_child_cannot_use_missing_or_ambiguous_base_config(self):
        child = self.venv_child()
        config = self.root / ".venv" / "pyvenv.cfg"
        for text in ("", "home = relative\n", "home = C:\\One\nhome = C:\\Two\n"):
            config.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(reset_tool.ResetRefused):
                reset_tool.verify_identity(child, self.root)
        config.unlink()
        with self.assertRaises(reset_tool.ResetRefused):
            reset_tool.verify_identity(child, self.root)

    def test_cim_seven_digit_utc_timestamps_have_python310_compatible_parsing(self):
        utc = reset_tool._creation_time(
            {"CreationDate": "2026-10-22T00:00:00.1234567Z"}
        )
        offset = reset_tool._creation_time(
            {"CreationDate": "2026-10-22T08:00:00.1234567+08:00"}
        )
        self.assertEqual(utc, offset)
        self.assertEqual(utc.microsecond, 123456)
        with self.assertRaises(reset_tool.ResetRefused):
            reset_tool._creation_time({"CreationDate": "2026-10-22T00:00:00"})


if __name__ == "__main__":
    unittest.main()
