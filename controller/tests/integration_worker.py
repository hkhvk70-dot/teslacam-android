"""Owned disposable process used solely by concurrency tests."""
import contextlib
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import offline_support
import lifecycle
import teslacam as tc


def main():
    mode, directory = sys.argv[1:]
    base = Path(directory).resolve()
    if not base.is_relative_to(offline_support.SCRATCH.resolve()):
        raise RuntimeError("fixture outside dedicated test root")
    c = dict(tc.DEFAULTS, state=str(base / "state"), run=str(base / "run"),
             cfg=str(base / "cfg"), img=str(base / "img"), proc_root=str(base / "proc"),
             services="pipeline", services_conf=str(base / "no-config"),
             mount_lock=str(base / "mount.lock"))
    lun = Path(c["cfg"]) / tc.gate.LUN_REL

    def msd(config, action):
        if action == "up":
            (base / "export-called").write_text("1")
        lun.write_text(config["img"] if action == "up" else "")
        return True, ""

    def wait_empty(pgid):
        if mode == "hold-stop":
            with tc.gate.control_lock(str(Path(c["run"]) / "control.lock"), timeout=0):
                (base / "control-available").write_text("1")
            (base / "stop-waiting").write_text("1")
            deadline = time.monotonic() + 15
            while not (base / "release-stop").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("test stop release timed out")
                time.sleep(0.02)
        return True

    owner = {"pid": os.getpid(), "starttime": 99,
             "boot_id": "22222222-2222-4222-8222-222222222222"}
    free = (tc.locks.FREE, {"loops": [], "mounts": [], "reason": ""})
    with patch.object(lifecycle, "_owner", return_value=owner), \
            patch.object(tc.sw, "_fsync_dir", return_value=None), \
            patch.object(tc, "run_msd", side_effect=msd), \
            patch.object(tc, "ensure_clean", return_value=(True, {}, False)), \
            patch.object(tc, "load_group", return_value={"pgid": 424242, "instance": "fixture"}), \
            patch.object(tc.pi, "verify", return_value={"reason": "ok", "members": [424242]}), \
            patch.object(tc.os, "killpg", create=True), \
            patch.object(tc, "wait_empty", side_effect=wait_empty), \
            patch.object(tc.locks, "probe_all", return_value=free):
        command = "stop" if mode in ("hold-stop", "stop") else mode
        return tc.dispatch(command, c, "")


if __name__ == "__main__":
    raise SystemExit(main())
