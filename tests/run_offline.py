# -*- coding: utf-8 -*-
"""Run the test files with the network switched off, and say which are green.

  PASS           exit 0
  FAIL           a real failure -- fix the code or the test
  NEEDS-NETWORK  it tried to reach the model / service / web: a bench or a
                 live probe, not an offline test
  TIMEOUT        ran past --timeout seconds

    py -3 tests/run_offline.py                 # every tracked tests/*.py
    py -3 tests/run_offline.py tests/a.py ...  # just these
    py -3 tests/run_offline.py --show          # print the failing lines too
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = Path(__file__).resolve().parent / "offline_site"
SELF = Path(__file__).name


def _tracked_tests():
    out = subprocess.run(["git", "ls-files", "tests/*.py"], cwd=ROOT,
                         capture_output=True, text=True, encoding="utf-8").stdout
    return [p for p in out.split() if "/" not in p[len("tests/"):]
            and Path(p).name not in (SELF, "__init__.py", "conftest.py")]


def main(argv):
    show = "--show" in argv
    timeout = 300
    if "--timeout" in argv:
        timeout = int(argv[argv.index("--timeout") + 1])
    files = [a for a in argv if a.endswith(".py")] or _tracked_tests()
    tmp = tempfile.mkdtemp(prefix="offline_run_")
    env = dict(os.environ, PYTHONPATH=str(SITE), PYTHONIOENCODING="utf-8",
               ANTON_LEDGER_DIR=tmp, TRACE_PATH=str(Path(tmp) / "agent_traces.jsonl"))
    piles = {"PASS": [], "FAIL": [], "NEEDS-NETWORK": [], "TIMEOUT": []}
    t0 = time.time()
    for path in files:
        started = time.time()
        try:
            done = subprocess.run([sys.executable, "-X", "utf8", path], cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            piles["TIMEOUT"].append((path, []))
            print("TIMEOUT        %s" % path, flush=True)
            continue
        lines = (done.stdout + "\n" + done.stderr).splitlines()
        refused = any("offline test tried to connect" in ln for ln in lines)
        pile = ("PASS" if done.returncode == 0 else
                "NEEDS-NETWORK" if refused else "FAIL")
        bad = [ln.strip()[:220] for ln in lines
               if ln.strip().startswith(("FAIL", "ERROR", "AssertionError", "Traceback"))
               or "Error:" in ln[:60]]
        piles[pile].append((path, bad))
        print("%-14s %-52s %5.1fs" % (pile, path, time.time() - started), flush=True)
        if show and pile == "FAIL":
            for ln in bad[:8]:
                print("    " + ln)
    print("\n%d files in %.0fs: %s" % (len(files), time.time() - t0, ", ".join(
        "%s %d" % (k, len(v)) for k, v in piles.items() if v)))
    for k in ("FAIL", "TIMEOUT"):
        for path, _ in piles[k]:
            print("  %s: %s" % (k, path))
    return 1 if piles["FAIL"] or piles["TIMEOUT"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
