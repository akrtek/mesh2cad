"""Turn face groups into a B-rep solid: one face per group, one edge per boundary chain."""
from __future__ import annotations

import collections

import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_MakeVertex, BRepBuilderAPI_MakeWire,
                                BRepBuilderAPI_Sewing)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.Geom import (Geom_ConicalSurface, Geom_CylindricalSurface, Geom_SphericalSurface)
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Pln, gp_Pnt
from OCP.ShapeFix import ShapeFix_Face, ShapeFix_Shape, ShapeFix_Shell, ShapeFix_Solid
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

from OCP.TopAbs import TopAbs_FACE, TopAbs_IN, TopAbs_SHELL, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopoDS import TopoDS_Shell, TopoDS_Solid

from .progress import Progress
from . import fit as F_
from .occ_compat import harray1_pnt, to_edge, to_face, to_shell, to_solid, to_wire


def P(p):
    return gp_Pnt(float(p[0]), float(p[1]), float(p[2]))


def D(v):
    return gp_Dir(float(v[0]), float(v[1]), float(v[2]))


def count(shape, kind):
    e = TopExp_Explorer(shape, kind)
    n = 0
    while e.More():
        n += 1
        e.Next()
    return n


def volume(shape):
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return g.Mass()


# ------------------------------------------------------------------ topology of groups
def boundary_chains(F, G):
    """Half-edges on group boundaries, node vertices and maximal chains between nodes."""
    he_face = {}
    for f, (a, b, c) in enumerate(F):
        he_face[(a, b)] = f; he_face[(b, c)] = f; he_face[(c, a)] = f
    bhe = {}  # directed boundary half-edge -> (group, other group)
    for (a, b), f in he_face.items():
        g2f = he_face.get((b, a))
        if g2f is None:
            raise ValueError("mesh is not closed/manifold")
        if G[f] != G[g2f]:
            bhe[(a, b)] = (G[f], G[g2f])
    vgroups = collections.defaultdict(set)
    for f, tri in enumerate(F):
        for v in tri:
            vgroups[v].add(G[f])
    und = collections.defaultdict(list)  # vertex -> undirected boundary edges
    for (a, b) in bhe:
        if a < b:
            und[a].append(b); und[b].append(a)
    nodes = {v for v, nb in und.items() if len(nb) != 2 or len(vgroups[v]) >= 3}
    seen = set()
    chains = []
    edge_chain = {}

    def walk(start, nxt):
        path = [start, nxt]
        seen.add(frozenset((start, nxt)))
        while path[-1] not in nodes and path[-1] != start:
            cur = path[-1]
            cand = [w for w in und[cur] if frozenset((cur, w)) not in seen]
            if not cand:
                break
            seen.add(frozenset((cur, cand[0])))
            path.append(cand[0])
        return path

    for v in nodes:
        for w in und[v]:
            if frozenset((v, w)) in seen:
                continue
            chains.append(walk(v, w))
    for v in list(und):
        for w in und[v]:
            if frozenset((v, w)) in seen:
                continue
            chains.append(walk(v, w))  # closed loop, ends where it started
    for ci, ch in enumerate(chains):
        for i in range(len(ch) - 1):
            edge_chain[(ch[i], ch[i + 1])] = (ci, +1)
            edge_chain[(ch[i + 1], ch[i])] = (ci, -1)
    return bhe, chains, edge_chain, nodes


def group_loops(bhe, gid_of_he, edge_chain):
    """For each group: list of loops, each a list of (chain id, direction)."""
    by_group = collections.defaultdict(dict)
    for (a, b), (g, _) in bhe.items():
        by_group[g].setdefault(a, []).append(b)
    loops = {}
    for g, out in by_group.items():
        used = set()
        gl = []
        for a0, bs in out.items():
            for b0 in bs:
                if (a0, b0) in used:
                    continue
                loop = []
                a, b = a0, b0
                while (a, b) not in used:
                    used.add((a, b))
                    loop.append((a, b))
                    nxt = [c for c in out.get(b, []) if (b, c) not in used]
                    if not nxt:
                        break
                    a, b = b, nxt[0]
                # compress to chain traversals
                seq = []
                for (x, y) in loop:
                    ci, s = edge_chain[(x, y)]
                    if not seq or seq[-1][0] != ci:
                        seq.append((ci, s))
                if len(seq) > 1 and seq[0][0] == seq[-1][0]:
                    seq.pop()
                gl.append(seq)
        loops[g] = gl
    return loops


