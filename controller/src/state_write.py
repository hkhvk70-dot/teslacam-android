#!/usr/bin/env python3
"""state_write.py —— 状态文件的原子写入与持久化

设计依据：设计修订稿 v6 第 2 节（P0~P4 提交协议）
职责：
  1. 原子替换（tempfile + os.replace）保证可见性
  2. 文件内容 fsync + 父目录 fsync 保证持久化
  3. 删除也做父目录 fsync（unlink 的持久化靠它）

调用方在任一步失败时，必须保留保护状态，不得当作成功。

命令行：
  state_write.py write  <path> <json>     # 写入（失败退出码非 0）
  state_write.py remove <path>            # 删除；不存在返回 0，失败非 0
  state_write.py read   <path>            # 读取；不存在返回 2
"""

import json
import os
import sys
import tempfile

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_MISSING = 2


def _fsync_dir(path):
    """对目录做 fsync，使 rename/unlink 的目录项变更落盘。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    dfd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_bytes(path, data):
    """原子写入并持久化。失败抛异常，调用方必须保留原保护状态。"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-tmp-")
    try:
        try:
            mv = memoryview(data)
            written = 0
            while written < len(mv):          # os.write 可能短写
                written += os.write(fd, mv[written:])
            os.fsync(fd)                      # 1) 文件内容落盘
        finally:
            os.close(fd)
        os.replace(tmp, path)                 # 2) 原子替换
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    _fsync_dir(path)                          # 3) 目录项落盘
    return True


def remove_durable(path):
    """删除并持久化目录项。文件不存在视为成功（幂等）。"""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False                          # 幂等：本来就没有
    _fsync_dir(path)
    return True


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(__doc__)
        return EXIT_FAIL

    cmd, path = argv[1], argv[2]

    try:
        if cmd == "write":
            if len(argv) < 4:
                sys.stderr.write("write 需要 <json>\n")
                return EXIT_FAIL
            write_bytes(path, argv[3].encode("utf-8"))
            return EXIT_OK

        if cmd == "remove":
            remove_durable(path)
            return EXIT_OK

        if cmd == "read":
            try:
                sys.stdout.write(read_text(path))
            except FileNotFoundError:
                return EXIT_MISSING
            return EXIT_OK

        sys.stderr.write("未知命令: %s\n" % cmd)
        return EXIT_FAIL

    except OSError as e:
        sys.stderr.write("失败: %s\n" % e)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv))
