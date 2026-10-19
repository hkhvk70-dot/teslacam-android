"""Mock-media integration checks. Never run a USB command or real encoder."""
import concurrent.futures
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_teslacam_indexer_switch as INDEX

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("event_queue", ROOT / "pipeline" / "transcode_queue.py")
QUEUE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUEUE)
OK = SimpleNamespace(returncode=0)
FAIL = SimpleNamespace(returncode=1)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix=".pipeline-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 2_000_000_000.0
        self.runner = Mock(return_value=OK)
        self.queue = QUEUE.Queue(self.root, self.root / "bin", runner=self.runner, clock=lambda: self.now,
                                 encode_lock=str(self.root / "shared-encoder.lock"))
        self.event = self.event_for("SentryClips/2025-01-01_12-00-00")

    def event_for(self, identifier):
        relative = f"TeslaCam/{identifier}/2025-01-01_12-00-00-front.mp4"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock-indexed-video")
        os.utime(path, (self.now - 600, self.now - 600))
        return {"id": identifier, "status": "complete", "clip_map": {"2025-01-01_12-00-00": {"front": relative}}, "mosaic": None}

    def step(self):
        return self.queue.step({"events": [self.event]})

    def proxy(self):
        relative = "_proxy/" + self.event["id"] + "/mosaic.mp4"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mock-index-validated-proxy")
        os.utime(path, (self.now, self.now))
        self.event["mosaic"] = relative
        return path

    def test_quiet_event_runs_without_browser_activity_marker(self):
        self.assertEqual(self.step()["status"], "encoded")
        self.assertEqual(self.runner.call_count, 2)
        call = self.runner.call_args_list[0]
        self.assertEqual(call.args[0][-2:], self.event["id"].split("/"))
        self.assertEqual(call.kwargs["env"]["TC_ENCODE_LOCK"], self.queue.encode_lock)
        self.assertEqual(call.kwargs["timeout"], 5400)

    def test_newest_event_wins_across_both_event_kinds(self):
        newer = self.event_for("SavedClips/2025-01-02_12-00-00")
        result = self.queue.step({"events": [self.event, newer]})
        self.assertEqual(result["event"], newer["id"])

    def test_growing_and_recently_changed_sources_are_not_encoded(self):
        self.event["status"] = "growing"
        self.assertEqual(self.step()["status"], "idle")
        self.event["status"] = "complete"
        source = self.root / next(iter(self.event["clip_map"].values()))["front"]
        os.utime(source, (self.now, self.now))
        self.assertEqual(self.step()["status"], "idle")
        self.runner.assert_not_called()

    def test_validated_current_proxy_is_not_reencoded(self):
        self.proxy()
        self.assertEqual(self.step()["status"], "idle")
        self.runner.assert_not_called()

    def test_removed_or_outdated_proxy_is_regenerated(self):
        proxy = self.proxy()
        os.utime(proxy, (self.now - 900, self.now - 900))
        self.assertEqual(self.step()["status"], "encoded")
        self.queue.finished.clear()
        proxy.unlink()
        self.assertEqual(self.step()["status"], "encoded")

    def test_cross_event_source_and_invalid_id_are_rejected(self):
        other = self.event_for("SavedClips/2025-01-02_12-00-00")
        self.event["clip_map"] = other["clip_map"]
        self.assertEqual(self.step()["status"], "idle")
        self.event["id"] = "SentryClips/../../outside"
        self.assertEqual(self.step()["status"], "idle")
        self.runner.assert_not_called()

    def test_failure_retries_after_backoff_not_forever_skipped(self):
        self.runner.return_value = FAIL
        self.assertEqual(self.step()["status"], "retry_later")
        self.assertEqual(self.step()["status"], "idle")
        self.assertEqual(self.runner.call_count, 1)
        self.now += 301
        self.runner.return_value = OK
        self.assertEqual(self.step()["status"], "encoded")

    def test_changed_source_bypasses_old_failure_backoff(self):
        self.runner.return_value = FAIL
        self.step()
        source = self.root / next(iter(self.event["clip_map"].values()))["front"]
        source.write_bytes(b"changed-mock-indexed-video")
        os.utime(source, (self.now - 300, self.now - 300))
        self.runner.return_value = OK
        self.assertEqual(self.step()["status"], "encoded")

    def test_encoder_timeout_is_delayed_instead_of_busy_looped(self):
        self.runner.side_effect = subprocess.TimeoutExpired("mock encoder", 5400)
        self.assertEqual(self.step()["status"], "retry_later")
        self.assertEqual(self.step()["status"], "idle")
        self.assertEqual(self.runner.call_count, 1)

    def test_success_is_not_reencoded_from_stale_index(self):
        self.runner.side_effect = [OK, FAIL]
        self.assertFalse(self.step()["index_refreshed"])
        self.assertEqual(self.step()["status"], "idle")
        self.assertEqual(self.runner.call_count, 2)


class AtomicIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "tests", prefix=".atomic-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_parallel_writers_never_share_temp_file_or_publish_partial_json(self):
        target = self.root / "index.json"
        payloads = [{"writer": n, "events": [str(n)] * 500} for n in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda data: INDEX.indexer.atomic_json(str(target), data), payloads))
        self.assertIn(json.loads(target.read_text()), payloads)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_publish_keeps_existing_index_and_cleans_own_temp(self):
        target = self.root / "index.json"
        target.write_text('{"existing":true}')
        with patch.object(INDEX.indexer.os, "replace", side_effect=OSError("simulated publish failure")):
            with self.assertRaises(OSError):
                INDEX.indexer.atomic_json(str(target), {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"existing": True})
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
