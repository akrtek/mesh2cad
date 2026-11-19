# Mesh2CAD

Turn an STL / 3MF / OBJ that was exported from CAD back into a **STEP solid with real CAD faces**:

- one flat face per flat surface
- true cylinders for holes and fillets
- cones for countersinks and chamfers
- spheres and tori for rounded corners

It does the same job as Fusion's *Generate Face Groups → Convert Mesh (Prismatic)*, which the free Personal license doesn't include. It runs on your own computer, offline.

Open the `.step` in Fusion (Data panel → Upload, or File → Open) and it comes in as an ordinary solid. You can sketch on its faces, project hole edges as true circles, and use Press Pull, Delete Face and Fillet. There are no faceted triangles anywhere.

## Download the app

Get the latest version from **[Releases](https://github.com/akrtek/mesh2cad/releases/latest)**:

| Your computer | Download |
| --- | --- |
| Mac with Apple Silicon (M1, M2, M3, M4 …), macOS 14+ | `Mesh2CAD-…-macOS-AppleSilicon.dmg` |
| Mac with an Intel processor, macOS 14+ | `Mesh2CAD-…-macOS-Intel.dmg` |
| Windows 10 / 11 (64-bit) | `Mesh2CAD-…-Windows.zip` |

To check which Mac you have, click the Apple menu  → **About This Mac** and look at "Chip" or "Processor". The download is about 300 MB because it includes the OpenCASCADE geometry kernel.

### First launch on a Mac (one time only)

The app isn't notarized by Apple yet (that needs a paid developer account), so macOS blocks it the first time:

1. Open the `.dmg` and drag **Mesh2CAD** onto **Applications**.
2. Open Mesh2CAD. macOS says it can't verify the developer: click **Done**.
3. Open **System Settings → Privacy & Security**, scroll down to *"Mesh2CAD was blocked…"*, click **Open Anyway**, and confirm.

After that it opens normally. Terminal users can run this instead:
`xattr -dr com.apple.quarantine /Applications/Mesh2CAD.app`

### First launch on Windows

Unzip the whole folder, keep the files together, and run **Mesh2CAD.exe**. If SmartScreen appears, click **More info → Run anyway**.

## Using it

1. **Add files.** Use Add Files… or Add Folder…, paste a path, or drop meshes onto the app's icon. Bambu Studio / OrcaSlicer `.3mf` projects work: every body comes out as a named solid.
2. **Choose where the STEP goes.** It saves next to each input or into one folder. *Never overwrite* (on by default) saves `name_1.step` instead of replacing a file. Your original meshes are never changed.
3. **Press Convert.**
   - The bars show progress for the current file and the whole batch.
   - The line underneath says what the converter is doing.
   - The heartbeat on the right shows its CPU use, so a long curved-surface search never looks frozen.
4. **Check the Summary.** Each body shows triangles → faces, the surfaces found (e.g. `8 plane · 6 cylinder`), the maximum deviation from the mesh, and the result. Double-click a row to show the STEP file.

| Option | What it does |
| --- | --- |
| Prismatic (default) | Fits planes, cylinders, cones, spheres and tori. |
| Planar | Merges flat faces only. Fast; curved areas stay as flat facets. |
| Faceted | One face per triangle, like Fusion's Faceted method. |
| Fit tolerance | Auto measures the mesh's own noise. Raise it for noisy or re-saved meshes, e.g. 0.005 mm. |
| Curve angle | Largest angle between neighbouring facets of one curved surface (default 40°). Raise it for very coarse meshes. |
| Time budget | Seconds per body for the curved-surface search (default 300). Anything not reached stays as flat facets. |

## Feedback and bug reports

This is a test version, so reports are very welcome. In the app, click **Feedback / report a problem…**. It copies your version, settings and the activity log to the clipboard and opens the [feedback form](https://github.com/akrtek/mesh2cad/issues/new/choose); paste them in. If a model fails, attaching it (or a MakerWorld / Printables link) helps most.

## Command line

```
$ mesh2cad bracket.stl
[0] bracket: 1348 triangles
    -> 14 faces (prismatic: 8 plane, 6 cylinder), max deviation 0.0074 mm, 0.6 s
wrote bracket.step (1 body)
```

```bash
mesh2cad part.stl                         # -> part.step
mesh2cad part.3mf -o out.step             # every body in the 3MF, one solid each
mesh2cad part.3mf --list                  # see the bodies first
mesh2cad part.3mf --body 1                # just body 1
mesh2cad part.stl --split                 # one STL with several loose pieces -> separate solids
mesh2cad part.stl --mode planar           # only merge flat faces (fast, never fits curves)
mesh2cad part.stl --report report.json    # per-body stats
```

`--tol`, `--angle` and `--max-seconds` match the app's options. `--progress` prints the machine-readable progress lines the app reads.

### Install from source (macOS, Windows, Linux)

You need Python 3.11–3.14.

```bash
git clone https://github.com/akrtek/mesh2cad.git && cd mesh2cad
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install .
mesh2cad --version
mesh2cad-gui                                           # the desktop app
```

With [uv](https://docs.astral.sh/uv/) it's one line: `uv tool install git+https://github.com/akrtek/mesh2cad`. That gives you the `mesh2cad` and `mesh2cad-gui` commands.

## How it works

It follows the same two steps Fusion uses, "face groups" then "prismatic conversion", but it was written from scratch, not copied from Fusion.

1. **Load and repair.** Weld duplicate vertices, flip away zero-area sliver triangles while keeping the mesh closed, and fix the winding.
2. **Measure the noise.** CAD exporters put every vertex exactly on the true surface, so any scatter on the large flat faces is numeric noise from exporting or re-saving. The fit tolerance is set to about 5× that (usually 0.0001–0.004 mm).
3. **Find flat regions.** Grow regions across neighbouring triangles that stay within tolerance of one plane. This also merges the long coplanar strips CAD tessellators make on curved surfaces.
4. **Find curved surfaces.** Grow over smoothly-joined regions, fitting a cylinder, cone, sphere or torus to the vertices. Several checks keep it from fooling itself:
   - **Over-determined fits only.** Any three points fit a circle, so a fit must be confirmed by extra data.
   - **Edge check.** When two shapes fit equally well, the triangle edges decide.
   - **Angular-span check.** Pieces of one curved surface span similar angles.
   - **Chord-depth check.** Every curved surface in an export uses the same chord tolerance, so a "surface" whose facets sit 50× deeper than the others is wrong.
5. **Snap.** Move every vertex exactly onto all the surfaces it touches. Corners land on exact plane intersections, and hole rims land exactly on both the deck and the cylinder.
6. **Build the B-rep.** One face per group and one edge per boundary between groups. Each edge is an exact line, circle or arc where it can be, otherwise a smooth spline. Then sew into a shell, make a solid, and validate it.
7. **Check itself.** Each curved face is compared with the triangles it replaces; any face that strays becomes flat facets for just that area. The whole result is checked both ways against the input mesh. If anything is off it falls back from prismatic to planar to faceted, so you always get a valid solid.

## What to expect

- **Best on meshes exported from CAD:** MakerWorld / Printables downloads, Fusion / Onshape / SolidWorks STL exports, Gridfinity bins, brackets, enclosures.
- **Freeform shapes** (lofts, sweeps, variable fillets, sculpted or organic forms) have no simple analytic surface. They come back as flat facets, merged where coplanar. The solid is still valid and editable, just not smooth there; Fusion's prismatic mode does the same.
- **3D scans** have no exact surfaces to recover. For scans, use a remodelling tool such as CNC Kitchen's [ScanRuler](https://scanruler.com).
- **Holes come back truly round.** The STEP restores the original CAD circle, so its volume can differ from the mesh by a hair.
- **Speed:** seconds for simple parts and about a minute for a 38k-triangle Gridfinity bin. Big freeform areas are the slow case; the time budget caps them.

## Tested on

Known CAD parts were exported to STL at two qualities, converted back, and compared with the original. Fine is a 0.01 mm chord; coarse is a 0.1 mm chord with ~29° steps. Every result is a single closed, valid solid.

| Part | Original CAD faces | Fine export | Coarse export |
| --- | --- | --- | --- |
| Plate with through, blind and edge-breaking holes | 8 plane, 6 cylinder | identical | identical |
| Box with vertical fillets | 6 plane, 4 cylinder | identical | identical |
| Box with top-edge fillets | 6 plane, 4 cylinder | identical | identical |
| Countersunk hole | 6 plane, 1 cone, 1 cylinder | identical | cone left as facets (+0.013 % vol) |
| Boss with spherical dome | 2 plane, 2 cylinder, 1 sphere | identical | identical |
| Rounded-corner pocket | 11 plane, 4 cylinder | identical | identical |
| Box with every edge filleted | 6 plane, 8 cylinder, 4 torus | identical | tori approximated by sphere patches + facets |
| Lofted freeform body | 7 plane, 2 B-spline | freeform as facets (valid) | freeform as facets |

A MakerWorld Gridfinity Dremel bit holder (7×2×5, 37,766 triangles) became **1,383 faces** (1,024 plane, 184 cylinder, 126 cone, 49 sphere) in under a minute. It's one closed solid within 0.015 mm of the mesh, and its 63 bit holes come back as true 3.50 mm cylinders.

```bash
python tests/smoke_check.py cli                                    # quick check (2 parts)
python tests/make_parts.py /tmp/parts && python tests/run_parts.py /tmp/parts   # full set
```

## Making a release (maintainer)

1. Bump `__version__` in `mesh2cad/__init__.py`, add a section to `CHANGELOG.md`, and push to `main`.
2. Create the tag in either of two ways:
   - On GitHub: **Releases → Draft a new release**, type the tag (e.g. `v0.2.0`) and choose *Create new tag on publish*, target `main`, then **Publish release**.
   - Or from a clone: `git tag v0.2.0 && git push origin v0.2.0`.
3. GitHub Actions builds and checks the Mac (Apple Silicon + Intel) and Windows apps, then attaches them to the release with the notes from `.github/RELEASE_NOTES.md` (about 25 minutes). Every push to `main` also builds them; the downloads are attached to that workflow run.

## License

MIT. The code is written from scratch and contains no code from Autodesk, BumpMesh or ScanRuler. Dependencies keep their own licenses: OpenCASCADE (LGPL-2.1 with exception), VTK (BSD), trimesh (MIT), NumPy / SciPy (BSD).
