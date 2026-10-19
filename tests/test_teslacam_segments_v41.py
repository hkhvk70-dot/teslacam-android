"""Independent v4.1 regression tests; no phone, credentials, or FFmpeg required.

The old v4 suite remains unchanged. This suite exercises the new 15-second
contract, event isolation, queue admission and already-ready live previews.
"""
import concurrent.futures
import http.server
import inspect
import json
import os
from pathlib import Path
import time
import threading
import unittest
from unittest.mock import Mock, patch

import test_teslacam_live_v4 as OLD

SERVICE = OLD.SERVICE
KEYS = OLD.KEYS
EVENT = "SentryClips/2026-10-20_01-00-00"
OTHER_EVENT = "SavedClips/2026-10-20_01-00-00"


class SegmentFixture(OLD.Fixture):
    def setUp(self):
        super().setUp()
        self.index["events"] = []
        for identifier in (EVENT, OTHER_EVENT):
            mapping = {}
            for key in KEYS:
                clips = {}
                for camera in SERVICE.QUAD:
                    relative = f"TeslaCam/{identifier}/{key}-{camera}.mp4"
                    path = self.root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f"event-{identifier}-{key}-{camera}".encode())
                    clips[camera] = relative
                mapping[key] = clips
            self.index["events"].append({"id": identifier, "clip_map": mapping})
        self.write_index()

    def new_job(self, view="quad", minute=KEYS[0], **options):
        status, _ = self.manager.request(view, minute, **options)
        self.assertEqual(status, 202)
        return self.manager.pending.get_nowait()

    def mark_ready(self, job, duration=15):
        (self.live / job["name"]).write_bytes(b"verified-h264-video")
        job.update(status="ready", duration=duration, source_duration=60, updated=time.monotonic())

    def encode_and_capture(self, job, source_duration=60):
        video = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720}
        if job["view"] != "quad":
            video.update(width=960, height=540)
        command = []
        def encoded(arguments, **kwargs):
            command[:] = arguments
            Path(arguments[-1]).write_bytes(b"validated-encoded-segment")
            return Mock(returncode=0, stderr="")
        def probe(path):
            if str(path) in job["sources"]:
                return source_duration, video
            offset = 15 * int(job.get("part") or 0)
            return min(15, source_duration - offset), video
        with patch.object(self.manager, "_probe", side_effect=probe), \
                patch.object(self.manager, "_faststart", return_value=True), \
                patch.object(SERVICE.subprocess, "run", side_effect=encoded):
            duration = self.manager._encode(job)
        return command, duration


