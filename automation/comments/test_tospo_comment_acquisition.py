from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cloud_comment_integration import (
    _merge_preserving_fetched,
    drain_comment_queue,
    enqueue_comment_generation,
    validate_comment_generation,
    verify_stored_tospo_generation,
)
from tospo_comment_acquisition import DiscoveryConflict, acquire_catalog, discover_pages


CATALOG = {
    "race_date": "2026-11-21",
    "races": [{
        "race_id": "202611214201",
        "race_date": "2026-11-21",
        "venue_code": "42",
        "venue": "名古屋",
        "race_number": 1,
        "registration_numbers": ["10001", "10002", "10003", "10004", "10005"],
        "entries": [
            {"registration_no": "10001", "car_number": 1, "rider_name": "選手 一郎"},
            {"registration_no": "10002", "car_number": 2, "rider_name": "選手 二郎"},
            {"registration_no": "10003", "car_number": 3, "rider_name": "選手 三郎"},
            {"registration_no": "10004", "car_number": 4, "rider_name": "選手 四郎"},
            {"registration_no": "10005", "car_number": 5, "rider_name": "選手 五郎"},
        ],
    }],
}

PAGE = """<html><head><title>2026年11月21日 名古屋競輪 1R 出走表</title></head><body>
11月21日 名古屋 1R
選手コメント
1
選手　一郎
自力で頑張ります。
2
選手 二郎
一郎君へ。
狙い目
</body></html>"""

NO_SECTION_PAGE = """<html><head><title>2026年11月21日 名古屋競輪 1R 出走表</title></head>
<body>11月21日 名古屋 1R 出走表</body></html>"""


class MemoryStore:
    def __init__(self):
        self.records = {}

    def get(self, record_type, record_key):
        return copy.deepcopy(self.records.get((record_type, record_key)))

    def cas_put(self, record, expected_hash):
        key = (record["record_type"], record["record_key"])
        current = self.records.get(key)
        if (current or {}).get("content_hash") != expected_hash:
            return False
        self.records[key] = copy.deepcopy(record)
        return True


class FakeTransport:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        return self.pages[url]


class TospoCommentAcquisitionTests(unittest.TestCase):
    def test_comments_and_unpublished_riders_are_explicit(self):
        records, report = acquire_catalog(
            CATALOG,
            {("名古屋", 1): {"url": "https://keirin.tokyo-sports.co.jp/list/race/20261120_1", "source": PAGE}},
            observed_at="2026-11-21T03:00:00Z",
        )
        self.assertEqual(report["comment_count"], 2)
        self.assertEqual(report["state_counts"]["not_published"], 3)
        self.assertEqual(report["retry_required"], 0)
        manifest = validate_comment_generation(records)
        self.assertEqual(manifest["source_provider"], "tospo-keirin")
        entries = records[0]["payload"]["entries"]
        self.assertEqual(entries[0]["race_pre"]["text"], "自力で頑張ります。")
        self.assertEqual(entries[2]["race_pre"]["status"], "not_published")
        self.assertTrue(entries[2]["race_pre"]["publication_checked"])

    def test_missing_comment_section_is_not_a_failure(self):
        records, report = acquire_catalog(
            CATALOG,
            {("名古屋", 1): {"url": "https://keirin.tokyo-sports.co.jp/list/race/20261120_1", "source": NO_SECTION_PAGE}},
            observed_at="2026-11-21T03:00:00Z",
        )
        self.assertEqual(report["no_comment_section_races"], 1)
        self.assertEqual(report["state_counts"]["not_published"], 5)
        self.assertEqual(report["retry_required"], 0)
        validate_comment_generation(records)

    def test_missing_page_requires_retry(self):
        records, report = acquire_catalog(
            CATALOG,
            {},
            observed_at="2026-11-21T03:00:00Z",
        )
        self.assertEqual(report["state_counts"]["fetch_failed"], 5)
        self.assertEqual(report["retry_required"], 5)
        validate_comment_generation(records)

    def test_tospo_verification_rejects_other_provider(self):
        records, _ = acquire_catalog(
            CATALOG,
            {("名古屋", 1): {"url": "https://keirin.tokyo-sports.co.jp/list/race/20261120_1", "source": PAGE}},
            observed_at="2026-11-21T03:00:00Z",
        )
        stored = {(row["record_type"], row["record_key"]): row for row in records}
        result = verify_stored_tospo_generation(lambda kind, key: stored.get((kind, key)), "2026-11-21")
        self.assertEqual(result["source_provider"], "tospo-keirin")
        wrong = copy.deepcopy(stored)
        wrong[("cloud_comment_manifest", "2026-11-21")]["payload"]["source_provider"] = "other"
        with self.assertRaisesRegex(ValueError, "not Tospo"):
            verify_stored_tospo_generation(lambda kind, key: wrong.get((kind, key)), "2026-11-21")

    def test_failed_retry_preserves_existing_race_pre_body(self):
        good, _ = acquire_catalog(
            CATALOG,
            {("名古屋", 1): {"url": "https://keirin.tokyo-sports.co.jp/list/race/20261120_1", "source": PAGE}},
            observed_at="2026-11-21T03:00:00Z",
        )
        failed, _ = acquire_catalog(CATALOG, {}, observed_at="2026-11-21T04:00:00Z")
        merged = _merge_preserving_fetched(good[0], failed[0])
        state = merged["payload"]["entries"][0]["race_pre"]
        self.assertEqual(state["status"], "fetched")
        self.assertEqual(state["text"], "自力で頑張ります。")
        self.assertEqual(state["last_attempt_status"], "fetch_failed")

    def test_complete_generation_publishes_dependencies_before_manifest(self):
        records, _ = acquire_catalog(
            CATALOG,
            {("名古屋", 1): {"url": "https://keirin.tokyo-sports.co.jp/list/race/20261120_1", "source": PAGE}},
            observed_at="2026-11-21T03:00:00Z",
        )
        with tempfile.TemporaryDirectory() as root:
            queue = Path(root) / "queue.json"
            enqueue_comment_generation(records, queue)
            self.assertEqual(json.loads(queue.read_text(encoding="utf-8"))[-1]["record"]["record_type"], "cloud_comment_manifest")
            store = MemoryStore()
            result = drain_comment_queue(queue, store)
            self.assertEqual(result["pending"], 0)
            verified = verify_stored_tospo_generation(store.get, "2026-11-21")
            self.assertEqual(verified["race_count"], 1)

    def test_duplicate_source_identity_is_rejected(self):
        listing = """<a href="/list/race/20261120_1">one</a><a href="/list/race/20261120_2">two</a>"""
        pages = {
            "https://keirin.tokyo-sports.co.jp/list/race": listing,
            "https://keirin.tokyo-sports.co.jp/list/race/20261120_1": PAGE,
            "https://keirin.tokyo-sports.co.jp/list/race/20261120_2": PAGE,
        }
        with self.assertRaisesRegex(DiscoveryConflict, "duplicate Tospo race page"):
            discover_pages(FakeTransport(pages), "2026-11-21", {("名古屋", 1)})


if __name__ == "__main__":
    unittest.main()
