"""Run private controller regressions; never invoke a production command.

Default: the 58-case host-compatible subset (one native case skips on Windows).
--native: the 128-case Android set, including real shell and /proc fixtures.
Results are written only when --json is supplied; use a path outside the repo.
"""
import argparse
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
import offline_support

HOST = ("test_clean_guard_new", "test_controller_new", "test_concurrency_new",
        "test_lifecycle_new", "test_reassert_boot")
NATIVE = ("test_status_view_new", "test_identity_race2_new", "test_edges2_new",
          "test_edges3_new", "test_compat_wrappers")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="store_true")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.native and (os.name != "posix" or not Path("/system/bin/sh").is_file()):
        parser.error("--native requires an Android/Termux runtime; use the host subset here")
    results = []
    for name in HOST + (NATIVE if args.native else ()):
        module = __import__(name)
        result = unittest.TextTestRunner(verbosity=1).run(
            unittest.defaultTestLoader.loadTestsFromModule(module))
        results.append(dict(suite=name, tests=result.testsRun, failures=len(result.failures),
                            errors=len(result.errors), skipped=len(result.skipped)))
    if args.native:
        # This old CLI suite recursively resets only subdirectories of this
        # freshly-created temporary directory, never a caller-supplied path.
        with tempfile.TemporaryDirectory(dir=offline_support.SCRATCH, prefix="reassert-cli-") as temp:
            p = subprocess.run([sys.executable, "-B", str(ROOT / "tests/test_reassert.py"), temp],
                               capture_output=True, timeout=45)
        output = p.stdout.decode(errors="replace")
        print(output)
        if p.stderr:
            print(p.stderr.decode(errors="replace"), file=sys.stderr)
        match = re.search(r"结果: (\d+) 通过, (\d+) 失败", output)
        results.append(dict(suite="test_reassert_cli", tests=sum(map(int, match.groups())) if match else 0,
                            failures=int(match[2]) if match else 1, errors=0, skipped=0,
                            exit_code=p.returncode))
    report = dict(platform=sys.platform, native=args.native, results=results,
                  tests_run=sum(r["tests"] for r in results),
                  skipped=sum(r["skipped"] for r in results),
                  ok=all(r["failures"] == r["errors"] == r.get("exit_code", 0) == 0 for r in results))
    if args.json:
        with args.json.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
