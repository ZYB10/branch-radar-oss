from __future__ import annotations

import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .config import NotificationSettings
from .models import CheckResult
from .process import CommandRunner
from .security import redact_sensitive_text


@dataclass(frozen=True)
class NotificationOutcome:
    status: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"status": self.status, "detail": self.detail}


class DesktopNotifier:
    """Best-effort native notifications without adding a runtime dependency."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        platform: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ):
        self.runner = runner or CommandRunner()
        self.platform = platform or sys.platform
        self.which = which

    def status(self) -> dict[str, Any]:
        binary = self._binary()
        return {
            "supported": binary is not None,
            "platform": self._platform_name(),
            "provider": "osascript"
            if self.platform.startswith("darwin")
            else ("notify-send" if self.platform.startswith("linux") else None),
        }

    def notify_check_run(
        self,
        results: Sequence[CheckResult],
        _check_run: dict[str, Any] | None,
        settings: NotificationSettings,
        *,
        now: datetime | None = None,
    ) -> NotificationOutcome:
        changed = [result for result in results if result.status == "changed"]
        if not settings.enabled or not changed:
            return NotificationOutcome("skipped")
        if settings.quiet_hours_enabled and is_quiet_time(
            settings.quiet_hours_start,
            settings.quiet_hours_end,
            now=now,
        ):
            return NotificationOutcome("quiet_hours")

        binary = self._binary()
        if binary is None:
            return NotificationOutcome("unsupported")
        title, body = _notification_copy(changed, settings.locale)
        if self.platform.startswith("darwin"):
            script = (
                "on run argv\n"
                "display notification (item 2 of argv) "
                "with title (item 1 of argv)\n"
                "end run"
            )
            argv = [binary, "-e", script, "--", title, body]
        else:
            argv = [
                binary,
                "--app-name=Branch Radar",
                "--urgency=normal",
                title,
                body,
            ]
        try:
            result = self.runner.run(argv, timeout=10)
        except OSError as exc:
            return NotificationOutcome(
                "error", redact_sensitive_text(str(exc))[:300]
            )
        if result.ok:
            return NotificationOutcome("sent")
        detail = redact_sensitive_text(
            (result.stderr or result.stdout).strip() or "notification command failed"
        )[:300]
        return NotificationOutcome("error", detail)

    def _binary(self) -> str | None:
        if self.platform.startswith("darwin"):
            return self.which("osascript")
        if self.platform.startswith("linux"):
            return self.which("notify-send")
        return None

    def _platform_name(self) -> str:
        if self.platform.startswith("darwin"):
            return "macOS"
        if self.platform.startswith("linux"):
            return "Linux"
        return self.platform


def is_quiet_time(
    start: str,
    end: str,
    *,
    now: datetime | None = None,
) -> bool:
    current = now or datetime.now().astimezone()
    minute = current.hour * 60 + current.minute
    start_minute = _clock_minutes(start)
    end_minute = _clock_minutes(end)
    if start_minute == end_minute:
        return True
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def _clock_minutes(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def _notification_copy(
    changed: Sequence[CheckResult],
    locale: str,
) -> tuple[str, str]:
    repositories = sorted({result.repository for result in changed})
    if locale == "en":
        title = "Branch Radar · %d change%s" % (
            len(changed), "" if len(changed) == 1 else "s"
        )
        scope = "%d repositor%s" % (
            len(repositories), "y" if len(repositories) == 1 else "ies"
        )
        detail = "Open Branch Radar to review this check."
    else:
        title = "Branch Radar · 发现 %d 项变化" % len(changed)
        scope = "%d 个仓库" % len(repositories)
        detail = "打开 Branch Radar 查看本次检查。"
    return title[:120], (scope + " · " + detail)[:500]
