"""Offline regression tests. No device, credentials or ffmpeg are used.

Run with the bundled Python and -B to avoid __pycache__:
  python -B diagnostics/test_teslacam_live_v4.py
All generated files live in temporary directories below diagnostics/.
"""
import concurrent.futures
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("teslacam_serve_v4", HERE.parent / "web" / "serve.py")
SERVICE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICE)
KEYS = ("2026-10-20_01-02-03", "2026-10-20_01-03-03", "2026-10-20_01-04-03")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".teslacam-test-", dir=HERE)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "events"
        self.live = self.base / "live"
        self.recent = self.root / "TeslaCam" / "RecentClips"
        self.recent.mkdir(parents=True)
        self.live.mkdir()
        self.index = {"minutes": [], "live": None}
        for key in KEYS:
            clips = {}
            for cam in SERVICE.QUAD:
                relative = f"TeslaCam/RecentClips/{key}-{cam}.mp4"
                path = self.root / relative
                path.write_bytes(f"original-{key}-{cam}".encode())
                clips[cam] = relative
            self.index["minutes"].append({"key": key, "clips": clips})
        self.index["live"] = self.index["minutes"][-1]
        self.write_index()
        self.manager = SERVICE.LiveManager(self.root, self.live, self.base / "encode.lock", start_worker=False)

    def write_index(self):
        (self.root / "index.json").write_text(json.dumps(self.index), encoding="utf-8")

    def expect_preview_error(self, status, callback):
        with self.assertRaises(SERVICE.PreviewError) as caught:
            callback()
        self.assertEqual(caught.exception.status, status)

    def job(self, view="quad"):
        status, _ = self.manager.request(view, KEYS[0])
        self.assertEqual(status, 202)
        return self.manager.pending.get_nowait()


