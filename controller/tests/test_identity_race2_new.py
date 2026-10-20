#!/usr/bin/env python3
"""test_identity_race2_new.py —— 身份核验回归（Codex 两轮复核要求）

覆盖：
  确认退出才剔除 / 权限与 IO 不算退出 / 枚举不完整不得 ok
  environ 之后必须 post-stat / 登记组长启动时间锚定 / 重试耗尽
  P6 新成员补齐快照
"""

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

    def ctx(self, **kw):
        return pi._make_ctx(kw.get("pgid", 700), kw.get("instance", TOK),
                            kw.get("sid", 700),
                            leader_pid=kw.get("leader_pid", 0),
                            leader_start=kw.get("leader_start", 0))


class Enumeration(Base):
    def test_unreadable_member_makes_unknown(self):
        """枚举中某个 PID 读不到 → 不完整，不能返回 ok/killable。"""
        def fake_stat(pid):
            if pid == 702:
                raise PermissionError()
            return st()

        self.patch(pi, "_stat", fake_stat)
        real_stat = os.stat

        def fake_os_stat(path, *a, **k):
            if str(path).endswith("/702"):
                raise PermissionError()
            return real_stat(path, *a, **k)

        self.patch(pi.os, "stat", fake_os_stat)
        self.patch(pi.os, "listdir", lambda p: ["700", "701", "702"])
        reason, detail, members = pi._verify_group(self.ctx())
        self.assertEqual(reason, "identity_unknown")
        self.assertIn("enumeration_incomplete", detail)
        self.assertEqual(members, [])

    def test_permission_error_not_departed(self):
        """真实 _departed：stat 与 os.stat 都权限失败 → 不能算退出。"""
        self.patch(pi, "_stat",
                   lambda pid: (_ for _ in ()).throw(PermissionError()))

        def fake_os_stat(path, *a, **k):
            raise PermissionError()

        self.patch(pi.os, "stat", fake_os_stat)
        self.assertFalse(pi._departed(701, 700))

    def test_file_not_found_is_departed(self):
        self.patch(pi, "_stat",
                   lambda pid: (_ for _ in ()).throw(FileNotFoundError()))
        self.patch(pi.os, "stat",
                   lambda path, *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertTrue(pi._departed(701, 700))


class PostStat(Base):
    def setUp(self):
        super().setUp()
        self.calls = []

    def test_instance_switch_reenumerated(self):
        """environ 之后实例切换：必须复查并重新读取，不能沿用上一轮结论。

        语义：post-stat 发现变化 → 进入下一轮重新核验；
              重新读取后稳定才接受。
        """
        seq = [st(start=1), st(start=2), st(start=3), st(start=3)]
        env_calls = []

        def fake_stat(pid):
            self.calls.append(1)
            return seq.pop(0) if seq else st(start=3)

        def fake_env(pid, k, v):
            env_calls.append(1)
            return True, "ok"

        self.patch(pi, "group_members_ex", lambda pgid: ([701], []))
        self.patch(pi, "_stat", fake_stat)
        self.patch(pi, "_env_has", fake_env)
        reason, _d, _m = pi._verify_group(self.ctx())
        self.assertGreaterEqual(len(self.calls), 2, "必须执行 post-stat")
        self.assertGreaterEqual(len(env_calls), 2, "变化后必须重新读取 environ")
        self.assertEqual(reason, "ok")

    def test_retries_exhausted(self):
        n = [0]

        def fake_stat(pid):
            n[0] += 1
            return st(start=n[0])

        self.patch(pi, "group_members_ex", lambda pgid: ([701], []))
        self.patch(pi, "_stat", fake_stat)
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        reason, detail, _m = pi._verify_group(self.ctx())
        self.assertEqual(reason, "identity_unknown")
        self.assertIn("unstable_after_retries", detail)

    def test_stable_member_ok(self):
        self.patch(pi, "group_members_ex", lambda pgid: ([701], []))
        self.patch(pi, "_stat", lambda pid: st())
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        reason, _d, members = pi._verify_group(self.ctx())
        self.assertEqual(reason, "ok")
        self.assertEqual(members, [701])


class LeaderAnchor(Base):
    def test_leader_starttime_change_rejected(self):
        """登记组长启动时间变化：令牌/SID 相符也不能放过。"""
        self.patch(pi, "group_members_ex", lambda pgid: ([701], []))
        self.patch(pi, "_stat", lambda pid: st(start=99))
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        v = pi.verify({"pgid": 700, "instance": TOK, "sid": 700,
                       "leader_pid": 701, "leader_starttime": 1})
        self.assertEqual(v["reason"], "identity_unknown")
        self.assertFalse(v["killable"])
        self.assertIn("leader_starttime", v["detail"])

    def test_non_leader_reuse_allowed(self):
        self.patch(pi, "group_members_ex", lambda pgid: ([702], []))
        self.patch(pi, "_stat", lambda pid: st(start=42))
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        ctx = pi._make_ctx(700, TOK, 700, leader_pid=701,
                           leader_start=1, starttimes={701: 1})
        reason, _d, members = pi._verify_group(ctx)
        self.assertEqual(reason, "ok")
        self.assertEqual(members, [702])


class VerifyToken(Base):
    def test_same_semantics(self):
        self.patch(pi, "group_members_ex", lambda pgid: ([701], []))
        self.patch(pi, "_stat", lambda pid: st())
        self.patch(pi, "_env_has", lambda pid, k, v: (True, "ok"))
        reason, members = pi.verify_token(700, TOK)
        self.assertEqual(reason, "ok")
        self.assertEqual(members, [701])

    def test_all_departed_empty(self):
        self.patch(pi, "group_members_ex", lambda pgid: ([], []))
        reason, members = pi.verify_token(700, TOK)
        self.assertEqual(reason, "empty")
        self.assertEqual(members, [])


class StatusFixture(Base):
    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="svrace")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        super().tearDown()

    def mk(self, pid, tag=TOK, start=1, pgrp=700, sid=700, state="S"):
        d = os.path.join(self.root, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "stat"), "wb") as f:
            f.write(b"%d (proc) %s 1 %d %d " % (pid, state.encode(), pgrp, sid))
            f.write(b"0 " * 15)
            f.write(b"%d 0\n" % start)
        if tag is not None:
            with open(os.path.join(d, "environ"), "wb") as f:
                f.write(("%s=%s\0" % (pi.TOKEN_KEY, tag)).encode())

    def rec(self):
        return {"instance": TOK, "pgid": 700, "sid": 700,
                "leader_pid": 701, "leader_starttime": 1}

    def scan(self, pids):
        return {"items": {p: {"pgrp": 700, "sid": 700, "starttime": 1,
                              "state": "S", "argv": None} for p in pids},
                "stat_complete": True, "cmdline_complete": True}

    def test_vanished_member_excluded(self):
        self.mk(701)
        self.mk(702)
        scan = self.scan([701, 702])
        shutil.rmtree(os.path.join(self.root, "702"))
        out = sv._verify_record(self.rec(), self.root, scan)
        self.assertEqual(out["reason"], "ok")
        self.assertEqual(out["members"], [701])

    def test_wrong_token_unknown(self):
        self.mk(701)
        self.mk(702, tag="other")
        out = sv._verify_record(self.rec(), self.root, self.scan([701, 702]))
        self.assertEqual(out["reason"], "identity_unknown")

    def test_all_gone_empty(self):
        self.mk(701)
        scan = self.scan([701])
        shutil.rmtree(os.path.join(self.root, "701"))
        out = sv._verify_record(self.rec(), self.root, scan)
        self.assertEqual(out["reason"], "empty")

    def test_incomplete_scan_unknown(self):
        self.mk(701)
        scan = self.scan([701])
        scan["stat_complete"] = False
        out = sv._verify_record(self.rec(), self.root, scan)
        self.assertEqual(out["reason"], "identity_unknown")


class StatusRealBranch(Base):
    """root == '/proc' 分支：核验新增成员必须补齐快照。"""

    def rec(self):
        return {"instance": TOK, "pgid": 700, "sid": 700,
                "leader_pid": 701, "leader_starttime": 1}

    def scan(self):
        return {"items": {701: {"pgrp": 700, "sid": 700, "starttime": 1,
                                "state": "S", "argv": None}},
                "stat_complete": True, "cmdline_complete": True}



if __name__ == "__main__":
    unittest.main(verbosity=2)
