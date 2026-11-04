import io
import json
import os
import plistlib
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from branch_radar.cli import Application, build_parser
from branch_radar.paths import AppPaths
from branch_radar.process import ProcessResult
from branch_radar.service import (
    DEFAULT_DAEMON_LABEL,
    DEFAULT_WEB_LABEL,
    LEGACY_DEFAULT_LABEL,
    LaunchdManager,
    ServiceInstallError,
    ServiceSpec,
    SystemdUserManager,
    build_manager,
    service_environment,
)


class FakeRunner:
    """Records launchctl/systemctl invocations instead of running them."""

    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        key = " ".join(argv)
        code = 1 if any(key.startswith(prefix) for prefix in self.failures) else 0
        return ProcessResult(argv, code, "", "boom" if code else "", 0.01)

    def commands(self):
        return [" ".join(call) for call in self.calls]


def paths_for(directory: str) -> AppPaths:
    home = Path(directory) / "state"
    return AppPaths(
        home=home,
        config=home / "config.json",
        database=home / "state.db",
        logs=home / "logs",
        cache=home / "cache",
        analysis_workspace=home / "analysis-workspace",
    )


class SpecValidationTests(unittest.TestCase):
    def test_invalid_specs_are_refused_before_anything_is_written(self):
        for spec in (
            ServiceSpec(mode="cron"),
            ServiceSpec(interval=5),
            ServiceSpec(port=70000),
            ServiceSpec(label=""),
            ServiceSpec(label="bad label"),
            ServiceSpec(label="../escape"),
        ):
            with self.assertRaises(ServiceInstallError):
                spec.validate()

    def test_unsupported_platform_is_an_explicit_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ServiceInstallError) as raised:
                build_manager(paths_for(directory), ServiceSpec(), platform="win32")
            self.assertIn("暂不支持", str(raised.exception))

    def test_daemon_command_is_one_shot_and_defaults_to_fifteen_minutes(self):
        spec = ServiceSpec(mode="daemon", use_codex=True, publish=True)
        self.assertEqual(spec.interval_seconds, 900)
        self.assertEqual(
            spec.command("/opt/python"),
            [
                "/opt/python",
                "-m",
                "branch_radar",
                "daemon",
                "--once",
                "--codex",
                "--publish",
                "--json",
            ],
        )

    def test_default_labels_are_mode_specific_and_explicit_labels_are_preserved(self):
        daemon = ServiceSpec(mode="daemon")
        web = ServiceSpec(mode="web")
        custom = ServiceSpec(mode="web", label="local.branch-radar")

        self.assertEqual(daemon.resolved_label, DEFAULT_DAEMON_LABEL)
        self.assertEqual(web.resolved_label, DEFAULT_WEB_LABEL)
        self.assertNotEqual(daemon.resolved_label, web.resolved_label)
        self.assertEqual(custom.resolved_label, "local.branch-radar")
        self.assertFalse(custom.uses_default_label)


class ServiceEnvironmentTests(unittest.TestCase):
    def test_default_state_directory_is_always_pinned(self):
        with tempfile.TemporaryDirectory() as directory:
            account_home = Path(directory) / "account"
            with mock.patch.dict(
                os.environ,
                {"HOME": str(account_home)},
                clear=True,
            ):
                paths = AppPaths.discover()
                environment = service_environment(paths, account_home)

        self.assertEqual(paths.home, account_home / ".branch-radar")
        self.assertEqual(environment["BRANCH_RADAR_HOME"], str(paths.home))

    def test_resolved_custom_state_directory_overrides_stale_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = paths_for(directory)
            with mock.patch.dict(
                os.environ,
                {"BRANCH_RADAR_HOME": "/stale/state-directory"},
                clear=True,
            ):
                environment = service_environment(paths, Path(directory) / "home")

        self.assertEqual(environment["BRANCH_RADAR_HOME"], str(paths.home))
        self.assertNotEqual(
            environment["BRANCH_RADAR_HOME"],
            "/stale/state-directory",
        )


