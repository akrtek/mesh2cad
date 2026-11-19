"""Face-group segmentation: the equivalent of Fusion's "Generate Face Groups".

Stage 1  planar regions: grow over triangle adjacency while triangles stay within
         `tol` of a common plane (this also merges the long coplanar strips that
         CAD tessellators emit for cylinders, cones and fillets).
Stage 2  curved groups: grow over the region graph across smooth edges (dihedral
         below `max_angle`), fitting cylinder / cone / sphere to the vertices.
         CAD tessellators put vertices exactly on the true surface, so a real
         curved surface fits to within float noise while a corner between two
         flat faces does not. A per-region angular-span check stops two large flat
         faces at a shallow angle from being mistaken for a coarse cylinder.
Everything not claimed by a curved group stays a plane group (one per region).
"""
from __future__ import annotations

import collections
import itertools
import time
from dataclasses import dataclass, field

import numpy as np
import trimesh

from . import fit as F_
from .progress import Progress


@dataclass
class Group:
    gid: int
    prim: object                       # Plane / Cylinder / Cone / Sphere
    faces: np.ndarray                  # triangle indices
    regions: list = field(default_factory=list)
    constrain: bool = True             # participates in vertex snapping
    sign: int = 1                      # +1 if mesh outward normal == prim.normal, -1 if opposite


