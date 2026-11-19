"""Mesh2CAD - desktop app for mesh2cad (Tkinter).

Each file is converted by its own worker process (`python -m mesh2cad --progress`, or the
app binary itself with --mesh2cad-worker when frozen), so the window never freezes and Stop
ends a run at once. The worker streams "@@progress <0..1> <stage>" lines that drive the
progress bars; everything else goes to the activity log. Results come back through the
CLI's JSON report and fill the summary table.
"""
from __future__ import annotations

import json
import math
import os
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import FEEDBACK_URL, __version__, macos

MESH_EXTS = {".stl", ".3mf", ".obj", ".ply", ".off", ".glb"}
SURFACE_ORDER = ["plane", "cylinder", "cone", "sphere", "torus", "freeform", "other"]
WORKER_FLAG = "--mesh2cad-worker"


# ====================================================================== helpers
def clean_path_text(raw_text: str) -> str:
    """Strip shell quotes that Finder's Copy as Pathname / Terminal drag add."""
    text = raw_text.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text.replace("\\ ", " ")


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    n = 1
    while True:
        cand = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def mesh_files_in(folder: Path, recursive: bool):
    it = folder.rglob("*") if recursive else folder.iterdir()
    for p in sorted(it):
        if p.is_file() and p.suffix.lower() in MESH_EXTS and not p.name.startswith("._"):
            yield p


def surfaces_text(face_types: dict) -> str:
    return " · ".join(f"{face_types[k]} {k}" for k in SURFACE_ORDER if face_types.get(k))