# ------------------------------------------------------------------ curves
def _fit_circle3d(pts):
    pl = F_.fit_plane(pts)
    if np.abs(pl.dist(pts)).max() > 1e-6 * (1 + np.abs(pts).max()):
        return None
    u, v = F_._perp_basis(pl.n)
    X = np.c_[pts @ u, pts @ v]
    c2, r = F_._circle_fit_2d(X)
    c = c2[0] * u + c2[1] * v + pl.n * pl.d
    err = np.abs(np.linalg.norm(pts - c, axis=1) - r).max()
    return (c, pl.n, r, err)


def make_edge(pts, closed, Vtx, i0, i1, lin_tol):
    """Line, circle/arc or interpolated B-spline through chain points."""
    n = len(pts)
    span = np.linalg.norm(pts.max(0) - pts.min(0))
    tol = max(lin_tol, 1e-9 * (1 + np.abs(pts).max()))
    if not closed:
        a, b = pts[0], pts[-1]
        ab = b - a
        L = np.linalg.norm(ab)
        if n == 2 or (L > 0 and np.abs(np.linalg.norm(np.cross(pts - a, ab / L), axis=1)).max() <= tol):
            return BRepBuilderAPI_MakeEdge(Vtx[i0], Vtx[i1]).Edge(), "line"
    if n >= 4 or (closed and n >= 3):
        circ = _fit_circle3d(pts)
        if circ is not None and circ[3] <= tol and circ[2] < 1e3 * max(span, 1e-9):
            c, nrm, r, _ = circ
            # orient circle so increasing parameter follows the chain
            t = np.cross(nrm, pts[0] - c)
            if t @ (pts[1] - pts[0]) < 0:
                nrm = -nrm
            gc = gp_Circ(gp_Ax2(P(c), D(nrm), D(F_._unit(pts[0] - c))), float(r))
            if closed:
                e = BRepBuilderAPI_MakeEdge(gc, Vtx[i0], Vtx[i0])
            else:
                e = BRepBuilderAPI_MakeEdge(gc, Vtx[i0], Vtx[i1])
            if e.IsDone():
                return e.Edge(), "circle"
    arr = harray1_pnt(n - 1 if closed else n)
    for i in range(n - 1 if closed else n):
        arr.SetValue(i + 1, P(pts[i]))
    it = GeomAPI_Interpolate(arr, bool(closed), 1e-9)
    it.Perform()
    crv = it.Curve()
    if closed:
        e = BRepBuilderAPI_MakeEdge(crv, Vtx[i0], Vtx[i0])
    else:
        e = BRepBuilderAPI_MakeEdge(crv, Vtx[i0], Vtx[i1])
    return e.Edge(), "bspline"


# ------------------------------------------------------------------ surfaces
def make_surface(prim, pts):
    """OCC surface for a primitive; seam placed opposite the patch."""
    if prim.kind == "plane":
        return None
    if prim.kind == "cylinder":
        a = prim.a
        h = (pts - prim.c) @ a
        loc = prim.c + a * np.median(h)
        rad = prim.normal(pts).mean(0)
        xd = -rad + 0 * a
        xd = xd - (xd @ a) * a
        if np.linalg.norm(xd) < 1e-6:
            xd = F_._perp_basis(a)[0]
        return Geom_CylindricalSurface(gp_Ax3(P(loc), D(a), D(F_._unit(xd))), float(prim.r))
    if prim.kind == "cone":
        a = prim.a
        h = (pts - prim.v) @ a
        h0 = float(np.median(h))
        loc = prim.v + a * h0
        rad = prim.normal(pts).mean(0)
        xd = -(rad - (rad @ a) * a)
        if np.linalg.norm(xd) < 1e-6:
            xd = F_._perp_basis(a)[0]
        return Geom_ConicalSurface(gp_Ax3(P(loc), D(a), D(F_._unit(xd))), float(prim.alpha), float(h0 * np.tan(prim.alpha)))
    if prim.kind == "sphere":
        m = F_._unit((pts - prim.c).mean(0))
        dirs = F_._unit(pts - prim.c)
        ang = np.degrees(np.arccos(np.clip(dirs @ m, -1, 1))).max()
        if ang < 80:  # small patch: keep poles and seam outside it
            z = F_._perp_basis(m)[0]
            return Geom_SphericalSurface(gp_Ax3(P(prim.c), D(z), D(-m)), float(prim.r))
        x = F_._perp_basis(m)[0]  # cap / big patch: pole in the middle
        return Geom_SphericalSurface(gp_Ax3(P(prim.c), D(m), D(x)), float(prim.r))
    if prim.kind == "torus":
        a = prim.a
        rad = (pts - prim.c) - np.outer((pts - prim.c) @ a, a)
        md = rad.mean(0)
        xd = -md if np.linalg.norm(md) > 1e-9 * (1 + prim.R) else F_._perp_basis(a)[0]
        from OCP.Geom import Geom_ToroidalSurface
        return Geom_ToroidalSurface(gp_Ax3(P(prim.c), D(a), D(F_._unit(xd))), float(prim.R), float(prim.r))
    raise ValueError(prim.kind)


