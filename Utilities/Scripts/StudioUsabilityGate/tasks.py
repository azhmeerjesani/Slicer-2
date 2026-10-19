"""Tasks of the usability gate: the walkthroughs of USABILITY section 16, the review scenarios and the state assertions.

Every task is a generator that acts only through the driver (``yield from gate.click(...)``); the action count is
derived by the driver from the input events, never entered here. A task feature-detects every planned feature
(palette, View section, follow strokes, Display presets, Notes field, convention chip, loaded set, close prompt
modes...) and uses it when it exists, so that later work packages change the counts only by landing their feature.
The steps that were really run are written to results.json and the wave review diffs them against the last run.

``ASSERTIONS`` lists the state assertions with the work package that they guard and the wave from which they must
pass; a failing assertion before that wave is an expected failure (recorded, not blocking).
"""

import json
import os
import re
import shutil
import subprocess

import numpy as np
import qt
import slicer
import vtk

from .harness import isAncestorOrSelf, sameObject, widgetText

#
# State assertions of USABILITY section 16
#
# guard: what the assertion protects; fromWave: first wave at which a failure blocks the gate.
# A failure before that wave is an "expected failure"; a pass before it is reported as an unexpected pass.

ASSERTIONS = {
    "first-run-window": dict(text="The first-run window covers at least 85% of the screen (real startup path through SLICERRC)", guard="QW-B", fromWave=1),
    "effect-after-return": dict(text="After every return to Draw the active effect is the user's last tool", guard="QW-A", fromWave=1),
    "effect-armed-on-entry": dict(text="Start on MNI template arms Brush (Paint) on the first entry", guard="QW-A", fromWave=1),
    "dock-width-stable": dict(text="The inspector width does not change (+-1 px) when a tool that was used before is chosen", guard="QW-B", fromWave=1),
    "dock-width-first-use": dict(text="The inspector width does not change (+-1 px) when a tool is used for the first time (the width of the workspaces is constant)", guard="W2-05", fromWave=2),
    "tab-never-toggles-inspector": dict(text="Tab never changes the inspector visibility", guard="QW-B", fromWave=1),
    "esc-ladder": dict(text="Esc leaves one state per press in the order of USABILITY section 3 (Place/W-L, maximized view, hidden panels, tool)", guard="QW-B", fromWave=1),
    "classic-studio-draw": dict(text="Draw -> Classic -> Studio returns to Draw", guard="QW-B", fromWave=1),
    "markups-toolbar-stays": dict(text="The Markups toolbar toggled from View > Toolbars stays visible (find:NAV-03)", guard="QW-B", fromWave=1),
    "recent-reopen-no-node": dict(text="Re-opening a Recent file or the same scan adds no node and keeps the traced lesion", guard="U-07", fromWave=2),
    "edge-letters-correct": dict(text="Edge letters are present and correct in Studio (Red axial: R on the left edge)", guard="QW-B", fromWave=1),
    "edge-letters-absent-classic": dict(text="Edge letters are absent in Classic", guard="QW-B", fromWave=1),
    "edge-letters-absent-figure": dict(text="Edge letters are absent on figure views", guard="U-07", fromWave=2),
    "figure-paint-guard": dict(text="A Paint stroke over the Yellow figure changes 0 voxels of the lesion", guard="U-07", fromWave=2),
    "figure-link-offset": dict(text="With Link views on, Yellow's offset stays 0 while a figure is shown", guard="U-07", fromWave=2),
    "no-banner-overlap": dict(text="No download banner overlaps a slice view", guard="U-01", fromWave=2),
    "save-grid-preserved": dict(text="Save lesion writes a uint8 NIfTI whose dims, sform and voxels equal the lesion grid", guard="QW-A", fromWave=1),
    "save-grid-nlin6-on-2009c": dict(text="A 91x109x91 NLin6Asym mask shown on the 2009c template is saved on its own grid", guard="W2-05", fromWave=2),
    "place-keeps-tool": dict(text="Draw -> Place point -> click -> drag paints (Place keeps the tool)", guard="W2-05", fromWave=2),
    "official-loads-saved": dict(text="The saved NIfTI loads in official Slicer 5.13 with identical voxels", guard="QW-A", fromWave=1),
    "no-hemisphere-word-scanner": dict(text="No hemisphere word is shown for a lesion in scanner space", guard="W2-05", fromWave=2),
    "wrong-side": dict(text="After the convention answer the traced lesion has the sign of the figure's patient side", guard="U-07", fromWave=2),
    "reopen-with-scan": dict(text="Dropping a scan together with its saved lesion opens the scan as the source and the lesion with all its voxels", guard="U-07", fromWave=2),
    "reopen-sidecar-offer": dict(text="Opening a saved lesion with its scan offers the scan (sidecar) and never binds a native-space lesion to the template", guard="U-07", fromWave=2),
    "dicom-draw-ready": dict(text="A dropped DICOM folder reaches a Draw-ready scan", guard="W2-05", fromWave=2),
    "esc-matrix": dict(text="Esc from each state of USABILITY section 3 leaves exactly that state", guard="U-01", fromWave=2),
    "keyboard-layouts": dict(text="On de and fr layouts Ctrl+5 and the edge handle toggle the inspector and AltGr+@ types @ in a search field", guard="U-01", fromWave=2),
    "menu-keyboard-reachable": dict(text="Every item of the app menu is reachable by keyboard (from wave 2)", guard="U-01", fromWave=2),
    "no-invisible-focus-stop": dict(text="No Tab stop on Library is invisible (from wave 2)", guard="U-01", fromWave=2),
    "window-fits-screen": dict(text="The main window never grows beyond the screen when layouts change: the inspector and the search box stay on the screen (1366x768)", guard="U-06", fromWave=2),
    "hover-only-control": dict(text="No control that a task needs is shown only while the pointer hovers its opener (the pin popup of the Classic view bars)", guard="W2-03", fromWave=2),
    "not-discoverable": dict(text="Every control that a task drives is visible, enabled and not covered when it is driven", guard="gate", fromWave=1),
    "task-completed": dict(text="The task ran to the end without an exception", guard="gate", fromWave=1),
}


#
# Environment helpers (not user actions)
#


def ensureStudio(g, timeout=30000):
    """Wait for the Studio shell (applied at startup); under --testing apply it by hand (Studio never runs by itself there)."""
    from StudioLib import shell as shellModule

    if slicer.app.testingEnabled() and shellModule.instance() is None:
        shellModule.Shell(force=True).apply()
    ok = yield from g.until(lambda: shellModule.instance() is not None and g.mainWindow.isVisible(), timeout, what="Studio shell")
    return ok


def resetState(g):
    """Return to a clean Library: no data, view transform mode, panels shown, default layout. Not a user action."""
    g.closePopups()
    interaction = slicer.app.applicationLogic().GetInteractionNode()
    if interaction is not None:
        interaction.SwitchToViewTransformMode()
    layoutManager = slicer.app.layoutManager()
    if layoutManager is not None:
        layoutManager.removeAllMaximizedViewNodes()
    shell = g.shell()
    if shell is not None:
        shell.setFocusMode(False)
        shell.setInspectorVisible(True)
    slicer.mrmlScene.Clear(0)
    if shell is not None:
        shell.switchTo("library")
    g.pump(300)


def sliceViewPoint(g, sliceName, ras):
    """Local point (in the slice view widget) of an RAS point on the displayed slice."""
    widget = g.sliceWidget(sliceName)
    node = widget.mrmlSliceNode()
    inverse = vtk.vtkMatrix4x4()
    vtk.vtkMatrix4x4.Invert(node.GetXYToRAS(), inverse)
    point = inverse.MultiplyPoint([float(ras[0]), float(ras[1]), float(ras[2]), 1.0])
    view = widget.sliceView()
    return round(point[0]), round(view.height - 1 - point[1])


def zigzag(cx, cy, z, rx, ry, rows=5, perRow=6):
    """RAS points that scribble over an ellipse, like a brush stroke that fills a small area."""
    points = []
    for index in range(rows + 1):
        fy = -1 + 2 * index / rows
        half = rx * (1 - fy * fy) ** 0.5
        y = cy + fy * ry
        xs = [cx - half, cx + half] if index % 2 == 0 else [cx + half, cx - half]
        for step in range(perRow):
            points.append((xs[0] + (xs[1] - xs[0]) * step / (perRow - 1), y, z))
    return points


def sliceOffset(g, name="Red"):
    return g.sliceWidget(name).sliceLogic().GetSliceOffset()


def scrollSign(g, name="Red"):
    """+1 if a wheel notch away from the user (button 4) moves the slice offset up, -1 if down. Calibrated once, not counted."""
    cached = g.notes.get("scrollSign")
    if cached is not None:
        return cached
    widget = g.sliceWidget(name)
    logic = widget.sliceLogic()
    before = logic.GetSliceOffset()
    interactor = widget.sliceView().interactorStyle().GetInteractor()
    interactor.InvokeEvent("MouseWheelForwardEvent")
    g.pump(40)
    after = logic.GetSliceOffset()
    logic.SetSliceOffset(before)
    g.pump(40)
    sign = 1 if after > before else -1
    g.notes["scrollSign"] = sign
    return sign


def wheelToOffset(g, name, target, description="", tolerance=0.6):
    """One wheel burst over a slice view that brings its slice offset to ``target`` (mm)."""
    logic = g.sliceWidget(name).sliceLogic()
    spacing = logic.GetLowestVolumeSliceSpacing()[2] or 1.0
    notches = round((target - logic.GetSliceOffset()) / spacing)
    if notches == 0:
        return True
    view = g.sliceView(name)
    sign = scrollSign(g, name)
    ok = yield from g.wheel(view, notches * sign, description or f"wheel the {name} view to {target:g} mm")
    yield 120
    return ok and abs(logic.GetSliceOffset() - target) <= max(tolerance, spacing / 2 + 0.1)


def shiftMove(g, name, ras, description=""):
    """Shift + mouse move over a slice view (the hidden crosshair gesture): counts 1."""
    view = g.sliceView(name)
    local = sliceViewPoint(g, name, ras)
    point, reason = g.clickability(view, local)
    if reason is not None:
        return g.refuse("shift-move", description or "Shift+move", reason, widgetText(view))
    g.focus()
    g.backend.keyDown("shift")
    g.backend.move(point)
    yield 40
    g.backend.move((point[0] + 3, point[1] + 1))
    yield 80
    g.backend.keyUp("shift")
    g.addStep("shift-move", description or f"Shift+move over {name}", 1, widgetText(view))
    yield 200
    return True


def dockWidthChecks(g, restore=None, effects=("Paint", "Erase", "Draw", "Smoothing", "Threshold", "Paint")):
    """The inspector width on tool changes (state check; the tools are switched through the editor, not counted).

    ``dock-width-first-use``: choosing a tool for the first time does not move the views (USABILITY section 6).
    ``dock-width-stable``: choosing a tool that was used before changes the width by at most 1 px (QW-B learns the width of every tool).
    """
    editor = g.segmentEditor()
    if editor is None or g.dockWidth() == 0:
        g.skip("dock-width-stable", "the inspector is not shown")
        return
    start = g.dockWidth()
    for name in effects:
        editor.setActiveEffectByName(name)
        yield 500
    afterFirstPass = g.dockWidth()
    worst = 0
    for name in effects:
        editor.setActiveEffectByName(name)
        yield 500
        worst = max(worst, abs(g.dockWidth() - afterFirstPass))
    g.check("dock-width-first-use", abs(afterFirstPass - start) <= 1, f"the inspector went from {start} to {afterFirstPass} px while each tool was used for the first time")
    g.check("dock-width-stable", worst <= 1, f"width after a second round of tool changes: {worst} px away from {afterFirstPass}")
    editor.setActiveEffectByName(restore) if restore else editor.setActiveEffect(None)
    yield 300


def toolButton(g, effectName):
    """The Segment Editor effect button of this name (visible), or None."""
    return g.widget(effectName, "QToolButton")


def isInsideWidget(inner, outer):
    return inner is not None and outer is not None and isAncestorOrSelf(outer, inner)


def globalRect(widget):
    origin = widget.mapToGlobal(qt.QPoint(0, 0))
    return origin.x(), origin.y(), widget.width, widget.height


def rectsIntersect(first, second):
    return not (first[0] + first[2] <= second[0] or second[0] + second[2] <= first[0] or first[1] + first[3] <= second[1] or second[1] + second[3] <= first[1])


def bannerOverlapsViews(g):
    """Names of the visible slice views that a download banner overlaps."""
    overlapped = []
    banners = [w for w in g.mainWindow.findChildren("QWidget") if (w.isVisible() and re.search(r"DownloadBanner$", w.className() or "")) or (w.isVisible() and w.objectName in ("StudioDownloadBanner", "DownloadBanner"))]
    if not banners:
        return overlapped
    layoutManager = slicer.app.layoutManager()
    for name in layoutManager.sliceViewNames():
        view = layoutManager.sliceWidget(name).sliceView()
        if not view.isVisible():
            continue
        viewRect = globalRect(view)
        if any(rectsIntersect(globalRect(banner), viewRect) for banner in banners):
            overlapped.append(name)
    return overlapped


def edgeLettersShown(g, name):
    """The (left, right, top, bottom) letters that are really in the renderer of a slice view, or None if there are none."""
    view = g.sliceView(name)
    renderers = view.renderWindow().GetRenderers()
    found = {}
    for index in range(renderers.GetNumberOfItems()):
        renderer = renderers.GetItemAsObject(index)
        props = renderer.GetViewProps()
        for propIndex in range(props.GetNumberOfItems()):
            prop = props.GetItemAsObject(propIndex)
            if prop.IsA("vtkTextActor") and hasattr(prop, "GetObjectName"):
                label = prop.GetObjectName() or ""
                if label.startswith("StudioOrientationLetter_"):
                    found[label.split("_", 1)[1]] = prop.GetInput()
    if not found:
        return None
    return tuple(found.get(edge, "") for edge in ("left", "right", "top", "bottom"))


def expectedEdgeLetters(g, name):
    """Letters of the four screen edges computed from the slice node (independent of the Studio code)."""
    matrix = g.sliceWidget(name).mrmlSliceNode().GetSliceToRAS()
    screenX = [matrix.GetElement(row, 0) for row in range(3)]
    screenY = [matrix.GetElement(row, 1) for row in range(3)]

    def letter(vector):
        axis = max(range(3), key=lambda i: abs(vector[i]))
        return ("R", "A", "S")[axis] if vector[axis] > 0 else ("L", "P", "I")[axis]

    return letter([-v for v in screenX]), letter(screenX), letter(screenY), letter([-v for v in screenY])


def lesionNodes(g):
    editor = g.segmentEditor()
    if editor is None:
        return None, None
    return editor.segmentationNode(), editor.sourceVolumeNode()


def lesionArray(g, referenceVolume=None):
    """The current segment as an array on its own grid (or on ``referenceVolume``)."""
    editor = g.segmentEditor()
    node = editor.segmentationNode()
    segmentId = editor.currentSegmentID()
    return slicer.util.arrayFromSegmentBinaryLabelmap(node, segmentId, referenceVolume)


def readNifti(path):
    """``(array [k, j, i], size, spacing, origin, direction, pixelId)`` of an image file, read with SimpleITK."""
    import SimpleITK as sitk

    image = sitk.ReadImage(path)
    return sitk.GetArrayFromImage(image), image.GetSize(), image.GetSpacing(), image.GetOrigin(), image.GetDirection(), image.GetPixelIDTypeAsString()


def sameGeometry(first, second, tolerance=1e-4):
    """True if two ``readNifti`` results have the same size, spacing, origin and direction (+-tolerance mm)."""
    if tuple(first[1]) != tuple(second[1]):
        return False
    return all(abs(a - b) <= tolerance for a, b in zip(first[2] + first[3] + first[4], second[2] + second[3] + second[4], strict=True))


