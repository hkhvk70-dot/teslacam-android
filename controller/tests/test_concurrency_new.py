"""Real independent test processes, with all business/device operations replaced."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import offline_support
import gate


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=offline_support.SCRATCH)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        for name in ("state", "run", "cfg", "proc"):
            (self.base / name).mkdir()
        lun = self.base / "cfg" / gate.LUN_REL
        lun.parent.mkdir(parents=True)
        lun.write_text("")
        (self.base / "img").write_bytes(b"fixture")
        self.worker = Path(__file__).with_name("integration_worker.py")
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for p in self.children:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=5)

    def launch(self, mode):
        p = subprocess.Popen([sys.executable, "-I", "-B", str(self.worker), mode, str(self.base)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.children.append(p)
        return p

    def result(self, p):
        out, err = p.communicate(timeout=8)
        self.assertTrue(out, err.decode(errors="replace"))
        return json.loads(out), p.returncode

    def wait_for_stop(self, p):
        deadline = time.monotonic() + 6
        while not (self.base / "stop-waiting").exists():
            if p.poll() is not None:
                self.fail("worker exited: " + repr(p.communicate()))
            if time.monotonic() > deadline:
                self.fail("worker did not enter stop wait")
            time.sleep(0.02)

    def test_start_and_watchdog_cannot_enter_during_stop_wait(self):
        stopping = self.launch("hold-stop")
        self.wait_for_stop(stopping)
        before = json.loads((self.base / "state" / "active_op.json").read_text())
        self.assertTrue((self.base / "control-available").exists())
        out, code = self.result(self.launch("start"))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out.get("reason"), "lifecycle_busy")
        self.assertTrue((self.base / "state" / "stop_requested").exists())
        self.assertFalse((self.base / "export-called").exists())
        out, code = self.result(self.launch("reassert"))
        self.assertEqual(code, 5, out)
        self.assertEqual(out.get("reason"), "lifecycle_busy")
        view, code = self.result(self.launch("status"))
        self.assertEqual(code, 0, view)
        self.assertEqual(view["active_op"]["id"], before["id"])
        self.assertEqual(view["active_op"]["phase"], "stopping_services")
        self.assertEqual(view["permission"], "forbidden")
        (self.base / "release-stop").write_text("1")
        out, code = self.result(stopping)
        self.assertEqual(code, 0, out)
        out, code = self.result(self.launch("start"))
        self.assertEqual(code, 0, out)

    def test_killed_controller_leaves_operation_blocked_until_explicit_stop(self):
        stopping = self.launch("hold-stop")
        self.wait_for_stop(stopping)
        stopping.kill()
        stopping.communicate(timeout=5)
        old = json.loads((self.base / "state" / "active_op.json").read_text())
        out, code = self.result(self.launch("start"))
        self.assertNotEqual(code, 0, out)
        self.assertEqual(out.get("reason"), "active_op_incomplete")
        out, code = self.result(self.launch("stop"))
        self.assertEqual(code, 0, out)
        self.assertEqual(out["active_op"]["previous"]["id"], old["id"])
        self.assertFalse(out["active_op"]["recovery_required"])


if __name__ == "__main__":
    unittest.main()
