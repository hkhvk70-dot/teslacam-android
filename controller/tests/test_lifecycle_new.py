"""Isolated lifecycle logic tests; never touch a phone or business image."""

import contextlib
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import offline_support
import lifecycle as lc


SCRATCH = getattr(offline_support, "SCRATCH", offline_support.ROOT / "_test_temp")
SCRATCH.mkdir(exist_ok=True)
OWNER = {"pid": 1234, "starttime": 9876,
         "boot_id": "22222222-2222-4222-8222-222222222222"}


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.c = {"state": str(root / "state"), "run": str(root / "run")}
        Path(self.c["state"]).mkdir()
        self.events = []
        self.held = False
        test = self

        class FakeLock:
            def __init__(self, path, timeout=0, job_mode=False):
                self.path = path
                self.job_mode = job_mode
                self.entered = False
                test.events.append(("make", path, timeout, job_mode))

            def __enter__(self):
                if test.held:
                    raise TimeoutError("busy fixture")
                test.held = True
                self.entered = True
                test.events.append(("lock",))
                return self

            def fileno(self):
                return 77

            def __exit__(self, *args):
                if self.entered:
                    self.entered = False
                    test.held = False
                    test.events.append(("close_only", self.job_mode))

        for target, value in (("_owner", lambda: dict(OWNER)), ("_boot_id", lambda: OWNER["boot_id"])):
            p = patch.object(lc, target, value)
            p.start()
            self.addCleanup(p.stop)
        patches = [
            patch.object(lc.locks, "MountLock", FakeLock),
            patch.object(lc.gate, "control_lock", side_effect=lambda *a, **k: contextlib.nullcontext()),
            patch.object(lc.sw, "_fsync_dir", return_value=None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def stored(self):
        return json.loads((Path(self.c["state"]) / "active_op.json").read_text(encoding="utf-8"))

    def put(self, value):
        (Path(self.c["state"]) / "active_op.json").write_text(
            json.dumps(value), encoding="utf-8")

    def completed(self):
        with lc.Operation(self.c, "stop") as op:
            op.finish(0, True)
        return self.stored()

    def test_missing_and_valid_records(self):
        self.assertEqual(lc.read_active(self.c), ("missing", None))
        rec = self.completed()
        self.assertEqual(lc.read_active(self.c), ("valid", rec))
        self.assertTrue(lc.validate(rec))

    def test_bad_schema_types_status_and_identity_are_unknown(self):
        valid = self.completed()
        variants = [None, [], {"schema": 1}]
        for key, value in (("schema", True), ("id", "pid-123"), ("op", []),
                           ("op", "reassert"), ("status", []), ("status", "stopped"),
                           ("phase", ""), ("exit_code", True), ("updated_at", float("nan")),
                           ("owner", {"pid": 1})):
            rec = copy.deepcopy(valid)
            rec[key] = value
            variants.append(rec)
        for value in variants:
            with self.subTest(value=value):
                self.put(value)
                self.assertEqual(lc.read_active(self.c), ("unknown", None))

    def test_bad_json_and_unreadable_are_unknown(self):
        path = Path(self.c["state"]) / "active_op.json"
        path.write_text("{not-json", encoding="utf-8")
        self.assertEqual(lc.read_active(self.c), ("unknown", None))
        with patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(lc.read_active(self.c), ("unknown", None))

    def test_success_has_identity_phase_and_retained_completed_record(self):
        with lc.Operation(self.c, "start") as op:
            self.assertEqual(op.fileno(), 77)
            self.assertEqual(self.stored()["status"], "running")
            self.assertEqual(op.record["owner"], OWNER)
            op.phase("starting_services")
            self.assertEqual(self.stored()["phase"], "starting_services")
            op.finish(0, True, {"lun": "exported"})
        rec = self.stored()
        self.assertEqual(rec["status"], "finished")
        self.assertEqual(rec["summary"], {"lun": "exported"})
        self.assertIn(("close_only", True), self.events)
        self.assertFalse(self.held)

    def test_nonzero_or_false_outcome_stays_incomplete(self):
        for code, ok in ((3, False), (0, False), (1, True)):
            with self.subTest(code=code, ok=ok):
                # Explicit stop is the recovery entry for a previous failure.
                with lc.Operation(self.c, "stop") as op:
                    op.finish(code, ok)
                self.assertEqual(self.stored()["status"], "incomplete")

    def test_exit_without_finish_marks_incomplete(self):
        with lc.Operation(self.c, "start"):
            pass
        rec = self.stored()
        self.assertEqual(rec["status"], "incomplete")
        self.assertEqual(rec["summary"]["reason"], "not_finished")

    def test_exception_marks_incomplete_and_preserves_exception(self):
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with lc.Operation(self.c, "start"):
                raise ValueError("fixture failure")
        self.assertEqual(self.stored()["status"], "incomplete")
        self.assertEqual(self.stored()["summary"]["reason"], "exception")
        self.assertFalse(self.held)

    def test_unfinished_prevents_all_nonstop_mutations(self):
        with lc.Operation(self.c, "start"):
            pass
        before = self.stored()
        for action in ("start", "maintain", "clear-hold", "clean-orphans"):
            with self.subTest(action=action), self.assertRaises(lc.Refused) as failure:
                with lc.Operation(self.c, action):
                    self.fail("refused operation entered")
            self.assertEqual(failure.exception.reason, "active_op_incomplete")
            self.assertEqual(self.stored(), before)
            self.assertFalse(self.held)

    def test_stop_recovers_unfinished_with_previous_summary(self):
        with lc.Operation(self.c, "start"):
            pass
        old = self.stored()
        with lc.Operation(self.c, "stop") as op:
            self.assertEqual(op.record["previous"]["id"], old["id"])
            self.assertNotEqual(op.id, old["id"])
            op.finish(0, True)
        with lc.Operation(self.c, "start") as op:
            self.assertEqual(op.record["previous"]["op"], "stop")
            op.finish(0, True)

    def test_malformed_blocks_start_but_stop_retains_unknown_summary(self):
        self.put({"bad": "record"})
        with self.assertRaises(lc.Refused) as failure:
            with lc.Operation(self.c, "start"):
                self.fail("entered")
        self.assertEqual(failure.exception.reason, "active_op_unknown")
        with lc.Operation(self.c, "stop") as op:
            self.assertEqual(op.record["previous"], {"state": "unknown"})
            op.finish(0, True)

    def test_held_full_operation_rejects_competitors_and_watchdog(self):
        with lc.Operation(self.c, "stop") as op:
            for contender in (lc.Operation(self.c, "start"), lc.ReassertGuard(self.c)):
                with self.assertRaises(lc.Refused) as failure:
                    contender.__enter__()
                self.assertEqual(failure.exception.reason, "lifecycle_busy")
                self.assertTrue(self.held)
            op.finish(0, True)

    def test_begin_write_failure_returns_no_operation_and_releases_lock(self):
        with patch.object(lc.sw, "write_bytes", side_effect=OSError("write failed")):
            with self.assertRaisesRegex(OSError, "write failed"):
                with lc.Operation(self.c, "start"):
                    self.fail("entered after failed durable begin")
        self.assertFalse(self.held)
        self.assertEqual(lc.read_active(self.c), ("missing", None))

    def test_finish_write_failure_propagates_and_never_becomes_success(self):
        with self.assertRaisesRegex(OSError, "write failed"):
            with lc.Operation(self.c, "start") as op:
                with patch.object(lc.sw, "write_bytes", side_effect=OSError("write failed")):
                    op.finish(0, True)
        self.assertEqual(self.stored()["status"], "incomplete")
        self.assertFalse(self.held)

    def test_record_identity_change_is_not_overwritten(self):
        op = lc.Operation(self.c, "start").__enter__()
        changed = self.stored()
        changed["id"] = "33333333-3333-4333-8333-333333333333"
        self.put(changed)
        with self.assertRaises(lc.Refused):
            op.phase("next")
        with self.assertRaises(lc.Refused):
            op.__exit__(None, None, None)
        self.assertEqual(self.stored(), changed)
        self.assertFalse(self.held)

    def test_finish_failure_after_visible_rename_is_reasserted_incomplete(self):
        original = lc.sw.write_bytes
        calls = []

        def visible_then_fail(path, data):
            original(path, data)
            calls.append(json.loads(data))
            if calls[-1]["status"] == "finished":
                raise OSError("directory fsync failed after rename")

        with self.assertRaisesRegex(OSError, "fsync failed"):
            with lc.Operation(self.c, "start") as op:
                with patch.object(lc.sw, "write_bytes", side_effect=visible_then_fail):
                    op.finish(0, True)
        self.assertEqual(self.stored()["status"], "incomplete")
        self.assertFalse(self.held)

    def test_watchdog_does_not_create_or_change_operation_record(self):
        with self.assertRaises(lc.Refused) as missing:
            with lc.ReassertGuard(self.c):
                self.fail("missing operation record authorized watchdog export")
        self.assertEqual(missing.exception.reason, "active_op_missing")
        self.assertEqual(lc.read_active(self.c), ("missing", None))
        self.completed()
        before = self.stored()
        with lc.ReassertGuard(self.c):
            self.assertEqual(self.stored(), before)
        self.assertEqual(self.stored(), before)

    def test_watchdog_refuses_incomplete_or_unknown_without_overwriting(self):
        with lc.Operation(self.c, "stop"):
            pass
        for value, reason in ((self.stored(), "active_op_incomplete"),
                              ({"bad": 1}, "active_op_unknown")):
            self.put(value)
            with self.assertRaises(lc.Refused) as failure:
                with lc.ReassertGuard(self.c):
                    self.fail("entered")
            self.assertEqual(failure.exception.reason, reason)
            self.assertEqual(self.stored(), value)
            self.assertFalse(self.held)

    def test_lock_interface_and_control_errors_fail_closed(self):
        with patch.object(lc.locks, "MountLock", side_effect=OSError("open denied")):
            # Construction failures are also lock-interface failures.
            with self.assertRaises(lc.Refused):
                lc.Operation(self.c, "start").__enter__()
        bad_control = unittest.mock.MagicMock()
        bad_control.__enter__.side_effect = OSError("control open denied")
        with patch.object(lc.gate, "control_lock", return_value=bad_control):
            with self.assertRaises(lc.Refused) as failure:
                lc.Operation(self.c, "start").__enter__()
            self.assertEqual(failure.exception.reason, "control_unknown")
        self.assertFalse(self.held)

    def test_owner_unknown_refuses_before_begin_record(self):
        with patch.object(lc, "_owner", side_effect=OSError("proc unavailable")):
            with self.assertRaises(lc.Refused) as failure:
                lc.Operation(self.c, "start").__enter__()
        self.assertEqual(failure.exception.reason, "owner_unknown")
        self.assertEqual(lc.read_active(self.c), ("missing", None))
        self.assertFalse(self.held)

    def test_cleanup_can_repair_incomplete_stop_but_cannot_clear_recovery(self):
        with lc.Operation(self.c, "stop") as op:
            op.finish(3, False)
        with lc.Operation(self.c, "clean-orphans") as op:
            self.assertTrue(op.record["recovery_required"])
            self.assertEqual(op.record["previous"]["op"], "stop")
            op.finish(0, True)
        self.assertEqual(self.stored()["status"], "finished")
        self.assertTrue(self.stored()["recovery_required"])
        for action in ("start", "maintain", "clear-hold"):
            with self.subTest(action=action), self.assertRaises(lc.Refused) as failure:
                with lc.Operation(self.c, action):
                    self.fail("recovery bypassed")
            self.assertEqual(failure.exception.reason, "recovery_required")
        with self.assertRaises(lc.Refused) as failure:
            with lc.ReassertGuard(self.c):
                self.fail("recovery bypassed")
        self.assertEqual(failure.exception.reason, "recovery_required")
        with lc.Operation(self.c, "stop") as op:
            op.finish(0, True)
        self.assertFalse(self.stored()["recovery_required"])
        with lc.Operation(self.c, "start") as op:
            op.finish(0, True)

    def test_cleanup_does_not_replace_incomplete_start_or_unknown_record(self):
        with lc.Operation(self.c, "start"):
            pass
        with self.assertRaises(lc.Refused):
            with lc.Operation(self.c, "clean-orphans"):
                self.fail("incomplete start bypassed")
        self.put({"bad": "record"})
        with self.assertRaises(lc.Refused):
            with lc.Operation(self.c, "clean-orphans"):
                self.fail("unknown record bypassed")


if __name__ == "__main__":
    unittest.main()
