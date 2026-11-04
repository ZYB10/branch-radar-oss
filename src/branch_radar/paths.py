from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    home: Path
    config: Path
    database: Path
    logs: Path
    cache: Path
    analysis_workspace: Path

    @classmethod
    def discover(cls) -> AppPaths:
        configured = os.environ.get("BRANCH_RADAR_HOME")
        home = Path(configured).expanduser() if configured else Path.home() / ".branch-radar"
        return cls(
            home=home,
            config=home / "config.json",
            database=home / "state.db",
            logs=home / "logs",
            cache=home / "cache",
            analysis_workspace=home / "analysis-workspace",
        )

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.analysis_workspace.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            for directory in (
                self.home, self.logs, self.cache, self.analysis_workspace
            ):
                directory.chmod(0o700)
