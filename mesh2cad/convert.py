"""End-to-end conversion with graceful fallbacks."""
from __future__ import annotations

import collections
import time

import numpy as np
import trimesh

from . import brep, meshio, segment, snap
from .progress import Progress
from OCP.TopAbs import TopAbs_FACE


def estimate_noise(V, F):
    """Coordinate noise, measured on the largest flat faces: grow regions with a loose
    distance limit but a tight angle limit (so curved surfaces don't join), fit planes
    and take the scatter. CAD exports put vertices exactly on surfaces, so the scatter
    on a flat face is numeric noise from export / re-saving."""
    diag = float(np.linalg.norm(V.max(0) - V.min(0)))
    S = segment.Segmenter(V, F, tol=max(0.01, 1e-4 * diag), plane_angle_deg=0.5, log=lambda *a: None)
    regs = S.planar_regions()
    areas = np.array([S.A[r].sum() for r in regs])
    res = []
    from .fit import fit_plane
    for i in np.argsort(-areas)[:10]:
        if areas[i] < 0.02 * areas.max():  # only genuinely big flat faces
            continue
        vids = np.unique(F[regs[i]].ravel())
        if len(vids) < 6:
            continue
        Q = V[vids]
        pl = fit_plane(Q)
        d = np.abs(pl.dist(Q))
        keep = d <= max(3 * np.median(d), 1e-12)  # drop border contamination, refit
        if keep.sum() >= 4:
            pl = fit_plane(Q[keep])
            d = np.abs(pl.dist(Q))
        res.append(np.percentile(d, 90))
    # max over faces: noise is often uneven (one re-saved face can carry all of it)
    return float(np.max(res)) if res else None


def default_tol(V, F=None):
    diag = float(np.linalg.norm(V.max(0) - V.min(0)))
    noise = estimate_noise(V, F) if F is not None else None
    floor = 2e-6 * diag
    if noise is None:
        return max(0.005, 2e-5 * diag)
    return float(min(max(5 * noise, floor), 0.02))


