"""Export as NIfTI (A-11, decision D-N1): orientation, headers, sidecars, segmentations, maps, projects and the dialog.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioNiftiExportTest'); slicer.app.exit(0)"

Every file written is read back with nibabel when it is importable (it is only used here, never by the product) and
always with ``LesionNetworkMappingLib.nifti``. The core check is laterality: a marker drawn in the subject's LEFT
hemisphere must land at negative RAS x in the file, whatever the storage order of the volume (RAS, LPS, LAS on the FSL
MNI grid, oblique). Nothing is downloaded; the Slicer Studio folder is a temporary folder (``SLICER_STUDIO_PROJECTS_ROOT``).
"""

import json
import os
import shutil
import tempfile
import unittest

import numpy as np
import qt
import vtk

import slicer

try:
    import nibabel
except ImportError:
    nibabel = None


def _matrix(array):
    matrix = vtk.vtkMatrix4x4()
    for row in range(4):
        for column in range(4):
            matrix.SetElement(row, column, float(array[row][column]))
    return matrix


def _ijkToRas(node):
    matrix = vtk.vtkMatrix4x4()
    node.GetIJKToRASMatrix(matrix)
    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _allFiles(root):
    found = []
    for directory, _folders, files in os.walk(root):
        found += [os.path.join(directory, name) for name in files]
    return sorted(found)


