import errno
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from branch_radar.git import GitCloneCancelled, GitError
from branch_radar.integrations.lark_cli import LarkError
from branch_radar.locking import exclusive_lock
from branch_radar.models import ChangeFacts, DiffStats, RepositoryCandidate
from branch_radar.services import (
    RadarService,
    ServiceBusy,
    ServiceError,
    _atomic_rename_no_replace,
    _clone_progress_reporter,
    _CloneRootGuard,
    _deterministic_check_run_summary,
    _validate_clone_directory_name,
)
from branch_radar.web.views import check_run_view


class _FakeLark:
    def __init__(self, result=None, error=None):
        self.result = result or {
            "url": "https://example.feishu.invalid/docx/created",
            "document_id": "created",
        }
        self.error = error
        self.calls = 0

    def create_document(self, _title):
        self.calls += 1
        if self.error:
            raise self.error
        return dict(self.result)


class LarkCreateIdempotencyTests(unittest.TestCase):
    def create_service(self, directory):
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_HOME": directory}):
            return RadarService.create()

    def test_success_is_persisted_and_same_request_does_not_create_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            fake = _FakeLark()
            with mock.patch.object(service, "lark", return_value=fake):
                first = service.create_lark_document(
                    "项目动态", request_id="request-success-001"
                )
                second = service.create_lark_document(
                    "项目动态", request_id="request-success-001"
                )
            self.assertEqual(first, second)
            self.assertEqual(fake.calls, 1)

            restarted = self.create_service(directory)
            never_called = _FakeLark(error=AssertionError("must not call Lark again"))
            with mock.patch.object(restarted, "lark", return_value=never_called):
                recovered = restarted.create_lark_document(
                    "项目动态", request_id="request-success-001"
                )
            self.assertEqual(recovered, first)
            self.assertEqual(never_called.calls, 0)


class RetentionTests(unittest.TestCase):
    def create_service(self, directory):
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_HOME": directory}):
            return RadarService.create()

    def test_retention_runs_once_a_day_and_can_be_switched_off(self):
        from datetime import timedelta

        from branch_radar.services import RETENTION_CHECK_INTERVAL

        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            self.assertEqual(service.config.retention_days, 180)

            first = service.enforce_retention()
            self.assertTrue(first["ran"])
            self.assertEqual(first["retention_days"], 180)
            pruned_at = service.database.daemon_health()["last_prune_at"]
            self.assertIsNotNone(pruned_at)

            # Same day: housekeeping does not repeat on every polling cycle.
            second = service.enforce_retention()
            self.assertFalse(second["ran"])
            self.assertEqual(second["reason"], "not_due")

            due = datetime.now(timezone.utc) + RETENTION_CHECK_INTERVAL + timedelta(minutes=1)
            third = service.enforce_retention(now=due)
            self.assertTrue(third["ran"])

            service.update_preferences(retention_days=0)
            disabled = service.enforce_retention(now=due + timedelta(days=2))
            self.assertFalse(disabled["ran"])
            self.assertEqual(disabled["reason"], "disabled")

    def test_retention_yields_to_a_running_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            with exclusive_lock(service.paths.home / "operation.lock"):
                result = service.enforce_retention()
            # Housekeeping must never take the lock away from a real check.
            self.assertFalse(result["ran"])
            self.assertEqual(result["reason"], "busy")
            self.assertIsNone(service.database.daemon_health()["last_prune_at"])


