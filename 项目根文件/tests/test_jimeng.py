"""Exercise the extracted CLI client with local fixtures and no external services."""

import concurrent.futures
import importlib.util
import json
import sys
import tempfile
import threading
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "AstrBot/data/plugins/workbuddy_bridge/jimeng.py"
)
SPEC = importlib.util.spec_from_file_location("jimeng_under_test", MODULE_PATH)
JIMENG = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = JIMENG
SPEC.loader.exec_module(JIMENG)


class JimengClientTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.state = self.root / "draw_sessions.json"
        self.settings = JIMENG.JimengSettings(
            bin_path="configured-folder/dreamina.exe",
            output_dir=self.root / "images",
            model="chosen-model",
            ratio="16:9",
            resolution="2k",
            timeout=30,
        )
        self.runner = Mock(side_effect=AssertionError("Unexpected CLI operation"))
        self.runner_patch = patch.object(JIMENG.subprocess, "run", self.runner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)
        self.client = JIMENG.JimengClient(self.state, logger=Mock())

    def test_existing_mapping_loads_without_cli_or_file_changes(self):
        original = '{"123": "456", "789": "0", "invalid": "10"}'
        self.state.write_text(original, encoding="utf-8")
        client = JIMENG.JimengClient(self.state)
        self.assertEqual(client.sessions, {"123": "456"})
        self.assertEqual(self.state.read_text(encoding="utf-8"), original)
        self.runner.assert_not_called()

    def test_renamed_session_is_reused_from_existing_mapping(self):
        self.client.sessions = {"123": "456"}
        self.runner.side_effect = None
        self.runner.return_value = types.SimpleNamespace(
            returncode=0,
            stdout="456  renamed from image prompt  No  2026-10-27 12:00",
            stderr="",
        )
        self.assertEqual(
            self.client.ensure_session("name", "123", self.settings), "456"
        )
        self.runner.assert_not_called()

    def test_saved_session_outside_recent_page_is_not_recreated(self):
        self.client.sessions = {"123": "456"}
        self.client.session_list = Mock(return_value={"999": "someone else"})
        self.assertEqual(
            self.client.ensure_session("name", "123", self.settings), "456"
        )
        self.client.session_list.assert_not_called()
        self.runner.assert_not_called()

    def test_owner_suffix_recovery_persists_for_next_client(self):
        self.client.session_list = Mock(
            return_value={
                "0": "",
                "111": "someone使用 · 1230",
                "222": "owner使用 · 123",
            }
        )
        self.assertEqual(
            self.client.ensure_session("name", "123", self.settings), "222"
        )
        self.assertEqual(JIMENG.JimengClient(self.state).sessions, {"123": "222"})
        self.runner.assert_not_called()

    def test_new_session_uses_configured_executable_and_bounded_owner_label(self):
        self.client.session_list = Mock(return_value={"0": ""})
        self.runner.side_effect = None
        self.runner.return_value = types.SimpleNamespace(
            returncode=0, stdout="ID: 456", stderr=""
        )
        self.assertEqual(
            self.client.ensure_session("name" * 50, "123", self.settings), "456"
        )
        args = self.runner.call_args.args[0]
        self.assertEqual(args[:3], [self.settings.bin_path, "session", "create"])
        self.assertLessEqual(len(args[3]), 50)
        self.assertTrue(args[3].endswith("· 123"))
        self.assertEqual(JIMENG.JimengClient(self.state).sessions, {"123": "456"})

    def test_bad_session_table_never_creates_a_session(self):
        self.runner.side_effect = None
        for code, output in ((1, "unavailable"), (0, ""), (0, "unexpected columns")):
            with self.subTest(code=code, output=output):
                self.runner.reset_mock()
                self.runner.return_value = types.SimpleNamespace(
                    returncode=code, stdout=output, stderr=""
                )
                with self.assertRaises(RuntimeError):
                    self.client.ensure_session("name", "123", self.settings)
                self.assertEqual(self.runner.call_count, 1)
                self.assertEqual(
                    self.runner.call_args.args[0][1:3], ["session", "list"]
                )

    def test_invalid_created_session_is_not_saved(self):
        self.client.session_list = Mock(return_value={"0": ""})
        self.runner.side_effect = None
        for output in ("ID: 0", "missing", "ID: 12\nID: 13"):
            with self.subTest(output=output):
                self.runner.return_value = types.SimpleNamespace(
                    returncode=0, stdout=output, stderr=""
                )
                with self.assertRaises(RuntimeError):
                    self.client.ensure_session("name", "123", self.settings)
                self.assertFalse(self.state.exists())

    def test_generate_preserves_parameters_and_returns_this_submission_file(self):
        image = self.settings.output_dir / "result.png"

        def run(args, **kwargs):
            self.assertEqual(args[0], self.settings.bin_path)
            if args[1] == "text2image":
                flags = dict(zip(args[2::2], args[3::2]))
                self.assertEqual(
                    flags,
                    {
                        "--prompt": "a blue whale",
                        "--ratio": "4:3",
                        "--resolution_type": "2k",
                        "--model_version": "chosen-model",
                        "--generate_num": "1",
                        "--session": "456",
                        "--poll": "0",
                    },
                )
                body = {"submit_id": "our-task"}
            else:
                self.assertEqual(
                    args[1:],
                    [
                        "query_result",
                        "--submit_id",
                        "our-task",
                        "--download_dir",
                        str(self.settings.output_dir),
                    ],
                )
                image.write_bytes(b"offline fixture")
                body = {
                    "gen_status": "success",
                    "result_json": {"images": [{"path": str(image)}]},
                }
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(body), stderr=""
            )

        self.runner.side_effect = run
        path = self.client.generate(
            "a blue whale", session="456", settings=self.settings, ratio="4:3"
        )
        self.assertEqual(path, str(image))
        self.assertEqual(self.runner.call_count, 2)

    def test_failed_generation_does_not_switch_models_or_retry(self):
        self.runner.side_effect = [
            types.SimpleNamespace(
                returncode=0, stdout='{"submit_id":"our-task"}', stderr=""
            ),
            types.SimpleNamespace(
                returncode=0,
                stdout='{"gen_status":"failed","fail_reason":"backend error"}',
                stderr="",
            ),
        ]
        with self.assertRaisesRegex(JIMENG.GenerationFailed, "backend error"):
            self.client.generate("cat", session="456", settings=self.settings)
        self.assertEqual(self.runner.call_count, 2)

    def test_query_authentication_error_retries_only_the_saved_submission(self):
        saved = []
        image = self.settings.output_dir / "image.png"

        def run(args, **kwargs):
            if args[1] == "text2image":
                return types.SimpleNamespace(
                    returncode=0, stdout='{"submit_id":"original"}'
                )
            self.assertEqual(saved, ["original"])
            self.assertEqual(args[3], "original")
            if self.runner.call_count == 2:
                return types.SimpleNamespace(
                    returncode=1, stdout="", stderr="login fail ret=10002"
                )
            image.write_bytes(b"image")
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "gen_status": "success",
                        "result_json": {"images": [{"path": str(image)}]},
                    }
                ),
            )

        self.runner.side_effect = run
        with patch.object(JIMENG.time, "sleep"):
            self.assertEqual(
                self.client.generate(
                    "cat",
                    session="456",
                    settings=self.settings,
                    on_submitted=saved.append,
                ),
                str(image),
            )
        self.assertEqual(
            [call.args[0][1] for call in self.runner.call_args_list],
            ["text2image", "query_result", "query_result"],
        )

    def test_resume_queries_existing_task_without_submitting_or_callback(self):
        image = self.settings.output_dir / "restored.png"
        callback = Mock()

        def run(args, **kwargs):
            self.assertEqual(args[1:4], ["query_result", "--submit_id", "existing"])
            image.write_bytes(b"image")
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "gen_status": "success",
                        "result_json": {"images": [{"path": str(image)}]},
                    }
                ),
            )

        self.runner.side_effect = run
        self.assertEqual(
            self.client.generate(
                "unused",
                session="456",
                settings=self.settings,
                submit_id="existing",
                on_submitted=callback,
            ),
            str(image),
        )
        callback.assert_not_called()
        self.assertEqual(self.runner.call_count, 1)

    def test_uncertain_submission_is_never_replayed(self):
        responses = [
            JIMENG.subprocess.TimeoutExpired("fake-cli", 30),
            types.SimpleNamespace(returncode=1, stdout="", stderr="login fail"),
            types.SimpleNamespace(returncode=0, stdout="not JSON"),
            types.SimpleNamespace(returncode=0, stdout='{"gen_status":"running"}'),
            types.SimpleNamespace(returncode=0, stdout="[]"),
        ]
        for response in responses:
            with self.subTest(response=response):
                self.runner.reset_mock()
                self.runner.side_effect = [response]
                callback = Mock()
                with self.assertRaises(JIMENG.SubmissionUncertain):
                    self.client.generate(
                        "cat",
                        session="456",
                        settings=self.settings,
                        on_submitted=callback,
                    )
                self.assertEqual(self.runner.call_count, 1)
                callback.assert_not_called()

    def test_failed_submission_persistence_stops_before_query(self):
        self.runner.side_effect = [
            types.SimpleNamespace(returncode=0, stdout='{"submit_id":"saved-id"}')
        ]
        callback = Mock(side_effect=OSError("disk unavailable"))
        with self.assertRaisesRegex(OSError, "disk unavailable"):
            self.client.generate(
                "cat", session="456", settings=self.settings, on_submitted=callback
            )
        callback.assert_called_once_with("saved-id")
        self.assertEqual(self.runner.call_count, 1)

    def test_three_consecutive_query_errors_stop_without_new_submission(self):
        self.runner.side_effect = [
            types.SimpleNamespace(returncode=0, stdout='{"submit_id":"original"}'),
            types.SimpleNamespace(returncode=1, stdout="", stderr="unavailable"),
            types.SimpleNamespace(returncode=0, stdout="not JSON"),
            types.SimpleNamespace(returncode=0, stdout='{"ret":10002}'),
        ]
        with (
            patch.object(JIMENG.time, "sleep"),
            self.assertRaisesRegex(RuntimeError, "three times"),
        ):
            self.client.generate("cat", session="456", settings=self.settings)
        self.assertEqual(
            [c.args[0][1] for c in self.runner.call_args_list],
            ["text2image", "query_result", "query_result", "query_result"],
        )

    def test_submission_polling_and_sleep_share_one_deadline(self):
        now = [100.0]
        waits = []

        def run(args, **kwargs):
            waits.append(kwargs["timeout"])
            if args[1] == "text2image":
                now[0] += 20
                return types.SimpleNamespace(
                    returncode=0, stdout='{"submit_id":"original"}'
                )
            now[0] += 7
            return types.SimpleNamespace(
                returncode=0, stdout='{"gen_status":"running"}'
            )

        def sleep(delay):
            now[0] += delay

        self.runner.side_effect = run
        with (
            patch.object(JIMENG.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(JIMENG.time, "sleep", side_effect=sleep),
            self.assertRaises(TimeoutError),
        ):
            self.client.generate("cat", session="456", settings=self.settings)
        self.assertEqual(waits, [30, 10])
        self.assertEqual(now[0], 130)
        self.assertEqual(self.runner.call_count, 2)

    def test_session_list_and_create_share_one_deadline(self):
        now = [100.0]
        waits = []

        def run(args, **kwargs):
            waits.append(kwargs["timeout"])
            if args[2] == "list":
                now[0] += 23
                return types.SimpleNamespace(
                    returncode=0, stdout="0  default  Yes  2026-10-27 12:00"
                )
            now[0] += 3
            return types.SimpleNamespace(returncode=0, stdout="ID: 456")

        self.runner.side_effect = run
        with patch.object(JIMENG.time, "monotonic", side_effect=lambda: now[0]):
            self.assertEqual(
                self.client.ensure_session("name", "123", self.settings), "456"
            )
        self.assertEqual(waits, [30, 7])
        self.assertEqual(now[0], 126)

    def test_explicit_deadline_and_command_cap_are_honoured(self):
        self.runner.side_effect = JIMENG.subprocess.TimeoutExpired("fake-cli", 30)
        with patch.object(JIMENG.time, "monotonic", return_value=100):
            with self.assertRaises(TimeoutError):
                self.client.generate(
                    "cat",
                    session="456",
                    settings=replace(self.settings, timeout=120),
                    submit_id="existing",
                    deadline=150,
                )
            self.assertEqual(self.runner.call_args.kwargs["timeout"], 30)
            self.runner.reset_mock()
            with self.assertRaises(TimeoutError):
                self.client.generate(
                    "cat",
                    session="456",
                    settings=self.settings,
                    submit_id="existing",
                    deadline=103,
                )
            self.assertEqual(self.runner.call_args.kwargs["timeout"], 3)

    def test_cancellation_before_work_never_runs_cli(self):
        cancel = threading.Event()
        cancel.set()
        for operation in [
            lambda: self.client.ensure_session(
                "name", "123", self.settings, cancel_event=cancel
            ),
            lambda: self.client.generate(
                "cat", session="456", settings=self.settings, cancel_event=cancel
            ),
            lambda: self.client.rename_session(
                "456", "name", "123", self.settings, cancel_event=cancel
            ),
        ]:
            with self.assertRaises(InterruptedError):
                operation()
        self.runner.assert_not_called()

    def test_cancellation_during_submission_preserves_id_before_stopping(self):
        cancel = threading.Event()
        saved = []

        def run(*args, **kwargs):
            cancel.set()
            return types.SimpleNamespace(
                returncode=0, stdout='{"submit_id":"original"}'
            )

        self.runner.side_effect = run
        with self.assertRaises(InterruptedError):
            self.client.generate(
                "cat",
                session="456",
                settings=self.settings,
                cancel_event=cancel,
                on_submitted=saved.append,
            )
        self.assertEqual(saved, ["original"])
        self.assertEqual(self.runner.call_count, 1)

    def test_cancellation_interrupts_poll_wait(self):
        cancel = Mock()
        cancel.is_set.return_value = False
        cancel.wait.return_value = True
        self.runner.side_effect = [
            types.SimpleNamespace(returncode=0, stdout='{"gen_status":"running"}')
        ]
        with self.assertRaises(InterruptedError):
            self.client.generate(
                "cat",
                session="456",
                settings=self.settings,
                submit_id="existing",
                cancel_event=cancel,
            )
        cancel.wait.assert_called_once()
        self.assertLessEqual(cancel.wait.call_args.args[0], 5)

    def test_not_pass_is_terminal_and_is_not_polled_again(self):
        self.runner.side_effect = [
            types.SimpleNamespace(returncode=0, stdout='{"gen_status":"not_pass"}')
        ]
        with self.assertRaisesRegex(JIMENG.GenerationFailed, "not_pass"):
            self.client.generate(
                "cat", session="456", settings=self.settings, submit_id="existing"
            )
        self.assertEqual(self.runner.call_count, 1)

    def test_result_outside_download_directory_or_directory_is_rejected(self):
        outside = self.root / "another-task.png"
        outside.write_bytes(b"not this task")
        directory = self.settings.output_dir / "not-a-file.png"
        directory.mkdir(parents=True)
        for image in (outside, directory):
            with self.subTest(image=image):
                self.runner.side_effect = [
                    types.SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "gen_status": "success",
                                "result_json": {"images": [{"path": str(image)}]},
                            }
                        ),
                    )
                ]
                with self.assertRaisesRegex(RuntimeError, "local image"):
                    self.client.generate(
                        "cat",
                        session="456",
                        settings=self.settings,
                        submit_id="existing",
                    )

    def test_default_session_is_rejected_before_creating_output_directory(self):
        for session in ("", "0"):
            with self.subTest(session=session), self.assertRaises(RuntimeError):
                self.client.generate("cat", session=session, settings=self.settings)
        self.runner.assert_not_called()
        self.assertFalse(self.settings.output_dir.exists())

    def test_missing_local_image_is_not_reported_as_success(self):
        self.runner.side_effect = [
            types.SimpleNamespace(
                returncode=0, stdout='{"submit_id":"our-task"}', stderr=""
            ),
            types.SimpleNamespace(
                returncode=0,
                stdout='{"gen_status":"success","result_json":{"images":[]}}',
                stderr="",
            ),
        ]
        with self.assertRaisesRegex(RuntimeError, "local image"):
            self.client.generate("cat", session="456", settings=self.settings)

    def test_rename_failure_is_reported_without_raising(self):
        self.runner.side_effect = None
        self.runner.return_value = types.SimpleNamespace(
            returncode=1, stdout="", stderr="not found"
        )
        self.assertFalse(
            self.client.rename_session("456", "name", "123", self.settings)
        )
        self.assertFalse(self.client.rename_session("0", "name", "123", self.settings))
        self.assertEqual(self.runner.call_count, 1)

    def test_concurrent_session_bindings_survive_reload(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda n: self.client._remember_session(str(n), str(n + 100)),
                    range(10),
                )
            )
        self.assertEqual(
            JIMENG.JimengClient(self.state).sessions,
            {str(n): str(n + 100) for n in range(10)},
        )
        self.assertFalse(self.state.with_name(self.state.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
