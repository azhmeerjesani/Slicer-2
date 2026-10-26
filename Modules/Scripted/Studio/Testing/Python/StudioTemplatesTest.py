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
