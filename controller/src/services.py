#!/usr/bin/env python3
"""services.py —— 逐服务启动、登记、就绪、回滚（P4）

设计要点：
  - 每个服务有自己的命令/环境/日志/就绪判据，不再用一条通用 spawn
  - 启动即注入实例令牌并登记 run/<name>.group（供 proc_identity 核验）
  - "进程存在" ≠ "服务就绪"：alive 与 tcp 两种判据分开
  - 必需服务失败 → 整体失败；回滚只针对**本次尝试**启动的实例（按令牌核验）
  - 鉴权值只从文件读入环境，不进入任何输出或记录
"""

import json
import os
import socket
import subprocess
import time
import uuid

import proc_identity as pi

DEFAULT_CONFIG = "/data/teslacam/services.json"


def load(path=DEFAULT_CONFIG):
    """加载服务配置。非 dict 条目（如 _comment）一律忽略。"""
    with open(path) as f:
        raw = json.load(f)
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _env_for(spec, instance, base_env=None):
    env = dict(base_env if base_env is not None else os.environ)
    for k, v in (spec.get("env") or {}).items():
        env[str(k)] = str(v)
    # 从文件读敏感值，不写入任何记录。读取失败视为该服务不可启动。
    missing = []
    for k, p in (spec.get("env_files") or {}).items():
        try:
            with open(p) as f:
                env[str(k)] = f.read().strip()
        except OSError:
            missing.append(str(k))
    env[pi.TOKEN_KEY] = instance
    return env, missing


def _alive(pgid):
    return bool(pi.group_members(pgid))


def _wait_ready(spec, pgid, timeout):
    """返回 (ok, reason)。"""
    ready = spec.get("ready") or {"type": "alive"}
    t = float(ready.get("timeout", timeout))
    deadline = time.time() + t

    if ready.get("type") == "tcp":
        host = ready.get("host", "127.0.0.1")
        port = int(ready.get("port"))
        while time.time() < deadline:
            if not _alive(pgid):
                return False, "exited_before_ready"
            try:
                with socket.create_connection((host, port), timeout=1.0):
                    return True, "ready"
            except OSError:
                time.sleep(0.2)
        return False, "tcp_timeout"

    # alive：给一小段启动时间，再确认进程组仍在
    while time.time() < deadline:
        if not _alive(pgid):
            return False, "exited_early"
        time.sleep(0.1)
        if _alive(pgid):
            return True, "ready"
    return False, "alive_timeout"


def start_one(spec, name, run_dir, base_env=None, timeout=15.0):
    """启动单个服务。返回记录 dict（含 ok/reason）。"""
    if spec.get("enabled") is False:
        return {"name": name, "state": "disabled", "ok": True}

    cmd = spec.get("cmd")
    if not cmd:
        return {"name": name, "state": "failed", "ok": False, "reason": "no_cmd"}

    instance = str(uuid.uuid4())
    env, env_missing = _env_for(spec, instance, base_env)
    if env_missing:
        return {"name": name, "state": "failed", "ok": False,
                "reason": "env_file_missing:%s" % ",".join(env_missing)}

    log_path = spec.get("log")
    try:
        if log_path:
            fh = open(log_path, "ab")
        else:
            fh = subprocess.DEVNULL
    except OSError:
        fh = subprocess.DEVNULL

    try:
        p = subprocess.Popen(cmd, env=env, start_new_session=True,
                             stdout=fh, stderr=fh, cwd=spec.get("cwd"))
    except OSError as e:
        return {"name": name, "state": "failed", "ok": False,
                "reason": "spawn_failed:%s" % e}
    finally:
        try:
            if log_path:
                fh.close()
        except Exception:      # noqa: BLE001
            pass

    time.sleep(0.15)
    try:
        rec = pi.register(os.path.join(run_dir, name + ".group"),
                          instance, p.pid, p.pid)
    except Exception as e:     # noqa: BLE001
        # 登记失败也要保留本次尝试的追踪信息（pgid + 令牌可用于核验）
        return {"name": name, "state": "failed", "ok": False,
                "reason": "register_failed:%s" % e,
                "pgid": p.pid, "instance": instance, "registered": False,
                "track": {"pgid": p.pid, "instance": instance}}

    ok, why = _wait_ready(spec, p.pid, timeout)
    return {"name": name, "state": "running" if ok else "failed",
            "ok": ok, "reason": why, "pgid": p.pid, "record": rec}


def rollback(started, run_dir, wait=5.0):
    """只回收本次尝试启动的实例（令牌核验通过才终止），并等待确认退出。"""
    done = []
    for item in started:
        rec = item.get("record")
        if rec:
            v = pi.verify(rec)
            pgid = rec["pgid"]
        else:
            track = item.get("track") or {}
            pgid = track.get("pgid")
            if not pgid:
                continue
            reason, _m = pi.verify_token(pgid, track.get("instance"))
            v = {"reason": reason}
        if v["reason"] == "empty":
            done.append({"name": item["name"], "killed": False, "exited": True,
                         "note": "already gone"})
            continue
        if v["reason"] != "ok":
            done.append({"name": item["name"], "killed": False, "exited": False,
                         "note": "identity_unknown, left running"})
            continue
        try:
            os.killpg(pgid, 15)
        except OSError as e:
            done.append({"name": item["name"], "killed": False, "exited": False,
                         "note": "kill_failed:%s" % e})
            continue
        deadline = time.time() + wait
        while time.time() < deadline and pi.group_members(pgid):
            time.sleep(0.1)
        still = bool(pi.group_members(pgid))
        done.append({"name": item["name"], "killed": True, "exited": not still,
                     "note": "" if not still else "still_running_after_term"})
    return done


def start_all(config, run_dir, out=None, timeout=15.0, base_env=None):
    """按配置启动全部服务。返回 (ok, results, rollback_info)。"""
    results = []
    started = []
    ok_all = True

    for name, spec in config.items():
        r = start_one(spec, name, run_dir, base_env, timeout)
        results.append(r)
        if r["state"] == "disabled":
            continue

        # 只要派生了进程就纳入本次回滚对象（record 或登记失败时的 track）
        if r.get("record") or r.get("track"):
            started.append(r)

        if r["ok"]:
            continue

        if spec.get("required", False):
            ok_all = False
            break
        # 可选服务失败：**回收该实例**，留下明确的降级记录
        if r.get("record") or r.get("track"):
            r["rollback"] = rollback([r], run_dir)

    rb = None
    if not ok_all:
        rb = rollback(started, run_dir)

    return ok_all, results, rb
