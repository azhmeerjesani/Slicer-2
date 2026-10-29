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
  the study's own column (empty cells untraced); an include column written by pandas (1.0 / 0.0 / empty) keeps its meaning
  too, and Library, the cards and the analyses read it the same way (``study.is_included``).
- ``test_8_study_list_qc_keeps_cursor`` (BB-1): a QC key (P / X / U) refreshes the subject list (a model reset) but keeps the
  keyboard cursor (the current index) on the subject, so that the next Down goes to the next subject and the next QC key
  marks that one (it went to the first row: the wrong subject was marked); a selection made elsewhere moves the cursor too.
- ``test_12_quit_with_study_subject_open_in_draw`` (BB-1): a child process opens a study subject in Draw and quits: no traceback
  (the segment editor masking observer read the destroyed "More tools" button), exit code 0.
- ``test_13_failed_adopt_leaves_folder_unchanged`` (BB-1): a participants.tsv that cannot be adopted (ids "01" and "sub-01" collide,
  or no participant_id column) raises and leaves the folder as it was (no .studio/, lesions/ or derivatives/); after the table
  is fixed the study is created and .studio/participants.orig.tsv is the table that was adopted, not the broken one.
- ``test_15_inspector_keeps_width_with_a_study`` (BB-1): with a study open (Library cards, Map cohort tabs) the inspector stays at its
  stored width in every workspace, and no workspace module's stored width (``Studio/WorkspacePanelWidths/*``) is wider than it.
