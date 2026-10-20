#!/usr/bin/env python3
"""Serialize complete controller operations and record their durable identity.

The operation lock is separate from the short state/control lock and from the
filesystem check's op_inprogress.json. Only finite helper jobs may inherit its
descriptor; ordinary long-lived services must not inherit it.
"""

import contextlib
import json
import math
import os
import time
import uuid

import gate
import locks
import proc_identity as pi
import state_write as sw


ACTIONS = frozenset(("start", "stop", "maintain", "clear-hold", "clean-orphans"))
STATUSES = frozenset(("running", "finished", "incomplete"))


class Refused(RuntimeError):
    """A lifecycle precondition could not be established safely."""

    def __init__(self, reason, detail=""):
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _timestamp(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def validate(record):
    """Return False for malformed or unsupported operation records."""
    if not isinstance(record, dict):
        return False
    if type(record.get("schema")) is not int or record["schema"] != 1:
        return False
    if (not _uuid(record.get("id")) or not isinstance(record.get("op"), str)
            or record["op"] not in ACTIONS):
        return False
    phase = record.get("phase")
    if not isinstance(phase, str) or not phase or len(phase) > 160:
        return False
    if not isinstance(record.get("status"), str) or record["status"] not in STATUSES:
        return False
    owner = record.get("owner")
    if not isinstance(owner, dict):
        return False
    if not _integer(owner.get("pid"), 2) or not _integer(owner.get("starttime"), 1):
        return False
    if not _uuid(owner.get("boot_id")):
        return False
    if not _timestamp(record.get("started_at")) or not _timestamp(record.get("updated_at")):
        return False
    if record["updated_at"] < record["started_at"]:
        return False
    code = record.get("exit_code")
    if "exit_code" not in record:
        return False
    if record["status"] == "running":
        if code is not None or ("ok" in record and record["ok"] is not False):
            return False
    else:
        if not _integer(code) or type(record.get("ok")) is not bool:
            return False
        if record["status"] == "finished" and (code != 0 or not record["ok"]):
            return False
        if record["status"] == "incomplete" and code == 0 and record["ok"]:
            return False
    if "previous" in record and not isinstance(record["previous"], dict):
        return False
    if "recovery_required" in record and type(record["recovery_required"]) is not bool:
        return False
    return True


def _path(c):
    return os.path.join(c["state"], "active_op.json")


def read_active(c):
    """Return (missing|valid|unknown, record_or_None), without interpreting age.

    This reader does not take a lock. Writers replace the file atomically; callers
    making decisions must hold the control lock in addition to their work lock.
    """
    try:
        with open(_path(c), encoding="utf-8") as stream:
            record = json.load(stream)
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError, UnicodeError):
        return "unknown", None
    if not validate(record):
        return "unknown", None
    return "valid", record


def _boot_id():
    """Return this boot's bounded, validated identity; unreadable is unknown."""
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as stream:
        raw = stream.read(128)
    if len(raw) == 128:
        raise ValueError("current boot identity oversized")
    boot_id = raw.strip()
    if not _uuid(boot_id):
        raise ValueError("current boot identity unavailable")
    return boot_id


def _owner():
    st = pi._stat(os.getpid())
    boot_id = _boot_id()
    result = {"pid": os.getpid(), "starttime": st["starttime"], "boot_id": boot_id}
    if not _integer(result["pid"], 2) or not _integer(result["starttime"], 1) or not _uuid(boot_id):
        raise OSError("controller identity unavailable")
    return result


def _write(c, record):
    if not validate(record):
        raise ValueError("invalid operation record")
    raw = json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
    sw.write_bytes(_path(c), raw)


def _acquire(c, timeout):
    try:
        holder = locks.MountLock(os.path.join(c["run"], "lifecycle.lock"),
                                 timeout=timeout, job_mode=True)
        holder.__enter__()
    except TimeoutError as exc:
        raise Refused("lifecycle_busy", "another lifecycle operation or helper is still active") from exc
    except (OSError, RuntimeError) as exc:
        raise Refused("lifecycle_unknown", "cannot establish lifecycle lock: %s" % exc) from exc
    return holder


@contextlib.contextmanager
def _control(c, timeout=10.0):
    try:
        guard = gate.control_lock(os.path.join(c["run"], "control.lock"), timeout=timeout)
        guard.__enter__()
    except TimeoutError as exc:
        raise Refused("control_busy", "state control lock is busy") from exc
    except (OSError, RuntimeError) as exc:
        raise Refused("control_unknown", "cannot establish state control lock: %s" % exc) from exc
    try:
        yield
    finally:
        guard.__exit__(None, None, None)


