from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TextIO

fcntl: ModuleType | None
try:
    import fcntl as imported_fcntl
except ImportError:  # pragma: no cover - Branch Radar currently targets macOS/Linux.
    fcntl = None
else:
    fcntl = imported_fcntl


class LockUnavailable(RuntimeError):
    pass


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Prevent overlapping checks and publishers in separate processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle: TextIO = path.open("a+", encoding="utf-8")
    if fcntl is None:
        handle.close()
        raise LockUnavailable("当前平台暂不支持 Branch Radar 进程锁")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A held lock is the expected answer here, not a failure worth
            # chaining a traceback onto.
            raise LockUnavailable(
                "另一个 Branch Radar 检查或发布任务正在运行"
            ) from None
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
