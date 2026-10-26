"""Library extension: the Flags column and the "2D" and "Has flags" filters of the subject list (L-28, LNM addendum 7.2).

The flags are the ``qc_flags`` of the participants table (``lesionops.QC_FLAGS``: side_mismatch, csf, nonbinary, empty, subvoxel,
outside, xform, grid, multipart, duplicate). They are written by the Draw intake checks (``StudioLib.lesionqc``) when a lesion
is saved or fixed and when the duplicate scan finds a pair, so the Classic Library list (the Studio module panel) and the Studio
Library show the same column. "2D" lists the single-slice and several-slice tracings (``dimensionality``).

Importing this module also installs the study hooks of ``lesionqc`` (the dimensionality Done hook and the duplicate scan), so
that they work in a study even when the Draw panel was never opened.
"""

import logging
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


def hasFlags(row):
    """The "Has flags" filter."""
    return bool(_flags(row))


def isTwoD(row):
    """The "2D" filter: a single-slice or several-single-slice tracing."""
    return row.get("dimensionality") in ("2D", "multi2D")


def flagsCell(row):
    """``(text, level, tooltip)`` of the Flags column: "Side, CSF +1", amber when there is a flag."""
    codes = _flags(row)
    if not codes:
        return "—", "muted", ""
    names = [_(FLAG_SHORT.get(code, code)) for code in codes]
    text = ", ".join(names) if len(names) <= 2 else f"{names[0]}, {names[1]} +{len(names) - 2}"
    try:
        from importlib import import_module

        explanations = import_module("LesionNetworkMappingLib.lesionops").QC_FLAGS
    except ImportError:
        explanations = {}
    tooltip = "\n".join(f"{_(FLAG_SHORT.get(code, code))}: {explanations.get(code, code)}" for code in codes)
    return text, "warn", tooltip


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
