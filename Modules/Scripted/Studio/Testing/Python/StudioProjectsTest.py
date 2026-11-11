"""Projects of Slicer Studio: the Slicer Studio folder and the continuous saving of a project (A-09, D-A16).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioProjectsTest'); slicer.app.exit(0)"

Nothing runs by itself under --testing: each test creates ``AutosaveService(force=True)`` with
``studioActiveOverride=True`` (the service acts as in Studio) and ``SLICER_STUDIO_PROJECTS_ROOT`` set to a temporary
folder "Slicer Studio tést" (spaces and non-ASCII characters). The tests cover:

- names (macOS/Windows/Linux), unique names, compound extensions, cloud-synced folders, the web default;
- the layout: a project is created only for user data, named after the scan; Images/ holds a byte-for-byte copy of the
  scan (the original is untouched); kind folders (Segmentations, Maps, Markups, Tables, Models, Transforms) exist only
  when something is written into them; the scene file has relative paths only (StudioPrivacyCheck passes);
- after a save nothing is "modified since read" and the close summary says everything is saved (no question about
  unsaved changes);
- incremental saving: an edited lesion rewrites its own file only; a file another program changed is kept and Studio
  writes "<name> (Slicer Studio).<ext>"; a removed node's file goes to .studio/removed;
- copy-on-open of a scene from outside (the original is not modified), continuing a project in place, rename,
  duplicate, trash, moving the folder with the open project;
- the crash offer (a project left "open" with a stale lock), study suppression and Classic (no project is created);
- a large volume written by the background writer (marked stored without a second write on the main thread);
- a file that official Slicer saved next to the scene file is moved into its kind folder;
- a closed-surface segmentation (a file plus a folder) saved again in place; temporary folders left by a crash go;
- a detached-header volume (.nhdr) from outside is written into the project as one file;
- a colour table (.ctbl) taken over by the project names its own file, not the absolute path of the original, on its first line;
- a project loaded in Classic continues in place in Studio (no copy); the Library order follows official saves;
- a project saves when its data fits on the disk, also with less than 1 GB free (only the private autosave keeps 1 GB);
- a scene file that another program changed is kept: Studio writes "<project> (Slicer Studio).mrml" next to it;
- a file from outside that the project took over is "already open" when it is opened again, also after a restart;
- Studio's own folders (Studies/, Projects/, a project, the Slicer Studio folder) never become a study;
- moving the Slicer Studio folder into a folder inside it is refused before anything is made;
- "Save a copy as…" leaves the project as it is: the next save writes nothing again and leaves no stale copy of the scan;
- a 4D NIfTI image is saved as .seq.nrrd (official 3D Slicer 5.12 reads volume sequences from NRRD only), also in a
  project saved before with a copy of the NIfTI;
- a disk that fills up during a save: the save fails with the reason, the saved scene and lesion files stay as they were,
  and the next save once space is free writes everything again;
- Save lesion after the open project was renamed or the folder moved writes into the project where it is now (no ghost
  folder of the old name);
- a scene saved inside a project folder (a subfolder such as Exports/) opens as a copy: only the project's own scene file
  continues the project in place;
- a lesion that was cleared (or has one voxel) is saved so that the project opens again, also in official 3D Slicer;
- leaving or quitting a project that was only looked at saves nothing and keeps "Last edited" (an edit made just before
  leaving is saved);
- SIGTERM (stopping the web build) quits as File > Exit does: an edit made after the last save is in the project.
"""

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.parse

import numpy as np
import qt
import vtk

import slicer


def _plainToolTip(label):
    """The tool tip of a label as a normalized path (Qt may wrap it in rich text)."""
    import html

    return os.path.normpath(html.unescape(re.sub(r"<[^>]+>", "", str(label.toolTip))))


def _pumpFor(seconds):
    end = time.time() + seconds
    while time.time() < end:
        slicer.app.processEvents()
        time.sleep(0.01)


def _md5File(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _relativeFiles(root):
    found = []
    for directory, _folders, files in os.walk(root):
        for name in files:
            found.append(os.path.relpath(os.path.join(directory, name), root).replace(os.sep, "/"))
    return sorted(found)


def _privacyModule():
    import importlib.util

    source = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
    path = os.path.join(source, "Utilities", "Scripts", "StudioPrivacyCheck.py")
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location("StudioPrivacyCheck", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The original, kept before any test replaces the Python name (the official dialogs call the C++ function)
_REAL_OPEN_URL = qt.QDesktopServices.openUrl

class StudioProjectsTest(unittest.TestCase):
    """The Slicer Studio folder, projects and their continuous saving. Needs the main window."""

    def setUp(self):
        from StudioLib import autosave, autosave_io, projects

        self.autosave, self.io, self.projects = autosave, autosave_io, projects
        slicer.mrmlScene.Clear(0)
        self.tempDir = tempfile.mkdtemp(prefix="StudioProjectsTest-")
        self.root = os.path.join(self.tempDir, "Slicer Studio tést")
        self.userDir = os.path.join(self.tempDir, "user data é")
        os.makedirs(self.userDir)
        self._environment = {name: os.environ.get(name) for name in (projects.ROOT_ENVIRONMENT, projects.ENABLED_ENVIRONMENT)}
        os.environ[projects.ROOT_ENVIRONMENT] = self.root
        os.environ.pop(projects.ENABLED_ENVIRONMENT, None)
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.dispose()
        self.io.backgroundWriter().drain(30.0)
        slicer.mrmlScene.Clear(0)
        for name, value in self._environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tempDir, ignore_errors=True)

    #
    # Helpers
    #

    def _service(self, studio=True):
        service = self.autosave.AutosaveService(force=True, root=os.path.join(self.tempDir, "private autosave"))
        service.studioActiveOverride = studio
        service.reloadSettings()
        self.services.append(service)
        return service

    def _userVolume(self, name="sub-012_T1w", shape=(30, 40, 50), extension=".nii.gz"):
        k, j, i = np.indices(shape)
        voxels = ((i * 7 + j * 13 + k * 29) % 500).astype(np.int16)
        node = slicer.util.addVolumeFromArray(voxels, name=name)
        node.SetSpacing(1.0, 1.1, 1.2)
        path = os.path.join(self.userDir, name + extension)
        self.assertTrue(slicer.util.saveNode(node, path))
        slicer.mrmlScene.RemoveNode(node)
        return slicer.util.loadVolume(path), path

    def _lesion(self, volume, name="Lesions"):
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
        shape = slicer.util.arrayFromVolume(volume).shape
        array = np.zeros(shape, np.uint8)
        array[5:10, 6:12, 7:14] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        return segmentation, segmentId, array

    def _projectFor(self, service):
        service.poll()
        self.assertIsNotNone(service.project, "a project is created for the user's data")
        return service.project

    def _save(self, service):
        self.assertTrue(service.saveNow("test", progress=False))
        self.io.backgroundWriter().drain(30.0)

    def _projectFiles(self, project):
        return [path for path in _relativeFiles(project.path) if not path.startswith(".studio/")]

    #
    # Tests
    #

    def test_01_names(self):
        projects = self.projects
        self.assertEqual(projects.sanitizeName('a/b\\c:d*e?f"g<h>i|j. '), "a b c d e f g h i j")
        self.assertEqual(projects.sanitizeName("CON"), "_CON")
        self.assertEqual(projects.sanitizeName("   "), projects.UNTITLED_NAME)
        self.assertEqual(len(projects.sanitizeName("x" * 300)), projects.MAX_NAME_LENGTH)
        self.assertEqual(projects.splitExtension("Lesions.seg.nrrd"), ("Lesions", ".seg.nrrd"))
        self.assertEqual(projects.splitExtension("sub-01_T1w.nii.gz"), ("sub-01_T1w", ".nii.gz"))
        self.assertEqual(projects.splitExtension("Peaks.mrk.json"), ("Peaks", ".mrk.json"))
        self.assertEqual(projects.displayName("sub-012_T1w.nii.gz"), "sub-012 T1w")
        folder = os.path.join(self.tempDir, "names")
        os.makedirs(folder)
        open(os.path.join(folder, "Lesions.seg.nrrd"), "w").close()
        self.assertEqual(projects.uniqueName(folder, "Lesions.seg.nrrd"), "Lesions 2.seg.nrrd")
        self.assertEqual(projects.uniqueName(folder, "lesions.SEG.nrrd", reserved=("lesions 2.seg.nrrd",)), "lesions 3.SEG.nrrd")
        os.makedirs(os.path.join(folder, "Project"))
        self.assertEqual(projects.uniqueName(folder, "Project", isFile=False), "Project 2")
        for path, service in (("/Users/a/Library/Mobile Documents/com~apple~CloudDocs/Slicer Studio", "iCloud Drive"),
                              ("/Users/a/Dropbox (Lab)/Slicer Studio", "Dropbox"), ("C:\\Users\\a\\OneDrive - Uni\\Slicer Studio", "OneDrive"),
                              ("/Users/a/Library/CloudStorage/GoogleDrive-a@b.c/My Drive/x", "Google Drive"), ("/Users/a/Box/x", "Box"),
                              ("/home/a/Documents/Slicer Studio", None), ("/home/a/boxes/x", None)):
            self.assertEqual(projects.syncedService(path), service, path)
        # Box Drive on current macOS, but not a folder that merely starts with "box"
        self.assertEqual(projects.syncedService("/Users/ana/Library/CloudStorage/Box-Box/Lab/Slicer Studio"), "Box")
        self.assertIsNone(projects.syncedService("/Users/ana/Library/CloudStorage/Boxes/Lab"))
        # A folder reached through a symbolic link into a synced folder
        if hasattr(os, "symlink"):
            synced = os.path.join(self.tempDir, "Dropbox", "Documents")
            os.makedirs(synced)
            link = os.path.join(self.tempDir, "LinkedDocuments")
            try:
                os.symlink(synced, link)
            except OSError:
                link = None
            if link:
                self.assertEqual(projects.syncedService(os.path.join(link, "Slicer Studio")), "Dropbox")
        # The environment wins and skips the chooser
        self.assertEqual(projects.root(), os.path.normpath(self.root))
        self.assertFalse(projects.needsChooser())
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        self.assertTrue(projects.root().endswith(os.path.join("Documents", "Slicer Studio")) or projects.isWebBuild()
                        or bool(qt.QSettings().value(projects.ROOT_KEY)))
        os.environ["SLICER_STUDIO_WEB"] = "1"
        try:
            self.assertEqual(projects.defaultRoot(), "/data/Slicer Studio")
            self.assertTrue(projects.isRootChosen())
        finally:
            os.environ.pop("SLICER_STUDIO_WEB")
        os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_02_layout(self):
        projects = self.projects
        service = self._service()
        # No user data: no project (an empty drawing is not data)
        empty = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Empty")
        service.poll()
        self.assertIsNone(service.project)
        self.assertFalse(os.path.exists(self.root), "nothing is written before there is data")
        slicer.mrmlScene.RemoveNode(empty)
        volume, path = self._userVolume()
        sourceMd5, sourceTime = _md5File(path), os.stat(path).st_mtime_ns
        project = self._projectFor(service)
        self.assertEqual(project.name, "sub-012 T1w")
        self.assertEqual(os.path.dirname(project.path), os.path.join(os.path.normpath(self.root), "Projects"))
        self.assertTrue(os.path.isfile(os.path.join(self.root, projects.ABOUT_FILE)))
        self.assertIs(projects.currentProject(), project)
        self._save(service)
        files = self._projectFiles(project)
        self.assertIn("Images/sub-012_T1w.nii.gz", files)
        self.assertIn("sub-012 T1w.mrml", files)
        self.assertEqual(_md5File(os.path.join(project.path, "Images", "sub-012_T1w.nii.gz")), sourceMd5, "a byte-for-byte copy")
        self.assertEqual((_md5File(path), os.stat(path).st_mtime_ns), (sourceMd5, sourceTime), "the original is never changed")
        for kind in ("Segmentations", "Maps", "Markups", "Tables", "Models", "Transforms", "Lesions", "Exports", "Other"):
            self.assertFalse(os.path.exists(os.path.join(project.path, kind)), f"no empty {kind} folder")
        self.assertEqual(volume.GetStorageNode().GetFileName(), os.path.join(project.path, "Images", "sub-012_T1w.nii.gz"))
        self.assertFalse(volume.GetModifiedSinceRead())
        self.assertEqual(slicer.mrmlScene.GetURL(), project.scenePath)
        data = projects.readJson(projects.projectFile(project.path))
        self.assertEqual((data["format"], data["name"], data["state"], data["counts"]["scans"]), ("slicer-studio-project", "sub-012 T1w", "open", 1))
        text = open(project.scenePath, encoding="utf-8").read()
        self.assertNotIn(self.tempDir, text)
        self.assertIn('fileName="Images/sub-012_T1w.nii.gz"', text)
        privacy = _privacyModule()
        if privacy is not None:
            self.assertEqual(privacy.scan([self.root], out=lambda line: None), 0)

    def test_03_kinds_and_saved_state(self):
        import vtk

        service = self._service()
        volume, _path = self._userVolume()
        _segmentation, _segmentId, _array = self._lesion(volume)
        tmap = slicer.util.addVolumeFromArray(np.random.default_rng(1).normal(0, 3, (20, 20, 20)).astype(np.float32), name="T map")
        tmap.SetAttribute("LNM.Result", "1")
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Peaks")
        points.AddControlPoint((1, 2, 3))
        table = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "Stats")
        table.AddColumn().SetName("name")
        values = vtk.vtkDoubleArray()
        values.SetName("value")
        table.AddColumn(values)
        table.AddEmptyRow()
        table.SetCellText(0, 0, "a")
        table.SetCellText(0, 1, "1.5")
        model = slicer.modules.models.logic().AddModel(_sphere())
        model.SetName("Sphere")
        transform = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "Align")
        matrix = vtk.vtkMatrix4x4()
        matrix.SetElement(0, 3, 5.0)
        transform.SetMatrixTransformToParent(matrix)
        project = self._projectFor(service)
        self._save(service)
        files = self._projectFiles(project)
        for expected in ("Segmentations/Lesions.seg.nrrd", "Maps/T map.nrrd", "Markups/Peaks.mrk.json", "Tables/Stats.tsv", "Transforms/Align.h5"):
            self.assertIn(expected, files)
        self.assertTrue(any(path.startswith("Models/Sphere.") for path in files), files)
        # A table's schema file is part of it: it stays when the table is saved again
        self.assertIn("Tables/Stats.schema.tsv", files)
        table.SetCellText(0, 0, "b")
        self._save(service)
        self.assertIn("Tables/Stats.schema.tsv", self._projectFiles(project))
        self.assertFalse([path for path in _relativeFiles(project.path) if path.startswith(".studio/removed/")])
        self.assertFalse(slicer.mrmlScene.GetStorableNodesModifiedSinceRead(), "saved data is not 'modified since read'")
        self.assertFalse(slicer.mrmlScene.GetModifiedSinceRead(), "the scene file is saved")
        self.assertFalse(service.projectNeedsSave(includeScene=True))
        self.assertEqual(service.unsavedSummary(), "")
        # Quitting asks nothing about unsaved changes: the close summary says everything is saved
        from StudioLib import firstrun

        autosaveInstance = self.autosave._service
        self.autosave._service = service
        try:
            mode, summary = firstrun.projectCloseSummary()
        finally:
            self.autosave._service = autosaveInstance
        self.assertEqual(mode, firstrun.CLOSE_MODE_LESION_SAVED)
        self.assertIn("All changes are saved in Projects/sub-012 T1w", summary)
        self.assertEqual(service.closeFlushResult, (True, ""))

    def test_03c_running_map_or_normalize_is_named_when_quitting_or_closing_the_scene(self):
        """Quitting says that a Map network / Normalize run is going and is stopped (not only "All changes are saved");
        Ctrl+N and Ctrl+W ask first, and Cancel keeps the scene and the run (bug quit-question-midrun-says-all-saved).
        """
        import importlib
        import types

        from StudioLib import firstrun, projects_ui

        jobrunner = importlib.import_module("LesionNetworkMappingUI.jobrunner")
        runner = types.SimpleNamespace(runningJobs=lambda: [])
        job = types.SimpleNamespace(spec=types.SimpleNamespace(name="engine"), progress=0.19, isFinished=lambda: False)
        original = jobrunner.instance
        jobrunner.instance = lambda: runner
        service = self._service()
        asked = []
        originalQuestion = projects_ui._question
        projects_ui._question = lambda parent, title, text, buttons, default=0: (asked.append((title, text, default)), 1)[1]
        calls = []
        originalNew = service.newProject
        service.newProject = lambda: calls.append("new")
        originalService = projects_ui._service
        projects_ui._service = lambda: service
        try:
            self.assertEqual(firstrun.runningJobNotice(), "")
            self.assertTrue(projects_ui.newProject() is True)  # nothing runs: no question
            self.assertEqual((asked, calls), ([], ["new"]))
            runner.runningJobs = lambda: [job]
            notice = firstrun.runningJobNotice()
            self.assertIn("Map network is running (19%)", notice)
            self.assertIn("Quitting stops it", notice)
            self.assertIn("Closing the scene stops it", firstrun.runningJobNotice(sceneOnly=True))
            self.assertIn("Quitting stops it", firstrun._withRunNotice("All changes are saved in X. Quit?"))
            # Ctrl+N and Ctrl+W: asked, Cancel (the default) changes nothing
            calls.clear()
            self.assertFalse(projects_ui.newProject())
            self.assertFalse(projects_ui.closeScene())
            self.assertEqual(calls, [])
            self.assertEqual(len(asked), 2)
            self.assertIn("Map network is running", asked[0][1])
            self.assertEqual(asked[0][2], 1, "Cancel is the default")
            # A batch normalization of the Library is not stopped by closing the scene, but by quitting
            job.spec.name = "normalize-batch"
            self.assertEqual(firstrun.runningJobNotice(sceneOnly=True), "")
            self.assertIn("Normalize to MNI is running", firstrun.runningJobNotice())
        finally:
            jobrunner.instance = original
            projects_ui._question = originalQuestion
            projects_ui._service = originalService
            service.newProject = originalNew

    def test_03b_user_scan_named_like_a_template_is_user_data(self):
        """A scan the user opened from a file is user data whatever its name (FSL's MNI152_T1_2mm_brain is not a template)."""
        service = self._service()
        volume, _path = self._userVolume(name="MNI152_T1_2mm_brain")
        self.assertFalse(self.projects.isTemplateNode(volume))
        project = self._projectFor(service)
        self.assertEqual(os.path.basename(project.path), self.projects.defaultProjectName())
        self.assertNotEqual(self.projects.defaultProjectName(), self.projects.UNTITLED_NAME)
        self.assertEqual(self.projects.counts()["scans"], 1)
        # A template is still not user data: tagged, or named after the template space (a scene saved before the tag)
        volume.SetAttribute("Studio.Role", "template")
        self.assertTrue(self.projects.isTemplateNode(volume))
        volume.RemoveAttribute("Studio.Role")
        volume.SetName("MNI152NLin2009cAsym 2 mm")
        self.assertTrue(self.projects.isTemplateNode(volume))

    def test_04_incremental_and_other_program(self):
        service = self._service()
        volume, _path = self._userVolume()
        segmentation, segmentId, array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        segmentationFile = os.path.join(project.path, "Segmentations", "Lesions.seg.nrrd")
        imageFile = os.path.join(project.path, "Images", "sub-012_T1w.nii.gz")
        imageTime = os.stat(imageFile).st_mtime_ns
        before = os.stat(segmentationFile).st_mtime_ns
        time.sleep(0.02)
        array[12:14, 12:14, 12:14] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        self.assertTrue(service.projectNeedsSave())
        self._save(service)
        self.assertNotEqual(os.stat(segmentationFile).st_mtime_ns, before, "the lesion's own file is rewritten")
        self.assertEqual(os.stat(imageFile).st_mtime_ns, imageTime, "unchanged data is not written again")
        # Another program changes the file: it is kept, Studio writes its own version next to it
        time.sleep(0.02)
        with open(segmentationFile, "ab") as f:
            f.write(b"\n")
        theirs = _md5File(segmentationFile)
        array[2:4, 2:4, 2:4] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))
        self._save(service)
        self.assertEqual(_md5File(segmentationFile), theirs, "the other program's file is never overwritten")
        ours = os.path.join(project.path, "Segmentations", "Lesions (Slicer Studio).seg.nrrd")
        self.assertTrue(os.path.isfile(ours))
        self.assertEqual(segmentation.GetStorageNode().GetFileName(), ours)
        self.assertIn(("conflict", {"conflicts": [("Lesions.seg.nrrd", "Lesions (Slicer Studio).seg.nrrd")]}), announced)
        self.assertFalse(segmentation.GetModifiedSinceRead())

    def test_05_removed_node(self):
        service = self._service()
        _volume, _path = self._userVolume()
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Peaks")
        points.AddControlPoint((1, 2, 3))
        project = self._projectFor(service)
        self._save(service)
        self.assertTrue(os.path.isfile(os.path.join(project.path, "Markups", "Peaks.mrk.json")))
        slicer.mrmlScene.RemoveNode(points)
        self._save(service)
        self.assertFalse(os.path.exists(os.path.join(project.path, "Markups")), "the empty kind folder is removed")
        removed = [path for path in _relativeFiles(project.path) if path.startswith(".studio/removed/")]
        self.assertEqual([os.path.basename(path) for path in removed], ["Peaks.mrk.json"], "kept in .studio/removed, not deleted")
        self.assertNotIn("Peaks", open(project.scenePath, encoding="utf-8").read())

    def test_05b_superseded_file_leaves_the_project(self):
        """BB-1: a scan whose data are edited is written under a new name; the earlier copy ("sub-012_T1w.nii.gz" next
        to the new "sub-012_T1w.nrrd" shares a base name) goes to .studio/removed too, while the data file that a
        storage node writes next to its file (a table's schema) is kept.
        """
        service = self._service()
        volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        old = os.path.join(project.path, "Images", "sub-012_T1w.nii.gz")
        self.assertTrue(os.path.isfile(old))
        array = slicer.util.arrayFromVolume(volume)
        array[1:3, 1:3, 1:3] += 7
        slicer.util.arrayFromVolumeModified(volume)
        self.assertTrue(service.projectNeedsSave())
        self._save(service)
        files = _relativeFiles(project.path)
        imageFiles = [path for path in files if path.startswith("Images/")]
        self.assertNotIn("Images/sub-012_T1w.nii.gz", imageFiles, files)
        self.assertEqual(len(imageFiles), 1, files)
        self.assertEqual([os.path.basename(path) for path in files if path.startswith(".studio/removed/")], ["sub-012_T1w.nii.gz"], files)

    def test_05c_companion_files_are_not_orphans(self):
        from StudioLib import autosave_io

        suffixes = autosave_io._COMPANION_SUFFIXES
        self.assertIn(".schema.tsv", suffixes[".tsv"])
        self.assertIn(".raw.gz", suffixes[".nhdr"])
        self.assertNotIn(".mgz", [suffix for values in suffixes.values() for suffix in values])

    def test_06_copy_on_open_and_continue(self):
        projects = self.projects
        # A scene saved by 3D Slicer somewhere else
        volume, _path = self._userVolume("MR Head", extension=".nrrd")
        self._lesion(volume)
        outside = os.path.join(self.tempDir, "outside scene")
        os.makedirs(outside)
        scenePath = os.path.join(outside, "Head study.mrml")
        self.assertTrue(slicer.util.saveScene(scenePath))
        before = {path: (_md5File(os.path.join(outside, path)), os.stat(os.path.join(outside, path)).st_mtime_ns) for path in _relativeFiles(outside)}
        before.update({os.path.join("..", "user data é", "MR Head.nrrd"): (_md5File(os.path.join(self.userDir, "MR Head.nrrd")), None)})
        slicer.mrmlScene.Clear(0)
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))
        service.setPendingSceneSource(scenePath)
        slicer.util.loadScene(scenePath)
        self.assertIsNotNone(service.project, "a scene from elsewhere opens as a new project")
        self.assertEqual(service.project.name, "Head study")
        self.assertIn("copy", announced)
        self._save(service)
        project = service.project
        files = self._projectFiles(project)
        self.assertIn("Images/MR Head.nrrd", files)
        self.assertTrue(any(path.startswith("Segmentations/") for path in files), files)
        for path, (md5, mtime) in before.items():
            full = os.path.normpath(os.path.join(outside, path))
            self.assertEqual(_md5File(full), md5, f"{path} is not changed")
            if mtime is not None:
                self.assertEqual(os.stat(full).st_mtime_ns, mtime)
        # Closing the scene keeps the project on disk; opening it again continues in place
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        self.assertEqual(projects.readJson(projects.projectFile(projectPath))["state"], "closed")
        times = {path: os.stat(os.path.join(projectPath, path)).st_mtime_ns for path in self._projectFiles(project)}
        self.assertTrue(service.openProject(projectPath))
        self.assertEqual(service.project.path, projectPath)
        self.assertFalse(service.dirty, "a project opened in place is up to date")
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesions"))
        self.assertTrue(service.saveNow("test", progress=False))
        for path, mtime in times.items():
            if not path.endswith(".mrml"):
                self.assertEqual(os.stat(os.path.join(projectPath, path)).st_mtime_ns, mtime, f"{path} is not rewritten")

    def test_07b_permission_denied_while_renaming_is_said_in_plain_words(self):
        """BB-1: a read-only Projects folder showed "[Errno 13] Permission denied: '<old>' -> '<new>'" (error number and full
        paths) in a "Slicer error" box; the message says "Permission denied" and names the folder.
        """
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        other = projects.duplicateFolder(project.path)
        self.io.backgroundWriter().drain(30.0)
        folderName = os.path.basename(os.path.dirname(project.path))
        realRename = os.rename

        def denied(old, new, *args, **kwargs):
            raise PermissionError(13, "Permission denied", old, None, new)

        os.rename = denied
        try:
            for rename in (lambda: service.renameProject("Renamed BB"), lambda: projects.renameFolder(other, "Renamed BB 2")):
                with self.assertRaises(projects.ProjectError) as caught:
                    rename()
                text = str(caught.exception)
                self.assertEqual(text, "The project could not be renamed: Permission denied (in the folder %s)" % folderName)
                self.assertNotIn("Errno", text)
                self.assertNotIn("->", text)
        finally:
            os.rename = realRename
        self.assertTrue(os.path.isdir(project.path), "nothing was renamed")

    def test_07_rename_duplicate_trash(self):
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        oldPath = project.path
        newPath = service.renameProject("Patient A: left/right")
        self.assertEqual(os.path.basename(newPath), "Patient A left right")
        self.assertFalse(os.path.exists(oldPath))
        self.assertTrue(os.path.isfile(os.path.join(newPath, "Patient A left right.mrml")))
        self.assertFalse(os.path.exists(os.path.join(newPath, "sub-012 T1w.mrml")))
        self.assertEqual(slicer.mrmlScene.GetURL(), os.path.join(newPath, "Patient A left right.mrml"))
        self.assertTrue(volume.GetStorageNode().GetFileName().startswith(newPath + os.sep))
        self._save(service)
        self.assertEqual(projects.readJson(projects.projectFile(newPath))["name"], "Patient A left right")
        # Another project of the folder: duplicate, rename and trash it (never the open one)
        duplicated = []
        copyPath = projects.duplicateFolder(newPath, onDone=duplicated.append)
        self.io.backgroundWriter().drain(30.0)
        self.assertEqual(duplicated, [copyPath])
        self.assertTrue(os.path.isfile(os.path.join(copyPath, os.path.basename(copyPath) + ".mrml")))
        self.assertFalse(os.path.exists(os.path.join(copyPath, ".studio", "lock")))
        renamed = projects.renameFolder(copyPath, "Copy B")
        self.assertTrue(os.path.isfile(os.path.join(renamed, "Copy B.mrml")))
        with self.assertRaises(projects.ProjectError):
            projects.trashFolder(newPath)
        self.assertTrue(projects.trashFolder(renamed, confirmDelete=lambda: True))
        self.assertFalse(os.path.exists(renamed))
        names = [info.name for info in projects.listProjects()]
        self.assertEqual(names, ["Patient A left right"])
        # The renamed project loads with all its data
        slicer.mrmlScene.Clear(0)
        self.assertTrue(service.openProject(newPath))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesions"))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("sub-012_T1w"))

    @contextlib.contextmanager
    def _caseInsensitiveFileSystem(self):
        """Simulate APFS/HFS+ (macOS default): a name matches an entry that differs only in letter case.

        ``os.path.normcase`` does not fold case there, so only the file system calls ``exists`` and ``samefile`` tell.
        """
        realExists, realSamefile = os.path.exists, os.path.samefile

        def resolve(path):
            path = os.path.normpath(str(path))
            if realExists(path):
                return path
            parent, name = os.path.split(path)
            if not parent or parent == path:
                return None
            parent = resolve(parent)
            if parent is None or not os.path.isdir(parent):
                return None
            for entry in os.listdir(parent):
                if entry.lower() == name.lower():
                    return os.path.join(parent, entry)
            return None

        def exists(path):
            return resolve(path) is not None

        def samefile(first, second):
            return realSamefile(resolve(first) or first, resolve(second) or second)

        os.path.exists, os.path.samefile = exists, samefile
        try:
            yield
        finally:
            os.path.exists, os.path.samefile = realExists, realSamefile

    def test_07b_case_only_rename(self):
        """Renaming a project by its letter case only ("Study" to "study") works on a case-insensitive file system."""
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        studyPath = service.renameProject("Study")
        self.assertEqual(os.path.basename(studyPath), "Study")
        otherPath = projects.duplicateFolder(studyPath)
        self.io.backgroundWriter().drain(30.0)
        otherPath = projects.renameFolder(otherPath, "Other")
        with self._caseInsensitiveFileSystem():
            # A closed project
            closedPath = projects.renameFolder(otherPath, "other")
            self.assertEqual(os.path.basename(closedPath), "other")
            self.assertTrue(os.path.isfile(os.path.join(closedPath, "other.mrml")))
            self.assertEqual(projects.readJson(projects.projectFile(closedPath))["name"], "other")
            # Another project with the same name apart from the case is still refused
            with self.assertRaises(projects.ProjectError):
                projects.renameFolder(closedPath, "STUDY")
            with self.assertRaises(projects.ProjectError):
                service.renameProject("OTHER")
            # The open project
            lowerPath = service.renameProject("study")
            self.assertEqual(os.path.basename(lowerPath), "study")
            self.assertEqual(project.name, "study")
            self.assertTrue(os.path.isfile(os.path.join(lowerPath, "study.mrml")))
            self._save(service)
            self.assertEqual(projects.readJson(projects.projectFile(lowerPath))["name"], "study")
        self.assertIn("study", os.listdir(os.path.dirname(lowerPath)))
        self.assertNotIn("Study", os.listdir(os.path.dirname(lowerPath)))
        # Case-sensitive file system: two projects whose names differ only in case stay two projects
        self.assertFalse(projects.isSamePath(lowerPath, closedPath))
        # ... but a rename never creates such a pair, even where the file system tells them apart (Linux)
        with self.assertRaises(projects.ProjectError):
            service.renameProject("OTHER")
        with self.assertRaises(projects.ProjectError):
            projects.renameFolder(closedPath, "STUDY")

    def test_07bb_duplicate_is_complete_before_it_gets_its_name(self):
        """A Duplicate commits with its final rename: the copy is complete (scene named, project.json closed) before it."""
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        self._projectFor(service)
        self._save(service)
        studyPath = service.renameProject("Study")
        seen = []
        realRename = os.rename

        def spyRename(source, target, *args, **kwargs):
            if str(source).endswith(projects.COPY_MARKER):
                data = projects.readProjectJson(source) or {}
                seen.append((sorted(n for n in os.listdir(source) if n.endswith(".mrml")), data.get("name"), data.get("state")))
            return realRename(source, target, *args, **kwargs)

        os.rename = spyRename
        try:
            copyPath = projects.duplicateFolder(studyPath)
            self.io.backgroundWriter().drain(30.0)
        finally:
            os.rename = realRename
        self.assertEqual(seen, [(["Study copy.mrml"], "Study copy", "closed")])
        self.assertTrue(os.path.isfile(os.path.join(copyPath, "Study copy.mrml")))

    def test_07c_rename_refuses_a_name_that_differs_only_in_letter_case(self):
        """No rename leaves two project folders whose names differ only in case (they cannot coexist on macOS or Windows)."""
        projects = self.projects
        root = tempfile.mkdtemp(dir=os.environ.get("TMPDIR") or None)
        try:
            for name in ("MRHead", "Other"):
                os.makedirs(os.path.join(root, name))
                with open(os.path.join(root, name, "project.json"), "w") as handle:
                    handle.write('{"name": "%s"}' % name)
            with self.assertRaises(projects.ProjectError):
                projects.renameFolder(os.path.join(root, "Other"), "MRHEAD")
            self.assertEqual(sorted(os.listdir(root)), ["MRHead", "Other"])
            self.assertTrue(projects.nameTaken(root, "mrhead"))
            self.assertFalse(projects.nameTaken(root, "mrhead", os.path.join(root, "MRHead")))
            self.assertFalse(projects.nameTaken(root, "Fresh"))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_08_move_root(self):
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        os.makedirs(os.path.join(self.root, "Studies", "Study A"))
        with open(os.path.join(self.root, "Studies", "Study A", "study.json"), "w") as f:
            json.dump({"name": "Study A"}, f)
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        try:
            newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
            moved = service.moveRoot(newRoot, moveContent=True)
            self.assertEqual(sorted(moved), sorted(["Projects", "Studies", projects.ABOUT_FILE]))
            self.assertEqual(projects.root(), os.path.normpath(newRoot))
            self.assertTrue(service.project.path.startswith(os.path.normpath(newRoot) + os.sep))
            self.assertTrue(os.path.isfile(os.path.join(newRoot, "Studies", "Study A", "study.json")))
            self.assertFalse(os.path.exists(os.path.join(self.root, "Projects")))
            self.assertTrue(volume.GetStorageNode().GetFileName().startswith(os.path.normpath(newRoot)))
            self._save(service)
            self.assertIs(service.project, project)
            self.assertTrue(os.path.isfile(project.scenePath))
        finally:
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_08b_move_root_registered_paths(self):
        """Move: a connectome registered in LNM/ (where Convert and "Get a connectome…" put it) and other path settings
        inside the moved items follow the folder; paths outside the moved items stay as they are.
        """
        import LesionNetworkMapping as lnm

        projects = self.projects
        service = self._service()
        keys = self.autosave.MOVED_PATH_KEYS
        settings = qt.QSettings()
        saved = {key: settings.value(key) for key in keys}
        known = dict(lnm.LesionNetworkMappingLogic._knownConnectomes)
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, self.root)
        try:
            for key in keys:
                settings.remove(key)
            logic = lnm.LesionNetworkMappingLogic()
            bundlePath = os.path.join(self.root, "LNM", "connectomes", "Demo.lnmc")
            logic.createDemoConnectome(bundlePath, nSubjects=2, register=True, shape=(8, 9, 8), runs=(12,), affine=None)
            identifier = logic.rememberConnectome(logic.openBundle(bundlePath))
            outside = os.path.join(self.tempDir, "elsewhere", "Other.lnmc")
            notMoved = os.path.join(self.root, "Mine", "Kept.lnmc")  # in the folder, but not in an item that moves
            settings.setValue(lnm.CONNECTOMES_SETTING, logic.registeredConnectomes() + [outside, notMoved])
            os.makedirs(os.path.join(self.root, "Studies", "Study A"))
            with open(os.path.join(self.root, "Studies", "Study A", "study.json"), "w") as f:
                json.dump({"name": "Study A"}, f)
            settings.setValue(lnm.COHORT_STUDY_SETTING, os.path.join(self.root, "Studies", "Study A"))
            settings.setValue("Studio/LNM/FetchDestination", os.path.join(self.root, "LNM", "Connectomes"))
            newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
            moved = service.moveRoot(newRoot, moveContent=True)
            self.assertIn("LNM", moved)
            newBundle = os.path.join(os.path.normpath(newRoot), "LNM", "connectomes", "Demo.lnmc")
            self.assertTrue(os.path.isfile(os.path.join(newBundle, "connectome.json")))
            self.assertEqual(lnm.LesionNetworkMappingLogic.registeredConnectomes(), [newBundle, outside, notMoved])
            self.assertEqual(lnm.LesionNetworkMappingLogic._knownConnectomes[identifier], newBundle)
            self.assertEqual(logic.resolveConnectome(identifier), newBundle)
            # A later session (nothing remembered): the registered bundle is found at its new place
            lnm.LesionNetworkMappingLogic._knownConnectomes.clear()
            self.assertEqual(lnm.LesionNetworkMappingLogic().resolveConnectome(identifier), newBundle)
            self.assertEqual(str(settings.value(lnm.COHORT_STUDY_SETTING)), os.path.join(os.path.normpath(newRoot), "Studies", "Study A"))
            self.assertEqual(str(settings.value("Studio/LNM/FetchDestination")), os.path.join(os.path.normpath(newRoot), "LNM", "Connectomes"))
            # Settings > Change… > Move without the autosave service (it moves the folder itself): the paths follow too
            from unittest import mock

            from StudioLib import projects_ui

            thirdRoot = os.path.join(self.tempDir, "third", "Slicer Studio")

            class FakeChooser:
                path = thirdRoot
                continueButton = mock.Mock()
                setWindowTitle = mock.Mock()
                choose = mock.Mock()
                exec = mock.Mock()
                exec_ = mock.Mock()
                deleteLater = mock.Mock()

                def __init__(self, *args, **kwargs):
                    pass

            with mock.patch.object(self.autosave, "_service", None), \
                    mock.patch.object(projects_ui, "FolderChooserDialog", FakeChooser), \
                    mock.patch.object(projects_ui, "_question", lambda *args, **kwargs: 0), \
                    mock.patch.object(projects_ui, "_runWithProgress", lambda function, *args, **kwargs: function()):
                self.assertEqual(projects_ui.changeRootDialog(None), thirdRoot)
            thirdBundle = os.path.join(os.path.normpath(thirdRoot), "LNM", "connectomes", "Demo.lnmc")
            self.assertTrue(os.path.isfile(os.path.join(thirdBundle, "connectome.json")))
            self.assertEqual(lnm.LesionNetworkMappingLogic.registeredConnectomes(), [thirdBundle, outside, notMoved])
            self.assertEqual(lnm.LesionNetworkMappingLogic().resolveConnectome(identifier), thirdBundle)
        finally:
            lnm.LesionNetworkMappingLogic._knownConnectomes.clear()
            lnm.LesionNetworkMappingLogic._knownConnectomes.update(known)
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_09_crash_offer(self):
        projects = self.projects
        service = self._service()
        _volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        path = project.path
        # A crash: the lock of a process that is gone and project.json still "open"
        project.unlock()
        service.project = None
        service.projectWriter = None
        projects.setCurrentProject(None)
        self.io.backgroundWriter().drain(10.0)
        data = projects.readJson(projects.projectFile(path))
        data["state"] = "open"
        self.io.atomicWriteJson(projects.projectFile(path), data)
        with open(os.path.join(path, ".studio", "lock"), "w") as f:
            f.write("999999\nSlicer\n" + qt.QSysInfo.machineHostName() + "\n")
        offered = []
        service.setProjectPresenter(offered.append)
        crashed = service.scanProjectsAtStartup()
        self.assertEqual([info.name for info in crashed], ["sub-012 T1w"])
        self.assertEqual(len(offered), 1)
        self.io.backgroundWriter().drain(10.0)
        self.assertEqual(projects.readJson(projects.projectFile(path))["state"], "crashed", "offered once")
        self.assertEqual(service.scanProjectsAtStartup(), [])
        self.assertTrue(service.openProject(path))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("sub-012_T1w"))
        # A project open in another (live) Slicer Studio is never offered nor taken
        other = projects.createProject("Busy")
        self.assertTrue(other.lock())
        try:
            self.assertTrue(projects.isLockedByOther(other.path))
            infos = [info for info in projects.listProjects() if info.name == "Busy"]
            self.assertEqual(infos[0].state, "open")
            self.assertNotIn("Busy", [info.name for info in service.scanProjectsAtStartup()])
        finally:
            other.unlock()

    def test_10_study_and_classic(self):
        service = self._service(studio=False)
        volume, _path = self._userVolume()
        service.poll()
        self.assertIsNone(service.project, "Classic never creates a project")
        self.assertFalse(os.path.exists(os.path.join(self.root, "Projects")))
        service.studioActiveOverride = True
        service._studyOpen = lambda: True
        service._lastProjectCheck = None
        service.poll()
        self.assertIsNone(service.project, "no project while a study is open")
        service._studyOpen = lambda: False
        service._lastProjectCheck = None
        project = self._projectFor(service)
        self._save(service)
        # A study opens: the project is saved and stops
        service._studyOpen = lambda: True
        self._lesion(volume)
        service.poll()
        self.assertIsNone(service.project)
        self.assertTrue(os.path.isfile(os.path.join(project.path, "Segmentations", "Lesions.seg.nrrd")))
        # Projects turned off: the private recovery autosave as before
        service._studyOpen = lambda: False
        os.environ[self.projects.ENABLED_ENVIRONMENT] = "0"
        service._lastProjectCheck = None
        service.poll()
        self.assertIsNone(service.project)

    def test_11_large_volume(self):
        service = self._service()
        _volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        big = slicer.util.addVolumeFromArray(np.random.default_rng(2).random((64, 128, 128)).astype(np.float32), name="Derived")
        self.assertTrue(service.autosaveNow("test"))
        self.assertTrue(service.waitForIdle(120.0))
        metrics = service.metrics["last"]
        self.assertLess(metrics["maxSliceMs"], 100.0, metrics)
        self.assertGreater(metrics["workerMs"], 0.0)
        derived = os.path.join(project.path, "Images", "Derived.nrrd")
        self.assertTrue(os.path.isfile(derived))
        self.assertFalse(big.GetModifiedSinceRead(), "marked stored without a second write")
        self.assertEqual(big.GetStorageNode().GetFileName(), derived)
        loaded = slicer.util.loadVolume(derived)
        self.assertTrue(np.array_equal(slicer.util.arrayFromVolume(loaded), slicer.util.arrayFromVolume(big)))

    def test_12_official_save_moved_into_kind_folder(self):
        service = self._service()
        _volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        # 3D Slicer's Save dialog put a new node's file next to the scene file
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Targets")
        points.AddControlPoint((4, 5, 6))
        self.assertTrue(slicer.util.saveNode(points, os.path.join(project.path, "Targets.mrk.json")))
        self.assertFalse(points.GetModifiedSinceRead())
        self.assertTrue(service.saveNow("test", progress=False))
        self.io.backgroundWriter().drain(10.0)
        self.assertFalse(os.path.exists(os.path.join(project.path, "Targets.mrk.json")))
        self.assertTrue(os.path.isfile(os.path.join(project.path, "Markups", "Targets.mrk.json")))
        self.assertEqual(points.GetStorageNode().GetFileName(), os.path.join(project.path, "Markups", "Targets.mrk.json"))
        text = open(project.scenePath, encoding="utf-8").read()
        self.assertIn('fileName="Markups/Targets.mrk.json"', text)

    def test_12b_view_only_change_is_saved_by_save_now(self):
        """BB-1: what a view shows (overlay, background, opacity) is not part of the autosave fingerprint, but Save now
        and leaving write it to the scene, as official 3D Slicer does: they do not say "nothing to save".
        """
        service = self._service()
        volume, _path = self._userVolume()
        other = slicer.util.addVolumeFromArray(np.arange(8 * 9 * 10, dtype=np.int16).reshape(8, 9, 10), name="Second scan")
        project = self._projectFor(service)
        self._save(service)
        self.assertFalse(service.projectNeedsSave(includeScene=True), "nothing changed since the save")
        composite = slicer.app.layoutManager().sliceWidget("Red").mrmlSliceCompositeNode()
        composite.SetBackgroundVolumeID(other.GetID())
        composite.SetForegroundVolumeID(volume.GetID())
        composite.SetForegroundOpacity(0.4)
        self.assertFalse(service.projectNeedsSave(), "the autosave does not count a view change")
        self.assertTrue(service.projectNeedsSave(includeScene=True), "Save now and leaving do")
        self._save(service)
        self.assertFalse(service.projectNeedsSave(includeScene=True))
        text = open(project.scenePath, encoding="utf-8").read()
        self.assertIn('foregroundOpacity="0.4"', text)

    def test_13_ui(self):
        """The Library section, the folder chooser and the save status of a project (built with the ui builders)."""
        from StudioLib import projects_ui

        from StudioLib import draw, library

        service = self._service()
        volume, _path = self._userVolume()
        segmentation, _segmentId, _array = self._lesion(volume)
        self._projectFor(service)
        self._save(service)
        # The lesion is saved with the project: the Lesion card says so, and closing the scene asks nothing (it saves first)
        self.assertTrue(draw.saveStateText(segmentation)[0].startswith("Saved in the project"), draw.saveStateText(segmentation))
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        try:
            self.assertIs(library._projectService(), service)
        finally:
            self.autosave._service = running
        section = projects_ui.ProjectsSection()
        try:
            section.refresh()
            self.assertEqual(section.widget.objectName, "studioProjectsSection")
            cards = section.cards()
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0].objectName, "studioProjectCard")
            self.assertTrue(cards[0].property("studioCurrent"))
            texts = [label.text for label in cards[0].findChildren("QLabel")]
            self.assertIn("sub-012 T1w", texts)
            self.assertTrue(any("1 scan · 1 lesion" in text for text in texts), texts)
            self.assertEqual([text for text, _callback in section.menuActions(cards[0].property("studioProjectPath"))][:3],
                             ["Open", "Rename…", "Duplicate"])
        finally:
            section.dispose()
            section.widget.deleteLater()
        dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Dropbox", "Slicer Studio"))
        try:
            self.assertEqual(dialog.objectName, "StudioProjectsFolderDialog")
            self.assertFalse(dialog.syncedLabel.isHidden())
            self.assertIn("Dropbox", dialog.syncedLabel.text)
            dialog.setPath(os.path.join(self.tempDir, "Slicer Studio"))
            self.assertTrue(dialog.syncedLabel.isHidden())
            dialog.reject()  # Escape accepts the proposal
            self.assertEqual(dialog.result(), qt.QDialog.Accepted)
        finally:
            dialog.deleteLater()
        # Web build: a folder outside /data is warned about in the chooser (lost when the container is removed); /data is not
        projects = self.projects
        webValue = os.environ.get("SLICER_STUDIO_WEB")
        os.environ["SLICER_STUDIO_WEB"] = "1"
        try:
            self.assertEqual(projects.webFolderNotice("/data/Slicer Studio"), "")
            self.assertEqual(projects.webFolderNotice("/data"), "")
            self.assertNotEqual(projects.webFolderNotice("/database/Slicer Studio"), "")
            dialog = projects_ui.FolderChooserDialog(proposal="/tmp/Slicer Studio")
            try:
                self.assertFalse(dialog.syncedNotice.isHidden())
                self.assertIn("outside /data", dialog.syncedLabel.text)
                self.assertIn("lost when the container is removed", dialog.syncedLabel.text)
                dialog.setPath("/data/Slicer Studio")
                self.assertTrue(dialog.syncedNotice.isHidden())
            finally:
                dialog.deleteLater()
            os.environ.pop("SLICER_STUDIO_WEB")
            if not os.path.exists("/.dockerenv"):
                self.assertEqual(projects.webFolderNotice("/tmp/Slicer Studio"), "", "no web warning on the desktop")
        finally:
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue
        # The warning appears while the dialog is open (Choose… on a synced folder): the dialog grows with it and the rows of
        # "Inside this folder" keep their size
        dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Slicer Studio"))
        try:
            dialog.show()
            slicer.app.processEvents()
            plainHeight = dialog.height
            dialog.setPath(os.path.join(self.tempDir, "Dropbox", "Lab"))
            slicer.app.processEvents()
            self.assertFalse(dialog.syncedNotice.isHidden())
            self.assertGreaterEqual(dialog.height, plainHeight + dialog.syncedNotice.height)
            card = dialog.findChild("QFrame", "StudioProjectsFolderInsideCard")
            squeezed = [(label.text, label.height, label.sizeHint.height()) for label in card.findChildren("QLabel")
                        if label.text and label.height < label.sizeHint.height()]
            self.assertEqual(squeezed, [])
        finally:
            dialog.hide()
            dialog.deleteLater()
        status = service.status()
        self.assertEqual(status.projectName, "sub-012 T1w")
        text, level, toolTip, _icon = projects_ui.projectStatus(status)
        self.assertEqual((text, level), ("Saved", "ok"))
        self.assertIn("Projects/sub-012 T1w", toolTip)
        items = {item["id"] for item in projects_ui.paletteItems()}
        self.assertTrue({"projects.new", "projects.open", "projects.rename", "projects.showFolder", "projects.changeFolder"} <= items)

    def test_13a_keyboard_focus_ring_shows_on_the_open_projects_card(self):
        """The card of the open project (studioCurrent) keeps its accent border, and a keyboard focus on it must still show
        (grid and list): the studioCurrent rules used to come after the :focus rules and replaced the ring.
        """
        from StudioLib import projects_ui

        service = self._service()
        self._userVolume()
        self._projectFor(service)
        self._save(service)
        section = projects_ui.ProjectsSection()
        remembered = section.viewMode  # setViewMode remembers the view in the settings
        try:
            section.widget.resize(900, 500)
            section.widget.show()
            section.widget.activateWindow()
            section.widget.raise_()
            slicer.app.processEvents()
            for mode in ("grid", "list"):
                section.setViewMode(mode)
                section.refresh()
                slicer.app.processEvents()
                cards = section.cards()
                self.assertEqual(len(cards), 1)
                card = cards[0]
                self.assertTrue(card.property("studioCurrent"))
                self.assertEqual(card.property("studioProjectView"), mode)
                card.clearFocus()
                slicer.app.processEvents()
                before = card.grab().toImage()
                card.setFocus(qt.Qt.TabFocusReason)
                for _attempt in range(20):  # the window of a bare widget is activated by the window system, which takes a moment
                    slicer.app.processEvents()
                    if card.hasFocus():
                        break
                    section.widget.activateWindow()
                    time.sleep(0.05)
                self.assertTrue(card.hasFocus(), mode)
                after = card.grab().toImage()
                changed = sum(1 for x in range(after.width()) for y in range(after.height()) if before.pixel(x, y) != after.pixel(x, y))
                self.assertGreater(changed, 50, f"the focus ring of the open project's card is invisible in the {mode} view")
        finally:
            section.setViewMode(remembered)
            section.widget.hide()
            section.dispose()
            section.widget.setParent(None)
            section.widget.deleteLater()
            qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.app.processEvents()

    def test_13b_folder_chooser_rows_follow_a_larger_application_font(self):
        """With a large (18 px) application font the names of "Inside this folder" ("About this folder") are not cut (BB-1)."""
        from StudioLib import projects_ui

        base = qt.QFont(qt.QApplication.font())
        large = qt.QFont(base)
        large.setPixelSize(18)
        qt.QApplication.setFont(large)
        dialog = None
        try:
            slicer.app.processEvents()
            dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Slicer Studio"))
            dialog.show()
            for _step in range(5):
                slicer.app.processEvents()
            card = dialog.findChild("QFrame", "StudioProjectsFolderInsideCard")
            names = [label for label in card.findChildren("QLabel") if label.text in ("Projects", "Studies", "LNM", "About this folder")]
            self.assertEqual(len(names), 4)
            for label in names:
                needed = qt.QFontMetrics(label.font).horizontalAdvance(label.text)
                self.assertGreaterEqual(label.width, needed, f"{label.text} is cut")
        finally:
            if dialog is not None:
                dialog.hide()
                dialog.deleteLater()
            qt.QApplication.setFont(base)
            slicer.app.processEvents()

    def test_13bb_project_card_body_follows_a_larger_application_font(self):
        """At a 125% or 150% application font the name of a grid card starts below the thumbnail (the card was a fixed
        128 + 54 px, so the 22 px name label slid under the thumbnail); the card keeps its 182 px at the default font, and a
        font change while the Library shows follows (BB-1).
        """
        from StudioLib import projects_ui, ui

        service = self._service()
        self._userVolume()
        self._projectFor(service)
        self._save(service)
        base = qt.QFont(qt.QApplication.font())
        section = projects_ui.ProjectsSection()
        remembered = section.viewMode  # setViewMode remembers the view in the settings

        def check(message):
            slicer.app.processEvents()
            card = section.cards()[0]
            self.assertEqual(card.property("studioProjectView"), "grid")
            thumbnail = card.findChild("QLabel", "studioProjectCardThumbnail")
            name = card.findChild("QLabel", "studioProjectCardName")
            meta = card.findChild("QLabel", "studioProjectCardMeta")
            thumbnailBottom = thumbnail.mapTo(card, qt.QPoint(0, thumbnail.height)).y()
            self.assertGreaterEqual(name.mapTo(card, qt.QPoint(0, 0)).y(), thumbnailBottom, f"{message}: the name is under the thumbnail")
            self.assertGreaterEqual(name.height, qt.QFontMetrics(name.font).height(), f"{message}: the name is squeezed")
            self.assertGreaterEqual(meta.height, qt.QFontMetrics(meta.font).height(), f"{message}: the meta line is squeezed")
            self.assertLessEqual(meta.mapTo(card, qt.QPoint(0, meta.height)).y(), card.height, f"{message}: the meta line leaves the card")
            return card.height

        try:
            section.setViewMode("grid")
            section.widget.resize(900, 500)
            section.widget.show()
            section.refresh()
            defaultHeight = check("default font")
            if ui.applicationFontPx() == 14:
                self.assertEqual(defaultHeight, projects_ui.CARD_THUMBNAIL_HEIGHT + projects_ui.CARD_BODY_HEIGHT)
            previous = defaultHeight
            for pixelSize in (17, 21):
                font = qt.QFont(base)
                font.setPixelSize(pixelSize)
                qt.QApplication.setFont(font)  # while the section shows: the cards follow
                for _step in range(5):
                    slicer.app.processEvents()
                height = check(f"{pixelSize} px font, changed while shown")
                self.assertGreater(height, previous, f"the card does not grow at {pixelSize} px")
                previous = height
                section.refresh()  # the cards of the new font
                section.setViewMode("grid")
                self.assertEqual(check(f"{pixelSize} px font, rebuilt"), height)
        finally:
            qt.QApplication.setFont(base)
            section.setViewMode(remembered)
            section.widget.hide()
            section.dispose()
            section.widget.setParent(None)
            section.widget.deleteLater()
            qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.app.processEvents()

    def test_13bc_project_card_elided_name_fits_its_label(self):
        """An elided card name, ellipsis included, is never wider than its label, whatever the width of the card (a name
        elided to card.width - 26 was 1 to 3 px too wide, and the last dot of the "…" was cut; BB-1).
        """
        from StudioLib import projects_ui

        service = self._service()
        self._userVolume()
        self._projectFor(service)
        service.renameProject("Very long project name for the multicentre post-stroke aphasia cohort 脳卒中患者の拡散強調画像")
        self._save(service)
        section = projects_ui.ProjectsSection()
        remembered = section.viewMode
        try:
            section.setViewMode("grid")
            section.widget.show()
            section.refresh()
            elidedCards = 0
            for width in range(330, 700, 7):
                section.widget.resize(width, 500)
                for _step in range(4):
                    slicer.app.processEvents()
                for card in section.cards():
                    name = card.findChild("QLabel", "studioProjectCardName")
                    elidedCards += name.text.endswith("…")
                    need = qt.QFontMetrics(name.font).horizontalAdvance(name.text)
                    self.assertLessEqual(need, name.contentsRect().width, f"{width}: the elided name is wider than its label")
            self.assertGreater(elidedCards, 0, "no name was elided: the test checks nothing")
        finally:
            section.setViewMode(remembered)
            section.widget.hide()
            section.dispose()
            section.widget.setParent(None)
            section.widget.deleteLater()
            qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.app.processEvents()

    def test_13c_folder_chooser_enter_presses_continue(self):
        """Enter in the folder chooser presses the primary "Continue" and does not open the folder picker, whichever button has
        the initial focus; Cancel (Change folder) is never the default either (BB-1).
        """
        from StudioLib import projects_ui

        opened = []
        original = qt.QFileDialog.getExistingDirectory
        qt.QFileDialog.getExistingDirectory = staticmethod(lambda *args, **kwargs: (opened.append(1), "")[1])
        try:
            for cancellable in (False, True):
                dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Slicer Studio"), cancellable=cancellable)
                accepted = []
                dialog.connect("accepted()", lambda accepted=accepted: accepted.append(1))
                try:
                    dialog.show()
                    for _step in range(5):
                        slicer.app.processEvents()
                    self.assertFalse(dialog.chooseButton.autoDefault)
                    self.assertTrue(dialog.continueButton.isDefault())
                    self.assertFalse(dialog.chooseButton.isDefault())
                    self.assertEqual(qt.QApplication.focusWidget(), dialog.continueButton, "the keyboard focus starts on Continue")
                    # Enter on the widget that has the keyboard focus (the first button of the focus chain)
                    for target in (qt.QApplication.focusWidget() or dialog, dialog.chooseButton):
                        opened.clear()
                        accepted.clear()
                        dialog.setResult(qt.QDialog.Rejected)
                        for eventType in (qt.QEvent.KeyPress, qt.QEvent.KeyRelease):
                            qt.QApplication.sendEvent(target, qt.QKeyEvent(eventType, qt.Qt.Key_Return, qt.Qt.NoModifier))
                        slicer.app.processEvents()
                        self.assertEqual(opened, [], f"Enter opened the folder picker (cancellable={cancellable})")
                        self.assertEqual(accepted, [1], f"Enter did not press Continue (cancellable={cancellable})")
                        dialog.show()
                        slicer.app.processEvents()
                finally:
                    dialog.hide()
                    dialog.deleteLater()
        finally:
            qt.QFileDialog.getExistingDirectory = original
            slicer.app.processEvents()

    def test_14_closed_surface_segmentation(self):
        """A segmentation written as "<name>.seg.vtm" plus a "<name>.seg/" folder is saved again in place (the folder is
        replaced) and the project reloads with every segment; nothing refers to a temporary folder.
        """
        service = self._service()
        _volume, _path = self._userVolume()
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Surfaces")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.GetSegmentation().SetSourceRepresentationName("Closed surface")
        logic = slicer.modules.segmentations.logic()
        for center in ((0, 0, 0), (20, 0, 0)):
            model = slicer.modules.models.logic().AddModel(_sphere(center))
            self.assertTrue(logic.ImportModelToSegmentationNode(model, segmentation))
            slicer.mrmlScene.RemoveNode(model)
            project = self._projectFor(service)
            self._save(service)
            self.assertFalse(segmentation.GetModifiedSinceRead())
            self.assertEqual(segmentation.GetStorageNode().GetFileName(), os.path.join(project.path, "Segmentations", "Surfaces.seg.vtm"))
        files = _relativeFiles(project.path)
        self.assertFalse([path for path in files if "studio-tmp" in path], files)
        self.assertFalse([path for path in files if path.startswith(".studio/removed/")], files)
        self.assertEqual(len([path for path in files if path.startswith("Segmentations/Surfaces.seg/")]), 2, files)
        self.assertNotIn("studio-tmp", open(project.scenePath, encoding="utf-8").read())
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(projectPath))
        reloaded = slicer.mrmlScene.GetFirstNodeByName("Surfaces")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.GetSegmentation().GetNumberOfSegments(), 2)
        # A temporary folder left by a save that was interrupted is deleted when the project is opened again
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        leftover = os.path.join(projectPath, "Segmentations", self.io.PROJECT_TEMPORARY_PREFIX + "deadbeef")
        os.makedirs(leftover)
        open(os.path.join(leftover, "Surfaces.seg.vtm"), "w").close()
        self.assertTrue(service.openProject(projectPath))
        self.io.backgroundWriter().drain(10.0)
        self.assertFalse(os.path.exists(leftover))

    def test_15_detached_header_from_outside(self):
        """A volume read from a detached header (.nhdr + data file) outside the folder is written into the project as a
        self-contained file (copying the header alone would break it).
        """
        service = self._service()
        volume, _path = self._userVolume("Head", extension=".nhdr")
        self.assertTrue([name for name in os.listdir(self.userDir) if name.startswith("Head") and not name.endswith(".nhdr")])
        expected = slicer.util.arrayFromVolume(volume).copy()
        project = self._projectFor(service)
        self._save(service)
        files = self._projectFiles(project)
        self.assertFalse([name for name in files if name.endswith(".nhdr")], files)
        self.assertIn("Images/Head.nrrd", files)
        self.assertEqual(volume.GetStorageNode().GetFileName(), os.path.join(project.path, "Images", "Head.nrrd"))
        self.assertFalse(volume.GetModifiedSinceRead())
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(projectPath))
        reloaded = slicer.mrmlScene.GetFirstNodeByName("Head")
        self.assertIsNotNone(reloaded)
        self.assertTrue(np.array_equal(slicer.util.arrayFromVolume(reloaded), expected))

    def test_15a_image_pair_data_file_from_outside(self):
        """A volume opened from the .img of a NIfTI pair (.hdr + .img) outside the folder is written into the project as a
        self-contained file: copying the .img alone left the scene pointing at a file without its header, which neither
        Slicer Studio nor official 3D Slicer can read.
        """
        for name in ("Head.img", "Head.IMG", "Head.img.gz", "Head.hdr", "Head.hdr.gz", "Head.nhdr"):
            self.assertTrue(self.io.ProjectWriter._hasDataFiles(os.path.join(self.userDir, name)), name)
        for name in ("Head.nrrd", "Head.nii.gz", "Head.nii", ".img"):
            self.assertFalse(self.io.ProjectWriter._hasDataFiles(os.path.join(self.userDir, name)), name)
        service = self._service()
        volume, _path = self._userVolume("Pair", extension=".hdr")
        slicer.mrmlScene.RemoveNode(volume)
        imagePath = os.path.join(self.userDir, "Pair.img")
        self.assertTrue(os.path.isfile(imagePath), os.listdir(self.userDir))
        volume = slicer.util.loadVolume(imagePath)
        self.assertTrue(volume.GetStorageNode().GetFileName().endswith("Pair.img"))
        name = volume.GetName()
        expected = slicer.util.arrayFromVolume(volume).copy()
        project = self._projectFor(service)
        self._save(service)
        files = self._projectFiles(project)
        self.assertFalse([name for name in files if name.lower().endswith((".img", ".hdr"))], files)
        self.assertIn(f"Images/{name}.nrrd", files)
        self.assertEqual(volume.GetStorageNode().GetFileName(), os.path.join(project.path, "Images", f"{name}.nrrd"))
        self.assertFalse(volume.GetModifiedSinceRead())
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(projectPath))
        reloaded = slicer.mrmlScene.GetFirstNodeByName(name)
        self.assertIsNotNone(reloaded)
        self.assertIsNotNone(reloaded.GetImageData())
        self.assertTrue(np.array_equal(slicer.util.arrayFromVolume(reloaded), expected))

    def test_15b_color_table_taken_over_names_no_local_path(self):
        """A colour table that 3D Slicer saved as .ctbl names the absolute path of its file on its first line: the project's
        copy (opened from outside, or moved from next to the scene file) names its own file only, and the privacy check
        reads colour tables.
        """
        checker = _privacyModule()
        service = self._service()
        _volume, _path = self._userVolume()
        colors = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", "My labels")
        colors.SetTypeToUser()
        colors.SetNumberOfColors(3)
        colors.SetColor(0, "Background", 0.0, 0.0, 0.0, 0.0)
        colors.SetColor(1, "Lesion", 1.0, 0.0, 0.0, 1.0)
        colors.SetColor(2, "Edema", 0.0, 1.0, 0.0, 1.0)
        outsidePath = os.path.join(self.userDir, "My labels.ctbl")
        self.assertTrue(slicer.util.saveNode(colors, outsidePath))
        slicer.mrmlScene.RemoveNode(colors)
        with open(outsidePath, "rb") as f:
            original = f.read()
        self.assertTrue(original.startswith(b"# Color table file "), original[:120])
        self.assertIn(os.path.basename(self.tempDir).encode(), original.split(b"\n", 1)[0], "3D Slicer writes the full path")
        colors = slicer.util.loadColorTable(outsidePath)
        project = self._projectFor(service)
        self._save(service)
        copied = colors.GetStorageNode().GetFileName()
        self.assertTrue(copied.startswith(project.path + os.sep), copied)
        self.assertTrue(copied.endswith(".ctbl"), copied)
        with open(copied, "rb") as f:
            data = f.read()
        self.assertEqual(data.split(b"\n", 1)[0], b"# Color table file " + os.path.basename(copied).encode())
        self.assertEqual(data.split(b"\n", 1)[1], original.split(b"\n", 1)[1], "the colours are copied unchanged")
        self.assertNotIn(os.path.basename(self.tempDir).encode(), data)
        with open(outsidePath, "rb") as f:
            self.assertEqual(f.read(), original, "the outside file is not changed")
        self.assertFalse(colors.GetModifiedSinceRead())
        # 3D Slicer's Save dialog put a colour table next to the scene file: moved into its kind folder, without the path
        second = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLColorTableNode", "More labels")
        second.SetHideFromEditors(False)  # a colour table the user loaded (new colour nodes are hidden)
        second.SetTypeToUser()
        second.SetNumberOfColors(2)
        second.SetColor(1, "Region", 0.0, 0.0, 1.0, 1.0)
        self.assertTrue(slicer.util.saveNode(second, os.path.join(project.path, "More labels.ctbl")))
        self.assertFalse(second.GetModifiedSinceRead())
        self.assertTrue(service.saveNow("test", progress=False))
        self.io.backgroundWriter().drain(10.0)
        moved = second.GetStorageNode().GetFileName()
        self.assertNotEqual(os.path.dirname(moved), project.path, moved)
        with open(moved, "rb") as f:
            self.assertEqual(f.read().split(b"\n", 1)[0], b"# Color table file More labels.ctbl")
        # Every file of the project names no absolute local path, and both tables open again
        for path in self._projectFiles(project):
            with open(os.path.join(project.path, path), "rb") as f:
                self.assertNotIn(os.path.basename(self.tempDir).encode(), f.read(), path)
        reloaded = slicer.util.loadColorTable(copied)
        self.assertEqual(reloaded.GetColorName(1), "Lesion")
        self.assertEqual(slicer.util.loadColorTable(moved).GetColorName(1), "Region")
        if checker is None:
            return
        self.assertEqual(checker.scan([project.path], out=lambda _line: None), 0)
        # The privacy check reads colour tables (.ctbl, and a .txt colour table) that name a local path
        for name in ("Leaky.ctbl", "Leaky.txt"):
            folder = os.path.join(self.tempDir, "privacy-" + name)
            os.makedirs(folder)
            with open(os.path.join(folder, name), "wb") as f:
                f.write(b"# Color table file /home/alice/patients/Leaky.ctbl\n# 2 values\n1 Lesion 255 0 0 255\n")
            lines = []
            self.assertEqual(checker.scan([folder], out=lines.append), 1, f"{name} is flagged: {lines}")

    def test_16_project_loaded_in_classic_continues_in_place(self):
        """A project of the folder loaded in Classic (official Load Scene) continues in place when Studio is shown: no copy."""
        projects = self.projects
        service = self._service()
        self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        projectPath, scenePath = project.path, project.scenePath
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        classic = self._service(studio=False)
        service.dispose()
        self.services.remove(service)
        slicer.util.loadScene(scenePath)
        classic.poll()
        self.assertIsNone(classic.project, "Classic loads a project like any scene")
        classic.studioActiveOverride = True
        classic._lastProjectCheck = None
        classic.poll()
        self.assertIsNotNone(classic.project)
        self.assertEqual(os.path.normpath(classic.project.path), os.path.normpath(projectPath))
        self.assertEqual(sorted(os.listdir(projects.projectsFolder())), [os.path.basename(projectPath)], "no duplicate project")
        # vtkMRMLScene::Clear keeps the scene URL: new data after it starts a new project, never goes into the old one
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(classic.project)
        self._userVolume("Other scan")
        classic._lastProjectCheck = None
        classic.poll()
        self.assertIsNotNone(classic.project)
        self.assertEqual(classic.project.name, "Other scan")

    def test_17_library_order_after_official_save(self):
        """A project that official 3D Slicer saved last (project.json not updated) is listed first."""
        projects = self.projects
        older = projects.createProject("Older")
        newer = projects.createProject("Newer")
        now = time.time()
        for project, modified in ((older, now - 60), (newer, now - 30)):
            with open(project.scenePath, "w", encoding="utf-8") as f:
                f.write("<MRML></MRML>\n")
            os.utime(project.scenePath, (modified, modified))
            self.io.atomicWriteJson(projects.projectFile(project.path), {"name": project.name, "modified": projects._isoTime(modified)})
        self.assertEqual([info.name for info in projects.listProjects()], ["Newer", "Older"])
        # Official 3D Slicer saves the older project now: it comes first
        os.utime(older.scenePath, (now, now))
        self.assertEqual([info.name for info in projects.listProjects()], ["Older", "Newer"])

    def test_18_ampersand_in_file_names(self):
        """A scan named "Smith & Jones T1" gives a well-formed project scene that opens again (MRML escapes "&")."""
        import xml.etree.ElementTree as ET

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume("Smith & Jones T1", shape=(12, 14, 16))
        self._lesion(volume, name="Lesions (Smith & Jones T1)")
        project = self._projectFor(service)
        self._save(service)
        scenePath = projects.sceneFile(project.path)
        with open(scenePath, encoding="utf-8") as f:
            text = f.read()
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            self.fail(f"the project scene is not well-formed XML: {error}")
        fileNames = [self.io.urlDecode(element.get("fileName")) for element in root.iter() if element.get("fileName")]
        self.assertTrue(any("&" in name and name.startswith("Images/") for name in fileNames), fileNames)
        for name in fileNames:
            self.assertTrue(os.path.isfile(os.path.join(project.path, name)), name)
        # The project opens again with its scan and lesion
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(projectPath))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Smith & Jones T1"))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesions (Smith & Jones T1)"))
        storage = slicer.mrmlScene.GetFirstNodeByName("Smith & Jones T1").GetStorageNode()
        self.assertTrue(os.path.isfile(storage.GetFileName()), storage.GetFileName())
        # A project scene written with a bare "&" (older builds, official 3D Slicer) is repaired when it is opened
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        with open(scenePath, "w", encoding="utf-8") as f:
            f.write(text.replace("&amp;", "&"))
        self.assertTrue(service.openProject(projectPath))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Smith & Jones T1"))
        with open(scenePath, encoding="utf-8") as f:
            ET.fromstring(f.read())
        self.assertFalse(self.io.repairBareAmpersands(scenePath), "a well-formed scene is not rewritten")
        # Renaming the folder of hidden nodes follows escaped names
        privateScene = os.path.join(self.tempDir, "private.mrml")
        with open(privateScene, "w", encoding="utf-8") as f:
            f.write('<MRML><Volume id="v" fileName="R&amp;D%20T1_Private/a.nrrd"/></MRML>')
        projects._rewritePrivatePaths(privateScene, "R&D T1_Private", "R&D T2_Private")
        with open(privateScene, encoding="utf-8") as f:
            self.assertEqual(ET.fromstring(f.read()).find("Volume").get("fileName"), "R&D%20T2_Private/a.nrrd")

    def test_19_open_another_project_while_one_is_open(self):
        """Opening project A while project B is open saves and closes B, then loads A (B is not read again)."""
        projects = self.projects
        service = self._service()
        self._userVolume("First scan", shape=(12, 14, 16))
        first = self._projectFor(service)
        self._save(service)
        firstPath = first.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        self._userVolume("Second scan", shape=(12, 14, 16))
        second = self._projectFor(service)
        self.assertNotEqual(second.path, firstPath)
        self._save(service)
        secondPath = second.path
        for openedPath, closedPath, present, absent in (
            (firstPath, secondPath, "First scan", "Second scan"),
            (secondPath, firstPath, "Second scan", "First scan"),
        ):
            urls = []
            tag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.StartImportEvent, lambda caller, event, urls=urls: urls.append(slicer.mrmlScene.GetURL()))
            try:
                self.assertTrue(service.openProject(openedPath))
            finally:
                slicer.mrmlScene.RemoveObserver(tag)
            self.io.backgroundWriter().drain(10.0)
            self.assertEqual(urls, [projects.sceneFile(openedPath)], "the scene of the opened project is read")
            self.assertIsNotNone(service.project)
            self.assertEqual(service.project.path, openedPath)
            self.assertEqual(os.path.normpath(slicer.mrmlScene.GetURL()), os.path.normpath(projects.sceneFile(openedPath)))
            self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName(present))
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName(absent))
            self.assertEqual(projects.readJson(projects.projectFile(closedPath))["state"], "closed")

    def test_20_save_with_less_than_1gb_free(self):
        """A project saves (continuously and with Ctrl+S) when its data fits on the disk, also below 1 GB free (D-A16).

        The 1 GB floor belongs to the private recovery autosave (A-01) only; a project save needs room for its data.
        """
        import shutil as shutilModule
        from unittest import mock

        service = self._service()
        self._userVolume("Small disk", shape=(20, 24, 28))
        project = self._projectFor(service)
        realDiskUsage = shutilModule.disk_usage
        freeBytes = [12 << 20]  # 12 MB free: far below 1 GB, far above what this project needs

        def diskUsage(path):
            return realDiskUsage(path)._replace(free=freeBytes[0])

        with mock.patch.object(shutilModule, "disk_usage", side_effect=diskUsage):
            self.assertTrue(service.saveNow("test", progress=False), service._projectPausedReason)
            self.io.backgroundWriter().drain(30.0)
            self.assertIn("Images/Small disk.nii.gz", self._projectFiles(project))
            self.assertIn("Small disk.mrml", self._projectFiles(project))
            # A lesion added later is saved too
            volume = slicer.util.getNode("Small disk")
            segmentation, segmentId, array = self._lesion(volume, name="Lesion")
            self.assertTrue(service.saveNow("test", progress=False), service._projectPausedReason)
            self.io.backgroundWriter().drain(30.0)
            self.assertIn("Segmentations/Lesion.seg.nrrd", self._projectFiles(project))
            self.assertEqual(service.unsavedSummary(), "")
            # A save that does not fit is still refused, with the reason
            freeBytes[0] = 1024
            array[12:14, 12:14, 12:14] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            self.assertFalse(service.saveNow("test", progress=False))
            self.assertIn("free on the disk", service._projectPausedReason)
            self.assertNotIn("1.0 GB", service._projectPausedReason)
            # The private recovery autosave keeps its 1 GB floor
            freeBytes[0] = 12 << 20
            self.assertFalse(service._enoughSpace(0, self.tempDir))
            self.assertTrue(service._enoughSpace(0, self.tempDir, project=True))

    def test_21_failed_root_move_keeps_open_project(self):
        """A move of the Slicer Studio folder that fails (the new disk is full) leaves the open project where it was.

        No partial copy stays in the new folder, the setting is unchanged, and the open project keeps saving into the
        old folder, where Library lists it.
        """
        import errno
        import shutil as shutilModule
        from unittest import mock

        projects = self.projects
        service = self._service()
        self._userVolume("Big scan", shape=(20, 24, 28))
        self._projectFor(service)
        self._save(service)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        volume, _path = self._userVolume("Open scan", shape=(12, 14, 16))
        project = self._projectFor(service)
        self._save(service)
        openPath = project.path
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        realCopyfile = shutilModule.copyfile

        def fullDisk(source, destination, *args, **kwargs):
            if os.sep + "Big scan" + os.sep in os.fspath(source):
                with open(destination, "wb") as f:
                    f.write(b"\0" * 16)  # what fitted on the disk
                raise OSError(errno.ENOSPC, "No space left on device", destination)
            return realCopyfile(source, destination, *args, **kwargs)

        try:
            newRoot = os.path.join(self.tempDir, "full disk", "Slicer Studio")
            with mock.patch.object(projects, "_sameVolume", return_value=False), mock.patch.object(shutilModule, "copyfile", side_effect=fullDisk):
                with self.assertRaises(OSError) as context:
                    service.moveRoot(newRoot, moveContent=True)
            self.assertEqual(projects.root(), os.path.normpath(self.root), "the setting is unchanged")
            # The open project stays in the old folder, with its scene and its files
            self.assertIs(service.project, project)
            self.assertEqual(project.path, openPath)
            self.assertEqual(os.path.normpath(slicer.mrmlScene.GetURL()), os.path.normpath(projects.sceneFile(openPath)))
            self.assertTrue(volume.GetStorageNode().GetFileName().startswith(openPath + os.sep))
            self.assertTrue(os.path.isfile(os.path.join(openPath, ".studio", "lock")), "the project is locked again")
            self.assertIn(openPath, [info.path for info in projects.listProjects()])
            self.assertEqual(sorted(info.name for info in projects.listProjects()), ["Big scan", "Open scan"])
            # No partial copy stays in the new folder, and the message names the first reason only
            self.assertFalse(os.path.exists(os.path.join(newRoot, "Projects")), "no partial copy stays in the new folder")
            self.assertEqual(context.exception.errno, errno.ENOSPC)
            self.assertIn("No space left on device", str(context.exception))
            self.assertNotIn(self.root, str(context.exception))
            # A later edit is saved into the old folder
            array = slicer.util.arrayFromVolume(volume)
            array[0, 0, 0] = 4242
            slicer.util.arrayFromVolumeModified(volume)
            self._save(service)
            saved = slicer.util.loadVolume(volume.GetStorageNode().GetFileName(), {"show": False})
            self.assertEqual(int(slicer.util.arrayFromVolume(saved)[0, 0, 0]), 4242)
        finally:
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_22_scene_file_changed_by_another_program(self):
        """The project's .mrml that another program (official 3D Slicer) changed is kept: Studio writes its own scene as
        "<project> (Slicer Studio).mrml" next to it, says so once, and keeps saving there. A scene file that this Slicer
        wrote itself (the official Save dialog) is not another program's change.
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16))
        project = self._projectFor(service)
        self._save(service)
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))

        def edit(value):
            array = slicer.util.arrayFromVolume(volume)
            array[0, 0, 0] = value
            slicer.util.arrayFromVolumeModified(volume)
            self._save(service)

        # This Slicer saves the scene file itself (the official Save dialog): Studio goes on writing it
        self.assertTrue(slicer.util.saveScene(project.scenePath))
        edit(11)
        self.assertEqual([event for event, _info in announced], [])
        self.assertEqual([name for name in os.listdir(project.path) if name.endswith(".mrml")], ["Scan.mrml"])
        # Another program adds a node to the scene file
        time.sleep(0.02)
        with open(project.scenePath, encoding="utf-8") as f:
            text = f.read()
        added = '<MarkupsFiducial id="vtkMRMLMarkupsFiducialNodeOther" name="Added by official Slicer" ></MarkupsFiducial>\n'
        with open(project.scenePath, "w", encoding="utf-8") as f:
            f.write(text.replace("</MRML>", added + "</MRML>"))
        theirs = _md5File(project.scenePath)
        edit(22)
        self.assertEqual(_md5File(project.scenePath), theirs, "the other program's scene file is never overwritten")
        ours = os.path.join(project.path, "Scan (Slicer Studio).mrml")
        self.assertTrue(os.path.isfile(ours))
        self.assertIn(("conflict", {"conflicts": [("Scan.mrml", "Scan (Slicer Studio).mrml")]}), announced)
        self.assertFalse(slicer.mrmlScene.GetModifiedSinceRead())
        self.assertEqual(slicer.mrmlScene.GetURL(), project.scenePath)
        # The next saves go on in Studio's scene file, without another notice or another copy
        del announced[:]
        edit(33)
        self.assertEqual(_md5File(project.scenePath), theirs)
        self.assertEqual(announced, [])
        self.assertEqual(sorted(name for name in os.listdir(project.path) if name.endswith(".mrml")), ["Scan (Slicer Studio).mrml", "Scan.mrml"])
        self.assertFalse([path for path in _relativeFiles(project.path) if path.startswith(".studio/removed/") and path.endswith(".mrml")], "no scene file is moved away")
        # Studio's scene file is a standard scene with relative paths: it loads with the last edit
        with open(ours, encoding="utf-8") as f:
            self.assertNotIn(self.tempDir, f.read())
        imageFile = volume.GetStorageNode().GetFileName()
        service.dispose()
        self.services.remove(service)
        slicer.mrmlScene.Clear(0)
        slicer.util.loadScene(ours)
        loaded = slicer.util.getNode("Scan")
        self.assertEqual(os.path.normpath(loaded.GetStorageNode().GetFileName()), os.path.normpath(imageFile))
        self.assertEqual(int(slicer.util.arrayFromVolume(loaded)[0, 0, 0]), 33)

    def test_23_scene_opened_while_a_project_is_open(self):
        """Open… (or drop, or Open folder…) of a scene while a project is open opens the scene as its own project (A-09):
        the open project is saved and closed and keeps its data; a scene from outside the folder opens as a new project that
        starts as a copy; a project of the folder continues in place; the open project's own scene is not added again.
        """
        from StudioLib import importer, shell

        projects = self.projects
        # A scene bundle saved by 3D Slicer somewhere else
        volume, _path = self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume, "Outside lesion")
        bundlePath = os.path.join(self.tempDir, "Outside bundle.mrb")
        self.assertTrue(slicer.util.saveScene(bundlePath))
        bundleMd5 = _md5File(bundlePath)
        slicer.mrmlScene.Clear(0)
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, project.name if project else None)))
        self._userVolume("First scan", shape=(12, 14, 16))
        first = self._projectFor(service)
        self._save(service)
        firstPath = first.path
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        studioShell = shell.Shell(force=True)
        try:
            studioShell.apply()
            slicer.app.processEvents()
            # A scene from outside the folder: a new project that starts as a copy; the open project is not changed
            del announced[:]
            result = importer.loadFiles([bundlePath], studioShell)
            self.io.backgroundWriter().drain(30.0)
            self.assertEqual(result.scenes, [bundlePath])
            self.assertIsNotNone(service.project)
            self.assertNotEqual(service.project.path, firstPath, "the scene is not added to the open project")
            self.assertEqual(service.project.name, "Outside bundle")
            self.assertIn(("copy", "Outside bundle"), announced)
            self.assertEqual(projects.readJson(projects.projectFile(firstPath))["state"], "closed")
            for name in ("MR Head", "Outside lesion"):
                self.assertEqual(len(slicer.util.getNodes(name, useLists=True).get(name, [])), 1, name)
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("First scan"))
            self.assertEqual([path for path in self._projectFiles(first) if "MR Head" in path or "Outside" in path], [])
            self.assertEqual(_md5File(bundlePath), bundleMd5, "the original is not changed")
            # Open folder… on a project of the folder: it continues in place (the open project is closed first)
            secondPath = service.project.path
            self.assertEqual(importer.loadFolder(firstPath, studioShell), "scene")
            self.io.backgroundWriter().drain(30.0)
            self.assertEqual(service.project.path, firstPath)
            self.assertEqual(projects.readJson(projects.projectFile(secondPath))["state"], "closed")
            self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("First scan"))
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("MR Head"))
            # The open project's own scene file: nothing is added again
            importer.loadFiles([projects.sceneFile(firstPath)], studioShell)
            self.assertEqual(service.project.path, firstPath)
            self.assertEqual(len(slicer.util.getNodes("First scan", useLists=True).get("First scan", [])), 1)
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()

    def test_24_reopen_outside_file_after_save(self):
        """A file from outside the folder that the project took over (copied into Images/, Lesions/...) is "already open" when
        it is opened again (Open…, drop, Recent, a folder card's Load all), also after a restart: no second node, no second
        file in the project. The outside path is kept in .studio only (the scene file holds relative paths only).
        """
        from StudioLib import importer, shell

        volume, scanPath = self._userVolume("patient_T1", shape=(12, 14, 16))
        slicer.mrmlScene.RemoveNode(volume)
        labels = np.zeros((16, 14, 12), np.uint8)
        labels[4:8, 5:9, 3:6] = 1
        lesionNode = slicer.util.addVolumeFromArray(labels, name="patient_T1_lesion", nodeClassName="vtkMRMLLabelMapVolumeNode")
        lesionPath = os.path.join(self.userDir, "patient_T1_lesion.nii.gz")
        self.assertTrue(slicer.util.saveNode(lesionNode, lesionPath))
        slicer.mrmlScene.RemoveNode(lesionNode)
        service = self._service()
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        studioShell = shell.Shell(force=True)

        def count(name, className):
            return len([node for node in slicer.util.getNodesByClass(className) if node.GetName() == name])

        try:
            studioShell.apply()
            slicer.app.processEvents()
            result = importer.loadFiles([scanPath, lesionPath], studioShell)
            self.assertEqual(len(result.volumes), 1)
            self.assertEqual(len(result.segmentations), 1)
            scan, lesion = result.volumes[0], result.segmentations[0]
            project = self._projectFor(service)
            self._save(service)
            projectPath = project.path
            for node in (scan, lesion):
                self.assertTrue(self.projects.isInside(node.GetStorageNode().GetFileName(), projectPath),
                                f"{node.GetName()} is saved in the project")
            filesBefore = self._projectFiles(project)
            # Opened again (Open…, drop, Load all): the nodes become current again, nothing is loaded twice
            again = importer.loadFiles([scanPath, lesionPath], studioShell)
            self.assertEqual((again.volumes, again.segmentations), ([], []))
            self.assertEqual({node.GetID() for node in again.alreadyOpen}, {scan.GetID(), lesion.GetID()})
            self.assertEqual(importer.openNodeForFile(scanPath).GetID(), scan.GetID())
            # Recent
            recent = importer.loadRecentFile({"fileName": scanPath, "fileType": "VolumeFile"}, shell=studioShell)
            self.assertEqual([node.GetID() for node in recent.alreadyOpen], [scan.GetID()])
            self.assertEqual(count(scan.GetName(), "vtkMRMLScalarVolumeNode"), 1)
            self.assertEqual(count(lesion.GetName(), "vtkMRMLSegmentationNode"), 1)
            self._save(service)
            self.assertEqual(self._projectFiles(project), filesBefore, "no second copy in the project")
            # The outside paths stay out of the scene file
            with open(project.scenePath, encoding="utf-8") as f:
                sceneText = f.read()
            self.assertNotIn(os.path.basename(self.userDir), sceneText)
            # The project folder is shared whole: .studio/sources.json holds identifiers of the outside files, no path
            sourcesPath = os.path.join(projectPath, ".studio", "sources.json")
            with open(sourcesPath, encoding="utf-8") as f:
                sourcesText = f.read()
            self.assertNotIn(os.path.basename(self.userDir), sourcesText)
            self.assertNotIn(self.tempDir, sourcesText)
            sources = json.loads(sourcesText)["sources"]
            self.assertEqual(len(sources), 2)
            self.assertTrue(all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in sources.values()), sources)
            # After a restart (the project is closed and opened again): still the same nodes
            scanName, lesionName = scan.GetName(), lesion.GetName()
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)
            self.assertIsNone(service.project)
            self.assertTrue(service.openProject(projectPath))
            scan = slicer.mrmlScene.GetFirstNodeByName(scanName)
            self.assertIsNotNone(scan)
            reopened = importer.loadFiles([scanPath, lesionPath], studioShell)
            self.assertEqual((reopened.volumes, reopened.segmentations), ([], []))
            self.assertEqual(len(reopened.alreadyOpen), 2)
            self.assertEqual(count(scanName, "vtkMRMLScalarVolumeNode"), 1)
            self.assertEqual(count(lesionName, "vtkMRMLSegmentationNode"), 1)
            # A project of an earlier version (sources.json holds the paths): still "already open", and the next save
            # writes identifiers instead
            legacy = {key: (scanPath if "Images" in key else lesionPath) for key in sources}
            with open(sourcesPath, "w", encoding="utf-8") as f:
                json.dump({"format": self.io.SOURCES_FORMAT, "version": 1, "sources": legacy}, f)
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)
            self.assertTrue(service.openProject(projectPath))
            scan = slicer.mrmlScene.GetFirstNodeByName(scanName)
            migrated = importer.loadFiles([scanPath, lesionPath], studioShell)
            self.assertEqual((migrated.volumes, migrated.segmentations), ([], []))
            self.assertEqual(len(migrated.alreadyOpen), 2)
            scan.SetName(scanName + " renamed")  # a change, so that the project is saved
            scan.SetName(scanName)
            self._save(service)
            with open(sourcesPath, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["sources"], sources)
            # Closed and opened again from outside: loaded anew (nothing is matched to a node that left the scene)
            slicer.mrmlScene.RemoveNode(scan)
            fresh = importer.loadFiles([scanPath], studioShell)
            self.assertEqual(len(fresh.volumes), 1)
            self.assertEqual(fresh.alreadyOpen, [])
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()

    def test_25_studio_folders_never_become_studies(self):
        """New study…, Create study and the folder card refuse Studio's own folders: Studies/ (where the New study dialog
        starts), Projects/, a project, the Slicer Studio folder, LNM/, a folder that holds the Slicer Studio folder and a
        folder inside a study; no study.json is written there. A folder of the user's that holds another study keeps that
        study's files out of the new one; a new folder inside Studies/ is a study as before.
        """
        from StudioLib import library, study_ui

        if not study_ui.isAvailable():
            self.skipTest("studies are not part of this build")
        projects = self.projects
        projects.ensureRoot()
        _volume, scanPath = self._userVolume("patient_T1", shape=(12, 14, 16))
        slicer.mrmlScene.Clear(0)
        scanFolder = library._importer().scanFolder
        # A project (Images/ and .studio/) and a study made the intended way: Studies/Stroke
        project = os.path.join(projects.projectsFolder(), "alpha T1")
        os.makedirs(os.path.join(project, "Images"))
        os.makedirs(os.path.join(project, ".studio"))
        shutil.copy(scanPath, os.path.join(project, "Images", "alpha_T1.nii.gz"))
        stroke = os.path.join(projects.studiesFolder(), "Stroke")
        os.makedirs(stroke)
        shutil.copy(scanPath, os.path.join(stroke, "sub-01_T1w.nii.gz"))
        study_ui.createStudy(scanFolder(stroke)[0], folder=stroke, openIt=False)
        self.assertTrue(os.path.isfile(os.path.join(stroke, "study.json")))
        # New study…, accepting the folder the dialog opens on (Studies/), then choosing a project
        answers, starts = [], []

        class FileDialog:
            @staticmethod
            def getExistingDirectory(parent, title, start, *args):
                starts.append(start)
                return answers.pop(0) if answers else start

        class FakeQt:
            QFileDialog = FileDialog

            def __getattr__(self, name):
                return getattr(qt, name)

        study_ui.qt = FakeQt()
        try:
            self.assertIsNone(study_ui.newStudyDialog())
            answers.append(project)
            self.assertIsNone(study_ui.newStudyDialog())
        finally:
            study_ui.qt = qt
        self.assertEqual(os.path.normpath(starts[0]), os.path.normpath(projects.studiesFolder()))
        self.assertFalse(os.path.exists(os.path.join(projects.studiesFolder(), "study.json")))
        self.assertFalse(os.path.exists(os.path.join(project, "study.json")))
        self.assertEqual(projects.listStudies(), [stroke])
        self.assertIsNone(study_ui.studyFolderProblem(stroke))
        os.makedirs(projects.lnmFolder())
        refused = [projects.studiesFolder(), projects.projectsFolder(), project, os.path.join(project, "Images"), self.root,
                   self.tempDir, projects.lnmFolder(), os.path.join(stroke, "lesions")]
        for folder in refused:
            self.assertTrue(study_ui.studyFolderProblem(folder), folder)
            with self.assertRaises(ValueError, msg=folder):
                study_ui.createStudy(scanFolder(folder)[0], folder=folder, openIt=False)
            self.assertFalse(os.path.exists(os.path.join(folder, "study.json")), folder)
        # The folder card of Projects/ says why instead of offering Create study
        card = library.FolderCard(projects.projectsFolder(), scanFolder(projects.projectsFolder())[0])
        try:
            self.assertIsNone(card.createStudyButton)
            self.assertEqual(card.planLabel.text, study_ui.studyFolderProblem(projects.projectsFolder()))
            self.assertEqual(card.loadAllButton.property("slicerRole"), "primary")
        finally:
            card.close()
        self.assertEqual([info.name for info in projects.listProjects()], ["alpha T1"])
        # A folder of the user's that holds a study: that study's files are not subjects of the new one
        shutil.copy(scanPath, os.path.join(self.userDir, "sub-02_T1w.nii.gz"))
        inner = os.path.join(self.userDir, "older study")
        os.makedirs(inner)
        shutil.copy(scanPath, os.path.join(inner, "sub-09_T1w.nii.gz"))
        study_ui.createStudy(scanFolder(inner)[0], folder=inner, openIt=False)
        files = scanFolder(self.userDir)[0]
        self.assertIn(os.path.join(inner, "sub-09_T1w.nii.gz"), files)
        outer = study_ui.createStudy(files, folder=self.userDir, openIt=False)
        self.assertIn("sub-02", outer.subject_ids)
        self.assertNotIn("sub-09", outer.subject_ids)
        # A new folder inside Studies/ is a study as before
        aphasia = os.path.join(projects.studiesFolder(), "Aphasia")
        os.makedirs(aphasia)
        study_ui.createStudy([], folder=aphasia, openIt=False)
        self.assertEqual(sorted(os.path.basename(path) for path in projects.listStudies()), ["Aphasia", "Stroke"])

    def test_26_failed_scene_load_leaves_no_broken_project(self):
        """A scene from elsewhere that does not load (truncated .mrml) leaves no project and no "Opened a copy" message; one
        whose files are partly missing opens as a copy of what loaded, says what is missing, and leaves nothing unsaved
        (New project then closes the scene without 3D Slicer's "Save before closing scene?").
        """
        projects = self.projects
        volume, volumePath = self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        segmentation, _segmentId, _array = self._lesion(volume, "Tumor")
        self.assertTrue(slicer.util.saveNode(segmentation, os.path.join(self.userDir, "Tumor.seg.nrrd")))
        outside = os.path.join(self.tempDir, "outside scene")
        os.makedirs(outside)
        scenePath = os.path.join(outside, "Head study.mrml")
        self.assertTrue(slicer.util.saveScene(scenePath))
        with open(scenePath, encoding="utf-8") as file:
            text = file.read()
        brokenPath = os.path.join(outside, "Broken study.mrml")
        with open(brokenPath, "w", encoding="utf-8") as file:
            file.write(text[: len(text) // 2])
        slicer.mrmlScene.Clear(0)
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))

        # Truncated scene file: nothing is loaded, so no project is created
        service.setPendingSceneSource(brokenPath)
        with contextlib.suppress(Exception):
            slicer.util.loadScene(brokenPath)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project, "a scene that does not load is not copied into a project")
        self.assertEqual([info.name for info in projects.listProjects()], [])
        # Not a copy message: the user is told that the file is damaged (BB-1), and it is left as it is
        self.assertEqual([(event, info.get("damaged")) for event, info in announced], [("not-opened", True)])
        self.assertEqual(announced[0][1]["reason"], "Broken study is damaged and could not be read: nothing was opened, and the file is left as it is.")
        self.assertEqual(slicer.mrmlScene.GetURL(), "")
        with open(brokenPath, encoding="utf-8") as file:
            self.assertEqual(file.read(), text[: len(text) // 2])
        del announced[:]

        # A scan of the scene is missing: the copy holds the rest and says what is missing
        slicer.mrmlScene.Clear(0)
        os.remove(volumePath)
        service.setPendingSceneSource(scenePath)
        with contextlib.suppress(Exception):
            slicer.util.loadScene(scenePath)
        project = service.project
        self.assertIsNotNone(project, "the data that loaded opens as a copy")
        self.assertEqual([event for event, _info in announced], ["copy-incomplete"], "no success message")
        self.assertEqual(announced[0][1].get("unread"), ["MR Head"])
        self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("MR Head"), "a node without data is not kept in the copy")
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Tumor"))
        self._save(service)
        modified = vtk.vtkCollection()
        slicer.mrmlScene.GetStorableNodesModifiedSinceRead(modified)
        names = [modified.GetItemAsObject(index).GetName() for index in range(modified.GetNumberOfItems())]
        self.assertEqual(names, [], "no data file is left unsaved (closing the scene does not ask about it)")
        files = self._projectFiles(project)
        self.assertTrue(any(path.startswith("Segmentations/") for path in files), files)
        self.assertFalse(any("MR Head" in path for path in files), files)
        self.assertEqual([info.name for info in projects.listProjects()], ["Head study"])

    def test_27_show_in_files_and_link_outs_without_a_desktop(self):
        """Show in Files and link-outs do something visible where the desktop cannot open them: the web build (no file manager or
        browser in the container) and a desktop without a launcher show the location (the --data folder in the web build)
        with [Copy path] / [Copy link]; where the desktop opens it, nothing extra shows.
        """
        projects = self.projects
        folder = os.path.join(self.tempDir, "A folder")
        os.makedirs(folder)
        opened = []
        launcher = {"works": True}
        realOpenUrl = qt.QDesktopServices.openUrl

        def fakeOpenUrl(url):
            opened.append(url.toString())
            return launcher["works"]

        def dialog():
            for found in slicer.util.mainWindow().findChildren("QDialog"):
                if found.objectName == "StudioLocationDialog" and found.isVisible():
                    return found
            return None

        def closeDialog():
            found = dialog()
            if found is not None:
                found.close()
            slicer.app.processEvents()

        def field():
            return dialog().findChild(qt.QLineEdit, "StudioLocationField").text

        webValue = os.environ.get("SLICER_STUDIO_WEB")
        qt.QDesktopServices.openUrl = fakeOpenUrl
        try:
            # A desktop that opens it: no message
            os.environ.pop("SLICER_STUDIO_WEB", None)
            if not os.path.exists("/.dockerenv"):
                self.assertTrue(projects.showInFileManager(folder))
                self.assertEqual(len(opened), 1)
                self.assertIsNone(dialog(), "nothing extra shows when the file manager opened the folder")
                self.assertTrue(projects.openLink("https://doi.org/10.1000/182"))
                self.assertEqual(len(opened), 2)
                self.assertIsNone(dialog())
                # A desktop without a launcher
                launcher["works"] = False
                self.assertTrue(projects.showInFileManager(folder))
                self.assertIsNotNone(dialog(), "the location shows when no file manager could be started")
                self.assertEqual(field(), folder)
                closeDialog()
                launcher["works"] = True

            # The web build: no attempt to start a launcher that is not there
            os.environ["SLICER_STUDIO_WEB"] = "1"
            del opened[:]
            self.assertFalse(projects.showInFileManager(os.path.join(folder, "missing")), "a missing folder is still reported")
            self.assertIsNone(dialog())
            self.assertTrue(projects.showInFileManager(folder))
            self.assertEqual(opened, [], "the web build does not call the desktop")
            self.assertIsNotNone(dialog(), "Show in Files shows the folder")
            self.assertEqual(field(), folder)
            self.assertNotIn("/data is the data folder", "".join(label.text for label in dialog().findChildren(qt.QLabel)))
            qt.QApplication.clipboard().setText("")
            dialog().findChild(qt.QPushButton, "StudioLocationCopyButton").click()
            self.assertEqual(qt.QApplication.clipboard().text(), folder)
            closeDialog()

            # The folder of the web build is the --data folder of the host
            projects._showLocationDialog("folder", "/data/Slicer Studio/Projects/Head")
            note = dialog().findChild(qt.QLabel, "StudioLocationHostNote")
            self.assertIsNotNone(note, "the host data folder is named")
            self.assertIn("--data", note.text)
            self.assertIn("Slicer Studio/Projects/Head", note.text)
            closeDialog()

            # A link to the data folder (the container's ~/Documents) is shown as /data/... with the same note
            fakeData = os.path.realpath(os.path.join(self.tempDir, "fakedata"))
            os.makedirs(os.path.join(fakeData, "Exports"))
            linkPath = os.path.join(self.tempDir, "Documents")
            os.symlink(fakeData, linkPath)
            realWebData = projects.WEB_DATA_FOLDER
            projects.WEB_DATA_FOLDER = fakeData
            try:
                projects.showInFileManager(os.path.join(linkPath, "Exports"))
                self.assertEqual(field(), fakeData + "/Exports", "the link is resolved to the data folder path")
                note = dialog().findChild(qt.QLabel, "StudioLocationHostNote")
                self.assertIsNotNone(note, "a folder reached through a link to the data folder gets the host note")
                self.assertIn("In that folder: Exports", note.text)
                closeDialog()
            finally:
                projects.WEB_DATA_FOLDER = realWebData

            # The buttons of the interface
            from StudioLib import projects_ui

            self.assertTrue(projects_ui.showRootFolder() is not None)
            self.assertIsNotNone(dialog(), "the project folder commands show the folder too")
            self.assertEqual(field(), projects.root())
            closeDialog()

            # Link-outs show the link to copy
            self.assertTrue(projects.openLink("https://doi.org/10.1000/182"))
            self.assertEqual(opened, [])
            self.assertEqual(field(), "https://doi.org/10.1000/182")
            dialog().findChild(qt.QPushButton, "StudioLocationCopyButton").click()
            self.assertEqual(qt.QApplication.clipboard().text(), "https://doi.org/10.1000/182")
            closeDialog()
            from StudioLib import libcard_columns

            libcard_columns.openExternalUrl("https://example.org/page")
            self.assertEqual(field(), "https://example.org/page")
            closeDialog()
            from LesionNetworkMappingUI import widgets

            self.assertTrue(widgets.showInFileManager(folder))
            self.assertEqual(field(), folder)
            closeDialog()
            widgets.openLink("https://example.org/lnm")
            self.assertEqual(field(), "https://example.org/lnm")
            closeDialog()
            self.assertEqual(opened, [])
        finally:
            qt.QDesktopServices.openUrl = realOpenUrl
            closeDialog()
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue

    def test_27b_web_file_urls_show_the_location(self):
        """A file:// address or a bare folder path (Report a Bug "Open log location" / "Open log in editor", Settings, Segmentations
        export, Sample Data, Screen Capture) shows the location window with [Copy path] in the web build, not nothing.
        """
        projects = self.projects
        folder = os.path.join(self.tempDir, "Official folder")
        os.makedirs(folder)
        logFile = os.path.join(folder, "Slicer.log")
        open(logFile, "w").close()
        webValue = os.environ.get("SLICER_STUDIO_WEB")
        os.environ["SLICER_STUDIO_WEB"] = "1"

        def dialog():
            for found in qt.QApplication.topLevelWidgets():
                if found.objectName == "StudioLocationDialog" and found.isVisible():
                    return found
            return None

        try:
            self.assertTrue(projects.installWebLinkHandler())
            for name, url, expected in (
                ("file url", qt.QUrl.fromLocalFile(logFile), logFile),
                ("bare path", qt.QUrl(folder, qt.QUrl.TolerantMode), folder),
            ):
                _REAL_OPEN_URL(url)
                slicer.app.processEvents()
                found = dialog()
                self.assertIsNotNone(found, "%s shows the location" % name)
                self.assertEqual(found.windowTitle, "Folder location")
                self.assertEqual(found.findChild(qt.QLineEdit, "StudioLocationField").text, expected)
                found.close()
                slicer.app.processEvents()
            _REAL_OPEN_URL(qt.QUrl("https://example.org/x"))
            slicer.app.processEvents()
            found = dialog()
            self.assertIsNotNone(found)
            self.assertEqual(found.windowTitle, "Link")
            found.close()
        finally:
            projects.uninstallWebLinkHandler()
            found = dialog()
            if found is not None:
                found.close()
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue

    def test_27a_location_window_is_usable_above_a_modal_dialog(self):
        """The location window opened from a modal dialog (Settings, Report a Bug) is a child of that dialog.

        Qt drops the clicks on a window that does not descend from the active modal dialog, and the window manager keeps the
        modal dialog on top, so a window that was a child of the main window showed behind the dialog and its [Copy] button
        did nothing.
        """
        projects = self.projects
        folder = os.path.join(self.tempDir, "Modal folder")
        os.makedirs(folder)
        webValue = os.environ.get("SLICER_STUDIO_WEB")
        os.environ["SLICER_STUDIO_WEB"] = "1"
        modal = qt.QDialog(slicer.util.mainWindow())
        modal.setObjectName("TestModalDialog")
        modal.setModal(True)
        modal.show()
        slicer.app.processEvents()
        shown = None
        try:
            self.assertEqual(qt.QApplication.activeModalWidget(), modal)
            self.assertTrue(projects.showInFileManager(folder))
            slicer.app.processEvents()
            shown = modal.findChild(qt.QDialog, "StudioLocationDialog")
            self.assertIsNotNone(shown, "the location window is a child of the modal dialog")
            self.assertTrue(shown.isVisible())
            self.assertEqual(shown.findChild(qt.QLineEdit, "StudioLocationField").text, folder)
            # A second window replaces the first one and is a child of the modal dialog too
            projects.openLink("https://example.org/modal")
            slicer.app.processEvents()
            links = [found for found in modal.findChildren("QDialog") if found.objectName == "StudioLocationDialog" and found.isVisible()]
            self.assertEqual(len(links), 1, "the first window was closed")
            self.assertEqual(links[0].findChild(qt.QLineEdit, "StudioLocationField").text, "https://example.org/modal")
            qt.QApplication.clipboard().setText("before")
            # Qt blocks the input of a window unless it descends from the active modal dialog
            ancestor = links[0].parent()
            while ancestor is not None and ancestor != modal:
                ancestor = ancestor.parent()
            self.assertEqual(ancestor, modal, "the location window descends from the modal dialog, so Qt does not block it")
            links[0].findChild(qt.QPushButton, "StudioLocationCopyButton").click()
            self.assertEqual(qt.QApplication.clipboard().text(), "https://example.org/modal")
        finally:
            modal.close()
            modal.deleteLater()
            slicer.app.processEvents()
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue
        # Without a modal dialog the window is a child of the main window again
        os.environ["SLICER_STUDIO_WEB"] = "1"
        try:
            self.assertIsNone(qt.QApplication.activeModalWidget())
            found = projects._showLocationDialog("folder", folder)
            self.assertEqual(found.parent(), slicer.util.mainWindow())
            found.close()
        finally:
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue

    def test_27b_clicked_links_in_rich_text_without_a_desktop(self):
        """Links that a user clicks in rich-text labels and text browsers show the link window in the web build.

        ``setOpenExternalLinks(True)`` made Qt call ``QDesktopServices::openUrl`` itself, which does nothing in the container,
        so a click on a DOI showed nothing. ``projects.routeLinks`` sends the click to ``projects.openLink`` instead.
        """
        from LesionNetworkMappingUI import card_recipe, widgets
        from StudioLib import updates

        url = "https://doi.org/10.1038/s41467-019-11353-z"
        opened = []
        realOpenUrl = qt.QDesktopServices.openUrl

        def fakeOpenUrl(qurl):
            opened.append(qurl.toString())
            return True

        def dialog():
            for found in slicer.util.mainWindow().findChildren("QDialog"):
                if found.objectName == "StudioLocationDialog" and found.isVisible():
                    return found
            return None

        def closeDialog():
            found = dialog()
            if found is not None:
                found.close()
            slicer.app.processEvents()

        def click(widget, pos):
            for kind in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease):
                event = qt.QMouseEvent(kind, qt.QPointF(pos), qt.QPointF(widget.mapToGlobal(pos)), qt.Qt.LeftButton, qt.Qt.LeftButton, qt.Qt.NoModifier)
                qt.QApplication.sendEvent(widget, event)
            slicer.app.processEvents()

        def shownLink():
            found = dialog()
            self.assertIsNotNone(found, "clicking a link shows the link window")
            return found.findChild(qt.QLineEdit, "StudioLocationField").text

        webValue = os.environ.get("SLICER_STUDIO_WEB")
        os.environ["SLICER_STUDIO_WEB"] = "1"
        qt.QDesktopServices.openUrl = fakeOpenUrl
        host = None
        try:
            # A label with one link (LNM help link) and the recipe popover label
            helpLink = widgets.helpLink("Open the DOI", url)
            popover = card_recipe.Popover(slicer.util.mainWindow(), "BBTestPopover")
            popover.label.setText(f'<a href="{url}">doi link</a>')
            for label in (helpLink, popover.label):
                self.assertFalse(label.openExternalLinks, "Qt must not open links itself")
                host = qt.QWidget()
                layout = qt.QVBoxLayout(host)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.addWidget(label)
                host.resize(300, 60)
                host.show()
                slicer.app.processEvents()
                click(label, qt.QPoint(20, 18) if label is popover.label else qt.QPoint(10, label.height // 2))
                self.assertEqual(shownLink(), url)
                closeDialog()
                label.setParent(None)
                host.deleteLater()
                host = None
            self.assertEqual(opened, [], "the web build does not call the desktop")

            # A text browser: the clicked anchor goes to the same window; a link inside the text opens nothing
            browser = updates._notesBrowser("Notes\n\n[Release notes](" + url + ")")
            self.assertFalse(browser.openLinks)
            self.assertFalse(browser.openExternalLinks)
            browser.anchorClicked(qt.QUrl(url))
            self.assertEqual(shownLink(), url)
            closeDialog()
            browser.anchorClicked(qt.QUrl("#top"))
            self.assertIsNone(dialog(), "a link inside the text opens no window")
            plain = widgets.routeLinks(qt.QTextBrowser())
            self.assertFalse(plain.openLinks)
            self.assertEqual(opened, [])

            # A desktop that opens links: the browser gets it, nothing extra shows
            os.environ.pop("SLICER_STUDIO_WEB", None)
            if not os.path.exists("/.dockerenv"):
                browser.anchorClicked(qt.QUrl(url))
                self.assertEqual(opened, [url])
                self.assertIsNone(dialog())
        finally:
            qt.QDesktopServices.openUrl = realOpenUrl
            closeDialog()
            if host is not None:
                host.deleteLater()
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue

    def test_27c_official_link_outs_show_the_link_in_the_web_build(self):
        """The official actions (the Help menu: Documentation, Quick Start, Get Help ...) call ``QDesktopServices::openUrl``
        themselves. In the web build there is no browser, so the installed handler shows the link with [Copy link] (before, only
        a log line said that Qt could not detect a web browser). Elsewhere no handler is installed.
        """
        projects = self.projects

        def dialog():
            for found in slicer.util.mainWindow().findChildren("QDialog"):
                if found.objectName == "StudioLocationDialog" and found.isVisible():
                    return found
            return None

        def closeDialog():
            found = dialog()
            if found is not None:
                found.close()
            slicer.app.processEvents()

        webValue = os.environ.get("SLICER_STUDIO_WEB")
        try:
            # Not the web build: nothing is installed
            os.environ.pop("SLICER_STUDIO_WEB", None)
            if not os.path.exists("/.dockerenv"):
                self.assertFalse(projects.installWebLinkHandler())

            os.environ["SLICER_STUDIO_WEB"] = "1"
            self.assertTrue(projects.installWebLinkHandler())
            self.assertTrue(projects.installWebLinkHandler(), "calling it again is safe")
            self.assertIsNone(dialog())

            # The official Help actions call QDesktopServices::openUrl in C++ (as the Help menu does). The Python name
            # qt.QDesktopServices.openUrl is not used: the tests above replace it, and putting the original back breaks it.
            helpMenu = slicer.util.mainWindow().findChild("QMenu", "HelpMenu")
            self.assertIsNotNone(helpMenu, "the main window has its Help menu")
            shown = []
            for action in helpMenu.actions():
                label = action.text.replace("&", "")
                if not any(label.startswith(name) for name in ("Documentation", "Quick Start", "Get Help", "Visit the Slicer Forum")):
                    continue
                action.trigger()
                slicer.app.processEvents()
                found = dialog()
                self.assertIsNotNone(found, "Help > %s shows the link" % label)
                link = found.findChild(qt.QLineEdit, "StudioLocationField").text
                self.assertTrue(link.startswith("http"), link)
                qt.QApplication.clipboard().setText("")
                found.findChild(qt.QPushButton, "StudioLocationCopyButton").click()
                self.assertEqual(qt.QApplication.clipboard().text(), link)
                shown.append(label)
                closeDialog()
            self.assertGreaterEqual(len(shown), 2, "the Help menu has link-out entries: %s" % shown)

            # After uninstall nothing is routed
            projects.uninstallWebLinkHandler()
            for action in helpMenu.actions():
                if action.text.replace("&", "").startswith("Documentation"):
                    action.trigger()
                    slicer.app.processEvents()
                    self.assertIsNone(dialog(), "no handler, no link window")
        finally:
            projects.uninstallWebLinkHandler()
            closeDialog()
            if webValue is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = webValue

    def test_28_save_copy_as_starts_in_an_existing_folder(self):
        """"Save a copy as…" opens in an existing folder, never the current folder that Qt uses for a missing one (HOME in
        the web container, "/" for a Finder-started app): Exports/ of a new project (created), else Studio/LastSaveFolder
        (/data in the web build), else the home folder.
        """
        from StudioLib import projects_ui

        settings = qt.QSettings()
        savedFolder = settings.value("Studio/LastSaveFolder")
        lastFolder = os.path.join(self.tempDir, "data")
        os.makedirs(lastFolder)
        elsewhere = os.path.join(self.tempDir, "working directory")
        os.makedirs(elsewhere)
        starts = []
        startExisted = []
        realGetSaveFileName = qt.QFileDialog.getSaveFileName

        def fakeGetSaveFileName(parent, caption, start, *args):
            starts.append(start)
            startExisted.append(os.path.isdir(os.path.dirname(start)))  # what Qt sees when the dialog opens
            return ""  # Cancel

        workingDirectory = os.getcwd()
        running = self.autosave._service
        qt.QFileDialog.getSaveFileName = fakeGetSaveFileName
        try:
            os.chdir(elsewhere)
            # No project: the last save folder, else the home folder
            self.assertIsNone(self.projects.currentProject())
            settings.setValue("Studio/LastSaveFolder", lastFolder)
            self.assertIsNone(projects_ui.saveCopyAs())
            self.assertEqual(os.path.realpath(os.path.dirname(starts[-1])), os.path.realpath(lastFolder))
            settings.setValue("Studio/LastSaveFolder", os.path.join(lastFolder, "missing"))
            self.assertIsNone(projects_ui.saveCopyAs())
            self.assertEqual(os.path.realpath(os.path.dirname(starts[-1])), os.path.realpath(qt.QDir.homePath()))
            # A new project: its Exports/ folder, created for the dialog (it did not exist yet)
            settings.setValue("Studio/LastSaveFolder", lastFolder)
            service = self._service()
            self.autosave._service = service  # as the running service of the application
            self._userVolume()
            project = self._projectFor(service)
            self._save(service)
            exports = project.folder(self.projects.EXPORTS)
            self.assertFalse(os.path.isdir(exports), "a new project has no Exports/ folder yet")
            self.assertIsNone(projects_ui.saveCopyAs())
            start = starts[-1]
            self.assertTrue(startExisted[-1], f"the dialog starts in an existing folder: {start}")
            self.assertEqual(os.path.realpath(os.path.dirname(start)), os.path.realpath(exports))
            self.assertEqual(os.path.basename(start), project.name + ".mrb")
            # Cancelled: the folder made for the dialog is removed again (A-09: a subfolder only when something is written)
            self.assertFalse(os.path.isdir(exports), "a cancelled 'Save a copy as' leaves no empty Exports/ folder")
            # A folder that existed (with content) is never removed
            os.makedirs(exports)
            open(os.path.join(exports, "keep.txt"), "w").close()
            self.assertIsNone(projects_ui.saveCopyAs())
            self.assertTrue(os.path.isfile(os.path.join(exports, "keep.txt")))
            os.remove(os.path.join(exports, "keep.txt"))
            self.assertIsNone(projects_ui.saveCopyAs())
            self.assertTrue(os.path.isdir(exports), "an Exports/ folder that existed stays")
            os.rmdir(exports)
        finally:
            qt.QFileDialog.getSaveFileName = realGetSaveFileName
            self.autosave._service = running
            os.chdir(workingDirectory)
            if savedFolder is None:
                settings.remove("Studio/LastSaveFolder")
            else:
                settings.setValue("Studio/LastSaveFolder", savedFolder)

    def test_29_long_non_ascii_names_stay_under_the_file_name_limit(self):
        """A name is capped in bytes, not only in characters: 84 Japanese characters are 252 bytes, and the temporary scene
        file (+25 bytes) went over the 255 bytes of a file name, so the project was never saved.
        """
        projects = self.projects
        for text in ("脳卒中患者の拡散強調画像" * 7, "😀" * 70, "é" * 300, "x" * 300):
            name = projects.sanitizeName(text)
            self.assertLessEqual(len(name), projects.MAX_NAME_LENGTH)
            self.assertLessEqual(len(name.encode("utf-8")), projects.MAX_NAME_BYTES, text[:3])
            self.assertEqual(name, text[: len(name)], "cut on a character boundary, nothing else changed")
            # The longest file Studio writes for the name: the temporary scene file of the conflict copy
            longest = f"{name}{self.io.CONFLICT_SUFFIX} 99.studio-tmp-12345678{projects.SCENE_EXTENSION}"
            self.assertLess(len(longest.encode("utf-8")), 255)
        self.assertEqual(projects.sanitizeName("脳卒中" * 100, maxLength=1000), "脳卒中" * 22)
        # A copied file keeps its whole extension
        service = self._service()
        fileName = "脳卒中患者の拡散強調画像" * 5 + "脳卒中患者の拡散強調"  # 70 characters, 210 bytes: a file name
        volume, _path = self._userVolume(name=fileName)
        volume.SetName("脳卒中患者の拡散強調画像" * 7)  # 84 characters, 252 bytes
        project = self._projectFor(service)
        self.assertLessEqual(len(project.name.encode("utf-8")), projects.MAX_NAME_BYTES)
        self._save(service)
        self.assertTrue(os.path.isfile(project.scenePath), "the scene file is written")
        self.assertTrue(any(path.endswith(".nii.gz") and "脳卒中" in path for path in self._projectFiles(project)), self._projectFiles(project))
        for path in _relativeFiles(project.path):
            for part in path.split("/"):
                self.assertLess(len(part.encode("utf-8")), 255, path)
        # Rename to a long emoji name: capped, no raw "File name too long"
        renamed = service.renameProject("😀" * 70)
        self.assertLessEqual(len(os.path.basename(renamed).encode("utf-8")), projects.MAX_NAME_BYTES)
        self._save(service)
        self.assertTrue(os.path.isfile(service.project.scenePath))
        self.assertTrue(volume.GetStorageNode().GetFileName().startswith(renamed + os.sep))

    def test_30_continue_card_follows_the_project_created_while_library_shows(self):
        """A scan loaded while Library is shown makes the project a moment later: the Continue card named "Open now · Not in
        a project yet" until the next workspace switch, while the project card, the top bar and the inspector knew the project.
        """
        from StudioLib import library

        def card(canvas):
            return canvas.currentWork.titleLabel.property("fullText"), canvas.currentWork.metaLabel.property("fullText")

        service = self._service()
        self._userVolume()
        canvas = library.LibraryCanvas()
        running = self.autosave._service
        self.autosave._service = service
        try:
            canvas.widget.show()  # Library is shown (a hidden Library lists its projects when it is shown)
            canvas.refresh()
            self.assertIsNone(self.projects.currentProject(), "no project yet")
            self.assertEqual(card(canvas), ("Open now", "Not in a project yet"))
            project = self._projectFor(service)  # the project is made after the card was shown: the listeners are told
            deadline = time.time() + 10
            while time.time() < deadline and card(canvas)[0] == "Open now":
                slicer.app.processEvents()
                time.sleep(0.02)
            self.assertEqual(card(canvas)[0], project.name)
            self.assertIn("Projects \u203a " + project.name, card(canvas)[1])
            self.assertNotIn("Not in a project yet", card(canvas)[1])
            # A card made while the project is already open starts with it
            second = library.LibraryCanvas()
            try:
                second.refreshData()
                self.assertEqual(card(second)[0], project.name)
            finally:
                second.dispose()
        finally:
            self.autosave._service = running
            canvas.widget.hide()
            canvas.dispose()

    def test_31_copy_notice_is_not_replaced_by_the_loaded_toast(self):
        """A scene from outside opens as a copy: the toast "Opened a copy in Projects/<name>. The original is not changed."
        (a consequence, it stays) is still shown after the load; the plain "Loaded <file>" does not replace it.
        """
        from StudioLib import importer, projects_ui, shell

        self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        bundlePath = os.path.join(self.tempDir, "Outside bundle.mrb")
        self.assertTrue(slicer.util.saveScene(bundlePath))
        slicer.mrmlScene.Clear(0)
        service = self._service()
        service.setProjectAnnouncer(projects_ui.announce)
        running = self.autosave._service
        self.autosave._service = service
        studioShell = shell.Shell(force=True)
        try:
            studioShell.apply()
            slicer.app.processEvents()
            importer.loadFiles([bundlePath], studioShell)
            self.io.backgroundWriter().drain(30.0)
            slicer.app.processEvents()
            texts = [text for _time, text, _level in studioShell.recentMessages()]
            self.assertTrue(any(text.startswith("Opened a copy in ") for text in texts), texts)
            self.assertIn("Opened a copy in ", studioShell.toastText())
            self.assertIn("The original is not changed.", studioShell.toastText())
            self.assertTrue(studioShell.consequenceHeld())
            # Nothing is held once the consequence is gone: a plain "Loaded" confirmation is shown as before
            studioShell._stickyToast.hide()
            self.assertFalse(studioShell.consequenceHeld())
            slicer.mrmlScene.Clear(0)
            importer.loadFiles([os.path.join(self.userDir, "MR Head.nrrd")], studioShell)
            self.assertEqual(studioShell.toastText(), "Loaded MR Head")
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()

    def test_32_root_move_into_its_own_content_is_refused_before_anything_is_created(self):
        """Moving the Slicer Studio folder into a folder inside it (a project, Projects/, the folder itself) is refused with a
        plain message before anything is made: it used to fail with "[Errno 22] Invalid argument" and leave the new folder
        (for "Slicer Studio" picked inside a project, a stray folder inside the project) behind.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        oldRoot = projects.root()

        def snapshot():
            found = []
            for folder, _names, _files in os.walk(oldRoot):
                if os.sep + ".studio" not in folder:
                    found.append(os.path.relpath(folder, oldRoot))
            return sorted(found)

        before = snapshot()
        targets = [os.path.join(project.path, "sub"),
                   os.path.join(project.path, "Images", projects.ROOT_FOLDER_NAME),
                   os.path.join(projects.projectsFolder(), "newhome"),
                   os.path.join(oldRoot, "newhome")]
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        running = self.autosave._service
        self.autosave._service = service
        try:
            for target in targets:
                with self.assertRaises(OSError) as context:
                    service.moveRoot(target, moveContent=True)
                self.assertIn("inside the current Slicer Studio folder", str(context.exception))
                self.assertNotIn("Errno", str(context.exception))
                self.assertFalse(os.path.exists(target), target)
                self.assertEqual(snapshot(), before, "no folder is made")
                self.assertEqual(projects.root(), os.path.normpath(self.root), "the setting is unchanged")
                with self.assertRaises(OSError):
                    projects.moveContent(oldRoot, target)
                self.assertFalse(os.path.exists(target), target)
            # Nothing happened to the open project
            self.assertIs(service.project, project)
            self.assertTrue(os.path.isdir(project.path))
            self.assertTrue(os.path.isfile(os.path.join(project.path, ".studio", "lock")), "the project is still locked")
            # The Change… dialog says it before asking anything, and makes nothing
            pick = os.path.join(project.path, "Images", projects.ROOT_FOLDER_NAME)
            shown = []

            class FakeChooser:
                path = pick
                continueButton = mock.Mock()
                setWindowTitle = mock.Mock()
                choose = mock.Mock()
                exec = mock.Mock()
                exec_ = mock.Mock()
                deleteLater = mock.Mock()

                def __init__(self, *args, **kwargs):
                    pass

            def fail(*args, **kwargs):
                raise AssertionError("the question is not asked")

            with mock.patch.object(projects_ui, "FolderChooserDialog", FakeChooser), \
                    mock.patch.object(projects_ui, "_question", fail), \
                    mock.patch.object(slicer.util, "infoDisplay", lambda text, *a, **k: shown.append(text)), \
                    mock.patch.object(slicer.util, "errorDisplay", lambda text, *a, **k: shown.append(text)):
                self.assertIsNone(projects_ui.changeRootDialog(None))
            self.assertEqual(len(shown), 1, shown)
            self.assertIn("inside the current Slicer Studio folder", shown[0])
            self.assertNotIn("Errno", shown[0])
            self.assertFalse(os.path.exists(pick))
            self.assertEqual(snapshot(), before)
            self.assertEqual(projects.root(), os.path.normpath(self.root))
            # A folder outside still works, and so does the parent of the folder
            outside = os.path.join(self.tempDir, "elsewhere", "Slicer Studio")
            self.assertIn("Projects", service.moveRoot(outside, moveContent=True))
            self.assertEqual(projects.root(), os.path.normpath(outside))
        finally:
            self.autosave._service = running
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root



    def test_33_save_a_copy_leaves_the_project_unchanged(self):
        """"Save a copy as…" writes the .mrb without marking the data "modified since read": the next project save does not
        write every node again (the scan opened as .nii.gz was written as a second .nrrd and the scene pointed at it, the
        .nii.gz stayed in Images/ unreferenced), and the scan stays the byte-for-byte copy. Data that was not saved yet stays
        modified. The copy is a bundle that official Slicer loads.
        """
        from StudioLib import projects_ui

        service = self._service()
        volume, scanPath = self._userVolume()
        segmentation, _segmentId, _array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        scan = os.path.join(project.path, "Images", "sub-012_T1w.nii.gz")
        self.assertEqual(_md5File(scan), _md5File(scanPath), "Images/ holds a byte-for-byte copy of the scan")
        before = self._projectFiles(project)
        copyPath = os.path.join(self.tempDir, "A copy.mrb")
        running = self.autosave._service
        self.autosave._service = service
        try:
            self.assertEqual(projects_ui.saveCopyAs(copyPath), copyPath)
        finally:
            self.autosave._service = running
        self.assertTrue(os.path.isfile(copyPath))
        self.assertEqual(slicer.mrmlScene.GetURL(), project.scenePath, "the scene stays in its project")
        self.assertFalse(slicer.mrmlScene.GetStorableNodesModifiedSinceRead(), "the copy does not mark the data as unsaved")
        self._save(service)
        self.assertEqual(self._projectFiles(project), before, "nothing new, nothing stale in the project")
        self.assertEqual(_md5File(scan), _md5File(scanPath), "the scan stays the copy that was made when it was opened")
        with open(project.scenePath, encoding="utf-8") as f:
            sceneText = f.read()
        self.assertIn('fileName="Images/sub-012_T1w.nii.gz"', sceneText, "the scene still points at the scan that was copied")
        self.assertNotIn("sub-012_T1w.nrrd", sceneText)
        self.assertFalse([path for path in _relativeFiles(project.path) if path.startswith(".studio/removed/")])
        # The copy has the data and the scene (a bundle of the official writer: a scene file and a Data folder)
        import zipfile

        with zipfile.ZipFile(copyPath) as archive:
            names = archive.namelist()
        self.assertTrue(any(name.endswith(".mrml") for name in names), names)
        self.assertTrue(any(name.endswith(".seg.nrrd") for name in names), names)
        self.assertTrue(any("sub-012_T1w" in name for name in names), names)
        # Not saved yet stays modified: written by the next save, as before
        segmentation.GetSegmentation().GetNthSegment(0).SetName("Renamed")
        segmentation.StorableModified()
        self.assertTrue(segmentation.GetModifiedSinceRead())
        self.assertTrue(projects_ui.writeBundleKeepingState(slicer.mrmlScene, os.path.join(self.tempDir, "Second copy.mrb")))
        self.assertTrue(segmentation.GetModifiedSinceRead(), "data that was not saved yet is still to be saved")
        self.assertFalse(volume.GetModifiedSinceRead())
        self.assertEqual(slicer.mrmlScene.GetURL(), os.path.join(self.tempDir, "Second copy.mrb"))  # (saveCopyAs restores it)
        slicer.mrmlScene.SetURL(project.scenePath)
        slicer.mrmlScene.SetRootDirectory(project.path)
        self._save(service)
        self.assertEqual([path for path in self._projectFiles(project) if path.endswith(".nrrd") and "sub-012_T1w" in path], [])
        self.assertTrue(os.path.isfile(scan))
        # An unwritable folder is an error, not a crash
        self.assertFalse(projects_ui.writeBundleKeepingState(slicer.mrmlScene, os.path.join(self.tempDir, "missing folder", "x.mrb")))
        slicer.mrmlScene.SetURL(project.scenePath)
        slicer.mrmlScene.SetRootDirectory(project.path)


    def test_34_privacy_check_scans_every_text_member_of_a_bundle(self):
        import zipfile

        checker = _privacyModule()
        if checker is None:
            self.skipTest("StudioPrivacyCheck.py not found")
        cases = {
            "Table.csv": "subject,file\nsub-01,/home/alice/data/sub-01_lesion.nii.gz\n",
            "Notes.txt": "Lesions from /Users/alice/Desktop/patients/\n",
            "Table.tsv": "participant_id\tlesion\nsub-01\t/scratch/alice/lesions/sub-01.nii.gz\n",
        }
        for member, text in cases.items():
            folder = os.path.join(self.tempDir, "privacy-" + member)
            os.makedirs(folder)
            with zipfile.ZipFile(os.path.join(folder, "scene.mrb"), "w") as archive:
                archive.writestr("scene/scene.mrml", '<MRML version="Slicer4.4.0"></MRML>')
                archive.writestr("scene/Data/" + member, text)
            lines = []
            self.assertEqual(checker.scan([folder], out=lines.append), 1, f"{member} inside a .mrb is flagged: {lines}")
            # The same bundle with relative paths only passes
            clean = os.path.join(self.tempDir, "privacy-clean-" + member)
            os.makedirs(clean)
            with zipfile.ZipFile(os.path.join(clean, "scene.mrb"), "w") as archive:
                archive.writestr("scene/scene.mrml", '<MRML version="Slicer4.4.0"></MRML>')
                archive.writestr("scene/Data/" + member, "sub-01,Data/sub-01_lesion.nii.gz\n")
            self.assertEqual(checker.scan([clean], out=lambda _line: None), 0)

    def test_35_utilities_scripts_print_their_usage_for_help(self):
        import io
        import runpy
        import sys

        source = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
        scripts = os.path.join(source, "Utilities", "Scripts")
        names = ["StudioPrivacyCheck.py", os.path.join("StudioSettingsCheck", "analyse.py"), os.path.join("StudioSettingsCheck", "inidiff.py")]
        if not all(os.path.isfile(os.path.join(scripts, name)) for name in names):
            self.skipTest("Utilities/Scripts not found")
        for name in names:
            for flag in ("--help", "-h"):
                saved = sys.argv, sys.stdout, list(sys.path)
                sys.argv, sys.stdout = [name, flag], io.StringIO()
                status = None
                try:
                    runpy.run_path(os.path.join(scripts, name), run_name="__main__")
                except SystemExit as exit:
                    status = exit.code
                finally:
                    printed = sys.stdout.getvalue()
                    sys.argv, sys.stdout, sys.path[:] = saved
                self.assertIn(status, (0, None), f"{name} {flag} exits with 0")
                self.assertIn("usage:", printed, f"{name} {flag} prints its usage text")

    def test_35b_utilities_scripts_report_bad_arguments_without_a_traceback(self):
        """Bad arguments end with a one-line error and exit status 2, not a Python traceback (BB-1)."""
        import subprocess
        import sys

        source = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))))
        scripts = os.path.join(source, "Utilities", "Scripts")
        if not os.path.isdir(os.path.join(scripts, "StudioSettingsCheck")):
            self.skipTest("Utilities/Scripts not found")
        missing = os.path.join(self.tempDir, "does-not-exist")
        base = os.path.join(self.tempDir, "base.ini")
        with open(base, "w") as stream:
            stream.write("[General]\nkey=1\n")
        cases = [
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), base, base], "expected three ini files"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py")], "expected three ini files"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), "--keys"], "--keys needs exactly one ini file"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), base, base, missing], f"no such ini file: {missing}"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), missing, base, base], f"no such ini file: {missing}"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), "--keys", missing], f"no such ini file: {missing}"),
            ([os.path.join("StudioSettingsCheck", "inidiff.py"), "--keys", self.tempDir], f"no such ini file: {self.tempDir}"),
            ([os.path.join("StudioSettingsCheck", "analyse.py"), missing], "not the output folder"),
            ([os.path.join("StudioSettingsCheck", "analyse.py")], "expected one argument"),
            (["StudioReadmeCheck.py", "--readme", missing], "README file not found"),
            (["StudioReadmeCheck.py", "--state", missing], "state file not found"),
        ]
        for arguments, message in cases:
            # a plain Python interpreter (never the Slicer launcher): the scripts only need the standard library
            result = subprocess.run([sys.executable if os.path.basename(sys.executable).lower().startswith("python") else "python3", "-I",
                                     os.path.join(scripts, arguments[0])] + arguments[1:], capture_output=True, text=True, timeout=120, check=False)
            label = " ".join(arguments)
            self.assertEqual(result.returncode, 2, f"{label}: {result.stderr}")
            self.assertNotIn("Traceback", result.stderr, label)
            self.assertIn(message, result.stderr, label)

    def test_36_unwritable_project_folder_is_a_warning_in_plain_words(self):
        """A project folder that cannot be written (Ctrl+S) is anticipated: a warning without a traceback, and a reason
        without the error number and the temporary file path (BB-1).
        """
        import logging
        from unittest import mock

        service = self._service()
        self._userVolume("Read only", shape=(20, 24, 28))
        project = self._projectFor(service)
        self._save(service)
        slicer.util.getNode("Read only").SetSpacing(2.0, 2.0, 2.0)
        slicer.util.getNode("Read only").Modified()
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Recorder(level=logging.DEBUG)
        temporary = os.path.join(project.path, "Images", ".studio-tmp-04a5c3ad")
        error = PermissionError(13, "Permission denied", temporary)
        logging.getLogger().addHandler(handler)
        try:
            with mock.patch.object(type(service.projectWriter), "runMainThreadStep", side_effect=error):
                self.assertFalse(service.saveNow("test", progress=False))
        finally:
            logging.getLogger().removeHandler(handler)
        self.io.backgroundWriter().drain(30.0)
        self.assertEqual(service._projectPausedReason, "Permission denied")
        self.assertEqual([record for record in records if record.levelno >= logging.ERROR], [])
        self.assertTrue([record for record in records if record.levelno == logging.WARNING])
        self.assertFalse([record for record in records if record.exc_info or "Traceback" in record.getMessage()])
        # Nothing stays pending: the next save works once the folder is writable again
        self.assertTrue(service.saveNow("test", progress=False), service._projectPausedReason)
        self.io.backgroundWriter().drain(30.0)

    def test_37_open_study_starts_in_studies_not_above_the_last_folder(self):
        """Open study… without a recent study starts in Studies/ (as New study… does), else in the last open folder itself
        (not its parent: in the web build that was "/"); with a recent study it starts in the folder that holds it.
        """
        from unittest import mock

        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("studies are not part of this build")
        projects = self.projects
        projects.ensureRoot()
        starts = []

        class FileDialog:
            @staticmethod
            def getExistingDirectory(parent, title, start, *args):
                starts.append(start)
                return ""

        class FakeQt:
            QFileDialog = FileDialog

            def __getattr__(self, name):
                return getattr(qt, name)

        settings = qt.QSettings()
        saved = {key: settings.value(key) for key in ("Studio/LastOpenDirectory", study_ui.RECENT_STUDIES_KEY)}
        lastFolder = os.path.join(self.tempDir, "last", "scans")
        os.makedirs(lastFolder)
        settings.setValue("Studio/LastOpenDirectory", lastFolder)
        settings.setValue(study_ui.RECENT_STUDIES_KEY, "")
        study_ui.qt = FakeQt()
        try:
            study_ui.openStudyDialog()
            with mock.patch.object(study_ui, "_studiesFolder", return_value=(None, None)):
                study_ui.openStudyDialog()
            recentFolder = os.path.join(self.tempDir, "recent", "Stroke")
            os.makedirs(recentFolder)
            study_ui.createStudy([], folder=recentFolder, openIt=False)
            study_ui.recordRecentStudy(recentFolder, "Stroke")
            study_ui.openStudyDialog()
        finally:
            study_ui.qt = qt
            for key, value in saved.items():
                settings.setValue(key, value if value is not None else "")
        self.assertEqual(os.path.normpath(starts[0]), os.path.normpath(projects.studiesFolder()))
        self.assertEqual(os.path.normpath(starts[1]), os.path.normpath(lastFolder))
        self.assertEqual(os.path.normpath(starts[2]), os.path.normpath(os.path.dirname(recentFolder)))

    def test_38_4d_nifti_is_saved_as_a_sequence_official_5_12_reads(self):
        """A 4D NIfTI image (fMRI) is saved into the project as .seq.nrrd, never as a copy of the NIfTI: official 3D Slicer
        5.12 reads volume sequences from NRRD only. A project saved before with Other/<name>.nii.gz is rewritten as well.
        """
        import SimpleITK as sitk

        from StudioLib import importer

        frames = []
        for index in range(4):
            frame = sitk.GetImageFromArray(np.full((6, 7, 8), (index + 1) * 10.0, np.float32))
            frame.SetSpacing((2.0, 2.0, 2.0))
            frames.append(frame)
        image = sitk.JoinSeries(frames)
        image.SetSpacing((2.0, 2.0, 2.0, 1.5))
        source = os.path.join(self.userDir, "sub-02_task-motor_bold.nii.gz")
        sitk.WriteImage(image, source)
        sourceMd5 = _md5File(source)
        service = self._service()
        importer.loadSequence(source)
        project = self._projectFor(service)
        self._save(service)
        projectPath, scenePath = project.path, project.scenePath
        files = self._projectFiles(project)
        self.assertFalse([name for name in files if name.endswith(".nii.gz")], files)
        sequenceFile = "Other/sub-02_task-motor_bold.seq.nrrd"
        self.assertIn(sequenceFile, files)
        with open(os.path.join(projectPath, sequenceFile), "rb") as f:
            self.assertTrue(f.read(4) == b"NRRD", "official 3D Slicer 5.12 reads it")
        with open(scenePath, encoding="utf-8") as f:
            sceneText = f.read()
        self.assertIn(f'fileName="{sequenceFile}"', sceneText)
        self.assertNotIn('.nii.gz"', sceneText)
        sequence = slicer.util.getNodesByClass("vtkMRMLSequenceNode")[0]
        self.assertFalse(sequence.GetModifiedSinceRead())
        self.assertEqual(_md5File(source), sourceMd5, "the original is untouched")

        def reopenedFrames():
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)
            self.assertTrue(service.openProject(projectPath))
            sequences = slicer.util.getNodesByClass("vtkMRMLSequenceNode")
            self.assertEqual(len(sequences), 1)
            values = []
            for index in range(sequences[0].GetNumberOfDataNodes()):
                array = slicer.util.arrayFromVolume(sequences[0].GetNthDataNode(index))
                self.assertEqual(array.shape, (6, 7, 8))
                values.append(float(array.min()) if array.min() == array.max() else None)
            return values

        self.assertEqual(reopenedFrames(), [10.0, 20.0, 30.0, 40.0])
        # A project saved by an earlier version (the sequence kept as Other/<name>.nii.gz) is rewritten on the next save
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        os.remove(os.path.join(projectPath, sequenceFile))
        shutil.copy2(source, os.path.join(projectPath, "Other", "sub-02_task-motor_bold.nii.gz"))
        with open(scenePath, "w", encoding="utf-8") as f:
            f.write(sceneText.replace(f'fileName="{sequenceFile}"', 'fileName="Other/sub-02_task-motor_bold.nii.gz"'))
        self.assertTrue(service.openProject(projectPath))
        self._save(service)
        with open(scenePath, encoding="utf-8") as f:
            sceneText = f.read()
        self.assertIn(f'fileName="{sequenceFile}"', sceneText)
        self.assertNotIn('.nii.gz"', sceneText)
        self.assertIn(sequenceFile, self._projectFiles(service.project))
        self.assertEqual(reopenedFrames(), [10.0, 20.0, 30.0, 40.0])

    def test_39_disk_full_during_a_save_keeps_the_saved_files(self):
        """The disk fills up while a save runs (BB-1): the C++ writers (vtkMRMLScene::Commit, teem's nrrdSave) then
        leave a truncated or empty file that they report as written. The save fails ("Not saved" with the reason),
        the project's scene and lesion files stay as they were, quitting is not told that everything is saved, and the
        next save once space is free writes everything again.
        """
        import shutil as shutilModule
        import xml.etree.ElementTree as ET
        from unittest import mock

        from StudioLib import thumbnails

        service = self._service()
        volume, _path = self._userVolume("Disk full", shape=(20, 24, 28))
        project = self._projectFor(service)
        segmentation, segmentId, array = self._lesion(volume, name="Lesion")
        self._save(service)
        lesionPath = os.path.join(project.path, "Segmentations", "Lesion.seg.nrrd")
        saved = {path: _md5File(path) for path in (project.scenePath, lesionPath)}
        # The lesion is edited again; the disk is full from the moment the save started (after its free-space check)
        array[12:16, 12:16, 12:16] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
        realDiskUsage = shutilModule.disk_usage

        def diskUsage(path):
            return realDiskUsage(path)._replace(free=0 if service.plan is not None else 1 << 30)

        with mock.patch.object(shutilModule, "disk_usage", side_effect=diskUsage):
            self.assertTrue(service.autosaveNow("test"))
            self.assertTrue(service.waitForIdle(120.0))
            status = service.status()
            self.assertEqual(status.state, "error", status.message)
            self.assertIn("Not saved", status.message)
            self.assertEqual(service._projectPausedReason, os.strerror(28))
            self.assertEqual({path: _md5File(path) for path in saved}, saved, "the saved files are kept")
            ET.parse(project.scenePath)
            self.assertTrue(segmentation.GetModifiedSinceRead(), "the lesion stays unsaved")
            self.assertEqual([name for name in _relativeFiles(project.path) if ".studio-tmp" in name or name.endswith((".tmp", ".part"))], [])
            # Quitting is not told that everything is saved (the disk is still full during the quit save)
            self.assertFalse(service.flushForClose())
        # Space is free again: Ctrl+S writes the lesion and the scene
        self.assertTrue(service.saveNow("test", progress=False), service._projectPausedReason)
        self.io.backgroundWriter().drain(30.0)
        self.assertNotEqual(_md5File(lesionPath), saved[lesionPath])
        ET.parse(project.scenePath)
        self.assertFalse(segmentation.GetModifiedSinceRead())
        self.assertEqual(service.status().state, "idle")
        # A scene file that cannot be written completely is reported as not written (vtkMRMLScene::Commit)
        self.assertFalse(self.io.sceneFileComplete(os.path.join(self.tempDir, "missing.mrml")))
        truncated = os.path.join(self.tempDir, "truncated.mrml")
        with open(project.scenePath, "rb") as f:
            content = f.read()
        with open(truncated, "wb") as f:
            f.write(content[: len(content) // 2])
        self.assertFalse(self.io.sceneFileComplete(truncated))
        self.assertTrue(self.io.sceneFileComplete(project.scenePath))
        if os.path.exists("/dev/full"):
            scene = slicer.vtkMRMLScene()
            scene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Volume")
            self.assertEqual(scene.Commit("/dev/full"), 0)
        # A thumbnail that cannot be written leaves no temporary file behind
        blocked = os.path.join(self.tempDir, "thumbnails", "blocked.png")
        os.makedirs(os.path.join(blocked, "not empty"))
        with self.assertRaises(OSError):
            thumbnails._writeAtomic(blocked, b"png")
        self.assertEqual(sorted(os.listdir(os.path.dirname(blocked))), ["blocked.png"])

    def test_40_root_move_is_refused_while_another_studio_has_a_project_open(self):
        """Moving the Slicer Studio folder while another running Slicer Studio has one of its projects open (BB-1) is
        refused before anything is saved or moved, with a message that names the project, as rename and Move to Trash
        already are. The folder used to move with that project and its lock: the other window went on saving into the old
        folder, outside the Slicer Studio folder, and its later work dropped out of the Library.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        oldRoot = projects.root()
        # Another Slicer Studio has "Busy" open (its lock is held; the same process stands for the other one)
        busy = projects.createProject("Busy")
        self.assertTrue(busy.lock())
        self.assertTrue(projects.isLockedByOther(busy.path))
        self.assertEqual(projects.projectsOpenElsewhere(oldRoot), ["Busy"], "the own open project is not listed")
        newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        running = self.autosave._service
        self.autosave._service = service

        def unchanged():
            self.assertFalse(os.path.exists(newRoot), "nothing is made in the new folder")
            self.assertTrue(os.path.isfile(os.path.join(busy.path, ".studio", "lock")), "the other project keeps its lock")
            self.assertEqual(projects.root(), os.path.normpath(self.root), "the setting is unchanged")
            self.assertIs(service.project, project)
            self.assertTrue(os.path.isdir(project.path))
            self.assertTrue(project.isLocked(), "the own project stays locked")

        try:
            with self.assertRaises(projects.OpenElsewhereError) as context:
                service.moveRoot(newRoot, moveContent=True)
            self.assertIn("Busy is open in another Slicer Studio", str(context.exception))
            unchanged()
            # The Change… dialog says it before asking anything
            shown = []

            class FakeChooser:
                path = newRoot
                continueButton = mock.Mock()
                setWindowTitle = mock.Mock()
                choose = mock.Mock()
                exec = mock.Mock()
                exec_ = mock.Mock()
                deleteLater = mock.Mock()

                def __init__(self, *args, **kwargs):
                    pass

            def fail(*args, **kwargs):
                raise AssertionError("the question is not asked")

            with mock.patch.object(projects_ui, "FolderChooserDialog", FakeChooser), \
                    mock.patch.object(projects_ui, "_question", fail), \
                    mock.patch.object(slicer.util, "infoDisplay", lambda text, *a, **k: shown.append(text)), \
                    mock.patch.object(slicer.util, "errorDisplay", lambda text, *a, **k: shown.append(text)):
                self.assertIsNone(projects_ui.changeRootDialog(None))
            self.assertEqual(len(shown), 1, shown)
            self.assertIn("Busy is open in another Slicer Studio", shown[0])
            unchanged()
            # Once the other window has closed it, the folder moves
            busy.unlock()
            self.assertEqual(projects.projectsOpenElsewhere(oldRoot), [])
            # A project opened elsewhere while the own project saves before the move: still refused, nothing moved
            realSaveNow = service.saveNow

            def saveThenOpenElsewhere(*args, **kwargs):
                result = realSaveNow(*args, **kwargs)
                self.assertTrue(busy.lock())
                return result

            with mock.patch.object(service, "saveNow", saveThenOpenElsewhere):
                with self.assertRaises(projects.OpenElsewhereError):
                    service.moveRoot(newRoot, moveContent=True)
            unchanged()
            self.assertEqual(service._suspendCount, 0, "autosave runs again")
            busy.unlock()
            moved = service.moveRoot(newRoot, moveContent=True)
            self.assertIn("Projects", moved)
            self.assertEqual(projects.root(), os.path.normpath(newRoot))
            self.assertTrue(service.project.path.startswith(os.path.normpath(newRoot) + os.sep))
        finally:
            busy.unlock()
            self.autosave._service = running
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_41_save_lesion_follows_a_renamed_or_moved_project(self):
        """Save lesion (the CTA and Ctrl+Shift+S) after the open project was renamed, or the Slicer Studio folder moved
        (BB-1), writes the lesion's file in the project where it is now. It used to write to the absolute path of the
        first save: the old project folder was made again (a ghost project in the Library), the project kept the older
        lesion, and the Lesion card still showed the old path.
        """
        from StudioLib import draw

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        segmentation, segmentId, array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)

        def voxels(path):
            node = slicer.util.loadLabelVolume(path, {"show": False})
            try:
                return int(np.count_nonzero(slicer.util.arrayFromVolume(node)))
            finally:
                slicer.mrmlScene.RemoveNode(node)

        def paint(box):
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)

        originalNodes = draw.lesionNodes
        draw.lesionNodes = lambda editor=None: (segmentation, volume)
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        try:
            first = os.path.join(project.path, projects.LESIONS, "lesA.nii.gz")
            self.assertEqual(draw.saveLesionDialog(None, path=first), first)
            firstCount = voxels(first)
            oldPath = project.path
            # Rename the open project: Save follows it
            newPath = service.renameProject("Patient A")
            renamedLesion = os.path.join(newPath, projects.LESIONS, "lesA.nii.gz")
            self.assertEqual(draw.lesionSaveState(segmentation)[:2], ("saved", renamedLesion), "the state shows the new path")
            paint(np.s_[12:16, 14:20, 16:22])
            self.assertEqual(draw.lesionSaveState(segmentation)[0], "unsaved")
            self.assertEqual(draw.saveLesionNow(), renamedLesion)
            self.assertFalse(os.path.exists(oldPath), "the folder of the old name is not made again")
            self.assertEqual([info.name for info in projects.listProjects()], ["Patient A"], "no ghost project")
            secondCount = voxels(renamedLesion)
            self.assertGreater(secondCount, firstCount, "the project's own lesion has the new paint")
            self.assertEqual(draw.lesionSaveState(segmentation)[:2], ("saved", renamedLesion))
            # Move the Slicer Studio folder: Save follows the project into the new folder
            newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
            service.moveRoot(newRoot, moveContent=True)
            movedLesion = os.path.join(service.project.path, projects.LESIONS, "lesA.nii.gz")
            self.assertTrue(movedLesion.startswith(os.path.normpath(newRoot) + os.sep))
            self.assertEqual(draw.lesionSaveState(segmentation)[:2], ("saved", movedLesion))
            paint(np.s_[18:22, 14:20, 16:22])
            self.assertEqual(draw.saveLesionNow(), movedLesion)
            self.assertFalse(os.path.exists(os.path.join(self.root, "Projects")), "nothing is made in the old folder")
            self.assertGreater(voxels(movedLesion), secondCount)
            # The folder of the saved file is gone (deleted outside Slicer Studio): Save asks, it never makes it again
            shutil.rmtree(os.path.dirname(movedLesion))
            paint(np.s_[24:26, 14:20, 16:22])
            dialogs = []
            originalDialog = draw.saveLesionDialog
            draw.saveLesionDialog = lambda shell=None, path=None: dialogs.append(path)
            try:
                self.assertIsNone(draw.saveLesionNow())
            finally:
                draw.saveLesionDialog = originalDialog
            self.assertEqual(dialogs, [None])
            self.assertFalse(os.path.exists(os.path.dirname(movedLesion)))
        finally:
            draw.lesionNodes = originalNodes
            draw._savedRecords.clear()
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_42_root_move_takes_your_templates_along(self):
        """Change folder > Move moves Templates/ with Projects/ and Studies/ (BB-1): your templates stay listed and the default
        stays yours. It used to leave Templates/ in the old folder, so your templates vanished from every list, the default fell
        back to the bundled brain and studies that use a template of yours reported it as not registered. A folder that holds
        only templates is offered the move too.
        """
        from StudioLib import templates

        projects = self.projects
        self.assertEqual(projects.TEMPLATES_DIR, templates.TEMPLATES_DIR)
        settings = qt.QSettings()
        previousDefault = settings.value(templates.DEFAULT_KEY)
        try:
            image = templates.builtinTemplates()[0].imagePath
            mine = templates.addTemplate(image, name="My lab template", makeDefault=True)
            self.assertEqual([t.id for t in templates.userTemplates()], [mine.id])
            self.assertEqual(templates.defaultTemplateId(), mine.id)
            self.assertTrue(projects.hasContent(self.root), "a folder with only your templates is offered the move")
            self.assertIn(projects.TEMPLATES_DIR, projects.movableItems(self.root))
            slicer.mrmlScene.Clear(0)
            service = self._service()
            self._userVolume()
            self._projectFor(service)
            self._save(service)
            os.environ.pop(projects.ROOT_ENVIRONMENT)
            settings.setValue(projects.ROOT_KEY, self.root)
            newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
            moved = service.moveRoot(newRoot, moveContent=True)
            self.assertIn(projects.TEMPLATES_DIR, moved)
            self.assertEqual(projects.root(), os.path.normpath(newRoot))
            self.assertFalse(os.path.exists(os.path.join(self.root, projects.TEMPLATES_DIR)), "nothing stays in the old folder")
            self.assertEqual([t.id for t in templates.userTemplates()], [mine.id], "your template is still listed")
            self.assertTrue(templates.userTemplates()[0].folder.startswith(os.path.normpath(newRoot) + os.sep))
            self.assertEqual(templates.defaultTemplateId(), mine.id, "and still the default")
            # Into a folder that already has templates: the two Templates/ folders are merged
            third = os.path.join(self.tempDir, "third", "Slicer Studio")
            theirs = templates.addTemplate(image, name="Their template", rootPath=third)
            moved = service.moveRoot(third, moveContent=True)
            self.assertIn(projects.TEMPLATES_DIR, moved)
            self.assertEqual(sorted(t.id for t in templates.userTemplates()), sorted([mine.id, theirs.id]))
            self.assertEqual(templates.defaultTemplateId(), mine.id)
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            if previousDefault is None:
                settings.remove(templates.DEFAULT_KEY)
            else:
                settings.setValue(templates.DEFAULT_KEY, previousDefault)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            templates.registerGrids(force=True)

    def test_43_duplicate_of_the_open_project_holds_the_latest_edits(self):
        """Library card > Duplicate of the open project saves it first (as Rename, Open project and Save a copy as… do), so the
        copy holds what the user sees (BB-1). It used to copy the folder as the last autosave left it: a lesion drawn since
        then (up to IdleSeconds + IntervalMinutes) was missing from the copy, while the toast said "Duplicated as". When the
        save fails, nothing is copied.
        """
        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        volume, _scanPath = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        # Edits after the last save: a lesion and a markup, not saved yet
        self._lesion(volume, name="New lesion")
        markup = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Points")
        markup.AddControlPoint(1, 2, 3)
        self.assertFalse(os.path.isdir(os.path.join(project.path, "Segmentations")), "the edits are not on disk yet")
        running = self.autosave._service
        self.autosave._service = service
        try:
            returned = projects_ui.duplicate(project.path)
            self.io.backgroundWriter().drain(30.0)
            copyPath = os.path.join(os.path.dirname(project.path), os.path.basename(project.path) + " copy")
            self.assertTrue(os.path.isdir(copyPath), copyPath)
            files = _relativeFiles(copyPath)
            self.assertTrue([f for f in files if f.startswith("Segmentations/") and f.endswith(".seg.nrrd")], files)
            self.assertTrue([f for f in files if f.startswith("Markups/") and f.endswith(".mrk.json")], files)
            self.assertEqual(returned, copyPath)
            with open(projects.sceneFile(copyPath), encoding="utf-8") as f:
                sceneText = f.read()
            self.assertIn('name="New lesion"', sceneText)
            self.assertIn('name="Points"', sceneText)
            self.assertFalse(os.path.exists(os.path.join(copyPath, ".studio", "lock")))
            self.assertEqual(projects.currentProject().path, project.path, "the open project stays open")
            # When the open project cannot be saved, nothing is copied (no "Duplicated as" with stale data)
            markup.AddControlPoint(4, 5, 6)
            before = sorted(os.listdir(os.path.dirname(project.path)))
            saveNow = service.saveNow
            service.saveNow = lambda *args, **kwargs: False
            try:
                self.assertIsNone(projects_ui.duplicate(project.path))
            finally:
                service.saveNow = saveNow
            self.io.backgroundWriter().drain(30.0)
            self.assertEqual(sorted(os.listdir(os.path.dirname(project.path))), before)
            # Another project (not the open one) is copied as it is, without saving the open project
            otherPath = projects_ui.duplicate(copyPath)
            self.io.backgroundWriter().drain(30.0)
            self.assertTrue(os.path.isfile(os.path.join(otherPath, os.path.basename(otherPath) + ".mrml")))
        finally:
            self.autosave._service = running

    def test_44_recent_outside_scene_opens_as_its_own_project(self):
        """Library > Recent on a scene from outside the folder (.mrb or .mrml) while a project is open opens the scene as its
        own project, as Open... and drop do (A-09, BB-1): the open project is saved and closed and keeps only its data. It used
        to trigger the recent file action, which added the scene's nodes to the open project, and autosave wrote them into
        that project's folder (two subjects mixed in one project).
        """
        from StudioLib import importer, shell

        projects = self.projects
        volume, _path = self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        segmentation, _segmentId, _array = self._lesion(volume, "Outside lesion")
        outside = os.path.join(self.tempDir, "outside recent")
        os.makedirs(outside)
        bundlePath = os.path.join(outside, "Outside bundle.mrb")
        self.assertTrue(slicer.util.saveScene(bundlePath))
        self.assertTrue(slicer.util.saveNode(segmentation, os.path.join(outside, "Outside lesion.seg.nrrd")))
        scenePath = os.path.join(outside, "Outside scene.mrml")
        self.assertTrue(slicer.util.saveScene(scenePath))
        bundleMd5 = _md5File(bundlePath)
        sceneMd5 = _md5File(scenePath)
        slicer.mrmlScene.Clear(0)
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, project.name if project else None)))
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        studioShell = shell.Shell(force=True)
        try:
            studioShell.apply()
            slicer.app.processEvents()
            for index, (path, expectedName) in enumerate(((bundlePath, "Outside bundle"), (scenePath, "Outside scene"))):
                if service.project is not None:
                    self._save(service)
                slicer.mrmlScene.Clear(0)
                self._userVolume(f"Own scan {index}", shape=(12, 14, 16))
                own = self._projectFor(service)
                self._save(service)
                ownPath = own.path
                del announced[:]
                result = importer.loadRecentFile({"fileName": path, "fileType": "SceneFile"}, None, studioShell)
                self.io.backgroundWriter().drain(30.0)
                self.assertEqual(result.scenes, [path])
                self.assertIsNotNone(service.project)
                self.assertNotEqual(service.project.path, ownPath, "the scene is not added to the open project")
                self.assertEqual(service.project.name, expectedName)
                self.assertIn(("copy", expectedName), announced)
                self.assertEqual(projects.readJson(projects.projectFile(ownPath))["state"], "closed")
                self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName(f"Own scan {index}"))
                for name in ("MR Head", "Outside lesion"):
                    self.assertEqual(len(slicer.util.getNodes(name, useLists=True).get(name, [])), 1, name)
                ownFiles = _relativeFiles(ownPath)
                self.assertEqual([f for f in ownFiles if "MR Head" in f or "Outside" in f], [], ownFiles)
                with open(projects.sceneFile(ownPath), encoding="utf-8") as f:
                    self.assertNotIn('name="MR Head"', f.read())
            self.assertEqual(_md5File(bundlePath), bundleMd5, "the original is not changed")
            self.assertEqual(_md5File(scenePath), sceneMd5, "the original is not changed")
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()

    def test_45_missing_data_file_of_a_project_is_named_and_the_rest_keeps_saving(self):
        """A project whose data file was deleted or damaged outside Slicer Studio (Finder, a partial copy) opens with the rest
        and names what could not be loaded (BB-1). Opening it raised an uncaught RuntimeError, no file was named, the node
        without data stayed in the scene so every save failed ("write failed") and quitting asked "Save before exit?".
        The damaged file itself is left where it is.
        """
        service = self._service()
        for name in ("Keep", "Gone", "Broken"):
            self._userVolume(name, shape=(12, 14, 16), extension=".nrrd")
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        files = self._projectFiles(project)
        gonePath = os.path.join(projectPath, next(path for path in files if os.path.basename(path).startswith("Gone.")))
        brokenPath = os.path.join(projectPath, next(path for path in files if os.path.basename(path).startswith("Broken.")))
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        os.remove(gonePath)
        with open(brokenPath, "r+b") as file:
            file.truncate(300)
        with open(brokenPath, "rb") as file:
            brokenBytes = file.read()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))

        self.assertTrue(service.openProject(projectPath), "the project opens with the data that could be read")
        self.assertEqual(service.project.path, projectPath)
        self.assertEqual([event for event, _info in announced], ["opened"])
        self.assertEqual(sorted(announced[0][1].get("unread") or []), ["Broken", "Gone"], "the files that could not be read are named")
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Keep"))
        self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("Gone"), "a node without data is not kept")
        self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("Broken"), "a node without data is not kept")
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "New points")
        points.AddControlPoint(1, 2, 3)
        self.assertTrue(service.saveNow("test", progress=False), "the rest of the project keeps saving")
        self.io.backgroundWriter().drain(30.0)
        self.assertTrue(service.lastSaveComplete())
        modified = vtk.vtkCollection()
        slicer.mrmlScene.GetStorableNodesModifiedSinceRead(modified)
        names = [modified.GetItemAsObject(index).GetName() for index in range(modified.GetNumberOfItems())]
        self.assertEqual(names, [], "nothing is left unsaved (quitting does not ask about it)")
        self.assertTrue(any(path.startswith("Markups/") for path in self._projectFiles(service.project)))
        with open(brokenPath, "rb") as file:
            self.assertEqual(file.read(), brokenBytes, "the damaged file is left as it is")

        # Opened again: the saved project has only the data that loads, nothing is named any more
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        del announced[:]
        self.assertTrue(service.openProject(projectPath))
        self.assertEqual(announced, [("opened", {})])
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Keep"))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("New points"))

    def test_47b_ctrl_s_right_after_opening_a_scan_saves_the_project(self):
        """Ctrl+S in the first second after a scan was opened (BB-1). The project is made by the autosave poll (up to 1 s
        later), so Ctrl+S found none and opened the classic "Save Scene and Unsaved Data" dialog, which wrote files into the
        home folder. It now makes the project first (D-A16: Ctrl+S saves now); the classic dialog stays for an empty scene,
        Classic and a study. Here the classic Save action is replaced by one that only counts (a dialog would block the test).
        """
        from StudioLib import projects_ui

        mainWindow = slicer.util.mainWindow()
        real = mainWindow.findChild("QAction", "FileSaveSceneAction")
        self.assertIsNotNone(real)
        real.setObjectName("FileSaveSceneActionHidden")
        standIn = qt.QAction(mainWindow)
        standIn.setObjectName("FileSaveSceneAction")
        classicDialogs = []
        standIn.connect("triggered()", lambda: classicDialogs.append(1))
        running = self.autosave._service
        studio = self._service()
        classic = self._service(studio=False)
        try:
            self.autosave._service = studio
            # An empty scene has nothing to save: no project is made, the classic dialog is the fallback
            self.assertIsNone(studio.ensureProject())
            self.assertFalse(projects_ui.saveNow())
            self.assertEqual(len(classicDialogs), 1)
            self.assertIsNone(studio.project)
            self._userVolume()
            self.assertIsNone(studio.project, "no poll has run: the project does not exist yet")
            self.assertTrue(projects_ui.saveNow(), "Ctrl+S makes the project and saves into it")
            self.assertEqual(len(classicDialogs), 1, "the classic Save dialog did not open")
            self.io.backgroundWriter().drain(30.0)
            self.assertIsNotNone(studio.project)
            self.assertTrue(studio.lastSaveComplete())
            self.assertTrue(os.path.isfile(studio.project.scenePath))
            self.assertTrue(any(path.startswith("Images/") for path in self._projectFiles(studio.project)))
            self.assertIs(studio.ensureProject(), studio.project, "the project that is open is returned as it is")
            # Classic: no project for the scene (projects are made in Studio only)
            studio.releaseProject()
            self.autosave._service = classic
            self.assertIsNone(classic.ensureProject())
            self.assertIsNone(classic.project)
        finally:
            self.autosave._service = running
            standIn.setObjectName("")
            standIn.deleteLater()
            real.setObjectName("FileSaveSceneAction")
        self.assertEqual(len(self.projects.listProjects(self.root)), 1, "one project, made once")

    def test_47c_new_project_close_scene_and_quit_right_after_opening_a_scan_save_the_project(self):
        """Ctrl+N, Ctrl+W and quit in the first second after a scan was opened (BB-1). No project existed before the next
        autosave poll, so they fell back to the official "Save before closing scene?" / "Save before exit?" question. They
        make the project first, as Ctrl+S does, and save into it (D-A16). Here the official Close Scene action is replaced
        by one that only counts (the question would block the test).
        """
        from StudioLib import projects_ui

        mainWindow = slicer.util.mainWindow()
        real = mainWindow.findChild("QAction", "FileCloseSceneAction")
        self.assertIsNotNone(real)
        real.setObjectName("FileCloseSceneActionHidden")
        standIn = qt.QAction(mainWindow)
        standIn.setObjectName("FileCloseSceneAction")
        official = []
        standIn.connect("triggered()", lambda: official.append(1))
        running = self.autosave._service
        studio = self._service()
        try:
            self.autosave._service = studio
            for key in ("quit", "newProject", "closeScene"):
                self._userVolume()
                self.assertIsNone(studio.project, "no poll has run: the project does not exist yet")
                if key == "quit":
                    self.assertTrue(studio.flushForClose(), "quitting saves into a project made now")
                elif key == "newProject":
                    studio.newProject()
                else:
                    self.assertTrue(projects_ui.closeScene())
                self.io.backgroundWriter().drain(30.0)
                self.assertEqual(len(self.projects.listProjects(self.root)), 1 if key == "quit" else {"newProject": 2, "closeScene": 3}[key],
                                 key + ": the data became a project")
                studio.releaseProject()
                slicer.mrmlScene.Clear(0)
            # New project and Close scene close through the official action only after the save (it asks nothing then)
            closedAfterSave = len(official)
            # An empty scene keeps the official action
            self.assertTrue(projects_ui.closeScene())
            self.assertEqual(len(official), closedAfterSave + 1)
        finally:
            self.autosave._service = running
            standIn.setObjectName("")
            standIn.deleteLater()
            real.setObjectName("FileCloseSceneAction")

    def test_46_root_move_across_two_mounts_of_one_disk_copies(self):
        """Change folder > Move from a bind mount (the web build's /data, a Docker volume) to a folder of the same disk
        (BB-1). Both have one st_dev, so the move took the rename path only, and rename(2) refuses to cross mount points
        (EXDEV): nothing moved, "[Errno 18] Invalid cross-device link" and an empty new folder were left. It now copies,
        checks and deletes, as between two disks. Here rename refuses EXDEV between the two folders, as two mounts do.
        """
        import errno
        from unittest import mock

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        os.makedirs(os.path.join(self.root, "Studies", "Study A"))
        with open(os.path.join(self.root, "Studies", "Study A", "study.json"), "w") as f:
            json.dump({"name": "Study A"}, f)
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        newRoot = os.path.join(self.tempDir, "other mount", "Slicer Studio")
        oldPrefix = os.path.normpath(self.root) + os.sep
        newPrefix = os.path.normpath(newRoot) + os.sep
        realRename = os.rename
        refused = []

        def crossMount(source, destination, *args, **kwargs):
            if os.path.normpath(os.fspath(source)).startswith(oldPrefix) and os.path.normpath(os.fspath(destination)).startswith(newPrefix):
                refused.append(os.fspath(source))
                raise OSError(errno.EXDEV, "Invalid cross-device link", os.fspath(source), None, os.fspath(destination))
            return realRename(source, destination, *args, **kwargs)

        try:
            with mock.patch.object(projects, "_sameVolume", return_value=True), mock.patch.object(projects.os, "rename", side_effect=crossMount):
                moved = service.moveRoot(newRoot, moveContent=True)
            self.assertTrue(refused, "the rename across the two mounts was tried and refused")
            self.assertEqual(sorted(moved), sorted(["Projects", "Studies", projects.ABOUT_FILE]))
            self.assertEqual(projects.root(), os.path.normpath(newRoot))
            self.assertTrue(os.path.isfile(os.path.join(newRoot, "Studies", "Study A", "study.json")))
            self.assertFalse(os.path.exists(os.path.join(self.root, "Projects")), "the originals are deleted after the copy")
            self.assertFalse(os.path.exists(os.path.join(self.root, "Studies")))
            self.assertIs(service.project, project)
            self.assertTrue(project.path.startswith(newPrefix))
            self.assertTrue(volume.GetStorageNode().GetFileName().startswith(newPrefix))
            self._save(service)
            self.assertTrue(os.path.isfile(project.scenePath))
            # Another failure of the move leaves no empty new folder behind
            failed = os.path.join(self.tempDir, "failed", "Slicer Studio")

            def denied(source, destination, *args, **kwargs):
                raise OSError(errno.EACCES, "Permission denied", os.fspath(source))

            with mock.patch.object(projects, "_sameVolume", return_value=True), mock.patch.object(projects.os, "rename", side_effect=denied):
                with self.assertRaises(OSError):
                    projects.moveContent(newRoot, failed)
            self.assertFalse(os.path.exists(failed), "no empty folder is left when nothing was moved")
            self.assertTrue(os.path.isdir(os.path.join(newRoot, "Projects")), "the originals stay")
        finally:
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_47_change_folder_dialog_cancels_on_escape_close_and_cancel(self):
        """Settings > Saving > Change…: Escape, closing the window or Cancel leaves the folder unchanged (BB-1). The dialog
        reused the first-start chooser, whose Escape accepts the proposal, so Escape after picking a folder in an empty
        Slicer Studio folder silently made it the new folder. Only the first-start chooser accepts on Escape.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        oldRoot = os.path.normpath(self.root)
        qt.QSettings().setValue(projects.ROOT_KEY, oldRoot)
        os.makedirs(oldRoot, exist_ok=True)
        self.assertFalse(projects.hasContent(oldRoot))

        def run(name, closer):
            pick = os.path.join(self.tempDir, "picked " + name)
            os.makedirs(pick, exist_ok=True)
            seen = []

            def tick():
                for widget in qt.QApplication.topLevelWidgets():
                    if widget.isVisible() and widget.objectName == "StudioProjectsFolderDialog":
                        seen.append(widget.windowTitle)
                        timer.stop()
                        closer(widget)
                        return

            timer = qt.QTimer()
            timer.setInterval(100)
            timer.connect("timeout()", tick)
            timer.start()
            try:
                with mock.patch.object(qt.QFileDialog, "getExistingDirectory", return_value=pick):
                    result = projects_ui.changeRootDialog(None)
            finally:
                timer.stop()
            self.assertEqual(seen, ["Change the Slicer Studio folder"], name)
            return pick, result

        try:
            escape = lambda w: qt.QApplication.sendEvent(w, qt.QKeyEvent(qt.QEvent.KeyPress, qt.Qt.Key_Escape, qt.Qt.NoModifier))
            for name, closer in (("escape", escape), ("close", lambda w: w.close()),
                                 ("cancel", lambda w: w.findChild("QPushButton", "StudioProjectsFolderCancelButton").click())):
                pick, result = run(name, closer)
                self.assertIsNone(result, name)
                self.assertEqual(projects.root(), oldRoot, name)
            # Use this folder still changes it
            pick, result = run("use", lambda w: w.continueButton.click())
            self.assertEqual(result, os.path.normpath(pick))
            self.assertEqual(projects.root(), os.path.normpath(pick))
        finally:
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
        # The first-start chooser has no Cancel and still accepts on Escape
        dialog = projects_ui.FolderChooserDialog(proposal=oldRoot)
        try:
            self.assertIsNone(dialog.cancelButton)
            dialog.reject()
            self.assertEqual(dialog.result(), qt.QDialog.Accepted)
            self.assertFalse(dialog.cancelled)
        finally:
            dialog.deleteLater()

    def test_48_project_folder_renamed_outside_keeps_one_scene_file(self):
        """A project folder renamed in the file manager (or copied and renamed) is opened, saved and keeps ONE scene file
        (BB-1). Its old-named scene was loaded as it was, every save wrote "<new folder name>.mrml" next to it, and the
        old-named file stayed behind without the later work (official 3D Slicer opened the outdated one). The scene (and
        its _Private folder) follows the folder name as an in-app rename does, and project.json gets the new name.
        """
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        oldPath, oldName = project.path, os.path.basename(project.path)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        newPath = os.path.join(os.path.dirname(oldPath), "Renamed outside")
        os.rename(oldPath, newPath)
        self.assertTrue(os.path.isfile(os.path.join(newPath, oldName + ".mrml")))

        self.assertTrue(service.openProject(newPath))
        self.assertEqual(service.project.path, newPath)
        self.assertEqual(sorted(name for name in os.listdir(newPath) if name.endswith(".mrml")), ["Renamed outside.mrml"])
        self.assertEqual(slicer.mrmlScene.GetURL(), os.path.join(newPath, "Renamed outside.mrml"))
        self.assertEqual(projects.readJson(projects.projectFile(newPath))["name"], "Renamed outside")
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesions"))
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "After the rename").AddControlPoint(1, 2, 3)
        self._save(service)
        self.assertEqual(sorted(name for name in os.listdir(newPath) if name.endswith(".mrml")), ["Renamed outside.mrml"])
        with open(os.path.join(newPath, "Renamed outside.mrml"), encoding="utf-8") as scene:
            self.assertIn("After the rename", scene.read())
        self.assertEqual([info.name for info in projects.listProjects()], ["Renamed outside"])

        # A folder with several scene files is left alone (which one is the project's is not known)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        twoPath = os.path.join(os.path.dirname(newPath), "Two scenes")
        os.rename(newPath, twoPath)
        shutil.copy(os.path.join(twoPath, "Renamed outside.mrml"), os.path.join(twoPath, "Other.mrml"))
        os.rename(os.path.join(twoPath, "Renamed outside.mrml"), os.path.join(twoPath, "First.mrml"))
        self.assertEqual(projects.adoptRenamedScene(twoPath), projects.sceneFile(twoPath))
        self.assertEqual(sorted(name for name in os.listdir(twoPath) if name.endswith(".mrml")), ["First.mrml", "Other.mrml"])

    def test_49_project_times_survive_a_time_zone_change_and_the_dst_hour(self):
        """project.json times carry a UTC marker: a project saved in Tokyo is not dated in the future when read in Los Angeles."""
        projects = self.projects
        from StudioLib import recovery

        savedZone = os.environ.get("TZ")

        def setZone(zone):
            os.environ["TZ"] = zone
            time.tzset()

        try:
            now = time.time()
            setZone("Asia/Tokyo")
            east = projects._isoTime(now)
            self.assertTrue(east.endswith("Z"), east)
            eastSession = self.autosave._isoTime(now)
            setZone("America/Los_Angeles")
            self.assertAlmostEqual(projects.parseTime(east), now, delta=1.0)
            self.assertAlmostEqual(recovery._parseTime(eastSession), now, delta=1.0)
            # The repeated hour of the DST fall-back (2026-11-01 01:30 EDT and EST) round-trips
            setZone("America/New_York")
            for epoch in (1793511000.0, 1793514600.0):  # 05:30Z (01:30 EDT) and 06:30Z (01:30 EST)
                self.assertEqual(projects.parseTime(projects._isoTime(epoch)), epoch)
                self.assertEqual(recovery._parseTime(self.autosave._isoTime(epoch)), epoch)
            # Offsets are honoured; a time without a marker (an earlier version) is local time
            self.assertEqual(projects.parseTime("2026-11-01T05:30:00Z"), 1793511000.0)
            self.assertEqual(projects.parseTime("2026-11-01T14:30:00+09:00"), 1793511000.0)
            self.assertEqual(projects.parseTime("2026-11-01T00:00:00-05:30"), 1793511000.0)
            self.assertEqual(projects.parseTime("2026-06-01T12:00:00"), time.mktime(time.strptime("2026-06-01T12:00:00", "%Y-%m-%dT%H:%M:%S")))
            self.assertIsNone(projects.parseTime("yesterday"))
            self.assertIsNone(projects.parseTime(None))
            # Library order: the project saved last comes first, whatever the zone of each save
            setZone("Asia/Tokyo")
            first = projects.createProject("Saved in east")
            second = projects.createProject("Saved in west")
            self.io.atomicWriteJson(projects.projectFile(first.path), {"name": first.name, "modified": projects._isoTime(now - 120)})
            setZone("America/Los_Angeles")
            self.io.atomicWriteJson(projects.projectFile(second.path), {"name": second.name, "modified": projects._isoTime(now - 60)})
            for project in (first, second):  # the file times of the folder and the scene must not decide
                for path in (project.path, project.scenePath):
                    if os.path.exists(path):
                        os.utime(path, (1.0, 1.0))
            infos = {info.name: info for info in projects.listProjects()}
            self.assertLess(infos["Saved in east"].modified, now - 100)
            self.assertAlmostEqual(infos["Saved in west"].modified, now - 60, delta=2.0)
        finally:
            if savedZone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = savedZone
            time.tzset()

    def test_50_library_follows_other_windows_and_a_vanished_project_is_not_a_failed_save(self):
        """Another window creates and renames projects in the folder: the Library lists them without a workspace switch
        (a folder watcher), and opening a card whose folder is gone says it was renamed or moved (no "Not saved", no Retry)
        and lists the folder again.
        """
        from StudioLib import projects_ui

        projects = self.projects
        projects.createProject("First")
        section = projects_ui.ProjectsSection()

        def names():
            return sorted(os.path.basename(str(card.property("studioProjectPath"))) for card in section.cards())

        def waitFor(predicate, seconds=10.0):
            end = time.time() + seconds
            while time.time() < end and not predicate():
                slicer.app.processEvents()
                time.sleep(0.02)
            return predicate()

        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))
        messages = []
        original = projects_ui.message
        try:
            section.refresh()
            self.assertEqual(names(), ["First"])
            # What another process does: a folder appears in Projects/ (no notification of this process)
            other = os.path.join(projects.projectsFolder(), "Other")
            os.makedirs(other)
            self.assertTrue(waitFor(lambda: names() == ["First", "Other"]), names())
            renamed = os.path.join(projects.projectsFolder(), "Other renamed")
            os.rename(other, renamed)
            self.assertTrue(waitFor(lambda: names() == ["First", "Other renamed"]), names())
            # A stale card: its folder is gone
            self.assertFalse(service.openProject(other))
            self.assertEqual([event for event, _info in announced], ["not-opened"])
            self.assertTrue(announced[0][1]["missing"])
            self.assertEqual(announced[0][1]["reason"], "Other was renamed or moved; the list is refreshed")
            # The message is a warning without the Retry of a failed save
            projects_ui.message = lambda text, **kwargs: messages.append((text, kwargs))
            projects_ui.announce("not-opened", None, **announced[0][1])
            self.assertEqual(messages[0][0], "Other was renamed or moved; the list is refreshed")
            self.assertNotIn("action", messages[0][1])
            # A folder that exists without a scene file keeps its own reason
            announced.clear()
            self.assertFalse(service.openProject(renamed))
            self.assertFalse(announced[0][1]["missing"])
            self.assertEqual(announced[0][1]["reason"], "Other renamed has no scene file")
        finally:
            projects_ui.message = original
            section.dispose()
            section.widget.deleteLater()

    def test_51_locked_project_warning_is_shown_once_while_the_scene_names_it(self):
        """A window that holds the data of a project another Slicer Studio has open (it lost the race to open it) retries
        to attach on every new scene fingerprint, but says "open in another Slicer Studio" once, not after every edit; it
        attaches silently once the other window lets go, and says it again if the project is lost again.
        """
        projects = self.projects
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))
        self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        # The scene keeps the project's files and its URL, but another window now holds the project
        service.releaseProject()
        other = projects.Project(project.path)
        self.assertTrue(other.lock())
        announced.clear()
        try:
            service.poll()
            self.assertIsNone(service.project, "the project is held by the other window")
            self.assertEqual(announced, ["locked"])
            for edit in range(4):
                markups = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", f"F{edit}")
                markups.AddControlPoint([edit, 0, 0])
                service.poll()
                self.assertIsNone(service.project)
            self.assertEqual(announced, ["locked"], "no new warning after each edit")
            # The other window lets go: the retry attaches, quietly except for the "opened" message
            other.unlock()
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "After")
            service.poll()
            self.assertIsNotNone(service.project)
            self.assertEqual(announced, ["locked", "opened"])
            # Lost again later: the warning is news again
            service.releaseProject()
            self.assertTrue(other.lock())
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Again")
            service.poll()
            self.assertIsNone(service.project)
            self.assertEqual(announced, ["locked", "opened", "locked"])
        finally:
            other.unlock()

    def _answerDialog(self, objectName, buttonName, seen, fill=None):
        """Poll for the visible dialog ``objectName``, record how it looks in ``seen`` and click its button ``buttonName``."""
        timer = qt.QTimer()
        timer.setInterval(50)

        def poll():
            for widget in qt.QApplication.topLevelWidgets():
                if widget.isVisible() and widget.objectName == objectName:
                    seen.update(
                        className=widget.className(),
                        frameless=bool(int(widget.windowFlags()) & int(qt.Qt.FramelessWindowHint)),
                        chrome=widget.property("studioDialogChrome"),
                        iconTile=widget.findChild("QLabel", objectName + "Icon") is not None,
                        buttons=[child.text for child in widget.findChildren("QPushButton")])
                    if fill is not None:
                        widget.findChild("QLineEdit", "StudioProjectsRenameEdit").setText(fill)
                    widget.findChild("QPushButton", buttonName).click()
                    timer.stop()
                    return

        timer.connect("timeout()", poll)
        timer.start()
        return timer

    def test_52_rename_and_trash_dialogs_have_the_dialog_chrome(self):
        """Rename project and Move to Trash use the Studio dialog chrome (VISUAL 4.7: frameless card, icon tile, one primary
        button), not the stock QInputDialog and QMessageBox with their legacy OK/Cancel and question-mark icons.
        """
        from StudioLib import projects_ui

        projects = self.projects
        projects.ensureRoot()
        first = projects.createProject("First project")
        other = projects.createProject("Second project")
        # Rename: the chrome, the new name is used; Cancel changes nothing
        seen = {}
        timer = self._answerDialog("StudioProjectsRenameDialog", "StudioProjectsRenameDialogButton0", seen, fill="Renamed project")
        newPath = projects_ui.renameDialog(first.path)
        timer.stop()
        self.assertEqual(seen.get("className"), "RenameDialog", seen)
        self.assertTrue(seen["frameless"], seen)
        self.assertTrue(seen["chrome"], seen)
        self.assertTrue(seen["iconTile"], seen)
        self.assertEqual(sorted(seen["buttons"]), ["Cancel", "Rename"])
        self.assertEqual(os.path.basename(newPath), "Renamed project")
        self.assertTrue(os.path.isdir(newPath))
        seen = {}
        timer = self._answerDialog("StudioProjectsRenameDialog", "StudioProjectsRenameDialogButton1", seen, fill="Never used")
        self.assertIsNone(projects_ui.renameDialog(newPath))
        timer.stop()
        self.assertTrue(os.path.isdir(newPath))
        # Move to Trash: the chrome; Cancel keeps the project, Move to Trash removes it
        seen = {}
        timer = self._answerDialog("StudioProjectsQuestion", "StudioProjectsQuestionButton1", seen)
        self.assertFalse(projects_ui.moveToTrash(other.path))
        timer.stop()
        self.assertEqual(seen.get("className"), "_ChromeDialog", seen)
        self.assertTrue(seen["frameless"], seen)
        self.assertTrue(seen["chrome"], seen)
        self.assertTrue(seen["iconTile"], seen)
        self.assertEqual(sorted(seen["buttons"]), ["Cancel", "Move to Trash"])
        self.assertTrue(os.path.isdir(other.path))
        # The answer of a three-button question is the index of the clicked button
        timer = self._answerDialog("StudioProjectsQuestion", "StudioProjectsQuestionButton2", {})
        self.assertEqual(projects_ui._question(None, "Title", "Text", ["Move", "Keep", "Cancel"]), 2)
        timer.stop()
        timer = self._answerDialog("StudioProjectsQuestion", "StudioProjectsQuestionButton0", {})
        self.assertEqual(projects_ui._question(None, "Title", "Text", ["Move", "Keep", "Cancel"]), 0)
        timer.stop()

    def test_52b_trash_of_the_open_project_is_refused_before_asking_in_the_dialog_chrome(self):
        """Move to Trash on the open project said "Move the project X to the Trash?" and, after the user confirmed, refused in a
        legacy "Slicer error" box (BB-1): it is refused up front, with no question, in the Studio dialog chrome (one OK button);
        the other refusals (rename of a taken name, a folder without a scene) use the same dialog.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        projects.ensureRoot()
        project = projects.createProject("Case 7")
        previous = projects.currentProject()
        projects.setCurrentProject(project)
        try:
            def fail(*args, **kwargs):
                raise AssertionError("asked a question or showed the legacy error box")

            seen = {}
            timer = self._answerDialog("StudioProjectsNotice", "StudioProjectsNoticeButton0", seen)
            with mock.patch.object(projects_ui, "_question", fail), mock.patch.object(slicer.util, "errorDisplay", fail):
                self.assertFalse(projects_ui.moveToTrash(project.path))
            timer.stop()
            self.assertEqual(seen.get("className"), "_ChromeDialog", seen)
            self.assertTrue(seen["frameless"], seen)
            self.assertTrue(seen["chrome"], seen)
            self.assertTrue(seen["iconTile"], seen)
            self.assertEqual(seen["buttons"], ["OK"])
            self.assertTrue(os.path.isdir(project.path))
            # A name that is taken: the refusal of Rename is the same dialog
            taken = projects.createProject("Taken")
            other = projects.createProject("Other")
            seen = {}
            notices = []
            with mock.patch.object(projects_ui, "_notice", lambda parent, title, text: notices.append((title, text))), \
                    mock.patch.object(slicer.util, "errorDisplay", fail):
                timer = self._answerDialog("StudioProjectsRenameDialog", "StudioProjectsRenameDialogButton0", seen, fill="Taken")
                self.assertIsNone(projects_ui.renameDialog(other.path))
                timer.stop()
            self.assertEqual(len(notices), 1, notices)
            self.assertTrue(os.path.isdir(other.path))
            self.assertTrue(os.path.isdir(taken.path))
        finally:
            projects.setCurrentProject(previous)

    def test_52c_rename_to_only_forbidden_characters_is_refused_not_renamed_untitled(self):
        """Typing "//:*?" in Rename renamed the project "Untitled project" (BB-1: the fallback name of a new project), a name the
        user did not type: it is refused with a short reason and the project keeps its name; mixed text is still cleaned up.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        projects.ensureRoot()
        closed = projects.createProject("Case 8")
        notices = []
        for typed in ("//:*?", "...", " . / . "):
            del notices[:]
            with mock.patch.object(projects_ui, "_notice", lambda _parent, title, text: notices.append((title, text))):
                timer = self._answerDialog("StudioProjectsRenameDialog", "StudioProjectsRenameDialogButton0", {}, fill=typed)
                self.assertIsNone(projects_ui.renameDialog(closed.path), typed)
                timer.stop()
            self.assertEqual(len(notices), 1, (typed, notices))
            self.assertTrue(os.path.isdir(closed.path), typed)
            self.assertFalse(os.path.exists(os.path.join(projects.projectsFolder(), projects.UNTITLED_NAME)), typed)
        # Mixed text keeps its letters
        timer = self._answerDialog("StudioProjectsRenameDialog", "StudioProjectsRenameDialogButton0", {}, fill="a/b")
        newPath = projects_ui.renameDialog(closed.path)
        timer.stop()
        self.assertEqual(os.path.basename(newPath), "a b")

    def test_53b_moved_study_with_a_taken_name_is_named_after_its_new_folder(self):
        """A study that Change folder > Move lands as "<name> 2" is named "<name> 2" in its study.json too (BB-1): the Library
        titles a study card by that name, so the two "Stroke" studies were identical cards. A name the user chose stays; the
        recent-studies entry follows the new name.
        """
        from StudioLib import autosave, study_ui

        projects = self.projects
        oldRoot = os.path.join(self.tempDir, "rm-old", "Slicer Studio")
        newRoot = os.path.join(self.tempDir, "rm-new", "Slicer Studio")

        def makeStudy(root, folder, name, mark):
            path = os.path.join(root, projects.STUDIES_DIR, folder)
            os.makedirs(path)
            with open(os.path.join(path, "study.json"), "w", encoding="utf-8") as f:
                json.dump({"format": "slicer-studio-study", "version": 1, "name": name, "created": "2026-01-01"}, f, indent=2)
                f.write("\n")
            with open(os.path.join(path, "whose.txt"), "w") as f:
                f.write(mark)
            return path

        oldStroke = makeStudy(oldRoot, "Stroke", "Stroke", "old")
        makeStudy(oldRoot, "Chosen", "My chosen name", "old")
        makeStudy(oldRoot, "Fresh", "Fresh", "old")
        newStroke = makeStudy(newRoot, "Stroke", "Stroke", "new")
        makeStudy(newRoot, "Chosen", "My chosen name", "new")
        settings = qt.QSettings()
        previousRecent = settings.value(study_ui.RECENT_STUDIES_KEY)
        settings.setValue(study_ui.RECENT_STUDIES_KEY, json.dumps([{"path": oldStroke, "name": "Stroke"}]))
        try:
            renamed = {}
            projects.moveContent(oldRoot, newRoot, renamed=renamed)
            studies = os.path.join(newRoot, projects.STUDIES_DIR)
            self.assertEqual(projects.readJson(os.path.join(studies, "Stroke 2", "study.json"))["name"], "Stroke 2")
            self.assertEqual(projects.readJson(os.path.join(studies, "Stroke 2", "study.json"))["created"], "2026-01-01")
            self.assertEqual(open(os.path.join(studies, "Stroke 2", "whose.txt")).read(), "old")
            self.assertEqual(projects.readJson(os.path.join(newStroke, "study.json"))["name"], "Stroke", "the study that kept the name")
            self.assertEqual(open(os.path.join(newStroke, "whose.txt")).read(), "new")
            self.assertEqual(projects.readJson(os.path.join(studies, "Chosen 2", "study.json"))["name"], "My chosen name", "a chosen name stays")
            self.assertEqual(projects.readJson(os.path.join(studies, "Fresh", "study.json"))["name"], "Fresh", "not renamed: untouched")
            from StudioLib import projects_ui

            names = sorted(projects_ui.studySummary(path)["name"] for path in projects.listStudies(newRoot) if "Stroke" in path)
            self.assertEqual(names, ["Stroke", "Stroke 2"], "two different cards in the Library")
            autosave._updateRecentStudies(oldRoot, newRoot, renamed)
            recent = json.loads(settings.value(study_ui.RECENT_STUDIES_KEY))
            self.assertEqual(os.path.normpath(recent[0]["path"]), os.path.join(studies, "Stroke 2"))
            self.assertEqual(recent[0]["name"], "Stroke 2")
        finally:
            if previousRecent is None:
                settings.remove(study_ui.RECENT_STUDIES_KEY)
            else:
                settings.setValue(study_ui.RECENT_STUDIES_KEY, previousRecent)

    def test_53_root_move_into_a_folder_with_a_project_of_the_same_name_follows_its_own_project(self):
        """Change folder > Move into a Slicer Studio folder that already has a project with the open project's name (BB-1):
        the moved project lands as "<name> 2" and the open project follows it there, scene file named after the folder.
        The other project keeps its files. It used to map the open project to the other project's folder: the next save
        wrote the lesion and a scene into that project (next to its own, other scan) and rewrote its project.json, while
        the moved project no longer received the edits. A recent study that moved as "<name> 2" follows it too.
        """
        from StudioLib import study_ui

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        segmentation, segmentId, array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        name = project.name
        os.makedirs(os.path.join(self.root, projects.STUDIES_DIR, "Study A"))
        oldStudy = os.path.join(self.root, projects.STUDIES_DIR, "Study A")
        # The other Slicer Studio folder: a project and a study with the same names
        newRoot = os.path.join(self.tempDir, "other", "Slicer Studio")
        theirs = projects.createProject(name, rootPath=newRoot)
        self.assertEqual(theirs.name, name)
        with open(os.path.join(theirs.path, name + projects.SCENE_EXTENSION), "w") as f:
            f.write("<MRML></MRML>\n")
        os.makedirs(os.path.join(newRoot, projects.STUDIES_DIR, "Study A"))
        theirsBefore = {path: _md5File(os.path.join(theirs.path, path)) for path in _relativeFiles(theirs.path)}
        settings = qt.QSettings()
        previousRecent = settings.value(study_ui.RECENT_STUDIES_KEY)
        settings.setValue(study_ui.RECENT_STUDIES_KEY, json.dumps([{"path": oldStudy, "name": "Study A"}]))
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, self.root)
        try:
            service.moveRoot(newRoot, moveContent=True)
            ours = os.path.join(os.path.normpath(newRoot), projects.PROJECTS_DIR, name + " 2")
            self.assertTrue(os.path.isdir(ours))
            self.assertIs(service.project, project)
            self.assertEqual(os.path.normpath(service.project.path), ours, "the open project follows its own moved folder")
            self.assertEqual(service.project.name, name + " 2")
            self.assertTrue(os.path.isfile(os.path.join(ours, name + " 2" + projects.SCENE_EXTENSION)), "the scene follows the folder name")
            self.assertTrue(volume.GetStorageNode().GetFileName().startswith(ours + os.sep))
            array[12:16, 14:20, 16:22] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            self._save(service)
            scenes = sorted(entry for entry in os.listdir(ours) if entry.endswith(projects.SCENE_EXTENSION))
            self.assertEqual(scenes, [name + " 2" + projects.SCENE_EXTENSION], "one scene file")
            self.assertEqual(projects.readJson(projects.projectFile(ours)).get("name"), name + " 2")
            theirsAfter = {path: _md5File(os.path.join(theirs.path, path)) for path in _relativeFiles(theirs.path)
                           if not path.startswith(".studio/")}
            self.assertEqual(theirsAfter, {path: digest for path, digest in theirsBefore.items() if not path.startswith(".studio/")},
                             "the other project is not touched")
            self.assertEqual(projects.readJson(projects.projectFile(theirs.path)).get("name"), name)
            recent = json.loads(settings.value(study_ui.RECENT_STUDIES_KEY))
            self.assertEqual(os.path.normpath(recent[0]["path"]), os.path.join(os.path.normpath(newRoot), projects.STUDIES_DIR, "Study A 2"))
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            if previousRecent is None:
                settings.remove(study_ui.RECENT_STUDIES_KEY)
            else:
                settings.setValue(study_ui.RECENT_STUDIES_KEY, previousRecent)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root


    def test_33b_save_a_copy_of_an_unedited_project_is_not_an_edit(self):
        """Exporting a copy changes and restores the storage nodes' file names (their MTimes change), but it is not an
        edit: a project that was up to date stays up to date, the next save does not rewrite the scene file and "Last
        edited" stays. An edit made after the copy still counts.
        """
        from StudioLib import projects_ui

        service = self._service()
        volume, _scanPath = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        self.assertFalse(service.projectNeedsSave(includeScene=True))
        sceneBefore = os.stat(project.scenePath).st_mtime_ns
        copyPath = os.path.join(self.tempDir, "Unedited copy.mrb")
        running = self.autosave._service
        self.autosave._service = service
        try:
            self.assertEqual(projects_ui.saveCopyAs(copyPath), copyPath)
        finally:
            self.autosave._service = running
        self.assertFalse(service.projectNeedsSave(includeScene=True), "a copy is not an edit")
        self._save(service)
        self.assertEqual(os.stat(project.scenePath).st_mtime_ns, sceneBefore, "the scene file is not rewritten")
        volume.SetName("Renamed after the copy")
        self.assertTrue(service.projectNeedsSave(), "a real edit after the copy is still saved")

    def test_54_scene_saved_inside_a_project_folder_opens_as_a_copy(self):
        """A scene that official 3D Slicer saved into a subfolder of a project (Projects/<name>/Exports/variant/) is not the
        project (BB-1): opening it (Open…/drop with or without a project open, or Load Scene in Classic followed by Studio)
        opens it as its own project that starts as a copy. It used to continue as the enclosing project, so the next save
        rewrote "<name>.mrml" with the other scene and the project's scan and lesion were no longer in it.
        """
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume("MRHead", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume, "Lesion")
        project = self._projectFor(service)
        self._save(service)
        projectPath, scenePath = project.path, project.scenePath
        with open(scenePath, encoding="utf-8") as f:
            sceneText = f.read()
        self.assertIn("Lesion", sceneText)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        # Another scene, with its own scan, saved by 3D Slicer inside the project folder
        variantDir = os.path.join(projectPath, "Exports", "variant")
        os.makedirs(variantDir)
        k, j, i = np.indices((10, 11, 12))
        variantScan = slicer.util.addVolumeFromArray(((i + j + k) % 50).astype(np.int16), name="Variant scan")
        self.assertTrue(slicer.util.saveNode(variantScan, os.path.join(variantDir, "Variant scan.nrrd")))
        variantPath = os.path.join(variantDir, "variant.mrml")
        service.dispose()
        self.services.remove(service)
        self.assertTrue(slicer.util.saveScene(variantPath))
        slicer.mrmlScene.Clear(0)
        self.assertIsNone(projects.projectOfSceneFile(variantPath))
        self.assertEqual(os.path.normpath(projects.projectOfSceneFile(scenePath)), os.path.normpath(projectPath))

        def assertProjectKept():
            self.io.backgroundWriter().drain(30.0)
            with open(scenePath, encoding="utf-8") as f:
                self.assertEqual(f.read(), sceneText, "the project's own scene file is not changed")

        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))
        # No project open: Open… / drop loads the scene and it opens as a copy
        service.setPendingSceneSource(variantPath)
        slicer.util.loadScene(variantPath)
        self.assertIsNotNone(service.project, "the scene opens as its own project")
        self.assertNotEqual(os.path.normpath(service.project.path), os.path.normpath(projectPath))
        self.assertEqual(service.project.name, "variant")
        self.assertIn("copy", announced)
        self.assertNotIn("opened", announced)
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Edit").AddControlPoint(1, 2, 3)
        self._save(service)
        assertProjectKept()
        self.assertIn("Images/Variant scan.nrrd", self._projectFiles(service.project), "the copy holds the scene's own scan")
        # Another project open: Open… replaces it with the scene, which again opens as a copy
        copyPath = service.project.path
        del announced[:]
        # The same file again while its copy is open keeps that copy ("<name> is already open", no "variant 2")
        self.assertTrue(service.openSceneFile(variantPath))
        self.assertEqual(os.path.normpath(service.project.path), os.path.normpath(copyPath))
        self.assertEqual(announced, [])
        # A changed file is a new scene: it opens as another copy
        stat = os.stat(variantPath)
        os.utime(variantPath, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.assertTrue(service.openSceneFile(variantPath))
        self.assertIsNotNone(service.project)
        self.assertNotIn(os.path.normpath(service.project.path), (os.path.normpath(projectPath), os.path.normpath(copyPath)))
        self.assertIn("copy", announced)
        self._save(service)
        assertProjectKept()
        # Loaded in Classic (official Load Scene), then Studio is shown: a copy as well, never the enclosing project
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        service.dispose()
        self.services.remove(service)
        classic = self._service(studio=False)
        slicer.util.loadScene(variantPath)
        classic.poll()
        self.assertIsNone(classic.project)
        classic.studioActiveOverride = True
        classic._lastProjectCheck = None
        classic.poll()
        self.assertIsNotNone(classic.project)
        self.assertNotEqual(os.path.normpath(classic.project.path), os.path.normpath(projectPath))
        self._save(classic)
        assertProjectKept()
        # The project's own scene still continues in place
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(classic.openProject(projectPath))
        self.assertEqual(os.path.normpath(classic.project.path), os.path.normpath(projectPath))
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesion"))


    def test_54b_same_outside_scene_opened_again_keeps_its_copy(self):
        """The same scene file from outside Projects opened twice in a row (a double drop, Open… again) keeps the copy that
        is open: no second project "<name> 2" (BB-1). A file that changed since is a new scene and opens as another copy.
        """
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))
        slicer.util.addVolumeFromArray(np.ones((6, 7, 8), np.int16), name="Outside scan")
        outside = os.path.join(self.tempDir, "outside_scene.mrb")
        self.assertTrue(slicer.util.saveScene(outside))
        slicer.mrmlScene.Clear(0)
        service.poll()
        self.assertTrue(service.openSceneFile(outside))
        first = service.project
        self.assertIsNotNone(first)
        self.assertEqual(announced.count("copy"), 1)
        self.io.backgroundWriter().drain(30.0)
        self.assertTrue(service.isOpenCopyOf(outside))
        self.assertTrue(service.openSceneFile(outside))
        self.assertEqual(os.path.normpath(service.project.path), os.path.normpath(first.path))
        self.assertEqual(announced.count("copy"), 1, "no second copy")
        self.assertEqual(len(self.projects.listProjects(self.root)), 1)


    def test_55_root_move_is_refused_while_another_studio_has_a_study_open(self):
        """Moving the Slicer Studio folder while another running Slicer Studio has one of its studies open (BB-1) is
        refused before anything is moved, with a message that names the study, as it is for a project open there. The
        folder used to move Studies/ from under the other window: its tracing failed to save ("no NIfTI file to write the
        lesion on"), the moved study kept the old lesion, and its journal and heartbeat re-created a half study in the
        old folder. An open study is recognised by the live heartbeat in its ``.studio/session.json``.
        """
        import socket
        import subprocess
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        projects.ensureRoot(self.root)
        oldRoot = projects.root()
        other = subprocess.Popen(["sleep", "600"])  # the other Slicer Studio (a live process of this host)

        def makeStudy(relative, pid, live=True):
            path = os.path.join(projects.studiesFolder(oldRoot), *relative.split("/"))
            os.makedirs(os.path.join(path, ".studio"), exist_ok=True)
            with open(os.path.join(path, "study.json"), "w", encoding="utf-8") as f:
                f.write("{}\n")
            record = {"format": "slicer-studio-session", "pid": pid, "host": socket.gethostname(), "app": "Slicer Studio",
                      "heartbeat_epoch": time.time() if live else 0}
            with open(os.path.join(path, ".studio", "session.json"), "w", encoding="utf-8") as f:
                json.dump(record, f)
            return path

        newRoot = os.path.join(self.tempDir, "moved", "Slicer Studio")
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, self.root)
        running = self.autosave._service
        self.autosave._service = service
        try:
            makeStudy("RM", other.pid)
            makeStudy("Lab/Nested", other.pid)
            makeStudy("Closed", other.pid, live=False)  # closed there: its heartbeat is 0
            makeStudy("Here", os.getpid())  # this window's own heartbeat (a study open here is refused on its own)
            gone = subprocess.Popen(["true"])
            gone.wait()
            makeStudy("Crashed", gone.pid)  # a fresh heartbeat of a process that no longer exists

            def unchanged():
                self.assertFalse(os.path.exists(newRoot), "nothing is made in the new folder")
                self.assertTrue(os.path.isfile(os.path.join(oldRoot, "Studies", "RM", "study.json")), "the study stays")
                self.assertEqual(projects.root(), os.path.normpath(self.root), "the setting is unchanged")

            with self.assertRaises(projects.OpenElsewhereError) as context:
                service.moveRoot(newRoot, moveContent=True)
            self.assertIn("The studies Lab/Nested, RM are open in another Slicer Studio", str(context.exception))
            unchanged()
            self.assertEqual(projects.studiesOpenElsewhere(oldRoot), ["Lab/Nested", "RM"])
            # Only one left open there: named alone
            makeStudy("Lab/Nested", other.pid, live=False)
            with self.assertRaises(projects.OpenElsewhereError) as context:
                service.moveRoot(newRoot, moveContent=True)
            self.assertIn("The study RM is open in another Slicer Studio", str(context.exception))
            unchanged()
            # The Change… dialog says it before asking anything
            shown = []

            class FakeChooser:
                path = newRoot
                continueButton = mock.Mock()
                setWindowTitle = mock.Mock()
                choose = mock.Mock()
                exec = mock.Mock()
                exec_ = mock.Mock()
                deleteLater = mock.Mock()

                def __init__(self, *args, **kwargs):
                    pass

            def fail(*args, **kwargs):
                raise AssertionError("the question is not asked")

            with mock.patch.object(projects_ui, "FolderChooserDialog", FakeChooser), \
                    mock.patch.object(projects_ui, "_question", fail), \
                    mock.patch.object(slicer.util, "infoDisplay", lambda text, *a, **k: shown.append(text)), \
                    mock.patch.object(slicer.util, "errorDisplay", lambda text, *a, **k: shown.append(text)):
                self.assertIsNone(projects_ui.changeRootDialog(None))
            self.assertEqual(len(shown), 1, shown)
            self.assertIn("The study RM is open in another Slicer Studio", shown[0])
            unchanged()
            # Once the other window has closed it (heartbeat 0), the folder moves with the studies
            makeStudy("RM", other.pid, live=False)
            self.assertEqual(projects.studiesOpenElsewhere(oldRoot), [])
            moved = service.moveRoot(newRoot, moveContent=True)
            self.assertIn("Studies", moved)
            self.assertEqual(projects.root(), os.path.normpath(newRoot))
            self.assertTrue(os.path.isfile(os.path.join(newRoot, "Studies", "RM", "study.json")))
        finally:
            other.kill()
            other.wait()
            self.autosave._service = running
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root


    def test_56_lock_left_by_an_earlier_process_with_the_same_pid_is_stale(self):
        """A lock left by a Slicer Studio that was stopped or crashed, whose PID is the PID of this process, is stale.

        Each start of the web container is a new PID namespace with the machine ID of the image, so the next Slicer Studio
        often gets the PID of the one that held the lock. QLockFile then found a running process with that PID and name (the
        new one itself) and the project stayed "open in another Slicer Studio" at every start: no crash offer, and open,
        rename and Move to Trash were refused until .studio/lock was deleted by hand.
        """
        projects = self.projects
        service = self._service()
        _volume, _path = self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        path = project.path
        lockPath = os.path.join(path, ".studio", "lock")
        with open(lockPath, encoding="utf-8") as f:
            ownLock = f.read()
        self.assertEqual(ownLock.split("\n")[0], str(os.getpid()), "QLockFile writes the PID of this process")
        # The process is gone (killed, no clean quit): its lock and project.json "open" stay; the next one has its PID
        project.unlock()
        service.project = None
        service.projectWriter = None
        projects.setCurrentProject(None)
        self.io.backgroundWriter().drain(10.0)
        data = projects.readJson(projects.projectFile(path))
        data["state"] = "open"
        self.io.atomicWriteJson(projects.projectFile(path), data)
        with open(lockPath, "w", encoding="utf-8") as f:
            f.write(ownLock)
        self.assertFalse(projects.isLockedByOther(path), "the process that held the lock is gone")
        offered = []
        service.setProjectPresenter(offered.append)
        self.assertEqual([info.name for info in service.scanProjectsAtStartup()], [project.name], "the crash offer lists it")
        # The same lock while this process holds the project (another window of the same process stands for it): locked
        with open(lockPath, "w", encoding="utf-8") as f:
            f.write(ownLock)
        holder = projects.Project(path)
        self.assertTrue(holder.lock())
        try:
            self.assertTrue(projects.isLockedByOther(path))
            self.assertFalse(projects.Project(path).lock())
        finally:
            holder.unlock()
        self.assertFalse(os.path.exists(lockPath))
        # The same PID written on another computer (a shared folder): QLockFile cannot check it, so it stays locked
        lines = ownLock.split("\n")
        while len(lines) < 5:
            lines.append("")
        lines[2], lines[3] = "another-computer", "0123456789abcdef0123456789abcdef"
        with open(lockPath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self.assertTrue(projects.isLockedByOther(path))
        self.assertFalse(projects.Project(path).lock())
        self.assertTrue(os.path.exists(lockPath), "the lock of another computer is kept")
        # Then the project opens at the next start: the stale lock of the same PID is replaced by the lock of this one
        with open(lockPath, "w", encoding="utf-8") as f:
            f.write(ownLock)
        self.assertTrue(service.openProject(path))
        self.assertTrue(service.project.isLocked())

    def test_57_failed_or_repeated_duplicate_leaves_no_temporary_project(self):
        """Library card > Duplicate that fails (a full disk) deletes its partial copy and says why in plain words; two
        Duplicates in a row make "<name> copy" and "<name> copy 2" (BB-1). A failed copy used to stay in Projects/ as
        "<name> copy.studio-copy" (listed in the Library as a project), the toast showed the raw list of failed files with
        their absolute paths, and every later Duplicate of the project failed with "File exists". A second Duplicate before
        the first one finished took the same name and failed with "Directory not empty", leaving the same folder behind.
        """
        import errno
        import shutil as shutilModule
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        project = projects.createProject("MRHead")
        os.makedirs(os.path.join(project.path, "Images"))
        for index in range(3):
            with open(os.path.join(project.path, "Images", f"scan {index}.nrrd"), "wb") as f:
                f.write(b"\1" * 4096)
        with open(os.path.join(project.path, "MRHead.mrml"), "w", encoding="utf-8") as f:
            f.write("<MRML></MRML>\n")
        path = project.path
        parent = os.path.dirname(path)

        def entries():
            return sorted(os.listdir(parent))

        realCopyfile = shutilModule.copyfile

        def fullDisk(source, destination, *args, **kwargs):
            if os.path.basename(os.fspath(source)) == "scan 1.nrrd":
                with open(destination, "wb") as f:
                    f.write(b"\0" * 16)  # what fitted on the disk
                raise OSError(errno.ENOSPC, "No space left on device", destination)
            return realCopyfile(source, destination, *args, **kwargs)

        toasts = []
        with mock.patch.object(projects_ui, "message", side_effect=lambda text, **kwargs: toasts.append(str(text))):
            # A full disk: the partial copy is deleted, and the message says why without any path
            with mock.patch.object(shutilModule, "copyfile", side_effect=fullDisk):
                projects_ui.duplicate(path)
                self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertEqual(entries(), ["MRHead"], "nothing of the failed copy stays")
            self.assertEqual([info.name for info in projects.listProjects()], ["MRHead"])
            self.assertEqual(len(toasts), 1, toasts)
            self.assertIn("not enough disk space", toasts[0])
            self.assertNotIn(self.tempDir, toasts[0])
            self.assertNotIn("Errno", toasts[0])
            # Once space is free, Duplicate works again (no "File exists")
            toasts.clear()
            self.assertEqual(os.path.basename(projects_ui.duplicate(path)), "MRHead copy")
            self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertEqual(toasts, ["Duplicated as MRHead copy"])
            expected = sorted(name.replace("MRHead.mrml", "MRHead copy.mrml") for name in _relativeFiles(path) if name != ".studio/lock")
            self.assertEqual(_relativeFiles(os.path.join(parent, "MRHead copy")), expected)
            # Two Duplicates in a row, the second before the first one finished: two copies with their own names
            toasts.clear()
            first = projects_ui.duplicate(path)
            second = projects_ui.duplicate(path)
            self.assertEqual([os.path.basename(first), os.path.basename(second)], ["MRHead copy 2", "MRHead copy 3"])
            self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertEqual(sorted(toasts), ["Duplicated as MRHead copy 2", "Duplicated as MRHead copy 3"])
            self.assertEqual(entries(), ["MRHead", "MRHead copy", "MRHead copy 2", "MRHead copy 3"])
            for copyPath in (first, second):
                self.assertTrue(os.path.isfile(os.path.join(copyPath, os.path.basename(copyPath) + ".mrml")))
                self.assertEqual(projects.readJson(projects.projectFile(copyPath))["name"], os.path.basename(copyPath))
            # A partial copy left by an earlier version (or a crash) is not a project, and does not block the next copy
            leftover = os.path.join(parent, "MRHead copy 4.studio-copy")
            os.makedirs(os.path.join(leftover, "Images"))
            self.assertNotIn("MRHead copy 4.studio-copy", [info.name for info in projects.listProjects()])
            toasts.clear()
            fourth = projects_ui.duplicate(path)
            self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertEqual(toasts, ["Duplicated as MRHead copy 4"])
            self.assertEqual(os.path.basename(fourth), "MRHead copy 4")
            self.assertFalse(os.path.exists(leftover))
        self.assertFalse([name for name in entries() if projects.COPY_MARKER in name])

    def test_57b_startup_deletes_the_partial_copy_of_an_interrupted_duplicate(self):
        """A Duplicate that was killed midway (a crash, kill -9, a power loss) left "<name> copy.studio-copy" of up to
        hundreds of MB in Projects/: hidden from the Library, never deleted, moved along with every later move of the
        folder, and only replaced if the same project was duplicated again (BB-1). The startup deletes it, and keeps the
        copy of a Duplicate that is running in this process.
        """
        projects = self.projects
        service = self._service()
        project = projects.createProject("Big")
        parent = os.path.dirname(project.path)
        leftover = os.path.join(parent, "Big copy.studio-copy")
        os.makedirs(os.path.join(leftover, "Images"))
        with open(os.path.join(leftover, "Images", "scan.nrrd"), "wb") as f:
            f.write(b"\1" * 4096)
        # A Duplicate of this process that is running: its copy is kept
        running = projects._reserveCopyName(parent, "Big running")
        runningCopy = os.path.join(parent, running + projects.COPY_MARKER)
        os.makedirs(runningCopy)
        try:
            self.assertEqual(projects.removeCopyLeftovers(), ["Big copy.studio-copy"])
            self.assertFalse(os.path.exists(leftover))
            self.assertTrue(os.path.isdir(runningCopy))
            os.makedirs(leftover)
            service.removeInterruptedCopies()
            self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertFalse(os.path.exists(leftover), "the startup deletes the copy of an interrupted Duplicate")
            self.assertTrue(os.path.isdir(runningCopy))
        finally:
            projects._releaseCopyName(parent, running)
        self.assertEqual(sorted(os.listdir(parent)), ["Big", "Big running.studio-copy"])
        self.assertEqual([info.name for info in projects.listProjects()], ["Big"])
        self.assertEqual(projects.removeCopyLeftovers(), ["Big running.studio-copy"], "no longer running: a leftover")
        self.assertEqual(os.listdir(parent), ["Big"])

    def test_57c_startup_of_another_slicer_studio_keeps_a_running_duplicate(self):
        """A second Slicer Studio started on the same folder while a Duplicate copied a big project deleted the copy as the
        leftover of an interrupted Duplicate (its startup only knew the Duplicates of its own process). The copy then
        went on into the deleted folder, and the Duplicate said "Duplicated as <name> copy" for a copy without its scene
        file (the Library listed it; opening it said "has no scene file"), or failed and left a broken project (BB-1).
        A copy that a running Duplicate is making is locked, so the startup of another Slicer Studio keeps it, and a copy
        only gets its name when it is complete.
        """
        import shutil as shutilModule
        import threading
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        project = projects.createProject("Patient")
        os.makedirs(os.path.join(project.path, "Images"))
        os.makedirs(os.path.join(project.path, "Segmentations"))
        for folder, name in (("Images", "patient.nii.gz"), ("Segmentations", "lesion.seg.nrrd")):
            with open(os.path.join(project.path, folder, name), "wb") as f:
                f.write(b"\1" * 4096)
        with open(os.path.join(project.path, "Patient.mrml"), "w", encoding="utf-8") as f:
            f.write("<MRML></MRML>\n")
        path = project.path
        parent = os.path.dirname(path)

        def entries():
            return sorted(os.listdir(parent))

        # The copy waits after its first file (a big project, a slow disk) until the other Slicer Studio has started
        realCopyfile = shutilModule.copyfile
        copying, release = threading.Event(), threading.Event()

        def slowCopy(source, destination, *args, **kwargs):
            result = realCopyfile(source, destination, *args, **kwargs)
            if projects.COPY_MARKER in os.fspath(destination) and not copying.is_set():
                copying.set()
                release.wait(30.0)
            return result

        toasts = []
        with mock.patch.object(projects_ui, "message", side_effect=lambda text, **kwargs: toasts.append(str(text))):
            with mock.patch.object(shutilModule, "copyfile", side_effect=slowCopy):
                copyPath = projects_ui.duplicate(path)
                try:
                    self.assertTrue(copying.wait(30.0), "the copy started")
                    temporary = copyPath + projects.COPY_MARKER
                    self.assertTrue(os.path.isdir(temporary))
                    # The startup clean-up of another Slicer Studio: the Duplicates of this process are not its own
                    with mock.patch.object(projects, "_pendingCopies", set()):
                        self.assertEqual(projects.removeCopyLeftovers(), [], "a running Duplicate is not a leftover")
                    self.assertTrue(os.path.isdir(temporary))
                finally:
                    release.set()
                self.assertTrue(self.io.backgroundWriter().drain(30.0))
        self.assertEqual(toasts, ["Duplicated as Patient copy"])
        expected = sorted(name.replace("Patient.mrml", "Patient copy.mrml") for name in _relativeFiles(path) if name != ".studio/lock")
        self.assertEqual(_relativeFiles(copyPath), expected, "the copy is complete")
        self.assertEqual(entries(), ["Patient", "Patient copy"], "no partial copy and no lock file stay")

        # A copy that another Slicer Studio is making under the name that this one would use: kept, and this Duplicate
        # copies into another folder
        held = os.path.join(parent, "Patient copy 2" + projects.COPY_MARKER)
        heldLock = projects._copyLockPath(held)
        descriptor, busy = projects._lockCopy(heldLock)
        self.assertFalse(busy)
        os.makedirs(os.path.join(held, "Images"))
        try:
            toasts.clear()
            with mock.patch.object(projects_ui, "message", side_effect=lambda text, **kwargs: toasts.append(str(text))):
                second = projects_ui.duplicate(path)
                self.assertTrue(self.io.backgroundWriter().drain(30.0))
            self.assertEqual(toasts, ["Duplicated as Patient copy 2"])
            self.assertTrue(os.path.isfile(os.path.join(second, "Patient copy 2.mrml")))
            self.assertTrue(os.path.isdir(os.path.join(held, "Images")), "the copy of the other Slicer Studio is kept")
            self.assertEqual(projects.removeCopyLeftovers(), [])
            self.assertTrue(os.path.isdir(held))
        finally:
            projects._unlockCopy(descriptor, heldLock)
        # Once that Slicer Studio is gone (its lock is released), what it left is a leftover; and so is a lock file
        # without its copy (killed after the rename) or one that nothing holds
        stale = os.path.join(parent, "Patient copy 3" + projects.COPY_MARKER)
        os.makedirs(stale)
        for lockPath in (projects._copyLockPath(stale), projects._copyLockPath(os.path.join(parent, "Gone" + projects.COPY_MARKER))):
            with open(lockPath, "w", encoding="utf-8"):
                pass
        os.makedirs(held, exist_ok=True)
        self.assertEqual(sorted(projects.removeCopyLeftovers()), ["Patient copy 2.studio-copy", "Patient copy 3.studio-copy"])
        self.assertEqual(entries(), ["Patient", "Patient copy", "Patient copy 2"])

    def test_57d_duplicate_whose_copy_lost_its_scene_is_not_reported_done(self):
        """A copy whose scene file (or project.json) is missing when the copy ends (its folder was deleted while it was
        copied) is not renamed into a project that cannot be opened: the Duplicate fails and leaves nothing (BB-1).
        """
        import shutil as shutilModule
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        project = projects.createProject("Scan")
        os.makedirs(os.path.join(project.path, "Images"))
        with open(os.path.join(project.path, "Images", "scan.nrrd"), "wb") as f:
            f.write(b"\1" * 4096)
        with open(os.path.join(project.path, "Scan.mrml"), "w", encoding="utf-8") as f:
            f.write("<MRML></MRML>\n")
        parent = os.path.dirname(project.path)
        realCopytree = shutilModule.copytree

        def lossyCopytree(source, destination, *args, **kwargs):
            result = realCopytree(source, destination, *args, **kwargs)
            if os.fspath(destination).endswith(projects.COPY_MARKER):  # the project folder (not its subfolders)
                os.remove(os.path.join(destination, "Scan.mrml"))
            return result

        toasts = []
        with mock.patch.object(projects_ui, "message", side_effect=lambda text, **kwargs: toasts.append(str(text))):
            with mock.patch.object(shutilModule, "copytree", side_effect=lossyCopytree):
                projects_ui.duplicate(project.path)
                self.assertTrue(self.io.backgroundWriter().drain(30.0))
        self.assertEqual(len(toasts), 1, toasts)
        self.assertIn("could not be duplicated", toasts[0])
        self.assertEqual(sorted(os.listdir(parent)), ["Scan"])

    def test_58_open_project_on_a_folder_outside_the_studio_folder_leaves_it_unchanged(self):
        """Open project… on a project folder outside the Slicer Studio folder (a colleague's project copied to the Desktop
        under another name) opens a copy in Projects/ and leaves the original byte-for-byte unchanged (A-09, BB-1). The
        renamed-folder repair ran on the outside folder first: its scene was renamed to "<folder>.mrml" and its
        project.json rewritten, while the toast said "The original is not changed".
        """
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        outside = os.path.join(self.userDir, "Patient7_from Bob")
        shutil.copytree(projectPath, outside)
        lock = os.path.join(outside, ".studio", "lock")
        if os.path.exists(lock):
            os.remove(lock)
        before = {path: _md5File(os.path.join(outside, path)) for path in _relativeFiles(outside)}
        self.assertIn(os.path.basename(projectPath) + ".mrml", before, "the scene keeps its old name in the outside folder")
        announced = []
        service.setProjectAnnouncer(lambda event, opened, **info: announced.append((event, opened.name if opened else None)))

        self.assertTrue(service.openProject(outside))
        slicer.app.processEvents()
        self.io.backgroundWriter().drain(30.0)
        self.assertIsNotNone(service.project)
        self.assertEqual(os.path.dirname(service.project.path), projects.projectsFolder(), "a copy opens in Projects/")
        self.assertEqual(service.project.name, "Patient7 from Bob", "the copy is named after the folder")
        self.assertIn(("copy", "Patient7 from Bob"), announced)
        self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("Lesions"))
        self._save(service)
        self.assertTrue(os.path.isfile(os.path.join(service.project.path, "Patient7 from Bob.mrml")))
        after = {path: _md5File(os.path.join(outside, path)) for path in _relativeFiles(outside)}
        self.assertEqual(after, before, "the outside folder is not changed (no file renamed, added or rewritten)")

    def test_59_read_only_project_folder_opens_as_a_copy_not_as_open_elsewhere(self):
        """A project folder in Projects/ that cannot be written (a read-only share or medium) is not "open in another
        Slicer Studio" and does not fail silently: it continues as a copy in Projects/ that saves, and the original is left
        unchanged (BB-1). The lock file could not be created, so Project.lock() returned False as for a lock held elsewhere;
        a folder without .studio (written by official Slicer) raised the OSError of os.makedirs inside the scene load.
        The tests run as root, so the read-only folder is simulated: os.access (Studio project) or os.makedirs (plain
        folder) fail for paths inside it as on a read-only file system.
        """
        import errno
        from unittest import mock

        projects = self.projects
        service = self._service()
        self._userVolume("MR Head", shape=(12, 14, 16), extension=".nrrd")
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)

        def copyOf(name, keepStudio):
            folder = os.path.join(projects.projectsFolder(), name)
            shutil.copytree(projectPath, folder)
            lock = os.path.join(folder, ".studio", "lock")
            if os.path.exists(lock):
                os.remove(lock)
            if not keepStudio:
                shutil.rmtree(os.path.join(folder, ".studio"))
            os.rename(projects.sceneFile(folder), os.path.join(folder, name + ".mrml"))
            return folder

        realAccess = os.access
        realMakedirs = os.makedirs

        def inside(path, folder):
            real = os.path.normcase(os.path.realpath(str(path)))
            return real == os.path.normcase(os.path.realpath(folder)) or real.startswith(os.path.normcase(os.path.realpath(folder)) + os.sep)

        announced = []
        service.setProjectAnnouncer(lambda event, opened, **info: announced.append(
            (event, opened.name if opened else None, info.get("copy").name if info.get("copy") else None)))
        cases = [("Shared project", True), ("Shared scene", False)]
        for name, keepStudio in cases:
            with self.subTest(name=name):
                folder = copyOf(name, keepStudio)
                before = {path: _md5File(os.path.join(folder, path)) for path in _relativeFiles(folder)}

                def access(path, mode, *args, folder=folder, **kwargs):
                    if mode & os.W_OK and inside(path, folder):
                        return False
                    return realAccess(path, mode, *args, **kwargs)

                def makedirs(path, *args, folder=folder, **kwargs):
                    if inside(path, folder):
                        raise OSError(errno.EROFS, os.strerror(errno.EROFS), str(path))
                    return realMakedirs(path, *args, **kwargs)

                # A Studio project: os.access tells; a plain folder: only creating .studio fails (as on NFS, where access
                # may not know)
                patch = mock.patch.object(os, "access", access) if keepStudio else mock.patch.object(os, "makedirs", makedirs)
                announced.clear()
                with patch:
                    readOnly = projects.Project(folder)
                    self.assertFalse(readOnly.lock())
                    self.assertTrue(readOnly.readOnly, "a folder that cannot be written is told apart from a lock held elsewhere")
                    self.assertFalse(projects.isLockedByOther(folder))
                    self.assertTrue(service.openProject(folder))
                    slicer.app.processEvents()
                    self.io.backgroundWriter().drain(30.0)
                    self.assertIsNotNone(service.project, f"{name}: changes are saved to a project")
                    self.assertEqual(service.project.name, name + " 2", "the copy is named after the folder")
                    self.assertEqual(os.path.dirname(service.project.path), projects.projectsFolder())
                    self.assertNotIn("locked", [event for event, *_rest in announced], "not 'open in another Slicer Studio'")
                    self.assertIn(("read-only", name, name + " 2"), announced)
                    self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("MR Head"))
                    points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "P")
                    points.AddControlPoint(1, 2, 3)
                    self._save(service)
                self.assertTrue(os.path.isfile(os.path.join(service.project.path, name + " 2.mrml")))
                self.assertTrue(_relativeFiles(os.path.join(service.project.path, "Images")), "the scan is copied into the copy")
                after = {path: _md5File(os.path.join(folder, path)) for path in _relativeFiles(folder)}
                self.assertEqual(after, before, "the read-only folder is not changed")
                service.releaseProject()
                slicer.mrmlScene.Clear(0)
                self.io.backgroundWriter().drain(10.0)

    def test_60_save_data_in_this_slicer_is_not_another_programs_change(self):
        """Classic saves with File > Save Data (Ctrl+S) while the open project keeps saving: the files that this Slicer
        wrote itself (through the nodes' own storage nodes) are the project's files, not another program's change. The
        next save writes them again in place, with no "(Slicer Studio)" copy and no notice; a later change by another
        program is still kept (also for a large volume written in the background).
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Pts")
        points.AddControlPoint((1, 2, 3))
        big = slicer.util.addVolumeFromArray(np.random.default_rng(3).random((64, 128, 128)).astype(np.float32), name="Derived")
        project = self._projectFor(service)
        self._save(service)
        service.studioActiveOverride = False  # Classic: the open project keeps saving
        scanFile = os.path.join(project.path, "Images", "Scan.nrrd")
        pointsFile = os.path.join(project.path, "Markups", "Pts.mrk.json")
        bigFile = os.path.join(project.path, "Images", "Derived.nrrd")
        self.assertEqual(volume.GetStorageNode().GetFileName(), scanFile)
        self.assertEqual(points.GetStorageNode().GetFileName(), pointsFile)
        self.assertEqual(big.GetStorageNode().GetFileName(), bigFile)
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))

        def editScan(value):
            array = slicer.util.arrayFromVolume(volume)
            array[0, 0, 0] = value
            slicer.util.arrayFromVolumeModified(volume)

        def editBig(value):
            array = slicer.util.arrayFromVolume(big)
            array[0, 0, 0] = value
            slicer.util.arrayFromVolumeModified(big)

        for value in (101, 102):  # twice: the copies used to appear every other round
            with self.subTest(round=value):
                editScan(value)
                editBig(value)
                points.SetNthControlPointPosition(0, value, 2, 3)
                time.sleep(0.02)
                # Save Data writes each node to its own file (as slicer.util.saveNode does)
                for node, path in ((volume, scanFile), (points, pointsFile), (big, bigFile)):
                    self.assertTrue(slicer.util.saveNode(node, path))
                editScan(value + 100)
                editBig(value + 100)
                points.SetNthControlPointPosition(0, value + 100, 2, 3)
                self._save(service)
                self.assertEqual([event for event, _info in announced], [], "no 'Another program changed' notice")
                self.assertEqual(sorted(path for path in self._projectFiles(project) if "Slicer Studio" in path), [])
                self.assertEqual(volume.GetStorageNode().GetFileName(), scanFile)
                self.assertEqual(points.GetStorageNode().GetFileName(), pointsFile)
                self.assertEqual(big.GetStorageNode().GetFileName(), bigFile)
                self.assertFalse(volume.GetModifiedSinceRead())
                self.assertFalse(big.GetModifiedSinceRead())
                loaded = slicer.util.loadVolume(scanFile)
                self.assertEqual(int(slicer.util.arrayFromVolume(loaded)[0, 0, 0]), value + 100, "the file holds the latest edit")
                slicer.mrmlScene.RemoveNode(loaded)
                loaded = slicer.util.loadVolume(bigFile)
                self.assertAlmostEqual(float(slicer.util.arrayFromVolume(loaded)[0, 0, 0]), value + 100)
                slicer.mrmlScene.RemoveNode(loaded)
                with open(pointsFile, encoding="utf-8") as f:
                    markup = json.load(f)["markups"][0]
                sign = -1 if markup.get("coordinateSystem") == "LPS" else 1
                self.assertEqual(sign * markup["controlPoints"][0]["position"][0], value + 100)
        # Another program changes the files after that: they are kept, Studio writes its own versions next to them
        time.sleep(0.02)
        theirs = {}
        for path in (scanFile, bigFile):
            with open(path, "ab") as f:
                f.write(b"\n")
            theirs[path] = _md5File(path)
        editScan(7)
        editBig(7)
        self._save(service)
        for path in (scanFile, bigFile):
            self.assertEqual(_md5File(path), theirs[path], "the other program's file is never overwritten")
        conflicts = sorted(conflict for event, info in announced if event == "conflict" for conflict in info["conflicts"])
        self.assertEqual(conflicts, [("Derived.nrrd", "Derived (Slicer Studio).nrrd"), ("Scan.nrrd", "Scan (Slicer Studio).nrrd")])

    def test_61_reopening_a_project_keeps_one_data_tree_item_per_node(self):
        """Opening a project loads its scene with clearing the scene first. That used to give every image and lesion one
        more subject hierarchy item at each open, which the next save wrote into the project scene (official 3D Slicer
        then listed each node several times in the Data module). A project scene that already has repeated items is
        repaired when it is opened: one item per node, the lesion keeps its segment.
        """
        import re
        import xml.etree.ElementTree as ET

        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        scenePath = self.projects.sceneFile(projectPath)

        def runtimeItems():
            shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
            ids = vtk.vtkIdList()
            shNode.GetItemChildren(shNode.GetSceneItemID(), ids, True)
            counts = {}
            for index in range(ids.GetNumberOfIds()):
                node = shNode.GetItemDataNode(ids.GetId(index))
                name = node.GetName() if node else shNode.GetItemName(ids.GetId(index))
                counts[name] = counts.get(name, 0) + 1
            return counts

        def fileItems():
            with open(scenePath, encoding="utf-8") as f:
                dataNodes = re.findall(r'<SubjectHierarchyItem id="\d+" dataNode="([^"]+)"', f.read())
            return {dataNode: dataNodes.count(dataNode) for dataNode in dataNodes}

        expected = runtimeItems()  # the image, the lesion and its segment
        self.assertEqual(sorted(expected.values()), [1, 1, 1], expected)
        self.assertEqual(sorted(fileItems().values()), [1, 1])
        for cycle in range(3):
            with self.subTest(cycle=cycle):
                slicer.mrmlScene.Clear(0)
                self.io.backgroundWriter().drain(10.0)
                self.assertIsNone(service.project)
                self.assertTrue(service.openProject(projectPath))
                self.assertEqual(runtimeItems(), expected, "one item per node after opening the project")
                lesions = slicer.mrmlScene.GetFirstNodeByName("Lesions")
                lesions.GetSegmentation().GetNthSegment(0).SetColor(0.2 * cycle, 0.5, 0.9)  # an edit: the project is saved
                self._save(service)
                self.assertEqual(sorted(fileItems().values()), [1, 1], "one item per node in the saved project scene")

        # A project scene saved before the fix: every data node item (with its segment branch) is there four times
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        tree = ET.parse(scenePath)
        sceneItem = tree.getroot().find("SubjectHierarchy/SubjectHierarchyItem")
        dataItems = [item for item in sceneItem.findall("SubjectHierarchyItem") if item.get("dataNode")]
        nextId = max(int(item.get("id")) for item in tree.getroot().iter("SubjectHierarchyItem")) + 1
        for _copy in range(3):
            for dataItem in dataItems:
                copied = ET.fromstring(ET.tostring(dataItem))
                for item in copied.iter("SubjectHierarchyItem"):  # parents come before their children
                    item.set("id", str(nextId))
                    for child in item.findall("SubjectHierarchyItem"):
                        child.set("parent", str(nextId))
                    nextId += 1
                sceneItem.append(copied)
        tree.write(scenePath, encoding="utf-8", xml_declaration=True)
        self.assertEqual(sorted(fileItems().values()), [4, 4])
        self.assertTrue(service.openProject(projectPath))
        self.assertEqual(runtimeItems(), expected, "the repeated items are not loaded")
        lesions = slicer.mrmlScene.GetFirstNodeByName("Lesions")
        self.assertEqual(lesions.GetSegmentation().GetNumberOfSegments(), 1, "the lesion keeps its segment")
        lesions.GetSegmentation().GetNthSegment(0).SetColor(0.9, 0.1, 0.1)
        self._save(service)
        self.assertEqual(sorted(fileItems().values()), [1, 1], "the project scene is repaired at the next save")


    def test_62_unwritable_studio_folder_is_reported_once_not_after_every_edit(self):
        """The Slicer Studio folder cannot be created (a read-only volume, BB-1): no project can be made, and every new
        scene fingerprint tries again. The user is told once, in plain words (no error number, no path, no Retry that would
        open the official Save Scene dialog), saving is shown as paused, and the first project is made as soon as the folder
        can be written (the message is news again if it fails again).
        """
        from StudioLib import projects_ui

        projects = self.projects
        blocker = os.path.join(self.tempDir, "a file")
        with open(blocker, "w") as f:
            f.write("not a folder")
        badRoot = os.path.join(blocker, "Slicer Studio")
        os.environ[projects.ROOT_ENVIRONMENT] = badRoot
        service = self._service()
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))
        self._userVolume()
        for edit in range(4):
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", f"F{edit}")
            service.poll()
        self.assertIsNone(service.project)
        self.assertEqual([event for event, _info in announced], ["root-failed"], "no new message after each edit")
        reason = announced[0][1]["reason"]
        self.assertNotIn("Errno", reason)
        self.assertNotIn(self.tempDir, reason)
        status = service.status()
        self.assertEqual(status.state, "paused")
        self.assertTrue(status.message.startswith("Autosave paused"), status.message)
        self.assertNotIn("Errno", status.message)
        self.assertNotIn(self.tempDir, status.message)
        self.assertTrue(service.rootUnwritable())
        # The toast: one warning, no Retry
        toasts = []
        original = projects_ui.message
        projects_ui.message = lambda text, **kwargs: toasts.append((text, kwargs))
        try:
            projects_ui.announce("root-failed", None, reason=reason)
        finally:
            projects_ui.message = original
        self.assertEqual(len(toasts), 1)
        self.assertNotIn("Errno", toasts[0][0])
        self.assertNotIn(self.tempDir, toasts[0][0])
        self.assertNotEqual(toasts[0][1].get("actionText"), "Retry")
        # The folder becomes writable (or another one is set): a project is made, quietly
        os.environ[projects.ROOT_ENVIRONMENT] = self.root
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Later")
        service.poll()
        self.assertIsNotNone(service.project)
        self.assertFalse(service.rootUnwritable())
        self.assertEqual([event for event, _info in announced], ["root-failed", "created"])
        # A later failure is news again
        service.releaseProject()
        os.environ[projects.ROOT_ENVIRONMENT] = badRoot
        slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Again")
        service.poll()
        self.assertEqual([event for event, _info in announced], ["root-failed", "created", "root-failed"])

    def test_63_first_start_chooser_does_not_keep_a_folder_that_cannot_be_created(self):
        """Continue on a folder that cannot be created (a read-only volume, BB-1) said "cannot be created" but still stored
        it as the chosen folder, so the next start did not ask again. The folder is stored only once it exists and can be
        written: Continue asks again (the toast has no error number), and Escape leaves the choice for the next start.
        """
        from StudioLib import projects_ui

        projects = self.projects
        settings = qt.QSettings()
        saved = {key: settings.value(key) for key in (projects.ROOT_KEY, projects.ROOT_CHOSEN_KEY)}
        for key in saved:
            settings.remove(key)
        blocker = os.path.join(self.tempDir, "a file")
        with open(blocker, "w") as f:
            f.write("not a folder")
        badRoot = os.path.join(blocker, "Slicer Studio")
        toasts = []
        answered = []
        original = projects_ui.message
        projects_ui.message = lambda text, **kwargs: toasts.append(str(text))
        try:
            dialog = projects_ui.showChooser(lambda: answered.append(True))
            dialog.setPath(badRoot)
            dialog.accept()
            self.assertEqual(len(toasts), 1, toasts)
            self.assertNotIn("Errno", toasts[0])
            self.assertIsNone(settings.value(projects.ROOT_KEY), "the unusable folder is not stored")
            self.assertIsNone(settings.value(projects.ROOT_CHOSEN_KEY), "so the next start asks again")
            self.assertEqual(answered, [], "the chooser asks again")
            again = projects_ui._chooser
            self.assertIsNotNone(again)
            self.assertIsNot(again, dialog)
            # Another folder is accepted
            again.setPath(os.path.join(self.tempDir, "Good folder"))
            again.accept()
            self.assertEqual(answered, [True])
            self.assertTrue(os.path.isdir(os.path.join(self.tempDir, "Good folder")))
            self.assertTrue(slicer.util.toBool(settings.value(projects.ROOT_CHOSEN_KEY)))
            # Escape on an unusable folder never blocks the application: nothing is stored, the next start asks again
            for key in saved:
                settings.remove(key)
            toasts.clear()
            answered.clear()
            dialog = projects_ui.showChooser(lambda: answered.append(True))
            dialog.setPath(badRoot)
            dialog.reject()
            self.assertEqual(len(toasts), 1, toasts)
            self.assertEqual(answered, [True])
            self.assertIsNone(projects_ui._chooser)
            self.assertIsNone(settings.value(projects.ROOT_CHOSEN_KEY))
        finally:
            projects_ui.message = original
            if projects_ui._chooser is not None:
                projects_ui._chooser.reject()
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)


    def test_64_looking_at_a_project_is_not_an_edit(self):
        """Opening a project and going through the workspaces (Draw, Map, Library) without an edit leaves "Last edited" alone.
        What the views and the editors do by themselves is not a change of the project: the nodes that the scene does not
        save (the slice models and their display and transform nodes appear once the views are shown), the state of the
        Segment Editor and the Subject Hierarchy items made again with new numbers for the same data. A change of
        the hierarchy that the user makes (a folder, a data node moved into it) and an edit of the data still count.
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        _segmentation, segmentId, _array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(project.path))
        io = self.io
        scene = slicer.mrmlScene
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(scene)
        volume = scene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")  # the nodes of the project that was opened
        lesion = scene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
        self.assertIsNotNone(volume)
        self.assertIsNotNone(lesion)

        def needsSave():
            return service.projectNeedsSave(io.sceneFingerprint(scene, io.sentinelIds()))

        self.assertFalse(needsSave(), "a project that was only opened is up to date")

        # The views show: nodes that are not saved with the scene appear and change
        for className in ("vtkMRMLModelNode", "vtkMRMLLinearTransformNode", "vtkMRMLSliceDisplayNode"):
            node = scene.CreateNodeByClass(className)
            node.SetName("view " + className)
            node.SetSaveWithScene(False)
            node.SetHideFromEditors(True)
            scene.AddNode(node)
            node.Modified()
        self.assertFalse(needsSave(), "nodes that the scene does not save are not changes")

        # The Segment Editor keeps its own state
        editorNode = scene.AddNewNodeByClass("vtkMRMLSegmentEditorNode", "SegmentEditor")
        self.assertTrue(needsSave(), "a new node is a change")
        self._save(service)
        self.assertFalse(needsSave())
        editorNode.SetSelectedSegmentID(segmentId)
        editorNode.SetActiveEffectName("Paint")
        editorNode.Modified()
        self.assertFalse(needsSave(), "the state of the Segment Editor is not an edit")

        # The hierarchy items are made again with other numbers for the same data (entering and leaving Draw does it)
        # (in their place: the order of the items is part of the project, BB-1)
        siblings = vtk.vtkIdList()
        shNode.GetItemChildren(shNode.GetSceneItemID(), siblings, False)
        siblingIds = [siblings.GetId(index) for index in range(siblings.GetNumberOfIds())]
        position = siblingIds.index(shNode.GetItemByDataNode(volume))
        shNode.RemoveItem(shNode.GetItemByDataNode(volume), False, False)  # the item only, not the volume
        newItem = shNode.CreateItem(shNode.GetSceneItemID(), volume)
        if position + 1 < len(siblingIds):
            shNode.MoveItem(newItem, siblingIds[position + 1])
        shNode.SetItemExpanded(shNode.GetSceneItemID(), False)
        shNode.Modified()
        self.assertFalse(needsSave(), "the same hierarchy with new item numbers is not a change")

        # Map shows the lesion as an outline and gives it its look back on leaving: that is not an edit, but an edit of the
        # display made meanwhile (or later) is
        try:
            from LesionNetworkMappingLib import display
        except ImportError:
            display = None
        if display is not None:
            state = display.lesionOutline(lesion, segmentId)
            self.assertFalse(needsSave(), "the outline is a view state: files never hold it (test_73)")
            display.restoreLesionDisplay(state)
            self.assertFalse(needsSave(), "the lesion looks as it did: nothing to save")
            state = display.lesionOutline(lesion, segmentId)
            lesion.GetDisplayNode().SetOpacity(0.4)  # the user's edit while the outline is shown
            display.restoreLesionDisplay(state)
            self.assertTrue(needsSave(), "a display edit made meanwhile is an edit")
            self._save(service)
            self.assertFalse(needsSave())
            state = display.lesionOutline(lesion, segmentId)
            lesion.GetDisplayNode().SetOpacity(0.5)  # an edit while Map is open: an autosave writes it, not the outline ...
            self._save(service)
            self.assertFalse(needsSave())
            display.restoreLesionDisplay(state)
            self.assertFalse(needsSave(), "... so leaving Map has nothing more to save")
            lesion.GetDisplayNode().SetOpacity(0.7)
            self.assertTrue(needsSave(), "a display edit after Map is an edit")
            self._save(service)
            self.assertFalse(needsSave())

        # Going back to Library (projects_ui.onWorkspaceEntered) saves nothing: the project file keeps the time of the last edit
        projectJson = os.path.join(project.path, ".studio", "project.json")
        before = (self.projects.readJson(projectJson).get("modified"), os.path.getmtime(self.projects.sceneFile(project.path)))
        time.sleep(1.1)
        self.assertFalse(service.autosaveNow("workspace", synchronous=True), "nothing to save")
        self.io.backgroundWriter().drain(30.0)
        after = (self.projects.readJson(projectJson).get("modified"), os.path.getmtime(self.projects.sceneFile(project.path)))
        self.assertEqual(after, before, "Last edited and the scene file are as they were")

        # A change that the user makes is one
        folder = shNode.CreateFolderItem(shNode.GetSceneItemID(), "Group")
        self.assertTrue(needsSave(), "a new folder is a change")
        self._save(service)
        self.assertFalse(needsSave())
        shNode.SetItemParent(shNode.GetItemByDataNode(volume), folder)
        self.assertTrue(needsSave(), "moving a node into a folder is a change")
        self._save(service)
        self.assertFalse(needsSave())
        volume.SetName("Scan renamed")
        self.assertTrue(needsSave(), "a rename is a change")
        self._save(service)
        self.assertFalse(needsSave())
        lesion.GetSegmentation().GetNthSegment(0).SetColor(0.3, 0.6, 0.1)
        self.assertTrue(needsSave(), "a data edit is a change")
        self._save(service)
        self.assertFalse(needsSave())

    def test_65_hidden_library_does_not_list_projects_and_a_save_rebuilds_one_card(self):
        """With many projects ("Show all"), every save of the open project rebuilt every card and stalled the application
        for a few hundred milliseconds, even while working in Draw. A Library that is not on screen waits (a scheduled
        refresh is held until it is shown), and a refresh builds again only the card of the project that changed.
        """
        from StudioLib import projects_ui

        projects = self.projects
        created = [projects.createProject(name) for name in ("Alpha", "Beta", "Gamma")]
        parent = qt.QWidget()
        layout = qt.QVBoxLayout(parent)
        section = projects_ui.ProjectsSection()
        layout.addWidget(section.widget)
        section.deferWhenHidden = True
        built = []
        original = projects_ui.ProjectsSection._card

        def counting(sectionSelf, info):
            built.append(info.name)
            return original(sectionSelf, info)

        def pump(seconds):
            end = time.time() + seconds
            while time.time() < end:
                slicer.app.processEvents()
                time.sleep(0.02)

        try:
            parent.hide()
            section.refresh()  # an explicit refresh always lists (the Library does it on entering)
            self.assertEqual(len(section.cards()), 3)
            before = {str(card.property("studioProjectPath")): card for card in section.cards()}
            projects_ui.ProjectsSection._card = counting
            # A save of one project while the Library is hidden: nothing is listed or built
            target = created[1]
            self.io.atomicWriteJson(projects.projectFile(target.path), {"name": target.name, "modified": projects._isoTime(time.time() + 60)})
            section.scheduleRefresh()
            pump(projects_ui.REFRESH_DELAY_MS / 1000.0 + 0.5)
            self.assertEqual(built, [], "a hidden Library builds no cards")
            # Shown: it catches up, and only the changed card is built again
            parent.show()
            pump(projects_ui.REFRESH_DELAY_MS / 1000.0 + 1.0)
            self.assertEqual(built, [target.name])
            after = {str(card.property("studioProjectPath")): card for card in section.cards()}
            self.assertEqual(sorted(after), sorted(before))
            for path, card in after.items():
                if path == target.path:
                    self.assertIsNot(card, before[path])
                else:
                    self.assertIs(card, before[path], "the card of an unchanged project is kept")
            # Shown: a change is listed with no wait for a switch, as before
            built.clear()
            self.io.atomicWriteJson(projects.projectFile(target.path), {"name": target.name, "modified": projects._isoTime(time.time() + 120)})
            section.scheduleRefresh()
            pump(projects_ui.REFRESH_DELAY_MS / 1000.0 + 1.0)
            self.assertEqual(built, [target.name])
        finally:
            projects_ui.ProjectsSection._card = original
            section.dispose()
            parent.deleteLater()

    def test_66_web_file_dialogs_without_a_folder_start_in_the_last_open_folder(self):
        """README: "File dialogs start in /data until you choose another folder". Map > Import coordinates (and about twenty
        other dialogs) passed "" or "~" as the start folder, so the first of them opened in the working folder of the
        container, its HOME. In the web build the first such dialog starts in ``Studio/LastOpenDirectory`` (the start-up
        script sets it to /data); a later one is left to Qt, which reuses the folder of the last dialog. Elsewhere nothing changes.
        """
        from StudioLib import importer

        settings = qt.QSettings()
        savedFolder = settings.value("Studio/LastOpenDirectory")
        savedWeb = os.environ.get("SLICER_STUDIO_WEB")
        savedGiven = importer._webStartFolderGiven
        realGetOpenFileName = qt.QFileDialog.getOpenFileName
        folder = os.path.join(self.tempDir, "data folder")
        os.makedirs(folder)
        starts = []
        try:
            settings.setValue("Studio/LastOpenDirectory", folder)
            os.environ.pop("SLICER_STUDIO_WEB", None)
            if not os.path.exists("/.dockerenv"):
                importer._webStartFolderGiven = False
                self.assertEqual(importer.dialogStartFolder("~"), "~", "the desktop keeps what the dialog passed")
                self.assertEqual(importer.dialogStartFolder(), "")
            os.environ["SLICER_STUDIO_WEB"] = "1"
            importer._webStartFolderGiven = False
            self.assertEqual(importer.dialogStartFolder(os.path.expanduser("~")), folder)
            self.assertEqual(importer.dialogStartFolder(os.path.expanduser("~")), "", "later dialogs reuse the last folder of Qt")
            settings.setValue("Studio/LastOpenDirectory", os.path.join(self.tempDir, "gone"))
            importer._webStartFolderGiven = False
            self.assertIn(importer.dialogStartFolder(), ("/data", ""), "a folder that does not exist is not offered")
            self.assertNotEqual(importer.dialogStartFolder("~"), os.path.join(self.tempDir, "gone"))
            # The real call sites: Map > Import coordinates and Import stimulation sites, then a Studio card
            settings.setValue("Studio/LastOpenDirectory", folder)
            importer._webStartFolderGiven = False
            slicer.util.selectModule("LesionNetworkMapping")
            widget = slicer.modules.lesionnetworkmapping.widgetRepresentation().self()

            def fakeGetOpenFileName(parent, caption, start, *args):
                starts.append((str(caption), str(start)))
                return ""

            qt.QFileDialog.getOpenFileName = fakeGetOpenFileName
            widget.onImportCoordinates()
            widget.onImportStimulation()
            self.assertEqual(starts[0], ("Import coordinates", folder))
            self.assertEqual(starts[1][0], "Import stimulation sites")
            self.assertEqual(starts[1][1], "", "the second dialog leaves the folder to Qt")
            importer._webStartFolderGiven = False
            from StudioLib import libcard_sources

            source = libcard_sources.SourcesCard.__new__(libcard_sources.SourcesCard)
            source.widget = None
            del starts[:]
            source.chooseFile()
            self.assertEqual(starts, [("Import records", folder)])
        finally:
            qt.QFileDialog.getOpenFileName = realGetOpenFileName
            importer._webStartFolderGiven = savedGiven
            if savedWeb is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = savedWeb
            if savedFolder is None:
                settings.remove("Studio/LastOpenDirectory")
            else:
                settings.setValue("Studio/LastOpenDirectory", savedFolder)


    def test_66d_web_figure_and_template_image_dialogs_start_in_the_last_open_folder(self):
        """README: "File dialogs start in /data until you choose another folder". Figure > Show source... passed "" (Qt then
        opens the container HOME), "Choose the template image" fell back to HOME, and Open figure... always restarted in /data
        even after a figure was picked in a subfolder.
        """
        from StudioLib import figure, templates_ui

        settings = qt.QSettings()
        keys = ("Studio/LastOpenDirectory", figure.LAST_FOLDER_KEY, templates_ui.LAST_DIRECTORY_KEY)
        saved = {key: settings.value(key) for key in keys}
        savedWeb = os.environ.get("SLICER_STUDIO_WEB")
        data = os.path.join(self.tempDir, "data")
        sub = os.path.join(data, "figs")
        os.makedirs(sub)
        try:
            for key in keys:
                settings.remove(key)
            settings.setValue("Studio/LastOpenDirectory", data)
            os.environ["SLICER_STUDIO_WEB"] = "1"
            self.assertEqual(figure._lastFigureFolder(), data)
            self.assertEqual(templates_ui._lastDirectory(), data)
            settings.setValue(figure.LAST_FOLDER_KEY, sub)
            self.assertEqual(figure._lastFigureFolder(), sub, "a folder that was chosen stays")
            settings.setValue(templates_ui.LAST_DIRECTORY_KEY, sub)
            self.assertEqual(templates_ui._lastDirectory(), sub)
            settings.setValue(figure.LAST_FOLDER_KEY, os.path.join(self.tempDir, "gone"))
            self.assertEqual(figure._lastFigureFolder(), data)
            os.environ.pop("SLICER_STUDIO_WEB", None)
            settings.remove(templates_ui.LAST_DIRECTORY_KEY)
            self.assertEqual(figure._lastFigureFolder(), "", "the desktop build is unchanged")
            self.assertEqual(templates_ui._lastDirectory(), os.path.expanduser("~"))
        finally:
            if savedWeb is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = savedWeb
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)

    def test_66b_web_recipe_folder_and_engine_run_dialogs_start_in_the_last_open_folder(self):
        """"Add a folder of lab recipes", "Folder for lab recipes" (Save as lab recipe without a study) and "Engine run folder"
        passed no start folder, so in the web build the first of them opened in the container HOME, not /data. They now start
        in ``Studio/LastOpenDirectory`` like the other dialogs of the web build; a later one is left to Qt.
        """
        from unittest import mock

        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import card_recipe
        from StudioLib import importer

        settings = qt.QSettings()
        savedFolder = settings.value("Studio/LastOpenDirectory")
        savedWeb = os.environ.get("SLICER_STUDIO_WEB")
        savedGiven = importer._webStartFolderGiven
        realGetExistingDirectory = qt.QFileDialog.getExistingDirectory
        folder = os.path.join(self.tempDir, "data folder 2")
        os.makedirs(folder)
        starts = []

        def fakeGetExistingDirectory(parent, caption, *args):
            starts.append((str(caption), str(args[0]) if args else None))
            return ""

        def saveAsLabRecipe():
            header = card_recipe.RecipeHeader.__new__(card_recipe.RecipeHeader)
            header.recipe = object()
            header.labRecipeFolder = lambda: None
            header.currentParameters = lambda: {}
            fakeRecipes = mock.Mock()
            fakeRecipes.custom_label.return_value = "x"
            with mock.patch.object(card_recipe, "_recipes", return_value=fakeRecipes):
                header.saveLabRecipe(title="a recipe")

        try:
            settings.setValue("Studio/LastOpenDirectory", folder)
            os.environ["SLICER_STUDIO_WEB"] = "1"
            qt.QFileDialog.getExistingDirectory = fakeGetExistingDirectory
            for call, caption in (
                (lambda: card_recipe.RecipeHeader.__new__(card_recipe.RecipeHeader).chooseRecipeFolder(), "Add a folder of lab recipes"),
                (saveAsLabRecipe, "Folder for lab recipes"),
                (lambda: self._engineRunCard().chooseEngineRun(), "Engine run folder"),
            ):
                importer._webStartFolderGiven = False
                del starts[:]
                call()
                self.assertEqual(starts, [(caption, folder)], caption)
                del starts[:]
                call()
                self.assertEqual(starts, [(caption, "")], "the second dialog leaves the folder to Qt")
            os.environ.pop("SLICER_STUDIO_WEB", None)
            del starts[:]
            card_recipe.RecipeHeader.__new__(card_recipe.RecipeHeader).chooseRecipeFolder()
            self.assertEqual(starts, [("Add a folder of lab recipes", "")], "outside the web build nothing changes")
        finally:
            qt.QFileDialog.getExistingDirectory = realGetExistingDirectory
            importer._webStartFolderGiven = savedGiven
            if savedWeb is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = savedWeb
            if savedFolder is None:
                settings.remove("Studio/LastOpenDirectory")
            else:
                settings.setValue("Studio/LastOpenDirectory", savedFolder)

    def test_66c_web_map_connectome_cohort_and_card_dialogs_start_in_the_last_open_folder(self):
        """Map > Add connectome, Convert (Folder, Mask, Bundle folder), Cohort > Open study and the Clusters, Networks and
        Export cards' Open / Add ROI / export dialogs passed their own start folder, empty on first use, so in the web build the
        first of them opened in the container HOME, not /data. When they have no folder of their own they now start in
        ``Studio/LastOpenDirectory`` like the other dialogs; a folder of their own is kept, and elsewhere nothing changes.
        """
        slicer.util.selectModule("LesionNetworkMapping")
        import LesionNetworkMapping as lnm
        from LesionNetworkMappingUI import card_clusters, card_export, card_networks
        from StudioLib import importer

        widget = slicer.modules.lesionnetworkmapping.widgetRepresentation().self()
        settings = qt.QSettings()
        savedFolder = settings.value("Studio/LastOpenDirectory")
        savedExport = settings.value("Studio/LNM/LastExportFolder")
        savedCohort = settings.value(lnm.COHORT_STUDY_SETTING)
        savedWeb = os.environ.get("SLICER_STUDIO_WEB")
        savedGiven = importer._webStartFolderGiven
        realGetExistingDirectory = qt.QFileDialog.getExistingDirectory
        realGetOpenFileName = qt.QFileDialog.getOpenFileName
        folder = os.path.join(self.tempDir, "data folder 3")
        os.makedirs(folder)
        starts = []

        def fakeDialog(parent, caption, start="", *args):
            starts.append((str(caption), str(start)))
            return ""

        def convertDialog():
            dialog = lnm.ConvertConnectomeDialog.__new__(lnm.ConvertConnectomeDialog)
            dialog.source = lambda: "nifti"
            dialog.inputEdit = qt.QLineEdit()
            dialog.outEdit = qt.QLineEdit()
            return dialog

        def cohortOpenStudy():
            widget.cohortStudy = None
            widget.onOpenCohortStudy()

        def addBundle():
            widget._connectomePath = ""
            widget.onAddBundle()

        def clustersExport():
            card = card_clusters.ClustersCard.__new__(card_clusters.ClustersCard)
            card.table = object()
            card.export()

        def networksExport():
            card = card_networks.NetworksCard.__new__(card_networks.NetworksCard)
            card.rows = [object()]
            card.export()

        def exportCard():
            card = card_export.ExportCard.__new__(card_export.ExportCard)
            card._run = object
            card.studyDestination = lambda: None
            card.export()

        calls = (
            (addBundle, "Choose a connectome bundle (.lnmc folder)"),
            (cohortOpenStudy, "Choose a study folder"),
            (lambda: convertDialog().onBrowseInput(), "Choose the folder of the time series"),
            (lambda: convertDialog().onBrowseMask(), "Choose the mask of the connectome grid"),
            (lambda: convertDialog().onBrowseOut(), "Choose where the bundle folder is written"),
            (lambda: card_clusters.SourceBar.__new__(card_clusters.SourceBar).openFile(), "Open a map"),
            (lambda: card_networks.NetworksCard.__new__(card_networks.NetworksCard).addRoiFile(), "Add a region of interest"),
            (clustersExport, "Export the cluster table into…"),
            (networksExport, "Export the networks into…"),
            (exportCard, "Export the network map into…"),
        )
        try:
            settings.setValue("Studio/LastOpenDirectory", folder)
            settings.remove("Studio/LNM/LastExportFolder")
            settings.remove(lnm.COHORT_STUDY_SETTING)
            qt.QFileDialog.getExistingDirectory = fakeDialog
            qt.QFileDialog.getOpenFileName = fakeDialog
            os.environ["SLICER_STUDIO_WEB"] = "1"
            for call, caption in calls:
                importer._webStartFolderGiven = False
                del starts[:]
                call()
                self.assertEqual(starts, [(caption, folder)], caption)
                del starts[:]
                call()
                self.assertEqual(starts, [(caption, "")], "the second dialog leaves the folder to Qt: " + caption)
            # A folder of their own is kept
            own = os.path.join(self.tempDir, "own folder")
            os.makedirs(own)
            settings.setValue("Studio/LNM/LastExportFolder", own)
            importer._webStartFolderGiven = False
            del starts[:]
            exportCard()
            card_clusters.SourceBar.__new__(card_clusters.SourceBar).openFile()
            self.assertEqual(starts, [("Export the network map into…", own), ("Open a map", own)])
            settings.remove("Studio/LNM/LastExportFolder")
            os.environ.pop("SLICER_STUDIO_WEB", None)
            for call, caption in calls:
                del starts[:]
                call()
                self.assertEqual(starts, [(caption, "")], "outside the web build nothing changes: " + caption)
        finally:
            qt.QFileDialog.getExistingDirectory = realGetExistingDirectory
            qt.QFileDialog.getOpenFileName = realGetOpenFileName
            importer._webStartFolderGiven = savedGiven
            if savedWeb is None:
                os.environ.pop("SLICER_STUDIO_WEB", None)
            else:
                os.environ["SLICER_STUDIO_WEB"] = savedWeb
            for key, value in (("Studio/LastOpenDirectory", savedFolder), ("Studio/LNM/LastExportFolder", savedExport), (lnm.COHORT_STUDY_SETTING, savedCohort)):
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)

    def _engineRunCard(self):
        from LesionNetworkMappingUI import card_robust

        card = card_robust.RobustCard.__new__(card_robust.RobustCard)
        card.widget = None
        return card

    def test_67_opening_a_project_again_while_it_loads_loads_it_once(self):
        """A second click on a project card while that project is still loading (the two releases are handled in one pass of
        the event loop; the load processes timer events) is ignored: the scene is read once, not twice.
        """
        from unittest import mock

        from StudioLib import projects_ui

        service = self._service()
        self._userVolume("Scan", shape=(12, 14, 16))
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)

        loads = []
        results = []

        def secondClick():
            results.append(projects_ui.openProject(projectPath))

        def onStart(caller, event):
            loads.append(slicer.mrmlScene.GetURL())

        tag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.StartImportEvent, onStart)
        try:
            with mock.patch.object(projects_ui, "_service", lambda: service), mock.patch.object(projects_ui, "_shell", lambda: None):
                # Two clicks handled in one pass of the event loop: the card's click filter defers each one with a 0 ms timer,
                # and the load of the first one runs the queued timer of the second
                qt.QTimer.singleShot(0, secondClick)
                self.assertTrue(projects_ui.openProject(projectPath))
                slicer.app.processEvents()
        finally:
            slicer.mrmlScene.RemoveObserver(tag)
        self.io.backgroundWriter().drain(10.0)
        self.assertEqual(len(loads), 1, "the project scene is loaded once")
        self.assertEqual(results, [True], "the second request was handled (and ignored)")
        self.assertIsNotNone(service.project)
        self.assertEqual(os.path.normcase(os.path.realpath(service.project.path)), os.path.normcase(os.path.realpath(projectPath)))
        self.assertIsNone(projects_ui._openingPath, "a finished open leaves nothing behind")
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 1)

    def test_68_documentation_names_the_first_start_folder_buttons_as_they_are_labelled(self):
        """The docs named a "Choose folder…" button in the first-start question; the button is "Choose…" (BB-1).
        Every button the documents name for that dialog must exist in it.
        """
        from StudioLib import projects_ui

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        documents = {name: os.path.join(root, *name.split("/")) for name in ("README.md", "Docs/user_guide/studio_projects.md")}
        if not all(os.path.isfile(path) for path in documents.values()):
            self.skipTest("the documentation is not next to this build")

        dialog = projects_ui.FolderChooserDialog()
        try:
            labels = {str(button.text) for button in dialog.findChildren("QPushButton")}
        finally:
            dialog.deleteLater()
        self.assertEqual(labels, {"Choose…", "Continue"})
        for name, path in documents.items():
            with open(path, encoding="utf-8") as stream:
                content = stream.read()
            self.assertNotIn("Choose folder…", content, f"{name} names a button that the folder question does not have")
            self.assertIn("Choose…", content, f"{name} does not name the button that picks another folder")

    def test_68b_documentation_names_the_folders_of_a_new_project_with_one_scan(self):
        """The docs said that a new project with one scan has just Images/ and its .mrml file; opening a scan also creates the
        empty "Lesions (<scan>)" drawing, which is saved into Segmentations/ (BB-1). The folders that a saved one-scan project
        holds must be exactly the folders that the page names for it.
        """
        import re

        from StudioLib import importer, shell

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "Docs", "user_guide", "studio_projects.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            content = " ".join(stream.read().split())
        sentence = re.search(r"A folder appears only when something is saved into it, so a new project with one scan (.*?\.mrml`)", content)
        self.assertIsNotNone(sentence, "the page no longer says what a new project with one scan holds")
        named = set(re.findall(r"`([A-Za-z]+)/`", sentence.group(1)))

        volume, scanPath = self._userVolume("one_scan", shape=(12, 14, 16))
        slicer.mrmlScene.RemoveNode(volume)
        service = self._service()
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        studioShell = shell.Shell(force=True)
        try:
            studioShell.apply()
            slicer.app.processEvents()
            importer.loadFiles([scanPath], studioShell)
            project = self._projectFor(service)
            self._save(service)
            held = {name for name in os.listdir(project.path) if os.path.isdir(os.path.join(project.path, name)) and not name.startswith(".")}
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()
        self.assertEqual(held, named, "the page names other folders for a new project with one scan than the project holds")

    def test_69_renaming_a_folder_patient_or_study_is_saved(self):
        """Renaming a folder, patient or study item in the Data module was never saved (BB-1): the item has no data node, so
        vtkMRMLSubjectHierarchyNode::SetItemName does not modify the hierarchy node, and the fingerprint cached the
        hierarchy by the node's MTime. Ctrl+S and the save before quitting wrote nothing and reopening showed the old name.
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        project = self._projectFor(service)
        io = self.io
        scene = slicer.mrmlScene
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(scene)
        sceneItem = shNode.GetSceneItemID()
        patient = shNode.CreateSubjectItem(sceneItem, "Patient 1")
        study = shNode.CreateStudyItem(patient, "Study 1")
        folder = shNode.CreateFolderItem(study, "Alpha Folder")
        shNode.SetItemParent(shNode.GetItemByDataNode(volume), folder)
        self._save(service)

        def needsSave():
            return service.projectNeedsSave(io.sceneFingerprint(scene, io.sentinelIds()))

        def sceneText():
            with open(self.projects.sceneFile(project.path), encoding="utf-8") as stream:
                return stream.read()

        self.assertFalse(needsSave())
        self.assertIn("Alpha Folder", sceneText())

        # Ctrl+S writes a folder rename
        shNode.SetItemName(folder, "Beta Folder")
        self.assertEqual(shNode.GetItemName(folder), "Beta Folder")
        self.assertTrue(needsSave(), "renaming a folder is an edit")
        self._save(service)
        self.assertFalse(needsSave())
        self.assertIn("Beta Folder", sceneText())
        self.assertNotIn("Alpha Folder", sceneText())

        # The save before quitting writes a patient and a study rename
        shNode.SetItemName(patient, "Patient 7")
        self.assertTrue(needsSave(), "renaming a patient is an edit")
        shNode.SetItemName(study, "Study 7")
        self.assertTrue(service.flushForClose())
        self.io.backgroundWriter().drain(30.0)
        self.assertFalse(needsSave())
        text = sceneText()
        for name in ("Patient 7", "Study 7", "Beta Folder"):
            self.assertIn(name, text)
        for name in ("Patient 1", "Study 1"):
            self.assertNotIn(name, text)

        # Reopening the project shows the new names
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(project.path))
        shNode = slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(slicer.mrmlScene)
        sceneItem = shNode.GetSceneItemID()
        patient = shNode.GetItemChildWithName(sceneItem, "Patient 7")
        self.assertTrue(patient, "the renamed patient is in the reopened project")
        study = shNode.GetItemChildWithName(patient, "Study 7")
        self.assertTrue(study, "the renamed study is in the reopened project")
        folder = shNode.GetItemChildWithName(study, "Beta Folder")
        self.assertTrue(folder, "the renamed folder is in the reopened project")
        volume = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLScalarVolumeNode")
        self.assertEqual(shNode.GetItemParent(shNode.GetItemByDataNode(volume)), folder)


    def test_70_cleared_or_one_voxel_lesion_is_saved_so_the_project_opens_again(self):
        """A lesion painted and then cleared (Logical operators > Clear, or every voxel erased) keeps its extent but
        holds only zeros. Saved cropped (the project writer crops segmentations), it was written as a 1x1x1 image with
        the segment extent "0 0 0 0 0 0": reading it crashed Studio and official 3D Slicer 5.12/5.13, so the project
        could never be opened again (BB-1). A one-voxel lesion was written the same way. Now an all-zero segment is
        written empty and a one-voxel image gets a blank voxel; a file saved the old way opens as well.
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        segmentation, segmentId, array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        lesionFile = os.path.join(project.path, "Segmentations", "Lesions.seg.nrrd")
        self.assertTrue(os.path.isfile(lesionFile))

        def header(path):
            with open(path, "rb") as stream:
                text = stream.read(4096).decode("latin1")
            return {line.split(":", 1)[0]: line.split(":", 1)[1].lstrip("=").strip() for line in text.split("\n\n", 1)[0].splitlines() if ":" in line}

        def reloaded(path):
            node = slicer.util.loadSegmentation(path)
            self.assertIsNotNone(node, path)
            self.assertEqual(node.GetSegmentation().GetNumberOfSegments(), 1, "the lesion segment is kept")
            loadedId = node.GetSegmentation().GetNthSegmentID(0)
            labelmap = node.GetBinaryLabelmapInternalRepresentation(loadedId)
            if labelmap is None or labelmap.IsEmpty():
                voxels = np.zeros_like(array)  # an empty segment (slicer.util cannot export one)
            else:
                voxels = slicer.util.arrayFromSegmentBinaryLabelmap(node, loadedId, volume)
            slicer.mrmlScene.RemoveNode(node)
            return voxels

        # Cleared: every voxel set to 0 (the labelmap keeps its extent, as after Logical operators > Clear or erasing)
        slicer.util.updateSegmentBinaryLabelmapFromArray(np.zeros_like(array), segmentation, segmentId, volume)
        labelmap = segmentation.GetBinaryLabelmapInternalRepresentation(segmentId)
        extent = labelmap.GetExtent()
        self.assertTrue(extent[0] <= extent[1] and extent[2] <= extent[3] and extent[4] <= extent[5], "the cleared labelmap keeps an extent")
        self._save(service)
        fields = header(lesionFile)
        self.assertEqual(fields.get("Segment0_Extent"), "0 -1 0 -1 0 -1", "a cleared lesion is written as an empty segment")
        self.assertEqual(int(reloaded(lesionFile).sum()), 0)

        # One voxel: kept where it is
        one = np.zeros_like(array)
        one[6, 7, 8] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(one, segmentation, segmentId, volume)
        self._save(service)
        self.assertNotEqual(header(lesionFile).get("sizes"), "1 1 1", "a 1x1x1 image is read as 'no image data'")
        voxels = reloaded(lesionFile)
        self.assertEqual([index.tolist() for index in np.nonzero(voxels)], [[6], [7], [8]])

        # The project opens again after the lesion was cleared
        slicer.util.updateSegmentBinaryLabelmapFromArray(np.zeros_like(array), segmentation, segmentId, volume)
        self._save(service)
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertTrue(service.openProject(project.path))
        reopened = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
        self.assertIsNotNone(reopened)
        self.assertEqual(reopened.GetSegmentation().GetNumberOfSegments(), 1)

        # A file saved the old way (1x1x1 image, segment extent "0 0 0 0 0 0") opens with an empty lesion
        oldFile = os.path.join(self.userDir, "Old cleared lesion.seg.nrrd")
        with open(oldFile, "wb") as stream:
            stream.write(
                b"NRRD0004\ntype: unsigned char\ndimension: 3\nspace: left-posterior-superior\nsizes: 1 1 1\n"
                b"space directions: (-1,0,0) (0,-1,0) (0,0,1)\nkinds: domain domain domain\nencoding: raw\n"
                b"space origin: (0,0,0)\nSegment0_Extent:=0 0 0 0 0 0\nSegment0_ID:=Segment_1\nSegment0_LabelValue:=1\n"
                b"Segment0_Layer:=0\nSegment0_Name:=Lesion\nSegmentation_ContainedRepresentationNames:=Binary labelmap|\n"
                b"Segmentation_MasterRepresentation:=Binary labelmap\nSegmentation_ReferenceImageExtentOffset:=0 0 0\n\n\x00",
            )
        old = slicer.util.loadSegmentation(oldFile)
        self.assertIsNotNone(old)
        self.assertEqual(old.GetSegmentation().GetNumberOfSegments(), 1, "the lesion segment is kept")
        labelmap = old.GetBinaryLabelmapInternalRepresentation(old.GetSegmentation().GetNthSegmentID(0))
        self.assertTrue(labelmap.IsEmpty() or labelmap.GetScalarRange()[1] == 0, "the old cleared lesion opens empty")


    def test_71_project_taken_over_after_another_window_lets_go_keeps_that_windows_later_saves(self):
        """A window that loaded a project's scene file (Load Scene, Recent, drag and drop) while another Slicer Studio had
        the project open continues the project once the other window lets go. Its scene is the one read before the other
        window's later saves: those are kept, as another program's change ("<project> (Slicer Studio).mrml" and a notice),
        never overwritten by the older scene (BB-1). When nothing changed since the scene was read, it continues in place.
        """
        projects = self.projects
        for otherSaves in (True, False):
            slicer.mrmlScene.Clear(0)
            first = self._service()
            self._userVolume("Scan" if otherSaves else "Quiet", shape=(12, 14, 16))
            markups = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "A first")
            markups.AddControlPoint([1, 1, 1])
            project = self._projectFor(first)
            self._save(first)
            first.dispose()
            self.services.remove(first)
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)
            # The other window holds the project: this one reads its scene file and is refused
            other = projects.Project(project.path)
            self.assertTrue(other.lock())
            try:
                service = self._service()
                announced = []
                service.setProjectAnnouncer(lambda event, project, announced=announced, **info: announced.append((event, info)))
                self.assertTrue(slicer.util.loadScene(project.scenePath))
                self.assertIsNone(service.project, "the project is held by the other window")
                self.assertEqual([event for event, _info in announced], ["locked"])
                if otherSaves:
                    # The other window saves a later edit into the project's scene file
                    time.sleep(0.02)
                    with open(project.scenePath, encoding="utf-8") as f:
                        text = f.read()
                    added = '<MarkupsFiducial id="vtkMRMLMarkupsFiducialNodeLater" name="A later edit" ></MarkupsFiducial>\n'
                    with open(project.scenePath, "w", encoding="utf-8") as f:
                        f.write(text.replace("</MRML>", added + "</MRML>"))
                theirs = _md5File(project.scenePath)
                slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "B edit 1").AddControlPoint([3, 3, 3])
                service.poll()
                self.assertIsNone(service.project)
            finally:
                other.unlock()
            # The other window lets go: the next edit continues the project
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "B edit 2").AddControlPoint([4, 4, 4])
            service.poll()
            self.assertIsNotNone(service.project)
            self.assertEqual(os.path.normpath(service.project.path), os.path.normpath(project.path))
            self._save(service)
            stem = os.path.basename(project.scenePath)[: -len(projects.SCENE_EXTENSION)]
            ours = os.path.join(project.path, f"{stem} (Slicer Studio).mrml")
            if otherSaves:
                self.assertEqual(_md5File(project.scenePath), theirs, "the other window's later save is not overwritten")
                with open(project.scenePath, encoding="utf-8") as f:
                    self.assertIn("A later edit", f.read())
                self.assertTrue(os.path.isfile(ours), "this window's scene is saved next to it")
                with open(ours, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("B edit 1", text)
                self.assertIn("B edit 2", text)
                self.assertIn(("conflict", {"conflicts": [(os.path.basename(project.scenePath), os.path.basename(ours))]}), announced)
            else:
                self.assertFalse(os.path.exists(ours), "nothing changed since the scene was read: it continues in place")
                with open(project.scenePath, encoding="utf-8") as f:
                    text = f.read()
                self.assertIn("A first", text)
                self.assertIn("B edit 2", text)
                self.assertNotIn("conflict", [event for event, _info in announced])
            service.releaseProject()
            service.dispose()
            self.services.remove(service)
            self.io.backgroundWriter().drain(10.0)


    def test_72_root_move_keeps_a_project_or_study_named_like_a_kind_folder_apart(self):
        """Change folder > Move into a folder that already has a project named "LNM", "Studies" or "Projects" (or a study
        named "LNM"...) with the same name (BB-1): the moved one lands as "<name> 2" with its own scene and images, like any
        other project. Those names used to be merged into the other one (one folder with "LNM 2.mrml", "Images 2" and
        ".studio 2", the moved scene pointing at the other project's image), so the moved project could no longer be
        reached and loaded the other scan. Only the kind folders themselves (Projects/, Studies/, LNM/) are merged.
        """
        projects = self.projects
        oldRoot = os.path.join(self.tempDir, "A", "Slicer Studio")
        newRoot = os.path.join(self.tempDir, "B", "Slicer Studio")
        names = (projects.LNM_DIR, projects.STUDIES_DIR, projects.PROJECTS_DIR, projects.TEMPLATES_DIR, "MRHead")

        def makeProject(rootPath, name, content):
            folder = os.path.join(rootPath, projects.PROJECTS_DIR, name)
            os.makedirs(os.path.join(folder, "Images"))
            os.makedirs(os.path.join(folder, projects.PRIVATE_DIR))
            with open(os.path.join(folder, "Images", name + ".nrrd"), "w") as f:
                f.write(content)
            with open(os.path.join(folder, name + projects.SCENE_EXTENSION), "w") as f:
                f.write('<MRML><VolumeArchetypeStorage fileName="Images/%s.nrrd"/></MRML>\n' % name)

        for name in names:
            makeProject(oldRoot, name, "A " + name)
            makeProject(newRoot, name, "B " + name)
            os.makedirs(os.path.join(oldRoot, projects.STUDIES_DIR, name))
            with open(os.path.join(oldRoot, projects.STUDIES_DIR, name, "study.json"), "w") as f:
                f.write('{"from": "A"}\n')
            os.makedirs(os.path.join(newRoot, projects.STUDIES_DIR, name))
            with open(os.path.join(newRoot, projects.STUDIES_DIR, name, "study.json"), "w") as f:
                f.write('{"from": "B"}\n')
        os.makedirs(os.path.join(oldRoot, projects.LNM_DIR, projects.LNM_DIR))
        with open(os.path.join(oldRoot, projects.LNM_DIR, projects.LNM_DIR, "map.txt"), "w") as f:
            f.write("A")
        os.makedirs(os.path.join(newRoot, projects.LNM_DIR, projects.LNM_DIR))
        with open(os.path.join(newRoot, projects.LNM_DIR, projects.LNM_DIR, "map.txt"), "w") as f:
            f.write("B")
        before = {name: _relativeFiles(os.path.join(newRoot, projects.PROJECTS_DIR, name)) for name in names}
        renamed = {}
        projects.moveContent(oldRoot, newRoot, renamed=renamed)
        projectsDir = os.path.join(newRoot, projects.PROJECTS_DIR)
        self.assertEqual(sorted(os.listdir(projectsDir)), sorted([*names, *(name + " 2" for name in names)]))
        for name in names:
            theirs = os.path.join(projectsDir, name)
            self.assertEqual(_relativeFiles(theirs), before[name], "%s: the other project gets no files of the moved one" % name)
            with open(os.path.join(theirs, "Images", name + ".nrrd")) as f:
                self.assertEqual(f.read(), "B " + name)
            ours = os.path.join(projectsDir, name + " 2")
            self.assertEqual(_relativeFiles(ours), sorted(["Images/%s.nrrd" % name, name + projects.SCENE_EXTENSION]),
                             "%s: the moved project keeps its own files under their own names" % name)
            with open(os.path.join(ours, "Images", name + ".nrrd")) as f:
                self.assertEqual(f.read(), "A " + name, "%s: the moved scene finds its own image" % name)
            self.assertTrue(os.path.isdir(os.path.join(ours, projects.PRIVATE_DIR)))
            self.assertEqual(renamed.get(os.path.join(projects.PROJECTS_DIR, name)), os.path.join(projects.PROJECTS_DIR, name + " 2"))
            self.assertEqual(os.path.normpath(projects.pathAfterMove(os.path.join(oldRoot, projects.PROJECTS_DIR, name), oldRoot, newRoot, renamed)),
                             os.path.normpath(ours), "%s: the open project follows its own folder" % name)
            for study, origin in ((name, "B"), (name + " 2", "A")):
                with open(os.path.join(newRoot, projects.STUDIES_DIR, study, "study.json")) as f:
                    self.assertIn(origin, f.read(), study)
        self.assertEqual(sorted(os.listdir(os.path.join(newRoot, projects.LNM_DIR))), [projects.LNM_DIR, projects.LNM_DIR + " 2"])
        self.assertFalse(os.path.exists(os.path.join(oldRoot, projects.PROJECTS_DIR)), "everything moved")


    def test_73_map_outline_is_never_written_into_the_project_or_a_copy(self):
        """Map shows the lesion as a white outline (fill off, white override colour): a view state. Files written while Map
        is shown keep the lesion's own look (fill on, its own colour): the project scene (Ctrl+S, then a crash), the
        autosave, a .mrb copy for a colleague (official Slicer opens it too) and the project saved by New project. Before,
        all of them held the outline, and a project reopened after New project or a crash showed the lesion outline-only
        in Draw for good (BB-1). The outline stays on screen meanwhile, and showing it is not an edit of the project.
        """
        import xml.etree.ElementTree as ET
        import zipfile

        from LesionNetworkMappingLib import display
        from StudioLib import projects_ui

        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        lesion, segmentId, _array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        io = self.io
        scene = slicer.mrmlScene
        displayNode = lesion.GetDisplayNode()
        displayNode.SetVisibility2DFill(True)
        self._save(service)

        def needsSave():
            return service.projectNeedsSave(io.sceneFingerprint(scene, io.sentinelIds()))

        def writtenLook(xml, where):
            """(fill, override colour of the lesion segment) of the segmentation display node in a scene file's text."""
            looks = []
            for element in ET.fromstring(xml).iter("SegmentationDisplay"):
                properties = {}
                for segment in (element.get("SegmentationDisplayProperties") or "").split("|"):
                    fields = segment.split()
                    if fields:
                        properties[urllib.parse.unquote(fields[0])] = dict(field.split(":", 1) for field in fields[1:] if ":" in field)
                override = properties.get(segmentId, {})
                looks.append((element.get("Visibility2DFill"), tuple(float(override.get("OverrideColor" + c, -1)) for c in "RGB")))
            self.assertEqual(len(looks), 1, where)
            return looks[0]

        ownLook = ("true", (-1.0, -1.0, -1.0))

        def assertOutlineShown(where):
            color = displayNode.GetSegmentOverrideColor(segmentId)
            self.assertEqual((displayNode.GetVisibility2DFill(), (color.GetX(), color.GetY(), color.GetZ())), (False, (1.0, 1.0, 1.0)),
                             where + ": the outline stays on screen")

        def projectLook(where):
            with open(project.scenePath, encoding="utf-8") as f:
                return writtenLook(f.read(), where)

        self.assertEqual(projectLook("before Map"), ownLook)
        state = display.lesionOutline(lesion, segmentId)
        assertOutlineShown("Map")
        self.assertFalse(needsSave(), "showing the outline is not an edit")

        # Ctrl+S in Map (a crash may follow: the file on disk is what the project reopens with)
        lesion.GetSegmentation().GetSegment(segmentId).SetName("Renamed in Map")  # an edit, so that the project is written
        lesion.StorableModified()
        self._save(service)
        self.assertEqual(projectLook("Ctrl+S in Map"), ownLook, "the project keeps the lesion's own look")
        assertOutlineShown("after Ctrl+S")
        self.assertFalse(scene.GetModifiedSinceRead(), "after Ctrl+S in Map, closing the scene or quitting asks nothing")
        self.assertFalse(needsSave(), "writing the scene while the outline is shown is not an edit")

        # The autosave (crash recovery) writes its scene text the same way
        xml, _root = io.sceneXml(scene)
        self.assertEqual(writtenLook(xml, "autosave"), ownLook)
        assertOutlineShown("after the autosave")

        # Save a copy as… .mrb
        copyPath = os.path.join(self.tempDir, "For a colleague.mrb")
        running = self.autosave._service
        self.autosave._service = service
        try:
            self.assertEqual(projects_ui.saveCopyAs(copyPath), copyPath)
        finally:
            self.autosave._service = running
        with zipfile.ZipFile(copyPath) as archive:
            sceneNames = [name for name in archive.namelist() if name.endswith(".mrml")]
            self.assertEqual(len(sceneNames), 1, archive.namelist())
            copyXml = archive.read(sceneNames[0]).decode("utf-8")
        self.assertEqual(writtenLook(copyXml, ".mrb copy"), ownLook, "the copy shows the lesion as the colleague drew it")
        assertOutlineShown("after Save a copy as")

        # A bundle written by the official writer (Save dialog, slicer.util.saveScene) while the module shows Map
        widget = slicer.modules.lesionnetworkmapping.widgetRepresentation().self()
        officialPath = os.path.join(self.tempDir, "Official.mrb")
        self.assertTrue(slicer.util.saveScene(officialPath))
        with zipfile.ZipFile(officialPath) as archive:
            officialXml = archive.read(next(name for name in archive.namelist() if name.endswith(".mrml"))).decode("utf-8")
        self.assertEqual(writtenLook(officialXml, "official .mrb"), ownLook)
        assertOutlineShown("after the official writer")
        slicer.mrmlScene.SetURL(project.scenePath)
        slicer.mrmlScene.SetRootDirectory(project.path)
        self._save(service)  # (the official writer marks the data modified)
        self.assertEqual(projectLook("saved again in Map"), ownLook)
        self.assertFalse(needsSave())

        # Leaving Map gives the lesion its look back, still with nothing to save
        display.restoreLesionDisplay(state)
        color = displayNode.GetSegmentOverrideColor(segmentId)
        self.assertEqual((displayNode.GetVisibility2DFill(), color.GetX()), (True, -1.0))
        self.assertFalse(needsSave(), "the lesion looks as it did: nothing to save")

        # New project (the scene is closed) while Map shows the outline, after an edit: the project is saved first
        widget._outlineState = display.lesionOutline(lesion, segmentId)
        assertOutlineShown("Map again")
        lesion.GetSegmentation().GetSegment(segmentId).SetName("Renamed before New project")
        lesion.StorableModified()
        asked = []

        def closeQuestion():
            dialog = slicer.app.activeModalWidget()
            if dialog is not None:
                asked.append(str(dialog.windowTitle))
                dialog.reject()

        timer = qt.QTimer()
        timer.setInterval(200)
        timer.connect("timeout()", closeQuestion)
        timer.start()
        try:
            service.newProject()  # Ctrl+N: saved, then closed with File > Close Scene
        finally:
            timer.stop()
        self.assertEqual(asked, [], "New project from Map asks nothing (the project was saved)")
        self.assertEqual(scene.GetNumberOfNodesByClass("vtkMRMLSegmentationNode"), 0, "the scene was closed")
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(widget._outlineState)
        with open(project.scenePath, encoding="utf-8") as f:
            closedXml = f.read()
        self.assertIn("Renamed before New project", closedXml, "the project was saved when the scene closed")
        self.assertEqual(writtenLook(closedXml, "New project"), ownLook, "the project closed in Map reopens with the lesion's own look")
        self.assertEqual(display._liveOutlines(), [], "no outline is left behind with the closed scene")

    def test_74_damaged_project_scene_is_not_opened_or_saved_over(self):
        """A project whose scene file was cut short (an interrupted save, a sync conflict) does not parse: nothing of it is
        read. It opened as an empty project with "Continuing ..." and no word of the damage, and the next edit saved over
        the scene with only what was added, so the project's scan and lesion dropped out of it (BB-1). It is now not
        opened: the user is told, the damaged scene and the data files are left as they are, and data added afterwards
        starts a new project.
        """
        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        projectPath, scenePath = project.path, project.scenePath
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)
        with open(scenePath, "rb") as f:
            data = f.read()
        damaged = data[: len(data) // 2]
        with open(scenePath, "wb") as f:
            f.write(damaged)
        filesBefore = [path for path in _relativeFiles(projectPath) if not path.startswith(".studio/")]
        self.assertIn("Images/Scan.nrrd", filesBefore)
        stateBefore = projects.readJson(projects.projectFile(projectPath))["state"]
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))

        self.assertFalse(service.openProject(projectPath), "a project whose scene cannot be read is not opened")
        self.assertIsNone(service.project)
        self.assertEqual([event for event, _info in announced], ["not-opened"])
        info = announced[0][1]
        self.assertTrue(info.get("damaged"))
        self.assertFalse(info.get("missing"))
        self.assertEqual(os.path.normpath(info.get("path")), os.path.normpath(projectPath))
        self.assertIn("damaged", info.get("reason"))
        self.assertEqual(slicer.mrmlScene.GetURL(), "", "the scene no longer names the damaged file")

        # The message is a warning whose Show opens the project folder (its data files are there)
        messages = []
        original = projects_ui.message
        projects_ui.message = lambda text, **kwargs: messages.append((text, kwargs))
        try:
            projects_ui.announce("not-opened", None, **info)
        finally:
            projects_ui.message = original
        self.assertEqual(messages[0][0], info["reason"])
        self.assertEqual((messages[0][1].get("level"), messages[0][1].get("actionText")), ("warning", "Show"))

        # Data added now (also from the project's own folder) starts a new project: nothing is saved over the damaged one
        slicer.util.loadVolume(os.path.join(projectPath, "Images", "Scan.nrrd"))
        self._userVolume("Other", shape=(12, 14, 16), extension=".nrrd")
        newProject = self._projectFor(service)
        self.assertNotEqual(os.path.normcase(newProject.path), os.path.normcase(projectPath))
        self._save(service)
        with open(scenePath, "rb") as f:
            self.assertEqual(f.read(), damaged, "the damaged scene is left as it is")
        self.assertEqual([path for path in _relativeFiles(projectPath) if not path.startswith(".studio/")], filesBefore)
        self.assertEqual(projects.readJson(projects.projectFile(projectPath))["state"], stateBefore)

        # Loaded in Classic (official Load Scene: the empty scene still names the damaged file), then data of the project
        # folder is added and Studio is shown: that does not continue the project in place over its damaged scene either
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        service.studioActiveOverride = False
        with self.assertRaises(RuntimeError):
            slicer.util.loadScene(scenePath)
        self.assertIsNone(service.project)
        self.assertEqual(os.path.normpath(slicer.mrmlScene.GetURL()), os.path.normpath(scenePath))
        slicer.util.loadVolume(os.path.join(projectPath, "Images", "Scan.nrrd"))
        service.studioActiveOverride = True
        copyProject = self._projectFor(service)
        self.assertNotEqual(os.path.normcase(copyProject.path), os.path.normcase(projectPath))
        self._save(service)
        with open(scenePath, "rb") as f:
            self.assertEqual(f.read(), damaged, "the damaged scene is left as it is")
        self.assertEqual([path for path in _relativeFiles(projectPath) if not path.startswith(".studio/")], filesBefore)

    def test_75_project_json_of_another_shape_does_not_hide_the_other_projects(self):
        """A .studio/project.json that is valid JSON but not an object (a list, a string), or whose "counts" is not an
        object, made listProjects raise: the Library showed no project at all (its empty state) and the crash scan at
        startup failed for every project (BB-1). The damaged project is now listed with fallback values, like a project.json
        that cannot be read, and the other projects are listed as before.
        """
        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        for name in ("Alpha", "Beta"):
            good = projects.createProject(name)
            self.io.atomicWriteJson(projects.projectFile(good.path), {"name": name, "state": "closed", "counts": {"scans": 1}})
        shapes = ("[1, 2]", '"hello"', "3", '{"counts": 3}', '{"counts": [1, 2]}',
                  '{"counts": {"scans": "two", "maps": [1], "models": {"a": 1}, "tables": true, "lesions": 2}}')
        damaged = projects.createProject("Damaged")
        for text in shapes:
            with open(projects.projectFile(damaged.path), "w", encoding="utf-8") as f:
                f.write(text)
            infos = {info.name: info for info in projects.listProjects()}
            self.assertEqual(sorted(infos), ["Alpha", "Beta", "Damaged"], text)
            self.assertEqual(infos["Alpha"].contentLine(), "1 scan", text)
            info = infos["Damaged"]
            self.assertIsInstance(info.data, dict, text)
            self.assertTrue(all(isinstance(value, int) for value in info.counts.values()), text)
            self.assertEqual(info.contentLine(), "2 lesions" if "lesions" in text else "Empty", text)
            self.assertEqual(service.scanProjectsAtStartup(), [], text)
            self.assertIsInstance(projects.Project(damaged.path).jsonData(state="closed"), dict, text)
            section = projects_ui.ProjectsSection()
            try:
                section.refresh()
                names = sorted(os.path.basename(str(card.property("studioProjectPath"))) for card in section.cards())
                self.assertEqual(names, ["Alpha", "Beta", "Damaged"], text)
            finally:
                section.dispose()
        self.assertEqual(projects.contentLine({"scans": "two", "maps": 1}), "1 map")
        # A save repairs the file: project.json is an object again
        with open(projects.projectFile(damaged.path), "w", encoding="utf-8") as f:
            f.write("[1, 2]")
        projects.Project(damaged.path).writeJson(state="closed")
        self.io.backgroundWriter().drain(10.0)
        self.assertEqual(projects.readJson(projects.projectFile(damaged.path)).get("name"), "Damaged")

    @unittest.skipUnless(sys.platform.startswith("linux"), "the freedesktop.org Trash (a temporary home stands in for it)")
    def test_76_move_to_trash_uses_the_system_trash_without_asking(self):
        """Move to Trash (a project, a template) puts the folder in the Trash: no "There is no Trash here" question.

        PythonQt has only the instance QFile.moveToTrash(), and Qt 5 puts the Trash into the current folder when the data
        folder (~/.local/share) does not exist yet.
        """
        projects = self.projects
        home = os.path.join(self.tempDir, "home é")
        dataFolder = os.path.join(home, "data ü", "share")  # does not exist yet
        os.makedirs(home)
        saved = {name: os.environ.get(name) for name in ("HOME", "XDG_DATA_HOME")}
        os.environ["HOME"] = home
        os.environ["XDG_DATA_HOME"] = dataFolder
        try:
            projects.ensureRoot()
            project = projects.createProject("Trash me ü")
            name = os.path.basename(project.path)
            asked = []
            self.assertTrue(projects.trashFolder(project.path, confirmDelete=lambda: asked.append(True) or False))
            self.assertEqual(asked, [], "no question when there is a Trash")
            self.assertFalse(os.path.exists(project.path))
            self.assertTrue(os.path.isdir(os.path.join(dataFolder, "Trash", "files", name)))
            self.assertTrue(os.path.isfile(os.path.join(dataFolder, "Trash", "info", name + ".trashinfo")))
            self.assertFalse(os.path.exists(os.path.join(os.getcwd(), "files", name)), "never a Trash in the current folder")
            # A plain folder (a template's) goes there too
            folder = os.path.join(self.tempDir, "Template ä")
            os.makedirs(folder)
            self.assertTrue(projects.moveToTrash(folder))
            self.assertFalse(os.path.exists(folder))
            self.assertTrue(os.path.isdir(os.path.join(dataFolder, "Trash", "files", "Template ä")))
        finally:
            for variable, value in saved.items():
                if value is None:
                    os.environ.pop(variable, None)
                else:
                    os.environ[variable] = value

    def test_77_opening_a_project_while_a_study_is_open_closes_the_study(self):
        """Opening a project from Library while a study was open attached it, said "Continuing ..." and released it at the
        next poll (no project is saved while a study is open), so the project's later edits were never saved and nothing
        said so (BB-1). The explicit open now closes the study first (its subject's edits are written) and the project keeps
        saving; a project that cannot be opened leaves the study open; a scene file loaded while a study is open keeps
        3D Slicer's behavior (no project is attached or announced).
        """
        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("studies are not part of this build")
        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        projectPath = project.path
        service.releaseProject()
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        self.assertIsNone(service.project)

        # A study is open
        studyFolder = os.path.join(projects.studiesFolder(), "Plain")
        os.makedirs(studyFolder)
        _volume, scanPath = self._userVolume("sub-01_T1w", shape=(12, 14, 16))
        slicer.mrmlScene.Clear(0)
        shutil.copy(scanPath, os.path.join(studyFolder, "sub-01_T1w.nii.gz"))
        session = study_ui.createStudy([os.path.join(studyFolder, "sub-01_T1w.nii.gz")], folder=studyFolder)
        self.addCleanup(study_ui.closeStudy)
        self.assertIs(study_ui.session(), session)
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))

        # A folder that cannot be opened leaves the study as it is
        self.assertFalse(service.openProject(os.path.join(self.tempDir, "no such project")))
        self.assertIs(study_ui.session(), session)

        # A scene file loaded while the study is open: no project is attached, nothing is announced as continued
        announced.clear()
        slicer.util.loadScene(projects.sceneFile(projectPath), {"clear": True})
        self.assertIsNone(service.project)
        self.assertNotIn("opened", announced)
        self.assertIs(study_ui.session(), session)

        # Open project: the study is closed first, the project is attached and stays attached
        announced.clear()
        self.assertTrue(service.openProject(projectPath))
        self.assertIsNone(study_ui.session(), "the study is closed by opening a project")
        self.assertIsNotNone(service.project)
        self.assertEqual(os.path.normcase(os.path.realpath(service.project.path)), os.path.normcase(os.path.realpath(projectPath)))
        self.assertEqual(announced.count("opened"), 1)
        service.poll()
        slicer.app.processEvents()
        service.poll()
        self.assertIsNotNone(service.project, "the project is not released at the next poll")

        # Its later edits are saved
        segmentation = slicer.util.getFirstNodeByClassByName("vtkMRMLSegmentationNode", "Lesions")
        volume = slicer.util.getFirstNodeByClassByName("vtkMRMLScalarVolumeNode", "Scan")
        self.assertIsNotNone(segmentation)
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        edited = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
        edited[0:3, 0:3, 0:3] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(edited, segmentation, segmentId, volume)
        service.poll()
        self._save(service)
        self.assertIsNotNone(service.project)
        slicer.mrmlScene.Clear(0)
        reloaded = slicer.util.loadSegmentation(os.path.join(projectPath, "Segmentations", "Lesions.seg.nrrd"))
        volume = slicer.util.addVolumeFromArray(np.zeros(edited.shape, np.int16), name="Reference")
        volume.SetSpacing(1.0, 1.1, 1.2)
        saved = slicer.util.arrayFromSegmentBinaryLabelmap(reloaded, reloaded.GetSegmentation().GetNthSegmentID(0), volume)
        self.assertGreater(int(saved[0:3, 0:3, 0:3].sum()), 0, "the edit made after opening the project is saved")

    def test_78_opening_a_study_hides_the_open_projects_lesion(self):
        """A scan opened on its own (a project) and traced in Draw kept its lesion shown over every study subject's scan
        once a study was opened (on the same grid it lands inside the subject's brain), while Draw said the subject's
        lesion was empty: only the lesions of a loaded set were hidden (BB-1). Opening a study now saves and releases the
        project first (its scene keeps the lesion shown) and then hides every segmentation that is not a subject's; closing
        the study shows them again.
        """
        import xml.etree.ElementTree as ET

        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("studies are not part of this build")
        projects = self.projects
        # The study's scan, on the same grid as the project's scan
        studyFolder = os.path.join(projects.studiesFolder(), "Grid")
        os.makedirs(studyFolder)
        _subjectVolume, scanPath = self._userVolume("sub-01_T1w", shape=(12, 14, 16))
        shutil.copy(scanPath, os.path.join(studyFolder, "sub-01_T1w.nii.gz"))
        slicer.mrmlScene.Clear(0)

        # A project: a scan and its traced lesion, shown in the views
        service = self._service()
        running = self.autosave._service
        self.autosave._service = service  # as the running service of the application
        self.addCleanup(setattr, self.autosave, "_service", running)
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        lesion, _segmentId, _array = self._lesion(volume)
        self.assertTrue(lesion.GetDisplayNode().GetVisibility())
        project = self._projectFor(service)
        self._save(service)

        # Open a study (Create study opens it) and its subject
        session = study_ui.createStudy([os.path.join(studyFolder, "sub-01_T1w.nii.gz")], folder=studyFolder)
        self.addCleanup(study_ui.closeStudy)
        self.assertIs(study_ui.session(), session)
        self.assertIsNone(service.project, "the project is saved and released when the study opens")
        self.assertFalse(lesion.GetDisplayNode().GetVisibility(), "the project's lesion is hidden while the study is open")
        sid = session.study.subject_ids[0]
        self.assertTrue(session.open(sid, switchToDraw=False))
        own = session.segmentationNode()
        self.assertTrue(own.GetDisplayNode().GetVisibility(), "the subject's lesion is shown")
        shown = [node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
                 if node.GetDisplayNode() is not None and node.GetDisplayNode().GetVisibility()]
        self.assertEqual(shown, [own.GetName()], "only the subject's lesion is shown over the subject's scan")

        # The project was saved before its lesion was hidden: it opens with the lesion shown
        self.io.backgroundWriter().drain(30.0)
        displays = list(ET.parse(projects.sceneFile(project.path)).getroot().iter("SegmentationDisplay"))
        self.assertTrue(displays)
        self.assertTrue(all(element.get("visibility") == "true" for element in displays), "the project's scene keeps its lesion shown")

        # Closing the study shows the work that was open again
        self.assertTrue(study_ui.closeStudy())
        self.assertFalse(slicer.mrmlScene.IsNodePresent(own), "the subject's lesion is removed with the study")
        self.assertTrue(lesion.GetDisplayNode().GetVisibility(), "the project's lesion is shown again once the study is closed")

    def test_79_root_move_merges_the_lnm_app_folders(self):
        """Change folder > Move into a folder whose LNM/ already has ReferenceMaps/, normalization/, ControlSets/ and
        connectomes/ (BB-1): each is merged into the one there, whose name the Map module looks for. They used to move as
        "ReferenceMaps 2", "normalization 2"..., so the moved reference maps were no longer listed and the scans'
        ``LNM.Normalization`` ("normalization/<scan>/...") no longer resolved. An item inside them is renamed (" 2") only
        when its name is taken there, and LNM/'s other items as before.
        """
        try:
            from LesionNetworkMappingLib import refmaps
        except ImportError:
            self.skipTest("the Map module is not available")
        from StudioLib import templates

        projects = self.projects
        oldRoot = os.path.join(self.tempDir, "A", "Slicer Studio")
        newRoot = os.path.join(self.tempDir, "B", "Slicer Studio")
        image, mask = templates.builtinTemplates()[0].imagePath, templates.builtinTemplates()[0].maskPath
        for rootPath, label, own in ((newRoot, "B", image), (oldRoot, "A", mask)):
            lnm = os.path.join(rootPath, projects.LNM_DIR)
            refmaps.add_file(own, title=label + " map", root=refmaps.registry_root(data_directory=lnm))
            for folder, names in (("normalization", ("Scan-" + label, "Shared-0123abcd")), ("ControlSets", ("set-" + label,)),
                                  ("connectomes", ("conn-%s.lnmc" % label,)), ("python-packages", ("nibabel",))):
                for name in names:
                    os.makedirs(os.path.join(lnm, folder, name))
                    with open(os.path.join(lnm, folder, name, "from.txt"), "w") as f:
                        f.write(label)
        renamed = {}
        projects.moveContent(oldRoot, newRoot, renamed=renamed)
        lnm = os.path.join(newRoot, projects.LNM_DIR)
        self.assertEqual(sorted(os.listdir(lnm)), ["ControlSets", "ReferenceMaps", "connectomes", "normalization", "python-packages",
                                                   "python-packages 2"], "the app folders are merged; another item is renamed as before")
        self.assertEqual([entry["title"] for entry in refmaps.entries(refmaps.registry_root(data_directory=lnm))], ["B map", "A map"],
                         "the moved reference map is listed")
        for entry in refmaps.entries(refmaps.registry_root(data_directory=lnm)):
            self.assertTrue(os.path.isfile(refmaps.entry_path(entry, refmaps.registry_root(data_directory=lnm))), entry["title"])
        self.assertEqual(sorted(os.listdir(os.path.join(lnm, "normalization"))), ["Scan-A", "Scan-B", "Shared-0123abcd", "Shared-0123abcd 2"])
        with open(os.path.join(lnm, "normalization", "Scan-A", "from.txt")) as f:
            self.assertEqual(f.read(), "A", "normalization/<scan>/... of the moved folder resolves in the new data directory")
        self.assertEqual(sorted(os.listdir(os.path.join(lnm, "ControlSets"))), ["set-A", "set-B"])
        self.assertEqual(sorted(os.listdir(os.path.join(lnm, "connectomes"))), ["conn-A.lnmc", "conn-B.lnmc"])
        shared = os.path.join(projects.LNM_DIR, "normalization", "Shared-0123abcd")
        self.assertEqual(renamed.get(shared), shared + " 2", "a taken name inside an app folder moves under another name")
        with open(os.path.join(newRoot, shared + " 2", "from.txt")) as f:
            self.assertEqual(f.read(), "A")
        self.assertEqual(os.path.normpath(projects.pathAfterMove(os.path.join(oldRoot, shared, "from.txt"), oldRoot, newRoot, renamed)),
                         os.path.normpath(os.path.join(newRoot, shared + " 2", "from.txt")))
        self.assertNotIn(os.path.join(projects.LNM_DIR, "ReferenceMaps"), renamed)
        self.assertFalse(os.path.exists(os.path.join(oldRoot, projects.LNM_DIR)), "everything moved")

    def test_80_interrupted_root_move_to_another_volume_leaves_no_partial_copy_under_the_real_name(self):
        """Change folder > Move to another volume, killed during the copy, then the same move again (BB-1). The copy went
        straight to its real name (Projects/, or Projects/P2 when the new folder had Projects/ already), so the kill left
        complete and truncated projects there under the real names; the retry merged into them and moved every complete
        original as "<name> 2": each project was listed twice, and the truncated "P2" (its scan cut in the middle) kept the
        name the user knows. A copy now has a hidden name until it is complete and checked, and the retry deletes what an
        interrupted move left. Here the copy of P2's scan writes half of it and the process "dies" (an exception that no
        ``except OSError`` catches, as a kill).
        """
        import shutil as shutilModule
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects

        class Killed(BaseException):
            pass

        def makeProject(rootPath, name, size):
            folder = os.path.join(rootPath, projects.PROJECTS_DIR, name)
            os.makedirs(os.path.join(folder, "Images"))
            os.makedirs(os.path.join(folder, projects.PRIVATE_DIR))
            with open(os.path.join(folder, "Images", name + ".nrrd"), "wb") as f:
                f.write(os.urandom(size))
            with open(os.path.join(folder, name + projects.SCENE_EXTENSION), "w") as f:
                f.write('<MRML><VolumeArchetypeStorage fileName="Images/%s.nrrd"/></MRML>\n' % name)

        realCopyfile = shutilModule.copyfile

        def killedInP2(source, destination, *args, **kwargs):
            if os.path.basename(os.fspath(source)) == "P2.nrrd":
                with open(source, "rb") as original, open(destination, "wb") as f:
                    f.write(original.read(os.path.getsize(source) // 2))
                raise Killed()
            return realCopyfile(source, destination, *args, **kwargs)

        for case, existing in (("new folder", ()), ("folder with projects", ("Other",))):
            with self.subTest(case=case):
                oldRoot = os.path.join(self.tempDir, case, "A", "Slicer Studio")
                newRoot = os.path.join(self.tempDir, case, "B", "My projects")  # not named "Slicer Studio"
                for name in ("P1", "P2", "P3"):
                    makeProject(oldRoot, name, 4096)
                for name in existing:
                    makeProject(newRoot, name, 1024)
                originals = {name: _relativeFiles(os.path.join(oldRoot, projects.PROJECTS_DIR, name)) for name in ("P1", "P2", "P3")}
                with open(os.path.join(oldRoot, projects.PROJECTS_DIR, "P2", "Images", "P2.nrrd"), "rb") as f:
                    scan = f.read()
                with mock.patch.object(projects, "_sameVolume", return_value=False), mock.patch.object(shutilModule, "copyfile", side_effect=killedInP2):
                    with self.assertRaises(Killed):
                        projects.moveContent(oldRoot, newRoot)
                # After the kill: the new folder lists no partial project, the old one still has P2 complete
                self.assertEqual(sorted(info.name for info in projects.listProjects(newRoot)), sorted(existing + tuple(
                    name for name in ("P1", "P3") if not os.path.isdir(os.path.join(oldRoot, projects.PROJECTS_DIR, name)))),
                    "only complete projects have their name in the new folder")
                with open(os.path.join(oldRoot, projects.PROJECTS_DIR, "P2", "Images", "P2.nrrd"), "rb") as f:
                    self.assertEqual(f.read(), scan, "the original stays complete")
                if not existing:
                    # Change… > Choose the same folder again: it holds only the hidden partial copy, so it is used as is
                    # (no "Slicer Studio" folder inside it, where the partial copy would stay forever)
                    self.assertTrue(os.listdir(newRoot), "the partial copy is there, hidden")
                    original = qt.QFileDialog.getExistingDirectory
                    qt.QFileDialog.getExistingDirectory = staticmethod(lambda *args, newRoot=newRoot, **kwargs: newRoot)
                    dialog = projects_ui.FolderChooserDialog(proposal=oldRoot, cancellable=True)
                    try:
                        dialog.choose()
                        self.assertEqual(os.path.normpath(dialog.path), os.path.normpath(newRoot))
                    finally:
                        qt.QFileDialog.getExistingDirectory = original
                        dialog.deleteLater()
                        slicer.app.processEvents()
                # The same move again
                with mock.patch.object(projects, "_sameVolume", return_value=False):
                    renamed = {}
                    projects.moveContent(oldRoot, newRoot, renamed=renamed)
                self.assertEqual(renamed, {}, "no project moves as \"<name> 2\"")
                self.assertEqual(sorted(info.name for info in projects.listProjects(newRoot)), sorted(("P1", "P2", "P3", *existing)),
                                 "each project is listed once")
                projectsDir = os.path.join(newRoot, projects.PROJECTS_DIR)
                self.assertEqual(sorted(os.listdir(projectsDir)), sorted(("P1", "P2", "P3", *existing)), "no hidden partial copy is left")
                self.assertEqual(sorted(name for name in os.listdir(newRoot) if name.startswith(".")), [], "no hidden partial copy is left")
                for name, files in originals.items():
                    self.assertEqual(_relativeFiles(os.path.join(projectsDir, name)), files, name)
                with open(os.path.join(projectsDir, "P2", "Images", "P2.nrrd"), "rb") as f:
                    self.assertEqual(f.read(), scan, "P2 has its whole scan")
                self.assertFalse(os.path.exists(os.path.join(oldRoot, projects.PROJECTS_DIR)), "the originals are deleted after the copy")

        # Killed while the original is deleted, after its copy is complete: no half original stays under its name, so the
        # same move again does not move it as "<name> 2" next to its complete copy
        realRmtree = shutilModule.rmtree
        for case, existing in (("delete, new folder", ()), ("delete, folder with projects", ("Other",))):
            with self.subTest(case=case):
                oldRoot = os.path.join(self.tempDir, case, "A", "Slicer Studio")
                newRoot = os.path.join(self.tempDir, case, "B", "My projects")
                for name in ("P1", "P2", "P3"):
                    makeProject(oldRoot, name, 4096)
                for name in existing:
                    makeProject(newRoot, name, 1024)
                originals = {name: _relativeFiles(os.path.join(oldRoot, projects.PROJECTS_DIR, name)) for name in ("P1", "P2", "P3")}
                oldPrefix = os.path.normpath(oldRoot) + os.sep

                def killedWhileDeleting(path, *args, oldPrefix=oldPrefix, **kwargs):
                    if os.path.normpath(os.fspath(path)).startswith(oldPrefix):
                        for directory, _folders, files in os.walk(path):
                            for name in files:
                                os.remove(os.path.join(directory, name))  # the first file is deleted, then the process dies
                                raise Killed()
                    return realRmtree(path, *args, **kwargs)

                with mock.patch.object(projects, "_sameVolume", return_value=False), mock.patch.object(projects.shutil, "rmtree", side_effect=killedWhileDeleting):
                    with self.assertRaises(Killed):
                        projects.moveContent(oldRoot, newRoot)
                with mock.patch.object(projects, "_sameVolume", return_value=False):
                    renamed = {}
                    projects.moveContent(oldRoot, newRoot, renamed=renamed)
                self.assertEqual(renamed, {}, "no project moves as \"<name> 2\"")
                self.assertEqual(sorted(info.name for info in projects.listProjects(newRoot)), sorted(("P1", "P2", "P3", *existing)),
                                 "each project is listed once")
                for name, files in originals.items():
                    self.assertEqual(_relativeFiles(os.path.join(newRoot, projects.PROJECTS_DIR, name)), files, name)
                self.assertEqual(sorted(os.listdir(oldRoot)), [], "what the interrupted delete left is deleted")

    def test_81_interrupted_root_move_is_finished_at_the_next_start(self):
        """Change folder > Move to another volume, killed once an original was deleted (BB-1). The setting changed only after
        every item had moved, so the next start read the old folder, which lacked the deleted items: Library listed half
        the projects or none, the open project's crash offer was lost (its complete copy was in the new folder), and
        nothing told the user. The move is now recorded (Studio/Projects/MovePending) until the setting points at the new
        folder, and the next start finishes it, points the setting there, says so and then offers the crashed project.
        """
        import shutil as shutilModule
        from unittest import mock

        projects = self.projects

        class Killed(BaseException):
            pass

        def makeProject(rootPath, name, state):
            folder = os.path.join(rootPath, projects.PROJECTS_DIR, name)
            os.makedirs(os.path.join(folder, "Images"))
            os.makedirs(os.path.join(folder, projects.PRIVATE_DIR))
            with open(os.path.join(folder, "Images", name + ".nrrd"), "wb") as f:
                f.write(os.urandom(4096))
            with open(os.path.join(folder, name + projects.SCENE_EXTENSION), "w") as f:
                f.write('<MRML><VolumeArchetypeStorage fileName="Images/%s.nrrd"/></MRML>\n' % name)
            with open(projects.projectFile(folder), "w") as f:
                json.dump({"name": name, "state": state}, f)

        realRmtree = shutilModule.rmtree
        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        try:
            # Killed while the original Projects/ is deleted (the first item), and while Studies/ is (once Projects/ is gone)
            for case, item in (("Projects", projects.PROJECTS_DIR), ("Studies", projects.STUDIES_DIR)):
                with self.subTest(case=case):
                    oldRoot = os.path.normpath(os.path.join(self.tempDir, "interrupted " + case, "A", "Slicer Studio"))
                    newRoot = os.path.normpath(os.path.join(self.tempDir, "interrupted " + case, "B", "My projects"))
                    for name, state in (("P1", "closed"), ("P2", "open"), ("P3", "closed")):
                        makeProject(oldRoot, name, state)
                    os.makedirs(os.path.join(oldRoot, projects.STUDIES_DIR, "Study A"))
                    with open(os.path.join(oldRoot, projects.STUDIES_DIR, "Study A", "study.json"), "w") as f:
                        json.dump({"name": "Study A"}, f)
                    originals = {name: _relativeFiles(os.path.join(oldRoot, projects.PROJECTS_DIR, name)) for name in ("P1", "P2", "P3")}
                    settings.setValue(projects.ROOT_KEY, oldRoot)
                    hidden = "." + item + projects.MOVED_MARKER

                    def killedWhileDeleting(path, *args, hidden=hidden, **kwargs):
                        if os.path.basename(os.path.normpath(os.fspath(path))) == hidden:
                            for directory, _folders, files in os.walk(path):
                                for name in files:
                                    os.remove(os.path.join(directory, name))  # the first file is deleted, then the process dies
                                    raise Killed()
                        return realRmtree(path, *args, **kwargs)

                    service = self._service()
                    with mock.patch.object(projects, "_sameVolume", return_value=False), mock.patch.object(projects.shutil, "rmtree", side_effect=killedWhileDeleting):
                        with self.assertRaises(Killed):
                            service.moveRoot(newRoot, moveContent=True)
                    self.assertEqual(projects.root(), oldRoot, "the kill came before the setting changed")
                    self.assertEqual(projects.pendingMove(), (oldRoot, newRoot), "the move is recorded until the setting changes")
                    # The next start
                    restarted = self._service()
                    offered = []
                    restarted.setProjectPresenter(offered.append)
                    notices = []
                    with mock.patch.object(projects, "_sameVolume", return_value=False):
                        result = restarted.finishInterruptedMove(run=lambda function: function(),
                                                                 notify=lambda text, level, notices=notices: notices.append((level, text)))
                    self.assertEqual(result, "finished")
                    self.assertEqual(projects.root(), newRoot, "the setting points where the projects are")
                    self.assertIsNone(projects.pendingMove())
                    self.assertEqual(len(notices), 1, "the user is told")
                    self.assertEqual(notices[0][0], "info")
                    self.assertIn("finished", notices[0][1])
                    self.assertEqual(sorted(info.name for info in projects.listProjects()), ["P1", "P2", "P3"], "each project is listed once")
                    for name, files in originals.items():
                        self.assertEqual(_relativeFiles(os.path.join(newRoot, projects.PROJECTS_DIR, name)), files, name)
                    self.assertTrue(os.path.isfile(os.path.join(newRoot, projects.STUDIES_DIR, "Study A", "study.json")))
                    self.assertEqual(sorted(os.listdir(oldRoot)), [], "nothing is left in the old folder")
                    crashed = restarted.scanProjectsAtStartup()
                    self.assertEqual([info.name for info in crashed], ["P2"], "the project that was open is offered")
                    self.assertEqual(len(offered), 1)
                    self.io.backgroundWriter().drain(30.0)

            # The new folder cannot be found now (a drive that is not connected): nothing moves, the move stays pending
            oldRoot = os.path.normpath(os.path.join(self.tempDir, "unavailable", "A"))
            missing = os.path.normpath(os.path.join(self.tempDir, "unavailable", "drive", "Slicer Studio"))
            makeProject(oldRoot, "P1", "closed")
            settings.setValue(projects.ROOT_KEY, oldRoot)
            projects.recordPendingMove(oldRoot, missing)
            notices = []
            self.assertEqual(self._service().finishInterruptedMove(run=lambda function: function(), notify=lambda text, level: notices.append(level)),
                             "unavailable")
            self.assertEqual(notices, ["warning"])
            self.assertEqual(projects.root(), oldRoot)
            self.assertEqual(projects.pendingMove(), (oldRoot, missing))
            self.assertFalse(os.path.exists(missing))
            # Another folder was chosen since: the record is dropped, nothing moves
            projects.setRoot(oldRoot)
            self.assertIsNone(projects.pendingMove(), "choosing a folder ends a pending move")
            projects.recordPendingMove(os.path.join(self.tempDir, "elsewhere"), missing)
            self.assertIsNone(self._service().finishInterruptedMove(run=lambda function: function(), notify=lambda text, level: notices.append(level)))
            self.assertIsNone(projects.pendingMove())
            # A move that fails (reported to the user) leaves no pending move: the setting stays as it is
            target = os.path.join(self.tempDir, "unavailable", "B")
            with mock.patch.object(projects, "moveContent", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self._service().moveRoot(target, moveContent=True)
            self.assertIsNone(projects.pendingMove())
            self.assertEqual(projects.root(), oldRoot)
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            projects.clearPendingMove()
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_81b_interrupted_root_move_into_a_folder_with_the_same_names_follows_its_own_items(self):
        """Change folder > Move into a folder that has a study, a template and a connectome with the same names, killed
        midway; the next start finishes the move (BB-1). The items that moved as "<name> 2" before the kill, and the template
        ids made unique, were kept in memory only: the finished move left two "Stroke" studies, pointed the recent study, the
        Cohort study and the registered connectome at the new folder's own items that kept the names, and left two templates
        with one id (the default one of them). The move now writes them into its journal as it goes, and the move that
        finishes it ends as an uninterrupted move would. A kill runs no ``finally``: here the steps that a move runs in its
        ``finally`` do nothing once it is "killed".
        """
        from unittest import mock

        from StudioLib import projects_ui, study_ui, templates

        projects = self.projects

        class Killed(BaseException):
            pass

        def makeStudy(root, mark):
            path = os.path.join(root, projects.STUDIES_DIR, "Stroke")
            os.makedirs(path)
            with open(os.path.join(path, "study.json"), "w", encoding="utf-8") as f:
                json.dump({"format": "slicer-studio-study", "version": 1, "name": "Stroke", "created": "2026-01-01"}, f, indent=2)
                f.write("\n")
            with open(os.path.join(path, "whose.txt"), "w") as f:
                f.write(mark)
            return path

        def makeConnectome(root, mark):
            path = os.path.join(root, projects.LNM_DIR, "connectomes", "GSP")
            os.makedirs(path)
            with open(os.path.join(path, "whose.txt"), "w") as f:
                f.write(mark)
            return path

        def whose(path):
            with open(os.path.join(path, "whose.txt")) as f:
                return f.read()

        builtin = templates.builtinTemplates()[0]
        settings = qt.QSettings()
        keys = (templates.DEFAULT_KEY, study_ui.RECENT_STUDIES_KEY, "Studio/LNM/Connectomes", "Studio/LNM/CohortStudy")
        previous = {key: settings.value(key) for key in keys}
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        realRename = os.rename
        realFollow = projects._studiesFollowRenamedFolders
        realUnique = projects._movedTemplateIdsUnique
        try:
            # Killed right after A's study, or A's template, lands as "<name> 2"; and at the very end of the move, once
            # every item moved and the moved template has its own id
            for case, killAfter in (("study", os.path.join(projects.STUDIES_DIR, "Stroke 2")),
                                    ("template", os.path.join(projects.TEMPLATES_DIR, "My template 2")),
                                    ("end", None)):
                with self.subTest(case=case):
                    oldRoot = os.path.normpath(os.path.join(self.tempDir, "same names " + case, "A", "Slicer Studio"))
                    newRoot = os.path.normpath(os.path.join(self.tempDir, "same names " + case, "B", "Slicer Studio"))
                    newStudy = makeStudy(newRoot, "B")
                    newConnectome = makeConnectome(newRoot, "B")
                    theirs = templates.addTemplate(builtin.imagePath, name="My template", rootPath=newRoot)
                    makeStudy(oldRoot, "A")
                    oldStudy = os.path.join(oldRoot, projects.STUDIES_DIR, "Stroke")
                    oldConnectome = makeConnectome(oldRoot, "A")
                    mine = templates.addTemplate(builtin.maskPath, name="My template", rootPath=oldRoot, makeDefault=True)
                    self.assertEqual(mine.id, theirs.id, "the two templates have the same id")
                    settings.setValue(projects.ROOT_KEY, oldRoot)
                    settings.setValue(study_ui.RECENT_STUDIES_KEY, json.dumps([{"path": oldStudy, "name": "Stroke"}]))
                    settings.setValue("Studio/LNM/CohortStudy", oldStudy)
                    settings.setValue("Studio/LNM/Connectomes", [oldConnectome])
                    killed = []

                    def killingRename(source, destination, *args, killAfter=killAfter, killed=killed, **kwargs):
                        realRename(source, destination, *args, **kwargs)
                        if killAfter and not killed and os.path.normpath(os.fspath(destination)).endswith(os.sep + killAfter):
                            killed.append(destination)
                            raise Killed()

                    def follow(*args, killAfter=killAfter, killed=killed):
                        if killed:
                            return None
                        if killAfter is None:
                            killed.append("end")
                            raise Killed()
                        return realFollow(*args)

                    def unique(*args, killed=killed, **kwargs):
                        return None if killed else realUnique(*args, **kwargs)

                    service = self._service()
                    with mock.patch.object(projects.os, "rename", side_effect=killingRename), \
                            mock.patch.object(projects, "_studiesFollowRenamedFolders", side_effect=follow), \
                            mock.patch.object(projects, "_movedTemplateIdsUnique", side_effect=unique):
                        with self.assertRaises(Killed):
                            service.moveRoot(newRoot, moveContent=True, run=lambda function: function())
                    self.assertTrue(killed)
                    self.assertEqual(projects.pendingMove(), (oldRoot, newRoot))
                    # The next start
                    notices = []
                    result = self._service().finishInterruptedMove(run=lambda function: function(),
                                                                   notify=lambda text, level, notices=notices: notices.append(level))
                    self.assertEqual(result, "finished")
                    self.assertEqual(notices, ["info"])
                    self.assertEqual(projects.root(), newRoot)
                    self.assertIsNone(projects.pendingMove())
                    # Two different studies, each one listed under its own name
                    movedStudy = os.path.join(newRoot, projects.STUDIES_DIR, "Stroke 2")
                    self.assertEqual(whose(movedStudy), "A")
                    self.assertEqual(whose(newStudy), "B")
                    names = sorted(projects_ui.studySummary(path)["name"] for path in projects.listStudies(newRoot))
                    self.assertEqual(names, ["Stroke", "Stroke 2"], "two different cards in the Library")
                    # The paths follow A's items, never B's that kept the names
                    recent = json.loads(settings.value(study_ui.RECENT_STUDIES_KEY))
                    self.assertEqual(os.path.normpath(recent[0]["path"]), movedStudy, "the recent study")
                    self.assertEqual(recent[0]["name"], "Stroke 2")
                    self.assertEqual(os.path.normpath(settings.value("Studio/LNM/CohortStudy")), movedStudy, "the Cohort study")
                    connectomes = settings.value("Studio/LNM/Connectomes")
                    connectomes = [connectomes] if isinstance(connectomes, str) else list(connectomes)
                    movedConnectome = os.path.join(newRoot, projects.LNM_DIR, "connectomes", "GSP 2")
                    self.assertEqual([os.path.normpath(path) for path in connectomes], [movedConnectome], "the registered connectome")
                    self.assertEqual(whose(movedConnectome), "A")
                    self.assertEqual(whose(newConnectome), "B")
                    # A's template has its own id and name and stays the default; B's keeps its id
                    listed = {template.id: template for template in templates.userTemplates(newRoot)}
                    self.assertEqual(len(listed), 2, "two templates, two ids")
                    self.assertEqual(listed[theirs.id].imageSha256, theirs.imageSha256, "B's template keeps its id")
                    others = [template for template in listed.values() if template.id != theirs.id]
                    self.assertEqual(others[0].imageSha256, mine.imageSha256)
                    self.assertEqual(others[0].name, "My template 2")
                    self.assertEqual(templates.defaultTemplateId(), others[0].id, "A's template stays the default")
                    self.assertEqual(sorted(os.listdir(oldRoot)), [], "nothing is left in the old folder")
                    journalPath = projects.moveJournalPath()
                    self.assertFalse(journalPath and os.path.exists(journalPath), "the journal ends with the move")
        finally:
            for key, value in previous.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            projects.clearPendingMove()
            getattr(projects, "discardMoveJournal", lambda: None)()
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            templates.registerGrids(force=True)

    def test_81c_failed_root_move_into_a_folder_with_the_same_names_follows_what_moved(self):
        """Change folder > Move into a folder that has a study, a template and a connectome with the same names fails
        partway (the new disk is full); the user frees space and moves again (BB-1). The failed move left the recent study,
        the Cohort study, the registered connectomes and the default template on A's items that had moved away (as
        "<name> 2"), and the move done again, which knows nothing of those names, pointed them at the new folder's own
        items that kept the names (B's study and connectome); the Cohort study stayed on a folder that is gone. Now what
        moved is followed when the move fails, as the open project is, and the move done again ends as one move would.
        """
        import errno
        from unittest import mock

        from StudioLib import study_ui, templates

        projects = self.projects

        def makeStudy(root, mark):
            path = os.path.join(root, projects.STUDIES_DIR, "Stroke")
            os.makedirs(path)
            with open(os.path.join(path, "study.json"), "w", encoding="utf-8") as f:
                json.dump({"format": "slicer-studio-study", "version": 1, "name": "Stroke", "created": "2026-01-01"}, f, indent=2)
                f.write("\n")
            with open(os.path.join(path, "whose.txt"), "w") as f:
                f.write(mark)
            return path

        def makeConnectome(root, mark):
            path = os.path.join(root, projects.LNM_DIR, "connectomes", "GSP")
            os.makedirs(path)
            with open(os.path.join(path, "whose.txt"), "w") as f:
                f.write(mark)
            return path

        def whose(path):
            with open(os.path.join(path, "whose.txt")) as f:
                return f.read()

        def connectomeSetting():
            value = settings.value("Studio/LNM/Connectomes")
            value = [value] if isinstance(value, str) else list(value or ())
            return [os.path.normpath(path) for path in value]

        builtin = templates.builtinTemplates()[0]
        settings = qt.QSettings()
        keys = (templates.DEFAULT_KEY, study_ui.RECENT_STUDIES_KEY, "Studio/LNM/Connectomes", "Studio/LNM/CohortStudy")
        previous = {key: settings.value(key) for key in keys}
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        realRename = os.rename
        try:
            # The disk fills up while Templates/ moves (A's study and connectome moved, A's template did not), or at the
            # very end (everything but "About this folder.txt" moved, A's template with a new id)
            for case, failing in (("templates", os.path.join(projects.TEMPLATES_DIR, "My template")),
                                  ("end", projects.ABOUT_FILE)):
                with self.subTest(case=case):
                    oldRoot = os.path.normpath(os.path.join(self.tempDir, "full disk " + case, "A", "Slicer Studio"))
                    newRoot = os.path.normpath(os.path.join(self.tempDir, "full disk " + case, "B", "Slicer Studio"))
                    newStudy = makeStudy(newRoot, "B")
                    newConnectome = makeConnectome(newRoot, "B")
                    theirs = templates.addTemplate(builtin.imagePath, name="My template", rootPath=newRoot)
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(newRoot, projects.ABOUT_FILE))
                    oldStudy = makeStudy(oldRoot, "A")
                    oldConnectome = makeConnectome(oldRoot, "A")
                    mine = templates.addTemplate(builtin.maskPath, name="My template", rootPath=oldRoot, makeDefault=True)
                    self.assertEqual(mine.id, theirs.id, "the two templates have the same id")
                    with open(os.path.join(oldRoot, projects.ABOUT_FILE), "w") as f:
                        f.write("about")
                    settings.setValue(projects.ROOT_KEY, oldRoot)
                    settings.setValue(study_ui.RECENT_STUDIES_KEY, json.dumps([{"path": oldStudy, "name": "Stroke"}]))
                    settings.setValue("Studio/LNM/CohortStudy", oldStudy)
                    settings.setValue("Studio/LNM/Connectomes", [oldConnectome])

                    def fullDisk(source, destination, *args, failing=failing, **kwargs):
                        if os.path.normpath(os.fspath(source)).endswith(os.sep + failing):
                            raise OSError(errno.ENOSPC, "No space left on device", os.fspath(destination))
                        return realRename(source, destination, *args, **kwargs)

                    with mock.patch.object(projects.os, "rename", side_effect=fullDisk):
                        with self.assertRaises(OSError) as context:
                            self._service().moveRoot(newRoot, moveContent=True, run=lambda function: function())
                    self.assertEqual(context.exception.errno, errno.ENOSPC)
                    self.assertEqual(projects.root(), oldRoot, "the setting is unchanged")
                    self.assertIsNone(projects.pendingMove())
                    movedStudy = os.path.join(newRoot, projects.STUDIES_DIR, "Stroke 2")
                    movedConnectome = os.path.join(newRoot, projects.LNM_DIR, "connectomes", "GSP 2")
                    self.assertFalse(os.path.exists(oldStudy), "A's study moved")
                    self.assertEqual(whose(movedStudy), "A")
                    self.assertEqual(whose(movedConnectome), "A")
                    # What moved is followed now, though the move failed
                    recent = json.loads(settings.value(study_ui.RECENT_STUDIES_KEY))
                    self.assertEqual(os.path.normpath(recent[0]["path"]), movedStudy, "the recent study after the failure")
                    self.assertEqual(recent[0]["name"], "Stroke 2")
                    self.assertEqual(os.path.normpath(settings.value("Studio/LNM/CohortStudy")), movedStudy, "the Cohort study after the failure")
                    self.assertEqual(connectomeSetting(), [movedConnectome], "the registered connectome after the failure")
                    if case == "end":
                        self.assertNotEqual(templates.defaultTemplateId(), theirs.id, "A's template moved with a new id: the default follows it")
                    else:
                        self.assertEqual(templates.defaultTemplateId(), mine.id, "A's template did not move")
                    # The user frees space and moves again
                    self._service().moveRoot(newRoot, moveContent=True, run=lambda function: function())
                    self.assertEqual(projects.root(), newRoot)
                    self.assertEqual(sorted(os.listdir(oldRoot)), [], "nothing is left in the old folder")
                    recent = json.loads(settings.value(study_ui.RECENT_STUDIES_KEY))
                    self.assertEqual(os.path.normpath(recent[0]["path"]), movedStudy, "the recent study stays A's")
                    self.assertEqual(os.path.normpath(settings.value("Studio/LNM/CohortStudy")), movedStudy, "the Cohort study stays A's")
                    self.assertEqual(connectomeSetting(), [movedConnectome], "the registered connectome stays A's")
                    self.assertEqual(whose(newStudy), "B")
                    self.assertEqual(whose(newConnectome), "B")
                    listed = {template.id: template for template in templates.userTemplates(newRoot)}
                    self.assertEqual(len(listed), 2, "two templates, two ids")
                    self.assertEqual(listed[theirs.id].imageSha256, theirs.imageSha256, "B's template keeps its id")
                    others = [template for template in listed.values() if template.id != theirs.id]
                    self.assertEqual(others[0].imageSha256, mine.imageSha256)
                    self.assertEqual(templates.defaultTemplateId(), others[0].id, "A's template stays the default")
        finally:
            for key, value in previous.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            projects.clearPendingMove()
            getattr(projects, "discardMoveJournal", lambda: None)()
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            templates.registerGrids(force=True)

    def test_81d_failed_root_move_after_projects_moved_keeps_them_listed(self):
        """Change folder > Move fails partway (the new disk is full) after Projects/ and Studies/ moved: the setting stays on
        the old folder, whose Projects/ is gone. Library listed no project, in this session and after a restart, and nothing
        named the folder that holds them (BB-1). Now Library lists the projects and studies that moved (never the new
        folder's own), each start names that folder, and moving again ends it.
        """
        import errno
        from unittest import mock

        projects = self.projects
        settings = qt.QSettings()
        oldRoot = os.path.normpath(os.path.join(self.tempDir, "partial", "A", "Slicer Studio"))
        newRoot = os.path.normpath(os.path.join(self.tempDir, "partial", "Q", "Slicer Studio"))

        def makeProject(root, name):
            path = os.path.join(root, projects.PROJECTS_DIR, name)
            os.makedirs(path)
            with open(os.path.join(path, name + ".mrml"), "w") as f:
                f.write("<MRML></MRML>\n")
            return path

        def makeStudy(root, name):
            path = os.path.join(root, projects.STUDIES_DIR, name)
            os.makedirs(path)
            with open(os.path.join(path, "study.json"), "w", encoding="utf-8") as f:
                json.dump({"format": "slicer-studio-study", "version": 1, "name": name, "created": "2026-01-01"}, f)
            return path

        makeProject(oldRoot, "Alpha")
        makeProject(oldRoot, "Beta")
        makeStudy(oldRoot, "Stroke")
        os.makedirs(os.path.join(oldRoot, projects.LNM_DIR, "maps"))
        with open(os.path.join(oldRoot, projects.LNM_DIR, "maps", "big.bin"), "wb") as f:
            f.write(b"\0" * 64)
        makeProject(newRoot, "Theirs")
        makeStudy(newRoot, "Their study")
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        settings.setValue(projects.ROOT_CHOSEN_KEY, True)
        realRename = os.rename
        try:
            def fullDisk(source, destination, *args, **kwargs):
                if os.sep + projects.LNM_DIR + os.sep in os.fspath(source) + os.sep:
                    raise OSError(errno.ENOSPC, "No space left on device", os.fspath(destination))
                return realRename(source, destination, *args, **kwargs)

            with mock.patch.object(projects.os, "rename", side_effect=fullDisk):
                with self.assertRaises(OSError) as context:
                    self._service().moveRoot(newRoot, moveContent=True, run=lambda function: function())
            self.assertEqual(context.exception.errno, errno.ENOSPC)
            self.assertEqual(projects.root(), oldRoot, "the setting is unchanged")
            self.assertIsNone(projects.pendingMove(), "a reported failure is not finished by itself")
            self.assertFalse(os.path.exists(os.path.join(oldRoot, projects.PROJECTS_DIR)), "Projects/ moved")
            self.assertTrue(os.path.isdir(os.path.join(newRoot, projects.PROJECTS_DIR, "Alpha")))
            self.assertTrue(os.path.isdir(os.path.join(oldRoot, projects.LNM_DIR)), "LNM/ did not move")
            expectedProjects = sorted(os.path.join(newRoot, projects.PROJECTS_DIR, name) for name in ("Alpha", "Beta"))
            expectedStudies = [os.path.join(newRoot, projects.STUDIES_DIR, "Stroke")]
            # Library lists what moved, never the new folder's own projects and studies
            self.assertEqual(sorted(info.path for info in projects.listProjects()), expectedProjects)
            self.assertEqual(projects.listStudies(), expectedStudies)
            self.assertEqual(sorted(info.name for info in projects.listProjects(oldRoot)), [], "the old folder itself has none")
            # A restart: the record is kept in the settings, the listing is the same, and the user is told where they are
            settings.sync()
            self.assertEqual(sorted(info.path for info in projects.listProjects()), expectedProjects)
            told = []
            result = self._service().finishInterruptedMove(run=lambda function: function(), notify=lambda text, level: told.append((text, level)))
            self.assertEqual(result, "partial")
            self.assertEqual(len(told), 1)
            self.assertIn(projects.shortPath(newRoot), told[0][0], "the notice names the folder that holds the projects")
            self.assertEqual(told[0][1], "warning")
            self.assertEqual(projects.root(), oldRoot, "nothing is moved by itself")
            # The user frees space and moves again: one folder, everything listed once, and the record ends
            self._service().moveRoot(newRoot, moveContent=True, run=lambda function: function())
            self.assertEqual(projects.root(), newRoot)
            self.assertIsNone(projects.partialMove())
            self.assertEqual(sorted(info.name for info in projects.listProjects()), ["Alpha", "Beta", "Theirs"])
            self.assertEqual(sorted(os.path.basename(path) for path in projects.listStudies()), ["Stroke", "Their study"])
            told.clear()
            self.assertIsNone(self._service().finishInterruptedMove(notify=lambda text, level: told.append(text)))
            self.assertEqual(told, [])
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            projects.clearPendingMove()
            projects.clearPartialMove()
            projects.discardMoveJournal()
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_81e_project_left_in_new_folder_by_failed_move_opens_in_place(self):
        """After Change folder > Move failed partway, Library lists the projects that moved from the new folder (81d), but
        opening one made "a copy" in the old folder that took the edits, Library listed it twice, and moving again left the
        stale original next to the edited copy as "<name> 2" (BB-1). Now it opens and saves in place, Opened from elsewhere
        does not list it, and moving again ends with one project that holds the edits. The new folder's own projects (there
        before the move) still open as a copy.
        """
        from unittest import mock

        from StudioLib import library

        projects = self.projects
        settings = qt.QSettings()
        oldRoot = os.path.normpath(os.path.join(self.tempDir, "partial open", "A", "Slicer Studio"))
        newRoot = os.path.normpath(os.path.join(self.tempDir, "partial open", "T", "Slicer Studio"))
        os.makedirs(oldRoot)
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        settings.setValue(projects.ROOT_CHOSEN_KEY, True)
        try:
            service = self._service()
            announced = []
            service.setProjectAnnouncer(lambda event, project, **info: announced.append(event))
            for name in ("Alpha", "Beta"):
                self._userVolume(name)
                self._projectFor(service)
                self._save(service)
                service.newProject()
                slicer.mrmlScene.Clear(0)
                self.io.backgroundWriter().drain(10.0)
            theirs = os.path.join(newRoot, projects.PROJECTS_DIR, "Theirs")
            os.makedirs(theirs)
            with open(os.path.join(theirs, "Theirs.mrml"), "w") as f:
                f.write("<MRML></MRML>\n")
            names = sorted(os.listdir(projects.projectsFolder()))
            self.assertEqual(len(names), 2, names)
            # What _moveRoot leaves when Projects/ moved and Studies/ failed (the new disk is full)
            for name in names:
                shutil.move(os.path.join(oldRoot, projects.PROJECTS_DIR, name), os.path.join(newRoot, projects.PROJECTS_DIR, name))
            projects.recordPartialMove(oldRoot, newRoot, ["Theirs"], [])
            moved = os.path.join(newRoot, projects.PROJECTS_DIR, names[0])
            self.assertIn(moved, [info.path for info in projects.listProjects()])
            self.assertEqual(projects.projectOfPath(os.path.join(moved, "Images", "x.nrrd")), moved)
            self.assertEqual(projects.projectOfSceneFile(projects.sceneFile(moved)), moved)
            self.assertIsNone(projects.projectOfPath(os.path.join(theirs, "Theirs.mrml")), "the new folder's own project")
            del announced[:]
            self.assertTrue(service.openProject(moved))
            self.assertEqual(os.path.normpath(service.project.path), moved, "opened in place, not as a copy in the old folder")
            self.assertNotIn("copy", announced)
            self.assertFalse(os.path.isdir(os.path.join(oldRoot, projects.PROJECTS_DIR, names[0])))
            markups = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Edit points")
            markups.AddControlPoint([1, 2, 3], "e")
            self._save(service)
            with open(projects.sceneFile(moved), encoding="utf-8", errors="ignore") as f:
                self.assertIn("Edit points", f.read(), "the edit is saved in place")
            self.assertEqual(sorted(info.name for info in projects.listProjects()), names)
            # Opened from elsewhere does not list it (Projects does)
            class Action:
                def __init__(self, path):
                    self.path = path

                def property(self, name):
                    return {"fileName": self.path, "fileType": "SceneFile"} if name == "fileParameters" else None

            class Menu:
                def actions(self):
                    return [Action(projects.sceneFile(moved)), Action(os.path.join(theirs, "Theirs.mrml"))]

            with mock.patch.object(library, "recentlyLoadedMenu", return_value=Menu()):
                recent = [os.path.normpath(entry.path) for entry in library.recentEntries(limit=50)]
            self.assertEqual(recent, [os.path.join(theirs, "Theirs.mrml")], "the moved project is listed in Projects only")
            # The user frees space and moves again: one project of each name, and the open one holds the edits
            service.moveRoot(newRoot, moveContent=True, run=lambda function: function())
            self.assertEqual(projects.root(), newRoot)
            self.assertEqual(sorted(os.listdir(projects.projectsFolder())), sorted(names + ["Theirs"]))
            self.assertEqual(os.path.normpath(service.project.path), moved)
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            projects.clearPendingMove()
            projects.clearPartialMove()
            projects.discardMoveJournal()
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_83_crash_offer_follows_the_projects_when_the_folder_is_moved(self):
        """A crash offer (the Library card, the notice, the Classic box) made before Change folder > Move: Open project then
        opened the old folder's path, which the move had emptied, and answered "P2 was renamed or moved" (BB-1). The offer
        follows the project to the new folder, and under its new name when the new folder had one with the same name.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects

        def makeProject(rootPath, name, state):
            folder = os.path.join(rootPath, projects.PROJECTS_DIR, name)
            os.makedirs(os.path.join(folder, projects.PRIVATE_DIR))
            with open(os.path.join(folder, name + projects.SCENE_EXTENSION), "w") as f:
                f.write("<MRML></MRML>\n")
            with open(projects.projectFile(folder), "w") as f:
                json.dump({"name": name, "state": state}, f)
            return folder

        oldRoot = os.path.normpath(os.path.join(self.tempDir, "crash move", "A", "Slicer Studio"))
        newRoot = os.path.normpath(os.path.join(self.tempDir, "crash move", "B", "Slicer Studio"))
        makeProject(oldRoot, "P1", "closed")
        crashedPath = makeProject(oldRoot, "P2", "open")
        makeProject(newRoot, "P2", "closed")  # the new folder has a project of that name: the moved one becomes "P2 2"
        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        try:
            service = self._service()
            offered = []
            service.setProjectPresenter(offered.append)
            self.assertEqual([info.name for info in service.scanProjectsAtStartup()], ["P2"])
            self.assertEqual(service.recoveryPath(crashedPath), crashedPath, "before a move the offer names the project's folder")
            opened = []
            with mock.patch.object(projects_ui, "openProject", lambda path, shell=None: opened.append(path) or True), \
                    mock.patch.object(projects_ui, "_service", lambda: service):
                # The Classic box and the card are made before the move, as at startup
                box = projects_ui.showCrashOfferDialog(offered[0])
                owner = mock.Mock()
                owner.openCrashed = lambda path: projects_ui.ProjectsUi.openCrashed(owner, path)
                card = projects_ui.CrashCard(offered[0], owner)
                try:
                    service.moveRoot(newRoot, moveContent=True)
                    self.assertEqual(projects.root(), newRoot)
                    movedPath = os.path.join(newRoot, projects.PROJECTS_DIR, "P2 2")
                    self.assertTrue(os.path.isdir(movedPath))
                    self.assertFalse(os.path.exists(crashedPath))
                    self.assertEqual(service.recoveryPath(crashedPath), movedPath)
                    self.assertEqual([os.path.normpath(info.path) for info in service.pendingProjectRecoveries()], [movedPath],
                                     "a card shown again later is made with the project where it is")
                    card.openButtons[0].click()
                    self.assertEqual([os.path.normpath(path) for path in opened], [movedPath], "the card opens the moved project")
                    if box is not None:
                        for button in box.buttons():
                            if button.text == "Open project":
                                button.click()
                        self.assertEqual([os.path.normpath(path) for path in opened], [movedPath] * 2, "the Classic box does too")
                    # Moved again: the offer goes on following it
                    third = os.path.normpath(os.path.join(self.tempDir, "crash move", "C", "Slicer Studio"))
                    service.moveRoot(third, moveContent=True)
                    self.assertEqual(service.recoveryPath(crashedPath), os.path.join(third, projects.PROJECTS_DIR, "P2 2"))
                finally:
                    card.dispose()
                    if box is not None:
                        box.close()
                        box.deleteLater()
        finally:
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            self.io.backgroundWriter().drain(10.0)

    def test_82_cards_follow_the_clock_while_the_app_stays_open(self):
        """The relative time of a project card and a study card ("just now", "Edited just now") follows the clock: a card kept
        while nothing about its project changed said "just now" for hours until the app was restarted (BB-1).
        """
        from StudioLib import library, projects_ui

        projects = self.projects
        project = projects.createProject("Old project")
        studyFolder = os.path.join(projects.studiesFolder(), "Old study")
        os.makedirs(studyFolder)
        self.io.atomicWriteJson(os.path.join(studyFolder, "study.json"), {"name": "Old study"})
        section = projects_ui.ProjectsSection()
        studies = projects_ui.StudiesSection()
        real = time.time
        try:
            def metas(owner, name):
                # The cards that are listed (a card that was replaced waits for its deletion, with its labels)
                slicer.app.processEvents()
                return [str(card.findChild("QLabel", name).text) for card in owner.cards()]

            section.refresh()
            studies.refresh()
            self.assertTrue(metas(section, "studioProjectCardMeta")[0].startswith("just now"), metas(section, "studioProjectCardMeta"))
            self.assertTrue(metas(studies, "studioStudyMeta")[0].startswith("Edited just now"), metas(studies, "studioStudyMeta"))
            for offset, expected in ((3 * 3600, "3 h ago"), (2 * 86400 + 5, "2 days ago")):
                time.time = lambda offset=offset: real() + offset
                section.refresh()  # what entering the Library does: the project did not change
                studies.refresh()
                projectMeta = metas(section, "studioProjectCardMeta")[0]
                self.assertTrue(projectMeta.startswith(expected), (expected, projectMeta))
                self.assertEqual(str(section.cards()[0].property("studioProjectPath")), project.path)
                studyMeta = metas(studies, "studioStudyMeta")[0]
                self.assertTrue(studyMeta.startswith("Edited " + expected), (expected, studyMeta))
            # The Continue card is set again with the project on every refresh of the Library
            info = section.currentInfo() or next(info for info in projects.listProjects() if info.path == project.path)
            canvas = library.LibraryCanvas()
            try:
                canvas.currentWork.setProject(info)
                self.assertIn("Edited 2 days ago", canvas.currentWork.metaLabel.property("fullText"))
            finally:
                canvas.dispose()
        finally:
            time.time = real
            for owner in (section, studies):
                owner.dispose()
                owner.widget.setParent(None)
                owner.widget.deleteLater()
            slicer.app.processEvents()

    def test_84_studies_section_lists_the_studies_of_the_new_folder_after_change_folder(self):
        """Library > Studies listed only the studies of the old folder after Change folder > Move into a folder with its own
        studies, until the workspace was switched and back: only the Projects section listened to the folder changes (BB-1).
        """
        from StudioLib import projects_ui

        projects = self.projects

        def makeStudy(rootPath, name):
            folder = os.path.join(rootPath, "Studies", name)
            os.makedirs(folder)
            self.io.atomicWriteJson(os.path.join(folder, "study.json"), {"name": name})

        oldRoot = os.path.normpath(os.path.join(self.tempDir, "studies move", "A", "Slicer Studio"))
        newRoot = os.path.normpath(os.path.join(self.tempDir, "studies move", "B", "Slicer Studio"))
        makeStudy(oldRoot, "Aphasia")
        makeStudy(newRoot, "Stroke")
        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        section = projects_ui.StudiesSection()

        def listed():
            slicer.app.processEvents()
            return sorted(os.path.basename(str(card.property("studioStudyPath"))) for card in section.cards())

        def waitFor(expected, seconds=5.0):
            end = time.time() + seconds
            while time.time() < end and listed() != expected:
                time.sleep(0.02)
            return listed()

        try:
            self.assertEqual(listed(), ["Aphasia"])
            service = self._service()
            service.moveRoot(newRoot, moveContent=True)
            self.assertEqual(sorted(os.path.basename(path) for path in projects.listStudies()), ["Aphasia", "Stroke"])
            self.assertEqual(waitFor(["Aphasia", "Stroke"]), ["Aphasia", "Stroke"], "the section follows the new folder")
            # A section that was disposed no longer listens (and no timer is left to fire on it)
            section.dispose()
            self.assertNotIn(section.scheduleRefresh, projects._listeners)
        finally:
            section.dispose()
            section.widget.setParent(None)
            section.widget.deleteLater()
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            self.io.backgroundWriter().drain(10.0)
            slicer.app.processEvents()

    def test_85_continue_card_follows_the_moved_project(self):
        """After Change folder > Move the Continue card (Library) named the scan's folder in the old location until the next
        scene event, and after a move that failed partway (the open project was followed into the new folder while the setting
        stayed on the old one) it said "Open now · Not in a project yet" (BB-1). The card shows where the scan is now, and
        describes the open project wherever it is.
        """
        import errno
        from unittest import mock

        from StudioLib import library

        projects = self.projects

        def card(canvas):
            slicer.app.processEvents()
            work = canvas.currentWork
            folders = [str(label.property("fullText")) for _id, label in work._scanRows]
            return work.titleLabel.property("fullText"), work.metaLabel.property("fullText"), folders

        def waitFor(canvas, condition, seconds=10.0):
            end = time.time() + seconds
            while time.time() < end and not condition(card(canvas)):
                time.sleep(0.02)
            return card(canvas)

        service = self._service()
        volume, _path = self._userVolume("Moving scan", shape=(12, 14, 16))
        project = self._projectFor(service)
        self._save(service)
        oldRoot = projects.root()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        qt.QSettings().setValue(projects.ROOT_KEY, oldRoot)
        canvas = library.LibraryCanvas()
        running = self.autosave._service
        self.autosave._service = service
        try:
            canvas.widget.show()
            canvas.refresh()
            title, meta, folders = card(canvas)
            self.assertEqual(title, project.name)
            self.assertEqual(folders, [os.path.join(project.path, "Images")])
            # A move that succeeds: the folder line names the scan's new folder at once, not after the next scene event
            newRoot = os.path.normpath(os.path.join(self.tempDir, "card move", "A", "Slicer Studio"))
            service.moveRoot(newRoot, moveContent=True)
            movedPath = os.path.join(newRoot, projects.PROJECTS_DIR, project.name)
            self.assertEqual(os.path.dirname(volume.GetStorageNode().GetFileName()), os.path.join(movedPath, "Images"))
            title, meta, folders = waitFor(canvas, lambda state: state[2] == [os.path.join(movedPath, "Images")])
            self.assertEqual(folders, [os.path.join(movedPath, "Images")], "the card names the scan's new folder")
            self.assertEqual(title, project.name)
            # A move that fails partway: the project moved, the setting stays on the old folder
            realMove = projects.moveContent
            failedRoot = os.path.normpath(os.path.join(self.tempDir, "card move", "B", "Slicer Studio"))

            def failsAfterProjects(*args, **kwargs):
                realMove(*args, **kwargs)
                raise OSError(errno.ENOSPC, "No space left on device")

            with mock.patch.object(projects, "moveContent", side_effect=failsAfterProjects):
                with self.assertRaises(OSError):
                    service.moveRoot(failedRoot, moveContent=True)
            self.assertEqual(projects.root(), newRoot, "the setting is unchanged")
            movedPath = os.path.join(failedRoot, projects.PROJECTS_DIR, project.name)
            self.assertEqual(os.path.normpath(service.project.path), movedPath)
            title, meta, folders = waitFor(canvas, lambda state: state[2] == [os.path.join(movedPath, "Images")])
            self.assertEqual(folders, [os.path.join(movedPath, "Images")])
            self.assertEqual(title, project.name)
            self.assertIn("Projects \u203a " + project.name, meta)
            self.assertNotIn("Not in a project yet", meta)
        finally:
            self.autosave._service = running
            canvas.widget.hide()
            canvas.dispose()
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root
            self.io.backgroundWriter().drain(10.0)

    def test_70_tab_goes_through_the_project_cards_in_the_order_they_are_shown(self):
        """Qt's focus chain follows the creation order of the widgets, so the cards that a refresh built later (the card of
        the project that was just saved, which then moves to the first place; the cards after a new sort order) were
        reached after the links below the grid. Tab goes through the cards as they are shown, before those links.
        """
        from StudioLib import projects_ui

        projects = self.projects
        created = [projects.createProject(f"Case {number}") for number in range(1, 7)]
        parent = qt.QWidget()
        parent.resize(1000, 800)
        layout = qt.QVBoxLayout(parent)
        section = projects_ui.ProjectsSection()
        layout.addWidget(section.widget)
        # Created after the section, shown below it (like the links of the Library page)
        link = qt.QPushButton("Link below the cards", parent)
        layout.addWidget(link)
        layout.addStretch(1)

        def pump(seconds):
            end = time.time() + seconds
            while time.time() < end:
                slicer.app.processEvents()
                time.sleep(0.02)

        def stamp(project, offset):
            self.io.atomicWriteJson(projects.projectFile(project.path), {"name": project.name, "modified": projects._isoTime(time.time() + offset)})

        def shown():
            cards = section.cards()
            positions = {id(card): (card.mapTo(parent, qt.QPoint(0, 0)).y(), card.mapTo(parent, qt.QPoint(0, 0)).x()) for card in cards}
            return [str(card.property("studioProjectPath")) for card in sorted(cards, key=lambda card: positions[id(card)])]

        def tabbed():
            order = []
            widget = section.sortButton
            for _ in range(400):
                widget = widget.nextInFocusChain()
                if widget == section.sortButton:
                    break
                if widget.objectName == "studioProjectCard" and widget.window() == parent.window():
                    order.append(str(widget.property("studioProjectPath")))
                elif widget == link:
                    order.append("link")
            return order

        try:
            for number, project in enumerate(created):
                stamp(project, -60 * number)
            parent.show()
            section.refresh()
            pump(0.3)
            self.assertEqual(len(section.cards()), 6)
            self.assertGreater(len({card.mapTo(parent, qt.QPoint(0, 0)).y() for card in section.cards()}), 1, "the cards are on two rows")
            self.assertEqual(tabbed(), shown() + ["link"], "Tab goes through the cards as shown, then to the link")
            # The 3rd project is saved: its card is the only one that is built again and is shown first
            stamp(created[2], 120)
            section.refresh()
            pump(0.3)
            self.assertEqual(shown()[0], created[2].path)
            self.assertEqual(tabbed(), shown() + ["link"], "the card that was built again is reached first, as shown")
            # Another sort order only places the cards again
            section.setSortOrder("name")
            pump(0.3)
            self.assertEqual(shown(), [project.path for project in created])
            self.assertEqual(tabbed(), shown() + ["link"], "a new sort order changes the Tab order with the cards")
        finally:
            section.dispose()
            parent.deleteLater()

    def test_86_project_named_with_a_studio_marker_stays_listed_kept_and_keeps_its_scene(self):
        """A project renamed "Pilot.studio-copy", or named after a scan "visit.studio-copy.nii.gz", was hidden from the
        Library and deleted with all its data at the next start as an interrupted Duplicate; one named after
        "pilot.studio-part.nii.gz" or "pilot.studio-tmp-a.nii.gz" lost its scene file when it was opened, as a leftover of
        a save (BB-1). Names never get Studio's temporary name parts, and a project that already has one (named by an
        earlier version) is listed, kept at the start and keeps its scene file.
        """
        projects = self.projects
        for name in ("Pilot.studio-copy", "a.studio-part", "a.studio-tmp-1", "x.STUDIO-COPY", ".studio-copy"):
            sanitized = projects.sanitizeName(name)
            self.assertFalse([marker for marker in projects.TEMPORARY_MARKERS if marker in sanitized.lower()], sanitized)
        self.assertEqual(projects.sanitizeName("Pilot.studio-copy"), "Pilot studio-copy")
        self.assertEqual(projects.displayName("visit.studio-copy.nii.gz"), "visit studio-copy")
        self.assertEqual(projects.displayName("pilot.studio-part.nii.gz"), "pilot studio-part")
        self.assertEqual(projects.sanitizeName("my.studio"), "my.studio", "only the temporary name parts are changed")
        # Rename (a closed project, the open project) and a project named after a scan
        service = self._service()
        closed = projects.createProject("Baseline")
        self.assertEqual(os.path.basename(projects.renameFolder(closed.path, "Study.studio-copy")), "Study studio-copy")
        self._userVolume("visit.studio-copy", shape=(20, 24, 28))
        project = self._projectFor(service)
        self.assertEqual(project.name, "visit studio-copy")
        self._save(service)
        self.assertEqual(os.path.basename(service.renameProject("Followup.studio-part")), "Followup studio-part")
        slicer.mrmlScene.Clear(0)
        self.io.backgroundWriter().drain(10.0)
        # Projects named by an earlier version: listed, kept by the startup, and their scene file is kept when opened
        parent = os.path.dirname(project.path)
        legacy = {}
        for name in ("Pilot.studio-copy", "pilot.studio-part", "pilot.studio-tmp-a"):
            path = os.path.join(parent, name)
            shutil.copytree(os.path.join(parent, "Followup studio-part"), path, ignore=shutil.ignore_patterns(projects.LOCK_FILE))
            os.rename(os.path.join(path, "Followup studio-part.mrml"), os.path.join(path, name + ".mrml"))
            legacy[name] = path
        listed = [info.name for info in projects.listProjects()]
        for name in ("Study studio-copy", "Followup studio-part", *legacy):
            self.assertIn(name, listed)
        self.assertEqual(projects.removeCopyLeftovers(), [])
        service.removeInterruptedCopies()
        self.assertTrue(self.io.backgroundWriter().drain(30.0))
        for name, path in legacy.items():
            self.assertTrue(os.path.isfile(os.path.join(path, name + ".mrml")), f"{name} is kept at the start")
            self.assertEqual(projects.temporaryLeftovers(path), [], f"the scene file of {name} is not a leftover")
        for name, path in legacy.items():
            self.assertTrue(service.openProject(path), name)
            self.io.backgroundWriter().drain(10.0)
            self.assertTrue(os.path.isfile(os.path.join(path, name + ".mrml")), f"opening {name} keeps its scene file")
            self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 1, f"{name} opens with its scan")
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)

    def test_87_project_killed_before_its_first_save_is_deleted_not_offered_as_saved(self):
        """A project folder is made when the first scan loads, and its first save takes seconds. A kill (or a full disk)
        in between left only .studio/project.json (state "open") and a .studio-part file: the crash offer said "Your
        project X was saved at <time>", its Open project only toasted "has no scene file", and the Library listed an
        "Empty" card that never opened and was never removed (BB-1). The startup deletes such a folder and never offers a
        folder without a scene; a folder with anything of the user's (a scene, a saved time, a scan) is kept.
        """
        projects = self.projects
        service = self._service()
        offered = []
        service.setProjectPresenter(offered.append)

        def killedFirstSave(name, **extra):
            project = projects.createProject(name)  # project.json state "open", no savedAt
            with open(os.path.join(project.path, ".studio", "lock"), "w") as f:
                f.write("999999\nSlicer\n" + qt.QSysInfo.machineHostName() + "\n")
            os.makedirs(os.path.join(project.path, "Images"))
            with open(os.path.join(project.path, "Images", "MRHead.nrrd.studio-part"), "wb") as f:
                f.write(b"\1" * 1024)
            return project.path

        gone = killedFirstSave("Killed first save")
        goneJson = killedFirstSave("Killed json part")
        os.remove(os.path.join(goneJson, "Images", "MRHead.nrrd.studio-part"))
        os.rmdir(os.path.join(goneJson, "Images"))
        with open(os.path.join(goneJson, ".studio", "project.json.1234.part"), "w") as f:
            f.write("{")
        # Kept: a scan of the user's in the folder, a saved time, a scene file, a project that is open in a live window
        withScan = killedFirstSave("Has a scan")
        with open(os.path.join(withScan, "Images", "MRHead.nrrd"), "wb") as f:
            f.write(b"\1" * 1024)
        saved = killedFirstSave("Saved once")
        data = projects.readJson(projects.projectFile(saved))
        data["savedAt"] = "2026-11-01T10:00:00Z"
        self.io.atomicWriteJson(projects.projectFile(saved), data)
        withScene = killedFirstSave("Has a scene")
        with open(os.path.join(withScene, "Has a scene.mrml"), "w") as f:
            f.write("<MRML/>")
        busy = projects.createProject("Busy first save")
        self.assertTrue(busy.lock())
        try:
            self.assertEqual(sorted(info.name for info in projects.listProjects() if info.contentLine() == "Empty"),
                             sorted(["Killed first save", "Killed json part", "Has a scan", "Saved once", "Busy first save"]),
                             "before: the Library lists them as Empty projects")
            self.assertTrue(projects.isNeverSaved(gone) and projects.isNeverSaved(goneJson))
            self.assertFalse(any(projects.isNeverSaved(path) for path in (withScan, saved, withScene)))
            service.removeUnsavedProjects()
            self.assertFalse(os.path.exists(gone), "the folder that never got a scene is deleted")
            self.assertFalse(os.path.exists(goneJson))
            for path in (withScan, saved, withScene, busy.path):
                self.assertTrue(os.path.isdir(path), os.path.basename(path))
            self.assertEqual(os.path.isfile(os.path.join(withScan, "Images", "MRHead.nrrd")), True)
            crashed = service.scanProjectsAtStartup()
            self.assertEqual([info.name for info in crashed], ["Has a scene"], "only a project with a scene is offered")
            self.assertEqual(len(offered), 1)
            self.assertEqual([info.name for info in offered[0]], ["Has a scene"])
        finally:
            busy.unlock()
        # The next start: nothing is left of the killed first save, and nothing is offered again
        self.assertEqual(projects.removeUnsavedProjects(), ["Busy first save"], "its window is gone now: nothing of it was saved")
        self.assertEqual(sorted(info.name for info in projects.listProjects()),
                         ["Has a scan", "Has a scene", "Saved once"])
        # The project that is open in this process and has no scene yet is never deleted
        current = projects.createProject("First save pending")
        self.assertTrue(current.lock())
        projects.setCurrentProject(current)
        try:
            self.assertEqual(projects.removeUnsavedProjects(), [])
            self.assertTrue(os.path.isdir(current.path))
        finally:
            projects.setCurrentProject(None)
            current.unlock()


    def test_88_chooser_uses_a_studio_folder_without_projects_as_is(self):
        """Choose… on a Slicer Studio folder that has no Projects/ yet (only "About this folder.txt", studies, templates or
        LNM/ folders: subfolders are made only when something is written there) nested a "Slicer Studio" folder inside it,
        and its studies dropped out of the Library (BB-1). Such a folder is used as is, in the first-start chooser and in
        Change… of Settings, Saving; a folder of the user's own files still gets a "Slicer Studio" folder inside it.
        """
        from StudioLib import projects_ui

        projects = self.projects

        def folderWith(name, *files):
            folder = os.path.join(self.tempDir, name)
            os.makedirs(folder)
            for relative in files:
                path = os.path.join(folder, *relative.split("/"))
                if relative.endswith("/"):
                    os.makedirs(path, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("{}")
            return os.path.normpath(folder)

        studioFolders = [
            folderWith("LabLNM", projects.ABOUT_FILE, "Studies/Aphasia cohort/study.json", "LNM/"),
            folderWith("Lab about only", projects.ABOUT_FILE),
            folderWith("Lab studies only", "Studies/Aphasia cohort/study.json"),
            folderWith("Lab templates only", "Templates/my-mni/template.json"),
            folderWith("Lab LNM only", "LNM/normalization/sub-01/"),
            folderWith("Lab projects", "Projects/"),
        ]
        userFolders = [
            folderWith("My documents", "notes.txt"),
            folderWith("My research", "LNM/scripts.py", "Studies/paper.docx", "Templates/letter.dotx"),
        ]
        realGetExistingDirectory = qt.QFileDialog.getExistingDirectory
        picked = []
        qt.QFileDialog.getExistingDirectory = staticmethod(lambda *args, **kwargs: picked[-1])
        try:
            for cancellable in (False, True):
                for folder in studioFolders:
                    picked.append(folder)
                    dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Slicer Studio"), cancellable=cancellable)
                    try:
                        dialog.choose()
                        self.assertEqual(os.path.normpath(dialog.path), folder, f"used as is: {os.path.basename(folder)}")
                    finally:
                        dialog.deleteLater()
                for folder in userFolders:
                    picked.append(folder)
                    dialog = projects_ui.FolderChooserDialog(proposal=os.path.join(self.tempDir, "Slicer Studio"), cancellable=cancellable)
                    try:
                        dialog.choose()
                        self.assertEqual(os.path.normpath(dialog.path), os.path.join(folder, projects.ROOT_FOLDER_NAME),
                                         f"a Slicer Studio folder inside: {os.path.basename(folder)}")
                    finally:
                        dialog.deleteLater()
        finally:
            qt.QFileDialog.getExistingDirectory = realGetExistingDirectory
            slicer.app.processEvents()
        # The study of the folder chosen as is is listed there
        self.assertEqual([os.path.basename(path) for path in projects.listStudies(studioFolders[0])], ["Aphasia cohort"])
        self.assertTrue(projects.isStudioFolder(os.path.join(self.tempDir, "new", projects.ROOT_FOLDER_NAME)), "by its name")
        self.assertFalse(projects.isStudioFolder(""))

    def test_89_second_window_leaves_a_running_move_alone(self):
        """A second Slicer Studio started while the first one moves the Slicer Studio folder (Change… > Move, to another
        volume) took the move's Studio/Projects/MovePending record for an interrupted move (BB-1): it ran the same move at
        the same time, deleted the first window's partial copies (whose move then failed), said "Slicer Studio was closed
        while it moved your projects", and offered the project the first window had open (unlocked while it moves) as
        crashed. A running move now holds a lock file (``projects.takeMoveLock``) until the setting points at the new
        folder: the second window leaves that move alone and says it runs in another window, offers no crashed project
        from the folder that moves, and refuses to move the folder itself. Once that process is gone (killed), the
        record is an interrupted move again and the next start finishes it.
        """
        import subprocess

        projects = self.projects
        if not shutil.which("sleep"):
            self.skipTest("needs the sleep program")
        oldRoot = os.path.normpath(os.path.join(self.tempDir, "A", "Slicer Studio"))
        newRoot = os.path.normpath(os.path.join(self.tempDir, "B", "My projects"))
        folder = os.path.join(oldRoot, projects.PROJECTS_DIR, "MRHead")
        os.makedirs(os.path.join(folder, "Images"))
        os.makedirs(os.path.join(folder, projects.PRIVATE_DIR))
        with open(os.path.join(folder, "Images", "MRHead.nrrd"), "wb") as f:
            f.write(os.urandom(4096))
        with open(os.path.join(folder, "MRHead" + projects.SCENE_EXTENSION), "w") as f:
            f.write('<MRML><VolumeArchetypeStorage fileName="Images/MRHead.nrrd"/></MRML>\n')
        with open(projects.projectFile(folder), "w") as f:
            json.dump({"name": "MRHead", "state": "open"}, f)  # open in the window that moves it, and unlocked for the move
        os.makedirs(newRoot)  # the move has begun: the new folder exists
        lockPath = projects.moveLockPath()
        self.assertTrue(lockPath, "the move lock has a place")
        os.makedirs(os.path.dirname(lockPath), exist_ok=True)

        # The lock file that the moving window holds: what Qt writes for this process, with the process id of another live
        # process of this program (a copy of "sleep" under this program's name, as Qt checks the name of the process)
        probe = qt.QLockFile(os.path.join(self.tempDir, "probe.lock"))
        self.assertTrue(probe.tryLock(0))
        with open(os.path.join(self.tempDir, "probe.lock"), encoding="utf-8") as f:
            lines = f.read().split("\n")
        probe.unlock()
        self.assertEqual(int(lines[0]), os.getpid())
        program = os.path.join(self.tempDir, "other", lines[1] or "SlicerApp-real")
        os.makedirs(os.path.dirname(program))
        shutil.copy2(shutil.which("sleep"), program)
        other = subprocess.Popen([program, "600"])  # the other Slicer Studio window, moving the folder

        def holdLock(pid):
            with open(lockPath, "w", encoding="utf-8") as f:
                f.write("\n".join([str(pid)] + lines[1:]))

        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        try:
            projects.recordPendingMove(oldRoot, newRoot)
            holdLock(other.pid)
            self.assertTrue(projects.isMoveRunningElsewhere())
            service = self._service()
            offered = []
            service.setProjectPresenter(offered.append)
            notices = []
            moves = []
            result = service.finishInterruptedMove(run=moves.append,
                                                   notify=lambda text, level: notices.append((level, text)))
            self.assertEqual(result, "running", "a running move is not an interrupted one")
            self.assertEqual(moves, [], "nothing is moved from here")
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0][0], "info")
            self.assertIn("in another window", notices[0][1])
            self.assertNotIn("was closed", notices[0][1])
            self.assertEqual(projects.root(), oldRoot, "the setting is the moving window's to change")
            self.assertEqual(projects.pendingMove(), (oldRoot, newRoot), "the record stays")
            self.assertTrue(os.path.isfile(os.path.join(folder, "Images", "MRHead.nrrd")), "nothing is moved")
            self.assertEqual(os.listdir(newRoot), [])
            self.assertEqual(service.scanProjectsAtStartup(), [], "the project that moves is not offered as crashed")
            self.assertEqual(offered, [])
            self.io.backgroundWriter().drain(30.0)
            self.assertEqual(projects.readProjectJson(folder).get("state"), "open", "not marked crashed")
            self.assertEqual(service.removeUnsavedProjects(), [])
            # Change… here while the other window moves: refused before anything is done, with a plain message
            with self.assertRaises(projects.OpenElsewhereError) as context:
                service.moveRoot(newRoot, moveContent=True)
            self.assertIsInstance(context.exception, projects.MoveRunningElsewhereError)
            self.assertIn("in another window", str(context.exception))
            self.assertEqual(projects.root(), oldRoot)
            self.assertEqual(projects.pendingMove(), (oldRoot, newRoot))
            self.assertTrue(os.path.exists(lockPath), "the other window's lock is kept")

            # The moving window is killed: its record is an interrupted move, finished at the next start
            other.kill()
            other.wait()
            self.assertFalse(projects.isMoveRunningElsewhere())
            during = []

            def run(function):
                # This window's own move holds the lock, and is not "elsewhere" for itself
                during.append((os.path.exists(lockPath), projects.isMoveRunningElsewhere()))
                return function()

            restarted = self._service()
            restarted.setProjectPresenter(offered.append)
            notices = []
            result = restarted.finishInterruptedMove(run=run, notify=lambda text, level: notices.append((level, text)))
            self.assertEqual(result, "finished")
            self.assertEqual(during, [(True, False)])
            self.assertFalse(os.path.exists(lockPath), "released once the setting points at the new folder")
            self.assertEqual(projects.root(), newRoot)
            self.assertIsNone(projects.pendingMove())
            self.assertEqual([info.name for info in restarted.scanProjectsAtStartup()], ["MRHead"], "a project left open is offered now")
            self.io.backgroundWriter().drain(30.0)
        finally:
            if other.poll() is None:
                other.kill()
                other.wait()
            projects.releaseMoveLock()
            with contextlib.suppress(OSError):
                os.remove(lockPath)
            projects.clearPendingMove()

    def test_90_chosen_folder_on_a_drive_that_is_not_connected_is_not_created_again(self):
        """The chosen Slicer Studio folder is on a drive that is not connected now (BB-1): Library looked empty with no word
        why ("Open a scan or start on the template"), and opening a scan created a new, empty "Slicer Studio/Projects/<name>"
        at the missing path, so projects were split between two folders once the drive was back. The folder is not created
        again by itself: no project is made (autosave is paused, told once), Library and Show in Files say that the folder
        cannot be found, and the first project is made in it once the drive is connected again. A folder that the user
        chooses (the chooser, Change…) or that the environment sets is still created.
        """
        from StudioLib import projects_ui

        projects = self.projects
        drive = os.path.join(self.tempDir, "USB")
        unplugged = os.path.join(self.tempDir, "USB-unplugged")
        chosenRoot = os.path.normpath(os.path.join(drive, "Slicer Studio"))
        settings = qt.QSettings()
        saved = {key: settings.value(key) for key in (projects.ROOT_KEY, projects.ROOT_CHOSEN_KEY)}
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, chosenRoot)
        settings.setValue(projects.ROOT_CHOSEN_KEY, True)
        toasts = []
        shown = []
        originalMessage, originalShow = projects_ui.message, projects.showInFileManager
        projects_ui.message = lambda text, **kwargs: toasts.append((str(text), kwargs))
        projects.showInFileManager = lambda path: shown.append(path) or True
        section = None
        try:
            os.makedirs(chosenRoot)  # made by the chooser, on the drive
            self.assertFalse(projects.isRootMissing())
            projects.createProject("MRHead")  # the folder as it was, the drive connected
            os.rename(drive, unplugged)  # the drive is unplugged
            self.assertTrue(projects.isRootMissing())
            self.assertFalse(projects.isRootMissing(os.path.join(self.tempDir, "elsewhere")), "only the chosen folder")
            with self.assertRaises(projects.RootMissingError):
                projects.ensureRoot()
            with self.assertRaises(projects.RootMissingError):
                projects.createProject("New")
            self.assertFalse(os.path.exists(drive), "nothing is created at the missing path")

            # Opening a scan and editing: no project in a new, empty folder; the user is told once
            service = self._service()
            announced = []
            service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))
            self._userVolume()
            for edit in range(3):
                slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", f"F{edit}")
                service.poll()
            self.assertIsNone(service.project)
            self.assertFalse(os.path.exists(drive), "the folder is not created again by the autosave")
            self.assertEqual([event for event, _info in announced], ["root-failed"])
            self.assertTrue(announced[0][1].get("missing"))
            status = service.status()
            self.assertEqual(status.state, "paused")
            self.assertIn("cannot be found", status.message)
            self.assertFalse(service.rootUnwritable(), "not told as a folder that cannot be written")
            projects_ui.announce("root-failed", None, reason=None, missing=True)
            self.assertEqual(len(toasts), 1)
            self.assertIn("cannot be found", toasts[0][0])
            self.assertIn("Connect the drive", toasts[0][0])
            self.assertEqual(toasts[0][1].get("actionText"), "Change folder…")

            # Library: the section says that the folder cannot be found, not "Open a scan…"
            section = projects_ui.ProjectsSection()
            section.refresh()
            self.assertFalse(section.emptyLabel.isHidden())
            self.assertIn("cannot be found", section.emptyLabel.text)
            self.assertIn("Connect the drive", section.emptyLabel.text)
            self.assertIn("cannot be found", section.captionText())
            # Show in Files tells it too, and does not create the folder
            toasts.clear()
            self.assertFalse(projects_ui.showRootFolder())
            self.assertFalse(projects_ui.showProjectFolder())
            self.assertEqual(shown, [])
            self.assertEqual(len(toasts), 2)
            self.assertIn("cannot be found", toasts[0][0])
            self.assertFalse(os.path.exists(drive))

            # The drive is connected again: the projects are listed, and the first project is made there
            os.rename(unplugged, drive)
            self.assertFalse(projects.isRootMissing())
            section.refresh()
            self.assertEqual([os.path.basename(card.property("studioProjectPath")) for card in section.cards()], ["MRHead"])
            self.assertNotIn("cannot be found", section.captionText())
            self.assertEqual(section.emptyLabel.text, projects_ui._emptyText())
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Later")
            service.poll()
            self.assertIsNotNone(service.project)
            self.assertEqual(os.path.dirname(service.project.path), os.path.join(chosenRoot, projects.PROJECTS_DIR))
            self.assertFalse(service.rootUnwritable())
            service.releaseProject()

            # A folder that the user chooses again (the chooser, Change…) is created; the environment's folder always is
            os.rename(drive, unplugged)
            self.assertEqual(projects.ensureRoot(chosenRoot, recreate=True), chosenRoot)
            self.assertTrue(os.path.isdir(chosenRoot))
            environmentRoot = os.path.join(self.tempDir, "from the environment")
            os.environ[projects.ROOT_ENVIRONMENT] = environmentRoot
            self.assertFalse(projects.isRootMissing())
            projects.ensureRoot()
            self.assertTrue(os.path.isdir(environmentRoot))
        finally:
            projects_ui.message, projects.showInFileManager = originalMessage, originalShow
            if section is not None:
                section.dispose()
                section.widget.deleteLater()
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)

    def test_91_second_click_of_a_double_click_on_a_card_does_not_open_it_again(self):
        """A real double click is handled when the first click's open has loaded the project: Qt then reports the second
        press as a double click and its release must not queue another open, which would run after what the user did in
        between (Ctrl+N closes the project, the queued open reopens it) (BB-1).
        """
        from StudioLib import projects_ui

        class Section:
            widget = None

            def __init__(self):
                self.opened = []

            def open(self, path):
                self.opened.append(path)

            def onShown(self):
                pass

            def updateColumns(self):
                pass

            def elideCard(self, card):
                pass

        def send(widget, kind, button=qt.Qt.LeftButton):
            pos = qt.QPoint(5, 5)
            event = qt.QMouseEvent(kind, qt.QPointF(pos), qt.QPointF(widget.mapToGlobal(pos)), button, button, qt.Qt.NoModifier)
            qt.QApplication.sendEvent(widget, event)

        def realDoubleClick(widget):
            # What Qt delivers for a double click: press, release, double click (in place of the second press), release
            for kind in (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonRelease, qt.QEvent.MouseButtonDblClick, qt.QEvent.MouseButtonRelease):
                send(widget, kind)

        for filterClass, key in ((projects_ui._ClickFilter, "studioProjectPath"), (projects_ui._StudyClickFilter, "studioStudyPath")):
            section = Section()
            clickFilter = filterClass(section)
            card = qt.QWidget()
            card.setProperty(key, "/projects/A")
            card.installEventFilter(clickFilter)
            card.resize(40, 30)
            card.show()
            try:
                slicer.app.processEvents()
                # One click opens the card (once)
                send(card, qt.QEvent.MouseButtonPress)
                send(card, qt.QEvent.MouseButtonRelease)
                slicer.app.processEvents()
                self.assertEqual(section.opened, ["/projects/A"], filterClass.__name__)
                # A double click opens it once: the release of the second click queues nothing
                section.opened.clear()
                realDoubleClick(card)
                slicer.app.processEvents()
                self.assertEqual(section.opened, ["/projects/A"], f"{filterClass.__name__}: a double click opens the card once")
                # The next click (and the next double click) are not swallowed
                section.opened.clear()
                send(card, qt.QEvent.MouseButtonPress)
                send(card, qt.QEvent.MouseButtonRelease)
                slicer.app.processEvents()
                self.assertEqual(section.opened, ["/projects/A"], f"{filterClass.__name__}: the click after a double click opens")
                section.opened.clear()
                realDoubleClick(card)
                slicer.app.processEvents()
                self.assertEqual(len(section.opened), 1)
            finally:
                card.removeEventFilter(clickFilter)
                card.hide()
                card.deleteLater()

    def test_92_damaged_outside_scene_leaves_the_open_project_open_with_one_message(self):
        """A scene file from outside the Slicer Studio folder that was cut short, opened in Library while a project is open (or
        with none open), is refused before anything is replaced: the open project stays open (it was saved and closed first,
        and the toast said that nothing was opened), and the user gets the one toast, with no "Slicer error" dialog and no
        logged traceback on top of it (BB-1).
        """
        from unittest import mock

        from StudioLib import importer, projects_ui, shell

        service = self._service()
        service.setProjectAnnouncer(projects_ui.announce)
        running = self.autosave._service
        self.autosave._service = service
        outside = os.path.join(self.tempDir, "damaged outside")
        os.makedirs(outside)
        brokenPath = os.path.join(outside, "Brain scene.mrml")
        damaged = '<?xml version="1.0" encoding="UTF-8"?>\n<MRML  version="Slicer4.4.0" userTags="">\n <Selection\n  id="vtkMRMLSelectionNodeSingleton" name="Sele'
        with open(brokenPath, "w", encoding="utf-8") as file:
            file.write(damaged)
        studioShell = shell.Shell(force=True)
        try:
            studioShell.apply()
            slicer.app.processEvents()
            for withProject in (False, True):
                slicer.mrmlScene.Clear(0)
                self.io.backgroundWriter().drain(10.0)
                self.assertIsNone(service.project)
                if withProject:
                    self._userVolume("T1", shape=(10, 11, 12), extension=".nrrd")
                    project = self._projectFor(service)
                    self._save(service)
                    self.assertTrue(service.opensSceneAsProject())
                shown = []
                errors = []
                with mock.patch.object(slicer.util, "errorDisplay", lambda text, *a, into=shown, **k: into.append(text)), \
                        mock.patch.object(importer.logging, "error", lambda text, *a, into=errors, **k: into.append(text)):
                    result = importer.loadFiles([brokenPath], studioShell)
                    slicer.app.processEvents()
                label = "with a project open" if withProject else "with nothing open"
                self.assertEqual(shown, [], f"{label}: no error dialog on top of the toast")
                self.assertEqual(errors, [], f"{label}: no error and traceback logged")
                self.assertEqual(result.errors, [])
                self.assertEqual(result.scenes, [])
                self.assertIn("damaged and could not be read", studioShell.toastText(), label)
                if withProject:
                    self.assertIsNotNone(service.project, "the open project stays open")
                    self.assertEqual(service.project.path, project.path)
                    self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("T1"), "its data is still in the scene")
                with open(brokenPath, encoding="utf-8") as file:
                    self.assertEqual(file.read(), damaged, "the damaged file is left as it is")
        finally:
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()

    def test_93_same_name_taken_by_another_window_between_choice_and_creation_gets_the_next_name(self):
        """Two windows that create a project of the same name at the same moment: the one that loses the race for the folder
        gets "Name 2" and a project, not "Autosave paused: the Slicer Studio folder cannot be written (File exists)" (BB-1).
        The other window is simulated: it makes the folder right after this one has chosen the name.
        """
        service = self._service()
        projects = self.projects
        folder = projects.projectsFolder(projects.ensureRoot())
        real = projects.uniqueName
        raced = []

        def uniqueNameThenOtherWindowCreatesIt(*args, **kwargs):
            name = real(*args, **kwargs)
            if len(raced) < 2:  # lost twice in a row: "Race", then "Race 2" are taken in between
                raced.append(name)
                os.makedirs(os.path.join(folder, name, projects.PRIVATE_DIR))
            return name

        from unittest import mock

        with mock.patch.object(projects, "uniqueName", uniqueNameThenOtherWindowCreatesIt):
            project = service.createProject("Race", announce=None)
        self.assertEqual(raced, ["Race", "Race 2"])
        self.assertIsNotNone(project, "a project is created, not an unwritable-folder pause")
        self.assertEqual(os.path.basename(project.path), "Race 3")
        self.assertTrue(os.path.isdir(projects.privateFolder(project.path)))
        self.assertNotEqual(service._status.state, "paused", "autosave is not paused")
        self.assertEqual(sorted(os.listdir(folder)), ["Race", "Race 2", "Race 3"])
        # The projects of the other window were left as they were
        self.assertEqual(os.listdir(os.path.join(folder, "Race")), [projects.PRIVATE_DIR])

    def test_94_library_inspector_project_card_follows_the_folder_after_a_root_move(self):
        """Change folder > Move: the project card of the Library inspector shows the open project's new folder (BB-1).
        It showed "Projects > <name>" with the tool tip of the old folder, which no longer exists, until the next
        workspace switch: the card was rebuilt only for scene events and when the panel was shown, and the move changes
        neither.
        """
        from unittest import mock

        from StudioLib import library

        projects = self.projects
        service = self._service()
        volume, _path = self._userVolume()
        self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        name = project.name
        oldPath = os.path.normpath(project.path)
        newRoot = os.path.join(self.tempDir, "card-move", "Slicer Studio")
        os.makedirs(newRoot)
        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        settings.setValue(projects.ROOT_KEY, self.root)
        panel = None
        try:
            with mock.patch.object(library.LibraryPanel, "withCanvas", staticmethod(lambda: True)):
                panel = library.LibraryPanel()
                panel.widget.show()
                panel.build()
                _pumpFor(0.2)
                # The move may also touch the scene: only the notifications of the project are to refresh the card here
                panel._sceneRefresher.setEnabled(False)
                card = panel.overviewCard
                self.assertEqual(_plainToolTip(card.pathLabel), oldPath)
                service.moveRoot(newRoot, moveContent=True)
                newPath = os.path.join(os.path.normpath(newRoot), projects.PROJECTS_DIR, os.path.basename(service.project.path))
                self.assertEqual(os.path.normpath(service.project.path), newPath)
                self.assertFalse(os.path.isdir(oldPath))
                _pumpFor(0.4)
                self.assertEqual(_plainToolTip(card.pathLabel), newPath, "the card shows the new folder")
                self.assertIn(service.project.name, str(card.pathLabel.property("fullText")))
        finally:
            if panel is not None:
                panel.widget.hide()
                panel._disposeProjectListener()
            settings.remove(projects.ROOT_KEY)
            settings.remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_95_leaving_a_project_that_was_only_looked_at_saves_nothing(self):
        """Opening a project and leaving it without an edit (New project, opening another project, quitting) rewrote its
        scene file and moved "Last edited" to now (BB-1), so a project that was only looked at went to the top of Library.
        The save before leaving counted what the scene does by itself after an open (the hierarchy made again, the views'
        slice models) as edits; it now goes by what the periodic autosave goes by (test_64), and New project asks nothing.
        An edit made just before leaving (a paint stroke, a rename, a moved point) is still saved.
        """
        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        self._lesion(volume)
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Peaks")
        points.AddControlPoint((1.0, 2.0, 3.0))
        project = self._projectFor(service)
        self._save(service)
        projectJson = os.path.join(project.path, ".studio", "project.json")
        lesionFile = os.path.join(project.path, "Segmentations", "Lesions.seg.nrrd")
        pointsFile = os.path.join(project.path, "Markups", "Peaks.mrk.json")
        asked = []

        def closeQuestion():
            dialog = slicer.app.activeModalWidget()
            if dialog is not None:
                asked.append(str(dialog.windowTitle))
                dialog.reject()

        timer = qt.QTimer()
        timer.setInterval(200)
        timer.connect("timeout()", closeQuestion)

        def onDisk():
            with open(project.scenePath, encoding="utf-8") as f:
                sceneText = f.read()
            return {"modified": self.projects.readJson(projectJson).get("modified"), "sceneTime": os.path.getmtime(project.scenePath),
                    "scene": sceneText, "lesion": _md5File(lesionFile), "points": _md5File(pointsFile)}

        def look():
            """Open the project as a card click does and only look at it: the scene then changes by itself."""
            slicer.mrmlScene.Clear(0)
            self.io.backgroundWriter().drain(10.0)
            self.assertTrue(service.openProject(project.path))
            scene = slicer.mrmlScene
            # The views show: their slice models (not saved with the scene) appear, and the slice views fit their size
            model = scene.CreateNodeByClass("vtkMRMLModelNode")
            model.UnRegister(None)  # a new reference: the scene keeps it
            model.SetName("Red Volume Slice")
            model.SetSaveWithScene(False)
            model.SetHideFromEditors(True)
            scene.AddNode(model)
            sliceNode = scene.GetFirstNodeByClass("vtkMRMLSliceNode")
            if sliceNode is not None:
                sliceNode.SetFieldOfView(321.0, 123.0, 1.0)
            # The hierarchy of the scene file is merged after the load (its items are made again with new numbers)
            slicer.vtkMRMLSubjectHierarchyNode.GetSubjectHierarchyNode(scene).Modified()
            self.assertTrue(scene.GetModifiedSinceRead(), "the scene changed by itself since it was read")
            self.assertFalse(service.projectNeedsSave(includeScene=True), "a project that was only looked at is up to date")
            return scene

        def leave(how):
            time.sleep(1.1)  # "Last edited" has whole seconds
            timer.start()
            try:
                if how == "new project":
                    service.newProject()  # Ctrl+N: the project is saved if needed, then the scene is closed
                    self.assertIsNone(service.project)
                    self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLSegmentationNode"), 0, "the scene was closed")
                elif how == "another project":
                    slicer.mrmlScene.Clear(0)  # opening another project's card closes the scene of this one first
                    self.assertIsNone(service.project)
                else:
                    self.assertTrue(service.flushForClose(), "quitting: the project is up to date before the close prompt")
            finally:
                timer.stop()
            self.io.backgroundWriter().drain(30.0)
            self.assertEqual(asked, [], f"{how}: nothing is asked")
            return onDisk()

        before = onDisk()
        for how in ("new project", "another project", "quit"):
            look()
            self.assertEqual(leave(how), before, f"{how}: the scene file and Last edited are as they were")

        # An edit made just before leaving is saved
        scene = look()
        lesion = scene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
        scan = scene.GetFirstNodeByName("Scan")
        segmentId = lesion.GetSegmentation().GetNthSegmentID(0)
        painted = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, segmentId, scan)
        lesionVoxels = int(painted.sum())
        painted[1:3, 1:3, 1:3] = 1  # a paint stroke
        slicer.util.updateSegmentBinaryLabelmapFromArray(painted, lesion, segmentId, scan)
        after = leave("new project")
        self.assertNotEqual(after["lesion"], before["lesion"], "the paint stroke is saved")
        self.assertNotEqual(after["modified"], before["modified"], "Last edited moves")
        before = after
        scene = look()
        scene.GetFirstNodeByName("Scan").SetName("Scan renamed")
        after = leave("quit")
        self.assertIn('name="Scan renamed"', after["scene"], "the rename is saved")
        self.assertNotEqual(after["modified"], before["modified"], "Last edited moves")
        before = after
        scene = look()
        scene.GetFirstNodeByName("Peaks").SetNthControlPointPosition(0, 4.0, 5.0, 6.0)
        after = leave("new project")
        self.assertNotEqual(after["points"], before["points"], "the moved point is saved")
        self.assertNotEqual(after["modified"], before["modified"], "Last edited moves")
        # The project opens with the three edits
        self.assertTrue(service.openProject(project.path))
        scene = slicer.mrmlScene
        scan = scene.GetFirstNodeByName("Scan renamed")
        self.assertIsNotNone(scan)
        lesion = scene.GetFirstNodeByClass("vtkMRMLSegmentationNode")
        reopened = slicer.util.arrayFromSegmentBinaryLabelmap(lesion, lesion.GetSegmentation().GetNthSegmentID(0), scan)
        self.assertEqual(int(reopened.sum()), lesionVoxels + 8)
        position = [0.0, 0.0, 0.0]
        scene.GetFirstNodeByName("Peaks").GetNthControlPointPosition(0, position)
        self.assertEqual([round(value, 3) for value in position], [4.0, 5.0, 6.0])

    def test_96_change_folder_refuses_a_folder_that_cannot_be_written(self):
        """Settings > Saving > Change… to a folder that cannot be made or written (a read-only drive) is refused before the
        setting changes, as the first-start chooser does (BB-1). The folder was stored first and its error then reported as
        "Your projects could not all be moved … the folder setting is unchanged": the setting had changed, Library lost the
        projects that were kept in the old folder and autosave paused for every new scan.
        """
        from unittest import mock

        from StudioLib import projects_ui

        projects = self.projects
        settings = qt.QSettings()
        os.environ.pop(projects.ROOT_ENVIRONMENT)
        oldRoot = os.path.normpath(self.root)
        settings.setValue(projects.ROOT_KEY, oldRoot)
        os.makedirs(oldRoot, exist_ok=True)
        # A file where the new folder's parent should be: it cannot be made, even by an administrator (as on a read-only drive)
        blocker = os.path.join(self.tempDir, "a file")
        with open(blocker, "w") as f:
            f.write("not a folder")
        blocked = os.path.join(blocker, projects.ROOT_FOLDER_NAME)
        # A folder that exists but cannot be written
        readOnly = os.path.join(self.tempDir, "read-only", projects.ROOT_FOLDER_NAME)
        os.makedirs(readOnly)
        realAccess = os.access

        def access(path, mode, *args, **kwargs):
            if os.path.normpath(str(path)) == os.path.normpath(readOnly) and mode & os.W_OK:
                return False
            return realAccess(path, mode, *args, **kwargs)

        template = os.path.join(oldRoot, projects.TEMPLATES_DIR, "Kept")

        def change(newRoot, answer, service):
            shown = []

            class FakeChooser:
                path = newRoot
                continueButton = mock.Mock()
                setWindowTitle = mock.Mock()
                choose = mock.Mock()
                exec = mock.Mock()
                exec_ = mock.Mock()
                deleteLater = mock.Mock()

                def __init__(self, *args, **kwargs):
                    pass

            running = self.autosave._service
            self.autosave._service = service
            try:
                with mock.patch.object(projects_ui, "FolderChooserDialog", FakeChooser), \
                        mock.patch.object(projects_ui, "_question", lambda *args, **kwargs: answer), \
                        mock.patch.object(projects_ui, "_runWithProgress", lambda function, *args, **kwargs: function()), \
                        mock.patch.object(projects_ui, "_notice", lambda parent, title, text: shown.append(text)), \
                        mock.patch.object(slicer.util, "infoDisplay", lambda text, *a, **k: shown.append(text)), \
                        mock.patch.object(slicer.util, "errorDisplay", lambda text, *a, **k: shown.append(text)), \
                        mock.patch.object(os, "access", access):
                    return projects_ui.changeRootDialog(None), shown
            finally:
                self.autosave._service = running

        try:
            service = self._service()
            # Empty old folder (no question), kept projects ("Keep them where they are") and Move; with and without the service
            for keep, answer in ((False, None), (True, 1), (True, 0)):
                if keep:
                    os.makedirs(template, exist_ok=True)
                    with open(os.path.join(template, "template.json"), "w") as f:
                        f.write("{}")
                for newRoot in (blocked, readOnly):
                    for running in (service, None):
                        case = f"keep={keep} answer={answer} {os.path.basename(os.path.dirname(newRoot))} service={running is not None}"
                        result, shown = change(newRoot, answer, running)
                        self.assertIsNone(result, case)
                        self.assertEqual(projects.root(), oldRoot, case)
                        self.assertEqual(os.path.normpath(str(qt.QSettings().value(projects.ROOT_KEY))), oldRoot, case)
                        self.assertEqual(len(shown), 1, (case, shown))
                        self.assertIn("cannot be used", shown[0], case)
                        self.assertIn("Choose another folder", shown[0], case)
                        self.assertNotIn("could not all be moved", shown[0], case)
                        self.assertIsNone(projects.pendingMove(), case)
                        if keep:
                            self.assertTrue(os.path.isfile(os.path.join(template, "template.json")), case)
                            self.assertFalse(os.path.exists(os.path.join(readOnly, projects.TEMPLATES_DIR)), case)
            # A folder that can be written is still used
            usable = os.path.join(self.tempDir, "usable", projects.ROOT_FOLDER_NAME)
            result, shown = change(usable, 1, service)
            self.assertEqual(result, usable)
            self.assertEqual(shown, [])
            self.assertEqual(projects.root(), os.path.normpath(usable))
            self.assertTrue(os.path.isfile(os.path.join(template, "template.json")), "kept where it was")
        finally:
            qt.QSettings().remove(projects.ROOT_KEY)
            qt.QSettings().remove(projects.ROOT_CHOSEN_KEY)
            os.environ[projects.ROOT_ENVIRONMENT] = self.root

    def test_97_save_a_copy_over_an_older_copy_that_cannot_be_written_is_not_reported_saved(self):
        """"Save a copy as…" onto an older copy in a folder that cannot be written (or onto a read-only older copy) fails, and
        says so, naming the folder or the file. It said "Saved a copy as MRHead.mrb" and returned the path because the older
        file of that name was still there: the success was taken from the file being there, not from the writer (BB-1).
        """
        from unittest import mock

        from StudioLib import projects_ui

        service = self._service()
        self._userVolume()
        self._projectFor(service)
        self._save(service)
        folder = os.path.join(self.tempDir, "read-only copies")
        os.makedirs(folder)
        copyPath = os.path.join(folder, "MRHead.mrb")
        with open(copyPath, "w") as f:
            f.write("old copy")
        realAccess = os.access
        denied = []

        def access(path, mode, *args, **kwargs):
            # As a read-only folder or file is for a user who is not an administrator (the test may run as root)
            if os.path.normpath(os.path.abspath(str(path))) in denied and mode & os.W_OK:
                return False
            return realAccess(path, mode, *args, **kwargs)

        toasts = []
        running = self.autosave._service
        self.autosave._service = service
        try:
            with mock.patch.object(projects_ui, "message", lambda text, **kwargs: toasts.append((str(text), kwargs))), \
                    mock.patch.object(os, "access", access):
                # The folder cannot be written: the writer refuses it, the older copy stays
                denied[:] = [os.path.normpath(folder)]
                self.assertIsNone(projects_ui.saveCopyAs(copyPath))
                self.assertEqual(len(toasts), 1, toasts)
                self.assertNotIn("Saved a copy", toasts[-1][0])
                self.assertIn("could not be saved", toasts[-1][0])
                self.assertIn(folder, toasts[-1][0])
                self.assertEqual(toasts[-1][1].get("level"), "warning")
                # A read-only older copy (the bundle cannot be compressed over it): the writer fails, the file is named
                denied[:] = [os.path.normpath(copyPath)]
                with mock.patch.object(projects_ui, "writeBundleKeepingState", lambda scene, path: False):
                    self.assertIsNone(projects_ui.saveCopyAs(copyPath))
                self.assertEqual(len(toasts), 2, toasts)
                self.assertNotIn("Saved a copy", toasts[-1][0])
                self.assertIn("could not be saved", toasts[-1][0])
                self.assertIn("MRHead.mrb is read-only", toasts[-1][0])
                with open(copyPath) as f:
                    self.assertEqual(f.read(), "old copy", "nothing was written")
                # A folder that can be written: the older copy is replaced by the bundle and the copy is reported saved
                denied[:] = []
                self.assertEqual(projects_ui.saveCopyAs(copyPath), copyPath)
                self.assertEqual(toasts[-1][0], "Saved a copy as MRHead.mrb")
                import zipfile

                self.assertTrue(zipfile.is_zipfile(copyPath), "the older copy was replaced by the bundle")
        finally:
            self.autosave._service = running

    def test_98_locked_project_warning_is_seen_when_opened_early_or_with_a_load_toast(self):
        """BB-1: a project that another Slicer Studio has open, opened in this window. (1) From the command line the scene is
        loaded before the service installs its user interface (no announcer yet): the "open in another Slicer Studio"
        warning, given once per project, was dropped and never shown. It is now given to the announcer once it is set.
        (2) With Open... the warning was a plain 8 s toast that the "Loaded scan.mrml" of the same load replaced 0.2 s later.
        It now stays until the next input, so the load confirmation does not replace it.
        """
        from types import SimpleNamespace

        from StudioLib import importer, projects_ui, shell

        projects = self.projects
        self._userVolume()
        first = self._service()
        project = self._projectFor(first)
        self._save(first)
        first.releaseProject()
        other = projects.Project(project.path)
        self.assertTrue(other.lock())
        running = self.autosave._service
        studioShell = shell.Shell(force=True)
        try:
            # (1) No announcer yet: the warning waits for it
            service = self._service()
            service.poll()
            self.assertIsNone(service.project, "the project is held by the other window")
            self.assertTrue(service._lockedAnnounced, "the warning was given")
            announced = []
            service.setProjectAnnouncer(lambda event, opened, **info: announced.append((event, opened.name if opened else None)))
            self.assertEqual(announced, [("locked", project.name)])
            service.setProjectAnnouncer(lambda event, opened, **info: announced.append(event))
            self.assertEqual(len(announced), 1, "given once")
            # (2) The warning in Studio, then the load confirmation of the same load: the warning stays
            self.autosave._service = service
            studioShell.apply()
            slicer.app.processEvents()
            projects_ui.announce("locked", other)
            warning = f"{project.name} is open in another Slicer Studio: changes made here are not saved to it."
            self.assertEqual(studioShell.toastText(), warning)
            loaded = SimpleNamespace(segmentations=[], nodes=lambda: [], alreadyOpen=[], scenes=[project.scenePath])
            importer._loadToast(loaded, studioShell)
            slicer.app.processEvents()
            self.assertEqual(studioShell.toastText(), warning, "not replaced by 'Loaded <scene>'")
        finally:
            other.unlock()
            self.autosave._service = running
            studioShell.dispose()
            slicer.app.processEvents()


    def test_99_classic_close_asks_nothing_about_a_saved_unedited_project(self):
        """A project that is up to date on disk but whose scene is "modified since read" (what an open changes: views,
        hierarchy numbers) is closed unasked by File > Close Scene and by quitting in Classic (the project save, not the
        official "Save before ...?" prompt, is what protects the data); a project that is not saved still asks.
        """
        from StudioLib import firstrun

        mainWindow = slicer.util.mainWindow()
        if not hasattr(mainWindow, "closeSceneSummaryRequested"):
            self.skipTest("the main window has no close scene summary hook")
        service = self._service()
        self._userVolume()
        self._projectFor(service)
        self._save(service)
        # What an open leaves behind: the scene differs from its file, the project has nothing to save
        slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").Modified()
        slicer.mrmlScene.GetSubjectHierarchyNode().Modified()
        self.assertTrue(slicer.mrmlScene.GetModifiedSinceRead())
        self.assertFalse(service.projectNeedsSave(includeScene=True))
        handled = slicer.app.property(firstrun.DISCLAIMER_HANDLED_PROPERTY)
        slicer.app.setProperty(firstrun.DISCLAIMER_HANDLED_PROPERTY, False)
        service.connectCloseSummary()
        try:
            for signalName in ("closeSceneSummaryRequested", "closeSummaryRequested"):
                mainWindow.setProperty(firstrun.CLOSE_MODE_PROPERTY, "")
                getattr(mainWindow, signalName)()
                self.assertEqual(mainWindow.property(firstrun.CLOSE_MODE_PROPERTY), "projectSaved", signalName)
            # Not up to date: the handler leaves the official prompt on
            service.saveNow = lambda *args, **kwargs: False
            mainWindow.setProperty(firstrun.CLOSE_MODE_PROPERTY, "")
            mainWindow.closeSceneSummaryRequested()
            self.assertFalse(mainWindow.property(firstrun.CLOSE_MODE_PROPERTY))
        finally:
            slicer.app.setProperty(firstrun.DISCLAIMER_HANDLED_PROPERTY, handled)
            mainWindow.setProperty(firstrun.CLOSE_MODE_PROPERTY, "")

    def test_104_new_project_and_close_scene_with_a_study_open_ask_nothing(self):
        """A study opened from Library while a project was open saved and released the project but kept its scan and lesion
        in the scene (the lesion hidden, which marks the scene modified): with no project, Ctrl+N and Ctrl+W fell back to
        3D Slicer's File > Close Scene and asked "Save before closing scene?" about saved work, with a "Save scene" that
        writes outside the project model (BB-1). They now close the study (its subject's edits are written first) and the
        scene unasked.
        """
        from unittest import mock

        from StudioLib import projects_ui, study_ui

        if not study_ui.isAvailable():
            self.skipTest("studies are not part of this build")
        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or mainWindow.findChild(qt.QAction, "FileCloseSceneAction") is None:
            self.skipTest("the main window has no Close Scene action")
        projects = self.projects
        service = self._service()
        studyFolder = os.path.join(projects.studiesFolder(), "Plain")
        os.makedirs(studyFolder)
        _volume, scanPath = self._userVolume("sub-01_T1w", shape=(12, 14, 16))
        slicer.mrmlScene.Clear(0)
        shutil.copy(scanPath, os.path.join(studyFolder, "sub-01_T1w.nii.gz"))
        asked = []

        def closeQuestion():
            dialog = slicer.app.activeModalWidget()
            if dialog is not None:
                asked.append(str(dialog.windowTitle))
                dialog.reject()

        timer = qt.QTimer()
        timer.setInterval(100)
        timer.connect("timeout()", closeQuestion)
        timer.start()
        self.addCleanup(timer.stop)
        self.addCleanup(study_ui.closeStudy)
        patch = mock.patch.object(self.autosave, "instance", return_value=service)  # (the one study_ui and projects_ui use)
        patch.start()
        self.addCleanup(patch.stop)
        for key in ("newProject", "closeScene"):
            volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
            self._lesion(volume)
            self._projectFor(service)
            self._save(service)
            session = study_ui.createStudy([os.path.join(studyFolder, "sub-01_T1w.nii.gz")], folder=studyFolder)
            self.assertIs(study_ui.session(), session)
            self.assertIsNone(service.project, "the study released the project")
            slicer.mrmlScene.GetNodeByID("vtkMRMLSliceNodeRed").Modified()
            slicer.mrmlScene.GetSubjectHierarchyNode().Modified()
            self.assertTrue(slicer.mrmlScene.GetModifiedSinceRead())
            if key == "newProject":
                service.newProject()
            else:
                self.assertTrue(projects_ui.closeScene())
            for _step in range(5):
                slicer.app.processEvents()
                time.sleep(0.05)
            self.assertEqual(asked, [], key + " asks nothing with a study open")
            self.assertIsNone(study_ui.session(), key + " closes the study")
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLSegmentationNode"), 0, "the scene was closed")
            self.io.backgroundWriter().drain(10.0)
        timer.stop()

    def test_105_new_project_asks_nothing_about_a_volume_not_saved_with_the_scene(self):
        """A Map run in progress keeps its temporary or partial map in a volume that is not saved with the scene: the project
        save leaves it "modified since read" while the scene itself is not. Ctrl+N and Ctrl+W then fell back to 3D Slicer's
        File > Close Scene, which asked "Save before closing scene?" about that volume (BB-1). They now close the saved
        project unasked.
        """
        from unittest import mock

        from StudioLib import firstrun, projects_ui

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or mainWindow.findChild(qt.QAction, "FileCloseSceneAction") is None:
            self.skipTest("the main window has no Close Scene action")
        service = self._service()
        asked = []

        def closeQuestion():
            dialog = slicer.app.activeModalWidget()
            if dialog is not None:
                asked.append(str(dialog.windowTitle))
                dialog.reject()

        timer = qt.QTimer()
        timer.setInterval(100)
        timer.connect("timeout()", closeQuestion)
        timer.start()
        self.addCleanup(timer.stop)
        handled = slicer.app.property(firstrun.DISCLAIMER_HANDLED_PROPERTY)
        slicer.app.setProperty(firstrun.DISCLAIMER_HANDLED_PROPERTY, True)  # Studio's own close prompt
        self.addCleanup(slicer.app.setProperty, firstrun.DISCLAIMER_HANDLED_PROPERTY, handled)
        patch = mock.patch.object(self.autosave, "instance", return_value=service)  # (the one projects_ui uses)
        patch.start()
        self.addCleanup(patch.stop)
        for key in ("newProject", "closeScene"):
            volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
            self._lesion(volume)
            self._projectFor(service)
            temporary = slicer.util.addVolumeFromArray(np.zeros((4, 5, 6), dtype=np.float32), name="Network map (computing…)")
            temporary.SetSaveWithScene(False)
            self._save(service)
            self.assertFalse(slicer.mrmlScene.GetModifiedSinceRead(), "the save wrote the scene")
            self.assertTrue(slicer.mrmlScene.GetStorableNodesModifiedSinceRead(), "the temporary map is not saved")
            if key == "newProject":
                service.newProject()
            else:
                self.assertTrue(projects_ui.closeScene())
            for _step in range(5):
                slicer.app.processEvents()
                time.sleep(0.05)
            self.assertEqual(asked, [], key + " asks nothing about a volume that is not saved with the scene")
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLSegmentationNode"), 0, key + " closed the scene")
            self.assertIsNone(slicer.mrmlScene.GetFirstNodeByName("Network map (computing…)"), key + " closed the scene")
            self.io.backgroundWriter().drain(10.0)
        timer.stop()

    def test_100_files_saved_elsewhere_come_back_under_their_plain_names(self):
        """Classic "Save Data" into another folder points every node at the new files; the project's old files leave in the
        same save, so the re-imported copies keep "Scan.nrrd" etc. instead of "Scan 2.nrrd" (BB-1).
        """
        service = self._service()
        volume, _path = self._userVolume("Scan")
        points = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "Pts")
        points.AddControlPoint((1, 2, 3))
        project = self._projectFor(service)
        self._save(service)
        before = self._projectFiles(project)
        self.assertIn("Markups/Pts.mrk.json", before)
        elsewhere = os.path.join(self.tempDir, "elsewhere")
        os.makedirs(elsewhere)
        self.assertTrue(slicer.util.saveNode(volume, os.path.join(elsewhere, "Scan.nrrd")))
        self.assertTrue(slicer.util.saveNode(points, os.path.join(elsewhere, "Pts.mrk.json")))
        points.AddControlPoint((4, 5, 6))
        self.assertTrue(slicer.util.saveNode(points, os.path.join(elsewhere, "Pts.mrk.json")))
        self._save(service)
        after = self._projectFiles(project)
        self.assertFalse([path for path in after if " 2." in path], after)
        self.assertIn("Markups/Pts.mrk.json", after)
        self.assertEqual(sorted(path for path in after if path.startswith("Images/")), ["Images/Scan.nrrd"], after)
        self.assertEqual(sorted(os.listdir(elsewhere)), ["Pts.mrk.json", "Scan.nrrd"], "the user's other folder is untouched")
        for node in (volume, points):
            self.assertTrue(os.path.abspath(node.GetStorageNode().GetFileName()).startswith(os.path.abspath(project.path)))

    @unittest.skipUnless(os.name == "posix", "SIGTERM is POSIX")
    def test_101_sigterm_saves_an_edit_made_after_the_last_save(self):
        """Stopping the web build (Ctrl+C of the local launcher, docker stop) sends SIGTERM to Slicer, which had no handler: the
        process ended before aboutToQuit saved the project, so a paint stroke made less than IntervalMinutes after the last
        save was lost while the top bar said "Saved" (BB-1). SIGTERM now quits as File > Exit does: the stroke is in the
        project's lesion file and project.json says "closed". The private recovery session still counts as a crash.
        """
        import signal

        service = self._service()
        volume, _path = self._userVolume("Scan", shape=(12, 14, 16), extension=".nrrd")
        lesion, segmentId, array = self._lesion(volume)
        project = self._projectFor(service)
        self._save(service)
        lesionFile = os.path.join(project.path, "Segmentations", "Lesions.seg.nrrd")
        savedVoxels = int(array.sum())
        array[1:3, 1:3, 1:3] = 1  # a paint stroke after the save
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, lesion, segmentId, volume)
        paintedVoxels = int(array.sum())
        self.assertGreater(paintedVoxels, savedVoxels)
        exits = []
        # The test application keeps running: the quit is what aboutToQuit does
        service._exitApplication = lambda: (exits.append(True), service._onAboutToQuit())
        self.assertTrue(service.installStopSignalHandler(), "SIGTERM is handled")
        handler = signal.getsignal(signal.SIGTERM)
        self.assertTrue(callable(handler) and handler not in (signal.SIG_DFL, signal.SIG_IGN))
        os.kill(os.getpid(), signal.SIGTERM)
        end = time.time() + 10
        while not exits and time.time() < end:
            slicer.app.processEvents()
            time.sleep(0.01)
        self.assertEqual(exits, [True], "SIGTERM quits the application once")
        self.assertTrue(service._stoppedBySignal)
        self.io.backgroundWriter().drain(30.0)
        self.assertIsNone(service.project, "the project is released")
        self.assertEqual(self.projects.readJson(os.path.join(project.path, ".studio", "project.json")).get("state"), "closed")
        saved = slicer.util.loadSegmentation(lesionFile)
        try:
            savedId = saved.GetSegmentation().GetNthSegmentID(0)
            self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(saved, savedId, volume).sum()), paintedVoxels,
                             "the paint stroke made after the last save is in the project")
        finally:
            slicer.mrmlScene.RemoveNode(saved)
        service.dispose()
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.SIG_DFL, "dispose() restores the default SIGTERM action")


    def test_102_project_in_a_read_only_folder_is_not_half_removed(self):
        """BB-1: Move to Trash of a project in a read-only Projects folder (a share where only Projects/ is locked) asked
        "This system has no Trash. Delete the project permanently?" (there is a Trash; the folder is read-only), then
        deleted every file of the project but not its folder and showed "[Errno 13] Permission denied". It now says the
        folder is read-only, asks nothing and deletes nothing. Read-only is simulated (root may write anywhere).
        """
        from StudioLib import projects_ui

        projects = self.projects
        realAccess, realTrash = os.access, projects.moveToTrash
        realQuestion, realNotice = projects_ui._question, projects_ui._notice
        for case in ("Projects folder", "project folder", "folder inside, no trash"):
            with self.subTest(case=case):
                parent = os.path.join(self.tempDir, "Read only ü " + str(len(case)), "Projects")
                folder = os.path.join(parent, "MRHead")
                inside = os.path.join(folder, "Data")
                os.makedirs(inside)
                for name in ("MRHead.mrml", os.path.join("Data", "scan.nrrd")):
                    with open(os.path.join(folder, name), "w") as file:
                        file.write("x")
                readOnly = {"Projects folder": parent, "project folder": folder}.get(case, inside)
                before = sorted(os.path.relpath(os.path.join(d, n), folder) for d, _f, ns in os.walk(folder) for n in ns)
                questions, notices = [], []

                def access(path, mode, *args, readOnly=readOnly, **kwargs):
                    if mode & os.W_OK and os.path.normpath(str(path)) == os.path.normpath(readOnly):
                        return False
                    return realAccess(path, mode, *args, **kwargs)

                def question(parent, title, text, buttons, default=0, questions=questions):
                    questions.append(text)
                    return 0  # "Move to Trash", then "Delete"

                os.access = access
                projects_ui._question = question
                projects_ui._notice = lambda parent, title, text, notices=notices: notices.append(text)
                if case == "folder inside, no trash":
                    projects.moveToTrash = lambda path: False
                try:
                    with self.assertRaises(projects.ProjectError) as context:
                        projects.trashFolder(folder, confirmDelete=lambda questions=questions: questions.append("delete") or True)
                    self.assertFalse(projects_ui.moveToTrash(folder))
                finally:
                    os.access, projects.moveToTrash = realAccess, realTrash
                    projects_ui._question, projects_ui._notice = realQuestion, realNotice
                text = str(context.exception)
                self.assertIn("read-only", text)
                self.assertIn("Nothing was deleted", text)
                self.assertNotIn("Errno", text)
                self.assertNotIn("Trash", text)
                self.assertEqual(notices, [text])
                if case == "folder inside, no trash":
                    # Found only when the move failed: after "Move to Trash?", never "This system has no Trash"
                    self.assertEqual(len(questions), 1)
                    self.assertNotIn("no Trash", questions[0])
                else:
                    self.assertEqual(questions, [], "said before any question")
                after = sorted(os.path.relpath(os.path.join(d, n), folder) for d, _f, ns in os.walk(folder) for n in ns)
                self.assertEqual(after, before, "nothing deleted")
                self.assertTrue(projects.trashFolder(folder, confirmDelete=lambda: True))
                self.assertFalse(os.path.exists(folder))

    def test_103_lock_left_on_another_computer_can_be_taken_over(self):
        """BB-1: a project whose .studio/lock was written by QLockFile on another computer (it crashed there, or the
        folder was copied while open) could never be opened, renamed or trashed, and Open said it "is open in another
        Slicer Studio: changes made here are not saved to it" although nothing was opened. QLockFile cannot check a process
        of another computer, so the lock never became stale. Now: a recent one is refused with a message that names the
        computer and offers Open here (which takes the lock); one whose project shows no heartbeat for
        ``FOREIGN_LOCK_STALE_S`` is stale. A lock of a live process on this computer is never taken.
        """
        from StudioLib import projects_ui

        projects = self.projects
        service = self._service()
        self._userVolume()
        project = self._projectFor(service)
        self._save(service)
        path = project.path
        service.releaseProject()
        projects.setCurrentProject(None)
        self.io.backgroundWriter().drain(10.0)
        slicer.mrmlScene.Clear(0)
        lockPath = os.path.join(path, ".studio", "lock")
        self.assertFalse(os.path.exists(lockPath))
        variants = {
            "machine id": "4242\nSlicerApp-real\nlab-laptop\n0123456789abcdef0123456789abcdef\n11111111-2222-3333-4444-555555555555\n",
            "host name only": "4242\nSlicerApp-real\nlab-laptop\n",
        }
        announced = []
        service.setProjectAnnouncer(lambda event, project, **info: announced.append((event, info)))
        messages = []
        realMessage = projects_ui.message
        projects_ui.message = lambda text, **kwargs: messages.append((text, kwargs))
        try:
            for variant, text in variants.items():
                with self.subTest(variant=variant):
                    # A recent lock of another computer: refused, with the computer named and a way out
                    with open(lockPath, "w") as f:
                        f.write(text)
                    self.assertTrue(projects.isLockedByOther(path))
                    holder = projects.lockHolder(path)
                    self.assertTrue(holder["otherComputer"])
                    self.assertEqual(holder["host"], "lab-laptop")
                    refusal = projects.trashRefusal(path)
                    self.assertIn("lab-laptop", refusal)
                    self.assertIn("Open here", refusal)
                    announced.clear()
                    self.assertFalse(service.openProject(path))
                    self.assertEqual([event for event, _info in announced], ["locked"])
                    info = announced[0][1]
                    self.assertIs(info["opened"], False)
                    messages.clear()
                    projects_ui.announce("locked", projects.Project(path), **info)
                    self.assertEqual(len(messages), 1)
                    toast, options = messages[0]
                    self.assertIn("was not opened", toast)
                    self.assertIn("lab-laptop", toast)
                    self.assertNotIn("changes made here are not saved", toast)
                    self.assertEqual(options.get("actionText"), "Open here")
                    # Open here: the lock is taken and the project opens
                    self.assertTrue(projects.takeOverLock(path))
                    self.assertTrue(service.openProject(path))
                    self.assertIsNotNone(service.project)
                    self.assertIsNotNone(slicer.mrmlScene.GetFirstNodeByName("sub-012_T1w"))
                    service.releaseProject()
                    projects.setCurrentProject(None)
                    self.io.backgroundWriter().drain(10.0)
                    slicer.mrmlScene.Clear(0)
                    # A lock of another computer whose project has shown no heartbeat for hours is stale: it opens
                    with open(lockPath, "w") as f:
                        f.write(text)
                    data = projects.readJson(projects.projectFile(path))
                    old = projects._isoTime(time.time() - projects.FOREIGN_LOCK_STALE_S - 60)
                    data.update(heartbeat=old, state="open")
                    self.io.atomicWriteJson(projects.projectFile(path), data)
                    self.assertFalse(projects.isLockedByOther(path))
                    self.assertIsNone(projects.trashRefusal(path))
                    self.assertTrue(service.openProject(path))
                    self.assertIsNotNone(service.project)
                    service.releaseProject()
                    projects.setCurrentProject(None)
                    self.io.backgroundWriter().drain(10.0)
                    slicer.mrmlScene.Clear(0)
            # A live Slicer Studio of this computer (the same process stands for it) is never taken over, however old
            other = projects.Project(path)
            self.assertTrue(other.lock())
            try:
                data = projects.readJson(projects.projectFile(path))
                data.update(heartbeat=projects._isoTime(time.time() - 10 * projects.FOREIGN_LOCK_STALE_S))
                self.io.atomicWriteJson(projects.projectFile(path), data)
                self.assertTrue(projects.isLockedByOther(path))
                self.assertFalse(projects.lockHolder(path)["otherComputer"])
                self.assertFalse(projects.takeOverLock(path))
                self.assertTrue(os.path.isfile(lockPath), "the lock of this computer is kept")
                self.assertNotIn("Open here", projects.lockedText(path))
            finally:
                other.unlock()
        finally:
            projects_ui.message = realMessage


def _sphere(center=(0, 0, 0)):
    import vtk

    source = vtk.vtkSphereSource()
    source.SetCenter(center)
    source.SetRadius(5)
    source.Update()
    return source.GetOutput()