def activeDialogButton(g, *labels):
    """A button of the modal dialog or popup whose text starts with one of ``labels`` (case-insensitive), else None."""
    top = qt.QApplication.activeModalWidget() or qt.QApplication.activePopupWidget()
    if top is None:
        return None
    for label in labels:
        wanted = label.lower()
        for button in top.findChildren("QAbstractButton"):
            if button.isVisible() and str(button.text).replace("&", "").strip().lower().startswith(wanted):
                return button
    return None


def modalDialog(g):
    return qt.QApplication.activeModalWidget()


def saveFileDialogPath(g, path, description="Type the file name, then Save", folder=False):
    """Fill the modal Qt file dialog with ``path`` (1 action for the field) and confirm (1 action). Return True when the dialog closed.

    For a folder dialog, Enter in the field only navigates into the folder: the field is typed with Enter, and Choose confirms.
    """
    dialog = qt.QApplication.activeModalWidget()
    if dialog is None:
        yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None, 4000, what="file dialog")
        dialog = qt.QApplication.activeModalWidget()
    if dialog is None:
        g.check("task-completed", False, "the file dialog did not open")
        return False
    nameEdit = dialog.findChild("QLineEdit", "fileNameEdit")
    if nameEdit is None:
        g.check("task-completed", False, "the file dialog has no file name field")
        return False
    yield from g.type(nameEdit, path, description, selectAll=True, enter=folder)
    yield 500

    def dialogOpen():
        active = qt.QApplication.activeModalWidget()
        return active is not None and sameObject(active, dialog)

    attempts = 0
    while dialogOpen() and attempts < 3:
        button = None
        for candidate in dialog.findChildren("QPushButton"):
            if candidate.isVisible() and str(candidate.text).replace("&", "").strip().lower() in ("save", "open", "choose"):
                button = candidate
        # The first click may only close the completion list under the file name field (a second one confirms)
        yield from g.click(button, "Confirm the file dialog")
        yield 500
        attempts += 1
    return not dialogOpen()


def waitForDraw(g, timeout=20000):
    """Wait until Draw is current and the Segment Editor holds a segmentation, a segment and a source volume."""

    def ready():
        editor = g.segmentEditor()
        return (g.currentWorkspace() == "draw" and editor is not None and editor.segmentationNode() is not None
                and editor.sourceVolumeNode() is not None and bool(editor.currentSegmentID()))

    ok = yield from g.until(ready, timeout, what="Draw ready")
    yield 600
    return ok


def closeAppStep(g):
    """Close the application window (the window's close button; counts 1) and answer the close prompt; the gate keeps running (the window is shown again)."""
    qt.QApplication.setQuitOnLastWindowClosed(False)
    try:
        yield from g.closeWindow(g.mainWindow, "Close the application window")
        yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None or not g.mainWindow.isVisible(), 4000, what="close prompt")
        modal = qt.QApplication.activeModalWidget()
        if modal is not None:
            g.shot("close-prompt")
            g.note("closePrompt", {"text": str(getattr(modal, "text", "")), "buttons": [str(b.text) for b in modal.findChildren("QPushButton") if b.isVisible()]})
            button = activeDialogButton(g, "exit", "quit", "discard", "close", "don't save")
            yield from g.click(button, "Answer the close prompt: exit")
        yield from g.until(lambda: not g.mainWindow.isVisible(), 6000, what="main window closing")
        g.note("closed", not g.mainWindow.isVisible())
        g.mainWindow.show()
        g.fitWindow()
        yield 400
    finally:
        qt.QApplication.setQuitOnLastWindowClosed(True)


#
# Task: novice-first-trace
#


def startupAndDisclaimer(g):
    """Start of a first-run task: through the real startup path the disclaimer (a modal) is open; click OK (1 action)."""
    yield from ensureStudio(g)
    window = g.mainWindow
    screenWidth, screenHeight = g.screenGeometry()
    covers = window.width >= 0.85 * screenWidth and window.height >= 0.85 * screenHeight
    if g.config.get("viaRc"):
        g.check("first-run-window", covers, f"window {window.width}x{window.height} on a {screenWidth}x{screenHeight} screen")
    else:
        g.skip("first-run-window", "the task did not start through SLICERRC (no first-run startup path)")
    if g.config.get("viaRc"):
        yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None, 15000, what="first-run disclaimer")
        g.shot("first-launch-disclaimer")
        button = activeDialogButton(g, "ok")
        if button is None:
            g.warn("no first-run disclaimer modal appeared")
        else:
            yield from g.click(button, "Read the disclaimer, click OK")
            yield 400
    else:
        g.warn("the first-run disclaimer path is not exercised (the task did not start through SLICERRC)")
    return True


def startOnTemplateStep(g):
    """Click 'Start on MNI template' and wait for Draw. Return the list of facts about the landing."""
    button = g.widget("StudioLibraryStartOnTemplateButton", "QPushButton")
    yield from g.click(button, "Click 'Start on MNI template'")
    ready = yield from waitForDraw(g)
    return ready


def dismissBanner(g):
    """Dismiss the 1 mm template banner if it is visible (1 action). Return True if it was."""
    overlapped = bannerOverlapsViews(g)
    g.check("no-banner-overlap", not overlapped, f"the download banner overlaps the {', '.join(overlapped)} view" if overlapped else "")
    button = g.widget("StudioDownloadBannerSecondaryButton", "QPushButton")
    if button is not None:
        yield from g.click(button, "Dismiss the 1 mm template banner ('Not now')")
        yield 200
        return True
    return False


def armBrush(g):
    """If no tool is armed on arrival, pick Paint (1 action); otherwise nothing."""
    armed = g.effectName()
    g.check("effect-armed-on-entry", armed == "Paint", f"active effect on arrival: {armed}")
    if armed is None:
        yield from g.click(toolButton(g, "Paint"), "Click the Paint tool")
        yield 300
    return g.effectName()


def paintSlice(g, z, centerXY, radii, description):
    """Two strokes that fill an ellipse at S=z on the Red view. Return the voxel count after."""
    points = zigzag(centerXY[0], centerXY[1], z, radii[0], radii[1])
    half = len(points) // 2
    view = g.sliceView("Red")
    for index, chunk in enumerate((points[: half + 1], points[half:])):
        local = [sliceViewPoint(g, "Red", p) for p in chunk]
        yield from g.drag(view, local, f"{description}: stroke {index + 1}")
    yield 150
    return g.voxelCount()


def novice(g):
    """The novice walkthrough: first launch, template, a 5-slice lesion, check views, wrong stroke and Undo, Erase, Save lesion, quit."""
    fast = bool(g.config.get("fast"))
    yield from startupAndDisclaimer(g)
    shell = g.shell()
    g.shot("library-first-run")
    yield from startOnTemplateStep(g)
    g.shot("draw-landing")
    yield from dismissBanner(g)
    yield from armBrush(g)
    dockBefore = g.dockWidth()
    editor = g.segmentEditor()
    spacing = g.sliceWidget("Red").sliceLogic().GetLowestVolumeSliceSpacing()[2]
    start = sliceOffset(g)
    # Left thalamus, MNI152NLin2009cAsym: centre about (-11, -18, 7)
    slices = [13.5, 11.5, 9.5, 7.5, 5.5]
    scale = {13.5: 0.6, 11.5: 0.9, 9.5: 1.0, 7.5: 0.9, 5.5: 0.6}
    before = g.voxelCount() or 0
    for index, z in enumerate(slices):
        # The template slices are 2 mm apart: snap to the grid of the displayed volume
        yield from wheelToOffset(g, "Red", z, f"Wheel to axial S={z}")
        voxels = yield from paintSlice(g, sliceOffset(g), (-11.0, -18.0), (5.5 * scale[z], 8.0 * scale[z]), f"Brush the left thalamus at S={z}")
        if index in (0, 2, 4):
            g.shot(f"painted-slice-{index + 1}")
    lesionVoxels = g.voxelCount()
    g.note("lesionVoxelsAfterFiveSlices", lesionVoxels)
    g.check("dock-width-stable", abs(g.dockWidth() - dockBefore) <= 1, f"dock {dockBefore} -> {g.dockWidth()} px while painting")
    g.note("dockWidthWhilePainting", [dockBefore, g.dockWidth()])
    # Check the lesion in the other views: do they follow? (planned: follow strokes) else Shift+move over the lesion (hidden gesture)
    centroid = (-11.0, -18.0, 9.5)
    follows = abs(sliceOffset(g, "Green") - centroid[1]) <= 3.0 and abs(sliceOffset(g, "Yellow") - centroid[0]) <= 3.0
    if not follows:
        yield from shiftMove(g, "Red", centroid, "Shift+move over the lesion in the axial view to see it in the other views")
    g.note("otherViewsFollow", follows)
    g.shot("check-other-views")
    # A wrong stroke on the right thalamus, then Undo from the top bar
    countBefore = g.voxelCount()
    yield from wheelToOffset(g, "Red", 9.5, "Wheel back to S=9.5")
    wrong = [sliceViewPoint(g, "Red", (x, -18.0, sliceOffset(g))) for x in (9.0, 11.0, 13.0)]
    yield from g.drag(g.sliceView("Red"), wrong, "Mistake: a stroke on the right thalamus")
    yield 150
    countWrong = g.voxelCount()
    yield from g.click(g.widget("StudioUndoButton", "QToolButton"), "Click Undo in the top bar")
    yield 300
    countUndone = g.voxelCount()
    g.note("undo", {"before": countBefore, "afterWrong": countWrong, "afterUndo": countUndone})
    g.check("effect-after-return", g.effectName() is not None, f"effect after Undo: {g.effectName()}")
    # Erase a few voxels at the edge of the lesion
    yield from g.click(toolButton(g, "Erase"), "Click the Erase tool")
    yield 200
    edge = [sliceViewPoint(g, "Red", (x, -18.0, sliceOffset(g))) for x in (-14.0, -12.5, -11.0)]
    yield from g.drag(g.sliceView("Red"), edge, "Erase a few voxels along the lesion")
    yield 150
    countErased = g.voxelCount()
    g.note("erase", {"before": countUndone, "after": countErased})
    # Save lesion... to <out>/save/sub-01_lesion.nii.gz
    savePath = os.path.join(g.out, "save", "sub-01_lesion.nii.gz")
    os.makedirs(os.path.dirname(savePath), exist_ok=True)
    if os.path.exists(savePath):
        os.remove(savePath)
    segmentationNode, _sourceVolume = lesionNodes(g)
    expectedArray = lesionArray(g).copy()
    yield from g.click(g.widget("StudioSaveLesionButton", "QPushButton"), "Click 'Save lesion…'")
    yield from saveFileDialogPath(g, savePath)
    yield from g.until(lambda: os.path.isfile(savePath), 6000, what="the lesion file")
    yield 300
    g.shot("saved")
    checkSavedLesion(g, savePath, expectedArray, segmentationNode, "save-grid-preserved")
    yield from dockWidthChecks(g, restore=g.effectName())
    if not fast:
        yield from officialLoadCheck(g, savePath, readNifti(savePath))
    if not fast and getattr(g, "isLastJob", True):
        yield from closeAppStep(g)
    g.note("sliceStart", start)
    g.note("sliceSpacing", spacing)


def lesionGrid(segmentationNode):
    """``(dims, 4x4 ijk->RAS array)`` of the grid the lesion is drawn on (its reference image geometry), or None."""
    parameter = segmentationNode.GetSegmentation().GetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName())
    if not parameter:
        return None
    image = slicer.vtkOrientedImageData()
    slicer.vtkSegmentationConverter.DeserializeImageGeometry(parameter, image, False)
    matrix = vtk.vtkMatrix4x4()
    image.GetImageToWorldMatrix(matrix)
    return tuple(image.GetDimensions()), np.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)])


def fileGrid(saved):
    """``(dims, 4x4 ijk->RAS array)`` of a ``readNifti`` result (SimpleITK works in LPS)."""
    directions = np.array(saved[4]).reshape(3, 3)
    ijkToLps = np.eye(4)
    ijkToLps[:3, :3] = directions * np.array(saved[2])
    ijkToLps[:3, 3] = saved[3]
    return tuple(saved[1]), np.diag([-1.0, -1.0, 1.0, 1.0]) @ ijkToLps


def checkSavedLesion(g, path, expectedArray, segmentationNode, assertionId, tolerance=1e-4):
    """The saved file is a uint8 NIfTI with the grid of the lesion (dims and sform within 1e-4 mm) and its voxels."""
    if not os.path.isfile(path):
        g.check(assertionId, False, f"{path} was not written")
        return False
    saved = readNifti(path)
    problems = []
    if "8-bit unsigned" not in saved[5]:
        problems.append(f"pixel type {saved[5]}")
    grid = lesionGrid(segmentationNode)
    if grid is not None:
        dims, matrix = fileGrid(saved)
        if dims != grid[0]:
            problems.append(f"dims {dims} != lesion grid {grid[0]}")
        elif float(np.abs(matrix - grid[1]).max()) > tolerance:
            problems.append(f"sform differs from the lesion grid by {float(np.abs(matrix - grid[1]).max()):.4g} mm")
    if expectedArray is not None:
        savedMask = saved[0] > 0
        if savedMask.shape != expectedArray.shape or int(savedMask.sum()) != int(np.count_nonzero(expectedArray)):
            problems.append(f"voxels {int(savedMask.sum())} != lesion {int(np.count_nonzero(expectedArray))}")
        elif not np.array_equal(savedMask, expectedArray > 0):
            problems.append("voxel positions differ")
    g.check(assertionId, not problems, "; ".join(problems) or f"{tuple(saved[1])} uint8, {int((saved[0] > 0).sum())} voxels")
    return not problems


#
# Planned features: detected by object name, so that a task uses a feature as soon as its work package lands
#

PLANNED = {
    # flat view bar and View section (W2-03, U-06)
    "viewSection": r"^Studio(View|Display)(Section|Button|Popover)|^StudioViewMenu",
    "displayPresets": r"DisplayPreset|WindowLevelPreset|^StudioWindowLevel",
    "linkToggle": r"^Studio.*Link(Views)?(Button|Toggle)|^StudioLinkViews",
    "crosshairToggle": r"^Studio.*Crosshair(Button|Toggle)|^StudioCrosshair",
    "probe": r"^StudioProbe|^StudioInViewProbe",
    "palette": r"^StudioPalette",
    "modePill": r"^StudioModePill",
    "inspectorHandle": r"^StudioInspectorHandle",
    "inspectorNotice": r"^StudioInspectorNotice",
    "notesField": r"^StudioNotes|^StudioCaseNotes|^StudioCaseCard",
    "conventionChip": r"^Studio.*Convention",
    "loadedSet": r"^StudioLoadedSet|^StudioSetNavigator",
    "cheatSheet": r"^StudioCheatSheet",
    "continueInDraw": r"^Studio.*Continue",
}


def planned(g, key, root=None):
    """The first visible widget whose object name matches ``PLANNED[key]`` (the feature exists), or None."""
    pattern = re.compile(PLANNED[key])
    for top in ([root] if root is not None else g.topLevelsInOrder() + [g.mainWindow]):
        if top is None:
            continue
        for widget in top.findChildren("QWidget"):
            name = widget.objectName
            if name and pattern.search(name) and widget.isVisible():
                return widget
    return None


def appMenuButton(g):
    return g.widget("StudioAppMenuButton", "QToolButton")


def appMenu(g, path, description=""):
    """Click through the ≡ menu: ``["View", "Toolbars", "Markups"]`` (1 action per click)."""
    return (yield from g.menu(appMenuButton(g), path, description or "≡ > " + " > ".join(path)))


def sceneNodeCount():
    return slicer.mrmlScene.GetNumberOfNodes()


