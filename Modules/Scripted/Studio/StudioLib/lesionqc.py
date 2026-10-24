"""Lesion QC v2 of the Draw intake: chips on the Lesion card, recorded one-click fixes, duplicates and the 2D policy (L-28).

Catch tracing errors while the case is still open (LNM addendum 3.10, 7.2, 7.3; SPEC 5.2, D19):

- **Chips** (a third chip row of the Lesion card, ``LesionCard.addChipRow``): side against the reported side (a verdict
  only for laterality, and only on an MNI grid: scanner space says "side unknown"), the traced location against the
  paper's ("Traced: L thalamus 72%, L PLIC 18% | Paper: left paramedian thalamic"), CSF share, mask hygiene, a possible
  duplicate and the dimensionality ("2D · axial · 1 slice → 2 mm"). They run 300 ms after the last edit, on the cropped
  labelmap on the main thread (``LesionQc.lastMs``; the budget is ``CHIP_BUDGET_MS``) and are cached by the labelmap
  content. Atlas and prior crops are cached; the atlases load in a worker thread. Without cached atlases a
  "Get atlases (HOCPAL/HOSPA, CC BY-SA 4.0)" chip runs ``LesionNetworkMappingLib.fetch`` as a background job.
- **Fixes**, each one undo step of the segment editor, with the original file kept (``desc-preflip``, ``desc-prebinarize``,
  ``desc-preclip``) and one line appended to ``<study>/.studio/audit.jsonl``: Mirror L↔R (world x → -x, approximate;
  toggles ``flipped``), Binarize at 0.5, Clip outside brain, Mark duplicate (``duplicate_of`` and ``include`` = 0) and
  Not a duplicate (dismissed pairs are read from the audit log). An undo of a fix in the editor is noticed and takes its
  study values back (the original file and the audit line stay).
- **Duplicates**: a ``threading.Thread`` keeps ``lesionops.LesionIndex`` current with numpy only (D19); its results come
  back to the main thread through a queue and a ``QTimer`` drain. Nothing in a worker touches MRML, VTK or Qt.
- **Dimensionality** (a study Done hook, order ``DONE_HOOK_ORDER``): ``lesionops.detect_dimensionality`` (the figure normal of
  the Figure card when there is one). A 2D or multi2D tracing in a study without ``trace_policy.extrude_2d`` shows a
  non-modal forced-choice card in Draw (Keep single-slice, Extrude to N mm total with fractional edges, Extrude ±N/2 mm
  binary) and blocks Done until it is answered. The choice is stored in ``study.json``. After the lesion is written
  the drawn mask is kept as ``desc-traced``, ``desc-lesion`` is the analysis mask and ``desc-lesionProb`` the fractional
  one; ``dimensionality``, ``planes``, ``n_slices``, ``extrude_mm`` and ``volume_ml`` of the row and the ``extrude``
  record of the sidecar follow. ``reapplyExtrusionPolicy(study)`` regenerates everything from ``desc-traced`` (Search:
  "Re-apply extrusion policy").
- ``qc_flags`` (``lesionops.qc_flags``) are written through the ``StudySession`` on the main thread: by the Done hook, after a
  fix, when the duplicate scan finds a pair and when the checks change for a saved lesion.

Network maps are never shown in Draw (``draw`` removes them from the slice composites); this module draws nothing in the
views. Fork-only settings: none. Files of a study only; no absolute path is written to a shared file (the audit log and the
sidecar hold paths relative to the study).
"""

import contextlib
import importlib
import json
import logging
import math
import os
import queue
import re
import shutil
import threading
import time
import traceback

from StudioLib import lazyNumpy as np
import qt
import slicer
from slicer.i18n import tr as _

from . import ui

#: Chips are evaluated this long after the last change of the lesion
DEBOUNCE_MS = 300
#: Budget of one chip evaluation on the main thread (measured in ``LesionQc.lastMs``)
CHIP_BUDGET_MS = 15.0
#: Object name and key of the chip row in the Lesion card
ROW_KEY = "qc"
ROW_OBJECT_NAME = "StudioLesionQcRow"
#: Order of the Done hook (the figure hook, order 20, runs before it and leaves the plane normal in the context)
DONE_HOOK_ORDER = 50
#: Key and order of the extrusion-policy card in the Draw inspector (before the tools)
POLICY_CARD_KEY = "lesionqc.policy"
POLICY_CARD_ORDER = 28
#: Catalog ids of the atlases of the "Get atlases" chip (Harvard-Oxford cortical and subcortical, CC BY-SA 4.0)
ATLAS_IDS = ("harvard-oxford-cortical-th25-2mm", "harvard-oxford-subcortical-th25-2mm")
#: Priors and labels fetched with them: the lateral ventricle labels (the HOSPA file again) and the 2009c CSF prior
PRIOR_IDS = (("templates", "nlin6asym-hospa-ventricles"), ("templates", "nlin2009casym-res02-csf-probseg"))
CSF_PRIOR_IDS = {"2009": "nlin2009casym-res02-csf-probseg"}
VENTRICLE_IDS = {"fsl6": "nlin6asym-hospa-ventricles"}
BRAIN_MASK_IDS = {"fsl6": "nlin6asym-res02-brain-mask"}
#: Location rows below this percentage of the lesion are not listed; at most this many are
LOCATION_MIN_PCT = 5.0
LOCATION_MAX_ROWS = 3
#: Largest number of lesion voxels sampled for the "outside brain" share (a regular subsample above it)
OUTSIDE_SAMPLE_VOXELS = 20000
#: Policy modes of ``study.json`` ``trace_policy.extrude_2d``
MODE_SINGLE = "single"
MODE_FRACTIONAL = "fractional"
MODE_BINARY = "binary_pm"
DEFAULT_EXTRUDE_MM = 2.0
#: Drain timer of the worker results
DRAIN_MS = 120
#: The duplicate scan starts this long after a lesion was written, and after a study was opened
SCAN_DELAY_MS = 1500
OPEN_SCAN_DELAY_MS = 2500
#: Task state names
SEMANTICS = {
    MODE_SINGLE: "kept as a single slice",
    MODE_FRACTIONAL: "fractional, exact total thickness of {total:g} mm (edge slices fractional)",
    MODE_BINARY: "binary ±{half:g} mm ({voxels} voxels at {step:g} mm)",
}


def _lnm(name):
    return importlib.import_module("LesionNetworkMappingLib." + name)


def _draw():
    return importlib.import_module("StudioLib.draw")


def _studyUi():
    """``StudioLib.study_ui`` when the study library is available, else None."""
    try:
        module = importlib.import_module("StudioLib.study_ui")
    except ImportError:
        return None
    return module if module.isAvailable() else None


def _rowSpaceFamily(study, row):
    """Template family of the space of a subject ("fsl6", "2009", ...), None in scanner (native) or unknown space (the row's space, else the study's)."""
    name = None if _missing(row.get("space")) else row.get("space")
    name = name or study.space
    family = _lnm("geometry").space_family(name)
    return None if family in ("native", "unknown") else family


def _comparable(study, a, b):
    """True when two subjects have lesions on grids of the same template (duplicates are only looked for there)."""
    if a not in study or b not in study:
        return False
    family = _rowSpaceFamily(study, study.subject(a))
    return family is not None and family == _rowSpaceFamily(study, study.subject(b))


def _session():
    studyUi = _studyUi()
    return studyUi.session() if studyUi is not None else None


def _now():
    return _lnm("study").now_iso()


def _shell():
    try:
        return importlib.import_module("StudioLib.shell").instance()
    except Exception:
        return None


def _toast(text, **kwargs):
    shell = _shell()
    if shell is not None:
        try:
            shell.toast(text, **kwargs)
        except Exception:
            logging.error(f"Studio: toast failed:\n{traceback.format_exc()}")


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def _discardWidget(widget):
    """Delete a widget built here once the event is processed (see ``draw._discardWidget``)."""
    for target in (widget, *widget.findChildren("QWidget")):
        ui.bindStyleSheet(target, None)
    widget.hide()
    widget.deleteLater()


#
# Pure numpy part (no Slicer, VTK or Qt; unit-testable)
#


def lesionCrop(maskKji, ijkToRas):
    """``(values[i, j, k] bool, affine, bbox)`` of the lesion cropped to its voxels, None when it is empty.

    :param maskKji: 3D array in numpy order (k, j, i), non-zero voxels are the lesion.
    :param ijkToRas: 4x4 matrix of the (i, j, k) index of ``maskKji``; ``affine`` is the matrix of the crop.
    :param bbox: ``(k0, k1, j0, j1, i0, i1)`` inclusive in ``maskKji``.
    """
    from . import lesioncard

    mask = np.asarray(maskKji)
    mask = mask if mask.dtype == bool else mask != 0
    bbox = lesioncard._boundingBox(mask)
    if bbox is None:
        return None
    k0, k1, j0, j1, i0, i1 = bbox
    values = np.ascontiguousarray(mask[k0:k1 + 1, j0:j1 + 1, i0:i1 + 1].transpose(2, 1, 0))
    shift = np.eye(4)
    shift[:3, 3] = (i0, j0, k0)
    return values, np.asarray(ijkToRas, dtype=float) @ shift, bbox


def worldBox(shape, affine):
    """``(low, high)`` RAS corners (voxel centres) of the box of an array of ``shape`` with ``affine``."""
    corners = np.array([[i, j, k, 1.0] for i in (0, shape[0] - 1) for j in (0, shape[1] - 1) for k in (0, shape[2] - 1)])
    points = (np.asarray(affine, dtype=float) @ corners.T)[:3].T
    return points.min(axis=0), points.max(axis=0)


