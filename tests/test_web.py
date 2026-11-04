from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from branch_radar.cli import Application, build_parser
from branch_radar.folder_picker import FolderPickerError, FolderPickResult
from branch_radar.services import RadarService, ServiceError
from branch_radar.web.jobs import JobManager
from branch_radar.web.server import (
    INTEGRATION_CACHE_TTL_SECONDS,
    MAX_BODY_BYTES,
    RadarHTTPServer,
    WebController,
    create_server,
)
from branch_radar.web.views import (
    check_run_view,
    dashboard_view,
    event_view,
    integration_view,
    watch_view,
)


class _FakeDatabase:
    def __init__(self) -> None:
        self.events: dict[int, dict[str, Any]] = {}

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        return self.events.get(event_id)

    def list_events(
        self,
        *,
        limit: int = 50,
        repository_id: int | None = None,
        branch_id: int | None = None,
        before_id: int | None = None,
        since: str | None = None,
        include_baseline: bool = True,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        values = sorted(self.events.values(), key=lambda row: row["id"], reverse=True)
        if not include_baseline:
            values = [row for row in values if row.get("kind") != "baseline"]
        if since is not None:
            values = [row for row in values if str(row.get("detected_at") or "") >= since]
        if repository_id is not None:
            values = [row for row in values if row.get("repository_id") == repository_id]
        if branch_id is not None:
            values = [row for row in values if row.get("branch_id") == branch_id]
        if before_id is not None:
            values = [row for row in values if row["id"] < before_id]
        return values[:limit]


class _FakeService:
    """A deterministic service double that never invokes Git, Codex, or Lark."""

    def __init__(self, home: Path) -> None:
        self.paths = SimpleNamespace(home=home)
        self.database = _FakeDatabase()
        self.discovery_blocked = False
        self.discovery_started = threading.Event()
        self.discovery_release = threading.Event()
        self.clone_blocked = False
        self.clone_started = threading.Event()
        self.clone_release = threading.Event()
        self.clone_cancelled = threading.Event()
        self.clone_cancel_exception: Exception | None = None
        self.clone_calls = []
        self.watch_blocked = False
        self.watch_started = threading.Event()
        self.watch_release = threading.Event()
        self.lark_create_blocked = False
        self.lark_create_started = threading.Event()
        self.lark_create_release = threading.Event()
        self.lark_create_calls = 0
        self.lark_create_titles = []
        self.check_calls = 0
        self.last_dashboard_since: str | None = None
        self.last_check_branch_ids = None
        self.last_check_repository_id = None
        self.check_results = []
        self.check_publications = []
        self.last_check_run_value = None
        self.integration_calls = 0
        self.repository_profile_updates = []
        self.removed_watch_ids = []
        self.preference_updates = []
        self.acknowledged_check_runs = []
        self.monitoring_updates = []
        self.monitoring_value = {
            "enabled": False,
            "interval_seconds": 900,
            "notifications_enabled": False,
            "quiet_hours": {"enabled": False, "start": "22:00", "end": "08:00"},
            "locale": "zh-CN",
            "supported": True,
            "platform": "launchd",
            "installed": False,
            "loaded": False,
            "notifications_supported": True,
            "notification_provider": "osascript",
            "last_attempt_at": None,
            "last_success_at": None,
            "next_run_at": None,
            "error": None,
        }

    def dashboard(self, *, since: str | None = None) -> dict[str, Any]:
        self.last_dashboard_since = since
        event_rows = self.database.list_events(
            limit=41, include_baseline=False, since=since
        )
        has_more = len(event_rows) > 40
        events = event_rows[:40]
        return {
            "counts": {
                "repositories": 1,
                "branches": 1,
                "branch_errors": 0,
                "events": len(self.database.events),
                "pending_deliveries": 0,
            },
            "watches": [],
            "events": events,
            "events_has_more": has_more,
            "events_next_before_id": events[-1]["id"] if has_more and events else None,
            "settings": {
                "scan_roots": [str(self.paths.home)],
                "scan_max_depth": 5,
                "poll_interval_seconds": 900,
                "codex_enabled": False,
                "lark_enabled": False,
                "lark_document": None,
            },
        }

    def integrations(self) -> dict[str, Any]:
        self.integration_calls += 1
        return {
            "codex": {"available": False, "authenticated": False},
            "lark": {"available": False, "verified": False, "document": None},
        }

    def monitoring_status(self) -> dict[str, Any]:
        return dict(self.monitoring_value)

    def configure_monitoring(self, **values) -> dict[str, Any]:
        self.monitoring_updates.append(values)
        self.monitoring_value.update({
            "enabled": values["enabled"],
            "interval_seconds": values["interval_seconds"],
            "notifications_enabled": values["notifications_enabled"],
            "quiet_hours": {
                "enabled": values["quiet_hours_enabled"],
                "start": values["quiet_hours_start"],
                "end": values["quiet_hours_end"],
            },
            "locale": values["locale"],
            "installed": values["enabled"],
            "loaded": values["enabled"],
        })
        return dict(self.monitoring_value)

    def discover(self, roots=None) -> list[dict[str, Any]]:
        self.discovery_started.set()
        if self.discovery_blocked:
            if not self.discovery_release.wait(timeout=5):
                raise RuntimeError("test discovery release timed out")
        root = (roots or [str(self.paths.home)])[0]
        return [{
            "repository_id": 10,
            "name": "demo",
            "path": str(Path(root) / "demo"),
            "remote_name": "origin",
            "remote_url": "git@example.invalid:lab/demo.git",
            "canonical_remote": "example.invalid/lab/demo",
            "current_branch": "main",
            "upstream_branch": "main",
            "default_branch": "main",
            "is_worktree": False,
            "branches": ["main", "develop"],
            "watched_branches": ["main"],
            "error": None,
        }]

    def add_watches(self, entries, *, progress=None) -> dict[str, Any]:
        if progress:
            progress({
                "phase": "fetch",
                "label": "正在获取所选分支的远程引用",
                "detail": "仅获取本次选择的分支。",
                "current": 0,
                "total": sum(len(entry.get("branches") or ["main"]) for entry in entries),
                "indeterminate": True,
            })
        self.watch_started.set()
        if self.watch_blocked:
            if not self.watch_release.wait(timeout=5):
                raise RuntimeError("test watch release timed out")
        watched = []
        for index, entry in enumerate(entries, start=1):
            for branch in entry.get("branches") or ["main"]:
                watched.append({
                    "repository_id": 10,
                    "branch_id": 20 + index,
                    "repository": "demo",
                    "branch": branch,
                    "baseline_pending": False,
                })
        return {"watched": watched, "skipped": []}

    def clone_repository(
        self,
        url,
        parent,
        *,
        directory_name=None,
        progress=None,
        cancel_event=None,
    ) -> dict[str, Any]:
        self.clone_calls.append({
            "url": url,
            "parent": parent,
            "directory_name": directory_name,
            "has_progress": progress is not None,
            "has_cancel_event": cancel_event is not None,
        })
        if progress:
            progress({
                "phase": "clone",
                "label": "正在克隆仓库",
                "detail": "正在接收 Git 对象。",
                "current": 0,
                "total": 1,
                "indeterminate": True,
            })
        self.clone_started.set()
        if self.clone_blocked:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if cancel_event is not None and cancel_event.is_set():
                    self.clone_cancelled.set()
                    if self.clone_cancel_exception is not None:
                        raise self.clone_cancel_exception
                    raise ServiceError("克隆已取消", code="clone_cancelled")
                if self.clone_release.wait(timeout=0.01):
                    break
            else:
                raise RuntimeError("test clone release timed out")
        path = Path(parent) / (directory_name or "demo")
        return {
            "repository": {
                "repository_id": None,
                "name": directory_name or "demo",
                "path": str(path),
                "remote_name": "origin",
                "remote_url": "https://token@github.com/lab/demo.git",
                "canonical_remote": "github.com/lab/demo",
                "current_branch": "main",
                "upstream_branch": "main",
                "default_branch": "main",
                "is_worktree": False,
                "branches": ["main", "develop"],
                "watched_branches": [],
                "error": None,
            },
            "reused": False,
        }

    def update_repository_profile(self, repository_id, *, display_name, tags):
        self.repository_profile_updates.append({
            "repository_id": repository_id,
            "display_name": display_name,
            "tags": list(tags),
        })
        return {
            "id": repository_id,
            "name": "demo",
            "display_name": display_name,
            "tags_json": json.dumps(tags, ensure_ascii=False),
        }

    def update_preferences(self, *, scan_roots=None, **_changes):
        roots = list(scan_roots or [])
        self.preference_updates.append(roots)
        return SimpleNamespace(scan_roots=roots)

    def remove_watch(self, branch_id):
        self.removed_watch_ids.append(branch_id)
        return {
            "branch_id": branch_id,
            "repository_id": 10,
            "repository": "demo",
            "branch": "main",
        }

    preview_remote = RadarService.preview_remote

    def check(
        self, *, fetch=True, use_codex=False, publish=False, branch_ids=None,
        repository_id=None, progress=None,
    ):
        self.check_calls += 1
        self.last_check_branch_ids = branch_ids
        self.last_check_repository_id = repository_id
        if progress:
            progress({
                "phase": "resolve",
                "label": "正在解析分支引用",
                "detail": "已核对所选分支。",
                "current": 1,
                "total": 1,
                "indeterminate": False,
            })
        return self.check_results, self.check_publications

    def last_check_run(self):
        return self.last_check_run_value

    def acknowledge_check_run(self, run_id):
        if run_id == "b" * 32:
            raise ServiceError("没有找到该检查批次", code="check_run_not_found")
        self.acknowledged_check_runs.append(run_id)
        return {
            "id": run_id,
            "status": "succeeded",
            "started_at": "2026-08-21T00:00:00+00:00",
            "finished_at": "2026-08-21T00:00:01+00:00",
            "acknowledged_at": "2026-08-21T00:00:02+00:00",
            "scope_kind": "all",
            "requested_branch_ids": [],
            "actual_branch_ids": [],
            "events": [],
            "event_ids": [],
            "blocked": [],
            "summary": None,
        }

    def publish(self, *, limit=1000):
        return []

    def set_lark_document(self, document: str) -> dict[str, Any]:
        return {"document": document}

    def create_lark_document(
        self, title: str | None = None, *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.lark_create_calls += 1
        self.lark_create_titles.append(title)
        self.lark_create_started.set()
        if self.lark_create_blocked:
            if not self.lark_create_release.wait(timeout=5):
                raise RuntimeError("test Lark release timed out")
        return {
            "url": "https://example.feishu.invalid/docx/test",
            "title": title,
        }

    def release_jobs(self) -> None:
        self.discovery_release.set()
        self.clone_release.set()
        self.watch_release.set()
        self.lark_create_release.set()


class WebCliTests(unittest.TestCase):
    def test_web_arguments_and_command_forwarding(self):
        args = build_parser().parse_args([
            "web", "--port", "0", "--no-open", "--no-color",
        ])
        self.assertEqual(args.handler, "command_web")
        self.assertEqual(args.port, 0)
        self.assertFalse(args.open_browser)
        self.assertTrue(args.no_color)

        defaults = build_parser().parse_args(["web"])
        self.assertEqual(defaults.port, 8765)
        self.assertTrue(defaults.open_browser)

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"BRANCH_RADAR_HOME": directory}
        ):
            app = Application(args)
            with mock.patch("branch_radar.web.serve_web", return_value=23) as serve:
                self.assertEqual(app.command_web(), 23)
            kwargs = serve.call_args.kwargs
            self.assertEqual(kwargs["port"], 0)
            self.assertFalse(kwargs["open_browser"])
            self.assertIsNotNone(kwargs["service"])


