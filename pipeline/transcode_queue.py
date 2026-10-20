"""Optional background event previews; foreground 15-second previews need no queue.

The guarded transcode.sh owns the shared FFmpeg lock. This worker only locks
its own singleton file, avoiding recursive acquisition of the encode lock.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

EVENT_ID = re.compile(r"^(SentryClips|SavedClips)/(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})$")


class Queue:
    def __init__(self, root, bin_dir, quiet_seconds=180, retry_seconds=300,
                 runner=subprocess.run, clock=time.time, encode_lock=None):
        self.root, self.bin_dir = Path(root).resolve(), Path(bin_dir).resolve()
        self.quiet_seconds, self.retry_seconds = quiet_seconds, retry_seconds
        self.runner, self.clock = runner, clock
        self.encode_lock = encode_lock or os.environ.get("TC_ENCODE_LOCK") or os.environ.get("ENCODE_LOCK", "/data/local/tmp/teslacam-encode.lock")
        self.retries, self.finished = {}, {}

    def eligible(self, index):
        now = self.clock()
        ids = {e.get("id") for e in index.get("events") or [] if isinstance(e.get("id"), str)}
        self.retries = {k: v for k, v in self.retries.items() if k in ids}
        self.finished = {k: v for k, v in self.finished.items() if k in ids}
        for event in sorted(index.get("events") or [], key=lambda e: str(e.get("id", "")).split("/", 1)[-1], reverse=True):
            identifier = event.get("id", "")
            match = EVENT_ID.fullmatch(identifier) if isinstance(identifier, str) else None
            if not match or event.get("status") not in ("complete", "partial"):
                continue
            event_root = (self.root / "TeslaCam" / identifier).resolve()
            if not event_root.is_relative_to(self.root / "TeslaCam"):
                continue
            try:
                relative_paths = sorted({p for clips in event.get("clip_map", {}).values() for p in clips.values()})
                if not relative_paths:
                    continue
                sources = []
                for relative in relative_paths:
                    source = (self.root / relative).resolve()
                    if not source.is_relative_to(event_root) or source.suffix != ".mp4":
                        raise ValueError("source outside event")
                    stat = source.stat()
                    if stat.st_size <= 0:
                        raise ValueError("empty source")
                    sources.append((relative, stat.st_size, stat.st_mtime_ns))
                newest_ns = max(row[2] for row in sources)
                if now - newest_ns / 1e9 < self.quiet_seconds:
                    continue
                signature = tuple(sources)
                if self.finished.get(identifier) == signature:
                    continue
                retry = self.retries.get(identifier)
                if retry and retry[0] == signature and now < retry[1]:
                    continue
                # Only trust a proxy that the indexer already validated and
                # whose output is newer than the current source version.
                mosaic = event.get("mosaic")
                if mosaic:
                    proxy = (self.root / mosaic).resolve()
                    proxy_root = (self.root / "_proxy" / identifier).resolve()
                    if proxy_root.is_relative_to(self.root / "_proxy") and proxy.is_relative_to(proxy_root):
                        try:
                            if proxy.stat().st_mtime_ns >= newest_ns:
                                continue
                        except OSError:
                            pass
                yield identifier, signature, match.groups()
            except (OSError, TypeError, ValueError, AttributeError):
                continue

    def step(self, index):
        # Admission guard only; shutdown of an existing job is coordinated by
        # the controller's service group, not by this marker check alone.
        state = self.root.parent / "state"
        if any((state / name).exists() for name in ("stop_requested", "export_forbidden.json")):
            return {"status": "blocked"}
        candidate = next(self.eligible(index), None)
        if candidate is None:
            return {"status": "idle"}
        identifier, signature, (kind, name) = candidate
        environment = dict(os.environ, EV=str(self.root), OUT=str(self.root), TC_ENCODE_LOCK=self.encode_lock)
        try:
            result = self.runner(["sh", str(self.bin_dir / "transcode.sh"), kind, name], env=environment, timeout=5400, check=False)
            success = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            success = False
        if not success:
            self.retries[identifier] = (signature, self.clock() + self.retry_seconds)
            return {"status": "retry_later", "event": identifier}
        self.finished[identifier] = signature
        self.retries.pop(identifier, None)
        try:
            result = self.runner([sys.executable, str(self.bin_dir / "index.py")], env=environment, timeout=120, check=False)
            indexed = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            indexed = False
        return {"status": "encoded", "event": identifier, "index_refreshed": indexed}


def main():
    import fcntl  # Android/POSIX runtime; offline tests never enter main().
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="process at most one event")
    args = parser.parse_args()
    data = Path(os.environ.get("TC_DATA", "/data/teslacam"))
    if not data.is_dir():
        raise SystemExit("Project directory missing")
    # This lock is deliberately different from teslacam-encode.lock.
    with open(data / ".event-queue.lock", "a") as singleton:
        try:
            fcntl.flock(singleton.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        quiet = float(os.environ.get("QUIET_MIN", "3")) * 60
        if not math.isfinite(quiet) or quiet < 0:
            raise SystemExit("Invalid QUIET_MIN")
        worker = Queue(data / "events", data / "bin", quiet_seconds=quiet)
        while True:
            try:
                with open(data / "events" / "index.json", encoding="utf-8-sig") as stream:
                    index = json.load(stream)
                result = worker.step(index)
            except (OSError, ValueError, TypeError, AttributeError):
                result = {"status": "index_unavailable"}
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return
            time.sleep(5 if result["status"] == "encoded" else 30)


if __name__ == "__main__":
    main()
