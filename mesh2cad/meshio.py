"""Mesh loading (STL/OBJ/PLY/OFF/GLB via trimesh, 3MF via a built-in parser that
understands the 3MF production extension and Bambu/Orca project files) and repair."""
from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import numpy as np
import trimesh

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


@dataclass
class Body:
    name: str
    V: np.ndarray
    F: np.ndarray


def _mat(s):
    if not s:
        return np.eye(4)
    a = [float(x) for x in s.split()]
    T = np.eye(4)
    T[:3, :3] = np.array(a[:9]).reshape(3, 3).T
    T[:3, 3] = a[9:12]
    return T


def _load_3mf(path):
    z = zipfile.ZipFile(path)
    names = set(z.namelist())
    unit_scale = {"micron": 1e-3, "millimeter": 1.0, "centimeter": 10.0, "inch": 25.4, "foot": 304.8, "meter": 1000.0}
    files = {}

    def parse(part):
        part = part.lstrip("/")
        if part not in files:
            root = ET.fromstring(z.read(part))
            files[part] = (root, unit_scale.get(root.get("unit", "millimeter"), 1.0))
        return files[part]

    # Bambu/Orca: negative parts / modifiers listed in Metadata/model_settings.config
    skip = set()
    part_names, obj_names = {}, {}
    if "Metadata/model_settings.config" in names:
        cfg = ET.fromstring(z.read("Metadata/model_settings.config"))
        for obj in cfg.iter("object"):
            for md in obj.findall("metadata"):
                if md.get("key") == "name":
                    obj_names[obj.get("id")] = md.get("value")
            for part in obj.iter("part"):
                if part.get("subtype", "normal_part") != "normal_part":
                    skip.add((obj.get("id"), part.get("id")))
                for md in part.findall("metadata"):
                    if md.get("key") == "name":
                        part_names[(obj.get("id"), part.get("id"))] = md.get("value")

    def objects(part):
        root, _ = parse(part)
        return {o.get("id"): o for o in root.iter(f"{{{NS_CORE}}}object")}

    bodies = []

    def emit(part, oid, T, name, top_id):
        o = objects(part)[oid]
        pn = part_names.get((top_id, oid))
        if pn and not (name and name.startswith(pn)):  # Bambu: "x.stl" part inside object "x.stl_A"
            name = pn
        mesh = o.find(f"{{{NS_CORE}}}mesh")
        _, scale = parse(part)
        if mesh is not None:
            V = np.array([[float(v.get(k)) for k in "xyz"] for v in mesh.find(f"{{{NS_CORE}}}vertices")]) * scale
            F = np.array([[int(t.get(k)) for k in ("v1", "v2", "v3")] for t in mesh.find(f"{{{NS_CORE}}}triangles")], dtype=np.int64)
            V = (np.c_[V, np.ones(len(V))] @ T.T)[:, :3]
            bodies.append(Body(name or o.get("name") or f"object{oid}", V, F))
        comps = o.find(f"{{{NS_CORE}}}components")
        if comps is not None:
            for c in comps:
                sub = c.get(f"{{{NS_PROD}}}path") or part
                if (top_id, c.get("objectid")) in skip:
                    continue
                emit(sub, c.get("objectid"), T @ _mat(c.get("transform")), name, top_id)

    root, _ = parse("3D/3dmodel.model")
    for item in root.iter(f"{{{NS_CORE}}}item"):
        if item.get("printable", "1") == "0":
            pass
        oid = item.get("objectid")
        obj = objects("3D/3dmodel.model")[oid]
        emit("3D/3dmodel.model", oid, _mat(item.get("transform")), obj_names.get(oid) or obj.get("name") or f"object{oid}", oid)
    return bodies


def load(path):
    p = str(path).lower()
    if p.endswith(".3mf"):
        bodies = _load_3mf(path)
    else:
        m = trimesh.load(path, force="mesh", process=False)
        bodies = [Body(re.sub(r"\.[^.]+$", "", str(path).split("/")[-1]), np.asarray(m.vertices, float), np.asarray(m.faces))]
    return bodies


# ----------------------------------------------------------------------------- repair

def _fix_slivers(V, F, area_eps):
    """Remove zero-area (collinear) triangles by flipping the long edge with its neighbour.
    Keeps the mesh watertight, unlike just deleting them."""
    for _ in range(10):
        m = trimesh.Trimesh(V, F, process=False)
        bad = np.where(m.area_faces < area_eps)[0]
        if len(bad) == 0:
            break
        edge_face = {}
        for fi, (a, b, c) in enumerate(F):
            for e in ((a, b), (b, c), (c, a)):
                edge_face[e] = fi
        F = F.copy()
        dead = set()
        changed = False
        for fi in bad:
            if fi in dead:
                continue
            tri = F[fi]
            P = V[tri]
            L = [np.linalg.norm(P[(i + 1) % 3] - P[i]) for i in range(3)]
            i = int(np.argmax(L))
            a, b, c = tri[i], tri[(i + 1) % 3], tri[(i + 2) % 3]  # a->b is the long edge, c lies on it
            gj = edge_face.get((b, a))
            if gj is None or gj in dead or gj == fi:
                continue
            g = list(F[gj])
            k = g.index(b)
            d = g[(k + 2) % 3]  # neighbour (b, a, d)
            if d == c:
                continue
            F[fi] = (a, c, d)
            F[gj] = (c, b, d)
            dead.update((fi, gj))
            changed = True
        if not changed:
            break
    return F


def repair(V, F, log=print):
    m = trimesh.Trimesh(V, F, process=True, validate=False)
    m.merge_vertices()
    m.update_faces(m.unique_faces())
    Fm = np.asarray(m.faces)
    m.update_faces((Fm[:, 0] != Fm[:, 1]) & (Fm[:, 1] != Fm[:, 2]) & (Fm[:, 2] != Fm[:, 0]))  # repeated-index faces
    diag = float(np.linalg.norm(m.extents))
    F2 = _fix_slivers(np.asarray(m.vertices), np.asarray(m.faces), (1e-7 * diag) ** 2)
    m = trimesh.Trimesh(m.vertices, F2, process=False)
    nondeg = m.area_faces > 0
    if (~nondeg).any():
        log(f"  {int((~nondeg).sum())} zero-area triangles remain (left in place)")
    if not m.is_winding_consistent or m.volume < 0:
        trimesh.repair.fix_normals(m)
    if not m.is_watertight:
        trimesh.repair.fill_holes(m)
    m.remove_unreferenced_vertices()
    return np.asarray(m.vertices, float), np.asarray(m.faces, np.int64), bool(m.is_watertight)
