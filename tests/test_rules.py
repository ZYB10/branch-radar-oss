import unittest

from branch_radar.models import ChangeFacts, CommitInfo, DiffStats
from branch_radar.rules import analyze_categories, summarize_with_rules


def commit(subject="feat: add endpoint", parents=None):
    return CommitInfo(
        sha="a" * 40,
        parents=parents or ["b" * 40],
        author_name="Developer",
        author_email="dev@example.com",
        author_date="2026-08-18T10:00:00+08:00",
        committer_name="Integrator",
        committer_email="integrator@example.com",
        commit_date="2026-08-18T11:00:00+08:00",
        subject=subject,
    )


class RuleSummaryTests(unittest.TestCase):
    def test_high_risk_categories(self):
        categories, signals = analyze_categories(
            ["api/routes/material.py", "migrations/001_add.sql", "Dockerfile"],
            [commit()],
        )
        self.assertIn("api", categories)
        self.assertIn("database", categories)
        self.assertIn("deployment", categories)
        self.assertIn("feature", categories)
        self.assertTrue(signals)

    def test_merge_actor_and_contributor_are_distinct(self):
        merge = commit("Merge branch feature", parents=["1" * 40, "2" * 40])
        facts = ChangeFacts(
            repository_name="app",
            repository_path="/tmp/app",
            remote_url=None,
            branch="main",
            event_kind="fast_forward",
            old_sha="1" * 40,
            new_sha="a" * 40,
            merge_base="1" * 40,
            detected_at="2026-08-18T00:00:00+00:00",
            commits=[commit(), merge],
            diff=DiffStats(files_changed=1, insertions=5, files=["api/routes.py"]),
            categories=["api", "feature"],
        )
        summary = summarize_with_rules(facts)
        self.assertTrue(any("实际贡献者：Developer" in item for item in summary.highlights))
        self.assertTrue(any("集成人：Integrator" in item for item in summary.highlights))
        self.assertTrue(any(risk["area"] == "接口兼容" for risk in summary.risks))

    def test_incomplete_or_truncated_analysis_is_explicitly_highlighted(self):
        facts = ChangeFacts(
            repository_name="app",
            repository_path="/tmp/app",
            remote_url=None,
            branch="feature/recreated",
            event_kind="recreated",
            old_sha=None,
            new_sha="b" * 40,
            merge_base=None,
            detected_at="now",
            previous_tip="a" * 40,
            analysis_complete=False,
            commits_are_tip_sample=True,
            categories=["other"],
        )
        summary = summarize_with_rules(facts)
        reasons = " ".join(item["reason"] for item in summary.risks)
        self.assertIn("无法完整", reasons)
        self.assertIn("变化提交总数和净 diff 未知", summary.overview)
        self.assertNotIn("个提交影响", summary.overview)


if __name__ == "__main__":
    unittest.main()
