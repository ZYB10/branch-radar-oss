from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ..security import redact_sensitive_text


def watch_view(row: dict[str, Any]) -> dict[str, Any]:
    error = row.get("last_error")
    if error:
        error = redact_sensitive_text(str(error))
        local_path = row.get("local_path")
        if local_path:
            error = error.replace(str(local_path), "[repository]")
    return {
        "id": row.get("branch_id"),
        "repository_id": row.get("repository_id"),
        "repository": row.get("repository"),
        "repository_alias": _safe_repository_alias(row.get("repository_alias")),
        "repository_tags": _repository_tags(row.get("repository_tags_json")),
        "branch": row.get("branch"),
        "remote_name": row.get("remote_name"),
        "present": bool(row.get("present")),
        "sha": row.get("last_observed_oid"),
        "last_checked_at": row.get("last_checked_at"),
        "last_success_at": row.get("last_success_at"),
        "error": error[:1000] if error else None,
        "baseline_pending": row.get("last_observed_oid") is None
        and row.get("last_success_at") is None
        and not bool(row.get("last_error")),
    }


def _safe_repository_alias(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized[:80] or None


def _repository_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [
        item.strip()[:24]
        for item in value[:8]
        if isinstance(item, str) and item.strip()
    ]


def candidate_view(row: dict[str, Any]) -> dict[str, Any]:
    branches = [
        str(item).strip()
        for item in (row.get("branches") or [])
        if isinstance(item, str) and item.strip()
    ]
    watched_branches = [
        str(item).strip()
        for item in (row.get("watched_branches") or [])
        if isinstance(item, str) and item.strip()
    ]
    error = row.get("error")
    if error:
        error = redact_sensitive_text(str(error))
        path = row.get("path")
        if path:
            error = error.replace(str(path), "[repository]")
    return {
        "repository_id": row.get("repository_id"),
        "name": row.get("name"),
        "path": row.get("path"),
        "remote_name": row.get("remote_name"),
        "canonical_remote": row.get("canonical_remote"),
        "current_branch": row.get("current_branch"),
        "upstream_branch": row.get("upstream_branch"),
        "default_branch": row.get("default_branch"),
        "is_worktree": bool(row.get("is_worktree")),
        "branches": branches[:200],
        "watched_branches": watched_branches[:200],
        "error": error[:1000] if error else None,
    }


def event_view(row: dict[str, Any], *, detailed: bool = False) -> dict[str, Any]:
    facts = row.get("facts") or {}
    list_metadata = row.get("list_metadata") or {}
    summary = row.get("summary") or {}
    commits = facts.get("commits") or []
    value: dict[str, Any] = {
        "id": row.get("id"),
        "repository_id": row.get("repository_id"),
        "branch_id": row.get("branch_id"),
        "repository": row.get("repository"),
        "branch": row.get("branch"),
        "kind": row.get("kind"),
        "detected_at": row.get("detected_at"),
        "topology": {
            # These are observed Git facts, not a reconstructed branch graph.
            # Keep absent or malformed endpoints null so the canvas can render
            # an explicit history gap instead of inventing a connection.
            "from_oid": _safe_oid(row.get("from_oid")),
            "to_oid": _safe_oid(row.get("to_oid")),
            "merge_base_oid": _safe_oid(row.get("merge_base_oid")),
        },
        "summary": {
            "title": summary.get("title"),
            "overview": summary.get("overview"),
        },
        "authors": list(row.get("authors") or _unique(
            commit.get("author_name") for commit in commits if isinstance(commit, dict)
        ))[:12],
        "check_run_id": row.get("check_run_id"),
        "is_new": bool(row.get("is_new")),
        "analysis_complete": bool(list_metadata.get("analysis_complete", True)),
        "timeline": _compact_timeline(list_metadata.get("timeline")),
        "categories": [
            str(item)[:40]
            for item in (list_metadata.get("categories") or [])[:12]
            if isinstance(item, str) and item
        ],
        "signals": [
            str(item)[:300]
            for item in (list_metadata.get("signals") or [])
            if isinstance(item, str) and "breaking" in item.casefold()
        ][:3],
        "stats": _compact_stats(list_metadata.get("stats")),
    }
    if detailed:
        dropped_commits = facts.get("dropped_commits") or []
        diff = facts.get("diff") or {}
        value.update({
            "key": row.get("event_key"),
            "old_sha": row.get("from_oid"),
            "new_sha": row.get("to_oid"),
            "merge_base": row.get("merge_base_oid"),
            "summary": _summary_view(summary),
            "summary_source": row.get("summary_source") or summary.get("source"),
            "delivery_state": row.get("delivery_state"),
            "analysis_complete": facts.get("analysis_complete", True),
            "commits_truncated": bool(facts.get("commits_truncated")),
            "stats": {
                "commits": facts.get("commit_count"),
                "dropped_commits": facts.get("dropped_commit_count"),
                "files": diff.get("files_changed", 0),
                "insertions": diff.get("insertions", 0),
                "deletions": diff.get("deletions", 0),
            },
        })
        value["commits"] = [
            _commit_view(commit) for commit in _commits_newest_first(commits)[:50]
        ]
        value["dropped"] = [_commit_view(commit) for commit in dropped_commits[:50]]
        value["files"] = [str(path) for path in (diff.get("files") or [])[:100]]
        value["signals"] = [str(item) for item in (facts.get("signals") or [])]
    return value


def integration_view(payload: dict[str, Any]) -> dict[str, Any]:
    codex = payload.get("codex") or {}
    lark = payload.get("lark") or {}
    document_details = lark.get("document_details")
    if not isinstance(document_details, dict):
        document_details = {}
    return {
        "codex": {
            "detected": bool(codex.get("detected") or codex.get("available")),
            "available": bool(codex.get("available")),
            "authenticated": bool(codex.get("authenticated")),
            "auth_mode": _safe_auth_mode(codex.get("auth_mode"))
            if codex.get("authenticated") else None,
            "version": _safe_label(codex.get("version")),
            "error": _integration_error(codex.get("error"), provider="Codex"),
        },
        "lark": {
            "available": bool(lark.get("available")),
            "verified": bool(lark.get("verified")),
            "version": _safe_label(lark.get("version")),
            "user_name": _safe_label(lark.get("user_name")),
            "document_configured": bool(lark.get("document")),
            "document_name": _safe_label(document_details.get("title")),
            "document_url": _safe_lark_document_url(
                document_details.get("url") or lark.get("document")
            ),
            "error": _integration_error(lark.get("error"), provider="飞书"),
        },
    }


def dashboard_view(payload: dict[str, Any]) -> dict[str, Any]:
    settings = payload.get("settings") or {}
    health = payload.get("daemon_health") or {}
    try:
        consecutive_failures = max(0, int(health.get("consecutive_failures") or 0))
    except (TypeError, ValueError, OverflowError):
        consecutive_failures = 0
    try:
        pending_check_run_count = max(
            0, int(payload.get("pending_check_run_count") or 0)
        )
    except (TypeError, ValueError, OverflowError):
        pending_check_run_count = 0
    return {
        "counts": dict(payload.get("counts") or {}),
        "watches": [watch_view(row) for row in payload.get("watches") or []],
        "events": [event_view(row) for row in payload.get("events") or []],
        "events_has_more": bool(payload.get("events_has_more")),
        "events_next_before_id": payload.get("events_next_before_id"),
        "latest_check_run": check_run_view(payload.get("latest_check_run")),
        "pending_check_run": check_run_view(payload.get("pending_check_run")),
        "pending_check_run_count": pending_check_run_count,
        "daemon_health": {
            "last_attempt_at": _safe_timestamp(health.get("last_attempt_at")),
            "last_success_at": _safe_timestamp(health.get("last_success_at")),
            "last_failure_at": _safe_timestamp(health.get("last_failure_at")),
            "consecutive_failures": consecutive_failures,
        },
        "settings": {
            "scan_roots": _scan_roots(settings.get("scan_roots")),
            "poll_interval_seconds": settings.get("poll_interval_seconds"),
            "codex_enabled": bool(settings.get("codex_enabled")),
            "lark_enabled": bool(settings.get("lark_enabled")),
            "lark_document_configured": bool(settings.get("lark_document")),
        },
    }


def check_run_view(row: Any) -> Any:
    if not isinstance(row, dict):
        return None

    def count(key: str) -> int:
        value = row.get(key)
        if isinstance(value, bool):
            return 0
        try:
            return max(0, min(int(value or 0), 2_147_483_647))
        except (TypeError, ValueError, OverflowError):
            return 0

    raw_requested = row.get("requested_branch_ids")
    raw_actual = row.get("actual_branch_ids")
    requested_count = _positive_integer_count(raw_requested)
    actual_count = _positive_integer_count(raw_actual)
    requested = _safe_integer_list(raw_requested, limit=500)
    actual = _safe_integer_list(raw_actual, limit=500)
    events = []
    for item in (row.get("events") or [])[:200]:
        if not isinstance(item, dict):
            continue
        events.append({
            "id": item.get("id"),
            "repository": _safe_label(item.get("repository")),
            "branch": _safe_label(item.get("branch"), max_length=500),
            "kind": _safe_label(item.get("kind")),
            "title": _safe_label(item.get("title"), max_length=300),
            "categories": [
                str(category)[:40]
                for category in (item.get("categories") or [])[:12]
                if isinstance(category, str) and category
            ],
        })
    blocked = []
    for item in (row.get("blocked") or [])[:50]:
        if not isinstance(item, dict):
            continue
        error = item.get("error")
        blocked.append({
            "branch_id": item.get("branch_id"),
            "repository_id": item.get("repository_id"),
            "repository": _safe_label(item.get("repository_name")),
            "branch": _safe_label(item.get("branch_name"), max_length=500),
            "status": _safe_label(item.get("status")),
            "error": redact_sensitive_text(str(error))[:1000] if error else None,
        })
    summary = _check_run_summary_view(row.get("summary"))
    error = row.get("last_error")
    event_ids = _safe_integer_list(row.get("event_ids"), limit=200)
    actionable = any(
        count(key) > 0
        for key in (
            "event_count",
            "blocked_count",
            "error_count",
            "publication_failed_count",
            "publication_unknown_count",
            "publication_backlog_failed_count",
            "publication_backlog_unknown_count",
        )
    ) or row.get("status") in {"failed", "interrupted"}
    return {
        "id": row.get("id") if isinstance(row.get("id"), str) else None,
        "status": _safe_label(row.get("status")),
        "started_at": _safe_timestamp(row.get("started_at")),
        "finished_at": _safe_timestamp(row.get("finished_at")),
        "acknowledged_at": _safe_timestamp(row.get("acknowledged_at")),
        "actionable": actionable,
        "pending": bool(
            actionable and row.get("finished_at") and not row.get("acknowledged_at")
        ),
        "scope": {
            "kind": row.get("scope_kind")
            if row.get("scope_kind") in {"all", "repository", "branches"}
            else "all",
            "repository_id": row.get("repository_id"),
            "requested_branch_ids": requested,
            "actual_branch_ids": actual,
        },
        "options": {
            "fetch": bool(row.get("fetch")),
            "use_codex": bool(row.get("use_codex")),
            "publish": bool(row.get("publish")),
        },
        "counts": {
            "requested": actual_count
            if row.get("scope_kind") != "branches" else requested_count,
            "checked": count("checked_count"),
            "changed": count("changed_count"),
            "unchanged": count("unchanged_count"),
            "baselines": count("baseline_count"),
            "blocked": count("blocked_count"),
            "errors": count("error_count"),
            "events": count("event_count"),
        },
        "event_ids": event_ids,
        "events": events,
        "events_truncated": bool(row.get("events_truncated")),
        "blocked": blocked,
        "blocked_truncated": bool(row.get("blocked_truncated")),
        "publication": {
            "requested": count("publication_requested_count"),
            "succeeded": count("publication_succeeded_count"),
            "failed": count("publication_failed_count"),
            "unknown": count("publication_unknown_count"),
            "backlog": {
                "succeeded": count("publication_backlog_succeeded_count"),
                "failed": count("publication_backlog_failed_count"),
                "unknown": count("publication_backlog_unknown_count"),
            },
            "truncated": bool(row.get("publications_truncated")),
        },
        "summary": summary,
        "summary_warning": redact_sensitive_text(
            str(row.get("summary_warning") or "")
        )[:1000] or None,
        "error": redact_sensitive_text(str(error))[:1000] if error else None,
    }


def _check_run_summary_view(value: Any) -> Any:
    if not isinstance(value, dict):
        return None

    def text(key: str, limit: int) -> str:
        item = value.get(key)
        return redact_sensitive_text(item).strip()[:limit] if isinstance(item, str) else ""

    def strings(key: str) -> list[str]:
        items = value.get(key)
        if not isinstance(items, list):
            return []
        return [
            redact_sensitive_text(item).strip()[:500]
            for item in items[:5]
            if isinstance(item, str) and item.strip()
        ]

    return {
        "title": text("title", 200),
        "overview": text("overview", 1200),
        "themes": strings("themes"),
        "priorities": strings("priorities"),
        "connections": strings("connections"),
        "completeness_note": text("completeness_note", 1000),
        "source": value.get("source")
        if value.get("source") in {"rules", "codex"} else "rules",
    }


def _safe_integer_list(value: Any, *, limit: int) -> list[int]:
    if not isinstance(value, list):
        return []
    return [
        item for item in value[:limit]
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]


def _positive_integer_count(value: Any) -> int:
    if not isinstance(value, list):
        return 0
    return sum(
        isinstance(item, int) and not isinstance(item, bool) and item > 0
        for item in value
    )


def _compact_stats(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}

    def count(key: str) -> int:
        item = source.get(key)
        if isinstance(item, bool):
            return 0
        try:
            return max(0, min(int(item or 0), 2_147_483_647))
        except (TypeError, ValueError, OverflowError):
            return 0

    insertions = count("insertions")
    deletions = count("deletions")
    return {
        "commits": count("commits"),
        "files": count("files"),
        "insertions": insertions,
        "deletions": deletions,
        "changes": min(insertions + deletions, 2_147_483_647),
    }


def _compact_timeline(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    tip_committed_at = _safe_aware_timestamp(source.get("tip_committed_at"))
    first_committed_at = _safe_aware_timestamp(source.get("first_committed_at"))
    last_committed_at = _safe_aware_timestamp(source.get("last_committed_at"))
    raw_authors = source.get("authors")
    author_items = raw_authors if isinstance(raw_authors, list) else []
    authors = []
    for item in author_items[:12]:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        if not isinstance(raw_name, str):
            continue
        name = " ".join(redact_sensitive_text(raw_name[:1000]).split())[:120]
        if not name:
            continue
        sampled_commits = item.get("sampled_commits")
        if isinstance(sampled_commits, bool):
            sampled_commits = 0
        try:
            sampled_count = max(0, min(int(sampled_commits or 0), 2_147_483_647))
        except (TypeError, ValueError, OverflowError):
            sampled_count = 0
        authors.append(
            {
                "name": name,
                "first_committed_at": _safe_aware_timestamp(
                    item.get("first_committed_at")
                ),
                "last_committed_at": _safe_aware_timestamp(
                    item.get("last_committed_at")
                ),
                "sampled_commits": sampled_count,
            }
        )
    basis = (
        "tip_commit"
        if source.get("basis") == "tip_commit" and tip_committed_at is not None
        else "detected"
    )
    if basis == "detected":
        tip_committed_at = None
    return {
        "basis": basis,
        "tip_committed_at": tip_committed_at,
        "first_committed_at": first_committed_at,
        "last_committed_at": last_committed_at,
        "range_complete": source.get("range_complete") is True,
        "authors": authors,
    }


def _scan_roots(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value[:16]
        if isinstance(item, str) and item.strip() and len(item.strip()) <= 4096
    ]


def _safe_timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 64:
        return None
    return normalized


def _safe_aware_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        normalized = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    return normalized.isoformat()


def _summary_view(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": summary.get("title"),
        "overview": summary.get("overview"),
        "categories": [str(item) for item in (summary.get("categories") or [])],
        "highlights": [str(item) for item in (summary.get("highlights") or [])],
        "risks": [
            {
                "level": item.get("level"),
                "area": item.get("area"),
                "reason": item.get("reason"),
            }
            for item in (summary.get("risks") or [])
            if isinstance(item, dict)
        ],
        "verification": [str(item) for item in (summary.get("verification") or [])],
        "deployment": summary.get("deployment") or {"impact": "unknown", "notes": []},
        "confidence": summary.get("confidence"),
        "source": summary.get("source"),
    }


def _commit_view(commit: dict[str, Any]) -> dict[str, Any]:
    return {
        "sha": commit.get("sha"),
        "subject": commit.get("subject"),
        "author": commit.get("author_name"),
        "author_date": commit.get("author_date"),
        "committer": commit.get("committer_name"),
        "commit_date": commit.get("commit_date"),
        "merge": len(commit.get("parents") or []) > 1,
    }


def _safe_oid(value: Any) -> str | None:
    """Return a full SHA-1/SHA-256 object id, never an inferred endpoint."""
    if not isinstance(value, str) or len(value) not in {40, 64}:
        return None
    normalized = value.casefold()
    if (
        not normalized.strip("0")
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        return None
    return normalized


def _commits_newest_first(commits: Iterable[Any]) -> list[dict[str, Any]]:
    """Order display copies by commit time without mutating authoritative facts."""
    safe_commits = [commit for commit in commits if isinstance(commit, dict)]
    return sorted(safe_commits, key=_commit_sort_time, reverse=True)


def _commit_sort_time(commit: dict[str, Any]) -> datetime:
    value = commit.get("commit_date")
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def _unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen = set()
    for value in values:
        if not value:
            continue
        text = str(value)
        if text not in seen:
            result.append(text)
            seen.add(text)
    return result[:12]


def _integration_error(value: Any, *, provider: str) -> Any:
    if not value:
        return None
    normalized = redact_sensitive_text(str(value)).lower()
    if "未找到" in normalized or "not found" in normalized:
        return (
            "当前 Branch Radar 进程未找到 %s CLI；"
            "请刷新检测，或在配置中指定可执行文件路径。" % provider
        )
    if "超时" in normalized or "timed out" in normalized or "timeout" in normalized:
        return "%s 连接检查超时，请稍后重试。" % provider
    if "keychain" in normalized or "钥匙串" in normalized:
        return "%s：无法读取系统钥匙串，请在普通 Terminal 中重新登录。" % provider
    if any(term in normalized for term in ("auth", "login", "token", "身份", "授权")):
        return "%s 身份验证失败，请在普通 Terminal 中重新登录。" % provider
    return "%s 连接检查失败，请在普通 Terminal 中运行 branch-radar doctor --json。" % provider


def _safe_label(value: Any, *, max_length: int = 120) -> Any:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    return redact_sensitive_text(normalized)[:max_length]


def _safe_lark_document_url(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 2048:
        return None
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    allowed_host = any(
        host == domain or host.endswith("." + domain)
        for domain in ("feishu.cn", "larksuite.com")
    )
    allowed_path = any(
        parsed.path.startswith(prefix) for prefix in ("/docx/", "/docs/", "/wiki/")
    )
    if (
        parsed.scheme != "https"
        or not allowed_host
        or not allowed_path
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return urlunsplit(("https", host, parsed.path, "", ""))


def _safe_auth_mode(value: Any) -> str:
    if isinstance(value, str) and value in {"chatgpt", "api_key", "authenticated"}:
        return value
    return "authenticated"