class RadarServiceTests(unittest.TestCase):
    def test_clone_progress_reports_partial_clone_checkout_stage(self):
        updates = []
        receive = _clone_progress_reporter(updates.append)

        receive("stderr", "Receiving objects: 100% (80/80), done.\n")
        receive("stderr", "Updating files: 42% (42/100)\r")

        self.assertEqual(updates[-1]["phase"], "verify")
        self.assertEqual(updates[-1]["label"], "正在生成工作区")
        self.assertEqual(updates[-1]["current"], 42)
        self.assertEqual(updates[-1]["total"], 100)
        self.assertEqual(updates[-1]["percent"], 42)
        self.assertFalse(updates[-1]["indeterminate"])

    def test_deterministic_run_summary_orders_high_risk_events_first(self):
        events = [
            {
                "id": 1,
                "repository": "api",
                "branch": "main",
                "kind": "fast_forward",
                "facts": {
                    "analysis_complete": True,
                    "categories": ["api"],
                    "signals": [],
                },
                "summary": {"title": "更新接口"},
            },
            {
                "id": 2,
                "repository": "kernel",
                "branch": "main",
                "kind": "force_push",
                "facts": {
                    "analysis_complete": False,
                    "categories": ["docs"],
                    "signals": [],
                },
                "summary": {"title": "历史被重写"},
            },
            {
                "id": 3,
                "repository": "storage",
                "branch": "main",
                "kind": "fast_forward",
                "facts": {
                    "analysis_complete": True,
                    "categories": ["database"],
                    "signals": [],
                },
                "summary": {"title": "迁移数据结构"},
            },
            {
                "id": 4,
                "repository": "deploy",
                "branch": "main",
                "kind": "fast_forward",
                "facts": {
                    "analysis_complete": True,
                    "categories": ["deployment"],
                    "signals": [],
                },
                "summary": {"title": "调整发布流程"},
            },
        ]
        summary = _deterministic_check_run_summary([], events)
        self.assertTrue(summary["priorities"][0].startswith("kernel / main"))
        self.assertTrue(summary["priorities"][1].startswith("storage / main"))
        self.assertTrue(summary["priorities"][2].startswith("deploy / main"))
        self.assertEqual(summary["themes"][:3], ["数据库", "部署", "API"])
        self.assertIn("无法完整重建", summary["completeness_note"])

    def create_service(self, directory):
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_HOME": directory}):
            return RadarService.create()

    @staticmethod
    def candidate(path: Path, *, branch: str = "main") -> RepositoryCandidate:
        return RepositoryCandidate(
            name="same-name",
            path=str(path),
            remote_name=None,
            remote_url=None,
            canonical_remote=None,
            current_branch=branch,
            upstream_branch=None,
            default_branch=branch,
            branches=[branch],
        )

    def test_discovery_matches_watches_by_exact_local_path_not_repository_name(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            first = self.candidate(Path(directory) / "first")
            second = self.candidate(Path(directory) / "second")
            repository_id = service.database.upsert_repository(first)
            service.database.add_branch(
                repository_id,
                "main",
                None,
                baseline_oid="a" * 40,
            )

            with mock.patch(
                "branch_radar.services.RepositoryScanner.scan",
                return_value=[first, second],
            ):
                discovered = service.discover([directory])

            self.assertEqual(discovered[0]["repository_id"], repository_id)
            self.assertEqual(discovered[0]["watched_branches"], ["main"])
            self.assertIsNone(discovered[1]["repository_id"])
            self.assertEqual(discovered[1]["watched_branches"], [])

    def test_discovery_without_configured_or_explicit_roots_is_a_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            config = service.store.load()
            config.scan_roots = []
            service.store.save(config)

            with mock.patch("branch_radar.services.RepositoryScanner") as scanner:
                self.assertEqual(service.discover(), [])

            scanner.assert_not_called()

    def test_discovery_validates_explicit_and_legacy_configured_roots_first(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            filesystem_root = Path(directory).anchor

            with mock.patch("branch_radar.services.RepositoryScanner") as scanner:
                with self.assertRaises(ServiceError) as explicit_error:
                    service.discover([filesystem_root])
            self.assertEqual(explicit_error.exception.code, "invalid_scan_roots")
            scanner.assert_not_called()

            config = service.store.load()
            config.scan_roots = [filesystem_root]
            service.store.save(config)
            with mock.patch("branch_radar.services.RepositoryScanner") as scanner:
                with self.assertRaises(ServiceError) as configured_error:
                    service.discover()
            self.assertEqual(configured_error.exception.code, "invalid_scan_roots")
            scanner.assert_not_called()

    def test_discovery_keeps_valid_legacy_absolute_config_roots_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            config = service.store.load()
            config.scan_roots = [directory]
            service.store.save(config)
            scanner = mock.Mock()
            scanner.scan.return_value = []

            with mock.patch(
                "branch_radar.services.RepositoryScanner", return_value=scanner
            ) as scanner_type:
                self.assertEqual(service.discover(), [])

            scanner_type.assert_called_once_with(service.git)
            scanner.scan.assert_called_once_with(
                [Path(directory)], max_depth=config.scan_max_depth
            )

    def test_scan_root_with_trailing_space_is_preserved_and_scanned_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user_home = temporary / "home"
            app_home = user_home / ".branch-radar"
            plain = user_home / "projects"
            spaced = user_home / "projects "
            plain.mkdir(parents=True)
            spaced.mkdir()

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(user_home),
                    "BRANCH_RADAR_HOME": str(app_home),
                },
            ):
                service = RadarService.create()
                updated = service.update_preferences(scan_roots=[str(spaced)])
                scanner = mock.Mock()
                scanner.scan.return_value = []

                with mock.patch(
                    "branch_radar.services.RepositoryScanner", return_value=scanner
                ):
                    self.assertEqual(service.discover(), [])

                self.assertEqual(updated.scan_roots, ["~/projects "])
                self.assertEqual(service.store.load().scan_roots, ["~/projects "])
                scanner.scan.assert_called_once_with(
                    [spaced], max_depth=updated.scan_max_depth
                )
                self.assertNotEqual(spaced, plain)

    def test_scan_root_preferences_are_deduplicated_and_portable(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user_home = temporary / "home"
            app_home = user_home / ".branch-radar"
            project = user_home / "projects" / "demo"
            external = temporary / "shared-repositories"
            project.mkdir(parents=True)
            external.mkdir()

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(user_home),
                    "BRANCH_RADAR_HOME": str(app_home),
                },
            ):
                service = RadarService.create()
                updated = service.update_preferences(
                    scan_roots=[
                        str(project),
                        "~/projects/demo",
                        str(external),
                        str(external),
                    ]
                )

                expected = ["~/projects/demo", str(external)]
                self.assertEqual(updated.scan_roots, expected)
                self.assertEqual(service.store.load().scan_roots, expected)

    def test_scan_root_preferences_reject_unsafe_or_invalid_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            user_home = temporary / "home"
            app_home = user_home / ".branch-radar"
            valid = user_home / "projects"
            valid.mkdir(parents=True)
            regular_file = user_home / "not-a-directory.txt"
            regular_file.write_text("not a repository root", encoding="utf-8")
            too_many = []
            for index in range(17):
                item = user_home / ("root-%02d" % index)
                item.mkdir()
                too_many.append(str(item))

            invalid_values = {
                "more than sixteen roots": too_many,
                "path longer than limit": [str(valid) + ("x" * 4096)],
                "missing path": [str(user_home / "missing")],
                "regular file": [str(regular_file)],
                "filesystem root": [Path(temporary.anchor).as_posix()],
                "whitespace only": ["   "],
            }
            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(user_home),
                    "BRANCH_RADAR_HOME": str(app_home),
                },
            ):
                service = RadarService.create()
                for label, roots in invalid_values.items():
                    with self.subTest(label=label), self.assertRaises(
                        ServiceError
                    ) as raised:
                        service.update_preferences(scan_roots=roots)
                    self.assertEqual(raised.exception.code, "invalid_scan_roots")

                self.assertEqual(service.store.load().scan_roots, [])

    def test_dashboard_uses_health_from_database_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            health = {
                "last_attempt_at": "2026-08-20T10:00:00+00:00",
                "last_success_at": None,
                "last_failure_at": "2026-08-20T10:00:00+00:00",
                "consecutive_failures": 1,
                "last_error": "remote unavailable",
                "last_prune_at": None,
            }
            snapshot = {
                "counts": {
                    "repositories": 0,
                    "branches": 0,
                    "branch_errors": 0,
                    "events": 0,
                    "pending_deliveries": 0,
                },
                "daemon_health": health,
                "watches": [],
                "events": [],
            }
            with mock.patch.object(
                service.database, "dashboard_snapshot", return_value=snapshot
            ), mock.patch.object(
                service.database,
                "daemon_health",
                side_effect=AssertionError("dashboard must not open a second connection"),
            ):
                dashboard = service.dashboard()

            self.assertIs(dashboard["daemon_health"], health)

    def test_monitoring_preferences_install_a_scheduled_service_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            manager = mock.Mock()
            manager.status.return_value = {
                "platform": "launchd",
                "installed": True,
                "loaded": True,
            }
            notifier = mock.Mock()
            notifier.status.return_value = {
                "supported": True,
                "provider": "osascript",
            }
            with mock.patch(
                "branch_radar.services.build_manager", return_value=manager
            ), mock.patch(
                "branch_radar.services.DesktopNotifier", return_value=notifier
            ):
                status = service.configure_monitoring(
                    enabled=True,
                    interval_seconds=300,
                    notifications_enabled=True,
                    quiet_hours_enabled=True,
                    quiet_hours_start="22:30",
                    quiet_hours_end="07:30",
                    locale="en",
                )

            manager.install.assert_called_once_with()
            manager.uninstall.assert_not_called()
            stored = service.store.load()
            self.assertEqual(stored.poll_interval_seconds, 300)
            self.assertTrue(stored.notifications.enabled)
            self.assertTrue(stored.notifications.quiet_hours_enabled)
            self.assertEqual(stored.notifications.locale, "en")
            self.assertTrue(status["enabled"])
            self.assertTrue(status["notifications_supported"])
            self.assertIsNotNone(status["next_run_at"])

    def test_monitoring_disable_uninstalls_without_erasing_preferences(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            manager = mock.Mock()
            manager.status.return_value = {
                "platform": "systemd",
                "installed": False,
                "loaded": False,
            }
            notifier = mock.Mock()
            notifier.status.return_value = {
                "supported": True,
                "provider": "notify-send",
            }
            with mock.patch(
                "branch_radar.services.build_manager", return_value=manager
            ), mock.patch(
                "branch_radar.services.DesktopNotifier", return_value=notifier
            ):
                status = service.configure_monitoring(
                    enabled=False,
                    interval_seconds=1800,
                    notifications_enabled=True,
                    quiet_hours_enabled=False,
                    quiet_hours_start="22:00",
                    quiet_hours_end="08:00",
                    locale="zh-CN",
                )

            manager.uninstall.assert_called_once_with()
            manager.install.assert_not_called()
            self.assertFalse(status["enabled"])
            self.assertIsNone(status["next_run_at"])
            stored = service.store.load()
            self.assertEqual(stored.poll_interval_seconds, 1800)
            self.assertTrue(stored.notifications.enabled)

    def test_monitoring_failed_new_install_restores_config_and_cleans_service(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            previous = service.store.load()
            previous_payload = previous.to_dict()
            previous_manager = mock.Mock()
            previous_manager.status.return_value = {
                "platform": "launchd",
                "installed": False,
                "loaded": False,
            }
            attempted_manager = mock.Mock()
            attempted_manager.install.side_effect = OSError("bootstrap failed")

            with mock.patch(
                "branch_radar.services.build_manager",
                side_effect=[previous_manager, attempted_manager],
            ):
                with self.assertRaises(ServiceError) as raised:
                    service.configure_monitoring(
                        enabled=True,
                        interval_seconds=300,
                        notifications_enabled=True,
                        quiet_hours_enabled=True,
                        quiet_hours_start="23:00",
                        quiet_hours_end="06:30",
                        locale="en",
                    )

            self.assertEqual(raised.exception.code, "monitoring_service_failed")
            attempted_manager.uninstall.assert_called_once_with()
            previous_manager.install.assert_not_called()
            self.assertEqual(service.store.load().to_dict(), previous_payload)
            self.assertEqual(service.config.to_dict(), previous_payload)
            self.assertEqual(service._config_signature, service.store.signature())

    def test_monitoring_failed_update_reinstalls_existing_service_with_old_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            previous_payload = service.store.load().to_dict()
            previous_manager = mock.Mock()
            previous_manager.status.return_value = {
                "platform": "systemd",
                "installed": True,
                "loaded": True,
            }
            attempted_manager = mock.Mock()
            attempted_manager.install.side_effect = OSError("restart failed")

            with mock.patch(
                "branch_radar.services.build_manager",
                side_effect=[previous_manager, attempted_manager],
            ) as manager_factory:
                with self.assertRaises(ServiceError):
                    service.configure_monitoring(
                        enabled=True,
                        interval_seconds=3600,
                        notifications_enabled=True,
                        quiet_hours_enabled=False,
                        quiet_hours_start="22:00",
                        quiet_hours_end="08:00",
                        locale="en",
                    )

            old_spec = manager_factory.call_args_list[0].args[1]
            attempted_spec = manager_factory.call_args_list[1].args[1]
            self.assertEqual(old_spec.interval, 900)
            self.assertEqual(attempted_spec.interval, 3600)
            previous_manager.install.assert_called_once_with()
            attempted_manager.uninstall.assert_not_called()
            self.assertEqual(service.store.load().to_dict(), previous_payload)
            self.assertEqual(service.config.to_dict(), previous_payload)
            self.assertEqual(service._config_signature, service.store.signature())

    def test_monitoring_failed_uninstall_restores_existing_service_and_config(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            previous_payload = service.store.load().to_dict()
            previous_manager = mock.Mock()
            previous_manager.status.return_value = {
                "platform": "launchd",
                "installed": True,
                "loaded": True,
            }
            attempted_manager = mock.Mock()
            attempted_manager.uninstall.side_effect = OSError("bootout failed")

            with mock.patch(
                "branch_radar.services.build_manager",
                side_effect=[previous_manager, attempted_manager],
            ):
                with self.assertRaises(ServiceError) as raised:
                    service.configure_monitoring(
                        enabled=False,
                        interval_seconds=1800,
                        notifications_enabled=True,
                        quiet_hours_enabled=True,
                        quiet_hours_start="21:30",
                        quiet_hours_end="07:00",
                        locale="en",
                    )

            self.assertEqual(raised.exception.code, "monitoring_service_failed")
            previous_manager.install.assert_called_once_with()
            self.assertEqual(service.store.load().to_dict(), previous_payload)
            self.assertEqual(service.config.to_dict(), previous_payload)
            self.assertEqual(service._config_signature, service.store.signature())

    def test_monitoring_stale_heartbeat_estimates_a_future_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            manager = mock.Mock()
            manager.status.return_value = {
                "platform": "launchd",
                "installed": True,
                "loaded": True,
            }
            notifier = mock.Mock()
            notifier.status.return_value = {
                "supported": True,
                "provider": "osascript",
            }
            with mock.patch.object(
                service.database,
                "daemon_health",
                return_value={
                    "last_attempt_at": "2020-01-01T00:00:00+00:00",
                    "last_success_at": "2020-01-01T00:00:00+00:00",
                },
            ), mock.patch(
                "branch_radar.services.build_manager", return_value=manager
            ), mock.patch(
                "branch_radar.services.DesktopNotifier", return_value=notifier
            ):
                status = service.monitoring_status()

            next_run = datetime.fromisoformat(status["next_run_at"])
            self.assertGreater(next_run, datetime.now(timezone.utc))

    def test_monitoring_rejects_unlisted_intervals_and_invalid_quiet_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            with self.assertRaises(ServiceError) as interval_error:
                service.update_preferences(poll_interval_seconds=60)
            self.assertEqual(
                interval_error.exception.code, "invalid_monitoring_interval"
            )
            with self.assertRaises(ServiceError) as quiet_error:
                service.update_preferences(quiet_hours_start="24:00")
            self.assertEqual(quiet_error.exception.code, "invalid_quiet_hours")

    def test_monitoring_update_uses_the_shared_external_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            with exclusive_lock(service.paths.home / "operation.lock"):
                with self.assertRaises(ServiceBusy):
                    service.configure_monitoring(
                        enabled=True,
                        interval_seconds=300,
                        notifications_enabled=True,
                        quiet_hours_enabled=False,
                        quiet_hours_start="22:00",
                        quiet_hours_end="08:00",
                        locale="zh-CN",
                    )
            stored = service.store.load()
            self.assertEqual(stored.poll_interval_seconds, 900)
            self.assertFalse(stored.notifications.enabled)

    def test_discovery_respects_shared_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            with exclusive_lock(service.paths.home / "operation.lock"):
                with self.assertRaises(ServiceBusy):
                    service.discover([directory])

    def test_clone_repository_stages_verifies_and_atomically_finalizes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()

            def clone(_url, destination, **kwargs):
                destination.mkdir()
                kwargs["progress"](
                    "stderr", "Receiving objects: 50% (5/10)\r"
                )

            def inspect(path):
                return RepositoryCandidate(
                    name="app",
                    path=str(path),
                    remote_name="origin",
                    remote_url="https://github.com/acme/app.git",
                    canonical_remote="github.com/acme/app",
                    current_branch="main",
                    upstream_branch="main",
                    default_branch="main",
                    branches=["main", "feature/ui"],
                )

            service.git.clone_repository.side_effect = clone
            updates = []
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                side_effect=inspect,
            ):
                result = service.clone_repository(
                    "https://github.com/acme/app/tree/main?tab=readme",
                    str(projects),
                    directory_name="app-checkout",
                    progress=updates.append,
                )

            target = projects.resolve() / "app-checkout"
            self.assertFalse(result["reused"])
            self.assertEqual(result["repository"]["path"], str(target))
            self.assertEqual(
                result["repository"]["branches"], ["main", "feature/ui"]
            )
            self.assertTrue(target.is_dir())
            self.assertEqual(
                list(projects.glob(".branch-radar-clone-*.partial")), []
            )
            self.assertIn("download", [update["phase"] for update in updates])

    def test_atomic_clone_finalize_refuses_to_replace_an_empty_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "staging"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            marker = source / "owned-by-clone"
            marker.write_text("keep", encoding="utf-8")

            with _CloneRootGuard(root) as root_guard:
                with self.assertRaises(OSError) as raised:
                    _atomic_rename_no_replace(
                        source, target, dir_fd=root_guard.fd
                    )

            self.assertIn(raised.exception.errno, {errno.EEXIST, errno.ENOTEMPTY})
            self.assertTrue(marker.is_file())
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])

    def test_atomic_clone_finalize_fails_closed_on_unsupported_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "staging"
            target = root / "target"
            source.mkdir()
            with mock.patch(
                "branch_radar.services.platform.system", return_value="Haiku"
            ), self.assertRaises(OSError) as raised:
                _atomic_rename_no_replace(source, target, dir_fd=None)
            self.assertEqual(raised.exception.errno, errno.ENOTSUP)
            self.assertTrue(source.is_dir())
            self.assertFalse(target.exists())

    def test_linux_atomic_finalize_uses_syscall_when_libc_wrapper_is_missing(self):
        class FakeSyscall:
            restype = None

            def __init__(self):
                self.args = None

            def __call__(self, *args):
                self.args = args
                return 0

        class FakeLibc:
            def __init__(self):
                self.syscall = FakeSyscall()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "staging"
            target = root / "target"
            fake_libc = FakeLibc()
            with mock.patch(
                "branch_radar.services.platform.system", return_value="Linux"
            ), mock.patch(
                "branch_radar.services.platform.machine", return_value="x86_64"
            ), mock.patch(
                "branch_radar.services.ctypes.CDLL", return_value=fake_libc
            ):
                _atomic_rename_no_replace(source, target, dir_fd=42)

            self.assertIsNotNone(fake_libc.syscall.args)
            assert fake_libc.syscall.args is not None
            self.assertEqual(fake_libc.syscall.args[0].value, 316)
            self.assertEqual(fake_libc.syscall.args[1].value, 42)
            self.assertEqual(fake_libc.syscall.args[3].value, 42)
            self.assertEqual(fake_libc.syscall.args[5].value, 1)

    def test_clone_finalize_race_preserves_the_competing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()

            def clone(_url, destination, **_kwargs):
                destination.mkdir()

            def inspect(path):
                return RepositoryCandidate(
                    name="app",
                    path=str(path),
                    remote_name="origin",
                    remote_url="https://github.com/acme/app.git",
                    canonical_remote="github.com/acme/app",
                    current_branch="main",
                    upstream_branch="main",
                    default_branch="main",
                    branches=["main"],
                )

            service.git.clone_repository.side_effect = clone

            def race(source, target, *, dir_fd):
                target.mkdir()
                (target / "belongs-to-user").write_text("keep", encoding="utf-8")
                _atomic_rename_no_replace(source, target, dir_fd=dir_fd)

            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                side_effect=inspect,
            ), mock.patch(
                "branch_radar.services._atomic_rename_no_replace",
                side_effect=race,
            ), self.assertRaises(ServiceError) as raised:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )

            self.assertEqual(raised.exception.code, "clone_destination_exists")
            target = projects.resolve() / "app"
            self.assertEqual(
                (target / "belongs-to-user").read_text(encoding="utf-8"),
                "keep",
            )
            self.assertEqual(
                list(projects.glob(".branch-radar-clone-*.partial")), []
            )

    def test_clone_root_identity_change_never_cleans_through_replaced_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            moved_projects = root / "projects-original"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()
            replacement_staging = []

            def clone(_url, destination, **_kwargs):
                destination.mkdir()
                projects.rename(moved_projects)
                projects.mkdir()
                replacement = projects / destination.name
                replacement.mkdir()
                (replacement / "belongs-to-replacement").write_text(
                    "keep", encoding="utf-8"
                )
                replacement_staging.append(replacement)

            service.git.clone_repository.side_effect = clone
            with self.assertRaises(ServiceError) as raised:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )

            self.assertEqual(raised.exception.code, "clone_cleanup_failed")
            self.assertEqual(len(replacement_staging), 1)
            self.assertEqual(
                (replacement_staging[0] / "belongs-to-replacement").read_text(
                    encoding="utf-8"
                ),
                "keep",
            )
            original_staging = moved_projects / replacement_staging[0].name
            self.assertTrue(original_staging.is_dir())

    def test_clone_repository_reuses_only_same_repository_at_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            target = projects / "app"
            target.mkdir(parents=True)
            target = target.resolve()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()
            same = RepositoryCandidate(
                name="app",
                path=str(target),
                remote_name="origin",
                remote_url="git@github.com:Acme/App.git",
                canonical_remote="github.com/Acme/App",
                current_branch="main",
                upstream_branch="main",
                default_branch="main",
                branches=["main"],
            )
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                return_value=same,
            ):
                result = service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )
            self.assertTrue(result["reused"])
            service.git.clone_repository.assert_not_called()

            different = RepositoryCandidate(**{
                **same.to_dict(),
                "canonical_remote": "github.com/acme/other",
            })
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                return_value=different,
            ), self.assertRaises(ServiceError) as raised:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )
            self.assertEqual(raised.exception.code, "clone_destination_exists")

    def test_clone_repository_validates_root_name_and_cleans_cancelled_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            nested = projects / "nested"
            other = root / "other"
            nested.mkdir(parents=True)
            other.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            self.assertEqual(
                _validate_clone_directory_name(".github"), ".github"
            )
            for invalid_parent in (str(other), str(nested)):
                with self.subTest(parent=invalid_parent), self.assertRaises(
                    ServiceError
                ) as raised:
                    service.clone_repository(
                        "https://github.com/acme/app.git", invalid_parent
                    )
                self.assertEqual(raised.exception.code, "invalid_clone_parent")
            for invalid_name in (
                "../escape", "nested/app", "CON", "name ", ".git", ".GIT",
                ".branch-radar-clone-reserved.partial",
            ):
                with self.subTest(name=invalid_name), self.assertRaises(
                    ServiceError
                ) as raised:
                    service.clone_repository(
                        "https://github.com/acme/app.git",
                        str(projects),
                        directory_name=invalid_name,
                    )
                self.assertEqual(raised.exception.code, "invalid_clone_directory")

            service.git = mock.MagicMock()

            def cancel(_url, destination, **_kwargs):
                destination.mkdir()
                (destination / "partial-pack").write_bytes(b"x" * 1024)
                raise GitCloneCancelled("cancelled")

            service.git.clone_repository.side_effect = cancel
            with self.assertRaises(ServiceError) as cancelled:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )
            self.assertEqual(cancelled.exception.code, "clone_cancelled")
            self.assertEqual(
                list(projects.glob(".branch-radar-clone-*.partial")), []
            )

    def test_clone_repository_reports_when_cancelled_stage_cannot_be_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()

            def cancel(_url, destination, **_kwargs):
                destination.mkdir()
                raise GitCloneCancelled("cancelled")

            service.git.clone_repository.side_effect = cancel
            with mock.patch(
                "branch_radar.services._cleanup_clone_staging",
                return_value=False,
            ), self.assertRaises(ServiceError) as cancelled:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )
            self.assertEqual(cancelled.exception.code, "clone_cleanup_failed")
            self.assertIn("临时目录未能完全清理", str(cancelled.exception))
            self.assertNotIn(str(projects.resolve()), str(cancelled.exception))

    def test_clone_repository_does_not_expose_arbitrary_git_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()
            service.git.clone_repository.side_effect = GitError(
                "fatal: token=private-value at /private/local/path"
            )
            with self.assertRaises(ServiceError) as raised:
                service.clone_repository(
                    "https://github.com/acme/app.git", str(projects)
                )
            self.assertEqual(raised.exception.code, "clone_failed")
            message = str(raised.exception)
            self.assertNotIn("private-value", message)
            self.assertNotIn("/private/local/path", message)

    def test_clone_verification_prefers_cancellation_over_candidate_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects = root / "projects"
            projects.mkdir()
            service = self.create_service(str(root / "state"))
            config = service.store.load()
            config.scan_roots = [str(projects)]
            service.store.save(config)
            service.git = mock.MagicMock()
            cancel_event = threading.Event()

            def clone(_url, destination, **_kwargs):
                destination.mkdir()

            def inspect(path):
                cancel_event.set()
                return RepositoryCandidate(
                    name="broken",
                    path=str(path),
                    remote_name=None,
                    remote_url=None,
                    canonical_remote=None,
                    current_branch=None,
                    upstream_branch=None,
                    default_branch=None,
                    error="verification failed",
                )

            service.git.clone_repository.side_effect = clone
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                side_effect=inspect,
            ), self.assertRaises(ServiceError) as raised:
                service.clone_repository(
                    "https://github.com/acme/app.git",
                    str(projects),
                    cancel_event=cancel_event,
                )
            self.assertEqual(raised.exception.code, "clone_cancelled")

    def test_add_watches_returns_safe_repository_and_branch_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            service.git = mock.Mock()
            service.git.branch_ref.return_value = "a" * 40
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                return_value=candidate,
            ):
                result = service.add_watches([
                    {"path": candidate.path, "branches": ["main"]}
                ])

            watched = result["watched"][0]
            self.assertIsInstance(watched["repository_id"], int)
            self.assertIsInstance(watched["branch_id"], int)
            row = service.database.list_watches()[0]
            self.assertEqual(watched["repository_id"], row["repository_id"])
            self.assertEqual(watched["branch_id"], row["branch_id"])

    def test_add_watches_fetches_only_selected_branches_and_reports_phases(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = RepositoryCandidate(
                name="demo",
                path=str(Path(directory) / "checkout"),
                remote_name="origin",
                remote_url="https://example.invalid/team/demo.git",
                canonical_remote="example.invalid/team/demo",
                current_branch="main",
                upstream_branch="main",
                default_branch="main",
                branches=["main", "feature/ui"],
            )
            service.git = mock.Mock()
            service.git.branch_ref.side_effect = ["a" * 40, "b" * 40]
            updates = []
            with mock.patch(
                "branch_radar.services.RepositoryScanner.inspect",
                return_value=candidate,
            ):
                result = service.add_watches(
                    [{"path": candidate.path, "branches": ["main", "feature/ui"]}],
                    progress=updates.append,
                )

            self.assertEqual(len(result["watched"]), 2)
            service.git.fetch.assert_called_once_with(
                Path(candidate.path),
                "origin",
                timeout=600,
                expected_fingerprint=candidate.canonical_remote,
                branches=["main", "feature/ui"],
            )
            phases = [update["phase"] for update in updates]
            self.assertIn("inspect", phases)
            self.assertIn("fetch", phases)
            self.assertIn("resolve", phases)
            self.assertIn("save", phases)

    def test_check_can_target_selected_branch_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            main_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            feature_id = service.database.add_branch(
                repository_id, "feature", None, baseline_oid="b" * 40
            )

            with mock.patch("branch_radar.services.BranchMonitor") as monitor_type:
                monitor_type.return_value.check_watches.return_value = []
                results, publications = service.check(
                    fetch=False, branch_ids=[feature_id]
                )

            self.assertEqual(results, [])
            self.assertEqual(publications, [])
            selected = monitor_type.return_value.check_watches.call_args.args[0]
            self.assertEqual([row["branch_id"] for row in selected], [feature_id])
            self.assertNotEqual(main_id, feature_id)
            self.assertIsNone(service.database.daemon_health()["last_success_at"])

    def test_check_reports_publish_phase_before_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            config = service.store.load()
            config.lark.document = "https://team.feishu.cn/docx/docx-safe"
            publisher = mock.Mock()
            publisher.publish_pending.return_value = []
            updates = []
            with mock.patch.object(
                service, "refresh_config", return_value=config
            ), mock.patch(
                "branch_radar.services.BranchMonitor"
            ) as monitor_type, mock.patch.object(
                service, "_publisher", return_value=publisher
            ), mock.patch.object(service, "lark", return_value=mock.Mock()):
                monitor_type.return_value.check_watches.return_value = []
                service.check(fetch=False, publish=True, progress=updates.append)

            phases = [update["phase"] for update in updates]
            self.assertLess(phases.index("finalize"), phases.index("publish"))
            self.assertLess(phases.index("publish"), phases.index("complete"))
            publisher.publish_pending.assert_called_once_with(limit=1000)

    def test_check_run_keeps_rule_summary_when_batch_codex_fails(self):
        class Source:
            def refresh(self, _watches, *, fetch=True):
                return {}

            def resolve(self, watch):
                return {"main": "c" * 40, "feature": "d" * 40}[watch["branch"]]

            def describe(self, watch, transition):
                return ChangeFacts(
                    repository_name=watch["repository"],
                    repository_path=watch["local_path"],
                    remote_url=None,
                    branch=watch["branch"],
                    event_kind="fast_forward",
                    old_sha=transition.old_sha,
                    new_sha=transition.new_sha,
                    merge_base=transition.old_sha,
                    detected_at="2026-08-21T00:00:00+00:00",
                    commit_count=2,
                    diff=DiffStats(files_changed=2, insertions=20, deletions=2),
                    categories=["api"],
                )

            def attach_patch_excerpt(self, *_args, **_kwargs):
                return None

        class FailedCodex:
            def summarize(self, _facts):
                raise RuntimeError("single summary unavailable")

            def summarize_check_run(self, _events, *, completeness_note):
                raise RuntimeError("batch summary unavailable")

        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            service.database.add_branch(
                repository_id, "feature", None, baseline_oid="b" * 40
            )
            updates = []
            with mock.patch(
                "branch_radar.services.GitFactSource", return_value=Source()
            ), mock.patch.object(service, "codex", return_value=FailedCodex()):
                results, publications = service.check(
                    fetch=False,
                    use_codex=True,
                    progress=updates.append,
                )

            self.assertEqual([item.status for item in results], ["changed", "changed"])
            self.assertEqual(publications, [])
            run = service.last_check_run()
            self.assertEqual(run["status"], "succeeded")
            self.assertEqual(run["event_count"], 2)
            self.assertEqual(run["summary"]["source"], "rules")
            self.assertIn("Codex 批次综述失败", run["summary_warning"])
            self.assertEqual(len(run["event_ids"]), 2)
            phases = [update["phase"] for update in updates]
            self.assertLess(phases.index("finalize"), phases.index("summarize"))
            self.assertLess(phases.index("summarize"), phases.index("complete"))

    def test_check_run_codex_summary_removes_state_and_repository_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            main_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            feature_id = service.database.add_branch(
                repository_id, "feature", None, baseline_oid="b" * 40
            )
            watches = service.database.list_watches()
            run_id = service.database.begin_check_run(
                scope_kind="all",
                repository_id=None,
                requested_branch_ids=[],
                watches=watches,
                fetch=False,
                use_codex=True,
                publish=False,
            )
            fallback = {
                "title": "本轮发现 2 个变化",
                "overview": "完成",
                "themes": [],
                "priorities": [],
                "connections": [],
                "completeness_note": "",
                "source": "rules",
            }
            service.database.finish_check_run(
                run_id,
                results=[
                    {
                        "branch_id": main_id,
                        "status": "unchanged",
                        "event_id": None,
                        "blocked": False,
                    },
                    {
                        "branch_id": feature_id,
                        "status": "unchanged",
                        "event_id": None,
                        "blocked": False,
                    },
                ],
                publications=[],
                summary=fallback,
            )
            private_path = str(service.paths.analysis_workspace)

            class PathEchoingCodex:
                def summarize_check_run(self, _events, *, completeness_note):
                    return {
                        "title": "结果位于 %s" % private_path,
                        "overview": "仓库 %s" % candidate.path,
                        "themes": [private_path],
                        "priorities": [str(candidate.path)],
                        "connections": [private_path],
                        "completeness_note": completeness_note,
                    }

            events = [
                {
                    "id": event_id,
                    "repository": "demo",
                    "branch": branch,
                    "kind": "fast_forward",
                    "facts": {"categories": ["api"], "signals": []},
                    "summary": {"title": "更新"},
                }
                for event_id, branch in ((1, "main"), (2, "feature"))
            ]
            service._enhance_check_run_summary(
                run_id,
                PathEchoingCodex(),
                events,
                fallback,
                watches,
            )

            run = service.database.get_check_run(run_id)
            payload = json.dumps(
                {"run": run, "view": check_run_view(run)},
                ensure_ascii=False,
            )
            self.assertNotIn(private_path, payload)
            self.assertNotIn(str(candidate.path), payload)
            self.assertIn("[repository]", payload)
            self.assertEqual(run["summary"]["source"], "codex")

    def test_check_run_errors_do_not_persist_repository_or_state_paths(self):
        class BlockedSource:
            def refresh(self, watches, *, fetch=True):
                watch = watches[0]
                return {
                    watch["branch_id"]: "failed at %s using %s"
                    % (watch["local_path"], state_path)
                }

        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory))
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            with mock.patch(
                "branch_radar.services.GitFactSource", return_value=BlockedSource()
            ):
                results, _ = service.check(fetch=False)

            run = service.last_check_run()
            serialized = str({"result": results[0].to_dict(), "run": run})
            self.assertNotIn(str(candidate.path), serialized)
            self.assertNotIn(state_path, serialized)
            self.assertIn("[repository]", serialized)

    def test_check_can_target_every_enabled_branch_in_one_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            selected_repository = self.candidate(Path(directory) / "selected")
            selected_repository.name = "selected"
            selected_repository_id = service.database.upsert_repository(
                selected_repository
            )
            main_id = service.database.add_branch(
                selected_repository_id, "main", None, baseline_oid="a" * 40
            )
            feature_id = service.database.add_branch(
                selected_repository_id, "feature", None, baseline_oid="b" * 40
            )

            other_repository = self.candidate(Path(directory) / "other")
            other_repository.name = "other"
            other_repository_id = service.database.upsert_repository(other_repository)
            other_id = service.database.add_branch(
                other_repository_id, "main", None, baseline_oid="c" * 40
            )

            with mock.patch("branch_radar.services.BranchMonitor") as monitor_type:
                monitor_type.return_value.check_watches.return_value = []
                results, publications = service.check(
                    fetch=False,
                    branch_ids=[main_id, feature_id],
                )

            self.assertEqual(results, [])
            self.assertEqual(publications, [])
            selected = monitor_type.return_value.check_watches.call_args.args[0]
            self.assertEqual(
                {row["branch_id"] for row in selected},
                {main_id, feature_id},
            )
            self.assertEqual(
                {row["repository_id"] for row in selected},
                {selected_repository_id},
            )
            self.assertNotIn(other_id, {row["branch_id"] for row in selected})

    def test_check_rejects_branch_selection_if_one_branch_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            main_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            feature_id = service.database.add_branch(
                repository_id, "feature", None, baseline_oid="b" * 40
            )
            service.database.disable_watch(feature_id)

            with mock.patch("branch_radar.services.BranchMonitor") as monitor_type:
                with self.assertRaises(ServiceError) as raised:
                    service.check(
                        fetch=False,
                        branch_ids=[main_id, feature_id],
                    )

            self.assertEqual(raised.exception.code, "selection_not_found")
            monitor_type.return_value.check_watches.assert_not_called()

    def test_check_repository_resolves_all_current_enabled_watches_under_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            selected_repository = self.candidate(Path(directory) / "selected")
            selected_repository.name = "selected"
            selected_repository_id = service.database.upsert_repository(
                selected_repository
            )
            main_id = service.database.add_branch(
                selected_repository_id, "main", None, baseline_oid="a" * 40
            )
            feature_id = service.database.add_branch(
                selected_repository_id, "feature", None, baseline_oid="b" * 40
            )
            disabled_id = service.database.add_branch(
                selected_repository_id, "retired", None, baseline_oid="c" * 40
            )
            service.database.disable_watch(disabled_id)

            other_repository = self.candidate(Path(directory) / "other")
            other_repository.name = "other"
            other_repository_id = service.database.upsert_repository(other_repository)
            other_id = service.database.add_branch(
                other_repository_id, "main", None, baseline_oid="d" * 40
            )

            with mock.patch("branch_radar.services.BranchMonitor") as monitor_type:
                monitor_type.return_value.check_watches.return_value = []
                results, publications = service.check(
                    fetch=False,
                    repository_id=selected_repository_id,
                )

            self.assertEqual(results, [])
            self.assertEqual(publications, [])
            selected = monitor_type.return_value.check_watches.call_args.args[0]
            self.assertEqual(
                {row["branch_id"] for row in selected},
                {main_id, feature_id},
            )
            self.assertNotIn(disabled_id, {row["branch_id"] for row in selected})
            self.assertNotIn(other_id, {row["branch_id"] for row in selected})

    def test_check_repository_allows_more_than_branch_selection_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            for index in range(201):
                service.database.add_branch(
                    repository_id,
                    "branch-%03d" % index,
                    None,
                    baseline_oid="a" * 40,
                )

            with mock.patch("branch_radar.services.BranchMonitor") as monitor_type:
                monitor_type.return_value.check_watches.return_value = []
                service.check(fetch=False, repository_id=repository_id)

            selected = monitor_type.return_value.check_watches.call_args.args[0]
            self.assertEqual(len(selected), 201)

    def test_check_repository_rejects_missing_or_ambiguous_selector(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            branch_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )
            disabled_candidate = self.candidate(Path(directory) / "disabled")
            disabled_candidate.name = "disabled"
            disabled_repository_id = service.database.upsert_repository(
                disabled_candidate
            )
            disabled_branch_id = service.database.add_branch(
                disabled_repository_id, "main", None, baseline_oid="b" * 40
            )
            service.database.disable_watch(disabled_branch_id)

            attempts = (
                ({"repository_id": 999}, "selection_not_found"),
                (
                    {"repository_id": disabled_repository_id},
                    "selection_not_found",
                ),
                ({"repository_id": True}, "invalid_selection"),
                (
                    {"repository_id": repository_id, "branch_ids": [branch_id]},
                    "invalid_selection",
                ),
            )
            for arguments, expected in attempts:
                with self.subTest(arguments=arguments):
                    with mock.patch(
                        "branch_radar.services.BranchMonitor"
                    ) as monitor_type, self.assertRaises(ServiceError) as raised:
                        service.check(fetch=False, **arguments)
                    self.assertEqual(raised.exception.code, expected)
                    monitor_type.return_value.check_watches.assert_not_called()

    def test_check_rejects_missing_selected_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            with self.assertRaises(ServiceError) as raised:
                service.check(fetch=False, branch_ids=[999])
            self.assertEqual(raised.exception.code, "selection_not_found")
            self.assertEqual(
                service.database.daemon_health()["consecutive_failures"], 0
            )

    def test_remove_watch_returns_safe_identity_and_disables_it(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            branch_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )

            removed = service.remove_watch(branch_id)

            self.assertEqual(removed, {
                "branch_id": branch_id,
                "repository_id": repository_id,
                "repository": "same-name",
                "branch": "main",
            })
            self.assertEqual(service.database.list_watches(), [])
            with self.assertRaises(ServiceError) as raised:
                service.remove_watch(branch_id)
            self.assertEqual(raised.exception.code, "watch_not_found")

    def test_remove_watch_respects_shared_operation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            candidate = self.candidate(Path(directory) / "checkout")
            repository_id = service.database.upsert_repository(candidate)
            branch_id = service.database.add_branch(
                repository_id, "main", None, baseline_oid="a" * 40
            )

            with exclusive_lock(service.paths.home / "operation.lock"):
                with self.assertRaises(ServiceBusy):
                    service.remove_watch(branch_id)

            self.assertEqual(len(service.database.list_watches()), 1)

    def test_integrations_refreshes_configured_document_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            config = service.store.load()
            config.lark.document = "https://team.feishu.cn/docx/docx-safe"
            service.store.save(config)
            lark = mock.Mock()
            lark.status.return_value = {
                "available": True,
                "verified": True,
            }
            lark.verify_document.return_value = {
                "title": "项目动态",
                "url": "https://team.feishu.cn/docx/docx-safe",
            }

            with mock.patch.object(service, "lark", return_value=lark):
                payload = service.integrations()

            self.assertEqual(payload["lark"]["document_details"]["title"], "项目动态")
            lark.verify_document.assert_called_once_with(config.lark.document)

    def test_same_request_id_with_different_title_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            fake = _FakeLark()
            with mock.patch.object(service, "lark", return_value=fake):
                service.create_lark_document(
                    "标题 A", request_id="request-conflict-001"
                )
                with self.assertRaises(ServiceError) as raised:
                    service.create_lark_document(
                        "标题 B", request_id="request-conflict-001"
                    )
            self.assertEqual(raised.exception.code, "idempotency_conflict")
            self.assertEqual(fake.calls, 1)

    def test_unknown_remote_result_is_persisted_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            fake = _FakeLark(
                error=LarkError("响应丢失", may_have_succeeded=True)
            )
            with mock.patch.object(service, "lark", return_value=fake):
                with self.assertRaises(ServiceError) as first:
                    service.create_lark_document(
                        "项目动态", request_id="request-unknown-001"
                    )
                with self.assertRaises(ServiceError) as second:
                    service.create_lark_document(
                        "项目动态", request_id="request-unknown-002"
                    )
            self.assertEqual(first.exception.code, "lark_create_unknown")
            self.assertEqual(second.exception.code, "lark_create_unknown")
            self.assertEqual(fake.calls, 1)

    def test_lock_contention_does_not_poison_request_key(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.create_service(directory)
            fake = _FakeLark()
            with mock.patch.object(service, "lark", return_value=fake):
                with exclusive_lock(service.paths.home / "operation.lock"):
                    with self.assertRaises(ServiceBusy):
                        service.create_lark_document(
                            "项目动态", request_id="request-lock-001"
                        )
                created = service.create_lark_document(
                    "项目动态", request_id="request-lock-001"
                )
            self.assertIn("url", created)
            self.assertEqual(fake.calls, 1)


if __name__ == "__main__":
    unittest.main()
