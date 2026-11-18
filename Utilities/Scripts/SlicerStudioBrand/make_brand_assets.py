#!/usr/bin/env python3
"""Make every Slicer Studio logo, icon and splash file from the logo source.

The logo was designed in Claude Design (direction "A - Slice stack"). Its master
file is ``Applications/SlicerApp/Resources/Brand/SlicerStudioLogo.svg``: a
1024x1024 navy tile with three isometric slices and an orange lesion. This
script never changes that design. The variants it derives only place the
master on another canvas, or leave out the tile or the lightest slice:

``SlicerStudioIcon-macOS.svg``
    the tile inside the macOS icon grid (824 of 1024 units, 100 units of
    margin), so that the Dock and Finder show it like other current apps;
``SlicerStudioLogo-small.svg``
    the tile without the lightest slice, for 16-32 pixel icons;
``SlicerStudioMark.svg``
    the slices and the lesion without the tile, on a transparent background;
``SlicerStudioLockup-horizontal.svg``, ``SlicerStudioLockup-vertical.svg``
    the tile and the "Slicer Studio" wordmark;
``SlicerStudioSplash.svg``
    the start-up splash screen.

The wordmark ("Slicer Studio" in Space Grotesk SemiBold, letter spacing
-0.01 em, as on the Claude Design canvas) and the "based on 3D Slicer" line
(Inter) are stored as outlines in ``SlicerStudioWordmark.svg`` and
``SlicerStudioBasedOn.svg``, so no rendering depends on installed fonts.
``outline`` remakes them from font files (needs fontTools).

Usage (in a throwaway virtual environment with ``pip install cairosvg pillow``,
plus ``fonttools`` for ``outline``)::

    python -I make_brand_assets.py build      # write every file below
    python -I make_brand_assets.py check      # verify them (standard library only)
    python -I make_brand_assets.py outline --wordmark-font SpaceGrotesk-SemiBold.ttf --text-font Inter-Regular.otf

The generated files are committed, so that neither the macOS nor the Linux
build needs cairo.
"""

import argparse
import io
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, os.pardir, os.pardir))
APP_RESOURCES = os.path.join("Applications", "SlicerApp", "Resources")
BRAND = os.path.join(APP_RESOURCES, "Brand")
SVG_NS = "http://www.w3.org/2000/svg"

NAVY = "#0f1b33"
BLUE = "#2f6fe0"
SPLASH_TEXT = "#ffffff"
SPLASH_SUBTLE = "#9fb3d9"

# Generated files: path -> (kind, details). Kinds: png (width, height), ico (sizes), icns (sizes in pixels).
ICNS_TYPES = (  # type, pixels, variant
    (b"icp4", 16, "small"),
    (b"icp5", 32, "small"),
    (b"ic11", 32, "small"),  # 16@2x
    (b"ic12", 64, "icon"),  # 32@2x
    (b"ic07", 128, "icon"),
    (b"ic08", 256, "icon"),
    (b"ic13", 256, "icon"),  # 128@2x
    (b"ic09", 512, "icon"),
    (b"ic14", 512, "icon"),  # 256@2x
    (b"ic10", 1024, "icon"),  # 512@2x
)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
FAVICON_SIZES = (16, 32, 48)
HICOLOR_SIZES = (48, 128, 256, 512)


def _p(*parts):
    return os.path.join(*parts)


