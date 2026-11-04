from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence

from .models import ChangeFacts, CommitInfo, Summary

_CATEGORY_PATTERNS: Sequence[tuple[str, Sequence[str]]] = (
    ("database", ("migration", "migrations/", "alembic", ".sql", "prisma/schema")),
    ("deployment", ("dockerfile", "docker-compose", "compose.yaml", "helm/", "charts/", "k8s/", "kubernetes/", ".github/workflows/", ".gitlab-ci")),
    ("api", ("openapi", "swagger", "/api/", "routes/", "router", "controller", "schema/", "proto/")),
    ("config", (".env", "config/", "settings.", "application.yml", "application.yaml", "pyproject.toml", "tsconfig")),
    ("dependency", ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "requirements", "poetry.lock", "uv.lock", "go.mod", "go.sum", "cargo.toml", "cargo.lock")),
    ("test", ("tests/", "test_", ".test.", ".spec.", "__tests__/")),
    ("docs", ("docs/", "readme", "changelog", ".md")),
)


def analyze_categories(files: Iterable[str], commits: Iterable[CommitInfo]) -> tuple[list[str], list[str]]:
    categories: set[str] = set()
    signals: list[str] = []
    normalized_files = [path.replace("\\", "/").lower() for path in files]
    for category, patterns in _CATEGORY_PATTERNS:
        matched = [path for path in normalized_files if any(pattern in path for pattern in patterns)]
        if matched:
            categories.add(category)
            signals.append("%s 相关文件 %d 个" % (_category_label(category), len(matched)))

    for commit in commits:
        subject = commit.subject.lower()
        if re.match(r"^(feat|feature)(\(.+\))?[!:]", subject):
            categories.add("feature")
        elif re.match(r"^(fix|bugfix|hotfix)(\(.+\))?[!:]", subject):
            categories.add("fix")
        elif re.match(r"^refactor(\(.+\))?[!:]", subject):
            categories.add("refactor")
        elif re.match(r"^test(\(.+\))?[!:]", subject):
            categories.add("test")
        elif re.match(r"^docs?(\(.+\))?[!:]", subject):
            categories.add("docs")
        if "breaking change" in subject or "!:" in subject:
            signals.append("提交说明包含 breaking change 信号")

    if not categories:
        categories.add("other")
    return sorted(categories), _dedupe(signals)


