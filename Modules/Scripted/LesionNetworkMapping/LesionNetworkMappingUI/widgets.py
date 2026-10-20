"""Shared widgets of the Map cards: card, chip, banner, progress row, segmented control, table, disclaimer, help link.

Plain Qt and CTK widgets with ``slicerRole`` properties, painted by the application style
(``qSlicerStyle``, SPEC 4.7). Chip colours come from ``StudioLib.ui`` when it can be imported, else
from the application palette. There is no application-wide stylesheet (SPEC 0.1.5): a stylesheet is
only set on a chip or banner when the style does not paint roles.
"""

import html

import ctk
import qt
import slicer

CHIP_LEVELS = ("neutral", "busy", "ok", "warn", "danger")

#: Height of the progress bar of a progress row (SPEC 7.9: 4 px)
PROGRESS_BAR_HEIGHT = 4

# Colours of the chip levels when neither the style nor StudioLib.ui provide them: (dark, light)
_LEVEL_COLORS = {
    "ok": ("#3fb950", "#1a7f37"),
    "warn": ("#e3b341", "#9a6700"),
    "danger": ("#f85149", "#cf222e"),
}


def _studioUi():
    """``StudioLib.ui``, or None when the Studio module is not available."""
    try:
        from StudioLib import ui
    except Exception:
        return None
    return ui


def _styleSupportsRoles():
    app = getattr(slicer, "app", None)
    return bool(app is not None and app.style().property("supportsSlicerRole"))


def _repolish(widget):
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def _isDark():
    return slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.Window).lightness() < 128


def _paletteChipColors(level):
    """(background, foreground) colour names of a chip level from the application palette."""
    palette = slicer.app.palette()
    dark = _isDark()
    if level == "busy":
        highlight = palette.color(qt.QPalette.Active, qt.QPalette.Highlight)
        background = qt.QColor(highlight)
        background.setAlpha(46)
        return f"rgba({background.red()},{background.green()},{background.blue()},{background.alpha()})", highlight.name()
    if level in _LEVEL_COLORS:
        color = qt.QColor(_LEVEL_COLORS[level][0 if dark else 1])
        return f"rgba({color.red()},{color.green()},{color.blue()},41)", color.name()
    return palette.color(qt.QPalette.Active, qt.QPalette.Button).name(), palette.color(qt.QPalette.Active, qt.QPalette.WindowText).name()


def _font(widget, pixelSize, bold=False):
    ui = _studioUi()
    font = qt.QFont(widget.font)
    size = pixelSize
    if ui is not None and hasattr(ui, "fontPx"):
        try:
            size = ui.fontPx(pixelSize)
        except Exception:
            size = pixelSize
    font.setPixelSize(int(size))
    if bold:
        font.setWeight(qt.QFont.DemiBold)
    widget.setFont(font)


#
# Card
#


def card(title, objectName=None, collapsed=False):
    """Return a collapsible inspector card: a ``ctk.ctkCollapsibleButton`` with ``slicerRole`` "card".

    Add a layout to it (``qt.QFormLayout(card)``). ``objectName`` should be stable (SPEC 9.2), for example "LNMQACard".
    """
    button = ctk.ctkCollapsibleButton()
    button.text = title
    if objectName:
        button.setObjectName(objectName)
    button.setProperty("slicerRole", "card")
    button.collapsed = bool(collapsed)
    return button


#
# Chip
#


def setChipLevel(chipLabel, level, text=None):
    """Change the level (and optionally the text) of a chip made by ``chip()``."""
    if level not in CHIP_LEVELS:
        raise ValueError(f"Unknown chip level: {level}")
    if text is not None:
        chipLabel.setText(text)
    ui = _studioUi()
    if ui is not None:
        ui.setChipLevel(chipLabel, level)
        return
    chipLabel.setProperty("chipLevel", level)
    if _styleSupportsRoles():
        chipLabel.setStyleSheet("")
    else:
        background, foreground = _paletteChipColors(level)
        chipLabel.setStyleSheet(f"QLabel {{ border: none; border-radius: 10px; padding: 0px 8px; background-color: {background}; color: {foreground}; }}")
    _repolish(chipLabel)


