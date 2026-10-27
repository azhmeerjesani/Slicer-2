import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import *
from slicer.util import VTKObservationMixin


#
# Presentations of the Segment Editor widget (Slicer Studio, SPEC 5.1)
#

#: "classic" is the Segment Editor as in 3D Slicer; "studio" is the Draw workspace of Slicer Studio
PRESENTATIONS = ("classic", "studio")

#: Effects shown as tiles in the studio presentation (an effect that is not registered is left out)
STUDIO_EFFECT_ORDER = ("Paint", "Draw", "Smart fill", "Erase", "Fill between slices")
#: Order of all effects when "More tools" is on in the studio presentation (extension effects follow)
STUDIO_ALL_EFFECTS_ORDER = ("Paint", "Draw", "Smart fill", "Erase", "Fill between slices", "Level tracing", "Threshold", "Grow from seeds",
                            "Smoothing", "Islands", "Margin", "Scissors", "Logical operators", "Hollow", "Mask volume")

#: Undo depth of both presentations; 30 in the studio presentation once segmentation history snapshots are cheap
UNDO_STATES = 20
STUDIO_UNDO_STATES_SHALLOW_HISTORY = 30


def _cheapUndoSnapshots():
    """True if segmentation undo states share derived representations (closed surfaces) instead of copying them
    (vtkSegmentationHistory of this build has the marker, and it is enabled): then a deeper undo history is cheap.
    """
    history = getattr(slicer, "vtkSegmentationHistory", None)
    getter = getattr(history, "GetShallowDerivedRepresentations", None)
    return bool(getter()) if getter is not None else False


#: Explicit property tables (nothing is restored from remembered values). Properties missing on older
#: qMRMLSegmentEditorWidget builds are skipped.
CLASSIC_PROPERTIES = {
    "segmentationNodeSelectorVisible": True,
    "sourceVolumeNodeSelectorVisible": True,
    "addRemoveSegmentButtonsVisible": True,
    "show3DButtonVisible": True,
    "switchToSegmentationsButtonVisible": True,
    "maskingSectionVisible": True,
    "effectButtonStyle": qt.Qt.ToolButtonIconOnly,
    "effectColumnCount": 2,
    "unorderedEffectsVisible": True,
    "maximumNumberOfUndoStates": UNDO_STATES,
    "effectsAboveSegments": False,
    "maskingSectionCollapsed": False,
    "studioPresentation": False,
}
STUDIO_PROPERTIES = {
    "segmentationNodeSelectorVisible": False,
    "sourceVolumeNodeSelectorVisible": False,
    "addRemoveSegmentButtonsVisible": False,
    "show3DButtonVisible": False,
    "switchToSegmentationsButtonVisible": False,
    # Shown with "More tools", or while it restricts editing (_updateMaskingVisibility)
    "maskingSectionVisible": False,
    "effectButtonStyle": qt.Qt.ToolButtonTextUnderIcon,
    "unorderedEffectsVisible": False,
    "maximumNumberOfUndoStates": UNDO_STATES,
    "effectsAboveSegments": True,
    "maskingSectionCollapsed": True,
    "studioPresentation": True,
    "autoCreateSegmentOnEffectActivation": True,
}
#: Properties shown again by "Show all controls" (the classic controls inside the studio presentation)
SHOW_ALL_CONTROLS_PROPERTIES = ("segmentationNodeSelectorVisible", "sourceVolumeNodeSelectorVisible", "addRemoveSegmentButtonsVisible",
                                "show3DButtonVisible", "switchToSegmentationsButtonVisible")
#: Effect parameters of each presentation: {(effect name, parameter): value}. Set only when the effect defines the
#: parameter (Outline: lasso on release; Fill gaps: the selected segment only).
CLASSIC_EFFECT_PARAMETERS = {("Draw", "ApplyOnRelease"): 0, ("Fill between slices", "SelectedSegmentOnly"): 0}
STUDIO_EFFECT_PARAMETERS = {("Draw", "ApplyOnRelease"): 1, ("Fill between slices", "SelectedSegmentOnly"): 1}

#: Widgets of the editor hidden in the studio presentation (the top bar and z/y undo; the hint line and the "?" help)
STUDIO_HIDDEN_WIDGETS = ("UndoRedoGroupBox", "EffectHelpBrowser")

#: Settings key of the inline rename of the studio presentation (the classic key, which official Slicer reads too,
#: is never written by the presentation)
CLASSIC_TERMINOLOGY_SETTINGS_KEY = "Segmentations/UseTerminologySelector"
STUDIO_TERMINOLOGY_SETTINGS_KEY = "Studio/Draw/UseTerminologySelector"

#: Effect tiles of the studio presentation: preferred 62 x 60 px (style), a 20 px icon, at least 56 px wide so that a
#: narrow inspector shrinks the tiles instead of widening; five columns when the inspector viewport (the width of the
#: module widget) is at least 5 x 62 + 4 x 4 px, else four
TILE_MINIMUM_SIZE = (56, 60)
TILE_ICON_SIZE = 20
TILE_SPACING = 4
FIVE_COLUMNS_WIDTH = 5 * 62 + 4 * TILE_SPACING

#: Mnemonics of the studio presentation: (key, effect name or action, description)
STUDIO_MNEMONICS = (
    ("P", "Paint", _("Brush")),
    ("D", "Draw", _("Outline")),
    ("M", "Smart fill", _("Smart fill")),
    ("E", "Erase", _("Erase (again: the previous tool)")),
    ("L", "Level tracing", _("Auto outline")),
    ("Shift+F", "Fill between slices", _("Fill gaps")),
    ("O", "toggleFill", _("Fill or outline only")),
)


def classicProperties():
    """The classic table, with the first segment created on the first effect unless testing (SPEC 9.1, D24)."""
    return {**CLASSIC_PROPERTIES, "autoCreateSegmentOnEffectActivation": not slicer.app.testingEnabled()}


def positionalKeyText(layoutIndex):
    """Key of the effect at ``layoutIndex`` of the effect grid (qMRMLSegmentEditorWidget: 1..0, then Shift+1..0)."""
    if 1 <= layoutIndex <= 10:
        return str(layoutIndex % 10)
    if 11 <= layoutIndex <= 20:
        return f"Shift+{(layoutIndex - 10) % 10}"
    return ""


