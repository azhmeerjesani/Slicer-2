"""Studies in Slicer Studio: the Lightroom-catalog layer of the Library (SPEC 3.1 "with a study", 5.5, 7.3; D15, D19).

A study is a plain folder (``LesionNetworkMappingLib.study``): ``study.json``, ``participants.tsv``, ``lesions/``,
``derivatives/`` and ``.studio/``. This module opens one study at a time and edits one subject at a time:

- ``createStudy(files)`` makes a study of a folder of NIfTI files (header-only scanning: no node is created). A folder
  that already has a ``participants.tsv`` is adopted in place: its columns and their order are kept, a backup is
  written once to ``.studio/participants.orig.tsv`` and the values of a group column ("patient", "HC", "1"...) are
  mapped to case / control / exclude (``groupMappingProposal()``); the original text is kept in ``group_original``.
- ``StudySession`` is ``shell.studySession`` (SPEC 2.10): ``study``, ``current()``, ``open(sid)``, ``next()``,
  ``previous()``, ``done()``, ``markDirty()`` and the extension points of the LNM addendum (``addDoneHook()``,
  ``addOpenHook()``, ``next(queue=…)``, ``flush()``, ``applyInbox()`` / ``mergeInbox()``, ``isDirty()``,
  ``saveStatus()``).
- Only the current subject is in the scene: opening a subject removes the previous subject's scan (never the shared
  study template, which is cached) and lesion, loads the next ones and edits the lesion in Draw. The undo history
  goes with the removed segmentation.
- **Done → next** writes the lesion with ``nifti.write_like`` on the grid it was traced on (the header of the file
  that defined the segmentation's grid: the lesion file, else the scan, else the template), to
  ``lesions/<sid>_space-<space>_desc-lesion_mask.nii.gz`` (a lesion already under ``lesions/`` is overwritten; one
  anywhere else, including ``derivatives/`` and the files of an adopted folder, is never modified: the row is
  repointed), with its BIDS sidecar, updates ``participants.tsv`` (status traced, volume, parts, gaps, centroid,
  trace time) and opens the next untraced subject. ``Alt+←/→`` write the same way without marking the subject traced.
  Leaving a subject never fails because of its lesion's content: an erased lesion writes nothing (its file is kept,
  a toast says so) and edits that cancel out (paint, then undo) leave the file and the row untouched.
- A subject switch pauses the rendering of the views (one render when it ends) instead of showing a wait cursor that
  processes events.
- Nothing is written outside the study folder; ``participants.tsv`` is written only on the main thread, through
  ``Study.save()`` (lock and merge). Jobs post to ``.studio/inbox``; the session merges the inbox when a study is
  opened, when Library is shown and on request. ``.studio/session.json`` holds a heartbeat (every 15 s) while the
  study is open.

The session works in Studio and in Classic (the Studio module panel lists the subjects there). Settings: only
``Studio/RecentStudies`` (a JSON list of the recently opened studies).
"""

import contextlib
import importlib
import json
import logging
import os
import re
import shutil
import socket
import time
import traceback

import numpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import space

RECENT_STUDIES_KEY = "Studio/RecentStudies"
MAX_RECENT_STUDIES = 10
#: .studio/session.json is refreshed this often while a study is open (A-04's ``pipeline init`` refuses a live one)
HEARTBEAT_INTERVAL_MS = 15000
SESSION_FILE = "session.json"
#: participants.tsv is saved this long after the last change (changes are kept in memory meanwhile)
SAVE_DELAY_MS = 400
#: How long a save waits for .studio/participants.lock on the main thread before it is retried from a timer
SAVE_LOCK_TIMEOUT_S = 0.5
SAVE_RETRY_MS = 2000
#: Tracing time stops counting after this long without an edit (SPEC 5.5)
IDLE_PAUSE_S = 120.0
#: Backup of an adopted participants table (written once)
ADOPT_BACKUP = "participants.orig.tsv"
ADOPT_DICTIONARY_BACKUP = "participants.orig.json"
#: Column that keeps the original text of a mapped "group" column
GROUP_ORIGINAL_COLUMN = "group_original"
#: Node attribute of the subject's nodes (a participant id, never a path)
SUBJECT_ATTRIBUTE = "Studio.StudySubject"
#: The default tracing template: the bundled MNI152NLin2009cAsym 2 mm brain (works offline)
DEFAULT_TEMPLATE_SAMPLE = "MNI152NLin2009cAsym 2mm brain"
TEMPLATE_1MM_SAMPLE = "MNI152NLin2009cAsym 1mm T1w"
NLIN6_1MM_SAMPLE = "MNI152NLin6Asym 1mm T1w (FSL terms)"
NLIN6_2MM_SAMPLE = "MNI152NLin6Asym 2mm T1w (FSL terms)"
#: Fork-only setting: the TemplateFlow transform that resamples MNI152NLin2009cAsym images onto MNI152NLin6Asym
WARP_TRANSFORM_KEY = "Studio/LNM/TransformNLin2009cToNLin6"
BRAIN_MASK_SAMPLE = "MNI152NLin2009cAsym 2mm brain mask"
#: Spaces of the connectome bundles that do not say theirs (GSP1000 is on the FSL MNI152 grid)
GSP_SPACE = "MNI152NLin6Asym"
#: Prefix of the template-only cases (case reports): sub-case001, sub-case002...
CASE_PREFIX = "case"

#: Words of group values (lowercase) and the study group they map to (L-01: patient/HC/1/0 → case/control)
GROUP_WORDS = {
    "case": ("case", "cases", "patient", "patients", "pat", "pt", "p", "stroke", "lesion", "lesioned", "affected", "symptomatic",
             "impaired", "deficit", "yes", "y", "true", "1"),
    "control": ("control", "controls", "ctrl", "ctl", "con", "hc", "healthy", "healthy control", "healthy_control", "healthycontrol",
                "nc", "normal", "unaffected", "asymptomatic", "no", "n", "false", "0"),
    "exclude": ("exclude", "excluded", "excl", "x", "drop", "dropped"),
}
#: Columns that may hold the group of a subject, in the order they are tried
GROUP_COLUMN_CANDIDATES = ("group", "diagnosis", "dx", "cohort", "condition", "patient", "is_patient", "case_control",
                           "casecontrol", "subject_type", "participant_type", "type", "arm")
GROUP_TARGETS = ("case", "control", "exclude")


class Blocking(Exception):
    """Raised by a Done hook (``StudySession.addDoneHook()``) to stop Done → next; the message is shown inline."""


#
# Library access
#

_available = None


def _lnm(name):
    return importlib.import_module("LesionNetworkMappingLib." + name)


def isAvailable():
    """True when the study library (``LesionNetworkMappingLib.study``) can be imported in this build."""
    global _available
    if _available is None:
        try:
            _lnm("study")
            _lnm("nifti")
            _available = True
        except Exception:
            logging.info(f"Studio: studies are not available in this build:\n{traceback.format_exc()}")
            _available = False
    return _available


def _currentShell(shell=None):
    if shell is not None:
        return shell if shell.isActive() else None
    from . import shell as shellModule

    return shellModule.instance()


def _importer():
    from . import importer

    return importer


def _draw():
    try:
        from . import draw
    except ImportError:
        return None
    return draw


@contextlib.contextmanager
def _busyCursor():
    """A wait cursor for the duration of the block, without processing events (``slicer.util.WaitCursor`` does)."""
    qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
    try:
        yield
    finally:
        qt.QApplication.restoreOverrideCursor()


@contextlib.contextmanager
def _renderPaused():
    """Pause the rendering of the views (``qSlicerApplication.pauseRender``) for the duration of the block."""
    pause = getattr(slicer.app, "pauseRender", None)
    if pause is None:
        yield
        return
    pause()
    try:
        yield
    finally:
        slicer.app.resumeRender()


def _replaceFile(source, destination):
    """``os.replace``, retried while another program holds the file (``autosave_io.replaceWithRetry``)."""
    try:
        from . import autosave_io
    except ImportError:
        os.replace(source, destination)
        return
    autosave_io.replaceWithRetry(source, destination)


def _commitDrawCardEdits():
    """Notes and the source reference still being typed in Draw's Lesion card reach the lesion before it is written."""
    draw = _draw()
    commit = getattr(draw, "_commitCardEdits", None) if draw is not None else None
    if commit is not None:
        try:
            commit()
        except Exception:
            logging.error(f"Studio: the Lesion card edits could not be committed:\n{traceback.format_exc()}")


def _toast(shell, text, **kwargs):
    shell = _currentShell(shell)
    if shell is not None:
        shell.toast(text, **kwargs)
    else:
        logging.info(f"Studio study: {text}")


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _now():
    return _lnm("study").now_iso()


def _isNifti(path):
    lower = str(path).lower()
    return lower.endswith(".nii") or lower.endswith(".nii.gz")


def _isInside(path, folder):
    try:
        relative = os.path.relpath(os.path.realpath(path), os.path.realpath(folder))
    except ValueError:
        return False
    return relative != ".." and not relative.startswith(".." + os.sep) and not os.path.isabs(relative)


#
# Group mapping (adopt in place, participants tables)
#


def groupTarget(value):
    """The study group of a group value: "case", "control", "exclude", or None when it is not recognized."""
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    if text in ("", "n/a", "na", "nan", "none", "-"):
        return None
    for target, words in GROUP_WORDS.items():
        if text in words:
            return target
    return None


def groupMappingProposal(columns, rows):
    """Propose how a table's group column maps to the study groups.

    :return: ``(column, {value: "case" | "control" | "exclude" | None})`` for the first column of
      ``GROUP_COLUMN_CANDIDATES`` (or any column) whose values are all recognized words, ``(column, mapping)`` with None
      targets for a "group" column with unknown values (the user chooses), or None when there is nothing to map (no
      group column, or a "group" column that already holds case / control / n/a only).
    """
    lowered = {str(column).lower(): column for column in columns if column != "participant_id"}

    def values(column):
        found = []
        for row in rows:
            value = str(row.get(column, "n/a")).strip()
            if value and value != "n/a" and value not in found:
                found.append(value)
        return found

    if "group" in lowered:
        groupValues = values(lowered["group"])
        if all(value in ("case", "control") for value in groupValues):
            return None
        return lowered["group"], {value: groupTarget(value) for value in groupValues}
    ordered = [lowered[name] for name in GROUP_COLUMN_CANDIDATES if name in lowered]
    ordered += [column for column in lowered.values() if column not in ordered]
    for column in ordered:
        columnValues = values(column)
        if not 2 <= len(columnValues) <= 6:
            continue
        targets = {value: groupTarget(value) for value in columnValues}
        if all(target in ("case", "control", "exclude") for target in targets.values()) and {"case", "control"} & set(targets.values()):
            if column.lower() not in GROUP_COLUMN_CANDIDATES and set(targets.values()) <= {"case", "control"} and all(
                    value in ("0", "1") for value in columnValues):
                continue  # a 0/1 column that is not named like a group (for example "include" or a symptom flag)
            return column, targets
    return None


