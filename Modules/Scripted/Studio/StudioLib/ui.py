"""Visual helpers of Slicer Studio: style tokens, tinted Lucide icons and widget roles.

These helpers and the shell API (:mod:`StudioLib.shell`) are the only Studio internals that
other modules may use:

- ``token(name)``, ``isDark()``: colors of the graphite themes (dark and light);
- ``icon(name, size=18, role="normal")``: a Lucide icon tinted for every widget state;
- ``setRole(widget, role)``: the ``slicerRole`` widget property;
- builders ``card()``, ``chip()``, ``kbd()``, ``primaryButton()``, ``segmented()``, ``noticeLine()``
  and ``wrapRow()``;
- ``tooltip(name, key, description)``: the "Name · key · description" tooltip format;
- ``contrastRatio()``, ``primaryScreen()``, ``fontPx()`` and ``installButtonKeys()`` (keyboard
  activation of focused buttons).

Colors come from ``qSlicerStyle.token()`` when the application style provides it. A fallback
table with the same values is used otherwise (for example with a Qt built-in style).

Roles are painted by the application style when it reports ``supportsSlicerRole``. Until then
a small stylesheet that only matches the widget's role is set on the Studio widget itself:
there is no application-wide stylesheet.
"""

import html
import os

import qt
import slicer
from slicer.i18n import tr as _

#: Directory of the vendored Lucide icons (ISC license, see LICENSE in that directory)
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Resources", "Icons", "Lucide")

ROLES = ("card", "cardLow", "primary", "secondary", "ghost", "segmented", "toolTile", "sectionHeader", "overline", "secondaryText", "caption", "chip", "kbd",
         "topBar", "compact", "iconTile", "switch")

CHIP_LEVELS = ("neutral", "busy", "ok", "warn", "danger")

#: Type scale of the visual design (VISUAL.md 3): style -> (pixel size for a 13 px body, weight). Sizes go through
#: ``fontPx()``, so that the application font size scales them.
TYPE = {
    "display": (22, qt.QFont.DemiBold),  # Library page title
    "title": (18, qt.QFont.DemiBold),  # dialog titles, the project name of the Continue card
    "section": (14, qt.QFont.DemiBold),  # panel header titles, card titles
    "body": (13, qt.QFont.Normal),
    "name": (13, qt.QFont.Medium),  # names in rows and cards
    "label": (12, qt.QFont.DemiBold),  # section labels (text2)
    "caption": (12, qt.QFont.Normal),  # meta lines (text3)
    "small": (11, qt.QFont.Normal),  # chips, stat labels
    "overline": (11, qt.QFont.DemiBold),  # the rare all-caps labels
    "number": (15, qt.QFont.DemiBold),  # stat values
}

#: Corner radii of the visual design (px)
RADIUS = {"key": 5, "control": 7, "tile": 9, "card": 10, "cardLow": 12, "dialog": 14}

#: Spacing of the 4 px grid (VISUAL.md 2.7)
INSPECTOR_PADDING = 16
CARD_PADDING = 14
GROUP_SPACING = 20
ROW_HEIGHT = 32
LIST_ROW_HEIGHT = 34
SETTINGS_ROW_HEIGHT = 44
COMPACT_HEIGHT = 28
BUTTON_HEIGHT = 32
PANEL_ACTION_HEIGHT = 38

#: Text colors of ``label()``: token -> slicerRole that qSlicerStyle paints in that color
_TEXT_ROLES = {"text": None, "text2": "secondaryText", "text3": "caption"}

# Token values of the dark and light themes, used when the application style does not provide
# them. Keep in sync with qSlicerStyle::tokenColor() (Base/QTGUI/qSlicerStyle.cxx).
_DARK_TOKENS = {
    "canvas": "#0f0f11",
    "card": "#1d1d20",
    "card-low": "#18181b",
    "selected": "#2a2a2f",
    "control-hover": "#26262a",
    "control-pressed": "#202024",
    "hairline": "#26262a",
    "border": "#34343a",
    "control-border": "#6b6b74",
    "text2": "#a1a1aa",
    "text3": "#8b8b94",
    "text-disabled": "#71717a",
    "accent": "#2f6fe0",
    # accent-hover and accent-pressed are derived from accent (lightness +/- 8%)
    "accent-fg": "#7aa7ff",
    "accent-subtle": "#22324f",
    "success": "#3fb950",
    "warning": "#e3b341",
    "danger": "#f85149",
    "view-bar": "#161618",
    "lesion": "#ff5a5f",
}

_LIGHT_TOKENS = {
    "canvas": "#f1f1f4",
    "card": "#f6f6f8",
    "card-low": "#ffffff",
    "selected": "#ffffff",
    "control-hover": "#eeeef2",
    "control-pressed": "#e4e4e9",
    "hairline": "#e4e4e8",
    "border": "#d4d4da",
    "control-border": "#8a8a94",
    "text2": "#52525b",
    "text3": "#6b6b74",
    "text-disabled": "#8e8e98",
    "accent": "#2f6fe0",
    # accent-hover and accent-pressed are derived from accent (lightness +/- 8%)
    "accent-fg": "#2158bd",
    "accent-subtle": "#e3ecfc",
    "success": "#1a7f37",
    "warning": "#9a6700",
    "danger": "#cf222e",
    "view-bar": "#ffffff",
    "lesion": "#ff5a5f",
}

#: Colors of the slice views (the 8 px dot of the view bars), the same in both themes
VIEW_COLORS = {"Red": "#e5484d", "Green": "#30a46c", "Yellow": "#f5d90a"}

# Palette roles that can also be read with token()
_PALETTE_TOKENS = {
    "window": qt.QPalette.Window,
    "text": qt.QPalette.WindowText,
    "base": qt.QPalette.Base,
    "button": qt.QPalette.Button,
    "button-text": qt.QPalette.ButtonText,
    "highlight": qt.QPalette.Highlight,
    "highlighted-text": qt.QPalette.HighlightedText,
    "tooltip": qt.QPalette.ToolTipBase,
    "tooltip-text": qt.QPalette.ToolTipText,
}


def isDark():
    """Return True if the application palette is dark (window color lightness < 128)."""
    return slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.Window).lightness() < 128


def _shiftLightness(color, delta):
    hue, saturation, lightness, alpha = color.hslHueF(), color.hslSaturationF(), color.lightnessF(), color.alphaF()
    return qt.QColor.fromHslF(hue, saturation, min(max(lightness + delta, 0.0), 1.0), alpha).toRgb()


def token(name):
    """Return the ``qt.QColor`` of a style token for the current application palette.

    Style tokens are listed in SPEC 4.2 and 4.3 and VISUAL.md 3 (``canvas``, ``card``, ``card-low``, ``selected``,
    ``hairline``, ``border``, ``control-border``, ``text2``, ``text3``, ``text-disabled``, ``accent``, ``accent-fg``,
    ``accent-subtle``, ``success``, ``warning``, ``danger``, ``view-bar``...). Palette roles are also available as
    ``window``, ``text``, ``base``, ``button``, ``highlight``, ``tooltip`` and ``tooltip-text``.
    """
    role = _PALETTE_TOKENS.get(name)
    if role is not None:
        return slicer.app.palette().color(qt.QPalette.Active, role)
    styleToken = getattr(slicer.app.style(), "token", None)
    if styleToken is not None:
        color = styleToken(name)
        if color.isValid():
            return color
    tokens = _DARK_TOKENS if isDark() else _LIGHT_TOKENS
    value = tokens.get(name)
    if value is None and name in ("accent-hover", "accent-pressed"):
        return _shiftLightness(qt.QColor(tokens["accent"]), 0.08 if name == "accent-hover" else -0.08)
    if value is None:
        raise ValueError(f"Unknown Studio style token: {name}")
    return qt.QColor(value)


def _rgba(color, alpha):
    """Stylesheet rgba() of a QColor with the given alpha in [0, 1]."""
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {round(alpha * 255)})"


#
# Palette changes
#

_listeners = []
_boundStyleSheets = []  # [widget, function]
_watcher = None
#: Application font size (px) of the last refresh: a different size is an application font change
_lastApplicationFontPx = None


