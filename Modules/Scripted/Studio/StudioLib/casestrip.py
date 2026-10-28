"""The case strip of Draw (L-45; SPEC_LNM_ADDENDUM 7.3; USABILITY 2, 3, 6).

While a study subject is open in Studio's Draw, a strip of 28 px lines heads the inspector cards (``draw.addInspectorCard``,
order 0) and answers "which case, of which paper, where in my worklist":

- **Queue** "Untraced · 3/12 ▾": the trace queue that Done → next and Alt+←/→ follow (``StudySession.setQueue``: All
  subjects, Untraced, Needs review, Changes requested, Flagged, This paper, Second tracing and the Library extension queues),
  and the position of the case in it.
- **Case** id: a click opens the subject switcher (the breadcrumb's popover).
- **Paper and case fields**: the DOI short form (a click opens it in the browser; right-click to edit), "Fig 2B" with the
  figure crop's thumbnail (prefetched with the case, ``StudySession.figureThumbnail``), the modality, the side and location
  reported by the paper, the symptom, and the reviewer's comment (read only: it belongs to the audited review, L-43).
- **Inline edit**: a click on a field opens a one-line editor under the strip (Enter saves, Esc cancels, nothing modal). The
  value is checked against the study schema (``schema.normalize_value``; a DOI must look like 10.xxxx/…): an invalid value is
  refused with the reason, an unusual one (an unknown modality) is kept on a second Enter. It is saved through the
  ``StudySession`` (participants.tsv). A paper field changed on one case is offered for the paper's other cases. Leaving the
  case with the editor open (Done → next, Alt+←/→, the subject switcher) saves it as Enter would
  (``commitPendingEdit``, called by ``StudySession``); an invalid value keeps the case open with the reason.
- **Blind tracing** (L-43, ``review.blinded_columns``): the fields of blinded columns (the symptom) are not shown.

Each field has a fixed line (``LINES``: the queue, the case and the DOI on the first, the figure, modality, side, symptom
and review comment on the second), so a field is found in the same place from case to case; the second line is left out
when none of its fields is shown. Fields that do not fit their line go, in order, into a "+N" menu at the end of the last
line. While the strip is shown, Draw's header keeps the height it needs (``_HeaderPin``): a short inspector scrolls instead
of squeezing the cards. Empty paper fields show as "+ DOI"... only in a literature study (a row names a record, DOI, PMID
or citation); in such a study the Lesion card ⋯ menu also gets "New case from this paper" and "New case from clipboard
figure" (``StudySession.newCaseFromPaper``).

Installed by ``study_ui.install`` (Studio only: never in Classic or under ``--testing``). objectNames: StudioCaseStrip,
StudioCaseStripLines, StudioCaseStripQueue, StudioCaseStripField (property ``caseField``), StudioCaseStripMore,
StudioCaseStripEditor, StudioCaseStripEditorInput, StudioCaseStripEditorSave, StudioCaseStripEditorCancel,
StudioCaseStripEditorMessage.
"""

import contextlib
import logging
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import ui

CARD_KEY = "casestrip"
CARD_ORDER = 0
LINE_HEIGHT = 28
FIELD_HEIGHT = 24
SPACING = 4
#: Longest field text (px) before it is elided
FIELD_MAX_WIDTH = 150
THUMBNAIL_PX = 20
#: Fields: (key, columns, title), in display order
FIELDS = (
    ("case", ("participant_id",), _("Case")),
    ("doi", ("doi",), _("DOI")),
    ("figure", ("figure", "panel"), _("Figure")),
    ("modality", ("image_modality",), _("Modality")),
    ("side", ("reported_side", "reported_location"), _("Reported side and location")),
    ("symptom", ("symptom",), _("Symptom")),
    ("review", ("review_comment",), _("Review comment")),
)
#: The fixed line of each field ("queue" is the queue chip)
LINES = (("queue", "case", "doi"), ("figure", "modality", "side", "symptom", "review"))
#: Paper fields offered for the paper's other cases after an edit
PAPER_FIELDS = ("doi", "pmid", "citation", "image_modality")
#: Lesion card ⋯ menu items
NEW_CASE_TEXT = _("New case from this paper")
NEW_CASE_FIGURE_TEXT = _("New case from clipboard figure")
SIDE_WORDS = {"L": _("Left"), "R": _("Right"), "bilateral": _("Bilateral"), "midline": _("Midline")}


def _studyUi():
    from . import study_ui

    return study_ui


def _session():
    studyUi = _studyUi()
    return studyUi.session() if studyUi.isAvailable() else None


def _shell():
    from . import shell

    return shell.instance()


def _openLink(url):
    from . import projects

    projects.openLink(url)


def _draw():
    try:
        from . import draw
    except ImportError:
        return None
    return draw


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _blinded(studyObject):
    """Columns hidden from tracers (L-43), or an empty set."""
    try:
        from LesionNetworkMappingLib import review as rules
    except ImportError:
        return set()
    try:
        return set(rules.blinded_columns(studyObject))
    except Exception:
        logging.error(f"Studio: the blinded columns could not be read:\n{traceback.format_exc()}")
        return set()


