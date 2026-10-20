#!/usr/bin/env python3
"""test_edges3_new.py —— fix4 回归：environ 窗口 / 完整 build / 枚举确认"""

import builtins
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_CAND = [
    os.path.join(HERE, "..", "src"),
    os.path.normpath(os.path.join(HERE, "..", "..", "src")),
    "/data/local/tmp/p5p6fix/src",
]
SRC = next((p for p in _CAND if os.path.isdir(p)), _CAND[0])
sys.path.insert(0, SRC)

import proc_identity as pi      # noqa: E402
import status_view as sv        # noqa: E402

TOK = "inst-abc"


def st(pgrp=700, sid=700, start=1, state="S"):
    return {"state": state, "pgrp": pgrp, "sid": sid, "starttime": start}


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.patch(pi, "_own_pgid", lambda: -1)

    def patch(self, obj, name, fn):
        self.saved[(id(obj), name)] = (obj, name, getattr(obj, name))
        setattr(obj, name, fn)

    def tearDown(self):
        for (o, name, old) in self.saved.values():
            setattr(o, name, old)


class EnvironWindow(Base):
    """令牌读取期间的变化必须被 post-stat 发现。"""

    def setUp(self):
        super().setUp()
        self.after_environ = None
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if isinstance(path, str) and path.startswith("/proc/") \
                    and path.endswith("/environ"):
                return io.BytesIO(("%s=%s\0" % (pi.TOKEN_KEY, TOK)).encode())
            return real_open(path, *a, **k)

        self.patch(builtins, "open", fake_open)

    def _run(self, post):
        calls = {"n": 0}

        def fake_stat(root, pid):
            calls["n"] += 1
            return post if calls["n"] >= 2 else st()

        self.patch(sv, "_stat", fake_stat)
        self.patch(sv, "_departed", lambda pid, rec, root="/proc": False)
        rec = {"instance": TOK, "pgid": 700, "sid": 700,
               "leader_pid": 701, "leader_starttime": 1}
        return sv._bind_member(702, rec, "/proc"), calls["n"]

    def test_dead_during_environ(self):
        (state, snap), n = self._run(st(state="Z"))
        self.assertEqual(state, "gone")
        self.assertIsNone(snap)
        self.assertGreaterEqual(n, 2, "environ 之后必须再读一次 stat")

    def test_left_group_during_environ(self):
        (state, _s), _n = self._run(st(pgrp=900))
        self.assertEqual(state, "gone")

    def test_instance_changed_during_environ(self):
        (state, _s), _n = self._run(st(start=99))
        self.assertEqual(state, "bad")

    def test_stable_ok(self):
        (state, snap), _n = self._run(st())
        self.assertEqual(state, "ok")
        self.assertEqual(snap["starttime"], 1)


class EnumConfirm(Base):
    def test_missing_file_but_dir_present(self):
        """stat 报不存在、PID 目录仍在 → 未知，不能跳过。"""
        self.patch(pi, "_stat",
                   lambda pid: (_ for _ in ()).throw(FileNotFoundError()))
        self.patch(pi, "_departed", lambda pid, pgid=None: False)
        self.patch(pi.os, "listdir", lambda p: ["701"])
        reason, detail, _m = pi._verify_group(pi._make_ctx(700, TOK, 700))
        self.assertEqual(reason, "identity_unknown")
        self.assertIn("enumeration_incomplete", detail)

    def test_dir_unreadable(self):
        """目录观察权限不足 → 无法确认 → 未知。"""
        self.patch(pi, "_stat",
                   lambda pid: (_ for _ in ()).throw(PermissionError()))
        self.patch(pi.os, "stat",
                   lambda p, *a, **k: (_ for _ in ()).throw(PermissionError()))
        self.patch(pi.os, "listdir", lambda p: ["701"])
        reason, _d, _m = pi._verify_group(pi._make_ctx(700, TOK, 700))
        self.assertEqual(reason, "identity_unknown")


class BuildRegression(Base):
    """完整 build()：绑定阶段成员退出，不得 KeyError。"""

    def setUp(self):
        super().setUp()
        self.base = tempfile.mkdtemp(prefix="svbuild")
        self.addCleanup(shutil.rmtree, self.base, True)
        os.makedirs(os.path.join(self.base, "run"))
        os.makedirs(os.path.join(self.base, "state"))
        with open(os.path.join(self.base, "services.json"), "w") as f:
            json.dump({"pipeline": {"cmd": ["sh", "/data/teslacam/bin/pipeline.sh"]}}, f)
        with open(os.path.join(self.base, "run", "pipeline.group"), "w") as f:
            json.dump({"instance": TOK, "pgid": 702, "sid": 702,
                       "leader_pid": 702, "leader_starttime": 1}, f)

    def cfg(self):
        return {"state": os.path.join(self.base, "state"),
                "run": os.path.join(self.base, "run"),
                "services_conf": os.path.join(self.base, "services.json"),
                "proc_root": "/proc",
                "cfg": os.path.join(self.base, "cfg"),
                "img": os.path.join(self.base, "img")}

    def scan_with(self, pids):
        items = {}
        for pid in pids:
            items[pid] = {"state": "S", "pgrp": pid, "sid": pid, "starttime": 1,
                          "argv": ["sh", "/data/teslacam/bin/pipeline.sh"]}
        return {"items": items, "complete": True, "stat_complete": True,
                "cmdline_complete": True, "scanned": len(items), "reasons": []}

    def _run(self, verify_result, pop_pids):
        scan = self.scan_with([702])

        def fake_scan(c):
            return scan

        def fake_verify(rec, root, sc):
            for pid in pop_pids:
                sc["items"].pop(pid, None)
            return verify_result

        self.patch(sv, "_scan_proc", fake_scan)
        self.patch(sv, "_verify_record", fake_verify)
        out = sv.build(self.cfg())
        return out, scan

    def test_member_exit_partial(self):
        out, _scan = self._run(
            {"reason": "ok", "members": [701], "complete": True}, [702])
        self.assertIn("services", out)
        self.assertNotIn(702, [u["pid"] for u in out.get("unmanaged", [])])

    def test_member_exit_all(self):
        out, _scan = self._run(
            {"reason": "empty", "members": [], "complete": True}, [702])
        self.assertIn("services", out)
        self.assertNotIn(702, [u["pid"] for u in out.get("unmanaged", [])])


if __name__ == "__main__":
    unittest.main(verbosity=2)
