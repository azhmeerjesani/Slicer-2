"""Library extension: "Cohort diagnostics" and the "Lesion overview" of the STUDY card (L-31, LNM addendum 3.4, 7.2).

Before a study is mapped, the properties of its lesion set decide what the maps can say (van den Heuvel 2026: set size,
heterogeneity and pairwise overlap; Cotovio 2022: 2D and 3D tracings; Boes 2021). The card shows them from one run of
``LesionNetworkMappingLib.lesionset`` (numpy and scipy only):

- the lesions per group, and the ones that could not be used and why;
- the volume (median, IQR, skew; "log(volume) is suggested" above a skew of 2);
- the pairwise Dice histogram (a small bar plot), the most lesions covering one voxel and the possible duplicates;
- the tissue shares (gray matter, white matter, CSF) from the priors of the study's template, downloaded only when you click
  "Get tissue priors…" (TemplateFlow, a few MB, cached) and used from the cache afterwards;
- the hemisphere split, the 2D / 3D tally and the source types;
- the case/control balance as standardized mean differences, amber above 0.25, and "Create volume-matched control subset"
  (a named subset in ``study.json``, ``subsets[name]``; the ``include`` flags are never edited);
- flags (more than 50 % white matter, less than one gray-matter voxel) and literature context lines with their citations.

The run is in a worker thread (a numpy-only function; the main thread only reads the result) and the main thread is lent
to it by a timer, as the cohort analysis card does. Studies of more than ``CLI_MIN_LESIONS`` lesions run the ``lesionset``
command line through the job runner instead (Stop works the same). The per-subject values (hemisphere, dimensionality and,
with priors, the tissue fractions) go through the study inbox and are merged by the StudySession, the single writer of
participants.tsv; ``derivatives/lnm/lesionset.json`` keeps the last summary, which the card shows again when the study is
reopened.

**Lesion overview** writes the exact overlap count and percent maps (``derivatives/lesions``) and opens them in the slice views
with a discrete standard ``vtkMRMLColorTableNode`` (one entry per count, transparent at zero, saved with the scene; the nodes
carry ``Studio.Role=statmap``, so they are never an editing target), and exports the numbered lesion gallery (the ``gallery``
figure preset at 300 dpi), the overlap figure and ``supplementary_lesions.tsv``, whose case numbers are the gallery numbers.
The order is recorded in ``lesionset.json`` (``gallery``), so the numbers stay with the lesions. Nothing is written outside the
study folder, and no path in these files is absolute.

Search: "Check lesion set", "Lesion overview". Works in Classic (the Studio module panel hosts the same cards) and without a
shell. Tests may replace ``priorsFor`` (cached priors) and set ``allowDownloads``.
"""

import datetime
import json
import logging
import os
import queue
import shutil
import threading
import time
import traceback

import numpy as np
import qt
import slicer
from slicer.i18n import tr as _

from . import library, ui

#: Order of the card among the STUDY cards (after normalization, before the cohort analysis)
CARD_ORDER = 50
#: Studies with more lesions than this run the ``lesionset`` command line in the job runner
CLI_MIN_LESIONS = 500
#: The worker's results are handed over every PUMP_MS; the main thread sleeps PUMP_SLEEP_S in between so that the worker runs
PUMP_MS = 15
PUMP_SLEEP_S = 0.008
#: Downloads are refused under --testing unless a test sets this
allowDownloads = False
#: Template family of the studies that can download tissue priors, and the catalog entries of GM, WM and CSF
PRIOR_FAMILY = "2009"
PRIOR_SPACE = "MNI152NLin2009cAsym"
PRIOR_IDS = ("nlin2009casym-res02-gm-probseg", "nlin2009casym-res02-wm-probseg", "nlin2009casym-res02-csf-probseg")
#: Output names under ``derivatives/lesions``
GALLERY_NAME = "lesion_gallery"
OVERLAP_NAME = "lesion_overlap"
TABLE_NAME = "supplementary_lesions.tsv"
GALLERY_DPI = 300
GALLERY_PER_PAGE = 30
#: Display names of the overlap volumes and their colour tables
COUNT_NODE = "Lesion overlap (count)"
PERCENT_NODE = "Lesion overlap (percent)"
COUNT_LUT = "Lesion overlap count"
PERCENT_LUT = "Lesion overlap percent"
#: |SMD| above this is amber (lesionset.SMD_FLAG)
SMD_FLAG = 0.25
#: Height of the Dice histogram in pixels
PLOT_HEIGHT = 76
#: Names of the balance covariates in the table
COVARIATE_LABELS = {"volume_ml": "Volume (mL)", "wm_frac": "White matter share", "dim_2d": "2D tracings", "hemisphere_left": "Left hemisphere"}
SOURCE_LABELS = {"own_scan": "own scan", "case_report": "case report", "dataset": "dataset", "control_db": "control database", "unknown": "unknown"}


#
# Lazy access to the library
#


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _toast(text, ms=3000, **kwargs):
    shell = library._shell()
    if shell is not None:
        shell.toast(text, ms, **kwargs)
    else:
        logging.info(f"Studio cohort diagnostics: {text}")


def _rel(root, path):
    """``path`` relative to ``root`` with "/" separators (shared files hold no absolute path)."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def formatAge(isoText):
    """"2 h ago", "5 min ago" or "just now" of an ISO time ("" when it cannot be read)."""
    try:
        when = datetime.datetime.fromisoformat(str(isoText).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    seconds = (datetime.datetime.now(datetime.UTC) - when).total_seconds()
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return _("{n} min ago").format(n=int(seconds // 60))
    if seconds < 48 * 3600:
        return _("{n} h ago").format(n=int(seconds // 3600))
    return _("{n} days ago").format(n=int(seconds // 86400))


#
# Tissue priors (cached files of the fetch layer; downloaded only on a click)
#


def cacheFolder():
    """The fetch cache of the application (``<cache>/LNM``)."""
    return os.path.join(slicer.app.cachePath, "LNM")


def priorFamilyOf(study):
    """The template family of a study ("2009", "fsl6", ...)."""
    geometry = _lnm("geometry")
    return geometry.space_family(study.space or PRIOR_SPACE)


def priorsFor(study):
    """``{"gm", "wm", "csf", "space"}`` of the cached tissue priors of the study's template family, or None (nothing is downloaded)."""
    if priorFamilyOf(study) != PRIOR_FAMILY:
        return None
    fetch = _lnm("fetch")
    paths = []
    for resourceId in PRIOR_IDS:
        path = fetch.cached_path(fetch.find_resource(resourceId, ("templates",)), cacheFolder())
        if path is None:
            return None
        paths.append(path)
    return {"gm": paths[0], "wm": paths[1], "csf": paths[2], "space": PRIOR_SPACE}


def fetchPriorsWork(progress, cancel, cache=None):
    """The worker function of "Get tissue priors…": fetch the three 2 mm priors (verified by their sha256). Return their paths."""
    fetch = _lnm("fetch")
    cache = cache or cacheFolder()
    paths = []
    for index, resourceId in enumerate(PRIOR_IDS):
        progress("priors", index, len(PRIOR_IDS))
        paths.append(fetch.fetch(fetch.find_resource(resourceId, ("templates",)), cache, cancel=cancel))
    progress("priors", len(PRIOR_IDS), len(PRIOR_IDS))
    return paths