# Raster outputs: (path, source variant, width, height)
PNG_OUTPUTS = (
    (_p(APP_RESOURCES, "Icons", "Small", "Slicer-DesktopIcon.png"), "small", 16, 16),
    (_p(APP_RESOURCES, "Icons", "Medium", "Slicer-DesktopIcon.png"), "small", 32, 32),
    (_p(APP_RESOURCES, "Icons", "Large", "Slicer-DesktopIcon.png"), "logo", 48, 48),
    (_p(APP_RESOURCES, "Icons", "XLarge", "Slicer-DesktopIcon.png"), "logo", 64, 64),
    (_p(APP_RESOURCES, "Icons", "XXLarge", "Slicer-DesktopIcon.png"), "logo", 256, 256),
    (_p(APP_RESOURCES, "Images", "Slicer-SplashScreen.png"), "splash", 574, 377),
    (_p(APP_RESOURCES, "Images", "Slicer-Logo.png"), "vertical", 330, 412),
    (_p(APP_RESOURCES, "Images", "Slicer-ModulePanelLogo.png"), "horizontal", 162, 37),
    (_p(APP_RESOURCES, "Images", "Slicer-ModulePanelLogo@2x.png"), "horizontal", 324, 74),
    (_p("Modules", "Loadable", "SlicerWelcome", "Resources", "Icons", "SlicerWelcome.png"), "small", 16, 16),
    (_p("Modules", "Scripted", "ScreenCapture", "Resources", "SlicerWatermark.png"), "mark", 138, 138),
    (_p("Modules", "Scripted", "WebServer", "Resources", "docroot", "images", "SlicerStudio-DesktopIcon.png"), "logo", 64, 64),
    (_p("Docs", "_static", "images", "SlicerStudio-Mark.png"), "logo", 200, 200),
    (_p("Docs", "_static", "images", "SlicerStudio-favicon-32.png"), "small", 32, 32),
) + tuple(
    (_p(APP_RESOURCES, "Linux", "icons", "hicolor", f"{size}x{size}", "apps", "slicer-studio.png"), "logo", size, size)
    for size in HICOLOR_SIZES
)
ICO_OUTPUTS = (
    (_p(APP_RESOURCES, "Slicer.ico"), ICO_SIZES),
    (_p("Modules", "Scripted", "WebServer", "Resources", "docroot", "favicon.ico"), FAVICON_SIZES),
    (_p("Docs", "_static", "images", "SlicerStudio-favicon.ico"), FAVICON_SIZES),
)
ICNS_OUTPUT = _p(APP_RESOURCES, "Slicer.icns")
# SVG copies outside the Brand folder (path, variant)
SVG_COPIES = (
    (_p(APP_RESOURCES, "Images", "Slicer-SplashScreen.svg"), "splash"),
    (_p(APP_RESOURCES, "Images", "Slicer_Logo_Horizontal.svg"), "horizontal"),
    (_p("Docs", "_static", "images", "SlicerStudio-Mark.svg"), "logo"),
    (_p("Docs", "images", "studio", "SlicerStudioLogo.svg"), "logo"),
)
VARIANT_FILES = {
    "logo": "SlicerStudioLogo.svg",
    "icon": "SlicerStudioIcon-macOS.svg",
    "small": "SlicerStudioLogo-small.svg",
    "mark": "SlicerStudioMark.svg",
    "horizontal": "SlicerStudioLockup-horizontal.svg",
    "vertical": "SlicerStudioLockup-vertical.svg",
    "splash": "SlicerStudioSplash.svg",
}


# --------------------------------------------------------------------------------------------------------------------
# Reading the master logo and the outlined text


def _readMaster():
    """Return (tile element text, list of slice/lesion element texts) of the master logo."""
    path = _p(ROOT, BRAND, VARIANT_FILES["logo"])
    tree = ET.parse(path)
    root = tree.getroot()
    if root.get("viewBox") != "0 0 1024 1024":
        raise SystemExit(f"{path}: expected viewBox 0 0 1024 1024")
    tile = None
    shapes = []
    for element in root:
        tag = element.tag.split("}")[-1]
        if tag == "title":
            continue
        element.tag = tag
        text = ET.tostring(element, encoding="unicode").replace(f' xmlns="{SVG_NS}"', "").strip()
        if tag == "rect" and tile is None:
            tile = text
        else:
            shapes.append((element, text))
    if tile is None or len(shapes) != 4:
        raise SystemExit(f"{path}: expected a tile, three slices and a lesion")
    return tile, shapes


def _readOutline(name):
    """Return (minX, minY, width, height, inner SVG) of an outlined text file in the Brand folder."""
    path = _p(ROOT, BRAND, name)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    viewBox = [float(v) for v in re.search(r'viewBox="([^"]+)"', text).group(1).split()]
    inner = re.search(r"<g\b.*</g>", text, re.S).group(0)
    inner = re.sub(r'\sfill="[^"]*"', "", inner, count=1)  # the colour is set by the caller
    return viewBox[0], viewBox[1], viewBox[2], viewBox[3], inner