def windowLevel():
    node = slicer.app.layoutManager().sliceWidget("Red").sliceLogic().GetBackgroundLayer().GetVolumeDisplayNode()
    return (round(node.GetWindow(), 2), round(node.GetLevel(), 2)) if node is not None else None


def interactionMode():
    return slicer.app.applicationLogic().GetInteractionNode().GetCurrentInteractionMode()


def viewTransformMode():
    return slicer.vtkMRMLInteractionNode.ViewTransform


def fieldOfView(g, name="Red"):
    return tuple(round(v, 2) for v in g.sliceWidget(name).mrmlSliceNode().GetFieldOfView())


def hoverKeyMapped(g, name, keysym):
    """True if a key in a slice view moves the slice (probed silently with the view's interactor; the offset is restored)."""
    widget = g.sliceWidget(name)
    logic = widget.sliceLogic()
    before = logic.GetSliceOffset()
    interactor = widget.sliceView().interactorStyle().GetInteractor()
    interactor.SetKeySym(keysym)
    interactor.SetKeyCode("\0")
    interactor.InvokeEvent("KeyPressEvent")
    interactor.InvokeEvent("KeyReleaseEvent")
    g.pump(40)
    moved = abs(logic.GetSliceOffset() - before) > 1e-6
    logic.SetSliceOffset(before)
    g.pump(40)
    return moved


def edgeLetterCheck(g, name="Red"):
    """Assert that the edge letters of a slice view are present and correct (Red axial: R on the left edge)."""
    shown = edgeLettersShown(g, name)
    expected = expectedEdgeLetters(g, name)
    if shown is None:
        g.check("edge-letters-correct", False, f"no edge letters in the {name} view (expected {expected})")
        return False
    ok = tuple(shown) == tuple(expected)
    if name == "Red" and tuple(expected) == ("R", "L", "A", "P"):
        ok = ok and shown[0] == "R"
    g.check("edge-letters-correct", ok, f"{name}: shown {tuple(shown)}, expected {tuple(expected)}")
    return ok


#
# Task: patient-scan-navigation
#


