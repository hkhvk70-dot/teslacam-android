#!/usr/bin/env python3
"""test_reassert.py —— P2 看门狗导出授权测试

覆盖 reassert 的六种判定：停止 / 失败 hold / 进行中操作 /
LUN 未知 / 未处于导出状态 / 正常重新导出。
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_CAND = [
    os.path.join(HERE, "teslacam.py"),
    os.path.normpath(os.path.join(HERE, "..", "src", "teslacam.py")),
]
SRC = next((p for p in _CAND if os.path.exists(p)), _CAND[0])

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  PASS  %s" % name)
    else:
        failed += 1
        print("  FAIL  %s  %s" % (name, detail))


def env(base, lun="", markers=(), dirty=True, cfg_missing=False):
    shutil.rmtree(base, ignore_errors=True)
    for d in ("state", "run"):
        os.makedirs(os.path.join(base, d))
    cfg = os.path.join(base, "cfg")
    if not cfg_missing:
        lun_dir = os.path.join(cfg, "functions", "mass_storage.0", "lun.0")
        os.makedirs(lun_dir)
        with open(os.path.join(lun_dir, "file"), "w") as f:
            f.write(lun)
    msd = os.path.join(base, "msd.sh")
    with open(msd, "w") as f:
        f.write("#!/system/bin/sh\n"
                "LF=%r\n"
                "case \"$1\" in\n"
                "  up) echo /data/teslacam/cam_disk.img > \"$LF\" ;;\n"
                "  down) : > \"$LF\" ;;\n"
                "esac\nexit 0\n" % os.path.join(cfg, "functions",
                                                "mass_storage.0", "lun.0", "file"))
    img = os.path.join(base, "img")
    open(img, "wb").write(b"R" * 2000)
    for m in markers:
        open(os.path.join(base, "state", m), "w").write("1")
    if dirty:
        open(os.path.join(base, "state", "export_dirty"), "w").close()
    c = dict(state=os.path.join(base, "state"), run=os.path.join(base, "run"),
             cfg=cfg, img=img, msd=msd)
    # Positive watchdog fixtures now require an explicit same-boot completed
    # controller record.  No production path or business command is invoked.
    sys.path.insert(0, os.path.dirname(SRC))
    import lifecycle
    with lifecycle.Operation(c, "start") as operation:
        operation.finish(0, True)
    return c


def call(c):
    args = [sys.executable, SRC, "reassert",
            "--state", c["state"], "--run", c["run"], "--cfg", c["cfg"],
            "--img", c["img"], "--msd", c["msd"], "--services", "none"]
    env2 = dict(os.environ)
    env2["LD_LIBRARY_PATH"] = "/data/data/com.termux/files/usr/lib"
    r = subprocess.run(args, capture_output=True, timeout=60, env=env2)
    try:
        return json.loads(r.stdout.decode()), r.returncode
    except ValueError:
        print("    raw:", r.stdout.decode()[:200], r.stderr.decode()[:300])
        return {}, r.returncode


def lun_of(c):
    p = os.path.join(c["cfg"], "functions", "mass_storage.0", "lun.0", "file")
    try:
        return open(p).read().strip()
    except OSError:
        return "<missing>"


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "/data/local/tmp/tw/ra"
    os.makedirs(base, exist_ok=True)
    print("测试目录: %s\n源码: %s\n" % (base, SRC))

    for name, marker, reason in (
            ("stop_requested", "停止标记", "stopping"),
            ("export_forbidden.json", "失败 hold", "hold"),
            ("op_inprogress.json", "进行中操作", "op_in_progress")):
        c = env(os.path.join(base, name.replace(".", "_")), lun="", markers=[name])
        out, rc = call(c)
        check("T-%s 跳过且不导出" % reason,
              rc == 5 and out.get("skipped") is True and lun_of(c) == "",
              "%s rc=%s lun=%r" % (json.dumps(out, ensure_ascii=False), rc, lun_of(c)))

    # LUN 未知（cfg 目录不存在）
    c = env(os.path.join(base, "lun_unknown"), cfg_missing=True)
    out, rc = call(c)
    check("T-lun_unknown 跳过", rc == 5 and out.get("reason") == "lun_unknown", str(out))

    # 已脱离且没有 dirty（系统处于 stopped）→ 不擅自导出
    c = env(os.path.join(base, "not_exported"), lun="", dirty=False)
    out, rc = call(c)
    check("T-not_exported 不擅自导出",
          rc == 5 and out.get("reason") == "not_exported" and lun_of(c) == "", str(out))

    # 正常：dirty 存在且已脱离 → 重新导出
    c = env(os.path.join(base, "normal"), lun="", dirty=True)
    out, rc = call(c)
    check("T-normal 重新导出", rc == 0 and lun_of(c) != "", str(out))

    # 已导出时调用不应报错
    c = env(os.path.join(base, "already"), lun="/data/teslacam/cam_disk.img", dirty=True)
    out, rc = call(c)
    check("T-already 已导出仍成功", rc == 0, str(out))

    print("\n结果: %d 通过, %d 失败" % (passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