def _priorsFolder(study, priors):
    """A folder of the study's private cache with the three priors under their TemplateFlow names (for the command line)."""
    folder = os.path.join(study.path, ".studio", "cache", "priors")
    os.makedirs(folder, exist_ok=True)
    for tissue in ("gm", "wm", "csf"):
        target = os.path.join(folder, os.path.basename(priors[tissue]))
        if not os.path.isfile(target) or os.path.getsize(target) != os.path.getsize(priors[tissue]):
            shutil.copyfile(priors[tissue], target)
    return folder


#
# The work of the worker thread (numpy only; nothing of Qt, VTK or MRML)
#


def duplicatePairs(occ, dice, study=None):
    """``[[a, b, dice]]`` of the lesion pairs with a Dice of at least ``lesionops.DUPLICATE_DICE`` (not dismissed in the audit log), largest first."""
    lesionops = _lnm("lesionops")
    n = len(occ)
    if n < 2 or not len(dice.condensed):
        return []
    dismissed = set()
    if study is not None:
        try:
            from . import lesionqc

            dismissed = lesionqc.dismissedPairs(study)
        except Exception:
            logging.debug(f"Studio cohort diagnostics: the dismissed duplicates could not be read:\n{traceback.format_exc()}")
    first, second = np.triu_indices(n, 1)
    found = []
    for k in np.flatnonzero(dice.condensed >= lesionops.DUPLICATE_DICE):
        a, b = occ.sids[int(first[k])], occ.sids[int(second[k])]
        if frozenset((a, b)) not in dismissed:
            found.append([a, b, round(float(dice.condensed[k]), 3)])
    return sorted(found, key=lambda pair: -pair[2])