class _PaletteWatcher(qt.QObject):
    """Event filter on a hidden top-level widget: it receives the application palette, style and font changes."""

    def __init__(self):
        qt.QObject.__init__(self)
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioPaletteWatcher")
        self.widget.installEventFilter(self)
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.connect("timeout()", _refresh)

    def eventFilter(self, obj, event):
        if event.type() in (qt.QEvent.ApplicationPaletteChange, qt.QEvent.PaletteChange, qt.QEvent.StyleChange, qt.QEvent.ApplicationFontChange):
            # The style and the palette are often changed together: refresh once. A font change resizes the Studio
            # fonts (``_font()``) and rebuilds the stylesheets, whose font sizes come from ``fontPx()``.
            self.timer.start()
        return False


def _ensureWatcher():
    global _watcher, _lastApplicationFontPx
    if _watcher is None:
        _watcher = _PaletteWatcher()
        _lastApplicationFontPx = applicationFontPx()
        import atexit

        atexit.register(_deleteOrphansAtExit)


def _deleteOrphansAtExit():
    """At Python exit (after the application quit, before the modules are cleared): delete the bound widgets without a parent.

    Python owns a widget without a parent. Left to the clearing of this module, its deletion can call Python event filters (of
    the widget or of its buttons) while the interpreter is being finalized, and the application crashes on quit. The Python
    callbacks of its buttons are disconnected first (see ``importer.releaseWidget``).
    """
    import contextlib

    entries = list(_boundStyleSheets)
    _boundStyleSheets.clear()
    for widget, _function in entries:
        if not _isAlive(widget) or widget.parent() is not None:
            continue
        for button in widget.findChildren("QAbstractButton"):
            with contextlib.suppress(Exception):
                button.disconnect("clicked()")
        removeStudioEventFilters(widget)
        with contextlib.suppress(Exception):
            widget.delete()


def removeStudioEventFilters(widget):
    """Remove the shared Python event filters of this module (``wrapRow()``/``noticeLine()`` wrapping, ``installButtonKeys()``,
    ``elideLabel()``)
    from ``widget`` and its children.

    Call it before deleting a widget from Python (``widget.delete()``): the destruction of its layout sends events to the widget,
    and a Python event filter called for a widget that Python is deleting crashes PythonQt.
    """
    if not _isAlive(widget):
        return
    filters = [instance for instance in (_wrapFilter, _buttonKeys, _elideFilter) if instance is not None]
    if not filters:
        return
    for target in (widget, *widget.findChildren("QWidget")):
        for instance in filters:
            target.removeEventFilter(instance)


def addPaletteListener(callback):
    """Call ``callback()`` (once per event loop iteration) after the application palette or style changed."""
    _ensureWatcher()
    if callback not in _listeners:
        _listeners.append(callback)


def removePaletteListener(callback):
    if callback in _listeners:
        _listeners.remove(callback)


def _isAlive(widget):
    try:
        widget.objectName
    except Exception:
        return False
    return True


def _refresh():
    global _lastApplicationFontPx
    _iconCache.clear()
    fontPixelSize = applicationFontPx()
    if _lastApplicationFontPx is not None and fontPixelSize != _lastApplicationFontPx:
        _refreshStudioWidgetFonts(_lastApplicationFontPx)
    _lastApplicationFontPx = fontPixelSize
    _refreshFonts()
    _refreshIconTiles()
    for entry in list(_boundStyleSheets):
        widget, function = entry
        if not _isAlive(widget):
            _boundStyleSheets.remove(entry)
            continue
        _applyBoundStyleSheet(widget, function)
    for callback in list(_listeners):
        try:
            callback()
        except Exception:
            import logging
            import traceback

            logging.error(f"Studio palette listener failed:\n{traceback.format_exc()}")


def _applyBoundStyleSheet(widget, function):
    styleSheet = function() if function is not None else ""
    if widget.styleSheet != styleSheet:
        widget.setStyleSheet(styleSheet)


def bindStyleSheet(widget, function):
    """Set the stylesheet returned by ``function()`` on ``widget`` and refresh it after palette changes.

    The function builds the stylesheet from tokens. It must only match ``widget`` (and Studio-owned
    children), never other application widgets. ``function=None`` removes the binding and the stylesheet.
    """
    _ensureWatcher()
    for entry in _boundStyleSheets:
        if entry[0] == widget:
            entry[1] = function
            break
    else:
        if function is not None:
            _boundStyleSheets.append([widget, function])
    if function is None:
        for entry in list(_boundStyleSheets):
            if entry[0] == widget:
                _boundStyleSheets.remove(entry)
    _applyBoundStyleSheet(widget, function)


#
# Icons
#

_svgCache = {}
_iconCache = {}
_iconCachePaletteKey = None

# Icon tint per role: (normal, active/hover, checked, disabled)
_ICON_TINTS = {
    "normal": ("text2", "text", "accent-fg", "text-disabled"),
    "strong": ("text", "text", "accent-fg", "text-disabled"),
    "accent": ("accent-fg", "accent-fg", "accent-fg", "text-disabled"),
    "onAccent": ("#ffffff", "#ffffff", "#ffffff", "text-disabled"),
    "danger": ("danger", "danger", "danger", "text-disabled"),
    "warning": ("warning", "warning", "warning", "text-disabled"),
}


def iconPath(name):
    return os.path.join(ICON_DIR, f"{name}.svg")


def _svgData(name):
    data = _svgCache.get(name)
    if data is None:
        with open(iconPath(name), "rb") as f:
            # Opaque black: only the alpha channel is used, the color is applied when tinting
            data = f.read().replace(b"currentColor", b"#000000")
        _svgCache[name] = data
    return data


def _renderMask(name, pixelSize):
    """Rasterize the icon with Qt's svg image format plugin (PythonQt does not wrap QtSvg)."""
    buffer = qt.QBuffer()
    buffer.setData(qt.QByteArray(_svgData(name)))
    buffer.open(qt.QIODevice.ReadOnly)
    reader = qt.QImageReader(buffer, qt.QByteArray(b"svg"))
    reader.setScaledSize(qt.QSize(pixelSize, pixelSize))
    image = reader.read()
    buffer.close()
    if image.isNull():
        raise RuntimeError(f"Cannot render Studio icon '{name}': {reader.errorString()}")
    return image.convertToFormat(qt.QImage.Format_ARGB32_Premultiplied)


def _tinted(mask, color, devicePixelRatio):
    image = qt.QImage(mask)
    painter = qt.QPainter(image)
    painter.setCompositionMode(qt.QPainter.CompositionMode_SourceIn)
    painter.fillRect(image.rect(), color)
    painter.end()
    pixmap = qt.QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(devicePixelRatio)
    return pixmap


def _tintColor(value):
    return qt.QColor(value) if value.startswith("#") else token(value)


def icon(name, size=18, role="normal"):
    """Return a ``qt.QIcon`` of the Lucide icon ``name`` (file name without extension).

    The icon is tinted for each state: normal = text2, active (hover) = text, checked = accent-fg,
    disabled = text-disabled. ``role`` selects another tint set: "strong" (text), "accent", "onAccent"
    (white, for icons on an accent fill), "danger" or "warning".
    Pixmaps are rendered at 1x and at the device pixel ratio. Icons are cached per palette.
    """
    global _iconCachePaletteKey
    paletteKey = slicer.app.palette().cacheKey()
    if paletteKey != _iconCachePaletteKey:
        _iconCache.clear()
        _iconCachePaletteKey = paletteKey
    key = (name, size, role)
    result = _iconCache.get(key)
    if result is not None:
        return result
    tints = _ICON_TINTS[role]
    ratios = [1.0]
    devicePixelRatio = slicer.app.devicePixelRatio()
    if devicePixelRatio > 1.0:
        ratios.append(devicePixelRatio)
    result = qt.QIcon()
    for ratio in ratios:
        mask = _renderMask(name, round(size * ratio))
        pixmaps = {}
        for tint in tints:
            if tint not in pixmaps:
                pixmaps[tint] = _tinted(mask, _tintColor(tint), ratio)
        normal, active, checked, disabled = (pixmaps[tint] for tint in tints)
        for state, regular in ((qt.QIcon.Off, normal), (qt.QIcon.On, checked)):
            result.addPixmap(regular, qt.QIcon.Normal, state)
            result.addPixmap(active if state == qt.QIcon.Off else checked, qt.QIcon.Active, state)
            result.addPixmap(active if state == qt.QIcon.Off else checked, qt.QIcon.Selected, state)
            result.addPixmap(disabled, qt.QIcon.Disabled, state)
    _iconCache[key] = result
    return result


