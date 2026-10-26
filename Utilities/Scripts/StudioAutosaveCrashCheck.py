#!/usr/bin/env python3
r"""Check the scene autosave and crash recovery of Slicer Studio end to end (A-01, SPEC 9.9).

Phases (each one a Slicer process, headless, with a fresh temporary HOME shared by phases A and B):

- **A** (the fork, normal mode, not ``--testing``): build a lesion-mapping scene (a user volume
  ``~/user data/MR Head.nrrd``, the MNI template copied into the application cache, a "Lesions" segmentation,
  a T map with a diverging color table, a "Peaks" point list, a "Stats" table, layout 7001), autosave it with
  ``autosave.instance().autosaveNow()``, measure full and lesion-only generations, record what the scene contains
  (``expected.json``), then kill the process with SIGKILL (``os._exit(137)`` where SIGKILL does not exist).
- **B** (the fork, same HOME): the next launch must show ``StudioRecoveryDialog`` listing the session (with
  ``--studio``, the Studio recovery card ``StudioRecoveryCard`` of A-01b instead, and no dialog). The session is
  recovered; volumes, segments, markups, table and color table must be identical, no storage node may point into
  the autosave folder, written nodes must keep their original file (or none) and stay modified, and the scene
  must still have unsaved changes.
- **C** (each ``--checker``, for example official Slicer 5.12.4 and 5.13): load the autosaved ``Scene.mrml``
  directly and compare its content; new errors in the application log fail the check. Official installations
  run under ``LANG=LC_ALL=C.UTF-8``.

With ``--projects`` (A-09) the same scene is a *project* of the Slicer Studio folder (Studio, ``SLICER_STUDIO_PROJECTS_ROOT``
= ``<HOME>/Slicer Studio``): phase A waits for the project, saves it, measures the lesion-only saves, makes one more
edit that is not saved yet and crashes; phase B must show ``StudioProjectCrashCard``, open the project from it and find
every saved edit (the last, unsaved one is lost), every file inside the project and nothing "modified since read"; phase
C loads the project's scene file in each checker. Without ``--projects`` the private recovery autosave of A-01 is
checked (``SLICER_STUDIO_PROJECTS=0``).

A report is written as ``report.json`` and ``report.txt`` in the output directory; the exit code is non-zero if
any check fails. Only the Python standard library is used outside Slicer.

Usage::

  StudioAutosaveCrashCheck.py --slicer PATH --out DIR [--checker NAME=PATH ...] [--studio | --projects] [--timeout S]
      [--no-xvfb] [--mrhead FILE]

Example::

  StudioAutosaveCrashCheck.py --slicer /opt/S-r/Slicer-build/Slicer \
      --checker official-5.12.4=/opt/slicer-dl/Slicer-5.12.4-linux-amd64/Slicer \
      --checker official-5.13=/opt/slicer-dl/Slicer-5.13.0-2026-10-16-linux-amd64/Slicer --out /tmp/crash

The code that runs inside a checker only uses public APIs that exist in Slicer 5.12.
"""

import argparse
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
import traceback

ERROR_LEVELS = ("Error", "Critical", "Fatal")
# Messages caused by the test environment (no network, software rendering), not by the loaded files
ENVIRONMENT_NOISE = ("extension", "Extension", "QNetworkReply", "Network", "SSL", "libGL", "MESA", "llvmpipe", "GLX", "XDG_RUNTIME_DIR", "QOpenGLWidget")
TEMPLATE_FILE = "tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz"
GENERATION_RUNS = 5

# =============================================================================
# Code that runs inside Slicer
# =============================================================================


def runInSlicer(configPath):
    """Entry point of the generated scripts."""
    with open(configPath, encoding="utf-8") as f:
        config = json.load(f)
    phase = {"A": _PhaseA, "B": _PhaseB, "C": _PhaseC}[config["phase"]]
    phase(config).start()


def _md5(array):
    import hashlib

    import numpy as np

    return hashlib.md5(np.ascontiguousarray(array).tobytes()).hexdigest()


def _nodeByName(name, className):
    import slicer

    nodes = slicer.mrmlScene.GetNodesByClassByName(className, name)
    return nodes.GetItemAsObject(0) if nodes.GetNumberOfItems() else None


def _sceneContent():
    """What the checks compare (the same in every Slicer version)."""
    import numpy as np

    import slicer

    content = {}
    volume = _nodeByName("MR Head", "vtkMRMLScalarVolumeNode")
    for name in ("MR Head", "Template", "T map"):
        node = _nodeByName(name, "vtkMRMLScalarVolumeNode")
        content[name] = _md5(slicer.util.arrayFromVolume(node)) if node is not None and node.GetImageData() is not None else None
    segmentation = _nodeByName("Lesions", "vtkMRMLSegmentationNode")
    segments = {}
    if segmentation is not None and volume is not None:
        for index in range(segmentation.GetSegmentation().GetNumberOfSegments()):
            segmentId = segmentation.GetSegmentation().GetNthSegmentID(index)
            segments[segmentId] = _md5(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume))
    content["segments"] = segments
    points = _nodeByName("Peaks", "vtkMRMLMarkupsFiducialNode")
    content["Peaks"] = slicer.util.arrayFromMarkupsControlPoints(points).round(4).tolist() if points is not None else None
    table = _nodeByName("Stats", "vtkMRMLTableNode")
    content["Stats"] = [[table.GetCellText(r, c) for c in range(table.GetNumberOfColumns())] for r in range(table.GetNumberOfRows())] if table is not None else None
    lut = _nodeByName("LNM Diverging", "vtkMRMLColorTableNode")
    if lut is not None:
        values = [list(lut.GetLookupTable().GetTableValue(i)) for i in range(lut.GetNumberOfColors())]
        content["LUT"] = np.round(np.array(values) * 255).astype(int).tolist()
    else:
        content["LUT"] = None
    counts = {}
    scene = slicer.mrmlScene
    for index in range(scene.GetNumberOfNodes()):
        node = scene.GetNthNode(index)
        if node.GetSaveWithScene() and not node.GetSingletonTag() and not node.IsA("vtkMRMLStorageNode") and not node.GetHideFromEditors():
            counts[node.GetClassName()] = counts.get(node.GetClassName(), 0) + 1
    content["nodeCounts"] = counts
    return content


