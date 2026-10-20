#!/usr/bin/env python3
"""teslacam.py —— 统一入口（A8）

由 src/teslacam.sh 转调。命令：
  status | stop | start | maintain | clear-hold | clean-orphans | reassert

约束（设计修订稿 v6）：
  - 完整操作持生命周期锁，状态转换使用短控制锁
  - 启动/维护先经导出门禁；需要检查时先检查并提交凭据，再导出
  - stop **不删除 export_dirty**（停止成功 ≠ 确认干净）
  - 终止进程组前核验实例令牌；身份不明一律不杀
  - 每个命令只有一个 JSON 出口
"""

import json
import functools
import os
import signal
import subprocess
import sys
import time

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import lifecycle
import status_view

import gate                    # noqa: E402
import locks                   # noqa: E402
import services                # noqa: E402
import proc_identity as pi     # noqa: E402
import state_write as sw       # noqa: E402
import commit                  # noqa: E402

DEFAULTS = {
    "state": "/data/teslacam/state",
    "run": "/data/teslacam/run",
    "cfg": "/config/usb_gadget/g1",
    "img": "/data/teslacam/cam_disk.img",
    "msd": "/data/teslacam/bin/msd.sh",
    "checker": "/data/teslacam/bin/fsck_img.sh",
    "spawn": "",
    "mount_lock": "/data/local/tmp/teslacam-mount.lock",
    "sys_block": "/sys/block",
    "proc_mounts": "/proc/mounts",
    "services_conf": "/data/teslacam/services.json",
    "mnt": "/data/teslacam/mnt",
    "services": "msdwatch,pipeline,serve,frpc,transcode_queue",
}

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_INCOMPLETE = 3
EXIT_REFUSED = 4



def _control(c):
    return gate.control_lock(os.path.join(c["run"], "control.lock"))


def _phase(c, name):
    operation = c.get("_operation")
    if isinstance(operation, lifecycle.Operation):
        operation.phase(name)


def _coordinated(action):
    def decorate(body):
        @functools.wraps(body)
        def wrapped(c, out, *args):
            try:
                with lifecycle.Operation(c, action) as operation:
                    local = dict(c, _operation=operation)
                    out["op_id"] = operation.id
                    code = body(local, out, *args)
                    if code == EXIT_OK and not out.get("ok"):
                        code = EXIT_INCOMPLETE
                    summary = {k: out[k] for k in ("failed_at", "residual", "services_detail",
                                                   "unmanaged", "cleanup_failed") if k in out}
                    operation.finish(code, out.get("ok") is True, summary)
                    out["active_op"] = operation.record
                    return code
            except lifecycle.Refused as exc:
                out.update(ok=False, failed_at="lifecycle", reason=exc.reason,
                           detail=exc.detail, lifecycle_busy=exc.reason == "lifecycle_busy")
                return EXIT_INCOMPLETE
            except (OSError, TimeoutError) as exc:
                out.update(ok=False, failed_at="operation_state", detail=str(exc))
                return EXIT_INCOMPLETE
        return wrapped
    return decorate


def _helper_kwargs(c, lock_fd=None):
    fds = []
    operation = c.get("_operation")
    if operation is not None:
        fds.append(operation.fileno())
    if lock_fd is not None:
        fds.append(lock_fd)
    return {"pass_fds": tuple(dict.fromkeys(fds))} if fds else {}


def _service_names(c):
    names = {name.strip() for name in c.get("services", "").split(",") if name.strip()}
    path = c.get("services_conf")
    if path and os.path.exists(path):
        try:
            names.update(services.load(path))
        except (OSError, ValueError, TypeError, AttributeError):
            # A broken configuration must not prevent stopping known records.
            # The final status observation still reports the configuration error.
            pass
    for name in os.listdir(c["run"]):
        if name.endswith(".group"):
            names.add(name[:-6])
    return sorted(name for name in names if name not in (".", "..")
                  and "/" not in name and "\\" not in name)


def _failure(c, reason):
    with _control(c):
        commit.commit_failure(c["state"], reason)


