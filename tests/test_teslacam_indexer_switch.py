"""Offline regression tests; all MP4s and index output live in temporary folders."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "index.py"
with mock.patch.dict(os.environ, {"FFPROBE": "test-ffprobe"}):
    spec = importlib.util.spec_from_file_location("teslacam_indexer_switch", MODULE_PATH)
    indexer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(indexer)


def box(kind, payload=b""):
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def probe_result(duration="60.0", frames="1800", streams=None, returncode=0):
    if streams is None:
        streams = [{"codec_type": "video", "duration": duration, "nb_frames": frames}]
    return SimpleNamespace(returncode=returncode, stdout=json.dumps({
        "streams": streams, "format": {"duration": duration},
    }), stderr="")


def fresh_audit():
    return {key: [] for key in ("unknown_files", "unknown_cameras", "unfinished",
                               "event_without_json", "bad_event_json", "deduped")}


class IndexerSwitchTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures within the writable workspace when Windows sandboxes
        # expose a system TEMP directory without inherited write permission.
        self.temp = tempfile.TemporaryDirectory(prefix="teslacam-indexer-test-", dir=MODULE_PATH.parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patch_out = mock.patch.object(indexer, "OUT", str(self.root))
        self.patch_cache = mock.patch.object(indexer, "CACHE_FILE", str(self.root / ".index-cache.json"))
        self.patch_out.start()
        self.patch_cache.start()
        self.addCleanup(self.patch_out.stop)
        self.addCleanup(self.patch_cache.stop)

    def mp4(self, relative="clip.mp4", content=None):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = box(b"ftyp", b"isom") + box(b"moov", b"metadata") + box(b"mdat", b"frames")
        path.write_bytes(content)
        return str(path)

    def test_old_moov_only_cache_is_revalidated(self):
        path = self.mp4()
        stat = os.stat(path)
        cache = {path: {"size": stat.st_size, "mtime": int(stat.st_mtime),
                        "ready": True, "why": "moov"}}
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result("N/A", "1")) as run:
            self.assertEqual(indexer.mp4_ready(path, cache), (False, "too_few_frames"))
        run.assert_called_once()
        self.assertEqual(cache[path]["probe_version"], indexer.PROBE_VERSION)
        self.assertEqual(cache[path]["mtime_ns"], stat.st_mtime_ns)

    def test_normal_video_passes_and_unchanged_file_uses_cache(self):
        path = self.mp4()
        cache = {}
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result()) as run:
            self.assertEqual(indexer.mp4_ready(path, cache), (True, "video_ready"))
            self.assertEqual(indexer.mp4_ready(path, cache), (True, "video_ready"))
        run.assert_called_once()
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], "test-ffprobe")
        self.assertIn("v:0", args[0])
        self.assertEqual(kwargs["timeout"], 15)

    def test_nanosecond_mtime_change_revalidates_same_size(self):
        path = self.mp4()
        cache = {}
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result()) as run:
            indexer.mp4_ready(path, cache)
            stat = os.stat(path)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
            self.assertEqual(indexer.mp4_ready(path, cache), (True, "video_ready"))
        self.assertEqual(run.call_count, 2)

    def test_probe_version_change_invalidates_cache(self):
        path = self.mp4()
        stat = os.stat(path)
        cache = {path: {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                        "probe_version": "old", "ready": True, "why": "video_ready"}}
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result()) as run:
            self.assertTrue(indexer.mp4_ready(path, cache)[0])
        run.assert_called_once()

    def test_one_frame_is_rejected_even_with_positive_duration(self):
        path = self.mp4()
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result("60", "1")):
            self.assertEqual(indexer.mp4_ready(path, {}), (False, "too_few_frames"))

    def test_unknown_frame_count_with_valid_duration_passes(self):
        path = self.mp4()
        for frames in ("N/A", None):
            with self.subTest(frames=frames), mock.patch.object(
                    indexer.subprocess, "run", return_value=probe_result("60", frames)):
                self.assertTrue(indexer.mp4_ready(path, {})[0])

    def test_invalid_or_nonfinite_duration_is_rejected(self):
        path = self.mp4()
        for duration in ("N/A", None, "0", "-1", "nan", "inf", "-inf"):
            with self.subTest(duration=duration), mock.patch.object(
                    indexer.subprocess, "run", return_value=probe_result(duration)):
                self.assertEqual(indexer.mp4_ready(path, {}), (False, "invalid_duration"))

    def test_container_duration_can_supply_missing_stream_duration(self):
        path = self.mp4()
        result = probe_result("60", streams=[{"codec_type": "video", "nb_frames": "N/A"}])
        with mock.patch.object(indexer.subprocess, "run", return_value=result):
            self.assertTrue(indexer.mp4_ready(path, {})[0])

    def test_no_video_stream_is_rejected(self):
        path = self.mp4()
        for streams in ([], [{"codec_type": "audio", "duration": "60"}]):
            with self.subTest(streams=streams), mock.patch.object(
                    indexer.subprocess, "run", return_value=probe_result(streams=streams)):
                self.assertEqual(indexer.mp4_ready(path, {}), (False, "no_video_stream"))

    def test_probe_execution_failure_is_closed_and_retried(self):
        path = self.mp4()
        for failure, expected in ((FileNotFoundError("missing"), "probe_failed"),
                                  (subprocess.TimeoutExpired("ffprobe", 15), "probe_timeout")):
            cache = {}
            with self.subTest(failure=expected), mock.patch.object(
                    indexer.subprocess, "run", side_effect=failure):
                self.assertEqual(indexer.mp4_ready(path, cache), (False, expected))
                self.assertNotIn(path, cache)
            with mock.patch.object(indexer.subprocess, "run", return_value=probe_result()):
                self.assertTrue(indexer.mp4_ready(path, cache)[0])

    def test_nonzero_probe_exit_and_invalid_json_do_not_pass(self):
        path = self.mp4()
        results = [(probe_result(returncode=1), "probe_failed"),
                   (SimpleNamespace(returncode=0, stdout="not json"), "probe_invalid")]
        for result, expected in results:
            with self.subTest(expected=expected), mock.patch.object(
                    indexer.subprocess, "run", return_value=result):
                self.assertEqual(indexer.mp4_ready(path, {}), (False, expected))

    def test_unfinished_or_truncated_mp4_never_reaches_probe(self):
        contents = [
            (box(b"ftyp", b"isom") + box(b"mdat", b"frames"), "no_moov"),
            (box(b"moov", b"metadata") + (100).to_bytes(4, "big") + b"mdat" + b"short", "truncated"),
            (box(b"moov", b"metadata") + b"tail", "truncated"),
            ((1).to_bytes(4, "big") + b"moov" + (12).to_bytes(8, "big"), "truncated"),
        ]
        for content, expected in contents:
            with self.subTest(expected=expected):
                path = self.mp4(content=content)
                with mock.patch.object(indexer.subprocess, "run") as run:
                    self.assertEqual(indexer.mp4_ready(path, {}), (False, expected))
                run.assert_not_called()

    def test_file_changing_during_probe_is_not_published(self):
        path = self.mp4()
        def change_file(*args, **kwargs):
            with open(path, "ab") as stream:
                stream.write(box(b"free"))
            return probe_result()
        cache = {}
        with mock.patch.object(indexer.subprocess, "run", side_effect=change_file):
            self.assertEqual(indexer.mp4_ready(path, cache), (False, "changed_during_probe"))
        self.assertNotIn(path, cache)

    def make_event(self):
        kind, name = "SavedClips", "2026-10-19_23-42-49"
        event = self.root / "TeslaCam" / kind / name
        event.mkdir(parents=True)
        (event / "event.json").write_text('{"timestamp":"2026-10-19T23:42:47"}', encoding="utf-8")
        (event / "thumb.png").write_bytes(b"thumbnail")
        source = self.mp4(f"TeslaCam/{kind}/{name}/2026-10-19_23-41-59-front.mp4")
        return kind, name, event, source

    def test_bad_source_proxy_and_mosaic_are_not_published(self):
        kind, name, event, source = self.make_event()
        bad_source = self.mp4(f"TeslaCam/{kind}/{name}/2026-10-19_23-41-59-back.mp4")
        bad_proxy = self.mp4(f"_proxy/{kind}/{name}/cam_front.mp4")
        good_proxy = self.mp4(f"_proxy/{kind}/{name}/cam_right_repeater.mp4")
        mosaic = self.mp4(f"_proxy/{kind}/{name}/mosaic.mp4", content=box(b"ftyp", b"isom"))
        (Path(mosaic).parent / "mosaic.json").write_text('{"minutes":[{"start":0,"dur":60}]}')
        before = {p: Path(p).read_bytes() for p in (source, bad_source, bad_proxy, good_proxy, mosaic)}
        def classify(args, **kwargs):
            return probe_result("N/A", "1") if args[-1] in (bad_source, bad_proxy) else probe_result()
        audit = fresh_audit()
        with mock.patch.object(indexer.subprocess, "run", side_effect=classify):
            result = indexer.scan_one_event(str(event), kind, {}, audit, time.time() + 600)
        self.assertEqual(result["cams"], ["front"])
        self.assertEqual(result["minutes"], 1)
        self.assertNotIn("front", result["proxies"])
        self.assertEqual(result["proxies"], {"right_repeater": indexer.rel(good_proxy)})
        self.assertIsNone(result["mosaic"])
        self.assertEqual(result["mosaic_map"], [])
        self.assertEqual(result["source_errors"][indexer.rel(bad_source)], "too_few_frames")
        self.assertEqual(result["proxy_errors"]["front"], "too_few_frames")
        self.assertEqual(result["proxy_errors"]["mosaic"], "no_moov")
        self.assertTrue(any(indexer.rel(bad_proxy) in item for item in audit["unfinished"]))
        self.assertEqual(before, {p: Path(p).read_bytes() for p in before})

    def test_valid_proxy_and_mosaic_remain_compatible(self):
        kind, name, event, source = self.make_event()
        proxy = self.mp4(f"_proxy/{kind}/{name}/cam_front.mp4")
        mosaic = self.mp4(f"_proxy/{kind}/{name}/mosaic.mp4")
        mapping = [{"key": "2026-10-19_23-41-59", "start": 0, "dur": 60}]
        (Path(mosaic).parent / "mosaic.json").write_text(json.dumps({"minutes": mapping}))
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result()):
            result = indexer.scan_one_event(str(event), kind, {}, fresh_audit(), time.time() + 600)
        self.assertEqual(result["proxies"]["front"], indexer.rel(proxy))
        self.assertEqual(result["mosaic"], indexer.rel(mosaic))
        self.assertEqual(result["mosaic_map"], mapping)
        self.assertEqual(result["clips"]["front"], indexer.rel(source))
        self.assertEqual(result["proxy_errors"], {})

    def test_main_keeps_invalid_events_visible_but_without_media(self):
        kind, name, event, source = self.make_event()
        with mock.patch.object(indexer.subprocess, "run", return_value=probe_result("N/A", "1")):
            indexer.main()
        result = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["clip_map"], {})
        self.assertIn("no_clips", result["events"][0]["status_reasons"])
        self.assertEqual(result["events"][0]["source_errors"][indexer.rel(source)], "too_few_frames")
        self.assertTrue((self.root / "audit.json").is_file())
        self.assertTrue((self.root / ".index-cache.json").is_file())
        self.assertTrue(Path(source).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
