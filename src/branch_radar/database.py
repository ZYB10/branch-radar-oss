from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import ChangeFacts, RepositoryCandidate, Summary
from .retention import parse_retention_cutoff
from .security import redact_sensitive_text

SCHEMA_VERSION = 3

MAX_CHECK_RUN_EVENT_REFS = 200
MAX_CHECK_RUN_BLOCKED_REFS = 50
MAX_CHECK_RUN_PUBLICATIONS = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConcurrentUpdate(RuntimeError):
    pass


class UnsupportedSchemaVersion(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        if os.name == "posix":
            self.path.chmod(0o600)
        with self.connect() as connection:
            version_row = connection.execute("PRAGMA user_version").fetchone()
            current_version = int(version_row[0]) if version_row is not None else 0
            if current_version > SCHEMA_VERSION:
                raise UnsupportedSchemaVersion(
                    "状态库版本 %d 高于当前程序支持的版本 %d；请升级 Branch Radar"
                    % (current_version, SCHEMA_VERSION)
                )
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                PRAGMA busy_timeout = 5000;

                CREATE TABLE IF NOT EXISTS repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    display_name TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    local_path TEXT NOT NULL UNIQUE,
                    remote_name TEXT,
                    remote_url TEXT,
                    canonical_remote TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_error TEXT
                );

                CREATE INDEX IF NOT EXISTS repositories_remote_idx
                    ON repositories(canonical_remote);

                CREATE TABLE IF NOT EXISTS tracked_branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    remote_name TEXT,
                    remote_fingerprint TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    present INTEGER NOT NULL DEFAULT 1,
                    last_observed_oid TEXT,
                    last_present_oid TEXT,
                    observation_sequence INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(repository_id, name)
                );

                CREATE TABLE IF NOT EXISTS branch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    tracked_branch_id INTEGER NOT NULL REFERENCES tracked_branches(id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    from_oid TEXT,
                    to_oid TEXT,
                    merge_base_oid TEXT,
                    detected_at TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    authors_json TEXT,
                    summary_json TEXT,
                    summary_source TEXT,
                    list_metadata_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(tracked_branch_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS check_runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    repository_id INTEGER,
                    requested_branch_ids_json TEXT NOT NULL DEFAULT '[]',
                    actual_branch_ids_json TEXT NOT NULL DEFAULT '[]',
                    fetch INTEGER NOT NULL,
                    use_codex INTEGER NOT NULL,
                    publish INTEGER NOT NULL,
                    checked_count INTEGER NOT NULL DEFAULT 0,
                    changed_count INTEGER NOT NULL DEFAULT 0,
                    unchanged_count INTEGER NOT NULL DEFAULT 0,
                    baseline_count INTEGER NOT NULL DEFAULT 0,
                    blocked_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    event_count INTEGER NOT NULL DEFAULT 0,
                    publications_json TEXT NOT NULL DEFAULT '[]',
                    publications_truncated INTEGER NOT NULL DEFAULT 0,
                    publication_requested_count INTEGER NOT NULL DEFAULT 0,
                    publication_succeeded_count INTEGER NOT NULL DEFAULT 0,
                    publication_failed_count INTEGER NOT NULL DEFAULT 0,
                    publication_unknown_count INTEGER NOT NULL DEFAULT 0,
                    publication_backlog_succeeded_count INTEGER NOT NULL DEFAULT 0,
                    publication_backlog_failed_count INTEGER NOT NULL DEFAULT 0,
                    publication_backlog_unknown_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT,
                    summary_source TEXT,
                    summary_warning TEXT,
                    last_error TEXT,
                    acknowledged_at TEXT
                );

                CREATE TABLE IF NOT EXISTS check_run_branches (
                    check_run_id TEXT NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
                    branch_id INTEGER NOT NULL,
                    repository_id INTEGER NOT NULL,
                    repository_name TEXT NOT NULL,
                    branch_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    event_id INTEGER REFERENCES branch_events(id) ON DELETE SET NULL,
                    error TEXT,
                    PRIMARY KEY(check_run_id, branch_id)
                );

                CREATE TABLE IF NOT EXISTS check_run_events (
                    check_run_id TEXT NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL REFERENCES branch_events(id) ON DELETE CASCADE,
                    PRIMARY KEY(check_run_id, event_id),
                    UNIQUE(event_id)
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES branch_events(id) ON DELETE CASCADE,
                    destination_type TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    leased_until TEXT,
                    external_ref TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(event_id, destination_type, destination_key)
                );

                CREATE TABLE IF NOT EXISTS external_requests (
                    request_key TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_health (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    last_prune_at TEXT
                );

                INSERT OR IGNORE INTO runtime_health(singleton) VALUES (1);

                CREATE INDEX IF NOT EXISTS tracked_branches_enabled_repository_idx
                    ON tracked_branches(repository_id, name COLLATE NOCASE)
                    WHERE enabled=1;

                CREATE INDEX IF NOT EXISTS branch_events_nonbaseline_id_idx
                    ON branch_events(id DESC)
                    WHERE kind!='baseline';

                CREATE INDEX IF NOT EXISTS branch_events_branch_nonbaseline_id_idx
                    ON branch_events(tracked_branch_id, id DESC)
                    WHERE kind!='baseline';

                CREATE INDEX IF NOT EXISTS branch_events_detected_at_idx
                    ON branch_events(detected_at);

                CREATE INDEX IF NOT EXISTS check_runs_finished_idx
                    ON check_runs(finished_at DESC);

                CREATE INDEX IF NOT EXISTS check_runs_pending_idx
                    ON check_runs(started_at)
                    WHERE acknowledged_at IS NULL;

                CREATE INDEX IF NOT EXISTS check_run_events_event_idx
                    ON check_run_events(event_id);

                CREATE INDEX IF NOT EXISTS deliveries_event_latest_idx
                    ON deliveries(event_id, id DESC);

                CREATE INDEX IF NOT EXISTS deliveries_destination_state_idx
                    ON deliveries(destination_type, destination_key, state, id);

                CREATE INDEX IF NOT EXISTS deliveries_active_state_idx
                    ON deliveries(state)
                    WHERE state IN ('pending', 'retry', 'unknown', 'inflight');
                """
            )
            self._ensure_column(connection, "deliveries", "lease_token", "TEXT")
            self._ensure_column(connection, "deliveries", "leased_until", "TEXT")
            self._ensure_column(connection, "tracked_branches", "last_present_oid", "TEXT")
            self._ensure_column(connection, "tracked_branches", "remote_fingerprint", "TEXT")
            self._ensure_column(connection, "repositories", "display_name", "TEXT")
            self._ensure_column(connection, "branch_events", "authors_json", "TEXT")
            self._ensure_column(
                connection,
                "branch_events",
                "list_metadata_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            for column in (
                "publication_backlog_succeeded_count",
                "publication_backlog_failed_count",
                "publication_backlog_unknown_count",
            ):
                self._ensure_column(
                    connection,
                    "check_runs",
                    column,
                    "INTEGER NOT NULL DEFAULT 0",
                )
            self._ensure_column(
                connection,
                "check_runs",
                "publications_truncated",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection, "repositories", "tags_json", "TEXT NOT NULL DEFAULT '[]'"
            )
            connection.execute(
                """
                UPDATE tracked_branches
                SET last_present_oid=last_observed_oid
                WHERE last_present_oid IS NULL AND last_observed_oid IS NOT NULL
                """
            )
            connection.execute(
                """
                UPDATE tracked_branches
                SET remote_fingerprint=(
                    SELECT r.canonical_remote
                    FROM repositories r
                    WHERE r.id=tracked_branches.repository_id
                      AND r.remote_name=tracked_branches.remote_name
                )
                WHERE remote_name IS NOT NULL
                  AND remote_fingerprint IS NULL
                """
            )
            self._backfill_event_authors(connection)
            self._backfill_event_list_metadata(
                connection,
                force=current_version < SCHEMA_VERSION,
            )
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)

    @classmethod
    def _backfill_event_authors(cls, connection: sqlite3.Connection) -> None:
        while True:
            rows = connection.execute(
                """
                SELECT id, facts_json
                FROM branch_events
                WHERE authors_json IS NULL
                ORDER BY id
                LIMIT 500
                """
            ).fetchall()
            if not rows:
                return
            updates = []
            for row in rows:
                try:
                    facts = json.loads(row["facts_json"])
                except (TypeError, json.JSONDecodeError):
                    facts = {}
                if not isinstance(facts, dict):
                    facts = {}
                updates.append(
                    (
                        json.dumps(
                            cls._authors_from_facts(facts),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        row["id"],
                    )
                )
            connection.executemany(
                "UPDATE branch_events SET authors_json=? WHERE id=?", updates
            )

    @classmethod
    def _backfill_event_list_metadata(
        cls,
        connection: sqlite3.Connection,
        *,
        force: bool = False,
    ) -> None:
        last_id = 0
        while True:
            missing_clause = (
                ""
                if force
                else "AND (list_metadata_json IS NULL OR list_metadata_json='{}')"
            )
            rows = connection.execute(
                """
                SELECT id, to_oid, facts_json
                FROM branch_events
                WHERE id>? %s
                ORDER BY id
                LIMIT 500
                """
                % missing_clause,
                (last_id,),
            ).fetchall()
            if not rows:
                return
            updates = []
            for row in rows:
                try:
                    facts = json.loads(row["facts_json"])
                except (TypeError, json.JSONDecodeError):
                    facts = {"analysis_complete": False}
                if not isinstance(facts, dict):
                    facts = {"analysis_complete": False}
                updates.append(
                    (
                        json.dumps(
                            cls._event_list_metadata(
                                facts,
                                to_oid=row["to_oid"],
                            ),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        row["id"],
                    )
                )
            connection.executemany(
                "UPDATE branch_events SET list_metadata_json=? WHERE id=?", updates
            )
            last_id = int(rows[-1]["id"])

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(%s)" % table).fetchall()
        }
        if column not in columns:
            connection.execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration)
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def upsert_repository(self, candidate: RepositoryCandidate) -> int:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO repositories(
                    name, local_path, remote_name, remote_url, canonical_remote,
                    enabled, created_at, last_seen_at, last_error
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(local_path) DO UPDATE SET
                    name=CASE
                        WHEN repositories.canonical_remote IS NULL THEN excluded.name
                        ELSE repositories.name
                    END,
                    remote_name=CASE
                        WHEN repositories.canonical_remote IS NULL THEN excluded.remote_name
                        ELSE repositories.remote_name
                    END,
                    remote_url=CASE
                        WHEN repositories.canonical_remote IS NULL THEN excluded.remote_url
                        ELSE repositories.remote_url
                    END,
                    canonical_remote=COALESCE(
                        repositories.canonical_remote, excluded.canonical_remote
                    ),
                    enabled=1,
                    last_seen_at=excluded.last_seen_at,
                    last_error=excluded.last_error
                """,
                (
                    candidate.name,
                    candidate.path,
                    candidate.remote_name,
                    candidate.remote_url,
                    candidate.canonical_remote,
                    now,
                    now,
                    candidate.error,
                ),
            )
            row = connection.execute(
                "SELECT id FROM repositories WHERE local_path = ?", (candidate.path,)
            ).fetchone()
            return int(row["id"])

    def add_branch(
        self,
        repository_id: int,
        branch: str,
        remote_name: str | None,
        *,
        remote_fingerprint: str | None = None,
        baseline_oid: str | None = None,
        present: bool | None = None,
        rebaseline: bool = False,
    ) -> int:
        now = utc_now()
        initial_present = bool(baseline_oid) if present is None else present
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expected_remote = self._resolve_remote_fingerprint(
                connection,
                repository_id,
                remote_name,
                remote_fingerprint,
            )
            existing = connection.execute(
                """
                SELECT id, remote_name, remote_fingerprint FROM tracked_branches
                WHERE repository_id=? AND name=?
                """,
                (repository_id, branch),
            ).fetchone()
            if existing is not None:
                old_remote = existing["remote_name"]
                old_fingerprint = existing["remote_fingerprint"]
                if (
                    old_remote != remote_name
                    or old_fingerprint != expected_remote
                ) and not rebaseline:
                    raise ValueError(
                        "分支 %s 的 remote 绑定发生变化（%s → %s），拒绝静默改绑；"
                        "请用 watch add --rebaseline 明确重建基线"
                        % (
                            branch,
                            old_fingerprint or old_remote or "local",
                            expected_remote or remote_name or "local",
                        )
                    )
                if rebaseline:
                    connection.execute(
                        """
                        UPDATE tracked_branches
                        SET remote_name=?, remote_fingerprint=?, enabled=1, present=?,
                            last_observed_oid=?, last_present_oid=?,
                            observation_sequence=observation_sequence+1,
                            last_checked_at=?, last_success_at=?, last_error=NULL
                        WHERE id=?
                        """,
                        (
                            remote_name,
                            expected_remote,
                            1 if initial_present else 0,
                            baseline_oid,
                            baseline_oid,
                            now,
                            now if baseline_oid else None,
                            int(existing["id"]),
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE tracked_branches SET enabled=1 WHERE id=?",
                        (int(existing["id"]),),
                    )
                return int(existing["id"])
            connection.execute(
                """
                INSERT INTO tracked_branches(
                    repository_id, name, remote_name, remote_fingerprint,
                    enabled, present,
                    last_observed_oid, last_present_oid,
                    last_checked_at, last_success_at, created_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repository_id,
                    branch,
                    remote_name,
                    expected_remote,
                    1 if initial_present else 0,
                    baseline_oid,
                    baseline_oid,
                    now,
                    now if baseline_oid else None,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM tracked_branches WHERE repository_id=? AND name=?",
                (repository_id, branch),
            ).fetchone()
            return int(row["id"])

    @staticmethod
    def _resolve_remote_fingerprint(
        connection: sqlite3.Connection,
        repository_id: int,
        remote_name: str | None,
        remote_fingerprint: str | None,
    ) -> str | None:
        if remote_name is None:
            if remote_fingerprint is not None:
                raise ValueError("本地分支不能绑定 remote fingerprint")
            return None

        fingerprint = remote_fingerprint.strip() if remote_fingerprint else None
        if fingerprint is None:
            repository = connection.execute(
                """
                SELECT remote_name, canonical_remote
                FROM repositories WHERE id=?
                """,
                (repository_id,),
            ).fetchone()
            if (
                repository is not None
                and repository["remote_name"] == remote_name
                and repository["canonical_remote"]
            ):
                fingerprint = str(repository["canonical_remote"])
        if fingerprint is None:
            raise ValueError(
                "remote %s 缺少可验证的 fingerprint；请重新扫描后显式建立基线"
                % remote_name
            )
        return fingerprint

    def list_branch_bindings(self, local_path: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT b.name AS branch, b.remote_name, b.remote_fingerprint,
                       b.enabled
                FROM tracked_branches b
                JOIN repositories r ON r.id=b.repository_id
                WHERE r.local_path=?
                ORDER BY b.id
                """,
                (local_path,),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_watches(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return self._list_watches(connection)

    def disable_watch(self, branch_id: int) -> dict[str, Any] | None:
        """Disable one watch without deleting its cursor or event history.

        Bumping the observation sequence invalidates any stale observation that
        was read before the watch was disabled.  Service callers also hold the
        shared operation lock, but the compare-and-swap guard keeps this safe
        for direct database users as well.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT
                    b.id AS branch_id, b.name AS branch,
                    b.repository_id, r.name AS repository
                FROM tracked_branches b
                JOIN repositories r ON r.id=b.repository_id
                WHERE b.id=? AND b.enabled=1 AND r.enabled=1
                """,
                (branch_id,),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                """
                UPDATE tracked_branches
                SET enabled=0, observation_sequence=observation_sequence+1
                WHERE id=? AND enabled=1
                """,
                (branch_id,),
            )
            connection.execute(
                """
                UPDATE repositories
                SET enabled=0
                WHERE id=?
                  AND NOT EXISTS (
                      SELECT 1 FROM tracked_branches
                      WHERE repository_id=? AND enabled=1
                  )
                """,
                (int(row["repository_id"]), int(row["repository_id"])),
            )
            return dict(row)

    @staticmethod
    def _list_watches(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT
                b.id AS branch_id, b.name AS branch, b.remote_name,
                b.remote_fingerprint,
                b.present, b.last_observed_oid, b.last_checked_at,
                b.last_present_oid, b.last_success_at, b.last_error,
                b.observation_sequence,
                r.id AS repository_id, r.name AS repository,
                r.display_name AS repository_alias,
                r.tags_json AS repository_tags_json,
                r.local_path, r.remote_url, r.canonical_remote
            FROM tracked_branches b
            JOIN repositories r ON r.id=b.repository_id
            WHERE b.enabled=1 AND r.enabled=1
            ORDER BY r.name COLLATE NOCASE, b.name COLLATE NOCASE
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def update_repository_profile(
        self,
        repository_id: int,
        *,
        display_name: str | None,
        tags: Sequence[str],
    ) -> dict[str, Any] | None:
        tags_json = json.dumps(list(tags), ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE repositories
                SET display_name=?, tags_json=?
                WHERE id=? AND enabled=1
                """,
                (display_name, tags_json, repository_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                """
                SELECT id, name, display_name, tags_json
                FROM repositories
                WHERE id=? AND enabled=1
                """,
                (repository_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_repositories(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM repositories WHERE enabled=1 ORDER BY name COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def begin_check_run(
        self,
        *,
        scope_kind: str,
        repository_id: int | None,
        requested_branch_ids: Sequence[int],
        watches: Sequence[dict[str, Any]],
        fetch: bool,
        use_codex: bool,
        publish: bool,
    ) -> str:
        if scope_kind not in {"all", "repository", "branches"}:
            raise ValueError("检查范围无效")
        run_id = uuid.uuid4().hex
        started_at = utc_now()
        actual_branch_ids = [int(watch["branch_id"]) for watch in watches]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # This method is called while holding operation.lock. Any older
            # running row therefore belongs to a process that exited before it
            # could finalize the durable run.
            interrupted = connection.execute(
                "SELECT id FROM check_runs WHERE status='running'"
            ).fetchall()
            if interrupted:
                connection.execute(
                    """
                    UPDATE check_run_branches
                    SET status='interrupted'
                    WHERE status='pending'
                      AND check_run_id IN (
                        SELECT id FROM check_runs WHERE status='running'
                      )
                    """
                )
                connection.execute(
                    """
                    UPDATE check_runs
                    SET status='interrupted', finished_at=?,
                        last_error='上一次检查进程在写入完成状态前结束',
                        error_count=MAX(1, (
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status='error'
                        )),
                        checked_count=(
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status NOT IN ('pending', 'interrupted')
                        ),
                        changed_count=(
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status='changed'
                        ),
                        unchanged_count=(
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status='unchanged'
                        ),
                        baseline_count=(
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status='baseline'
                        ),
                        blocked_count=(
                            SELECT COUNT(*) FROM check_run_branches crb
                            WHERE crb.check_run_id=check_runs.id
                              AND crb.status IN ('blocked', 'interrupted')
                        ),
                        event_count=(
                            SELECT COUNT(*) FROM check_run_events cre
                            JOIN branch_events e ON e.id=cre.event_id
                            WHERE cre.check_run_id=check_runs.id AND e.kind!='baseline'
                        )
                    WHERE status='running'
                    """,
                    (started_at,),
                )
            connection.execute(
                """
                INSERT INTO check_runs(
                    id, started_at, status, scope_kind, repository_id,
                    requested_branch_ids_json, actual_branch_ids_json,
                    fetch, use_codex, publish
                ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    started_at,
                    scope_kind,
                    repository_id,
                    json.dumps(list(requested_branch_ids), separators=(",", ":")),
                    json.dumps(actual_branch_ids, separators=(",", ":")),
                    1 if fetch else 0,
                    1 if use_codex else 0,
                    1 if publish else 0,
                ),
            )
            connection.executemany(
                """
                INSERT INTO check_run_branches(
                    check_run_id, branch_id, repository_id,
                    repository_name, branch_name
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        int(watch["branch_id"]),
                        int(watch["repository_id"]),
                        str(watch["repository"])[:200],
                        str(watch["branch"])[:500],
                    )
                    for watch in watches
                ],
            )
        return run_id

    def finish_check_run(
        self,
        run_id: str,
        *,
        results: Sequence[dict[str, Any]],
        publications: Sequence[Mapping[str, Any]],
        summary: dict[str, Any],
        summary_warning: str | None = None,
    ) -> dict[str, Any]:
        finished_at = utc_now()
        checked_count = len(results)
        changed_count = sum(item.get("status") == "changed" for item in results)
        unchanged_count = sum(item.get("status") == "unchanged" for item in results)
        baseline_count = sum(item.get("status") == "baseline" for item in results)
        blocked_count = sum(bool(item.get("blocked")) for item in results)
        error_count = sum(
            item.get("status") == "error" and not bool(item.get("blocked"))
            for item in results
        )
        visible_event_ids = {
            int(item["event_id"])
            for item in results
            if item.get("status") == "changed" and item.get("event_id") is not None
        }
        provided_publications = [dict(item) for item in publications]
        current_publications = [
            dict(item)
            for item in provided_publications
            if item.get("event_id") in visible_event_ids
        ]
        backlog_publications = [
            dict(item)
            for item in provided_publications
            if item.get("event_id") not in visible_event_ids
        ]
        publish_requested = self._run_publish(run_id)
        publication_requested = len(visible_event_ids) if publish_requested else 0
        represented_event_ids = {
            int(item["event_id"])
            for item in current_publications
            if item.get("event_id") in visible_event_ids
        }
        missing_publication_ids = (
            visible_event_ids - represented_event_ids if publish_requested else set()
        )
        current_publications.extend(
            {
                "event_id": event_id,
                "status": "unknown",
                "error": "发布任务没有返回可核验的结果",
            }
            for event_id in sorted(missing_publication_ids)
        )
        for item in current_publications:
            item["scope"] = "current"
        for item in backlog_publications:
            item["scope"] = "backlog"
        publication_succeeded = sum(
            item.get("status") in {"published", "already_present"}
            for item in current_publications
        )
        publication_failed = sum(
            item.get("status") == "error" for item in current_publications
        )
        publication_unknown = sum(
            item.get("status") == "unknown" for item in current_publications
        )
        backlog_succeeded = sum(
            item.get("status") in {"published", "already_present"}
            for item in backlog_publications
        )
        backlog_failed = sum(
            item.get("status") == "error" for item in backlog_publications
        )
        backlog_unknown = sum(
            item.get("status") == "unknown" for item in backlog_publications
        )
        status = (
            "reconcile_required"
            if publication_unknown or backlog_unknown
            else (
                "completed_with_errors"
                if blocked_count or error_count or publication_failed or backlog_failed
                else "succeeded"
            )
        )
        safe_publications = self._safe_publications(
            [*current_publications, *backlog_publications]
        )
        publications_truncated = (
            len(current_publications) + len(backlog_publications)
            > MAX_CHECK_RUN_PUBLICATIONS
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in results:
                branch_id = item.get("branch_id")
                if isinstance(branch_id, bool) or not isinstance(branch_id, int):
                    continue
                result_status = (
                    "blocked" if item.get("blocked") else str(item.get("status") or "error")
                )
                error = item.get("error")
                safe_error = (
                    redact_sensitive_text(str(error))[:2000] if error else None
                )
                connection.execute(
                    """
                    UPDATE check_run_branches
                    SET status=?, error=?
                    WHERE check_run_id=? AND branch_id=?
                    """,
                    (result_status, safe_error, run_id, branch_id),
                )
            updated = connection.execute(
                """
                UPDATE check_runs
                SET finished_at=?, status=?, checked_count=?, changed_count=?,
                    unchanged_count=?, baseline_count=?, blocked_count=?,
                    error_count=?, event_count=?, publications_json=?,
                    publications_truncated=?,
                    publication_requested_count=?,
                    publication_succeeded_count=?,
                    publication_failed_count=?, publication_unknown_count=?,
                    publication_backlog_succeeded_count=?,
                    publication_backlog_failed_count=?,
                    publication_backlog_unknown_count=?,
                    summary_json=?, summary_source=?, summary_warning=?, last_error=NULL
                WHERE id=? AND status='running'
                """,
                (
                    finished_at,
                    status,
                    checked_count,
                    changed_count,
                    unchanged_count,
                    baseline_count,
                    blocked_count,
                    error_count,
                    len(visible_event_ids),
                    json.dumps(safe_publications, ensure_ascii=False, separators=(",", ":")),
                    1 if publications_truncated else 0,
                    publication_requested,
                    publication_succeeded,
                    publication_failed,
                    publication_unknown,
                    backlog_succeeded,
                    backlog_failed,
                    backlog_unknown,
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    str(summary.get("source") or "rules"),
                    redact_sensitive_text(summary_warning)[:1000]
                    if summary_warning else None,
                    run_id,
                ),
            )
            if updated.rowcount != 1:
                raise KeyError("unknown or finalized check run %s" % run_id)
            return self._get_check_run(connection, run_id) or {}

    def fail_check_run(self, run_id: str, message: str) -> None:
        finished_at = utc_now()
        safe_error = redact_sensitive_text(message or "检查失败")[:2000]
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE check_run_branches
                SET status='interrupted', error=COALESCE(error, ?)
                WHERE check_run_id=? AND status='pending'
                """,
                (safe_error, run_id),
            )
            connection.execute(
                """
                UPDATE check_runs
                SET status='failed', finished_at=?, last_error=?,
                    error_count=MAX(1, (
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=? AND status='error'
                    )),
                    checked_count=(
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=?
                          AND status NOT IN ('pending', 'interrupted')
                    ),
                    changed_count=(
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=? AND status='changed'
                    ),
                    unchanged_count=(
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=? AND status='unchanged'
                    ),
                    baseline_count=(
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=? AND status='baseline'
                    ),
                    blocked_count=(
                        SELECT COUNT(*) FROM check_run_branches
                        WHERE check_run_id=?
                          AND status IN ('blocked', 'interrupted')
                    ),
                    event_count=(
                        SELECT COUNT(*) FROM check_run_events cre
                        JOIN branch_events e ON e.id=cre.event_id
                        WHERE cre.check_run_id=? AND e.kind!='baseline'
                    )
                WHERE id=? AND status='running'
                """,
                (
                    finished_at,
                    safe_error,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                    run_id,
                ),
            )

    def save_check_run_summary(
        self,
        run_id: str,
        summary: dict[str, Any],
        *,
        warning: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE check_runs
                SET summary_json=?, summary_source=?, summary_warning=?
                WHERE id=? AND finished_at IS NOT NULL
                """,
                (
                    json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
                    str(summary.get("source") or "rules"),
                    redact_sensitive_text(warning)[:1000] if warning else None,
                    run_id,
                ),
            )

    def get_check_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            return self._get_check_run(connection, run_id)

    def acknowledge_check_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE check_runs
                SET acknowledged_at=COALESCE(acknowledged_at, ?)
                WHERE id=? AND finished_at IS NOT NULL
                """,
                (utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                return None
            return self._get_check_run(connection, run_id)

    def _run_publish(self, run_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT publish FROM check_runs WHERE id=?", (run_id,)
            ).fetchone()
            return bool(row and row["publish"])

    @staticmethod
    def _safe_publications(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        safe: list[dict[str, Any]] = []
        for value in values[:MAX_CHECK_RUN_PUBLICATIONS]:
            item: dict[str, Any] = {
                "event_id": value.get("event_id"),
                "status": str(value.get("status") or "unknown")[:40],
                "scope": "current" if value.get("scope") == "current" else "backlog",
            }
            if value.get("error"):
                item["error"] = redact_sensitive_text(str(value["error"]))[:1000]
            safe.append(item)
        return safe

    @staticmethod
    def _actionable_check_run_clause(alias: str = "check_runs") -> str:
        counts = " OR ".join(
            "%s.%s>0" % (alias, column)
            for column in (
                "event_count",
                "blocked_count",
                "error_count",
                "publication_failed_count",
                "publication_unknown_count",
                "publication_backlog_failed_count",
                "publication_backlog_unknown_count",
            )
        )
        return "(%s OR %s.status IN ('failed', 'interrupted'))" % (counts, alias)

    def _get_check_run(
        self, connection: sqlite3.Connection, run_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM check_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        event_rows = connection.execute(
            """
            SELECT e.id, e.kind, e.summary_json, e.list_metadata_json,
                   b.name AS branch, r.name AS repository
            FROM check_run_events cre
            JOIN branch_events e ON e.id=cre.event_id
            JOIN tracked_branches b ON b.id=e.tracked_branch_id
            JOIN repositories r ON r.id=b.repository_id
            WHERE cre.check_run_id=? AND e.kind!='baseline'
            ORDER BY e.id
            LIMIT ?
            """,
            (run_id, MAX_CHECK_RUN_EVENT_REFS + 1),
        ).fetchall()
        blocked_rows = connection.execute(
            """
            SELECT branch_id, repository_id, repository_name, branch_name,
                   status, error
            FROM check_run_branches
            WHERE check_run_id=? AND status IN ('blocked', 'error', 'interrupted')
            ORDER BY repository_name COLLATE NOCASE, branch_name COLLATE NOCASE
            LIMIT ?
            """,
            (run_id, MAX_CHECK_RUN_BLOCKED_REFS + 1),
        ).fetchall()
        value["events"] = [
            self._check_run_event_ref(dict(item))
            for item in event_rows[:MAX_CHECK_RUN_EVENT_REFS]
        ]
        value["event_ids"] = [item["id"] for item in value["events"]]
        value["events_truncated"] = len(event_rows) > MAX_CHECK_RUN_EVENT_REFS
        value["blocked"] = [dict(item) for item in blocked_rows[:MAX_CHECK_RUN_BLOCKED_REFS]]
        value["blocked_truncated"] = len(blocked_rows) > MAX_CHECK_RUN_BLOCKED_REFS
        for key in ("requested_branch_ids_json", "actual_branch_ids_json"):
            try:
                decoded = json.loads(value.pop(key))
            except (TypeError, json.JSONDecodeError):
                decoded = []
            value[key.removesuffix("_json")] = decoded if isinstance(decoded, list) else []
        for key in ("publications_json", "summary_json"):
            raw = value.pop(key)
            try:
                decoded = json.loads(raw) if raw else ([] if key == "publications_json" else None)
            except (TypeError, json.JSONDecodeError):
                decoded = [] if key == "publications_json" else None
            value[key.removesuffix("_json")] = decoded
        return value

    @staticmethod
    def _check_run_event_ref(value: dict[str, Any]) -> dict[str, Any]:
        try:
            summary = json.loads(value.pop("summary_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            summary = {}
        try:
            metadata = json.loads(value.pop("list_metadata_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return {
            "id": value.get("id"),
            "repository": value.get("repository"),
            "branch": value.get("branch"),
            "kind": value.get("kind"),
            "title": summary.get("title") if isinstance(summary, dict) else None,
            "categories": list(metadata.get("categories") or [])[:12]
            if isinstance(metadata, dict) else [],
        }

    def overview_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            return self._overview_counts(connection)

    def daemon_health(self) -> dict[str, Any]:
        with self.connect() as connection:
            return self._daemon_health(connection)

    @staticmethod
    def _daemon_health(connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT last_attempt_at, last_success_at, last_failure_at,
                   consecutive_failures, last_error, last_prune_at
            FROM runtime_health WHERE singleton=1
            """
        ).fetchone()
        if row is None:
            return {
                "last_attempt_at": None,
                "last_success_at": None,
                "last_failure_at": None,
                "consecutive_failures": 0,
                "last_error": None,
                "last_prune_at": None,
            }
        return dict(row)

    def record_daemon_attempt(self) -> str:
        checked_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                "UPDATE runtime_health SET last_attempt_at=? WHERE singleton=1",
                (checked_at,),
            )
        return checked_at

    def record_daemon_success(self) -> str:
        checked_at = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runtime_health
                SET last_attempt_at=?, last_success_at=?,
                    consecutive_failures=0, last_error=NULL
                WHERE singleton=1
                """,
                (checked_at, checked_at),
            )
        return checked_at

    def record_daemon_failure(self, message: str) -> str:
        checked_at = utc_now()
        error = redact_sensitive_text(message or "检查失败")[:2000]
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE runtime_health
                SET last_attempt_at=?, last_failure_at=?,
                    consecutive_failures=consecutive_failures+1,
                    last_error=?
                WHERE singleton=1
                """,
                (checked_at, checked_at, error),
            )
        return checked_at

    @contextmanager
    def daemon_heartbeat(self) -> Iterator[None]:
        """Record one daemon cycle without masking its original failure."""
        self.record_daemon_attempt()
        try:
            yield
        except Exception as exc:
            try:
                self.record_daemon_failure(str(exc))
            except Exception:
                # The original check failure is more useful than a secondary
                # heartbeat write error, especially when SQLite itself failed.
                pass
            raise
        else:
            try:
                self.record_daemon_success()
            except Exception as exc:
                try:
                    self.record_daemon_failure(
                        "写入后台成功心跳失败：%s" % str(exc)
                    )
                except Exception:
                    pass
                raise

    def prune(
        self,
        before: str,
        *,
        dry_run: bool = False,
        vacuum: bool = False,
    ) -> dict[str, Any]:
        """Remove old terminal history while preserving unresolved delivery work."""
        if not isinstance(before, str) or not before.strip():
            raise ValueError("清理截止时间不能为空")
        before = parse_retention_cutoff(before)
        if dry_run and vacuum:
            raise ValueError("预览清理时不能执行 VACUUM")

        active_states = ("pending", "retry", "unknown", "inflight")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            actionable = self._actionable_check_run_clause("cr")
            run_protection = """
                EXISTS (
                    SELECT 1 FROM check_run_events cre
                    JOIN check_runs cr ON cr.id=cre.check_run_id
                    WHERE cre.event_id=e.id
                      AND cr.acknowledged_at IS NULL
                      AND (%s OR cr.status='running')
                )
            """ % actionable
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS old_events,
                    SUM(CASE WHEN (EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.event_id=e.id
                          AND d.state IN (?, ?, ?, ?)
                    ) OR %s) THEN 1 ELSE 0 END) AS protected_events,
                    SUM(CASE WHEN NOT (EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.event_id=e.id
                          AND d.state IN (?, ?, ?, ?)
                    ) OR %s) THEN (
                        SELECT COUNT(*) FROM deliveries d2 WHERE d2.event_id=e.id
                    ) ELSE 0 END) AS terminal_deliveries
                FROM branch_events e
                WHERE e.detected_at < ?
                """ % (run_protection, run_protection),
                (*active_states, *active_states, before),
            ).fetchone()
            old_events = int(counts["old_events"] or 0)
            protected_events = int(counts["protected_events"] or 0)
            event_count = old_events - protected_events
            delivery_count = int(counts["terminal_deliveries"] or 0)
            external_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM external_requests
                    WHERE updated_at < ? AND state IN ('succeeded', 'failed')
                    """,
                    (before,),
                ).fetchone()[0]
            )
            check_run_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM check_runs cr
                    WHERE cr.finished_at < ?
                      AND (cr.acknowledged_at IS NOT NULL OR NOT %s)
                    """ % actionable,
                    (before,),
                ).fetchone()[0]
            )

            if not dry_run:
                connection.execute(
                    """
                    DELETE FROM branch_events
                    WHERE detected_at < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM deliveries d
                          WHERE d.event_id=branch_events.id
                            AND d.state IN ('pending', 'retry', 'unknown', 'inflight')
                      )
                      AND NOT %s
                    """ % run_protection.replace("e.id", "branch_events.id"),
                    (before,),
                )
                connection.execute(
                    """
                    DELETE FROM check_runs
                    WHERE id IN (
                        SELECT cr.id FROM check_runs cr
                        WHERE cr.finished_at < ?
                          AND (cr.acknowledged_at IS NOT NULL OR NOT %s)
                    )
                    """ % actionable,
                    (before,),
                )
                connection.execute(
                    """
                    DELETE FROM external_requests
                    WHERE updated_at < ? AND state IN ('succeeded', 'failed')
                    """,
                    (before,),
                )
                connection.execute(
                    "UPDATE runtime_health SET last_prune_at=? WHERE singleton=1",
                    (utc_now(),),
                )

        if vacuum and not dry_run:
            with self.connect() as connection:
                connection.execute("VACUUM")
        return {
            "before": before,
            "dry_run": dry_run,
            "events": event_count,
            "deliveries": delivery_count,
            "external_requests": external_count,
            "check_runs": check_run_count,
            "protected_events": protected_events,
            "vacuumed": bool(vacuum and not dry_run),
        }

    @staticmethod
    def _overview_counts(connection: sqlite3.Connection) -> dict[str, int]:
        row = connection.execute(
            """
            WITH branch_totals AS (
                SELECT
                    COUNT(*) AS branches,
                    SUM(CASE
                        WHEN b.last_error IS NOT NULL
                          OR b.present=0
                          OR (
                              b.last_observed_oid IS NULL
                              AND b.last_success_at IS NULL
                          )
                        THEN 1 ELSE 0
                    END)
                        AS branch_errors
                FROM tracked_branches b
                JOIN repositories r ON r.id=b.repository_id
                WHERE b.enabled=1 AND r.enabled=1
            )
            SELECT
                (SELECT COUNT(*) FROM repositories WHERE enabled=1)
                    AS repositories,
                branch_totals.branches AS branches,
                COALESCE(branch_totals.branch_errors, 0) AS branch_errors,
                (SELECT COUNT(*)
                   FROM branch_events e
                   JOIN tracked_branches eb ON eb.id=e.tracked_branch_id
                   JOIN repositories er ON er.id=eb.repository_id
                  WHERE e.kind!='baseline' AND eb.enabled=1 AND er.enabled=1)
                    AS events,
                (SELECT COUNT(*) FROM deliveries
                  WHERE state IN ('pending', 'retry', 'unknown', 'inflight'))
                    AS pending_deliveries
            FROM branch_totals
            """
        ).fetchone()
        return {
            "repositories": int(row["repositories"]),
            "branches": int(row["branches"]),
            "branch_errors": int(row["branch_errors"]),
            "events": int(row["events"]),
            "pending_deliveries": int(row["pending_deliveries"]),
        }

    def set_check_error(
        self,
        branch_id: int,
        message: str,
        *,
        expected_sequence: int,
        expected_oid: str | None,
        check_run_id: str | None = None,
        check_run_status: str = "error",
    ) -> None:
        safe_message = redact_sensitive_text(message)[:2000]
        with self.connect() as connection:
            if check_run_id is not None:
                connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tracked_branches SET last_checked_at=?, last_error=?
                WHERE id=? AND observation_sequence=? AND last_observed_oid IS ?
                """,
                (
                    utc_now(),
                    safe_message,
                    branch_id,
                    expected_sequence,
                    expected_oid,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdate("分支状态已被另一个任务更新；旧错误未写入")
            if check_run_id is not None:
                status = "blocked" if check_run_status == "blocked" else "error"
                updated = connection.execute(
                    """
                    UPDATE check_run_branches
                    SET status=?, error=?
                    WHERE check_run_id=? AND branch_id=?
                    """,
                    (status, safe_message, check_run_id, branch_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("检查批次缺少对应的分支快照")

    def touch_unchanged(
        self,
        branch_id: int,
        *,
        present: bool = True,
        expected_sequence: int,
        expected_oid: str | None,
        check_run_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            if check_run_id is not None:
                connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tracked_branches
                SET present=?, last_checked_at=?, last_success_at=?, last_error=NULL
                WHERE id=? AND observation_sequence=? AND last_observed_oid IS ?
                """,
                (
                    1 if present else 0,
                    now,
                    now,
                    branch_id,
                    expected_sequence,
                    expected_oid,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdate("分支状态已被另一个任务更新；旧检查结果已丢弃")
            if check_run_id is not None:
                updated = connection.execute(
                    """
                    UPDATE check_run_branches
                    SET status='unchanged', error=NULL
                    WHERE check_run_id=? AND branch_id=?
                    """,
                    (check_run_id, branch_id),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("检查批次缺少对应的分支快照")

    def create_event_and_advance(
        self,
        branch_id: int,
        facts: ChangeFacts,
        initial_summary: Summary,
        *,
        expected_sequence: int,
        check_run_id: str | None = None,
    ) -> tuple[int, str]:
        event_key = "BR-%s" % uuid.uuid4().hex
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT observation_sequence, last_observed_oid FROM tracked_branches WHERE id=?",
                (branch_id,),
            ).fetchone()
            if row is None:
                raise KeyError("unknown tracked branch %s" % branch_id)
            if row["last_observed_oid"] != facts.old_sha:
                raise ConcurrentUpdate(
                    "分支游标已被另一个任务推进；本次结果未重复写入，请重试检查"
                )
            if int(row["observation_sequence"]) != expected_sequence:
                raise ConcurrentUpdate(
                    "分支观察序号已被另一个任务推进；本次结果未重复写入，请重试检查"
                )
            sequence = int(row["observation_sequence"]) + 1
            stored_facts = facts.to_dict()
            if stored_facts.get("patch_excerpt"):
                stored_facts["patch_excerpt"] = "[patch not persisted by Branch Radar]"
            cursor = connection.execute(
                """
                INSERT INTO branch_events(
                    event_key, tracked_branch_id, sequence, kind,
                    from_oid, to_oid, merge_base_oid, detected_at, facts_json,
                    authors_json, summary_json, summary_source, list_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    branch_id,
                    sequence,
                    facts.event_kind,
                    facts.old_sha,
                    facts.new_sha,
                    facts.merge_base,
                    facts.detected_at,
                    json.dumps(stored_facts, ensure_ascii=False),
                    json.dumps(
                        self._authors_from_facts(stored_facts),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    json.dumps(initial_summary.to_dict(), ensure_ascii=False),
                    initial_summary.source,
                    json.dumps(
                        self._event_list_metadata(
                            stored_facts,
                            to_oid=facts.new_sha,
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise RuntimeError("SQLite 未返回新事件 ID")
            if check_run_id is not None:
                connection.execute(
                    """
                    INSERT INTO check_run_events(check_run_id, event_id)
                    VALUES (?, ?)
                    """,
                    (check_run_id, event_id),
                )
                updated = connection.execute(
                    """
                    UPDATE check_run_branches
                    SET event_id=?, status=?, error=NULL
                    WHERE check_run_id=? AND branch_id=?
                    """,
                    (
                        event_id,
                        "baseline" if facts.event_kind == "baseline" else "changed",
                        check_run_id,
                        branch_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError("检查批次缺少对应的分支快照")
            now = utc_now()
            connection.execute(
                """
                UPDATE tracked_branches
                SET last_observed_oid=?, observation_sequence=?, present=?,
                    last_present_oid=CASE WHEN ?='deleted' THEN last_present_oid ELSE ? END,
                    last_checked_at=?, last_success_at=?, last_error=NULL
                WHERE id=?
                """,
                (
                    facts.new_sha,
                    sequence,
                    0 if facts.event_kind == "deleted" else 1,
                    facts.event_kind,
                    facts.new_sha,
                    now,
                    now,
                    branch_id,
                ),
            )
            return event_id, event_key

    def save_summary(self, event_id: int, summary: Summary) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE branch_events
                SET summary_json=?, summary_source=?
                WHERE id=?
                """,
                (json.dumps(summary.to_dict(), ensure_ascii=False), summary.source, event_id),
            )

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
        with self.connect() as connection:
            return self._list_events(
                connection,
                limit=limit,
                repository_id=repository_id,
                branch_id=branch_id,
                before_id=before_id,
                since=since,
                include_baseline=include_baseline,
                compact=compact,
            )

    def dashboard_snapshot(
        self, *, event_limit: int, since: str | None = None
    ) -> dict[str, Any]:
        """Read the dashboard from one consistent SQLite snapshot."""
        with self.connect() as connection:
            connection.execute("BEGIN")
            latest = connection.execute(
                """
                SELECT id FROM check_runs
                WHERE finished_at IS NOT NULL
                ORDER BY finished_at DESC, started_at DESC, rowid DESC LIMIT 1
                """
            ).fetchone()
            actionable = self._actionable_check_run_clause("cr")
            pending = connection.execute(
                """
                SELECT cr.id FROM check_runs cr
                WHERE cr.finished_at IS NOT NULL
                  AND cr.acknowledged_at IS NULL
                  AND %s
                ORDER BY cr.started_at, cr.rowid LIMIT 1
                """ % actionable
            ).fetchone()
            pending_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM check_runs cr
                    WHERE cr.finished_at IS NOT NULL
                      AND cr.acknowledged_at IS NULL
                      AND %s
                    """ % actionable
                ).fetchone()[0]
            )
            return {
                "counts": self._overview_counts(connection),
                "daemon_health": self._daemon_health(connection),
                "watches": self._list_watches(connection),
                "events": self._list_events(
                    connection,
                    limit=event_limit,
                    since=since,
                    include_baseline=False,
                    compact=True,
                    enabled_only=True,
                ),
                "latest_check_run": self._get_check_run(
                    connection, str(latest["id"])
                ) if latest else None,
                "pending_check_run": self._get_check_run(
                    connection, str(pending["id"])
                ) if pending else None,
                "pending_check_run_count": pending_count,
            }

    def _list_events(
        self,
        connection: sqlite3.Connection,
        *,
        limit: int,
        repository_id: int | None = None,
        branch_id: int | None = None,
        before_id: int | None = None,
        since: str | None = None,
        include_baseline: bool = True,
        compact: bool = False,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if enabled_only:
            clauses.extend(("b.enabled=1", "r.enabled=1"))
        if not include_baseline:
            clauses.append("e.kind!='baseline'")
        if repository_id is not None:
            clauses.append("r.id=?")
            parameters.append(repository_id)
        if branch_id is not None:
            clauses.append("b.id=?")
            parameters.append(branch_id)
        if before_id is not None:
            clauses.append("e.id<?")
            parameters.append(before_id)
        if since is not None:
            # Timestamps are stored as ISO-8601 UTC, so a lexical comparison is
            # a chronological one, and branch_events_detected_at_idx serves it.
            clauses.append("e.detected_at>=?")
            parameters.append(since)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.append(limit)
        if compact:
            event_columns = """
                e.id, e.event_key, e.kind, e.detected_at,
                e.from_oid, e.to_oid, e.merge_base_oid,
                e.summary_json, e.summary_source, e.authors_json,
                e.list_metadata_json
            """
            delivery_column = "NULL AS delivery_state"
        else:
            event_columns = "e.*"
            delivery_column = """
                (
                    SELECT d.state FROM deliveries d
                    WHERE d.event_id=e.id ORDER BY d.id DESC LIMIT 1
                ) AS delivery_state
            """
        rows = connection.execute(
            """
            SELECT
                %s, b.id AS branch_id, b.name AS branch,
                r.id AS repository_id, r.name AS repository,
                (
                    SELECT cre.check_run_id FROM check_run_events cre
                    WHERE cre.event_id=e.id LIMIT 1
                ) AS check_run_id,
                EXISTS(
                    SELECT 1
                    FROM check_run_events ncre
                    JOIN check_runs ncr ON ncr.id=ncre.check_run_id
                    WHERE ncre.event_id=e.id
                      AND ncr.acknowledged_at IS NULL
                      AND (
                        ncr.event_count>0 OR ncr.blocked_count>0 OR ncr.error_count>0
                        OR ncr.publication_failed_count>0
                        OR ncr.publication_unknown_count>0
                        OR ncr.publication_backlog_failed_count>0
                        OR ncr.publication_backlog_unknown_count>0
                      )
                ) AS is_new,
                %s
            FROM branch_events e
            JOIN tracked_branches b ON b.id=e.tracked_branch_id
            JOIN repositories r ON r.id=b.repository_id
            %s
            ORDER BY e.id DESC LIMIT ?
            """ % (event_columns, delivery_column, where),
            parameters,
        ).fetchall()
        decoder = self._decode_compact_event if compact else self._decode_event
        return [decoder(dict(row)) for row in rows]

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT e.*, b.id AS branch_id, b.name AS branch,
                       r.id AS repository_id, r.name AS repository,
                       r.local_path, r.remote_url,
                       (
                           SELECT d.state FROM deliveries d
                           WHERE d.event_id=e.id ORDER BY d.id DESC LIMIT 1
                       ) AS delivery_state
                FROM branch_events e
                JOIN tracked_branches b ON b.id=e.tracked_branch_id
                JOIN repositories r ON r.id=b.repository_id
                WHERE e.id=?
                """,
                (event_id,),
            ).fetchone()
            return self._decode_event(dict(row)) if row else None

    def get_events(self, event_ids: Sequence[int]) -> list[dict[str, Any]]:
        ordered_ids = list(dict.fromkeys(int(event_id) for event_id in event_ids))
        if not ordered_ids:
            return []
        if len(ordered_ids) > 5000:
            raise ValueError("单次最多读取 5000 个事件")
        values: dict[int, dict[str, Any]] = {}
        with self.connect() as connection:
            connection.execute("BEGIN")
            for offset in range(0, len(ordered_ids), 500):
                chunk = ordered_ids[offset:offset + 500]
                placeholders = ",".join("?" for _item in chunk)
                rows = connection.execute(
                    """
                    SELECT e.*, b.id AS branch_id, b.name AS branch,
                           r.id AS repository_id, r.name AS repository,
                           r.local_path, r.remote_url,
                           (
                               SELECT d.state FROM deliveries d
                               WHERE d.event_id=e.id ORDER BY d.id DESC LIMIT 1
                           ) AS delivery_state
                    FROM branch_events e
                    JOIN tracked_branches b ON b.id=e.tracked_branch_id
                    JOIN repositories r ON r.id=b.repository_id
                    WHERE e.id IN (%s)
                    """ % placeholders,
                    chunk,
                ).fetchall()
                for row in rows:
                    decoded = self._decode_event(dict(row))
                    values[int(decoded["id"])] = decoded
        return [values[event_id] for event_id in ordered_ids if event_id in values]

    @staticmethod
    def _decode_event(value: dict[str, Any]) -> dict[str, Any]:
        value["facts"] = json.loads(value.pop("facts_json"))
        value["remote_url"] = value["facts"].get("remote_url")
        value["authors"] = Database._decode_authors(value.pop("authors_json", None))
        raw_metadata = value.pop("list_metadata_json", None)
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        value["list_metadata"] = metadata if isinstance(metadata, dict) else {}
        raw_summary = value.pop("summary_json")
        value["summary"] = json.loads(raw_summary) if raw_summary else None
        return value

    @staticmethod
    def _decode_compact_event(value: dict[str, Any]) -> dict[str, Any]:
        value["authors"] = Database._decode_authors(value.pop("authors_json", None))
        value["is_new"] = bool(value.get("is_new"))
        raw_metadata = value.pop("list_metadata_json", None)
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        value["list_metadata"] = metadata if isinstance(metadata, dict) else {}
        raw_summary = value.pop("summary_json")
        value["summary"] = json.loads(raw_summary) if raw_summary else None
        return value

    @classmethod
    def _event_list_metadata(
        cls,
        facts: Any,
        *,
        to_oid: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(facts, dict):
            facts = {}
        raw_diff = facts.get("diff")
        diff: dict[str, Any] = raw_diff if isinstance(raw_diff, dict) else {}
        raw_categories = facts.get("categories")
        categories = raw_categories if isinstance(raw_categories, list) else []
        raw_signals = facts.get("signals")
        signals = raw_signals if isinstance(raw_signals, list) else []

        def count(value: Any) -> int:
            if isinstance(value, bool):
                return 0
            try:
                return max(0, min(int(value or 0), 2_147_483_647))
            except (TypeError, ValueError, OverflowError):
                return 0

        return {
            "categories": [
                str(item)[:40]
                for item in categories[:20]
                if isinstance(item, str) and item
            ],
            "signals": [
                str(item)[:300]
                for item in signals[:20]
                if isinstance(item, str) and item
            ],
            "analysis_complete": bool(facts.get("analysis_complete", True)),
            "timeline": cls._event_timeline_metadata(facts, to_oid=to_oid),
            "stats": {
                "commits": count(facts.get("commit_count")),
                "files": count(diff.get("files_changed")),
                "insertions": count(diff.get("insertions")),
                "deletions": count(diff.get("deletions")),
            },
        }

    @classmethod
    def _event_timeline_metadata(
        cls,
        facts: dict[str, Any],
        *,
        to_oid: Any = None,
    ) -> dict[str, Any]:
        commits = facts.get("commits")
        retained = commits if isinstance(commits, list) else []
        tip_oid = cls._normalized_git_oid(to_oid)
        if tip_oid is None:
            tip_oid = cls._normalized_git_oid(facts.get("new_sha"))

        valid_dates: list[tuple[datetime, str]] = []
        tip_committed_at: str | None = None
        author_samples: dict[str, dict[str, Any]] = {}
        for commit in retained:
            if not isinstance(commit, dict):
                continue
            author = cls._safe_timeline_author(commit.get("author_name"))
            parsed_date = cls._normalized_commit_date(commit.get("commit_date"))
            if parsed_date is not None:
                valid_dates.append(parsed_date)
                commit_oid = cls._normalized_git_oid(commit.get("sha"))
                if tip_oid is not None and commit_oid == tip_oid:
                    tip_committed_at = parsed_date[1]
            if author is None:
                continue
            sample = author_samples.get(author)
            if sample is None:
                if len(author_samples) >= 12:
                    continue
                sample = {
                    "name": author,
                    "first": None,
                    "last": None,
                    "sampled_commits": 0,
                }
                author_samples[author] = sample
            sample["sampled_commits"] += 1
            if parsed_date is None:
                continue
            if sample["first"] is None or parsed_date[0] < sample["first"][0]:
                sample["first"] = parsed_date
            if sample["last"] is None or parsed_date[0] > sample["last"][0]:
                sample["last"] = parsed_date

        first_committed_at = (
            min(valid_dates, key=lambda item: item[0])[1] if valid_dates else None
        )
        last_committed_at = (
            max(valid_dates, key=lambda item: item[0])[1] if valid_dates else None
        )
        authors = []
        for sample in author_samples.values():
            first = sample.pop("first")
            last = sample.pop("last")
            authors.append(
                {
                    "name": sample["name"],
                    "first_committed_at": first[1] if first is not None else None,
                    "last_committed_at": last[1] if last is not None else None,
                    "sampled_commits": sample["sampled_commits"],
                }
            )

        return {
            "basis": "tip_commit" if tip_committed_at is not None else "detected",
            "tip_committed_at": tip_committed_at,
            "first_committed_at": first_committed_at,
            "last_committed_at": last_committed_at,
            "range_complete": bool(facts.get("analysis_complete", True))
            and not bool(facts.get("commits_truncated"))
            and not bool(facts.get("commits_are_tip_sample")),
            "authors": authors,
        }

    @staticmethod
    def _normalized_git_oid(value: Any) -> str | None:
        if not isinstance(value, str) or len(value) not in {40, 64}:
            return None
        normalized = value.casefold()
        if not normalized.strip("0") or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            return None
        return normalized

    @staticmethod
    def _normalized_commit_date(value: Any) -> tuple[datetime, str] | None:
        if not isinstance(value, str) or not value or len(value) > 64:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            normalized = parsed.astimezone(timezone.utc)
        except (OverflowError, ValueError):
            return None
        return normalized, normalized.isoformat()

    @staticmethod
    def _safe_timeline_author(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = " ".join(redact_sensitive_text(value[:1000]).split())[:120]
        return normalized or None

    @staticmethod
    def _decode_authors(value: Any) -> list[str]:
        if not isinstance(value, str):
            return []
        try:
            authors = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(authors, list):
            return []
        return [str(author) for author in authors[:12] if author]

    @staticmethod
    def _authors_from_facts(facts: dict[str, Any]) -> list[str]:
        result: list[str] = []
        seen = set()
        for commit in facts.get("commits") or []:
            if not isinstance(commit, dict) or not commit.get("author_name"):
                continue
            author = str(commit["author_name"])
            if author in seen:
                continue
            result.append(author)
            seen.add(author)
            if len(result) == 12:
                break
        return result

    def ensure_delivery(self, event_id: int, destination_type: str, destination_key: str) -> int:
        idempotency_key = "%s:%s:%s" % (destination_type, destination_key, event_id)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO deliveries(
                    event_id, destination_type, destination_key,
                    idempotency_key, state, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                ON CONFLICT(event_id, destination_type, destination_key) DO NOTHING
                """,
                (event_id, destination_type, destination_key, idempotency_key, utc_now()),
            )
            row = connection.execute(
                """
                SELECT id FROM deliveries
                WHERE event_id=? AND destination_type=? AND destination_key=?
                """,
                (event_id, destination_type, destination_key),
            ).fetchone()
            return int(row["id"])

    def list_events_without_delivery(
        self,
        destination_type: str,
        destination_key: str,
        *,
        limit: int,
    ) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT e.id
                FROM branch_events e
                LEFT JOIN deliveries d
                  ON d.event_id=e.id
                 AND d.destination_type=?
                 AND d.destination_key=?
                WHERE e.kind!='baseline' AND d.id IS NULL
                ORDER BY e.id
                LIMIT ?
                """,
                (destination_type, destination_key, limit),
            ).fetchall()
            return [int(row["id"]) for row in rows]

    def list_pending_deliveries(
        self,
        destination_type: str,
        destination_key: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 5000
        ):
            raise ValueError("待发布数量必须在 1 到 5000 之间")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, e.event_key, e.kind, e.detected_at,
                       e.facts_json, e.summary_json, e.summary_source,
                       b.name AS branch, r.name AS repository, r.remote_url
                FROM deliveries d
                JOIN branch_events e ON e.id=d.event_id
                JOIN tracked_branches b ON b.id=e.tracked_branch_id
                JOIN repositories r ON r.id=b.repository_id
                WHERE d.destination_type=? AND d.destination_key=?
                  AND (
                    d.state IN ('pending', 'retry', 'unknown')
                    OR (d.state='inflight' AND d.leased_until <= ?)
                )
                ORDER BY e.id
                LIMIT ?
                """,
                (destination_type, destination_key, utc_now(), limit),
            ).fetchall()
            values: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["facts"] = json.loads(item.pop("facts_json"))
                item["remote_url"] = item["facts"].get("remote_url")
                summary_raw = item.pop("summary_json")
                item["summary"] = json.loads(summary_raw) if summary_raw else None
                values.append(item)
            return values

    def claim_delivery(self, delivery_id: int, *, lease_seconds: int = 300) -> str | None:
        token = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        leased_until = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE deliveries
                SET state='inflight', lease_token=?, leased_until=?
                WHERE id=? AND (
                    state IN ('pending', 'retry', 'unknown')
                    OR (state='inflight' AND leased_until <= ?)
                )
                """,
                (token, leased_until, delivery_id, now.isoformat(timespec="seconds")),
            )
            return token if cursor.rowcount == 1 else None

    def mark_delivery_success(
        self,
        delivery_id: int,
        external_ref: str | None = None,
        *,
        lease_token: str | None = None,
    ) -> None:
        with self.connect() as connection:
            parameters: list[Any] = [external_ref, utc_now(), delivery_id]
            condition = "id=?"
            if lease_token:
                condition += " AND lease_token=?"
                parameters.append(lease_token)
            connection.execute(
                """
                UPDATE deliveries
                SET state='succeeded', attempts=attempts+1,
                    external_ref=?, last_error=NULL, delivered_at=?,
                    lease_token=NULL, leased_until=NULL
                WHERE %s
                """ % condition,
                parameters,
            )

    def mark_delivery_error(
        self,
        delivery_id: int,
        message: str,
        *,
        unknown: bool = False,
        lease_token: str | None = None,
    ) -> None:
        with self.connect() as connection:
            parameters: list[Any] = [
                "unknown" if unknown else "retry",
                redact_sensitive_text(message)[:4000],
                delivery_id,
            ]
            condition = "id=?"
            if lease_token:
                condition += " AND lease_token=?"
                parameters.append(lease_token)
            connection.execute(
                """
                UPDATE deliveries
                SET state=?, attempts=attempts+1, last_error=?,
                    lease_token=NULL, leased_until=NULL
                WHERE %s
                """ % condition,
                parameters,
            )

    def reserve_external_request(
        self, request_key: str, operation: str, request_fingerprint: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM external_requests WHERE request_key=?",
                (request_key,),
            ).fetchone()
            if existing is not None:
                row = dict(existing)
                if (
                    row["operation"] != operation
                    or row["request_fingerprint"] != request_fingerprint
                ):
                    raise ValueError("同一幂等标识不能用于不同的外部写入请求")
                row["created"] = False
                row["result"] = (
                    json.loads(row["result_json"]) if row.get("result_json") else None
                )
                return row
            unresolved = connection.execute(
                """
                SELECT * FROM external_requests
                WHERE operation=? AND request_fingerprint=?
                  AND state IN ('pending', 'unknown')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (operation, request_fingerprint),
            ).fetchone()
            if unresolved is not None:
                row = dict(unresolved)
                row["created"] = False
                row["result"] = (
                    json.loads(row["result_json"]) if row.get("result_json") else None
                )
                return row
            connection.execute(
                """
                INSERT INTO external_requests(
                    request_key, operation, request_fingerprint, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_key, operation, request_fingerprint, now, now),
            )
            return {
                "request_key": request_key,
                "operation": operation,
                "request_fingerprint": request_fingerprint,
                "state": "pending",
                "result": None,
                "last_error": None,
                "created": True,
            }

    def finish_external_request(
        self,
        request_key: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if state not in {"succeeded", "failed", "unknown"}:
            raise ValueError("外部请求终态无效")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE external_requests
                SET state=?, result_json=?, last_error=?, updated_at=?
                WHERE request_key=? AND state='pending'
                """,
                (
                    state,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    redact_sensitive_text(error or "")[:2000] or None,
                    utc_now(),
                    request_key,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdate("外部请求状态已被另一个任务更新")