def cropVolume(data, affine, low, high, pad=2):
    """``(crop, cropAffine)`` of a volume ``data[i, j, k]`` around the RAS box ``low``-``high`` (plus ``pad`` voxels), None
    when the box does not overlap the volume.
    """
    affine = np.asarray(affine, dtype=float)
    inverse = np.linalg.inv(affine)
    corners = np.array([[x, y, z, 1.0] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
    ijk = (inverse @ corners.T)[:3].T
    start = np.maximum(np.floor(ijk.min(axis=0)).astype(int) - pad, 0)
    stop = np.minimum(np.ceil(ijk.max(axis=0)).astype(int) + pad + 1, np.array(data.shape[:3]))
    if np.any(stop <= start):
        return None
    shift = np.eye(4)
    shift[:3, 3] = start
    crop = np.ascontiguousarray(data[start[0]:stop[0], start[1]:stop[1], start[2]:stop[2]])
    return crop, affine @ shift


def outsidePercent(values, affine, brain, maxVoxels=OUTSIDE_SAMPLE_VOXELS):
    """Percentage of the lesion voxels (centres, nearest voxel) outside a brain mask ``(data[i, j, k], affine)``; None when empty.

    A lesion of more than ``maxVoxels`` voxels is sampled regularly.
    """
    data, brainAffine = brain
    found = np.argwhere(values)
    if len(found) == 0:
        return None
    if len(found) > maxVoxels:
        found = found[::math.ceil(len(found) / maxVoxels)]
    points = np.c_[found, np.ones(len(found))] @ np.asarray(affine, dtype=float).T
    voxels = np.rint(points @ np.linalg.inv(np.asarray(brainAffine, dtype=float)).T)[:, :3].astype(int)
    inside = np.all((voxels >= 0) & (voxels < np.array(data.shape[:3])), axis=1)
    brainVoxel = np.zeros(len(voxels), dtype=bool)
    brainVoxel[inside] = np.asarray(data)[voxels[inside, 0], voxels[inside, 1], voxels[inside, 2]] > 0
    return 100.0 * float(np.count_nonzero(~brainVoxel)) / len(voxels)


def shortLabel(name):
    """"L thalamus" from "Left Thalamus": the side as a letter, the rest in lower case unless it is an acronym."""
    text = str(name or "").strip()
    match = re.match(r"^(Left|Right)\s+(.*)$", text)
    side, rest = (("L" if match.group(1) == "Left" else "R"), match.group(2)) if match else (None, text)
    words = rest.split(" ")
    words = [word if (word.isupper() and len(word) > 1) or any(character.isupper() for character in word[1:]) else word.lower() for word in words]
    rest = " ".join(words)
    return f"{side} {rest}" if side else rest


def locationText(rows, minPct=LOCATION_MIN_PCT, maxRows=LOCATION_MAX_ROWS):
    """"L thalamus 72%, L PLIC 18%" of label rows ``[(label, pct)]`` (largest first), "" when none reaches ``minPct``."""
    kept = [(label, pct) for label, pct in sorted(rows, key=lambda row: -row[1]) if pct >= minPct and label][:maxRows]
    return ", ".join(f"{shortLabel(label)} {pct:.0f}%" for label, pct in kept)


def descPath(path, desc):
    """The file next to a lesion file with another ``desc-`` entity: ``..._desc-lesion_mask.nii.gz`` → ``..._desc-<desc>_mask.nii.gz``."""
    folder, name = os.path.split(os.fspath(path))
    if "desc-lesion_mask" in name:
        return os.path.join(folder, name.replace("desc-lesion_mask", f"desc-{desc}_mask"))
    for extension in (".nii.gz", ".nii"):
        if name.lower().endswith(extension):
            return os.path.join(folder, f"{name[:-len(extension)]}_desc-{desc}{name[-len(extension):]}")
    return os.path.join(folder, f"{name}_desc-{desc}")


def policyText(policy, spacing=None):
    """Short text of a stored policy for the dimensionality chip: "kept as drawn", "2 mm", "3 mm binary"; None when not chosen."""
    if not policy:
        return None
    mode = policy.get("mode") if isinstance(policy, dict) else str(policy)
    total = policy.get("total_mm") if isinstance(policy, dict) else None
    if mode == MODE_SINGLE:
        return _("kept as drawn")
    try:
        total = float(total) if total is not None else DEFAULT_EXTRUDE_MM
    except (TypeError, ValueError):
        total = DEFAULT_EXTRUDE_MM
    if mode == MODE_BINARY:
        step = float(spacing) if spacing else 1.0
        voxels = 2 * _lnm("lesionops")._round_half_down((total / 2.0) / step) + 1
        return _("{mm:g} mm binary").format(mm=voxels * step)
    return _("{mm:g} mm").format(mm=total)


def policySemantics(mode, totalMm=DEFAULT_EXTRUDE_MM, step=1.0):
    """The sentence stored in ``study.json`` ``trace_policy.extrude_2d.semantics`` (and shown in the methods)."""
    if mode == MODE_SINGLE:
        return SEMANTICS[MODE_SINGLE]
    if mode == MODE_BINARY:
        voxels = 2 * _lnm("lesionops")._round_half_down((float(totalMm) / 2.0) / step) + 1
        return SEMANTICS[MODE_BINARY].format(half=float(totalMm) / 2.0, voxels=voxels, step=step)
    return SEMANTICS[MODE_FRACTIONAL].format(total=float(totalMm))


def dimensionalityText(info, policy=None, spacing=None):
    """``(text, level)`` of the dimensionality chip: "2D · axial · 1 slice → 2 mm", None for an empty lesion.

    ``level`` is "warn" for a 2D tracing whose study has no policy yet, else "neutral".
    """
    if not info or not info.get("dimensionality"):
        return None
    dim = info["dimensionality"]
    planes = [plane for plane in info.get("planes") or [] if plane]
    if dim == "3D":
        return _("3D"), "neutral"
    if dim == "2D":
        plane = planes[0] if planes else ""
        count = info.get("n_slices_normal") if plane == "oblique" else (info.get("n_slices") or {}).get(plane, 1)
        slices = _("1 slice") if (count or 1) == 1 else _("{count} slices").format(count=count)
        head = f"2D · {plane} · {slices}" if plane else f"2D · {slices}"
    else:
        head = _("Several single slices") + (f" · {' + '.join(planes)}" if planes else "")
    chosen = policyText(policy, spacing)
    if chosen is None:
        return head + " → " + _("choose how to use it"), "warn"
    return head + " → " + chosen, "neutral"


def reportedSideWord(side):
    return {"L": _("left"), "R": _("right"), "bilateral": _("both sides"), "midline": _("midline")}.get(side or "", "")


def tracedSideWord(side):
    return {"L": _("left"), "R": _("right"), "bilateral": _("both hemispheres"), "midline": _("the midline")}.get(side or "", "")


class Resources:
    """Cached atlases, priors and brain masks of the chips (arrays only; loaded in a worker thread, see ``QcCore``).

    ``state`` is "unloaded", "loading" or "ready"; ``missingAtlases`` is True when a Harvard-Oxford atlas is not in the cache.
    ``atlases`` and the dictionaries are replaced as a whole by the main thread when the worker's result is drained.
    """

    def __init__(self):
        self.state = "unloaded"
        self.atlases = []
        self.csf = {}  # family -> (array[i, j, k], affine, family)
        self.ventricles = {}  # family -> (array, affine, family, labels)
        self.brain = {}  # family -> (array, affine)
        self.missingAtlases = False
        self.version = 0
        self._crops = {}

    def atlasesFor(self, family):
        """``(atlases, crossFamily)``: the cached atlases of the lesion's family, else all of them (the family differs)."""
        same = [atlas for atlas in self.atlases if atlas.family == family]
        if same:
            return same, False
        return list(self.atlases), bool(self.atlases)

    def cropped(self, key, data, affine, low, high):
        """Crop of a cached array around a box, cached by the key and the box (the lesion rarely moves between edits)."""
        box = (key, tuple(np.round(low, 1)), tuple(np.round(high, 1)))
        entry = self._crops.get(box)
        if entry is None:
            entry = cropVolume(data, affine, low, high) or (None, None)
            if len(self._crops) > 48:
                self._crops.pop(next(iter(self._crops)))
            self._crops[box] = entry
        return entry

    def croppedAtlas(self, atlas, low, high):
        key = ("atlas", atlas.id)
        box = (key, tuple(np.round(low, 1)), tuple(np.round(high, 1)))
        cached = self._crops.get(box)
        if cached is None:
            crop, affine = cropVolume(atlas.data, atlas.affine, low, high) or (None, None)
            atlasesModule = _lnm("atlases")
            cached = None if crop is None else atlasesModule.Atlas(atlas.id, crop, affine, atlas.labels, atlas.family, atlas.space, atlas.title, atlas.licence, atlas.badge, atlas.networks)
            if len(self._crops) > 48:
                self._crops.pop(next(iter(self._crops)))
            self._crops[box] = (cached, None)
            return cached
        return cached[0]

    def reset(self):
        self._crops = {}


def _drawnSpacing(ops, info, affine, planeNormal=None):
    """Voxel size (mm) along the normal of the drawn plane of a 2D lesion, None otherwise."""
    if not info or info.get("dimensionality") != "2D":
        return None
    try:
        normal = np.asarray(planeNormal, dtype=float) if planeNormal is not None else ops._plane_normal(info["planes"][0])
        return float(ops._layer_step(affine, normal / np.linalg.norm(normal)))
    except Exception:
        return None


_PARTS_STRUCTURE = None


def _partsStructure():
    global _PARTS_STRUCTURE
    if _PARTS_STRUCTURE is None:
        _PARTS_STRUCTURE = np.ones((3, 3, 3), dtype=bool)
    return _PARTS_STRUCTURE


def hygieneOfMask(values, affine, parts, multifocal=False):
    """``lesionops.hygiene`` for a binary lesion crop whose 26-connected parts are already counted (the checks of a mask in the scene).

    The same fields as ``lesionops.hygiene``; the header, grid and connectome checks need a file (see the file check of the worker).
    """
    voxels = int(np.count_nonzero(values))
    voxelMl = abs(float(np.linalg.det(np.asarray(affine, dtype=float)[:3, :3]))) / 1000.0
    found = {"nonbinary": False, "n_nonbinary": 0, "value_range": [0.0, 1.0] if voxels else None, "empty": voxels == 0, "volume_ml": voxels * voxelMl, "n_parts": int(parts),
             "multipart": parts > 1 and not multifocal, "connectome_sum": None, "subvoxel": False, "outside_pct": None, "outside": False, "xform_mm": None, "xform": False,
             "grid_matches": None, "grid": False, "left_handed": bool(np.linalg.det(np.asarray(affine, dtype=float)[:3, :3]) < 0), "messages": []}
    if voxels == 0:
        found["messages"].append({"code": "empty", "level": "amber", "text": "The lesion is empty"})
    elif found["multipart"]:
        found["messages"].append({"code": "multipart", "level": "amber", "text": f"The lesion has {parts} separate parts (26-connected) but is not marked multifocal"})
    return found


def dimensionalityOfMask(ops, values, affine, parts, planeNormal=None):
    """``lesionops.detect_dimensionality`` with a shortcut for the usual 3D lesion: one 26-connected part that spans more than one
    voxel along every axis of a grid aligned with the anatomical axes cannot be single-slice (``values`` is the tight crop).
    """
    if planeNormal is None and parts == 1 and all(extent > 1 for extent in values.shape):
        if all(ops._aligned_voxel_axis(affine, ops._plane_normal(plane)) is not None for plane in ops.PLANES):
            return {"dimensionality": "3D", "planes": [], "n_slices": {}, "drawn_axis": None, "n_components": 1}
    return ops.detect_dimensionality(values, affine, planeNormal)


def _flagsOfResult(ops, result):
    results = {"hygiene": result["hygiene"]}
    if result.get("side") is not None:
        results["side"] = result["side"]
    if result.get("csf") is not None:
        results["csf"] = result["csf"]
    return ops.qc_flags(results)


def evaluateCore(values, affine, mni=False, space=None, reportedSide=None, multifocal=False, resources=None, planeNormal=None):
    """The first stage of the checks on a tight lesion crop (see ``lesionCrop``): hygiene, dimensionality, side and the share outside the brain.

    Numpy only. ``evaluateExtra`` adds the CSF share and the atlas labels. Returns ``{voxels, hygiene, outsidePct, dimensionality, spacing,
    side, csf, location, crossFamily, family, flags, ms}``; ``side`` is None in scanner space.
    """
    from scipy import ndimage

    ops = _lnm("lesionops")
    geometry = _lnm("geometry")
    result = {"voxels": int(np.count_nonzero(values)), "hygiene": None, "outsidePct": None, "dimensionality": None, "spacing": None, "side": None, "csf": None,
              "location": None, "crossFamily": False, "family": None, "flags": [], "ms": {}}
    timings = result["ms"]

    def lap(name, start):
        timings[name] = (time.perf_counter() - start) * 1000.0

    start = time.perf_counter()
    _labels, parts = ndimage.label(values, structure=_partsStructure())
    result["hygiene"] = hygieneOfMask(values, affine, parts, multifocal)
    lap("hygiene", start)
    start = time.perf_counter()
    result["dimensionality"] = dimensionalityOfMask(ops, values, affine, parts, planeNormal)
    result["spacing"] = _drawnSpacing(ops, result["dimensionality"], affine, planeNormal)
    lap("dimensionality", start)
    family = geometry.space_family(space) if mni and space else None
    result["family"] = family
    if mni:
        start = time.perf_counter()
        result["side"] = ops.side_check(values, affine, reportedSide)
        lap("side", start)
        start = time.perf_counter()
        brain = resources.brain.get(family) if resources is not None and family else None
        if brain is not None:
            low, high = worldBox(values.shape, affine)
            crop = resources.cropped(("brain", family), brain[0], brain[1], low, high)
            result["outsidePct"] = outsidePercent(values, affine, (crop[0], crop[1])) if crop[0] is not None else 100.0
            result["hygiene"]["outside_pct"] = result["outsidePct"]
            result["hygiene"]["outside"] = result["outsidePct"] > ops.OUTSIDE_PCT_MAX
            if result["hygiene"]["outside"]:
                result["hygiene"]["messages"].append({"code": "outside", "level": "amber", "text": f"{result['outsidePct']:.1f}% of the lesion is outside the brain"})
        lap("outside", start)
    result["flags"] = _flagsOfResult(ops, result)
    return result


def evaluateExtra(result, values, affine, space=None, resources=None):
    """The second stage: the CSF share against the cached prior and the atlas labels (MNI lesions). Adds to ``result`` and returns it."""
    if result.get("family") is None or resources is None:
        return result
    ops = _lnm("lesionops")
    family = result["family"]
    timings = result["ms"]
    low, high = worldBox(values.shape, affine)
    start = time.perf_counter()
    prior = resources.csf.get(family) if family in ("2009", "fsl6") else None
    labels = resources.ventricles.get(family) if family in ("2009", "fsl6") else None
    try:
        if prior is not None:
            crop = resources.cropped(("csf", family), prior[0], prior[1], low, high)
            if crop[0] is not None:
                result["csf"] = ops.csf_share(values, affine, csf=(crop[0], crop[1], family), lesion_space=family)
        elif labels is not None:
            crop = resources.cropped(("ventricles", family), labels[0], labels[1], low, high)
            if crop[0] is not None:
                result["csf"] = ops.csf_share(values, affine, ventricle_labels=(crop[0], crop[1], family, labels[3]), lesion_space=family)
    except Exception:
        logging.warning(f"Studio: the CSF share could not be computed:\n{traceback.format_exc()}")
    timings["csf"] = (time.perf_counter() - start) * 1000.0
    start = time.perf_counter()
    if resources.atlases:
        atlasList, cross = resources.atlasesFor(family)
        cropped = [resources.croppedAtlas(atlas, low, high) for atlas in atlasList]
        cropped = [atlas for atlas in cropped if atlas is not None]
        if cropped:
            try:
                label = _lnm("atlases").label_mask(values, affine, cropped, allow_cross_family=cross, space=space)
                result["location"] = [(row.label, row.pct_of_mask) for row in label if row.label]
                result["crossFamily"] = cross
            except Exception:
                logging.warning(f"Studio: the atlas labels could not be computed:\n{traceback.format_exc()}")
    timings["location"] = (time.perf_counter() - start) * 1000.0
    result["flags"] = _flagsOfResult(ops, result)
    return result


def evaluate(values, affine, mni=False, space=None, reportedSide=None, multifocal=False, resources=None, planeNormal=None, tight=False):
    """Both stages of the checks on a lesion crop (``evaluateCore`` then ``evaluateExtra``). Numpy only.

    :param values: bool array [i, j, k] of the lesion; ``tight``: it is the tight crop of ``lesionCrop`` (else it is cropped here).
    :param mni: the lesion is on an MNI grid (hemisphere words and atlas labels are only given there).
    :param space: the space name of the grid ("MNI152NLin6Asym").
    :return: the result of ``evaluateCore`` with ``csf`` and ``location`` (None without a cached prior or atlas); ``flags`` are
      ``lesionops.qc_flags`` codes of these checks (not the duplicate flag).
    """
    if not tight:
        found = lesionCrop(np.ascontiguousarray(np.asarray(values).transpose(2, 1, 0)), affine)
        if found is None:
            return {"voxels": 0, "hygiene": hygieneOfMask(np.zeros((1, 1, 1), bool), affine, 0, multifocal), "outsidePct": None, "dimensionality": {"dimensionality": None, "planes": [], "n_slices": {}},
                    "spacing": None, "side": None, "csf": None, "location": None, "crossFamily": False, "family": None, "flags": ["empty"], "ms": {}}
        values, affine = found[0], found[1]
    result = evaluateCore(values, affine, mni, space, reportedSide, multifocal, resources, planeNormal)
    return evaluateExtra(result, values, affine, space, resources) if mni else result


#
# Chips (pure: specs of what to show)
#


def buildChips(result, row=None, mni=False, duplicates=(), policy=None, studyOpen=False, resourcesState="ready", missingAtlases=False,
               atlasJob=None, fileHygiene=None):
    """The chips for a ``evaluate()`` result as a list of specs ``{key, level, badge, text, tooltip, actions: [{id, text, tooltip}]}``.

    :param row: the participants row of the subject (``reported_side``, ``reported_location``, ``multifocal``), or None.
    :param duplicates: ``[(sid, dice)]`` of the possible duplicates of the subject (not dismissed).
    :param policy: ``trace_policy.extrude_2d`` of the study.
    :param atlasJob: None, "running" or a failure text of the last "Get atlases" job.
    :param fileHygiene: ``lesionops.hygiene`` result of the lesion file on disk (not binary, header, grid), or None.
    """
    ops = _lnm("lesionops")
    row = row or {}
    chips = []
    if result is None:
        return chips
    reported = ops.normalize_side(None if _missing(row.get("reported_side")) else row.get("reported_side"))
    reportedLocation = None if _missing(row.get("reported_location")) else str(row["reported_location"]).strip()
    # Side against the report: a verdict only for laterality, and only where left and right are known (MNI)
    if not mni:
        text = _("Side unknown in scanner space")
        if reported:
            text += " · " + _("the report says {side}").format(side=reportedSideWord(reported))
        chips.append({"key": "side", "level": "neutral", "badge": _("Side"), "text": text,
                      "tooltip": _("Left and right are only known on an MNI template: x = 0 is not the midline in scanner space"), "actions": []})
    elif result.get("side") is not None and reported:
        side = result["side"]
        traced = tracedSideWord(side["traced_side"])
        if side["mismatch"]:
            chips.append({"key": "side", "level": "warn", "badge": _("Side"),
                          "text": _("Traced {traced} · the report says {reported}. Radiological figure?").format(traced=traced, reported=reportedSideWord(reported)),
                          "tooltip": _("The lesion is in the {traced} hemisphere of the template but the source says {reported}").format(traced=traced, reported=reportedSideWord(reported)),
                          "actions": [{"id": "mirror", "text": _("Mirror L↔R"),
                                       "tooltip": _("Mirror the lesion left-right (world x → -x). Approximate: templates are asymmetric. For a figure whose left and right were mixed up, retracing on the correct side is better. The original is kept; Undo restores it.")}]})
        elif side["level"] == "info":
            chips.append({"key": "side", "level": "neutral", "badge": _("Side"), "text": str(side["message"]), "tooltip": "", "actions": []})
        else:
            chips.append({"key": "side", "level": "ok", "badge": _("Side"),
                          "text": _("Traced {traced} · matches the report").format(traced=traced), "tooltip": "", "actions": []})
    # Location against the paper's
    locationRows = result.get("location")
    parts = []
    if mni and locationRows:
        text = locationText(locationRows)
        if text:
            parts.append(_("Traced: {text}").format(text=text) + (" " + _("(approx.: the atlas is in another template family)") if result.get("crossFamily") else ""))
    if reportedLocation:
        parts.append(_("Paper: {text}").format(text=reportedLocation))
    if parts:
        chips.append({"key": "location", "level": "neutral", "badge": _("Where"), "text": " | ".join(parts),
                      "tooltip": _("Where the lesion is, from the Harvard-Oxford atlases, next to what the paper says. Only the side gives a verdict."), "actions": []})
    elif mni and missingAtlases and resourcesState != "loading" and result.get("voxels"):
        running = atlasJob == "running"
        failed = atlasJob if atlasJob not in (None, "running") else None
        text = _("Getting atlases…") if running else (_("Atlases could not be downloaded: {reason}").format(reason=failed) if failed else _("The location of the lesion needs atlases"))
        chips.append({"key": "atlases", "level": "busy" if running else ("warn" if failed else "neutral"), "badge": _("Where"), "text": text,
                      "tooltip": _("Harvard-Oxford cortical and subcortical atlases (CC BY-SA 4.0) and a CSF prior, a few MB from TemplateFlow, downloaded only when you click"),
                      "actions": [] if running else [{"id": "getAtlases", "text": _("Get atlases (HOCPAL/HOSPA, CC BY-SA 4.0)"),
                                                      "tooltip": _("Download the Harvard-Oxford atlases (CC BY-SA 4.0) into the cache; nothing else is downloaded")}]})
    # CSF
    csf = result.get("csf")
    if csf is not None and getattr(csf, "flagged", False):
        chips.append({"key": "csf", "level": "warn", "badge": "CSF",
                      "text": _("{pct:.0f}% of the lesion is in CSF or the ventricles").format(pct=csf.pct),
                      "tooltip": _("Tracings that include sulci or ventricles map CSF, not tissue. Check the outline."), "actions": []})
    # Hygiene
    hygiene = result.get("hygiene") or {}
    messages = []
    actions = []
    if hygiene.get("multipart"):
        messages.append(_("{count} separate parts but not marked multifocal").format(count=hygiene.get("n_parts")))
    if hygiene.get("outside"):
        messages.append(_("{pct:.0f}% outside the brain").format(pct=hygiene.get("outside_pct") or 0.0))
        actions.append({"id": "clip", "text": _("Clip outside brain"), "tooltip": _("Remove the voxels outside the brain mask of the template (one undo step; the original is kept)")})
    filed = fileHygiene or {}
    if filed.get("nonbinary"):
        messages.append(_("the file has values other than 0 and 1"))
        actions.append({"id": "binarize", "text": _("Binarize at 0.5"), "tooltip": _("Replace the lesion by the voxels of the saved file with a value of at least 0.5 (one undo step; the original file is kept)")})
    if filed.get("xform"):
        messages.append(_("the file's sform and qform disagree by {mm:.2f} mm").format(mm=filed.get("xform_mm") or 0.0))
    if filed.get("grid"):
        messages.append(_("the file is not on the study template grid"))
    if messages:
        chips.append({"key": "hygiene", "level": "warn", "badge": _("Mask"), "text": "; ".join(messages).capitalize(), "tooltip": _("Checks of the lesion mask"), "actions": actions})
    # Duplicates
    if duplicates:
        other, dice = duplicates[0]
        more = len(duplicates) - 1
        text = _("May be the same case as {sid} (Dice {dice:.2f})").format(sid=other, dice=dice) + (_(" and {count} more").format(count=more) if more else "")
        chips.append({"key": "duplicate", "level": "warn", "badge": _("Same?"), "text": text,
                      "tooltip": _("The overlap with another lesion of this study is above Dice 0.6: the same case may be reported twice"),
                      "actions": [{"id": "markDuplicate", "text": _("Mark duplicate"), "tooltip": _("Exclude this lesion from analyses and record that it duplicates {sid}").format(sid=other)},
                                  {"id": "notDuplicate", "text": _("Not a duplicate"), "tooltip": _("Dismiss: the pair is not flagged again (recorded)")}]})
    # Dimensionality
    shown = dimensionalityText(result.get("dimensionality"), policy, result.get("spacing"))
    if shown is not None and result["dimensionality"]["dimensionality"] != "3D":
        text, level = shown
        chips.append({"key": "dimensionality", "level": level, "badge": _("Slices"), "text": text,
                      "tooltip": _("Single-slice tracings are used as drawn or extruded to a thickness, once for the whole study. The tracing as drawn is always kept."),
                      "actions": [{"id": "policy", "text": _("Change…") if policy else _("Choose…"), "tooltip": _("Choose how 2D tracings are used in this study")}] if studyOpen else []})
    return chips


#
# Worker thread and core (the part that outlives a card)
#


class _Worker:
    """One background thread that runs the jobs in order. Numpy and files only (D19): no MRML, VTK or Qt object is used here.

    The results are collected by the main thread with ``drain()``. ``pending`` counts the jobs not yet drained.
    """

    def __init__(self):
        self._jobs = queue.Queue()
        self._results = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self.pending = 0
        self._indexes = {}  # study path -> LesionIndex (used by the thread only)

    def submit(self, kind, **payload):
        with self._lock:
            self.pending += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="StudioLesionQcWorker", daemon=True)
                self._thread.start()
        self._jobs.put((kind, payload))

    def _run(self):
        while True:
            job = self._jobs.get()
            if job is None:
                return
            kind, payload = job
            try:
                result = getattr(self, "_do_" + kind)(**payload)
                self._results.put((kind, payload.get("tag"), result, None))
            except Exception:
                self._results.put((kind, payload.get("tag"), None, traceback.format_exc()))

    def drain(self):
        found = []
        while True:
            try:
                found.append(self._results.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            self.pending -= len(found)
        return found

    def stop(self):
        self._jobs.put(None)

    # Jobs

    def _index(self, studyPath):
        ops = _lnm("lesionops")
        index = self._indexes.get(studyPath)
        if index is None:
            index = ops.LesionIndex.load(ops.index_path(studyPath))
            self._indexes[studyPath] = index
        return index

    def _do_resources(self, cache, brainPath=None, tag=None):
        fetch = _lnm("fetch")
        atlases = _lnm("atlases")
        nifti = _lnm("nifti")
        loaded = []
        for atlasId in ATLAS_IDS:
            try:
                loaded.append(atlases.Atlas.load(atlasId, cache))
            except fetch.FetchError:
                continue

        def volume(resourceId, catalog):
            try:
                resource = fetch.find_resource(resourceId, (catalog,))
                path = fetch.cached_path(resource, cache)
                if path is None:
                    return None, None
                data, affine, _header = nifti.read_nifti(path)
                return (data[..., 0] if data.ndim > 3 else data), (resource, affine)
            except (fetch.FetchError, OSError, ValueError):
                return None, None

        csf, ventricles, brain = {}, {}, {}
        for family, resourceId in CSF_PRIOR_IDS.items():
            data, extra = volume(resourceId, "templates")
            if data is not None:
                csf[family] = (np.asarray(data, dtype=np.float32), extra[1], family)
        for family, resourceId in VENTRICLE_IDS.items():
            data, extra = volume(resourceId, "templates")
            if data is not None:
                labelsOfInterest = extra[0].get("labels_of_interest") or {}
                ventricles[family] = (np.asarray(data), extra[1], family, sorted({int(value) for value in labelsOfInterest.values()}) or None)
        for family, resourceId in BRAIN_MASK_IDS.items():
            data, extra = volume(resourceId, "templates")
            if data is not None:
                brain[family] = (np.asarray(data) > 0, extra[1])
        if brainPath:
            try:
                data, affine, _header = nifti.read_nifti(brainPath)
                brain["2009"] = ((data[..., 0] if data.ndim > 3 else data) > 0, affine)
            except (OSError, ValueError):
                pass
        return {"atlases": loaded, "csf": csf, "ventricles": ventricles, "brain": brain, "missingAtlases": len(loaded) < len(ATLAS_IDS)}

    def _do_scan(self, studyPath, entries, dismissed, tag=None):
        ops = _lnm("lesionops")
        nifti = _lnm("nifti")
        index = self._index(studyPath)
        wanted = {sid for sid, _path in entries}
        changed = False
        for sid in index.sids():
            if sid not in wanted:
                index.remove(sid)
                changed = True
        for sid, path in entries:
            try:
                key = ops.file_key(path)
            except OSError:
                if sid in index:
                    index.remove(sid)
                    changed = True
                continue
            if index.is_current(sid, path):
                continue
            try:
                data, affine, _header = nifti.read_nifti(path)
                index.update(sid, data[..., 0] if data.ndim > 3 else data, affine, key=key)
                changed = True
            except (OSError, ValueError):
                logging.warning(f"Studio: the lesion of {sid} could not be indexed for duplicates")
        if changed:
            index.save(ops.index_path(studyPath))
        pairs = [(a, b, dice) for a, b, dice in index.all_pairs() if frozenset((a, b)) not in dismissed]
        return {"studyPath": studyPath, "pairs": pairs, "indexed": len(index)}

    def _do_current(self, studyPath, sid, values, affine, dismissed, tag=None):
        index = self._index(studyPath)
        index.update(sid, values, affine, key=None)
        candidates = [(other, dice) for other, dice in index.candidates(sid) if frozenset((sid, other)) not in dismissed]
        return {"studyPath": studyPath, "sid": sid, "candidates": candidates}

    def _do_filehygiene(self, sid, path, templateGrid, multifocal, key, tag=None):
        ops = _lnm("lesionops")
        nifti = _lnm("nifti")
        data, affine, header = nifti.read_nifti(path)
        found = ops.hygiene(data[..., 0] if data.ndim > 3 else data, affine, header=header, template_grid=templateGrid, multifocal=multifocal)
        return {"sid": sid, "key": key, "hygiene": found}


def templateGridId(study):
    """Registry id of the study template's grid ("TF2009-2mm", "NLin6-1mm"), None when it is not a registry grid."""
    geometry = _lnm("geometry")
    template = study.template if isinstance(study.template, dict) else {}
    name = template.get("id") or study.space
    if not name:
        return None
    resolution = template.get("res") or 1
    family = geometry.space_family(name)
    grid = {"fsl6": f"NLin6-{resolution:g}mm", "2009": f"TF2009-{resolution:g}mm"}.get(family) if isinstance(resolution, (int, float)) else None
    return grid if grid in geometry.GRID_BY_ID else None


def storedPolicy(study):
    """``trace_policy.extrude_2d`` of a study as ``{mode, total_mm, semantics}`` with a canonical mode, None when not chosen."""
    value = (study.trace_policy or {}).get("extrude_2d")
    if isinstance(value, str):
        value = {"mode": value}
    if not isinstance(value, dict) or not value.get("mode"):
        return None
    mode = str(value["mode"]).lower()
    if mode.startswith("frac"):
        mode = MODE_FRACTIONAL
    elif mode.startswith("bin"):
        mode = MODE_BINARY
    elif mode.startswith(("single", "keep", "none")):
        mode = MODE_SINGLE
    else:
        return None
    total = value.get("total_mm")
    try:
        total = float(total) if total is not None else (None if mode == MODE_SINGLE else DEFAULT_EXTRUDE_MM)
    except (TypeError, ValueError):
        total = DEFAULT_EXTRUDE_MM
    return {"mode": mode, "total_mm": total, "semantics": value.get("semantics")}


def setPolicy(study, mode, totalMm=DEFAULT_EXTRUDE_MM):
    """Store the 2D policy in the study (``trace_policy.extrude_2d``; the other keys of the policy are kept)."""
    stored = dict(study.metadata.get("trace_policy") or {})
    resolution = (study.trace_policy or {}).get("res") or 1
    step = float(resolution) if isinstance(resolution, (int, float)) else 1.0
    stored["extrude_2d"] = {"mode": mode, "total_mm": None if mode == MODE_SINGLE else float(totalMm), "semantics": policySemantics(mode, totalMm, step)}
    study.trace_policy = stored
    return stored["extrude_2d"]


def _readAudit(study):
    """The records of ``<study>/.studio/audit.jsonl`` (damaged lines are skipped)."""
    path = os.path.join(study.path, _lnm("study").PRIVATE_DIR, _lnm("study").AUDIT_JSONL)
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    if isinstance(record, dict):
                        records.append(record)
    except OSError:
        pass
    return records


def dismissedPairs(study):
    """Pairs ``{frozenset({a, b})}`` that the user said are not duplicates (``lesion_not_duplicate`` records of the audit log)."""
    return {frozenset((record["sid"], record["other"])) for record in _readAudit(study) if record.get("event") == "lesion_not_duplicate" and record.get("sid") and record.get("other")}


def flagsOf(row):
    """The qc_flags codes of a row (a list; "n/a" and "none" are no flags)."""
    value = (row or {}).get("qc_flags")
    if _missing(value) or str(value).strip().lower() == "none":
        return []
    return [code for code in re.split(r"[;,]", str(value)) if code]


def flagsValue(codes):
    """The qc_flags cell for a list of codes, in the order of ``lesionops.QC_FLAGS``; None (n/a) for none."""
    order = list(_lnm("lesionops").QC_FLAGS)
    ordered = [code for code in order if code in set(codes)] + [code for code in codes if code not in order]
    return ";".join(dict.fromkeys(ordered)) or None


def _stat(path):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _appendAudit(study, record):
    """Append one record to ``<study>/.studio/audit.jsonl`` (one write call; paths in a record are relative to the study)."""
    studyModule = _lnm("study")
    path = os.path.join(study.path, studyModule.PRIVATE_DIR, studyModule.AUDIT_JSONL)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _commit(study, session, sid, values, audit):
    """Set cells of a row and append one audit record, on the main thread.

    The open session sets and saves the cells itself (``StudySession.setValues`` and ``flush``: the single writer). The inbox is
    not used: ``applyInbox()`` would also merge the results that jobs posted, which other cards hold back until the subject's
    edits are written (the normalization manager).
    """
    record = {"at": _now(), "by": "lesionqc", **audit}
    record.setdefault("sid", sid)
    if values:
        if session is not None:
            session.setValues([sid], values)
            session.flush(lockTimeout=0.5)
        else:
            study.set(sid, values)
            study.save()
    _appendAudit(study, record)


def _writeAtomic(path, writer):
    """Write a file through a temporary name and replace the target (``writer(temporaryPath)``)."""
    extension = ".nii.gz" if path.lower().endswith(".nii.gz") else os.path.splitext(path)[1]
    temporary = f"{path[:-len(extension)] if extension else path}.{os.getpid()}.tmp{extension}"
    try:
        writer(temporary)
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)


class QcCore:
    """What the chips share with the study: the worker, the cached resources, the duplicate results, the audit and the post-write work.

    One per application (``core()``). Views (``LesionQc``) register a callable with ``addListener()`` and are called when
    something they show changed (resources loaded, duplicates found, a job finished).
    """

    def __init__(self):
        self.worker = _Worker()
        self.resources = Resources()
        self.studyPath = None
        self.dismissed = set()
        self.pairs = []
        self.current = {}  # sid -> [(other, dice)] of the lesion in the scene
        self.fileHygiene = {}  # sid -> (file key, hygiene result)
        self.lastSeen = {}  # sid -> stat of the lesion file when this module last looked
        self.pendingDone = {}  # sid -> {"time", "normal", "dimensionality"}
        self.atlasJob = None  # None, "running" or the reason of the last failure
        self._atlasProcess = None
        self._atlasQueue = []
        self._atlasError = None
        self._atlasFailed = False
        self.policyCard = None
        self.listeners = []
        self.brainMaskPath = None
        self.scanCount = 0
        self.lastScanSeconds = None
        self._scanStarted = None
        self._timer = qt.QTimer()
        self._timer.setInterval(DRAIN_MS)
        self._timer.connect("timeout()", self._drain)
        self._scanTimer = qt.QTimer()
        self._scanTimer.setSingleShot(True)
        self._scanTimer.setInterval(SCAN_DELAY_MS)
        self._scanTimer.connect("timeout()", self.requestScan)
        with contextlib.suppress(Exception):
            slicer.app.connect("aboutToQuit()", self.dispose)

    def dispose(self):
        self._timer.stop()
        self._scanTimer.stop()
        self.worker.stop()

    def addListener(self, callback):
        if callback not in self.listeners:
            self.listeners.append(callback)

    def removeListener(self, callback):
        while callback in self.listeners:
            self.listeners.remove(callback)

    def _changed(self):
        for callback in list(self.listeners):
            try:
                callback()
            except Exception:
                logging.error(f"Studio: a lesion QC listener failed:\n{traceback.format_exc()}")

    def _submit(self, kind, **payload):
        self.worker.submit(kind, **payload)
        if not self._timer.isActive():
            self._timer.start()

    def idle(self):
        return self.worker.pending == 0 and not self._scanTimer.isActive()

    def waitIdle(self, timeoutS=20.0):
        """Process events until the worker has no job left and its results are drained (tests). Return True when idle."""
        end = time.time() + timeoutS
        while time.time() < end:
            slicer.app.processEvents()
            self._drain()
            if self.idle():
                return True
            time.sleep(0.01)
        return self.idle()

    # Resources

    def cacheDir(self):
        fetch = _lnm("fetch")
        explicit = os.environ.get(fetch.CACHE_ENV)
        if explicit:
            return fetch.cache_dir(explicit)
        cachePath = getattr(slicer.app, "cachePath", "") if slicer.app is not None else ""
        return fetch.cache_dir(os.path.join(cachePath, "LNM") if cachePath else None)

    def ensureResources(self, reload=False):
        """Load the cached atlases, priors and brain masks in the worker (once; ``reload`` after a download)."""
        if self.resources.state == "loading" or (self.resources.state == "ready" and not reload):
            return
        self.resources.state = "loading"
        if self.brainMaskPath is None:
            try:
                self.brainMaskPath = _studyUi().brainMaskFile() or ""
            except Exception:
                self.brainMaskPath = ""
        self._submit("resources", cache=self.cacheDir(), brainPath=self.brainMaskPath or None)

    # Study

    def studyOpened(self, session):
        path = session.study.path
        if path == self.studyPath:
            return
        if self.policyCard is not None and self.policyCard.session is not session:
            self.hidePolicyCard()
        self.studyPath = path
        self.pairs = []
        self.current = {}
        self.fileHygiene = {}
        self.lastSeen = {}
        self.pendingDone = {}
        self.dismissed = dismissedPairs(session.study)
        self.scheduleScan(OPEN_SCAN_DELAY_MS)  # not while the study page is being built: the worker shares the interpreter lock

    def studyClosed(self):
        self.hidePolicyCard()  # its choice belongs to the study that was closed
        if self.studyPath is None:
            return
        self.studyPath = None
        self.pairs = []
        self.current = {}
        self.fileHygiene = {}
        self.lastSeen = {}
        self.pendingDone = {}
        self._changed()

    def scheduleScan(self, delayMs=SCAN_DELAY_MS):
        self._scanTimer.setInterval(int(delayMs))
        self._scanTimer.start()

    def requestScan(self):
        session = _session()
        if session is None:
            return
        study = session.study
        # Only lesions on a template grid: in scanner space the grids of two patients say nothing about where the lesions are
        entries = [(sid, path) for sid, path in study.lesion_paths(include_only=False, existing_only=True, with_ids=True)
                   if sid in study and _rowSpaceFamily(study, study.subject(sid)) is not None]
        self._scanStarted = time.perf_counter()
        self._submit("scan", studyPath=study.path, entries=entries, dismissed=frozenset(self.dismissed))

    def requestCurrent(self, session, sid, values, affine):
        if sid not in session.study or _rowSpaceFamily(session.study, session.study.subject(sid)) is None:
            return
        self._submit("current", studyPath=session.study.path, sid=sid, values=np.array(values, dtype=np.uint8), affine=np.array(affine), dismissed=frozenset(self.dismissed))

    def requestFileHygiene(self, session, sid):
        row = session.study.subject(sid) if sid in session.study else None
        if not row or _missing(row.get("lesion")):
            return
        path = session.study.abspath(row["lesion"])
        key = _stat(path)
        if key is None or self.fileHygiene.get(sid, (None, None))[0] == key:
            return
        self._submit("filehygiene", sid=sid, path=path, templateGrid=templateGridId(session.study) if row.get("space") not in ("native",) else None,
                     multifocal=str(row.get("multifocal")) == "1", key=key)

    def duplicatesOf(self, sid):
        """``[(other sid, dice)]`` of the possible duplicates of the subject (scan and scene results, not dismissed, not resolved)."""
        found = {}
        for a, b, dice in self.pairs:
            if sid in (a, b):
                found[b if a == sid else a] = dice
        for other, dice in self.current.get(sid, []):
            found[other] = max(found.get(other, 0.0), dice)
        session = _session()
        out = []
        for other, dice in found.items():
            if frozenset((sid, other)) in self.dismissed:
                continue
            if session is not None and not _comparable(session.study, sid, other):
                continue
            if session is not None and other in session.study and sid in session.study:
                rows = (session.study.subject(sid), session.study.subject(other))
                if rows[0].get("duplicate_of") == other or rows[1].get("duplicate_of") == sid:
                    continue
            out.append((other, dice))
        return sorted(out, key=lambda item: (-item[1], item[0]))

    def _drain(self):
        results = self.worker.drain()
        changed = False
        for kind, _tag, result, error in results:
            if error:
                logging.error(f"Studio: the lesion QC job {kind} failed:\n{error}")
                if kind == "resources":
                    self.resources.state = "ready"
                continue
            if kind == "resources":
                self.resources.atlases = result["atlases"]
                self.resources.csf = result["csf"]
                self.resources.ventricles = result["ventricles"]
                self.resources.brain = result["brain"]
                self.resources.missingAtlases = result["missingAtlases"]
                self.resources.state = "ready"
                self.resources.version += 1
                self.resources.reset()
                changed = True
            elif kind == "scan":
                if result["studyPath"] == self.studyPath:
                    self.pairs = result["pairs"]
                    self.scanCount += 1
                    if self._scanStarted is not None:
                        self.lastScanSeconds = time.perf_counter() - self._scanStarted
                    self._applyDuplicateFlags()
                    changed = True
            elif kind == "current":
                if result["studyPath"] == self.studyPath:
                    self.current[result["sid"]] = result["candidates"]
                    changed = True
            elif kind == "filehygiene":
                self.fileHygiene[result["sid"]] = (result["key"], result["hygiene"])
                changed = True
        if self.worker.pending <= 0:
            self._timer.stop()
        if changed:
            self._changed()

    def _applyDuplicateFlags(self):
        """The ``duplicate`` flag of the rows follows the pairs of the scan (main thread, through the session)."""
        session = _session()
        if session is None or session.study.path != self.studyPath:
            return
        study = session.study
        flagged = set()
        for a, b, _dice in self.pairs:
            if not _comparable(study, a, b):
                continue
            if study.subject(a).get("duplicate_of") == b or study.subject(b).get("duplicate_of") == a:
                continue
            flagged.update((a, b))
        changes = {}
        for row in list(study.subjects):
            sid = row["participant_id"]
            flags = flagsOf(row)
            if ("duplicate" in flags) == (sid in flagged):
                continue
            flags = [code for code in flags if code != "duplicate"] + (["duplicate"] if sid in flagged else [])
            changes[sid] = flagsValue(flags)
        if len(changes) == 1:
            sid, value = next(iter(changes.items()))
            session.setValues([sid], {"qc_flags": value})
        elif changes:
            for sid, value in changes.items():
                study.set(sid, {"qc_flags": value})
            session.scheduleSave()
            with contextlib.suppress(Exception):
                _studyUi()._notify(session)  # one notice for all the rows (the list is rebuilt once)

    def syncFlags(self, session, sid, codes):
        """Write the ``qc_flags`` of a subject: the checks' codes plus the duplicate flag of the pairs (through the session)."""
        if sid not in session.study:
            return False
        flags = list(codes)
        if self.duplicatesOf(sid) and "duplicate" not in flags:
            flags.append("duplicate")
        value = flagsValue(flags)
        if (value or "") == (flagsValue(flagsOf(session.study.subject(sid))) or ""):
            return False
        session.setValues([sid], {"qc_flags": value})
        return True

    # Recorded edits

    def commit(self, session, sid, values, audit):
        """Post values and an audit record to the inbox of the study and merge them now (``.studio/audit.jsonl``)."""
        _commit(session.study, session, sid, values, audit)

    def keepOriginal(self, session, sid, desc, scene=None):
        """Keep the lesion as it was before a fix: ``lesions/<sid>_space-<space>_desc-<desc>_mask.nii.gz`` (the first original is never replaced).

        The saved file is copied when it exists and the scene has no unsaved edits; otherwise ``scene`` (``(array_kji, ijkToRas)``,
        the lesion before the fix) is written on the grid of the subject. Return the path relative to the study, or None.
        """
        study = session.study
        row = study.subject(sid)
        studyModule = _lnm("study")
        space = row.get("space") if not _missing(row.get("space")) else (study.space or "native")
        space = space if re.fullmatch(r"[A-Za-z0-9]+", str(space or "")) else "native"
        target = os.path.join(study.path, studyModule.LESIONS_DIR, f"{sid}_space-{space}_desc-{desc}_mask.nii.gz")
        if os.path.exists(target):
            return study.relpath(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        saved = study.abspath(row["lesion"]) if not _missing(row.get("lesion")) else None
        if saved and os.path.isfile(saved) and not session.isDirty(sid):
            _writeAtomic(target, lambda temporary: shutil.copy2(saved, temporary))
        elif scene is not None and session.gridReference() and os.path.isfile(session.gridReference()):
            array, ijkToRas = scene
            _writeAtomic(target, lambda temporary: _lnm("nifti").write_like(temporary, array, ijkToRas, session.gridReference(), dtype=np.uint8, descrip="Slicer Studio lesion before " + desc))
        else:
            return None
        return study.relpath(target)

    def markDuplicate(self, session, sid, other, dice=None):
        """Exclude the subject (``include`` = 0) and record that it duplicates ``other`` (``duplicate_of``). Return the values to restore."""
        row = session.study.subject(sid)
        previous = {"duplicate_of": row.get("duplicate_of") if not _missing(row.get("duplicate_of")) else None, "include": row.get("include"),
                    "qc_flags": row.get("qc_flags") if not _missing(row.get("qc_flags")) else None}
        flags = [code for code in flagsOf(row) if code != "duplicate"]
        self.commit(session, sid, {"duplicate_of": other, "include": 0, "qc_flags": flagsValue(flags)},
                    {"event": "lesion_duplicate", "other": other, "dice": None if dice is None else round(float(dice), 3), "from": {key: value for key, value in previous.items() if key != "qc_flags"}})
        self._applyDuplicateFlags()
        self._changed()
        return previous

    def restoreDuplicate(self, session, sid, previous):
        values = dict(previous)
        self.commit(session, sid, values, {"event": "lesion_duplicate_undo", "restored": {key: value for key, value in previous.items() if key != "qc_flags"}})
        self._applyDuplicateFlags()
        self._changed()

    def notDuplicate(self, session, sid, other, dice=None):
        """Dismiss a pair: it is not flagged again (the record in the audit log is the memory)."""
        self.dismissed.add(frozenset((sid, other)))
        self.commit(session, sid, {}, {"event": "lesion_not_duplicate", "other": other, "dice": None if dice is None else round(float(dice), 3)})
        self.pairs = [pair for pair in self.pairs if frozenset(pair[:2]) not in self.dismissed]
        self.current = {key: [item for item in value if frozenset((key, item[0])) not in self.dismissed] for key, value in self.current.items()}
        self._applyDuplicateFlags()
        self._changed()

    # After a change of the study

    def afterNotify(self, session):
        """After a change of the study: finish what a Done started (the files of a 2D tracing), or notice a plain write of the lesion."""
        sid = session.current()
        if sid is None or sid not in session.study:
            return
        row = session.study.subject(sid)
        if _missing(row.get("lesion")):
            return
        stat = _stat(session.study.abspath(row["lesion"]))
        pending = self.pendingDone.get(sid)
        if pending is not None:
            written = stat is not None and stat[0] / 1e9 >= pending["time"] - 1.0 and stat != self.lastSeen.get(sid)
            if written:
                self.pendingDone.pop(sid, None)
                self.lastSeen[sid] = stat
                if pending["dimensionality"] in ("2D", "multi2D") or hasExtrusion(session.study, sid):
                    self.process(session, sid, normal=pending["normal"])  # a lesion that is 3D again drops what its extrusion left
                self.scheduleScan()
                self.requestFileHygiene(session, sid)
            elif time.time() - pending["time"] > 20.0:
                self.pendingDone.pop(sid, None)
            return
        if sid not in self.lastSeen:
            self.lastSeen[sid] = stat
            return
        if stat != self.lastSeen[sid]:
            self.lastSeen[sid] = stat
            self.process(session, sid)
            self.scheduleScan()
            self.requestFileHygiene(session, sid)

    def process(self, session, sid, normal=None):
        """``regenerate`` for the subject just written; the lesion file may be rewritten, so its stamp is remembered again."""
        try:
            regenerate(session.study, sid, session, source="lesion", normal=normal)
        except Exception:
            logging.error(f"Studio: the 2D policy could not be applied to {sid}:\n{traceback.format_exc()}")
        row = session.study.subject(sid)
        self.lastSeen[sid] = _stat(session.study.abspath(row["lesion"])) if not _missing(row.get("lesion")) else None

    # The 2D policy

    def askPolicy(self, session, resume=False, change=False):
        """Show the non-modal forced-choice card (``resume``: Done continues once the choice is made; ``change``: a policy exists)."""
        if self.policyCard is not None:
            self.policyCard.dispose()
        self.policyCard = PolicyCard(self, session, resume=resume, change=change)
        self.policyCard.show()
        return self.policyCard

    def choosePolicy(self, session, mode, totalMm=DEFAULT_EXTRUDE_MM, resume=False):
        """Store the choice in ``study.json`` and re-apply it to the 2D lesions already in the study (a changed policy)."""
        study = session.study
        previous = storedPolicy(study)
        stored = setPolicy(study, mode, totalMm)
        session.flush()
        self.hidePolicyCard()
        applied = []
        if previous is None or (previous["mode"], previous["total_mm"]) != (stored["mode"], stored["total_mm"]):
            applied = reapplyExtrusionPolicy(session)
        text = _("2D tracings: {semantics}").format(semantics=stored["semantics"])
        done = [item for item in applied if item.get("mode")]
        if done:
            text += " · " + _("applied to {count} lesions already in the study").format(count=len(done))
        _toast(text, ms=4000, level="consequence")
        self._changed()
        if resume:
            qt.QTimer.singleShot(0, self._resumeDone)
        return stored

    def _resumeDone(self):
        try:
            _draw().doneNext(_shell())
        except Exception:
            logging.error(f"Studio: Done could not continue after the choice:\n{traceback.format_exc()}")

    def hidePolicyCard(self):
        if self.policyCard is not None:
            self.policyCard.dispose()
            self.policyCard = None

    # Atlases (explicit download, a light job)

    def fetchArgs(self, catalog, resourceId):
        return ["get", "--catalog", catalog, "--id", resourceId, "--cache", self.cacheDir()]

    def startAtlasJob(self):
        """Run ``LesionNetworkMappingLib.fetch get`` for the Harvard-Oxford atlases and the priors, one after the other, in the background."""
        if self.atlasJob == "running":
            return False
        self.atlasJob = "running"
        self._atlasQueue = [("atlases", resourceId) for resourceId in ATLAS_IDS] + list(PRIOR_IDS)
        self._atlasError = None
        self._atlasFailed = False
        self._changed()
        self._nextAtlasStep()
        return True

    def _nextAtlasStep(self):
        if not self._atlasQueue or self._atlasFailed:
            self._finishAtlasJob()
            return
        catalog, resourceId = self._atlasQueue.pop(0)
        try:
            jobrunner = importlib.import_module("LesionNetworkMappingUI.jobrunner")
            spec = jobrunner.JobSpec("LesionNetworkMappingLib.fetch", self.fetchArgs(catalog, resourceId), heavy=False, kind="fetch")
            args = [jobrunner.pythonExecutable(), "-m", spec.module, *spec.args]
            # The command line of the fetch job has no --threads: it is not started through the JobRunner (which adds it) but with the same environment
            self._atlasProcess = slicer.util.launchConsoleProcess(args, useStartupEnvironment=False, updateEnvironment=jobrunner.instance().environment(spec, 1),
                                                                  cwd=jobrunner.moduleDir(), blocking=False, logCallback=self._onAtlasLine, completedCallback=self._onAtlasCompleted)
        except Exception as e:
            self._atlasError = str(e)
            self._atlasFailed = True
            qt.QTimer.singleShot(0, self._finishAtlasJob)

    def _onAtlasLine(self, line):
        text = str(line).strip()
        if not text.startswith("{"):
            return
        try:
            event = json.loads(text)
        except ValueError:
            return
        if event.get("event") == "error":
            self._atlasError = str(event.get("message") or event.get("code") or "error")
            self._atlasFailed = True

    def _onAtlasCompleted(self, returnCode):
        if returnCode != 0:
            self._atlasFailed = True
            if not self._atlasError:
                self._atlasError = _("the download job ended with code {code}").format(code=returnCode)
        self._nextAtlasStep()

    def _finishAtlasJob(self):
        self._atlasProcess = None
        if self._atlasFailed:
            self.atlasJob = (self._atlasError or _("unknown error")).splitlines()[0][:120]
        else:
            self.atlasJob = None
            _toast(_("Atlases ready: the location of the lesion is shown"), ms=3000)
            self.ensureResources(reload=True)
        self._changed()


_core = None


def core():
    """The ``QcCore`` of the application (created on first use)."""
    global _core
    if _core is None:
        _core = QcCore()
    return _core


#
# Dimensionality and extrusion (files of a study; numpy only apart from the session calls)
#


def figureOwnsPlanes(row, planeNormal=None):
    """True when the ``planes`` column of ``row`` is the Figure card's (L-29): the lesion was traced from a figure.

    The column is shared: for a figure tracing it holds the planes of the figure panels, for another 2D tracing the plane found
    by ``detect_dimensionality``. The checks and the extrusion never write or clear the figure's planes.
    """
    if planeNormal is not None:
        return True
    row = row or {}
    return "figure" in str(row.get("trace_method") or "") or not _missing(row.get("fig_convention"))


def _extrusionColumns(row, planeNormal=None):
    """``EXTRUSION_COLUMNS`` that belong to the extrusion for this row (``planes`` is left out for a figure tracing)."""
    return tuple(key for key in EXTRUSION_COLUMNS if key != "planes" or not figureOwnsPlanes(row, planeNormal))


def dimensionColumns(info, planeNormal=None, figure=False):
    """Row values of a ``detect_dimensionality`` result: ``dimensionality``, and for 2D and multi2D ``planes`` and ``n_slices``.

    ``planes`` is left to the Figure card when a plane normal is given or ``figure`` is true (its planes are the figure's).
    """
    dim = info.get("dimensionality")
    values = {"dimensionality": dim}
    if dim in ("2D", "multi2D"):
        planes = [plane for plane in info.get("planes") or [] if plane]
        if planeNormal is None and not figure and planes:
            values["planes"] = ";".join(planes)
        if dim == "2D":
            plane = planes[0] if planes else None
            values["n_slices"] = info.get("n_slices_normal") if plane == "oblique" else (info.get("n_slices") or {}).get(plane, 1)
        else:
            values["n_slices"] = info.get("n_components")
    return values


EXTRUSION_COLUMNS = ("planes", "n_slices", "extrude_mm", "fractional_sum")


def hasExtrusion(study, sid):
    """True when the row or the files next to the lesion still hold the result of an earlier 2D extrusion."""
    row = study.subject(sid)
    if any(not _missing(row.get(key)) for key in _extrusionColumns(row)):
        return True
    if _missing(row.get("lesion")):
        return False
    lesionPath = study.abspath(row["lesion"])
    return os.path.exists(descPath(lesionPath, "traced")) or os.path.exists(descPath(lesionPath, "lesionProb"))


def _dropExtrusion(study, session, sid, row, values, tracedPath, probPath, result, normal=None):
    """A lesion that is no longer a 2D tracing (it was edited into a 3D one): remove what an earlier extrusion left.

    The row's extrusion columns are cleared (not the planes of a figure tracing), ``desc-lesionProb`` and ``desc-traced`` next to
    the lesion (in ``lesions/``) are removed and the ``extrude`` record of the sidecar is dropped, with one audit line. Nothing is
    written when there is no trace of an earlier extrusion.
    """
    schema = _lnm("schema")
    for key in _extrusionColumns(row, normal):
        if not _missing(row.get(key)):
            values[key] = None
    removed = []
    for path in (probPath, tracedPath):
        if os.path.exists(path):
            os.remove(path)
            removed.append(study.relpath(path))
    dropped = False
    sidecarPath = study.lesion_sidecar_path(sid)
    try:
        with open(sidecarPath, encoding="utf-8-sig") as f:
            data = json.load(f)
        entry = data.get(schema.FORK_KEY) if isinstance(data, dict) else None
        if isinstance(entry, dict) and "extrude" in entry:
            del entry["extrude"]
            schema.write_text_atomic(sidecarPath, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
            dropped = True
    except (OSError, ValueError):
        pass
    changed = values != {key: row.get(key) for key in values}
    if removed or dropped:
        result["files"] = []
        result["removed"] = removed
        _commit(study, session, sid, values, {"event": "lesion_extrude", "mode": None, "description": "reverted: the lesion is 3D again",
                                              "dimensionality": values.get("dimensionality"), "removed": removed})
    elif changed:
        _commit(study, session, sid, values, {"event": "lesion_dimensionality", "dimensionality": values.get("dimensionality")})


def regenerate(study, sid, session=None, source="lesion", normal=None):
    """Write the analysis files of a 2D tracing from the drawn one and update the row, the sidecar and the audit log.

    ``source`` "lesion": the lesion file of the row is the tracing as drawn (it was just written): it is kept as ``desc-traced``
    when the policy extrudes. ``source`` "traced": the tracing as drawn is ``desc-traced`` (re-apply of a changed policy).
    The policy is ``study.json`` ``trace_policy.extrude_2d``:

    - single: ``desc-lesion`` is the drawn tracing (restored from ``desc-traced``), a stale ``desc-lesionProb`` is removed;
    - fractional: ``desc-lesionProb`` is the slab with exact thickness (edge slices fractional), ``desc-lesion`` its binary
      version (>= 0.5);
    - binary_pm: ``desc-lesion`` is the binary slab (± whole voxels), no ``desc-lesionProb``.

    Only 2D and multi2D tracings are extruded. The row always gets ``dimensionality`` (and for 2D the planes, ``n_slices`` and
    ``extrude_mm``). Return ``{sid, dimensionality, mode, files, values}`` or None when there was nothing to read.
    """
    nifti = _lnm("nifti")
    ops = _lnm("lesionops")
    row = study.subject(sid)
    if _missing(row.get("lesion")):
        return None
    lesionPath = study.abspath(row["lesion"])
    lesionsFolder = os.path.join(os.path.abspath(study.path), _lnm("study").LESIONS_DIR)
    if os.path.commonpath([os.path.abspath(lesionPath), lesionsFolder]) != lesionsFolder:
        return None  # a lesion file of the user's (outside lesions/) is never modified, and nothing is written next to it
    tracedPath, probPath = descPath(lesionPath, "traced"), descPath(lesionPath, "lesionProb")
    if source == "traced" and not os.path.isfile(tracedPath):
        source = "lesion"  # never extruded: the lesion file is the tracing as drawn
    sourcePath = tracedPath if source == "traced" else lesionPath
    if not os.path.isfile(sourcePath):
        return None
    data, affine, _header = nifti.read_nifti(sourcePath)
    data = data[..., 0] if data.ndim > 3 else data
    drawn = np.asarray(data) > 0
    info = ops.detect_dimensionality(drawn, affine, normal)
    dim = info["dimensionality"]
    if dim is None:
        return None
    values = dimensionColumns(info, normal, figure=figureOwnsPlanes(row))
    result = {"sid": sid, "dimensionality": dim, "mode": None, "files": [], "values": values, "info": info}
    policy = storedPolicy(study)
    if dim not in ("2D", "multi2D") and source == "lesion":
        _dropExtrusion(study, session, sid, row, values, tracedPath, probPath, result, normal)
        return result
    if dim not in ("2D", "multi2D") or policy is None:
        if values != {key: row.get(key) for key in values}:
            _commit(study, session, sid, values, {"event": "lesion_dimensionality", "dimensionality": dim})
        return result
    mode = policy["mode"]
    result["mode"] = mode
    removed = []
    files = {}
    audit = {"event": "lesion_extrude", "mode": mode, "total_mm": policy["total_mm"], "dimensionality": dim, "source": source}
    if mode == MODE_SINGLE:
        values["extrude_mm"] = None
        if source == "traced":
            _writeAtomic(lesionPath, lambda temporary: shutil.copy2(tracedPath, temporary))
            files["lesion"] = lesionPath
        elif os.path.exists(tracedPath):
            _writeAtomic(tracedPath, lambda temporary: shutil.copy2(lesionPath, temporary))  # desc-traced stays "as drawn"
            files["traced"] = tracedPath
        if os.path.exists(probPath):
            os.remove(probPath)
            removed.append(study.relpath(probPath))
        audit.update(description="kept as a single slice", effective_mm=None)
        if "fractional_sum" in row:
            values["fractional_sum"] = None
    else:
        total = float(policy["total_mm"] or DEFAULT_EXTRUDE_MM)
        spacing = np.sqrt((np.asarray(affine, dtype=float)[:3, :3] ** 2).sum(axis=0))
        pad = math.ceil(total / float(spacing.min())) + 3
        found = np.argwhere(drawn)
        low = np.maximum(found.min(axis=0) - pad, 0)
        high = np.minimum(found.max(axis=0) + pad + 1, np.array(drawn.shape))
        sub = drawn[low[0]:high[0], low[1]:high[1], low[2]:high[2]]
        shift = np.eye(4)
        shift[:3, 3] = low
        subAffine = np.asarray(affine, dtype=float) @ shift
        out, record = ops.extrude(sub, subAffine, total, normal=normal, mode=mode)
        if source == "lesion":
            _writeAtomic(tracedPath, lambda temporary: shutil.copy2(lesionPath, temporary))
        files["traced"] = tracedPath
        kji = np.ascontiguousarray(out.transpose(2, 1, 0))
        binary = (kji >= ops.LESION_THRESHOLD if mode == MODE_FRACTIONAL else kji > 0).astype(np.uint8)
        _writeAtomic(lesionPath, lambda temporary: nifti.write_like(temporary, binary, subAffine, tracedPath, dtype=np.uint8, descrip="Slicer Studio lesion (extruded)"))
        files["lesion"] = lesionPath
        voxelMl = abs(float(np.linalg.det(np.asarray(affine, dtype=float)[:3, :3]))) / 1000.0
        if mode == MODE_FRACTIONAL:
            _writeAtomic(probPath, lambda temporary: nifti.write_like(temporary, kji.astype(np.float32), subAffine, tracedPath, dtype=np.float32, descrip="Slicer Studio lesion probability"))
            files["lesionProb"] = probPath
            values["volume_ml"] = round(float(kji.sum()) * voxelMl, 6)
            values["fractional_sum"] = round(float(kji.sum()) * voxelMl, 6)
        else:
            if os.path.exists(probPath):
                os.remove(probPath)
                removed.append(study.relpath(probPath))
            values["volume_ml"] = round(float(binary.sum()) * voxelMl, 6)
            if "fractional_sum" in row:
                values["fractional_sum"] = None
        values["extrude_mm"] = round(float(record["effective_mm"]), 4)
        audit.update(description=record["description"], effective_mm=values["extrude_mm"], oblique=record["oblique"], clipped=record["clipped"], planes=record["planes"])
        result["record"] = record
    stat = os.stat(lesionPath)
    values["lesion_mtime"] = round(stat.st_mtime, 3)
    values["lesion_size"] = stat.st_size
    if _core is not None:
        _core.lastSeen[sid] = (stat.st_mtime_ns, stat.st_size)  # before the commit: the listener must not take this write for a drawn lesion
    audit["files"] = {name: study.relpath(path) for name, path in files.items()}
    if removed:
        audit["removed"] = removed
    result["files"] = sorted(files.values())
    result["values"] = values
    try:
        sidecar = {"mode": mode, "total_mm": policy["total_mm"], "semantics": policy.get("semantics") or policySemantics(mode, policy["total_mm"] or DEFAULT_EXTRUDE_MM),
                   "traced": os.path.basename(tracedPath) if mode != MODE_SINGLE else None, "prob": os.path.basename(probPath) if mode == MODE_FRACTIONAL else None}
        if "record" in result:
            sidecar.update({key: result["record"][key] for key in ("effective_mm", "voxels_per_side", "description", "planes", "normal", "oblique", "clipped")})
        study.write_lesion_sidecar(sid, studio={"extrude": sidecar})
    except Exception:
        logging.warning(f"Studio: the sidecar of {sid} could not record the extrusion:\n{traceback.format_exc()}")
    _commit(study, session, sid, values, audit)
    return result


def reapplyExtrusionPolicy(study=None):
    """Regenerate the analysis lesions of every 2D and multi2D subject from ``desc-traced`` with the study's policy.

    ``study`` is a ``Study`` or a ``StudySession`` (default: the open one). A subject open in Draw with unsaved edits is skipped.
    Return the list of results of ``regenerate`` (``{sid, skipped}`` for a skipped one); empty without a policy.
    """
    session = _session()
    if study is None:
        study = session.study if session is not None else None
    elif hasattr(study, "study") and hasattr(study, "current"):
        session, study = study, study.study
    if study is None or storedPolicy(study) is None:
        return []
    results = []
    for row in list(study.subjects):
        sid = row["participant_id"]
        if row.get("dimensionality") not in ("2D", "multi2D") or _missing(row.get("lesion")):
            continue
        if session is not None and session.study is study and session.isDirty(sid):
            results.append({"sid": sid, "skipped": "unsaved edits"})
            continue
        sidecar = study.read_lesion_sidecar(sid) or {}
        normal = ((sidecar.get(_lnm("schema").FORK_KEY) or {}).get("extrude") or {}).get("normal")
        try:
            result = regenerate(study, sid, session if session is not None and session.study is study else None, source="traced", normal=normal)
        except Exception:
            logging.error(f"Studio: the extrusion of {sid} could not be re-applied:\n{traceback.format_exc()}")
            result = {"sid": sid, "skipped": "error"}
        if result is not None:
            results.append(result)
            if session is not None and session.study is study:
                core().lastSeen[sid] = _stat(study.abspath(study.subject(sid)["lesion"]))
    return results


#
# Study hooks (Done, open, changes of the study)
#


def _spaceInfo(node):
    """``(mni, spaceName)`` of a segmentation."""
    from . import space

    found = space.spaceOf(node)
    return (found is not None), (found[0] if found else None)


def _figureNormal():
    """The plane normal of the Figure card when exactly one panel is being traced, else None (the figure module is not imported for it)."""
    import sys

    module = sys.modules.get("StudioLib.figure")
    current = module.session() if module is not None and hasattr(module, "session") else None
    panels = getattr(current, "panels", None) if current is not None else None
    if panels and len(panels) == 1:
        try:
            return [float(value) for value in current.planeNormals()[0]]
        except Exception:
            return None
    return None


def doneHook(sid, labelmapArray, ijkToRas, ctx):
    """Done → next: dimensionality, the 2D policy (a forced choice) and the qc_flags of the lesion.

    A 2D or multi2D tracing in a study without ``trace_policy.extrude_2d`` shows the policy card and blocks Done until it is
    answered. The columns are returned; the files follow once the lesion is written (``QcCore.afterNotify``).
    """
    studyUi = _studyUi()
    session = ctx.get("session")
    study = ctx.get("study")
    found = lesionCrop(labelmapArray, ijkToRas)
    if found is None or study is None:
        return None
    values, affine, _bbox = found
    ops = _lnm("lesionops")
    figureNormals = ctx.get("figure_plane_normals") or []
    normal = ctx.get("figure_plane_normal") if len(figureNormals) == 1 else None
    info = ops.detect_dimensionality(values, affine, normal)
    dim = info["dimensionality"]
    extras = dimensionColumns(info, normal)
    if dim in ("2D", "multi2D") and storedPolicy(study) is None:
        core().askPolicy(session, resume=True)
        raise studyUi.Blocking(_("This is a single-slice tracing. Choose once how 2D tracings are used in this study (the card in Draw), then Done again"))
    row = ctx.get("row") or {}
    node = ctx.get("segmentationNode")
    mni, spaceName = _spaceInfo(node) if node is not None else (False, None)
    result = evaluate(values, affine, mni=mni, space=spaceName, reportedSide=None if _missing(row.get("reported_side")) else row.get("reported_side"),
                      multifocal=str(row.get("multifocal")) == "1", resources=core().resources, planeNormal=normal, tight=True)
    flags = list(result["flags"])
    if core().duplicatesOf(sid):
        flags.append("duplicate")
    extras["qc_flags"] = flagsValue(flags)
    core().pendingDone[sid] = {"time": time.time(), "normal": normal, "dimensionality": dim}
    return extras


def restoreTraced(session, sid, ctx):
    """A 2D lesion that was extruded opens as drawn: the scene gets ``desc-traced`` (the analysis files are regenerated on Done)."""
    study = session.study
    row = study.subject(sid)
    policy = storedPolicy(study)
    if row.get("dimensionality") not in ("2D", "multi2D") or _missing(row.get("lesion")) or policy is None or policy["mode"] == MODE_SINGLE:
        return False
    tracedPath = descPath(study.abspath(row["lesion"]), "traced")
    node = ctx.get("segmentationNode")
    if not os.path.isfile(tracedPath) or node is None or node.GetSegmentation().GetNumberOfSegments() < 1:
        return False
    draw = _draw()
    data, affine, _header = _lnm("nifti").read_nifti(tracedPath)
    data = data[..., 0] if data.ndim > 3 else data
    kji = np.ascontiguousarray((np.asarray(data) > 0).transpose(2, 1, 0)).astype(np.uint8)
    segmentId = node.GetSegmentation().GetNthSegmentID(0)
    openStamp = getattr(session, "_openStamp", None)
    session._openStamp = None  # the segment observers ignore this change: the lesion is not edited, only shown as it was drawn
    try:
        draw._writeMask(node, segmentId, kji, affine, (0, kji.shape[2] - 1, 0, kji.shape[1] - 1, 0, kji.shape[0] - 1))
    finally:
        studyUi = _studyUi()
        session._openStamp = studyUi._contentStamp(node) if studyUi is not None else openStamp
        session._dirty = False
    _markSaved(node, study.abspath(row["lesion"]))
    return True


def _markSaved(node, path):
    """The Lesion card shows "Saved" for a lesion that was only put back as drawn (its analysis file is regenerated on Done)."""
    try:
        import vtk

        draw = _draw()
        reference = vtk.vtkWeakReference()
        reference.Set(node)
        draw._savedRecords[node.GetID()] = (reference, path, draw._contentStamp(node), _now())
    except Exception:
        logging.debug("Studio: the save state of a restored lesion was not set", exc_info=True)


def openHook(sid, ctx):
    """After a subject is opened: remember the lesion file, check its header and values, show a 2D lesion as drawn."""
    session = ctx.get("session")
    if session is None or sid not in session.study:
        return
    c = core()
    row = session.study.subject(sid)
    c.lastSeen[sid] = _stat(session.study.abspath(row["lesion"])) if not _missing(row.get("lesion")) else None
    c.current.pop(sid, None)
    try:
        c.requestFileHygiene(session, sid)
        restoreTraced(session, sid, ctx)
    except Exception:
        logging.error(f"Studio: the lesion of {sid} could not be checked on opening:\n{traceback.format_exc()}")


_busy = False
_installedHooks = False


def _onStudyChanged(studySession=None):
    """Study listener: keep the hooks on the open session and do the work that follows a write of the lesion."""
    global _busy
    studyUi = _studyUi()
    if studyUi is None or _busy:
        return
    session = studySession or studyUi.session()
    if session is None:
        if _core is not None:
            _core.studyClosed()
        return
    session.removeDoneHook(doneHook)
    session.addDoneHook(doneHook, order=DONE_HOOK_ORDER)
    session.removeOpenHook(openHook)
    session.addOpenHook(openHook)
    c = core()
    c.studyOpened(session)
    _busy = True
    try:
        c.afterNotify(session)
    except Exception:
        logging.error(f"Studio: the lesion QC could not follow the study:\n{traceback.format_exc()}")
    finally:
        _busy = False


def installStudyHooks():
    """Register the study listener, the Search entries and the hooks of the open study (idempotent)."""
    global _installedHooks
    studyUi = _studyUi()
    if studyUi is None:
        return False
    studyUi.removeListener(_onStudyChanged)
    studyUi.addListener(_onStudyChanged)
    if not _installedHooks:
        _installedHooks = True
        try:
            palette = importlib.import_module("StudioLib.palette")
            palette.unregisterProvider(_paletteEntries)
            palette.registerProvider(_paletteEntries)
        except Exception:
            pass
    _onStudyChanged()
    return True


#
# The 2D policy card (a transient, non-modal forced choice)
#


def _setPixelFont(widget, basePx, weight=None):
    font = qt.QFont(widget.font)
    font.setPixelSize(ui.fontPx(basePx))
    if weight is not None:
        font.setWeight(weight)
    widget.setFont(font)


class PolicyCard:
    """Choose once how 2D tracings are used in the study: keep them single-slice, extrude to a total thickness with fractional
    edge slices (exact thickness), or extrude ±half the thickness as a binary mask. The tracing as drawn is always kept.

    It is an inspector card of Draw (``draw.addInspectorCard``), in Classic a small non-modal window. With ``resume`` (Done was
    blocked) Done continues once the choice is made; with ``change`` (a policy exists) there is a Cancel button.
    """

    def __init__(self, qcCore, session, resume=False, change=False):
        self.core = qcCore
        self.session = session
        self.resume = resume
        self.change = change
        self._window = None
        self._hosted = False
        self._shell = None
        frame, layout = ui.card(_("2D tracing"))
        frame.setObjectName("StudioExtrusionPolicyCard")
        frame.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        self.widget = frame
        current = storedPolicy(session.study) if session is not None else None
        intro = (_("Now: {semantics}. Choosing another re-applies it to the 2D lesions of the study from the tracings as drawn.").format(semantics=policySemantics(current["mode"], current["total_mm"] or DEFAULT_EXTRUDE_MM))
                 if change and current else _("This lesion is a single slice. Choose once how 2D tracings are used in this study. The tracing as drawn is always kept (desc-traced)."))
        self.intro = qt.QLabel(intro)
        self.intro.setObjectName("StudioExtrusionPolicyIntro")
        self.intro.setWordWrap(True)
        self.intro.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        _setPixelFont(self.intro, 12)
        layout.addWidget(self.intro)
        thickness = qt.QHBoxLayout()
        thickness.setSpacing(6)
        thicknessLabel = qt.QLabel(_("Thickness"))
        _setPixelFont(thicknessLabel, 12)
        thickness.addWidget(thicknessLabel)
        self.spin = qt.QDoubleSpinBox()
        self.spin.setObjectName("StudioExtrusionMmSpin")
        self.spin.setRange(0.5, 20.0)
        self.spin.setSingleStep(0.5)
        self.spin.setDecimals(1)
        self.spin.setSuffix(" mm")
        self.spin.setValue(float((current or {}).get("total_mm") or DEFAULT_EXTRUDE_MM))
        self.spin.setAccessibleName(_("Total thickness in mm"))
        self.spin.connect("valueChanged(double)", self._onThickness)
        thickness.addWidget(self.spin)
        thickness.addStretch(1)
        layout.addLayout(thickness)
        self.fractionalButton = self._button("StudioExtrusionFractionalButton", "primary", self._onFractional)
        self.binaryButton = self._button("StudioExtrusionBinaryButton", "secondary", self._onBinary)
        self.keepButton = self._button("StudioExtrusionKeepButton", "secondary", self._onKeep)
        self.keepButton.setText(_("Keep single-slice"))
        self.keepButton.setToolTip(ui.tooltip(_("Keep single-slice"), None, _("map the one slice only")))
        for button in (self.fractionalButton, self.binaryButton, self.keepButton):
            layout.addWidget(button)
        self.cancelButton = None
        if change:
            self.cancelButton = qt.QToolButton()
            self.cancelButton.setObjectName("StudioExtrusionCancelButton")
            self.cancelButton.setText(_("Cancel"))
            self.cancelButton.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
            self.cancelButton.setFocusPolicy(qt.Qt.TabFocus)
            self.cancelButton.connect("clicked()", self._onCancel)
            ui.setRole(self.cancelButton, "ghost")
            ui.installButtonKeys(self.cancelButton)
            layout.addWidget(self.cancelButton, 0, qt.Qt.AlignRight)
        self._onThickness(self.spin.value)

    def _button(self, objectName, role, callback):
        button = qt.QPushButton()
        button.setObjectName(objectName)
        button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        button.setFocusPolicy(qt.Qt.TabFocus)
        button.connect("clicked()", callback)
        ui.setRole(button, role)
        ui.installButtonKeys(button)
        return button

    def _onThickness(self, value):
        total = float(self.spin.value)
        self.fractionalButton.setText(_("Extrude to {mm:g} mm total").format(mm=total))
        self.fractionalButton.setToolTip(ui.tooltip(_("Extrude to {mm:g} mm total").format(mm=total), None, _("exact total thickness centred on the drawn slice; the edge slices are fractional (desc-lesionProb)")))
        self.binaryButton.setText(_("Extrude ±{mm:g} mm, binary").format(mm=total / 2.0))
        self.binaryButton.setToolTip(ui.tooltip(_("Extrude ±{mm:g} mm, binary").format(mm=total / 2.0), None, _("whole voxels on both sides of the drawn slice (3 voxels at 1 mm)")))

    def _choose(self, mode):
        if _session() is not self.session:  # the study of this card was closed or replaced: nothing is written to it
            self.core.hidePolicyCard()
            return
        self.core.choosePolicy(self.session, mode, float(self.spin.value), resume=self.resume)

    def _onFractional(self):
        self._choose(MODE_FRACTIONAL)

    def _onBinary(self):
        self._choose(MODE_BINARY)

    def _onKeep(self):
        self._choose(MODE_SINGLE)

    def _onCancel(self):
        self.core.hidePolicyCard()

    def show(self):
        """In Draw (Studio) an inspector card; otherwise a non-modal window."""
        shell = _shell()
        draw = _draw()
        try:
            studioActive = shell is not None and shell.isActive()
        except Exception:
            studioActive = False
        if studioActive:
            draw.addInspectorCard(POLICY_CARD_KEY, self.widget, POLICY_CARD_ORDER)
            self._hosted = True
            self._shell = shell
            with contextlib.suppress(Exception):
                shell.registerEscapeOverlay(self.widget, self.core.hidePolicyCard)  # Esc closes the card (Done stays blocked and asks again)
            if shell.currentWorkspace() != "draw":
                shell.switchTo("draw")
            qt.QTimer.singleShot(0, lambda: self.fractionalButton.setFocus(qt.Qt.OtherFocusReason) if ui._isAlive(self.fractionalButton) else None)
            return
        window = qt.QDialog(slicer.util.mainWindow())
        window.setObjectName("StudioExtrusionPolicyWindow")
        window.setWindowTitle(_("2D tracing"))
        window.setModal(False)
        windowLayout = qt.QVBoxLayout(window)
        windowLayout.addWidget(self.widget)
        window.setMinimumWidth(340)
        self._window = window
        window.show()
        window.raise_()

    def dispose(self):
        if self._hosted:
            with contextlib.suppress(Exception):
                self._shell.unregisterEscapeOverlay(self.widget)
            with contextlib.suppress(Exception):
                _draw().removeInspectorCard(POLICY_CARD_KEY)
            self._hosted = False
        if self._window is not None:
            self._window.hide()
            self._window.deleteLater()
            self._window = None
        if ui._isAlive(self.widget):
            _discardWidget(self.widget)


#
# The chips of the Lesion card
#


class _QcPanel:
    """The chip rows: one line per chip (a coloured label, a text that wraps and the actions as buttons)."""

    def __init__(self, qc):
        self.qc = qc
        self.widget = qt.QWidget()
        self.widget.setObjectName(ROW_OBJECT_NAME)
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        self.layout = qt.QVBoxLayout(self.widget)
        self.layout.setContentsMargins(0, 2, 0, 0)
        self.layout.setSpacing(5)
        self.chips = {}
        self.signature = None

    def clear(self):
        self.setChips([])

    def alive(self):
        """False once the row was destroyed (the Lesion card or the main window is gone, for example while the application quits)."""
        return ui._isAlive(self.widget) and ui._isAlive(self.layout)

    def setChips(self, specs):
        signature = json.dumps(specs, sort_keys=True, default=str)
        if signature == self.signature or not self.alive():
            return
        self.signature = signature
        for widget in self.chips.values():
            self.layout.removeWidget(widget)
            _discardWidget(widget)
        self.chips = {}
        for spec in specs:
            widget = self._build(spec)
            self.layout.addWidget(widget)
            self.chips[spec["key"]] = widget
        self.widget.setVisible(bool(specs))

    def _build(self, spec):
        frame = qt.QFrame()
        frame.setObjectName("StudioQcChip_" + spec["key"])
        frame.setProperty("studioQcLevel", spec["level"])
        frame.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        frame.setAccessibleName(f"{spec['badge']}: {spec['text']}")
        layout = qt.QVBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = qt.QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        badge = ui.chip(spec["badge"], spec["level"])
        badge.setObjectName("StudioQcBadge")
        badge.setToolTip(spec.get("tooltip") or "")
        top.addWidget(badge, 0, qt.Qt.AlignTop)
        label = qt.QLabel(spec["text"])
        label.setObjectName("StudioQcText")
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        label.setToolTip(spec.get("tooltip") or "")
        _setPixelFont(label, 12)
        token = "text" if spec["level"] == "warn" else "text2"
        ui.bindStyleSheet(label, lambda token=token: f"QLabel {{ color: {ui.token(token).name()}; background: transparent; border: none; }}")
        top.addWidget(label, 1)
        layout.addLayout(top)
        if spec.get("actions"):
            row = qt.QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)
            for action in spec["actions"]:
                button = qt.QToolButton()
                button.setObjectName("StudioQc" + action["id"][0].upper() + action["id"][1:] + "Button")
                button.setText(action["text"])
                button.setToolTip(action.get("tooltip") or "")
                button.setToolButtonStyle(qt.Qt.ToolButtonTextOnly)
                button.setFocusPolicy(qt.Qt.TabFocus)
                button.connect("clicked()", lambda identifier=action["id"]: self.qc.onAction(identifier))
                ui.setRole(button, "ghost")
                ui.installButtonKeys(button)
                _setPixelFont(button, 12)
                row.addWidget(button)
            row.addStretch(1)
            layout.addLayout(row)
        return frame


def _digestOf(values, affine):
    """A value that is equal for equal lesions (shape, position and the voxels), to notice an undo of a fix."""
    import zlib

    return (values.shape, tuple(np.round(np.asarray(affine, dtype=float)[:3, 3], 3)), zlib.crc32(np.ascontiguousarray(values).tobytes()))


class LesionQc:
    """The QC chips of one Lesion card (``attach(card)``): evaluation 300 ms after the last change, the chip row and the fixes."""

    def __init__(self, card):
        self.card = card
        self.panel = _QcPanel(self)
        card.addChipRow(ROW_KEY, self.panel.widget)
        self.result = None
        self.chips = []
        self.lastMs = None
        self.lastExtraMs = None
        self.lastTimings = {}
        self.runs = 0
        self._token = 0
        self._stopped = False
        self._version = -1
        self._sceneTags = []
        self._key = None
        self._pendingKey = None
        self._lesionId = None
        self._context = None
        self._digest = None
        self._fixes = []
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.connect("timeout()", self.run)
        core().addListener(self._onCoreChanged)
        self._menuLabel = _("2D extrusion policy…")
        card.addMenuAction(self._menuLabel, self.choosePolicy)
        scene = slicer.mrmlScene
        self._sceneTags = [scene.AddObserver(slicer.vtkMRMLScene.StartCloseEvent, self._onSceneClosing),
                           scene.AddObserver(slicer.vtkMRMLScene.EndCloseEvent, self._onSceneClosed)]
        slicer.app.connect("aboutToQuit()", self._onAboutToQuit)

    def _stop(self):
        """No evaluation, render or core callback from now on: the timers, the core listener and the scene observers are released."""
        self._stopped = True
        self._timer.stop()
        self._token += 1
        core().removeListener(self._onCoreChanged)
        for tag in self._sceneTags:
            slicer.mrmlScene.RemoveObserver(tag)
        self._sceneTags = []

    def _onAboutToQuit(self):
        """The application quits: an evaluation pending (300 ms after the last edit) must not run once the card is destroyed."""
        self._stop()

    def dispose(self):
        self._stop()
        with contextlib.suppress(Exception):
            slicer.app.disconnect("aboutToQuit()", self._onAboutToQuit)
        self.card.removeChipRow(ROW_KEY)
        self.card.removeMenuAction(self._menuLabel)

    def _onSceneClosing(self, caller=None, event=None):
        self._timer.stop()
        self._token += 1

    def _onSceneClosed(self, caller=None, event=None):
        """The lesion is gone: no chips, no fixes to follow."""
        self._timer.stop()
        self._token += 1
        self._lesionId = None
        self._key = None
        self._pendingKey = None
        self._digest = None
        self._fixes = []
        self._context = None
        self.result = None
        self._render()
        core().current.clear()

    # Scheduling

    def notify(self, state):
        """Called by the card on every refresh: restart the debounce when the lesion, its row or the resources changed."""
        if self._stopped:
            return
        lesionId = state.get("lesionId")
        if not lesionId or not state.get("enabled", True):
            self._lesionId = None
            self._key = None
            self._timer.stop()
            self.result = None
            self._render()
            return
        node = slicer.mrmlScene.GetNodeByID(lesionId)
        if node is None:
            return
        draw = _draw()
        session = self._sessionFor(node)
        row = None
        if session is not None:
            sid = session.current()
            row = session.study.subject(sid) if sid in session.study else None
        rowKey = (row.get("reported_side"), row.get("reported_location"), row.get("multifocal"), row.get("lesion")) if row else None
        policy = storedPolicy(session.study) if session is not None else None
        key = (lesionId, draw._contentStamp(node), rowKey, core().resources.version, json.dumps(policy, sort_keys=True) if policy else None)
        self._lesionId = lesionId
        if key == self._key or (key == self._pendingKey and self._timer.isActive()):
            return
        self._pendingKey = key
        self._timer.start()

    def _sessionFor(self, node):
        session = _session()
        if session is None or session.current() is None:
            return None
        current = session.segmentationNode()
        return session if current is not None and node is not None and current.GetID() == node.GetID() else None

    def _onCoreChanged(self):
        """Resources were loaded: evaluate again. Duplicates, file checks or the atlas job changed: the chips are shown again at once."""
        if self._lesionId is None:
            return
        if core().resources.version != self._version:
            self._pendingKey = None
            self._key = None
            self._timer.start()
        elif self.result is not None:
            self._render()

    # Evaluation

    def lesion(self):
        """``{node, segmentId, values, affine, bbox, mask, matrix}`` of the lesion shown by the card, None when it is empty."""
        draw = _draw()
        node = slicer.mrmlScene.GetNodeByID(self._lesionId) if self._lesionId else None
        if node is None:
            return None
        segmentId = draw._selectedSegmentId(node, draw.segmentEditor())
        data = draw._labelmapArray(node, segmentId) if segmentId else None
        if data is None:
            return None
        found = lesionCrop(data[0], data[1])
        if found is None:
            return None
        return {"node": node, "segmentId": segmentId, "values": found[0], "affine": found[1], "bbox": found[2], "mask": data[0], "matrix": data[1]}

    def run(self):
        """Evaluate the chips now (the debounce timer calls it): the first stage here, the CSF share and the atlas labels in the next event."""
        self._timer.stop()
        if self._stopped or not self.panel.alive():
            return
        started = time.perf_counter()
        self._key = self._pendingKey
        self._token += 1
        self._version = core().resources.version
        lesion = self.lesion()
        if lesion is None:
            self.result = None
            self._context = None
            self._render()
            return
        session = self._sessionFor(lesion["node"])
        sid = session.current() if session is not None else None
        row = session.study.subject(sid) if sid and sid in session.study else None
        mni, spaceName = _spaceInfo(lesion["node"])
        c = core()
        if mni:
            c.ensureResources()
        normal = _figureNormal() if session is not None else None
        result = evaluateCore(lesion["values"], lesion["affine"], mni=mni, space=spaceName,
                              reportedSide=None if not row or _missing(row.get("reported_side")) else row["reported_side"],
                              multifocal=bool(row) and str(row.get("multifocal")) == "1", resources=c.resources, planeNormal=normal)
        self.result = result
        self._context = {"session": session, "sid": sid, "row": row, "mni": mni, "space": spaceName, "lesion": lesion}
        digest = _digestOf(lesion["values"], lesion["affine"])
        self._followUndo(session, sid, digest)
        if digest != self._digest:
            self._digest = digest
            if session is not None and sid:
                c.current.pop(sid, None)
                if mni:
                    c.requestCurrent(session, sid, lesion["values"], lesion["affine"])
        if session is not None and sid:
            c.requestFileHygiene(session, sid)
        self._render()
        self.lastTimings = dict(result["ms"])
        self.lastMs = (time.perf_counter() - started) * 1000.0
        self.lastTimings["stage1"] = self.lastMs
        self.runs += 1
        if mni and c.resources.state == "ready" and (c.resources.atlases or c.resources.csf or c.resources.ventricles):
            token = self._token
            qt.QTimer.singleShot(0, lambda: self._runExtra(token, lesion["values"], lesion["affine"], spaceName))
        else:
            self._syncFlags()

    def _runExtra(self, token, values, affine, spaceName):
        """The second stage (CSF share, atlas labels), unless a newer evaluation replaced this one."""
        if token != self._token or self.result is None:
            return
        started = time.perf_counter()
        evaluateExtra(self.result, values, affine, spaceName, core().resources)
        self._render()
        self.lastExtraMs = (time.perf_counter() - started) * 1000.0
        self.lastTimings["extra"] = self.lastExtraMs
        self.lastTimings["total"] = self.lastMs + self.lastExtraMs
        self._syncFlags()

    def _syncFlags(self):
        """The qc_flags of a saved lesion follow the checks (a lesion that was never written has no flags in the table yet)."""
        context = self._context or {}
        session, sid, row = context.get("session"), context.get("sid"), context.get("row")
        if session is not None and sid and row is not None and not _missing(row.get("lesion")) and self.result is not None:
            core().syncFlags(session, sid, self.result["flags"])

    def _render(self):
        if self._stopped or not self.panel.alive():
            return
        c = core()
        context = self._context or {}
        session, sid, row = context.get("session"), context.get("sid"), context.get("row")
        policy = storedPolicy(session.study) if session is not None else None
        fileHygiene = c.fileHygiene.get(sid, (None, None))[1] if sid else None
        self.chips = buildChips(self.result, row, context.get("mni", False), c.duplicatesOf(sid) if sid else [], policy, session is not None,
                                c.resources.state, c.resources.missingAtlases, c.atlasJob, fileHygiene)
        self.panel.setChips(self.chips)

    # Actions

    def onAction(self, identifier):
        handler = {"mirror": self.mirror, "binarize": self.binarize, "clip": self.clip, "markDuplicate": self.markDuplicate,
                   "notDuplicate": self.notDuplicate, "getAtlases": self.getAtlases, "policy": self.choosePolicy}.get(identifier)
        if handler is not None:
            handler()

    def getAtlases(self):
        core().startAtlasJob()

    def choosePolicy(self):
        """The Slices chip and the ⋯ menu: the 2D policy card of the open study."""
        session = (self._context or {}).get("session") or _session()
        if session is None:
            _toast(_("Open or create a study to choose how 2D tracings are used"))
            return
        core().askPolicy(session, change=storedPolicy(session.study) is not None)

    def _candidate(self):
        context = self._context or {}
        sid = context.get("sid")
        candidates = core().duplicatesOf(sid) if sid else []
        return (context.get("session"), sid, candidates[0]) if candidates else (None, sid, None)

    def markDuplicate(self):
        session, sid, candidate = self._candidate()
        if session is None:
            return False
        previous = core().markDuplicate(session, sid, candidate[0], candidate[1])
        _toast(_("{sid} is excluded as a duplicate of {other}").format(sid=sid, other=candidate[0]), ms=5000, level="consequence",
               action=lambda: core().restoreDuplicate(session, sid, previous), actionText=_("Undo"))
        return True

    def notDuplicate(self):
        session, sid, candidate = self._candidate()
        if session is None:
            return False
        core().notDuplicate(session, sid, candidate[0], candidate[1])
        _toast(_("{sid} and {other} are not flagged again").format(sid=sid, other=candidate[0]), ms=3000)
        return True

    def _applyMask(self, lesion, newValues, newAffine, op, desc, valuesAfter, audit, toast):
        """Replace the lesion by ``newValues`` (a bool array [i, j, k] on ``newAffine``) as one undo step; record it in a study.

        Returns True when the lesion was changed.
        """
        draw = _draw()
        editor = draw.segmentEditor()
        if editor is None:
            return False
        session = self._sessionFor(lesion["node"])
        sid = session.current() if session is not None else None
        scene = (np.ascontiguousarray(lesion["values"].transpose(2, 1, 0)).astype(np.uint8), lesion["affine"])
        before = _digestOf(lesion["values"], lesion["affine"])
        originalPath = None
        if session is not None and sid:
            try:
                originalPath = core().keepOriginal(session, sid, desc, scene)
            except Exception:
                logging.error(f"Studio: the original lesion of {sid} could not be kept:\n{traceback.format_exc()}")
                _toast(_("The original lesion could not be kept: nothing was changed"), level="warning")
                return False
        editor.saveStateForUndo()
        kji = np.ascontiguousarray(np.asarray(newValues).transpose(2, 1, 0)).astype(np.uint8)
        draw._writeMask(lesion["node"], lesion["segmentId"], kji, newAffine, (0, kji.shape[2] - 1, 0, kji.shape[1] - 1, 0, kji.shape[0] - 1))
        after = self._digestNow()
        if session is not None and sid:
            row = session.study.subject(sid)
            valuesBefore = {key: (None if _missing(row.get(key)) else row.get(key)) for key in valuesAfter}
            record = {"event": "lesion_" + op, "sid": sid, "original": originalPath, **audit}
            core().commit(session, sid, valuesAfter, record)
            self._fixes.append({"op": op, "sid": sid, "before": before, "after": after, "valuesBefore": valuesBefore, "valuesAfter": dict(valuesAfter), "state": "applied"})
        _toast(toast, ms=5000, level="consequence", action=draw._undo, actionText=_("Undo"))
        self._digest = None
        self._pendingKey = None
        self._key = None
        self._timer.start()
        return True

    def _digestNow(self):
        lesion = self.lesion()
        return _digestOf(lesion["values"], lesion["affine"]) if lesion is not None else None

    def mirror(self):
        """Mirror L↔R: world x → -x onto the grid of the lesion (approximate), one undo step, ``flipped`` toggled (1 after the first mirror, 0 after the second), the original as desc-preflip."""
        from . import space

        lesion = self.lesion()
        if lesion is None or not (self._context or {}).get("mni"):
            _toast(_("Left and right are only known on an MNI template: the lesion was not mirrored"), level="warning")
            return False
        grid = space.gridOf(lesion["node"])
        if grid is None:
            _toast(_("The lesion has no reference grid: it was not mirrored"), level="warning")
            return False
        dims, reference = grid
        referenceInverse = np.linalg.inv(reference)
        toReference = referenceInverse @ lesion["affine"]
        offset = toReference[:3, 3]
        if not np.allclose(toReference[:3, :3], np.eye(3), atol=1e-3) or not np.allclose(offset, np.rint(offset), atol=1e-3):
            _toast(_("The lesion is not on the grid of its scan: it was not mirrored"), level="warning")
            return False
        offset = np.rint(offset).astype(int)
        values = lesion["values"]
        low, high = worldBox(values.shape, lesion["affine"])
        flip = np.diag([-1.0, 1.0, 1.0, 1.0])
        corners = np.array([[x, y, z, 1.0] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])
        mirrored = (referenceInverse @ flip @ corners.T)[:3].T
        regionLow = np.minimum(offset, np.floor(mirrored.min(axis=0)).astype(int)) - 2
        regionHigh = np.maximum(offset + np.array(values.shape) - 1, np.ceil(mirrored.max(axis=0)).astype(int)) + 2
        regionLow = np.maximum(regionLow, 0)
        regionHigh = np.minimum(regionHigh, np.array(dims) - 1)
        shape = tuple(int(n) for n in regionHigh - regionLow + 1)
        region = np.zeros(shape, dtype=np.float64)
        start = offset - regionLow
        stop = start + np.array(values.shape)
        if np.any(start < 0) or np.any(stop > np.array(shape)):
            _toast(_("The lesion reaches the edge of its grid: it was not mirrored"), level="warning")
            return False
        region[start[0]:stop[0], start[1]:stop[1], start[2]:stop[2]] = values
        shift = np.eye(4)
        shift[:3, 3] = regionLow
        regionAffine = reference @ shift
        ops = _lnm("lesionops")
        _fractional, binary, note = ops.mirror_world(region, regionAffine)
        if not binary.any():
            _toast(_("The mirrored lesion would fall outside the grid: it was not mirrored"), level="warning")
            return False
        voxelsBefore, voxelsAfter = int(np.count_nonzero(values)), int(np.count_nonzero(binary))
        audit = {"approximate": True, "note": note, "voxels_before": voxelsBefore, "voxels_after": voxelsAfter}
        return self._applyMask(lesion, binary > 0, regionAffine, "mirror", "preflip", {"flipped": 0 if str((self._context.get("row") or {}).get("flipped")) == "1" else 1}, audit,
                               _("Mirrored left-right (approximate). Retracing on the correct side is better for a figure whose sides were mixed up."))

    def binarize(self):
        """Binarize at 0.5: the lesion becomes the voxels of its saved file with a value of at least 0.5 (one undo step)."""
        context = self._context or {}
        lesion, session, sid = self.lesion(), context.get("session"), context.get("sid")
        if lesion is None or session is None or not sid:
            return False
        row = session.study.subject(sid)
        if _missing(row.get("lesion")) or not os.path.isfile(session.study.abspath(row["lesion"])):
            return False
        if session.isDirty(sid):
            _toast(_("Save the lesion first: Binarize reads the saved file"), level="warning")
            return False
        data, affine, _header = _lnm("nifti").read_nifti(session.study.abspath(row["lesion"]))
        data = data[..., 0] if data.ndim > 3 else data
        binary = np.asarray(data) >= 0.5
        audit = {"threshold": 0.5, "voxels_before": int(np.count_nonzero(lesion["values"])), "voxels_after": int(np.count_nonzero(binary))}
        return self._applyMask(lesion, binary, affine, "binarize", "prebinarize", {}, audit, _("Binarized at 0.5"))

    def clip(self):
        """Clip outside brain: remove the lesion voxels outside the brain mask of the template (one undo step)."""
        lesion = self.lesion()
        c = core()
        family = (self.result or {}).get("family")
        brain = c.resources.brain.get(family) if family else None
        if lesion is None or brain is None:
            _toast(_("No brain mask is known for this scan"))
            return False
        values, affine = lesion["values"], lesion["affine"]
        low, high = worldBox(values.shape, affine)
        cropped, cropAffine = cropVolume(brain[0], brain[1], low, high) or (None, None)
        if cropped is None:
            inside = np.zeros(values.shape, dtype=bool)
        else:
            geometry = _lnm("geometry")
            inside = geometry.resample_to_grid(cropped.astype(np.float64), cropAffine, geometry.Grid(values.shape, affine), mode="nearest") > 0.5
        clipped = values & inside
        removed = int(np.count_nonzero(values)) - int(np.count_nonzero(clipped))
        if removed == 0:
            _toast(_("Nothing is outside the brain mask"))
            return False
        if not clipped.any():
            _toast(_("The whole lesion is outside the brain mask: it was not clipped"), level="warning")
            return False
        audit = {"voxels_before": int(np.count_nonzero(values)), "voxels_after": int(np.count_nonzero(clipped)), "removed": removed}
        return self._applyMask(lesion, clipped, affine, "clip", "preclip", {}, audit, _("Clipped {count} voxels outside the brain").format(count=removed))

    # Undo of a fix in the segment editor

    def _followUndo(self, session, sid, digest):
        """When the editor undoes (or redoes) the last fix, the study values of the fix go back (or come again); the audit log says so."""
        if session is None or not sid or not self._fixes:
            return
        top = self._fixes[-1]
        if top["sid"] != sid:
            return
        c = core()
        if top["state"] == "applied" and digest == top["before"] and digest != top["after"]:
            top["state"] = "undone"
            c.commit(session, sid, top["valuesBefore"], {"event": "lesion_" + top["op"] + "_undone"})
        elif top["state"] == "undone" and digest == top["after"] and digest != top["before"]:
            top["state"] = "applied"
            c.commit(session, sid, top["valuesAfter"], {"event": "lesion_" + top["op"] + "_redone"})


def attach(card):
    """Add the QC chips and the study hooks to a Lesion card (called by ``LesionCard``). Return the ``LesionQc``."""
    installStudyHooks()
    return LesionQc(card)


#
# Search entries (W3-06 provider)
#


def markCurrentDuplicate():
    """Search: Mark duplicate (the current lesion against its first candidate)."""
    card = _draw().lesionCard()
    qc = getattr(card, "qc", None) if card is not None else None
    return qc.markDuplicate() if qc is not None else False


def _hasDuplicate():
    session = _session()
    sid = session.current() if session is not None else None
    return bool(sid and _core is not None and _core.duplicatesOf(sid))


def _canReapply():
    session = _session()
    return bool(session is not None and storedPolicy(session.study) is not None
                and any(row.get("dimensionality") in ("2D", "multi2D") for row in session.study.subjects))


def _reapplyWithToast():
    results = reapplyExtrusionPolicy()
    done = [item for item in results if item.get("mode")]
    skipped = [item for item in results if item.get("skipped")]
    text = _("Extrusion policy re-applied to {count} lesions").format(count=len(done))
    if skipped:
        text += " · " + _("{count} skipped").format(count=len(skipped))
    _toast(text, ms=4000)
    return results


def _askPolicy():
    session = _session()
    if session is not None:
        core().askPolicy(session, change=storedPolicy(session.study) is not None)


def _paletteEntries(shell=None):
    category = _("Draw")
    return [
        {"id": "lesionqc.markDuplicate", "title": _("Mark duplicate"), "category": category, "synonyms": ["duplicate", "same case", "reported twice", "exclude duplicate"],
         "callback": markCurrentDuplicate, "enabled": _hasDuplicate, "description": _("Exclude the lesion that overlaps another lesion of the study and record which one")},
        {"id": "lesionqc.reapplyExtrusion", "title": _("Re-apply extrusion policy"), "category": category, "synonyms": ["extrude", "extrusion", "2d lesions", "single slice", "thickness"],
         "callback": _reapplyWithToast, "enabled": _canReapply, "description": _("Regenerate the 2D lesions of the study from the tracings as drawn")},
        {"id": "lesionqc.extrusionPolicy", "title": _("2D extrusion policy…"), "category": category, "synonyms": ["extrude", "2d", "thickness", "single slice"],
         "callback": _askPolicy, "enabled": lambda: _session() is not None, "description": _("Choose how single-slice tracings are used in this study")},
    ]
