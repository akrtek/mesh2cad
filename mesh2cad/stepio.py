"""STEP output: one solid per body, named, in millimetres."""
from __future__ import annotations

from OCP.BRep import BRep_Builder
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopoDS import TopoDS_Compound


import contextlib
import os
import sys


def _flush_c_stdio():
    """Flush the C library's stdout buffer (OCC prints through it), best effort."""
    import ctypes
    for name in ((None,) if not sys.platform.startswith("win") else ("ucrtbase", "msvcrt")):
        try:
            ctypes.CDLL(name).fflush(None)
        except Exception:
            pass


@contextlib.contextmanager
def _quiet_stdout():
    """OCC's STEP writer prints transfer statistics straight to fd 1."""
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
        _flush_c_stdio()
        fd = os.dup(1)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
    except OSError:
        yield
        return
    try:
        yield
    finally:
        _flush_c_stdio()  # OCC's buffered text goes to /dev/null, not into our log later
        os.dup2(fd, 1)
        os.close(fd)
        os.close(devnull)


def write_step(shapes, path, names=None):
    with _quiet_stdout():
        return _write_step(shapes, path, names)


def _write_step(shapes, path, names=None):
    """shapes: list of TopoDS_Shape. Tries XDE (named bodies) first, falls back to plain."""
    try:
        return _write_named(shapes, path, names or [f"Body{i + 1}" for i in range(len(shapes))])
    except Exception:
        pass
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    b = BRep_Builder()
    comp = TopoDS_Compound()
    b.MakeCompound(comp)
    for s in shapes:
        b.Add(comp, s)
    w = STEPControl_Writer()
    w.Transfer(comp if len(shapes) > 1 else shapes[0], STEPControl_AsIs)
    return str(w.Write(str(path))).endswith("RetDone")


def _write_named(shapes, path, names):
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool
    doc = TDocStd_Document(TCollection_ExtendedString("XmlOcaf"))
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    for s, n in zip(shapes, names):
        lab = st.AddShape(s, False)
        TDataStd_Name.Set_s(lab, TCollection_ExtendedString(str(n)))
    Interface_Static.SetCVal_s("write.step.schema", "AP214")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    if not w.Transfer(doc, STEPControl_AsIs):
        raise RuntimeError("XDE transfer failed")
    return str(w.Write(str(path))).endswith("RetDone")
