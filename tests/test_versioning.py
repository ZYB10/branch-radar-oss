import json
import os
import re
import unittest
from pathlib import Path

from branch_radar import __version__

ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    """Read project.version without requiring tomllib on Python 3.9."""
    project_body = None
    lines = (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines()
    collected = []
    in_project = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[project]":
            in_project = True
            continue
        if in_project and stripped.startswith("["):
            break
        if in_project:
            collected.append(line)
    if collected:
        project_body = "\n".join(collected)

    if project_body is None:
        raise AssertionError("pyproject.toml is missing its [project] table")
    matches = re.findall(
        r'^\s*version\s*=\s*["\']([^"\']+)["\']\s*(?:#.*)?$',
        project_body,
        flags=re.MULTILINE,
    )
    if len(matches) != 1:
        raise AssertionError("[project] must declare exactly one literal version")
    return matches[0]


def current_changelog_version() -> str:
    payload = json.loads(
        (ROOT / "src/branch_radar/resources/web/changelog.json").read_text(
            encoding="utf-8"
        )
    )
    releases = payload.get("releases")
    if not isinstance(releases, list) or not releases:
        raise AssertionError("changelog must contain at least one release")
    current = [release for release in releases if release.get("current") is True]
    if len(current) != 1:
        raise AssertionError("changelog must mark exactly one release as current")
    if current[0] is not releases[0]:
        raise AssertionError("the current changelog release must be listed first")
    version = current[0].get("version")
    if not isinstance(version, str) or not version:
        raise AssertionError("the current changelog release needs a version")
    return version


class VersionConsistencyTests(unittest.TestCase):
    def test_package_metadata_and_changelog_versions_match(self):
        expected = project_version()
        self.assertEqual(__version__, expected)
        self.assertEqual(current_changelog_version(), expected)

    def test_release_tag_matches_current_version_when_provided(self):
        release_tag = os.environ.get("RELEASE_TAG")
        if not release_tag:
            self.skipTest("RELEASE_TAG is only provided by the release workflow")
        self.assertEqual(release_tag, "v%s" % project_version())


if __name__ == "__main__":
    unittest.main()