def galleryOrder(study, occ):
    """The participant ids of the gallery: the recorded order (or the table order) of the included lesions that were loaded."""
    export = _lnm("export")
    loaded = set(occ.sids)
    return [sid for sid in export.gallery_order(study) if sid in loaded]


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def checkLesionSet(studyPath, includeUnreviewed=False, priors=None, overview=False, template=None, progress=None, cancel=None):
    """Diagnostics of the lesion set of a study: ``lesionset`` loading, exact overlap, Dice, summary and ``lesionset.json``.

    :param includeUnreviewed: also use lesions that are not approved when the study requires review.
    :param priors: tissue priors (``priorsFor``), or None.
    :param overview: also write the overlap maps and export the numbered gallery, the overlap figure and the case table.
    :param template: display template of the figures (a NIfTI path), or None.
    :param progress: ``progress(stage, done, total)``; ``cancel()`` returns True to stop (``jobs.Cancelled``).
    :return: a dict: ``n``, ``summary`` (None when no lesion could be used), ``excluded``, ``duplicates``, ``review``, ``path`` of
      lesionset.json, ``inbox``, ``overview`` (paths of the maps, figures and table) and ``seconds``.
    """
    jobs, lesionset, figures, export = _lnm("jobs"), _lnm("lesionset"), _lnm("figures"), _lnm("export")
    Study = _lnm("study").Study
    started = time.monotonic()

    def note(stage, done=None, total=None):
        if progress is not None:
            progress(stage, done, total)

    def stop():
        if cancel is not None and cancel():
            raise jobs.Cancelled()

    study = Study.open(studyPath)
    included = [row for row in study.subjects if _lnm("study").is_included(row)]
    review = {"required": bool(study.review_required), "included": len(included), "approved": sum(1 for row in included if row.get("review_status") == "approved")}
    note("load", 0, len(included))
    occ = lesionset.load_occupancies(study, require_review=False if includeUnreviewed else None, cancel=cancel, progress=lambda done, total: note("load", done, total))
    result = {"n": len(occ), "excluded": occ.excluded, "summary": None, "duplicates": [], "review": review, "path": None, "inbox": None, "overview": None, "source": "thread"}
    if len(occ) == 0:
        result["seconds"] = round(time.monotonic() - started, 3)
        return result
    stop()
    note("overlap")
    maps = lesionset.overlap_maps(occ)
    stop()
    note("dice")
    dice = lesionset.pairwise_dice(occ)
    result["duplicates"] = duplicatePairs(occ, dice, study)
    stop()
    note("summary")
    summary = lesionset.summarize(occ, priors, maps=maps)
    old = _readJson(lesionset.lesionset_path(study)) or {}
    outputs = dict(old.get("outputs") or {})
    extra = {"gallery": old["gallery"]} if isinstance(old.get("gallery"), dict) else {}
    if overview:
        result["overview"] = overviewWork(study, occ, maps, template, note, stop, outputs, extra)
    path, inbox = lesionset.write_lesionset_json(study, summary, occ, post=True, outputs=outputs, extra=extra)
    result.update({"summary": summary, "path": path, "inbox": inbox, "excluded": summary["excluded"]})
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def overviewWork(study, occ, maps, template, note, stop, outputs, extra):
    """The files of the Lesion overview (see the module docstring). Return their paths; ``outputs`` and ``extra`` are updated for lesionset.json."""
    lesionset, figures, export = _lnm("lesionset"), _lnm("figures"), _lnm("export")
    out = os.path.join(study.path, "derivatives", "lesions")
    note("maps")
    countPath, percentPath = lesionset.write_overlap(study, maps, occ, out_dir=out)
    order = galleryOrder(study, occ)
    paths = [study.abspath(occ.rows[sid]["lesion"]) for sid in order]
    labels = [str(number) for number in range(1, len(order) + 1)]
    watermark = figures.watermark_for(study=study.path)
    stop()
    note("gallery")
    pages = figures.gallery(paths, template, labels, per_page=GALLERY_PER_PAGE, dpi=GALLERY_DPI, watermark=watermark)
    stop()
    note("figures")
    gallery = figures.write_figure(pages, out, GALLERY_NAME)
    overlapFigure = figures.overlap(count=countPath, n_lesions=len(occ), template=template, dpi=GALLERY_DPI, watermark=watermark)
    overlap = figures.write_figure(overlapFigure, out, OVERLAP_NAME)
    note("table")
    table = export.supplementary_lesions_tsv(study, os.path.join(out, TABLE_NAME), order=order)
    outputs.update({"overlap_count": _rel(study.path, countPath), "overlap_pct": _rel(study.path, percentPath), "gallery": _rel(study.path, gallery["dir"]),
                    "overlap_figure": _rel(study.path, overlap["dir"]), "supplementary_table": _rel(study.path, table)})
    extra["gallery"] = {"sids": order, "dir": _rel(study.path, gallery["dir"]), "table": _rel(study.path, table), "dpi": GALLERY_DPI, "pages": len(pages),
                        "created": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
    return {"count": countPath, "percent": percentPath, "gallery": gallery["dir"], "overlap": overlap["dir"], "table": table, "order": order,
            "pages": len(pages), "max_count": maps.max_count, "n": len(occ), "legend": maps.legend, "files": gallery["files"]}


def recordedResult(studyPath):
    """The last ``lesionset.json`` of a study as a result dict (``source`` "file"), or None."""
    lesionset = _lnm("lesionset")
    data = _readJson(os.path.join(studyPath, "derivatives", "lnm", lesionset.LESIONSET_JSON))
    if not data or data.get("format") != lesionset.FORMAT or not isinstance(data.get("summary"), dict):
        return None
    summary = data["summary"]
    return {"n": summary.get("n", 0), "excluded": summary.get("excluded") or {}, "summary": summary, "duplicates": [], "review": None,
            "path": os.path.join(studyPath, "derivatives", "lnm", lesionset.LESIONSET_JSON), "inbox": None, "overview": None, "source": "file",
            "created": data.get("created"), "gallery": data.get("gallery") if isinstance(data.get("gallery"), dict) else None}


def createMatchedSubset(session, summary, name=None, covariates=("volume_ml",), seed=0):
    """Store a covariate-matched control subset as ``study.json`` ``subsets[name]`` (the include flags are never edited).

    Each case gets the nearest control on the standardized covariates, without replacement (``lesionset.matched_subsample``).
    Returns ``{"name", "sids", "matched"}``, or None when there are no cases or no controls.
    """
    lesionset = _lnm("lesionset")
    covariateValues = lesionset.covariates_from_summary(summary)
    cases = {lesion["participant_id"]: covariateValues[lesion["participant_id"]] for lesion in summary["lesions"] if lesion["group"] == "case"}
    controls = {lesion["participant_id"]: covariateValues[lesion["participant_id"]] for lesion in summary["lesions"] if lesion["group"] == "control"}
    if not cases or not controls:
        return None
    matched = lesionset.matched_subsample(cases, controls, list(covariates), seed=seed)
    base = name or "volume_matched"
    existing = session.study.subsets
    name, suffix = base, 1
    while name in existing:
        suffix += 1
        name = f"{base}-{suffix}"
    lesionset.add_subset(session.study, name, matched["sids"])
    session.scheduleSave()
    session.flush()
    return {"name": name, "sids": matched["sids"], "matched": matched}


#
# Texts of the card (pure functions of a summary)
#


def groupsText(summary, blind=False):
    """"7 cases · 5 controls"; with ``blind`` (blind tracing, L-43) only the number of lesions, without the group split."""
    groups = (summary.get("groups") or {}) if not blind else {}
    parts = []
    for key, singular, plural in (("case", "case", "cases"), ("control", "control", "controls")):
        if key in groups:
            parts.append(f"{groups[key]} {_(singular if groups[key] == 1 else plural)}")
    others = sum(count for key, count in groups.items() if key not in ("case", "control"))
    if others:
        parts.append(_("{n} without a group").format(n=others))
    return " · ".join(parts) or (_("1 lesion") if summary.get("n", 0) == 1 else _("{n} lesions").format(n=summary.get("n", 0)))


def excludedText(excluded):
    """"3 not used: lesion file not found (2), empty lesion (1)" ("" when every lesion was used)."""
    total = sum(len(sids) for sids in excluded.values())
    if not total:
        return ""
    reasons = ", ".join(f"{reason} ({len(sids)})" for reason, sids in excluded.items())
    return _("{n} not used: {reasons}").format(n=total, reasons=reasons)


def _number(value, digits=1):
    return "—" if value is None else f"{value:.{digits}f}"


def volumeText(volumes):
    if not volumes or not volumes.get("n"):
        return ""
    text = _("median {median} mL · IQR {q1}\u2013{q3} · skew {skew}").format(median=_number(volumes["median"]), q1=_number(volumes["q1"]), q3=_number(volumes["q3"]), skew=_number(volumes["skew"]))
    if volumes.get("suggest_log"):
        text += "\n" + _("Skew above 2: log(volume) is suggested as the covariate.")
    return text


def diceText(summary):
    dice = summary.get("dice") or {}
    if dice.get("mean") is None:
        return _("one lesion: no pairs")
    overlap = summary.get("overlap") or {}
    text = _("mean {mean} · max {maximum}").format(mean=_number(dice["mean"], 2), maximum=_number(dice["max"], 2))
    if overlap.get("n"):
        text += " · " + _("most overlap {count} of {n} lesions ({pct}%)").format(count=overlap["max_count"], n=overlap["n"], pct=round(100.0 * overlap["max_count"] / overlap["n"]))
    return text


def tissueText(summary):
    tissue = summary.get("tissue")
    if not tissue:
        return ""
    mean = tissue["mean_pct"]
    return " · ".join(f"{label} {_number(mean[key], 0)}%" for key, label in (("gm", "GM"), ("wm", "WM"), ("csf", "CSF")))


def whereText(summary):
    """The hemisphere split and the 2D / 3D tally."""
    hemisphere = summary.get("hemisphere") or {}
    parts = [f"{_(label)} {hemisphere[key]}" for key, label in (("L", "Left"), ("R", "Right"), ("bilateral", "Bilateral")) if hemisphere.get(key)]
    dims = summary.get("dimensionality") or {}
    flat = dims.get("2D", 0) + dims.get("multi2D", 0)
    parts.append(f"2D {flat} · 3D {dims.get('3D', 0)}" + (f" · {_('unknown')} {dims['unknown']}" if dims.get("unknown") else ""))
    return " · ".join(parts)


def sourceText(summary):
    tally = summary.get("source_type") or {}
    return " · ".join(f"{_(SOURCE_LABELS.get(key, key))} {count}" for key, count in tally.items())


def balanceRows(summary):
    """``[(label, cases text, controls text, smd text, flagged)]`` of the balance table (None without cases and controls)."""
    balance = summary.get("balance")
    if not balance:
        return None
    rows = []
    for key, entry in balance.items():
        binary = entry.get("binary") or key == "wm_frac"  # shares are shown as percentages

        def show(value, binary=binary):
            if value is None:
                return "—"
            return f"{100 * value:.0f}%" if binary else f"{value:.1f}"

        smd = entry.get("smd")
        rows.append((_(COVARIATE_LABELS.get(key, key)), show(entry.get("case_mean")), show(entry.get("control_mean")), "—" if smd is None else f"{smd:+.2f}", bool(entry.get("flagged"))))
    return rows


def lesionFlags(summary):
    """``[(participant id, text)]``: more than 50 % white matter, or less than one gray-matter voxel (needs the tissue priors)."""
    if not summary.get("tissue"):
        return []
    found = []
    for lesion in summary.get("lesions") or []:
        wm, gm = lesion.get("wm_frac"), lesion.get("gm_frac")
        if wm is not None and wm > 0.5:
            found.append((lesion["participant_id"], _("{pct}% white matter").format(pct=round(100 * wm))))
        if gm is not None and (gm * (lesion.get("voxels") or 0)) < 1.0:
            found.append((lesion["participant_id"], _("less than one gray-matter voxel")))
    return found


def listText(items, limit=3):
    """"a, b, c +2" of the first ``limit`` items."""
    shown = ", ".join(items[:limit])
    return shown + (f" +{len(items) - limit}" if len(items) > limit else "")


#
# The Dice histogram (a small QImage bar plot)
#


def diceHistogramImage(counts, edges, width, height=PLOT_HEIGHT, ratio=1.0, flaggedFrom=None):
    """A bar plot of the pairwise Dice histogram as a ``QImage`` (``width`` x ``height`` px at ``ratio``).

    Bars grow with log(1 + count); bins from ``flaggedFrom`` (the duplicate Dice) on are amber. The axis shows 0, 0.5 and 1.
    """
    image = qt.QImage(max(1, int(width * ratio)), max(1, int(height * ratio)), qt.QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(qt.QColor(0, 0, 0, 0))
    painter = qt.QPainter(image)
    painter.setRenderHint(qt.QPainter.Antialiasing, True)
    font = qt.QFont(slicer.app.font())
    font.setPixelSize(ui.fontPx(10))
    painter.setFont(font)
    axisHeight = ui.fontPx(10) + 6
    left, right, top = 2.0, 2.0, 4.0
    plotBottom = height - axisHeight
    area = max(1.0, width - left - right)
    count = len(counts)
    peak = max([np.log1p(c) for c in counts] + [1e-9])
    barWidth = area / max(count, 1)
    normal, flagged, axis = ui.token("accent"), ui.token("warning"), ui.token("text3")
    for index, value in enumerate(counts):
        if not value:
            continue
        barHeight = (plotBottom - top) * float(np.log1p(value)) / peak
        isFlagged = flaggedFrom is not None and edges[index] >= flaggedFrom - 1e-9
        painter.setPen(qt.Qt.NoPen)
        painter.setBrush(qt.QBrush(flagged if isFlagged else normal))
        painter.drawRoundedRect(qt.QRectF(left + index * barWidth + 0.5, plotBottom - barHeight, max(1.0, barWidth - 1.0), barHeight), 1.5, 1.5)
    painter.setPen(qt.QPen(axis, 1.0))
    painter.drawLine(qt.QPointF(left, plotBottom + 0.5), qt.QPointF(left + area, plotBottom + 0.5))
    for fraction, label in ((0.0, "0"), (0.5, "0.5"), (1.0, "1")):
        x = left + area * fraction
        rect = qt.QRectF(x - 14, plotBottom + 2, 28, axisHeight - 2)
        if fraction == 0.0:
            rect = qt.QRectF(left, plotBottom + 2, 28, axisHeight - 2)
            flags = int(qt.Qt.AlignLeft | qt.Qt.AlignVCenter)
        elif fraction == 1.0:
            rect = qt.QRectF(left + area - 28, plotBottom + 2, 28, axisHeight - 2)
            flags = int(qt.Qt.AlignRight | qt.Qt.AlignVCenter)
        else:
            flags = int(qt.Qt.AlignHCenter | qt.Qt.AlignVCenter)
        painter.drawText(rect, flags, label)
    painter.end()
    return image


#
# The overlap maps in the views
#


def _nodesNamed(name, className):
    found = []
    collection = slicer.mrmlScene.GetNodesByClassByName(className, name)
    collection.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    for index in range(collection.GetNumberOfItems()):
        found.append(collection.GetItemAsObject(index))
    return found


def removeOverlapNodes():
    """Remove the overlap volumes of an earlier overview and their colour tables."""
    for name, className in ((COUNT_NODE, "vtkMRMLScalarVolumeNode"), (PERCENT_NODE, "vtkMRMLScalarVolumeNode"), (COUNT_LUT, "vtkMRMLColorTableNode"), (PERCENT_LUT, "vtkMRMLColorTableNode")):
        for node in _nodesNamed(name, className):
            slicer.mrmlScene.RemoveNode(node)


def overlapColorTable(name, entries, maximum):
    """A discrete standard colour table: entry 0 transparent, entry ``k`` the sequential colour of ``k / maximum`` (the figures' colours).

    It is a plain ``vtkMRMLColorTableNode`` of type User, saved with the scene, so official Slicer shows the same colours.
    """
    figures = _lnm("figures")
    node = slicer.vtkMRMLColorTableNode()
    node.SetName(name)
    node.SetTypeToUser()
    node.SetNumberOfColors(entries)
    node.SetSaveWithScene(True)
    modifying = node.StartModify()
    node.SetColor(0, "0", 0.0, 0.0, 0.0, 0.0)
    for k in range(1, entries):
        r, g, b = (float(v) for v in figures.count_color(k, maximum))
        node.SetColor(k, str(k), r, g, b, 1.0)
    node.EndModify(modifying)
    slicer.mrmlScene.AddNode(node)
    return node


def showOverlapInViews(overview, show=True):
    """Load the overlap count and percent maps, give them discrete colour tables and show the count in the slice views.

    :param overview: the ``overview`` of a result (``count``, ``percent``, ``max_count``).
    :return: ``{"count": volume node, "percent": volume node, "countTable": colour table, "peak": [R, A, S] of a voxel with the most lesions}``.
    """
    removeOverlapNodes()
    maxCount = max(int(overview["max_count"]), 1)
    nodes = {}
    for key, path, name, lutName, entries, maximum in (("count", overview["count"], COUNT_NODE, COUNT_LUT, maxCount + 1, maxCount),
                                                        ("percent", overview["percent"], PERCENT_NODE, PERCENT_LUT, 101, 100)):
        volume = slicer.util.loadVolume(path, {"name": name, "show": False})
        volume.SetAttribute("Studio.Role", "statmap")
        volume.CreateDefaultDisplayNodes()
        table = overlapColorTable(lutName, entries, maximum)
        display = volume.GetDisplayNode()
        display.SetAndObserveColorNodeID(table.GetID())
        display.SetAutoWindowLevel(False)
        display.SetWindowLevel(entries - 1, (entries - 1) / 2.0)
        display.SetInterpolate(False)
        nodes[key] = volume
        nodes[key + "Table"] = table
    nodes["peak"] = _peak(nodes["count"])
    if show:
        scene = slicer.mrmlScene
        background = None
        composite = scene.GetFirstNodeByClass("vtkMRMLSliceCompositeNode")
        if composite is not None:
            background = composite.GetBackgroundVolumeID()
        if background:
            slicer.util.setSliceViewerLayers(foreground=nodes["count"], foregroundOpacity=0.85)
        else:
            slicer.util.setSliceViewerLayers(background=nodes["count"], foreground=None, foregroundOpacity=0.85)
        if nodes["peak"] is not None:
            try:
                slicer.modules.markups.logic().JumpSlicesToLocation(*nodes["peak"], True)
            except Exception:
                logging.debug(f"Studio cohort diagnostics: the slices could not be moved to the overlap peak:\n{traceback.format_exc()}")
    return nodes


def viewsWorkspace():
    """The workspace that shows the slice views next to the overlap: Map when the build has it, else Draw."""
    try:
        from . import workspaces

        ws = workspaces.workspace("map")
        return "map" if ws is not None and workspaces.isAvailable(ws) else "draw"
    except Exception:
        return "draw"


def _peak(volume):
    """RAS of a voxel with the largest count (the first in array order), or None for an empty map."""
    import vtk

    array = slicer.util.arrayFromVolume(volume)
    if not array.size or int(array.max()) <= 0:
        return None
    k, j, i = np.unravel_index(int(np.argmax(array)), array.shape)
    matrix = vtk.vtkMatrix4x4()
    volume.GetIJKToRASMatrix(matrix)
    ras = matrix.MultiplyPoint([float(i), float(j), float(k), 1.0])
    return [ras[0], ras[1], ras[2]]


#
# The worker thread
#


class _Worker:
    """Runs one function in a thread and hands its result to ``callback(result, error)`` on the main thread.

    Python threads only run while the main thread runs Python: while a run is going, a 15 ms timer drains the result queue and
    sleeps a few milliseconds, so the work progresses without freezing the application (the pattern of the cohort analysis
    card). The function is called as ``function(progress=..., cancel=..., **kwargs)``.
    """

    def __init__(self, callback, onProgress=None):
        self.callback = callback
        self.onProgress = onProgress
        self._queue = queue.Queue()
        self._thread = None
        self._cancel = threading.Event()
        self.progress = None
        self._shown = None
        self._timer = qt.QTimer()
        self._timer.setInterval(PUMP_MS)
        self._timer.connect("timeout()", self._pump)

    def isBusy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, function, **kwargs):
        if self.isBusy():
            return False
        self._cancel.clear()
        self.progress, self._shown = None, None
        self._thread = threading.Thread(target=self._work, args=(function, kwargs), name="studio-cohort-diagnostics", daemon=True)
        self._thread.start()
        self._timer.start()
        return True

    def cancel(self):
        self._cancel.set()

    def _setProgress(self, stage, done=None, total=None):
        self.progress = (stage, done, total)

    def _work(self, function, kwargs):
        jobs = _lnm("jobs")
        try:
            self._queue.put((function(progress=self._setProgress, cancel=self._cancel.is_set, **kwargs), None))
        except jobs.Cancelled:
            self._queue.put((None, "cancelled"))
        except Exception as e:
            logging.debug(f"Studio cohort diagnostics: the run failed:\n{traceback.format_exc()}")
            self._queue.put((None, f"{type(e).__name__}: {e}"))

    def _pump(self):
        if self.onProgress is not None and self.progress != self._shown:
            self._shown = self.progress
            try:
                self.onProgress(self.progress)
            except Exception:
                logging.exception("Studio cohort diagnostics: the progress could not be shown")
        while True:
            try:
                result, error = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self.callback(result, error)
            except Exception:
                logging.exception("Studio cohort diagnostics: the result could not be shown")
        if self.isBusy():
            time.sleep(PUMP_SLEEP_S)  # lends the interpreter to the worker
        elif self._queue.empty():
            self._timer.stop()

    def waitForIdle(self, timeoutS=60.0):
        """Process events until the run has ended and its result is shown (tests)."""
        deadline = time.monotonic() + timeoutS
        while (self.isBusy() or not self._queue.empty()) and time.monotonic() < deadline:
            slicer.app.processEvents()
            time.sleep(0.005)
        self._pump()
        slicer.app.processEvents()
        return not self.isBusy()

    def stop(self):
        self._cancel.set()
        self._timer.stop()


#
# The card
#


class _ResizeWatcher(qt.QObject):
    """Calls ``callback()`` when the watched widget is resized."""

    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            qt.QTimer.singleShot(0, self.callback)
        return False


def _keyValue(key, objectName):
    """A row with a small key and a wrapped value: ``(row widget, value label)``."""
    row = qt.QWidget()
    row.setObjectName(objectName + "Row")
    layout = qt.QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    label = library._cardLabel(objectName + "Key", 11, "text2")
    label.setText(key)
    label.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Preferred)
    label.setFixedWidth(ui.fontPx(66))
    label.setAlignment(qt.Qt.AlignLeft | qt.Qt.AlignTop)
    layout.addWidget(label)
    value = library._cardLabel(objectName, 12, "text")
    value.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    layout.addWidget(value, 1)
    return row, value


