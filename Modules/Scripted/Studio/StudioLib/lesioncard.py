"""Lesion card of the Draw workspace of Slicer Studio (SPEC 5.2, USABILITY 7.7 and 8).

Two parts:

- ``computeLesionMetrics(array_kji, spacing, ijkToRAS, axis=0, brainMask=None)``: pure numpy (and
  ``scipy.ndimage.label``) metrics of a lesion mask: voxel count, volume in mL, number of parts (26-connectivity),
  gap slices, centroid, hemisphere extent and the part outside a brain mask. Unit-testable without Slicer.
- ``LesionCard``: the card at the top of Draw. It only shows what its controller (``StudioLib.draw``) gives it
  (``refresh(state)``) and calls the controller back; it never changes the scene by itself.

    Row 1   [swatch] [name or lesion switcher ▾] [eye] [center] [follow] [⋯]
    Row 2   Left · 2.27 mL · 1 part · 0 gaps       (amber parts / gaps / outside with their links)
    Row 3   Saved · sub-01_lesion.nii.gz · 2 min ago       [New lesion] once the lesion is saved
    Row 4   space line (amber or neutral), when there is one
    Row 5   hint line (the suggested tool, the active tool), when there is one
    Row 6   [Fill gaps and specks] ...... [Notes ▸]   and the Notes fields when open
            chip rows added with ``addChipRow()``

``addChipRow(key, widget)`` and ``addMenuAction(label, callback)`` let other work packages (figure card, case
strip, policy card) add their rows and ⋯ items without editing this file.
"""

import html

import numpy as np
import qt
import slicer
from slicer.i18n import tr as _

from . import ui

#: Connectivity of the parts of a lesion: 26 neighbours (faces, edges and corners)
PARTS_STRUCTURE = np.ones((3, 3, 3), dtype=bool)

#: Link targets of the readout line
LINK_KEEP_LARGEST = "keep-largest"
LINK_FILL = "fill"
LINK_CLIP = "clip"

#: Height of the first row (swatch, name, buttons)
ROW_HEIGHT = 28
BUTTON_SIZE = 24
SWATCH_SIZE = 16
#: The card gets narrower than its contents rather than making the inspector wider
MINIMUM_WIDTH = 200


#
# Metrics (pure numpy)
#


def _boundingBox(mask):
    """``(k0, k1, j0, j1, i0, i1)`` (inclusive) of the non-zero voxels of a 3D boolean array, None if it is empty.

    The k range is found on the whole array, the j and i ranges only on that slab: a small lesion in a large
    grid costs about one pass over the array.
    """
    kAny = mask.any(axis=(1, 2))
    kIndices = np.flatnonzero(kAny)
    if kIndices.size == 0:
        return None
    k0, k1 = int(kIndices[0]), int(kIndices[-1])
    slab = mask[k0:k1 + 1]
    jIndices = np.flatnonzero(slab.any(axis=(0, 2)))
    iIndices = np.flatnonzero(slab.any(axis=(0, 1)))
    return k0, k1, int(jIndices[0]), int(jIndices[-1]), int(iIndices[0]), int(iIndices[-1])