class SegmentBoundaryTests(SegmentFixture):
    def test_segment_zero_is_bounded_to_fifteen_seconds(self):
        command, duration = self.encode_and_capture(self.new_job(part=0))
        self.assertEqual(float(command[command.index("-t") + 1]), 15)
        self.assertEqual(duration, 15)

    def test_nonzero_segment_seeks_each_input_before_decoding(self):
        command, duration = self.encode_and_capture(self.new_job(part=2))
        self.assertEqual(command.count("-i"), 4)
        self.assertEqual(command.count("-ss"), 4)
        previous_input = -1
        for position, argument in enumerate(command):
            if argument != "-i":
                continue
            preceding = command[previous_input + 2:position]
            self.assertIn("-ss", preceding)
            self.assertEqual(float(preceding[preceding.index("-ss") + 1]), 30)
            previous_input = position
        self.assertEqual(duration, 15)

    def test_final_partial_segment_uses_remaining_duration(self):
        command, duration = self.encode_and_capture(self.new_job(part=3), 59.75)
        self.assertAlmostEqual(float(command[command.index("-t") + 1]), 14.75)
        self.assertAlmostEqual(duration, 14.75)

    def test_segment_beyond_end_does_not_start_encoder(self):
        job = self.new_job(part=4)
        with patch.object(self.manager, "_probe", return_value=(59.75, {})), \
                patch.object(SERVICE.subprocess, "run") as encoder:
            self.expect_preview_error(404, lambda: self.manager._encode(job))
        encoder.assert_not_called()
        self.assertEqual(list(self.live.iterdir()), [])

    def test_part_outside_supported_bounds_is_rejected(self):
        for part in (-1, 8, 1000000):
            with self.subTest(part=part):
                self.expect_preview_error(400, lambda: self.manager.request("quad", KEYS[0], part=part))
        self.assertTrue(self.manager.pending.empty())

    def test_single_camera_segment_also_uses_input_seek(self):
        command, duration = self.encode_and_capture(self.new_job("front", part=1))
        self.assertEqual(command.count("-i"), 1)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(float(command[command.index("-ss") + 1]), 15)
        self.assertEqual(duration, 15)

    def test_overlong_segment_cache_is_regenerated_in_worker(self):
        job = self.new_job(part=0)
        output = self.live / job["name"]
        output.write_bytes(b"old-overlong-cache")
        video = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720}
        def probe(path):
            if Path(path) == output and output.read_bytes() == b"old-overlong-cache":
                return 60, video
            return (60 if str(path) in job["sources"] else 15), video
        def encoded(arguments, **kwargs):
            Path(arguments[-1]).write_bytes(b"new-short-segment")
            return Mock(returncode=0, stderr="")
        with patch.object(self.manager, "_probe", side_effect=probe), \
                patch.object(self.manager, "_faststart", return_value=True), \
                patch.object(SERVICE.subprocess, "run", side_effect=encoded) as encoder:
            duration = self.manager._encode(job)
        self.assertEqual(encoder.call_count, 1)
        self.assertEqual(duration, 15)
        self.assertEqual(output.read_bytes(), b"new-short-segment")

    def test_ready_metadata_tells_client_where_segment_belongs(self):
        job = self.new_job(part=2, event=EVENT)
        self.mark_ready(job)
        status, response = self.manager.request("quad", KEYS[0], part=2, event=EVENT)
        self.assertEqual(status, 200)
        self.assertEqual(response["part"], 2)
        self.assertEqual(response["offset"], 30)
        self.assertEqual(response["event"], EVENT)
        self.assertEqual(response["next_part"], 3)

    def test_last_segment_has_no_next_segment(self):
        job = self.new_job(part=3)
        self.mark_ready(job, 14.75)
        job["source_duration"] = 59.75
        status, response = self.manager.request("quad", KEYS[0], part=3)
        self.assertEqual(status, 200)
        self.assertIsNone(response["next_part"])


class EventSelectionSecurityTests(SegmentFixture):
    def test_indexed_event_uses_its_original_clips(self):
        job = self.new_job(part=0, event=EVENT)
        expected = self.index["events"][0]["clip_map"][KEYS[0]]
        self.assertEqual(set(job["sources"]), {str((self.root / p).resolve()) for p in expected.values()})

    def test_event_without_explicit_part_defaults_to_first_segment(self):
        job = self.new_job(event=EVENT)
        self.assertEqual(job["part"], 0)
        self.assertEqual(job["offset"], 0)

    def test_event_cannot_select_a_minute_from_another_event(self):
        del self.index["events"][0]["clip_map"][KEYS[1]]
        self.write_index()
        self.expect_preview_error(404, lambda: self.manager.request("quad", KEYS[1], part=0, event=EVENT))

    def test_unknown_or_traversing_event_is_not_opened(self):
        for event in ("SentryClips/missing", "../private", "../../events/TeslaCam", str(self.base.resolve())):
            with self.subTest(event=event):
                self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0], part=0, event=event))

    def test_event_paths_cannot_escape_selected_event_directory(self):
        mapping = self.index["events"][0]["clip_map"][KEYS[0]]
        other = self.index["events"][1]["clip_map"][KEYS[0]]["front"]
        for relative in (other, f"TeslaCam/{EVENT}/../../RecentClips/{KEYS[0]}-front.mp4",
                         self.index["minutes"][0]["clips"]["front"]):
            with self.subTest(relative=relative):
                mapping["front"] = relative
                self.write_index()
                self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0], part=0, event=EVENT))

    def test_event_cannot_use_an_arbitrary_file_from_same_directory(self):
        # A valid-looking .mp4 path still needs to come from clip_map.
        unindexed = self.root / f"TeslaCam/{EVENT}/2026-10-20_08-00-00-front.mp4"
        unindexed.write_bytes(b"not-indexed")
        self.expect_preview_error(404, lambda: self.manager.request("front", "2026-10-20_08-00-00", part=0, event=EVENT))

    def test_deleted_event_source_returns_unavailable(self):
        relative = self.index["events"][0]["clip_map"][KEYS[0]]["front"]
        (self.root / relative).unlink()
        self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0], part=0, event=EVENT))

    def test_recent_preview_cannot_be_pointed_at_event_directory(self):
        self.index["minutes"][0]["clips"]["front"] = self.index["events"][0]["clip_map"][KEYS[0]]["front"]
        self.write_index()
        self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0], part=0))

    def test_source_directory_symlink_cannot_escape_teslacam_root(self):
        # Windows may restrict symlink creation. In that case emulate exactly
        # the resolved directory and file paths, keeping the containment test.
        real_resolve = Path.resolve
        for scope, event in (("RecentClips", None), (EVENT, EVENT)):
            with self.subTest(scope=scope):
                isolated = self.base / ("symlink-recent" if event is None else "symlink-event")
                isolated.mkdir()
                target = isolated / "outside-originals"
                target.mkdir()
                for camera in SERVICE.QUAD:
                    (target / f"{KEYS[0]}-{camera}.mp4").write_bytes(b"outside-video")
                root = isolated / "events"
                root.mkdir()
                (root / "index.json").write_text(json.dumps(self.index), encoding="utf-8")
                link = root / "TeslaCam" / scope
                link.parent.mkdir(parents=True, exist_ok=True)
                manager = SERVICE.LiveManager(root, self.live, isolated / "encode.lock", start_worker=False)
                try:
                    link.symlink_to(target, target_is_directory=True)
                except OSError:
                    def redirected_resolve(path, *args, **kwargs):
                        try:
                            suffix = path.relative_to(link)
                        except ValueError:
                            return real_resolve(path, *args, **kwargs)
                        return real_resolve(target / suffix, *args, **kwargs)
                    with patch.object(SERVICE.Path, "resolve", redirected_resolve):
                        self.expect_preview_error(404, lambda: manager.request("front", KEYS[0], event=event, part=0))
                else:
                    self.expect_preview_error(404, lambda: manager.request("front", KEYS[0], event=event, part=0))
                self.assertTrue(manager.pending.empty())