def readTable(path):
    """(columns, rows) of a participants table (.tsv, or .csv with commas)."""
    study = _lnm("study")
    if str(path).lower().endswith(".csv"):
        import csv

        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            table = [row for row in reader if any(cell.strip() for cell in row)]
        if not table:
            return [], []
        columns = [name.strip() for name in table[0]]
        rows = [{column: (value.strip() or study.NA) for column, value in zip(columns, row + [""] * (len(columns) - len(row)), strict=False)}
                for row in table[1:]]
        return columns, rows
    return study.read_tsv(path)


def applyGroupMapping(rows, column, mapping):
    """Set the study group of ``rows`` (dicts) from ``column`` with ``mapping`` ({value: target}); return the changed count.

    "exclude" sets ``include`` to 0 (group n/a). The original text of a mapped "group" column is kept in
    ``group_original``; another source column is never changed.
    """
    changed = 0
    for row in rows:
        original = str(row.get(column, "n/a")).strip()
        target = mapping.get(original)
        group = target if target in ("case", "control") else "n/a"
        if column == "group" and original != group and GROUP_ORIGINAL_COLUMN not in row:
            row[GROUP_ORIGINAL_COLUMN] = original
        if target == "exclude":
            row["include"] = "0"
        if row.get("group") != group:
            row["group"] = group
            changed += 1
    return changed


#
# Templates and connectome space
#


def registeredConnectomeSpace():
    """The space of the first registered connectome bundle (``Studio/LNM/Connectomes`` → connectome.json ``space``;
    a GSP1000 bundle without one is MNI152NLin6Asym), else None.
    """
    value = qt.QSettings().value("Studio/LNM/Connectomes")
    paths = [value] if isinstance(value, str) else list(value or [])
    for path in paths:
        try:
            with open(os.path.join(str(path), "connectome.json"), encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        spaceName = manifest.get("space") if isinstance(manifest, dict) else None
        if spaceName:
            return str(spaceName)
        if "gsp" in str(manifest.get("name", "")).lower():
            return GSP_SPACE
    return None


def defaultTemplate():
    """study.json ``template`` of a new study: the 2009c 1 mm T1w when it was downloaded before, else the bundled 2 mm brain."""
    importer = _importer()
    if importer.cachedTemplatePath(TEMPLATE_1MM_SAMPLE) is not None:
        return {"id": "MNI152NLin2009cAsym", "res": 1, "source": "sampledata:" + TEMPLATE_1MM_SAMPLE}
    return {"id": "MNI152NLin2009cAsym", "res": 2, "source": "sampledata:" + DEFAULT_TEMPLATE_SAMPLE}


def defaultTracePolicy(template):
    """study.json ``trace_policy``: the connectome space (else the template's) at the template resolution."""
    return {"space": registeredConnectomeSpace() or template.get("id"), "res": template.get("res", 2), "convention_default": "radiological",
            "extrude_2d": None}


def templateMismatch(studyObject):
    """The amber line when the tracing template is not in the connectome space, else None."""
    template = studyObject.template or {}
    policy = studyObject.trace_policy
    target = policy.get("space")
    if not target or not template.get("id") or template.get("id") == target:
        return None
    return _("{template} differs from connectome space {space}: subcortical structures can shift by a few mm").format(
        template=template.get("id"), space=target)


def _softwareRecord():
    record = {"slicer": slicer.app.applicationVersion}
    revision = getattr(slicer.app, "repositoryRevision", "")
    if revision:
        record["fork_rev"] = str(revision)
    return record


#
# Recent studies (Studio/RecentStudies)
#


def recentStudies():
    """The recently opened studies, newest first: ``[{"path", "name", "time", "subject"}]`` (folders that still hold a study)."""
    value = slicer.util.settingsValue(RECENT_STUDIES_KEY, "")
    try:
        entries = json.loads(str(value)) if value else []
    except ValueError:
        entries = []
    if not isinstance(entries, list):
        return []
    study = _lnm("study") if isAvailable() else None
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("path") and (study is None or study.Study.is_study(entry["path"]))]


def recordRecentStudy(path, name, subject=None):
    path = os.path.abspath(str(path))
    entries = [entry for entry in recentStudies() if entry.get("path") != path]
    entries.insert(0, {"path": path, "name": str(name), "time": time.time(), "subject": subject})
    qt.QSettings().setValue(RECENT_STUDIES_KEY, json.dumps(entries[:MAX_RECENT_STUDIES]))


#
# Creating and adopting studies
#


def _newMetadata(studyModule, folder, name, template):
    return {
        "format": studyModule.STUDY_FORMAT,
        "version": studyModule.STUDY_VERSION,
        "name": name or os.path.basename(os.path.normpath(folder)),
        "created": studyModule.now_iso(),
        "template": template,
        "space": template.get("id") if template else None,
        "rules": dict(studyModule.DEFAULT_RULES),
        "connectomes": [],
        "software": _softwareRecord(),
        "trace_policy": defaultTracePolicy(template or {}),
    }


def adoptFolder(folder, name=None, groupColumn=None, groupMapping=None, template=None):
    """Make a study of a folder that has a participants.tsv (adopt in place) and return the opened ``Study``.

    The table's columns and their order are kept (the missing standard columns are appended); its rows keep their
    values, except the ids (normalized to "sub-<label>", the original in ``source_id``) and the group mapping
    (``applyGroupMapping()``). participants.tsv and participants.json are backed up once to ``.studio/``.
    """
    studyModule = _lnm("study")
    folder = os.path.abspath(folder)
    tsvPath = os.path.join(folder, studyModule.PARTICIPANTS_TSV)
    privateDir = os.path.join(folder, studyModule.PRIVATE_DIR)
    for subfolder in (studyModule.LESIONS_DIR, studyModule.DERIVATIVES_DIR, os.path.join(studyModule.PRIVATE_DIR, "thumbs"),
                      os.path.join(studyModule.PRIVATE_DIR, "journal")):
        os.makedirs(os.path.join(folder, subfolder), exist_ok=True)
    backup = os.path.join(privateDir, ADOPT_BACKUP)
    if not os.path.exists(backup):
        shutil.copy2(tsvPath, backup)
    dictionaryPath = os.path.join(folder, studyModule.PARTICIPANTS_JSON)
    dictionary = {}
    if os.path.isfile(dictionaryPath):
        dictionaryBackup = os.path.join(privateDir, ADOPT_DICTIONARY_BACKUP)
        if not os.path.exists(dictionaryBackup):
            shutil.copy2(dictionaryPath, dictionaryBackup)
        try:
            with open(dictionaryPath, encoding="utf-8-sig") as f:
                dictionary = json.load(f)
        except (OSError, ValueError):
            dictionary = {}
        if not isinstance(dictionary, dict):
            dictionary = {}
    columns, rows = studyModule.read_tsv(tsvPath)
    if "participant_id" not in columns:
        raise ValueError(_("{path} has no participant_id column").format(path=tsvPath))
    added = []
    seen = set()
    for row in rows:
        original = row["participant_id"]
        sid = studyModule.participant_label(original)
        if sid in seen:
            raise ValueError(_("{path}: {id} appears twice (after normalizing the ids to sub-<label>)").format(path=tsvPath, id=sid))
        seen.add(sid)
        if sid != original:
            row["participant_id"] = sid
            if studyModule.is_missing(row.get("source_id")):
                row["source_id"] = original
                if "source_id" not in columns and "source_id" not in added:
                    added.append("source_id")
    if groupColumn and groupMapping:
        applyGroupMapping(rows, groupColumn, groupMapping)
        if any(GROUP_ORIGINAL_COLUMN in row for row in rows) and GROUP_ORIGINAL_COLUMN not in columns:
            added.append(GROUP_ORIGINAL_COLUMN)
            for row in rows:
                row.setdefault(GROUP_ORIGINAL_COLUMN, studyModule.NA)
    ordered = ["participant_id"] + [column for column in columns if column != "participant_id"]
    ordered += [column for column in studyModule.STANDARD_COLUMNS if column not in ordered]
    ordered += [column for column in added if column not in ordered]
    template = template or defaultTemplate()
    studyObject = studyModule.Study(folder, _newMetadata(studyModule, folder, name, template), ordered, rows, dictionary)
    # The new study.json is written next to the user's table, without the merge of a later save (nothing else writes it yet)
    studyObject.save(merge=False)
    return studyModule.Study.open(folder)


def _commonFolder(files):
    folders = [os.path.dirname(os.path.abspath(path)) for path in files]
    if not folders:
        return None
    try:
        return os.path.commonpath(folders)
    except ValueError:
        return folders[0]


def createStudy(files, shell=None, folder=None, groupColumn=None, groupMapping=None, name=None, openIt=True):
    """Create a study of NIfTI files (a dropped folder, a loaded set) and open it; return the ``StudySession``.

    The study folder is ``folder``, else the folder that holds the files. A folder that already is a study is opened
    (and the files are added to it); a folder with a participants.tsv is adopted in place (``adoptFolder()``).
    Scans and lesions are paired by subject id from their file names, reading only their headers: no node is created.
    """
    if not isAvailable():
        raise RuntimeError(_("Studies need the lesion network mapping library, which is not part of this build"))
    studyModule = _lnm("study")
    files = [os.path.abspath(str(path)) for path in files or []]
    folder = os.path.abspath(folder or _commonFolder(files) or "")
    if not folder or not os.path.isdir(folder):
        raise ValueError(_("Choose a study folder"))
    # The study's own application files and network maps are never subjects
    excluded = [os.path.join(folder, studyModule.PRIVATE_DIR), os.path.join(folder, studyModule.DERIVATIVES_DIR, "lnm")]
    files = [path for path in files if not any(_isInside(path, other) for other in excluded)]
    startTime = time.perf_counter()
    if studyModule.Study.is_study(folder):
        studyObject = studyModule.Study.open(folder)
        how = "opened"
    elif os.path.isfile(os.path.join(folder, studyModule.PARTICIPANTS_TSV)):
        if groupColumn is None and groupMapping is None:
            columns, rows = studyModule.read_tsv(os.path.join(folder, studyModule.PARTICIPANTS_TSV))
            proposal = groupMappingProposal(columns, rows)
            if proposal is not None:
                groupColumn, groupMapping = proposal
        studyObject = adoptFolder(folder, name, groupColumn, groupMapping)
        how = "adopted"
    else:
        template = defaultTemplate()
        studyObject = studyModule.Study.create(folder, name, template=template, software=_softwareRecord())
        studyObject.trace_policy = defaultTracePolicy(template)
        how = "created"
    report = studyObject.add_files(files) if files else {"added": [], "updated": [], "skipped": [], "pairs": {}, "warnings": []}
    gridRows = _gridCandidates(studyObject)
    _applyGridAnswers(studyObject, gridRows)
    studyObject.save()
    elapsed = time.perf_counter() - startTime
    logging.info(f"Studio: study {studyObject.name} {how} in {elapsed * 1000:.0f} ms ({len(studyObject)} subjects, {len(files)} files)")
    if not openIt:
        return studyObject
    session = openStudy(studyObject, shell)
    session.creationReport = {"how": how, "seconds": elapsed, "report": report, "files": len(files)}
    session._gridRows = gridRows
    shell = _currentShell(shell)
    message = {
        "created": _("Study {name} created: {n} subjects"),
        "adopted": _("Study {name} created in place: {n} subjects (participants.tsv kept, backup in .studio)"),
        "opened": _("Study {name}: {n} subjects"),
    }[how].format(name=studyObject.name, n=len(studyObject))
    if report.get("skipped"):
        message += " · " + _("{n} files skipped").format(n=len(report["skipped"]))
    _toast(shell, message, ms=5000, action=lambda: qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(folder)), actionText=_("Show folder"),
           level="consequence")
    return session


