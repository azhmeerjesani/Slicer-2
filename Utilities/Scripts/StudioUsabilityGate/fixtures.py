"""Offline fixtures of the usability gate: everything is generated here from the bundled MNI template (no download).

``makeFixtures(directory)`` writes under ``directory`` and returns a dictionary of paths and facts
(``fixtures.json`` holds the same):

- ``patient_T1.nii.gz``: the template resampled to 1 mm with the origin shifted by 37 mm, so it is NOT an MNI grid,
  and a hypointense right-frontal blob; ``patient_T1_lesion.nii.gz`` is a lesion mask on its grid;
- ``case1_fig2.png`` (radiological, lesion in the patient's right thalamus) and ``case2_fig1_neuro.png`` (neurological
  convention, lesion in the patient's LEFT frontal lobe), drawn with QImage and QPainter from template slices; the
  lesion is ringed in yellow so that the gate can find it on screen, and an "R" marks the patient's right;
- ``cohort/``: six lesion masks ``sub-01..06_lesion.nii.gz`` on the FSL MNI152NLin6Asym 2 mm grid (91x109x91) and
  ``participants.tsv``;
- ``cohort40/``: 40 tiny NIfTI files (a scan and a lesion for each of 20 subjects);
- ``sub-05_desc-T_funcmap.nii.gz``: a float T map on the NLin6Asym grid with a positive and a negative tail;
- ``dicom/``: a 10-slice synthetic MR series (pydicom; skipped and reported as ``dicomSkipped`` when it is missing).
"""

import json
import os

import numpy as np

#: Grid of the FSL MNI152NLin6Asym 2 mm template (NIfTI sform: x = 90 - 2 i, y = -126 + 2 j, z = -72 + 2 k)
NLIN6_SIZE = (91, 109, 91)
NLIN6_SPACING = 2.0
NLIN6_ORIGIN_RAS = (90.0, -126.0, -72.0)
#: Shift of the patient scan origin: the scan is not on an MNI grid
PATIENT_ORIGIN_SHIFT_MM = 37.0
TEMPLATE_FILE_NAME = "tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz"


def templatePath():
    """Path of the bundled 2 mm MNI152NLin2009cAsym brain (the offline template of "Start on MNI template")."""
    import slicer

    candidates = []
    try:
        moduleDirectory = os.path.dirname(slicer.modules.sampledata.path)
        candidates.append(os.path.join(moduleDirectory, "Resources", "Data", TEMPLATE_FILE_NAME))
        candidates.append(os.path.join(moduleDirectory, "SampleData", "Resources", "Data", TEMPLATE_FILE_NAME))
    except Exception:
        pass
    sourceDirectory = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    candidates.append(os.path.join(sourceDirectory, "Modules", "Scripted", "SampleData", "Resources", "Data", TEMPLATE_FILE_NAME))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError("The bundled MNI template was not found: " + ", ".join(candidates))


def _sitk():
    import SimpleITK as sitk

    return sitk


def nlin6Image(array, sitkPixelType=None):
    """A SimpleITK image on the NLin6Asym 2 mm grid from an array indexed [k, j, i] (91 x 109 x 91)."""
    sitk = _sitk()
    image = sitk.GetImageFromArray(array)
    if sitkPixelType is not None:
        image = sitk.Cast(image, sitkPixelType)
    image.SetSpacing((NLIN6_SPACING,) * 3)
    # SimpleITK works in LPS: x_L = -x_R, y_L = -y_R. x_R = 90 - 2 i gives x_L = -90 + 2 i; y_R = -126 + 2 j gives y_L = 126 - 2 j
    image.SetOrigin((-NLIN6_ORIGIN_RAS[0], -NLIN6_ORIGIN_RAS[1], NLIN6_ORIGIN_RAS[2]))
    image.SetDirection((1, 0, 0, 0, -1, 0, 0, 0, 1))
    return image


def _ball(shape, centerIJK, radiusVoxels):
    k, j, i = np.ogrid[: shape[0], : shape[1], : shape[2]]
    return (i - centerIJK[0]) ** 2 + (j - centerIJK[1]) ** 2 + (k - centerIJK[2]) ** 2 <= radiusVoxels**2


def nlin6IJK(rasMm):
    """Voxel (i, j, k) of an MNI RAS point on the NLin6Asym 2 mm grid."""
    return ((NLIN6_ORIGIN_RAS[0] - rasMm[0]) / 2.0, (rasMm[1] - NLIN6_ORIGIN_RAS[1]) / 2.0, (rasMm[2] - NLIN6_ORIGIN_RAS[2]) / 2.0)


def _writeImage(image, path):
    sitk = _sitk()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sitk.WriteImage(image, path, useCompression=path.endswith(".gz"))
    return path


