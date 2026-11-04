import unittest
from datetime import datetime, timezone

from branch_radar.retention import parse_retention_cutoff


class RetentionCutoffTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def test_relative_duration_is_converted_to_utc_cutoff(self):
        self.assertEqual(
            parse_retention_cutoff("90d", now=self.now),
            "2026-05-22T12:00:00+00:00",
        )
        self.assertEqual(
            parse_retention_cutoff(" 12W ", now=self.now),
            "2026-05-28T12:00:00+00:00",
        )

    def test_iso_date_and_offset_are_normalized(self):
        self.assertEqual(
            parse_retention_cutoff("2026-08-01", now=self.now),
            "2026-08-01T00:00:00+00:00",
        )
        self.assertEqual(
            parse_retention_cutoff("2026-08-01T08:00:00+08:00", now=self.now),
            "2026-08-01T00:00:00+00:00",
        )

    def test_rejects_ambiguous_or_future_values(self):
        for value in (
            "",
            "90 days",
            "3m",
            "0d",
            "2027-01-01",
            "999999999999999999999d",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_retention_cutoff(value, now=self.now)


if __name__ == "__main__":
    unittest.main()
