#!/usr/bin/env python3
"""commit.py —— 提交协议 P0~P4（A16）

设计依据：设计修订稿 v6 第 2、4 节
顺序（先写证据，最后删保护标记）：
  P0  os.sync()            镜像检查结果落盘（失败即可中止）
  P1  fsck_result.json     检查结果与凭据
  P2  initialized.json     clean_at 更新
  P3  删除 export_dirty    ← 只有 P0~P2 全部成功才允许
  P4  删除 op_inprogress

任一步失败：保留 op_inprogress 与 export_dirty，写 export_forbidden，
返回失败，不得当作提交成功。

注意：os.sync() 成功只表示内核接受了同步请求，**不证明数据到达存储介质**；
真实掉电能力仍未验证。
"""

import json
import os
import sys
import time

import gate
import state_write as sw

C_OK = 0
C_FAIL = 1


def _p(state_dir, name):
    return os.path.join(state_dir, name)


def _now():
    return int(time.time())


def _write_forbidden(state_dir, reason):
    """失败留痕。本身失败只能上报，无法再保护。"""
    rec = {"since": _now(), "reason": reason, "by": "commit"}
    try:
        sw.write_bytes(_p(state_dir, "export_forbidden.json"),
                       json.dumps(rec).encode())
        return True
    except OSError:
        return False


def begin_maintenance(state_dir, image_path=None, op="maintenance"):
    """维护开始前先写保护。必须在控制锁内调用。"""
    rec = {"op": op, "since": _now()}
    if image_path:
        try:
            rec["image_id"] = gate.image_id(image_path)
        except OSError:
            rec["image_id"] = None
    sw.write_bytes(_p(state_dir, "op_inprogress.json"),
                   json.dumps(rec).encode())
    return rec


def commit_success(state_dir, image_path, fsck_rc, checks=None):
    """成功提交。只有 fsck 结果可接受时才调用。"""
    steps = []
    step = None

    # fsck 退出码：0 干净，1 已修复；2 及以上表示需要进一步处理
    if fsck_rc not in (0, 1):
        _write_forbidden(state_dir, "fsck_rc=%d" % fsck_rc)
        return {"ok": False, "failed_at": "precheck", "steps": steps,
                "detail": "fsck_rc=%d" % fsck_rc}

    try:
        for step in ("P0", "P1", "P2", "P3", "P4"):
            if step == "P0":
                # 镜像检查结果落盘；同时取弱指纹供后续凭据使用
                os.sync()
                iid = gate.image_id(image_path)

            elif step == "P1":
                sw.write_bytes(_p(state_dir, "fsck_result.json"), json.dumps({
                    "ok": True, "rc": fsck_rc, "at": _now(),
                    "image_id": iid, "checks": checks or [],
                }).encode())

            elif step == "P2":
                sw.write_bytes(_p(state_dir, "initialized.json"), json.dumps({
                    "image_id": iid, "inited_at": _now(), "clean_at": _now(),
                }).encode())

            elif step == "P3":
                # 只有 P0~P2 全部成功才会走到这里
                sw.remove_durable(_p(state_dir, "export_dirty"))

            elif step == "P4":
                sw.remove_durable(_p(state_dir, "op_inprogress.json"))

            steps.append(step)

    except Exception as e:                       # noqa: BLE001
        _write_forbidden(state_dir, "commit_failed@%s:%s" % (step, e))
        return {"ok": False, "failed_at": step,
                "steps": steps, "detail": str(e)}

    return {"ok": True, "steps": steps, "detail": "committed"}


def commit_failure(state_dir, reason):
    """失败出口：保留保护，禁止导出。"""
    _write_forbidden(state_dir, reason)
    return {"ok": False, "detail": reason}


def main(argv):
    if len(argv) < 3:
        sys.stderr.write(
            "commit.py begin    <state> <image>\n"
            "commit.py success  <state> <image> <fsck_rc>\n"
            "commit.py fail     <state> <reason>\n")
        return 2
    cmd = argv[1]
    if cmd == "begin":
        print(json.dumps(begin_maintenance(argv[2], argv[3])))
        return 0
    if cmd == "success":
        r = commit_success(argv[2], argv[3], int(argv[4]))
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1
    if cmd == "fail":
        print(json.dumps(commit_failure(argv[2], argv[3]), ensure_ascii=False))
        return 1
    sys.stderr.write("unknown cmd\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