def isLiteratureStudy(studyObject):
    """True when a row names a paper (record, DOI, PMID or citation) or is a case report: the paper fields are offered."""
    for row in studyObject.subjects:
        if row.get("source_type") == "case_report" or any(not _missing(row.get(column)) for column in ("record_id", "doi", "pmid", "citation")):
            return True
    return False


def figureText(row):
    """"Fig 2B" from the ``figure`` and ``panel`` columns ("Fig 2B; 3A" for several panels), or ""."""
    figures = [part.strip() for part in str(row.get("figure") or "").split(";")] if not _missing(row.get("figure")) else []
    panels = [part.strip() for part in str(row.get("panel") or "").split(";")] if not _missing(row.get("panel")) else []
    if not figures and not panels:
        return ""
    count = max(len(figures), len(panels))
    labels = []
    for index in range(count):
        figure = figures[index] if index < len(figures) else (figures[-1] if figures else "")
        panel = panels[index] if index < len(panels) else ""
        if figure.lower().startswith("fig"):
            figure = figure.split(".", 1)[-1].strip() if "." in figure else figure[3:].strip()
        labels.append(f"{figure}{panel}")
    return _("Fig {label}").format(label="; ".join(dict.fromkeys(labels)))


def fieldText(key, row, studyUi=None):
    """The text of a field (without eliding), or "" when its columns are empty."""
    studyUi = studyUi or _studyUi()
    if key == "case":
        return str(row.get("participant_id", ""))
    if key == "doi":
        return studyUi.normalizeDoi(row.get("doi"))
    if key == "figure":
        return figureText(row)
    if key == "modality":
        return "" if _missing(row.get("image_modality")) else str(row["image_modality"])
    if key == "side":
        side = "" if _missing(row.get("reported_side")) else SIDE_WORDS.get(str(row["reported_side"]), str(row["reported_side"]))
        location = "" if _missing(row.get("reported_location")) else str(row["reported_location"])
        return " · ".join(part for part in (side, location) if part)
    if key == "symptom":
        return "" if _missing(row.get("symptom")) else str(row["symptom"])
    if key == "review":
        return "" if _missing(row.get("review_comment")) else str(row["review_comment"])
    return ""


class _Keys(qt.QObject):
    """Enter saves and Esc cancels the inline editor (also from a combo box); Esc then never reaches the Esc ladder."""

    def __init__(self, strip):
        qt.QObject.__init__(self)
        self.strip = strip

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType not in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress):
            return False
        key = event.key()
        if key not in (qt.Qt.Key_Escape, qt.Qt.Key_Return, qt.Qt.Key_Enter) or ui.hasKeyModifiers(event):
            return False
        if eventType == qt.QEvent.ShortcutOverride:
            event.accept()
            return True
        popup = getattr(obj, "view", None)
        if callable(popup) and obj.view().isVisible():
            return False
        if key != qt.Qt.Key_Escape and obj.inherits("QAbstractButton"):
            # Return on a focused button presses it (Cancel cancels)
            return False
        if key == qt.Qt.Key_Escape:
            self.strip.cancelEdit()
        else:
            self.strip.saveEdit()
        return True


class _Lines(qt.QObject):
    """Lays the strip's fields out again when its width changes."""

    def __init__(self, strip):
        qt.QObject.__init__(self)
        self.strip = strip

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            self.strip.relayout()
        return False


class _HeaderPin(qt.QObject):
    """Keeps Draw's header (Lesion card, inspector cards) at the height it needs at its width while the strip is shown.

    The header's vertical policy is Maximum: in a short inspector the strip's lines would make it squeeze the rows of the
    Lesion card and of the cards below over each other instead of letting the inspector scroll (the review state of L-43
    does the same while it adds its notice). The header's own minimum height is given back when the strip is hidden.
    """

    def __init__(self):
        qt.QObject.__init__(self)
        self.header = None
        self.saved = 0
        self._scheduled = False

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.LayoutRequest and not self._scheduled:
            self._scheduled = True
            qt.QTimer.singleShot(0, self._run)
        return False

    def _run(self):
        self._scheduled = False
        self.update()

    def attach(self, header):
        if header is self.header:
            self.update()
            return
        self.detach()
        if header is None:
            return
        self.header, self.saved = header, header.minimumHeight
        header.installEventFilter(self)
        self.update()

    def update(self):
        header = self.header
        if header is None or not _isAlive(header):
            self.header = None
            return
        layout = header.layout()
        if layout is not None:
            layout.activate()
        # The height at the header's width (word-wrapped labels), not its size hint (computed for another width)
        needed = header.heightForWidth(header.width) if header.hasHeightForWidth() and header.width > 0 else -1
        height = max(self.saved, needed if needed > 0 else header.sizeHint.height())
        if header.minimumHeight != height:
            header.setMinimumHeight(height)

    def detach(self):
        header, self.header = self.header, None
        if header is not None and _isAlive(header):
            header.removeEventFilter(self)
            header.setMinimumHeight(self.saved)


