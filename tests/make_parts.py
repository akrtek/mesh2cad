"""Generate known CAD parts with OCC, export as STL (like a CAD exporter would).
Each part exercises a feature class; ground truth (volume, face types) is saved."""
import json, sys, os
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakeSphere, BRepPrimAPI_MakeCone
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet, BRepFilletAPI_MakeChamfer
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer
from OCP.gp import gp_Pnt, gp_Ax2, gp_Dir, gp_Vec, gp_Trsf
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.GeomAbs import GeomAbs_Line
from OCP.GProp import GProp_GProps
from OCP.BRepGProp import BRepGProp
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mesh2cad.occ_compat import to_edge, to_face

def cyl(x, y, z, r, h, d=(0, 0, 1)):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(*d)), r, h).Shape()

def cut(a, b): return BRepAlgoAPI_Cut(a, b).Shape()
def fuse(a, b): return BRepAlgoAPI_Fuse(a, b).Shape()

def edges(s):
    e = TopExp_Explorer(s, TopAbs_EDGE); out = []
    while e.More(): out.append(to_edge(e.Current())); e.Next()
    return out

def fillet_vertical(s, r):
    mf = BRepFilletAPI_MakeFillet(s)
    for e in edges(s):
        c = BRepAdaptor_Curve(e)
        if c.GetType() == GeomAbs_Line and abs(c.Line().Direction().Z()) > 0.999: mf.Add(r, e)
    return mf.Shape()

def fillet_all_lines_at_z(s, r, z):
    mf = BRepFilletAPI_MakeFillet(s); n = 0
    for e in edges(s):
        c = BRepAdaptor_Curve(e)
        p0, p1 = c.Value(c.FirstParameter()), c.Value(c.LastParameter())
        if abs(p0.Z() - z) < 1e-6 and abs(p1.Z() - z) < 1e-6: mf.Add(r, e); n += 1
    return mf.Shape()

def parts():
    # 1 plate with through holes, a blind hole and an edge-breaking hole
    p = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60, 40, 10).Shape()
    for x, y in [(10, 10), (50, 10), (10, 30), (50, 30)]: p = cut(p, cyl(x, y, -1, 3, 12))
    p = cut(p, cyl(30, 20, 5, 6, 6)); p = cut(p, cyl(60, 20, -1, 4, 12))
    yield "plate_holes", p
    # 2 box with vertical fillets + top edge fillets (cylinders + spheres at corners... toroidal blends)
    b = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50, 30, 20).Shape(); b = fillet_vertical(b, 5)
    yield "box_vfillet", b
    b2 = fillet_all_lines_at_z(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50, 30, 20).Shape(), 3, 20)
    yield "box_topfillet", b2
    # 3 countersunk hole (cone) + chamfer
    c = BRepPrimAPI_MakeBox(gp_Pnt(-20, -20, 0), 40, 40, 8).Shape()
    c = cut(c, cyl(0, 0, -1, 2.5, 10))
    c = cut(c, BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(0, 0, 8 - 2.5), gp_Dir(0, 0, 1)), 2.5, 5.0, 2.5).Shape())
    yield "countersink", c
    # 4 boss with dome (sphere) on a base
    d = fuse(cyl(0, 0, 0, 15, 5), cyl(0, 0, 5, 8, 10))
    d = fuse(d, BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 15), 8).Shape())
    yield "boss_dome", d
    # 5 rounded-corner pocket with filleted rim (torus where rim fillet goes round the corners)
    t = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 60, 40, 15).Shape()
    pocket = fillet_vertical(BRepPrimAPI_MakeBox(gp_Pnt(10, 10, 5), 40, 20, 20).Shape(), 6)
    t = cut(t, pocket)
    t = fillet_all_lines_at_z(t, 1.5, 15) if False else t
    yield "pocket_rounded", t
    # 6 box with vertical fillets then the whole top rim filleted -> torus patches at corners
    a = fillet_vertical(BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 50, 30, 20).Shape(), 6)
    yield "box_allfillet", fillet_all_lines_at_z(a, 2, 20)
    # 7 freeform: lofted (B-spline) body on a block - must come back valid, freeform as facets
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeWire, BRepBuilderAPI_MakeEdge
    from OCP.gp import gp_Circ, gp_Elips
    loft = BRepOffsetAPI_ThruSections(True, False)
    for z, (ra, rb) in [(10, (12, 8)), (20, (6, 10)), (30, (10, 4))]:
        xd = gp_Dir(1, 0, 0) if ra >= rb else gp_Dir(0, 1, 0)
        el = gp_Elips(gp_Ax2(gp_Pnt(0, 0, z), gp_Dir(0, 0, 1), xd), max(ra, rb), min(ra, rb))
        loft.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(el).Edge()).Wire())
    loft.Build()
    yield "freeform_loft", fuse(BRepPrimAPI_MakeBox(gp_Pnt(-15, -15, 0), 30, 30, 10).Shape(), loft.Shape())

def tess_write(shape, path, lin=0.01, ang=0.35):
    BRepMesh_IncrementalMesh(shape, lin, False, ang, True)
    w = StlAPI_Writer(); w.ASCIIMode = False
    try: w.SetASCIIMode(False)
    except Exception: pass
    w.Write(shape, path)

def truth(shape):
    g = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, g)
    kinds = {}
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        k = str(BRepAdaptor_Surface(to_face(e.Current())).GetType()).split('_')[-1].split('.')[-1]
        kinds[k] = kinds.get(k, 0) + 1; e.Next()
    return {"volume": g.Mass(), "faces": kinds}

if __name__ == "__main__":
    out = sys.argv[1]; os.makedirs(out, exist_ok=True); meta = {}
    lin, ang = (0.1, 0.5) if "--coarse" in sys.argv else (0.01, 0.35)
    for name, s in parts():
        tess_write(s, f"{out}/{name}.stl", lin, ang); meta[name] = truth(s); print(name, meta[name])
    json.dump(meta, open(f"{out}/truth.json", "w"), indent=1)
