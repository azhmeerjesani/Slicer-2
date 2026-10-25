"""Map workspace of Slicer Studio (SPEC 3.3, 7.9, 7.10; USABILITY 14): hooks ``enter(shell)`` / ``leave(shell)``,
``loadMap(path, shell)`` and map browsing.

Map is the Lesion Network Mapping module in the inspector (SPEC D2); these hooks give it the views and the keys:

- **Layers.** ``enter`` saves, for every slice composite node, the background, foreground and label volume IDs and the
  foreground and label opacities (``captureLayers``), then shows the Map layers: background = the template of the
  connectome's space (an MNI152NLin6Asym 2 mm T1w already downloaded through Sample Data for an NLin6Asym connectome,
  else the bundled MNI152NLin2009cAsym 2 mm brain; never a download), foreground = the result (opacity 0.8, the module's
  colour table), no label layer, the lesion as a white outline. ``leave`` puts the saved layers back exactly, also when
  another page (Draw's review views, A-03b) changed them before Map was entered. The module calls ``captureLayers()``
  before it changes a layer, so the layers seen before Map are the ones that come back.
- **Context.** The Draw segment (the current "Lesion" segment of the Segment Editor) becomes the seed of a lesion run,
  and the open study (``shell.studySession``) the cohort of the Cohort seed.
- **Keys.** ``Ctrl+Enter`` is the inspector's primary button (Map network: the preflight checks, then compute; with
  Seed = Cohort the card that registered ``MapContext.setPrimaryOverride('cohort', …)`` runs, A-04c), ``Ctrl+E``
  exports, ``Alt+←`` / ``Alt+→`` show the previous / next map of the browsed set.
- **Status chip.** "No connectome", "Ready", "Done · N=1000", "Done · Preview N=100", set by the module; while the
  shared job runner runs a job it owns the chip ("Mapping 34% · 2 min left") and nothing here overwrites it.

``loadMap(path, shell)`` is how the Library importer (U-07) shows a statistical map opened from Library or dropped: the
map becomes the Result card's map (``Studio.Role`` = "statmap", ``LNM.Result``), with the module's diverging colour
table, a symmetric window and the threshold rule; no lesion segmentation is created and Map is shown.

**Browsing** (USABILITY 14, Q2): the maps of the same folder and kind (``<name>_conn-<label>_desc-T_funcmap.nii.gz``), or
the rows of a cohort's maps.tsv (``browseCohort``), are shown one at a time in the same volume, with the subject's lesion
as a white outline (one hidden segmentation, never saved, removed when Map is left): the scene never holds one volume
per subject. Network maps are never shown in Draw.
"""

import logging
import os
import re
import traceback
import weakref

import qt
import slicer
from slicer.i18n import tr as _

#: Map keys (SPEC Appendix A; they pass the keymap collision test, SPEC 9.6)
EXPORT_SHORTCUT = "Ctrl+E"
PREVIOUS_MAP_SHORTCUT = "Alt+Left"
NEXT_MAP_SHORTCUT = "Alt+Right"
#: Foreground opacity of the map (SPEC 7.10)
MAP_OPACITY = 0.8
#: Node attribute of the browsing lesion (a hidden segmentation that is never saved)
BROWSE_LESION_ATTRIBUTE = "LNM.BrowseLesion"
BROWSE_LESION_NAME = "LNM browsing lesion"
#: Sample Data name of the FSL template shown under maps of an MNI152NLin6Asym connectome (when it was downloaded)
NLIN6_TEMPLATE_SAMPLE = "MNI152NLin6Asym 2mm T1w (FSL terms)"
NLIN6_TEMPLATE_FILE = "tpl-MNI152NLin6Asym_res-02_T1w.nii.gz"

#: Per-lesion engine outputs: <name>_conn-<label>_desc-<desc><suffix>_funcmap.nii.gz
_MAP_NAME = re.compile(r"^(?P<name>.+?)_conn-(?P<label>[A-Za-z0-9]+)_desc-(?P<desc>AvgRFz|AvgR|T)(?P<suffix>[A-Za-z0-9]*)_funcmap\.nii(?:\.gz)?$")
_SUBJECT = re.compile(r"(sub-[A-Za-z0-9]+)")

_state = {"layers": None, "shell": None, "shortcutsShell": None, "takenActions": {}, "overlay": None,
          # Draw's lesion (segmentation node id, segment id) when the Map seed was last chosen or adopted, and the
          # contexts whose seed changes are watched (an explicit seed choice wins over the Draw adoption)
          "drawLesionAtSeedChoice": None, "adopting": False, "seedWatched": []}


#
# The module
#


def moduleWidget(create=True):
    """The Lesion Network Mapping module widget (created when ``create``), or None when the module is not loaded."""
    widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
    if widget is not None or not create:
        return widget
    module = getattr(slicer.modules, "lesionnetworkmapping", None)
    if module is None:
        return None
    return module.widgetRepresentation().self()


def _shellOrNone(shell=None):
    if shell is not None:
        return shell
    try:
        from . import shell as shellModule

        return shellModule.instance()
    except Exception:
        return None


#
# Layers
#


def _compositeNodes():
    return slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode")


