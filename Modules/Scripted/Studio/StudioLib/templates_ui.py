"""Your own MNI templates: the user interface (A-10, decision D-T2; VISUAL.md 4.7b, Templates.png, AddTemplate.png).

- ``AddTemplateDialog`` ("Add your own MNI template", ``studioAddTemplateDialog``): pick or drop the image, see what
  was detected (``templates.identify``), name it, choose its space and contrast, add an optional brain mask, whole-head
  image, 3D brain surfaces and a source and licence note, and "Use as my default template". The files are copied in
  the background writer with a progress bar; the originals are never changed.
- ``TemplatesSettingsPanel`` (Settings > Templates, ``studioTemplatesPanel``): every template with its mid-axial
  thumbnail, name, Built-in or Yours chip, space · resolution · contrast, the default (✓ Default or Set as default) and
  a ⋯ menu (Start on this template, Show in Finder, Remove…).
- ``populateTemplateMenu`` (``studioTemplateMenu``): the menu of the Library's "Start on template ▾" split button.
- Search entries: "Add MNI template", "Manage templates" and "Start on <your template>".
- ``offerMissingStudyTemplate``: the notice "This study uses the template <name> (MNI …). Add it…" (the dialog then
  accepts only the file with the study's checksum).

The model is ``templates``. Only the fork-only key ``Studio/Templates/Default`` is written (by ``templates``).
"""

import logging
import os
import re
import sys
import traceback

import ctk
import qt
import slicer
from slicer.i18n import tr as _

from . import templates, ui

PANEL_NAME = "Templates"
PANEL_OBJECT_NAME = "studioTemplatesPanel"
DIALOG_OBJECT_NAME = "studioAddTemplateDialog"
MENU_OBJECT_NAME = "studioTemplateMenu"
DIALOG_WIDTH = 600
ROW_THUMBNAIL = 48
CARD_THUMBNAIL = 72
#: Widest file list of an "Optional" row (px; longer lists are elided in the middle, the tooltip has them all)
VALUE_WIDTH = 230
NAME_WIDTH = 420  # a template name in the Settings list is elided to this width
FILE_NAME_WIDTH = 330  # the file name in the add dialog is elided to this width
CONTRAST_LABELS = (("T1w", "T1w"), ("T2w", "T2w"), ("PD", "PD"), ("FLAIR", "FLAIR"), ("CT", "CT"), ("other", "Other"))
IMAGE_FILTER = "Images (*.nii *.nii.gz *.nrrd);;All files (*)"
SURFACE_FILTER = "Surfaces (*.vtk *.vtp *.stl *.obj *.ply *.gii);;All files (*)"
#: Last folder of the file dialogs (fork-only)
LAST_DIRECTORY_KEY = "Studio/Templates/LastDirectory"

_panel = None
_dialog = None


def _shell():
    module = sys.modules.get("StudioLib.shell")
    current = module.instance() if module is not None and hasattr(module, "instance") else None
    return current if current is not None and current.isActive() else None


def message(text, level="info"):
    shell = _shell()
    if shell is not None:
        shell.toast(text, level=level)
    else:
        logging.info(f"Slicer Studio: {text}")


def _isAlive(widget):
    try:
        widget.objectName
        return True
    except Exception:
        return False


def _label(text, objectName, pixelSize=13, weight=None, color=None, wrap=False):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    ui._font(label, ui.fontPx(pixelSize), weight)
    if wrap:
        label.setWordWrap(True)
    if color:
        ui.bindStyleSheet(label, lambda: f"QLabel {{ color: {ui.token(color).name()}; background: transparent; border: none; }}")
    return label


def _setElidedText(label, text, maxWidth, mode=None):
    """Show ``text`` in a one-line ``label``, elided to ``maxWidth`` px (the whole text is its tool tip when elided).

    A plain QLabel asks for its whole text width, so a long name would widen the dialog that holds it.
    """
    metrics = qt.QFontMetrics(label.font)
    shown = metrics.elidedText(text or "", mode if mode is not None else qt.Qt.ElideRight, maxWidth)
    label.setText(shown)
    label.setToolTip(text if shown != text else "")


def _button(text, objectName, callback, role="secondary"):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
    ui.setRole(button, role)
    button.connect("clicked()", callback)
    return button


def _iconTile(iconName, size=40):
    tile = qt.QLabel()
    tile.setObjectName("studioTemplateIconTile")
    tile.setFixedSize(size, size)
    tile.setAlignment(qt.Qt.AlignCenter)
    tile.setPixmap(ui.pixmap(iconName, 20, "accent-fg"))
    ui.bindStyleSheet(tile, lambda: f"QLabel#studioTemplateIconTile {{ background-color: {ui.token('accent-subtle').name()}; border-radius: 10px; }}")
    return tile


def _thumbnailLabel(size, objectName="studioTemplateThumbnail"):
    label = qt.QLabel()
    label.setObjectName(objectName)
    label.setFixedSize(size, size)
    label.setAlignment(qt.Qt.AlignCenter)
    label.setStyleSheet(f"QLabel#{objectName} {{ background-color: #000000; border-radius: 6px; }}")
    return label


def _lastDirectory():
    value = qt.QSettings().value(LAST_DIRECTORY_KEY)
    return str(value) if value and os.path.isdir(str(value)) else os.path.expanduser("~")


def _rememberDirectory(path):
    if path:
        qt.QSettings().setValue(LAST_DIRECTORY_KEY, os.path.dirname(path))


