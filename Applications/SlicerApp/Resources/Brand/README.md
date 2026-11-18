# Slicer Studio logo

`SlicerStudioLogo.svg` is the master of the Slicer Studio logo. It was designed
in Claude Design (canvas "Slicer Studio logo directions", direction
"A · Slice stack"). The logo is a 1024 x 1024 navy tile (`#0f1b33`, corner
radius 228) holding three isometric image slices in the accent blue of the
application (`#2f6fe0` at 35 %, 65 % and 100 % opacity). The top slice has a
light blue edge (`#7aa7ff`) and an orange lesion (`#ff8a3d`).

Do not edit the generated files by hand. Change the master (or the outlined
text), then run the script below. The other SVG files here are made from the
master by `Utilities/Scripts/SlicerStudioBrand/make_brand_assets.py`. They
only place it on another canvas, or leave out the tile or the lightest slice:

| File | Use |
|---|---|
| `SlicerStudioIcon-macOS.svg` | macOS icon (`Slicer.icns`): the tile inside the macOS icon grid (824 of 1024 units) |
| `SlicerStudioLogo-small.svg` | 16 to 32 pixel icons: without the lightest slice, which merges with the others at that size |
| `SlicerStudioMark.svg` | the slices without the tile (screen capture watermark, splash screen) |
| `SlicerStudioLockup-horizontal.svg` | module panel logo |
| `SlicerStudioLockup-vertical.svg` | About box |
| `SlicerStudioSplash.svg` | start-up splash screen (574 x 377) |
| `SlicerStudioWordmark.svg` | "Slicer Studio" in Space Grotesk SemiBold, letter spacing -0.01 em, as outlines |
| `SlicerStudioBasedOn.svg` | "based on 3D Slicer" in Inter Regular, as outlines |

The wordmark and the subtitle are stored as outlines, so no rendering depends on
installed fonts. Space Grotesk and Inter are under the SIL Open Font License;
the fonts themselves are not part of this repository.

To remake every icon, PNG and splash file (they are committed, so builds do not
need these tools), use a throwaway virtual environment:

```console
python3 -m venv /tmp/brand-venv
/tmp/brand-venv/bin/pip install cairosvg pillow
/tmp/brand-venv/bin/python -I Utilities/Scripts/SlicerStudioBrand/make_brand_assets.py build
python3 -I Utilities/Scripts/SlicerStudioBrand/make_brand_assets.py check
```

`check` needs only the Python standard library. It verifies that every
generated file is present and has the expected pixel sizes.

The product name "3D Slicer" stays where it names the platform Slicer Studio is
based on (license, About box, acknowledgments), the official application, or an
identifier that keeps files and settings compatible with official 3D Slicer.
For example, the application name `Slicer`, the settings organization
`slicer.org` and `Slicer.ini`, and the scene and file formats stay unchanged.