def worker_command() -> list[str]:
    """How to start a conversion process (the frozen app re-runs itself in worker mode)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, WORKER_FLAG]
    return [sys.executable, "-m", "mesh2cad"]


def fmt_secs(s: float) -> str:
    s = int(max(s, 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def process_usage(pid: int):
    """(cpu %, memory MB) of a running process, or None. Uses `ps` (macOS / Linux)."""
    try:
        out = subprocess.run(["ps", "-o", "%cpu=,rss=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=3).stdout.split()
        return float(out[0]), float(out[1]) / 1024.0
    except Exception:
        return None


def reveal_in_file_manager(p: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(p)])
    elif sys.platform.startswith("win"):
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(Path(p).parent)])


def open_with_default_app(p: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    elif sys.platform.startswith("win"):
        os.startfile(str(p))  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(p)])


# ====================================================================== colours
def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(c) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in c)


def mix(a: str, b: str, t: float) -> str:
    """Colour t of the way from a to b (also used to fake translucency over a known background)."""
    A, B = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * t for x, y in zip(A, B)))


WINDOW_BG = "#30333b"      # lighter charcoal
TEXT = "#eef0f8"
MUTED = "#c3c8df"
ACCENT = "#7b8cff"         # periwinkle blue
ACCENT2 = "#a77dff"        # violet
WARN = "#ffcf7a"
ERR = "#ff9a9a"
# section cards step from deep blue to violet, a little lighter each time
CARD_FIRST, CARD_LAST = "#23418c", "#5d3ea6"   # solid blue -> violet


class Theme:
    def __init__(self, root: tk.Tk, n_cards: int):
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        self.cards = [mix(CARD_FIRST, CARD_LAST, i / max(n_cards - 1, 1)) for i in range(n_cards)]
        base = tkfont.nametofont("TkDefaultFont")
        fam, sz = base.actual("family"), abs(int(base.actual("size"))) or 13

        def font(size=sz, **kw):
            return tkfont.Font(root=root, family=fam, size=size, **kw)

        self.f_base, self.f_bold, self.f_small = font(), font(weight="bold"), font(size=sz - 1)
        self.f_header = font(size=sz + 1, weight="bold")
        self.f_title = font(size=sz + 9, weight="bold")
        self.f_button = font(size=sz + 2, weight="bold")
        fixed = tkfont.nametofont("TkFixedFont")
        self.f_mono = tkfont.Font(root=root, family=fixed.actual("family"), size=max(sz - 2, 9))
        self.row_h = int(self.f_base.metrics("linespace") * 1.65)

        root.configure(bg=WINDOW_BG)
        s = self.style
        s.configure(".", font=self.f_base, background=WINDOW_BG, foreground=TEXT)
        s.configure("Window.TLabel", background=WINDOW_BG, foreground=TEXT)
        s.configure("WindowMuted.TLabel", background=WINDOW_BG, foreground=MUTED, font=self.f_small)
        s.configure("Title.TLabel", background=WINDOW_BG, foreground="#ffffff", font=self.f_title)
        self._button("Window.TButton", WINDOW_BG)
        for i in range(n_cards):
            self._card(i)

    # ------------------------------------------------------------------ per-card styles
    def colors(self, i: int) -> dict:
        fill = self.cards[i]
        return dict(fill=fill,
                    inset=mix(fill, "#0b0d16", 0.42),           # list / table / log wells
                    border=mix(fill, "#ffffff", 0.17),          # light, low-contrast outer edge
                    divider=mix(fill, "#ffffff", 0.24),         # line between stacked sections
                    header=mix("#ffffff", ACCENT, 0.30))

    def _button(self, name: str, fill: str) -> None:
        s = self.style
        face, hover = mix(fill, "#ffffff", 0.12), mix(fill, "#ffffff", 0.20)
        edge = mix(fill, "#ffffff", 0.26)
        s.configure(name, background=face, foreground=TEXT, bordercolor=edge, lightcolor=face, darkcolor=face,
                    focuscolor=ACCENT, relief="flat", padding=(12, 4), font=self.f_base)
        s.map(name,
              background=[("disabled", mix(fill, "#ffffff", 0.05)), ("pressed", mix(fill, "#000000", 0.12)),
                          ("active", hover)],
              lightcolor=[("pressed", mix(fill, "#000000", 0.12)), ("active", hover)],
              darkcolor=[("pressed", mix(fill, "#000000", 0.12)), ("active", hover)],
              foreground=[("disabled", mix(TEXT, fill, 0.55))],
              bordercolor=[("focus", ACCENT)])

    def _card(self, i: int) -> None:
        s, c = self.style, self.colors(i)
        fill, inset, border = c["fill"], c["inset"], c["border"]
        p = f"C{i}"
        s.configure(f"{p}.TFrame", background=fill)
        s.configure(f"{p}.TLabel", background=fill, foreground=TEXT)
        s.configure(f"{p}.Muted.TLabel", background=fill, foreground=MUTED, font=self.f_small)
        s.configure(f"{p}.Bold.TLabel", background=fill, foreground=TEXT, font=self.f_bold)
        s.configure(f"{p}.Header.TLabel", background=fill, foreground=c["header"], font=self.f_header)
        self._button(f"{p}.TButton", fill)
        self._button(f"{p}.Small.TButton", fill)
        s.configure(f"{p}.Small.TButton", padding=(10, 1), font=self.f_small)
        for kind in ("TEntry", "TSpinbox"):
            s.configure(f"{p}.{kind}", fieldbackground=inset, foreground=TEXT, insertcolor=TEXT,
                        bordercolor=border, lightcolor=inset, darkcolor=inset, background=mix(fill, "#ffffff", 0.12),
                        arrowcolor=MUTED, selectbackground=ACCENT, selectforeground="#ffffff", padding=(6, 3))
            s.map(f"{p}.{kind}",
                  bordercolor=[("focus", ACCENT)],
                  fieldbackground=[("disabled", mix(inset, fill, 0.55))],
                  foreground=[("disabled", mix(TEXT, fill, 0.55))])
        s.configure(f"{p}.Vertical.TScrollbar", background=mix(fill, "#ffffff", 0.16), troughcolor=inset,
                    bordercolor=inset, lightcolor=mix(fill, "#ffffff", 0.16), darkcolor=mix(fill, "#ffffff", 0.16),
                    arrowcolor=MUTED, gripcount=0, arrowsize=11)
        s.map(f"{p}.Vertical.TScrollbar", background=[("active", mix(fill, "#ffffff", 0.28))])
        s.configure(f"{p}.Treeview", background=inset, fieldbackground=inset, foreground=TEXT, bordercolor=border,
                    lightcolor=inset, darkcolor=inset, rowheight=self.row_h, font=self.f_base)
        s.map(f"{p}.Treeview", background=[("selected", mix(inset, ACCENT, 0.55))],
              foreground=[("selected", "#ffffff")])
        head = mix(inset, "#ffffff", 0.07)
        s.configure(f"{p}.Treeview.Heading", background=head, foreground=MUTED, bordercolor=inset,
                    lightcolor=head, darkcolor=head, relief="flat", font=self.f_small, padding=(6, 4))
        s.map(f"{p}.Treeview.Heading", background=[("active", mix(inset, "#ffffff", 0.13))])


# ====================================================================== custom widgets
# Tk leaks a little memory for every canvas item created and deleted, so these widgets build
# their items once per size and afterwards only move or recolour them.
def _round_rect(x1, y1, x2, y2, r, r_bottom=None):
    """Points for a smoothed polygon with top corner radius r and bottom radius r_bottom.
    Doubled points keep the straight sides straight; a radius of 0 gives a sharp corner."""
    rb = r if r_bottom is None else r_bottom

    def corner(before, at, after, rad):
        return [*at, *at, *at] if rad <= 0 else [*before, *before, *at, *after, *after]

    return (corner((x1, y1 + r), (x1, y1), (x1 + r, y1), r) + corner((x2 - r, y1), (x2, y1), (x2, y1 + r), r)
            + corner((x2, y2 - rb), (x2, y2), (x2 - rb, y2), rb) + corner((x1 + rb, y2), (x1, y2), (x1, y2 - rb), rb))


class Card(tk.Canvas):
    """One section of the stacked column. Put content in `.body`; the card follows its height
    (or, with expand=True, gives the body whatever height it gets).

    position: "first" (rounded top), "middle", "last" (rounded bottom) or "only" (all rounded).
    Cards sit flush; a thin light line separates each section from the one above."""

    def __init__(self, parent, theme: Theme, index: int, title: str = "", hint: str = "", expand=False,
                 position="only", radius=12, pad=11):
        c = theme.colors(index)
        super().__init__(parent, bg=WINDOW_BG, highlightthickness=0, bd=0, height=40)
        self.fill, self.border, self.radius, self.pad, self.expand = c["fill"], c["border"], radius, pad, expand
        self.prefix, self.colors, self.position = f"C{index}", c, position
        self._rt = radius if position in ("first", "only") else 0
        self._rb = radius if position in ("last", "only") else 0
        self._bg = self.create_polygon(_round_rect(0, 0, 40, 40, self._rt, self._rb), smooth=True, fill=self.fill,
                                       outline=self.border if position == "only" else "")
        # outer edges of the column (sides, plus the rounded top or bottom); "middle" cards have two plain sides
        n = 2 if position == "middle" else (1 if position in ("first", "last") else 0)
        self._edges = [self.create_line(0, 0, 1, 1, fill=self.border, width=1, smooth=(position != "middle"))
                       for _ in range(n)]
        self._divider = (self.create_line(0, 0, 1, 0, fill=c["divider"], width=1)
                         if position in ("middle", "last") else None)
        self.body = tk.Frame(self, bg=self.fill, bd=0, highlightthickness=0)
        if title:
            head = tk.Frame(self.body, bg=self.fill)
            head.pack(fill="x", pady=(0, 6))
            ttk.Label(head, text=title, style=f"{self.prefix}.Header.TLabel").pack(side="left")
            if hint:
                ttk.Label(head, text=hint, style=f"{self.prefix}.Muted.TLabel").pack(side="left", padx=(10, 0))
            self.head = head
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._on_canvas)
        self.body.bind("<Configure>", self._on_body)

    def _on_body(self, _e=None):
        w = self.body.winfo_reqwidth() + 2 * self.pad
        h = self.body.winfo_reqheight() + 2 * self.pad
        if (w, h) != getattr(self, "_req", None):
            self._req = (w, h)
            self.configure(width=w)
            if not self.expand:
                self.configure(height=h)

    def _on_canvas(self, e):
        w, h = e.width, e.height
        self.itemconfigure(self._win, width=max(w - 2 * self.pad, 1))
        if self.expand:
            self.itemconfigure(self._win, height=max(h - 2 * self.pad, 1))
        if self.position == "only":
            self.coords(self._bg, *_round_rect(0.5, 0.5, w - 0.5, h - 0.5, self._rt, self._rb))
        else:
            self.coords(self._bg, *_round_rect(0, 0, w, h, self._rt, self._rb))
        x1, x2, r = 0.5, w - 0.5, self.radius
        if self.position == "first":   # up the left side, round the top, down the right side
            y = 0.5
            self.coords(self._edges[0], x1, h, x1, h, x1, r, x1, r, x1, y, x1 + r, y, x1 + r, y,
                        x2 - r, y, x2 - r, y, x2, y, x2, r, x2, r, x2, h, x2, h)
        elif self.position == "last":  # down the left side, round the bottom, up the right side
            y = h - 0.5
            self.coords(self._edges[0], x1, 0, x1, 0, x1, h - r, x1, h - r, x1, y, x1 + r, y, x1 + r, y,
                        x2 - r, y, x2 - r, y, x2, y, x2, h - r, x2, h - r, x2, 0, x2, 0)
        elif self.position == "middle":
            self.coords(self._edges[0], x1, 0, x1, h)
            self.coords(self._edges[1], x2, 0, x2, h)
        if self._divider is not None:
            self.coords(self._divider, 0, 0.5, w, 0.5)


def _capsule_columns(width, height, radius):
    """Top/bottom inset of each pixel column of a rounded shape `width` px long."""
    out = []
    for x in range(width):
        cx = x + 0.5
        dx = radius - cx if cx < radius else (cx - (width - radius) if cx > width - radius else 0.0)
        out.append(radius - math.sqrt(max(radius * radius - dx * dx, 0.0)) if dx > 0 else 0.0)
    return out


class GradientBar(tk.Canvas):
    """Rounded progress bar with a blue-to-violet gradient fill."""

    def __init__(self, parent, bg, trough, height=10):
        super().__init__(parent, height=height, bg=bg, highlightthickness=0, bd=0)
        self.trough, self.value = trough, 0.0
        self._cols, self._colors, self._shown, self._size = [], [], 0, None
        self.bind("<Configure>", lambda _e: self._build())

    def _build(self) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or (w, h) == self._size:
            return
        self._size = (w, h)
        self.delete("all")  # only on resize
        self._colors = [mix(ACCENT, ACCENT2, x / max(w - 1, 1)) for x in range(w)]
        self._cols = [self.create_line(x, dy, x, h - dy, fill=self.trough)
                      for x, dy in enumerate(_capsule_columns(w, h, h / 2.0))]
        self._shown = 0
        self._paint()

    def set(self, v: float) -> None:
        v = min(max(float(v), 0.0), 1.0)
        if abs(v - self.value) >= 0.001 or v in (0.0, 1.0):
            self.value = v
            self._paint()

    def _paint(self) -> None:
        n = int(round(len(self._cols) * self.value))
        if n > self._shown:
            for x in range(self._shown, n):
                self.itemconfigure(self._cols[x], fill=self._colors[x])
        elif n < self._shown:
            for x in range(n, self._shown):
                self.itemconfigure(self._cols[x], fill=self.trough)
        self._shown = n


class GradientButton(tk.Canvas):
    """The primary action button (rounded, gradient)."""

    def __init__(self, parent, text, command, bg, font, width=150, height=36):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.command, self.bgc = command, bg
        self.enabled, self._hover, self._down = True, False, False
        self._cols = []
        r = 9.0
        for x, dy in enumerate(_capsule_columns(width, height, r)):
            self._cols.append(self.create_line(x, dy, x, height - dy, fill=bg))
        self._text = self.create_text(width / 2, height / 2, text=text, font=font, fill="#ffffff")
        self._state = None
        self._paint()
        self.bind("<Enter>", lambda _e: self._set(hover=True))
        self.bind("<Leave>", lambda _e: self._set(hover=False, down=False))
        self.bind("<ButtonPress-1>", lambda _e: self._set(down=True))
        self.bind("<ButtonRelease-1>", self._release)

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self.configure(cursor="hand2" if on else "")
        self._paint()

    def _set(self, hover=None, down=None):
        if hover is not None:
            self._hover = hover
        if down is not None:
            self._down = down
        self._paint()

    def _release(self, e):
        inside = 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height()
        self._set(down=False)
        if inside and self.enabled and self.command:
            self.command()

    def _paint(self):
        state = (self.enabled, self._hover and self.enabled, self._down and self.enabled)
        if state == self._state:
            return
        self._state = state
        if self.enabled:
            a, b = ACCENT, ACCENT2
            if self._hover:
                a, b = mix(a, "#ffffff", 0.12), mix(b, "#ffffff", 0.12)
            if self._down:
                a, b = mix(a, "#000000", 0.15), mix(b, "#000000", 0.15)
            fg = "#ffffff"
        else:
            a = b = mix(self.bgc, "#ffffff", 0.10)
            fg = mix(TEXT, self.bgc, 0.55)
        n = len(self._cols)
        for x, item in enumerate(self._cols):
            self.itemconfigure(item, fill=mix(a, b, x / max(n - 1, 1)))
        self.itemconfigure(self._text, fill=fg)


class Toggle(tk.Frame):
    """Checkbox (value=None) or radio button with a crisp vector indicator.
    (ttk's clam theme draws a ticked box as an "✕", which reads as "off".)"""

    def __init__(self, parent, card, theme, text, variable, value=None, command=None):
        super().__init__(parent, bg=card.fill, cursor="hand2")
        self.var, self.value, self.command, self.c = variable, value, command, card.colors
        self.radio, self._hover = value is not None, False
        s = self.size = max(14, int(theme.f_base.metrics("linespace") * 0.9))
        c = self.ind = tk.Canvas(self, width=s, height=s, bg=card.fill, highlightthickness=0, bd=0)
        c.pack(side="left", padx=(0, 6))
        if self.radio:
            self._box = c.create_oval(1, 1, s - 2, s - 2, width=1.2)
            k = s * 0.32
            self._mark = c.create_oval(k, k, s - 1 - k, s - 1 - k, fill="#ffffff", outline="")
        else:
            self._box = c.create_polygon(_round_rect(1, 1, s - 2, s - 2, 4), smooth=True, width=1.2)
            self._mark = c.create_line(s * 0.26, s * 0.52, s * 0.43, s * 0.69, s * 0.75, s * 0.31, fill="#ffffff",
                                       width=max(2, s // 8), capstyle="round", joinstyle="round")
        self.lab = tk.Label(self, text=text, bg=card.fill, fg=TEXT, font=theme.f_base, bd=0, padx=0, pady=2)
        self.lab.pack(side="left")
        for w in (self, self.ind, self.lab):
            w.bind("<Button-1>", self._click)
            w.bind("<Enter>", lambda _e: self._set_hover(True))
            w.bind("<Leave>", lambda _e: self._set_hover(False))
        self._trace = variable.trace_add("write", lambda *_a: self._draw())
        self.bind("<Destroy>", self._untrace)
        self._draw()

    def _untrace(self, _e=None):
        try:
            self.var.trace_remove("write", self._trace)
        except (tk.TclError, ValueError):
            pass

    def on(self) -> bool:
        return self.var.get() == self.value if self.radio else bool(self.var.get())

    def _set_hover(self, h):
        if h != self._hover:
            self._hover = h
            self._draw()

    def _click(self, _e=None):
        if self.radio:
            self.var.set(self.value)
        else:
            self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        on, inset, fill = self.on(), self.c["inset"], self.c["fill"]
        face = ACCENT if on else (mix(inset, "#ffffff", 0.12) if self._hover else inset)
        edge = ACCENT if on else mix(fill, "#ffffff", 0.42)
        self.ind.itemconfigure(self._box, fill=face, outline=edge)
        self.ind.itemconfigure(self._mark, state="normal" if on else "hidden")


# ====================================================================== the app
class Mesh2CadApp:
    N_CARDS = 6  # input, output, options, run, summary, log

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Mesh2CAD")
        height = max(760, min(940, self.root.winfo_screenheight() - 110))
        self.root.geometry(f"1120x{height}")
        self.root.minsize(1000, 740)
        self.theme = Theme(root, self.N_CARDS)

        self.files: list[Path] = []
        self.paste_var = tk.StringVar()
        self.recursive_var = tk.BooleanVar(value=False)
        self.out_mode_var = tk.StringVar(value="beside")
        self.out_dir_var = tk.StringVar()
        self.no_overwrite_var = tk.BooleanVar(value=True)
        self.mode_var = tk.StringVar(value="prismatic")
        self.auto_tol_var = tk.BooleanVar(value=True)
        self.tol_var = tk.StringVar(value="0.005")
        self.angle_var = tk.StringVar(value="40")
        self.seconds_var = tk.StringVar(value="300")
        self.split_var = tk.BooleanVar(value=False)
        self.json_var = tk.BooleanVar(value=False)

        self._ui_queue: queue.Queue = queue.Queue()
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._row_paths: dict[str, Path] = {}
        self._totals = [0, 0, 0, 0]  # files, bodies, triangles, faces
        # run state (main thread only)
        self.running = False
        self._run_t0 = self._file_t0 = self._last_alive = 0.0
        self._file_i, self._file_n, self._file_frac = 0, 0, 0.0
        self._usage = None
        self._idle_since = None
        self._activity = None

        self._build_ui()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(1000, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        try:  # files dropped on the app icon in Finder / Dock (macOS)
            self.root.createcommand("::tk::mac::OpenDocument", self._open_documents)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        T = self.theme
        main = tk.Frame(self.root, bg=WINDOW_BG)
        main.pack(fill="both", expand=True, padx=16, pady=(10, 14))

        top = tk.Frame(main, bg=WINDOW_BG)
        top.pack(fill="x")
        ttk.Label(top, text="Mesh to CAD", style="Title.TLabel").pack(side="left")
        ttk.Label(top, text=f"   STL / 3MF / OBJ from CAD  →  STEP solids with real faces  ·  v{__version__}",
                  style="WindowMuted.TLabel").pack(side="left", pady=(8, 0))
        ttk.Button(top, text="Feedback / report a problem…", style="Window.TButton",
                   command=self.send_feedback).pack(side="right")
        top.pack_configure(pady=(0, 8))
        gap = dict(fill="x")  # sections stack flush into one column
        # --- input files
        card = Card(main, T, 0, "Input files", "add files or a folder, paste a path, or drop files on the app icon",
                    position="first")
        card.pack(**gap)
        b, p, c = card.body, card.prefix, card.colors
        row = tk.Frame(b, bg=card.fill)
        row.pack(fill="x")
        lb = tk.Frame(row, bg=c["border"], bd=0)
        lb.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(lb, bg=c["inset"])
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        self.file_list = tk.Listbox(inner, height=3, selectmode="extended", activestyle="none", bd=0,
                                    highlightthickness=0, relief="flat", bg=c["inset"], fg=TEXT,
                                    selectbackground=mix(c["inset"], ACCENT, 0.6), selectforeground="#ffffff",
                                    font=T.f_base)
        sb = ttk.Scrollbar(inner, orient="vertical", command=self.file_list.yview, style=f"{p}.Vertical.TScrollbar")
        self.file_list.configure(yscrollcommand=sb.set)
        self.file_list.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=4)
        sb.pack(side="left", fill="y")
        btns = tk.Frame(row, bg=card.fill)
        btns.pack(side="left", padx=(12, 0), anchor="n")
        for k, (text, cmd) in enumerate((("Add Files…", self.add_files), ("Add Folder…", self.add_folder),
                                         ("Remove Selected", self.remove_selected), ("Clear List", self.clear_files))):
            ttk.Button(btns, text=text, width=16, style=f"{p}.TButton", command=cmd).grid(
                row=k // 2, column=k % 2, padx=3, pady=2, sticky="ew")
        prow = tk.Frame(b, bg=card.fill)
        prow.pack(fill="x", pady=(8, 0))
        ttk.Label(prow, text="Or paste a path:", style=f"{p}.TLabel").pack(side="left")
        e = ttk.Entry(prow, textvariable=self.paste_var, style=f"{p}.TEntry")
        e.pack(side="left", fill="x", expand=True, padx=(10, 8))
        e.bind("<Return>", lambda _e: self.add_pasted())
        ttk.Button(prow, text="Add", style=f"{p}.TButton", command=self.add_pasted).pack(side="left")
        Toggle(prow, card, T, "Include subfolders", self.recursive_var).pack(side="left", padx=(14, 0))

        # --- output
        card = Card(main, T, 1, "Output", "your original mesh files are never changed", position="middle")
        card.pack(**gap)
        b, p = card.body, card.prefix
        r2 = tk.Frame(b, bg=card.fill)
        r2.pack(fill="x")
        Toggle(r2, card, T, "Next to each input file", self.out_mode_var, "beside").pack(side="left")
        Toggle(r2, card, T, "All to:", self.out_mode_var, "folder").pack(side="left", padx=(16, 0))
        ttk.Entry(r2, textvariable=self.out_dir_var, style=f"{p}.TEntry").pack(side="left", fill="x", expand=True,
                                                                             padx=(6, 6))
        ttk.Button(r2, text="Browse…", style=f"{p}.TButton", command=self.browse_out_dir).pack(side="left")
        Toggle(r2, card, T, "Never overwrite (save as name_1.step, …)", self.no_overwrite_var).pack(
            side="left", padx=(16, 0))

        # --- options
        card = Card(main, T, 2, "Conversion options", position="middle")
        card.pack(**gap)
        b, p = card.body, card.prefix
        # collapsible: a one-line summary stands in for the controls (saves height on smaller screens)
        self.opts_summary, self.opts_chevron = tk.StringVar(), tk.StringVar()
        ttk.Label(card.head, textvariable=self.opts_chevron, style=f"{p}.Muted.TLabel").pack(side="right")
        ttk.Label(card.head, textvariable=self.opts_summary, style=f"{p}.Muted.TLabel").pack(side="left", padx=(12, 0))
        for w in (card.head, *card.head.winfo_children()):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", lambda _e: self._toggle_options())
        grid = tk.Frame(b, bg=card.fill)
        grid.pack(fill="x")
        self._opts_grid, self._opts_head = grid, card.head
        lab = dict(style=f"{p}.TLabel")
        ttk.Label(grid, text="Mode", **lab).grid(row=0, column=0, sticky="w", pady=1)
        mrow = tk.Frame(grid, bg=card.fill)
        mrow.grid(row=0, column=1, sticky="w")
        for val, text in (("prismatic", "Prismatic: planes, holes, fillets, cones, spheres, tori (best)"),
                          ("planar", "Planar: merge flat faces only (fast)"),
                          ("faceted", "Faceted: one face per triangle")):
            Toggle(mrow, card, T, text, self.mode_var, val).pack(side="left", padx=(0, 18))
        ttk.Label(grid, text="Fit tolerance", **lab).grid(row=1, column=0, sticky="w", pady=1)
        trow = tk.Frame(grid, bg=card.fill)
        trow.grid(row=1, column=1, sticky="w")
        Toggle(trow, card, T, "Auto (measured from the mesh's own noise)", self.auto_tol_var,
               command=self._sync_tol).pack(side="left")
        ttk.Label(trow, text="   or", **lab).pack(side="left")
        self.tol_entry = ttk.Entry(trow, textvariable=self.tol_var, width=8, style=f"{p}.TEntry")
        self.tol_entry.pack(side="left", padx=6)
        ttk.Label(trow, text="mm  (raise for noisy or re-saved meshes, e.g. 0.005)",
                  style=f"{p}.Muted.TLabel").pack(side="left")
        ttk.Label(grid, text="Curve angle", **lab).grid(row=2, column=0, sticky="w", pady=1)
        arow = tk.Frame(grid, bg=card.fill)
        arow.grid(row=2, column=1, sticky="w")
        ttk.Spinbox(arow, from_=5, to=90, increment=5, textvariable=self.angle_var, width=5,
                    style=f"{p}.TSpinbox").pack(side="left")
        ttk.Label(arow, text="° between facets of one curved surface (raise for coarse meshes)",
                  style=f"{p}.Muted.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(arow, text="      Time budget", **lab).pack(side="left")
        ttk.Spinbox(arow, from_=0, to=3600, increment=30, textvariable=self.seconds_var, width=6,
                    style=f"{p}.TSpinbox").pack(side="left", padx=(8, 0))
        ttk.Label(arow, text="s per body (0 = no limit)", style=f"{p}.Muted.TLabel").pack(side="left", padx=(6, 0))
        crow = tk.Frame(grid, bg=card.fill)
        crow.grid(row=3, column=1, sticky="w", pady=(1, 0))
        Toggle(crow, card, T, "Split disconnected pieces into separate bodies", self.split_var).pack(side="left")
        Toggle(crow, card, T, "Also save a JSON report next to each STEP", self.json_var).pack(side="left",
                                                                                            padx=(22, 0))
        grid.columnconfigure(0, minsize=130)
        self._sync_tol()
        for v in (self.mode_var, self.auto_tol_var, self.tol_var, self.angle_var, self.seconds_var, self.split_var,
                  self.json_var):
            v.trace_add("write", lambda *_a: self._update_opts_summary())
        self._opts_open = True
        if self.root.winfo_screenheight() < 1050:  # start folded on laptop-height screens
            self._toggle_options()
        else:
            self._update_opts_summary()

        # --- run: actions + progress
        card = Card(main, T, 3, position="middle")
        card.pack(**gap)
        b, p, c = card.body, card.prefix, card.colors
        act = tk.Frame(b, bg=card.fill)
        act.pack(fill="x")
        self.run_button = GradientButton(act, "Convert", self.start_run, bg=card.fill, font=T.f_button,
                                         width=150, height=34)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(act, text="Stop", style=f"{p}.TButton", command=self.stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=(10, 0))
        ttk.Button(act, text="Clear Results", style=f"{p}.TButton", command=self.clear_results).pack(side="left",
                                                                                                   padx=(8, 0))
        ttk.Button(act, text="Show in Finder" if sys.platform == "darwin" else "Show File", style=f"{p}.TButton",
                   command=self.reveal_selected).pack(side="right")
        ttk.Button(act, text="Open STEP", style=f"{p}.TButton", command=self.open_selected).pack(side="right",
                                                                                              padx=(0, 8))
        self.status_var = tk.StringVar(value="Ready. Add files, then press Convert.")
        self.elapsed_var = tk.StringVar(value="")
        ttk.Label(act, textvariable=self.status_var, style=f"{p}.Bold.TLabel", width=1).pack(side="left", fill="x",
                                                                                             expand=True, padx=(16, 8))
        brow = tk.Frame(b, bg=card.fill)
        brow.pack(fill="x", pady=(8, 2))
        self.file_bar, self.file_pct = self._bar(brow, card, "This file")
        tk.Frame(brow, bg=card.fill, width=16).pack(side="left")
        self.all_bar, self.all_pct = self._bar(brow, card, "All files")
        drow = tk.Frame(b, bg=card.fill)
        drow.pack(fill="x", pady=(2, 0))
        self.heart_var = tk.StringVar(value="")
        self.heart_label = ttk.Label(drow, textvariable=self.heart_var, style=f"{p}.Muted.TLabel")
        self.heart_label.pack(side="right")
        ttk.Label(drow, textvariable=self.elapsed_var, style=f"{p}.Muted.TLabel").pack(side="right", padx=(0, 14))
        self.stage_var = tk.StringVar(value="")
        ttk.Label(drow, textvariable=self.stage_var, style=f"{p}.Muted.TLabel", width=1).pack(side="left", fill="x",
                                                                                             expand=True)

        # --- summary + activity log share the rest of the height (about 60 / 40), each with a minimum,
        # so neither disappears on a short screen
        area = tk.Frame(main, bg=WINDOW_BG, height=220)
        area.pack(fill="both", expand=True)
        sum_card = Card(area, T, 4, "Summary", "double-click a row to show the STEP file", expand=True,
                        position="middle")
        log_card = Card(area, T, 5, "Activity log", expand=True, position="last")

        def share(e):
            log_h = max(86, min(int(e.height * 0.42), e.height - 100))
            sum_card.place(x=0, y=0, width=e.width, height=e.height - log_h)
            log_card.place(x=0, y=e.height - log_h, width=e.width, height=log_h)

        area.bind("<Configure>", share)

        card = log_card
        b, p, c = card.body, card.prefix, card.colors
        ttk.Button(card.head, text="Copy Log", style=f"{p}.Small.TButton", command=self.copy_log).pack(side="right")
        lf = tk.Frame(b, bg=c["border"])
        lf.pack(fill="both", expand=True)
        li = tk.Frame(lf, bg=c["inset"])
        li.pack(fill="both", expand=True, padx=1, pady=1)
        self.log_box = tk.Text(li, height=2, state="disabled", font=T.f_mono, bg=c["inset"], fg=TEXT, bd=0,
                               highlightthickness=0, relief="flat", wrap="none", insertbackground=TEXT,
                               selectbackground=mix(c["inset"], ACCENT, 0.6), padx=8, pady=6)
        lsb = ttk.Scrollbar(li, orient="vertical", command=self.log_box.yview, style=f"{p}.Vertical.TScrollbar")
        self.log_box.configure(yscrollcommand=lsb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")
        self.log_box.tag_configure("dim", foreground=MUTED)

        # --- summary table
        card = sum_card
        b, p, c = card.body, card.prefix, card.colors
        self.totals_var = tk.StringVar(value="")
        ttk.Label(b, textvariable=self.totals_var, style=f"{p}.Bold.TLabel").pack(side="bottom", anchor="w",
                                                                                pady=(8, 0))
        tf = tk.Frame(b, bg=c["border"])
        tf.pack(fill="both", expand=True)
        ti = tk.Frame(tf, bg=c["inset"])
        ti.pack(fill="both", expand=True, padx=1, pady=1)
        cols = ("file", "body", "tris", "faces", "reduction", "surfaces", "dev", "mode", "time", "result")
        heads = ("File", "Body", "Triangles", "Faces", "Reduction", "Surfaces", "Max dev mm", "Mode", "Time s", "Result")
        widths = (150, 140, 75, 55, 90, 200, 85, 75, 55, 110)
        self.tree = ttk.Treeview(ti, columns=cols, show="headings", height=2, style=f"{p}.Treeview")
        for col, h, w in zip(cols, heads, widths):
            self.tree.heading(col, text=h)
            anchor = "e" if col in ("tris", "faces", "dev", "time") else "w"
            self.tree.column(col, width=w, minwidth=40, anchor=anchor, stretch=(col == "surfaces"))
        tsb = ttk.Scrollbar(ti, orient="vertical", command=self.tree.yview, style=f"{p}.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="left", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.reveal_selected())
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("err", foreground=ERR)

    def _bar(self, row, card, label):
        ttk.Label(row, text=label, style=f"{card.prefix}.Muted.TLabel").pack(side="left")
        bar = GradientBar(row, bg=card.fill, trough=card.colors["inset"], height=10)
        bar.pack(side="left", fill="x", expand=True, padx=(8, 6))
        pct = tk.StringVar(value="")
        ttk.Label(row, textvariable=pct, style=f"{card.prefix}.Muted.TLabel", width=4, anchor="e").pack(side="left")
        return bar, pct

    def _toggle_options(self) -> None:
        self._opts_open = not self._opts_open
        if self._opts_open:
            self._opts_head.pack_configure(pady=(0, 6))
            self._opts_grid.pack(fill="x")
        else:
            self._opts_grid.pack_forget()
            self._opts_head.pack_configure(pady=0)
        self._update_opts_summary()

    def _update_opts_summary(self) -> None:
        self.opts_chevron.set("Hide ▴" if self._opts_open else "Change ▾")
        if self._opts_open:
            self.opts_summary.set("")
            return
        tol = "auto tolerance" if self.auto_tol_var.get() else f"tolerance {self.tol_var.get()} mm"
        secs = self.seconds_var.get()
        parts = [self.mode_var.get().capitalize(), tol, f"curve {self.angle_var.get()}°",
                 "no time limit" if secs.strip() in ("0", "0.0") else f"budget {secs} s"]
        if self.split_var.get():
            parts.append("split pieces")
        if self.json_var.get():
            parts.append("JSON report")
        self.opts_summary.set(" · ".join(parts))

    def _sync_tol(self) -> None:
        self.tol_entry.state(["disabled"] if self.auto_tol_var.get() else ["!disabled"])

    # ------------------------------------------------------------------ queue (main thread)
    def _drain_ui_queue(self) -> None:
        try:
            for _ in range(500):
                kind, payload = self._ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "row":
                    values, tag, path = payload
                    iid = self.tree.insert("", tk.END, values=values, tags=(tag,) if tag else ())
                    if path:
                        self._row_paths[iid] = path
                    self.tree.see(iid)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "totals":
                    self.totals_var.set(payload)
                elif kind == "file":
                    self._file_i, self._file_n, self._file_frac = payload[0], payload[1], 0.0
                    self._file_t0 = self._last_alive = time.time()
                    self._usage, self._idle_since = None, None
                    self.stage_var.set("Starting…")
                    self._show_progress()
                elif kind == "progress":
                    frac, text = payload
                    self._file_frac = max(self._file_frac, frac)
                    if text:
                        self.stage_var.set(text)
                    self._last_alive = time.time()
                    self._show_progress()
                elif kind == "alive":
                    self._last_alive = time.time()
                elif kind == "usage":
                    self._usage = payload
                elif kind == "running":
                    self._set_running(payload)
                elif kind == "error":
                    messagebox.showerror(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _append_log(self, msg: str, tag: str | None = None) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert(tk.END, msg + "\n", (tag,) if tag else ())
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > 5000:  # keep the log from growing without bound in long sessions
            self.log_box.delete("1.0", f"{lines - 4000}.0")
        self.log_box.see(tk.END)
        self.log_box.configure(state="disabled")

    def _show_progress(self) -> None:
        f = self._file_frac
        self.file_bar.set(f)
        self.file_pct.set(f"{100 * f:.0f}%")
        if self._file_n:
            a = (self._file_i - 1 + f) / self._file_n
            self.all_bar.set(a)
            self.all_pct.set(f"{100 * a:.0f}%")

    def _set_running(self, on: bool) -> None:
        self.running = on
        self.run_button.set_enabled(not on)
        self.stop_button.state(["!disabled"] if on else ["disabled"])
        if on:
            self._run_t0 = time.time()
            self.file_bar.set(0)
            self.all_bar.set(0)
            self.file_pct.set("0%")
            self.all_pct.set("0%")
            self._activity = macos.begin_activity("Converting meshes to STEP")
        else:
            macos.end_activity(self._activity)
            self._activity = None
            self.heart_var.set("")
            if not self._cancel.is_set():
                self.file_bar.set(1.0 if self._file_n else 0)
                self.file_pct.set("100%" if self._file_n else "")
                self.all_bar.set(1.0 if self._file_n else 0)
                self.all_pct.set("100%" if self._file_n else "")
                self.stage_var.set("")
            self.elapsed_var.set(f"took {fmt_secs(time.time() - self._run_t0)}" if self._run_t0 else "")

    def _tick(self) -> None:
        """Once a second: elapsed time and a heartbeat that shows the worker is alive."""
        try:
            if self.running:
                now = time.time()
                el = f"this file {fmt_secs(now - self._file_t0)}" if self._file_t0 else ""
                if self._file_n > 1:
                    el += f" · total {fmt_secs(now - self._run_t0)}"
                self.elapsed_var.set(el)
                quiet = now - self._last_alive
                u = self._usage
                if u is not None:
                    cpu, mem = u
                    busy = cpu >= 5
                    self._idle_since = None if busy else (self._idle_since or now)
                    mem_s = f"{mem / 1024:.1f} GB" if mem >= 1024 else f"{mem:.0f} MB"
                    if self._idle_since and now - self._idle_since > 45 and quiet > 45:
                        self.heart_var.set(f"⚠ no activity for {fmt_secs(quiet)} — it may be stuck; Stop and report it")
                    else:
                        dot = "●" if int(now) % 2 else "○"
                        self.heart_var.set(f"{dot} working · CPU {cpu:.0f}% · {mem_s}")
                else:
                    dot = "●" if int(now) % 2 else "○"
                    self.heart_var.set(f"{dot} working · last update {fmt_secs(quiet)} ago")
        finally:
            self.root.after(1000, self._tick)

    def log(self, msg: str, tag: str | None = None) -> None:
        self._ui_queue.put(("log", (msg, tag)))

    # ------------------------------------------------------------------ inputs
    def _add_paths(self, paths) -> None:
        added = 0
        known = set(self.files)
        for raw in paths:
            p = Path(clean_path_text(str(raw))).expanduser()
            try:
                cands = list(mesh_files_in(p, self.recursive_var.get())) if p.is_dir() else [p]
            except OSError as ex:
                self.log(f"Could not read {p}: {ex}")
                continue
            for c in cands:
                if c.is_file() and c.suffix.lower() in MESH_EXTS and c not in known:
                    self.files.append(c)
                    known.add(c)
                    self.file_list.insert(tk.END, str(c))
                    added += 1
                elif not c.exists():
                    self.log(f"Not found: {c}")
        if added:
            self.file_list.see(tk.END)
            self.status_var.set(f"{len(self.files)} file(s) ready.")
        elif paths:
            self.status_var.set("No mesh files found there (looking for .stl .3mf .obj .ply .off .glb).")

    def _open_documents(self, *paths) -> None:
        self._add_paths(paths)

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            parent=self.root, title="Choose mesh files",
            filetypes=[("Meshes", "*.stl *.STL *.3mf *.3MF *.obj *.OBJ *.ply *.off *.glb"), ("All files", "*")])
        self._add_paths(paths)

    def add_folder(self) -> None:
        d = filedialog.askdirectory(parent=self.root, title="Choose a folder of meshes")
        if d:
            self._add_paths([d])

    def add_pasted(self) -> None:
        if self.paste_var.get().strip():
            self._add_paths([self.paste_var.get()])
            self.paste_var.set("")

    def remove_selected(self) -> None:
        for i in reversed(self.file_list.curselection()):
            self.file_list.delete(i)
            del self.files[i]

    def clear_files(self) -> None:
        self.file_list.delete(0, tk.END)
        self.files.clear()

    def browse_out_dir(self) -> None:
        d = filedialog.askdirectory(parent=self.root, title="Save STEP files to")
        if d:
            self.out_dir_var.set(d)
            self.out_mode_var.set("folder")

    def clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._row_paths.clear()
        self._totals = [0, 0, 0, 0]
        self.totals_var.set("")
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", tk.END)
        self.log_box.configure(state="disabled")
        if not self.running:
            self._file_n = 0
            for bar, pct in ((self.file_bar, self.file_pct), (self.all_bar, self.all_pct)):
                bar.set(0)
                pct.set("")
            self.stage_var.set("")
            self.elapsed_var.set("")
            self.status_var.set(f"{len(self.files)} file(s) ready." if self.files else "Ready.")

    def copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log_box.get("1.0", "end-1c"))
        self.status_var.set("Activity log copied to the clipboard.")

    # ------------------------------------------------------------------ run
    def _settings(self):
        try:
            angle = float(self.angle_var.get())
            secs = float(self.seconds_var.get())
            tol = None if self.auto_tol_var.get() else float(self.tol_var.get())
        except ValueError:
            raise ValueError("Tolerance, curve angle and time budget must be numbers.")
        if tol is not None and not (0 < tol <= 1):
            raise ValueError("Tolerance should be between 0 and 1 mm (typical: 0.001 – 0.01).")
        if not (1 <= angle <= 90):
            raise ValueError("Curve angle should be between 1 and 90 degrees.")
        if secs < 0:
            raise ValueError("Time budget can't be negative (0 = no limit).")
        out_dir = None
        if self.out_mode_var.get() == "folder":
            od = clean_path_text(self.out_dir_var.get())
            if not od or not Path(od).is_dir():
                raise ValueError("Choose an existing output folder (or save next to the input files).")
            out_dir = Path(od)
        return dict(mode=self.mode_var.get(), tol=tol, angle=angle, secs=secs, split=self.split_var.get(),
                    json=self.json_var.get(), out_dir=out_dir, no_overwrite=self.no_overwrite_var.get())

    def start_run(self) -> None:
        if self.running:
            return
        if not self.files:
            messagebox.showinfo("Mesh2CAD", "Add at least one mesh file first.")
            return
        try:
            st = self._settings()
        except ValueError as ex:
            messagebox.showerror("Mesh2CAD", str(ex))
            return
        self._cancel.clear()
        self._set_running(True)
        threading.Thread(target=self._run, args=(list(self.files), st), daemon=True).start()

    def stop_run(self) -> None:
        self._cancel.set()
        self._kill_worker()
        self.log("Stopping…")

    def _kill_worker(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def _on_close(self) -> None:
        if self.running and not messagebox.askyesno("Mesh2CAD", "A conversion is still running. Stop it and quit?"):
            return
        self._cancel.set()
        self._kill_worker()
        self.root.destroy()

    def _watch_usage(self, proc: subprocess.Popen) -> None:
        """Background: report the worker's CPU and memory every 2 s while it runs."""
        while proc.poll() is None and not self._cancel.is_set():
            u = process_usage(proc.pid)
            if u is not None:
                self._ui_queue.put(("usage", u))
            time.sleep(2)

    def _run(self, files, st) -> None:
        done = 0
        try:
            done = self._run_files(files, st)
        except Exception:  # never leave the window stuck in "running"
            self.log("Unexpected error in the app:\n" + traceback.format_exc())
        finally:
            self._proc = None
            msg = "Stopped." if self._cancel.is_set() else f"Done: {done} of {len(files)} file(s) converted."
            self._ui_queue.put(("status", msg))
            self.log(msg)
            self._ui_queue.put(("running", False))

    def _run_files(self, files, st) -> int:
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        done = 0
        for i, src in enumerate(files, 1):
            if self._cancel.is_set():
                break
            out_dir = st["out_dir"] or src.parent
            out = out_dir / f"{src.stem}.step"
            if st["no_overwrite"]:
                out = unique_path(out)
            self._ui_queue.put(("file", (i, len(files), src.name)))
            self._ui_queue.put(("status", f"Converting {i} of {len(files)}: {src.name}"))
            self.log(f"── {src.name} → {out.name}")
            fd, rep_path = tempfile.mkstemp(suffix=".json", prefix="mesh2cad_")
            os.close(fd)
            cmd = worker_command() + [str(src), "-o", str(out), "--mode", st["mode"], "--angle", str(st["angle"]),
                                      "--max-seconds", str(st["secs"]), "--report", rep_path, "--progress"]
            if st["tol"] is not None:
                cmd += ["--tol", str(st["tol"])]
            if st["split"]:
                cmd.append("--split")
            rc = -1
            try:
                kw = {}
                if sys.platform.startswith("win"):
                    kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                              encoding="utf-8", errors="replace", bufsize=1, env=env, **kw)
                threading.Thread(target=self._watch_usage, args=(self._proc,), daemon=True).start()
                for line in self._proc.stdout:
                    line = line.rstrip()
                    if line.startswith("@@progress "):
                        val, _, text = line[len("@@progress "):].partition("\t")
                        try:
                            self._ui_queue.put(("progress", (float(val), text)))
                        except ValueError:
                            pass
                    elif line:
                        self.log("   " + line, "dim" if line.lstrip().startswith("time:") else None)
                    else:
                        self._ui_queue.put(("alive", None))
                rc = self._proc.wait()
            except Exception as ex:  # noqa
                self.log(f"   could not start the conversion: {ex}")
            reports = []
            try:
                txt = Path(rep_path).read_text()
                reports = json.loads(txt) if txt.strip() else []
            except Exception:
                reports = []
            finally:
                try:
                    os.unlink(rep_path)
                except OSError:
                    pass
            if self._cancel.is_set():
                self._row(src, "—", "", "", "", "", "", "", "", "stopped", "err", None)
                break
            if rc != 0 and not reports:
                self._row(src, "—", "", "", "", "", "", "", "", "failed (see log)", "err", None)
            for r in reports:
                if "error" in r:
                    self._row(src, r.get("body", ""), "", "", "", "", "", "", "", "failed: " + r["error"][:40], "err",
                              None)
                    continue
                tris, faces = int(r.get("triangles", 0)), int(r.get("faces", 0))
                red = f"{100 * (1 - faces / tris):.1f}% fewer" if tris else ""
                mode = r.get("mode", "")
                if not r.get("closed_solid", False):
                    result, tag = "⚠ not closed", "warn"
                elif mode != st["mode"]:
                    result, tag = f"✓ solid ({mode} fallback)", "warn"
                else:
                    result, tag = "✓ solid", ""
                dev = r.get("deviation_mm")
                self._row(src, r.get("body", ""), f"{tris:,}", f"{faces:,}", red,
                          surfaces_text(r.get("face_types", {})), f"{dev:.4f}" if dev is not None else "",
                          mode, f"{r.get('seconds', 0):.1f}", result, tag, out)
                self._totals[1] += 1
                self._totals[2] += tris
                self._totals[3] += faces
            if reports and out.exists():
                done += 1
                self._totals[0] += 1
                if st["json"]:
                    jp = out.with_suffix(".json")
                    jp = unique_path(jp) if st["no_overwrite"] else jp
                    jp.write_text(json.dumps(reports, indent=1))
            self._update_totals()
        return done

    def _row(self, src, body, tris, faces, red, surf, dev, mode, secs, result, tag, out) -> None:
        self._ui_queue.put(("row", ((src.name, body, tris, faces, red, surf, dev, mode, secs, result), tag, out)))

    def _update_totals(self) -> None:
        f, b, t, fa = self._totals
        if b and t:
            self._ui_queue.put(("totals", f"Total: {f} file(s), {b} bod{'y' if b == 1 else 'ies'} — "
                                          f"{t:,} triangles → {fa:,} faces ({100 * (1 - fa / t):.1f}% fewer)"))

    # ------------------------------------------------------------------ results / feedback
    def _selected_path(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Mesh2CAD", "Select a row in the summary first.")
            return None
        p = self._row_paths.get(sel[0])
        if not p or not Path(p).exists():
            messagebox.showinfo("Mesh2CAD", "That row has no STEP file.")
            return None
        return p

    def reveal_selected(self) -> None:
        p = self._selected_path()
        if p:
            reveal_in_file_manager(p)

    def open_selected(self) -> None:
        p = self._selected_path()
        if p:
            open_with_default_app(p)

    def diagnostics(self) -> str:
        try:
            st = self._settings()
            st["out_dir"] = "folder" if st["out_dir"] else "beside input"
        except ValueError as ex:
            st = {"settings": f"invalid ({ex})"}
        log = self.log_box.get("1.0", "end-1c").splitlines()[-150:]
        return "\n".join([
            f"Mesh2CAD {__version__} ({'app' if getattr(sys, 'frozen', False) else 'source'})",
            f"{platform.platform()} · {platform.machine()} · Python {platform.python_version()} · "
            f"Tk {self.root.tk.call('info', 'patchlevel')}",
            f"Settings: {json.dumps(st)}",
            "", "Activity log (last 150 lines):", "```", *log, "```"])

    def send_feedback(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.diagnostics())
        webbrowser.open(FEEDBACK_URL)
        messagebox.showinfo(
            "Mesh2CAD feedback",
            "Your browser is opening the feedback page.\n\nYour version, settings and activity log were copied to "
            "the clipboard: paste them into the report (⌘V / Ctrl+V). If a model fails, attaching it helps a lot.")


def _selftest(app: Mesh2CadApp, src: Path, out_json: Path, timeout: float = 900) -> None:
    """Build check: convert one file through the whole window -> worker path, save the result, quit."""
    out_dir = Path(tempfile.mkdtemp(prefix="mesh2cad_selftest_"))
    app.out_dir_var.set(str(out_dir))
    app.out_mode_var.set("folder")
    app._add_paths([str(src)])
    t0 = time.time()

    def poll():
        if app.running and time.time() - t0 < timeout:
            app.root.after(500, poll)
            return
        result = {"version": __version__, "tk": str(app.root.tk.call("info", "patchlevel")),
                  "status": app.status_var.get(), "progress": app._file_frac,
                  "rows": [[str(v) for v in app.tree.item(i)["values"]] for i in app.tree.get_children()],
                  "outputs": sorted(p.name for p in out_dir.iterdir()),
                  "log": app.log_box.get("1.0", "end-1c").splitlines()[-40:]}
        out_json.write_text(json.dumps(result, indent=1))
        hold = float(os.environ.get("MESH2CAD_SELFTEST_HOLD", "0.3"))  # seconds to leave the window up
        app.root.after(int(hold * 1000), app.root.destroy)

    def start():
        app.start_run()
        app.root.after(500, poll)

    app.root.after(1500, start)


def main() -> None:
    root = tk.Tk()
    app = Mesh2CadApp(root)
    args = [a for a in sys.argv[1:] if not a.startswith("-psn")]  # macOS may pass a process serial number
    if os.environ.get("MESH2CAD_SELFTEST"):
        _selftest(app, Path(os.environ["MESH2CAD_SELFTEST"]),
                  Path(os.environ.get("MESH2CAD_SELFTEST_OUT", "mesh2cad_selftest.json")))
    elif args:
        app._add_paths(args)
    root.mainloop()


if __name__ == "__main__":
    main()