def _gridCandidates(studyObject):
    """{grid id: {"candidates", "sids", "resolution"}} of the lesion (else scan) files on an ambiguous registry grid
    that ``grid_answers`` does not answer yet (headers only).
    """
    geometry = _lnm("geometry")
    nifti = _lnm("nifti")
    found = {}
    answers = studyObject.grid_answers
    for row in studyObject.subjects:
        path = studyObject.abspath(row.get("lesion")) or studyObject.abspath(row.get("scan"))
        if not path or not _isNifti(path) or not os.path.isfile(path):
            continue
        try:
            header = nifti.read_nifti_header(path)
            match = geometry.detect_grid_candidates(header["shape"][:3], header["affine"])
        except (OSError, ValueError):
            continue
        if match is None or not match.ambiguous:
            continue
        entry = found.setdefault(match.grid_id, {"candidates": list(match.candidates), "sids": [], "resolution": match.resolution,
                                                 "answered": match.grid_id in answers})
        entry["sids"].append(row["participant_id"])
    return found


def _applyGridAnswers(studyObject, gridRows):
    answers = studyObject.grid_answers
    for gridId, entry in gridRows.items():
        answer = answers.get(gridId)
        if not answer:
            continue
        for sid in entry["sids"]:
            if studyObject.subject(sid).get("space") != answer:
                studyObject.set(sid, space=answer)


#
# The current session
#

_session = None
_listeners = []


def session():
    """The open ``StudySession``, or None."""
    return _session


def addListener(callback):
    """``callback(session)`` after the study or its rows change (opened, closed, rows edited, subject opened)."""
    if callback not in _listeners:
        _listeners.append(callback)


def removeListener(callback):
    while callback in _listeners:
        _listeners.remove(callback)


def _notify(studySession=None):
    for callback in list(_listeners):
        try:
            callback(studySession if studySession is not None else _session)
        except Exception:
            logging.error(f"Studio: study listener failed:\n{traceback.format_exc()}")


def openStudy(pathOrStudy, shell=None, subject=None, showLibrary=True):
    """Open a study (a folder, its study.json or a ``Study``) as the current session and return it.

    The study that was open is closed first. In Studio, Library shows its subject list. With ``subject``, that subject
    is opened in Draw.
    """
    global _session
    studyModule = _lnm("study")
    studyObject = pathOrStudy if isinstance(pathOrStudy, studyModule.Study) else studyModule.Study.open(str(pathOrStudy))
    if _session is not None:
        if os.path.normcase(_session.study.path) == os.path.normcase(studyObject.path):
            # The study is already open: its session (with its in-memory changes) is kept
            if subject:
                _session.open(subject)
            return _session
        closeStudy()
    shell = _currentShell(shell)
    studySession = StudySession(studyObject, shell)
    _session = studySession
    from . import shell as shellModule

    live = shellModule._shell
    if live is not None:
        live.studySession = studySession
    # A study replaces the loaded set of U-07: its lesions are hidden and no longer stepped through
    importer = _importer()
    loaded = importer._loadedSet
    if loaded is not None:
        loaded.forget(hideLesions=True)
    recordRecentStudy(studyObject.path, studyObject.name)
    _registerWithAutosave()
    _connectQuit()
    studySession.start()
    _notify(studySession)
    if shell is not None and showLibrary and not subject:
        if shell.currentWorkspace() != "library":
            shell.switchTo("library")
        else:
            from . import library

            library.refreshStudy()
    if subject:
        studySession.open(subject)
    return studySession


_quitConnected = [False]


def _connectQuit():
    if not _quitConnected[0]:
        slicer.app.connect("aboutToQuit()", _onAboutToQuit)
        _quitConnected[0] = True


def _onAboutToQuit():
    """The application quits: the current subject's edits are written and the study is closed (its heartbeat removed)."""
    if _session is None:
        return
    try:
        closeStudy()
    except Exception:
        logging.error(f"Studio: the study could not be closed when the application quit:\n{traceback.format_exc()}")


def closeStudy():
    """Close the open study (the current subject's edits are written first). Return True if one was open."""
    global _session
    if _session is None:
        return False
    current, _session = _session, None
    current.close()
    from . import shell as shellModule

    live = shellModule._shell
    if live is not None and getattr(live, "studySession", None) is current:
        live.studySession = None
    _notify(None)
    return True


def openStudyDialog(shell=None):
    """Open study…: choose a study folder (or its study.json)."""
    if not isAvailable():
        _toast(shell, _("Studies are not available in this build"))
        return None
    start = (recentStudies() or [{}])[0].get("path") or _importer()._lastDirectory()
    folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Open study"), os.path.dirname(start) if start else "")
    if not folder:
        return None
    studyModule = _lnm("study")
    if not studyModule.Study.is_study(folder):
        slicer.util.errorDisplay(_("{folder} is not a study (it has no study.json). Use New study… or drop the folder to create one.").format(folder=folder))
        return None
    return openStudy(folder, shell)


def newStudyDialog(shell=None):
    """New study…: choose a folder; its NIfTI files (if any) become the subjects, its participants.tsv is adopted."""
    if not isAvailable():
        _toast(shell, _("Studies are not available in this build"))
        return None
    folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("New study: choose the study folder"), _importer()._lastDirectory())
    if not folder:
        return None
    niftiFiles, _isDicom = _importer().scanFolder(folder)
    studyModule = _lnm("study")
    privateDir = os.path.join(folder, studyModule.PRIVATE_DIR)
    niftiFiles = [path for path in niftiFiles if not _isInside(path, privateDir)]
    return createStudy(niftiFiles, shell, folder=folder)


def openParticipantsTable(path, shell=None):
    """A participants table was opened or dropped: imported into the open study, else offered as a new study."""
    shell = _currentShell(shell)
    columns, rows = readTable(path)
    shown = [column for column in columns if column != "participant_id"][:4]
    summary = _("{name}: {n} participants ({columns})").format(name=os.path.basename(path), n=len(rows), columns=", ".join(shown) or "participant_id")
    if _session is not None:
        result = _session.importTable(path)
        _toast(shell, summary + " · " + _("{added} added, {updated} updated in {study}").format(
            added=len(result["added"]), updated=len(result["updated"]), study=_session.study.name), ms=5000, level="consequence")
        return result
    folder = os.path.dirname(os.path.abspath(path))
    if shell is not None:
        def create():
            niftiFiles, _isDicom = _importer().scanFolder(folder)
            createStudy(niftiFiles, shell, folder=folder)

        shell.toast(summary, ms=8000, action=create, actionText=_("Create study"), level="consequence")
    return None


#
# Scene helpers
#


def _removeNode(nodeId):
    """Remove a node with its display and storage nodes."""
    scene = slicer.mrmlScene
    node = scene.GetNodeByID(nodeId) if nodeId else None
    if node is None:
        return
    related = [node.GetNthDisplayNode(index) for index in range(node.GetNumberOfDisplayNodes())] if hasattr(node, "GetNumberOfDisplayNodes") else []
    if hasattr(node, "GetStorageNode"):
        related.append(node.GetStorageNode())
    scene.RemoveNode(node)
    for other in related:
        if other is not None and scene.IsNodePresent(other):
            scene.RemoveNode(other)


def _storagePath(node):
    storage = node.GetStorageNode() if node is not None else None
    fileName = storage.GetFileName() if storage is not None else None
    return os.path.abspath(fileName) if fileName else None


def sampleFile(sampleName):
    """The local file of a SampleData template: bundled with the module, else downloaded before; None otherwise."""
    try:
        import SampleData

        source = SampleData.SampleDataLogic().sourceForSampleName(sampleName)
    except Exception:
        return None
    if source is None:
        return None
    for uri in source.uris:
        uri = str(uri)
        if uri.startswith("file://"):
            path = qt.QUrl(uri).toLocalFile()
            if os.path.isfile(path):
                return path
    return _importer().cachedTemplatePath(sampleName)


def _lesionSegmentIds(segmentationNode):
    segmentation = segmentationNode.GetSegmentation()
    return [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())]


def _internalArray(segmentationNode, segmentId):
    """``(mask_kji bool, ijkToRAS 4x4)`` of a segment's internal binary labelmap (its own extent), or None when empty."""
    labelmap = segmentationNode.GetBinaryLabelmapInternalRepresentation(segmentId)
    if labelmap is None or labelmap.IsEmpty() or labelmap.GetPointData().GetScalars() is None:
        return None
    segment = segmentationNode.GetSegmentation().GetSegment(segmentId)
    labelValue = segment.GetLabelValue() if segment is not None else 1
    array = slicer.util.arrayFromSegmentInternalBinaryLabelmap(segmentationNode, segmentId)
    imageToWorld = vtk.vtkMatrix4x4()
    labelmap.GetImageToWorldMatrix(imageToWorld)
    matrix = np.array([[imageToWorld.GetElement(row, column) for column in range(4)] for row in range(4)])
    extent = labelmap.GetExtent()
    offset = np.eye(4)
    offset[:3, 3] = (extent[0], extent[2], extent[4])
    return array == labelValue, matrix @ offset


def _cropped(mask, matrix):
    """Crop a mask to its non-zero bounding box: ``(crop, ijkToRAS of the crop)``, or None when empty."""
    occupied = [np.flatnonzero(mask.any(axis=axes)) for axes in ((1, 2), (0, 2), (0, 1))]
    if any(indices.size == 0 for indices in occupied):
        return None
    (k0, k1), (j0, j1), (i0, i1) = ((int(indices[0]), int(indices[-1])) for indices in occupied)
    offset = np.eye(4)
    offset[:3, 3] = (i0, j0, k0)
    return np.ascontiguousarray(mask[k0:k1 + 1, j0:j1 + 1, i0:i1 + 1]), matrix @ offset


def lesionMask(segmentationNode):
    """The lesion of a segmentation as ``(mask_kji uint8, ijkToRAS 4x4)``, cropped to its voxels, or None when empty.

    One segment: its internal labelmap. Several: their union on the segmentation's reference geometry.
    """
    segmentIds = _lesionSegmentIds(segmentationNode)
    if not segmentIds:
        return None
    if len(segmentIds) == 1:
        data = _internalArray(segmentationNode, segmentIds[0])
        if data is None:
            return None
        cropped = _cropped(*data)
        return None if cropped is None else (cropped[0].astype(np.uint8), cropped[1])
    scene = slicer.mrmlScene
    idArray = vtk.vtkStringArray()
    for segmentId in segmentIds:
        idArray.InsertNextValue(segmentId)
    nodeIdsBefore = {scene.GetNthNode(index).GetID() for index in range(scene.GetNumberOfNodes())}
    try:
        labelmapNode = scene.AddNewNodeByClass("vtkMRMLLabelMapVolumeNode", scene.GenerateUniqueName("StudioStudyExport"))
        labelmapNode.SetHideFromEditors(True)
        if not slicer.modules.segmentations.logic().ExportSegmentsToLabelmapNode(segmentationNode, idArray, labelmapNode, None,
                                                                                 slicer.vtkSegmentation.EXTENT_REFERENCE_GEOMETRY):
            raise RuntimeError(_("Failed to export the lesion"))
        array = slicer.util.arrayFromVolume(labelmapNode) > 0
        ijkToRas = vtk.vtkMatrix4x4()
        labelmapNode.GetIJKToRASMatrix(ijkToRas)
        matrix = np.array([[ijkToRas.GetElement(row, column) for column in range(4)] for row in range(4)])
    finally:
        for index in reversed(range(scene.GetNumberOfNodes())):
            node = scene.GetNthNode(index)
            if node is not None and node.GetID() not in nodeIdsBefore:
                scene.RemoveNode(node)
    cropped = _cropped(array, matrix)
    return None if cropped is None else (cropped[0].astype(np.uint8), cropped[1])


