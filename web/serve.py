#!/data/data/com.termux/files/usr/bin/python3
"""TeslaCam HTTP service: versioned, bounded, low-priority live previews."""
import hashlib
import datetime
import hmac
import http.cookies
import http.server
import ipaddress
import json
import math
import os
from pathlib import Path
import queue
import re
import socket
import socketserver
import subprocess
import threading
import time
import urllib.parse

try:
    import fcntl
except ImportError:  # Offline Windows tests only; Android provides fcntl.
    fcntl = None

ROOT = os.environ.get("ROOT", "/data/teslacam/events")
PORT = int(os.environ.get("PORT", "8081"))
TOKEN = os.environ.get("TOKEN", "")
LIVE = os.environ.get("LIVE_DIR", "/data/teslacam/live")
P = "/data/data/com.termux/files/usr"
FFMPEG = os.environ.get("FFMPEG", P + "/bin/ffmpeg")
FFPROBE = os.environ.get("FFPROBE", P + "/bin/ffprobe")
ENCODE_LOCK = os.environ.get("ENCODE_LOCK", "/data/local/tmp/teslacam-encode.lock")
CAMS = ("front", "back", "left_repeater", "right_repeater", "left_pillar", "right_pillar")
QUAD = CAMS[:4]
MINUTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
CACHE_RE = re.compile(r"^(?:qv4_[\d_-]+_[a-z_]+|qv5_[\d_-]+_[a-z_]+_p[0-7])_[a-f0-9]{16}\.mp4$")
SEGMENT_SECONDS = 15
CT = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg",
      ".json": "application/json; charset=utf-8", ".html": "text/html; charset=utf-8"}


class PreviewError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


