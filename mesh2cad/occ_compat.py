"""Small shims so the code runs on cadquery-ocp 7.8.x (OCCT 7.8) and 8.x (OCCT 8)."""
from OCP.TopoDS import TopoDS


def _cast(name):
    f = getattr(TopoDS, name + "_s", None)
    return f if f is not None else getattr(TopoDS, name)


to_vertex = _cast("Vertex")
to_edge = _cast("Edge")
to_wire = _cast("Wire")
to_face = _cast("Face")
to_shell = _cast("Shell")
to_solid = _cast("Solid")


def harray1_pnt(n):
    """1-based array of gp_Pnt of length n (TColgp_HArray1OfPnt on OCCT 7.x)."""
    try:
        from OCP.TColgp import TColgp_HArray1OfPnt
        return TColgp_HArray1OfPnt(1, n)
    except ImportError:
        from OCP.collections import HArray1_gp_Pnt
        return HArray1_gp_Pnt(1, n)
