#!/usr/bin/env python3
"""locks.py —— 工作锁与镜像独占探测（P1 修正版）

三态探测：free / busy / unknown
  - 读不到 ≠ 空闲。任何探测失败都返回 unknown，调用方必须拒绝。
  - 正常未配置的 loop 节点（无 backing_file）不算故障。

锁：flock 绑定到 open file description。
  - 把 fd 传给子进程后，父进程**只关闭自己的 fd**，不调用 LOCK_UN。
  - 只要还有子进程持有该 fd，锁就保持有效（修正四）。
"""

import fcntl
import os
import time

DEFAULT_MOUNT_LOCK = "/data/local/tmp/teslacam-mount.lock"

FREE, BUSY, UNKNOWN = "free", "busy", "unknown"


def _unescape(field):
    """解码 /proc/mounts 的八进制转义，如 \\040 -> 空格。"""
    out = []
    i = 0
    while i < len(field):
        c = field[i]
        if c == "\\" and i + 3 < len(field):
            try:
                out.append(chr(int(field[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(c)
        i += 1
    return "".join(out)


# ---------------- loop 探测 ----------------

def probe_loops(image_path, sys_block="/sys/block"):
    """返回 (state, loops, reason)。"""
    real = os.path.realpath(image_path)
    try:
        names = sorted(n for n in os.listdir(sys_block) if n.startswith("loop"))
    except OSError as e:
        return UNKNOWN, [], "sys_block_unreadable:%s" % e

    found = []
    for n in names:
        bf = os.path.join(sys_block, n, "loop", "backing_file")
        try:
            with open(bf) as f:
                backing = f.read().strip()
        except FileNotFoundError:
            # 节点正常未配置（或枚举后消失）：不是故障
            continue
        except OSError as e:
            return UNKNOWN, found, "backing_read_failed:%s:%s" % (n, e)
        if not backing:
            continue
        if not backing.startswith("/"):
            backing = "/" + backing
        try:
            same = os.path.realpath(backing) == real
        except OSError:
            return UNKNOWN, found, "realpath_failed:%s" % n
        if same:
            found.append(n)

    return (BUSY if found else FREE), found, ""


# ---------------- 挂载探测 ----------------

def _device_aliases(loop_names):
    """loop43 的等价设备路径集合。"""
    out = set()
    for n in loop_names:
        out.add(n)
        out.add("/dev/" + n)
        out.add("/dev/block/" + n)
    return out


def probe_mounts(image_path, loop_names=(), proc_mounts="/proc/mounts"):
    """返回 (state, lines, reason)。解析 /proc/mounts 的实际字段格式。"""
    real = os.path.realpath(image_path)
    aliases = _device_aliases(loop_names)

    try:
        with open(proc_mounts) as f:
            raw = f.read().splitlines()
    except OSError as e:
        return UNKNOWN, [], "mounts_unreadable:%s" % e

    hits = []
    for line in raw:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        src = _unescape(fields[0])
        tgt = _unescape(fields[1])

        if src in aliases or os.path.basename(src) in aliases:
            hits.append(line)
            continue
        # 兜底：镜像文件路径直接出现在源里（某些 mount 实现保留原路径）
        if src == real or os.path.realpath(src) == real if src.startswith("/") else False:
            hits.append(line)
            continue
        # 极少数情况下挂载点就是镜像路径
        if tgt == real:
            hits.append(line)

    return (BUSY if hits else FREE), hits, ""


def probe_all(image_path, sys_block="/sys/block", proc_mounts="/proc/mounts"):
    """组合探测：先 loops，再 mounts（即使 loops 命中也要给出完整信息）。"""
    lstate, loops, lreason = probe_loops(image_path, sys_block)
    mstate, mlines, mreason = probe_mounts(image_path, loops, proc_mounts)

    if lstate == UNKNOWN or mstate == UNKNOWN:
        return UNKNOWN, {"loops": loops, "mounts": mlines,
                         "reason": lreason or mreason}
    if lstate == BUSY:
        return BUSY, {"loops": loops, "mounts": mlines, "reason": "loop_in_use"}
    if mstate == BUSY:
        return BUSY, {"loops": loops, "mounts": mlines, "reason": "mounted"}
    return FREE, {"loops": [], "mounts": [], "reason": ""}


# ---------------- 锁 ----------------

class MountLock:
    """挂载工作锁。

    job_mode=False：常规用法，__exit__ 显式解锁。
    job_mode=True ：把 fd 交给子作业后，父进程只关闭 fd，不解锁；
                    锁存活到最后一个持有该 fd 的进程退出（修正四）。
    """

    def __init__(self, path=DEFAULT_MOUNT_LOCK, timeout=0.0, job_mode=False):
        self.path = path
        self.timeout = timeout
        self.job_mode = job_mode
        self.fd = None

    def __enter__(self):
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(d, exist_ok=True)
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            raise RuntimeError("lock_open_failed:%s" % e)

        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    raise TimeoutError("mount lock busy")
                time.sleep(0.05)
            except OSError as e:
                os.close(self.fd)
                self.fd = None
                raise RuntimeError("lock_iface_error:%s" % e)

    def fileno(self):
        return self.fd

    def __exit__(self, *a):
        if self.fd is not None:
            if not self.job_mode:
                try:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            # job_mode：子进程仍持有同一 open file description，
            # 只关闭自己的 fd，锁由内核在该 OFD 全部关闭后释放。
            os.close(self.fd)
            self.fd = None
        return False


def try_lock(path=DEFAULT_MOUNT_LOCK, timeout=0.0):
    """探测锁是否可用。返回 (state, reason)。"""
    try:
        with MountLock(path, timeout):
            return FREE, ""
    except TimeoutError:
        return BUSY, "lock_held"
    except RuntimeError as e:
        return UNKNOWN, str(e)
