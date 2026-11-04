import tempfile
import unittest
from pathlib import Path

from branch_radar.config import AppConfig, ConfigStore
from branch_radar.paths import AppPaths


class ConfigValidationTests(unittest.TestCase):
    def test_default_scan_roots_do_not_assume_a_machine_layout(self):
        self.assertEqual(AppConfig().scan_roots, [])
        self.assertEqual(AppConfig.from_dict({}).scan_roots, [])

    def test_invalid_nested_shape_has_friendly_error(self):
        with self.assertRaisesRegex(ValueError, "codex.*JSON object"):
            AppConfig.from_dict({"codex": ["enabled"]})

    def test_null_numeric_value_has_friendly_error(self):
        with self.assertRaisesRegex(ValueError, "scan_max_depth.*整数"):
            AppConfig.from_dict({"scan_max_depth": None})

    def test_config_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = AppPaths(
                home=root,
                config=root / "config.json",
                database=root / "state.db",
                logs=root / "logs",
                cache=root / "cache",
                analysis_workspace=root / "analysis",
            )
            store = ConfigStore(paths)
            store.save(AppConfig(scan_roots=[str(root)]))
            self.assertEqual(store.load().scan_roots, [str(root)])
            first = store.signature()
            self.assertIsNotNone(first)
            store.save(AppConfig(scan_roots=[str(root), str(root / "other")]))
            self.assertNotEqual(store.signature(), first)

    def test_legacy_absolute_scan_roots_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            absolute_root = str(Path(directory).resolve())
            config = AppConfig.from_dict({"scan_roots": [absolute_root]})

            self.assertEqual(config.scan_roots, [absolute_root])

    def test_initial_fetch_timeout_is_longer_and_configurable(self):
        self.assertEqual(AppConfig().initial_fetch_timeout_seconds, 600)
        configured = AppConfig.from_dict({"initial_fetch_timeout_seconds": 420})
        self.assertEqual(configured.initial_fetch_timeout_seconds, 420)

    def test_notification_preferences_are_backward_compatible_and_validated(self):
        legacy = AppConfig.from_dict({})
        self.assertFalse(legacy.notifications.enabled)
        self.assertEqual(legacy.notifications.quiet_hours_start, "22:00")

        configured = AppConfig.from_dict({
            "notifications": {
                "enabled": True,
                "quiet_hours_enabled": True,
                "quiet_hours_start": "21:30",
                "quiet_hours_end": "07:15",
                "locale": "en",
            }
        })
        self.assertTrue(configured.notifications.enabled)
        self.assertEqual(configured.notifications.quiet_hours_start, "21:30")
        self.assertEqual(configured.notifications.locale, "en")

        with self.assertRaisesRegex(ValueError, "HH:MM"):
            AppConfig.from_dict({
                "notifications": {"quiet_hours_start": "25:00"}
            })
        for invalid_locale in ([], {}, "fr"):
            with self.subTest(locale=invalid_locale), self.assertRaisesRegex(
                ValueError, "zh-CN"
            ):
                AppConfig.from_dict({
                    "notifications": {"locale": invalid_locale}
                })


if __name__ == "__main__":
    unittest.main()