def _setThumbnailAsync(label, template=None, path=None, size=ROW_THUMBNAIL):
    """Show the mid-axial slice of a template (or an image file) in ``label``, computed in the background writer."""
    if template is not None:
        cached = templates.thumbnailPixmap(template, size, compute=False)
        if cached is not None:
            label.setPixmap(cached)
            return
        path = template.imagePath
    label.setPixmap(ui.pixmap("brain", int(size * 0.5), "text3"))
    if not path or not os.path.isfile(path):
        return
    try:
        from . import autosave_io

        executor = autosave_io.backgroundWriter()
    except Exception:
        return

    def done(array):
        if array is None or not _isAlive(label):
            return
        image = templates.arrayToImage(array)
        if template is not None and template.folder:
            try:
                image.save(os.path.join(template.folder, templates.THUMBNAIL_FILE), "PNG")
            except Exception:
                pass
        pixmap = qt.QPixmap.fromImage(image.scaled(size, size, qt.Qt.KeepAspectRatio, qt.Qt.SmoothTransformation))
        # Kept in memory too: the rows of the built-in templates (no folder) are not recomputed on each list refresh
        templates.rememberThumbnail(template, size, pixmap)
        label.setPixmap(pixmap)

    def failed(error):
        logging.debug(f"Slicer Studio: no thumbnail for {path}: {error}")

    executor.submit(templates.thumbnailArray, path, onDone=done, onError=failed)


#
# Add dialog
#


def _companionRole(path):
    """"mask" or "head" when a file name says so ("tpl_brain_mask.nii.gz", "MNI152_T1_1mm_head.nii.gz"), else None."""
    words = set(re.split(r"[^a-z0-9]+", os.path.basename(path).lower()))
    return "mask" if words & {"mask", "brainmask"} else "head" if words & {"head", "skull"} else None


class _DropFilter(qt.QObject):
    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.DragEnter and event.mimeData().hasUrls():
            event.acceptProposedAction()
            return True
        if event.type() == qt.QEvent.Drop and event.mimeData().hasUrls():
            paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
            event.acceptProposedAction()
            if paths:
                self.callback(paths)
            return True
        return False


class _OptionalRow:
    """A 40 px row of the "Optional" list: icon, title, hint, the chosen files and an Add… / x button."""

    def __init__(self, objectName, iconName, title, hint, buttonText, onAdd, onClear):
        self.widget = qt.QWidget()
        self.widget.setObjectName(objectName)
        self.widget.setMinimumHeight(40)
        layout = qt.QHBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        icon = qt.QLabel()
        icon.setPixmap(ui.pixmap(iconName, 16, "text2"))
        layout.addWidget(icon)
        self.titleLabel = _label(f"{title}", objectName + "Title", 13)
        layout.addWidget(self.titleLabel)
        self.hintLabel = _label(hint, objectName + "Hint", 12, color="text3")
        layout.addWidget(self.hintLabel)
        layout.addStretch(1)
        self.valueLabel = _label("", objectName + "Value", 12, color="text2")
        self.valueLabel.setMaximumWidth(VALUE_WIDTH)
        self.valueLabel.hide()
        layout.addWidget(self.valueLabel)
        self.clearButton = qt.QToolButton()
        self.clearButton.setObjectName(objectName + "Clear")
        self.clearButton.setIcon(ui.icon("x", 14))
        self.clearButton.setToolTip(_("Remove"))
        ui.setRole(self.clearButton, "ghost")
        self.clearButton.connect("clicked()", onClear)
        self.clearButton.hide()
        layout.addWidget(self.clearButton)
        self.addButton = _button(buttonText, objectName + "Add", onAdd)
        layout.addWidget(self.addButton)

    def setValue(self, text):
        metrics = qt.QFontMetrics(self.valueLabel.font)
        self.valueLabel.setText(metrics.elidedText(text or "", qt.Qt.ElideMiddle, VALUE_WIDTH) if text else "")
        self.valueLabel.setToolTip(text or "")
        self.valueLabel.setVisible(bool(text))
        self.hintLabel.setVisible(not text)
        self.clearButton.setVisible(bool(text))


