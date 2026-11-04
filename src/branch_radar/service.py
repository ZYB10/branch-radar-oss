from __future__ import annotations

import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path

from .paths import AppPaths
from .process import CommandRunner

LEGACY_DEFAULT_LABEL = "com.branch-radar.agent"
DEFAULT_DAEMON_LABEL = "com.branch-radar.daemon"
DEFAULT_WEB_LABEL = "com.branch-radar.web"
# Kept as the daemon default for callers that imported this historical symbol.
DEFAULT_LABEL = DEFAULT_DAEMON_LABEL
DEFAULT_LABELS = {
    "daemon": DEFAULT_DAEMON_LABEL,
    "web": DEFAULT_WEB_LABEL,
}
DEFAULT_INTERVAL_SECONDS = 900
SUPPORTED_MODES = ("daemon", "web")


class ServiceInstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceSpec:
    """What the background unit should run."""

    mode: str = "daemon"
    interval: int | None = None
    use_codex: bool = False
    publish: bool = False
    port: int = 8765
    # None means the stable mode-specific default.  Keeping this distinction
    # lets migrations clean the historical shared label without touching an
    # explicitly named user service.
    label: str | None = None

    def validate(self) -> None:
        if self.mode not in SUPPORTED_MODES:
            raise ServiceInstallError(
                "不支持的服务模式 %s；可选 %s" % (self.mode, "、".join(SUPPORTED_MODES))
            )
        if self.interval is not None and self.interval < 10:
            raise ServiceInstallError("轮询间隔不能小于 10 秒")
        if not 0 <= self.port <= 65535:
            raise ServiceInstallError("端口必须在 0 到 65535 之间")
        if not self.resolved_label:
            raise ServiceInstallError("服务标签不能为空")
        if any(character in self.resolved_label for character in "/ \t\n"):
            raise ServiceInstallError("服务标签不能包含空白或路径分隔符")

    def command(self, executable: str) -> list[str]:
        # `-m branch_radar` rather than the console script: it pins the unit to
        # the interpreter that installed it, which survives a renamed venv.
        argv = [executable, "-m", "branch_radar", self.mode]
        if self.mode == "daemon":
            # The operating system owns recurrence.  Each invocation performs
            # one bounded check and exits instead of keeping a Python process
            # resident merely to sleep between checks.
            argv.append("--once")
            if self.use_codex:
                argv.append("--codex")
            if self.publish:
                argv.append("--publish")
            argv.append("--json")
        else:
            argv += ["--port", str(self.port), "--no-open"]
        return argv

    @property
    def interval_seconds(self) -> int:
        return self.interval or DEFAULT_INTERVAL_SECONDS

    @property
    def resolved_label(self) -> str:
        if self.label is not None:
            return self.label
        return DEFAULT_LABELS.get(self.mode, DEFAULT_DAEMON_LABEL)

    @property
    def uses_default_label(self) -> bool:
        return self.label is None


