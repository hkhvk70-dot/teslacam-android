#!/usr/bin/env python3
"""test_edges2_new.py —— fix2 三处边界回归

① 枚举期间正常消失的 PID 不得让整组变 unknown
② 补快照时确认退出的成员必须从返回名单剔除，全退为 empty
③ 补快照必须绑定刚核验的实例（离组/会话变更/组长锚定/令牌）
"""

import builtins
import io
import os
import sys
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


class Enumeration(Base):
    def test_unrelated_pid_vanished(self):
        """无关 PID 正常消失：不得影响健康目标组。"""
        def fake_stat(pid):
            if pid == 999:
                raise FileNotFoundError()
            return st()

        self.patch(pi, "_stat", fake_stat)
        self.patch(pi.os, "listdir", lambda p: ["701", "999"])
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        # 目录观察也注入，不依赖宿主是否恰好存在 999 号进程
        real = os.stat

        def fake_os_stat(path, *a, **k):
            if str(path).endswith("/999"):
                raise FileNotFoundError()
            return real(path, *a, **k)

        self.patch(pi.os, "stat", fake_os_stat)
        reason, _d, members = pi._verify_group(
            pi._make_ctx(700, TOK, 700))
        self.assertEqual(reason, "ok")
        self.assertEqual(members, [701])

    def test_target_member_vanished(self):
        """目标子成员消失：剔除，其余仍核验。"""
        def fake_stat(pid):
            if pid == 702:
                raise FileNotFoundError()
            return st()

        self.patch(pi, "_stat", fake_stat)
        self.patch(pi.os, "listdir", lambda p: ["701", "702"])
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        real2 = os.stat

        def fake_os_stat2(path, *a, **k):
            if str(path).endswith("/702"):
                raise FileNotFoundError()
            return real2(path, *a, **k)

        self.patch(pi.os, "stat", fake_os_stat2)
        reason, _d, members = pi._verify_group(pi._make_ctx(700, TOK, 700))
        self.assertEqual(reason, "ok")
        self.assertEqual(members, [701])

    def test_unreadable_still_unknown(self):
        """权限不可读且仍在：必须 unknown，不能当成消失。"""
        def fake_stat(pid):
            if pid == 702:
                raise PermissionError()
            return st()

        self.patch(pi, "_stat", fake_stat)
        self.patch(pi.os, "listdir", lambda p: ["701", "702"])
        self.patch(pi, "_departed", lambda pid, pgid=None: False)
        reason, detail, _m = pi._verify_group(pi._make_ctx(700, TOK, 700))
        self.assertEqual(reason, "identity_unknown")
        self.assertIn("enumeration_incomplete", detail)

    def test_departed_uses_dir_observation(self):
        """_departed 不依赖真实 /proc，只看注入的 stat 结果。"""
        self.patch(pi, "_stat",
                   lambda pid: (_ for _ in ()).throw(FileNotFoundError()))
        real = os.stat

        def fake_os_stat(path, *a, **k):
            raise FileNotFoundError()

        self.patch(pi.os, "stat", fake_os_stat)
        self.assertTrue(pi._departed(701, 700))


class SnapshotBind(Base):
    """root == '/proc' 分支：用 builtins.open 提供 environ。"""

    def setUp(self):
        super().setUp()
        self.environ = {}
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if isinstance(path, str) and path.startswith("/proc/") \
                    and path.endswith("/environ"):
                pid = int(path.split("/")[2])
                return io.BytesIO(self.environ.get(pid, b""))
            return real_open(path, *a, **k)

        self.patch(builtins, "open", fake_open)
        for pid in (701, 702):
            self.environ[pid] = ("%s=%s\0" % (pi.TOKEN_KEY, TOK)).encode()

    def rec(self):
        return {"instance": TOK, "pgid": 700, "sid": 700,
                "leader_pid": 701, "leader_starttime": 1}

    def scan(self, pids=(701,)):
        return {"items": {p: {"pgrp": 700, "sid": 700, "starttime": 1,
                              "state": "S", "argv": None} for p in pids},
                "stat_complete": True, "cmdline_complete": True}

    def verify_ok(self, members):
        self.patch(sv.pi, "verify",
                   lambda r: {"reason": "ok", "members": members,
                              "killable": True})

    def test_stable_new_member_bound(self):
        self.verify_ok([701, 702])
        self.patch(sv, "_stat", lambda root, pid: st())
        scan = self.scan()
        out = sv._verify_record(self.rec(), "/proc", scan)
        self.assertEqual(out["reason"], "ok")
        self.assertEqual(out["members"], [701, 702])
        self.assertIn(702, scan["items"])
        self.assertEqual(scan["items"][702]["pgrp"], 700)

    def test_new_member_left_group(self):
        """核验后离组：剔除，不补快照。"""
        self.verify_ok([701, 702])

        def fake_stat(root, pid):
            return st(pgrp=900, sid=900, start=99) if pid == 702 else st()

        self.patch(sv, "_stat", fake_stat)
        scan = self.scan()
        out = sv._verify_record(self.rec(), "/proc", scan)
        self.assertEqual(out["reason"], "ok")
        self.assertEqual(out["members"], [701])
        self.assertNotIn(702, scan["items"])

    def test_all_gone_is_empty(self):
        self.verify_ok([702])
        self.patch(sv, "_stat",
                   lambda root, pid: (_ for _ in ()).throw(FileNotFoundError()))
        self.patch(sv, "_departed", lambda pid, rec, root="/proc": True)
        scan = self.scan()
        out = sv._verify_record(self.rec(), "/proc", scan)
        self.assertEqual(out["reason"], "empty")
        self.assertEqual(out["members"], [])

    def test_leader_starttime_changed(self):
        """补快照时组长启动时间变化：未知，不能 ok。"""
        self.verify_ok([701])
        self.patch(sv, "_stat", lambda root, pid: st(start=99))
        out = sv._verify_record(self.rec(), "/proc", self.scan())
        self.assertEqual(out["reason"], "identity_unknown")

    def test_token_mismatch_after_verify(self):
        self.verify_ok([701, 702])
        self.environ[702] = b"TESLACAM_INSTANCE=other\0"
        self.patch(sv, "_stat", lambda root, pid: st())
        out = sv._verify_record(self.rec(), "/proc", self.scan())
        self.assertEqual(out["reason"], "identity_unknown")

    def test_unstable_stat_unknown(self):
        seq = [st(start=1), st(start=2)]

        def fake_stat(root, pid):
            return seq.pop(0) if seq else st(start=2)

        self.verify_ok([702])
        self.patch(sv, "_stat", fake_stat)
        out = sv._verify_record(self.rec(), "/proc", self.scan())
        self.assertEqual(out["reason"], "identity_unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