class CohortCard:
    """The 'Cohort diagnostics' card of the STUDY section (CARD, see the module docstring)."""

    def __init__(self, session):
        self.session = session
        self.studyPath = None
        self.result = None
        self.includeUnreviewed = False
        self.cliJob = None
        self.matchedName = None
        self._wantOverview = False
        self._runPath = None  # the study the running (or last) check belongs to
        self._fetching = False
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioCohortCard")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)
        title = qt.QLabel(_("Cohort diagnostics"))
        title.setObjectName("StudioCohortTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 12, qt.QFont.DemiBold)
        layout.addWidget(title)
        self.statusLabel = library._cardLabel("StudioCohortStatus", 12, "text")
        layout.addWidget(self.statusLabel)
        self.includeCheck = qt.QCheckBox(_("Include lesions that are not approved yet"))
        self.includeCheck.setObjectName("StudioCohortIncludeUnreviewed")
        self.includeCheck.setToolTip(_("This study requires review: analyses use approved lesions only. Check the others too."))
        self.includeCheck.connect("toggled(bool)", self._onInclude)
        layout.addWidget(self.includeCheck)
        self.runRow, self.runButton = library._rowButton(_("Check the lesion set"), "StudioCohortRunButton", self.run, "secondary")
        self.runButton.setToolTip(ui.tooltip(_("Check the lesion set"), None, _("Overlap, Dice, volumes, tissue, balance and possible duplicates, before mapping")))
        layout.addWidget(self.runRow)
        progressRow = qt.QHBoxLayout()
        progressRow.setSpacing(8)
        self.progressLabel = library._cardLabel("StudioCohortProgress", 12, "accent")
        progressRow.addWidget(self.progressLabel, 1)
        self.stopLink = library._linkButton(_("Stop"), "StudioCohortStopButton", self.stop)
        progressRow.addWidget(self.stopLink)
        self.progressWidget = qt.QWidget()
        self.progressWidget.setLayout(progressRow)
        progressRow.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.progressWidget)
        self.emptyLabel = library._cardLabel("StudioCohortEmpty", 12, "warning")
        layout.addWidget(self.emptyLabel)
        self.checkAnywayLink = library._linkButton(_("Check them anyway"), "StudioCohortCheckAnyway", self.checkAnyway)
        layout.addWidget(self.checkAnywayLink, 0, qt.Qt.AlignLeft)
        # Results
        self.results = qt.QWidget()
        self.results.setObjectName("StudioCohortResults")
        resultLayout = qt.QVBoxLayout(self.results)
        resultLayout.setContentsMargins(0, 0, 0, 0)
        resultLayout.setSpacing(6)
        self.rows = {}
        for key, label in (("groups", _("Lesions")), ("volume", _("Volume")), ("dice", _("Dice")), ("tissue", _("Tissue")), ("where", _("Where")), ("source", _("Source")),
                           ("duplicates", _("Duplicates")), ("flags", _("Flags"))):
            row, value = _keyValue(label, "StudioCohort" + key.capitalize())
            self.rows[key] = (row, value)
            if key == "tissue":
                resultLayout.addWidget(row)
                self.priorsLink = library._linkButton(_("Get tissue priors…"), "StudioCohortPriorsButton", self.getPriors)
                self.priorsLink.setToolTip(_("Downloads the MNI152NLin2009cAsym 2 mm gray matter, white matter and CSF probability maps from TemplateFlow "
                                            "(about 3 MB, MNI/McGill permissive licence, checked by SHA-256) only now, and keeps them in the application cache"))
                resultLayout.addWidget(self.priorsLink, 0, qt.Qt.AlignLeft)
                continue
            if key == "dice":
                resultLayout.addWidget(row)
                self.diceImage = qt.QLabel()
                self.diceImage.setObjectName("StudioCohortDiceHistogram")
                self.diceImage.setFixedHeight(PLOT_HEIGHT)
                self.diceImage.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
                self._plotWidth = 0
                self._plotWatcher = _ResizeWatcher(self._onPlotResize)
                library._installEventFilter(self.diceImage, self._plotWatcher)
                resultLayout.addWidget(self.diceImage)
                continue
            resultLayout.addWidget(row)
        self.balanceTitle = library._cardLabel("StudioCohortBalanceTitle", 11, "text2")
        self.balanceTitle.setText(_("Case/control balance (standardized mean difference; amber above 0.25)"))
        resultLayout.addWidget(self.balanceTitle)
        self.balanceTable = qt.QTableWidget(0, 4)
        self.balanceTable.setObjectName("StudioCohortBalanceTable")
        self.balanceTable.setHorizontalHeaderLabels([_("Covariate"), _("Cases"), _("Controls"), _("SMD")])
        self.balanceTable.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.balanceTable.setSelectionMode(qt.QAbstractItemView.NoSelection)
        self.balanceTable.verticalHeader().setVisible(False)
        header = self.balanceTable.horizontalHeader()
        header.setSectionResizeMode(qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        self.balanceTable.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.balanceTable.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.balanceTable.setAccessibleName(_("Case/control balance"))
        resultLayout.addWidget(self.balanceTable)
        self.matchRow, self.matchButton = library._rowButton(_("Create volume-matched control subset"), "StudioCohortMatchButton", self.createSubset, "secondary")
        self.matchButton.setToolTip(_("Each case gets the control with the nearest volume (no replacement). Stored as a named subset in study.json; "
                                      "no lesion is excluded."))
        resultLayout.addWidget(self.matchRow)
        self.matchLabel = library._cardLabel("StudioCohortMatched", 12, "text")
        resultLayout.addWidget(self.matchLabel)
        self.contextLink = library._linkButton(_("Literature context ▸"), "StudioCohortContextButton", self._toggleContext)
        resultLayout.addWidget(self.contextLink, 0, qt.Qt.AlignLeft)
        self.contextLabel = library._cardLabel("StudioCohortContext", 11, "text2")
        self.contextLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        resultLayout.addWidget(self.contextLabel)
        layout.addWidget(self.results)
        # Overview
        self.overviewRow, self.overviewButton = library._rowButton(_("Lesion overview"), "StudioCohortOverviewButton", self.overview, "secondary")
        self.overviewButton.setToolTip(ui.tooltip(_("Lesion overview"), None, _("Exact overlap maps in the views, the numbered gallery and the case table, in the study folder")))
        layout.addWidget(self.overviewRow)
        self.overviewLabel = library._cardLabel("StudioCohortOverviewLabel", 12, "text2")
        layout.addWidget(self.overviewLabel)
        self.folderLink = library._linkButton(_("Show folder"), "StudioCohortFolderButton", self.showFolder)
        layout.addWidget(self.folderLink, 0, qt.Qt.AlignLeft)
        self.worker = _Worker(self._onResult, self._onProgress)
        self.fetcher = _Worker(self._onPriors, self._onProgress)
        self._lastOverview = None
        ui.addPaletteListener(self._onPalette)
        _cards.append(self)
        self._renderStatus()

    # State

    def dispose(self):
        self.worker.stop()
        self.fetcher.stop()
        ui.removePaletteListener(self._onPalette)
        if self in _cards:
            _cards.remove(self)

    def refresh(self, selectedIds=None):
        session = _session()
        if session is None:
            return
        self.session = session
        study = session.study
        if study.path != self.studyPath:
            self.studyPath = study.path
            self._runPath = None  # a result of the previous study that is still on its way is dropped
            self.worker.cancel()
            self.result = recordedResult(study.path)
            self.matchedName = None
            self.includeUnreviewed = False
            self._lastOverview = None
            self.includeCheck.setChecked(False)
            self.matchLabel.setText("")
            self.contextLabel.setVisible(False)
            self.contextLink.setText(_("Literature context ▸"))
        included = [row for row in study.subjects if _lnm("study").is_included(row)]
        pending = study.review_required and any(row.get("review_status") != "approved" for row in included)
        self.includeCheck.setVisible(bool(pending))
        self._renderStatus()

    def isBusy(self):
        return self.worker.isBusy() or (self.cliJob is not None and not self.cliJob.isFinished()) or self._fetching

    # Actions

    def run(self, overview=False):
        """Check the lesion set (in the worker thread, or the command line for a large study). Return True when a run started."""
        session = _session()
        if session is None or self.isBusy():
            return False
        self.session = session
        self.matchLabel.setText("")
        session.flush()
        study = session.study
        _importForWorker()
        priors = priorsFor(study)
        size = sum(1 for row in study.subjects if _lnm("study").is_included(row))
        self._wantOverview = bool(overview)
        self._runPath = study.path
        if size > CLI_MIN_LESIONS and not overview and not self.includeUnreviewed:
            return self._runCli(study, priors)
        template = self._template(study) if overview else None
        started = self.worker.start(checkLesionSet, studyPath=study.path, includeUnreviewed=self.includeUnreviewed, priors=priors, overview=bool(overview), template=template)
        self._renderStatus()
        return started

    def _runCli(self, study, priors):
        jobrunner = _jobrunner()
        if jobrunner is None:
            _toast(_("The job runner is not available"))
            return False
        args = ["--study", study.path]
        if priors:
            args += ["--priors", _priorsFolder(study, priors)]
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.lesionset", args, title=_("Checking lesions"), heavy=True, kind="lesionset", onDone=self._onCliDone, runRoot=os.path.join(study.path, ".studio"))
        self.cliJob = jobrunner.instance().submit(spec)
        self._renderStatus()
        return True

    def _onCliDone(self, job):
        session = _session()
        result = None
        if job.succeeded() and session is not None:
            result = recordedResult(session.study.path)
            if result is not None:
                result["source"] = "cli"
                result["created"] = None
                session.applyInbox()  # the command line posted the per-subject values to the study inbox
        self._onResult(result, None if result is not None else _("The command line did not finish ({state})").format(state=job.state))

    def stop(self):
        self.worker.cancel()
        self.fetcher.cancel()
        if self.cliJob is not None and not self.cliJob.isFinished():
            self.cliJob.stop()

    def checkAnyway(self):
        self.includeCheck.setChecked(True)
        return self.run(overview=self._wantOverview)

    def _onInclude(self, checked):
        self.includeUnreviewed = bool(checked)

    def overview(self):
        """Lesion overview: the overlap maps in the views, the numbered gallery and the case table. Return True when it started."""
        return self.run(overview=True)

    def _template(self, study):
        """The display template of the figures: the bundled 2009c brain for a 2009c study, else None (the lesions' own field of view)."""
        try:
            reportfigs, geometry = _lnm("reportfigs"), _lnm("geometry")
            path = reportfigs.find_template()
            space = reportfigs.template_space(path) if path else None
            if path and space and geometry.space_family(space) == geometry.space_family(study.space or PRIOR_SPACE):
                return path
        except Exception:
            logging.debug(f"Studio cohort diagnostics: no display template:\n{traceback.format_exc()}")
        return None

    def getPriors(self):
        """Download the tissue priors of the study's template family (only on this click), then check again. Return True when it started."""
        session = _session()
        if session is None or self.isBusy():
            return False
        if priorFamilyOf(session.study) != PRIOR_FAMILY:
            return False
        if slicer.app.testingEnabled() and not allowDownloads:
            self.statusLabel.setText(_("Downloads are disabled while testing."))
            return False
        self._fetching = True
        self.fetcher.start(fetchPriorsWork, cache=cacheFolder())
        self._renderStatus()
        return True

    def _onPriors(self, paths, error):
        self._fetching = False
        if error:
            self.priorsNote = error if error != "cancelled" else ""
            self.statusLabel.setText(_("The tissue priors were not downloaded: {error}").format(error=error) if error != "cancelled" else _("Download stopped."))
            self._renderStatus(keepStatus=True)
            return
        _toast(_("Tissue priors downloaded"))
        self.run(overview=self._wantOverview)

    def createSubset(self):
        """Create a volume-matched control subset in study.json. Return its name, or None."""
        session = _session()
        if session is None or not self.result or not self.result.get("summary") or "group" in library._hiddenColumns(session.study):
            return None
        made = createMatchedSubset(session, self.result["summary"])
        if made is None:
            return None
        self.matchedName = made["name"]
        matched = made["matched"]
        cases = [sid for sid in made["sids"] if sid in {lesion["participant_id"] for lesion in self.result["summary"]["lesions"] if lesion["group"] == "case"}]
        before, after = matched["smd_before"].get("volume_ml"), matched["smd_after"].get("volume_ml")
        text = _("Subset {name}: {cases} cases + {controls} controls").format(name=made["name"], cases=len(cases), controls=len(made["sids"]) - len(cases))
        if before is not None and after is not None:
            text += " · " + _("volume SMD {before} → {after}").format(before=f"{before:+.2f}", after=f"{after:+.2f}")
        if matched.get("unmatched_cases"):
            text += " · " + _("{n} cases without a control").format(n=len(matched["unmatched_cases"]))
        self.matchLabel.setText(text + ". " + _("Stored in study.json; no lesion was excluded."))
        return made["name"]

    def showFolder(self):
        session = _session()
        if session is not None:
            folder = os.path.join(session.study.path, "derivatives", "lesions")
            from . import projects

            projects.showInFileManager(folder if os.path.isdir(folder) else session.study.path)

    def _toggleContext(self):
        show = not self.contextLabel.isVisibleTo(self.widget)
        self.contextLabel.setVisible(show)
        self.contextLink.setText(_("Literature context ▾") if show else _("Literature context ▸"))

    # Results

    def _onProgress(self, progress):
        if progress is None:
            return
        stage, done, total = progress
        names = {"load": _("Reading lesions"), "overlap": _("Overlap"), "dice": _("Dice"), "summary": _("Summary"), "maps": _("Writing overlap maps"),
                 "gallery": _("Rendering the gallery"), "figures": _("Writing figures"), "table": _("Writing the case table"), "priors": _("Downloading tissue priors")}
        text = names.get(stage, stage)
        if done is not None and total:
            text += f" {done}/{total}"
        self.progressLabel.setText(text + "…")

    def _onResult(self, result, error):
        session = _session()
        if self._runPath is None or self._runPath != self.studyPath or session is None or session.study.path != self._runPath:
            return  # the open study changed while the check ran: its result does not belong to this study
        if error:
            self.result = None if error != "cancelled" else self.result
            self.statusLabel.setText(_("Stopped.") if error == "cancelled" else _("The check failed: {error}").format(error=error))
            self._renderStatus(keepStatus=True)
            return
        self.result = result
        if session is not None and result.get("inbox"):
            session.applyInbox()
        overview = result.get("overview")
        if overview:
            self._lastOverview = overview
            self._afterOverview(overview)
        self._renderStatus()

    def _afterOverview(self, overview):
        try:
            showOverlapInViews(overview)
        except Exception:
            logging.error(f"Studio cohort diagnostics: the overlap maps could not be shown:\n{traceback.format_exc()}")
            _toast(_("The overlap maps were written but could not be shown in the views"), 5000, level="warning")
            return
        shell = library._shell()
        action = actionText = None
        if shell is not None and shell.currentWorkspace() == "library":
            target = viewsWorkspace()
            action, actionText = (lambda: shell.switchTo(target)), _("Show in views")
        _toast(_("Lesion overview: {legend}. Gallery and table are in derivatives/lesions").format(legend=overview["legend"]), 7000, action=action, actionText=actionText)

    def _onPalette(self):
        if self.result and self.result.get("summary"):
            self._renderDice(self.result["summary"])

    # Rendering

    def _setRow(self, key, text):
        row, value = self.rows[key]
        value.setText(text)
        row.setVisible(bool(text))

    def _renderStatus(self, keepStatus=False):
        busy = self.isBusy()
        result = self.result
        self.runButton.setEnabled(not busy)
        self.overviewButton.setEnabled(not busy and self.session is not None)
        self.progressWidget.setVisible(busy)
        if not busy:
            self.progressLabel.setText("")
        self.emptyLabel.setVisible(False)
        self.checkAnywayLink.setVisible(False)
        self.results.setVisible(False)
        self.runButton.setText(_("Check again") if result and result.get("summary") else _("Check the lesion set"))
        if busy:
            if not keepStatus:
                self.statusLabel.setText(_("Checking…") if not self._fetching else _("Downloading…"))
        elif result is None:
            if not keepStatus:
                self.statusLabel.setText(_("Not checked yet. The lesion set decides what the network maps can say: size, overlap, volumes and balance."))
        elif not result.get("summary"):
            self.statusLabel.setText(_("Nothing to check."))
            reasons = excludedText(result.get("excluded") or {})
            review = result.get("review") or {}
            self.emptyLabel.setText(reasons or _("No lesion could be used."))
            self.emptyLabel.setVisible(True)
            self.checkAnywayLink.setVisible(bool(review.get("required") and not self.includeUnreviewed))
        else:
            summary = result["summary"]
            age = formatAge(result.get("created") or summary.get("created")) if result.get("source") == "file" else ""
            if not keepStatus:
                seconds = result.get("seconds")
                one = summary["n"] == 1
                if age:
                    status = (_("1 lesion · checked {age}") if one else _("{n} lesions · checked {age}")).format(n=summary["n"], age=age)
                elif seconds is not None:
                    status = (_("1 lesion · checked in {seconds} s") if one else _("{n} lesions · checked in {seconds} s")).format(n=summary["n"], seconds=f"{seconds:.1f}")
                else:
                    status = _("1 lesion") if one else _("{n} lesions").format(n=summary["n"])
                self.statusLabel.setText(status)
            self.results.setVisible(True)
            self._renderResults(result)
        gallery = (result or {}).get("gallery") if result else None
        overviewDone = bool(self._lastOverview) or bool(gallery)
        self.overviewLabel.setText(_("Overlap maps, gallery and case table are in derivatives/lesions.") if overviewDone else "")
        self.overviewLabel.setVisible(overviewDone)
        self.folderLink.setVisible(overviewDone)

    def _renderResults(self, result):
        summary = result["summary"]
        # Blind tracing (L-43): the case/control split and balance are not shown, as in the subject list counts
        blind = "group" in library._hiddenColumns(self.session.study) if self.session is not None else False
        groups = groupsText(summary, blind=blind)
        excluded = excludedText(result.get("excluded") or summary.get("excluded") or {})
        review = result.get("review") or {}
        if review.get("required") and self.includeUnreviewed and review.get("approved", 0) < review.get("included", 0):
            excluded = (excluded + "\n" if excluded else "") + _("Includes lesions that are not approved yet ({approved} of {included} are approved).").format(approved=review["approved"], included=review["included"])
        self._setRow("groups", groups + ("\n" + excluded if excluded else ""))
        self._setRow("volume", volumeText(summary.get("volumes")))
        self._setRow("dice", diceText(summary))
        self._renderDice(summary)
        tissue = tissueText(summary)
        study = self.session.study if self.session is not None else None
        offer = False
        note = tissue
        if not tissue:
            if summary.get("tissue_error"):
                note = summary["tissue_error"]
            elif study is not None and priorFamilyOf(study) == PRIOR_FAMILY:
                note = _("needs the template's tissue priors")
                offer = True
            else:
                note = _("not available for this template yet")
        self._setRow("tissue", note)
        self.priorsLink.setVisible(offer)
        self._setRow("where", whereText(summary))
        self._setRow("source", sourceText(summary))
        duplicates = result.get("duplicates") or []
        flagged = [f"{a} ~ {b} ({value:.2f})" for a, b, value in duplicates]
        if not flagged and study is not None:
            flagged = [row["participant_id"] for row in study.subjects if "duplicate" in str(row.get("qc_flags", "")).replace(",", ";").split(";")]
        self._setRow("duplicates", (listText(flagged) if flagged else _("none found")) if result.get("source") == "thread" or flagged else "")
        flags = lesionFlags(summary)
        self._setRow("flags", listText([f"{sid}: {text}" for sid, text in flags]) if flags else (_("none") if summary.get("tissue") else ""))
        rows = balanceRows(summary) if not blind else None
        self.balanceTitle.setVisible(bool(rows))
        self.balanceTable.setVisible(bool(rows))
        self.matchRow.setVisible(bool(rows))
        if rows:
            self._renderBalance(rows)
        self.matchButton.setEnabled(bool(rows))
        context = summary.get("literature") or []
        self.contextLabel.setText("\n".join(f"• {line['text']}" for line in context))
        self.contextLink.setVisible(bool(context))
        if not self.contextLabel.isVisibleTo(self.widget):
            self.contextLabel.setVisible(False)

    def _renderBalance(self, rows):
        table = self.balanceTable
        table.setRowCount(len(rows))
        for index, (label, cases, controls, smd, flagged) in enumerate(rows):
            for column, text in enumerate((label, cases, controls, ("! " if flagged else "") + smd)):
                item = qt.QTableWidgetItem(text)
                item.setFlags(qt.Qt.ItemIsEnabled)
                if column:
                    alignment = qt.Qt.AlignRight | qt.Qt.AlignVCenter
                    try:
                        item.setTextAlignment(alignment)
                    except (TypeError, ValueError):
                        item.setTextAlignment(int(alignment))
                if flagged and column == 3:
                    item.setForeground(qt.QBrush(ui.token("warning")))
                    font = qt.QFont(item.font())
                    font.setBold(True)
                    item.setFont(font)
                    item.setToolTip(_("The groups differ on this covariate (|SMD| above {limit})").format(limit=SMD_FLAG))
                table.setItem(index, column, item)
        rowHeight = max(24, ui.fontPx(24))
        for index in range(len(rows)):
            table.setRowHeight(index, rowHeight)
        table.setFixedHeight(table.horizontalHeader().height + rowHeight * len(rows) + 4)

    def _onPlotResize(self):
        """The plot follows the width of its label."""
        if library._isAlive(self.diceImage) and self.result and self.result.get("summary") and self.diceImage.width != self._plotWidth:
            self._renderDice(self.result["summary"])

    def _renderDice(self, summary):
        dice = summary.get("dice") or {}
        histogram = dice.get("histogram")
        has = bool(histogram and dice.get("n_pairs"))
        self.diceImage.setVisible(has)
        if not has:
            return
        lesionops = _lnm("lesionops")
        ratio = max(1.0, float(slicer.app.devicePixelRatio()))
        width = self._plotWidth = self.diceImage.width if self.diceImage.width > 120 else 290
        image = diceHistogramImage(histogram["counts"], histogram["edges"], width, PLOT_HEIGHT, ratio, flaggedFrom=lesionops.DUPLICATE_DICE)
        pixmap = qt.QPixmap.fromImage(image)
        pixmap.setDevicePixelRatio(ratio)
        self.diceImage.setPixmap(pixmap)
        description = _("Pairwise Dice of {pairs} lesion pairs: mean {mean}, maximum {maximum}. Bars use a log scale; amber bars are at or above {limit}, "
                        "possible duplicates.").format(pairs=dice["n_pairs"], mean=_number(dice["mean"], 2), maximum=_number(dice["max"], 2), limit=lesionops.DUPLICATE_DICE)
        self.diceImage.setToolTip(description)
        self.diceImage.setAccessibleName(description)


def _importForWorker():
    """Import on the main thread what the worker thread uses, so that no Qt-dependent module is first imported off the main thread."""
    import importlib

    for name in ("lesionset", "figures", "export", "jobs", "reportfigs"):
        try:
            _lnm(name)
        except Exception:
            logging.debug(f"Studio cohort diagnostics: {name} could not be imported:\n{traceback.format_exc()}")
    try:
        importlib.import_module("StudioLib.lesionqc")
    except Exception:
        logging.debug(f"Studio cohort diagnostics: lesionqc could not be imported:\n{traceback.format_exc()}")


def _jobrunner():
    import importlib

    try:
        return importlib.import_module("LesionNetworkMappingUI.jobrunner")
    except Exception:
        logging.debug(f"Studio cohort diagnostics: no job runner:\n{traceback.format_exc()}")
        return None


_cards = []  # live cards
_widgets = {}  # widget: CohortCard


def _createCard(session):
    card = CohortCard(session)
    _widgets[card.widget] = card
    card.widget.connect("destroyed()", lambda: (card.dispose(), _widgets.pop(card.widget, None)))
    card.refresh()
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _widgets.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


def liveCard():
    """The cohort diagnostics card of the open study (the Library inspector or the module panel), or None."""
    for card in list(_cards):
        if library._isAlive(card.widget):
            return card
    return None


def _withCard(function):
    """Run ``function(card)`` on the live card; Library is shown first when no card exists yet."""
    if _session() is None:
        _toast(_("Open a study first"))
        return False
    card = liveCard()
    if card is None:
        shell = library._shell()
        if shell is not None:
            shell.switchTo("library")
            slicer.app.processEvents()
        card = liveCard()
    if card is None:
        _toast(_("Show the study in Library first"))
        return False
    return function(card)


def checkLesionSetAction():
    """Search: Check lesion set."""
    return _withCard(lambda card: card.run())


def lesionOverviewAction():
    """Search: Lesion overview."""
    return _withCard(lambda card: card.overview())


#
# Library extension points
#

CARD = {"section": "study", "order": CARD_ORDER, "create": _createCard, "refresh": _refreshCard}


def _paletteEnabled():
    return _session() is not None


#: Search entries (dict form of the provider contract of StudioLib.palette)
PALETTE_ACTIONS = [
    {"id": "study.cohortDiagnostics", "title": "Check lesion set (cohort diagnostics)", "category": "Library", "synonyms": ["dice", "overlap", "smd", "balance", "duplicates", "tissue", "volume"],
     "callback": checkLesionSetAction, "enabled": _paletteEnabled, "description": "Overlap, Dice, volumes, tissue, case/control balance and duplicates of the study's lesions"},
    {"id": "study.lesionOverview", "title": "Lesion overview (overlap map and gallery)", "category": "Library", "synonyms": ["gallery", "overlap map", "supplementary table", "figure", "numbered"],
     "callback": lesionOverviewAction, "enabled": _paletteEnabled, "description": "Show the exact overlap maps and export the numbered gallery and the case table"},
]
