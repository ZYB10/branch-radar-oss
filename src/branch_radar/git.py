from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .models import CommitInfo, DiffStats
from .process import CommandRunner, ProcessResult
from .remote import normalize_remote, parse_github_clone_locator
from .security import redact_sensitive_text


class GitError(RuntimeError):
    pass


class GitCloneCancelled(GitError):
    pass


class GitOperationCancelled(GitError):
    pass


class GitOperationDeadlineExceeded(GitError):
    pass


class GitClient:
    def __init__(self, runner: CommandRunner | None = None, binary: str | None = None):
        self.runner = runner or CommandRunner()
        self.binary = binary or shutil.which("git") or "git"
        self._execution = threading.local()

    @contextmanager
    def execution_limits(
        self,
        *,
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ) -> Iterator[None]:
        previous = getattr(self._execution, "limits", None)
        self._execution.limits = (cancel_event, deadline)
        try:
            yield
        finally:
            if previous is None:
                del self._execution.limits
            else:
                self._execution.limits = previous

    def run(
        self,
        repository: Path,
        args: Sequence[str],
        *,
        timeout: float = 30,
        check: bool = True,
        env: Mapping[str, str] | None = None,
        stdin: str | None = None,
        stdout_limit_bytes: int = 16_777_216,
        stderr_limit_bytes: int = 2_097_152,
    ) -> ProcessResult:
        cancel_event: threading.Event | None = None
        deadline: float | None = None
        limits = getattr(self._execution, "limits", None)
        if limits is not None:
            cancel_event, deadline = limits
        if cancel_event is not None and cancel_event.is_set():
            raise GitOperationCancelled("Git 操作已取消")
        effective_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitOperationDeadlineExceeded("Git 操作超过统一截止时间")
            effective_timeout = min(timeout, remaining)
        result = self.runner.run_limited(
            [self.binary, "-C", str(repository), *list(args)],
            timeout=effective_timeout,
            stdin=stdin,
            env=_git_environment(env),
            replace_env=True,
            cancel_event=cancel_event,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
        if result.cancelled:
            raise GitOperationCancelled("Git 操作已取消")
        if result.timed_out and deadline is not None:
            raise GitOperationDeadlineExceeded("Git 操作超过统一截止时间")
        if check and not result.ok:
            detail = (result.stderr or result.stdout).strip()
            if result.timed_out:
                detail = "命令超时"
            elif result.stdout_truncated:
                detail = "Git 输出超过 %s 字节安全上限" % stdout_limit_bytes
            raise GitError(detail or "git command failed")
        return result

    def clone_repository(
        self,
        remote_url: str,
        destination: Path,
        *,
        timeout: float = 600,
        progress: Callable[[str, str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Clone one validated GitHub repository into a new directory."""
        try:
            remote = parse_github_clone_locator(remote_url)
        except ValueError as exc:
            raise GitError(str(exc)) from exc
        if destination.exists() or destination.is_symlink():
            raise GitError("克隆目标已经存在")
        try:
            parent = destination.parent.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise GitError("克隆目标的父目录不存在或无法读取") from exc
        if not parent.is_dir():
            raise GitError("克隆目标的父路径不是目录")
        if timeout <= 0:
            raise GitError("克隆超时必须大于零")

        safety_config = [
            "-c", "protocol.allow=never",
            "-c", "protocol.ssh.allow=always",
            "-c", "protocol.http.allow=never",
            "-c", "protocol.https.allow=always",
            "-c", "protocol.git.allow=never",
            "-c", "protocol.file.allow=never",
            "-c", "protocol.ext.allow=never",
            "-c", "core.sshCommand=ssh -o BatchMode=yes",
            "-c", "core.hooksPath=%s" % os.devnull,
            "-c", "fetch.pruneTags=false",
        ]
        environment = _git_environment({
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "LC_ALL": "C",
        })
        result = self.runner.run_limited(
            [
                self.binary,
                *safety_config,
                "clone",
                # Keep every branch and the complete commit graph, while letting
                # the remote defer historical file contents until Git needs them.
                "--filter=blob:none",
                "--no-tags",
                "--no-recurse-submodules",
                "--no-local",
                "--progress",
                "--",
                remote.sanitized_url,
                str(destination),
            ],
            timeout=timeout,
            env=environment,
            replace_env=True,
            cancel_event=cancel_event,
            progress=progress,
            stdout_limit_bytes=2_097_152,
            stderr_limit_bytes=2_097_152,
        )
        if result.ok:
            return
        if result.cancelled:
            raise GitCloneCancelled("仓库克隆已取消")
        if result.timed_out:
            raise GitError("仓库克隆超过 %d 秒" % timeout)
        if result.stdout_truncated:
            raise GitError("Git 输出超过安全上限")
        detail = redact_sensitive_text((result.stderr or result.stdout).strip())
        raise GitError(detail or "仓库克隆失败")

    def repository_root(self, path: Path) -> Path:
        output = self.run(path, ["rev-parse", "--show-toplevel"]).stdout.strip()
        return Path(output).resolve()

    def is_worktree(self, path: Path) -> bool:
        marker = path / ".git"
        return marker.is_file()

    def remotes(self, path: Path) -> list[str]:
        result = self.run(path, ["remote"], check=False)
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def select_remote(self, path: Path) -> str | None:
        remotes = self.remotes(path)
        if not remotes:
            return None
        upstream = self.upstream(path)
        if upstream and "/" in upstream:
            candidate = upstream.split("/", 1)[0]
            if candidate in remotes:
                return candidate
        if "origin" in remotes:
            return "origin"
        return remotes[0]

    def remote_url(self, path: Path, remote: str) -> str | None:
        result = self.run(path, ["remote", "get-url", remote], check=False)
        return result.stdout.strip() or None if result.ok else None

    def current_branch(self, path: Path) -> str | None:
        result = self.run(path, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
        return result.stdout.strip() or None if result.ok else None

    def upstream(self, path: Path) -> str | None:
        result = self.run(
            path,
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            check=False,
        )
        return result.stdout.strip() or None if result.ok else None

    def default_branch(self, path: Path, remote: str | None) -> str | None:
        if remote:
            result = self.run(
                path,
                ["symbolic-ref", "--quiet", "--short", "refs/remotes/%s/HEAD" % remote],
                check=False,
            )
            value = result.stdout.strip()
            if result.ok and value.startswith(remote + "/"):
                return value[len(remote) + 1 :]
        return self.current_branch(path)

    def fetch(
        self,
        path: Path,
        remote: str,
        *,
        timeout: float = 120,
        expected_fingerprint: str | None = None,
        branches: Sequence[str] | None = None,
    ) -> None:
        if not is_valid_branch_name(remote):
            raise GitError("remote 名称不符合 Git 引用命名规则")
        remote_url = self.remote_url(path, remote)
        if not remote_url or not is_safe_remote_url(remote_url):
            raise GitError(
                "拒绝自动 fetch 非标准 Git transport；仅支持 ssh/http/https/git/file、scp 风格或本地路径"
            )
        if expected_fingerprint is not None:
            current_fingerprint = normalize_remote(remote_url, base=path).canonical
            if current_fingerprint != expected_fingerprint:
                raise GitError(
                    "remote %s 的仓库身份在检查期间发生变化；已拒绝 fetch" % remote
                )
        verified_remote = "branch-radar-%s" % secrets.token_hex(8)
        safety_config = [
            "-c", "protocol.allow=never",
            "-c", "protocol.ssh.allow=always",
            "-c", "protocol.http.allow=always",
            "-c", "protocol.https.allow=always",
            "-c", "protocol.git.allow=always",
            "-c", "protocol.file.allow=always",
            "-c", "protocol.ext.allow=never",
            "-c", "core.sshCommand=ssh -o BatchMode=yes",
            "-c", "fetch.pruneTags=false",
            "-c", "remote.%s.pruneTags=false" % verified_remote,
        ]
        wildcard_refspec = "+refs/heads/*:refs/remotes/%s/*" % remote
        environment = {
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "remote.%s.url" % verified_remote,
            "GIT_CONFIG_VALUE_0": remote_url,
        }
        missing_branches: list[str] = []
        if branches is None:
            fetch_refspecs = [wildcard_refspec]
        else:
            selected = list(dict.fromkeys(
                require_valid_branch_name(branch) for branch in branches
            ))
            if not selected:
                raise GitError("至少需要一个有效分支才能获取远程引用")
            remote_refs = self.run(
                path,
                safety_config + [
                    "ls-remote", "--heads", "--", verified_remote,
                ] + ["refs/heads/%s" % branch for branch in selected],
                timeout=min(timeout, 60),
                check=False,
                env=environment,
                stdout_limit_bytes=2_097_152,
                stderr_limit_bytes=2_097_152,
            )
            if not remote_refs.ok:
                detail = (remote_refs.stderr or remote_refs.stdout).strip()
                if remote_refs.timed_out:
                    detail = "读取远程分支列表超时"
                elif remote_refs.stdout_truncated:
                    detail = "远程分支列表超过安全上限"
                raise GitError(detail or "无法读取远程分支列表")
            if remote_refs.stdout_truncated:
                raise GitError("远程分支列表超过安全上限")
            selected_refs = {
                "refs/heads/%s" % branch: branch for branch in selected
            }
            remote_oids: dict[str, str] = {}
            for line in remote_refs.stdout.splitlines():
                if not line.strip():
                    continue
                fields = line.split("\t", 1)
                if len(fields) != 2:
                    raise GitError("远程分支列表格式无效")
                oid, ref = fields[0].strip().lower(), fields[1].strip()
                branch = selected_refs.get(ref)
                if branch is None:
                    continue
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                    raise GitError("远程分支列表包含无效 Git object id")
                previous_oid = remote_oids.get(branch)
                if previous_oid is not None and previous_oid != oid:
                    raise GitError("远程分支列表包含冲突的 Git object id")
                remote_oids[branch] = oid
            present_branches = [
                branch
                for branch in selected
                if branch in remote_oids
            ]
            missing_branches = [
                branch for branch in selected if branch not in present_branches
            ]
            if present_branches:
                local_oids = self._remote_tracking_oids(
                    path,
                    remote,
                    present_branches,
                    timeout=min(timeout, 30),
                )
                changed_branches = [
                    branch
                    for branch in present_branches
                    if local_oids.get(branch) != remote_oids[branch]
                ]
                if not changed_branches:
                    self._delete_remote_tracking_refs(
                        path, remote, missing_branches
                    )
                    return
                fetch_refspecs = [
                    "+refs/heads/%s:refs/remotes/%s/%s"
                    % (branch, remote, branch)
                    for branch in changed_branches
                ]
            else:
                fetch_refspecs = []
        prune_arguments = ["--prune"] if branches is None else []
        if not fetch_refspecs:
            self._delete_remote_tracking_refs(path, remote, missing_branches)
            return
        temporary_packs_before = _temporary_pack_snapshot(path)
        try:
            result = self.run(
                path,
                [*safety_config, "fetch", *prune_arguments, "--no-tags", "--no-recurse-submodules", "--no-auto-maintenance", "--no-write-fetch-head", "--no-write-commit-graph", "--upload-pack=git-upload-pack", "--refmap=%s" % wildcard_refspec, "--", verified_remote, *fetch_refspecs],
                timeout=timeout,
                check=False,
                env=environment,
                stdout_limit_bytes=2_097_152,
                stderr_limit_bytes=2_097_152,
            )
        except OSError:
            _discard_new_temporary_packs(path, temporary_packs_before)
            raise
        if result.ok:
            self._delete_remote_tracking_refs(path, remote, missing_branches)
            return

        removed_count, removed_bytes = _discard_new_temporary_packs(
            path, temporary_packs_before
        )
        if result.timed_out:
            detail = "对象下载超过 %d 秒" % timeout
        elif result.stdout_truncated:
            detail = "Git 输出超过 %s 字节安全上限" % 2_097_152
        else:
            detail = (result.stderr or result.stdout).strip() or "git command failed"
        if removed_count:
            detail += "；已自动清理本次中断下载的临时数据（%s）" % _format_bytes(
                removed_bytes
            )
        raise GitError(detail)

    def _remote_tracking_oids(
        self,
        path: Path,
        remote: str,
        branches: Sequence[str],
        *,
        timeout: float,
    ) -> dict[str, str]:
        prefix = "refs/remotes/%s/" % remote
        selected_refs = {
            prefix + require_valid_branch_name(branch): branch
            for branch in branches
        }
        result = self.run(
            path,
            [
                "for-each-ref",
                "--format=%(refname)%00%(objectname)",
                "--",
                *selected_refs,
            ],
            timeout=timeout,
            check=False,
            stdout_limit_bytes=2_097_152,
            stderr_limit_bytes=2_097_152,
        )
        if not result.ok or result.stdout_truncated:
            _raise_unexpected_git_result(result, "读取本地远程分支引用")

        local_oids: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if not line:
                continue
            fields = line.split("\x00", 1)
            if len(fields) != 2:
                raise GitError("本地远程分支引用格式无效")
            ref, oid = fields[0].strip(), fields[1].strip().lower()
            branch = selected_refs.get(ref)
            if branch is None:
                continue
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                raise GitError("本地远程分支引用包含无效 Git object id")
            local_oids[branch] = oid
        return local_oids

    def _delete_remote_tracking_refs(
        self, path: Path, remote: str, branches: Sequence[str]
    ) -> None:
        if not branches:
            return
        commands = "".join(
            "delete refs/remotes/%s/%s\n" % (
                remote, require_valid_branch_name(branch)
            )
            for branch in branches
        )
        self.run(path, ["update-ref", "--stdin"], stdin=commands)

    def branch_ref(self, path: Path, remote: str | None, branch: str) -> str | None:
        require_valid_branch_name(branch)
        candidates = (
            ["refs/remotes/%s/%s" % (remote, branch)]
            if remote
            else ["refs/heads/%s" % branch]
        )
        for ref in candidates:
            result = self.run(
                path,
                ["rev-parse", "--verify", "--quiet", "%s^{commit}" % ref],
                check=False,
            )
            if result.ok and result.stdout.strip():
                return result.stdout.strip()
            if result.returncode != 1 or result.timed_out or result.stdout_truncated:
                _raise_unexpected_git_result(result, "读取分支引用")
        return None

    def list_branches(self, path: Path, remote: str | None) -> list[str]:
        prefixes = ["refs/heads"]
        if remote:
            prefixes.append("refs/remotes/%s" % remote)
        result = self.run(
            path,
            ["for-each-ref", "--format=%(refname:short)", *prefixes],
            check=False,
        )
        values = set()
        for line in result.stdout.splitlines():
            ref = line.strip()
            if not ref or ref.endswith("/HEAD"):
                continue
            if remote and ref.startswith(remote + "/"):
                ref = ref[len(remote) + 1 :]
            values.add(ref)
        return sorted(values)

    def is_ancestor(self, path: Path, older: str, newer: str) -> bool:
        result = self.run(path, ["merge-base", "--is-ancestor", older, newer], check=False)
        if result.returncode not in {0, 1} or result.timed_out or result.stdout_truncated:
            _raise_unexpected_git_result(result, "判断提交祖先关系")
        return result.returncode == 0

    def object_exists(self, path: Path, oid: str) -> bool:
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", oid):
            raise GitError("无效 Git object id")
        result = self.run(path, ["cat-file", "--batch-check"], stdin=oid + "\n")
        line = result.stdout.strip()
        if line.endswith(" missing"):
            return False
        fields = line.split()
        if len(fields) < 2:
            raise GitError("git cat-file 返回了无法识别的结果")
        return fields[1] == "commit"

    def merge_base(self, path: Path, left: str, right: str) -> str | None:
        result = self.run(path, ["merge-base", left, right], check=False)
        if (
            result.returncode == 1
            and not result.timed_out
            and not result.stdout_truncated
        ):
            return None
        if not result.ok:
            _raise_unexpected_git_result(result, "计算 merge-base")
        return result.stdout.strip() or None

    def commits_between(
        self, path: Path, older: str | None, newer: str, *, limit: int = 250
    ) -> list[CommitInfo]:
        revision = "%s..%s" % (older, newer) if older else newer
        return self._log(path, ["--reverse", "--topo-order", "--max-count=%d" % limit, revision])

    def commits_not_reachable(
        self, path: Path, include: str, exclude: str, *, limit: int = 250
    ) -> list[CommitInfo]:
        return self._log(
            path,
            ["--reverse", "--topo-order", "--max-count=%d" % limit, include, "--not", exclude],
        )

    def commit_count_between(self, path: Path, older: str | None, newer: str) -> int:
        revision = "%s..%s" % (older, newer) if older else newer
        result = self.run(path, ["rev-list", "--count", revision])
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise GitError("git rev-list 返回了无效提交数") from exc

    def commit_count_not_reachable(self, path: Path, include: str, exclude: str) -> int:
        result = self.run(path, ["rev-list", "--count", include, "--not", exclude])
        try:
            return int(result.stdout.strip())
        except ValueError as exc:
            raise GitError("git rev-list 返回了无效提交数") from exc

    def _log(self, path: Path, revisions: Sequence[str]) -> list[CommitInfo]:
        format_value = "%H%x1f%P%x1f%an%x1f%ae%x1f%aI%x1f%cn%x1f%ce%x1f%cI%x1f%s%x1e"
        result = self.run(
            path,
            ["log", "--no-decorate", "--format=%s" % format_value, *list(revisions)],
        )
        commits: list[CommitInfo] = []
        for record in result.stdout.split("\x1e"):
            clean = record.strip("\r\n")
            if not clean:
                continue
            fields = clean.split("\x1f")
            if len(fields) != 9:
                continue
            commits.append(
                CommitInfo(
                    sha=fields[0],
                    parents=fields[1].split() if fields[1] else [],
                    author_name=_redact_git_display_text(fields[2]),
                    author_email=_redact_git_display_text(fields[3]),
                    author_date=fields[4],
                    committer_name=_redact_git_display_text(fields[5]),
                    committer_email=_redact_git_display_text(fields[6]),
                    commit_date=fields[7],
                    subject=_redact_git_display_text(fields[8]),
                )
            )
        return commits

    def diff_stats(self, path: Path, older: str, newer: str) -> DiffStats:
        numstat = self.run(path, ["diff", "--numstat", older, newer])
        status_result = self.run(path, ["diff", "--name-status", older, newer])
        stats = DiffStats()
        for line in numstat.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added, deleted, filename = parts
            stats.files.append(_redact_git_display_text(filename))
            stats.files_changed += 1
            if added == "-" or deleted == "-":
                stats.binary_files += 1
            else:
                stats.insertions += int(added)
                stats.deletions += int(deleted)
        for line in status_result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                item: dict[str, str] = {
                    "status": parts[0],
                    "path": _redact_git_display_text(parts[-1]),
                }
                if len(parts) >= 3:
                    item["old_path"] = _redact_git_display_text(parts[1])
                stats.status.append(item)
        return stats

    def patch_excerpt(self, path: Path, older: str, newer: str, max_bytes: int) -> str:
        result = self.runner.run_limited(
            [
                self.binary,
                "-C",
                str(path),
                "diff",
                "--no-ext-diff",
                "--unified=2",
                older,
                newer,
                "--",
            ],
            timeout=60,
            stdout_limit_bytes=max_bytes,
            env=_git_environment(),
            replace_env=True,
        )
        if not result.ok and not result.stdout_truncated:
            detail = (result.stderr or result.stdout).strip()
            if result.timed_out:
                detail = "生成 diff 摘要超时"
            raise GitError(detail or "git diff failed")
        safe_patch = _redact_patch(result.stdout)
        encoded = safe_patch.encode("utf-8", "replace")
        if len(encoded) <= max_bytes and not result.stdout_truncated:
            return safe_patch
        clipped = encoded[:max_bytes].decode("utf-8", "ignore")
        return clipped + "\n\n[diff truncated by Branch Radar]"


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TEMPORARY_PACK = re.compile(r"^tmp_pack_[A-Za-z0-9]+$")


def _redact_git_display_text(value: str) -> str:
    """Sanitize Git-provided text before it can enter persisted facts."""
    return redact_sensitive_text(_clean_control(value))


def _git_common_directory(repository: Path) -> Path | None:
    """Resolve a checkout's common Git directory without invoking another process."""
    marker = repository / ".git"
    try:
        if marker.is_dir():
            git_directory = marker.resolve()
        elif marker.is_file():
            value = marker.read_text(encoding="utf-8", errors="replace")[:4096].strip()
            if not value.lower().startswith("gitdir:"):
                return None
            location = value.split(":", 1)[1].strip()
            git_directory = Path(location)
            if not git_directory.is_absolute():
                git_directory = marker.parent / git_directory
            git_directory = git_directory.resolve()
        else:
            return None

        common_marker = git_directory / "commondir"
        if not common_marker.is_file():
            return git_directory
        common_value = common_marker.read_text(
            encoding="utf-8", errors="replace"
        )[:4096].strip()
        if not common_value:
            return git_directory
        common_directory = Path(common_value)
        if not common_directory.is_absolute():
            common_directory = git_directory / common_directory
        return common_directory.resolve()
    except OSError:
        return None


def _temporary_pack_snapshot(repository: Path) -> dict[str, tuple[int, int]]:
    common_directory = _git_common_directory(repository)
    if common_directory is None:
        return {}
    pack_directory = common_directory / "objects" / "pack"
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        entries = list(pack_directory.iterdir())
    except OSError:
        return snapshot
    for entry in entries:
        if not _TEMPORARY_PACK.fullmatch(entry.name):
            continue
        try:
            stat = entry.lstat()
        except OSError:
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        snapshot[entry.name] = (stat.st_ino, stat.st_size)
    return snapshot


def _discard_new_temporary_packs(
    repository: Path, before: Mapping[str, tuple[int, int]]
) -> tuple[int, int]:
    """Remove only temp packs created by the just-finished fetch process."""
    common_directory = _git_common_directory(repository)
    if common_directory is None:
        return 0, 0
    pack_directory = common_directory / "objects" / "pack"
    removed_count = 0
    removed_bytes = 0
    try:
        entries = list(pack_directory.iterdir())
    except OSError:
        return 0, 0
    for entry in entries:
        if entry.name in before or not _TEMPORARY_PACK.fullmatch(entry.name):
            continue
        try:
            stat = entry.lstat()
            if entry.is_symlink() or not entry.is_file():
                continue
            entry.unlink()
        except OSError:
            continue
        removed_count += 1
        removed_bytes += stat.st_size
    return removed_count, removed_bytes


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if amount < 1024 or unit == "GiB":
            return ("%.0f %s" if unit == "B" else "%.2f %s") % (amount, unit)
        amount /= 1024
    return "%d B" % value


def _git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a small Git environment without repository/config injection vars."""
    allowed = (
        "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
        "XDG_CONFIG_HOME", "SSH_AUTH_SOCK", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
        "DEVELOPER_DIR", "SYSTEMROOT", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    )
    environment = {
        key: os.environ[key]
        for key in allowed
        if os.environ.get(key)
    }
    environment["GIT_PAGER"] = "cat"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        environment.update(extra)
    return environment


def _clean_control(value: str) -> str:
    return _CONTROL.sub("", value).strip()


def _raise_unexpected_git_result(result: ProcessResult, action: str) -> None:
    if result.timed_out:
        raise GitError("%s超时" % action)
    if result.stdout_truncated:
        raise GitError("%s的输出超过安全上限" % action)
    detail = (result.stderr or result.stdout).strip()
    raise GitError("%s失败：%s" % (action, detail or "exit %s" % result.returncode))


_BRANCH_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\\x7f")


def is_valid_branch_name(value: str) -> bool:
    """Mirror `git check-ref-format --branch` without paying for a subprocess.

    Watch lists can hold hundreds of branches; forking git per name to learn
    what we can decide locally is not worth it. Names still reach git only
    inside a `refs/heads/` or `refs/remotes/` prefix, so this is defence in
    depth rather than the only barrier against option injection.
    """
    if not value or not isinstance(value, str):
        return False
    if value != value.strip():
        return False
    if value.startswith(("-", "/")) or value.endswith("/"):
        return False
    if value.endswith(".") or value == "@":
        return False
    if ".." in value or "//" in value or "@{" in value:
        return False
    if any(character in _BRANCH_FORBIDDEN_CHARS or ord(character) < 0x20 for character in value):
        return False
    return all(
        component and not component.startswith(".") and not component.endswith(".lock")
        for component in value.split("/")
    )


def require_valid_branch_name(value: str) -> str:
    if not is_valid_branch_name(value):
        raise GitError("分支名 %r 不符合 Git 引用命名规则" % value)
    return value


_SCP_SAFE = re.compile(r"^(?:[^/@:\s]+@)?[^/:\s]+:[^\s].*$")


def is_safe_remote_url(value: str) -> bool:
    remote = value.strip()
    if not remote or any(character in remote for character in ("\n", "\r", "\x00")):
        return False
    if remote.startswith("-"):
        return False
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", remote):
        return False
    if "://" not in remote and _SCP_SAFE.match(remote):
        return True
    parsed = urlsplit(remote)
    if parsed.scheme:
        return parsed.scheme.lower() in {"ssh", "http", "https", "git", "file"}
    # A scheme-less value that is not SCP syntax is a local filesystem path.
    # Reject option-like values, but allow common relatives such as origin.git.
    return True


_SENSITIVE_PATH_MARKERS = (
    "/.env",
    " .env",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".p12",
    ".pfx",
    ".key ",
    "credentials.json",
    "auth.json",
    "secrets.",
    "/secrets/",
)


def _redact_patch(value: str) -> str:
    lines: list[str] = []
    redact_file = False
    emitted_notice = False
    redact_private_key = False
    for line in value.splitlines():
        if line.startswith("diff --git "):
            lower = " " + line.lower() + " "
            redact_file = any(marker in lower for marker in _SENSITIVE_PATH_MARKERS)
            emitted_notice = False
            # File names are untrusted display text too. A credential-shaped
            # path must not bypass the content redaction applied below.
            lines.append(redact_sensitive_text(line))
            continue
        if "-----BEGIN " in line and "PRIVATE KEY-----" in line:
            redact_private_key = True
            lines.append("[Branch Radar redacted private key material]")
            continue
        if redact_private_key:
            if "-----END " in line and "PRIVATE KEY-----" in line:
                redact_private_key = False
            continue
        if redact_file:
            if not emitted_notice:
                lines.append("[Branch Radar redacted sensitive file contents]")
                emitted_notice = True
            continue
        lines.append(redact_sensitive_text(line))
    suffix = "\n" if value.endswith("\n") else ""
    return "\n".join(lines) + suffix
