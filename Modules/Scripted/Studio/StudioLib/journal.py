"""Crash-safe journal of the lesion being traced in a study (SPEC 5.5; D19, D-A7).

Two seconds after the last change of the current subject's lesion, the main thread extracts the lesion segment's
voxels, cropped to their bounding box (the array and its IJK to RAS matrix, numpy only), and the process-wide
background writer (``autosave_io.backgroundWriter()``, one worker thread that touches only numpy arrays and files)
writes them with ``LesionNetworkMappingLib.nifti`` to ``<study>/.studio/journal/<sid>.nii.gz`` (gzip level 1, through
a temporary file and ``autosave_io.replaceWithRetry``). Only applied voxels are journaled: a pending Smart fill preview
is not part of the segment.

When a subject is opened and its journal is newer than its lesion file (Slicer stopped before the lesion was
written), a non-modal notice offers "Recover unsaved tracing (<age>)": Recover replaces the lesion with the journaled
voxels (one undo step), Discard removes the journal. Writing the lesion (Done, or leaving the subject) removes it.

The blind second tracing (``adjudicate.SecondTracingState``, L-44) has a journal of its own,
``SecondTracingJournal``: ``<study>/.studio/journal/rater-<label>/<sid>.nii.gz``, compared with the second rater's own
tracing file, and offered in the second tracing's notice when the subject is opened in it again (the first tracing is
never loaded for it). Saving the second tracing removes it.

The scene autosave (A-01) runs as well (D-A7): it covers the scene, the journal covers the subject's lesion inside the
study. ``install(shell)`` / ``uninstall(shell)`` make this module a Studio plug-in; the journal itself is created by
the study session (``study_ui.StudySession``) and also works in Classic.
"""

import contextlib
import logging
import os
import time
import traceback

import numpy as np
import qt
import slicer
from slicer.i18n import tr as _

#: The journal is written this long after the last change of the lesion
JOURNAL_DELAY_MS = 2000
JOURNAL_FOLDER = "journal"
NOTICE_KEY = "study-journal"
#: How long discarding a journal waits for the journal write that may be running
DRAIN_TIMEOUT_S = 2.0


def _lnm(name):
    import importlib

    return importlib.import_module("LesionNetworkMappingLib." + name)


def _io():
    from . import autosave_io

    return autosave_io


def _currentShell():
    from . import shell

    return shell.instance()