class CaseStrip:
    """The case strip widget (``widget``); ``refresh()`` follows the study session."""

    def __init__(self):
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioCaseStrip")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.lines = qt.QWidget()
        self.lines.setObjectName("StudioCaseStripLines")
        self.lines.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.lines.setFixedHeight(LINE_HEIGHT)
        layout.addWidget(self.lines)
        self._linesFilter = _Lines(self)
        self.lines.installEventFilter(self._linesFilter)

        self.queueButton = self._button("StudioCaseStripQueue")
        self.queueButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.queueMenu = qt.QMenu(self.queueButton)
        self.queueMenu.setObjectName("StudioCaseStripQueueMenu")
        self.queueMenu.connect("aboutToShow()", self._fillQueueMenu)
        self.queueButton.setMenu(self.queueMenu)
        self._queueGroup = qt.QActionGroup(self.queueMenu)  # QMenu.clear() deletes the actions, not a group
        self._queueGroup.setExclusive(True)
        self.fieldButtons = {}
        for key, _columns, title in FIELDS:
            button = self._button("StudioCaseStripField")
            button.setProperty("caseField", key)
            button.setAccessibleName(title)
            button.connect("clicked()", lambda key=key: self._onFieldClicked(key))
            button.setContextMenuPolicy(qt.Qt.CustomContextMenu)
            button.connect("customContextMenuRequested(QPoint)", lambda point, key=key: self._onFieldMenu(key))
            self.fieldButtons[key] = button
        self.moreButton = self._button("StudioCaseStripMore")
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.moreMenu = qt.QMenu(self.moreButton)
        self.moreButton.setMenu(self.moreMenu)
        self._hidden = []  # field keys that did not fit

        self.editor = qt.QFrame()
        self.editor.setObjectName("StudioCaseStripEditor")
        self.editor.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        editorLayout = qt.QVBoxLayout(self.editor)
        editorLayout.setContentsMargins(8, 6, 8, 6)
        editorLayout.setSpacing(4)
        self.editorTitle = qt.QLabel()
        self.editorTitle.setObjectName("StudioCaseStripEditorTitle")
        editorLayout.addWidget(self.editorTitle)
        self.inputsRow = qt.QHBoxLayout()
        self.inputsRow.setSpacing(4)
        editorLayout.addLayout(self.inputsRow)
        self.messageLabel = qt.QLabel()
        self.messageLabel.setObjectName("StudioCaseStripEditorMessage")
        self.messageLabel.setWordWrap(True)
        self.messageLabel.hide()
        editorLayout.addWidget(self.messageLabel)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(4)
        buttons.addStretch(1)
        self.cancelButton = qt.QPushButton(_("Cancel · {key}").format(key=ui.keyText("Esc")))
        self.cancelButton.setObjectName("StudioCaseStripEditorCancel")
        ui.setRole(self.cancelButton, "ghost")
        ui.installButtonKeys(self.cancelButton)
        self.cancelButton.connect("clicked()", self.cancelEdit)
        buttons.addWidget(self.cancelButton)
        self.saveButton = qt.QPushButton(_("Save · {key}").format(key=ui.keyText("Enter")))
        self.saveButton.setObjectName("StudioCaseStripEditorSave")
        ui.setRole(self.saveButton, "secondary")
        ui.installButtonKeys(self.saveButton)
        self.saveButton.connect("clicked()", self.saveEdit)
        buttons.addWidget(self.saveButton)
        editorLayout.addLayout(buttons)
        self.editor.hide()
        layout.addWidget(self.editor)
        self._keys = _Keys(self)
        self._pin = _HeaderPin()
        self._editing = None  # field key
        self._inputs = {}  # column -> input widget
        self._editSid = None
        self._confirmed = None  # values whose warnings the user accepted (a second Enter)
        self._committing = False
        self._row = None
        self._sid = None
        self._fonts()
        ui.bindStyleSheet(self.widget, self._styleSheet)

    def _button(self, objectName):
        button = qt.QToolButton(self.lines)
        button.setObjectName(objectName)
        button.setAutoRaise(True)
        button.setFixedHeight(FIELD_HEIGHT)
        button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        button.setIconSize(qt.QSize(THUMBNAIL_PX, THUMBNAIL_PX - 4))
        button.setFocusPolicy(qt.Qt.TabFocus)
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        ui.setRole(button, "ghost")
        ui.installButtonKeys(button)
        return button

    def _fonts(self):
        for button in [self.queueButton, self.moreButton, *self.fieldButtons.values()]:
            font = qt.QFont(button.font)
            font.setPixelSize(ui.fontPx(12))
            button.setFont(font)
        font = qt.QFont(self.fieldButtons["case"].font)
        font.setWeight(qt.QFont.DemiBold)
        self.fieldButtons["case"].setFont(font)
        font = qt.QFont(self.editorTitle.font)
        font.setPixelSize(ui.fontPx(12))
        font.setWeight(qt.QFont.DemiBold)
        self.editorTitle.setFont(font)

    def _styleSheet(self):
        return (
            f"QFrame#StudioCaseStrip {{ background-color: {ui.token('card').name()}; border: 1px solid {ui.token('hairline').name()}; border-radius: 8px; }}"
            f"QToolButton#StudioCaseStripField[casePlaceholder=\"true\"] {{ color: {ui.token('text3').name()}; }}"
            f"QToolButton#StudioCaseStripField[caseLevel=\"warn\"] {{ color: {ui.token('warning').name()}; }}"
            f"QToolButton#StudioCaseStripQueue {{ color: {ui.token('accent-fg').name()}; }}"
            f"QFrame#StudioCaseStripEditor {{ background-color: {ui.token('accent-subtle').name()}; border: none; border-top: 1px solid {ui.token('hairline').name()}; "
            "border-bottom-left-radius: 8px; border-bottom-right-radius: 8px; }"
            f"QLabel#StudioCaseStripEditorTitle {{ color: {ui.token('text2').name()}; background: transparent; }}")

    # Content

    def refresh(self):
        """Show the current subject of the study (the strip is hidden without one)."""
        if not _isAlive(self.widget):
            return
        session = _session()
        sid = session.current() if session is not None else None
        if session is None or sid is None or sid not in session.study:
            self._row = self._sid = None
            if self._editing is not None:
                self.cancelEdit()
            self.widget.hide()
            self._pin.detach()
            return
        studyUi = _studyUi()
        row = session.study.subject(sid)
        if self._editing is not None and self._editSid != sid and not self._committing:
            # Another case was opened without the study session saving the editor (it normally does when a case is left);
            # saving notifies the study listeners, so this refresh runs again meanwhile
            self._committing = True
            try:
                saved, reason = self.commitEdit(session)
            finally:
                self._committing = False
            if not saved:
                shell = _shell()
                if shell is not None:
                    shell.toast(_("Not saved for {sid}: {reason}").format(sid=self._editSid, reason=reason), ms=6000, level="warning")
                self.cancelEdit()
        self._row, self._sid = row, sid
        blinded = _blinded(session.study)
        literature = isLiteratureStudy(session.study)
        # Queue and position
        try:
            label = session.queueLabel()
            position, count = session.queuePosition()
        except Exception:
            logging.error(f"Studio: the trace queue could not be read:\n{traceback.format_exc()}")
            label, position, count = _("All subjects"), None, len(session.study)
        positionText = f"{position}/{count}" if position is not None else f"—/{count}"
        spec = session.queue()
        shortLabel = spec["label"] if spec is not None else _("All")
        self.queueButton.setText(f"{shortLabel} · {positionText} ▾")
        self.queueButton.setToolTip(ui.tooltip(_("Trace queue: {queue}").format(queue=label), None,
                                               _("case {position} of {count}; Done → next and Alt+←/→ follow it").format(position=position or "—", count=count)))
        self.queueButton.setAccessibleName(_("Trace queue {queue}, case {position} of {count}").format(queue=label, position=position or "—", count=count))
        # Fields
        for key, columns, title in FIELDS:
            button = self.fieldButtons[key]
            text = fieldText(key, row, studyUi)
            hidden = any(column in blinded for column in columns)
            placeholder = not text and literature and key not in ("case", "review")
            visible = not hidden and (bool(text) or placeholder)
            button.setProperty("caseVisible", visible)
            if not visible:
                continue
            shown = text if text else _("+ {field}").format(field=self._placeholderName(key))
            metrics = qt.QFontMetrics(button.font)
            elided = metrics.elidedText(shown, qt.Qt.ElideMiddle if key == "doi" else qt.Qt.ElideRight, FIELD_MAX_WIDTH)
            button.setText(elided)
            button.setProperty("casePlaceholder", placeholder)
            level = "warn" if key == "review" and row.get("review_status") == "changes" else ""
            button.setProperty("caseLevel", level)
            button.style().unpolish(button)
            button.style().polish(button)
            button.setToolTip(self._fieldToolTip(key, text, title))
            button.setAccessibleName(f"{title}: {text}" if text else title)
            button.setIcon(qt.QIcon())
        self._updateThumbnail(session, sid)
        self.widget.show()
        self.relayout()
        self._pin.attach(self.widget.parentWidget())

    @staticmethod
    def _placeholderName(key):
        return {"doi": _("DOI"), "figure": _("Fig"), "modality": _("Modality"), "side": _("Side, location"), "symptom": _("Symptom")}.get(key, key)

    @staticmethod
    def _fieldToolTip(key, text, title):
        if key == "case":
            return ui.tooltip(_("Case {sid}").format(sid=text), None, _("click to switch to another case of the study"))
        if key == "doi":
            return ui.tooltip(text or _("DOI"), None, _("click to open the paper in the browser; right-click to edit") if text else _("click to enter the DOI"))
        if key == "side":
            return ui.tooltip(_("Reported by the paper: {text}").format(text=text) if text else title, None, _("click to edit"))
        if key == "review":
            return ui.tooltip(_("Reviewer: {text}").format(text=text), None, _("click to read it in full"))
        return ui.tooltip(f"{title}: {text}" if text else title, None, _("click to edit"))

    def _updateThumbnail(self, session, sid):
        """The crop of the case's figure (a new case of the paper has the paper's crops in its folder, not its own figure)."""
        button = self.fieldButtons["figure"]
        if not button.property("caseVisible") or self._row is None or (not figureText(self._row) and _missing(self._row.get("fit_rms_mm"))):
            return
        try:
            thumbnail = session.figureThumbnail(sid)
        except Exception:
            logging.error(f"Studio: the figure crop of {sid} could not be shown:\n{traceback.format_exc()}")
            thumbnail = None
        if thumbnail is None:
            return
        pixmap = qt.QPixmap()
        if pixmap.loadFromData(qt.QByteArray(thumbnail[0]), "PNG"):
            button.setIcon(qt.QIcon(pixmap))

    def relayout(self):
        """Place the visible fields on their fixed 28 px lines (``LINES``), in order; the fields that do not fit their line
        go into "+N" at the end of the last line.
        """
        if not _isAlive(self.lines) or self._row is None:
            return
        left = 4
        width = max(1, self.lines.width - 2 * left)
        for button in self.fieldButtons.values():
            if not button.property("caseVisible"):
                button.hide()
        rows = []
        for keys in LINES:
            items = [self.queueButton if key == "queue" else self.fieldButtons[key] for key in keys
                     if key == "queue" or self.fieldButtons[key].property("caseVisible")]
            if items:
                rows.append(items)

        def place(items, room):
            """``(placed [(item, x, width)], the items that do not fit, x after the last one)``: the first item is always
            placed (elided to the room), the others in order while they fit.
            """
            x, placed, rest = 0, [], []
            for index, item in enumerate(items):
                itemWidth = min(item.sizeHint.width(), room)
                if rest or (index > 0 and x + itemWidth > room):
                    rest.append(item)
                    continue
                placed.append((item, x, itemWidth))
                x += itemWidth + SPACING
            return placed, rest, x

        lines = [place(items, width) for items in rows]
        moreWidth = self.moreButton.sizeHint.width() + SPACING
        if any(rest for _p, rest, _x in lines):
            # "+N" at the end of the last line: that line keeps room for it
            lines[-1] = place(rows[-1], max(1, width - moreWidth))
        top = (LINE_HEIGHT - FIELD_HEIGHT) // 2
        overflow = []
        for lineIndex, (placed, rest, _x) in enumerate(lines):
            for item, itemX, itemWidth in placed:
                item.setGeometry(left + itemX, lineIndex * LINE_HEIGHT + top, itemWidth, FIELD_HEIGHT)
                item.show()
            for item in rest:
                item.hide()
            overflow.extend(rest)
        self._hidden = [str(item.property("caseField")) for item in overflow if item is not self.queueButton]
        titles = {key: title for key, _c, title in FIELDS}
        if self._hidden:
            self.moreButton.setText(f"+{len(self._hidden)}")
            self.moreButton.setToolTip(ui.tooltip(_("More case fields"), None, ", ".join(titles[key] for key in self._hidden)))
            self.moreMenu.clear()
            for key in self._hidden:
                action = self.moreMenu.addAction(f"{titles[key]}: {fieldText(key, self._row) or _('not set')}")
                action.connect("triggered()", lambda key=key: self._onFieldClicked(key))
            self.moreButton.setGeometry(left + lines[-1][2], (len(lines) - 1) * LINE_HEIGHT + top, moreWidth - SPACING, FIELD_HEIGHT)
            self.moreButton.show()
        else:
            self.moreButton.hide()
        height = max(1, len(lines)) * LINE_HEIGHT
        if self.lines.height != height:
            self.lines.setFixedHeight(height)

    # Queue

    def _fillQueueMenu(self):
        self.queueMenu.clear()
        session = _session()
        if session is None:
            return
        studyUi = _studyUi()
        group = self._queueGroup
        active = session.queue()
        activeKey = active["key"] if active is not None else None
        allAction = self.queueMenu.addAction(_("All subjects (table order) · {n}").format(n=len(session.study)))
        allAction.setCheckable(True)
        allAction.setChecked(activeKey is None)
        allAction.setToolTip(_("Done → next opens the next untraced subject; Alt+←/→ step in table order"))
        group.addAction(allAction)
        allAction.connect("triggered()", lambda: self._setQueue(None))
        self.queueMenu.addSeparator()
        row = self._row or {}
        for spec in studyUi.availableQueues(row):
            count = len(session.queueMembers(spec))
            label = session.queueLabel(spec)
            action = self.queueMenu.addAction(f"{label} · {count}")
            action.setCheckable(True)
            action.setChecked(spec["key"] == activeKey)
            action.setEnabled(count > 0 or spec["key"] == activeKey)
            action.setObjectName(f"StudioCaseStripQueue_{spec['key']}")
            group.addAction(action)
            action.connect("triggered()", lambda key=spec["key"]: self._setQueue(key))
        if studyUi.recordOf(row) is None:
            action = self.queueMenu.addAction(_("This paper (this case names no paper)"))
            action.setEnabled(False)

    def _setQueue(self, key):
        session = _session()
        if session is None:
            return
        try:
            session.setQueue(key)
        except ValueError as e:
            shell = _shell()
            if shell is not None:
                shell.toast(str(e), level="warning")

    # Field clicks

    def _onFieldClicked(self, key):
        session = _session()
        if session is None or self._row is None:
            return
        text = fieldText(key, self._row)
        if key == "case":
            _studyUi().showSubjectSwitcher(_shell())
            return
        if key == "doi" and text:
            _openLink("https://doi.org/" + text)
            return
        self.startEdit(key)

    def _onFieldMenu(self, key):
        if self._row is None:
            return
        menu = qt.QMenu(self.widget)
        menu.setObjectName("StudioCaseStripFieldMenu")
        text = fieldText(key, self._row)
        if key == "doi" and text:
            menu.addAction(_("Open in the browser")).connect("triggered()", lambda: _openLink("https://doi.org/" + text))
        if key != "case":
            menu.addAction(_("Edit…") if key != "review" else _("Read")).connect("triggered()", lambda: self.startEdit(key))
        if text:
            menu.addAction(_("Copy")).connect("triggered()", lambda: qt.QApplication.clipboard().setText(text))
        if key == "figure":
            session = _session()
            thumbnail = session.figureThumbnail(self._sid) if session is not None else None
            if thumbnail is not None:
                menu.addAction(_("Show the figure source")).connect("triggered()", lambda: self._showSource(thumbnail[2]))
        if menu.actions():
            run = getattr(menu, "exec", None) or menu.exec_  # QMenu.exec in Qt 6, exec_ in Qt 5
            run(qt.QCursor.pos())
        menu.deleteLater()

    @staticmethod
    def _showSource(fitPath):
        try:
            from . import figure

            figure.showSource(fitPath, _shell())
        except Exception as e:
            logging.error(f"Studio: the figure source could not be shown:\n{traceback.format_exc()}")
            shell = _shell()
            if shell is not None:
                shell.toast(_("The figure source could not be shown: {error}").format(error=e), level="warning")

    # Inline editor

    def isEditing(self):
        return self._editing is not None

    def startEdit(self, key):
        """Open the inline editor of a field (the review comment is shown read-only)."""
        session = _session()
        if session is None or self._row is None:
            return False
        if self._editing is not None:
            self.cancelEdit()
        row = self._row
        self._editing, self._editSid, self._confirmed = key, self._sid, None
        self._inputs = {}
        titles = {k: t for k, _c, t in FIELDS}
        self.editorTitle.setText(titles[key])
        self.messageLabel.hide()
        readOnly = key == "review"
        if readOnly:
            label = qt.QLabel(fieldText(key, row))
            label.setObjectName("StudioCaseStripEditorInput")
            label.setWordWrap(True)
            label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
            reviewer = row.get("reviewer")
            if not _missing(reviewer):
                label.setToolTip(_("Reviewer: {name}").format(name=reviewer))
            self.inputsRow.addWidget(label, 1)
            self._inputs["review_comment"] = label
        elif key == "doi":
            self._lineEdit("doi", _studyUi().normalizeDoi(row.get("doi")), _("10.xxxx/…"))
        elif key == "figure":
            self._lineEdit("figure", "" if _missing(row.get("figure")) else row["figure"], _("Figure (2, S3)"))
            self._lineEdit("panel", "" if _missing(row.get("panel")) else row["panel"], _("Panel (B)"), stretch=0, width=70)
        elif key == "modality":
            self._combo("image_modality", row.get("image_modality"), self._levels("image_modality"))
        elif key == "side":
            self._combo("reported_side", row.get("reported_side"), [(level, SIDE_WORDS.get(level, level)) for level in self._levels("reported_side")], stretch=0)
            self._lineEdit("reported_location", "" if _missing(row.get("reported_location")) else row["reported_location"], _("Location (e.g. left insula)"))
        elif key == "symptom":
            edit = self._lineEdit("symptom", "" if _missing(row.get("symptom")) else row["symptom"], _("Symptom"))
            values = sorted({str(other.get("symptom")) for other in session.study.subjects if not _missing(other.get("symptom"))})
            completer = qt.QCompleter(values, edit)
            completer.setCaseSensitivity(qt.Qt.CaseInsensitive)
            edit.setCompleter(completer)
        self.saveButton.setVisible(not readOnly)
        self.cancelButton.setText(_("Close · {key}").format(key=ui.keyText("Esc")) if readOnly else _("Cancel · {key}").format(key=ui.keyText("Esc")))
        self.editor.show()
        for widget in (self.editor, self.cancelButton):
            widget.removeEventFilter(self._keys)
            widget.installEventFilter(self._keys)
        shell = _shell()
        if shell is not None and hasattr(shell, "registerEscapeOverlay"):
            shell.registerEscapeOverlay(self.editor, self.cancelEdit)
        first = next(iter(self._inputs.values()))
        (self.cancelButton if readOnly else first).setFocus(qt.Qt.OtherFocusReason)
        return True

    @staticmethod
    def _levels(column):
        try:
            from LesionNetworkMappingLib import schema
        except ImportError:
            return []
        spec = schema.registered(column)
        return list(spec.levels) if spec is not None and spec.levels else []

    def _lineEdit(self, column, text, placeholder, stretch=1, width=None):
        edit = qt.QLineEdit(str(text))
        edit.setObjectName("StudioCaseStripEditorInput")
        edit.setProperty("caseColumn", column)
        edit.setPlaceholderText(placeholder)
        ui.setClearButton(edit)
        if width:
            edit.setFixedWidth(width)
        edit.installEventFilter(self._keys)
        self.inputsRow.addWidget(edit, stretch)
        self._inputs[column] = edit
        return edit

    def _combo(self, column, value, levels, stretch=1):
        combo = qt.QComboBox()
        combo.setObjectName("StudioCaseStripEditorInput")
        combo.setProperty("caseColumn", column)
        combo.addItem(_("Not set"), "n/a")
        for level in levels:
            level, text = level if isinstance(level, tuple) else (level, level)
            combo.addItem(text, level)
        current = "n/a" if _missing(value) else str(value)
        index = combo.findData(current)
        if index < 0:
            combo.addItem(current, current)
            index = combo.count - 1
        combo.setCurrentIndex(index)
        combo.installEventFilter(self._keys)
        self.inputsRow.addWidget(combo, stretch)
        self._inputs[column] = combo
        return combo

    def _values(self):
        values = {}
        for column, widget in self._inputs.items():
            if widget.className() == "QComboBox":
                values[column] = str(widget.currentData)
            elif widget.className() == "QLineEdit":
                values[column] = " ".join(str(widget.text).split())  # no tab or newline reaches participants.tsv
        return values

    def _message(self, text, level):
        token = {"danger": "danger", "warn": "warning"}.get(level, "text2")
        self.messageLabel.setStyleSheet(f"QLabel {{ color: {ui.token(token).name()}; background: transparent; }}")
        self.messageLabel.setText(text)
        self.messageLabel.show()

    def check(self, values):
        """``(normalized values, errors, warnings)`` of the editor's values against the study schema."""
        studyUi = _studyUi()
        try:
            from LesionNetworkMappingLib import schema
        except ImportError:
            schema = None
        normalized, errors, warnings = {}, [], []
        for column, text in values.items():
            text = text.strip() or "n/a"
            if column == "doi" and text != "n/a":
                text = studyUi.normalizeDoi(text)
                if not studyUi.isDoi(text):
                    warnings.append(_("{text} does not look like a DOI (10.xxxx/…)").format(text=text))
            if schema is not None:
                try:
                    text, warning = schema.normalize_value(column, text)
                except ValueError as e:
                    errors.append(str(e))
                    continue
                if warning:
                    warnings.append(warning)
            normalized[column] = text
        return normalized, errors, warnings

    def saveEdit(self):
        """Enter: check the values and save them through the study session. Return True if saved (or nothing changed)."""
        session = _session()
        if self._editing is None or session is None:
            return False
        if self._editing == "review":
            self.cancelEdit()
            return True
        if self._editSid != session.current() or self._editSid not in session.study:
            self.cancelEdit()
            return False
        values, errors, warnings = self.check(self._values())
        if errors:
            self._message(" ".join(errors), "danger")
            return False
        if warnings and self._confirmed != values:
            self._confirmed = values
            self._message(" ".join(warnings) + " " + _("Press Enter again to keep it."), "warn")
            return False
        return self._store(session, values)[0]

    def commitEdit(self, session=None):
        """Save the open editor's values for its case without a second Enter (the case is being left: Done → next, Alt+←/→,
        the subject switcher; a warning does not hold the value back). Return ``(True, "")`` when saved or when nothing was
        being edited, ``(False, reason)`` when a value is invalid: the editor stays open and shows the reason.
        """
        session = session or _session()
        if self._editing is None:
            return True, ""
        if self._editing == "review" or session is None or self._editSid not in session.study:
            self.cancelEdit()
            return True, ""
        values, errors, _warnings = self.check(self._values())
        if errors:
            self._message(" ".join(errors), "danger")
            return False, self._reason(" ".join(errors))
        return self._store(session, values)

    def _reason(self, text):
        titles = {key: title for key, _c, title in FIELDS}
        return f"{titles.get(self._editing, self._editing)}: {text}"

    def _store(self, session, values):
        """Save checked values for the edited case and close the editor: ``(True, "")``, or ``(False, reason)``."""
        sid = self._editSid
        before = session.study.subject(sid)
        changed = {column: value for column, value in values.items() if str(before.get(column, "n/a")) != value}
        if changed:
            try:
                session.setValues([sid], changed)
            except ValueError as e:
                self._message(str(e), "danger")
                return False, self._reason(str(e))
        self._close()
        if changed:
            self._offerForPaper(session, sid, before, changed)
        return True, ""

    def _offerForPaper(self, session, sid, before, changed):
        """A paper field changed on one case: offer the same change for the paper's other cases that had the old value."""
        studyUi = _studyUi()
        paperChanges = {column: value for column, value in changed.items() if column in PAPER_FIELDS}
        paper = studyUi.recordOf(before)
        if not paperChanges or paper is None:
            return
        others = [row["participant_id"] for row in session.study.subjects if row["participant_id"] != sid and studyUi.recordOf(row) == paper
                  and all(str(row.get(column, "n/a")) == str(before.get(column, "n/a")) for column in paperChanges)]
        shell = _shell()
        if not others or shell is None:
            return
        shell.toast(_("Changed for {sid}. The paper has {n} other cases with the old value").format(sid=sid, n=len(others)), ms=8000,
                    action=lambda: session.setValues(others, paperChanges), actionText=_("Apply to {n}").format(n=len(others)), level="consequence")

    def cancelEdit(self):
        """Esc: close the inline editor without saving."""
        self._close()

    def _close(self):
        if self._editing is None:
            return
        focus = slicer.app.focusWidget()
        hadFocus = focus is not None and self.editor.isAncestorOf(focus)
        self._editing = self._editSid = self._confirmed = None
        shell = _shell()
        if shell is not None and hasattr(shell, "unregisterEscapeOverlay"):
            with contextlib.suppress(Exception):
                shell.unregisterEscapeOverlay(self.editor)
        for widget in (self.editor, self.cancelButton):
            widget.removeEventFilter(self._keys)
        for widget in list(self._inputs.values()):
            widget.removeEventFilter(self._keys)
            self.inputsRow.removeWidget(widget)
            widget.hide()
            widget.deleteLater()
        self._inputs = {}
        self.messageLabel.hide()
        self.editor.hide()
        if hadFocus:
            self.fieldButtons["case"].setFocus(qt.Qt.OtherFocusReason)

    def removeEventFilters(self):
        """Python event filters must not run while Python is finalized (the application quits)."""
        self._close()
        self._pin.detach()
        if _isAlive(self.lines):
            self.lines.removeEventFilter(self._linesFilter)

    def dispose(self):
        self.removeEventFilters()
        if _isAlive(self.widget):
            self.widget.hide()
            self.widget.deleteLater()