def build_face(group, loops_g, chain_edges, V, F, tol):
    from OCP.TopoDS import TopoDS_Face
    wires = []
    for seq in loops_g:
        mw = BRepBuilderAPI_MakeWire()
        for ci, s in seq:
            e = chain_edges[ci]
            mw.Add(e if s > 0 else to_edge(e.Reversed()))
        if not mw.IsDone():
            raise RuntimeError("wire failed")
        wires.append(mw.Wire())
    vids = np.unique(F[group.faces].ravel())
    pts = V[vids]
    prim = group.prim
    if prim.kind == "plane":
        # outer loop = largest projected area
        n = prim.n
        mesh_n = np.cross(V[F[group.faces[0]][1]] - V[F[group.faces[0]][0]], V[F[group.faces[0]][2]] - V[F[group.faces[0]][0]])
        if mesh_n @ n < 0:
            n = -n
        c = pts.mean(0)
        pln = gp_Pln(P(c - (c @ prim.n - prim.d) * prim.n), D(n))
        areas = []
        for w in wires:
            mf = BRepBuilderAPI_MakeFace(pln, w, True)
            g = GProp_GProps()
            BRepGProp.SurfaceProperties_s(mf.Face(), g) if mf.IsDone() else None
            areas.append(abs(g.Mass()) if mf.IsDone() else 0)
        order = np.argsort(areas)[::-1]
        mf = BRepBuilderAPI_MakeFace(pln, wires[order[0]], True)
        for i in order[1:]:
            mf.Add(wires[i])
        face = mf.Face()
    else:
        surf = make_surface(prim, pts)
        sgn = _surface_sign(surf, group, V, F)
        b = BRep_Builder()
        face = TopoDS_Face()
        b.MakeFace(face, surf, float(tol))
        for w in wires:
            b.Add(face, w if sgn > 0 else to_wire(w.Reversed()))
    sf = ShapeFix_Face(face)
    sf.SetPrecision(float(tol) * 0.1)
    sf.SetMaxTolerance(float(tol) * 10)
    sf.Perform()
    face = sf.Face()
    if not BRepCheck_Analyzer(face).IsValid():
        # usually edges a hair off the surface (same-parameter): let OCC widen tolerances
        fx = ShapeFix_Shape(face)
        fx.SetPrecision(float(tol) * 0.01)
        fx.SetMaxTolerance(float(tol) * 10)
        fx.Perform()
        face = to_face(fx.Shape())
    if prim.kind != "plane" and sgn < 0:
        face = to_face(face.Reversed())
    if not BRepCheck_Analyzer(face).IsValid():
        raise RuntimeError("invalid face")
    return face


