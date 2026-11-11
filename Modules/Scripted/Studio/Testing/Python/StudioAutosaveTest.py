"""Scene autosave and crash recovery of Slicer Studio (StudioLib/autosave.py, autosave_io.py, recovery.py; A-01).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioAutosaveTest'); slicer.app.exit(0)"

Autosave never runs by itself under --testing; the tests create services with
``AutosaveService(force=True, root=<temporary folder 'Auto save tést'>)`` (spaces and non-ASCII characters)
and dispose them. They cover:

- (a) zero side effects of a generation on the live scene (node count, storage files, URL, modified state);
- (b) the classification: referenced user files, copied cache files, written new data;
- (c) incremental generations, and no generation when nothing changed;
- (d) the round trip of volumes, segments, markups, tables and color tables (with alpha);
- (e) recover(): no storage node in the autosave folder, original files, modified state and URL restored;
  a referenced original file that is gone does not stop the re-linking (the session is recovered and the file
  reported); another load error fails the recovery, with no storage node left in the autosave folder;
- (f) a modified 512^3 volume: chunked snapshot and background write without freezing the main thread;
- (g) scan(): a session locked by a live process, and a crashed session;
- (h) retention; (i) a scene closed without saving keeps a closed-unsaved slot;
- (j) writeScalarNrrd for every supported type and an oblique grid; (k) loading an autosave directly;
- (l) nothing exists under --testing without force; (m) unsavedSummary(), thumbnails without network maps and
  no file written in a study folder; (n) the Search provider entries (with a stand-in module without the palette);
- (o) heavy nodes keep their previous autosave between their writes; data above MaxNodeMB is listed as not autosaved;
- (p) a generation aborted while finalize() runs keeps the folders that the committed Scene.mrml references;
- (q) a tensor (DTI) volume is autosaved, and a node whose job fails does not stop the autosave of the others;
- (r) recovering the oldest closed slot of the session keeps that slot while it is recovered;
- (s) a generation starts IdleSeconds after the last change (poll timing).

Studio surfaces (StudioLib/autosave_ui.py, A-01b), with ``Shell(force=True).apply()`` and the test service as
``autosave.instance()``:

- (t) the top-bar status: hidden, "Autosaving…", then "Autosaved · just now"; the tooltip says autosave is not
  saving; the menu (recovery actions, Open project… and Saving settings… without a project, A-09); icon only at 1366 px and in compact mode; the switcher stays centred; the slot records
  the workspace; no new shortcuts and no key collision;
- (u) the recovery card in the Library notice slot (above the drop zone and the study page) and not the dialog; the
  inspector notice in Draw and its Show; Recover closes the card and returns to the recorded workspace; Later;
- (v) Classic removes the status, the card and the notice and gives back the default presenter; Studio again shows
  them; no visible string says "Saved".
- (x) A-09: while the scene is a project it is saved there and no private session is written; when the project is
  released (a study opens) the private recovery autosave runs again.
- (y) a node removed after the autosave and project helpers listed it is freed (no leaked GetNodesByClass collection).
- (za) Qt 6 (QAction without menu()): the File menu actions are installed, and a failing install step does not skip the
  later ones.
"""

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np
import qt
import vtk

import slicer


def _md5(array):
    return hashlib.md5(np.ascontiguousarray(array).tobytes()).hexdigest()


def _filesUnder(path):
    found = {}
    for directory, _, fileNames in os.walk(path):
        for fileName in fileNames:
            filePath = os.path.join(directory, fileName)
            found[filePath] = os.stat(filePath).st_mtime_ns
    return found