def pixmap(name, size=18, color="text2"):
    """Return a ``qt.QPixmap`` of the Lucide icon ``name`` in a single color (token name or "#rrggbb")."""
    ratio = max(1.0, slicer.app.devicePixelRatio())
    return _tinted(_renderMask(name, round(size * ratio)), _tintColor(color), ratio)


#
# Roles
#


def supportsRoles():
    """Return True if the application style paints the ``slicerRole`` property itself."""
    return bool(slicer.app.style().property("supportsSlicerRole"))


def _roleStyleSheet(role):
    """Scoped fallback stylesheet of a role. Selectors only match widgets that have the role."""
    t = {name: token(name).name() for name in (
        "window", "text", "button", "canvas", "card", "card-low", "selected", "hairline", "border", "control-border", "text2", "text3", "text-disabled",
        "control-hover", "control-pressed", "accent", "accent-hover", "accent-pressed", "accent-fg", "accent-subtle", "success", "warning", "danger", "view-bar")}
    if role in ("card", "cardLow"):
        fill, radius = (t["card"], RADIUS["card"]) if role == "card" else (t["card-low"], RADIUS["cardLow"])
        return (
            f'QFrame[slicerRole="{role}"] {{ background-color: {fill}; border: 1px solid {t["hairline"]}; border-radius: {radius}px; }}'
            f'QFrame[slicerRole="{role}"] QLabel[slicerRole="sectionHeader"] {{ color: {t["text2"]}; font-size: {fontPx(12)}px; font-weight: 600; border: none; background: transparent; }}'
            f'QFrame[slicerRole="{role}"] QLabel {{ border: none; background: transparent; }}')
    if role == "iconTile":
        return f'QLabel[slicerRole="iconTile"] {{ background-color: {t["accent-subtle"]}; border: none; border-radius: 8px; }}'
    if role == "overline":
        return f'QLabel[slicerRole="overline"] {{ color: {t["text2"]}; font-size: {fontPx(11)}px; font-weight: 600; }}'
    if role == "primary":
        return (
            f'*[slicerRole="primary"] {{ background-color: {t["accent"]}; color: #ffffff; border: none; border-radius: 7px; padding: 0px 14px; min-height: 32px; font-weight: 600; }}'
            f'*[slicerRole="primary"]:hover {{ background-color: {t["accent-hover"]}; }}'
            f'*[slicerRole="primary"]:pressed {{ background-color: {t["accent-pressed"]}; }}'
            f'*[slicerRole="primary"]:disabled {{ background-color: {t["border"]}; color: {t["text-disabled"]}; }}'
            # Keyboard focus (USABILITY 9.2): a ring, the padding and minimum height reduced by its width so that the size does
            # not change. On the accent fill the ring has the text color (accent-fg would not stand out) and is 3 px wide:
            # the antialiased rounded edge of a 2 px border leaves less than a 2 px ring at 3:1 there.
            f'*[slicerRole="primary"]:focus {{ border: 3px solid {t["text"]}; padding: 0px 11px; min-height: 26px; }}')
    if role == "secondary":
        return (
            f'*[slicerRole="secondary"] {{ background-color: {t["button"]}; color: {t["text"]}; border: 1px solid {t["control-border"]}; border-radius: 7px; padding: 0px 12px; min-height: 32px; }}'
            f'*[slicerRole="secondary"]:hover {{ background-color: {t["control-hover"]}; }}'
            f'*[slicerRole="secondary"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="secondary"]:disabled {{ color: {t["text-disabled"]}; }}'
            f'*[slicerRole="secondary"]:focus {{ border: 2px solid {t["accent-fg"]}; padding: 0px 11px; min-height: 30px; }}')
    if role == "ghost":
        return (
            f'*[slicerRole="ghost"] {{ background-color: transparent; color: {t["text2"]}; border: 1px solid transparent; border-radius: 6px; padding: 3px; }}'
            f'*[slicerRole="ghost"]:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="ghost"]:checked {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:disabled {{ color: {t["text-disabled"]}; }}'
            f'*[slicerRole="ghost"]:focus {{ border: 2px solid {t["accent-fg"]}; padding: 2px; }}'
            f'QToolButton[slicerRole="ghost"]::menu-indicator {{ image: none; width: 0px; }}')
    if role == "segmented":
        return (
            f'QFrame[slicerRole="segmented"] {{ background-color: {t["canvas"]}; border: 1px solid {t["hairline"]}; border-radius: 8px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton {{ background-color: transparent; color: {t["text2"]}; border: none; border-radius: 6px; padding: 3px 12px; font-size: {fontPx(13)}px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:checked {{ background-color: {t["selected"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:disabled {{ color: {t["text-disabled"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:focus {{ border: 2px solid {t["accent-fg"]}; padding: 1px 10px; }}')
    if role == "chip":
        neutral = (t["control-hover"], t["text2"])
        levels = {
            "neutral": neutral,
            "busy": (t["accent-subtle"], t["accent-fg"]),
            "ok": (_rgba(token("success"), 0.16), t["success"]),
            "warn": (_rgba(token("warning"), 0.16), t["warning"]),
            "danger": (_rgba(token("danger"), 0.16), t["danger"]),
        }
        styleSheet = f'QLabel[slicerRole="chip"] {{ border: none; border-radius: 10px; padding: 0px 8px; min-height: 20px; max-height: 20px; font-size: {fontPx(12)}px; background-color: {neutral[0]}; color: {neutral[1]}; }}'
        for level, (background, foreground) in levels.items():
            styleSheet += f'QLabel[slicerRole="chip"][chipLevel="{level}"] {{ background-color: {background}; color: {foreground}; }}'
        return styleSheet
    if role == "kbd":
        return (
            f'QLabel[slicerRole="kbd"] {{ background-color: transparent; color: {t["text2"]}; border: 1px solid {t["border"]}; border-radius: 5px; padding: 0px 5px; font-size: {fontPx(11)}px; }}')
    if role == "topBar":
        return (
            f'QToolBar[slicerRole="topBar"] {{ background-color: {t["view-bar"]}; border: none; border-bottom: 1px solid {t["hairline"]}; padding: 0px; spacing: 0px; }}')
    return ""


# Roles that get a fallback stylesheet until the application style supports roles (SPEC 4.7)
_FALLBACK_ROLES = ("card", "cardLow", "iconTile", "overline", "primary", "secondary", "ghost", "segmented", "chip", "kbd", "topBar")


def setRole(widget, role):
    """Set the ``slicerRole`` property of ``widget`` and re-polish it.

    Roles change only paint and size, never behavior. Valid roles are listed in ``ROLES``.
    """
    if role not in ROLES:
        raise ValueError(f"Unknown slicerRole: {role}")
    widget.setProperty("slicerRole", role)
    if role in _FALLBACK_ROLES:
        bindStyleSheet(widget, lambda role=role: "" if supportsRoles() else _roleStyleSheet(role))
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


#
# Builders
#


#: Widgets sized by ``_font()``: [widget, size at a 13 px body, weight, letter spacing], resized when the application
#: font changes (USABILITY 9.4). Deleted widgets are dropped when the list is refreshed or has grown.
_fontRegistry = []
_fontRegistryPruneSize = 256
#: Dynamic property of the widgets in ``_fontRegistry``
_FONT_REGISTERED_PROPERTY = "studioFontRegistered"


def _applyFont(widget, pixelSize, weight, letterSpacing):
    font = qt.QFont(widget.font)
    font.setPixelSize(max(1, round(pixelSize)))
    if weight is not None:
        font.setWeight(weight)
    if letterSpacing is not None:
        font.setLetterSpacing(qt.QFont.AbsoluteSpacing, letterSpacing)
    widget.setFont(font)


