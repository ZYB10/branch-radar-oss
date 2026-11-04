from __future__ import annotations

import os
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .git import GitClient, GitError, is_safe_remote_url
from .models import RepositoryCandidate
from .remote import normalize_remote
from .security import redact_sensitive_text

_PRUNE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "target",
    ".cache",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "Library",
}

_DEFAULT_MAX_WORKERS = 4


class RepositoryScanner:
    def __init__(
        self,
        git: GitClient | None = None,
        *,
        max_workers: int = _DEFAULT_MAX_WORKERS,
    ):
        self.git = git or GitClient()
        self.max_workers = max(1, min(int(max_workers), 8))

    def scan(self, roots: Iterable[Path], *, max_depth: int = 5) -> list[RepositoryCandidate]:
        repository_paths: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            expanded = root.expanduser()
            if not expanded.exists() or not expanded.is_dir():
                continue
            for repository in self._find_roots(expanded, max_depth=max_depth):
                try:
                    key = str(repository.resolve())
                except OSError:
                    key = str(repository.absolute())
                if key in seen:
                    continue
                seen.add(key)
                repository_paths.append(repository)
        if len(repository_paths) == 1:
            inspected = [self._inspect_safely(repository_paths[0])]
        elif repository_paths:
            worker_count = min(self.max_workers, len(repository_paths))
            with ThreadPoolExecutor(
                max_workers=worker_count, thread_name_prefix="branch-radar-scan"
            ) as executor:
                inspected = list(executor.map(self._inspect_safely, repository_paths))
        else:
            inspected = []
        inspected.sort(key=lambda item: (item.name.lower(), item.path.lower()))
        return inspected

    def _inspect_safely(self, path: Path) -> RepositoryCandidate:
        try:
            return self.inspect(path)
        except Exception as exc:
            # One unusual checkout must not hide every other discovery result.
            # The scanner returns a bounded DTO, never a traceback.
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path.absolute()
            return RepositoryCandidate(
                name=resolved.name,
                path=str(resolved),
                remote_name=None,
                remote_url=None,
                canonical_remote=None,
                current_branch=None,
                upstream_branch=None,
                default_branch=None,
                error="检查仓库失败：%s" % redact_sensitive_text(str(exc)),
            )

    def _find_roots(self, root: Path, *, max_depth: int) -> Iterable[Path]:
        if (root / ".git").exists():
            yield root
            return
        try:
            iterator = os.walk(str(root), topdown=True, followlinks=False)
            for current, directories, _files in iterator:
                current_path = Path(current)
                try:
                    relative = current_path.relative_to(root)
                    depth = len(relative.parts)
                except ValueError:
                    continue
                directories[:] = [
                    name
                    for name in directories
                    if name not in _PRUNE_DIRS and not (name.startswith(".") and name != ".git")
                ]
                marker = current_path / ".git"
                if marker.exists():
                    yield current_path
                    directories[:] = []
                    continue
                if depth >= max_depth:
                    directories[:] = []
        except OSError:
            return

    def inspect(self, path: Path) -> RepositoryCandidate:
        resolved = path.resolve()
        try:
            root = self.git.repository_root(resolved)
            remote_name = self.git.select_remote(root)
            remote_raw = self.git.remote_url(root, remote_name) if remote_name else None
            remote_url = None
            canonical = None
            name = root.name
            if remote_raw:
                if not is_safe_remote_url(remote_raw):
                    raise ValueError("远程地址使用了不受支持的 Git transport，已拒绝读取和保存")
                normalized = normalize_remote(remote_raw, base=root)
                remote_url = normalized.sanitized_url
                canonical = normalized.canonical
                name = normalized.project_path.rsplit("/", 1)[-1] or name
            current = self.git.current_branch(root)
            upstream = self.git.upstream(root)
            upstream_branch = upstream
            if upstream and remote_name and upstream.startswith(remote_name + "/"):
                upstream_branch = upstream[len(remote_name) + 1 :]
            branches = self.git.list_branches(root, remote_name)[:200]
            return RepositoryCandidate(
                name=name,
                path=str(root),
                remote_name=remote_name,
                remote_url=remote_url,
                canonical_remote=canonical,
                current_branch=current,
                upstream_branch=upstream_branch,
                default_branch=self.git.default_branch(root, remote_name),
                is_worktree=self.git.is_worktree(root),
                branches=branches,
            )
        except (GitError, OSError, ValueError) as exc:
            return RepositoryCandidate(
                name=resolved.name,
                path=str(resolved),
                remote_name=None,
                remote_url=None,
                canonical_remote=None,
                current_branch=None,
                upstream_branch=None,
                default_branch=None,
                error=redact_sensitive_text(str(exc)),
            )