class StudioNiftiExportTest(unittest.TestCase):
    """``StudioLib.niftiexport`` (engine) and ``StudioLib.niftiexport_ui`` (dialog and entry points)."""

    @classmethod
    def setUpClass(cls):
        cls.tempDir = tempfile.mkdtemp(prefix="StudioNiftiExportTest-")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tempDir, ignore_errors=True)

    def setUp(self):
        from StudioLib import niftiexport, projects

        self.export = niftiexport
        slicer.mrmlScene.Clear(0)
        self.out = tempfile.mkdtemp(prefix="out ", dir=self.tempDir)
        settings = qt.QSettings()
        self._settings = {key: settings.value(key) for key in (niftiexport.LAST_FOLDER_KEY, niftiexport.FORMAT_KEY, niftiexport.SEGMENTATION_MODE_KEY)}
        self.root = os.path.join(self.tempDir, "Slicer Studio tést")
        self._environment = {name: os.environ.get(name) for name in (projects.ROOT_ENVIRONMENT, projects.ENABLED_ENVIRONMENT)}
        os.environ[projects.ROOT_ENVIRONMENT] = self.root
        os.environ.pop(projects.ENABLED_ENVIRONMENT, None)

    def tearDown(self):
        from StudioLib import autosave_io, niftiexport_ui, projects

        dialog = niftiexport_ui.currentDialog()
        if dialog is not None:
            dialog.close()
        projects.setCurrentProject(None)
        autosave_io.backgroundWriter().drain(30.0)
        slicer.mrmlScene.Clear(0)
        settings = qt.QSettings()
        for key, value in self._settings.items():
            if value is None:
                settings.remove(key)
            else:
                settings.setValue(key, value)
        for name, value in self._environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        slicer.app.processEvents()

    def runTest(self):
        for name in sorted(name for name in dir(self) if name.startswith("test_")):
            self.setUp()
            try:
                getattr(self, name)()
            finally:
                self.tearDown()

    #
    # Helpers
    #

    def _volume(self, name, array_kji, ijkToRas, className="vtkMRMLScalarVolumeNode"):
        node = slicer.mrmlScene.AddNewNodeByClass(className, name)
        slicer.util.updateVolumeFromArray(node, array_kji)
        node.SetIJKToRASMatrix(_matrix(ijkToRas))
        node.CreateDefaultDisplayNodes()
        return node

    def _read(self, path):
        """``(data [i, j, k], affine, header)`` read with LesionNetworkMappingLib.nifti (and checked against nibabel)."""
        from LesionNetworkMappingLib import nifti

        data, affine, header = nifti.read_nifti(path)
        if nibabel is not None:
            image = nibabel.load(path)
            np.testing.assert_allclose(image.affine, affine, atol=1e-5)
            np.testing.assert_array_equal(np.asarray(image.dataobj), data)
            self.assertEqual(int(image.header["sform_code"]), header["sform_code"])
            self.assertEqual(int(image.header["qform_code"]), header["qform_code"])
            np.testing.assert_allclose(image.header.get_qform(), image.header.get_sform(), atol=1e-4)
        return data, affine, header

    def _sidecar(self, path):
        with open(self.export.sidecarPath(path), encoding="utf-8") as f:
            return json.load(f)

    def _markerRas(self, path):
        """The RAS position (world mm) of the brightest voxel of a file, as a NIfTI reader places it."""
        data, affine, _header = self._read(path)
        index = np.unravel_index(int(np.argmax(data)), data.shape)
        return (affine @ np.array([*index, 1.0]))[:3]

    def _assertNoAbsolutePaths(self, folder):
        """No sidecar, label table or README holds an absolute path (CLAUDE.md: shared files)."""
        home = os.path.expanduser("~")
        for path in _allFiles(folder):
            if path.lower().endswith((".nii", ".nii.gz")):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            for forbidden in (self.tempDir, home + os.sep, "/tmp/", "/home/", "/Users/", "C:\\"):
                self.assertNotIn(forbidden, text, f"{os.path.basename(path)} holds an absolute path")

    def _phantom(self, name, ijkToRas, shape=(21, 21, 21), markerRas=(-6.0, 2.0, 4.0)):
        """A volume whose single bright voxel is at ``markerRas`` (the subject's LEFT when x < 0)."""
        array = np.zeros(shape[::-1], np.int16)
        ijk = np.linalg.solve(np.asarray(ijkToRas, float), np.array([*markerRas, 1.0]))[:3]
        i, j, k = (round(value) for value in ijk)
        self.assertTrue(all(0 <= index < size for index, size in zip((i, j, k), shape, strict=True)), "marker inside the grid")
        array[k, j, i] = 1000
        return self._volume(name, array, ijkToRas)

    #
    # Tests
    #

    def test_01_scalar_volumes_keep_type_and_geometry(self):
        """int16 and float32 scans: data type, voxels and affine (= IJK to RAS) kept; codes 1; window/level not applied."""
        k, j, i = np.indices((6, 5, 4))
        for dtype in (np.int16, np.float32):
            array = ((i * 3 + j * 5 + k * 7) % 41).astype(dtype) - 7
            ijkToRas = np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 20], [0, 0, 1.2, -30], [0, 0, 0, 1]])
            node = self._volume(f"scan_{np.dtype(dtype).name}", array, ijkToRas)
            node.GetDisplayNode().SetAutoWindowLevel(False)
            node.GetDisplayNode().SetWindowLevel(5, 2)
            path = os.path.join(self.out, f"scan_{np.dtype(dtype).name}.nii.gz")
            result = self.export.exportVolume(node, path)
            self.assertEqual(result.errors, [])
            data, affine, header = self._read(path)
            self.assertEqual(data.dtype, np.dtype(dtype))
            np.testing.assert_array_equal(data, array.transpose(2, 1, 0))
            np.testing.assert_allclose(affine, ijkToRas, atol=1e-6)
            self.assertEqual((header["sform_code"], header["qform_code"]), (1, 1))
            self.assertEqual(header["xyzt_units"], 10)
            sidecar = self._sidecar(path)
            for key in ("GeneratedBy", "CreationDate", "SourceNode", "Space", "SformCode", "SpatialUnits", "Units", "DataType"):
                self.assertIn(key, sidecar)
            self.assertEqual(sidecar["Space"], "scanner")
            self.assertEqual(sidecar["SourceNode"], node.GetName())
            self.assertTrue(sidecar["GeneratedBy"].startswith("Slicer Studio"))
        # The scene is unchanged: no node was added
        self.assertEqual(len(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")), 2)
        self._assertNoAbsolutePaths(self.out)

    def test_02_left_is_left(self):
        """A marker in the LEFT hemisphere lands at negative x in the file for RAS, LPS, LAS and oblique storage orders."""
        transform = vtk.vtkTransform()
        transform.RotateX(17)
        transform.RotateZ(-31)
        transform.PostMultiply()
        transform.Translate(1.5, -2.25, 3.0)
        oblique = np.array([[transform.GetMatrix().GetElement(row, column) for column in range(4)] for row in range(4)])
        oblique[:3, 3] -= oblique[:3, :3] @ np.array([10.0, 10.0, 10.0])
        orders = {
            "RAS": np.array([[1.0, 0, 0, -10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]),
            "LPS": np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]]),
            "LAS": np.array([[-1.0, 0, 0, 10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]),
            "left-handed": np.array([[0, 0, -1.0, 10], [0, 1.0, 0, -10], [1.0, 0, 0, -10], [0, 0, 0, 1]]),
            "oblique": oblique,
        }
        for order, ijkToRas in orders.items():
            node = self._phantom(f"phantom {order}", ijkToRas)
            path = os.path.join(self.out, f"phantom_{order}.nii.gz")
            self.assertEqual(self.export.exportVolume(node, path).errors, [])
            marker = self._markerRas(path)
            self.assertLess(marker[0], 0.0, f"{order}: the left marker is at x = {marker[0]}")
            np.testing.assert_allclose(marker, (-6.0, 2.0, 4.0), atol=0.75, err_msg=order)
            if nibabel is not None:
                # FSL convention check (as fslorient would say): the x axis code of the qform names the patient's side
                codes = nibabel.aff2axcodes(nibabel.load(path).affine)
                expected = nibabel.aff2axcodes(ijkToRas)
                self.assertEqual(codes, expected, order)
        # The same on the FSL MNI152 2 mm grid (stored LAS): marker at x = -40 mm, sform/qform code 4 (MNI)
        mni = np.array([[-2.0, 0, 0, 90], [0, 2.0, 0, -126], [0, 0, 2.0, -72], [0, 0, 0, 1]])
        node = self._phantom("phantom MNI", mni, shape=(91, 109, 91), markerRas=(-40.0, -20.0, 10.0))
        path = os.path.join(self.out, "phantom_MNI.nii.gz")
        self.assertEqual(self.export.exportVolume(node, path).errors, [])
        np.testing.assert_allclose(self._markerRas(path), (-40.0, -20.0, 10.0), atol=1e-4)
        _data, _affine, header = self._read(path)
        self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4))
        self.assertEqual(header["shape"], (91, 109, 91))
        self.assertEqual(self._sidecar(path)["Space"], "MNI152NLin6Asym")
        # A lesion drawn in the left hemisphere, exported as a mask: also at negative x
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Left lesion")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(node)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion")
        mask = (slicer.util.arrayFromVolume(node) > 0).astype(np.uint8)
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentation, segmentId, node)
        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "left_lesion.nii.gz"), mode=self.export.SEGMENTATION_MASKS)
        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.images), 1)
        np.testing.assert_allclose(self._markerRas(result.images[0]), (-40.0, -20.0, 10.0), atol=1e-4)

    def test_03_oblique_and_dicom_derived(self):
        """An oblique, DICOM-derived volume keeps its full direction matrix (no resampling); its name comes from the series."""
        transform = vtk.vtkTransform()
        transform.RotateY(23)
        transform.RotateX(-11)
        transform.Scale(0.9, 0.9, 3.0)
        transform.PostMultiply()
        transform.Translate(-80.0, -100.0, -40.0)
        ijkToRas = np.array([[transform.GetMatrix().GetElement(row, column) for column in range(4)] for row in range(4)])
        array = (np.arange(8 * 9 * 10).reshape(8, 9, 10) % 300).astype(np.int16)
        node = self._volume("3: T2 FLAIR AX", array, ijkToRas)
        node.SetAttribute("DICOM.instanceUIDs", "1.2.3.4.5 1.2.3.4.6")
        self.assertEqual(self.export.dicomNodes(), [node])
        jobs = self.export.planDicom(self.out)
        self.assertEqual(len(jobs), 1)
        result = self.export.runJobs(jobs)
        self.assertEqual(result.errors, [])
        path = result.images[0]
        self.assertEqual(os.path.basename(path), self.export.dicomFileName(node) + ".nii.gz")
        data, affine, header = self._read(path)
        np.testing.assert_allclose(affine, ijkToRas, atol=1e-5)
        self.assertEqual(data.shape, (10, 9, 8))
        np.testing.assert_array_equal(data, array.transpose(2, 1, 0))
        self.assertEqual(header["sform_code"], 1)
        sidecar = self._sidecar(path)
        self.assertTrue(sidecar["DicomDerived"])
        for forbidden in ("PatientName", "PatientID", "StudyDate", "AcquisitionDate"):
            self.assertNotIn(forbidden, sidecar)
        slicer.mrmlScene.Clear(0)
        self._realDicomSeries()

    def _realDicomSeries(self):
        """A real (synthetic, pydicom-written) oblique DICOM series loaded by the DICOM module: a marker on the patient's LEFT
        (LPS x > 0) is at RAS x < 0 in the exported file, the series description names it, no patient data in the sidecar.
        """
        try:
            import pydicom
            from pydicom.dataset import FileDataset, FileMetaDataset
            from pydicom.uid import ExplicitVRLittleEndian, generate_uid
        except ImportError:
            return
        from DICOMLib import DICOMUtils

        folder = os.path.join(self.tempDir, "dicom series")
        os.makedirs(folder, exist_ok=True)
        angle = np.deg2rad(20.0)
        rowDirection = np.array([np.cos(angle), np.sin(angle), 0.0])  # LPS direction of increasing column index
        columnDirection = np.array([-np.sin(angle), np.cos(angle), 0.0])  # LPS direction of increasing row index
        normal = np.cross(rowDirection, columnDirection)
        origin = np.array([-30.0, -30.0, -18.0])
        rows, columns, slices, spacing, thickness = 32, 32, 12, 2.0, 3.0
        marker = (8, 26, 5)  # (row, column, slice)
        markerLps = origin + marker[2] * thickness * normal + marker[1] * spacing * rowDirection + marker[0] * spacing * columnDirection
        self.assertGreater(markerLps[0], 0.0, "the marker is on the patient's left (LPS x > 0)")
        studyUid, seriesUid, frameUid = generate_uid(), generate_uid(), generate_uid()
        for number in range(slices):
            meta = FileMetaDataset()
            meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
            meta.MediaStorageSOPInstanceUID = generate_uid()
            meta.TransferSyntaxUID = ExplicitVRLittleEndian
            dataset = FileDataset(os.path.join(folder, f"slice{number:02d}.dcm"), {}, file_meta=meta, preamble=b"\0" * 128)
            dataset.SOPClassUID = meta.MediaStorageSOPClassUID
            dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
            dataset.PatientName = "Export^Test"
            dataset.PatientID = "NIFTI001"
            dataset.StudyInstanceUID, dataset.SeriesInstanceUID, dataset.FrameOfReferenceUID = studyUid, seriesUid, frameUid
            dataset.StudyDate = "20260101"
            dataset.SeriesDescription = "T1 MPRAGE oblique"
            dataset.Modality = "MR"
            dataset.SeriesNumber = 3
            dataset.InstanceNumber = number + 1
            dataset.ImagePositionPatient = [float(value) for value in origin + number * thickness * normal]
            dataset.ImageOrientationPatient = [float(value) for value in (*rowDirection, *columnDirection)]
            dataset.PixelSpacing = [spacing, spacing]
            dataset.SliceThickness = thickness
            dataset.Rows, dataset.Columns = rows, columns
            dataset.SamplesPerPixel = 1
            dataset.PhotometricInterpretation = "MONOCHROME2"
            dataset.BitsAllocated = dataset.BitsStored = 16
            dataset.HighBit = 15
            dataset.PixelRepresentation = 0
            pixels = np.full((rows, columns), 10, np.uint16)
            if number == marker[2]:
                pixels[marker[0], marker[1]] = 4000
            dataset.PixelData = pixels.tobytes()
            pydicom.dcmwrite(os.path.join(folder, f"slice{number:02d}.dcm"), dataset, enforce_file_format=True)
        # A temporary database; the original one is restored as it was (TemporaryDICOMDatabase would open the original
        # setting, "." under --testing, and create a database in the current folder)
        settings = qt.QSettings()
        settingsKey = slicer.dicomDatabaseDirectorySettingsKey
        originalSetting = settings.value(settingsKey)
        originalOpen = bool(slicer.dicomDatabase.isOpen)
        DICOMUtils.openTemporaryDatabase(os.path.join(self.tempDir, "dicom database"))
        try:
            database = slicer.dicomDatabase
            DICOMUtils.importDicom(folder, database)
            loaded = DICOMUtils.loadSeriesByUID([seriesUid])
            self.assertTrue(loaded)
            volume = slicer.mrmlScene.GetNodeByID(loaded[0])
            self.assertIn(volume, self.export.dicomNodes())
            result = self.export.runJobs(self.export.planDicom(self.out))
            self.assertEqual(result.errors, [])
            path = result.images[0]
            self.assertEqual(os.path.basename(path), "003_T1_MPRAGE_oblique.nii.gz")
            ras = self._markerRas(path)
            np.testing.assert_allclose(ras, (-markerLps[0], -markerLps[1], markerLps[2]), atol=1e-3)
            self.assertLess(ras[0], 0.0)
            _data, affine, _header = self._read(path)
            np.testing.assert_allclose(affine, _ijkToRas(volume), atol=1e-5)
            sidecar = self._sidecar(path)
            self.assertEqual((sidecar["SeriesDescription"], sidecar["SeriesNumber"], sidecar["Modality"]), ("T1 MPRAGE oblique", 3, "MR"))
            text = json.dumps(sidecar)
            for forbidden in ("Export^Test", "NIFTI001", "20260101"):
                self.assertNotIn(forbidden, text)
        finally:
            if slicer.dicomDatabase.isOpen:
                slicer.dicomDatabase.closeDatabase()
            if originalSetting:
                settings.setValue(settingsKey, originalSetting)
                # Opened again only if it was open (under --testing the setting is "." and nothing was opened yet)
                if originalOpen and os.path.isdir(str(originalSetting)):
                    DICOMUtils.openDatabase(str(originalSetting))
            else:
                settings.remove(settingsKey)

    def test_04_label_map_and_segmentation(self):
        """Label maps keep their values; segmentations as one label map (1..N, .tsv and ITK-SNAP tables) or one mask per
        segment; empty segments are skipped with a note.
        """
        ijkToRas = np.array([[-1.0, 0, 0, 20], [0, -1.0, 0, 20], [0, 0, 1.0, -10], [0, 0, 0, 1]])
        labels = np.zeros((12, 14, 16), np.uint8)
        labels[2:4, 3:5, 4:6] = 3
        labels[6:8, 7:9, 8:10] = 7
        labelmap = self._volume("labels", labels, ijkToRas, "vtkMRMLLabelMapVolumeNode")
        result = self.export.exportVolume(labelmap, os.path.join(self.out, "labels.nii.gz"))
        data, affine, _header = self._read(result.images[0])
        self.assertEqual(data.dtype, np.uint8)
        np.testing.assert_array_equal(data, labels.transpose(2, 1, 0))
        self.assertEqual([entry["Value"] for entry in self._sidecar(result.images[0])["Labels"]], [3, 7])

        scan = self._volume("scan", np.zeros((12, 14, 16), np.int16), ijkToRas)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (scan)")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        names = ("Left lesion", "Right lesion", "Empty")
        ids = [segmentation.GetSegmentation().AddEmptySegment("", name) for name in names]
        first = np.zeros((12, 14, 16), np.uint8)
        first[2:5, 3:6, 4:7] = 1
        second = np.zeros((12, 14, 16), np.uint8)
        second[6:9, 7:10, 8:12] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(first, segmentation, ids[0], scan)
        slicer.util.updateSegmentBinaryLabelmapFromArray(second, segmentation, ids[1], scan)
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()

        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "lesions.nii.gz"))
        self.assertEqual(result.errors, [])
        self.assertTrue(any("Empty" in note for note in result.notes), result.notes)
        data, affine, _header = self._read(result.images[0])
        np.testing.assert_allclose(affine, ijkToRas, atol=1e-6)
        np.testing.assert_array_equal(data, (first + 2 * second).transpose(2, 1, 0))
        sidecar = self._sidecar(result.images[0])
        self.assertEqual([(entry["Value"], entry["Name"]) for entry in sidecar["Labels"]], [(1, "Left lesion"), (2, "Right lesion")])
        base = self.export.splitExtension(result.images[0])[0]
        with open(base + ".tsv", encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines()[:3], ["index\tname\tcolor", f"1\tLeft lesion\t{sidecar['Labels'][0]['Color']}",
                                                         f"2\tRight lesion\t{sidecar['Labels'][1]['Color']}"])
        with open(base + ".txt", encoding="utf-8") as f:
            text = f.read()
        self.assertIn('"Left lesion"', text)
        self.assertIn("IDX -R- -G- -B- -A-- VIS MSH LABEL", text)

        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "lesions.nii.gz"), mode=self.export.SEGMENTATION_MASKS)
        self.assertEqual([os.path.basename(path) for path in result.images], ["lesions_label-Leftlesion_mask.nii.gz", "lesions_label-Rightlesion_mask.nii.gz"])
        for path, expected, name in zip(result.images, (first, second), names, strict=False):
            data, affine, _header = self._read(path)
            self.assertEqual(data.dtype, np.uint8)
            np.testing.assert_array_equal(data, expected.transpose(2, 1, 0))
            sidecar = self._sidecar(path)
            self.assertEqual(sidecar["Segment"], name)
            self.assertEqual(sidecar["VoxelCount"], int(expected.sum()))
        # No node was left in the scene
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore)

        # Resampled onto a reference image (nearest neighbour): the reference grid, the same world voxels
        reference = self._volume("reference 0.5 mm", np.zeros((24, 28, 32), np.int16),
                                 np.array([[-0.5, 0, 0, 20.25], [0, -0.5, 0, 20.25], [0, 0, 0.5, -10.25], [0, 0, 0, 1]]))
        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "resampled.nii.gz"), mode=self.export.SEGMENTATION_MASKS,
                                                reference=reference)
        data, affine, _header = self._read(result.images[0])
        np.testing.assert_allclose(affine, _ijkToRas(reference), atol=1e-6)
        self.assertEqual(data.shape, (32, 28, 24))
        self.assertEqual(int(data.sum()), int(first.sum()) * 8)
        self.assertIn("Resampled", self._sidecar(result.images[0]))
        self._assertNoAbsolutePaths(self.out)

    def test_04b_segment_lost_on_chosen_grid(self):
        """A segment with no voxel left on the chosen (coarser) grid is reported, never written as a silent all-zero mask."""
        scan = self._volume("scan 1 mm", np.zeros((60, 60, 60), np.int16), np.diag([-1.0, -1.0, 1.0, 1.0]))
        reference = self._volume("FLAIR 3 mm", np.zeros((20, 20, 20), np.int16),
                                 np.array([[-3.0, 0, 0, 28.5], [0, -3.0, 0, 28.5], [0, 0, 3.0, -28.5], [0, 0, 0, 1]]))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        thin = segmentation.GetSegmentation().AddEmptySegment("", "Thin")
        big = segmentation.GetSegmentation().AddEmptySegment("", "Big")
        thinArray = np.zeros((60, 60, 60), np.uint8)
        thinArray[30, 25:35, 15:19] = 1  # 1 mm thick, between two 3 mm slices
        bigArray = np.zeros((60, 60, 60), np.uint8)
        bigArray[10:40, 10:40, 10:40] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(thinArray, segmentation, thin, scan)
        slicer.util.updateSegmentBinaryLabelmapFromArray(bigArray, segmentation, big, scan)

        masks = self.export.exportSegmentation(segmentation, os.path.join(self.out, "masks.nii.gz"), mode=self.export.SEGMENTATION_MASKS,
                                               reference=reference)
        self.assertEqual(masks.errors, [])
        self.assertEqual(len(masks.images), 1, masks.images)
        self.assertIn("Big", masks.images[0])
        self.assertGreater(int(self._read(masks.images[0])[0].sum()), 0)
        self.assertTrue(any("Thin" in note and "chosen grid" in note for note in masks.notes), masks.notes)

        labelmap = self.export.exportSegmentation(segmentation, os.path.join(self.out, "labels.nii.gz"), reference=reference)
        self.assertEqual(labelmap.errors, [])
        counts = {entry["Name"]: entry["VoxelCount"] for entry in self._sidecar(labelmap.images[0])["Labels"]}
        self.assertEqual(counts["Thin"], 0)
        self.assertGreater(counts["Big"], 0)
        self.assertTrue(any("Thin" in note and "chosen grid" in note for note in labelmap.notes), labelmap.notes)
        self.assertFalse(any("Big" in note for note in masks.notes + labelmap.notes), masks.notes + labelmap.notes)

    def test_04ba_segment_hidden_by_a_later_segment_is_not_blamed_on_the_grid(self):
        """A segment entirely covered by a later one is missing from the label map because of the overlap: the note says so."""
        scan = self._volume("scan overlap", np.zeros((20, 20, 20), np.int16), np.diag([-1.0, -1.0, 1.0, 1.0]))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesions")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        core = segmentation.GetSegmentation().AddEmptySegment("", "Core")
        penumbra = segmentation.GetSegmentation().AddEmptySegment("", "Penumbra")
        coreArray = np.zeros((20, 20, 20), np.uint8)
        coreArray[8:12, 8:12, 2:6] = 1
        penumbraArray = np.zeros((20, 20, 20), np.uint8)
        penumbraArray[7:13, 7:13, 1:7] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(coreArray, segmentation, core, scan)
        slicer.util.updateSegmentBinaryLabelmapFromArray(penumbraArray, segmentation, penumbra, scan)

        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "overlap.nii.gz"))
        self.assertEqual(result.errors, [])
        counts = {entry["Name"]: entry["VoxelCount"] for entry in self._sidecar(result.images[0])["Labels"]}
        self.assertEqual(counts, {"Core": 0, "Penumbra": 216})
        self.assertFalse(any("chosen grid" in note for note in result.notes), result.notes)
        covered = [note for note in result.notes if "Core" in note]
        self.assertEqual(len(covered), 1, result.notes)
        self.assertIn("covered by a later segment", covered[0])
        self.assertIn("One mask per segment", covered[0])
        self.assertFalse(any("Penumbra" in note for note in result.notes), result.notes)

    def test_04c_transform_note_with_a_reference_grid(self):
        """On a reference grid the sidecar names the lesion's own transform, and the reference's one separately."""
        scan = self._volume("scan", np.zeros((40, 40, 40), np.int16), np.array([[1.0, 0, 0, -20], [0, 1.0, 0, -20], [0, 0, 1.0, -20], [0, 0, 0, 1]]))
        shifted = np.array([[1.0, 0, 0, -30], [0, 1.0, 0, -20], [0, 0, 1.0, -20], [0, 0, 0, 1]])
        reference = self._volume("reference", np.zeros((40, 40, 40), np.int16), shifted)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("", "Lesion")
        array = np.zeros((40, 40, 40), np.uint8)
        array[19:22, 19:22, 4:7] = 1  # world x = -15
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, scan)
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "move10")
        move = np.eye(4)
        move[0, 3] = 10.0
        transformNode.SetMatrixTransformToParent(_matrix(move))

        def exported(name):
            result = self.export.exportSegmentation(segmentation, os.path.join(self.out, name + ".nii.gz"), mode=self.export.SEGMENTATION_MASKS,
                                                    reference=reference if name.startswith("b") else scan, overwrite=True)
            self.assertEqual(result.errors, [])
            data, affine, header = self._read(result.images[0])
            centre = (affine @ np.append(np.argwhere(data > 0).mean(0), 1.0))[0]
            return self._sidecar(result.images[0]), result.notes, header["sform_code"], centre

        # A) the lesion is transformed, the reference is not: the transform moves the mask and the sidecar says so
        segmentation.SetAndObserveTransformNodeID(transformNode.GetID())
        sidecar, notes, code, centre = exported("a_lesion_transformed")
        self.assertAlmostEqual(centre, -5.0, delta=1.0)
        self.assertEqual(sidecar["TransformApplied"]["Name"], "move10")
        self.assertEqual(sidecar["Space"], "aligned")
        self.assertEqual(code, 2)
        self.assertNotIn("ReferenceTransformApplied", sidecar)
        self.assertTrue(any("its transform was applied" in note for note in notes), notes)
        # B) only the reference is transformed: the lesion's transform is not claimed, the reference's one is named apart
        segmentation.SetAndObserveTransformNodeID(None)
        reference.SetAndObserveTransformNodeID(transformNode.GetID())
        sidecar, notes, code, centre = exported("b_reference_transformed")
        self.assertAlmostEqual(centre, -15.0, delta=1.0)
        self.assertNotIn("TransformApplied", sidecar)
        self.assertEqual(sidecar["ReferenceTransformApplied"]["Name"], "move10")
        self.assertEqual(code, 2)
        self.assertFalse(any("its transform was applied" in note for note in notes), notes)
        # C) both share the transform: it is applied once, to the affine
        segmentation.SetAndObserveTransformNodeID(transformNode.GetID())
        sidecar, notes, code, centre = exported("b_both_transformed")
        self.assertEqual(sidecar["TransformApplied"]["Name"], "move10")
        self.assertNotIn("ReferenceTransformApplied", sidecar)
        self.assertEqual(code, 2)

    def test_04d_resampled_lesion_keeps_its_own_space(self):
        """Resampling a lesion onto another image's grid is not a normalization: the space comes from the lesion, not the grid."""
        mniGrid = np.array([[-2.0, 0, 0, 90], [0, 2.0, 0, -126], [0, 0, 2.0, -72], [0, 0, 0, 1]])
        scanGrid = np.array([[1.0, 0, 0, -20], [0, 1.0, 0, -20], [0, 0, 1.0, -20], [0, 0, 0, 1]])
        scan = self._volume("scan", np.zeros((40, 40, 40), np.int16), scanGrid)
        template = self._volume("mni", np.zeros((91, 109, 91), np.int16), mniGrid)

        def lesion(name, volume, block, tag=None):
            segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
            segmentation.CreateDefaultDisplayNodes()
            segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
            segmentId = segmentation.GetSegmentation().AddEmptySegment("", "Lesion")
            array = np.zeros(slicer.util.arrayFromVolume(volume).shape, np.uint8)
            array[block] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            if tag:
                segmentation.SetAttribute("Studio.Space", tag)
            return segmentation

        def exported(segmentation, name, reference=None):
            result = self.export.exportSegmentation(segmentation, os.path.join(self.out, name + ".nii.gz"), mode=self.export.SEGMENTATION_MASKS,
                                                    reference=reference, overwrite=True)
            self.assertEqual(result.errors, [])
            _data, _affine, header = self._read(result.images[0])
            return self._sidecar(result.images[0]), (header["sform_code"], header["qform_code"])

        # A native lesion (untagged, or tagged native) on the MNI grid is still in scanner space
        for tag in (None, "native"):
            native = lesion("native " + str(tag), scan, (slice(15, 25), slice(15, 25), slice(15, 25)), tag)
            sidecar, codes = exported(native, "native_own_" + str(tag))
            self.assertEqual((sidecar["Space"], codes), ("scanner", (1, 1)))
            sidecar, codes = exported(native, "native_on_mni_" + str(tag), reference=template)
            self.assertIn("Resampled", sidecar)
            self.assertEqual((sidecar["Space"], codes), ("scanner", (1, 1)), tag)
            self.assertNotIn("Template", sidecar)
        # An MNI lesion on a scan grid is still MNI (tagged or recognised by its grid)
        for tag in (None, "MNI152NLin6Asym"):
            mni = lesion("mni " + str(tag), template, (slice(35, 45), slice(60, 70), slice(40, 50)), tag)
            sidecar, codes = exported(mni, "mni_on_scan_" + str(tag), reference=scan)
            self.assertIn("Resampled", sidecar)
            self.assertEqual((sidecar["Space"], codes), ("MNI152NLin6Asym", (4, 4)), tag)
        self._assertNoAbsolutePaths(self.out)

    def test_05_template_codes(self):
        """sform/qform code 4 on a recognised MNI grid (the bundled template) and on the user's own template (A-10)."""
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import templates

        builtin = templates.builtinTemplates()[0]
        data, affine, _header = nifti.read_nifti(builtin.imagePath)
        template = slicer.util.loadVolume(builtin.imagePath)
        result = self.export.exportVolume(template, os.path.join(self.out, "template.nii.gz"))
        _data, exported, header = self._read(result.images[0])
        self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4))
        np.testing.assert_allclose(exported, affine, atol=1e-5)
        self.assertEqual(self._sidecar(result.images[0])["Space"], "MNI152NLin2009cAsym")
        # Moved by a transform, the template (tagged with its space) is no longer on the MNI grid: code 2, aligned
        template.SetAttribute("Studio.Space", "MNI152NLin2009cAsym 2")
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "shift")
        shift = np.eye(4)
        shift[:3, 3] = (5.0, 0.0, 0.0)
        transformNode.SetMatrixTransformToParent(_matrix(shift))
        template.SetAndObserveTransformNodeID(transformNode.GetID())
        result = self.export.exportVolume(template, os.path.join(self.out, "template_moved.nii.gz"))
        _data, _affine, header = self._read(result.images[0])
        self.assertEqual((header["sform_code"], header["qform_code"]), (2, 2))
        sidecar = self._sidecar(result.images[0])
        self.assertEqual(sidecar["Space"], "aligned")
        self.assertNotIn("Template", sidecar)
        template.SetAndObserveTransformNodeID(None)
        # The user's own 1.5 mm template (not a built-in grid)
        spacing = 1.5
        shape = tuple(int(np.floor((data.shape[axis] - 1) * 2.0 / spacing)) + 1 for axis in range(3))
        target = np.diag([spacing, spacing, spacing, 1.0])
        target[:3, 3] = affine[:3, 3]
        resampled = geometry.resample_to_grid(data, affine, geometry.Grid(shape, target), mode="linear").astype(np.float32)
        imagePath = os.path.join(self.tempDir, "my_mni_1.5mm_T1w.nii.gz")
        nifti.write_nifti(imagePath, resampled, target)
        own = templates.addTemplate(imagePath, name="My MNI 1.5 mm", space="MNI152NLin6Asym")
        templates.registerGrids(force=True)
        try:
            node = slicer.util.loadVolume(own.imagePath)
            node.SetName("my scan on my template")
            result = self.export.exportVolume(node, os.path.join(self.out, "own.nii.gz"))
            _data, _affine, header = self._read(result.images[0])
            self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4))
            sidecar = self._sidecar(result.images[0])
            self.assertEqual(sidecar["Space"], "MNI152NLin6Asym")
            self.assertEqual(sidecar["Template"]["id"], own.id)
            self.assertEqual(sidecar["Template"]["sha256"], own.imageSha256)
            self.assertNotIn("path", json.dumps(sidecar["Template"]).lower())
        finally:
            templates.removeTemplate(own.id, confirmDelete=lambda *args: True)
        self._assertNoAbsolutePaths(self.out)

    def test_06_transforms_and_formats(self):
        """A linear transform is applied to the exported affine (the scene is unchanged, code 2); .nii and .nii.gz."""
        node = self._phantom("moved", np.array([[1.0, 0, 0, -10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "shift")
        shift = np.eye(4)
        shift[:3, 3] = (-5.0, 0.0, 0.0)
        transformNode.SetMatrixTransformToParent(_matrix(shift))
        node.SetAndObserveTransformNodeID(transformNode.GetID())
        before = _ijkToRas(node)
        result = self.export.exportVolume(node, os.path.join(self.out, "moved.nii"))
        self.assertTrue(result.images[0].endswith(".nii") and not result.images[0].endswith(".nii.gz"))
        with open(result.images[0], "rb") as f:
            self.assertNotEqual(f.read(2), b"\x1f\x8b", "a .nii file is not compressed")
        np.testing.assert_allclose(self._markerRas(result.images[0]), (-11.0, 2.0, 4.0), atol=1e-5)
        _data, _affine, header = self._read(result.images[0])
        self.assertEqual(header["sform_code"], 2)
        self.assertTrue(self._sidecar(result.images[0])["TransformApplied"]["Linear"])
        self.assertTrue(result.notes)
        np.testing.assert_allclose(_ijkToRas(node), before)
        self.assertIs(node.GetParentTransformNode(), transformNode)
        result = self.export.exportVolume(node, os.path.join(self.out, "moved_compressed.nii.gz"))
        with open(result.images[0], "rb") as f:
            self.assertEqual(f.read(2), b"\x1f\x8b")
        # Existing files are never replaced silently (the .json sidecar of moved.nii.gz would be the one of moved.nii)
        with self.assertRaises(FileExistsError):
            self.export.exportVolume(node, os.path.join(self.out, "moved.nii.gz"))
        self.export.exportVolume(node, os.path.join(self.out, "moved.nii.gz"), overwrite=True)
        # A non-linear (grid) transform is hardened into a resampled copy: the marker moves with it, the scene does not change
        import vtk.util.numpy_support

        field = vtk.vtkImageData()
        field.SetDimensions(5, 5, 5)
        field.SetSpacing(10.0, 10.0, 10.0)
        field.SetOrigin(-20.0, -20.0, -20.0)
        field.AllocateScalars(vtk.VTK_DOUBLE, 3)
        displacements = vtk.util.numpy_support.vtk_to_numpy(field.GetPointData().GetScalars())
        displacements[:] = 0.0
        displacements[:, 0] = 3.0
        gridTransform = slicer.vtkOrientedGridTransform()
        gridTransform.SetDisplacementGridData(field)
        gridNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLGridTransformNode", "warp")
        gridNode.SetAndObserveTransformToParent(gridTransform)
        node.SetAndObserveTransformNodeID(gridNode.GetID())
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()
        result = self.export.exportVolume(node, os.path.join(self.out, "warped.nii.gz"))
        self.assertEqual(result.errors, [])
        np.testing.assert_allclose(self._markerRas(result.images[0]), (-3.0, 2.0, 4.0), atol=0.75)
        sidecar = self._sidecar(result.images[0])
        self.assertFalse(sidecar["TransformApplied"]["Linear"])
        self.assertEqual(sidecar["SformCode"], 2)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore)
        np.testing.assert_allclose(_ijkToRas(node), before)
        self.assertIs(node.GetParentTransformNode(), gridNode)
        self.assertIn("linear interpolation", sidecar["TransformApplied"]["Description"])
        # A label map is resampled nearest neighbour, and its sidecar says so
        labels = self._volume("labels", np.zeros((21, 21, 21), np.uint8), np.eye(4), className="vtkMRMLLabelMapVolumeNode")
        labels.SetAndObserveTransformNodeID(gridNode.GetID())
        result = self.export.exportVolume(labels, os.path.join(self.out, "labels_warped.nii.gz"))
        self.assertIn("nearest neighbour", self._sidecar(result.images[0])["TransformApplied"]["Description"])

    def test_06aa_sheared_linear_transform_is_resampled(self):
        """A linear transform with shear (a 12-DOF affine registration with non-uniform scaling) cannot be folded into a
        NIfTI affine: its qform (rotation and zooms only) would differ from its sform, and 3D Slicer (qform) would place the
        voxels several mm away from FSL and nibabel (sform). The copy is resampled instead: sform equals qform, and both put
        the marker where the views show it. Scans, label maps and segmentations alike.
        """
        angle = np.deg2rad(30.0)
        rotation = np.eye(4)
        rotation[:2, :2] = [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
        sheared = np.diag([2.0, 1.0, 1.0, 1.0]) @ rotation
        sheared[:3, 3] = (1.5, -2.0, 0.5)
        self.assertTrue(self.export.hasShear(sheared))
        self.assertFalse(self.export.hasShear(rotation))
        self.assertFalse(self.export.hasShear(np.diag([2.0, 1.0, 3.0, 1.0])), "a non-uniform scale alone has no shear")
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "affine")
        transformNode.SetMatrixTransformToParent(_matrix(sheared))
        ijkToRas = np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]])
        marker = np.array([-6.0, 2.0, 4.0, 1.0])
        shown = (sheared @ marker)[:3]

        def assertWhereShown(path, sidecarInterpolation):
            from LesionNetworkMappingLib import nifti

            _data, affine, header = self._read(path)
            # Every reader agrees: the qform (what 3D Slicer and ITK read) equals the sform (FSL, nibabel)
            self.assertFalse(self.export.hasShear(affine), "the written affine has no shear")
            np.testing.assert_allclose(header["qform"], header["sform"], atol=1e-5)
            self.assertLess(nifti.xform_disagreement(header), 1e-3)
            self.assertEqual((header["sform_code"], header["qform_code"]), (2, 2))
            np.testing.assert_allclose(self._markerRas(path), shown, atol=1.0)
            # 3D Slicer reopens the file with the marker where the views showed it
            reopened = slicer.util.loadVolume(path)
            array = slicer.util.arrayFromVolume(reopened)
            k, j, i = np.unravel_index(int(np.argmax(array)), array.shape)
            np.testing.assert_allclose((_ijkToRas(reopened) @ [i, j, k, 1.0])[:3], shown, atol=1.0)
            slicer.mrmlScene.RemoveNode(reopened)
            transformApplied = self._sidecar(path)["TransformApplied"]
            self.assertTrue(transformApplied["Linear"])
            self.assertIn(f"resampled, {sidecarInterpolation}", transformApplied["Description"])

        scan = self._phantom("sheared scan", ijkToRas, markerRas=marker[:3])
        scan.SetAndObserveTransformNodeID(transformNode.GetID())
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()
        result = self.export.exportVolume(scan, os.path.join(self.out, "sheared.nii.gz"))
        self.assertEqual(result.errors, [])
        assertWhereShown(result.images[0], "linear interpolation")
        # The scene is unchanged and no temporary node is left
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore)
        np.testing.assert_allclose(_ijkToRas(scan), ijkToRas)
        self.assertIs(scan.GetParentTransformNode(), transformNode)

        labels = np.zeros((21, 21, 21), np.uint8)
        i, j, k = (round(value) for value in np.linalg.solve(ijkToRas, marker)[:3])
        labels[k, j, i] = 5
        labelmap = self._volume("sheared labels", labels, ijkToRas, className="vtkMRMLLabelMapVolumeNode")
        labelmap.SetAndObserveTransformNodeID(transformNode.GetID())
        result = self.export.exportVolume(labelmap, os.path.join(self.out, "sheared_labels.nii.gz"))
        assertWhereShown(result.images[0], "nearest neighbour")
        self.assertEqual(set(np.unique(self._read(result.images[0])[0]).tolist()), {0, 5}, "label values are kept")

        # A segmentation is exported on its own cropped extent: a block lesion fills it to the corners, and the voxels
        # outside the moved block must stay 0 (the resampled mask is not its whole bounding box)
        grid = self._volume("grid", np.zeros((21, 21, 21), np.int16), ijkToRas)
        block = np.zeros((21, 21, 21), np.uint8)
        block[k - 1 : k + 2, j - 1 : j + 2, i - 1 : i + 2] = 1
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (sheared scan)")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(grid)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("", "Lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray(block, segmentation, segmentId, grid)
        segmentation.SetAndObserveTransformNodeID(transformNode.GetID())
        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "sheared_lesion.nii.gz"), mode=self.export.SEGMENTATION_MASKS)
        self.assertEqual(result.errors, [])
        data, affine, _header = self._read(result.images[0])
        self.assertFalse(self.export.hasShear(affine))
        # The transform doubles the volume: 27 voxels of 1 mm become about 54 (nearest neighbour)
        self.assertTrue(40 <= int(data.sum()) <= 70, int(data.sum()))
        centre = (affine @ np.array([*np.argwhere(data > 0).mean(axis=0), 1.0]))[:3]
        np.testing.assert_allclose(centre, shown, atol=0.75)
        self.assertIn("resampled, nearest neighbour", self._sidecar(result.images[0])["TransformApplied"]["Description"])

    def test_06ab_hardened_sheared_grid_is_resampled(self):
        """Harden transform of a 12-DOF affine (non-uniform scale along rotated axes) leaves the volume's OWN IJK to RAS
        matrix sheared, with no parent transform. It is resampled like a sheared parent transform: sform equals qform, and
        FSL/nibabel and 3D Slicer all read the marker where the views show it. Codes stay 1 (no transform is applied), the
        sidecar says the copy was resampled. Scans, label maps, and lesions drawn on the hardened scan (masks and label map).
        """
        angle = np.deg2rad(20.0)
        rotation = np.eye(4)
        rotation[:2, :2] = [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]]
        sheared = rotation @ np.diag([1.10, 0.92, 1.0, 1.0]) @ rotation.T
        sheared[:3, 3] = (2.0, -1.0, 0.5)
        ijkToRas = np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]])
        marker = np.array([-6.0, 2.0, 4.0, 1.0])
        shown = (sheared @ marker)[:3]
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "affine registration")
        transformNode.SetMatrixTransformToParent(_matrix(sheared))

        def hardened(node):
            node.SetAndObserveTransformNodeID(transformNode.GetID())
            slicer.vtkSlicerTransformLogic().hardenTransform(node)
            self.assertIsNone(node.GetParentTransformNode())
            self.assertTrue(self.export.hasShear(_ijkToRas(node)), "hardening keeps the shear in the volume's own grid")
            return node

        def assertConsistent(path, interpolation):
            from LesionNetworkMappingLib import nifti

            _data, affine, header = self._read(path)
            self.assertFalse(self.export.hasShear(affine), "the written affine has no shear")
            np.testing.assert_allclose(header["qform"], header["sform"], atol=1e-5)
            self.assertLess(nifti.xform_disagreement(header), 1e-3)
            self.assertEqual((header["sform_code"], header["qform_code"]), (1, 1), "no transform is applied: scanner")
            sidecar = self._sidecar(path)
            self.assertNotIn("TransformApplied", sidecar)
            self.assertEqual(sidecar["GridResampled"]["Interpolation"], interpolation)

        def assertReopenedWhereShown(path):
            reopened = slicer.util.loadVolume(path)
            array = slicer.util.arrayFromVolume(reopened)
            k, j, i = np.unravel_index(int(np.argmax(array)), array.shape)
            np.testing.assert_allclose((_ijkToRas(reopened) @ [i, j, k, 1.0])[:3], shown, atol=1.0)
            slicer.mrmlScene.RemoveNode(reopened)

        scan = hardened(self._phantom("hardened scan", ijkToRas, markerRas=marker[:3]))
        scanGrid = _ijkToRas(scan)
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()
        result = self.export.exportVolume(scan, os.path.join(self.out, "hardened.nii.gz"))
        self.assertEqual(result.errors, [])
        assertConsistent(result.images[0], "linear interpolation")
        np.testing.assert_allclose(self._markerRas(result.images[0]), shown, atol=1.0)
        assertReopenedWhereShown(result.images[0])
        self.assertTrue(any("resampled" in note for note in result.notes), result.notes)
        # The scene is unchanged and no temporary node is left
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore)
        np.testing.assert_allclose(_ijkToRas(scan), scanGrid)

        labels = np.zeros((21, 21, 21), np.uint8)
        i, j, k = (round(value) for value in np.linalg.solve(ijkToRas, marker)[:3])
        labels[k, j, i] = 5
        labelmap = hardened(self._volume("hardened labels", labels, ijkToRas, className="vtkMRMLLabelMapVolumeNode"))
        result = self.export.exportVolume(labelmap, os.path.join(self.out, "hardened_labels.nii.gz"))
        assertConsistent(result.images[0], "nearest neighbour")
        np.testing.assert_allclose(self._markerRas(result.images[0]), shown, atol=1.0)
        self.assertEqual(set(np.unique(self._read(result.images[0])[0]).tolist()), {0, 5}, "label values are kept")

        # A lesion drawn on the hardened scan (its reference grid is the sheared one)
        block = np.zeros((21, 21, 21), np.uint8)
        block[k - 1 : k + 2, j - 1 : j + 2, i - 1 : i + 2] = 1
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (hardened scan)")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("", "Lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray(block, segmentation, segmentId, scan)
        for mode, name in ((self.export.SEGMENTATION_MASKS, "hardened_lesion_masks.nii.gz"), (self.export.SEGMENTATION_LABELMAP, "hardened_lesion.nii.gz")):
            result = self.export.exportSegmentation(segmentation, os.path.join(self.out, name), mode=mode)
            self.assertEqual(result.errors, [])
            assertConsistent(result.images[0], "nearest neighbour")
            data, affine, _header = self._read(result.images[0])
            self.assertTrue(15 <= int(np.count_nonzero(data)) <= 40, int(np.count_nonzero(data)))
            centre = (affine @ np.array([*np.argwhere(data > 0).mean(axis=0), 1.0]))[:3]
            np.testing.assert_allclose(centre, shown, atol=0.75)
            self.assertTrue(any("resampled" in note for note in result.notes), result.notes)

    def test_06b_fallback_writer_keeps_the_scene(self):
        """3D Slicer's writer (the fallback without LesionNetworkMappingLib) does not re-point the node's storage node."""
        node = self._phantom("fallback", np.array([[1.0, 0, 0, -10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        storageNodeId = node.GetStorageNodeID()
        nodesBefore = slicer.mrmlScene.GetNumberOfNodes()
        path = os.path.join(self.out, "fallback.nii.gz")
        task = self.export.WriteTask(path, None, _ijkToRas(node), self.export.XFORM_SCANNER, None, node=node)
        self.export._writeWithSlicer(task)
        self.assertTrue(os.path.isfile(path))
        np.testing.assert_allclose(self._markerRas(path), (-6.0, 2.0, 4.0), atol=1e-4)
        self.assertEqual(node.GetStorageNodeID(), storageNodeId)
        self.assertEqual(slicer.mrmlScene.GetNumberOfNodes(), nodesBefore)

    def test_06c_mask_names_keep_bids_entity_order(self):
        """``desc`` stays the last entity of a mask name (BIDS); other names just add ``_label-<segment>_mask``."""
        self.assertEqual(self.export.maskBaseName("sub-01_desc-lesion", "Lesion"), "sub-01_label-Lesion_desc-lesion_mask")
        self.assertEqual(self.export.maskBaseName("lesions", "Left"), "lesions_label-Left_mask")

    def test_06d_mask_names_never_equal_another_segments_name(self):
        """Names that differ only by punctuation or case: a suffix never makes a file name that is another segment's name."""
        scan = self._volume("scan", np.zeros((30, 30, 30), np.int16), np.eye(4))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        names = ("Lesion 1", "Lesion-1", "Lesion 12", "Cyst", "cyst", "Cyst2")
        for index, name in enumerate(names):
            segmentId = segmentation.GetSegmentation().AddEmptySegment("", name)
            data = np.zeros((30, 30, 30), np.uint8)
            data[3 + 4 * index, 5, 5] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(data, segmentation, segmentId, scan)
        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "Lesions.nii.gz"), mode=self.export.SEGMENTATION_MASKS)
        self.assertEqual(result.errors, [])
        found = {os.path.basename(path): self._sidecar(path)["Segment"] for path in result.images}
        self.assertEqual(found, {
            "Lesions_label-Lesion1_mask.nii.gz": "Lesion 1",
            "Lesions_label-Lesion1dup2_mask.nii.gz": "Lesion-1",
            "Lesions_label-Lesion12_mask.nii.gz": "Lesion 12",
            "Lesions_label-Cyst_mask.nii.gz": "Cyst",
            "Lesions_label-cystdup2_mask.nii.gz": "cyst",
            "Lesions_label-Cyst2_mask.nii.gz": "Cyst2",
        })

    def test_06da_accented_letters_are_transliterated_in_names(self):
        """Non-ASCII letters are not deleted from file names: "Ödem rechts" -> ``Odemrechts``; CJK names use the segment number."""
        export = self.export
        self.assertEqual(export.entityValue("Ödem rechts"), "Odemrechts")
        self.assertEqual(export.entityValue("Lésion"), "Lesion")
        self.assertEqual(export.entityValue("Ĉerbo"), "Cerbo")
        self.assertEqual(export.entityValue("Straße Øre"), "StrasseOre")
        self.assertEqual(export.safeName("Läsionen"), "Lasionen")
        self.assertEqual(export.safeName("Ĉerbo"), "Cerbo")
        self.assertEqual(export.safeName("Läsionen"), export.entityValue("Läsionen"))
        self.assertEqual(export.safeName("扫描"), "image")
        scan = self._volume("scan", np.zeros((30, 30, 30), np.int16), np.eye(4))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Läsionen")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        names = ("Ödem rechts", "Lésion", "病变", "水肿", "Odem rechts")
        for index, name in enumerate(names):
            segmentId = segmentation.GetSegmentation().AddEmptySegment("", name)
            data = np.zeros((30, 30, 30), np.uint8)
            data[3 + 4 * index, 5, 5] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(data, segmentation, segmentId, scan)
        result = self.export.exportSegmentation(segmentation, os.path.join(self.out, "Lasionen.nii.gz"), mode=self.export.SEGMENTATION_MASKS)
        self.assertEqual(result.errors, [])
        found = {os.path.basename(path): self._sidecar(path)["Segment"] for path in result.images}
        self.assertEqual(found, {
            "Lasionen_label-Odemrechts_mask.nii.gz": "Ödem rechts",
            "Lasionen_label-Lesion_mask.nii.gz": "Lésion",
            "Lasionen_label-segment3_mask.nii.gz": "病变",
            "Lasionen_label-segment4_mask.nii.gz": "水肿",
            "Lasionen_label-Odemrechtsdup2_mask.nii.gz": "Odem rechts",
        })

    def test_06e_4d_image_is_written_whole(self):
        """A 4D image (fMRI) opened as a sequence is written with every time point and its TR, not only the one on screen."""
        from LesionNetworkMappingLib import nifti
        from StudioLib import autosave_io, importer, niftiexport_ui

        shape, count = (9, 11, 7), 6
        series = np.zeros((*shape, count), np.int16)
        for t in range(count):
            series[1, 5, 3, t] = 10 * (t + 1)
            series[..., t] += np.int16(t)
        affine = np.array([[2.0, 0, 0, -9], [0, 2.0, 0, -11], [0, 0, 2.0, -7], [0, 0, 0, 1]])
        source = os.path.join(self.tempDir, "sub-07_task-motor_bold.nii.gz")
        nifti.write_nifti(source, series, affine, sform_code=1, qform_code=1, time_step=2.0)
        proxy = importer.loadSequence(source)
        # The views show time point 3: the export still writes all of them
        slicer.modules.sequences.logic().GetFirstBrowserNodeForProxyNode(proxy).SetSelectedItemNumber(3)
        result = self.export.exportNodes([proxy], self.out)
        self.assertEqual(result.errors, [])
        self.assertEqual([os.path.basename(path) for path in result.images], ["sub-07_task-motor_bold.nii.gz"])
        data, written, header = self._read(result.images[0])
        self.assertEqual(data.shape, (*shape, count))
        np.testing.assert_array_equal(data, series)
        np.testing.assert_allclose(written, affine, atol=1e-6)
        self.assertEqual(header["dim"][0], 4)
        self.assertAlmostEqual(float(header["pixdim"][4]), 2.0)
        sidecar = self._sidecar(result.images[0])
        self.assertEqual(sidecar["Shape"], [*shape, count])
        self.assertEqual(sidecar["TimePoints"], count)
        self.assertAlmostEqual(sidecar["RepetitionTime"], 2.0)
        self.assertEqual(sidecar["SourceFile"], "sub-07_task-motor_bold.nii.gz")
        sequenceNode = self.export.sequenceOf(proxy)
        self.assertIsNotNone(sequenceNode)
        self.assertEqual(self.export.timePoints(proxy), count)
        self.assertAlmostEqual(self.export.timeStep(sequenceNode), 2.0)
        # A linear transform of the shown volume applies to every time point (folded into the affine)
        transformNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLLinearTransformNode", "shift")
        shift = np.eye(4)
        shift[:3, 3] = (-5.0, 0.0, 0.0)
        transformNode.SetMatrixTransformToParent(_matrix(shift))
        proxy.SetAndObserveTransformNodeID(transformNode.GetID())
        result = self.export.exportVolume(proxy, os.path.join(self.out, "moved_bold.nii.gz"))
        self.assertEqual(result.errors, [])
        data, written, _header = self._read(result.images[0])
        np.testing.assert_array_equal(data, series)
        np.testing.assert_allclose(written, shift @ affine, atol=1e-6)
        # The dialog says the image has 6 time points
        dialog = niftiexport_ui.exportNodesDialog([proxy], show=False, executor=autosave_io.InlineExecutor())
        self.assertIn("6 time points", dialog.list.item(0).text())
        dialog.close()

    def test_07_map_with_threshold_mask(self):
        """A map is written as float32 with a thresholded binary mask (|T| >= t, or one sign) and the threshold in the sidecar."""
        ijkToRas = np.array([[-2.0, 0, 0, 90], [0, 2.0, 0, -126], [0, 0, 2.0, -72], [0, 0, 0, 1]])
        values = np.zeros((91, 109, 91), np.float64)
        values[40, 50, 30] = 6.0
        values[41, 50, 30] = -5.0
        values[42, 50, 30] = 2.0
        node = self._volume("sub-01_T", values, ijkToRas)
        node.SetAttribute("LNM.Statistic", "T")
        self.assertEqual(self.export.kindOf(node), self.export.KIND_MAP)
        result = self.export.exportVolume(node, os.path.join(self.out, "sub-01_T.nii.gz"), threshold=4.5)
        self.assertEqual(len(result.images), 2)
        data, _affine, header = self._read(result.images[0])
        self.assertEqual(data.dtype, np.float32)
        self.assertEqual(header["sform_code"], 4)
        mask, _affine, _header = self._read(self.export.mapMaskPath(result.images[0]))
        self.assertEqual(mask.dtype, np.uint8)
        self.assertEqual(int(mask.sum()), 2)
        sidecar = self._sidecar(result.images[0])
        self.assertEqual(sidecar["Threshold"]["Value"], 4.5)
        self.assertEqual(sidecar["Statistic"], "T")
        self.assertEqual(self._sidecar(self.export.mapMaskPath(result.images[0]))["Kind"], "mask")
        positive = self.export.thresholdMask(values.transpose(2, 1, 0), 4.5, "pos")
        self.assertEqual(int(positive.sum()), 1)

    def test_07a_derived_file_names_never_collide_across_jobs(self):
        """A map's threshold mask and a segment's mask never get the name of another job's file (scans named like them)."""
        ijkToRas = np.array([[-2.0, 0, 0, 20], [0, 2.0, 0, -20], [0, 0, 2.0, -20], [0, 0, 0, 1]])
        scan1 = self._volume("Lesions_label-Lesion_mask", np.full((10, 10, 10), 5, np.int16), ijkToRas)
        scan2 = self._volume("T_desc-thresholded_mask", np.full((10, 10, 10), 6, np.int16), ijkToRas)
        values = np.zeros((10, 10, 10), np.float32)
        values[5, 5, 5] = 9
        tmap = self._volume("T", values, ijkToRas)
        tmap.SetAttribute("LNM.Statistic", "T")
        tmap.SetAttribute("LNM.Result", "1")
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan1)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("", "Lesion")
        mask = np.zeros((10, 10, 10), np.uint8)
        mask[3, 3, 3] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentation, segmentId, scan1)
        jobs = self.export.planExport(self.export.exportableNodes(), self.out, segmentationMode=self.export.SEGMENTATION_MASKS,
                                      mapOptions={tmap.GetID(): {"threshold": 3.0, "sign": "both"}})
        planned = [os.path.normcase(path).lower() for path in self.export.plannedFiles(jobs)]
        self.assertEqual(len(planned), len(set(planned)), "every planned file has its own path")
        result = self.export.runJobs(jobs)
        written = sorted(name for name in os.listdir(self.out) if name.endswith(".nii.gz"))
        self.assertEqual(len(written), len(result.images), "one file on disk per image reported")
        data, _affine, _header = self._read(os.path.join(self.out, "Lesions_label-Lesion_mask.nii.gz"))
        self.assertTrue((data == 5).all(), "the scan is not overwritten by the segment mask")

    def test_07b_map_on_a_declared_connectome_space_is_mni(self):
        """A Map result of a connectome on a grid that is not a built-in one (the demo, the user's conversions) is in the
        connectome's declared space: code 4 and that space in the sidecar, as the engine's own file of the map.
        """
        import LesionNetworkMapping as lnm
        from LesionNetworkMappingLib import connectome, nifti

        logic = lnm.LesionNetworkMappingLogic()
        demoPath = os.path.join(self.tempDir, "demo.lnmc")
        lnm.writeDemoConnectome(demoPath, nSubjects=2, shape=(8, 9, 8), runs=(12,))
        demo = connectome.ConnectomeBundle.open(demoPath)
        self.assertEqual(logic.connectomeSpaceName(demo), lnm.DEMO_SPACE)
        nativePath = os.path.join(self.tempDir, "native.lnmc")
        native = connectome.ConnectomeBundle.open(connectome.make_synthetic(nativePath, shape=(8, 9, 8), n_subjects=2, runs=(12,))["path"])
        self.assertIsNone(logic.connectomeSpaceName(native))
        self.assertIsNone(logic.connectomeSpaceName(None))

        values = np.zeros(tuple(demo.shape), np.float32)
        values[3, 4, 3] = 6.0
        enginePath = os.path.join(self.tempDir, "lesion_conn-Demo_desc-T_funcmap.nii.gz")
        nifti.write_nifti(enginePath, values, demo.affine)
        self.assertEqual(nifti.read_nifti_header(enginePath)["sform_code"], 4)
        volume = logic.loadMap(enginePath, "Lesion T", None, attributes={"LNM.Statistic": "T", "LNM.N": 2,
                                                                         "Studio.Space": logic.connectomeSpaceName(demo)})
        self.assertEqual(self.export.kindOf(volume), self.export.KIND_MAP)
        result = self.export.exportVolume(volume, os.path.join(self.tempDir, "mapout", "map.nii.gz"), threshold=4.0)
        self.assertEqual(len(result.images), 2)
        for path in result.images:
            _data, _affine, header = self._read(path)
            self.assertEqual((header["sform_code"], header["qform_code"]), (4, 4), path)
            self.assertEqual(self._sidecar(path)["Space"], lnm.DEMO_SPACE)
        # A later map with a connectome without a space removes the attribute (no stale space)
        logic.loadMap(enginePath, "Lesion T", volume, attributes={"Studio.Space": logic.connectomeSpaceName(native)})
        self.assertIsNone(volume.GetAttribute("Studio.Space"))
        # A map file opened from disk takes the space of its sidecar
        sidecar = os.path.join(self.tempDir, "lesion_conn-Demo_lnm.json")
        with open(sidecar, "w", encoding="utf-8") as f:
            json.dump({"N": 2, "connectome": {"space": lnm.DEMO_SPACE}}, f)
        self.assertEqual(logic.mapFileInfo(enginePath)["space"], lnm.DEMO_SPACE)

    def test_08_dialog_multi_select_and_overwrite(self):
        """The dialog exports several images at once, asks before replacing files and remembers only Studio/* keys."""
        from StudioLib import autosave_io, niftiexport_ui

        settings = qt.QSettings()
        keysBefore = {key for key in settings.allKeys() if not key.startswith("Studio/")}
        first = self._phantom("first scan", np.array([[1.0, 0, 0, -10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        second = self._phantom("second scan", np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        third = self._phantom("third scan", np.array([[-1.0, 0, 0, 10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        dialog = niftiexport_ui.exportNodesDialog([first, second], show=False, executor=autosave_io.InlineExecutor())
        self.assertEqual(dialog.objectName, niftiexport_ui.DIALOG_OBJECT_NAME)
        self.assertEqual([node.GetName() for node in dialog.checkedNodes()], ["first scan", "second scan"])
        self.assertEqual(dialog.list.count, 3)
        dialog.setFolder(self.out)
        # A click on the format updates the summary (idClicked in Qt 6, buttonClicked(int) before Qt 5.15)
        dialog.formatGroup.button(1).click()
        self.assertEqual(dialog.extension(), ".nii")
        self.assertIn("first_scan.nii", dialog.summaryLabel.text)
        self.assertNotIn("first_scan.nii.gz", dialog.summaryLabel.text)
        dialog.formatGroup.button(0).click()
        self.assertIn("first_scan.nii.gz", dialog.summaryLabel.text)
        dialog.setFormat(".nii")
        self.assertIn("first_scan.nii", dialog.summaryLabel.text)
        self.assertFalse(dialog.segmentationFrame.isVisibleTo(dialog))
        runner = dialog.export(confirm=lambda existing: self.fail("nothing exists yet"))
        self.assertTrue(runner.wait(60))
        self.assertEqual(sorted(os.path.basename(path) for path in runner.result.images), ["first_scan.nii", "second_scan.nii"])
        # Again: the files exist, and nothing is written unless the user agrees
        stamps = {path: os.path.getmtime(path) for path in runner.result.images}
        dialog = niftiexport_ui.exportNodesDialog([first, second, third], show=False, executor=autosave_io.InlineExecutor())
        dialog.setFolder(self.out)
        dialog.setFormat(".nii")
        self.assertTrue(dialog.existingNotice.isVisibleTo(dialog))
        asked = []
        self.assertIsNone(dialog.export(confirm=lambda existing: asked.append(list(existing)) or False))
        self.assertEqual(len(asked[0]), 4)
        self.assertEqual({path: os.path.getmtime(path) for path in stamps}, stamps)
        self.assertFalse(os.path.exists(os.path.join(self.out, "third_scan.nii")))
        runner = dialog.export(confirm=lambda existing: True)
        self.assertTrue(runner.wait(60))
        self.assertEqual(len(runner.result.images), 3)
        self.assertEqual(settings.value(self.export.FORMAT_KEY), ".nii")
        self.assertEqual(os.path.normpath(settings.value(self.export.LAST_FOLDER_KEY)), os.path.normpath(self.out))
        self.assertEqual({key for key in settings.allKeys() if not key.startswith("Studio/")}, keysBefore)
        # A segmentation shows the segmentation options
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(first)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray((slicer.util.arrayFromVolume(first) > 0).astype(np.uint8), segmentation, segmentId, first)
        dialog = niftiexport_ui.exportNodesDialog([segmentation], show=False, executor=autosave_io.InlineExecutor())
        self.assertTrue(dialog.segmentationFrame.isVisibleTo(dialog))
        self.assertTrue(dialog.referenceCombo.isVisibleTo(dialog))
        dialog.setFolder(self.out)
        dialog.segmentationGroup.button(1).click()
        self.assertEqual(dialog.segmentationMode(), self.export.SEGMENTATION_MASKS)
        self.assertIn("Lesions_label-Lesion_mask.nii", dialog.summaryLabel.text)
        dialog.segmentationGroup.button(0).click()
        self.assertIn("Lesions_dseg.nii", dialog.summaryLabel.text)
        self.assertIn("plus 1 sidecar and 1 label table", dialog.summaryLabel.text)
        dialog.segmentationGroup.button(1).click()
        self.assertIn("plus 1 sidecar", dialog.summaryLabel.text)
        self.assertNotIn("label", dialog.summaryLabel.text.replace("label-Lesion", ""))
        # With more images than rows, the chosen one is scrolled into view
        extra = [self._volume(f"extra {index}", np.zeros((3, 3, 3), np.int16), np.eye(4)) for index in range(8)]
        dialog = niftiexport_ui.exportNodesDialog([segmentation], show=False, executor=autosave_io.InlineExecutor())
        self.assertGreater(dialog.list.count, niftiexport_ui.LIST_ROWS)
        dialog.show()
        slicer.app.processEvents()
        checked = [dialog.list.item(row) for row in range(dialog.list.count) if dialog.list.item(row).checkState() == qt.Qt.Checked]
        self.assertEqual(len(checked), 1)
        rect = dialog.list.visualItemRect(checked[0])
        self.assertTrue(rect.top() >= 0 and rect.bottom() <= dialog.list.viewport().height, "the chosen image is visible")
        dialog.close()
        for node in extra:
            slicer.mrmlScene.RemoveNode(node)
        self._assertNoAbsolutePaths(self.out)

    def test_08_summary_counts_sidecars_and_names_label_tables_only_when_written(self):
        """"plus 1 sidecar" / "plus 3 sidecars": no "label files" for plain images (only a label map writes label tables)."""
        from StudioLib import autosave_io, niftiexport_ui

        scans = [self._volume(f"count scan {index}", np.zeros((3, 3, 3), np.int16), np.eye(4)) for index in range(3)]
        for chosen, expected in ((scans[:1], "1 image: count_scan_0.nii.gz · plus 1 sidecar"), (scans, "plus 3 sidecars")):
            dialog = niftiexport_ui.exportNodesDialog(chosen, show=False, executor=autosave_io.InlineExecutor())
            dialog.setFolder(self.out)
            self.assertIn(expected, dialog.summaryLabel.text)
            self.assertNotIn("label", dialog.summaryLabel.text)
            self.assertNotIn(" and ", dialog.summaryLabel.text.split("plus", 1)[1])
            dialog.close()

    def test_08_list_title_is_sentence_case(self):
        """The list title reads "Images", not an ALL-CAPS overline (VISUAL 2.4: overlines only for CONTINUE and group headers)."""
        from StudioLib import autosave_io, niftiexport_ui

        node = self._volume("caps check", np.zeros((3, 3, 3), np.int16), np.eye(4))
        dialog = niftiexport_ui.exportNodesDialog([node], show=False, executor=autosave_io.InlineExecutor())
        title = dialog.findChild(qt.QLabel, "StudioNiftiExportListTitle")
        self.assertIsNotNone(title)
        self.assertEqual(title.text, "Images")
        self.assertNotEqual(title.font.capitalization(), qt.QFont.AllUppercase)
        dialog.close()
        slicer.mrmlScene.RemoveNode(node)

    def test_08a_replace_question_defaults_to_keeping_the_files(self):
        """In the "Replace existing files?" question the default (Enter) and Escape button is Cancel, never Replace."""
        from StudioLib import autosave_io, niftiexport_ui

        node = self._volume("keep scan", np.zeros((3, 3, 3), np.int16), np.eye(4))
        dialog = niftiexport_ui.exportNodesDialog([node], show=False, executor=autosave_io.InlineExecutor())
        dialog.setFolder(self.out)
        seen = {}

        def answer():
            for widget in qt.QApplication.topLevelWidgets():
                if widget.isVisible() and widget.objectName == "StudioNiftiExportReplaceQuestion":
                    default, escape = widget.defaultButton(), widget.escapeButton()
                    seen["default"] = default.text if default is not None else None
                    seen["escape"] = escape.text if escape is not None else None
                    if default is not None:
                        default.click()  # what Enter does
                    else:
                        widget.reject()
                    return
            qt.QTimer.singleShot(50, answer)

        qt.QTimer.singleShot(50, answer)
        self.assertFalse(dialog.confirmOverwrite([os.path.join(self.out, "keep_scan.nii.gz")]))
        self.assertEqual(seen, {"default": "Cancel", "escape": "Cancel"})
        dialog.close()
        slicer.mrmlScene.RemoveNode(node)

    def test_08ad_write_failure_is_plain_and_keeps_the_dialog_open(self):
        """A folder that does not accept files: a plain message (no errno, no temporary file name), a warning in the log (no
        error badge), and the dialog stays open so that another folder can be chosen.
        """
        import logging

        from StudioLib import autosave_io, niftiexport_ui

        class Failing(autosave_io.InlineExecutor):
            def submit(self, function, *args, onDone=None, onError=None):
                onError(PermissionError(13, "Permission denied", os.path.join(self.folder, "sub-01.nii.gz.4242.tmp")))

        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append((record.levelno, record.getMessage()))

        handler = Handler()
        logging.getLogger().addHandler(handler)
        toasts = []
        original = niftiexport_ui.message
        niftiexport_ui.message = lambda text, **kwargs: toasts.append(text)
        node = self._volume("unwritable scan", np.zeros((3, 3, 3), np.int16), np.eye(4))
        try:
            executor = Failing()
            executor.folder = self.out
            dialog = niftiexport_ui.exportNodesDialog([node], show=False, executor=executor)
            dialog.show()
            dialog.setFolder(self.out)
            runner = dialog.export()
            self.assertTrue(runner.wait(30))
            self.assertEqual(len(toasts), 1)
            self.assertIn("could not be written", toasts[0])
            self.assertIn("Permission denied", toasts[0])
            for raw in (".tmp", "Errno", "4242", "Nothing was exported"):
                self.assertNotIn(raw, toasts[0])
            self.assertEqual(runner.result.errors, ["unwritable scan: Permission denied"])
            self.assertFalse([text for level, text in records if level >= logging.ERROR and "NIfTI export" in text], "expected failures are not errors")
            self.assertTrue([text for level, text in records if level == logging.WARNING and "NIfTI export" in text])
            # Still open and usable: another folder can be chosen and exported to
            self.assertTrue(dialog.isVisible())
            self.assertIsNone(dialog.runner)
            self.assertTrue(dialog.exportButton.isEnabled())
            self.assertTrue(dialog.list.isEnabled())
            self.assertTrue(dialog.changeButton.isEnabled())
            dialog.executor = autosave_io.InlineExecutor()
            runner = dialog.export()
            self.assertTrue(runner.wait(30))
            self.assertEqual([os.path.basename(path) for path in runner.result.images], ["unwritable_scan.nii.gz"])
            self.assertFalse(dialog.isVisible())
        finally:
            niftiexport_ui.message = original
            logging.getLogger().removeHandler(handler)
            slicer.mrmlScene.RemoveNode(node)

    def test_08aa_dialog_grows_when_the_summary_wraps(self):
        """Select all makes the summary wrap onto more lines: the dialog grows and the image list stays above the folder card."""
        from StudioLib import autosave_io, niftiexport_ui

        nodes = []
        for index in range(12):
            name = f"scan {index:02d}" if index != 3 else "sub-03_ses-preoperative_acq-highresolution_run-01_T1w_with_a_really_long_descriptive_name"
            nodes.append(self._volume(name, np.zeros((3, 3, 3), np.int16), np.eye(4)))
        dialog = niftiexport_ui.exportNodesDialog([nodes[0]], show=False, executor=autosave_io.InlineExecutor())
        dialog.setFolder(self.out)
        dialog.show()
        for _ in range(5):
            slicer.app.processEvents()
        card = dialog.findChild(qt.QWidget, "StudioNiftiExportFolderCard")
        heightBefore = dialog.height
        summaryBefore = dialog.summaryLabel.height
        dialog.selectAll()
        for _ in range(5):
            slicer.app.processEvents()
        self.assertGreater(dialog.summaryLabel.height, summaryBefore, "the summary wraps onto more lines")
        self.assertGreater(dialog.height, heightBefore, "the dialog grows with the summary")
        listBottom = dialog.list.geometry.y() + dialog.list.geometry.height()
        self.assertLessEqual(listBottom, card.geometry.y(), "the image list is not drawn under the folder card")
        self.assertEqual(dialog.list.height, dialog.list.maximumHeight, "with room, the list shows all its rows")
        dialog.close()

    def test_08ac_dialog_opens_at_the_height_its_content_needs(self):
        """With a segmentation listed (the tall layout) the first show is not capped at 2/3 of the screen height (Qt's adjustSize
        limit, 512 px at 1366x768): the list stays above the folder card and the segmented controls keep their size.
        """
        from StudioLib import autosave_io, niftiexport_ui, ui

        first = self._volume("scan", np.ones((3, 3, 3), np.int16), np.eye(4))
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (sub-03_ses-01_acq-highres_T1w_with_a_rather_long_descriptive_name)")
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(first)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray(np.ones((3, 3, 3), np.uint8), segmentation, segmentId, first)
        others = [self._volume(f"sub-03_{name}_resampled_onto_a_coarse_grid_for_the_test", np.ones((3, 3, 3), np.int16), np.eye(4)) for name in ("FLAIR", "T2w")]
        dialog = niftiexport_ui.exportNodesDialog([segmentation] + others, show=False, executor=autosave_io.InlineExecutor())
        dialog.setFolder(self.out)
        dialog.show()
        for _ in range(5):
            slicer.app.processEvents()
        layout = dialog.layout()
        screenHeight = ui.screenOf(dialog).availableGeometry.height()
        needed = layout.totalHeightForWidth(dialog.width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        self.assertGreaterEqual(dialog.height, min(needed, screenHeight - 40), "the dialog opens at the height its content needs")
        card = dialog.findChild(qt.QWidget, "StudioNiftiExportFolderCard")
        listBottom = dialog.list.geometry.y() + dialog.list.geometry.height()
        self.assertLessEqual(listBottom, card.geometry.y(), "the image list is not drawn under the folder card")
        for name in ("StudioNiftiExportFormat",):
            control = dialog.findChild(qt.QWidget, name)
            self.assertGreaterEqual(control.height, control.sizeHint.height(), f"{name} is not clipped")
        dialog.close()

    def test_08ae_short_screen_shrinks_the_list_instead_of_drawing_it_under_the_folder_card(self):
        """On a screen too short for the whole dialog (1366x768 at a 125 % app font) the dialog is capped to the screen: the image
        list gives up rows and scrolls, it is not drawn under the folder card (BB-1 nifti-export).
        """
        from types import SimpleNamespace

        from StudioLib import autosave_io, niftiexport_ui, ui

        nodes = [self._volume(f"sub-{index:02d}_ses-baseline_acq-mprage_run-{index}_T1w", np.ones((3, 3, 3), np.int16), np.eye(4)) for index in range(8)]
        original = ui.screenOf
        # A screen whose usable height is 500 px: less than the dialog needs with 6 list rows
        ui.screenOf = lambda widget: SimpleNamespace(availableGeometry=SimpleNamespace(height=lambda: 500))
        dialog = None
        try:
            dialog = niftiexport_ui.exportNodesDialog(nodes, show=False, executor=autosave_io.InlineExecutor())
            dialog.setFolder(self.out)
            dialog.selectAll()
            dialog.show()
            for _ in range(5):
                slicer.app.processEvents()
            layout = dialog.layout()
            self.assertLess(dialog.height, layout.totalHeightForWidth(dialog.width) if layout.hasHeightForWidth() else layout.totalSizeHint().height(),
                            "the dialog is capped by the screen (the test needs a squeezed layout)")
            card = dialog.findChild(qt.QWidget, "StudioNiftiExportFolderCard")
            listBottom = dialog.list.geometry.y() + dialog.list.geometry.height()
            self.assertLessEqual(listBottom, card.geometry.y(), "the image list is not drawn under the folder card")
            self.assertLess(dialog.list.height, dialog.list.maximumHeight, "the list gave up rows")
            self.assertGreaterEqual(dialog.list.height, dialog.list.minimumHeight)
            self.assertGreater(dialog.list.verticalScrollBar().maximum, 0, "the list scrolls")
        finally:
            ui.screenOf = original
            if dialog is not None:
                dialog.close()

    def test_08ab_long_folder_path_is_elided_and_keeps_the_end(self):
        """A destination path wider than the folder card is elided with an ellipsis that keeps 'Exports/nifti'; the tooltip has it all."""
        from StudioLib import autosave_io, niftiexport_ui

        node = self._volume("sub-07_T1w", np.zeros((3, 3, 3), np.int16), np.eye(4))
        dialog = niftiexport_ui.exportNodesDialog([node], show=False, executor=autosave_io.InlineExecutor())
        folder = os.path.join(self.out, "Projects", "sub-07 T1w and a rather long project name for this scan", "Exports", "nifti")
        dialog.setFolder(folder)
        dialog.show()
        for _ in range(5):
            slicer.app.processEvents()
        label = dialog.folderLabel
        metrics = qt.QFontMetrics(label.font)
        shown = str(label.text)
        self.assertLessEqual(metrics.horizontalAdvance(shown), label.width, "the path fits the label")
        self.assertIn("\u2026", shown, "the path is elided with an ellipsis")
        self.assertTrue(shown.endswith("Exports/nifti"), shown)
        self.assertIn(folder, label.toolTip, "the whole path is the tooltip")
        # Resizing keeps the path elided to the new width and the tooltip complete
        dialog.resize(dialog.width + 300, dialog.height)
        for _ in range(5):
            slicer.app.processEvents()
        self.assertLessEqual(metrics.horizontalAdvance(str(label.text)), label.width)
        self.assertIn(folder, label.toolTip)
        dialog.close()

    def test_08b_message_names_the_skipped_segments_and_other_notes(self):
        """The message after an export carries the notes (empty segments skipped, empty threshold mask, transform), not only the log."""
        from StudioLib import niftiexport_ui

        result = self.export.Result(self.out)
        result.images = [os.path.join(self.out, "Lesions_label-Lesion_mask.nii.gz")]
        result.notes = ["Lesions: empty segments were skipped: Edema"]
        shown = []
        original = niftiexport_ui.message
        niftiexport_ui.message = lambda text, **kwargs: shown.append(text)
        try:
            niftiexport_ui.announce(result)
            result.notes += ["No voxel of T passes the threshold: the mask is empty", "T: its transform was applied to the exported copy"]
            niftiexport_ui.announce(result)
        finally:
            niftiexport_ui.message = original
        self.assertIn("Exported 1 NIfTI image", shown[0])
        self.assertIn("Edema", shown[0])
        self.assertIn("empty segments were skipped", shown[0])
        self.assertIn("Edema", shown[1])
        self.assertIn("No voxel of T passes", shown[1])
        self.assertIn("1 more note", shown[1])

    def test_09_project_export(self):
        """Export project as NIfTI: images, lesions as masks and maps into <project>/Exports/nifti with BIDS-like names and a README."""
        from StudioLib import autosave_io, niftiexport_ui, projects, projects_ui, templates

        project = projects.createProject("NIfTI project")
        projects.setCurrentProject(project)
        folder = self.export.projectExportFolder()
        self.assertEqual(folder, os.path.join(project.path, "Exports", "nifti"))
        ijkToRas = np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]])
        scan = self._phantom("sub-07_T1w", ijkToRas)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (sub-07_T1w)")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion", "Lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray((slicer.util.arrayFromVolume(scan) > 0).astype(np.uint8), segmentation, segmentId, scan)
        statmap = self._volume("sub-07_network", np.ones((21, 21, 21), np.float32), ijkToRas)
        statmap.SetAttribute("LNM.Statistic", "T")
        template = slicer.util.loadVolume(templates.builtinTemplates()[0].imagePath)
        template.SetAttribute("Studio.Role", "template")
        dialog = niftiexport_ui.exportProjectDialog(show=False, executor=autosave_io.InlineExecutor(), mapOptions={})
        self.assertEqual(os.path.normpath(dialog.folder), os.path.normpath(folder))
        self.assertEqual(dialog.segmentationMode(), self.export.SEGMENTATION_MASKS)
        self.assertNotIn(template.GetID(), [node.GetID() for node in dialog.checkedNodes()])
        announced = int(dialog.folderMeta.text.split()[0])
        runner = dialog.export(confirm=lambda existing: self.fail(f"{existing} exist"))
        self.assertTrue(runner.wait(60))
        self.assertEqual(runner.result.errors, [])
        names = sorted(os.path.basename(path) for path in _allFiles(folder))
        # The folder card counts every file written, README.txt included
        self.assertEqual(announced, len(names))
        for expected in ("README.txt", "sub-07_T1w.nii.gz", "sub-07_T1w.json", "sub-07_label-Lesion_desc-lesion_mask.nii.gz",
                         "sub-07_stat-T_statmap.nii.gz"):
            self.assertIn(expected, names)
        with open(os.path.join(folder, "README.txt"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("sub-07_T1w.nii.gz", readme)
        self.assertIn("Templates are not copied", readme)
        np.testing.assert_allclose(self._markerRas(os.path.join(folder, "sub-07_label-Lesion_desc-lesion_mask.nii.gz")), (-6.0, 2.0, 4.0), atol=1e-4)
        sidecar = self._sidecar(os.path.join(folder, "sub-07_label-Lesion_desc-lesion_mask.nii.gz"))
        self.assertEqual(sidecar["Kind"], "mask")
        self._assertNoAbsolutePaths(project.path)
        # The project menu has the entry
        menu = qt.QMenu()
        actions = projects_ui.addStatusMenuActions(menu)
        self.assertIn("nifti", actions)
        self.assertEqual(actions["nifti"].objectName, "StudioSaveStatusExportNiftiAction")
        menu.deleteLater()

    def test_09a_project_reexport_removes_earlier_files(self):
        """Export project as NIfTI again: the masks of renamed, deleted or erased segments of the earlier export are named in
        the question and removed, so that Exports/nifti matches the project; a file the user put there stays and the README names it.
        """
        from StudioLib import autosave_io, niftiexport_ui, projects

        project = projects.createProject("NIfTI re-export")
        projects.setCurrentProject(project)
        folder = self.export.projectExportFolder(create=True)
        ijkToRas = np.array([[-1.0, 0, 0, 10], [0, -1.0, 0, 10], [0, 0, 1.0, -10], [0, 0, 0, 1]])
        scan = self._phantom("sub-03_T1w", ijkToRas)
        segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions (sub-03_T1w)")
        segmentation.CreateDefaultDisplayNodes()
        segmentation.SetReferenceImageGeometryParameterFromVolumeNode(scan)
        mask = (slicer.util.arrayFromVolume(scan) > 0).astype(np.uint8)
        for name in ("Core", "Rim"):
            segmentId = segmentation.GetSegmentation().AddEmptySegment(name, name)
            slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentation, segmentId, scan)
        # A file the user put into the folder (no Studio sidecar, never listed by a README) is never removed
        manual = os.path.join(folder, "manual_mask.nii.gz")
        with open(manual, "wb") as f:
            f.write(b"not a Studio file")

        def export(answer):
            asked = []

            dialog = niftiexport_ui.exportProjectDialog(show=False, executor=autosave_io.InlineExecutor(), mapOptions={})

            def confirm(files):
                # The files that would be replaced, then those of the earlier export that would be removed (named in the question)
                self.assertEqual(files, dialog._existing + dialog._stale)
                asked.append((sorted(os.path.basename(path) for path in dialog._existing), sorted(os.path.basename(path) for path in dialog._stale)))
                return answer

            notice = dialog.existingNotice.findChild("QLabel", "StudioNoticeBoxText").text if dialog.existingNotice.isVisibleTo(dialog) else ""
            shown = []
            original = niftiexport_ui.message
            niftiexport_ui.message = lambda text, **kwargs: shown.append(text)
            try:
                runner = dialog.export(confirm=confirm)
                if runner is not None:
                    self.assertTrue(runner.wait(60))
                    self.assertEqual(runner.result.errors, [])
            finally:
                niftiexport_ui.message = original
                dialog.close()
            return runner, asked, notice, shown

        runner, asked, _notice, _shown = export(True)
        self.assertEqual(asked, [])
        names = sorted(os.listdir(folder))
        for expected in ("sub-03_label-Core_desc-lesion_mask.nii.gz", "sub-03_label-Rim_desc-lesion_mask.json"):
            self.assertIn(expected, names)
        # Rename Rim, delete Core: the second export names the four files of the earlier export in its question
        segments = segmentation.GetSegmentation()
        segments.GetSegment("Rim").SetName("Penumbra")
        segments.RemoveSegment("Core")
        runner, asked, notice, _shown = export(False)
        self.assertIsNone(runner)
        stale = ["sub-03_label-Core_desc-lesion_mask.json", "sub-03_label-Core_desc-lesion_mask.nii.gz",
                 "sub-03_label-Rim_desc-lesion_mask.json", "sub-03_label-Rim_desc-lesion_mask.nii.gz"]
        self.assertEqual(asked[0][1], stale)
        self.assertNotIn("manual_mask.nii.gz", asked[0][1])
        self.assertIn("4 files of an earlier export that are not part of this one will be removed", notice)
        self.assertTrue(all(os.path.exists(os.path.join(folder, name)) for name in stale), "Cancel removes nothing")
        # The real question names them apart from the files it replaces
        dialog = niftiexport_ui.exportProjectDialog(show=False, executor=autosave_io.InlineExecutor(), mapOptions={})
        seen = {}

        def answer():
            for widget in qt.QApplication.topLevelWidgets():
                if widget.isVisible() and widget.objectName == "StudioNiftiExportReplaceQuestion":
                    seen["text"] = widget.text
                    widget.defaultButton().click()
                    return
            qt.QTimer.singleShot(50, answer)

        qt.QTimer.singleShot(50, answer)
        self.assertFalse(dialog.confirmOverwrite(dialog._existing, dialog._stale))
        dialog.close()
        self.assertIn("4 files of an earlier export in nifti are not part of this one and will be removed:", seen["text"])
        self.assertIn("sub-03_label-Core_desc-lesion_mask.nii.gz", seen["text"])
        runner, asked, _notice, shown = export(True)
        self.assertEqual(asked[0][1], stale)
        names = sorted(os.listdir(folder))
        for name in stale:
            self.assertNotIn(name, names)
        self.assertIn("sub-03_label-Penumbra_desc-lesion_mask.nii.gz", names)
        self.assertIn("manual_mask.nii.gz", names)
        self.assertEqual(sorted(os.path.basename(path) for path in runner.result.removed), stale)
        self.assertIn("removed 4 files of an earlier export", shown[0])
        with open(os.path.join(folder, "README.txt"), encoding="utf-8") as f:
            readme = f.read()
        self.assertNotIn("label-Core", readme)
        self.assertNotIn("label-Rim", readme)
        self.assertIn("Also in this folder, not written by this export:\n- manual_mask.nii.gz", readme)
        self.assertEqual(self.export.readmeImages(os.path.join(folder, "README.txt")),
                         ["sub-03_T1w.nii.gz", "sub-03_label-Penumbra_desc-lesion_mask.nii.gz"])
        # A segment erased to empty is skipped, and its earlier mask goes too
        segmentId = segments.GetSegmentIdBySegmentName("Penumbra")
        slicer.util.updateSegmentBinaryLabelmapFromArray(np.zeros_like(mask), segmentation, segmentId, scan)
        segments.AddEmptySegment("Edge", "Edge")
        slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentation, segments.GetSegmentIdBySegmentName("Edge"), scan)
        runner, asked, _notice, shown = export(True)
        self.assertEqual(asked[0][1], ["sub-03_label-Penumbra_desc-lesion_mask.json", "sub-03_label-Penumbra_desc-lesion_mask.nii.gz"])
        self.assertFalse(any("Penumbra" in name for name in os.listdir(folder)))
        self.assertIn("Edge", " ".join(os.listdir(folder)))
        # The script API asks too (FileExistsError) and removes them with overwrite=True
        segments.RemoveSegment(segments.GetSegmentIdBySegmentName("Edge"))
        readme = (os.path.join(folder, "README.txt"), "NIfTI export")
        jobs = self.export.planProject()
        with self.assertRaises(FileExistsError):
            self.export.runJobs(jobs, readme=readme)
        result = self.export.runJobs(self.export.planProject(), overwrite=True, readme=readme)
        self.assertEqual(sorted(os.path.basename(path) for path in result.removed),
                         ["sub-03_label-Edge_desc-lesion_mask.json", "sub-03_label-Edge_desc-lesion_mask.nii.gz"])
        self.assertEqual(sorted(name for name in os.listdir(folder) if "_mask" in name), ["manual_mask.nii.gz"])
        self._assertNoAbsolutePaths(project.path)

    def test_10_entry_points(self):
        """Library's Open now ⋯, Draw's lesion ⋯ and Search list the exports; a figure is not exportable."""
        from StudioLib import draw, library, niftiexport_ui

        scan = self._phantom("scan", np.array([[1.0, 0, 0, -10], [0, 1.0, 0, -10], [0, 0, 1.0, -10], [0, 0, 0, 1]]))
        figure = self._volume("figure", np.zeros((1, 10, 10), np.uint8), np.eye(4))
        figure.SetAttribute("Studio.Role", "figure")
        self.assertTrue(self.export.unavailableReason(figure))
        self.assertNotIn(figure, self.export.exportableNodes())
        card = library.OpenNowCard()
        try:
            card.refresh()
            menus = card.widget.findChildren("QMenu", "StudioOpenNowMenu")
            self.assertTrue(menus)
            exportActions = [action for menu in menus for action in menu.actions() if action.objectName == "StudioOpenNowExportNifti"]
            self.assertEqual(len(exportActions), len(menus))
            enabled = {action.isEnabled() for action in exportActions}
            self.assertEqual(enabled, {True, False}, "enabled for the scan, disabled for the figure")
        finally:
            card.dispose()
        titles = [entry[0] for entry in draw.lesionMenuActions() if entry is not None]
        self.assertIn("Export each segment as a NIfTI mask…", titles)
        self.assertLess(titles.index("Save lesion as…"), titles.index("Export each segment as a NIfTI mask…"))
        ids = [entry[0] for entry in niftiexport_ui.paletteEntries()]
        self.assertEqual(ids, ["niftiexport.export", "niftiexport.project", "niftiexport.dicom"])
        # Convert DICOM to NIfTI without DICOM series: a message, no dialog
        self.assertIsNone(niftiexport_ui.convertDicomDialog(show=False))
        self.assertIsNotNone(scan)