def _face_deviation(face, group, V, F, tol):
    """Max distance from a built face to the triangles it replaces (catches faces trimmed
    to the wrong side of a periodic surface)."""
    import trimesh
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRep import BRep_Tool
    tri_pts = V[F[group.faces]]
    chord = float(np.abs(group.prim.dist(tri_pts.mean(1))).max())
    defl = max(chord * 0.5, 5 * tol, 1e-3)
    BRepMesh_IncrementalMesh(face, defl, False, 0.3, True)
    loc = TopLoc_Location()
    tri = BRep_Tool.Triangulation_s(face, loc)
    if tri is None:
        return np.inf, 0.0
    T = loc.Transformation()
    pts = []
    for i in range(1, tri.NbNodes() + 1):
        q = tri.Node(i).Transformed(T)
        pts.append((q.X(), q.Y(), q.Z()))
    pts = np.array(pts)
    if len(pts) > 3000:
        pts = pts[np.random.default_rng(0).choice(len(pts), 3000, replace=False)]
    sub = trimesh.Trimesh(V, F[group.faces], process=False)
    d = np.abs(trimesh.proximity.closest_point(sub, pts)[1]).max()
    return float(d), 1.5 * chord + 5 * tol + defl


def _surface_sign(surf, group, V, F):
    """+1 if the OCC surface normal agrees with the mesh's outward normal on this group."""
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.GeomLProp import GeomLProp_SLProps
    faces = group.faces
    idx = faces[np.linspace(0, len(faces) - 1, min(len(faces), 15)).astype(int)]
    votes = 0.0
    for f in idx:
        tri = V[F[f]]
        n = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        a = np.linalg.norm(n)
        if a == 0:
            continue
        pr = GeomAPI_ProjectPointOnSurf(P(tri.mean(0)), surf)
        if pr.NbPoints() == 0:
            continue
        u, v = pr.LowerDistanceParameters()
        props = GeomLProp_SLProps(surf, u, v, 1, 1e-9)
        if not props.IsNormalDefined():
            continue
        sn = props.Normal()
        votes += np.sign(n @ np.array([sn.X(), sn.Y(), sn.Z()])) * a
    return 1 if votes >= 0 else -1


def build_solid(V, F, G, groups, tol, log=print, unify=True, progress=None):
    prog = progress or Progress()
    prog.set(0.0, "tracing face boundaries", force=True)
    bhe, chains, edge_chain, nodes = boundary_chains(F, G)
    loops = group_loops(bhe, None, edge_chain)
    # vertices
    vtx = {}
    for ch in chains:
        for i in (ch[0], ch[-1]):
            if i not in vtx:
                vtx[i] = BRepBuilderAPI_MakeVertex(P(V[i])).Vertex()
    lin_tol = tol * 0.02
    chain_edges = []
    kinds = collections.Counter()
    for ci, ch in enumerate(chains):
        if ci % 50 == 0:
            prog.set(0.15 * ci / max(len(chains), 1), f"{ci:,} of {len(chains):,} edges")
        closed = ch[0] == ch[-1]
        pts = V[ch]
        e, k = make_edge(pts, closed, vtx, ch[0], ch[-1], lin_tol)
        chain_edges.append(e)
        kinds[k] += 1
    faces = []
    failed = []
    reasons = {}
    for gi, g in enumerate(groups):
        if gi % 20 == 0:
            prog.set(0.15 + 0.6 * gi / max(len(groups), 1), f"{gi:,} of {len(groups):,} faces")
        if g.gid not in loops:
            failed.append(g.gid)
            continue
        try:
            fc = build_face(g, loops[g.gid], chain_edges, V, F, tol)
            if g.prim.kind != "plane":
                dev, allow = _face_deviation(fc, g, V, F, tol)
                if dev > allow:
                    raise RuntimeError(f"face strays {dev:.3f} mm from its triangles (allowed {allow:.3f})")
            faces.append(fc)
        except Exception as ex:  # noqa
            failed.append(g.gid)
            reasons[g.gid] = str(ex)[:80]
    if failed:
        return None, {"failed_groups": failed, "edge_kinds": dict(kinds), "reasons": reasons}
    prog.set(0.75, f"sewing {len(faces):,} faces into a solid", force=True)
    shape = sew_to_solid(faces, tol)
    if shape is None:
        return None, {"failed_groups": [], "edge_kinds": dict(kinds), "reasons": {}, "not_closed": True}
    if unify:
        prog.set(0.9, "merging faces that share a surface", force=True)
        u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
        u.SetLinearTolerance(float(tol) * 0.01)
        u.SetAngularTolerance(1e-7)
        u.Build()
        shape = u.Shape()
    prog.set(0.95, "validating the solid", force=True)
    ok = BRepCheck_Analyzer(shape).IsValid()
    if not ok:
        fx = ShapeFix_Shape(shape)
        fx.Perform()
        shape = fx.Shape()
        ok = BRepCheck_Analyzer(shape).IsValid()
    ok = ok and is_closed_solid(shape)
    return shape, {"valid": ok, "edge_kinds": dict(kinds), "chains": len(chains), "faces_built": len(faces)}


