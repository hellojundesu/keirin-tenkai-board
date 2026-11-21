import copy
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cloud_independent_acquisition import build_day, digest
from cloud_venue_generation import VenueGenerationMockStore, build_publishable_venues, build_venue_generations
from cloud_venue_publish import VenueGatewayStore

COMMENTS_DIR = HERE.parent / "comments"
sys.path.insert(0, str(COMMENTS_DIR))
from cloud_comment_catalog import build_catalog, build_catalog_from_active_venues


DATA_DAY = HERE.parent.parent / "data" / "2026-11-18"


class VenueGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        records, manifest = build_day(DATA_DAY)
        cls.batches = build_venue_generations(records)
        cls.manifest = manifest

    def make_store(self, root: str) -> VenueGenerationMockStore:
        return VenueGenerationMockStore(Path(root) / "store.json")

    def test_real_day_is_split_without_loss(self):
        self.assertEqual(self.manifest["valid_races"], 79)
        self.assertEqual(len(self.batches), 8)
        self.assertEqual(sum(len(batch["records"]) for batch in self.batches), 79)
        self.assertEqual({batch["venue_name"] for batch in self.batches}, {"小倉", "西武園", "大垣", "宇都宮", "静岡", "小松島", "高知", "松山"})

    def test_other_venue_failure_does_not_block_complete_venue(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            complete, failed = self.batches[0], self.batches[1]
            store.stage(complete)
            store.publish(complete, 0)
            partial = copy.deepcopy(failed)
            partial["records"].pop()
            with self.assertRaisesRegex(ValueError, "race set mismatch"):
                store.stage(partial)
            active = store.read_active_venue("racecard", complete["race_date"], complete["venue_code"])
            self.assertEqual(active["generation_id"], complete["generation_id"])
            self.assertIsNone(store.read_active_venue("racecard", failed["race_date"], failed["venue_code"]))

    def test_partial_saved_day_still_emits_complete_venues(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            day = Path(root) / "2026-11-18"
            shutil.copytree(DATA_DAY, day)
            manifest_path = day / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "partial"
            manifest["completedVenues"] = [name for name in manifest["completedVenues"] if name != "小倉"]
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            (day / "小倉-12.json").unlink()
            batches, report = build_publishable_venues(day)
            self.assertEqual(report["publishable_venues"], 7)
            self.assertEqual(report["rejected_venues"], 1)
            self.assertNotIn("小倉", {batch["venue_name"] for batch in batches})
            self.assertEqual(sum(len(batch["records"]) for batch in batches), 67)

    def test_old_generation_survives_partial_retry(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            old = self.batches[0]
            store.stage(old)
            store.publish(old, 0)
            replacement = copy.deepcopy(old)
            replacement["records"][0]["payload"]["quality"]["marker"] = "replacement"
            replacement["records"][0]["content_hash"] = digest(replacement["records"][0]["payload"])
            replacement["generation_id"] = digest([row["content_hash"] for row in replacement["records"]])
            for row in replacement["records"]:
                row["payload"]["cloud_generation"]["generation_id"] = replacement["generation_id"]
                row["content_hash"] = digest(row["payload"])
            replacement["generation_id"] = digest([row["content_hash"] for row in replacement["records"]])
            for row in replacement["records"]:
                row["payload"]["cloud_generation"]["generation_id"] = replacement["generation_id"]
                row["content_hash"] = digest(row["payload"])
            replacement["expected_race_ids"] = replacement["expected_race_ids"][:-1]
            with self.assertRaises(ValueError):
                store.stage(replacement)
            self.assertEqual(store.read_active_venue("racecard", old["race_date"], old["venue_code"])["generation_id"], old["generation_id"])

    def test_retry_is_idempotent_and_conflicting_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            batch = self.batches[0]
            self.assertEqual(store.stage(batch), "staged")
            self.assertEqual(store.stage(batch), "unchanged")
            first = store.publish(batch, 0)
            second = store.publish(batch, first.venue_revision)
            self.assertEqual(first.action, "published")
            self.assertEqual(second.action, "already_active")
            conflict = copy.deepcopy(batch)
            conflict["records"][0]["source_updated_at"] = "2099-01-01T00:00:00+00:00"
            with self.assertRaisesRegex(ValueError, "generation_conflict"):
                store.stage(conflict)

    def test_stale_revision_cannot_replace_active_generation(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            batch = self.batches[0]
            store.stage(batch)
            store.publish(batch, 0)
            replacement = copy.deepcopy(batch)
            replacement["generation_id"] = "f" * 64
            replacement["manifest"]["cloud_generation"]["generation_id"] = replacement["generation_id"]
            replacement["manifest_hash"] = digest(replacement["manifest"])
            for row in replacement["records"]:
                row["payload"]["cloud_generation"]["generation_id"] = replacement["generation_id"]
                row["content_hash"] = digest(row["payload"])
            store.stage(replacement)
            with self.assertRaisesRegex(ValueError, "revision_conflict"):
                store.publish(replacement, 0)
            self.assertEqual(store.read_active_venue("racecard", batch["race_date"], batch["venue_code"])["generation_id"], batch["generation_id"])

    def test_concurrent_publish_has_one_winner(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            original = self.batches[0]
            candidates = []
            for marker in ("a", "b"):
                batch = copy.deepcopy(original)
                batch["generation_id"] = marker * 64
                batch["manifest"]["cloud_generation"]["generation_id"] = batch["generation_id"]
                batch["manifest_hash"] = digest(batch["manifest"])
                for row in batch["records"]:
                    row["payload"]["cloud_generation"]["generation_id"] = batch["generation_id"]
                    row["content_hash"] = digest(row["payload"])
                store.stage(batch)
                candidates.append(batch)
            outcomes = []
            barrier = threading.Barrier(2)

            def run(batch):
                barrier.wait()
                try:
                    outcomes.append(store.publish(batch, 0).action)
                except ValueError as exc:
                    outcomes.append(str(exc))

            threads = [threading.Thread(target=run, args=(batch,)) for batch in candidates]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertCountEqual(outcomes, ["published", "revision_conflict"])

    def test_day_index_tracks_mixed_active_venue_generations(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            for batch in self.batches:
                store.stage(batch)
                store.publish(batch, 0)
            day = store.read_day("racecard", "2026-11-18")
            self.assertEqual(len(day["payload"]["venues"]), 8)
            self.assertEqual(sum(len(row["race_ids"]) for row in day["payload"]["venues"]), 79)
            self.assertEqual(day["revision"], 8)

    def test_comment_catalog_is_compatible_with_active_venue_index(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            for batch in self.batches:
                store.stage(batch)
                store.publish(batch, 0)
            day = store.read_day("racecard", "2026-11-18")
            cloud_catalog = build_catalog_from_active_venues(
                day,
                lambda family, race_date, venue_code: store.read_active_venue(family, race_date, venue_code)["records"],
            )
            file_catalog = build_catalog(DATA_DAY)
            self.assertEqual(len(cloud_catalog["races"]), 79)
            self.assertEqual(
                [row["race_id"] for row in cloud_catalog["races"]],
                sorted(row["race_id"] for row in file_catalog["races"]),
            )

    def test_comment_catalog_rejects_generation_mixing(self):
        with tempfile.TemporaryDirectory(dir=HERE) as root:
            store = self.make_store(root)
            batch = self.batches[0]
            store.stage(batch)
            store.publish(batch, 0)
            day = store.read_day("racecard", "2026-11-18")
            mixed = copy.deepcopy(batch["records"])
            mixed[0]["payload"]["cloud_generation"]["generation_id"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "record mismatch"):
                build_catalog_from_active_venues(day, lambda *_: mixed)

    def test_live_gateway_uses_venue_rpcs_and_revision(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def call(self, payload):
                self.calls.append(payload)
                operation = payload["operation"]
                if operation == "stage_venue_generation":
                    return {"action": "staged"}
                if operation == "read_venue_pointer":
                    return {"pointer": {"revision": 4}}
                if operation == "publish_venue_generation":
                    return {"action": "published", "venue_revision": 5, "day_revision": 9}
                if operation == "read_day_index":
                    return {"day_index": {"revision": 9, "payload": {"venues": []}}}
                raise AssertionError(operation)

        client = FakeClient()
        store = VenueGatewayStore(client)
        batch = self.batches[0]
        self.assertEqual(store.stage(batch), "staged")
        revision = store.active_revision("racecard", batch["race_date"], batch["venue_code"])
        self.assertEqual(revision, 4)
        result = store.publish(batch, revision)
        self.assertEqual(result["venue_revision"], 5)
        self.assertEqual(store.read_day("racecard", batch["race_date"])["revision"], 9)
        self.assertEqual([row["operation"] for row in client.calls], [
            "stage_venue_generation", "read_venue_pointer",
            "publish_venue_generation", "read_day_index",
        ])
        self.assertEqual(client.calls[2]["expected_revision"], 4)


if __name__ == "__main__":
    unittest.main()