def nav(g):
    """Open a patient scan, navigate (wheel, zoom, pan, window/level, layouts, link, crosshair, maximize), paint with an intensity range,
    smooth, return to Library and continue.
    """
    yield from ensureStudio(g)
    resetState(g)
    scanPath = g.fixtures["patientScan"]
    blob = g.fixtures["patientBlobScannerRAS"]
    yield from g.drop([scanPath], "Drop the patient scan on the window")
    yield from waitForDraw(g)
    g.shot("draw-landing")
    edgeLetterCheck(g, "Red")
    editor = g.segmentEditor()
    g.note("landingEffect", g.effectName())
    # Wheel to the frontal lobe (the hypointense blob)
    yield from wheelToOffset(g, "Red", blob[2], "Wheel the axial view to the frontal lobe")
    g.shot("frontal")
    # PageUp, when it is mapped to the slice step
    if hoverKeyMapped(g, "Red", "Prior"):
        yield from g.hover(g.sliceView("Red"), "Point at the axial view")
        before = sliceOffset(g)
        yield from g.key("PageUp", "PageUp steps the slice")
        g.note("pageUp", {"moved": abs(sliceOffset(g) - before) > 1e-6})
    else:
        g.note("pageUp", "not mapped")
    # Zoom (Ctrl+wheel), pan (middle drag) and fit
    fov = fieldOfView(g)
    yield from g.wheel(g.sliceView("Red"), 3, "Zoom in with Ctrl+wheel", modifiers=("ctrl",))
    g.note("zoomChangedFov", fieldOfView(g) != fov)
    view = g.sliceView("Red")
    center = (view.width // 2, view.height // 2)
    yield from g.drag(view, [center, (center[0] + 60, center[1] + 30), (center[0] + 120, center[1] + 60)], "Pan with the middle button", button=2)
    g.shot("zoomed-panned")
    redBar = g.widgets("FitToWindowToolButton", "QToolButton")
    fitButton = min(redBar, key=lambda w: w.mapToGlobal(qt.QPoint(0, 0)).x()) if redBar else None
    yield from g.click(fitButton, "Fit the view to the window")
    # Window/level: a Display preset when it exists, else the view's context menu (Adjust window/level), drag, Esc
    levelBefore = windowLevel()
    presets = planned(g, "displayPresets")
    if presets is not None:
        yield from g.click(presets, "Choose a Display preset")
        yield 300
    else:
        yield from g.click(g.sliceView("Red"), "Right-click the axial view", button=3)
        yield from g.until(lambda: qt.QApplication.activePopupWidget() is not None, 2000)
        menu = qt.QApplication.activePopupWidget()
        if menu is not None:
            yield from g.clickMenuItem(menu, "Adjust window/level", "Choose Adjust window/level")
            g.note("modeAfterMenu", interactionMode())
            yield from g.drag(g.sliceView("Red"), [(center[0], center[1]), (center[0] + 40, center[1] - 30), (center[0] + 80, center[1] - 60)], "Drag to adjust window/level")
            yield from g.key("Esc", "Esc leaves the window/level mode")
            g.check("esc-ladder", interactionMode() == viewTransformMode(), f"interaction mode after Esc: {interactionMode()}")
    g.note("windowLevel", {"before": levelBefore, "after": windowLevel()})
    # Layouts: the picker, then Alt+3, Alt+4, Alt+1
    layoutManager = slicer.app.layoutManager()
    layoutBefore = layoutManager.layout
    yield from g.click(g.widget("StudioLayoutButton", "QToolButton"), "Open the layout picker")
    yield from g.until(lambda: g.widget("StudioLayoutTile_single", "QToolButton") is not None, 2000)
    yield from g.click(g.widget("StudioLayoutTile_single", "QToolButton"), "Pick the Single layout")
    singleLayout = layoutManager.layout
    g.note("layouts", {"before": layoutBefore, "single": singleLayout})
    for chord, label in (("Alt+3", "Four-up"), ("Alt+4", "Row"), ("Alt+1", "Focus")):
        yield from g.key(chord, f"{chord} switches to the {label} layout")
        yield 200
    g.shot("layouts")
    screenWidth, screenHeight = g.screenGeometry()
    window = g.mainWindow
    right, bottom = window.x + window.width, window.y + window.height
    g.check("window-fits-screen", right <= screenWidth and bottom <= screenHeight, f"the main window is {window.width}x{window.height} at ({window.x}, {window.y}) on a {screenWidth}x{screenHeight} screen after the layouts")
    # Link the views: the toggle of the view bar (hover popup in Classic bars)
    linkToggle = planned(g, "linkToggle")
    if linkToggle is not None:
        yield from g.click(linkToggle, "Link the views")
        yield from g.wheel(g.sliceView("Red"), -3, "Zoom out while linked", modifiers=("ctrl",))
        yield from g.click(linkToggle, "Unlink the views")
    else:
        pins = sorted(g.widgets("PinButton", "QToolButton"), key=lambda w: (w.mapToGlobal(qt.QPoint(0, 0)).y(), w.mapToGlobal(qt.QPoint(0, 0)).x()))
        pin = pins[0] if pins else None
        yield from g.hover(pin, "Hover the pin of the Red view bar (its popup holds the link toggle)")
        yield from g.until(lambda: g.widget("SliceLinkButton", "QToolButton") is not None, 2500)
        linkButton = g.widget("SliceLinkButton", "QToolButton")
        yield from g.click(linkButton, "Link the views")
        yield from g.wheel(g.sliceView("Red"), -3, "Zoom out while linked", modifiers=("ctrl",))
        yield from g.hover(pin, "Hover the pin again")
        yield from g.until(lambda: g.widget("SliceLinkButton", "QToolButton") is not None, 2500)
        yield from g.click(g.widget("SliceLinkButton", "QToolButton"), "Unlink the views")
    # Crosshair: its toggle, or the Crosshair Selection toolbar from View > Toolbars
    crosshair = planned(g, "crosshairToggle")
    if crosshair is not None:
        yield from g.click(crosshair, "Show the crosshair")
    else:
        yield from appMenu(g, ["View", "Toolbars", "Crosshair Selection"], "Show the Crosshair toolbar")
        yield 400
        toolbar = g.widget("ViewersToolBar", "QToolBar")
        button = None
        if toolbar is not None:
            buttons = [b for b in toolbar.findChildren("QToolButton") if b.isVisible()]
            button = buttons[0] if buttons else None
        yield from g.click(button, "Click the crosshair button")
    crosshairNode = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
    g.note("crosshairMode", crosshairNode.GetCrosshairMode() if crosshairNode is not None else None)
    # Maximize a view (double-click) and restore it with Esc
    yield from g.click(g.sliceView("Red"), "Double-click the axial view to maximize it", double=True)
    yield 400
    maximized = layoutManager.layoutLogic().GetLayoutNode().GetNumberOfMaximizedViewNodes() > 0
    g.note("maximized", maximized)
    yield from g.key("Esc", "Esc restores the layout")
    yield 300
    restored = layoutManager.layoutLogic().GetLayoutNode().GetNumberOfMaximizedViewNodes() == 0
    g.check("esc-ladder", (not maximized) or restored, f"maximized view after Esc: {not restored}")
    # The intensity under the pointer: the in-view probe when it exists, else ⋯ > Show Data Probe
    if planned(g, "probe") is None:
        yield from g.click(g.widget("StudioPanelMoreButton", "QToolButton"), "Open the panel menu (⋯)")
        yield from g.until(lambda: qt.QApplication.activePopupWidget() is not None, 2000)
        menu = qt.QApplication.activePopupWidget()
        if menu is not None:
            yield from g.clickMenuItem(menu, "Show Data Probe", "Show Data Probe")
    # Paint with an intensity range (white matter)
    paint = toolButton(g, "Paint")
    if g.effectName() != "Paint":
        yield from g.click(paint, "Pick the Paint tool")
        yield 300
    yield from g.click(g.widget("SourceVolumeIntensityMaskCheckBox", "QCheckBox"), "Tick 'Editable intensity range'", localPoint=(7, 7))
    yield 300
    rangeWidget = g.widget("SourceVolumeIntensityMaskRangeWidget")
    fields = [f for f in rangeWidget.findChildren("QLineEdit") if f.isVisible()] if rangeWidget is not None else []
    if len(fields) >= 2:
        yield from g.type(fields[0], "500", "Type the lower intensity", selectAll=True, enter=True)
        yield from g.type(fields[1], "1000", "Type the upper intensity", selectAll=True, enter=True)
    g.note("intensityRangeFields", len(fields))
    wm = (blob[0] - 14.0, blob[1] + 4.0, blob[2])
    scribble = zigzag(wm[0], wm[1], sliceOffset(g), 7.0, 7.0, rows=6, perRow=8)
    half = len(scribble) // 2
    for index, chunk in enumerate((scribble[: half + 1], scribble[half:])):
        yield from g.drag(g.sliceView("Red"), [sliceViewPoint(g, "Red", p) for p in chunk], f"Paint stroke {index + 1} in the frontal white matter")
    g.note("paintedVoxels", g.voxelCount())
    # No hemisphere word for a lesion in scanner space (a patient scan is not on an MNI grid)
    words = [w for w in g.dock().findChildren("QLabel") if w.isVisible() and re.search(r"\b(Left|Right|Both hemispheres)\b", str(w.text))]
    g.check("no-hemisphere-word-scanner", not words, "hemisphere word in the inspector for a scanner-space lesion: " + "; ".join(str(w.text)[:40] for w in words[:3]))
    # Smooth the lesion
    yield from g.click(toolButton(g, "Smoothing"), "Pick the Smoothing tool")
    yield 400
    apply = g.buttonWithText("Apply")
    yield from g.click(apply, "Apply the smoothing")
    yield 600
    lastTool = g.effectName()
    yield from dockWidthChecks(g, restore=lastTool)
    # Back to Library and continue: the tool, the lesion and the slice survive the round trip
    offset = sliceOffset(g)
    voxels = g.voxelCount()
    nodesBefore = sceneNodeCount()
    yield from g.click(g.widget("StudioLibraryWorkspaceButton", "QToolButton"), "Go back to Library")
    yield 500
    g.shot("library-with-data")
    cont = planned(g, "continueInDraw")
    if cont is not None:
        yield from g.click(cont, "Continue in Draw")
    else:
        yield from g.until(lambda: g.widget("StudioRecentRow", "QWidget") is not None, 3000)
        yield from g.click(g.widget("StudioRecentRow", "QWidget"), "Click the scan in Recent")
    yield from waitForDraw(g)
    g.check("effect-after-return", g.effectName() == lastTool, f"effect after returning to Draw: {g.effectName()}, last tool: {lastTool}")
    g.check("recent-reopen-no-node", sceneNodeCount() == nodesBefore and g.voxelCount() == voxels, f"nodes {nodesBefore} -> {sceneNodeCount()}, lesion voxels {voxels} -> {g.voxelCount()}")
    g.note("sliceOffsetKept", abs(sliceOffset(g) - offset) < 1e-6)
    g.shot("back-in-draw")


#
# Search (Ctrl+K): the module finder dialog now, the palette when it lands
#


def searchPopup(g):
    """The open search: the palette (planned), else the module finder dialog. None if nothing is open."""
    for top in (qt.QApplication.activePopupWidget(), qt.QApplication.activeModalWidget()):
        if top is not None:
            return top
    palette = planned(g, "palette")
    if palette is not None:
        return palette.window()
    for widget in qt.QApplication.topLevelWidgets():
        if widget.isVisible() and widget.className() == "qSlicerModuleFinderDialog":
            return widget
    return None


def searchField(top):
    """The line edit of the search (the filter box of the module finder dialog, or the palette's field)."""
    if top is None:
        return None
    box = top.findChild("QLineEdit", "FilterTitleSearchBox")
    if box is not None:
        return box
    for edit in top.findChildren("QLineEdit"):
        if edit.isVisible() and edit.objectName != "qt_spinbox_lineedit":
            return edit
    return None


def searchResults(top, limit=6):
    """The texts of the first rows of the result list, and the text of the current one (None if none is selected)."""
    if top is None:
        return [], None
    views = [v for v in top.findChildren("QAbstractItemView") if v.isVisible() and v.model() is not None]
    if not views:
        return [], None
    view = max(views, key=lambda v: v.width * v.height)
    model = view.model()
    rows = [str(model.index(row, 0).data()) for row in range(min(model.rowCount(), limit))]
    current = view.currentIndex()
    return rows, (str(current.data()) if current.isValid() else None)


def openSearch(g, how="key"):
    """Open the search with Ctrl+K (or by clicking the pill): 1 action."""
    if how == "pill":
        yield from g.click(g.widget("StudioSearchPill", "QPushButton"), "Click the search pill")
    else:
        yield from g.key("Ctrl+K", "Press Ctrl+K to search")
    yield from g.until(lambda: searchPopup(g) is not None and searchField(searchPopup(g)) is not None, 4000, what="search")
    yield 300
    return searchPopup(g)


def searchAndOpen(g, title, natural=None, maxCharacters=16):
    """Open the search, type the natural query for ``title`` (1 action per character) and open the tool with Enter.

    A person types the start of the name (``natural``: "vol" for Volumes). If the wanted tool is not the selected result then,
    more characters of its name are typed until it is: this measures the ranking. When the tool is already selected on opening
    (a most-recently-used list, the warm session), only Enter is needed. Return the number of characters typed, or None if the
    tool was not found (the search is closed with Esc).
    """
    natural = natural or title
    top = yield from openSearch(g)
    if top is None:
        return None

    def selected():
        rows, current = searchResults(searchPopup(g))
        return (current or (rows[0] if rows else "") or "").strip().lower() == title.lower()

    typed = 0
    if not selected():
        full = title.lower()
        while typed < maxCharacters and (typed < len(natural) or not selected()) and typed < len(full):
            character = natural[typed] if typed < len(natural) else full[typed]
            yield from g.type(None, character, f"Type {character!r} in the search", perCharacter=True, settle=100)
            typed += 1
            yield 120
    found = selected()
    if found:
        yield from g.key("Return", f"Enter opens {title}")
        yield 800
    else:
        g.note(f"search:{title}", {"typed": typed, "rows": searchResults(searchPopup(g))[0]})
        g.check("task-completed", False, f"the search did not offer {title} after {typed} characters: {searchResults(searchPopup(g))[0]}")
        yield from g.key("Esc", "Esc closes the search")
        yield 300
    g.note(f"searchTyped:{title}", typed)
    return typed if found else None


def waitTransient(g, moduleName, timeout=6000):
    """Wait until the module is shown in the inspector as a transient module (no workspace tab checked)."""
    shell = g.shell()
    ok = yield from g.until(lambda: shell is not None and shell.currentModuleName() == moduleName, timeout, what=f"module {moduleName}")
    yield 500
    return ok


def closeTransient(g, description="Close the tool and return"):
    """Click ✕ in the inspector header ('Close and return to <workspace>'): 1 action. Return True if Draw is back."""
    button = g.widget("StudioPanelCloseButton", "QToolButton")
    yield from g.click(button, description)
    ok = yield from g.until(lambda: g.currentWorkspace() == "draw", 4000, what="back in Draw")
    yield 500
    return ok


def classicMenuBar(g):
    return g.mainWindow.menuBar()


def clickMenuBarItem(g, text, description=""):
    """Click an item of the Classic menu bar (1 action); the menu opens."""
    bar = classicMenuBar(g)
    wanted = text.replace("&", "").lower()
    action = None
    for candidate in bar.actions():
        if str(candidate.text).replace("&", "").lower() == wanted:
            action = candidate
    if action is None or not bar.isVisible():
        return g.refuse("menubar", description or text, "the menu bar is hidden or has no such menu", text)
    geometry = bar.actionGeometry(action)
    local = (geometry.x() + geometry.width() // 2, geometry.y() + geometry.height() // 2)
    point = bar.mapToGlobal(qt.QPoint(local[0], local[1]))
    hit = qt.QApplication.widgetAt(point)
    if hit is None or not isAncestorOrSelf(bar, hit):
        return g.refuse("menubar", description or text, f"covered by {widgetText(hit)}", text)
    g.backend.click((point.x(), point.y()))
    g.addStep("menubar", description or f"menu bar: {text}", 1, text)
    yield 400
    return True


#
# Task: find-tools-and-return (first session, and the warm session in the settings folder of the first)
#


# T7 of find-tools: (query, words one of the first rows must contain). The cheapest possible T7 is Ctrl+K and Esc per query.
T7_QUERIES = (
    ("threshold", ("threshold",)),
    ("light", ("light", "appearance", "theme")),
    ("undo", ("undo",)),
    ("python", ("python",)),
    ("brush", ("brush", "paint", "draw")),
    ("window level", ("window", "volumes", "display")),
    ("fiducial", ("fiducial", "markups", "point")),
)
T7_PALETTE_CHARACTERS = 2
T7_ROWS_READ = 6
# Ctrl+K style actions of T10 (Classic round trip): menu button, item, View menu, item.
T10_ACTIONS = 4


def searchShows(g, wanted):
    """True if one of the first rows of the open search contains one of the words ``wanted`` (lower case)."""
    rows, _current = searchResults(searchPopup(g), limit=T7_ROWS_READ)
    return any(word in row.lower() for row in rows for word in wanted)


def findToolsBody(g, warm=False):
    yield from ensureStudio(g)
    resetState(g)

    # Setup (not counted): start on the template with one stroke on the lesion
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Setup: Start on MNI template", setup=True)
    yield from waitForDraw(g)
    banner = g.widget("StudioDownloadBannerSecondaryButton", "QPushButton")
    if banner is not None:
        yield from g.click(banner, "Setup: dismiss the template banner", setup=True)
    if g.effectName() is None:
        yield from g.click(toolButton(g, "Paint"), "Setup: pick Paint", setup=True)
    view = g.sliceView("Red")
    stroke = [sliceViewPoint(g, "Red", (-10.0 + i, -18.0 + (i % 3), sliceOffset(g))) for i in range(0, 12, 2)]
    yield from g.drag(view, stroke, "Setup: one stroke on the lesion", setup=True)
    yield 300
    g.shot("setup-done")
    baseVoxels = g.voxelCount()
    baseTool = g.effectName()
    g.note("setup", {"voxels": baseVoxels, "tool": baseTool})
    taskCounts = {}

    def begin(name):
        taskCounts[name] = g.actions

    def end(name):
        taskCounts[name] = g.actions - taskCounts[name]
        g.log("task-part", part=name, actions=taskCounts[name])

    # T1: a Display (window/level) preset of Volumes, and back with the tool armed
    begin("T1")
    typed = yield from searchAndOpen(g, "Volumes", "vol")
    if typed is not None:
        yield from waitTransient(g, "Volumes")
        g.shot("T1-volumes")
        preset = None
        for group in (g.widget("PresetsGroupBox", "QGroupBox"),):
            if group is not None:
                for candidate in group.findChildren("QToolButton"):
                    if candidate.isVisible() and ("Brain" in candidate.objectName or "Brain" in str(candidate.toolTip)):
                        preset = candidate
        levelBefore = windowLevel()
        yield from g.click(preset, "Click the CT-Brain preset")
        yield 300
        g.note("T1-windowLevel", {"before": levelBefore, "after": windowLevel()})
        yield from closeTransient(g, "Close Volumes and return to Draw")
        g.check("effect-after-return", g.effectName() == baseTool, f"tool after T1: {g.effectName()}, before: {baseTool}")
    end("T1")
    # T2: place a point (Markups), and back with the tool still armed and painting
    begin("T2")
    typed = yield from searchAndOpen(g, "Markups", "mark")
    if typed is not None:
        yield from waitTransient(g, "Markups")
        create = g.widget("CreateFiducialPushButton", "QPushButton")
        yield from g.click(create, "Click 'Point List' to place points")
        yield 400
        yield from g.click(g.sliceView("Red"), "Click in the axial view to place a point", localPoint=(g.sliceView("Red").width // 2 + 120, g.sliceView("Red").height // 2 + 80))
        yield 300
        fiducials = slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode")
        g.note("T2-points", [node.GetNumberOfControlPoints() for node in fiducials])
        yield from closeTransient(g, "Close Markups and return to Draw")
        g.check("effect-after-return", g.effectName() == baseTool, f"tool after T2 (Place point): {g.effectName()}, before: {baseTool}")
        keepsTool = g.effectName() == baseTool
        if not keepsTool:
            yield from g.click(toolButton(g, baseTool or "Paint"), f"Pick {baseTool or 'Paint'} again (the point placement dropped it)")
            yield 300
        voxelsBefore = g.voxelCount()
        stroke = [sliceViewPoint(g, "Red", (-9.0 + i, -19.0 + (i % 3), sliceOffset(g))) for i in range(0, 12, 2)]
        yield from g.drag(view, stroke, "Drag in the view: it should paint")
        yield 200
        painted = (g.voxelCount() or 0) > (voxelsBefore or 0)
        g.check("place-keeps-tool", keepsTool and painted, f"tool re-armed by itself: {keepsTool}; the drag painted: {painted}")
    end("T2")
    # T3: Transforms, and back
    begin("T3")
    typed = yield from searchAndOpen(g, "Transforms", "trans")
    if typed is not None:
        yield from waitTransient(g, "Transforms")
        g.shot("T3-transforms")
        yield from closeTransient(g, "Close Transforms and return to Draw")
        g.check("effect-after-return", g.effectName() == baseTool, f"tool after T3: {g.effectName()}, before: {baseTool}")
    end("T3")
    # T4: the theme
    begin("T4")
    yield from appMenu(g, ["Appearance", "Light"], "≡ > Appearance > Light")
    yield 800
    g.shot("T4-light")
    yield from appMenu(g, ["Appearance", "Dark"], "≡ > Appearance > Dark")
    yield 800
    end("T4")
    # T5: the Python console and the error log
    begin("T5")
    for entry, dockName in (("Python console", "PythonConsoleDockWidget"), ("Error log", "ErrorLogDockWidget")):
        yield from appMenu(g, [entry], f"≡ > {entry}")
        yield 600
        dock = g.mainWindow.findChild("QDockWidget", dockName)
        if entry == "Python console":
            g.shot("T5-console")
        closeButton = dock.findChild("QAbstractButton", "qt_dockwidget_closebutton") if dock is not None else None
        if closeButton is not None and closeButton.isVisible():
            yield from g.click(closeButton, f"Close the {entry.lower()}")
        elif dock is not None and dock.isVisible():
            yield from appMenu(g, [entry], f"≡ > {entry} again to close it")
        yield 300
    end("T5")
    # T6: help for the current tool
    begin("T6")
    yield from g.click(g.widget("StudioPanelHelpButton", "QToolButton"), "Click ? for help on the tool")
    yield 700
    g.shot("T6-help")
    helpOpen = qt.QApplication.activePopupWidget() is not None
    yield from g.key("Esc", "Esc closes the help")
    yield 300
    g.note("T6-helpPopup", helpOpen)
    end("T6")
    # T9: undo and redo
    begin("T9")
    voxelsNow = g.voxelCount()
    yield from g.click(g.widget("StudioUndoButton", "QToolButton"), "Click Undo")
    yield 300
    afterUndo = g.voxelCount()
    yield from g.click(g.widget("StudioRedoButton", "QToolButton"), "Click Redo")
    yield 300
    afterRedo = g.voxelCount()
    yield from g.key("Ctrl+Z", "Press Ctrl+Z")
    yield 300
    afterKeyUndo = g.voxelCount()
    yield from g.key("Ctrl+Y", "Press Ctrl+Y")
    yield 300
    afterKeyRedo = g.voxelCount()
    g.note("T9", {"now": voxelsNow, "undo": afterUndo, "redo": afterRedo, "keyUndo": afterKeyUndo, "keyRedo": afterKeyRedo})
    g.check("effect-after-return", g.effectName() == baseTool, f"tool after undo and redo: {g.effectName()}, last tool: {baseTool}")
    end("T9")
    scored = sum(taskCounts[name] for name in ("T1", "T2", "T3", "T4", "T5", "T6", "T9"))
    g.metric("scored", scored)
    g.notes["parts"] = dict(taskCounts)
    # T7: what the search offers for seven things a person looks for (exploration; not scored). A person stops typing as soon as
    # the list shows what they were after; with a palette (tier and category rows) the rows are read first and at most
    # T7_PALETTE_CHARACTERS characters are typed, so the cost is the quality of the palette, not the length of the query.
    begin("T7")
    found = {}
    for query, wanted in T7_QUERIES:
        top = yield from openSearch(g)
        field = searchField(top)
        palette = planned(g, "palette") is not None
        limit = T7_PALETTE_CHARACTERS if palette else len(query)
        typed = 0

        if field is not None:
            while not searchShows(g, wanted) and typed < limit:
                yield from g.type(None, query[typed], f"Type {query[typed]!r} of {query!r} in the search", perCharacter=True, settle=100)
                typed += 1
                yield 150
        yield 200
        rows, _current = searchResults(searchPopup(g), limit=T7_ROWS_READ)
        found[query] = {"found": searchShows(g, wanted), "typed": typed, "rows": rows}
        g.note(f"T7:{query}", rows)
        yield from g.key("Esc", "Esc closes the search")
        yield 300
    g.note("T7-found", found)
    g.metric("t7Found", sum(1 for entry in found.values() if entry["found"]))
    end("T7")
    # T10: Classic and back to Studio
    begin("T10")
    if g.config.get("fast"):
        g.skip("classic-studio-draw", "fast mode: the Classic switch is installed at startup, not under --testing")
    else:
        yield from appMenu(g, ["Classic interface"], "≡ > Classic interface")
        yield 1200
        classicShown = g.shell() is None
        g.shot("T10-classic")
        if classicShown:
            yield from clickMenuBarItem(g, "View", "Open the View menu")
            popup = qt.QApplication.activePopupWidget()
            if popup is not None:
                yield from g.clickMenuItem(popup, "Studio interface", "Choose View > Studio interface")
            yield 1500
        g.check("classic-studio-draw", g.currentWorkspace() == "draw", f"workspace after Classic -> Studio: {g.currentWorkspace()}")
    end("T10")
    g.metric("total", g.actions)
    g.shot("end")


def findTools(g):
    yield from findToolsBody(g, warm=False)


def findToolsWarm(g):
    yield from findToolsBody(g, warm=True)


#
# Task: classic-migration (persona A: a long-time Slicer user; persona B: keyboard only)
#


def focusStops(g, presses, area=""):
    """Press Tab ``presses`` times. Return ``[(widget text, visible cue?, inspector visible)]`` per stop (counts 1 action per press)."""
    stops = []
    dock = g.dock()
    for index in range(presses):
        visibleBefore = dock.isVisible()
        yield from g.key("Tab", f"Press Tab ({area} stop {index + 1})")
        yield 120
        widget = qt.QApplication.focusWidget()
        _point, reason = g.clickability(widget) if widget is not None else (None, "no focus")
        stops.append({"widget": widgetText(widget), "visibleCue": reason is None, "reason": reason, "inspectorToggled": dock.isVisible() != visibleBefore})
    return stops


def keysToDicomMenu(g):
    """Persona B: keys to reach ≡ > File > Add DICOM Data. None if unreachable. The keys that did nothing are listed in the notes, not counted."""
    attempts = (["F10"], ["Alt+F"])
    for first in attempts:
        yield from g.key(first[0], f"Press {first[0]} to reach the menus", settle=300)
        popup = qt.QApplication.activePopupWidget()
        if popup is not None and popup.inherits("QMenu"):
            count = 1
            # After F10 the menu bar is active: F opens File, then D chooses Add DICOM Data (mnemonics, as in official Slicer).
            # Alt+F opens File itself, so only D is left.
            for key in ("D",) if first[0] == "Alt+F" else ("F", "D"):
                yield from g.key(key, f"Press {key}", settle=300)
                count += 1
                popup = qt.QApplication.activePopupWidget()
            shell = g.shell()
            reached = qt.QApplication.activeModalWidget() is not None or (shell is not None and shell.currentModuleName() == "DICOM") or g.mainWindow.moduleSelector().selectedModule == "DICOM"
            g.closePopups()
            return count if reached else None
        g.note("keyAttempt:" + first[0], "no menu opened")
    return None


def classic(g):
    """Persona B (keyboard only: Tab stops and the menu), then persona A: the familiar tour and the productive core."""
    yield from ensureStudio(g)
    resetState(g)
    yield 500
    # ---------------- persona B (probes: not part of the tour) ----------------
    start = g.actions
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    keys = yield from keysToDicomMenu(g)
    g.metric("keysToDicomMenu", keys)
    g.closePopups()
    stops = yield from focusStops(g, 12, "Library")
    invisible = [stop for stop in stops if not stop["visibleCue"]]
    g.metric("invisibleTabStops", len(invisible))
    g.note("invisibleStops", [(stop["widget"], stop["reason"]) for stop in invisible][:12])
    g.metric("tabToggles", sum(1 for stop in stops if stop["inspectorToggled"]))
    g.check("tab-never-toggles-inspector", not any(stop["inspectorToggled"] for stop in stops), f"{sum(1 for stop in stops if stop['inspectorToggled'])} of {len(stops)} Tab presses on Library changed the inspector")
    g.check("no-invisible-focus-stop", not invisible, f"{len(invisible)} of {len(stops)} Tab stops on Library had no visible target: " + "; ".join(stop["widget"] for stop in invisible[:5]))
    g.check("menu-keyboard-reachable", keys is not None and keys <= 5, f"keys to ≡ > File > Add DICOM Data: {keys}")
    g.note("personaBActions", g.actions - start)
    g.shot("persona-b-library")
    # ---------------- persona A: the familiar tour ----------------
    tourStart = g.actions
    yield from g.click(g.widget("StudioAppMenuButton", "QToolButton"), "Look for File, Edit, View, Help: open the ≡ menu")
    yield from g.until(lambda: qt.QApplication.activePopupWidget() is not None, 2000)
    menu = qt.QApplication.activePopupWidget()
    if menu is not None:
        yield from g.clickMenuItem(menu, "File", "Open the File submenu")
        yield 300
        g.shot("tour-file-menu")
    g.closePopups()
    typed = yield from searchAndOpen(g, "Data", "data")
    if typed is not None:
        yield from waitTransient(g, "Data")
        g.shot("tour-data-module")
    yield from g.click(g.widget("StudioDrawWorkspaceButton", "QToolButton"), "Click Draw (the Segment Editor), with no data loaded")
    yield 800
    yield from g.click(g.widget("StudioLayoutButton", "QToolButton"), "Open the layout picker")
    yield from g.until(lambda: g.widget("StudioMoreLayoutsButton", "QPushButton") is not None, 2000)
    yield from g.click(g.widget("StudioMoreLayoutsButton", "QPushButton"), "Click More layouts…")
    yield 400
    g.shot("tour-more-layouts")
    g.closePopups()
    yield from g.key("Ctrl+S", "Press Ctrl+S to save the scene")
    yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None, 3000)
    g.shot("tour-save-dialog")
    cancel = activeDialogButton(g, "cancel", "close")
    yield from g.click(cancel, "Cancel the save dialog") if cancel is not None else iter(())
    yield 300
    # Toolbars: Markups from View > Toolbars must stay visible (find:NAV-03)
    yield from appMenu(g, ["View", "Toolbars", "Markups"], "≡ > View > Toolbars > Markups")
    yield 800
    toolbar = g.mainWindow.findChild("QToolBar", "MarkupsToolBar")
    shownNow = toolbar is not None and toolbar.isVisible()
    yield 1500
    shownLater = toolbar is not None and toolbar.isVisible()
    g.check("markups-toolbar-stays", shownNow and shownLater, f"Markups toolbar visible right after: {shownNow}, 1.5 s later: {shownLater}")
    g.shot("tour-markups-toolbar")
    yield from g.click(g.widget("StudioPanelMoreButton", "QToolButton"), "Open the panel menu (⋯)")
    yield from g.until(lambda: qt.QApplication.activePopupWidget() is not None, 2000)
    menu = qt.QApplication.activePopupWidget()
    if menu is not None:
        yield from g.clickMenuItem(menu, "Show Data Probe", "Show Data Probe")
    yield 300
    yield from g.key("Ctrl+H", "Press Ctrl+H (Home)")
    yield 400
    yield from g.key("Ctrl+Alt+0", "Switch to the Classic interface")
    yield 1200
    g.shot("tour-classic")
    edgeClassicAbsent = edgeLettersShown(g, "Red") is None
    g.check("edge-letters-absent-classic", edgeClassicAbsent, "edge letters in the Classic interface" if not edgeClassicAbsent else "none in Classic")
    yield from g.key("Ctrl+Alt+0", "Switch back to the Studio interface")
    yield 1500
    yield from g.key("Ctrl+/", "Press Ctrl+/ for the keyboard shortcuts")
    yield 400
    g.closePopups()
    g.metric("tour", g.actions - tourStart)
    # ---------------- persona A: the productive core (the offline template is the sample) ----------------
    coreStart = g.actions
    yield from g.key("Ctrl+Alt+1", "Go to Library")
    yield 500
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Click 'Start on MNI template'")
    yield from waitForDraw(g)
    banner = g.widget("StudioDownloadBannerSecondaryButton", "QPushButton")
    if banner is not None:
        # The banner is not part of the core (it can be ignored); it is not clicked
        g.note("bannerShownOnCore", True)
    yield from armBrush(g)
    stroke = [sliceViewPoint(g, "Red", (-10.0 + i, -18.0 + (i % 3), sliceOffset(g))) for i in range(0, 12, 2)]
    yield from g.drag(g.sliceView("Red"), stroke, "Paint a stroke")
    yield 200
    savePath = os.path.join(g.out, "save", "classic_lesion.nii.gz")
    os.makedirs(os.path.dirname(savePath), exist_ok=True)
    if os.path.exists(savePath):
        os.remove(savePath)
    yield from g.click(g.widget("StudioSaveLesionButton", "QPushButton"), "Click 'Save lesion…'")
    yield from saveFileDialogPath(g, savePath)
    yield from g.until(lambda: os.path.isfile(savePath), 6000, what="the lesion file")
    g.metric("core", g.actions - coreStart)
    g.shot("core-saved")
    g.note("tourActions", g.metrics.get("tour"))


#
# Figures (case-report tracing): drop, arrange, answer the convention question, trace what is seen
#


def chooseFromCombo(g, combo, text, description=""):
    """Open a combo box (1 action) and click the row that starts with ``text`` (1 action). ``combo`` may be a qMRMLNodeComboBox."""
    inner = combo.findChild("QComboBox") if combo is not None and not combo.inherits("QComboBox") else combo
    if inner is None:
        return g.refuse("combo", description or text, "no combo box", widgetText(combo))
    yield from g.click(inner, description or f"Open the list to choose {text}")
    yield from g.until(lambda: qt.QApplication.activePopupWidget() is not None, 2000)
    popup = qt.QApplication.activePopupWidget()
    views = popup.findChildren("QAbstractItemView") if popup is not None else []
    if not views:
        return g.refuse("combo", description or text, "the list did not open", text)
    view = views[0]
    model = view.model()
    wanted = text.lower()
    labels = []

    def walk(parent):
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            label = str(index.data() or "")
            labels.append(label)
            if label.lower().startswith(wanted):
                return index
            found = walk(index)
            if found is not None:
                return found
        return None

    index = walk(qt.QModelIndex())
    if index is not None:
        view.scrollTo(index)
        yield 120
        rect = view.visualRect(index)
        point = view.viewport().mapToGlobal(qt.QPoint(rect.center().x(), rect.center().y()))
        hit = qt.QApplication.widgetAt(point)
        if hit is None or not isAncestorOrSelf(popup, hit):
            g.closePopups()
            return g.refuse("combo", description or text, f"row {labels[-1]!r} covered by {widgetText(hit)}", text)
        g.backend.click((point.x(), point.y()))
        g.addStep("combo", f"Choose {labels[-1]!r}", 1, widgetText(inner))
        yield 300
        return True
    g.closePopups()
    return g.refuse("combo", description or text, f"no such row among {labels}", text)


def sliceBarWidget(g, sliceName, objectName, className="QWidget"):
    """A widget of the view bar (controller) of a slice view, or None."""
    controller = g.sliceWidget(sliceName).sliceController()
    if controller is None:
        return None
    for candidate in controller.findChildren(className):
        if candidate.objectName == objectName:
            return candidate
    return None


def sliceBackgroundName(sliceName):
    node = slicer.app.layoutManager().sliceWidget(sliceName).sliceLogic().GetBackgroundLayer().GetVolumeNode()
    return node.GetName() if node is not None else None


def sliceForegroundName(sliceName):
    node = slicer.app.layoutManager().sliceWidget(sliceName).sliceLogic().GetForegroundLayer().GetVolumeNode()
    return node.GetName() if node is not None else None


def templateVolumeName():
    for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
        if node.GetName().startswith("MNI152"):
            return node.GetName()
    return None


def figureNodeName(path):
    return os.path.splitext(os.path.basename(path))[0]


def dropFigure(g, path):
    """Drop a figure PNG on the window; the classic Add Data dialog asks for OK (1 action)."""
    yield from g.drop([path], f"Drop {os.path.basename(path)} on the window")
    yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None or slicer.util.getNode(figureNodeName(path)) is not None, 6000, what="figure dialog")
    modal = qt.QApplication.activeModalWidget()
    if modal is not None:
        g.shot("figure-dialog")
        yield from g.click(activeDialogButton(g, "ok"), "Click OK in the Add Data dialog")
        yield from g.until(lambda: qt.QApplication.activeModalWidget() is None, 6000)
    yield from g.until(lambda: any(node.GetName() == figureNodeName(path) for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 8000, what="figure volume")
    yield 800


def arrangeFigureViews(g, path):
    """Figure in Yellow (axial, fitted), template in Red and Green. Skipped when the build routes the figure itself."""
    figure, template = figureNodeName(path), templateVolumeName()
    if sliceBackgroundName("Yellow") == figure and sliceBackgroundName("Red") == template:
        g.note("figureRouted", True)
        return True
    g.note("figureRouted", False)
    for name in ("Red", "Green"):
        if sliceBackgroundName(name) == template:
            continue
        pin = sliceBarWidget(g, name, "PinButton", "QToolButton")
        yield from g.hover(pin, f"Hover the pin of the {name} bar")
        yield from g.until(lambda name=name: sliceBarWidget(g, name, "BackgroundComboBox") is not None and sliceBarWidget(g, name, "BackgroundComboBox").isVisible(), 2500)
        yield from chooseFromCombo(g, sliceBarWidget(g, name, "BackgroundComboBox"), template, f"Show the template in the {name} view")
        yield from g.click(sliceBarWidget(g, name, "FitToWindowToolButton", "QToolButton"), f"Fit the template to the {name} view")
        yield 300
    # Yellow: axial, fitted
    pin = sliceBarWidget(g, "Yellow", "PinButton", "QToolButton")
    yield from g.hover(pin, "Hover the pin of the Yellow bar")
    yield from g.until(lambda: sliceBarWidget(g, "Yellow", "SliceOrientationSelector") is not None and sliceBarWidget(g, "Yellow", "SliceOrientationSelector").isVisible(), 2500)
    selector = sliceBarWidget(g, "Yellow", "SliceOrientationSelector")
    yield from chooseFromCombo(g, selector, "Axial", "Set the Yellow view to axial")
    fit = sliceBarWidget(g, "Yellow", "FitToWindowToolButton", "QToolButton")
    yield from g.click(fit, "Fit the figure to the Yellow view")
    yield 400
    return sliceBackgroundName("Yellow") == figure


def answerConvention(g, neurological):
    """Answer the convention question of a figure when the build asks it: the patient's right on the Left/Right of the image."""
    chip = planned(g, "conventionChip")
    if chip is None:
        # The notice line has no promised object name: it is the container of the buttons Left, Right and Not sure
        for button in g.mainWindow.findChildren("QAbstractButton"):
            if button.isVisible() and str(button.text).strip().lower() == "not sure" and button.parent() is not None:
                chip = button.parent()
                break
    if chip is None:
        g.note("conventionQuestion", False)
        return False
    g.note("conventionQuestion", True)
    wanted = "right" if neurological else "left"
    for button in chip.findChildren("QAbstractButton"):
        label = str(button.text).strip().lower()
        if button.isVisible() and label == wanted:
            yield from g.click(button, f"Answer: the patient's right is on the {wanted} of the figure")
            yield 500
            return True
    g.warn("the convention question has no answer button named " + wanted)
    return False


def grabRegion(g, widget):
    """The screen pixels of a widget as a numpy array (rows, columns, 3)."""
    import tempfile

    from PIL import Image

    point = widget.mapToGlobal(qt.QPoint(0, 0))
    screen = g.screenOf()
    pixmap = screen.grabWindow(0, point.x(), point.y(), widget.width, widget.height)
    path = os.path.join(tempfile.gettempdir(), f"gate-grab-{os.getpid()}.png")
    pixmap.save(path)
    return np.array(Image.open(path).convert("RGB"))


def brainBounds(pixels):
    """Bounding box (left, top, right, bottom) of the grey tissue in a screenshot (not black, not white, not coloured)."""
    red, green, blue = (pixels[..., i].astype(int) for i in range(3))
    grey = (np.abs(red - green) < 8) & (np.abs(green - blue) < 8) & (red > 40) & (red < 245)
    if not grey.any():
        return None
    # Ignore thin annotations (labels, rulers): keep the bounds of the densest block
    rowCounts, columnCounts = grey.sum(axis=1), grey.sum(axis=0)
    rowsDense = np.nonzero(rowCounts > 0.02 * grey.shape[1])[0]
    columnsDense = np.nonzero(columnCounts > 0.02 * grey.shape[0])[0]
    if len(rowsDense) == 0 or len(columnsDense) == 0:
        return None
    return int(columnsDense[0]), int(rowsDense[0]), int(columnsDense[-1]), int(rowsDense[-1])


def figureRingCentroid(pixels):
    """Centroid (column, row) of the yellow ring of the lesion in a screenshot of the figure view (the left-most one when the
    figure has several panels), or None.
    """
    red, green, blue = (pixels[..., i].astype(int) for i in range(3))
    yellow = (red > 200) & (green > 200) & (blue < 110)
    rows, columns = np.nonzero(yellow)
    if len(rows) < 12:
        return None
    order = np.argsort(columns)
    columns, rows = columns[order], rows[order]
    gaps = np.diff(columns)
    if len(gaps) and gaps.max() > 25:
        split = int(np.argmax(gaps)) + 1
        columns, rows = columns[:split], rows[:split]
    return float(columns.mean()), float(rows.mean())


def sheetBounds(pixels):
    """Bounding box (left, top, right, bottom) of the white sheet of a figure in a screenshot of its view, or None."""
    white = (pixels[..., 0] >= 245) & (pixels[..., 1] >= 245) & (pixels[..., 2] >= 245)
    rowsDense = np.nonzero(white.sum(axis=1) > 0.2 * white.shape[1])[0]
    columnsDense = np.nonzero(white.sum(axis=0) > 0.2 * white.shape[0])[0]
    if len(rowsDense) == 0 or len(columnsDense) == 0:
        return None
    return int(columnsDense[0]), int(rowsDense[0]), int(columnsDense[-1]), int(rowsDense[-1])


def volumeRectInView(g, sliceName, volumeNode):
    """The rectangle (left, top, right, bottom), in pixels of the slice view, that a volume covers on the displayed slice."""
    bounds = [0.0] * 6
    volumeNode.GetRASBounds(bounds)
    offset = sliceOffset(g, sliceName)
    first = sliceViewPoint(g, sliceName, (bounds[0], bounds[2], offset))
    second = sliceViewPoint(g, sliceName, (bounds[1], bounds[3], offset))
    return min(first[0], second[0]), min(first[1], second[1]), max(first[0], second[0]), max(first[1], second[1])


def traceWhatIsSeen(g, figureView, drawSlice, facts, strokes=2, description="Trace the lesion"):
    """Paint on the template view at the place that matches where the lesion is in the figure view, by eye: the same fraction across
    the panel as across the template slice (what a person does, without knowing the convention of the figure). Return the lesion
    centre in RAS, or None.
    """
    figurePixels = grabRegion(g, figureView)
    centroid = figureRingCentroid(figurePixels)
    sheet = sheetBounds(figurePixels)
    if centroid is None or sheet is None:
        g.warn("the lesion ring or the figure was not found in the figure view")
        return None
    scale = (sheet[2] - sheet[0] + 1) / float(facts["size"][0])
    panel = None
    for rect in facts["panelRects"]:
        left, top = sheet[0] + rect[0] * scale, sheet[1] + rect[1] * scale
        right, bottom = left + rect[2] * scale, top + rect[3] * scale
        if left <= centroid[0] <= right and top <= centroid[1] <= bottom:
            panel = (left, top, right, bottom)
    if panel is None:
        g.warn("the lesion ring is outside the panels of the figure")
        return None
    fx = (centroid[0] - panel[0]) / (panel[2] - panel[0])
    fy = (centroid[1] - panel[1]) / (panel[3] - panel[1])
    rect = volumeRectInView(g, "Red", slicer.util.getNode(templateVolumeName()))
    x = rect[0] + fx * (rect[2] - rect[0])
    y = rect[1] + fy * (rect[3] - rect[1])
    g.note("traceTarget", {"figureFraction": [round(fx, 3), round(fy, 3)], "templatePixel": [round(x), round(y)]})
    template = slicer.util.getNode(templateVolumeName())
    before = lesionArray(g, template).copy()
    for index in range(strokes):
        points = []
        for step in range(7):
            points.append((int(x - 7 + 14 * step / 6), int(y + (-5 if index == 0 else 5) + (3 if step % 2 else -3))))
        yield from g.drag(drawSlice, points, f"{description}: stroke {index + 1}")
    yield 300
    return newVoxelsCenterRAS(before, lesionArray(g, template), template)


def newVoxelsCenterRAS(before, after, volumeNode):
    """Centre in RAS (mm) of the voxels that are in ``after`` and not in ``before`` (arrays on the grid of ``volumeNode``), or None."""
    added = np.argwhere((after > 0) & (before == 0))
    if len(added) == 0:
        return None
    ijk = added[:, ::-1].mean(axis=0)
    matrix = vtk.vtkMatrix4x4()
    volumeNode.GetIJKToRASMatrix(matrix)
    point = matrix.MultiplyPoint([float(ijk[0]), float(ijk[1]), float(ijk[2]), 1.0])
    return [point[0], point[1], point[2]]


def segmentCenterRAS(node, segmentId):
    """Centre of the voxels of a segment in RAS (mm), or None if the segment is empty (the output list is filled in place)."""
    if node is None or not segmentId:
        return None
    segment = node.GetSegmentation().GetSegment(segmentId)
    if segment is None:
        return None
    center = [0.0, 0.0, 0.0]
    node.GetSegmentCenterRAS(segmentId, center)
    if center == [0.0, 0.0, 0.0]:
        array = slicer.util.arrayFromSegmentBinaryLabelmap(node, segmentId)
        if not np.count_nonzero(array):
            return None
    return center


def saveLesionTo(g, name):
    path = os.path.join(g.out, "save", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    yield from g.click(g.widget("StudioSaveLesionButton", "QPushButton"), "Click 'Save lesion…'")
    yield from saveFileDialogPath(g, path)
    yield from g.until(lambda: os.path.isfile(path), 6000, what=name)
    return path


def recordCaseMetadata(g, text, description):
    """Record DOI, figure, symptom and age: the Notes field when the build has one, else a Texts node."""
    field = planned(g, "notesField")
    if field is not None:
        edit = field if field.inherits("QTextEdit") or field.inherits("QLineEdit") or field.inherits("QPlainTextEdit") else (field.findChild("QTextEdit") or field.findChild("QLineEdit"))
        yield from g.type(edit, text, description)
        return True
    typed = yield from searchAndOpen(g, "Texts", "text")
    if typed is None:
        return False
    yield from waitTransient(g, "Texts")
    selector = g.widget("TextNodeSelector", "qMRMLNodeComboBox")
    yield from chooseFromCombo(g, selector, "Create new", "Create a new Text node")
    yield 400
    edit = g.widget("TextEdit", "QTextEdit")
    if edit is not None and not edit.isEnabled():
        yield from g.click(g.widget("EditButton", "QPushButton"), "Click Edit")
    yield from g.type(g.widget("TextEdit", "QTextEdit"), text, description)
    yield 300
    yield from closeTransient(g, "Close Texts and return to Draw")
    return True


def newLesion(g):
    """New lesion for the next case: its button when the build has one; else Library > Start on the template again (the lesion
    carries over in this build; recorded).
    """
    button = g.widget("StudioNewLesionButton", "QPushButton") or g.buttonWithText("New lesion")
    if button is not None:
        yield from g.click(button, "Click New lesion")
        yield 600
        return True
    yield from g.click(g.widget("StudioLibraryWorkspaceButton", "QToolButton"), "Click Library")
    yield 500
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Click 'Start on MNI template'")
    yield from waitForDraw(g)
    g.note("newLesionVia", "start-on-template")
    return True


def matchSlice(g, view, target, description="Find the slice that matches the figure"):
    """Scroll the template to the figure's slice like a person: coarse bursts first, then single steps, comparing the two views
    (1 action per burst). The comparison itself is reading (0).
    """
    logic = g.sliceWidget("Red").sliceLogic()
    spacing = logic.GetLowestVolumeSliceSpacing()[2] or 1.0
    sign = scrollSign(g, "Red")
    guard = 0
    while abs(logic.GetSliceOffset() - target) > spacing * 0.6 and guard < 8:
        remaining = (target - logic.GetSliceOffset()) / spacing
        notches = int(remaining) if abs(remaining) >= 3 else (1 if remaining > 0 else -1)
        notches = max(-4, min(4, notches)) if abs(remaining) >= 3 else notches
        yield from g.wheel(view, notches * sign, f"{description}: scroll {notches:+d}")
        yield 150
        guard += 1
    return abs(logic.GetSliceOffset() - target) <= spacing * 0.6


def caseBody(g, caseKey, path, firstCase):
    """One case: bring the figure in, arrange, answer the convention question, match the slice, trace, record the metadata, save."""
    facts = g.fixtures[caseKey]
    figure = figureNodeName(path)
    if firstCase:
        yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Click 'Start on MNI template'")
        yield from waitForDraw(g)
        scrollSign(g, "Red")
        banner = g.widget("StudioDownloadBannerSecondaryButton", "QPushButton")
        if banner is not None:
            yield from g.click(banner, "Dismiss the 1 mm template banner")
    else:
        yield from newLesion(g)
    yield from dropFigure(g, path)
    yield from arrangeFigureViews(g, path)
    yield from answerConvention(g, facts["neurological"])
    g.shot(f"{caseKey}-figure-arranged")
    zTarget = facts["lesionMniRAS"][2]
    template = templateVolumeName()
    red = g.sliceView("Red")
    yield from matchSlice(g, red, zTarget)
    editorBefore = g.voxelCount() or 0
    yield from armBrush(g) if g.effectName() is None else iter(())
    yellow = g.sliceView("Yellow")
    center = yield from traceWhatIsSeen(g, yellow, red, facts, strokes=2, description=f"Trace the lesion of {caseKey}")
    g.shot(f"{caseKey}-traced")
    g.note(f"{caseKey}-lesionCenterRAS", center)
    if center is not None:
        expectedSign = -1 if facts["patientSide"] == "left" else 1
        ok = (center[0] * expectedSign) > 0
        g.check("wrong-side", ok, f"{caseKey}: the figure's lesion is on the patient's {facts['patientSide']}; the traced lesion centre is x = {center[0]:.1f} mm")
    else:
        g.check("wrong-side", False, f"{caseKey}: nothing was traced")
    return center


def figureGuards(g, path):
    """State assertions about a figure shown in Yellow (probes: their input is not counted): edge letters, a Paint stroke over the
    figure, and Link views. Skipped when the figure is not shown in Yellow.
    """
    figure = figureNodeName(path)
    if sliceBackgroundName("Yellow") != figure:
        g.skip("figure-paint-guard", "the figure is not shown in the Yellow view")
        g.skip("figure-link-offset", "the figure is not shown in the Yellow view")
        g.skip("edge-letters-absent-figure", "the figure is not shown in the Yellow view")
        return
    letters = edgeLettersShown(g, "Yellow")
    g.check("edge-letters-absent-figure", letters is None or not any(letters), f"edge letters on the figure view: {letters}")
    template = slicer.util.getNode(templateVolumeName())
    if g.effectName() != "Paint":
        g.segmentEditor().setActiveEffectByName("Paint")
        yield 300
    before = np.count_nonzero(lesionArray(g, template))
    view = g.sliceView("Yellow")
    # Where the figure (a picture at z = 0 of template space) overlaps the template
    points = [sliceViewPoint(g, "Yellow", (-60.0 + dx, -60.0 + dy, 0.0)) for dx, dy in ((-10, -10), (0, -5), (10, 0), (10, 10))]
    yield from g.drag(view, points, "Probe: a Paint stroke over the figure", setup=True)
    yield 300
    after = np.count_nonzero(lesionArray(g, template))
    g.check("figure-paint-guard", after == before, f"a stroke over the figure changed the lesion by {after - before} voxels")
    if after != before:
        g.segmentEditor().undo()
        yield 300
    # Link views: Yellow must stay on the figure (offset 0) when Red is scrolled
    for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        node.SetLinkedControl(True)
    yield 200
    yield from g.wheel(g.sliceView("Red"), 3 * scrollSign(g, "Red"), "Probe: scroll the Red view with Link views on", setup=True)
    yield 400
    g.check("figure-link-offset", abs(sliceOffset(g, "Yellow")) < 1e-3, f"Yellow offset with the views linked: {sliceOffset(g, 'Yellow'):.2f} mm")
    for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        node.SetLinkedControl(False)


def case(g):
    """Two case reports: template, figure drop and arrangement, convention question, slice match, trace, metadata, save, next case."""
    yield from ensureStudio(g)
    resetState(g)
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    start = g.actions
    figures = g.fixtures
    for index, (key, name) in enumerate((("case1", "case1_fig2.png"), ("case2", "case2_fig1_neuro.png"))):
        path = os.path.join(figures["directory"], name)
        yield from caseBody(g, key, path, firstCase=index == 0)
        yield from recordCaseMetadata(g, f"DOI 10.1000/gate.{index + 1}; {name}; symptom: aphasia; age: 6{index}", f"Record DOI, figure, symptom and age ({key})")
        saved = yield from saveLesionTo(g, f"{key}_lesion.nii.gz")
        g.note(f"{key}-saved", saved)
        g.metric(f"{key}", g.actions - start)
        start = g.actions
    g.metric("total", sum(g.metrics.get(key, 0) for key in ("case1", "case2")))
    yield from figureGuards(g, os.path.join(figures["directory"], "case2_fig1_neuro.png"))
    g.shot("end")


#
# Task: lnm-cohort
#


def selectTreeRow(g, view, text, description=""):
    """Click the row of a tree or list view whose text starts with ``text`` (1 action)."""
    model = view.model()
    wanted = text.lower()
    found = []

    def walk(parent):
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            label = str(index.data() or "")
            found.append(label)
            if label.lower().startswith(wanted):
                return index
            child = walk(index)
            if child is not None:
                return child
        return None

    index = walk(qt.QModelIndex())
    if index is None:
        return g.refuse("row", description or text, f"no such row among {found[:12]}", text)
    view.scrollTo(index)
    yield 100
    rect = view.visualRect(index)
    point = view.viewport().mapToGlobal(qt.QPoint(rect.center().x(), rect.center().y()))
    hit = qt.QApplication.widgetAt(point)
    if hit is None or not isAncestorOrSelf(view, hit):
        return g.refuse("row", description or text, f"covered by {widgetText(hit)}", text)
    g.focus()
    g.backend.click((point.x(), point.y()))
    g.addStep("row", description or f"click the row {text}", 1, widgetText(view))
    yield 300
    return True


def openFolderThroughDialog(g, folder):
    """Click 'Open folder…', type the path and Choose (3 actions)."""
    yield from g.click(g.widget("StudioLibraryOpenFolderButton", "QPushButton"), "Click 'Open folder…'")
    yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None, 4000, what="folder dialog")
    ok = yield from saveFileDialogPath(g, folder, "Type the folder path, then Choose", folder=True)
    return ok


def openFileThroughDialog(g, path):
    """Click 'Open…' in Library, type the path and Open (3 actions)."""
    yield from g.click(g.widget("StudioLibraryOpenButton", "QPushButton"), "Click 'Open…'")
    yield from g.until(lambda: qt.QApplication.activeModalWidget() is not None, 4000, what="open dialog")
    ok = yield from saveFileDialogPath(g, path, "Type the file path, then Open")
    return ok


def lnm(g):
    """Cohort of lesion masks: Load all, one-subject QC, the next subject, a thresholded T map; then the study path (B)."""
    yield from ensureStudio(g)
    resetState(g)
    yield from g.until(lambda: g.widget("StudioLibraryOpenFolderButton", "QPushButton") is not None, 8000)
    cohort = g.fixtures["cohort"]["folder"]
    start = g.actions
    # Folder, Load all
    yield from openFolderThroughDialog(g, cohort)
    yield from g.until(lambda: g.widget("StudioFolderCardLoadAllButton", "QPushButton") is not None, 5000, what="folder card")
    g.shot("folder-card")
    yield from g.click(g.widget("StudioFolderCardLoadAllButton", "QPushButton"), "Click 'Load all'")
    yield from g.until(lambda: len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")) >= 6, 20000, what="the six lesions")
    yield 800
    review = planned(g, "loadedSet")
    g.note("loadedSet", review is not None)
    if g.currentWorkspace() != "draw":
        yield from g.click(g.widget("StudioDrawWorkspaceButton", "QToolButton"), "Review in Draw (the Draw tab)")
        yield from waitForDraw(g)
    g.shot("draw-after-load-all")
    g.note("loadedSegmentations", len(slicer.util.getNodesByClass("vtkMRMLSegmentationNode")))
    # One-subject QC: Segment Statistics for the lesion that Draw shows
    typed = yield from searchAndOpen(g, "Segment Statistics", "segm")
    if typed is not None:
        yield from waitTransient(g, "SegmentStatistics")
        selector = g.widget("segmentationSelector", "qMRMLNodeComboBox") or g.widget("SegmentationSelector", "qMRMLNodeComboBox")
        if selector is not None:
            current = selector.currentNode()
            g.note("qcSegmentationPreselected", current is not None)
            if current is None:
                yield from chooseFromCombo(g, selector, "sub-01", "Choose the subject's lesion")
        yield from g.click(g.buttonWithText("Apply"), "Apply the statistics")
        yield 1500
        g.shot("qc")
        yield from closeTransient(g, "Close Segment Statistics and return to Draw")
    g.metric("qc", g.actions - start)
    # The next subject
    if review is not None:
        yield from g.key("Alt+Right", "Alt+→ to the next subject")
        yield 600
    else:
        editorSelector = g.widget("SegmentationNodeComboBox", "qMRMLNodeComboBox")
        yield from chooseFromCombo(g, editorSelector, "sub-02", "Pick the next subject in the Segmentation list")
        yield 600
    g.metric("setReview", g.actions - start)
    # The T map: open it, show it over the template with its threshold
    yield from g.click(g.widget("StudioLibraryWorkspaceButton", "QToolButton"), "Click Library")
    yield 600
    tmap = g.fixtures["tmap"]["path"]
    yield from openFileThroughDialog(g, tmap)
    yield from g.until(lambda: any(node.GetName().startswith("sub-05_desc-T") for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 10000, what="the T map")
    yield from g.until(lambda: g.currentWorkspace() == "draw", 4000)
    yield 800
    g.shot("tmap-opened")
    tmapNode = next((node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if node.GetName().startswith("sub-05_desc-T")), None)
    shown = tmapNode is not None and any(
        sliceBackgroundName(name) == tmapNode.GetName() or sliceForegroundName(name) == tmapNode.GetName() for name in ("Red", "Green", "Yellow"))
    g.note("tmapShown", shown)
    typed = yield from searchAndOpen(g, "Volumes", "vol")
    if typed is not None:
        yield from waitTransient(g, "Volumes")
        tree = g.widget("ActiveVolumeNodeSelector", "qMRMLSubjectHierarchyTreeView")
        yield from selectTreeRow(g, tree, "sub-05_desc-T", "Select the T map in Volumes")
        yield from chooseFromCombo(g, g.widget("ColorTableComboBox", "qMRMLColorTableComboBox"), "ColdToHotRainbow", "Choose the ColdToHotRainbow table")
        threshold = g.widget("MRMLVolumeThresholdWidget", "qMRMLVolumeThresholdWidget")
        modeCombo = None
        if threshold is not None:
            modeCombo = next((c for c in threshold.findChildren("QComboBox") if c.objectName == "AutoManualComboBox"), None)
        if modeCombo is not None:
            yield from chooseFromCombo(g, modeCombo, "Manual", "Set the threshold to Manual")
            minimum = next((c for c in threshold.findChildren("QLineEdit") if c.isVisible()), None)
            yield from g.type(minimum, str(g.fixtures["tmap"]["threshold"]), "Type the threshold", selectAll=True, enter=True)
        yield 500
        g.shot("tmap-threshold")
        yield from closeTransient(g, "Close Volumes and return to Draw")
    g.metric("A", g.actions - start)
    yield from lnmStudyPath(g)
    g.shot("end")


#: The buttons of the study path (B), found by their text: create the study, map every lesion, group statistics, export
STUDY_STEPS = (
    ("map every lesion", r"map all|map \d+ lesions|run everything|map network"),
    ("group statistics", r"group|statistics|overlap|sensitivity"),
    ("export", r"export"),
)


def visibleButton(g, pattern):
    regex = re.compile(pattern, re.I)
    for button in g.mainWindow.findChildren("QAbstractButton"):
        if button.isVisible() and button.isEnabled() and regex.search(str(button.text).replace("&", "")):
            return button
    return None


def lnmStudyPath(g):
    """Study path (B): folder, study, maps of every lesion, group statistics, export, with no code and no terminal. Needs W3-07 (study),
    W3-08 (maps) and A-04: while the folder card says "Create study (coming)" the metric B is None (needs code) and the reason is
    recorded. When the button exists, the next buttons are found by their text (STUDY_STEPS); a missing one ends the path with None
    and names the step.
    """
    yield from g.click(g.widget("StudioLibraryWorkspaceButton", "QToolButton"), "Click Library")
    yield 600
    yield from openFolderThroughDialog(g, g.fixtures["cohort"]["folder"])
    yield from g.until(lambda: g.widget("StudioFolderCardCreateStudyButton", "QPushButton") is not None, 5000, what="folder card")
    start = g.actions
    button = g.widget("StudioFolderCardCreateStudyButton", "QPushButton")
    if button is None or not button.isEnabled():
        g.metric("B", None)
        g.note("studyPath", f"needs code: the folder card says {str(button.text) if button is not None else 'nothing'!r} (W3-07, W3-08 and A-04 are not in this build)")
        return
    yield from g.click(button, "Click 'Create study'")
    yield 1500
    for label, pattern in STUDY_STEPS:
        found = yield from g.until(lambda pattern=pattern: visibleButton(g, pattern) is not None, 8000, what=label)
        if not found:
            g.metric("B", None)
            g.note("studyPath", f"needs code: no button for the step '{label}' after {g.actions - start} actions")
            return
        yield from g.click(visibleButton(g, pattern), f"Click '{label}'")
        yield 2000
    g.metric("B", g.actions - start)
    g.note("studyPath", "UI only")


#
# Review scenarios
#


def wrongSide(g):
    """A neurological figure with an R marker: after the convention answer the traced lesion lands on the correct hemisphere."""
    yield from ensureStudio(g)
    resetState(g)
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    path = os.path.join(g.fixtures["directory"], "case2_fig1_neuro.png")
    center = yield from caseBody(g, "case2", path, firstCase=True)
    g.metric("actions", g.actions)
    g.note("lesionCenterRAS", center)
    g.shot("end")


def findText(widgets, pattern):
    regex = re.compile(pattern, re.I)
    for widget in widgets:
        try:
            text = widget.text if hasattr(widget, "text") and not callable(widget.text) else ""
        except Exception:
            text = ""
        if text and widget.isVisible() and regex.search(str(text)):
            return widget
    return None


def reopen(g):
    """Save a lesion, then open it alone and with its scan; a native-space lesion is never bound to the template."""
    yield from ensureStudio(g)
    resetState(g)
    scan = g.fixtures["patientScan"]
    blob = g.fixtures["patientBlobScannerRAS"]
    yield from g.drop([scan], "Drop the patient scan")
    yield from waitForDraw(g)
    scrollSign(g, "Red")
    yield from wheelToOffset(g, "Red", blob[2], "Wheel to the blob")
    points = [sliceViewPoint(g, "Red", (blob[0] + dx, blob[1] + dy, sliceOffset(g))) for dx, dy in ((-5, -5), (5, -5), (5, 5), (-5, 5), (-5, -3))]
    yield from g.drag(g.sliceView("Red"), points, "Paint a stroke over the blob")
    _segmentation, _source = lesionNodes(g)
    savedPath = yield from saveLesionTo(g, "sub-01_lesion.nii.gz")
    folder = os.path.dirname(savedPath)
    sidecars = [name for name in os.listdir(folder) if name.startswith("sub-01_lesion") and name.endswith(".json")]
    sidecarOk = False
    if sidecars:
        with open(os.path.join(folder, sidecars[0])) as stream:
            sidecar = json.load(stream)
        sourceValue = str(sidecar.get("Source", sidecar.get("source", "")))
        sidecarOk = bool(sourceValue) and not os.path.isabs(sourceValue)
    g.note("sidecar", {"files": sidecars, "relativeSource": sidecarOk})
    # Start over (environment): the saved file is the only thing that is left
    nodeNamesBefore = len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"))
    slicer.mrmlScene.Clear(0)
    yield 500
    g.shell().switchTo("library")
    yield 500
    # The template is open, then the saved native-space lesion is opened alone
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Click 'Start on MNI template'")
    yield from waitForDraw(g)
    template = templateVolumeName()
    yield from g.drop([savedPath], "Drop the saved lesion alone")
    yield 1500
    editor = g.segmentEditor()
    node = editor.segmentationNode()
    sourceNode = editor.sourceVolumeNode()
    boundToTemplate = sourceNode is not None and sourceNode.GetName() == template
    g.note("lesionAloneSource", sourceNode.GetName() if sourceNode is not None else None)
    g.shot("lesion-alone")
    offered = findText(g.mainWindow.findChildren("QLabel") + g.mainWindow.findChildren("QPushButton"), r"scan this lesion|open the scan|open scan")
    g.check("reopen-sidecar-offer", sidecarOk and offered is not None and not boundToTemplate,
            f"sidecar with a relative Source written: {sidecarOk}; the scan is offered: {offered is not None}; the native-space lesion is bound to the template: {boundToTemplate}")
    # With its scan
    yield from g.drop([scan, savedPath], "Drop the scan and its lesion together")
    yield 1800
    editor = g.segmentEditor()
    sourceNode = editor.sourceVolumeNode()
    g.note("withScanSource", sourceNode.GetName() if sourceNode is not None else None)
    segmentation = editor.segmentationNode()
    g.note("withScanLesionVoxels", g.voxelCount())
    savedVoxels = int((readNifti(savedPath)[0] > 0).sum())
    scanName = os.path.basename(scan).split(".")[0]
    sourceName = sourceNode.GetName() if sourceNode is not None else None
    g.check("reopen-with-scan", sourceName is not None and scanName in sourceName and g.voxelCount() == savedVoxels,
            f"source volume {sourceName} (the scan is {scanName}); lesion voxels {g.voxelCount()} (saved file: {savedVoxels})")
    g.shot("lesion-with-scan")
    g.metric("actions", g.actions)
    yield from nlin6SaveCheck(g)


def nlin6SaveCheck(g):
    """A 91x109x91 NLin6Asym lesion mask shown on the 2009c template is saved on its own grid (dims, sform, voxels)."""
    slicer.mrmlScene.Clear(0)
    yield 500
    g.shell().switchTo("library")
    yield 500
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Setup: Start on MNI template", setup=True)
    yield from waitForDraw(g)
    original = os.path.join(g.fixtures["cohort"]["folder"], "sub-01_lesion.nii.gz")
    yield from g.drop([original], "Drop an NLin6Asym lesion mask on the 2009c template")
    yield 1500
    path = yield from saveLesionTo(g, "nlin6_lesion.nii.gz")
    reference = readNifti(original)
    if not os.path.isfile(path):
        g.check("save-grid-nlin6-on-2009c", False, "no file was written")
        return
    saved = readNifti(path)
    problems = []
    if not sameGeometry(saved, reference):
        problems.append(f"grid {tuple(saved[1])} instead of {tuple(reference[1])}")
    elif int((saved[0] > 0).sum()) != int((reference[0] > 0).sum()) or not np.array_equal(saved[0] > 0, reference[0] > 0):
        problems.append("the voxels differ from the lesion mask")
    if "8-bit unsigned" not in saved[5]:
        problems.append(f"pixel type {saved[5]}")
    g.check("save-grid-nlin6-on-2009c", not problems, "; ".join(problems) or f"saved on {tuple(saved[1])} with the grid and voxels of the mask")
    # The saved file opens in official Slicer 5.13 with identical voxels
    yield from officialLoadCheck(g, path, reference)


# Source shared by the gate and the script of the official process: a digest of where (RAS, 0.01 mm) the voxels > 0 are
VOXEL_DIGEST_SOURCE = """
import hashlib
import numpy as np


def voxelPositionDigest(array, ijkToRas):
    matrix = np.asarray(ijkToRas, float)
    index = np.argwhere(array > 0)[:, ::-1].astype(float)
    points = np.round(index @ matrix[:, :3].T + matrix[:, 3], 2) + 0.0
    order = np.lexsort(points.T[::-1])
    return hashlib.sha1(np.ascontiguousarray(points[order]).tobytes()).hexdigest()


"""

OFFICIAL_SLICER = "/opt/slicer-dl/Slicer-5.13.0-2026-10-16-linux-amd64/Slicer"


def officialLoadCheck(g, path, reference):
    """Load ``path`` in official Slicer 5.13 (a subprocess, ``--disable-settings``): the voxels must equal ``reference``'s."""
    if not os.path.isfile(OFFICIAL_SLICER):
        g.skip("official-loads-saved", f"{OFFICIAL_SLICER} is not installed")
        return
    script = os.path.join(g.out, "official_check.py")
    resultPath = os.path.join(g.out, "official_check.json")
    if os.path.exists(resultPath):
        os.remove(resultPath)
    with open(script, "w") as stream:
        stream.write(
            VOXEL_DIGEST_SOURCE
            + "import json, slicer, vtk\n"
            f"node = slicer.util.loadVolume({path!r})\n"
            "array = slicer.util.arrayFromVolume(node)\n"
            "matrix = vtk.vtkMatrix4x4()\n"
            "node.GetIJKToRASMatrix(matrix)\n"
            "ijkToRas = [[matrix.GetElement(row, column) for column in range(4)] for row in range(3)]\n"
            f"json.dump({{'dims': list(array.shape), 'sum': int((array > 0).sum()), 'dtype': str(array.dtype), 'digest': voxelPositionDigest(array, ijkToRas)}}, open({resultPath!r}, 'w'))\n"
            "slicer.app.exit(0)\n")
    # The official process must not inherit anything of the fork under test (its rc file, Python and library paths, Qt plugins)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "SLICERRC" and not key.startswith(("SLICER", "PYTHON", "QT_")) and key not in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH")
    }
    officialHome = os.path.join(g.out, "official-home")
    os.makedirs(officialHome, exist_ok=True)
    environment["HOME"] = officialHome
    command = [OFFICIAL_SLICER, "--disable-settings", "--no-splash", "--python-script", script]
    if shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a", "-s", "-screen 0 1280x1024x24", *command]
    process = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield from g.until(lambda: process.poll() is not None, 120000, step=500, what="official Slicer")
    if process.poll() is None:
        process.kill()
    if not os.path.isfile(resultPath):
        g.check("official-loads-saved", False, "official Slicer 5.13 did not load the file")
        return
    with open(resultPath) as stream:
        result = json.load(stream)
    import numpy as np

    expected = int((reference[0] > 0).sum())
    # SimpleITK works in LPS with the file's axis order; Slicer may flip an axis on loading, so the voxels are compared by position (RAS)
    direction = np.asarray(reference[4], float).reshape(3, 3) * np.asarray(reference[2], float)
    ijkToLps = np.column_stack([direction, np.asarray(reference[3], float)])
    ijkToRas = np.diag([-1.0, -1.0, 1.0]) @ ijkToLps
    namespace = {}
    exec(VOXEL_DIGEST_SOURCE, namespace)
    samePositions = result["digest"] == namespace["voxelPositionDigest"](reference[0], ijkToRas)
    g.check("official-loads-saved", result["sum"] == expected and sorted(result["dims"]) == sorted(reference[1]) and samePositions,
            f"official 5.13 read dims {result['dims'][::-1]}, {result['sum']} voxels, the voxels are at the same positions in space: {samePositions} "
            f"(expected dims {list(reference[1])}, {expected} voxels)")


def dicom(g):
    """Drop a DICOM folder: the actions to a Draw-ready scan (None when it is not reached)."""
    yield from ensureStudio(g)
    resetState(g)
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    folder = g.fixtures.get("dicom", {}).get("folder")
    if not folder:
        g.skip("dicom-draw-ready", g.fixtures.get("dicomSkipped", "no DICOM fixture"))
        g.metric("toDrawReady", None)
        return
    start = g.actions
    yield from g.drop([folder], "Drop the DICOM folder")
    yield 3000
    for _ in range(3):
        modal = qt.QApplication.activeModalWidget()
        if modal is None:
            break
        g.shot("dicom-dialog")
        g.note("dicomDialog", str(getattr(modal, "text", ""))[:200])
        yield from g.click(activeDialogButton(g, "ok", "close", "yes"), "Click OK in the dialog")
        yield 1000
    g.shot("dicom-after-drop")
    volumes = [node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if "Synthetic" in node.GetName() or "slice" in node.GetName().lower() or node.GetName().startswith("1:")]
    ready = g.currentWorkspace() == "draw" and g.segmentEditor() is not None and g.segmentEditor().sourceVolumeNode() is not None
    g.note("dicomVolumes", [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")])
    g.check("dicom-draw-ready", ready, f"workspace {g.currentWorkspace()}, source volume {g.segmentEditor().sourceVolumeNode().GetName() if g.segmentEditor() and g.segmentEditor().sourceVolumeNode() else None}")
    g.metric("toDrawReady", (g.actions - start) if ready else None)


def stateSnapshot(g):
    shell = g.shell()
    layoutNode = slicer.app.layoutManager().layoutLogic().GetLayoutNode()
    return {
        "mode": interactionMode(),
        "maximized": layoutNode.GetNumberOfMaximizedViewNodes() > 0,
        "focusMode": bool(shell.focusMode()) if shell is not None else None,
        "inspector": bool(shell.isInspectorVisible()) if shell is not None else None,
        "effect": g.effectName(),
        "workspace": g.currentWorkspace(),
        "module": shell.currentModuleName() if shell is not None else None,
        "layout": slicer.app.layoutManager().layout,
        "popup": qt.QApplication.activePopupWidget() is not None or qt.QApplication.activeModalWidget() is not None,
        "focus": widgetText(qt.QApplication.focusWidget()),
    }


def drawStateEffect(g):
    from StudioLib import draw

    shell = g.shell()
    state = draw.drawState(shell)
    return None if state.effect is draw._UNSET else state.effect


def moduleSpinBoxEdit(g):
    """The line edit of a visible, enabled spin box (QSpinBox, QDoubleSpinBox or a ctk one) of the open module panel, or None."""
    for edit in g.mainWindow.findChildren("QLineEdit"):
        parent = edit.parent()
        if parent is None or not (edit.objectName == "qt_spinbox_lineedit" or parent.inherits("QAbstractSpinBox")):
            continue
        if edit.isVisible() and edit.isEnabled() and not edit.readOnly and edit.width > 20 and edit.height > 8:
            return edit
    return None


def escMatrix(g):
    """Esc from each state of USABILITY section 3: the ladder in order, then every state on its own."""
    yield from ensureStudio(g)
    resetState(g)
    yield from g.until(lambda: g.widget("StudioLibraryStartOnTemplateButton", "QPushButton") is not None, 8000)
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Setup: Start on MNI template", setup=True)
    yield from waitForDraw(g)
    banner = g.widget("StudioDownloadBannerSecondaryButton", "QPushButton")
    if banner is not None:
        yield from g.click(banner, "Setup: dismiss the template banner", setup=True)
    interaction = slicer.app.applicationLogic().GetInteractionNode()
    selection = slicer.app.applicationLogic().GetSelectionNode()
    layoutManager = slicer.app.layoutManager()
    results = {}

    def record(name, ok, detail):
        results[name] = (ok, detail)
        g.log("esc-state", state=name, ok=ok, detail=detail)

    # --- the ladder in order (QW-B): the window/level mode, a maximized view, hidden panels, the tool ---
    g.segmentEditor().setActiveEffectByName("Paint")
    yield 400
    interaction.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.AdjustWindowLevel)
    yield 300
    layoutManager.addMaximizedViewNode(g.sliceWidget("Red").mrmlSliceNode()) if hasattr(layoutManager, "addMaximizedViewNode") else None
    yield 300
    yield from g.key("F11", "Setup: F11 hides the panels", settle=400)
    toolBefore = g.effectName()
    order = []
    for rung in ("windowLevel", "maximized", "focusMode"):
        before = stateSnapshot(g)
        yield from g.key("Esc", f"Esc (rung {rung})", settle=400)
        after = stateSnapshot(g)
        order.append((rung, before, after))
    # The tool is the last rung. Another mouse mode deselects it (C++), so it is armed again here
    g.segmentEditor().setActiveEffectByName("Paint")
    yield 400
    before = stateSnapshot(g)
    yield from g.key("Esc", "Esc (rung tool)", settle=400)
    order.append(("tool", before, stateSnapshot(g)))
    place = order[0][2]["mode"] == viewTransformMode() and order[0][1]["mode"] != viewTransformMode()
    maximized = order[1][1]["maximized"] and not order[1][2]["maximized"] and order[1][2]["focusMode"]
    panels = order[2][1]["focusMode"] and not order[2][2]["focusMode"]
    tool = order[3][2]["effect"] is None and order[3][1]["effect"] is not None
    interaction.SwitchToViewTransformMode()
    effects = [f"{rung}: {before['effect']} -> {after['effect']}" for rung, before, after in order]
    g.check("esc-ladder", place and maximized and panels and tool,
            f"Esc ladder order: window/level mode left on press 1: {place}; maximized view restored on press 2: {maximized}; panels shown on press 3: {panels}; tool deselected on press 4: {tool} (tool per press: {'; '.join(effects)})")
    recorded = drawStateEffect(g)
    record("tool", tool, f"after Esc the tool is deselected ({tool}); Draw records {recorded}")
    # --- Place mode alone: the tool resumes when the mode ends (W2-05) ---
    g.segmentEditor().setActiveEffectByName("Paint")
    yield 400
    interaction.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.Place)
    yield 300
    deselectedByPlace = g.effectName() is None
    yield from g.key("Esc", "Esc leaves Place mode", settle=500)
    resumes = g.effectName() == "Paint"
    g.check("place-keeps-tool", resumes, f"Place deselected the tool: {deselectedByPlace}; after Esc the tool is {g.effectName()}")
    record("place", interactionMode() == viewTransformMode() and resumes, f"mode {interactionMode()}, tool {g.effectName()}")
    # --- Adjust window/level mode ---
    interaction.SetCurrentInteractionMode(slicer.vtkMRMLInteractionNode.AdjustWindowLevel)
    yield 300
    yield from g.key("Esc", "Esc leaves the window/level mode", settle=400)
    record("windowLevel", interactionMode() == viewTransformMode(), f"mode after Esc: {interactionMode()}")
    # --- a maximized view (double-click) ---
    yield from g.click(g.sliceView("Red"), "Double-click the axial view", double=True)
    yield 400
    wasMaximized = stateSnapshot(g)["maximized"]
    yield from g.key("Esc", "Esc restores the layout", settle=400)
    record("maximized", wasMaximized and not stateSnapshot(g)["maximized"], f"maximized before {wasMaximized}, after {stateSnapshot(g)['maximized']}")
    # --- Single (Alt+2): a maximize from U-06, a layout change before ---
    layoutBefore = layoutManager.layout
    yield from g.key("Alt+2", "Alt+2 (Single)", settle=500)
    single = stateSnapshot(g)
    yield from g.key("Esc", "Esc leaves Single", settle=400)
    back = stateSnapshot(g)
    singleLeft = (back["layout"] == layoutBefore) or (single["maximized"] and not back["maximized"])
    record("single", singleLeft, f"layout {layoutBefore} -> {single['layout']} (maximized {single['maximized']}) -> after Esc {back['layout']} (maximized {back['maximized']})")
    layoutManager.setLayout(layoutBefore)
    layoutManager.removeAllMaximizedViewNodes()
    yield 400
    # --- hidden panels (F11) ---
    yield from g.key("F11", "F11 hides the panels", settle=400)
    wasFocus = stateSnapshot(g)["focusMode"]
    yield from g.key("Esc", "Esc shows the panels", settle=400)
    record("hiddenPanels", wasFocus and not stateSnapshot(g)["focusMode"], f"focus mode before {wasFocus}, after {stateSnapshot(g)['focusMode']}")
    # --- the palette (a popup or modal) ---
    yield from openSearch(g)
    wasOpen = searchPopup(g) is not None
    yield from g.key("Esc", "Esc closes the search", settle=400)
    record("palette", wasOpen and searchPopup(g) is None, f"search open before {wasOpen}, after {searchPopup(g) is not None}")
    # --- the cheat sheet ---
    yield from g.key("Ctrl+/", "Ctrl+/ shows the shortcuts", settle=500)
    sheet = planned(g, "cheatSheet")
    if sheet is not None:
        yield from g.key("Esc", "Esc closes the cheat sheet", settle=400)
        record("cheatSheet", planned(g, "cheatSheet") is None, "the cheat sheet closed" if planned(g, "cheatSheet") is None else "the cheat sheet is still open")
    else:
        results["cheatSheet"] = (None, "this build shows a toast, not a cheat sheet overlay")
    # --- the tool (Draw) ---
    g.segmentEditor().setActiveEffectByName("Paint")
    yield 400
    yield from g.key("Esc", "Esc deselects the tool", settle=400)
    record("activeTool", g.effectName() is None, f"tool after Esc: {g.effectName()}")
    # --- a spin box with the focus: the first Esc leaves the spin box and runs the ladder in the same press ---
    g.segmentEditor().setActiveEffectByName("Paint")
    yield 500
    spin = None
    for candidate in g.mainWindow.findChildren("QLineEdit"):
        if candidate.objectName == "qt_spinbox_lineedit" and candidate.isVisible() and str(candidate.text).endswith("%"):
            spin = candidate
    if spin is not None:
        yield from g.click(spin, "Click the brush size field")
        yield 300
        focusBefore = widgetText(qt.QApplication.focusWidget())
        yield from g.key("Esc", "Esc with a spin box focused", settle=500)
        record("spinBox", g.effectName() is None, f"focus {focusBefore}; tool after the first Esc: {g.effectName()}")
    else:
        results["spinBox"] = (None, "the brush size field was not found")
    # --- a transient module, with nothing focused and then with a spin box of the module focused (rung 5) ---
    for variant, stateName in ((False, "transient"), (True, "transientSpinBox")):
        g.segmentEditor().setActiveEffectByName("Paint")
        yield 300
        typed = yield from searchAndOpen(g, "Volumes", "vol")
        if typed is None:
            results[stateName] = (None, "Volumes was not found by the search")
            continue
        yield from waitTransient(g, "Volumes")
        focusBefore = widgetText(qt.QApplication.focusWidget())
        if variant:
            spin = moduleSpinBoxEdit(g)
            if spin is None:
                results[stateName] = (None, "the module panel has no visible spin box")
                yield from closeTransient(g, "Close Volumes")
                continue
            yield from g.click(spin, "Click a spin box of the module panel")
            yield 300
            focusBefore = widgetText(qt.QApplication.focusWidget())
        yield from g.key("Esc", "Esc with a spin box of the module focused" if variant else "Esc in a transient module", settle=700)
        left = g.currentWorkspace() == "draw"
        record(stateName, left, f"focus before: {focusBefore}; workspace after Esc: {g.currentWorkspace()}, module {g.shell().currentModuleName()}, focus {widgetText(qt.QApplication.focusWidget())}")
        if not left:
            yield from closeTransient(g, "Close Volumes")
    # --- Library (outside the ladder) ---
    yield from g.key("Ctrl+Alt+1", "Go to Library", settle=600)
    yield from g.key("Esc", "Esc on Library", settle=400)
    record("library", g.currentWorkspace() == "library", f"workspace after Esc on Library: {g.currentWorkspace()}")
    failing = {name: detail for name, (ok, detail) in results.items() if ok is False}
    skipped = {name: detail for name, (ok, detail) in results.items() if ok is None}
    g.note("escStates", {name: {"ok": ok, "detail": detail} for name, (ok, detail) in results.items()})
    g.check("esc-matrix", not failing, "; ".join(f"{name}: {detail}" for name, detail in failing.items()) or f"{len(results) - len(skipped)} states leave exactly their own state")
    g.metric("states", len(results))
    g.shot("end")


def keyboardLayouts(g):
    r"""Layouts de and fr (real X keyboards: xdotool only): Ctrl+\\ cannot be typed; Ctrl+5 and the edge handle toggle the inspector;
    AltGr+@ in a search field types '@' (the key of '@' on each layout: AltGr+Q on de, AltGr+0 on fr).
    """
    yield from ensureStudio(g)
    if g.backend.name != "xdotool":
        for key in ("keyboard-layouts",):
            g.skip(key, "needs the xdotool input backend (a real X keyboard)")
        return
    if shutil.which("setxkbmap") is None:
        g.skip("keyboard-layouts", "setxkbmap is not installed")
        return
    resetState(g)
    yield from g.click(g.widget("StudioLibraryStartOnTemplateButton", "QPushButton"), "Setup: Start on MNI template", setup=True)
    yield from waitForDraw(g)
    problems = []
    original = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, check=False).stdout
    try:
        for layout in ("de", "fr"):
            subprocess.run(["setxkbmap", layout], check=False)
            yield 400
            shell = g.shell()
            # Ctrl+5 (View > Module Panel in Slicer, the second inspector key of Studio)
            before = shell.isInspectorVisible()
            yield from g.key("Ctrl+5", f"[{layout}] Ctrl+5", settle=500)
            toggled = shell.isInspectorVisible() != before
            if shell.isInspectorVisible() == before:
                problems.append(f"{layout}: Ctrl+5 did not toggle the inspector")
            else:
                yield from g.key("Ctrl+5", f"[{layout}] Ctrl+5 shows it again", settle=500)
            # The edge handle of the hidden inspector
            shell.setInspectorVisible(False)
            yield 400
            handle = planned(g, "inspectorHandle")
            if handle is not None:
                yield from g.click(handle, f"[{layout}] Click the edge handle")
                yield 500
                if not shell.isInspectorVisible():
                    problems.append(f"{layout}: the edge handle did not show the inspector")
            else:
                problems.append(f"{layout}: there is no edge handle for a hidden inspector")
                shell.setInspectorVisible(True)
            yield 300
            # AltGr+2 in a search field: '@' on the layouts that have it there, typed into the focused search field
            top = yield from openSearch(g)
            field = searchField(top)
            if field is not None:
                field.clear()
                # AltGr + the key that types '@' on the layout (the 2 key types the superscript two or a fraction)
                g.backend.run("key", "--clearmodifiers", "ISO_Level3_Shift+" + {"de": "q", "fr": "agrave"}[layout])
                yield 300
                typedText = str(field.text)
                g.note(f"altgr2:{layout}", typedText)
                if "@" not in typedText:
                    problems.append(f"{layout}: AltGr+@ typed {typedText!r}, not '@'")
            g.closePopups()
            yield 300
    finally:
        keymap = {key: value.strip() for key, value in (line.split(":", 1) for line in original.splitlines() if ":" in line)}
        restore = ["setxkbmap", "-layout", keymap.get("layout", "us"), "-variant", keymap.get("variant", "")]
        restore += ["-option", ""] + (["-option", keymap["options"]] if keymap.get("options") else [])
        subprocess.run(restore, check=False)
    g.check("keyboard-layouts", not problems, "; ".join(problems) or "Ctrl+5, the edge handle and AltGr+2 work on de and fr")


#
# Registry
#


class TaskSpec:
    """A task of the gate. ``headline`` is the metric that is the task's score (None: all actions of the task)."""

    def __init__(self, name, title, run, headline=None, viaRc=False, fitWindow=True, homeFrom=None, keyboardLayouts=False):
        self.name = name
        self.title = title
        self.run = run
        self.headline = headline
        #: Start the task through SLICERRC so that the real first-run path (and the disclaimer) runs
        self.viaRc = viaRc
        self.fitWindow = fitWindow
        #: Run in the settings folder that this task left behind (the warm session of find-tools)
        self.homeFrom = homeFrom
        self.keyboardLayouts = keyboardLayouts


TASK_SPECS = [
    TaskSpec("novice", "novice-first-trace", novice, viaRc=True, fitWindow=False),
    TaskSpec("nav", "patient-scan-navigation", nav),
    TaskSpec("find-tools", "find-tools-and-return", findTools, headline="scored"),
    TaskSpec("find-tools-warm", "find-tools-and-return, warm session", findToolsWarm, headline="scored", homeFrom="find-tools"),
    TaskSpec("classic", "classic-migration", classic, headline="tour"),
    TaskSpec("case", "case-report-tracing", case, headline="total"),
    TaskSpec("lnm", "lnm-cohort", lnm, headline="A"),
    TaskSpec("wrong-side", "review scenario: wrong side", wrongSide, headline="actions"),
    TaskSpec("reopen", "review scenario: reopen a saved lesion", reopen, headline="actions"),
    TaskSpec("dicom", "review scenario: DICOM folder drop", dicom, headline="toDrawReady"),
    TaskSpec("esc-matrix", "review scenario: the Esc matrix", escMatrix, headline="states"),
    TaskSpec("keyboard-layouts", "review scenario: keyboard layouts de and fr", keyboardLayouts, keyboardLayouts=True),
]


def taskSpec(name):
    for spec in TASK_SPECS:
        if spec.name == name:
            return spec
    raise KeyError(name)


def runSpecs(gate, specs, wave, onDone, lastJob=True, targets=None):
    """Run task specs one after the other from the event loop; ``onDone(results, errors)`` gets the results.json entries."""
    from . import harness

    targets = targets if targets is not None else harness.loadTargets()
    results = {}
    jobs = []
    for index, spec in enumerate(specs):

        def begin(gate, spec=spec, index=index):
            gate.isLastJob = lastJob and index == len(specs) - 1
            if spec.fitWindow:
                gate.fitWindow()

        def end(gate, error, spec=spec):
            results[spec.name] = harness.buildTaskResult(gate, spec, error, targets, wave, ASSERTIONS)

        jobs.append((spec.name, lambda gate, spec=spec: spec.run(gate), begin, end))
    scheduler = harness.Scheduler(gate, jobs, lambda: onDone(results, scheduler.errors))
    return scheduler
