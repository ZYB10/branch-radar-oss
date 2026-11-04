import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from branch_radar.config import CodexSettings
from branch_radar.integrations.codex_cli import (
    CodexCli,
    CodexError,
    _bundled_codex_candidates,
)
from branch_radar.models import ChangeFacts
from branch_radar.paths import AppPaths
from branch_radar.process import ProcessResult


class FakeCodexRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[0].endswith("git") or (len(argv) > 1 and argv[1] == "init"):
            return ProcessResult(list(argv), 0, "", "", 0.01)
        output_index = argv.index("--output-last-message") + 1
        output = Path(argv[output_index])
        output.write_text(json.dumps({
            "title": "新增材料接口",
            "overview": "增加材料查询能力。",
            "categories": ["feature", "api"],
            "highlights": ["新增查询接口"],
            "risks": [{"level": "medium", "area": "API", "reason": "需要联调"}],
            "verification": ["运行接口测试"],
            "deployment": {"impact": "none", "notes": []},
            "confidence": "high",
            "evidence": [{"path": "api/material.py", "why": "接口文件"}],
        }), encoding="utf-8")
        return ProcessResult(
            list(argv), 0,
            '{"type":"turn.completed","usage":{}}\n', "warning only", 0.1,
        )

    def run_limited(self, argv, **kwargs):
        return self.run(argv, **kwargs)


class MissingCodexRunner(FakeCodexRunner):
    def run_limited(self, _argv, **_kwargs):
        raise FileNotFoundError("codex disappeared")


class FakeStatusRunner:
    def __init__(
        self,
        login_stdout="",
        login_stderr="WARNING: PATH aliases unavailable\nLogged in using ChatGPT\n",
        version_returncode=0,
    ):
        self.calls = []
        self.login_stdout = login_stdout
        self.login_stderr = login_stderr
        self.version_returncode = version_returncode

    def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if argv[-1] == "--version":
            return ProcessResult(
                list(argv), self.version_returncode,
                "codex-cli 0.148.0" if self.version_returncode == 0 else "",
                "version probe failed" if self.version_returncode else "", 0.01,
            )
        return ProcessResult(
            list(argv), 0, self.login_stdout, self.login_stderr, 0.01,
        )