def captureLayers():
    """Save the layers of every slice view once (until ``restoreLayers``); return whether they were saved now.

    Only in Studio: the Classic interface keeps its own layers.
    """
    shell = _shellOrNone()
    if _state["layers"] is not None or shell is None or not getattr(shell, "isActive", lambda: False)():
        return False
    _state["layers"] = {node.GetID(): (node.GetBackgroundVolumeID(), node.GetForegroundVolumeID(), node.GetLabelVolumeID(),
                                       node.GetForegroundOpacity(), node.GetLabelOpacity()) for node in _compositeNodes()}
    return True


def layersCaptured():
    return _state["layers"] is not None


def restoreLayers():
    """Put back the layers saved by ``captureLayers`` exactly (a volume removed since is not shown); return whether there were any."""
    saved, _state["layers"] = _state["layers"], None
    if saved is None:
        return False
    scene = slicer.mrmlScene
    for node in _compositeNodes():
        layers = saved.get(node.GetID())
        if layers is None:
            continue
        background, foreground, label, foregroundOpacity, labelOpacity = layers
        present = [volumeId if volumeId and scene.GetNodeByID(volumeId) is not None else None for volumeId in (background, foreground, label)]
        wasModifying = node.StartModify()
        if node.GetBackgroundVolumeID() != present[0]:
            node.SetBackgroundVolumeID(present[0])
        if node.GetForegroundVolumeID() != present[1]:
            node.SetForegroundVolumeID(present[1])
        if node.GetLabelVolumeID() != present[2]:
            node.SetLabelVolumeID(present[2])
        node.SetForegroundOpacity(foregroundOpacity)
        node.SetLabelOpacity(labelOpacity)
        node.EndModify(wasModifying)
    return True


def _spaceFamily(bundle):
    if bundle is None:
        return None
    try:
        from LesionNetworkMappingLib import geometry

        detected = geometry.detect_space((bundle.shape, bundle.affine))
        name = detected[0] if detected else (getattr(bundle, "metadata", None) or {}).get("space")
        return geometry.space_family(name) if name else None
    except Exception:
        return None


def _nodeFamily(node):
    """The space family of a template volume's ``Studio.Space`` attribute, or None."""
    value = node.GetAttribute("Studio.Space") or ""
    if not value:
        return None
    try:
        from LesionNetworkMappingLib import geometry

        return geometry.space_family(value.split(" ")[0])
    except Exception:
        return None


def _defaultUserTemplate(family):
    """The user's own default template (A-10) when it is of ``family``, else None."""
    try:
        from LesionNetworkMappingLib import geometry

        from . import templates

        template = templates.defaultTemplate()
    except Exception:
        return None
    if template is None or not template.isUser or not template.isAvailable():
        return None
    return template if geometry.space_family(template.space) == family else None


def _cachedSample(fileName):
    cache = slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory() if slicer.mrmlScene.GetCacheManager() else ""
    try:
        from . import downloads

        folders = [downloads.cacheDirectory(), cache]
    except Exception:
        folders = [cache]
    for folder in folders:
        path = os.path.join(folder or "", fileName)
        if folder and os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    return None


def templateVolume(bundle=None, load=True):
    """The template shown under maps (module docstring): a template volume of the connectome's space in the scene, the
    downloaded MNI152NLin6Asym 2 mm T1w for an NLin6Asym connectome, else the bundled MNI152NLin2009cAsym 2 mm brain
    (loaded from the application's files when ``load``). Nothing is downloaded. None when there is no template.
    """
    from . import space

    family = _spaceFamily(bundle)
    templates = [node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")
                 if node.GetAttribute(space.ROLE_ATTRIBUTE) == space.ROLE_TEMPLATE and not node.IsA("vtkMRMLLabelMapVolumeNode")]
    if family:
        for node in templates:
            if family.lower() in (node.GetAttribute(space.SPACE_ATTRIBUTE) or "").lower() or _nodeFamily(node) == family:
                return node
        userTemplate = _defaultUserTemplate(family) if load else None
        if userTemplate is not None:
            # The user's own default template (A-10) of the connectome's family
            try:
                from . import importer

                return importer.loadTemplateVolume(userTemplate)
            except Exception:
                logging.error(f"Studio Map: cannot load the template {userTemplate.name}:\n{traceback.format_exc()}")
    if family and "nlin6" in family.lower() and load:
        path = _cachedSample(NLIN6_TEMPLATE_FILE)
        if path is not None:
            try:
                node = slicer.util.loadVolume(path, {"name": "MNI152NLin6Asym_2mm_T1w", "singleFile": True, "show": False})
                node.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_TEMPLATE)
                node.SetAttribute(space.SPACE_ATTRIBUTE, "MNI152NLin6Asym 2")
                return node
            except Exception:
                logging.error(f"Studio Map: cannot load the MNI152NLin6Asym template:\n{traceback.format_exc()}")
    if templates:
        return templates[0]
    if not load:
        return None
    try:
        from . import importer

        return importer.studyTemplateVolume(importer.TEMPLATE_SAMPLE_2MM)
    except Exception:
        logging.error(f"Studio Map: cannot load the MNI template:\n{traceback.format_exc()}")
        return None


#
# Hooks
#