def _contentStamp(segmentationNode):
    if segmentationNode is None:
        return None
    segmentation = segmentationNode.GetSegmentation()
    newest = 0
    binaryName = slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName()
    for index in range(segmentation.GetNumberOfSegments()):
        representation = segmentation.GetNthSegment(index).GetRepresentation(binaryName)
        if representation is not None:
            newest = max(newest, representation.GetMTime())
    return segmentation.GetNumberOfSegments(), newest


#
# Lesion metrics (worker-safe: numpy only)
#

_brainMask = None


def brainMaskFile():
    """Path of the bundled MNI152NLin2009cAsym 2 mm brain mask, or None."""
    return sampleFile(BRAIN_MASK_SAMPLE)


def outsideBrainPercent(maskIndicesIjk, ijkToRas, brainMask):
    """Percentage of lesion voxels (an (n, 3) array of i, j, k indices) outside a brain mask ``(data_ijk, affine)``."""
    data, affine = brainMask
    if not len(maskIndicesIjk):
        return None
    points = np.c_[maskIndicesIjk, np.ones(len(maskIndicesIjk))] @ np.asarray(ijkToRas, float).T
    voxels = np.rint(points @ np.linalg.inv(affine).T)[:, :3].astype(int)
    inside = np.all((voxels >= 0) & (voxels < np.array(data.shape[:3])), axis=1)
    brain = np.zeros(len(voxels), bool)
    brain[inside] = data[voxels[inside, 0], voxels[inside, 1], voxels[inside, 2]] > 0
    return 100.0 * float(np.count_nonzero(~brain)) / len(voxels)


def metricsValues(maskKji, ijkToRas, lesionSpace=None, brainMask=None):
    """participants.tsv values of a lesion mask (worker-safe): volume_ml, n_parts, gaps, centroid, outside_brain_pct."""
    from . import lesioncard

    metrics = lesioncard.computeLesionMetrics(maskKji, None, ijkToRas)
    values = {"volume_ml": round(metrics["ml"], 6), "n_parts": metrics["parts"], "gaps": len(metrics["gaps"])}
    centroid = metrics["centroid_ras"] or (None, None, None)
    for axis, value in zip(("x", "y", "z"), centroid, strict=True):
        values["centroid_" + axis] = None if value is None else round(value, 2)
    outside = None
    if brainMask is not None and lesionSpace == "MNI152NLin2009cAsym" and metrics["voxels"]:
        kk, jj, ii = np.nonzero(maskKji)
        outside = outsideBrainPercent(np.c_[ii, jj, kk], ijkToRas, brainMask)
    values["outside_brain_pct"] = None if outside is None else round(outside, 2)
    values["_voxels"] = metrics["voxels"]
    values["_centroid"] = metrics["centroid_ras"]
    values["_spansBoth"] = metrics["spans_both"]
    return values


def _fileMetrics(path, lesionSpace, brainMask):
    """Metrics of a lesion file (worker thread): ``(values, mtime, size)``."""
    nifti = _lnm("nifti")
    stat = os.stat(path)
    data, affine, _header = nifti.read_nifti(path, apply_scaling=False)
    if data.ndim > 3:
        data = data[..., 0]
    values = metricsValues(np.ascontiguousarray(np.transpose(data != 0, (2, 1, 0))), affine, lesionSpace, brainMask)
    return values, stat.st_mtime, stat.st_size


def _loadBrainMask():
    global _brainMask
    if _brainMask is None:
        path = brainMaskFile()
        if path is None:
            _brainMask = False
        else:
            try:
                data, affine, _header = _lnm("nifti").read_nifti(path)
                _brainMask = (data, affine)
            except Exception:
                _brainMask = False
    return _brainMask or None


#
# StudySession
#


class _SegmentObserver:
    """Calls ``callback`` when a segment of the observed segmentation changes."""

    def __init__(self, callback):
        self.callback = callback
        self.segmentation = None
        self.tags = []

    def observe(self, segmentationNode):
        self.dispose()
        if segmentationNode is None:
            return
        self.segmentation = segmentationNode.GetSegmentation()
        for event in (slicer.vtkSegmentation.SegmentModified, slicer.vtkSegmentation.SegmentAdded, slicer.vtkSegmentation.SegmentRemoved,
                      slicer.vtkSegmentation.RepresentationModified):
            self.tags.append(self.segmentation.AddObserver(event, self._onModified))

    def _onModified(self, caller, event):
        self.callback()

    def dispose(self):
        if self.segmentation is not None:
            for tag in self.tags:
                self.segmentation.RemoveObserver(tag)
        self.segmentation = None
        self.tags = []


