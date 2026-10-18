"""Start-up and round-trip check of a packaged Slicer Studio application.

Run by the package builds (macOS disk image, Linux tarball) on the packaged
application, with temporary settings:

    Slicer --no-splash --disable-settings --ignore-slicerrc --exit-after-startup
        --python-script SlicerStudioSmokeCheck.py

It prints ``SLICER_STUDIO_SMOKE_OK`` when every check passed. The caller must
also fail when the log contains a Python traceback: errors raised in start-up
callbacks (for example while the Studio shell is applied) are printed but do
not stop the application.
"""

import os
import shutil
import tempfile

import numpy as np
import qt
import slicer
import ssl


def check(condition, message):
    if not condition:
        raise RuntimeError("start-up test failed: " + message)


mainWindow = slicer.util.mainWindow()
check(mainWindow is not None, "no main window")
missing = [name for name in ("volumes", "segmenteditor", "markups", "data") if not hasattr(slicer.modules, name)]
check(not missing, "missing modules %s" % missing)
hasStudio = hasattr(slicer.modules, "studio")
print("SLICER_STUDIO_SMOKE version={} qt={} ssl={} mainWindow={} studio={}".format(
    slicer.app.applicationVersion, qt.qVersion(), ssl.OPENSSL_VERSION, mainWindow is not None, hasStudio))

studioShell = None
if hasStudio:
    from StudioLib import shell

    # Studio is on by default (SlicerDefaultSettings.ini): the shell must have been applied without errors.
    studioShell = shell.instance()
    check(studioShell is not None, "the Studio shell is not active")
    check(mainWindow.findChild("QWidget", "StudioTopBar") is not None, "the Studio top bar is missing")

# A small volume with a "lesion", traced into a segmentation.
volumeArray = np.zeros((48, 64, 64), dtype=np.int16)
volumeArray[8:40, 8:56, 8:56] = 100
volumeNode = slicer.util.addVolumeFromArray(volumeArray, name="SmokeVolume")
segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "SmokeLesions")
segmentationNode.CreateDefaultDisplayNodes()
segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
segmentId = segmentationNode.GetSegmentation().AddEmptySegment("", "Lesion")
lesion = np.zeros(volumeArray.shape, dtype=np.uint8)
lesion[20:28, 28:36, 28:36] = 1
slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentationNode, segmentId, volumeNode)

if studioShell is not None:
    for key in ("draw", "library", "draw"):
        studioShell.switchTo(key)
        slicer.app.processEvents()
        check(studioShell.currentWorkspace() == key, "could not switch to the %s workspace" % key)
    studioShell.switchTo("library")
    slicer.app.processEvents()

# Files written here must open in official 3D Slicer: a scene bundle and a NIfTI label map.
outputDir = tempfile.mkdtemp(prefix="slicer-studio-smoke-")
try:
    labelmapNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", "SmokeLesionLabel")
    slicer.modules.segmentations.logic().ExportVisibleSegmentsToLabelmapNode(segmentationNode, labelmapNode, volumeNode)
    niftiPath = os.path.join(outputDir, "lesion.nii.gz")
    check(slicer.util.saveNode(labelmapNode, niftiPath), "could not save %s" % niftiPath)
    scenePath = os.path.join(outputDir, "scene.mrb")
    check(slicer.util.saveScene(scenePath), "could not save %s" % scenePath)

    slicer.mrmlScene.Clear(0)
    slicer.app.processEvents()
    slicer.util.loadScene(scenePath)
    reloaded = slicer.util.getFirstNodeByClassByName("vtkMRMLSegmentationNode", "SmokeLesions")
    check(reloaded is not None, "the segmentation is missing after reloading the scene")
    reloadedLesion = slicer.util.arrayFromSegmentBinaryLabelmap(
        reloaded, reloaded.GetSegmentation().GetSegmentIdBySegmentName("Lesion"), slicer.util.getNode("SmokeVolume"))
    check(int(reloadedLesion.sum()) == int(lesion.sum()), "the lesion changed after reloading the scene")
    reloadedLabel = slicer.util.loadLabelVolume(niftiPath)
    check(int((slicer.util.arrayFromVolume(reloadedLabel) > 0).sum()) == int(lesion.sum()),
          "the lesion changed after reloading the NIfTI file")
finally:
    shutil.rmtree(outputDir, ignore_errors=True)

print("SLICER_STUDIO_SMOKE_OK")
