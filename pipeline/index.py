#!/usr/bin/env python3
# index.py v2 - 扫描已采集的 TeslaCam 片段，生成 index.json + audit.json
#
# 判定原则：
#   1. event.json 是事件的权威标志；文件夹存在不代表事件成立
#   2. MP4 顶层结构完整、含 moov 且 ffprobe 确认可播放的视频才发布
#   3. 事件是否完整看"文件已静默 QUIET_MIN 分钟"+ 基本要素齐全
#   4. 不认识的文件/摄像头一律进 audit，绝不静默丢弃
import json
import math
import os
import re
import subprocess
import tempfile
import time

OUT = os.environ.get("OUT", "/data/teslacam/events")
QUIET_MIN = int(os.environ.get("QUIET_MIN", "5"))
CACHE_FILE = os.path.join(OUT, ".index-cache.json")
FFPROBE = os.environ.get("FFPROBE", "/data/data/com.termux/files/usr/bin/ffprobe")
PROBE_VERSION = "video-duration-frames-v1"

KNOWN_CAMS = ("front", "back", "left_repeater", "right_repeater",
              "left_pillar", "right_pillar")

CLIP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[_-](\d{2})-(\d{2})-(\d{2})-([A-Za-z_]+)\.mp4$")
EVENT_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[_-]\d{2}-\d{2}-\d{2}$")


def load_cache():
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def atomic_json(path, value):
    """A writer owns its temporary name, including concurrent index runs."""
    parent = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, separators=(",", ":"))
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows may briefly deny overlapping rename/delete handles.
                # Android/POSIX errors remain immediate; retries are bounded.
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(0.01 * (attempt + 1))
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def save_cache(c):
    try:
        atomic_json(CACHE_FILE, c)
    except Exception:
        pass


def probe_video(path):
    """只读取媒体元数据；失败或无有效视频时长时不发布文件。"""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_type,duration,nb_frames:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        return False, "probe_timeout"
    except OSError:
        return False, "probe_failed"
    if result.returncode != 0:
        return False, "probe_failed"
    try:
        data = json.loads(result.stdout)
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            return False, "no_video_stream"
        try:
            frames = int(video.get("nb_frames", "N/A"))
        except (TypeError, ValueError):
            frames = None
        if frames is not None and frames < 2:
            return False, "too_few_frames"
        durations = [video.get("duration"), (data.get("format") or {}).get("duration")]
        for value in durations:
            try:
                duration = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                return True, "video_ready"
        return False, "invalid_duration"
    except (ValueError, TypeError, AttributeError):
        return False, "probe_invalid"


def mp4_ready(path, cache):
    """完整 MP4 结构 + 视频探测，缓存按 size/mtime_ns/探测版本失效。"""
    try:
        st = os.stat(path)
    except OSError:
        return False, "stat_failed"
    c = cache.get(path)
    if (isinstance(c, dict) and c.get("probe_version") == PROBE_VERSION
            and c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns):
        return c.get("ready", False), c.get("why", "")

    ready, why = False, ""
    if st.st_size <= 0:
        ready, why = False, "empty"
    else:
        try:
            with open(path, "rb") as f:
                pos, has_moov = 0, False
                while pos + 8 <= st.st_size:
                    f.seek(pos)
                    hdr = f.read(8)
                    if len(hdr) < 8:
                        why = "truncated"
                        break
                    bsize = int.from_bytes(hdr[0:4], "big")
                    btype = hdr[4:8]
                    header_size = 8
                    if bsize == 1:
                        header_size = 16
                        ext = f.read(8)
                        if len(ext) < 8:
                            why = "truncated"
                            break
                        bsize = int.from_bytes(ext, "big")
                    elif bsize == 0:
                        bsize = st.st_size - pos
                    if bsize < header_size or pos + bsize > st.st_size:
                        why = "truncated"
                        break
                    if btype == b"moov":
                        has_moov = True
                    pos += bsize
                if not why:
                    if pos != st.st_size:
                        why = "truncated"
                    elif has_moov:
                        ready, why = probe_video(path)
                    else:
                        why = "no_moov"
        except OSError:
            ready, why = False, "read_error"

    try:
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            ready, why = False, "changed_during_probe"
    except OSError:
        ready, why = False, "stat_failed"
    # ffprobe 的临时执行故障下次重试，不能把暂时不可用永久写进缓存。
    if why in ("probe_timeout", "probe_failed", "changed_during_probe", "stat_failed"):
        cache.pop(path, None)
    else:
        cache[path] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                       "probe_version": PROBE_VERSION, "ready": ready, "why": why}
    return ready, why