class LiveRequestTests(Fixture):
    def test_repeated_signature_is_one_job(self):
        self.assertEqual(self.manager.request("quad", KEYS[0])[0], 202)
        self.assertEqual(self.manager.request("quad", KEYS[0])[0], 202)
        self.assertEqual(self.manager.pending.qsize(), 1)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_concurrent_same_signature_is_deduplicated(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.manager.request("quad", KEYS[0]), range(16)))
        self.assertEqual({result[0] for result in results}, {202})
        self.assertEqual(self.manager.pending.qsize(), 1)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_pending_queue_is_bounded_and_returns_429(self):
        self.manager.request("quad", KEYS[0])
        status, data = self.manager.request("quad", KEYS[1])
        self.assertEqual(status, 429)
        self.assertEqual(data["status"], "busy")
        self.assertEqual(self.manager.pending.qsize(), 1)
        self.assertEqual(len(self.manager.jobs), 1)

    def test_processing_job_allows_only_one_waiting_job(self):
        processing = self.job()
        processing["status"] = "processing"
        self.assertEqual(self.manager.request("quad", KEYS[0])[0], 202)
        self.assertEqual(self.manager.request("quad", KEYS[1])[0], 202)
        self.assertEqual(self.manager.request("quad", KEYS[2])[0], 429)

    def test_source_change_creates_a_new_version(self):
        first = self.job()
        source = Path(first["sources"][0])
        source.write_bytes(source.read_bytes() + b"changed")
        status, _ = self.manager.request("quad", KEYS[0])
        self.assertEqual(status, 202)
        second = self.manager.pending.get_nowait()
        self.assertNotEqual(first["signature"], second["signature"])
        self.assertNotEqual(first["name"], second["name"])

    def test_ready_cache_has_stable_versioned_url(self):
        job = self.job()
        (self.live / job["name"]).write_bytes(b"validated-cache")
        job.update(status="ready", duration=59.8)
        status, data = self.manager.request("quad", KEYS[0])
        self.assertEqual(status, 200)
        self.assertRegex(data["url"], r"^live-cache/qv4_2026-10-20_01-02-03_quad_[a-f0-9]{16}\.mp4$")
        self.assertEqual(data["url"], self.manager.request("quad", KEYS[0])[1]["url"])
        self.assertEqual((data["width"], data["height"], data["fps"]), (1280, 720, 12))

    def test_missing_camera_is_404(self):
        del self.index["minutes"][0]["clips"]["back"]
        self.write_index()
        self.expect_preview_error(404, lambda: self.manager.request("quad", KEYS[0]))

    def test_deleted_source_is_404(self):
        (self.root / self.index["minutes"][0]["clips"]["front"]).unlink()
        self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0]))

    def test_invalid_view_is_400(self):
        self.expect_preview_error(400, lambda: self.manager.request("../../front", KEYS[0]))

    def test_invalid_minute_is_404(self):
        self.expect_preview_error(404, lambda: self.manager.request("front", "../../secret"))

    def test_missing_index_is_503(self):
        (self.root / "index.json").unlink()
        self.expect_preview_error(503, lambda: self.manager.request("front", KEYS[0]))

    def test_default_minute_uses_live_key(self):
        status, _ = self.manager.request("quad")
        self.assertEqual(status, 202)
        self.assertEqual(self.manager.pending.get_nowait()["key"], KEYS[-1])

    def test_source_must_stay_in_recent_clips(self):
        outside = self.root / "private.mp4"
        outside.write_bytes(b"private")
        for relative in ("private.mp4", "../private.mp4", str(outside.resolve())):
            with self.subTest(relative=relative):
                self.index["minutes"][0]["clips"]["front"] = relative
                self.write_index()
                self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0]))

    def test_similarly_named_directory_cannot_escape_containment(self):
        sibling = self.root / "TeslaCam" / "RecentClips-evil"
        sibling.mkdir()
        (sibling / "test.mp4").write_bytes(b"private")
        self.index["minutes"][0]["clips"]["front"] = "TeslaCam/RecentClips-evil/test.mp4"
        self.write_index()
        self.expect_preview_error(404, lambda: self.manager.request("front", KEYS[0]))

    def test_failed_job_is_throttled_without_raw_fallback(self):
        job = self.job()
        job.update(status="error", message="encode failed", updated=time.monotonic())
        status, data = self.manager.request("quad", KEYS[0])
        self.assertEqual(status, 503)
        self.assertEqual(data["status"], "error")
        self.assertNotIn("url", data)
        self.assertTrue(self.manager.pending.empty())