def chip(text, level="neutral"):
    """Return a 20 px pill label; ``level`` is one of ``CHIP_LEVELS`` (neutral, busy, ok, warn, danger)."""
    if level not in CHIP_LEVELS:
        raise ValueError(f"Unknown chip level: {level}")
    label = qt.QLabel(text)
    label.setObjectName("LNMChip")
    label.setAlignment(qt.Qt.AlignCenter)
    label.setFixedHeight(20)
    label.setProperty("chipLevel", level)
    ui = _studioUi()
    if ui is not None:
        ui.setRole(label, "chip")
    else:
        label.setProperty("slicerRole", "chip")
    setChipLevel(label, level)
    return label


#
# Banner
#


def banner(text, actions=(), level="warn"):
    """Return a notice frame: a word-wrapped text and one button per ``(label, callback)`` of ``actions``.

    The frame is named ``LNMBanner``, its text ``LNMBannerText`` (change it with ``setBannerText()``) and its
    buttons ``LNMBannerButton``. ``level`` (neutral, busy, ok, warn, danger) colours its left edge.
    """
    frame = qt.QFrame()
    frame.setObjectName("LNMBanner")
    frame.setProperty("bannerLevel", level)
    frame.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(10, 6, 6, 6)
    layout.setSpacing(6)
    label = qt.QLabel(text)
    label.setObjectName("LNMBannerText")
    label.setWordWrap(True)
    label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    layout.addWidget(label, 1)
    for buttonText, callback in actions:
        button = qt.QToolButton()
        button.setObjectName("LNMBannerButton")
        button.setText(buttonText)
        button.setAutoRaise(True)
        button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        button.setProperty("slicerRole", "ghost")
        button.connect("clicked()", callback)
        layout.addWidget(button, 0, qt.Qt.AlignTop)
    ui = _studioUi()
    if ui is not None:
        ui.bindStyleSheet(frame, lambda: _bannerStyleSheet(level))  # follows theme changes
    else:
        frame.setStyleSheet(_bannerStyleSheet(level))
    return frame


def _bannerStyleSheet(level):
    """Stylesheet of a banner frame only (its border; the left edge has the level's colour)."""
    ui = _studioUi()
    if ui is not None and level in ("ok", "warn", "danger", "busy"):
        edge = ui.token({"ok": "success", "warn": "warning", "danger": "danger", "busy": "accent"}[level]).name()
        hairline = ui.token("border").name()
    else:
        edge = _paletteChipColors(level)[1]
        hairline = slicer.app.palette().color(qt.QPalette.Active, qt.QPalette.Mid).name()
    return (
        f"QFrame#LNMBanner {{ border: 1px solid {hairline}; border-left: 3px solid {edge}; border-radius: 6px; }}"
        "QFrame#LNMBanner QLabel { border: none; background: transparent; }")


def setBannerText(bannerFrame, text):
    """Change the text of a banner made by ``banner()``."""
    label = bannerFrame.findChild(qt.QLabel, "LNMBannerText")
    if label is not None:
        label.setText(text)


#
# Progress row
#


