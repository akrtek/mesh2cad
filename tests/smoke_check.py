"""Quick end-to-end checks, used by the build workflow (and handy locally).

    python tests/smoke_check.py cli                 # the converter from source
    python tests/smoke_check.py frozen <app binary> # a packaged app: worker mode + window self-test

Exits non-zero if a known part doesn't convert to the expected faces.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EXPECT = {  # part -> (faces, face types)
    "plate_holes": (14, {"plane": 8, "cylinder": 6}),
    "box_allfillet": (18, {"plane": 6, "cylinder": 8, "torus": 4}),
}


def check_report(name: str, report_path: Path) -> None:
    rep = json.loads(report_path.read_text())[0]
    faces, types = EXPECT[name]
    ok = rep.get("closed_solid") and rep.get("faces") == faces and rep.get("face_types") == types
    print(f"  {name}: {rep.get('faces')} faces {rep.get('face_types')} closed={rep.get('closed_solid')} "
          f"{rep.get('seconds')} s -> {'OK' if ok else 'WRONG'}", flush=True)
    if not ok:
        sys.exit(f"{name}: expected {faces} faces {types}")


def run_converter(cmd_prefix: list[str], tmp: Path) -> None:
    for name in EXPECT:
        out, rep = tmp / f"{name}.step", tmp / f"{name}.json"
        r = subprocess.run(cmd_prefix + [str(DATA / f"{name}.stl"), "-o", str(out), "--report", str(rep), "--progress"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        progress = sum(1 for line in r.stdout.splitlines() if line.startswith("@@progress"))
        print(f"  exit {r.returncode}, {progress} progress lines", flush=True)
        if r.returncode != 0 or not out.exists():
            print(r.stdout[-3000:], r.stderr[-3000:])
            sys.exit(f"{name}: conversion failed")
        if progress == 0:
            sys.exit(f"{name}: no progress lines reached the caller")
        if "Statistics on Transfer" in r.stdout:
            sys.exit("STEP writer output leaked into the log")
        check_report(name, rep)


def screenshot(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["screencapture", "-x", str(path)], timeout=30, check=False)


def window_selftest(app: str, tmp: Path) -> None:
    out = Path(os.environ.get("SELFTEST_JSON", tmp / "selftest.json")).resolve()
    shots = Path(os.environ.get("SELFTEST_SHOTS", ".")).resolve()
    env = dict(os.environ, MESH2CAD_SELFTEST=str(DATA / "box_allfillet.stl"), MESH2CAD_SELFTEST_OUT=str(out),
               MESH2CAD_SELFTEST_HOLD="6")
    tag = f"{platform.system()}-{platform.machine()}".lower()
    p = subprocess.Popen([app], env=env)
    t0, shot = time.time(), False
    while p.poll() is None and time.time() - t0 < 600:
        time.sleep(0.5)
        if out.exists() and not shot:  # finished; the window stays up a few seconds for this picture
            time.sleep(1.0)
            screenshot(shots / f"window-{tag}.png")
            shot = True
    if p.poll() is None:
        p.kill()
        sys.exit("window self-test timed out")
    res = json.loads(out.read_text())
    print(f"  window: Tk {res['tk']}, status {res['status']!r}, rows {res['rows']}, outputs {res['outputs']}", flush=True)
    if not res["status"].startswith("Done: 1 of 1") or "box_allfillet.step" not in res["outputs"]:
        print("\n".join(res.get("log", [])))
        sys.exit("window self-test failed")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without ✓
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else "cli"
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        if mode == "cli":
            print("converter from source:")
            run_converter([sys.executable, "-m", "mesh2cad"], tmp)
        elif mode == "frozen":
            app = str(Path(sys.argv[2]).resolve())
            print(f"packaged app {app}:")
            run_converter([app, "--mesh2cad-worker"], tmp)
            window_selftest(app, tmp)
        else:
            sys.exit(__doc__)
    print("all good")


if __name__ == "__main__":
    main()
