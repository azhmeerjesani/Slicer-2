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

ROLES = ("card", "primary", "secondary", "ghost", "segmented", "toolTile", "sectionHeader", "secondaryText", "caption", "chip", "kbd", "topBar", "compact")

CHIP_LEVELS = ("neutral", "busy", "ok", "warn", "danger")

# Token values of the dark and light themes, used when the application style does not provide
# them. Keep in sync with qSlicerStyle::tokenColor() (Base/QTGUI/qSlicerStyle.cxx).
_DARK_TOKENS = {
    "canvas": "#141416",
    "card": "#232327",
    "control-hover": "#35353b",
    "control-pressed": "#26262a",
    "hairline": "#2e2e34",
    "border": "#3a3a41",
    "control-border": "#71717a",
    "text2": "#a1a1aa",
    "text3": "#94949c",
    "text-disabled": "#71717a",
    "accent": "#3568d4",
    "accent-hover": "#3f74e3",
    "accent-pressed": "#2c5bbf",
    "accent-fg": "#6ea3ff",
    "accent-subtle": "#22324f",
    "success": "#3fb950",
    "warning": "#e3b341",
    "danger": "#f85149",
    "view-bar": "#161618",
    "lesion": "#f2545b",
}

_LIGHT_TOKENS = {
    "canvas": "#e4e4e8",
    "card": "#ffffff",
    "control-hover": "#eeeef2",
    "control-pressed": "#e4e4e9",
    "hairline": "#e1e1e6",
    "border": "#cacad1",
    "control-border": "#7a7a84",
    "text2": "#5c5c66",
    "text3": "#63636c",
    "text-disabled": "#8e8e98",
    "accent": "#2f6fde",
    # accent-hover and accent-pressed are derived from accent (lightness +/- 8%)
    "accent-fg": "#2158bd",
    "accent-subtle": "#e3ecfc",
    "success": "#1a7f37",
    "warning": "#9a6700",
    "danger": "#cf222e",
    "view-bar": "#e9e9ed",
    "lesion": "#f2545b",
}

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

    Style tokens are listed in SPEC 4.2 and 4.3 (``canvas``, ``card``, ``hairline``, ``border``,
    ``control-border``, ``text2``, ``text3``, ``text-disabled``, ``accent``, ``accent-fg``,
    ``accent-subtle``, ``success``, ``warning``, ``danger``, ``view-bar``...). Palette roles are also available as ``window``, ``text``,
    ``base``, ``button``, ``highlight``, ``tooltip`` and ``tooltip-text``.
    """
    role = _PALETTE_TOKENS.get(name)
    if role is not None:
        return slicer.app.palette().color(qt.QPalette.Active, role)
    styleToken = getattr(slicer.app.style(), "token", None)
    if styleToken is not None:
        color = styleToken(name)
        if color.isValid():
            return color
    if isDark():
        value = _DARK_TOKENS.get(name)
    else:
        value = _LIGHT_TOKENS.get(name)
        if value is None and name in ("accent-hover", "accent-pressed"):
            return _shiftLightness(qt.QColor(_LIGHT_TOKENS["accent"]), 0.08 if name == "accent-hover" else -0.08)
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


class _PaletteWatcher(qt.QObject):
    """Event filter on a hidden top-level widget: it receives the application palette and style changes."""

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
        if event.type() in (qt.QEvent.ApplicationPaletteChange, qt.QEvent.PaletteChange, qt.QEvent.StyleChange):
            # The style and the palette are often changed together: refresh once
            self.timer.start()
        return False


def _ensureWatcher():
    global _watcher
    if _watcher is None:
        _watcher = _PaletteWatcher()


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
    _iconCache.clear()
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
        "window", "text", "button", "card", "hairline", "border", "control-border", "text2", "text3", "text-disabled", "control-hover", "control-pressed",
        "accent", "accent-hover", "accent-pressed", "accent-fg", "accent-subtle", "success", "warning", "danger", "view-bar")}
    if role == "card":
        return (
            f'QFrame[slicerRole="card"] {{ background-color: {t["card"]}; border: 1px solid {t["hairline"]}; border-radius: 8px; }}'
            f'QFrame[slicerRole="card"] QLabel[slicerRole="sectionHeader"] {{ color: {t["text2"]}; font-size: 11px; font-weight: 600; border: none; background: transparent; }}'
            f'QFrame[slicerRole="card"] QLabel {{ border: none; background: transparent; }}')
    if role == "primary":
        return (
            f'*[slicerRole="primary"] {{ background-color: {t["accent"]}; color: #ffffff; border: none; border-radius: 6px; padding: 0px 14px; min-height: 32px; font-weight: 600; }}'
            f'*[slicerRole="primary"]:hover {{ background-color: {t["accent-hover"]}; }}'
            f'*[slicerRole="primary"]:pressed {{ background-color: {t["accent-pressed"]}; }}'
            f'*[slicerRole="primary"]:disabled {{ background-color: {t["border"]}; color: {t["text-disabled"]}; }}')
    if role == "secondary":
        return (
            f'*[slicerRole="secondary"] {{ background-color: {t["button"]}; color: {t["text"]}; border: 1px solid {t["control-border"]}; border-radius: 6px; padding: 0px 12px; min-height: 28px; }}'
            f'*[slicerRole="secondary"]:hover {{ background-color: {t["control-hover"]}; }}'
            f'*[slicerRole="secondary"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="secondary"]:disabled {{ color: {t["text-disabled"]}; }}')
    if role == "ghost":
        return (
            f'*[slicerRole="ghost"] {{ background-color: transparent; color: {t["text2"]}; border: 1px solid transparent; border-radius: 6px; padding: 3px; }}'
            f'*[slicerRole="ghost"]:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="ghost"]:checked {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:disabled {{ color: {t["text-disabled"]}; }}'
            f'QToolButton[slicerRole="ghost"]::menu-indicator {{ image: none; width: 0px; }}')
    if role == "segmented":
        return (
            f'QFrame[slicerRole="segmented"] {{ background-color: {t["control-pressed"]}; border: 1px solid {t["hairline"]}; border-radius: 8px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton {{ background-color: transparent; color: {t["text2"]}; border: none; border-radius: 6px; padding: 3px 12px; font-size: 13px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:checked {{ background-color: {t["accent-subtle"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:disabled {{ color: {t["text-disabled"]}; }}')
    if role == "chip":
        neutral = (t["control-hover"], t["text2"])
        levels = {
            "neutral": neutral,
            "busy": (t["accent-subtle"], t["accent-fg"]),
            "ok": (_rgba(token("success"), 0.16), t["success"]),
            "warn": (_rgba(token("warning"), 0.16), t["warning"]),
            "danger": (_rgba(token("danger"), 0.16), t["danger"]),
        }
        styleSheet = f'QLabel[slicerRole="chip"] {{ border: none; border-radius: 10px; padding: 0px 8px; min-height: 20px; max-height: 20px; font-size: 12px; background-color: {neutral[0]}; color: {neutral[1]}; }}'
        for level, (background, foreground) in levels.items():
            styleSheet += f'QLabel[slicerRole="chip"][chipLevel="{level}"] {{ background-color: {background}; color: {foreground}; }}'
        return styleSheet
    if role == "kbd":
        return (
            f'QLabel[slicerRole="kbd"] {{ background-color: transparent; color: {t["text2"]}; border: 1px solid {t["border"]}; border-radius: 4px; padding: 0px 5px; font-size: 11px; }}')
    if role == "topBar":
        return (
            f'QToolBar[slicerRole="topBar"] {{ background-color: {t["view-bar"]}; border: none; border-bottom: 1px solid {t["hairline"]}; padding: 0px; spacing: 0px; }}')
    return ""


# Roles that get a fallback stylesheet until the application style supports roles (SPEC 4.7)
_FALLBACK_ROLES = ("card", "primary", "secondary", "ghost", "segmented", "chip", "kbd", "topBar")


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


def _font(widget, pixelSize, weight=None, letterSpacing=None):
    font = qt.QFont(widget.font)
    font.setPixelSize(pixelSize)
    if weight is not None:
        font.setWeight(weight)
    if letterSpacing is not None:
        font.setLetterSpacing(qt.QFont.AbsoluteSpacing, letterSpacing)
    widget.setFont(font)


def card(title=None):
    """Return ``(frame, layout)``: a rounded card with an optional 11 px uppercase title."""
    frame = qt.QFrame()
    frame.setObjectName("StudioCard")
    setRole(frame, "card")
    layout = qt.QVBoxLayout(frame)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)
    if title:
        label = qt.QLabel(title.upper())
        label.setObjectName("StudioCardTitle")
        label.setProperty("slicerRole", "sectionHeader")
        _font(label, 11, qt.QFont.DemiBold, 0.6)
        layout.addWidget(label)
    return frame, layout


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
    bindStyleSheet(frame, lambda: (
        f"QFrame[studioNoticeLine=\"true\"] {{ background-color: {token('accent-subtle').name()}; border: none; border-bottom: 1px solid {token('hairline').name()}; }}"
        f"QLabel#StudioNoticeText {{ color: {token('text').name()}; background: transparent; }}"))
    return frame


def noticeActions(notNow=None, details=None):
    """The usual actions of an offer notice: "Not now" and "Details" (an action is left out when its callback is None)."""
    actions = []
    if notNow is not None:
        actions.append((_("Not now"), notNow))
    if details is not None:
        actions.append((_("Details"), details))
    return actions