def _font(widget, pixelSize, weight=None, letterSpacing=None):
    """Set a font of ``pixelSize`` px on ``widget`` and keep it in proportion to the application font.

    ``pixelSize`` is the size for the current application font: a ``fontPx()`` value, or a size designed for the 13 px
    body. When the application font changes, the widget gets the same ratio of the new font (``_refreshFonts()``).
    """
    _ensureWatcher()
    _applyFont(widget, pixelSize, weight, letterSpacing)
    _register(widget, pixelSize * DESIGN_BODY_PX / applicationFontPx(), weight, letterSpacing)


def _register(widget, designPx, weight, letterSpacing):
    """Keep ``widget`` at ``designPx`` (px at a 13 px body) in the ``_font()`` registry."""
    global _fontRegistryPruneSize
    if widget.property(_FONT_REGISTERED_PROPERTY):
        for entry in _fontRegistry:
            if entry[0] == widget:
                entry[1:] = [designPx, weight, letterSpacing]
                return
    widget.setProperty(_FONT_REGISTERED_PROPERTY, True)
    _fontRegistry.append([widget, designPx, weight, letterSpacing])
    if len(_fontRegistry) > _fontRegistryPruneSize:
        _fontRegistry[:] = [entry for entry in _fontRegistry if _isAlive(entry[0])]
        _fontRegistryPruneSize = max(256, 2 * len(_fontRegistry))


def _isStudioWidget(widget):
    """True if ``widget`` or the closest named part of the window above it is a Studio widget (``Studio*`` object name).

    The search stops at a Slicer widget (``qMRML*``, ``qSlicer*``: views, module panels, the main window).
    """
    while widget is not None:
        if str(widget.objectName).startswith("Studio"):
            return True
        if widget.className().startswith(("qMRML", "qSlicer")):
            return False
        widget = widget.parentWidget()
    return False


def _refreshStudioWidgetFonts(previousApplicationFontPx):
    """Make every Studio widget of the main window follow an application font change (USABILITY 9.4).

    - A widget under a stylesheet keeps the font that the stylesheet style resolved when it was polished: Qt marks it
      as set on the widget, so it no longer follows the application font. Under each topmost Studio stylesheet, the
      widgets whose font the application did not set get an empty font (the inherited one again), then the stylesheet
      is set again, which polishes them with the new font and the font rules of the stylesheets.
    - A Studio widget with a pixel font set directly (``widget.setFont()``, not ``_font()``) joins the ``_font()``
      registry with the same ratio to the previous application font, so that ``_refreshFonts()`` resizes it now and
      after each later change.
    """
    mainWindow = slicer.util.mainWindow() if hasattr(slicer.util, "mainWindow") else None
    if mainWindow is None:
        return
    styled = []
    explicit = []
    for widget in mainWindow.findChildren("QWidget"):
        if widget.styleSheet and str(widget.objectName).startswith("Studio"):
            styled.append(widget)
        if widget.testAttribute(qt.Qt.WA_SetFont) and widget.font.pixelSize() > 0 and not widget.property(_FONT_REGISTERED_PROPERTY):
            explicit.append(widget)
    for index, root in enumerate(styled):
        # isAncestorOf() is also true for the widget itself
        if any(other.isAncestorOf(root) for otherIndex, other in enumerate(styled) if otherIndex != index):
            continue
        # Parents before children (findChildren() lists the tree depth first)
        for widget in [root] + list(root.findChildren("QWidget")):
            if not widget.testAttribute(qt.Qt.WA_SetFont):
                widget.setFont(qt.QFont())
        styleSheet = root.styleSheet
        root.setStyleSheet("")
        root.setStyleSheet(styleSheet)
    for widget in explicit:
        if _isStudioWidget(widget):
            _register(widget, widget.font.pixelSize() * DESIGN_BODY_PX / previousApplicationFontPx, None, None)


def _refreshFonts():
    """Size the widgets of ``_font()`` for the current application font."""
    alive = []
    for entry in _fontRegistry:
        widget, designPx, weight, letterSpacing = entry
        if not _isAlive(widget):
            continue
        alive.append(entry)
        pixelSize = fontPx(designPx)
        if widget.font.pixelSize() != pixelSize:
            _applyFont(widget, pixelSize, weight, letterSpacing)
    _fontRegistry[:] = alive


def card(title=None):
    """Return ``(frame, layout)``: a rounded card (``card`` fill, 10 px radius, 14 px padding) with an optional section
    label (12 px SemiBold, text2, sentence case).
    """
    frame = qt.QFrame()
    frame.setObjectName("StudioCard")
    setRole(frame, "card")
    layout = qt.QVBoxLayout(frame)
    layout.setContentsMargins(CARD_PADDING, CARD_PADDING - 2, CARD_PADDING, CARD_PADDING)
    layout.setSpacing(8)
    if title:
        label = qt.QLabel(title)
        label.setObjectName("StudioCardTitle")
        label.setProperty("slicerRole", "sectionHeader")
        setText(label, "label")
        layout.addWidget(label)
    return frame, layout


def setText(widget, style, letterSpacing=None):
    """Give ``widget`` the font of a ``TYPE`` style ("display", "title", "section", "body", "name", "label", "caption",
    "small", "overline" or "number"), kept in proportion to the application font.
    """
    pixelSize, weight = TYPE[style]
    if letterSpacing is None and style == "display":
        # -1% at 22 px
        letterSpacing = -0.2
    _font(widget, fontPx(pixelSize), weight, letterSpacing)
    if style == "number":
        tabular(widget)
    return widget


def tabular(widget):
    """Use tabular figures in ``widget`` (Inter "tnum") where Qt supports font features (Qt 6.7 and later)."""
    font = qt.QFont(widget.font)
    setFeature = getattr(font, "setFeature", None)
    if setFeature is None:
        return widget
    try:
        setFeature("tnum", 1)
    except Exception:
        return widget
    widget.setFont(font)
    return widget


def label(text="", style="body", color="text", objectName=None, wrap=False):
    """Return a plain-text ``qt.QLabel`` in a ``TYPE`` style and a text color ("text", "text2" or "text3")."""
    widget = qt.QLabel(text)
    if objectName:
        widget.setObjectName(objectName)
    widget.setTextFormat(qt.Qt.PlainText)
    widget.setWordWrap(wrap)
    role = _TEXT_ROLES[color]
    if role is not None:
        widget.setProperty("slicerRole", role)
    setText(widget, style)
    return widget


def sectionLabel(text, objectName=None):
    """Return a section label: 12 px SemiBold, text2, sentence case ("Tools", "Open now")."""
    widget = label(text, "label", "text2", objectName)
    widget.setProperty("slicerRole", "sectionHeader")
    return widget


def overline(text, objectName=None):
    """Return an all-caps overline label (11 px SemiBold, text2): only for "CONTINUE" and the group headers of Search."""
    widget = qt.QLabel(text)
    if objectName:
        widget.setObjectName(objectName)
    widget.setTextFormat(qt.Qt.PlainText)
    setRole(widget, "overline")
    font = qt.QFont(widget.font)
    font.setPixelSize(fontPx(TYPE["overline"][0]))
    font.setWeight(TYPE["overline"][1])
    font.setCapitalization(qt.QFont.AllUppercase)
    font.setLetterSpacing(qt.QFont.PercentageSpacing, 104.0)
    widget.setFont(font)
    _register(widget, TYPE["overline"][0], TYPE["overline"][1], None)
    return widget


def iconTile(iconName, size=36, iconSize=18, objectName="StudioIconTile"):
    """Return a ``size`` px square label with the Lucide icon ``iconName`` (accent-fg) on an accent-subtle rounded square."""
    widget = qt.QLabel()
    widget.setObjectName(objectName)
    widget.setFixedSize(size, size)
    widget.setAlignment(qt.Qt.AlignCenter)
    setRole(widget, "iconTile")
    widget.setProperty("studioIconName", iconName)
    widget.setProperty("studioIconSize", iconSize)
    widget.setPixmap(pixmap(iconName, iconSize, "accent-fg"))
    _ensureWatcher()
    _iconTiles.append(widget)
    return widget


#: Icon tiles whose pixmap follows the palette (``iconTile()``)
_iconTiles = []