def _drawLesion():
    """Draw's current lesion: ``(segmentationNode, segmentId)`` of the Segment Editor (its "Lesion" segment, else the
    selected one), or (None, None).
    """
    try:
        from . import draw

        editor = draw.segmentEditor()
    except Exception:
        editor = None
    segmentationNode = editor.segmentationNode() if editor is not None else None
    if segmentationNode is None or segmentationNode.GetAttribute(BROWSE_LESION_ATTRIBUTE) == "1":
        return None, None
    segmentation = segmentationNode.GetSegmentation()
    segmentId = segmentation.GetSegmentIdBySegmentName("Lesion") or segmentation.GetSegmentIdBySegmentName("lesion") or editor.currentSegmentID()
    if not segmentId or segmentation.GetSegment(segmentId) is None:
        return segmentationNode, None
    return segmentationNode, segmentId


def _seedKey(segmentationNode, segmentId):
    return (segmentationNode.GetID() if segmentationNode is not None else None, segmentId or None)


def noteSeedChoice():
    """Record that the Map seed was chosen explicitly (Draw's "Use for Map", the Seed card): entering Map then keeps it
    and adopts Draw's lesion only when Draw shows another lesion than at this choice.
    """
    if not _state["adopting"]:
        _state["drawLesionAtSeedChoice"] = _seedKey(*_drawLesion())


def _watchSeed(widget):
    """Seed changes made in Map (or by its cards) count as explicit choices (``noteSeedChoice``)."""
    ctx = getattr(widget, "ctx", None)
    observers = getattr(ctx, "seedChanged", None)
    if observers is None or any(watched() is ctx for watched in _state["seedWatched"]):
        return
    observers.append(noteSeedChoice)
    _state["seedWatched"] = [watched for watched in _state["seedWatched"] if watched() is not None] + [weakref.ref(ctx)]


def _adoptDrawSeed(widget):
    """The current segment of the Segment Editor (Draw) becomes the lesion seed: its "Lesion" segment, else the selected
    one. An explicit seed choice (``noteSeedChoice``) wins: Draw's lesion is adopted only when it changed since then.
    """
    if widget is None or widget.seedMode() != "lesion":
        return
    segmentationNode, segmentId = _drawLesion()
    if segmentationNode is None or not segmentId:
        return
    current = widget.ctx.seed()
    key = _seedKey(segmentationNode, segmentId)
    if current[0] is segmentationNode and current[1] == segmentId:
        _state["drawLesionAtSeedChoice"] = key
        return
    chosen = current[0] is not None and current[0].GetScene() is not None and current[1]
    if chosen and _state["drawLesionAtSeedChoice"] == key:
        return  # Draw still shows the lesion it showed when the seed was chosen: the choice stands
    _state["adopting"] = True
    try:
        widget.ctx.setSeed(segmentationNode, segmentId)
    finally:
        _state["adopting"] = False
    _state["drawLesionAtSeedChoice"] = key
    widget.updateGUIFromParameterNode()


def _installShortcuts(shell):
    if _state["shortcutsShell"] is shell:
        return
    _state["shortcutsShell"] = shell
    shell.addWorkspaceShortcut("map", EXPORT_SHORTCUT, exportCurrent, _("Export the maps"))
    shell.addWorkspaceShortcut("map", PREVIOUS_MAP_SHORTCUT, lambda: browseStep(-1), _("Previous map"))
    shell.addWorkspaceShortcut("map", NEXT_MAP_SHORTCUT, lambda: browseStep(1), _("Next map"))


def _takeActionShortcuts(shell):
    """Alt+←/→ browse maps in Map: the module navigation actions (QKeySequence Back/Forward, Alt+←/→ on Windows and
    Linux) keep only their other keys meanwhile, as in Draw, so that no key is ambiguous.
    """
    import qt

    taken = {qt.QKeySequence(key).toString(qt.QKeySequence.PortableText) for key in (PREVIOUS_MAP_SHORTCUT, NEXT_MAP_SHORTCUT)}
    for objectName in ("PreviousModuleAction", "NextModuleAction"):
        if objectName in _state["takenActions"]:
            continue
        action = shell.mainWindow.findChild("QAction", objectName) if getattr(shell, "mainWindow", None) is not None else None
        if action is None:
            continue
        shortcuts = list(action.shortcuts())
        kept = [key for key in shortcuts if key.toString(qt.QKeySequence.PortableText) not in taken]
        if len(kept) != len(shortcuts):
            _state["takenActions"][objectName] = (action, shortcuts)
            action.setShortcuts(kept)


def _restoreActionShortcuts():
    for action, shortcuts in list(_state["takenActions"].values()):
        try:
            action.setShortcuts(shortcuts)
        except Exception:
            pass  # deleted with the main window
    _state["takenActions"] = {}


#
# Viewer extras (VISUAL 4.5): the map legend (bottom-right of the main view) and the layer chips (bottom-left)
#

#: Size of the legend card and the margin from the edges of the view
LEGEND_WIDTH = 220
OVERLAY_MARGIN = 10
#: Height of the colour bar of the legend
LEGEND_BAR_HEIGHT = 8
#: The view under the overlays: the large view of the Studio layouts
OVERLAY_VIEW = "Red"
#: Height reserved at the bottom of the view for its corner text (the image name) under the layer chips
CORNER_TEXT_HEIGHT = 22


