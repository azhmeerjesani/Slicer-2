import logging
import os

import qt
import vtk
import vtkITK

import slicer
from slicer.i18n import tr as _

from SegmentEditorEffects import *


class SegmentEditorLevelTracingEffect(AbstractScriptedSegmentEditorLabelEffect):
    """LevelTracingEffect is a LabelEffect implementing level tracing fill
    using intensity-based isolines
    """

    def __init__(self, scriptedEffect):
        scriptedEffect.name = "Level tracing"  # no tr (don't translate it because modules find effects by name)
        scriptedEffect.title = _("Level tracing")
        AbstractScriptedSegmentEditorLabelEffect.__init__(self, scriptedEffect)

        # Effect-specific members
        self.levelTracingPipelines = {}
        self.lastXY = None

    def clone(self):
        import qSlicerSegmentationsEditorEffectsPythonQt as effects

        clonedEffect = effects.qSlicerSegmentEditorScriptedLabelEffect(None)
        clonedEffect.setPythonSource(__file__.replace("\\", "/"))
        return clonedEffect

    def icon(self):
        iconPath = os.path.join(os.path.dirname(__file__), "Resources/Icons/LevelTracing.png")
        if os.path.exists(iconPath):
            return qt.QIcon(iconPath)
        return qt.QIcon()

    def helpText(self):
        return "<html>" + _("""Add uniform intensity region to selected segment<br>.
        <p><ul style="margin: 0">
        <li><b>Mouse move:</b> current background voxel is used to find a closed path that
        follows the same intensity value back to the starting point within the current slice.
        <li><b>Left-click:</b> add the previewed region to the current segment.
        </ul><p>""")

    def setupOptionsFrame(self):
        self.operationAddRadioButton = qt.QRadioButton(_("Add"))
        self.operationAddRadioButton.objectName = self.__class__.__name__ + "OperationAdd"
        self.operationAddRadioButton.setToolTip(_("Add the previewed region to the selected segment."))
        self.operationSubtractRadioButton = qt.QRadioButton(_("Subtract"))
        self.operationSubtractRadioButton.objectName = self.__class__.__name__ + "OperationSubtract"
        self.operationSubtractRadioButton.setToolTip(_("Remove the previewed region from the selected segment."))
        self.operationButtonGroup = qt.QButtonGroup()
        self.operationButtonGroup.addButton(self.operationAddRadioButton)
        self.operationButtonGroup.addButton(self.operationSubtractRadioButton)
        self.operationAddRadioButton.setChecked(True)
        operationLayout = qt.QHBoxLayout()
        operationLayout.addWidget(self.operationAddRadioButton)
        operationLayout.addWidget(self.operationSubtractRadioButton)
        operationLayout.addStretch(1)
        self.scriptedEffect.addLabeledOptionsWidget(_("Operation:"), operationLayout)
        self.operationSubtractRadioButton.connect("toggled(bool)", self.updateMRMLFromGUI)

        self.sliceRotatedErrorLabel = qt.QLabel()
        # This widget displays an error message if the slice view is not aligned
        # with the segmentation's axes.
        self.scriptedEffect.addOptionsWidget(self.sliceRotatedErrorLabel)

    def setMRMLDefaults(self):
        self.scriptedEffect.setParameterDefault("Operation", OUTLINE_OPERATION_ADD)

    def updateGUIFromMRML(self):
        subtract = isOutlineSubtracted(self.scriptedEffect)
        wasBlocked = self.operationSubtractRadioButton.blockSignals(True)
        self.operationSubtractRadioButton.setChecked(subtract)
        self.operationSubtractRadioButton.blockSignals(wasBlocked)
        wasBlocked = self.operationAddRadioButton.blockSignals(True)
        self.operationAddRadioButton.setChecked(not subtract)
        self.operationAddRadioButton.blockSignals(wasBlocked)

    def updateMRMLFromGUI(self):
        self.scriptedEffect.setParameter("Operation",
                                         OUTLINE_OPERATION_SUBTRACT if self.operationSubtractRadioButton.checked else OUTLINE_OPERATION_ADD)

    def activate(self):
        self.sliceRotatedErrorLabel.text = ""

    def deactivate(self):
        # Clear draw pipelines
        for sliceWidget, pipeline in self.levelTracingPipelines.items():
            self.scriptedEffect.removeActor2D(sliceWidget, pipeline.actor)
        self.levelTracingPipelines = {}
        self.lastXY = None

    def processInteractionEvents(self, callerInteractor, eventId, viewWidget):
        abortEvent = False

        # Only allow for slice views
        if viewWidget.className() != "qMRMLSliceWidget":
            return abortEvent
        # Get draw pipeline for current slice
        pipeline = self.pipelineForWidget(viewWidget)
        if pipeline is None:
            return abortEvent

        anyModifierKeyPressed = callerInteractor.GetShiftKey() or callerInteractor.GetControlKey() or callerInteractor.GetAltKey()

        if eventId == vtk.vtkCommand.LeftButtonPressEvent and not anyModifierKeyPressed:
            abortEvent = True

            # Make sure the user wants to do the operation, even if the segment is not visible
            if not self.scriptedEffect.confirmCurrentSegmentVisible():
                self.sliceRotatedErrorLabel.text = ""
                pipeline.actor.VisibilityOff()
                self.lastXY = None
                pipeline.sliceWidget.sliceView().scheduleRender()
                return abortEvent

            if pipeline.polyData.GetLines() is None or pipeline.polyData.GetLines().GetNumberOfCells() == 0:
                # No region is previewed, nothing to do (no undo state is saved)
                return abortEvent

            self.scriptedEffect.saveStateForUndo()

            # Get modifier labelmap
            modifierLabelmap = self.scriptedEffect.defaultModifierLabelmap()

            # Apply poly data on modifier labelmap
            pipeline.appendPolyMask(modifierLabelmap)
            # TODO: it would be nice to reduce extent
            if isOutlineSubtracted(self.scriptedEffect):
                modificationMode = slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeRemove
            else:
                modificationMode = slicer.qSlicerSegmentEditorAbstractEffect.ModificationModeAdd
            self.scriptedEffect.modifySelectedSegmentByLabelmap(modifierLabelmap, modificationMode)

        elif eventId == vtk.vtkCommand.MouseMoveEvent:
            if pipeline.actionState == "":
                xy = callerInteractor.GetEventPosition()
                if pipeline.preview(xy):
                    self.sliceRotatedErrorLabel.text = ""
                else:
                    self.sliceRotatedErrorLabel.text = (
                        '<b><font color="red">'
                        + _("Slice view is not aligned with segmentation axis.<br>To use this effect, click the 'Slice views orientation' warning button.")
                        + "</font></b>")
                abortEvent = True
                self.lastXY = xy
        elif eventId == vtk.vtkCommand.EnterEvent:
            self.sliceRotatedErrorLabel.text = ""
            pipeline.actor.VisibilityOn()
        elif eventId == vtk.vtkCommand.LeaveEvent:
            self.sliceRotatedErrorLabel.text = ""
            pipeline.actor.VisibilityOff()
            self.lastXY = None

        return abortEvent

    def processViewNodeEvents(self, callerViewNode, eventId, viewWidget):
        if callerViewNode and callerViewNode.IsA("vtkMRMLSliceNode"):
            # Get draw pipeline for current slice
            pipeline = self.pipelineForWidget(viewWidget)
            if pipeline is None:
                logging.error("processViewNodeEvents: Invalid pipeline")
                return

            # Update the preview to the new slice
            if pipeline.actionState == "" and self.lastXY:
                pipeline.preview(self.lastXY)

    def pipelineForWidget(self, sliceWidget):
        if sliceWidget in self.levelTracingPipelines:
            return self.levelTracingPipelines[sliceWidget]

        # Create pipeline if does not yet exist
        pipeline = LevelTracingPipeline(self, sliceWidget)

        # Add actor
        renderer = self.scriptedEffect.renderer(sliceWidget)
        if renderer is None:
            logging.error("setupPreviewDisplay: Failed to get renderer!")
            return None
        self.scriptedEffect.addActor2D(sliceWidget, pipeline.actor)

        self.levelTracingPipelines[sliceWidget] = pipeline
        return pipeline