def makePatientScan(directory, facts):
    """patient_T1.nii.gz (1 mm, shifted origin, right-frontal blob) and patient_T1_lesion.nii.gz (a mask on its grid)."""
    sitk = _sitk()
    template = sitk.ReadImage(templatePath())
    spacing = (1.0, 1.0, 1.0)
    size = [round(template.GetSize()[axis] * template.GetSpacing()[axis] / spacing[axis]) for axis in range(3)]
    resampler = sitk.ResampleImageFilter()
    resampler.SetOutputSpacing(spacing)
    resampler.SetSize(size)
    resampler.SetOutputDirection(template.GetDirection())
    resampler.SetOutputOrigin(template.GetOrigin())
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetDefaultPixelValue(0)
    image = resampler.Execute(template)
    array = sitk.GetArrayFromImage(image).astype(np.float32)
    high = float(np.percentile(array[array > 0], 99.5)) if (array > 0).any() else 1.0
    array = np.clip(array / high * 800.0, 0, 1000)
    # Physical points of the template are in LPS: the right-frontal blob is at MNI RAS (30, 40, 20)
    blobRas = (30.0, 40.0, 20.0)
    blobIndex = image.TransformPhysicalPointToContinuousIndex((-blobRas[0], -blobRas[1], blobRas[2]))
    ball = _ball(array.shape, blobIndex, 8.0)
    hypointense = array.copy()
    hypointense[ball] = hypointense[ball] * 0.3
    lesion = np.zeros(array.shape, dtype=np.uint8)
    lesion[_ball(array.shape, blobIndex, 7.0)] = 1
    scan = sitk.GetImageFromArray(hypointense.astype(np.int16))
    scan.CopyInformation(image)
    # Not an MNI grid: origin shifted by 37 mm along every axis
    origin = tuple(value + PATIENT_ORIGIN_SHIFT_MM for value in image.GetOrigin())
    scan.SetOrigin(origin)
    mask = sitk.GetImageFromArray(lesion)
    mask.CopyInformation(scan)
    scanPath = _writeImage(scan, os.path.join(directory, "patient_T1.nii.gz"))
    maskPath = _writeImage(mask, os.path.join(directory, "patient_T1_lesion.nii.gz"))
    facts["patientScan"] = scanPath
    facts["patientLesion"] = maskPath
    facts["patientBlobScannerRAS"] = [blobRas[0] + PATIENT_ORIGIN_SHIFT_MM * -1, blobRas[1] + PATIENT_ORIGIN_SHIFT_MM * -1, blobRas[2] + PATIENT_ORIGIN_SHIFT_MM]
    facts["patientBlobMniRAS"] = list(blobRas)
    facts["patientOriginShiftMm"] = PATIENT_ORIGIN_SHIFT_MM
    facts["patientSize"] = size


def _grayQImage(sliceArray, scale):
    """A scaled QImage of a 2D array (rows top to bottom)."""
    import qt

    rows, columns = sliceArray.shape
    low, high = float(sliceArray.min()), float(np.percentile(sliceArray, 99.5)) or 1.0
    gray = np.clip((sliceArray - low) / max(high - low, 1e-6) * 255.0, 0, 255).astype(np.uint8)
    small = qt.QImage(columns, rows, qt.QImage.Format_RGB32)
    for row in range(rows):
        for column in range(columns):
            value = int(gray[row, column])
            small.setPixel(column, row, qt.QColor(value, value, value).rgb())
    return small.scaled(columns * scale, rows * scale, qt.Qt.IgnoreAspectRatio, qt.Qt.SmoothTransformation)


