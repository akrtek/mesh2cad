"""macOS helpers: keep App Nap from throttling a long conversion.

When the window is in the background, macOS "App Nap" slows the app's timers, so the
log and progress bar can freeze until you click the window again. While a conversion
runs we tell macOS the work is user-initiated (same idea as the `appnope` package),
using only ctypes. Every call is best-effort and a no-op on other systems.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

_NSActivityIdleSystemSleepDisabled = 1 << 20
_NSActivityUserInitiated = 0x00FFFFFF | _NSActivityIdleSystemSleepDisabled

_objc = None


def _load():
    global _objc
    if _objc is None:
        _objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(ctypes.util.find_library("Foundation"))
        _objc.objc_getClass.restype = ctypes.c_void_p
        _objc.sel_registerName.restype = ctypes.c_void_p
        _objc.objc_msgSend.restype = ctypes.c_void_p
    return _objc


def _send(obj, sel, *args, argtypes=()):
    o = _load()
    o.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p, *argtypes]
    return o.objc_msgSend(obj, o.sel_registerName(sel), *args)


def begin_activity(reason: str = "Converting meshes"):
    """Start a user-initiated activity (no App Nap, no idle sleep). Returns a token or None."""
    if sys.platform != "darwin":
        return None
    try:
        o = _load()
        info = _send(o.objc_getClass(b"NSProcessInfo"), b"processInfo")
        text = _send(o.objc_getClass(b"NSString"), b"stringWithUTF8String:", reason.encode(),
                     argtypes=(ctypes.c_char_p,))
        token = _send(info, b"beginActivityWithOptions:reason:", ctypes.c_uint64(_NSActivityUserInitiated),
                      ctypes.c_void_p(text), argtypes=(ctypes.c_uint64, ctypes.c_void_p))
        if token:
            _send(token, b"retain")
        return token
    except Exception:
        return None


def end_activity(token) -> None:
    if not token or sys.platform != "darwin":
        return
    try:
        o = _load()
        info = _send(o.objc_getClass(b"NSProcessInfo"), b"processInfo")
        _send(info, b"endActivity:", ctypes.c_void_p(token), argtypes=(ctypes.c_void_p,))
        _send(token, b"release")
    except Exception:
        pass
