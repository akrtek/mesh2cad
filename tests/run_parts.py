import json, sys, os, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mesh2cad import meshio, convert, brep
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_FACE
from OCP.BRepAdaptor import BRepAdaptor_Surface
from mesh2cad.occ_compat import to_face
d = sys.argv[1]; truth = json.load(open(f"{d}/truth.json"))
only = sys.argv[2:] or list(truth)
for name in only:
    b = meshio.load(f"{d}/{name}.stl")[0]
    shape, rep = convert.convert_body(b.V, b.F, log=lambda *a: None)
    kinds = collections.Counter()
    e = TopExp_Explorer(shape, TopAbs_FACE)
    while e.More():
        kinds[str(BRepAdaptor_Surface(to_face(e.Current())).GetType()).split('_')[-1].split('.')[-1]] += 1; e.Next()
    tv = truth[name]["volume"]
    print(f"{name:15s} tris {rep['triangles']:6d} mode {rep.get('mode'):9s} closed-solid {rep['closed_solid']} faces {rep['faces']:4d} {dict(kinds)} | truth {truth[name]['faces']} | vol err {100*(rep['volume']-tv)/tv:+.3f}% | {rep['seconds']}s")