class LiveManager:
    """One worker and one waiting job; repeated requests share a job/cache."""
    def __init__(self, root=ROOT, live=LIVE, lockfile=ENCODE_LOCK, start_worker=True):
        self.root = Path(root).resolve()
        self.live = Path(live).resolve()
        self.lockfile = lockfile
        self.lock = threading.Lock()
        self.pending = queue.Queue(maxsize=1)
        self.jobs = {}
        self.validated = {}
        self.live.mkdir(parents=True, exist_ok=True)
        if start_worker:
            threading.Thread(target=self._worker, daemon=True, name="live-preview").start()

    def sources(self, view, minute, event=None):
        if view not in ("quad",) + CAMS:
            raise PreviewError("不支持的镜头", 400)
        try:
            with open(self.root / "index.json", encoding="utf-8-sig") as f:
                index = json.load(f)
        except (OSError, ValueError):
            raise PreviewError("录像索引暂不可用", 503)
        selected_event = None
        if event is not None:
            if not isinstance(event, str) or not re.fullmatch(r"(?:SentryClips|SavedClips)/\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}", event):
                raise PreviewError("事件编号不可用", 404)
            selected_event = next((e for e in index.get("events", []) if e.get("id") == event), None)
            if not selected_event:
                raise PreviewError("该事件已不在录像索引中", 404)
        if not minute:
            minute = sorted((selected_event.get("clip_map") or {}).keys())[0] if selected_event and selected_event.get("clip_map") else (index.get("live") or {}).get("key")
        if not isinstance(minute, str) or not MINUTE_RE.fullmatch(minute):
            raise PreviewError("没有可用的录像分钟", 404)
        if selected_event:
            clips = (selected_event.get("clip_map") or {}).get(minute) or {}
            allowed_root = self.root / "TeslaCam" / event
        else:
            rows = list(index.get("minutes") or [])
            if index.get("live"):
                rows.append(index["live"])
            clips = next((row.get("clips", {}) for row in rows if row.get("key") == minute), {})
            allowed_root = self.root / "TeslaCam" / "RecentClips"
        selected = QUAD if view == "quad" else (view,)
        if not allowed_root.resolve().is_relative_to(self.root / "TeslaCam"):
            raise PreviewError("录像路径不可用", 404)
        sources = []
        for cam in selected:
            rel = clips.get(cam)
            if not rel:
                raise PreviewError("这分钟的四路片段尚未齐全" if view == "quad" else "这分钟尚无该镜头片段", 404)
            path = (self.root / rel).resolve()
            if not path.is_relative_to(allowed_root.resolve()):
                raise PreviewError("录像路径不可用", 404)
            if not path.is_file():
                raise PreviewError("该片段已不在本地缓存", 404)
            sources.append(str(path))
        return minute, tuple(sources)

    @staticmethod
    def signature(sources):
        return tuple((p, os.stat(p).st_size, os.stat(p).st_mtime_ns) for p in sources)

    def _make_job(self, view, minute=None, event=None, part=None, priority="foreground"):
        if part is not None and (type(part) is not int or not 0 <= part <= 7):
            raise PreviewError("录像分段编号不可用", 400)
        if priority not in ("foreground", "prefetch"):
            raise PreviewError("任务类型不可用", 400)
        try:
            minute, sources = self.sources(view, minute, event)
            signature = self.signature(sources)
        except OSError:
            raise PreviewError("片段正在更新，请稍后重试", 404)
        if part is None and event is None:
            digest = hashlib.sha256(repr(("h264-12fps-v4", view, signature)).encode()).hexdigest()[:16]
            name = f"qv4_{minute}_{view}_{digest}.mp4"
        else:
            if part is None:
                part = 0
            digest = hashlib.sha256(repr(("h264-12fps-v5-segment15", event, view, part, signature)).encode()).hexdigest()[:16]
            name = f"qv5_{minute}_{view}_p{part}_{digest}.mp4"
        return {"name": name, "view": view, "key": minute, "sources": sources,
                "event": event, "part": part, "offset": 0 if part is None else part * SEGMENT_SECONDS,
                "priority": priority, "signature": signature, "status": "queued", "updated": time.monotonic()}

    def _restore_cache(self, job):
        """Hit validated disk output before queue admission, including after restart."""
        path = self.live / job["name"]
        if not path.is_file():
            return False
        try:
            stat = path.stat()
            fingerprint = (job["name"], stat.st_size, stat.st_mtime_ns)
            if self.signature(job["sources"]) != job["signature"]:
                return False
            restored = self.validated.get(fingerprint)
            if restored:
                job.update(restored)
                return True
            duration, video = self._probe(path)
            expected = (1280, 720) if job["view"] == "quad" else (960, 540)
            if (video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p"
                    or (video.get("width"), video.get("height")) != expected or not self._faststart(path)
                    or job.get("part") is not None and duration > SEGMENT_SECONDS + .2):
                return False
            source_duration = min([self._probe(p)[0] for p in job["sources"]] + [120.0]) if job.get("part") is not None else duration
            if source_duration <= job.get("offset", 0):
                return False
            if self.signature(job["sources"]) != job["signature"]:
                return False
            restored = {"duration": duration, "source_duration": source_duration, "status": "ready", "updated": time.monotonic()}
            self.validated[fingerprint] = restored
            job.update(restored)
            return True
        except (OSError, PreviewError, subprocess.TimeoutExpired):
            return False

    def latest_cached(self, view):
        if view not in ("quad",) + CAMS:
            raise PreviewError("不支持的镜头", 400)
        try:
            index = json.loads((self.root / "index.json").read_text(encoding="utf-8-sig"))
            keys = sorted(set([r["key"] for r in index.get("minutes", [])] + ([index["live"]["key"]] if index.get("live") else [])), reverse=True)
            latest = datetime.datetime.strptime(keys[0], "%Y-%m-%d_%H-%M-%S") if keys else None
            for key in keys:
                captured = datetime.datetime.strptime(key, "%Y-%m-%d_%H-%M-%S")
                if (latest - captured).total_seconds() > 180:
                    break
                for part in (0, None):
                    try:
                        job = self._make_job(view, key, part=part)
                        with self.lock:
                            existing = self.jobs.get(job["name"])
                            if existing and existing["status"] == "ready" and (self.live / job["name"]).is_file():
                                return 200, self._ready(existing)
                            if self._restore_cache(job):
                                self.jobs[job["name"]] = job
                                return 200, self._ready(job)
                    except PreviewError:
                        continue
        except (OSError, ValueError, KeyError, TypeError):
            raise PreviewError("录像索引暂不可用", 503)
        raise PreviewError("近期暂无已准备好的压缩缓存", 404)

    def request(self, view, minute=None, event=None, part=None, priority="foreground", cached=False):
        if cached:
            if event is not None:
                raise PreviewError("缓存入口只用于最近录像", 400)
            return self.latest_cached(view)
        job = self._make_job(view, minute, event, part, priority)
        name = job["name"]
        now = time.monotonic()
        with self.lock:
            self.jobs = {k: v for k, v in self.jobs.items()
                         if v["status"] in ("queued", "processing") or now - v["updated"] < 3600}
            previous = self.jobs.get(name)
            if previous and previous["status"] == "ready" and (self.live / name).is_file():
                return 200, self._ready(previous)
            if previous and previous["status"] in ("queued", "processing"):
                if priority == "foreground":
                    previous["priority"] = priority
                return 202, {"status": previous["status"], "retry_after": 1}
            if previous and previous["status"] == "error" and now - previous["updated"] < 15:
                return 503, {"status": "error", "message": previous["message"], "retry_after": 5}
            if self._restore_cache(job):
                self.jobs[name] = job
                return 200, self._ready(job)
            if self.pending.full():
                with self.pending.mutex:
                    waiting = self.pending.queue[0] if self.pending.queue else None
                if priority == "foreground" and waiting and waiting.get("priority") == "prefetch":
                    try:
                        removed = self.pending.get_nowait()
                        removed.update(status="discarded", updated=now)
                        self.pending.task_done()
                    except queue.Empty:
                        pass
                if self.pending.full():
                    return 429, {"status": "busy", "retry_after": 1, "message": "正在生成其他预览，请稍候"}
            self.jobs[name] = job
            self.pending.put_nowait(job)
        return 202, {"status": "queued", "retry_after": 1}

    @staticmethod
    def _ready(job):
        part = job.get("part")
        offset = job.get("offset", 0)
        duration = job.get("duration", 0)
        next_part = part + 1 if part is not None and part < 7 and offset + duration + .15 < job.get("source_duration", offset + duration) else None
        return {"status": "ready", "url": "live-cache/" + job["name"],
                "key": job["key"], "view": job["view"],
                "part": part, "offset": offset, "next_part": next_part,
                "event": job.get("event"), "segment_seconds": SEGMENT_SECONDS,
                "width": 1280 if job["view"] == "quad" else 960,
                "height": 720 if job["view"] == "quad" else 540,
                "fps": 12, "duration": job.get("duration")}

    def _probe(self, path):
        result = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt,width,height,nb_frames:format=duration",
            "-of", "json", str(path)], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise PreviewError("预览文件尚未完成，请重试")
        try:
            data = json.loads(result.stdout)
            video = data["streams"][0]
            duration = float(data["format"]["duration"])
            frames = video.get("nb_frames", "N/A")
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("duration")
            if frames != "N/A" and int(frames) < 2:
                raise ValueError("frames")
        except (KeyError, ValueError, TypeError, IndexError):
            raise PreviewError("片段没有有效的连续录像")
        return duration, video

    @staticmethod
    def _faststart(path):
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            pos = 0
            while pos + 8 <= size:
                f.seek(pos)
                header = f.read(8)
                length, kind = int.from_bytes(header[:4], "big"), header[4:]
                if length == 1:
                    length = int.from_bytes(f.read(8), "big")
                if kind == b"moov":
                    return True
                if kind == b"mdat" or length < 8:
                    return False
                pos += length
        return False

    def _command(self, job, output, duration):
        nice = P + "/bin/nice" if os.path.isfile(P + "/bin/nice") else "/system/bin/nice"
        cmd = [nice, "-n", "15", FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-filter_complex_threads", "1", "-filter_threads", "1"]
        for path in job["sources"]:
            cmd += ["-threads", "1"]
            if job.get("offset", 0) > 0:
                # Seek each source before decoding, rather than decode a full minute then discard.
                cmd += ["-ss", f'{job["offset"]:.6f}']
            cmd += ["-i", path]
        if job["view"] == "quad":
            filters = []
            for i in range(4):
                filters.append(f"[{i}:v:0]fps=12,scale=640:360:force_original_aspect_ratio=decrease,"
                    f"pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS[v{i}]")
            filters.append("[v0][v1][v2][v3]xstack=inputs=4:layout=0_0|w0_0|0_h0|w0_h0:shortest=1[out]")
            cmd += ["-filter_complex", ";".join(filters), "-map", "[out]"]
            bitrate, maxrate = "1000k", "1200k"
        else:
            cmd += ["-map", "0:v:0", "-vf", "fps=12,scale=960:540:force_original_aspect_ratio=decrease,"
                    "pad=960:540:(ow-iw)/2:(oh-ih)/2,setsar=1,setpts=PTS-STARTPTS"]
            bitrate, maxrate = "600k", "750k"
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline", "-level:v", "3.1",
                "-threads", "2", "-b:v", bitrate, "-maxrate", maxrate, "-bufsize", "2000k",
                "-g", "24", "-pix_fmt", "yuv420p", "-an", "-sn", "-dn", "-map_metadata", "-1",
                "-movflags", "+faststart", "-shortest", "-t", f"{duration:.6f}", str(output)]
        return cmd

    def _encode(self, job):
        output = self.live / job["name"]
        expected = (1280, 720) if job["view"] == "quad" else (960, 540)
        if job.get("part") is not None and self._restore_cache(job):
            return job["duration"]
        if job.get("part") is None and output.is_file():
            try:
                duration, video = self._probe(output)
                if (video.get("codec_name") == "h264" and video.get("pix_fmt") == "yuv420p"
                        and (video.get("width"), video.get("height")) == expected
                        and self._faststart(output)):
                    return duration
            except PreviewError:
                pass  # Preserve the old file until a validated replacement is ready.
        temporary = self.live / (job["name"] + f".part-{os.getpid()}.mp4")
        Path(self.lockfile).parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.lockfile, "a") as lock:
                deadline = time.monotonic() + 90
                if fcntl:
                    while True:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() > deadline:
                                raise PreviewError("其他转码正忙，请稍后重试")
                            time.sleep(1)
                if self.signature(job["sources"]) != job["signature"]:
                    raise PreviewError("原片已更新，请重新选择该分钟")
                durations = [self._probe(p)[0] for p in job["sources"]]
                source_duration = min(min(durations), 120.0)
                offset = job.get("offset", 0)
                if source_duration - offset < 1 / 12:
                    raise PreviewError("该分钟已播放完", 404)
                duration = min(SEGMENT_SECONDS, source_duration - offset) if job.get("part") is not None else source_duration
                job["source_duration"] = source_duration
                with self.lock:
                    job["status"] = "processing"
                result = subprocess.run(self._command(job, temporary, duration),
                    capture_output=True, text=True, timeout=240,
                    env={**os.environ, "LD_LIBRARY_PATH": P + "/lib"})
                if result.returncode:
                    print(f"live-preview ffmpeg rc={result.returncode} detail={result.stderr[-1200:]!r}", flush=True)
                    raise PreviewError("压缩预览生成失败，请稍后重试")
                if self.signature(job["sources"]) != job["signature"]:
                    raise PreviewError("原片已更新，请重新选择该分钟")
                ready_duration, video = self._probe(temporary)
                if (video.get("codec_name") != "h264" or video.get("pix_fmt") != "yuv420p"
                        or (video.get("width"), video.get("height")) != expected
                        or not self._faststart(temporary)):
                    raise PreviewError("压缩预览校验失败，请重试")
                if self.signature(job["sources"]) != job["signature"]:
                    raise PreviewError("原片已更新，请重新选择该分钟")
                os.replace(temporary, output)
                return ready_duration
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _worker(self):
        while True:
            job = self.pending.get()
            start = time.monotonic()
            try:
                duration = self._encode(job)
                with self.lock:
                    job.update(status="ready", duration=duration, updated=time.monotonic())
                print(f'live-preview ready view={job["view"]} key={job["key"]} part={job.get("part")} event={job.get("event")} seconds={time.monotonic()-start:.2f}', flush=True)
                self._prune()
            except Exception as error:
                message = str(error) if isinstance(error, PreviewError) else "压缩预览暂不可用，请重试"
                with self.lock:
                    job.update(status="error", message=message, updated=time.monotonic())
                print(f'live-preview error view={job["view"]} key={job["key"]} type={type(error).__name__}', flush=True)
            finally:
                self.pending.task_done()

    def _prune(self):
        try:
            files = sorted([p for p in self.live.glob("qv*.mp4") if CACHE_RE.fullmatch(p.name)], key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[192:]:
                if time.time() - path.stat().st_mtime > 3600:
                    path.unlink()
        except OSError:
            pass  # Cache cleanup must not invalidate a successfully published clip.


MANAGER = None

LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>哨兵 · 登录</title><style>
:root{color-scheme:light dark}*{box-sizing:border-box}body{margin:0;background:#f5f5f7;color:#1d1d1f;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;min-height:100svh;
display:grid;place-items:center;padding:24px}form{width:min(100%,360px);padding:30px;background:#fff;
border-radius:24px;box-shadow:0 12px 48px #0000000b}h1{font-size:30px;margin:0 0 8px;letter-spacing:-1px}
p{color:#6e6e73;font-size:14px;margin:0 0 24px}input,button{width:100%;padding:14px 16px;font:inherit;
border-radius:12px;border:0}input{background:#f5f5f7;color:inherit;outline-offset:3px}
button{margin-top:14px;background:#007aff;color:#fff;font-weight:600}
@media(prefers-color-scheme:dark){body{background:#000;color:#f5f5f7}form{background:#1c1c1e}input{background:#2c2c2e}}
</style><form method="post" action=""><h1>哨兵</h1><p>查看车辆录像与最近片段</p>
<input type="password" name="k" autocomplete="current-password" placeholder="访问码" required>
<button type="submit">继续</button></form></html>'''


def safe_child(base, relative):
    root = Path(base).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        return None
    return path


def same_secret(supplied, expected):
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


TAILNET_NETWORKS = tuple(map(ipaddress.ip_network, ("100.64.0.0/10", "fd7a:115c:a1e0::/48")))
LAN_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")))


def connection_access(peer):
    """Describe the HTTP socket's entry only, never infer a Tailscale UDP path.

    The existing public FRP tunnel connects to this service through loopback.
    Ignore Host and forwarding headers: a client can supply those itself.
    """
    try:
        address = ipaddress.ip_address(peer.split("%", 1)[0])
    except (AttributeError, ValueError):
        return "unknown"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if address.is_loopback:
        return "forwarded"
    if any(address in network for network in TAILNET_NETWORKS):
        return "tailnet"
    if address.is_link_local or any(address in network for network in LAN_NETWORKS):
        return "lan"
    return "external"


class H(http.server.BaseHTTPRequestHandler):
    server_version = "TeslaCam4.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # Do not log query strings, cookies or access codes.

    def log_request(self, code="-", size="-"):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith(("/api/live", "/live-cache/")) or (path.startswith("/f/") and path.endswith(".mp4")):
            print(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": self.command,
                              "path": path, "status": code}, ensure_ascii=False), flush=True)

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    def ok(self):
        self.grant = False
        if not TOKEN:
            return True
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        supplied = (query.get("k") or [""])[0]
        if same_secret(supplied, TOKEN):
            self.grant = True
            return True
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            supplied = cookie["tc"].value if "tc" in cookie else ""
        except http.cookies.CookieError:
            return False
        return same_secret(supplied, TOKEN)

    def end_headers(self):
        if getattr(self, "grant", False):
            self.send_header("Set-Cookie", f"tc={TOKEN}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax")
        super().end_headers()

    def send(self, code, body=b"", ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code, data):
        extra = {"Retry-After": data["retry_after"]} if "retry_after" in data else None
        self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def serve_file(self, path, immutable=False):
        try:
            f = open(path, "rb")
        except (OSError, TypeError):
            return self.send(404, b"not found")
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end, code = 0, size - 1, 200
            hdr = (self.headers.get("Range") or "").strip()
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", hdr)
            if match and any(match.groups()):
                a, b = match.groups()
                if a:
                    start = int(a)
                    end = min(int(b), end) if b else end
                else:
                    suffix = int(b)
                    start = max(0, size - suffix)
                    if suffix == 0:
                        start = size
                if start >= size or start > end:
                    return self.send(416, b"", extra={"Content-Range": f"bytes */{size}"})
                code = 206
            length = max(0, end - start + 1)
            self.send_response(code)
            self.send_header("Content-Type", CT.get(Path(path).suffix.lower(), "application/octet-stream"))
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "private, max-age=31536000, immutable" if immutable else "no-store")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            f.seek(start)
            left = length
            while left > 0:
                block = f.read(min(131072, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return self.send(400, b"invalid length")
        if n < 0:
            self.close_connection = True
            return self.send(400, b"invalid length")
        if n > 4096:
            self.close_connection = True
            return self.send(413, b"request too large")
        body = self.rfile.read(n).decode("utf-8", "replace")
        supplied = (urllib.parse.parse_qs(body).get("k") or [""])[0]
        if TOKEN and same_secret(supplied, TOKEN):
            self.grant = True
            return self.send(302, extra={"Location": "./"})
        return self.send(200, LOGIN.encode(), "text/html; charset=utf-8")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if not self.ok():
            if path.startswith(("/api/", "/live-cache/")):
                return self.json(401, {"status": "unauthorized", "message": "请重新登录"})
            return self.send(200, LOGIN.encode(), "text/html; charset=utf-8")
        if path in ("/", "/index.html"):
            return self.serve_file(Path(ROOT) / "index.html")
        if path == "/index.json":
            return self.serve_file(Path(ROOT) / "index.json")
        if path == "/api/connection":
            return self.json(200, {"access": connection_access(self.client_address[0]),
                                   "peer_path": "unknown",
                                   "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if path == "/api/live" or path.startswith("/live/"):
            query = urllib.parse.parse_qs(parsed.query)
            view = path[6:] if path.startswith("/live/") else (query.get("view") or ["quad"])[0]
            minute = (query.get("minute") or [None])[0]
            try:
                part_text = (query.get("part") or [None])[0]
                if part_text is not None and not re.fullmatch(r"[0-7]", part_text):
                    raise PreviewError("录像分段编号不可用", 400)
                part = int(part_text) if part_text is not None else None
                event = (query.get("event") or [None])[0]
                priority = (query.get("priority") or ["foreground"])[0]
                cached = (query.get("cached") or ["0"])[0] == "1"
                # Preserve the original two-argument path for existing clients.
                if event is None and part is None and priority == "foreground" and not cached:
                    code, data = MANAGER.request(view, minute)
                else:
                    code, data = MANAGER.request(view, minute, event=event, part=part, priority=priority, cached=cached)
            except PreviewError as error:
                return self.json(error.status, {"status": "unavailable" if error.status == 404 else "error", "message": str(error)})
            # Compatibility for old pages; no original-byte fallback.
            if path.startswith("/live/") and code == 200:
                return self.serve_file(Path(LIVE) / data["url"].split("/")[-1], immutable=True)
            return self.json(code, data)
        if path.startswith("/live-cache/"):
            name = path[len("/live-cache/"):]
            if not CACHE_RE.fullmatch(name):
                return self.send(404, b"not found")
            full = safe_child(LIVE, name)
            return self.serve_file(full, immutable=True) if full else self.send(403, b"forbidden")
        if path.startswith("/f/"):
            full = safe_child(ROOT, path[3:])
            return self.serve_file(full) if full else self.send(403, b"forbidden")
        return self.send(404, b"not found")


class Srv(socketserver.ThreadingTCPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()


if __name__ == "__main__":
    if PORT == 8080:
        raise SystemExit("Port 8080 is reserved for tepla; TeslaCam must use 8081.")
    if not TOKEN:
        raise SystemExit("TOKEN is required; configure a private access code before starting.")
    os.makedirs(ROOT, exist_ok=True)
    MANAGER = LiveManager()
    print(f"v4.2 serving on :{PORT} token={'on' if TOKEN else 'off'}", flush=True)
    with Srv(("::", PORT), H) as server:
        server.serve_forever()