def pdir(c, name):
    return os.path.join(c["state"], name)


def load_group(c, name):
    try:
        with open(os.path.join(c["run"], name + ".group")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def run_msd(c, action):
    try:
        r = subprocess.run(["sh", c["msd"], action], capture_output=True,
                           timeout=60, **_helper_kwargs(c))
        return r.returncode == 0, (r.stderr or b"").decode(errors="replace")[:200]
    except Exception as exc:
        return False, str(exc)



def run_checker(c, lock_fd=None):
    try:
        r = subprocess.run(["sh", c["checker"], c["img"]], capture_output=True,
                           timeout=3600, **_helper_kwargs(c, lock_fd))
        return r.returncode, (r.stderr or b"").decode(errors="replace")[:300]
    except subprocess.TimeoutExpired:
        return 124, "checker timeout"
    except Exception as exc:
        return 99, str(exc)




def wait_empty(pgid, tries=20, delay=0.2):
    empties = 0
    for _ in range(tries):
        if not pi.group_members(pgid):
            empties += 1
            if empties >= 2:
                return True
        else:
            empties = 0
        time.sleep(delay)
    return False


# ---------------- status ----------------

def status_body(c, out):
    out.update(status_view.build(c))
    return EXIT_OK



# ---------------- 共用：确保干净 ----------------

def _unescape(field):
    return locks._unescape(field)


def _mount_source(proc_mounts, mnt):
    """返回 (ok, source)。ok=False 表示挂载表不可读。"""
    try:
        with open(proc_mounts) as f:
            lines = f.read().splitlines()
    except OSError:
        return False, None
    for line in lines:
        fld = line.split()
        if len(fld) >= 2 and _unescape(fld[1]) == mnt:
            return True, _unescape(fld[0])
    return True, None


def cleanup_orphans(c, out, loop_names, lock_fd=None):
    """只清理**可证明属于本项目**的残留。

    前提（调用方保证）：已持有挂载锁；探测结果必须是 BUSY（UNKNOWN 一律不清）。
    规则：
      - 挂载点的来源设备必须属于本项目的 loop；来源不明则不卸载
      - 只 detach backing 是本项目镜像的 loop
      - 记录真实返回码；失败进 cleanup_failed，不冒充成功
    """
    notes, failed = [], []
    mnt = c.get("mnt", "/data/teslacam/mnt")
    proc_mounts = c.get("proc_mounts", "/proc/mounts")
    aliases = locks._device_aliases(loop_names)

    ok, src = _mount_source(proc_mounts, mnt)
    if not ok:
        # 挂载表读不到：无法确认归属，**任何资源都不动**
        failed.append("mounts_unreadable")
    elif src is not None and os.path.basename(src) not in aliases:
        # 挂载点被别人占用：不 umount，也**不 detach**（否则可能破坏对方）
        failed.append("mount_source_not_ours:%s" % src)
    else:
        if src is not None:
            for args in (["umount", mnt], ["umount", "-l", mnt]):
                try:
                    r = subprocess.run(args, capture_output=True, timeout=15,
                                       **_helper_kwargs(c, lock_fd))
                except Exception as e:              # noqa: BLE001
                    failed.append("%s:%s" % (args[0], e))
                    break
                if r.returncode == 0:
                    notes.append(" ".join(args))
                    break
                failed.append("%s rc=%d" % (args[0], r.returncode))

        # 只有确认挂载归属（或确认未挂载）后才 detach
        if not any(x.startswith("umount") for x in failed):
            for name in loop_names:
                dev = "/dev/block/" + name
                try:
                    r = subprocess.run(["losetup", "-d", dev],
                                       capture_output=True, timeout=15,
                                       **_helper_kwargs(c, lock_fd))
                except Exception as e:              # noqa: BLE001
                    failed.append("detach %s:%s" % (dev, e))
                    continue
                if r.returncode == 0:
                    notes.append("detach:" + dev)
                else:
                    failed.append("detach %s rc=%d" % (dev, r.returncode))

    if notes:
        out.setdefault("orphans_cleaned", []).extend(notes)
    if failed:
        out.setdefault("cleanup_failed", []).extend(failed)
    return (not failed), notes


def probe_image(c):
    """按配置提供的观察来源探测镜像占用。

    生产默认仍是 /sys/block 与 /proc/mounts；测试可通过 --sys-block /
    --proc-mounts 注入临时来源，避免触碰宿主资源观察。
    """
    return locks.probe_all(c["img"],
                           c.get("sys_block", "/sys/block"),
                           c.get("proc_mounts", "/proc/mounts"))


def ensure_clean(c, out, ignore=()):
    """Inspect under short control locks; hold the work lock through commit.

    Production callers hold the complete lifecycle lock. Never hold control
    while waiting for a work lock or for checker/helper processes.
    """
    out["ran_checker"] = False
    with _control(c):
        r = gate.evaluate(c["state"], c["cfg"], c["img"], ignore=ignore)
        if r["code"] == gate.C_OK:
            out["steps"].append("gate:ok(no-checker)")
            return True, r, False
        if r["code"] not in (gate.C_RECOVER, gate.C_NEED_CHECK):
            return False, r, False
        out["steps"].append("recover:op_inprogress" if r["code"] == gate.C_RECOVER
                            else "check:%s" % r.get("clean"))
        lun, _ = gate.read_lun(c["cfg"])
        out["steps"].append("lun_pre_check:%s" % lun)
        if lun != "detached":
            out.update(ok=False, failed_at="lun_precondition", lun=lun)
            return False, r, False
        if r["code"] == gate.C_NEED_CHECK:
            commit.begin_maintenance(c["state"], c["img"], op="maintenance")
    _phase(c, "checking_image")
    try:
        holder = locks.MountLock(c["mount_lock"], timeout=3.0, job_mode=True)
        holder.__enter__()
    except (TimeoutError, RuntimeError) as exc:
        out.update(ok=False, failed_at="exclusive_pre",
                   exclusive_state=locks.BUSY if isinstance(exc, TimeoutError) else locks.UNKNOWN,
                   reason="lock_held" if isinstance(exc, TimeoutError) else str(exc))
        return False, r, False
    try:
        with _control(c):
            check = gate.evaluate(c["state"], c["cfg"], c["img"], ignore=ignore)
            lun, _ = gate.read_lun(c["cfg"])
            if lun != "detached" or check["code"] not in (gate.C_RECOVER, gate.C_NEED_CHECK):
                out.update(ok=False, failed_at="precheck_revalidation", lun=lun)
                return False, check, False
        state, info = probe_image(c)
        out["steps"].append("probe_pre:%s" % state)
        if state != locks.FREE:
            out.update(ok=False, failed_at="exclusive_pre", exclusive_state=state,
                       reason=info.get("reason"), loops=info.get("loops"), mounts=info.get("mounts"))
            return False, r, False
        out["steps"].append("exclusive:ok")
        rc, detail = run_checker(c, holder.fileno())
        out["ran_checker"] = True
        out["steps"].append("checker:rc=%s" % rc)
        state, info = probe_image(c)
        out["steps"].append("after_checker:%s" % state)
        if state != locks.FREE:
            _failure(c, "residue_after_check")
            out.update(ok=False, failed_at="cleanup", exclusive_state=state, reason=info.get("reason"))
            return False, r, True
        with _control(c):
            lun, _ = gate.read_lun(c["cfg"])
            if lun != "detached":
                commit.commit_failure(c["state"], "lun_changed_during_check")
                out.update(ok=False, failed_at="lun_revalidation", lun=lun)
                return False, r, True
            res = commit.commit_success(c["state"], c["img"], rc, checks=["checker_rc=%s" % rc])
            if not res["ok"]:
                out.update(ok=False, failed_at="commit", commit=res, detail=res.get("detail", detail))
                return False, r, True
            out["steps"].extend(res["steps"])
            r2 = gate.evaluate(c["state"], c["cfg"], c["img"], ignore=ignore)
            out["steps"].append("regate:%d" % r2["code"])
        return r2["code"] == gate.C_OK, r2, True
    finally:
        holder.__exit__(None, None, None)


@_coordinated("start")
def start_body(c, out):
    _phase(c, "preflight")
    view = status_view.build(c)
    conflicts = [name for name, item in view["services"].items()
                 if item.get("registered") and item.get("alive") is not False]
    if conflicts or view["unmanaged"] or not view["inspection"]["complete"]:
        out.update(ok=False, failed_at="service_preflight", conflicts=conflicts,
                   unmanaged=view["unmanaged"], inspection=view["inspection"])
        return EXIT_INCOMPLETE
    conf_path = c.get("services_conf")
    conf = services.load(conf_path) if conf_path and os.path.exists(conf_path) else None
    with _control(c):
        if os.path.exists(pdir(c, "stop_requested")):
            sw.remove_durable(pdir(c, "stop_requested"))
            out["steps"].append("clear_stop_requested")
    ok, result, ran = ensure_clean(c, out)
    if not ok:
        out.update(ok=False, failed_at=out.get("failed_at", "gate"), gate_code=result["code"])
        return EXIT_REFUSED if result["code"] in (gate.C_FORBIDDEN, gate.C_STOPPING) else EXIT_INCOMPLETE
    _phase(c, "preparing_export")
    with _control(c):
        if conf is not None:
            try:
                sw.write_bytes(pdir(c, "control_managed"), b"1")
            except OSError as exc:
                out.update(ok=False, failed_at="control_managed", detail=str(exc))
                return EXIT_INCOMPLETE
        sw.write_bytes(pdir(c, "export_dirty"), b"1")
        out["steps"].append("export_dirty")
    ok, detail = run_msd(c, "up")
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
    out["steps"].append("msd_up")
    out["lun"] = lun
    if not ok or lun != "exported":
        _failure(c, "msd_up_failed")
        out.update(ok=False, failed_at="msd_up", lun=lun, detail=detail)
        return EXIT_INCOMPLETE
    _phase(c, "starting_services")
    if c.get("spawn"):
        # Keep the test compatibility route on the same tracked service path.
        conf = {"spawned": {"cmd": ["sh", "-c", c["spawn"]], "required": True,
                            "ready": {"type": "alive"}}}
    if conf is None:
        out.update(ok=True, lun=lun, services="none")
        return EXIT_OK
    ok, results, rollback = services.start_all(conf, c["run"], out=out,
                                               timeout=float(c.get("ready_timeout", 15)))
    out["services_detail"] = [{k: v for k, v in result.items()
                                if k not in ("record", "track", "instance")} for result in results]
    if not ok:
        out.update(ok=False, failed_at="services", rolled_back=rollback)
        return EXIT_INCOMPLETE
    out.update(ok=True, lun=lun, services=[r["name"] for r in results if r.get("state") == "running"])
    return EXIT_OK



# ---------------- maintain ----------------

@_coordinated("maintain")
def maintain_body(c, out):
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
    out["lun"] = lun
    if lun != "detached":
        out.update(ok=False, failed_at="precondition", detail="maintain requires stop first")
        return EXIT_REFUSED
    ok, result, ran = ensure_clean(c, out, ignore=("stop_requested",))
    out.update(ok=ok, checked=bool(ran), checker_ran=bool(ran))
    if not ok:
        out.update(failed_at=out.get("failed_at", "check"), gate_code=result["code"])
    return EXIT_OK if ok else EXIT_INCOMPLETE



# ---------------- stop ----------------

@_coordinated("stop")
def stop_body(c, out):
    _phase(c, "detaching_usb")
    with _control(c):
        sw.write_bytes(pdir(c, "stop_requested"), b"1")
        out["steps"].append("stop_requested")
    ok, detail = run_msd(c, "down")
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
    out["steps"].append("msd_down")
    if not ok or lun != "detached":
        _failure(c, "msd_down_failed")
        out.update(ok=False, failed_at="msd_down", lun=lun, detail=detail)
        return EXIT_INCOMPLETE
    _phase(c, "stopping_services")
    residual = []
    for name in _service_names(c):
        rec = load_group(c, name)
        if not isinstance(rec, dict):
            continue  # The final observation reports missing/invalid identities.
        verified = pi.verify(rec)
        if verified["reason"] == "empty":
            out["steps"].append("stopped:%s(empty)" % name)
            continue
        if verified["reason"] != "ok":
            residual.append({"name": name, "reason": verified["reason"]})
            continue
        try:
            os.killpg(rec["pgid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            residual.append({"name": name, "reason": "signal_failed", "detail": str(exc)})
            continue
        if not wait_empty(rec["pgid"]):
            residual.append({"name": name, "reason": "timeout"})
        else:
            out["steps"].append("stopped:%s" % name)
    _phase(c, "verifying_stopped")
    view = status_view.build(c)
    residual.extend(view["residue"])
    for name, item in view["services"].items():
        if item.get("registered") and item.get("alive") is not False:
            residual.append({"name": name, "reason": "service_still_present", "alive": item.get("alive")})
    if not view["inspection"]["complete"]:
        residual.append({"reason": "inspection_incomplete"})
    if view["unmanaged"]:
        residual.append({"reason": "unmanaged_instances"})
    try:
        with locks.MountLock(c["mount_lock"], timeout=0, job_mode=True):
            state, info = probe_image(c)
            if state != locks.FREE:
                residual.append({"reason": "image_resources_" + state, "resources": info})
    except (TimeoutError, RuntimeError, OSError):
        residual.append({"reason": "mount_work_not_quiescent"})
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
    out.update(lun=lun, unmanaged=view["unmanaged"], residual=residual)
    if residual or lun != "detached":
        _failure(c, "stop_incomplete")
        out.update(ok=False, failed_at="stop")
        return EXIT_INCOMPLETE
    out.update(ok=True, export_dirty_kept=True, stop_requested_kept=True)
    return EXIT_OK



# ---------------- clear-hold ----------------

@_coordinated("clear-hold")
def clear_hold_body(c, out, reason):
    _phase(c, "checking_hold_clear")
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
    view = status_view.build(c)
    live = [name for name, entry in view["services"].items()
            if entry.get("registered") and entry.get("alive") is not False]
    if lun != "detached" or live or view["unmanaged"] or not view["inspection"]["complete"]:
        out.update(ok=False, failed_at="clear_hold_precondition", lun=lun,
                   blocked=live, unmanaged=view["unmanaged"])
        return EXIT_INCOMPLETE
    with _control(c):
        sw.remove_durable(pdir(c, "export_forbidden.json"))
    out.update(ok=True, detail="hold cleared", reason=reason,
               note="dirty, stop intent and maintenance evidence retained")
    return EXIT_OK



# ---------------- clean-orphans（显式操作） ----------------

@_coordinated("clean-orphans")
def clean_orphans_body(c, out, confirm):
    if confirm is not True:
        out.update(ok=False, required="confirm", detail="requires exact --confirm flag")
        return EXIT_REFUSED
    with _control(c):
        lun, _ = gate.read_lun(c["cfg"])
        out["lun"] = lun
        if lun != "detached":
            out.update(ok=False, failed_at="lun_precondition")
            return EXIT_REFUSED
        if not os.path.exists(pdir(c, "stop_requested")):
            out.update(ok=False, failed_at="stop_required", detail="stop before explicit cleanup")
            return EXIT_REFUSED
    _phase(c, "explicit_cleanup")
    try:
        holder = locks.MountLock(c["mount_lock"], timeout=1, job_mode=True)
        holder.__enter__()
    except (TimeoutError, RuntimeError) as exc:
        out.update(ok=False, failed_at="exclusive", detail=str(exc))
        return EXIT_INCOMPLETE
    try:
        with _control(c):
            lun, _ = gate.read_lun(c["cfg"])
            if lun != "detached":
                out.update(ok=False, failed_at="lun_precondition", lun=lun)
                return EXIT_REFUSED
        state, info = probe_image(c)
        if state == locks.UNKNOWN:
            out.update(ok=False, failed_at="probe", exclusive_state=state, reason=info.get("reason"))
            return EXIT_INCOMPLETE
        if state == locks.FREE:
            out.update(ok=True, detail="no orphan")
            return EXIT_OK
        with _control(c):
            sw.write_bytes(pdir(c, "export_dirty"), b"1")
        ok, notes = cleanup_orphans(c, out, info.get("loops") or [], holder.fileno())
        state, info = probe_image(c)
        if not ok or state != locks.FREE:
            out.update(ok=False, failed_at="cleanup", exclusive_state=state, reason=info.get("reason"))
            return EXIT_INCOMPLETE
        out.update(ok=True, cleaned=notes)
        return EXIT_OK
    finally:
        holder.__exit__(None, None, None)


# ---------------- reassert（看门狗专用） ----------------

EXIT_SKIP = 5


def reassert_body(c, out):
    try:
        with lifecycle.ReassertGuard(c) as guard:
            local = dict(c, _operation=guard)
            with _control(local):
                for name, reason in (("stop_requested", "stopping"),
                                     ("export_forbidden.json", "hold"),
                                     ("op_inprogress.json", "op_in_progress")):
                    if os.path.exists(pdir(local, name)):
                        out.update(ok=False, skipped=True, reason=reason)
                        return EXIT_SKIP
                lun, _ = gate.read_lun(local["cfg"])
                if lun == "unknown" or (lun == "detached" and not os.path.exists(pdir(local, "export_dirty"))):
                    out.update(ok=False, skipped=True, reason="lun_unknown" if lun == "unknown" else "not_exported")
                    return EXIT_SKIP
            ok, detail = run_msd(local, "up")
            with _control(local):
                lun2, _ = gate.read_lun(local["cfg"])
            out.update(ok=bool(ok) and lun2 == "exported", action="reassert",
                       lun_before=lun, lun_after=lun2, detail=detail)
            return EXIT_OK if out["ok"] else EXIT_FAIL
    except lifecycle.Refused as exc:
        out.update(ok=False, skipped=True, reason=exc.reason, detail=exc.detail)
        return EXIT_SKIP



# ---------------- 单一出口 ----------------

def dispatch(cmd, c, reason):
    out = {"action": cmd, "ok": False, "steps": []}
    try:
        if cmd == "status":
            code = status_body(c, out)
            out["ok"] = True
        elif cmd == "stop":
            code = stop_body(c, out)
        elif cmd == "start":
            code = start_body(c, out)
        elif cmd == "maintain":
            code = maintain_body(c, out)
        elif cmd == "clean-orphans":
            code = clean_orphans_body(c, out, c.get("confirm") is True)
        elif cmd == "reassert":
            code = reassert_body(c, out)
        elif cmd == "clear-hold":
            code = clear_hold_body(c, out, reason)
        else:
            out.update({"ok": False, "detail": "unknown command"})
            code = EXIT_FAIL
    except Exception as e:                       # noqa: BLE001
        out.update({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        code = EXIT_FAIL
    out["exit_code"] = code
    print(json.dumps(out, ensure_ascii=False))
    return code


def parse(argv):
    c = dict(DEFAULTS)
    cmd, words = None, []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--confirm":
            c["confirm"] = True
            i += 1
            continue
        if a.startswith("--") and i + 1 < len(argv):
            # 连字符统一转下划线：--proc-root 落到 proc_root，
            # 否则配置键会静默失配并回落到默认值（真实 /proc、真实配置）
            c[a[2:].replace("-", "_")] = argv[i + 1]
            i += 2
        else:
            if cmd is None:
                cmd = a
            else:
                words.append(a)
            i += 1
    return cmd, c, " ".join(words)


def main(argv):
    cmd, c, reason = parse(argv)
    if not cmd:
        sys.stderr.write(__doc__)
        return EXIT_FAIL
    return dispatch(cmd, c, reason)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
