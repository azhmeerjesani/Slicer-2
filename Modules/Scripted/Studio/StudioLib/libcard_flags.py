"""Library extension: the Flags column and the "2D" and "Has flags" filters of the subject list (L-28, LNM addendum 7.2).

The flags are the ``qc_flags`` of the participants table (``lesionops.QC_FLAGS``: side_mismatch, csf, nonbinary, empty, subvoxel,
outside, xform, grid, multipart, duplicate). They are written by the Draw intake checks (``StudioLib.lesionqc``) when a lesion
is saved or fixed and when the duplicate scan finds a pair, so the Classic Library list (the Studio module panel) and the Studio
Library show the same column. "2D" lists the single-slice and several-slice tracings (``dimensionality``).

Importing this module also installs the study hooks of ``lesionqc`` (the dimensionality Done hook and the duplicate scan), so
that they work in a study even when the Draw panel was never opened.
"""

import logging
import os
import time
import traceback

from slicer.i18n import tr as _

#: Short names of the flags in the column, and what they mean (``lesionops.QC_FLAGS`` has the full sentences)
FLAG_SHORT = {
    "side_mismatch": "Side",
    "csf": "CSF",
    "nonbinary": "Not binary",
    "empty": "Empty",
    "subvoxel": "Tiny",
    "outside": "Outside",
    "xform": "Header",
    "grid": "Grid",
    "multipart": "Parts",
    "duplicate": "Duplicate",
}


def _flags(row):
    """The qc_flags codes of a row ("n/a" and "none" are no flags)."""
    value = row.get("qc_flags")
    if value is None or str(value).strip().lower() in ("", "n/a", "none"):
        return []
    return [code for code in str(value).replace(",", ";").split(";") if code]


#: Pseudo flag (not stored in ``qc_flags``) of a row linked from a control set whose lesion file cannot be found: the set
#: is not registered on this computer (removed, or the study was opened on another computer), or its file was removed (BB-1)
MISSING_CONTROL = "missing_control"
MISSING_CONTROL_SHORT = "Not found"
_REGISTERED_TTL = 2.0  # seconds a "set is registered" answer is reused (the list asks once per row)
_registered = {}  # (registry folder, set name): (time, registered)


def missingControlLesion(row):
    """True for a row linked from a control set ("controlset:<name>/<rel>") whose lesion file cannot be found."""
    lesion = row.get("lesion")
    if row.get("source_type") != "control_db" or not isinstance(lesion, str) or not lesion.startswith("controlset:"):
        return False
    try:
        from importlib import import_module

        controlsets = import_module("LesionNetworkMappingLib.controlsets")
        root = controlsets.registry_root(getattr(import_module("LesionNetworkMappingLib.study").Study, "controlset_root", None))
        parsed = controlsets.parse_path(lesion)
        if parsed is None:
            return True
        key, now = (root, parsed[0]), time.monotonic()
        cached = _registered.get(key)
        if cached is None or now - cached[0] > _REGISTERED_TTL:
            try:
                controlsets.load(parsed[0], root)
                cached = (now, True)
            except (OSError, ValueError):
                cached = (now, False)
            _registered[key] = cached
        return not cached[1] or not os.path.isfile(controlsets.resolve_path(lesion, root))
    except Exception:
        logging.debug(f"Studio Library: a control lesion could not be resolved:\n{traceback.format_exc()}")
        return False


def hasFlags(row):
    """The "Has flags" filter (also a control lesion that cannot be found)."""
    return bool(_flags(row)) or missingControlLesion(row)


def isTwoD(row):
    """The "2D" filter: a single-slice or several-single-slice tracing."""
    return row.get("dimensionality") in ("2D", "multi2D")


def flagsCell(row):
    """``(text, level, tooltip)`` of the Flags column: "Side, CSF +1", amber when there is a flag."""
    codes = _flags(row)
    missing = missingControlLesion(row)
    if not codes and not missing:
        return "—", "muted", ""
    names = ([_(MISSING_CONTROL_SHORT)] if missing else []) + [_(FLAG_SHORT.get(code, code)) for code in codes]
    text = ", ".join(names) if len(names) <= 2 else f"{names[0]}, {names[1]} +{len(names) - 2}"
    try:
        from importlib import import_module

        explanations = import_module("LesionNetworkMappingLib.lesionops").QC_FLAGS
    except ImportError:
        explanations = {}
    lines = [f"{_(FLAG_SHORT.get(code, code))}: {explanations.get(code, code)}" for code in codes]
    if missing:
        lines.insert(0, _("{short}: the lesion file of this control row cannot be found (its control set is not registered on this "
                          "computer, or the file was removed). Unlink or register the set again in Control sets.").format(short=_(MISSING_CONTROL_SHORT)))
    return text, "warn", "\n".join(lines)


COLUMNS = [{"key": "qc_flags", "header": "Flags", "width": 110, "valueFn": flagsCell}]
FILTERS = [
    {"key": "two_d", "label": "2D", "predicate": isTwoD},
    {"key": "has_flags", "label": "Has flags", "predicate": hasFlags},
]

try:
    from . import lesionqc

    lesionqc.installStudyHooks()
except Exception:
    logging.error(f"Studio Library: the lesion intake hooks could not be installed:\n{traceback.format_exc()}")
