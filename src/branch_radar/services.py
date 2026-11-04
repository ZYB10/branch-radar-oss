from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import os
import platform
import re
import shutil
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from .config import AppConfig, ConfigStore
from .database import Database
from .git import (
    GitClient,
    GitCloneCancelled,
    GitError,
    is_safe_remote_url,
    is_valid_branch_name,
)
from .integrations.codex_cli import CodexCli
from .integrations.lark_cli import LarkCli, LarkError
from .locking import LockUnavailable, exclusive_lock
from .models import CheckResult, RepositoryCandidate
from .monitor import BranchMonitor
from .notifications import DesktopNotifier
from .paths import AppPaths
from .publishers import LarkDocumentTarget, PublicationOutcome, Publisher
from .remote import parse_github_clone_locator, parse_repository_locator
from .scanner import RepositoryScanner
from .security import redact_sensitive_text
from .service import ServiceInstallError, ServiceSpec, build_manager
from .sources import GitFactSource, Watch

MAX_SCAN_ROOTS = 16
MAX_SCAN_ROOT_LENGTH = 4096
MAX_CLONE_DIRECTORY_LENGTH = 200

_CLONE_PROGRESS = re.compile(
    r"(?P<operation>Receiving objects|Resolving deltas|Counting objects|"
    r"Compressing objects|Updating files):\s+(?P<percent>\d{1,3})%\s+"
    r"\((?P<current>\d+)/(?P<total>\d+)\)"
)
_CLONE_PROGRESS_LABELS = {
    "Receiving objects": (
        "download", "正在下载 Git 对象", "Git 正在接收仓库对象。", False,
    ),
    "Resolving deltas": (
        "verify", "正在整理仓库历史", "Git 正在解析对象差异。", False,
    ),
    "Counting objects": (
        "connect", "正在读取远程对象", "远程正在统计需要传输的对象。", True,
    ),
    "Compressing objects": (
        "connect", "远程正在准备数据", "远程正在压缩待传输对象。", True,
    ),
    "Updating files": (
        "verify", "正在生成工作区", "Git 正在写入当前分支的文件。", False,
    ),
}
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *("COM%d" % index for index in range(1, 10)),
    *("LPT%d" % index for index in range(1, 10)),
}
_RENAME_NOREPLACE = 1
_RENAME_EXCL = 0x00000004
_LINUX_RENAMEAT2_SYSCALLS = {
    "x86_64": 316,
    "amd64": 316,
    "i386": 353,
    "i686": 353,
    "aarch64": 276,
    "arm64": 276,
    "armv6l": 382,
    "armv7l": 382,
    "ppc64": 357,
    "ppc64le": 357,
    "s390x": 347,
    "riscv64": 276,
}


# Retention is housekeeping, not a check: running it once a day keeps the
# tables bounded without adding work to every polling cycle.
RETENTION_CHECK_INTERVAL = timedelta(hours=24)


def _absolute_scan_root(value: str) -> Path:
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.abspath(str(expanded)))


def _portable_scan_root(path: Path) -> str:
    home = Path(os.path.abspath(str(Path.home())))
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    if not relative.parts:
        return "~"
    return "~/" + relative.as_posix()


def display_scan_roots(values: Sequence[str]) -> list[str]:
    """Return stable, HOME-relative labels without changing configured roots."""
    return [_portable_scan_root(_absolute_scan_root(value)) for value in values]


def _validate_clone_directory_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ServiceError("仓库目录名称无效", code="invalid_clone_directory")
    if (
        not value
        or value != value.strip()
        or len(value) > MAX_CLONE_DIRECTORY_LENGTH
        or value in {".", ".."}
        or value.casefold() == ".git"
        or (
            value.startswith(".branch-radar-clone-")
            and value.endswith(".partial")
        )
        or value.endswith((".", " "))
        or any(ord(character) < 0x20 for character in value)
        or any(character in '<>:"/\\|?*\0' for character in value)
    ):
        raise ServiceError("仓库目录名称无效", code="invalid_clone_directory")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ServiceError("仓库目录名称无效", code="invalid_clone_directory")
    return value


def _same_github_repository(left: str | None, right: str) -> bool:
    return bool(left and left.casefold() == right.casefold())


def _clone_progress_reporter(
    report: Callable[[dict[str, Any]], None],
) -> Callable[[str, str], None]:
    buffer = ""
    last_progress: tuple[str, int, int] | None = None
    lock = threading.Lock()

    def receive(stream: str, chunk: str) -> None:
        nonlocal buffer, last_progress
        if stream != "stderr" or not chunk:
            return
        update: dict[str, Any] | None = None
        with lock:
            buffer = (buffer + chunk)[-8192:]
            matches = list(_CLONE_PROGRESS.finditer(buffer))
            if not matches:
                return
            match = matches[-1]
            operation = match.group("operation")
            current = max(0, int(match.group("current")))
            total = max(1, int(match.group("total")))
            key = (operation, current, total)
            if key == last_progress:
                return
            last_progress = key
            phase, label, detail, indeterminate = _CLONE_PROGRESS_LABELS[operation]
            update = {
                "phase": phase,
                "label": label,
                "detail": detail,
                "indeterminate": indeterminate,
            }
            if not indeterminate:
                update.update({
                    "current": min(current, total),
                    "total": total,
                    "percent": max(0, min(int(match.group("percent")), 100)),
                })
        if update is not None:
            report(update)

    return receive


class _CloneRootGuard:
    """Pin and repeatedly verify the user-selected clone root."""

    def __init__(
        self,
        path: Path,
        expected_identity: tuple[int, int] | None = None,
    ):
        self.path = path
        self.identity: tuple[int, int] | None = (
            expected_identity or self._path_identity()
        )
        self.fd: int | None = None

    def __enter__(self) -> _CloneRootGuard:
        self.check()
        if platform.system() != "Windows":
            if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
                raise ServiceError(
                    "当前平台无法安全固定仓库保存目录",
                    code="clone_root_changed",
                )
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                self.fd = os.open(self.path, flags)
                self.check()
            except Exception:
                self.close()
                raise
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def check(self) -> None:
        if self.identity is None or self._path_identity() != self.identity:
            raise ServiceError(
                "仓库保存目录在克隆期间发生变化",
                code="clone_root_changed",
            )
        if self.fd is not None:
            descriptor_stat = os.fstat(self.fd)
            descriptor_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if (
                not stat.S_ISDIR(descriptor_stat.st_mode)
                or descriptor_identity != self.identity
            ):
                raise ServiceError(
                    "仓库保存目录在克隆期间发生变化",
                    code="clone_root_changed",
                )

    def _path_identity(self) -> tuple[int, int]:
        try:
            path_stat = self.path.lstat()
        except OSError as exc:
            raise ServiceError(
                "仓库保存目录在克隆期间不可用",
                code="clone_root_changed",
            ) from exc
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            raise ServiceError(
                "仓库保存目录在克隆期间发生变化",
                code="clone_root_changed",
            )
        return path_stat.st_dev, path_stat.st_ino