class SignatureAndAdmissionTests(SegmentFixture):
    def test_different_segments_have_distinct_cache_urls(self):
        one = self.new_job(part=0)
        two = self.new_job(part=1)
        self.assertNotEqual(one["name"], two["name"])

    def test_full_minute_and_segment_zero_cannot_share_cache(self):
        full = self.new_job()
        segment = self.new_job(part=0)
        self.assertNotEqual(full["name"], segment["name"])

    def test_event_and_recent_same_minute_cannot_share_cache(self):
        recent = self.new_job(part=0)
        event = self.new_job(part=0, event=EVENT)
        other = self.new_job(part=0, event=OTHER_EVENT)
        self.assertEqual(len({recent["name"], event["name"], other["name"]}), 3)

    def test_changing_event_original_invalidates_segment_version(self):
        before = self.new_job(part=0, event=EVENT)
        source = Path(before["sources"][0])
        source.write_bytes(source.read_bytes() + b"updated")
        after = self.new_job(part=0, event=EVENT)
        self.assertNotEqual(before["name"], after["name"])

    def test_same_segment_concurrency_shares_one_job(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.manager.request("quad", KEYS[0], part=0), range(24)))
        self.assertEqual({status for status, _ in results}, {202})
        self.assertEqual(self.manager.pending.qsize(), 1)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_foreground_replaces_waiting_prefetch(self):
        self.manager.request("quad", KEYS[0], part=1, priority="prefetch")
        status, _ = self.manager.request("front", KEYS[1], part=0)
        self.assertEqual(status, 202)
        self.assertEqual(self.manager.pending.qsize(), 1)
        admitted = self.manager.pending.get_nowait()
        self.assertEqual((admitted["key"], admitted["view"]), (KEYS[1], "front"))

    def test_prefetch_never_displaces_waiting_foreground(self):
        self.manager.request("front", KEYS[0], part=0)
        status, _ = self.manager.request("quad", KEYS[1], part=0, priority="prefetch")
        self.assertEqual(status, 429)
        self.assertEqual(self.manager.pending.qsize(), 1)
        self.assertEqual(self.manager.pending.get_nowait()["view"], "front")

    def test_active_prefetch_is_not_cancelled_by_foreground(self):
        active = self.new_job(part=0, priority="prefetch")
        active["status"] = "processing"
        status, _ = self.manager.request("front", KEYS[1], part=0)
        self.assertEqual(status, 202)
        self.assertEqual(active["status"], "processing")
        self.assertIs(self.manager.jobs[active["name"]], active)
        self.assertEqual(self.manager.pending.qsize(), 1)

    def test_foreground_claim_of_same_prefetch_protects_it_from_replacement(self):
        self.manager.request("quad", KEYS[0], part=0, priority="prefetch")
        self.manager.request("quad", KEYS[0], part=0)
        status, _ = self.manager.request("front", KEYS[1], part=0)
        self.assertEqual(status, 429)
        self.assertEqual(self.manager.pending.get_nowait()["view"], "quad")

    def test_replaced_prefetch_does_not_leak_unfinished_queue_tasks(self):
        self.manager.request("quad", KEYS[0], part=0, priority="prefetch")
        self.manager.request("front", KEYS[1], part=0)
        self.manager.pending.get_nowait()
        self.manager.pending.task_done()
        self.assertEqual(self.manager.pending.unfinished_tasks, 0)


