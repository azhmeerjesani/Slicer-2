"""Studies in Slicer Studio: the Lightroom-catalog layer of the Library (SPEC 3.1 "with a study", 5.5, 7.3; D15, D19).

A study is a plain folder (``LesionNetworkMappingLib.study``): ``study.json``, ``participants.tsv``, ``lesions/``,
``derivatives/`` and ``.studio/``. This module opens one study at a time and edits one subject at a time:

- ``createStudy(files)`` makes a study of a folder of NIfTI files (header-only scanning: no node is created). A folder
  that already has a ``participants.tsv`` is adopted in place: its columns and their order are kept, a backup is
  written once to ``.studio/participants.orig.tsv`` and the values of a group column ("patient", "HC", "1"...) are
  mapped to case / control / exclude (``groupMappingProposal()``); the original text is kept in ``group_original``.
  Another column named like a study column but holding other values (a "status" of "alive" / "deceased") is kept as
  ``status_original``, so that tracing never overwrites it.
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
  trace time) and opens the next untraced subject. ``Alt+←/→`` write the same way without marking the subject traced;
  such a write that changes the voxels of an approved or rejected lesion reopens its review (→ pending, audited).
  Leaving a subject never fails because of its lesion's content: an erased lesion writes nothing (its file is kept,
  a toast says so) and edits that cancel out (paint, then undo) leave the file and the row untouched.
- A subject switch pauses the rendering of the views (one render when it ends) instead of showing a wait cursor that
  processes events.
- **Trace queues** (L-45): ``setQueue(key)`` makes Untraced, Needs review, Changes requested, Flagged, This paper (the
  cases of the current case's ``record_id``, else DOI), Second tracing or a Library extension queue the active queue.
  Done → next opens ``next_subject(after, queue)`` (the next case the queue still has to do, wrapping) and Alt+←/→ step
  through the queue's cases; the queue and the position are kept in ``.studio/session.json`` for the next session. Without
  a queue, W3-07's order applies. The case strip of Draw (``StudioLib.casestrip``) shows and chooses the queue.
- **Case series** (L-45): ``newCaseFromPaper()`` ("New case from this paper") adds ``sub-<record><nn>`` with the paper's
  columns (``PAPER_COLUMNS``) after the paper's last case and opens it on the study template;
  ``newCaseFromClipboardFigure()`` then opens the clipboard's image in the Figure card (L-29).
- **Next-case prefetch** (L-45): after a case is opened, the shared background writer (``autosave_io.backgroundWriter``)
  reads the lesion array and the figure crop of the case that Done → next and Alt+→ would open (numpy, Pillow and the
  NIfTI reader only; ``setPrefetchGuard`` checks that it calls no Qt, VTK or MRML). Opening that case then builds its
  segmentation from the array instead of reading the file, and the case strip shows the crop without reading it.
- The STUDY card of Library shows the throughput line "212/300 traced · median 4.1 min/case · ~6 h left"
  (``throughputText``, from ``trace_seconds``).
- Nothing is written outside the study folder; ``participants.tsv`` is written only on the main thread, through
  ``Study.save()`` (lock and merge). Jobs post to ``.studio/inbox``; the session merges the inbox when a study is
  opened, when Library is shown and on request. A job result that repoints the open subject (its scan, lesion or space)
  never applies under its open scene: it waits while the subject has edits that are not written (retried), else the
  subject is left, the result merged and the subject opened again (BB-1). ``.studio/session.json`` holds a heartbeat (every 15 s) while the
  study is open, and the trace queue; it is kept with ``heartbeat_epoch`` 0 when the study is closed.
- **Two windows on one study**: opening a study whose ``session.json`` is the live heartbeat of another process says so
  (a warning toast; the other window is told at its next heartbeat), and closing never marks that other session closed.
  A lesion file that another window wrote after this one opened the subject is never replaced silently: it is copied
  to ``.studio/conflicts/`` first and a toast names the copy.

The session works in Studio and in Classic (the Studio module panel lists the subjects there). Settings: only
``Studio/RecentStudies`` (a JSON list of the recently opened studies).
"""

import contextlib
import datetime
import importlib
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
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
#: A heartbeat younger than this is a live session (as ``pipeline.SESSION_STALE_S`` of A-04's ``pipeline init``)
SESSION_STALE_S = 60.0
#: Folder of .studio/ that keeps a lesion file another window wrote before this one replaced it
CONFLICTS_DIR = "conflicts"
#: A lesion file that this session did not read and that changed this long before its subject was opened (or later) is
#: taken as written by another window (file times can be this coarse)
CONFLICT_MTIME_SLACK_S = 2.0
#: participants.tsv is saved this long after the last change (changes are kept in memory meanwhile)
SAVE_DELAY_MS = 400
#: How long a save waits for .studio/participants.lock on the main thread before it is retried from a timer
SAVE_LOCK_TIMEOUT_S = 0.5
SAVE_RETRY_MS = 2000
#: A job result for the open subject's scan, lesion or space waits while its edits are not written; the merge is retried this often
INBOX_RETRY_MS = 1500
#: The row columns that say which files (and grid) the open subject's scene shows
INBOX_SCENE_COLUMNS = ("scan", "lesion", "space")
#: Tracing time stops counting after this long without an edit (SPEC 5.5)
IDLE_PAUSE_S = 120.0
#: Backup of an adopted participants table (written once)
ADOPT_BACKUP = "participants.orig.tsv"
ADOPT_DICTIONARY_BACKUP = "participants.orig.json"
#: Column that keeps the original text of a mapped "group" column
GROUP_ORIGINAL_COLUMN = "group_original"
#: An adopted column named like a study column with fixed values (status, qc, include, group) but holding other values
#: ("alive", "deceased") is kept as "<name>_original"; the study column gets its default values
ORIGINAL_SUFFIX = "_original"
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

#: Trace queues (L-45). The active queue drives Done → next and Alt+←/→ in Draw; without one, Done → next opens the next
#: untraced subject and Alt+←/→ step in table order (W3-07)
QUEUE_UNTRACED = "untraced"
QUEUE_NEEDS_REVIEW = "needs_review"
QUEUE_CHANGES = "changes_requested"
QUEUE_FLAGGED = "flagged"
QUEUE_PAPER = "this_paper"
QUEUE_SECOND = "second_tracing"
BUILTIN_QUEUES = (QUEUE_UNTRACED, QUEUE_NEEDS_REVIEW, QUEUE_CHANGES, QUEUE_FLAGGED, QUEUE_PAPER, QUEUE_SECOND)
#: Statuses of a case that still has to be traced
TODO_STATUSES = ("untraced", "inprogress")
#: Second tracings (L-44 registers ``rater2_status``) that are not finished
SECOND_TRACING_TODO = ("assigned", "pending", "untraced", "inprogress")
#: Record-level columns that "New case from this paper" copies (the paper, not the case)
PAPER_COLUMNS = ("record_id", "doi", "pmid", "citation", "image_modality", "source_type", "source_dataset")
#: Prefetched cases kept in memory (the next case of Done → next and of Alt+→)
PREFETCH_KEEP = 3
#: Longest side (px) of the figure crop thumbnail that the prefetch prepares for the case strip
CROP_THUMBNAIL_PX = 96

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


class StudyFileError(ValueError):
    """A study cannot be opened because its study.json is missing, unreadable or damaged; the message is plain words that
    can be shown to the user as they are (no parser message, no traceback).
    """


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


def _plainOSError(error):
    from . import autosave
    return autosave.plainOSError(error)


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


def _commitCaseStripEdit(studySession):
    """A case strip field still being edited (L-45) is saved before its case is left, as Enter would save it (a warning
    does not hold it back). Return ``(True, "")``, or ``(False, reason)`` when a value is invalid (the editor stays open).
    """
    try:
        from . import casestrip
    except ImportError:
        return True, ""
    try:
        return casestrip.commitPendingEdit(studySession)
    except Exception:
        logging.error(f"Studio: the case strip edit could not be saved:\n{traceback.format_exc()}")
        return True, ""


def _caseStripEditing():
    """True while a case strip field is being edited (its value is saved when the case is left)."""
    try:
        from . import casestrip
    except ImportError:
        return False
    strip = getattr(casestrip, "_strip", None)
    try:
        return strip is not None and casestrip._isAlive(strip.widget) and bool(strip.isEditing())
    except Exception:
        return False


def _toast(shell, text, **kwargs):
    shell = _currentShell(shell)
    if shell is not None:
        shell.toast(text, **kwargs)
    else:
        logging.info(f"Studio study: {text}")


def _showFolder(folder):
    from . import projects

    projects.showInFileManager(folder)


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def _now():
    return _lnm("study").now_iso()


def _fileStamp(path):
    """``(normcase path, mtime_ns, size)`` of a file, None when it does not exist."""
    try:
        stat = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    return (os.path.normcase(os.path.abspath(path)), stat.st_mtime_ns, stat.st_size)


def _liveOtherSession(record):
    """``record`` (the content of a study's ``.studio/session.json``) when it is the heartbeat of another live session, else
    None: younger than ``SESSION_STALE_S``, of another process, and on this host a process that still exists.
    """
    if not isinstance(record, dict):
        return None
    try:
        beat = float(record.get("heartbeat_epoch") or 0.0)
    except (TypeError, ValueError):
        return None
    if beat <= 0.0 or time.time() - beat > SESSION_STALE_S:
        return None
    if record.get("host") == socket.gethostname():
        pid = record.get("pid")
        if pid == os.getpid():
            return None
        try:
            if pid and not _lnm("jobs").pid_alive(pid):
                return None
        except ImportError:
            pass
    return record


def _isNifti(path):
    lower = str(path).lower()
    return lower.endswith(".nii") or lower.endswith(".nii.gz")


def _isInside(path, folder):
    try:
        relative = os.path.relpath(os.path.realpath(path), os.path.realpath(folder))
    except ValueError:
        return False
    return relative != ".." and not relative.startswith(".." + os.sep) and not os.path.isabs(relative)


