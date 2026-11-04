import unittest

from branch_radar.security import redact_sensitive_text


class SensitiveTextRedactionTests(unittest.TestCase):
    PROVIDER_TOKENS = (
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "gho_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_11AA0_exampleTokenMaterial_0123456789",
        "glpat-abcdefghijklmnopqrst",
        "xoxb-example-placeholder-for-tests",
        "xoxa-example-placeholder-for-tests",
        "xoxp-example-placeholder-for-tests",
        "xoxr-example-placeholder-for-tests",
        "xoxs-example-placeholder-for-tests",
        "sk-proj-abcdefghijklmnopqrstuvwxyz012345",
        "AKIAIOSFODNN7EXAMPLE",
    )

    def test_bare_provider_tokens_are_redacted(self):
        for token in self.PROVIDER_TOKENS:
            with self.subTest(token=token[:12]):
                redacted = redact_sensitive_text("commit leaked %s." % token)
                self.assertEqual(
                    redacted,
                    "commit leaked <redacted-provider-token>.",
                )

    def test_redacted_output_never_contains_the_original_provider_token(self):
        for token in self.PROVIDER_TOKENS:
            with self.subTest(token=token[:12]):
                redacted = redact_sensitive_text(
                    "subject=%s body=(%s)" % (token, token)
                )
                self.assertNotIn(token, redacted)

    def test_existing_secret_shapes_remain_redacted(self):
        cases = {
            "Authorization: Bearer abc.def-123": "Authorization: Bearer <redacted>",
            "PRIVATE_TOKEN: glpat-short": "PRIVATE_TOKEN: <redacted>",
            '{"access_token": "json-secret"}': '{"access_token": "<redacted>"}',
            "https://alice:secret@example.com/repo.git": "https://example.com/repo.git",
            "eyJabcdefgh.ijklmnopqr.stuvwxyz01": "<redacted-jwt>",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(redact_sensitive_text(raw), expected)

    def test_similar_non_secrets_are_unchanged(self):
        values = (
            "ghp_short",
            "gho_documentation_example",
            "github_pat_notes",
            "glpat-example",
            "xoxb-example",
            "sk-feature-login",
            "AKIAIOSFODNN7EXAMPL",
            "prefixghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(redact_sensitive_text(value), value)


if __name__ == "__main__":
    unittest.main()