def summarize_with_rules(facts: ChangeFacts) -> Summary:
    commits = facts.commits
    non_merge = [commit for commit in commits if not commit.is_merge]
    merges = [commit for commit in commits if commit.is_merge]
    contributor_counts = Counter(commit.author_name for commit in non_merge or commits)
    contributors = [name for name, _count in contributor_counts.most_common()]

    stats = facts.diff
    kind_labels = {
        "fast_forward": "新增提交",
        "force_push": "发生强制推送",
        "rewind": "分支发生回退",
        "deleted": "分支已删除",
        "recreated": "分支重新出现",
        "history_gap": "分支变化（历史对象缺失）",
        "baseline": "建立初始基线",
    }
    action = kind_labels.get(facts.event_kind, "分支发生变化")
    title = "%s · %s" % (facts.branch, action)

    commit_total = facts.commit_count if facts.commit_count is not None else len(commits)
    if facts.event_kind == "deleted":
        overview = "远程分支 %s 已不可见，最后观察到的提交为 %s。" % (
            facts.branch,
            _short(facts.old_sha),
        )
    elif not facts.analysis_complete:
        overview = "旧提交对象已不可用，变化提交总数和净 diff 未知。"
        if facts.commits_are_tip_sample and commits:
            overview += " 当前仅展示新 tip 最近 %s 条历史样本。" % len(commits)
    else:
        overview = (
            "%s 个提交影响 %s 个文件，新增 %s 行、删除 %s 行。"
            % (commit_total, stats.files_changed, stats.insertions, stats.deletions)
        )
        if facts.commits_truncated:
            overview += " 提交明细仅展开其中 %s 条。" % len(commits)

    highlights: list[str] = []
    subjects = [commit.subject for commit in non_merge if commit.subject]
    if not subjects:
        subjects = [commit.subject for commit in commits if commit.subject]
    for subject in subjects[-6:]:
        if subject not in highlights:
            highlights.append(subject)
    if merges:
        actors = _dedupe([commit.committer_name for commit in merges])
        highlights.append("包含 %d 个合并提交；集成人：%s" % (len(merges), "、".join(actors)))
    if contributors:
        highlights.append("实际贡献者：%s" % "、".join(contributors))
    if not highlights:
        highlights.append(action)

    risks: list[dict[str, str]] = []
    if facts.event_kind in {"force_push", "rewind"}:
        risks.append({
            "level": "high",
            "area": "Git 历史",
            "reason": "分支历史不是普通快进，已有提交可能被替换或移除。",
        })
    if facts.event_kind == "history_gap":
        risks.append({
            "level": "high",
            "area": "Git 历史",
            "reason": "本地已缺少上一次提交对象，无法完整计算新增、移除提交和净 diff。",
        })
    elif not facts.analysis_complete:
        risks.append({
            "level": "high",
            "area": "分析完整性",
            "reason": "旧提交对象已不可用，无法完整计算新增/移除提交与净 diff。",
        })
    if facts.commits_truncated or facts.dropped_commits_truncated:
        risks.append({
            "level": "medium",
            "area": "提交明细",
            "reason": "提交数量超过展开上限；总数准确，但贡献者与逐条说明仅基于已展开部分。",
        })
    if "database" in facts.categories:
        risks.append({
            "level": "high",
            "area": "数据库",
            "reason": "检测到迁移或 SQL 相关文件变化，需要确认执行顺序与回滚策略。",
        })
    if "deployment" in facts.categories or "config" in facts.categories:
        risks.append({
            "level": "medium",
            "area": "部署配置",
            "reason": "检测到部署或运行配置变化，本地与目标环境可能需要同步调整。",
        })
    if "api" in facts.categories:
        risks.append({
            "level": "medium",
            "area": "接口兼容",
            "reason": "检测到 API 或数据契约相关文件，建议核对调用方兼容性。",
        })
    if not risks:
        risks.append({"level": "low", "area": "常规变更", "reason": "未命中高风险文件规则，仍需结合业务场景验证。"})

    verification: list[str] = []
    if "test" in facts.categories:
        verification.append("运行本次改动覆盖的测试，并确认新增测试在目标环境稳定通过。")
    else:
        verification.append("运行受影响模块的单元测试和关键业务冒烟测试。")
    if "api" in facts.categories:
        verification.append("联调 API 请求、响应结构与异常分支。")
    if "database" in facts.categories:
        verification.append("在可回滚环境演练数据库迁移。")
    if "deployment" in facts.categories or "config" in facts.categories:
        verification.append("对照部署清单检查环境变量、镜像和启动参数。")

    deployment_impact = "none"
    deployment_notes: list[str] = []
    if "database" in facts.categories or "deployment" in facts.categories:
        deployment_impact = "required"
        deployment_notes.append("存在数据库或部署资产变化。")
    elif "config" in facts.categories or "dependency" in facts.categories:
        deployment_impact = "possible"
        deployment_notes.append("配置或依赖变化可能需要重新安装或调整环境。")

    evidence = [
        {"path": path, "why": "本次净 diff 中的受影响文件"}
        for path in facts.diff.files[:8]
    ]
    return Summary(
        title=title,
        overview=overview,
        categories=facts.categories,
        highlights=highlights,
        risks=risks,
        verification=_dedupe(verification),
        deployment={"impact": deployment_impact, "notes": deployment_notes},
        confidence="medium" if facts.analysis_complete and (facts.diff.files or commits) else "low",
        evidence=evidence,
        source="rules",
    )


def _category_label(category: str) -> str:
    labels = {
        "api": "接口",
        "database": "数据库",
        "config": "配置",
        "dependency": "依赖",
        "deployment": "部署",
        "test": "测试",
        "docs": "文档",
    }
    return labels.get(category, category)


def _dedupe(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _short(value: str | None) -> str:
    return value[:8] if value else "未知"