def face_types(shape):
    """Count final faces by surface type: {'plane': 8, 'cylinder': 6, ...}."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopExp import TopExp_Explorer
    from .occ_compat import to_face
    names = {"Plane": "plane", "Cylinder": "cylinder", "Cone": "cone", "Sphere": "sphere", "Torus": "torus",
             "BSplineSurface": "freeform", "BezierSurface": "freeform"}
    out = collections.Counter()
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        t = str(BRepAdaptor_Surface(to_face(e.Current())).GetType()).split(".")[-1].split("_")[-1]
        out[names.get(t, "other")] += 1
        e.Next()
    return dict(out)


def deviation(shape, V, F, defl):
    """Two-way check: output surface vs input mesh. Returns (max out->mesh, max mesh->out)."""
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopExp import TopExp_Explorer
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from .occ_compat import to_face
    BRepMesh_IncrementalMesh(shape, defl, False, 0.2, True)
    pts, tris, off = [], [], 0
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        f = to_face(e.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(f, loc)
        if tri is not None:
            T = loc.Transformation()
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i).Transformed(T)
                pts.append((p.X(), p.Y(), p.Z()))
            for i in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(i).Get()
                tris.append((a - 1 + off, b - 1 + off, c - 1 + off))
            off = len(pts)
        e.Next()
    out = trimesh.Trimesh(np.array(pts), np.array(tris), process=False)
    src = trimesh.Trimesh(V, F, process=False)
    rng = np.random.default_rng(0)

    def maxdist(mesh, Q, n=6000, chunk=1500):
        if len(Q) > n:
            Q = Q[rng.choice(len(Q), n, replace=False)]
        best = 0.0
        for i in range(0, len(Q), chunk):
            best = max(best, float(np.abs(trimesh.proximity.closest_point(mesh, Q[i:i + chunk])[1]).max()))
        return best

    return maxdist(src, out.vertices), maxdist(out, V)


def _demote(groups, G, bad_gids, regions):
    """Replace failed groups: curved -> one plane group per planar region; plane -> one group per triangle."""
    out = []
    G = G.copy()
    for g in groups:
        if g.gid not in bad_gids:
            out.append(g)
            continue
        if g.prim.kind != "plane" and g.regions:
            for r in g.regions:
                out.append(segment.Group(-1, None, regions[r], [r], True, 1))
        else:
            for f in g.faces:
                out.append(segment.Group(-1, None, np.array([f]), [], False, 1))
    return out


def _refit_planes(V, F, groups, N):
    from . import fit as F_
    for i, g in enumerate(groups):
        g.gid = i
        if g.prim is None:
            vids = np.unique(F[g.faces].ravel())
            if len(g.faces) == 1:
                n = N[g.faces[0]]
                if not np.isfinite(n).all() or np.linalg.norm(n) < 0.5:
                    n = np.array([0, 0, 1.0])
                g.prim = F_.Plane(n, n @ V[vids].mean(0))
                g.constrain = False
            else:
                pl = F_.fit_plane(V[vids])
                if pl.n @ N[g.faces].sum(0) < 0:
                    pl = F_.Plane(-pl.n, -pl.d)
                g.prim = pl
    Gn = np.empty(len(F), np.int64)
    for g in groups:
        Gn[g.faces] = g.gid
    return Gn


STAGES = ("Repairing mesh", "Measuring mesh noise", "Finding flat faces", "Finding curved surfaces",
          "Snapping vertices", "Building faces", "Checking against the mesh", "Building faceted solid", "Finishing")


def convert_body(V, F, tol=None, max_angle=40.0, mode="prismatic", log=print, max_seconds=300, progress=None):
    prog = progress or Progress()
    prog.reset_timings()
    t0 = time.time()
    with prog.part(0.0, 0.03, "Repairing mesh"):
        V, F, watertight = meshio.repair(V, F, log=log)
    if not watertight:
        log("  mesh is not closed; output will be a surface body")
    if not tol:
        with prog.part(0.03, 0.07, "Measuring mesh noise"):
            tol = default_tol(V, F)
    log(f"  tolerance {tol:.2e} mm")
    mesh = trimesh.Trimesh(V, F, process=False)
    mesh_vol = float(mesh.volume)
    N = np.asarray(mesh.face_normals)
    report = {"triangles": int(len(F)), "tol": tol}
    modes = {"prismatic": ["prismatic", "planar", "faceted"], "planar": ["planar", "faceted"], "faceted": ["faceted"]}[mode]
    shape = None
    with prog.part(0.07, 0.97):
        S = segment.Segmenter(V, F, tol, max_angle_deg=max_angle, log=log)
        S.deadline = time.time() + max_seconds if max_seconds else None
        S.progress = prog
        for m in modes:
            with prog.part(prog.local(), 1.0):
                if m != modes[0]:
                    prog.say(f"trying {m} mode instead")
                if m == "faceted":
                    with prog.part(0.0, 1.0, "Building faceted solid"):
                        shape = brep.faceted_solid(V, F, progress=prog)
                    report.update(mode="faceted")
                    break
                with prog.part(0.0, 0.62 if m == "prismatic" else 0.25):
                    G, groups = S.run(curved=(m == "prismatic"))
                shape = None
                for attempt in range(4):
                    with prog.part(prog.local(), 1.0):
                        with prog.part(0.0, 0.08, "Snapping vertices"):
                            Vs, moved, bad = snap.snap(V, F, G, groups, tol, progress=prog)
                        if bad:
                            bad_g = {G[f] for f in range(len(F)) if any(v in {b for b, _ in bad} for v in F[f]) and groups[G[f]].prim.kind != "plane"}
                            if bad_g:
                                groups = _demote(groups, G, bad_g, S.regions)
                                G = _refit_planes(V, F, groups, N)
                                continue
                        with prog.part(0.08, 0.8, "Building faces"):
                            if attempt:
                                prog.say(f"retry {attempt + 1}")
                            shape, info = brep.build_solid(Vs, F, G, groups, tol, progress=prog)
                        if shape is None and info.get("not_closed"):
                            log(f"  {m}: faces would not sew into a closed solid")
                            break
                        if shape is None:
                            groups = _demote(groups, G, set(info["failed_groups"]), S.regions)
                            G = _refit_planes(V, F, groups, N)
                            continue
                        with prog.part(0.8, 1.0, "Checking against the mesh"):
                            vol = abs(brep.volume(shape))
                            if not info["valid"] or (watertight and abs(vol - mesh_vol) > 0.02 * mesh_vol):
                                log(f"  {m}: result rejected (valid={info['valid']}, volume {vol:.1f} vs mesh {mesh_vol:.1f})")
                                shape = None
                                break
                            # surfaces may sit off the facets by the chord depth, never by much more
                            chord = max([0.0] + [float(np.abs(g.prim.dist(S.C[g.faces])).max()) for g in groups if g.prim.kind != "plane"])
                            defl = max(chord * 0.5, 5 * tol, 0.002)
                            allow = max(1.5 * chord + 5 * tol, 10 * tol) + defl
                            d1, d2 = deviation(shape, Vs, F, defl)
                        report.update(deviation_mm=round(max(d1, d2), 5), allowed_mm=round(allow, 5))
                        if max(d1, d2) > allow:
                            log(f"  {m}: result rejected, deviates {max(d1, d2):.4f} mm from the mesh (allowed {allow:.4f})")
                            shape = None
                            break
                        report.update(mode=m, groups=dict(collections.Counter(g.prim.kind for g in groups)), edges=info["edge_kinds"])
                        break
                if shape is not None:
                    break
    with prog.part(0.97, 1.0, "Finishing"):
        report["closed_solid"] = bool(brep.is_closed_solid(shape))
        report["faces"] = brep.count(shape, TopAbs_FACE)
        report["face_types"] = face_types(shape)
        report["volume"] = abs(brep.volume(shape))
        report["mesh_volume"] = mesh_vol
    report["seconds"] = round(time.time() - t0, 1)
    report["stage_seconds"] = {k: round(v, 2) for k, v in prog.timings.items() if k in STAGES}
    return shape, report
