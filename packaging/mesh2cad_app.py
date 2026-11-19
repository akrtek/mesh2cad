"""Entry point of the packaged app (PyInstaller).

    Mesh2CAD                              opens the window
    Mesh2CAD --mesh2cad-worker <cli args> runs one conversion (the window starts these)

The window runs every conversion in its own process so it never freezes; in the
packaged app that process is this same binary in worker mode.
"""
import os
import sys


def _ensure_stdio() -> None:
    # Windowed builds can start without stdout/stderr objects; the worker's output goes
    # to the pipe the window gave it, so reattach to those file descriptors.
    for name, fd in (("stdout", 1), ("stderr", 2)):
        stream = getattr(sys, name)
        if stream is None:
            try:
                setattr(sys, name, open(fd, "w", encoding="utf-8", buffering=1, closefd=False))
            except OSError:
                setattr(sys, name, open(os.devnull, "w"))
        elif hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # the window reads UTF-8


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mesh2cad-worker":
        _ensure_stdio()
        from mesh2cad.cli import main as cli_main

        sys.exit(cli_main(sys.argv[2:]))
    from mesh2cad.gui import main as gui_main

    gui_main()