class AddTemplateDialog(qt.QDialog):
    """"Add your own MNI template" (``studioAddTemplateDialog``, VISUAL.md 4.7b).

    ``requiredSha256``: re-adding the template of a study (only the file with this checksum is accepted);
    ``expectedName`` and ``expectedSpace`` prefill the form. ``executor``: the background writer (tests pass an inline one).
    """

    def __init__(self, parent=None, imagePath=None, requiredSha256=None, expectedName=None, expectedSpace=None, executor=None, onAdded=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.setObjectName(DIALOG_OBJECT_NAME)
        self.setWindowTitle(_("Add your own MNI template"))
        self.setModal(True)
        self.setFixedWidth(DIALOG_WIDTH)
        self.setAcceptDrops(True)
        self.requiredSha256 = requiredSha256
        self.expectedName = expectedName
        self.expectedSpace = expectedSpace
        self.addButton = None
        self.executor = executor
        self.onAdded = onAdded
        self.imagePath = None
        self.detection = None
        self.maskPath = None
        self.headPath = None
        self.surfacePaths = []
        self.notes = ""
        self.plan = None
        self.template = None
        self._nameEdited = False

        layout = qt.QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(14)

        header = qt.QHBoxLayout()
        header.setSpacing(14)
        header.addWidget(_iconTile("brain"), 0, qt.Qt.AlignTop)
        headerText = qt.QVBoxLayout()
        headerText.setSpacing(4)
        self.titleLabel = _label(_("Add your own MNI template") if not requiredSha256 else _("Add the study's template"), "studioAddTemplateTitle", 18,
                                 qt.QFont.DemiBold)
        headerText.addWidget(self.titleLabel)
        body = (_("Use any brain image that is already in MNI space. It is copied into your Slicer Studio folder; the original file is not changed.")
                if not requiredSha256 else
                _("This study uses the template {name}. Choose the same file: it is checked by its checksum before it is added.").format(
                    name=expectedName or _("of the study")))
        self.bodyLabel = _label(body, "studioAddTemplateBody", 13, color="text2", wrap=True)
        headerText.addWidget(self.bodyLabel)
        header.addLayout(headerText, 1)
        layout.addLayout(header)

        # File card
        self.fileCard = qt.QFrame()
        self.fileCard.setObjectName("studioAddTemplateFileCard")
        ui.setRole(self.fileCard, "card")
        cardLayout = qt.QHBoxLayout(self.fileCard)
        cardLayout.setContentsMargins(12, 12, 12, 12)
        cardLayout.setSpacing(14)
        self.thumbnail = _thumbnailLabel(CARD_THUMBNAIL, "studioAddTemplateThumbnail")
        cardLayout.addWidget(self.thumbnail, 0, qt.Qt.AlignTop)
        details = qt.QVBoxLayout()
        details.setSpacing(6)
        fileRow = qt.QHBoxLayout()
        fileRow.setSpacing(8)
        self.fileLabel = _label(_("No image chosen"), "studioAddTemplateFileName", 13, qt.QFont.Medium)
        fileRow.addWidget(self.fileLabel)
        self.changeButton = qt.QPushButton(_("Choose…"))
        self.changeButton.setObjectName("studioAddTemplateChange")
        self.changeButton.setFlat(True)
        self.changeButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.changeButton.connect("clicked()", self.chooseImage)
        ui.bindStyleSheet(self.changeButton, lambda: f"QPushButton#studioAddTemplateChange {{ color: {ui.token('accent-fg').name()}; border: none; background: transparent; padding: 0px; }}")
        fileRow.addWidget(self.changeButton)
        fileRow.addStretch(1)
        details.addLayout(fileRow)
        self.detectionLabel = _label(_("Drop a NIfTI or NRRD file here, or choose one."), "studioAddTemplateDetection", 13, color="text2", wrap=True)
        details.addWidget(self.detectionLabel)
        self.factsLabel = _label("", "studioAddTemplateFacts", 12, color="text2", wrap=True)
        self.factsLabel.hide()
        details.addWidget(self.factsLabel)
        self.warningLabel = _label("", "studioAddTemplateWarnings", 12, color="warning", wrap=True)
        self.warningLabel.hide()
        details.addWidget(self.warningLabel)
        cardLayout.addLayout(details, 1)
        layout.addWidget(self.fileCard)

        # Form
        form = qt.QGridLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(10)
        form.setColumnMinimumWidth(0, 92)
        form.addWidget(_label(_("Name"), "studioAddTemplateNameLabel", 13, color="text2"), 0, 0)
        self.nameEdit = qt.QLineEdit()
        self.nameEdit.setObjectName("studioAddTemplateName")
        self.nameEdit.setPlaceholderText(_("A name you will recognize, for example ICBM 2009b 0.5 mm"))
        self.nameEdit.connect("textEdited(QString)", self._onNameEdited)
        form.addWidget(self.nameEdit, 0, 1)
        form.addWidget(_label(_("Space"), "studioAddTemplateSpaceLabel", 13, color="text2"), 1, 0)
        spaceRow = qt.QHBoxLayout()
        spaceRow.setSpacing(8)
        self.spaceCombo = qt.QComboBox()
        self.spaceCombo.setObjectName("studioAddTemplateSpace")
        for spaceId, label in templates.SPACES:
            self.spaceCombo.addItem(label, spaceId)
        self.spaceCombo.setCurrentIndex(self.spaceCombo.findData(templates.OTHER_SPACE))
        self.spaceCombo.connect("currentIndexChanged(int)", self._update)
        spaceRow.addWidget(self.spaceCombo, 1)
        self.spaceLabelEdit = qt.QLineEdit()
        self.spaceLabelEdit.setObjectName("studioAddTemplateSpaceName")
        self.spaceLabelEdit.setPlaceholderText(_("Your label (optional)"))
        self.spaceLabelEdit.setMaximumWidth(180)
        spaceRow.addWidget(self.spaceLabelEdit)
        form.addLayout(spaceRow, 1, 1)
        form.addWidget(_label(_("Contrast"), "studioAddTemplateContrastLabel", 13, color="text2"), 2, 0)
        self.contrastFrame, self.contrastGroup = ui.segmented([label for _value, label in CONTRAST_LABELS])
        self.contrastFrame.setObjectName("studioAddTemplateContrast")
        self.contrastGroup.button(0).setChecked(True)
        form.addWidget(self.contrastFrame, 2, 1, qt.Qt.AlignLeft)
        form.setColumnStretch(1, 1)
        layout.addLayout(form)
        if expectedName:
            self.nameEdit.setText(expectedName)
            self._nameEdited = True
        if expectedSpace and self.spaceCombo.findData(expectedSpace) >= 0:
            self.spaceCombo.setCurrentIndex(self.spaceCombo.findData(expectedSpace))

        # Optional
        separator = qt.QFrame()
        separator.setObjectName("studioAddTemplateSeparator")
        separator.setFixedHeight(1)
        ui.bindStyleSheet(separator, lambda: f"QFrame#studioAddTemplateSeparator {{ background-color: {ui.token('hairline').name()}; border: none; }}")
        layout.addWidget(separator)
        layout.addWidget(_label(_("Optional"), "studioAddTemplateOptionalTitle", 12, qt.QFont.DemiBold, color="text2"))
        optional = qt.QVBoxLayout()
        optional.setSpacing(0)
        self.maskRow = _OptionalRow("studioAddTemplateMask", "circle-dashed", _("Brain mask"), _("needed to normalize scans to this template"),
                                    _("Add…"), lambda: self.chooseCompanion("mask"), lambda: self.setCompanion("mask", None))
        self.headRow = _OptionalRow("studioAddTemplateHead", "scan-eye", _("Whole-head image"), _("helps normalize tilted scans"),
                                    _("Add…"), lambda: self.chooseCompanion("head"), lambda: self.setCompanion("head", None))
        self.surfacesRow = _OptionalRow("studioAddTemplateSurfaces", "box", _("3D brain surfaces"), _(".gii .vtk .obj .stl, shown in the 3D view"),
                                        _("Add…"), self.chooseSurfaces, lambda: self.setSurfaces([]))
        self.notesRow = _OptionalRow("studioAddTemplateNotes", "list", _("Source and licence"), _("so others can cite it"),
                                     _("Add note…"), self.editNotes, lambda: self.setNotes(""))
        for row in (self.maskRow, self.headRow, self.surfacesRow, self.notesRow):
            optional.addWidget(row.widget)
        layout.addLayout(optional)

        self.errorLabel = _label("", "studioAddTemplateError", 12, color="danger", wrap=True)
        self.errorLabel.hide()
        layout.addWidget(self.errorLabel)
        self.progressBar = qt.QProgressBar()
        self.progressBar.setObjectName("studioAddTemplateProgress")
        self.progressBar.setRange(0, 1000)
        self.progressBar.setTextVisible(False)
        self.progressBar.setFixedHeight(6)
        self.progressBar.hide()
        layout.addWidget(self.progressBar)

        footer = qt.QHBoxLayout()
        footer.setSpacing(8)
        # A switch (VISUAL 4.7b), not a check box
        self.defaultCheck = ui.switch(_("Use as my default template"), "studioAddTemplateDefault")
        self.defaultCheck.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Fixed)
        self.defaultCheck.setToolTip(_("Start on template then opens this template"))
        footer.addWidget(self.defaultCheck)
        footer.addStretch(1)
        self.cancelButton = _button(_("Cancel"), "studioAddTemplateCancel", self.reject)
        footer.addWidget(self.cancelButton)
        self.addButton = _button(_("Add template"), "studioAddTemplateAddButton", self.addTemplate, role="primary")
        self.addButton.setDefault(True)
        footer.addWidget(self.addButton)
        layout.addLayout(footer)

        self._dropFilter = _DropFilter(self._onDropped)
        self.installEventFilter(self._dropFilter)
        self._progressTimer = qt.QTimer(self)
        self._progressTimer.setInterval(100)
        self._progressTimer.connect("timeout()", self._showProgress)
        # Dialog chrome (VISUAL 4.7, V-02): frameless, rounded, bordered, over a backdrop
        ui.dialogChrome(self)
        if imagePath:
            self.setImage(imagePath)
        self._update()

    # Files

    def _onDropped(self, paths):
        """Dropped files: the template image, then a brain mask or head image when its file name says so ("..._mask",
        "..._head"; never guessed otherwise), and 3D surfaces.
        """
        surfaces = [path for path in paths if templates.isSurfaceFile(path)]
        roles = {path: _companionRole(path) for path in paths if templates.isImageFile(path)}
        images = sorted(roles, key=lambda path: roles[path] is not None)  # the template image first
        if images and self.imagePath is None:
            self.setImage(images.pop(0))
        for path in images:
            if roles[path] == "mask" and self.maskPath is None:
                self.setCompanion("mask", path)
            elif roles[path] == "head" and self.headPath is None:
                self.setCompanion("head", path)
            else:
                message(_("{name} was not added: choose a brain mask or head image with its own Choose… button").format(name=os.path.basename(path)))
        if surfaces:
            self.setSurfaces(self.surfacePaths + surfaces)

    def chooseImage(self):
        path = qt.QFileDialog.getOpenFileName(self, _("Choose the template image"), _lastDirectory(), IMAGE_FILTER)
        if path:
            _rememberDirectory(path)
            self.setImage(path)

    def setImage(self, path):
        """Check an image (header only) and show what was detected."""
        self.imagePath = path
        self.detection = templates.identify(path)
        _setElidedText(self.fileLabel, os.path.basename(path), FILE_NAME_WIDTH, qt.Qt.ElideMiddle)
        self.fileLabel.setToolTip(path)
        self.changeButton.setText(_("Change…"))
        detection = self.detection
        if detection.ok:
            self.detectionLabel.setText("✓ " + detection.message)
            ui.bindStyleSheet(self.detectionLabel, lambda: f"QLabel {{ color: {ui.token('success').name()}; background: transparent; border: none; }}")
        else:
            self.detectionLabel.setText(detection.message)
            ui.bindStyleSheet(self.detectionLabel, lambda: f"QLabel {{ color: {ui.token('danger').name()}; background: transparent; border: none; }}")
        self.factsLabel.setText(detection.facts)
        self.factsLabel.setVisible(bool(detection.facts))
        lines = list(detection.warnings) + ([detection.privacyNote] if detection.privacyNote else [])
        self.warningLabel.setText("\n".join(lines))
        self.warningLabel.setVisible(bool(lines))
        self._fitWrappedLabels()
        if detection.ok:
            if not self._nameEdited:
                self.nameEdit.setText(templates.proposedName(path))
            # A known grid names its space; otherwise the space the study recorded (re-adding its template), else "MNI (other)"
            guess = detection.spaceGuess or self.expectedSpace or templates.OTHER_SPACE
            if self.spaceCombo.findData(guess) >= 0:
                self.spaceCombo.setCurrentIndex(self.spaceCombo.findData(guess))
            _setThumbnailAsync(self.thumbnail, path=path, size=CARD_THUMBNAIL)
        else:
            self.thumbnail.setPixmap(ui.pixmap("circle-alert", 28, "danger"))
        # A mask or head of another grid is dropped when the image changes
        for role in ("mask", "head"):
            current = getattr(self, role + "Path")
            if current and not (detection.ok and templates.checkCompanion(detection, current)[0]):
                self.setCompanion(role, None)
        self._setError("")
        self._update()

    def chooseCompanion(self, role):
        title = _("Choose the brain mask") if role == "mask" else _("Choose the whole-head image")
        path = qt.QFileDialog.getOpenFileName(self, title, _lastDirectory(), IMAGE_FILTER)
        if path:
            _rememberDirectory(path)
            self.setCompanion(role, path)

    def setCompanion(self, role, path):
        """Set the brain mask or head image (None clears it); a file of another grid is refused with a message."""
        row = self.maskRow if role == "mask" else self.headRow
        if path:
            ok, reason = templates.checkCompanion(self.detection, path)
            if not ok:
                self._setError(reason)
                return False
        setattr(self, role + "Path", path or None)
        row.setValue(os.path.basename(path) if path else "")
        self._setError("")
        self._update()
        return True

    def chooseSurfaces(self):
        paths = qt.QFileDialog.getOpenFileNames(self, _("Choose 3D brain surfaces"), _lastDirectory(), SURFACE_FILTER)
        if paths:
            _rememberDirectory(paths[0])
            self.setSurfaces(self.surfacePaths + list(paths))

    def setSurfaces(self, paths):
        """Set the 3D brain surfaces; the ones that are not in MNI space (or cannot be read) are refused with a message."""
        accepted, reasons = [], []
        for path in paths:
            if path in accepted:
                continue
            ok, reason = templates.checkSurface(path)
            if ok:
                accepted.append(path)
            else:
                reasons.append(reason)
        self.surfacePaths = accepted
        self.surfacesRow.setValue(", ".join(os.path.basename(path) for path in accepted))
        self._setError(" ".join(reasons))
        self._update()

    def editNotes(self):
        text = qt.QInputDialog.getMultiLineText(self, _("Source and licence"), _("Where the template comes from, how to cite it and its licence:"), self.notes)
        if isinstance(text, tuple):
            text, ok = text[0], text[1]
            if not ok:
                return
        if text is not None:
            self.setNotes(str(text))

    def setNotes(self, text):
        self.notes = (text or "").strip()
        first = self.notes.splitlines()[0] if self.notes else ""
        self.notesRow.setValue(first[:40] + ("…" if len(first) > 40 else ""))

    # Form

    def _onNameEdited(self, text):
        self._nameEdited = True
        self._update()

    def contrast(self):
        index = self.contrastGroup.checkedId()
        return CONTRAST_LABELS[index][0] if 0 <= index < len(CONTRAST_LABELS) else "T1w"

    def setContrast(self, value):
        for index, (contrast, _label) in enumerate(CONTRAST_LABELS):
            if contrast == value:
                self.contrastGroup.button(index).setChecked(True)

    def space(self):
        return self.spaceCombo.currentData if not callable(self.spaceCombo.currentData) else self.spaceCombo.currentData()

    def _setError(self, text):
        self.errorLabel.setText(text or "")
        self.errorLabel.setVisible(bool(text))
        self._fitWrappedLabels()

    def _fitWrappedLabels(self):
        """Reserve the full height of the word-wrapped labels.

        The dialog has a fixed width, but a layout works out its height from the width a wrapped label would take
        when it is not wrapped: the dialog is then too short and the last lines (the privacy note) are cut. The width
        each label gets is known here, so its height for that width becomes its minimum height.
        """
        margins = self.layout().contentsMargins()
        cardMargins = self.fileCard.layout().contentsMargins()
        cardWidth = DIALOG_WIDTH - margins.left() - margins.right()
        detailsWidth = cardWidth - cardMargins.left() - cardMargins.right() - self.fileCard.layout().spacing - CARD_THUMBNAIL
        for label, width in ((self.detectionLabel, detailsWidth), (self.factsLabel, detailsWidth), (self.warningLabel, detailsWidth),
                             (self.errorLabel, cardWidth)):
            label.setMinimumHeight(label.heightForWidth(width) if label.text else 0)
        self.layout().invalidate()
        self.layout().activate()
        if self.isVisible():
            self.resize(self.width, max(self.height, self.sizeHint.height()))

    def _update(self, *args):
        if getattr(self, "addButton", None) is None:
            return  # called while the dialog is built
        other = self.space() == templates.OTHER_SPACE
        self.spaceLabelEdit.setVisible(other)
        name = str(self.nameEdit.text).strip()
        ready = self.detection is not None and self.detection.ok and bool(name) and self.plan is None
        self.addButton.setEnabled(ready)
        for widget in (self.nameEdit, self.spaceCombo, self.contrastFrame, self.changeButton):
            widget.setEnabled(self.plan is None)

    # Adding

    def addTemplate(self):
        """Check everything, then copy the files in the background (progress bar); the dialog closes when the template is added."""
        if self.plan is not None:
            return
        name = str(self.nameEdit.text).strip()
        spaceLabel = str(self.spaceLabelEdit.text).strip() or None
        try:
            plan = templates.prepareAdd(self.imagePath, mask=self.maskPath, head=self.headPath, surfaces=self.surfacePaths, name=name, space=self.space(),
                                        contrast=self.contrast(), notes=self.notes, spaceLabel=spaceLabel, makeDefault=self.defaultCheck.checked,
                                        requiredSha256=self.requiredSha256)
        except templates.TemplateError as error:
            self._setError(str(error))
            return
        except Exception as error:
            logging.error(f"Slicer Studio: the template could not be checked:\n{traceback.format_exc()}")
            self._setError(str(error))
            return
        self.plan = plan
        self._setError("")
        self.progressBar.setValue(0)
        self.progressBar.show()
        self.addButton.setText(_("Adding…"))
        self._update()
        executor = self.executor
        if executor is None:
            from . import autosave_io

            executor = autosave_io.backgroundWriter()
        self._progressTimer.start()
        executor.submit(templates.copyFiles, plan, onDone=self._onCopied, onError=self._onCopyFailed)

    def _showProgress(self):
        if self.plan is not None and self.plan.totalBytes:
            self.progressBar.setValue(int(1000 * min(1.0, self.plan.progress / float(self.plan.totalBytes))))

    def _onCopied(self, record):
        self._progressTimer.stop()
        plan, self.plan = self.plan, None
        if plan is None:
            return
        try:
            self.template = templates.finishAdd(plan, record)
        except Exception as error:
            self._onCopyFailed(error)
            return
        message(_("Added the template {name}").format(name=self.template.name))
        if self.onAdded is not None:
            try:
                self.onAdded(self.template)
            except Exception:
                logging.error(f"Slicer Studio: after adding a template:\n{traceback.format_exc()}")
        if _isAlive(self):
            self.accept()

    def _onCopyFailed(self, error):
        self._progressTimer.stop()
        self.plan = None
        if not _isAlive(self):
            return
        self.progressBar.hide()
        self.addButton.setText(_("Add template"))
        text = str(error) if isinstance(error, templates.TemplateError) else _("The template could not be added: {error}").format(error=error)
        if not isinstance(error, templates.TemplateError):
            logging.error(f"Slicer Studio: adding a template failed: {error}")
        self._setError(text)
        self._update()

    def reject(self):
        if self.plan is not None:
            # Cancel the copy: the staging folder is removed and nothing is added
            self.plan.cancelled = True
        qt.QDialog.reject(self)