class _LogCapture:
    def __init__(self):
        import slicer

        self.model = slicer.app.errorLogModel()
        self.first = self.model.rowCount()

    def entries(self):
        found = []
        for row in range(self.first, self.model.rowCount()):
            level = self.model.index(row, 2).data()
            if level in ERROR_LEVELS + ("Warning",):
                found.append({"level": level, "text": (self.model.logEntryDescription(row) or "")[:400]})
        return found


class _Phase:
    def __init__(self, config):
        self.config = config
        self.result = {"phase": config["phase"]}

    def start(self):
        import qt

        qt.QTimer.singleShot(500, self._guarded)

    def _guarded(self):
        try:
            self.run()
        except Exception:
            self.result["exception"] = traceback.format_exc()
            self.finish()

    def wait(self, condition, timeoutS, then):
        """Call ``then()`` once ``condition()`` is true (polled from timers), or record a timeout."""
        import qt

        deadline = time.monotonic() + timeoutS

        def poll():
            try:
                if condition():
                    then()
                    return
                if time.monotonic() > deadline:
                    self.result.setdefault("failures", []).append(f"timeout waiting for {getattr(condition, '__name__', 'a condition')}")
                    self.finish()
                    return
                qt.QTimer.singleShot(100, poll)
            except Exception:
                self.result["exception"] = traceback.format_exc()
                self.finish()

        poll()

    def write(self):
        with open(self.config["resultPath"], "w", encoding="utf-8") as f:
            json.dump(self.result, f, indent=1, default=str)

    def finish(self):
        import slicer

        self.write()
        slicer.app.exit(0)


