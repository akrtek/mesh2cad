"""mesh2cad: convert STL / 3MF / OBJ meshes into STEP solids with real CAD faces."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import trimesh

from . import __version__, convert, meshio, stepio
from .progress import Progress


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="mesh2cad",
        description="Convert a triangle mesh (STL, 3MF, OBJ, PLY) from CAD into a STEP solid whose "
                    "flat faces, holes, fillets, cones, spheres and tori are real CAD surfaces.")
    ap.add_argument("input", help="mesh file (.stl .3mf .obj .ply .off .glb)")
    ap.add_argument("-o", "--output", help="output .step (default: input name + .step)")
    ap.add_argument("--mode", choices=["prismatic", "planar", "faceted"], default="prismatic",
                    help="prismatic: fit planes + curved surfaces (default); planar: merge flat faces only; "
                         "faceted: one face per triangle")
    ap.add_argument("--tol", type=float, default=None,
                    help="fit tolerance in mm (default: measured from the mesh's own noise)")
    ap.add_argument("--angle", type=float, default=40.0,
                    help="largest angle between neighbouring facets of one curved surface, degrees (default 40)")
    ap.add_argument("--max-seconds", type=float, default=300,
                    help="time budget per body for the curved-surface search (default 300; 0 = no limit)")
    ap.add_argument("--body", type=int, action="append",
                    help="convert only this body index (repeatable); see --list")
    ap.add_argument("--list", action="store_true", help="list bodies in the file and exit")
    ap.add_argument("--split", action="store_true",
                    help="treat disconnected pieces of one mesh as separate bodies")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument("--progress", action="store_true",
                    help="print machine-readable progress lines (\"@@progress <0..1> <stage>\"); used by the app")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--version", action="version", version=f"mesh2cad {__version__}")
    a = ap.parse_args(argv)

    log = (lambda *x: None) if a.quiet else (lambda *x: print(*x, flush=True))
    prog = Progress(_emit if a.progress else None)
    src = Path(a.input)
    prog.set(0.0, "Loading mesh", force=True)
    bodies = meshio.load(src)
    if a.split:
        out = []
        for b in bodies:
            parts = trimesh.Trimesh(b.V, b.F, process=True).split(only_watertight=False)
            for k, p in enumerate(parts):
                out.append(meshio.Body(f"{b.name}_{k + 1}" if len(parts) > 1 else b.name,
                                       np.asarray(p.vertices, float), np.asarray(p.faces)))
        bodies = out
    if a.list:
        for i, b in enumerate(bodies):
            ext = b.V.max(0) - b.V.min(0)
            print(f"[{i}] {b.name}: {len(b.F)} triangles, {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f} mm")
        return 0
    pick = list(a.body) if a.body else list(range(len(bodies)))
    shapes, names, reports = [], [], []
    t0 = time.time()
    # each body gets a share of the bar proportional to its triangle count
    weights = np.array([max(len(bodies[i].F), 1) for i in pick], float)
    edges = 0.02 + 0.95 * np.r_[0.0, np.cumsum(weights)] / weights.sum()
    for k, i in enumerate(pick):
        b = bodies[i]
        log(f"[{i}] {b.name}: {len(b.F)} triangles")
        try:
            prog.prefix = f"Body {k + 1} of {len(pick)} · " if len(pick) > 1 else ""
            with prog.part(edges[k], edges[k + 1]):
                shape, rep = convert.convert_body(b.V, b.F, tol=a.tol, max_angle=a.angle, mode=a.mode, log=log,
                                                  max_seconds=a.max_seconds, progress=prog)
        except Exception as ex:  # keep going with the other bodies
            log(f"    failed: {ex}")
            reports.append({"body": b.name, "error": str(ex)})
            continue
        times = prog.timing_line(convert.STAGES)
        if times:
            log(f"    time: {times}")
        g = rep.get("groups", {})
        kinds = ", ".join(f"{v} {k}" for k, v in sorted(g.items(), key=lambda kv: -kv[1]))
        log(f"    -> {rep['faces']} faces ({rep['mode']}{': ' + kinds if kinds else ''}), "
            f"max deviation {rep.get('deviation_mm', float('nan')):.4f} mm, {rep['seconds']} s")
        shapes.append(shape)
        names.append(b.name)
        rep["body"] = b.name
        reports.append(rep)
    if not shapes:
        print("nothing converted", file=sys.stderr)
        return 1
    out = Path(a.output) if a.output else src.with_suffix(".step")
    prog.prefix = ""
    with prog.part(0.97, 1.0, "Writing STEP"):
        ok = stepio.write_step(shapes, out, names)
    log(f"wrote {out} ({len(shapes)} bod{'y' if len(shapes) == 1 else 'ies'}) in {time.time() - t0:.1f} s" if ok else "STEP write failed")
    if a.report:
        Path(a.report).write_text(json.dumps(reports, indent=1, default=float))
    return 0 if ok else 2


def _emit(value, text):
    print(f"@@progress {value:.4f}\t{text}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