class CodexCliTests(unittest.TestCase):
    @staticmethod
    def paths_for(root):
        return AppPaths(
            home=root,
            config=root / "config.json",
            database=root / "state.db",
            logs=root / "logs",
            cache=root / "cache",
            analysis_workspace=root / "analysis",
        )

    def test_detects_bundled_codex_when_background_path_has_no_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "ChatGPT.app" / "Contents" / "Resources" / "codex"
            bundled.parent.mkdir(parents=True)
            bundled.write_text("stub", encoding="utf-8")
            bundled.chmod(0o700)
            with mock.patch(
                "branch_radar.integrations.codex_cli.shutil.which", return_value=None
            ), mock.patch(
                "branch_radar.integrations.codex_cli._bundled_codex_candidates",
                return_value=(bundled,),
            ):
                adapter = CodexCli(
                    CodexSettings(), self.paths_for(root), runner=FakeStatusRunner()
                )
                status = adapter.status()

        self.assertEqual(adapter.binary, str(bundled))
        self.assertTrue(status["detected"])
        self.assertTrue(status["available"])
        self.assertTrue(status["authenticated"])

    def test_macos_candidates_cover_system_and_user_chatgpt_apps(self):
        with mock.patch(
            "branch_radar.integrations.codex_cli.sys.platform", "darwin"
        ), mock.patch(
            "branch_radar.integrations.codex_cli.Path.home",
            return_value=Path("/Users/example"),
        ):
            candidates = _bundled_codex_candidates()

        self.assertEqual(
            candidates,
            (
                Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
                Path("/Users/example/Applications/ChatGPT.app/Contents/Resources/codex"),
            ),
        )

    def test_non_macos_has_no_chatgpt_bundle_candidates(self):
        with mock.patch(
            "branch_radar.integrations.codex_cli.sys.platform", "linux"
        ):
            self.assertEqual(_bundled_codex_candidates(), ())

    def test_explicit_binary_stays_authoritative(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "branch_radar.integrations.codex_cli.shutil.which",
            return_value=sys.executable,
        ) as which:
            root = Path(directory)
            adapter = CodexCli(
                CodexSettings(binary=sys.executable), self.paths_for(root),
                runner=FakeStatusRunner(),
            )

        self.assertEqual(adapter.binary, sys.executable)
        which.assert_called_once_with(sys.executable)

    def test_stale_explicit_binary_returns_a_status_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "branch_radar.integrations.codex_cli._bundled_codex_candidates"
        ) as candidates:
            root = Path(directory)
            runner = FakeStatusRunner()
            status = CodexCli(
                CodexSettings(binary="/definitely/missing/codex"),
                self.paths_for(root), runner=runner,
            ).status()

        self.assertFalse(status["detected"])
        self.assertFalse(status["available"])
        self.assertIn("codex.binary", status["error"])
        self.assertEqual(runner.calls, [])
        candidates.assert_not_called()

    def test_spawn_error_is_a_bounded_unavailable_status(self):
        class RaisingRunner:
            def run(self, _argv, **_kwargs):
                raise FileNotFoundError("disappeared between resolve and spawn")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = CodexCli(
                CodexSettings(binary=sys.executable), self.paths_for(root),
                runner=RaisingRunner(),
            ).status()

        self.assertTrue(status["detected"])
        self.assertFalse(status["available"])
        self.assertNotIn("disappeared", status["error"])

    def test_failed_version_probe_does_not_attempt_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = FakeStatusRunner(version_returncode=1)
            status = CodexCli(
                CodexSettings(binary=sys.executable), self.paths_for(root),
                runner=runner,
            ).status()

        self.assertTrue(status["detected"])
        self.assertFalse(status["available"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(len(runner.calls), 1)

    def test_non_executable_bundle_is_not_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root / "codex"
            bundled.write_text("stub", encoding="utf-8")
            bundled.chmod(0o600)
            with mock.patch(
                "branch_radar.integrations.codex_cli.shutil.which", return_value=None
            ), mock.patch(
                "branch_radar.integrations.codex_cli._bundled_codex_candidates",
                return_value=(bundled,),
            ):
                status = CodexCli(
                    CodexSettings(), self.paths_for(root), runner=FakeStatusRunner()
                ).status()

        self.assertFalse(status["detected"])
        self.assertFalse(status["available"])

    def test_status_normalizes_chatgpt_login_from_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = AppPaths(
                home=root,
                config=root / "config.json",
                database=root / "state.db",
                logs=root / "logs",
                cache=root / "cache",
                analysis_workspace=root / "analysis",
            )
            runner = FakeStatusRunner()
            status = CodexCli(
                CodexSettings(binary=sys.executable), paths, runner=runner
            ).status()

        self.assertTrue(status["authenticated"])
        self.assertTrue(status["detected"])
        self.assertEqual(status["auth_mode"], "chatgpt")
        self.assertNotIn("login", status)
        self.assertEqual(runner.calls[1][0][-2:], ["login", "status"])

    def test_status_auth_mode_requires_one_exact_known_status_line(self):
        cases = (
            ("Logged in using an API key\n", "", "api_key"),
            ("custom provider session\n", "", "authenticated"),
            (
                "Logged in using an API key\n",
                "WARNING: ChatGPT auth unavailable\n",
                "api_key",
            ),
            (
                "Logged in using ChatGPT\n",
                "Logged in using an API key\n",
                "authenticated",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = AppPaths(
                home=root,
                config=root / "config.json",
                database=root / "state.db",
                logs=root / "logs",
                cache=root / "cache",
                analysis_workspace=root / "analysis",
            )
            for stdout, stderr, expected in cases:
                with self.subTest(expected=expected, stdout=stdout, stderr=stderr):
                    status = CodexCli(
                        CodexSettings(binary=sys.executable), paths,
                        runner=FakeStatusRunner(stdout, stderr),
                    ).status()
                    self.assertEqual(status["auth_mode"], expected)

    def test_structured_read_only_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = AppPaths(
                home=root,
                config=root / "config.json",
                database=root / "state.db",
                logs=root / "logs",
                cache=root / "cache",
                analysis_workspace=root / "analysis",
            )
            paths.ensure()
            (paths.analysis_workspace / ".git").mkdir()
            runner = FakeCodexRunner()
            adapter = CodexCli(
                CodexSettings(binary=sys.executable, timeout_seconds=10),
                paths,
                runner=runner,
            )
            facts = ChangeFacts(
                repository_name="app",
                repository_path="/tmp/app",
                remote_url=None,
                branch="main",
                event_kind="fast_forward",
                old_sha="a" * 40,
                new_sha="b" * 40,
                merge_base="a" * 40,
                detected_at="now",
            )
            summary = adapter.summarize(facts)
            self.assertEqual(summary.source, "codex")
            argv, kwargs = runner.calls[-1]
            self.assertIn("--ephemeral", argv)
            self.assertIn("--ignore-user-config", argv)
            self.assertIn("--ignore-rules", argv)
            self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")
            self.assertEqual(argv[-1], "-")
            self.assertIn("untrusted data", kwargs["stdin"])
            self.assertIn("Do not call tools", kwargs["stdin"])
            self.assertNotIn("/tmp/app", kwargs["stdin"])
            self.assertEqual(kwargs["stdout_limit_bytes"], 2_000_000)
            self.assertTrue(kwargs["replace_env"])
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["env"])

    def test_summarize_wraps_spawn_errors_without_exposing_process_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.paths_for(root)
            paths.ensure()
            (paths.analysis_workspace / ".git").mkdir()
            adapter = CodexCli(
                CodexSettings(binary=sys.executable), paths,
                runner=MissingCodexRunner(),
            )
            facts = ChangeFacts(
                repository_name="app",
                repository_path="/tmp/app",
                remote_url=None,
                branch="main",
                event_kind="fast_forward",
                old_sha="a" * 40,
                new_sha="b" * 40,
                merge_base="a" * 40,
                detected_at="now",
            )

            with self.assertRaisesRegex(CodexError, "无法启动") as raised:
                adapter.summarize(facts)

        self.assertNotIn("disappeared", str(raised.exception))

    def test_check_run_summary_rebuilds_a_strict_diff_free_bundle(self):
        class BatchRunner(FakeCodexRunner):
            def run(self, argv, **kwargs):
                self.calls.append((list(argv), kwargs))
                if argv[0].endswith("git") or (len(argv) > 1 and argv[1] == "init"):
                    return ProcessResult(list(argv), 0, "", "", 0.01)
                output = Path(argv[argv.index("--output-last-message") + 1])
                output.write_text(json.dumps({
                    "title": "跨仓库接口变更 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
                    "overview": "两个仓库同时调整 API。",
                    "themes": ["API"],
                    "priorities": ["先检查后端"],
                    "connections": ["共享接口契约"],
                    "completeness_note": "",
                }), encoding="utf-8")
                return ProcessResult(
                    list(argv), 0, '{"type":"turn.completed"}\n', "", 0.1
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.paths_for(root)
            paths.ensure()
            (paths.analysis_workspace / ".git").mkdir()
            runner = BatchRunner()
            adapter = CodexCli(
                CodexSettings(binary=sys.executable), paths, runner=runner
            )
            dangerous = {
                "event_id": 1,
                "repository": "backend",
                "branch": "main",
                "kind": "fast_forward",
                "analysis_complete": True,
                "summary": {
                    "title": "API update",
                    "overview": "Changes endpoint",
                    "categories": ["api"],
                    "signals": ["提交说明包含 breaking change 信号"],
                },
                "stats": {"commits": 2, "files": 3, "insertions": 4, "deletions": 1},
                "diff": "RAW-DIFF-MUST-NOT-LEAVE",
                "files": ["PRIVATE-FILE-MUST-NOT-LEAVE"],
                "commits": ["PRIVATE-COMMIT-MUST-NOT-LEAVE"],
                "patch_excerpt": "PRIVATE-PATCH-MUST-NOT-LEAVE",
                "remote_url": "PRIVATE-REMOTE-MUST-NOT-LEAVE",
                "repository_path": "PRIVATE-PATH-MUST-NOT-LEAVE",
                "email": "PRIVATE-EMAIL-MUST-NOT-LEAVE",
            }
            summary = adapter.summarize_check_run(
                [dangerous, dict(dangerous, event_id=2, repository="frontend")],
                completeness_note="",
            )

        stdin = runner.calls[-1][1]["stdin"]
        for private in (
            "RAW-DIFF-MUST-NOT-LEAVE",
            "PRIVATE-FILE-MUST-NOT-LEAVE",
            "PRIVATE-COMMIT-MUST-NOT-LEAVE",
            "PRIVATE-PATCH-MUST-NOT-LEAVE",
            "PRIVATE-REMOTE-MUST-NOT-LEAVE",
            "PRIVATE-PATH-MUST-NOT-LEAVE",
            "PRIVATE-EMAIL-MUST-NOT-LEAVE",
        ):
            self.assertNotIn(private, stdin)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456", summary["title"])
        self.assertIn("<redacted", summary["title"])


if __name__ == "__main__":
    unittest.main()
