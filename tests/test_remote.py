import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from branch_radar.database import Database
from branch_radar.git import (
    GitClient,
    GitCloneCancelled,
    GitError,
    _redact_patch,
    is_safe_remote_url,
)
from branch_radar.models import ChangeFacts, RepositoryCandidate
from branch_radar.process import ProcessResult
from branch_radar.remote import (
    normalize_remote,
    parse_github_clone_locator,
    parse_repository_locator,
)
from branch_radar.scanner import RepositoryScanner


class RemoteNormalizationTests(unittest.TestCase):
    def test_scanner_inspects_repositories_concurrently_and_sorts_results(self):
        class ConcurrentScanner(RepositoryScanner):
            def __init__(self):
                super().__init__(max_workers=4)
                self.barrier = threading.Barrier(2)

            def inspect(self, path):
                # This synchronization point cannot be crossed by a serial
                # implementation, and avoids asserting fragile durations.
                self.barrier.wait(timeout=2)
                return RepositoryCandidate(
                    name=path.name,
                    path=str(path),
                    remote_name=None,
                    remote_url=None,
                    canonical_remote=None,
                    current_branch=None,
                    upstream_branch=None,
                    default_branch=None,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("zeta", "Alpha"):
                (root / name / ".git").mkdir(parents=True)

            candidates = ConcurrentScanner().scan([root])

        self.assertEqual([item.name for item in candidates], ["Alpha", "zeta"])
        self.assertTrue(all(item.error is None for item in candidates))

    def test_scanner_isolates_unexpected_repository_error(self):
        class PartiallyFailingScanner(RepositoryScanner):
            def inspect(self, path):
                if path.name == "broken":
                    raise RuntimeError("token=should-not-leak")
                return RepositoryCandidate(
                    name=path.name,
                    path=str(path),
                    remote_name=None,
                    remote_url=None,
                    canonical_remote=None,
                    current_branch=None,
                    upstream_branch=None,
                    default_branch=None,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("healthy", "broken"):
                (root / name / ".git").mkdir(parents=True)

            candidates = PartiallyFailingScanner().scan([root])

        self.assertEqual(len(candidates), 2)
        broken = next(item for item in candidates if item.name == "broken")
        healthy = next(item for item in candidates if item.name == "healthy")
        self.assertIsNotNone(broken.error)
        self.assertNotIn("should-not-leak", broken.error)
        self.assertIsNone(healthy.error)

    def test_scp_style_remote(self):
        remote = normalize_remote("git@git.example.org:lab/Sample-Cloud.git")
        self.assertEqual(remote.canonical, "git.example.org/lab/Sample-Cloud")
        self.assertEqual(remote.sanitized_url, "git@git.example.org:lab/Sample-Cloud.git")

    def test_https_credentials_are_removed(self):
        remote = normalize_remote("https://alice:secret@git.example.com:8443/team/app.git?token=x")
        self.assertEqual(remote.canonical, "git.example.com:8443/team/app")
        self.assertEqual(remote.sanitized_url, "https://git.example.com:8443/team/app.git")
        self.assertNotIn("alice", remote.sanitized_url)
        self.assertNotIn("secret", remote.sanitized_url)
        self.assertNotIn("token", remote.sanitized_url)

    def test_ssh_url_keeps_non_secret_username(self):
        remote = normalize_remote("ssh://git@git.example.com/team/App.git")
        self.assertEqual(remote.canonical, "git.example.com/team/App")
        self.assertEqual(remote.sanitized_url, "ssh://git@git.example.com/team/App.git")

    def test_relative_local_remote_uses_repository_base(self):
        remote = normalize_remote("../origin.git", base=Path("/tmp/work/repo"))
        self.assertEqual(remote.host, "local")
        self.assertTrue(remote.sanitized_url.endswith("/tmp/work/origin.git"))

    def test_file_url_and_plain_relative_remote_are_supported(self):
        file_remote = normalize_remote("file:///tmp/origin.git")
        relative = normalize_remote("origin.git", base=Path("/tmp/work/repo"))
        self.assertEqual(
            Path(file_remote.sanitized_url), Path("/tmp/origin.git").resolve()
        )
        self.assertTrue(relative.sanitized_url.endswith("/tmp/work/repo/origin.git"))
        self.assertTrue(is_safe_remote_url("origin.git"))

    def test_unsafe_transport_helpers_are_rejected(self):
        self.assertFalse(is_safe_remote_url("ext::sh -c evil"))
        self.assertFalse(is_safe_remote_url("custom::payload"))
        self.assertFalse(is_safe_remote_url("--upload-pack=evil:repo"))
        self.assertTrue(is_safe_remote_url("git@git.example.org:lab/app.git"))
        self.assertTrue(is_safe_remote_url("workgit:team/app.git"))
        self.assertTrue(is_safe_remote_url("localhost:/srv/app.git"))
        self.assertTrue(is_safe_remote_url("https://git.example.org/lab/app.git"))
        with self.assertRaises(ValueError):
            normalize_remote("ext::helper token=super-secret")

    def test_github_locator_strips_web_paths_and_www(self):
        https = parse_repository_locator(
            "https://alice:token@www.github.com/acme/app/tree/feature/x"
        )
        self.assertEqual(https.canonical, "github.com/acme/app")
        self.assertEqual(https.host, "github.com")
        self.assertEqual(https.project_path, "acme/app")
        self.assertNotIn("token", https.sanitized_url)
        ssh = parse_repository_locator("git@github.com:acme/app.git")
        self.assertEqual(ssh.canonical, "github.com/acme/app")
        with self.assertRaisesRegex(ValueError, "本地路径"):
            parse_repository_locator("/tmp/origin.git")
        with self.assertRaisesRegex(ValueError, "owner/repo"):
            parse_repository_locator("https://github.com/acme")

    def test_github_clone_locator_accepts_safe_transports_and_web_branch(self):
        https = parse_github_clone_locator(
            "https://www.github.com/Acme/App/tree/feature/x?tab=readme#top"
        )
        self.assertEqual(https.canonical, "github.com/Acme/App")
        self.assertEqual(https.sanitized_url, "https://github.com/Acme/App.git")
        ssh = parse_github_clone_locator("ssh://git@github.com/acme/app.git")
        self.assertEqual(ssh.sanitized_url, "ssh://git@github.com/acme/app.git")
        scp = parse_github_clone_locator("git@github.com:acme/app.git")
        self.assertEqual(scp.sanitized_url, "git@github.com:acme/app.git")
        dot_repo = parse_github_clone_locator("https://github.com/acme/.github.git")
        self.assertEqual(dot_repo.project_path, "acme/.github")

    def test_github_clone_locator_rejects_credentials_local_and_other_hosts(self):
        invalid = (
            "https://token@github.com/acme/app.git",
            "https://github.com:443/acme/app.git",
            "http://github.com/acme/app.git",
            "https://gitlab.com/acme/app.git",
            "ssh://root@github.com/acme/app.git",
            "file:///tmp/app.git",
            "/tmp/app.git",
            "ext::helper payload",
            "https://github.com/acme/app/blob/main/README.md",
            "https://github.com/acme//app.git",
            "https://github.com/acme%2fapp/repo.git",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_github_clone_locator(value)

    def test_clone_repository_uses_bounded_safe_noninteractive_git(self):
        class Runner:
            def __init__(self):
                self.argv = []
                self.kwargs = {}

            def run_limited(self, argv, **kwargs):
                self.argv = list(argv)
                self.kwargs = kwargs
                callback = kwargs.get("progress")
                if callback:
                    callback("stderr", "Receiving objects: 50% (5/10)\r")
                return ProcessResult(list(argv), 0, "", "", 0.01)

        with tempfile.TemporaryDirectory() as directory:
            runner = Runner()
            updates = []
            destination = Path(directory) / "checkout"
            GitClient(runner=runner, binary="git").clone_repository(
                "https://github.com/acme/app.git",
                destination,
                progress=lambda stream, chunk: updates.append((stream, chunk)),
            )

        self.assertIn("clone", runner.argv)
        self.assertIn("--filter=blob:none", runner.argv)
        self.assertIn("--no-tags", runner.argv)
        self.assertIn("--no-recurse-submodules", runner.argv)
        self.assertIn("--no-local", runner.argv)
        self.assertNotIn("--depth", runner.argv)
        self.assertNotIn("--single-branch", runner.argv)
        self.assertIn("protocol.allow=never", runner.argv)
        self.assertIn("protocol.file.allow=never", runner.argv)
        self.assertIn("core.hooksPath=%s" % os.devnull, runner.argv)
        self.assertEqual(runner.kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(runner.kwargs["env"]["GIT_LFS_SKIP_SMUDGE"], "1")
        self.assertEqual(runner.kwargs["env"]["LC_ALL"], "C")
        self.assertTrue(runner.kwargs["replace_env"])
        self.assertEqual(len(updates), 1)

    def test_clone_repository_classifies_cancellation(self):
        class Runner:
            def run_limited(self, argv, **_kwargs):
                return ProcessResult(
                    list(argv), 130, "", "", 0.01, cancelled=True
                )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(GitCloneCancelled):
                GitClient(runner=Runner(), binary="git").clone_repository(
                    "git@github.com:acme/app.git",
                    Path(directory) / "checkout",
                )

    def test_git_execution_limits_propagate_cancel_and_shared_deadline(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run_limited(self, argv, **kwargs):
                self.calls.append((list(argv), kwargs))
                return ProcessResult(list(argv), 0, "ok", "", 0.01)

        runner = Runner()
        client = GitClient(runner=runner, binary="git")
        cancel_event = threading.Event()
        deadline = time.monotonic() + 1
        with client.execution_limits(
            cancel_event=cancel_event, deadline=deadline
        ):
            result = client.run(Path("/repo"), ["status"], timeout=30)
        self.assertTrue(result.ok)
        kwargs = runner.calls[0][1]
        self.assertIs(kwargs["cancel_event"], cancel_event)
        self.assertGreater(kwargs["timeout"], 0)
        self.assertLessEqual(kwargs["timeout"], 1)

        cancel_event.set()
        with client.execution_limits(cancel_event=cancel_event, deadline=deadline):
            candidate = RepositoryScanner(client).inspect(Path("/repo"))
        self.assertIn("取消", candidate.error or "")
        self.assertEqual(len(runner.calls), 1)

    def test_unsafe_remote_is_not_returned_by_scanner_or_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repository), "config",
                    "remote.origin.url", "ext::helper token=super-secret",
                ],
                check=True,
            )
            candidate = RepositoryScanner().inspect(repository)
            self.assertIsNotNone(candidate.error)
            self.assertIsNone(candidate.remote_url)
            self.assertNotIn("super-secret", str(candidate.to_dict()))

    def test_inspect_lists_remote_tracking_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            subprocess.run(
                ["git", "init", "-q", str(repository)],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.email", "dev@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "config", "user.name", "Dev"],
                check=True,
            )
            (repository / "README").write_text("demo\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(repository), "add", "README"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "commit", "-qm", "init"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(repository), "remote", "add", "origin",
                    "https://github.com/acme/app.git",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "update-ref", "refs/remotes/origin/main", "HEAD"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repository), "update-ref", "refs/remotes/origin/release", "HEAD"],
                check=True,
            )
            candidate = RepositoryScanner().inspect(repository)
            self.assertIsNone(candidate.error)
            self.assertEqual(candidate.canonical_remote, "github.com/acme/app")
            self.assertIn("main", candidate.branches)
            self.assertIn("release", candidate.branches)

            database = Database(root / "state.db")
            database.initialize()
            database.upsert_repository(candidate)
            self.assertNotIn("super-secret", str(database.list_repositories()))

    def test_patch_redacts_secret_files_and_assignments(self):
        patch = (
            "diff --git a/.env b/.env\n"
            "+TOKEN=super-secret\n"
            "diff --git a/app.py b/app.py\n"
            "+api_key = abc123\n"
            "+{\"access_token\": \"json-secret\"}\n"
            "+-----BEGIN PRIVATE KEY-----\n"
            "+private-material\n"
            "+-----END PRIVATE KEY-----\n"
            "+print('safe')\n"
        )
        redacted = _redact_patch(patch)
        self.assertNotIn("super-secret", redacted)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("json-secret", redacted)
        self.assertNotIn("private-material", redacted)
        self.assertIn("redacted sensitive file", redacted)
        self.assertIn("print('safe')", redacted)

    def test_git_display_text_is_redacted_before_facts_serialization(self):
        author_name_token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        author_email_token = "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        committer_name_token = "github_pat_11AA0_exampleTokenMaterial_0123456789"
        committer_email_token = "glpat-ABCDEFGHIJKLMNOPQRSTUVWX"
        file_token = "xoxb-example-placeholder-for-tests"
        patch_path_token = "AKIAIOSFODNN7EXAMPLE"
        patch_content_token = "sk-live-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        tokens = (
            author_name_token,
            author_email_token,
            committer_name_token,
            committer_email_token,
            file_token,
            patch_path_token,
            patch_content_token,
        )

        class Runner:
            def run_limited(self, argv, **_kwargs):
                if "log" in argv:
                    record = "\x1f".join(
                        [
                            "a" * 40,
                            "b" * 40,
                            "Alice %s" % author_name_token,
                            "%s@example.invalid" % author_email_token,
                            "2026-08-20T00:00:00+00:00",
                            "Integrator %s" % committer_name_token,
                            "%s@example.invalid" % committer_email_token,
                            "2026-08-20T00:01:00+00:00",
                            "Ship safe dashboard",
                        ]
                    )
                    return ProcessResult(list(argv), 0, record + "\x1e\n", "", 0.01)
                if "--numstat" in argv:
                    return ProcessResult(
                        list(argv),
                        0,
                        "3\t1\tsrc/%s.txt\n" % file_token,
                        "",
                        0.01,
                    )
                if "--name-status" in argv:
                    return ProcessResult(
                        list(argv),
                        0,
                        "R100\tdocs/safe-old.txt\tsrc/%s.txt\n" % file_token,
                        "",
                        0.01,
                    )
                raise AssertionError("unexpected git invocation: %r" % (argv,))

        client = GitClient(runner=Runner(), binary="git")
        commits = client.commits_between(
            Path("/tmp/repo"), "b" * 40, "a" * 40
        )
        diff = client.diff_stats(Path("/tmp/repo"), "b" * 40, "a" * 40)
        patch = _redact_patch(
            "diff --git a/src/%s.txt b/src/%s.txt\n"
            "--- a/src/%s.txt\n"
            "+++ b/src/%s.txt\n"
            "@@ -1 +1 @@\n"
            "+safe setting = true # %s\n"
            % (
                patch_path_token,
                patch_path_token,
                patch_path_token,
                patch_path_token,
                patch_content_token,
            )
        )
        facts = ChangeFacts(
            repository_name="safe-app",
            repository_path="/tmp/safe-app",
            remote_url="https://git.example.com/team/safe-app.git",
            branch="main",
            event_kind="fast_forward",
            old_sha="b" * 40,
            new_sha="a" * 40,
            merge_base="b" * 40,
            detected_at="2026-08-20T00:02:00+00:00",
            commits=commits,
            diff=diff,
            patch_excerpt=patch,
        )

        serialized = json.dumps(facts.to_dict(), ensure_ascii=False)
        for token in tokens:
            self.assertNotIn(token, serialized)
        self.assertEqual(commits[0].subject, "Ship safe dashboard")
        self.assertEqual(commits[0].author_name, "Alice <redacted-provider-token>")
        self.assertEqual(diff.status[0]["status"], "R100")
        self.assertEqual(diff.status[0]["old_path"], "docs/safe-old.txt")
        self.assertIn("src/<redacted-provider-token>.txt", serialized)
        self.assertIn("+safe setting = true", patch)

    def test_patch_capture_uses_bounded_runner(self):
        class Runner:
            def __init__(self):
                self.kwargs = None

            def run_limited(self, argv, **kwargs):
                self.kwargs = kwargs
                return ProcessResult(
                    list(argv),
                    -15,
                    "+safe = True\n",
                    "",
                    0.01,
                    stdout_truncated=True,
                )

        runner = Runner()
        excerpt = GitClient(runner=runner, binary="git").patch_excerpt(
            Path("/tmp/repo"), "a" * 40, "b" * 40, 1024
        )
        self.assertEqual(runner.kwargs["stdout_limit_bytes"], 1024)
        self.assertIn("diff truncated", excerpt)

    def test_fetch_argv_never_contains_remote_credentials(self):
        class Runner:
            def __init__(self):
                self.calls = []
                self.kwargs = []

            def run(self, argv, **kwargs):
                self.calls.append(list(argv))
                self.kwargs.append(kwargs)
                if "get-url" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "https://user:private-token@git.example.com/team/app.git\n",
                        "", 0.01,
                    )
                if "ls-remote" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "a" * 40 + "\trefs/heads/main\n"
                        + "b" * 40 + "\trefs/heads/feature/ui\n",
                        "", 0.01,
                    )
                return ProcessResult(list(argv), 0, "", "", 0.01)

            def run_limited(self, argv, **kwargs):
                return self.run(argv, **kwargs)

        runner = Runner()
        GitClient(runner=runner, binary="git").fetch(Path("/tmp/repo"), "origin")
        fetch_argv = runner.calls[-1]
        self.assertTrue(any("refs/remotes/origin" in item for item in fetch_argv))
        self.assertTrue(any(item.startswith("--refmap=") for item in fetch_argv))
        self.assertIn("--no-recurse-submodules", fetch_argv)
        self.assertIn("--no-auto-maintenance", fetch_argv)
        self.assertNotIn("private-token", " ".join(fetch_argv))
        remote_argument = fetch_argv[fetch_argv.index("--") + 1]
        self.assertTrue(remote_argument.startswith("branch-radar-"))
        self.assertIn(
            "private-token",
            runner.kwargs[-1]["env"]["GIT_CONFIG_VALUE_0"],
        )

        with self.assertRaises(GitError):
            GitClient(runner=runner, binary="git").fetch(
                Path("/tmp/repo"),
                "origin",
                expected_fingerprint="git.example.com/other/app",
            )

        GitClient(runner=runner, binary="git").fetch(
            Path("/tmp/repo"),
            "origin",
            branches=["main", "feature/ui"],
        )
        scoped_argv = runner.calls[-1]
        self.assertIn(
            "+refs/heads/main:refs/remotes/origin/main",
            scoped_argv,
        )
        self.assertIn(
            "+refs/heads/feature/ui:refs/remotes/origin/feature/ui",
            scoped_argv,
        )
        self.assertNotIn(
            "+refs/heads/*:refs/remotes/origin/*",
            scoped_argv[scoped_argv.index("--") + 2 :],
        )
        self.assertNotIn("--prune", scoped_argv)

    def test_fetch_skips_object_fetch_when_selected_tracking_refs_are_current(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run_limited(self, argv, **_kwargs):
                self.calls.append(list(argv))
                if "get-url" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "https://git.example.com/team/app.git\n", "", 0.01,
                    )
                if "ls-remote" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "a" * 40 + "\trefs/heads/main\n"
                        + "b" * 40 + "\trefs/heads/feature/ui\n",
                        "", 0.01,
                    )
                if "for-each-ref" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "refs/remotes/origin/main\x00" + "a" * 40 + "\n"
                        + "refs/remotes/origin/feature/ui\x00" + "b" * 40 + "\n",
                        "", 0.01,
                    )
                raise AssertionError("unexpected git invocation: %r" % (argv,))

        runner = Runner()
        GitClient(runner=runner, binary="git").fetch(
            Path("/tmp/repo"),
            "origin",
            branches=["main", "feature/ui"],
        )

        self.assertFalse(any("fetch" in call for call in runner.calls))
        self.assertTrue(any("ls-remote" in call for call in runner.calls))
        self.assertTrue(any("for-each-ref" in call for call in runner.calls))

    def test_fetch_with_partial_change_fetches_only_the_changed_branch(self):
        class Runner:
            def __init__(self):
                self.calls = []

            def run_limited(self, argv, **_kwargs):
                self.calls.append(list(argv))
                if "get-url" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "https://git.example.com/team/app.git\n", "", 0.01,
                    )
                if "ls-remote" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "a" * 40 + "\trefs/heads/main\n"
                        + "b" * 40 + "\trefs/heads/feature/ui\n",
                        "", 0.01,
                    )
                if "for-each-ref" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "refs/remotes/origin/main\x00" + "a" * 40 + "\n"
                        + "refs/remotes/origin/feature/ui\x00" + "c" * 40 + "\n",
                        "", 0.01,
                    )
                if "fetch" in argv:
                    return ProcessResult(list(argv), 0, "", "", 0.01)
                raise AssertionError("unexpected git invocation: %r" % (argv,))

        runner = Runner()
        GitClient(runner=runner, binary="git").fetch(
            Path("/tmp/repo"),
            "origin",
            branches=["main", "feature/ui"],
        )

        fetch_calls = [call for call in runner.calls if "fetch" in call]
        self.assertEqual(len(fetch_calls), 1)
        fetch_argv = fetch_calls[0]
        self.assertNotIn(
            "+refs/heads/main:refs/remotes/origin/main", fetch_argv
        )
        self.assertIn(
            "+refs/heads/feature/ui:refs/remotes/origin/feature/ui",
            fetch_argv,
        )
        self.assertNotIn("--prune", fetch_argv)
        self.assertNotIn(
            "+refs/heads/*:refs/remotes/origin/*",
            fetch_argv[fetch_argv.index("--") + 2 :],
        )

    def test_fetch_fast_path_deletes_missing_remote_tracking_branch(self):
        class Runner:
            def __init__(self):
                self.calls = []
                self.kwargs = []

            def run_limited(self, argv, **kwargs):
                self.calls.append(list(argv))
                self.kwargs.append(kwargs)
                if "get-url" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "https://git.example.com/team/app.git\n", "", 0.01,
                    )
                if "ls-remote" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "a" * 40 + "\trefs/heads/main\n", "", 0.01,
                    )
                if "for-each-ref" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "refs/remotes/origin/main\x00" + "a" * 40 + "\n",
                        "", 0.01,
                    )
                if "update-ref" in argv:
                    return ProcessResult(list(argv), 0, "", "", 0.01)
                raise AssertionError("unexpected git invocation: %r" % (argv,))

        runner = Runner()
        GitClient(runner=runner, binary="git").fetch(
            Path("/tmp/repo"),
            "origin",
            branches=["main", "retired"],
        )

        self.assertFalse(any("fetch" in call for call in runner.calls))
        update_index = next(
            index
            for index, call in enumerate(runner.calls)
            if "update-ref" in call
        )
        self.assertEqual(
            runner.kwargs[update_index]["stdin"],
            "delete refs/remotes/origin/retired\n",
        )

    def test_failed_fetch_removes_only_new_temporary_pack(self):
        class Runner:
            def __init__(self, pack_directory):
                self.pack_directory = pack_directory

            def run_limited(self, argv, **_kwargs):
                if "get-url" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "https://git.example.com/team/app.git\n", "", 0.01,
                    )
                if "ls-remote" in argv:
                    return ProcessResult(
                        list(argv), 0,
                        "a" * 40 + "\trefs/heads/main\n", "", 0.01,
                    )
                if "for-each-ref" in argv:
                    return ProcessResult(list(argv), 0, "", "", 0.01)
                (self.pack_directory / "tmp_pack_NEW123").write_bytes(b"x" * 4096)
                return ProcessResult(
                    list(argv), 124, "", "", 1.0, timed_out=True,
                )

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            pack_directory = repository / ".git" / "objects" / "pack"
            pack_directory.mkdir(parents=True)
            previous = pack_directory / "tmp_pack_PREVIOUS"
            previous.write_bytes(b"keep")
            with self.assertRaisesRegex(GitError, "已自动清理.*4.00 KiB"):
                GitClient(
                    runner=Runner(pack_directory), binary="git"
                ).fetch(repository, "origin", timeout=1, branches=["main"])
            self.assertTrue(previous.exists())
            self.assertFalse((pack_directory / "tmp_pack_NEW123").exists())


if __name__ == "__main__":
    unittest.main()
