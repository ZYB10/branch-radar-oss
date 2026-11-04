from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RepositoryCandidate:
    name: str
    path: str
    remote_name: str | None
    remote_url: str | None
    canonical_remote: str | None
    current_branch: str | None
    upstream_branch: str | None
    default_branch: str | None
    is_worktree: bool = False
    error: str | None = None
    branches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CommitInfo:
    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    author_date: str
    committer_name: str
    committer_email: str
    commit_date: str
    subject: str

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiffStats:
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    binary_files: int = 0
    files: list[str] = field(default_factory=list)
    status: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ChangeFacts:
    repository_name: str
    repository_path: str
    remote_url: str | None
    branch: str
    event_kind: str
    old_sha: str | None
    new_sha: str | None
    merge_base: str | None
    detected_at: str
    previous_tip: str | None = None
    analysis_complete: bool = True
    commit_count: int | None = None
    dropped_commit_count: int | None = None
    commits_truncated: bool = False
    dropped_commits_truncated: bool = False
    commits_are_tip_sample: bool = False
    commits: list[CommitInfo] = field(default_factory=list)
    dropped_commits: list[CommitInfo] = field(default_factory=list)
    diff: DiffStats = field(default_factory=DiffStats)
    categories: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    patch_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Summary:
    title: str
    overview: str
    categories: list[str]
    highlights: list[str]
    risks: list[dict[str, str]]
    verification: list[str]
    deployment: dict[str, Any]
    confidence: str
    evidence: list[dict[str, str]]
    source: str = "rules"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheckResult:
    repository: str
    branch: str
    status: str
    event_id: int | None = None
    event_kind: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    summary: Summary | None = None
    error: str | None = None
    warning: str | None = None
    branch_id: int | None = None
    blocked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