#
# Plug-in (study_ui.install)
#

_strip = None
_installedShell = None
_selectorConnected = None


def strip():
    """The case strip (created once), or None before ``install()``."""
    return _strip


def commitPendingEdit(studySession=None):
    """Save the strip's open inline editor before its case is left (``StudySession`` calls it): ``(True, "")`` when saved or
    when nothing is being edited, ``(False, reason)`` when a value is invalid (the editor stays open with the reason).
    """
    if _strip is None or not _isAlive(_strip.widget) or not _strip.isEditing():
        return True, ""
    return _strip.commitEdit(studySession)


def _ensureCard():
    """Add the strip to the Draw inspector and the case series items to the Lesion card ⋯ menu (once the Draw panel exists)."""
    global _strip
    if _installedShell is None:
        return False
    draw = _draw()
    if draw is None:
        return False
    try:
        extension = draw.extension()
        if extension._header is None:
            return False
        if _strip is None or not _isAlive(_strip.widget):
            _strip = CaseStrip()
        if extension._cards.get(CARD_KEY, (None,))[0] is not _strip.widget:
            extension.addInspectorCard(CARD_KEY, _strip.widget, CARD_ORDER)
        _updateLesionMenu(draw)
        _strip.refresh()
        return True
    except Exception:
        logging.error(f"Studio: the case strip could not be added:\n{traceback.format_exc()}")
        return False