def _remove_tree_at(parent_fd: int, name: str, expected_device: int) -> None:
    try:
        entry_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if entry_stat.st_dev != expected_device:
        raise OSError(errno.EXDEV, "refusing to cross a filesystem boundary")
    if not stat.S_ISDIR(entry_stat.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    child_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened_stat = os.fstat(child_fd)
        if (
            opened_stat.st_dev != entry_stat.st_dev
            or opened_stat.st_ino != entry_stat.st_ino
        ):
            raise OSError(errno.ESTALE, "directory changed during cleanup")
        for child_name in os.listdir(child_fd):
            _remove_tree_at(child_fd, child_name, expected_device)
    finally:
        os.close(child_fd)
    os.rmdir(name, dir_fd=parent_fd)


def _cleanup_clone_staging(path: Path, root: _CloneRootGuard) -> bool:
    """Remove only this attempt's staging path without following a changed root."""
    if (
        path.parent != root.path
        or not path.name.startswith(".branch-radar-clone-")
        or not path.name.endswith(".partial")
    ):
        return False
    try:
        root.check()
        if root.fd is not None:
            assert root.identity is not None
            _remove_tree_at(root.fd, path.name, root.identity[0])
        elif path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        root.check()
    except (OSError, ServiceError):
        return False
    if root.fd is not None:
        try:
            os.stat(path.name, dir_fd=root.fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False
    return not os.path.lexists(str(path))


def _atomic_rename_no_replace(
    source: Path, target: Path, *, dir_fd: int | None
) -> None:
    """Atomically rename a directory while refusing an existing target."""
    if source.parent != target.parent or "/" in source.name or "/" in target.name:
        raise OSError(errno.EINVAL, "clone rename paths must share one parent")
    system = platform.system()
    if system == "Windows":
        # os.rename maps to a non-replacing MoveFile operation on Windows.
        os.rename(source, target)
        return
    if system not in {"Darwin", "Linux"}:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unsupported on this platform",
        )
    if dir_fd is None:
        raise OSError(errno.ENOTSUP, "a pinned clone root is required")

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source.name)
    target_bytes = os.fsencode(target.name)
    if system == "Darwin":
        function = getattr(libc, "renameatx_np", None)
        if function is None:
            raise OSError(errno.ENOTSUP, "renameatx_np is unavailable")
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = function(
            dir_fd, source_bytes, dir_fd, target_bytes, _RENAME_EXCL
        )
    else:
        function = getattr(libc, "renameat2", None)
        if function is not None:
            function.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            function.restype = ctypes.c_int
            ctypes.set_errno(0)
            result = function(
                dir_fd,
                source_bytes,
                dir_fd,
                target_bytes,
                _RENAME_NOREPLACE,
            )
        else:
            syscall_number = _LINUX_RENAMEAT2_SYSCALLS.get(
                platform.machine().lower()
            )
            syscall = getattr(libc, "syscall", None)
            if syscall_number is None or syscall is None:
                raise OSError(errno.ENOTSUP, "renameat2 is unavailable")
            syscall.restype = ctypes.c_long
            ctypes.set_errno(0)
            result = syscall(
                ctypes.c_long(syscall_number),
                ctypes.c_int(dir_fd),
                ctypes.c_char_p(source_bytes),
                ctypes.c_int(dir_fd),
                ctypes.c_char_p(target_bytes),
                ctypes.c_uint(_RENAME_NOREPLACE),
            )
    if result != 0:
        error_number = ctypes.get_errno() or errno.EIO
        raise OSError(error_number, os.strerror(error_number), str(target))


def _bounded_run_summary(value: dict[str, Any]) -> dict[str, Any]:
    def text(key: str, limit: int) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            return ""
        return redact_sensitive_text(item).strip()[:limit]

    def strings(key: str) -> list[str]:
        items = value.get(key)
        if not isinstance(items, list):
            return []
        return [
            redact_sensitive_text(item).strip()[:500]
            for item in items[:5]
            if isinstance(item, str) and item.strip()
        ]

    source = value.get("source")
    return {
        "title": text("title", 200) or "本轮检查完成",
        "overview": text("overview", 1200),
        "themes": strings("themes"),
        "priorities": strings("priorities"),
        "connections": strings("connections"),
        "completeness_note": text("completeness_note", 1000),
        "source": source if source in {"rules", "codex"} else "rules",
    }


_RUN_CATEGORY_LABELS = {
    "api": "API",
    "database": "数据库",
    "deployment": "部署",
    "dependency": "依赖",
    "config": "配置",
    "feature": "功能",
    "fix": "修复",
    "refactor": "重构",
    "test": "测试",
    "docs": "文档",
    "other": "其他",
}
_RUN_CATEGORY_RISK = {
    "database": 0,
    "deployment": 1,
    "dependency": 2,
    "api": 3,
    "config": 4,
}


def _sanitize_check_run_error(
    message: str, watches: Sequence[dict[str, Any]], paths: AppPaths
) -> str:
    value = _sanitize_check_run_value(message or "检查失败", watches, paths)
    return str(value)[:2000]


def _sanitize_check_run_value(
    value: Any, watches: Sequence[dict[str, Any]], paths: AppPaths
) -> Any:
    private_paths = {
        str(path)
        for path in (
            paths.home,
            paths.config,
            paths.database,
            paths.logs,
            paths.cache,
            paths.analysis_workspace,
        )
    }
    private_paths.update(
        str(watch.get("local_path"))
        for watch in watches
        if watch.get("local_path")
    )

    def sanitize(item: Any) -> Any:
        if isinstance(item, str):
            text = redact_sensitive_text(item)
            for private_path in sorted(private_paths, key=len, reverse=True):
                if private_path:
                    text = text.replace(private_path, "[repository]")
            return text
        if isinstance(item, list):
            return [sanitize(child) for child in item]
        if isinstance(item, dict):
            return {str(key): sanitize(child) for key, child in item.items()}
        return item

    return sanitize(value)


def _sanitize_check_run_summary(
    value: dict[str, Any], watches: Sequence[dict[str, Any]], paths: AppPaths
) -> dict[str, Any]:
    sanitized = _sanitize_check_run_value(value, watches, paths)
    return _bounded_run_summary(sanitized if isinstance(sanitized, dict) else {})


def _safe_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, min(int(value or 0), 2_147_483_647))
    except (TypeError, ValueError, OverflowError):
        return 0


def _check_run_ai_priority(event: dict[str, Any]) -> tuple[int, int]:
    raw_facts = event.get("facts")
    facts: dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
    kind = event.get("kind")
    if kind in {"force_push", "history_gap"}:
        rank = -4
    elif any(
        isinstance(signal, str) and "breaking" in signal.casefold()
        for signal in (facts.get("signals") or [])
    ):
        rank = -3
    else:
        category_ranks = [
            _RUN_CATEGORY_RISK.get(category, 20)
            for category in (facts.get("categories") or [])
            if isinstance(category, str)
        ]
        rank = min(category_ranks, default=20)
    return rank, _safe_count(event.get("id"))