class _OverlayFilter(qt.QObject):
    """Follows the main view (moved, resized, shown or hidden) to keep the overlays on it."""

    def __init__(self, overlay):
        qt.QObject.__init__(self)
        self.overlay = overlay

    def eventFilter(self, obj, event):
        if event.type() in (qt.QEvent.Resize, qt.QEvent.Move, qt.QEvent.Show, qt.QEvent.Hide):
            self.overlay.schedulePlace()
        return False


class MapOverlay:
    """The Studio viewer extras of Map: a floating colour legend card (bottom-right of the main slice view: the statistic,
    the threshold, an 8 px bar of the LNM colour table with ticks) and layer chips (bottom-left: "Network T · 80%", the
    template, "Lesion outline").

    They are Qt widgets over the view (children of the main window's central widget, like the toasts): no VTK actor
    changes. They are shown only while Studio shows Map with a map in the views, follow the theme, the threshold and the
    opacity (``refresh()``), and are deleted when Map is left.
    """

    def __init__(self, shell):
        from . import ui

        self.ui = ui
        self.shell = shell
        parent = shell.mainWindow.centralWidget()
        self.parent = parent
        self.legend = qt.QFrame(parent)
        self.legend.setObjectName("StudioMapLegend")
        self.legend.setAttribute(qt.Qt.WA_StyledBackground, True)
        self.legend.setFixedWidth(LEGEND_WIDTH)
        layout = qt.QVBoxLayout(self.legend)
        layout.setContentsMargins(12, 9, 12, 8)
        layout.setSpacing(6)
        top = qt.QHBoxLayout()
        top.setSpacing(8)
        self.statisticLabel = ui.label("T", "caption", "text", "StudioMapLegendStatistic")
        ui.setText(self.statisticLabel, "label")
        top.addWidget(self.statisticLabel)
        top.addStretch(1)
        self.thresholdLabel = ui.label("", "caption", "text2", "StudioMapLegendThreshold")
        ui.tabular(self.thresholdLabel)
        top.addWidget(self.thresholdLabel)
        layout.addLayout(top)
        self.bar = qt.QLabel()
        self.bar.setObjectName("StudioMapLegendBar")
        self.bar.setFixedHeight(LEGEND_BAR_HEIGHT)
        layout.addWidget(self.bar)
        ticks = qt.QHBoxLayout()
        self.tickLabels = []
        for index, alignment in enumerate((qt.Qt.AlignLeft, qt.Qt.AlignHCenter, qt.Qt.AlignRight)):
            tick = ui.label("", "caption", "text3", f"StudioMapLegendTick{index}")
            ui.tabular(tick)
            tick.setAlignment(alignment | qt.Qt.AlignVCenter)
            ticks.addWidget(tick, 1)
            self.tickLabels.append(tick)
        layout.addLayout(ticks)

        self.chips = qt.QWidget(parent)
        self.chips.setObjectName("StudioMapLayerChips")
        chipsLayout = qt.QHBoxLayout(self.chips)
        chipsLayout.setContentsMargins(0, 0, 0, 0)
        chipsLayout.setSpacing(6)
        self.chipLabels = {}
        for key in ("network", "template", "lesion"):
            chip = qt.QLabel()
            chip.setObjectName(f"StudioMapLayerChip_{key}")
            chip.setAttribute(qt.Qt.WA_StyledBackground, True)
            ui.setText(chip, "caption")
            chipsLayout.addWidget(chip)
            self.chipLabels[key] = chip
        for widget in (self.legend, self.chips):
            widget.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
            ui.bindStyleSheet(widget, self._styleSheet)
            widget.hide()
        self._state = None
        self._view = None
        self._filter = _OverlayFilter(self)
        self._placeTimer = qt.QTimer()
        self._placeTimer.setSingleShot(True)
        self._placeTimer.setInterval(0)
        self._placeTimer.connect("timeout()", self.place)
        ui.addPaletteListener(self.refresh)

    def _styleSheet(self):
        ui = self.ui
        window = qt.QColor(ui.token("window"))
        border = ui.token("hairline")
        return (f"QFrame#StudioMapLegend {{ background-color: {ui._rgba(window, 0.86)}; border: 1px solid {border.name()}; border-radius: 10px; }}"
                "QFrame#StudioMapLegend QLabel { background: transparent; border: none; }"
                f"QLabel[objectName^=\"StudioMapLayerChip_\"] {{ background-color: {ui._rgba(window, 0.78)}; color: {ui.token('text2').name()};"
                f" border: 1px solid {border.name()}; border-radius: 6px; padding: 2px 8px; }}")

    def _sliceWidget(self):
        layoutManager = slicer.app.layoutManager()
        widget = layoutManager.sliceWidget(OVERLAY_VIEW) if layoutManager is not None else None
        return widget if widget is not None and widget.isVisible() else None

    def _watch(self, sliceWidget):
        if sliceWidget is self._view:
            return
        if self._view is not None and self.ui._isAlive(self._view):
            self._view.removeEventFilter(self._filter)
        self._view = sliceWidget
        if sliceWidget is not None:
            sliceWidget.installEventFilter(self._filter)

    def refresh(self):
        """Show what the Map module shows now (``LesionNetworkMappingWidget.legendState()``), or hide."""
        if not self.ui._isAlive(self.legend):
            return
        widget = moduleWidget(create=False)
        state = widget.legendState() if widget is not None and hasattr(widget, "legendState") else None
        self._state = state
        if state is None:
            self.legend.hide()
            self.chips.hide()
            return
        statistic = str(state["statistic"])
        self.statisticLabel.setText(statistic)
        self.thresholdLabel.setText(str(state["thresholdText"]))
        window = float(state["window"] or 0.0)
        for tick, value in zip(self.tickLabels, (-window, 0.0, window), strict=True):
            tick.setText(_tickText(value))
        self._paintBar()
        opacity = round(100 * float(state["opacity"]))
        self.chipLabels["network"].setText(_("Network {statistic} · {pct}%").format(statistic=statistic, pct=opacity))
        templateName = _templateChipText()
        self.chipLabels["template"].setText(templateName)
        self.chipLabels["template"].setVisible(bool(templateName))
        lesion = self.chipLabels["lesion"]
        lesion.setText("\u25a1 " + _("Lesion outline"))
        lesion.setVisible(bool(state["outline"]))
        self.place()

    def _paintBar(self):
        """The colour bar: the LNM diverging colours from -window to +window."""
        from LesionNetworkMappingLib import colormaps

        width = max(1, LEGEND_WIDTH - 24)
        ratio = self.bar.devicePixelRatioF() if hasattr(self.bar, "devicePixelRatioF") else 1.0
        image = qt.QImage(int(width * ratio), int(LEGEND_BAR_HEIGHT * ratio), qt.QImage.Format_ARGB32_Premultiplied)
        image.fill(qt.Qt.transparent)
        colors = colormaps.lnm_diverging(64)
        painter = qt.QPainter(image)
        painter.setRenderHint(qt.QPainter.Antialiasing)
        gradient = qt.QLinearGradient(0, 0, image.width(), 0)
        for index, (r, g, b) in enumerate(colors):
            gradient.setColorAt(index / (len(colors) - 1), qt.QColor.fromRgbF(r, g, b))
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QBrush(gradient))
        radius = image.height() / 2.0
        painter.drawRoundedRect(qt.QRectF(0, 0, image.width(), image.height()), radius, radius)
        painter.end()
        pixmap = qt.QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        self.bar.setPixmap(pixmap)

    def schedulePlace(self):
        if not self._placeTimer.isActive():
            self._placeTimer.start()

    def place(self):
        """Put the legend at the bottom-right and the chips at the bottom-left of the main view (hidden without it)."""
        if not self.ui._isAlive(self.legend):
            return
        sliceWidget = self._sliceWidget()
        self._watch(sliceWidget)
        if self._state is None or sliceWidget is None:
            self.legend.hide()
            self.chips.hide()
            return
        view = sliceWidget.sliceView()
        topLeft = self.parent.mapFromGlobal(view.mapToGlobal(qt.QPoint(0, 0)))
        rect = qt.QRect(topLeft.x(), topLeft.y(), view.width, view.height)
        self.legend.adjustSize()
        legendSize = self.legend.sizeHint
        fits = rect.width() >= LEGEND_WIDTH + 2 * OVERLAY_MARGIN + 120 and rect.height() >= legendSize.height() + 80
        # Above the corner text of the view (the image name, the radiological statement)
        self.legend.move(rect.right() - OVERLAY_MARGIN - LEGEND_WIDTH, rect.bottom() - OVERLAY_MARGIN - CORNER_TEXT_HEIGHT - legendSize.height())
        self.legend.setVisible(fits)
        self.chips.adjustSize()
        chipsSize = self.chips.sizeHint
        roomForChips = rect.width() - (LEGEND_WIDTH + 3 * OVERLAY_MARGIN if fits else 2 * OVERLAY_MARGIN)
        self.chips.move(rect.left() + OVERLAY_MARGIN, rect.bottom() - OVERLAY_MARGIN - CORNER_TEXT_HEIGHT - chipsSize.height())
        self.chips.setVisible(chipsSize.width() <= roomForChips)
        for widget in (self.legend, self.chips):
            if widget.isVisible():
                widget.raise_()

    def dispose(self):
        self.ui.removePaletteListener(self.refresh)
        self._placeTimer.stop()
        self._watch(None)
        for widget in (self.legend, self.chips):
            if self.ui._isAlive(widget):
                self.ui.bindStyleSheet(widget, None)
                widget.hide()
                widget.deleteLater()