class ProbeAndEncodeTests(Fixture):
    def probe_result(self, duration="60.0", frames="720", **video):
        stream = dict(codec_name="h264", pix_fmt="yuv420p", width=1280, height=720,
                      nb_frames=frames)
        stream.update(video)
        return Mock(returncode=0, stdout=json.dumps({"format": {"duration": duration}, "streams": [stream]}))

    def test_probe_rejects_na_duration_and_single_frame(self):
        for duration, frames in (("N/A", "N/A"), ("60", "1"), ("0", "720"), ("nan", "720"), ("inf", "720")):
            with self.subTest(duration=duration, frames=frames), patch.object(SERVICE.subprocess, "run", return_value=self.probe_result(duration, frames)):
                self.expect_preview_error(503, lambda: self.manager._probe("not-opened.mp4"))

    def test_probe_permits_unknown_frame_count_with_valid_duration(self):
        with patch.object(SERVICE.subprocess, "run", return_value=self.probe_result("12.5", "N/A")):
            self.assertEqual(self.manager._probe("not-opened.mp4")[0], 12.5)

    def test_probe_subprocess_failure_is_unavailable(self):
        with patch.object(SERVICE.subprocess, "run", return_value=Mock(returncode=1, stdout="")):
            self.expect_preview_error(503, lambda: self.manager._probe("not-opened.mp4"))

    def test_quad_command_has_cpu_budget_fps_and_shortest_end(self):
        job = self.job()
        cmd = self.manager._command(job, self.live / "output.mp4", 48.25)
        self.assertTrue(cmd[0].endswith("/nice"))
        self.assertEqual(cmd[1:3], ["-n", "15"])
        self.assertEqual(cmd.count("-i"), 4)
        self.assertEqual(cmd[cmd.index("-filter_complex_threads") + 1], "1")
        self.assertEqual(cmd[cmd.index("-filter_threads") + 1], "1")
        self.assertEqual(cmd.count("-threads"), 5)
        for i, argument in enumerate(cmd):
            if argument == "-i":
                self.assertEqual(cmd[i - 2:i], ["-threads", "1"])
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "libx264")
        filters = cmd[cmd.index("-filter_complex") + 1]
        self.assertEqual(filters.count("fps=12"), 4)
        self.assertIn("shortest=1", filters)
        self.assertIn("-shortest", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "48.250000")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")
        self.assertEqual(cmd[cmd.index("-movflags") + 1], "+faststart")

    def test_single_view_command_is_compressed_without_audio(self):
        job = self.job("front")
        cmd = self.manager._command(job, self.live / "output.mp4", 60)
        self.assertEqual(cmd.count("-i"), 1)
        self.assertIn("fps=12", cmd[cmd.index("-vf") + 1])
        self.assertEqual(cmd[cmd.index("-b:v") + 1], "600k")
        for argument in ("-an", "-sn", "-dn", "-shortest"):
            self.assertIn(argument, cmd)

    def test_encode_timeout_cleans_partial_and_never_publishes_raw(self):
        job = self.job()
        def timeout(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"partial-encode")
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        with patch.object(self.manager, "_probe", return_value=(60, {})), patch.object(SERVICE.subprocess, "run", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.manager._encode(job)
        self.assertEqual(list(self.live.iterdir()), [])
        self.assertTrue(all(Path(path).is_file() for path in job["sources"]))

    def test_failed_encode_cleans_partial_without_raw_fallback(self):
        job = self.job()
        def failed(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"bad-encode")
            return Mock(returncode=1, stderr="simulated ffmpeg error")
        with patch.object(self.manager, "_probe", return_value=(60, {})), patch.object(SERVICE.subprocess, "run", side_effect=failed):
            self.expect_preview_error(503, lambda: self.manager._encode(job))
        self.assertEqual(list(self.live.iterdir()), [])

    def test_source_change_during_encode_prevents_publish(self):
        job = self.job()
        def changed(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"compressed")
            Path(job["sources"][0]).write_bytes(b"newly-replaced-original")
            return Mock(returncode=0)
        with patch.object(self.manager, "_probe", return_value=(60, {})), patch.object(SERVICE.subprocess, "run", side_effect=changed):
            self.expect_preview_error(503, lambda: self.manager._encode(job))
        self.assertEqual(list(self.live.iterdir()), [])

    def test_failed_atomic_publish_leaves_no_visible_preview(self):
        job = self.job()
        video = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720}
        def encoded(cmd, **kwargs):
            self.assertFalse((self.live / job["name"]).exists())
            Path(cmd[-1]).write_bytes(b"compressed-only")
            return Mock(returncode=0)
        with patch.object(self.manager, "_probe", return_value=(60, video)), patch.object(self.manager, "_faststart", return_value=True), patch.object(SERVICE.subprocess, "run", side_effect=encoded), patch.object(SERVICE.os, "replace", side_effect=OSError("simulated publish failure")):
            with self.assertRaises(OSError):
                self.manager._encode(job)
        self.assertEqual(list(self.live.iterdir()), [])

    def test_successful_encode_publishes_only_after_validation(self):
        job = self.job()
        video = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720}
        def encoded(cmd, **kwargs):
            self.assertFalse((self.live / job["name"]).exists())
            Path(cmd[-1]).write_bytes(b"compressed-only")
            return Mock(returncode=0)
        with patch.object(self.manager, "_probe", return_value=(59.2, video)), patch.object(self.manager, "_faststart", return_value=True), patch.object(SERVICE.subprocess, "run", side_effect=encoded):
            self.assertEqual(self.manager._encode(job), 59.2)
        self.assertEqual((self.live / job["name"]).read_bytes(), b"compressed-only")
        self.assertEqual(len(list(self.live.iterdir())), 1)

    def test_existing_cache_wrong_dimensions_or_pixel_format_is_regenerated(self):
        job = self.job()
        output = self.live / job["name"]
        output.write_bytes(b"old-wrong-format")
        good = {"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1280, "height": 720}
        bad = {**good, "pix_fmt": "yuv444p", "width": 1920, "height": 1080}
        def probe(path):
            return 60, bad if Path(path) == output and output.read_bytes() == b"old-wrong-format" else good
        def encoded(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"new-correct-format")
            return Mock(returncode=0)
        with patch.object(self.manager, "_probe", side_effect=probe), patch.object(self.manager, "_faststart", return_value=True), patch.object(SERVICE.subprocess, "run", side_effect=encoded) as run:
            self.manager._encode(job)
        self.assertEqual(run.call_count, 1, "wrong-format cache must not bypass output validation")
        self.assertEqual(output.read_bytes(), b"new-correct-format")

    def test_faststart_requires_moov_before_mdat(self):
        path = self.live / "atoms.mp4"
        for content, expected in ((b"\x00\x00\x00\x08moov\x00\x00\x00\x08mdat", True),
                                  (b"\x00\x00\x00\x08mdat\x00\x00\x00\x08moov", False),
                                  (b"\x00\x00\x00\x00free", False)):
            with self.subTest(content=content):
                path.write_bytes(content)
                self.assertEqual(self.manager._faststart(path), expected)


class HTTPTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sample = b"0123456789abcdef"
        (self.root / "sample.mp4").write_bytes(self.sample)
        (self.root / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
        self.cache_name = "qv4_2026-10-20_01-02-03_quad_0123456789abcdef.mp4"
        (self.live / self.cache_name).write_bytes(self.sample)
        self.api = Mock()
        self.api.request.return_value = (200, {"status": "ready", "url": "live-cache/" + self.cache_name})
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

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", headers=None, body=None, authorized=True):
        request_headers = {"Connection": "close"}
        if authorized:
            request_headers["Cookie"] = "tc=test-access-code"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_complete_file_get_and_head(self):
        status, headers, body = self.request("/f/sample.mp4")
        self.assertEqual((status, body), (200, self.sample))
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Content-Length"], "16")
        status, headers, body = self.request("/f/sample.mp4", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertEqual(headers["Content-Length"], "16")

    def test_normal_open_suffix_and_clipped_ranges(self):
        for header, expected, content_range in (("bytes=2-5", b"2345", "bytes 2-5/16"),
                                                ("bytes=12-", b"cdef", "bytes 12-15/16"),
                                                ("bytes=-4", b"cdef", "bytes 12-15/16"),
                                                ("bytes=14-999", b"ef", "bytes 14-15/16"),
                                                ("bytes=-999", self.sample, "bytes 0-15/16")):
            with self.subTest(header=header):
                status, headers, body = self.request("/f/sample.mp4", headers={"Range": header})
                self.assertEqual((status, body), (206, expected))
                self.assertEqual(headers["Content-Range"], content_range)
                self.assertEqual(int(headers["Content-Length"]), len(expected))

    def test_unsatisfiable_reverse_and_zero_suffix_ranges(self):
        for header in ("bytes=16-", "bytes=999-1000", "bytes=8-3", "bytes=-0"):
            with self.subTest(header=header):
                status, headers, body = self.request("/f/sample.mp4", headers={"Range": header})
                self.assertEqual((status, body), (416, b""))
                self.assertEqual(headers["Content-Range"], "bytes */16")

    def test_invalid_or_multiple_ranges_are_ignored_as_complete_get(self):
        # HTTP allows an unsupported or malformed Range header to be ignored.
        for header in ("garbage", "bytes=-", "bytes=a-b", "bytes=0-1,4-5", "items=0-1"):
            with self.subTest(header=header):
                status, _, body = self.request("/f/sample.mp4", headers={"Range": header})
                self.assertEqual((status, body), (200, self.sample))

    def test_head_range_returns_headers_without_body(self):
        status, headers, body = self.request("/f/sample.mp4", method="HEAD", headers={"Range": "bytes=3-7"})
        self.assertEqual((status, body), (206, b""))
        self.assertEqual(headers["Content-Range"], "bytes 3-7/16")
        self.assertEqual(headers["Content-Length"], "5")

    def test_cookie_authentication_requires_exact_value(self):
        self.assertEqual(self.request("/api/live")[0], 200)
        for cookie in ("", "other=test-access-code", "tc=test-access-code-suffix", "tc=prefix-test-access-code", "tc=no; other=test-access-code"):
            with self.subTest(cookie=cookie):
                self.assertEqual(self.request("/api/live", authorized=False, headers={"Cookie": cookie})[0], 401)

    def test_query_authentication_sets_http_only_cookie(self):
        status, headers, _ = self.request("/api/live?k=test-access-code", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn("tc=test-access-code;", headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/live?k=test-access-code-extra", authorized=False)[0], 401)

    def test_unicode_query_access_code_is_rejected_without_crashing(self):
        status, _, _ = self.request("/api/live?k=%E4%B8%AD%E6%96%87", authorized=False)
        self.assertEqual(status, 401)

    def test_post_login_and_invalid_login(self):
        status, headers, body = self.request("/", method="POST", body="k=test-access-code", authorized=False,
                                           headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "./")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(body, b"")
        status, _, body = self.request("/", method="POST", body="k=wrong", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<form", body)

    def test_unicode_post_access_code_is_rejected_without_crashing(self):
        status, _, body = self.request("/", method="POST", body="k=%E4%B8%AD%E6%96%87", authorized=False)
        self.assertEqual(status, 200)
        self.assertIn(b"<form", body)

    def test_api_and_index_are_no_store_cache_is_private_immutable(self):
        for path in ("/api/live", "/index.json", "/index.html", "/f/sample.mp4"):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-store")
        status, headers, body = self.request("/live-cache/" + self.cache_name)
        self.assertEqual((status, body), (200, self.sample))
        self.assertEqual(headers["Cache-Control"], "private, max-age=31536000, immutable")

    def test_queued_preview_has_retry_after_and_no_original_url(self):
        self.api.request.return_value = (202, {"status": "queued", "retry_after": 2})
        status, headers, body = self.request("/api/live?view=quad&minute=" + KEYS[0])
        self.assertEqual(status, 202)
        self.assertEqual(headers["Retry-After"], "2")
        self.assertNotIn("url", json.loads(body))
        self.api.request.assert_called_once_with("quad", KEYS[0])

    def test_legacy_preview_never_falls_back_to_original(self):
        self.api.request.return_value = (503, {"status": "error", "message": "failed"})
        status, headers, body = self.request("/live/quad")
        self.assertEqual(status, 503)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertNotIn("url", json.loads(body))

    def test_paths_cannot_escape_root_or_cache(self):
        (self.base / "secret.txt").write_bytes(b"must-not-leak")
        for path, expected_status in (("/f/../secret.txt", 403), ("/f/%2e%2e/secret.txt", 403),
                                      ("/f/", 403), ("/live-cache/../secret.txt", 404),
                                      ("/live-cache/not-a-versioned-cache.mp4", 404)):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, expected_status)
                self.assertNotIn(b"must-not-leak", body)

    def test_missing_files_and_unknown_endpoints_are_404(self):
        self.assertEqual(self.request("/f/missing.mp4")[0], 404)
        self.assertEqual(self.request("/unknown")[0], 404)
        self.assertEqual(self.request("/live-cache/qv4_2026-10-20_01-02-03_quad_ffffffffffffffff.mp4")[0], 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
