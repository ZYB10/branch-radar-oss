from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..database import utc_now
from ..git import GitClient, GitError, _git_common_directory, is_safe_remote_url
from ..models import ChangeFacts
from ..remote import normalize_remote
from ..rules import analyze_categories
from ..security import redact_sensitive_text
from .base import BranchTransition, FactSource, Watch

_DEFAULT_MAX_WORKERS = 4


@dataclass(frozen=True)
class _Resolution:
    value: str | None = None
    error: Exception | None = None


@dataclass(frozen=True)
class _PreparedGroup:
    repository: str
    block_error: str | None
    resolutions: dict[int, _Resolution]


class GitFactSource(FactSource):
    """Observe branches through local checkouts already on this machine."""

    name = "git"

    def __init__(
        self,
        config: AppConfig,
        git: GitClient | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
        max_workers: int = _DEFAULT_MAX_WORKERS,
    ):
        self.config = config
        self.git = git or GitClient()
        self.progress = progress or (lambda _value: None)
        self.max_workers = max(1, min(int(max_workers), 8))
        self._progress_lock = threading.Lock()
        self._resolve_lock = threading.Lock()
        self._resolve_current = 0
        self._resolve_total = 0
        self._resolutions: dict[int, _Resolution] = {}

    # -- round preparation -------------------------------------------------

    def refresh(self, watches: list[Watch], *, fetch: bool = True) -> dict[int, str]:
        grouped: dict[tuple[int, str], list[Watch]] = {}
        for watch in watches:
            key = _group_key(watch)
            grouped.setdefault(key, []).append(watch)

        blocked: dict[int, str] = {}
        self._resolutions = {}
        if not grouped:
            with self._resolve_lock:
                self._resolve_current = 0
                self._resolve_total = 0
            return blocked

        # Git object databases are shared by worktrees and by remotes in the
        # same checkout. Different repositories may run in parallel, but
        # common-directory work units keep all reads/fetches for one object store
        # ordered and gives each repo/remote exactly one fetch per round. This
        # also prevents one timed-out fetch from mistaking another worktree's
        # temporary pack for its own cleanup target.
        work_units: dict[str, list[tuple[tuple[int, str], list[Watch]]]] = {}
        for key, group in grouped.items():
            lock_key = _lock_key(Path(str(group[0]["local_path"])))
            work_units.setdefault(lock_key, []).append((key, group))

        phase = "fetch" if fetch and any(key[1] for key in grouped) else "inspect"
        total_groups = len(grouped)
        worker_count = min(self.max_workers, len(work_units))
        self._report({
            "phase": phase,
            "label": "正在准备 %d 个仓库" % total_groups,
            "detail": (
                "正在获取并解析仓库引用。"
                if fetch else "正在解析本地仓库引用。"
            ),
            "current": 0,
            "total": total_groups,
            "indeterminate": False,
        })

        completed = 0
        with ThreadPoolExecutor(
            max_workers=worker_count, thread_name_prefix="branch-radar-git"
        ) as executor:
            futures = {
                executor.submit(
                    self._prepare_work_unit,
                    groups,
                    fetch=fetch,
                ): groups
                for groups in work_units.values()
            }
            for future in as_completed(futures):
                for group, prepared in future.result():
                    if prepared.block_error:
                        for watch in group:
                            blocked[int(watch["branch_id"])] = prepared.block_error
                    else:
                        self._resolutions.update(prepared.resolutions)
                    completed += 1
                    self._report({
                        "phase": phase,
                        "label": "已准备 %s" % prepared.repository,
                        "detail": "已完成 %d/%d 个仓库；分支结果将按关注列表顺序处理。"
                        % (completed, total_groups),
                        "current": completed,
                        "total": total_groups,
                        "indeterminate": False,
                    })

        with self._resolve_lock:
            self._resolve_current = 0
            self._resolve_total = sum(
                1 for watch in watches if int(watch["branch_id"]) not in blocked
            )
        return blocked

    def _prepare_work_unit(
        self,
        groups: list[tuple[tuple[int, str], list[Watch]]],
        *,
        fetch: bool,
    ) -> list[tuple[list[Watch], _PreparedGroup]]:
        prepared = []
        for key, group in groups:
            try:
                result = self._prepare_group(key, group, fetch=fetch)
            except Exception as exc:
                # A repository-specific adapter or process failure must not
                # discard observations already prepared for other repos.
                repository = str(group[0].get("repository") or "仓库")
                message = redact_sensitive_text(str(exc) or "未知错误")[:2000]
                result = _PreparedGroup(
                    repository,
                    "准备仓库失败：%s" % message,
                    {},
                )
            prepared.append((group, result))
        return prepared

    def _prepare_group(
        self,
        key: tuple[int, str],
        group: list[Watch],
        *,
        fetch: bool,
    ) -> _PreparedGroup:
        repository = str(group[0].get("repository") or "仓库")
        binding_error = self._validate_remote_binding(key, group)
        if binding_error:
            return _PreparedGroup(repository, binding_error, {})

        remote = key[1]
        if fetch and remote:
            branches = list(dict.fromkeys(str(item["branch"]) for item in group))
            is_initial = any(not item.get("last_observed_oid") for item in group)
            timeout = (
                self.config.initial_fetch_timeout_seconds
                if is_initial
                else self.config.fetch_timeout_seconds
            )
            try:
                self.git.fetch(
                    Path(str(group[0]["local_path"])),
                    remote,
                    timeout=timeout,
                    expected_fingerprint=str(group[0]["remote_fingerprint"]),
                    branches=branches,
                )
            except (GitError, OSError) as exc:
                return _PreparedGroup(
                    repository,
                    "git fetch 失败：%s" % redact_sensitive_text(str(exc)),
                    {},
                )

        resolutions: dict[int, _Resolution] = {}
        for watch in group:
            try:
                value = self.git.branch_ref(
                    Path(str(watch["local_path"])),
                    _remote_name(watch),
                    str(watch["branch"]),
                )
                resolutions[int(watch["branch_id"])] = _Resolution(value=value)
            except (GitError, OSError) as exc:
                resolutions[int(watch["branch_id"])] = _Resolution(error=exc)
        return _PreparedGroup(repository, None, resolutions)

    def _validate_remote_binding(
        self, key: tuple[int, str], group: list[Watch]
    ) -> str | None:
        remote = key[1]
        if not remote:
            return None
        expected_values = {
            str(watch["remote_fingerprint"])
            if watch.get("remote_fingerprint")
            else None
            for watch in group
        }
        if None in expected_values:
            return (
                "remote 绑定缺少 fingerprint；为避免把其他仓库当成原分支，"
                "已拒绝检查。请用 watch add --rebaseline 明确重建基线"
            )
        if len(expected_values) != 1:
            return (
                "同一 remote 存在不一致的 fingerprint；已拒绝 fetch 和游标推进，"
                "请逐一确认后重建基线"
            )

        watch = group[0]
        path = Path(str(watch["local_path"]))
        try:
            raw_url = self.git.remote_url(path, remote)
            if not raw_url:
                raise GitError("无法读取 remote %s 的地址" % remote)
            if not is_safe_remote_url(raw_url):
                raise GitError("remote %s 使用了不受支持的 Git transport" % remote)
            current = normalize_remote(raw_url, base=path).canonical
        except (GitError, OSError, ValueError) as exc:
            return "验证 remote 绑定失败：%s" % redact_sensitive_text(str(exc))

        expected = next(iter(expected_values))
        if current != expected:
            return (
                "remote %s 的仓库身份已变化（%s → %s）；已拒绝自动 fetch 和游标推进。"
                "请核对后使用 watch add --rebaseline 明确重建基线"
                % (remote, expected, current)
            )
        return None

    # -- observation -------------------------------------------------------

    def resolve(self, watch: Watch) -> str | None:
        branch = str(watch["branch"])
        with self._resolve_lock:
            current = self._resolve_current
            total = self._resolve_total
        self._report({
            "phase": "resolve",
            "label": "正在解析分支引用",
            "detail": "核对 %s，并判断是否出现真实变化。" % branch,
            "current": current,
            "total": total,
            "indeterminate": False,
        })
        try:
            with self._resolve_lock:
                resolution = self._resolutions.pop(int(watch["branch_id"]), None)
            if resolution is None:
                return self.git.branch_ref(
                    Path(str(watch["local_path"])), _remote_name(watch), branch
                )
            if resolution.error is not None:
                raise resolution.error
            return resolution.value
        finally:
            with self._resolve_lock:
                self._resolve_current += 1
                current = self._resolve_current
                total = self._resolve_total
            self._report({
                "phase": "resolve",
                "label": "正在解析分支引用",
                "detail": "已核对 %d/%d 个分支。" % (
                    current, total
                ),
                "current": current,
                "total": total,
                "indeterminate": False,
            })

    def _report(self, value: dict[str, Any]) -> None:
        # The callback is commonly a mutable Web job object. Never invoke it
        # from worker threads or concurrently, even if resolve becomes
        # parallel at a later point.
        with self._progress_lock:
            self.progress(value)

    def describe(self, watch: Watch, transition: BranchTransition) -> ChangeFacts:
        path = Path(str(watch["local_path"]))
        if transition.new_sha is None:
            return self._deleted_facts(watch, transition)
        if transition.old_sha is None:
            if transition.is_reappearance and transition.previous_tip and self.git.object_exists(
                path, transition.previous_tip
            ):
                return self._compared_facts(
                    watch, transition, base=transition.previous_tip, kind="recreated"
                )
            return self._unanchored_facts(watch, transition)
        if not self.git.object_exists(path, transition.old_sha):
            return self._history_gap_facts(watch, transition)
        return self._compared_facts(
            watch,
            transition,
            base=transition.old_sha,
            kind=self._classify(path, transition.old_sha, transition.new_sha),
        )

    def attach_patch_excerpt(
        self, watch: Watch, facts: ChangeFacts, *, max_bytes: int
    ) -> None:
        path = Path(str(watch["local_path"]))
        base = facts.previous_tip if facts.event_kind == "recreated" else facts.old_sha
        if not base or not facts.new_sha:
            return
        if not self.git.object_exists(path, base):
            return
        facts.patch_excerpt = self.git.patch_excerpt(
            path, base, facts.new_sha, max_bytes
        )

    # -- classification helpers -------------------------------------------

    def _classify(self, path: Path, old_sha: str, new_sha: str) -> str:
        if self.git.is_ancestor(path, old_sha, new_sha):
            return "fast_forward"
        if self.git.is_ancestor(path, new_sha, old_sha):
            return "rewind"
        return "force_push"

    def _base_facts(
        self, watch: Watch, kind: str, **overrides: Any
    ) -> ChangeFacts:
        path = Path(str(watch["local_path"]))
        return ChangeFacts(
            repository_name=str(watch["repository"]),
            repository_path=str(path),
            remote_url=self.event_remote_url(watch),
            branch=str(watch["branch"]),
            event_kind=kind,
            old_sha=overrides.pop("old_sha", None),
            new_sha=overrides.pop("new_sha", None),
            merge_base=overrides.pop("merge_base", None),
            detected_at=utc_now(),
            **overrides,
        )

    def _deleted_facts(self, watch: Watch, transition: BranchTransition) -> ChangeFacts:
        return self._base_facts(watch, "deleted", old_sha=transition.old_sha)

    def _compared_facts(
        self, watch: Watch, transition: BranchTransition, *, base: str, kind: str
    ) -> ChangeFacts:
        path = Path(str(watch["local_path"]))
        new_sha = str(transition.new_sha)
        merge_base = self.git.merge_base(path, base, new_sha)
        commits = self.git.commits_between(path, base, new_sha)
        commit_count = self.git.commit_count_between(path, base, new_sha)
        dropped = []
        dropped_count = 0
        drops_history = (
            kind in {"rewind", "force_push"}
            or (kind == "recreated" and not self.git.is_ancestor(path, base, new_sha))
        )
        if drops_history:
            dropped = self.git.commits_not_reachable(path, base, new_sha)
            dropped_count = self.git.commit_count_not_reachable(path, base, new_sha)
        diff = self.git.diff_stats(path, base, new_sha)
        categories, signals = analyze_categories(diff.files, commits)
        return self._base_facts(
            watch,
            kind,
            old_sha=None if kind == "recreated" else base,
            new_sha=new_sha,
            merge_base=merge_base,
            previous_tip=transition.previous_tip if kind == "recreated" else None,
            commit_count=commit_count,
            dropped_commit_count=dropped_count,
            commits_truncated=commit_count > len(commits),
            dropped_commits_truncated=dropped_count > len(dropped),
            commits=commits,
            dropped_commits=dropped,
            diff=diff,
            categories=categories,
            signals=signals,
        )

    def _unanchored_facts(
        self, watch: Watch, transition: BranchTransition
    ) -> ChangeFacts:
        """First sight of a branch, or a reappearance whose old objects are gone."""
        path = Path(str(watch["local_path"]))
        kind = "recreated" if transition.is_reappearance else "baseline"
        commits = self.git.commits_between(path, None, str(transition.new_sha), limit=1)
        facts = self._base_facts(
            watch,
            kind,
            new_sha=transition.new_sha,
            previous_tip=transition.previous_tip if kind == "recreated" else None,
            analysis_complete=kind != "recreated",
            commit_count=len(commits) if kind != "recreated" else None,
            commits_are_tip_sample=kind == "recreated",
            commits=commits,
        )
        facts.categories, facts.signals = analyze_categories([], commits)
        if kind == "recreated" and transition.previous_tip:
            facts.signals.append(
                "删除前的提交对象已被本地 Git 清理，无法计算完整重建 diff"
            )
        return facts

    def _history_gap_facts(
        self, watch: Watch, transition: BranchTransition
    ) -> ChangeFacts:
        """The previous tip is no longer in the object store; net diff is unknowable."""
        path = Path(str(watch["local_path"]))
        commits = self.git.commits_between(path, None, str(transition.new_sha), limit=20)
        categories, signals = analyze_categories([], commits)
        signals.append("上一次提交对象已被本地 Git 清理，无法计算完整净 diff")
        return self._base_facts(
            watch,
            "history_gap",
            old_sha=transition.old_sha,
            new_sha=transition.new_sha,
            analysis_complete=False,
            commit_count=None,
            commits_are_tip_sample=True,
            commits=commits,
            categories=categories,
            signals=signals,
        )

    def event_remote_url(self, watch: Watch) -> str | None:
        remote_name = _remote_name(watch)
        if not remote_name:
            return None
        path = Path(str(watch["local_path"]))
        try:
            raw = self.git.remote_url(path, remote_name)
            if not raw or not is_safe_remote_url(raw):
                return None
            return normalize_remote(raw, base=path).sanitized_url
        except (GitError, OSError, ValueError):
            return None


def _remote_name(watch: Watch) -> str | None:
    return str(watch["remote_name"]) if watch.get("remote_name") else None


def _group_key(watch: Watch) -> tuple[int, str]:
    return (
        int(watch["repository_id"]),
        str(watch["remote_name"]) if watch.get("remote_name") else "",
    )


def _lock_key(path: Path) -> str:
    common_directory = _git_common_directory(path)
    if common_directory is not None:
        return str(common_directory)
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())