def is_closed_solid(shape):
    """Exactly one solid whose single shell has no free (unshared) edges."""
    from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
    from OCP.TopAbs import TopAbs_EDGE
    if count(shape, TopAbs_SOLID) != 1 or count(shape, TopAbs_SHELL) != 1:
        return False
    fb = ShapeAnalysis_FreeBounds(shape)
    return count(fb.GetClosedWires(), TopAbs_EDGE) == 0 and count(fb.GetOpenWires(), TopAbs_EDGE) == 0


def sew_to_solid(faces, tol):
    """Sew faces into one closed shell and make a solid of it (None if it won't close)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
    sew = BRepBuilderAPI_Sewing(float(max(5 * tol, 1e-6)))
    for f in faces:
        sew.Add(f)
    sew.Perform()
    sewn = sew.SewedShape()
    shells = []
    e = TopExp_Explorer(sewn, TopAbs_SHELL)
    while e.More():
        shells.append(to_shell(e.Current()))
        e.Next()
    if len(shells) != 1:
        return None
    sfs = ShapeFix_Shell(shells[0])
    sfs.Perform()
    shell = sfs.Shell()
    ms = BRepBuilderAPI_MakeSolid(shell)
    if not ms.IsDone():
        return None
    fs = ShapeFix_Solid(ms.Solid())
    fs.Perform()
    out = fs.Shape()
    if count(out, TopAbs_SOLID) != 1:
        return None
    sol = to_solid(TopExp_Explorer(out, TopAbs_SOLID).Current())
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    cl = BRepClass3d_SolidClassifier(sol)
    cl.PerformInfinitePoint(1e-7)
    if cl.State() == TopAbs_IN:  # inside-out
        sol = to_solid(sol.Reversed())
    return sol


def faceted_solid(V, F, progress=None):
    """Last resort: one planar face per triangle, coplanar ones merged afterwards."""
    prog = progress or Progress()
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace as MF
    vtx = [BRepBuilderAPI_MakeVertex(P(p)).Vertex() for p in V]
    E = {}

    def edge(a, b):
        k = (min(a, b), max(a, b))
        if k not in E:
            E[k] = BRepBuilderAPI_MakeEdge(vtx[k[0]], vtx[k[1]]).Edge()
        e = E[k]
        return e if a == k[0] else to_edge(e.Reversed())

    b = BRep_Builder()
    shell = TopoDS_Shell()
    b.MakeShell(shell)
    for k, (a, c, d) in enumerate(F):
        if k % 500 == 0:
            prog.set(0.6 * k / len(F), f"{k:,} of {len(F):,} triangles")
        n = np.cross(V[c] - V[a], V[d] - V[a])
        if np.linalg.norm(n) == 0:
            continue
        w = BRepBuilderAPI_MakeWire(edge(a, c), edge(c, d), edge(d, a)).Wire()
        b.Add(shell, MF(gp_Pln(P(V[a]), D(n)), w, True).Face())
    solid = TopoDS_Solid()
    b.MakeSolid(solid)
    b.Add(solid, shell)
    prog.set(0.6, "merging coplanar triangles", force=True)
    fs = ShapeFix_Solid(solid)
    fs.Perform()
    u = ShapeUpgrade_UnifySameDomain(fs.Solid(), True, True, False)
    u.Build()
    out = u.Shape()
    if not is_closed_solid(out):
        prog.set(0.8, "sewing triangles into a solid", force=True)
        sew = BRepBuilderAPI_Sewing(1e-6)
        sew.Add(shell)
        sew.Perform()
        out2 = sew_to_solid([f for f in _faces(sew.SewedShape())], 1e-6)
        if out2 is not None:
            out = out2
    return out


def _faces(shape):
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        yield to_face(e.Current())
        e.Next()