class StudioAutosaveTest(unittest.TestCase):
    """Autosave engine and recovery. Needs the main window (layout, settings dialog)."""

    def setUp(self):
        from StudioLib import autosave, autosave_io

        self.autosave = autosave
        self.io = autosave_io
        slicer.mrmlScene.Clear(0)
        self.tempDir = tempfile.mkdtemp(prefix="StudioAutosaveTest-")
        self.root = os.path.join(self.tempDir, "Auto save tést")
        self.userDir = os.path.join(self.tempDir, "user data é")
        os.makedirs(self.userDir)
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.dispose()
        self.io.backgroundWriter().drain(30.0)
        slicer.mrmlScene.Clear(0)
        shutil.rmtree(self.tempDir, ignore_errors=True)

    #
    # Helpers
    #

    def _service(self):
        service = self.autosave.AutosaveService(force=True, root=self.root)
        # The test folders are in the system temporary folder: treat them as user folders
        service.writerOptionsOverride = {"systemTemporaryIsVolatile": False}
        self.services.append(service)
        return service

    def _generate(self, service, expectStarted=True):
        started = service.autosaveNow("test")
        self.assertEqual(started, expectStarted)
        self.assertTrue(service.waitForIdle(120.0))
        return started

    def _userVolume(self, name="MR Head", shape=(40, 50, 60)):
        k, j, i = np.indices(shape)
        voxels = ((i * 7 + j * 13 + k * 29) % 500).astype(np.int16)
        node = slicer.util.addVolumeFromArray(voxels, name=name)
        node.SetSpacing(1.0, 1.1, 1.2)
        node.SetOrigin(-20.0, 10.0, 5.0)
        path = os.path.join(self.userDir, f"{name}.nrrd")
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        return slicer.util.loadVolume(path)

    def _cachedVolume(self):
        os.makedirs(slicer.app.cachePath, exist_ok=True)
        node = slicer.util.addVolumeFromArray(np.arange(8 * 9 * 10, dtype=np.uint8).reshape(8, 9, 10), name="Template")
        path = os.path.join(slicer.app.cachePath, "StudioAutosaveTest-template.nrrd")
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        template = slicer.util.loadVolume(path)
        template.SetName("Template")
        return template

    def _buildScene(self):
        scene = slicer.mrmlScene
        volume = self._userVolume()
        template = self._cachedVolume()
        segmentation = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        shape = slicer.util.arrayFromVolume(volume).shape
        k, j, i = np.indices(shape)
        lesion = (((k - 20) ** 2 + (j - 25) ** 2 + (i - 30) ** 2) <= 36).astype(np.uint8)
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        tvalues = np.random.default_rng(1).normal(0, 3, size=(12, 14, 16)).astype(np.float32)
        tmap = slicer.util.addVolumeFromArray(tvalues, name="T map")
        lut = scene.AddNewNodeByClass("vtkMRMLColorTableNode", "LNM Diverging")
        lut.SetTypeToUser()
        lut.SetNumberOfColors(16)
        for index in range(16):
            lut.SetColor(index, f"c{index}", index / 15.0, 0.25, 1 - index / 15.0, round(abs(index - 7.5) / 7.5 * 255) / 255.0)
        tmap.CreateDefaultDisplayNodes()
        tmap.GetDisplayNode().SetAndObserveColorNodeID(lut.GetID())
        points = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Peaks")
        for position in ((10, 20, 30), (-5.5, 7.25, 1.0)):
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
        return {"volume": volume, "template": template, "segmentation": segmentation, "segmentId": segmentId, "tmap": tmap,
                "lut": lut, "points": points, "table": table, "lesion": lesion}

    def _liveState(self):
        scene = slicer.mrmlScene
        storable = {}
        for node in slicer.util.getNodesByClass("vtkMRMLStorableNode"):
            storageNode = node.GetStorageNode()
            storable[node.GetID()] = (bool(node.GetModifiedSinceRead()), storageNode.GetFileName() if storageNode else None)
        return {
            "count": scene.GetNumberOfNodes(),
            "url": scene.GetURL(),
            "root": scene.GetRootDirectory(),
            "storedTime": scene.GetStoredTime(),
            "modified": bool(scene.GetModifiedSinceRead()),
            "storableModified": bool(scene.GetStorableNodesModifiedSinceRead()),
            "storable": storable,
        }

    def _expected(self, built):
        volume = built["volume"]
        lut = built["lut"]
        return {
            "MR Head": _md5(slicer.util.arrayFromVolume(volume)),
            "Template": _md5(slicer.util.arrayFromVolume(built["template"])),
            "T map": _md5(slicer.util.arrayFromVolume(built["tmap"])),
            "lesion": _md5(slicer.util.arrayFromSegmentBinaryLabelmap(built["segmentation"], built["segmentId"], volume)),
            "segmentId": built["segmentId"],
            "points": slicer.util.arrayFromMarkupsControlPoints(built["points"]).round(4).tolist(),
            "table": [[built["table"].GetCellText(r, c) for c in range(2)] for r in range(3)],
            "lut": [list(lut.GetLookupTable().GetTableValue(i)) for i in range(16)],
        }

    def _checkLoaded(self, expected):
        def node(name, className):
            nodes = slicer.mrmlScene.GetNodesByClassByName(className, name)
            return nodes.GetItemAsObject(0) if nodes.GetNumberOfItems() else None

        volume = node("MR Head", "vtkMRMLScalarVolumeNode")
        self.assertIsNotNone(volume)
        for name in ("MR Head", "Template", "T map"):
            loaded = node(name, "vtkMRMLScalarVolumeNode")
            self.assertIsNotNone(loaded, name)
            self.assertEqual(_md5(slicer.util.arrayFromVolume(loaded)), expected[name], name)
        segmentation = node("Lesions", "vtkMRMLSegmentationNode")
        self.assertIsNotNone(segmentation)
        # Looked up by ID: AddEmptySegment('Lesion') names the segment 'Segment_1'
        self.assertIsNotNone(segmentation.GetSegmentation().GetSegment(expected["segmentId"]))
        self.assertEqual(_md5(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, expected["segmentId"], volume)), expected["lesion"])
        points = node("Peaks", "vtkMRMLMarkupsFiducialNode")
        self.assertEqual(slicer.util.arrayFromMarkupsControlPoints(points).round(4).tolist(), expected["points"])
        table = node("Stats", "vtkMRMLTableNode")
        self.assertEqual([[table.GetCellText(r, c) for c in range(2)] for r in range(3)], expected["table"])
        lut = node("LNM Diverging", "vtkMRMLColorTableNode")
        self.assertTrue(np.allclose([list(lut.GetLookupTable().GetTableValue(i)) for i in range(16)], expected["lut"], atol=1.0 / 255))

    def _storageFilesUnder(self, root):
        canonical = os.path.realpath(root)
        found = []
        for storageNode in slicer.util.getNodesByClass("vtkMRMLStorageNode"):
            fileName = storageNode.GetFileName()
            if fileName and os.path.realpath(fileName).startswith(canonical + os.sep):
                found.append(fileName)
        return found

    #
    # Tests
    #

    def test_a_noSideEffects(self):
        built = self._buildScene()
        service = self._service()
        before = self._liveState()
        self.assertTrue(before["modified"])
        self._generate(service)
        after = self._liveState()
        self.assertEqual(before, after)
        self.assertTrue(self.io.commitDoesNotMarkStored(), "the vtkMRMLScene::Commit fix is built")
        self.assertEqual(self.io.sentinelIds(), ())
        self.assertTrue(slicer.mrmlScene.GetModifiedSinceRead())
        self.assertTrue(built["segmentation"].GetModifiedSinceRead())
        self.assertIsNone(built["segmentation"].GetStorageNode())
        if os.name == "posix":
            self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertTrue(os.path.exists(os.path.join(self.root, self.io.README_FILE)))

    def test_b_classification(self):
        self._buildScene()
        service = self._service()
        self._generate(service)
        kinds = {slicer.mrmlScene.GetNodeByID(nodeId).GetName(): entry["kind"] for nodeId, entry in service.writer.entries.items()}
        self.assertEqual(kinds["MR Head"], "reference")
        self.assertEqual(kinds["Template"], "copy")
        for name in ("Lesions", "T map", "LNM Diverging", "Peaks", "Stats"):
            self.assertEqual(kinds[name], "write", name)
        sceneText = open(os.path.join(service.writer.slotDir, self.io.SCENE_FILE), encoding="utf-8").read()
        self.assertIn(self.io.urlEncode(os.path.join(self.userDir, "MR Head.nrrd")), sceneText)
        entry = next(e for e in service.writer.entries.values() if e["name"] == "Lesions")
        self.assertTrue(entry["dir"].startswith("data/vtkMRMLSegmentationNode"))
        self.assertTrue(entry["file"].endswith(".seg.nrrd"))

    def test_c_incremental(self):
        built = self._buildScene()
        service = self._service()
        self._generate(service)
        slotDir = service.writer.slotDir
        dataBefore = _filesUnder(os.path.join(slotDir, "data"))
        # Nothing changed: no generation, no file touched
        self._generate(service, expectStarted=False)
        self.assertEqual(_filesUnder(os.path.join(slotDir, "data")), dataBefore)
        lesion = built["lesion"].copy()
        lesion[20, 25, 37] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
        self._generate(service)
        dataAfter = _filesUnder(os.path.join(slotDir, "data"))
        changed = sorted({os.path.relpath(p, slotDir).split(os.sep)[1] for p in set(dataBefore) ^ set(dataAfter)})
        self.assertEqual(len(changed), 2, changed)  # the old and the new directory of the segmentation
        self.assertTrue(all(name.startswith("vtkMRMLSegmentationNode") for name in changed), changed)
        unchanged = {p: t for p, t in dataBefore.items() if "vtkMRMLSegmentationNode" not in p}
        self.assertEqual({p: dataAfter.get(p) for p in unchanged}, unchanged)
        self.assertEqual(service.metrics["last"]["nodesWritten"], 1)

    def test_d_roundTrip(self):
        built = self._buildScene()
        expected = self._expected(built)
        service = self._service()
        self._generate(service)
        scenePath = os.path.join(service.writer.slotDir, self.io.SCENE_FILE)
        service.dispose()  # loading the autosave directly is tested in (k)
        slicer.mrmlScene.Clear(0)
        slicer.util.loadScene(scenePath)
        self._checkLoaded(expected)

    def test_e_recover(self):
        from StudioLib import recovery

        built = self._buildScene()
        expected = self._expected(built)
        scene = slicer.mrmlScene
        scene.SetURL(os.path.join(self.userDir, "My scene.mrml"))
        scene.SetRootDirectory(self.userDir)
        service = self._service()
        self._generate(service)
        sessions = recovery.scan(self.root, None)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].status, "running")
        slot = sessions[0].slots[0]
        self.assertTrue(slot.unsaved)
        scene.Clear(0)
        scene.SetURL("")
        report = recovery.recover(slot, mode="replace")
        self.assertIsNotNone(report)
        self._checkLoaded(expected)
        self.assertEqual(self._storageFilesUnder(self.root), [])
        self.assertEqual(scene.GetURL(), os.path.join(self.userDir, "My scene.mrml"))
        self.assertEqual(os.path.realpath(scene.GetRootDirectory()), os.path.realpath(self.userDir))
        volume = slicer.util.getNode("MR Head")
        self.assertEqual(volume.GetStorageNode().GetFileName(), os.path.join(self.userDir, "MR Head.nrrd"))
        self.assertFalse(volume.GetModifiedSinceRead())
        template = slicer.util.getNode("Template")
        self.assertEqual(os.path.realpath(template.GetStorageNode().GetFileName()), os.path.realpath(os.path.join(slicer.app.cachePath, "StudioAutosaveTest-template.nrrd")))
        for name in ("Lesions", "T map", "Peaks", "Stats", "LNM Diverging"):
            node = slicer.util.getNode(name)
            self.assertTrue(node.GetModifiedSinceRead(), name)
            self.assertIsNone(node.GetStorageNode(), name)
        self.assertTrue(scene.GetStorableNodesModifiedSinceRead())

    def test_e3_recoverWithMissingOriginal(self):
        """BB-1: a referenced original file moved or deleted after the autosave failed the scene load, and recover()
        returned before re-linking: the lesion stayed on the autosave folder, the session stayed offered and Save
        proposed the autosave folder. Now the loaded nodes are re-linked, the missing file is reported and the session
        is recovered.
        """
        from StudioLib import recovery

        built = self._buildScene()
        lesionVoxels = int(built["lesion"].sum())
        scene = slicer.mrmlScene
        scene.SetURL(os.path.join(self.userDir, "My scene.mrml"))
        scene.SetRootDirectory(self.userDir)
        service = self._service()
        self._generate(service)
        scanPath = os.path.join(self.userDir, "MR Head.nrrd")
        os.makedirs(os.path.join(self.tempDir, "moved"))
        shutil.move(scanPath, os.path.join(self.tempDir, "moved", "MR Head.nrrd"))
        slot = recovery.scan(self.root, None)[0].slots[0]
        scene.Clear(0)
        scene.SetURL("")
        report = recovery.recover(slot, mode="replace")
        self.assertIsNotNone(report, "the recovery stopped at the missing original file")
        self.assertEqual([os.path.realpath(path) for path in report["missing"]], [os.path.realpath(scanPath)])
        self.assertEqual(self._storageFilesUnder(self.root), [])
        self.assertEqual(scene.GetURL(), os.path.join(self.userDir, "My scene.mrml"))
        segmentation = slicer.util.getNode("Lesions")
        self.assertIsNone(segmentation.GetStorageNode(), "Save must ask where to save the lesion")
        self.assertTrue(segmentation.GetModifiedSinceRead())
        self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, built["segmentId"]).sum()), lesionVoxels)
        self.assertEqual(os.path.realpath(slicer.util.getNode("MR Head").GetStorageNode().GetFileName()), os.path.realpath(scanPath))
        self.assertEqual(slot.session.status, "recovered")

    def test_e4_recoverLoadErrorKeepsSessionOffered(self):
        """A load error that missing original files do not explain (a damaged autosave file) still fails the recovery:
        the error is raised, the session is not marked recovered, and no storage node is left on the autosave folder.
        """
        from StudioLib import recovery

        self._buildScene()
        service = self._service()
        self._generate(service)
        slot = recovery.scan(self.root, None)[0].slots[0]
        entry = next(e for e in service.writer.entries.values() if e["name"] == "Lesions")
        with open(os.path.join(slot.path, entry["file"]), "wb") as f:
            f.write(b"not a nrrd file")
        slicer.mrmlScene.Clear(0)
        with self.assertRaises(RuntimeError):  # shown in an error box, outside testing
            recovery.recover(slot, mode="replace")
        self.assertEqual(self._storageFilesUnder(self.root), [])
        self.assertNotEqual(slot.session.status, "recovered")

    def _recentlyLoadedPaths(self):
        """The files of the official recently loaded list: the menu of the main window and the shared setting."""
        from StudioLib import library

        paths = []
        menu = library.recentlyLoadedMenu()
        if menu is not None:
            for action in menu.actions():
                properties = action.property("fileParameters")
                if properties:
                    paths.append(str(properties.get("fileName", "")))
        settings = qt.QSettings()
        count = settings.beginReadArray("RecentlyLoadedFiles/RecentFiles")
        for index in range(count):
            settings.setArrayIndex(index)
            properties = settings.value("file")
            if properties:
                paths.append(str(dict(properties).get("fileName", "")))
        settings.endArray()
        return [os.path.normcase(os.path.realpath(path)) for path in paths if path]

    def test_e5_recoverIsNotARecentFile(self):
        """BB-1: Recover loaded the private autosave scene like a file of the user: it was added to the official
        RecentlyLoadedFiles (shared with official 3D Slicer) and listed in Library > Opened from elsewhere, and it outlived
        its session. Recovering is not opening a file: nothing is recorded, and the IO manager signals are restored.
        """
        from StudioLib import library, recovery

        self._buildScene()
        service = self._service()
        self._generate(service)
        slot = recovery.scan(self.root, None)[0].slots[0]
        scenePath = os.path.normcase(os.path.realpath(slot.scenePath()))
        root = os.path.normcase(os.path.realpath(self.root))
        self.assertIsNotNone(library.recentlyLoadedMenu(), "the test needs the main window")
        settings = qt.QSettings()
        keepKey = "RecentlyLoadedFiles/NumberToKeep"
        previousKeep = settings.value(keepKey)
        settings.setValue(keepKey, 10)  # the main window records nothing without it (a new settings file)
        try:
            slicer.mrmlScene.Clear(0)
            ioManager = slicer.app.coreIOManager()
            self.assertFalse(ioManager.signalsBlocked())
            self.assertIsNotNone(recovery.recover(slot, mode="replace"))
            self.assertFalse(ioManager.signalsBlocked())
            self.assertNotIn(scenePath, self._recentlyLoadedPaths())
            self.assertEqual([path for path in self._recentlyLoadedPaths() if path.startswith(root + os.sep)], [])
            self.assertEqual([entry for entry in library.recentEntries(limit=50) if os.path.realpath(entry.path).startswith(root)], [])
            # Control: a scene that is loaded like a file of the user is recorded
            slicer.util.loadScene(slot.scenePath(), {"clear": True})
            self.assertIn(scenePath, self._recentlyLoadedPaths())
        finally:
            if previousKeep is None:
                settings.remove(keepKey)
            else:
                settings.setValue(keepKey, previousKeep)
            slicer.mrmlScene.Clear(0)

    def test_e6_recoverFailureRestoresIOSignals(self):
        from StudioLib import recovery

        self._buildScene()
        service = self._service()
        self._generate(service)
        slot = recovery.scan(self.root, None)[0].slots[0]
        entry = next(e for e in service.writer.entries.values() if e["name"] == "Lesions")
        with open(os.path.join(slot.path, entry["file"]), "wb") as f:
            f.write(b"not a nrrd file")
        slicer.mrmlScene.Clear(0)
        with self.assertRaises(RuntimeError):
            recovery.recover(slot, mode="replace")
        self.assertFalse(slicer.app.coreIOManager().signalsBlocked())

    def test_e7_autosaveFolderIsInternalForRecent(self):
        """Entries that an earlier version recorded are not listed in Library (the autosave folder is not a user folder)."""
        from StudioLib import library

        previous = os.environ.get("SLICER_STUDIO_AUTOSAVE_DIR")
        os.environ["SLICER_STUDIO_AUTOSAVE_DIR"] = self.root
        try:
            self.assertTrue(library.isInternalPath(os.path.join(self.root, "20260101-000000-1-1", "scene-1", "Scene.mrml")))
            self.assertFalse(library.isInternalPath(os.path.join(self.userDir, "My scene.mrml")))
        finally:
            if previous is None:
                os.environ.pop("SLICER_STUDIO_AUTOSAVE_DIR", None)
            else:
                os.environ["SLICER_STUDIO_AUTOSAVE_DIR"] = previous

    def test_e8_recoverRestoresWriteOptionsOfUserFiles(self):
        """BB-1: the autosave writes its private copies cropped and uncompressed, and Recover kept those options on the
        storage nodes of the user's files: a later Save rewrote lesion.seg.nrrd cropped to the lesion and scan.nrrd as raw
        NRRD. Now Recover puts back the options of the user's files (also for autosaves that did not record them).
        """
        from StudioLib import recovery

        def header(path):
            with open(path, "rb") as stream:
                text = stream.read(1 << 16).split(b"\n\n", 1)[0].decode("latin-1")
            return {line.split(":", 1)[0]: line.split(":", 1)[1].strip() for line in text.splitlines()[1:] if ":" in line and ":=" not in line}

        scene = slicer.mrmlScene
        volume = self._userVolume(name="scan", shape=(20, 24, 28))  # small: written by the C++ storage node (uncompressed)
        segmentation = scene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        lesion = np.zeros((20, 24, 28), dtype=np.uint8)
        lesion[8:12, 10:14, 12:16] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        segPath = os.path.join(self.userDir, "lesion.seg.nrrd")
        segmentation.AddDefaultStorageNode()
        segmentation.GetStorageNode().SetFileName(segPath)
        self.assertTrue(segmentation.GetStorageNode().WriteData(segmentation))
        scanPath = volume.GetStorageNode().GetFileName()
        before = {"seg": header(segPath)["sizes"], "scan": header(scanPath)["sizes"]}
        self.assertEqual(before["seg"], "28 24 20", "the user's lesion is on the full grid of the scan")
        self.assertEqual((header(segPath)["encoding"], header(scanPath)["encoding"]), ("gzip", "gzip"))

        for manifestOptions in (True, False):
            with self.subTest(manifestOptions=manifestOptions):
                scene.Clear(0)
                volume = slicer.util.loadVolume(scanPath)
                segmentation = slicer.util.loadSegmentation(segPath)
                self.assertFalse(segmentation.GetStorageNode().GetCropToMinimumExtent())
                # Edits: paint in the lesion, change the scan
                lesion[9:11, 11:13, 13:15] = 0
                slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentation.GetSegmentation().GetNthSegmentID(0), volume)
                voxels = slicer.util.arrayFromVolume(volume)
                voxels[0, 0, 0] += 1
                slicer.util.arrayFromVolumeModified(volume)
                service = self._service()
                self._generate(service)
                scenePath = os.path.join(service.writer.slotDir, self.io.SCENE_FILE)
                with open(scenePath, encoding="utf-8") as stream:
                    self.assertIn('CropToMinimumExtent="true"', stream.read(), "the private autosave copy is cropped")
                slot = recovery.scan(self.root, None)[0].slots[0]
                if not manifestOptions:
                    # An autosave of an earlier version: the manifest has no options of the original storage nodes
                    manifestPath = os.path.join(slot.path, self.io.MANIFEST_FILE)
                    manifest = self.io.readJson(manifestPath)
                    for entry in manifest["entries"].values():
                        self.assertIn("originalStorageOptions", entry)
                        entry.pop("originalStorageOptions")
                    self.io.atomicWriteText(manifestPath, json.dumps(manifest))
                service.dispose()
                scene.Clear(0)
                self.assertIsNotNone(recovery.recover(slot, mode="replace"))
                self.assertEqual(self._storageFilesUnder(self.root), [])
                segmentation = slicer.util.getNode("lesion")
                volume = slicer.util.getNode("scan")
                segStorage, scanStorage = segmentation.GetStorageNode(), volume.GetStorageNode()
                self.assertEqual(os.path.realpath(segStorage.GetFileName()), os.path.realpath(segPath))
                self.assertEqual(os.path.realpath(scanStorage.GetFileName()), os.path.realpath(scanPath))
                self.assertFalse(segStorage.GetCropToMinimumExtent(), "Recover kept the crop of the private autosave copy")
                self.assertTrue(segStorage.GetUseCompression())
                self.assertTrue(scanStorage.GetUseCompression(), "Recover kept the uncompressed writing of the private autosave copy")
                # File > Save writes the user's files as before the crash
                self.assertTrue(segStorage.WriteData(segmentation))
                self.assertTrue(scanStorage.WriteData(volume))
                self.assertEqual(header(segPath)["sizes"], before["seg"], "Save cropped the user's lesion file")
                self.assertEqual(header(scanPath)["sizes"], before["scan"])
                self.assertEqual((header(segPath)["encoding"], header(scanPath)["encoding"]), ("gzip", "gzip"))
                shutil.rmtree(self.root, ignore_errors=True)

    def test_e2_recoveredMessagePlural(self):
        from StudioLib import recovery

        one = recovery.recoveredText(1, "15:53")
        self.assertIn("Recovered 1 item from", one)
        self.assertNotIn("1 items", one)
        self.assertIn("Save to keep it.", one)
        many = recovery.recoveredText(3, "15:53")
        self.assertIn("Recovered 3 items from the autosave of 15:53", many)
        self.assertIn("Save to keep them.", many)

    def test_e3_classicRecoveredMessageHasNoDetailsWithoutDetails(self):
        """Classic's message box has a details section (and the padded title it needs) only when there are details."""
        from StudioLib import recovery

        calls = []
        realInfoDisplay = slicer.util.infoDisplay
        slicer.util.infoDisplay = lambda text, **kwargs: calls.append((text, kwargs))
        try:
            recovery._classicMessage("Recovered 2 items.", [])
            recovery._classicMessage("Recovered 2 items.", ["Changed on disk since the autosave: a.nrrd"])
            recovery._classicMessage("Recovered 2 items.", [], {"missing": [os.path.join(self.root, "b.nrrd")]})
            recovery._classicMessage("Recovered 2 items.", [], {"changed": [os.path.join(self.root, "c.nrrd")]})
        finally:
            slicer.util.infoDisplay = realInfoDisplay
        self.assertEqual(len(calls), 4)
        self.assertNotIn("detailedText", calls[0][1])
        self.assertTrue(calls[0][1]["windowTitle"].strip())
        self.assertIn("a.nrrd", calls[1][1]["detailedText"])
        self.assertNotIn("detailedText", calls[2][1])
        self.assertIn("b.nrrd", calls[2][0])
        self.assertIn("c.nrrd", calls[3][0])  # a scan changed since the autosave is named in the main text

    def test_f_largeVolume(self):
        scene = slicer.mrmlScene
        service = self._service()
        big = (np.random.default_rng(0).integers(0, 1000, size=(512, 512, 512)) // 7).astype(np.int16)
        node = slicer.util.addVolumeFromArray(big, name="Big CT")
        del big
        slicer.util.setSliceViewerLayers(background=node, fit=True)
        # Let the first renderings of the large volume finish (they block the main thread without autosave too): wait until
        # the event loop has been quiet (no step over 20 ms) for one second, at least 2 s and at most 15 s
        start = last = quietSince = time.perf_counter()
        while last - start < 15.0:
            slicer.app.processEvents()
            time.sleep(0.005)
            now = time.perf_counter()
            if now - last > 0.020:
                quietSince = now
            last = now
            if now - start >= 2.0 and now - quietSince >= 1.0:
                break
        intervals = []
        last = [time.perf_counter()]

        def tick():
            now = time.perf_counter()
            intervals.append((now - last[0]) * 1000.0)
            last[0] = now

        probe = qt.QTimer()
        probe.setInterval(10)
        probe.connect("timeout()", tick)
        start = time.perf_counter()
        probe.start()
        last[0] = time.perf_counter()
        self._generate(service)
        probe.stop()
        totalS = time.perf_counter() - start
        metrics = service.metrics["last"]
        maxInterval = max(intervals) if intervals else 0.0
        print(f"StudioAutosaveTest 512^3: total {totalS:.2f} s, maxSliceMs {metrics['maxSliceMs']:.1f}, "
              f"mainThreadMs {metrics['mainThreadMs']:.1f}, probe max interval {maxInterval:.1f} ms over {len(intervals)} ticks")
        self.assertLessEqual(metrics["maxSliceMs"], 100.0)
        self.assertLessEqual(maxInterval, 100.0)
        entry = service.writer.entries[node.GetID()]
        self.assertEqual(entry["kind"], "write")
        expected = _md5(slicer.util.arrayFromVolume(node))
        loaded = slicer.util.loadVolume(os.path.join(service.writer.slotDir, entry["file"]))
        self.assertEqual(_md5(slicer.util.arrayFromVolume(loaded)), expected)
        scene.RemoveNode(loaded)

    def test_g_scan(self):
        from StudioLib import recovery

        self._buildScene()
        service = self._service()
        self._generate(service)
        # A session folder whose lock file names a process that has exited, and that says "running"
        crashedDir = os.path.join(self.root, "20260101-120000-1-abcd")
        os.makedirs(os.path.join(crashedDir, "scene-1"))
        shutil.copy(os.path.join(service.writer.slotDir, self.io.SCENE_FILE), os.path.join(crashedDir, "scene-1", self.io.SCENE_FILE))
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        host = qt.QSysInfo.machineHostName()
        with open(os.path.join(crashedDir, self.io.LOCK_FILE), "w") as f:
            f.write(f"{process.pid}\nSlicer\n{host}\n")
        data = service._sessionJson()
        data.update(sessionId="20260101-120000-1-abcd", state="running", pid=process.pid)
        data["slots"] = [{"name": "scene-1", "state": "current", "unsaved": True, "summary": {"title": "Crashed"}}]
        self.io.atomicWriteJson(os.path.join(crashedDir, self.io.SESSION_FILE), data)
        sessions = {s.sessionId: s for s in recovery.scan(self.root, None)}
        self.assertEqual(sessions[service.sessionId].status, "running")
        self.assertEqual(sessions["20260101-120000-1-abcd"].status, "crashed")
        self.assertTrue(sessions["20260101-120000-1-abcd"].presentable())
        self.assertFalse(sessions[service.sessionId].presentable())
        # The dialog lists the crashed session at startup
        dialog = recovery.showRecoveryDialog([s for s in sessions.values() if s.presentable()], startup=True)
        try:
            self.assertEqual(dialog.objectName, "StudioRecoveryDialog")
            self.assertFalse(dialog.isModal())
            self.assertEqual(dialog.list.topLevelItemCount, 1)
            self.assertIn("Crashed", dialog.list.topLevelItem(0).text(1))
        finally:
            dialog.close()

    def test_g2_crashedSessionInReadOnlyFolderIsCrashedNotRunning(self):
        """QLockFile cannot delete the stale lock of a crashed session in a folder that cannot be written (it says locked)."""
        import types

        from StudioLib import recovery

        self._buildScene()
        service = self._service()
        self._generate(service)
        crashedDir = os.path.join(self.root, "20260101-120000-1-abcd")
        os.makedirs(os.path.join(crashedDir, "scene-1"))
        shutil.copy(os.path.join(service.writer.slotDir, self.io.SCENE_FILE), os.path.join(crashedDir, "scene-1", self.io.SCENE_FILE))
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        lockPath = os.path.join(crashedDir, self.io.LOCK_FILE)

        def writeLock(pid, host=None):
            with open(lockPath, "w") as f:
                f.write(f"{pid}\nSlicer\n{host or qt.QSysInfo.machineHostName()}\n")

        writeLock(process.pid)
        data = service._sessionJson()
        data.update(sessionId="20260101-120000-1-abcd", state="running", pid=process.pid)
        data["slots"] = [{"name": "scene-1", "state": "current", "unsaved": True, "summary": {"title": "Crashed"}}]
        self.io.atomicWriteJson(os.path.join(crashedDir, self.io.SESSION_FILE), data)

        class StuckLock:
            """A QLockFile that cannot take the lock because it cannot delete the stale lock file (no write permission)."""

            def __init__(self, path):
                pass

            def setStaleLockTime(self, ms):
                pass

            def tryLock(self, timeout=0):
                return False

        def sessionStatus():
            return {s.sessionId: s for s in recovery.scan(self.root, None)}["20260101-120000-1-abcd"]

        realQt = recovery.qt
        recovery.qt = types.SimpleNamespace(QLockFile=StuckLock, QSysInfo=qt.QSysInfo)
        try:
            session = sessionStatus()
            self.assertEqual(session.status, "crashed")
            self.assertTrue(session.presentable())
            self.assertEqual(recovery.lockState(crashedDir), "free")
            # A live holder, another machine and an unreadable lock stay locked
            writeLock(os.getpid())
            self.assertEqual(sessionStatus().status, "running")
            writeLock(process.pid, "some-other-machine.example")
            self.assertEqual(sessionStatus().status, "running")
            with open(lockPath, "w") as f:
                f.write("not a lock\n")
            self.assertEqual(sessionStatus().status, "running")
        finally:
            recovery.qt = realQt

        # With the real QLockFile and a folder that really cannot be written (not possible as the superuser)
        if os.name == "posix" and os.geteuid() != 0:
            writeLock(process.pid)
            os.chmod(crashedDir, 0o500)
            try:
                self.assertEqual(sessionStatus().status, "crashed")
            finally:
                os.chmod(crashedDir, 0o700)

    def test_h_retention(self):
        from StudioLib import recovery

        now = time.time()
        os.makedirs(self.root)

        def fake(name, status, ageDays, presented=1, own=False):
            path = os.path.join(self.root, name)
            os.makedirs(os.path.join(path, "scene-1"))
            with open(os.path.join(path, "scene-1", "data.bin"), "wb") as f:
                f.write(b"x" * (300 << 10))
            return {"path": path, "status": status, "own": own, "updated": now - ageDays * 86400, "presentedCount": presented,
                    "mtime": now - ageDays * 86400, "slots": [(os.path.join(path, "scene-1"), "current")]}

        records = [
            fake("own", "running", 0, own=True),
            fake("other-running", "running", 30),
            fake("clean", "closed", 0),
            fake("discarded", "discarded", 0),
            fake("old-unsaved", "closed-unsaved", 20),
            fake("old-crash-presented", "crashed", 20),
            fake("old-crash-new", "crashed", 20, presented=0),
            *[fake(f"recent-{i}", "closed-unsaved", i + 1) for i in range(6)],
        ]
        deleted = {os.path.basename(p) for p in recovery.prune(records, {"MaxAgeDays": 14, "KeepSessions": 5, "MaxDiskMB": 2048}, now)}
        self.assertTrue({"clean", "discarded", "old-unsaved", "old-crash-presented"} <= deleted, deleted)
        self.assertNotIn("own", deleted)
        self.assertNotIn("other-running", deleted)
        # 7 candidates (6 recent + the never-presented crash) -> the 2 oldest recent ones go; the unpresented crash is kept
        self.assertNotIn("old-crash-new", deleted)
        self.assertIn("recent-5", deleted)
        self.assertIn("recent-4", deleted)
        self.assertNotIn("recent-0", deleted)
        # Disk cap: 1 MB keeps deleting the oldest non-live sessions
        remaining = [r for r in records if os.path.basename(r["path"]) not in deleted]
        deleted2 = {os.path.basename(p) for p in recovery.prune(remaining, {"MaxAgeDays": 14, "KeepSessions": 5, "MaxDiskMB": 1}, now)}
        self.assertNotIn("own", deleted2)
        self.assertNotIn("other-running", deleted2)
        self.assertIn("recent-3", deleted2)

    def test_i_closeWithoutSaving(self):
        built = self._buildScene()
        service = self._service()
        self._generate(service)
        lesion = built["lesion"].copy()
        lesion[20, 25, 38] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
        slotDir = service.writer.slotDir
        slicer.mrmlScene.Clear(0)  # the user discarded the changes
        self.assertTrue(service.waitForIdle(30.0))
        data = self.io.readJson(os.path.join(service.sessionDir, self.io.SESSION_FILE))
        slots = {slot["name"]: slot for slot in data["slots"]}
        self.assertEqual(slots["scene-1"]["state"], "closed-unsaved")
        self.assertEqual(service.slotIndex, 2)
        manifest = self.io.readJson(os.path.join(slotDir, self.io.MANIFEST_FILE))
        self.assertEqual(manifest["generation"], 2)  # the final generation has the last edit
        # A scene without unsaved changes is not kept
        service2 = self._service()
        volume = self._userVolume("Saved")
        self.assertFalse(volume.GetModifiedSinceRead())
        slicer.mrmlScene.Clear(0)
        self.assertIsNone(service2.sessionDir)

    def test_j_writeScalarNrrd(self):
        # Oblique grid: a rotation of 20 degrees about the S axis, anisotropic spacing and an origin
        angle = np.radians(20.0)
        rotation = np.array([[np.cos(angle), -np.sin(angle), 0.0], [np.sin(angle), np.cos(angle), 0.0], [0.0, 0.0, 1.0]])
        affine = np.eye(4)
        affine[:3, :3] = rotation @ np.diag([0.8, 0.9, 1.3])
        affine[:3, 3] = [-12.5, 20.25, -30.0]
        rows = affine.tolist()
        os.makedirs(self.root)
        rng = np.random.default_rng(2)
        for dtype in ("int8", "uint8", "int16", "uint16", "int32", "uint32", "float32", "float64"):
            for level in (0, 1):
                array = (rng.integers(0, 100, size=(5, 6, 7)) - (50 if dtype.startswith("int") or dtype.startswith("float") else 0)).astype(dtype)
                path = os.path.join(self.root, f"v {dtype} {level}.nrrd")
                self.io.writeScalarNrrd(path, array, rows, level=level)
                node = slicer.util.loadVolume(path)
                loaded = slicer.util.arrayFromVolume(node)
                self.assertEqual(loaded.dtype, np.dtype(dtype), dtype)
                self.assertTrue(np.array_equal(loaded, array), dtype)
                loadedMatrix = vtk.vtkMatrix4x4()
                node.GetIJKToRASMatrix(loadedMatrix)
                difference = max(abs(loadedMatrix.GetElement(r, c) - rows[r][c]) for r in range(4) for c in range(4))
                self.assertLess(difference, 1e-9, dtype)
                slicer.mrmlScene.RemoveNode(node)

    def test_k_loadAutosaveDirectly(self):
        built = self._buildScene()
        expected = self._expected(built)
        service = self._service()
        self._generate(service)
        scenePath = os.path.join(service.writer.slotDir, self.io.SCENE_FILE)
        slicer.mrmlScene.Clear(0)
        slicer.util.loadScene(scenePath)
        self._checkLoaded(expected)
        self.assertEqual(self._storageFilesUnder(self.root), [])
        self.assertFalse(os.path.realpath(slicer.mrmlScene.GetURL() or "/").startswith(os.path.realpath(self.root)))
        self.assertTrue(slicer.util.getNode("Lesions").GetModifiedSinceRead())

    def test_l_offUnderTesting(self):
        self.assertTrue(slicer.app.testingEnabled())
        self.autosave.onStartupCompleted()
        self.assertIsNone(self.autosave.instance())
        mainWindow = slicer.util.mainWindow()
        self.assertIsNone(mainWindow.findChild(qt.QAction, "StudioRecoverAutosaveAction"))
        self.assertIsNone(mainWindow.findChild(qt.QAction, "StudioAutosaveNowAction"))
        self.assertIsNone(slicer.app.settingsDialog().findChild(qt.QWidget, "StudioAutosaveSettingsPanel"))
        from StudioLib import autosave_settings

        self.assertIsNone(autosave_settings.install(None))
        self.assertEqual(self.autosave.unsavedSummary(), "")
        self.assertFalse(self.autosave.autosaveNow("test"))
        with self.autosave.suspended("test"):
            pass

    def test_m_summaryThumbnailStudy(self):
        scene = slicer.mrmlScene
        service = self._service()
        self.assertEqual(service.unsavedSummary(), "")
        studyDir = os.path.join(self.tempDir, "My study")
        os.makedirs(os.path.join(studyDir, "lesions"))
        with open(os.path.join(studyDir, "study.json"), "w") as f:
            json.dump({"name": "test"}, f)
        lesionSource = slicer.util.addVolumeFromArray(np.zeros((10, 11, 12), np.uint8), name="sub-01_lesion")
        lesionPath = os.path.join(studyDir, "lesions", "sub-01_lesion.nrrd")
        self.assertTrue(slicer.util.saveNode(lesionSource, lesionPath))
        scene.RemoveNode(lesionSource)
        lesion = slicer.util.loadVolume(lesionPath)
        studyFiles = _filesUnder(studyDir)
        array = slicer.util.arrayFromVolume(lesion)
        array[5, 5, 5] = 1
        slicer.util.arrayFromVolumeModified(lesion)
        self._generate(service)
        self.assertEqual(_filesUnder(studyDir), studyFiles)
        summary = service.unsavedSummary()
        self.assertIn("autosaved at", summary)
        self.assertIn(time.strftime("%H:", time.localtime(service.status().lastSaveTime)), summary)
        self.assertNotIn("Saved", summary.replace("autosaved", ""))
        # Thumbnails never show a network map
        networkMap = slicer.util.addVolumeFromArray(np.ones((10, 11, 12), np.float32), name="Network map")
        networkMap.SetAttribute("LNM.Result", "1")
        slicer.util.setSliceViewerLayers(background=lesion, foreground=networkMap)
        slicer.app.processEvents()
        self.assertIsNone(service.grabThumbnail())
        slicer.util.setSliceViewerLayers(background=lesion, foreground=None)
        slicer.app.processEvents()
        self.assertIsNotNone(service.grabThumbnail())
        summaryData = self.io.summarizeScene(scene, {networkMap.GetID(): {"kind": "write"}, lesion.GetID(): {"kind": "write"}})
        self.assertNotIn("Network map", summaryData["volumes"])
        self.assertEqual(summaryData["other"], 1)

    def test_o_heavyAndLargeNodes(self):
        built = self._buildScene()
        service = self._service()
        self._generate(service)
        segmentationId = built["segmentation"].GetID()
        entry = service.writer.entries[segmentationId]
        # A heavy node written less than 10 minutes ago keeps its previous autosave (flagged stale)
        entry.update(heavy=True, writtenAt=time.time())
        lesion = built["lesion"].copy()
        lesion[20, 25, 39] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
        self._generate(service)
        kept = service.writer.entries[segmentationId]
        self.assertTrue(kept.get("stale"))
        self.assertEqual(kept["dir"], entry["dir"])
        self.assertTrue(service.dirty, "the scene stays dirty until the heavy node is autosaved")
        # Data larger than Studio/Autosave/MaxNodeMB is not autosaved and is listed as such
        service.writerOptionsOverride["maxNodeBytes"] = 1024
        tmap = built["tmap"]
        array = slicer.util.arrayFromVolume(tmap)
        array[0, 0, 0] += 1.0
        slicer.util.arrayFromVolumeModified(tmap)
        self._generate(service)
        self.assertEqual(service.writer.entries[tmap.GetID()].get("reason"), "too large")
        notAutosaved = [item["name"] for item in service.slots["scene-1"]["summary"]["notAutosaved"]]
        self.assertIn("T map", notAutosaved)

    def test_n_paletteProvider(self):
        import importlib.machinery
        import importlib.util
        import types

        import StudioLib

        service = self._service()
        if importlib.util.find_spec("StudioLib.palette") is None:
            # Without the palette (W3-06), nothing is registered; check the contract with a stand-in module
            self.assertFalse(service.registerPaletteProvider())
            providers = []
            fake = types.ModuleType("StudioLib.palette")
            fake.__spec__ = importlib.machinery.ModuleSpec("StudioLib.palette", None)
            fake.registerProvider = providers.append
            sys.modules["StudioLib.palette"] = fake
            StudioLib.palette = fake
            try:
                self.assertTrue(service.registerPaletteProvider())
            finally:
                del sys.modules["StudioLib.palette"]
                del StudioLib.palette
            self.assertEqual(len(providers), 1)
            items = providers[0](None)
        else:
            from StudioLib import palette

            if not hasattr(palette, "registerProvider"):
                self.skipTest("the palette has no provider contract")
            self.assertTrue(service.registerPaletteProvider())
            items = service.paletteItems()
            if hasattr(palette, "unregisterProvider"):
                palette.unregisterProvider(service.paletteItems)
        self.assertEqual({item["id"] for item in items}, {"autosave.recover", "autosave.now", "autosave.settings"})
        for item in items:
            self.assertTrue(callable(item["callback"]))
            self.assertNotIn("Saved", item["title"])

    def _abortWhileWorkerIn(self, service, moduleFunctionName, nodeToRemove, condition=lambda *args: True):
        """Start a generation, and remove ``nodeToRemove`` (which aborts it) while the background writer is inside
        ``autosave_io.<moduleFunctionName>`` (slowed down by 0.4 s, as for a larger scene).
        """
        original = getattr(self.io, moduleFunctionName)
        entered = []

        def slow(*args, **kwargs):
            if condition(*args):
                entered.append(time.monotonic())
                time.sleep(0.4)
            return original(*args, **kwargs)

        setattr(self.io, moduleFunctionName, slow)
        try:
            self.assertTrue(service.autosaveNow("test"))
            deadline = time.monotonic() + 30.0
            while not entered and time.monotonic() < deadline:
                slicer.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(entered, f"{moduleFunctionName}() was not called")
            slicer.mrmlScene.RemoveNode(nodeToRemove)  # NodeRemovedEvent aborts the generation
            self.assertIsNone(service.plan)
            # No new generation is planned before the result of the aborted one is known
            self.assertFalse(service.autosaveNow("test"))
            self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertTrue(service.waitForIdle(30.0))
        finally:
            setattr(self.io, moduleFunctionName, original)
        self.assertIsNone(service.writer.finalizing)

    def _checkSlotFolders(self, slotDir):
        """Every folder that the manifest describes exists, and no other folder is left."""
        manifest = self.io.readJson(os.path.join(slotDir, self.io.MANIFEST_FILE))
        self.assertFalse(manifest["pending"])
        dirs = {entry["dir"] for entry in manifest["entries"].values() if entry.get("dir")}
        self.assertTrue(dirs)
        self.assertEqual({f"data/{name}" for name in os.listdir(os.path.join(slotDir, "data"))}, dirs)
        return manifest

    def test_p_abortDuringFinalize(self):
        built = self._buildScene()
        service = self._service()
        # Aborted before the commit point: nothing of the generation is left
        other = slicer.util.addVolumeFromArray(np.zeros((5, 5, 5), np.int16), name="Other")
        self._abortWhileWorkerIn(service, "postProcessSceneXml", other)
        slotDir = service.writer.slotDir
        self.assertFalse(os.path.exists(os.path.join(slotDir, self.io.SCENE_FILE)))
        self.assertEqual(os.listdir(os.path.join(slotDir, "data")) if os.path.isdir(os.path.join(slotDir, "data")) else [], [])
        self.assertEqual(service.writer.generation, 0)
        # Aborted while Scene.mrml is replaced: the generation is complete and its folders are kept
        other = slicer.util.addVolumeFromArray(np.zeros((5, 5, 5), np.int16), name="Other 2")
        self._abortWhileWorkerIn(service, "atomicWriteText", other, lambda path, *args: path.endswith(self.io.SCENE_FILE))
        manifest = self._checkSlotFolders(slotDir)
        self.assertEqual(manifest["generation"], 2)  # generation numbers are not reused
        self.assertEqual(service.writer.generation, 2)
        self.assertIsNone(service._savedFingerprint, "the aborted generation does not mark the scene autosaved")
        # The next generation leaves only its own folders
        lesion = built["lesion"].copy()
        lesion[20, 25, 38] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
        self._generate(service)
        self.assertEqual(self._checkSlotFolders(slotDir)["generation"], 3)
        # A crash now recovers the scene
        expected = _md5(slicer.util.arrayFromVolume(built["volume"]))
        scenePath = os.path.join(slotDir, self.io.SCENE_FILE)
        service.dispose()
        slicer.mrmlScene.Clear(0)
        self.assertTrue(slicer.util.loadScene(scenePath))
        self.assertEqual(_md5(slicer.util.arrayFromVolume(slicer.util.getNode("MR Head"))), expected)
        self.assertEqual(slicer.util.getNode("Lesions").GetSegmentation().GetNumberOfSegments(), 1)

    def test_q_tensorVolumeAndFailingNode(self):
        scene = slicer.mrmlScene
        # A 64^3 DTI volume (9 MB of tensors, no scalars): written through its own storage node
        dti = scene.AddNewNodeByClass("vtkMRMLDiffusionTensorVolumeNode", "DTI")
        imageData = vtk.vtkImageData()
        imageData.SetDimensions(64, 64, 64)
        tensors = vtk.vtkFloatArray()
        tensors.SetNumberOfComponents(9)
        tensors.SetNumberOfTuples(64**3)
        tensors.Fill(1.0)
        tensors.SetName("tensors")
        imageData.GetPointData().SetTensors(tensors)
        dti.SetAndObserveImageData(imageData)
        dti.CreateDefaultDisplayNodes()
        # A volume of more than SNAPSHOT_MIN_MB whose snapshot job fails
        broken = slicer.util.addVolumeFromArray(np.ones((64, 128, 128), np.int16), name="Broken")
        small = slicer.util.addVolumeFromArray(np.full((10, 10, 10), 3, np.int16), name="Small")
        service = self._service()
        snapshotJob = self.io._SnapshotJob
        originalRun = snapshotJob.run

        def failingRun(job, *args):
            if job.nodeId == broken.GetID():
                raise ValueError("simulated failure")
            return originalRun(job, *args)

        snapshotJob.run = failingRun
        try:
            self._generate(service)
        finally:
            snapshotJob.run = originalRun
        self.assertEqual(service.status().state, "idle")
        entries = service.writer.entries
        self.assertEqual(entries[dti.GetID()]["kind"], "write")
        self.assertTrue(os.path.exists(os.path.join(service.writer.slotDir, entries[dti.GetID()]["file"])))
        self.assertEqual(entries[broken.GetID()]["kind"], "error")
        self.assertEqual(entries[small.GetID()]["kind"], "write")
        self.assertIn("Broken", [item["name"] for item in service.slots["scene-1"]["summary"]["notAutosaved"]])
        self.assertTrue(service.dirty, "a node that is not autosaved keeps the scene dirty")
        # The next generation writes the volume through its C++ storage node
        self._generate(service)
        self.assertEqual(service.writer.entries[broken.GetID()]["kind"], "write")
        self.assertFalse(service.dirty)
        scenePath = os.path.join(service.writer.slotDir, self.io.SCENE_FILE)
        service.dispose()
        scene.Clear(0)
        self.assertTrue(slicer.util.loadScene(scenePath))
        loaded = slicer.util.getNode("DTI")
        self.assertEqual(loaded.GetClassName(), "vtkMRMLDiffusionTensorVolumeNode")
        self.assertEqual(loaded.GetImageData().GetPointData().GetTensors().GetNumberOfTuples(), 64**3)
        self.assertEqual(int(slicer.util.arrayFromVolume(slicer.util.getNode("Broken")).sum()), 64 * 128 * 128)

    def test_r_recoverOldestClosedSlot(self):
        from StudioLib import recovery

        service = self._service()
        previous = self.autosave._service
        self.autosave._service = service  # recover() protects the slot through the service of the application
        self.addCleanup(setattr, self.autosave, "_service", previous)
        md5s = {}
        for index in range(3):
            voxels = (np.arange(20 * 21 * 22).reshape(20, 21, 22) * (index + 1) % 300).astype(np.int16)
            slicer.util.addVolumeFromArray(voxels, name=f"Volume {index + 1}")
            md5s[index + 1] = _md5(voxels)
            self._generate(service)
            if index < 2:
                slicer.mrmlScene.Clear(0)  # closed without saving: a closed-unsaved slot
                self.assertTrue(service.waitForIdle(30.0))
        own = next(session for session in recovery.scan(self.root, service.sessionId) if session.own)
        slots = {slot.name: slot for slot in own.slots}
        self.assertEqual(slots["scene-1"].state, "closed-unsaved")
        self.assertEqual(slots["scene-2"].state, "closed-unsaved")
        report = recovery.recover(slots["scene-1"], mode="replace")
        self.assertIsNotNone(report)
        self.assertTrue(service.waitForIdle(30.0))
        self.assertTrue(self.io.backgroundWriter().drain(30.0))
        self.assertEqual(_md5(slicer.util.arrayFromVolume(slicer.util.getNode("Volume 1"))), md5s[1])
        # The recovered slot, the other closed slot and the replaced scene are all kept
        self.assertTrue(os.path.isdir(slots["scene-1"].path))
        self.assertTrue(os.path.isdir(slots["scene-2"].path))
        data = self.io.readJson(os.path.join(service.sessionDir, self.io.SESSION_FILE))
        states = {slot["name"]: slot.get("state") for slot in data["slots"]}
        self.assertEqual(states.get("scene-1"), "closed-unsaved")
        self.assertEqual(states.get("scene-2"), "closed-unsaved")
        self.assertEqual(states.get("scene-3"), "closed-unsaved")
        self.assertFalse(service._protectedSlots)
        # At the next close, the limit applies again (the oldest closed slots go first)
        self._generate(service)
        slicer.mrmlScene.Clear(0)
        self.assertTrue(service.waitForIdle(30.0))
        self.assertTrue(self.io.backgroundWriter().drain(30.0))
        closed = sorted(name for name, slot in service.slots.items() if slot.get("state") == "closed-unsaved")
        self.assertEqual(closed, ["scene-3", "scene-4"])
        self.assertFalse(os.path.isdir(slots["scene-1"].path))

    def test_s_pollTiming(self):
        built = self._buildScene()
        service = self._service()
        service.reloadSettings()
        service.settings["IdleSeconds"] = 5
        self._generate(service)
        self.assertFalse(service.dirty)
        service._pollTimer.start()  # as after the installation (normally 1 s after startup)
        try:
            service.poll()
            lesion = built["lesion"].copy()
            lesion[20, 25, 38] = 1
            edited = time.monotonic()
            slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
            service.lastGenerationTime = None  # not limited by IntervalMinutes here
            started = None
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                slicer.app.processEvents()
                if service.plan is not None and started is None:
                    started = time.monotonic() - edited
                if started is not None and service.plan is None:
                    break
                time.sleep(0.005)
        finally:
            service._pollTimer.stop()
        self.assertIsNotNone(started, "no generation started")
        print(f"StudioAutosaveTest poll timing: generation started {started:.2f} s after the edit (IdleSeconds 5)")
        # The change is detected by the next poll, 3 s later, and counted from the middle of that gap (1.5 s). The
        # polls then follow the idle deadline (1 s polls at most): the generation starts 6.5 s after the edit, not at
        # the following 3 s poll (9 s)
        self.assertGreaterEqual(started, 6.5 - 0.5)
        self.assertLessEqual(started, 6.5 + 0.6)
        self.assertTrue(service.waitForIdle(30.0))
        self.assertEqual(service.writer.generation, 2)

    def test_s2_clockGoesBack(self):
        """The wall clock set back (NTP, manual change, resumed VM) must not stop the autosave (idle and rate use a clock that never goes back)."""
        import time as realTime

        class ShiftedTime:
            """The ``time`` module with the wall clock ``time()`` 1 h behind (``monotonic()`` is not affected)."""

            def time(self):
                return realTime.time() - 3600.0

            def __getattr__(self, name):
                return getattr(realTime, name)

        built = self._buildScene()
        service = self._service()
        service.reloadSettings()
        service.settings["IdleSeconds"] = 1
        service.settings["IntervalMinutes"] = 0.05  # 3 s
        self._generate(service)
        self.assertFalse(service.dirty)
        self.autosave.time = ShiftedTime()
        self.addCleanup(lambda: setattr(self.autosave, "time", realTime))
        service._pollTimer.start()
        try:
            service.poll()
            lesion = built["lesion"].copy()
            lesion[20, 25, 38] = 1
            edited = time.monotonic()
            slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, built["segmentation"], built["segmentId"], built["volume"])
            started = None
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline and started is None:
                slicer.app.processEvents()
                if service.plan is not None:
                    started = time.monotonic() - edited
                time.sleep(0.005)
        finally:
            service._pollTimer.stop()
        self.assertIsNotNone(started, "no autosave started in 25 s after the wall clock went back by 1 h")
        self.assertLess(started, 15.0)
        self.assertTrue(service.waitForIdle(30.0))
        self.assertFalse(service.dirty)

    #
    # Studio surfaces (A-01b)
    #

    def _pump(self, count=4):
        for _index in range(count):
            slicer.app.processEvents()
            time.sleep(0.01)

    def _applyStudio(self, service):
        """Apply Studio with ``service`` as the running autosave service (restored at the end of the test)."""
        from StudioLib import shell

        mainWindow = slicer.util.mainWindow()
        self.assertIsNotNone(mainWindow, "the Studio tests need the main window")
        geometry = mainWindow.saveGeometry()
        previous = self.autosave._service
        self.autosave._service = service

        def restore():
            live = shell._shell
            if live is not None:
                live.dispose()
            self.autosave._service = previous
            mainWindow.restoreGeometry(geometry)
            self._pump()

        self.addCleanup(restore)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        self._pump()
        if mainWindow.width < 1800:
            mainWindow.showNormal()
            mainWindow.resize(1800, max(mainWindow.height, 800))
            self._pump(8)
        return studioShell

    def _crashedSessions(self, service, workspace="draw"):
        """A copy of the session of ``service`` that looks crashed (its lock names an exited process)."""
        from StudioLib import recovery

        crashedId = "20260101-120000-1-abcd"
        crashedDir = os.path.join(self.root, crashedId)
        shutil.copytree(service.sessionDir, crashedDir, ignore=shutil.ignore_patterns(self.io.LOCK_FILE))
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        with open(os.path.join(crashedDir, self.io.LOCK_FILE), "w") as f:
            f.write(f"{process.pid}\nSlicer\n{qt.QSysInfo.machineHostName()}\n")
        data = self.io.readJson(os.path.join(crashedDir, self.io.SESSION_FILE))
        data.update(sessionId=crashedId, state="running", pid=process.pid)
        for slot in data["slots"]:
            slot["workspace"] = workspace
            slot["unsaved"] = True
        self.io.atomicWriteJson(os.path.join(crashedDir, self.io.SESSION_FILE), data)
        sessions = [s for s in recovery.scan(self.root, service.sessionId) if s.presentable()]
        self.assertEqual([s.sessionId for s in sessions], [crashedId])
        return sessions

    def _studioScene(self):
        volume = slicer.util.addVolumeFromArray(np.arange(20 * 21 * 22, dtype=np.int16).reshape(20, 21, 22) % 300, name="MR Head")
        volume.CreateDefaultDisplayNodes()
        return volume

    def _visibleStrings(self, root):
        texts = []
        for widget in [root, *root.findChildren("QWidget")]:
            if not widget.isVisible():
                continue
            for name in ("text", "toolTip"):
                value = widget.property(name)
                if isinstance(value, str) and value:
                    texts.append(value)
        return texts

    def test_t_studioStatus(self):
        from StudioLib import autosave_ui, shell
        from StudioTest import assertNoKeyCollisions

        service = self._service()
        studioShell = self._applyStudio(service)
        topBar = studioShell.topBar
        button = topBar.saveStatusButton
        self.assertEqual(button.objectName, "StudioSaveStatus")
        self.assertIsNotNone(autosave_ui.instance())
        self.assertFalse(button.visible, "hidden while nothing was autosaved")
        shortcutsBefore = sorted(str(item) for item in studioShell.shortcuts())

        seen = []
        service.addStatusListener(lambda status: seen.append((status.state, topBar.saveStatus()[0] if topBar.saveStatus() else None)))
        self._studioScene()
        self._generate(service)
        self._pump()
        self.assertIn(("saving", "Autosaving…"), seen)
        self.assertEqual(seen[-1], ("idle", "Autosaved · just now"))
        self.assertTrue(button.visible)
        self.assertEqual(button.text, "Autosaved · just now")
        self.assertIn("not saving", button.toolTip)
        self.assertIn("not saving", topBar.saveStatus()[2])
        self.assertTrue(autosave_ui.instance()._timer.isActive(), "refreshed every 30 s while shown")
        self.assertEqual(autosave_ui.instance()._timer.interval, 30000)

        # The menu without a project (and its disabled header): the recovery actions, Open project… (A-09) and the settings
        menu = button.menu()
        self.assertIsNotNone(menu)
        self.assertEqual(menu.objectName, "StudioSaveStatusMenu")
        texts = [action.text for action in menu.actions() if not action.isSeparator() and action.visible]
        self.assertEqual(texts[1:], ["Autosave now", "Recover autosaved work…", "Open autosave folder", "Open project…", "Saving settings…"])
        self.assertTrue(texts[0].startswith("Last autosave "))
        self.assertFalse(menu.actions()[0].enabled)
        self.assertEqual(button.popupMode, qt.QToolButton.InstantPopup)

        # The slot records where the user was
        slot = service.slots[service.slotName()]
        self.assertIn(slot.get("workspace"), ("library", "draw"))
        sessionData = self.io.readJson(os.path.join(service.sessionDir, self.io.SESSION_FILE))
        self.assertIn(sessionData["slots"][0].get("workspace"), ("library", "draw"))

        # Icon only below 1500 px and in compact mode; the text stays in the tooltip; the switcher stays centred
        mainWindow = slicer.util.mainWindow()
        self.assertFalse(topBar.isSaveStatusCompact())
        mainWindow.resize(1366, 768)
        self._pump(8)
        self.assertLessEqual(mainWindow.width, 1400)
        self.assertTrue(topBar.isSaveStatusCompact())
        self.assertIn("Autosaved · just now", button.toolTip)
        switcher = topBar.switcherFrame
        centre = switcher.mapTo(topBar.contents, qt.QPoint(switcher.width // 2, 0)).x()
        self.assertLessEqual(abs(centre - topBar.contents.width // 2), 2)
        mainWindow.resize(1800, 800)
        self._pump(8)
        self.assertGreaterEqual(mainWindow.width, 1700)
        self.assertFalse(topBar.isSaveStatusCompact())
        topBar.setCompact(True)
        self.assertTrue(topBar.isSaveStatusCompact())
        topBar.setCompact(False)
        studioShell._updateTopBarCompact()

        # Paused, then hidden when disabled
        service._setState("paused", "The disk is full")
        self.assertEqual(button.text, "Autosave paused")
        self.assertIn("The disk is full", button.toolTip)
        service._setState("idle")
        self.assertEqual(button.text, "Autosaved · just now")

        # No new shortcuts, no key collision
        self.assertEqual(sorted(str(item) for item in studioShell.shortcuts()), shortcutsBefore)
        assertNoKeyCollisions()
        self.assertIsNotNone(shell.instance())

    def test_u_studioRecoveryCard(self):
        from StudioLib import autosave_ui, library, recovery

        service = self._service()
        self._studioScene()
        self._generate(service)
        sessions = self._crashedSessions(service, workspace="draw")
        studioShell = self._applyStudio(service)
        ui = autosave_ui.instance()
        self.assertIsNotNone(ui)
        self.assertEqual(service._presenter, ui.presentRecovery)
        studioShell.switchTo("library")
        self._pump()
        dialogBefore = recovery._dialog

        # The card goes at the top of the Library canvas, the dialog is not shown
        service._presenter(sessions, startup=True)
        self._pump()
        self.assertIs(recovery._dialog, dialogBefore)
        self.assertFalse(recovery._dialog is not None and recovery._dialog.isVisible())
        canvas = library.canvas()
        card = ui.card
        self.assertIsNotNone(card)
        self.assertEqual(card.widget.objectName, "StudioRecoveryCard")
        self.assertEqual(library.notices(), {autosave_ui.NOTICE_KEY: card.widget})
        self.assertTrue(card.widget.isVisible())
        layout = canvas.contentLayout
        self.assertLess(layout.indexOf(canvas.noticeContainer), layout.indexOf(canvas.studyContainer))
        cardTop = card.widget.mapTo(canvas.widget, qt.QPoint(0, 0)).y()
        dropTop = canvas.dropZone.mapTo(canvas.widget, qt.QPoint(0, 0)).y()
        self.assertLess(cardTop + card.widget.height, dropTop + 1)
        self.assertIsNone(studioShell.inspectorNoticeKey(), "no inspector notice in Library")
        self.assertEqual(card.recoverButton.text, "Recover")
        self.assertIn("closed unexpectedly", " ".join(label.text for label in card.widget.findChildren("QLabel")))
        self.assertFalse(card.allButton.visible, "one autosaved scene")

        # In Draw, an inspector notice points to it; Show goes to Library and clears it
        studioShell.switchTo("draw")
        self._pump()
        ui.presentRecovery(sessions)
        self._pump()
        self.assertEqual(studioShell.inspectorNoticeKey(), autosave_ui.NOTICE_KEY)
        showButton = [b for b in studioShell.inspectorNotice().findChildren("QToolButton") if b.text == "Show"]
        self.assertEqual(len(showButton), 1)
        showButton[0].click()
        self._pump()
        self.assertEqual(studioShell.currentWorkspace(), "library")
        self.assertIsNone(studioShell.inspectorNoticeKey())
        self.assertTrue(ui.card.widget.isVisible())

        # Recover restores the scene, closes the card and returns to the recorded workspace (Draw)
        slicer.mrmlScene.Clear(0)
        ui.card.recoverButton.click()
        self._pump()
        self.assertIsNone(ui.card)
        self.assertEqual(library.notices(), {})
        self.assertFalse(canvas.noticeContainer.isVisible())
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("MR Head"))
        self.assertEqual(studioShell.currentWorkspace(), "draw")
        self.assertFalse(sessions[0].presentable(), "marked recovered")

        # A recovered session is not offered again
        self.assertIsNone(ui.presentRecovery(sessions))

    def test_u2_recoveryHandlerMarksRecovered(self):
        """BB-1: reopening the study (a recovery handler) from the card or from the dialog left the session 'running' in
        session.json, so the next start offered the same unsaved work again.
        """
        from StudioLib import autosave_ui, recovery

        service = self._service()
        self._studioScene()
        self._generate(service)
        handled = []
        service.registerRecoveryHandler(lambda slot: True, handled.append)

        def state(session):
            return (self.io.readJson(os.path.join(session.path, self.io.SESSION_FILE)) or {}).get("state")

        # The card ("Reopen study at <sid>")
        sessions = self._crashedSessions(service)
        studioShell = self._applyStudio(service)
        ui = autosave_ui.instance()
        self.assertEqual(state(sessions[0]), "running")
        self.assertTrue(ui.recover(sessions[0].recoverableSlots()[0]))
        self.assertEqual(len(handled), 1)
        self.io.backgroundWriter().drain(5.0)  # the write goes through the background writer
        self.assertEqual(state(sessions[0]), "recovered")
        self.assertEqual([s for s in recovery.scan(self.root, service.sessionId) if s.presentable()], [])

        # The dialog ("Recover unsaved work...")
        shutil.rmtree(sessions[0].path)
        sessions = self._crashedSessions(service)
        self.assertEqual(state(sessions[0]), "running")
        dialog = recovery.RecoveryDialog()
        try:
            dialog.setSessions(sessions, startup=True, root=self.root)
            dialog.list.setCurrentItem(dialog.list.topLevelItem(0))
            dialog.onRecover()
        finally:
            dialog.close()
        self.assertEqual(len(handled), 2)
        self.io.backgroundWriter().drain(5.0)
        self.assertEqual(state(sessions[0]), "recovered")
        self.assertIsNotNone(studioShell)

    def test_u3_reopenStudyKeepsOtherData(self):
        """BB-1: the card of a crash in a study listed '1 point list · 1 table', but 'Reopen study at <sid>' only reopened
        the study: the autosaved point list and table were never loaded, and no part of the UI could restore them. The
        study's subject is reopened by the study (not loaded twice), the other data is added with the views kept.
        """
        from StudioLib import autosave_ui, importer, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        from LesionNetworkMappingLib import geometry, nifti

        service = self._service()
        studioShell = self._applyStudio(service)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        self.addCleanup(study_ui.closeStudy)
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.userDir, "Points study")
        os.makedirs(folder)
        scans = []
        for index, sid in enumerate(("sub-01", "sub-02")):
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], (np.indices(grid.shape).sum(axis=0) % (50 + index)).astype(np.int16), grid.affine)
        session = study_ui.createStudy(scans, studioShell, folder=folder)
        self.assertTrue(session.open("sub-01", switchToDraw=False))
        self._pump()
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Targets")
        for position in ((1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)):
            points.AddControlPoint(vtk.vtkVector3d(*position))
        table = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "Measurements")
        table.AddColumn().SetName("value")
        table.SetCellText(table.AddEmptyRow(), 0, "7")
        self._generate(service)
        sessions = self._crashedSessions(service)
        slot = sessions[0].recoverableSlots()[0]
        self.assertIn("1 point list", slot.contentsText())
        self.assertIn("1 table", slot.contentsText())
        self.assertEqual(autosave_ui.recoverText(slot), "Reopen study at sub-01")

        # The restart: no study, an empty scene
        study_ui.closeStudy()
        slicer.mrmlScene.Clear(0)
        self._pump()
        self.assertTrue(autosave_ui.instance().recover(slot))
        self._pump()
        session = study_ui.session()
        self.assertIsNotNone(session)
        self.assertEqual(session.current(), "sub-01")
        # The subject is the study's own, once (the autosaved copy of its scan and lesion is not added)
        self.assertEqual([node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")], [session.segmentationNode().GetID()])
        self.assertEqual([node.GetID() for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")], [session.volumeNode().GetID()])
        # The point list and the table are back, on no file of the autosave folder
        recoveredPoints = slicer.util.getNodesByClass("vtkMRMLMarkupsFiducialNode")
        self.assertEqual([node.GetName() for node in recoveredPoints], ["Targets"])
        self.assertEqual(recoveredPoints[0].GetNumberOfControlPoints(), 3)
        recoveredTables = slicer.util.getNodesByClass("vtkMRMLTableNode")
        self.assertEqual([node.GetName() for node in recoveredTables], ["Measurements"])
        self.assertEqual(recoveredTables[0].GetCellText(0, 0), "7")
        self.assertEqual(self._storageFilesUnder(self.root), [])
        # The views still show the subject and Draw still edits its lesion (loading a scene empties them)
        for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
            self.assertEqual(compositeNode.GetBackgroundVolumeID(), session.volumeNode().GetID())
        editor = importer.segmentEditorWidget()
        editor = getattr(editor, "editor", editor)
        if editor is not None and editor.mrmlSegmentEditorNode() is not None:
            self.assertEqual(editor.segmentationNode().GetID(), session.segmentationNode().GetID())
        # No temporary scene file is left in the private slot folder
        self.assertEqual([name for name in os.listdir(slot.path) if name.endswith(".mrml")], [self.io.SCENE_FILE])

    def test_v_studioClassic(self):
        from StudioLib import autosave_ui, library

        service = self._service()
        self._studioScene()
        self._generate(service)
        sessions = self._crashedSessions(service)
        service._pendingRecoveries = list(sessions)
        studioShell = self._applyStudio(service)
        ui = autosave_ui.instance()
        # The scan already ran: the card is offered at install
        self.assertIsNotNone(ui.card)
        studioShell.switchTo("draw")
        self._pump()
        ui.presentRecovery(sessions)
        self._pump()
        self.assertEqual(studioShell.inspectorNoticeKey(), autosave_ui.NOTICE_KEY)
        self.assertTrue(studioShell.topBar.saveStatusButton.visible)
        texts = self._visibleStrings(slicer.util.mainWindow()) + self._visibleStrings(ui.card.widget)
        saved = [text for text in texts if "Saved" in text.replace("Autosaved", "")]
        self.assertEqual(saved, [], "autosave strings say Autosaved, never Saved")

        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(autosave_ui.instance())
        self.assertIsNone(service._presenter, "the default presenter (A-01's dialog) is back")
        self.assertEqual(library.notices(), {})
        self.assertIsNone(studioShell.inspectorNoticeKey())
        buttons = [b for b in slicer.util.mainWindow().findChildren("QToolButton") if b.objectName == "StudioSaveStatus" and b.isVisible()]
        self.assertEqual(buttons, [])
        self.assertNotIn(ui.context, service._contextProviders)

        # Studio again: the status and the card come back
        studioShell.apply(midSession=True)
        self._pump()
        ui = autosave_ui.instance()
        self.assertIsNotNone(ui)
        self.assertTrue(studioShell.topBar.saveStatusButton.visible)
        self.assertIsNotNone(ui.card)
        self.assertEqual(list(library.notices()), [autosave_ui.NOTICE_KEY])

        # Later: not offered again in this run
        ui.card.laterButton.click()
        self._pump()
        self.assertIsNone(ui.card)
        self.assertEqual(library.notices(), {})
        self.assertIsNone(ui.presentRecovery(sessions))
        autosave_ui._dismissed.clear()

    def test_w_studioStaleCard(self):
        from StudioLib import autosave_ui, library

        service = self._service()
        self._studioScene()
        self._generate(service)
        sessions = self._crashedSessions(service)
        studioShell = self._applyStudio(service)
        studioShell.switchTo("library")
        self._pump()
        ui = autosave_ui.instance()

        # Recovered meanwhile from "Recover autosaved work…" (session.json says so): Recover does not load it twice
        card = ui.presentRecovery(sessions)
        self.assertIsNotNone(card)
        sessionFile = os.path.join(sessions[0].path, self.io.SESSION_FILE)
        data = self.io.readJson(sessionFile)
        data["state"] = "recovered"
        self.io.atomicWriteJson(sessionFile, data)
        slicer.mrmlScene.Clear(0)
        card.recoverButton.click()
        self._pump()
        self.assertIsNone(ui.card, "the stale card closes")
        self.assertEqual(library.notices(), {})
        self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("MR Head"), "nothing loaded")

        # The card's widget deleted from outside (for example with the canvas): closing and Classic still clean up
        data["state"] = "running"
        self.io.atomicWriteJson(sessionFile, data)
        card = ui.presentRecovery(sessions)
        self.assertIsNotNone(card)
        card.widget.delete()
        self._pump()
        studioShell.classic(persist=False)
        self._pump()
        self.assertIsNone(autosave_ui.instance())
        self.assertIsNone(service._presenter)
        self.assertFalse(studioShell.topBar is not None and studioShell.topBar.saveStatusButton.visible)

    def test_x_projectReplacesPrivateAutosave(self):
        """A-09: in a project, the scene is saved in the project and nothing is written to the private recovery folder;
        once the project is released (a study opens), the private recovery autosave runs again.
        """
        from StudioLib import projects

        environment = os.environ.get(projects.ROOT_ENVIRONMENT)
        os.environ[projects.ROOT_ENVIRONMENT] = os.path.join(self.tempDir, "Slicer Studio")
        try:
            service = self._service()
            # Without studioActiveOverride a service under --testing never creates a project
            self._userVolume()
            service.poll()
            self.assertIsNone(service.project)
            service.studioActiveOverride = True
            service.poll()
            self.assertIsNotNone(service.project)
            self.assertTrue(service.saveNow("test", progress=False))
            self.assertIsNone(service.sessionDirectory(), "no private session while the scene is a project")
            self.assertEqual(service.unsavedSummary(), "")
            # A study opens: the project stops; the next change is autosaved privately (A-01)
            service._studyOpen = lambda: True
            service.poll()
            self.assertIsNone(service.project)
            slicer.util.addVolumeFromArray(np.zeros((8, 8, 8), np.int16), name="New")
            self._generate(service)
            self.assertIsNotNone(service.sessionDirectory())
            self.assertTrue(os.path.isfile(os.path.join(service.slotDirectory(), self.io.SCENE_FILE)))
        finally:
            if environment is None:
                os.environ.pop(projects.ROOT_ENVIRONMENT, None)
            else:
                os.environ[projects.ROOT_ENVIRONMENT] = environment

    def test_z_recoveryDialogChrome(self):
        """VISUAL 4.7: the recovery dialog has the Studio dialog chrome, the header with the icon tile, a link on the left, one
        primary button on the right, and an empty state instead of an empty list.
        """
        from StudioLib import recovery

        dialog = recovery.RecoveryDialog()
        try:
            self.assertTrue(dialog.property("studioDialogChrome"))
            self.assertTrue(dialog.windowFlags() & qt.Qt.FramelessWindowHint)
            self.assertFalse(dialog.isModal())
            tile = dialog.findChild(qt.QLabel, "StudioRecoveryIcon")
            self.assertEqual((tile.width, tile.height), (40, 40))
            self.assertEqual(dialog.titleLabel.text, "Recover unsaved work")
            self.assertEqual(dialog.recoverButton.property("slicerRole"), "primary")
            self.assertEqual(dialog.settingsLink.property("studioLink"), True)
            self.assertIn("Autosave settings", dialog.settingsLink.text)
            dialog.setSessions([], startup=False, root=self.root)
            dialog.show()
            self.assertTrue(dialog.emptyLabel.isVisible())
            self.assertFalse(dialog.list.isVisible())
            self.assertFalse(dialog.recoverButton.enabled)
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_y_removedNodesAreFreed(self):
        """BB-1: every helper that lists scene nodes (private autosave poll and generation, project save, recovery,
        projects and Lesion Network Mapping helpers) must release the collection that GetNodesByClass returns;
        otherwise the collection keeps each listed node, and its image data, in memory after it is removed.
        """
        import gc

        import LesionNetworkMapping
        from LesionNetworkMappingLib import display
        from StudioLib import projects, recovery

        freed = set()

        def watch(name, obj):
            obj.AddObserver(vtk.vtkCommand.DeleteEvent, lambda caller, event, name=name: freed.add(name))

        def addData():
            # A volume shown in the views is released by the view pipeline only at its next display change, also in
            # official Slicer: nothing is shown here
            array = np.arange(40 * 50 * 60, dtype=np.int16).reshape(40, 50, 60)
            path = os.path.join(self.userDir, "Saved scan.nrrd")
            self.assertTrue(slicer.util.saveNode(slicer.util.addVolumeFromArray(array, name="Saved scan"), path))
            slicer.mrmlScene.Clear(0)
            saved = slicer.util.loadVolume(path, {"show": False})  # has a storage node: listed, not copied
            unsaved = slicer.util.addVolumeFromArray(np.arange(30 * 40 * 50, dtype=np.int16).reshape(30, 40, 50), name="Unsaved scan")
            segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
            segmentation.CreateDefaultDisplayNodes()
            segmentation.GetSegmentation().AddEmptySegment("Lesion")
            for node in (saved, unsaved, segmentation):
                watch(node.GetName(), node)
            watch("Saved scan image", saved.GetImageData())
            watch("Unsaved scan image", unsaved.GetImageData())
            return [node.GetID() for node in (saved, unsaved, segmentation)]

        environment = os.environ.get(projects.ROOT_ENVIRONMENT)
        os.environ[projects.ROOT_ENVIRONMENT] = os.path.join(self.tempDir, "Slicer Studio")
        try:
            nodeIDs = addData()
            service = self._service()
            service.poll()
            self._generate(service)  # private recovery autosave
            service.studioActiveOverride = True
            service.poll()
            self.assertIsNotNone(service.project)
            self.assertTrue(service.saveNow("test", progress=False))  # project save
            scene = slicer.mrmlScene
            projects.sceneHasUserData(scene)
            projects.defaultProjectName(scene)
            projects.counts(scene)
            recovery.sceneHasDisplayableData(scene)
            recovery.autosaveSlotOfScene(scene, [self.root])
            LesionNetworkMapping.migrateLegacyConnectomePaths(scene)
            display.ensureLUT(scene)
            self.io.backgroundWriter().drain(30.0)
            for nodeID in nodeIDs:
                slicer.mrmlScene.RemoveNode(slicer.mrmlScene.GetNodeByID(nodeID))
            service.poll()
            self.io.backgroundWriter().drain(30.0)
            gc.collect()
            self.assertEqual(sorted(freed), sorted(["Saved scan", "Unsaved scan", "Lesions", "Saved scan image", "Unsaved scan image"]))
        finally:
            if environment is None:
                os.environ.pop(projects.ROOT_ENVIRONMENT, None)
            else:
                os.environ[projects.ROOT_ENVIRONMENT] = environment

    def test_za_installWithoutQActionMenu(self):
        """BB-1, Qt 6: QAction.menu() is a template that PythonQt does not wrap, so File menu actions have no menu
        attribute. installActions() still puts 'Recover Autosaved Work…' after the Recent submenu and adds 'Autosave
        now', and when one install step fails the later ones (Search entries, settings panel, close summary, project
        File menu items) are still installed.
        """
        from StudioLib import autosave_settings, projects_ui

        class Qt6Action:
            """A QAction as PythonQt wraps it with Qt 6: every attribute except menu."""

            def __init__(self, action):
                object.__setattr__(self, "_action", action)

            def __getattr__(self, name):
                if name in ("menu", "setMenu"):
                    raise AttributeError(f"QAction has no attribute named '{name}'")
                return getattr(self._action, name)

            def __eq__(self, other):
                return self._action == getattr(other, "_action", other)

            __hash__ = None

        def unwrap(action):
            return getattr(action, "_action", action)

        class Qt6FileMenu:
            def __init__(self, menu):
                self.menu = menu

            def actions(self):
                return [Qt6Action(action) for action in self.menu.actions()]

            def insertAction(self, before, action):
                self.menu.insertAction(unwrap(before), unwrap(action))

            def addAction(self, action):
                self.menu.addAction(unwrap(action))

        window = qt.QMainWindow()
        fileMenu = window.menuBar().addMenu("File")
        fileMenu.setObjectName("FileMenu")
        for text in ("Add Data", "Add DICOM Data"):
            fileMenu.addAction(text)
        fileMenu.addMenu("Recent").addAction("scan.nrrd")
        fileMenu.addAction("Save")
        fileMenu.addSeparator()
        fileMenu.addAction("Exit")

        class Qt6Window:
            """The main window, whose File menu actions have no menu() (the QActions are parented to the real window)."""

            def __getattr__(self, name):
                return getattr(window, name)

            def findChild(self, className, name):
                return Qt6FileMenu(fileMenu) if name == "FileMenu" else window.findChild(className, name)

        realMainWindow = slicer.util.mainWindow
        realQAction = qt.QAction
        slicer.util.mainWindow = lambda *args, **kwargs: Qt6Window()
        # PythonQt needs a real QObject parent: give it the real window behind the stand-in
        qt.QAction = lambda text, parent=None: realQAction(text, window if isinstance(parent, Qt6Window) else parent)
        try:
            service = self._service()
            service.installActions()
        finally:
            slicer.util.mainWindow = realMainWindow
            qt.QAction = realQAction
        try:
            self.assertEqual([str(action.text) for action in fileMenu.actions() if not action.isSeparator()],
                             ["Add Data", "Add DICOM Data", "Recent", "Recover Autosaved Work…", "Save", "Exit"])
            self.assertEqual(service.recoverAction.objectName, "StudioRecoverAutosaveAction")
            self.assertEqual(service.autosaveNowAction.objectName, "StudioAutosaveNowAction")
            self.assertIn("StudioAutosaveNowAction", [action.objectName for action in window.actions()])
        finally:
            window.deleteLater()

        # A failing step does not skip the steps after it
        service = self._service()
        ran = []

        def qt6InstallActions():
            ran.append("installActions")
            raise AttributeError("QAction has no attribute named 'menu'")

        service.installActions = qt6InstallActions
        service.registerPaletteProvider = lambda: ran.append("registerPaletteProvider")
        service.connectCloseSummary = lambda: ran.append("connectCloseSummary")
        realSettingsInstall = autosave_settings.install
        realAttachService = projects_ui.attachService
        autosave_settings.install = lambda svc=None: ran.append("autosave_settings.install")
        projects_ui.attachService = lambda svc: ran.append("projects_ui.attachService")
        try:
            service._install()
        finally:
            autosave_settings.install = realSettingsInstall
            projects_ui.attachService = realAttachService
        self.assertEqual(ran, ["installActions", "registerPaletteProvider", "autosave_settings.install", "connectCloseSummary",
                               "projects_ui.attachService"])
        self.assertTrue(service._pollTimer.isActive())

    def test_zb_docsNameTheSavingPanel(self):
        """BB-1: A-09 renamed the Autosave settings panel to Saving and relabelled its controls. The documentation and the
        README.txt of the autosave folder name the panel and the controls that exist: every bold name of the Saving section
        of settings.md is a label or a button of the panel, and the old names are gone.
        """
        import re

        from StudioLib import autosave_io, autosave_settings

        self.assertEqual(autosave_settings.PANEL_NAME, "Saving")
        panel = autosave_settings.AutosaveSettingsPanel(None)
        try:
            texts = set()
            for className in ("QLabel", "QAbstractButton"):
                for widget in panel.findChildren(className):
                    texts.add(str(widget.text).strip())
            texts.discard("")
        finally:
            panel.deleteLater()

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        documents = {name: os.path.join(root, *name.split("/")) for name in
                     ("README.md", "Docs/user_guide/settings.md", "Docs/user_guide/modules/studio.md")}
        if not all(os.path.isfile(path) for path in documents.values()):
            self.skipTest("the documentation is not next to this build")
        contents = {}
        for name, path in documents.items():
            with open(path, encoding="utf-8") as stream:
                contents[name] = stream.read()

        section = contents["Docs/user_guide/settings.md"].split("\n### Saving\n", 1)[1].split("\n### ", 1)[0]
        names = [" ".join(name.split()) for name in re.findall(r"\*\*([^*]+)\*\*", section)]
        self.assertGreater(len(names), 10)
        # "Show in Files" is "Show in Finder" or "Show in Explorer" on the other systems
        missing = [name for name in names if name not in texts and not name.startswith("Show in ")]
        self.assertEqual(missing, [], "settings.md names controls that the Saving panel does not have")

        stale = ["Application Settings > Autosave", "Autosave the scene", "Autosave panel", "in the Autosave settings",
                 "Save continuously", "**At most every**", "**or after a pause of**", "**Max size**",
                 "**Recover autosaved work…**", "**Keep**"]
        for name, content in contents.items():
            for phrase in stale:
                self.assertNotIn(phrase, content, f"{name} still says {phrase!r}")
        self.assertNotIn("Application Settings > Autosave", autosave_io._README_TEXT)
        self.assertIn("Application Settings > Saving", autosave_io._README_TEXT)

    def test_zb2_studioProjectsDocNamesTheFolderButtonsOfTheSavingPanel(self):
        """BB-1: studio_projects.md named a "Show" button next to "Change…" in Settings > Saving; the button is "Show in Files"
        ("Show in Finder" on macOS, "Show in Explorer" on Windows). Every bold button name that the sentence about the Slicer
        Studio folder of the Saving panel gives must be a button of the panel (the file manager name differs per system).
        """
        import re

        from StudioLib import autosave_settings

        panel = autosave_settings.AutosaveSettingsPanel(None)
        try:
            buttons = {str(button.text).strip() for button in panel.findChildren("QAbstractButton")}
            names = [str(panel.findChild(qt.QPushButton, name).text) for name in
                     ("StudioProjectsShowRootButton", "StudioProjectsChangeRootButton")]
        finally:
            panel.deleteLater()
        self.assertEqual(names[1], "Change\u2026")
        self.assertTrue(names[0].startswith("Show in "), names[0])

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "Docs", "user_guide", "studio_projects.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
        sentence = content.split("shows the Slicer Studio folder, with ", 1)[1].split(". When you change", 1)[0]
        named = [" ".join(name.split()) for name in re.findall(r"\*\*([^*]+)\*\*", sentence)]
        self.assertIn("Change\u2026", named)
        for name in named:
            self.assertTrue(name in buttons or name in ("Show in Files", "Show in Finder", "Show in Explorer"),
                            f"studio_projects.md names {name!r}, which the Saving panel does not have")
        self.assertNotIn("Show", named, "the Saving panel has no button called Show")

    def test_zc0_cameraMovingAfterOpenIsNotAnEdit(self):
        """BB-1: a camera node is a storable node, but the 3D view moves it on its own a moment after a scene with points,
        a line or a curve is shown (the project was rewritten and "Last edited" moved on every open). A camera moving is
        not an edit; a new node is still a change.
        """
        scene = slicer.mrmlScene
        scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Pts").AddControlPoint([1.0, 2.0, 3.0], "P1")
        camera = scene.AddNewNodeByClass("vtkMRMLCameraNode", "ViewCamera")

        def fingerprint():
            return self.io.sceneFingerprint(scene, self.io.sentinelIds())

        before = fingerprint()
        camera.SetPosition(0.0, 400.0, 10.0)
        camera.SetFocalPoint(1.0, 2.0, 3.0)
        camera.Modified()
        self.assertEqual(fingerprint(), before, "a moved camera makes the project look edited")
        scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "More")
        self.assertNotEqual(fingerprint(), before)

    def test_zc1_folderAttributeIsAnEdit(self):
        """BB-1: an attribute set on a subject hierarchy item (``SetItemAttribute``: the DICOM tag editor, extensions)
        does not modify the node; it is an edit of the project and changes the fingerprint (quit lost it).
        """
        scene = slicer.mrmlScene
        hierarchy = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(scene)
        folder = hierarchy.CreateFolderItem(hierarchy.GetSceneItemID(), "Group A")

        def fingerprint():
            return self.io.sceneFingerprint(scene, self.io.sentinelIds())

        before = fingerprint()
        hierarchy.SetItemAttribute(folder, "Group", "case")
        after = fingerprint()
        self.assertNotEqual(after, before, "a folder attribute is not seen as an edit")
        hierarchy.SetItemAttribute(folder, "Group", "control")
        self.assertNotEqual(fingerprint(), after)

    def test_zc2_pausedStatusIsPlainWhenFolderCannotBeWritten(self):
        """BB-1: "Autosave paused" says why in plain words, not "[Errno 13] Permission denied: '<session path>'"."""
        # The autosave folder is below a file, so its session folder cannot be created
        blocker = os.path.join(self.tempDir, "a file")
        with open(blocker, "w") as f:
            f.write("not a folder")
        self.root = os.path.join(blocker, "StudioAutosave")
        service = self._service()
        self._userVolume()
        service.autosaveNow("test")
        status = service.status()
        self.assertEqual(status.state, "paused", status.message)
        self.assertTrue(status.message.startswith("Autosave paused: the autosave folder cannot be written"), status.message)
        self.assertNotIn("Errno", status.message)
        self.assertNotIn(self.tempDir, status.message)
        # Any OSError (writing step, failed background generation) says the same, with the folder named
        service.root = os.path.join(self.tempDir, "StudioAutosave")
        error = PermissionError(13, "Permission denied", os.path.join(service.root, "20261109-session"))
        service._pauseForOSError(error)
        self.assertEqual(
            service.status().message,
            "Autosave paused: the autosave folder cannot be written (Permission denied (in the folder StudioAutosave)).",
        )

    def test_zc3_synchronousAutosaveIntoUnwritableFolderPausesPlainly(self):
        """BB-1: a close/exit autosave that cannot write pauses with the plain message (no traceback, no busy state)."""
        service = self._service()
        self._userVolume()
        service.autosaveNow("test")
        self.assertTrue(service.waitForIdle(120.0))
        writer = service._slotWriter()
        realFinalize = writer.finalize

        def failing(plan):
            raise PermissionError(13, "Permission denied", os.path.join(service.root, "session", "scene-1", "data"))

        errors = []

        class _Handler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.ERROR:
                    errors.append(record.getMessage())

        handler = _Handler()
        logging.getLogger().addHandler(handler)
        writer.finalize = failing
        try:
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Changed").AddControlPoint(1.0, 2.0, 3.0)
            service.autosaveNow("test", synchronous=True)
        finally:
            writer.finalize = realFinalize
            logging.getLogger().removeHandler(handler)
        status = service.status()
        self.assertEqual(status.state, "paused", status.message)
        self.assertTrue(status.message.startswith("Autosave paused: the autosave folder cannot be written"), status.message)
        self.assertEqual(errors, [])

    def test_zd_discardConfirmationSaysTrash(self):
        """BB-1: Discard moves the session to the OS Trash, so its confirmation must not say "This cannot be undone"
        (a user who believes it is deleted will not empty the Trash); it names the Trash.
        """
        from StudioLib import projects, recovery

        folder = os.path.join(self.root, "session-trash")
        os.makedirs(folder)
        session = recovery.SessionInfo(folder, {"updated": ""}, "closed")
        asked = []
        moved = []
        realQuestion, realMove, realMark = qt.QMessageBox.question, projects.moveToTrash, recovery.markSession
        try:
            qt.QMessageBox.question = lambda parent, title, text, *args: (asked.append(text), qt.QMessageBox.Discard)[1]
            projects.moveToTrash = lambda path: (moved.append(path), True)[1]
            recovery.markSession = lambda *args, **kwargs: None
            self.assertTrue(recovery.discard(session))
        finally:
            qt.QMessageBox.question, projects.moveToTrash, recovery.markSession = realQuestion, realMove, realMark
        self.assertEqual(moved, [folder])
        self.assertEqual(len(asked), 1)
        self.assertNotIn("cannot be undone", asked[0].lower())
        self.assertIn("Trash", asked[0])

    def test_zc_labelLockAndTreeOrderAreEdits(self):
        """BB-1: the label, the description and the lock of a control point (they invoke a custom modified event, not
        Modified(), so the node's MTime stays) and the order of siblings in the Data tree (the fingerprint sorted the items)
        are edits of the project: the fingerprint changes, the poll marks the scene as not saved, and the next autosave
        writes them (it said "Saved" and a crash lost them).
        """
        scene = slicer.mrmlScene
        points = scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Pts")
        points.AddControlPoint([1.0, 2.0, 3.0], "P1")
        others = [scene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", name) for name in ("B", "C")]
        hierarchy = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(scene)

        def fingerprint():
            return self.io.sceneFingerprint(scene, self.io.sentinelIds())

        edits = [
            ("label", lambda: points.SetNthControlPointLabel(0, "Target")),
            ("description", lambda: points.SetNthControlPointDescription(0, "the target")),
            ("lock", lambda: points.SetNthControlPointLocked(0, True)),
            ("unlock", lambda: points.SetNthControlPointLocked(0, False)),
            ("order", lambda: hierarchy.MoveItem(hierarchy.GetItemByDataNode(others[1]), hierarchy.GetItemByDataNode(others[0]))),
        ]
        for name, edit in edits:
            before = fingerprint()
            edit()
            self.assertNotEqual(fingerprint(), before, f"the fingerprint does not see the {name} edit")
            self.assertEqual(fingerprint(), fingerprint())

        # The poll sees the edit and the next generation writes the new label
        service = self._service()
        self._generate(service)
        service.poll()
        self.assertFalse(service.dirty)
        points.SetNthControlPointLabel(0, "Renamed")
        service.poll()
        self.assertTrue(service.dirty, "the status says saved with an unsaved label")
        self._generate(service)
        self.assertFalse(service.dirty)

    def test_ze_thumbnailSkipsLesionHiddenInTheViews(self):
        """BB-1: a segmentation that the slice views hide (a lesion of another subject, hidden by the importer) is not the
        lesion of the project thumbnail: the thumbnail shows what the views show.
        """
        from StudioLib import thumbnails

        volume = self._userVolume()
        slicer.util.setSliceViewerLayers(background=volume)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Other lesion")
        segmentation.CreateDefaultDisplayNodes()
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        lesion = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
        lesion[20, 25, 30] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        self.assertEqual(len(thumbnails.projectThumbnailSource().lesions), 1)
        segmentation.GetDisplayNode().SetVisibility2D(False)
        source = thumbnails.projectThumbnailSource()
        self.assertIsNotNone(source)
        self.assertEqual(len(source.lesions), 0)

    def test_zf_openRecoveryFolderBeforeFirstAutosave(self):
        """BB-1: Settings > Saving > Recovery "Open" did nothing while the recovery folder did not exist yet (Studio work goes to
        Projects, so a fresh install has none): it now creates the folder and shows it.
        """
        import shutil
        import tempfile

        from StudioLib import autosave_settings, projects

        base = tempfile.mkdtemp(prefix="StudioAutosaveOpen")
        folder = os.path.join(base, "StudioAutosave")
        shown = []
        realShow, realEnv = projects.showInFileManager, os.environ.get("SLICER_STUDIO_AUTOSAVE_DIR")
        projects.showInFileManager = lambda path: shown.append(path) or True
        os.environ["SLICER_STUDIO_AUTOSAVE_DIR"] = folder
        panel = autosave_settings.AutosaveSettingsPanel(None)
        try:
            self.assertFalse(os.path.exists(folder))
            panel.onOpenDirectory()
            self.assertEqual(shown, [os.path.normpath(folder)])
            self.assertTrue(os.path.isdir(folder))
        finally:
            panel.deleteLater()
            projects.showInFileManager = realShow
            if realEnv is None:
                os.environ.pop("SLICER_STUDIO_AUTOSAVE_DIR", None)
            else:
                os.environ["SLICER_STUDIO_AUTOSAVE_DIR"] = realEnv
            shutil.rmtree(base, ignore_errors=True)
