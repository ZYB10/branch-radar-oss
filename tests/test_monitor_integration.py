import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from branch_radar.config import AppConfig, ConfigStore
from branch_radar.database import Database
from branch_radar.git import GitClient
from branch_radar.monitor import BranchMonitor
from branch_radar.paths import AppPaths
from branch_radar.remote import normalize_remote
from branch_radar.scanner import RepositoryScanner
from branch_radar.services import RadarService, ServiceError


def git(*args, cwd=None):
    return subprocess.run(
        ["git", *list(args)],
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class MonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.origin = self.root / "origin.git"
        self.work = self.root / "work"
        git("init", "--bare", "-b", "main", str(self.origin))
        git("clone", str(self.origin), str(self.work))
        git("config", "user.name", "Alice", cwd=self.work)
        git("config", "user.email", "alice@example.com", cwd=self.work)
        (self.work / "README.md").write_text("first\n", encoding="utf-8")
        git("add", "README.md", cwd=self.work)
        git("commit", "-m", "docs: initial", cwd=self.work)
        git("push", "-u", "origin", "main", cwd=self.work)

        self.database = Database(self.root / "state.db")
        self.database.initialize()
        self.git_client = GitClient()
        candidate = RepositoryScanner(self.git_client).inspect(self.work)
        repository_id = self.database.upsert_repository(candidate)
        self.repository_id = repository_id
        baseline = self.git_client.branch_ref(self.work, "origin", "main")
        self.database.add_branch(repository_id, "main", "origin", baseline_oid=baseline)
        self.baseline = baseline

    def tearDown(self):
        self.temporary.cleanup()

    def test_fast_forward_is_durable_and_idempotent(self):
        (self.work / "api.py").write_text("def list_materials():\n    return []\n", encoding="utf-8")
        git("add", "api.py", cwd=self.work)
        git("commit", "-m", "feat(api): add material listing", cwd=self.work)
        git("push", cwd=self.work)

        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        with mock.patch.object(
            self.git_client,
            "patch_excerpt",
            side_effect=AssertionError("ordinary checks must not read a raw diff"),
        ):
            first = monitor.check_all(fetch=True)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].status, "changed")
        self.assertEqual(first[0].event_kind, "fast_forward")
        self.assertEqual(first[0].old_sha, self.baseline)
        self.assertEqual(first[0].summary.source, "rules")

        events = self.database.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["facts"]["commits"][0]["author_name"], "Alice")
        self.assertIn("api.py", events[0]["facts"]["diff"]["files"])

        second = monitor.check_all(fetch=False)
        self.assertEqual(second[0].status, "unchanged")
        self.assertEqual(len(self.database.list_events()), 1)

    def test_force_push_records_added_and_dropped_history(self):
        (self.work / "old.txt").write_text("old\n", encoding="utf-8")
        git("add", "old.txt", cwd=self.work)
        git("commit", "-m", "feat: old direction", cwd=self.work)
        old_tip = git("rev-parse", "HEAD", cwd=self.work)
        git("push", cwd=self.work)
        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        monitor.check_all(fetch=True)

        git("reset", "--hard", self.baseline, cwd=self.work)
        (self.work / "new.txt").write_text("new\n", encoding="utf-8")
        git("add", "new.txt", cwd=self.work)
        git("commit", "-m", "feat: new direction", cwd=self.work)
        git("push", "--force-with-lease", cwd=self.work)

        result = monitor.check_all(fetch=True)[0]
        self.assertEqual(result.event_kind, "force_push")
        event = self.database.get_event(result.event_id)
        dropped = [item["sha"] for item in event["facts"]["dropped_commits"]]
        self.assertIn(old_tip, dropped)
        self.assertIn("new.txt", event["facts"]["diff"]["files"])

    def test_rewind_is_distinct_from_diverged_force_push(self):
        (self.work / "ahead.txt").write_text("ahead\n", encoding="utf-8")
        git("add", "ahead.txt", cwd=self.work)
        git("commit", "-m", "feat: ahead commit", cwd=self.work)
        git("push", cwd=self.work)
        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        monitor.check_all(fetch=True)

        git("reset", "--hard", self.baseline, cwd=self.work)
        git("push", "--force-with-lease", cwd=self.work)
        result = monitor.check_all(fetch=True)[0]
        self.assertEqual(result.event_kind, "rewind")

    def test_remote_delete_is_not_hidden_by_local_branch_and_recreate_is_detected(self):
        git("checkout", "-b", "feature/local-copy", cwd=self.work)
        (self.work / "feature.txt").write_text("feature\n", encoding="utf-8")
        git("add", "feature.txt", cwd=self.work)
        git("commit", "-m", "feat: feature branch", cwd=self.work)
        git("push", "-u", "origin", "feature/local-copy", cwd=self.work)
        feature_oid = git("rev-parse", "HEAD", cwd=self.work)
        self.database.add_branch(
            self.repository_id,
            "feature/local-copy",
            "origin",
            baseline_oid=feature_oid,
        )
        git("checkout", "main", cwd=self.work)
        git("push", "origin", "--delete", "feature/local-copy", cwd=self.work)

        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        deleted = next(
            item for item in monitor.check_all(fetch=True)
            if item.branch == "feature/local-copy"
        )
        self.assertEqual(deleted.event_kind, "deleted")

        # Re-running init/watch must not overwrite the durable deleted cursor state.
        self.database.add_branch(
            self.repository_id,
            "feature/local-copy",
            "origin",
            baseline_oid=feature_oid,
        )
        missing = next(
            item for item in monitor.check_all(fetch=False)
            if item.branch == "feature/local-copy"
        )
        self.assertEqual(missing.status, "unchanged")
        self.assertEqual(missing.event_kind, "deleted")

        git("checkout", "feature/local-copy", cwd=self.work)
        (self.work / "feature.txt").write_text("feature v2\n", encoding="utf-8")
        git("add", "feature.txt", cwd=self.work)
        git("commit", "-m", "feat: evolve recreated branch", cwd=self.work)
        git("checkout", "main", cwd=self.work)
        git("push", "origin", "feature/local-copy:feature/local-copy", cwd=self.work)
        recreated = next(
            item for item in monitor.check_all(fetch=True)
            if item.branch == "feature/local-copy"
        )
        self.assertEqual(recreated.event_kind, "recreated")
        event = self.database.get_event(recreated.event_id)
        self.assertEqual(event["facts"]["previous_tip"], feature_oid)
        self.assertIn("feature.txt", event["facts"]["diff"]["files"])

    def test_upstream_remote_is_preferred_over_origin(self):
        upstream = self.root / "upstream.git"
        git("init", "--bare", "-b", "main", str(upstream))
        git("remote", "add", "upstream", str(upstream), cwd=self.work)
        git("push", "-u", "upstream", "main", cwd=self.work)
        self.assertEqual(self.git_client.select_remote(self.work), "upstream")

    def test_fetch_ignores_repository_refspec_and_prune_tags_settings(self):
        git(
            "config",
            "--add",
            "remote.origin.fetch",
            "+refs/heads/*:refs/heads/mirror/*",
            cwd=self.work,
        )
        git("tag", "local-only", cwd=self.work)
        git("config", "fetch.pruneTags", "true", cwd=self.work)
        git("config", "remote.origin.pruneTags", "true", cwd=self.work)

        self.git_client.fetch(self.work, "origin")

        mirror_ref = subprocess.run(
            ["git", "rev-parse", "--verify", "refs/heads/mirror/main"],
            cwd=str(self.work),
            capture_output=True,
        )
        self.assertNotEqual(mirror_ref.returncode, 0)
        self.assertEqual(git("rev-parse", "refs/tags/local-only", cwd=self.work), self.baseline)

    def test_each_watched_remote_is_fetched_independently(self):
        upstream = self.root / "upstream-independent.git"
        git("init", "--bare", "-b", "main", str(upstream))
        git("remote", "add", "upstream", str(upstream), cwd=self.work)
        git("checkout", "-b", "upstream-feature", cwd=self.work)
        (self.work / "upstream.txt").write_text("upstream\n", encoding="utf-8")
        git("add", "upstream.txt", cwd=self.work)
        git("commit", "-m", "feat: upstream only", cwd=self.work)
        git("push", "-u", "upstream", "HEAD:remote-only", cwd=self.work)
        upstream_oid = git("rev-parse", "refs/remotes/upstream/remote-only", cwd=self.work)
        git("checkout", "main", cwd=self.work)
        self.database.add_branch(
            self.repository_id,
            "remote-only",
            "upstream",
            remote_fingerprint=normalize_remote(
                str(upstream), base=self.work
            ).canonical,
            baseline_oid=upstream_oid,
        )

        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        with mock.patch.object(
            self.git_client, "fetch", wraps=self.git_client.fetch
        ) as fetch_mock:
            results = monitor.check_all(fetch=True)

        fetched_remotes = {call.args[1] for call in fetch_mock.call_args_list}
        self.assertEqual(fetched_remotes, {"origin", "upstream"})
        self.assertTrue(all(result.status == "unchanged" for result in results))

    def test_remote_rebind_during_fetch_cannot_advance_cursor(self):
        replacement = self.root / "race-replacement.git"
        git("init", "--bare", "-b", "main", str(replacement))
        original_fetch = self.git_client.fetch

        def rebind_then_fetch(path, remote, **kwargs):
            git("remote", "set-url", remote, str(replacement), cwd=self.work)
            return original_fetch(path, remote, **kwargs)

        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        with mock.patch.object(
            self.git_client, "fetch", side_effect=rebind_then_fetch
        ):
            result = monitor.check_all(fetch=True)[0]

        self.assertEqual(result.status, "error")
        self.assertIn("检查期间发生变化", result.error)
        watch = self.database.list_watches()[0]
        self.assertEqual(watch["last_observed_oid"], self.baseline)
        self.assertEqual(watch["observation_sequence"], 0)
        self.assertEqual(self.database.list_events(), [])

    def test_changed_remote_url_is_blocked_before_fetch_until_rebaseline(self):
        replacement = self.root / "replacement.git"
        git("init", "--bare", "-b", "main", str(replacement))
        (self.work / "replacement.txt").write_text("replacement\n", encoding="utf-8")
        git("add", "replacement.txt", cwd=self.work)
        git("commit", "-m", "feat: replacement history", cwd=self.work)
        replacement_oid = git("rev-parse", "HEAD", cwd=self.work)
        git("push", str(replacement), "HEAD:main", cwd=self.work)
        git("remote", "set-url", "origin", str(replacement), cwd=self.work)

        monitor = BranchMonitor(self.database, AppConfig(), git=self.git_client)
        with mock.patch.object(
            self.git_client,
            "fetch",
            wraps=self.git_client.fetch,
        ) as fetch_mock:
            blocked = monitor.check_all(fetch=True)
        self.assertEqual(blocked[0].status, "error")
        self.assertIn("仓库身份已变化", blocked[0].error)
        fetch_mock.assert_not_called()
        unchanged = self.database.list_watches()[0]
        self.assertEqual(unchanged["last_observed_oid"], self.baseline)
        self.assertEqual(unchanged["observation_sequence"], 0)
        self.assertEqual(self.database.list_events(), [])

        home = self.root / "service-home"
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_HOME": str(home)}):
            paths = AppPaths.discover()
        paths.ensure()
        config = AppConfig()
        store = ConfigStore(paths)
        store.save(config)
        service = RadarService(
            paths,
            store,
            config,
            self.database,
            self.git_client,
        )

        with mock.patch.object(
            self.git_client,
            "fetch",
            wraps=self.git_client.fetch,
        ) as fetch_mock, self.assertRaises(ServiceError):
            service.add_watches([{
                "path": str(self.work),
                "branches": ["main"],
            }])
        fetch_mock.assert_not_called()

        service.add_watches([{
            "path": str(self.work),
            "branches": ["main"],
            "rebaseline": True,
        }])
        rebound = self.database.list_watches()[0]
        self.assertEqual(
            rebound["remote_fingerprint"],
            normalize_remote(str(replacement), base=self.work).canonical,
        )
        self.assertEqual(rebound["last_observed_oid"], replacement_oid)
        self.assertEqual(rebound["observation_sequence"], 1)
        self.assertIsNone(rebound["last_error"])

        after = monitor.check_all(fetch=True)
        self.assertEqual(after[0].status, "unchanged")

    def test_git_environment_cannot_redirect_facts_to_another_repository(self):
        other = self.root / "other"
        git("init", "-q", "-b", "main", str(other))
        git("config", "user.name", "Mallory", cwd=other)
        git("config", "user.email", "mallory@example.com", cwd=other)
        git("commit", "--allow-empty", "-m", "other", cwd=other)
        with mock.patch.dict(
            os.environ,
            {
                "GIT_DIR": str(other / ".git"),
                "GIT_WORK_TREE": str(other),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.bare",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            observed = self.git_client.branch_ref(self.work, "origin", "main")
        self.assertEqual(observed, self.baseline)


if __name__ == "__main__":
    unittest.main()