def _deterministic_check_run_summary(
    results: Sequence[CheckResult], events: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    changed = len(events)
    repositories = {
        str(event.get("repository")) for event in events if event.get("repository")
    }
    blocked = sum(result.blocked for result in results)
    errors = sum(result.status == "error" and not result.blocked for result in results)
    category_counts: dict[str, int] = {}
    category_repositories: dict[str, set[str]] = {}
    incomplete = 0
    priorities: list[str] = []
    priority_kinds = {"force_push", "history_gap", "deleted", "recreated"}
    for event in sorted(events, key=_check_run_ai_priority):
        raw_facts = event.get("facts")
        facts: dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
        if not bool(facts.get("analysis_complete", True)):
            incomplete += 1
        repository = str(event.get("repository") or "")
        for category in facts.get("categories") or []:
            if not isinstance(category, str) or not category:
                continue
            category_counts[category] = category_counts.get(category, 0) + 1
            category_repositories.setdefault(category, set()).add(repository)
        raw_summary = event.get("summary")
        summary: dict[str, Any] = (
            raw_summary if isinstance(raw_summary, dict) else {}
        )
        title = str(summary.get("title") or "")
        signals = [
            str(signal).lower() for signal in (facts.get("signals") or [])
            if isinstance(signal, str)
        ]
        risky_categories = {
            category
            for category in (facts.get("categories") or [])
            if category in _RUN_CATEGORY_RISK
        }
        if (
            event.get("kind") in priority_kinds
            or any("breaking" in signal for signal in signals)
            or risky_categories
        ) and title:
            priorities.append("%s / %s：%s" % (
                event.get("repository") or "仓库",
                event.get("branch") or "分支",
                title,
            ))
    connections = [
        "%s同时出现在 %d 个仓库的变化中" % (
            _RUN_CATEGORY_LABELS.get(category, category),
            len(repositories_for_category),
        )
        for category, repositories_for_category in category_repositories.items()
        if len(repositories_for_category) >= 2
    ]
    completeness_parts = []
    if blocked:
        completeness_parts.append(
            "本轮有 %d 个分支未能观察，结果不是完整全景。" % blocked
        )
    if incomplete:
        completeness_parts.append(
            "本轮有 %d 条变化无法完整重建证据。" % incomplete
        )
    if errors:
        completeness_parts.append("另有 %d 个分支检查失败。" % errors)
    if changed:
        title = "本轮发现 %d 个变化" % changed
        overview = "%d 个仓库产生 %d 个变化；本轮共检查 %d 个分支。" % (
            len(repositories), changed, len(results)
        )
    else:
        title = "本轮检查完成"
        overview = "本轮检查了 %d 个分支，没有发现新的可见变化。" % len(results)
    return _bounded_run_summary({
        "title": title,
        "overview": overview,
        "themes": [
            _RUN_CATEGORY_LABELS.get(category, category)
            for category in sorted(
                category_counts,
                key=lambda item: (
                    _RUN_CATEGORY_RISK.get(item, 20),
                    -category_counts[item],
                    item,
                ),
            )
        ],
        "priorities": priorities,
        "connections": connections,
        "completeness_note": " ".join(completeness_parts),
        "source": "rules",
    })


def _check_run_ai_event(event: dict[str, Any]) -> dict[str, Any]:
    raw_facts = event.get("facts")
    facts: dict[str, Any] = raw_facts if isinstance(raw_facts, dict) else {}
    raw_summary = event.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    raw_diff = facts.get("diff")
    diff: dict[str, Any] = raw_diff if isinstance(raw_diff, dict) else {}
    raw_categories = facts.get("categories")
    categories: list[Any] = raw_categories if isinstance(raw_categories, list) else []
    raw_signals = facts.get("signals")
    signals: list[Any] = raw_signals if isinstance(raw_signals, list) else []
    return {
        "event_id": event.get("id"),
        "repository": redact_sensitive_text(str(event.get("repository") or ""))[:200],
        "branch": redact_sensitive_text(str(event.get("branch") or ""))[:500],
        "kind": str(event.get("kind") or "")[:50],
        "analysis_complete": bool(facts.get("analysis_complete", True)),
        "summary": {
            "title": redact_sensitive_text(str(summary.get("title") or ""))[:300],
            "overview": redact_sensitive_text(str(summary.get("overview") or ""))[:1200],
            "categories": [
                str(item)[:40] for item in categories[:12]
                if isinstance(item, str) and item
            ],
            "signals": [
                redact_sensitive_text(str(item))[:300]
                for item in signals[:12]
                if isinstance(item, str) and item
            ],
        },
        "stats": {
            "commits": _safe_count(facts.get("commit_count")),
            "files": _safe_count(diff.get("files_changed")),
            "insertions": _safe_count(diff.get("insertions")),
            "deletions": _safe_count(diff.get("deletions")),
        },
    }


def validate_scan_roots(values: Sequence[str]) -> list[str]:
    """Validate and normalize user-selected discovery roots for persistence."""
    if len(values) > MAX_SCAN_ROOTS:
        raise ServiceError(
            "扫描目录最多设置 %d 个" % MAX_SCAN_ROOTS,
            code="invalid_scan_roots",
        )

    roots: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ServiceError("扫描目录格式无效", code="invalid_scan_roots")
        if not value.strip() or len(value) > MAX_SCAN_ROOT_LENGTH or "\0" in value:
            raise ServiceError("扫描目录格式无效", code="invalid_scan_roots")
        path = _absolute_scan_root(value)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ServiceError(
                "扫描目录不存在或无法读取：%s" % _portable_scan_root(path),
                code="invalid_scan_roots",
            ) from None
        if resolved == Path(resolved.anchor):
            raise ServiceError(
                "不能扫描整个文件系统，请选择更具体的目录",
                code="invalid_scan_roots",
            )
        if not resolved.is_dir():
            raise ServiceError(
                "扫描路径不是目录：%s" % _portable_scan_root(path),
                code="invalid_scan_roots",
            )
        key = os.path.normcase(str(resolved))
        if key in seen:
            continue
        seen.add(key)
        roots.append(_portable_scan_root(path))
    return roots


class ServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str = "service_error"):
        super().__init__(message)
        self.code = code


class ServiceBusy(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, code="operation_in_progress")


