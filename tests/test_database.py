import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from branch_radar.database import (
    SCHEMA_VERSION,
    ConcurrentUpdate,
    Database,
    UnsupportedSchemaVersion,
)
from branch_radar.models import ChangeFacts, CommitInfo, RepositoryCandidate
from branch_radar.rules import summarize_with_rules


def candidate(path):
    return RepositoryCandidate(
        name="app",
        path=str(path),
        remote_name="origin",
        remote_url="https://git.example.com/team/app.git",
        canonical_remote="git.example.com/team/app",
        current_branch="main",
        upstream_branch="main",
        default_branch="main",
    )


def facts(old_sha, new_sha):
    value = ChangeFacts(
        repository_name="app",
        repository_path="/tmp/app",
        remote_url="https://git.example.com/team/app.git",
        branch="main",
        event_kind="fast_forward",
        old_sha=old_sha,
        new_sha=new_sha,
        merge_base=old_sha,
        detected_at="2026-08-18T00:00:00+00:00",
    )
    value.patch_excerpt = "+access_token=should-not-persist"
    return value


class DatabaseStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "state.db")
        self.database.initialize()
        repository_id = self.database.upsert_repository(candidate(self.root / "checkout"))
        self.branch_id = self.database.add_branch(
            repository_id, "main", "origin", baseline_oid="a" * 40
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_cursor_compare_and_swap_rejects_duplicate_observation(self):
        change = facts("a" * 40, "b" * 40)
        event_id, _key = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )
        self.assertGreater(event_id, 0)
        stored = self.database.get_event(event_id)
        self.assertEqual(
            stored["facts"]["patch_excerpt"],
            "[patch not persisted by Branch Radar]",
        )
        self.assertNotIn("should-not-persist", str(stored))
        with self.assertRaises(ConcurrentUpdate):
            duplicate = facts("a" * 40, "b" * 40)
            self.database.create_event_and_advance(
                self.branch_id,
                duplicate,
                summarize_with_rules(duplicate),
                expected_sequence=0,
            )
        with self.assertRaises(ConcurrentUpdate):
            self.database.touch_unchanged(
                self.branch_id,
                expected_sequence=0,
                expected_oid="a" * 40,
            )
        with self.assertRaises(ConcurrentUpdate):
            self.database.set_check_error(
                self.branch_id,
                "stale failure",
                expected_sequence=0,
                expected_oid="a" * 40,
            )
        self.assertIsNone(self.database.list_watches()[0]["last_error"])

    def test_daemon_heartbeat_records_success_and_redacted_failures(self):
        initial = self.database.daemon_health()
        self.assertIsNone(initial["last_success_at"])
        self.assertEqual(initial["consecutive_failures"], 0)

        with self.database.daemon_heartbeat():
            pass
        healthy = self.database.daemon_health()
        self.assertIsNotNone(healthy["last_success_at"])
        self.assertEqual(healthy["consecutive_failures"], 0)

        secret = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            with self.database.daemon_heartbeat():
                raise RuntimeError("provider failed %s" % secret)
        failed = self.database.daemon_health()
        self.assertEqual(failed["consecutive_failures"], 1)
        self.assertNotIn(secret, failed["last_error"])
        self.assertIsNotNone(failed["last_failure_at"])

        with self.database.daemon_heartbeat():
            pass
        self.assertEqual(self.database.daemon_health()["consecutive_failures"], 0)

        with mock.patch.object(
            self.database,
            "record_daemon_success",
            side_effect=sqlite3.OperationalError("heartbeat busy"),
        ), mock.patch.object(self.database, "record_daemon_failure") as failed_write:
            with self.assertRaises(sqlite3.OperationalError):
                with self.database.daemon_heartbeat():
                    pass
        failed_write.assert_called_once()

    def test_dashboard_snapshot_includes_daemon_health(self):
        self.database.record_daemon_failure("remote unavailable")

        snapshot = self.database.dashboard_snapshot(event_limit=10)

        self.assertEqual(snapshot["daemon_health"], self.database.daemon_health())
        self.assertEqual(snapshot["daemon_health"]["consecutive_failures"], 1)

    def test_check_run_links_event_cursor_and_acknowledgement(self):
        watch = self.database.list_watches()[0]
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=False,
        )
        change = facts("a" * 40, "b" * 40)
        change.categories = ["api", "database"]
        change.signals = [
            "API 相关文件 2 个",
            "提交说明包含 breaking change 信号",
        ]
        change.commit_count = 3
        change.diff.files_changed = 4
        change.diff.insertions = 120
        change.diff.deletions = 20
        event_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
            check_run_id=run_id,
        )

        with self.database.connect() as connection:
            linked = connection.execute(
                "SELECT check_run_id FROM check_run_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            advanced = connection.execute(
                "SELECT last_observed_oid FROM tracked_branches WHERE id=?",
                (self.branch_id,),
            ).fetchone()
        self.assertEqual(linked["check_run_id"], run_id)
        self.assertEqual(advanced["last_observed_oid"], "b" * 40)

        self.database.finish_check_run(
            run_id,
            results=[{
                "branch_id": self.branch_id,
                "status": "changed",
                "event_id": event_id,
                "blocked": False,
            }],
            publications=[],
            summary={
                "title": "本轮发现 1 个变化",
                "overview": "完成",
                "themes": ["API"],
                "priorities": [],
                "connections": [],
                "completeness_note": "",
                "source": "rules",
            },
        )
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["pending_check_run"]["id"], run_id)
        compact = snapshot["events"][0]
        self.assertEqual(compact["check_run_id"], run_id)
        self.assertTrue(compact["is_new"])
        self.assertEqual(compact["list_metadata"]["categories"], ["api", "database"])
        self.assertEqual(compact["list_metadata"]["stats"]["insertions"], 120)

        acknowledged = self.database.acknowledge_check_run(run_id)
        self.assertIsNotNone(acknowledged["acknowledged_at"])
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertIsNone(snapshot["pending_check_run"])
        self.assertFalse(snapshot["events"][0]["is_new"])

    def test_starting_check_recovers_interrupted_run_with_visible_events(self):
        watch = self.database.list_watches()[0]
        first_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=False,
        )
        change = facts("a" * 40, "b" * 40)
        event_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
            check_run_id=first_id,
        )
        self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=self.database.list_watches(),
            fetch=False,
            use_codex=False,
            publish=False,
        )

        recovered = self.database.get_check_run(first_id)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["error_count"], 1)
        self.assertEqual(recovered["checked_count"], 1)
        self.assertEqual(recovered["changed_count"], 1)
        self.assertEqual(recovered["event_count"], 1)
        self.assertEqual(recovered["event_ids"], [event_id])
        self.assertEqual(recovered["blocked"], [])
        with self.database.connect() as connection:
            branch = connection.execute(
                """
                SELECT status, event_id FROM check_run_branches
                WHERE check_run_id=? AND branch_id=?
                """,
                (first_id, self.branch_id),
            ).fetchone()
        self.assertEqual(branch["status"], "changed")
        self.assertEqual(branch["event_id"], event_id)
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["pending_check_run"]["id"], first_id)

    def test_recovered_run_preserves_multiple_atomic_branch_errors(self):
        repository_id = self.database.list_watches()[0]["repository_id"]
        feature_id = self.database.add_branch(
            repository_id, "feature", "origin", baseline_oid="c" * 40
        )
        watches = self.database.list_watches()
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=watches,
            fetch=False,
            use_codex=False,
            publish=False,
        )
        for watch in watches:
            self.database.set_check_error(
                watch["branch_id"],
                "remote unavailable",
                expected_sequence=watch["observation_sequence"],
                expected_oid=watch["last_observed_oid"],
                check_run_id=run_id,
            )
        self.database.begin_check_run(
            scope_kind="branches",
            repository_id=None,
            requested_branch_ids=[feature_id],
            watches=[watch for watch in watches if watch["branch_id"] == feature_id],
            fetch=False,
            use_codex=False,
            publish=False,
        )

        recovered = self.database.get_check_run(run_id)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["checked_count"], 2)
        self.assertEqual(recovered["error_count"], 2)
        self.assertEqual(
            {item["status"] for item in recovered["blocked"]},
            {"error"},
        )

    def test_early_fatal_run_is_pending_with_zero_checked_branches(self):
        repository_id = self.database.list_watches()[0]["repository_id"]
        self.database.add_branch(
            repository_id, "feature", "origin", baseline_oid="c" * 40
        )
        watches = self.database.list_watches()
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=watches,
            fetch=True,
            use_codex=False,
            publish=False,
        )

        self.database.fail_check_run(run_id, "fetch process failed")

        run = self.database.get_check_run(run_id)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["checked_count"], 0)
        self.assertEqual(run["error_count"], 1)
        self.assertEqual(run["blocked_count"], 2)
        self.assertEqual(len(run["blocked"]), 2)
        self.assertEqual(
            {item["status"] for item in run["blocked"]},
            {"interrupted"},
        )
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["pending_check_run"]["id"], run_id)
        self.assertEqual(snapshot["pending_check_run_count"], 1)

    def test_non_actionable_run_is_latest_but_not_pending(self):
        watch = self.database.list_watches()[0]
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=False,
        )
        self.database.finish_check_run(
            run_id,
            results=[{
                "branch_id": self.branch_id,
                "status": "unchanged",
                "event_id": None,
                "blocked": False,
            }],
            publications=[],
            summary={"title": "完成", "source": "rules"},
        )
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["latest_check_run"]["id"], run_id)
        self.assertIsNone(snapshot["pending_check_run"])
        self.assertEqual(snapshot["pending_check_run_count"], 0)

    def test_pending_runs_advance_in_order_and_all_events_stay_new(self):
        watch = self.database.list_watches()[0]
        run_ids = []
        event_ids = []
        old = "a" * 40
        for sequence, letter in enumerate(("b", "c")):
            run_id = self.database.begin_check_run(
                scope_kind="all",
                repository_id=None,
                requested_branch_ids=[],
                watches=[watch],
                fetch=False,
                use_codex=False,
                publish=False,
            )
            change = facts(old, letter * 40)
            event_id, _ = self.database.create_event_and_advance(
                self.branch_id,
                change,
                summarize_with_rules(change),
                expected_sequence=sequence,
                check_run_id=run_id,
            )
            self.database.finish_check_run(
                run_id,
                results=[{
                    "branch_id": self.branch_id,
                    "status": "changed",
                    "event_id": event_id,
                    "blocked": False,
                }],
                publications=[],
                summary={"title": "变化", "source": "rules"},
            )
            run_ids.append(run_id)
            event_ids.append(event_id)
            old = letter * 40
            watch = self.database.list_watches()[0]

        same_time = "2026-08-21T00:00:00+00:00"
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE check_runs SET started_at=?, finished_at=?",
                (same_time, same_time),
            )

        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["pending_check_run_count"], 2)
        self.assertEqual(snapshot["pending_check_run"]["id"], run_ids[0])
        self.assertEqual(snapshot["latest_check_run"]["id"], run_ids[1])
        self.assertTrue(all(event["is_new"] for event in snapshot["events"]))
        self.database.acknowledge_check_run(run_ids[0])
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["pending_check_run"]["id"], run_ids[1])
        by_id = {event["id"]: event for event in snapshot["events"]}
        self.assertFalse(by_id[event_ids[0]]["is_new"])
        self.assertTrue(by_id[event_ids[1]]["is_new"])

    def test_baseline_run_is_not_actionable(self):
        repository_id = self.database.list_watches()[0]["repository_id"]
        branch_id = self.database.add_branch(
            repository_id, "new", "origin", baseline_oid=None
        )
        watch = next(
            item for item in self.database.list_watches()
            if item["branch_id"] == branch_id
        )
        run_id = self.database.begin_check_run(
            scope_kind="branches",
            repository_id=None,
            requested_branch_ids=[branch_id],
            watches=[watch],
            fetch=False,
            use_codex=True,
            publish=False,
        )
        baseline = facts(None, "d" * 40)
        baseline.event_kind = "baseline"
        event_id, _ = self.database.create_event_and_advance(
            branch_id,
            baseline,
            summarize_with_rules(baseline),
            expected_sequence=0,
            check_run_id=run_id,
        )
        self.database.finish_check_run(
            run_id,
            results=[{
                "branch_id": branch_id,
                "status": "baseline",
                "event_id": event_id,
                "blocked": False,
            }],
            publications=[],
            summary={"title": "建立基线", "source": "rules"},
        )
        run = self.database.get_check_run(run_id)
        self.assertEqual(run["event_count"], 0)
        self.assertEqual(run["event_ids"], [])
        self.assertIsNone(self.database.dashboard_snapshot(event_limit=10)["pending_check_run"])

    def test_publish_run_marks_missing_current_event_outcome_unknown(self):
        watch = self.database.list_watches()[0]
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=True,
        )
        change = facts("a" * 40, "b" * 40)
        event_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
            check_run_id=run_id,
        )

        run = self.database.finish_check_run(
            run_id,
            results=[{
                "branch_id": self.branch_id,
                "status": "changed",
                "event_id": event_id,
                "blocked": False,
            }],
            publications=[],
            summary={"title": "变化", "source": "rules"},
        )

        self.assertEqual(run["status"], "reconcile_required")
        self.assertEqual(run["publication_requested_count"], 1)
        self.assertEqual(run["publication_unknown_count"], 1)
        self.assertEqual(run["publication_succeeded_count"], 0)
        self.assertEqual(
            run["publications"],
            [{
                "event_id": event_id,
                "status": "unknown",
                "scope": "current",
                "error": "发布任务没有返回可核验的结果",
            }],
        )

    def test_quiet_publish_run_records_backlog_failures_separately(self):
        watch = self.database.list_watches()[0]
        cases = (
            ("error", "completed_with_errors", "publication_backlog_failed_count"),
            ("unknown", "reconcile_required", "publication_backlog_unknown_count"),
        )
        for offset, (outcome, expected_status, count_key) in enumerate(cases, start=1):
            with self.subTest(outcome=outcome):
                run_id = self.database.begin_check_run(
                    scope_kind="all",
                    repository_id=None,
                    requested_branch_ids=[],
                    watches=[watch],
                    fetch=False,
                    use_codex=False,
                    publish=True,
                )
                run = self.database.finish_check_run(
                    run_id,
                    results=[{
                        "branch_id": self.branch_id,
                        "status": "unchanged",
                        "event_id": None,
                        "blocked": False,
                    }],
                    publications=[{
                        "event_id": 10_000 + offset,
                        "status": outcome,
                        "error": "historical delivery failed",
                    }],
                    summary={"title": "完成", "source": "rules"},
                )
                self.assertEqual(run["status"], expected_status)
                self.assertEqual(run["publication_requested_count"], 0)
                self.assertEqual(run["publication_failed_count"], 0)
                self.assertEqual(run["publication_unknown_count"], 0)
                self.assertEqual(run[count_key], 1)
                self.assertEqual(run["publications"][0]["scope"], "backlog")

    def test_check_run_bounds_publication_details_but_keeps_full_counts(self):
        watch = self.database.list_watches()[0]
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=True,
        )
        run = self.database.finish_check_run(
            run_id,
            results=[{
                "branch_id": self.branch_id,
                "status": "unchanged",
                "event_id": None,
                "blocked": False,
            }],
            publications=[
                {"event_id": 20_000 + offset, "status": "published"}
                for offset in range(201)
            ],
            summary={"title": "完成", "source": "rules"},
        )

        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["publication_backlog_succeeded_count"], 201)
        self.assertEqual(len(run["publications"]), 200)
        self.assertEqual(run["publications_truncated"], 1)

    def test_prune_protects_event_linked_to_still_running_run(self):
        watch = self.database.list_watches()[0]
        run_id = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=False,
        )
        change = facts("a" * 40, "b" * 40)
        event_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
            check_run_id=run_id,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE branch_events SET detected_at=? WHERE id=?",
                ("2026-08-18T00:00:00+00:00", event_id),
            )

        result = self.database.prune("2026-08-20T00:00:00+00:00")

        self.assertEqual(result["events"], 0)
        self.assertEqual(result["protected_events"], 1)
        self.assertIsNotNone(self.database.get_event(event_id))
        self.assertEqual(self.database.get_check_run(run_id)["status"], "running")

    def test_prune_removes_old_acknowledged_and_quiet_runs_but_protects_pending(self):
        watch = self.database.list_watches()[0]
        runs = []
        events = []
        old = "a" * 40
        for sequence, letter in enumerate(("b", "c")):
            run_id = self.database.begin_check_run(
                scope_kind="all",
                repository_id=None,
                requested_branch_ids=[],
                watches=[watch],
                fetch=False,
                use_codex=False,
                publish=False,
            )
            change = facts(old, letter * 40)
            event_id, _ = self.database.create_event_and_advance(
                self.branch_id,
                change,
                summarize_with_rules(change),
                expected_sequence=sequence,
                check_run_id=run_id,
            )
            self.database.finish_check_run(
                run_id,
                results=[{
                    "branch_id": self.branch_id,
                    "status": "changed",
                    "event_id": event_id,
                    "blocked": False,
                }],
                publications=[],
                summary={"title": "变化", "source": "rules"},
            )
            runs.append(run_id)
            events.append(event_id)
            old = letter * 40
            watch = self.database.list_watches()[0]
        self.database.acknowledge_check_run(runs[1])
        quiet_run = self.database.begin_check_run(
            scope_kind="all",
            repository_id=None,
            requested_branch_ids=[],
            watches=[watch],
            fetch=False,
            use_codex=False,
            publish=False,
        )
        self.database.finish_check_run(
            quiet_run,
            results=[{
                "branch_id": self.branch_id,
                "status": "unchanged",
                "event_id": None,
                "blocked": False,
            }],
            publications=[],
            summary={"title": "完成", "source": "rules"},
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE check_runs SET started_at=?, finished_at=?",
                ("2026-08-18T00:00:00+00:00", "2026-08-18T00:01:00+00:00"),
            )

        result = self.database.prune("2026-08-20T00:00:00+00:00")

        self.assertEqual(result["events"], 1)
        self.assertEqual(result["protected_events"], 1)
        self.assertEqual(result["check_runs"], 2)
        self.assertIsNotNone(self.database.get_event(events[0]))
        self.assertIsNone(self.database.get_event(events[1]))
        self.assertIsNotNone(self.database.get_check_run(runs[0]))
        self.assertIsNone(self.database.get_check_run(runs[1]))
        self.assertIsNone(self.database.get_check_run(quiet_run))

    def test_overview_attention_counts_each_active_branch_once(self):
        repository_id = self.database.list_watches()[0]["repository_id"]
        self.database.touch_unchanged(
            self.branch_id,
            present=False,
            expected_sequence=0,
            expected_oid="a" * 40,
        )
        pending_id = self.database.add_branch(
            repository_id,
            "pending",
            "origin",
            baseline_oid=None,
            present=True,
        )
        self.database.add_branch(
            repository_id,
            "healthy",
            "origin",
            baseline_oid="b" * 40,
        )

        self.assertEqual(self.database.overview_counts()["branch_errors"], 2)

        # An errored baseline-pending branch remains one attention item.
        self.database.set_check_error(
            pending_id,
            "fetch failed",
            expected_sequence=0,
            expected_oid=None,
        )
        self.assertEqual(self.database.overview_counts()["branch_errors"], 2)

        # Disabled watches no longer contribute to active dashboard attention.
        self.database.disable_watch(self.branch_id)
        self.assertEqual(self.database.overview_counts()["branch_errors"], 1)

    def test_prune_preserves_events_with_unresolved_deliveries(self):
        first = facts("a" * 40, "b" * 40)
        first_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            first,
            summarize_with_rules(first),
            expected_sequence=0,
        )
        terminal_delivery = self.database.ensure_delivery(first_id, "lark_doc", "doc")
        self.database.mark_delivery_success(terminal_delivery)

        second = facts("b" * 40, "c" * 40)
        second_id, _ = self.database.create_event_and_advance(
            self.branch_id,
            second,
            summarize_with_rules(second),
            expected_sequence=1,
        )
        self.database.ensure_delivery(second_id, "lark_doc", "doc")

        self.database.reserve_external_request("finished", "publish", "one")
        self.database.finish_external_request("finished", state="succeeded")
        self.database.reserve_external_request("pending", "publish", "two")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE external_requests SET updated_at='2026-08-18T00:00:00+00:00'"
            )

        preview = self.database.prune(
            "2026-08-19T00:00:00+00:00", dry_run=True
        )
        self.assertEqual(preview["events"], 1)
        self.assertEqual(preview["deliveries"], 1)
        self.assertEqual(preview["external_requests"], 1)
        self.assertEqual(preview["protected_events"], 1)
        self.assertIsNotNone(self.database.get_event(first_id))

        with self.assertRaises(ValueError):
            self.database.prune("not-a-cutoff")
        self.assertIsNotNone(self.database.get_event(first_id))

        removed = self.database.prune("2026-08-19T00:00:00+00:00")
        self.assertEqual(removed, {**preview, "dry_run": False})
        self.assertIsNone(self.database.get_event(first_id))
        self.assertIsNotNone(self.database.get_event(second_id))
        with self.database.connect() as connection:
            requests = connection.execute(
                "SELECT request_key FROM external_requests ORDER BY request_key"
            ).fetchall()
        self.assertEqual([row["request_key"] for row in requests], ["pending"])
        self.assertIsNotNone(self.database.daemon_health()["last_prune_at"])

    def test_repository_profile_preserves_original_name_and_tags(self):
        repository_id = self.database.list_repositories()[0]["id"]
        updated = self.database.update_repository_profile(
            repository_id,
            display_name="实验室后端",
            tags=["后端", "核心服务"],
        )
        self.assertEqual(updated["name"], "app")
        self.assertEqual(updated["display_name"], "实验室后端")
        watches = self.database.list_watches()
        self.assertEqual(watches[0]["repository"], "app")
        self.assertEqual(watches[0]["repository_alias"], "实验室后端")
        self.assertEqual(watches[0]["repository_tags_json"], '["后端","核心服务"]')
        self.assertIsNone(
            self.database.update_repository_profile(
                repository_id + 100,
                display_name="missing",
                tags=[],
            )
        )

    def test_disable_watch_preserves_history_and_cursor_and_can_resume(self):
        change = facts("a" * 40, "b" * 40)
        event_id, _key = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )
        before = self.database.list_watches()[0]

        removed = self.database.disable_watch(self.branch_id)

        self.assertEqual(removed["branch_id"], self.branch_id)
        self.assertEqual(removed["branch"], "main")
        self.assertEqual(self.database.list_watches(), [])
        self.assertEqual(self.database.list_repositories(), [])
        snapshot = self.database.dashboard_snapshot(event_limit=10)
        self.assertEqual(snapshot["counts"]["branches"], 0)
        self.assertEqual(snapshot["counts"]["events"], 0)
        self.assertEqual(snapshot["events"], [])

        # Historical events remain queryable outside the active dashboard.
        self.assertEqual([row["id"] for row in self.database.list_events()], [event_id])
        with self.database.connect() as connection:
            stored = connection.execute(
                """
                SELECT enabled, last_observed_oid, observation_sequence
                FROM tracked_branches WHERE id=?
                """,
                (self.branch_id,),
            ).fetchone()
        self.assertEqual(stored["enabled"], 0)
        self.assertEqual(stored["last_observed_oid"], before["last_observed_oid"])
        self.assertEqual(
            stored["observation_sequence"], before["observation_sequence"] + 1
        )

        repository_id = self.database.upsert_repository(candidate(self.root / "checkout"))
        resumed_id = self.database.add_branch(
            repository_id, "main", "origin", baseline_oid="c" * 40
        )
        resumed = self.database.list_watches()[0]
        self.assertEqual(resumed_id, self.branch_id)
        self.assertEqual(resumed["last_observed_oid"], "b" * 40)
        self.assertEqual(resumed["observation_sequence"], 2)

    def test_disabling_one_watch_keeps_repository_and_sibling_enabled(self):
        repository_id = self.database.list_watches()[0]["repository_id"]
        sibling_id = self.database.add_branch(
            repository_id, "develop", "origin", baseline_oid="d" * 40
        )

        self.assertIsNotNone(self.database.disable_watch(self.branch_id))

        watches = self.database.list_watches()
        self.assertEqual([row["branch_id"] for row in watches], [sibling_id])
        self.assertEqual(len(self.database.list_repositories()), 1)
        self.assertIsNone(self.database.disable_watch(self.branch_id))

    def test_event_listing_filters_by_safe_ids_and_uses_keyset_cursor(self):
        event_ids = []
        old = "a" * 40
        for sequence, letter in enumerate(("b", "c", "d")):
            change = facts(old, letter * 40)
            event_id, _key = self.database.create_event_and_advance(
                self.branch_id,
                change,
                summarize_with_rules(change),
                expected_sequence=sequence,
            )
            event_ids.append(event_id)
            old = letter * 40

        repository_id = self.database.list_watches()[0]["repository_id"]
        newest = self.database.list_events(
            repository_id=repository_id,
            branch_id=self.branch_id,
            limit=2,
        )
        self.assertEqual([row["id"] for row in newest], event_ids[:0:-1])
        self.assertTrue(all(row["repository_id"] == repository_id for row in newest))
        self.assertTrue(all(row["branch_id"] == self.branch_id for row in newest))

        older = self.database.list_events(
            repository_id=repository_id,
            branch_id=self.branch_id,
            before_id=newest[-1]["id"],
            limit=2,
        )
        self.assertEqual([row["id"] for row in older], [event_ids[0]])
        self.assertEqual(
            self.database.list_events(repository_id=repository_id + 100, limit=10),
            [],
        )

    def test_event_listing_can_narrow_to_a_time_window(self):
        event_ids = []
        old = "a" * 40
        for sequence, letter in enumerate(("b", "c", "d")):
            change = facts(old, letter * 40)
            event_id, _key = self.database.create_event_and_advance(
                self.branch_id,
                change,
                summarize_with_rules(change),
                expected_sequence=sequence,
            )
            event_ids.append(event_id)
            old = letter * 40

        # Timestamps are written by the database, so age the rows directly to
        # get a deterministic window without waiting.
        stamps = {
            event_ids[0]: "2020-01-01T00:00:00+00:00",
            event_ids[1]: "2026-08-01T00:00:00+00:00",
            event_ids[2]: "2026-08-20T00:00:00+00:00",
        }
        with self.database.connect() as connection:
            for event_id, detected_at in stamps.items():
                connection.execute(
                    "UPDATE branch_events SET detected_at=? WHERE id=?",
                    (detected_at, event_id),
                )

        self.assertEqual(
            [row["id"] for row in self.database.list_events(since="2026-08-10T00:00:00+00:00")],
            [event_ids[2]],
        )
        self.assertEqual(
            [row["id"] for row in self.database.list_events(since="2026-01-01T00:00:00+00:00")],
            [event_ids[2], event_ids[1]],
        )
        self.assertEqual(
            [row["id"] for row in self.database.list_events(since="2030-01-01T00:00:00+00:00")],
            [],
        )
        # The window composes with the keyset cursor rather than replacing it.
        self.assertEqual(
            [
                row["id"]
                for row in self.database.list_events(
                    since="2026-01-01T00:00:00+00:00", before_id=event_ids[2]
                )
            ],
            [event_ids[1]],
        )

    def test_event_listing_can_exclude_baseline_events(self):
        baseline = facts("a" * 40, "b" * 40)
        baseline.event_kind = "baseline"
        event_id, _key = self.database.create_event_and_advance(
            self.branch_id,
            baseline,
            summarize_with_rules(baseline),
            expected_sequence=0,
        )

        self.assertEqual([row["id"] for row in self.database.list_events()], [event_id])
        self.assertEqual(self.database.list_events(include_baseline=False), [])

    def test_compact_event_listing_skips_facts_and_preserves_search_authors(self):
        change = facts("a" * 40, "b" * 40)
        change.commits = [
            CommitInfo(
                sha="b" * 40,
                parents=["a" * 40],
                author_name="Alice",
                author_email="alice@example.invalid",
                author_date="2026-08-18T00:00:00+00:00",
                committer_name="Alice",
                committer_email="alice@example.invalid",
                commit_date="2026-08-18T00:00:00+00:00",
                subject="Optimize dashboard",
            )
        ]
        self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )

        compact = self.database.list_events(compact=True)[0]
        self.assertNotIn("facts", compact)
        self.assertNotIn("local_path", compact)
        self.assertEqual(compact["from_oid"], "a" * 40)
        self.assertEqual(compact["to_oid"], "b" * 40)
        self.assertEqual(compact["merge_base_oid"], "a" * 40)
        self.assertEqual(compact["authors"], ["Alice"])
        self.assertTrue(compact["summary"]["title"])

    def test_compact_timeline_uses_exact_tip_commit_not_latest_clock(self):
        change = facts("a" * 40, "b" * 40)
        change.commit_count = 2
        change.commits = [
            CommitInfo(
                sha="b" * 40,
                parents=["a" * 40],
                author_name="Alice",
                author_email="alice@example.invalid",
                author_date="2030-01-01T00:00:00+00:00",
                committer_name="Integrator",
                committer_email="integrator@example.invalid",
                commit_date="2026-08-18T10:00:00-04:00",
                subject="Tip commit",
            ),
            CommitInfo(
                sha="c" * 40,
                parents=[],
                author_name="Bob",
                author_email="bob@example.invalid",
                author_date="2026-08-20T01:00:00+00:00",
                committer_name="Integrator",
                committer_email="integrator@example.invalid",
                commit_date="2026-08-20T09:00:00+08:00",
                subject="Parent with a newer clock",
            ),
        ]
        self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )

        timeline = self.database.list_events(compact=True)[0]["list_metadata"][
            "timeline"
        ]

        self.assertEqual(timeline["basis"], "tip_commit")
        self.assertEqual(
            timeline["tip_committed_at"], "2026-08-18T14:00:00+00:00"
        )
        self.assertEqual(
            timeline["first_committed_at"], "2026-08-18T14:00:00+00:00"
        )
        self.assertEqual(
            timeline["last_committed_at"], "2026-08-20T01:00:00+00:00"
        )
        self.assertTrue(timeline["range_complete"])
        self.assertEqual(
            timeline["authors"],
            [
                {
                    "name": "Alice",
                    "first_committed_at": "2026-08-18T14:00:00+00:00",
                    "last_committed_at": "2026-08-18T14:00:00+00:00",
                    "sampled_commits": 1,
                },
                {
                    "name": "Bob",
                    "first_committed_at": "2026-08-20T01:00:00+00:00",
                    "last_committed_at": "2026-08-20T01:00:00+00:00",
                    "sampled_commits": 1,
                },
            ],
        )

    def test_compact_timeline_falls_back_when_tip_is_missing_and_ignores_invalid_dates(self):
        change = facts("a" * 40, "b" * 40)
        change.commits_are_tip_sample = True
        change.commits = [
            CommitInfo(
                sha="c" * 40,
                parents=[],
                author_name="Alice",
                author_email="alice@example.invalid",
                author_date="2026-08-19T10:00:00+00:00",
                committer_name="Alice",
                committer_email="alice@example.invalid",
                commit_date="2026-08-19T10:00:00",
                subject="Naive time",
            ),
            CommitInfo(
                sha="d" * 40,
                parents=[],
                author_name="Alice",
                author_email="alice@example.invalid",
                author_date="2026-08-19T10:00:00+00:00",
                committer_name="Alice",
                committer_email="alice@example.invalid",
                commit_date="not-a-timestamp",
                subject="Invalid time",
            ),
            CommitInfo(
                sha="e" * 40,
                parents=[],
                author_name="Bob",
                author_email="bob@example.invalid",
                author_date="2026-08-18T10:00:00+00:00",
                committer_name="Bob",
                committer_email="bob@example.invalid",
                commit_date="2026-08-18T10:00:00Z",
                subject="Valid sampled commit",
            ),
        ]
        self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )

        timeline = self.database.list_events(compact=True)[0]["list_metadata"][
            "timeline"
        ]

        self.assertEqual(timeline["basis"], "detected")
        self.assertIsNone(timeline["tip_committed_at"])
        self.assertEqual(
            timeline["first_committed_at"], "2026-08-18T10:00:00+00:00"
        )
        self.assertEqual(
            timeline["last_committed_at"], "2026-08-18T10:00:00+00:00"
        )
        self.assertFalse(timeline["range_complete"])
        self.assertEqual(timeline["authors"][0], {
            "name": "Alice",
            "first_committed_at": None,
            "last_committed_at": None,
            "sampled_commits": 2,
        })

    def test_schema_upgrade_backfills_timeline_into_nonempty_list_metadata(self):
        change = facts("a" * 40, "b" * 40)
        change.categories = ["api"]
        change.commits = [
            CommitInfo(
                sha="b" * 40,
                parents=["a" * 40],
                author_name="Alice",
                author_email="alice@example.invalid",
                author_date="2026-08-18T12:00:00+00:00",
                committer_name="Alice",
                committer_email="alice@example.invalid",
                commit_date="2026-08-18T12:00:00+00:00",
                subject="Persist timeline facts",
            )
        ]
        event_id, _event_key = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE branch_events SET list_metadata_json=? WHERE id=?",
                (json.dumps({"categories": ["stale"]}), event_id),
            )
            connection.execute("PRAGMA user_version = 2")

        self.database.initialize()

        compact = self.database.list_events(compact=True)[0]
        self.assertEqual(compact["list_metadata"]["categories"], ["api"])
        self.assertEqual(
            compact["list_metadata"]["timeline"]["tip_committed_at"],
            "2026-08-18T12:00:00+00:00",
        )
        with self.database.connect() as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION,
            )

    def test_schema_upgrade_marks_corrupt_facts_timeline_incomplete(self):
        change = facts("a" * 40, "b" * 40)
        event_id, _event_key = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE branch_events SET facts_json=?, list_metadata_json='{}' WHERE id=?",
                ("{invalid", event_id),
            )
            connection.execute("PRAGMA user_version = 2")

        self.database.initialize()

        timeline = self.database.list_events(compact=True)[0]["list_metadata"][
            "timeline"
        ]
        self.assertEqual(timeline["basis"], "detected")
        self.assertFalse(timeline["range_complete"])

    def test_performance_indexes_are_installed(self):
        with sqlite3.connect(str(self.database.path)) as connection:
            indexes = {
                row[0]
                for table in ("tracked_branches", "branch_events", "deliveries")
                for row in connection.execute("SELECT name FROM pragma_index_list(?)", (table,))
            }
        self.assertTrue({
            "tracked_branches_enabled_repository_idx",
            "branch_events_nonbaseline_id_idx",
            "branch_events_branch_nonbaseline_id_idx",
            "deliveries_event_latest_idx",
            "deliveries_destination_state_idx",
            "deliveries_active_state_idx",
        }.issubset(indexes))

    def test_delivery_claim_is_exclusive_and_retryable(self):
        change = facts("a" * 40, "b" * 40)
        event_id, _key = self.database.create_event_and_advance(
            self.branch_id,
            change,
            summarize_with_rules(change),
            expected_sequence=0,
        )
        delivery_id = self.database.ensure_delivery(event_id, "lark_doc", "doc-token")
        first_lease = self.database.claim_delivery(delivery_id)
        self.assertIsNotNone(first_lease)
        self.assertIsNone(self.database.claim_delivery(delivery_id))
        self.database.mark_delivery_error(
            delivery_id, "temporary", lease_token=first_lease
        )
        second_lease = self.database.claim_delivery(delivery_id)
        self.assertIsNotNone(second_lease)
        self.assertNotEqual(first_lease, second_lease)
        self.database.mark_delivery_success(
            delivery_id, "marker", lease_token=second_lease
        )
        self.assertEqual(
            self.database.list_pending_deliveries("lark_doc", "doc-token"), []
        )

    def test_delivery_backlog_pages_from_oldest_undelivered_event(self):
        event_ids = []
        old = "a" * 40
        for sequence, letter in enumerate(("b", "c", "d")):
            change = facts(old, letter * 40)
            event_id, _key = self.database.create_event_and_advance(
                self.branch_id,
                change,
                summarize_with_rules(change),
                expected_sequence=sequence,
            )
            event_ids.append(event_id)
            old = letter * 40

        first_page = self.database.list_events_without_delivery(
            "lark_doc", "doc-token", limit=2
        )
        self.assertEqual(first_page, event_ids[:2])
        for event_id in first_page:
            self.database.ensure_delivery(event_id, "lark_doc", "doc-token")
        self.assertEqual(
            self.database.list_events_without_delivery(
                "lark_doc", "doc-token", limit=2
            ),
            event_ids[2:],
        )

    def test_remote_rebind_requires_explicit_rebaseline(self):
        alternate = RepositoryCandidate(
            name="upstream-app",
            path=str(self.root / "checkout"),
            remote_name="upstream",
            remote_url="https://git.example.com/upstream/app.git",
            canonical_remote="git.example.com/upstream/app",
            current_branch="main",
            upstream_branch="main",
            default_branch="main",
        )
        self.database.upsert_repository(alternate)
        repository = self.database.list_repositories()[0]
        self.assertEqual(repository["name"], "app")
        self.assertEqual(
            repository["canonical_remote"], "git.example.com/team/app"
        )
        with self.assertRaisesRegex(ValueError, "拒绝静默"):
            self.database.add_branch(
                1,
                "main",
                "upstream",
                remote_fingerprint=alternate.canonical_remote,
                baseline_oid="z" * 40,
            )
        watch = self.database.list_watches()[0]
        self.assertEqual(watch["remote_name"], "origin")
        self.assertEqual(watch["last_observed_oid"], "a" * 40)

        self.database.add_branch(
            1,
            "main",
            "upstream",
            remote_fingerprint=alternate.canonical_remote,
            baseline_oid="z" * 40,
            rebaseline=True,
        )
        rebound = self.database.list_watches()[0]
        self.assertEqual(rebound["remote_name"], "upstream")
        self.assertEqual(rebound["last_observed_oid"], "z" * 40)
        self.assertEqual(rebound["observation_sequence"], 1)

    def test_same_oid_rebaseline_invalidates_stale_observation(self):
        self.database.add_branch(
            1,
            "main",
            "upstream",
            remote_fingerprint="git.example.com/upstream/app",
            baseline_oid="a" * 40,
            rebaseline=True,
        )
        stale = facts("a" * 40, "b" * 40)
        with self.assertRaises(ConcurrentUpdate):
            self.database.create_event_and_advance(
                self.branch_id,
                stale,
                summarize_with_rules(stale),
                expected_sequence=0,
            )

    def test_same_remote_name_fingerprint_change_requires_rebaseline(self):
        replacement = "git.example.com/other/app"
        with self.assertRaisesRegex(ValueError, "拒绝静默"):
            self.database.add_branch(
                1,
                "main",
                "origin",
                remote_fingerprint=replacement,
                baseline_oid="z" * 40,
            )
        unchanged = self.database.list_watches()[0]
        self.assertEqual(unchanged["remote_fingerprint"], "git.example.com/team/app")
        self.assertEqual(unchanged["last_observed_oid"], "a" * 40)

        self.database.add_branch(
            1,
            "main",
            "origin",
            remote_fingerprint=replacement,
            baseline_oid="z" * 40,
            rebaseline=True,
        )
        rebound = self.database.list_watches()[0]
        self.assertEqual(rebound["remote_fingerprint"], replacement)
        self.assertEqual(rebound["last_observed_oid"], "z" * 40)
        self.assertEqual(rebound["observation_sequence"], 1)

    def test_migration_backfills_primary_remote_and_preserves_local_only(self):
        legacy_path = self.root / "legacy.db"
        with sqlite3.connect(str(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE repositories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL UNIQUE,
                    remote_name TEXT,
                    remote_url TEXT,
                    canonical_remote TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_error TEXT
                );
                CREATE TABLE tracked_branches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repository_id INTEGER NOT NULL REFERENCES repositories(id),
                    name TEXT NOT NULL,
                    remote_name TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    present INTEGER NOT NULL DEFAULT 1,
                    last_observed_oid TEXT,
                    observation_sequence INTEGER NOT NULL DEFAULT 0,
                    last_checked_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(repository_id, name)
                );
                INSERT INTO repositories(
                    id, name, local_path, remote_name, remote_url,
                    canonical_remote, created_at, last_seen_at
                ) VALUES
                    (1, 'remote', '/tmp/remote', 'origin',
                     'https://git.example.com/team/app.git',
                     'git.example.com/team/app', 'now', 'now'),
                    (2, 'local', '/tmp/local', NULL, NULL, NULL, 'now', 'now');
                INSERT INTO tracked_branches(
                    repository_id, name, remote_name, last_observed_oid, created_at
                ) VALUES
                    (1, 'main', 'origin', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'now'),
                    (2, 'local-main', NULL, 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', 'now');
                """
            )

        legacy = Database(legacy_path)
        legacy.initialize()
        watches = legacy.list_watches()
        by_branch = {row["branch"]: row for row in watches}
        self.assertEqual(
            by_branch["main"]["remote_fingerprint"],
            "git.example.com/team/app",
        )
        self.assertIsNone(by_branch["local-main"]["remote_fingerprint"])
        repositories = legacy.list_repositories()
        self.assertTrue(all(row["display_name"] is None for row in repositories))
        self.assertTrue(all(row["tags_json"] == "[]" for row in repositories))

    def test_initialize_sets_schema_version_and_rejects_future_database(self):
        with self.database.connect() as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION,
            )

        future_path = self.root / "future.db"
        with sqlite3.connect(str(future_path)) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker(value) VALUES ('preserve')")
            connection.execute("PRAGMA user_version = %d" % (SCHEMA_VERSION + 1))
        with self.assertRaises(UnsupportedSchemaVersion):
            Database(future_path).initialize()
        with sqlite3.connect(str(future_path)) as connection:
            self.assertEqual(
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
                SCHEMA_VERSION + 1,
            )
            self.assertEqual(
                connection.execute("SELECT value FROM marker").fetchone()[0],
                "preserve",
            )


if __name__ == "__main__":
    unittest.main()