class _PhaseA(_Phase):
    """Build the scene, autosave it, measure generations, then crash."""

    def run(self):
        import qt

        import slicer

        def serviceReady():
            from StudioLib import autosave

            return autosave.instance() is not None and slicer.util.mainWindow().findChild(qt.QAction, "StudioRecoverAutosaveAction") is not None

        self.wait(serviceReady, 60, self.build)

    def projectReady(self):
        return self.service.project is not None

    def projectBuilt(self):
        """--projects: the project exists; save it, then measure lesion-only saves."""
        self.result["projectPath"] = self.service.project.path
        self.result["firstSave"] = self.service.saveNow("crash check", progress=False)
        self.result["firstSaveMetrics"] = self.service.metrics["last"]
        self.generations = []
        self.projectIncremental()

    def projectIncremental(self):
        """One lesion-only edit saved by the project engine; GENERATION_RUNS of them, then the crash."""
        import slicer

        if len(self.generations) >= GENERATION_RUNS:
            self.projectCrash()
            return
        offset = len(self.generations) + 1
        self.lesion[self.center[0], self.center[1], self.center[2] + 12 + offset] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.lesion, self.segmentation, self.segmentId, self.volume)
        if not self.service.autosaveNow("lesion edit"):
            self.result.setdefault("failures", []).append("autosaveNow did not start a project save after a lesion edit")
            self.projectCrash()
            return
        self.wait(self.generationFinished, 120, self.projectSaved)

    def projectSaved(self):
        self.generations.append(self.service.metrics["last"])
        self.projectIncremental()

    def projectCrash(self):
        import slicer

        service = self.service
        self.result["generations"] = self.generations
        self.result["expected"] = _sceneContent()
        self.result["modifiedAfterSave"] = bool(slicer.mrmlScene.GetStorableNodesModifiedSinceRead())
        # One more edit that is not saved yet (lost by the crash)
        self.lesion[self.center[0] - 8, self.center[1], self.center[2]] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.lesion, self.segmentation, self.segmentId, self.volume)
        self.result["unsavedVoxel"] = [int(self.center[0] - 8), int(self.center[1]), int(self.center[2])]
        self.result["pollMs"] = [service.poll() or service.metrics["pollMs"] for _ in range(5)]
        self.result["nodeCount"] = slicer.mrmlScene.GetNumberOfNodes()
        self.result["crashed"] = True
        self.write()
        sys.stdout.flush()
        if hasattr(signal, "SIGKILL"):
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(137)

    def build(self):
        import numpy as np
        import vtk

        import slicer
        from StudioLib import autosave

        self.service = autosave.instance()
        scene = slicer.mrmlScene
        userDir = os.path.join(os.path.expanduser("~"), "user data")
        os.makedirs(userDir, exist_ok=True)
        mrHeadPath = os.path.join(userDir, "MR Head.nrrd")
        if self.config.get("mrhead"):
            source = slicer.util.loadVolume(self.config["mrhead"])
        else:
            k, j, i = np.indices((130, 256, 256))
            voxels = (((i - 128) ** 2 + (j - 128) ** 2) / 50 + (k * 3) % 200).astype(np.int16)
            source = slicer.util.addVolumeFromArray(voxels, name="MR Head")
            source.SetSpacing(0.94, 0.94, 1.3)
        slicer.util.saveNode(source, mrHeadPath)
        scene.RemoveNode(source)
        volume = slicer.util.loadVolume(mrHeadPath)
        # The MNI template, copied into the application cache (a volatile place: copied, not referenced)
        import SampleData

        bundled = os.path.join(os.path.dirname(SampleData.__file__), "Resources", "Data", TEMPLATE_FILE)
        os.makedirs(slicer.app.cachePath, exist_ok=True)
        cached = os.path.join(slicer.app.cachePath, TEMPLATE_FILE)
        if os.path.exists(bundled):
            shutil.copy(bundled, cached)
            template = slicer.util.loadVolume(cached)
        else:
            template = slicer.util.addVolumeFromArray(np.arange(97 * 115 * 97, dtype=np.int16).reshape(97, 115, 97) % 300, name="Template")
            template.SetSpacing(2, 2, 2)
            slicer.util.saveNode(template, cached.replace(".nii.gz", ".nrrd"))
        template.SetName("Template")
        segmentation = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        self.segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        segmentation.GetSegmentation().GetSegment(self.segmentId).SetColor(0.949, 0.329, 0.357)
        shape = slicer.util.arrayFromVolume(volume).shape
        kk, jj, ii = np.ogrid[: shape[0], : shape[1], : shape[2]]
        center = (shape[0] // 2, shape[1] // 2 + 20, shape[2] // 2 + 30)
        self.lesion = (((kk - center[0]) ** 2 + (jj - center[1]) ** 2 + (ii - center[2]) ** 2) <= 12**2).astype(np.uint8)
        self.center = center
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.lesion, segmentation, self.segmentId, volume)
        ijkToRas = vtk.vtkMatrix4x4()
        template.GetIJKToRASMatrix(ijkToRas)
        tvalues = np.random.default_rng(1).normal(0, 3, size=slicer.util.arrayFromVolume(template).shape).astype(np.float32)
        tmap = slicer.util.addVolumeFromArray(tvalues, ijkToRas, "T map")
        lut = scene.AddNewNodeByClass("vtkMRMLColorTableNode", "LNM Diverging")
        lut.SetTypeToUser()
        lut.SetNumberOfColors(256)
        for index in range(256):
            lut.SetColor(index, f"c{index}", index / 255.0, 0.2, 1 - index / 255.0, round(abs(index - 127.5) / 127.5 * 255) / 255.0)
        tmap.CreateDefaultDisplayNodes()
        tmap.GetDisplayNode().SetAndObserveColorNodeID(lut.GetID())
        points = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Peaks")
        for position in ((10, 20, 30), (-5.5, 7.25, 1.0), (0, 0, 0)):
            points.AddControlPoint(position)
        table = scene.AddNewNodeByClass("vtkMRMLTableNode", "Stats")
        table.AddColumn().SetName("name")
        column = vtk.vtkDoubleArray()
        column.SetName("value")
        table.AddColumn(column)
        for row in range(3):
            table.AddEmptyRow()
            table.SetCellText(row, 0, f"row {row}")
            table.SetCellText(row, 1, str(row * 1.5))
        slicer.util.setSliceViewerLayers(background=volume, fit=True)
        layoutManager = slicer.app.layoutManager()
        try:
            from StudioLib import shell

            studio = shell.instance()
            if studio is not None:
                studio.switchTo("draw")
        except Exception:
            pass
        layoutManager.setLayout(7001)
        self.volume, self.segmentation = volume, segmentation
        self.result["layout"] = layoutManager.layout
        if self.config.get("projects"):
            self.wait(self.projectReady, 60, self.projectBuilt)
            return
        self.measureFullGenerations()

    def measureFullGenerations(self):
        """Main-thread cost of full generations of this scene, in scratch slots (median of several runs)."""
        import qt

        import slicer
        from StudioLib import autosave_io

        runs = []
        scratchDir = os.path.join(self.config["outDir"], "scratch")
        for run in range(GENERATION_RUNS):
            slotDir = os.path.join(scratchDir, f"full-{run}")
            os.makedirs(slotDir, exist_ok=True)
            writer = autosave_io.SlotWriter(slotDir, autosave_io.backgroundWriter(), {"autosaveRoot": scratchDir})
            plan = writer.plan(slicer.mrmlScene)
            while not writer.runMainThreadStep(plan):
                slicer.app.processEvents()
            writer.submitWorkerJobs(plan)
            writer.executor.drain(60)
            result = writer.finalize(plan)
            runs.append({"planMs": plan.metrics["planMs"], "mainThreadMs": plan.metrics["mainThreadMs"], "maxSliceMs": plan.metrics["maxSliceMs"],
                         "mainThreadTotalMs": plan.metrics["planMs"] + plan.metrics["mainThreadMs"], "bytes": result["bytes"],
                         "jobMs": plan.metrics.get("jobMs")})
        shutil.rmtree(os.path.join(self.config["outDir"], "scratch"), ignore_errors=True)
        self.result["fullGenerations"] = runs

        qt.QTimer.singleShot(200, self.autosave)

    def autosave(self):
        self.generations = []
        self.result["autosaveNowStarted"] = self.service.autosaveNow("crash check")
        self.wait(self.generationFinished, 120, self.incremental)

    def generationFinished(self):
        from StudioLib import autosave_io

        return self.service.plan is None and autosave_io.backgroundWriter().pending() == 0 and self.service.status().lastSaveTime is not None

    def incremental(self):
        import slicer

        self.generations.append(self.service.metrics["last"])
        if len(self.generations) > GENERATION_RUNS:
            self.crash()
            return
        # Lesion-only change: one more voxel
        offset = len(self.generations)
        self.lesion[self.center[0], self.center[1], self.center[2] + 12 + offset] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(self.lesion, self.segmentation, self.segmentId, self.volume)
        started = self.service.autosaveNow("lesion edit")
        if not started:
            self.result.setdefault("failures", []).append("autosaveNow did not start after a lesion edit")
            self.crash()
            return
        self.wait(self.generationFinished, 120, self.incremental)

    def crash(self):
        import slicer

        service = self.service
        self.result["generations"] = self.generations
        # Thumbnail cost (a separate tick of a generation, at most every 5 minutes)
        thumbnailMs = []
        for _ in range(3):
            start = time.perf_counter()
            image = service.grabThumbnail()
            thumbnailMs.append((time.perf_counter() - start) * 1000.0)
        self.result["thumbnailMs"] = thumbnailMs
        self.result["thumbnailGrabbed"] = image is not None
        self.result["pollMs"] = [service.poll() or service.metrics["pollMs"] for _ in range(5)]
        self.result["nodeCount"] = slicer.mrmlScene.GetNumberOfNodes()
        self.result["sessionDir"] = service.sessionDirectory()
        self.result["slotDir"] = service.writer.slotDir if service.writer is not None else None
        self.result["expected"] = _sceneContent()
        self.result["kinds"] = {entry.get("name"): entry.get("kind") for entry in (service.writer.entries if service.writer else {}).values()}
        self.result["sceneModified"] = bool(slicer.mrmlScene.GetModifiedSinceRead())
        self.result["storableModified"] = bool(slicer.mrmlScene.GetStorableNodesModifiedSinceRead())
        self.result["unsavedSummary"] = service.unsavedSummary()
        self.result["crashed"] = True
        self.write()
        sys.stdout.flush()
        if hasattr(signal, "SIGKILL"):
            os.kill(os.getpid(), signal.SIGKILL)
        os._exit(137)