def _validated_clock_time(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise ServiceError(
            "勿扰时间必须使用 HH:MM 格式",
            code="invalid_quiet_hours",
        )
    return value


def _next_monitoring_run(
    last_attempt_at: Any,
    interval_seconds: int,
    *,
    enabled: bool,
) -> str | None:
    if not enabled:
        return None
    current = datetime.now(timezone.utc)
    anchor = current
    if isinstance(last_attempt_at, str):
        try:
            candidate = datetime.fromisoformat(last_attempt_at)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
            anchor = candidate.astimezone(timezone.utc)
        except ValueError:
            pass
    estimate = anchor + timedelta(seconds=interval_seconds)
    if estimate <= current:
        # The platform APIs differ in how they expose the next timer elapse.
        # Once a recorded attempt is stale, show a conservative next interval
        # instead of presenting "now" as if it were an exact schedule.
        estimate = current + timedelta(seconds=interval_seconds)
    return estimate.isoformat()


class RadarService:
    """Business operations shared by the terminal and local Web UI."""

    def __init__(
        self,
        paths: AppPaths,
        store: ConfigStore,
        config: AppConfig,
        database: Database,
        git: GitClient,
    ):
        self.paths = paths
        self.store = store
        self.config = config
        self.database = database
        self.git = git
        self._config_lock = threading.RLock()
        self._config_signature = store.signature()
        self._last_check_run_id: str | None = None

    @classmethod
    def create(cls) -> RadarService:
        paths = AppPaths.discover()
        paths.ensure()
        store = ConfigStore(paths)
        config = store.load()
        database = Database(paths.database)
        database.initialize()
        return cls(paths, store, config, database, GitClient())

    def refresh_config(self) -> AppConfig:
        with self._config_lock:
            signature = self.store.signature()
            if signature != self._config_signature:
                self.config = self.store.load()
                self._config_signature = self.store.signature()
            return self.config

    def update_preferences(
        self,
        *,
        scan_roots: Sequence[str] | None = None,
        codex_enabled: bool | None = None,
        lark_enabled: bool | None = None,
        retention_days: int | None = None,
        poll_interval_seconds: int | None = None,
        notifications_enabled: bool | None = None,
        quiet_hours_enabled: bool | None = None,
        quiet_hours_start: str | None = None,
        quiet_hours_end: str | None = None,
        notification_locale: str | None = None,
    ) -> AppConfig:
        """Merge non-secret onboarding preferences without overwriting live config."""
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                with self._config_lock:
                    latest = self.store.load()
                    if scan_roots is not None:
                        latest.scan_roots = validate_scan_roots(scan_roots)
                    if codex_enabled is not None:
                        latest.codex.enabled = bool(codex_enabled)
                    if lark_enabled is not None:
                        latest.lark.enabled = bool(lark_enabled)
                    if retention_days is not None:
                        if (
                            isinstance(retention_days, bool)
                            or not isinstance(retention_days, int)
                            or not 0 <= retention_days <= 3650
                        ):
                            raise ServiceError(
                                "保留天数必须在 0 到 3650 之间",
                                code="invalid_retention",
                            )
                        latest.retention_days = retention_days
                    if poll_interval_seconds is not None:
                        if (
                            isinstance(poll_interval_seconds, bool)
                            or not isinstance(poll_interval_seconds, int)
                            or poll_interval_seconds not in {300, 900, 1800, 3600}
                        ):
                            raise ServiceError(
                                "检查频率仅支持 5、15、30 或 60 分钟",
                                code="invalid_monitoring_interval",
                            )
                        latest.poll_interval_seconds = poll_interval_seconds
                    if notifications_enabled is not None:
                        latest.notifications.enabled = bool(notifications_enabled)
                    if quiet_hours_enabled is not None:
                        latest.notifications.quiet_hours_enabled = bool(
                            quiet_hours_enabled
                        )
                    if quiet_hours_start is not None:
                        latest.notifications.quiet_hours_start = (
                            _validated_clock_time(quiet_hours_start)
                        )
                    if quiet_hours_end is not None:
                        latest.notifications.quiet_hours_end = (
                            _validated_clock_time(quiet_hours_end)
                        )
                    if notification_locale is not None:
                        if (
                            not isinstance(notification_locale, str)
                            or notification_locale not in {"zh-CN", "en"}
                        ):
                            raise ServiceError(
                                "通知语言仅支持中文或英文",
                                code="invalid_notification_locale",
                            )
                        latest.notifications.locale = notification_locale
                    self.store.save(latest)
                    self.config = latest
                    self._config_signature = self.store.signature()
                    return latest
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def monitoring_status(self) -> dict[str, Any]:
        config = self.refresh_config()
        health = self.database.daemon_health()
        notifier = DesktopNotifier().status()
        try:
            manager = build_manager(
                self.paths,
                ServiceSpec(
                    mode="daemon", interval=config.poll_interval_seconds
                ),
            )
            service_status = manager.status()
            supported = True
            platform_name = str(service_status.get("platform") or "unknown")
            service_error = None
        except (ServiceInstallError, OSError):
            service_status = {"installed": False, "loaded": False}
            supported = False
            platform_name = sys.platform
            service_error = "当前平台暂不支持自动管理后台监控"

        installed = bool(service_status.get("installed"))
        loaded = bool(service_status.get("loaded"))
        return {
            "enabled": installed and loaded,
            "interval_seconds": config.poll_interval_seconds,
            "notifications_enabled": config.notifications.enabled,
            "quiet_hours": {
                "enabled": config.notifications.quiet_hours_enabled,
                "start": config.notifications.quiet_hours_start,
                "end": config.notifications.quiet_hours_end,
            },
            "locale": config.notifications.locale,
            "supported": supported,
            "platform": platform_name,
            "installed": installed,
            "loaded": loaded,
            "notifications_supported": bool(notifier.get("supported")),
            "notification_provider": notifier.get("provider"),
            "last_attempt_at": health.get("last_attempt_at"),
            "last_success_at": health.get("last_success_at"),
            "next_run_at": _next_monitoring_run(
                health.get("last_attempt_at"),
                config.poll_interval_seconds,
                enabled=installed and loaded,
            ),
            "next_run_estimated": installed and loaded,
            "error": service_error,
        }

    def configure_monitoring(
        self,
        *,
        enabled: bool,
        interval_seconds: int,
        notifications_enabled: bool,
        quiet_hours_enabled: bool,
        quiet_hours_start: str,
        quiet_hours_end: str,
        locale: str,
    ) -> dict[str, Any]:
        if not all(
            isinstance(value, bool)
            for value in (enabled, notifications_enabled, quiet_hours_enabled)
        ):
            raise ServiceError(
                "后台监控开关格式无效",
                code="invalid_monitoring_settings",
            )
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, int)
            or interval_seconds not in {300, 900, 1800, 3600}
        ):
            raise ServiceError(
                "检查频率仅支持 5、15、30 或 60 分钟",
                code="invalid_monitoring_interval",
            )
        quiet_hours_start = _validated_clock_time(quiet_hours_start)
        quiet_hours_end = _validated_clock_time(quiet_hours_end)
        if not isinstance(locale, str) or locale not in {"zh-CN", "en"}:
            raise ServiceError(
                "通知语言仅支持中文或英文",
                code="invalid_notification_locale",
            )
        try:
            # Saving preferences and replacing the user service are one logical
            # external operation. Keeping the shared lock across both prevents
            # concurrent Web requests from splitting the unit schedule from the
            # configuration it is meant to run with.
            with exclusive_lock(self.paths.home / "operation.lock"):
                with self._config_lock:
                    previous_config = self.store.load()
                    previous_manager = build_manager(
                        self.paths,
                        ServiceSpec(
                            mode="daemon",
                            interval=previous_config.poll_interval_seconds,
                        ),
                    )
                    previous_status = previous_manager.status()
                    previous_service_existed = bool(
                        previous_status.get("installed")
                        or previous_status.get("loaded")
                    )
                    config = copy.deepcopy(previous_config)
                    config.poll_interval_seconds = interval_seconds
                    config.notifications.enabled = notifications_enabled
                    config.notifications.quiet_hours_enabled = quiet_hours_enabled
                    config.notifications.quiet_hours_start = quiet_hours_start
                    config.notifications.quiet_hours_end = quiet_hours_end
                    config.notifications.locale = locale
                    manager = build_manager(
                        self.paths,
                        ServiceSpec(mode="daemon", interval=interval_seconds),
                    )
                    service_operation_started = False
                    try:
                        self.store.save(config)
                        self.config = config
                        self._config_signature = self.store.signature()
                        service_operation_started = True
                        if enabled:
                            manager.install()
                        else:
                            manager.uninstall()
                    except (ServiceInstallError, OSError):
                        # Restore the configuration before restoring the old
                        # unit so an immediately started process cannot observe
                        # settings from the failed update.
                        try:
                            self.store.save(previous_config)
                        except OSError:
                            # Continue with service compensation even if the
                            # on-disk rollback itself cannot be completed.
                            pass
                        self.config = previous_config
                        self._config_signature = self.store.signature()
                        if service_operation_started:
                            try:
                                if previous_service_existed:
                                    previous_manager.install()
                                else:
                                    manager.uninstall()
                            except (ServiceInstallError, OSError):
                                # Preserve the original failure. The caller gets
                                # a failed result and can inspect service logs;
                                # configuration state is still rolled back.
                                pass
                        raise
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc
        except (ServiceInstallError, OSError) as exc:
            action = "启用" if enabled else "停用"
            raise ServiceError(
                "无法%s后台监控；请查看本机服务日志或在终端运行 service status"
                % action,
                code="monitoring_service_failed",
            ) from exc
        return self.monitoring_status()

    def codex(self, config: AppConfig | None = None) -> CodexCli:
        return CodexCli((config or self.config).codex, self.paths)

    def lark(self, config: AppConfig | None = None) -> LarkCli:
        return LarkCli((config or self.config).lark)

    def dashboard(
        self, *, event_limit: int = 40, since: str | None = None
    ) -> dict[str, Any]:
        config = self.refresh_config()
        snapshot = self.database.dashboard_snapshot(
            event_limit=event_limit + 1, since=since
        )
        event_rows = snapshot["events"]
        events_has_more = len(event_rows) > event_limit
        events = event_rows[:event_limit]
        return {
            "counts": snapshot["counts"],
            "daemon_health": snapshot["daemon_health"],
            "watches": snapshot["watches"],
            "events": events,
            "events_has_more": events_has_more,
            "events_next_before_id": events[-1].get("id") if events_has_more and events else None,
            "latest_check_run": snapshot.get("latest_check_run"),
            "pending_check_run": snapshot.get("pending_check_run"),
            "pending_check_run_count": snapshot.get("pending_check_run_count", 0),
            "settings": {
                "scan_roots": display_scan_roots(config.scan_roots),
                "scan_max_depth": config.scan_max_depth,
                "poll_interval_seconds": config.poll_interval_seconds,
                "codex_enabled": config.codex.enabled,
                "lark_enabled": config.lark.enabled,
                "lark_document": config.lark.document,
            },
        }

    def last_check_run(self) -> dict[str, Any] | None:
        run_id = self._last_check_run_id
        return self.database.get_check_run(run_id) if run_id else None

    def acknowledge_check_run(self, run_id: str) -> dict[str, Any]:
        run = self.database.acknowledge_check_run(run_id)
        if run is None:
            raise ServiceError("没有找到该检查批次", code="check_run_not_found")
        return run

    def update_repository_profile(
        self,
        repository_id: int,
        *,
        display_name: str | None,
        tags: Sequence[str],
    ) -> dict[str, Any]:
        repository = self.database.update_repository_profile(
            repository_id,
            display_name=display_name,
            tags=tags,
        )
        if repository is None:
            raise ServiceError("没有找到该仓库", code="repository_not_found")
        return repository

    def remove_watch(self, branch_id: int) -> dict[str, Any]:
        if (
            isinstance(branch_id, bool)
            or not isinstance(branch_id, int)
            or branch_id <= 0
        ):
            raise ServiceError("关注编号无效", code="invalid_watch_id")
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                watch = self.database.disable_watch(branch_id)
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc
        if watch is None:
            raise ServiceError("没有找到该关注分支", code="watch_not_found")
        return {
            "branch_id": int(watch["branch_id"]),
            "repository_id": int(watch["repository_id"]),
            "repository": str(watch["repository"]),
            "branch": str(watch["branch"]),
        }

    def integrations(self) -> dict[str, Any]:
        config = self.refresh_config()
        codex = self.codex(config).status()
        lark_client = self.lark(config)
        lark = lark_client.status()
        lark["document"] = config.lark.document
        if config.lark.document and lark.get("verified"):
            try:
                lark["document_details"] = lark_client.verify_document(
                    config.lark.document
                )
            except LarkError:
                # The configured destination remains useful even if metadata
                # cannot be refreshed. Publishing performs its own proof of
                # success and will surface any real access failure.
                pass
        return {"codex": codex, "lark": lark}

    def discover(
        self,
        roots: Sequence[str] | None = None,
        *,
        max_depth: int | None = None,
    ) -> list[dict[str, Any]]:
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                return self._discover_unlocked(roots, max_depth=max_depth)
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def _discover_unlocked(
        self,
        roots: Sequence[str] | None = None,
        *,
        max_depth: int | None = None,
    ) -> list[dict[str, Any]]:
        config = self.refresh_config()
        root_values = validate_scan_roots(
            config.scan_roots if roots is None else roots
        )
        if not root_values:
            return []
        scanner = RepositoryScanner(self.git)
        watches_by_path: dict[str, dict[str, Any]] = {}
        for watch in self.database.list_watches():
            local_path = watch.get("local_path")
            if not isinstance(local_path, str) or not local_path:
                continue
            match = watches_by_path.setdefault(
                local_path,
                {
                    "repository_id": watch.get("repository_id"),
                    "watched_branches": set(),
                },
            )
            branch = watch.get("branch")
            if isinstance(branch, str) and branch:
                match["watched_branches"].add(branch)

        values: list[dict[str, Any]] = []
        for item in scanner.scan(
            [Path(value).expanduser() for value in root_values],
            max_depth=max_depth or config.scan_max_depth,
        ):
            value = item.to_dict()
            watch_match = watches_by_path.get(item.path)
            value["repository_id"] = (
                watch_match.get("repository_id") if watch_match else None
            )
            value["watched_branches"] = sorted(
                watch_match.get("watched_branches", set())
                if watch_match
                else set(),
                key=str.casefold,
            )
            values.append(value)
        return values

    def preview_remote(self, url: Any) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ServiceError("请输入 GitHub 仓库地址", code="remote_required")
        value = url.strip()
        if len(value) > 2048:
            raise ServiceError("仓库地址过长", code="invalid_remote")
        if not is_safe_remote_url(value):
            raise ServiceError("不支持的远程地址", code="invalid_remote")
        try:
            locator = parse_repository_locator(value)
        except ValueError as exc:
            raise ServiceError(str(exc), code="invalid_remote") from exc
        return {
            "canonical": locator.canonical,
            "host": locator.host,
            "project": locator.project_path,
        }

    def clone_repository(
        self,
        url: Any,
        parent: Any,
        *,
        directory_name: str | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Clone a GitHub repository into an explicitly configured scan root."""
        if not isinstance(url, str) or not url.strip():
            raise ServiceError("请输入 GitHub 仓库地址", code="remote_required")
        try:
            locator = parse_github_clone_locator(url)
        except ValueError as exc:
            raise ServiceError(str(exc), code="invalid_remote") from exc
        if not isinstance(parent, str) or not parent.strip():
            raise ServiceError("请选择仓库保存目录", code="clone_parent_required")
        if len(parent) > MAX_SCAN_ROOT_LENGTH or "\0" in parent:
            raise ServiceError("仓库保存目录无效", code="invalid_clone_parent")

        config = self.refresh_config()
        configured_roots = validate_scan_roots(config.scan_roots)
        try:
            selected_input = _absolute_scan_root(parent)
            selected_stat = selected_input.lstat()
            if (
                stat.S_ISLNK(selected_stat.st_mode)
                or not stat.S_ISDIR(selected_stat.st_mode)
            ):
                raise OSError(errno.ELOOP, "clone root must be a real directory")
            selected_identity = (selected_stat.st_dev, selected_stat.st_ino)
            selected_parent = selected_input.resolve(strict=True)
            root_guard = _CloneRootGuard(
                selected_parent, expected_identity=selected_identity
            )
            allowed_roots = {
                _absolute_scan_root(value).resolve(strict=True)
                for value in configured_roots
            }
        except (OSError, RuntimeError):
            raise ServiceError(
                "仓库保存目录不存在或无法读取",
                code="invalid_clone_parent",
            ) from None
        if selected_parent not in allowed_roots:
            raise ServiceError(
                "只能克隆到已明确配置的扫描目录",
                code="invalid_clone_parent",
            )

        default_name = locator.project_path.rsplit("/", 1)[-1]
        target_name = _validate_clone_directory_name(
            default_name if directory_name is None else directory_name
        )
        target = selected_parent / target_name
        report = progress or (lambda _value: None)
        report({
            "phase": "validate",
            "label": "已验证 GitHub 仓库",
            "detail": "正在确认保存位置和本地仓库冲突。",
            "indeterminate": True,
        })
        if cancel_event is not None and cancel_event.is_set():
            raise ServiceError("仓库克隆已取消", code="clone_cancelled")

        try:
            with exclusive_lock(self.paths.home / "operation.lock"), root_guard:
                deadline = (
                    time.monotonic() + config.initial_fetch_timeout_seconds
                )
                scanner = RepositoryScanner(self.git)
                root_guard.check()
                if target.exists() or target.is_symlink():
                    with self.git.execution_limits(
                        cancel_event=cancel_event,
                        deadline=deadline,
                    ):
                        existing = scanner.inspect(target)
                    if cancel_event is not None and cancel_event.is_set():
                        raise ServiceError(
                            "仓库克隆已取消", code="clone_cancelled"
                        )
                    if time.monotonic() >= deadline:
                        raise ServiceError(
                            "仓库检查超时，请稍后重试", code="clone_timeout"
                        )
                    root_guard.check()
                    if (
                        existing.error is None
                        and Path(existing.path) == target
                        and _same_github_repository(
                            existing.canonical_remote, locator.canonical
                        )
                    ):
                        repository = self._candidate_result(existing)
                        report({
                            "phase": "complete",
                            "label": "已使用本地现有仓库",
                            "detail": "仓库无需重复下载，可以继续选择关注分支。",
                            "current": 1,
                            "total": 1,
                            "indeterminate": False,
                        })
                        return {"repository": repository, "reused": True}
                    raise ServiceError(
                        "目标目录已经存在，且不是同一个 GitHub 仓库",
                        code="clone_destination_exists",
                    )

                staging = selected_parent / (
                    ".branch-radar-clone-%s.partial" % uuid.uuid4().hex
                )
                while staging.exists() or staging.is_symlink():
                    root_guard.check()
                    staging = selected_parent / (
                        ".branch-radar-clone-%s.partial" % uuid.uuid4().hex
                    )
                try:
                    root_guard.check()
                    report({
                        "phase": "connect",
                        "label": "正在连接 GitHub",
                        "detail": "正在使用本机已有的 Git 身份验证远程仓库。",
                        "indeterminate": True,
                    })
                    self.git.clone_repository(
                        locator.sanitized_url,
                        staging,
                        timeout=max(0.001, deadline - time.monotonic()),
                        progress=_clone_progress_reporter(report),
                        cancel_event=cancel_event,
                    )
                    if cancel_event is not None and cancel_event.is_set():
                        raise GitCloneCancelled("仓库克隆已取消")
                    root_guard.check()
                    report({
                        "phase": "verify",
                        "label": "正在验证仓库",
                        "detail": "正在确认仓库身份与可用分支。",
                        "indeterminate": True,
                    })
                    root_guard.check()
                    with self.git.execution_limits(
                        cancel_event=cancel_event,
                        deadline=deadline,
                    ):
                        candidate = scanner.inspect(staging)
                    if cancel_event is not None and cancel_event.is_set():
                        raise GitCloneCancelled("仓库克隆已取消")
                    if time.monotonic() >= deadline:
                        raise GitError("仓库克隆超过统一截止时间")
                    root_guard.check()
                    if (
                        candidate.error is not None
                        or Path(candidate.path) != staging
                        or not _same_github_repository(
                            candidate.canonical_remote, locator.canonical
                        )
                    ):
                        raise ServiceError(
                            "下载完成，但仓库身份验证失败",
                            code="clone_verification_failed",
                        )
                    report({
                        "phase": "finalize",
                        "label": "正在完成克隆",
                        "detail": "正在将验证通过的仓库保存到所选目录。",
                        "indeterminate": True,
                    })
                    root_guard.check()
                    if target.exists() or target.is_symlink():
                        raise ServiceError(
                            "目标目录在克隆期间已被占用",
                            code="clone_destination_exists",
                        )
                    try:
                        _atomic_rename_no_replace(
                            staging, target, dir_fd=root_guard.fd
                        )
                    except OSError as exc:
                        if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                            raise
                        raise ServiceError(
                            "目标目录在克隆期间已被占用",
                            code="clone_destination_exists",
                        ) from exc
                    root_guard.check()
                    candidate.path = str(target)
                    repository = self._candidate_result(candidate)
                    report({
                        "phase": "complete",
                        "label": "仓库克隆完成",
                        "detail": "仓库已经就绪，可以继续选择关注分支。",
                        "current": 1,
                        "total": 1,
                        "indeterminate": False,
                    })
                    return {"repository": repository, "reused": False}
                except GitCloneCancelled as exc:
                    cleaned = _cleanup_clone_staging(staging, root_guard)
                    raise ServiceError(
                        (
                            "仓库克隆已取消"
                            if cleaned
                            else "仓库克隆已取消，但临时目录未能完全清理"
                        ),
                        code=(
                            "clone_cancelled"
                            if cleaned
                            else "clone_cleanup_failed"
                        ),
                    ) from exc
                except ServiceError as exc:
                    if _cleanup_clone_staging(staging, root_guard):
                        raise
                    raise ServiceError(
                        "仓库处理未完成，且临时目录未能完全清理",
                        code="clone_cleanup_failed",
                    ) from exc
                except GitError as exc:
                    cleaned = _cleanup_clone_staging(staging, root_guard)
                    timed_out = str(exc).startswith("仓库克隆超过")
                    message = (
                        "仓库克隆超时，请稍后重试"
                        if timed_out
                        else "无法克隆仓库，请确认本机 Git 身份可以访问该仓库且网络正常"
                    )
                    if not cleaned:
                        message += "；临时目录未能完全清理"
                    error_code = (
                        "clone_cleanup_failed"
                        if not cleaned
                        else "clone_timeout" if timed_out else "clone_failed"
                    )
                    raise ServiceError(
                        message,
                        code=error_code,
                    ) from exc
                except OSError as exc:
                    cleaned = _cleanup_clone_staging(staging, root_guard)
                    message = "无法完成本地仓库写入"
                    if not cleaned:
                        message += "；临时目录未能完全清理"
                    raise ServiceError(
                        message,
                        code=(
                            "clone_failed" if cleaned else "clone_cleanup_failed"
                        ),
                    ) from exc
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def _candidate_result(self, candidate: RepositoryCandidate) -> dict[str, Any]:
        value = candidate.to_dict()
        value["repository_id"] = None
        value["watched_branches"] = []
        for watch in self.database.list_watches():
            if watch.get("local_path") != candidate.path:
                continue
            value["repository_id"] = watch.get("repository_id")
            branch = watch.get("branch")
            if isinstance(branch, str) and branch:
                value["watched_branches"].append(branch)
        value["watched_branches"] = sorted(
            set(value["watched_branches"]), key=str.casefold
        )
        return value

    def add_watches(
        self,
        entries: Sequence[dict[str, Any]],
        *,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not entries:
            raise ServiceError("请至少选择一个仓库", code="selection_required")
        if len(entries) > 100:
            raise ServiceError("单次最多添加 100 个仓库", code="selection_too_large")
        config = self.refresh_config()
        watched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        report = progress or (lambda _value: None)
        repository_total = len(entries)
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                scanner = RepositoryScanner(self.git)
                for repository_index, entry in enumerate(entries, start=1):
                    report({
                        "phase": "inspect",
                        "label": "正在读取仓库信息",
                        "detail": "确认 Git remote、当前分支和本地引用。",
                        "repository_current": repository_index,
                        "repository_total": repository_total,
                        "indeterminate": True,
                    })
                    path_value = entry.get("path")
                    if not isinstance(path_value, str) or not path_value.strip():
                        skipped.append({"path": "", "error": "仓库路径无效"})
                        continue
                    candidate = scanner.inspect(Path(path_value).expanduser())
                    if candidate.error:
                        skipped.append({"path": candidate.path, "error": candidate.error})
                        continue
                    rebaseline_value = entry.get("rebaseline", False)
                    if not isinstance(rebaseline_value, bool):
                        skipped.append({
                            "path": candidate.path,
                            "error": "rebaseline 必须是布尔值",
                        })
                        continue
                    rebaseline = rebaseline_value
                    branches_value = entry.get("branches") or []
                    if not isinstance(branches_value, list) or not all(
                        isinstance(value, str) for value in branches_value
                    ):
                        skipped.append({"path": candidate.path, "error": "分支列表格式无效"})
                        continue
                    branches = list(dict.fromkeys(
                        value.strip() for value in branches_value if value.strip()
                    ))
                    if not branches:
                        default_branch = (
                            candidate.upstream_branch
                            or candidate.current_branch
                            or candidate.default_branch
                        )
                        branches = [default_branch] if default_branch else []
                    if not branches:
                        skipped.append({"path": candidate.path, "error": "无法确定要关注的分支"})
                        continue

                    binding_error = self._remote_binding_change_error(
                        candidate.path,
                        candidate.remote_name,
                        candidate.canonical_remote,
                        branches,
                        rebaseline=rebaseline,
                    )
                    if binding_error:
                        skipped.append({"path": candidate.path, "error": binding_error})
                        continue

                    baseline_ready = True
                    baseline_error: str | None = None
                    valid_fetch_branches = [
                        branch for branch in branches if is_valid_branch_name(branch)
                    ]
                    if candidate.remote_name and valid_fetch_branches:
                        report({
                            "phase": "fetch",
                            "label": "正在获取所选分支的远程引用",
                            "detail": (
                                "仅获取本次选择的 %d 个分支；首次基线最长等待 %d 秒。"
                                % (
                                    len(valid_fetch_branches),
                                    config.initial_fetch_timeout_seconds,
                                )
                            ),
                            "current": 0,
                            "total": len(valid_fetch_branches),
                            "repository_current": repository_index,
                            "repository_total": repository_total,
                            "indeterminate": True,
                        })
                        try:
                            self.git.fetch(
                                Path(candidate.path),
                                candidate.remote_name,
                                timeout=config.initial_fetch_timeout_seconds,
                                expected_fingerprint=candidate.canonical_remote,
                                branches=valid_fetch_branches,
                            )
                        except GitError as exc:
                            baseline_ready = False
                            baseline_error = redact_sensitive_text(str(exc))
                    if rebaseline and not baseline_ready:
                        skipped.append({
                            "path": candidate.path,
                            "error": "重建基线前必须成功刷新目标 remote；原有游标未修改",
                        })
                        continue

                    resolved: list[tuple[str, str | None]] = []
                    resolution_failed = False
                    for branch_index, branch in enumerate(branches, start=1):
                        report({
                            "phase": "resolve",
                            "label": "正在解析分支引用",
                            "detail": "核对第 %d/%d 个所选分支。" % (
                                branch_index, len(branches)
                            ),
                            "current": branch_index - 1,
                            "total": len(branches),
                            "repository_current": repository_index,
                            "repository_total": repository_total,
                            "indeterminate": False,
                        })
                        if not is_valid_branch_name(branch):
                            skipped.append({
                                "path": candidate.path,
                                "branch": branch,
                                "error": "分支名不符合 Git 引用命名规则",
                            })
                            resolution_failed = resolution_failed or rebaseline
                            continue
                        oid = (
                            self.git.branch_ref(
                                Path(candidate.path), candidate.remote_name, branch
                            )
                            if baseline_ready
                            else None
                        )
                        if baseline_ready and not oid:
                            skipped.append({
                                "path": candidate.path,
                                "branch": branch,
                                "error": "无法解析分支 %s" % branch,
                            })
                            resolution_failed = resolution_failed or rebaseline
                            continue
                        resolved.append((branch, oid))
                    if not resolved or resolution_failed:
                        continue

                    repository_id = self.database.upsert_repository(candidate)
                    for branch_index, (branch, oid) in enumerate(resolved, start=1):
                        report({
                            "phase": "save",
                            "label": "正在保存静默基线",
                            "detail": "写入第 %d/%d 个分支，不会生成历史变更消息。" % (
                                branch_index, len(resolved)
                            ),
                            "current": branch_index - 1,
                            "total": len(resolved),
                            "repository_current": repository_index,
                            "repository_total": repository_total,
                            "indeterminate": False,
                        })
                        try:
                            branch_id = self.database.add_branch(
                                repository_id,
                                branch,
                                candidate.remote_name,
                                remote_fingerprint=candidate.canonical_remote,
                                baseline_oid=oid,
                                present=True if not baseline_ready else bool(oid),
                                rebaseline=rebaseline,
                            )
                        except ValueError as exc:
                            skipped.append({
                                "path": candidate.path,
                                "branch": branch,
                                "error": str(exc),
                            })
                            continue
                        watched.append({
                            "repository_id": repository_id,
                            "branch_id": branch_id,
                            "repository": candidate.name,
                            "path": candidate.path,
                            "branch": branch,
                            "remote_name": candidate.remote_name,
                            "baseline": oid,
                            "baseline_pending": not baseline_ready,
                            "warning": baseline_error,
                        })
                        report({
                            "phase": "save",
                            "label": "正在保存静默基线",
                            "detail": "已保存 %d/%d 个分支。" % (
                                branch_index, len(resolved)
                            ),
                            "current": branch_index,
                            "total": len(resolved),
                            "repository_current": repository_index,
                            "repository_total": repository_total,
                            "indeterminate": False,
                        })
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc
        if not watched:
            detail = skipped[0]["error"] if skipped else "没有成功建立任何分支关注"
            raise ServiceError(detail, code="watch_not_created")
        return {"watched": watched, "skipped": skipped}

    def _remote_binding_change_error(
        self,
        local_path: str,
        remote_name: str | None,
        remote_fingerprint: str | None,
        branches: Sequence[str],
        *,
        rebaseline: bool,
    ) -> str | None:
        if remote_name and not remote_fingerprint:
            return "无法验证 remote %s 的仓库 fingerprint，已拒绝 fetch" % remote_name

        bindings = self.database.list_branch_bindings(local_path)
        requested = set(branches)
        changed_requested = [
            binding
            for binding in bindings
            if binding["branch"] in requested
            and (
                binding.get("remote_name") != remote_name
                or binding.get("remote_fingerprint") != remote_fingerprint
            )
        ]
        changed_shared_remote = [
            binding
            for binding in bindings
            if remote_name
            and binding.get("remote_name") == remote_name
            and binding.get("remote_fingerprint") != remote_fingerprint
        ]
        changed = changed_requested or changed_shared_remote
        if changed and not rebaseline:
            return (
                "remote 的仓库身份已变化，拒绝在未确认时 fetch 或重置游标；"
                "请核对后使用 watch add --rebaseline"
            )
        if rebaseline and changed_shared_remote:
            omitted = [
                str(binding["branch"])
                for binding in changed_shared_remote
                if binding["branch"] not in requested
            ]
            if omitted:
                return (
                    "remote %s 的 URL 对所有关联分支同时生效；安全重建基线必须同时包含：%s"
                    % (remote_name, "、".join(omitted))
                )
        return None

    def check(
        self,
        *,
        fetch: bool = True,
        use_codex: bool = False,
        publish: bool = False,
        branch_ids: Sequence[int] | None = None,
        repository_id: int | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[list[CheckResult], list[PublicationOutcome]]:
        report = progress or (lambda _value: None)

        def safe_report(value: dict[str, Any]) -> None:
            try:
                report(value)
            except Exception:
                # Progress is presentation-only and cannot determine whether
                # an otherwise durable check succeeds.
                pass

        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                config = self.refresh_config()
                if publish and not config.lark.document:
                    raise ServiceError(
                        "尚未关联飞书文档；请先在连接页面关联或创建文档",
                        code="lark_document_required",
                    )
                if branch_ids is not None and repository_id is not None:
                    raise ServiceError(
                        "仓库和分支选择不能同时使用", code="invalid_selection"
                    )
                all_watches = self.database.list_watches()
                if repository_id is not None:
                    if (
                        isinstance(repository_id, bool)
                        or not isinstance(repository_id, int)
                        or repository_id <= 0
                    ):
                        raise ServiceError(
                            "请选择有效仓库", code="invalid_selection"
                        )
                    watches = [
                        watch
                        for watch in all_watches
                        if int(watch["repository_id"]) == repository_id
                    ]
                    if not watches:
                        raise ServiceError(
                            "所选仓库不存在或没有关注分支",
                            code="selection_not_found",
                        )
                elif branch_ids is None:
                    watches = all_watches
                else:
                    if (
                        not branch_ids
                        or len(branch_ids) > 200
                        or any(
                            isinstance(branch_id, bool)
                            or not isinstance(branch_id, int)
                            or branch_id <= 0
                            for branch_id in branch_ids
                        )
                    ):
                        raise ServiceError(
                            "请选择 1 到 200 个有效分支", code="invalid_selection"
                        )
                    requested = set(branch_ids)
                    watches = [
                        watch
                        for watch in all_watches
                        if int(watch["branch_id"]) in requested
                    ]
                    found = {int(watch["branch_id"]) for watch in watches}
                    if found != requested:
                        raise ServiceError(
                            "所选分支不存在或已取消关注", code="selection_not_found"
                        )
                scope_kind = (
                    "repository"
                    if repository_id is not None
                    else ("branches" if branch_ids is not None else "all")
                )
                run_id = self.database.begin_check_run(
                    scope_kind=scope_kind,
                    repository_id=repository_id,
                    requested_branch_ids=list(branch_ids or []),
                    watches=watches,
                    fetch=fetch,
                    use_codex=use_codex,
                    publish=publish,
                )
                self._last_check_run_id = run_id
                try:
                    # Composition root for observation: swapping in a mirror or
                    # webhook source is a change here, not inside the monitor.
                    codex_client = self.codex(config) if use_codex else None
                    monitor = BranchMonitor(
                        self.database,
                        config,
                        source=GitFactSource(config, git=self.git, progress=safe_report),
                        codex_summarizer=codex_client,
                    )
                    results = monitor.check_watches(
                        cast(list[Watch], watches),
                        fetch=fetch,
                        use_codex=use_codex,
                        check_run_id=run_id,
                    )
                    safe_report({
                        "phase": "finalize",
                        "label": "正在整理本轮检查",
                        "detail": "已完成分支观察，正在汇总变化与异常。",
                        "current": len(results),
                        "total": len(results),
                        "indeterminate": False,
                    })
                    publications: list[PublicationOutcome] = []
                    if publish and config.lark.document:
                        safe_report({
                            "phase": "publish",
                            "label": "正在对账飞书发布",
                            "detail": "正在核验本轮事件及历史待处理投递。",
                            "current": 0,
                            "total": len(results),
                            "indeterminate": True,
                        })
                        publisher = self._publisher(config)
                        publisher.queue_events(
                            result.event_id
                            for result in results
                            if result.event_id is not None
                        )
                        publisher.queue_all_events(limit=1000)
                        publications = publisher.publish_pending(limit=1000)

                    for result in results:
                        if result.error:
                            result.error = _sanitize_check_run_error(
                                result.error, watches, self.paths
                            )
                        if result.warning:
                            result.warning = _sanitize_check_run_error(
                                result.warning, watches, self.paths
                            )
                    for publication in publications:
                        if publication.get("error"):
                            publication["error"] = _sanitize_check_run_error(
                                str(publication["error"]), watches, self.paths
                            )
                    visible_event_ids = [
                        int(result.event_id)
                        for result in results
                        if result.status == "changed" and result.event_id is not None
                    ]
                    event_rows = self.database.get_events(visible_event_ids)
                    deterministic = _deterministic_check_run_summary(
                        results, event_rows
                    )
                    serialized_results = [item.to_dict() for item in results]
                    self.database.finish_check_run(
                        run_id,
                        results=serialized_results,
                        publications=publications,
                        summary=deterministic,
                    )
                    if codex_client is not None and len(event_rows) >= 2:
                        safe_report({
                            "phase": "summarize",
                            "label": "正在生成本轮综述",
                            "detail": "仅使用已脱敏的结构化变化事实。",
                            "current": len(event_rows),
                            "total": len(event_rows),
                            "indeterminate": True,
                        })
                        self._enhance_check_run_summary(
                            run_id,
                            codex_client,
                            event_rows,
                            deterministic,
                            watches,
                        )
                    safe_report({
                        "phase": "complete",
                        "label": "分支检查已完成",
                        "detail": "检查结果已经持久保存%s。" % (
                            "，飞书发布已完成对账" if publish else ""
                        ),
                        "current": len(results),
                        "total": len(results),
                        "indeterminate": False,
                    })
                    return results, publications
                except Exception as exc:
                    try:
                        self.database.fail_check_run(
                            run_id,
                            _sanitize_check_run_error(str(exc), watches, self.paths),
                        )
                    except Exception:
                        pass
                    raise
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def _enhance_check_run_summary(
        self,
        run_id: str,
        codex: CodexCli,
        events: Sequence[dict[str, Any]],
        fallback: dict[str, Any],
        watches: Sequence[dict[str, Any]],
    ) -> None:
        try:
            max_events = 50
            selected = sorted(events, key=_check_run_ai_priority)[:max_events]
            completeness = str(fallback.get("completeness_note") or "")
            if len(events) > len(selected):
                sampled = "本轮共 %d 条变化，AI 综述仅处理其中 %d 条高优先级事件。" % (
                    len(events), len(selected)
                )
                completeness = " ".join(
                    item for item in (completeness, sampled) if item
                )
            bundles = [_check_run_ai_event(event) for event in selected]
            enhanced = codex.summarize_check_run(
                bundles,
                completeness_note=completeness,
            )
            enhanced["completeness_note"] = completeness
            enhanced["source"] = "codex"
            self.database.save_check_run_summary(
                run_id,
                _sanitize_check_run_summary(enhanced, watches, self.paths),
            )
        except Exception as exc:
            warning = "Codex 批次综述失败，已保留规则综述：%s" % (
                _sanitize_check_run_error(str(exc), [], self.paths)
            )
            try:
                self.database.save_check_run_summary(
                    run_id,
                    fallback,
                    warning=warning,
                )
            except Exception:
                pass

    def _publisher(self, config: AppConfig) -> Publisher:
        """Build the publisher for the configured destination.

        The only place a destination is chosen, mirroring how `check()` is the
        only place a `FactSource` is chosen.
        """
        document = config.lark.document
        if not document:
            raise ServiceError("尚未关联飞书文档", code="lark_document_required")
        return Publisher(
            self.database, LarkDocumentTarget(self.lark(config), document), document
        )

    def enforce_retention(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Drop history past the configured window, at most once a day.

        A background service checks forever, so without this the event, check
        run and delivery tables only ever grow. Pruning preserves unresolved
        delivery work and unacknowledged check runs, so this can never discard
        something the user still has to act on.
        """
        config = self.refresh_config()
        if config.retention_days <= 0:
            return {"ran": False, "reason": "disabled"}
        current = now or datetime.now(timezone.utc)
        last = self.database.daemon_health().get("last_prune_at")
        if last:
            try:
                previous = datetime.fromisoformat(str(last))
            except ValueError:
                previous = None
            if previous is not None:
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
                if current - previous < RETENTION_CHECK_INTERVAL:
                    return {"ran": False, "reason": "not_due"}
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                result = self.database.prune("%dd" % config.retention_days)
        except LockUnavailable:
            # Retention is housekeeping; a busy check matters more and the next
            # cycle will pick it up.
            return {"ran": False, "reason": "busy"}
        return dict(result, ran=True, retention_days=config.retention_days)

    def publish(self, *, limit: int = 1000) -> list[PublicationOutcome]:
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                config = self.refresh_config()
                if not config.lark.document:
                    raise ServiceError(
                        "尚未关联飞书文档", code="lark_document_required"
                    )
                publisher = self._publisher(config)
                publisher.queue_all_events(limit=limit)
                return publisher.publish_pending(limit=limit)
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def set_lark_document(self, document: str) -> dict[str, Any]:
        if not isinstance(document, str) or not document.strip():
            raise ServiceError("请输入飞书文档链接或 token", code="document_required")
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                config = self.refresh_config()
                try:
                    self.lark(config).verify_document(document.strip())
                except LarkError as exc:
                    raise ServiceError(str(exc), code="lark_document_unavailable") from exc
                with self._config_lock:
                    latest = self.store.load()
                    latest.lark.document = document.strip()
                    latest.lark.enabled = True
                    self.store.save(latest)
                    self.config = latest
                    self._config_signature = self.store.signature()
                return {"document": document.strip()}
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc

    def create_lark_document(
        self, title: str | None = None, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_key = request_id or uuid.uuid4().hex
        if not isinstance(request_key, str) or not 8 <= len(request_key) <= 128:
            raise ServiceError("创建请求缺少有效幂等标识", code="idempotency_required")
        try:
            with exclusive_lock(self.paths.home / "operation.lock"):
                config = self.refresh_config()
                document_title = (title or config.lark.document_title).strip()
                if not document_title:
                    raise ServiceError("请输入文档标题", code="title_required")
                fingerprint = hashlib.sha256(document_title.encode("utf-8")).hexdigest()
                try:
                    reservation = self.database.reserve_external_request(
                        request_key, "lark_create_document", fingerprint
                    )
                except ValueError as exc:
                    raise ServiceError(
                        str(exc), code="idempotency_conflict"
                    ) from exc
                if not reservation["created"]:
                    if reservation["state"] == "succeeded" and reservation.get("result"):
                        return dict(reservation["result"])
                    if reservation["state"] in {"pending", "unknown"}:
                        raise ServiceError(
                            "该创建请求或同标题请求已执行且结果待确认；"
                            "请先在飞书云空间检查，不要重复创建",
                            code="lark_create_unknown",
                        )
                    raise ServiceError(
                        str(reservation.get("last_error") or "该创建请求此前失败"),
                        code="lark_create_failed",
                    )
                try:
                    created = self.lark(config).create_document(document_title)
                except LarkError as exc:
                    code = "lark_create_unknown" if exc.may_have_succeeded else "lark_create_failed"
                    message = str(exc)
                    if exc.may_have_succeeded:
                        message += "；结果待确认，请先在飞书云空间检查，不要直接重复创建"
                    self.database.finish_external_request(
                        request_key,
                        state="unknown" if exc.may_have_succeeded else "failed",
                        error=message,
                    )
                    raise ServiceError(message, code=code) from exc
                with self._config_lock:
                    latest = self.store.load()
                    latest.lark.document = created["url"]
                    latest.lark.enabled = True
                    self.store.save(latest)
                    self.config = latest
                self.database.finish_external_request(
                    request_key, state="succeeded", result=created
                )
                return created
        except LockUnavailable as exc:
            raise ServiceBusy(str(exc)) from exc
