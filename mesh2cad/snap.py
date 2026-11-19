"""Move every vertex exactly onto the surfaces of the face groups it touches.

After snapping, a vertex shared by three planes sits on their exact corner, a hole
rim vertex sits exactly on both the deck plane and the cylinder, etc. This is what
lets coplanar triangles merge and lets boundary curves be fitted as exact lines and
circles."""
from __future__ import annotations

import numpy as np

from .progress import Progress


def snap(V, F, G, groups, tol, iters=60, log=print, progress=None):
    prog = progress or Progress()
    V = V.copy()
    vg = [set() for _ in range(len(V))]
    for f, (a, b, c) in enumerate(F):
        g = G[f]
        if groups[g].constrain:
            vg[a].add(g); vg[b].add(g); vg[c].add(g)
    moved_max = 0.0
    bad = []
    for v in range(len(V)):
        if v % 500 == 0:
            prog.set(v / len(V), f"{v:,} of {len(V):,} vertices")
        gs = vg[v]
        if not gs:
            continue
        p0 = V[v]
        prims = [groups[g].prim for g in gs]
        planes = [pr for pr in prims if pr.kind == "plane"]
        curved = [pr for pr in prims if pr.kind != "plane"]
        p = p0.copy()
        if planes and not curved:
            p = _project_planes(p0, planes)
        else:
            for _ in range(iters):
                q = p
                for pr in prims:
                    q = pr.project(q[None])[0]
                if np.linalg.norm(q - p) < 1e-12:
                    p = q
                    break
                p = q
        d = np.linalg.norm(p - p0)
        if d > 4 * tol:
            bad.append((v, d))
            continue
        moved_max = max(moved_max, d)
        V[v] = p
    return V, moved_max, bad


def _project_planes(p0, planes):
    """Closest point to p0 satisfying all plane equations (least-norm KKT solve)."""
    Nm = np.array([pl.n for pl in planes])
    d = np.array([pl.d for pl in planes])
    # dedupe near-parallel planes (keep the solve well conditioned)
    keep = []
    for i in range(len(Nm)):
        if all(abs(Nm[i] @ Nm[j]) < 1 - 1e-9 for j in keep):
            keep.append(i)
    Nm, d = Nm[keep], d[keep]
    if len(Nm) > 3:
        Nm, d = Nm[:3], d[:3]
    r = d - Nm @ p0
    lam, *_ = np.linalg.lstsq(Nm @ Nm.T, r, rcond=None)
    return p0 + Nm.T @ lam
