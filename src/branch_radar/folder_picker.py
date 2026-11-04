from __future__ import annotations

import shutil
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .process import CommandRunner, ProcessResult

FOLDER_PICKER_TIMEOUT_SECONDS = 300
FOLDER_PICKER_OUTPUT_LIMIT_BYTES = 8192


class FolderPickerError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FolderPickResult:
    path: str | None = None
    cancelled: bool = False


class FolderPicker:
    """Open a native directory chooser through a bounded shell-free process."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        platform: str | None = None,
        which: Callable[[str], str | None] = shutil.which,
        env: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        timeout_seconds: int = FOLDER_PICKER_TIMEOUT_SECONDS,
    ) -> None:
        self.runner = runner or CommandRunner()
        self.platform = platform
        self.which = which
        self.env = env
        self.cancel_event = cancel_event
        self.timeout_seconds = timeout_seconds

    def pick(self) -> FolderPickResult:
        if self.cancel_event is not None and self.cancel_event.is_set():
            return FolderPickResult(cancelled=True)
        platform_name = self.platform or _runtime_platform()
        command = self._command(platform_name)
        result = self.runner.run_limited(
            command,
            timeout=self.timeout_seconds,
            cancel_event=self.cancel_event,
            stdout_limit_bytes=FOLDER_PICKER_OUTPUT_LIMIT_BYTES,
            stderr_limit_bytes=FOLDER_PICKER_OUTPUT_LIMIT_BYTES,
        )
        if result.cancelled:
            return FolderPickResult(cancelled=True)
        if result.timed_out:
            raise FolderPickerError(
                "目录选择已超时，请重新打开后选择目录",
                code="folder_picker_timeout",
            )
        if _was_cancelled(platform_name, result):
            return FolderPickResult(cancelled=True)
        if not result.ok or result.stderr_truncated:
            raise FolderPickerError(
                "无法打开系统目录选择器，请确认桌面会话可用后重试",
                code="folder_picker_failed",
            )
        selected = result.stdout.rstrip("\r\n")
        if not selected:
            raise FolderPickerError(
                "系统目录选择器没有返回有效目录，请重试",
                code="folder_picker_failed",
            )
        return FolderPickResult(path=selected)

    def _command(self, platform_name: str) -> Sequence[str]:
        if platform_name == "darwin":
            binary = self.which("osascript")
            if binary:
                return [
                    binary,
                    "-e",
                    'POSIX path of (choose folder with prompt "选择要扫描的项目目录")',
                ]
        elif platform_name.startswith("linux"):
            environment = self.env if self.env is not None else _runtime_environment()
            if not (
                environment.get("DISPLAY") or environment.get("WAYLAND_DISPLAY")
            ):
                raise FolderPickerError(
                    "当前 Linux 会话没有可用的图形界面；请手动输入目录路径",
                    code="folder_picker_unavailable",
                )
            zenity = self.which("zenity")
            if zenity:
                return [
                    zenity,
                    "--file-selection",
                    "--directory",
                    "--title=选择要扫描的项目目录",
                ]
            kdialog = self.which("kdialog")
            if kdialog:
                return [
                    kdialog,
                    "--getexistingdirectory",
                    str(Path.home()),
                    "--title",
                    "选择要扫描的项目目录",
                ]
        raise FolderPickerError(
            "当前系统未找到可用的目录选择器；请手动输入目录路径",
            code="folder_picker_unavailable",
        )


def _runtime_platform() -> str:
    """Keep supported platform branches visible to cross-platform analysis."""
    return sys.platform


def _runtime_environment() -> Mapping[str, str]:
    # Imported lazily so tests can provide an isolated environment mapping.
    import os

    return os.environ


def _was_cancelled(platform_name: str, result: ProcessResult) -> bool:
    if platform_name.startswith("linux"):
        return (
            result.returncode == 1
            and not result.timed_out
            and not result.stderr.strip()
        )
    if platform_name == "darwin" and result.returncode != 0:
        error = result.stderr.casefold()
        return "-128" in error or "user canceled" in error
    return False