def isEditableSourceVolume(node):
    """True if ``node`` may be a source volume of the studio presentation: a scalar volume that is not a
    reference image (Studio.Role figure or statmap), not a colour (vector) image and not a single slice.
    """
    if node is None or not node.IsA("vtkMRMLScalarVolumeNode") or node.IsA("vtkMRMLLabelMapVolumeNode"):
        return False
    if node.IsA("vtkMRMLVectorVolumeNode"):
        return False
    if node.GetAttribute("Studio.Role") in ("figure", "statmap"):
        return False
    imageData = node.GetImageData()
    return imageData is None or 1 not in imageData.GetDimensions()


#
# SegmentEditor
#
class SegmentEditor(ScriptedLoadableModule):
    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("Segment Editor")
        self.parent.categories = ["", translate("qSlicerAbstractCoreModule", "Segmentation")]
        self.parent.dependencies = ["Segmentations", "SubjectHierarchy"]
        self.parent.contributors = ["Csaba Pinter (Queen's University), Andras Lasso (Queen's University)"]
        self.parent.helpText = _("""
This module allows editing segmentation objects by directly drawing and using segmentation tools on the contained segments.
Representations other than the labelmap one (which is used for editing) are automatically updated real-time,
so for example the closed surface can be visualized as edited in the 3D view.
""")
        self.parent.helpText += parent.defaultDocumentationLink
        self.parent.acknowledgementText = _("""
This work is part of SparKit project, funded by Cancer Care Ontario (CCO)'s ACRU program
and Ontario Consortium for Adaptive Interventions in Radiation Oncology (OCAIRO).
""")

    def setup(self):
        # Register subject hierarchy plugin
        import SubjectHierarchyPlugins

        scriptedPlugin = slicer.qSlicerSubjectHierarchyScriptedPlugin(None)
        scriptedPlugin.setPythonSource(SubjectHierarchyPlugins.SegmentEditorSubjectHierarchyPlugin.filePath)


class _TileEventFilter(qt.QObject):
    """Studio presentation: a click on the active tile deselects its tool (the visible twin of Esc), the tile grid
    adapts its column count to the inspector width, and tile outlines follow their tile.
    """

    def __init__(self, widget):
        qt.QObject.__init__(self)
        self.widget = widget
        self.pressedTile = None

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType == qt.QEvent.Resize:
            self.widget._onTileGridResized(obj)
            return False
        if eventType in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease) and not obj.inherits("QAbstractButton"):
            # The tile grid frame (gaps, margins, empty cells): only its resizes matter
            return False
        if eventType == qt.QEvent.MouseButtonPress and event.button() == qt.Qt.LeftButton:
            if obj.checkable and obj.checked and self.widget._tileEffectIsActive(obj):
                self.pressedTile = obj
                return True
            self.pressedTile = None
        elif eventType == qt.QEvent.MouseButtonRelease and event.button() == qt.Qt.LeftButton and self.pressedTile is not None:
            tile = self.pressedTile
            self.pressedTile = None
            if tile == obj and obj.rect.contains(event.pos()):
                self.widget.editor.setActiveEffect(None)
            return True
        return False