def _samePath(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def studyFolderProblem(folder):
    """Why ``folder`` cannot become a new study, or None (A-09 layout): the Slicer Studio folder or a folder that holds it,
    its Projects/, Studies/ and LNM/ folders, a project or a folder inside one, LNM/'s folders, or a folder inside another
    study. Their NIfTI files belong to projects and to other studies; a study goes in its own folder (Studies/<name>/).
    """
    if not folder:
        return None
    folder = os.path.abspath(str(folder))
    try:
        from . import projects
    except ImportError:
        projects = None
    if projects is not None and projects.isEnabled():
        rootPath = projects.root()
        if _samePath(folder, rootPath):
            return _("This is the Slicer Studio folder: choose or create a folder inside Studies/ for the study")
        if _isInside(rootPath, folder):
            return _("{name} holds the Slicer Studio folder, whose projects and studies would become subjects: "
                     "choose the folder of your study's files").format(name=os.path.basename(folder) or folder)
        studiesPath = projects.studiesFolder(rootPath)
        if _samePath(folder, studiesPath):
            return _("Studies/ holds your studies: choose or create a folder inside it (one folder per study)")
        projectsPath = projects.projectsFolder(rootPath)
        if _samePath(folder, projectsPath):
            return _("Projects/ holds your projects, not a study: choose or create a folder inside Studies/")
        if _isInside(folder, projectsPath):
            project = projects.projectOfPath(folder, rootPath)
            name = os.path.basename(project) if project else os.path.basename(folder)
            return _("This folder is part of the project {name}: choose or create a folder inside Studies/ for the study").format(name=name)
        lnmPath = projects.lnmFolder(rootPath)
        if _isInside(folder, lnmPath):
            return _("LNM/ holds lesion network mapping data, not a study: choose or create a folder inside Studies/")
    studyModule = _lnm("study")
    if not studyModule.Study.is_study(folder):
        parent = os.path.dirname(folder)
        while parent and parent != os.path.dirname(parent):
            if studyModule.Study.is_study(parent):
                return _("This folder is inside the study {name}: open that study, or choose a folder outside it").format(
                    name=os.path.basename(parent))
            parent = os.path.dirname(parent)
    return None


def _outsideOtherStudies(files, folder):
    """``files`` without those inside another study (a subfolder of ``folder`` with a study.json): never subjects of this one."""
    studyModule = _lnm("study")
    folder = os.path.normcase(os.path.realpath(folder))
    cache = {}

    def inStudy(directory):
        if os.path.normcase(directory) == folder or os.path.dirname(directory) == directory:
            return False
        if directory not in cache:
            cache[directory] = studyModule.Study.is_study(directory) or inStudy(os.path.dirname(directory))
        return cache[directory]

    return [path for path in files if not (_isInside(path, folder) and inStudy(os.path.dirname(os.path.realpath(path))))]


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


def _userDefaultTemplate():
    """The user's own default template (A-10), or None (the default is a built-in one)."""
    try:
        from . import templates

        template = templates.defaultTemplate()
    except Exception:
        return None
    return template if template is not None and template.isUser and template.isAvailable() else None


def defaultTemplate():
    """study.json ``template`` of a new study: the user's own default template (A-10: its id, name, space and sha256, never a
    path), else the 2009c 1 mm T1w when it was downloaded before, else the bundled 2 mm brain.
    """
    userTemplate = _userDefaultTemplate()
    if userTemplate is not None:
        from . import templates

        return templates.studyRecord(userTemplate)
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


def _makeDirs(path, root, made):
    """``os.makedirs(path, exist_ok=True)``; the folders that were created (parents first) are appended to ``made``."""
    missing = []
    current = path
    while current != root and not os.path.isdir(current):
        missing.append(current)
        current = os.path.dirname(current)
    os.makedirs(path, exist_ok=True)
    made.extend(reversed(missing))


def adoptFolder(folder, name=None, groupColumn=None, groupMapping=None, template=None):
    """Make a study of a folder that has a participants.tsv (adopt in place) and return the opened ``Study``.

    The table's columns and their order are kept (the missing standard columns are appended); its rows keep their
    values, except the ids (normalized to "sub-<label>", the original in ``source_id``) and the group mapping
    (``applyGroupMapping()``). A column named like a study column with fixed values (status, qc, include, group) that
    holds other values ("alive", "deceased") is kept as ``<name>_original`` (``_keepClashingColumns()``; the returned
    study's ``adoptedRenames`` lists them). participants.tsv and participants.json are backed up once to ``.studio/``.
    """
    studyModule = _lnm("study")
    folder = os.path.abspath(folder)
    tsvPath = os.path.join(folder, studyModule.PARTICIPANTS_TSV)
    privateDir = os.path.join(folder, studyModule.PRIVATE_DIR)
    dictionaryPath = os.path.join(folder, studyModule.PARTICIPANTS_JSON)
    dictionary = {}
    if os.path.isfile(dictionaryPath):
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
    columns = [column for column in columns if column != "participant_id"]
    renamed = _keepClashingColumns(studyModule, columns + added, rows, dictionary)
    columns = [renamed.get(column, column) for column in columns]
    ordered = ["participant_id"] + columns
    ordered += [column for column in studyModule.STANDARD_COLUMNS if column not in ordered]
    ordered += [column for column in added if column not in ordered]
    template = template or defaultTemplate()
    # Everything that can fail on the user's table has been checked: only now the folders and the backups are made, so a
    # table that cannot be adopted leaves the folder as it was (and a retry backs up the table that is adopted then).
    # What this call made is removed again when the rest fails.
    made = []
    try:
        for subfolder in (studyModule.LESIONS_DIR, studyModule.DERIVATIVES_DIR, os.path.join(studyModule.PRIVATE_DIR, "thumbs"),
                          os.path.join(studyModule.PRIVATE_DIR, "journal")):
            _makeDirs(os.path.join(folder, subfolder), folder, made)
        for source, backupName in ((tsvPath, ADOPT_BACKUP), (dictionaryPath, ADOPT_DICTIONARY_BACKUP)):
            backup = os.path.join(privateDir, backupName)
            if os.path.isfile(source) and not os.path.exists(backup):
                shutil.copy2(source, backup)
                made.append(backup)
        studyObject = studyModule.Study(folder, _newMetadata(studyModule, folder, name, template), ordered, rows, dictionary)
        # The new study.json is written next to the user's table, without the merge of a later save (nothing else writes it yet)
        studyObject.save(merge=False)
        opened = studyModule.Study.open(folder)
    except BaseException:
        if not os.path.exists(os.path.join(folder, studyModule.STUDY_JSON)):
            for path in reversed(made):
                try:
                    os.rmdir(path) if os.path.isdir(path) else os.remove(path)
                except OSError:
                    pass
        raise
    opened.adoptedRenames = renamed
    return opened


def _keepClashingColumns(studyModule, columns, rows, dictionary):
    """Keep the user's columns that are named like a study column with fixed values but hold other values.

    A "status" column of "alive" / "deceased" is renamed to ``status_original`` (its values, position and data dictionary
    entry unchanged) and the study's own column gets its default values (an "include" column of yes / no / true / false or
    1.0 / 0.0 keeps its meaning as 1 / 0, ``study.include_value``), so that tracing never overwrites the user's values. A study column that already holds
    allowed values only is the study's column; its empty cells get the default. ``columns`` (without participant_id),
    ``rows`` and ``dictionary`` are changed in place.

    :return: {old name: new name} of the renamed columns.
    """
    renamed = {}
    for column, allowed in studyModule.ALLOWED_VALUES.items():
        if column not in columns:
            continue
        default = studyModule.DEFAULTS.get(column, studyModule.NA)
        values = [str(row.get(column, studyModule.NA)).strip() for row in rows]
        if all(studyModule.is_missing(value) or value in allowed for value in values):
            for row in rows:
                if studyModule.is_missing(row.get(column)):
                    row[column] = default
            continue
        target, number = column + ORIGINAL_SUFFIX, 2
        while target in columns or target in dictionary or any(target in row for row in rows):
            target, number = f"{column}{ORIGINAL_SUFFIX}{number}", number + 1
        columns[columns.index(column)] = target
        for row, value in zip(rows, values, strict=True):
            row[target] = row.pop(column, studyModule.NA)
            mapped = studyModule.include_value(value) if column == "include" else None
            row[column] = mapped or default
        if column in dictionary:
            dictionary[target] = dictionary.pop(column)
        renamed[column] = target
        logging.info(f"Studio: the adopted column {column} holds other values than the study's ({', '.join(allowed)}); kept as {target}")
    return renamed


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
    # Studio's own folders (Studies/, Projects/, a project, ...) and folders inside a study never become a study
    problem = studyFolderProblem(folder)
    if problem:
        raise ValueError(problem)
    # The study's own application files and network maps are never subjects
    excluded = [os.path.join(folder, studyModule.PRIVATE_DIR), os.path.join(folder, studyModule.DERIVATIVES_DIR, "lnm")]
    files = [path for path in files if not any(_isInside(path, other) for other in excluded)]
    # Nor are the files of another study in a subfolder
    files = _outsideOtherStudies(files, folder)
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
    for column, kept in (getattr(studyObject, "adoptedRenames", None) or {}).items():
        message += " · " + _("your {column} column is kept as {kept}").format(column=column, kept=kept)
    if report.get("skipped"):
        message += " · " + (_("1 file skipped") if len(report["skipped"]) == 1 else _("{n} files skipped").format(n=len(report["skipped"])))
    _toast(shell, message, ms=5000, action=lambda: _showFolder(folder), actionText=_("Show folder"),
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


_saveListeners = []


def addSaveListener(callback):
    """``callback(session)`` when a save of participants.tsv has finished (saved or failed), so a view that shows
    ``saveStatus()`` can refresh just that line; ``addListener`` callbacks are not called for it.
    """
    if callback not in _saveListeners:
        _saveListeners.append(callback)


def removeSaveListener(callback):
    while callback in _saveListeners:
        _saveListeners.remove(callback)


def _notifySaved(studySession):
    for callback in list(_saveListeners):
        try:
            callback(studySession)
        except Exception:
            logging.error(f"Studio: study save listener failed:\n{traceback.format_exc()}")


def _notify(studySession=None):
    for callback in list(_listeners):
        try:
            callback(studySession if studySession is not None else _session)
        except Exception:
            logging.error(f"Studio: study listener failed:\n{traceback.format_exc()}")


def _readStudy(studyModule, path):
    """``Study.open(path)``; a damaged or unreadable study.json raises ``StudyFileError`` with a plain message."""
    try:
        return studyModule.Study.open(path)
    except OSError as error:
        raise StudyFileError(_("The study in {folder} cannot be opened: study.json cannot be read ({reason}). Check that the "
                               "folder and its study.json are still there and that you may read them.").format(
            folder=_studyFolder(path), reason=_plainOSError(error))) from error
    except ValueError as error:
        # json.JSONDecodeError and UnicodeDecodeError are ValueErrors; "Not a study" is a study.json of another kind
        if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)) or str(error).startswith("Not a study"):
            raise StudyFileError(_("The study in {folder} cannot be opened: its study.json is damaged or is not a study "
                                   "file. Restore study.json from a backup or from a copy of the study folder (your "
                                   "subjects' images and lesion masks in the folder are not touched), then open the study "
                                   "again.").format(folder=_studyFolder(path))) from error
        raise StudyFileError(_("The study in {folder} cannot be opened: {reason}").format(
            folder=_studyFolder(path), reason=error)) from error


def _studyFolder(path):
    path = os.path.abspath(path)
    return os.path.dirname(path) if os.path.basename(path) == "study.json" else path


def openStudy(pathOrStudy, shell=None, subject=None, showLibrary=True):
    """Open a study (a folder, its study.json or a ``Study``) as the current session and return it.

    The study that was open is closed first. In Studio, Library shows its subject list. With ``subject``, that subject
    is opened in Draw.
    """
    global _session
    studyModule = _lnm("study")
    studyObject = pathOrStudy if isinstance(pathOrStudy, studyModule.Study) else _readStudy(studyModule, str(pathOrStudy))
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
    """The application quits: the current subject's edits are written and the study is closed (its heartbeat stopped)."""
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
    # Starts where the studies are: the parent of the newest recent study (so that it is listed), else Studies/ of the
    # Slicer Studio folder (as New study… does), else the last open folder itself (a folder: no dirname of it)
    recent = (recentStudies() or [{}])[0].get("path")
    created = None
    if recent:
        start = os.path.dirname(recent)
    else:
        start, created = _studiesFolder()
        start = start or _importer()._lastDirectory()
    folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Open study"), start or "")
    if created and os.path.isdir(created) and not os.listdir(created):
        os.rmdir(created)  # created for the dialog only: never leave an empty folder
    if not folder:
        return None
    studyModule = _lnm("study")
    if not studyModule.Study.is_study(folder):
        slicer.util.errorDisplay(_("{folder} is not a study (it has no study.json). Use New study… or drop the folder to create one.").format(folder=folder))
        return None
    return openStudy(folder, shell)


def _studiesFolder():
    """``(folder, created)``: Studies/ of the Slicer Studio folder (A-09), where "New study" starts; ``created`` is the folder
    when it was created for the dialog (removed again if it stays empty), else None. ``(None, None)`` without projects.
    """
    try:
        from . import projects
    except ImportError:
        return None, None
    if not projects.isEnabled():
        return None, None
    folder = projects.studiesFolder()
    if os.path.isdir(folder):
        return folder, None
    try:
        projects.ensureRoot()
        os.makedirs(folder)
    except OSError:
        return None, None
    return folder, folder


def newStudyDialog(shell=None):
    """New study…: choose a folder (the dialog starts in Studies/ of the Slicer Studio folder); its NIfTI files (if any)
    become the subjects, its participants.tsv is adopted. Studies/ itself and Studio's other own folders are refused with
    a warning (``studyFolderProblem()``): a study goes in a folder of its own.
    """
    if not isAvailable():
        _toast(shell, _("Studies are not available in this build"))
        return None
    start, created = _studiesFolder()
    folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("New study: choose the study folder"), start or _importer()._lastDirectory())
    if created and os.path.isdir(created) and not os.listdir(created):
        os.rmdir(created)  # created for the dialog only: never leave an empty folder
    if not folder:
        return None
    problem = studyFolderProblem(folder)
    if problem:
        _toast(shell, problem, ms=8000, level="warning")
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
    problem = studyFolderProblem(folder) if shell is not None else None
    if problem:
        shell.toast(summary + " · " + problem, ms=8000, level="warning")
    elif shell is not None:
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

    # Gaps along S (the lesion z-extent): the k axis of a sagittal or coronal acquisition is R or A
    axis = lesioncard.nearestArrayAxis(ijkToRas, (0.0, 0.0, 1.0))[0] if ijkToRas is not None else 0
    metrics = lesioncard.computeLesionMetrics(maskKji, None, ijkToRas, axis=axis)
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
# Papers, trace queues and throughput (L-45)
#


