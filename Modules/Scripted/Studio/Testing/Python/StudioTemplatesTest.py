"""Your own MNI templates (A-10, decision D-T2): the registry, the checks, the dialog, Start on template, spaces, projects
and studies.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioTemplatesTest'); slicer.app.exit(0)"

Nothing is downloaded: the test templates are made from the bundled MNI152NLin2009cAsym 2 mm brain (resampled to
1.5 mm, stored LAS, stacked into a 4D file), and the non-MNI head has the header of the MRHead sample. The Slicer
Studio folder is a temporary folder "Slicer Studio tést" (``SLICER_STUDIO_PROJECTS_ROOT``); the setting
``Studio/Templates/Default`` is restored afterwards.
"""

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import numpy as np
import qt
import vtk

import slicer


def _md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _allFiles(root):
    found = []
    for directory, _folders, files in os.walk(root):
        found += [os.path.join(directory, name) for name in files]
    return found


class StudioTemplatesTest(unittest.TestCase):
    """The registry (StudioLib.templates), its user interface (templates_ui) and every place a template is used."""

    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import templates

        cls.tempDir = tempfile.mkdtemp(prefix="StudioTemplatesTest-")
        cls.inputs = os.path.join(cls.tempDir, "my templates é")
        os.makedirs(cls.inputs)
        builtin = templates.builtinTemplates()[0]
        cls.bundled, cls.bundledMask = builtin.imagePath, builtin.maskPath
        data, affine, _header = nifti.read_nifti(cls.bundled)
        cls.bundledShape = data.shape
        # A 1.5 mm MNI image (not a known grid): the bundled brain resampled, sform/qform 4
        spacing = 1.5
        low = affine[:3, 3]
        shape = tuple(int(np.floor((data.shape[axis] - 1) * 2.0 / spacing)) + 1 for axis in range(3))
        target = np.diag([spacing, spacing, spacing, 1.0])
        target[:3, 3] = low
        resampled = geometry.resample_to_grid(data, affine, geometry.Grid(shape, target), mode="linear").astype(np.float32)
        cls.image15 = os.path.join(cls.inputs, "my_mni_1.5mm_T1w.nii.gz")
        nifti.write_nifti(cls.image15, resampled, target)
        cls.mask15 = os.path.join(cls.inputs, "my_mni_1.5mm_mask.nii.gz")
        nifti.write_nifti(cls.mask15, (resampled > 10).astype(np.uint8), target)
        cls.shape15 = shape
        # The bundled brain stored LAS (x flipped): the same known grid
        flipped = np.diag([-2.0, 2.0, 2.0, 1.0])
        flipped[:3, 3] = (low[0] + 2.0 * (data.shape[0] - 1), low[1], low[2])
        cls.las = os.path.join(cls.inputs, "bundled_LAS.nii.gz")
        nifti.write_nifti(cls.las, data[::-1].astype(np.float32), flipped)
        # A 4D file (two volumes of the 1.5 mm image)
        cls.image4d = os.path.join(cls.inputs, "my_mni_4d.nii.gz")
        nifti.write_nifti(cls.image4d, np.stack([resampled, resampled * 0.5], axis=-1), target)
        # A head scan that is not in MNI space: the header of the MRHead sample (scanner coordinates, NRRD)
        cls.head = os.path.join(cls.inputs, "MRHead.nrrd")
        header = ("NRRD0004\ntype: short\ndimension: 3\nspace: left-posterior-superior\nsizes: 256 256 130\n"
                  "space directions: (0,1,0) (0,0,-1) (-1.2999954223632812,0,0)\nkinds: domain domain domain\nendian: little\n"
                  "encoding: gzip\nspace origin: (86.644897460937486,-133.92860412597656,116.78569793701172)\n\n")
        with open(cls.head, "wb") as f:
            f.write(header.encode("ascii"))
            f.write(gzip.compress(bytes(256 * 256 * 130 * 2)))
        # A brain-sized surface in MNI coordinates, and one far away
        cls.surface = cls._sphere(os.path.join(cls.inputs, "lh.pial.vtk"), (-30.0, -18.0, 18.0), 45.0)
        cls.farSurface = cls._sphere(os.path.join(cls.inputs, "far.vtk"), (400.0, 0.0, 0.0), 45.0)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tempDir, ignore_errors=True)

    @staticmethod
    def _sphere(path, center, radius):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        source.SetThetaResolution(16)
        source.SetPhiResolution(16)
        source.Update()
        writer = vtk.vtkPolyDataWriter()
        writer.SetFileName(path)
        writer.SetHeader("SPACE=RAS")  # the coordinate system comment that Slicer reads
        writer.SetInputData(source.GetOutput())
        writer.Write()
        return path

    def setUp(self):
        from StudioLib import projects, templates, templates_ui

        self.templates, self.ui, self.projects = templates, templates_ui, projects
        slicer.mrmlScene.Clear(0)
        self.root = os.path.join(self.tempDir, "Slicer Studio tést")
        shutil.rmtree(self.root, ignore_errors=True)
        self._environment = {name: os.environ.get(name) for name in (projects.ROOT_ENVIRONMENT, projects.ENABLED_ENVIRONMENT)}
        os.environ[projects.ROOT_ENVIRONMENT] = self.root
        os.environ.pop(projects.ENABLED_ENVIRONMENT, None)
        settings = qt.QSettings()
        self._default = settings.value(templates.DEFAULT_KEY)
        self._lastDirectory = settings.value(templates_ui.LAST_DIRECTORY_KEY)
        settings.remove(templates.DEFAULT_KEY)
        self._keysBefore = set(settings.allKeys())
        self.inputMd5 = {path: _md5(path) for path in _allFiles(self.inputs)}
        templates.registerGrids(force=True)
        self.services = []

    def tearDown(self):
        from StudioLib import autosave_io

        for service in self.services:
            service.dispose()
        autosave_io.backgroundWriter().drain(30.0)
        slicer.mrmlScene.Clear(0)
        for path, digest in self.inputMd5.items():
            self.assertEqual(_md5(path), digest, f"{path} was changed")
        settings = qt.QSettings()
        # The bundled template comes through Sample Data, whose loads the IO manager lists in RecentlyLoadedFiles (as in official
        # 3D Slicer); everything else written by the templates is under Studio/
        added = {key for key in set(settings.allKeys()) - self._keysBefore if not key.startswith("RecentlyLoadedFiles/")}
        self.assertTrue(all(key.startswith("Studio/") for key in added), f"keys outside Studio/: {sorted(added)}")
        for key, value in ((self.templates.DEFAULT_KEY, self._default), (self.ui.LAST_DIRECTORY_KEY, self._lastDirectory)):
            if value is None:
                settings.remove(key)
            else:
                settings.setValue(key, value)
        for name, value in self._environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.root, ignore_errors=True)
        self.templates.registerGrids(force=True)

    #
    # Helpers
    #

    def _add(self, path=None, name="My MNI 1.5 mm", **kwargs):
        return self.templates.addTemplate(path or self.image15, name=name, **kwargs)

    def _shell(self):
        import sys

        module = sys.modules.get("StudioLib.shell")
        return module.instance() if module is not None and hasattr(module, "instance") else None

    #
    # Identify
    #

    def test_01_identify(self):
        templates = self.templates
        for path in (self.bundled, self.las):
            detection = templates.identify(path)
            self.assertTrue(detection.ok, path)
            self.assertEqual(detection.status, "known")
            self.assertEqual(detection.spaceGuess, "MNI152NLin2009cAsym")
            self.assertEqual(detection.resolution, 2)
        self.assertEqual(templates.identify(self.las).orientation, "LAS")
        detection = templates.identify(self.image15)
        self.assertTrue(detection.ok)
        self.assertEqual(detection.status, "mni")
        self.assertEqual(detection.resolution, 1.5)
        self.assertEqual(detection.shape, self.shape15)
        self.assertIn("1.5 mm isotropic", detection.facts)
        self.assertIn("sform 4", detection.facts)
        self.assertIsNone(detection.privacyNote)
        head = templates.identify(self.head)
        self.assertFalse(head.ok)
        self.assertEqual(head.status, "refused")
        self.assertEqual(head.message, templates.REFUSAL)
        fourD = templates.identify(self.image4d)
        self.assertTrue(fourD.ok)
        self.assertEqual(fourD.frames, 2)
        self.assertTrue(any("2 volumes" in line for line in fourD.warnings))
        broken = os.path.join(self.tempDir, "broken.nii.gz")
        with open(broken, "wb") as f:
            f.write(b"not an image")
        self.assertEqual(templates.identify(broken).status, "error")
        # Masks and heads must share the image's grid (any storage order)
        self.assertTrue(templates.checkCompanion(detection, self.mask15)[0])
        self.assertFalse(templates.checkCompanion(detection, self.bundledMask)[0])
        self.assertTrue(templates.checkCompanion(templates.identify(self.las), self.bundledMask)[0])
        # Surfaces must lie in the MNI box
        self.assertEqual(templates.checkSurface(self.surface), (True, ""))
        self.assertFalse(templates.checkSurface(self.farSurface)[0])

    #
    # Registry
    #

    def test_02_add_copy_remove_default(self):
        templates = self.templates
        self.assertEqual(templates.defaultTemplateId(), templates.BUILTIN_DEFAULT)
        self.assertEqual(next(template.id for template in templates.listTemplates() if template.builtin), templates.BUILTIN_DEFAULT)
        self.assertFalse(os.path.exists(templates.templatesFolder()), "nothing is written before a template is added")
        template = self._add(mask=self.mask15, surfaces=(self.surface,), space="MNI152NLin2009bAsym", contrast="T1w", notes="Made for a test. CC0.")
        self.assertEqual(template.id, "user:my-mni-1-5-mm")
        self.assertEqual(os.path.dirname(template.folder), templates.templatesFolder())
        self.assertEqual(os.path.basename(template.folder), "My MNI 1.5 mm")
        files = sorted(os.path.relpath(path, template.folder).replace(os.sep, "/") for path in _allFiles(template.folder))
        self.assertEqual(files, ["my_mni_1.5mm_T1w.nii.gz", "my_mni_1.5mm_mask.nii.gz", "surfaces/lh.pial.vtk", "template.json"])
        with open(os.path.join(template.folder, "template.json"), encoding="utf-8") as f:
            record = json.load(f)
        text = json.dumps(record)
        self.assertNotIn(self.tempDir, text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertEqual(record["schema"], "slicer-studio-template")
        self.assertEqual(record["space"], "MNI152NLin2009bAsym")
        self.assertEqual(record["resolution_mm"], 1.5)
        self.assertEqual(record["sha256"]["image"], _sha256(self.image15))
        self.assertEqual(record["sha256"]["mask"], _sha256(self.mask15))
        self.assertEqual(record["sha256"]["surfaces/lh.pial.vtk"], _sha256(self.surface))
        self.assertEqual(_md5(template.imagePath), _md5(self.image15))
        self.assertEqual(template.metaLine(), "MNI152NLin2009bAsym · 1.5 mm · T1w · with brain mask")
        self.assertTrue(template.canNormalize())
        self.assertEqual(templates.templateById("my-mni-1-5-mm").id, template.id)
        self.assertEqual(templates.templateByChecksum(_sha256(self.image15)).id, template.id)
        self.assertEqual(templates.templateForGrid(self.shape15, templates.identify(self.image15).affine).id, template.id)
        # Default: only Studio/Templates/Default
        templates.setDefault(template.id)
        self.assertEqual(qt.QSettings().value(templates.DEFAULT_KEY), template.id)
        self.assertEqual(templates.defaultTemplateId(), template.id)
        # Remove: the folder goes (to the trash where there is one) and the default falls back to the bundled template
        self.assertTrue(templates.removeTemplate(template.id, confirmDelete=lambda: True))
        self.assertFalse(os.path.exists(template.folder))
        self.assertIsNone(templates.templateById(template.id))
        self.assertEqual(templates.defaultTemplateId(), templates.BUILTIN_DEFAULT)
        with self.assertRaises(templates.TemplateError):
            templates.removeTemplate(templates.BUILTIN_DEFAULT)

    def test_03_refusals_and_names(self):
        templates = self.templates
        with self.assertRaises(templates.TemplateError) as context:
            self._add(self.head, name="Head")
        self.assertEqual(str(context.exception), templates.REFUSAL)
        with self.assertRaises(templates.TemplateError):
            self._add(mask=self.bundledMask, name="Wrong mask")
        with self.assertRaises(templates.TemplateError):
            self._add(surfaces=(self.farSurface,), name="Far surface")
        self.assertFalse(os.path.exists(templates.templatesFolder()) and os.listdir(templates.templatesFolder()), "a refused add leaves nothing")
        first = self._add(name="ICBM test")
        with self.assertRaises(templates.TemplateError):
            self._add(name="icbm TEST")  # the same name
        with self.assertRaises(templates.TemplateError):
            self._add(name=templates.builtinTemplates()[0].name)  # a built-in name
        second = self._add(name="ICBM: test")  # another name with the same slug and folder name
        self.assertNotEqual(first.slug, second.slug)
        self.assertNotEqual(first.folder, second.folder)
        self.assertTrue(os.path.isdir(second.folder))
        # A 4D file is added with its first volume only (a 3D NIfTI)
        fourD = self._add(self.image4d, name="Four D")
        detection = templates.identify(fourD.imagePath)
        self.assertEqual(detection.frames, 1)
        self.assertEqual(detection.shape, self.shape15)
        with self.assertRaises(templates.TemplateError):
            self._add(name="Copy off", copy=False)

    #
    # Start on template, spaces, Draw
    #

    def test_04_start_on_user_template(self):
        from StudioLib import draw, importer, space

        templates = self.templates
        template = self._add(surfaces=(self.surface,), space="MNI152NLin6Asym")
        shell = self._shell()
        volume, segmentation = importer.startOnTemplate(shell=shell, templateId=template.id)
        self.assertIsNotNone(volume)
        self.assertEqual(volume.GetName(), template.name)
        self.assertEqual(volume.GetAttribute("Studio.Role"), "template")
        self.assertEqual(volume.GetAttribute("Studio.TemplateId"), template.id)
        self.assertEqual(volume.GetAttribute("Studio.Space"), "MNI152NLin6Asym 1.5")
        self.assertEqual(space.spaceOf(volume), ("MNI152NLin6Asym", 1.5))
        self.assertTrue(space.isMni(volume))
        self.assertTrue(importer._isTemplate(volume))
        # The lesion is drawn on the template's grid, and its grid is recognized as MNI by itself (registered grid)
        grid = space.gridOf(segmentation)
        self.assertIsNotNone(grid)
        self.assertEqual(tuple(grid[0]), self.shape15)
        self.assertEqual(space.spaceOf(grid), ("MNI152NLin6Asym", 1.5))
        self.assertEqual(space.hemisphereWord(-20.0, segmentation), "Left")
        self.assertEqual(space.hemisphereWord(20.0, segmentation), "Right")
        line, level = draw.DrawExtension._spaceLine(None, segmentation, volume)
        self.assertEqual((line, level), (f"On {template.name} · MNI152NLin6Asym 1.5 mm", "neutral"))
        # The surface is a light gray model in the 3D view only, not user data of a project
        models = [node for node in slicer.util.getNodesByClass("vtkMRMLModelNode") if node.GetAttribute("Studio.Role") == templates.ROLE_SURFACE]
        self.assertEqual(len(models), 1)
        display = models[0].GetDisplayNode()
        self.assertAlmostEqual(display.GetOpacity(), templates.SURFACE_OPACITY)
        self.assertFalse(display.GetVisibility2D())
        self.assertTrue(self.projects.isTemplateNode(models[0]))
        self.assertTrue(self.projects.isTemplateNode(volume))
        # Starting again reuses the volume and its surface
        again, _segmentation = importer.startOnTemplate(shell=shell, templateId=template.id)
        self.assertEqual(again.GetID(), volume.GetID())
        self.assertEqual(len([node for node in slicer.util.getNodesByClass("vtkMRMLModelNode") if node.GetAttribute("Studio.Role") == templates.ROLE_SURFACE]), 1)
        # The default template is used by the plain Start on template
        slicer.mrmlScene.Clear(0)
        templates.setDefault(template.id)
        volume, _segmentation = importer.startOnTemplate(shell=shell)
        self.assertEqual(volume.GetAttribute("Studio.TemplateId"), template.id)
        # The bundled template chosen explicitly
        slicer.mrmlScene.Clear(0)
        volume, _segmentation = importer.startOnTemplate(shell=shell, templateId=templates.BUILTIN_DEFAULT)
        self.assertEqual(space.spaceOf(volume), ("MNI152NLin2009cAsym", 2))
        # The sidecar of a lesion saved on your template names it (id, name, space, sha256), never by path
        slicer.mrmlScene.Clear(0)
        volume, segmentation = importer.startOnTemplate(shell=shell, templateId=template.id)
        sidecar = draw.writeSidecar(os.path.join(self.tempDir, "lesion.nii.gz"), segmentation, volume)
        with open(sidecar, encoding="utf-8") as f:
            content = json.load(f)
        self.assertEqual(content["Template"], {"id": template.id, "name": template.name, "space": "MNI152NLin6Asym", "sha256": template.imageSha256})
        self.assertEqual(content["Space"], "MNI152NLin6Asym")

    def test_05_anisotropic_template_space(self):
        """A template whose voxels are not cubic cannot be a registered grid: it is recognized by its exact grid."""
        from LesionNetworkMappingLib import nifti

        from StudioLib import space

        data, affine, _header = nifti.read_nifti(self.image15)
        stretched = affine.copy()
        stretched[2, 2] = 3.0
        path = os.path.join(self.tempDir, "aniso.nii.gz")
        nifti.write_nifti(path, data[:, :, ::2], stretched)
        template = self._add(path, name="Aniso", space="MNI")
        self.assertTrue(any("not cubic" in line for line in self.templates.identify(path).warnings))
        self.assertEqual(space.spaceOf((data[:, :, ::2].shape, stretched)), ("MNI", 1.5))
        self.assertEqual(space.spaceLabel(("MNI", 1.5)), "MNI (other) 1.5 mm")
        self.templates.removeTemplate(template.id, confirmDelete=lambda: True)
        self.assertIsNone(space.spaceOf((data[:, :, ::2].shape, stretched)))

    #
    # Map
    #

    def test_06_map_family_notice(self):
        import LesionNetworkMapping

        logic = LesionNetworkMapping.LesionNetworkMappingLogic
        self.assertEqual(logic.familyNotice(("MNI152NLin6Asym", 1), "MNI152NLin6Asym"), "")
        self.assertIn("a few millimetres", logic.familyNotice(("MNI152NLin2009bAsym", 0.5), "MNI152NLin6Asym"))
        self.assertIn("MNI (other)", logic.familyNotice(("MNI", 1.5), "MNI152NLin2009cAsym"))
        self.assertEqual(logic.familyNotice(None, "MNI152NLin6Asym"), "")
        self.assertEqual(logic.familyNotice(("MNI152NLin2009bAsym", 0.5), None), "")

    #
    # Projects
    #

    def test_07_project_save_reopen(self):
        from StudioLib import autosave, autosave_io, importer

        template = self._add(surfaces=(self.surface,), space="MNI152NLin2009bAsym")
        service = autosave.AutosaveService(force=True, root=os.path.join(self.tempDir, "private autosave"))
        service.studioActiveOverride = True
        service.reloadSettings()
        self.services.append(service)
        volume, segmentation = importer.startOnTemplate(shell=self._shell(), templateId=template.id)
        service.poll()
        self.assertIsNone(service.project, "the template and its surface alone are not user data")
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        lesion = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
        lesion[60:70, 70:80, 30:40] = 1  # k, j, i: left hemisphere (low i is x < 0 on a RAS grid)
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        service.poll()
        project = service.project
        self.assertIsNotNone(project)
        self.assertTrue(service.saveNow("test", progress=False))
        autosave_io.backgroundWriter().drain(30.0)
        files = [os.path.relpath(path, project.path).replace(os.sep, "/") for path in _allFiles(project.path)]
        images = [name for name in files if name.startswith("Images/")]
        self.assertEqual(len(images), 1, files)
        self.assertEqual(_md5(os.path.join(project.path, images[0])), _md5(template.imagePath), "a byte-for-byte copy of the template in Images/")
        scenePath = self.projects.sceneFile(project.path)
        with open(scenePath, encoding="utf-8") as f:
            sceneText = f.read()
        self.assertNotIn(self.tempDir, sceneText)
        self.assertNotIn(os.path.expanduser("~"), sceneText)
        self.assertIn("Studio.TemplateId", sceneText)
        # Reopen: the template and the lesion are aligned, and the template is still recognized
        service.dispose()
        self.services.remove(service)
        slicer.mrmlScene.Clear(0)
        # Read without a RecentlyLoadedFiles entry (only Studio/* settings may change in this test)
        slicer.mrmlScene.SetURL(scenePath)
        self.assertTrue(slicer.mrmlScene.Connect())
        volumes = [node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if node.GetAttribute("Studio.TemplateId") == template.id]
        self.assertEqual(len(volumes), 1)
        segmentations = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        self.assertEqual(len(segmentations), 1)
        reloaded = slicer.util.arrayFromSegmentBinaryLabelmap(segmentations[0], segmentations[0].GetSegmentation().GetNthSegmentID(0), volumes[0])
        self.assertEqual(int(reloaded.sum()), int(lesion.sum()))
        self.assertTrue(np.array_equal(reloaded > 0, lesion > 0))
        from StudioLib import space

        self.assertEqual(space.spaceOf(volumes[0]), ("MNI152NLin2009bAsym", 1.5))
        privacy = self._privacyModule()
        if privacy is not None:
            # StudioPrivacyCheck: no absolute local path in the project or in template.json
            self.assertEqual(privacy.scan([project.path, template.folder], out=lambda line: None), 0)

    @staticmethod
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

    #
    # Studies
    #

    def test_08_study_missing_template_and_readd(self):
        from LesionNetworkMappingLib.study import Study

        from StudioLib import autosave_io

        templates = self.templates
        template = self._add(name="Study template", space="MNI152NLin6Asym")
        record = templates.studyRecord(template)
        self.assertEqual(record, {"id": "MNI152NLin6Asym", "res": 1.5, "source": template.id, "name": "Study template", "space": "MNI152NLin6Asym",
                                  "sha256": _sha256(self.image15)})
        study = Study.create(os.path.join(self.tempDir, "study é"), name="Study", template=record)
        with open(os.path.join(study.path, "study.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(self.inputs, text)
        self.assertNotIn(self.root, text)
        privacy = self._privacyModule()
        if privacy is not None:
            self.assertEqual(privacy.scan([study.path], out=lambda line: None), 0)
        self.assertEqual(Study.open(study.path).user_template(), {"id": template.id, "name": "Study template", "space": "MNI152NLin6Asym",
                                                                   "sha256": _sha256(self.image15)})
        self.assertEqual(templates.templateForStudyRecord(record).id, template.id)
        templates.removeTemplate(template.id, confirmDelete=lambda: True)
        self.assertIsNone(templates.templateForStudyRecord(record))
        # Add it… accepts only the file with the study's checksum
        dialog = self.ui.AddTemplateDialog(requiredSha256=record["sha256"], expectedName=record["name"], expectedSpace=record["space"],
                                           executor=autosave_io.InlineExecutor())
        dialog.setImage(self.las)
        dialog.addTemplate()
        self.assertIn("checksum", dialog.errorLabel.text)
        self.assertIsNone(templates.templateForStudyRecord(record))
        self.assertEqual(os.listdir(templates.templatesFolder()), [], "a refused file leaves nothing in Templates")
        dialog.setImage(self.image15)
        self.assertEqual(dialog.nameEdit.text, "Study template")
        self.assertEqual(dialog.space(), "MNI152NLin6Asym")
        dialog.addTemplate()
        self.assertIsNotNone(dialog.template)
        found = templates.templateForStudyRecord(record)
        self.assertIsNotNone(found, "found again by its checksum")
        dialog.deleteLater()

    #
    # User interface
    #

    def test_09_dialog_panel_menu_search(self):
        from StudioLib import autosave_io

        templates, ui = self.templates, self.ui
        dialog = ui.AddTemplateDialog(executor=autosave_io.InlineExecutor())
        self.assertEqual(dialog.objectName, "studioAddTemplateDialog")
        self.assertFalse(dialog.addButton.enabled)
        dialog.setImage(self.head)
        self.assertEqual(dialog.detectionLabel.text, templates.REFUSAL)
        self.assertFalse(dialog.addButton.enabled)
        dialog.setImage(self.las)
        self.assertTrue(dialog.addButton.enabled)
        self.assertEqual(dialog.space(), "MNI152NLin2009cAsym")
        self.assertIn("known grid", dialog.factsLabel.text)
        dialog.setImage(self.image15)
        self.assertEqual(dialog.nameEdit.text, "my mni 1.5mm T1w")
        self.assertEqual(dialog.space(), templates.OTHER_SPACE)
        self.assertFalse(dialog.setCompanion("mask", self.bundledMask))
        self.assertFalse(dialog.errorLabel.isHidden())
        self.assertTrue(dialog.setCompanion("mask", self.mask15))
        dialog.setSurfaces([self.surface, self.farSurface])
        self.assertEqual(dialog.surfacePaths, [self.surface])
        dialog.setNotes("Source: a test\nLicence: CC0")
        dialog.setContrast("FLAIR")
        dialog.nameEdit.setText("Dialog template")
        dialog.spaceCombo.setCurrentIndex(dialog.spaceCombo.findData("MNI152NLin2009aSym"))
        dialog.defaultCheck.setChecked(True)
        dialog.addTemplate()
        template = dialog.template
        self.assertIsNotNone(template)
        self.assertEqual(template.contrast, "FLAIR")
        self.assertEqual(template.space, "MNI152NLin2009aSym")
        self.assertEqual(template.notes, "Source: a test\nLicence: CC0")
        self.assertEqual(len(template.surfacePaths), 1)
        self.assertEqual(templates.defaultTemplateId(), template.id)
        dialog.deleteLater()
        # Settings > Templates
        panel = ui.TemplatesSettingsPanel()
        panel.refresh()
        self.assertEqual(panel.objectName, "studioTemplatesPanel")
        self.assertEqual(len(panel.rows), len(templates.listTemplates()))
        row = panel.rowFor(template.id)
        self.assertEqual(row.property("isDefault"), "true")
        self.assertIsNotNone(row.findChild(qt.QLabel, "studioTemplateDefaultLabel"))
        builtinRow = panel.rowFor(templates.BUILTIN_DEFAULT)
        builtinRow.findChild(qt.QPushButton, "studioTemplateSetDefault").click()
        self.assertEqual(templates.defaultTemplateId(), templates.BUILTIN_DEFAULT)
        self.assertTrue(panel.remove(template.id, confirm=False))
        self.assertIsNone(panel.rowFor(template.id))
        panel.deleteLater()
        # Start on template ▾
        menu = qt.QMenu()
        ui.populateTemplateMenu(menu, onStart=lambda templateId: None)
        names = [action.objectName for action in menu.actions()]
        self.assertEqual(menu.objectName, "studioTemplateMenu")
        self.assertIn("studioTemplateMenuAdd", names)
        self.assertIn("studioTemplateMenuManage", names)
        checked = [action.data() for action in menu.actions() if action.checkable and action.checked]
        self.assertEqual(checked, [templates.BUILTIN_DEFAULT])
        menu.deleteLater()
        # Search
        ids = [item["id"] for item in ui.paletteItems()]
        self.assertIn("templates.add", ids)
        self.assertIn("templates.manage", ids)

    def test_10_library_split_button(self):
        """Library > Start on template ▾ (the objectName of the button is kept)."""
        from StudioLib import library

        page = library.canvas()
        self.assertEqual(page.templateButton.objectName, "StudioLibraryStartOnTemplateButton")
        self.assertEqual(page.templateButton.text, "Start on template")
        self.assertEqual(page.templateMenuButton.objectName, "StudioLibraryTemplateMenuButton")
        self.assertIs(page.templateMenuButton.menu(), page.templateMenu)
        page._fillTemplateMenu()
        self.assertEqual(page.templateMenu.objectName, "studioTemplateMenu")
        self.assertGreaterEqual(len(page.templateMenu.actions()), 4)

    def test_11_missing_default_and_study_choice(self):
        """A default whose files are gone falls back to the bundled brain (never a download); the menu's study template adds
        a study case.
        """
        import SampleData

        from StudioLib import importer, space

        templates = self.templates
        oneMm = "builtin:mni2009c-1mm"
        sampleFile, cachedTemplatePath, downloadSample = templates._sampleFile, importer.cachedTemplatePath, SampleData.SampleDataLogic.downloadSample
        oneMmSample = templates.sampleNameOf(templates.templateById(oneMm))
        requested = []

        def download(logic, sampleName, *args, **kwargs):
            requested.append(sampleName)
            if sampleName == oneMmSample:
                raise AssertionError("the 1 mm template was downloaded without a click")
            return downloadSample(logic, sampleName, *args, **kwargs)

        # The 1 mm template was downloaded, made the default, then the cache was cleared
        templates._sampleFile = lambda name: None if name == oneMmSample else sampleFile(name)
        importer.cachedTemplatePath = lambda name=importer.TEMPLATE_SAMPLE_1MM: None if name == oneMmSample else cachedTemplatePath(name)
        SampleData.SampleDataLogic.downloadSample = download
        try:
            qt.QSettings().setValue(templates.DEFAULT_KEY, oneMm)
            self.assertFalse(templates.templateById(oneMm).isAvailable())
            self.assertEqual(templates.defaultTemplateId(), templates.BUILTIN_DEFAULT)
            volume, _segmentation = importer.startOnTemplate(shell=self._shell())
            self.assertEqual(space.spaceOf(volume), ("MNI152NLin2009cAsym", 2))
            self.assertNotIn(oneMmSample, requested)
            with self.assertRaises(RuntimeError):
                importer.startOnTemplate(shell=self._shell(), templateId=oneMm)
            self.assertNotIn(oneMmSample, requested)
        finally:
            templates._sampleFile, importer.cachedTemplatePath, SampleData.SampleDataLogic.downloadSample = sampleFile, cachedTemplatePath, downloadSample
            qt.QSettings().remove(templates.DEFAULT_KEY)
        # A study open: the default and the study's own template add a study case; another template does not
        template = self._add(name="Study own", space="MNI152NLin6Asym")
        other = self._add(name="Another", space="MNI152NLin6Asym", mask=self.mask15)

        class Session:
            def __init__(self, record):
                self.record = record

            def templateRecord(self):
                return self.record

        own = Session(templates.studyRecord(template))
        self.assertTrue(importer._isStudyTemplateChoice(own, template.id))
        self.assertTrue(importer._isStudyTemplateChoice(own, templates.BUILTIN_DEFAULT), "the default template")
        self.assertFalse(importer._isStudyTemplateChoice(own, other.id))
        builtin = Session(templates.studyRecord(templates.templateById(templates.BUILTIN_DEFAULT)))
        templates.setDefault(other.id)
        self.assertTrue(importer._isStudyTemplateChoice(builtin, templates.BUILTIN_DEFAULT), "the study's built-in template")
        self.assertTrue(importer._isStudyTemplateChoice(builtin, other.id), "the default template")
        self.assertFalse(importer._isStudyTemplateChoice(builtin, template.id))
        self.assertFalse(importer._isStudyTemplateChoice(builtin, "user:not-here"))

    #
    # A lesion on a template that is not registered here (BB-1: removed, another computer, a study without it)
    #

    def test_12_missing_template_lesion_stays_mni(self):
        import LesionNetworkMapping

        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import draw, importer, space, study_ui

        templates = self.templates
        template = self._add(name="Lab ICBM 1.5", space="MNI152NLin6Asym")
        expected = ("MNI152NLin6Asym", 1.5)
        logic = LesionNetworkMapping.LesionNetworkMappingLogic()

        def checkMni(segmentation, label):
            self.assertEqual(space.spaceOf(segmentation), expected, label)
            self.assertEqual(space.hemisphereWord(-20.0, segmentation), "Left", label)
            check = logic.checkSeed(segmentation, segmentation.GetSegmentation().GetNthSegmentID(0))
            self.assertEqual(tuple(check["space"] or ()), expected, f"{label}: Map's seed space")

        # (1) The template is removed in Settings while a lesion drawn on it is open
        volume, segmentation = importer.startOnTemplate(shell=self._shell(), templateId=template.id)
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        lesion = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
        lesion[60:70, 70:80, 30:40] = 1  # k, j, i: left hemisphere
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        checkMni(segmentation, "template registered")
        self.assertTrue(templates.removeTemplate(template.id, confirmDelete=lambda: True))
        self.assertIsNone(space.spaceOf(space.gridOf(segmentation)), "the grid alone is no longer known")
        checkMni(segmentation, "template removed while open")
        line, level = draw.DrawExtension._spaceLine(None, segmentation, volume)
        self.assertNotIn("scan space", line or "")
        self.assertNotEqual(level, "warn")
        # (2) A project reopened without the template: its volume in Images/ still says Studio.Space; the lesion has no
        # node reference to it
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        segmentation.RemoveNodeReferenceIDs(role)
        checkMni(segmentation, "volume on the same grid")
        slicer.mrmlScene.Clear(0)

        # (3) A study drawn on the template, opened where the template is not: the case opens on the built-in template
        template = self._add(name="Lab ICBM 1.5", space="MNI152NLin6Asym")
        record = templates.studyRecord(template)
        study = Study.create(os.path.join(self.tempDir, "study missing template"), name="Study", template=record)
        sid = "sub-case001"
        study.add_subject(sid, {"group": "case", "status": "traced", "space": "MNI152NLin6Asym", "trace_template": "MNI152NLin6Asym"})
        lesionPath = os.path.join(study.path, *study.lesion_relpath(sid).split("/"))
        os.makedirs(os.path.dirname(lesionPath), exist_ok=True)
        _data, affine, _header = nifti.read_nifti(template.imagePath)
        mask = np.zeros(self.shape15, np.uint8)
        mask[30:40, 70:80, 60:70] = 1  # i, j, k: left hemisphere
        nifti.write_nifti(lesionPath, mask, affine)
        study.set(sid, {"lesion": study.relpath(lesionPath)})
        study.save()
        self.assertTrue(templates.removeTemplate(template.id, confirmDelete=lambda: True))
        settings = qt.QSettings()
        recent = settings.value(study_ui.RECENT_STUDIES_KEY)
        try:
            session = study_ui.openStudy(study.path, shell=self._shell(), subject=sid, showLibrary=False)
            slicer.app.processEvents()
            self.assertIsNone(session.volumeNode().GetAttribute("Studio.TemplateId"), "the built-in template meanwhile")
            checkMni(session.segmentationNode(), "study case without its template")
        finally:
            study_ui.closeStudy()
            if recent is None:
                settings.remove(study_ui.RECENT_STUDIES_KEY)
            else:
                settings.setValue(study_ui.RECENT_STUDIES_KEY, recent)
            shutil.rmtree(study.path, ignore_errors=True)

    def test_13_surfaces_without_coordinate_header_are_mni_ras(self):
        from StudioLib import importer

        templates = self.templates
        folder = os.path.join(self.tempDir, "headerless surfaces")
        os.makedirs(folder, exist_ok=True)

        def ellipsoid(low, high):
            source = vtk.vtkSphereSource()
            source.SetThetaResolution(24)
            source.SetPhiResolution(24)
            source.SetRadius(0.5)
            transform = vtk.vtkTransform()
            transform.Translate(*[(a + b) / 2.0 for a, b in zip(low, high, strict=True)])
            transform.Scale(*[b - a for a, b in zip(low, high, strict=True)])
            moved = vtk.vtkTransformPolyDataFilter()
            moved.SetTransform(transform)
            moved.SetInputConnection(source.GetOutputPort())
            triangles = vtk.vtkTriangleFilter()
            triangles.SetInputConnection(moved.GetOutputPort())
            triangles.Update()
            return triangles.GetOutput()

        def writeObj(path, polyData, comment=None):
            # As FreeSurfer, BrainNet or nibabel exports write it: no coordinate system comment unless given
            with open(path, "w", encoding="ascii") as f:
                if comment:
                    f.write(f"# {comment}\n")
                for index in range(polyData.GetNumberOfPoints()):
                    f.write("v {:.4f} {:.4f} {:.4f}\n".format(*polyData.GetPoint(index)))
                ids = vtk.vtkIdList()
                polys = polyData.GetPolys()
                polys.InitTraversal()
                while polys.GetNextCell(ids):
                    f.write("f " + " ".join(str(ids.GetId(i) + 1) for i in range(ids.GetNumberOfIds())) + "\n")
            return path

        def writeWith(writer, path, polyData):
            writer.SetFileName(path)
            writer.SetInputData(polyData)
            writer.Write()
            return path

        # A left hemisphere in MNI (RAS) coordinates, without a coordinate system comment, as .obj, .stl and legacy .vtk
        hemisphere = ellipsoid((-68.0, -100.0, -44.0), (-4.0, 68.0, 76.0))
        headerless = [writeObj(os.path.join(folder, "lh.pial.obj"), hemisphere),
                      writeWith(vtk.vtkSTLWriter(), os.path.join(folder, "lh.pial.stl"), hemisphere),
                      writeWith(vtk.vtkPolyDataWriter(), os.path.join(folder, "lh.pial.vtk"), hemisphere)]
        for path in headerless:
            self.assertEqual(templates.checkSurface(path), (True, ""), f"{os.path.basename(path)}: MNI coordinates are RAS")
            bounds = templates._readSurfaceBounds(path)
            self.assertLess(bounds[1], 0.0, f"{os.path.basename(path)} stays in the left hemisphere")
            self.assertLess(bounds[2], -90.0, f"{os.path.basename(path)} keeps its occipital pole at the back")
        # A file that states its coordinate system is still read in it: the same hemisphere stored as LPS
        flipped = vtk.vtkTransformPolyDataFilter()
        lpsTransform = vtk.vtkTransform()
        lpsTransform.Scale(-1.0, -1.0, 1.0)
        flipped.SetTransform(lpsTransform)
        flipped.SetInputData(hemisphere)
        flipped.Update()
        lps = writeObj(os.path.join(folder, "lh.pial_lps.obj"), flipped.GetOutput(), comment="exported SPACE=LPS")
        self.assertEqual(templates.checkSurface(lps), (True, ""))
        self.assertLess(templates._readSurfaceBounds(lps)[1], 0.0)

        # A small left-anterior structure on a template: its model is where the file says, not mirrored
        blob = writeObj(os.path.join(folder, "left_blob.obj"), ellipsoid((-55.0, 5.0, 5.0), (-25.0, 35.0, 35.0)))
        self.assertEqual(templates.checkSurface(blob), (True, ""))
        template = self._add(surfaces=(blob,), space="MNI152NLin6Asym")
        importer.startOnTemplate(shell=self._shell(), templateId=template.id)
        models = [node for node in slicer.util.getNodesByClass("vtkMRMLModelNode") if node.GetAttribute("Studio.Role") == templates.ROLE_SURFACE]
        self.assertEqual(len(models), 1)
        bounds = [0.0] * 6
        models[0].GetRASBounds(bounds)
        self.assertTrue(-56.0 < bounds[0] < bounds[1] < -24.0, f"left, not mirrored: {bounds}")
        self.assertTrue(4.0 < bounds[2] < bounds[3] < 36.0, f"anterior, not mirrored: {bounds}")
        # Saved with the project as Slicer writes models (with a coordinate system header), so official Slicer reads it unmirrored
        path = os.path.join(folder, "saved_blob.vtk")
        self.assertTrue(slicer.util.saveNode(models[0], path))
        self.assertTrue(-56.0 < templates._readSurfaceBounds(path)[1] < -24.0)
        shutil.rmtree(folder, ignore_errors=True)

    @staticmethod
    def _width(size):
        size = size() if callable(size) else size
        return size.width()

    def test_14_long_names_are_elided(self):
        """A long template name or file name is elided ("…", the whole text as tooltip) and does not widen the Settings list or the add dialog."""
        templates, ui = self.templates, self.ui
        longName = "MNI152 ICBM 2009c Nonlinear Asymmetric 0.5 mm T1-weighted skull-stripped from the McGill BIC website " * 2
        panel = ui.TemplatesSettingsPanel()
        panel.refresh()
        before = self._width(panel.rowFor(templates.BUILTIN_DEFAULT).minimumSizeHint)
        template = self._add(name=longName)
        panel.refresh()
        row = panel.rowFor(template.id)
        nameLabel = row.findChild(qt.QLabel, "studioTemplateName")
        self.assertIn("…", nameLabel.text)
        self.assertIn(longName.strip(), nameLabel.toolTip.replace("</p>", ""))  # Studio wraps tool tips in rich text
        self.assertLessEqual(self._width(nameLabel.sizeHint), ui.NAME_WIDTH + 2)
        self.assertLessEqual(self._width(row.minimumSizeHint), before + ui.NAME_WIDTH)
        shortRow = panel.rowFor(templates.BUILTIN_DEFAULT)
        self.assertNotIn("…", shortRow.findChild(qt.QLabel, "studioTemplateName").text)
        self.assertEqual(shortRow.findChild(qt.QLabel, "studioTemplateName").toolTip, "")
        panel.deleteLater()
        dialog = ui.AddTemplateDialog()
        longFile = os.path.join(self.tempDir, "mni_icbm152_t1_tal_nlin_asym_09c_0.5mm_skull_stripped_brain_extracted_version_2009_final.nii.gz")
        shutil.copy(self.image15, longFile)
        dialog.setImage(longFile)
        self.assertIn("…", dialog.fileLabel.text)
        self.assertTrue(dialog.fileLabel.text.endswith(".nii.gz"))
        self.assertIn(longFile, dialog.fileLabel.toolTip)
        self.assertLessEqual(self._width(dialog.fileLabel.sizeHint), ui.FILE_NAME_WIDTH + 2)
        dialog.deleteLater()
        os.remove(longFile)

    def test_15_sform_qform_disagreement_is_refused(self):
        """A NIfTI file whose sform (MNI) and qform disagree is refused: the check reads the sform, while Slicer loads the
        qform, so lesions drawn on it would be treated as MNI at the wrong place. A template added before the check (its
        image changed afterwards) is not loaded either.
        """
        import struct

        from LesionNetworkMappingLib import nifti

        from StudioLib import importer

        templates = self.templates
        data, affine, _header = nifti.read_nifti(self.image15)
        agreeing = os.path.join(self.tempDir, "agreeing_1.5mm.nii")
        nifti.write_nifti(agreeing, data, affine)
        disagreeing = os.path.join(self.tempDir, "disagreeing_1.5mm.nii")
        shutil.copy(agreeing, disagreeing)
        header = nifti.read_nifti_header(disagreeing)
        endian = header["byteorder"]
        with open(disagreeing, "r+b") as f:
            f.seek(268)  # qoffset_x: the qform moves 40 mm to the right, the sform (MNI, code 4) stays
            (qoffsetX,) = struct.unpack(endian + "f", f.read(4))
            f.seek(268)
            f.write(struct.pack(endian + "f", qoffsetX + 40.0))
        header = nifti.read_nifti_header(disagreeing)
        self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4))
        self.assertAlmostEqual(nifti.xform_disagreement(header), 40.0, places=3)
        detection = templates.identify(disagreeing)
        self.assertFalse(detection.ok)
        self.assertEqual(detection.status, "refused")
        self.assertIn("sform and qform", detection.message)
        self.assertAlmostEqual(templates.readHeader(agreeing)["xformMm"], 0.0, places=3)
        with self.assertRaises(templates.TemplateError):
            self._add(disagreeing, name="Disagreeing")
        self.assertFalse(os.path.exists(templates.templatesFolder()) and os.listdir(templates.templatesFolder()), "a refused add leaves nothing")
        # As a brain mask of an agreeing template
        ok, reason = templates.checkCompanion(templates.identify(agreeing), disagreeing)
        self.assertFalse(ok)
        self.assertIn("sform and qform", reason)
        # A template added from an agreeing file whose image was replaced by the disagreeing one is not loaded at the qform
        template = self._add(agreeing, name="Agreeing")
        shutil.copy(disagreeing, template.imagePath)
        with self.assertRaises(RuntimeError) as context:
            importer.startOnTemplate(shell=self._shell(), templateId=template.id)
        self.assertIn("sform and qform", str(context.exception))
        self.assertFalse([node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode") if node.GetAttribute("Studio.TemplateId") == template.id])
        for path in (agreeing, disagreeing):
            os.remove(path)

    def test_16_known_grid_template_space_wins_for_lesions(self):
        """A template on a built-in grid (the bundled 2 mm grid, stored LAS) added with another space: a lesion drawn on
        it is in the space chosen for the template (Draw's notice, the sidecar, Map's seed space and family check), not
        in the grid's built-in space. A lesion on that grid with no tagged volume keeps the built-in space.
        """
        import LesionNetworkMapping

        from StudioLib import draw, importer, space

        self.assertEqual(self.templates.identify(self.las).status, "known")
        template = self._add(self.las, name="SPM-like 2 mm", space="MNI152Lin")
        expected = ("MNI152Lin", 2)
        volume, segmentation = importer.startOnTemplate(shell=self._shell(), templateId=template.id)
        self.assertEqual(space.spaceOf(volume), expected)
        self.assertEqual(space.spaceOf(space.gridOf(segmentation)), ("MNI152NLin2009cAsym", 2), "the grid alone is the built-in grid")
        segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
        lesion = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
        lesion[30:40, 50:60, 25:35] = 1  # k, j, i
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentation, segmentId, volume)
        self.assertEqual(space.spaceOf(segmentation), expected)
        self.assertEqual(space.sharedGridSpace(segmentation), expected)
        line, level = draw.DrawExtension._spaceLine(None, segmentation, volume)
        self.assertEqual((line, level), (f"On {template.name} · MNI152Lin 2 mm", "neutral"))
        sidecar = draw.writeSidecar(os.path.join(self.tempDir, "lesion_known_grid.nii.gz"), segmentation, volume)
        with open(sidecar, encoding="utf-8") as f:
            content = json.load(f)
        os.remove(sidecar)
        self.assertEqual(content["Space"], "MNI152Lin")
        self.assertEqual(content["Template"]["space"], "MNI152Lin")
        logic = LesionNetworkMapping.LesionNetworkMappingLogic()
        role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
        for label in ("drawn on the template", "no node reference (a reopened project)"):
            check = logic.checkSeed(segmentation, segmentId)
            self.assertEqual(tuple(check["space"] or ()), expected, f"{label}: Map's seed space")
            self.assertIn("a few millimetres", logic.familyNotice(check["space"], "MNI152NLin2009cAsym"), label)
            segmentation.RemoveNodeReferenceIDs(role)
        self.assertEqual(space.spaceOf(segmentation), expected)
        # Without the tagged template volume, a lesion on the grid is in the grid's built-in space
        slicer.mrmlScene.RemoveNode(volume)
        self.assertEqual(space.spaceOf(segmentation), ("MNI152NLin2009cAsym", 2))
        self.assertIsNone(space.sharedGridSpace(segmentation))

    def test_17_readded_same_name_loads_its_own_image(self):
        """A template removed in Settings and another image added under the same name (the same id "user:<slug>"): Start on
        template loads the new image, never the removed template's volume that is still in the scene (also when that volume
        carries the id again, as in a project reopened after the change).
        """
        from StudioLib import draw, importer, space

        templates = self.templates
        shell = self._shell()
        first = self._add(name="Swap", surfaces=(self.surface,))
        oldVolume, _segmentation = importer.startOnTemplate(shell=shell, templateId=first.id)
        self.assertEqual(tuple(oldVolume.GetImageData().GetDimensions()), self.shape15)
        oldSpace = oldVolume.GetAttribute("Studio.Space")
        surfaces = [node for node in slicer.util.getNodesByClass("vtkMRMLModelNode") if node.GetAttribute("Studio.TemplateId") == first.id]
        self.assertEqual(len(surfaces), 1)
        self.assertTrue(templates.removeTemplate(first.id, confirmDelete=lambda: True))
        self.assertIsNotNone(slicer.mrmlScene.GetNodeByID(oldVolume.GetID()), "the removed template's volume stays in the scene")
        self.assertEqual(oldVolume.GetAttribute("Studio.Space"), oldSpace, "and keeps its space")
        self.assertEqual(oldVolume.GetAttribute("Studio.Role"), "template")
        self.assertIsNone(surfaces[0].GetAttribute("Studio.TemplateId"))
        second = self._add(self.las, name="Swap", space="MNI152NLin6Asym")
        self.assertEqual(second.id, first.id)
        volume, segmentation = importer.startOnTemplate(shell=shell, templateId=second.id)
        self.assertNotEqual(volume.GetID(), oldVolume.GetID())
        self.assertEqual(sorted(volume.GetImageData().GetDimensions()), sorted(self.bundledShape[:3]))
        self.assertEqual(volume.GetAttribute("Studio.TemplateId"), second.id)
        self.assertEqual(space.spaceOf(volume), ("MNI152NLin6Asym", 2))
        self.assertIsNone(templates.templateOfNode(oldVolume))
        line, _level = draw.DrawExtension._spaceLine(None, segmentation, volume)
        self.assertEqual(line, f"On {second.name} · MNI152NLin6Asym 2 mm")
        self.assertEqual(importer.loadTemplateVolume(second).GetID(), volume.GetID(), "the new volume is reused")
        # The removed template's volume tagged with the id again (a project reopened after the change): not the new template
        slicer.mrmlScene.RemoveNode(volume)
        oldVolume.SetAttribute("Studio.TemplateId", second.id)
        self.assertIsNone(templates.templateOfNode(oldVolume))
        volume = importer.loadTemplateVolume(second)
        self.assertNotEqual(volume.GetID(), oldVolume.GetID())
        self.assertEqual(sorted(volume.GetImageData().GetDimensions()), sorted(self.bundledShape[:3]))
        self.assertIsNone(oldVolume.GetAttribute("Studio.TemplateId"), "the stale volume no longer carries the id")
        self.assertEqual(templates.templateOfNode(volume).id, second.id)