class _PhaseB(_Phase):
    """The next launch offers the recovery; recover and compare."""

    def run(self):
        import qt

        import slicer

        self.capture = _LogCapture()
        if self.config.get("projects"):
            self.wait(lambda: slicer.util.mainWindow().findChild(qt.QWidget, "StudioProjectCrashCard") is not None, 60, self.openProject)
            return

        def offered():
            if self.config.get("studio"):
                return slicer.util.mainWindow().findChild(qt.QWidget, "StudioRecoveryCard") is not None
            dialog = slicer.util.mainWindow().findChild(qt.QDialog, "StudioRecoveryDialog")
            return dialog is not None and dialog.isVisible()

        self.wait(offered, 60, self.recover)

    def recover(self):
        import qt

        import slicer
        from StudioLib import autosave, autosave_io, recovery

        mainWindow = slicer.util.mainWindow()
        dialog = mainWindow.findChild(qt.QDialog, "StudioRecoveryDialog")
        self.result["dialogVisible"] = bool(dialog is not None and dialog.isVisible())
        if self.config.get("studio"):
            self.result["recoveryCard"] = mainWindow.findChild(qt.QWidget, "StudioRecoveryCard") is not None
        service = autosave.instance()
        sessions = [s for s in service.pendingRecoveries() if s.path == self.config["sessionDir"]]
        self.result["sessionListed"] = bool(sessions)
        rows = []
        if dialog is not None:
            for index in range(dialog.list.topLevelItemCount):
                item = dialog.list.topLevelItem(index)
                rows.append({"time": item.text(0), "contents": item.text(1), "size": item.text(2), "status": item.text(3), "thumbnail": not item.icon(0).isNull()})
        self.result["dialogRows"] = rows
        if not sessions:
            self.result.setdefault("failures", []).append("the crashed session is not listed")
            self.finish()
            return
        slot = sessions[0].recoverableSlots()[0]
        notifications = []
        recovery.notify = lambda text, report=None: notifications.append(text)
        start = time.perf_counter()
        report = recovery.recover(slot, mode="replace")
        self.result["recoverSeconds"] = time.perf_counter() - start
        self.result["report"] = report
        self.result["notification"] = notifications
        if dialog is not None:
            dialog.close()
        scene = slicer.mrmlScene
        self.result["content"] = _sceneContent()
        root = os.path.realpath(service.rootDirectory())
        insideRoot = []
        storage = {}
        for node in slicer.util.getNodesByClass("vtkMRMLStorableNode"):
            if not node.GetSaveWithScene():
                continue
            storageNode = node.GetStorageNode()
            fileName = storageNode.GetFileName() if storageNode is not None else None
            storage[node.GetName()] = {"file": fileName, "modified": bool(node.GetModifiedSinceRead())}
            if fileName and os.path.realpath(fileName).startswith(root + os.sep):
                insideRoot.append(fileName)
        self.result["storage"] = storage
        self.result["storageInsideAutosave"] = insideRoot
        self.result["sceneUrl"] = scene.GetURL()
        self.result["storableModified"] = bool(scene.GetStorableNodesModifiedSinceRead())
        autosave_io.backgroundWriter().drain(10.0)
        self.result["sessionState"] = (autosave_io.readJson(os.path.join(self.config["sessionDir"], autosave_io.SESSION_FILE)) or {}).get("state")
        self.result["messages"] = self.capture.entries()
        self.finish()


    def openProject(self):
        import qt

        import slicer
        from StudioLib import autosave

        card = slicer.util.mainWindow().findChild(qt.QWidget, "StudioProjectCrashCard")
        self.result["crashCard"] = card is not None
        self.result["crashCardText"] = [label.text for label in card.findChildren(qt.QLabel)] if card is not None else []
        buttons = [button for button in card.findChildren(qt.QPushButton) if button.objectName == "StudioProjectCrashCardOpenButton"] if card else []
        if not buttons:
            self.result.setdefault("failures", []).append("the crash card has no Open project button")
            self.finish()
            return
        start = time.perf_counter()
        buttons[0].click()
        for _ in range(20):
            slicer.app.processEvents()
        self.result["openSeconds"] = time.perf_counter() - start
        service = autosave.instance()
        scene = slicer.mrmlScene
        self.result["projectOpen"] = service.project.path if service.project is not None else None
        self.result["content"] = _sceneContent()
        outside = []
        for node in slicer.util.getNodesByClass("vtkMRMLStorableNode"):
            if not node.GetSaveWithScene() or node.GetHideFromEditors():
                continue
            storageNode = node.GetStorageNode()
            fileName = storageNode.GetFileName() if storageNode is not None else None
            if fileName and service.project is not None and not os.path.realpath(fileName).startswith(os.path.realpath(service.project.path) + os.sep):
                outside.append(fileName)
        self.result["storageOutsideProject"] = outside
        self.result["sceneUrl"] = scene.GetURL()
        self.result["storableModified"] = bool(scene.GetStorableNodesModifiedSinceRead())
        self.result["messages"] = self.capture.entries()
        self.finish()


