"""Controller integration against disposable state and blocked platform helpers."""
import contextlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

import offline_support
import lifecycle
import teslacam as tc


OWNER = {"pid": 1234, "starttime": 99,
         "boot_id": "22222222-2222-4222-8222-222222222222"}
FREE = (tc.locks.FREE, {"loops": [], "mounts": [], "reason": ""})


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=offline_support.SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name in ("state", "run", "cfg", "proc"):
            (root / name).mkdir()
        self.c = dict(tc.DEFAULTS, state=str(root / "state"), run=str(root / "run"),
                      cfg=str(root / "cfg"), img=str(root / "img"),
                      proc_root=str(root / "proc"), services="", services_conf=str(root / "no-config"),
                      mount_lock=str(root / "mount.lock"), proc_mounts=str(root / "mounts"))
        self.lun = root / "cfg" / tc.gate.LUN_REL
        self.lun.parent.mkdir(parents=True)
        self.lun.write_text("")
        Path(self.c["img"]).write_bytes(b"fixture")
        Path(self.c["proc_mounts"]).write_text("")
        for p in (patch.object(lifecycle, "_owner", return_value=OWNER),
                  patch.object(tc.sw, "_fsync_dir", return_value=None),
                  patch.object(tc.locks, "probe_all", return_value=FREE),
                  patch.object(tc.subprocess, "run", side_effect=AssertionError("real helper blocked")),
                  patch.object(tc.subprocess, "Popen", side_effect=AssertionError("real service blocked")),
                  patch.object(tc.os, "killpg", create=True, side_effect=AssertionError("real signal blocked"))):
            p.start()
            self.addCleanup(p.stop)

    def call(self, action, *args):
        out = {"ok": False, "steps": []}
        code = getattr(tc, action + "_body")(self.c, out, *args)
        return code, out

    def fake_msd(self, c, action):
        self.lun.write_text(c["img"] if action == "up" else "")
        return True, ""

    def test_stop_completes_and_keeps_intent_then_explicit_start(self):
        with patch.object(tc, "run_msd", side_effect=self.fake_msd):
            code, out = self.call("stop")
            self.assertEqual(code, 0, out)
            stop_record = lifecycle.read_active(self.c)[1]
            self.assertEqual(stop_record["status"], "finished")
            self.assertTrue((Path(self.c["state"]) / "stop_requested").exists())
            with patch.object(tc, "ensure_clean", return_value=(True, {}, False)):
                code, out = self.call("start")
            self.assertEqual(code, 0, out)
            self.assertFalse((Path(self.c["state"]) / "stop_requested").exists())
            rec = lifecycle.read_active(self.c)[1]
            self.assertEqual(rec["previous"]["id"], stop_record["id"])

    def test_start_refuses_unfinished_stop_without_clearing_barrier(self):
        with lifecycle.Operation(self.c, "stop"):
            pass
        marker = Path(self.c["state"]) / "stop_requested"
        marker.write_text("1")
        with patch.object(tc, "ensure_clean") as checker:
            code, out = self.call("start")
        self.assertNotEqual(code, 0)
        self.assertEqual(out["reason"], "active_op_incomplete")
        self.assertTrue(marker.exists())
        checker.assert_not_called()

    def test_failed_stop_can_cleanup_but_only_retry_stop_unblocks_start(self):
        busy = (tc.locks.BUSY, {"loops": ["loop42"], "mounts": [], "reason": "loop_in_use"})
        with patch.object(tc, "run_msd", side_effect=self.fake_msd), \
                patch.object(tc.locks, "probe_all", return_value=busy):
            code, out = self.call("stop")
        self.assertNotEqual(code, 0, out)
        with patch.object(tc, "run_msd", side_effect=self.fake_msd):
            code, out = tc.clean_orphans_body(self.c, {"steps": []}, True), None
            self.assertEqual(code, 0)
            self.assertTrue(lifecycle.read_active(self.c)[1]["recovery_required"])
            code, out = self.call("start")
            self.assertNotEqual(code, 0)
            code, out = self.call("stop")
            self.assertEqual(code, 0, out)
            self.assertFalse(lifecycle.read_active(self.c)[1]["recovery_required"])

    def test_stop_reports_unmanaged_and_never_signals_it(self):
        view = tc.status_view.build(self.c)
        view["unmanaged"] = [{"pid": 5555, "reason": "fixture"}]
        with patch.object(tc.status_view, "build", return_value=view), \
                patch.object(tc, "run_msd", side_effect=self.fake_msd), \
                patch.object(tc.os, "killpg", create=True) as signal:
            code, out = self.call("stop")
        self.assertNotEqual(code, 0)
        self.assertEqual(out["unmanaged"][0]["pid"], 5555)
        signal.assert_not_called()

    def test_start_does_not_overwrite_existing_or_unknown_instance(self):
        view = tc.status_view.build(self.c)
        view["services"] = {"serve": {"registered": True, "alive": None}}
        marker = Path(self.c["state"]) / "stop_requested"
        marker.write_text("1")
        with patch.object(tc.status_view, "build", return_value=view), \
                patch.object(tc, "run_msd") as msd:
            code, out = self.call("start")
        self.assertNotEqual(code, 0)
        self.assertEqual(out["failed_at"], "service_preflight")
        self.assertTrue(marker.exists())
        msd.assert_not_called()

    def test_checker_wait_releases_control_and_commit_retains_work_lock(self):
        seen = []

        def checker(c, fd):
            with tc.gate.control_lock(str(Path(c["run"]) / "control.lock"), timeout=0):
                seen.append("control_available_during_checker")
            return 0, ""

        def commit(*args, **kwargs):
            with self.assertRaises(TimeoutError):
                with tc.locks.MountLock(self.c["mount_lock"], timeout=0):
                    pass
            seen.append("work_lock_held_during_commit")
            return {"ok": True, "steps": []}

        with patch.object(tc.gate, "evaluate", side_effect=[{"code": 11}, {"code": 11}, {"code": 0}]), \
                patch.object(tc, "run_checker", side_effect=checker), \
                patch.object(tc.commit, "commit_success", side_effect=commit):
            ok, _, ran = tc.ensure_clean(self.c, {"steps": []})
        self.assertTrue(ok and ran)
        self.assertEqual(len(seen), 2)

    def test_busy_and_unknown_never_auto_cleanup_or_check(self):
        for state in (tc.locks.BUSY, tc.locks.UNKNOWN):
            with self.subTest(state=state), \
                    patch.object(tc.gate, "evaluate", return_value={"code": 11}), \
                    patch.object(tc.locks, "probe_all", return_value=(state, {})), \
                    patch.object(tc, "cleanup_orphans") as cleanup, \
                    patch.object(tc, "run_checker") as checker:
                ok, _, ran = tc.ensure_clean(self.c, {"steps": []})
                self.assertFalse(ok or ran)
                cleanup.assert_not_called()
                checker.assert_not_called()

    def test_finite_helpers_inherit_only_work_and_lifecycle_descriptors(self):
        c = dict(self.c, _operation=types.SimpleNamespace(fileno=lambda: 41))
        with patch.object(tc.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stderr=b"")) as run:
            tc.run_msd(c, "up")
            self.assertEqual(run.call_args.kwargs["pass_fds"], (41,))
            tc.run_checker(c, 42)
            self.assertEqual(run.call_args.kwargs["pass_fds"], (41, 42))
            tc.cleanup_orphans(c, {}, ["loop42"], 42)
            self.assertEqual(run.call_args.kwargs["pass_fds"], (41, 42))

    def test_long_lived_services_do_not_receive_pass_fds(self):
        spec = {"cmd": ["fixture-service"], "ready": {"type": "alive"}}
        rec = {"pgid": 5555, "instance": "fixture"}
        with patch.object(tc.services.subprocess, "Popen", return_value=types.SimpleNamespace(pid=5555)) as spawn, \
                patch.object(tc.services.time, "sleep"), \
                patch.object(tc.services.pi, "register", return_value=rec), \
                patch.object(tc.services, "_wait_ready", return_value=(True, "ready")):
            result = tc.services.start_one(spec, "fixture", self.c["run"])
        self.assertTrue(result["ok"])
        self.assertNotIn("pass_fds", spawn.call_args.kwargs)
        self.assertIsNot(spawn.call_args.kwargs.get("close_fds"), False)

    def test_clean_requires_stop_and_never_calls_cleanup_when_lun_exported(self):
        for lun in (self.c["img"], ""):
            self.lun.write_text(lun)
            with patch.object(tc, "cleanup_orphans") as cleanup:
                out = {"steps": []}
                code = tc.clean_orphans_body.__wrapped__(self.c, out, True)
            self.assertNotEqual(code, 0)
            cleanup.assert_not_called()

    def test_clean_failure_stays_failed_even_after_free_rescan(self):
        (Path(self.c["state"]) / "stop_requested").write_text("1")
        busy = (tc.locks.BUSY, {"loops": ["loop42"], "mounts": []})
        with patch.object(tc.locks, "probe_all", side_effect=[busy, FREE]), \
                patch.object(tc, "cleanup_orphans", return_value=(False, [])):
            code = tc.clean_orphans_body(self.c, {"steps": []}, True)
        self.assertNotEqual(code, 0)
        self.assertEqual(lifecycle.read_active(self.c)[1]["status"], "incomplete")

    def test_state_begin_failure_prevents_all_business_actions(self):
        with patch.object(lifecycle, "_write", side_effect=OSError("injected")), \
                patch.object(tc, "run_msd") as msd:
            code, out = self.call("stop")
        self.assertNotEqual(code, 0)
        self.assertFalse(out["ok"])
        msd.assert_not_called()

    def test_bad_config_does_not_hide_known_records_from_stop(self):
        Path(self.c["services_conf"]).write_text("[]")
        (Path(self.c["run"]) / "serve.group").write_text("{}")
        self.assertIn("serve", tc._service_names(self.c))

    def test_ready_socket_scan_is_bounded(self):
        class ManyFDs:
            def __enter__(self):
                return iter(types.SimpleNamespace(path="fixture-fd") for _ in range(5000))

            def __exit__(self, *args):
                pass

        with patch.object(tc.status_view, "_stat", return_value={"starttime": 1, "pgrp": 2, "sid": 2}), \
                patch.object(tc.status_view.os, "scandir", return_value=ManyFDs()), \
                patch.object(tc.status_view.os, "readlink", return_value="anon_inode:fixture"):
            result = tc.status_view._socket_inodes("fixture", [2], {"items": {2: {"starttime": 1}}})
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
