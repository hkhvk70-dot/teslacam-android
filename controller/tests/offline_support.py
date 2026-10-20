"""Test-only Windows imports/locking support. Production never imports this."""

import errno
import os
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "_test_temp"
SCRATCH.mkdir(exist_ok=True)


def install_fcntl():
    if os.name != "nt":
        return
    if "fcntl" in sys.modules:
        return
    import msvcrt
    mod = types.ModuleType("fcntl")
    mod.LOCK_EX, mod.LOCK_NB, mod.LOCK_UN = 2, 4, 8

    def flock(fd, operation):
        # An actual byte-range lock permits independent Windows test processes
        # to compete. It does not certify Linux flock/OFD/inheritance behavior.
        if os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"0")
        os.lseek(fd, 0, os.SEEK_SET)
        if operation & mod.LOCK_UN:
            return msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        try:
            return msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError(errno.EWOULDBLOCK, "test lock busy") from exc
            raise

    mod.flock = flock
    sys.modules["fcntl"] = mod


def prepare():
    sys.dont_write_bytecode = True
    install_fcntl()
    sys.path.insert(0, str(ROOT / "src"))


prepare()
