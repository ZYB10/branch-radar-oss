from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from ..database import utc_now
from ..folder_picker import FolderPicker, FolderPickerError
from ..security import redact_sensitive_text
from ..services import (
    RadarService,
    ServiceBusy,
    ServiceError,
    display_scan_roots,
    validate_scan_roots,
)
from .jobs import JobBusy, JobManager, JobNotCancellable, JobResult
from .views import (
    candidate_view,
    check_run_view,
    dashboard_view,
    event_view,
    integration_view,
)

MAX_BODY_BYTES = 64 * 1024
# Smaller JSON bodies fit one TCP segment either way, so gzip below this
# size only adds framing bytes and CPU.
JSON_GZIP_MIN_BYTES = 1024
# Upper bound on a held job request. Long enough that an ordinary check
# finishes inside one request, short enough to stay clear of the idle
# timeouts a browser or proxy applies to a connection that says nothing.
MAX_JOB_WAIT_SECONDS = 25
# A year of lookback is past any window the console offers and keeps the
# computed cutoff well inside the range datetime can represent.
MAX_EVENT_WINDOW_HOURS = 24 * 366
# Codex/lark login state changes outside this process, so a cached probe is a
# hint with a shelf life, not a fact. Past the TTL the payload is still served
# (it beats showing "unknown") but is flagged so the console re-probes.
INTEGRATION_CACHE_TTL_SECONDS = 120
ASSETS = {
    "/assets/app.css": "app.css",
    "/assets/theme-bootstrap.js": "theme-bootstrap.js",
    "/assets/i18n.js": "i18n.js",
    "/assets/app.js": "app.js",
    "/assets/ui-model.js": "ui-model.js",
    "/assets/changelog.json": "changelog.json",
    "/assets/branch-radar-mark.svg": "branch-radar-mark.svg",
    "/favicon.svg": "branch-radar-mark.svg",
}

SocketRequest = socket.socket | tuple[bytes, socket.socket]


