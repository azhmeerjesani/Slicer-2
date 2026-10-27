"""Regression tests of the problems found by the release gates (G4 -> W4-I1): features of different work packages that meet.

Run with the main window and testing enabled::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest Studio StudioRegressionTest

- ``test_1_figure_planes_kept``: a lesion traced from a figure keeps the ``planes`` column of the Figure card (the planes of the
  figure panels) when the extrusion hook sees a 3D lesion; another row loses the planes of an earlier 2D extrusion.
- ``test_2_quit_with_chips_pending``: a Slicer child process applies Studio, traces a lesion, leaves the QC evaluation pending
  (300 ms debounce), replaces a notice of the inspector slot and keeps a notice line without a parent, then quits. It must exit
  with code 0, without a Python traceback or an abnormal exit (the G4 crash: Python event filters called while the interpreter
  was finalized).
- ``test_3_map_primary_pinned``: in Studio, the primary button of Map is in the inspector's sticky footer (above the
  disclaimer), so that it never scrolls out of view or moves when a card above it grows; it is back in the Compute card in
  Classic and in the other workspaces.
- ``test_4_two_windows_same_study`` (BB-1): a study whose ``.studio/session.json`` is the live heartbeat of another process
  says so when it is opened; a lesion file that the other window wrote after this one opened the subject is copied to
  ``.studio/conflicts/`` (with a toast) before it is replaced, never lost silently; closing leaves the other session's
  heartbeat live (``pipeline init`` still refuses the study).
- ``test_5_study_notes_kept`` (BB-1): the Notes and source reference of Draw's Lesion card for a study subject are written to
  its lesion sidecar when the subject is left without a mask edit (an untraced subject too), are shown again when it is
  reopened, survive a later mask write, and a cleared value is removed from the sidecar.
- ``test_6_probabilistic_lesion_binarize`` (BB-1): a study subject whose lesion file is a probabilistic mask (float values
  below 1) opens with its voxels above 0, not as an empty lesion with unsaved changes; the Mask chip offers Binarize at 0.5,
  which keeps the voxels of at least 0.5 and the original file as ``desc-prebinarize``.
- ``test_7_adopted_status_column_kept`` (BB-1): adopting a folder whose participants.tsv has its own "status" column
  (alive / deceased) keeps it as ``status_original`` (values, position and data dictionary entry) and starts the study's
  status at untraced, so that Done -> next finds the next subject and tracing never overwrites the user's values; an
  "include" column of yes / no is kept as ``include_original`` and keeps its meaning; a status column of study values is
  the study's own column (empty cells untraced).
- ``test_8_study_list_qc_keeps_cursor`` (BB-1): a QC key (P / X / U) refreshes the subject list (a model reset) but keeps the
  keyboard cursor (the current index) on the subject, so that the next Down goes to the next subject and the next QC key
  marks that one (it went to the first row: the wrong subject was marked); a selection made elsewhere moves the cursor too.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

import numpy as np

#: Seconds for the child process of the quit test (startup, Start on template, trace, quit)
QUIT_TIMEOUT_S = 300

QUIT_SCRIPT = r"""
import sys
import time

import qt
import slicer


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        slicer.app.processEvents()
        time.sleep(0.005)