def _refreshIconTiles():
    alive = []
    for widget in _iconTiles:
        if not _isAlive(widget):
            continue
        alive.append(widget)
        widget.setPixmap(pixmap(str(widget.property("studioIconName")), int(widget.property("studioIconSize")), "accent-fg"))
    _iconTiles[:] = alive


def switch(text="", objectName=None):
    """Return a ``qt.QCheckBox`` drawn as a switch: its text on the left, the switch on its right edge."""
    widget = qt.QCheckBox(text)
    if objectName:
        widget.setObjectName(objectName)
    widget.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
    widget.setMinimumHeight(ROW_HEIGHT)
    setRole(widget, "switch")
    return widget


#: Notice levels: level -> (token of the tint, icon)
NOTICE_LEVELS = {"info": ("accent-fg", "info"), "warning": ("warning", "triangle-alert"), "danger": ("danger", "circle-alert"), "success": ("success", "circle-check")}


def notice(text, level="info", linkText=None, callback=None, objectName="StudioNotice"):
    """Return a notice (VISUAL.md 4.8): one sentence on an 8% tint of the level color with a 22% border (radius 8),
    a 14 px icon, and an optional link ("Details", "Read more") that calls ``callback``.

    The text is the ``StudioNoticeBoxText`` label, the link the ``StudioNoticeBoxLink`` button. Change the text and the
    level later with ``setNotice()``.
    """
    frame = qt.QFrame()
    frame.setObjectName(objectName)
    frame.setProperty("studioNoticeBox", True)
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(8)
    iconLabel = qt.QLabel()
    iconLabel.setObjectName("StudioNoticeBoxIcon")
    iconLabel.setFixedSize(16, 17)
    iconLabel.setAlignment(qt.Qt.AlignHCenter | qt.Qt.AlignTop)
    layout.addWidget(iconLabel, 0, qt.Qt.AlignTop)
    column = qt.QVBoxLayout()
    column.setSpacing(2)
    textLabel = qt.QLabel(text)
    textLabel.setObjectName("StudioNoticeBoxText")
    textLabel.setWordWrap(True)
    textLabel.setTextFormat(qt.Qt.PlainText)
    textLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    setText(textLabel, "caption")
    column.addWidget(textLabel)
    linkButton = None
    if linkText:
        linkButton = linkButtonWidget(linkText, callback, "StudioNoticeBoxLink")
        column.addWidget(linkButton, 0, qt.Qt.AlignLeft)
    layout.addLayout(column, 1)
    frame.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    setNotice(frame, text, level)
    return frame


def setNotice(frame, text=None, level=None):
    """Change the text and the level ("info", "warning", "danger", "success") of a ``notice()``."""
    if text is not None:
        frame.findChild("QLabel", "StudioNoticeBoxText").setText(text)
    if level is not None:
        if level not in NOTICE_LEVELS:
            raise ValueError(f"Unknown notice level: {level}")
        frame.setProperty("studioNoticeLevel", level)

        def styleSheet(frame=frame):
            colorName, iconName = NOTICE_LEVELS[str(frame.property("studioNoticeLevel"))]
            color = token(colorName)
            iconLabel = frame.findChild("QLabel", "StudioNoticeBoxIcon")
            if iconLabel is not None:
                iconLabel.setPixmap(pixmap(iconName, 14, colorName))
            return (
                f'QFrame[studioNoticeBox="true"] {{ background-color: {_rgba(color, 0.08)}; border: 1px solid {_rgba(color, 0.22)}; border-radius: 8px; }}'
                f'QFrame[studioNoticeBox="true"] QLabel {{ background: transparent; border: none; color: {token("text").name()}; }}')

        bindStyleSheet(frame, styleSheet)
    return frame


def linkButtonWidget(text, callback=None, objectName="StudioLink"):
    """Return a flat push button that looks like a link (accent-fg text, underlined when hovered)."""
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setFlat(True)
    button.setProperty("studioLink", True)
    button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    button.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Fixed)
    setText(button, "caption")
    if callback is not None:
        button.connect("clicked()", callback)
    installButtonKeys(button)
    bindStyleSheet(button, lambda: (
        f'QPushButton[studioLink="true"] {{ background: transparent; border: none; color: {token("accent-fg").name()}; padding: 0px; text-align: left; }}'
        'QPushButton[studioLink="true"]:hover { text-decoration: underline; }'
        f'QPushButton[studioLink="true"]:focus {{ border: 1px solid {token("accent-fg").name()}; border-radius: 3px; }}'))
    return button


def listRow(iconName, text, value=None, callback=None, objectName="StudioListRow"):
    """Return a 34 px row button: a 16 px icon, the text, an optional text3 value and a chevron (Draw: "Normalize to MNI",
    "Trace from a figure", "Notes"). The value label is the ``StudioListRowValue`` child.
    """
    button = qt.QPushButton()
    button.setObjectName(objectName)
    button.setFixedHeight(LIST_ROW_HEIGHT)
    button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    button.setAccessibleName(text)
    setRole(button, "ghost")
    layout = qt.QHBoxLayout(button)
    layout.setContentsMargins(8, 0, 6, 0)
    layout.setSpacing(10)
    iconLabel = qt.QLabel()
    iconLabel.setObjectName("StudioListRowIcon")
    iconLabel.setProperty("studioIconName", iconName)
    iconLabel.setFixedSize(16, 16)
    layout.addWidget(iconLabel)
    textLabel = label(text, "body", "text", "StudioListRowText")
    layout.addWidget(textLabel, 1)
    valueLabel = label(value or "", "caption", "text3", "StudioListRowValue")
    layout.addWidget(valueLabel)
    # Only once it is in the row: showing a label without a parent would open it as a window (a native window that is
    # created and destroyed again, 20 ms each at the first open of Draw)
    valueLabel.setVisible(bool(value))
    chevron = qt.QLabel()
    chevron.setObjectName("StudioListRowChevron")
    chevron.setFixedSize(14, 14)
    layout.addWidget(chevron)
    for child in (iconLabel, textLabel, valueLabel, chevron):
        child.setAttribute(qt.Qt.WA_TransparentForMouseEvents)

    def refreshIcons(button=button):
        if not _isAlive(button):
            removePaletteListener(refreshIcons)
            return
        rowIcon = button.findChild("QLabel", "StudioListRowIcon")
        rowIcon.setPixmap(pixmap(str(rowIcon.property("studioIconName")), 16, "text2"))
        button.findChild("QLabel", "StudioListRowChevron").setPixmap(pixmap("chevron-right", 14, "text3"))

    refreshIcons()
    addPaletteListener(refreshIcons)
    if callback is not None:
        button.connect("clicked()", callback)
    installButtonKeys(button)
    return button


class _ElideFilter(qt.QObject):
    """Elides the text of a one-line label to its width when it is resized (``elideLabel()``)."""

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            _showElided(obj)
        return False


_elideFilter = None


def _showElided(label):
    text = str(label.property("studioFullText") or "")
    width = int(label.width) - int(label.contentsMargins().left()) - int(label.contentsMargins().right())
    # Before the label is laid out its width says nothing: the whole text is shown
    shown = label.fontMetrics().elidedText(text, qt.Qt.ElideRight, width) if width > 40 else text
    if str(label.text) != shown:
        label.setText(shown)
    label.setToolTip(text if shown != text else "")


def elideLabel(label):
    """Make ``label`` a one-line label whose text is elided ("…") to its width; set its text with ``setElidedText()``.
    The whole text is the tooltip while it is elided. The label never widens its container (Ignored width).
    """
    global _elideFilter
    if _elideFilter is None:
        _elideFilter = _ElideFilter()
    label.setWordWrap(False)
    label.setTextFormat(qt.Qt.PlainText)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
    label.setProperty("studioElided", True)
    label.installEventFilter(_elideFilter)
    return label


def setElidedText(label, text):
    """Set the text of an ``elideLabel()``."""
    label.setProperty("studioFullText", text or "")
    _showElided(label)


def setListRowValue(row, value):
    """Change the value text of a ``listRow()`` (hidden when empty)."""
    valueLabel = row.findChild("QLabel", "StudioListRowValue")
    valueLabel.setText(value or "")
    valueLabel.setVisible(bool(value))


