#!/usr/bin/env python3
"""Real reassert boot guards with private memory state and optional native flock.

The lifecycle/teslacam functions are loaded unchanged from --src.  Only their
filesystem/process/USB collaborators are substituted.  The optional POSIX case
uses a finite child and one lock file in TemporaryDirectory, never production.
"""

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import types
import unittest

HERE = Path(__file__).resolve().parent
SUPPORT = HERE.parent / "test_recovery_boundaries.py"
DEFAULT_SOURCE = (HERE.parent / "src" if (HERE.parent / "src").is_dir()
                  else HERE.parent / "candidate" / "src")
SOURCE = DEFAULT_SOURCE
BOOT_PATH = "/proc/sys/kernel/random/boot_id"

spec = importlib.util.spec_from_file_location("reassert_boot_memory_support", SUPPORT)
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
support.SOURCE = SOURCE  # Discovery/import must use this package too, not the baseline.


class BootHarness(support.MemoryHarness):
    def __init__(self):
        self.boot_error = None
        self.boot_read_sizes = []
        self.opened = []
        super().__init__()
        self.files[BOOT_PATH] = (support.NEW_BOOT + "\n").encode("ascii")

    def open(self, path, mode="r", **kwargs):
        self.opened.append(path)
        if path != BOOT_PATH:
            return super().open(path, mode, **kwargs)
        if self.boot_error is not None:
            raise self.boot_error
        if path not in self.files:
            raise FileNotFoundError(path)
        raw = self.files[path].decode(kwargs.get("encoding", "utf-8"))
        owner = self

        class ObservedStream(io.StringIO):
            def read(self, size=-1):
                owner.boot_read_sizes.append(size)
                return super().read(size)

        return ObservedStream(raw)

    def finished(self, boot=support.NEW_BOOT, **changes):
        rec = self.stale("finished")
        rec["owner"]["boot_id"] = boot
        rec.update(changes)
        assert self.lifecycle.validate(rec)
        self.files[self.active] = json.dumps(rec).encode("utf-8")
        return rec