def showAddDialog(parent=None, imagePath=None, requiredSha256=None, expectedName=None, expectedSpace=None, onAdded=None, modal=True):
    """Open "Add your own MNI template"; return the dialog (non-blocking when ``modal`` is False, as in tests)."""
    global _dialog
    dialog = AddTemplateDialog(parent, imagePath=imagePath, requiredSha256=requiredSha256, expectedName=expectedName, expectedSpace=expectedSpace,
                               onAdded=onAdded)
    _dialog = dialog
    if modal and not slicer.app.testingEnabled():
        run = getattr(dialog, "exec", None) or dialog.exec_
        run()
    else:
        dialog.show()
    return dialog


#
# Settings > Templates
#


class _ShowEventFilter(qt.QObject):
    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            self.callback()
        return False


class TemplatesSettingsPanel(ctk.ctkSettingsPanel):
    """Settings > Templates (``studioTemplatesPanel``): every template, the default, Add your own…, Show in Finder."""

    def __init__(self, *args, **kwargs):
        ctk.ctkSettingsPanel.__init__(self, *args, **kwargs)
        self.setObjectName(PANEL_OBJECT_NAME)
        self.rows = []
        layout = qt.QVBoxLayout(self)
        # The margins of the other Studio settings pages (VISUAL 4.7)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(14)
        header = qt.QHBoxLayout()
        titles = qt.QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(_label(_("Templates"), "studioTemplatesTitle", 18, qt.QFont.DemiBold))
        titles.addWidget(_label(_("MNI brains you can draw on, map from and normalize to."), "studioTemplatesDescription", 13, color="text2", wrap=True))
        header.addLayout(titles, 1)
        self.addButton = _button(_("+ Add your own…"), "studioTemplatesAddButton", self.add)
        header.addWidget(self.addButton, 0, qt.Qt.AlignTop)
        layout.addLayout(header)

        self.listFrame = qt.QFrame()
        self.listFrame.setObjectName("studioTemplatesList")
        self.listLayout = qt.QVBoxLayout(self.listFrame)
        self.listLayout.setContentsMargins(0, 0, 0, 0)
        self.listLayout.setSpacing(0)
        layout.addWidget(self.listFrame)

        self.notice = qt.QFrame()
        self.notice.setObjectName("studioTemplatesFamilyNotice")
        noticeLayout = qt.QHBoxLayout(self.notice)
        noticeLayout.setContentsMargins(12, 10, 12, 10)
        noticeLayout.setSpacing(8)
        infoIcon = qt.QLabel()
        infoIcon.setPixmap(ui.pixmap("circle-help", 16, "accent-fg"))
        noticeLayout.addWidget(infoIcon, 0, qt.Qt.AlignTop)
        noticeLayout.addWidget(_label(_("Lesions drawn on a template in another MNI family (for example FSL's 6Asym and 2009c) still map, with a note: "
                                        "the families are a few millimetres apart. \"MNI (other)\" templates map with the same note."),
                                      "studioTemplatesFamilyText", 12, wrap=True), 1)
        layout.addWidget(self.notice)

        footer = qt.QHBoxLayout()
        footer.setSpacing(6)
        folderIcon = qt.QLabel()
        folderIcon.setPixmap(ui.pixmap("folder-open", 14, "text3"))
        footer.addWidget(folderIcon)
        self.folderLabel = _label("", "studioTemplatesFolder", 12, color="text3")
        footer.addWidget(self.folderLabel)
        self.showFolderButton = qt.QPushButton()
        self.showFolderButton.setObjectName("studioTemplatesShowFolder")
        self.showFolderButton.setFlat(True)
        self.showFolderButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.showFolderButton.connect("clicked()", self.showFolder)
        ui.bindStyleSheet(self.showFolderButton, lambda: f"QPushButton#studioTemplatesShowFolder {{ color: {ui.token('accent-fg').name()}; border: none; background: transparent; padding: 0px; }}")
        footer.addWidget(self.showFolderButton)
        footer.addStretch(1)
        layout.addLayout(footer)
        layout.addStretch(1)

        ui.bindStyleSheet(self, self._styleSheet)
        self._showFilter = _ShowEventFilter(self.refresh)
        self.installEventFilter(self._showFilter)
        templates.addListener(self._onTemplatesChanged)
        # The rows are built when the panel is shown (listing the templates reads the disk)

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("border", "hairline", "card", "accent-subtle", "accent-fg", "text", "text2")}
        return (f"QFrame#studioTemplatesList {{ border: 1px solid {t['border']}; border-radius: 10px; background: transparent; }}"
                f"QFrame#studioTemplateRow {{ border: none; border-bottom: 1px solid {t['hairline']}; background: transparent; }}"
                f"QFrame#studioTemplateRow[last=\"true\"] {{ border-bottom: none; }}"
                f"QFrame#studioTemplateRow[isDefault=\"true\"] {{ background-color: {t['card']}; }}"
                f"QFrame#studioTemplatesFamilyNotice {{ background-color: {t['accent-subtle']}; border: 1px solid {t['border']}; border-radius: 8px; }}"
                f"QLabel#studioTemplatesFamilyText {{ color: {t['text']}; background: transparent; border: none; }}"
                f"QLabel#studioTemplateDefaultLabel {{ color: {t['accent-fg']}; background: transparent; border: none; }}")

    def _onTemplatesChanged(self):
        if _isAlive(self) and self.isVisible():
            self.refresh()

    def refresh(self):
        """Rebuild the rows (the default, the thumbnails, the folder line)."""
        while self.listLayout.count():
            item = self.listLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.rows = []
        defaultId = templates.defaultTemplateId()
        entries = templates.listTemplates()
        for index, template in enumerate(entries):
            row = self._row(template, template.id == defaultId, index == len(entries) - 1)
            self.listLayout.addWidget(row)
            self.rows.append(row)
        from . import projects

        folder = templates.templatesFolder()
        parts = [part for part in projects.shortPath(os.path.dirname(folder)).replace("\\", "/").split("/") if part]
        shown = " › ".join(parts[-2:] + [templates.TEMPLATES_DIR])  # noqa: RUF001
        self.folderLabel.setText(_("Stored in {folder}").format(folder=shown))
        self.folderLabel.setToolTip(folder)
        self.showFolderButton.setText(_("Show in {manager}").format(manager=projects.fileManagerName()))
        self.showFolderButton.setEnabled(os.path.isdir(folder))

    def _row(self, template, isDefault, last):
        row = qt.QFrame()
        row.setObjectName("studioTemplateRow")
        row.setProperty("templateId", template.id)
        row.setProperty("isDefault", "true" if isDefault else "false")
        row.setProperty("last", "true" if last else "false")
        layout = qt.QHBoxLayout(row)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        thumbnail = _thumbnailLabel(ROW_THUMBNAIL)
        _setThumbnailAsync(thumbnail, template=template, size=ROW_THUMBNAIL)
        layout.addWidget(thumbnail)
        texts = qt.QVBoxLayout()
        texts.setSpacing(2)
        nameRow = qt.QHBoxLayout()
        nameRow.setSpacing(8)
        nameLabel = _label("", "studioTemplateName", 13, qt.QFont.Medium)
        _setElidedText(nameLabel, template.name, NAME_WIDTH)
        nameRow.addWidget(nameLabel)
        chip = ui.chip(_("Built-in") if template.builtin else _("Yours"), "neutral" if template.builtin else "busy")
        chip.setObjectName("studioTemplateKindChip")
        nameRow.addWidget(chip)
        if template.surfacePaths:
            surfaces = ui.chip(_("{count} surfaces").format(count=len(template.surfacePaths)) if len(template.surfacePaths) != 1 else _("1 surface"), "neutral")
            surfaces.setObjectName("studioTemplateSurfacesChip")
            nameRow.addWidget(surfaces)
        nameRow.addStretch(1)
        texts.addLayout(nameRow)
        meta = _label(template.metaLine(), "studioTemplateMeta", 12, color="text2")
        if template.notes:
            meta.setToolTip(template.notes)
        texts.addWidget(meta)
        layout.addLayout(texts, 1)
        if isDefault:
            defaultLabel = qt.QLabel("✓ " + _("Default"))
            defaultLabel.setObjectName("studioTemplateDefaultLabel")
            ui._font(defaultLabel, ui.fontPx(13), qt.QFont.Medium)
            layout.addWidget(defaultLabel)
        else:
            setDefault = _button(_("Set as default"), "studioTemplateSetDefault", lambda templateId=template.id: self.setDefault(templateId), role="ghost")
            layout.addWidget(setDefault)
        more = qt.QToolButton()
        more.setObjectName("studioTemplateMoreButton")
        more.setIcon(ui.icon("ellipsis", 16))
        more.setToolTip(_("More"))
        more.setPopupMode(qt.QToolButton.InstantPopup)
        ui.setRole(more, "ghost")
        menu = qt.QMenu(more)
        menu.setObjectName("studioTemplateRowMenu")
        startAction = menu.addAction(_("Start on this template"))
        startAction.connect("triggered()", lambda templateId=template.id: startOn(templateId))
        if template.isUser:
            showAction = menu.addAction(_("Show in {manager}").format(manager=_fileManagerName()))
            showAction.connect("triggered()", lambda folder=template.folder: _showFolder(folder))
            menu.addSeparator()
            removeAction = menu.addAction(_("Remove…"))
            removeAction.setObjectName("studioTemplateRemoveAction")
            removeAction.connect("triggered()", lambda templateId=template.id: self.remove(templateId))
        more.setMenu(menu)
        layout.addWidget(more)
        return row

    def rowFor(self, templateId):
        for row in self.rows:
            if row.property("templateId") == templateId:
                return row
        return None

    def add(self):
        showAddDialog(self.window(), onAdded=lambda template: self.refresh())

    def setDefault(self, templateId):
        templates.setDefault(templateId)
        self.refresh()

    def remove(self, templateId, confirm=True):
        template = templates.templateById(templateId)
        if template is None or template.builtin:
            return False
        if confirm and not slicer.app.testingEnabled():
            question = _("Remove the template {name}? Its folder goes to the Trash; drawings and studies that use it keep their files.").format(
                name=template.name)
            if not slicer.util.confirmOkCancelDisplay(question, windowTitle=_("Remove template")):
                return False
        try:
            removed = templates.removeTemplate(templateId, confirmDelete=lambda: slicer.app.testingEnabled() or slicer.util.confirmOkCancelDisplay(
                _("There is no Trash here: delete the template folder for good?"), windowTitle=_("Remove template")))
        except Exception as error:
            slicer.util.errorDisplay(str(error)) if not slicer.app.testingEnabled() else logging.error(str(error))
            return False
        self.refresh()
        if removed:
            message(_("Removed the template {name}").format(name=template.name))
        return removed

    def showFolder(self):
        _showFolder(templates.templatesFolder())