class _PhaseC(_Phase):
    """Load the autosaved scene directly (any Slicer version) and record its content."""

    def run(self):
        import slicer

        capture = _LogCapture()
        try:
            slicer.util.loadScene(self.config["scenePath"])
            self.result["loaded"] = True
        except Exception:
            self.result["loaded"] = False
            self.result["loadException"] = traceback.format_exc()
        for _ in range(5):
            slicer.app.processEvents()
        self.result["version"] = slicer.app.applicationVersion
        self.result["content"] = _sceneContent()
        layoutManager = slicer.app.layoutManager()
        self.result["layout"] = layoutManager.layout
        shown = []
        for name in layoutManager.sliceViewNames():
            widget = layoutManager.sliceWidget(name)
            if widget is not None and widget.isVisibleTo(slicer.util.mainWindow()) and widget.width > 1:
                shown.append(name)
        self.result["sliceViewsShown"] = sorted(shown)
        self.result["messages"] = capture.entries()
        self.finish()


# =============================================================================
# Harness (outside Slicer)
# =============================================================================


def _generatedScript(configPath):
    return (
        "# Generated by StudioAutosaveCrashCheck.py\n"
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('StudioAutosaveCrashCheck', {os.path.abspath(__file__)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"module.runInSlicer({configPath!r})\n"
    )


def _runSlicer(slicerPath, name, config, home, args, official=False):
    runDir = os.path.join(args.out, "runs", name)
    shutil.rmtree(runDir, ignore_errors=True)
    os.makedirs(runDir)
    config = dict(config, resultPath=os.path.join(runDir, "result.json"), outDir=args.out)
    configPath = os.path.join(runDir, "config.json")
    with open(configPath, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=1)
    scriptPath = os.path.join(runDir, "script.py")
    with open(scriptPath, "w", encoding="utf-8") as f:
        f.write(_generatedScript(configPath))
    command = [slicerPath, "--no-splash", "--ignore-slicerrc", "--python-script", scriptPath]
    if not args.no_xvfb:
        command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", *command]
    environment = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, ".config"), XDG_CACHE_HOME=os.path.join(home, ".cache"),
                       XDG_DATA_HOME=os.path.join(home, ".local", "share"))
    environment.pop("SLICER_STUDIO_AUTOSAVE", None)
    environment.pop("SLICER_STUDIO_AUTOSAVE_DIR", None)
    # Projects (A-09): a throwaway Slicer Studio folder in this HOME (no folder chooser); without --projects, projects are
    # off and the private recovery autosave of A-01 is checked
    environment["SLICER_STUDIO_PROJECTS_ROOT"] = os.path.join(home, "Slicer Studio")
    if config.get("projects"):
        environment.pop("SLICER_STUDIO_PROJECTS", None)
    else:
        environment["SLICER_STUDIO_PROJECTS"] = "0"
    if official:
        # Official 5.12.4 fails on non-ASCII paths under the POSIX locale (A-04 E9)
        environment.update(LANG="C.UTF-8", LC_ALL="C.UTF-8")
    print(f"[{time.strftime('%H:%M:%S')}] {name}: {slicerPath}", flush=True)
    start = time.time()
    with open(os.path.join(runDir, "slicer.log"), "w", encoding="utf-8") as log:
        try:
            returncode = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout, check=False).returncode
        except subprocess.TimeoutExpired:
            returncode = "timeout"
    result = {}
    if os.path.exists(config["resultPath"]):
        with open(config["resultPath"], encoding="utf-8") as f:
            result = json.load(f)
    else:
        result["exception"] = f"no result (exit code {returncode}); see {os.path.join(runDir, 'slicer.log')}"
    result["returncode"] = returncode
    result["seconds"] = round(time.time() - start, 1)
    return result


def _isNoise(message):
    return any(word in message["text"] for word in ENVIRONMENT_NOISE)