class ReadyCacheTests(SegmentFixture):
    def probe_video(self, path):
        view = "front" if "_front_" in str(path) else "quad"
        video = {"codec_name": "h264", "pix_fmt": "yuv420p",
                 "width": 960 if view == "front" else 1280,
                 "height": 540 if view == "front" else 720}
        return (15 if Path(path).parent == self.live else 60), video

    def cached(self, view="quad", **options):
        with patch.object(self.manager, "_probe", side_effect=self.probe_video), \
                patch.object(self.manager, "_faststart", return_value=True):
            return self.manager.request(view, cached=True, **options)

    def test_cache_miss_never_queues_new_work(self):
        self.expect_preview_error(404, lambda: self.cached())
        self.assertTrue(self.manager.pending.empty())
        self.assertEqual(self.manager.jobs, {})

    def test_latest_ready_selected_view_is_immediately_returned(self):
        first = self.new_job(minute=KEYS[0], part=0)
        latest = self.new_job(minute=KEYS[2], part=0)
        self.mark_ready(first)
        self.mark_ready(latest)
        status, response = self.cached()
        self.assertEqual(status, 200)
        self.assertEqual(response["key"], KEYS[2])
        self.assertTrue(self.manager.pending.empty())

    def test_cache_does_not_substitute_another_camera(self):
        other_view = self.new_job("front", part=0)
        self.mark_ready(other_view)
        self.expect_preview_error(404, lambda: self.cached("quad"))
        self.assertTrue(self.manager.pending.empty())

    def test_event_preview_is_never_returned_as_live_cache(self):
        event = self.new_job(part=0, event=EVENT)
        self.mark_ready(event)
        self.expect_preview_error(404, lambda: self.cached())
        self.assertTrue(self.manager.pending.empty())

    def test_cache_older_than_three_capture_minutes_is_not_returned(self):
        old_key = "2026-10-20_01-00-00"
        clips = {}
        for camera in SERVICE.QUAD:
            relative = f"TeslaCam/RecentClips/{old_key}-{camera}.mp4"
            (self.root / relative).write_bytes(b"old-original")
            clips[camera] = relative
        self.index["minutes"].append({"key": old_key, "clips": clips})
        self.write_index()
        old = self.new_job(minute=old_key, part=0)
        self.mark_ready(old)
        self.expect_preview_error(404, lambda: self.cached())
        self.assertTrue(self.manager.pending.empty())

    def test_recent_cache_is_usable_even_when_wall_clock_is_much_later(self):
        # Freshness is relative to the latest available capture. The UI labels
        # true wall-clock age separately instead of inventing a live timestamp.
        ready = self.new_job(minute=KEYS[1], part=0)
        self.mark_ready(ready)
        with patch.object(SERVICE.time, "time", return_value=2_000_000_000):
            status, response = self.cached()
        self.assertEqual((status, response["key"]), (200, KEYS[1]))

    def test_changed_original_prevents_serving_stale_versioned_cache(self):
        ready = self.new_job(part=0)
        self.mark_ready(ready)
        source = Path(ready["sources"][0])
        source.write_bytes(source.read_bytes() + b"changed")
        self.expect_preview_error(404, lambda: self.cached())

    def test_deleted_cache_file_is_not_reported_as_ready(self):
        ready = self.new_job(part=0)
        self.mark_ready(ready)
        (self.live / ready["name"]).unlink()
        self.expect_preview_error(404, lambda: self.cached())

    def test_ready_cache_is_served_when_waiting_queue_is_full(self):
        ready = self.new_job(part=0)
        self.mark_ready(ready)
        self.manager.request("front", KEYS[1], part=0)
        status, response = self.cached()
        self.assertEqual((status, response["key"]), (200, KEYS[0]))
        self.assertEqual(self.manager.pending.qsize(), 1)

    def test_on_disk_cache_survives_service_restart_and_queue_contention(self):
        ready = self.new_job(part=0)
        self.mark_ready(ready)
        self.manager = SERVICE.LiveManager(self.root, self.live, self.base / "encode.lock", start_worker=False)
        self.manager.request("front", KEYS[1], part=0)
        with patch.object(self.manager, "_probe", side_effect=self.probe_video), \
                patch.object(self.manager, "_faststart", return_value=True):
            status, response = self.manager.request("quad", KEYS[0], part=0)
        self.assertEqual((status, response["key"]), (200, KEYS[0]))
        self.assertEqual(self.manager.pending.qsize(), 1)

    def test_cache_validation_failure_does_not_expose_unverified_video(self):
        ready = self.new_job(part=0)
        self.mark_ready(ready)
        self.manager = SERVICE.LiveManager(self.root, self.live, self.base / "encode.lock", start_worker=False)
        with patch.object(self.manager, "_probe", return_value=(15, {"codec_name": "hevc", "pix_fmt": "yuv420p", "width": 1280, "height": 720})), \
                patch.object(self.manager, "_faststart", return_value=True):
            self.expect_preview_error(404, lambda: self.manager.request("quad", cached=True))
        self.assertTrue(self.manager.pending.empty())


