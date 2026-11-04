from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..security import redact_sensitive_text
from ..timeutil import display_local_time
from .base import PublicationError, PublicationTarget


class MarkdownFileTarget(PublicationTarget):
    """Append events to a local Markdown file.

    The destination that needs no account, no network and no third-party CLI,
    which is what makes it the honest test of the `PublicationTarget` seam:
    everything about leases, verification and outcome recording stays in
    `Publisher`, and this only has to append text and read it back.
    """

    destination_type = "markdown_file"

    def __init__(self, path: Path):
        self.path = Path(path)

    def contains(self, marker: str) -> bool:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return any(marker in line for line in handle)
        except FileNotFoundError:
            return False
        except OSError as exc:
            # Unreadable is not the same as absent; treating it as absent
            # would append a duplicate on the next attempt.
            raise PublicationError(
                "无法读取发布文件：%s" % redact_sensitive_text(str(exc))
            ) from exc

    def append(self, delivery: dict[str, Any], marker: str) -> list[Any]:
        body = render_event_markdown(delivery, marker)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # The append may have been partially written, so the caller must
            # verify rather than assume either result.
            raise PublicationError(
                "无法写入发布文件：%s" % redact_sensitive_text(str(exc)),
                may_have_succeeded=True,
            ) from exc
        return []


def render_event_markdown(event: dict[str, Any], marker: str) -> str:
    """Render one event as plain Markdown.

    Mirrors the Lark renderer's rule: repository and commit data is text, and
    diffs, author emails and remote URLs never appear.
    """
    facts = event.get("facts") or {}
    summary = event.get("summary") or {}
    diff = facts.get("diff") or {}
    commits = facts.get("commits") or []
    complete = facts.get("analysis_complete", True) is not False
    non_merge = [c for c in commits if len(c.get("parents") or []) <= 1]
    contributors: list[str] = []
    for commit in non_merge or commits:
        name = commit.get("author_name")
        if isinstance(name, str) and name and name not in contributors:
            contributors.append(name)

    commit_count = facts.get("commit_count")
    if commit_count is None and complete and not facts.get("commits_are_tip_sample"):
        commit_count = len(commits)
    file_count = diff.get("files_changed")
    if file_count is None and complete:
        file_count = len(diff.get("files") or [])

    title = summary.get("title") or str(event.get("kind") or "分支变化")
    metrics = " · ".join([
        "检测于 %s" % display_local_time(str(event.get("detected_at") or "")),
        "%s 个提交" % (commit_count if commit_count is not None else "?"),
        "%s 个文件" % (file_count if file_count is not None else "?"),
        "+%s / -%s" % (
            diff.get("insertions", 0) if complete else "?",
            diff.get("deletions", 0) if complete else "?",
        ),
        "贡献者：%s" % ("、".join(contributors) or "未识别"),
    ])

    lines = [
        "",
        "## %s / %s · %s" % (
            event.get("repository"), event.get("branch"), title
        ),
        "",
        "<!-- %s -->" % marker,
        "",
        "*%s*" % metrics,
        "",
        str(summary.get("overview") or "分支发生变化"),
        "",
    ]
    highlights = (summary.get("highlights") or [])[:3]
    if highlights:
        lines.append("### 主要变化")
        lines.append("")
        lines.extend("- %s" % item for item in highlights)
        lines.append("")
    risks = (summary.get("risks") or [])[:3]
    if risks:
        lines.append("### 风险")
        lines.append("")
        lines.extend("- %s" % item for item in risks)
        lines.append("")
    if not complete:
        lines.append("> 证据不完整，以上统计可能有缺失。")
        lines.append("")
    return redact_sensitive_text("\n".join(lines))


def default_markdown_path(home: Path) -> Path:
    return home / "published" / "branch-radar.md"


__all__ = [
    "MarkdownFileTarget",
    "default_markdown_path",
    "render_event_markdown",
]
