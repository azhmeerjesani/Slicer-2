# Export as NIfTI

Slicer Studio writes any image you see as a NIfTI file (`.nii.gz`, or `.nii`
if you prefer) that FSL, SPM, AFNI, MRIcroGL, nibabel and official 3D Slicer
read. Left and right stay exactly as Slicer Studio shows them.

## Where to find it

| What you want | Where |
| --- | --- |
| One or more scans, label maps, segmentations or maps | Library: the **⋯** of a row under *Open now* › **Export as NIfTI…** (check more rows in the dialog to export several at once), or Search (`Ctrl+K`) › *Export as NIfTI…* |
| Every segment of the lesion as its own mask | Draw: the lesion's **⋯** › **Export each segment as a NIfTI mask…** (next to *Save lesion as…*) |
| The network map and a mask of the voxels that pass its threshold | Map: the **⋯** of the panel › **Export map as NIfTI…**, or the Export card › **Map as NIfTI…** |
| The whole project | The save status menu of the top bar, or the project's card in Library › **Export project as NIfTI…**, or Search |
| DICOM series as NIfTI | Open the DICOM folder (*Open folder…*), then Search › **Convert DICOM to NIfTI** |

*Save lesion* (`Ctrl+Shift+S`) in Draw still writes the lesion as one NIfTI
mask with its sidecar, as before. In the Classic interface, *File › Save Data*
with the `.nii.gz` format is 3D Slicer's own NIfTI export and is unchanged.

## The dialog

- **Images**: everything that is open and can be written as NIfTI. The ones you
  picked are checked; check others to export them at once. Figures (pictures of
  published papers) are not listed: they have no position in space.
- **Folder**: `Exports/nifti/` of the open project, else the last folder you
  used. *Change…* picks another one.
- **Format**: `.nii.gz` (compressed, the default) or `.nii`.
- **Segmentations** (shown when a segmentation is checked): **One label map**
  (values 1, 2, 3… in the order of the segments) or **One mask per segment**
  (0/1 each). **Grid**: the segmentation's own grid (the default: nothing is
  resampled), or another open image, onto which the masks are resampled with
  nearest-neighbour interpolation (for example the MNI template).
- A summary lists the files that will be written. If some already exist, a
  warning says so, and *Export* asks before it replaces them.
- The images are written in the background with a progress bar; `Esc` or
  *Cancel* stops after the current image. A message then says what was
  written, with **Show in Finder** (Explorer, Files).

## What each export writes

| Export | Files |
| --- | --- |
| A scan or other image | `<name>.nii.gz` with the image's own data type (int16, float32, uint8…). Window and level are display settings and are never applied to the voxels. |
| A label map | `<name>.nii.gz` with the same values, and their names and colours in the sidecar. |
| A segmentation as one label map | `<name>_dseg.nii.gz` (values 1…N), `<name>_dseg.tsv` (a BIDS look-up table: index, name, colour) and `<name>_dseg.txt` (an ITK-SNAP label description file). Where segments overlap, a voxel has the value of the later segment. |
| A segmentation as one mask per segment | `<name>_label-<segment>_mask.nii.gz` for each segment (uint8, 0 and 1). Empty segments are skipped, and the message says which. |
| A map | `<name>.nii.gz` as float32, and with a threshold applied in Map `<name>_desc-thresholded_mask.nii.gz` (uint8: the voxels with \|value\| ≥ threshold, or one sign). |
| A project | All of the above for every image, lesion (one mask per segment) and map of the project, with BIDS-like names when the names hold a subject (`sub-01_T1w.nii.gz`, `sub-01_label-Lesion_desc-lesion_mask.nii.gz`, `sub-01_stat-T_statmap.nii.gz`) and a `README.txt` that lists the files. Templates are not copied: they are public files known by name. |
| DICOM series | `<series number>_<series description>.nii.gz`, for example `003_T1_MPRAGE.nii.gz`. |