def _fileManagerName():
    from . import projects

    return projects.fileManagerName()


def _showFolder(folder):
    from . import projects

    projects.showInFileManager(folder)


def installSettingsPanel(force=False):
    """Add Settings > Templates (idempotent; never under --testing unless ``force``)."""
    global _panel
    if slicer.app.testingEnabled() and not force:
        return None
    dialog = slicer.app.settingsDialog() if hasattr(slicer.app, "settingsDialog") else None
    if dialog is None:
        return None
    existing = dialog.findChild(qt.QWidget, PANEL_OBJECT_NAME)
    if existing is not None:
        return existing
    _panel = TemplatesSettingsPanel()
    dialog.addPanel(_(PANEL_NAME), _panel)
    return _panel


def panel():
    return _panel


def showManage():
    """Open the application settings at Templates."""
    installSettingsPanel(force=True)
    dialog = slicer.app.settingsDialog()
    mainWindow = slicer.util.mainWindow()
    if dialog is None or mainWindow is None:
        return
    dialog.setCurrentPanel(_(PANEL_NAME))
    action = mainWindow.findChild(qt.QAction, "EditApplicationSettingsAction")
    if action is not None:
        action.trigger()


#
# Start on template ▾ (Library) and Search
#


def startOn(templateId=None):
    """Start on a template of the registry (Draw with an empty lesion on it)."""
    from . import importer

    try:
        importer.startOnTemplate(shell=_shell(), templateId=templateId)
    except Exception as error:
        logging.error(f"Slicer Studio: Start on template failed:\n{traceback.format_exc()}")
        if not slicer.app.testingEnabled():
            slicer.util.errorDisplay(_("The template could not be opened.") + f"\n\n{error}")