class ReassertBoot(unittest.TestCase):
    def harness(self):
        h = BootHarness()
        h.finished()
        h.files[h.path("export_dirty")] = b"1"
        return h

    def assert_refused(self, h, reason):
        before = copy.deepcopy(h.files)
        code, out = h.call("reassert")
        self.assertEqual(code, h.tc.EXIT_SKIP)
        self.assertFalse(out["ok"])
        self.assertTrue(out["skipped"])
        self.assertEqual(out["reason"], reason)
        self.assertEqual(h.events, [])
        self.assertEqual(h.checker_calls, 0)
        self.assertEqual(h.files, before)
        self.assertEqual(h.held, set())
        return out

    def test_same_boot_finished_runs_msd_once_without_rewriting_active(self):
        h = self.harness()
        active = h.files[h.active]
        code, out = h.call("reassert")
        self.assertEqual(code, h.tc.EXIT_OK)
        self.assertTrue(out["ok"])
        self.assertEqual(h.events, [("simulated_msd", "up")])
        self.assertEqual(h.checker_calls, 0)
        self.assertEqual(h.files[h.active], active)
        self.assertEqual(h.files[h.lun], h.c["img"].encode())
        self.assertEqual(h.boot_read_sizes, [128])
        self.assertEqual(h.held, set())

    def test_old_boot_refuses_without_writing_active(self):
        h = self.harness()
        h.finished(boot=support.OLD_BOOT)
        self.assert_refused(h, "boot_changed")

    def test_missing_record_refuses_before_boot_observation(self):
        h = self.harness()
        del h.files[h.active]
        h.boot_error = AssertionError("boot must not be consulted")
        self.assert_refused(h, "active_op_missing")
        self.assertEqual(h.boot_read_sizes, [])

    def test_unknown_record_refuses_before_boot_observation(self):
        h = self.harness()
        h.files[h.active] = b"{not valid json"
        h.boot_error = AssertionError("boot must not be consulted")
        self.assert_refused(h, "active_op_unknown")

    def test_running_record_refuses(self):
        h = self.harness()
        h.stale("running")
        self.assert_refused(h, "active_op_incomplete")

    def test_incomplete_record_refuses(self):
        h = self.harness()
        h.stale("incomplete")
        self.assert_refused(h, "active_op_incomplete")

    def test_recovery_required_refuses_before_boot_observation(self):
        h = self.harness()
        h.finished(recovery_required=True)
        h.boot_error = AssertionError("boot must not be consulted")
        self.assert_refused(h, "recovery_required")

    def test_boot_unreadable_is_unknown(self):
        h = self.harness()
        h.boot_error = PermissionError("private injected read denial")
        self.assert_refused(h, "boot_unknown")

    def test_boot_missing_is_unknown(self):
        h = self.harness()
        del h.files[BOOT_PATH]
        self.assert_refused(h, "boot_unknown")

    def test_boot_invalid_is_unknown(self):
        h = self.harness()
        h.files[BOOT_PATH] = b"not-a-uuid\n"
        self.assert_refused(h, "boot_unknown")

    def test_boot_empty_is_unknown(self):
        h = self.harness()
        h.files[BOOT_PATH] = b""
        self.assert_refused(h, "boot_unknown")

    def test_boot_oversize_is_unknown_and_read_is_bounded(self):
        h = self.harness()
        h.files[BOOT_PATH] = support.NEW_BOOT.encode() + b" " * 300
        self.assert_refused(h, "boot_unknown")
        self.assertEqual(h.boot_read_sizes, [128])

    def test_boot_non_ascii_is_unknown(self):
        h = self.harness()
        h.files[BOOT_PATH] = b"\xff\n"
        self.assert_refused(h, "boot_unknown")

    def test_lifecycle_busy_precedes_record_and_boot_checks(self):
        for record in ("missing", "old_boot", "running"):
            with self.subTest(record=record):
                h = self.harness()
                if record == "missing":
                    del h.files[h.active]
                elif record == "old_boot":
                    h.finished(boot=support.OLD_BOOT)
                else:
                    h.stale("running")
                before = copy.deepcopy(h.files)
                lock = "/private/run/lifecycle.lock"
                h.held.add(lock)
                h.boot_error = AssertionError("busy must win")
                h.opened.clear()
                code, out = h.call("reassert")
                self.assertEqual(code, h.tc.EXIT_SKIP)
                self.assertEqual(out["reason"], "lifecycle_busy")
                self.assertEqual(h.opened, [])
                self.assertEqual(h.events, [])
                self.assertEqual(h.files, before)
                self.assertEqual(h.held, {lock})

    def test_guard_exit_releases_on_body_exception(self):
        h = self.harness()
        before = copy.deepcopy(h.files)
        guard = h.lifecycle.ReassertGuard(h.c)
        with self.assertRaisesRegex(RuntimeError, "private body failure"):
            with guard:
                self.assertEqual(h.held, {"/private/run/lifecycle.lock"})
                self.assertEqual(guard.fileno(), 101)
                raise RuntimeError("private body failure")
        self.assertEqual(h.held, set())
        self.assertIsNone(guard._holder)
        self.assertEqual(h.files, before)

    def test_guard_rejection_releases_descriptor(self):
        h = self.harness()
        h.finished(boot=support.OLD_BOOT)
        guard = h.lifecycle.ReassertGuard(h.c)
        with self.assertRaises(h.lifecycle.Refused) as raised:
            guard.__enter__()
        self.assertEqual(raised.exception.reason, "boot_changed")
        self.assertIsNone(guard._holder)
        self.assertEqual(h.held, set())

    def test_existing_stop_hold_and_lun_checks_still_refuse(self):
        cases = (("stop_requested", "stopping"),
                 ("export_forbidden.json", "hold"),
                 ("op_inprogress.json", "op_in_progress"))
        for marker, reason in cases:
            with self.subTest(marker=marker):
                h = self.harness()
                h.files[h.path(marker)] = b"1"
                self.assert_refused(h, reason)
        h = self.harness()
        del h.files[h.path("export_dirty")]
        self.assert_refused(h, "not_exported")

    def test_real_owner_uses_validated_boot_reader(self):
        h = self.harness()
        # Reload actual definitions because the common harness stubs _owner for
        # unrelated Operation tests.  The real _owner and _boot_id execute here.
        lc = support.load_real("lifecycle", dict(h.lifecycle.__dict__))
        lc.pi = types.SimpleNamespace(_stat=lambda pid: {"starttime": 17})
        result = lc._owner()
        self.assertEqual(result["boot_id"], support.NEW_BOOT)
        self.assertEqual(result["starttime"], 17)
        h.files[BOOT_PATH] = b"invalid"
        with self.assertRaises(ValueError):
            lc._owner()

    @unittest.skipUnless(os.name == "posix", "native flock requires POSIX/Android")
    def test_native_finite_helper_retains_lock_after_parent_close(self):
        import fcntl
        h = self.harness()
        # Actual MountLock definitions, one private tempfile, one bounded child.
        native = support.load_real("locks", {"fcntl": fcntl, "os": os, "time": time})
        h.lifecycle.locks = native
        with tempfile.TemporaryDirectory(prefix="reassert-boot-native-") as tmp:
            h.c["run"] = tmp
            path = os.path.join(tmp, "lifecycle.lock")
            holder = native.MountLock(path, timeout=0, job_mode=True)
            holder.__enter__()
            read_end, write_end = os.pipe()
            child = None
            try:
                code = ("import os,select,sys; fd=int(sys.argv[1]); "
                        "r=int(sys.argv[2]); print('ready',flush=True); "
                        "select.select([r],[],[],8); os.close(fd); os.close(r)")
                child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code,
                                          str(holder.fileno()), str(read_end)],
                                         pass_fds=(holder.fileno(), read_end),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True)
                os.close(read_end)
                read_end = None
                readable, _, _ = select.select([child.stdout], [], [], 5)
                self.assertTrue(readable, "private helper never became ready")
                self.assertEqual(child.stdout.readline().strip(), "ready")
                holder.__exit__(None, None, None)
                del h.files[h.active]
                before = copy.deepcopy(h.files)
                rc, out = h.call("reassert")
                self.assertEqual(rc, h.tc.EXIT_SKIP)
                self.assertEqual(out["reason"], "lifecycle_busy")
                self.assertEqual(h.files, before)
                self.assertEqual(h.events, [])
                os.close(write_end)
                write_end = None
                self.assertEqual(child.wait(timeout=10), 0)
                self.assertEqual(native.try_lock(path)[0], native.FREE)
                rc, out = h.call("reassert")
                self.assertEqual(out["reason"], "active_op_missing")
            finally:
                holder.__exit__(None, None, None)
                for fd in (read_end, write_end):
                    if fd is not None:
                        os.close(fd)
                if child is not None:
                    # Child has an internal eight-second bound and pipe close
                    # wakes it; never signal an unverified numeric PID.
                    child.wait(timeout=10)
                    child.stdout.close()
                    child.stderr.close()


def main():
    global SOURCE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--json", type=Path, required=True)
    args = parser.parse_args()
    SOURCE = args.src.resolve()
    support.SOURCE = SOURCE
    hashes = {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest()
              for name in ("lifecycle.py", "teslacam.py", "gate.py", "locks.py")}
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReassertBoot)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    evidence = {"scope": "unchanged candidate reassert/lifecycle definitions; private collaborators",
                "source": str(SOURCE), "sha256": hashes,
                "support_sha256": hashlib.sha256(SUPPORT.read_bytes()).hexdigest(),
                "tests_run": result.testsRun, "failures": len(result.failures),
                "errors": len(result.errors), "skipped": len(result.skipped),
                "native_flock_executed": os.name == "posix",
                "not_verified": ["device reboot/power loss", "actual USB reconnection",
                                 "production image or business services"]}
    with args.json.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
    print(json.dumps(evidence, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 2


if __name__ == "__main__":
    sys.exit(main())