Every image has a `.json` sidecar next to it (BIDS style), which official 3D
Slicer and FSL ignore: the Slicer Studio version, the creation date, the node it
came from, the space (`MNI152NLin2009cAsym`, `MNI152NLin6Asym`, `scanner`…), the
template's id and checksum when the image is on one of your own templates, the
sform/qform code, the data type and units, the segment names and colours of a
segmentation, and the statistic and threshold of a map. A lesion's sidecar also
has the fields that *Save lesion* writes (the scan relative to the sidecar, your
notes and the source reference). Sidecars never contain an absolute path of your
computer, and DICOM sidecars hold only the series number, description and
modality, never the patient's name, identifier or dates.

## Orientation guarantees

- **Left stays left.** The affine of the file (its sform and qform) is the
  image's IJK-to-RAS matrix, exactly as 3D Slicer's own NIfTI writer stores it.
  The voxels are written in the image's own order and are never flipped or
  reoriented. A voxel in the subject's left hemisphere is at negative x (RAS) in
  FSL, SPM, AFNI, MRIcroGL and nibabel. `nibabel.aff2axcodes(img.affine)` gives
  the same axis codes as the image had in Slicer (for example `('L', 'P', 'S')`
  for a DICOM-derived scan).
- **Nothing is resampled** unless you choose a grid for the masks. Oblique and
  DICOM-derived volumes keep their full direction matrix. A left-handed grid is
  stored with `qfac = -1`, as 3D Slicer does.
- **Transforms.** If a transform is applied to an image in the scene, the
  exported copy is moved by it (a linear transform only changes the affine; a
  non-linear one is resampled). The scene itself is not changed, and the sidecar
  says which transform was applied.
- **Codes.** sform and qform are both written and equal:

  | Code | Meaning | When |
  | --- | --- | --- |
  | 4 (`mni_152`) | MNI space | the image is on a known MNI grid (for example the bundled MNI152NLin2009cAsym 2 mm, or FSL's MNI152NLin6Asym 2 mm), on one of your own templates, or is marked as normalized to MNI, and no transform is applied to it |
  | 2 (`aligned_anat`) | aligned | a transform was applied to the exported copy (even to an MNI image: it is no longer on its grid) |
  | 1 (`scanner_anat`) | scanner | everything else: native scans, DICOM series, lesions drawn on them |

  Units are millimetres and seconds (`xyzt_units = 10`), as 3D Slicer writes them.

These guarantees are tested on every build: a phantom with a single bright voxel
in the left hemisphere, stored RAS, LPS, LAS (the FSL MNI grid), left-handed and
oblique, and a synthetic oblique DICOM series loaded by the DICOM module, are
exported and read back with nibabel; the marker is at negative x every time. The
compatibility check (`SlicerFileCompatibilityCheck.py --nifti`) exports in Slicer
Studio and loads the files in official 3D Slicer 5.12.4 and 5.13 (same IJK-to-RAS
matrix and voxels), and the other way round; the affine of each exported file is
the one 3D Slicer's own NIfTI export of the same image gives. For axis-aligned
images the sform is identical to the byte; for oblique ones it differs by at most
one float32 rounding step (below 1e-7 mm).

## Using the files

FSL:

```bash
fslhd sub-01_T1w.nii.gz | grep -E "sform_code|qform_code|sto_xyz"
fslorient -getorient sub-01_T1w.nii.gz   # NEUROLOGICAL or RADIOLOGICAL: the storage order, not the display
fsleyes sub-01_T1w.nii.gz sub-01_label-Lesion_desc-lesion_mask.nii.gz -cm red
```

ITK-SNAP shows the names and colours of a `_dseg` label map with
*Segmentation › Import Label Descriptions…* and the `.txt` file next to it; the
`.tsv` file is the BIDS look-up table of the same labels.

SPM: *Display* or *Check Reg* the `.nii` files (choose `.nii` in the dialog if
your SPM version does not read `.nii.gz`).

Python:

```python
import nibabel as nib
import numpy as np

image = nib.load("sub-01_label-Lesion_desc-lesion_mask.nii.gz")
print(nib.aff2axcodes(image.affine))           # the axis codes, e.g. ('L', 'P', 'S')
ijk = np.argwhere(image.get_fdata() > 0)
ras = nib.affines.apply_affine(image.affine, ijk)
print("left hemisphere" if ras[:, 0].mean() < 0 else "right hemisphere")
```

Official 3D Slicer: drag the file onto the window, or *File › Add Data*. A mask
opens as a label map when you tick *LabelMap* in the options.