def computeLesionMetrics(array_kji, spacing, ijkToRAS, axis=0, brainMask=None):
    """Metrics of a lesion mask.

    :param array_kji: 3D array in numpy order (k, j, i); non-zero voxels are the lesion.
    :param spacing: (i, j, k) voxel size in mm, used for the volume when ``ijkToRAS`` is None.
    :param ijkToRAS: 4x4 matrix (numpy array or nested lists) that maps the (i, j, k, 1) index of the array to RAS.
    :param axis: array axis (0 = k, 1 = j, 2 = i) along which gap slices are counted (the slicing axis of a view).
    :param brainMask: optional array of the same shape; ``outside_pct`` is the percentage of lesion voxels outside it.
    :return: ``{voxels, ml, parts, gaps, centroid_ras, outside_pct, bbox, x_min, x_max, spans_both}``:

      - ``gaps``: array indices along ``axis``, inside the lesion extent, of the slices without any lesion voxel;
      - ``centroid_ras``: (R, A, S) of the centre of mass, None for an empty lesion;
      - ``x_min``/``x_max``: RAS x extent of the voxel centres; ``spans_both``: voxels lie more than one voxel on both
        sides of x = 0 (the midline on an MNI grid only);
      - ``bbox``: ``(k0, k1, j0, j1, i0, i1)`` inclusive, None for an empty lesion.
    """
    array = np.asarray(array_kji)
    mask = array if array.dtype == bool else array != 0
    matrix = np.asarray(ijkToRAS, dtype=float) if ijkToRAS is not None else np.diag([*(float(s) for s in spacing), 1.0])
    voxelVolume = abs(float(np.linalg.det(matrix[:3, :3])))
    empty = {"voxels": 0, "ml": 0.0, "parts": 0, "gaps": [], "centroid_ras": None, "outside_pct": None, "bbox": None,
             "x_min": None, "x_max": None, "spans_both": False}
    if mask.ndim != 3 or mask.size == 0:
        return empty
    bbox = _boundingBox(mask)
    if bbox is None:
        return empty
    k0, k1, j0, j1, i0, i1 = bbox
    crop = mask[k0:k1 + 1, j0:j1 + 1, i0:i1 + 1]
    voxels = int(np.count_nonzero(crop))

    from scipy import ndimage

    _labels, parts = ndimage.label(crop, structure=PARTS_STRUCTURE)

    # Gap slices: empty slices between the first and the last slice of the lesion along the axis
    otherAxes = tuple(index for index in range(3) if index != axis)
    occupied = crop.any(axis=otherAxes)
    offset = (k0, j0, i0)[axis]
    gaps = [int(index) + offset for index in np.flatnonzero(~occupied)]

    kk, jj, ii = np.nonzero(crop)
    indices = np.vstack((ii + i0, jj + j0, kk + k0, np.ones(ii.size))).astype(float)
    centroid = matrix @ indices.mean(axis=1)
    xs = matrix[0] @ indices
    xMin, xMax = float(xs.min()), float(xs.max())
    voxelWidthX = float(np.linalg.norm(matrix[0, :3])) or 1.0

    outside = None
    if brainMask is not None:
        brain = np.asarray(brainMask)
        if brain.shape == mask.shape:
            brainCrop = brain[k0:k1 + 1, j0:j1 + 1, i0:i1 + 1] != 0
            outside = 100.0 * float(np.count_nonzero(crop & ~brainCrop)) / voxels
    return {
        "voxels": voxels,
        "ml": voxels * voxelVolume / 1000.0,
        "parts": int(parts),
        "gaps": gaps,
        "centroid_ras": (float(centroid[0]), float(centroid[1]), float(centroid[2])),
        "outside_pct": outside,
        "bbox": bbox,
        "x_min": xMin,
        "x_max": xMax,
        "spans_both": xMin < -voxelWidthX and xMax > voxelWidthX,
    }


def formatMl(ml):
    """Volume text: "2.27 mL", "0.042 mL" (three significant digits below 0.1 mL)."""
    if ml >= 100:
        return f"{ml:.0f} mL"
    if ml >= 0.1:
        return f"{ml:.2f} mL"
    return f"{ml:.2g} mL" if ml > 0 else "0 mL"


def readoutHtml(metrics, sideWord=None, sideLevel="neutral", outsideKnown=False):
    """The readout line of the card as rich text: "Left · 2.27 mL · 1 part · 0 gaps", amber items with their links.

    :param metrics: a ``computeLesionMetrics()`` result (or None: "Empty lesion").
    :param sideWord: "Left", "Right" or "Both hemispheres" (MNI only), None in scanner space.
    """
    warning = ui.token("warning").name()
    link = ui.token("accent-fg").name()

    def amber(text):
        return f'<span style="color: {warning};">{html.escape(text)}</span>'

    def anchor(target, text):
        return f'<a href="{target}" style="color: {link}; text-decoration: none;">{html.escape(text)}</a>'

    if metrics is None or not metrics.get("voxels"):
        return html.escape(_("Empty lesion · pick a tool and draw"))
    parts = []
    if sideWord:
        parts.append(amber(sideWord) if sideLevel == "warn" else html.escape(sideWord))
    parts.append(html.escape(formatMl(metrics["ml"])))
    count = metrics["parts"]
    partsText = _("1 part") if count == 1 else _("{count} parts").format(count=count)
    parts.append(amber(partsText) + " " + anchor(LINK_KEEP_LARGEST, _("Keep largest")) if count > 1 else html.escape(partsText))
    gapCount = len(metrics["gaps"])
    gapsText = _("1 gap") if gapCount == 1 else _("{count} gaps").format(count=gapCount)
    parts.append(amber(gapsText) + " " + anchor(LINK_FILL, _("Fill")) if gapCount > 0 else html.escape(gapsText))
    outside = metrics.get("outside_pct")
    if outsideKnown and outside is not None:
        outsideText = _("{pct:.0f}% outside brain").format(pct=outside)
        parts.append(amber(outsideText) + " " + anchor(LINK_CLIP, _("Clip")) if outside > 1.0 else html.escape(outsideText))
    return " · ".join(parts)