class SegmentHTTPTests(SegmentFixture):
    def setUp(self):
        super().setUp()
        (self.root / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
        self.api = Mock()
        self.api.request.return_value = (202, {"status": "queued", "retry_after": 2})
        self.globals = patch.multiple(SERVICE, ROOT=str(self.root), LIVE=str(self.live), TOKEN="test-access-code", MANAGER=self.api)
        self.globals.start()
        self.addCleanup(self.globals.stop)
        self.silence = patch.object(SERVICE.H, "log_request", return_value=None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SERVICE.H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    stop_server = OLD.HTTPTests.stop_server
    request = OLD.HTTPTests.request

    def passed_options(self):
        arguments, options = self.api.request.call_args
        bound = inspect.signature(SERVICE.LiveManager.request).bind(None, *arguments, **options)
        bound.apply_defaults()
        return bound.arguments

    def test_event_minute_and_part_are_forwarded(self):
        status, headers, body = self.request(f"/api/live?view=front&event=SentryClips%2F2026-10-20_01-00-00&minute={KEYS[0]}&part=2")
        self.assertEqual(status, 202)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Retry-After"], "2")
        options = self.passed_options()
        self.assertEqual((options["view"], options["minute"], options["event"], options["part"]), ("front", KEYS[0], EVENT, 2))
        self.assertNotIn("url", json.loads(body))

    def test_noninteger_part_is_a_client_error_without_enqueuing(self):
        for part in ("abc", "1.5", "nan"):
            with self.subTest(part=part):
                self.api.request.reset_mock()
                status, _, _ = self.request(f"/api/live?part={part}")
                self.assertEqual(status, 400)
                self.api.request.assert_not_called()

    def test_cached_query_requests_no_new_job(self):
        self.request("/api/live?view=quad&cached=1")
        self.assertTrue(self.passed_options()["cached"])

    def test_prefetch_priority_is_forwarded(self):
        self.request(f"/api/live?view=front&minute={KEYS[0]}&part=1&priority=prefetch")
        self.assertEqual(self.passed_options()["priority"], "prefetch")

    def test_versioned_segment_cache_supports_range_playback(self):
        name = "qv5_2026-10-20_01-02-03_quad_p2_0123456789abcdef.mp4"
        (self.live / name).write_bytes(b"0123456789abcdef")
        status, headers, body = self.request("/live-cache/" + name, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, body), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/16")
        self.assertEqual(headers["Cache-Control"], "private, max-age=31536000, immutable")

    def test_unauthenticated_event_requests_never_reach_manager(self):
        status, _, _ = self.request(f"/api/live?event=SentryClips%2F2026-10-20_01-00-00&minute={KEYS[0]}&part=0", authorized=False)
        self.assertEqual(status, 401)
        self.api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
