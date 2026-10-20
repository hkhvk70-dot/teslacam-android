#!/usr/bin/env python3
"""gate.py —— 导出门禁（A15）

设计依据：设计修订稿 v6 第 3 节（gate 七级判定）
必须在**控制锁内**调用；本模块提供 control_lock，但不代替调用方持锁。

判定顺序（先命中先返回）：
  12 stop_requested        停止优先
  10 export_forbidden      需 clear-hold
  11 op_inprogress         进入恢复分支（不是放行也不是普通拒绝）
  13 LUN 读取失败          未知 ≠ 未导出
  14 LUN 非空              已导出
  20 干净凭据不成立        需要检查
   0 全部通过              允许导出

LUN 三态：detached / exported / unknown（读取失败）
干净判定：clean_confirmed / not_initialized / image_mismatch / unknown / dirty
"""

import fcntl
import hashlib
import json
import os
import sys

C_OK = 0
C_FORBIDDEN = 10
C_RECOVER = 11
C_STOPPING = 12
C_LUN_UNKNOWN = 13
C_EXPORTED = 14
C_NEED_CHECK = 20

LUN_REL = "functions/mass_storage.0/lun.0/file"


# ---------------- 控制锁 ----------------

class control_lock:
    """控制锁上下文管理器。超时抛 TimeoutError。"""

    def __init__(self, path, timeout=10.0):
        self.path = path
        self.timeout = timeout
        self.fd = None

    def __enter__(self):
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(d, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        import time
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self.fd
            except OSError:
                if time.time() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError("control lock busy")
                time.sleep(0.05)

    def __exit__(self, *a):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None
        return False


# ---------------- 读取 ----------------

def read_lun(cfg_dir):
    """返回 (state, raw)。state ∈ detached / exported / unknown。"""
    p = os.path.join(cfg_dir, LUN_REL)
    try:
        with open(p, "r") as f:
            raw = f.read()
    except OSError:
        return "unknown", None      # 读不到 ≠ 未导出
    raw = raw.strip()
    return ("exported" if raw else "detached"), raw


def read_json(path):
    """返回 (ok, obj)。失败区分存在与否。"""
    try:
        with open(path) as f:
            return True, json.load(f)
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return False, "error"


def image_id(path, probe=4096):
    """弱指纹：size + 首 probe 字节 + 尾 probe 字节。不宣称能识别全部变化。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(probe)
        if size > probe:
            f.seek(max(0, size - probe))
            tail = f.read(probe)
        else:
            tail = head
    h = lambda b: hashlib.sha256(b).hexdigest()[:16]
    return "size:%d|head:%s|tail:%s" % (size, h(head), h(tail))


def check_clean(state_dir, img_path):
    """返回 (clean: bool, reason: str)。"""
    dirty_p = os.path.join(state_dir, "export_dirty")
    init_p = os.path.join(state_dir, "initialized.json")

    try:
        dirty = os.path.exists(dirty_p)
    except OSError:
        return False, "unknown"

    if dirty:
        return False, "dirty"

    ok, init = read_json(init_p)
    if not ok:
        return False, "unknown" if init == "error" else "not_initialized"

    if not isinstance(init, dict) or not init.get("image_id"):
        return False, "not_initialized"

    try:
        cur = image_id(img_path)
    except OSError:
        return False, "unknown"

    if init["image_id"] != cur:
        return False, "image_mismatch"

    return True, "clean_confirmed"


# ---------------- 门禁 ----------------

def evaluate(state_dir, cfg_dir, img_path, ignore=()):
    """七级判定。调用方必须已持有控制锁。

    ignore: 跳过的检查项名（如 maintain 在"已停止"状态下忽略 stop_requested）。
    """
    for name, code in (("stop_requested", C_STOPPING),
                       ("export_forbidden.json", C_FORBIDDEN),
                       ("op_inprogress.json", C_RECOVER)):
        if name in ignore:
            continue
        p = os.path.join(state_dir, name)
        try:
            if os.path.exists(p):
                return {"code": code, "gate": name, "clean": None,
                        "lun": None, "detail": "state present"}
        except OSError:
            return {"code": C_NEED_CHECK, "gate": name, "clean": "unknown",
                    "lun": None, "detail": "stat failed"}

    lun_state, raw = read_lun(cfg_dir)
    if lun_state == "unknown":
        return {"code": C_LUN_UNKNOWN, "gate": "lun", "clean": None,
                "lun": "unknown", "detail": "lun unreadable"}
    if lun_state == "exported":
        return {"code": C_EXPORTED, "gate": "lun", "clean": None,
                "lun": "exported", "detail": raw}

    clean, why = check_clean(state_dir, img_path)
    if not clean:
        return {"code": C_NEED_CHECK, "gate": "clean", "clean": why,
                "lun": lun_state, "detail": why}

    return {"code": C_OK, "gate": "pass", "clean": why, "lun": lun_state,
            "detail": "all checks passed"}


def main(argv):
    if len(argv) < 4:
        sys.stderr.write("gate.py <state_dir> <cfg_dir> <image_path> [--lock <path>]\n")
        return 2
    state_dir, cfg_dir, img = argv[1], argv[2], argv[3]
    lock = argv[5] if len(argv) > 5 and argv[4] == "--lock" else None

    try:
        if lock:
            with control_lock(lock):
                r = evaluate(state_dir, cfg_dir, img)
        else:
            r = evaluate(state_dir, cfg_dir, img)
    except TimeoutError as e:
        print(json.dumps({"code": 19, "gate": "lock", "detail": str(e)}))
        return 19

    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["code"] == C_OK else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
