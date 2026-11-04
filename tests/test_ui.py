import unittest

from branch_radar.ui import parse_selection


class SelectionTests(unittest.TestCase):
    def test_all(self):
        self.assertEqual(parse_selection("all", 4), [0, 1, 2, 3])

    def test_ranges_and_duplicates(self):
        self.assertEqual(parse_selection("1, 3-5, 3", 5), [0, 2, 3, 4])

    def test_chinese_comma(self):
        self.assertEqual(parse_selection("1，2", 2), [0, 1])


if __name__ == "__main__":
    unittest.main()
