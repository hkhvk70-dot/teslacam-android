#!/usr/bin/env python3
"""proc_identity.py —— 进程组启动身份核验（A18）

设计依据：设计修订稿 v6 第 1 节
核心：SID/PGID 会被复用，命令行只是辅助。
     主依据是**启动时注入的实例令牌** TESLACAM_INSTANCE=<uuid>，
     通过 /proc/<pid>/environ 核验每个成员。

命令行：
  proc_identity.py register <record.json> <instance> <pgid> <leader_pid>
  proc_identity.py verify   <record.json>          # 输出 JSON 判定
  proc_identity.py members  <pgid>

判定结果（reason）：
  ok                全部成员核验通过，可整组终止
  empty             组内无进程（调用方需连续两次为空才认定已停止）
  identity_unknown  任一成员令牌缺失/不可读/SID 不符 → 拒绝终止
  refused           组号非法或为危险值
"""

import json
import os
import sys

TOKEN_KEY = "TESLACAM_INSTANCE"


# ---------------- /proc 读取 ----------------

def _stat(pid):
    """返回 pgrp/sid/starttime。comm 可能含空格括号，从最后一个 ')' 之后解析。"""
    with open("/proc/%d/stat" % pid, "rb") as f:
        raw = f.read()
    rp = raw.rfind(b")")
    if rp < 0:
        raise OSError("bad stat")
    rest = raw[rp + 2:].split()
    # rest: state ppid pgrp session tty ... ; starttime 是第 22 字段 → 这里下标 19
    return {
        "state": rest[0].decode("ascii", "replace"),
        "pgrp": int(rest[2]),
        "sid": int(rest[3]),
        "starttime": int(rest[19]),
    }


def _env_has(pid, key, value):
    """检查进程环境是否含 key=value。返回 (bool, reason)。"""
    try:
        with open("/proc/%d/environ" % pid, "rb") as f:
            raw = f.read()
    except OSError:
        return False, "environ_unreadable"
    needle = ("%s=%s" % (key, value)).encode()
    for item in raw.split(b"\0"):
        if item == needle:
            return True, "ok"
    return False, "token_missing"


def group_members(pgid):
    """枚举进程组内所有进程（不依赖 pgrep）。"""
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            st = _stat(pid)
        except (OSError, ValueError, IndexError):
            continue
        if st["pgrp"] == pgid and st["state"] not in ("Z", "X", "x"):
            out.append(pid)          # 僵尸不算存活成员
    return out


# ---------------- 登记 ----------------