class WebController:
    def __init__(self, service: RadarService):
        self.service = service
        self.jobs = JobManager()
        self._shutdown = threading.Event()
        self._integration_cache: dict[str, Any] | None = None
        self._integration_cache_at = 0.0
        self._integration_lock = threading.Lock()
        self._idempotency_lock = threading.Lock()
        self._idempotent_jobs: dict[str, dict[str, str]] = {}

    def dashboard(self, query: str = "") -> dict[str, Any]:
        # The console seeds its activity list from this reply, so the same
        # window applies here; otherwise the seed carries events the view has
        # already been told to hide.
        payload = dashboard_view(
            self.service.dashboard(since=_event_window_cutoff(query))
        )
        payload["app"] = {
            "version": __version__,
            "time": utc_now(),
        }
        payload["active_job"] = self.jobs.active()
        return payload

    def integrations(self) -> dict[str, Any]:
        with self._integration_lock:
            if self._integration_cache is not None:
                age = time.monotonic() - self._integration_cache_at
                payload = dict(self._integration_cache)
                payload["stale"] = age > INTEGRATION_CACHE_TTL_SECONDS
                return payload
            return {
                "checked": False,
                "stale": True,
                "codex": {"available": False, "authenticated": False},
                "lark": {
                    "available": False,
                    "verified": False,
                    "document_configured": False,
                },
            }

    def monitoring(self) -> dict[str, Any]:
        return self.service.monitoring_status()

    def submit_monitoring_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {
            "enabled",
            "interval_seconds",
            "notifications_enabled",
            "quiet_hours",
            "locale",
        }
        if set(payload) != required:
            raise ServiceError(
                "后台监控设置字段不完整",
                code="invalid_monitoring_settings",
            )
        enabled = payload.get("enabled")
        notifications_enabled = payload.get("notifications_enabled")
        interval_seconds = payload.get("interval_seconds")
        locale = payload.get("locale")
        quiet_hours = payload.get("quiet_hours")
        if not isinstance(enabled, bool) or not isinstance(
            notifications_enabled, bool
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
        if not isinstance(locale, str) or locale not in {"zh-CN", "en"}:
            raise ServiceError(
                "通知语言仅支持中文或英文",
                code="invalid_notification_locale",
            )
        if not isinstance(quiet_hours, dict) or set(quiet_hours) != {
            "enabled", "start", "end"
        }:
            raise ServiceError(
                "勿扰时段设置格式无效",
                code="invalid_quiet_hours",
            )
        quiet_enabled = quiet_hours.get("enabled")
        quiet_start = quiet_hours.get("start")
        quiet_end = quiet_hours.get("end")
        if (
            not isinstance(quiet_enabled, bool)
            or not isinstance(quiet_start, str)
            or not isinstance(quiet_end, str)
        ):
            raise ServiceError(
                "勿扰时段设置格式无效",
                code="invalid_quiet_hours",
            )
        return self.jobs.submit(
            "monitoring",
            lambda: self.service.configure_monitoring(
                enabled=enabled,
                interval_seconds=interval_seconds,
                notifications_enabled=notifications_enabled,
                quiet_hours_enabled=quiet_enabled,
                quiet_hours_start=quiet_start,
                quiet_hours_end=quiet_end,
                locale=locale,
            ),
        )

    def submit_integration_refresh(self, _payload: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            value = integration_view(self.service.integrations())
            value["checked"] = True
            with self._integration_lock:
                self._integration_cache = value
                self._integration_cache_at = time.monotonic()
            return dict(value, stale=False)

        return self.jobs.submit("integrations", run)

    def event(self, event_id: int) -> dict[str, Any] | None:
        row = self.service.database.get_event(event_id)
        if not row or row.get("kind") == "baseline":
            return None
        return event_view(row, detailed=True)

    def events(self, query: str) -> dict[str, Any]:
        try:
            parameters = parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=10,
            )
        except ValueError as exc:
            raise ServiceError("事件查询参数无效", code="invalid_query") from exc
        allowed = {"repository_id", "branch_id", "before_id", "limit", "since_hours"}
        if set(parameters) - allowed:
            raise ServiceError("事件查询包含不支持的参数", code="invalid_query")
        if any(len(values) != 1 for values in parameters.values()):
            raise ServiceError("事件查询参数不能重复", code="invalid_query")

        repository_id = _positive_query_integer(parameters, "repository_id")
        branch_id = _positive_query_integer(parameters, "branch_id")
        before_id = _positive_query_integer(parameters, "before_id")
        limit = _positive_query_integer(parameters, "limit", default=40)
        if limit is None or limit > 100:
            raise ServiceError("limit 必须在 1 到 100 之间", code="invalid_query")

        # The window is a lookback in hours rather than a client timestamp, so
        # the cutoff is anchored to the server clock the events were stamped
        # with instead of to whatever the browser believes the time is.
        since = _window_cutoff(_positive_query_integer(parameters, "since_hours"))

        rows = self.service.database.list_events(
            repository_id=repository_id,
            branch_id=branch_id,
            before_id=before_id,
            since=since,
            limit=limit + 1,
            include_baseline=False,
            compact=True,
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "events": [event_view(row) for row in page],
            "next_before_id": page[-1].get("id") if has_more and page else None,
            "has_more": has_more,
        }

    def submit_discovery(self, payload: dict[str, Any]) -> dict[str, Any]:
        roots = payload.get("roots")
        if roots is not None and (
            not isinstance(roots, list)
            or not all(isinstance(value, str) and value.strip() for value in roots)
        ):
            raise ServiceError("扫描目录格式无效", code="invalid_roots")
        return self.jobs.submit(
            "discover",
            lambda: {
                "repositories": [
                    candidate_view(item) for item in self.service.discover(roots)
                ]
            },
        )

    def update_scan_roots(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) != {"roots"}:
            raise ServiceError(
                "扫描目录设置仅接受 roots 字段",
                code="invalid_scan_roots",
            )
        roots = payload.get("roots")
        if not isinstance(roots, list) or not all(
            isinstance(value, str) for value in roots
        ):
            raise ServiceError("扫描目录格式无效", code="invalid_scan_roots")
        config = self.service.update_preferences(scan_roots=roots)
        return {
            "ok": True,
            "scan_roots": display_scan_roots(config.scan_roots),
        }

    def submit_scan_root_picker(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise ServiceError(
                "目录选择请求不支持额外字段",
                code="invalid_folder_picker_request",
            )

        def run() -> dict[str, Any]:
            try:
                selection = FolderPicker(cancel_event=self._shutdown).pick()
            except FolderPickerError as exc:
                raise ServiceError(str(exc), code=exc.code) from exc
            if selection.cancelled:
                return {"cancelled": True, "scan_root": None}
            assert selection.path is not None
            normalized = validate_scan_roots([selection.path])
            return {"cancelled": False, "scan_root": normalized[0]}

        return self.jobs.submit("pick_scan_root", run)

    def preview_remote(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = payload.get("url")
        if url is None:
            url = payload.get("remote")
        return {"remote": self.service.preview_remote(url)}

    def submit_repository_clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"url", "parent", "directory_name"}
        if set(payload) - allowed or "url" not in payload or "parent" not in payload:
            raise ServiceError(
                "克隆请求仅接受 url、parent 和可选 directory_name 字段",
                code="invalid_clone_request",
            )
        url = payload.get("url")
        parent = payload.get("parent")
        directory_name = payload.get("directory_name")
        if (
            not isinstance(url, str)
            or not url.strip()
            or len(url) > 2048
            or _contains_control_character(url)
        ):
            raise ServiceError("仓库地址格式无效", code="invalid_clone_request")
        if (
            not isinstance(parent, str)
            or not parent.strip()
            or len(parent) > 4096
            or "\0" in parent
        ):
            raise ServiceError("克隆目录格式无效", code="invalid_clone_request")
        if directory_name is not None and (
            not isinstance(directory_name, str)
            or not directory_name.strip()
            or len(directory_name) > 255
            or _contains_control_character(directory_name)
        ):
            raise ServiceError("目录名称格式无效", code="invalid_clone_request")

        def run(
            progress: Callable[[dict[str, Any]], None],
            cancel_event: threading.Event,
        ) -> dict[str, Any]:
            result = self.service.clone_repository(
                url.strip(),
                parent,
                directory_name=directory_name,
                progress=progress,
                cancel_event=cancel_event,
            )
            if not isinstance(result, dict):
                raise ServiceError("克隆结果格式无效", code="clone_failed")
            repository = result.get("repository")
            to_dict = getattr(repository, "to_dict", None)
            if callable(to_dict):
                repository = to_dict()
            if not isinstance(repository, dict):
                raise ServiceError("无法读取克隆后的仓库", code="clone_failed")
            return {
                "repository": candidate_view(repository),
                "reused": bool(result.get("reused", False)),
            }

        return self.jobs.submit(
            "clone",
            run,
            with_progress=True,
            cancellable=True,
            initial_progress={
                "phase": "queued",
                "label": "正在准备克隆仓库",
                "detail": "即将验证目标目录并连接 GitHub。",
                "current": 0,
                "total": 1,
                "indeterminate": True,
            },
        )

    def cancel_job(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if payload:
            raise ServiceError(
                "取消任务请求不支持额外字段",
                code="invalid_job_cancel_request",
            )
        try:
            job = self.jobs.cancel(job_id)
        except JobNotCancellable as exc:
            raise ServiceError(str(exc), code="job_not_cancellable") from exc
        if job is None:
            raise ServiceError("没有找到该任务", code="job_not_found")
        return job

    def submit_watches(self, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise ServiceError("请选择要关注的仓库", code="selection_required")
        branch_count = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"path", "branches"}:
                raise ServiceError("关注项包含不支持的字段", code="invalid_watch")
            branches = entry.get("branches") or []
            if not isinstance(branches, list) or not all(
                isinstance(value, str) and value.strip() for value in branches
            ):
                raise ServiceError("分支列表格式无效", code="invalid_watch")
            branch_count += len(branches)
        if branch_count > 200:
            raise ServiceError("单次最多添加 200 个分支", code="selection_too_large")
        def run(progress: Callable[[dict[str, Any]], None]) -> JobResult:
            result = self.service.add_watches(entries, progress=progress)
            pending = any(
                item.get("baseline_pending") or item.get("warning")
                for item in result.get("watched") or []
            )
            status = "completed_with_errors" if result.get("skipped") or pending else "succeeded"
            safe_result = {
                "watched": [
                    {
                        "repository": item.get("repository"),
                        "repository_id": item.get("repository_id"),
                        "branch_id": item.get("branch_id"),
                        "branch": item.get("branch"),
                        "baseline_pending": bool(item.get("baseline_pending")),
                        "warning": redact_sensitive_text(str(item.get("warning") or ""))[:1000] or None,
                    }
                    for item in result.get("watched") or []
                ],
                "skipped": [
                    {
                        "branch": item.get("branch"),
                        "error": redact_sensitive_text(str(item.get("error") or ""))[:1000],
                    }
                    for item in result.get("skipped") or []
                ],
            }
            progress({
                "phase": "complete",
                "label": "分支关注已完成",
                "detail": "正在刷新页面中的关注分支。",
                "current": len(safe_result["watched"]),
                "total": branch_count,
                "indeterminate": False,
            })
            return JobResult(safe_result, status)

        return self.jobs.submit(
            "watch",
            run,
            with_progress=True,
            initial_progress={
                "phase": "queued",
                "label": "正在准备关注任务",
                "detail": "即将读取仓库并获取所选分支的最新引用。",
                "current": 0,
                "total": branch_count,
                "indeterminate": True,
            },
        )

    def remove_watch(
        self, branch_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload:
            raise ServiceError(
                "取消关注请求不支持额外字段", code="invalid_watch"
            )
        return {"ok": True, "removed": self.service.remove_watch(branch_id)}

    def update_repository_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"repository_id", "alias", "tags"}:
            raise ServiceError(
                "仓库资料包含不支持的字段", code="invalid_repository_profile"
            )
        repository_id = payload.get("repository_id")
        if (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
        ):
            raise ServiceError("仓库编号无效", code="invalid_repository_profile")

        alias = payload.get("alias")
        if alias is not None and not isinstance(alias, str):
            raise ServiceError("仓库别名格式无效", code="invalid_repository_profile")
        display_name = alias.strip() if isinstance(alias, str) else None
        if display_name and (
            len(display_name) > 80 or _contains_control_character(display_name)
        ):
            raise ServiceError(
                "仓库别名最多 80 个字符，且不能包含控制字符",
                code="invalid_repository_profile",
            )
        display_name = display_name or None

        raw_tags = payload.get("tags", [])
        if not isinstance(raw_tags, list) or len(raw_tags) > 8:
            raise ServiceError("标签最多设置 8 个", code="invalid_repository_profile")
        tags = []
        seen = set()
        for raw_tag in raw_tags:
            if not isinstance(raw_tag, str):
                raise ServiceError("标签格式无效", code="invalid_repository_profile")
            tag = raw_tag.strip()
            tag_key = tag.casefold()
            if (
                not tag
                or len(tag) > 24
                or "," in tag
                or "，" in tag
                or _contains_control_character(tag)
            ):
                raise ServiceError(
                    "每个标签应为 1 到 24 个字符，且不能包含逗号",
                    code="invalid_repository_profile",
                )
            if tag_key in seen:
                continue
            seen.add(tag_key)
            tags.append(tag)

        repository = self.service.update_repository_profile(
            repository_id,
            display_name=display_name,
            tags=tags,
        )
        return {
            "repository": {
                "id": repository_id,
                "name": repository.get("name"),
                "alias": display_name,
                "tags": tags,
            }
        }

    def submit_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        fetch = _boolean(payload, "fetch", True)
        use_codex = _boolean(payload, "use_codex", False)
        publish = _boolean(payload, "publish", False)
        branch_ids = payload.get("branch_ids")
        repository_id = payload.get("repository_id")
        if "repository_id" in payload and (
            isinstance(repository_id, bool)
            or not isinstance(repository_id, int)
            or repository_id <= 0
        ):
            raise ServiceError(
                "repository_id 必须是正整数",
                code="invalid_selection",
            )
        if branch_ids is not None:
            if (
                not isinstance(branch_ids, list)
                or not 1 <= len(branch_ids) <= 200
                or any(
                    isinstance(branch_id, bool)
                    or not isinstance(branch_id, int)
                    or branch_id <= 0
                    for branch_id in branch_ids
                )
            ):
                raise ServiceError(
                    "branch_ids 必须包含 1 到 200 个正整数",
                    code="invalid_selection",
                )
            branch_ids = list(dict.fromkeys(branch_ids))
        if branch_ids is not None and repository_id is not None:
            raise ServiceError(
                "repository_id 和 branch_ids 不能同时使用",
                code="invalid_selection",
            )

        def run(progress: Callable[[dict[str, Any]], None]) -> JobResult:
            results, publications = self.service.check(
                fetch=fetch,
                use_codex=use_codex,
                publish=publish,
                branch_ids=branch_ids,
                repository_id=repository_id,
                progress=progress,
            )
            safe_results = []
            for result in results:
                serialized = result.to_dict()
                if serialized.get("event_kind") == "baseline":
                    serialized["event_id"] = None
                    serialized["summary"] = None
                safe_results.append(serialized)
            last_check_run = getattr(self.service, "last_check_run", None)
            check_run = check_run_view(
                last_check_run() if callable(last_check_run) else None
            )
            value = {
                "results": safe_results,
                "publications": publications,
                "check_run": check_run,
            }
            has_errors = any(result.status == "error" for result in results) or any(
                item.get("status") == "error" for item in publications
            )
            has_unknown = any(item.get("status") == "unknown" for item in publications)
            status = "reconcile_required" if has_unknown else (
                "completed_with_errors" if has_errors else "succeeded"
            )
            severity = {
                "succeeded": 0,
                "completed_with_errors": 1,
                "reconcile_required": 2,
            }
            persisted_status = (
                str(check_run.get("status")) if isinstance(check_run, dict) else ""
            )
            if severity.get(persisted_status, -1) > severity[status]:
                status = persisted_status
            return JobResult(value, status)

        selected_count = len(branch_ids) if branch_ids is not None else 0
        return self.jobs.submit(
            "check",
            run,
            with_progress=True,
            initial_progress={
                "phase": "queued",
                "label": "正在准备分支检查",
                "detail": (
                    "将获取所选仓库全部关注分支的远程对象。"
                    if repository_id is not None
                    else (
                        "将只获取所选分支的远程对象。"
                        if selected_count
                        else "将按仓库获取所有关注分支的远程对象。"
                    )
                ),
                "current": 0,
                "total": selected_count,
                "indeterminate": True,
            },
        )

    def acknowledge_check_run(
        self, run_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if payload:
            raise ServiceError(
                "确认检查批次不支持额外字段",
                code="invalid_check_run_acknowledgement",
            )
        return {
            "ok": True,
            "check_run": check_run_view(
                self.service.acknowledge_check_run(run_id)
            ),
        }

    def submit_publish(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = payload.get("limit", 1000)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ServiceError("发布数量必须在 1 到 5000 之间", code="invalid_limit")
        def run() -> JobResult:
            outcomes = self.service.publish(limit=limit)
            has_unknown = any(item.get("status") == "unknown" for item in outcomes)
            has_errors = any(item.get("status") == "error" for item in outcomes)
            status = "reconcile_required" if has_unknown else (
                "completed_with_errors" if has_errors else "succeeded"
            )
            return JobResult({"outcomes": outcomes}, status)

        return self.jobs.submit("publish", run)

    def submit_lark_connect(self, payload: dict[str, Any]) -> dict[str, Any]:
        document = payload.get("document")
        if not isinstance(document, str):
            raise ServiceError("请输入飞书文档链接或 token", code="document_required")

        def run() -> dict[str, Any]:
            self.service.set_lark_document(document)
            self._clear_integrations()
            return {"configured": True}

        return self.jobs.submit("lark_connect", run)

    def submit_lark_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = payload.get("title")
        if title is not None and not isinstance(title, str):
            raise ServiceError("文档标题格式无效", code="invalid_title")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
            raise ServiceError("创建请求缺少幂等标识", code="idempotency_required")

        def run() -> dict[str, Any]:
            self.service.create_lark_document(title, request_id=request_id)
            self._clear_integrations()
            return {"configured": True, "title": title or "项目分支动态"}

        with self._idempotency_lock:
            map_key = "lark_create:" + request_id
            existing_record = self._idempotent_jobs.get(map_key)
            if existing_record:
                if existing_record["title"] != (title or ""):
                    raise ServiceError(
                        "同一幂等标识不能用于不同的文档标题",
                        code="idempotency_conflict",
                    )
                existing = self.jobs.get(existing_record["job_id"])
                if existing is not None:
                    return existing
            job = self.jobs.submit("lark_create", run)
            self._idempotent_jobs[map_key] = {
                "job_id": job["id"],
                "title": title or "",
            }
            return job

    def _clear_integrations(self) -> None:
        with self._integration_lock:
            self._integration_cache = None
            self._integration_cache_at = 0.0

    def close(self) -> None:
        self._shutdown.set()
        self.jobs.shutdown()


class RadarHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, server_address: tuple[str, int], controller: WebController
    ):
        self.controller = controller
        self.csrf_token = secrets.token_urlsafe(32)
        self.started_at = utc_now()
        # A persistent connection occupies a slot for its whole idle life, not
        # just while a request is in flight, so the bound has to cover the
        # several connections a browser keeps open per tab. JobManager separately
        # caps how many of those may be parked on a long poll.
        self._request_slots = threading.BoundedSemaphore(64)
        super().__init__(server_address, RadarRequestHandler)

    def process_request(
        self, request: SocketRequest, client_address: tuple[str, int]
    ) -> None:
        if not self._request_slots.acquire(blocking=False):
            request_socket = request[1] if isinstance(request, tuple) else request
            request_socket.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(
        self, request: SocketRequest, client_address: tuple[str, int]
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()

    def handle_error(
        self, request: SocketRequest, client_address: tuple[str, int]
    ) -> None:
        error = sys.exc_info()[1]
        seen = set()
        while error is not None and id(error) not in seen:
            if isinstance(error, (BrokenPipeError, ConnectionResetError)):
                return
            seen.add(id(error))
            error = error.__cause__ or error.__context__
        super().handle_error(request, client_address)

    @property
    def public_url(self) -> str:
        return "http://127.0.0.1:%d" % self.server_port

    @property
    def launch_url(self) -> str:
        return "%s/#session=%s" % (self.public_url, self.csrf_token)


class RadarRequestHandler(BaseHTTPRequestHandler):
    server: RadarHTTPServer
    server_version = "BranchRadar"
    sys_version = ""
    # Every reply carries an accurate Content-Length, so the connection can be
    # reused instead of rebuilt for each poll, asset and job request.
    protocol_version = "HTTP/1.1"
    # An idle persistent connection still holds a worker thread and one request
    # slot, so it is reclaimed rather than kept until the browser lets go. The
    # same bound caps a client that opens a connection and never speaks.
    timeout = 10
    _body_pending = False

    def do_GET(self) -> None:
        if not self._accept_request():
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/":
            self._send_resource("index.html", "text/html; charset=utf-8", cache=False)
            return
        if path in ASSETS:
            filename = ASSETS[path]
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            if filename.endswith((".css", ".js")):
                mime += "; charset=utf-8"
            self._send_resource(filename, mime, cache=True)
            return
        if path == "/api/v1/health":
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "version": __version__,
                "started_at": self.server.started_at,
            })
            return
        if path.startswith("/api/v1/") and not self._valid_token():
            return
        if path == "/api/v1/dashboard":
            try:
                payload = self.server.controller.dashboard(parsed.query)
            except ServiceError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path == "/api/v1/integrations":
            self._send_json(HTTPStatus.OK, self.server.controller.integrations())
            return
        if path == "/api/v1/monitoring":
            try:
                payload = self.server.controller.monitoring()
            except ServiceError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path == "/api/v1/events":
            try:
                payload = self.server.controller.events(parsed.query)
            except ServiceError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.startswith("/api/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            try:
                wait_seconds, revision = _job_wait_parameters(parsed.query)
            except ServiceError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
                return
            if wait_seconds > 0:
                job = self.server.controller.jobs.wait_for_change(
                    job_id, revision, wait_seconds
                )
            else:
                job = self.server.controller.jobs.get(job_id)
            if job is None:
                self._json_error(HTTPStatus.NOT_FOUND, "job_not_found", "没有找到该任务")
            else:
                self._send_json(HTTPStatus.OK, {"job": job})
            return
        if path.startswith("/api/v1/events/"):
            try:
                event_id = _parse_positive_integer(
                    path.rsplit("/", 1)[-1], "event_id"
                )
            except ServiceError:
                self._json_error(HTTPStatus.NOT_FOUND, "event_not_found", "没有找到该事件")
                return
            event = self.server.controller.event(event_id)
            if event is None:
                self._json_error(HTTPStatus.NOT_FOUND, "event_not_found", "没有找到该事件")
            else:
                self._send_json(HTTPStatus.OK, {"event": event})
            return
        self._json_error(HTTPStatus.NOT_FOUND, "not_found", "没有找到该页面")

    def do_POST(self) -> None:
        self._body_pending = bool(self.headers.get("Content-Length"))
        if not self._accept_request(mutating=True):
            return
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
            result = self._route_post(path, payload)
        except RequestBodyError as exc:
            self._json_error(exc.status, exc.code, str(exc))
            return
        except JobBusy as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "error": {
                        "code": "operation_in_progress",
                        "message": "已有任务正在运行",
                    },
                    "active_job": exc.active_job,
                },
            )
            return
        except ServiceBusy as exc:
            self._json_error(HTTPStatus.CONFLICT, exc.code, str(exc))
            return
        except ServiceError as exc:
            status = (
                HTTPStatus.CONFLICT
                if exc.code in {"idempotency_conflict", "job_not_cancellable"}
                else (
                    HTTPStatus.NOT_FOUND
                    if exc.code in {"check_run_not_found", "job_not_found"}
                    else HTTPStatus.BAD_REQUEST
                )
            )
            self._json_error(status, exc.code, str(exc))
            return
        if path in {
            "/api/v1/preferences/scan-roots",
            "/api/v1/remotes/preview",
            "/api/v1/repositories/profile",
        } or re.fullmatch(
            r"/api/v1/check-runs/[0-9a-f]{32}/acknowledge", path
        ):
            self._send_json(HTTPStatus.OK, result)
            return
        self._send_json(HTTPStatus.ACCEPTED, {"job": result})

    def do_PUT(self) -> None:
        self._body_pending = bool(self.headers.get("Content-Length"))
        if not self._accept_request(mutating=True):
            return
        path = urlsplit(self.path).path
        if path != "/api/v1/monitoring":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "没有找到该接口")
            return
        try:
            payload = self._read_json()
            result = self.server.controller.submit_monitoring_update(payload)
        except RequestBodyError as exc:
            self._json_error(exc.status, exc.code, str(exc))
            return
        except ServiceBusy as exc:
            self._json_error(HTTPStatus.CONFLICT, exc.code, str(exc))
            return
        except ServiceError as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, exc.code, str(exc))
            return
        except JobBusy as exc:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "ok": False,
                    "error": {
                        "code": "operation_in_progress",
                        "message": "已有任务正在运行",
                    },
                    "active_job": exc.active_job,
                },
            )
            return
        self._send_json(HTTPStatus.ACCEPTED, {"job": result})

    def do_DELETE(self) -> None:
        self._body_pending = bool(self.headers.get("Content-Length"))
        if not self._accept_request(mutating=True):
            return
        path = urlsplit(self.path).path
        try:
            payload = self._read_json()
            result = self._route_delete(path, payload)
        except RequestBodyError as exc:
            self._json_error(exc.status, exc.code, str(exc))
            return
        except ServiceBusy as exc:
            self._json_error(HTTPStatus.CONFLICT, exc.code, str(exc))
            return
        except ServiceError as exc:
            status = (
                HTTPStatus.NOT_FOUND
                if exc.code == "watch_not_found"
                else HTTPStatus.BAD_REQUEST
            )
            self._json_error(status, exc.code, str(exc))
            return
        self._send_json(HTTPStatus.OK, result)

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _route_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        cancel_job_match = re.fullmatch(
            r"/api/v1/jobs/([A-Za-z0-9_-]{8,128})/cancel", path
        )
        if cancel_job_match:
            return self.server.controller.cancel_job(
                cancel_job_match.group(1), payload
            )
        check_run_match = re.fullmatch(
            r"/api/v1/check-runs/([0-9a-f]{32})/acknowledge", path
        )
        if check_run_match:
            return self.server.controller.acknowledge_check_run(
                check_run_match.group(1), payload
            )
        routes = {
            "/api/v1/discoveries": self.server.controller.submit_discovery,
            "/api/v1/preferences/scan-roots/picker": (
                self.server.controller.submit_scan_root_picker
            ),
            "/api/v1/preferences/scan-roots": self.server.controller.update_scan_roots,
            "/api/v1/remotes/preview": self.server.controller.preview_remote,
            "/api/v1/repository-clones": (
                self.server.controller.submit_repository_clone
            ),
            "/api/v1/repositories/profile": self.server.controller.update_repository_profile,
            "/api/v1/watches": self.server.controller.submit_watches,
            "/api/v1/checks": self.server.controller.submit_check,
            "/api/v1/publications": self.server.controller.submit_publish,
            "/api/v1/integrations/refresh": self.server.controller.submit_integration_refresh,
            "/api/v1/lark/connect": self.server.controller.submit_lark_connect,
            "/api/v1/lark/create": self.server.controller.submit_lark_create,
        }
        function = routes.get(path)
        if function is None:
            raise RequestBodyError(HTTPStatus.NOT_FOUND, "not_found", "没有找到该接口")
        return function(payload)

    def _route_delete(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        prefix = "/api/v1/watches/"
        if not path.startswith(prefix) or "/" in path[len(prefix):]:
            raise RequestBodyError(
                HTTPStatus.NOT_FOUND, "not_found", "没有找到该接口"
            )
        value = path[len(prefix):]
        try:
            branch_id = _parse_positive_integer(value, "watch_id")
        except ServiceError as exc:
            raise ServiceError("关注编号无效", code="invalid_watch_id") from exc
        return self.server.controller.remove_watch(branch_id, payload)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST, "invalid_length", "请求长度无效"
            ) from exc
        if length < 0 or length > MAX_BODY_BYTES:
            raise RequestBodyError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "请求内容过大",
            )
        try:
            body = self.rfile.read(length)
        finally:
            self._body_pending = False
        try:
            value = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST, "invalid_json", "JSON 格式无效"
            ) from exc
        if not isinstance(value, dict):
            raise RequestBodyError(
                HTTPStatus.BAD_REQUEST, "object_required", "请求内容必须是 JSON object"
            )
        return value

    def _accept_request(self, *, mutating: bool = False) -> bool:
        """Apply every entry check, or answer the request and return False.

        Kept in one place so a new verb cannot be added with one of the checks
        quietly missing. ``mutating`` adds the checks that only a state-changing
        request needs: a same-origin caller, the session token, and a JSON body.
        """
        if not self._valid_host():
            self._json_error(HTTPStatus.FORBIDDEN, "invalid_host", "请求来源无效")
            return False
        if not mutating:
            return True
        if not self._valid_origin():
            self._json_error(HTTPStatus.FORBIDDEN, "invalid_origin", "请求来源无效")
            return False
        if not self._valid_token():
            return False
        content_type = (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        )
        if content_type != "application/json":
            self._json_error(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "json_required",
                "请求必须使用 JSON",
            )
            return False
        return True

    def _valid_token(self) -> bool:
        # compare_digest keeps the reply time independent of how much of the
        # token a caller guessed right.
        supplied = self.headers.get("X-Branch-Radar-Token") or ""
        if secrets.compare_digest(supplied, self.server.csrf_token):
            return True
        self._json_error(
            HTTPStatus.FORBIDDEN,
            "invalid_token",
            "页面会话已失效，请从启动终端输出的安全链接重新打开",
        )
        return False

    def _valid_host(self) -> bool:
        raw = self.headers.get("Host")
        if not raw:
            return False
        try:
            parsed = urlsplit("//" + raw)
            port = parsed.port
        except ValueError:
            return False
        return (
            parsed.hostname in {"127.0.0.1", "localhost"}
            and port == self.server.server_port
        )

    def _valid_origin(self) -> bool:
        raw = self.headers.get("Origin")
        if not raw:
            return False
        try:
            parsed = urlsplit(raw)
            port = parsed.port or (80 if parsed.scheme == "http" else None)
        except ValueError:
            return False
        host = urlsplit("//" + (self.headers.get("Host") or ""))
        return (
            parsed.scheme == "http"
            and parsed.hostname == host.hostname
            and port == self.server.server_port
        )

    def _method_not_allowed(self) -> None:
        self._json_error(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "该操作不受支持",
        )

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        # The dashboard is re-read after every job, and its watch list dominates
        # the body, so an uncompressed reply wastes far more than the CPU that
        # compressing it costs. Below the threshold a single segment carries the
        # body either way and gzip only adds framing.
        encoding: str | None = None
        if len(content) >= JSON_GZIP_MIN_BYTES and _accepts_gzip(
            self.headers.get("Accept-Encoding", "")
        ):
            compressed = gzip.compress(content, compresslevel=6, mtime=0)
            if len(compressed) < len(content):
                content = compressed
                encoding = "gzip"
        self._send_bytes(
            status,
            content,
            "application/json; charset=utf-8",
            content_encoding=encoding,
            vary="Accept-Encoding",
        )

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._send_json(status, {
            "ok": False,
            "error": {
                "code": code,
                "message": redact_sensitive_text(message)[:1000],
            },
        })

    def _send_resource(self, filename: str, content_type: str, *, cache: bool) -> None:
        content, compressed, etag = _resource_payload(filename)
        cache_control = "private, max-age=0, must-revalidate" if cache else "no-store"
        if cache and self.headers.get("If-None-Match") == etag:
            self._send_bytes(
                HTTPStatus.NOT_MODIFIED,
                b"",
                content_type,
                cache_control=cache_control,
                etag=etag,
                vary="Accept-Encoding",
            )
            return
        use_gzip = len(compressed) < len(content) and _accepts_gzip(
            self.headers.get("Accept-Encoding", "")
        )
        self._send_bytes(
            HTTPStatus.OK,
            compressed if use_gzip else content,
            content_type,
            cache_control=cache_control,
            etag=etag if cache else None,
            content_encoding="gzip" if use_gzip else None,
            vary="Accept-Encoding",
        )

    def _send_bytes(
        self,
        status: HTTPStatus,
        content: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
        etag: str | None = None,
        content_encoding: str | None = None,
        vary: str | None = None,
    ) -> None:
        # Answering without consuming the request body leaves those bytes in the
        # stream; closing is cheaper and safer than draining input this request
        # already decided to reject. Say so, so the client stops reusing it too.
        closing = self._body_pending
        self.send_response(int(status))
        if closing:
            self.send_header("Connection", "close")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        if etag:
            self.send_header("ETag", etag)
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        if vary:
            self.send_header("Vary", vary)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class RequestBodyError(ValueError):
    def __init__(self, status: HTTPStatus, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


def create_server(service: RadarService, *, port: int = 8765) -> RadarHTTPServer:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("端口必须在 0 到 65535 之间")
    return RadarHTTPServer(("127.0.0.1", port), WebController(service))


def serve_web(
    *,
    port: int = 8765,
    open_browser: bool = True,
    service: RadarService | None = None,
) -> int:
    service = service or RadarService.create()
    server = create_server(service, port=port)
    launch_url = server.launch_url
    print("\n  Branch Radar Web 已启动")
    print("  %s" % launch_url)
    print("  仅本机可访问；按 Ctrl-C 安全退出\n")
    timer: threading.Timer | None = None
    if open_browser:
        timer = threading.Timer(0.35, webbrowser.open_new_tab, args=(launch_url,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if timer is not None:
            timer.cancel()
        server.shutdown()
        server.server_close()
        if server.controller.jobs.active() is not None:
            print("  正在等待当前任务安全结束…")
        server.controller.close()
    return 0


@lru_cache(maxsize=16)
def _resource_payload(filename: str) -> tuple[bytes, bytes, str]:
    content = (
        resources.files("branch_radar.resources")
        .joinpath("web")
        .joinpath(filename)
        .read_bytes()
    )
    compressed = gzip.compress(content, compresslevel=6, mtime=0)
    etag = 'W/"%s"' % hashlib.sha256(content).hexdigest()[:24]
    return content, compressed, etag


def _window_cutoff(since_hours: int | None) -> str | None:
    """Turn a lookback in hours into an absolute cutoff.

    The window is expressed in hours rather than as a client timestamp so the
    cutoff is anchored to the same clock that stamped the events, instead of to
    whatever the browser believes the time is.
    """
    if since_hours is None:
        return None
    if since_hours > MAX_EVENT_WINDOW_HOURS:
        raise ServiceError(
            "since_hours 最多为 %d" % MAX_EVENT_WINDOW_HOURS, code="invalid_query"
        )
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    return cutoff.isoformat(timespec="seconds")


def _event_window_cutoff(query: str) -> str | None:
    if not query:
        return None
    try:
        parameters = parse_qs(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=2
        )
    except ValueError as exc:
        raise ServiceError("查询参数无效", code="invalid_query") from exc
    if set(parameters) - {"since_hours"}:
        raise ServiceError("查询包含不支持的参数", code="invalid_query")
    if any(len(values) != 1 for values in parameters.values()):
        raise ServiceError("查询参数不能重复", code="invalid_query")
    return _window_cutoff(_positive_query_integer(parameters, "since_hours"))


def _job_wait_parameters(query: str) -> tuple[float, int | None]:
    """Read the optional long-poll window from a job request.

    Without ``wait`` the reply is immediate, which keeps the endpoint usable by
    scripts and by any client that would rather ask again on its own schedule.
    """
    if not query:
        return 0.0, None
    try:
        parameters = parse_qs(
            query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
        )
    except ValueError as exc:
        raise ServiceError("任务查询参数无效", code="invalid_query") from exc
    if set(parameters) - {"wait", "revision"}:
        raise ServiceError("任务查询包含不支持的参数", code="invalid_query")
    if any(len(values) != 1 for values in parameters.values()):
        raise ServiceError("任务查询参数不能重复", code="invalid_query")

    wait_seconds = 0.0
    raw_wait = parameters.get("wait", ["0"])[0]
    if raw_wait:
        try:
            wait_seconds = float(raw_wait)
        except ValueError as exc:
            raise ServiceError("wait 必须是数字", code="invalid_query") from exc
        if not 0 <= wait_seconds <= MAX_JOB_WAIT_SECONDS:
            raise ServiceError(
                "wait 必须在 0 到 %d 秒之间" % MAX_JOB_WAIT_SECONDS,
                code="invalid_query",
            )

    revision: int | None = None
    raw_revision = parameters.get("revision", [""])[0]
    if raw_revision:
        revision = _parse_positive_integer(raw_revision, "revision")
    return wait_seconds, revision


def _accepts_gzip(value: str) -> bool:
    for item in value.lower().split(","):
        parts = [part.strip() for part in item.split(";")]
        if not parts or parts[0] != "gzip":
            continue
        quality = 1.0
        for parameter in parts[1:]:
            if not parameter.startswith("q="):
                continue
            try:
                quality = float(parameter[2:])
            except ValueError:
                quality = 0.0
        return quality > 0
    return False


def _boolean(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ServiceError("%s 必须是布尔值" % key, code="invalid_option")
    return value


def _contains_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _positive_query_integer(
    parameters: dict[str, list[str]],
    key: str,
    *,
    default: int | None = None,
) -> int | None:
    values = parameters.get(key)
    if values is None:
        return default
    return _parse_positive_integer(values[0], key)


def _parse_positive_integer(value: str, key: str) -> int:
    if not value.isascii() or not value.isdecimal():
        raise ServiceError("%s 必须是正整数" % key, code="invalid_query")
    if len(value) > 19:
        raise ServiceError("%s 超出支持范围" % key, code="invalid_query")
    parsed = int(value)
    if parsed <= 0 or parsed > 9223372036854775807:
        raise ServiceError("%s 必须是正整数" % key, code="invalid_query")
    return parsed