#
# Widget
#


def _setPixelFont(widget, basePx, weight=None):
    font = qt.QFont(widget.font)
    font.setPixelSize(ui.fontPx(basePx))
    if weight is not None:
        font.setWeight(weight)
    widget.setFont(font)


def _iconButton(iconName, toolTip, objectName, checkable=False):
    button = qt.QToolButton()
    button.setObjectName(objectName)
    button.setIcon(ui.icon(iconName, 16))
    button.setIconSize(qt.QSize(16, 16))
    button.setFixedSize(BUTTON_SIZE, BUTTON_SIZE)
    button.setAutoRaise(True)
    button.setCheckable(checkable)
    button.setToolTip(toolTip)
    button.setAccessibleName(toolTip.split(" · ")[0] if " · " in toolTip else toolTip)
    button.setFocusPolicy(qt.Qt.TabFocus)
    ui.setRole(button, "ghost")
    ui.installButtonKeys(button)
    return button


class _NameEdit(qt.QLineEdit):
    """Flat line edit of the lesion name: Enter commits, Esc (with unsent edits) restores the name."""

    def __init__(self, commit):
        qt.QLineEdit.__init__(self)
        self._commit = commit
        self._shown = ""
        self.connect("editingFinished()", self._onEditingFinished)

    def setName(self, name):
        self._shown = name
        if not self.hasFocus() and self.text != name:
            self.setText(name)
            self.setCursorPosition(0)

    def _onEditingFinished(self):
        text = str(self.text).strip()
        if text and text != self._shown:
            self._commit(text)
        elif not text:
            self.setText(self._shown)


