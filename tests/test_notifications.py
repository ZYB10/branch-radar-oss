import unittest
from datetime import datetime

from branch_radar.config import NotificationSettings
from branch_radar.models import CheckResult
from branch_radar.notifications import DesktopNotifier, is_quiet_time
from branch_radar.process import ProcessResult


class FakeRunner:
    def __init__(self, *, returncode=0):
        self.returncode = returncode
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return ProcessResult(
            list(argv), self.returncode, "", "failed" if self.returncode else "", 0.01
        )


class QuietHoursTests(unittest.TestCase):
    def test_overnight_window_wraps_midnight(self):
        self.assertTrue(
            is_quiet_time("22:00", "08:00", now=datetime(2026, 8, 27, 23, 0))
        )
        self.assertTrue(
            is_quiet_time("22:00", "08:00", now=datetime(2026, 8, 27, 7, 59))
        )
        self.assertFalse(
            is_quiet_time("22:00", "08:00", now=datetime(2026, 8, 27, 8, 0))
        )

    def test_same_start_and_end_means_the_whole_day(self):
        self.assertTrue(
            is_quiet_time("09:00", "09:00", now=datetime(2026, 8, 27, 15, 0))
        )


class DesktopNotifierTests(unittest.TestCase):
    def changed(self):
        return [CheckResult("radar", "main", "changed", event_id=1)]

    def test_macos_uses_argv_for_untrusted_notification_copy(self):
        runner = FakeRunner()
        notifier = DesktopNotifier(
            runner=runner,
            platform="darwin",
            which=lambda name: "/usr/bin/osascript" if name == "osascript" else None,
        )
        outcome = notifier.notify_check_run(
            self.changed(),
            {"summary": {"title": 'Changed "quoted" content'}},
            NotificationSettings(enabled=True),
        )
        self.assertEqual(outcome.status, "sent")
        argv = runner.calls[0][0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertEqual(argv[-2], "Branch Radar · 发现 1 项变化")
        self.assertIn("打开 Branch Radar 查看本次检查", argv[-1])
        self.assertNotIn('Changed "quoted" content', argv[2])
        self.assertNotIn('Changed "quoted" content', argv[-1])

    def test_no_change_or_disabled_setting_is_silent(self):
        runner = FakeRunner()
        notifier = DesktopNotifier(
            runner=runner,
            platform="linux",
            which=lambda _name: "/usr/bin/notify-send",
        )
        unchanged = [CheckResult("radar", "main", "unchanged")]
        self.assertEqual(
            notifier.notify_check_run(
                unchanged, None, NotificationSettings(enabled=True)
            ).status,
            "skipped",
        )
        self.assertEqual(
            notifier.notify_check_run(
                self.changed(), None, NotificationSettings(enabled=False)
            ).status,
            "skipped",
        )
        self.assertEqual(runner.calls, [])

    def test_quiet_hours_suppress_the_command(self):
        runner = FakeRunner()
        notifier = DesktopNotifier(
            runner=runner,
            platform="linux",
            which=lambda _name: "/usr/bin/notify-send",
        )
        settings = NotificationSettings(
            enabled=True,
            quiet_hours_enabled=True,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
        )
        outcome = notifier.notify_check_run(
            self.changed(), None, settings, now=datetime(2026, 8, 27, 23, 0)
        )
        self.assertEqual(outcome.status, "quiet_hours")
        self.assertEqual(runner.calls, [])

    def test_english_copy_and_missing_provider_are_bounded(self):
        notifier = DesktopNotifier(
            runner=FakeRunner(), platform="win32", which=lambda _name: None
        )
        outcome = notifier.notify_check_run(
            self.changed(), None, NotificationSettings(enabled=True, locale="en")
        )
        self.assertEqual(outcome.status, "unsupported")
        self.assertFalse(notifier.status()["supported"])


if __name__ == "__main__":
    unittest.main()