def _missing(value):
    return value is None or str(value).strip() in ("", "n/a")


def isIncluded(row):
    """True unless the row's include value means 0: the study library's own test (``study.is_included``)."""
    return _lnm("study").is_included(row)


def normalizeDoi(text):
    """A DOI without its resolver prefix ("https://doi.org/", "doi:"), or "" when missing."""
    if _missing(text):
        return ""
    doi = str(text).strip()
    doi = re.sub(r"(?i)^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"(?i)^doi:\s*", "", doi)
    return doi.strip()


def isDoi(text):
    """True for a DOI of the form 10.<registrant>/<suffix>."""
    return re.fullmatch(r"10\.\d{4,9}/\S+", normalizeDoi(text)) is not None


def recordOf(row):
    """The paper of a row: its ``record_id``, else its DOI (lower case, prefixed "doi:"), else None."""
    row = row or {}
    if not _missing(row.get("record_id")):
        return str(row["record_id"]).strip()
    doi = normalizeDoi(row.get("doi"))
    return "doi:" + doi.lower() if doi else None


def paperLabel(row):
    """How a paper is named on screen: the citation, else the record, else the DOI."""
    row = row or {}
    for column in ("citation", "record_id", "doi"):
        if not _missing(row.get(column)):
            return str(row[column]).strip()
    return ""


def _libcardEntry(kind, key):
    """A Library extension queue (``kind`` "queue") or filter chip ("filter") by key, or None."""
    try:
        from . import library
    except ImportError:
        return None
    try:
        entries = library.libcardQueues() if kind == "queue" else library.libcardFilters()
    except Exception:
        logging.error(f"Studio: the Library extensions could not be read:\n{traceback.format_exc()}")
        return None
    for entry in entries:
        if entry.get("key") == key:
            return entry
    return None


def _reviewRules():
    try:
        return _lnm("review")
    except ImportError:
        return None


def _secondTracingPredicate():
    """L-44's "Second tracing" queue when it is installed, else the rows whose ``rater2_status`` is not finished."""
    try:
        from . import library

        for entry in library.libcardQueues():
            if "second" in str(entry.get("key", "")).lower() or str(entry.get("label", "")).lower() == "second tracing":
                return entry["predicate"]
    except Exception:
        logging.error(f"Studio: the Library extensions could not be read:\n{traceback.format_exc()}")
    return lambda row: str(row.get("rater2_status", "")).strip().lower() in SECOND_TRACING_TODO


def queueSpec(key, record=None):
    """A trace queue: ``{"key", "label", "record", "predicate": fn(row), "todo": fn(row)}``, or None for an unknown key.

    ``predicate`` says which subjects the queue holds (Alt+←/→, the position "3 of 12"); ``todo`` which of them Done → next
    opens (for "This paper": its cases that are not traced yet). ``record`` is the paper of "This paper" (``recordOf()``).
    Other Library extension queues (``libcard_*.QUEUES``, for example "Normalization review") are accepted by key.
    """
    if key is None:
        return None
    rules = _reviewRules()

    def libcardOr(kind, entryKey, fallback):
        entry = _libcardEntry(kind, entryKey)
        return entry["predicate"] if entry is not None else fallback

    if key == QUEUE_UNTRACED:
        def predicate(row):
            return row.get("status") in TODO_STATUSES

        return {"key": key, "label": _("Untraced"), "record": None, "predicate": predicate,
                "todo": lambda row: predicate(row) and isIncluded(row)}
    if key == QUEUE_NEEDS_REVIEW:
        predicate = libcardOr("queue", key, rules.needs_review if rules is not None else (lambda row: row.get("review_status") == "pending"))
        return {"key": key, "label": _("Needs review"), "record": None, "predicate": predicate, "todo": predicate}
    if key == QUEUE_CHANGES:
        predicate = libcardOr("queue", key, rules.changes_requested if rules is not None else (lambda row: row.get("review_status") == "changes"))
        return {"key": key, "label": _("Changes requested"), "record": None, "predicate": predicate, "todo": predicate}
    if key == QUEUE_FLAGGED:
        predicate = libcardOr("filter", "has_flags", lambda row: not _missing(row.get("qc_flags")))
        return {"key": key, "label": _("Flagged"), "record": None, "predicate": predicate, "todo": predicate}
    if key == QUEUE_PAPER:
        if not record:
            return None

        def samePaper(row):
            return recordOf(row) == record

        return {"key": key, "label": _("This paper"), "record": record, "predicate": samePaper,
                "todo": lambda row: samePaper(row) and row.get("status") in TODO_STATUSES and isIncluded(row)}
    if key == QUEUE_SECOND:
        predicate = _secondTracingPredicate()
        return {"key": key, "label": _("Second tracing"), "record": None, "predicate": predicate, "todo": predicate}
    entry = _libcardEntry("queue", key)
    if entry is None:
        return None
    return {"key": key, "label": _(str(entry.get("label", key))), "record": None, "predicate": entry["predicate"], "todo": entry["predicate"]}


def availableQueues(row=None):
    """The queues offered for a case ``row`` (the current one): the built-in queues ("This paper" only when the case names
    a paper), then the other Library extension queues.
    """
    specs = []
    for key in BUILTIN_QUEUES:
        spec = queueSpec(key, recordOf(row) if key == QUEUE_PAPER else None)
        if spec is not None:
            specs.append(spec)
    try:
        from . import library

        extra = [entry for entry in library.libcardQueues() if entry.get("key") not in BUILTIN_QUEUES and "second" not in str(entry.get("key", "")).lower()]
    except Exception:
        extra = []
    for entry in extra:
        spec = queueSpec(entry.get("key"))
        if spec is not None:
            specs.append(spec)
    return specs


def _safe(predicate, row):
    try:
        return bool(predicate(row))
    except Exception:
        logging.error(f"Studio: a study queue predicate failed:\n{traceback.format_exc()}")
        return False


def throughput(studyObject):
    """Tracing progress of the included subjects: ``{"traced", "total", "median_s", "left_s"}``.

    Traced counts the traced and mapped subjects; the median is over their ``trace_seconds`` (rows without a time are
    left out) and the time left is the median times the subjects still to trace (None without a time).
    """
    rows = [row for row in studyObject.subjects if isIncluded(row)]
    traced = [row for row in rows if row.get("status") in ("traced", "mapped")]
    seconds = []
    for row in traced:
        try:
            value = float(row.get("trace_seconds"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            seconds.append(value)
    median = float(np.median(seconds)) if seconds else None
    left = len(rows) - len(traced)
    return {"traced": len(traced), "total": len(rows), "median_s": median, "left_s": None if median is None else median * left}


def _durationText(seconds):
    minutes = seconds / 60.0
    if minutes < 90:
        return _("~{n} min").format(n=max(1, round(minutes)))
    hours = minutes / 60.0
    text = f"{hours:.1f}".rstrip("0").rstrip(".") if hours < 10 else f"{round(hours)}"
    return _("~{n} h").format(n=text)


def throughputText(studyObject):
    """"212/300 traced · median 4.1 min/case · ~6 h left" (the parts that are known)."""
    values = throughput(studyObject)
    parts = [_("{traced}/{total} traced").format(traced=values["traced"], total=values["total"])]
    if values["median_s"] is not None:
        parts.append(_("median {minutes} min/case").format(minutes=f"{values['median_s'] / 60.0:.1f}"))
        if values["left_s"]:
            parts.append(_("{duration} left").format(duration=_durationText(values["left_s"])))
    return " · ".join(parts)


def caseLabel(text):
    """The letters and digits of a text (participant labels allow nothing else)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(text or ""))


#
# Next-case prefetch (worker side: numpy, Pillow and the NIfTI reader only; no Qt, VTK or MRML)
#

#: Module prefixes that the prefetch worker must never call (``setPrefetchGuard``)
_GUARDED_PREFIXES = ("qt", "PythonQt", "slicer", "vtk", "ctk", "MRML", "mrml", "qSlicer", "qMRML", "SegmentEditor")
_guard = {"enabled": False, "violations": [], "threads": set(), "jobs": 0}
_guardLock = threading.Lock()


def setPrefetchGuard(enabled):
    """Verification: record every call that the prefetch worker makes into Qt, VTK, MRML or ``slicer`` (and a job that runs
    on the main thread). ``prefetchGuardReport()`` returns them. Off by default (it profiles the worker).
    """
    with _guardLock:
        _guard.update({"enabled": bool(enabled), "violations": [], "threads": set(), "jobs": 0})


def prefetchGuardReport():
    """``{"jobs", "threads", "violations"}`` recorded since ``setPrefetchGuard(True)``."""
    with _guardLock:
        return {"jobs": _guard["jobs"], "threads": sorted(_guard["threads"]), "violations": list(_guard["violations"])}


class _WorkerGuard:
    """Profiles the calling (worker) thread while a prefetch job runs and records guarded calls (``setPrefetchGuard``)."""

    def __enter__(self):
        thread = threading.current_thread()
        with _guardLock:
            _guard["jobs"] += 1
            _guard["threads"].add(thread.name)
            if thread is threading.main_thread():
                _guard["violations"].append("job ran on the main thread")
        sys.setprofile(self._profile)
        return self

    def __exit__(self, *exc):
        sys.setprofile(None)
        return False

    @staticmethod
    def _profile(frame, event, arg):
        if event == "call":
            name = str(frame.f_globals.get("__name__", ""))
            what = f"{name}.{frame.f_code.co_name}"
            if frame.f_code.co_name == "find_spec":
                what += f"({frame.f_locals.get('fullname')})"  # an import run in the worker
        elif event == "c_call":
            owner = getattr(arg, "__self__", None)
            name = str(getattr(arg, "__module__", None) or (type(owner).__module__ if owner is not None else ""))
            what = f"{name}.{getattr(arg, '__name__', '?')}"
        else:
            return
        if name.split(".", 1)[0].startswith(_GUARDED_PREFIXES):
            with _guardLock:
                if len(_guard["violations"]) < 50:
                    _guard["violations"].append(what)


def _readPrefetchLesion(path):
    """The lesion of a case for ``StudySession._load`` (worker): ``{"array" (uint8, k j i), "affine"}`` or ``{"fallback": reason}``.

    Only plain, non-empty binary masks whose voxel-to-RAS matrix Slicer's reader certainly gives the same way are taken
    (``_itkGeometry``): anything else is loaded by the segmentation reader, which places a file the way Slicer always does.
    """
    nifti = _lnm("nifti")
    header = nifti.read_nifti_header(path)
    if header["sform_code"] <= 0 and header["qform_code"] <= 0:
        return {"fallback": "no sform or qform"}
    affine = _itkGeometry(header)
    if affine is None:
        return {"fallback": "the reader may place it otherwise (oblique sform and qform, shear, units)"}
    data, _affine, _header = nifti.read_nifti(path, apply_scaling=False)
    if data.ndim != 3 or data.dtype.kind not in "iub":
        return {"fallback": f"{data.ndim}D {data.dtype} image"}
    values = set(np.unique(data).tolist())
    if not values <= {0, 1}:
        return {"fallback": "not a 0/1 mask"}
    if 1 not in values:
        # The reader makes an empty "Lesion" segment of its own (AddEmptySegment): the same structure as any empty file
        return {"fallback": "empty mask"}
    return {"array": np.ascontiguousarray(np.transpose(data, (2, 1, 0)), dtype=np.uint8), "affine": affine,
            "spacing": tuple(float(value) for value in header["pixdim"][1:4])}


def _itkGeometry(header):
    """The voxel-to-RAS matrix that Slicer's NIfTI reader (ITK ``NiftiImageIO``) gives a file, or None when it is not
    certain to be this one.

    ITK reads the float32 sform or qform, takes the directions from its normalized columns, the spacing from pixdim and
    the origin from its translation. It takes the sform when only the sform is set (it must be orthonormal: a sheared
    sform is not read at all), the qform when only the qform is set; with both, its choice is certain only when the two
    are the same matrix (axis-aligned images, MNI-space lesions) or the sform is scanner-based.
    """
    pixdim = np.asarray(header["pixdim"][1:4], float)
    if np.any(pixdim <= 0) or (header["xyzt_units"] & 0x07) not in (0, 2):
        return None  # mirrored or zero spacing, or units other than mm (ITK scales them)
    sform = np.asarray(header["sform"], np.float32).astype(float)
    qform = np.asarray(header["qform"], np.float32).astype(float)

    def orthonormal(matrix):
        columns = matrix[:3, :3] / np.linalg.norm(matrix[:3, :3], axis=0)
        return np.abs(columns @ columns.T - np.eye(3)).max() <= 1e-5  # ITK: 1e-4 (a margin keeps clear of its edge)

    sformCode, qformCode = header["sform_code"], header["qform_code"]
    if sformCode > 0 and not orthonormal(sform):
        return None
    if sformCode > 0 and (qformCode <= 0 or sformCode == 1):  # NIFTI_XFORM_SCANNER_ANAT
        chosen = sform
    elif sformCode <= 0:
        chosen = qform
    elif np.array_equal(sform, qform):
        chosen = sform  # ITK may take either: it is the same matrix
    else:
        return None
    affine = np.eye(4)
    affine[:3, :3] = chosen[:3, :3] / np.linalg.norm(chosen[:3, :3], axis=0) * pixdim[np.newaxis, :]
    affine[:3, 3] = chosen[:3, 3]
    return affine


def _readPrefetchCrop(sid, folders):
    """The first figure crop of a case (``sources/<record or sid>/fig-<k>.png`` whose fit names the case or nobody), as a
    PNG thumbnail (worker): ``{"path", "png": bytes, "size": (w, h)}`` or None.
    """
    import io

    from PIL import Image

    for folder in folders:
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not (name.startswith("fig-") and name.endswith("_fit.json")):
                continue
            fitPath = os.path.join(folder, name)
            try:
                with open(fitPath, encoding="utf-8") as f:
                    owner = json.load(f).get("participant_id")
            except (OSError, ValueError, AttributeError):
                continue
            pngPath = fitPath[:-len("_fit.json")] + ".png"
            if owner not in (None, sid) or not os.path.isfile(pngPath):
                continue
            with Image.open(pngPath) as image:
                size = image.size
                thumbnail = image.convert("RGBA")
                thumbnail.thumbnail((CROP_THUMBNAIL_PX, CROP_THUMBNAIL_PX))
                buffer = io.BytesIO()
                thumbnail.save(buffer, format="PNG")
            return {"path": pngPath, "fit": fitPath, "png": buffer.getvalue(), "size": size}
    return None


_pillowWarm = [False]


def _warmPillow():
    """Run Pillow's lazy imports (its plug-ins) on the main thread once: an import in the worker would run the import
    machinery there (``sys.meta_path`` holds the finder of vtkmodules).
    """
    if _pillowWarm[0]:
        return
    _pillowWarm[0] = True
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4)).save(buffer, format="PNG")
    buffer.seek(0)
    with Image.open(buffer) as image:
        thumbnail = image.convert("RGBA")
        thumbnail.thumbnail((2, 2))
        thumbnail.save(io.BytesIO(), format="PNG")


def _prefetchCase(request):
    """Worker job of ``StudySession._schedulePrefetch``: read the next case's lesion array and figure crop."""
    guard = _WorkerGuard() if _guard["enabled"] else contextlib.nullcontext()
    with guard:
        result = {"sid": request["sid"], "lesion": None, "crop": None, "thread": threading.current_thread().name}
        path = request.get("lesion")
        if path:
            try:
                stat = os.stat(path)
                lesion = _readPrefetchLesion(path)
                lesion.update({"path": path, "stat": (stat.st_mtime_ns, stat.st_size)})
                result["lesion"] = lesion
            except Exception as error:
                result["lesion"] = {"path": path, "fallback": f"unreadable: {error}"}
        try:
            result["crop"] = _readPrefetchCrop(request["sid"], request.get("figureFolders") or [])
        except Exception as error:
            result["cropError"] = str(error)
        return result


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
        self._sidecarText = ("", "")  # the Notes and SourceReference of the current lesion's sidecar (see _sidecarTextChanged)
        self._dirty = False
        self._markedDirty = False  # markDirty(): an edit that the lesion's voxels do not show (always written)
        self._activeSeconds = 0.0
        self._lastActivity = None
        self._doneHooks = []  # [(order, sequence, fn)]
        self._openHooks = []
        self._swallowNext = False
        self._lastSaveTime = None
        self._lastSaveError = None
        self.lastInboxRejected = []  # [(file, participant id or None, message)] that the last applyInbox() rejected
        self.lastInboxHeld = []  # [(file, [participant ids])] that the last applyInbox() held back (the open subject's edits)
        self._inboxReloading = False
        self._inboxRetryTimer = qt.QTimer()
        self._inboxRetryTimer.setSingleShot(True)
        self._inboxRetryTimer.setInterval(INBOX_RETRY_MS)
        self._inboxRetryTimer.connect("timeout()", self._onInboxRetry)
        self._removed = []  # undo records of "Remove from study"
        self.creationReport = None
        self._gridRows = {}
        self._metricsWriter = None
        self._metricsPending = set()
        self._closed = False
        self._queue = None  # the active trace queue: {"key", "record"} (L-45)
        self._queuePosition = None  # the last subject opened while the queue was active
        self._prefetched = {}  # sid -> result of _prefetchCase (main thread only)
        self._prefetchPending = set()
        self._cropCache = {}  # sid -> figure crop thumbnail of the current case ({} when it has none)
        self.prefetchEnabled = True
        self.lastSwitchSeconds = None
        self.lastSwitchPrefetched = False
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
        self._otherSession = None  # the last heartbeat seen of another live session of this study (two windows)
        self._warnedSessions = set()  # (host, pid) of the other sessions this one was told about
        self._lesionStamp = None  # (normcase path, mtime_ns, size) of the current lesion file as read or written here
        self._openedEpoch = None  # time.time() when the current subject was opened

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
        """Start the heartbeat, merge the inbox, ask the pending grid questions and compute the missing lesion metrics.

        The trace queue and the position of the last session (``.studio/session.json``) are restored first.
        """
        self._restoreQueue()
        # Another window with this study open: said now, before this session's heartbeat replaces the other one's
        self._noticeOtherSession(_liveOtherSession(self._readSessionRecord()))
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
        """Write the current subject's edits, save participants.tsv, remove the session's nodes and stop the heartbeat
        (``.studio/session.json`` is kept with ``heartbeat_epoch`` 0 and the trace queue).
        """
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
        self._inboxRetryTimer.stop()
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
        self._prefetched.clear()
        # The heartbeat stops (epoch 0: no program takes the study for open); the trace queue and its position stay for the
        # next session
        self._writeSessionRecord(closed=True)
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
        self._writeSessionRecord()

    def _readSessionRecord(self):
        try:
            with open(self._sessionFile(), encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def _noticeOtherSession(self, other):
        """Another live session has this study open (``other``: its heartbeat): remember it, and say so once per session."""
        if other is None:
            return
        self._otherSession = other
        key = (other.get("host"), other.get("pid"))
        if key in self._warnedSessions:
            return
        self._warnedSessions.add(key)
        logging.warning(f"Studio: the study {self.study.name} is also open in process {other.get('pid')} on {other.get('host')}")
        _toast(self._shell, _("Study {name} is also open in another Slicer Studio window (process {pid} on {host}). Trace each "
                              "subject in one window only.").format(name=self.study.name, pid=other.get("pid"), host=other.get("host")),
               ms=12000, level="warning")

    def _writeSessionRecord(self, closed=False):
        """``.studio/session.json``: the heartbeat while the study is open (``heartbeat_epoch`` 0 once closed), the
        current subject and the trace queue with its position (L-45).

        Another live session of the study (a second window) is noticed here; closing never marks it closed: its heartbeat
        is left (or put back) for ``pipeline init`` and the next session.
        """
        other = _liveOtherSession(self._readSessionRecord())
        if closed:
            if other is not None:
                return
            if _liveOtherSession(self._otherSession) is not None:
                try:
                    _lnm("schema").write_text_atomic(self._sessionFile(), json.dumps(self._otherSession) + "\n")
                except OSError:
                    logging.warning(f"Studio: cannot write the study heartbeat:\n{traceback.format_exc()}")
                return
        else:
            self._noticeOtherSession(other)
        content = {"format": "slicer-studio-session", "pid": os.getpid(), "host": socket.gethostname(), "app": "Slicer Studio",
                   "started": self._startedAt, "heartbeat": _now(), "heartbeat_epoch": 0 if closed else time.time(), "subject": self._current}
        if closed:
            content["closed"] = _now()
        if self._queue is not None:
            content["queue"] = {"key": self._queue["key"], "record": self._queue.get("record"), "position": self._queuePosition}
        try:
            path = self._sessionFile()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _lnm("schema").write_text_atomic(path, json.dumps(content) + "\n")
        except OSError:
            logging.warning(f"Studio: cannot write the study heartbeat:\n{traceback.format_exc()}")

    def _restoreQueue(self):
        """The trace queue and position recorded by the last session of this study (``.studio/session.json``)."""
        try:
            with open(self._sessionFile(), encoding="utf-8") as f:
                record = json.load(f).get("queue")
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(record, dict) or queueSpec(record.get("key"), record.get("record")) is None:
            return
        if record["key"] == QUEUE_PAPER and not any(recordOf(row) == record.get("record") for row in self.study.subjects):
            return  # the paper has no case any more (its rows were removed or its DOI changed)
        self._queue = {"key": record["key"], "record": record.get("record")}
        position = record.get("position")
        self._queuePosition = position if position in self.study else None

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
            _notifySaved(self)
            return False
        self._lastSaveTime = time.time()
        self._lastSaveError = None
        _notifySaved(self)  # the view drew "Saving…" while the save timer was active
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

    def applyInbox(self, report=True):
        """Merge the values that jobs posted to .studio/inbox (on open, when Library is shown, on request). Return the files applied.

        Files that the merge rejects (unknown subject, invalid value, unreadable file) are moved to
        ``.studio/inbox/rejected/``; ``lastInboxRejected`` lists them and, with ``report``, a toast says that
        they were not merged and why (``inboxRejectedText()``).

        A result that changes the open subject's scan, lesion or space (a normalization, a warp) never applies under the
        open scene, which Done would then write with the new row's name and space: while the subject has edits that are
        not written (or a case strip field is being edited), its file stays in the inbox (``lastInboxHeld``) and the merge is
        retried every ``INBOX_RETRY_MS``; otherwise the subject is left (its notes written), the result merged and the
        subject opened again.
        """
        jobs = _lnm("jobs")
        known = len(self.study.inbox_errors)
        current = self._current
        # The rows of the subjects whose results waited: a lesion written meanwhile is kept as their native lesion
        waited = {sid: dict(self.study.subject(sid)) for _path, sids in self.lastInboxHeld for sid in sids if sid in self.study}
        try:
            applied = self.study.apply_inbox(save=False, hold=self._inboxHold(current))
            held = list(self.study.inbox_held)
            if held and not self._inboxReloading and not self.isDirty() and not _caseStripEditing() and self._leaveForInbox():
                # The subject is closed: nothing of its scene can be written with the new row
                try:
                    applied += self.study.apply_inbox(save=False)
                    held = list(self.study.inbox_held)
                finally:
                    self._reopenAfterInbox(current)
            applied += self._keepWrittenLesions(waited, applied)
        except Exception:
            logging.error(f"Studio: the study inbox could not be merged:\n{traceback.format_exc()}")
            return []
        self.lastInboxHeld = held
        if held and not self._closed:
            self._inboxRetryTimer.start()
        self.lastInboxRejected = list(self.study.inbox_errors[known:])
        if applied:
            try:
                self.study.save(lock_timeout=SAVE_LOCK_TIMEOUT_S)
                self._lastSaveTime = time.time()
            except jobs.Locked:
                self.scheduleSave()
            _notify(self)
            if not self._closed:
                # A result that repoints the lesion or space (normalization, warp) leaves the metrics of the native file on the row
                self.computeMissingMetrics()
        if self.lastInboxRejected and report:
            _toast(self._shell, self.inboxRejectedText(), ms=9000, level="warning")
        return applied

    def _inboxHold(self, current):
        """``hold`` of ``Study.apply_inbox``: values that change the scan, lesion or space of the open subject ``current``."""
        if current is None or current not in self.study:
            return None
        isMissing = _lnm("study").is_missing

        def hold(sid, values):
            if sid != current:
                return False
            row = self.study.subject(sid)
            for column in INBOX_SCENE_COLUMNS:
                if column not in values:
                    continue
                new, old = values[column], row.get(column)
                if (isMissing(new) and isMissing(old)) or (not isMissing(new) and not isMissing(old) and str(new) == str(old)):
                    continue
                return True
            return False

        return hold

    def _keepWrittenLesions(self, waited, applied):
        """A result that waited for its subject's edits moved the row off a native lesion that is not the one it was made
        from (Done wrote the tracing to another file meanwhile): that lesion becomes the row's ``lesion_native`` (the result
        is then Stale), as the normalization manager keeps a lesion edited during its job. Return the files applied.
        """
        studyModule = _lnm("study")
        isMissing = studyModule.is_missing
        merged = {sid for _path, sids in applied for sid in sids}
        kept = {}
        for sid, before in waited.items():
            lesion = before.get("lesion")
            if sid not in merged or sid not in self.study or isMissing(lesion) or not (isMissing(before.get("space")) or before.get("space") == "native"):
                continue
            row = self.study.subject(sid)
            if row.get("lesion") == lesion or row.get("lesion_native") == lesion or isMissing(row.get("space")) or row.get("space") == "native":
                continue
            kept[sid] = lesion
        if not kept:
            return []
        audit = [{"event": "norm_lesion_edited", "sid": sid, "lesion_native": path, "by": "studio", "at": _now()} for sid, path in kept.items()]
        studyModule.Study.post_results(self.study.path, {sid: {"lesion_native": path} for sid, path in kept.items()}, "inbox-edited", audit=audit)
        _toast(self._shell, _("The lesion of {sids} was edited while it was normalized: it is marked Stale (Redo or Re-warp)").format(
            sids=", ".join(sorted(kept))), ms=8000, level="consequence")
        return self.study.apply_inbox(save=False, hold=self._inboxHold(self._current))

    def _leaveForInbox(self):
        """Close the open subject (its notes and case strip are written) before a result repoints its row. Return True if closed."""
        if self._current is None:
            return False
        if not self._leave(write=True):
            return False
        self._removeSubjectNodes()
        self._current = None
        self._inboxReloading = True
        return True

    def _reopenAfterInbox(self, sid):
        """Open ``sid`` again after the merge, with the files its row points at now (the workspace stays)."""
        try:
            if sid is not None and sid in self.study and self._current is None and not self.open(sid, switchToDraw=False):
                shell = _currentShell(self._shell)
                if shell is not None:
                    # The breadcrumb never names a subject that is not open
                    shell.setBreadcrumb(self.study.name, None, onClick=lambda: showSubjectSwitcher(shell))
        finally:
            self._inboxReloading = False

    def _onInboxRetry(self):
        if not self._closed and self.lastInboxHeld:
            self.applyInbox()

    def inboxRejectedText(self, rejected=None):
        """Sentence for the job results that the last merge rejected ("" when none): how many files and why."""
        rejected = self.lastInboxRejected if rejected is None else rejected
        files = list(dict.fromkeys(path for path, _sid, _message in rejected))
        if not files:
            return ""
        reasons = []
        for _path, sid, message in rejected:
            message = message if len(message) <= 80 else message[:77] + "…"
            reason = f"{sid}: {message}" if sid else message
            if reason not in reasons:
                reasons.append(reason)
        shown = "; ".join(reasons[:3]) + (" …" if len(reasons) > 3 else "")
        if len(files) == 1:
            return _("1 job result was not merged ({reasons}). It is kept in .studio/inbox/rejected.").format(reasons=shown)
        return _("{n} job results were not merged ({reasons}). They are kept in .studio/inbox/rejected.").format(n=len(files), reasons=shown)

    mergeInbox = applyInbox

    # Rows

    def setValues(self, sids, values):
        """Set cells of several subjects (validated, see ``Study.set``) and save soon."""
        for sid in sids:
            self.study.set(sid, values)
            # A figure saved for the case (L-29 writes its columns) shows in the case strip
            self._cropCache.pop(sid, None)
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
        if source.startswith("user:"):
            # The user's own template (A-10), found by id and checksum; else the built-in template of its family
            node = self._userTemplateNode(record)
            self._templateCache[source] = node.GetID()
            return node
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

    def _userTemplateNode(self, record):
        """The volume of the study's own template (``templates.templateForStudyRecord``). When it is not on this computer, the
        notice "This study uses the template … Add it…" is shown and the built-in template of its family is used meanwhile.
        """
        from . import templates

        importer = _importer()
        template = templates.templateForStudyRecord(record)
        if template is not None:
            with importer._ownLoad():
                node = importer.loadTemplateVolume(template)
            node.SetAttribute(SUBJECT_ATTRIBUTE, "template")
            self._templateFiles[node.GetID()] = template.imagePath
            return node
        self._offerMissingTemplate(record)
        try:
            from LesionNetworkMappingLib import geometry

            family = geometry.space_family(record.get("space") or record.get("id"))
        except Exception:
            family = None
        sampleName = DEFAULT_TEMPLATE_SAMPLE
        if family == "fsl6" and importer.cachedTemplatePath(NLIN6_2MM_SAMPLE) is not None:
            sampleName = NLIN6_2MM_SAMPLE
        node = importer.studyTemplateVolume(sampleName)
        self._templateFiles[node.GetID()] = _storagePath(node) or sampleFile(sampleName)
        return node

    def _tagTemplateLesionSpace(self, row, segmentationNode):
        """Tag a lesion drawn on the study's own template with that template's space (``Studio.Space``) when its grid is not
        recognized: the template is not on this computer (A-10 step 5: the case opens on the built-in template of its family
        with the mismatch notice, and the lesion stays an MNI lesion, as the study, participants.tsv and the sidecar say).
        """
        from . import templates

        record = self.templateRecord()
        if not templates.isUserStudyRecord(record) or segmentationNode.GetAttribute(space.SPACE_ATTRIBUTE):
            return
        recordSpace = str(record.get("space") or record.get("id") or "")
        rowSpace = row.get("space")
        if not recordSpace or recordSpace.lower() in ("native", "scanner", "orig", "individual"):
            return
        if not _missing(rowSpace) and str(rowSpace) != recordSpace:
            return
        if space.spaceOf(segmentationNode) is not None:
            return
        resolution = record.get("res")
        grid = space.gridOf(segmentationNode)
        box = space.worldBox(*grid) if grid is not None else None
        try:
            resolution = float(resolution) if resolution is not None else None
        except (TypeError, ValueError):
            resolution = None
        if resolution is not None and (box is None or abs(float(box[1].min()) - resolution) > 0.01):
            # Not on a grid of that template's voxel size: the lesion file says nothing reliable about its space
            return
        value = f"{recordSpace} {resolution:g}" if resolution is not None else recordSpace
        segmentationNode.SetAttribute(space.SPACE_ATTRIBUTE, value)

    def _offerMissingTemplate(self, record):
        """The notice of a study template that is not on this computer (once per session); Add it… reopens the case on it."""
        if getattr(self, "_missingTemplateOffered", False):
            return
        self._missingTemplateOffered = True
        logging.warning(f"Slicer Studio: the study template {record.get('name') or record.get('source')} is not on this computer")
        try:
            from . import templates_ui

            def added(template):
                # The cached fallback is forgotten: the next case (or this one, reopened) uses the added template
                self._templateCache.pop(str(record.get("source")), None)
                self._missingTemplateOffered = False

            templates_ui.offerMissingStudyTemplate(record, _currentShell(), onAdded=added)
        except Exception:
            logging.error(f"Slicer Studio: the missing template notice failed:\n{traceback.format_exc()}")

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

    # Trace queues (L-45)

    def queue(self):
        """The active trace queue (``queueSpec()`` dict), or None (W3-07's order)."""
        if self._queue is None:
            return None
        return queueSpec(self._queue["key"], self._queue.get("record"))

    def setQueue(self, key, record=None):
        """Make ``key`` (a ``QUEUE_*`` key or a Library extension queue key; None: no queue) the active trace queue.

        "This paper" takes the paper of ``record`` (a ``recordOf()`` value), else of the current case. The queue and its
        position are kept in ``.studio/session.json``. Return the queue spec (None when cleared).
        :raises ValueError: an unknown queue, or "This paper" without a paper.
        """
        if key is None:
            self._queue = None
            self._queuePosition = None
        else:
            if key == QUEUE_PAPER and record is None and self._current is not None:
                record = recordOf(self.study.subject(self._current))
            spec = queueSpec(key, record)
            if spec is None:
                raise ValueError(_("This case names no paper (DOI or record)") if key == QUEUE_PAPER else f"Unknown queue {key!r}")
            self._queue = {"key": key, "record": spec.get("record")}
            self._queuePosition = self._current if self._current is not None and _safe(spec["predicate"], self.study.subject(self._current)) else None
        self._writeHeartbeat()
        self._schedulePrefetch()
        _notify(self)
        return self.queue()

    def queueLabel(self, queue=None):
        """The name of a queue on screen (default: the active one): "Untraced", "This paper · Smith et al. 2019"; "All
        subjects" without one.
        """
        spec = self._resolveQueue(queue)
        if spec is None:
            return _("All subjects")
        if spec.get("key") == QUEUE_PAPER:
            for row in self.study.subjects:
                if recordOf(row) == spec.get("record"):
                    return f"{spec['label']} · {paperLabel(row)}"
        return spec["label"]

    def queueMembers(self, queue=None):
        """participant_ids of a queue (default: the active one; None without one: every subject), in table order."""
        spec = self._resolveQueue(queue)
        rows = self.study.subjects
        if spec is None:
            return [row["participant_id"] for row in rows]
        return [row["participant_id"] for row in rows if _safe(spec["predicate"], row)]

    def queuePosition(self, queue=None):
        """``(position, count)`` of the current subject in a queue (default: the active one): position is 1-based, None when
        the subject is not in the queue.
        """
        members = self.queueMembers(queue)
        position = members.index(self._current) + 1 if self._current in members else None
        return position, len(members)

    def _resolveQueue(self, queue):
        if queue is None:
            return self.queue()
        if isinstance(queue, dict):
            return queue
        if callable(queue):
            return {"key": None, "label": "", "record": None, "predicate": queue, "todo": queue}
        record = recordOf(self.study.subject(self._current)) if queue == QUEUE_PAPER and self._current in self.study else None
        spec = queueSpec(queue, record)
        if spec is None:
            raise KeyError(f"Unknown queue {queue!r}")
        return spec

    def next_subject(self, after=None, queue=None):
        """The subject that Done → next opens after ``after`` (default: the current subject, else the queue position).

        Generalizes ``Study.next_subject(after, status)`` (W3-07): with a queue (a key, a spec, a predicate ``fn(row)``, or
        by default the active queue) the first subject after ``after`` in table order, wrapping around, that the queue still
        has to do; without one, the next untraced subject (W3-07), and when none is left the next one still in progress (a
        subject left half-traced by Alt+←/→ is not done, so Done never reports a study traced while one is open). Return its
        participant_id, or None.
        """
        after = after if after is not None else (self._current or self._queuePosition)
        spec = self._resolveQueue(queue)
        if spec is None:
            return self.study.next_subject(after, status="untraced") or self.study.next_subject(after, status="inprogress")
        ids = self.study.subject_ids
        start = ids.index(after) + 1 if after in ids else 0
        for sid in ids[start:] + ids[:start]:
            if sid != after and _safe(spec["todo"], self.study.subject(sid)):
                return sid
        return None

    def _stepTarget(self, delta, queue=None):
        """The subject Alt+→ (``delta`` 1) or Alt+← (-1) opens in a queue (default: the active one), without wrapping."""
        spec = self._resolveQueue(queue)
        ids = self.study.subject_ids
        current = self._current or self._queuePosition
        start = ids.index(current) if current in ids else (-1 if delta > 0 else len(ids))
        index = start + delta
        while 0 <= index < len(ids):
            if spec is None or _safe(spec["predicate"], self.study.subject(ids[index])):
                return ids[index]
            index += delta
        return None

    # Next-case prefetch (L-45): the worker reads files only; the nodes are made on the main thread when the case is opened

    def _schedulePrefetch(self):
        """Read the lesion and the figure crop of the cases that Done → next and Alt+→ would open, in the background."""
        if self._closed or not self.prefetchEnabled or self._current is None:
            return
        try:
            targets = [self.next_subject(self._current), self._stepTarget(1)]
        except Exception:
            logging.error(f"Studio: the next case could not be found:\n{traceback.format_exc()}")
            return
        targets = [sid for sid in dict.fromkeys(targets) if sid is not None and sid != self._current]
        for sid in list(self._prefetched):
            if sid not in targets:
                del self._prefetched[sid]
        studyModule = _lnm("study")
        for sid in targets:
            if sid in self._prefetched or sid in self._prefetchPending:
                continue
            row = self.study.subject(sid)
            lesion = self.study.abspath(row.get("lesion"))
            folders = [os.path.join(self.study.path, "sources", str(name)) for name in dict.fromkeys(
                value for value in (row.get("record_id"), sid) if not studyModule.is_missing(value))]
            request = {"sid": sid, "lesion": lesion if lesion and _isNifti(lesion) else None, "figureFolders": folders}
            _warmPillow()

            self._prefetchPending.add(sid)
            try:
                from . import autosave_io

                writer = autosave_io.backgroundWriter()
            except ImportError:
                self._prefetchPending.discard(sid)
                return
            writer.submit(_prefetchCase, request, onDone=self._onPrefetched,
                          onError=lambda error, sid=sid: self._onPrefetchError(sid, error))

    def _onPrefetched(self, result):
        sid = result["sid"]
        self._prefetchPending.discard(sid)
        if self._closed or sid == self._current or sid not in self.study:
            return
        self._prefetched[sid] = result
        while len(self._prefetched) > PREFETCH_KEEP:
            del self._prefetched[next(iter(self._prefetched))]

    def _onPrefetchError(self, sid, error):
        self._prefetchPending.discard(sid)
        logging.warning(f"Studio: the next case {sid} could not be read ahead: {error}")

    def waitForPrefetch(self, timeoutS=10.0):
        """Wait until the scheduled prefetch jobs are done (tests and measurements). Return True if they are."""
        deadline = time.monotonic() + timeoutS
        try:
            from . import autosave_io

            writer = autosave_io.backgroundWriter()
        except ImportError:
            return True
        while self._prefetchPending and time.monotonic() < deadline:
            writer.drain(0.05)
        return not self._prefetchPending

    def prefetched(self, sid):
        """The prefetch result of ``sid`` (``{"lesion", "crop"}``), or None."""
        return self._prefetched.get(sid)

    def figureThumbnail(self, sid=None):
        """``(png bytes, crop path, fit path)`` of the case's first figure crop, from the prefetch when it is there, else read
        now (main thread). None when the case has no saved figure.
        """
        sid = sid or self._current
        if sid is None or sid not in self.study:
            return None
        cached = self._cropCache.get(sid)
        if cached is None:
            entry = self._prefetched.get(sid)
            crop = entry.get("crop") if entry is not None else None
            if crop is None and (entry is None or "cropError" in entry):
                row = self.study.subject(sid)
                folders = [os.path.join(self.study.path, "sources", str(name)) for name in dict.fromkeys(
                    value for value in (row.get("record_id"), sid) if not _missing(value))]
                try:
                    crop = _readPrefetchCrop(sid, folders)
                except Exception:
                    logging.warning(f"Studio: the figure crop of {sid} could not be read:\n{traceback.format_exc()}")
                    crop = None
            cached = crop or {}
            self._cropCache = {sid: cached}
        if not cached:
            return None
        return cached["png"], cached["path"], cached["fit"]

    def _takePrefetchedLesion(self, sid, lesionPath):
        """The prefetched lesion of ``sid`` when it is still the file of the row, unchanged since it was read; else None."""
        entry = self._prefetched.pop(sid, None)
        if entry is not None and "cropError" not in entry:
            # The case strip shows the crop without reading it ({}: the case has no saved figure)
            self._cropCache = {sid: entry.get("crop") or {}}
        lesion = (entry or {}).get("lesion")
        if not lesion or "array" not in lesion or not lesionPath or os.path.normcase(lesion["path"]) != os.path.normcase(lesionPath):
            return None
        try:
            stat = os.stat(lesionPath)
        except OSError:
            return None
        return lesion if (stat.st_mtime_ns, stat.st_size) == tuple(lesion["stat"]) else None

    def _segmentationFromPrefetch(self, sid, lesion):
        """A lesion segmentation made from a prefetched array, as ``importer.loadLesion`` makes it from the file: one
        "Lesion" segment (Segment_1, label 1) whose binary labelmap and reference geometry are the file's grid.
        """
        import vtk.util.numpy_support

        importer = _importer()
        array = lesion["array"]
        fileMatrix = vtk.vtkMatrix4x4()
        for row in range(4):
            for column in range(4):
                fileMatrix.SetElement(row, column, float(lesion["affine"][row, column]))
        # As the segmentation reader computes it, to the last bit (the reference geometry is compared as text): vtkITK's
        # RAS-to-IJK matrix is the inverse of the file's matrix, and the reader inverts it back
        rasToIjk = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(fileMatrix, rasToIjk)
        matrix = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(rasToIjk, matrix)
        labelmap = slicer.vtkOrientedImageData()
        labelmap.SetExtent(0, array.shape[2] - 1, 0, array.shape[1] - 1, 0, array.shape[0] - 1)
        # The reader's image arrives with the file's spacing and origin, which SetImageToWorldMatrix keeps when they are
        # within machine epsilon of the matrix's
        if "spacing" in lesion:
            labelmap.SetSpacing(*lesion["spacing"])
            labelmap.SetOrigin(*(float(value) for value in lesion["affine"][:3, 3]))
        labelmap.SetImageToWorldMatrix(matrix)
        scalars = vtk.util.numpy_support.numpy_to_vtk(array.ravel(), deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
        scalars.SetName("ImageScalars")
        labelmap.GetPointData().SetScalars(scalars)
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", sid)
        segmentationNode.CreateDefaultDisplayNodes()
        segmentation = segmentationNode.GetSegmentation()
        segmentation.SetConversionParameter(slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName(),
                                            slicer.vtkSegmentationConverter.SerializeImageGeometry(labelmap))
        segment = slicer.vtkSegment()
        segment.SetName(importer.LESION_SEGMENT_NAME)
        segment.SetColor(*importer.LESION_COLOR)
        segment.SetLabelValue(1)
        segment.AddRepresentation(slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName(), labelmap)
        segmentation.AddSegment(segment, "Segment_1")
        importer.styleLesionSegment(segmentationNode, "Segment_1")
        # Draw shows "Saved · <file>" for a lesion read from its file and not changed (``draw.lesionSaveState``)
        draw = _draw()
        records = getattr(draw, "_savedRecords", None) if draw is not None else None
        if isinstance(records, dict):
            reference = vtk.vtkWeakReference()
            reference.Set(segmentationNode)
            records[segmentationNode.GetID()] = (reference, lesion["path"], _contentStamp(segmentationNode), None)
        return segmentationNode

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
        # A scan that the table names but that is not on disk (moved, renamed, a drive that is not connected) is never
        # replaced by the study template: the lesion would be traced on another image and saved as the subject's
        missingScan = self._missingFile(sid, "scan")
        if missingScan is not None:
            logging.warning(f"Studio: the scan of {sid} is missing: {missingScan[1]}")
            self._sayMissing(shell, sid, "scan", missingScan)
            return False
        missingLesion = self._missingFile(sid, "lesion")
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
        if self._queue is not None:
            spec = self.queue()
            if spec is not None and _safe(spec["predicate"], self.study.subject(sid)):
                self._queuePosition = sid
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
        # The next case of the queue is read while this one is traced
        self._schedulePrefetch()
        if missingLesion is not None:
            # Last: a traced subject shown with an empty lesion is said, and stays said until the next input
            self._sayMissing(shell, sid, "lesion", missingLesion)
        return True

    def _missingFile(self, sid, column):
        """``(table path, absolute path)`` of the ``scan`` or ``lesion`` of ``sid`` when the table names a file that is
        not on disk, else None (no file named, or the file is there).
        """
        value = self.study.subject(sid).get(column)
        if _lnm("study").is_missing(value):
            return None
        path = self.study.abspath(value)
        if path and os.path.isfile(path):
            return None
        return str(value), path or str(value)

    def _sayMissing(self, shell, sid, column, missing):
        """Tell the user that the scan or lesion file of ``sid`` is missing (a consequence: it stays until the next input)."""
        tablePath, path = missing
        if column == "scan":
            text = _("{sid} was not opened: its scan {file} is missing (moved, renamed, or on a drive that is not connected)")
        else:
            text = _("The lesion of {sid} is missing: {file} (moved, renamed, or on a drive that is not connected). "
                     "An empty lesion is shown; Done saves a new one")
        text = text.format(sid=sid, file=tablePath)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            folder = self.study.path
        _toast(shell, text, ms=8000, action=lambda: _showFolder(folder), actionText=_("Show folder"), level="consequence",
               toolTip=path)

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
            # Never the template in its place (``open()`` says so before the current subject is left)
            raise FileNotFoundError(_("the scan {file} is missing").format(file=row.get("scan")))
        if lesionPath and not os.path.isfile(lesionPath):
            logging.warning(f"Studio: the lesion of {sid} is missing: {lesionPath}")
            lesionPath = None
        previous = dict(self._nodeIds)
        previousVolume = self.volumeNode()
        self._cropCache = {}
        # The lesion file as it is before it is read: a later change of it is another window's (see _write)
        openedEpoch = time.time()
        lesionStamp = _fileStamp(lesionPath) if lesionPath else None
        prefetched = self._takePrefetchedLesion(sid, lesionPath)
        self.lastSwitchPrefetched = prefetched is not None
        with importer._ownLoad():
            if scanPath:
                volumeNode = slicer.util.loadVolume(scanPath, {"singleFile": True, "show": False})
                # The file's name, without the "_2" that the scene adds for a name it used before (an earlier subject switch)
                volumeNode.SetName(importer._baseName(scanPath))
                volumeNode.SetAttribute(SUBJECT_ATTRIBUTE, sid)
            else:
                volumeNode = self.templateNode()
            if prefetched is not None:
                segmentationNode = self._segmentationFromPrefetch(sid, prefetched)
                gridReference = lesionPath
            elif lesionPath:
                segmentationNode = importer.loadLesion(lesionPath)
                gridReference = lesionPath
            else:
                segmentationNode = importer.createLesionSegmentation(volumeNode, sid)
                gridReference = scanPath or self._templateFiles.get(volumeNode.GetID()) or _storagePath(volumeNode)
        segmentationNode.SetName(sid)
        segmentationNode.SetAttribute(SUBJECT_ATTRIBUTE, sid)
        if not scanPath and lesionPath:
            self._tagTemplateLesionSpace(row, segmentationNode)
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
        self._lesionStamp = lesionStamp
        self._openedEpoch = openedEpoch
        self._openStamp = _contentStamp(segmentationNode)
        # The Notes and source reference of the lesion's sidecar are shown again (Draw's Lesion card) and kept on rewrite
        self._sidecarText = self._restoreSidecarText(sid, segmentationNode)
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
        if write and not self.writeCurrent():
            return False
        if self._journal is not None:
            self._journal.detach()
        return True

    def writeCurrent(self):
        """Write the current subject's edits now; the subject stays open (``_leave()`` and the close prompt: a study has
        no Save). Return True when nothing is left unwritten (also when there was nothing to write), False when the
        lesion or a case strip field could not be written (a toast says why).
        """
        if self._current is None:
            return True
        self._applyPendingSmartFill()
        _commitDrawCardEdits()
        # A case strip field being edited is saved too; an invalid value keeps the case open (after the lesion is
        # written: a closing study still writes it)
        stripSaved, reason = _commitCaseStripEdit(self)
        if not stripSaved:
            _toast(self._shell, _("{sid} stays open: {reason}").format(sid=self._current, reason=reason), ms=6000, level="warning")
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
                    result = self._write(status=None, runHooks=False, mask=mask)
                    if result.get("conflict"):
                        _toast(self._shell, self._conflictText(result), ms=15000, level="consequence", toolTip=result["conflict"])
                except Blocking as e:
                    _toast(self._shell, str(e), ms=6000, level="warning")
                    return False
                except OSError as e:
                    # Expected (a folder that cannot be written, a full disk): plain words, a warning without a traceback
                    logging.warning(f"Studio: cannot write the lesion of {self._current}: {e}")
                    _toast(self._shell, _("The lesion of {sid} could not be written: {error}").format(sid=self._current, error=_plainOSError(e)),
                           ms=6000, level="warning")
                    return False
                except Exception as e:
                    logging.error(f"Studio: cannot write the lesion of {self._current}:\n{traceback.format_exc()}")
                    _toast(self._shell, _("The lesion of {sid} could not be written: {error}").format(sid=self._current, error=e), ms=6000,
                           level="warning")
                    return False
        # Notes or a source reference typed without a mask edit are written too (to the lesion's sidecar)
        segmentationNode = self.segmentationNode()
        if segmentationNode is not None and self._sidecarTextChanged(segmentationNode):
            error = self._writeSidecar(self._current, segmentationNode)
            if error is not None:
                _toast(self._shell, _("The notes of {sid} could not be written: {error}").format(sid=self._current, error=error), ms=6000,
                       level="warning")
        return stripSaved

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

    def _changedElsewhere(self, target):
        """True when the lesion file ``target`` was written by another program (a second window of the study) since this
        session opened the subject: it is not the file as this session read or wrote it, or, when this session did not
        read it, it changed after the subject was opened.
        """
        stamp = _fileStamp(target)
        if stamp is None:
            return False
        known = self._lesionStamp
        if known is not None and known[0] == stamp[0]:
            return known != stamp
        try:
            modified = os.stat(target).st_mtime
        except OSError:
            return False
        return self._openedEpoch is not None and modified >= self._openedEpoch - CONFLICT_MTIME_SLACK_S

    def noteLesionWritten(self, sid, path):
        """This program rewrote the lesion file of the open subject ``sid`` outside ``_write`` (the 2D extrusion of
        ``lesionqc.regenerate``): it is not another window's change.
        """
        if sid is not None and sid == self._current:
            self._lesionStamp = _fileStamp(path)

    def _keepConflictCopy(self, target):
        """Copy the lesion file ``target`` to ``.studio/conflicts/`` before it is replaced; return the copy's path."""
        studyModule = _lnm("study")
        folder = os.path.join(self.study.path, studyModule.PRIVATE_DIR, CONFLICTS_DIR)
        name = os.path.basename(target)
        extension = ".nii.gz" if name.lower().endswith(".nii.gz") else os.path.splitext(name)[1]
        stem = name[:-len(extension)] if extension else name
        stamp = time.strftime("%Y%m%dT%H%M%S")
        os.makedirs(folder, exist_ok=True)
        copy = os.path.join(folder, f"{stem}_{stamp}{extension}")
        counter = 2
        while os.path.exists(copy):
            copy = os.path.join(folder, f"{stem}_{stamp}-{counter}{extension}")
            counter += 1
        shutil.copy2(target, copy)
        return copy

    def _conflictText(self, result):
        """The toast of a write that kept another window's version of the lesion (``result["conflict"]``)."""
        return _("{sid} was also saved in another Slicer Studio window. Your tracing is saved; the other version is kept as "
                 "{copy}").format(sid=result["sid"], copy=self.study.relpath(result["conflict"]))

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
        # An approval (or rejection) is for the voxels that were reviewed: a write without Done (leaving the subject, the
        # close prompt) that changes them reopens the review (→ pending, audited); on Done the review Done hook does it
        reopenReview = None if runHooks else self._reviewAfterEdit(row, mask)
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
        conflict = None
        try:
            nifti.write_like(temporary, array, ijkToRas, reference, dtype=np.uint8, descrip="Slicer Studio lesion")
            # Another window wrote this subject's lesion since it was opened here: its version is kept, not lost
            if self._changedElsewhere(target):
                try:
                    conflict = self._keepConflictCopy(target)
                except OSError as e:
                    raise Blocking(_("The lesion of {sid} was changed in another window and could not be kept: {error}").format(sid=sid, error=e)) from e
                logging.warning(f"Studio: the lesion of {sid} was changed by another program; that version was kept as {conflict}")
            _replaceFile(temporary, target)
        except ValueError as e:
            raise Blocking(_("The lesion of {sid} cannot be written on its grid: {error}").format(sid=sid, error=e)) from e
        finally:
            with contextlib.suppress(OSError):
                os.remove(temporary)
        self._lesionStamp = _fileStamp(target)
        self._prefetched.pop(sid, None)
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
        reviewValues = self._reopenReview(sid, reopenReview) if reopenReview else None
        self._writeSidecar(sid, segmentationNode, studio=reviewValues)
        self.flush(lockTimeout=SAVE_LOCK_TIMEOUT_S)
        # The written file now defines the grid; the edits are written
        self._gridReference = target
        self._openStamp = _contentStamp(segmentationNode)
        self._recordSaved(segmentationNode, target)
        self._dirty = False
        self._markedDirty = False
        self._activeSeconds = 0.0
        self._lastActivity = None
        if self._journal is not None:
            self._journal.discard(sid)
        _notify(self)
        return {"sid": sid, "path": target, "values": updates, "conflict": conflict}

    def _reviewAfterEdit(self, row, mask):
        """The review state of the current subject after its lesion is written with ``mask`` without Done: "pending" when
        an approved or rejected lesion gets other voxels (``review.status_after_done``, edited), else None (kept).
        """
        rules = _reviewRules()
        if rules is None or rules.status_of(row) not in ("approved", "rejected") or self._sameAsLesionFile(mask):
            return None
        return rules.status_after_done(self.study, row, edited=True)

    def _reopenReview(self, sid, target):
        """Move the review of ``sid`` to ``target`` after its reviewed lesion was rewritten without Done (audited, ``via``
        "leave"). Return the ``SlicerStudio`` sidecar values of the new review state, or None when it was not recorded.
        """
        rules = _lnm("review")
        try:
            from . import review as studioReview

            who = studioReview.storedRaterName() or studioReview.UNNAMED
        except Exception:
            who = "unnamed"
        try:
            rules.apply_transition(self.study, sid, target, who, extra={"via": "leave", "edited": True})
        except Exception:
            logging.error(f"Studio: the review of {sid} could not be reopened after its lesion changed:\n{traceback.format_exc()}")
            return None
        return {"review": rules.sidecar_values(self.study, self.study.subject(sid))["review"]}

    @staticmethod
    def _recordSaved(segmentationNode, path):
        """Draw's save record of the lesion: it was just written to ``path`` (so the Lesion card reads "Saved · <file>"
        and the close prompt does not report changes, also when the subject stays open after Done).
        """
        draw = _draw()
        records = getattr(draw, "_savedRecords", None) if draw is not None else None
        if not isinstance(records, dict):
            return
        savedTime = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        reference = vtk.vtkWeakReference()
        reference.Set(segmentationNode)
        records[segmentationNode.GetID()] = (reference, path, _contentStamp(segmentationNode), savedTime)
        segmentationNode.SetAttribute(getattr(draw, "SAVED_FILE_ATTRIBUTE", "Studio.LesionSavedFile"), os.path.basename(path))
        segmentationNode.SetAttribute(getattr(draw, "SAVED_TIME_ATTRIBUTE", "Studio.LesionSavedTime"), savedTime)

    @staticmethod
    def _sidecarTextKeys():
        """``((sidecar key, node attribute), ...)`` of Draw's Notes and source reference (W2-05)."""
        draw = _draw()
        return (("Notes", getattr(draw, "NOTES_ATTRIBUTE", "Studio.LesionNotes")),
                ("SourceReference", getattr(draw, "SOURCE_REFERENCE_ATTRIBUTE", "Studio.LesionSourceReference")))

    def _restoreSidecarText(self, sid, segmentationNode):
        """Set the ``Notes`` and ``SourceReference`` of the subject's lesion sidecar on its lesion node (a value already on
        the node wins), as ``importer.restoreSidecarText`` does for a lesion opened alone. Return the sidecar's values.
        """
        try:
            sidecar = self.study.read_lesion_sidecar(sid) or {}
        except (OSError, ValueError):
            sidecar = {}
        values = []
        for key, attribute in self._sidecarTextKeys():
            value = sidecar.get(key)
            value = value if isinstance(value, str) else ""
            if value and segmentationNode.GetAttribute(attribute) is None:
                segmentationNode.SetAttribute(attribute, value)
            values.append(value)
        return tuple(values)

    def _sidecarTextChanged(self, segmentationNode):
        """True when the lesion's Notes or source reference differ from what its sidecar holds."""
        current = tuple(segmentationNode.GetAttribute(attribute) or "" for _key, attribute in self._sidecarTextKeys())
        return current != tuple(self._sidecarText)

    def _writeSidecar(self, sid, segmentationNode, studio=None):
        """The BIDS sidecar of the lesion (``Study.write_lesion_sidecar``) with Draw's Notes and source reference (W2-05).
        A value cleared on the lesion is removed from the sidecar. ``studio``: more ``SlicerStudio`` values. Return None,
        or the error when it was not written.
        """
        try:
            path = self.study.write_lesion_sidecar(sid, studio={"generated_by": f"Slicer Studio {slicer.app.applicationVersion}", **(studio or {})})
            with open(path, encoding="utf-8") as f:
                content = json.load(f)
            changed = False
            for key, attribute in self._sidecarTextKeys():
                value = segmentationNode.GetAttribute(attribute)
                if value is None:
                    continue  # never set on this lesion: the sidecar's value is kept
                if value and content.get(key) != value:
                    content[key] = value
                    changed = True
                elif not value and key in content:
                    del content[key]
                    changed = True
            if changed:
                _lnm("schema").write_text_atomic(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.warning(f"Studio: the sidecar of {sid} could not be written:\n{traceback.format_exc()}")
            return e
        if sid == self._current:
            self._sidecarText = tuple(segmentationNode.GetAttribute(attribute) or "" for _key, attribute in self._sidecarTextKeys())
        return None

    def done(self, writeLesion=True):
        """Done → next: write the lesion (status traced, metrics), then open ``next_subject()``: the next case of the active
        trace queue (L-45), else the next untraced subject. At the end of "This paper" a toast offers "New case from this
        paper".

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
        # A case strip field being edited (Ctrl+Enter from its editor) is saved first; an invalid value stops Done
        stripSaved, reason = _commitCaseStripEdit(self)
        if not stripSaved:
            _toast(shell, _("{sid} is not done: {reason}").format(sid=sid, reason=reason), ms=6000, level="warning")
            return None
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
        except OSError as e:
            logging.warning(f"Studio: Done could not write the lesion of {sid}: {e}")
            _toast(shell, _("The lesion of {sid} could not be saved: {error}").format(sid=sid, error=_plainOSError(e)), ms=6000, level="warning")
            return None
        except Exception as e:
            logging.error(f"Studio: Done failed for {sid}:\n{traceback.format_exc()}")
            _toast(shell, _("The lesion of {sid} could not be saved: {error}").format(sid=sid, error=e), ms=6000, level="warning")
            return None
        if result.get("conflict"):
            _toast(shell, self._conflictText(result), ms=15000, level="consequence", toolTip=result["conflict"])
        elif result.get("path"):
            _toast(shell, _("Saved {name}").format(name=os.path.basename(result["path"])), ms=2500, toolTip=result["path"])
        # The active trace queue decides the next case (L-45); without one, the next untraced subject (W3-07)
        spec = self.queue()
        nextSid = self.next_subject(sid)
        if nextSid is None and spec is None:
            _toast(shell, _("All subjects of {study} are traced").format(study=self.study.name), ms=4000, level="consequence")
        elif nextSid is None:
            self._queueFinished(spec, sid)
        else:
            self.open(nextSid, switchToDraw=False)
        self._autosaveAfterDone()
        return result

    def _queueFinished(self, spec, sid):
        """Done on the last case of a queue: say so; for "This paper", offer the next case of the paper."""
        shell = _currentShell(self._shell)
        # The queue's paper (the case just done may be of another paper, opened while the queue was active)
        record = spec.get("record") if spec.get("key") == QUEUE_PAPER else None
        rows = [row for row in self.study.subjects if record is not None and recordOf(row) == record]
        if rows:
            source = sid if sid in self.study and recordOf(self.study.subject(sid)) == record else rows[-1]["participant_id"]
            _toast(shell, _("No more cases of {paper} to trace").format(paper=paperLabel(rows[0]) or _("this paper")), sticky=True,
                   action=lambda: self.newCaseFromPaper(source), actionText=_("New case from this paper"), level="consequence")
        else:
            _toast(shell, _("No more cases in {queue}").format(queue=self.queueLabel(spec)), ms=4000, level="consequence")

    def _autosaveAfterDone(self):
        """A case is done: autosave the session once the next case is shown (A-01; never during the switch)."""
        try:
            from . import autosave
        except ImportError:
            return
        qt.QTimer.singleShot(0, lambda: autosave.autosaveNow("case done"))

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
        if queue in BUILTIN_QUEUES:
            return self._resolveQueue(queue)["predicate"]
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
        if status is None and queue is None and self._queue is not None:
            # Alt+←/→ follow the active trace queue (L-45)
            spec = self.queue()
            if spec is not None:
                sid = self._stepTarget(delta, spec)
                if sid is None:
                    _toast(self._shell, (_("Last case in {queue}") if delta > 0 else _("First case in {queue}")).format(queue=self.queueLabel()))
                    return None
                return sid if self.open(sid, switchToDraw=False) else None
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

    # Case series (L-45): the next case of the same paper

    def paperCaseId(self, row):
        """The id of the next case of a paper: ``sub-<record><nn>`` (the record's letters and digits, else the citation's,
        else the case id without its trailing number), the first free ``nn`` from 01.
        """
        label = caseLabel(row.get("record_id")) if not _missing(row.get("record_id")) else ""
        if not label and not _missing(row.get("citation")):
            label = caseLabel(str(row["citation"]).split(",")[0])[:24]
        if not label:
            label = re.sub(r"\d+$", "", caseLabel(str(row.get("participant_id", ""))[4:])) or CASE_PREFIX
        number = 1
        while f"sub-{label}{number:02d}" in self.study:
            number += 1
        return f"sub-{label}{number:02d}"

    def newCaseFromPaper(self, sid=None, switchToDraw=True):
        """"New case from this paper": a new subject ``sub-<record><nn>`` with the paper's columns (``PAPER_COLUMNS``:
        record_id, doi, pmid, citation, image_modality...) and group of ``sid`` (default: the current case), placed after
        the paper's last case and opened on the study template. Return the new id.

        :raises ValueError: no case open, or a case that names no paper (record, DOI, PMID or citation).
        """
        source = sid or self._current
        if source is None or source not in self.study:
            raise ValueError(_("Open a case of the paper first"))
        row = self.study.subject(source)
        values = {column: row[column] for column in PAPER_COLUMNS if not _missing(row.get(column))}
        if not any(column in values for column in ("record_id", "doi", "pmid", "citation")):
            raise ValueError(_("{sid} names no paper yet: enter its DOI or record first").format(sid=source))
        newSid = self.paperCaseId(row)
        record = self.templateRecord()
        studyModule = _lnm("study")
        values.update({"group": row.get("group") if row.get("group") in ("case", "control") else "case", "status": "untraced",
                       "space": record.get("id") or studyModule.NA, "trace_template": record.get("id") or studyModule.NA})
        self.study.add_subject(newSid, values)
        # After the paper's last case: the cases of a paper stay together in table order (Alt+←/→, This paper)
        rows = self.study._rows
        moved = rows.pop()
        paper = recordOf(row)
        last = max((index for index, other in enumerate(rows) if recordOf(other) == paper), default=len(rows) - 1)
        rows.insert(last + 1, moved)
        self.flush()
        previous = self._current
        if not self.open(newSid, switchToDraw=switchToDraw):
            return newSid
        shell = _currentShell(self._shell)
        _toast(shell, _("New case {sid} from {paper}").format(sid=newSid, paper=paperLabel(row) or _("this paper")), ms=6000,
               action=lambda: self._undoNewCase(newSid, previous), actionText=_("Undo"), level="consequence")
        _notify(self)
        return newSid

    def _undoNewCase(self, sid, previous):
        """Undo "New case from this paper" while the new case is untouched: remove it and go back to the case it came from."""
        if sid not in self.study:
            return False
        row = self.study.subject(sid)
        if (sid == self._current and self.isDirty()) or not _missing(row.get("lesion")):
            _toast(self._shell, _("{sid} has a lesion now: remove it from the study in Library").format(sid=sid), level="warning")
            return False
        if previous in self.study:
            self.open(previous, switchToDraw=False)
        self.removeSubjects([sid])
        self._removed.pop()  # not offered by "Undo remove"
        return True

    def newCaseFromClipboardFigure(self, sid=None, shell=None):
        """"New case from clipboard figure": ``newCaseFromPaper()``, then the clipboard's image as the figure to trace from
        (the Figure card, L-29). Return the new id.

        :raises ValueError: the clipboard holds no image (nothing is created), or the case names no paper.
        """
        from . import figure

        if not figure.clipboardHasImage():
            raise ValueError(_("The clipboard holds no image: copy the figure first"))
        newSid = self.newCaseFromPaper(sid, switchToDraw=True)
        if self._current != newSid:
            # The new case could not be opened (a Done hook kept the previous one): the figure is not pasted into another case
            return newSid
        figure.pasteFigure(_currentShell(shell or self._shell))
        return newSid


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
            if sid in studyObject and studyObject.subject(sid).get("lesion") != studyObject.relpath(binary):
                # Held back while the subject's edits are not written (StudySession.applyInbox): the sidecar of the lesion that is
                # still open is not changed; the audit record keeps the warp's provenance
                logging.info(f"Studio: the warp of {sid} is merged when its edits are written")
            else:
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
        from . import ui

        self.search = qt.QLineEdit()
        self.search.setObjectName("StudioSubjectSwitcherSearch")
        self.search.setPlaceholderText(_("Search subjects"))
        ui.setClearButton(self.search)
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


def _agreementContext():
    """``{"queue", "subject"}`` of the active second tracing or adjudication (``adjudicate.state()``), or None.

    The study session has no subject then (the state released it, L-44): the autosave names the state's subject apart, so
    that recovery reopens the state, never the first tracing of a blind second tracing.
    """
    adjudicate = sys.modules.get("StudioLib.adjudicate")
    state = adjudicate.state() if adjudicate is not None else None
    if state is None or state.session is not _session or not state.current:
        return None
    return {"queue": state.queue, "subject": state.current}


def _autosaveContext():
    if _session is None:
        return {}
    study = {"path": _session.study.path, "name": _session.study.name, "subject": _session.current()}
    agreementContext = _agreementContext()
    if agreementContext is not None:
        study["subject"] = None
        study["agreement"] = agreementContext
    return {"study": study}


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
    agreementRecord = record.get("agreement") if isinstance(record.get("agreement"), dict) else None
    if agreementRecord and agreementRecord.get("subject"):
        # A second tracing or an adjudication: the study opens and the state reopens its subject (a second tracing then
        # offers its journal); the subject itself is never opened by the session (blinding)
        session = openStudy(record["path"])
        _reopenAgreement(session, agreementRecord.get("queue"), agreementRecord["subject"])
        return
    openStudy(record["path"], subject=record.get("subject") or None)


def _reopenAgreement(session, queue, sid):
    """Start the second tracing or adjudication of an autosave at ``sid`` when it still waits in its queue."""
    if session is None or sid not in session.study:
        return False
    try:
        from . import adjudicate
    except ImportError:
        return False
    row = session.study.subject(sid)
    if queue == adjudicate.QUEUE_SECOND and adjudicate.secondTodo(row):
        return adjudicate.startSecondTracing(sid=sid) is not None
    if queue == adjudicate.QUEUE_ADJUDICATE and adjudicate.needsAdjudication(row):
        return adjudicate.startAdjudication(sid=sid) is not None
    return False


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
            {"id": "study.newCaseFromPaper", "title": _("New case from this paper"), "category": _("Action"),
             "synonyms": ["case series", "next case", "same paper", "another case", "save and next case"],
             "callback": lambda: newCaseFromPaperAction(shell), "enabled": _currentCaseNamesPaper,
             "description": _("A new case with the paper's DOI, citation and modality, on the study template")},
            {"id": "study.newCaseFromClipboard", "title": _("New case from clipboard figure"), "category": _("Action"),
             "synonyms": ["paste figure", "case series", "next case figure"],
             "callback": lambda: newCaseFromClipboardFigureAction(shell),
             "enabled": lambda: _currentCaseNamesPaper() and _clipboardHasImage(),
             "description": _("A new case of the same paper, traced from the figure in the clipboard")},
            {"id": "study.queue.all", "title": _("Trace queue: All subjects"), "category": _("Action"), "synonyms": ["queue", "table order"],
             "callback": lambda: _setQueueAction(None, shell), "description": _("Done → next opens the next untraced subject")},
        ]
        row = _session.study.subject(_session.current()) if _session.current() in _session.study else None
        for spec in availableQueues(row):
            entries.append({"id": f"study.queue.{spec['key']}", "title": _("Trace queue: {queue}").format(queue=spec["label"]), "category": _("Action"),
                            "synonyms": ["queue", "next case", "worklist"], "callback": lambda key=spec["key"]: _setQueueAction(key, shell),
                            "description": _("Done → next and Alt+←/→ follow this queue")})
    return entries


def _currentCaseNamesPaper():
    if _session is None or _session.current() not in _session.study:
        return False
    row = _session.study.subject(_session.current())
    return any(not _missing(row.get(column)) for column in ("record_id", "doi", "pmid", "citation"))


def _clipboardHasImage():
    try:
        from . import figure
    except ImportError:
        return False
    return figure.clipboardHasImage()


def _runCaseAction(action, shell=None):
    """Run a case series action; its refusal (ValueError) is a toast, never a dialog."""
    try:
        return action()
    except ValueError as e:
        _toast(shell, str(e), ms=5000, level="warning")
        return None


def newCaseFromPaperAction(shell=None):
    """"New case from this paper" (Lesion card ⋯, Search): a refusal is a toast. Return the new id or None."""
    if _session is None:
        return None
    return _runCaseAction(_session.newCaseFromPaper, shell)


def newCaseFromClipboardFigureAction(shell=None):
    """"New case from clipboard figure" (Lesion card ⋯, Search): a refusal is a toast. Return the new id or None."""
    if _session is None:
        return None
    return _runCaseAction(lambda: _session.newCaseFromClipboardFigure(shell=shell), shell)


def _setQueueAction(key, shell=None):
    if _session is None:
        return None
    result = _runCaseAction(lambda: _session.setQueue(key), shell)
    if _session.queue() is None:
        _toast(shell, _("Done → next opens the next untraced subject"), ms=3000)
    else:
        _toast(shell, _("Done → next follows {queue}").format(queue=_session.queueLabel()), ms=3000)
    return result


#
# The throughput line of the STUDY card (a Library extension registered at run time: StudioLib.libcard_throughput)
#

THROUGHPUT_LIBCARD = "StudioLib.libcard_throughput"


def _createThroughputCard(studySession):
    label = qt.QLabel()
    label.setObjectName("StudioStudyThroughput")
    label.setWordWrap(True)
    label.setToolTip(_("Traced subjects, the median tracing time per case (trace_seconds) and the time left at that pace"))
    return label


def _refreshThroughputCard(widget, studySession, selectedIds=None):
    widget.setText(throughputText(studySession.study))


def _registerThroughputCard():
    """Add the throughput line to the STUDY card through the Library extension registry (``library.libcardModules``
    also lists the ``StudioLib.libcard_*`` modules that are in ``sys.modules``).
    """
    if THROUGHPUT_LIBCARD in sys.modules:
        return
    import types

    module = types.ModuleType(THROUGHPUT_LIBCARD, "Throughput line of the STUDY card (L-45; defined in StudioLib.study_ui).")
    module.CARD = {"section": "study", "order": 5, "create": _createThroughputCard, "refresh": _refreshThroughputCard}
    sys.modules[THROUGHPUT_LIBCARD] = module
    # Only a registry that was read already is read again: the extension modules are imported when Library first needs them
    library = sys.modules.get(f"{__package__}.library")
    if library is not None and getattr(library, "_libcards", None) is not None:
        library.libcardModules(reload=True)


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
    try:
        _registerThroughputCard()
    except Exception:
        logging.error(f"Studio: the study throughput line could not be added:\n{traceback.format_exc()}")
    try:
        from . import casestrip

        casestrip.install(shell)
    except Exception:
        logging.error(f"Studio: the case strip could not be installed:\n{traceback.format_exc()}")


def uninstall(shell):
    """Classic: the study stays open (the Studio module panel lists its subjects); the case strip leaves Draw."""
    global _installedShell
    _installedShell = None
    try:
        from . import palette

        palette.unregisterProvider(_paletteEntries)
    except ImportError:
        pass
    try:
        from . import casestrip

        casestrip.uninstall(shell)
    except Exception:
        logging.error(f"Studio: the case strip could not be removed:\n{traceback.format_exc()}")
