#!/usr/bin/env python3
"""P6: bounded, read-only observations. No command line or secret is returned.

This is an observation, not a lock-protected safety decision. Unknown facts stay
unknown. ``ready: process_alive`` deliberately makes no application-health claim.
"""
import errno
import ipaddress
import json
import os
import posixpath
import re
import socket
import time

import gate
import proc_identity as pi


def _json(path, limit=1048576):
    try:
        with open(path, "rb") as f:
            raw = f.read(limit + 1)
        if len(raw) > limit:
            return "unknown", None
        return "valid", json.loads(raw)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, UnicodeError):
        return "unknown", None


def _present(path):
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _stat(root, pid):
    with open(os.path.join(root, str(pid), "stat"), "rb") as f:
        raw = f.read(16384)
    at = raw.rfind(b")")
    if at < 0:
        raise ValueError("bad stat")
    fields = raw[at + 2:].split()
    return {"state": fields[0].decode("ascii"), "pgrp": int(fields[2]),
            "sid": int(fields[3]), "starttime": int(fields[19])}


def _scan_proc(c):
    """An unreadable or surviving, malformed PID never means an empty group."""
    root = c.get("proc_root", "/proc")
    out = {"items": {}, "complete": True, "stat_complete": True,
           "cmdline_complete": True, "scanned": 0, "reasons": []}
    deadline = time.monotonic() + min(2.0, max(0.01, float(c.get("status_scan_seconds", 0.75))))
    limit = min(20000, max(1, int(c.get("status_scan_limit", 10000))))
    try:
        entries = os.scandir(root)
        with entries:
            for ent in entries:
                if not ent.name.isdigit():
                    continue
                if out["scanned"] >= limit or time.monotonic() >= deadline:
                    out["stat_complete"] = out["cmdline_complete"] = False
                    out["reasons"].append("scan_bound_reached")
                    break
                pid = int(ent.name)
                out["scanned"] += 1
                try:
                    st = _stat(root, pid)
                except (OSError, ValueError, IndexError, UnicodeError):
                    if _present(os.path.join(root, ent.name)) is not False:
                        out["stat_complete"] = False
                        out["reasons"].append("pid_stat_unknown")
                    continue
                if st["state"] in ("Z", "X", "x"):
                    continue
                st["argv"] = None
                try:
                    with open(os.path.join(root, ent.name, "cmdline"), "rb") as f:
                        raw = f.read(131073)
                    if len(raw) > 131072:
                        raise ValueError("cmdline too large")
                    st["argv"] = [x.decode("utf-8", "surrogateescape")
                                  for x in raw.rstrip(b"\0").split(b"\0") if x]
                    after = _stat(root, pid)
                    if any(after[k] != st[k] for k in ("starttime", "pgrp", "sid")):
                        st["argv"] = None
                        out["stat_complete"] = False
                        raise ValueError("pid changed during observation")
                except (OSError, ValueError, IndexError, UnicodeError):
                    if _present(os.path.join(root, ent.name)) is False:
                        # 已确认消失：不把旧条目留在本次观察里
                        continue
                    out["cmdline_complete"] = False
                    out["reasons"].append("pid_cmdline_unknown")
                out["items"][pid] = st
    except OSError:
        out["stat_complete"] = out["cmdline_complete"] = False
        out["reasons"].append("proc_unreadable")
    out["complete"] = out["stat_complete"] and out["cmdline_complete"]
    out["reasons"] = sorted(set(out["reasons"]))
    return out


def _valid_record(rec):
    return (isinstance(rec, dict) and isinstance(rec.get("instance"), str)
            and bool(rec["instance"]) and all(type(rec.get(k)) is int for k in
                ("pgid", "sid", "leader_pid", "leader_starttime"))
            and rec["pgid"] > 1 and rec["sid"] > 0 and rec["leader_pid"] > 0)


def _departed(pid, rec, root="/proc"):
    """确认该 PID 已不属于本次观察：不存在、死亡/僵尸、或已离开目标组。"""
    try:
        st = _stat(root, pid)
    except (OSError, ValueError, IndexError, UnicodeError):
        return _present(os.path.join(root, str(pid))) is False
    if st["state"] in ("Z", "X", "x"):
        return True
    if st["pgrp"] != rec["pgid"]:
        return True
    return False