def _compareContent(expected, actual, label):
    problems = []
    for key in ("MR Head", "Template", "T map", "segments", "Peaks", "Stats", "LUT", "nodeCounts"):
        if expected.get(key) != actual.get(key):
            detail = ""
            if isinstance(expected.get(key), dict) and isinstance(actual.get(key), dict):
                differing = sorted(k for k in set(expected[key]) | set(actual[key]) if expected[key].get(k) != actual[key].get(k))
                detail = f" ({', '.join(f'{k}: {expected[key].get(k)} != {actual[key].get(k)}' for k in differing[:5])})"
            problems.append(f"{label}: {key} differs{detail}")
    return problems


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--slicer", required=True, help="the fork's Slicer launcher")
    parser.add_argument("--checker", action="append", default=[], metavar="NAME=PATH", help="a Slicer that loads the autosave directly (repeatable)")
    parser.add_argument("--out", required=True, help="output directory (replaced)")
    parser.add_argument("--timeout", type=int, default=900, help="time limit of each Slicer process in seconds")
    parser.add_argument("--studio", action="store_true", help="expect the Studio recovery card (A-01b) instead of the dialog")
    parser.add_argument("--projects", action="store_true", help="check a project of the Slicer Studio folder (A-09) instead of the private autosave")
    parser.add_argument("--no-xvfb", action="store_true", help="do not run Slicer in xvfb-run")
    parser.add_argument("--mrhead", help="volume file used as 'MR Head' (default: a synthetic volume)")
    args = parser.parse_args(argv)
    args.out = os.path.abspath(args.out)
    shutil.rmtree(args.out, ignore_errors=True)
    os.makedirs(args.out)
    checkers = []
    for item in args.checker:
        name, separator, path = item.partition("=")
        if not separator or not os.path.exists(path):
            parser.error(f"--checker expects NAME=PATH of an existing launcher, got '{item}'")
        checkers.append((name, os.path.abspath(path)))

    report = {"slicer": args.slicer, "phases": {}, "problems": []}
    problems = report["problems"]
    home = os.path.join(args.out, "home")
    os.makedirs(home)

    if args.studio:
        probe = subprocess.run([args.slicer, "--no-splash", "--no-main-window", "--testing", "--python-code",
                                "import importlib.util,slicer; slicer.app.exit(0 if importlib.util.find_spec('StudioLib.autosave_ui') else 3)"],
                               env=dict(os.environ, HOME=os.path.join(args.out, "probe-home")), capture_output=True, timeout=args.timeout, check=False)
        if probe.returncode != 0:
            problems.append("A-01b not present (StudioLib.autosave_ui cannot be imported): --studio cannot be checked")

    if args.projects:
        return _mainProjects(args, checkers, report, problems, home)
    phaseA = _runSlicer(args.slicer, "A-autosave-and-crash", {"phase": "A", "mrhead": args.mrhead}, home, args)
    report["phases"]["A"] = phaseA
    if "exception" in phaseA or not phaseA.get("crashed"):
        problems.append("phase A failed: " + (phaseA.get("exception") or "no crash").strip().splitlines()[-1])
    for failure in phaseA.get("failures", []):
        problems.append(f"phase A: {failure}")
    expected = phaseA.get("expected") or {}
    full = phaseA.get("fullGenerations") or []
    generations = phaseA.get("generations") or []
    incremental = generations[1:]
    metrics = {
        "fullMainThreadTotalMsMedian": _median([g.get("mainThreadTotalMs") for g in full]),
        "fullMaxSliceMs": max([g.get("maxSliceMs", 0) for g in full] or [None]),
        "lesionOnlyMainThreadTotalMsMedian": _median([g.get("mainThreadTotalMs") for g in incremental]),
        "lesionOnlyMaxSliceMs": max([g.get("maxSliceMs", 0) for g in incremental] or [None]),
        "serviceMaxSliceMs": max([g.get("maxSliceMs", 0) for g in generations] or [None]),
        "pollMsMedian": _median(phaseA.get("pollMs") or []),
        "thumbnailMsMedian": _median(phaseA.get("thumbnailMs") or []),
        "nodeCount": phaseA.get("nodeCount"),
        "firstGeneration": generations[0] if generations else None,
    }
    report["metrics"] = metrics
    if phaseA.get("crashed"):
        if metrics["fullMainThreadTotalMsMedian"] is not None and metrics["fullMainThreadTotalMsMedian"] > 30:
            problems.append(f"full generation main-thread median {metrics['fullMainThreadTotalMsMedian']} ms > 30 ms")
        if metrics["lesionOnlyMainThreadTotalMsMedian"] is not None and metrics["lesionOnlyMainThreadTotalMsMedian"] > 25:
            problems.append(f"lesion-only generation main-thread median {metrics['lesionOnlyMainThreadTotalMsMedian']} ms > 25 ms")
        for key in ("fullMaxSliceMs", "serviceMaxSliceMs"):
            if metrics[key] is not None and metrics[key] > 100:
                problems.append(f"{key} {metrics[key]} ms > 100 ms")
        if metrics["pollMsMedian"] is not None and metrics["pollMsMedian"] > 2:
            problems.append(f"poll {metrics['pollMsMedian']} ms > 2 ms")
        kinds = phaseA.get("kinds") or {}
        if kinds.get("MR Head") != "reference":
            problems.append(f"MR Head is autosaved as {kinds.get('MR Head')}, expected a reference to the user file")
        if kinds.get("Template") != "copy":
            problems.append(f"the cached template is autosaved as {kinds.get('Template')}, expected a copy")
        if not phaseA.get("sceneModified") or not phaseA.get("storableModified"):
            problems.append("the live scene lost its unsaved state after autosaving")

    if phaseA.get("crashed"):
        phaseB = _runSlicer(args.slicer, "B-recover", {"phase": "B", "studio": args.studio, "sessionDir": phaseA.get("sessionDir")}, home, args)
        report["phases"]["B"] = phaseB
        if "exception" in phaseB:
            problems.append("phase B failed: " + phaseB["exception"].strip().splitlines()[-1])
        for failure in phaseB.get("failures", []):
            problems.append(f"phase B: {failure}")
        if args.studio:
            if not phaseB.get("recoveryCard"):
                problems.append("phase B: StudioRecoveryCard is not shown")
            if phaseB.get("dialogVisible"):
                problems.append("phase B: StudioRecoveryDialog is shown in Studio")
        elif not phaseB.get("dialogVisible"):
            problems.append("phase B: StudioRecoveryDialog is not shown")
        if not phaseB.get("sessionListed"):
            problems.append("phase B: the crashed session is not listed")
        rows = phaseB.get("dialogRows") or []
        if not args.studio and not any(row.get("time") and row.get("contents") and row.get("size") for row in rows):
            problems.append("phase B: the dialog does not show time, contents and size")
        if not args.studio and not any(row.get("thumbnail") for row in rows):
            problems.append("phase B: the dialog shows no thumbnail")
        if phaseB.get("content"):
            problems += _compareContent(expected, phaseB["content"], "recovered scene")
        if phaseB.get("storageInsideAutosave"):
            problems.append(f"phase B: storage nodes point into the autosave folder: {phaseB['storageInsideAutosave'][:3]}")
        storage = phaseB.get("storage") or {}
        if storage:
            userFile = os.path.join(home, "user data", "MR Head.nrrd")
            if (storage.get("MR Head") or {}).get("file") != userFile:
                problems.append(f"phase B: MR Head is not on its original file ({(storage.get('MR Head') or {}).get('file')})")
            for name in ("Lesions", "T map", "Peaks", "Stats", "LNM Diverging"):
                info = storage.get(name) or {}
                if info.get("file") or not info.get("modified"):
                    problems.append(f"phase B: {name} should have no file and be modified ({info})")
        if phaseB.get("sceneUrl"):
            problems.append(f"phase B: the scene URL is {phaseB.get('sceneUrl')}, expected none (never saved)")
        if "storableModified" in phaseB and not phaseB["storableModified"]:
            problems.append("phase B: the recovered scene has no unsaved changes")
        if phaseB.get("sessionState") not in ("recovered", None):
            problems.append(f"phase B: the session state is {phaseB.get('sessionState')}, expected recovered")
        errors = [m for m in phaseB.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
        problems += [f"phase B: log {m['level']}: {m['text'][:200]}" for m in errors]

        scenePath = os.path.join(phaseA.get("slotDir") or "", "Scene.mrml")
        report["phases"]["C"] = {}
        for name, path in [("fork", args.slicer)] + checkers:
            checkerHome = os.path.join(args.out, f"home-{name}")
            os.makedirs(checkerHome, exist_ok=True)
            result = _runSlicer(path, f"C-{name}", {"phase": "C", "scenePath": scenePath}, checkerHome, args, official=(name != "fork"))
            report["phases"]["C"][name] = result
            if "exception" in result:
                problems.append(f"phase C {name}: " + result["exception"].strip().splitlines()[-1])
                continue
            if not result.get("loaded"):
                problems.append(f"phase C {name}: loading failed")
            problems += _compareContent(expected, result.get("content") or {}, f"phase C {name}")
            if sorted(result.get("sliceViewsShown") or []) != ["Green", "Red", "Yellow"]:
                problems.append(f"phase C {name}: slice views shown: {result.get('sliceViewsShown')}")
            errors = [m for m in result.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
            problems += [f"phase C {name}: log {m['level']}: {m['text'][:200]}" for m in errors]

    report["passed"] = not problems
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True, default=str)
    text = _formatReport(report)
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0 if report["passed"] else 1


def _mainProjects(args, checkers, report, problems, home):
    """--projects: a project saved continuously, a crash, the crash offer, and the project in official Slicer."""
    phaseA = _runSlicer(args.slicer, "A-project-and-crash", {"phase": "A", "mrhead": args.mrhead, "projects": True}, home, args)
    report["phases"]["A"] = phaseA
    if "exception" in phaseA or not phaseA.get("crashed"):
        problems.append("phase A failed: " + (phaseA.get("exception") or "no crash").strip().splitlines()[-1])
    problems += [f"phase A: {failure}" for failure in phaseA.get("failures", [])]
    expected = phaseA.get("expected") or {}
    generations = [g for g in (phaseA.get("generations") or []) if g]
    report["metrics"] = {
        "firstSaveMainThreadTotalMs": (phaseA.get("firstSaveMetrics") or {}).get("mainThreadTotalMs"),
        "lesionOnlyMainThreadTotalMsMedian": _median([g.get("mainThreadTotalMs") for g in generations]),
        "lesionOnlyMaxSliceMs": max([g.get("maxSliceMs", 0) for g in generations] or [None]),
        "sceneFileMsMedian": _median([g.get("sceneMs") for g in generations]),
        "pollMsMedian": _median(phaseA.get("pollMs") or []),
        "nodeCount": phaseA.get("nodeCount"),
    }
    metrics = report["metrics"]
    if phaseA.get("crashed"):
        if not phaseA.get("firstSave"):
            problems.append("phase A: the first save of the project failed")
        if phaseA.get("modifiedAfterSave"):
            problems.append("phase A: nodes are still 'modified since read' after saving the project")
        if metrics["lesionOnlyMaxSliceMs"] is not None and metrics["lesionOnlyMaxSliceMs"] > 100:
            problems.append(f"lesion-only save: a main-thread slice took {metrics['lesionOnlyMaxSliceMs']} ms > 100 ms")
        if metrics["pollMsMedian"] is not None and metrics["pollMsMedian"] > 2:
            problems.append(f"poll {metrics['pollMsMedian']} ms > 2 ms")
        projectPath = phaseA.get("projectPath")
        phaseB = _runSlicer(args.slicer, "B-crash-offer", {"phase": "B", "projects": True}, home, args)
        report["phases"]["B"] = phaseB
        if "exception" in phaseB:
            problems.append("phase B failed: " + phaseB["exception"].strip().splitlines()[-1])
        problems += [f"phase B: {failure}" for failure in phaseB.get("failures", [])]
        if not phaseB.get("crashCard"):
            problems.append("phase B: StudioProjectCrashCard is not shown")
        if os.path.normpath(phaseB.get("projectOpen") or "") != os.path.normpath(projectPath or "-"):
            problems.append(f"phase B: the project was not opened ({phaseB.get('projectOpen')})")
        if phaseB.get("content"):
            problems += _compareContent(expected, phaseB["content"], "reopened project")
        if phaseB.get("storageOutsideProject"):
            problems.append(f"phase B: files outside the project: {phaseB['storageOutsideProject'][:3]}")
        if phaseB.get("storableModified"):
            problems.append("phase B: the reopened project has unsaved changes")
        errors = [m for m in phaseB.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
        problems += [f"phase B: log {m['level']}: {m['text'][:200]}" for m in errors]
        scenePath = os.path.join(projectPath or "", os.path.basename(projectPath or "") + ".mrml")
        report["phases"]["C"] = {}
        for name, path in [("fork", args.slicer)] + checkers:
            checkerHome = os.path.join(args.out, f"home-{name}")
            os.makedirs(checkerHome, exist_ok=True)
            result = _runSlicer(path, f"C-{name}", {"phase": "C", "scenePath": scenePath}, checkerHome, args, official=(name != "fork"))
            report["phases"]["C"][name] = result
            if "exception" in result:
                problems.append(f"phase C {name}: " + result["exception"].strip().splitlines()[-1])
                continue
            if not result.get("loaded"):
                problems.append(f"phase C {name}: loading failed")
            problems += _compareContent(expected, result.get("content") or {}, f"phase C {name}")
            if sorted(result.get("sliceViewsShown") or []) != ["Green", "Red", "Yellow"]:
                problems.append(f"phase C {name}: slice views shown: {result.get('sliceViewsShown')}")
            errors = [m for m in result.get("messages", []) if m["level"] in ERROR_LEVELS and not _isNoise(m)]
            problems += [f"phase C {name}: log {m['level']}: {m['text'][:200]}" for m in errors]
    report["passed"] = not problems
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1, sort_keys=True, default=str)
    lines = ["Slicer Studio project crash check (A-09)", "=" * 40, ""]
    lines.append(f"Phase A (project saved, then SIGKILL): {'ok' if phaseA.get('crashed') else 'FAILED'}  project {phaseA.get('projectPath')}")
    lines += [f"  {key}: {value}" for key, value in metrics.items()]
    phaseB = report["phases"].get("B") or {}
    lines.append(f"Phase B: crash card {phaseB.get('crashCard')} {phaseB.get('crashCardText')}, opened {phaseB.get('projectOpen')}")
    for name, result in (report["phases"].get("C") or {}).items():
        lines.append(f"Phase C {name}: version {result.get('version')}, loaded {result.get('loaded')}, slice views {result.get('sliceViewsShown')}")
    lines += ["", "Problems:"] + [f"  - {p}" for p in problems] + ["  (none)" if not problems else ""]
    lines += [f"Result: {'PASS' if report['passed'] else 'FAIL'}", ""]
    text = "\n".join(lines)
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 0 if report["passed"] else 1


def _formatReport(report):
    lines = ["Slicer Studio autosave crash check", "=" * 34, ""]
    phaseA = report["phases"].get("A", {})
    lines.append(f"Phase A (autosave, then SIGKILL): {'ok' if phaseA.get('crashed') else 'FAILED'}  ({phaseA.get('seconds')} s, exit {phaseA.get('returncode')})")
    lines.append(f"  session: {phaseA.get('sessionDir')}")
    lines.append(f"  kinds: {phaseA.get('kinds')}")
    lines.append(f"  close prompt line: {phaseA.get('unsavedSummary')}")
    metrics = report.get("metrics", {})
    lines.append("  metrics:")
    for key in ("fullMainThreadTotalMsMedian", "fullMaxSliceMs", "lesionOnlyMainThreadTotalMsMedian", "lesionOnlyMaxSliceMs", "serviceMaxSliceMs", "pollMsMedian",
                "thumbnailMsMedian", "nodeCount"):
        lines.append(f"    {key}: {metrics.get(key)}")
    phaseB = report["phases"].get("B")
    if phaseB is not None:
        lines += ["", f"Phase B (next launch, recover): {phaseB.get('seconds')} s"]
        lines.append(f"  dialog visible: {phaseB.get('dialogVisible')}, card: {phaseB.get('recoveryCard')}, session listed: {phaseB.get('sessionListed')}")
        for row in phaseB.get("dialogRows") or []:
            lines.append(f"  row: {row}")
        lines.append(f"  recover report: {phaseB.get('report')}")
        lines.append(f"  notification: {phaseB.get('notification')}")
    for name, result in (report["phases"].get("C") or {}).items():
        lines.append(f"Phase C {name}: version {result.get('version')}, loaded {result.get('loaded')}, layout {result.get('layout')}, "
                     f"slice views {result.get('sliceViewsShown')}, log entries {len(result.get('messages') or [])}")
        for message in (result.get("messages") or [])[:5]:
            lines.append(f"    {message['level']}: {message['text'][:160]}")
    lines += ["", "Problems:"] + [f"  - {p}" for p in report["problems"]] + ["  (none)" if not report["problems"] else ""]
    lines += [f"Result: {'PASS' if report['passed'] else 'FAIL'}", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