def _drawFigure(path, panels, neurological, lesionMni, scale=3, facts=None, key=""):
    """Draw a figure of axial template panels: lesion (dark blob with a yellow ring) and an 'R' marker on the patient's right.

    ``neurological`` mirrors the panels (the patient's left on the screen left).
    """
    import qt

    sitk = _sitk()
    template = sitk.DICOMOrient(sitk.ReadImage(templatePath()), "LPS")
    array = sitk.GetArrayFromImage(template).astype(np.float32)
    margin = 12
    panelImages = []
    lesionPixels = []
    for zMni in panels:
        index = template.TransformPhysicalPointToContinuousIndex((-lesionMni[0], -lesionMni[1], zMni))
        sliceIndex = round(template.TransformPhysicalPointToContinuousIndex((0.0, 0.0, zMni))[2])
        sliceIndex = min(max(sliceIndex, 0), array.shape[0] - 1)
        image = _grayQImage(array[sliceIndex], scale)
        column = (index[0] + 0.5) * scale
        row = (index[1] + 0.5) * scale
        if neurological:
            # Mirror: the patient's left on the screen left
            image = image.mirrored(True, False)
            column = image.width() - column
        painter = qt.QPainter(image)
        painter.setRenderHint(qt.QPainter.Antialiasing, True)
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QColor(20, 20, 20))
        painter.drawEllipse(qt.QPointF(column, row), 11.0, 8.0)
        painter.setBrush(qt.QBrush())
        painter.setPen(qt.QPen(qt.QColor(255, 255, 0), 3))
        painter.drawEllipse(qt.QPointF(column, row), 17.0, 13.0)
        painter.end()
        panelImages.append(image)
        lesionPixels.append((column, row))
    width = sum(image.width() for image in panelImages) + margin * (len(panelImages) + 1)
    height = panelImages[0].height() + margin * 2 + 54
    figure = qt.QImage(width, height, qt.QImage.Format_RGB32)
    figure.fill(qt.QColor(255, 255, 255))
    painter = qt.QPainter(figure)
    font = qt.QFont("DejaVu Sans", 14)
    font.setBold(True)
    painter.setFont(font)
    x = margin
    lesionInFigure = []
    panelRects = []
    for number, image in enumerate(panelImages):
        painter.drawImage(x, margin, image)
        panelRects.append([x, margin, image.width(), image.height()])
        painter.setPen(qt.QColor(255, 255, 255))
        painter.drawText(x + 6, margin + 22, "AB"[number])
        # The 'R' marks the patient's right: on the screen left in the radiological convention, on the right in the neurological one
        if neurological:
            painter.drawText(x + image.width() - 24, margin + image.height() // 2, "R")
        else:
            painter.drawText(x + 8, margin + image.height() // 2, "R")
        lesionInFigure.append((x + lesionPixels[number][0], margin + lesionPixels[number][1]))
        x += image.width() + margin
    painter.setPen(qt.QColor(0, 0, 0))
    caption = "Axial T1, neurological convention" if neurological else "Axial T1, radiological convention"
    painter.setFont(qt.QFont("DejaVu Sans", 9))
    painter.drawText(margin, height - 18, caption)
    painter.end()
    figure.save(path)
    if facts is not None:
        facts[key] = {
            "path": path,
            "neurological": neurological,
            "size": [width, height],
            "lesionPixels": lesionInFigure,
            "panelRects": panelRects,
            "lesionMniRAS": [lesionMni[0], lesionMni[1], panels[0]],
            "patientSide": "left" if lesionMni[0] < 0 else "right",
        }


def makeFigures(directory, facts):
    # Case 1: radiological convention, lesion in the patient's right thalamus (x > 0)
    _drawFigure(os.path.join(directory, "case1_fig2.png"), (8.0, 12.0), False, (12.0, -18.0), facts=facts, key="case1")
    # Case 2: neurological convention, lesion in the patient's LEFT frontal lobe (x < 0)
    _drawFigure(os.path.join(directory, "case2_fig1_neuro.png"), (14.0,), True, (-34.0, 38.0), facts=facts, key="case2")


def makeCohort(directory, facts):
    """Six lesion masks on the NLin6Asym 2 mm grid and participants.tsv; the T map of sub-05; a folder of 40 tiny files."""
    sitk = _sitk()
    cohort = os.path.join(directory, "cohort")
    os.makedirs(cohort, exist_ok=True)
    centers = [(-30, 20, 10), (30, 18, 8), (-12, -18, 8), (12, -20, 6), (-40, -10, 30), (38, -44, 40)]
    lesions = []
    for number, center in enumerate(centers, 1):
        mask = np.zeros(NLIN6_SIZE[::-1], dtype=np.uint8)
        mask[_ball(mask.shape, nlin6IJK(center), 4.0)] = 1
        path = _writeImage(nlin6Image(mask, sitk.sitkUInt8), os.path.join(cohort, f"sub-{number:02d}_lesion.nii.gz"))
        lesions.append({"path": path, "centerMniRAS": list(center), "voxels": int(mask.sum())})
    with open(os.path.join(cohort, "participants.tsv"), "w") as stream:
        stream.write("participant_id\tgroup\tage\tsex\tsymptom_score\n")
        for number in range(1, 7):
            stream.write(f"sub-{number:02d}\t{'case' if number <= 4 else 'control'}\t{50 + number * 3}\t{'FM'[number % 2]}\t{number * 2.5:.1f}\n")
    facts["cohort"] = {"folder": cohort, "lesions": lesions, "gridSize": list(NLIN6_SIZE)}

    # T map with both tails (a positive peak in the frontal lobe, a negative one in the parietal lobe)
    k, j, i = np.indices(NLIN6_SIZE[::-1], dtype=np.float32)
    positive = nlin6IJK((-30, 20, 10))
    negative = nlin6IJK((38, -44, 40))
    funcmap = (26.0 * np.exp(-(((i - positive[0]) ** 2 + (j - positive[1]) ** 2 + (k - positive[2]) ** 2) / (2 * 9.0**2)))
               - 21.0 * np.exp(-(((i - negative[0]) ** 2 + (j - negative[1]) ** 2 + (k - negative[2]) ** 2) / (2 * 9.0**2))))
    funcmapPath = _writeImage(nlin6Image(funcmap.astype(np.float32), sitk.sitkFloat32), os.path.join(directory, "sub-05_desc-T_funcmap.nii.gz"))
    facts["tmap"] = {"path": funcmapPath, "max": float(funcmap.max()), "min": float(funcmap.min()), "threshold": 14.62}

    cohort40 = os.path.join(directory, "cohort40")
    os.makedirs(cohort40, exist_ok=True)
    rng = np.random.default_rng(5)
    for number in range(1, 21):
        scan = sitk.GetImageFromArray((rng.random((8, 8, 8)) * 100).astype(np.int16))
        scan.SetSpacing((4.0, 4.0, 4.0))
        mask = sitk.GetImageFromArray(np.pad(np.ones((2, 2, 2), np.uint8), 3))
        mask.CopyInformation(scan)
        _writeImage(scan, os.path.join(cohort40, f"sub-{number:02d}_T1w.nii.gz"))
        _writeImage(mask, os.path.join(cohort40, f"sub-{number:02d}_lesion.nii.gz"))
    facts["cohort40"] = {"folder": cohort40, "files": 40}


def makeDicomSeries(directory, facts):
    """A 10-slice synthetic MR series, written with pydicom (reported and skipped when pydicom is missing)."""
    try:
        import pydicom
        from pydicom.dataset import FileDataset, FileMetaDataset
        from pydicom.uid import ExplicitVRLittleEndian, generate_uid
    except ImportError:
        facts["dicomSkipped"] = "pydicom is not importable"
        return
    folder = os.path.join(directory, "dicom")
    os.makedirs(folder, exist_ok=True)
    studyUid, seriesUid, frameUid = generate_uid(), generate_uid(), generate_uid()
    rng = np.random.default_rng(9)
    base = (rng.random((64, 64)) * 200).astype(np.uint16)
    for number in range(10):
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.4"
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        dataset = FileDataset(os.path.join(folder, f"slice{number:02d}.dcm"), {}, file_meta=meta, preamble=b"\0" * 128)
        dataset.SOPClassUID = meta.MediaStorageSOPClassUID
        dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        dataset.PatientName = "Gate^Synthetic"
        dataset.PatientID = "GATE001"
        dataset.PatientBirthDate = "19700101"
        dataset.PatientSex = "O"
        dataset.StudyInstanceUID = studyUid
        dataset.SeriesInstanceUID = seriesUid
        dataset.FrameOfReferenceUID = frameUid
        dataset.StudyDate = "20260101"
        dataset.StudyDescription = "Usability gate"
        dataset.SeriesDescription = "Synthetic T1"
        dataset.Modality = "MR"
        dataset.SeriesNumber = 1
        dataset.StudyID = "1"
        dataset.InstanceNumber = number + 1
        dataset.ImagePositionPatient = [0.0, 0.0, 3.0 * number]
        dataset.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        dataset.PixelSpacing = [2.0, 2.0]
        dataset.SliceThickness = 3.0
        dataset.Rows = 64
        dataset.Columns = 64
        dataset.SamplesPerPixel = 1
        dataset.PhotometricInterpretation = "MONOCHROME2"
        dataset.BitsAllocated = 16
        dataset.BitsStored = 16
        dataset.HighBit = 15
        dataset.PixelRepresentation = 0
        dataset.PixelData = (base + number * 5).astype(np.uint16).tobytes()
        pydicom.dcmwrite(os.path.join(folder, f"slice{number:02d}.dcm"), dataset, enforce_file_format=True)
    facts["dicom"] = {"folder": folder, "slices": 10}


def makeFixtures(directory):
    """Write every fixture under ``directory`` (an existing set is reused when ``fixtures.json`` exists)."""
    os.makedirs(directory, exist_ok=True)
    factsPath = os.path.join(directory, "fixtures.json")
    if os.path.isfile(factsPath):
        with open(factsPath) as stream:
            facts = json.load(stream)
        if all(os.path.exists(value) for key, value in facts.items() if key in ("patientScan", "patientLesion")):
            return facts
    facts = {"directory": directory}
    makePatientScan(directory, facts)
    makeFigures(directory, facts)
    makeCohort(directory, facts)
    makeDicomSeries(directory, facts)
    with open(factsPath, "w") as stream:
        json.dump(facts, stream, indent=1)
    return facts