def populateTemplateMenu(menu, onStart=None):
    """Fill the "Start on template ▾" menu: every template (the default checked), then Add your own template… and Manage
    templates…. ``onStart(templateId)`` starts on one (default ``startOn``).
    """
    menu.clear()
    menu.setObjectName(MENU_OBJECT_NAME)
    onStart = onStart or startOn
    defaultId = templates.defaultTemplateId()
    for template in templates.listTemplates():
        action = menu.addAction(template.name)
        action.setObjectName("studioTemplateMenuItem")
        action.setData(template.id)
        action.setCheckable(True)
        action.setChecked(template.id == defaultId)
        action.setToolTip(template.metaLine())
        action.connect("triggered()", lambda templateId=template.id: onStart(templateId))
    menu.addSeparator()
    addAction = menu.addAction(_("Add your own template…"))
    addAction.setObjectName("studioTemplateMenuAdd")
    addAction.connect("triggered()", showAddDialog)
    manageAction = menu.addAction(_("Manage templates…"))
    manageAction.setObjectName("studioTemplateMenuManage")
    manageAction.connect("triggered()", showManage)
    return menu


def paletteItems(shell=None):
    items = [
        {"id": "templates.add", "title": _("Add MNI template"), "category": _("Action"), "callback": showAddDialog,
         "synonyms": ["add template", "my own template", "own mni brain", "custom template", "mni brain model", "import template", "icbm", "fsl mni152",
                      "new template"],
         "description": _("Add your own MNI-space brain image to draw on, map from and normalize to")},
        {"id": "templates.manage", "title": _("Manage templates"), "category": _("Action"), "callback": showManage,
         "synonyms": ["templates", "default template", "remove template", "template settings", "mni templates"]},
    ]
    for template in templates.userTemplates():
        items.append({"id": f"templates.start.{template.slug}", "title": _("Start on {name}").format(name=template.name), "category": _("Action"),
                      "callback": lambda templateId=template.id: startOn(templateId), "synonyms": ["template", "start on template", template.space.lower()],
                      "description": template.metaLine()})
    return items