def _tickText(value):
    """A tick of the legend: "-12", "0", "12" (one decimal below 10)."""
    if abs(value) < 1e-9:
        return "0"
    text = f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"
    return text.replace("-", "\u2212")


def _templateChipText():
    """The short name of the image under the map in the main view ("MNI152NLin2009cAsym"), or ""."""
    layoutManager = slicer.app.layoutManager()
    sliceWidget = layoutManager.sliceWidget(OVERLAY_VIEW) if layoutManager is not None else None
    compositeNode = sliceWidget.mrmlSliceCompositeNode() if sliceWidget is not None else None
    node = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID()) if compositeNode is not None and compositeNode.GetBackgroundVolumeID() else None
    if node is None:
        return ""
    name = node.GetName() or ""
    try:
        from DataProbeLib.SliceViewAnnotations import flatDisplayName

        name = flatDisplayName(name, node.GetAttribute("Studio.Role") == "template")
    except Exception:
        name = name.replace("_", " ")
    return name.split(" \u00b7 ")[0]


def overlay():
    """The map legend and layer chips while Studio shows Map (None otherwise)."""
    return _state.get("overlay")


def refreshOverlay():
    """Update the legend and the chips (the Map module calls it after a change of the map, its threshold or opacity)."""
    current = _state.get("overlay")
    if current is not None:
        current.refresh()


