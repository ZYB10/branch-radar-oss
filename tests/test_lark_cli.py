import unittest

from branch_radar.config import LarkSettings
from branch_radar.integrations.lark_cli import (
    MAX_LARK_STDOUT_BYTES,
    LarkCli,
    LarkError,
    render_event_xml,
)
from branch_radar.process import ProcessResult


class FakeRunner:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run_limited(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return self.results.pop(0)


def result(code=0, stdout="", stderr="", *, timed_out=False, stdout_truncated=False):
    return ProcessResult(
        [], code, stdout, stderr, 0.01,
        timed_out=timed_out,
        stdout_truncated=stdout_truncated,
    )


class LarkCliTests(unittest.TestCase):
    def test_status_uses_explicit_verify_and_friendly_keychain_error(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(code=1, stderr='{"ok":false,"error":{"message":"keychain Get failed"}}'),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        status = adapter.status()
        self.assertFalse(status["verified"])
        self.assertIn("Keychain", status["error"])
        self.assertIn("--verify", runner.calls[1][0])

    def test_bot_only_status_is_not_accepted_for_user_document_writes(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(stdout='{"identity":"bot","verified":true,"identities":{"bot":{"status":"ready","available":true,"verified":true}}}'),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        self.assertFalse(adapter.status()["verified"])

    def test_ready_user_status_matches_real_auth_contract(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(stdout='{"identity":"user","verified":true,"identities":{"user":{"status":"ready","available":true,"userName":"Tester","tokenStatus":"valid"}}}'),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        status = adapter.status()
        self.assertTrue(status["verified"])
        self.assertEqual(status["user_name"], "Tester")
        # Status is cached inside a publish batch to avoid repeated Keychain checks.
        self.assertTrue(adapter.status()["verified"])
        self.assertEqual(len(runner.calls), 2)

    def test_scope_failure_preserves_missing_scopes_and_suggestion(self):
        runner = FakeRunner([
            result(
                code=1,
                stdout='{"ok":false,"granted":[],"missing":["docx:document:write_only"],"suggestion":"请管理员授权"}',
            ),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        with self.assertRaisesRegex(LarkError, "write_only.*管理员授权"):
            adapter.check_document_scopes()

    def test_verified_user_after_automatic_refresh_is_accepted(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(stdout='{"identity":"user","verified":true,"identities":{"user":{"status":"needs_refresh","available":true,"message":"server verification succeeded after refresh","tokenStatus":"needs_refresh"}}}'),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        self.assertTrue(adapter.status()["verified"])

    def test_verify_document_returns_safe_display_metadata(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(stdout='{"identity":"user","verified":true,"identities":{"user":{"status":"ready","available":true,"userName":"Tester","tokenStatus":"valid"}}}'),
            result(stdout='{"ok":true,"granted":["docx:document:readonly","docx:document:write_only"],"missing":[]}'),
            result(stdout='{"ok":true,"data":{"document":{"document_id":"docx-safe","revision_id":7,"content":"<title>研发 &amp; 项目动态</title><p>正文不会进入 Web DTO</p>","url":"https://team.feishu.cn/docx/docx-safe"}}}'),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)

        document = adapter.verify_document("docx-safe")

        self.assertEqual(document["title"], "研发 & 项目动态")
        self.assertEqual(
            document["url"], "https://team.feishu.cn/docx/docx-safe"
        )
        self.assertIn("+fetch", runner.calls[-1][0])

    def test_renderer_escapes_untrusted_commit_text(self):
        event = {
            "event_key": "BR-test",
            "repository": "app<script>",
            "branch": "main",
            "kind": "fast_forward",
            "detected_at": "now",
            "facts": {
                "old_sha": "a" * 40,
                "new_sha": "b" * 40,
                "commits": [{"parents": ["a"], "author_name": "A&B"}],
                "diff": {
                    "files_changed": 1,
                    "insertions": 1,
                    "deletions": 0,
                    "files": ["src/<bad>.py"],
                },
            },
            "summary": {
                "title": "<unsafe>",
                "overview": "A & B",
                "risks": [{
                    "level": "high",
                    "area": "API & UI",
                    "reason": "检查 <unsafe> 内容",
                }],
            },
        }
        xml = render_event_xml(event)
        self.assertNotIn("<script>", xml)
        self.assertIn("&lt;script&gt;", xml)
        self.assertIn("A &amp; B", xml)
        self.assertIn("贡献者：A&amp;B", xml)
        self.assertIn("API &amp; UI", xml)
        self.assertIn("检查 &lt;unsafe&gt; 内容", xml)
        self.assertIn("src/&lt;bad&gt;.py", xml)
        self.assertIn("BRANCH-RADAR:BR-test", xml)

    def test_renderer_is_compact_and_limits_secondary_lists(self):
        event = {
            "event_key": "BR-compact",
            "repository": "loom",
            "branch": "main",
            "kind": "fast_forward",
            "detected_at": "2026-08-18T15:09:36+00:00",
            "facts": {
                "old_sha": "a" * 40,
                "new_sha": "b" * 40,
                "commit_count": 7,
                "commits": [
                    {"parents": ["a"], "author_name": "Alice"},
                    {"parents": ["b"], "author_name": "Bob"},
                ],
                "diff": {
                    "files_changed": 4,
                    "insertions": 12,
                    "deletions": 5,
                    "files": ["one.py", "two.py", "three.py", "four.py"],
                },
            },
            "summary": {
                "title": "修复会话搜索",
                "overview": "改善搜索结果的无障碍语义。",
                "highlights": ["变化一", "变化二", "变化三", "变化四"],
                "risks": [
                    {"level": "low", "area": "普通变化", "reason": "不应展示"},
                    {"level": "medium", "area": "接口", "reason": "需要联调"},
                    {"level": "high", "area": "数据库", "reason": "需要回滚方案"},
                ],
                "verification": ["验证一", "验证二", "验证三"],
                "deployment": {"impact": "none", "notes": []},
            },
        }

        xml = render_event_xml(event)

        self.assertIn("<h2>loom / main · 修复会话搜索</h2>", xml)
        self.assertIn("检测于 ", xml)
        self.assertIn("7 个提交", xml)
        self.assertIn("4 个文件", xml)
        self.assertIn("+12 / -5", xml)
        self.assertIn("贡献者：Alice、Bob", xml)
        self.assertIn("改善搜索结果的无障碍语义。", xml)
        for value in ("变化一", "变化二", "变化三"):
            self.assertIn(value, xml)
        self.assertNotIn("变化四", xml)
        self.assertNotIn("不应展示", xml)
        self.assertIn("中风险 · 接口", xml)
        self.assertIn("高风险 · 数据库", xml)
        self.assertIn("验证一", xml)
        self.assertIn("验证二", xml)
        self.assertNotIn("验证三", xml)
        for value in ("one.py", "two.py", "three.py"):
            self.assertIn(value, xml)
        self.assertNotIn("four.py", xml)
        self.assertIn(
            '<span text-color="gray">证据：SHA aaaaaaaa → bbbbbbbb', xml
        )
        self.assertIn("BRANCH-RADAR:BR-compact", xml)
        self.assertNotIn("<table>", xml)

    def test_renderer_hides_low_risk_for_normal_event(self):
        event = {
            "event_key": "BR-low",
            "repository": "app",
            "branch": "main",
            "kind": "fast_forward",
            "detected_at": "now",
            "facts": {"commits": [], "diff": {}},
            "summary": {
                "risks": [{
                    "level": "low",
                    "area": "常规变更",
                    "reason": "仍需结合业务验证",
                }],
                "deployment": {"impact": "none", "notes": []},
            },
        }

        xml = render_event_xml(event)

        self.assertNotIn("需要关注", xml)
        self.assertNotIn("仍需结合业务验证", xml)

    def test_renderer_surfaces_abnormal_events_and_deployment_impact(self):
        abnormal = {
            "event_key": "BR-force",
            "repository": "app",
            "branch": "main",
            "kind": "force_push",
            "detected_at": "now",
            "facts": {"commits": [], "diff": {}},
            "summary": {"risks": [], "deployment": {"impact": "none", "notes": []}},
        }
        deployment = {
            **abnormal,
            "event_key": "BR-deploy",
            "kind": "fast_forward",
            "summary": {
                "risks": [],
                "deployment": {"impact": "required", "notes": ["需要执行迁移"]},
            },
        }

        abnormal_xml = render_event_xml(abnormal)
        deployment_xml = render_event_xml(deployment)

        self.assertIn("高风险 · Git 历史", abnormal_xml)
        self.assertIn("检测到强制推送", abnormal_xml)
        self.assertIn("高风险 · 部署影响", deployment_xml)
        self.assertIn("需要执行迁移", deployment_xml)

    def test_mutating_timeout_is_an_unknown_result(self):
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=FakeRunner([]))
        with self.assertRaises(LarkError) as raised:
            adapter._require_envelope(
                result(code=124, stderr="timeout", timed_out=True),
                "创建飞书文档",
                mutation=True,
            )
        self.assertTrue(raised.exception.may_have_succeeded)

    def test_mutating_invalid_json_is_an_unknown_result(self):
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=FakeRunner([]))
        with self.assertRaises(LarkError) as raised:
            adapter._require_envelope(
                result(stdout="not-json"),
                "创建飞书文档",
                mutation=True,
            )
        self.assertTrue(raised.exception.may_have_succeeded)

    def test_lark_output_is_bounded_and_truncation_is_never_silent_success(self):
        runner = FakeRunner([
            result(stdout="lark-cli version 1.0.86"),
            result(stdout='{"identity":"user","verified":true,"identities":{"user":{"status":"ready","available":true,"userName":"Tester","tokenStatus":"valid"}}}'),
            result(stdout='{"ok":true,', stdout_truncated=True),
        ])
        adapter = LarkCli(LarkSettings(binary="/bin/lark-cli"), runner=runner)
        with self.assertRaises(LarkError) as raised:
            adapter.append_xml("doc-token", "<p>x</p>")
        self.assertIn("安全上限", str(raised.exception))
        self.assertTrue(raised.exception.may_have_succeeded)
        self.assertEqual(
            runner.calls[-1][1]["stdout_limit_bytes"], MAX_LARK_STDOUT_BYTES
        )


if __name__ == "__main__":
    unittest.main()
