from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any

from .paths import AppPaths


def _default_scan_roots() -> list[str]:
    """Keep first-run discovery explicit instead of guessing a folder layout."""
    return []


@dataclass
class CodexSettings:
    enabled: bool = True
    binary: str | None = None
    timeout_seconds: int = 240
    max_input_bytes: int = 300_000


@dataclass
class LarkSettings:
    enabled: bool = True
    binary: str | None = None
    identity: str = "user"
    document: str | None = None
    document_title: str = "Branch Radar · 项目分支动态"
    timeout_seconds: int = 60


@dataclass
class NotificationSettings:
    """Local notification preferences for scheduled background checks."""

    enabled: bool = False
    quiet_hours_enabled: bool = False
    quiet_hours_start: str = "22:00"
    quiet_hours_end: str = "08:00"
    locale: str = "zh-CN"


@dataclass
class AppConfig:
    version: int = 1
    scan_roots: list[str] = field(default_factory=_default_scan_roots)
    scan_max_depth: int = 5
    poll_interval_seconds: int = 900
    # An always-on service accumulates events, check runs and deliveries with no
    # natural end. 0 keeps everything and leaves pruning entirely manual.
    retention_days: int = 180
    fetch_timeout_seconds: int = 120
    initial_fetch_timeout_seconds: int = 600
    patch_excerpt_bytes: int = 80_000
    codex: CodexSettings = field(default_factory=CodexSettings)
    lark: LarkSettings = field(default_factory=LarkSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是 JSON object")
        codex_raw = _mapping(raw.get("codex", {}), "codex")
        lark_raw = _mapping(raw.get("lark", {}), "lark")
        notifications_raw = _mapping(
            raw.get("notifications", {}), "notifications"
        )
        scan_roots = raw.get("scan_roots", _default_scan_roots())
        if not isinstance(scan_roots, list) or not all(
            isinstance(value, str) and value.strip() for value in scan_roots
        ):
            raise ValueError("配置项 scan_roots 必须是非空路径字符串组成的数组")
        return cls(
            version=_integer(raw.get("version", 1), "version", 1, 100),
            scan_roots=list(scan_roots),
            scan_max_depth=_integer(
                raw.get("scan_max_depth", 5), "scan_max_depth", 0, 20
            ),
            poll_interval_seconds=_integer(
                raw.get("poll_interval_seconds", 900),
                "poll_interval_seconds", 10, 86_400,
            ),
            retention_days=_integer(
                raw.get("retention_days", 180), "retention_days", 0, 3650
            ),
            fetch_timeout_seconds=_integer(
                raw.get("fetch_timeout_seconds", 120),
                "fetch_timeout_seconds", 1, 3600,
            ),
            initial_fetch_timeout_seconds=_integer(
                raw.get("initial_fetch_timeout_seconds", 600),
                "initial_fetch_timeout_seconds", 1, 3600,
            ),
            patch_excerpt_bytes=_integer(
                raw.get("patch_excerpt_bytes", 80_000),
                "patch_excerpt_bytes", 1024, 1_000_000,
            ),
            codex=CodexSettings(
                enabled=_boolean(codex_raw.get("enabled", True), "codex.enabled"),
                binary=_optional_string(codex_raw.get("binary"), "codex.binary"),
                timeout_seconds=_integer(
                    codex_raw.get("timeout_seconds", 240),
                    "codex.timeout_seconds", 1, 3600,
                ),
                max_input_bytes=_integer(
                    codex_raw.get("max_input_bytes", 300_000),
                    "codex.max_input_bytes", 16_384, 2_000_000,
                ),
            ),
            lark=LarkSettings(
                enabled=_boolean(lark_raw.get("enabled", True), "lark.enabled"),
                binary=_optional_string(lark_raw.get("binary"), "lark.binary"),
                identity=_identity(lark_raw.get("identity", "user")),
                document=_optional_string(lark_raw.get("document"), "lark.document"),
                document_title=_required_string(
                    lark_raw.get("document_title", "Branch Radar · 项目分支动态"),
                    "lark.document_title",
                ),
                timeout_seconds=_integer(
                    lark_raw.get("timeout_seconds", 60),
                    "lark.timeout_seconds", 1, 3600,
                ),
            ),
            notifications=NotificationSettings(
                enabled=_boolean(
                    notifications_raw.get("enabled", False),
                    "notifications.enabled",
                ),
                quiet_hours_enabled=_boolean(
                    notifications_raw.get("quiet_hours_enabled", False),
                    "notifications.quiet_hours_enabled",
                ),
                quiet_hours_start=_clock_time(
                    notifications_raw.get("quiet_hours_start", "22:00"),
                    "notifications.quiet_hours_start",
                ),
                quiet_hours_end=_clock_time(
                    notifications_raw.get("quiet_hours_end", "08:00"),
                    "notifications.quiet_hours_end",
                ),
                locale=_locale(
                    notifications_raw.get("locale", "zh-CN"),
                    "notifications.locale",
                ),
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfigStore:
    def __init__(self, paths: AppPaths):
        self.paths = paths

    def load(self) -> AppConfig:
        if not self.paths.config.exists():
            return AppConfig()
        with self.paths.config.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            raise ValueError("配置文件顶层必须是 JSON object")
        return AppConfig.from_dict(raw)

    def signature(self) -> tuple[int, int] | None:
        """Cheaply detect external config replacements without reparsing JSON."""
        try:
            stat = self.paths.config.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def save(self, config: AppConfig) -> None:
        self.paths.ensure()
        payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(
            prefix="config-", suffix=".tmp", dir=str(self.paths.home)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.paths.config)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("配置项 %s 必须是 JSON object" % name)
    return value


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("配置项 %s 必须是整数" % name)
    if not minimum <= value <= maximum:
        raise ValueError(
            "配置项 %s 必须在 %s 到 %s 之间" % (name, minimum, maximum)
        )
    return int(value)


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError("配置项 %s 必须是 true 或 false" % name)
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_string(value, name)


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("配置项 %s 必须是非空字符串" % name)
    return value


def _identity(value: Any) -> str:
    if value != "user":
        raise ValueError("配置项 lark.identity 当前只支持 user")
    return "user"


def _clock_time(value: Any, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise ValueError("配置项 %s 必须是 HH:MM 格式" % name)
    return value


def _locale(value: Any, name: str) -> str:
    if not isinstance(value, str) or value not in {"zh-CN", "en"}:
        raise ValueError("配置项 %s 仅支持 zh-CN 或 en" % name)
    return value