def _installOverlay(shell):
    _removeOverlay()
    if shell is None or not hasattr(shell, "mainWindow") or shell.mainWindow is None or shell.mainWindow.centralWidget() is None:
        return
    if not _state.get("quitConnected"):
        # The overlay's Python event filter must not outlive the interpreter
        slicer.app.connect("aboutToQuit()", _removeOverlay)
        _state["quitConnected"] = True
    try:
        _state["overlay"] = MapOverlay(shell)
        _state["overlay"].refresh()
    except Exception:
        logging.error(f"Studio Map: the map legend could not be shown:\n{traceback.format_exc()}")
        _state["overlay"] = None


def _removeOverlay():
    current = _state.pop("overlay", None)
    if current is not None:
        try:
            current.dispose()
        except Exception:
            logging.error(f"Studio Map: the map legend could not be removed:\n{traceback.format_exc()}")


def moreMenuActions(shell):
    """⋯ of the Map panel header: the recipes and the reporting checks (their header rows show only with a study)."""
    widget = moduleWidget(create=False)
    if widget is None or not hasattr(widget, "studioMenuActions"):
        return []
    try:
        return widget.studioMenuActions()
    except Exception:
        logging.error(f"Studio Map: the menu items could not be listed:\n{traceback.format_exc()}")
        return []


def enter(shell):
    _state["shell"] = shell
    captureLayers()
    widget = moduleWidget()
    if widget is None:
        return
    _takeActionShortcuts(shell)
    _installShortcuts(shell)
    _watchSeed(widget)
    _adoptDrawSeed(widget)
    setPresentation = getattr(widget, "setStudioPresentation", None)
    if setPresentation is not None:
        try:
            setPresentation(True)
        except Exception:
            logging.error(f"Studio Map: cannot give the panel its Studio look:\n{traceback.format_exc()}")
    try:
        widget.showMapLayers()
    except Exception:
        logging.error(f"Studio Map: cannot show the map layers:\n{traceback.format_exc()}")
    _browser.showLesion()
    shell.setPrimaryAction(widget.onPrimary, widget.computeButton.text)
    widget._refreshStatusChip()
    _installOverlay(shell)


def leave(shell):
    _removeOverlay()
    _restoreActionShortcuts()
    _browser.removeLesion()
    widget = moduleWidget(create=False)
    if widget is not None:
        try:
            widget.restoreLesionDisplay()
        except Exception:
            logging.error(f"Studio Map: cannot restore the lesion display:\n{traceback.format_exc()}")
        # Classic keeps Map shown without leaving the module: the Classic look of the panel comes back
        setPresentation = getattr(widget, "setStudioPresentation", None)
        if setPresentation is not None:
            try:
                setPresentation(False)
            except Exception:
                logging.error(f"Studio Map: cannot give the panel its Classic look back:\n{traceback.format_exc()}")
        # Classic keeps Map shown without leaving the module: the primary button goes back to the Compute card and the
        # disclaimer below the cards (the sticky footer is removed with the shell)
        releaseFooter = getattr(widget, "releaseShellFooter", None)
        if releaseFooter is not None:
            try:
                releaseFooter(shell)
            except Exception:
                logging.error(f"Studio Map: cannot give the inspector footer back:\n{traceback.format_exc()}")
    restoreLayers()
    if getattr(shell, "_primaryAction", None) is not None and widget is not None and shell._primaryAction == widget.onPrimary:
        shell.setPrimaryAction(None, "")
    _state["shell"] = None


def exportCurrent():
    """Ctrl+E: export what Map shows (the run's maps, or with Seed = Cohort the group maps)."""
    widget = moduleWidget(create=False)
    if widget is None:
        return
    if widget.seedMode() == "cohort":
        card = widget.ctx.card("cohort")
        if card is not None and hasattr(card, "export"):
            card.export()
            return
    widget.onExport()


#
# Statistical maps opened from Library or dropped
#