def _svg(width, height, body, viewBox=None, title="Slicer Studio"):
    viewBox = viewBox or f"0 0 {width} {height}"
    return (
        f'<svg xmlns="{SVG_NS}" width="{width}" height="{height}" viewBox="{viewBox}">\n'
        f"  <title>{title}</title>\n{body}</svg>\n"
    )


def _place(outline, x, y, width=None, height=None, fill="#000000"):
    """Place an outline so that its box starts at (x, y) and fits width or height. Return (svg, w, h)."""
    minX, minY, w, h, inner = outline
    scale = width / w if width is not None else height / h
    group = (
        f'  <g transform="translate({x:.2f} {y:.2f}) scale({scale:.5f}) translate({-minX:.2f} {-minY:.2f})" fill="{fill}">'
        f"{inner}</g>\n"
    )
    return group, w * scale, h * scale


def _tileGroup(tile, shapes, x, y, size):
    body = "".join("    " + text + "\n" for _, text in shapes)
    return f'  <g transform="translate({x:.2f} {y:.2f}) scale({size / 1024:.6f})">\n    {tile}\n{body}  </g>\n'


# The slices and the lesion lie inside this square of the 1024 canvas (with the stroke of the top slice).
MARK_BOX = (220, 224, 584)  # x, y, size


def _markGroup(shapes, x, y, size):
    bx, by, bs = MARK_BOX
    scale = size / bs
    body = "".join("    " + text + "\n" for _, text in shapes)
    return f'  <g transform="translate({x:.2f} {y:.2f}) scale({scale:.6f}) translate({-bx} {-by})">\n{body}  </g>\n'


def makeVariantSvgs():
    """Return {variant: svg text} for every derived variant (the master is returned unchanged)."""
    tile, shapes = _readMaster()
    with open(_p(ROOT, BRAND, VARIANT_FILES["logo"]), encoding="utf-8") as f:
        master = f.read()
    wordmark = _readOutline("SlicerStudioWordmark.svg")
    basedOn = _readOutline("SlicerStudioBasedOn.svg")
    lightest = min(shapes[:3], key=lambda item: float(item[0].get("fill-opacity", "1")))
    svgs = {"logo": master}

    # macOS icon grid: 824/1024 tile with a 100 unit margin (Apple's macOS 11+ app icon template).
    svgs["icon"] = _svg(1024, 1024, _tileGroup(tile, shapes, 100, 100, 824), title="Slicer Studio")

    # Small icons: the lightest slice merges with the others below 32 pixels, so it is left out.
    small = [item for item in shapes if item is not lightest]
    svgs["small"] = _svg(1024, 1024, _tileGroup(tile, small, 0, 0, 1024))

    # Mark without the tile, transparent background.
    svgs["mark"] = _svg(584, 584, _markGroup(shapes, 0, 0, 584))

    # Horizontal lockup (module panel, 162x37 at 1x): tile, then the wordmark in the accent blue, which reads on
    # light and dark application themes.
    width, height = 1620, 370
    body = _tileGroup(tile, shapes, 10, 10, 350)
    text, tw, th = _place(wordmark, 0, 0, height=150)
    text, tw, th = _place(wordmark, 410, (height - 150) / 2, height=150, fill=BLUE)
    if 410 + tw > width - 10:
        text, tw, th = _place(wordmark, 410, 0, width=width - 420, fill=BLUE)
        text, tw, th = _place(wordmark, 410, (height - th) / 2, width=width - 420, fill=BLUE)
    svgs["horizontal"] = _svg(width, height, body + text)

    # Vertical lockup (About box, 330x412 at 1x).
    width, height = 1650, 2060
    tileSize = 1150
    body = _tileGroup(tile, shapes, (width - tileSize) / 2, 90, tileSize)
    text, tw, th = _place(wordmark, 0, 0, width=1500)
    text, tw, th = _place(wordmark, (width - 1500) / 2, 90 + tileSize + (height - 90 - tileSize - th) / 2, width=1500, fill=BLUE)
    svgs["vertical"] = _svg(width, height, body + text)

    # Splash (574x377): navy, mark at the left, wordmark and "based on 3D Slicer" at the right. The bottom band is
    # left free for the start-up messages, which the application draws in a light colour.
    width, height = 574, 377
    body = f'  <rect width="{width}" height="{height}" fill="{NAVY}"/>\n'
    markSize = 196
    markY = 66
    body += _markGroup(shapes, 30, markY, markSize)
    textX = 30 + markSize + 22
    textWidth = width - textX - 34
    text, tw, th = _place(wordmark, textX, 0, width=textWidth)
    subtitle, sw, sh = _place(basedOn, 0, 0, height=th * 0.42)
    blockHeight = th + th * 0.55 + sh
    top = markY + markSize / 2 - blockHeight / 2
    text, tw, th = _place(wordmark, textX, top, width=textWidth, fill=SPLASH_TEXT)
    subtitle, sw, sh = _place(basedOn, textX + 3, top + th * 1.55, height=th * 0.42, fill=SPLASH_SUBTLE)
    body += text + subtitle
    body += f'  <rect x="0" y="{height - 4}" width="{width}" height="4" fill="{BLUE}"/>\n'
    svgs["splash"] = _svg(width, height, body)
    return svgs