class Segmenter:
    def __init__(self, V, F, tol, max_angle_deg=40.0, plane_angle_deg=1.0, min_regions=3,
                 kinds=("cylinder", "cone", "sphere", "torus"), log=print):
        self.V, self.F = V, F
        self.tol = tol
        self.max_angle = np.radians(max_angle_deg)
        self.plane_cos = np.cos(np.radians(plane_angle_deg))
        self.min_regions = min_regions
        self.kinds = kinds
        self.log = log
        self.max_sag = None
        self.deadline = None  # time.time() limit for curved-surface search
        self.progress = Progress()  # replaced by the caller to report progress
        m = trimesh.Trimesh(V, F, process=False)
        self.N = np.asarray(m.face_normals)
        self.A = np.asarray(m.area_faces)
        L = np.linalg.norm(V[F[:, [1, 2, 0]]] - V[F], axis=2).max(1)
        self.sliver = self.A <= 1e-6 * L ** 2
        self.C = np.asarray(m.triangles_center)
        self.adj = [[] for _ in range(len(F))]
        for a, b in m.face_adjacency:
            self.adj[a].append(b)
            self.adj[b].append(a)

    # ------------------------------------------------------------------ stage 1
    def planar_regions(self):
        V, F, N, A = self.V, self.F, self.N, self.A
        region = -np.ones(len(F), np.int64)
        regions = []
        order = np.argsort(-A)
        area_eps = 1e-12 * max(A.max(), 1e-30)
        sliver = self.sliver
        prog, nF, done = self.progress, len(F), 0
        for s in order:
            if region[s] >= 0 or sliver[s]:
                continue
            rid = len(regions)
            if rid % 32 == 0:
                prog.set(done / nF, f"{done:,} of {nF:,} triangles grouped")
            members = [s]
            region[s] = rid
            n = N[s]
            d = float(V[F[s]].mean(0) @ n)
            vset = set(F[s])
            next_refit = 4
            stack = [s]
            while stack:
                t = stack.pop()
                for u in self.adj[t]:
                    if region[u] >= 0:
                        continue
                    if not sliver[u] and N[u] @ n < self.plane_cos:
                        continue
                    if np.abs(V[F[u]] @ n - d).max() > self.tol:
                        continue
                    region[u] = rid
                    members.append(u)
                    vset.update(F[u])
                    stack.append(u)
                    if len(members) >= next_refit:
                        pl = F_.fit_plane(V[list(vset)])
                        if pl.n @ n < 0:
                            pl = F_.Plane(-pl.n, -pl.d)
                        if np.abs(pl.dist(V[list(vset)])).max() <= self.tol:
                            n, d = pl.n, pl.d
                        next_refit *= 2
            regions.append(np.array(members))
            done += len(members)
        # slivers carry no orientation: give each to the region of a neighbour (repeat for chains)
        for _ in range(20):
            left = np.where(region < 0)[0]
            if not len(left):
                break
            for f in left:
                for u in self.adj[f]:
                    if region[u] >= 0:
                        region[f] = region[u]
                        regions[region[u]] = np.r_[regions[region[u]], f]
                        break
        for f in np.where(region < 0)[0]:  # isolated sliver islands
            region[f] = len(regions)
            regions.append(np.array([f]))
        keep = [i for i, r in enumerate(regions) if len(r)]
        remap = -np.ones(len(regions), np.int64)
        remap[keep] = np.arange(len(keep))
        regions = [regions[i] for i in keep]
        region = remap[region]
        self.region = region
        self.regions = regions
        # region normals (area weighted), vertex sets, adjacency
        self.rN = np.array([F_._unit((N[r] * A[r][:, None]).sum(0)) if A[r].sum() > 0 else N[r[0]] for r in regions])
        self.rV = [np.unique(F[r].ravel()) for r in regions]
        radj = collections.defaultdict(set)
        for f in range(len(F)):
            for g in self.adj[f]:
                if region[f] != region[g]:
                    radj[region[f]].add(region[g])
        self.radj = radj
        return regions

    # ------------------------------------------------------------------ stage 2
    def _smooth(self, r1, r2):
        c = np.clip(self.rN[r1] @ self.rN[r2], -1, 1)
        return np.arccos(c) <= self.max_angle

    def _internal_mids(self, faces):
        fs = set(faces.tolist())
        mids = []
        for f in faces:
            for g in self.adj[f]:
                if g > f and g in fs:
                    sh = np.intersect1d(self.F[f], self.F[g])
                    if len(sh) == 2:
                        mids.append(self.V[sh].mean(0))
        return np.array(mids) if mids else np.zeros((0, 3))

    def _fit(self, regs, kinds=None):
        kinds = kinds or self.kinds
        key = (tuple(sorted(regs)), tuple(kinds), self.max_sag)
        cache = self.__dict__.setdefault("_fit_cache", {})
        if key in cache:
            return cache[key]
        out = self._fit_uncached(regs, kinds)
        cache[key] = out
        return out

    def _fit_uncached(self, regs, kinds):
        faces = np.concatenate([self.regions[r] for r in regs])
        vids = np.unique(np.concatenate([self.rV[r] for r in regs]))
        P = self.V[vids]
        Nf, Af, Cf = self.N[faces], self.A[faces], self.C[faces]
        ok = Af > 0
        Nf, Af, Cf = Nf[ok], Af[ok], Cf[ok]
        if len(Nf) < 3:
            return None
        cands = []
        ext = float(np.linalg.norm(P.max(0) - P.min(0)))
        gate = 15 * self.tol
        gate_t = max(15 * self.tol, 0.004 * ext)
        if "cylinder" in kinds:
            cands.append(F_.fit_cylinder(P, Nf, Af, gate=gate))
        cyl_ok = bool(cands) and np.abs(cands[0].dist(P)).max() <= self.tol
        if "sphere" in kinds:
            cands.append(F_.fit_sphere(P, gate=gate))
        if "cone" in kinds and not cyl_ok:
            cands.append(F_.fit_cone(P, Nf, Cf, Af, gate=gate_t))
        if "torus" in kinds and len(P) >= 12 and not cyl_ok:
            cands.append(F_.fit_torus(P, Nf, Cf, Af, gate=gate_t))
        good = []
        for prim in cands:
            if len(P) < prim.nparams + 3:
                continue
            vals = [getattr(prim, k, 1.0) for k in ("r", "R", "alpha")]
            if not np.all(np.isfinite(vals)):
                continue
            if prim.kind in ("cylinder", "sphere") and prim.r > 50 * ext:
                continue  # practically flat: leave to planes
            if prim.kind == "cone" and not (np.radians(2) < prim.alpha < np.radians(80)):
                continue
            if prim.kind == "torus" and (prim.r > 50 * ext or prim.r <= 0 or abs(prim.R) > 100 * ext):
                continue
            err = np.abs(prim.dist(P)).max()
            if err > self.tol:
                continue
            if not self._regions_ok(prim, regs):
                continue
            good.append(prim)
        if not good:
            return None
        if len(good) == 1:
            return good[0]
        mids = self._internal_mids(faces)
        scored = []
        for prim in good:
            me = float(np.abs(prim.dist(mids)).mean()) if len(mids) else 0.0
            scored.append((me, prim.nparams, prim))
        best_me = min(x[0] for x in scored)
        ok = [x for x in scored if x[0] <= best_me * 1.5 + 0.02 * self.tol]
        ok.sort(key=lambda x: (x[1], x[0]))
        return ok[0][2]

    def _region_samples(self, r):
        if not hasattr(self, "_samp"):
            self._samp = {}
        if r not in self._samp:
            faces = self.regions[r]
            pts = [self.C[faces]]
            mids = self._internal_mids(faces)
            if len(mids):
                pts.append(mids)
            self._samp[r] = np.vstack(pts)
        return self._samp[r]

    def _sagitta(self, prim, r):
        """How far the region's facets sit off the surface (chord depth)."""
        return float(np.abs(prim.dist(self._region_samples(r))).max())

    def _span(self, prim, r):
        ns = prim.normal(self.V[self.rV[r]])
        return float(np.arccos(np.clip((ns @ ns.T).min(), -1, 1)))

    def _spans_consistent(self, spans, cand=None):
        """CAD tessellators step around a curved surface in roughly equal angles, so the
        pieces of one surface span similar angles. A big flat face that happens to fit a
        huge cylinder spans far more than its neighbouring strips."""
        sp = np.asarray(spans)
        slack = np.radians(1.0)
        if cand is None:
            return sp.max() <= 5 * np.median(sp) + slack
        return cand <= 5 * np.median(sp) + slack

    def _region_ok(self, prim, r, sign=None):
        """Region must lie on prim, its facets must be consistent with the surface normal
        (same convex/concave sense) and span no more than max_angle around the surface."""
        P = self.V[self.rV[r]]
        if np.abs(prim.dist(P)).max() > self.tol:
            return False
        n_surf = prim.normal(P).mean(0)
        nn = np.linalg.norm(n_surf)
        if nn < 1e-9:
            return False
        s = np.sign(self.rN[r] @ (n_surf / nn))
        if s == 0 or (sign is not None and s != sign):
            return False
        if np.arccos(np.clip(abs(self.rN[r] @ (n_surf / nn)), -1, 1)) > self.max_angle * 0.75:
            return False
        ns = prim.normal(P)
        span = np.arccos(np.clip((ns @ ns.T).min(), -1, 1))
        if span > self.max_angle * 1.05 + 1e-6:
            return False
        if self.max_sag is not None and self._sagitta(prim, r) > self.max_sag:
            return False
        return True

    def _regions_ok(self, prim, regs):
        signs = set()
        for r in regs:
            if not self._region_ok(prim, r):
                return False
            P = self.V[self.rV[r]]
            n_surf = F_._unit(prim.normal(P).mean(0))
            signs.add(int(np.sign(self.rN[r] @ n_surf)))
        if len(signs) != 1:
            return False
        return self._spans_consistent([self._span(prim, r) for r in regs])

    def curved_groups(self, keep=(), allow_run=False):
        R = len(self.regions)
        owner = -np.ones(R, np.int64)
        groups = []
        for prim, members, sign in keep:
            for r in members:
                owner[r] = len(groups)
            groups.append((prim, list(members), sign))
        # small regions first: curved-surface strips are small relative to flat faces
        rarea = np.array([self.A[r].sum() for r in self.regions])
        order = np.argsort(rarea)
        tried_fail = 0
        failed = set()
        prog = self.progress
        for k, s in enumerate(order):
            if k % 8 == 0:
                detail = f"{k:,} of {R:,} regions checked · {len(groups):,} curved surfaces"
                if self.deadline is not None:
                    left = max(0, int(self.deadline - time.time()))
                    detail += f" · time budget {left // 60}:{left % 60:02d} left"
                prog.set(k / max(R, 1), detail)
            if owner[s] >= 0:
                continue
            if self.deadline is not None and time.time() > self.deadline:
                if not getattr(self, "_warned", False):
                    self.log("  time budget reached: remaining areas stay as flat facets")
                    self._warned = True
                break
            nbrs = [r for r in self.radj[s] if owner[r] < 0 and self._smooth(s, r)]
            if not nbrs:
                continue
            # freeform areas: every neighbour already failed as a seed -> this one will too
            if not allow_run and sum(1 for r in nbrs if r in failed) >= max(2, len(nbrs)):
                continue
            prim = None
            seeds = []
            sa = rarea[s]
            small = [r for r in nbrs if rarea[r] <= 6 * sa]
            ring2 = list(small)
            for a in small:
                for x in self.radj[a]:
                    if x != s and x not in ring2 and owner[x] < 0 and rarea[x] <= 6 * sa and self._smooth(a, x):
                        ring2.append(x)
            # run of similar-sized smooth neighbours until the fit is over-determined
            # (fan-tessellated cones/corners are single triangles: 3 of them are not enough)
            run, seen_r, vcount = [s], {s}, set(self.rV[s])
            q = collections.deque([s])
            while q and len(vcount) < 14 and len(run) < 12:
                x = q.popleft()
                for y in self.radj[x]:
                    if y in seen_r or owner[y] >= 0 or not self._smooth(x, y):
                        continue
                    seen_r.add(y)
                    if not (sa / 4 <= rarea[y] <= 4 * sa):
                        continue
                    run.append(y)
                    vcount.update(self.rV[y])
                    q.append(y)
            if ring2:
                seeds.append([s] + ring2)
            if len(small) >= 2:
                seeds.append([s] + small)
            seeds += [[s, a, b] for a, b in itertools.combinations(nbrs, 2)]
            for a in nbrs:
                for x in self.radj[a]:
                    if x != s and owner[x] < 0 and self._smooth(a, x):
                        seeds.append([s, a, x] + [y for y in self.radj[x] if y not in (s, a) and owner[y] < 0 and self._smooth(x, y)][:2])
            seeds = seeds[:8]
            if allow_run and len(run) >= 3:
                seeds.append(run)  # second pass only: fans of single triangles
            seen = set()
            for sd in seeds[:12]:
                key = tuple(sorted(set(sd)))
                if key in seen or len(key) < 3:
                    continue
                seen.add(key)
                prim = self._fit(list(key))
                if prim is not None:
                    members = list(key)
                    break
            if prim is None:
                tried_fail += 1
                failed.add(s)
                continue
            P0 = self.V[self.rV[members[0]]]
            sign = int(np.sign(self.rN[members[0]] @ F_._unit(prim.normal(P0).mean(0))))
            mset = set(members)
            spans = [self._span(prim, m) for m in members]
            span_prim = prim
            frontier = collections.deque(r for m in members for r in self.radj[m])
            since_refit = 0
            upgrades = 0
            while frontier:
                r = frontier.popleft()
                if r in mset or owner[r] >= 0:
                    continue
                if not any(self._smooth(r, m) for m in self.radj[r] if m in mset):
                    continue
                if not self._region_ok(prim, r, sign):
                    if upgrades < 6 and np.abs(prim.dist(self.V[self.rV[r]])).max() < 20 * self.tol:
                        upgrades += 1
                        p2 = self._fit(list(mset) + [r])
                        if p2 is None:
                            continue
                        prim = p2
                    else:
                        continue
                if span_prim is not prim:
                    spans = [self._span(prim, m) for m in mset]
                    span_prim = prim
                sp_r = self._span(prim, r)
                if not self._spans_consistent(spans, sp_r):
                    continue
                spans.append(sp_r)
                mset.add(r)
                since_refit += 1
                frontier.extend(self.radj[r])
                if since_refit >= max(4, len(mset) // 3):
                    since_refit = 0
                    p2 = self._fit(list(mset), kinds=(prim.kind,))
                    if p2 is not None:
                        prim = p2
            if len(mset) < self.min_regions:
                continue
            p2 = self._fit(list(mset))
            if p2 is not None:
                prim = p2
            members = sorted(mset)
            gid = len(groups)
            for r in members:
                owner[r] = gid
            groups.append((prim, members, sign))
        self.owner = owner
        return groups

    def absorb(self, cg):
        """Give leftover regions to an adjacent curved group they fit (greedy-order fix)."""
        owner = self.owner
        changed = True
        while changed:
            changed = False
            for gi, (prim, members, sign) in enumerate(cg):
                mset = set(members)
                for m in list(members):
                    for r in self.radj[m]:
                        if owner[r] >= 0 or r in mset or not self._smooth(r, m):
                            continue
                        if not self._region_ok(prim, r, sign):
                            continue
                        if not self._spans_consistent([self._span(prim, x) for x in members[:60]], self._span(prim, r)):
                            continue
                        members.append(r)
                        mset.add(r)
                        owner[r] = gi
                        changed = True
        return cg

    def merge_same(self, cg):
        """Join adjacent curved groups that lie on one surface (greedy growth can split one)."""
        changed = True
        while changed:
            changed = False
            owner = -np.ones(len(self.regions), np.int64)
            for gi, (_, members, _) in enumerate(cg):
                owner[members] = gi
            for gi in range(len(cg)):
                pa, ma, sa = cg[gi]
                nb = {owner[r] for m in ma for r in self.radj[m]} - {-1, gi}
                for gj in sorted(nb):
                    pb, mb, sb = cg[gj]
                    if pb.kind != pa.kind or sa != sb:
                        continue
                    p2 = self._fit(list(ma) + list(mb), kinds=(pa.kind,))
                    if p2 is None:
                        continue
                    cg[gi] = (p2, list(ma) + list(mb), sa)
                    cg.pop(gj)
                    changed = True
                    break
                if changed:
                    break
        self.owner = -np.ones(len(self.regions), np.int64)
        for gi, (_, members, _) in enumerate(cg):
            self.owner[members] = gi
        return cg

    def _deflection(self, cg):
        sags = []
        for prim, members, _ in cg:
            if len(members) >= 6:
                sags.append(np.percentile([self._sagitta(prim, r) for r in members], 95))
        return float(np.median(sags)) if sags else None

    # ------------------------------------------------------------------ assemble
    def run(self, curved=True):
        P = self.progress
        with P.part(0.0, 0.15 if curved else 0.9, "Finding flat faces"):
            self.planar_regions()
        cg = []
        if curved:
            self.max_sag = None
            with P.part(0.15, 1.0, "Finding curved surfaces"):
                with P.part(0.0, 0.7):
                    cg = self.curved_groups()
                with P.part(0.7, 0.86):
                    cg = self.curved_groups(keep=cg, allow_run=True)  # leftovers: small fans
                d = self._deflection(cg)
                if d is not None:
                    lim = 3 * d + 2 * self.tol
                    good = [g for g in cg if max(self._sagitta(g[0], r) for r in g[1]) <= lim]
                    if len(good) < len(cg):
                        self.log(f"  chord depth {d:.4f} mm: re-grouping {len(cg) - len(good)} implausible fits")
                        self.max_sag = lim
                        with P.part(0.86, 0.9):
                            cg = self.curved_groups(keep=good)
                        with P.part(0.9, 0.94):
                            cg = self.curved_groups(keep=cg, allow_run=True)
                with P.part(0.94, 1.0):
                    P.say(f"tidying up {len(cg):,} curved surfaces")
                    cg = self.absorb(cg)
                    cg = self.merge_same(cg)
        groups = []
        G = -np.ones(len(self.F), np.int64)
        for prim, regs, sign in cg:
            faces = np.concatenate([self.regions[r] for r in regs])
            g = Group(len(groups), prim, faces, regs, True, sign)
            G[faces] = g.gid
            groups.append(g)
        for r, faces in enumerate(self.regions):
            if curved and self.owner[r] >= 0:
                continue
            vids = self.rV[r]
            if len(faces) == 1:
                tri = self.V[self.F[faces[0]]]
                n = self.N[faces[0]] if self.A[faces[0]] > 0 else self.rN[r]
                prim = F_.Plane(n, n @ tri.mean(0))
                constrain = False
            else:
                prim = F_.fit_plane(self.V[vids])
                if prim.n @ self.rN[r] < 0:
                    prim = F_.Plane(-prim.n, -prim.d)
                constrain = True
            g = Group(len(groups), prim, faces, [r], constrain, 1)
            G[faces] = g.gid
            groups.append(g)
        self.G = G
        self.groups = groups
        return G, groups