def loadMap(path, shell=None):
    """Show a statistical map file in Map (module docstring); return its volume node.

    Returns None when Map cannot show it now (a map is being computed, or the file cannot be read): a toast
    says why and the workspace and views stay as they were. Without the Map module the importer's own
    thresholded overlay shows it.
    """
    shell = _shellOrNone(shell)
    widget = moduleWidget()
    if widget is None:
        # No Map module in this build: the importer's own thresholded overlay
        from . import importer

        mapNode = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
        importer._routeStatMap(path, shell, importer.LoadResult(), mapNode=mapNode)
        return mapNode
    captured = captureLayers() if shell is not None and shell.currentWorkspace() != "map" else False
    try:
        volume = widget.showMapFile(path)
    except Exception as exc:
        # The importer's entry point: say why the map did not open (a map job is running, or the file
        # cannot be read) and stay where the user is, with the views as they were
        if captured:
            restoreLayers()
        if isinstance(exc, RuntimeError):
            message = str(exc)
            logging.warning(f"Studio Map: {os.path.basename(path)} not shown: {message}")
        else:
            message = _("Cannot show {name} as a map").format(name=os.path.basename(path))
            logging.error(f"Studio Map: cannot show {path}:\n{traceback.format_exc()}")
        if shell is not None:
            shell.toast(message, level="consequence")
        return None
    _browser.setFromFile(path, widget)
    if shell is not None and shell.currentWorkspace() != "map":
        shell.switchTo("map")
    else:
        _browser.showLesion()
    return volume


def showOpenMap(path, shell=None):
    """Show again the map file that Map already shows (re-opened from Recent or dropped again): Map is entered with its
    layers and nothing is loaded. Return its volume, or None when Map does not show ``path``.
    """
    widget = moduleWidget(create=False)
    loaded = getattr(widget, "loadedMap", None) if widget is not None else None
    node = getattr(widget, "_parameterNode", None) if widget is not None else None
    volume = node.resultVolume if node is not None else None
    if not path or not loaded or not loaded.get("path") or volume is None or volume.GetScene() is None:
        return None

    def normalized(value):
        return os.path.normcase(os.path.realpath(os.path.abspath(str(value))))

    if normalized(loaded["path"]) != normalized(path):
        return None
    shell = _shellOrNone(shell)
    if shell is not None and shell.currentWorkspace() != "map":
        shell.switchTo("map")  # enter() shows the map's layers
    else:
        try:
            widget.showMapLayers()
        except Exception:
            logging.error(f"Studio Map: cannot show the map layers:\n{traceback.format_exc()}")
    return volume


#
# Browsing one map at a time
#


def _readJson(path):
    import json

    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _studyLesion(widget, sid):
    study = getattr(widget.ctx, "study", None) if widget is not None else None
    if study is None or not sid or sid not in study:
        return None
    row = study.subject(sid)
    lesion = row.get("lesion")
    if not lesion or lesion == "n/a":
        return None
    path = study.abspath(lesion)
    return path if os.path.isfile(path) else None


def _sidecarLesion(folder, name, label, suffix):
    desc = f"_desc-{suffix}" if suffix else ""
    sidecar = _readJson(os.path.join(folder, f"{name}_conn-{label}{desc}_lnm.json")) or {}
    lesion = (sidecar.get("lesion") or {}).get("file")
    if not lesion:
        return None
    path = lesion if os.path.isabs(lesion) else os.path.normpath(os.path.join(folder, *lesion.split("/")))
    return path if os.path.isfile(path) else None


def _entryLesion(folder):
    """The lesion of a cohort map cache entry (its entry.json), or None."""
    entry = _readJson(os.path.join(folder, "entry.json")) or {}
    lesion = entry.get("lesion")
    if not lesion:
        return None
    try:
        from LesionNetworkMappingLib import cohortmaps

        path = cohortmaps.resolve_lesion_path(lesion, folder)
    except Exception:
        path = None
    return path if path and os.path.isfile(path) else None


def _lesionKey(path):
    """Identity of a lesion file: its absolute normalized path, size and modification time."""
    path = os.path.normcase(os.path.abspath(path))
    try:
        info = os.stat(path)
        return (path, info.st_size, info.st_mtime_ns)
    except OSError:
        return (path, None, None)