# --------------------------------------------------------------------------------------------------------------------
# Writing


def _render(svgText, width, height):
    import cairosvg
    from PIL import Image

    data = cairosvg.svg2png(bytestring=svgText.encode("utf-8"), output_width=width, output_height=height)
    image = Image.open(io.BytesIO(data))
    image.load()
    if image.size != (width, height):
        raise SystemExit(f"rendered {image.size}, expected {(width, height)}")
    return image.convert("RGBA")


def _pngBytes(image):
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


def icnsBytes(entries):
    """Pack (type, png bytes) entries into an .icns file (each type holds its own PNG)."""
    chunks = b"".join(kind + struct.pack(">I", 8 + len(data)) + data for kind, data in entries)
    return b"icns" + struct.pack(">I", 8 + len(chunks)) + chunks


def _write(relPath, data, written):
    path = _p(ROOT, relPath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "w" if isinstance(data, str) else "wb"
    kwargs = {"encoding": "utf-8", "newline": "\n"} if mode == "w" else {}
    with open(path, mode, **kwargs) as f:
        f.write(data)
    written.append(relPath)


def build():
    svgs = makeVariantSvgs()
    written = []
    for variant, name in VARIANT_FILES.items():
        if variant != "logo":
            _write(_p(BRAND, name), svgs[variant], written)
    for path, variant in SVG_COPIES:
        _write(path, svgs[variant], written)
    for path, variant, width, height in PNG_OUTPUTS:
        _write(path, _pngBytes(_render(svgs[variant], width, height)), written)
    for path, sizes in ICO_OUTPUTS:
        images = [_render(svgs["small" if size <= 32 else "logo"], size, size) for size in sizes]
        out = io.BytesIO()
        largest = images[-1]
        largest.save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
        _write(path, out.getvalue(), written)
    entries = [(kind, _pngBytes(_render(svgs[variant], size, size))) for kind, size, variant in ICNS_TYPES]
    _write(ICNS_OUTPUT, icnsBytes(entries), written)
    for path in written:
        print("wrote", path)
    return check()


# --------------------------------------------------------------------------------------------------------------------
# Checking (standard library only)


def pngSize(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def icoSizes(data):
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    if reserved != 0 or kind != 1:
        return None
    sizes = []
    for i in range(count):
        w, h = data[6 + 16 * i], data[7 + 16 * i]
        sizes.append((w or 256, h or 256))
    return sorted(sizes)


def icnsEntries(data):
    """Return {type: (width, height)} of an .icns file whose entries are PNG."""
    if data[:4] != b"icns" or struct.unpack(">I", data[4:8])[0] != len(data):
        return None
    entries = {}
    offset = 8
    while offset < len(data):
        kind = data[offset:offset + 4]
        length = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        if length < 8 or offset + length > len(data):
            return None
        entries[kind] = pngSize(data[offset + 8:offset + length])
        offset += length
    return entries


def check():
    problems = []

    def read(relPath):
        path = _p(ROOT, relPath)
        if not os.path.isfile(path):
            problems.append(f"missing: {relPath}")
            return None
        with open(path, "rb") as f:
            return f.read()

    for path, _variant, width, height in PNG_OUTPUTS:
        data = read(path)
        if data is not None and pngSize(data) != (width, height):
            problems.append(f"{path}: size {pngSize(data)}, expected {(width, height)}")
    for path, sizes in ICO_OUTPUTS:
        data = read(path)
        if data is not None and icoSizes(data) != sorted((s, s) for s in sizes):
            problems.append(f"{path}: sizes {icoSizes(data)}, expected {list(sizes)}")
    data = read(ICNS_OUTPUT)
    if data is not None:
        expected = {kind: (size, size) for kind, size, _variant in ICNS_TYPES}
        if icnsEntries(data) != expected:
            problems.append(f"{ICNS_OUTPUT}: entries {icnsEntries(data)}, expected {expected}")
    for name in list(VARIANT_FILES.values()) + ["SlicerStudioWordmark.svg", "SlicerStudioBasedOn.svg"]:
        data = read(_p(BRAND, name))
        if data is not None:
            try:
                ET.fromstring(data)
            except ET.ParseError as error:
                problems.append(f"{name}: {error}")
    for path, _variant in SVG_COPIES:
        read(path)
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print("brand assets: all files present with the expected sizes")
    return 1 if problems else 0


# --------------------------------------------------------------------------------------------------------------------
# Outlining text


def outlineText(fontPath, text, letterSpacingEm=0.0):
    """Return the SVG of ``text`` set in ``fontPath`` as outlines (font units, y down)."""
    from fontTools.pens.boundsPen import BoundsPen
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.ttLib import TTFont

    font = TTFont(fontPath)
    glyphSet = font.getGlyphSet()
    unitsPerEm = font["head"].unitsPerEm
    glyphOrder = font.getGlyphOrder()
    try:
        # Shaped with HarfBuzz (kerning, as a browser sets it) when uharfbuzz is installed
        import uharfbuzz as hb

        with open(fontPath, "rb") as f:
            hbFont = hb.Font(hb.Face(f.read()))
        buffer = hb.Buffer()
        buffer.add_str(text)
        buffer.guess_segment_properties()
        hb.shape(hbFont, buffer, {"kern": True, "liga": False})
        placed = [(glyphOrder[info.codepoint], pos.x_advance, pos.x_offset)
                  for info, pos in zip(buffer.glyph_infos, buffer.glyph_positions)]
    except ImportError:
        cmap = font.getBestCmap()
        placed = [(cmap[ord(char)], glyphSet[cmap[ord(char)]].width, 0) for char in text]
    paths = []
    bounds = BoundsPen(glyphSet)
    x = 0.0
    for name, advance, offset in placed:
        pen = SVGPathPen(glyphSet)
        glyph = glyphSet[name]
        glyph.draw(TransformPen(pen, (1, 0, 0, -1, x + offset, 0)))
        glyph.draw(TransformPen(bounds, (1, 0, 0, -1, x + offset, 0)))
        commands = pen.getCommands()
        if commands:
            paths.append(commands)
        x += advance + letterSpacingEm * unitsPerEm
    xMin, yMin, xMax, yMax = bounds.bounds
    family = font["name"].getDebugName(4)
    body = "".join(f'<path d="{d}"/>' for d in paths)
    return (
        f'<svg xmlns="{SVG_NS}" viewBox="{xMin:.0f} {yMin:.0f} {xMax - xMin:.0f} {yMax - yMin:.0f}">\n'
        f"  <title>{text} ({family}, outlines)</title>\n"
        f'  <g fill="{NAVY}">{body}</g>\n</svg>\n'
    )


def outline(args):
    written = []
    _write(_p(BRAND, "SlicerStudioWordmark.svg"), outlineText(args.wordmark_font, "Slicer Studio", -0.01), written)
    _write(_p(BRAND, "SlicerStudioBasedOn.svg"), outlineText(args.text_font, "based on 3D Slicer"), written)
    for path in written:
        print("wrote", path)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="write every derived SVG, PNG, ICO and ICNS file")
    sub.add_parser("check", help="check that every generated file is present with the expected sizes")
    outlineParser = sub.add_parser("outline", help="remake the outlined wordmark and subtitle from font files")
    outlineParser.add_argument("--wordmark-font", required=True, help="Space Grotesk SemiBold (.ttf/.otf)")
    outlineParser.add_argument("--text-font", required=True, help="Inter Regular (.ttf/.otf)")
    args = parser.parse_args(argv)
    if args.command == "build":
        return build()
    if args.command == "outline":
        return outline(args)
    return check()


if __name__ == "__main__":
    sys.exit(main())
