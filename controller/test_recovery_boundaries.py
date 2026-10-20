#!/usr/bin/env python3
"""Memory-only controller recovery regressions. No native locks/device/processes.

Production function/class ASTs execute unchanged. File contents, locks, LUN,
process/USB collaborators and fsync are explicit in-memory substitutes.
"""

import argparse
import ast
import contextlib
import copy
import functools
import hashlib
import io
import json
import math
from pathlib import Path
import posixpath
import signal
import sys
import time
import types
import unittest
import uuid

DEFAULT_SOURCE = Path(__file__).resolve().parent / "src"
SOURCE = DEFAULT_SOURCE
OLD_BOOT = "11111111-1111-4111-8111-111111111111"
NEW_BOOT = "22222222-2222-4222-8222-222222222222"
OWNER = {"pid": 7788, "starttime": 123456, "boot_id": NEW_BOOT}
OBSERVED_RISKS = []



def forbidden(*args, **kwargs):
    raise AssertionError("unexpected real process/device/lock collaborator")


def load_real(name, namespace):
    path = SOURCE / (name + ".py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = [node for node in tree.body if isinstance(
        node, (ast.Assign, ast.AnnAssign, ast.FunctionDef, ast.ClassDef))]
    module = types.ModuleType("memory_recovery_" + name)
    module.__dict__.update(namespace, __file__=str(path))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), module.__dict__)
    return module


class MemoryHarness:
    def __init__(self):
        self.files = {}
        self.events = []
        self.held = set()
        self.c = {"state": "/private/state", "run": "/private/run",
                  "cfg": "/private/cfg", "img": "/private/fixture.img",
                  "checker": "/private/checker", "msd": "/private/msd",
                  "mount_lock": "/private/run/work.lock", "services": "",
                  "services_conf": "", "spawn": ""}
        self.files[self.c["img"]] = b"private image fixture only"
        self.lun = "/private/cfg/functions/mass_storage.0/lun.0/file"
        self.files[self.lun] = b""
        self.active = self.path("active_op.json")
        self.down_ok = True
        self.up_ok = True
        self.checker_lun_reconnect = False
        self.checker_calls = 0
        self.sync_calls = 0
        owner = self

        class MemoryLock:
            def __init__(self, path, timeout=0, job_mode=False):
                self.path = path
                self.entered = False

            def __enter__(self):
                if self.path in owner.held:
                    raise TimeoutError("finite private helper still owns this lock")
                owner.held.add(self.path)
                self.entered = True
                return self

            def fileno(self):
                return 101 if self.path.endswith("lifecycle.lock") else 102

            def __exit__(self, *args):
                if self.entered:
                    owner.held.remove(self.path)
                    self.entered = False
                return False

        paths = types.SimpleNamespace(join=posixpath.join, dirname=posixpath.dirname,
                                      abspath=lambda p: p, basename=posixpath.basename,
                                      exists=lambda p: p in self.files,
                                      getsize=lambda p: len(self.files[p]))
        memory_os = types.SimpleNamespace(path=paths, listdir=lambda p: [],
                                          getpid=lambda: OWNER["pid"],
                                          killpg=forbidden, sync=self.sync)
        self.sw = types.SimpleNamespace(write_bytes=self.write, remove_durable=self.remove)
        self.locks = types.SimpleNamespace(MountLock=MemoryLock,
                                           FREE="free", BUSY="busy", UNKNOWN="unknown",
                                           probe_all=lambda *a: ("free", {"loops": [], "mounts": []}))
        common = {"json": json, "os": memory_os, "sys": sys, "time": time,
                  "open": self.open, "hashlib": hashlib, "signal": signal,
                  "functools": functools, "math": math, "uuid": uuid,
                  "contextlib": contextlib,
                  "subprocess": types.SimpleNamespace(run=forbidden, Popen=forbidden,
                                                       TimeoutExpired=TimeoutError)}
        self.gate = load_real("gate", common)
        self.gate.control_lock = lambda *a, **kw: contextlib.nullcontext()
        self.commit = load_real("commit", dict(common, gate=self.gate, sw=self.sw))
        self.lifecycle = load_real("lifecycle", dict(common, gate=self.gate, sw=self.sw,
                                                       locks=self.locks,
                                                       pi=types.SimpleNamespace(_stat=forbidden)))
        self.lifecycle._owner = lambda: dict(OWNER)
        self.view_calls = 0
        self.tc = load_real("teslacam", dict(common, gate=self.gate, sw=self.sw,
                                              commit=self.commit, lifecycle=self.lifecycle,
                                              locks=self.locks,
                                              status_view=types.SimpleNamespace(build=self.view),
                                              services=types.SimpleNamespace(load=forbidden),
                                              pi=types.SimpleNamespace(verify=forbidden)))
        self.tc.run_msd = self.run_msd
        self.tc.run_checker = self.run_checker

    def path(self, name):
        return posixpath.join(self.c["state"], name)

    def open(self, path, mode="r", **kwargs):
        if path not in self.files:
            raise FileNotFoundError(path)
        if "b" in mode:
            return io.BytesIO(self.files[path])
        return io.StringIO(self.files[path].decode(kwargs.get("encoding", "utf-8")))

    def write(self, path, data):
        self.events.append(("write", path))
        self.files[path] = bytes(data)

    def remove(self, path):
        self.events.append(("remove", path))
        self.files.pop(path, None)

    def sync(self):
        self.sync_calls += 1

    def view(self, c):
        self.view_calls += 1
        return {"services": {}, "unmanaged": [], "residue": [],
                "inspection": {"complete": True}}

    def run_msd(self, c, action):
        self.events.append(("simulated_msd", action))
        if action == "down" and self.down_ok:
            self.files[self.lun] = b""
            return True, "private LUN detached"
        if action == "up" and self.up_ok:
            self.files[self.lun] = self.c["img"].encode()
            return True, "private LUN exported"
        return False, "private detach failure"

    def run_checker(self, c, fd):
        self.checker_calls += 1
        self.events.append(("simulated_checker", c["img"], fd))
        if self.checker_lun_reconnect:
            self.files[self.lun] = self.c["img"].encode()
        return 0, "private checker result"

    def stale(self, status="running"):
        record = {"schema": 1, "id": "33333333-3333-4333-8333-333333333333",
                  "op": "maintain", "phase": "checking_image", "status": status,
                  "owner": dict(OWNER, boot_id=OLD_BOOT), "started_at": 1,
                  "updated_at": 2, "exit_code": None, "recovery_required": False}
        if status == "finished":
            record.update(exit_code=0, ok=True)
        elif status == "incomplete":
            record.update(exit_code=1, ok=False)
        assert self.lifecycle.validate(record)
        self.files[self.active] = json.dumps(record).encode()
        return record

    def record(self):
        return json.loads(self.files[self.active])

    def call(self, name):
        output = {"steps": [], "ok": False}
        return getattr(self.tc, name + "_body")(self.c, output), output