def register(record_path, instance, pgid, leader_pid):
    st = _stat(leader_pid)
    rec = {
        "instance": instance,
        "pgid": pgid,
        "sid": st["sid"],
        "leader_pid": leader_pid,
        "leader_starttime": st["starttime"],
        "started_at": int(__import__("time").time()),
    }
    d = os.path.dirname(os.path.abspath(record_path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = record_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, record_path)
    return rec


# ---------------- 核验 ----------------

def _own_pgid():
    try:
        return _stat(os.getpid())["pgrp"]
    except Exception:
        return -1


DEAD_STATES = ("Z", "X", "x")


def _exists_or_none(path):
    """返回 True / False / None。

    None 表示"无法确认"（权限、I/O 等），不能当成不存在。
    """
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _departed(pid, pgid=None):
    """确认该 PID 已不是本次观察的对象。

    只有"进程确实不存在""死亡/僵尸""已离开目标组"才返回 True。
    权限错误与 I/O 错误返回 False（无法确认，按仍在处理）。
    """
    try:
        st = _stat(pid)
    except ProcessLookupError:
        return True
    except OSError:
        present = _exists_or_none("/proc/%d" % pid)
        return present is False        # None（不可读）不算退出
    if st["state"] in DEAD_STATES:
        return True
    if pgid is not None and st["pgrp"] != pgid:
        return True
    return False


def group_members_ex(pgid):
    """枚举进程组成员，同时报告无法确认的成员。

    返回 (members, unknown_reasons)。unknown 非空时，本次枚举不完整，
    调用方不得据此返回可终止的 ok。
    """
    members, unknown = [], []
    try:
        names = os.listdir("/proc")
    except OSError as e:
        return [], ["proc_unreadable:%s" % e]
    for name in names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            st = _stat(pid)
        except OSError as e:
            # 任何读取失败都走退出确认：只有确认消失才跳过，
            # 目录仍在或无法确认的一律计为未知。
            if _departed(pid, pgid):
                continue
            unknown.append("%d:stat_unknown:%s" % (pid, e))
            continue
        if st["pgrp"] == pgid and st["state"] not in DEAD_STATES:
            members.append(pid)
    return sorted(members), unknown


def _probe_member(pid, ctx, tries=3):
    """核验单个成员。返回 (state, why)。

    ok   核验通过（environ 之后已复核同一实例）
    gone 已确认离开
    bad  可能仍在本组但无法确认
    """
    pgid = ctx["pgid"]
    for _attempt in range(tries):
        # ---- stat-before ----
        try:
            before = _stat(pid)
        except OSError:
            if _departed(pid, pgid):
                return "gone", "stat_gone"
            return "bad", "stat_unreadable"

        if before["state"] in DEAD_STATES:
            return "gone", "zombie"
        if before["pgrp"] != pgid:
            return "gone", "left_group"

        leader = pid == ctx["leader_pid"]
        known_start = ctx["starttimes"].get(pid)

        # 登记组长的启动时间必须锚定，不能被"新实例"分支绕过
        if leader and ctx["leader_start"] and before["starttime"] != ctx["leader_start"]:
            return "bad", "leader_starttime_mismatch"

        reused = (known_start is not None
                  and before["starttime"] != known_start)
        if reused and leader:
            return "bad", "leader_starttime_mismatch"

        if ctx["sid"] is not None and before["sid"] != ctx["sid"]:
            return "bad", "sid_mismatch"

        # ---- environ ----
        ok, why = _env_has(pid, TOKEN_KEY, ctx["instance"])
        if not ok:
            if _departed(pid, pgid):
                return "gone", "env_gone:%s" % why
            return "bad", why

        # ---- stat-after：必须仍是同一个实例 ----
        try:
            after = _stat(pid)
        except OSError:
            if _departed(pid, pgid):
                return "gone", "post_stat_gone"
            continue                       # 无法复核：进入下一轮
        if after["state"] in DEAD_STATES:
            return "gone", "post_zombie"
        if after["pgrp"] != pgid:
            return "gone", "post_left_group"
        if any(after[k] != before[k] for k in ("starttime", "pgrp", "sid")):
            # 观察期间实例已变化：重新判定，不沿用本轮结论
            if _departed(pid, pgid):
                return "gone", "post_changed_gone"
            continue
        if leader and ctx["leader_start"] and after["starttime"] != ctx["leader_start"]:
            return "bad", "leader_starttime_mismatch"

        return "ok", ("reused_verified" if reused else "ok")

    return "bad", "unstable_after_retries"


def _make_ctx(pgid, instance, sid, leader_pid=0, leader_start=0, starttimes=None):
    return {"pgid": pgid, "instance": instance, "sid": sid,
            "leader_pid": leader_pid, "leader_start": leader_start,
            "starttimes": starttimes or {}}


def _verify_group(ctx):
    """共享核验流程。返回 (reason, detail, members)。"""
    members, unknown = group_members_ex(ctx["pgid"])
    if unknown:
        # 枚举不完整：无法确认是否有成员被漏掉
        return ("identity_unknown",
                "enumeration_incomplete:" + ";".join(unknown[:3]), [])

    if not members:
        return "empty", "no member", []

    alive, bad = [], []
    for pid in members:
        state, why = _probe_member(pid, ctx)
        if state == "ok":
            alive.append(pid)
        elif state == "gone":
            continue
        else:
            bad.append("%d:%s" % (pid, why))

    if bad:
        return "identity_unknown", ";".join(bad), sorted(alive)
    if not alive:
        return "empty", "all members departed", []
    return "ok", "%d members verified" % len(alive), sorted(alive)


def verify(record):
    """返回 {reason, detail, members, killable}。"""
    pgid = record.get("pgid")
    instance = record.get("instance")
    sid = record.get("sid")
    leader_start = record.get("leader_starttime", 0)

    if not isinstance(pgid, int) or pgid <= 1:
        return {"reason": "refused", "detail": "pgid invalid",
                "members": [], "killable": False}
    if pgid == _own_pgid():
        return {"reason": "refused", "detail": "pgid is own group",
                "members": [], "killable": False}
    if not instance:
        return {"reason": "identity_unknown", "detail": "no instance token",
                "members": [], "killable": False}

    ctx = _make_ctx(pgid, instance, sid,
                    leader_pid=record.get("leader_pid", 0),
                    leader_start=leader_start)
    reason, detail, members = _verify_group(ctx)
    return {"reason": reason, "detail": detail, "members": members,
            "killable": reason == "ok" and bool(members)}


def verify_token(pgid, instance, sid=None):
    """无登记文件时按实例令牌核验。语义与 verify 一致。"""
    if not isinstance(pgid, int) or pgid <= 1:
        return "identity_unknown", []
    if pgid == _own_pgid():
        return "identity_unknown", []
    if not instance:
        return "identity_unknown", []
    reason, _detail, members = _verify_group(_make_ctx(pgid, instance, sid))
    return reason, members

def main(argv):
    if len(argv) < 2:
        sys.stderr.write(__doc__)
        return 1
    cmd = argv[1]

    if cmd == "register":
        if len(argv) < 6:
            sys.stderr.write("register <record> <instance> <pgid> <leader_pid>\n")
            return 1
        rec = register(argv[2], argv[3], int(argv[4]), int(argv[5]))
        print(json.dumps(rec))
        return 0

    if cmd == "verify":
        if len(argv) < 3:
            return 1
        with open(argv[2]) as f:
            rec = json.load(f)
        out = verify(rec)
        print(json.dumps(out))
        return 0 if out["reason"] == "ok" else 1

    if cmd == "members":
        print(json.dumps(group_members(int(argv[2]))))
        return 0

    sys.stderr.write("unknown cmd\n")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