class WebViewTests(unittest.TestCase):
    def test_check_run_exposes_backlog_publication_failures_as_actionable(self):
        payload = check_run_view({
            "id": "a" * 32,
            "status": "completed_with_errors",
            "started_at": "2026-08-21T00:00:00+00:00",
            "finished_at": "2026-08-21T00:00:01+00:00",
            "scope_kind": "all",
            "requested_branch_ids": [],
            "actual_branch_ids": [],
            "events": [],
            "event_ids": [],
            "blocked": [],
            "publication_backlog_failed_count": 2,
        })

        self.assertTrue(payload["actionable"])
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["publication"]["failed"], 0)
        self.assertEqual(payload["publication"]["backlog"]["failed"], 2)

    def test_check_run_requested_count_is_not_limited_by_scope_projection(self):
        branch_ids = list(range(1, 702))
        payload = check_run_view({
            "id": "a" * 32,
            "status": "succeeded",
            "scope_kind": "repository",
            "requested_branch_ids": [],
            "actual_branch_ids": branch_ids,
            "events": [],
            "event_ids": [],
            "blocked": [],
        })
        self.assertEqual(payload["counts"]["requested"], 701)
        self.assertEqual(len(payload["scope"]["actual_branch_ids"]), 500)

    def test_compact_event_exposes_only_triage_metadata(self):
        payload = event_view({
            "id": 7,
            "repository_id": 2,
            "branch_id": 3,
            "repository": "app",
            "branch": "main",
            "kind": "fast_forward",
            "detected_at": "2026-08-21T00:00:00+00:00",
            "from_oid": "a" * 40,
            "to_oid": "b" * 40,
            "merge_base_oid": "a" * 40,
            "summary": {"title": "API 变化", "overview": "更新接口"},
            "authors": ["Alice"],
            "check_run_id": "a" * 32,
            "is_new": 1,
            "list_metadata": {
                "analysis_complete": False,
                "timeline": {
                    "basis": "tip_commit",
                    "tip_committed_at": "2026-08-20T10:00:00+08:00",
                    "first_committed_at": "2026-08-20T01:00:00Z",
                    "last_committed_at": "2026-08-20T10:00:00+08:00",
                    "range_complete": True,
                    "authors": [{
                        "name": "Alice",
                        "first_committed_at": "2026-08-20T01:00:00Z",
                        "last_committed_at": "2026-08-20T10:00:00+08:00",
                        "sampled_commits": 2,
                    }],
                },
                "categories": ["api", "database"],
                "signals": [
                    "API 相关文件 3 个",
                    "提交说明包含 breaking change 信号",
                ],
                "stats": {
                    "commits": 2,
                    "files": 4,
                    "insertions": 120,
                    "deletions": 20,
                },
            },
        })
        self.assertEqual(payload["authors"], ["Alice"])
        self.assertEqual(payload["categories"], ["api", "database"])
        self.assertEqual(payload["signals"], ["提交说明包含 breaking change 信号"])
        self.assertEqual(payload["stats"]["changes"], 140)
        self.assertTrue(payload["is_new"])
        self.assertFalse(payload["analysis_complete"])
        self.assertEqual(payload["timeline"], {
            "basis": "tip_commit",
            "tip_committed_at": "2026-08-20T02:00:00+00:00",
            "first_committed_at": "2026-08-20T01:00:00+00:00",
            "last_committed_at": "2026-08-20T02:00:00+00:00",
            "range_complete": True,
            "authors": [{
                "name": "Alice",
                "first_committed_at": "2026-08-20T01:00:00+00:00",
                "last_committed_at": "2026-08-20T02:00:00+00:00",
                "sampled_commits": 2,
            }],
        })
        self.assertEqual(payload["check_run_id"], "a" * 32)
        self.assertEqual(payload["topology"], {
            "from_oid": "a" * 40,
            "to_oid": "b" * 40,
            "merge_base_oid": "a" * 40,
        })

    def test_compact_event_timeline_rejects_invalid_or_unbounded_metadata(self):
        authors = [
            {
                "name": "Author %02d" % index,
                "first_committed_at": "2026-08-20T00:00:00",
                "last_committed_at": "invalid",
                "sampled_commits": 2_147_483_648,
            }
            for index in range(13)
        ]
        authors[0]["name"] = "token=secret\nAlice"
        payload = event_view({
            "list_metadata": {
                "timeline": {
                    "basis": "tip_commit",
                    "tip_committed_at": "not-a-time",
                    "first_committed_at": "2026-08-20T10:00:00+08:00",
                    "last_committed_at": "2026-08-20T11:00:00+08:00",
                    "range_complete": 1,
                    "authors": authors,
                },
            },
        })

        timeline = payload["timeline"]
        self.assertEqual(timeline["basis"], "detected")
        self.assertIsNone(timeline["tip_committed_at"])
        self.assertEqual(
            timeline["first_committed_at"], "2026-08-20T02:00:00+00:00"
        )
        self.assertEqual(len(timeline["authors"]), 12)
        self.assertEqual(timeline["authors"][0]["name"], "token=<redacted> Alice")
        self.assertIsNone(timeline["authors"][0]["first_committed_at"])
        self.assertEqual(timeline["authors"][0]["sampled_commits"], 2_147_483_647)

    def test_compact_event_topology_keeps_missing_or_invalid_evidence_null(self):
        payload = event_view({
            "from_oid": "0" * 40,
            "to_oid": "B" * 40,
            "merge_base_oid": "not-an-object-id",
        })

        self.assertEqual(payload["topology"], {
            "from_oid": None,
            "to_oid": "b" * 40,
            "merge_base_oid": None,
        })

    def test_detailed_event_orders_commits_by_commit_time_stably(self):
        def commit(
            subject: str,
            commit_date: str | None,
            author_date: str,
        ) -> dict[str, Any]:
            return {
                "sha": subject,
                "subject": subject,
                "author_name": "Alice",
                "author_date": author_date,
                "committer_name": "Integrator",
                "commit_date": commit_date,
                "parents": [],
            }

        payload = event_view(
            {
                "facts": {
                    "commits": [
                        commit(
                            "oldest",
                            "2026-08-18T10:00:00+00:00",
                            "2026-08-30T00:00:00+00:00",
                        ),
                        commit(
                            "tie-first",
                            "2026-08-19T10:00:00+00:00",
                            "2026-08-01T00:00:00+00:00",
                        ),
                        commit("unknown", None, "2026-09-01T00:00:00+00:00"),
                        commit(
                            "invalid",
                            "not-a-timestamp",
                            "2026-10-01T00:00:00+00:00",
                        ),
                        commit(
                            "newest",
                            "2026-08-20T10:00:00+00:00",
                            "2026-07-01T00:00:00+00:00",
                        ),
                        commit(
                            "tie-second",
                            "2026-08-19T10:00:00+00:00",
                            "2026-08-02T00:00:00+00:00",
                        ),
                    ],
                    "dropped_commits": [],
                    "diff": {},
                },
            },
            detailed=True,
        )

        self.assertEqual(
            [item["subject"] for item in payload["commits"]],
            [
                "newest",
                "tie-first",
                "tie-second",
                "oldest",
                "unknown",
                "invalid",
            ],
        )

    def test_detailed_event_selects_latest_commits_before_applying_view_limit(self):
        commits = [
            {
                "sha": "commit-%02d" % index,
                "subject": "commit-%02d" % index,
                "author_name": "Alice",
                "author_date": "2026-08-20T00:%02d:00+00:00" % index,
                "committer_name": "Integrator",
                "commit_date": "2026-08-20T00:%02d:00+00:00" % index,
                "parents": [],
            }
            for index in range(55)
        ]

        payload = event_view(
            {"facts": {"commits": commits, "dropped_commits": [], "diff": {}}},
            detailed=True,
        )

        self.assertEqual(len(payload["commits"]), 50)
        self.assertEqual(payload["commits"][0]["subject"], "commit-54")
        self.assertEqual(payload["commits"][-1]["subject"], "commit-05")

    def test_dashboard_exposes_configured_scan_roots_without_other_paths(self):
        payload = dashboard_view({
            "settings": {
                "scan_roots": ["~/projects", "/srv/repositories"],
                "scan_max_depth": 5,
                "poll_interval_seconds": 900,
                "codex_enabled": False,
                "lark_enabled": False,
                "lark_document": None,
                "internal_state_path": "/private/state.db",
            }
        })

        self.assertEqual(
            payload["settings"]["scan_roots"],
            ["~/projects", "/srv/repositories"],
        )
        self.assertNotIn("scan_max_depth", payload["settings"])
        self.assertNotIn("internal_state_path", json.dumps(payload))

    def test_dashboard_bounds_and_filters_scan_root_labels(self):
        roots = ["~/repository-%02d" % index for index in range(20)]
        roots[1] = ""
        roots[2] = 42
        roots[3] = "x" * 4097

        payload = dashboard_view({"settings": {"scan_roots": roots}})

        self.assertEqual(
            payload["settings"]["scan_roots"],
            [roots[0], *roots[4:16]],
        )

    def test_deleted_branch_is_not_misreported_as_waiting_for_baseline(self):
        payload = watch_view({
            "branch_id": 7,
            "repository_id": 3,
            "repository": "demo",
            "branch": "main",
            "present": 0,
            "last_observed_oid": None,
            "last_checked_at": "2026-08-20T10:00:00+00:00",
            "last_success_at": "2026-08-20T10:00:00+00:00",
            "last_error": None,
        })

        self.assertFalse(payload["present"])
        self.assertFalse(payload["baseline_pending"])

        pending = watch_view({
            "branch_id": 8,
            "repository_id": 3,
            "repository": "demo",
            "branch": "release",
            "present": 0,
            "last_observed_oid": None,
            "last_success_at": None,
            "last_error": None,
        })
        self.assertTrue(pending["baseline_pending"])

    def test_dashboard_exposes_daemon_heartbeat_without_internal_errors(self):
        payload = dashboard_view({
            "daemon_health": {
                "last_attempt_at": "2026-08-20T10:00:00+00:00",
                "last_success_at": "2026-08-20T09:59:00+00:00",
                "last_failure_at": "2026-08-20T09:58:00+00:00",
                "consecutive_failures": "2",
                "last_error": "traceback /Users/private token=secret",
                "last_prune_at": "2026-08-01T00:00:00+00:00",
            }
        })
        self.assertEqual(payload["daemon_health"]["consecutive_failures"], 2)
        self.assertEqual(
            set(payload["daemon_health"]),
            {
                "last_attempt_at",
                "last_success_at",
                "last_failure_at",
                "consecutive_failures",
            },
        )
        self.assertNotIn("private", json.dumps(payload))
        self.assertNotIn("secret", json.dumps(payload))

    def test_integration_identity_view_is_bounded_and_does_not_expose_secrets(self):
        payload = integration_view({
            "codex": {
                "detected": True,
                "available": True,
                "authenticated": True,
                "version": "codex-cli " + ("v" * 200),
                "auth_mode": "chatgpt",
                "login": "Logged in using ChatGPT as private@example.com",
                "binary": "/private/bin/codex",
                "error": "auth failed for private@example.com at /Users/private/.codex",
            },
            "lark": {
                "available": True,
                "verified": True,
                "version": "lark-cli 1.0.86",
                "user_name": "赵" * 200,
                "document": "https://example.invalid/docx/private-token",
                "openId": "ou_private",
                "access_token": "secret-value",
                "scopes": ["docx:document:write_only"],
                "error": "keychain failed for ou_private with token=secret-value",
            },
        })

        self.assertEqual(payload["codex"]["auth_mode"], "chatgpt")
        self.assertEqual(len(payload["codex"]["version"]), 120)
        self.assertEqual(len(payload["lark"]["user_name"]), 120)
        self.assertEqual(
            payload["codex"]["error"],
            "Codex 身份验证失败，请在普通 Terminal 中重新登录。",
        )
        self.assertEqual(
            payload["lark"]["error"],
            "飞书：无法读取系统钥匙串，请在普通 Terminal 中重新登录。",
        )
        self.assertEqual(
            set(payload["codex"]),
            {"detected", "available", "authenticated", "auth_mode", "version", "error"},
        )
        self.assertEqual(
            set(payload["lark"]),
            {
                "available", "verified", "version", "user_name",
                "document_configured", "document_name", "document_url", "error",
            },
        )
        self.assertIsNone(payload["lark"]["document_name"])
        self.assertIsNone(payload["lark"]["document_url"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "private@example.com", "/private/bin/codex", "private-token",
            "ou_private", "secret-value", "docx:document:write_only",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_integration_view_exposes_only_trusted_lark_document_links(self):
        payload = integration_view({
            "lark": {
                "verified": True,
                "document": "docx-safe",
                "document_details": {
                    "title": "研发项目动态",
                    "url": "https://team.feishu.cn/docx/docx-safe?from=copy#section",
                },
            },
        })
        self.assertEqual(payload["lark"]["document_name"], "研发项目动态")
        self.assertEqual(
            payload["lark"]["document_url"],
            "https://team.feishu.cn/docx/docx-safe",
        )

        for unsafe in (
            "javascript:alert(1)",
            "http://team.feishu.cn/docx/docx-safe",
            "https://evil.example/docx/docx-safe",
            "https://team.feishu.cn.evil.example/docx/docx-safe",
            "https://user:pass@team.feishu.cn/docx/docx-safe",
            "https://team.feishu.cn:8443/docx/docx-safe",
            "https://team.feishu.cn/open-apis/docx/v1/documents/docx-safe",
        ):
            with self.subTest(unsafe=unsafe):
                value = integration_view({
                    "lark": {"document": unsafe, "document_details": {}}
                })
                self.assertIsNone(value["lark"]["document_url"])

    def test_integration_identity_view_rejects_non_string_labels(self):
        payload = integration_view({
            "codex": {"authenticated": True, "auth_mode": {"unexpected": True}},
            "lark": {"user_name": {"unexpected": True}, "version": ["bad"]},
        })
        self.assertEqual(payload["codex"]["auth_mode"], "authenticated")
        self.assertIsNone(payload["lark"]["user_name"])
        self.assertIsNone(payload["lark"]["version"])


class CreateServerTests(unittest.TestCase):
    def test_queued_clone_cancellation_is_immediately_terminal(self):
        worker = mock.Mock()
        with mock.patch("branch_radar.web.jobs.ThreadPoolExecutor") as executor_type:
            manager = JobManager()
            submitted = manager.submit(
                "clone",
                worker,
                cancellable=True,
            )
            cancelled = manager.cancel(submitted["id"])

        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error_code"], "clone_cancelled")
        self.assertIsNone(manager.active())
        worker.assert_not_called()
        manager.shutdown()
        executor_type.return_value.shutdown.assert_called_once_with(
            wait=True, cancel_futures=True
        )

    def test_clone_success_wins_a_cancel_race_after_worker_completion(self):
        manager = JobManager()
        started = threading.Event()

        def worker(cancel_event):
            started.set()
            if not cancel_event.wait(timeout=2):
                raise AssertionError("test did not request cancellation")
            return {"repository": "already-finalized"}

        submitted = manager.submit("clone", worker, cancellable=True)
        self.assertTrue(started.wait(timeout=1))
        cancelling = manager.cancel(submitted["id"])
        self.assertIsNotNone(cancelling)

        deadline = time.monotonic() + 2
        completed = None
        while time.monotonic() < deadline:
            completed = manager.get(submitted["id"])
            if completed and completed["status"] == "succeeded":
                break
            time.sleep(0.01)
        manager.shutdown()

        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(
            completed["result"], {"repository": "already-finalized"}
        )
        self.assertEqual(completed["progress"]["phase"], "complete")

    def test_controller_close_cancels_a_running_folder_picker_promptly(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = WebController(_FakeService(Path(directory)))
            picker_started = threading.Event()

            def picker_factory(*, cancel_event):
                picker = mock.Mock()

                def pick():
                    picker_started.set()
                    if not cancel_event.wait(timeout=3):
                        raise AssertionError("controller did not cancel folder picker")
                    return FolderPickResult(cancelled=True)

                picker.pick.side_effect = pick
                return picker

            with mock.patch(
                "branch_radar.web.server.FolderPicker",
                side_effect=picker_factory,
            ):
                job = controller.submit_scan_root_picker({})
                self.assertTrue(picker_started.wait(timeout=1))
                started = time.monotonic()
                controller.close()

            self.assertLess(time.monotonic() - started, 1.0)
            completed = controller.jobs.get(job["id"])
            self.assertIsNotNone(completed)
            assert completed is not None
            self.assertEqual(completed["status"], "succeeded")
            self.assertEqual(
                completed["result"],
                {"cancelled": True, "scan_root": None},
            )

    def test_port_zero_binds_loopback_and_selects_a_real_port(self):
        with tempfile.TemporaryDirectory() as directory:
            service = _FakeService(Path(directory))
            server = create_server(service, port=0)
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                self.assertGreater(server.server_port, 0)
                self.assertEqual(
                    server.public_url,
                    "http://127.0.0.1:%d" % server.server_port,
                )
                self.assertTrue(server.csrf_token)
                self.assertIn("#session=", server.launch_url)
                self.assertIn(server.csrf_token, server.launch_url)
            finally:
                server.server_close()
                server.controller.close()

    def test_handle_error_only_suppresses_disconnect_errors(self):
        server = object.__new__(RadarHTTPServer)
        request = mock.Mock()
        client = ("127.0.0.1", 1234)
        with mock.patch.object(ThreadingHTTPServer, "handle_error") as parent:
            try:
                raise BrokenPipeError("closed")
            except BrokenPipeError:
                server.handle_error(request, client)
            parent.assert_not_called()

            try:
                try:
                    raise ConnectionResetError("reset")
                except ConnectionResetError as cause:
                    raise RuntimeError("wrapper") from cause
            except RuntimeError:
                server.handle_error(request, client)
            parent.assert_not_called()

            try:
                raise RuntimeError("real server bug")
            except RuntimeError:
                server.handle_error(request, client)
            parent.assert_called_once_with(request, client)


class WebHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.service = _FakeService(Path(self.temporary.name))
        self.server = create_server(self.service, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.base_url = self.server.public_url
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.service.release_jobs()
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.server.controller.close()
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers or {},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=3) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def json_request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None = None,
        origin: str | None = "same-origin",
        content_type: str = "application/json",
        extra_headers: dict[str, str] | None = None,
        method: str = "POST",
    ):
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["X-Branch-Radar-Token"] = token
        if origin == "same-origin":
            headers["Origin"] = self.base_url
        elif origin is not None:
            headers["Origin"] = origin
        headers.update(extra_headers or {})
        return self.request(
            path,
            method=method,
            body=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )

    @staticmethod
    def decode_json(body: bytes) -> dict[str, Any]:
        return json.loads(body.decode("utf-8"))

    def wait_for_job(self, job_id: str, status: str = "succeeded") -> dict[str, Any]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response_status, _headers, body = self.request(
                "/api/v1/jobs/" + job_id,
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
            self.assertEqual(response_status, 200)
            job = self.decode_json(body)["job"]
            if job["status"] == status:
                return job
            if job["status"] == "failed" and status != "failed":
                self.fail("job failed: %s" % job.get("error"))
            time.sleep(0.01)
        self.fail("job %s did not reach %s" % (job_id, status))

    def test_home_assets_and_health_are_public(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type"))
        html = body.decode("utf-8")
        self.assertIn("Branch Radar", html)
        self.assertNotIn(self.server.csrf_token, html)
        self.assertNotIn("__BRANCH_RADAR_CSRF__", html)
        self.assertIn("/assets/app.css", html)
        self.assertIn('<script src="/assets/theme-bootstrap.js"></script>', html)
        self.assertIn("/assets/app.js", html)
        self.assertLess(
            html.index('/assets/theme-bootstrap.js'), html.index('/assets/app.css')
        )
        script_tags = re.findall(
            r"<script\b([^>]*)>(.*?)</script>", html, flags=re.IGNORECASE | re.DOTALL
        )
        self.assertTrue(script_tags)
        for attributes, inline_body in script_tags:
            self.assertRegex(attributes, r"\bsrc\s*=")
            self.assertFalse(inline_body.strip())
        self.assertIn('<link rel="icon" href="/favicon.svg" type="image/svg+xml">', html)
        self.assertIn('src="/assets/branch-radar-mark.svg"', html)
        self.assertEqual(html.count('class="brand-mark"'), 1)
        content_security_policy = headers.get("Content-Security-Policy")
        self.assertIn("frame-ancestors 'none'", content_security_policy)
        self.assertIn("script-src 'self'", content_security_policy)
        self.assertNotIn("'unsafe-inline'", content_security_policy)
        self.assertNotIn("'unsafe-eval'", content_security_policy)
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

        status, headers, body = self.request("/assets/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type"))
        self.assertIn("must-revalidate", headers.get("Cache-Control"))
        self.assertTrue(headers.get("ETag"))
        self.assertGreater(len(body), 100)
        self.assertIn("[hidden] { display: none !important; }", body.decode("utf-8"))

        etag = headers.get("ETag")
        status, headers, compressed = self.request(
            "/assets/app.css", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertIn("[hidden] { display: none !important; }", gzip.decompress(compressed).decode("utf-8"))
        status, headers, body = self.request(
            "/assets/app.css", headers={"If-None-Match": etag}
        )
        self.assertEqual(status, 304)
        self.assertEqual(body, b"")

        status, headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type"))
        self.assertGreater(len(body), 100)

        status, headers, body = self.request("/assets/theme-bootstrap.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type"))
        self.assertIn("must-revalidate", headers.get("Cache-Control"))
        self.assertGreater(len(body), 100)
        self.assertIn("bootstrapTheme", body.decode("utf-8"))

        status, headers, body = self.request("/assets/i18n.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type"))
        self.assertIn("must-revalidate", headers.get("Cache-Control"))
        self.assertGreater(len(body), 100)

        status, headers, body = self.request("/assets/ui-model.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type"))
        self.assertGreater(len(body), 100)

        status, headers, body = self.request("/assets/changelog.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type"))
        self.assertIn("must-revalidate", headers.get("Cache-Control"))
        self.assertTrue(headers.get("ETag"))
        changelog = self.decode_json(body)
        self.assertEqual(changelog["releases"][0]["version"], "0.3.0")
        self.assertTrue(changelog["releases"][0]["current"])

        for path in ("/favicon.svg", "/assets/branch-radar-mark.svg"):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn("image/svg+xml", headers.get("Content-Type"))
            self.assertLess(len(body), 4096)
            icon = body.decode("utf-8")
            self.assertIn('viewBox="0 0 32 32"', icon)
            self.assertNotIn("<script", icon.lower())
            self.assertNotIn("href=", icon.lower())

        status, _headers, body = self.request("/api/v1/health")
        self.assertEqual(status, 200)
        health = self.decode_json(body)
        self.assertTrue(health["ok"])
        self.assertIn("version", health)
        self.assertIn("started_at", health)

    def test_frontend_recovers_when_secure_link_reuses_open_tab(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        self.assertIn("function captureLaunchSession()", javascript)
        self.assertIn("if (captureLaunchSession())", javascript)
        self.assertIn("location.reload();", javascript)

    def test_sticky_topbar_compaction_uses_stable_scroll_hysteresis(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")

        self.assertIn("const TOPBAR_COMPACT_ENTER_Y = 64", javascript)
        self.assertIn("const TOPBAR_COMPACT_EXIT_Y = 16", javascript)
        self.assertIn("document.scrollingElement?.scrollTop ?? window.scrollY", javascript)

        sync_start = javascript.index("function syncStickyTopbar()")
        sync_end = javascript.index("function setEventKindFilter(", sync_start)
        sync = javascript[sync_start:sync_end]
        self.assertIn("stickyTopbarCompact", sync)
        self.assertIn("scrollY > TOPBAR_COMPACT_EXIT_Y", sync)
        self.assertIn("scrollY >= TOPBAR_COMPACT_ENTER_Y", sync)
        self.assertIn("if (compact === stickyTopbarCompact) return", sync)
        self.assertNotIn("getBoundingClientRect", sync)

        bind_start = javascript.index("function bindInteractions()")
        bind_end = javascript.index("async function initialize()", bind_start)
        interactions = javascript[bind_start:bind_end]
        self.assertIn('scheduleRender("topbar-scroll", syncStickyTopbar)', interactions)
        self.assertIn("entry?.borderBoxSize", interactions)
        self.assertIn("syncStickyTopbarClearance(stickyTopbarObservedHeight)", interactions)

        status, _headers, body = self.request("/assets/app.css")
        self.assertEqual(status, 200)
        reduced_motion = body.decode("utf-8").split(
            "@media (prefers-reduced-motion: reduce)", 1
        )[1]
        self.assertIn("transition-duration: 0.001ms !important", reduced_motion)

    def test_check_run_attention_list_does_not_infer_a_truncated_total(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        render_start = javascript.index("function renderCheckRunDetails(run)")
        render_end = javascript.index("function renderCheckRunBanner(payload)", render_start)
        renderer = javascript[render_start:render_end]

        self.assertIn("Math.max(run.blocked.length, run.counts.blocked)", renderer)
        self.assertIn('run.blockedTruncated ? t("需要关注的分支")', renderer)
        self.assertIn("另有更多分支需要关注。", renderer)
        self.assertNotIn("run.counts.blocked + run.counts.errors", renderer)
        self.assertNotIn("未完成分支", renderer)

        banner = javascript[render_end:]
        self.assertIn("run.publication.backlog.failed", banner)
        self.assertIn("历史投递", javascript)

    def test_changelog_uses_compact_native_disclosure_contract(self):
        status, _headers, body = self.request("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn('id="changelog-list" aria-live="polite" aria-busy="true"', html)
        self.assertIn('id="changelog-current-version"', html)
        self.assertIn('id="changelog-current-date"', html)
        self.assertIn('class="changelog-loading"', html)

        status, _headers, body = self.request("/assets/changelog.json")
        self.assertEqual(status, 200)
        releases = self.decode_json(body)["releases"]
        self.assertTrue(any(release.get("current") for release in releases))
        self.assertTrue(any(not release.get("current") for release in releases))

        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        render_start = javascript.index("function normalizedChangelogChanges(changes)")
        render_end = javascript.index("async function ensureChangelog(", render_start)
        renderer = javascript[render_start:render_end]

        for contract in (
            'element("details", "changelog-details")',
            'element("summary", "changelog-details-summary")',
            "changelog-category-chips",
            "changelog-category-chip",
            "changelog-change-count",
            "changelog-highlights",
            "changelog-highlight",
            "is-highlight",
            "changelog-details-label is-collapsed",
            "changelog-details-label is-expanded",
            'createIcon("chevron")',
            "项变化",
            "details.append(summary, changeGrid)",
        ):
            self.assertIn(contract, renderer)
        self.assertNotIn("details.open = true", renderer)
        self.assertNotIn('.setAttribute("open"', renderer)

        loader_end = javascript.index("function formatTime(", render_end)
        loader = javascript[render_end:loader_end]
        self.assertIn('container.setAttribute("aria-busy", "true")', loader)
        self.assertIn('container.setAttribute("aria-busy", "false")', loader)
        self.assertIn("暂时无法读取更新日志", loader)

    def test_changelog_revalidates_each_visit_and_preserves_last_good_content(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")

        set_view_start = javascript.index("function setView(name)")
        set_view_end = javascript.index("function syncContextActions()", set_view_start)
        set_view = javascript[set_view_start:set_view_end]
        self.assertIn(
            'if (selected === "changelog") ensureChangelog({ refresh: true });',
            set_view,
        )

        loader_start = javascript.index("async function ensureChangelog(")
        loader_end = javascript.index("function formatTime(", loader_start)
        loader = javascript[loader_start:loader_end]
        self.assertIn("async function ensureChangelog({ refresh = false } = {})", loader)
        self.assertIn("state.changelog && !refresh", loader)
        self.assertIn("const previous = state.changelog", loader)
        self.assertIn('cache: "no-cache"', loader)

        fallback_start = loader.index("} catch (_error) {")
        finally_start = loader.index("} finally {", fallback_start)
        fallback = loader[fallback_start:finally_start]
        self.assertIn("if (previous)", fallback)
        self.assertIn("state.changelog = previous", fallback)
        self.assertNotIn("renderChangelog(previous)", fallback)
        self.assertIn(
            'showToast(t("更新日志同步失败，已保留上次内容。"), true)',
            fallback,
        )
        self.assertLess(fallback.index("if (previous)"), fallback.index("} else {"))
        self.assertLess(fallback.index("} else {"), fallback.index("container.replaceChildren"))

        finalizer = loader[finally_start:]
        self.assertIn("state.changelogLoading = false", finalizer)
        self.assertIn('container.setAttribute("aria-busy", "false")', finalizer)

        renderer_start = javascript.index("function renderChangelog(payload)")
        renderer_end = javascript.index("async function ensureChangelog(", renderer_start)
        renderer = javascript[renderer_start:renderer_end]
        self.assertNotIn('container.setAttribute("aria-busy", "false")', renderer)
        self.assertIn("JSON.stringify(previous) !== JSON.stringify(payload)", loader)
        self.assertIn("if (changed) renderChangelog(payload)", loader)

    def test_current_changelog_surfaces_latest_event_detail_update(self):
        status, _headers, body = self.request("/assets/changelog.json")
        self.assertEqual(status, 200)
        releases = self.decode_json(body)["releases"]
        current = next(
            release for release in releases if release.get("date") == "2026-08-21"
        )

        self.assertEqual(current["date"], "2026-08-21")
        self.assertEqual(current["entry_type"], "update")
        self.assertFalse(current["current"])
        latest = next(release for release in releases if release.get("current"))
        self.assertEqual(latest["date"], "2026-08-25")
        self.assertEqual(latest["version"], "0.3.0")
        improvement_index, improvement = next(
            (index, change)
            for index, change in enumerate(current["changes"])
            if change.get("type") == "改进"
        )
        self.assertLess(improvement_index, 4)
        highlight = improvement["items"][0]
        for expected in ("移除", "SHA 范围", "新增提交", "最新优先"):
            self.assertIn(expected, highlight)

    def test_changelog_keeps_historical_release_and_adds_daily_update(self):
        status, _headers, body = self.request("/assets/changelog.json")
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        releases = payload["releases"]

        august_20 = [release for release in releases if release.get("date") == "2026-08-20"]
        august_21 = [release for release in releases if release.get("date") == "2026-08-21"]
        august_24 = [release for release in releases if release.get("date") == "2026-08-24"]
        august_25 = [release for release in releases if release.get("date") == "2026-08-25"]
        august_27 = [release for release in releases if release.get("date") == "2026-08-27"]
        self.assertEqual(len(august_20), 1)
        self.assertEqual(len(august_21), 1)
        self.assertEqual(len(august_24), 1)
        self.assertEqual(len(august_25), 2)
        self.assertEqual(len(august_27), 1)
        historical = august_20[0]
        previous_update = august_21[0]
        previous_clone_update = august_24[0]
        release = next(
            entry for entry in august_25 if entry.get("entry_type") != "update"
        )
        update = next(
            entry for entry in august_25 if entry.get("entry_type") == "update"
        )
        daily_update = august_27[0]

        self.assertEqual(historical["version"], "0.2.0")
        self.assertFalse(historical.get("current"))
        self.assertFalse(previous_update.get("current"))
        self.assertFalse(previous_clone_update.get("current"))
        self.assertEqual(update.get("version"), historical["version"])
        self.assertFalse(update.get("current"))
        self.assertEqual(update.get("entry_type"), "update")
        self.assertEqual(release.get("version"), "0.3.0")
        self.assertTrue(release.get("current"))
        self.assertNotIn("entry_type", release)
        self.assertIs(release, releases[0])
        self.assertNotIn("entry_type", historical)
        self.assertEqual(daily_update.get("version"), "0.3.0")
        self.assertEqual(daily_update.get("entry_type"), "update")
        self.assertFalse(daily_update.get("current"))

        immutable_release = {
            key: value
            for key, value in historical.items()
            if not key.endswith("_en")
        }
        immutable_release["changes"] = [
            {
                key: value
                for key, value in change.items()
                if not key.endswith("_en")
            }
            for change in historical["changes"]
        ]
        immutable_release.pop("current", None)
        encoded_release = json.dumps(
            immutable_release,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded_release).hexdigest(),
            "6a4e68d0c0c7d35e79d8331722bd2f1ad894c617ba54e8cc90d85bc76730dc23",
            "2026-08-20 的完整发布记录应保持不可变；当天修订必须另起记录",
        )
        self.assertEqual(
            [(change["type"], len(change["items"])) for change in historical["changes"]],
            [("新增", 6), ("改进", 12), ("性能", 5), ("安全", 3)],
        )

        historical_snapshot = [
            {key: value for key, value in entry.items() if key != "current"}
            for entry in releases[1:]
            if entry.get("date") != "2026-08-27"
        ]
        encoded_history = json.dumps(
            historical_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded_history).hexdigest(),
            "d5e7ee7002dc40b17e4b8eb7be1d951eec3ec65c19a027619c464fae8c639743",
            "既有中英文更新日志必须完整保留；新增记录应置顶而不是改写历史",
        )

        for entry in (
            release,
            daily_update,
            update,
            previous_clone_update,
            previous_update,
            historical,
        ):
            self.assertTrue(entry.get("headline"))
            self.assertTrue(entry.get("headline_en"))
            self.assertTrue(entry.get("summary"))
            self.assertTrue(entry.get("summary_en"))
            self.assertTrue(entry.get("changes"))
            for change in entry["changes"]:
                self.assertTrue(change.get("type"))
                self.assertTrue(change.get("type_en"))
                self.assertTrue(change.get("items"))
                self.assertEqual(len(change.get("items_en") or []), len(change["items"]))

        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        renderer_start = javascript.index("function renderChangelog(payload)")
        renderer_end = javascript.index("async function ensureChangelog(", renderer_start)
        renderer = javascript[renderer_start:renderer_end]
        self.assertIn("const current = releases.find", renderer)
        self.assertIn('release.entry_type === "update"', renderer)
        self.assertIn('"changelog-update-badge", "补充更新"', renderer)
        self.assertIn("releases.forEach((release, releaseIndex)", renderer)
        self.assertIn("fragment.append(article)", renderer)
        self.assertIn("container.replaceChildren(fragment)", renderer)

    def test_frontend_exposes_four_destinations_and_safe_primary_check(self):
        status, _headers, body = self.request("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertEqual(html.count('data-view="overview"'), 2)
        self.assertEqual(html.count('data-view="projects"'), 2)
        self.assertEqual(html.count('data-view="changelog"'), 2)
        self.assertEqual(html.count('data-view="settings"'), 2)
        self.assertNotIn('data-view="activity"', html)
        self.assertNotIn('data-view="connections"', html)
        self.assertNotIn('id="check-fetch"', html)
        self.assertIn('id="local-project-list"', html)
        self.assertIn('id="local-project-search"', html)
        self.assertIn('id="local-project-pagination"', html)
        self.assertIn('id="local-project-prev"', html)
        self.assertIn('id="local-project-next"', html)
        self.assertIn('id="scan-roots-button"', html)
        self.assertIn('id="scan-roots-dialog"', html)
        self.assertIn('id="scan-roots-form"', html)
        self.assertIn('id="scan-roots-error" role="alert"', html)
        self.assertIn('id="remote-url"', html)
        self.assertIn('id="branch-dialog"', html)
        self.assertIn('id="branch-search"', html)
        self.assertIn('id="select-all-branches"', html)
        self.assertIn('id="confirm-watch-label"', html)
        self.assertIn('id="watch-progress"', html)
        self.assertIn('id="watch-progress-track"', html)
        self.assertIn('id="watch-progress-elapsed"', html)
        self.assertIn('id="branch-dismiss-button"', html)
        self.assertIn('id="activity-scope"', html)
        self.assertIn('id="activity-load-more"', html)
        self.assertIn('id="event-detail-status"', html)
        self.assertIn('id="overview-status"', html)
        self.assertIn('class="watch-command-deck"', html)
        self.assertIn('id="watch-list-controls"', html)
        self.assertIn('id="batch-action-bar"', html)
        self.assertIn('id="clear-selected-watches"', html)
        self.assertIn('id="check-selected-button"', html)
        self.assertIn('id="watch-tab-watched"', html)
        self.assertIn('id="watch-tab-add"', html)
        self.assertIn('id="repository-tag-navigation"', html)
        self.assertIn('id="toggle-all-repositories"', html)
        self.assertIn('id="repository-profile-dialog"', html)
        self.assertIn('id="unwatch-dialog"', html)
        self.assertIn('id="unwatch-form"', html)
        self.assertIn('aria-describedby="unwatch-dialog-description unwatch-error"', html)
        self.assertIn('id="cancel-unwatch-button"', html)
        self.assertIn('id="overview-kicker" role="status" aria-live="polite"', html)
        self.assertIn('id="repository-profile-alias"', html)
        self.assertIn('id="repository-profile-tags"', html)
        self.assertIn('id="repository-tag-suggestions"', html)
        self.assertIn('id="repository-tag-options"', html)
        self.assertIn('id="changelog-list"', html)
        self.assertIn('id="changelog-current-version"', html)
        self.assertIn('data-watch-panel="watched"', html)
        self.assertIn('data-watch-panel="add" hidden', html)
        self.assertLess(html.index('id="watch-panel-watched"'), html.index('id="watch-panel-add"'))
        self.assertIn('id="activity-scope-check"', html)
        self.assertIn('id="lark-document-reference"', html)
        self.assertIn('id="lark-document-link"', html)
        self.assertIn('id="event-kind-menu"', html)
        self.assertIn('id="event-kind-trigger"', html)
        self.assertIn('class="repository-list"', html)
        self.assertNotIn('id="onboarding-dialog"', html)
        self.assertNotIn('id="projects-add-button"', html)

        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        self.assertIn(
            'async function submitSafeCheck(branchIds = null, scope = "all")',
            javascript,
        )
        self.assertIn(
            'button(t("检查仓库"), "button button-quiet button-compact '
            'repository-check-button"',
            javascript,
        )
        self.assertIn("async function submitRepositoryCheck(repositoryId)", javascript)
        self.assertIn("repository_id: repositoryId", javascript)
        self.assertIn(
            "const fullGroup = allGroupsByKey.get(repositoryGroupKey(group))",
            javascript,
        )
        self.assertIn("repositoryCheck.dataset.checkControl", javascript)
        self.assertIn('submission?.scope === "selected"', javascript)
        self.assertIn("use_codex: false", javascript)
        self.assertIn("publish: false", javascript)
        self.assertNotIn('getElementById("check-fetch")', javascript)
        self.assertIn("function setIntegrationIdentity", javascript)
        self.assertIn("function bindKindFilter()", javascript)
        self.assertIn("function bindWatchWorkspaceTabs()", javascript)
        self.assertIn("function setWatchWorkspaceTab", javascript)
        self.assertIn("function renderRepositoryTagNavigation", javascript)
        self.assertIn("function setRepositoryDisclosure", javascript)
        self.assertIn("function toggleAllRepositories", javascript)
        self.assertIn('disclosure.setAttribute("aria-expanded"', javascript)
        self.assertIn("REPOSITORY_EXPANSION_STORAGE_KEY", javascript)
        # A collapsed repository must not pay for rows nobody has opened, and
        # expanding one must build them before the disclosure measures height.
        self.assertIn("pendingBranchRows.set(branchList, buildBranchRows)", javascript)
        self.assertIn("if (nextExpanded) ensureBranchRows(branchList);", javascript)
        self.assertIn("function ensureBranchRows(branchList)", javascript)
        # Selection follows the filtered model. Reading mounted checkboxes would
        # silently drop every branch inside a collapsed repository.
        self.assertIn("return state.visibleWatchIds;", javascript)
        self.assertNotIn(
            'querySelectorAll("#project-list .repository-branch-select input")',
            javascript,
        )
        self.assertIn("function syncContextActions()", javascript)
        self.assertIn("function configuredScanRoots()", javascript)
        self.assertIn("function openScanRootsDialog()", javascript)
        self.assertIn("async function saveScanRoots(event)", javascript)
        self.assertIn('api("/api/v1/preferences/scan-roots"', javascript)
        self.assertIn("if (!hasConfiguredScanRoots())", javascript)
        self.assertIn("function syncBatchActionBar()", javascript)
        self.assertIn("function renderWatchProgress(job)", javascript)
        self.assertIn("function elapsedJobText(job)", javascript)
        self.assertIn(
            'dismiss.textContent = t(isRunning ? "收起到后台"', javascript
        )
        self.assertIn("function makeBranchActionMenu(group, watch)", javascript)
        self.assertIn(
            "function openUnwatchConfirmation(group, watch, returnFocus)",
            javascript,
        )
        self.assertIn("function setUnwatchBusy(busy)", javascript)
        self.assertIn("async function submitUnwatch(event)", javascript)
        self.assertIn("function latestWatchSuccessTime(watches)", javascript)
        self.assertIn(
            "latestWatchSuccessTime(payload.watches || [])",
            javascript,
        )
        self.assertIn('dialog.addEventListener("cancel"', javascript)
        self.assertIn("state.pendingUnwatch?.submitting", javascript)
        self.assertLess(
            javascript.index('showToast(t("已停止关注'),
            javascript.index("await loadDashboard();", javascript.index("async function submitUnwatch")),
        )
        self.assertIn("function watchAttentionReason(watch, recentKeys)", javascript)
        self.assertIn('search.placeholder = t("搜索标签")', javascript)
        self.assertIn("function renderRepositoryTagSuggestions", javascript)
        self.assertIn("function toggleRepositoryTag(tag)", javascript)
        self.assertIn('control.setAttribute("aria-pressed"', javascript)
        self.assertIn("async function saveRepositoryProfile", javascript)
        self.assertIn("async function ensureChangelog(", javascript)
        self.assertIn("function renderChangelog(payload)", javascript)
        self.assertIn('location.hash = "projects/add"', javascript)
        self.assertIn("function submitSelectedCheck()", javascript)
        self.assertIn("function renderLinkedLarkDocument(lark)", javascript)
        self.assertIn("payload.branch_ids = branchIds", javascript)
        self.assertIn('codex.detected ? "启动失败" : "未找到"', javascript)
        self.assertIn('getElementById("lark-identity-avatar").textContent', javascript)
        self.assertIn('getElementById("integration-status-announcement").textContent', javascript)
        self.assertIn("async function startDiscovery()", javascript)
        self.assertIn("async function matchRemoteUrl(url)", javascript)
        self.assertIn("function toggleSelectAllBranches()", javascript)
        self.assertIn("function filterBranchRows()", javascript)
        self.assertIn("function setWatchButtonState", javascript)
        self.assertIn("input.dataset.watched", javascript)
        self.assertIn("generation !== state.pollGeneration", javascript)
        self.assertIn("state.discoveryError", javascript)
        self.assertIn("function groupWatches(watches)", javascript)
        self.assertIn("async function loadActivityFeed", javascript)
        self.assertIn("function openActivityScope(watch)", javascript)
        self.assertIn("localProjectPageSize: 8", javascript)
        self.assertIn('input[type=checkbox]:checked:not(:disabled)', javascript)
        self.assertNotIn("watch.repository === name", javascript)
        self.assertNotIn("openOnboarding", javascript)

        status, _headers, body = self.request("/assets/app.css")
        self.assertEqual(status, 200)
        stylesheet = body.decode("utf-8")
        self.assertIn("--danger-solid: #b53649", stylesheet)
        self.assertIn("--on-danger: #fff9fb", stylesheet)
        self.assertIn(".unwatch-modal { width: min(540px", stylesheet)

    def test_event_detail_omits_sha_range_and_keeps_commit_section(self):
        status, _headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        javascript = body.decode("utf-8")
        render_start = javascript.index("function renderEventDetail(event)")
        render_end = javascript.index("function appendDetailList(", render_start)
        renderer = javascript[render_start:render_end]

        for obsolete_contract in (
            "SHA 范围",
            "复制 SHA",
            "copy-sha-button",
            "sha-range",
            "event.old_sha",
            "event.new_sha",
            "event.merge_base",
        ):
            self.assertNotIn(obsolete_contract, renderer)
        self.assertIn("新增提交", renderer)
        self.assertIn("最新优先", renderer)
        self.assertIn("commit-list", renderer)
        self.assertIn("commit.commit_date || commit.author_date", renderer)

    def test_repository_profile_update_is_synchronous_and_validated(self):
        status, _headers, body = self.json_request(
            "/api/v1/repositories/profile",
            {
                "repository_id": 10,
                "alias": " 实验室后端 ",
                "tags": ["后端", "核心", "后端"],
            },
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 200)
        repository = self.decode_json(body)["repository"]
        self.assertEqual(repository["name"], "demo")
        self.assertEqual(repository["alias"], "实验室后端")
        self.assertEqual(repository["tags"], ["后端", "核心"])
        self.assertEqual(
            self.service.repository_profile_updates[-1],
            {
                "repository_id": 10,
                "display_name": "实验室后端",
                "tags": ["后端", "核心"],
            },
        )

        invalid_payloads = [
            {"repository_id": True, "alias": "demo", "tags": []},
            {"repository_id": 10, "alias": "demo", "tags": ["x"] * 9},
            {"repository_id": 10, "alias": "demo", "tags": ["bad,tag"]},
            {"repository_id": 10, "alias": "demo", "tags": [], "path": "/tmp"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, _headers, body = self.json_request(
                    "/api/v1/repositories/profile",
                    payload,
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"],
                    "invalid_repository_profile",
                )

    def test_non_health_api_requires_session_token(self):
        status, _headers, body = self.request("/api/v1/dashboard")
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_token")

        status, _headers, body = self.request(
            "/api/v1/dashboard",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        dashboard = self.decode_json(body)
        self.assertEqual(dashboard["counts"]["branches"], 1)
        self.assertFalse(dashboard["events_has_more"])
        self.assertIsNone(dashboard["events_next_before_id"])
        self.assertNotIn("home", dashboard["app"])
        self.assertEqual(
            dashboard["settings"]["scan_roots"],
            [str(self.service.paths.home)],
        )
        self.assertNotIn("lark_document", dashboard["settings"])

    def test_monitoring_preferences_are_readable_and_same_origin_protected(self):
        path = "/api/v1/monitoring"
        status, _headers, body = self.request(
            path,
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.decode_json(body)["interval_seconds"], 900)

        payload = {
            "enabled": True,
            "interval_seconds": 300,
            "notifications_enabled": True,
            "quiet_hours": {
                "enabled": True,
                "start": "22:30",
                "end": "07:30",
            },
            "locale": "en",
        }
        status, _headers, body = self.json_request(
            path, payload, method="PUT"
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_token")

        status, _headers, body = self.json_request(
            path,
            payload,
            method="PUT",
            token=self.server.csrf_token,
            origin="http://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_origin")

        status, _headers, body = self.json_request(
            path,
            payload,
            method="PUT",
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(self.decode_json(body)["job"]["id"])
        saved = job["result"]
        self.assertTrue(saved["enabled"])
        self.assertEqual(saved["interval_seconds"], 300)
        self.assertEqual(saved["quiet_hours"]["start"], "22:30")
        self.assertEqual(
            self.service.monitoring_updates[-1],
            {
                "enabled": True,
                "interval_seconds": 300,
                "notifications_enabled": True,
                "quiet_hours_enabled": True,
                "quiet_hours_start": "22:30",
                "quiet_hours_end": "07:30",
                "locale": "en",
            },
        )

    def test_monitoring_preferences_reject_partial_or_invalid_payloads(self):
        valid = {
            "enabled": True,
            "interval_seconds": 900,
            "notifications_enabled": True,
            "quiet_hours": {
                "enabled": False,
                "start": "22:00",
                "end": "08:00",
            },
            "locale": "zh-CN",
        }
        invalid = [
            {"enabled": True},
            dict(valid, interval_seconds=60),
            dict(valid, notifications_enabled="yes"),
            dict(valid, quiet_hours={"enabled": False}),
            dict(valid, locale="fr"),
            dict(valid, locale=[]),
            dict(valid, locale={}),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                status, _headers, body = self.json_request(
                    "/api/v1/monitoring",
                    payload,
                    method="PUT",
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 400)
                self.assertTrue(self.decode_json(body)["error"]["code"].startswith("invalid_"))

    def test_scan_root_preferences_are_synchronous_and_protected(self):
        path = "/api/v1/preferences/scan-roots"
        payload = {"roots": ["~/projects", "~/worktrees"]}

        status, _headers, body = self.json_request(path, payload)
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_token")

        status, _headers, body = self.json_request(
            path,
            payload,
            token=self.server.csrf_token,
            origin="http://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_origin")

        status, _headers, body = self.json_request(
            path,
            payload,
            token=self.server.csrf_token,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.decode_json(body)["error"]["code"], "json_required")

        status, _headers, body = self.json_request(
            path,
            payload,
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.decode_json(body)["scan_roots"],
            ["~/projects", "~/worktrees"],
        )
        self.assertEqual(
            self.service.preference_updates,
            [["~/projects", "~/worktrees"]],
        )

        status, _headers, body = self.json_request(
            path,
            {"roots": []},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.decode_json(body)["scan_roots"], [])
        self.assertEqual(
            self.service.preference_updates,
            [["~/projects", "~/worktrees"], []],
        )

    def test_scan_root_preferences_reject_unsupported_payloads(self):
        path = "/api/v1/preferences/scan-roots"
        invalid_payloads = [
            {},
            {"roots": "~/projects"},
            {"roots": ["~/projects"], "unexpected": True},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, _headers, body = self.json_request(
                    path,
                    payload,
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"],
                    "invalid_scan_roots",
                )

        self.assertEqual(self.service.preference_updates, [])

    def test_scan_root_picker_is_a_bounded_job_and_returns_portable_path(self):
        path = "/api/v1/preferences/scan-roots/picker"
        fake_home = Path(self.temporary.name) / "home"
        selected = fake_home / "projects"
        selected.mkdir(parents=True)
        picker = mock.Mock()
        picker.pick.return_value = FolderPickResult(path=str(selected))

        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), mock.patch(
            "branch_radar.web.server.FolderPicker",
            return_value=picker,
        ):
            status, _headers, body = self.json_request(
                path,
                {},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 202)
            submitted = self.decode_json(body)["job"]
            self.assertEqual(submitted["kind"], "pick_scan_root")
            job = self.wait_for_job(submitted["id"])

        self.assertEqual(
            job["result"],
            {"cancelled": False, "scan_root": "~/projects"},
        )
        picker.pick.assert_called_once_with()

    def test_scan_root_picker_cancellation_is_successful(self):
        picker = mock.Mock()
        picker.pick.return_value = FolderPickResult(cancelled=True)
        with mock.patch(
            "branch_radar.web.server.FolderPicker",
            return_value=picker,
        ):
            status, _headers, body = self.json_request(
                "/api/v1/preferences/scan-roots/picker",
                {},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 202)
            job = self.wait_for_job(self.decode_json(body)["job"]["id"])

        self.assertEqual(
            job["result"],
            {"cancelled": True, "scan_root": None},
        )

    def test_scan_root_picker_reports_unavailable_picker_safely(self):
        picker = mock.Mock()
        picker.pick.side_effect = FolderPickerError(
            "当前系统未找到可用的目录选择器；请手动输入目录路径",
            code="folder_picker_unavailable",
        )
        with mock.patch(
            "branch_radar.web.server.FolderPicker",
            return_value=picker,
        ):
            status, _headers, body = self.json_request(
                "/api/v1/preferences/scan-roots/picker",
                {},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 202)
            job = self.wait_for_job(
                self.decode_json(body)["job"]["id"],
                status="failed",
            )

        self.assertEqual(job["error_code"], "folder_picker_unavailable")
        self.assertIn("手动输入", job["error"])
        self.assertNotIn("Traceback", json.dumps(job))

    def test_scan_root_picker_endpoint_reuses_request_protections(self):
        path = "/api/v1/preferences/scan-roots/picker"
        picker = mock.Mock()
        with mock.patch(
            "branch_radar.web.server.FolderPicker",
            return_value=picker,
        ):
            status, _headers, body = self.json_request(path, {})
            self.assertEqual(status, 403)
            self.assertEqual(
                self.decode_json(body)["error"]["code"],
                "invalid_token",
            )

            status, _headers, body = self.json_request(
                path,
                {},
                token=self.server.csrf_token,
                origin="http://evil.example",
            )
            self.assertEqual(status, 403)
            self.assertEqual(
                self.decode_json(body)["error"]["code"],
                "invalid_origin",
            )

            status, _headers, body = self.json_request(
                path,
                {},
                token=self.server.csrf_token,
                content_type="text/plain",
            )
            self.assertEqual(status, 415)
            self.assertEqual(
                self.decode_json(body)["error"]["code"],
                "json_required",
            )

            status, _headers, body = self.json_request(
                path,
                {"unexpected": True},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                self.decode_json(body)["error"]["code"],
                "invalid_folder_picker_request",
            )

        picker.pick.assert_not_called()

    def test_host_and_origin_validation(self):
        status, _headers, body = self.request(
            "/api/v1/health",
            headers={"Host": "evil.example:%d" % self.server.server_port},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_host")

        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
            origin="http://evil.example",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_origin")

        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
            origin=None,
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_origin")

        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
            origin=self.base_url,
        )
        self.assertEqual(status, 202)
        self.assertIn("job", self.decode_json(body))

    def test_post_csrf_content_type_and_body_limit(self):
        status, _headers, body = self.json_request("/api/v1/discoveries", {})
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_token")

        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertEqual(self.decode_json(body)["error"]["code"], "json_required")

        status, _headers, body = self.request(
            "/api/v1/discoveries",
            method="POST",
            body=b"x" * (MAX_BODY_BYTES + 1),
            headers={
                "Content-Type": "application/json",
                "X-Branch-Radar-Token": self.server.csrf_token,
                "Origin": self.base_url,
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(self.decode_json(body)["error"]["code"], "body_too_large")

    def test_discovery_job_reports_running_then_succeeded(self):
        self.service.discovery_blocked = True
        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {"roots": [self.temporary.name]},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job_id = self.decode_json(body)["job"]["id"]
        self.assertTrue(self.service.discovery_started.wait(timeout=1))

        status, _headers, body = self.request(
            "/api/v1/jobs/" + job_id,
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.decode_json(body)["job"]["status"], "running")

        self.service.discovery_release.set()
        job = self.wait_for_job(job_id)
        repositories = job["result"]["repositories"]
        self.assertEqual(repositories[0]["name"], "demo")
        self.assertEqual(repositories[0]["branches"], ["main", "develop"])
        self.assertEqual(repositories[0]["repository_id"], 10)
        self.assertEqual(repositories[0]["watched_branches"], ["main"])
        self.assertNotIn("remote_url", repositories[0])

    def test_remote_preview_is_synchronous_and_redacts_github_web_paths(self):
        status, _headers, body = self.json_request(
            "/api/v1/remotes/preview",
            {"url": "https://alice:super-secret@github.com/lab/demo/tree/main"},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        self.assertNotIn("job", payload)
        remote = payload["remote"]
        self.assertEqual(remote["canonical"], "github.com/lab/demo")
        self.assertEqual(remote["host"], "github.com")
        self.assertEqual(remote["project"], "lab/demo")
        self.assertNotIn("super-secret", json.dumps(payload))
        self.assertNotIn("alice", json.dumps(payload))

        status, _headers, body = self.json_request(
            "/api/v1/remotes/preview",
            {"url": ""},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.decode_json(body)["error"]["code"], "remote_required")

    def test_repository_clone_is_a_cancellable_bounded_job(self):
        status, _headers, body = self.json_request(
            "/api/v1/repository-clones",
            {
                "url": " https://github.com/lab/demo ",
                "parent": self.temporary.name,
                "directory_name": "renamed-demo",
            },
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        submitted = self.decode_json(body)["job"]
        self.assertEqual(submitted["kind"], "clone")
        self.assertTrue(submitted["cancellable"])

        job = self.wait_for_job(submitted["id"])
        self.assertFalse(job["result"]["reused"])
        repository = job["result"]["repository"]
        self.assertEqual(repository["name"], "renamed-demo")
        self.assertEqual(repository["canonical_remote"], "github.com/lab/demo")
        self.assertEqual(repository["branches"], ["main", "develop"])
        self.assertNotIn("remote_url", repository)
        self.assertNotIn("token", json.dumps(job))
        self.assertEqual(self.service.clone_calls, [{
            "url": "https://github.com/lab/demo",
            "parent": self.temporary.name,
            "directory_name": "renamed-demo",
            "has_progress": True,
            "has_cancel_event": True,
        }])

    def test_repository_clone_can_be_cancelled_and_releases_job_slot(self):
        self.service.clone_blocked = True
        status, _headers, body = self.json_request(
            "/api/v1/repository-clones",
            {
                "url": "https://github.com/lab/demo",
                "parent": self.temporary.name,
            },
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job_id = self.decode_json(body)["job"]["id"]
        self.assertTrue(self.service.clone_started.wait(timeout=1))

        status, _headers, body = self.json_request(
            "/api/v1/jobs/%s/cancel" % job_id,
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.decode_json(body)["job"]["status"], "cancelling")
        cancelled = self.wait_for_job(job_id, status="cancelled")
        self.assertEqual(cancelled["error_code"], "clone_cancelled")
        self.assertIsNone(cancelled["result"])
        self.assertTrue(self.service.clone_cancelled.is_set())

        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.wait_for_job(self.decode_json(body)["job"]["id"])

    def test_clone_cancel_cleanup_failures_remain_failed_and_are_redacted(self):
        failures = [
            (
                ServiceError(
                    "清理临时目录失败 token=super-secret",
                    code="clone_cleanup_failed",
                ),
                "clone_cleanup_failed",
                "super-secret",
            ),
            (
                RuntimeError("cleanup crashed token=unexpected-secret"),
                "operation_failed",
                "unexpected-secret",
            ),
        ]
        for failure, expected_code, secret in failures:
            with self.subTest(expected_code=expected_code):
                self.service.clone_blocked = True
                self.service.clone_cancel_exception = failure
                self.service.clone_started.clear()
                self.service.clone_cancelled.clear()
                status, _headers, body = self.json_request(
                    "/api/v1/repository-clones",
                    {
                        "url": "https://github.com/lab/demo",
                        "parent": self.temporary.name,
                    },
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 202)
                job_id = self.decode_json(body)["job"]["id"]
                self.assertTrue(self.service.clone_started.wait(timeout=1))

                status, _headers, body = self.json_request(
                    "/api/v1/jobs/%s/cancel" % job_id,
                    {},
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 202)
                failed = self.wait_for_job(job_id, status="failed")
                self.assertEqual(failed["error_code"], expected_code)
                self.assertIsNone(failed["result"])
                serialized = json.dumps(failed, ensure_ascii=False)
                self.assertNotIn(secret, serialized)
                self.assertNotIn("Traceback", serialized)
                if expected_code == "operation_failed":
                    self.assertIn("任务遇到内部错误", serialized)
                    self.assertNotIn("cleanup crashed", serialized)
                else:
                    self.assertIn("<redacted>", serialized)
                self.assertTrue(self.service.clone_cancelled.is_set())

    def test_repository_clone_and_cancel_requests_are_strictly_validated(self):
        invalid_payloads = [
            {},
            {"url": "https://github.com/lab/demo"},
            {"parent": self.temporary.name},
            {
                "url": "https://github.com/lab/demo",
                "parent": self.temporary.name,
                "directory_name": "",
            },
            {
                "url": "https://github.com/lab/demo",
                "parent": self.temporary.name,
                "unexpected": True,
            },
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, _headers, body = self.json_request(
                    "/api/v1/repository-clones",
                    payload,
                    token=self.server.csrf_token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"],
                    "invalid_clone_request",
                )
        self.assertEqual(self.service.clone_calls, [])

        status, _headers, body = self.json_request(
            "/api/v1/jobs/missing-job/cancel",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            self.decode_json(body)["error"]["code"], "job_not_found"
        )

        status, _headers, body = self.json_request(
            "/api/v1/jobs/missing-job/cancel",
            {},
        )
        self.assertEqual(status, 403)
        self.assertEqual(
            self.decode_json(body)["error"]["code"], "invalid_token"
        )

    def test_only_one_background_operation_is_accepted(self):
        self.service.discovery_blocked = True
        status, _headers, body = self.json_request(
            "/api/v1/discoveries",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        first_job_id = self.decode_json(body)["job"]["id"]
        self.assertTrue(self.service.discovery_started.wait(timeout=1))

        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {"fetch": False},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 409)
        conflict = self.decode_json(body)
        self.assertEqual(conflict["error"]["code"], "operation_in_progress")
        self.assertEqual(conflict["active_job"]["id"], first_job_id)
        self.assertEqual(self.service.check_calls, 0)

        self.service.discovery_release.set()
        self.wait_for_job(first_job_id)

    def test_integration_get_is_cached_and_refresh_is_an_explicit_job(self):
        status, _headers, body = self.request(
            "/api/v1/integrations",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertFalse(self.decode_json(body)["checked"])
        self.assertEqual(self.service.integration_calls, 0)

        status, _headers, body = self.json_request(
            "/api/v1/integrations/refresh",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(self.decode_json(body)["job"]["id"])
        self.assertTrue(job["result"]["checked"])
        self.assertEqual(self.service.integration_calls, 1)

        status, _headers, body = self.request(
            "/api/v1/integrations",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        self.assertTrue(payload["checked"])
        self.assertFalse(payload["stale"])
        self.assertEqual(self.service.integration_calls, 1)

    def test_cached_integration_status_goes_stale_instead_of_living_forever(self):
        status, _headers, body = self.json_request(
            "/api/v1/integrations/refresh",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.wait_for_job(self.decode_json(body)["job"]["id"])

        controller = self.server.controller
        controller._integration_cache_at -= INTEGRATION_CACHE_TTL_SECONDS + 1

        status, _headers, body = self.request(
            "/api/v1/integrations",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        # Still served (better than "unknown"), but flagged so the console re-probes.
        self.assertTrue(payload["checked"])
        self.assertTrue(payload["stale"])
        self.assertEqual(self.service.integration_calls, 1)

    def test_check_job_preserves_partial_error_terminal_state(self):
        self.service.check_results = [
            SimpleNamespace(status="error", to_dict=lambda: {"status": "error"})
        ]
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {"fetch": False, "use_codex": False, "publish": False},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(
            self.decode_json(body)["job"]["id"],
            status="completed_with_errors",
        )
        self.assertEqual(job["status"], "completed_with_errors")

    def test_check_run_acknowledgement_reuses_write_request_protections(self):
        run_id = "a" * 32
        path = "/api/v1/check-runs/%s/acknowledge" % run_id
        status, _headers, body = self.json_request(path, {})
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_token")

        status, _headers, body = self.json_request(
            path,
            {},
            token=self.server.csrf_token,
            origin="http://evil.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.decode_json(body)["error"]["code"], "invalid_origin")

        status, _headers, body = self.json_request(
            path, {}, token=self.server.csrf_token
        )
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["check_run"]["id"], run_id)
        self.assertFalse(payload["check_run"]["pending"])
        self.assertEqual(self.service.acknowledged_check_runs, [run_id])

        status, _headers, body = self.json_request(
            path,
            {"unexpected": True},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            self.decode_json(body)["error"]["code"],
            "invalid_check_run_acknowledgement",
        )

        status, _headers, body = self.json_request(
            "/api/v1/check-runs/not-a-run/acknowledge",
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 404)

        status, _headers, body = self.json_request(
            "/api/v1/check-runs/%s/acknowledge" % ("b" * 32),
            {},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            self.decode_json(body)["error"]["code"], "check_run_not_found"
        )

    def test_check_job_forwards_selected_branch_ids(self):
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {
                "fetch": True,
                "use_codex": False,
                "publish": False,
                "branch_ids": [23, 24, 23],
            },
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.wait_for_job(self.decode_json(body)["job"]["id"])
        self.assertEqual(self.service.last_check_branch_ids, [23, 24])

    def test_check_job_forwards_repository_selector(self):
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {
                "fetch": True,
                "use_codex": False,
                "publish": False,
                "repository_id": 10,
            },
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.wait_for_job(self.decode_json(body)["job"]["id"])
        self.assertEqual(self.service.last_check_repository_id, 10)
        self.assertIsNone(self.service.last_check_branch_ids)

    def test_check_job_rejects_invalid_or_ambiguous_repository_selection(self):
        payloads = (
            {"repository_id": None},
            {"repository_id": 0},
            {"repository_id": True},
            {"repository_id": "10"},
            {"repository_id": 10, "branch_ids": [23]},
        )
        for payload in payloads:
            status, _headers, body = self.json_request(
                "/api/v1/checks",
                payload,
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                self.decode_json(body)["error"]["code"], "invalid_selection"
            )

    def test_check_job_rejects_invalid_branch_selection(self):
        for branch_ids in ([], [0], [True], ["23"]):
            status, _headers, body = self.json_request(
                "/api/v1/checks",
                {"branch_ids": branch_ids},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 400)
            self.assertEqual(
                self.decode_json(body)["error"]["code"], "invalid_selection"
            )

    def test_check_job_does_not_expose_silent_baseline_event_id(self):
        self.service.check_results = [
            SimpleNamespace(
                status="baseline",
                event_kind="baseline",
                to_dict=lambda: {
                    "status": "baseline",
                    "event_kind": "baseline",
                    "event_id": 99,
                    "summary": {"title": "silent baseline"},
                },
            )
        ]
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {"fetch": False, "use_codex": False, "publish": False},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(self.decode_json(body)["job"]["id"])
        result = job["result"]["results"][0]
        self.assertEqual(result["status"], "baseline")
        self.assertIsNone(result["event_id"])
        self.assertIsNone(result["summary"])

    def test_publish_unknown_requires_reconciliation(self):
        self.service.check_publications = [{"status": "unknown"}]
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {"fetch": False, "use_codex": False, "publish": True},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(
            self.decode_json(body)["job"]["id"],
            status="reconcile_required",
        )
        self.assertEqual(job["status"], "reconcile_required")

    def test_check_job_does_not_hide_backlog_error_behind_succeeded_run(self):
        self.service.check_publications = [{"event_id": 9, "status": "error"}]
        self.service.last_check_run_value = {
            "id": "a" * 32,
            "status": "succeeded",
            "started_at": "2026-08-21T00:00:00+00:00",
            "finished_at": "2026-08-21T00:00:01+00:00",
            "scope_kind": "all",
            "requested_branch_ids": [],
            "actual_branch_ids": [],
            "events": [],
            "event_ids": [],
            "blocked": [],
        }
        status, _headers, body = self.json_request(
            "/api/v1/checks",
            {"fetch": False, "use_codex": False, "publish": True},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(
            self.decode_json(body)["job"]["id"],
            status="completed_with_errors",
        )
        self.assertEqual(job["status"], "completed_with_errors")

    def test_json_replies_compress_only_when_large_and_accepted(self):
        for index in range(1, 40):
            self.service.database.events[index] = {
                "id": index,
                "repository_id": 1,
                "branch_id": 1,
                "event_key": "BR-compressible-%d" % index,
                "repository": "demo-repository-with-a-long-enough-name",
                "branch": "feature/long-enough-branch-name-%d" % index,
                "kind": "fast_forward",
                "detected_at": "2026-08-18T00:00:00+00:00",
                "from_oid": "a" * 40,
                "to_oid": "b" * 40,
                "summary_source": "rules",
            }

        status, headers, compressed = self.request(
            "/api/v1/dashboard",
            headers={
                "X-Branch-Radar-Token": self.server.csrf_token,
                "Accept-Encoding": "gzip",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(headers.get("Vary"), "Accept-Encoding")
        self.assertEqual(headers.get("Content-Length"), str(len(compressed)))
        payload = json.loads(gzip.decompress(compressed).decode("utf-8"))
        self.assertEqual(len(payload["events"]), 39)

        status, headers, identity = self.request(
            "/api/v1/dashboard",
            headers={
                "X-Branch-Radar-Token": self.server.csrf_token,
                "Accept-Encoding": "identity",
            },
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(json.loads(identity.decode("utf-8")), payload)
        self.assertLess(len(compressed), len(identity))

    def test_small_json_replies_stay_uncompressed(self):
        status, headers, body = self.request(
            "/api/v1/health", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertLess(len(body), MAX_BODY_BYTES)
        self.assertTrue(self.decode_json(body)["ok"])

        status, headers, body = self.request(
            "/api/v1/does-not-exist",
            headers={
                "X-Branch-Radar-Token": self.server.csrf_token,
                "Accept-Encoding": "gzip",
            },
        )
        self.assertEqual(status, 404)
        self.assertIsNone(headers.get("Content-Encoding"))
        self.assertEqual(self.decode_json(body)["error"]["code"], "not_found")

    def test_job_request_waits_for_the_next_change_instead_of_a_timer(self):
        self.service.discovery_blocked = True
        status, _headers, body = self.json_request(
            "/api/v1/discoveries", {}, token=self.server.csrf_token
        )
        self.assertEqual(status, 202)
        job = self.decode_json(body)["job"]
        self.assertIsInstance(job["revision"], int)
        self.assertTrue(self.service.discovery_started.wait(timeout=3))

        # Held open while the job stands still: the reply is the timeout, not a
        # changed job, and the revision the client already knew is unchanged.
        settled = self.request(
            "/api/v1/jobs/%s?wait=1&revision=%d" % (job["id"], self._job_revision(job["id"])),
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(settled[0], 200)
        held_revision = self.decode_json(settled[2])["job"]["revision"]

        # Released mid-request: the reply arrives on the change, well inside the
        # window, rather than after it.
        started = time.monotonic()
        released = threading.Timer(0.15, self.service.discovery_release.set)
        released.start()
        try:
            status, _headers, body = self.request(
                "/api/v1/jobs/%s?wait=10&revision=%d" % (job["id"], held_revision),
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
        finally:
            released.cancel()
        elapsed = time.monotonic() - started
        self.assertEqual(status, 200)
        self.assertLess(elapsed, 5)
        self.assertGreater(self.decode_json(body)["job"]["revision"], held_revision)

    def test_finished_job_replies_at_once_and_bad_wait_is_rejected(self):
        status, _headers, body = self.json_request(
            "/api/v1/discoveries", {}, token=self.server.csrf_token
        )
        self.assertEqual(status, 202)
        job_id = self.decode_json(body)["job"]["id"]
        finished = self.wait_for_job(job_id)

        started = time.monotonic()
        status, _headers, body = self.request(
            "/api/v1/jobs/%s?wait=20&revision=%d" % (job_id, finished["revision"]),
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(self.decode_json(body)["job"]["status"], "succeeded")

        for query in ("wait=99", "wait=abc", "wait=1&wait=2", "unknown=1", "revision=0"):
            status, _headers, body = self.request(
                "/api/v1/jobs/%s?%s" % (job_id, query),
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
            self.assertEqual(status, 400, query)
            self.assertEqual(
                self.decode_json(body)["error"]["code"], "invalid_query", query
            )

    def _job_revision(self, job_id: str) -> int:
        _status, _headers, body = self.request(
            "/api/v1/jobs/" + job_id,
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        return int(self.decode_json(body)["job"]["revision"])

    def test_every_verb_enters_through_the_shared_request_gate(self):
        import inspect

        from branch_radar.web import server as server_module

        for verb, mutating in (("do_GET", False), ("do_POST", True), ("do_DELETE", True)):
            source = inspect.getsource(getattr(server_module.RadarRequestHandler, verb))
            self.assertIn("self._accept_request(", source, verb)
            self.assertEqual("mutating=True" in source, mutating, verb)
            # The gate owns these checks; repeating them per verb is how one
            # eventually goes missing.
            self.assertNotIn("_valid_origin()", source, verb)
            self.assertNotIn("json_required", source, verb)

        gate = inspect.getsource(server_module.RadarRequestHandler._valid_token)
        self.assertIn("secrets.compare_digest", gate)
        self.assertNotIn(
            '!= self.server.csrf_token',
            inspect.getsource(server_module.RadarRequestHandler),
        )

    def test_events_window_is_filtered_by_the_server(self):
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        ages = {1: timedelta(hours=2), 2: timedelta(days=3), 3: timedelta(days=40)}
        for event_id, age in ages.items():
            self.service.database.events[event_id] = {
                "id": event_id,
                "repository_id": 1,
                "branch_id": 1,
                "event_key": "BR-window-%d" % event_id,
                "repository": "demo",
                "branch": "main",
                "kind": "fast_forward",
                "detected_at": (now - age).isoformat(timespec="seconds"),
                "from_oid": "a" * 40,
                "to_oid": "b" * 40,
                "summary_source": "rules",
            }

        def ids(query: str):
            status, _headers, body = self.request(
                "/api/v1/events?" + query,
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
            self.assertEqual(status, 200, query)
            return [event["id"] for event in self.decode_json(body)["events"]]

        self.assertEqual(ids("since_hours=24"), [1])
        self.assertEqual(ids("since_hours=168"), [2, 1])
        self.assertEqual(ids("limit=40"), [3, 2, 1])

        for query in ("since_hours=0", "since_hours=-1", "since_hours=9999999"):
            status, _headers, body = self.request(
                "/api/v1/events?" + query,
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
            self.assertEqual(status, 400, query)
            self.assertEqual(
                self.decode_json(body)["error"]["code"], "invalid_query", query
            )

    def test_connection_is_reused_and_a_rejected_body_does_not_corrupt_it(self):
        import http.client

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=3
        )
        self.addCleanup(connection.close)

        # Two requests, one connection: the socket must survive the first reply.
        for _ in range(2):
            connection.request(
                "GET", "/api/v1/health", headers={"Host": self.server.public_url[7:]}
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.version, 11)
            self.assertTrue(self.decode_json(response.read())["ok"])
        self.assertFalse(response.will_close)

        # A body rejected before it was read must not be left in the stream for
        # the next request on the connection to parse as a request line.
        connection.request(
            "POST",
            "/api/v1/discoveries",
            body=json.dumps({"roots": ["/tmp"]}).encode("utf-8"),
            headers={
                "Host": self.server.public_url[7:],
                "Content-Type": "application/json",
                "Origin": self.base_url,
            },
        )
        rejected = connection.getresponse()
        self.assertEqual(rejected.status, 403)
        self.assertEqual(self.decode_json(rejected.read())["error"]["code"], "invalid_token")
        self.assertTrue(rejected.will_close)

    def test_dashboard_seeds_the_activity_list_through_the_same_window(self):
        status, _headers, body = self.request(
            "/api/v1/dashboard?since_hours=24",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertIsNotNone(self.service.last_dashboard_since)
        self.assertIn("events", self.decode_json(body))

        # No window asked for, none applied: the endpoint stays usable unscoped.
        status, _headers, _body = self.request(
            "/api/v1/dashboard",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(self.service.last_dashboard_since)

        for query in ("since_hours=0", "since_hours=9999999", "limit=5"):
            status, _headers, body = self.request(
                "/api/v1/dashboard?" + query,
                headers={"X-Branch-Radar-Token": self.server.csrf_token},
            )
            self.assertEqual(status, 400, query)
            self.assertEqual(
                self.decode_json(body)["error"]["code"], "invalid_query", query
            )

    def test_every_error_code_has_console_copy_in_both_languages(self):
        """The code is the contract between service and console.

        The service answers in one language; the console renders the copy. A
        code with no entry degrades to a generic failure and the reader loses
        the actual reason, so a missing entry fails here rather than in front
        of a user.
        """
        source_root = Path(__file__).resolve().parents[1] / "src" / "branch_radar"
        raised = set()
        for module in source_root.rglob("*.py"):
            body = module.read_text(encoding="utf-8")
            raised.update(re.findall(r'code="([a-z_]+)"', body))
            raised.update(
                re.findall(r'_json_error\(\s*[^,]+,\s*"([a-z_]+)"', body)
            )
        self.assertTrue(raised, "no error codes were discovered")

        web = source_root / "resources" / "web"
        app = (web / "app.js").read_text(encoding="utf-8")
        registry = re.search(r"const ERROR_COPY = \{(.*?)\n\};", app, re.S)
        self.assertIsNotNone(registry, "app.js must define an ERROR_COPY registry")
        covered = dict(re.findall(r'\n  ([a-z_]+): "([^"]+)",', registry.group(1)))

        self.assertEqual(
            sorted(raised - set(covered)),
            [],
            "these error codes have no console copy in app.js ERROR_COPY",
        )

        translations = (web / "i18n.js").read_text(encoding="utf-8")
        untranslated = sorted(
            {copy for copy in covered.values() if '"%s"' % copy not in translations}
        )
        self.assertEqual(
            untranslated, [], "these console strings have no English in i18n.js"
        )

    def test_event_detail_does_not_expose_emails_remote_or_patch(self):
        self.service.database.events[7] = {
            "id": 7,
            "repository_id": 2,
            "branch_id": 3,
            "event_key": "BR-safe-event",
            "repository": "demo",
            "branch": "main",
            "kind": "fast_forward",
            "detected_at": "2026-08-18T00:00:00+00:00",
            "from_oid": "a" * 40,
            "to_oid": "b" * 40,
            "merge_base_oid": "a" * 40,
            "summary_source": "rules",
            "remote_url": "https://user:password@example.invalid/lab/demo.git",
            "local_path": "/secret/local/path",
            "summary": {
                "title": "Safe title",
                "overview": "Safe overview",
                "categories": [],
                "highlights": [],
                "risks": [],
                "verification": [],
                "deployment": {"impact": "none", "notes": []},
                "confidence": "high",
                "source": "rules",
            },
            "facts": {
                "remote_url": "https://user:password@example.invalid/lab/demo.git",
                "repository_path": "/secret/local/path",
                "patch_excerpt": "TOP_SECRET_PATCH_CONTENT",
                "commit_count": 1,
                "dropped_commit_count": 0,
                "commits": [{
                    "sha": "b" * 40,
                    "parents": ["a" * 40],
                    "subject": "Safe subject",
                    "author_name": "Alice",
                    "author_email": "alice-secret@example.invalid",
                    "author_date": "2026-08-18T00:00:00+00:00",
                    "committer_name": "Bob",
                    "committer_email": "bob-secret@example.invalid",
                    "commit_date": "2026-08-18T00:01:00+00:00",
                }],
                "dropped_commits": [],
                "diff": {
                    "files_changed": 1,
                    "insertions": 2,
                    "deletions": 1,
                    "files": ["src/app.py"],
                },
                "signals": [],
            },
        }

        status, _headers, body = self.request(
            "/api/v1/events/7",
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        event = payload["event"]
        serialized = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("alice-secret@example.invalid", serialized)
        self.assertNotIn("bob-secret@example.invalid", serialized)
        self.assertNotIn("user:password", serialized)
        self.assertNotIn("TOP_SECRET_PATCH_CONTENT", serialized)
        self.assertNotIn("/secret/local/path", serialized)
        self.assertNotIn("remote_url", _all_keys(event))
        self.assertNotIn("patch_excerpt", _all_keys(event))
        self.assertNotIn("author_email", _all_keys(event))
        self.assertNotIn("committer_email", _all_keys(event))
        self.assertEqual(event["commits"][0]["author"], "Alice")

    def test_event_detail_hides_baseline_and_rejects_oversized_ids(self):
        self.service.database.events[10] = {"id": 10, "kind": "baseline"}
        token = {"X-Branch-Radar-Token": self.server.csrf_token}
        for path in (
            "/api/v1/events/10",
            "/api/v1/events/9223372036854775808",
            "/api/v1/events/" + ("0" * 100) + "1",
        ):
            with self.subTest(path=path):
                status, _headers, body = self.request(path, headers=token)
                self.assertEqual(status, 404)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"], "event_not_found"
                )

    def test_event_query_filters_and_pages_without_exposing_sensitive_facts(self):
        def event(event_id: int, repository_id: int, branch_id: int) -> dict[str, Any]:
            return {
                "id": event_id,
                "event_key": "BR-%d" % event_id,
                "repository_id": repository_id,
                "branch_id": branch_id,
                "repository": "demo",
                "branch": "main",
                "kind": "fast_forward",
                "detected_at": "2026-08-18T00:00:00+00:00",
                "from_oid": "a" * 40,
                "to_oid": "b" * 40,
                "merge_base_oid": "a" * 40,
                "summary_source": "rules",
                "local_path": "/secret/repository",
                "remote_url": "https://user:password@example.invalid/demo.git",
                "summary": {},
                "facts": {
                    "repository_path": "/secret/repository",
                    "patch_excerpt": "SECRET_PATCH",
                    "commits": [{"author_email": "secret@example.invalid"}],
                    "diff": {"files": ["secret.txt"]},
                },
            }

        baseline_event = event(10, 2, 3)
        baseline_event["kind"] = "baseline"
        self.service.database.events = {
            10: baseline_event,
            9: event(9, 2, 3),
            8: event(8, 2, 3),
            7: event(7, 4, 5),
        }
        token = {"X-Branch-Radar-Token": self.server.csrf_token}
        status, _headers, body = self.request(
            "/api/v1/events?repository_id=2&branch_id=3&limit=1",
            headers=token,
        )
        self.assertEqual(status, 200)
        first = self.decode_json(body)
        self.assertEqual([row["id"] for row in first["events"]], [9])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_before_id"], 9)
        self.assertEqual(first["events"][0]["repository_id"], 2)
        self.assertEqual(first["events"][0]["branch_id"], 3)

        status, _headers, body = self.request(
            "/api/v1/events?repository_id=2&branch_id=3&before_id=9&limit=1",
            headers=token,
        )
        self.assertEqual(status, 200)
        second = self.decode_json(body)
        self.assertEqual([row["id"] for row in second["events"]], [8])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_before_id"])
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("/secret/repository", serialized)
        self.assertNotIn("user:password", serialized)
        self.assertNotIn("secret@example.invalid", serialized)
        self.assertNotIn("SECRET_PATCH", serialized)
        self.assertNotIn("secret.txt", serialized)

    def test_event_query_rejects_unknown_duplicate_and_out_of_range_parameters(self):
        token = {"X-Branch-Radar-Token": self.server.csrf_token}
        queries = (
            "limit=0",
            "limit=101",
            "limit=1&limit=2",
            "repository_id=-1",
            "repository_id=9223372036854775808",
            "repository_id=" + ("0" * 100) + "1",
            "branch_id=",
            "unknown=1",
        )
        for query in queries:
            with self.subTest(query=query):
                status, _headers, body = self.request(
                    "/api/v1/events?" + query,
                    headers=token,
                )
                self.assertEqual(status, 400)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"], "invalid_query"
                )

    def test_watch_job_returns_safe_repository_and_branch_ids(self):
        status, _headers, body = self.json_request(
            "/api/v1/watches",
            {"entries": [{"path": "/private/project", "branches": ["main"]}]},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(self.decode_json(body)["job"]["id"])
        watched = job["result"]["watched"][0]
        self.assertEqual(watched["repository_id"], 10)
        self.assertEqual(watched["branch_id"], 21)
        self.assertNotIn("/private/project", json.dumps(job, ensure_ascii=False))

    def test_delete_watch_requires_same_origin_session_and_json(self):
        cases = (
            ({"token": None}, 403, "invalid_token"),
            ({"token": self.server.csrf_token, "origin": None}, 403, "invalid_origin"),
            ({
                "token": self.server.csrf_token,
                "content_type": "text/plain",
            }, 415, "json_required"),
        )
        for options, expected_status, expected_code in cases:
            with self.subTest(code=expected_code):
                status, _headers, body = self.json_request(
                    "/api/v1/watches/21",
                    {},
                    method="DELETE",
                    **options,
                )
                self.assertEqual(status, expected_status)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"], expected_code
                )
        self.assertEqual(self.service.removed_watch_ids, [])

    def test_delete_watch_returns_only_safe_identity(self):
        status, _headers, body = self.json_request(
            "/api/v1/watches/21",
            {},
            token=self.server.csrf_token,
            method="DELETE",
        )

        self.assertEqual(status, 200)
        payload = self.decode_json(body)
        self.assertEqual(payload, {
            "ok": True,
            "removed": {
                "branch_id": 21,
                "repository_id": 10,
                "repository": "demo",
                "branch": "main",
            },
        })
        self.assertEqual(self.service.removed_watch_ids, [21])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("path", serialized)
        self.assertNotIn("remote", serialized)

    def test_delete_watch_validates_path_body_and_missing_watch(self):
        for path, payload, expected_code in (
            ("/api/v1/watches/0", {}, "invalid_watch_id"),
            ("/api/v1/watches/not-an-id", {}, "invalid_watch_id"),
            ("/api/v1/watches/21/extra", {}, "not_found"),
            ("/api/v1/watches/21", {"unexpected": True}, "invalid_watch"),
        ):
            with self.subTest(path=path, code=expected_code):
                status, _headers, body = self.json_request(
                    path,
                    payload,
                    token=self.server.csrf_token,
                    method="DELETE",
                )
                self.assertEqual(status, 400 if expected_code != "not_found" else 404)
                self.assertEqual(
                    self.decode_json(body)["error"]["code"], expected_code
                )

        with mock.patch.object(
            self.service,
            "remove_watch",
            side_effect=ServiceError("没有找到该关注分支", code="watch_not_found"),
        ):
            status, _headers, body = self.json_request(
                "/api/v1/watches/999",
                {},
                token=self.server.csrf_token,
                method="DELETE",
            )
        self.assertEqual(status, 404)
        self.assertEqual(
            self.decode_json(body)["error"]["code"], "watch_not_found"
        )

    def test_watch_job_reports_bounded_progress_while_fetching(self):
        self.service.watch_blocked = True
        status, _headers, body = self.json_request(
            "/api/v1/watches",
            {"entries": [{"path": "/private/project", "branches": ["main", "feature"]}]},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        job_id = self.decode_json(body)["job"]["id"]
        self.assertTrue(self.service.watch_started.wait(timeout=1))

        status, _headers, body = self.request(
            "/api/v1/jobs/" + job_id,
            headers={"X-Branch-Radar-Token": self.server.csrf_token},
        )
        self.assertEqual(status, 200)
        running = self.decode_json(body)["job"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["progress"]["phase"], "fetch")
        self.assertEqual(running["progress"]["total"], 2)
        self.assertTrue(running["progress"]["indeterminate"])
        self.assertNotIn("/private/project", json.dumps(running, ensure_ascii=False))

        self.service.watch_release.set()
        completed = self.wait_for_job(job_id)
        self.assertEqual(completed["progress"]["phase"], "complete")
        self.assertEqual(completed["progress"]["current"], 2)

    def test_watch_job_marks_pending_baseline_as_completed_with_errors(self):
        result = {
            "watched": [{
                "repository_id": 10,
                "branch_id": 21,
                "repository": "demo",
                "branch": "main",
                "baseline_pending": True,
                "warning": "远程暂时不可用",
            }],
            "skipped": [],
        }
        with mock.patch.object(self.service, "add_watches", return_value=result):
            status, _headers, body = self.json_request(
                "/api/v1/watches",
                {"entries": [{"path": "/private/project", "branches": ["main"]}]},
                token=self.server.csrf_token,
            )
            self.assertEqual(status, 202)
            job = self.wait_for_job(
                self.decode_json(body)["job"]["id"],
                status="completed_with_errors",
            )

        self.assertTrue(job["result"]["watched"][0]["baseline_pending"])
        self.assertEqual(job["status"], "completed_with_errors")

    def test_lark_create_request_id_reuses_same_body_and_rejects_conflict(self):
        self.service.lark_create_blocked = True
        request_id = "same-request-key-1234"
        status, _headers, body = self.json_request(
            "/api/v1/lark/create",
            {"title": "First title", "request_id": request_id},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        first_job_id = self.decode_json(body)["job"]["id"]
        self.assertTrue(self.service.lark_create_started.wait(timeout=1))

        status, _headers, body = self.json_request(
            "/api/v1/lark/create",
            {"title": "First title", "request_id": request_id},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.decode_json(body)["job"]["id"], first_job_id)
        self.assertEqual(self.service.lark_create_calls, 1)
        self.assertEqual(self.service.lark_create_titles, ["First title"])

        status, _headers, body = self.json_request(
            "/api/v1/lark/create",
            {"title": "A conflicting retry title", "request_id": request_id},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(
            self.decode_json(body)["error"]["code"], "idempotency_conflict"
        )

        self.service.lark_create_release.set()
        self.wait_for_job(first_job_id)

        status, _headers, body = self.json_request(
            "/api/v1/lark/create",
            {"title": "First title", "request_id": request_id},
            token=self.server.csrf_token,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.decode_json(body)["job"]["id"], first_job_id)
        self.assertEqual(self.service.lark_create_calls, 1)


def _all_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(_all_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_all_keys(child))
    return keys


if __name__ == "__main__":
    unittest.main()
