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
- a project loaded in Classic continues in place in Studio (no copy); the Library order follows official saves;
- a project saves when its data fits on the disk, also with less than 1 GB free (only the private autosave keeps 1 GB);
- a scene file that another program changed is kept: Studio writes "<project> (Slicer Studio).mrml" next to it;
- a file from outside that the project took over is "already open" when it is opened again, also after a restart;
- Studio's own folders (Studies/, Projects/, a project, the Slicer Studio folder) never become a study;
- moving the Slicer Studio folder into a folder inside it is refused before anything is made;
- "Save a copy as…" leaves the project as it is: the next save writes nothing again and leaves no stale copy of the scan.
"""

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest

import numpy as np
import qt
import vtk

import slicer


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
        status = service.status()
        self.assertEqual(status.projectName, "sub-012 T1w")
        text, level, toolTip, _icon = projects_ui.projectStatus(status)
        self.assertEqual((text, level), ("Saved", "ok"))
        self.assertIn("Projects/sub-012 T1w", toolTip)
        items = {item["id"] for item in projects_ui.paletteItems()}
        self.assertTrue({"projects.new", "projects.open", "projects.rename", "projects.showFolder", "projects.changeFolder"} <= items)

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
        self.assertFalse(os.path.exists(os.path.join(project.path, ".studio", "removed")), "no scene file is moved away")
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
        self.assertEqual(announced, [])

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
        realGetSaveFileName = qt.QFileDialog.getSaveFileName

        def fakeGetSaveFileName(parent, caption, start, *args):
            starts.append(start)
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
            self.assertTrue(os.path.isdir(os.path.dirname(start)), f"the dialog starts in an existing folder: {start}")
            self.assertEqual(os.path.realpath(os.path.dirname(start)), os.path.realpath(exports))
            self.assertEqual(os.path.basename(start), project.name + ".mrb")
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


def _sphere(center=(0, 0, 0)):
    import vtk

    source = vtk.vtkSphereSource()
    source.SetCenter(center)
    source.SetRadius(5)
    source.Update()
    return source.GetOutput()