def service_environment(paths: AppPaths, home: Path) -> dict[str, str]:
    """The minimum a launch agent needs that a login shell would have given it.

    Background units start with a bare environment, so an inherited PATH is
    what keeps `git`, `codex` and `lark-cli` discoverable at all.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": str(home),
        # AppPaths has already resolved the state directory used by the
        # installing CLI.  Pin that authoritative value even when no override
        # was present in the installer's environment.
        "BRANCH_RADAR_HOME": str(paths.home),
    }
    for name in (
        "LANG",
        "LC_ALL",
        "SSH_AUTH_SOCK",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


class ServiceManager:
    """Generate and control a per-user background unit."""

    platform = "unknown"

    def __init__(
        self,
        paths: AppPaths,
        spec: ServiceSpec,
        *,
        runner: CommandRunner | None = None,
        executable: str | None = None,
        home: Path | None = None,
    ):
        self.paths = paths
        self.spec = spec
        self.runner = runner or CommandRunner()
        self.executable = executable or sys.executable
        self.home = home or Path(os.environ.get("HOME") or Path.home())

    @property
    def unit_path(self) -> Path:
        raise NotImplementedError

    @property
    def stdout_log(self) -> Path:
        return self.paths.logs / ("%s.out.log" % self.spec.mode)

    @property
    def stderr_log(self) -> Path:
        return self.paths.logs / ("%s.err.log" % self.spec.mode)

    def render(self) -> str:
        raise NotImplementedError

    def unit_definitions(self) -> dict[Path, str]:
        """Every file needed to install the service, keyed by destination."""
        return {self.unit_path: self.render()}

    def install(self) -> dict[str, object]:
        raise NotImplementedError

    def uninstall(self) -> dict[str, object]:
        raise NotImplementedError

    def status(self) -> dict[str, object]:
        raise NotImplementedError

    def _write_unit(self) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        for path, content in self.unit_definitions().items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            os.chmod(path, 0o600)

    def _run(self, argv: list[str], timeout: float = 30) -> tuple[bool, str]:
        result = self.runner.run(argv, timeout=timeout)
        detail = (result.stderr or result.stdout).strip()
        return result.ok, detail


class LaunchdManager(ServiceManager):
    platform = "launchd"

    @property
    def unit_path(self) -> Path:
        return (
            self.home
            / "Library"
            / "LaunchAgents"
            / ("%s.plist" % self.spec.resolved_label)
        )

    @property
    def legacy_unit_path(self) -> Path:
        return (
            self.home / "Library" / "LaunchAgents" / ("%s.plist" % LEGACY_DEFAULT_LABEL)
        )

    @property
    def domain_target(self) -> str:
        return "gui/%d" % os.getuid()

    @property
    def service_target(self) -> str:
        return "%s/%s" % (self.domain_target, self.spec.resolved_label)

    @property
    def legacy_service_target(self) -> str:
        return "%s/%s" % (self.domain_target, LEGACY_DEFAULT_LABEL)

    def render(self) -> str:
        definition: dict[str, object] = {
            "Label": self.spec.resolved_label,
            "ProgramArguments": self.spec.command(self.executable),
            "EnvironmentVariables": service_environment(self.paths, self.home),
            "ProcessType": "Background",
            "StandardOutPath": str(self.stdout_log),
            "StandardErrorPath": str(self.stderr_log),
            "WorkingDirectory": str(self.home),
        }
        if self.spec.mode == "daemon":
            definition["StartInterval"] = self.spec.interval_seconds
            # Do not let launchd's crash throttle silently stretch an
            # explicitly configured sub-minute schedule.
            definition["ThrottleInterval"] = min(60, self.spec.interval_seconds)
        else:
            # Web mode is intentionally resident; only periodic checks switch
            # to one-shot processes managed by the operating system timer.
            definition["RunAtLoad"] = True
            definition["KeepAlive"] = {"SuccessfulExit": False}
            definition["ThrottleInterval"] = 60
        return plistlib.dumps(definition).decode("utf-8")

    def install(self) -> dict[str, object]:
        self._write_unit()
        # `bootout` first so a re-install replaces the running definition
        # instead of erroring with "service already loaded".
        self._run(["launchctl", "bootout", self.service_target])
        ok, detail = self._run(
            ["launchctl", "bootstrap", self.domain_target, str(self.unit_path)]
        )
        if not ok:
            ok, detail = self._run(["launchctl", "load", "-w", str(self.unit_path)])
        if not ok:
            raise ServiceInstallError(
                "已写入 %s，但加载失败：%s" % (self.unit_path, detail or "未知错误")
            )
        self._run(["launchctl", "enable", self.service_target])
        # Retire the same-mode historical unit only after its replacement is
        # loaded, so a failed upgrade leaves the working service untouched.
        self._remove_matching_legacy_unit()
        return {
            "platform": self.platform,
            "unit": str(self.unit_path),
            "label": self.spec.resolved_label,
            "loaded": True,
            "interval_seconds": (
                self.spec.interval_seconds if self.spec.mode == "daemon" else None
            ),
        }

    def uninstall(self) -> dict[str, object]:
        existed = self.unit_path.exists()
        self._run(["launchctl", "bootout", self.service_target])
        if not existed:
            self._run(["launchctl", "remove", self.spec.resolved_label])
        if existed:
            self.unit_path.unlink()
        legacy_removed = self._remove_matching_legacy_unit()
        return {
            "platform": self.platform,
            "unit": str(self.unit_path),
            "removed": existed or legacy_removed,
        }

    def status(self) -> dict[str, object]:
        unit_path = self.unit_path
        target = self.service_target
        legacy = False
        ok, detail = self._run(["launchctl", "print", target])
        if not ok and self._legacy_unit_mode() == self.spec.mode:
            legacy_ok, legacy_detail = self._run(
                ["launchctl", "print", self.legacy_service_target]
            )
            if legacy_ok or self.legacy_unit_path.exists():
                unit_path = self.legacy_unit_path
                legacy = True
                ok, detail = legacy_ok, legacy_detail
        running_pid = None
        if ok:
            for line in detail.splitlines():
                stripped = line.strip()
                if stripped.startswith("pid = "):
                    running_pid = stripped.split("=", 1)[1].strip()
                    break
        return {
            "platform": self.platform,
            "unit": str(unit_path),
            "installed": self.unit_path.exists()
            or (
                self.spec.uses_default_label
                and self._legacy_unit_mode() == self.spec.mode
            ),
            "loaded": ok,
            "legacy": legacy,
            "pid": running_pid,
            "state": (
                "running"
                if running_pid
                else (
                    "scheduled"
                    if ok and self.spec.mode == "daemon"
                    else "loaded"
                    if ok
                    else "inactive"
                )
            ),
            "interval_seconds": (
                self.spec.interval_seconds if self.spec.mode == "daemon" else None
            ),
            "stdout_log": str(self.stdout_log),
            "stderr_log": str(self.stderr_log),
        }

    def _legacy_unit_mode(self) -> str | None:
        if not self.spec.uses_default_label:
            return None
        try:
            definition = plistlib.loads(self.legacy_unit_path.read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(definition, dict):
            return None
        return _command_mode(definition.get("ProgramArguments"))

    def _remove_matching_legacy_unit(self) -> bool:
        if self._legacy_unit_mode() != self.spec.mode:
            return False
        existed = self.legacy_unit_path.exists()
        self._run(["launchctl", "bootout", self.legacy_service_target])
        if existed:
            self.legacy_unit_path.unlink()
        return existed


class SystemdUserManager(ServiceManager):
    platform = "systemd"

    @property
    def unit_name(self) -> str:
        return "%s.service" % self.spec.resolved_label.replace(".", "-")

    @property
    def unit_path(self) -> Path:
        return self.home / ".config" / "systemd" / "user" / self.unit_name

    @property
    def timer_name(self) -> str:
        return "%s.timer" % self.spec.resolved_label.replace(".", "-")

    @property
    def timer_path(self) -> Path:
        return self.unit_path.with_name(self.timer_name)

    @property
    def legacy_unit_name(self) -> str:
        return "%s.service" % LEGACY_DEFAULT_LABEL.replace(".", "-")

    @property
    def legacy_unit_path(self) -> Path:
        return self.unit_path.with_name(self.legacy_unit_name)

    @property
    def legacy_timer_name(self) -> str:
        return "%s.timer" % LEGACY_DEFAULT_LABEL.replace(".", "-")

    @property
    def legacy_timer_path(self) -> Path:
        return self.unit_path.with_name(self.legacy_timer_name)

    def render(self) -> str:
        environment = service_environment(self.paths, self.home)
        lines = [
            "[Unit]",
            "Description=Branch Radar (%s)" % self.spec.mode,
            "After=network-online.target",
            "",
            "[Service]",
            "Type=%s" % ("oneshot" if self.spec.mode == "daemon" else "simple"),
            "ExecStart=%s"
            % " ".join(_quote(part) for part in self.spec.command(self.executable)),
            "WorkingDirectory=%s" % self.home,
            "StandardOutput=append:%s" % self.stdout_log,
            "StandardError=append:%s" % self.stderr_log,
        ]
        if self.spec.mode == "web":
            lines += ["Restart=on-failure", "RestartSec=60"]
        lines += [
            'Environment="%s=%s"' % (key, _escape_systemd_environment(value))
            for key, value in sorted(environment.items())
        ]
        if self.spec.mode == "web":
            lines += ["", "[Install]", "WantedBy=default.target", ""]
        else:
            lines.append("")
        return "\n".join(lines)

    def render_timer(self) -> str:
        if self.spec.mode != "daemon":
            raise ServiceInstallError("web 模式不使用 systemd timer")
        lines = [
            "[Unit]",
            "Description=Branch Radar scheduled checks",
            "",
            "[Timer]",
        ]
        # Both triggers use the configured interval. The first check therefore
        # cannot race the settings request that installed this timer, and each
        # later interval starts only after the previous one has finished.
        lines += [
            "OnActiveSec=%ss" % self.spec.interval_seconds,
            "OnUnitInactiveSec=%ss" % self.spec.interval_seconds,
        ]
        lines += [
            "AccuracySec=1s",
            "Unit=%s" % self.unit_name,
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
        return "\n".join(lines)

    def unit_definitions(self) -> dict[Path, str]:
        definitions = super().unit_definitions()
        if self.spec.mode == "daemon":
            definitions[self.timer_path] = self.render_timer()
        return definitions

    def install(self) -> dict[str, object]:
        self._write_unit()
        for path in self.unit_definitions():
            os.chmod(path, 0o644)

        # Stop both possible activation units before reloading.  This migrates
        # the old resident daemon, applies changed timer intervals immediately,
        # and makes switching between daemon and web modes deterministic.
        self._run(["systemctl", "--user", "disable", "--now", self.timer_name])
        self._run(["systemctl", "--user", "disable", "--now", self.unit_name])
        if self.spec.mode == "web" and self.timer_path.exists():
            self._run(
                [
                    "systemctl",
                    "--user",
                    "clean",
                    "--what=state",
                    self.timer_name,
                ]
            )
            self.timer_path.unlink()
        self._run(["systemctl", "--user", "daemon-reload"])
        activation_name = (
            self.timer_name if self.spec.mode == "daemon" else self.unit_name
        )
        ok, detail = self._run(
            ["systemctl", "--user", "enable", "--now", activation_name]
        )
        if not ok:
            raise ServiceInstallError(
                "已写入 %s，但启用失败：%s" % (self.unit_path, detail or "未知错误")
            )
        # Retire the same-mode historical unit only after its replacement is
        # active.  A second reload makes removal visible to the user manager.
        if self._remove_matching_legacy_units():
            self._run(["systemctl", "--user", "daemon-reload"])
        return {
            "platform": self.platform,
            "unit": str(self.unit_path),
            "label": self.unit_name,
            "loaded": True,
            "timer_unit": (
                str(self.timer_path) if self.spec.mode == "daemon" else None
            ),
            "interval_seconds": (
                self.spec.interval_seconds if self.spec.mode == "daemon" else None
            ),
            # Without lingering the unit stops at logout; enabling it needs
            # privileges this command deliberately does not take.
            "note": "如需登出后继续运行，请手动执行 loginctl enable-linger $USER",
        }

    def uninstall(self) -> dict[str, object]:
        # An explicit custom label may have been reused for the other mode;
        # clean only that same label.  Mode-specific defaults remain isolated.
        timer_existed = self.timer_path.exists()
        existed = self.unit_path.exists() or timer_existed
        self._run(["systemctl", "--user", "disable", "--now", self.timer_name])
        # Also stops a one-shot currently in flight and cleans up legacy
        # resident installations that predate the timer unit.
        self._run(["systemctl", "--user", "disable", "--now", self.unit_name])
        if timer_existed:
            self._run(
                [
                    "systemctl",
                    "--user",
                    "clean",
                    "--what=state",
                    self.timer_name,
                ]
            )
        if self.unit_path.exists():
            self.unit_path.unlink()
        if timer_existed:
            self.timer_path.unlink()
        legacy_removed = self._remove_matching_legacy_units()
        self._run(["systemctl", "--user", "daemon-reload"])
        return {
            "platform": self.platform,
            "unit": str(self.unit_path),
            "removed": existed or legacy_removed,
            "timer_unit": (
                str(self.timer_path) if self.spec.mode == "daemon" else None
            ),
        }

    def status(self) -> dict[str, object]:
        if self.spec.mode == "daemon":
            unit_path = self.unit_path
            timer_path = self.timer_path
            unit_name = self.unit_name
            timer_name = self.timer_name
            legacy = False
            timer_ok, timer_detail = self._run(
                ["systemctl", "--user", "is-active", timer_name], timeout=15
            )
            service_ok, service_detail = self._run(
                ["systemctl", "--user", "is-active", unit_name], timeout=15
            )
            service_running = _systemd_service_running(service_ok, service_detail)
            timer_loaded = _systemd_timer_loaded(timer_ok, timer_detail)
            loaded = timer_loaded or service_running
            legacy_mode = self._legacy_unit_mode()
            if not loaded and legacy_mode == self.spec.mode:
                legacy_timer_ok, legacy_timer_detail = self._run(
                    [
                        "systemctl",
                        "--user",
                        "is-active",
                        self.legacy_timer_name,
                    ],
                    timeout=15,
                )
                legacy_service_ok, legacy_service_detail = self._run(
                    [
                        "systemctl",
                        "--user",
                        "is-active",
                        self.legacy_unit_name,
                    ],
                    timeout=15,
                )
                legacy_service_running = _systemd_service_running(
                    legacy_service_ok, legacy_service_detail
                )
                legacy_timer_loaded = _systemd_timer_loaded(
                    legacy_timer_ok, legacy_timer_detail
                )
                if (
                    legacy_service_running
                    or legacy_timer_loaded
                    or self.legacy_unit_path.exists()
                    or self.legacy_timer_path.exists()
                ):
                    unit_path = self.legacy_unit_path
                    timer_path = self.legacy_timer_path
                    legacy = True
                    timer_ok, timer_detail = legacy_timer_ok, legacy_timer_detail
                    service_ok, service_detail = (
                        legacy_service_ok,
                        legacy_service_detail,
                    )
                    service_running = legacy_service_running
                    timer_loaded = legacy_timer_loaded
                    loaded = timer_loaded or service_running
            installed = (
                self.unit_path.exists()
                or self.timer_path.exists()
                or (
                    legacy_mode == self.spec.mode
                    and (
                        self.legacy_unit_path.exists()
                        or self.legacy_timer_path.exists()
                    )
                )
            )
            state = (
                "running"
                if service_running
                else "scheduled"
                if timer_loaded
                else timer_detail or service_detail or "inactive"
            )
            return {
                "platform": self.platform,
                "unit": str(unit_path),
                "timer_unit": str(timer_path),
                "installed": installed,
                "timer_installed": timer_path.exists(),
                "loaded": loaded,
                "legacy": legacy,
                "state": state,
                "service_state": service_detail
                or ("active" if service_ok else "inactive"),
                "timer_state": timer_detail or ("active" if timer_ok else "inactive"),
                "interval_seconds": self.spec.interval_seconds,
                "stdout_log": str(self.stdout_log),
                "stderr_log": str(self.stderr_log),
            }
        unit_path = self.unit_path
        unit_name = self.unit_name
        legacy = False
        ok, detail = self._run(
            ["systemctl", "--user", "is-active", unit_name], timeout=15
        )
        legacy_mode = self._legacy_unit_mode()
        if not ok and legacy_mode == self.spec.mode:
            legacy_ok, legacy_detail = self._run(
                ["systemctl", "--user", "is-active", self.legacy_unit_name],
                timeout=15,
            )
            if legacy_ok or self.legacy_unit_path.exists():
                unit_path = self.legacy_unit_path
                legacy = True
                ok, detail = legacy_ok, legacy_detail
        return {
            "platform": self.platform,
            "unit": str(unit_path),
            "installed": self.unit_path.exists()
            or (legacy_mode == self.spec.mode and self.legacy_unit_path.exists()),
            "loaded": ok,
            "legacy": legacy,
            "state": detail or ("active" if ok else "inactive"),
            "stdout_log": str(self.stdout_log),
            "stderr_log": str(self.stderr_log),
        }

    def _legacy_unit_mode(self) -> str | None:
        if not self.spec.uses_default_label:
            return None
        try:
            content = self.legacy_unit_path.read_text(encoding="utf-8")
        except OSError:
            return None
        return _service_file_mode(content)

    def _remove_matching_legacy_units(self) -> bool:
        if self._legacy_unit_mode() != self.spec.mode:
            return False
        service_existed = self.legacy_unit_path.exists()
        timer_existed = self.legacy_timer_path.exists()
        self._run(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                self.legacy_timer_name,
            ]
        )
        self._run(
            [
                "systemctl",
                "--user",
                "disable",
                "--now",
                self.legacy_unit_name,
            ]
        )
        if timer_existed:
            self._run(
                [
                    "systemctl",
                    "--user",
                    "clean",
                    "--what=state",
                    self.legacy_timer_name,
                ]
            )
            self.legacy_timer_path.unlink()
        if service_existed:
            self.legacy_unit_path.unlink()
        return service_existed or timer_existed


def _quote(value: str) -> str:
    return '"%s"' % value if " " in value else value


def _escape_systemd_environment(value: str) -> str:
    """Encode an Environment= value inside a systemd double-quoted string."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("%", "%%")
    )