class LaunchdTests(unittest.TestCase):
    def manager(self, directory, spec=None, runner=None):
        return LaunchdManager(
            paths_for(directory),
            spec or ServiceSpec(mode="daemon", interval=900),
            runner=runner or FakeRunner(),
            executable="/opt/venv/bin/python",
            home=Path(directory) / "home",
        )

    def test_unit_pins_the_installing_interpreter_and_state_directory(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "BRANCH_RADAR_HOME": "/custom/radar-home",
                    "DISPLAY": ":1",
                    "WAYLAND_DISPLAY": "wayland-1",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                },
            ),
        ):
            rendered = self.manager(directory).render()
            definition = plistlib.loads(rendered.encode("utf-8"))
        self.assertIn("<string>/opt/venv/bin/python</string>", rendered)
        self.assertIn("<string>branch_radar</string>", rendered)
        self.assertEqual(definition["StartInterval"], 900)
        self.assertIn("--once", definition["ProgramArguments"])
        self.assertIn("--json", definition["ProgramArguments"])
        self.assertNotIn("--interval", definition["ProgramArguments"])
        self.assertNotIn("KeepAlive", definition)
        self.assertNotIn("RunAtLoad", definition)
        self.assertEqual(definition["ThrottleInterval"], 60)
        # Without this the agent would quietly use a different state directory.
        self.assertEqual(
            definition["EnvironmentVariables"]["BRANCH_RADAR_HOME"],
            str(Path(directory) / "state"),
        )
        self.assertNotIn("/custom/radar-home", rendered)
        self.assertIn("BRANCH_RADAR_HOME", rendered)
        self.assertEqual(definition["EnvironmentVariables"]["DISPLAY"], ":1")
        self.assertEqual(
            definition["EnvironmentVariables"]["WAYLAND_DISPLAY"], "wayland-1"
        )
        self.assertEqual(
            definition["EnvironmentVariables"]["DBUS_SESSION_BUS_ADDRESS"],
            "unix:path=/run/user/1000/bus",
        )
        self.assertEqual(
            definition["EnvironmentVariables"]["XDG_RUNTIME_DIR"],
            "/run/user/1000",
        )

    def test_web_unit_remains_resident_and_has_no_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = plistlib.loads(
                self.manager(directory, ServiceSpec(mode="web", port=9001))
                .render()
                .encode("utf-8")
            )
        self.assertNotIn("StartInterval", definition)
        self.assertTrue(definition["RunAtLoad"])
        self.assertEqual(definition["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(
            definition["ProgramArguments"][-3:], ["--port", "9001", "--no-open"]
        )
        self.assertNotIn("--once", definition["ProgramArguments"])

    def test_short_daemon_interval_is_not_stretched_by_launchd_throttling(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = plistlib.loads(
                self.manager(directory, ServiceSpec(mode="daemon", interval=10))
                .render()
                .encode("utf-8")
            )
        self.assertEqual(definition["StartInterval"], 10)
        self.assertEqual(definition["ThrottleInterval"], 10)

    def test_install_replaces_any_previous_definition_and_restricts_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            manager = self.manager(directory, runner=runner)
            result = manager.install()

            self.assertTrue(manager.unit_path.exists())
            self.assertEqual(oct(manager.unit_path.stat().st_mode)[-3:], "600")
            self.assertTrue(result["loaded"])
            commands = manager.runner.commands()
            self.assertTrue(commands[0].startswith("launchctl bootout"))
            self.assertTrue(commands[1].startswith("launchctl bootstrap"))

    def test_install_falls_back_to_legacy_load_when_bootstrap_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(failures={"launchctl bootstrap"})
            manager = self.manager(directory, runner=runner)
            manager.install()
            self.assertIn(
                "launchctl load -w %s" % manager.unit_path, manager.runner.commands()
            )

    def test_install_failure_names_the_written_unit_instead_of_claiming_success(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(failures={"launchctl bootstrap", "launchctl load"})
            manager = self.manager(directory, runner=runner)
            with self.assertRaises(ServiceInstallError) as raised:
                manager.install()
            self.assertIn(str(manager.unit_path), str(raised.exception))

    def test_uninstall_boots_out_and_removes_the_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.install()
            result = manager.uninstall()
            self.assertTrue(result["removed"])
            self.assertFalse(manager.unit_path.exists())
            self.assertTrue(
                any(
                    command.startswith("launchctl bootout")
                    for command in manager.runner.commands()
                )
            )

    def test_uninstall_is_idempotent_when_nothing_is_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.manager(directory).uninstall()
            self.assertFalse(result["removed"])

    def test_default_daemon_migrates_a_legacy_daemon_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self.manager(
                directory,
                ServiceSpec(mode="daemon", label=LEGACY_DEFAULT_LABEL),
            )
            legacy._write_unit()
            runner = FakeRunner()

            current = self.manager(directory, runner=runner)
            current.install()

            self.assertFalse(legacy.unit_path.exists())
            self.assertTrue(current.unit_path.exists())
            self.assertIn(
                "launchctl bootout gui/%d/%s" % (os.getuid(), LEGACY_DEFAULT_LABEL),
                runner.commands(),
            )

    def test_default_daemon_does_not_stop_a_legacy_web_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_web = self.manager(
                directory,
                ServiceSpec(mode="web", label=LEGACY_DEFAULT_LABEL),
            )
            legacy_web._write_unit()
            runner = FakeRunner()

            current = self.manager(directory, runner=runner)
            current.install()

            self.assertTrue(legacy_web.unit_path.exists())
            self.assertTrue(current.unit_path.exists())
            self.assertNotIn(
                "launchctl bootout gui/%d/%s" % (os.getuid(), LEGACY_DEFAULT_LABEL),
                runner.commands(),
            )

    def test_default_daemon_and_web_units_can_be_installed_together(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = self.manager(directory)
            web = self.manager(directory, ServiceSpec(mode="web"))

            daemon.install()
            web.install()

            self.assertNotEqual(daemon.unit_path, web.unit_path)
            self.assertTrue(daemon.unit_path.exists())
            self.assertTrue(web.unit_path.exists())

    def test_status_recognizes_a_matching_legacy_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self.manager(
                directory,
                ServiceSpec(mode="daemon", label=LEGACY_DEFAULT_LABEL),
            )
            legacy._write_unit()
            current_target = "launchctl print gui/%d/%s" % (
                os.getuid(),
                DEFAULT_DAEMON_LABEL,
            )
            current = self.manager(
                directory, runner=FakeRunner(failures={current_target})
            )

            result = current.status()

            self.assertTrue(result["installed"])
            self.assertTrue(result["loaded"])
            self.assertTrue(result["legacy"])
            self.assertEqual(result["unit"], str(legacy.unit_path))


class SystemdTests(unittest.TestCase):
    def manager(self, directory, spec=None, runner=None):
        return SystemdUserManager(
            paths_for(directory),
            spec or ServiceSpec(mode="daemon", interval=900),
            runner=runner or FakeRunner(),
            executable="/opt/venv/bin/python",
            home=Path(directory) / "home",
        )

    def test_unit_quotes_environment_and_targets_the_user_manager(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(
                directory,
                ServiceSpec(mode="web", port=9001),
            )
            rendered = manager.render()
            self.assertIn(
                "ExecStart=/opt/venv/bin/python -m branch_radar web --port 9001 --no-open",
                rendered,
            )
            self.assertIn('Environment="HOME=', rendered)
            self.assertIn("WantedBy=default.target", rendered)

            result = manager.install()
            self.assertIn("loginctl enable-linger", str(result["note"]))
            self.assertIn(
                "systemctl --user enable --now com-branch-radar-web.service",
                manager.runner.commands(),
            )

    def test_unit_preserves_graphical_session_environment_for_notifications(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ,
                {
                    "DISPLAY": ":1",
                    "WAYLAND_DISPLAY": "wayland-1",
                    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
                    "XDG_RUNTIME_DIR": "/run/user/1000",
                },
            ),
        ):
            rendered = self.manager(directory).render()

        self.assertIn('Environment="DISPLAY=:1"', rendered)
        self.assertIn('Environment="WAYLAND_DISPLAY=wayland-1"', rendered)
        self.assertIn(
            'Environment="DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus"',
            rendered,
        )
        self.assertIn('Environment="XDG_RUNTIME_DIR=/run/user/1000"', rendered)

    def test_unit_escapes_special_characters_in_environment_values(self):
        value = 'locale "quoted"\\line\n100%'
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"LANG": value}),
        ):
            rendered = self.manager(directory).render()

        escaped_value = (
            value
            .replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("%", "%%")
        )
        self.assertIn(
            'Environment="LANG=%s"' % escaped_value,
            rendered,
        )
        self.assertNotIn('Environment="LANG=%s"' % value, rendered)

    def test_daemon_uses_a_one_shot_service_and_completion_based_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            service = manager.render()
            timer = manager.render_timer()

        self.assertIn("Type=oneshot", service)
        self.assertIn(
            "ExecStart=/opt/venv/bin/python -m branch_radar daemon --once --json",
            service,
        )
        self.assertNotIn("Restart=", service)
        self.assertNotIn("WantedBy=default.target", service)
        self.assertEqual(timer.count("OnActiveSec="), 1)
        self.assertIn("OnActiveSec=900s", timer)
        self.assertIn("OnUnitInactiveSec=900s", timer)
        self.assertNotIn("OnCalendar=", timer)
        self.assertNotIn("Persistent=", timer)
        self.assertIn("Unit=com-branch-radar-daemon.service", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_non_calendar_interval_keeps_exact_monotonic_schedule(self):
        with tempfile.TemporaryDirectory() as directory:
            timer = self.manager(
                directory, ServiceSpec(mode="daemon", interval=17)
            ).render_timer()

        self.assertIn("OnUnitInactiveSec=17s", timer)
        self.assertIn("OnActiveSec=17s", timer)
        self.assertNotIn("OnCalendar=", timer)

    def test_supported_intervals_use_one_non_overlapping_timer(self):
        for interval in (300, 900, 1800, 3600):
            with self.subTest(interval=interval):
                with tempfile.TemporaryDirectory() as directory:
                    timer = self.manager(
                        directory,
                        ServiceSpec(mode="daemon", interval=interval),
                    ).render_timer()

                self.assertEqual(timer.count("OnActiveSec="), 1)
                self.assertIn("OnActiveSec=%ss" % interval, timer)
                self.assertEqual(timer.count("OnUnitInactiveSec="), 1)
                self.assertIn("OnUnitInactiveSec=%ss" % interval, timer)
                self.assertNotIn("OnCalendar=", timer)

    def test_daemon_install_migrates_the_resident_service_and_enables_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            result = manager.install()

            self.assertTrue(manager.unit_path.exists())
            self.assertTrue(manager.timer_path.exists())
            self.assertEqual(oct(manager.unit_path.stat().st_mode)[-3:], "644")
            self.assertEqual(oct(manager.timer_path.stat().st_mode)[-3:], "644")
            self.assertEqual(result["timer_unit"], str(manager.timer_path))
            commands = manager.runner.commands()
            self.assertIn(
                "systemctl --user disable --now com-branch-radar-daemon.timer",
                commands,
            )
            self.assertIn(
                "systemctl --user disable --now com-branch-radar-daemon.service",
                commands,
            )
            self.assertIn(
                "systemctl --user enable --now com-branch-radar-daemon.timer",
                commands,
            )
            self.assertFalse(any(" start " in " %s " % command for command in commands))

    def test_daemon_uninstall_removes_timer_and_service_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.install()
            result = manager.uninstall()

            self.assertTrue(result["removed"])
            self.assertFalse(manager.unit_path.exists())
            self.assertFalse(manager.timer_path.exists())
            self.assertIn(
                "systemctl --user disable --now com-branch-radar-daemon.timer",
                manager.runner.commands(),
            )
            self.assertFalse(manager.uninstall()["removed"])

    def test_default_web_install_preserves_the_default_daemon_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon_manager = self.manager(directory)
            daemon_manager.install()
            self.assertTrue(daemon_manager.timer_path.exists())

            runner = FakeRunner()
            web_manager = self.manager(
                directory,
                ServiceSpec(mode="web", port=9001),
                runner=runner,
            )
            web_manager.install()

            self.assertTrue(daemon_manager.timer_path.exists())
            self.assertTrue(daemon_manager.unit_path.exists())
            self.assertTrue(web_manager.unit_path.exists())
            self.assertNotIn(
                "systemctl --user disable --now com-branch-radar-daemon.timer",
                runner.commands(),
            )
            self.assertIn(
                "systemctl --user enable --now com-branch-radar-web.service",
                runner.commands(),
            )

    def test_default_web_uninstall_preserves_default_daemon_units(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon_manager = self.manager(directory)
            daemon_manager.install()

            web_manager = self.manager(
                directory,
                ServiceSpec(mode="web"),
                runner=daemon_manager.runner,
            )
            web_manager.install()
            result = web_manager.uninstall()

            self.assertTrue(result["removed"])
            self.assertFalse(web_manager.unit_path.exists())
            self.assertTrue(daemon_manager.unit_path.exists())
            self.assertTrue(daemon_manager.timer_path.exists())

    def test_default_daemon_migrates_matching_legacy_units(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self.manager(
                directory,
                ServiceSpec(mode="daemon", label=LEGACY_DEFAULT_LABEL),
            )
            legacy._write_unit()
            runner = FakeRunner()

            current = self.manager(directory, runner=runner)
            current.install()

            self.assertFalse(legacy.unit_path.exists())
            self.assertFalse(legacy.timer_path.exists())
            self.assertTrue(current.unit_path.exists())
            self.assertTrue(current.timer_path.exists())
            self.assertIn(
                "systemctl --user disable --now com-branch-radar-agent.service",
                runner.commands(),
            )

    def test_default_daemon_preserves_a_legacy_web_service(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy_web = self.manager(
                directory,
                ServiceSpec(mode="web", label=LEGACY_DEFAULT_LABEL),
            )
            legacy_web._write_unit()
            runner = FakeRunner()

            current = self.manager(directory, runner=runner)
            current.install()

            self.assertTrue(legacy_web.unit_path.exists())
            self.assertTrue(current.unit_path.exists())
            self.assertNotIn(
                "systemctl --user disable --now com-branch-radar-agent.service",
                runner.commands(),
            )

    def test_status_recognizes_a_matching_legacy_resident_daemon(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy = self.manager(
                directory,
                ServiceSpec(mode="daemon", label=LEGACY_DEFAULT_LABEL),
            )
            legacy._write_unit()
            legacy.timer_path.unlink()
            runner = FakeRunner(
                failures={
                    "systemctl --user is-active com-branch-radar-daemon.timer",
                    "systemctl --user is-active com-branch-radar-daemon.service",
                    "systemctl --user is-active com-branch-radar-agent.timer",
                }
            )
            current = self.manager(directory, runner=runner)

            result = current.status()

            self.assertTrue(result["installed"])
            self.assertTrue(result["loaded"])
            self.assertTrue(result["legacy"])
            self.assertEqual(result["unit"], str(legacy.unit_path))
            self.assertEqual(result["state"], "running")

    def test_daemon_status_treats_an_idle_active_timer_as_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(
                failures={"systemctl --user is-active com-branch-radar-daemon.service"}
            )
            manager = self.manager(directory, runner=runner)
            manager._write_unit()
            result = manager.status()

        self.assertTrue(result["installed"])
        self.assertTrue(result["timer_installed"])
        self.assertTrue(result["loaded"])
        self.assertEqual(result["state"], "scheduled")
        self.assertEqual(result["interval_seconds"], 900)


class ServiceCliTests(unittest.TestCase):
    def run_command(self, argv, directory):
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_HOME": directory}):
            args = build_parser().parse_args(argv)
            app = Application(args)
            output = io.StringIO()
            with redirect_stdout(output):
                code = getattr(app, args.handler)()
            return code, output.getvalue()

    def test_dry_run_prints_the_unit_without_touching_the_system(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output = self.run_command(
                ["service", "install", "--dry-run", "--json"], directory
            )
            payload = json.loads(output)
            self.assertEqual(code, 0)
            self.assertTrue(payload["dry_run"])
            self.assertIn("branch_radar", payload["content"])
            rendered = "\n".join(item["content"] for item in payload["units"])
            self.assertIn("--once", rendered)
            self.assertNotIn("--interval", rendered)
            self.assertTrue("900s" in rendered or "<integer>900</integer>" in rendered)
            self.assertFalse(Path(payload["unit"]).exists())

    def test_background_publishing_requires_a_configured_document(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output = self.run_command(
                ["service", "install", "--publish", "--dry-run", "--json"], directory
            )
            self.assertEqual(code, 1)
            self.assertIn("飞书文档", output)

    def test_explicit_label_selects_the_requested_platform_unit(self):
        with tempfile.TemporaryDirectory() as directory:
            code, output = self.run_command(
                [
                    "service",
                    "install",
                    "--label",
                    "local.branch-radar",
                    "--dry-run",
                    "--json",
                ],
                directory,
            )
            payload = json.loads(output)

            self.assertEqual(code, 0)
            unit_name = Path(payload["unit"]).name
            self.assertIn(
                unit_name,
                {"local.branch-radar.plist", "local-branch-radar.service"},
            )
            self.assertNotIn("com-branch-radar", unit_name)


if __name__ == "__main__":
    unittest.main()
