from __future__ import annotations

import copy
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..database import utc_now
from ..security import redact_sensitive_text
from ..services import ServiceError


@dataclass(frozen=True)
class JobResult:
    value: Any
    status: str = "succeeded"


class JobBusy(RuntimeError):
    def __init__(self, active_job: dict[str, Any]):
        super().__init__("已有任务正在运行")
        self.active_job = active_job


class JobNotCancellable(RuntimeError):
    pass


class JobManager:
    # A held request costs one server thread, so the cap keeps a misbehaving or
    # multi-tab client from occupying every slot the HTTP server will hand out.
    MAX_WAITERS = 8

    def __init__(self, *, history_limit: int = 100):
        self.history_limit = history_limit
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cancel_events: dict[str, threading.Event] = {}
        # A Condition is still a lock, so every existing critical section keeps
        # its meaning; it additionally lets a reader sleep until a writer moves
        # the job on, instead of the client re-asking on a timer.
        self._lock = threading.Condition()
        self._revision = 0
        self._waiters = 0
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="branch-radar-web"
        )

    def submit(
        self,
        kind: str,
        function: Callable[..., Any],
        *,
        with_progress: bool = False,
        cancellable: bool = False,
        initial_progress: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            active = self._active_unlocked()
            if active is not None:
                raise JobBusy(copy.deepcopy(active))
            job_id = secrets.token_urlsafe(12)
            now = utc_now()
            job = {
                "id": job_id,
                "kind": kind,
                "status": "queued",
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
                "error_code": None,
                "cancellable": bool(cancellable),
                "progress": self._safe_progress(initial_progress),
            }
            self._jobs[job_id] = job
            self._touch_unlocked(job)
            if cancellable:
                self._cancel_events[job_id] = threading.Event()
            self._trim_unlocked()
            self._executor.submit(
                self._run, job_id, function, with_progress, cancellable
            )
            return copy.deepcopy(job)

    def _run(
        self,
        job_id: str,
        function: Callable[..., Any],
        with_progress: bool,
        cancellable: bool,
    ) -> None:
        cancel_event: threading.Event | None
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None and cancel_event.is_set():
                self._finish_cancelled_unlocked(job)
                return
            job["status"] = "running"
            job["started_at"] = utc_now()
            self._touch_unlocked(job)
        try:
            if with_progress and cancellable:
                assert cancel_event is not None
                result = function(
                    lambda value: self._update_progress(job_id, value),
                    cancel_event,
                )
            elif with_progress:
                result = function(
                    lambda value: self._update_progress(job_id, value)
                )
            elif cancellable:
                assert cancel_event is not None
                result = function(cancel_event)
            else:
                result = function()
        except ServiceError as exc:
            if cancel_event is not None and exc.code == "clone_cancelled":
                self._finish_cancelled(job_id)
            else:
                self._finish_error(
                    job_id,
                    str(exc),
                    exc.code,
                    retryable=exc.code in {"operation_in_progress"},
                )
        except Exception:  # Keep internals, paths and tracebacks out of Web DTOs.
            self._finish_error(
                job_id,
                "任务遇到内部错误，请重试或查看本地服务日志",
                "operation_failed",
            )
        else:
            terminal_status = "succeeded"
            if isinstance(result, JobResult):
                terminal_status = result.status
                result = result.value
            # A cooperative operation proves cancellation by raising the
            # dedicated ServiceError. If it returned a value while the cancel
            # request was racing with its final commit/rename, preserve the
            # proven success.
            if terminal_status not in {
                "succeeded", "completed_with_errors", "reconcile_required",
            }:
                terminal_status = "failed"
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None:
                    job["status"] = terminal_status
                    job["result"] = result
                    if job.get("kind") == "clone" and terminal_status == "succeeded":
                        job["progress"] = {
                            "phase": "complete",
                            "label": "仓库克隆完成",
                            "detail": "仓库已经就绪，可以继续选择关注分支。",
                            "current": 1,
                            "total": 1,
                            "indeterminate": False,
                        }
                    job["finished_at"] = utc_now()
                    self._touch_unlocked(job)

    def _update_progress(self, job_id: str, value: dict[str, Any]) -> None:
        safe_value = self._safe_progress(value)
        if safe_value is None:
            return
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job["status"] in {"queued", "running"}:
                job["progress"] = safe_value
                self._touch_unlocked(job)

    @staticmethod
    def _safe_progress(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        requested_phase = str(value.get("phase") or "working")
        phase = requested_phase if requested_phase in {
            "queued", "working", "prepare", "validate", "connect", "clone",
            "download", "inspect", "verify", "fetch", "resolve", "save",
            "finalize", "cleanup", "complete", "error", "cancelling",
            "cancelled",
        } else "working"
        label = redact_sensitive_text(str(value.get("label") or "正在处理"))[:160]
        detail = redact_sensitive_text(str(value.get("detail") or ""))[:500]
        safe: dict[str, Any] = {
            "phase": phase,
            "label": label,
            "detail": detail,
            "indeterminate": bool(value.get("indeterminate", False)),
        }
        for key in ("current", "total", "repository_current", "repository_total"):
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool):
                safe[key] = max(0, min(item, 10_000))
        percent = value.get("percent")
        if isinstance(percent, int) and not isinstance(percent, bool):
            safe["percent"] = max(0, min(percent, 100))
        return safe

    def _finish_error(
        self, job_id: str, message: str, code: str, *, retryable: bool = False
    ) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["status"] = "failed"
                job["error"] = redact_sensitive_text(message)[:2000]
                job["error_code"] = code
                job["retryable"] = retryable
                job["finished_at"] = utc_now()
                job["progress"] = {
                    "phase": "error",
                    "label": "任务未完成",
                    "detail": redact_sensitive_text(message)[:500],
                    "indeterminate": False,
                }
                self._touch_unlocked(job)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] not in {"queued", "running", "cancelling"}:
                return copy.deepcopy(job)
            cancel_event = self._cancel_events.get(job_id)
            if cancel_event is None:
                raise JobNotCancellable("该任务不支持取消")
            cancel_event.set()
            if job["status"] == "queued":
                self._finish_cancelled_unlocked(job)
                return copy.deepcopy(job)
            job["status"] = "cancelling"
            job["progress"] = {
                "phase": "cancelling",
                "label": "正在取消任务",
                "detail": "正在停止 Git 操作并清理本次创建的临时目录。",
                "indeterminate": True,
            }
            self._touch_unlocked(job)
            return copy.deepcopy(job)

    def _finish_cancelled(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                self._finish_cancelled_unlocked(job)

    def _finish_cancelled_unlocked(self, job: dict[str, Any]) -> None:
        job["status"] = "cancelled"
        job["result"] = None
        job["error"] = None
        job["error_code"] = "clone_cancelled"
        job["retryable"] = False
        job["finished_at"] = utc_now()
        job["progress"] = {
            "phase": "cancelled",
            "label": "任务已取消",
            "detail": "未完成的克隆数据已交由核心服务安全清理。",
            "indeterminate": False,
        }
        self._touch_unlocked(job)

    def _touch_unlocked(self, job: dict[str, Any]) -> None:
        """Publish a mutation so every sleeping reader re-checks its job."""
        self._revision += 1
        job["revision"] = self._revision
        self._lock.notify_all()

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def wait_for_change(
        self, job_id: str, revision: int | None, timeout: float
    ) -> dict[str, Any] | None:
        """Return the job once it moves past ``revision``, or when time runs out.

        A terminal job returns immediately: there is nothing further to wait
        for, and holding the request would only delay the reply.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if (
                revision is None
                or self._closed
                or self._waiters >= self.MAX_WAITERS
                or job["revision"] != revision
                or job["status"] not in {"queued", "running", "cancelling"}
            ):
                return copy.deepcopy(job)
            self._waiters += 1
            try:
                self._lock.wait_for(
                    lambda: (
                        self._closed
                        or self._jobs.get(job_id) is None
                        or self._jobs[job_id]["revision"] != revision
                    ),
                    timeout,
                )
            finally:
                self._waiters -= 1
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job is not None else None

    def active(self) -> dict[str, Any] | None:
        with self._lock:
            active = self._active_unlocked()
            return copy.deepcopy(active) if active is not None else None

    def _active_unlocked(self) -> dict[str, Any] | None:
        for job in reversed(self._jobs.values()):
            if job["status"] in {"queued", "running", "cancelling"}:
                return job
        return None

    def _trim_unlocked(self) -> None:
        while len(self._jobs) > self.history_limit:
            job_id, job = next(iter(self._jobs.items()))
            if job["status"] in {"queued", "running", "cancelling"}:
                break
            self._jobs.pop(job_id, None)
            self._cancel_events.pop(job_id, None)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            for job_id, event in self._cancel_events.items():
                job = self._jobs.get(job_id)
                if job is not None and job["status"] in {
                    "queued", "running", "cancelling"
                }:
                    event.set()
                    job["status"] = "cancelling"
                    self._touch_unlocked(job)
            self._lock.notify_all()
        self._executor.shutdown(wait=True, cancel_futures=True)
