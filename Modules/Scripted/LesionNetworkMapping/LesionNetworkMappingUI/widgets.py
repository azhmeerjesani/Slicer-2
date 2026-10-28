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
from slicer.i18n import tr as _

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


def wrapCheckBox(checkBox):
    """Return the widget to add to a layout in place of ``checkBox``: a row with the check box and its text as a word-wrapped
    label (Studio), so that a long text does not make the inspector wider. Without Studio it is ``checkBox`` itself.
    Show or hide the returned row, not the check box.
    """
    ui = _studioUi()
    return ui.wrapCheckBox(checkBox) if ui is not None else checkBox


def showInFileManager(path):
    """Open a folder or file with the desktop; where there is no file manager (the web build) show its location with [Copy path].

    ``StudioLib.projects`` does the work when the Studio module is available, else the desktop is asked directly.
    """
    try:
        from StudioLib import projects
    except Exception:
        projects = None
    if projects is not None:
        return projects.showInFileManager(path)
    return bool(qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path)))


def openLink(url):
    """Open a web address in the browser; where there is none (the web build) show the link with [Copy link]."""
    try:
        from StudioLib import projects
    except Exception:
        projects = None
    if projects is not None:
        return projects.openLink(url)
    return bool(qt.QDesktopServices.openUrl(qt.QUrl(url)))


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

    The application style (``qSlicerStyle``) draws it as a rounded card (``card`` fill, 10 px radius, 14 px padding) with
    a 44 px title row: the title in 14 px SemiBold and a chevron on the right (VISUAL 4.5). ``setStep()`` turns the title
    row into a step row (status circle, title and a one-line summary).

    Add a layout to it (``qt.QFormLayout(card)``). ``objectName`` should be stable (SPEC 9.2), for example "LNMQACard".
    """
    button = ctk.ctkCollapsibleButton()
    button.text = title
    if objectName:
        button.setObjectName(objectName)
    button.setProperty("slicerRole", "card")
    # Preferred, not the Minimum policy of the button: with Minimum a layout never makes the card narrower than its size hint
    # (the widest a row of the card can be, for example a form with its labels next to the fields), which would make the
    # inspector wider than its stored width. Preferred shrinks the card to its minimum size hint (rows wrap).
    button.setSizePolicy(qt.QSizePolicy.Preferred, button.sizePolicy.verticalPolicy())
    # The button measures its title row when its style changes: measure it again for the card role
    qt.QApplication.sendEvent(button, qt.QEvent(qt.QEvent.StyleChange))
    button.collapsed = bool(collapsed)
    return button


#: States of a step row (``setStep()``): done (a check), action (needs the user: "!"), current (its number in the
#: accent color), or "" (a later step)
STEP_STATES = ("done", "action", "current", "")


def setStep(cardButton, number, state="", summary=""):
    """Show the title row of ``cardButton`` (a ``card()``) as a step: a 20 px status circle with ``number``, the title and a
    one-line ``summary`` in the caption color. ``number=0`` shows a plain title row again.
    """
    if state not in STEP_STATES:
        raise ValueError(f"Unknown step state: {state}")
    values = {"studioStep": int(number), "studioStepState": state, "studioStepSummary": str(summary or "")}
    changed = False
    for name, value in values.items():
        if cardButton.property(name) != value:
            cardButton.setProperty(name, value)
            changed = True
    if changed:
        cardButton.update()


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
# Notice line (Studio): one sentence and "Details"
#


def noticeLine(objectName="LNMNoticeLine"):
    """Return a one-line notice (VISUAL 4.8, Map Result card): a 14 px warning icon, one sentence elided to the width and
    a "Details" link that shows the whole explanation in a popover. Set its contents with ``setNoticeLine()``.

    Its children: ``LNMNoticeLineText`` (the sentence) and ``LNMNoticeLineDetails`` (the link).
    """
    frame = qt.QFrame()
    frame.setObjectName(objectName)
    frame.setProperty("lnmNoticeLine", True)
    frame.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(10, 6, 8, 6)
    layout.setSpacing(8)
    icon = qt.QLabel()
    icon.setObjectName("LNMNoticeLineIcon")
    icon.setFixedSize(14, 16)
    layout.addWidget(icon)
    label = qt.QLabel()
    label.setObjectName("LNMNoticeLineText")
    label.setTextFormat(qt.Qt.PlainText)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
    _font(label, 12)
    layout.addWidget(label, 1)
    details = qt.QPushButton(_("Details"))
    details.setObjectName("LNMNoticeLineDetails")
    details.setFlat(True)
    details.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    details.setSizePolicy(qt.QSizePolicy.Maximum, qt.QSizePolicy.Fixed)
    _font(details, 12)
    details.connect("clicked()", lambda frame=frame, details=details: showPopover(details, str(frame.property("lnmNoticeDetails") or ""), "LNMNoticeLinePopover"))
    layout.addWidget(details)
    ui = _studioUi()
    if ui is not None:
        if hasattr(ui, "elideLabel"):
            ui.elideLabel(label)

        def styleSheet(frame=frame, icon=icon):
            color = ui.token("warning")
            icon.setPixmap(ui.pixmap("triangle-alert", 14, "warning"))
            return (f'QFrame[lnmNoticeLine="true"] {{ background-color: {ui._rgba(color, 0.08)}; border: 1px solid {ui._rgba(color, 0.22)};'
                    " border-radius: 8px; }"
                    f'QFrame[lnmNoticeLine="true"] QLabel {{ background: transparent; border: none; color: {ui.token("text").name()}; }}'
                    f'QPushButton#LNMNoticeLineDetails {{ background: transparent; border: none; padding: 0px; color: {ui.token("accent-fg").name()}; }}'
                    "QPushButton#LNMNoticeLineDetails:hover { text-decoration: underline; }")

        ui.bindStyleSheet(frame, styleSheet)
    else:
        frame.setStyleSheet(_bannerStyleSheet("warn").replace("LNMBanner", objectName))
    frame.hide()
    return frame


def setNoticeLine(frame, sentence, details=""):
    """Show ``sentence`` (one line) in a ``noticeLine()``; ``details`` (the whole explanation) is behind "Details", which is
    hidden when there are none. An empty sentence hides the notice.
    """
    label = frame.findChild(qt.QLabel, "LNMNoticeLineText")
    ui = _studioUi()
    if ui is not None and hasattr(ui, "setElidedText") and label.property("studioElided"):
        ui.setElidedText(label, sentence or "")
    else:
        label.setText(sentence or "")
    frame.setProperty("lnmNoticeDetails", details or "")
    frame.findChild(qt.QPushButton, "LNMNoticeLineDetails").setVisible(bool(details) and details != sentence)
    frame.setToolTip(details or sentence or "")
    frame.setVisible(bool(sentence))


def showPopover(anchor, text, objectName="LNMPopover"):
    """Show ``text`` (plain, word-wrapped, selectable) in a popover under ``anchor``."""
    menu = qt.QMenu(anchor)
    menu.setObjectName(objectName)
    label = qt.QLabel(text)
    label.setWordWrap(True)
    label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    label.setFixedWidth(320)
    label.setContentsMargins(12, 10, 12, 10)
    action = qt.QWidgetAction(menu)
    action.setDefaultWidget(label)
    menu.addAction(action)
    run = getattr(menu, "exec", None) or menu.exec_
    run(anchor.mapToGlobal(qt.QPoint(0, anchor.height)))
    menu.deleteLater()


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


#: Height of the rows of a ``tableView()`` (VISUAL 4.5: 28 px)
TABLE_ROW_HEIGHT = 28


def _tableStyleSheet():
    """Rows separated by hairlines on the card, a caption-colored header (VISUAL 4.5)."""
    ui = _studioUi()
    if ui is None:
        return ""
    hairline = ui.token("hairline").name()
    table = 'QTableWidget[lnmTable="true"]'
    return (
        f"{table} {{ background: transparent; border: none; gridline-color: {hairline}; }}"
        f"{table}::item {{ border: none; border-bottom: 1px solid {hairline}; padding: 0px 6px; }}"
        f"{table}::item:selected {{ background-color: {ui.token('selected').name()}; color: {ui.token('text').name()}; }}"
        f"{table} QHeaderView::section {{ background: transparent; color: {ui.token('text3').name()}; border: none;"
        f" border-bottom: 1px solid {hairline}; padding: 2px 6px; font-size: {ui.fontPx(11)}px; }}")


def tableView(columns):
    """Return a read-only ``qt.QTableWidget`` with the header ``columns``, row selection and no vertical header."""
    table = qt.QTableWidget(0, len(columns))
    table.setObjectName("LNMTable")
    table.setHorizontalHeaderLabels(list(columns))
    table.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
    table.setSelectionMode(qt.QAbstractItemView.SingleSelection)
    table.setAlternatingRowColors(False)
    table.setShowGrid(False)
    table.setFrameShape(qt.QFrame.NoFrame)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
    table.horizontalHeader().setSectionResizeMode(qt.QHeaderView.Stretch)  # fits the inspector: no horizontal scroll
    table.horizontalHeader().setHighlightSections(False)
    table.setWordWrap(False)
    # The rules match the property, not the object name (callers rename their tables)
    table.setProperty("lnmTable", True)
    ui = _studioUi()
    if ui is not None:
        ui.bindStyleSheet(table, _tableStyleSheet)
    return table


#: Text colors of positive (hot) and negative (cold) values in tables: (dark, light)
VALUE_COLORS = {"hot": ("#ffb02e", "#a35200"), "cold": ("#7ab8ff", "#1f5fbf")}


def valueColor(value):
    """``qt.QColor`` of a signed value in a table: hot for positive, cold for negative values (VISUAL 4.5)."""
    kind = "hot" if value >= 0 else "cold"
    return qt.QColor(VALUE_COLORS[kind][0 if _isDark() else 1])


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
    return _bindDisclaimerColor(label)


def _bindDisclaimerColor(label):
    # A word-wrapped label is not painted through the style: set the caption colour (text3) on the label itself
    ui = _studioUi()
    if ui is not None:
        ui.bindStyleSheet(label, lambda: f"QLabel#{label.objectName} {{ color: {ui.token('text3').name()}; }}")
    else:
        palette = qt.QPalette(label.palette)
        palette.setColor(qt.QPalette.WindowText, slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.WindowText))
        label.setPalette(palette)
    return label


#: The one line of the disclaimer in the Studio inspector (VISUAL 4.5); "Read more" shows ``export.DISCLAIMER``
DISCLAIMER_LINE = "Research use only. Lesion network mapping specificity is debated."


def disclaimerLine(parent=None):
    """Return the one-line disclaimer of the Studio inspector (``LNMDisclaimerLine``): an info icon, "Research use only.
    Lesion network mapping specificity is debated." and "Read more", which shows the whole ``export.DISCLAIMER`` (with
    its references) in a popover. The full text is also the tooltip.
    """
    from LesionNetworkMappingLib import export

    frame = qt.QWidget(parent)
    frame.setObjectName("LNMDisclaimerLine")
    layout = qt.QHBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    icon = qt.QLabel()
    icon.setObjectName("LNMDisclaimerIcon")
    icon.setFixedSize(14, 16)
    icon.setAlignment(qt.Qt.AlignHCenter | qt.Qt.AlignTop)
    ui = _studioUi()
    if ui is not None:
        icon.setPixmap(ui.pixmap("info", 13, "text3"))
    layout.addWidget(icon, 0, qt.Qt.AlignTop)
    link = ui.token("accent-fg").name() if ui is not None else slicer.app.palette().color(qt.QPalette.Link).name()
    label = qt.QLabel(f'{html.escape(DISCLAIMER_LINE)} <a href="#more" style="color: {link}; text-decoration: none;">Read more</a>')
    # The objectName of the disclaimer label that the Map tour and the tests point at
    label.setObjectName("LNMDisclaimer")
    label.setTextFormat(qt.Qt.RichText)
    label.setWordWrap(True)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    label.setToolTip(export.DISCLAIMER)
    label.setTextInteractionFlags(qt.Qt.LinksAccessibleByMouse | qt.Qt.LinksAccessibleByKeyboard)
    _font(label, 11)
    _bindDisclaimerColor(label)

    def readMore(_link=None, label=label):
        showDisclaimer(label)

    label.connect("linkActivated(QString)", readMore)
    layout.addWidget(label, 1)
    return frame


def showDisclaimer(anchor=None):
    """The whole disclaimer, with its references, in a popover under ``anchor`` (a message box without an anchor)."""
    from LesionNetworkMappingLib import export

    if anchor is None:
        slicer.util.infoDisplay(export.DISCLAIMER, windowTitle="Research use only")
        return
    menu = qt.QMenu(anchor)
    menu.setObjectName("LNMDisclaimerPopover")
    text = qt.QLabel(export.DISCLAIMER)
    text.setWordWrap(True)
    text.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    text.setFixedWidth(320)
    text.setContentsMargins(12, 10, 12, 10)
    action = qt.QWidgetAction(menu)
    action.setDefaultWidget(text)
    menu.addAction(action)
    run = getattr(menu, "exec", None) or menu.exec_
    run(anchor.mapToGlobal(qt.QPoint(0, anchor.height)))
    menu.deleteLater()


def helpLink(text, url):
    """Return a label with one link that opens ``url`` in the system browser."""
    label = qt.QLabel(f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>')
    label.setObjectName("LNMHelpLink")
    label.setTextFormat(qt.Qt.RichText)
    label.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
    label.setOpenExternalLinks(True)
    label.setToolTip(url)
    return label