def rel(p):
    return os.path.relpath(p, OUT).replace(os.sep, "/")


def parse_clip(name):
    m = CLIP_RE.match(name)
    if not m:
        return None
    d, hh, mm, ss, cam = m.groups()
    return {"key": "%s_%s-%s-%s" % (d, hh, mm, ss), "cam": cam}


def epoch_of(key):
    try:
        return int(time.mktime(time.strptime(key, "%Y-%m-%d_%H-%M-%S")))
    except Exception:
        return 0


def scan_recent(audit, cache):
    d = os.path.join(OUT, "TeslaCam", "RecentClips")
    minutes = {}
    if not os.path.isdir(d):
        return minutes
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        info = parse_clip(name)
        if not info:
            audit["unknown_files"].append(rel(p))
            continue
        if info["cam"] not in KNOWN_CAMS:
            audit["unknown_cameras"].append(info["cam"])
        ready, why = mp4_ready(p, cache)
        if not ready:
            audit["unfinished"].append("%s (%s)" % (rel(p), why))
            continue
        minutes.setdefault(info["key"], {})[info["cam"]] = rel(p)
    return minutes


def scan_one_event(p, kind, cache, audit, now):
    name = os.path.basename(p)
    by_minute = {}
    unknown_local = []
    newest = 0
    source_errors = {}

    for f in sorted(os.listdir(p)):
        fp = os.path.join(p, f)
        if not os.path.isfile(fp):
            continue
        try:
            newest = max(newest, os.stat(fp).st_mtime)
        except OSError:
            pass
        if f.endswith(".mp4"):
            info = parse_clip(f)
            if not info:
                unknown_local.append(f)
                continue
            if info["cam"] not in KNOWN_CAMS:
                audit["unknown_cameras"].append(info["cam"])
            ready, why = mp4_ready(fp, cache)
            if not ready:
                audit["unfinished"].append("%s (%s)" % (rel(fp), why))
                source_errors[rel(fp)] = why
                continue
            by_minute.setdefault(info["key"], {})[info["cam"]] = rel(fp)
        elif f in ("event.json", "thumb.png"):
            continue
        else:
            unknown_local.append(f)

    meta = {}
    ej = os.path.join(p, "event.json")
    has_json = os.path.isfile(ej)
    if has_json:
        try:
            with open(ej, encoding="utf-8-sig", errors="ignore") as fh:
                meta = json.load(fh)
        except Exception:
            meta = {}
            audit["bad_event_json"].append(rel(ej))
    else:
        audit["event_without_json"].append(rel(p))

    thumb = os.path.join(p, "thumb.png")
    has_thumb = os.path.isfile(thumb)

    all_cams = sorted({c for m in by_minute.values() for c in m})
    keys = sorted(by_minute.keys())
    quiet_sec = int(now - newest) if newest else 999999

    reasons = []
    if not has_json:
        reasons.append("no_event_json")
    if not has_thumb:
        reasons.append("no_thumb")
    if not all_cams:
        reasons.append("no_clips")
    if quiet_sec < QUIET_MIN * 60:
        reasons.append("still_writing")

    if "still_writing" in reasons:
        status = "growing"
    elif reasons:
        status = "partial"
    else:
        status = "complete"

    first = {}
    for k in keys:
        for cam, path in by_minute[k].items():
            first.setdefault(cam, path)

    if unknown_local:
        audit["unknown_files"].extend(rel(os.path.join(p, u)) for u in unknown_local)

    # 转码产物（连续长文件）
    pdir = os.path.join(OUT, "_proxy", kind, name)
    mosaic = os.path.join(pdir, "mosaic.mp4")
    mmap = []
    mj = os.path.join(pdir, "mosaic.json")
    if os.path.isfile(mj):
        try:
            with open(mj, encoding="utf-8") as fh:
                mmap = json.load(fh).get("minutes", [])
        except Exception:
            mmap = []
    proxies, proxy_errors = {}, {}
    if os.path.isdir(pdir):
        for c in KNOWN_CAMS:
            f = os.path.join(pdir, "cam_%s.mp4" % c)
            if os.path.isfile(f):
                ready, why = mp4_ready(f, cache)
                if ready:
                    proxies[c] = rel(f)
                else:
                    proxy_errors[c] = why
                    audit["unfinished"].append("%s (%s)" % (rel(f), why))
            elif c in all_cams:
                proxy_errors[c] = "missing"
    else:
        proxy_errors.update({c: "missing" for c in all_cams})
    valid_mosaic = None
    if os.path.isfile(mosaic):
        ready, why = mp4_ready(mosaic, cache)
        if ready:
            valid_mosaic = rel(mosaic)
        else:
            proxy_errors["mosaic"] = why
            audit["unfinished"].append("%s (%s)" % (rel(mosaic), why))

    return {
        "mosaic": valid_mosaic,
        "mosaic_map": mmap if valid_mosaic else [],
        "proxies": proxies,
        "source_errors": source_errors,
        "proxy_errors": proxy_errors,
        "id": "%s/%s" % (kind, name),
        "name": name,
        "kind": kind,
        "status": status,
        "status_reasons": reasons,
        "time": meta.get("timestamp") or name,
        "reason": meta.get("reason") or "",
        "city": meta.get("city") or "",
        "thumb": rel(thumb) if has_thumb else None,
        "cams": all_cams,
        "minutes": len(keys),
        "quiet_sec": quiet_sec,
        "clips": first,                 # 兼容旧 UI：每路第一个分钟
        "clip_map": by_minute,          # 完整：分钟 -> {cam: 路径}
    }