def _command_mode(arguments: object) -> str | None:
    if not isinstance(arguments, list):
        return None
    values = [str(item) for item in arguments]
    for index, value in enumerate(values[:-1]):
        if value in {"branch_radar", "branch-radar"}:
            mode = values[index + 1]
            if mode in SUPPORTED_MODES:
                return mode
    return None


def _service_file_mode(content: str) -> str | None:
    for executable in ("branch_radar", "branch-radar"):
        for mode in SUPPORTED_MODES:
            if "%s %s" % (executable, mode) in content:
                return mode
    return None


def _systemd_service_running(ok: bool, detail: str) -> bool:
    return ok or detail in {"active", "activating", "reloading"}


def _systemd_timer_loaded(ok: bool, detail: str) -> bool:
    return ok or detail in {"active", "activating"}


def build_manager(
    paths: AppPaths,
    spec: ServiceSpec,
    *,
    platform: str | None = None,
    runner: CommandRunner | None = None,
    executable: str | None = None,
    home: Path | None = None,
) -> ServiceManager:
    spec.validate()
    name = platform or sys.platform
    manager: type[ServiceManager]
    if name.startswith("darwin"):
        manager = LaunchdManager
    elif name.startswith("linux"):
        manager = SystemdUserManager
    else:
        raise ServiceInstallError(
            "当前平台 %s 暂不支持自动安装后台服务；请手动托管 branch-radar %s"
            % (name, spec.mode)
        )
    return manager(paths, spec, runner=runner, executable=executable, home=home)
