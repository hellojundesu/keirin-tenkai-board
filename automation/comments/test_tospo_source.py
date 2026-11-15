"""Offline contract tests for Tospo page parsing."""
import unittest
from tospo_source import parse_race_text


PAGE = """8月11日(火)
松山 初日
1R
GⅠ
出走表
選手コメント
1
中野　慎詞
高知の後は練習できました。自力。
2
柏野　智典
村田君に付けます。
狙い目
1
5
"""


class TospoSourceTests(unittest.TestCase):
    def test_exact_identity(self):
        comments = parse_race_text(
            PAGE, race_date="2026-08-11", venue="松山", race_number=1,
            source_url="https://keirin.tokyo-sports.co.jp/list/race/20260810_31803",
            expected_riders={1: "中野 慎詞", 2: "柏野智典", 3: "伊藤旭"},
        )
        self.assertEqual(set(comments), {1, 2})
        self.assertEqual(comments[1].kind, "race_pre")
        self.assertEqual(comments[2].text, "村田君に付けます。")

    def test_wrong_rider_is_not_assigned(self):
        comments = parse_race_text(
            PAGE, race_date="2026-08-11", venue="松山", race_number=1,
            source_url="https://keirin.tokyo-sports.co.jp/list/race/20260810_31803",
            expected_riders={1: "別人", 2: "柏野智典"},
        )
        self.assertEqual(set(comments), {2})

    def test_wrong_date_rejected(self):
        with self.assertRaises(ValueError):
            parse_race_text(
                PAGE, race_date="2026-08-12", venue="松山", race_number=1,
                source_url="https://keirin.tokyo-sports.co.jp/list/race/20260810_31803",
                expected_riders={1: "中野慎詞"},
            )


if __name__ == "__main__":
    unittest.main()