def _updateLesionMenu(draw):
    lesionCard = draw.lesionCard()
    if lesionCard is None:
        return
    for text in (NEW_CASE_TEXT, NEW_CASE_FIGURE_TEXT):
        lesionCard.removeMenuAction(text)
    session = _session()
    if session is None or not isLiteratureStudy(session.study):
        return  # a study without papers would only refuse them
    studyUi = _studyUi()
    lesionCard.addMenuAction(NEW_CASE_TEXT, lambda: studyUi.newCaseFromPaperAction(_installedShell))
    lesionCard.addMenuAction(NEW_CASE_FIGURE_TEXT, lambda: studyUi.newCaseFromClipboardFigureAction(_installedShell))


def _onStudyChanged(studySession=None):
    if _installedShell is None:
        return
    if _strip is None or not _isAlive(_strip.widget):
        _ensureCard()
        return
    draw = _draw()
    if draw is not None:
        _updateLesionMenu(draw)
    _strip.refresh()


def _onModuleSelected(moduleName):
    if moduleName == "SegmentEditor":
        qt.QTimer.singleShot(0, _ensureCard)


_quitConnected = [False]


def _onAboutToQuit():
    if _strip is not None:
        _strip.removeEventFilters()


def install(shell):
    """Studio: the strip in Draw (added once the Draw panel exists) and the Lesion card ⋯ items."""
    global _installedShell, _selectorConnected
    _installedShell = shell
    if not _quitConnected[0]:
        # Also at Python exit: a script can quit without aboutToQuit (and atexit runs this before ui deletes orphans)
        import atexit

        slicer.app.connect("aboutToQuit()", _onAboutToQuit)
        atexit.register(_onAboutToQuit)
        _quitConnected[0] = True
    studyUi = _studyUi()
    studyUi.removeListener(_onStudyChanged)
    studyUi.addListener(_onStudyChanged)
    selector = shell.mainWindow.moduleSelector() if hasattr(shell.mainWindow, "moduleSelector") else None
    if selector is not None and _selectorConnected is not selector:
        selector.connect("moduleSelected(QString)", _onModuleSelected)
        _selectorConnected = selector
    _ensureCard()


def uninstall(shell):
    """Classic: the strip and the Lesion card items leave Draw (the study stays open)."""
    global _installedShell, _strip, _selectorConnected
    _installedShell = None
    with contextlib.suppress(Exception):
        _studyUi().removeListener(_onStudyChanged)
    if _selectorConnected is not None:
        with contextlib.suppress(Exception):
            _selectorConnected.disconnect("moduleSelected(QString)", _onModuleSelected)
        _selectorConnected = None
    draw = _draw()
    if draw is not None:
        with contextlib.suppress(Exception):
            draw.removeInspectorCard(CARD_KEY)
        lesionCard = draw.lesionCard()
        if lesionCard is not None:
            for text in (NEW_CASE_TEXT, NEW_CASE_FIGURE_TEXT):
                lesionCard.removeMenuAction(text)
    if _strip is not None:
        _strip.dispose()
        _strip = None
