import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path


CLOUD_DIR = Path(__file__).resolve().parent
AUTOMATION_DIR = CLOUD_DIR.parent
sys.path.insert(0, str(CLOUD_DIR))
sys.path.insert(0, str(AUTOMATION_DIR / "comments"))

from cloud_end_to_end_pipeline import capture_published_generation, restore_published_generation
from cloud_oidc_gateway_client import OIDCGatewayStore
from cloud_supabase_adapter import JsonMockStore, drain_queue
from cloud_comment_integration import build_comment_generation, verify_stored_comment_generation


class FakeGatewayClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, payload):
        self.calls.append(payload)
        return self.responses.pop(0)


class RetryStore:
    def __init__(self):
        self.record = None
        self.attempts = 0

    def get(self, record_type, record_key):
        return copy.deepcopy(self.record)

    def cas_put(self, record, expected_hash):
        self.attempts += 1
        if self.attempts == 1:
            return False
        self.record = copy.deepcopy(record)
        return True


def published_records(generation="old-generation"):
    race_date = "2026-11-12"
    race_id = "202611120101"
    card = {
        "record_type": "cloud_racecard",
        "record_key": race_id,
        "payload": {"cloud_generation": {"generation_id": generation}},
        "content_hash": f"card-{generation}",
        "source_updated_at": "2026-11-12T00:00:00+00:00",
    }
    index = {
        "record_type": "cloud_races_index",
        "record_key": race_date,
        "payload": {
            "items": [{"race_id": race_id}],
            "cloud_generation": {"generation_id": generation},
        },
        "content_hash": f"index-{generation}",
        "source_updated_at": "2026-11-12T00:00:00+00:00",
    }
    manifest = {
        "record_type": "cloud_acquisition_manifest",
        "record_key": race_date,
        "payload": {
            "status": "complete",
            "valid_races": 1,
            "cloud_generation": {"generation_id": generation},
        },
        "content_hash": f"manifest-{generation}",
        "source_updated_at": "2026-11-12T00:00:00+00:00",
    }
    return [card, index, manifest]


class GenerationSafetyTests(unittest.TestCase):
    def test_gateway_accepts_refresh_generation_when_hash_matches(self):
        client = FakeGatewayClient([{"action": "refresh_generation", "stored_hash": "wanted"}])
        store = OIDCGatewayStore(client)
        record = {
            "record_type": "cloud_racecard",
            "record_key": "race-1",
            "payload": {"value": 1},
            "content_hash": "wanted",
            "source_updated_at": "2026-11-12T00:00:00+00:00",
        }
        self.assertTrue(store.cas_put(record, "old"))
        self.assertEqual(store.last_write_result["action"], "refresh_generation")
        self.assertEqual(len(client.calls), 1)

    def test_queue_retries_a_cas_conflict(self):
        with tempfile.TemporaryDirectory(dir=CLOUD_DIR) as root:
            queue = Path(root) / "queue.json"
            record = {
                "record_type": "cloud_race_result",
                "record_key": "race-1",
                "payload": {"status": "confirmed"},
                "content_hash": "wanted",
                "source_updated_at": "2026-11-12T00:00:00+00:00",
            }
            queue.write_text(json.dumps([{
                "status": "pending", "attempts": 0, "record": record, "last_error": None,
            }]), encoding="utf-8")
            store = RetryStore()
            result = drain_queue(queue, store)
            self.assertEqual(result["pending"], 0)
            self.assertEqual(result["counts"]["cas_retry"], 1)
            self.assertEqual(store.attempts, 2)

    def test_previous_complete_generation_can_be_restored(self):
        with tempfile.TemporaryDirectory(dir=CLOUD_DIR) as root:
            store = JsonMockStore(Path(root) / "store.json")
            for record in published_records():
                self.assertTrue(store.cas_put(record, None))
            snapshot = capture_published_generation(store, "2026-11-12")
            self.assertTrue(snapshot["publication"]["published"])

            current = store.get("cloud_racecard", "202611120101")
            replacement = published_records("partial-new")[0]
            self.assertTrue(store.cas_put(replacement, current["content_hash"]))

            result = restore_published_generation(store, "2026-11-12", snapshot)
            self.assertEqual(result["status"], "restored")
            self.assertEqual(result["active_publication"]["generation_id"], "old-generation")

    def test_comment_skip_requires_every_dependency(self):
        race_date = "2026-11-12"
        race_id = "202611120101"
        registrations = ["10001", "10002", "10003", "10004", "10005"]
        states = [
            {
                "race_id": race_id,
                "registration_no": registration,
                "comment_kind": kind,
                "fetch_status": "not_published",
            }
            for registration in registrations
            for kind in ("inspection", "result")
        ]
        records = build_comment_generation(
            race_date=race_date,
            expected_races=[{"race_id": race_id, "registration_numbers": registrations}],
            state_rows=states,
            observed_at="2026-11-12T00:00:00+00:00",
        )
        by_key = {(row["record_type"], row["record_key"]): row for row in records}

        result = verify_stored_comment_generation(lambda kind, key: by_key.get((kind, key)), race_date)
        self.assertEqual(result["race_count"], 1)
        del by_key[("cloud_race_comment", race_id)]
        with self.assertRaisesRegex(ValueError, "dependencies missing"):
            verify_stored_comment_generation(lambda kind, key: by_key.get((kind, key)), race_date)


if __name__ == "__main__":
    unittest.main()