def ageText(seconds):
    """"just now", "2 min ago", "3 h ago", "2 days ago"."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return _("just now")
    if seconds < 3600:
        return _("{n} min ago").format(n=int(seconds // 60))
    if seconds < 86400:
        return _("{n} h ago").format(n=int(seconds // 3600))
    return _("{n} days ago").format(n=int(seconds // 86400))


def writeJournalFile(path, dataIjk, affine):
    """Write a journal NIfTI atomically (worker thread: numpy and files only). Return ``(path, time)``."""
    nifti = _lnm("nifti")
    io = _io()
    temporary = f"{path[:-7]}.{os.getpid()}.part.nii.gz"
    try:
        nifti.write_nifti(temporary, dataIjk, affine, sform_code=nifti.XFORM_SCANNER, qform_code=nifti.XFORM_SCANNER, dtype=np.uint8,
                          descrip="Slicer Studio journal", compresslevel=1)
        io.replaceWithRetry(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temporary)
    return path, time.time()


def _lesionSegmentId(segmentationNode):
    segmentation = segmentationNode.GetSegmentation()
    segmentId = segmentation.GetSegmentIdBySegmentName("Lesion")
    if not segmentId and segmentation.GetNumberOfSegments() > 0:
        segmentId = segmentation.GetNthSegmentID(0)
    return segmentId or None


class Journal:
    """The journal of a study session (``study_ui.StudySession``): one subject at a time."""

    def __init__(self, session):
        self.session = session
        self.sid = None
        self.segmentationNodeId = None
        self._generation = 0
        self._lastWriteTime = None
        self._notice = None
        self._timer = qt.QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(JOURNAL_DELAY_MS)
        self._timer.connect("timeout()", self.writeNow)

    # Paths

    def folder(self):
        studyModule = _lnm("study")
        return os.path.join(self.session.study.path, studyModule.PRIVATE_DIR, JOURNAL_FOLDER)

    def path(self, sid):
        return os.path.join(self.folder(), f"{sid}.nii.gz")

    def lastWriteTime(self):
        return self._lastWriteTime

    # Subject

    def attach(self, sid, segmentationNode):
        """Journal ``sid`` from now on; offer the recovery of a journal newer than its lesion file."""
        self.detach()
        self.sid = sid
        self.segmentationNodeId = segmentationNode.GetID() if segmentationNode is not None else None
        offer = self.pendingRecovery()
        if offer is not None:
            self._showNotice(offer)

    def detach(self):
        """Stop journaling the current subject (a pending journal write is dropped: the lesion was written or discarded)."""
        self._timer.stop()
        self._withdrawNotice()
        self.sid = None
        self.segmentationNodeId = None
        self._generation += 1

    def schedule(self):
        """The lesion changed: write the journal 2 s after the last change."""
        if self.sid is not None:
            self._timer.start()

    def pendingRecovery(self, sid=None):
        """``(sid, journal path, age in seconds)`` when the subject's journal is newer than its lesion file, else None.

        A journal older than the lesion file is stale and removed.
        """
        sid = sid or self.sid
        if sid is None:
            return None
        path = self.path(sid)
        try:
            journalTime = os.path.getmtime(path)
        except OSError:
            return None
        lesion = self.lesionFile(sid)
        lesionTime = None
        if lesion:
            with contextlib.suppress(OSError):
                lesionTime = os.path.getmtime(lesion)
        if lesionTime is not None and lesionTime >= journalTime:
            with contextlib.suppress(OSError):
                os.remove(path)
            return None
        return sid, path, time.time() - journalTime

    def lesionFile(self, sid):
        """The file that the journal of ``sid`` is compared with (the subject's lesion), or None."""
        study = self.session.study
        if sid not in study:
            return None
        return study.abspath(study.subject(sid).get("lesion"))

    # Writing (main thread: extraction; worker: file)

    def _segmentationNode(self):
        node = slicer.mrmlScene.GetNodeByID(self.segmentationNodeId) if self.segmentationNodeId else None
        return node if node is not None and node.IsA("vtkMRMLSegmentationNode") else None

    def writeNow(self):
        """Extract the lesion now and hand the file write to the background writer. Return True if a write was queued."""
        from . import study_ui

        self._timer.stop()
        segmentationNode = self._segmentationNode()
        if self.sid is None or segmentationNode is None:
            return False
        segmentId = _lesionSegmentId(segmentationNode)
        data = study_ui._internalArray(segmentationNode, segmentId) if segmentId else None
        cropped = study_ui._cropped(*data) if data is not None else None
        if cropped is None:
            # Everything was erased: a single empty voxel at the grid origin records it
            dataIjk = np.zeros((1, 1, 1), np.uint8)
            matrix = data[1] if data is not None else np.eye(4)
        else:
            mask, matrix = cropped
            dataIjk = np.ascontiguousarray(np.transpose(mask, (2, 1, 0))).astype(np.uint8)
        path = self.path(self.sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        generation = self._generation
        _io().backgroundWriter().submit(writeJournalFile, path, dataIjk, np.array(matrix, dtype=float),
                                        onDone=lambda result: self._onWritten(generation, result),
                                        onError=lambda error: logging.warning(f"Studio: the study journal could not be written: {error}"))
        return True

    def _onWritten(self, generation, result):
        path, writtenTime = result
        if generation != self._generation:
            # The subject was written (or left) meanwhile: this journal is out of date
            with contextlib.suppress(OSError):
                os.remove(path)
            return
        self._lastWriteTime = writtenTime

    def discard(self, sid=None):
        """Remove a subject's journal (its lesion was written)."""
        sid = sid or self.sid
        self._timer.stop()
        self._generation += 1
        _io().backgroundWriter().drain(DRAIN_TIMEOUT_S)
        if sid is not None:
            with contextlib.suppress(OSError):
                os.remove(self.path(sid))
        self._withdrawNotice()

    # Recovery

    def recover(self, sid=None):
        """Replace the current lesion with the journaled voxels (one undo step). Return True if recovered."""
        offer = self.pendingRecovery(sid)
        segmentationNode = self._segmentationNode()
        if offer is None or segmentationNode is None or offer[0] != self.sid:
            return False
        nifti = _lnm("nifti")
        data, affine, _header = nifti.read_nifti(offer[1], apply_scaling=False)
        arrayKji = np.ascontiguousarray(np.transpose(data != 0, (2, 1, 0))).astype(np.uint8)
        segmentId = _lesionSegmentId(segmentationNode)
        if segmentId is None:
            return False
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        editor = widget.editor if widget is not None else None
        if editor is not None and editor.segmentationNode() is not None and editor.segmentationNode().GetID() == segmentationNode.GetID():
            editor.saveStateForUndo()
        volumeNode = slicer.util.addVolumeFromArray(arrayKji, ijkToRAS=affine, name=slicer.mrmlScene.GenerateUniqueName("StudioJournal"),
                                                    nodeClassName="vtkMRMLLabelMapVolumeNode")
        try:
            volumeNode.SetHideFromEditors(True)
            slicer.util.updateSegmentBinaryLabelmapFromArray(arrayKji, segmentationNode, segmentId, volumeNode)
        finally:
            displayNode = volumeNode.GetDisplayNode()
            storageNode = volumeNode.GetStorageNode()
            slicer.mrmlScene.RemoveNode(volumeNode)
            for node in (displayNode, storageNode):
                if node is not None and slicer.mrmlScene.IsNodePresent(node):
                    slicer.mrmlScene.RemoveNode(node)
        self._withdrawNotice()
        self._afterRecover()
        shell = _currentShell()
        if shell is not None:
            shell.toast(_("Recovered the unsaved tracing of {sid}").format(sid=self.sid), ms=3000)
        return True

    def _afterRecover(self):
        """The lesion was replaced by the journal: it has edits to write."""
        self.session.markDirty()

    def _recoverOffer(self):
        self.recover()

    def _discardOffer(self):
        sid = self.sid
        if sid is not None:
            with contextlib.suppress(OSError):
                os.remove(self.path(sid))
        self._withdrawNotice()

    def _showNotice(self, offer):
        shell = _currentShell()
        if shell is None:
            return
        from . import ui

        sid, _path, age = offer
        text = _("Recover unsaved tracing of {sid} ({age})").format(sid=sid, age=ageText(age))
        frame = ui.noticeLine(text, [(_("Recover"), self._recoverOffer), (_("Discard"), self._discardOffer)])
        frame.setObjectName("StudioJournalRecoveryNotice")
        self._notice = frame
        shell.setInspectorNotice(frame, key=NOTICE_KEY, workspace="draw")

    def _withdrawNotice(self):
        if self._notice is None:
            return
        notice, self._notice = self._notice, None
        shell = _currentShell()
        if shell is not None:
            shell.clearInspectorNotice(NOTICE_KEY)
        try:
            from . import importer

            importer.releaseWidget(notice)
        except Exception:
            logging.debug(f"Studio: journal notice release failed:\n{traceback.format_exc()}")

    def noticeWidget(self):
        return self._notice

    def dispose(self):
        self.detach()


class SecondTracingJournal(Journal):
    """The journal of a blind second tracing (``adjudicate.SecondTracingState``; L-44).

    One file per rater and subject, ``.studio/journal/rater-<label>/<sid>.nii.gz``, compared with the rater's own
    second tracing file (``rater2_lesion`` when it is this rater's). The offer is not a notice of its own: the second
    tracing's notice holds it (``noticeWidget()``; ``onNoticeChanged`` is called when it comes or goes), because the
    inspector notice slot holds one notice and the state's notice ("where am I", Exit) must stay.
    """

    def __init__(self, session, rater, onNoticeChanged=None):
        Journal.__init__(self, session)
        self.rater = rater
        self.label = _lnm("agreement").rater_label(rater)
        self.onNoticeChanged = onNoticeChanged

    def path(self, sid):
        return os.path.join(self.folder(), f"rater-{self.label}", f"{sid}.nii.gz")

    def lesionFile(self, sid):
        study = self.session.study
        if sid not in study:
            return None
        row = study.subject(sid)
        if not _lnm("agreement")._same_rater(row.get("rater2"), self.rater):
            return None
        return study.abspath(row.get("rater2_lesion"))

    def _afterRecover(self):
        """The state compares the tracing with its content stamp: the recovered voxels are unsaved edits already."""

    def _showNotice(self, offer):
        from . import ui

        sid, _path, age = offer
        text = _("Recover unsaved second tracing of {sid} ({age})").format(sid=sid, age=ageText(age))
        frame = ui.noticeLine(text, [(_("Recover"), self._recoverOffer), (_("Discard"), self._discardOffer)])
        frame.setObjectName("StudioSecondTracingRecoveryNotice")
        self._notice = frame
        self._noticeChanged()

    def _withdrawNotice(self):
        if self._notice is None:
            return
        notice, self._notice = self._notice, None
        self._noticeChanged()
        try:
            from . import importer

            importer.releaseWidget(notice)
        except Exception:
            logging.debug(f"Studio: second tracing journal notice release failed:\n{traceback.format_exc()}")

    def _noticeChanged(self):
        if self.onNoticeChanged is not None:
            try:
                self.onNoticeChanged()
            except Exception:
                logging.error(f"Studio: the second tracing notice could not be updated:\n{traceback.format_exc()}")


#
# Plug-in
#


def install(shell):
    """Studio plug-in: the journal follows the open study (``study_ui``), nothing to install."""


def uninstall(shell):
    """Classic: the notice of a pending recovery is hidden (the journal keeps running)."""
    from . import study_ui

    current = study_ui.session()
    journal = getattr(current, "_journal", None) if current is not None else None
    if journal is not None and journal.noticeWidget() is not None:
        shell.clearInspectorNotice(NOTICE_KEY)