class _Browser:
    """The set of maps that Alt+←/→ steps through: [(label, map path, lesion path or None)] and the current index."""

    def __init__(self):
        self.items = []
        self.index = -1
        self._lesionShown = None  # (browsing segmentation ID, lesion file key) of the outline shown

    def clear(self):
        self.items, self.index = [], -1
        self.removeLesion()

    def setItems(self, items, index=0):
        self.items = list(items)
        self.index = min(max(int(index), 0), len(self.items) - 1) if self.items else -1

    def setFromFile(self, path, widget=None):
        """The maps of the same kind next to ``path`` (engine outputs), with their lesions (sidecar, study, cache entry)."""
        path = os.path.abspath(path)
        folder, base = os.path.split(path)
        match = _MAP_NAME.match(base)
        if match is None:
            self.setItems([(os.path.basename(path), path, None)])
            return
        label, desc, suffix = match.group("label"), match.group("desc"), match.group("suffix")
        items = []
        for name in sorted(os.listdir(folder)):
            other = _MAP_NAME.match(name)
            if other is None or (other.group("label"), other.group("desc"), other.group("suffix")) != (label, desc, suffix):
                continue
            subject = _SUBJECT.search(other.group("name"))
            sid = subject.group(1) if subject else other.group("name")
            lesion = _sidecarLesion(folder, other.group("name"), label, suffix) or _studyLesion(widget, sid) or _entryLesion(folder)
            items.append((sid, os.path.join(folder, name), lesion))
        index = next((i for i, item in enumerate(items) if os.path.normpath(item[1]) == os.path.normpath(path)), 0)
        self.setItems(items or [(base, path, None)], index)

    def current(self):
        return self.items[self.index] if 0 <= self.index < len(self.items) else None

    def step(self, delta):
        if len(self.items) < 2:
            return None
        self.index = (self.index + delta) % len(self.items)
        return self.current()

    def _lesionNode(self, create):
        for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"):
            if node.GetAttribute(BROWSE_LESION_ATTRIBUTE) == "1":
                return node
        if not create:
            return None
        node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", BROWSE_LESION_NAME)
        node.SetAttribute(BROWSE_LESION_ATTRIBUTE, "1")
        node.SetHideFromEditors(True)  # never offered as a lesion to Draw or the Seed card
        node.SetSaveWithScene(False)
        node.CreateDefaultDisplayNodes()
        return node

    def showLesion(self):
        """Show the current map's lesion as a white outline (in the one browsing segmentation), or nothing."""
        item = self.current()
        lesion = item[2] if item is not None else None
        if not lesion:
            self.removeLesion()
            return None
        try:
            key = _lesionKey(lesion)
            node = self._lesionNode(create=False)
            if node is not None and self._lesionShown == (node.GetID(), key):
                return node  # the same file (lesion files of different subjects often share one name)
            import numpy as np

            from LesionNetworkMappingLib import display, nifti

            data, affine, _header = nifti.read_nifti(lesion)
            data = np.asarray(data).reshape(np.asarray(data).shape[:3])
            node = node or self._lesionNode(create=True)
            self._lesionShown = None
            labelmap = slicer.util.addVolumeFromArray(np.ascontiguousarray(np.transpose((data != 0).astype(np.uint8), (2, 1, 0))), ijkToRAS=affine,
                                                      name="__lnm_browse__", nodeClassName="vtkMRMLLabelMapVolumeNode")
            try:
                node.GetSegmentation().RemoveAllSegments()
                slicer.modules.segmentations.logic().ImportLabelmapToSegmentationNode(labelmap, node)
                segmentation = node.GetSegmentation()
                if segmentation.GetNumberOfSegments():
                    segmentation.GetNthSegment(0).SetName(item[0])
            finally:
                if labelmap.GetDisplayNode() is not None and labelmap.GetDisplayNode().GetColorNode() is not None and labelmap.GetDisplayNode().GetColorNode().GetName().startswith("__lnm_browse__"):
                    slicer.mrmlScene.RemoveNode(labelmap.GetDisplayNode().GetColorNode())
                slicer.mrmlScene.RemoveNode(labelmap)
            node.SetAttribute("LNM.BrowseLesionFile", os.path.basename(lesion))
            self._lesionShown = (node.GetID(), key)
            displayNode = node.GetDisplayNode()
            displayNode.SetVisibility2DFill(False)
            displayNode.SetVisibility2DOutline(True)
            displayNode.SetVisibility3D(False)
            for index in range(node.GetSegmentation().GetNumberOfSegments()):
                displayNode.SetSegmentOverrideColor(node.GetSegmentation().GetNthSegmentID(index), *display.OUTLINE_COLOR)
            return node
        except Exception:
            logging.error(f"Studio Map: cannot show the lesion {lesion}:\n{traceback.format_exc()}")
            return None

    def removeLesion(self):
        self._lesionShown = None
        node = self._lesionNode(create=False)
        if node is not None:
            slicer.mrmlScene.RemoveNode(node)


_browser = _Browser()


def browser():
    """The map browser of the Map workspace (``items``, ``index``, ``current()``)."""
    return _browser


def browseCohort(rows, index=0, statistic="T"):
    """Browse the per-lesion maps of a cohort (maps.tsv rows: participant_id, lesion and the map columns) from ``index``."""
    column = {"T": "T", "Fz": "AvgFz", "r": "AvgR"}.get(statistic, "T")
    items = [(row["participant_id"], row[column], row.get("lesion") if row.get("lesion") and os.path.isfile(row.get("lesion")) else None)
             for row in rows if row.get(column) and row.get(column) != "n/a"]
    _browser.setItems(items, index)
    return _showCurrent()


def browseStep(delta):
    """Alt+←/→: show the previous (-1) or next (+1) map of the browsed set in the same volume; return the item or None."""
    if _browser.step(delta) is None:
        return None
    item = _showCurrent()
    if item is None:
        _browser.step(-delta)  # not shown: the index stays on the map the views show
    return item


def _showCurrent():
    item = _browser.current()
    widget = moduleWidget(create=False)
    if item is None or widget is None:
        return None
    shell = _state["shell"]
    try:
        widget.showMapFile(item[1], label=item[0])
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            message = str(exc)
        else:
            message = _("Cannot show {name} as a map").format(name=os.path.basename(item[1]))
            logging.error(f"Studio Map: cannot show {item[1]}:\n{traceback.format_exc()}")
        if shell is not None:
            shell.toast(message, level="consequence")
        return None
    _browser.showLesion()
    if shell is not None and len(_browser.items) > 1:
        shell.toast(_("{label} · {index} of {count} · Alt+← / Alt+→").format(label=item[0], index=_browser.index + 1, count=len(_browser.items)))
    return item