def setChipLevel(chipLabel, level):
    """Change the level of a chip created by ``chip()``."""
    if level not in CHIP_LEVELS:
        raise ValueError(f"Unknown chip level: {level}")
    if chipLabel.property("chipLevel") == level:
        return
    chipLabel.setProperty("chipLevel", level)
    style = chipLabel.style()
    style.unpolish(chipLabel)
    style.polish(chipLabel)
    chipLabel.update()


def chip(text, level="neutral"):
    """Return a 20 px pill label. ``level`` is one of ``CHIP_LEVELS``."""
    label = qt.QLabel(text)
    label.setObjectName("StudioChip")
    label.setProperty("chipLevel", level)
    label.setAlignment(qt.Qt.AlignCenter)
    label.setFixedHeight(20)
    setRole(label, "chip")
    setChipLevel(label, level)
    return label


def kbd(text):
    """Return a keyboard key label (for example "Ctrl K")."""
    label = qt.QLabel(text)
    label.setObjectName("StudioKbd")
    label.setAlignment(qt.Qt.AlignCenter)
    setRole(label, "kbd")
    return label


def primaryButton(text):
    """Return the 32 px accent call-to-action button of a panel."""
    button = qt.QPushButton(text)
    button.setObjectName("StudioPrimaryButton")
    button.setMinimumHeight(32)
    button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    setRole(button, "primary")
    return button


def segmented(labels):
    """Return ``(frame, buttonGroup)``: exclusive checkable buttons in a segmented control.

    The buttons (``buttonGroup.buttons()``) have the ids 0, 1, 2... in the order of ``labels``.
    """
    frame = qt.QFrame()
    frame.setObjectName("StudioSegmented")
    frame.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(2, 2, 2, 2)
    layout.setSpacing(2)
    group = qt.QButtonGroup(frame)
    group.setExclusive(True)
    for index, text in enumerate(labels):
        button = qt.QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        group.addButton(button, index)
        layout.addWidget(button)
    setRole(frame, "segmented")
    return frame, group


def setRoundedMask(widget, radius):
    """Round the corners of a top-level ``widget`` (a popup, an overlay card) with a mask of its current size: popups
    have no translucent background without a compositor. Call it again after a resize.
    """
    width, height = int(widget.width), int(widget.height)
    if width <= 0 or height <= 0:
        return
    mask = qt.QBitmap(width, height)
    mask.fill(qt.Qt.color0)
    painter = qt.QPainter(mask)
    painter.setPen(qt.Qt.NoPen)
    painter.setBrush(qt.QBrush(qt.Qt.color1))
    painter.drawRoundedRect(0, 0, width, height, radius, radius)
    painter.end()
    widget.setMask(mask)


#
# Tooltips, contrast, screens and font sizes
#


def keyText(key):
    r"""``key`` ("Ctrl+K", "Esc") as displayed on this platform (``QKeySequence.NativeText``: "⌘K" on macOS).

    A text that is not exactly one key sequence ("Ctrl+\ or Ctrl+5", "Shift+wheel") is returned as given.
    """
    if not key:
        return ""
    key = str(key)
    if " " in key.strip():
        return key
    sequence = qt.QKeySequence(key)
    if sequence.count() != 1 or sequence.toString(qt.QKeySequence.PortableText).lower() != key.lower():
        return key
    return sequence.toString(qt.QKeySequence.NativeText) or key


def tooltip(name, key=None, description=None):
    """Return the Studio tooltip "Name · key · description" (USABILITY 10): the key as displayed on this platform.

    The parts are escaped: a key such as "Ctrl+<" is not read as markup.
    """
    text = " · ".join(part for part in (str(name), keyText(key), str(description) if description else "") if part)
    escaped = html.escape(text, quote=False)
    # Plain text unless escaping was needed (rich text tooltips are word-wrapped)
    return escaped if escaped == text else f"<qt>{escaped}</qt>"


def _relativeLuminance(color):
    color = qt.QColor(color)

    def channel(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrastRatio(color1, color2):
    """WCAG 2.x contrast ratio (1 to 21) of two colors (``qt.QColor`` or "#rrggbb")."""
    luminances = sorted((_relativeLuminance(color1), _relativeLuminance(color2)), reverse=True)
    return (luminances[0] + 0.05) / (luminances[1] + 0.05)


def _validScreen(screen):
    if screen is None:
        return None
    try:
        screen.geometry
    except ValueError:
        # A wrapper without a live QScreen
        return None
    return screen


def primaryScreen():
    """The primary screen, or None.

    In this PythonQt build ``slicer.app.primaryScreen`` is None and ``QGuiApplication.primaryScreen`` is a
    property of the class that does not hold a live screen (it is a method elsewhere): the first screen of
    ``QGuiApplication.screens()`` (the primary screen, by definition) is used then.
    """
    screen = getattr(qt.QGuiApplication, "primaryScreen", None)
    screen = _validScreen(screen() if callable(screen) else screen)
    if screen is None:
        screens = qt.QGuiApplication.screens()
        screen = _validScreen(screens[0]) if screens else None
    return screen


def screenOf(widget):
    """The screen that shows ``widget``, else the primary screen."""
    screen = _validScreen(widget.screen()) if widget is not None and callable(getattr(widget, "screen", None)) else None
    return screen if screen is not None else primaryScreen()


#: Pixel size of the body text that the Studio pixel sizes are designed for (SPEC 4.5)
DESIGN_BODY_PX = 13.0


def applicationFontPx():
    """Pixel size of the application font (a point size is converted at 96 dpi)."""
    font = qt.QApplication.font()
    pixelSize = qt.QFontInfo(font).pixelSize()
    if pixelSize > 0:
        return pixelSize
    return max(1.0, font.pointSizeF() * 96.0 / 72.0)


def fontPx(basePx):
    """Pixel size ``basePx`` (designed for a 13 px body) scaled with the application font (USABILITY 9.4)."""
    return max(1, round(basePx * applicationFontPx() / DESIGN_BODY_PX))


#
# Keyboard activation of buttons
#

_ACTIVATION_KEYS = (qt.Qt.Key_Space, qt.Qt.Key_Return, qt.Qt.Key_Enter)
_buttonKeys = None


def hasKeyModifiers(event):
    """True if Shift, Ctrl, Alt or Meta is held during the key event (the keypad modifier does not count)."""
    mask = qt.Qt.ShiftModifier | qt.Qt.ControlModifier | qt.Qt.AltModifier | qt.Qt.MetaModifier
    try:
        # Explicit integers: a QFlags value may not compare equal to an enum value (Qt 6)
        return bool(int(event.modifiers()) & int(mask))
    except (TypeError, ValueError):
        return bool(event.modifiers() & mask)


class _ButtonKeys(qt.QObject):
    """Space, Return and Enter press a focused button.

    The shortcut override is accepted, so that window-wide shortcuts on these keys (the Space shortcut
    of the Segment Editor) do not take them from a focused button, and the key press clicks the button
    (a QToolButton does not react to Return by itself).
    """

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType not in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress, qt.QEvent.KeyRelease):
            return False
        if event.key() not in _ACTIVATION_KEYS or hasKeyModifiers(event) or not obj.hasFocus() or not obj.isEnabled():
            return False
        if eventType == qt.QEvent.ShortcutOverride:
            event.accept()
            return True
        if eventType == qt.QEvent.KeyPress and not event.isAutoRepeat():
            obj.click()
        # Also the key release: the one of Space would click the button a second time
        return True


def installButtonKeys(button):
    """Let Space, Return and Enter press ``button`` (a QAbstractButton) when it has the keyboard focus.

    One shared event filter serves every button. Return ``button``.
    """
    global _buttonKeys
    if _buttonKeys is None:
        _buttonKeys = _ButtonKeys()
    button.installEventFilter(_buttonKeys)
    return button


#
# Height-for-width rows and notice lines
#

#: Width below which a ``wrapRow()`` (and a ``noticeLine()``) stacks its widgets
WRAP_ROW_WIDTH = 300
#: Narrowest text column of a ``noticeLine()`` that keeps its buttons on the same row
NOTICE_TEXT_MIN_WIDTH = 150
#: Margins and spacings of a ``noticeLine()``
NOTICE_MARGIN_LEFT = 12
NOTICE_MARGIN_RIGHT = 8
NOTICE_SPACING = 6
NOTICE_BUTTON_SPACING = 2
NOTICE_LINE_HEIGHT = 32
_wrapFilter = None


