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
        # 1 in the studio presentation (Fill gaps): also fill the gaps between parts that are several slices thick, along
        # the slicing axis of the Red view. 0 (default, Classic) keeps the result of official 3D Slicer.
        self.scriptedEffect.setParameterDefault("FillGapsBetweenThickParts", 0)

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
        if (self.scriptedEffect.parameterDefined("FillGapsBetweenThickParts")
                and self.scriptedEffect.integerParameter("FillGapsBetweenThickParts") != 0):
            self.fillGapsBetweenThickParts(mergedImage, outputLabelmap, self.sliceViewArrayAxis(imageToWorld))

    @staticmethod
    def sliceViewArrayAxis(imageToWorld, viewName="Red"):
        """Array axis (0 = k, 1 = j, 2 = i) of an image whose direction is closest to the slice normal of the view
        ``viewName`` (the axis along which the Lesion card counts gaps), else closest to S.
        """
        normal = [0.0, 0.0, 1.0]
        layoutManager = slicer.app.layoutManager()
        sliceWidget = layoutManager.sliceWidget(viewName) if layoutManager is not None else None
        if sliceWidget is not None:
            sliceToRas = sliceWidget.mrmlSliceNode().GetSliceToRAS()
            normal = [sliceToRas.GetElement(row, 2) for row in range(3)]
        best, bestCos = 0, -1.0
        for axis, column in ((0, 2), (1, 1), (2, 0)):
            direction = [imageToWorld.GetElement(row, column) for row in range(3)]
            length = vtk.vtkMath.Norm(direction) * (vtk.vtkMath.Norm(normal) or 1.0)
            cos = abs(vtk.vtkMath.Dot(direction, normal)) / (length or 1.0)
            if cos > bestCos:
                best, bestCos = axis, cos
        return best

    @staticmethod
    def fillGapsBetweenThickParts(mergedImage, outputLabelmap, axis):
        """Also fill empty slices between parts that are several slices thick, along the array axis ``axis`` only
        (0 = k, 1 = j, 2 = i: the slicing axis).

        The contour interpolator only interpolates from a slice whose direct neighbors are empty, so a gap
        between two parts that span several slices is left open. The interpolation along ``axis`` is repeated
        on a copy that keeps, for each segment, only the slices that face a gap (the last slice before and the
        first slice after each run of slices that are empty for that segment), and only the voxels it adds inside
        those empty slices are added to the output. Nothing that the plain interpolation produced is removed, and
        nothing is added on a slice that holds any voxel of the segment: two parts side by side within the slices
        are never joined.
        """
        import numpy as np
        import vtkITK
        from vtk.util import numpy_support

        inputScalars = mergedImage.GetPointData().GetScalars()
        outputScalars = outputLabelmap.GetPointData().GetScalars()
        if inputScalars is None or outputScalars is None or inputScalars.GetNumberOfComponents() != 1:
            return
        dims = mergedImage.GetDimensions()
        shape = (dims[2], dims[1], dims[0])
        if tuple(outputLabelmap.GetDimensions()) != tuple(dims):
            return
        source = numpy_support.vtk_to_numpy(inputScalars).reshape(shape)
        result = numpy_support.vtk_to_numpy(outputScalars).reshape(shape)
        labels = [int(label) for label in np.unique(source) if label != 0]
        changed = False
        otherAxes = tuple(other for other in range(3) if other != axis)
        reduced = np.zeros_like(source)
        gapSlicesByLabel = {}
        needed = False
        sourceView = np.moveaxis(source, axis, 0)
        reducedView = np.moveaxis(reduced, axis, 0)
        for label in labels:
            occupied = np.flatnonzero((source == label).any(axis=otherAxes))
            if occupied.size < 2:
                continue
            jumps = np.diff(occupied) > 1
            if not jumps.any():
                continue
            before, after = occupied[:-1][jumps], occupied[1:][jumps]
            facing = np.union1d(before, after)
            # Without a part that is thicker than one slice, the plain interpolation has already filled the gaps
            if occupied.size > facing.size:
                needed = True
            gaps = np.zeros(shape[axis], dtype=bool)
            for first, last in zip(before, after, strict=True):
                gaps[first + 1:last] = True
            gapSlicesByLabel[label] = gaps
            reducedView[facing] = np.where(sourceView[facing] == label, label, reducedView[facing])
        if not needed:
            return
        image = vtk.vtkImageData()
        image.SetDimensions(dims)
        image.SetSpacing(mergedImage.GetSpacing())
        image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(
            reduced.ravel(), deep=True, array_type=inputScalars.GetDataType()))
        interpolator = vtkITK.vtkITKMorphologicalContourInterpolator()
        interpolator.SetInputData(image)
        interpolator.SetAxis(2 - axis)  # ITK axes are x, y, z; the array axes are z, y, x
        interpolator.Update()
        interpolated = numpy_support.vtk_to_numpy(interpolator.GetOutput().GetPointData().GetScalars()).reshape(shape)
        for label, gaps in gapSlicesByLabel.items():
            inGaps = np.zeros(shape, dtype=bool)
            np.moveaxis(inGaps, axis, 0)[gaps] = True
            added = inGaps & (interpolated == label) & (source == 0) & (result == 0)
            if added.any():
                result[added] = label
                changed = True
        if changed:
            outputScalars.Modified()
            outputLabelmap.Modified()