class Operation:
    """Hold the complete operation lock and maintain one durable operation ID."""

    def __init__(self, c, action, timeout=0):
        if action not in ACTIONS:
            raise ValueError("unsupported lifecycle action")
        self.c = c
        self.action = action
        self.timeout = timeout
        self.id = None
        self.record = None
        self._holder = None
        self._finished = False

    def _control(self):
        return _control(self.c)

    def __enter__(self):
        self._holder = _acquire(self.c, self.timeout)
        try:
            with self._control():
                state, previous = read_active(self.c)
                unfinished = state == "valid" and previous["status"] != "finished"
                recovery = state == "valid" and previous.get("recovery_required", False)
                # A failed stop can expose resources needing explicit cleanup.
                # Cleanup may repair them, but cannot certify that stop finished.
                cleanup_repair = (self.action == "clean-orphans" and state == "valid"
                                  and previous["op"] == "stop"
                                  and previous["status"] == "incomplete")
                if (self.action != "stop" and not cleanup_repair
                        and (state == "unknown" or unfinished or recovery)):
                    reason = ("active_op_unknown" if state == "unknown" else
                              "recovery_required" if recovery else "active_op_incomplete")
                    raise Refused(reason, "previous operation is not confirmed complete; run an explicit stop first")
                try:
                    owner = _owner()
                except (OSError, ValueError, IndexError, KeyError) as exc:
                    raise Refused("owner_unknown", "cannot establish controller identity") from exc
                now = time.time()
                self.id = str(uuid.uuid4())
                self.record = {
                    "schema": 1, "id": self.id, "op": self.action,
                    "phase": "begin", "status": "running", "owner": owner,
                    "started_at": now, "updated_at": now, "exit_code": None,
                    "recovery_required": bool(recovery or unfinished or state == "unknown"),
                }
                if previous is not None:
                    self.record["previous"] = {
                        "id": previous["id"], "op": previous["op"],
                        "phase": previous["phase"], "status": previous["status"],
                        "exit_code": previous["exit_code"],
                    }
                elif state == "unknown":
                    self.record["previous"] = {"state": "unknown"}
                _write(self.c, self.record)
        except BaseException:
            self._holder.__exit__(None, None, None)
            self._holder = None
            raise
        return self

    def fileno(self):
        if self._holder is None:
            raise RuntimeError("operation is not active")
        return self._holder.fileno()

    def _current(self, require_running=True):
        if self._holder is None or self._finished:
            raise Refused("operation_not_active", "operation cannot be updated")
        state, record = read_active(self.c)
        if (state != "valid" or record["id"] != self.id
                or record["op"] != self.action
                or record["owner"] != self.record["owner"]
                or (require_running and record["status"] != "running")):
            raise Refused("active_op_changed", "operation record changed; refusing to overwrite it")
        return record

    def phase(self, name):
        if not isinstance(name, str) or not name or len(name) > 160:
            raise ValueError("invalid operation phase")
        with self._control():
            record = self._current()
            record["phase"] = name
            record["updated_at"] = max(time.time(), record["updated_at"])
            _write(self.c, record)
            self.record = record

    def finish(self, code, ok, summary=None):
        self._finish(code, ok, summary, require_running=True)

    def _finish(self, code, ok, summary, require_running):
        if not _integer(code) or type(ok) is not bool:
            raise ValueError("invalid operation outcome")
        with self._control():
            record = self._current(require_running=require_running)
            success = code == 0 and ok
            record.update({"status": "finished" if success else "incomplete",
                           "phase": "finished" if success else "incomplete",
                           "exit_code": code, "ok": ok,
                           "updated_at": max(time.time(), record["updated_at"])})
            if success and self.action == "stop":
                record["recovery_required"] = False
            if summary is not None:
                record["summary"] = summary
            _write(self.c, record)
            self.record = record
            self._finished = True

    def __exit__(self, exc_type, exc, tb):
        try:
            if not self._finished:
                # A failed directory fsync can leave a visible "finished"
                # rename even though finish() raised. Reassert an incomplete
                # record for this same ID; never rewrite another operation ID.
                self._finish(1, False,
                             {"reason": "exception" if exc_type else "not_finished"},
                             require_running=False)
        finally:
            if self._holder is not None:
                # job_mode closes only this descriptor. Finite helpers may still
                # hold the same open file description after the controller exits.
                self._holder.__exit__(None, None, None)
                self._holder = None
        return False


class ReassertGuard:
    """Nonblocking watchdog guard; never create or replace an operation record."""

    def __init__(self, c):
        self.c = c
        self._holder = None

    def __enter__(self):
        self._holder = _acquire(self.c, 0)
        try:
            with _control(self.c, timeout=0):
                state, record = read_active(self.c)
                if state == "unknown":
                    raise Refused("active_op_unknown", "operation record is unreadable or invalid")
                if state == "missing":
                    raise Refused("active_op_missing", "explicit controller start is required before watchdog export")
                if state == "valid" and record["status"] != "finished":
                    raise Refused("active_op_incomplete", "previous operation is not confirmed complete")
                if state == "valid" and record.get("recovery_required", False):
                    raise Refused("recovery_required", "explicit stop recovery must finish first")
                try:
                    boot_id = _boot_id()
                except (OSError, ValueError, UnicodeError) as exc:
                    raise Refused("boot_unknown", "cannot establish the current boot identity") from exc
                if record["owner"]["boot_id"] != boot_id:
                    raise Refused("boot_changed", "previous boot cannot authorize watchdog export; use the controller recovery path")
        except TimeoutError as exc:
            self._holder.__exit__(None, None, None)
            self._holder = None
            raise Refused("control_busy", "state control lock is busy") from exc
        except BaseException:
            self._holder.__exit__(None, None, None)
            self._holder = None
            raise
        return self

    def fileno(self):
        if self._holder is None:
            raise RuntimeError("guard is not active")
        return self._holder.fileno()

    def __exit__(self, *args):
        if self._holder is not None:
            self._holder.__exit__(*args)
            self._holder = None
        return False
