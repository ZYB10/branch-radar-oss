from __future__ import annotations

import threading
import unittest
from unittest import mock

from branch_radar.folder_picker import (
    FOLDER_PICKER_OUTPUT_LIMIT_BYTES,
    FolderPicker,
    FolderPickerError,
)
from branch_radar.process import ProcessResult


def process_result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    cancelled: bool = False,
) -> ProcessResult:
    return ProcessResult(
        argv=["picker"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        timed_out=timed_out,
        cancelled=cancelled,
    )


class FolderPickerTests(unittest.TestCase):
    def test_macos_uses_bounded_osascript_argv(self):
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(stdout="/Users/test/code/\n")
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda name: "/usr/bin/osascript" if name == "osascript" else None,
            timeout_seconds=45,
        )

        selection = picker.pick()

        self.assertEqual(selection.path, "/Users/test/code/")
        self.assertFalse(selection.cancelled)
        call = runner.run_limited.call_args
        argv = call.args[0]
        self.assertEqual(argv[0:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("choose folder", argv[2])
        self.assertEqual(call.kwargs["timeout"], 45)
        self.assertIsNone(call.kwargs["cancel_event"])
        self.assertEqual(
            call.kwargs["stdout_limit_bytes"],
            FOLDER_PICKER_OUTPUT_LIMIT_BYTES,
        )
        self.assertEqual(
            call.kwargs["stderr_limit_bytes"],
            FOLDER_PICKER_OUTPUT_LIMIT_BYTES,
        )

    def test_linux_prefers_zenity_then_falls_back_to_kdialog(self):
        binaries = {
            "zenity": "/usr/bin/zenity",
            "kdialog": "/usr/bin/kdialog",
        }
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(stdout="/srv/code\n")

        FolderPicker(
            runner,
            platform="linux",
            which=binaries.get,
            env={"DISPLAY": ":0"},
        ).pick()
        self.assertEqual(
            runner.run_limited.call_args.args[0][0],
            "/usr/bin/zenity",
        )

        runner.reset_mock()
        binaries.pop("zenity")
        FolderPicker(
            runner,
            platform="linux",
            which=binaries.get,
            env={"WAYLAND_DISPLAY": "wayland-0"},
        ).pick()
        self.assertEqual(
            runner.run_limited.call_args.args[0][0],
            "/usr/bin/kdialog",
        )

    def test_user_cancellation_is_a_successful_empty_selection(self):
        cases = [
            ("darwin", process_result(returncode=1, stderr="User canceled. (-128)")),
            ("linux", process_result(returncode=1)),
        ]
        for platform, result in cases:
            with self.subTest(platform=platform):
                runner = mock.Mock()
                runner.run_limited.return_value = result
                picker = FolderPicker(
                    runner,
                    platform=platform,
                    which=lambda _name: "/usr/bin/picker",
                    env={"DISPLAY": ":0"},
                )

                selection = picker.pick()

                self.assertTrue(selection.cancelled)
                self.assertIsNone(selection.path)

    def test_missing_picker_has_a_friendly_error_without_running_a_process(self):
        runner = mock.Mock()
        picker = FolderPicker(
            runner,
            platform="linux",
            which=lambda _name: None,
            env={"DISPLAY": ":0"},
        )

        with self.assertRaises(FolderPickerError) as raised:
            picker.pick()

        self.assertEqual(raised.exception.code, "folder_picker_unavailable")
        self.assertIn("手动输入", str(raised.exception))
        runner.run_limited.assert_not_called()

    def test_linux_without_a_graphical_session_fails_before_binary_lookup(self):
        runner = mock.Mock()
        binary_lookup = mock.Mock(return_value="/usr/bin/zenity")
        picker = FolderPicker(
            runner,
            platform="linux",
            which=binary_lookup,
            env={},
        )

        with self.assertRaises(FolderPickerError) as raised:
            picker.pick()

        self.assertEqual(raised.exception.code, "folder_picker_unavailable")
        self.assertIn("图形界面", str(raised.exception))
        binary_lookup.assert_not_called()
        runner.run_limited.assert_not_called()

    def test_linux_nonempty_stderr_is_not_misreported_as_cancellation(self):
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(
            returncode=1,
            stderr="cannot connect to display",
        )
        picker = FolderPicker(
            runner,
            platform="linux",
            which=lambda _name: "/usr/bin/zenity",
            env={"DISPLAY": ":0"},
        )

        with self.assertRaises(FolderPickerError) as raised:
            picker.pick()

        self.assertEqual(raised.exception.code, "folder_picker_failed")

    def test_only_line_endings_are_removed_from_selected_path(self):
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(stdout="/srv/code space \r\n")
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda _name: "/usr/bin/osascript",
        )

        selection = picker.pick()

        self.assertEqual(selection.path, "/srv/code space ")

    def test_process_failures_are_bounded_and_do_not_expose_stderr(self):
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(
            returncode=2,
            stderr="private path /Users/alice token=super-secret",
        )
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda _name: "/usr/bin/osascript",
        )

        with self.assertRaises(FolderPickerError) as raised:
            picker.pick()

        self.assertEqual(raised.exception.code, "folder_picker_failed")
        self.assertNotIn("alice", str(raised.exception))
        self.assertNotIn("super-secret", str(raised.exception))

    def test_timeout_has_a_distinct_safe_error(self):
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(
            returncode=124,
            timed_out=True,
        )
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda _name: "/usr/bin/osascript",
        )

        with self.assertRaises(FolderPickerError) as raised:
            picker.pick()

        self.assertEqual(raised.exception.code, "folder_picker_timeout")

    def test_service_shutdown_cancellation_is_a_safe_empty_selection(self):
        cancel_event = threading.Event()
        runner = mock.Mock()
        runner.run_limited.return_value = process_result(
            returncode=130,
            cancelled=True,
        )
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda _name: "/usr/bin/osascript",
            cancel_event=cancel_event,
        )

        selection = picker.pick()

        self.assertTrue(selection.cancelled)
        self.assertIsNone(selection.path)
        self.assertIs(
            runner.run_limited.call_args.kwargs["cancel_event"],
            cancel_event,
        )

    def test_already_stopped_service_does_not_launch_a_picker(self):
        cancel_event = threading.Event()
        cancel_event.set()
        runner = mock.Mock()
        picker = FolderPicker(
            runner,
            platform="darwin",
            which=lambda _name: "/usr/bin/osascript",
            cancel_event=cancel_event,
        )

        selection = picker.pick()

        self.assertTrue(selection.cancelled)
        runner.run_limited.assert_not_called()


if __name__ == "__main__":
    unittest.main()