class _WrapFilter(qt.QObject):
    """Switches the box layout of a widget between a row and a column when the widget is resized
    (the threshold is the ``studioWrapWidth`` property of the widget).
    """

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            layout = obj.layout()
            if layout is not None and hasattr(layout, "setDirection"):
                threshold = obj.property("studioWrapWidth") or WRAP_ROW_WIDTH
                direction = qt.QBoxLayout.TopToBottom if obj.width < threshold else qt.QBoxLayout.LeftToRight
                if layout.direction() != direction:
                    layout.setDirection(direction)
        return False


def _installWrap(widget, width):
    global _wrapFilter
    if _wrapFilter is None:
        _wrapFilter = _WrapFilter()
    widget.setProperty("studioWrapWidth", int(width))
    policy = qt.QSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    policy.setHeightForWidth(True)
    widget.setSizePolicy(policy)
    widget.installEventFilter(_wrapFilter)


def wrapRow(widgets, width=WRAP_ROW_WIDTH, spacing=6):
    """Return a widget that lays ``widgets`` out in a row, stacked when it is narrower than ``width`` px.

    Its horizontal size policy is Ignored: a row never makes its container (the inspector) wider.
    """
    row = qt.QWidget()
    row.setObjectName("StudioWrapRow")
    layout = qt.QBoxLayout(qt.QBoxLayout.LeftToRight, row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    _installWrap(row, width)
    return row


def noticeLine(text, actions=()):
    """Return a notice for the inspector notice slot (``shell.setInspectorNotice()``): one 32 px line with a
    word-wrapped text and buttons, for example ``noticeLine(text, noticeActions(notNow=close, details=show))``.

    ``actions`` are ``(text, callback)`` pairs. The text is the ``StudioNoticeText`` label and the buttons are
    named ``StudioNoticeButton``. The buttons go under the text when the inspector is narrow (below
    ``WRAP_ROW_WIDTH``, or below the width that leaves the text ``NOTICE_TEXT_MIN_WIDTH`` px next to the buttons),
    and the notice has an Ignored horizontal size policy: it never widens the inspector. The caller may rename the
    frame: its look follows the ``studioNoticeLine`` property, not the object name.
    """
    frame = qt.QFrame()
    frame.setObjectName("StudioNoticeLine")
    frame.setProperty("studioNoticeLine", True)
    frame.setMinimumHeight(NOTICE_LINE_HEIGHT)
    layout = qt.QBoxLayout(qt.QBoxLayout.LeftToRight, frame)
    layout.setContentsMargins(NOTICE_MARGIN_LEFT, 4, NOTICE_MARGIN_RIGHT, 4)
    layout.setSpacing(NOTICE_SPACING)
    label = qt.QLabel(text)
    label.setObjectName("StudioNoticeText")
    label.setWordWrap(True)
    layout.addWidget(label, 1)
    wrapWidth = WRAP_ROW_WIDTH
    if actions:
        buttonRow = qt.QHBoxLayout()
        buttonRow.setSpacing(NOTICE_BUTTON_SPACING)
        buttonRow.addStretch(1)
        buttonsWidth = 0
        for buttonText, callback in actions:
            button = qt.QToolButton()
            button.setObjectName("StudioNoticeButton")
            button.setText(buttonText)
            button.setAutoRaise(True)
            button.setFocusPolicy(qt.Qt.TabFocus)
            button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            button.connect("clicked()", callback)
            setRole(button, "ghost")
            installButtonKeys(button)
            buttonRow.addWidget(button)
            buttonsWidth += button.sizeHint.width() + NOTICE_BUTTON_SPACING
        layout.addLayout(buttonRow)
        # Three buttons ("Take the tour · What moved · Not now") leave the text no room in a 340 px inspector
        wrapWidth = max(wrapWidth, NOTICE_MARGIN_LEFT + NOTICE_MARGIN_RIGHT + NOTICE_SPACING + NOTICE_TEXT_MIN_WIDTH + buttonsWidth)
    _installWrap(frame, wrapWidth)
    # VISUAL 4.8: an 8% tint of the accent with a 22% border, rounded by 8 px
    bindStyleSheet(frame, lambda: (
        f"QFrame[studioNoticeLine=\"true\"] {{ background-color: {_rgba(token('accent-fg'), 0.08)}; border: 1px solid {_rgba(token('accent-fg'), 0.22)};"
        " border-radius: 8px; }"
        f"QLabel#StudioNoticeText {{ color: {token('text').name()}; background: transparent; border: none; }}"
        f"QToolButton#StudioNoticeButton {{ color: {token('accent-fg').name()}; }}"))
    return frame


def noticeActions(notNow=None, details=None):
    """The usual actions of an offer notice: "Not now" and "Details" (an action is left out when its callback is None)."""
    actions = []
    if notNow is not None:
        actions.append((_("Not now"), notNow))
    if details is not None:
        actions.append((_("Details"), details))
    return actions


#
# Dialog chrome, the backdrop and settings pages (VISUAL 4.6 and 4.7, V-02)
#

#: Padding of a dialog
DIALOG_PADDING = 24
#: Backdrop behind a dialog and behind Search and the cheat sheet (the black wash over the main window)
DIALOG_BACKDROP_ALPHA = 0.6
PALETTE_BACKDROP_ALPHA = 0.5
#: Width of the numeric fields of a settings page
SETTINGS_NUMBER_WIDTH = 88


class Backdrop(qt.QWidget):
    """A black wash over a window (``parent``): shown behind a dialog, Search or the cheat sheet. It follows the size of
    its parent and is transparent for the mouse (the popup or dialog above it takes the clicks).
    """

    def __init__(self, parent, alpha=DIALOG_BACKDROP_ALPHA):
        qt.QWidget.__init__(self, parent)
        self.setObjectName("StudioBackdrop")
        self.alpha = float(alpha)
        self.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        self.setAttribute(qt.Qt.WA_NoSystemBackground)
        self.setFocusPolicy(qt.Qt.NoFocus)
        self.hide()

    def paintEvent(self, event):
        painter = qt.QPainter(self)
        try:
            painter.fillRect(self.rect, qt.QColor(0, 0, 0, round(255 * self.alpha)))
        finally:
            painter.end()

    def cover(self):
        """Show over the whole parent window, above its other children."""
        parent = self.parentWidget()
        if parent is None:
            return
        self.setGeometry(0, 0, parent.width, parent.height)
        self.show()
        self.raise_()


class _BackdropFilter(qt.QObject):
    """Keeps a backdrop the size of its window."""

    def __init__(self, backdrop):
        qt.QObject.__init__(self)
        self.backdrop = backdrop

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize and _isAlive(self.backdrop) and self.backdrop.isVisible():
            self.backdrop.setGeometry(0, 0, obj.width, obj.height)
        return False


#: window -> (Backdrop, filter, number of owners shown)
_backdrops = {}


def showBackdrop(window, alpha=DIALOG_BACKDROP_ALPHA):
    """Show the backdrop of ``window`` (the main window) with ``alpha``. Each call needs one ``hideBackdrop(window)``."""
    if window is None or not _isAlive(window):
        return None
    key = id(window)
    entry = _backdrops.get(key)
    if entry is None or not _isAlive(entry[0]):
        backdrop = Backdrop(window, alpha)
        watcher = _BackdropFilter(backdrop)
        window.installEventFilter(watcher)
        entry = [backdrop, watcher, 0]
        _backdrops[key] = entry
    entry[0].alpha = float(alpha)
    entry[2] += 1
    entry[0].cover()
    entry[0].update()
    return entry[0]


def hideBackdrop(window):
    """Hide the backdrop of ``window`` when its last owner is gone."""
    if window is None:
        return
    entry = _backdrops.get(id(window))
    if entry is None:
        return
    entry[2] = max(0, entry[2] - 1)
    if entry[2] == 0 and _isAlive(entry[0]):
        entry[0].hide()


def isBackdropShown(window):
    entry = _backdrops.get(id(window)) if window is not None else None
    return entry is not None and _isAlive(entry[0]) and entry[0].isVisible()


class _DialogChromeFilter(qt.QObject):
    """The rounded mask of a dialog after each resize, the backdrop while it is shown, and moving it by its background (a
    frameless dialog has no title bar).
    """

    def __init__(self, backdrop):
        qt.QObject.__init__(self)
        self.backdrop = backdrop
        self._dragOffset = None
        self._shownOver = None

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Resize:
            setRoundedMask(obj, RADIUS["dialog"])
        elif eventType == qt.QEvent.Show:
            setRoundedMask(obj, RADIUS["dialog"])
            if self.backdrop and self._shownOver is None:
                window = obj.parentWidget().window() if obj.parentWidget() is not None else None
                if window is not None and window is not obj:
                    self._shownOver = window
                    showBackdrop(window, DIALOG_BACKDROP_ALPHA)
                    obj.raise_()
        elif eventType == qt.QEvent.Hide:
            if self._shownOver is not None:
                hideBackdrop(self._shownOver)
                self._shownOver = None
        elif eventType == qt.QEvent.MouseButtonPress and event.button() == qt.Qt.LeftButton:
            position = event.globalPos() if hasattr(event, "globalPos") else event.globalPosition().toPoint()
            self._dragOffset = position - obj.frameGeometry.topLeft()
        elif eventType == qt.QEvent.MouseMove and self._dragOffset is not None and (event.buttons() & qt.Qt.LeftButton):
            position = event.globalPos() if hasattr(event, "globalPos") else event.globalPosition().toPoint()
            obj.move(position - self._dragOffset)
        elif eventType == qt.QEvent.MouseButtonRelease:
            self._dragOffset = None
        return False


#: id(dialog) -> its _DialogChromeFilter
_chromeFilters = {}


def dialogChrome(dialog, backdrop=True, styleSheet=None):
    """Give a Studio dialog the dialog chrome (VISUAL 4.7): frameless, the ``card`` fill, a 1 px border, 14 px corners and a
    60% black backdrop over the main window while it is shown. Call it before the dialog is shown. The dialog keeps
    Esc and Enter, and can be moved by its background. ``styleSheet``: a function returning the dialog's own rules (one
    stylesheet is bound per widget, see ``bindStyleSheet()``).
    """
    dialog.setWindowFlag(qt.Qt.FramelessWindowHint, True)
    dialog.setProperty("studioDialogChrome", True)
    chromeFilter = _DialogChromeFilter(backdrop)
    dialog.installEventFilter(chromeFilter)
    # The filter lives as long as the dialog
    key = id(dialog)
    _chromeFilters[key] = chromeFilter

    def forget(*args, key=key):
        chromeFilter = _chromeFilters.pop(key, None)
        if chromeFilter is not None and chromeFilter._shownOver is not None:
            hideBackdrop(chromeFilter._shownOver)

    dialog.connect("destroyed()", forget)
    bindStyleSheet(dialog, lambda: (
        f'QDialog[studioDialogChrome="true"] {{ background-color: {token("card").name()}; border: 1px solid {token("border").name()};'
        f' border-radius: {RADIUS["dialog"]}px; }}') + (styleSheet() if styleSheet is not None else ""))
    return dialog


def dialogHeader(iconName, title, body=None, objectPrefix="StudioDialog"):
    """The header of a dialog (VISUAL 4.7): a 40 px icon tile beside the title (18/600) and the body text (text2).
    Return ``(layout, titleLabel, bodyLabel)``.
    """
    header = qt.QHBoxLayout()
    header.setSpacing(14)
    header.addWidget(iconTile(iconName, 40, 20, f"{objectPrefix}Icon"), 0, qt.Qt.AlignTop)
    column = qt.QVBoxLayout()
    column.setSpacing(6)
    titleLabel = label(title, "title", "text", f"{objectPrefix}Title", wrap=True)
    column.addWidget(titleLabel)
    bodyLabel = None
    if body:
        bodyLabel = label(body, "body", "text2", f"{objectPrefix}Body", wrap=True)
        column.addWidget(bodyLabel)
    header.addLayout(column, 1)
    return header, titleLabel, bodyLabel


def settingsPageHeader(layout, title, description=None, objectPrefix="StudioSettings"):
    """The top of a Studio settings page (VISUAL 4.7): the page title (18/600) and one line of description (text2)."""
    layout.addWidget(label(title, "title", "text", f"{objectPrefix}Title"))
    if description:
        layout.addWidget(label(description, "body", "text2", f"{objectPrefix}Description", wrap=True))
    layout.addSpacing(14)


def settingsRow(title, control=None, subtitle=None, last=False, objectName=None):
    """A 44 px row of a settings page: the title (and an optional text3 subtitle) on the left, ``control`` on the right,
    a hairline under it unless ``last``. Return the row frame; its title label is the ``StudioSettingsRowTitle`` child.
    """
    frame = qt.QFrame()
    frame.setObjectName(objectName or "StudioSettingsRow")
    frame.setProperty("studioSettingsRow", "last" if last else "row")
    frame.setMinimumHeight(SETTINGS_ROW_HEIGHT)
    rowLayout = qt.QHBoxLayout(frame)
    rowLayout.setContentsMargins(0, 6, 0, 6)
    rowLayout.setSpacing(12)
    column = qt.QVBoxLayout()
    column.setSpacing(1)
    titleLabel = label(title, "body", "text", "StudioSettingsRowTitle", wrap=True)
    column.addWidget(titleLabel)
    if subtitle:
        column.addWidget(label(subtitle, "caption", "text3", "StudioSettingsRowSubtitle", wrap=True))
    rowLayout.addLayout(column, 1)
    if control is not None:
        rowLayout.addWidget(control, 0, qt.Qt.AlignVCenter)
    return frame


def settingsStyleSheet():
    """The rules of the settings rows (``settingsRow()``) and fields (``numberField()``) of a page."""
    return (
        f'QFrame[studioSettingsRow="row"] {{ border: none; border-bottom: 1px solid {token("hairline").name()}; background: transparent; }}'
        'QFrame[studioSettingsRow="last"] { border: none; background: transparent; }'
        'QFrame[studioSettingsRow] QLabel { border: none; background: transparent; }'
        f'QFrame[studioFolderCard="true"] {{ background-color: {token("card-low").name()}; border: 1px solid {token("hairline").name()}; border-radius: 10px; }}'
        'QLabel[studioCardText="true"] { border: none; background: transparent; }')


def numberField(minimum, maximum, value, suffix="", objectName=None):
    """An 88 px right-aligned numeric field with its unit (``suffix``), for a settings row."""
    spinBox = qt.QSpinBox()
    if objectName:
        spinBox.setObjectName(objectName)
    spinBox.setRange(int(minimum), int(maximum))
    spinBox.setValue(int(value))
    spinBox.setSuffix(suffix)
    spinBox.setAlignment(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
    spinBox.setFixedWidth(SETTINGS_NUMBER_WIDTH)
    spinBox.setMinimumHeight(COMPACT_HEIGHT + 2)
    return spinBox


def folderCard(iconName="folder", objectName="StudioFolderCard"):
    """A folder card (VISUAL 4.7): an icon tile, the path (``pathLabel``) and a meta line (``metaLabel``), and a button
    row on the right (``buttonLayout``). Return ``(frame, pathLabel, metaLabel, buttonLayout)``.
    """
    frame = qt.QFrame()
    frame.setObjectName(objectName)
    frame.setProperty("studioFolderCard", True)
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(12)
    layout.addWidget(iconTile(iconName, 36, 18, f"{objectName}Icon"), 0, qt.Qt.AlignVCenter)
    column = qt.QVBoxLayout()
    column.setSpacing(2)
    pathLabel = label("", "name", "text", f"{objectName}Path")
    pathLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    pathLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    column.addWidget(pathLabel)
    metaLabel = label("", "caption", "text3", f"{objectName}Meta", wrap=True)
    metaLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    column.addWidget(metaLabel)
    for textLabel in (pathLabel, metaLabel):
        textLabel.setProperty("studioCardText", True)
    layout.addLayout(column, 1)
    buttonLayout = qt.QHBoxLayout()
    buttonLayout.setSpacing(8)
    layout.addLayout(buttonLayout)
    return frame, pathLabel, metaLabel, buttonLayout
