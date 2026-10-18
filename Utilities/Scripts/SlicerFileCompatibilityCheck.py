#!/usr/bin/env python3
r"""Check that files saved by one Slicer installation load correctly in another one.

For every ordered pair (writer A, checker B) of the given Slicer installations, a generated
writer script runs headless in A and builds a small test scene:

- a scalar volume (oblique, nearest display interpolation, fixed window/level, node attribute)
- a segmentation with a "Lesion" segment (binary labelmap source + closed surface) and a second segment
- a markups point list (labels, descriptions, selected/locked states)
- a table node (string, integer and floating point columns)
- a diverging color table node with alpha, used by a T-map volume
- a scripted module parameter node (parameters and a node reference)
- a 3D view with a non-default background
- the current layout: 7001 "Studio Focus" for a fork writer, Conventional for an official writer

The scene is saved twice: as a scene file with its data files in a directory, and as a ``.mrb``
bundle. A fork writer also runs the lesion network mapping command-line interface
(``python -m LesionNetworkMappingLib.engine``) on a small synthetic connectome, when
LesionNetworkMappingLib is available (in the writer or in the source tree), and saves its NIfTI outputs.

A generated checker script then runs headless in B, loads both saved scenes and the NIfTI outputs, and
records what it sees. This script compares that with what A recorded before saving:

- same node classes and counts (every singleton node saved in the scene exists after loading it),
  identical voxel data, geometry, segments, markup coordinates, table contents and color table
  entries (including alpha), parameter node content and view settings;
- working views: a fork scene saved with layout 7001 shows slice views in official Slicer (portable
  fallback to the Conventional layout) and the fork restores layout 7001; no widget of the application
  covers the slice views (a canvas that the fork's Studio interface shows over the views, such as its
  Library, is reported and hidden for the checks);
- no new errors or warnings in the application log, compared with B loading the scene B wrote itself;
- the NIfTI outputs load with identical geometry and voxel values.

Each Slicer process runs with fresh, isolated settings (a temporary HOME), so the user's settings are
neither used nor modified. A report is written as JSON (``report.json``) and as text (``report.txt``)
into the output directory, with screenshots in ``shots/``. The exit code is non-zero if any check fails.

Usage::

  SlicerFileCompatibilityCheck.py --slicer NAME=PATH [--slicer NAME=PATH ...] --out DIR
      [--pairs all|cross|WRITER:CHECKER[,WRITER:CHECKER...]] [--fork NAME ...] [--testing]
      [--timeout SECONDS] [--no-xvfb] [--lnm-lib-dir DIR]

  --slicer NAME=PATH  a Slicer launcher to test (repeatable). NAMEs starting with "fork" are treated as
                      the fork, unless --fork is given.
  --out DIR           output directory (created; previous results in it are replaced)
  --pairs             "all" (default): every ordered pair including a Slicer with itself;
                      "cross": only pairs of different installations; or an explicit list
  --fork NAME         name of a fork installation (repeatable)
  --testing           start Slicer with --testing (by default the normal user interface is used)
  --timeout SECONDS   time limit for each Slicer process (default: 900)
  --no-xvfb           do not wrap the Slicer processes in xvfb-run
  --lnm-lib-dir DIR   directory that contains the LesionNetworkMappingLib package, used when a fork
                      writer cannot import it (default: the source tree next to this script)

Example::

  SlicerFileCompatibilityCheck.py --slicer fork=/opt/S-r/Slicer-build/Slicer \\
      --slicer official-5.12.4=/opt/slicer-dl/Slicer-5.12.4-linux-amd64/Slicer \\
      --slicer official-5.13=/opt/slicer-dl/Slicer-5.13.0-2026-10-16-linux-amd64/Slicer \\
      --out /tmp/compat --pairs all

The code that runs inside Slicer only uses public APIs that exist in Slicer 5.12.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

# -----------------------------------------------------------------------------
# Constants shared by the harness and the scripts that run inside Slicer

SCENE_NAME = "CompatScene"
VOLUME_NAME = "CompatVolume"
TMAP_NAME = "CompatTMap"
COLOR_TABLE_NAME = "CompatDivergingLUT"
SEGMENTATION_NAME = "CompatSegmentation"
POINT_LIST_NAME = "CompatPoints"
TABLE_NAME = "CompatTable"
PARAMETER_NODE_NAME = "CompatParameters"
PARAMETER_MODULE_NAME = "SlicerFileCompatibilityCheck"

CONVENTIONAL_LAYOUT = 2  # vtkMRMLLayoutNode::SlicerLayoutConventionalView
STUDIO_FOCUS_LAYOUT = 7001
STUDIO_LAYOUT_ID_RANGE = (7001, 7099)  # fork-reserved user layout ids, written portably

# Canonical layout descriptions of the fork (SPEC 9.7 rule 3). The fork registers them in C++
# (vtkMRMLLayoutLogic); they are only registered from here when a fork build does not have them.
_SLICE_RED = (
    '<view class="vtkMRMLSliceNode" singletontag="Red"><property name="orientation" action="default">Axial</property>'
    '<property name="viewlabel" action="default">R</property><property name="viewcolor" action="default">#F34A33</property></view>'
)
_SLICE_GREEN = (
    '<view class="vtkMRMLSliceNode" singletontag="Green"><property name="orientation" action="default">Coronal</property>'
    '<property name="viewlabel" action="default">G</property><property name="viewcolor" action="default">#6EB04B</property></view>'
)
_SLICE_YELLOW = (
    '<view class="vtkMRMLSliceNode" singletontag="Yellow"><property name="orientation" action="default">Sagittal</property>'
    '<property name="viewlabel" action="default">Y</property><property name="viewcolor" action="default">#EDD54C</property></view>'
)
_VIEW_1 = '<view class="vtkMRMLViewNode" singletontag="1"><property name="viewlabel" action="default">1</property></view>'
STUDIO_LAYOUTS = {
    7001: (
        f'<layout type="horizontal" split="true"><item splitSize="720">{_SLICE_RED}</item><item splitSize="280">'
        f'<layout type="vertical" split="true"><item>{_SLICE_GREEN}</item><item>{_SLICE_YELLOW}</item></layout></item></layout>'
    ),
    7002: (
        f'<layout type="horizontal" split="true"><item splitSize="720">{_SLICE_RED}</item><item splitSize="280">'
        f'<layout type="vertical" split="true"><item>{_SLICE_GREEN}</item><item>{_SLICE_YELLOW}</item><item>{_VIEW_1}</item></layout></item></layout>'
    ),
    7003: f'<layout type="horizontal" split="true"><item>{_SLICE_RED}</item><item>{_SLICE_GREEN}</item><item>{_SLICE_YELLOW}</item></layout>',
}

# A writer saves the scene as a scene file with data files in a directory ("dir") and as a bundle ("mrb")
SCENE_FORMATS = ("dir", "mrb")

# Messages that the application emits asynchronously for reasons unrelated to the loaded files
# (no network access in the test environment, software rendering). They are reported but ignored.
ENVIRONMENT_NOISE = (
    r"Failed to download extension metadata",
    r"[Ee]xtensions? (server|catalog|manager)",
    r"QNetworkReply|Network (access|error)|Host .* not found|Connection refused|SSL",
    r"Error retrieving extensions? (metadata|update)",
    r"libGL|MESA|llvmpipe|GLX|QXcbConnection|XDG_RUNTIME_DIR",
    r"QOpenGLWidget|QVTKOpenGL",
)

ERROR_LEVELS = ("Error", "Critical", "Fatal")
WARNING_LEVELS = ("Warning",)

# Tolerances
GEOMETRY_TOLERANCE = 1e-5  # mm, matrix elements (text formats round doubles)
COLOR_TOLERANCE = 0.6 / 255.0  # color tables store 8-bit components
VALUE_TOLERANCE = 1e-6


# =============================================================================
# Code that runs inside Slicer (writer and checker). It imports Slicer modules lazily.
# =============================================================================


def runInSlicer(configPath):
    """Entry point of the generated writer and checker scripts."""
    import slicer

    with open(configPath, encoding="utf-8") as f:
        config = json.load(f)
    result = {"role": config["role"], "name": config["name"]}
    try:
        result["info"] = _slicerInfo()
        if config["role"] == "writer":
            result.update(_runWriter(config))
        else:
            result.update(_runChecker(config))
    except Exception:
        result["exception"] = traceback.format_exc()
        print(result["exception"], file=sys.stderr)
    try:
        with open(config["resultPath"], "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1, sort_keys=True, default=str)
    finally:
        slicer.app.exit(0)


def _processEvents(times=3):
    import slicer

    for _ in range(times):
        slicer.app.processEvents()


def _layoutNode():
    import slicer

    return slicer.app.layoutManager().layoutLogic().GetLayoutNode()


def _slicerInfo():
    import slicer

    layoutNode = _layoutNode()
    studioLayouts = {}
    for layoutId, description in STUDIO_LAYOUTS.items():
        registered = bool(layoutNode.IsLayoutDescription(layoutId))
        studioLayouts[str(layoutId)] = {
            "registered": registered,
            "canonical": registered and layoutNode.GetLayoutDescription(layoutId) == description,
        }
    return {
        "version": slicer.app.applicationVersion,
        "revision": slicer.app.repositoryRevision,
        "testing": bool(slicer.app.testingEnabled()),
        # Captured at startup, before anything is registered from this script
        "studioLayouts": studioLayouts,
    }


class _LogCapture:
    """Collect the application log entries (error log model) emitted between start() and stop()."""

    def __init__(self):
        import slicer

        self.model = slicer.app.errorLogModel()
        self.first = 0

    def start(self):
        _processEvents()
        self.first = self.model.rowCount()

    def stop(self):
        _processEvents()
        entries = []
        for row in range(self.first, self.model.rowCount()):
            level = self.model.index(row, 2).data()
            if level not in ERROR_LEVELS + WARNING_LEVELS:
                continue
            entries.append({
                "level": level,
                "origin": self.model.index(row, 3).data(),
                "text": self.model.logEntryDescription(row),
            })
        self.first = self.model.rowCount()
        return entries


def _md5(array):
    import hashlib

    import numpy as np

    return hashlib.md5(np.ascontiguousarray(array).tobytes()).hexdigest()


def _matrixToList(matrix):
    return [matrix.GetElement(row, column) for row in range(4) for column in range(4)]


def _nodeByName(name, className):
    import slicer

    nodes = slicer.mrmlScene.GetNodesByClassByName(className, name)
    node = nodes.GetItemAsObject(0) if nodes.GetNumberOfItems() > 0 else None
    return node


def _attributes(node):
    names = node.GetAttributeNames() or ()
    return {name: node.GetAttribute(name) for name in sorted(names)}


# -----------------------------------------------------------------------------
# Test scene


def _buildScene():
    """Create the test scene content (deterministic, no network access)."""
    import math

    import numpy as np
    import vtk

    import slicer

    scene = slicer.mrmlScene

    # Scalar volume on an oblique grid
    volume = scene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", VOLUME_NAME)
    k, j, i = np.indices((24, 30, 36))
    voxels = ((i * 7 + j * 13 + k * 29) % 1000 - 200).astype(np.int16)
    voxels[((i - 18) / 9.0) ** 2 + ((j - 15) / 7.0) ** 2 + ((k - 12) / 6.0) ** 2 <= 1.0] = 900
    slicer.util.updateVolumeFromArray(volume, voxels)
    volume.SetSpacing(0.75, 1.25, 1.5)
    volume.SetOrigin(-12.5, 20.25, -30.0)
    angle = math.radians(20.0)
    directions = vtk.vtkMatrix4x4()
    for row, values in enumerate(([-math.cos(angle), math.sin(angle), 0.0], [-math.sin(angle), -math.cos(angle), 0.0], [0.0, 0.0, 1.0])):
        for column, value in enumerate(values):
            directions.SetElement(row, column, value)
    volume.SetIJKToRASDirectionMatrix(directions)
    volume.SetAttribute("Compat.Role", "anatomy")
    volume.CreateDefaultDisplayNodes()
    displayNode = volume.GetDisplayNode()
    displayNode.SetAndObserveColorNodeID("vtkMRMLColorTableNodeGrey")
    displayNode.SetInterpolate(0)  # nearest neighbor display interpolation
    displayNode.SetAutoWindowLevel(0)
    displayNode.SetWindowLevel(800.0, 150.0)

    # Diverging color table with alpha (blue - transparent - red), 8-bit exact components
    colorTable = scene.AddNewNodeByClass("vtkMRMLColorTableNode", COLOR_TABLE_NAME)
    colorTable.SetTypeToUser()
    colorTable.SetNumberOfColors(256)
    for index in range(256):
        t = index / 255.0
        if t < 0.5:
            rgb = (round(255 * 2 * t), round(255 * 2 * t), 255)
        else:
            rgb = (255, round(255 * 2 * (1 - t)), round(255 * 2 * (1 - t)))
        alpha = round(255 * min(1.0, abs(2 * t - 1) * 1.5))
        colorTable.SetColor(index, f"c{index:03d}", rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0, alpha / 255.0)

    # T-map (float) using the diverging color table
    tmap = scene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", TMAP_NAME)
    k, j, i = np.indices((22, 26, 20)).astype(np.float32)
    tvalues = (6.0 * np.sin(i / 3.0) * np.cos(j / 4.0) + (k - 11.0) / 4.0).astype(np.float32)
    slicer.util.updateVolumeFromArray(tmap, tvalues)
    tmap.SetSpacing(2.0, 2.0, 2.0)
    tmap.SetOrigin(-19.0, -25.0, -21.0)
    tmap.SetAttribute("Compat.Role", "tmap")
    tmap.CreateDefaultDisplayNodes()
    tmapDisplay = tmap.GetDisplayNode()
    tmapDisplay.SetAndObserveColorNodeID(colorTable.GetID())
    tmapDisplay.SetAutoWindowLevel(0)
    tmapDisplay.SetWindowLevel(12.0, 0.0)
    tmapDisplay.SetInterpolate(1)

    # Segmentation: "Lesion" and a second segment, binary labelmap source + closed surface
    segmentation = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", SEGMENTATION_NAME)
    segmentation.CreateDefaultDisplayNodes()
    segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
    k, j, i = np.indices(voxels.shape)
    lesionId = segmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion", [0.9, 0.2, 0.2])
    lesion = (((i - 20) / 6.0) ** 2 + ((j - 14) / 5.0) ** 2 + ((k - 11) / 4.0) ** 2 <= 1.0).astype(np.uint8)
    slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, lesionId, volume)
    otherId = segmentation.GetSegmentation().AddEmptySegment("Other", "Other", [0.2, 0.5, 0.9])
    other = ((i >= 4) & (i < 10) & (j >= 20) & (j < 27) & (k >= 3) & (k < 9)).astype(np.uint8)
    slicer.util.updateSegmentBinaryLabelmapFromArray(other, segmentation, otherId, volume)
    segmentation.CreateClosedSurfaceRepresentation()

    # Markups point list
    pointList = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", POINT_LIST_NAME)
    for index, (position, label) in enumerate((([10.5, -20.25, 30.0], "P-1"), ([-5.0, 12.75, -8.5], "P-2"), ([0.0, 0.0, 0.0], "Origin"), ([33.25, 4.5, -17.75], "P-4"))):
        pointList.AddControlPoint(vtk.vtkVector3d(*position), label)
        pointList.SetNthControlPointDescription(index, f"description {index}")
    pointList.SetNthControlPointSelected(1, False)
    pointList.SetNthControlPointLocked(2, True)

    # Table with typed columns (values that are exact in the text format)
    table = scene.AddNewNodeByClass("vtkMRMLTableNode", TABLE_NAME)
    columns = (
        ("Subject", vtk.vtkStringArray(), ["sub-001", "sub-002", "sub 003", "sub-004", "sub-005"]),
        ("VolumeMm3", vtk.vtkDoubleArray(), [1024.25, 0.5, 12.125, 3000.0, -7.75]),
        ("Count", vtk.vtkIntArray(), [12, 0, -3, 1024, 7]),
    )
    for name, array, values in columns:
        array.SetName(name)
        for value in values:
            array.InsertNextValue(value)
        table.AddColumn(array)

    # Scripted module parameter node
    parameterNode = scene.AddNewNodeByClass("vtkMRMLScriptedModuleNode", PARAMETER_NODE_NAME)
    parameterNode.SetModuleName(PARAMETER_MODULE_NAME)
    parameterNode.SetParameter("Threshold", "2.5")
    parameterNode.SetParameter("Mode", "focus")
    parameterNode.SetParameter("Text", 'a < b & "c"')
    parameterNode.SetNodeReferenceID("InputVolume", volume.GetID())
    parameterNode.SetNodeReferenceID("ColorTable", colorTable.GetID())

    # 3D view with a non-default background
    viewNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLViewNode")
    viewNode.SetBackgroundColor(0.1, 0.2, 0.3)
    viewNode.SetBackgroundColor2(0.05, 0.1, 0.15)

    slicer.util.setSliceViewerLayers(background=volume, foreground=tmap, foregroundOpacity=0.5, fit=True)


def _sceneNodeIds():
    import slicer

    scene = slicer.mrmlScene
    return {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes()) if scene.GetNthNode(index)}


def _addedNodeClassCounts(baselineIds, savedOnly):
    """Count the nodes added since ``baselineIds`` was recorded, by class.

    Singleton nodes are not counted: they are merged into the existing node of the same singleton tag
    on load, and whether they exist before depends on the application state (modules used before) and
    on the scenes loaded before. Every singleton node saved in a scene must exist after loading it
    (see ``savedSingletons`` and ``_sceneSingletons``).
    """
    import slicer

    scene = slicer.mrmlScene
    counts = {}
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        if not node or node.GetID() in baselineIds or node.GetSingletonTag():
            continue
        if savedOnly and not node.GetSaveWithScene():
            continue
        counts[node.GetClassName()] = counts.get(node.GetClassName(), 0) + 1
    return counts


def _sceneSingletons():
    """Return the sorted "<XML tag>/<singleton tag>" keys of the singleton nodes of the scene."""
    import slicer

    scene = slicer.mrmlScene
    keys = set()
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        if node and node.GetSingletonTag():
            keys.add(f"{node.GetNodeTagName()}/{node.GetSingletonTag()}")
    return sorted(keys)


def _collectState():
    """Return a JSON-serializable description of the test scene content."""
    state = {}
    for key, function in (
        ("volume", lambda: _volumeState(_nodeByName(VOLUME_NAME, "vtkMRMLScalarVolumeNode"))),
        ("tmap", lambda: _volumeState(_nodeByName(TMAP_NAME, "vtkMRMLScalarVolumeNode"))),
        ("colorTable", lambda: _colorTableState(_nodeByName(COLOR_TABLE_NAME, "vtkMRMLColorTableNode"))),
        ("segmentation", _segmentationState),
        ("pointList", lambda: _pointListState(_nodeByName(POINT_LIST_NAME, "vtkMRMLMarkupsFiducialNode"))),
        ("table", lambda: _tableState(_nodeByName(TABLE_NAME, "vtkMRMLTableNode"))),
        ("parameterNode", lambda: _parameterNodeState(_nodeByName(PARAMETER_NODE_NAME, "vtkMRMLScriptedModuleNode"))),
        ("view3D", _view3DState),
        ("sliceComposite", _sliceCompositeState),
    ):
        try:
            state[key] = function()
        except Exception:
            state[key] = {"error": traceback.format_exc()}
    return state


def _volumeState(volume):
    import vtk

    import slicer

    if volume is None:
        return None
    voxels = slicer.util.arrayFromVolume(volume)
    matrix = vtk.vtkMatrix4x4()
    volume.GetIJKToRASMatrix(matrix)
    displayNode = volume.GetDisplayNode()
    display = None
    if displayNode:
        colorNode = displayNode.GetColorNode()
        display = {
            "class": displayNode.GetClassName(),
            "interpolate": displayNode.GetInterpolate(),
            "autoWindowLevel": displayNode.GetAutoWindowLevel(),
            "window": displayNode.GetWindow(),
            "level": displayNode.GetLevel(),
            "colorNode": colorNode.GetName() if colorNode else None,
        }
    return {
        "class": volume.GetClassName(),
        "shape": list(voxels.shape),
        "dtype": str(voxels.dtype),
        "md5": _md5(voxels),
        "ijkToRas": _matrixToList(matrix),
        "attributes": {name: value for name, value in _attributes(volume).items() if name.startswith("Compat.")},
        "display": display,
    }


def _colorTableState(colorTable):
    if colorTable is None:
        return None
    entries = []
    for index in range(colorTable.GetNumberOfColors()):
        rgba = [0.0, 0.0, 0.0, 0.0]
        colorTable.GetColor(index, rgba)
        entries.append([index, colorTable.GetColorName(index), *rgba])
    return {"class": colorTable.GetClassName(), "numberOfColors": colorTable.GetNumberOfColors(), "entries": entries}


def _segmentationState():
    import slicer

    segmentationNode = _nodeByName(SEGMENTATION_NAME, "vtkMRMLSegmentationNode")
    if segmentationNode is None:
        return None
    referenceVolume = _nodeByName(VOLUME_NAME, "vtkMRMLScalarVolumeNode")
    segmentation = segmentationNode.GetSegmentation()
    segments = []
    for index in range(segmentation.GetNumberOfSegments()):
        segmentId = segmentation.GetNthSegmentID(index)
        segment = segmentation.GetSegment(segmentId)
        mask = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, referenceVolume) != 0
        surface = segmentationNode.GetClosedSurfaceInternalRepresentation(segmentId) if segmentation.ContainsRepresentation("Closed surface") else None
        segments.append({
            "id": segmentId,
            "name": segment.GetName(),
            "color": list(segment.GetColor()),
            "voxels": int(mask.sum()),
            "md5": _md5(mask.astype("uint8")),
            "closedSurfacePoints": surface.GetNumberOfPoints() if surface else 0,
        })
    return {
        "class": segmentationNode.GetClassName(),
        "sourceRepresentation": segmentation.GetSourceRepresentationName(),
        "closedSurface": bool(segmentation.ContainsRepresentation("Closed surface")),
        "segments": segments,
    }


def _pointListState(pointList):
    if pointList is None:
        return None
    points = []
    for index in range(pointList.GetNumberOfControlPoints()):
        position = [0.0, 0.0, 0.0]
        pointList.GetNthControlPointPosition(index, position)
        points.append({
            "label": pointList.GetNthControlPointLabel(index),
            "description": pointList.GetNthControlPointDescription(index),
            "position": position,
            "selected": bool(pointList.GetNthControlPointSelected(index)),
            "locked": bool(pointList.GetNthControlPointLocked(index)),
        })
    return {"class": pointList.GetClassName(), "points": points}


def _tableState(table):
    if table is None:
        return None
    columns = []
    for column in range(table.GetNumberOfColumns()):
        name = table.GetColumnName(column)
        columns.append({
            "name": name,
            "type": table.GetColumnType(name),
            "cells": [table.GetCellText(row, column) for row in range(table.GetNumberOfRows())],
        })
    return {"class": table.GetClassName(), "columns": columns}


def _parameterNodeState(parameterNode):
    if parameterNode is None:
        return None
    references = {}
    for role in ("InputVolume", "ColorTable"):
        referenced = parameterNode.GetNodeReference(role)
        references[role] = referenced.GetName() if referenced else None
    return {
        "class": parameterNode.GetClassName(),
        "moduleName": parameterNode.GetModuleName(),
        "parameters": {name: parameterNode.GetParameter(name) for name in sorted(parameterNode.GetParameterNames())},
        "references": references,
    }


def _view3DState():
    import slicer

    viewNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLViewNode")
    return {"backgroundColor": list(viewNode.GetBackgroundColor()), "backgroundColor2": list(viewNode.GetBackgroundColor2())}


def _sliceCompositeState():
    import slicer

    compositeNode = slicer.app.layoutManager().sliceWidget("Red").mrmlSliceCompositeNode()
    background = slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID() or "")
    foreground = slicer.mrmlScene.GetNodeByID(compositeNode.GetForegroundVolumeID() or "")
    return {
        "background": background.GetName() if background else None,
        "foreground": foreground.GetName() if foreground else None,
        "foregroundOpacity": compositeNode.GetForegroundOpacity(),
    }


def _viewsState():
    """Describe the current layout and whether its views are shown and render the scene."""
    import numpy as np
    import vtk
    from vtk.util import numpy_support

    import slicer

    _processEvents()
    layoutManager = slicer.app.layoutManager()
    layoutNode = _layoutNode()
    try:
        viewport = layoutManager.viewport()
    except Exception:
        viewport = None
    ancestor = viewport if viewport is not None else slicer.util.mainWindow()
    sliceViews = {}
    covered = {}
    for name in layoutManager.sliceViewNames():
        widget = layoutManager.sliceWidget(name)
        sliceViews[name] = bool(widget.isVisibleTo(ancestor)) and widget.width > 1 and widget.height > 1
        coveringWidget = _coveringWidget(widget.sliceView()) if sliceViews[name] else None
        if coveringWidget:
            covered[name] = coveringWidget
    threeDViews = 0
    for index in range(layoutManager.threeDViewCount):
        widget = layoutManager.threeDWidget(index)
        threeDViews += 1 if widget.isVisibleTo(ancestor) else 0

    render = {"status": "skipped", "reason": "Red slice view is not shown"}
    redWidget = layoutManager.sliceWidget("Red")
    if not (viewport is None or viewport.isVisible()):
        render = {"status": "skipped", "reason": "the view area is hidden by the application user interface"}
    elif redWidget is not None and sliceViews.get("Red"):
        sliceView = redWidget.sliceView()
        sliceView.forceRender()
        # Render even if the application paused rendering, to check what the view would show
        sliceView.renderWindow().Render()
        windowToImage = vtk.vtkWindowToImageFilter()
        windowToImage.SetInput(sliceView.renderWindow())
        windowToImage.SetInputBufferTypeToRGB()
        windowToImage.ReadFrontBufferOff()
        windowToImage.Update()
        image = windowToImage.GetOutput()
        width, height, _ = image.GetDimensions()
        pixels = numpy_support.vtk_to_numpy(image.GetPointData().GetScalars()).reshape(height, width, -1)
        center = pixels[height // 4 : 3 * height // 4, width // 4 : 3 * width // 4].reshape(-1, pixels.shape[-1])
        render = {
            "status": "ok",
            "size": [width, height],
            "distinctColors": len(np.unique(center, axis=0)),
            "std": float(center.std()),
        }
    return {
        "layout": layoutManager.layout,
        "viewArrangement": layoutNode.GetViewArrangement(),
        "descriptionLength": len(layoutNode.GetCurrentLayoutDescription() or ""),
        "sliceViews": sliceViews,
        # Shown slice views that another widget of the application covers (name of that widget)
        "covered": covered,
        "threeDViewsShown": threeDViews,
        "viewportVisible": bool(viewport.isVisible()) if viewport is not None else None,
        "render": render,
    }


def _coveringWidget(view):
    """Return the name of the widget shown over the center of ``view``, or None if the view is not covered.

    A widget that is shown over the views (for example a full-size canvas of the application user
    interface) does not change whether the views are visible for Qt, but the user does not see them.
    """
    import qt

    center = view.mapToGlobal(qt.QPoint(view.width // 2, view.height // 2))
    top = qt.QApplication.widgetAt(center)
    if top is None or top == view or view.isAncestorOf(top):
        # None: nothing of the application at this position (the view is not covered by it)
        return None
    return top.objectName or top.className()


def _revealViews():
    """Hide the canvas that the application shows over the view area, if any. Return its name, or None.

    The Slicer Studio interface of the fork shows its Library workspace as a canvas over the view area.
    Hiding it does not change the layout, the views or the scene.
    """
    try:
        from StudioLib import shell as studioShell
    except ImportError:
        return None  # not a fork, or a fork without Studio
    studio = studioShell.instance()
    canvas = studio.canvas() if studio is not None else None
    if canvas is None or not canvas.isVisible():
        return None
    name = canvas.objectName or canvas.className()
    studio.setCanvas(None)
    _processEvents()
    return name


def _screenshot(path):
    import ctk

    import slicer

    _processEvents()
    slicer.util.forceRenderAllViews()
    image = ctk.ctkWidgetsUtils.grabWidget(slicer.util.mainWindow())
    image.save(path)


# -----------------------------------------------------------------------------
# Writer


def _setLayout(layoutId):
    import slicer

    slicer.app.layoutManager().setLayout(layoutId)
    _processEvents()
    slicer.util.resetSliceViews()
    _processEvents()
    return slicer.app.layoutManager().layout


def _runWriter(config):
    import slicer

    outDir = config["outDir"]
    capture = _LogCapture()
    mainWindow = slicer.util.mainWindow()
    mainWindow.resize(1400, 900)
    slicer.mrmlScene.Clear(0)
    _processEvents()
    capture.start()
    result = {"isFork": config["isFork"]}
    # Show the views (for the screenshot); it does not change what is saved
    result["startupCanvas"] = _revealViews()

    layoutId = CONVENTIONAL_LAYOUT
    if config["isFork"]:
        # SPEC 9.7 rule 3: the fork has the Studio layouts in C++. Older fork builds registered them
        # from Python (Studio): do the same here, so that their scenes can be checked too.
        layoutNode = _layoutNode()
        for studioLayoutId, description in STUDIO_LAYOUTS.items():
            if not layoutNode.IsLayoutDescription(studioLayoutId):
                layoutNode.AddLayoutDescription(studioLayoutId, description)
        layoutId = STUDIO_FOCUS_LAYOUT

    baselineIds = _sceneNodeIds()
    _buildScene()
    result["layout"] = _setLayout(layoutId)
    result["state"] = _collectState()
    result["views"] = _viewsState()

    # Scene file with its data files in a directory (what the Save data dialog does)
    sceneDir = os.path.join(outDir, "scene")
    os.makedirs(sceneDir, exist_ok=True)
    saved = {}
    for name, className, fileName in (
        (VOLUME_NAME, "vtkMRMLScalarVolumeNode", f"{VOLUME_NAME}.nrrd"),
        (TMAP_NAME, "vtkMRMLScalarVolumeNode", f"{TMAP_NAME}.nrrd"),
        (SEGMENTATION_NAME, "vtkMRMLSegmentationNode", f"{SEGMENTATION_NAME}.seg.nrrd"),
        (POINT_LIST_NAME, "vtkMRMLMarkupsFiducialNode", f"{POINT_LIST_NAME}.mrk.json"),
        (TABLE_NAME, "vtkMRMLTableNode", f"{TABLE_NAME}.tsv"),
        (COLOR_TABLE_NAME, "vtkMRMLColorTableNode", None),
    ):
        node = _nodeByName(name, className)
        if fileName is None:
            # Use the default format of this Slicer version for color tables
            fileName = f"{name}.{slicer.vtkMRMLColorTableStorageNode().GetDefaultWriteFileExtension()}"
        saved[fileName] = bool(slicer.util.saveNode(node, os.path.join(sceneDir, fileName)))
    sceneFile = os.path.join(sceneDir, f"{SCENE_NAME}.mrml")
    saved[f"{SCENE_NAME}.mrml"] = bool(slicer.util.saveScene(sceneFile))
    mrbFile = os.path.join(outDir, f"{SCENE_NAME}.mrb")
    saved[f"{SCENE_NAME}.mrb"] = bool(slicer.util.saveScene(mrbFile))
    result["saved"] = saved
    result["files"] = {"dir": sceneFile, "mrb": mrbFile}
    result["classCounts"] = _addedNodeClassCounts(baselineIds, savedOnly=True)

    shot = os.path.join(config["shotsDir"], f"writer_{config['name']}.png")
    _screenshot(shot)
    result["screenshot"] = shot

    if config["isFork"]:
        try:
            result["lnm"] = _writeLnmOutputs(config)
        except Exception:
            result["lnm"] = {"status": "error", "stderr": traceback.format_exc()}
    else:
        result["lnm"] = {"status": "skipped", "reason": "official writer (LNM is a fork module)"}
    result["messages"] = capture.stop()
    return result


def _importLnm(config):
    """Import LesionNetworkMappingLib from the writer, or else from the source tree. Return (module dir, how)."""
    import importlib

    try:
        package = importlib.import_module("LesionNetworkMappingLib")
        return os.path.dirname(os.path.dirname(os.path.abspath(package.__file__))), "installed"
    except ImportError:
        pass
    libDir = config.get("lnmLibDir")
    if libDir and os.path.isdir(os.path.join(libDir, "LesionNetworkMappingLib")):
        sys.path.insert(0, libDir)
        importlib.invalidate_caches()
        importlib.import_module("LesionNetworkMappingLib")
        return libDir, "source tree"
    return None, None


def _writeLnmOutputs(config):
    """Run the LNM command-line interface on a synthetic connectome and record its NIfTI outputs."""
    import importlib

    import numpy as np
    import vtk

    import slicer

    libDir, how = _importLnm(config)
    if libDir is None:
        return {"status": "skipped", "reason": "LesionNetworkMappingLib is not available in this Slicer or the source tree"}
    connectome = importlib.import_module("LesionNetworkMappingLib.connectome")
    nifti = importlib.import_module("LesionNetworkMappingLib.nifti")

    lnmDir = os.path.join(config["outDir"], "lnm")
    shutil.rmtree(lnmDir, ignore_errors=True)
    os.makedirs(lnmDir)
    bundle = connectome.make_synthetic(os.path.join(lnmDir, "Synthetic.lnmc"), shape=(20, 24, 20), n_subjects=4, runs=(20, 20), seed=1)
    lesion = np.zeros(bundle["shape"], dtype=np.uint8)
    ci, cj, ck = bundle["blobs"]["A"]["center_ijk"]
    lesion[ci - 2 : ci + 3, cj - 2 : cj + 3, ck - 1 : ck + 2] = 1
    lesionPath = os.path.join(lnmDir, "lesion.nii.gz")
    nifti.write_nifti(lesionPath, lesion, bundle["affine"])
    outputDir = os.path.join(lnmDir, "maps")
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([libDir] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []))
    command = [sys.executable, "-m", "LesionNetworkMappingLib.engine", "--bundle", bundle["path"], "--lesion", lesionPath, "--out", outputDir, "--threads", "1"]
    process = subprocess.run(command, check=False, env=environment, capture_output=True, text=True, timeout=600)
    events = []
    for line in process.stdout.splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    done = [event for event in events if event.get("event") == "done"]
    if process.returncode != 0 or not done:
        return {"status": "error", "library": how, "returncode": process.returncode, "stdout": process.stdout[-4000:], "stderr": process.stderr[-4000:]}

    outputs = []
    for path in done[0]["outputs"]:
        data, affine, _ = nifti.read_nifti(path)
        node = slicer.util.loadVolume(path)
        matrix = vtk.vtkMatrix4x4()
        node.GetIJKToRASMatrix(matrix)
        loaded = slicer.util.arrayFromVolume(node)
        outputs.append({
            "path": path,
            "niftiAffine": [float(value) for value in np.asarray(affine).reshape(-1)],
            "niftiShapeKJI": list(reversed([int(n) for n in data.shape[:3]])),
            "niftiMd5": _md5(np.transpose(data, (2, 1, 0)).astype(np.float64)),
            "writerSlicer": {"ijkToRas": _matrixToList(matrix), "shape": list(loaded.shape), "md5": _md5(loaded.astype(np.float64))},
        })
        slicer.mrmlScene.RemoveNode(node)
    return {"status": "ok", "library": how, "outputs": outputs}


# -----------------------------------------------------------------------------
# Checker


def _loadAndInspect(path, shotPath, capture):
    import slicer

    slicer.mrmlScene.Clear(0)
    _processEvents()
    baselineIds = _sceneNodeIds()
    capture.start()
    record = {"path": path}
    try:
        slicer.util.loadScene(path)  # raises an exception if loading fails
        record["loaded"] = True
    except Exception:
        record["loaded"] = False
        record["loadException"] = traceback.format_exc()
    _processEvents(5)
    record["classCounts"] = _addedNodeClassCounts(baselineIds, savedOnly=False)
    record["singletons"] = _sceneSingletons()
    record["state"] = _collectState()
    record["views"] = _viewsState()
    if record["views"]["covered"]:
        # Something was shown over the views when the scene was loaded: record it, then check the views
        canvas = _revealViews()
        if canvas:
            record["coveredAfterLoad"] = {"views": record["views"]["covered"], "hidden": canvas}
            record["views"] = _viewsState()
    _screenshot(shotPath)
    record["screenshot"] = shotPath
    record["messages"] = capture.stop()
    return record


def _checkLnmOutputs(lnm, capture):
    import numpy as np
    import vtk

    import slicer

    checked = []
    for output in lnm.get("outputs", []):
        capture.start()
        record = {"path": output["path"]}
        try:
            node = slicer.util.loadVolume(output["path"])
            matrix = vtk.vtkMatrix4x4()
            node.GetIJKToRASMatrix(matrix)
            loaded = slicer.util.arrayFromVolume(node)
            record.update({"ijkToRas": _matrixToList(matrix), "shape": list(loaded.shape), "md5": _md5(loaded.astype(np.float64))})
            slicer.mrmlScene.RemoveNode(node)
        except Exception:
            record["exception"] = traceback.format_exc()
        record["messages"] = capture.stop()
        checked.append(record)
    return checked


def _runChecker(config):
    import slicer

    slicer.util.mainWindow().resize(1400, 900)
    capture = _LogCapture()
    result = {"isFork": config["isFork"], "baseline": None, "writers": {}}
    # The checks are about the files: show the views if the application started with a canvas over them.
    # A canvas that is shown again by loading a scene is reported for that scene (coveredAfterLoad).
    result["startupCanvas"] = _revealViews()
    if config.get("baseline"):
        # Messages that this Slicer emits when it loads the scene it wrote itself
        result["baseline"] = _loadAndInspect(config["baseline"], os.path.join(config["shotsDir"], f"baseline_{config['name']}.png"), capture)
    for writer in config["writers"]:
        with open(writer["manifest"], encoding="utf-8") as f:
            manifest = json.load(f)
        records = {}
        for sceneFormat in SCENE_FORMATS:
            path = manifest.get("files", {}).get(sceneFormat)
            if not path:
                continue
            shot = os.path.join(config["shotsDir"], f"{writer['name']}__{config['name']}__{sceneFormat}.png")
            records[sceneFormat] = _loadAndInspect(path, shot, capture)
        slicer.mrmlScene.Clear(0)
        if manifest.get("lnm", {}).get("status") == "ok":
            records["lnm"] = _checkLnmOutputs(manifest["lnm"], capture)
        result["writers"][writer["name"]] = records
    return result


# =============================================================================
# Comparison (runs in the harness, outside Slicer)
# =============================================================================


def _close(a, b, tolerance):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tolerance


def _closeLists(a, b, tolerance):
    return a is not None and b is not None and len(a) == len(b) and all(_close(x, y, tolerance) for x, y in zip(a, b, strict=True))


def _compareVolume(label, expected, actual, problems):
    if actual is None:
        problems.append(f"{label}: node is missing")
        return
    for key in ("class", "shape", "dtype", "md5", "attributes"):
        if expected.get(key) != actual.get(key):
            problems.append(f"{label}: {key} differs ({expected.get(key)} != {actual.get(key)})")
    if not _closeLists(expected.get("ijkToRas"), actual.get("ijkToRas"), GEOMETRY_TOLERANCE):
        problems.append(f"{label}: IJK to RAS matrix differs ({expected.get('ijkToRas')} != {actual.get('ijkToRas')})")
    expectedDisplay, actualDisplay = expected.get("display") or {}, actual.get("display") or {}
    for key in ("class", "interpolate", "autoWindowLevel", "colorNode"):
        if expectedDisplay.get(key) != actualDisplay.get(key):
            problems.append(f"{label}: display {key} differs ({expectedDisplay.get(key)} != {actualDisplay.get(key)})")
    for key in ("window", "level"):
        if not _close(expectedDisplay.get(key), actualDisplay.get(key), VALUE_TOLERANCE * max(1.0, abs(expectedDisplay.get(key) or 0))):
            problems.append(f"{label}: display {key} differs ({expectedDisplay.get(key)} != {actualDisplay.get(key)})")


def compareStates(expected, actual):
    """Return the list of differences between the scene content recorded by a writer and a checker."""
    problems = []
    for key, value in actual.items():
        if isinstance(value, dict) and "error" in value:
            problems.append(f"{key}: could not be inspected: {value['error'].strip().splitlines()[-1]}")
    for key in ("volume", "tmap"):
        if isinstance(actual.get(key), dict) and "error" in actual[key]:
            continue
        _compareVolume(key, expected[key], actual.get(key), problems)

    colorTable, expectedColorTable = actual.get("colorTable"), expected["colorTable"]
    if colorTable is None:
        problems.append("colorTable: node is missing")
    elif "error" not in colorTable:
        if colorTable["numberOfColors"] != expectedColorTable["numberOfColors"]:
            problems.append(f"colorTable: number of colors differs ({expectedColorTable['numberOfColors']} != {colorTable['numberOfColors']})")
        else:
            bad = [e[0] for e, a in zip(expectedColorTable["entries"], colorTable["entries"], strict=True) if e[1] != a[1] or not _closeLists(e[2:], a[2:], COLOR_TOLERANCE)]
            if bad:
                problems.append(f"colorTable: {len(bad)} entries differ (first: {expectedColorTable['entries'][bad[0]]} != {colorTable['entries'][bad[0]]})")

    segmentation, expectedSegmentation = actual.get("segmentation"), expected["segmentation"]
    if segmentation is None:
        problems.append("segmentation: node is missing")
    elif "error" not in segmentation:
        if segmentation["sourceRepresentation"] != expectedSegmentation["sourceRepresentation"]:
            problems.append(f"segmentation: source representation differs ({expectedSegmentation['sourceRepresentation']} != {segmentation['sourceRepresentation']})")
        if expectedSegmentation["closedSurface"] and not segmentation["closedSurface"]:
            problems.append("segmentation: closed surface representation is missing")
        expectedSegments, segments = expectedSegmentation["segments"], segmentation["segments"]
        if [s["id"] for s in expectedSegments] != [s["id"] for s in segments]:
            problems.append(f"segmentation: segment IDs differ ({[s['id'] for s in expectedSegments]} != {[s['id'] for s in segments]})")
        for expectedSegment, segment in zip(expectedSegments, segments, strict=False):
            for key in ("name", "voxels", "md5"):
                if expectedSegment[key] != segment[key]:
                    problems.append(f"segment {expectedSegment['id']}: {key} differs ({expectedSegment[key]} != {segment[key]})")
            if not _closeLists(expectedSegment["color"], segment["color"], 1e-4):
                problems.append(f"segment {expectedSegment['id']}: color differs ({expectedSegment['color']} != {segment['color']})")
            if expectedSegment["closedSurfacePoints"] > 0 and segment["closedSurfacePoints"] == 0:
                problems.append(f"segment {expectedSegment['id']}: closed surface is empty")

    pointList, expectedPointList = actual.get("pointList"), expected["pointList"]
    if pointList is None:
        problems.append("pointList: node is missing")
    elif "error" not in pointList:
        if len(pointList["points"]) != len(expectedPointList["points"]):
            problems.append(f"pointList: number of points differs ({len(expectedPointList['points'])} != {len(pointList['points'])})")
        for index, (expectedPoint, point) in enumerate(zip(expectedPointList["points"], pointList["points"], strict=False)):
            for key in ("label", "description", "selected", "locked"):
                if expectedPoint[key] != point[key]:
                    problems.append(f"point {index}: {key} differs ({expectedPoint[key]} != {point[key]})")
            if not _closeLists(expectedPoint["position"], point["position"], 1e-4):
                problems.append(f"point {index}: position differs ({expectedPoint['position']} != {point['position']})")

    for key in ("table", "parameterNode", "sliceComposite"):
        if actual.get(key) is None:
            problems.append(f"{key}: node is missing")
        elif "error" not in actual[key] and actual[key] != expected[key]:
            for field in sorted(set(expected[key]) | set(actual[key])):
                if expected[key].get(field) != actual[key].get(field):
                    problems.append(f"{key}: {field} differs ({expected[key].get(field)} != {actual[key].get(field)})")

    view3D = actual.get("view3D")
    if view3D and "error" not in view3D:
        for key in ("backgroundColor", "backgroundColor2"):
            if not _closeLists(expected["view3D"][key], view3D[key], 1e-6):
                problems.append(f"3D view: {key} differs ({expected['view3D'][key]} != {view3D[key]})")
    return problems


def expectedLayout(writerLayout, checkerIsFork):
    """Layout a checker must show for a scene saved with ``writerLayout`` (SPEC 9.7 rule 3)."""
    if STUDIO_LAYOUT_ID_RANGE[0] <= writerLayout <= STUDIO_LAYOUT_ID_RANGE[1] and not checkerIsFork:
        return CONVENTIONAL_LAYOUT  # portable fallback
    return writerLayout


def compareViews(expectedLayoutId, views):
    problems = []
    if views.get("layout") != expectedLayoutId:
        problems.append(f"layout is {views.get('layout')}, expected {expectedLayoutId}")
    if not views.get("descriptionLength"):
        problems.append(f"layout {views.get('layout')} has no layout description: the view area is empty")
    shown = sorted(name for name, visible in views.get("sliceViews", {}).items() if visible)
    if expectedLayoutId in (CONVENTIONAL_LAYOUT, *STUDIO_LAYOUTS):
        missing = sorted({"Red", "Green", "Yellow"} - set(shown))
        if missing:
            problems.append(f"slice views {missing} are not shown (shown: {shown})")
    elif not shown and not views.get("threeDViewsShown"):
        problems.append("no view is shown")
    covered = views.get("covered") or {}
    if covered:
        problems.append(f"slice views {sorted(covered)} are covered by {sorted(set(covered.values()))}")
    render = views.get("render", {})
    if render.get("status") == "ok" and (render.get("distinctColors", 0) < 16 or render.get("std", 0.0) < 1.0):
        problems.append(f"the Red slice view does not render the scene ({render})")
    return problems


def _normalizeMessage(text):
    text = re.sub(r"(?<![\w.])/[^\s\"':,()]+", "<PATH>", text)
    text = re.sub(r"0x[0-9a-fA-F]+", "0x?", text)
    text = re.sub(r"\(\d+\)|\[\d+\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _isNoise(message):
    return any(re.search(pattern, message["text"]) for pattern in ENVIRONMENT_NOISE)


def compareMessages(messages, baselineMessages, samePair):
    """Return (problems, ignored) for the log entries of a load.

    A Slicer loading a scene that it wrote itself must not log errors. A Slicer loading a scene written by
    another installation must not log errors or warnings that it does not log for its own scene.
    """
    baseline = {(m["level"], _normalizeMessage(m["text"])) for m in baselineMessages or []}
    problems, ignored = [], []
    for message in messages:
        if _isNoise(message):
            ignored.append(message)
            continue
        key = (message["level"], _normalizeMessage(message["text"]))
        if samePair:
            if message["level"] in ERROR_LEVELS:
                problems.append(f"{message['level']}: {message['text'].strip()[:300]}")
        elif key not in baseline:
            problems.append(f"new {message['level'].lower()}: {message['text'].strip()[:300]}")
    return problems, ignored


def compareClassCounts(expected, actual):
    problems = []
    for className in sorted(set(expected) | set(actual)):
        if expected.get(className, 0) != actual.get(className, 0):
            problems.append(f"{className}: {expected.get(className, 0)} saved, {actual.get(className, 0)} loaded")
    return problems


def savedSingletons(scenePath):
    """Return the sorted "<XML tag>/<singleton tag>" keys of the singleton nodes saved in a scene file or bundle."""
    import xml.etree.ElementTree as ET
    import zipfile

    if scenePath.lower().endswith(".mrb"):
        with zipfile.ZipFile(scenePath) as bundle:
            # The scene file of the bundle (the shortest path, other .mrml files would be in subdirectories)
            sceneFiles = sorted((name for name in bundle.namelist() if name.lower().endswith(".mrml")), key=len)
            if not sceneFiles:
                raise ValueError(f"no scene file in {scenePath}")
            root = ET.fromstring(bundle.read(sceneFiles[0]))
    else:
        root = ET.parse(scenePath).getroot()
    return sorted({f"{element.tag}/{element.get('singletonTag')}" for element in root if element.get("singletonTag")})


def compareSingletons(saved, loaded):
    """Every singleton node saved in the scene must exist after loading it (it may have existed before)."""
    return [f"{key.replace('/', ' singletonTag=', 1)}: saved, missing after loading" for key in sorted(set(saved) - set(loaded))]


def compareLnm(writerOutputs, checked):
    problems = []
    byPath = {record["path"]: record for record in checked}
    for output in writerOutputs:
        name = os.path.basename(output["path"])
        record = byPath.get(output["path"])
        if record is None or "exception" in record:
            problems.append(f"{name}: could not be loaded" + (f": {record['exception'].strip().splitlines()[-1]}" if record else ""))
            continue
        if not _closeLists(output["niftiAffine"], record["ijkToRas"], 1e-4):
            problems.append(f"{name}: geometry differs from the NIfTI header ({output['niftiAffine']} != {record['ijkToRas']})")
        if not _closeLists(output["writerSlicer"]["ijkToRas"], record["ijkToRas"], 1e-6):
            problems.append(f"{name}: geometry differs from the writer ({output['writerSlicer']['ijkToRas']} != {record['ijkToRas']})")
        if record["shape"] != output["niftiShapeKJI"] or record["md5"] != output["niftiMd5"]:
            problems.append(f"{name}: voxel values differ from the NIfTI file")
        errors = [m for m in record.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
        if errors:
            problems.append(f"{name}: errors while loading: {errors[0]['text'].strip()[:300]}")
    return problems


# =============================================================================
# Format comparison (informational): structure of the files written by different installations
# =============================================================================


def _mrmlStructure(path):
    import xml.etree.ElementTree as ET

    structure = {}
    for element in ET.parse(path).getroot().iter():
        structure.setdefault(element.tag, set()).update(element.attrib)
    return structure


def _nrrdKeys(path):
    keys = set()
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("latin-1").rstrip("\r\n")
            if not line:
                break
            if line.startswith("#") or line.startswith("NRRD"):
                continue
            match = re.match(r"([^:]+):=?", line)
            if match:
                keys.add(match.group(1))
    return keys


def _jsonKeys(value, prefix=""):
    keys = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(prefix + key)
            keys |= _jsonKeys(item, prefix + key + ".")
    elif isinstance(value, list):
        for item in value:
            keys |= _jsonKeys(item, prefix + "[].")
    return keys


def fileStructure(sceneDir):
    """Return {file kind: set of structural keys} for the files of a saved scene directory."""
    structure = {}
    for fileName in sorted(os.listdir(sceneDir)):
        path = os.path.join(sceneDir, fileName)
        kind = fileName.replace(SCENE_NAME, "").replace("Compat", "")
        try:
            if fileName.endswith(".mrml"):
                for tag, attributes in _mrmlStructure(path).items():
                    structure[f"mrml <{tag}>"] = attributes
            elif fileName.endswith(".nrrd"):
                structure[kind] = _nrrdKeys(path)
            elif fileName.endswith(".json"):
                with open(path, encoding="utf-8") as f:
                    structure[kind] = _jsonKeys(json.load(f))
            elif fileName.endswith((".tsv", ".csv", ".ctbl", ".txt")):
                with open(path, encoding="utf-8", errors="replace") as f:
                    structure[kind] = {line.split("\t")[0] if fileName.endswith(".tsv") else line for line in f.read().splitlines()[:1]}
        except Exception as exc:
            structure[kind] = {f"<unreadable: {exc}>"}
    return structure


def compareStructures(reference, other):
    differences = []
    for kind in sorted(set(reference) | set(other)):
        if kind not in other:
            differences.append(f"{kind}: not written")
        elif kind not in reference:
            differences.append(f"{kind}: only written here")
        else:
            added, removed = sorted(other[kind] - reference[kind]), sorted(reference[kind] - other[kind])
            if added:
                differences.append(f"{kind}: additional {added}")
            if removed:
                differences.append(f"{kind}: missing {removed}")
    return differences


# =============================================================================
# Harness (runs outside Slicer)
# =============================================================================


def _generatedScript(configPath):
    return (
        "# Generated by SlicerFileCompatibilityCheck.py\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('SlicerFileCompatibilityCheck', {os.path.abspath(__file__)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"module.runInSlicer({configPath!r})\n"
    )


def _runSlicer(slicerPath, role, name, config, args):
    """Write the config and the generated script, run them in Slicer and return the result dictionary."""
    runDir = os.path.join(args.out, "runs", f"{role}_{name}")
    shutil.rmtree(runDir, ignore_errors=True)
    os.makedirs(runDir)
    home = os.path.join(runDir, "home")
    os.makedirs(home)
    config.update({"role": role, "name": name, "resultPath": os.path.join(runDir, "result.json"), "shotsDir": os.path.join(args.out, "shots")})
    configPath = os.path.join(runDir, "config.json")
    with open(configPath, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=1)
    scriptPath = os.path.join(runDir, f"{role}.py")
    with open(scriptPath, "w", encoding="utf-8") as f:
        f.write(_generatedScript(configPath))

    command = [slicerPath, "--no-splash", "--ignore-slicerrc", "--python-script", scriptPath]
    if args.testing:
        command.insert(1, "--testing")
    if not args.no_xvfb:
        command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", *command]
    # Isolated settings: never use or modify the settings that the user's installations share
    environment = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"), XDG_CACHE_HOME=os.path.join(home, ".cache"), XDG_DATA_HOME=os.path.join(home, ".local", "share"))
    print(f"[{time.strftime('%H:%M:%S')}] {role} {name}: {slicerPath}", flush=True)
    start = time.time()
    with open(os.path.join(runDir, "slicer.log"), "w", encoding="utf-8") as log:
        try:
            process = subprocess.run(command, check=False, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout)
            returncode = process.returncode
        except subprocess.TimeoutExpired:
            returncode = "timeout"
    result = {}
    if os.path.exists(config["resultPath"]):
        with open(config["resultPath"], encoding="utf-8") as f:
            result = json.load(f)
    result["returncode"] = returncode
    result["seconds"] = round(time.time() - start, 1)
    result["log"] = os.path.join(runDir, "slicer.log")
    if not os.path.exists(config["resultPath"]):
        with open(result["log"], encoding="utf-8", errors="replace") as log:
            lastLines = [line.strip() for line in log.read().splitlines() if line.strip()][-1:]
        result["exception"] = f"Slicer did not write a result (exit code {returncode}), see {result['log']}\n{' '.join(lastLines)[:300]}"
    return result


def _parsePairs(text, names):
    if text in ("all", "cross"):
        return [(writer, checker) for writer in names for checker in names if text == "all" or writer != checker]
    pairs = []
    for item in text.split(","):
        writer, _, checker = item.partition(":")
        if writer not in names or checker not in names:
            raise SystemExit(f"Unknown Slicer name in pair '{item}' (known: {', '.join(names)})")
        pairs.append((writer, checker))
    return pairs


def _evaluateWriter(name, writer):
    problems = []
    if "exception" in writer:
        problems.append(f"writer failed: {writer['exception'].strip().splitlines()[-1]}")
        return problems
    for fileName, ok in writer.get("saved", {}).items():
        if not ok:
            problems.append(f"saving {fileName} failed")
    if writer.get("isFork"):
        for layoutId, status in writer["info"]["studioLayouts"].items():
            if not status["registered"]:
                problems.append(f"layout {layoutId} is not registered in C++ (vtkMRMLLayoutLogic)")
            elif not status["canonical"]:
                problems.append(f"layout {layoutId} description is not the canonical one")
        if writer.get("layout") != STUDIO_FOCUS_LAYOUT:
            problems.append(f"could not set layout {STUDIO_FOCUS_LAYOUT} (layout is {writer.get('layout')})")
        if writer.get("lnm", {}).get("status") == "error":
            problems.append(f"LNM command line failed: {writer['lnm'].get('stderr', '').strip()[-300:]}")
    errors = [m for m in writer.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
    for message in errors:
        problems.append(f"{message['level']} while writing: {message['text'].strip()[:300]}")
    return problems


def _evaluatePair(writerName, checkerName, writer, checker, isFork):
    """Return {format: {"problems": [...], ...}} for one ordered pair."""
    evaluation = {}
    if "exception" in writer:
        return {"all": {"problems": ["writer failed"]}}
    if "exception" in checker:
        return {"all": {"problems": [f"checker failed: {checker['exception'].strip().splitlines()[-1]}"]}}
    records = checker.get("writers", {}).get(writerName)
    if records is None:
        return {"all": {"problems": ["checker did not load this writer's files"]}}
    baselineMessages = (checker.get("baseline") or {}).get("messages", [])
    expectedLayoutId = expectedLayout(writer["layout"], isFork[checkerName])
    for sceneFormat in SCENE_FORMATS:
        record = records.get(sceneFormat)
        if record is None:
            evaluation[sceneFormat] = {"problems": ["not checked"]}
            continue
        problems = []
        if not record.get("loaded"):
            problems.append("loading the scene failed" + (": " + record["loadException"].strip().splitlines()[-1] if record.get("loadException") else ""))
        problems += [f"node count: {p}" for p in compareClassCounts(writer["classCounts"], record["classCounts"])]
        try:
            saved = savedSingletons(writer["files"][sceneFormat])
        except Exception as exc:
            problems.append(f"singleton nodes: cannot read the saved scene: {exc}")
        else:
            problems += [f"singleton node {p}" for p in compareSingletons(saved, record.get("singletons", []))]
        problems += compareStates(writer["state"], record["state"])
        problems += [f"views: {p}" for p in compareViews(expectedLayoutId, record["views"])]
        messageProblems, ignored = compareMessages(record["messages"], baselineMessages, writerName == checkerName)
        problems += [f"log: {p}" for p in messageProblems]
        evaluation[sceneFormat] = {
            "problems": problems,
            "expectedLayout": expectedLayoutId,
            "layout": record["views"].get("layout"),
            "sliceViewsShown": sorted(n for n, v in record["views"].get("sliceViews", {}).items() if v),
            "render": record["views"].get("render"),
            "screenshot": record.get("screenshot"),
            "ignoredMessages": [m["text"].strip()[:200] for m in ignored],
            # Informational: a canvas of the application was shown over the views after loading (hidden for the checks)
            "coveredAfterLoad": record.get("coveredAfterLoad"),
        }
    lnm = writer.get("lnm", {})
    if lnm.get("status") == "ok":
        evaluation["lnm"] = {"problems": compareLnm(lnm["outputs"], records.get("lnm", []))}
    else:
        evaluation["lnm"] = {"problems": [], "skipped": lnm.get("reason") or lnm.get("status")}
    return evaluation


def _formatReport(report):
    lines = ["Slicer file compatibility check", "=" * 31, ""]
    for name, info in report["slicers"].items():
        lines.append(f"{name:<20} {info.get('version') or '?':<22} {'fork' if info['isFork'] else 'official'}  {info['path']}")
        if info.get("startupCanvas"):
            lines.append(f"{'':<20} (started with {info['startupCanvas']} over the views; hidden for the checks)")
    lines += ["", "Writers:"]
    for name, writer in report["writers"].items():
        status = "PASS" if not writer["problems"] else "FAIL"
        lnm = writer.get("lnm", {})
        lnmText = f"LNM outputs: {lnm.get('status')}" + (f" ({lnm.get('library') or lnm.get('reason')})" if lnm.get("library") or lnm.get("reason") else "")
        lines.append(f"  {name:<20} {status}  layout {writer.get('layout')}; {lnmText}")
        lines += [f"      - {p}" for p in writer["problems"]]

    names = list(report["slicers"])
    width = max(len(n) for n in names) + 2
    lines += ["", "Matrix (rows: writer, columns: checker; formats dir/mrb/lnm):", " " * width + "".join(f"{n:<{width + 4}}" for n in names)]
    for writerName in names:
        row = f"{writerName:<{width}}"
        for checkerName in names:
            pair = report["pairs"].get(f"{writerName}:{checkerName}")
            if pair is None:
                cell = "-"
            else:
                cell = "/".join(("skip" if part.get("skipped") else ("ok" if not part["problems"] else "FAIL")) for key, part in pair.items() if key != "all") or "FAIL"
                if "all" in pair:
                    cell = "FAIL"
            row += f"{cell:<{width + 4}}"
        lines.append(row)

    lines += ["", "Details:"]
    for pairName, pair in report["pairs"].items():
        writerName, checkerName = pairName.split(":")
        lines.append(f"  {writerName} -> {checkerName}")
        for key, part in pair.items():
            if part.get("skipped"):
                lines.append(f"    {key:<4} skipped: {part['skipped']}")
                continue
            status = "ok" if not part["problems"] else "FAIL"
            extra = ""
            if "layout" in part:
                extra = f" layout {part['layout']} (expected {part['expectedLayout']}), slice views {','.join(part['sliceViewsShown']) or 'none'}"
                render = part.get("render") or {}
                extra += f", render {render.get('status')}" + (f" ({render.get('distinctColors')} colors)" if render.get("status") == "ok" else "")
            lines.append(f"    {key:<4} {status}{extra}")
            lines += [f"         - {p}" for p in part["problems"]]
            if part.get("coveredAfterLoad"):
                covered = part["coveredAfterLoad"]
                lines.append(f"         (after loading, {covered['hidden']} covered slice views {','.join(sorted(covered['views']))}; hidden for the checks)")
            if part.get("ignoredMessages"):
                lines.append(f"         (ignored environment messages: {len(part['ignoredMessages'])})")
    if report.get("formatDifferences"):
        lines += ["", "File structure differences (informational; new XML attributes are allowed by SPEC 9.7 rule 2):"]
        for comparison, differences in report["formatDifferences"].items():
            lines.append(f"  {comparison}:")
            lines += [f"    - {d}" for d in differences] or ["    (none)"]
    lines += ["", f"Result: {'PASS' if report['passed'] else 'FAIL'}", ""]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check that files saved by one Slicer installation load correctly in another one.")
    parser.add_argument("--slicer", action="append", required=True, metavar="NAME=PATH", help="Slicer launcher to test (repeatable)")
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--pairs", default="all", help="'all' (default), 'cross' or WRITER:CHECKER[,WRITER:CHECKER...]")
    parser.add_argument("--fork", action="append", default=None, metavar="NAME", help="name of a fork installation (default: names starting with 'fork')")
    parser.add_argument("--testing", action="store_true", help="start Slicer with --testing")
    parser.add_argument("--timeout", type=int, default=900, help="time limit for each Slicer process in seconds")
    parser.add_argument("--no-xvfb", action="store_true", help="do not run Slicer in xvfb-run")
    parser.add_argument("--lnm-lib-dir", default=os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Modules", "Scripted", "LesionNetworkMapping")))
    args = parser.parse_args(argv)
    args.out = os.path.abspath(args.out)

    slicers = {}
    for item in args.slicer:
        name, separator, path = item.partition("=")
        if not separator or not name or not path:
            parser.error(f"--slicer expects NAME=PATH, got '{item}'")
        if not os.path.exists(path):
            parser.error(f"Slicer launcher not found: {path}")
        slicers[name] = os.path.abspath(path)
    forkNames = set(args.fork) if args.fork else {name for name in slicers if name.startswith("fork")}
    isFork = {name: name in forkNames for name in slicers}
    pairs = _parsePairs(args.pairs, list(slicers))

    for subdir in ("runs", "write", "shots"):
        shutil.rmtree(os.path.join(args.out, subdir), ignore_errors=True)
    os.makedirs(os.path.join(args.out, "shots"))

    # Writers: every installation that is part of a pair (checkers also load their own scene as baseline)
    involved = [name for name in slicers if any(name in pair for pair in pairs)]
    writers = {}
    for name in involved:
        outDir = os.path.join(args.out, "write", name)
        os.makedirs(outDir)
        writers[name] = _runSlicer(slicers[name], "writer", name, {"outDir": outDir, "isFork": isFork[name], "lnmLibDir": args.lnm_lib_dir}, args)
        with open(os.path.join(outDir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(writers[name], f, indent=1, sort_keys=True)

    # Checkers: one process per checker installation loads the files of all its writers
    checkers = {}
    for checkerName in involved:
        writerNames = [writer for writer, checker in pairs if checker == checkerName and "exception" not in writers[writer]]
        if not writerNames:
            continue
        baseline = writers[checkerName].get("files", {}).get("dir") if "exception" not in writers[checkerName] else None
        config = {
            "isFork": isFork[checkerName],
            "baseline": baseline,
            "writers": [{"name": w, "manifest": os.path.join(args.out, "write", w, "manifest.json")} for w in writerNames],
        }
        checkers[checkerName] = _runSlicer(slicers[checkerName], "checker", checkerName, config, args)

    report = {"slicers": {}, "writers": {}, "pairs": {}, "formatDifferences": {}}
    for name in involved:
        info = writers[name].get("info", {})
        report["slicers"][name] = {
            "path": slicers[name],
            "isFork": isFork[name],
            "version": info.get("version"),
            "revision": info.get("revision"),
            # Informational: canvas shown over the views at startup, hidden for the checks
            "startupCanvas": writers[name].get("startupCanvas") or checkers.get(name, {}).get("startupCanvas"),
        }
        report["writers"][name] = {
            "problems": _evaluateWriter(name, writers[name]),
            "layout": writers[name].get("layout"),
            "lnm": {key: writers[name].get("lnm", {}).get(key) for key in ("status", "library", "reason")},
            "seconds": writers[name].get("seconds"),
        }
    for writerName, checkerName in pairs:
        checker = checkers.get(checkerName, {"exception": "checker was not run"})
        report["pairs"][f"{writerName}:{checkerName}"] = _evaluatePair(writerName, checkerName, writers[writerName], checker, isFork)

    # Informational: structure of the files written by each fork compared with each official installation
    structures = {}
    for name in involved:
        sceneFile = writers[name].get("files", {}).get("dir")
        if sceneFile and os.path.isdir(os.path.dirname(sceneFile)):
            structures[name] = fileStructure(os.path.dirname(sceneFile))
    for forkName in [n for n in involved if isFork[n] and n in structures]:
        for officialName in [n for n in involved if not isFork[n] and n in structures]:
            report["formatDifferences"][f"{forkName} vs {officialName}"] = compareStructures(structures[officialName], structures[forkName])

    report["passed"] = not any(w["problems"] for w in report["writers"].values()) and not any(part["problems"] for pair in report["pairs"].values() for part in pair.values())
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True)
    text = _formatReport(report)
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