class ProgressRow:
    """A 4 px progress bar, a status line ("312 / 1000 subjects · ~1 min left") and a Stop button.

    ``widget`` is the row. ``setJob(job)`` makes Stop call ``job.stop()`` and ``update()`` reads the job's
    progress; ``setProgress()`` sets values directly. Stop is a button: it is never bound to Esc.
    """

    def __init__(self, stopText="Stop", objectNamePrefix="LNM"):
        self.job = None
        self.widget = qt.QWidget()
        self.widget.setObjectName(f"{objectNamePrefix}ProgressRow")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.bar = qt.QProgressBar()
        self.bar.setObjectName(f"{objectNamePrefix}ProgressBar")
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(PROGRESS_BAR_HEIGHT)
        self.bar.setRange(0, 1000)
        layout.addWidget(self.bar)
        row = qt.QHBoxLayout()
        row.setSpacing(6)
        self.label = qt.QLabel("")
        self.label.setObjectName(f"{objectNamePrefix}ProgressText")
        self.label.setProperty("slicerRole", "secondaryText")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.stopButton = qt.QPushButton(stopText)
        self.stopButton.setObjectName(f"{objectNamePrefix}StopButton")
        self.stopButton.setProperty("slicerRole", "secondary")
        self.stopButton.setToolTip("Stop the job (it keeps the results that are complete)")
        self.stopButton.setEnabled(False)
        self.stopButton.connect("clicked()", self._onStop)
        row.addWidget(self.stopButton)
        layout.addLayout(row)
        self.reset()

    def _onStop(self):
        if self.job is not None:
            self.job.stop()
            self.stopButton.setEnabled(False)
            self.label.setText("Stopping…")

    def setJob(self, job):
        """Follow ``job`` (a ``jobrunner.Job``) and let Stop stop it; None detaches the row."""
        self.job = job
        self.update()

    def update(self):
        """Show the state of the followed job (call it from the job's onEvent and onDone)."""
        job = self.job
        if job is None:
            self.stopButton.setEnabled(False)
            return
        if job.isQueued():
            self.setProgress(None, text=f"Waiting for the running job to finish ({job.title})")
            self.stopButton.setEnabled(True)
            return
        if job.isRunning():
            text = job.statusText()
            self.setProgress(job.progress, text="Stopping…" if job.stopRequested else text)
            self.stopButton.setEnabled(not job.stopRequested)
            return
        self.stopButton.setEnabled(False)
        if job.state == "done":
            self.setProgress(1.0, text="Done")
        elif job.state == "cancelled":
            self.setProgress(job.progress, text="Stopped")
        else:
            self.setProgress(job.progress, text=f"Failed: {job.failureReason or 'unknown error'}")

    def setProgress(self, fraction, text=None):
        """``fraction`` in 0..1, or None for a busy bar; ``text`` replaces the status line when given."""
        if fraction is None:
            self.bar.setRange(0, 0)
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(round(min(max(float(fraction), 0.0), 1.0) * 1000))
        if text is not None:
            self.label.setText(text)

    def reset(self, text=""):
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.label.setText(text)
        self.stopButton.setEnabled(self.job is not None and (self.job.isRunning() or self.job.isQueued()))


def progressRow(stopText="Stop", objectNamePrefix="LNM"):
    """Return a ``ProgressRow`` (``.widget``, ``.bar``, ``.label``, ``.stopButton``)."""
    return ProgressRow(stopText, objectNamePrefix)


#
# Segmented control and table
#


def segmented(labels):
    """Return ``(frame, buttonGroup)``: exclusive checkable buttons with ids 0, 1, 2… in the order of ``labels``.

    The frame has ``slicerRole`` "segmented"; the first button is checked.
    """
    frame = qt.QFrame()
    frame.setObjectName("LNMSegmented")
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
        button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        group.addButton(button, index)
        layout.addWidget(button)
    if labels:
        group.button(0).setChecked(True)
    ui = _studioUi()
    if ui is not None:
        ui.setRole(frame, "segmented")
    else:
        frame.setProperty("slicerRole", "segmented")
    return frame, group


def tableView(columns):
    """Return a read-only ``qt.QTableWidget`` with the header ``columns``, row selection and no vertical header."""
    table = qt.QTableWidget(0, len(columns))
    table.setObjectName("LNMTable")
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(qt.QHeaderView.Stretch)  # fits the inspector: no horizontal scroll
    table.setWordWrap(False)
    return table


#
# Text
#


def disclaimerLabel():
    """Return the research-only disclaimer label (``export.DISCLAIMER``, 11 px, always visible; SPEC 7.9)."""
    from LesionNetworkMappingLib import export

    label = qt.QLabel(export.DISCLAIMER)
    label.setObjectName("LNMDisclaimer")
    label.setWordWrap(True)
    label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    label.setProperty("slicerRole", "caption")
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    _font(label, 11)
    # A word-wrapped label is not painted through the style: set the caption colour (text3) on the label itself
    ui = _studioUi()
    if ui is not None:
        ui.bindStyleSheet(label, lambda: f"QLabel#LNMDisclaimer {{ color: {ui.token('text3').name()}; }}")
    else:
        palette = qt.QPalette(label.palette)
        palette.setColor(qt.QPalette.WindowText, slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.WindowText))
        label.setPalette(palette)
    return label


def helpLink(text, url):
    """Return a label with one link that opens ``url`` in the system browser."""
    label = qt.QLabel(f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>')
    label.setObjectName("LNMHelpLink")
    label.setTextFormat(qt.Qt.RichText)
    label.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
    label.setOpenExternalLinks(True)
    label.setToolTip(url)
    return label
