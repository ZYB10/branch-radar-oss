import json
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from branch_radar import i18n

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "branch_radar"
CATALOG = SOURCE / "resources" / "cli.en.json"

# The terminal surface: what a person reads directly. Deeper layers raise
# messages that reach the console through the same catalog, so they translate
# once their strings are added, but they are not required to be complete yet.
TERMINAL_MODULES = ("cli.py", "ui.py")


def catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def source_strings():
    found = set()
    for name in TERMINAL_MODULES:
        body = (SOURCE / name).read_text(encoding="utf-8")
        found.update(re.findall(r'"([^"\\]*[\u4e00-\u9fff][^"\\]*)"', body))
    return found


class LocaleResolutionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(i18n.set_locale, None)

    def test_environment_forms_people_actually_write_are_understood(self):
        for value, expected in (
            ("en", "en"),
            ("en_US.UTF-8", "en"),
            ("EN-GB", "en"),
            ("zh", "zh-CN"),
            ("zh_CN.UTF-8", "zh-CN"),
        ):
            self.assertEqual(i18n.normalize_locale(value), expected, value)
        # Neither C nor an unknown language should silently become English.
        for value in ("C", "POSIX", "", None, "fr_FR.UTF-8", "ja"):
            self.assertIsNone(i18n.normalize_locale(value), repr(value))

    def test_flag_beats_environment_and_environment_beats_the_default(self):
        with mock.patch.dict(os.environ, {"BRANCH_RADAR_LANG": "zh-CN", "LANG": "en_US.UTF-8"}):
            self.assertEqual(i18n.resolve_locale("en"), "en")
            self.assertEqual(i18n.resolve_locale(None), "zh-CN")
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(i18n.resolve_locale(None), "en")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(i18n.resolve_locale(None), i18n.DEFAULT_LOCALE)

    def test_an_untranslated_string_stays_readable(self):
        i18n.set_locale("en")
        self.assertEqual(i18n.t("这句没有条目"), "这句没有条目")
        self.assertEqual(i18n.t(""), "")

    def test_source_language_never_consults_the_catalog(self):
        i18n.set_locale("zh-CN")
        self.assertEqual(i18n.t("扫描本地 Git 仓库"), "扫描本地 Git 仓库")


class CatalogTests(unittest.TestCase):
    def test_every_terminal_string_has_english(self):
        """A half-translated CLI reads worse than an untranslated one."""
        missing = sorted(source_strings() - set(catalog()))
        self.assertEqual(
            missing, [], "these terminal strings have no entry in cli.en.json"
        )

    def test_translations_keep_their_format_specifiers(self):
        """A dropped %s is a crash, not a typo."""
        for source, translated in catalog().items():
            self.assertEqual(
                sorted(re.findall(r"%[sd]", source)),
                sorted(re.findall(r"%[sd]", translated)),
                source,
            )

    def test_english_catalog_is_actually_english(self):
        for source, translated in catalog().items():
            self.assertIsNone(
                re.search(r"[\u3400-\u9fff\u0400-\u04ff]", translated),
                "%s -> %s" % (source, translated),
            )

    def test_catalog_has_no_entries_for_strings_that_no_longer_exist(self):
        """A stale entry is a translation nobody will ever see again."""
        everything = set()
        for module in SOURCE.rglob("*.py"):
            body = module.read_text(encoding="utf-8")
            everything.update(re.findall(r'"([^"\\]*[\u4e00-\u9fff][^"\\]*)"', body))
        stale = sorted(set(catalog()) - everything)
        self.assertEqual(stale, [], "these catalog entries match no source string")


if __name__ == "__main__":
    unittest.main()