class LesionCard:
    """The Lesion card. ``controller`` implements the callbacks (``StudioLib.draw``):

    ``renameLesion(name)``, ``chooseColor()``, ``setLesionVisible(bool)``, ``selectLesion(nodeId)``,
    ``setLesionVisibleById(nodeId, bool)``, ``centerOnLesion()``, ``setFollowStrokes(bool)``, ``keepLargest()``,
    ``fillGaps()``, ``clipToBrain()``, ``fillGapsAndSpecks()``, ``newLesion()``, ``setNotes(text, lesionId)``,
    ``setSourceReference(text, lesionId)`` (the lesion shown when the text was typed) and ``menuActions()`` -> ``[(label, callback)]`` (None for a separator).
    """

    def __init__(self, controller):
        self.controller = controller
        self._extraMenuActions = []
        self._chipRows = {}
        self._state = {}

        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioLesionCard")
        ui.setRole(self.widget, "card")
        self.widget.setMinimumWidth(MINIMUM_WIDTH)
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(3)
        self.layout = layout

        # Row 1
        row = qt.QHBoxLayout()
        row.setSpacing(4)
        self.swatch = qt.QToolButton()
        self.swatch.setObjectName("StudioLesionSwatch")
        self.swatch.setFixedSize(SWATCH_SIZE + 4, SWATCH_SIZE + 4)
        self.swatch.setAutoRaise(True)
        self.swatch.setToolTip(ui.tooltip(_("Colour"), None, _("Choose the colour of the lesion")))
        self.swatch.setAccessibleName(_("Lesion colour"))
        self.swatch.setFocusPolicy(qt.Qt.TabFocus)
        self.swatch.connect("clicked()", self._onSwatchClicked)
        ui.installButtonKeys(self.swatch)
        row.addWidget(self.swatch)

        self.nameEdit = _NameEdit(self._onRename)
        self.nameEdit.setObjectName("StudioLesionNameEdit")
        self.nameEdit.setFrame(False)
        self.nameEdit.setMinimumWidth(60)
        self.nameEdit.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.nameEdit.setToolTip(ui.tooltip(_("Lesion name"), "Enter", _("Rename the lesion (the file name follows)")))
        self.nameEdit.setAccessibleName(_("Lesion name"))
        _setPixelFont(self.nameEdit, 13, qt.QFont.DemiBold)
        row.addWidget(self.nameEdit, 1)

        # Lesion switcher: shown instead of the name edit when there is more than one lesion
        self.switcher = qt.QToolButton()
        self.switcher.setObjectName("StudioLesionSwitcher")
        self.switcher.setPopupMode(qt.QToolButton.InstantPopup)
        self.switcher.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.switcher.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.switcher.setMinimumWidth(60)
        self.switcher.setToolTip(ui.tooltip(_("Lesions"), None, _("Choose the lesion to edit; the others keep their files")))
        self.switcher.setFocusPolicy(qt.Qt.TabFocus)
        _setPixelFont(self.switcher, 13, qt.QFont.DemiBold)
        self.switcherMenu = qt.QMenu(self.switcher)
        self.switcherMenu.setObjectName("StudioLesionSwitcherMenu")
        self.switcherMenu.connect("aboutToShow()", self._fillSwitcherMenu)
        self.switcher.setMenu(self.switcherMenu)
        ui.setRole(self.switcher, "ghost")
        self.switcher.hide()
        row.addWidget(self.switcher, 1)

        self.eyeButton = _iconButton("eye", ui.tooltip(_("Show lesion"), None, _("Show or hide the lesion in the views")), "StudioLesionEyeButton", checkable=True)
        self.eyeButton.connect("toggled(bool)", self._onEyeToggled)
        row.addWidget(self.eyeButton)
        self.centerButton = _iconButton("crosshair", ui.tooltip(_("Center on lesion"), "C", _("bring all views to the lesion")), "StudioCenterOnLesionButton")
        self.centerButton.connect("clicked()", self._onCenterClicked)
        row.addWidget(self.centerButton)
        self.followButton = _iconButton("link", ui.tooltip(_("Follow"), None, _("move the other views to each stroke")), "StudioFollowStrokesButton", checkable=True)
        self.followButton.connect("toggled(bool)", self._onFollowToggled)
        row.addWidget(self.followButton)
        self.moreButton = _iconButton("ellipsis", ui.tooltip(_("Lesion menu"), None, _("Rename, new lesion, save as, hide…")), "StudioLesionMenuButton")
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.menu = qt.QMenu(self.moreButton)
        self.menu.setObjectName("StudioLesionMenu")
        self.menu.connect("aboutToShow()", self._fillMenu)
        self.moreButton.setMenu(self.menu)
        row.addWidget(self.moreButton)
        layout.addLayout(row)

        # Rows 2 to 5
        self.readoutLabel = self._label("StudioLesionReadout", 12, links=True)
        self.saveStateLabel = self._label("StudioLesionSaveState", 12)
        self.spaceLabel = self._label("StudioLesionSpaceLine", 12)
        self.hintLabel = self._label("StudioLesionHint", 12)
        # Row 3: the save state, and "New lesion" once the lesion is saved (the next case starts on a new lesion and
        # never overwrites the saved file)
        self.saveRow = qt.QWidget()
        self.saveRow.setObjectName("StudioLesionSaveRow")
        self.saveRow.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        saveRowLayout = qt.QHBoxLayout(self.saveRow)
        saveRowLayout.setContentsMargins(0, 0, 0, 0)
        saveRowLayout.setSpacing(4)
        saveRowLayout.addWidget(self.saveStateLabel, 1)
        self.newLesionButton = qt.QPushButton(_("New lesion"))
        self.newLesionButton.setObjectName("StudioNewLesionButton")
        self.newLesionButton.setFlat(True)
        self.newLesionButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.newLesionButton.setToolTip(ui.tooltip(_("New lesion"), None, _("start the next lesion; the saved one keeps its file and is hidden")))
        self.newLesionButton.setFocusPolicy(qt.Qt.TabFocus)
        self.newLesionButton.connect("clicked()", self._onNewLesionClicked)
        ui.setRole(self.newLesionButton, "ghost")
        ui.installButtonKeys(self.newLesionButton)
        _setPixelFont(self.newLesionButton, 12)
        self.newLesionButton.hide()
        saveRowLayout.addWidget(self.newLesionButton, 0, qt.Qt.AlignTop)
        self.saveStateLabel.show()
        self.saveRow.hide()
        layout.addWidget(self.readoutLabel)
        layout.addWidget(self.saveRow)
        for label in (self.spaceLabel, self.hintLabel):
            layout.addWidget(label)

        # Row 6: one-click cleanup and the notes
        actionRow = qt.QHBoxLayout()
        actionRow.setSpacing(4)
        self.cleanupButton = qt.QToolButton()
        self.cleanupButton.setObjectName("StudioFillGapsAndSpecksButton")
        self.cleanupButton.setText(_("Fill gaps and specks"))
        self.cleanupButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.cleanupButton.setToolTip(ui.tooltip(_("Fill gaps and specks"), None, _("closes gaps up to 2 mm and removes islands under 10 mm³")))
        self.cleanupButton.setFocusPolicy(qt.Qt.TabFocus)
        self.cleanupButton.connect("clicked()", self._onCleanupClicked)
        ui.setRole(self.cleanupButton, "ghost")
        ui.installButtonKeys(self.cleanupButton)
        _setPixelFont(self.cleanupButton, 12)
        actionRow.addWidget(self.cleanupButton)
        actionRow.addStretch(1)
        self.notesButton = qt.QToolButton()
        self.notesButton.setObjectName("StudioLesionNotesButton")
        self.notesButton.setText(_("Notes"))
        self.notesButton.setCheckable(True)
        self.notesButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
        self.notesButton.setToolTip(ui.tooltip(_("Notes"), None, _("notes and the source (DOI, figure), saved next to the lesion file")))
        self.notesButton.setFocusPolicy(qt.Qt.TabFocus)
        self.notesButton.connect("toggled(bool)", self.setNotesVisible)
        ui.setRole(self.notesButton, "ghost")
        ui.installButtonKeys(self.notesButton)
        _setPixelFont(self.notesButton, 12)
        actionRow.addWidget(self.notesButton)
        layout.addLayout(actionRow)

        self.notesFrame = qt.QFrame()
        self.notesFrame.setObjectName("StudioLesionNotesFrame")
        notesLayout = qt.QVBoxLayout(self.notesFrame)
        notesLayout.setContentsMargins(0, 0, 0, 0)
        notesLayout.setSpacing(4)
        self.notesEdit = qt.QPlainTextEdit()
        self.notesEdit.setObjectName("StudioLesionNotesEdit")
        self.notesEdit.setPlaceholderText(_("Notes"))
        self.notesEdit.setTabChangesFocus(True)
        self.notesEdit.setFixedHeight(54)
        self.notesEdit.setAccessibleName(_("Lesion notes"))
        self.notesEdit.connect("textChanged()", self._onNotesChanged)
        notesLayout.addWidget(self.notesEdit)
        self.sourceReferenceEdit = qt.QLineEdit()
        self.sourceReferenceEdit.setObjectName("StudioLesionSourceReferenceEdit")
        self.sourceReferenceEdit.setPlaceholderText(_("Source (DOI, figure)"))
        self.sourceReferenceEdit.setAccessibleName(_("Source (DOI, figure)"))
        self.sourceReferenceEdit.connect("editingFinished()", self._commitSourceReference)
        notesLayout.addWidget(self.sourceReferenceEdit)
        self.notesFrame.hide()
        layout.addWidget(self.notesFrame)

        self.chipRowsLayout = qt.QVBoxLayout()
        self.chipRowsLayout.setSpacing(4)
        layout.addLayout(self.chipRowsLayout)

        self._notesTimer = qt.QTimer()
        self._notesTimer.setSingleShot(True)
        self._notesTimer.setInterval(500)
        self._notesTimer.connect("timeout()", self._commitNotes)
        ui.addPaletteListener(self._onPaletteChanged)

    def _label(self, objectName, basePx, links=False):
        label = qt.QLabel()
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        label.setTextFormat(qt.Qt.RichText if links else qt.Qt.PlainText)
        _setPixelFont(label, basePx)
        if links:
            label.setTextInteractionFlags(qt.Qt.LinksAccessibleByMouse | qt.Qt.LinksAccessibleByKeyboard)
            label.connect("linkActivated(QString)", self._onLinkActivated)
        label.hide()
        return label

    #
    # Contents
    #

    def refresh(self, state):
        """Show ``state``: ``{name, lesionId, color (r, g, b), visible, lesions [(id, name, visible)], readoutHtml, saveState
        (text), saveStateToolTip, offerNewLesion, spaceLine (text or None), spaceLevel ("warn" or "neutral"), hint, notes,
        sourceReference, follow, enabled}``. Missing keys keep their current value.
        """
        switched = "lesionId" in state and "lesionId" in self._state and state["lesionId"] != self._state["lesionId"]
        if switched:
            # Another lesion is shown: what was typed for the previous one goes to the previous one
            self.commitEdits()
        self._state.update(state)
        state = self._state
        name = state.get("name") or ""
        lesions = state.get("lesions") or []
        multiple = len(lesions) > 1
        self.nameEdit.setVisible(not multiple)
        self.switcher.setVisible(multiple)
        self.nameEdit.setName(name)
        self.switcher.setText(f"{name} ▾")
        self.switcher.setAccessibleName(_("Lesion {name}, {count} lesions").format(name=name, count=len(lesions)))
        # Without a segment yet (an empty lesion) the swatch is an empty outline: never an invisible button
        self._setSwatchColor(state.get("color"))
        enabled = bool(state.get("enabled", True))
        for widget in (self.swatch, self.nameEdit, self.eyeButton, self.centerButton, self.cleanupButton, self.notesButton):
            widget.setEnabled(enabled)
        self.eyeButton.blockSignals(True)
        self.eyeButton.setChecked(bool(state.get("visible", True)))
        self.eyeButton.setIcon(ui.icon("eye" if self.eyeButton.checked else "eye-off", 16))
        self.eyeButton.blockSignals(False)
        self.followButton.blockSignals(True)
        self.followButton.setChecked(bool(state.get("follow", True)))
        self.followButton.setIcon(ui.icon("link" if self.followButton.checked else "unlink", 16))
        self.followButton.blockSignals(False)
        self._setText(self.readoutLabel, state.get("readoutHtml"))
        self._setText(self.saveStateLabel, state.get("saveState"), "text3")
        self.saveStateLabel.setToolTip(state.get("saveStateToolTip") or "")
        self.saveRow.setVisible(bool(state.get("saveState")))
        self.newLesionButton.setVisible(bool(state.get("offerNewLesion")) and enabled)
        self._setText(self.spaceLabel, state.get("spaceLine"), "warning" if state.get("spaceLevel") == "warn" else "text2")
        self._setText(self.hintLabel, state.get("hint"), "text2")
        # Text being typed (focused, or not yet committed) is not replaced by the lesion's value, unless another
        # lesion is shown
        notesTyped = self.notesEdit.hasFocus() or self._notesTimer.isActive()
        if (switched or not notesTyped) and str(self.notesEdit.toPlainText()) != (state.get("notes") or ""):
            self.notesEdit.blockSignals(True)
            self.notesEdit.setPlainText(state.get("notes") or "")
            self.notesEdit.blockSignals(False)
        sourceTyped = self.sourceReferenceEdit.hasFocus() or self.sourceReferenceEdit.isModified()
        if (switched or not sourceTyped) and str(self.sourceReferenceEdit.text) != (state.get("sourceReference") or ""):
            self.sourceReferenceEdit.setText(state.get("sourceReference") or "")

    def _setText(self, label, text, colorToken=None):
        if not text:
            if label.isVisibleTo(self.widget):
                label.hide()
            return
        if str(label.text) != text:
            label.setText(text)
        if colorToken is not None:
            styleSheet = f"QLabel {{ color: {ui.token(colorToken).name()}; background: transparent; border: none; }}"
            if label.styleSheet != styleSheet:
                label.setStyleSheet(styleSheet)
        if not label.isVisibleTo(self.widget):
            label.show()

    def _setSwatchColor(self, color):
        if color is None:
            styleSheet = (f"QToolButton#StudioLesionSwatch {{ background-color: transparent; border: 1px dashed {ui.token('text3').name()}; "
                          f"border-radius: 4px; margin: 2px; }}")
        else:
            qcolor = qt.QColor.fromRgbF(*color[:3])
            styleSheet = (f"QToolButton#StudioLesionSwatch {{ background-color: {qcolor.name()}; border: 1px solid {ui.token('border').name()}; "
                          f"border-radius: 4px; margin: 2px; }}")
        if self.swatch.styleSheet != styleSheet:
            self.swatch.setStyleSheet(styleSheet)

    def _onPaletteChanged(self):
        if not ui._isAlive(self.widget):
            ui.removePaletteListener(self._onPaletteChanged)
            return
        for button, names in ((self.eyeButton, ("eye", "eye-off")), (self.followButton, ("link", "unlink"))):
            button.setIcon(ui.icon(names[0] if button.checked else names[1], 16))
        self.centerButton.setIcon(ui.icon("crosshair", 16))
        self.moreButton.setIcon(ui.icon("ellipsis", 16))
        state = dict(self._state)
        self._state = {}
        self.refresh(state)

    def setNotesVisible(self, visible):
        visible = bool(visible)
        if self.notesButton.checked != visible:
            self.notesButton.setChecked(visible)
            return
        self.notesButton.setText(_("Notes ▾") if visible else _("Notes"))
        self.notesFrame.setVisible(visible)

    def focusNotes(self, field="notes"):
        """Open the Notes and focus the notes field ("notes") or the source field ("source")."""
        self.setNotesVisible(True)
        (self.sourceReferenceEdit if field == "source" else self.notesEdit).setFocus(qt.Qt.OtherFocusReason)

    def startRename(self):
        """Focus the name field with the name selected (the ⋯ "Rename lesion" item)."""
        if self.switcher.isVisible():
            # Several lesions: rename in a dialog-free way by showing the edit for this lesion
            self.switcher.hide()
            self.nameEdit.show()
        self.nameEdit.setFocus(qt.Qt.OtherFocusReason)
        self.nameEdit.selectAll()

    #
    # Extension points (L-28, L-29, L-45)
    #

    def addChipRow(self, key, widget):
        """Add (or replace) a row of chips under the card's rows."""
        self.removeChipRow(key)
        self._chipRows[key] = widget
        widget.setParent(self.widget)
        self.chipRowsLayout.addWidget(widget)
        widget.show()

    def removeChipRow(self, key):
        widget = self._chipRows.pop(key, None)
        if widget is not None:
            self.chipRowsLayout.removeWidget(widget)
            widget.hide()
            widget.setParent(None)

    def addMenuAction(self, label, callback):
        """Add an item to the ⋯ menu (after the built-in items)."""
        self._extraMenuActions.append((label, callback))

    def removeMenuAction(self, label):
        self._extraMenuActions = [entry for entry in self._extraMenuActions if entry[0] != label]

    #
    # Events
    #

    def _onSwatchClicked(self):
        self.controller.chooseColor()

    def _onRename(self, name):
        self.controller.renameLesion(name)

    def _onCenterClicked(self):
        self.controller.centerOnLesion()

    def _onFollowToggled(self, checked):
        self.followButton.setIcon(ui.icon("link" if checked else "unlink", 16))
        self.controller.setFollowStrokes(bool(checked))

    def _onCleanupClicked(self):
        self.controller.fillGapsAndSpecks()

    def _onNewLesionClicked(self):
        self.controller.newLesion()

    def _onEyeToggled(self, checked):
        self.eyeButton.setIcon(ui.icon("eye" if checked else "eye-off", 16))
        self.controller.setLesionVisible(bool(checked))

    def _onLinkActivated(self, target):
        target = str(target)
        if target == LINK_KEEP_LARGEST:
            self.controller.keepLargest()
        elif target == LINK_FILL:
            self.controller.fillGaps()
        elif target == LINK_CLIP:
            self.controller.clipToBrain()

    def _onNotesChanged(self):
        self._notesTimer.start()

    def _commitNotes(self):
        self._notesTimer.stop()
        self.controller.setNotes(str(self.notesEdit.toPlainText()), self._state.get("lesionId"))

    def _commitSourceReference(self):
        self.sourceReferenceEdit.setModified(False)
        self.controller.setSourceReference(str(self.sourceReferenceEdit.text), self._state.get("lesionId"))

    def commitEdits(self):
        """Write the notes and the source reference that are still being typed to the lesion now (a save by keyboard
        shortcut neither waits for the notes debounce nor finishes the editing of the source field).

        Only edits the user made since the last commit are written: a field that only shows the lesion's own value is
        left alone.
        """
        if self._notesTimer.isActive():
            self._commitNotes()
        if self.sourceReferenceEdit.isModified():
            self._commitSourceReference()

    def _fillMenu(self):
        self.menu.clear()
        for entry in list(self.controller.menuActions()) + [None] + list(self._extraMenuActions):
            if entry is None:
                if self.menu.actions() and not self.menu.actions()[-1].isSeparator():
                    self.menu.addSeparator()
                continue
            label, callback = entry[0], entry[1]
            action = self.menu.addAction(label)
            action.connect("triggered()", callback)
        actions = self.menu.actions()
        if actions and actions[-1].isSeparator():
            self.menu.removeAction(actions[-1])

    def _fillSwitcherMenu(self):
        self.switcherMenu.clear()
        currentId = self._state.get("lesionId")
        for nodeId, name, visible in self._state.get("lesions") or []:
            action = qt.QWidgetAction(self.switcherMenu)
            rowWidget = qt.QWidget()
            rowLayout = qt.QHBoxLayout(rowWidget)
            rowLayout.setContentsMargins(8, 2, 4, 2)
            rowLayout.setSpacing(4)
            pick = qt.QToolButton()
            pick.setText(("✓ " if nodeId == currentId else "    ") + name)
            pick.setAutoRaise(True)
            pick.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            pick.setSizePolicy(qt.QSizePolicy.Expanding, qt.QSizePolicy.Fixed)
            pick.setStyleSheet("QToolButton { text-align: left; }")
            pick.connect("clicked()", lambda nodeId=nodeId: (self.switcherMenu.close(), self.controller.selectLesion(nodeId)))
            rowLayout.addWidget(pick, 1)
            eye = qt.QToolButton()
            eye.setCheckable(True)
            eye.setChecked(bool(visible))
            eye.setAutoRaise(True)
            eye.setIcon(ui.icon("eye" if visible else "eye-off", 16))
            eye.setToolTip(ui.tooltip(_("Show {name}").format(name=name), None, _("show or hide this lesion in the views")))
            eye.connect("toggled(bool)", lambda checked, nodeId=nodeId, eye=eye: (
                eye.setIcon(ui.icon("eye" if checked else "eye-off", 16)), self.controller.setLesionVisibleById(nodeId, checked)))
            rowLayout.addWidget(eye)
            action.setDefaultWidget(rowWidget)
            self.switcherMenu.addAction(action)
        self.switcherMenu.addSeparator()
        for entry in self.controller.menuActions():
            if entry is None:
                continue
            action = self.switcherMenu.addAction(entry[0])
            action.connect("triggered()", entry[1])

    def dispose(self):
        ui.removePaletteListener(self._onPaletteChanged)
        self._notesTimer.stop()
        for label in (self.readoutLabel,):
            try:
                label.disconnect("linkActivated(QString)", self._onLinkActivated)
            except Exception:
                pass
        self.controller = _NullController()
        # Deleted with its parent kept and its stylesheet bindings removed: a parentless widget that Python still holds
        # (ui.bindStyleSheet) would be deleted at interpreter exit, after its Python event filters are gone
        for target in (self.widget, *self.widget.findChildren("QWidget")):
            ui.bindStyleSheet(target, None)
        self.widget.hide()
        self.widget.deleteLater()


class _NullController:
    """Controller of a disposed card: every callback does nothing."""

    def menuActions(self):
        return []

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def isAvailable():
    """True when the card can be shown (scipy is needed for the parts)."""
    try:
        import scipy.ndimage  # noqa: F401
    except ImportError:
        return False
    return slicer.app is not None
