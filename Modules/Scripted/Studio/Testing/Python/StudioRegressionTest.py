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
"""

import json
import os
import shutil
import subprocess
import tempfile
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
