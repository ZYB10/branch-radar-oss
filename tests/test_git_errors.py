import unittest
from pathlib import Path

from branch_radar.git import GitClient, GitError, is_valid_branch_name
from branch_radar.process import ProcessResult


class ResultRunner:
    def __init__(self, result):
        self.result = result

    def run_limited(self, argv, **kwargs):
        self.result.argv = list(argv)
        return self.result


def result(code, stdout="", stderr="", timed_out=False):
    return ProcessResult([], code, stdout, stderr, 0.01, timed_out=timed_out)


class BranchNameValidationTests(unittest.TestCase):
    def test_git_ref_format_rules_are_enforced_locally(self):
        for name in ("main", "feature/x", "release/1.2.3", "ünïcode", "a_b-c"):
            self.assertTrue(is_valid_branch_name(name), name)
        for name in (
            "", " ", "-delete", "--upload-pack=evil", "a..b", "a b", "x/",
            "/x", ".hidden", "a.lock", "feat/.x", "@", "a@{0}", "x~1", "x^",
            "a//b", "x.", "a\x7fb", "a\nb", "back\\slash", "star*",
        ):
            self.assertFalse(is_valid_branch_name(name), repr(name))

    def test_branch_ref_refuses_malformed_names_before_touching_git(self):
        runner = ResultRunner(result(0, stdout="deadbeef"))
        client = GitClient(runner, binary="git")
        with self.assertRaises(GitError):
            client.branch_ref(Path("/repo"), "origin", "--upload-pack=evil")
        # The runner rewrites argv when invoked; an empty argv proves git never ran.
        self.assertEqual(runner.result.argv, [])


class GitErrorClassificationTests(unittest.TestCase):
    def test_missing_branch_is_distinct_from_git_failure(self):
        missing = GitClient(ResultRunner(result(1)), binary="git")
        self.assertIsNone(missing.branch_ref(Path("/repo"), "origin", "missing"))

        broken = GitClient(ResultRunner(result(128, stderr="corrupt object")), binary="git")
        with self.assertRaisesRegex(GitError, "corrupt object"):
            broken.branch_ref(Path("/repo"), "origin", "main")

        timed_out = GitClient(
            ResultRunner(result(124, timed_out=True)), binary="git"
        )
        with self.assertRaisesRegex(GitError, "超时"):
            timed_out.branch_ref(Path("/repo"), "origin", "main")

    def test_ancestor_and_merge_base_only_accept_expected_negative_exit(self):
        negative = GitClient(ResultRunner(result(1)), binary="git")
        self.assertFalse(negative.is_ancestor(Path("/repo"), "a", "b"))
        self.assertIsNone(negative.merge_base(Path("/repo"), "a", "b"))

        broken = GitClient(ResultRunner(result(128, stderr="bad revision")), binary="git")
        with self.assertRaises(GitError):
            broken.is_ancestor(Path("/repo"), "a", "b")

    def test_object_missing_uses_batch_protocol_not_exit_128(self):
        missing_oid = "a" * 40
        missing = GitClient(
            ResultRunner(result(0, stdout=missing_oid + " missing\n")), binary="git"
        )
        self.assertFalse(missing.object_exists(Path("/repo"), missing_oid))

        present = GitClient(
            ResultRunner(result(0, stdout=missing_oid + " commit 123\n")), binary="git"
        )
        self.assertTrue(present.object_exists(Path("/repo"), missing_oid))


if __name__ == "__main__":
    unittest.main()