def scan_events(audit, cache):
    now = time.time()
    events = []
    by_name = {}

    for kind in ("SentryClips", "SavedClips"):
        base = os.path.join(OUT, "TeslaCam", kind)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                audit["unknown_files"].append(rel(p))
                continue
            if not EVENT_DIR_RE.match(name):
                audit["unknown_files"].append(rel(p) + "/")
                continue

            ev = scan_one_event(p, kind, cache, audit, now)

            if name in by_name:
                prev = by_name[name]
                if kind == "SavedClips" and prev["kind"] != "SavedClips":
                    events.remove(prev)
                    events.append(ev)
                    by_name[name] = ev
                    audit["deduped"].append("%s: 同时存在于两处，采用 SavedClips" % name)
                else:
                    audit["deduped"].append("%s: 同时存在于两处，忽略 %s 版本" % (name, kind))
                continue
            by_name[name] = ev
            events.append(ev)

    events.sort(key=lambda e: e["name"], reverse=True)
    return events


def main():
    cache = load_cache()
    audit = {
        "checked_at": 0,
        "unknown_files": [],
        "unknown_cameras": [],
        "unfinished": [],
        "event_without_json": [],
        "bad_event_json": [],
        "deduped": [],
    }

    minutes = scan_recent(audit, cache)
    keys = sorted(minutes.keys())
    live = None
    if keys:
        k = keys[-1]
        live = {"key": k, "epoch": epoch_of(k), "clips": minutes[k]}

    events = scan_events(audit, cache)

    counts = {"complete": 0, "partial": 0, "growing": 0}
    for e in events:
        counts[e["status"]] = counts.get(e["status"], 0) + 1

    data = {
        "now": int(time.time()),
        "live": live,
        "minutes": [{"key": k, "clips": minutes[k]} for k in keys[-12:]][::-1],
        "events": events,
        "stats": {"events": len(events), "by_status": counts, "recent_minutes": len(keys)},
    }

    atomic_json(os.path.join(OUT, "index.json"), data)

    audit["unknown_cameras"] = sorted(set(audit["unknown_cameras"]))
    for k in ("unfinished", "unknown_files"):
        audit[k] = audit[k][:50]
    audit["checked_at"] = int(time.time())
    atomic_json(os.path.join(OUT, "audit.json"), audit)

    save_cache(cache)


if __name__ == "__main__":
    main()