def _bind_member(pid, rec, root):
    """为已核验成员补快照，并证明这份快照属于刚核验的同一实例。

    返回 (state, snapshot)：ok / gone / bad。
    """
    E = (OSError, ValueError, IndexError, UnicodeError)

    def read():
        return _stat(root, pid)

    def check(snapshot, stage):
        if snapshot["state"] in ("Z", "X", "x"):
            return "gone", "dead_" + stage
        if snapshot["pgrp"] != rec["pgid"]:
            return "gone", "left_group_" + stage
        if rec.get("sid") is not None and snapshot["sid"] != rec["sid"]:
            return "gone", "left_session_" + stage
        if pid == rec.get("leader_pid") \
                and snapshot["starttime"] != rec.get("leader_starttime"):
            return "bad", "leader_anchor_" + stage
        return None, ""

    try:
        before = read()
    except E:
        return ("gone", None) if _departed(pid, rec, root) else ("bad", None)
    state, _why = check(before, "pre")
    if state:
        return state, None

    # 令牌读取夹在两次 stat 之间：读到的令牌必须属于同一实例
    try:
        with open(os.path.join(root, str(pid), "environ"), "rb") as f:
            raw = f.read(1048577)
        needle = (pi.TOKEN_KEY + "=" + rec["instance"]).encode()
        if len(raw) > 1048576 or needle not in raw.split(b"\0"):
            return "bad", None
    except E:
        return ("gone", None) if _departed(pid, rec, root) else ("bad", None)

    try:
        after = read()
    except E:
        return ("gone", None) if _departed(pid, rec, root) else ("bad", None)
    state, _why = check(after, "post")
    if state:
        return state, None
    if any(after[k] != before[k] for k in ("starttime", "pgrp", "sid")):
        return "bad", None                       # environ 期间实例已变化
    return "ok", after


def _verify_record(rec, root, scan):
    members = sorted(pid for pid, st in scan["items"].items()
                     if st["pgrp"] == rec["pgid"])
    if not scan["stat_complete"]:
        return {"reason": "identity_unknown", "members": members, "complete": False}

    if root == "/proc":
        try:
            result = pi.verify(rec)
        except (OSError, ValueError, IndexError, TypeError):
            return {"reason": "identity_unknown", "members": members, "complete": False}
        got = sorted(result.get("members") or [])
        if got != members:
            # 名单不同：逐个复核"本次扫描有、核验结果没有"的成员，
            # 确认离开才接受差异；仍在组内则判未知。
            for pid in set(members) - set(got):
                if not _departed(pid, rec, root):
                    return {"reason": "identity_unknown",
                            "members": got, "complete": False}

        reason = result.get("reason", "identity_unknown")
        if reason == "empty":
            return {"reason": "empty", "members": [], "complete": True}
        if reason != "ok":
            return {"reason": reason, "members": got, "complete": False}

        # 为每个"已核验成员"补齐**与本次核验绑定**的实例快照：
        # 重新核验实例身份，已离组/死亡剔除，变化或不可确认判未知。
        survivors = []
        for pid in got:
            state, snap = _bind_member(pid, rec, root)
            if state == "ok":
                scan["items"][pid] = snap
                survivors.append(pid)
            elif state == "gone":
                scan["items"].pop(pid, None)
                continue
            else:
                return {"reason": "identity_unknown",
                        "members": survivors, "complete": False}

        if not survivors:
            return {"reason": "empty", "members": [], "complete": True}
        return {"reason": "ok", "members": survivors, "complete": True}

    # 夹具 /proc 根：同一套规则，明确剔除已确认离开的成员
    survivors = []
    for pid in members:
        if _departed(pid, rec, root):
            continue
        try:
            with open(os.path.join(root, str(pid), "environ"), "rb") as f:
                raw = f.read(1048577)
            now = _stat(root, pid)
            old = scan["items"][pid]
            needle = (pi.TOKEN_KEY + "=" + rec["instance"]).encode()
            if (len(raw) > 1048576 or needle not in raw.split(b"\0")
                    or now["sid"] != rec["sid"]
                    or any(now[k] != old[k] for k in ("starttime", "pgrp", "sid"))
                    or (pid == rec["leader_pid"]
                        and now["starttime"] != rec["leader_starttime"])):
                raise ValueError("identity not established")
        except (OSError, ValueError, IndexError, UnicodeError):
            if _departed(pid, rec, root):
                continue          # 读取期间确认离开：剔除，不算未知
            return {"reason": "identity_unknown", "members": survivors,
                    "complete": False}
        survivors.append(pid)

    if not survivors:
        return {"reason": "empty", "members": [], "complete": True}
    return {"reason": "ok", "members": survivors, "complete": True}