#
# SegmentEditorWidget
#
class SegmentEditorWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    def __init__(self, parent):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)

        # Members
        self.parameterSetNode = None
        self.editor = None
        # Presentation (Slicer Studio): the requested one, the applied one (None until setup() ran) and the extension
        self._presentation = "classic"
        self._appliedPresentation = None
        self._extension = None
        self._installedExtension = None
        self._extensionWidgets = []
        self._footerWidget = None
        self._presentationShortcuts = []
        self._moreToolsButton = None
        #: Widget of the extension shown after the tool tiles (``toolOptionsWidget()``)
        self._toolOptionsWidget = None
        #: Observation of the segment editor node: the masking section stays shown while it restricts editing
        self._maskingObservation = None
        self._tileFilter = None
        self._tileOutlines = {}
        self._previousEffectName = None
        self._observedSegmentation = None
        self._columnTimer = None
        self._bottomStretch = None
        self._mainWindow = None
        self._classicEffectNameOrder = []

    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        # Add margin to the sides
        self.layout.setContentsMargins(4, 0, 4, 0)

        #
        # Segment editor widget
        #
        import qSlicerSegmentationsModuleWidgetsPythonQt

        self.editor = qSlicerSegmentationsModuleWidgetsPythonQt.qMRMLSegmentEditorWidget()
        # The classic (official) tool order, restored by the classic presentation
        self._classicEffectNameOrder = list(self.editor.effectNameOrder())
        self.editor.setMaximumNumberOfUndoStates(UNDO_STATES)
        if not slicer.app.testingEnabled() and hasattr(self.editor, "autoCreateSegmentOnEffectActivation"):
            # The first effect creates the first segment (D24); never under --testing (SPEC 9.1)
            self.editor.autoCreateSegmentOnEffectActivation = True
        # Set parameter node first so that the automatic selections made when the scene is set are saved
        self.selectParameterNode()
        self.editor.setMRMLScene(slicer.mrmlScene)
        self.layout.addWidget(self.editor)

        # Observe editor effect registrations to make sure that any effects that are registered
        # later will show up in the segment editor widget. For example, if Segment Editor is set
        # as startup module, additional effects are registered after the segment editor widget is created.
        self.effectFactorySingleton = slicer.qSlicerSegmentEditorEffectFactory.instance()
        self.effectFactorySingleton.connect("effectRegistered(QString)", self.editorEffectRegistered)

        # Connect observers to scene events
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.StartCloseEvent, self.onSceneStartClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndCloseEvent, self.onSceneEndClose)
        self.addObserver(slicer.mrmlScene, slicer.mrmlScene.EndImportEvent, self.onSceneEndImport)

        self.editor.connect("segmentationNodeChanged(vtkMRMLSegmentationNode*)", self._onSegmentationNodeChanged)

        # A presentation requested before setup() is applied now
        if self._presentation != "classic":
            self._applyPresentation()
        else:
            self._appliedPresentation = "classic"

    def editorEffectRegistered(self):
        self.editor.updateEffectList()
        if self._appliedPresentation == "studio":
            self._applyStudioEffects()
            self._stackCheckBoxRows(True)

    #
    # Presentation (Slicer Studio, SPEC 5.1)
    #

    def presentation(self):
        """The presentation of the editor: "classic" or "studio"."""
        return self._presentation

    def presentationExtension(self):
        """The extension object of the studio presentation (see ``setPresentation()``), or None."""
        return self._extension

    def setPresentation(self, name, extension=None):
        """Show the editor as in 3D Slicer ("classic") or as the Draw workspace of Slicer Studio ("studio").

        Each presentation sets an explicit table of properties (``CLASSIC_PROPERTIES``, ``STUDIO_PROPERTIES``):
        nothing is restored from remembered values. Calling it again with the same name does nothing (unless a new
        extension is given). Before ``setup()`` the name is stored and applied at the end of ``setup()``.

        ``extension`` (studio only, kept until another one is given) is an object with these optional methods:

        - ``headerWidget()``: inserted above the editor; ``trailingWidget()``: inserted after the editor;
        - ``footerWidget()``: appended to the module layout, unless ``extension.footerManagedByHost`` is true (the
          Studio shell shows it in its sticky inspector footer while Draw is shown);
        - ``shortcuts()`` -> ``[(QKeySequence or text, callable)]``: main window shortcuts while the module is entered;
        - ``effectAppearance()`` -> ``{effectName: (text, QIcon)}``: the tile texts and icons;
        - ``effectToolTip(effectName, positionalKey)`` -> tooltip text of a tile (else the editor's tooltip);
        - ``onEnter()`` and ``onExit()``: the module was entered or left (in the studio presentation);
        - ``ensureSegmentation(editor)``: called on entry instead of creating a segmentation, which the studio
          presentation never does by itself while there is no scalar volume;
        - ``moreToolsHost()`` -> a box layout that holds "More tools ▾" (else it follows the tool tiles);
        - ``toolOptionsWidget()`` -> a widget shown right after the tool tiles (Draw: the brush size row).
        """
        if name not in PRESENTATIONS:
            raise ValueError(f"Unknown Segment Editor presentation: {name}")
        if extension is not None:
            self._extension = extension
        changed = name != self._presentation
        self._presentation = name
        if self.editor is None:
            # setup() applies it
            return
        if not changed and self._appliedPresentation == name and (name == "classic" or self._installedExtension is self._extension):
            return
        self._applyPresentation()

    def _applyPresentation(self):
        name = self._presentation
        wasEntered = self._isEntered()
        if self._appliedPresentation == "studio" or self._installedExtension is not None:
            if wasEntered:
                self._removePresentationShortcuts()
                self._callExtension(self._installedExtension, "onExit")
            self._removeExtensionWidgets()
        if name == "classic":
            self._applyClassic()
        else:
            self._applyStudio()
        self._appliedPresentation = name
        # The layout minimum of the module changed: the module panel must not keep the previous one (the Studio
        # inspector width follows the layout minimum of the workspace module)
        self.parent.updateGeometry()
        container = self.parent.parentWidget()
        if container is not None and container.layout() is not None:
            # updateGeometry() does not reach the container while the module widget is hidden
            container.layout().invalidate()
        if name == "studio" and wasEntered:
            self._installPresentationShortcuts()
            self._callExtension(self._installedExtension, "onEnter")

    def _setProperties(self, table):
        for propertyName, value in table.items():
            if not hasattr(self.editor, propertyName):
                # A qMRMLSegmentEditorWidget without this property (older build)
                continue
            if getattr(self.editor, propertyName) != value:
                setattr(self.editor, propertyName, value)

    def _setEffectParameters(self, parameters):
        for (effectName, parameterName), value in parameters.items():
            effect = self.editor.effectByName(effectName)
            if effect is None or not effect.parameterDefined(parameterName):
                continue
            try:
                if effect.integerParameter(parameterName) != value:
                    effect.setParameter(parameterName, value)
            except Exception:
                # No parameter node yet: the effect default applies
                pass

    def _child(self, className, objectName):
        if self.editor is None:
            return None
        try:
            return self.editor.findChild(className, objectName)
        except ValueError:
            # The editor widget was destroyed (application exit): a queued update has nothing to do
            return None

    def _segmentsTableView(self):
        return self._child("qMRMLSegmentsTableView", "SegmentsTableView")

    def _applyClassic(self):
        self._removeTileFilter()
        self._observeMasking(False)
        self._setModuleWidthPolicy(qt.QSizePolicy.Minimum)
        self._setProperties(classicProperties())
        self.editor.setEffectNameOrder(self._classicEffectNameOrder)
        self._setEffectParameters(CLASSIC_EFFECT_PARAMETERS)
        self.editor.setVisible(True)
        self._setBottomStretch(False)
        self._stackCheckBoxRows(False)
        for objectName in (*STUDIO_HIDDEN_WIDGETS, "SegmentsTableResizableFrame"):
            widget = self._child("QWidget", objectName)
            if widget is not None:
                widget.setVisible(True)
        for optionsWidget in (self._child("QGroupBox", "OptionsGroupBox"), self._child("QFrame", "EffectsOptionsFrame")):
            if optionsWidget is not None:
                optionsWidget.setMinimumWidth(0)
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is not None:
            effectsGroupBox.layout().setContentsMargins(-1, -1, -1, -1)
            effectsGroupBox.layout().setSpacing(-1)
        tableView = self._segmentsTableView()
        if tableView is not None and tableView.useTerminologySelectorSettingsKey != CLASSIC_TERMINOLOGY_SETTINGS_KEY:
            # Back to the settings default (the classic key is read, never written here)
            tableView.useTerminologySelectorSettingsKey = CLASSIC_TERMINOLOGY_SETTINGS_KEY
        self._observeSegmentation(None)
        # Tiles: back to the effect titles and icons (the column count change above rebuilt the grid)
        for button in self._effectButtons(allButtons=True):
            effect = self._buttonEffect(button)
            button.setText(effect.title if effect is not None else _("None"))
            button.setIcon(self._effectIcon(effect, button))
            button.setIconSize(qt.QSize())
            button.setMinimumSize(0, 0)
            button.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Preferred)
            button.setProperty("slicerRole", None)
            self._repolish(button)
        self._removeTileOutlines()
        # Tooltips of the classic presentation (the editor writes them when its effect list is rebuilt)
        self.editor.updateEffectList()

    def _showAllControls(self):
        return bool(getattr(self._extension, "showAllControls", False))

    def _applyStudio(self):
        showAll = self._showAllControls()
        undoStates = STUDIO_UNDO_STATES_SHALLOW_HISTORY if _cheapUndoSnapshots() else UNDO_STATES
        properties = {**STUDIO_PROPERTIES, "maximumNumberOfUndoStates": undoStates}
        if showAll:
            # "Show all controls": the classic controls of the Segment Editor inside Draw
            properties.update(dict.fromkeys(SHOW_ALL_CONTROLS_PROPERTIES, True))
        self._setProperties(properties)
        self._setModuleWidthPolicy(qt.QSizePolicy.Preferred)
        self._installMoreToolsButton()
        if showAll and not self._moreToolsShown():
            self._moreToolsButton.blockSignals(True)
            self._moreToolsButton.setChecked(True)
            self._moreToolsButton.setText(_("Fewer tools ▴"))
            self._moreToolsButton.blockSignals(False)
        self.editor.setEffectNameOrder(self._studioEffectOrder(self._moreToolsShown()))
        self.editor.unorderedEffectsVisible = self._moreToolsShown()
        self._observeMasking(True)
        self._updateMaskingVisibility()
        self._setEffectParameters(STUDIO_EFFECT_PARAMETERS)
        for objectName in STUDIO_HIDDEN_WIDGETS:
            widget = self._child("QWidget", objectName)
            if widget is not None:
                widget.setVisible(showAll)
        # Wide tool options (Paint, Margin) do not widen the inspector: they are laid out in its width (the studio look
        # shows the options frame without its group box, qMRMLSegmentEditorWidget)
        for optionsWidget in (self._child("QGroupBox", "OptionsGroupBox"), self._child("QFrame", "EffectsOptionsFrame")):
            if optionsWidget is not None:
                optionsWidget.setMinimumWidth(1)
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is not None:
            effectsGroupBox.layout().setContentsMargins(0, 0, 0, 0)
            effectsGroupBox.layout().setSpacing(TILE_SPACING)
        tableView = self._segmentsTableView()
        if tableView is not None:
            # Inline rename on double-click (D26). The classic settings key, which official Slicer reads, is not written.
            if tableView.useTerminologySelectorSettingsKey != STUDIO_TERMINOLOGY_SETTINGS_KEY:
                tableView.useTerminologySelectorSettingsKey = STUDIO_TERMINOLOGY_SETTINGS_KEY
            if tableView.useTerminologySelector:
                tableView.useTerminologySelector = False
        self._installTileFilter()
        if self._installedExtension is not self._extension or not self._extensionWidgets:
            self._installExtensionWidgets()
        self._setBottomStretch(True)
        self._stackCheckBoxRows(True)
        self._observeSegmentation(self.editor.segmentationNode())
        self._updateSegmentsTableVisibility()
        self._applyStudioEffects()
        self._scheduleColumnUpdate()

    def _setBottomStretch(self, stretch):
        """Studio presentation: the cards and tool options keep their height and the free space goes below them (in the
        classic presentation the segments table takes it; the studio presentation hides it with one segment).
        """
        if stretch and self._bottomStretch is None:
            self._bottomStretch = qt.QSpacerItem(0, 0, qt.QSizePolicy.Minimum, qt.QSizePolicy.Expanding)
            self.layout.addItem(self._bottomStretch)
            self._setEditorVerticalPolicy(qt.QSizePolicy.Maximum)
        elif not stretch and self._bottomStretch is not None:
            self.layout.removeItem(self._bottomStretch)
            self._bottomStretch = None
            self._setEditorVerticalPolicy(self._classicEditorVerticalPolicy)

    _classicEditorVerticalPolicy = None

    def _setEditorVerticalPolicy(self, policy):
        sizePolicy = self.editor.sizePolicy
        if SegmentEditorWidget._classicEditorVerticalPolicy is None:
            SegmentEditorWidget._classicEditorVerticalPolicy = sizePolicy.verticalPolicy()
        if policy is not None and sizePolicy.verticalPolicy() != policy:
            self.editor.setSizePolicy(sizePolicy.horizontalPolicy(), policy)

    def _stackCheckBoxRows(self, stacked):
        """Studio presentation: rows of check boxes in the tool options (Brush: Sphere brush, Edit in 3D views, Color smudge)
        are stacked, so that the narrow inspector does not clip their labels; the classic presentation puts them back in rows.
        """
        # The options frame: in the options group box (classic), or shown without it (studio look)
        optionsFrame = self._child("QFrame", "EffectsOptionsFrame")
        if optionsFrame is None:
            return
        direction = qt.QBoxLayout.TopToBottom if stacked else qt.QBoxLayout.LeftToRight
        for layout in optionsFrame.findChildren("QHBoxLayout"):
            widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
            widgets = [widget for widget in widgets if widget is not None]
            if len(widgets) < 2 or not all(widget.inherits("QCheckBox") for widget in widgets):
                continue
            if layout.direction() != direction:
                layout.setDirection(direction)

    def _setModuleWidthPolicy(self, policy):
        """Horizontal size policy of the module widget. The module panel gives it Minimum (qSlicerModulePanel.cxx:163),
        so the widest preferred width of its contents sets the panel width; the studio presentation lets it shrink to
        its layout minimum (Preferred), so the inspector keeps its width whatever tool is chosen.
        """
        sizePolicy = self.parent.sizePolicy
        if sizePolicy.horizontalPolicy() != policy:
            self.parent.setSizePolicy(policy, sizePolicy.verticalPolicy())

    #
    # Studio tiles
    #

    def _studioEffectOrder(self, allEffects):
        available = set(self.editor.availableEffectNames())
        order = STUDIO_ALL_EFFECTS_ORDER if allEffects else STUDIO_EFFECT_ORDER
        return [name for name in order if name in available]

    def _moreToolsShown(self):
        if self._moreToolsButton is None:
            return False
        try:
            return bool(self._moreToolsButton.checked)
        except ValueError:
            # The button was destroyed (application exit): nothing is shown any more
            return False

    def setMoreToolsShown(self, shown):
        """Studio presentation: show every tool ("More tools") or only the five tiles."""
        if self._moreToolsButton is not None and bool(self._moreToolsButton.checked) != bool(shown):
            self._moreToolsButton.setChecked(bool(shown))

    def _onMoreToolsToggled(self, shown):
        self._moreToolsButton.setText(_("Fewer tools ▴") if shown else _("More tools ▾"))
        if self._appliedPresentation != "studio":
            return
        self.editor.setEffectNameOrder(self._studioEffectOrder(shown))
        self.editor.unorderedEffectsVisible = bool(shown)
        self._updateMaskingVisibility()
        self._applyStudioEffects()

    @staticmethod
    def maskingRestrictsEditing(segmentEditorNode):
        """True if the masking settings of ``segmentEditorNode`` are not the defaults (paint everywhere, overwrite other
        segments, no intensity range): the studio presentation then keeps them in sight.
        """
        if segmentEditorNode is None:
            return False
        return (segmentEditorNode.GetMaskMode() != slicer.vtkMRMLSegmentationNode.EditAllowedEverywhere
                or segmentEditorNode.GetOverwriteMode() != slicer.vtkMRMLSegmentEditorNode.OverwriteAllSegments
                or bool(segmentEditorNode.GetSourceVolumeIntensityMask()))

    def _updateMaskingVisibility(self):
        """Studio presentation: the masking section (its title says "Paint everywhere · overwrite other segments") is with
        the tools under "More tools ▾"; it stays shown while the settings restrict editing, so that a stroke that paints
        nothing always has its reason in sight. "Show all controls" shows it too.
        """
        if self._presentation != "studio":
            return
        visible = self._moreToolsShown() or self._showAllControls() or self.maskingRestrictsEditing(self.editor.mrmlSegmentEditorNode())
        if hasattr(self.editor, "maskingSectionVisible") and bool(self.editor.maskingSectionVisible) != visible:
            self.editor.maskingSectionVisible = visible

    def _observeMasking(self, observe):
        if self._maskingObservation is not None:
            node, tag = self._maskingObservation
            node.RemoveObserver(tag)
            self._maskingObservation = None
        node = self.editor.mrmlSegmentEditorNode() if observe else None
        if node is not None:
            tag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._updateMaskingVisibility())
            self._maskingObservation = (node, tag)

    def _installMoreToolsButton(self):
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is None:
            return
        if self._moreToolsButton is None:
            button = qt.QToolButton()
            button.setObjectName("SegmentEditorMoreToolsButton")
            button.setProperty("studioMoreTools", True)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            button.setText(_("More tools ▾"))
            button.setToolTip(_("More tools · Auto outline, Threshold, Smooth, Islands, Margin, Scissors, Combine…"))
            button.setSizePolicy(qt.QSizePolicy.Minimum, qt.QSizePolicy.Fixed)
            button.connect("toggled(bool)", self._onMoreToolsToggled)
            # Owned by the editor also while it is not in the layout (classic presentation)
            button.setParent(self.editor)
            button.hide()
            self._moreToolsButton = button
        host = self._callExtension(self._extension, "moreToolsHost")
        if host is not None:
            # On the right of the section label of the tools (VISUAL 4.4)
            if host.indexOf(self._moreToolsButton) < 0:
                editorLayout = self._parentLayoutOf(self.editor.layout(), self._moreToolsButton)
                if editorLayout is not None:
                    editorLayout.removeWidget(self._moreToolsButton)
                host.addWidget(self._moreToolsButton, 0, qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        else:
            layout = self._parentLayoutOf(self.editor.layout(), effectsGroupBox)
            if layout is not None and layout.indexOf(self._moreToolsButton) < 0:
                layout.insertWidget(layout.indexOf(effectsGroupBox) + 1, self._moreToolsButton, 0, qt.Qt.AlignLeft)
        self._moreToolsButton.show()

    def _removeMoreToolsButton(self):
        button = self._moreToolsButton
        if button is None:
            return
        if button.checked:
            button.blockSignals(True)
            button.setChecked(False)
            button.setText(_("More tools ▾"))
            button.blockSignals(False)
        layout = self._parentLayoutOf(self.editor.layout(), button)
        if layout is not None:
            layout.removeWidget(button)
        else:
            parentLayout = button.parentWidget().layout() if button.parentWidget() is not None else None
            hostLayout = self._parentLayoutOf(parentLayout, button) if parentLayout is not None else None
            if hostLayout is not None:
                hostLayout.removeWidget(button)
        # Owned by the editor again (the host belongs to the extension)
        button.setParent(self.editor)
        button.hide()

    @classmethod
    def _parentLayoutOf(cls, layout, widget):
        """The layout (``layout`` or a nested one) that holds ``widget``, or None."""
        if layout is None:
            return None
        if layout.indexOf(widget) >= 0:
            return layout
        for index in range(layout.count()):
            child = layout.itemAt(index).layout()
            found = cls._parentLayoutOf(child, widget)
            if found is not None:
                return found
        return None

    def _effectButtons(self, allButtons=False):
        """The effect buttons in their layout order (the "None" button first); ``allButtons`` adds the hidden ones."""
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is None:
            return []
        layout = effectsGroupBox.layout()
        buttons = [layout.itemAt(index).widget() for index in range(layout.count())]
        buttons = [button for button in buttons if button is not None]
        if allButtons:
            for button in effectsGroupBox.findChildren("QToolButton"):
                if button not in buttons and button.parentWidget() == effectsGroupBox:
                    buttons.append(button)
        return buttons

    @staticmethod
    def _buttonEffect(button):
        """The effect of an effect button (None for the "None" button)."""
        effect = button.property("Effect")
        return effect if effect is not None and hasattr(effect, "name") else None

    def _tileEffectIsActive(self, button):
        effect = self._buttonEffect(button)
        activeEffect = self.editor.activeEffect()
        return effect is not None and activeEffect is not None and activeEffect.name == effect.name

    @staticmethod
    def _effectIcon(effect, button):
        """The icon of an effect button in the classic presentation (the effect's icon)."""
        if effect is None:
            return qt.QIcon(":Icons/NullEffect.png")
        try:
            return effect.icon()
        except Exception:
            icon = button.property("segmentEditorEffectIcon")
            return icon if icon is not None else button.icon

    @staticmethod
    def _repolish(widget):
        style = widget.style()
        style.unpolish(widget)
        style.polish(widget)
        widget.update()

    def refreshPresentation(self):
        """Apply the studio presentation again (after the extension changed ``showAllControls``)."""
        if self.editor is not None and self._appliedPresentation == "studio":
            self._applyStudio()

    def _applyStudioEffects(self):
        """Tiles of the studio presentation: the "None" button hidden (Esc, Space or a click on the active tile
        deselect), texts and icons from the extension, tooltips with the live positional key.
        """
        if self._presentation != "studio":
            return
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is None:
            return
        layout = effectsGroupBox.layout()
        buttons = self._effectButtons()
        columns = max(1, self.editor.effectColumnCount)
        # Re-place the tiles without the "None" button. It keeps the first layout index, so the positional keys of
        # the editor (Esc, 1, 2...) do not change.
        while layout.count():
            layout.takeAt(0)
        for index, button in enumerate(buttons):
            if index == 0:
                layout.addWidget(button, max(1, (len(buttons) - 2) // columns + 1), 0)
                button.hide()
                continue
            row, column = divmod(index - 1, columns)
            layout.addWidget(button, row, column)
            button.show()
        appearance = self._callExtension(self._extension, "effectAppearance") or {}
        for index, button in enumerate(buttons):
            if self._tileFilter is not None:
                button.removeEventFilter(self._tileFilter)
                button.installEventFilter(self._tileFilter)
            effect = self._buttonEffect(button)
            if effect is None:
                continue
            text, icon = appearance.get(effect.name, (None, None))
            if button.property("segmentEditorEffectIcon") is None:
                # The effect's own icon, for the classic presentation if the effect does not give it to Python
                button.setProperty("segmentEditorEffectIcon", button.icon)
            button.setText(text or effect.title)
            if icon is not None and not icon.isNull():
                button.setIcon(icon)
            button.setIconSize(qt.QSize(TILE_ICON_SIZE, TILE_ICON_SIZE))
            button.setMinimumSize(*TILE_MINIMUM_SIZE)
            button.setSizePolicy(qt.QSizePolicy.Preferred, qt.QSizePolicy.Fixed)
            if button.property("slicerRole") != "toolTile":
                button.setProperty("slicerRole", "toolTile")
                self._repolish(button)
            toolTip = self._callExtension(self._extension, "effectToolTip", effect.name, positionalKeyText(index))
            if toolTip:
                button.setToolTip(toolTip)
        for effectName in list(self._tileOutlines):
            self._updateTileOutline(effectName)

    def _installTileFilter(self):
        if self._tileFilter is None:
            self._tileFilter = _TileEventFilter(self)
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        if effectsGroupBox is not None:
            effectsGroupBox.removeEventFilter(self._tileFilter)
            effectsGroupBox.installEventFilter(self._tileFilter)

    def _removeTileFilter(self):
        if self._tileFilter is None:
            return
        for widget in [self._child("QFrame", "EffectsGroupBox"), *self._effectButtons(allButtons=True)]:
            if widget is not None:
                widget.removeEventFilter(self._tileFilter)

    def _onTileGridResized(self, obj):
        if obj == self._child("QFrame", "EffectsGroupBox"):
            self._scheduleColumnUpdate()
        else:
            self._updateTileOutlines()

    def _scheduleColumnUpdate(self):
        if self._columnTimer is None:
            self._columnTimer = qt.QTimer()
            self._columnTimer.setSingleShot(True)
            self._columnTimer.setInterval(0)
            self._columnTimer.connect("timeout()", self._updateColumnCount)
        self._columnTimer.start()

    def studioColumnCount(self, viewportWidth):
        """Columns of the tile grid: 5 when the inspector viewport is at least ``FIVE_COLUMNS_WIDTH`` px wide, else 4."""
        return 5 if viewportWidth >= FIVE_COLUMNS_WIDTH else 4

    def _updateColumnCount(self):
        if self._appliedPresentation != "studio" or self.editor is None:
            return
        if self._child("QFrame", "EffectsGroupBox") is None or not self.parent.isVisible():
            return
        # The module widget fills the viewport of the module panel's scroll area
        columns = self.studioColumnCount(self.parent.width)
        if self.editor.effectColumnCount != columns:
            self.editor.effectColumnCount = columns
            self._applyStudioEffects()

    #
    # Tile outlines ("suggested" and "paused" tools, drawn by the studio extension)
    #

    def setTileOutline(self, effectName, color=None, dashed=False, toolTip=None):
        """Studio presentation: outline the tile of ``effectName`` in ``color`` (a QColor or "#rrggbb"), dashed or
        solid; ``color=None`` removes the outline. The tile is not checked: it only shows a suggestion or a paused tool.
        """
        if color is None:
            outline = self._tileOutlines.pop(effectName, None)
            if outline is not None:
                outline.hide()
                outline.setParent(None)
                outline.deleteLater()
            return
        color = qt.QColor(color)
        outline = self._tileOutlines.get(effectName)
        if outline is None:
            outline = qt.QFrame()
            outline.setObjectName("SegmentEditorTileOutline")
            outline.setAttribute(qt.Qt.WA_TransparentForMouseEvents, True)
            self._tileOutlines[effectName] = outline
        outline.setStyleSheet(f"QFrame#SegmentEditorTileOutline {{ background: transparent; border: 2px {'dashed' if dashed else 'solid'} {color.name()}; border-radius: 6px; }}")
        outline.setProperty("studioToolTip", toolTip or "")
        self._updateTileOutline(effectName)

    def tileOutline(self, effectName):
        """The outline frame of a tile (``setTileOutline()``), or None."""
        return self._tileOutlines.get(effectName)

    def _updateTileOutline(self, effectName):
        outline = self._tileOutlines.get(effectName)
        if outline is None:
            return
        button = next((button for button in self._effectButtons() if button.objectName == effectName), None)
        if button is None or not button.isVisibleTo(self.editor):
            outline.hide()
            return
        if outline.parentWidget() != button:
            outline.setParent(button)
            if self._tileFilter is not None:
                button.removeEventFilter(self._tileFilter)
                button.installEventFilter(self._tileFilter)
        outline.setGeometry(button.rect)
        outline.show()
        outline.raise_()

    def _updateTileOutlines(self):
        for effectName in list(self._tileOutlines):
            self._updateTileOutline(effectName)

    def _removeTileOutlines(self):
        for effectName in list(self._tileOutlines):
            self.setTileOutline(effectName, None)

    #
    # Extension widgets
    #

    @staticmethod
    def _callExtension(extension, methodName, *args):
        method = getattr(extension, methodName, None) if extension is not None else None
        if method is None:
            return None
        try:
            return method(*args)
        except Exception:
            import logging
            import traceback

            logging.error(f"Segment Editor presentation extension {methodName}() failed:\n{traceback.format_exc()}")
            return None

    def _installExtensionWidgets(self):
        extension = self._extension
        self._installedExtension = extension
        if extension is None:
            return
        header = self._callExtension(extension, "headerWidget")
        if header is not None:
            self.layout.insertWidget(0, header)
            header.show()
            self._extensionWidgets.append(header)
        options = self._callExtension(extension, "toolOptionsWidget")
        effectsGroupBox = self._child("QFrame", "EffectsGroupBox")
        editorLayout = self._parentLayoutOf(self.editor.layout(), effectsGroupBox) if effectsGroupBox is not None else None
        if options is not None and editorLayout is not None:
            editorLayout.insertWidget(editorLayout.indexOf(effectsGroupBox) + 1, options)
            self._toolOptionsWidget = options
        trailing = self._callExtension(extension, "trailingWidget")
        if trailing is not None:
            self.layout.insertWidget(self.layout.indexOf(self.editor) + 1, trailing)
            trailing.show()
            self._extensionWidgets.append(trailing)
        if not getattr(extension, "footerManagedByHost", False):
            footer = self._callExtension(extension, "footerWidget")
            if footer is not None:
                self.layout.addWidget(footer)
                footer.show()
                self._extensionWidgets.append(footer)

    def _removeExtensionWidgets(self):
        for widget in self._extensionWidgets:
            try:
                self.layout.removeWidget(widget)
                # Kept as a hidden child of the module widget (a widget without parent that Python holds until the
                # application exits would be deleted after the application object)
                widget.hide()
            except Exception:
                # Deleted by the extension
                pass
        self._extensionWidgets = []
        self._installedExtension = None
        self._removeMoreToolsButton()
        options = self._toolOptionsWidget
        self._toolOptionsWidget = None
        if options is not None:
            try:
                layout = self._parentLayoutOf(self.editor.layout(), options)
                if layout is not None:
                    layout.removeWidget(options)
                options.hide()
            except Exception:
                # Deleted by the extension
                pass

    #
    # Segments table (studio: only shown with more than one segment)
    #

    def _onSegmentationNodeChanged(self, segmentationNode):
        if self._appliedPresentation == "studio":
            self._observeSegmentation(segmentationNode)
            self._updateSegmentsTableVisibility()

    def _observeSegmentation(self, segmentationNode):
        if self._observedSegmentation is not None:
            for event in (slicer.vtkSegmentation.SegmentAdded, slicer.vtkSegmentation.SegmentRemoved):
                self.removeObserver(self._observedSegmentation, event, self._onSegmentAddedOrRemoved)
        self._observedSegmentation = None
        if segmentationNode is not None and segmentationNode.GetSegmentation() is not None:
            self._observedSegmentation = segmentationNode.GetSegmentation()
            for event in (slicer.vtkSegmentation.SegmentAdded, slicer.vtkSegmentation.SegmentRemoved):
                self.addObserver(self._observedSegmentation, event, self._onSegmentAddedOrRemoved)

    def _onSegmentAddedOrRemoved(self, caller=None, event=None):
        self._updateSegmentsTableVisibility()

    def _updateSegmentsTableVisibility(self):
        if self._appliedPresentation != "studio" and self._presentation != "studio":
            return
        frame = self._child("QWidget", "SegmentsTableResizableFrame")
        if frame is None:
            return
        segmentationNode = self.editor.segmentationNode()
        count = segmentationNode.GetSegmentation().GetNumberOfSegments() if segmentationNode is not None else 0
        visible = count > 1 or bool(getattr(self._extension, "showAllControls", False))
        if frame.isVisibleTo(self.editor) != visible:
            frame.setVisible(visible)

    #
    # Shortcuts of the studio presentation
    #

    def _installPresentationShortcuts(self):
        self._removePresentationShortcuts()
        if self._mainWindow is None:
            # Looked up once (slicer.util.mainWindow() walks the top-level widgets)
            self._mainWindow = slicer.util.mainWindow()
        mainWindow = self._mainWindow
        if mainWindow is None:
            return
        for key, target, description in STUDIO_MNEMONICS:
            if target not in ("toggleFill",) and self.editor.effectByName(target) is None:
                continue
            self._addPresentationShortcut(mainWindow, key, lambda target=target: self._onMnemonic(target), description)
        for entry in self._callExtension(self._installedExtension, "shortcuts") or []:
            key, callback = entry[0], entry[1]
            description = entry[2] if len(entry) > 2 else ""
            self._addPresentationShortcut(mainWindow, key, callback, description)

    def _addPresentationShortcut(self, mainWindow, key, callback, description):
        shortcut = qt.QShortcut(qt.QKeySequence(key), mainWindow)
        name = "".join(part.capitalize() for part in str(description or key).replace("(", " ").replace(")", " ").split()[:2])
        shortcut.setObjectName(f"StudioDraw{name}Shortcut")
        shortcut.setContext(qt.Qt.WindowShortcut)
        # Listed by the Studio cheat sheet and by the key collision test
        shortcut.setProperty("studioShortcut", True)
        shortcut.setProperty("studioDescription", description)
        shortcut.connect("activated()", callback)
        self._presentationShortcuts.append(shortcut)

    def presentationShortcuts(self):
        """The main window shortcuts that the studio presentation installed while the module is entered."""
        return list(self._presentationShortcuts)

    def _removePresentationShortcuts(self):
        for shortcut in self._presentationShortcuts:
            try:
                shortcut.setEnabled(False)
                shortcut.setProperty("studioShortcut", False)
                shortcut.setObjectName("")
                shortcut.disconnect("activated()")
                shortcut.setParent(None)
                shortcut.deleteLater()
            except Exception:
                # Deleted with the main window
                pass
        self._presentationShortcuts = []

    def _onMnemonic(self, target):
        """P, D, M, E, L, Shift+F and O of the studio presentation."""
        if target == "toggleFill":
            segmentationNode = self.editor.segmentationNode()
            displayNode = segmentationNode.GetDisplayNode() if segmentationNode is not None else None
            if displayNode is not None:
                fill = not displayNode.GetVisibility2DFill()
                displayNode.SetVisibility2DFill(fill)
                displayNode.SetVisibility2DOutline(True)
            return
        activeEffect = self.editor.activeEffect()
        activeName = activeEffect.name if activeEffect is not None else None
        if target == "Erase" and activeName == "Erase":
            # E again: back to the previous tool
            target = self._previousEffectName
            if not target or self.editor.effectByName(target) is None:
                self.editor.setActiveEffect(None)
                return
        elif activeName == target:
            return
        if activeName is not None and activeName != "Erase":
            self._previousEffectName = activeName
        self.activateEffect(target)

    def activateEffect(self, effectName):
        """Activate an effect as a click on its tile does (the first segment is created or selected when needed)."""
        button = next((button for button in self._effectButtons(allButtons=True) if button.objectName == effectName), None)
        if button is not None and button.isEnabled():
            button.click()
        if self.editor.activeEffect() is None or self.editor.activeEffect().name != effectName:
            self.editor.setActiveEffectByName(effectName)

    #
    # Module
    #

    def _isEntered(self):
        try:
            return bool(self.parent.isEntered)
        except Exception:
            return False

    def selectParameterNode(self):
        # Select parameter set node if one is found in the scene, and create one otherwise
        segmentEditorSingletonTag = "SegmentEditor"
        segmentEditorNode = slicer.mrmlScene.GetSingletonNode(segmentEditorSingletonTag, "vtkMRMLSegmentEditorNode")
        if segmentEditorNode is None:
            segmentEditorNode = slicer.mrmlScene.CreateNodeByClass("vtkMRMLSegmentEditorNode")
            segmentEditorNode.UnRegister(None)
            segmentEditorNode.SetSingletonTag(segmentEditorSingletonTag)
            segmentEditorNode = slicer.mrmlScene.AddNode(segmentEditorNode)
        if self.parameterSetNode == segmentEditorNode:
            # nothing changed
            return
        self.parameterSetNode = segmentEditorNode
        self.editor.setMRMLSegmentEditorNode(self.parameterSetNode)

    def getDefaultSourceVolumeNodeID(self):
        layoutManager = slicer.app.layoutManager()
        firstForegroundVolumeID = None
        studio = self._appliedPresentation == "studio"

        def usable(volumeID):
            # The studio presentation never edits on a figure, a statistical map, a colour image or a single slice
            return not studio or isEditableSourceVolume(slicer.mrmlScene.GetNodeByID(volumeID))

        # Use first background volume node in any of the displayed layouts.
        # If no beackground volume node is in any slice view then use the first
        # foreground volume node.
        for sliceViewName in layoutManager.sliceViewNames():
            sliceWidget = layoutManager.sliceWidget(sliceViewName)
            if not sliceWidget:
                continue
            compositeNode = sliceWidget.mrmlSliceCompositeNode()
            if compositeNode.GetBackgroundVolumeID() and usable(compositeNode.GetBackgroundVolumeID()):
                return compositeNode.GetBackgroundVolumeID()
            if compositeNode.GetForegroundVolumeID() and not firstForegroundVolumeID and usable(compositeNode.GetForegroundVolumeID()):
                firstForegroundVolumeID = compositeNode.GetForegroundVolumeID()
        # No background volume was found, so use the foreground volume (if any was found)
        return firstForegroundVolumeID

    @staticmethod
    def hasScalarVolume():
        """True if the scene holds a volume that can be a source volume of the studio presentation."""
        return any(isEditableSourceVolume(node) for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"))

    def enter(self):
        """Runs whenever the module is reopened"""

        # Allow switching between effects and selected segment using keyboard shortcuts
        self.editor.installKeyboardShortcuts()

        # Set parameter set node if absent
        self.selectParameterNode()
        self.editor.updateWidgetFromMRML()

        studio = self._appliedPresentation == "studio"
        # If no segmentation node exists then create one so that the user does not have to create one manually
        if not self.editor.segmentationNodeID():
            if studio:
                # The Draw workspace creates its lesion segmentation itself (never without a scan), through its extension;
                # without one yet, the studio presentation creates none (the extension does when it is installed)
                self._callExtension(self._extension, "ensureSegmentation", self.editor)
            else:
                segmentationNode = slicer.mrmlScene.GetFirstNode(None, "vtkMRMLSegmentationNode")
                if not segmentationNode:
                    segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
                self.editor.setSegmentationNode(segmentationNode)
                if not self.editor.sourceVolumeNodeID():
                    sourceVolumeNodeID = self.getDefaultSourceVolumeNodeID()
                    self.editor.setSourceVolumeNodeID(sourceVolumeNodeID)

        if studio:
            # The module panel set its size policy when it showed the module
            self._setModuleWidthPolicy(qt.QSizePolicy.Preferred)
            self._installPresentationShortcuts()
            # The editor rebuilt its tooltips when it installed its keyboard shortcuts
            self._applyStudioEffects()
            self._callExtension(self._installedExtension, "onEnter")

    def exit(self):
        if self._appliedPresentation == "studio":
            self._removePresentationShortcuts()
            self._callExtension(self._installedExtension, "onExit")
        self.editor.setActiveEffect(None)
        self.editor.uninstallKeyboardShortcuts()
        self.editor.removeViewObservations()

    def onSceneStartClose(self, caller, event):
        self.parameterSetNode = None
        self.editor.setSegmentationNode(None)
        self.editor.removeViewObservations()

    def onSceneEndClose(self, caller, event):
        if self.parent.isEntered:
            self.selectParameterNode()
            self.editor.updateWidgetFromMRML()

    def onSceneEndImport(self, caller, event):
        if self.parent.isEntered:
            self.selectParameterNode()
            self.editor.updateWidgetFromMRML()

    def cleanup(self):
        self._removePresentationShortcuts()
        # The masking observer reads the "More tools" button: a node change while the application shuts down must not reach it
        self._observeMasking(False)
        self.removeObservers()
        if self._columnTimer is not None:
            self._columnTimer.stop()
            self._columnTimer.disconnect("timeout()", self._updateColumnCount)
            self._columnTimer = None
        self._removeTileFilter()
        self.effectFactorySingleton.disconnect("effectRegistered(QString)", self.editorEffectRegistered)


class SegmentEditorTest(ScriptedLoadableModuleTest):
    """This is the test case for your scripted module."""

    def setUp(self):
        """Do whatever is needed to reset the state - typically a scene clear will be enough."""
        slicer.mrmlScene.Clear(0)

    def runTest(self):
        """Currently no testing functionality."""
        self.setUp()
        self.test_SegmentEditor1()
        self.test_SegmentEditorPresentation()

    def test_SegmentEditor1(self):
        """Add test here later."""
        self.delayDisplay("Starting the test")
        self.delayDisplay("Test passed!")

    def test_SegmentEditorPresentation(self):
        """The studio presentation and back restores every property of the classic table (SPEC 5.1)."""
        self.delayDisplay("Segment Editor presentations")
        widget = slicer.modules.segmenteditor.widgetRepresentation().self()
        editor = widget.editor
        self.assertEqual(widget.presentation(), "classic")
        table = {name: value for name, value in classicProperties().items() if hasattr(editor, name)}
        classic = {name: getattr(editor, name) for name in table}
        self.assertEqual(classic, table)
        widget.setPresentation("studio")
        self.assertEqual(widget.presentation(), "studio")
        self.assertFalse(editor.segmentationNodeSelectorVisible)
        self.assertFalse(editor.findChild("QWidget", "UndoRedoGroupBox").isVisibleTo(editor))
        widget.setPresentation("studio")
        widget.setPresentation("classic")
        self.assertEqual({name: getattr(editor, name) for name in table}, table)
        self.assertTrue(editor.findChild("QWidget", "UndoRedoGroupBox").isVisibleTo(editor))
        # The official tool order (and so the positional keys 1..0) comes back
        self.assertEqual(list(editor.effectNameOrder())[:4], ["Threshold", "Paint", "Draw", "Erase"])
        self.assertEqual(list(editor.effectNameOrder()), widget._classicEffectNameOrder)
        self.delayDisplay("Test passed!")