#
# LevelTracingPipeline
#
class LevelTracingPipeline:
    """Visualization objects and pipeline for each slice view for level tracing"""

    def __init__(self, effect, sliceWidget):
        self.effect = effect
        self.sliceWidget = sliceWidget
        self.actionState = ""

        self.xyPoints = vtk.vtkPoints()
        self.rasPoints = vtk.vtkPoints()
        self.polyData = vtk.vtkPolyData()

        self.tracingFilter = vtkITK.vtkITKLevelTracingImageFilter()
        self.ijkToXY = vtk.vtkGeneralTransform()

        self.mapper = vtk.vtkPolyDataMapper2D()
        self.actor = vtk.vtkActor2D()
        self.mapper.SetInputData(self.polyData)
        self.actor.SetMapper(self.mapper)
        actorProperty = self.actor.GetProperty()
        actorProperty.SetColor(1, 1, 0)
        actorProperty.SetLineWidth(1)

    def preview(self, xy):
        """Calculate the current level trace view if the mouse is inside the volume extent
        Returns False if slice views are rotated.
        """

        # Get source volume image data
        sourceImageData = self.effect.scriptedEffect.sourceVolumeImageData()

        segmentationNode = self.effect.scriptedEffect.parameterSetNode().GetSegmentationNode()
        parentTransformNode = None
        if segmentationNode:
            parentTransformNode = segmentationNode.GetParentTransformNode()

        self.xyPoints.Reset()
        ijk = self.effect.xyToIjk(xy, self.sliceWidget, sourceImageData, parentTransformNode)
        dimensions = sourceImageData.GetDimensions()

        self.tracingFilter.SetInputData(sourceImageData)
        self.tracingFilter.SetSeed(ijk)

        # Select the plane corresponding to current slice orientation
        # for the input volume
        sliceNode = self.effect.scriptedEffect.viewNode(self.sliceWidget)
        offset = max(sliceNode.GetDimensions())

        i0, j0, k0 = self.effect.xyToIjk((0, 0), self.sliceWidget, sourceImageData, parentTransformNode)
        i1, j1, k1 = self.effect.xyToIjk((offset, offset), self.sliceWidget, sourceImageData, parentTransformNode)
        if i0 == i1:
            self.tracingFilter.SetPlaneToJK()
        elif j0 == j1:
            self.tracingFilter.SetPlaneToIK()
        elif k0 == k1:
            self.tracingFilter.SetPlaneToIJ()
        else:
            self.polyData.Reset()
            self.sliceWidget.sliceView().scheduleRender()
            return False

        self.tracingFilter.Update()
        polyData = self.tracingFilter.GetOutput()

        # Get source volume IJK to slice XY transform
        xyToRas = sliceNode.GetXYToRAS()
        rasToIjk = vtk.vtkMatrix4x4()
        sourceImageData.GetImageToWorldMatrix(rasToIjk)
        rasToIjk.Invert()
        xyToIjk = vtk.vtkGeneralTransform()
        xyToIjk.PostMultiply()
        xyToIjk.Concatenate(xyToRas)
        if parentTransformNode:
            worldToSegmentation = vtk.vtkMatrix4x4()
            parentTransformNode.GetMatrixTransformFromWorld(worldToSegmentation)
            xyToIjk.Concatenate(worldToSegmentation)
        xyToIjk.Concatenate(rasToIjk)
        ijkToXy = xyToIjk.GetInverse()
        if polyData.GetPoints():
            ijkToXy.TransformPoints(polyData.GetPoints(), self.xyPoints)
            self.polyData.DeepCopy(polyData)
            self.polyData.GetPoints().DeepCopy(self.xyPoints)
        else:
            self.polyData.Reset()
        self.sliceWidget.sliceView().scheduleRender()
        return True

    def appendPolyMask(self, modifierLabelmap):
        lines = self.polyData.GetLines()
        if lines.GetNumberOfCells() == 0:
            return

        # Apply poly data on modifier labelmap
        segmentationNode = self.effect.scriptedEffect.parameterSetNode().GetSegmentationNode()
        self.effect.scriptedEffect.appendPolyMask(modifierLabelmap, self.polyData, self.sliceWidget, segmentationNode)


def isOutlineSubtracted(scriptedEffect):
    """Return True if the "Operation" parameter of the effect is set to subtract the region (default: add)"""
    # Parameters may be missing (for example, in scenes saved by Slicer versions that did not have them)
    return scriptedEffect.parameterDefined("Operation") and scriptedEffect.parameter("Operation") == OUTLINE_OPERATION_SUBTRACT


OUTLINE_OPERATION_ADD = "Add"
OUTLINE_OPERATION_SUBTRACT = "Subtract"
