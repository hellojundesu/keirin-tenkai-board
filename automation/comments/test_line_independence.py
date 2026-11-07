import json
import tempfile
import unittest
from pathlib import Path
from cloud_comment_catalog import build_catalog


class CatalogTests(unittest.TestCase):
    def test_basic_cards_allow_missing_lines_and_partial_warning(self):
        self.check(False)

    def test_bad_registration_is_rejected(self):
        self.check(True)

    def check(self, invalid):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as root:
            day = Path(root) / '2026-11-06'
            day.mkdir()
            manifest = {'date': day.name, 'status': 'partial', 'warnings': ['line unavailable'], 'races': [{'venue': '青森', 'race': 1}]}
            card = {'date': day.name, 'venue': '青森', 'race': 1, 'raceId': '202611061201', 'lines': [], 'riders': [{'number': n, 'snum': str(n)} for n in range(1, 6)]}
            if invalid:
                card['riders'][0]['snum'] = ''
            (day / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            (day / '青森-1.json').write_text(json.dumps(card), encoding='utf-8')
            if invalid:
                with self.assertRaises(ValueError):
                    build_catalog(day)
            else:
                self.assertEqual(len(build_catalog(day)['races']), 1)


if __name__ == '__main__':
    unittest.main()
