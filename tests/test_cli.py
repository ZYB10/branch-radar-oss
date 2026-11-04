import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from branch_radar.cli import Application, _daemon_retry_delay, build_parser
from branch_radar.locking import exclusive_lock
from branch_radar.models import RepositoryCandidate


class CliExitSemanticsTests(unittest.TestCase):
    def test_discover_without_positional_paths_uses_configured_roots(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            root = Path(directory) / "projects"
            root.mkdir()
            args = build_parser().parse_args(["discover", "--json"])
            app = Application(args)
            app.config.scan_roots = [str(root)]
            scanner = mock.Mock()
            scanner.scan.return_value = []
            output = io.StringIO()

            with mock.patch(
                "branch_radar.cli.RepositoryScanner", return_value=scanner
            ) as scanner_type, redirect_stdout(output):
                code = app.command_discover()

            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"repositories": []})
            scanner_type.assert_called_once_with(app.git)
            scanner.scan.assert_called_once_with(
                [root], max_depth=app.config.scan_max_depth
            )

    def test_discover_preserves_a_positional_root_with_trailing_space(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            plain = Path(directory) / "projects"
            spaced = Path(directory) / "projects "
            plain.mkdir()
            spaced.mkdir()
            args = build_parser().parse_args(["discover", str(spaced), "--json"])
            app = Application(args)
            scanner = mock.Mock()
            scanner.scan.return_value = []
            output = io.StringIO()

            with mock.patch(
                "branch_radar.cli.RepositoryScanner", return_value=scanner
            ), redirect_stdout(output):
                code = app.command_discover()

            self.assertEqual(code, 0)
            scanner.scan.assert_called_once_with(
                [spaced], max_depth=app.config.scan_max_depth
            )
            self.assertNotEqual(spaced, plain)

    def test_init_preserves_a_root_argument_with_trailing_space(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            plain = Path(directory) / "projects"
            spaced = Path(directory) / "projects "
            plain.mkdir()
            spaced.mkdir()
            args = build_parser().parse_args([
                "init", "--root", str(spaced), "--all", "--non-interactive",
                "--no-codex", "--no-lark", "--json",
            ])
            app = Application(args)
            scanner = mock.Mock()
            scanner.scan.return_value = []
            output = io.StringIO()

            with mock.patch(
                "branch_radar.cli.RepositoryScanner", return_value=scanner
            ), redirect_stdout(output):
                code = app.command_init()

            self.assertEqual(code, 1)
            scanner.scan.assert_called_once_with(
                [spaced], max_depth=app.config.scan_max_depth
            )
            self.assertNotEqual(spaced, plain)

    def test_discover_rejects_invalid_roots_before_creating_scanner(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args(["discover", "--json"])
            app = Application(args)
            invalid_values = {
                "filesystem root": [Path(directory).anchor],
                "missing path": [str(Path(directory) / "missing")],
                "too many roots": [directory] * 17,
                "path too long": [directory + ("x" * 4096)],
                "whitespace only": ["   "],
            }

            for label, roots in invalid_values.items():
                app.args.paths = roots
                output = io.StringIO()
                with self.subTest(label=label), mock.patch(
                    "branch_radar.cli.RepositoryScanner"
                ) as scanner_type, redirect_stdout(output):
                    code = app.command_discover()
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(output.getvalue())["ok"])
                scanner_type.assert_not_called()

    def test_init_rejects_invalid_roots_before_creating_scanner(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            invalid_values = {
                "filesystem root": [Path(directory).anchor],
                "missing path": [str(Path(directory) / "missing")],
                "too many roots": [directory] * 17,
                "path too long": [directory + ("x" * 4096)],
                "whitespace only": ["   "],
            }

            for label, roots in invalid_values.items():
                argv = [
                    "init", "--all", "--non-interactive", "--no-codex",
                    "--no-lark", "--json",
                ]
                for root in roots:
                    argv.extend(["--root", root])
                app = Application(build_parser().parse_args(argv))
                output = io.StringIO()
                with self.subTest(label=label), mock.patch(
                    "branch_radar.cli.RepositoryScanner"
                ) as scanner_type, redirect_stdout(output):
                    code = app.command_init()
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(output.getvalue())["ok"])
                scanner_type.assert_not_called()

    def test_noninteractive_init_fails_when_every_repository_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "init", "--root", directory, "--all", "--non-interactive",
                "--no-codex", "--no-lark", "--json",
            ])
            app = Application(args)
            app.discover = lambda _roots: [RepositoryCandidate(
                name="unsafe",
                path=str(Path(directory) / "unsafe"),
                remote_name=None,
                remote_url=None,
                canonical_remote=None,
                current_branch=None,
                upstream_branch=None,
                default_branch=None,
                error="不支持的 remote",
            )]
            output = io.StringIO()
            with redirect_stdout(output):
                code = app.command_init()
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(len(payload["skipped"]), 1)

    def test_noninteractive_init_requires_an_explicit_scan_root(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "init", "--all", "--non-interactive", "--no-codex", "--no-lark",
                "--json",
            ])
            app = Application(args)
            app.discover = mock.Mock(
                side_effect=AssertionError("must not scan an implicit directory")
            )
            output = io.StringIO()

            with redirect_stdout(output):
                code = app.command_init()

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("--root", payload["error"])
            app.discover.assert_not_called()

    def test_daemon_publish_requires_document_before_loop(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "daemon", "--once", "--publish", "--json",
            ])
            app = Application(args)
            output = io.StringIO()
            with redirect_stdout(output):
                code = app.command_daemon()
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_daemon_once_contains_unexpected_database_error_and_records_health(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args(["daemon", "--once", "--json"])
            app = Application(args)
            service = mock.Mock()
            service.check.side_effect = sqlite3.OperationalError("database busy")
            output = io.StringIO()
            with mock.patch("branch_radar.cli.RadarService", return_value=service):
                with redirect_stdout(output):
                    code = app.command_daemon()

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["daemon_health"]["consecutive_failures"], 1)
            self.assertIn("database busy", payload["publications"][0]["error"])

    def test_daemon_retry_delay_never_shortens_base_interval(self):
        self.assertEqual(_daemon_retry_delay(900, 1), 900)
        self.assertEqual(_daemon_retry_delay(900, 2), 1800)
        self.assertEqual(_daemon_retry_delay(900, 8), 3600)
        self.assertEqual(_daemon_retry_delay(7200, 5), 7200)

    def test_daemon_json_survives_health_read_failure(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args(["daemon", "--once", "--json"])
            app = Application(args)
            service = mock.Mock()
            service.check.return_value = ([], [])
            service.config = app.config
            output = io.StringIO()
            with mock.patch("branch_radar.cli.RadarService", return_value=service):
                with mock.patch.object(
                    app.database,
                    "daemon_health",
                    side_effect=sqlite3.OperationalError("health unavailable"),
                ):
                    with redirect_stdout(output):
                        code = app.command_daemon()

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["daemon_health"]["consecutive_failures"], 0)

    def test_watch_rebaseline_is_an_explicit_flag(self):
        args = build_parser().parse_args([
            "watch", "add", "/tmp/repo", "--rebaseline",
        ])
        self.assertTrue(args.rebaseline)

    def test_watch_remove_soft_deletes_by_list_id(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "watch", "remove", "1", "--json",
            ])
            app = Application(args)
            candidate = RepositoryCandidate(
                name="demo",
                path=str(Path(directory) / "repo"),
                remote_name=None,
                remote_url=None,
                canonical_remote=None,
                current_branch="main",
                upstream_branch=None,
                default_branch="main",
            )
            repository_id = app.database.upsert_repository(candidate)
            branch_id = app.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            app.args.watch_id = branch_id
            output = io.StringIO()

            with redirect_stdout(output):
                code = app.command_watch_remove()

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["removed"]["branch_id"], branch_id)
            self.assertEqual(app.database.list_watches(), [])

    def test_watch_remove_help_explains_preserved_history(self):
        parser = build_parser()
        args = parser.parse_args(["watch", "remove", "7"])
        self.assertEqual(args.handler, "command_watch_remove")
        self.assertEqual(args.watch_id, 7)

    def test_prune_requires_explicit_cutoff_and_supports_preview(self):
        args = build_parser().parse_args([
            "prune", "--before", "90d", "--dry-run", "--json",
        ])
        self.assertEqual(args.handler, "command_prune")
        self.assertEqual(args.before, "90d")
        self.assertTrue(args.dry_run)
        self.assertFalse(args.vacuum)

    def test_prune_output_includes_check_runs_and_broader_protection_reason(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "prune", "--before", "90d", "--dry-run",
            ])
            app = Application(args)
            output = io.StringIO()
            app.console.stream = output
            with mock.patch.object(app.database, "prune", return_value={
                "dry_run": True,
                "events": 2,
                "check_runs": 3,
                "deliveries": 1,
                "external_requests": 4,
                "protected_events": 5,
                "vacuumed": False,
            }):
                code = app.command_prune()
        self.assertEqual(code, 0)
        self.assertIn("3 个检查批次", output.getvalue())
        self.assertIn("待确认批次或待处理投递", output.getvalue())

    def test_watch_add_respects_shared_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "watch", "add", str(Path(directory) / "repo"), "--json",
            ])
            app = Application(args)
            output = io.StringIO()
            with exclusive_lock(app.paths.home / "operation.lock"):
                with redirect_stdout(output):
                    code = app.command_watch_add()
            self.assertEqual(code, 1)
            self.assertIn("另一个 Branch Radar", json.loads(output.getvalue())["error"])

    def test_init_respects_shared_operation_lock_before_writing_watches(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"BRANCH_RADAR_HOME": directory}
        ):
            args = build_parser().parse_args([
                "init", "--root", directory, "--all", "--non-interactive",
                "--no-codex", "--no-lark", "--json",
            ])
            app = Application(args)
            app.discover = lambda _roots: [RepositoryCandidate(
                name="app",
                path=str(Path(directory) / "repo"),
                remote_name="origin",
                remote_url="https://git.example.com/team/app.git",
                canonical_remote="git.example.com/team/app",
                current_branch="main",
                upstream_branch="main",
                default_branch="main",
            )]
            output = io.StringIO()
            with exclusive_lock(app.paths.home / "operation.lock"):
                with redirect_stdout(output):
                    code = app.command_init()
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("另一个 Branch Radar", payload["skipped"][0]["error"])


if __name__ == "__main__":
    unittest.main()
