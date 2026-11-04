import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from branch_radar.config import AppConfig
from branch_radar.database import Database
from branch_radar.git import GitError
from branch_radar.models import ChangeFacts, CommitInfo, DiffStats, RepositoryCandidate
from branch_radar.monitor import BranchMonitor
from branch_radar.sources import BranchTransition, FactSource, GitFactSource


class StubSource(FactSource):
    """A source with no Git anywhere, standing in for a webhook or mirror feed."""

    name = "stub"

    def __init__(self, tips: dict[str, str | None], *, blocked=None):
        self.tips = tips
        self.blocked = blocked or {}
        self.described: list[BranchTransition] = []
        self.patch_calls = 0

    def refresh(self, watches, *, fetch: bool = True) -> dict[int, str]:
        self.fetch_requested = fetch
        return dict(self.blocked)

    def resolve(self, watch):
        return self.tips.get(str(watch["branch"]))

    def describe(self, watch, transition):
        self.described.append(transition)
        kind = "deleted" if transition.new_sha is None else (
            "baseline" if transition.old_sha is None else "fast_forward"
        )
        return ChangeFacts(
            repository_name=str(watch["repository"]),
            repository_path=str(watch["local_path"]),
            remote_url="https://example.invalid/team/app.git",
            branch=str(watch["branch"]),
            event_kind=kind,
            old_sha=transition.old_sha,
            new_sha=transition.new_sha,
            merge_base=None,
            detected_at="2026-08-18T00:00:00Z",
            commit_count=1,
            commits=[
                CommitInfo(
                    sha=transition.new_sha or "0" * 40,
                    parents=[],
                    author_name="Webhook",
                    author_email="hook@example.invalid",
                    author_date="2026-08-18T00:00:00Z",
                    committer_name="Webhook",
                    committer_email="hook@example.invalid",
                    commit_date="2026-08-18T00:00:00Z",
                    subject="feat: from a non-git source",
                )
            ],
            diff=DiffStats(files_changed=1, insertions=1, deletions=0, files=["api.py"]),
        )

    def attach_patch_excerpt(self, watch, facts, *, max_bytes):
        self.patch_calls += 1


class RecordingCodex:
    def __init__(self):
        self.calls = 0

    def summarize(self, facts):
        self.calls += 1
        raise AssertionError("summarize must not run for comparison-free kinds")


class FactSourceSeamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.database = Database(root / "state.db")
        self.database.initialize()
        candidate = RepositoryCandidate(
            name="app",
            path=str(root / "app"),
            remote_name="origin",
            remote_url="https://example.invalid/team/app.git",
            canonical_remote="example.invalid/team/app",
            current_branch="main",
            upstream_branch="origin/main",
            default_branch="main",
        )
        repository_id = self.database.upsert_repository(candidate)
        self.database.add_branch(
            repository_id, "main", "origin", remote_fingerprint="example.invalid/team/app"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_monitor_records_durable_events_from_a_source_without_git(self):
        tip = "a" * 40
        source = StubSource({"main": tip})
        monitor = BranchMonitor(self.database, AppConfig(), source=source)

        first = monitor.check_all(fetch=True)
        self.assertEqual([item.status for item in first], ["baseline"])
        self.assertTrue(source.fetch_requested)

        moved = "b" * 40
        source.tips["main"] = moved
        second = monitor.check_all(fetch=False)
        self.assertEqual(second[0].status, "changed")
        self.assertEqual(second[0].event_kind, "fast_forward")
        self.assertEqual(second[0].old_sha, tip)
        self.assertEqual(second[0].new_sha, moved)
        self.assertEqual(second[0].summary.source, "rules")

        third = monitor.check_all(fetch=False)
        self.assertEqual(third[0].status, "unchanged")

        events = self.database.list_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(self.database.list_watches()[0]["last_observed_oid"], moved)

    def test_blocked_branches_are_recorded_without_moving_the_cursor(self):
        branch_id = int(self.database.list_watches()[0]["branch_id"])
        source = StubSource({"main": "c" * 40}, blocked={branch_id: "remote 身份已变化"})
        monitor = BranchMonitor(self.database, AppConfig(), source=source)

        results = monitor.check_all(fetch=True)
        self.assertEqual(results[0].status, "error")
        self.assertIn("remote 身份已变化", results[0].error)
        self.assertEqual(source.described, [])
        self.assertEqual(self.database.list_events(), [])
        watch = self.database.list_watches()[0]
        self.assertIsNone(watch["last_observed_oid"])
        self.assertIn("remote 身份已变化", watch["last_error"])

    def test_comparison_free_kinds_never_reach_the_model(self):
        codex = RecordingCodex()
        source = StubSource({"main": "d" * 40})
        monitor = BranchMonitor(
            self.database, AppConfig(), source=source, codex_summarizer=codex
        )
        baseline = monitor.check_all(fetch=False, use_codex=True)
        self.assertEqual(baseline[0].event_kind, "baseline")

        source.tips["main"] = None
        deleted = monitor.check_all(fetch=False, use_codex=True)
        self.assertEqual(deleted[0].event_kind, "deleted")

        self.assertEqual(codex.calls, 0)
        self.assertEqual(source.patch_calls, 0)

    def test_git_source_fetches_only_watched_branches_with_initial_timeout(self):
        git = mock.Mock()
        git.remote_url.return_value = "https://example.invalid/team/app.git"
        config = AppConfig(
            fetch_timeout_seconds=90,
            initial_fetch_timeout_seconds=480,
        )
        updates = []
        watches = self.database.list_watches()
        source = GitFactSource(config, git=git, progress=updates.append)

        blocked = source.refresh(watches, fetch=True)

        self.assertEqual(blocked, {})
        git.fetch.assert_called_once_with(
            Path(str(watches[0]["local_path"])),
            "origin",
            timeout=480,
            expected_fingerprint="example.invalid/team/app",
            branches=["main"],
        )
        self.assertEqual(updates[-1]["phase"], "fetch")

    def test_git_source_prepares_distinct_repositories_concurrently(self):
        class ConcurrentGit:
            def __init__(self):
                self.fetch_barrier = threading.Barrier(2)
                self.fetch_calls = []

            def remote_url(self, path, _remote):
                return "https://example.invalid/team/%s.git" % path.name

            def fetch(self, path, remote, **kwargs):
                self.fetch_calls.append((path, remote, kwargs))
                # No sleeps: the test can pass only if two workers reach this
                # synchronization point at the same time.
                self.fetch_barrier.wait(timeout=2)

            def branch_ref(self, path, _remote, branch):
                return (path.name + "-" + branch).ljust(40, "0")[:40]

        root = Path(self.temporary.name)
        watches = [
            self._watch(1, 1, root / "app-one", "app-one"),
            self._watch(2, 2, root / "app-two", "app-two"),
        ]
        git = ConcurrentGit()
        callback_threads = []
        updates = []
        caller_thread = threading.get_ident()

        def record_progress(value):
            callback_threads.append(threading.get_ident())
            updates.append(value)

        source = GitFactSource(
            AppConfig(),
            git=git,
            progress=record_progress,
            max_workers=4,
        )

        self.assertEqual(source.refresh(watches, fetch=True), {})
        resolved = [source.resolve(watch) for watch in watches]

        self.assertEqual(len(git.fetch_calls), 2)
        self.assertEqual(len(set(resolved)), 2)
        self.assertEqual(set(callback_threads), {caller_thread})
        fetch_updates = [value for value in updates if value["phase"] == "fetch"]
        self.assertEqual([value["current"] for value in fetch_updates], [0, 1, 2])
        self.assertEqual({value["total"] for value in fetch_updates}, {2})
        self.assertEqual(fetch_updates[0]["label"], "正在准备 2 个仓库")
        self.assertEqual(fetch_updates[0]["detail"], "正在获取并解析仓库引用。")
        self.assertNotIn("并行", fetch_updates[0]["label"])

    def test_git_source_serializes_distinct_groups_in_one_common_directory(self):
        class SharedObjectStoreGit:
            def __init__(self):
                self.fetch_barrier = threading.Barrier(2)
                self.counter_lock = threading.Lock()
                self.active = 0
                self.max_active = 0
                self.fetch_calls = []

            def remote_url(self, path, _remote):
                return "https://example.invalid/team/%s.git" % path.name

            def fetch(self, path, remote, **kwargs):
                with self.counter_lock:
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.fetch_calls.append((path, remote, kwargs))
                try:
                    # If these repository groups are scheduled independently,
                    # both workers rendezvous here and max_active becomes 2.
                    # A shared common-directory keeps them in one serial unit,
                    # so the first wait times out before the second fetch starts.
                    try:
                        self.fetch_barrier.wait(timeout=0.2)
                    except threading.BrokenBarrierError:
                        pass
                finally:
                    with self.counter_lock:
                        self.active -= 1

            def branch_ref(self, path, _remote, branch):
                return (path.name + "-" + branch).ljust(40, "0")[:40]

        root = Path(self.temporary.name)
        watches = [
            self._watch(31, 31, root / "app-one", "app-one"),
            self._watch(32, 32, root / "app-two", "app-two"),
        ]
        git = SharedObjectStoreGit()
        source = GitFactSource(AppConfig(), git=git, max_workers=4)

        with mock.patch(
            "branch_radar.sources.git_source._git_common_directory",
            return_value=root / ".git-common",
        ):
            self.assertEqual(source.refresh(watches, fetch=True), {})

        self.assertEqual(len(git.fetch_calls), 2)
        self.assertEqual(git.max_active, 1)
        self.assertEqual(len({source.resolve(watch) for watch in watches}), 2)

    def test_git_source_uses_distinct_prepare_phase_without_fetch(self):
        git = mock.Mock()
        git.remote_url.return_value = "https://example.invalid/team/app.git"
        git.branch_ref.return_value = "a" * 40
        watches = self.database.list_watches()
        updates = []
        source = GitFactSource(AppConfig(), git=git, progress=updates.append)

        self.assertEqual(source.refresh(watches, fetch=False), {})
        self.assertEqual(source.resolve(watches[0]), "a" * 40)

        self.assertEqual([value["phase"] for value in updates], [
            "inspect", "inspect", "resolve", "resolve",
        ])
        self.assertEqual(updates[0]["detail"], "正在解析本地仓库引用。")
        self.assertEqual(
            [value["current"] for value in updates if value["phase"] == "resolve"],
            [0, 1],
        )

    def test_git_source_fetches_once_and_resolves_serially_per_group(self):
        class RecordingGit:
            def __init__(self):
                self.fetch_calls = []
                self.branch_calls = []

            def remote_url(self, _path, _remote):
                return "https://example.invalid/team/app.git"

            def fetch(self, path, remote, **kwargs):
                self.fetch_calls.append((path, remote, kwargs))

            def branch_ref(self, path, remote, branch):
                self.branch_calls.append((path, remote, branch))
                return branch.ljust(40, "0")[:40]

        root = Path(self.temporary.name)
        watches = [
            self._watch(1, 1, root / "app", "app", branch="main"),
            self._watch(2, 1, root / "app", "app", branch="release"),
        ]
        git = RecordingGit()
        source = GitFactSource(AppConfig(), git=git)

        self.assertEqual(source.refresh(watches, fetch=True), {})

        self.assertEqual(len(git.fetch_calls), 1)
        self.assertEqual(
            git.fetch_calls[0][2]["branches"], ["main", "release"]
        )
        self.assertEqual(
            [call[2] for call in git.branch_calls], ["main", "release"]
        )
        self.assertEqual(source.resolve(watches[0]), "main".ljust(40, "0")[:40])
        self.assertEqual(source.resolve(watches[1]), "release".ljust(40, "0")[:40])
        self.assertEqual(len(git.branch_calls), 2)

    def test_git_source_isolates_group_fetch_errors(self):
        class PartiallyFailingGit:
            def remote_url(self, path, _remote):
                return "https://example.invalid/team/%s.git" % path.name

            def fetch(self, path, _remote, **_kwargs):
                if path.name == "broken":
                    raise GitError("token=should-not-leak")
                if path.name == "os-broken":
                    raise OSError("process launch failed")

            def branch_ref(self, path, _remote, _branch):
                return (path.name + "-tip").ljust(40, "0")[:40]

        root = Path(self.temporary.name)
        broken = self._watch(11, 11, root / "broken", "broken")
        healthy = self._watch(12, 12, root / "healthy", "healthy")
        os_broken = self._watch(13, 13, root / "os-broken", "os-broken")
        source = GitFactSource(AppConfig(), git=PartiallyFailingGit())

        blocked = source.refresh([broken, healthy, os_broken], fetch=True)

        self.assertEqual(set(blocked), {11, 13})
        self.assertNotIn("should-not-leak", blocked[11])
        self.assertEqual(
            source.resolve(healthy), "healthy-tip".ljust(40, "0")[:40]
        )

    def test_git_source_isolates_unexpected_group_error_in_shared_work_unit(self):
        class UnexpectedGit:
            def remote_url(self, path, _remote):
                return "https://example.invalid/team/%s.git" % path.name

            def fetch(self, path, _remote, **_kwargs):
                if path.name == "broken":
                    raise RuntimeError("github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456")

            def branch_ref(self, path, _remote, _branch):
                return (path.name + "-tip").ljust(40, "0")[:40]

        root = Path(self.temporary.name)
        broken = self._watch(21, 21, root / "broken", "broken")
        healthy = self._watch(22, 22, root / "healthy", "healthy")
        source = GitFactSource(AppConfig(), git=UnexpectedGit())

        with mock.patch(
            "branch_radar.sources.git_source._lock_key", return_value="shared"
        ):
            blocked = source.refresh([broken, healthy], fetch=True)

        self.assertEqual(set(blocked), {21})
        self.assertNotIn("github_pat_", blocked[21])
        self.assertEqual(
            source.resolve(healthy), "healthy-tip".ljust(40, "0")[:40]
        )

    @staticmethod
    def _watch(
        branch_id, repository_id, path, repository, *, branch="main"
    ):
        return {
            "branch_id": branch_id,
            "repository_id": repository_id,
            "repository": repository,
            "local_path": str(path),
            "remote_name": "origin",
            "remote_fingerprint": "example.invalid/team/%s" % repository,
            "branch": branch,
            "last_observed_oid": None,
        }


if __name__ == "__main__":
    unittest.main()