def main():
    from StudioLib import draw, importer, shell, ui

    studioShell = shell.Shell(force=True)
    studioShell.apply()
    pump(200)
    importer.startOnTemplate(shell=studioShell)
    pump(800)
    editor = slicer.modules.SegmentEditorWidget.editor
    segmentationNode, segmentId, volumeNode = editor.segmentationNode(), editor.currentSegmentID(), editor.sourceVolumeNode()
    card = draw.lesionCard()
    qc = getattr(card, "qc", None) if card is not None else None
    # A notice that is replaced: the first line is taken out of the slot (no parent) and released
    importer._postNotice("figure-convention", importer._noticeFrame("StudioQuitTestNotice", "first", [("OK", lambda: None)]), studioShell)
    pump(50)
    importer._postNotice("figure-convention", importer._noticeFrame("StudioQuitTestNotice", "second", [("OK", lambda: None)]), studioShell)
    # A notice line that never had a parent, kept until the interpreter exits (its stylesheet binding holds it)
    slicer.studioQuitTestOrphan = ui.noticeLine("orphan", [("OK", lambda: None)])
    # Trace a lesion: the card refreshes and the QC evaluation waits for its 300 ms debounce
    array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
    center = [size // 2 for size in array.shape]
    array[center[0]:center[0] + 3, center[1]:center[1] + 3, center[2] + 6:center[2] + 9] = 1
    slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
    for _ in range(4):
        slicer.app.processEvents()
    pending = bool(qc is not None and qc._timer.isActive())
    rows = bool(qc is not None and ui._isAlive(qc.panel.widget))
    print(f"QUITTEST READY qc={qc is not None} pending={pending} row={rows}", flush=True)
    slicer.app.exit(0)


try:
    main()
except Exception:
    import traceback

    traceback.print_exc()
    sys.stdout.flush()
    slicer.app.exit(3)
"""


class _MoveRecorder(qt.QObject):
    """Event filter that appends the global y of ``widget`` to ``positions`` at each of its moves."""

    def __init__(self, widget, positions):
        super().__init__()
        self._widget = widget
        self._positions = positions

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Move:
            self._positions.append(self._widget.mapToGlobal(qt.QPoint(0, 0)).y())
        return False


class StudioRegressionTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="StudioRegressionTest", dir=slicer.app.temporaryPath)
        self.addCleanup(shutil.rmtree, self.directory, True)

    # Figure planes (L-28 + L-29)

    def _study(self, rows):
        """A study with a 3D lesion (a 3 x 3 x 3 voxel cube) in ``lesions/`` for each ``(sid, cells, traced)`` of ``rows``."""
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import lesionqc

        study = Study.create(os.path.join(self.directory, "study"))
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = [-20.0, -20.0, -20.0]
        cube = np.zeros((20, 20, 20), np.uint8)
        cube[8:11, 8:11, 8:11] = 1
        for sid, cells, traced in rows:
            study.add_subject(sid, space="MNI152NLin2009cAsym")
            relative = study.lesion_relpath(sid)
            path = study.abspath(relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            nifti.write_nifti(path, cube, affine)
            if traced:
                # What an earlier 2D extrusion left next to the lesion
                shutil.copyfile(path, lesionqc.descPath(path, "traced"))
            study.set(sid, {"lesion": relative, **cells})
        study.save()
        return study

    def test_1_figure_planes_kept(self):
        from StudioLib import lesionqc

        figureCells = {"fig_convention": "radiological", "planes": "axial", "trace_method": "manual+figure", "fit_rms_mm": "0.2"}
        study = self._study([("sub-fig", figureCells, False), ("sub-fig2", {**figureCells, "fig_convention": "n/a"}, True),
                             ("sub-2d", {"planes": "axial", "n_slices": "1", "extrude_mm": "2", "dimensionality": "2D"}, True)])
        # The figure's planes are not a trace of an extrusion
        self.assertFalse(lesionqc.hasExtrusion(study, "sub-fig"))
        self.assertTrue(lesionqc.hasExtrusion(study, "sub-2d"))
        self.assertTrue(lesionqc.figureOwnsPlanes(study.subject("sub-fig")))
        self.assertTrue(lesionqc.figureOwnsPlanes({}, planeNormal=[0.0, 0.0, 1.0]))
        self.assertFalse(lesionqc.figureOwnsPlanes(study.subject("sub-2d")))
        # A 3D lesion traced from a figure (Done, with or without the figure's plane normal): the planes stay
        lesionqc.regenerate(study, "sub-fig", None, source="lesion", normal=None)
        self.assertEqual(study.subject("sub-fig")["planes"], "axial")
        self.assertEqual(study.subject("sub-fig")["dimensionality"], "3D")
        lesionqc.regenerate(study, "sub-fig2", None, source="lesion", normal=[0.0, 0.0, 1.0])
        self.assertEqual(study.subject("sub-fig2")["planes"], "axial")
        self.assertFalse(os.path.exists(lesionqc.descPath(study.abspath(study.subject("sub-fig2")["lesion"]), "traced")))
        # A lesion that is 3D again after a 2D extrusion: the extrusion columns, planes included, are cleared
        lesionqc.regenerate(study, "sub-2d", None, source="lesion", normal=None)
        row = study.subject("sub-2d")
        for key in ("planes", "n_slices", "extrude_mm"):
            self.assertTrue(lesionqc._missing(row.get(key)), f"{key} = {row.get(key)!r}")
        # The dimensionality columns of a 2D tracing leave the planes to the Figure card
        info = {"dimensionality": "2D", "planes": ["coronal"], "n_slices": {"coronal": 1}}
        self.assertEqual(lesionqc.dimensionColumns(info)["planes"], "coronal")
        self.assertNotIn("planes", lesionqc.dimensionColumns(info, figure=True))
        self.assertNotIn("planes", lesionqc.dimensionColumns(info, planeNormal=[0.0, 1.0, 0.0]))

    # Quit (W4-I1)

    def test_2_quit_with_chips_pending(self):
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "quit_with_chips.py")
        with open(script, "w") as stream:
            stream.write(QUIT_SCRIPT)
        home = os.path.join(self.directory, "home")
        os.makedirs(home)
        environment = dict(os.environ)
        environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")})
        for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
            environment.pop(key, None)
        process = subprocess.run([launcher, "--no-splash", "--testing", "--python-script", script], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
        output = process.stdout.decode("utf-8", "replace")
        with open(os.path.join(slicer.app.temporaryPath, "StudioRegressionTest_quit.log"), "w") as stream:
            stream.write(output)
        ready = [line for line in output.splitlines() if line.startswith("QUITTEST READY")]
        self.assertTrue(ready, output[-3000:])
        self.assertIn("qc=True", ready[0], ready[0])
        self.assertIn("pending=True", ready[0], ready[0])
        self.assertNotIn("Traceback", output, output[-3000:])
        self.assertNotIn("exit abnormally", output, output[-3000:])
        self.assertEqual(process.returncode, 0, json.dumps(output[-3000:]))

    # Map (W4-I1, USABILITY P5)

    def test_3_map_primary_pinned(self):
        from StudioLib import shell

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            studioShell.switchTo("map")
            for _ in range(10):
                slicer.app.processEvents()
            widget = slicer.modules.LesionNetworkMappingWidget
            button = mainWindow.findChild("QPushButton", "LNMComputeButton")
            self.assertIs(button, widget.computeButton)
            footer = mainWindow.findChild("QWidget", "StudioInspectorFooter")
            self.assertIsNotNone(footer)
            self.assertTrue(self._isAncestor(footer, button), "the primary button is in the sticky footer")
            self.assertTrue(button.isVisible())
            disclaimer = widget.shellFooter.findChild("QLabel", "LNMDisclaimer")
            self.assertIsNotNone(disclaimer)
            self.assertLess(button.mapToGlobal(qt.QPoint(0, 0)).y(), disclaimer.mapToGlobal(qt.QPoint(0, 0)).y())
            # Not inside the scrolled panel: it does not move when the panel scrolls
            self.assertIsNone(self._scrollArea(button))
            # The footer takes only the room it needs from the inspector (its disclaimer line wraps: height for width)
            footerWidget = widget.shellFooter
            needed = footerWidget.heightForWidth(footerWidget.width)
            self.assertLessEqual(footerWidget.height, needed if needed > 0 else footerWidget.sizeHint.height(), "the sticky footer takes room from the inspector")
            # The button does not move when its reason line is shown or hidden either. When the line goes away (the preflight finishing clears it just
            # before a click), not even while the layouts settle: every place it takes then is recorded
            reason = widget.computeReasonLabel
            reasonText, reasonVisible = str(reason.text), reason.isVisible()
            settled = [button.mapToGlobal(qt.QPoint(0, 0)).y()]
            whileHiding = []
            watcher = _MoveRecorder(button, whileHiding)
            try:
                for visible in (True, False, True, False):
                    reason.setText("Accept the data use terms to compute the map" if visible else "")
                    if not visible:
                        button.installEventFilter(watcher)
                    reason.setVisible(visible)
                    for _ in range(6):
                        slicer.app.processEvents()
                    button.removeEventFilter(watcher)
                    settled.append(button.mapToGlobal(qt.QPoint(0, 0)).y())
            finally:
                button.removeEventFilter(watcher)
                reason.setText(reasonText)
                reason.setVisible(reasonVisible)
            self.assertEqual(len(set(settled)), 1, f"the pinned primary button moves when its reason line changes: {settled}")
            self.assertTrue(all(y == settled[0] for y in whileHiding), f"the pinned primary button moves while its reason line goes away: {whileHiding}")
            studioShell.switchTo("library")
            for _ in range(4):
                slicer.app.processEvents()
            computeCard = widget.parent.findChild("QWidget", "LNMComputeCard")
            self.assertTrue(self._isAncestor(computeCard, button), "back in the Compute card outside Map")
            studioShell.switchTo("map")
            for _ in range(4):
                slicer.app.processEvents()
            self.assertTrue(self._isAncestor(footer, button))
            # G5: the Compute card collapsed (a map opened from a file) while the button went back into it and out again: the
            # card's event filter (ctkCollapsibleButton keeps it after the reparenting) must not hide the pinned button
            computeCard.collapsed = True
            studioShell.switchTo("library")
            for _ in range(4):
                slicer.app.processEvents()
            self.assertTrue(self._isAncestor(computeCard, button))
            studioShell.switchTo("map")
            for _ in range(4):
                slicer.app.processEvents()
            self.assertTrue(self._isAncestor(footer, button))
            self.assertTrue(button.isVisible(), "the pinned primary button is hidden after a round trip through a collapsed Compute card")
            computeCard.collapsed = True
            for _ in range(4):
                slicer.app.processEvents()
            self.assertTrue(button.isVisible(), "collapsing the Compute card hides the pinned primary button")
        finally:
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            card = slicer.modules.LesionNetworkMappingWidget.parent.findChild("QWidget", "LNMComputeCard")
            if card is not None:
                card.collapsed = False  # no state left for later tests
        widget = slicer.modules.LesionNetworkMappingWidget
        self.assertTrue(self._isAncestor(widget.parent.findChild("QWidget", "LNMComputeCard"), widget.computeButton), "Classic: in the Compute card")

    # Two windows on one study (BB-1)

    def test_4_two_windows_same_study(self):
        import glob
        import socket
        import time

        from LesionNetworkMappingLib import nifti, pipeline
        from LesionNetworkMappingLib.study import Study

        from StudioLib import study_ui

        # Two subjects on one scan file each
        folder = os.path.join(self.directory, "Two windows")
        study = Study.create(folder, name="Two windows")
        for sid in ("sub-01", "sub-02"):
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "StudioTestScan")
            z, y, x = np.mgrid[0:24, 0:28, 0:20]
            slicer.util.updateVolumeFromArray(node, (100 * np.exp(-((x - 10) ** 2 + (y - 14) ** 2 + (z - 12) ** 2) / 50.0)).astype(np.int16))
            node.SetSpacing(2.0, 2.0, 2.5)
            scan = os.path.join(self.directory, f"{sid}_T1w.nii.gz")
            self.assertTrue(slicer.util.saveNode(node, scan))
            slicer.mrmlScene.RemoveNode(node)
            study.add_subject(sid, scan=study.relpath(scan), status="untraced", group="case")
        study.save()
        sessionFile = os.path.join(folder, ".studio", "session.json")

        def otherHeartbeat(subject):
            """The heartbeat of another Slicer Studio window (a live process of this host: this process's parent)."""
            record = {"format": "slicer-studio-session", "pid": os.getppid(), "host": socket.gethostname(), "app": "Slicer Studio",
                      "heartbeat_epoch": time.time(), "subject": subject}
            os.makedirs(os.path.dirname(sessionFile), exist_ok=True)
            with open(sessionFile, "w", encoding="utf-8") as f:
                json.dump(record, f)

        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append((str(text), kwargs.get("level")))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        otherHeartbeat("sub-01")
        try:
            session = study_ui.openStudy(folder, None, subject="sub-01", showLibrary=False)
            for _ in range(4):
                slicer.app.processEvents()
            # The study is open in the other window: said when it is opened
            self.assertTrue(any("also open in another Slicer Studio window" in text and level == "warning" for text, level in toasts), toasts)

            def paint(region):
                segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
                segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
                array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
                array[region] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
                for _ in range(4):
                    slicer.app.processEvents()

            def lesionFile(sid):
                return session.study.abspath(session.study.subject(sid).get("lesion"))

            def voxels(path):
                return int((nifti.read_nifti(path)[0] > 0).sum())

            def otherWindowWrites(path, reference, box):
                """The other window's Done: its own tracing (``box``, voxel ijk slices) replaces the file."""
                data, affine, _header = nifti.read_nifti(reference)
                mask = np.zeros(data.shape[:3], np.uint8)
                mask[box] = 1
                nifti.write_nifti(path, mask, affine)
                stat = os.stat(path)
                os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5 * 10**9))

            def conflicts():
                return sorted(glob.glob(os.path.join(folder, ".studio", study_ui.CONFLICTS_DIR, "*.nii.gz")))

            # This window writes sub-01 (8 voxels): nothing to keep
            paint((slice(8, 10), slice(10, 12), slice(6, 8)))
            self.assertTrue(session.writeCurrent())
            target = lesionFile("sub-01")
            self.assertEqual(voxels(target), 8)
            self.assertEqual(conflicts(), [])
            # The other window saves its own tracing of sub-01 (27 voxels), then this window writes again
            otherWindowWrites(target, session.study.abspath(session.study.subject("sub-01")["scan"]), (slice(2, 5), slice(2, 5), slice(2, 5)))
            toasts.clear()
            paint((slice(2, 4), slice(2, 4), slice(14, 16)))
            self.assertTrue(session.writeCurrent())
            self.assertEqual(voxels(target), 16, "this window's tracing is written")
            kept = conflicts()
            self.assertEqual(len(kept), 1, "the other window's version is kept")
            self.assertEqual(voxels(kept[0]), 27)
            self.assertTrue(any("also saved in another Slicer Studio window" in text and os.path.basename(kept[0]) in text for text, _level in toasts), toasts)
            # A later write of this window's own file keeps nothing more
            paint((slice(14, 16), slice(2, 4), slice(2, 4)))
            self.assertTrue(session.writeCurrent())
            self.assertEqual(voxels(target), 24)
            self.assertEqual(len(conflicts()), 1)
            # sub-02 has no lesion when it is opened here; the other window traces it first (as Done -> next of both windows)
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            target2 = target.replace("sub-01", "sub-02")
            otherWindowWrites(target2, session.study.abspath(session.study.subject("sub-02")["scan"]), (slice(2, 5), slice(2, 5), slice(2, 5)))
            paint((slice(8, 10), slice(10, 12), slice(6, 8)))
            toasts.clear()
            self.assertIsNotNone(session.done())
            self.assertEqual(lesionFile("sub-02"), target2)
            self.assertEqual(voxels(target2), 8)
            kept = [path for path in conflicts() if os.path.basename(path).startswith("sub-02")]
            self.assertEqual(len(kept), 1)
            self.assertEqual(voxels(kept[0]), 27)
            self.assertTrue(any("also saved in another Slicer Studio window" in text for text, _level in toasts), toasts)
            # The other window's heartbeat, then this window's (which replaces the record): closing this window leaves the
            # other one's heartbeat live
            otherHeartbeat("sub-02")
            session._writeHeartbeat()
            with open(sessionFile, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["pid"], os.getpid())
        finally:
            study_ui.closeStudy()
        with open(sessionFile, encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual(record["pid"], os.getppid())
        self.assertGreater(record["heartbeat_epoch"], 0)
        self.assertIsNotNone(pipeline._session_live(folder), "pipeline init still sees the other window's session")
        # Alone (the other window's heartbeat is stale): nothing is said, and closing marks the study closed, as before
        with open(sessionFile, "w", encoding="utf-8") as f:
            json.dump({**record, "heartbeat_epoch": time.time() - 2 * study_ui.SESSION_STALE_S}, f)
        toasts.clear()
        study_ui.openStudy(folder, None, showLibrary=False)
        study_ui.closeStudy()
        self.assertFalse(any("also open" in text for text, _level in toasts), toasts)
        with open(sessionFile, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["heartbeat_epoch"], 0)

    def test_5_study_notes_kept(self):
        from LesionNetworkMappingLib.study import Study

        from StudioLib import draw, study_ui

        folder = os.path.join(self.directory, "Notes study")
        study = Study.create(folder, name="Notes study")
        for sid in ("sub-01", "sub-02"):
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "StudioTestScan")
            z, y, x = np.mgrid[0:24, 0:28, 0:20]
            slicer.util.updateVolumeFromArray(node, (100 * np.exp(-((x - 10) ** 2 + (y - 14) ** 2 + (z - 12) ** 2) / 50.0)).astype(np.int16))
            node.SetSpacing(2.0, 2.0, 2.5)
            scan = os.path.join(self.directory, f"{sid}_T1w.nii.gz")
            self.assertTrue(slicer.util.saveNode(node, scan))
            slicer.mrmlScene.RemoveNode(node)
            study.add_subject(sid, scan=study.relpath(scan), status="untraced", group="case")
        study.save()
        notesKey, referenceKey = draw.NOTES_ATTRIBUTE, draw.SOURCE_REFERENCE_ATTRIBUTE
        notes, reference = "deep WM, check slice 40", "Doe 2019 Fig 3"

        try:
            session = study_ui.openStudy(folder, None, subject="sub-01", showLibrary=False)
            for _ in range(4):
                slicer.app.processEvents()

            def paint(region):
                segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
                segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
                array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
                array[region] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
                for _ in range(4):
                    slicer.app.processEvents()

            def sidecar(sid):
                return session.study.read_lesion_sidecar(sid) or {}

            def text():
                node = session.segmentationNode()
                return node.GetAttribute(notesKey) or "", node.GetAttribute(referenceKey) or ""

            # sub-01 has a lesion file; Notes typed afterwards, no mask edit, then the subject is left
            paint((slice(8, 10), slice(10, 12), slice(6, 8)))
            self.assertTrue(session.writeCurrent())
            lesionPath = session.study.abspath(session.study.subject("sub-01")["lesion"])
            lesionStat = os.stat(lesionPath)
            self.assertNotIn("Notes", sidecar("sub-01"))
            session.segmentationNode().SetAttribute(notesKey, notes)
            session.segmentationNode().SetAttribute(referenceKey, reference)
            self.assertFalse(session.isDirty())
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertEqual(sidecar("sub-01").get("Notes"), notes, "notes typed without a mask edit are written")
            self.assertEqual(sidecar("sub-01").get("SourceReference"), reference)
            self.assertEqual(os.stat(lesionPath).st_mtime_ns, lesionStat.st_mtime_ns, "the lesion file is not rewritten")
            # sub-02 has no lesion yet: its notes are kept too
            session.segmentationNode().SetAttribute(notesKey, "untraced note")
            # Reopened: the Lesion card's values are back on the lesion
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertEqual(text(), (notes, reference))
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertEqual(text(), ("untraced note", ""))
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            # A later mask write keeps them
            paint((slice(2, 4), slice(2, 4), slice(14, 16)))
            self.assertTrue(session.writeCurrent())
            self.assertEqual(sidecar("sub-01").get("Notes"), notes)
            # A cleared value is removed from the sidecar (the other one is kept)
            session.segmentationNode().SetAttribute(notesKey, "")
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertNotIn("Notes", sidecar("sub-01"))
            self.assertEqual(sidecar("sub-01").get("SourceReference"), reference)
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertEqual(text(), ("", reference))
        finally:
            study_ui.closeStudy()

    def test_6_probabilistic_lesion_binarize(self):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import draw, lesionqc, shell, study_ui

        if slicer.util.mainWindow() is None:
            self.skipTest("needs the main window")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        data = np.zeros(grid.shape, np.float32)
        data[30:35, 50:55, 45:50] = 0.3
        data[31:34, 51:54, 46:49] = 0.8
        folder = os.path.join(self.directory, "Probabilistic study")
        os.makedirs(folder)
        path = os.path.join(folder, "sub-01_lesion.nii.gz")
        nifti.write_nifti(path, data, grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy([path], studioShell, folder=folder)
            self.assertTrue(session.open("sub-01", switchToDraw=True))
            segmentationNode = session.segmentationNode()
            self.assertEqual(draw.lesionMetrics(segmentationNode, compute=True)["voxels"], int(np.count_nonzero(data > 0)),
                             "the voxels above 0 are the lesion (the reader truncates values below 1 to 0)")
            self.assertFalse(session.isDirty())
            self.assertEqual(draw.lesionSaveState(segmentationNode)[0], "saved")
            qc = getattr(draw.lesionCard(), "qc", None)
            self.assertIsNotNone(qc)

            def actions():
                return [action["id"] for chip in (qc.chips or []) for action in chip.get("actions", [])]

            end = time.time() + 60
            while time.time() < end and "binarize" not in actions():
                lesionqc.core().waitIdle(1.0)
                slicer.app.processEvents()
                time.sleep(0.02)
            self.assertIn("binarize", actions(), qc.chips)
            self.assertTrue(qc.binarize())
            for _ in range(4):
                slicer.app.processEvents()
            self.assertEqual(draw.lesionMetrics(session.segmentationNode(), compute=True)["voxels"], int(np.count_nonzero(data >= 0.5)))
            # The original file is kept, values and all
            lesions = os.path.join(folder, "lesions")
            kept = [name for name in os.listdir(lesions) if "_desc-prebinarize_" in name]
            self.assertTrue(kept, os.listdir(lesions))
            np.testing.assert_allclose(np.squeeze(nifti.read_nifti(os.path.join(lesions, kept[0]))[0]), data)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # Adopted participants.tsv with columns named like the study's (BB-1)

    def _adopt(self, name, header, values, dictionary=None):
        """Adopt a folder of sub-01..03 T1w scans and a participants.tsv of ``header`` and ``values`` (one list per subject)."""
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        folder = os.path.join(self.directory, name)
        os.makedirs(folder)
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        paths = []
        for index in range(1, 4):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        with open(os.path.join(folder, "participants.tsv"), "w", encoding="utf-8") as f:
            f.write("\t".join(header) + "\n" + "".join("\t".join(row) + "\n" for row in values))
        if dictionary is not None:
            with open(os.path.join(folder, "participants.json"), "w", encoding="utf-8") as f:
                json.dump(dictionary, f)
        return folder, study_ui.createStudy(paths, folder=folder, openIt=False)

    def test_7_adopted_status_column_kept(self):
        from LesionNetworkMappingLib.study import read_tsv

        header = ["participant_id", "age", "status", "include"]
        values = [["sub-01", "60", "alive", "yes"], ["sub-02", "61", "deceased", "no"], ["sub-03", "62", "alive", "yes"]]
        folder, study = self._adopt("Clinical", header, values, {"status": {"Description": "Vital status"}})
        self.assertEqual(study.columns[:4], ["participant_id", "age", "status_original", "include_original"])
        self.assertEqual(study.adoptedRenames, {"status": "status_original", "include": "include_original"})
        self.assertEqual([study.subject(sid)["status"] for sid in study.subject_ids], ["untraced"] * 3)
        self.assertEqual([study.subject(sid)["include"] for sid in study.subject_ids], ["1", "0", "1"])
        # Done -> next: sub-02 is excluded (include "no"), sub-03 is the next untraced subject
        self.assertEqual(study.next_subject("sub-01"), "sub-03")
        study.set("sub-01", status="traced")
        study.save()
        columns, rows = read_tsv(os.path.join(folder, "participants.tsv"))
        self.assertEqual(columns[:4], ["participant_id", "age", "status_original", "include_original"])
        self.assertEqual([row["status_original"] for row in rows], ["alive", "deceased", "alive"])
        self.assertEqual([row["include_original"] for row in rows], ["yes", "no", "yes"])
        self.assertEqual([row["status"] for row in rows], ["traced", "untraced", "untraced"])
        with open(os.path.join(folder, "participants.json"), encoding="utf-8") as f:
            dictionary = json.load(f)
        self.assertEqual(dictionary["status_original"]["Description"], "Vital status")
        self.assertEqual(dictionary["status"]["Description"], "Tracing and mapping progress")
        # A status column of study values is the study's own column
        header = ["participant_id", "status"]
        folder, study = self._adopt("Earlier study", header, [["sub-01", "traced"], ["sub-02", "n/a"], ["sub-03", "untraced"]])
        self.assertEqual(study.adoptedRenames, {})
        self.assertNotIn("status_original", study.columns)
        self.assertEqual([study.subject(sid)["status"] for sid in study.subject_ids], ["traced", "untraced", "untraced"])
        self.assertEqual(study.next_subject("sub-01"), "sub-02")

    # Keyboard triage of the subject list (BB-1)

    def test_8_study_list_qc_keeps_cursor(self):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import library, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Triage study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 7):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        studyList = None

        def pump():
            for _ in range(6):
                slicer.app.processEvents()

        def key(code):
            slicer.app.sendEvent(table, qt.QKeyEvent(qt.QEvent.KeyPress, code, qt.Qt.NoModifier))
            pump()

        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            studyList = library.StudyList()
            studyList.widget.resize(900, 400)
            studyList.widget.show()
            studyList.refresh()
            pump()
            table, model = studyList.table, studyList.model
            self.assertEqual([model.sid(row) for row in range(model.rowCount())], [f"sub-0{index}" for index in range(1, 7)])
            # A click on sub-04 (current and selected), then P
            table.selectionModel().setCurrentIndex(model.index(3, 0), qt.QItemSelectionModel.ClearAndSelect | qt.QItemSelectionModel.Rows)
            pump()
            self.assertEqual(studyList.selectedIds(), ["sub-04"])
            studyList.setQc("pass")
            pump()
            self.assertEqual(session.study.subject("sub-04")["qc"], "pass")
            self.assertEqual(studyList.selectedIds(), ["sub-04"])
            self.assertEqual(table.currentIndex().row(), 3, "the QC key keeps the keyboard cursor on the subject")
            # Down goes on to sub-05, and X marks sub-05 (not sub-01)
            key(qt.Qt.Key_Down)
            self.assertEqual(studyList.selectedIds(), ["sub-05"])
            studyList.setQc("fail")
            pump()
            self.assertEqual(session.study.subject("sub-05")["qc"], "fail")
            self.assertNotEqual(session.study.subject("sub-01")["qc"], "fail")
            # U changes nothing on an unreviewed subject but still refreshes: the cursor stays
            key(qt.Qt.Key_Down)
            self.assertEqual(studyList.selectedIds(), ["sub-06"])
            studyList.setQc("unreviewed")
            pump()
            self.assertEqual(table.currentIndex().row(), 5)
            key(qt.Qt.Key_Up)
            self.assertEqual(studyList.selectedIds(), ["sub-05"])
            # A selection made elsewhere (another list, the grid, Draw) moves the cursor with it
            library._setStudySelection(["sub-02"])
            pump()
            self.assertEqual(studyList.selectedIds(), ["sub-02"])
            self.assertEqual(table.currentIndex().row(), 1)
            key(qt.Qt.Key_Down)
            self.assertEqual(studyList.selectedIds(), ["sub-03"])
        finally:
            if studyList is not None:
                studyList.dispose()
                studyList.widget.deleteLater()
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # Done -> next without a queue reaches the subjects left in progress (BB-1)

    def test_9_done_next_reaches_inprogress(self):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Loop study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 4):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            self.assertIsNone(session.queue())
            session.open("sub-01", switchToDraw=False)
            study = session.study
            # Untraced subjects come first; a subject left in progress (Alt+Left/Right) is the fallback
            study.set("sub-02", status="inprogress")
            self.assertEqual(session.next_subject("sub-01"), "sub-03")
            study.set("sub-03", status="traced")
            self.assertEqual(session.next_subject("sub-01"), "sub-02")
            # Done on the last untraced subject opens the one still in progress, and does not say that all are traced
            del toasts[:]
            session.done(writeLesion=False)
            self.assertEqual(study.subject("sub-01")["status"], "traced")
            self.assertEqual(session.current(), "sub-02")
            self.assertFalse([text for text in toasts if "are traced" in text], toasts)
            # Done on the last subject to do: every subject is traced
            session.done(writeLesion=False)
            self.assertEqual([study.subject(sid)["status"] for sid in study.subject_ids], ["traced"] * 3)
            self.assertIsNone(session.next_subject("sub-02"))
            self.assertTrue([text for text in toasts if "are traced" in text], toasts)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    @staticmethod
    def _isAncestor(ancestor, widget):
        current = widget
        while current is not None:
            if current == ancestor:
                return True
            current = current.parentWidget()
        return False

    @staticmethod
    def _scrollArea(widget):
        current = widget.parentWidget()
        while current is not None:
            if current.inherits("QScrollArea"):
                return current
            current = current.parentWidget()
        return None