def _path(value):
    return posixpath.normpath(value) if isinstance(value, str) and value.startswith("/") else None


def _known_paths(c, conf):
    paths = {}
    interpreters = {"sh", "bash", "dash", "env", "busybox", "perl", "ruby", "node"}
    for name, spec in conf.items():
        cmd = spec.get("cmd") or []
        if not isinstance(cmd, list):
            continue
        for i, arg in enumerate(cmd):
            p = _path(arg)
            interpreter = (p and (posixpath.basename(p) in interpreters or
                           re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", posixpath.basename(p))))
            if p and ((i == 0 and not interpreter)
                      or p.endswith((".py", ".sh"))):
                paths.setdefault(p, set()).add(name)
    bindir = posixpath.dirname(c.get("msd", "/data/teslacam/bin/msd.sh"))
    for name, filename in (("pipeline", "pipeline.sh"), ("serve", "serve.py"),
                           ("msdwatch", "msdwatch.sh"), ("frpc", "frpc"),
                           ("transcode_queue", "transcode_queue.sh"),
                           ("collector", "collect.sh"), ("live_collector", "live_collect.sh"),
                           ("encoder", "transcode.sh"), ("indexer", "index.py")):
        paths.setdefault(posixpath.join(bindir, filename), set()).add(name)
    return paths


def _socket_inodes(root, members, scan):
    found = set()
    inspected = 0
    deadline = time.monotonic() + 0.25
    try:
        for pid in members:
            before = _stat(root, pid)
            if pid not in scan["items"] or before["starttime"] != scan["items"][pid]["starttime"]:
                return None
            folder = os.path.join(root, str(pid), "fd")
            with os.scandir(folder) as files:
                for ent in files:
                    inspected += 1
                    if inspected > 4096 or time.monotonic() >= deadline:
                        return None
                    target = os.readlink(ent.path)
                    if target.startswith("socket:[") and target.endswith("]"):
                        found.add(target[8:-1])
            after = _stat(root, pid)
            if any(after[k] != before[k] for k in ("starttime", "pgrp", "sid")):
                return None
        return found
    except (OSError, ValueError, KeyError, IndexError):
        return None


def _tcp_owner(root, host, port, members, scan):
    """True only when all compatible listening endpoints belong to this group."""
    own = _socket_inodes(root, members, scan)
    if own is None:
        return None
    ipv = ipaddress.ip_address(host)
    if ipv.version == 4:
        exact = ipv.packed[::-1].hex().upper()
        tables = [("tcp", {exact, "00000000"})]
    else:
        exact = b"".join(ipv.packed[i:i+4][::-1] for i in range(0, 16, 4)).hex().upper()
        tables = [("tcp6", {exact, "0" * 32})]
    listeners = set()
    try:
        for filename, addresses in tables:
            with open(os.path.join(root, "net", filename)) as f:
                lines = f.read(2097153)
            if len(lines) > 2097152:
                return None
            for line in lines.splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    return None
                address, p = fields[1].split(":")
                if int(p, 16) == port and address.upper() in addresses and fields[3] == "0A":
                    listeners.add(fields[9])
    except (OSError, ValueError, IndexError):
        return None
    if not listeners:
        return False
    return True if listeners <= own else None


def _tcp_connect(host, port):
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return True
    except OSError as e:
        if e.errno == errno.ECONNREFUSED:
            return False
        return None


def _ready(spec, entry, root, scan):
    rule = spec.get("ready") or {"type": "alive"}
    if not isinstance(rule, dict):
        return None, "ready_rule_unknown", "unknown"
    typ = rule.get("type", "alive")
    if entry["identity"] != "verified":
        return None if entry["alive"] is not False else False, "identity_not_verified", typ
    if typ == "alive":
        return entry["alive"], "process_alive", "process_alive"
    if typ != "tcp":
        return None, "ready_rule_unknown", "unknown"
    host = rule.get("host", "127.0.0.1")
    try:
        ip = ipaddress.ip_address(host)
        port = int(rule["port"])
        if not ip.is_loopback or not 0 < port < 65536:
            raise ValueError("local endpoint required")
    except (ValueError, TypeError, KeyError):
        return None, "nonlocal_or_invalid_probe", "tcp_local_owned"
    owner = _tcp_owner(root, host, port, entry["members"], scan)
    if owner is None:
        return None, "listener_owner_unknown", "tcp_local_owned"
    if not owner:
        return False, "not_listening", "tcp_local_owned"
    connected = _tcp_connect(host, port)
    return connected, ("tcp_local_owned" if connected else
                       "tcp_refused" if connected is False else "tcp_probe_unknown"), "tcp_local_owned"


def _active(c):
    try:
        import lifecycle
        state, rec = lifecycle.read_active(c)
    except (ImportError, OSError, ValueError, TypeError, AttributeError):
        return "unknown", None
    if state != "valid":
        return state, None
    if not isinstance(rec, dict):
        return "unknown", None
    keys = ("id", "op", "kind", "action", "phase", "status", "exit_code", "started_at", "updated_at", "finished_at", "recovery_required")
    safe = {k: rec[k] for k in keys if k in rec and
            (isinstance(rec[k], (str, int, float, bool)) or rec[k] is None)}
    owner = rec.get("owner")
    if isinstance(owner, dict):
        safe["owner"] = {k: owner[k] for k in ("pid", "pgid", "starttime") if type(owner.get(k)) is int}
    summary = rec.get("summary")
    if isinstance(summary, dict):
        # Historical exception messages may contain command arguments. Retain
        # machine reason codes and numeric identities, never raw error details.
        short = {}
        if isinstance(summary.get("failed_at"), str):
            short["failed_at"] = _reason_code(summary["failed_at"])
        for key in ("services_detail", "residual"):
            if isinstance(summary.get(key), list):
                short[key] = []
                for item in summary[key][:256]:
                    if not isinstance(item, dict):
                        continue
                    cleaned = {k: item[k] for k in ("name", "state") if isinstance(item.get(k), str)}
                    for k in ("pgid", "pid", "alive", "registered"):
                        if type(item.get(k)) in (int, bool):
                            cleaned[k] = item[k]
                    if isinstance(item.get("reason"), str):
                        cleaned["reason"] = _reason_code(item["reason"])
                    short[key].append(cleaned)
        safe["summary"] = short
    return "valid", safe


def _reason_code(value):
    head = value.split(":", 1)[0]
    return head if re.fullmatch(r"[a-zA-Z0-9_]{1,80}", head) else "operation_failed"


def build(c):
    """Return an additive JSON view; never create/remove any filesystem object."""
    failures, residue, reasons = [], [], []
    state = c["state"]
    flags = {k: _present(os.path.join(state, filename)) for k, filename in (
        ("stop_requested", "stop_requested"), ("op_inprogress", "op_inprogress.json"),
        ("export_dirty", "export_dirty"), ("hold", "export_forbidden.json"))}
    state_complete = all(v is not None for v in flags.values())
    for k in ("stop_requested", "op_inprogress", "hold"):
        if flags[k] is True:
            reasons.append(k)
        elif flags[k] is None:
            reasons.append(k + "_unknown")
    lun, _ = gate.read_lun(c["cfg"])
    if lun == "unknown":
        reasons.append("lun_unknown")
        state_complete = False
    if flags["export_dirty"] is None:
        reasons.append("export_dirty_unknown")
    if lun == "detached":
        clean, why = gate.check_clean(state, c["img"])
        if not clean:
            reasons.append("requires_check:" + why)
    active_state, active = _active(c)
    if active_state == "unknown":
        reasons.append("active_op_unknown")
        state_complete = False
    elif active_state == "valid":
        if active.get("status") != "finished" or active.get("exit_code") != 0:
            reasons.append("active_op_" + (active.get("status") if active.get("status") in
                                           ("running", "incomplete") else "unknown"))
        if active.get("recovery_required"):
            reasons.append("active_op_recovery_required")
        summary = active.get("summary") or {}
        if summary.get("failed_at"):
            failures.append({"source": "last_operation", "reason": summary["failed_at"]})
        for item in summary.get("services_detail", []):
            if item.get("state") == "failed":
                failures.append(dict(item, source="last_operation_service"))
        for item in summary.get("residual", []):
            residue.append(dict(item, source="last_operation"))

    conf_state, conf = _json(c.get("services_conf", "/data/teslacam/services.json"))
    config_complete = conf_state != "unknown"
    if conf_state == "valid" and isinstance(conf, dict):
        conf = {k: v for k, v in conf.items() if isinstance(k, str) and isinstance(v, dict)}
    elif conf_state == "missing":
        conf = {}
    else:
        conf, config_complete = {}, False
        failures.append({"source": "services_config", "reason": "config_unknown"})
    names = set(conf)
    legacy = c.get("services", "")
    names.update(s.strip() for s in legacy.split(",") if s.strip())
    records_complete = True
    try:
        with os.scandir(c["run"]) as files:
            for i, ent in enumerate(files):
                if i >= 2048:
                    records_complete = False
                    break
                if ent.name.endswith(".group"):
                    names.add(ent.name[:-6])
    except OSError:
        records_complete = False
    names = {n for n in names if n and n not in (".", "..") and "/" not in n and "\\" not in n}
    scan = _scan_proc(c)
    paths = _known_paths(c, conf)
    matches = {}
    for pid, st in scan["items"].items():
        matched = set()
        for arg in st["argv"] or []:
            matched.update(paths.get(_path(arg), ()))
        if matched:
            matches[pid] = matched
    result, covered = {}, set()
    service_inspection_complete = True
    for name in sorted(names):
        spec = conf.get(name, {})
        enabled = spec.get("enabled", True) if name in conf else None
        rs, rec = _json(os.path.join(c["run"], name + ".group"), 65536)
        entry = {"enabled": enabled, "registered": rs != "missing", "identity": "unregistered",
                 "alive": None, "ready": None, "reason": "unregistered", "pgid": None,
                 "members": [], "state": "unregistered", "ready_kind": None}
        suspicious = sorted(pid for pid, ns in matches.items() if name in ns)
        if rs == "valid" and _valid_record(rec):
            entry["pgid"] = rec["pgid"]
            v = _verify_record(rec, c.get("proc_root", "/proc"), scan)
            vr = v["reason"]
            entry.update(members=v["members"], state=vr, reason=vr,
                         identity="verified" if vr == "ok" else "empty" if vr == "empty" else "unknown",
                         alive=True if v["members"] else False if vr == "empty" else None)
            if vr == "ok":
                covered.update(v["members"])
                entry["ready"], entry["reason"], entry["ready_kind"] = _ready(
                    spec, entry, c.get("proc_root", "/proc"), scan)
                if enabled is not False and entry["ready"] is None:
                    service_inspection_complete = False
            elif vr == "empty":
                entry.update(ready=False, reason="group_empty")
            else:
                records_complete = False
                residue.append({"name": name, "pgid": rec["pgid"], "members": v["members"],
                                "reason": "identity_unknown", "alive": entry["alive"]})
        elif rs != "missing":
            entry.update(identity="unknown", state="identity_unknown", reason="malformed_or_unreadable_record")
            records_complete = False
            failures.append({"source": "registration", "name": name, "reason": entry["reason"]})
            residue.append({"name": name, "reason": entry["reason"], "alive": None})
        elif suspicious:
            entry.update(alive=True, members=suspicious, reason="observed_unregistered")
        if enabled is False:
            entry.update(state="disabled", ready=False if entry["alive"] is False else None)
            if entry["alive"] is True:
                residue.append({"name": name, "reason": "disabled_but_alive", "members": entry["members"], "alive": True})
        result[name] = entry
    # 只报告仍在本次观察快照中的条目：核验阶段已确认退出并被移除的 PID
    # 不再出现在 unmanaged 里，也不能让整次 status 因 KeyError 失败。
    unmanaged = [{"pid": pid, "pgid": scan["items"][pid]["pgrp"],
                  "starttime": scan["items"][pid]["starttime"], "services": sorted(ns),
                  "reason": "project_argv_match_identity_unverified"}
                 for pid, ns in sorted(matches.items())
                 if pid not in covered and pid in scan["items"]]
    inspection = {"complete": state_complete and config_complete and records_complete and scan["complete"] and service_inspection_complete,
                  "state_complete": state_complete, "config_complete": config_complete,
                  "services_complete": service_inspection_complete,
                  "registrations_complete": records_complete,
                  "proc": {k: v for k, v in scan.items() if k != "items"},
                  "snapshot": "best_effort_non_atomic", "side_effects": False}
    return {"lun": lun, "permission": "forbidden" if reasons else "allowed",
            "permission_reasons": reasons, "stop_requested": flags["stop_requested"],
            "op_inprogress": flags["op_inprogress"], "export_dirty": flags["export_dirty"],
            "active_op": active, "active_op_state": active_state, "services": result,
            "unmanaged": unmanaged, "residue": residue, "failures": failures,
            "inspection": inspection}