def registerPaletteProvider():
    try:
        from . import palette
    except Exception:
        return False
    if not hasattr(palette, "registerProvider"):
        return False
    palette.registerProvider(paletteItems)
    return True


def unregisterPaletteProvider():
    try:
        from . import palette
    except Exception:
        return
    if hasattr(palette, "unregisterProvider"):
        palette.unregisterProvider(paletteItems)


#
# Studies whose template is not on this computer
#


def offerMissingStudyTemplate(record, shell=None, onAdded=None):
    """The notice "This study uses the template <name> (MNI …). Add it…" (Add it… opens the add dialog that accepts only
    the file with the study's checksum). Return the notice widget, or None without a shell.
    """
    from . import importer

    name = record.get("name") or record.get("source") or _("its template")
    spaceText = templates.spaceDisplayName(record.get("space") or record.get("id"))
    text = _("This study uses the template {name} ({space}), which is not on this computer. It opens on the built-in template "
             "until you add it.").format(name=name, space=spaceText)

    def add():
        def added(template):
            importer._withdrawNotice("template-missing", shell)
            if onAdded is not None:
                onAdded(template)

        showAddDialog(requiredSha256=record.get("sha256"), expectedName=record.get("name"), expectedSpace=record.get("space"), onAdded=added)

    frame = importer._noticeFrame("studioTemplateMissingNotice", text, [(_("Add it…"), add), (_("Not now"), lambda: importer._withdrawNotice("template-missing", shell))],
                                  warning=True)
    importer._postNotice("template-missing", frame, shell)
    return frame


#
# Studio plug-in
#


#: Settings > Templates and the grids of your templates come after the Studio apply time (SPEC D11)
DEFERRED_INSTALL_MS = 1500
_installTimer = None


def _deferredInstall():
    try:
        installSettingsPanel()
        templates.ensureGridsRegistered()
    except Exception:
        logging.error(f"Slicer Studio: the templates could not be installed:\n{traceback.format_exc()}")


def install(shell):
    """Studio plug-in entry point (``shell.OPTIONAL_PLUGINS``): Search entries now; Settings > Templates and the grids of
    your templates shortly after.
    """
    global _installTimer
    registerPaletteProvider()
    if _installTimer is None:
        _installTimer = qt.QTimer()
        _installTimer.setSingleShot(True)
        _installTimer.setInterval(DEFERRED_INSTALL_MS)
        _installTimer.connect("timeout()", _deferredInstall)
    _installTimer.start()


def uninstall(shell):
    if _installTimer is not None:
        _installTimer.stop()  # a Studio -> Classic switch within DEFERRED_INSTALL_MS adds no Settings > Templates
    unregisterPaletteProvider()
