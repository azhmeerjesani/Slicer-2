import os

import qt
import vtk

import slicer
from slicer.i18n import tr as _

from SegmentEditorEffects import *


class SegmentEditorFillBetweenSlicesEffect(AbstractScriptedSegmentEditorAutoCompleteEffect):
    """AutoCompleteEffect is an effect that can create a full segmentation
    from a partial segmentation (not all slices are segmented or only
    part of the target structures are painted).
    """

    def __init__(self, scriptedEffect):
        AbstractScriptedSegmentEditorAutoCompleteEffect.__init__(self, scriptedEffect)
        scriptedEffect.name = "Fill between slices"  # no tr (don't translate it because modules find effects by name)
        scriptedEffect.title = _("Fill between slices")
        # True if the current preview was computed for the selected segment only (SelectedSegmentOnly parameter)
        self.previewOfSelectedSegmentOnly = False

    def clone(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects

        clonedEffect = effects.qSlicerSegmentEditorScriptedEffect(None)
        clonedEffect.setPythonSource(__file__.replace("\\", "/"))
        return clonedEffect

    def icon(self):
        iconPath = os.path.join(os.path.dirname(__file__), "Resources/Icons/FillBetweenSlices.png")
        if os.path.exists(iconPath):
            return qt.QIcon(iconPath)
        return qt.QIcon()

    def helpText(self):
        return "<html>" + _("""Interpolate segmentation between slices<br>. Instructions:
<p><ul>
<li>Create complete segmentation on selected slices using any editor effect.
Segmentation will only expanded if a slice is segmented but none of the direct neighbors are segmented, therefore
do not use sphere brush with Paint effect and always leave at least one empty slice between segmented slices.
<li>All visible segments will be interpolated, not just the selected segment, unless <dfn>Selected segment only</dfn> is checked (then the selected segment is interpolated even if it is hidden).
<li>Click <dfn>Fill</dfn> to interpolate and apply in one step, or <dfn>Initialize</dfn> to review the result before <dfn>Apply</dfn>.
<li>The complete segmentation will be created by interpolating segmentations in empty slices.
</ul><p>
Masking settings are ignored. If segments overlap, segment higher in the segments table will have priority.
The effect uses  <a href="https://insight-journal.org/browse/publication/977">morphological contour interpolation method</a>.
<p>""")

    def setupOptionsFrame(self):
        AbstractScriptedSegmentEditorAutoCompleteEffect.setupOptionsFrame(self)

        self.selectedSegmentOnlyCheckBox = qt.QCheckBox(_("Selected segment only"))
        self.selectedSegmentOnlyCheckBox.objectName = self.__class__.__name__ + "SelectedSegmentOnly"
        self.selectedSegmentOnlyCheckBox.setToolTip(_("If checked, only the selected segment is interpolated."
                                                      " Otherwise all visible segments are interpolated."))

        self.fillButton = qt.QPushButton(_("Fill"))
        self.fillButton.objectName = self.__class__.__name__ + "Fill"
        self.fillButton.setToolTip(_("Interpolate and apply in one step (one undo step)"))
        qSize = qt.QSizePolicy()
        qSize.setHorizontalPolicy(qt.QSizePolicy.Expanding)
        self.fillButton.setSizePolicy(qSize)

        fillFrame = qt.QHBoxLayout()
        fillFrame.addWidget(self.selectedSegmentOnlyCheckBox)
        fillFrame.addWidget(self.fillButton)
        self.scriptedEffect.addOptionsWidget(fillFrame)

        self.selectedSegmentOnlyCheckBox.connect("toggled(bool)", self.onSelectedSegmentOnlyToggled)
        self.fillButton.connect("clicked()", self.onFill)

    def setMRMLDefaults(self):
        AbstractScriptedSegmentEditorAutoCompleteEffect.setMRMLDefaults(self)
        self.scriptedEffect.setParameterDefault("SelectedSegmentOnly", 0)

    def updateGUIFromMRML(self):
        AbstractScriptedSegmentEditorAutoCompleteEffect.updateGUIFromMRML(self)
        wasBlocked = self.selectedSegmentOnlyCheckBox.blockSignals(True)
        self.selectedSegmentOnlyCheckBox.checked = self.scriptedEffect.integerParameter("SelectedSegmentOnly") != 0
        self.selectedSegmentOnlyCheckBox.blockSignals(wasBlocked)

    def updateMRMLFromGUI(self):
        AbstractScriptedSegmentEditorAutoCompleteEffect.updateMRMLFromGUI(self)
        self.scriptedEffect.setParameter("SelectedSegmentOnly", 1 if self.selectedSegmentOnlyCheckBox.checked else 0)

    def onSelectedSegmentOnlyToggled(self, checked):
        self.updateMRMLFromGUI()
        # Update an existing preview for the new set of input segments
        if self.getPreviewNode():
            self.delayedAutoUpdateTimer.start()

    def preview(self):
        if self.scriptedEffect.integerParameter("SelectedSegmentOnly") != 0:
            selectedSegmentID = self.scriptedEffect.parameterSetNode().GetSelectedSegmentID()
            if not selectedSegmentID:
                raise RuntimeError(_("Select a segment, or uncheck 'Selected segment only'."))
            if not (self.previewOfSelectedSegmentOnly and self.selectedSegmentIds is not None
                    and self.selectedSegmentIds.GetNumberOfValues() == 1
                    and self.selectedSegmentIds.GetValue(0) == selectedSegmentID):
                if self.selectedSegmentIds is not None:
                    # The current preview was computed for other input segments
                    self.reset()
                # The base class keeps input segments that are set before preview()
                self.selectedSegmentIds = vtk.vtkStringArray()
                self.selectedSegmentIds.InsertNextValue(selectedSegmentID)
            self.previewOfSelectedSegmentOnly = True
        elif self.previewOfSelectedSegmentOnly:
            # Switch back to all visible segments
            self.previewOfSelectedSegmentOnly = False
            if self.selectedSegmentIds is not None:
                self.reset()
        AbstractScriptedSegmentEditorAutoCompleteEffect.preview(self)

    def onFill(self):
        """Interpolate and apply in one step. Apply saves the only undo state."""
        if self.previewComputationInProgress:
            return
        try:
            self.onPreview()
        except Exception:
            # The error has already been displayed by onPreview. Do not apply a stale or partial preview.
            return
        previewNode = self.getPreviewNode()
        if previewNode is None or self.isPreviewEmpty(previewNode):
            # There was nothing to interpolate: discard the preview without adding an undo state
            if previewNode is not None:
                self.reset()
            slicer.util.showStatusMessage(_("Nothing to fill."), 3000)
            return
        self.onApply()

    @staticmethod
    def isPreviewEmpty(previewNode):
        """Return True if no segment of the preview segmentation contains any voxel."""
        import numpy as np
        from vtk.util.numpy_support import vtk_to_numpy

        segmentation = previewNode.GetSegmentation()
        segmentIds = vtk.vtkStringArray()
        segmentation.GetSegmentIDs(segmentIds)
        for index in range(segmentIds.GetNumberOfValues()):
            labelmap = slicer.vtkOrientedImageData()
            if not slicer.vtkSlicerSegmentationsModuleLogic.GetSegmentBinaryLabelmapRepresentation(
                    previewNode, segmentIds.GetValue(index), labelmap, False):
                continue
            scalars = labelmap.GetPointData().GetScalars() if labelmap.GetPointData() else None
            if scalars is not None and np.any(vtk_to_numpy(scalars)):
                return False
        return True

    def computePreviewLabelmap(self, mergedImage, outputLabelmap):
        import vtkITK

        interpolator = vtkITK.vtkITKMorphologicalContourInterpolator()
        interpolator.SetInputData(mergedImage)
        interpolator.Update()
        outputLabelmap.ShallowCopy(interpolator.GetOutput())
        imageToWorld = vtk.vtkMatrix4x4()
        mergedImage.GetImageToWorldMatrix(imageToWorld)
        outputLabelmap.SetImageToWorldMatrix(imageToWorld)