- ``test_17_job_result_waits_for_open_subject_edits`` (BB-1): a job result (a normalization) that repoints the open subject's
  scan, lesion and space while it has unsaved edits stays in the inbox when the inbox is merged (Library shown, "Merge job
  results", Search): Done writes the native tracing as the native lesion (never as template space) and the result is merged
  after it, with the edited file as ``lesion_native`` (also when Done wrote it to another file than the one the result was
  made from: the result is then Stale). For an open subject without edits the subject is closed, the result
  merged and the subject opened again on the new files.
- ``test_18_blind_tracing_hides_subject_and_cohort_cards`` (BB-1): with ``blind_tracing`` the SUBJECT card hides the group
  (Case / Control / —) and the score line, its Case section hides the symptom and domain fields (and writes neither), and the
  Cohort diagnostics card shows the number of lesions without the case/control split or the balance; without blinding they
  are all shown again.
- ``test_19_cohort_diagnostics_out_of_date_after_lesion_edit`` (BB-1): after a lesion of the study is edited, the Cohort
  diagnostics card says that its result (and the overlap maps, gallery and case table of the Lesion overview) is out of date,
  also when the recorded lesionset.json is shown again by a new card; the overlap volumes in the views are renamed
  "(out of date)"; "Create volume-matched control subset" stores nothing until the lesion set is checked again (it matched on
  volumes that no longer existed). A new check is current again.
- ``test_20_quit_with_case_strip_editor_open`` (BB-1): a child process opens a literature-study case in Draw, opens the case
  strip's inline editor ("+ DOI") and quits with ``slicer.app.exit`` (no window close): no traceback, exit code 0.
- ``test_21_share_dialog_shows_success_after_export`` (BB-1): after a successful "Export study for sharing" the dialog shows
  "Exported N files to ..." and Close with no failure mark in the checklist (the folder just written is not reported as "It
  already exists"); Export stays disabled for that output and is enabled again for another one.
- ``test_10_scripted_module_ui_widgets`` (BB-1): the build has the Qt Designer plugins of CTK and MRML, so that
  ``slicer.util.loadUI`` creates the CTK and qMRML widgets of a scripted module's ``.ui`` file (a ``qMRMLWidget`` root, a
  ``qMRMLNodeComboBox``, a ``ctkCollapsibleButton``), and Line Profile, Vector to Scalar Volume and Surface Toolbox set up
  their panel and logic. Without the plugins every such widget was a plain QWidget and ``setup()`` raised AttributeError.
"""

import json
import logging
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


QUIT_STUDY_SCRIPT = r"""
import os
import sys
import time

import numpy as np
import qt
import slicer


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        slicer.app.processEvents()
        time.sleep(0.005)


def main():
    from LesionNetworkMappingLib import geometry, nifti

    from StudioLib import shell, study_ui

    folder = os.environ["QUITTEST_FOLDER"]
    studioShell = shell.Shell(force=True)
    studioShell.apply()
    pump(200)
    grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
    mask = np.zeros(grid.shape, np.uint8)
    mask[30:34, 60:64, 50:53] = 1
    files = []
    for sid in ("sub-01", "sub-02"):
        path = os.path.join(folder, sid + "_lesion.nii.gz")
        nifti.write_nifti(path, mask, grid.affine, dtype=np.uint8)
        files.append(path)
    session = study_ui.createStudy(files, studioShell, folder=folder)
    pump(500)
    session.open("sub-01", switchToDraw=True)
    pump(800)
    print("QUITTEST READY", flush=True)
    slicer.app.exit(0)


try:
    main()
except Exception:
    import traceback

    traceback.print_exc()
    sys.stdout.flush()
    slicer.app.exit(3)
"""


QUIT_STRIP_SCRIPT = r"""
import os
import sys
import time

import numpy as np
import qt
import slicer


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        slicer.app.processEvents()
        time.sleep(0.005)


def main():
    from LesionNetworkMappingLib import geometry, nifti

    from StudioLib import shell, study_ui

    folder = os.environ["QUITTEST_FOLDER"]
    studioShell = shell.Shell(force=True)
    studioShell.apply()
    pump(200)
    grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
    mask = np.zeros(grid.shape, np.uint8)
    mask[30:34, 60:64, 50:53] = 1
    files = []
    for sid in ("sub-01", "sub-02"):
        path = os.path.join(folder, sid + "_lesion.nii.gz")
        nifti.write_nifti(path, mask, grid.affine, dtype=np.uint8)
        files.append(path)
    session = study_ui.createStudy(files, studioShell, folder=folder)
    pump(500)
    session.study.set("sub-01", record_id="smith2020", citation="Smith 2020")
    session.flush()
    session.open("sub-01", switchToDraw=True)
    pump(800)
    from StudioLib import casestrip

    strip = casestrip.strip()
    strip.startEdit("doi")
    pump(300)
    if not strip.isEditing():
        raise RuntimeError("the case strip editor did not open")
    print("QUITTEST READY", flush=True)
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

    def test_12_quit_with_study_subject_open_in_draw(self):
        """Quitting while a study subject is open in Draw is clean: the segment editor's masking observer (it reads the "More
        tools" button) is removed with the module widget, so a node change at shutdown does not read a destroyed button.
        """
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "quit_with_study.py")
        with open(script, "w") as stream:
            stream.write(QUIT_STUDY_SCRIPT)
        home = os.path.join(self.directory, "quit_home")
        folder = os.path.join(self.directory, "quit_study")
        os.makedirs(home)
        os.makedirs(folder)
        environment = dict(os.environ)
        environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "QUITTEST_FOLDER": folder,
                            "SLICER_STUDIO_PROJECTS_ROOT": os.path.join(self.directory, "quit_projects")})
        for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
            environment.pop(key, None)
        process = subprocess.run([launcher, "--no-splash", "--testing", "--python-script", script], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
        output = process.stdout.decode("utf-8", "replace")
        self.assertIn("QUITTEST READY", output, output[-3000:])
        self.assertNotIn("Traceback", output, output[-3000:])
        self.assertNotIn("exit abnormally", output, output[-3000:])
        self.assertEqual(process.returncode, 0, json.dumps(output[-3000:]))

    def test_20_quit_with_case_strip_editor_open(self):
        """Quitting (``slicer.app.exit``, no window close) while the case strip's inline editor is open is clean: the strip's
        ``aboutToQuit`` clean-up does not call ``isAncestorOf`` on the editor that Qt already deleted.
        """
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "quit_with_strip.py")
        with open(script, "w") as stream:
            stream.write(QUIT_STRIP_SCRIPT)
        home = os.path.join(self.directory, "quit_strip_home")
        folder = os.path.join(self.directory, "quit_strip_study")
        os.makedirs(home)
        os.makedirs(folder)
        environment = dict(os.environ)
        environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "QUITTEST_FOLDER": folder,
                            "SLICER_STUDIO_PROJECTS_ROOT": os.path.join(self.directory, "quit_strip_projects")})
        for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
            environment.pop(key, None)
        process = subprocess.run([launcher, "--no-splash", "--testing", "--python-script", script], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
        output = process.stdout.decode("utf-8", "replace")
        self.assertIn("QUITTEST READY", output, output[-3000:])
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
        # An include column written by pandas (1.0 / 0.0 / empty) keeps its meaning, and every reader agrees (BB-1)
        from StudioLib import libcard_analysis, libcard_cohort, libcard_normalize, study_ui

        header = ["participant_id", "include"]
        folder, study = self._adopt("Pandas study", header, [["sub-01", "1.0"], ["sub-02", "0.0"], ["sub-03", ""]])
        self.assertEqual([study.subject(sid)["include"] for sid in study.subject_ids], ["1", "0", "1"])
        for sid in study.subject_ids:
            study.set(sid, status="traced")
        study.save()
        study = study_ui._lnm("study").Study.open(folder)
        readers = (study_ui.isIncluded, libcard_normalize._included, libcard_cohort._lnm("study").is_included)
        self.assertEqual([[reader(row) for row in study.subjects] for reader in readers], [[True, False, True]] * 3)
        self.assertEqual(libcard_analysis.allTraced(study), (2, True))
        self.assertEqual([row["participant_id"] for row in study.analysis_rows()[0]], ["sub-01", "sub-03"])

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

    def test_9c_all_traced_toast_waits_for_a_consequence_with_its_action(self):
        """The "All N lesions traced" confirmation does not replace the consequence "No more cases of <paper> to
        trace" with its "New case from this paper" action: it waits for the next input (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import libcard_analysis, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "All traced study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        self.addCleanup(setattr, libcard_analysis, "ALL_TRACED_WAIT_MS", libcard_analysis.ALL_TRACED_WAIT_MS)
        libcard_analysis.ALL_TRACED_WAIT_MS = 50
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        session = None
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            self.addCleanup(libcard_analysis._toastedSessions.discard, id(session))
            for sid in session.study.subject_ids:
                session.study.set(sid, status="traced")
            consequence = "No more cases of Smith 2020 to trace"
            studioShell.toast(consequence, sticky=True, action=lambda: None, actionText="New case from this paper", level="consequence")
            self.assertTrue(studioShell.consequenceVisible())
            libcard_analysis._checkAllTraced(session)
            # Even after the minimum time of the consequence (the minimum timer is not what holds it back)
            studioShell._stickyToast.minimumTimer.stop()
            self.assertFalse(studioShell.consequenceHeld())
            for _ in range(6):
                slicer.app.processEvents()
                qt.QThread.msleep(30)
            self.assertEqual(studioShell._stickyToast.text(), consequence)
            self.assertEqual(studioShell._stickyToast.actionButton.text, "New case from this paper")
            self.assertFalse([text for _t, text, _l in studioShell.recentMessages() if "lesions traced" in text])
            # The next input dismisses the consequence: the confirmation follows
            studioShell._stickyToast.hide()
            deadline = time.time() + 5
            while time.time() < deadline and not [m for m in studioShell.recentMessages() if "lesions traced" in m[1]]:
                slicer.app.processEvents()
                qt.QThread.msleep(30)
            self.assertTrue([m for m in studioShell.recentMessages() if "All 2 lesions traced" in m[1]], studioShell.recentMessages())
            # And it is said once
            libcard_analysis._checkAllTraced(session)
            self.assertEqual(len([m for m in studioShell.recentMessages() if "lesions traced" in m[1]]), 1)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_9b_done_staying_on_the_case_leaves_the_lesion_saved(self):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import draw, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Stay study")
        os.makedirs(folder)
        scan = os.path.join(folder, "sub-01_T1w.nii.gz")
        nifti.write_nifti(scan, np.zeros(grid.shape, np.uint8), grid.affine)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy([scan], studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.open("sub-01", switchToDraw=False)
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            for _ in range(4):
                slicer.app.processEvents()
            self.assertNotEqual(draw.lesionSaveState(session.segmentationNode())[0], "saved")
            result = session.done()
            self.assertIsNotNone(result)
            # Nothing is left to trace: the subject stays open, and its lesion is the file that Done wrote
            self.assertEqual(session.current(), "sub-01")
            state, path, savedTime = draw.lesionSaveState(session.segmentationNode())
            self.assertEqual(state, "saved")
            self.assertEqual(os.path.normcase(path), os.path.normcase(result["path"]))
            self.assertTrue(savedTime)
            self.assertEqual(session.segmentationNode().GetAttribute(draw.SAVED_FILE_ATTRIBUTE), os.path.basename(result["path"]))
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_16_unwritable_lesion_folder_is_a_warning_in_plain_words(self):
        """A study folder that cannot be written (Done) is anticipated: a warning without a traceback, and a toast
        without the error number and the temporary file path; the subject stays open (BB-1).
        """
        from unittest import mock

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Read-only study")
        os.makedirs(folder)
        paths = [os.path.join(folder, f"sub-0{index}_T1w.nii.gz") for index in (1, 2)]
        for path in paths:
            nifti.write_nifti(path, np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Recorder(level=logging.DEBUG)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.open("sub-01", switchToDraw=False)
            temporary = os.path.join(folder, "lesions", "sub-01.123.tmp.nii.gz")
            error = PermissionError(13, "Permission denied", temporary)
            logging.getLogger().addHandler(handler)
            try:
                with mock.patch.object(session, "_write", side_effect=error):
                    self.assertIsNone(session.done())
            finally:
                logging.getLogger().removeHandler(handler)
            self.assertEqual(session.current(), "sub-01", "the subject stays open")
            self.assertEqual(session.study.subject("sub-01")["status"], "untraced")
            self.assertEqual([record for record in records if record.levelno >= logging.ERROR], [])
            self.assertTrue([record for record in records if record.levelno == logging.WARNING])
            self.assertFalse([record for record in records if record.exc_info or "Traceback" in record.getMessage()])
            self.assertTrue([text for text in toasts if "could not be saved: Permission denied" in text], toasts)
            for text in toasts:
                self.assertNotIn("Errno", text)
                self.assertNotIn(".tmp", text)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # Scripted module .ui files need the Qt Designer plugins (BB-1)

    def test_17_job_result_waits_for_open_subject_edits(self):
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study, is_missing

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        folder = os.path.join(self.directory, "Inbox open subject")
        os.makedirs(folder)
        nativeAffine = np.array([[1.2, 0, 0, -36], [0, 1.2, 0, -42], [0, 0, 1.2, -30], [0, 0, 0, 1]], float)
        nativeShape = (60, 70, 50)
        rng = np.random.default_rng(3)
        paths = []
        for index in range(1, 4):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], rng.normal(300, 8, nativeShape).astype(np.int16), nativeAffine, sform_code=1, qform_code=1)
        templateAffine = np.array([[2, 0, 0, -96.5], [0, 2, 0, -132.5], [0, 0, 2, -78.5], [0, 0, 0, 1]], float)
        templateShape = (97, 115, 97)

        def postNormalization(session, sid):
            """The result of a normalization of ``sid`` as normalize.py posts it (files in derivatives/normalization)."""
            row = session.study.subject(sid)
            out = os.path.join(folder, "derivatives", "normalization", sid)
            os.makedirs(out, exist_ok=True)
            mask = np.zeros(templateShape, np.uint8)
            mask[40:44, 50:54, 40:44] = 1
            prefix = f"derivatives/normalization/{sid}/{sid}_space-MNI152NLin2009cAsym"
            nifti.write_nifti(os.path.join(folder, prefix + "_desc-lesion_mask.nii.gz"), mask, templateAffine)
            nifti.write_nifti(os.path.join(folder, prefix + "_T1w.nii.gz"), mask.astype(np.int16) * 300, templateAffine)
            Study.post_result(folder, sid, {"scan_native": row["scan"], "lesion_native": None if is_missing(row.get("lesion")) else row["lesion"],
                                            "scan": prefix + "_T1w.nii.gz", "lesion": prefix + "_desc-lesion_mask.nii.gz", "space": "MNI152NLin2009cAsym",
                                            "norm_method": "test", "norm_qc": "unreviewed"}, source="normalize")

        def paint(session, box):
            segmentation, volume = session.segmentationNode(), session.volumeNode()
            segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            slicer.app.processEvents()

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            study = session.study
            # sub-02 is traced and written (a native lesion), then edited again without Done while its normalization finishes
            session.open("sub-02", switchToDraw=False)
            paint(session, np.s_[20:23, 30:33, 20:23])
            session.done()
            nativeLesion = study.subject("sub-02")["lesion"]
            self.assertEqual(study.subject("sub-02")["space"], "native")
            session.open("sub-02", switchToDraw=False)
            paint(session, np.s_[30:34, 30:33, 20:23])
            self.assertTrue(session.isDirty())
            postNormalization(session, "sub-02")
            # Every merge (Library shown, "Merge job results", Search) holds the result back: the row keeps the files of the scene
            session.applyInbox()
            row = study.subject("sub-02")
            self.assertEqual((row["lesion"], row["space"]), (nativeLesion, "native"))
            self.assertEqual([sids for _path, sids in session.lastInboxHeld], [["sub-02"]])
            self.assertEqual(len([name for name in os.listdir(os.path.join(folder, ".studio", "inbox")) if name.endswith(".json")]), 1)
            # Done writes the native tracing as the native lesion (on the native grid), then the result is merged
            result = session.done()
            self.assertEqual(study.relpath(result["path"]), nativeLesion)
            self.assertEqual(tuple(nifti.read_nifti_header(result["path"])["shape"][:3]), nativeShape)
            self.assertEqual(sorted(name for name in os.listdir(os.path.join(folder, "lesions")) if name.endswith(".nii.gz")),
                             ["sub-02_space-native_desc-lesion_mask.nii.gz"])
            self.assertNotEqual(session.current(), "sub-02")
            session.applyInbox()
            self.assertEqual(session.lastInboxHeld, [])
            row = study.subject("sub-02")
            self.assertEqual(row["space"], "MNI152NLin2009cAsym")
            self.assertTrue(row["lesion"].startswith("derivatives/normalization/sub-02/"), row["lesion"])
            self.assertEqual(row["lesion_native"], nativeLesion)
            edited = np.asarray(nifti.read_nifti(study.abspath(row["lesion_native"]))[0])
            self.assertEqual(int((edited > 0).sum()), 27 + 36)
            # A result made without the lesion that Done wrote meanwhile (another file): that lesion is kept as the native lesion
            untraced = session.current()
            self.assertIn(untraced, ("sub-01", "sub-03"))
            paint(session, np.s_[20:22, 20:22, 20:22])
            postNormalization(session, untraced)
            session.applyInbox()
            self.assertEqual(study.subject(untraced)["space"], "native")
            written = study.relpath(session.done()["path"])
            session.applyInbox()
            row = study.subject(untraced)
            self.assertEqual(row["space"], "MNI152NLin2009cAsym")
            self.assertEqual(row["lesion_native"], written)
            with open(os.path.join(folder, ".studio", "audit.jsonl"), encoding="utf-8") as f:
                self.assertIn("norm_lesion_edited", f.read())
            # An open subject without edits: closed, merged and opened again on the files of its new row
            clean = "sub-03" if untraced == "sub-01" else "sub-01"
            session.open(clean, switchToDraw=False)
            self.assertFalse(session.isDirty())
            postNormalization(session, clean)
            session.applyInbox()
            self.assertEqual(session.lastInboxHeld, [])
            self.assertEqual(session.current(), clean)
            self.assertEqual(study.subject(clean)["space"], "MNI152NLin2009cAsym")
            self.assertEqual(tuple(session.volumeNode().GetImageData().GetDimensions()), templateShape)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_10_scripted_module_ui_widgets(self):
        import sys
        import traceback

        loader = qt.QUiLoader()
        available = set(loader.availableWidgets())
        for className in ("qMRMLWidget", "qMRMLNodeComboBox", "qMRMLSpinBox", "qSlicerSimpleMarkupsWidget", "ctkCollapsibleButton",
                          "ctkSliderWidget", "qMRMLSubjectHierarchyTreeView"):
            self.assertIn(className, available, f"QUiLoader cannot create {className}: are the Qt Designer plugins built?")
        # The widgets of a .ui file are created with their own class, not as a plain QWidget
        uiPath = os.path.join(os.path.dirname(slicer.modules.lineprofile.path), "Resources", "UI", "LineProfile.ui")
        uiWidget = slicer.util.loadUI(uiPath)
        self.addCleanup(uiWidget.deleteLater)
        self.assertEqual(uiWidget.className(), "qMRMLWidget")
        ui = slicer.util.childWidgetVariables(uiWidget)
        self.assertEqual(ui.inputVolumeSelector.className(), "qMRMLNodeComboBox")
        self.assertEqual(ui.parametersCollapsibleButton.className(), "ctkCollapsibleButton")
        uiWidget.setMRMLScene(slicer.mrmlScene)
        self.assertEqual(list(ui.inputVolumeSelector.nodeTypes), ["vtkMRMLScalarVolumeNode"])
        # The modules set up their panel and logic without an error (setup() errors are only printed)
        errors = []
        originalHook = sys.excepthook
        sys.excepthook = lambda kind, value, tb: errors.append("".join(traceback.format_exception(kind, value, tb)))
        self.addCleanup(setattr, sys, "excepthook", originalHook)
        names = [name for name in ("LineProfile", "VectorToScalarVolume", "SurfaceToolbox") if hasattr(slicer.modules, name.lower())]
        self.assertIn("LineProfile", names)
        for name in names:
            widget = slicer.util.getModuleWidget(name)
            slicer.app.processEvents()
            self.assertIsNotNone(widget, name)
            self.assertIsNotNone(getattr(widget, "logic", None), f"{name}: no logic after setup()")
            self.assertEqual(errors, [], name)

    def test_11_dimensionality_chip_needs_study_to_choose(self):
        """Outside a study the 2D chip is neutral and does not ask for a choice that cannot be made (no Choose... action)."""
        from StudioLib import lesionqc

        result = {"dimensionality": {"dimensionality": "2D", "planes": ["axial"], "n_slices": {"axial": 1}}, "spacing": 1.0}

        def chip(**kwargs):
            chips = [c for c in lesionqc.buildChips(result, mni=True, **kwargs) if c["key"] == "dimensionality"]
            self.assertEqual(len(chips), 1)
            return chips[0]

        outside = chip(studyOpen=False)
        self.assertEqual(outside["level"], "neutral")
        self.assertNotIn("choose", outside["text"].lower())
        self.assertEqual(outside["actions"], [])
        inside = chip(studyOpen=True)
        self.assertEqual(inside["level"], "warn")
        self.assertEqual([a["text"] for a in inside["actions"]], ["Choose…"])
        chosen = chip(studyOpen=True, policy={"mode": "fractional", "total_mm": 2})
        self.assertEqual(chosen["level"], "neutral")
        self.assertEqual([a["text"] for a in chosen["actions"]], ["Change…"])

    def test_13_failed_adopt_leaves_folder_unchanged(self):
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        folder = os.path.join(self.directory, "Collide")
        os.makedirs(folder)
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        before = sorted(os.listdir(folder))
        tsv = os.path.join(folder, "participants.tsv")
        for broken, message in (
            ("participant_id\tage\n01\t40\nsub-01\t41\n02\t42\n", "appears twice"),
            ("id\tage\n1\t40\n", "no participant_id column"),
        ):
            with open(tsv, "w", encoding="utf-8") as f:
                f.write(broken)
            with self.assertRaises(ValueError) as raised:
                study_ui.createStudy(paths, folder=folder, openIt=False)
            self.assertIn(message, str(raised.exception))
            self.assertEqual(sorted(os.listdir(folder)), sorted(before + ["participants.tsv"]))
        fixed = "participant_id\tage\tnote\n01\t40\tfixed table\n02\t42\tfixed table\n"
        with open(tsv, "w", encoding="utf-8") as f:
            f.write(fixed)
        study = study_ui.createStudy(paths, folder=folder, openIt=False)
        self.assertEqual(study.subject_ids, ["sub-01", "sub-02"])
        with open(os.path.join(folder, ".studio", "participants.orig.tsv"), encoding="utf-8") as f:
            self.assertEqual(f.read(), fixed)

    def test_14_rejected_inbox_results_are_said(self):
        """Job results that the inbox merge rejects are said (toast, ``lastInboxRejected``), not only moved to rejected/ and logged (BB-1)."""
        from LesionNetworkMappingLib.study import Study

        from StudioLib import library, study_ui

        folder, _study = self._adopt("Inbox", ["participant_id", "age"], [["sub-01", "60"], ["sub-02", "61"], ["sub-03", "62"]])
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append((str(text), kwargs.get("level")))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        inbox = os.path.join(folder, ".studio", "inbox")
        rejectedFolder = os.path.join(inbox, "rejected")
        # A valid result alone: merged without a notice
        toasts.clear()
        Study.post_result(folder, "sub-01", {"age": 70}, "regression")
        self.assertEqual(len(session.applyInbox()), 1)
        self.assertEqual(session.lastInboxRejected, [])
        self.assertEqual(toasts, [])
        self.assertEqual(session.study.subject("sub-01")["age"], "70")
        # A result for an unknown subject (removed while the job ran), an invalid strict value and a broken file
        Study.post_result(folder, "sub-02", {"age": 71}, "regression")
        Study.post_result(folder, "sub-nobody", {"age": 1}, "regression")
        with open(os.path.join(inbox, "zz-strict.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-studio-inbox", "version": 1, "source": "x", "rows": {"sub-03": {"review_status": "maybe"}}}, f)
        with open(os.path.join(inbox, "zz-broken.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        toasts.clear()
        applied = session.applyInbox()
        self.assertEqual(len(applied), 4)
        self.assertEqual(len(os.listdir(rejectedFolder)), 3)
        self.assertEqual(session.study.subject("sub-02")["age"], "71")
        self.assertEqual(sorted(sid for _path, sid, _message in session.lastInboxRejected if sid), ["sub-03", "sub-nobody"])
        self.assertEqual(len(toasts), 1, toasts)
        text, level = toasts[0]
        self.assertEqual(level, "warning")
        self.assertIn("3 job results were not merged", text)
        self.assertIn("sub-nobody: unknown subject", text)
        self.assertIn("rejected", text)
        # The next merge has nothing new: no repeated notice
        toasts.clear()
        self.assertEqual(session.applyInbox(), [])
        self.assertEqual((session.lastInboxRejected, toasts), ([], []))
        # The Merge button: counts only the merged files and says the rejected one apart
        Study.post_result(folder, "sub-02", {"age": 72}, "regression")
        Study.post_result(folder, "sub-nobody", {"age": 2}, "regression")
        shown = []

        class FakeShell:
            def toast(self, text, **kwargs):
                shown.append((str(text), kwargs.get("level")))

        originals = (library._shell, library._studySession)
        library._shell, library._studySession = FakeShell, (lambda: session)
        try:
            library.StudyCard._merge(None)
        finally:
            library._shell, library._studySession = originals
        self.assertEqual(len(shown), 1, shown)
        self.assertIn("Merged 1 job results", shown[0][0])
        self.assertIn("1 job result was not merged", shown[0][0])
        self.assertEqual(shown[0][1], "warning")

    def test_15_inspector_keeps_width_with_a_study(self):
        """A study neither widens the inspector (Library cards, Map cohort cards) nor leaves it wide (BB-1).

        With a study open the Library cards (the grid question row, two check boxes with long texts) and the Map Cohort
        card (a form per tab, the seed modes, a combo next to a button) made the layout minimum of the workspace modules
        wider than ``Studio/PanelWidth``, and the widest minimum stayed for good (409 px with a study, 433 px with the
        Map cohort tabs, at 1366x768 with a stored width of 340 px). The inspector stays at its stored width in every
        workspace, the stored widths of the workspaces stay within it, and the views do not move.
        """
        from StudioLib import shell, study_ui, templates

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        settings = qt.QSettings()
        keys = [f"{shell._WORKSPACE_PANEL_WIDTHS_GROUP}/{name}" for name in ("Studio", "SegmentEditor", "LesionNetworkMapping")]
        saved = {key: settings.value(key) for key in keys}
        for key in keys:
            settings.remove(key)
        savedGeometry = mainWindow.saveGeometry()
        studioShell = None
        try:
            mainWindow.resize(1366, 768)
            studioShell = shell.Shell(force=True)
            studioShell.apply()
            dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")

            def pump(count=10):
                for _ in range(count):
                    slicer.app.processEvents()

            def widest():
                """The widest visible widgets of the module panel that a layout cannot shrink, for the failure messages."""
                scrollArea = mainWindow.findChild("QWidget", "ModulePanel").findChild("QScrollArea", "ScrollArea")
                found = []

                def walk(widget):
                    for child in widget.children():
                        if child.isWidgetType() and child.isVisible() and child.sizePolicy.horizontalPolicy() != qt.QSizePolicy.Ignored:
                            text = getattr(child, "text", "")
                            found.append((child.minimumSizeHint.width(), child.objectName or child.metaObject().className(), text if isinstance(text, str) else "", child.sizePolicy.horizontalPolicy()))
                            walk(child)

                walk(scrollArea.widget())
                return sorted(found, reverse=True)[:12]

            def check(step):
                pump()
                self.assertLessEqual(dock.width, studioShell.panelWidth + 2, f"{step}: the inspector is {dock.width} px, widest {widest()}")
                held = {name: width for name, width in studioShell._workspacePanelWidths.items() if width > studioShell.panelWidth + 2}
                self.assertEqual(held, {}, f"{step}: a workspace module asks for more than the stored width")
                for key in keys:
                    stored = str(settings.value(key, "") or "")
                    width = int(stored.partition("@")[0] or 0)
                    self.assertLessEqual(width, studioShell.panelWidth + 2, f"{step}: {key} = {stored}")

            for workspace in ("library", "draw", "map", "library"):
                studioShell.switchTo(workspace)
                check(f"project, {workspace}")
            folder = os.path.join(self.directory, "S")
            os.makedirs(folder)
            files = []
            for index in range(1, 4):
                path = os.path.join(folder, f"sub-{index:02d}_T1w.nii.gz")
                shutil.copyfile(templates.builtinTemplates()[0].imagePath, path)
                files.append(path)
            session = study_ui.createStudy(files, shell=studioShell, folder=folder, name="S")
            pump(20)
            studioShell.switchTo("library")
            check("study, library")
            session.open(session.study.subject_ids[0])
            for _ in range(30):
                if studioShell.currentWorkspace() == "draw":
                    break
                pump(4)
            check("study, draw")
            studioShell.switchTo("map")
            check("study, map")
            widget = slicer.modules.LesionNetworkMappingWidget
            self.assertIn("cohort", widget.seedModes)
            widget.seedModeGroup.button(widget.seedModes.index("cohort")).click()
            check("study, map cohort")
            # Every tab of the Cohort card, and the cards below it
            tabs = widget.parent.findChild("QTabWidget", "LNMCohortTabs")
            self.assertIsNotNone(tabs)
            for index in range(tabs.count):
                tabs.setCurrentIndex(index)
                check(f"study, map cohort tab {index}")
            study_ui.closeStudy()
            studioShell.switchTo("library")
            check("study closed, library")
            studioShell.switchTo("draw")
            check("study closed, draw")
        finally:
            if studioShell is not None:
                studioShell.dispose()
            mainWindow.restoreGeometry(savedGeometry)
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            slicer.mrmlScene.Clear(0)

    # Blind tracing hides group, scores and symptoms in the SUBJECT and Cohort diagnostics cards (BB-1)

    def test_18_blind_tracing_hides_subject_and_cohort_cards(self):
        from LesionNetworkMappingLib import review as rules

        from StudioLib import libcard_cohort, libcard_columns, library, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        rows = [(f"sub-0{index}", {"group": "case" if index <= 3 else "control", "NIHSS": str(index + 4)}, False) for index in range(1, 6)]
        study = self._study(rows)
        study.set_column_role("NIHSS", "score")
        study.set("sub-02", {"symptom": "neglect", "domain": "attention"})
        study.save()
        subjectCard = section = cohortCard = None

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def shown(widget):
            return widget is not None and not widget.isHidden() and widget.isVisibleTo(widget.window())

        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            subjectCard = library.SubjectCard()
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            subjectCard.widget.show()
            sectionWidget.show()
            cohortCard = libcard_cohort.CohortCard(session)
            cohortCard.widget.show()
            cohortCard.refresh()
            cohortCard.result = libcard_cohort.checkLesionSet(session.study.path)
            self.assertIsNotNone(cohortCard.result.get("summary"))

            def render():
                subjectCard.refresh("sub-02")
                section.refresh("sub-02")
                cohortCard.refresh()
                pump()

            # Without blinding: the group, the score, the symptom and the case/control split are shown
            render()
            self.assertTrue(shown(subjectCard.groupFrame))
            self.assertTrue(subjectCard.groupButtons.button(0).isChecked(), "sub-02 is a case")
            self.assertIn("NIHSS: 6", str(subjectCard.scoresLabel.text))
            self.assertTrue(shown(subjectCard.scoresLabel))
            self.assertTrue(shown(section.editors["symptom"]))
            self.assertEqual(str(section.editors["symptom"].text), "neglect")
            groups = str(cohortCard.rows["groups"][1].text)
            self.assertIn("3 cases", groups)
            self.assertTrue(shown(cohortCard.balanceTable))

            # Blind tracing: none of them is shown (L-43, LNM addendum 3.10)
            rules.set_study_setting(session.study, "blind_tracing", True, "Tester")
            self.assertEqual(set(rules.blinded_columns(session.study)), {"group", "NIHSS", "symptom", "domain"})
            self.assertTrue(set(rules.blinded_columns(session.study)) <= library._hiddenColumns(session.study))
            render()
            self.assertFalse(shown(subjectCard.groupFrame), "the group of the subject is shown while tracing blind")
            self.assertFalse(shown(subjectCard.groupLabel))
            self.assertFalse(shown(subjectCard.scoresLabel), f"the scores are shown while tracing blind: {subjectCard.scoresLabel.text}")
            self.assertNotIn("NIHSS", str(subjectCard.scoresLabel.text))
            for column in ("symptom", "domain"):
                editor = section.editors[column]
                self.assertFalse(shown(editor), f"the {column} field is shown while tracing blind")
                self.assertFalse(shown(section.rows[column][0]), f"the {column} label is shown while tracing blind")
                self.assertEqual(str(editor.currentText if hasattr(editor, "itemData") else editor.text).strip("—"), "", f"{column} kept a value")
            self.assertFalse(section._write("symptom", "aphasia"), "a blinded field is not written")
            self.assertEqual(session.study.subject("sub-02")["symptom"], "neglect")
            groups = str(cohortCard.rows["groups"][1].text)
            self.assertNotIn("case", groups)
            self.assertNotIn("control", groups)
            self.assertIn("5 lesions", groups)
            self.assertFalse(shown(cohortCard.balanceTable))
            self.assertFalse(shown(cohortCard.matchRow))
            self.assertIsNone(cohortCard.createSubset())
            # The group control does not write while hidden
            subjectCard._onGroup(1)
            self.assertEqual(session.study.subject("sub-02")["group"], "case")

            # Blinding off: everything is back
            rules.set_study_setting(session.study, "blind_tracing", False, "Tester")
            render()
            self.assertTrue(shown(subjectCard.groupFrame))
            self.assertTrue(subjectCard.groupButtons.button(0).isChecked())
            self.assertIn("NIHSS: 6", str(subjectCard.scoresLabel.text))
            self.assertTrue(shown(section.editors["symptom"]))
            self.assertEqual(str(section.editors["symptom"].text), "neglect")
            self.assertIn("3 cases", str(cohortCard.rows["groups"][1].text))
            self.assertTrue(shown(cohortCard.balanceTable))
        finally:
            if cohortCard is not None:
                cohortCard.dispose()
                cohortCard.widget.deleteLater()
            for widget in (subjectCard.widget if subjectCard is not None else None, section.widget if section is not None else None):
                if widget is not None:
                    widget.deleteLater()
            study_ui.closeStudy()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # Cohort diagnostics after a lesion edit (BB-1)

    def test_19_cohort_diagnostics_out_of_date_after_lesion_edit(self):
        from LesionNetworkMappingLib import nifti

        from StudioLib import libcard_cohort, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        rows = [(f"sub-0{index}", {"group": "case" if index <= 3 else "control"}, False) for index in range(1, 7)]
        study = self._study(rows)
        card = second = None

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def stale(widget):
            return not widget.staleLabel.isHidden() and "out of date" in str(widget.statusLabel.text)

        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            card = libcard_cohort.CohortCard(session)
            card.widget.show()
            card.refresh()
            self.assertTrue(card.overview(), "the Lesion overview did not start")
            self.assertTrue(card.worker.waitForIdle(120))
            pump()
            self.assertIsNotNone(card.result and card.result.get("summary"))
            self.assertFalse(card.stale)
            self.assertFalse(card.overviewStale)
            self.assertFalse(stale(card), card.statusLabel.text)
            def countNode():
                found = [node for node in libcard_cohort.overlapNodes() if node.GetAttribute(libcard_cohort.OVERLAP_ATTRIBUTE) == "count"]
                return found[0] if len(found) == 1 else None

            count = countNode()
            self.assertIsNotNone(count, "the overlap count is not in the views")
            self.assertEqual(count.GetName(), libcard_cohort.COUNT_NODE)
            recorded = json.load(open(os.path.join(study.path, "derivatives", "lnm", "lesionset.json"), encoding="utf-8"))
            self.assertEqual(recorded.get("inputs"), card.result["inputs"])
            self.assertEqual(recorded["gallery"].get("inputs"), card.result["inputs"])
            card.refresh()
            self.assertFalse(card.stale, "a result is out of date although no lesion changed")

            # Edit sub-02's lesion (as Done in Draw writes it): a larger lesion
            path = session.study.abspath(session.study.subject("sub-02")["lesion"])
            affine = np.diag([2.0, 2.0, 2.0, 1.0])
            affine[:3, 3] = [-20.0, -20.0, -20.0]
            block = np.zeros((20, 20, 20), np.uint8)
            block[4:14, 4:14, 4:14] = 1
            nifti.write_nifti(path, block, affine)
            later = time.time() + 2.0
            os.utime(path, (later, later))
            card.refresh()
            pump()
            self.assertTrue(card.stale, "the card shows the result from before the lesion edit as current")
            self.assertTrue(stale(card), card.statusLabel.text)
            self.assertTrue(card.overviewStale)
            self.assertIn("out of date", str(card.overviewLabel.text))
            self.assertFalse(card.matchButton.enabled)
            self.assertTrue(count.GetName().endswith(libcard_cohort.OUT_OF_DATE_SUFFIX), count.GetName())
            self.assertEqual(count.GetAttribute(libcard_cohort.OUT_OF_DATE_ATTRIBUTE), "1")
            card.refresh()
            self.assertEqual(count.GetName().count(libcard_cohort.OUT_OF_DATE_SUFFIX.strip()), 1, "marked twice")
            # No subset is matched on the volumes from before the edit
            self.assertIsNone(card.createSubset())
            self.assertNotIn("volume_matched", session.study.subsets)
            self.assertIn("Check again", str(card.matchLabel.text))

            # The recorded lesionset.json, shown again (a reopened study), is out of date too
            second = libcard_cohort.CohortCard(session)
            second.refresh()
            self.assertEqual(second.result.get("source"), "file")
            self.assertTrue(second.stale, "the recorded result is shown as current after the lesion edit")
            self.assertTrue(stale(second), second.statusLabel.text)
            self.assertTrue(second.overviewStale)
            # A lesionset.json written without a fingerprint: a lesion file newer than the file is out of date
            self.assertTrue(libcard_cohort.isOutOfDate(session.study, None, since=later - 10.0))
            self.assertFalse(libcard_cohort.isOutOfDate(session.study, None, since=later + 10.0))

            # Check again: current; the overview stays out of date until it is run again
            self.assertTrue(card.run())
            self.assertTrue(card.worker.waitForIdle(120))
            pump()
            self.assertFalse(card.stale)
            self.assertFalse(stale(card), card.statusLabel.text)
            self.assertTrue(card.overviewStale)
            self.assertTrue(card.matchButton.enabled)
            volumes = {lesion["participant_id"]: lesion["volume_ml"] for lesion in card.result["summary"]["lesions"]}
            self.assertGreater(volumes["sub-02"], 4.0, "the new check did not read the edited lesion")
            self.assertEqual(card.createSubset(), "volume_matched")
            self.assertIn("volume_matched", session.study.subsets)
            self.assertTrue(card.overview())
            self.assertTrue(card.worker.waitForIdle(120))
            pump()
            self.assertFalse(card.overviewStale)
            self.assertNotIn(libcard_cohort.OUT_OF_DATE_SUFFIX.strip(), countNode().GetName())
            self.assertEqual(len(libcard_cohort.overlapNodes()), 2, "the out-of-date overlap volumes were not replaced")
        finally:
            for widget in (card, second):
                if widget is not None:
                    widget.dispose()
                    widget.widget.deleteLater()
            study_ui.closeStudy()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_21_share_dialog_shows_success_after_export(self):
        """After a successful "Export study for sharing" the dialog shows the success ("Exported N files to ...") and Close, and
        its checklist has no failure mark: the folder that was just written is not reported as "It already exists" and Export
        stays disabled for it. Another output, or a failed or stopped export, behaves as before (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import libcard_share, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Share study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        dialog = None
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            dialog = libcard_share.ShareDialog(session)
            dialog.dialog.show()
            output = dialog.output()
            self.assertFalse(os.path.exists(output))
            self.assertTrue(dialog.exportButton.enabled, dialog.checklistLabel.text)
            dialog.exportButton.click()
            deadline = time.time() + 90
            while time.time() < deadline and dialog.result is None and dialog.error is None:
                slicer.app.processEvents()
                qt.QThread.msleep(30)
            slicer.app.processEvents()
            self.assertIsNone(dialog.error)
            self.assertIsNotNone(dialog.result)
            self.assertTrue(os.path.isdir(output))
            self.assertTrue(dialog.progressLabel.text.startswith("Exported "), dialog.progressLabel.text)
            self.assertEqual(list(dialog.blocking()), [], dialog.items)
            self.assertNotIn("Output chosen", dialog.checklistLabel.text)
            self.assertNotIn("already exists", dialog.checklistLabel.text)
            self.assertFalse(dialog.exportButton.enabled, "exporting again onto the folder just written")
            self.assertEqual(dialog.cancelButton.text, "Close")
            # Another output: the dialog is ready to export again
            dialog.outputEdit.setText(output + "-second")
            dialog.preview()
            self.assertTrue(dialog.exportButton.enabled, dialog.checklistLabel.text)
            # Back to the written folder after other edits: the plain "already exists" blocker is back for a new export
            dialog._exportedTo = None
            dialog.outputEdit.setText(output)
            dialog.preview()
            self.assertFalse(dialog.exportButton.enabled)
            self.assertIn("already exists", dialog.checklistLabel.text)
        finally:
            if dialog is not None:
                dialog.dialog.close()
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
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