class StudySession:
    """The open study and its current subject (``shell.studySession``, SPEC 2.10; see the module docstring)."""

    def __init__(self, studyObject, shell=None):
        self.study = studyObject
        self._shell = shell
        self._current = None
        self._nodeIds = {"scan": None, "segmentation": None}
        self._templateCache = {}  # source text -> volume node ID (the shared study template)
        self._templateFiles = {}  # template volume node ID -> its NIfTI file
        self._gridReference = None  # the file whose header the lesion is written with
        self._openStamp = None
        self._dirty = False
        self._markedDirty = False  # markDirty(): an edit that the lesion's voxels do not show (always written)
        self._activeSeconds = 0.0
        self._lastActivity = None
        self._doneHooks = []  # [(order, sequence, fn)]
        self._openHooks = []
        self._swallowNext = False
        self._lastSaveTime = None
        self._lastSaveError = None
        self._removed = []  # undo records of "Remove from study"
        self.creationReport = None
        self._gridRows = {}
        self._metricsWriter = None
        self._metricsPending = set()
        self._closed = False
        self._observer = _SegmentObserver(self._onSegmentModified)
        self._journal = None
        self._sceneCloseTag = None
        self._sceneClosingTag = None
        self._saveTimer = qt.QTimer()
        self._saveTimer.setSingleShot(True)
        self._saveTimer.setInterval(SAVE_DELAY_MS)
        self._saveTimer.connect("timeout()", self._onSaveTimeout)
        self._heartbeatTimer = qt.QTimer()
        self._heartbeatTimer.setInterval(HEARTBEAT_INTERVAL_MS)
        self._heartbeatTimer.connect("timeout()", self._writeHeartbeat)
        self._startedAt = _now()

    # Identity (A-01b records these in every autosave slot)

    @property
    def path(self):
        return self.study.path

    @property
    def root(self):
        return self.study.path

    @property
    def name(self):
        return self.study.name

    def current(self):
        """participant_id of the subject being edited, None if none."""
        return self._current

    def __repr__(self):
        return f"StudySession({self.study.name!r}, current={self._current!r})"

    # Lifecycle

    def start(self):
        """Start the heartbeat, merge the inbox, ask the pending grid questions and compute the missing lesion metrics."""
        self._writeHeartbeat()
        self._heartbeatTimer.start()
        self._sceneCloseTag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.EndCloseEvent, self._onSceneClosed)
        self._sceneClosingTag = slicer.mrmlScene.AddObserver(slicer.mrmlScene.StartCloseEvent, self._onSceneClosing)
        if not self._gridRows:
            try:
                self._gridRows = _gridCandidates(self.study)
            except Exception:
                logging.error(f"Studio: the grids of the study could not be checked:\n{traceback.format_exc()}")
        try:
            from . import journal

            self._journal = journal.Journal(self)
        except ImportError:
            self._journal = None
        self.applyInbox()
        self.computeMissingMetrics()

    def close(self):
        """Write the current subject's edits, save participants.tsv, remove the session's nodes and the heartbeat."""
        if self._closed:
            return
        try:
            if self._current is not None:
                self._leave(write=True)
        except Exception:
            logging.error(f"Studio: the current subject could not be written when the study closed:\n{traceback.format_exc()}")
        self._removeSubjectNodes()
        for nodeId in list(self._templateCache.values()):
            node = slicer.mrmlScene.GetNodeByID(nodeId)
            if node is not None and node.GetAttribute(SUBJECT_ATTRIBUTE) == "template":
                _removeNode(nodeId)
        self._templateCache.clear()
        self.flush()
        self._closed = True
        self._saveTimer.stop()
        self._heartbeatTimer.stop()
        for tag in (self._sceneCloseTag, self._sceneClosingTag):
            if tag is not None:
                slicer.mrmlScene.RemoveObserver(tag)
        self._sceneCloseTag = self._sceneClosingTag = None
        self._observer.dispose()
        if self._journal is not None:
            self._journal.dispose()
            self._journal = None
        if self._metricsWriter is not None:
            self._metricsWriter.shutdown(0.5)
            self._metricsWriter = None
        with contextlib.suppress(OSError):
            os.remove(self._sessionFile())
        shell = _currentShell(self._shell)
        if shell is not None:
            shell.setBreadcrumb(None)
            shell.clearInspectorNotice("study-journal")

    def _onSceneClosing(self, caller=None, event=None):
        """The scene is about to be closed: the current subject's edits are written first (a study has no Save)."""
        try:
            self._leave(write=True)
        except Exception:
            logging.error(f"Studio: the current subject could not be written before the scene closed:\n{traceback.format_exc()}")

    def _onSceneClosed(self, caller=None, event=None):
        """The scene was closed: the subject's nodes are gone (the study stays open)."""
        self._observer.dispose()
        if self._journal is not None:
            self._journal.detach()
        self._current = None
        self._nodeIds = {"scan": None, "segmentation": None}
        self._templateCache.clear()
        self._templateFiles.clear()
        self._dirty = False
        shell = _currentShell(self._shell)
        if shell is not None:
            shell.setBreadcrumb(None)
        _notify(self)

    # Heartbeat (.studio/session.json)

    def _sessionFile(self):
        studyModule = _lnm("study")
        return os.path.join(self.study.path, studyModule.PRIVATE_DIR, SESSION_FILE)

    def _writeHeartbeat(self):
        if self._closed:
            return
        content = {"format": "slicer-studio-session", "pid": os.getpid(), "host": socket.gethostname(), "app": "Slicer Studio",
                   "started": self._startedAt, "heartbeat": _now(), "heartbeat_epoch": time.time(), "subject": self._current}
        try:
            path = self._sessionFile()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _lnm("schema").write_text_atomic(path, json.dumps(content) + "\n")
        except OSError:
            logging.warning(f"Studio: cannot write the study heartbeat:\n{traceback.format_exc()}")

    # Saving participants.tsv (main thread only)

    def scheduleSave(self):
        """Save participants.tsv soon (changes made with ``Study.set`` are kept in memory meanwhile)."""
        if not self._closed:
            self._saveTimer.start()

    def _onSaveTimeout(self):
        self.flush(lockTimeout=SAVE_LOCK_TIMEOUT_S)

    def flush(self, lockTimeout=2.0):
        """Save pending participants.tsv changes now (main thread). Return True if saved."""
        self._saveTimer.stop()
        jobs = _lnm("jobs")
        try:
            self.study.save(lock_timeout=lockTimeout)
        except jobs.Locked:
            logging.info("Studio: participants.tsv is locked by another program, the save is retried")
            if not self._closed:
                self._saveTimer.start(SAVE_RETRY_MS)
            return False
        except Exception as e:
            self._lastSaveError = str(e)
            logging.error(f"Studio: cannot save the study {self.study.path}:\n{traceback.format_exc()}")
            _toast(self._shell, _("The study could not be saved: {error}").format(error=e), ms=6000, level="warning")
            return False
        self._lastSaveTime = time.time()
        self._lastSaveError = None
        return True

    def saveStatus(self):
        """``(text, timestamp)`` for the save status of the top bar (A-01b)."""
        if self._lastSaveError:
            return _("Study not saved"), self._lastSaveTime
        if self._saveTimer.isActive():
            return _("Saving…"), self._lastSaveTime
        journalTime = self._journal.lastWriteTime() if self._journal is not None else None
        if self._dirty and journalTime is not None:
            return _("Journal saved"), journalTime
        return _("All changes saved"), self._lastSaveTime

    def applyInbox(self):
        """Merge the values that jobs posted to .studio/inbox (on open, when Library is shown, on request). Return the files applied."""
        jobs = _lnm("jobs")
        try:
            applied = self.study.apply_inbox(save=False)
        except Exception:
            logging.error(f"Studio: the study inbox could not be merged:\n{traceback.format_exc()}")
            return []
        if applied:
            try:
                self.study.save(lock_timeout=SAVE_LOCK_TIMEOUT_S)
                self._lastSaveTime = time.time()
            except jobs.Locked:
                self.scheduleSave()
            _notify(self)
        return applied

    mergeInbox = applyInbox

    # Rows

    def setValues(self, sids, values):
        """Set cells of several subjects (validated, see ``Study.set``) and save soon."""
        for sid in sids:
            self.study.set(sid, values)
        self.scheduleSave()
        _notify(self)

    def setQc(self, sids, value, column="qc"):
        """P / X / U: ``value`` in pass, fail, unreviewed (``column``: qc, or a filter's qcColumn such as norm_qc)."""
        self.setValues(sids, {column: value})

    def importTable(self, path, groupColumn=None, groupMapping=None):
        """Merge a participants table into the study (``Study.import_tsv``) with the group mapping. Return its report."""
        columns, rows = readTable(path)
        if groupColumn is None:
            proposal = groupMappingProposal(columns, rows)
            if proposal is not None:
                groupColumn, groupMapping = proposal
        if groupColumn and groupMapping:
            applyGroupMapping(rows, groupColumn, groupMapping)
            columns = list(columns) + [column for column in ("group", "include", GROUP_ORIGINAL_COLUMN) if column not in columns and any(column in row for row in rows)]
        studyModule = _lnm("study")
        temporary = os.path.join(self.study.path, studyModule.PRIVATE_DIR, f"import-{os.getpid()}.tsv")
        studyModule.write_tsv(temporary, columns, rows)
        try:
            result = self.study.import_tsv(temporary)
        finally:
            with contextlib.suppress(OSError):
                os.remove(temporary)
        self.flush()
        _notify(self)
        return result

    def addFiles(self, paths):
        """Add scans and lesions to the study (header-only); return the ``Study.add_files`` report."""
        report = self.study.add_files(paths)
        gridRows = _gridCandidates(self.study)
        _applyGridAnswers(self.study, gridRows)
        self._gridRows = gridRows
        self.flush()
        self.computeMissingMetrics()
        _notify(self)
        return report

    def removeSubjects(self, sids):
        """Remove subjects from the study (their files are kept). Undo with ``undoRemove()``. Return the number removed."""
        rows = self.study.subjects
        order = [row["participant_id"] for row in rows]
        record = [(order.index(sid), self.study.subject(sid)) for sid in sids if sid in self.study]
        if not record:
            return 0
        if self._current in sids:
            self._leave(write=True)
            self._removeSubjectNodes()
            self._current = None
            shell = _currentShell(self._shell)
            if shell is not None:
                # The breadcrumb never names a subject that is not open (nor in the study any more)
                shell.setBreadcrumb(self.study.name, None, onClick=lambda: showSubjectSwitcher(shell))
        for _index, row in record:
            self.study.remove_subject(row["participant_id"])
        self._removed.append(record)
        self.flush()
        _notify(self)
        return len(record)

    def undoRemove(self):
        """Put the subjects of the last ``removeSubjects()`` back at their places. Return the number restored."""
        if not self._removed:
            return 0
        record = self._removed.pop()
        restored = 0
        for index, row in sorted(record, key=lambda item: item[0]):
            sid = row["participant_id"]
            if sid in self.study:
                continue
            self.study._insert(dict(row))
            # _insert appends: move the row back to its place in the table
            rows = self.study._rows
            moved = rows.pop()
            rows.insert(min(index, len(rows)), moved)
            restored += 1
        self.flush()
        _notify(self)
        return restored

    def gridQuestions(self):
        """Unanswered ambiguous grids: [(grid id, candidates, number of subjects)]."""
        answers = self.study.grid_answers
        return [(gridId, entry["candidates"], len(entry["sids"])) for gridId, entry in self._gridRows.items() if gridId not in answers]

    def answerGrid(self, gridId, spaceName):
        """Record the space of an ambiguous grid in study.json ``grid_answers`` and set it on its subjects."""
        answers = self.study.grid_answers
        answers[gridId] = spaceName
        self.study.grid_answers = answers
        _applyGridAnswers(self.study, self._gridRows)
        self.flush()
        _notify(self)

    # Lesion metrics on import (R5), computed off the main thread, written on it

    def computeMissingMetrics(self):
        """Compute the metrics of lesion files whose row has none (or whose file changed) in the background."""
        studyModule = _lnm("study")
        work = []
        for row in self.study.subjects:
            sid = row["participant_id"]
            path = self.study.abspath(row.get("lesion"))
            if sid in self._metricsPending or not path or not _isNifti(path) or not os.path.isfile(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            known = not studyModule.is_missing(row.get("volume_ml"))
            cached = row.get("lesion_mtime") == studyModule.format_value(round(stat.st_mtime, 3)) and row.get("lesion_size") == str(stat.st_size)
            if known and (cached or studyModule.is_missing(row.get("lesion_mtime"))):
                continue
            work.append((sid, path, None if studyModule.is_missing(row.get("space")) else row["space"]))
        if not work:
            return 0
        if self._metricsWriter is None:
            from . import autosave_io

            self._metricsWriter = autosave_io.BackgroundWriter()
        brainMask = _loadBrainMask()
        for sid, path, lesionSpace in work:
            self._metricsPending.add(sid)
            self._metricsWriter.submit(_fileMetrics, path, lesionSpace, brainMask,
                                       onDone=lambda result, sid=sid: self._onMetrics(sid, result),
                                       onError=lambda error, sid=sid: self._onMetricsError(sid, error))
        return len(work)

    def _onMetrics(self, sid, result):
        self._metricsPending.discard(sid)
        if self._closed or sid not in self.study:
            return
        values, mtime, size = result
        values = {key: value for key, value in values.items() if not key.startswith("_")}
        values["lesion_mtime"] = round(mtime, 3)
        values["lesion_size"] = size
        self.study.set(sid, values)
        self.scheduleSave()
        if not self._metricsPending:
            _notify(self)

    def _onMetricsError(self, sid, error):
        self._metricsPending.discard(sid)
        logging.warning(f"Studio: the lesion metrics of {sid} could not be computed: {error}")

    def waitForMetrics(self, timeoutS=30.0):
        """Wait until the background metrics are applied (tests). Return True if they all finished."""
        if self._metricsWriter is None:
            return True
        finished = self._metricsWriter.drain(timeoutS)
        return finished and not self._metricsPending

    # Hooks (LNM addendum 6.2)

    def addDoneHook(self, fn, order=50):
        """``fn(sid, labelmapArray, ijkToRAS, ctx) -> {column: value} | None``; raise ``Blocking(message)`` to stop Done."""
        self._doneHooks.append((order, len(self._doneHooks), fn))
        self._doneHooks.sort(key=lambda item: (item[0], item[1]))

    def removeDoneHook(self, fn):
        self._doneHooks = [item for item in self._doneHooks if item[2] is not fn]

    def addOpenHook(self, fn):
        """``fn(sid, ctx)`` after a subject is opened (ctx: session, study, row, segmentation and volume nodes)."""
        if fn not in self._openHooks:
            self._openHooks.append(fn)

    def removeOpenHook(self, fn):
        while fn in self._openHooks:
            self._openHooks.remove(fn)

    # Nodes of the current subject

    def segmentationNode(self):
        nodeId = self._nodeIds.get("segmentation")
        return slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None

    def volumeNode(self):
        nodeId = self._nodeIds.get("scan")
        return slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None

    def gridReference(self):
        """The file whose header the current lesion is written with."""
        return self._gridReference

    def templateRecord(self):
        return self.study.template or defaultTemplate()

    def templateNode(self):
        """The study template volume (loaded once per session and shared by the subjects without a scan)."""
        record = self.templateRecord()
        source = str(record.get("source") or ("sampledata:" + DEFAULT_TEMPLATE_SAMPLE))
        nodeId = self._templateCache.get(source)
        node = slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None
        if node is not None:
            return node
        importer = _importer()
        if source.startswith("sampledata:"):
            sampleName = source[len("sampledata:"):]
            if sampleName == TEMPLATE_1MM_SAMPLE and importer.cachedTemplatePath(TEMPLATE_1MM_SAMPLE) is None:
                sampleName = DEFAULT_TEMPLATE_SAMPLE
            node = importer.studyTemplateVolume(sampleName)
            # SampleData leaves its templates without a storage file: the lesion header comes from the template file
            self._templateFiles[node.GetID()] = _storagePath(node) or sampleFile(sampleName)
        else:
            path = self.study.abspath(source)
            if not path or not os.path.isfile(path):
                raise RuntimeError(_("The study template {path} is missing").format(path=path))
            with importer._ownLoad():
                node = slicer.util.loadVolume(path, {"singleFile": True, "show": False})
            node.SetAttribute(SUBJECT_ATTRIBUTE, "template")
            node.SetAttribute(space.ROLE_ATTRIBUTE, space.ROLE_TEMPLATE)
            self._templateFiles[node.GetID()] = path
        self._templateCache[source] = node.GetID()
        return node

    def _isTemplate(self, node):
        return node is not None and node.GetID() in self._templateCache.values()

    def _removeSubjectNodes(self):
        segmentationId = self._nodeIds.get("segmentation")
        scanId = self._nodeIds.get("scan")
        self._observer.dispose()
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        editor = widget.editor if widget is not None else None
        if editor is not None and editor.segmentationNode() is not None and editor.segmentationNode().GetID() == segmentationId:
            editor.setSegmentationNode(None)
        _removeNode(segmentationId)
        scan = slicer.mrmlScene.GetNodeByID(scanId) if scanId else None
        if scan is not None and not self._isTemplate(scan):
            _removeNode(scanId)
        self._nodeIds = {"scan": None, "segmentation": None}

    # Dirty state and tracing time

    def _onSegmentModified(self):
        if self._openStamp is None:
            return
        now = time.monotonic()
        if self._lastActivity is not None:
            self._activeSeconds += min(now - self._lastActivity, IDLE_PAUSE_S)
        self._lastActivity = now
        if not self._dirty and _contentStamp(self.segmentationNode()) != self._openStamp:
            self._dirty = True
        if self._journal is not None and self._dirty:
            self._journal.schedule()

    def markDirty(self):
        """The current subject's lesion changed (edits that the segment observers do not see)."""
        if self._current is not None:
            self._dirty = True
            self._markedDirty = True
            if self._journal is not None:
                self._journal.schedule()

    def isDirty(self, sid=None):
        """True when the subject (default: the current one) has edits in Draw that are not written yet."""
        sid = sid or self._current
        if sid is None or sid != self._current:
            return False
        return self._dirty or (_contentStamp(self.segmentationNode()) != self._openStamp if self.segmentationNode() is not None else False)

    def traceSeconds(self):
        """Tracing time of the current subject in this session (paused after 2 minutes without an edit)."""
        extra = min(time.monotonic() - self._lastActivity, IDLE_PAUSE_S) if self._lastActivity is not None else 0.0
        return self._activeSeconds + extra

    # Opening subjects

    def open(self, sid, switchToDraw=True):
        """Open a subject: write the current one's edits, load the scan (or the study template) and the lesion, and
        edit the lesion in Draw. Return True if the subject is open.
        """
        if sid is None or sid not in self.study:
            return False
        shell = _currentShell(self._shell)
        if sid == self._current and self.segmentationNode() is not None:
            if switchToDraw:
                self._showDraw(shell)
            return True
        startTime = time.perf_counter()
        try:
            from . import autosave

            suspended = autosave.suspended(_("switching subject"))
        except ImportError:
            suspended = contextlib.nullcontext()
        # Rendering is paused for the whole switch: loading, removing and binding nodes each force a render of every
        # view otherwise (the views render once, when it resumes)
        with suspended, _busyCursor(), _renderPaused():
            if self._current is not None and not self._leave(write=True):
                return False
            try:
                self._load(sid)
            except Exception as e:
                logging.error(f"Studio: cannot open {sid}:\n{traceback.format_exc()}")
                _toast(shell, _("{sid} could not be opened: {error}").format(sid=sid, error=e), ms=6000, level="warning")
                return False
        self.lastSwitchSeconds = time.perf_counter() - startTime
        self._writeHeartbeat()
        self._showBreadcrumb(shell)
        if self._journal is not None:
            self._journal.attach(sid, self.segmentationNode())
        ctx = self._context(sid)
        for fn in list(self._openHooks):
            try:
                fn(sid, ctx)
            except Exception:
                logging.error(f"Studio: a study open hook failed:\n{traceback.format_exc()}")
        _notify(self)
        if switchToDraw:
            self._showDraw(shell)
        return True

    def _showDraw(self, shell):
        if shell is None:
            return
        draw = _draw()
        if draw is not None:
            draw.requestArm(shell)
        if shell.currentWorkspace() != "draw":
            shell.switchTo("draw")

    def _load(self, sid):
        importer = _importer()
        row = self.study.subject(sid)
        scanPath = self.study.abspath(row.get("scan"))
        lesionPath = self.study.abspath(row.get("lesion"))
        if scanPath and not os.path.isfile(scanPath):
            logging.warning(f"Studio: the scan of {sid} is missing: {scanPath}")
            scanPath = None
        if lesionPath and not os.path.isfile(lesionPath):
            logging.warning(f"Studio: the lesion of {sid} is missing: {lesionPath}")
            lesionPath = None
        previous = dict(self._nodeIds)
        previousVolume = self.volumeNode()
        with importer._ownLoad():
            if scanPath:
                volumeNode = slicer.util.loadVolume(scanPath, {"singleFile": True, "show": False})
                # The file's name, without the "_2" that the scene adds for a name it used before (an earlier subject switch)
                volumeNode.SetName(importer._baseName(scanPath))
                volumeNode.SetAttribute(SUBJECT_ATTRIBUTE, sid)
            else:
                volumeNode = self.templateNode()
            if lesionPath:
                segmentationNode = importer.loadLesion(lesionPath)
                gridReference = lesionPath
            else:
                segmentationNode = importer.createLesionSegmentation(volumeNode, sid)
                gridReference = scanPath or self._templateFiles.get(volumeNode.GetID()) or _storagePath(volumeNode)
        segmentationNode.SetName(sid)
        segmentationNode.SetAttribute(SUBJECT_ATTRIBUTE, sid)
        displayNode = segmentationNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetVisibility(True)
        importer.selectInSegmentEditor(segmentationNode, volumeNode)
        importer.setViewLayers(background=volumeNode, fit=previousVolume is None or previousVolume.GetID() != volumeNode.GetID())
        # The previous subject's nodes go once the new ones are shown (its scan stays when it is the shared template)
        self._nodeIds = previous
        self._removeSubjectNodes()
        self._nodeIds = {"scan": volumeNode.GetID(), "segmentation": segmentationNode.GetID()}
        self._current = sid
        self._gridReference = gridReference
        self._openStamp = _contentStamp(segmentationNode)
        self._dirty = False
        self._markedDirty = False
        self._activeSeconds = 0.0
        self._lastActivity = None
        self._observer.observe(segmentationNode)

    def _context(self, sid):
        return {"session": self, "study": self.study, "row": self.study.subject(sid) if sid in self.study else {},
                "segmentationNode": self.segmentationNode(), "volumeNode": self.volumeNode(), "gridReference": self._gridReference}

    def _showBreadcrumb(self, shell):
        if shell is None or self._current is None:
            return
        shell.setBreadcrumb(self.study.name, self._current, onClick=lambda: showSubjectSwitcher(shell))

    def _leave(self, write=True):
        """Leave the current subject: write its edits (Lightroom: no Save), stop the journal. Return False to stay."""
        if self._current is None:
            return True
        if write:
            self._applyPendingSmartFill()
            _commitDrawCardEdits()
            if self.isDirty():
                mask = lesionMask(self.segmentationNode())
                if mask is None:
                    # Erasing every voxel never traps the user on the subject: nothing is written, the file is kept
                    self._leaveEmpty()
                elif not self._markedDirty and self._sameAsLesionFile(mask):
                    # Edits that cancel out (paint, then undo): the file and the row stay as they are
                    if self._journal is not None:
                        self._journal.discard(self._current)
                else:
                    try:
                        self._write(status=None, runHooks=False, mask=mask)
                    except Blocking as e:
                        _toast(self._shell, str(e), ms=6000, level="warning")
                        return False
                    except Exception as e:
                        logging.error(f"Studio: cannot write the lesion of {self._current}:\n{traceback.format_exc()}")
                        _toast(self._shell, _("The lesion of {sid} could not be written: {error}").format(sid=self._current, error=e), ms=6000,
                               level="warning")
                        return False
        if self._journal is not None:
            self._journal.detach()
        return True

    def _leaveEmpty(self):
        """Leave a subject whose lesion was erased: nothing is written, its lesion file (if any) is kept, it says so."""
        sid = self._current
        studyModule = _lnm("study")
        row = self.study.subject(sid)
        lesionPath = self.study.abspath(row.get("lesion"))
        if lesionPath and os.path.isfile(lesionPath):
            text = _("The lesion of {sid} is empty: nothing was written, {name} was kept").format(sid=sid, name=os.path.basename(lesionPath))
        else:
            text = _("The lesion of {sid} is empty: nothing was written").format(sid=sid)
            if row.get("status") == "inprogress":
                self.study.set(sid, status="untraced", modified=studyModule.now_iso())
                self.scheduleSave()
        _toast(self._shell, text, ms=8000, level="consequence")
        if self._journal is not None:
            self._journal.discard(sid)
        self._dirty = False
        self._markedDirty = False

    def _sameAsLesionFile(self, mask):
        """True if the cropped lesion ``mask`` (see ``lesionMask()``) has exactly the voxels of the subject's lesion file."""
        path = self.study.abspath(self.study.subject(self._current).get("lesion"))
        if not path or not _isNifti(path) or not os.path.isfile(path):
            return False
        try:
            data, affine, _header = _lnm("nifti").read_nifti(path, apply_scaling=False)
        except Exception:
            return False
        if data.ndim > 3:
            data = data[..., 0]
        cropped = _cropped(np.ascontiguousarray(np.transpose(data != 0, (2, 1, 0))), np.asarray(affine, float))
        if cropped is None:
            return False
        array, ijkToRas = mask
        return (cropped[0].shape == array.shape and np.array_equal(cropped[0], array.astype(bool))
                and np.allclose(cropped[1], ijkToRas, atol=1e-4))

    def _applyPendingSmartFill(self):
        draw = _draw()
        apply = getattr(draw, "_applyPendingSmartFill", None) if draw is not None else None
        if apply is not None:
            try:
                apply(self._shell)
            except Exception:
                logging.error(f"Studio: the Smart fill preview could not be applied:\n{traceback.format_exc()}")

    # Writing the lesion

    def _lesionTarget(self, sid, row, spaceName):
        """``(absolute path, review edit)``: the subject's file under lesions/ (overwritten), else the default study path."""
        studyModule = _lnm("study")
        lesionsDir = os.path.join(self.study.path, studyModule.LESIONS_DIR)
        current = self.study.abspath(row.get("lesion"))
        derivatives = os.path.join(self.study.path, studyModule.DERIVATIVES_DIR)
        reviewEdit = bool(current) and _isInside(current, derivatives)
        if current and _isNifti(current) and _isInside(current, lesionsDir):
            return current, reviewEdit
        return self.study.abspath(self.study.lesion_relpath(sid, spaceName)), reviewEdit

    def _spaceOfReference(self):
        reference = self._gridReference
        if not reference or not _isNifti(reference) or not os.path.isfile(reference):
            return None
        try:
            return _lnm("study").detect_file_space(reference)
        except (OSError, ValueError):
            return None

    def _write(self, status=None, runHooks=False, mask=None):
        """Write the current lesion (see the module docstring) and update its row. Return a result dict.

        :param status: the new status ("traced" for Done); None keeps it ("untraced" becomes "inprogress").
        :param mask: ``lesionMask()`` of the segmentation when the caller has it already.
        :raises Blocking: an empty lesion, a grid that cannot be written exactly, or a Done hook.
        """
        sid = self._current
        segmentationNode = self.segmentationNode()
        if sid is None or segmentationNode is None:
            raise Blocking(_("No subject is open"))
        nifti = _lnm("nifti")
        studyModule = _lnm("study")
        if mask is None:
            mask = lesionMask(segmentationNode)
        if mask is None:
            raise Blocking(_("The lesion of {sid} is empty: nothing was saved").format(sid=sid))
        array, ijkToRas = mask
        reference = self._gridReference
        if not reference or not os.path.isfile(reference) or not _isNifti(reference):
            raise Blocking(_("The grid of {sid} has no NIfTI file to write the lesion on").format(sid=sid))
        row = self.study.subject(sid)
        rowSpace = None if studyModule.is_missing(row.get("space")) else row["space"]
        spaceName = rowSpace or self._spaceOfReference() or self.study.space or "native"
        if not re.fullmatch(r"[A-Za-z0-9]+", spaceName):
            spaceName = "native"
        brainMask = _loadBrainMask() if spaceName == "MNI152NLin2009cAsym" else None
        values = metricsValues(array, ijkToRas, spaceName, brainMask)
        target, reviewEdit = self._lesionTarget(sid, row, spaceName)
        extras = {}
        if runHooks:
            ctx = self._context(sid)
            ctx.update({"metrics": dict(values), "target": target})
            for _order, _sequence, fn in list(self._doneHooks):
                result = fn(sid, array, ijkToRas, ctx)
                if result:
                    extras.update(result)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        extension = ".nii.gz" if target.lower().endswith(".nii.gz") else ".nii"
        temporary = f"{target[:-len(extension)]}.{os.getpid()}.tmp{extension}"
        try:
            nifti.write_like(temporary, array, ijkToRas, reference, dtype=np.uint8, descrip="Slicer Studio lesion")
            _replaceFile(temporary, target)
        except ValueError as e:
            raise Blocking(_("The lesion of {sid} cannot be written on its grid: {error}").format(sid=sid, error=e)) from e
        finally:
            with contextlib.suppress(OSError):
                os.remove(temporary)
        # Row values
        method = row.get("trace_method")
        tokens = [] if studyModule.is_missing(method) else str(method).split("+")
        if not tokens:
            tokens = ["manual"]
        draw = _draw()
        assisted = draw.assistCounts(sid) if draw is not None and hasattr(draw, "assistCounts") else None
        assistedVoxels = sum(int(entry.get("voxels", 0)) for entry in (assisted or {}).values()) if isinstance(assisted, dict) else 0
        if assistedVoxels and "smartfill" not in tokens:
            tokens.append("smartfill")
        if reviewEdit and "review-edit" not in tokens:
            tokens.append("review-edit")
        updates = {key: value for key, value in values.items() if not key.startswith("_")}
        previousSeconds = self.study.value(sid, "trace_seconds", float) or 0.0
        updates.update({
            "lesion": self.study.relpath(target),
            "space": spaceName,
            "trace_seconds": round(previousSeconds + self.traceSeconds()),
            "modified": studyModule.now_iso(),
            "trace_method": "+".join(tokens),
        })
        if assistedVoxels and values["_voxels"]:
            updates["assist_pct"] = round(min(100.0, 100.0 * assistedVoxels / values["_voxels"]), 2)
        currentStatus = row.get("status")
        if status is not None:
            updates["status"] = status
        elif currentStatus == "untraced":
            updates["status"] = "inprogress"
        updates.update(extras)
        self.study.set(sid, updates)
        stat = os.stat(target)
        self.study.set(sid, lesion_mtime=round(stat.st_mtime, 3), lesion_size=stat.st_size)
        self._writeSidecar(sid, segmentationNode)
        self.flush(lockTimeout=SAVE_LOCK_TIMEOUT_S)
        # The written file now defines the grid; the edits are written
        self._gridReference = target
        self._openStamp = _contentStamp(segmentationNode)
        self._dirty = False
        self._markedDirty = False
        self._activeSeconds = 0.0
        self._lastActivity = None
        if self._journal is not None:
            self._journal.discard(sid)
        _notify(self)
        return {"sid": sid, "path": target, "values": updates}

    def _writeSidecar(self, sid, segmentationNode):
        """The BIDS sidecar of the lesion (``Study.write_lesion_sidecar``) with Draw's Notes and source reference (W2-05)."""
        draw = _draw()
        notesKey = getattr(draw, "NOTES_ATTRIBUTE", "Studio.LesionNotes")
        referenceKey = getattr(draw, "SOURCE_REFERENCE_ATTRIBUTE", "Studio.LesionSourceReference")
        try:
            path = self.study.write_lesion_sidecar(sid, studio={"generated_by": f"Slicer Studio {slicer.app.applicationVersion}"})
            with open(path, encoding="utf-8") as f:
                content = json.load(f)
            notes = segmentationNode.GetAttribute(notesKey) or ""
            reference = segmentationNode.GetAttribute(referenceKey) or ""
            changed = False
            for key, value in (("Notes", notes), ("SourceReference", reference)):
                if value and content.get(key) != value:
                    content[key] = value
                    changed = True
            if changed:
                _lnm("schema").write_text_atomic(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n")
        except Exception:
            logging.warning(f"Studio: the sidecar of {sid} could not be written:\n{traceback.format_exc()}")

    def done(self, writeLesion=True):
        """Done → next: write the lesion (status traced, metrics), then open the next untraced subject.

        A Done hook may block it (``Blocking``): the message is shown inline (a toast), nothing is written and the
        subject stays open. Draw calls ``next()`` right after ``done()``: a plain ``next()`` (no status, no queue) in
        the same event is absorbed (the next subject is already open); ``next(status=…)`` and ``next(queue=…)`` are
        not. Return the write result, or None.
        """
        try:
            return self._done(writeLesion)
        finally:
            # After loading the next subject (a reader may process events), so that Draw's next() in this event is absorbed
            self._absorbNext()

    def _done(self, writeLesion):
        sid = self._current
        shell = _currentShell(self._shell)
        if sid is None:
            _toast(shell, _("Open a subject first"))
            return None
        self._applyPendingSmartFill()
        try:
            if writeLesion:
                result = self._write(status="traced", runHooks=True)
            else:
                self.study.set(sid, status="traced", modified=_now())
                self.flush(lockTimeout=SAVE_LOCK_TIMEOUT_S)
                result = {"sid": sid, "path": None, "values": {"status": "traced"}}
        except Blocking as e:
            _toast(shell, str(e), ms=6000, level="warning")
            return None
        except Exception as e:
            logging.error(f"Studio: Done failed for {sid}:\n{traceback.format_exc()}")
            _toast(shell, _("The lesion of {sid} could not be saved: {error}").format(sid=sid, error=e), ms=6000, level="warning")
            return None
        if result.get("path"):
            _toast(shell, _("Saved {name}").format(name=os.path.basename(result["path"])), ms=2500, toolTip=result["path"])
        nextSid = self.study.next_subject(sid, status="untraced")
        if nextSid is None:
            _toast(shell, _("All subjects of {study} are traced").format(study=self.study.name), ms=4000, level="consequence")
        else:
            self.open(nextSid, switchToDraw=False)
        return result

    def _absorbNext(self):
        """The plain ``next()`` that Draw calls right after ``done()`` (in the same event) does nothing."""
        self._swallowNext = True
        qt.QTimer.singleShot(0, self._releaseNext)

    def _releaseNext(self):
        self._swallowNext = False

    def _queuePredicate(self, queue):
        if queue is None:
            return None
        if callable(queue):
            return queue
        from . import library

        for entry in library.libcardQueues():
            if entry["key"] == queue or entry["label"] == queue:
                return entry["predicate"]
        raise KeyError(f"Unknown queue {queue!r}")

    def _step(self, delta, status=None, queue=None):
        if self._swallowNext and delta > 0 and status is None and queue is None:
            # Draw's plain next() after done(); a next() with a status or a queue (scripts, libcards) still steps
            self._swallowNext = False
            return self._current
        self._swallowNext = False
        predicate = self._queuePredicate(queue)
        rows = self.study.subjects
        ids = [row["participant_id"] for row in rows]
        if not ids:
            return None
        start = ids.index(self._current) if self._current in ids else (-1 if delta > 0 else len(ids))
        candidates = []
        for offset in range(1, len(ids) + 1):
            index = start + delta * offset
            if not 0 <= index < len(ids):
                break  # no wrap-around: a toast at the ends
            candidates.append(rows[index])
        for row in candidates:
            if status is not None and row.get("status") != status:
                continue
            if predicate is not None:
                try:
                    if not predicate(row):
                        continue
                except Exception:
                    logging.error(f"Studio: a study queue predicate failed:\n{traceback.format_exc()}")
                    continue
            return row["participant_id"] if self.open(row["participant_id"], switchToDraw=False) else None
        _toast(self._shell, _("Last subject of the study") if delta > 0 else _("First subject of the study"))
        return None

    def next(self, status=None, queue=None):
        """Write the current subject's edits (its status is kept) and open the next subject in table order
        (``status``: only that status; ``queue``: a libcard queue key or a predicate ``fn(row)``). Return its id.
        """
        return self._step(1, status, queue)

    def previous(self, status=None, queue=None):
        """Like ``next()``, backwards."""
        return self._step(-1, status, queue)

    # Template-only cases (case reports)

    def newCase(self, switchToDraw=True):
        """Add a subject without a scan ("sub-case001"...) whose lesion is drawn on the study template, and open it."""
        studyModule = _lnm("study")
        number = 1
        while f"sub-{CASE_PREFIX}{number:03d}" in self.study:
            number += 1
        sid = f"sub-{CASE_PREFIX}{number:03d}"
        record = self.templateRecord()
        self.study.add_subject(sid, {"group": "case", "status": "untraced", "space": record.get("id") or studyModule.NA,
                                     "trace_template": record.get("id") or studyModule.NA})
        self.flush()
        self.open(sid, switchToDraw=switchToDraw)
        _notify(self)
        return sid


#
# Warp 2009c lesions to the connectome space (LNM addendum (g); transforms CLI of L-05)
#


def canWarpToConnectomeSpace(studySession, sid):
    """True when the subject's lesion is on a 2009 template grid and the connectome space is MNI152NLin6Asym."""
    if studySession is None or sid not in studySession.study:
        return False
    row = studySession.study.subject(sid)
    lesion = studySession.study.abspath(row.get("lesion"))
    target = studySession.study.trace_policy.get("space")
    if not lesion or not os.path.isfile(lesion) or target != "MNI152NLin6Asym":
        return False
    geometry = _lnm("geometry")
    return geometry.space_family(str(row.get("space") or "")) == "2009"


def warpToConnectomeSpace(sid, transformPath=None, referencePath=None, shell=None):
    """Warp a subject's 2009c lesion to MNI152NLin6Asym with the transforms CLI (two background jobs: the fractional
    ``desc-lesionProb`` and the binarized ``desc-lesion`` at 0.5). The originals are kept; the row is repointed through the
    inbox with ``trace_template`` and the transform's sha256 (in the audit record and the lesion sidecar).

    :return: the first job, or None when cancelled.
    """
    studySession = _session
    if not canWarpToConnectomeSpace(studySession, sid):
        _toast(shell, _("{sid} has no MNI152NLin2009cAsym lesion to warp").format(sid=sid))
        return None
    from LesionNetworkMappingUI import jobrunner

    studyObject = studySession.study
    row = studyObject.subject(sid)
    lesion = studyObject.abspath(row["lesion"])
    target = "MNI152NLin6Asym"
    transformPath = transformPath or slicer.util.settingsValue(WARP_TRANSFORM_KEY, "")
    if not transformPath or not os.path.isfile(transformPath):
        transformPath = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Transform MNI152NLin2009cAsym → MNI152NLin6Asym"), "",
                                                       _("ITK transform") + " (tpl-MNI152NLin6Asym_from-MNI152NLin2009cAsym*.h5 *.h5 *.tfm *.txt)")
        if not transformPath:
            return None
        qt.QSettings().setValue(WARP_TRANSFORM_KEY, str(transformPath))
    referencePath = referencePath or sampleFile(NLIN6_2MM_SAMPLE)
    if referencePath is None:
        if not slicer.util.confirmOkCancelDisplay(_("The output grid is the FSL MNI152NLin6Asym 2 mm template, distributed under the FSL licence "
                                                    "terms (non-commercial use). Download it now?"), windowTitle=_("Warp to MNI152NLin6Asym")):
            return None
        import SampleData

        SampleData.SampleDataLogic().downloadSample(NLIN6_2MM_SAMPLE)
        referencePath = sampleFile(NLIN6_2MM_SAMPLE)
        if referencePath is None:
            raise RuntimeError(_("The MNI152NLin6Asym template could not be downloaded"))
    studyModule = _lnm("study")
    lesionsDir = os.path.join(studyObject.path, studyModule.LESIONS_DIR)
    probability = os.path.join(lesionsDir, f"{sid}_space-{target}_desc-lesionProb_mask.nii.gz")
    binary = os.path.join(lesionsDir, f"{sid}_space-{target}_desc-lesion_mask.nii.gz")
    runRoot = os.path.join(studyObject.path, studyModule.PRIVATE_DIR, "jobs")
    common = ["--input", lesion, "--transform", str(transformPath), "--reference", referencePath, "--source-space", "MNI152NLin2009cAsym",
              "--target-space", target]
    sourceSpace = row.get("space")

    def finish(job):
        if not job.succeeded():
            _toast(shell, _("Warping {sid} failed: {reason}").format(sid=sid, reason=job.failureReason or job.state), ms=6000, level="warning")
            return
        provenance = (job.doneEvent or {}).get("provenance") or {}
        audit = {"event": "warp", "sid": sid, "from": sourceSpace, "to": target, "transform": os.path.basename(str(transformPath)),
                 "transform_sha256": provenance.get("transform_sha256"), "original": row["lesion"],
                 "outputs": [studyObject.relpath(probability), studyObject.relpath(binary)], "at": studyModule.now_iso()}
        studyModule.Study.post_result(studyObject.path, sid, {"lesion": studyObject.relpath(binary), "space": target, "trace_template": sourceSpace},
                                      "warp", audit)
        if _session is studySession:
            studySession.applyInbox()
            try:
                studyObject.write_lesion_sidecar(sid, studio={"warped_from": row["lesion"], "trace_template": sourceSpace,
                                                              "transform": os.path.basename(str(transformPath)),
                                                              "transform_sha256": provenance.get("transform_sha256")})
            except Exception:
                logging.warning(f"Studio: the sidecar of the warped lesion of {sid} could not be written:\n{traceback.format_exc()}")
        _toast(shell, _("{sid} warped to {space} (the original is kept)").format(sid=sid, space=target), ms=4000, level="consequence")

    def binarize(job):
        if not job.succeeded():
            finish(job)
            return
        jobrunner.instance().submit(jobrunner.JobSpec("LesionNetworkMappingLib.transforms", ["warp", *common, "--output", binary, "--interpolation", "linear",
                                                                                            "--binarize-at", "0.5"],
                                                      title=_("Warping {sid}").format(sid=sid), kind="warp", runRoot=runRoot, onDone=finish))

    return jobrunner.instance().submit(jobrunner.JobSpec("LesionNetworkMappingLib.transforms", ["warp", *common, "--output", probability,
                                                                                              "--interpolation", "linear"],
                                                         title=_("Warping {sid}").format(sid=sid), kind="warp", runRoot=runRoot, onDone=binarize))


#
# Subject switcher (the breadcrumb popover)
#


class SubjectSwitcher:
    """A popup under the breadcrumb: a search field and the subjects of the study; Enter or a click opens one."""

    def __init__(self, studySession, anchor=None):
        self.session = studySession
        self.widget = qt.QFrame(slicer.util.mainWindow())
        self.widget.setObjectName("StudioSubjectSwitcher")
        self.widget.setWindowFlags(qt.Qt.Popup)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.search = qt.QLineEdit()
        self.search.setObjectName("StudioSubjectSwitcherSearch")
        self.search.setPlaceholderText(_("Search subjects"))
        self.search.setClearButtonEnabled(True)
        self.search.connect("textChanged(QString)", self._fill)
        self.search.connect("returnPressed()", self._openCurrent)
        layout.addWidget(self.search)
        self.list = qt.QListWidget()
        self.list.setObjectName("StudioSubjectSwitcherList")
        self.list.setUniformItemSizes(True)
        self.list.connect("itemActivated(QListWidgetItem*)", self._onActivated)
        self.list.connect("itemClicked(QListWidgetItem*)", self._onActivated)
        layout.addWidget(self.list)
        self.widget.setFixedSize(320, 360)
        self.search.installEventFilter(_switcherKeys)
        _switcherKeys.target = self
        self._fill("")
        if anchor is not None:
            point = anchor.mapToGlobal(qt.QPoint(0, anchor.height))
            self.widget.move(point)
        from . import ui

        ui.bindStyleSheet(self.widget, lambda: (
            f"QFrame#StudioSubjectSwitcher {{ background-color: {ui.token('card').name()}; border: 1px solid {ui.token('border').name()}; "
            "border-radius: 8px; }"))

    def show(self):
        self.widget.show()
        self.widget.raise_()
        self.search.setFocus(qt.Qt.PopupFocusReason)

    def _fill(self, text):
        studyModule = _lnm("study")
        query = str(text or "").strip().lower()
        self.list.clear()
        current = self.session.current()
        for row in self.session.study.subjects:
            sid = row["participant_id"]
            if query and query not in sid.lower():
                continue
            volume = row.get("volume_ml")
            parts = [sid, row.get("status", "")]
            if not studyModule.is_missing(volume):
                parts.append(_("{ml} mL").format(ml=volume))
            self.list.addItem(("●  " if sid == current else "    ") + " · ".join(parts))
            item = self.list.item(self.list.count - 1)
            item.setData(qt.Qt.UserRole, sid)
            if sid == current:
                self.list.setCurrentItem(item)
        if self.list.currentItem() is None and self.list.count:
            self.list.setCurrentRow(0)

    def _openCurrent(self):
        item = self.list.currentItem()
        if item is not None:
            self._onActivated(item)

    def _onActivated(self, item):
        sid = item.data(qt.Qt.UserRole)
        self.widget.close()
        if sid:
            self.session.open(str(sid))

    def moveSelection(self, step):
        row = max(0, min(self.list.count - 1, self.list.currentRow + step))
        self.list.setCurrentRow(row)


class _SwitcherKeys(qt.QObject):
    """Up/Down in the search field move the selection of the switcher list."""

    target = None

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.KeyPress and self.target is not None and event.key() in (qt.Qt.Key_Up, qt.Qt.Key_Down):
            self.target.moveSelection(-1 if event.key() == qt.Qt.Key_Up else 1)
            return True
        return False


_switcherKeys = _SwitcherKeys()
_switcher = None


def showSubjectSwitcher(shell=None):
    """Open the subject switcher under the breadcrumb (or at the top of the window). Return it, None without a study."""
    global _switcher
    if _session is None:
        return None
    shell = _currentShell(shell)
    anchor = None
    if shell is not None and shell.topBar is not None:
        anchor = getattr(shell.topBar, "breadcrumbButton", None)
    if _switcher is not None and _isAlive(_switcher.widget):
        _switcher.widget.close()
        _switcher.widget.deleteLater()
    _switcher = SubjectSwitcher(_session, anchor)
    _switcher.show()
    return _switcher


#
# Autosave recovery (A-01: "Reopen study at <sid>") and context
#


def _autosaveContext():
    if _session is None:
        return {}
    return {"study": {"path": _session.study.path, "name": _session.study.name, "subject": _session.current()}}


def _slotStudy(slot):
    study = getattr(slot, "study", None)
    if isinstance(study, str):
        return {"path": study}
    return study if isinstance(study, dict) else None


def _canRecover(slot):
    record = _slotStudy(slot)
    return bool(record and record.get("path") and isAvailable() and _lnm("study").Study.is_study(record["path"]))


def _recover(slot):
    record = _slotStudy(slot)
    openStudy(record["path"], subject=record.get("subject") or None)


#
# Search entries and plug-in
#


def _paletteEntries(shell=None):
    if not isAvailable():
        return []
    entries = [
        {"id": "study.new", "title": _("New study…"), "category": _("Action"), "synonyms": ["create study", "cohort", "new cohort", "study folder"],
         "callback": lambda: newStudyDialog(shell), "description": _("Make a study of a folder of scans and lesion masks")},
        {"id": "study.open", "title": _("Open study…"), "category": _("Action"), "synonyms": ["open cohort", "study.json", "participants"],
         "callback": lambda: openStudyDialog(shell), "description": _("Open a study folder")},
    ]
    if _session is not None:
        entries += [
            {"id": "study.close", "title": _("Close study"), "category": _("Action"), "synonyms": ["close cohort"], "callback": closeStudy},
            {"id": "study.subjects", "title": _("Subjects…"), "category": _("Action"), "synonyms": ["switch subject", "next subject", "go to subject"],
             "callback": lambda: showSubjectSwitcher(shell), "description": _("Open another subject of the study")},
            {"id": "study.newCase", "title": _("New case on the study template"), "category": _("Action"),
             "synonyms": ["new case", "case report", "template case"], "callback": lambda: _session.newCase() if _session is not None else None},
            {"id": "study.mergeInbox", "title": _("Merge job results into the study"), "category": _("Action"), "synonyms": ["inbox", "refresh study"],
             "callback": lambda: _session.applyInbox() if _session is not None else None},
        ]
    return entries


_installedShell = None
_autosaveRegistered = [None]


def _registerWithAutosave():
    """Add the study to every autosave slot and offer "Reopen study at <sid>" first (A-01). The autosave service starts after
    the Studio plug-ins, so this runs again when a study is opened.
    """
    try:
        from . import autosave
    except ImportError:
        return
    service = autosave.instance()
    if service is None or _autosaveRegistered[0] is service:
        return
    service.registerContextProvider(_autosaveContext)
    if not any(handler[0] is _canRecover for handler in service.recoveryHandlers()):
        service.registerRecoveryHandler(_canRecover, _recover)
    _autosaveRegistered[0] = service


def install(shell):
    """Studio plug-in (``shell.OPTIONAL_PLUGINS``): expose the open study as ``shell.studySession``, add the Search
    entries and the "Reopen study" recovery. The last study is offered in Recent, never opened automatically.
    """
    global _installedShell
    _installedShell = shell
    shell.studySession = _session
    try:
        from . import palette

        palette.registerProvider(_paletteEntries)
    except ImportError:
        pass
    _registerWithAutosave()
    qt.QTimer.singleShot(0, _registerWithAutosave)


def uninstall(shell):
    """Classic: the study stays open (the Studio module panel lists its subjects)."""
    global _installedShell
    _installedShell = None
    try:
        from . import palette

        palette.unregisterProvider(_paletteEntries)
    except ImportError:
        pass
