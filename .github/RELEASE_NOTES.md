**Mesh2CAD turns STL / 3MF / OBJ files exported from CAD back into STEP solids with real flat faces, holes, fillets, cones, spheres and tori.** You can then edit them in Fusion and other CAD tools like any other solid.

This is a test build, so feedback is very welcome. In the app, click **Feedback / report a problem…**. It copies your settings and the log, then opens the feedback form.

### Which file do I download?

| Your computer | Download |
| --- | --- |
| Mac with Apple Silicon (M1, M2, M3, M4 …) | `Mesh2CAD-…-macOS-AppleSilicon.dmg` |
| Mac with an Intel processor | `Mesh2CAD-…-macOS-Intel.dmg` |
| Windows 10 / 11 (64-bit) | `Mesh2CAD-…-Windows.zip` |

Not sure which Mac you have? Click the Apple menu  → **About This Mac**. It shows **Chip: Apple M…** or **Processor: Intel …**. You need macOS 14 Sonoma or newer.

### First launch on a Mac (one time only)

The app isn't notarized by Apple yet, so macOS blocks it the first time.

1. Open the `.dmg` and drag **Mesh2CAD** onto **Applications**.
2. Open Mesh2CAD from Applications. macOS says it can't verify it: click **Done**.
3. Open **System Settings → Privacy & Security**, scroll down to *"Mesh2CAD was blocked…"*, click **Open Anyway**, and confirm.

It opens normally after that. If you're comfortable with Terminal, this one line does the same thing:
`xattr -dr com.apple.quarantine /Applications/Mesh2CAD.app`

### First launch on Windows

1. Unzip the whole folder somewhere (e.g. Documents) and keep the files together.
2. Run **Mesh2CAD.exe**. If SmartScreen appears, click **More info → Run anyway**.

The download is large (~300 MB) because it includes the OpenCASCADE geometry kernel. See [CHANGELOG.md](https://github.com/akrtek/mesh2cad/blob/main/CHANGELOG.md) for what changed.
