"""Visual helpers of Slicer Studio: style tokens, tinted Lucide icons and widget roles.

These helpers and the shell API (:mod:`StudioLib.shell`) are the only Studio internals that
other modules may use:

- ``token(name)``, ``isDark()``: colors of the graphite themes (dark and light);
- ``icon(name, size=18, role="normal")``: a Lucide icon tinted for every widget state;
- ``setRole(widget, role)``: the ``slicerRole`` widget property;
- builders ``card()``, ``chip()``, ``kbd()``, ``primaryButton()``, ``segmented()``.

Colors come from ``qSlicerStyle.token()`` when the application style provides it. A fallback
table with the same values is used otherwise (for example with a Qt built-in style).

Roles are painted by the application style when it reports ``supportsSlicerRole``. Until then
a small stylesheet that only matches the widget's role is set on the Studio widget itself:
there is no application-wide stylesheet.
"""

import os

import qt
import slicer

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
    "text2": "#a1a1aa",
    "text3": "#71717a",
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
    "text2": "#5c5c66",
    "text3": "#8e8e98",
    "accent": "#2f6fde",
    # accent-hover and accent-pressed are derived from accent (lightness +/- 8%)
    "accent-fg": "#2f6fde",
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
    ``text2``, ``text3``, ``accent``, ``accent-fg``, ``accent-subtle``, ``success``, ``warning``,
    ``danger``, ``view-bar``...). Palette roles are also available as ``window``, ``text``,
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
    "normal": ("text2", "text", "accent-fg", "text3"),
    "strong": ("text", "text", "accent-fg", "text3"),
    "accent": ("accent-fg", "accent-fg", "accent-fg", "text3"),
    "onAccent": ("#ffffff", "#ffffff", "#ffffff", "text3"),
    "danger": ("danger", "danger", "danger", "text3"),
    "warning": ("warning", "warning", "warning", "text3"),
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
    disabled = text3. ``role`` selects another tint set: "strong" (text), "accent", "onAccent"
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
        "window", "text", "button", "card", "hairline", "border", "text2", "text3", "control-hover", "control-pressed",
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
            f'*[slicerRole="primary"]:disabled {{ background-color: {t["border"]}; color: {t["text3"]}; }}')
    if role == "secondary":
        return (
            f'*[slicerRole="secondary"] {{ background-color: {t["button"]}; color: {t["text"]}; border: 1px solid {t["border"]}; border-radius: 6px; padding: 0px 12px; min-height: 28px; }}'
            f'*[slicerRole="secondary"]:hover {{ background-color: {t["control-hover"]}; }}'
            f'*[slicerRole="secondary"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="secondary"]:disabled {{ color: {t["text3"]}; }}')
    if role == "ghost":
        return (
            f'*[slicerRole="ghost"] {{ background-color: transparent; color: {t["text2"]}; border: 1px solid transparent; border-radius: 6px; padding: 3px; }}'
            f'*[slicerRole="ghost"]:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:pressed {{ background-color: {t["control-pressed"]}; }}'
            f'*[slicerRole="ghost"]:checked {{ background-color: {t["accent-subtle"]}; border-color: {t["accent-fg"]}; color: {t["text"]}; }}'
            f'*[slicerRole="ghost"]:disabled {{ color: {t["text3"]}; }}'
            f'QToolButton[slicerRole="ghost"]::menu-indicator {{ image: none; width: 0px; }}')
    if role == "segmented":
        return (
            f'QFrame[slicerRole="segmented"] {{ background-color: {t["control-pressed"]}; border: 1px solid {t["hairline"]}; border-radius: 8px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton {{ background-color: transparent; color: {t["text2"]}; border: none; border-radius: 6px; padding: 3px 12px; font-size: 13px; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:hover {{ background-color: {t["control-hover"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:checked {{ background-color: {t["accent-subtle"]}; color: {t["text"]}; }}'
            f'QFrame[slicerRole="segmented"] QToolButton:disabled {{ color: {t["text3"]}; }}')
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
