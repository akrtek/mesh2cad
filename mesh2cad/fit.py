"""Analytic surface primitives and least-squares fitting.

Each primitive exposes:
    dist(P)      signed distance of points to the surface
    project(P)   closest points on the surface
    normal(P)    unit surface normal (primitive's own orientation)
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import least_squares


def _unit(v):
    v = np.asarray(v, float)
    n = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.where(n == 0, 1, n)


def _perp_basis(a):
    a = _unit(a)
    t = np.array([1.0, 0, 0]) if abs(a[0]) < 0.9 else np.array([0, 1.0, 0])
    u = _unit(np.cross(a, t))
    return u, np.cross(a, u)


class Plane:
    kind = "plane"
    nparams = 3

    def __init__(self, n, d):
        self.n = _unit(n)
        self.d = float(d)

    def dist(self, P):
        return P @ self.n - self.d

    def project(self, P):
        P = np.atleast_2d(P)
        return P - np.outer(self.dist(P), self.n)

    def normal(self, P):
        return np.tile(self.n, (len(np.atleast_2d(P)), 1))

    def __repr__(self):
        return f"Plane(n={np.round(self.n, 4)}, d={self.d:.4f})"


class Cylinder:
    kind = "cylinder"
    nparams = 5

    def __init__(self, c, a, r):
        self.a = _unit(a)
        c = np.asarray(c, float)
        self.c = c - (c @ self.a) * self.a  # point on axis closest to origin
        self.r = float(r)

    def _radial(self, P):
        w = np.atleast_2d(P) - self.c
        return w - np.outer(w @ self.a, self.a)

    def dist(self, P):
        return np.linalg.norm(self._radial(P), axis=1) - self.r

    def project(self, P):
        P = np.atleast_2d(P)
        rad = self._radial(P)
        return P - rad + _unit(rad) * self.r

    def normal(self, P):
        return _unit(self._radial(P))

    def __repr__(self):
        return f"Cylinder(r={self.r:.4f}, a={np.round(self.a, 4)}, c={np.round(self.c, 3)})"


class Sphere:
    kind = "sphere"
    nparams = 4

    def __init__(self, c, r):
        self.c = np.asarray(c, float)
        self.r = float(r)

    def dist(self, P):
        return np.linalg.norm(np.atleast_2d(P) - self.c, axis=1) - self.r

    def project(self, P):
        P = np.atleast_2d(P)
        return self.c + _unit(P - self.c) * self.r

    def normal(self, P):
        return _unit(np.atleast_2d(P) - self.c)

    def __repr__(self):
        return f"Sphere(r={self.r:.4f}, c={np.round(self.c, 3)})"


class Cone:
    """Single-nappe cone: apex v, axis a (radius grows along +a), half angle alpha."""
    kind = "cone"
    nparams = 6

    def __init__(self, v, a, alpha):
        self.v = np.asarray(v, float)
        self.a = _unit(a)
        self.alpha = float(alpha)

    def _hr(self, P):
        w = np.atleast_2d(P) - self.v
        h = w @ self.a
        rad = w - np.outer(h, self.a)
        return h, rad

    def dist(self, P):
        h, rad = self._hr(P)
        rho = np.linalg.norm(rad, axis=1)
        return rho * np.cos(self.alpha) - h * np.sin(self.alpha)

    def normal(self, P):
        h, rad = self._hr(P)
        return _unit(_unit(rad) * np.cos(self.alpha) - self.a * np.sin(self.alpha))

    def project(self, P):
        P = np.atleast_2d(P)
        return P - self.dist(P)[:, None] * self.normal(P)

    def __repr__(self):
        return f"Cone(alpha={np.degrees(self.alpha):.3f}deg, a={np.round(self.a, 4)}, v={np.round(self.v, 3)})"


# ----------------------------------------------------------------------------- fitting

def fit_plane(P, w=None):
    P = np.asarray(P, float)
    w = np.ones(len(P)) if w is None else np.asarray(w, float)
    c = (P * w[:, None]).sum(0) / w.sum()
    Q = (P - c) * np.sqrt(w)[:, None]
    _, _, vt = np.linalg.svd(Q, full_matrices=False)
    n = vt[-1]
    return Plane(n, n @ c)


def _circle_fit_2d(X):
    # Kasa algebraic fit, then Gauss-Newton refine
    A = np.c_[2 * X, np.ones(len(X))]
    b = (X ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    c = sol[:2]
    r = np.sqrt(max(sol[2] + c @ c, 1e-30))
    for _ in range(10):
        d = X - c
        rho = np.linalg.norm(d, axis=1)
        rho = np.where(rho == 0, 1e-30, rho)
        J = np.c_[-d / rho[:, None], -np.ones(len(X))]
        res = rho - r
        step, *_ = np.linalg.lstsq(J, -res, rcond=None)
        c = c + step[:2]
        r = r + step[2]
        if np.abs(step).max() < 1e-12:
            break
    return c, abs(r)


def fit_cylinder(P, N, w=None, gate=None):
    """P: vertices on the surface, N: facet normals (any sign)."""
    P = np.asarray(P, float)
    N = _unit(N)
    w = np.ones(len(N)) if w is None else np.asarray(w, float)
    M = (N * w[:, None]).T @ N
    evals, evecs = np.linalg.eigh(M)
    a = evecs[:, 0]
    u, v = _perp_basis(a)
    X = np.c_[P @ u, P @ v]
    c2, r = _circle_fit_2d(X)
    c = c2[0] * u + c2[1] * v
    cyl = Cylinder(c, a, r)
    if gate is not None and np.abs(cyl.dist(P)).max() > gate:
        return cyl
    # nonlinear refine on all 5 dof
    u0, v0 = _perp_basis(cyl.a)

    def unpack(x):
        a_ = _unit(cyl.a + x[0] * u0 + x[1] * v0)
        c_ = cyl.c + x[2] * u0 + x[3] * v0
        return c_, a_, cyl.r + x[4]

    def parts(x):
        araw = cyl.a + x[0] * u0 + x[1] * v0
        nr = np.linalg.norm(araw)
        a_ = araw / nr
        c_ = cyl.c + x[2] * u0 + x[3] * v0
        wv = P - c_
        h = wv @ a_
        d = wv - np.outer(h, a_)
        rho = np.linalg.norm(d, axis=1)
        e = d / np.where(rho == 0, 1, rho)[:, None]
        return a_, nr, h, rho, e

    def res(x):
        a_, nr, h, rho, e = parts(x)
        return rho - (cyl.r + x[4])

    def jac(x):
        a_, nr, h, rho, e = parts(x)
        J = np.empty((len(P), 5))
        for k, b in enumerate((u0, v0)):
            da = (b - a_ * (a_ @ b)) / nr
            J[:, k] = -h * (e @ da)
        J[:, 2] = -(e @ u0)
        J[:, 3] = -(e @ v0)
        J[:, 4] = -1.0
        return J

    try:
        sol = least_squares(res, np.zeros(5), jac=jac, method="lm", xtol=1e-12, ftol=1e-12, max_nfev=60)
        c_, a_, r_ = unpack(sol.x)
        cyl2 = Cylinder(c_, a_, abs(r_))
        if np.abs(cyl2.dist(P)).max() <= np.abs(cyl.dist(P)).max():
            cyl = cyl2
    except Exception:
        pass
    return cyl


def fit_sphere(P, gate=None):
    P = np.asarray(P, float)
    A = np.c_[2 * P, np.ones(len(P))]
    b = (P ** 2).sum(1)
    sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    c = sol[:3]
    r = np.sqrt(max(sol[3] + c @ c, 1e-30))
    if gate is not None and np.abs(np.linalg.norm(P - c, axis=1) - r).max() > gate:
        return Sphere(c, r)

    def res(x):
        return np.linalg.norm(P - x[:3], axis=1) - x[3]

    def jac(x):
        d = P - x[:3]
        n = np.linalg.norm(d, axis=1)
        return np.c_[-d / np.where(n == 0, 1, n)[:, None], -np.ones(len(P))]

    try:
        s = least_squares(res, np.r_[c, r], jac=jac, method="lm", xtol=1e-12, ftol=1e-12, max_nfev=60)
        c, r = s.x[:3], abs(s.x[3])
    except Exception:
        pass
    return Sphere(c, r)


def fit_cone(P, N, Pf, w=None, gate=None):
    """P: vertices, N: facet normals (oriented consistently), Pf: facet centroids."""
    P = np.asarray(P, float)
    N = _unit(N)
    w = np.ones(len(N)) if w is None else np.asarray(w, float)
    # cone normals lie on a circle on the unit sphere -> plane through normal tips
    m = (N * w[:, None]).sum(0) / w.sum()
    C = ((N - m) * w[:, None]).T @ (N - m)
    _, evecs = np.linalg.eigh(C)
    a = evecs[:, 0]
    # apex: tangent planes all pass through it: n_i . (p_i - v) = 0
    A = N * np.sqrt(w)[:, None]
    b = (N * Pf).sum(1) * np.sqrt(w)
    v, *_ = np.linalg.lstsq(A, b, rcond=None)
    if (P - v).mean(0) @ a < 0:
        a = -a
    h = (P - v) @ a
    rho = np.linalg.norm((P - v) - np.outer(h, a), axis=1)
    alpha = np.arctan2(np.median(rho), np.median(h)) if np.median(h) > 0 else 0.3
    cone = Cone(v, a, alpha)
    if gate is not None and np.abs(cone.dist(P)).max() > gate:
        return cone
    u0, v0 = _perp_basis(cone.a)

    def unpack(x):
        a_ = _unit(cone.a + x[0] * u0 + x[1] * v0)
        return cone.v + x[2:5], a_, cone.alpha + x[5]

    def parts(x):
        araw = cone.a + x[0] * u0 + x[1] * v0
        nr = np.linalg.norm(araw)
        a_ = araw / nr
        v_ = cone.v + x[2:5]
        al = cone.alpha + x[5]
        wv = P - v_
        hh = wv @ a_
        d = wv - np.outer(hh, a_)
        rr = np.linalg.norm(d, axis=1)
        e = d / np.where(rr == 0, 1, rr)[:, None]
        return a_, nr, al, wv, hh, rr, e

    def res(x):
        a_, nr, al, wv, hh, rr, e = parts(x)
        return rr * np.cos(al) - hh * np.sin(al)

    def jac(x):
        a_, nr, al, wv, hh, rr, e = parts(x)
        ca, sa = np.cos(al), np.sin(al)
        J = np.empty((len(P), 6))
        for k, b in enumerate((u0, v0)):
            da = (b - a_ * (a_ @ b)) / nr
            J[:, k] = -ca * hh * (e @ da) - sa * (wv @ da)
        J[:, 2:5] = -ca * e + sa * a_
        J[:, 5] = -rr * sa - hh * ca
        return J

    try:
        s = least_squares(res, np.zeros(6), jac=jac, method="lm", xtol=1e-12, ftol=1e-12, max_nfev=80)
        v_, a_, al = unpack(s.x)
        c2 = Cone(v_, a_, al)
        if 0 < c2.alpha < np.pi / 2 and np.abs(c2.dist(P)).max() <= np.abs(cone.dist(P)).max():
            cone = c2
    except Exception:
        pass
    return cone


class Torus:
    kind = "torus"
    nparams = 7

    def __init__(self, c, a, R, r):
        self.c = np.asarray(c, float)
        self.a = _unit(a)
        self.R = float(R)
        self.r = float(r)

    def _parts(self, P):
        w = np.atleast_2d(P) - self.c
        h = w @ self.a
        rad = w - np.outer(h, self.a)
        rho = np.linalg.norm(rad, axis=1)
        return h, rad, rho

    def dist(self, P):
        h, rad, rho = self._parts(P)
        return np.sqrt((rho - self.R) ** 2 + h ** 2) - self.r

    def normal(self, P):
        h, rad, rho = self._parts(P)
        return _unit(_unit(rad) * (rho - self.R)[:, None] + np.outer(h, self.a))

    def project(self, P):
        P = np.atleast_2d(P)
        return P - self.dist(P)[:, None] * self.normal(P)

    def __repr__(self):
        return f"Torus(R={self.R:.4f}, r={self.r:.4f}, a={np.round(self.a, 4)}, c={np.round(self.c, 3)})"


def revolution_axis(Pc, N, w=None):
    """Axis of a surface of revolution from facet centroids + normals (normal lines all
    meet the axis). Plücker-coordinate least squares (Pottmann & Wallner)."""
    N = _unit(N)
    w = np.ones(len(N)) if w is None else np.asarray(w, float)
    Mo = np.cross(Pc, N)
    X = np.c_[Mo, N] * np.sqrt(w)[:, None]
    M = X.T @ X
    M11, M12, M21, M22 = M[:3, :3], M[:3, 3:], M[3:, :3], M[3:, 3:]
    K = np.linalg.pinv(M22, rcond=1e-10)
    S = M11 - M12 @ K @ M21
    _, ev = np.linalg.eigh(S)
    a = ev[:, 0]
    abar = -K @ M21 @ a
    p0 = np.cross(a, abar)
    return p0, _unit(a)


def fit_torus(P, N, Pc, w=None, gate=None):
    P = np.asarray(P, float)
    p0, a = revolution_axis(Pc, N, w)
    h = (P - p0) @ a
    rho = np.linalg.norm((P - p0) - np.outer(h, a), axis=1)
    (h0, R), r = _circle_fit_2d(np.c_[h, rho])
    c = p0 + a * h0
    tor = Torus(c, a, R, r)
    if gate is not None and np.abs(tor.dist(P)).max() > gate:
        return tor
    u0, v0 = _perp_basis(tor.a)

    def unpack(x):
        a_ = _unit(tor.a + x[0] * u0 + x[1] * v0)
        return tor.c + x[2:5], a_, tor.R + x[5], tor.r + x[6]

    def parts(x):
        araw = tor.a + x[0] * u0 + x[1] * v0
        nr = np.linalg.norm(araw)
        a_ = araw / nr
        c_ = tor.c + x[2:5]
        R_, r_ = tor.R + x[5], tor.r + x[6]
        wv = P - c_
        hh = wv @ a_
        d = wv - np.outer(hh, a_)
        rr = np.linalg.norm(d, axis=1)
        e = d / np.where(rr == 0, 1, rr)[:, None]
        q = np.sqrt((rr - R_) ** 2 + hh ** 2)
        q = np.where(q == 0, 1e-30, q)
        return a_, nr, R_, r_, wv, hh, rr, e, q

    def res(x):
        a_, nr, R_, r_, wv, hh, rr, e, q = parts(x)
        return q - abs(r_)

    def jac(x):
        a_, nr, R_, r_, wv, hh, rr, e, q = parts(x)
        J = np.empty((len(P), 7))
        for k, b in enumerate((u0, v0)):
            da = (b - a_ * (a_ @ b)) / nr
            J[:, k] = ((rr - R_) * (-hh * (e @ da)) + hh * (wv @ da)) / q
        J[:, 2:5] = -(((rr - R_) / q)[:, None] * e + (hh / q)[:, None] * a_)
        J[:, 5] = -(rr - R_) / q
        J[:, 6] = -np.sign(r_) if r_ != 0 else -1.0
        return J

    try:
        s = least_squares(res, np.zeros(7), jac=jac, method="lm", xtol=1e-12, ftol=1e-12, max_nfev=80)
        c_, a_, R_, r_ = unpack(s.x)
        t2 = Torus(c_, a_, R_, abs(r_))
        if np.abs(t2.dist(P)).max() <= np.abs(tor.dist(P)).max():
            tor = t2
    except Exception:
        pass
    return tor
