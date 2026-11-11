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
- ``test_23_map_cards_keep_inspector_width`` (BB-1): expanding any Map card (Circuits with a long circuit name, Compare with its results
  shown) keeps the inspector at its stored width, and it stays there after the card is collapsed and in every workspace.
- ``test_24_map_seed_modes_keep_inspector_width`` (BB-1): choosing each Map seed mode (Coordinates, Stimulation, ...) keeps the inspector at
  the width of Draw, and it stays there back in Lesion mode and in every workspace, at the normal and at a 125 % application font.
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
- ``test_22_study_card_save_line_follows_the_save`` (BB-1): the STUDY card's save line says "Saving…" while the save of
  participants.tsv is pending and "All changes saved" once it has run (it stayed at "Saving…").
- ``test_10_scripted_module_ui_widgets`` (BB-1): the build has the Qt Designer plugins of CTK and MRML, so that
  ``slicer.util.loadUI`` creates the CTK and qMRML widgets of a scripted module's ``.ui`` file (a ``qMRMLWidget`` root, a
  ``qMRMLNodeComboBox``, a ``ctkCollapsibleButton``), and Line Profile, Vector to Scalar Volume and Surface Toolbox set up
  their panel and logic. Without the plugins every such widget was a plain QWidget and ``setup()`` raised AttributeError.
- ``test_33_extensions_manager_asks_for_the_official_build`` (BB-1): the Extensions Manager asks the extensions server for
  revision 35389 (official 5.13.0, the upstream build the fork is based on), not the fork's own commit count that matches no
  official build (empty catalog, install by name failed); extensions are still installed in the build's own
  ``Extensions-<revision>`` folder and their scripted modules are loaded as extensions (folder added to ``sys.path``).
- ``test_24_lesion_metrics_follow_a_result_that_repoints_the_lesion`` (BB-1): a merged result that repoints the lesion
  (a normalization) recomputes the row's centroid, outside-brain share and lesion size from the new file at once.
- ``test_27_sources_undo_reverts_the_change_just_made`` (BB-1): in the Sources card, U right after P or X undoes that change
  (the selection had moved on to the next record, and U reverted that record's earlier decision silently), selects the
  record and says what was undone; a row selected by the user is undone as before.
- ``test_29_second_tracing_survives_a_crash`` (BB-1): a blind second tracing is journaled like a first tracing
  (``.studio/journal/rater-<label>/<sid>.nii.gz``, 2 s after the last edit); the autosave names the second tracing and
  its subject (never the session's earlier subject); recovering that autosave reopens the second tracing, whose notice
  offers the journal; Recover brings the voxels back and saving removes the journal.
- ``test_56_study_folder_renamed_while_open_is_said_and_not_recreated`` (BB-1): a study folder renamed while the study is
  open is said in plain words by the heartbeat and by Done (not "the grid has no NIfTI file"), and the journal, the
  heartbeat and Done recreate no ``.studio`` folder at the old path; with the folder back, writing resumes.
- ``test_27b_sources_fetch_metadata_runs_in_the_background_and_can_be_cancelled`` (BB-1): Fetch metadata on the Sources card made its
  request on the main thread, so a network that does not answer froze the whole application for up to 10 s with no Cancel. The
  request now runs in a helper thread: the click returns at once, the button reads "Cancel lookup", the answer is applied on the
  main thread, a cancelled lookup ignores its late answer, and a failure is said on the card.
- ``test_30_missing_scan_or_lesion_is_said`` (BB-1): a subject whose scan file is missing is not opened on the study
  template (open and Done -> next say so, the current subject stays open); a traced subject whose lesion file is missing
  opens on its scan with an empty lesion and says so.
- ``test_34_draw_edits_an_existing_segmentation`` (BB-1): Draw edits a segmentation that Studio did not create (made by a
  script, or loaded from a file) on the scan it is bound to, like the classic Segment Editor: the editor has it and the scan,
  no "Lesions" segmentation is added, and no "SetSourceVolumeNode" error is logged (Draw said "No lesion" / "Open a scan"
  with its tools disabled).
- ``test_31_symptom_column_is_not_made_the_groups`` (BB-1): a yes / no, Y / N or true / false column that is not named like a
  group (a symptom such as "aphasia", "smoker") is never proposed as the study groups: New study… and the participants
  table's "Create study" leave the groups empty and keep the column. A group column ("diagnosis": patient / HC) is shown
  in the group-mapping step (L-01) before the study is made by these routes, as on the folder card: Cancel makes no
  study, and the groups are the ones chosen in the step.
- ``test_37_paint_brush_not_shown_before_its_model_exists`` (BB-1): the Paint effect shows its brush only once the brush model
  exists (built at the first mouse move). An event that is not a mouse move (the pointer entering a view that a layout change
  puts under a resting mouse: Four-up -> Library -> Draw -> Focus) used to show the brush with its first filter unconnected,
  and the render logged "Input port 0 of algorithm vtkTransformPolyDataFilter ... has 0 connections but is not optional"
  (the red error badge of the top bar).
- ``test_38_quit_after_fill_between_slices_exits_cleanly`` (BB-1): a child process runs the Fill between slices interpolator
  (``vtkITKMorphologicalContourInterpolator``) on a lesion with many traced slices and quits: exit code 0, no "exit abnormally"
  (the interpolator's thread_local ITK filters were destroyed in worker threads that end after ITK's globals at shutdown: a
  SIGSEGV after the StudioTest suite reported OK, and in official 3D Slicer after any use of Fill between slices).
- ``test_39_crash_check_studio_probe_runs_in_xvfb`` (BB-1): the ``--studio`` probe of ``StudioAutosaveCrashCheck.py`` (is
  ``StudioLib.autosave_ui`` there) starts Slicer in ``xvfb-run`` like the harness's other Slicer runs unless ``--no-xvfb``
  (without a display Qt cannot start, exit 1, and the report said "A-01b not present" and FAIL on every headless machine);
  only exit code 3 means "not present", another non-zero exit says that Slicer did not start.
- ``test_42_docs_name_the_classic_view_menu_item`` (BB-1): the documents that tell how to switch back from Classic name the
  Classic item as ``View > Switch to Studio interface`` (the item's text in Classic), never ``View > Studio interface`` (the
  checked item of Studio's View menu).
- ``test_45_docs_describe_the_map_footer_as_the_app_builds_it`` (BB-1): studio.md and lesionnetworkmapping.md describe the
  Map footer as the one line "Research use only. Lesion network mapping specificity is debated. Read more" (the whole
  statement and its references behind Read more and in the Report card), and studio.md lists the Circuits, Compare and Report
  cards of the Map inspector.
- ``test_51_docs_quote_the_view_orientation_statement_as_the_views_show_it`` (BB-1): studio.md quotes the statement of the
  axial and coronal views as "Radiological · L on right" / "Neurological · L on left" at the bottom right, as
  ``orientation.displayStatement`` returns it, not the old "Left is on screen right" at the bottom left.
- ``test_53_a_scan_opened_from_outside_a_study_never_feeds_done_next`` (BB-1): with a study open, opening a subject's own
  scan opens that subject; another scan closes the study first (the subject's lesion is written) and is saved like any
  scan; a lesion of another scan in Draw gets Save lesion, never Done → next of the hidden subject.
- ``test_55b_segment_selector_keeps_the_stored_selection_for_ids_the_new_node_has`` (BB-1): the multi-selection of
  qMRMLSegmentSelectorWidget survives a node change for the segment IDs the new node has (as upstream); only missing IDs are dropped, silently.
- ``test_54_return_in_the_columns_dialog_never_cancels_the_edits`` (BB-1): Return in the Columns… dialog (on a role combo, the
  table, the filter check box or a button) neither closes it nor drops the edits: the dialog has no default button (Cancel was,
  so Return threw every edit away without a word); Confirm still saves.
- ``test_62_docs_name_the_connectome_buttons_as_the_card_shows_them`` (BB-1): lesionnetworkmapping.md and lnm_data.md name the
  way to the catalog as the Connectome card shows it: **Get a connectome…** when no connectome is registered (the card has no
  Manage… button then), **Manage… > Get a connectome…** only when one is, and the card's demo button as "Use the demo".
- ``test_64_docs_put_the_save_status_on_the_left_of_the_top_bar`` (BB-1): studio.md and user_interface.md place the save status
  on the left of the top bar after the breadcrumb (VISUAL 4.1, as ``topbar.py`` adds it to the left layout) and call it the
  save status (Saved, Saving…, Not saved · Retry), not "the autosave status" on the right.
- ``test_65_qc_chip_text_shows_every_line`` (BB-1): the text of a QC chip of the Lesion card is as high as its wrapped lines need
  at its width (the row was capped at its size hint by a Maximum vertical policy, so the last line of a long text was cut off).
- ``test_66_cxx_extensions_of_the_official_build_are_said_incompatible`` (BB-1): an extension with compiled (C++) modules
  (a library in ``qt-loadable-modules``, an executable in ``cli-modules``) is incompatible, with a plain reason, and kept
  disabled (its folders are not in the module or launcher paths), at install and when found at start: such modules were
  built for official 3D Slicer and never load in the fork. An extension written in Python stays compatible and enabled. The Manage list
  does not say "Install pending restart" on the rows of the disabled ones (a restart would not load them).
- ``test_67_save_lesion_is_off_in_second_tracing_adjudication_and_review`` (BB-1): in a blind second tracing, an adjudication
  and a review, Save lesion (Ctrl+Shift+S), Save lesion as… (the Lesion card ⋯ menu and Search) are off with a toast: they
  proposed the first tracer's file (the scan's folder, its BIDS subject) and replaced it with the second rater's strokes, the
  A/B label map or the reviewer's edit. The footer of each state writes its own files.
- ``test_70_adjudication_tracings_are_read_only_until_edit`` (BB-1): an adjudication opens with no tool armed and the two
  tracings read-only (tool tiles and keys off, card fixes off, Draw's hint says why) until Edit…: the Brush stayed armed on A,
  a stroke changed A on screen, and choosing A or Union wrote the files on disk without the stroke and without a word. Edit…
  makes the Consensus segment editable, Cancel edit makes the tracings read-only again, and leaving gives an editable editor.
- ``test_71_adjudication_edit_survives_escape_and_undo`` (BB-1): Esc during an adjudication's Edit… keeps the edited
  consensus (it deselects the tool; only Cancel edit leaves the edit): Esc removed the Consensus segment without a word.
  After Cancel edit, Ctrl+Z (the Segment Editor's undo) brings the edited Consensus back, and Edit… resumes that segment
  instead of adding a second "Consensus" from the union: Save consensus wrote the plain union, every erasure lost.
- ``test_72_imported_clinical_table_keeps_status_and_include`` (BB-1): a clinical table opened (or dropped) into an open study
  with a "status" column of alive / deceased and an "include" column of yes / no is imported as adopting keeps it: status as
  ``status_original`` (the study's tracing status and QC unchanged), include yes / no as 1 / 0, and the other columns (age,
  NIHSS) imported. Nothing was imported and the toast said "Studies are not available in this build".
  ``Study.import_tsv`` reads include 1.0 / 0.0 (pandas); a table that still cannot be imported says why in the toast.
- ``test_73_project_closed_outside_draw_frees_its_lesion`` (BB-1): a project closed while Library (or Map) is shown frees its
  lesion: Draw's caches (lesion metrics, MNI check, saved records) are emptied by the close and the closed project's
  vtkSegmentation is deleted. They were cleared only by Draw's own close observer, active only while Draw is shown, and kept
  one lesion labelmap per closed project (up to 64; about 130 MB each at 0.5 mm).
- ``test_74_blind_search_does_not_match_the_group`` (BB-1): while the study traces blind, "Search subjects" of the Library
  list does not match the hidden group ("control" or "case" lists no subject, not exactly the controls or the cases) and its
  tooltip does not offer the group; without blinding the search by group works again.
- ``test_79_unreadable_participants_tsv_keeps_study_changes_at_close`` (BB-1): while a study is open, another program saves
  participants.tsv with an extra value that has no column name. Each save then fails (a warning, no traceback: the table is
  the user's to repair); the quit prompt says that the study changes of the subject are not saved and that they are kept;
  closing the study posts them to ``.studio/inbox`` (participants.tsv is left as it is), and the study opened again after the
  table is repaired has the subject's Done (status, lesion). They were kept only in memory and lost at quit.
- ``test_81_assist_counts_stay_in_their_study`` (BB-1): the Smart fill counts of study A's sub-01 are not read for another
  study's sub-01: study B's Brush-only tracing of its own sub-01 is recorded as ``manual`` without ``assist_pct`` or a Smart
  fill share in its sidecar (it was ``manual+smartfill`` with study A's numbers); study A opened again keeps its counts, and
  the counts of a lesion segment outside a study end with the scene (the next scene reuses its node IDs).
- ``test_82_damaged_lesion_or_scan_is_said`` (BB-1): a traced subject whose lesion file is cut short opens with an empty
  lesion and a consequence toast that names the damaged file (it opened silently, empty or with part of its voxels, and the
  Lesion card said "Unsaved changes" without an edit); the card says "Not saved yet" and leaving the subject keeps the file.
  A damaged scan is said too (its unread part is black), alone or with the lesion in one toast, also after the lesion was
  read ahead while it was intact.
- ``test_85_review_of_a_read_only_study_says_so_and_changes_nothing`` (BB-1): Approve or Request changes on a study whose folder
  cannot be written shows a warning toast that the review was not recorded and leaves the row as it was (an uncaught
  PermissionError of the audit log left it "pending" in memory).
- ``test_16b_read_only_study_says_so_and_logs_one_line_warnings`` (BB-1): a study folder that cannot be written is said
  when it opens, its save status is not "All changes saved", PermissionErrors of the heartbeat, the study save and the lesion
  QC jobs are one-line warnings (no traceback, no errno or lock name in the toast) and the quit prompt names the tracing.
"""

import inspect
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
import types
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

# Fill between slices (vtkITKMorphologicalContourInterpolator) on 40 traced slices, then quit
QUIT_AFTER_INTERPOLATION_SCRIPT = """
import numpy as np
import slicer
import vtk
import vtkITK
from vtk.util import numpy_support

array = np.zeros((120, 120, 120), np.uint8)
for index, z in enumerate(range(0, 120, 3)):
    array[z, 20 + index * 2:60 + index * 2, 20 + index:70 + index] = 1
    array[z, 80:100, 10 + index * 3:30 + index * 3] = 1
image = vtk.vtkImageData()
image.SetDimensions(array.shape[2], array.shape[1], array.shape[0])
image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(array.ravel(), deep=True))
interpolator = vtkITK.vtkITKMorphologicalContourInterpolator()
interpolator.SetInputData(image)
interpolator.Update()
result = numpy_support.vtk_to_numpy(interpolator.GetOutput().GetPointData().GetScalars())
print("QUITTEST READY filled=%d" % int((result > 0).sum()), flush=True)
slicer.app.exit(0)
"""

# Classic Segment Editor in an application that keeps the width of the module panel (not --testing): the panel width after each tool
CLASSIC_PANEL_WIDTH_SCRIPT = r"""
import json
import os
import time

import numpy as np
import slicer


def pump(ms):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        slicer.app.processEvents()
        time.sleep(0.005)


result = {"widths": {}, "error": ""}
try:
    mainWindow = slicer.util.mainWindow()
    mainWindow.resize(1366, 768)
    pump(1000)
    dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
    volume = slicer.util.addVolumeFromArray(np.zeros((40, 40, 40), np.uint8))
    pump(300)
    slicer.util.selectModule("SegmentEditor")
    pump(800)
    editor = slicer.modules.SegmentEditorWidget.editor
    segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
    segmentation.CreateDefaultDisplayNodes()
    editor.setSegmentationNode(segmentation)
    segmentID = segmentation.GetSegmentation().AddEmptySegment("", "Tumor")
    editor.setSourceVolumeNode(volume)
    editor.setCurrentSegmentID(segmentID)
    pump(300)
    steps = [("start", "")]
    steps += [(name, name) for name in ("Paint", "Threshold", "Mask volume", "Paint", "")]
    widths = []
    for label, effect in steps:
        if label != "start":
            editor.setActiveEffectByName(effect)
            pump(1500)
        widths.append([label or "none", int(dock.width)])
    result["widths"] = widths
except Exception:
    import traceback

    result["error"] = traceback.format_exc()
print("PANELWIDTH " + json.dumps(result), flush=True)
slicer.app.exit(0)
"""

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


#: Child process of test_89: a normal start (Studio installs its QFileOpenEvent filter), then slicer.app.exit(0)
QUIT_FILE_OPEN_FILTER_SCRIPT = r"""
import sys

import qt
import slicer


def quit():
    try:
        import Studio

        print("QUITTEST READY filter=%s" % (Studio._fileOpenFilter is not None), flush=True)
        slicer.app.exit(0)
    except Exception:
        import traceback

        traceback.print_exc()
        sys.stdout.flush()
        slicer.app.exit(3)


qt.QTimer.singleShot(3000, quit)
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


def _flagsValue(value):
    """The int of a QFlags property read, or None when it cannot be converted: with Qt 6 such a read (``widget.<flags>``
    or ``widget.property("<flags>")``) is an opaque object in PythonQt that int() rejects (Qt 5.15 gives a plain int).
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(value.value)
    except (AttributeError, TypeError, ValueError):
        return None


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

    def test_1b_keep_single_slice_reapply_measures_the_drawn_volume(self):
        """BB-1: changing the 2D policy back to "Keep single-slice" rewrites desc-lesion to the drawn slice, and volume_ml (the
        lesion-volume covariate) follows it. It kept the previous extrusion's volume, and the refreshed lesion_mtime and
        lesion_size made that stale value look current.
        """
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import lesionqc

        study = Study.create(os.path.join(self.directory, "single study"))
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        traced = np.zeros((40, 40, 40), np.uint8)
        traced[10:23, 10:23, 20] = 1  # 169 voxels of 1 mm on one axial slice
        study.add_subject("sub-01", space="MNI152NLin2009cAsym")
        relative = study.lesion_relpath("sub-01")
        path = study.abspath(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, traced, affine)
        study.set("sub-01", {"lesion": relative})
        study.save()

        def volume():
            return float(study.subject("sub-01")["volume_ml"])

        def analysisMl():
            return float(np.count_nonzero(np.squeeze(nifti.read_nifti(path)[0]))) / 1000.0

        lesionqc.setPolicy(study, lesionqc.MODE_BINARY, 4.0)  # ± 2 mm: 5 slices
        self.assertIsNotNone(lesionqc.regenerate(study, "sub-01", None, source="lesion"))
        self.assertAlmostEqual(analysisMl(), 0.845, places=6)
        self.assertAlmostEqual(volume(), 0.845, places=6)
        lesionqc.setPolicy(study, lesionqc.MODE_SINGLE)
        self.assertIsNotNone(lesionqc.regenerate(study, "sub-01", None, source="traced"))
        self.assertAlmostEqual(analysisMl(), 0.169, places=6)
        self.assertAlmostEqual(volume(), 0.169, places=6, msg="volume_ml follows the single-slice desc-lesion")
        # The same after a fractional extrusion
        lesionqc.setPolicy(study, lesionqc.MODE_FRACTIONAL, 2.0)
        lesionqc.regenerate(study, "sub-01", None, source="traced")
        self.assertNotAlmostEqual(volume(), 0.169, places=6)
        lesionqc.setPolicy(study, lesionqc.MODE_SINGLE)
        lesionqc.regenerate(study, "sub-01", None, source="traced")
        self.assertAlmostEqual(volume(), 0.169, places=6)
        # and on reopening the study
        self.assertAlmostEqual(float(Study.open(study.path).subject("sub-01")["volume_ml"]), 0.169, places=6)

    def test_1d_duplicate_flag_goes_when_the_partner_leaves_the_study(self):
        """BB-1: removing a subject from the study takes its duplicate pair with it: the partner keeps no ``duplicate`` flag, and
        putting the subject back (Undo) scans again.
        """
        from StudioLib import lesionqc, study_ui

        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        study_ui.openStudy(study.path, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        for _ in range(4):
            slicer.app.processEvents()
        session = study_ui.session()
        core = lesionqc.core()
        core.pairs = [("sub-01", "sub-02", 1.0)]
        core._applyDuplicateFlags()
        self.assertIn("duplicate", lesionqc.flagsOf(session.study.subject("sub-02")))
        self.assertEqual(session.removeSubjects(["sub-01"]), 1)
        self.assertEqual(core.pairs, [])
        self.assertNotIn("duplicate", lesionqc.flagsOf(session.study.subject("sub-02")))
        self.assertEqual(core.duplicatesOf("sub-02"), [])
        core._scanTimer.stop()
        self.assertEqual(session.undoRemove(), 1)
        self.assertTrue(core._scanTimer.isActive(), "the restored subject is scanned again")
        core._scanTimer.stop()

    def test_1c_binary_policy_semantics_follow_the_lesion_grid(self):
        """BB-1: the binary 2D policy's stored sentence (study.json, toast, methods "Lesion intake") counted the voxels at the
        template resolution (trace_policy.res, 2 mm by default) while each lesion is extruded on its own grid (1 mm here):
        "binary ±2 mm (3 voxels at 2 mm)" was recorded for an extrusion of 5 voxels at 1 mm, and the methods misreported it.
        """
        from LesionNetworkMappingLib import methods, nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import lesionqc

        study = Study.create(os.path.join(self.directory, "binary study"))
        study.trace_policy = {**(study.trace_policy or {}), "space": "MNI152NLin2009cAsym", "res": 2}
        traced = np.zeros((40, 40, 40), np.uint8)
        traced[10:23, 10:23, 20] = 1  # one axial slice on a 1 mm grid
        study.add_subject("sub-01", space="MNI152NLin2009cAsym")
        relative = study.lesion_relpath("sub-01")
        path = study.abspath(relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, traced, np.diag([1.0, 1.0, 1.0, 1.0]))
        study.set("sub-01", {"lesion": relative, "dimensionality": "2D"})
        study.save()

        stored = lesionqc.setPolicy(study, lesionqc.MODE_BINARY, 4.0)
        self.assertNotIn("at 2 mm", stored["semantics"])
        self.assertIn("±2 mm", stored["semantics"])
        result = lesionqc.regenerate(study, "sub-01", None, source="lesion")
        self.assertEqual(result["record"]["description"], "binary ±2 mm (5 voxels at 1 mm)")
        self.assertEqual(float(study.subject("sub-01")["extrude_mm"]), 5.0)
        sidecar = study.read_lesion_sidecar("sub-01")["SlicerStudio"]["extrude"]
        self.assertNotIn("voxels at 2 mm", sidecar["semantics"])
        self.assertIn("5 voxels at 1 mm", sidecar["description"])
        study.save()

        def methodsLine():
            text = methods.lesions_section(methods.Records(study.path, study=Study.open(study.path)), set()).text()
            return next(line for line in text.splitlines() if "2D extrusion policy" in line)

        line = methodsLine()
        self.assertNotIn("at 2 mm", line)
        self.assertIn("binary ±2 mm", line)
        self.assertIn("effective thickness per lesion: 5 mm 1", line)
        # A study saved before the fix keeps its sentence counted at the template resolution: the methods do not repeat it
        study.trace_policy = {**study.trace_policy, "extrude_2d": {**stored, "semantics": "binary ±2 mm (3 voxels at 2 mm)"}}
        study.save()
        line = methodsLine()
        self.assertNotIn("3 voxels at 2 mm", line)
        self.assertIn("effective thickness per lesion: 5 mm 1", line)
        # The sentence on one known grid still counts the voxels there
        self.assertEqual(lesionqc.policySemantics(lesionqc.MODE_BINARY, 4.0, 1.0), "binary ±2 mm (5 voxels at 1 mm)")

    # Atlas download failure (BB-1)

    def test_atlas_download_failure_chip_is_one_readable_line(self):
        """The Draw QC chip names the reason of a failed "Get atlases" job as a whole sentence: no cut at 120 characters in the
        middle of a word, no URL that cannot wrap, and the text stays a short sentence.
        """
        from StudioLib import lesionqc

        raw = ("No connection to templateflow.s3.amazonaws.com: the proxy server cannot be reached. Check the network and try again. "
               "See https://templateflow.s3.amazonaws.com/tpl-MNI152NLin6Asym/tpl-MNI152NLin6Asym_res-02_atlas-HOCPAL_desc-th25_dseg.nii.gz for the file.\nSecond line")
        for error, expectedPart in ((raw, "No connection to templateflow.s3.amazonaws.com: the proxy server cannot be reached."), ("", "unknown error")):
            core = types.SimpleNamespace(_atlasProcess=object(), _atlasFailed=True, _atlasError=error, atlasJob="running", _changed=lambda: None)
            lesionqc.QcCore._finishAtlasJob(core)
            reason = core.atlasJob
            self.assertIn(expectedPart, reason)
            self.assertNotIn("\n", reason)
            self.assertNotIn("https://", reason)
            self.assertLessEqual(len(reason), 160)
            if error:
                # cut at a word, ending in an ellipsis, never in the middle of a word or a URL
                self.assertTrue(reason.endswith("\u2026") or reason.endswith("."), reason)
                self.assertNotIn("amazonaws.c ", reason + " ")

    def test_missing_atlas_is_offered_next_to_a_location(self):
        """After a partial atlas download (the cortical atlas saved, the subcortical one not: disk full), or with only the paper's
        location, the Where chip still offers "Get atlases" and says the traced location is incomplete or why the last job failed.
        """
        from StudioLib import lesionqc

        cortical = [("Left Planum Temporale", 18.0)]
        for rows, row, job, expectedText in (
            (cortical, None, None, "incomplete"),
            (None, {"reported_location": "left thalamus"}, None, "needs atlases"),
            (cortical, None, "The disk is full", "The disk is full"),
        ):
            result = {"voxels": 100, "location": rows}
            chips = lesionqc.buildChips(result, row, mni=True, resourcesState="ready", missingAtlases=True, atlasJob=job)
            byKey = {chip["key"]: chip for chip in chips}
            self.assertIn("location", byKey)
            self.assertIn("atlases", byKey, [chip["key"] for chip in chips])
            self.assertIn(expectedText, byKey["atlases"]["text"])
            self.assertEqual([action["id"] for action in byKey["atlases"]["actions"]], ["getAtlases"])
        # Running: no button; all atlases present: no atlas chip
        chips = lesionqc.buildChips({"voxels": 100, "location": cortical}, mni=True, missingAtlases=True, atlasJob="running")
        self.assertEqual([chip["actions"] for chip in chips if chip["key"] == "atlases"], [[]])
        chips = lesionqc.buildChips({"voxels": 100, "location": cortical}, mni=True, missingAtlases=False)
        self.assertNotIn("atlases", [chip["key"] for chip in chips])

    def test_get_atlases_action_fits_the_default_inspector_width(self):
        """The "Get atlases" button is not middle-elided in the default 340 px inspector (its row is 274 px wide): the label is short
        enough for that row and the atlas names are in the tooltip (BB-1).
        """
        chips = self._atlasChips()
        action = chips["atlases"]["actions"][0]
        font = qt.QFont()
        font.setPixelSize(12)
        # 12 px horizontal padding on both sides of a ghost button
        self.assertLessEqual(qt.QFontMetrics(font).horizontalAdvance(action["text"]) + 24, 274, action["text"])
        self.assertIn("HOCPAL", action["tooltip"])
        self.assertIn("HOSPA", action["tooltip"])

    def _atlasChips(self):
        from StudioLib import lesionqc

        chips = lesionqc.buildChips({"voxels": 100, "location": [("Left Planum Temporale", 18.0)]}, mni=True, missingAtlases=True)
        return {chip["key"]: chip for chip in chips}

    # Damaged lesion file in the duplicate scan (BB-1)

    def test_82_truncated_lesion_file_does_not_stop_the_duplicate_scan(self):
        """A truncated .nii.gz (an interrupted copy: gzip raises EOFError) is skipped and said in words: the other lesions are
        still compared for duplicates, the file's old index entry is dropped, and the file check gives a chip, not a traceback.
        """
        from LesionNetworkMappingLib import nifti

        from StudioLib import lesionqc

        study = self._study([("sub-01", {}, False), ("sub-02", {}, False), ("sub-03", {}, False)])
        entries = [(sid, study.abspath(study.subject(sid)["lesion"])) for sid in ("sub-01", "sub-02", "sub-03")]
        worker = lesionqc._Worker()
        found = worker._do_scan(study.path, entries, frozenset())
        self.assertEqual({frozenset(pair[:2]) for pair in found["pairs"]},
                         {frozenset(("sub-01", "sub-02")), frozenset(("sub-01", "sub-03")), frozenset(("sub-02", "sub-03"))})
        # sub-02's file is cut to half its size (header intact)
        damaged = entries[1][1]
        with open(damaged, "rb") as f:
            content = f.read()
        with open(damaged, "wb") as f:
            f.write(content[:len(content) // 2])
        with self.assertRaises(EOFError):
            nifti.read_nifti(damaged)
        with self.assertLogs(level="WARNING") as logs:
            found = worker._do_scan(study.path, entries, frozenset())
        self.assertEqual([frozenset(pair[:2]) for pair in found["pairs"]], [frozenset(("sub-01", "sub-03"))])
        self.assertEqual(found["indexed"], 2)
        said = "\n".join(logs.output)
        self.assertIn("sub-02", said)
        self.assertIn("could not be read", said)
        self.assertNotIn("Traceback", said)
        # The next scan says it no more, and still compares the others
        with self.assertNoLogs(level="WARNING"):
            found = worker._do_scan(study.path, entries, frozenset())
        self.assertEqual([frozenset(pair[:2]) for pair in found["pairs"]], [frozenset(("sub-01", "sub-03"))])
        # The file check of the open subject: a Mask chip in words instead of a failed job
        key = lesionqc._stat(damaged)
        checked = worker._do_filehygiene("sub-02", damaged, None, False, key)
        self.assertEqual((checked["sid"], checked["key"]), ("sub-02", key))
        self.assertTrue(checked["hygiene"]["unreadable"])
        chips = [chip for chip in lesionqc.buildChips({"voxels": 0}, mni=True, fileHygiene=checked["hygiene"]) if chip["key"] == "hygiene"]
        self.assertEqual(len(chips), 1)
        self.assertIn("could not be read", chips[0]["text"])

    def test_83_closed_study_is_not_written_by_a_running_scan(self):
        """A duplicate scan that finishes after its study was closed (or the folder moved) writes nothing and recreates no folder."""
        from LesionNetworkMappingLib import lesionops

        from StudioLib import lesionqc

        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        entries = [(sid, study.abspath(study.subject(sid)["lesion"])) for sid in ("sub-01", "sub-02")]
        indexFile = lesionops.index_path(study.path)
        # Closed: no index file is written
        worker = lesionqc._Worker()
        worker.setClosed(study.path)
        worker._do_scan(study.path, entries, frozenset())
        self.assertFalse(os.path.exists(indexFile))
        # Reopened: it is written again
        worker.setClosed(study.path, False)
        worker._do_scan(study.path, entries, frozenset())
        self.assertTrue(os.path.isfile(indexFile))
        # A moved study folder is not recreated, closed or not
        gone = study.path + "-moved-away"
        os.rename(study.path, gone)
        try:
            lesionqc._Worker()._do_scan(study.path, entries, frozenset())
            self.assertFalse(os.path.exists(study.path))
        finally:
            os.rename(gone, study.path)

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

    def test_6b_map_seed_of_2d_study_subject_follows_the_extrusion_policy(self):
        """BB-1: Map's lesion seed of an open 2D study subject is the study's analysis lesion (desc-lesion), not the drawn slice."""
        from LesionNetworkMappingLib import geometry, nifti

        import LesionNetworkMapping
        from StudioLib import lesionqc, shell, study_ui

        if slicer.util.mainWindow() is None:
            self.skipTest("needs the main window")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Extruded study")
        os.makedirs(folder)
        traced = np.zeros(grid.shape, np.uint8)
        traced[40:50, 50:60, 40] = 1  # one axial slice
        cube = np.zeros(grid.shape, np.uint8)
        cube[40:44, 50:54, 40:44] = 1
        paths = [os.path.join(folder, "sub-01_lesion.nii.gz"), os.path.join(folder, "sub-02_lesion.nii.gz")]
        nifti.write_nifti(paths[0], traced, grid.affine)
        nifti.write_nifti(paths[1], cube, grid.affine)
        logic = LesionNetworkMapping.LesionNetworkMappingLogic()

        def slices(data):
            return int(np.unique(np.nonzero(data)[2]).size)

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            # The lesion as Done writes it, in lesions/ (an adopted file outside lesions/ is never extruded)
            relative = session.study.lesion_relpath("sub-01")
            os.makedirs(os.path.dirname(session.study.abspath(relative)), exist_ok=True)
            shutil.copyfile(paths[0], session.study.abspath(relative))
            session.study.set("sub-01", {"lesion": relative})
            lesionqc.setPolicy(session.study, lesionqc.MODE_FRACTIONAL, 6.0)  # 6 mm on 2 mm voxels: 3 whole slices
            session.flush()
            self.assertIsNotNone(lesionqc.regenerate(session.study, "sub-01", session, source="lesion"))
            lesionPath = session.study.abspath(session.study.subject("sub-01")["lesion"])
            analysis = np.squeeze(nifti.read_nifti(lesionPath)[0]) > 0
            self.assertEqual(slices(analysis), 3)
            self.assertTrue(session.open("sub-02", switchToDraw=True))
            self.assertTrue(session.open("sub-01", switchToDraw=True))
            for _ in range(4):
                slicer.app.processEvents()
            segmentationNode, segmentId = logic.defaultSeed()
            self.assertEqual(segmentationNode.GetID(), session.segmentationNode().GetID())
            drawn, _affine = logic.seedArray(segmentationNode, segmentId)
            self.assertEqual(slices(drawn), 1, "Draw shows the tracing as drawn (desc-traced)")
            data, _seedAffine, extrusion = logic.analysisSeedArray(segmentationNode, segmentId)
            self.assertIsNotNone(extrusion)
            self.assertEqual(extrusion["mode"], lesionqc.MODE_FRACTIONAL)
            self.assertEqual(slices(data), 3)
            self.assertEqual(int(np.count_nonzero(data)), int(np.count_nonzero(analysis)))
            check = logic.checkSeed(segmentationNode, segmentId)
            self.assertAlmostEqual(check["volumeMl"], np.count_nonzero(analysis) * 8.0 / 1000.0, places=6)
            self.assertTrue(check["extrusionNotice"])
            exported = logic.exportSeedLesion(segmentationNode, segmentId, os.path.join(self.directory, "seed", "lesion.nii.gz"))
            self.assertEqual(slices(np.squeeze(nifti.read_nifti(exported)[0])), 3, "the single-subject map uses the study's analysis lesion")
            # A study that keeps 2D tracings as a single slice maps the slice as drawn
            lesionqc.setPolicy(session.study, lesionqc.MODE_SINGLE)
            data, _seedAffine, extrusion = logic.analysisSeedArray(segmentationNode, segmentId)
            self.assertIsNone(extrusion)
            self.assertEqual(slices(data), 1)
            self.assertFalse(logic.checkSeed(segmentationNode, segmentId)["extrusionNotice"])
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_6c_imported_2d_lesions_get_the_extrusion_choice(self):
        """BB-1: 2D lesion files imported into a study (never drawn in Draw, where Done asks for the 2D policy) get their
        dimensionality, the 2D filter lists them, a toast asks for the study-level choice, the preflight warns while there is
        none, and the choice applies to them (the study's own copy in lesions/; the imported files are never modified).
        Before, they were mapped as single slices with no choice, no warning, and a policy chosen later had no effect.
        """
        import hashlib

        from LesionNetworkMappingLib import geometry, nifti, preflight

        import LesionNetworkMapping
        from StudioLib import libcard_flags, lesionqc, shell, study_ui

        if slicer.util.mainWindow() is None:
            self.skipTest("needs the main window")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Imported 2D study")
        os.makedirs(folder)
        one = np.zeros(grid.shape, np.uint8)
        one[40:50, 50:60, 40] = 1  # one axial slice
        two = np.zeros(grid.shape, np.uint8)
        two[40:50, 50:60, 40] = 1
        two[40:50, 50:60, 48] = 1  # two separate axial slices
        cube = np.zeros(grid.shape, np.uint8)
        cube[40:44, 50:54, 40:44] = 1
        paths = [os.path.join(folder, f"sub-0{index}_lesion.nii.gz") for index in (1, 2, 3)]
        for path, data in zip(paths, (one, two, cube), strict=True):
            nifti.write_nifti(path, data, grid.affine)

        def digest(path):
            with open(path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()

        original = {path: digest(path) for path in paths}

        def seedSlices(study):
            info = LesionNetworkMapping.LesionNetworkMappingLogic.cohortSeeds(study)
            return {sid: int(np.unique(np.nonzero(np.squeeze(nifti.read_nifti(path)[0]))[2]).size) for sid, path in info["lesions"]}

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            study = session.study
            core = lesionqc.core()
            core.requestDimensions(session)
            self.assertTrue(core.waitIdle(30))
            self.assertEqual({sid: study.subject(sid).get("dimensionality") for sid in ("sub-01", "sub-02", "sub-03")},
                             {"sub-01": "2D", "sub-02": "multi2D", "sub-03": "3D"})
            self.assertEqual([row["participant_id"] for row in study.subjects if libcard_flags.isTwoD(row)], ["sub-01", "sub-02"])
            self.assertIsNone(lesionqc.storedPolicy(study))
            messages = [text for _when, text, _level in lesionqc._shell().recentMessages()]  # the application's shell
            self.assertTrue(any(text.startswith("2 lesions are single-slice (2D) tracings") for text in messages), messages)
            # The preflight of Map and Cohort says that they would be mapped as single slices
            found = [check for check in preflight.run_preflight(study)["checks"] if check["id"] == "two_d_no_policy"]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["level"], "warn")
            self.assertEqual(found[0]["subjects"], ["sub-01", "sub-02"])
            self.assertEqual(found[0]["fix"]["id"], preflight.FIX_TWO_D_POLICY)
            self.assertEqual(seedSlices(study), {"sub-01": 1, "sub-02": 2, "sub-03": 4})
            # The choice (the same card as on Done in Draw) applies to the imported lesions
            card = core.askImportedPolicy(session)
            self.assertIsNotNone(card)
            card.spin.setValue(6.0)  # 6 mm on 2 mm voxels: 3 whole slices
            card._onFractional()
            self.assertEqual(lesionqc.storedPolicy(study)["mode"], lesionqc.MODE_FRACTIONAL)
            self.assertEqual(seedSlices(study), {"sub-01": 3, "sub-02": 6, "sub-03": 4})
            for sid in ("sub-01", "sub-02"):
                self.assertTrue(study.subject(sid)["lesion"].startswith("lesions/"), study.subject(sid)["lesion"])
            self.assertEqual(study.subject("sub-03")["lesion"], "sub-03_lesion.nii.gz")
            self.assertEqual({path: digest(path) for path in paths}, original, "the imported files are never modified")
            self.assertEqual([check["id"] for check in preflight.run_preflight(study)["checks"] if check["id"] == "two_d_no_policy"], [])
            # Changing the policy again re-applies it from the copy of the imported file (desc-traced)
            core.choosePolicy(session, lesionqc.MODE_SINGLE)
            self.assertEqual(seedSlices(study), {"sub-01": 1, "sub-02": 2, "sub-03": 4})
        finally:
            lesionqc.core().hidePolicyCard()
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # Adopted participants.tsv with columns named like the study's (BB-1)

    def _adopt(self, name, header, values, dictionary=None, lesions=()):
        """Adopt a folder of sub-01..03 T1w scans (and lesion files of the subject numbers ``lesions``) and a
        participants.tsv of ``header`` and ``values`` (one list per subject).
        """
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
        for index in lesions:
            paths.append(os.path.join(folder, f"sub-0{index}_lesion.nii.gz"))
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

    def test_7b_adopted_clinical_lesion_column_kept(self):
        """A participants.tsv with a clinical "lesion" column (yes / no) was read as lesion paths: the folder's lesion files
        were skipped ("already has a lesion") and tracing overwrote the user's values (BB-1). The column is kept as
        lesion_original and the study's lesion column pairs the files.
        """
        from LesionNetworkMappingLib.study import read_tsv

        header = ["participant_id", "diagnosis", "lesion", "age"]
        values = [["sub-01", "stroke", "yes", "60"], ["sub-02", "stroke", "yes", "61"], ["sub-03", "control", "no", "62"]]
        folder, study = self._adopt("Clinical lesion", header, values, {"lesion": {"Description": "Lesion on MRI"}}, lesions=(1,))
        self.assertEqual(study.adoptedRenames, {"lesion": "lesion_original"})
        self.assertEqual(study.columns[:4], ["participant_id", "diagnosis", "lesion_original", "age"])
        rows = [study.subject(sid) for sid in study.subject_ids]
        self.assertEqual([row["lesion_original"] for row in rows], ["yes", "yes", "no"])
        self.assertEqual(rows[0]["lesion"], "sub-01_lesion.nii.gz")
        self.assertEqual([row["status"] for row in rows], ["traced", "untraced", "untraced"])
        self.assertTrue(all(row["lesion"] == "n/a" for row in rows[1:]))
        # Tracing sub-02 writes the study's lesion column only
        study.set("sub-02", lesion="lesions/sub-02_desc-lesion_mask.nii.gz", status="traced")
        study.save()
        columns, saved = read_tsv(os.path.join(folder, "participants.tsv"))
        self.assertIn("lesion", columns)
        self.assertEqual([row["lesion_original"] for row in saved], ["yes", "yes", "no"])
        with open(os.path.join(folder, "participants.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["lesion_original"]["Description"], "Lesion on MRI")
        # A lesion column of paths (also of a missing file) is the study's own column
        header = ["participant_id", "lesion"]
        folder, study = self._adopt("Earlier lesions", header, [["sub-01", "sub-01_lesion.nii.gz"], ["sub-02", "lesions/gone.nii.gz"], ["sub-03", "n/a"]], lesions=(1,))
        self.assertEqual(study.adoptedRenames, {})
        self.assertNotIn("lesion_original", study.columns)
        self.assertEqual([study.subject(sid)["lesion"] for sid in study.subject_ids], ["sub-01_lesion.nii.gz", "lesions/gone.nii.gz", "n/a"])

    def test_7d_adopted_absolute_scan_paths_made_relative(self):
        """Absolute scan paths inside the adopted folder stayed absolute in participants.tsv, and the same files found
        by name were reported as skipped ("already has a scan") (BB-1). They are stored relative to the study.
        """
        from LesionNetworkMappingLib.study import read_tsv

        folder = os.path.join(self.directory, "Absolute scans")
        header = ["participant_id", "scan"]
        values = [[f"sub-0{n}", os.path.join(folder, f"sub-0{n}_T1w.nii.gz")] for n in (1, 2, 3)]
        folder, study = self._adopt("Absolute scans", header, values)
        self.assertEqual([study.subject(sid)["scan"] for sid in study.subject_ids], [f"sub-0{n}_T1w.nii.gz" for n in (1, 2, 3)])
        study.save()
        _, saved = read_tsv(os.path.join(folder, "participants.tsv"))
        self.assertEqual([row["scan"] for row in saved], [f"sub-0{n}_T1w.nii.gz" for n in (1, 2, 3)])
        # An absolute cell that names the same file is not a different scan
        study.set("sub-01", scan=os.path.join(folder, "sub-01_T1w.nii.gz"))
        report = study.add_files([os.path.join(folder, f"sub-0{n}_T1w.nii.gz") for n in (1, 2, 3)])
        self.assertEqual(report["skipped"], [])

    def test_7c_adopted_user_space_column_kept(self):
        """A participants.tsv with a user "space" column (field strength 1.5T / 3T) became the study's space column: traced
        lesions were named space-3T, no brain mask applied (outside_brain_pct n/a) and the share export left the lesions out
        as native space or shared them as space-3T (BB-1). The column is kept as space_original and the study's space comes
        from the files' grids.
        """
        from LesionNetworkMappingLib import share
        from LesionNetworkMappingLib.study import read_tsv

        from StudioLib import study_ui

        _folder, control = self._adopt("No space column", ["participant_id", "age"], [["sub-01", "60"], ["sub-02", "61"], ["sub-03", "62"]], lesions=(1,))
        expected = control.subject("sub-01")["space"]
        self.assertNotIn(expected, (None, "", "n/a", "native"))
        header = ["participant_id", "diagnosis", "space", "age"]
        values = [["sub-01", "stroke", "1.5T", "60"], ["sub-02", "stroke", "3T", "61"], ["sub-03", "control", "3T", "62"]]
        folder, study = self._adopt("Field strength", header, values, {"space": {"Description": "Scanner field strength"}}, lesions=(1,))
        self.assertEqual(study.adoptedRenames, {"space": "space_original"})
        self.assertEqual(study.columns[:4], ["participant_id", "diagnosis", "space_original", "age"])
        rows = [study.subject(sid) for sid in study.subject_ids]
        self.assertEqual([row["space_original"] for row in rows], ["1.5T", "3T", "3T"])
        self.assertEqual([row["space"] for row in rows], [expected] * 3)
        self.assertEqual(study.lesion_relpath("sub-02"), f"lesions/sub-02_space-{expected}_desc-lesion_mask.nii.gz")
        self.assertEqual(share._lesion_space(study, rows[0]), expected)
        columns, saved = read_tsv(os.path.join(folder, "participants.tsv"))
        self.assertIn("space", columns)
        self.assertEqual([row["space_original"] for row in saved], ["1.5T", "3T", "3T"])
        with open(os.path.join(folder, "participants.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["space_original"]["Description"], "Scanner field strength")
        # Importing such a table into the open study keeps the study's space too
        table = os.path.join(self.directory, "field-strength.tsv")
        with open(table, "w", encoding="utf-8") as f:
            f.write("participant_id\tspace\nsub-01\t7T\nsub-02\t3T\n")
        result = study_ui._importTable(study, table)
        self.assertEqual(result["renamed"], {"space": "space_original"})
        self.assertEqual([study.subject(sid)["space"] for sid in ("sub-01", "sub-02")], [expected] * 2)
        self.assertEqual([study.subject(sid)["space_original"] for sid in ("sub-01", "sub-02")], ["7T", "3T"])
        # A space column of template ids and native (any case) is the study's own column
        folder, study = self._adopt("Spaces", ["participant_id", "space"], [["sub-01", "MNI152NLin2009cAsym"], ["sub-02", "native"], ["sub-03", "n/a"]])
        self.assertEqual(study.adoptedRenames, {})
        self.assertNotIn("space_original", study.columns)
        self.assertEqual([study.subject(sid)["space"] for sid in ("sub-01", "sub-02")], ["MNI152NLin2009cAsym", "native"])

    def test_73_project_closed_outside_draw_frees_its_lesion(self):
        """Closing a project from Library kept its lesion in memory: ``draw._metricsCache`` held the closed project's
        vtkSegmentation (one entry per project, keyed by a new segment ID each time) and only Draw's close observer, active only
        while Draw is shown, cleared it (BB-1).
        """
        import gc

        import vtk

        from StudioLib import draw, shell

        if slicer.util.mainWindow() is None:
            self.skipTest("needs the main window")

        def pump():
            for _ in range(5):
                slicer.app.processEvents()

        slicer.mrmlScene.Clear(0)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            # Draw, then Library: the usual way to switch projects
            studioShell.switchTo("draw")
            pump()
            studioShell.switchTo("library")
            pump()
            for cycle in range(3):
                image = np.zeros((20, 22, 24), np.int16)
                image[5:12, 6:14, 7:15] = 100
                volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan")
                slicer.util.updateVolumeFromArray(volumeNode, image)
                segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
                segmentationNode.CreateDefaultDisplayNodes()
                segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
                # A new segment ID in each project, as Studio's lesions have
                segmentId = segmentationNode.GetSegmentation().AddEmptySegment(f"2.25.7300{cycle}", "lesion")
                slicer.util.updateSegmentBinaryLabelmapFromArray((image > 50).astype(np.uint8), segmentationNode, segmentId, volumeNode)
                self.assertGreater(draw.lesionMetrics(segmentationNode, compute=True, segmentId=segmentId)["voxels"], 0)
                draw._isMniLesion(segmentationNode)
                self.assertTrue(draw._metricsCache)
                self.assertTrue(draw._mniCache)
                saved = vtk.vtkWeakReference()
                saved.Set(segmentationNode)
                draw._savedRecords[segmentationNode.GetID()] = (saved, os.path.join(self.directory, "lesion.seg.nrrd"), None, 0)
                segmentation = vtk.vtkWeakReference()
                segmentation.Set(segmentationNode.GetSegmentation())
                del volumeNode, segmentationNode
                # The project is closed while Library is shown (Ctrl+N, Ctrl+W or opening the next project)
                slicer.mrmlScene.Clear(0)
                pump()
                gc.collect()
                self.assertEqual(len(draw._metricsCache), 0, f"project {cycle + 1}: the closed project's lesion metrics are kept")
                self.assertEqual(len(draw._mniCache), 0)
                self.assertEqual(len(draw._savedRecords), 0)
                self.assertIsNone(segmentation.Get(), f"project {cycle + 1}: the closed project's lesion is still in memory")
        finally:
            studioShell.dispose()
            pump()
            slicer.mrmlScene.Clear(0)

    def test_72_imported_clinical_table_keeps_status_and_include(self):
        from LesionNetworkMappingLib.study import Study

        from StudioLib import importer, study_ui

        folder, study = self._adopt("Import", ["participant_id", "age"], [["sub-01", "60"], ["sub-02", "61"], ["sub-03", "62"]])
        study.set("sub-01", status="traced", qc="pass")
        study.save()
        # The library reads include 1.0 / 0.0 (pandas) as 1 / 0 and still refuses a value that means nothing in the study
        pandasPath = os.path.join(self.directory, "pandas.tsv")
        with open(pandasPath, "w", encoding="utf-8") as f:
            f.write("participant_id\tmrs\tinclude\nsub-01\t2\t1.0\nsub-02\t3\t0.0\nsub-03\t4\t\n")
        library = Study.open(folder)
        library.import_tsv(pandasPath)
        self.assertEqual([(row["mrs"], row["include"]) for row in library.subjects], [("2", "1"), ("3", "0"), ("4", "1")])
        badPath = os.path.join(self.directory, "bad.tsv")
        with open(badPath, "w", encoding="utf-8") as f:
            f.write("participant_id\tqc\nsub-01\tmaybe\n")
        with self.assertRaises(ValueError):
            library.import_tsv(badPath)

        class FakeShell:
            def __init__(self):
                self.toasts = []

            def isActive(self):
                return True

            def toast(self, text, *args, **kwargs):
                self.toasts.append((str(text), kwargs.get("level")))

        fake = FakeShell()
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        clinical = os.path.join(self.directory, "clinical.tsv")
        with open(clinical, "w", encoding="utf-8") as f:
            f.write("participant_id\tage\tstatus\tinclude\tNIHSS\n"
                    "sub-01\t70\talive\tyes\t4\nsub-02\t71\tdeceased\tno\t9\nsub-03\t72\talive\tn/a\t12\n")
        importer._openParticipantsTable(clinical, fake)
        self.assertEqual(len(fake.toasts), 1, fake.toasts)
        self.assertNotIn("not available", fake.toasts[0][0])
        self.assertIn("3 updated", fake.toasts[0][0])
        self.assertIn("kept status as status_original", fake.toasts[0][0])
        rows = {row["participant_id"]: row for row in session.study.subjects}
        self.assertEqual([(rows[sid]["age"], rows[sid]["NIHSS"]) for sid in ("sub-01", "sub-02", "sub-03")], [("70", "4"), ("71", "9"), ("72", "12")])
        # The study's own columns: tracing status and QC unchanged, include from the yes / no (a missing cell keeps 1)
        self.assertEqual([rows[sid]["status"] for sid in ("sub-01", "sub-02", "sub-03")], ["traced", "untraced", "untraced"])
        self.assertEqual(rows["sub-01"]["qc"], "pass")
        self.assertEqual([rows[sid]["include"] for sid in ("sub-01", "sub-02", "sub-03")], ["1", "0", "1"])
        self.assertEqual([rows[sid]["status_original"] for sid in ("sub-01", "sub-02", "sub-03")], ["alive", "deceased", "alive"])
        self.assertNotIn("include_original", session.study.columns)  # yes / no keep their meaning: the study's own column
        # Importing the same table again updates the same columns (no status_original2)
        importer._openParticipantsTable(clinical, fake)
        self.assertNotIn("status_original2", session.study.columns)
        # A table that cannot be imported (ids that collide) says why, never that studies are not available
        with open(badPath, "w", encoding="utf-8") as f:
            f.write("participant_id\tage\n01\t1\nsub-01\t2\n")
        fake.toasts.clear()
        importer._openParticipantsTable(badPath, fake)
        self.assertEqual(len(fake.toasts), 1, fake.toasts)
        self.assertIn("bad.tsv was not imported", fake.toasts[0][0])
        self.assertNotIn("not available", fake.toasts[0][0])
        self.assertNotIn(".studio", fake.toasts[0][0])  # the reason names the user's table, not the temporary copy
        self.assertEqual(fake.toasts[0][1], "warning")

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

    def test_8b_study_list_headers_follow_the_font(self):
        """The study list column widths follow the application font: at 125 % (17 px) no header ('Lesion mL') is cut
        mid-letter (BB-1; the widths were fixed pixels).
        """
        import numpy as np
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import library, shell, study_ui, ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Header study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        studyList = None
        savedFont = qt.QFont(qt.QApplication.font())
        try:
            study_ui.createStudy(paths, studioShell, folder=folder)
            large = qt.QFont(savedFont)
            large.setPixelSize(17)
            qt.QApplication.setFont(large)
            studyList = library.StudyList()
            studyList.widget.resize(900, 400)
            studyList.widget.show()
            studyList.refresh()
            for _ in range(6):
                slicer.app.processEvents()
            header = studyList.table.horizontalHeader()
            columns = [entry["key"] for entry in studyList.model.columns]
            self.assertIn("volume_ml", columns)
            for index, key in enumerate(columns):
                self.assertGreaterEqual(header.sectionSize(index), header.sectionSizeHint(index), f"header {key} is cut")
            # The designed width (90 px for a 13 px body) scales with the font
            self.assertGreaterEqual(header.sectionSize(columns.index("volume_ml")), ui.fontPx(90))
        finally:
            qt.QApplication.setFont(savedFont)
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

    def test_9d_done_next_ends_the_flagged_queue(self):
        """Done -> next in the Flagged queue opens each flagged case once and then says "No more cases in Flagged": a flag
        that Done leaves (multipart) did not make the queue cycle between two flagged cases forever (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Flagged study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 5):
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
            study = session.study
            for sid in ("sub-01", "sub-03"):
                study.set(sid, qc_flags="multipart")
            session.open("sub-01", switchToDraw=False)
            session.setQueue(study_ui.QUEUE_FLAGGED)
            self.assertEqual(session.queueMembers(), ["sub-01", "sub-03"])
            opened = []
            for _ in range(4):
                del toasts[:]
                before = session.current()
                session.done(writeLesion=False)
                opened.append((before, session.current()))
                if [text for text in toasts if "No more cases in Flagged" in text]:
                    break
            # sub-01 -> sub-03, then the queue is finished and stays on sub-03 (its flag is still there)
            self.assertEqual(opened, [("sub-01", "sub-03"), ("sub-03", "sub-03")])
            self.assertTrue([text for text in toasts if "No more cases in Flagged" in text], toasts)
            # A new pass (the queue chosen again) offers the flagged cases again
            session.setQueue(study_ui.QUEUE_FLAGGED)
            self.assertEqual(session.next_subject("sub-03"), "sub-01")
            session.setQueue(None)
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

    def test_16b_read_only_study_says_so_and_logs_one_line_warnings(self):
        """A study whose folder cannot be written says so when it opens (changes cannot be saved), its card does not say
        "All changes saved", the expected PermissionErrors are warnings without a traceback, the toast gives the reason in
        plain words and the quit prompt names the unsaved tracing (BB-1).
        """
        from unittest import mock

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import firstrun, lesionqc, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Shared read-only study")
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
            logging.getLogger().addHandler(handler)
            try:
                # The study opens from a folder that cannot be written, and stays so (os.access says so; the tests run as
                # root): a folder found writable again ends the read-only state (test_16bc)
                notWritable = mock.patch.object(study_ui.StudySession, "_studyWritable", return_value=False)
                notWritable.start()
                self.addCleanup(notWritable.stop)
                session = study_ui.createStudy(paths, studioShell, folder=folder)
                self.assertIsNotNone(session)
                self.assertTrue(session.readOnly)
                self.assertTrue([text for text in toasts if "read-only" in text and "cannot be saved" in text], toasts)
                self.assertNotIn("All changes saved", session.saveStatus()[0])
                # The heartbeat, the study save and a lesion QC job all hit PermissionError
                error = PermissionError(13, "Permission denied", os.path.join(folder, ".studio", "participants.lock"))
                toasts[:] = []
                session.study.set("sub-01", notes="changed")
                with mock.patch.object(session.study, "save", side_effect=error):
                    self.assertFalse(session.flush())
                # The read-only state was announced once when the study opened: no "could not be saved" toast on later saves
                self.assertEqual(toasts, [])
                for text in toasts:
                    self.assertNotIn("Errno", text)
                    self.assertNotIn("participants.lock", text)
                # (a stand-in for the QC core: the shared one is not created here)
                qcError = ("Traceback (most recent call last):\n  File x\n"
                           "PermissionError: [Errno 13] Permission denied: '/x/.studio/cache'\n")
                fakeWorker = types.SimpleNamespace(pending=1, drain=lambda: [("scan", None, None, qcError)])
                lesionqc.QcCore._drain(types.SimpleNamespace(worker=fakeWorker))
                self.assertTrue([r for r in records if r.levelno == logging.WARNING and "lesion QC job scan" in r.getMessage()])
                # The quit prompt names the tracing that cannot be written
                session.open("sub-01", switchToDraw=False)
                session.markDirty()
                with mock.patch.object(session, "writeCurrent", return_value=False), mock.patch.object(session, "isDirty", return_value=True):
                    result = firstrun.studyCloseSummary()
                self.assertEqual(result[0], firstrun.CLOSE_MODE_LESION_SAVED)
                self.assertIn("sub-01", result[1])
                self.assertIn("read-only", result[1])
            finally:
                logging.getLogger().removeHandler(handler)
            self.assertEqual([record for record in records if record.levelno >= logging.ERROR], [])
            self.assertFalse([record for record in records if record.exc_info or "Traceback" in record.getMessage()])
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_16bb_read_only_study_refuses_a_table_and_files_plainly(self):
        """Importing a participants table or adding files into a read-only study says the study is read-only and nothing
        was imported: no errno, no temporary file name, no traceback; the drop path toasts the same words (BB-1).
        """
        from unittest import mock

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import importer, library, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Shared read-only table study")
        os.makedirs(folder)
        paths = [os.path.join(folder, f"sub-0{index}_T1w.nii.gz") for index in (1, 2)]
        for path in paths:
            nifti.write_nifti(path, np.zeros(grid.shape, np.uint8), grid.affine)
        table = os.path.join(self.directory, "ro-clinical.tsv")
        with open(table, "w", encoding="utf-8") as f:
            f.write("participant_id\tscore\nsub-01\t3\nsub-02\t4\n")
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            with mock.patch.object(study_ui.StudySession, "_studyWritable", return_value=False):
                session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertTrue(session.readOnly)
            with mock.patch.object(study_ui.StudySession, "_studyWritable", return_value=False):
                with self.assertRaises(study_ui.ReadOnlyStudyError) as caught:
                    session.importTable(table)
                self.assertIn("read-only", str(caught.exception))
                self.assertIn("not imported", str(caught.exception))
                self.assertNotIn("Errno", str(caught.exception))
                self.assertNotIn(".tmp", str(caught.exception))
                with self.assertRaises(study_ui.ReadOnlyStudyError):
                    session.addFiles(paths)
                # The Library handler shows the message without a traceback (a user problem, not a program error)
                records = []

                class Recorder(logging.Handler):
                    def emit(self, record):
                        records.append(record)

                handler = Recorder(level=logging.DEBUG)
                logging.getLogger().addHandler(handler)
                try:
                    self.assertIsNone(library._runAction(lambda: session.importTable(table), "Failed to import the table."))
                    # Open… / drop
                    toasts[:] = []
                    shellToasts = []
                    with mock.patch.object(studioShell, "toast", lambda text, **kwargs: shellToasts.append(str(text))):
                        importer._openParticipantsTable(table, studioShell)
                finally:
                    logging.getLogger().removeHandler(handler)
                self.assertEqual([record for record in records if record.levelno >= logging.ERROR], [])
                self.assertFalse([record for record in records if record.exc_info or "Traceback" in record.getMessage()])
                self.assertTrue([text for text in shellToasts if "read-only" in text and "ro-clinical.tsv" in text], shellToasts)
                self.assertFalse([text for text in shellToasts if "Errno" in text or ".tmp" in text], shellToasts)
            self.assertFalse(os.path.exists(os.path.join(folder, ".studio", f"import-{os.getpid()}.tsv")))
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_16bc_a_study_made_writable_again_saves_and_stops_saying_read_only(self):
        """A study folder that was read-only and is made writable again during the session: the session stops calling
        itself read-only (the top bar), the heartbeat resumes, and importing a table, adding files and the agreement
        work again without closing and reopening the study. The read-only state stuck for the whole session (BB-1).
        """
        import json
        from unittest import mock

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Writable again study")
        os.makedirs(folder)
        paths = [os.path.join(folder, f"sub-0{index}_T1w.nii.gz") for index in (1, 2)]
        for path in paths[:1]:
            nifti.write_nifti(path, np.zeros(grid.shape, np.uint8), grid.affine)
        table = os.path.join(self.directory, "writable-again.tsv")
        with open(table, "w", encoding="utf-8") as f:
            f.write("participant_id\tscore\nsub-01\t3\n")
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            # The folder cannot be written when the study opens (os.access says so; the tests run as root)
            with mock.patch.object(study_ui.StudySession, "_studyWritable", return_value=False):
                session = study_ui.createStudy(paths[:1], studioShell, folder=folder)
                self.assertTrue(session.readOnly)
                with self.assertRaises(study_ui.ReadOnlyStudyError):
                    session.importTable(table)
            sessionFile = session._sessionFile()
            if os.path.exists(sessionFile):
                os.remove(sessionFile)
            # Write access is given back: the next heartbeat is written (two-window detection works again)
            session._writeHeartbeat()
            self.assertFalse(session.readOnly, "the session stayed read-only after the folder became writable")
            self.assertTrue(os.path.exists(sessionFile), "the heartbeat stopped for good")
            with open(sessionFile, encoding="utf-8") as f:
                self.assertGreater(json.load(f)["heartbeat_epoch"], 0)

            # Read-only again through a failed save, then writable again: a successful save says so
            error = PermissionError(13, "Permission denied", os.path.join(folder, ".studio", "participants.lock"))
            session.study.set("sub-01", notes="changed")
            with mock.patch.object(session.study, "save", side_effect=error):
                self.assertFalse(session.flush())
            self.assertTrue(session.readOnly)
            self.assertIn("Read-only", session.saveStatus()[0])
            session.study.set("sub-01", notes="changed again")
            self.assertTrue(session.flush())
            self.assertFalse(session.readOnly)
            self.assertEqual(session.saveStatus()[0], "All changes saved")

            # Read-only again, then writable again: Import table, Add files and Compute agreement are not refused
            with mock.patch.object(session.study, "save", side_effect=error):
                session.study.set("sub-01", notes="third")
                self.assertFalse(session.flush())
            self.assertTrue(session.readOnly)
            session._requireWritable("The agreement was not computed.")  # raised ReadOnlyStudyError before the fix
            self.assertFalse(session.readOnly)
            session.flush()
            with mock.patch.object(session.study, "save", side_effect=error):
                session.study.set("sub-01", notes="fourth")
                self.assertFalse(session.flush())
            self.assertTrue(session.readOnly)
            session.importTable(table)
            self.assertFalse(session.readOnly)
            nifti.write_nifti(paths[1], np.zeros(grid.shape, np.uint8), grid.affine)
            session.addFiles(paths[1:])
            self.assertIn("sub-02", session.study)
            self.assertFalse([text for text in toasts if "not imported" in text or "not added" in text], toasts)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_16c_closing_a_read_only_study_does_not_try_to_keep_the_changes(self):
        """Closing a read-only study with changes that cannot be saved says nothing more (no inbox is posted to) and
        logs no error or traceback (BB-1).
        """
        from unittest import mock

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Shared read-only study close")
        os.makedirs(folder)
        path = os.path.join(folder, "sub-01_T1w.nii.gz")
        nifti.write_nifti(path, np.zeros(grid.shape, np.uint8), grid.affine)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        records = []

        class Recorder(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Recorder(level=logging.DEBUG)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            with mock.patch.object(study_ui.StudySession, "_studyWritable", return_value=False):
                session = study_ui.createStudy([path], studioShell, folder=folder)
            self.assertTrue(session.readOnly)
            session.study.set("sub-01", notes="changed")
            error = PermissionError(13, "Permission denied", os.path.join(folder, ".studio", "participants.lock"))
            logging.getLogger().addHandler(handler)
            try:
                with mock.patch.object(session.study, "save", side_effect=error), \
                        mock.patch.object(session.study, "keep_unsaved", side_effect=PermissionError(13, "Permission denied")) as keep:
                    study_ui.closeStudy()
                    keep.assert_not_called()
            finally:
                logging.getLogger().removeHandler(handler)
            self.assertEqual([r for r in records if r.levelno >= logging.ERROR], [])
            self.assertFalse([r for r in records if r.exc_info or "Traceback" in r.getMessage()])
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
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

    def test_33_extensions_manager_asks_for_the_official_build(self):
        """The Extensions Manager asks the server for the extensions of the official build the fork is based on."""
        import sys
        import textwrap

        # The server matches the revision exactly and has extensions only for official builds: the fork's own
        # revision (a commit count of the clone) matches none, so the catalog was empty ("All (0)") and install by
        # name failed. Official 5.13.0-2026-10-16 is revision 35389, built from upstream commit bccfb40, the commit the
        # fork is based on (update both when a newer upstream is merged: Slicer_UPSTREAM_REVISION in SlicerVersion.cmake).
        em = slicer.app.extensionsManagerModel()
        self.assertEqual(em.slicerRevision, "35389")
        url = em.extensionsListUrl().toString()
        if url:  # empty only if the frontend server is not set
            self.assertIn("/catalog/All/35389/", url)
        # The build keeps its own revision: it names its settings file and its extensions folder
        installPath = slicer.app.extensionsInstallPath
        self.assertTrue(installPath.replace("\\", "/").rstrip("/").endswith(f"Extensions-{slicer.app.revision}"), installPath)
        # A scripted module of an installed extension is loaded as an extension (its folder is added to sys.path for its
        # own helper packages), not as a module embedded in the application.
        name = "StudioBBExtensionProbe"
        extensionDir = os.path.join(installPath, name)
        moduleDir = os.path.join(extensionDir, "lib", f"Slicer-{slicer.app.majorVersion}.{slicer.app.minorVersion}", "qt-scripted-modules")
        os.makedirs(moduleDir, exist_ok=True)
        self.addCleanup(shutil.rmtree, extensionDir, True)
        modulePath = os.path.join(moduleDir, f"{name}.py")
        with open(modulePath, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent(f"""\
                from slicer.ScriptedLoadableModule import ScriptedLoadableModule


                class {name}(ScriptedLoadableModule):
                    def __init__(self, parent):
                        ScriptedLoadableModule.__init__(self, parent)
                        self.parent.title = "{name}"
                        self.parent.hidden = True
                """))
        factory = slicer.app.moduleManager().factoryManager()
        factory.registerModule(qt.QFileInfo(modulePath))
        self.assertTrue(factory.isRegistered(name))
        self.assertTrue(factory.loadModules([name]))

        def norm(path):
            return os.path.normcase(os.path.abspath(path))

        def removeFromSysPath():
            sys.path[:] = [p for p in sys.path if norm(p) != norm(moduleDir)]

        self.addCleanup(removeFromSysPath)
        self.assertIn(norm(moduleDir), [norm(p) for p in sys.path], "an extension's module was taken for an embedded module")

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

    def test_12_read_only_study_dimensionality_audit_skipped(self):
        """A read-only study: the dimensionality audit append fails quietly (one warning) and the 2D question is still asked."""
        from StudioLib import lesionqc

        asked = []

        class Row(dict):
            pass

        class Study:
            path = "/study"

            def __contains__(self, sid):
                return True

            def subject(self, sid):
                return {"lesion": "l.nii.gz"}

            def abspath(self, rel):
                return rel

        class Session:
            study = Study()

            def setValues(self, sids, values):
                pass

            def setSubjectValues(self, valuesBySid):
                pass

            def flush(self, lockTimeout=0):
                pass

        fake = types.SimpleNamespace(studyPath="/study", _askImported=asked.append)
        saved = (lesionqc._session, lesionqc._appendAudit, lesionqc._stat, lesionqc.dimensionColumns)
        session = Session()

        def deny(study, record):
            raise PermissionError(13, "Permission denied")

        lesionqc._session = lambda: session
        lesionqc._appendAudit = deny
        lesionqc._stat = lambda path: ("key",)
        lesionqc.dimensionColumns = lambda info, figure=False: {"dimensionality": "3D"}
        self.addCleanup(lambda: (setattr(lesionqc, "_session", saved[0]), setattr(lesionqc, "_appendAudit", saved[1]), setattr(lesionqc, "_stat", saved[2]), setattr(lesionqc, "dimensionColumns", saved[3])))
        result = {"studyPath": "/study", "found": [("sub-01", ("key",), {"dimensionality": "3D"})]}
        self.assertTrue(lesionqc.QcCore._applyDimensions(fake, result))
        self.assertEqual(asked, [session])

    def test_12b_dimensionality_of_many_lesions_sends_one_notice(self):
        """BB-1: the dimensionality that lesion QC found for the lesions of a study is written with ONE study notice.

        Before, each lesion was written with its own ``setValues`` and each notice refreshed the whole Library on the main
        thread: a 300-subject study froze the GUI for 2-10 s after it opened (in the grid, while the thumbnails were made).
        """
        from StudioLib import lesionqc, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        sids = [f"sub-{index:02d}" for index in range(1, 7)]
        study = self._study([(sid, {}, False) for sid in sids])
        session = study_ui.StudySession(study)
        self.addCleanup(session.close)
        notices = []

        def listener(studySession):
            notices.append(studySession)

        study_ui.addListener(listener)
        self.addCleanup(study_ui.removeListener, listener)
        saved = (lesionqc._session, lesionqc._stat)
        self.addCleanup(lambda: (setattr(lesionqc, "_session", saved[0]), setattr(lesionqc, "_stat", saved[1])))
        lesionqc._session = lambda: session
        lesionqc._stat = lambda path: ("key",)
        fake = types.SimpleNamespace(studyPath=study.path, _askImported=lambda s: None)
        info = {"dimensionality": "3D", "planes": [], "n_slices": {}}
        result = {"studyPath": study.path, "found": [(sid, ("key",), info) for sid in sids]}
        self.assertTrue(lesionqc.QcCore._applyDimensions(fake, result))
        self.assertEqual({sid: study.subject(sid).get("dimensionality") for sid in sids}, dict.fromkeys(sids, "3D"))
        self.assertEqual(len(notices), 1, "one notice (one Library refresh) for all the lesions, not one per lesion")
        # Written to participants.tsv (the flush after the values)
        from LesionNetworkMappingLib.study import Study

        reopened = Study.open(study.path)
        self.assertEqual({sid: reopened.subject(sid).get("dimensionality") for sid in sids}, dict.fromkeys(sids, "3D"))

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
            ("participant_id\tage\nsub-P01\t40\nsub-p01\t41\n", "only in letter case"),  # one lesion file on macOS
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
            self.assertIn("This set: 3 lesions", str(cohortCard.contextLabel.text))
            cohortCard.matchLabel.setText("Subset volume_matched: 3 cases + 2 controls")  # as made by createSubset before blinding

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
            self.assertEqual(str(cohortCard.matchLabel.text), "", "a subset line made before blinding shows the split")
            context = str(cohortCard.contextLabel.text)
            self.assertIn("This set: 5 lesions", context, "the set size line shows the number of cases while tracing blind")
            self.assertNotIn("This set: 3 lesions", context)
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
            self.assertIn("This set: 3 lesions", str(cohortCard.contextLabel.text))
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

    def test_21b_unwritable_folders_are_named_in_plain_words(self):
        """A control set or a share export into a folder that cannot be written says "The folder ... cannot be written. Choose
        another folder." (no exception class, errno or temporary name) and the share checklist flags such an output before
        Export is enabled. The tests run as root, so a folder without permission is simulated (BB-1).
        """
        import errno
        import types
        from unittest import mock

        from StudioLib import libcard_cohort, libcard_controls, libcard_share

        error = PermissionError(errno.EACCES, "Permission denied", os.path.join(self.directory, "out", "share-out.partial-12-abc"))
        message = libcard_cohort.unwritableMessage(error)
        self.assertIn(os.path.join(self.directory, "out"), message)
        self.assertIn("cannot be written", message)
        for noise in ("PermissionError", "Errno", "partial"):
            self.assertNotIn(noise, message)
        self.assertIsNone(libcard_cohort.unwritableMessage(FileNotFoundError(errno.ENOENT, "No such file", "x")))
        self.assertIsNone(libcard_cohort.unwritableMessage(ValueError("x")))

        folder = os.path.join(self.directory, "readonly-parent")
        os.makedirs(folder)
        fake = types.SimpleNamespace(zipCheck=types.SimpleNamespace(checked=False))
        unwritable = libcard_share.ShareDialog.unwritableFolder
        self.assertIsNone(unwritable(fake, os.path.join(folder, "new")))
        with mock.patch.object(os, "access", lambda path, mode: os.path.abspath(path) != folder):
            self.assertEqual(unwritable(fake, os.path.join(folder, "new")), folder)
            self.assertEqual(unwritable(fake, os.path.join(folder, "deeper", "new")), folder)
            fake.zipCheck.checked = True
            self.assertEqual(unwritable(fake, os.path.join(folder, "new.zip")), folder)

        # The registration dialog wraps its message instead of cutting it off
        if slicer.util.mainWindow() is not None:
            dialog = libcard_controls.AddControlSetDialog()
            try:
                self.assertTrue(dialog.progressLabel.wordWrap)
            finally:
                dialog.dialog.close()

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

    def test_22_study_card_save_line_follows_the_save(self):
        """The STUDY card says "Saving…" while the 400 ms save of participants.tsv is pending and "All changes saved" once it has
        run (the card was not refreshed when the save finished and kept "Saving…" for good) (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import library, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Save line study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        cards = None
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            cards = library.StudyCards()
            cards.widget.show()
            cards.refresh()
            label = cards.studyCard.saveLabel
            session.flush()
            cards.refresh()
            self.assertTrue(str(label.text).startswith("All changes saved"), label.text)
            session.study.set("sub-01", {"qc": "pass"})
            session.scheduleSave()
            cards.refresh()
            self.assertTrue(str(label.text).startswith("Saving"), label.text)
            deadline = time.time() + 10
            while time.time() < deadline and session._saveTimer.isActive():
                slicer.app.processEvents()
            for _ in range(4):
                slicer.app.processEvents()
            self.assertFalse(session._saveTimer.isActive())
            self.assertEqual(session.saveStatus()[0], "All changes saved")
            self.assertTrue(str(label.text).startswith("All changes saved"), label.text)
        finally:
            if cards is not None:
                cards.dispose()
                cards.widget.hide()
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_23_map_cards_keep_inspector_width(self):
        """Opening a Map card (Circuits, Compare, any other) neither widens the inspector nor leaves it wide (BB-1).

        The Circuits card (a combo of circuits next to two buttons) and the Compare card (a check box next to two menu
        buttons, the three result buttons) had a layout minimum of 525 and 476 px, and the widest minimum a workspace
        module needed stays for good (``Shell._workspacePanelWidths``): the inspector went from 340 px to 535 px at the
        first expansion and stayed there after the card was collapsed and in every workspace.
        """
        from StudioLib import shell

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

            studioShell.switchTo("map")
            pump()
            widget = slicer.modules.LesionNetworkMappingWidget
            cards = {cardId: card for cardId, (_spec, card) in widget._cardWidgets.items() if card is not None and card.inherits("ctkCollapsibleButton")}
            for cardId in ("circuits", "compare"):
                self.assertIn(cardId, cards)
            for card in cards.values():
                card.collapsed = True
            pump()
            collapsedWidth = dock.width
            self.assertLessEqual(collapsedWidth, studioShell.panelWidth + 2)
            # Long circuit names (the combo's size hint is its longest item) and the results of the Compare card
            circuits = cards["circuits"].findChild("QComboBox", "LNMCircuitsCircuitCombo")
            self.assertIsNotNone(circuits)
            circuits.addItem("circuit-a-very-long-name-of-a-circuit-made-from-a-cohort_conn-A-very-long-label")
            compare = cards["compare"].findChild("QWidget", "LNMCompareResults")
            self.assertIsNotNone(compare)
            compare.show()
            for cardId, card in cards.items():
                card.collapsed = False
                pump()
                self.assertEqual(dock.width, collapsedWidth, f"the {cardId} card widens the inspector")
                card.collapsed = True
                pump()
                self.assertEqual(dock.width, collapsedWidth, f"the inspector stays wide after the {cardId} card is collapsed")
            for workspace in ("draw", "library", "map"):
                studioShell.switchTo(workspace)
                pump()
                self.assertEqual(dock.width, collapsedWidth, f"the inspector is {dock.width} px in {workspace}")
            wide = {name: width for name, width in studioShell._workspacePanelWidths.items() if width > studioShell.panelWidth + 2}
            self.assertEqual(wide, {}, "a workspace module asks for more than the stored width")
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

    def test_24_map_seed_modes_keep_inspector_width(self):
        """Choosing a Map seed mode neither widens the inspector nor leaves it wide, at 100 % and 125 % font (BB-1).

        The check box "Flip the sign (TMS: anticorrelation predicts response)" of the Stimulation seed (a check box does not wrap
        its text), the Import / Points / Add points row of the Coordinates seed, and at a 125 % font the form rows and buttons
        of the other Map cards had a layout minimum wider than the inspector, and the widest minimum a workspace module needed
        stays for good (``Shell._workspacePanelWidths``): the inspector went from 340 px to 415 px in Stimulation mode and stayed
        there back in Lesion mode, in Draw and in Library.
        """
        from StudioLib import shell, ui

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        settings = qt.QSettings()
        keys = [f"{shell._WORKSPACE_PANEL_WIDTHS_GROUP}/{name}" for name in ("Studio", "SegmentEditor", "LesionNetworkMapping")]
        saved = {key: settings.value(key) for key in keys}
        savedGeometry = mainWindow.saveGeometry()
        savedFont = qt.QFont(qt.QApplication.font())
        studioShell = None

        def pump(count=10):
            for _ in range(count):
                slicer.app.processEvents()

        try:
            mainWindow.resize(1366, 768)
            for fontPx in (None, 17):
                if studioShell is not None:
                    studioShell.dispose()
                    studioShell = None
                for key in keys:
                    settings.remove(key)
                font = qt.QFont(savedFont)
                if fontPx is not None:
                    font.setPixelSize(fontPx)
                qt.QApplication.setFont(font)
                pump()
                label = f"at {ui.applicationFontPx():g} px font"
                studioShell = shell.Shell(force=True)
                studioShell.apply()
                dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
                studioShell.switchTo("draw")
                pump()
                drawWidth = dock.width
                studioShell.switchTo("map")
                pump()
                widget = slicer.modules.LesionNetworkMappingWidget
                self.assertEqual(dock.width, drawWidth, f"the Map lesion seed widens the inspector {label}")
                # Cohort mode needs a study with tabs of its own and is left out
                for index, mode in enumerate(widget.seedModes):
                    if mode == "cohort":
                        continue
                    widget.seedModeGroup.button(index).click()
                    pump()
                    self.assertEqual(dock.width, drawWidth, f"the {mode} seed mode widens the inspector {label}")
                widget.seedModeGroup.button(0).click()
                pump()
                self.assertEqual(dock.width, drawWidth, f"the inspector stays wide back in Lesion mode {label}")
                for workspace in ("draw", "library", "map"):
                    studioShell.switchTo(workspace)
                    pump()
                    self.assertEqual(dock.width, drawWidth, f"the inspector is {dock.width} px in {workspace} {label}")
        finally:
            if studioShell is not None:
                studioShell.dispose()
            qt.QApplication.setFont(savedFont)
            pump()
            mainWindow.restoreGeometry(savedGeometry)
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            slicer.mrmlScene.Clear(0)

    def test_24_map_cards_keep_inspector_width_at_125_percent_font(self):
        """Opening a Map card neither widens the inspector nor leaves it wide at a 125 % font (BB-1).

        The Threshold spin box and the sign choice of the Clusters and Networks source bar (the Circuits and Compare rows
        of controls and the Networks region buttons too) need about 350 px at a 17 px font, and the widest minimum a workspace
        module needed stays for good (``Shell._workspacePanelWidths``): the inspector went from 340 px to 390 px when the
        Clusters card opened, and stayed there in the other cards, in Draw and back in Map. The rows stack when they are
        narrower than their controls need side by side (``ui.wrapLayout()``).
        """
        from StudioLib import shell, ui

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        settings = qt.QSettings()
        keys = [f"{shell._WORKSPACE_PANEL_WIDTHS_GROUP}/{name}" for name in ("Studio", "SegmentEditor", "LesionNetworkMapping")]
        saved = {key: settings.value(key) for key in keys}
        savedGeometry = mainWindow.saveGeometry()
        savedFont = qt.QFont(qt.QApplication.font())
        studioShell = None

        def pump(count=10):
            for _ in range(count):
                slicer.app.processEvents()

        try:
            mainWindow.resize(1366, 768)
            for fontPx in (None, 17):
                if studioShell is not None:
                    studioShell.dispose()
                    studioShell = None
                for key in keys:
                    settings.remove(key)
                font = qt.QFont(savedFont)
                if fontPx is not None:
                    font.setPixelSize(fontPx)
                qt.QApplication.setFont(font)
                pump()
                label = f"at {ui.applicationFontPx():g} px font"
                studioShell = shell.Shell(force=True)
                studioShell.apply()
                dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
                studioShell.switchTo("map")
                pump()
                widget = slicer.modules.LesionNetworkMappingWidget
                cards = {cardId: card for cardId, (_spec, card) in widget._cardWidgets.items() if card is not None and card.inherits("ctkCollapsibleButton")}
                for cardId in ("clusters", "networks", "circuits", "compare"):
                    self.assertIn(cardId, cards)
                for card in cards.values():
                    card.collapsed = True
                pump()
                collapsedWidth = dock.width
                self.assertLessEqual(collapsedWidth, studioShell.panelWidth + 2, label)
                for cardId, card in cards.items():
                    card.collapsed = False
                    pump()
                    self.assertEqual(dock.width, collapsedWidth, f"the {cardId} card widens the inspector {label}")
                    card.collapsed = True
                    pump()
                    self.assertEqual(dock.width, collapsedWidth, f"the inspector stays wide after the {cardId} card is collapsed {label}")
                for workspace in ("draw", "library", "map"):
                    studioShell.switchTo(workspace)
                    pump()
                    self.assertEqual(dock.width, collapsedWidth, f"the inspector is {dock.width} px in {workspace} {label}")
                wide = {name: width for name, width in studioShell._workspacePanelWidths.items() if width > studioShell.panelWidth + 2}
                self.assertEqual(wide, {}, f"a workspace module asks for more than the stored width {label}")
        finally:
            if studioShell is not None:
                studioShell.dispose()
            qt.QApplication.setFont(savedFont)
            pump()
            mainWindow.restoreGeometry(savedGeometry)
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
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

    def test_24_map_calibrate_card_keeps_inspector_width(self):
        """Opening the Map Calibrate card neither widens the inspector nor leaves it wide at 1366 x 768 (BB-1).

        The check box "Also the sensitivity route (relocated lesions; slower)" was a plain QCheckBox, which draws its text on
        one line: its minimum width (367 px) made the card's minimum 506 px, and the widest minimum a workspace module needed
        stays for good (``Shell._workspacePanelWidths``, stored in ``Studio/WorkspacePanelWidths``): the inspector went from
        340 px to 516 px and stayed there after a restart. The option is now a word-wrapped label (``ui.wrapCheckBox()``).
        """
        from StudioLib import shell

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        settings = qt.QSettings()
        keys = [f"{shell._WORKSPACE_PANEL_WIDTHS_GROUP}/{name}" for name in ("Studio", "SegmentEditor", "LesionNetworkMapping")]
        saved = {key: settings.value(key) for key in keys}
        savedGeometry = mainWindow.saveGeometry()
        studioShell = None

        def pump(count=10):
            for _ in range(count):
                slicer.app.processEvents()

        try:
            mainWindow.resize(1366, 768)
            for key in keys:
                settings.remove(key)
            studioShell = shell.Shell(force=True)
            studioShell.apply()
            dock = mainWindow.findChild("QDockWidget", "PanelDockWidget")
            studioShell.switchTo("map")
            pump()
            widget = slicer.modules.LesionNetworkMappingWidget
            card = widget.ctx.card("calibrate")
            self.assertIsNotNone(card)
            checkBox = card.widget.findChild("QCheckBox", "LNMCalibrateSensitivityCheckBox")
            self.assertIsNotNone(checkBox)
            self.assertLess(checkBox.minimumSizeHint.width(), 60, "the sensitivity option's text is drawn by the check box on one line")
            before = dock.width
            self.assertLessEqual(before, studioShell.panelWidth + 2)
            card.widget.collapsed = False
            pump()
            self.assertLessEqual(card.widget.minimumSizeHint.width(), studioShell.panelWidth, "the Calibrate card needs more than the inspector width")
            self.assertEqual(dock.width, before, "the Calibrate card widens the inspector")
            card.widget.collapsed = True
            pump()
            self.assertEqual(dock.width, before, "the inspector stays wide after the Calibrate card is collapsed")
            wide = {name: width for name, width in studioShell._workspacePanelWidths.items() if width > studioShell.panelWidth + 2}
            self.assertEqual(wide, {}, "a workspace module asks for more than the stored width")
        finally:
            if studioShell is not None:
                studioShell.dispose()
            pump()
            mainWindow.restoreGeometry(savedGeometry)
            for key, value in saved.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            slicer.mrmlScene.Clear(0)

    def test_24_lesion_metrics_follow_a_result_that_repoints_the_lesion(self):
        """A merged result that repoints the lesion (a normalization) recomputes the row's metrics from the new file (BB-1).

        The centroid, outside-brain share and size of the native lesion stayed on a row that already read MNI until the
        study was opened again: ``StudySession.computeMissingMetrics`` ran only on open and on import.
        """
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import study_ui

        study = self._study([("sub-01", {}, False)])
        folder = study.path
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        self.assertTrue(session.waitForMetrics(60))
        slicer.app.processEvents()
        before = dict(session.study.subject("sub-01"))
        self.assertFalse(study_ui._missing(before.get("centroid_x")), before)
        # The result of a normalization: a lesion on another grid, at another place and of another size
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
        affine[:3, 3] = [-40.0, -60.0, -10.0]
        mask = np.zeros((60, 70, 40), np.uint8)
        mask[30:40, 35:45, 20:30] = 1
        relative = "derivatives/normalization/sub-01/sub-01_space-MNI152NLin2009cAsym_desc-lesion_mask.nii.gz"
        os.makedirs(os.path.dirname(os.path.join(folder, relative)))
        nifti.write_nifti(os.path.join(folder, relative), mask, affine)
        Study.post_result(folder, "sub-01", {"lesion": relative, "space": "MNI152NLin2009cAsym"}, source="normalize")
        self.assertEqual(len(session.applyInbox()), 1)
        self.assertTrue(session.waitForMetrics(60))
        slicer.app.processEvents()
        row = session.study.subject("sub-01")
        self.assertEqual(row["lesion"], relative)
        self.assertEqual(row["lesion_size"], str(os.stat(os.path.join(folder, relative)).st_size))
        self.assertNotEqual(row["lesion_size"], before["lesion_size"])
        self.assertNotEqual(row["centroid_x"], before["centroid_x"])
        self.assertNotEqual(row["lesion_mtime"], before["lesion_mtime"])
        self.assertAlmostEqual(float(row["volume_ml"]), 1.0, places=2)

    def test_25_cleared_description_of_a_registered_column_is_what_the_dialog_shows(self):
        """Columns...: clearing the description of a registered column (age) removed it from participants.json, but
        ``column_spec`` fell back to the registry's, so the reopened dialog showed "Age" again (BB-1). The file now holds
        what the dialog shows: the registry's description is written back, and a description of one's own stays.
        """
        from StudioLib import libcard_columns as lc
        from StudioLib import study_ui

        study = self._study([("sub-01", {"age": "60"}, False), ("sub-02", {"age": "70"}, False)])
        folder = study.path
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        slicer.app.processEvents()

        def shown(name):
            dialog = lc.ColumnsDialog(session)
            self.addCleanup(dialog.widget.deleteLater)
            return dialog, dialog.table.item(dialog.rowOf(name), lc.COL_DESCRIPTION).text()

        def saved():
            with open(os.path.join(folder, "participants.json"), encoding="utf-8") as f:
                return json.load(f)["age"].get("Description")

        registry = session.study.column_spec("age").description
        self.assertTrue(registry)
        dialog, _text = shown("age")
        dialog.setText("age", lc.COL_DESCRIPTION, "")
        result = dialog.confirm()
        self.assertEqual(result["applied"], ["age"], result)
        self.assertEqual(saved(), registry, "the file holds the description that the dialog shows")
        self.assertEqual(shown("age")[1], registry)
        # An own text is kept, in the file and in the dialog; a second clear goes back to the registry's
        dialog, _text = shown("age")
        dialog.setText("age", lc.COL_DESCRIPTION, "Age at stroke")
        dialog.confirm()
        self.assertEqual((saved(), shown("age")[1]), ("Age at stroke", "Age at stroke"))
        dialog, _text = shown("age")
        dialog.setText("age", lc.COL_DESCRIPTION, "")
        dialog.confirm()
        self.assertEqual((saved(), shown("age")[1]), (registry, registry))

    def test_26_study_card_folder_link_is_elided_to_its_pixel_width(self):
        """The folder link of the STUDY card shows the path elided to the pixel width of the link, the study folder name kept, and
        elides it again when the card is resized (it was shortened by characters and cut at the card edge without an ellipsis) (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import library, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Folder link study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 3):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        cards = None
        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            cards = library.StudyCards()
            cards.widget.resize(278, 400)
            cards.widget.show()
            link = cards.studyCard.folderLink
            metrics = qt.QFontMetrics(link.font)
            realPath = session.study.path
            for path in (
                "/Users/azhmeer/Documents/Slicer Studio/Studies/Stroke-LNM-cohort-2024",
                "/tmp/claude-0/bb1-study-work/r2s6l1366/ado",
                "/home/u/Stroke-LNM-cohort-2024",
                realPath,
            ):
                session.study.path = path
                cards.refresh()
                slicer.app.processEvents()
                text = str(link.text)
                self.assertLessEqual(metrics.horizontalAdvance(text), link.width, (path, text, link.width))
                self.assertTrue(text.endswith(path.rsplit("/", 1)[-1]), (path, text))
                self.assertIn(path, str(link.toolTip))
                if metrics.horizontalAdvance(path) > link.width:
                    self.assertIn("…", text)
                else:
                    self.assertEqual(text, path)
            session.study.path = "/Users/azhmeer/Documents/Slicer Studio/Studies/Stroke-LNM-cohort-2024"
            cards.refresh()
            slicer.app.processEvents()
            before = str(link.text)
            cards.widget.resize(520, 400)
            for _ in range(4):
                slicer.app.processEvents()
            after = str(link.text)
            self.assertGreater(len(after), len(before), (before, after))
            self.assertLessEqual(metrics.horizontalAdvance(after), link.width)
            session.study.path = realPath
        finally:
            if cards is not None:
                cards.widget.hide()
                cards.widget.deleteLater()
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_28_reviewed_lesion_edited_and_left_is_reviewed_again(self):
        """An approved (or rejected) lesion edited and then left without Done (Alt+arrows, opening another subject, the close
        prompt) kept its review and passed the review gate: an approval is for the voxels that were reviewed. Such a write
        now reopens the review (→ pending, audited ``via`` "leave", in the sidecar too); a write with the same voxels keeps
        it, and a rejected lesion stays excluded (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti
        from LesionNetworkMappingLib import review as rules

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Review study")
        os.makedirs(folder)
        scans = []
        for sid in ("sub-01", "sub-02", "sub-03"):
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def paint(box):
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()

        def reviewed(sid, to, reason=None):
            """Trace ``sid`` (Done) and record the reviewer's decision ``to``."""
            session.open(sid, switchToDraw=False)
            paint(np.s_[20:23, 30:33, 20:23])
            self.assertIsNotNone(session.done())
            pump()
            if rules.status_of(session.study.subject(sid)) == "none":
                rules.apply_transition(session.study, sid, "pending", "Tracer")
            rules.apply_transition(session.study, sid, to, "Rita Reviewer", reason=reason)
            session.flush()

        def gate():
            return [row["participant_id"] for row in session.study.analysis_rows(require_review=True)[0]]

        def sidecarReview(sid):
            with open(session.study.lesion_sidecar_path(sid), encoding="utf-8") as f:
                return json.load(f).get("SlicerStudio", {}).get("review", {}).get("status")

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.study.review_required = True
            session.flush()
            reviewed("sub-01", "approved")
            reviewed("sub-02", "approved")
            reviewed("sub-03", "rejected", reason="image quality")
            self.assertEqual(gate(), ["sub-01", "sub-02"])

            # Reopened and rewritten with the same voxels: the approval stands
            session.open("sub-02", switchToDraw=False)
            session.markDirty()
            self.assertTrue(session.writeCurrent())
            self.assertEqual(session.study.subject("sub-02")["review_status"], "approved")

            # Edited and left by opening another subject: pending again, audited, out of the gate, a later Done keeps it
            session.open("sub-01", switchToDraw=False)
            paint(np.s_[30:33, 40:43, 30:33])
            session.open("sub-02", switchToDraw=False)
            row = session.study.subject("sub-01")
            self.assertEqual(row["review_status"], "pending", "an approved lesion changed after the approval is still approved")
            self.assertEqual(gate(), ["sub-02"])
            last = rules.history(session.study, "sub-01")[-1]
            self.assertEqual((last["from"], last["to"], last["via"]), ("approved", "pending", "leave"))
            self.assertEqual(sidecarReview("sub-01"), "pending")
            session.open("sub-01", switchToDraw=False)
            self.assertIsNotNone(session.done())
            pump()
            self.assertEqual(session.study.subject("sub-01")["review_status"], "pending")

            # Edited and written by the close prompt (writeCurrent)
            session.open("sub-02", switchToDraw=False)
            paint(np.s_[30:33, 40:43, 30:33])
            self.assertTrue(session.writeCurrent())
            self.assertEqual(session.study.subject("sub-02")["review_status"], "pending")
            self.assertEqual(gate(), [])

            # A rejected lesion edited and left: pending, and still excluded until a reviewer approves it
            session.open("sub-03", switchToDraw=False)
            paint(np.s_[30:33, 40:43, 30:33])
            self.assertEqual(session.previous(), "sub-02")
            row = session.study.subject("sub-03")
            self.assertEqual(row["review_status"], "pending")
            self.assertEqual(str(row["include"]), "0")
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_27_sources_undo_reverts_the_change_just_made(self):
        """Sources card: P and X move the selection to the next record, and U then undid that record's earlier decision (and
        deleted it from the search log) instead of the change just made, without a word. U now undoes the change just made,
        selects its record and toasts it; after the user selects another row, U acts on that row (BB-1).
        """
        from LesionNetworkMappingLib.study import Study

        from StudioLib import libcard_sources, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        nbib = "\n\n".join(
            f"PMID- {pmid}\nTI  - {title}\nAU  - {author}\nDP  - {year}\nTA  - Brain"
            for pmid, title, author, year in (
                ("111111", "Lesion network mapping of a rare symptom after a pontine infarct.", "Smith J", "2020"),
                ("222222", "A second paper about thalamic hallucinations.", "Doe A", "2019"),
                ("333333", "Akinetic mutism after bilateral cingulate stroke: a case report.", "Ng P", "2018"),
            )
        )
        path = os.path.join(self.directory, "Lit")
        Study.create(path, name="Lit").save()
        toasts = []
        toast = libcard_sources._toast
        libcard_sources._toast = lambda text, *args, **kwargs: toasts.append(str(text))
        card = None
        try:
            session = study_ui.openStudy(path, showLibrary=False)
            self.assertIsNotNone(session)
            card = libcard_sources.SourcesCard(session)
            card.widget.show()
            card.importRecords(text=nbib, database="PubMed", query="q")
            self.assertEqual(card._rows, ["smith2020", "doe2019", "ng2018"])

            def stages():
                return {row["record_id"]: row["stage"] for row in card.load().rows}

            # P on doe2019 (an earlier decision), then P on smith2020: the selection moves on to doe2019
            card.table.selectRow(1)
            self.assertEqual(card.advanceSelected(), "screened")
            self.assertEqual(card.selectedId(), "ng2018")
            card.table.selectRow(0)
            self.assertEqual(card.advanceSelected(), "screened")
            self.assertEqual(card.selectedId(), "doe2019")
            self.assertEqual(card.undoSelected(), "identified")
            self.assertEqual(stages(), {"smith2020": "identified", "doe2019": "screened", "ng2018": "identified"})
            self.assertEqual(card.selectedId(), "smith2020")
            self.assertEqual([entry["record_id"] for entry in card.load().log], ["doe2019"])
            self.assertEqual(toasts[-1], "Undo · smith2020 back to Identified")

            # X on doe2019 moves the selection to ng2018; U restores doe2019, not ng2018
            card.table.selectRow(2)
            card.advanceSelected()  # ng2018 screened (the last row: the selection stays)
            card.table.selectRow(1)
            self.assertTrue(card.exclude("doe2019", "Not a case report"))
            self.assertEqual(card.selectedId(), "ng2018")
            self.assertEqual(card.undoSelected(), "screened")
            self.assertEqual(stages(), {"smith2020": "identified", "doe2019": "screened", "ng2018": "screened"})
            self.assertEqual(card.selectedId(), "doe2019")

            # A row selected by the user after P: U undoes that row's last change
            card.table.selectRow(0)
            card.advanceSelected()  # smith2020 screened, selection on doe2019
            card.table.selectRow(2)
            self.assertEqual(card.undoSelected(), "identified")
            self.assertEqual(stages(), {"smith2020": "screened", "doe2019": "screened", "ng2018": "identified"})
            self.assertEqual(toasts[-1], "Undo · ng2018 back to Identified")
        finally:
            libcard_sources._toast = toast
            if card is not None:
                card.widget.hide()
                card.widget.deleteLater()
            study_ui.closeStudy()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_27b_sources_fetch_metadata_runs_in_the_background_and_can_be_cancelled(self):
        """Sources > Fetch metadata made its request on the main thread: on a network that does not answer the whole application
        froze for up to 10 s with no Cancel. The request now runs in a helper thread: the click returns at once, the event loop
        keeps running, the button becomes "Cancel lookup", and the result is applied on the main thread when it arrives; a
        cancelled lookup ignores its late answer (BB-1).
        """
        import threading

        from LesionNetworkMappingLib.study import Study

        from StudioLib import libcard_sources, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        crossref = {
            "message": {
                "title": ["A paper"],
                "author": [{"family": "Smith", "given": "J"}],
                "issued": {"date-parts": [[2020]]},
                "container-title": ["Brain"],
                "DOI": "10.1000/xyz",
            },
        }
        release = threading.Event()
        calls, threads = [], []

        def waiting(url, params):
            calls.append(url)
            threads.append(threading.current_thread())
            release.wait(20)
            return crossref

        def failing(url, params):
            raise ConnectionError("no network")

        def wait(condition, seconds=5.0):
            end = time.time() + seconds
            while not condition() and time.time() < end:
                slicer.app.processEvents()
                time.sleep(0.01)
            return condition()

        path = os.path.join(self.directory, "LitFetch")
        Study.create(path, name="LitFetch").save()
        toasts = []
        toast = libcard_sources._toast
        libcard_sources._toast = lambda text, *args, **kwargs: toasts.append(str(text))
        card = None
        try:
            session = study_ui.openStudy(path, showLibrary=False)
            self.assertIsNotNone(session)
            card = libcard_sources.SourcesCard(session)
            card.widget.show()
            card.importRecords(text="10.1000/xyz", database="Manual", query="q")
            self.assertEqual(len(card._rows), 1)
            card.table.selectRow(0)
            self.assertTrue(card.fetchButton.isEnabled())
            self.assertEqual(card.fetchButton.text, "Fetch metadata")

            def title():
                return card.load().rows[0]["title"]

            self.assertEqual(title(), "")
            # A request that does not answer: the click returns at once and the event loop keeps running
            card._lookupGet = waiting
            started = time.time()
            card.fetchButton.click()
            self.assertLess(time.time() - started, 1.0, "the click must not wait for the request")
            self.assertTrue(card.lookupBusy())
            self.assertEqual(card.fetchButton.text, "Cancel lookup")
            self.assertTrue(card.fetchButton.isEnabled())
            self.assertTrue(wait(lambda: calls, 5), "the request did not start")
            self.assertNotEqual(threads[0], threading.main_thread(), "the request must not run on the main thread")
            wait(lambda: False, 0.3)  # the event loop runs while the request waits
            self.assertTrue(card.lookupBusy())
            self.assertEqual(title(), "", "nothing is filled before the answer arrives")
            # The answer arrives: it is applied on the main thread
            release.set()
            self.assertTrue(wait(lambda: not card.lookupBusy()))
            self.assertEqual(title(), "A paper")
            self.assertEqual(card.fetchButton.text, "Fetch metadata")
            self.assertTrue(toasts[-1].startswith("Filled"), toasts[-1])

            # Cancel: the late answer is ignored
            card.load().rows[0]["title"] = ""
            card._save()
            release.clear()
            del calls[:]
            card.fetchButton.click()
            self.assertTrue(wait(lambda: calls, 5))
            self.assertTrue(card.lookupBusy())
            card.fetchButton.click()
            self.assertFalse(card.lookupBusy())
            self.assertEqual(card.fetchButton.text, "Fetch metadata")
            self.assertEqual(toasts[-1], "Lookup cancelled")
            release.set()
            wait(lambda: False, 0.4)
            self.assertEqual(title(), "", "a cancelled lookup must not fill the record")

            # A failure is said on the card
            card._lookupGet = failing
            card.fetchButton.click()
            self.assertTrue(wait(lambda: not card.lookupBusy()))
            self.assertTrue(card.errorLabel.isVisible())
            self.assertIn("lookup failed", card.errorLabel.text)
            self.assertEqual(title(), "")
        finally:
            release.set()
            libcard_sources._toast = toast
            if card is not None:
                card.widget.hide()
                card.widget.deleteLater()
            study_ui.closeStudy()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_30_missing_scan_or_lesion_is_said(self):
        """A subject whose scan file is missing opened silently on the study template (a lesion traced there was saved
        as the subject's), and a traced subject whose lesion file is missing opened as an empty lesion without a word.
        The scan is never replaced by the template, and both are said (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Missing study")
        os.makedirs(folder)
        scans = []
        for index in range(1, 4):
            scans.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append((str(text), kwargs))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            study = session.study
            # sub-01 is traced (its lesion is written), then sub-02 is open
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            for _ in range(4):
                slicer.app.processEvents()
            self.assertIsNotNone(session.done())
            self.assertEqual(session.current(), "sub-02")
            lesion01 = study.abspath(study.subject("sub-01")["lesion"])
            self.assertTrue(os.path.isfile(lesion01))
            # The scan of sub-03 and the lesion of sub-01 are moved away
            os.remove(scans[2])
            os.remove(lesion01)

            def missingToasts(sid):
                return [(text, kwargs) for text, kwargs in toasts if sid in text and "missing" in text]

            # sub-03 is not opened on the template: the user is told, and sub-02 stays open
            del toasts[:]
            self.assertFalse(session.open("sub-03", switchToDraw=False))
            self.assertEqual(session.current(), "sub-02")
            self.assertEqual(session.volumeNode().GetAttribute(study_ui.SUBJECT_ATTRIBUTE), "sub-02")
            said = missingToasts("sub-03")
            self.assertTrue(said, toasts)
            self.assertIn("sub-03_T1w.nii.gz", said[-1][0])
            self.assertNotIn(folder, said[-1][0], "the toast names the table path, not the local path")
            self.assertEqual(said[-1][1].get("level"), "consequence")
            self.assertEqual(said[-1][1].get("actionText"), "Show folder")
            # Done on sub-02 does not open sub-03 on the template either, and sub-03 is not traced
            del toasts[:]
            self.assertIsNotNone(session.done(writeLesion=False))
            self.assertEqual(session.current(), "sub-02")
            self.assertTrue(missingToasts("sub-03"), toasts)
            self.assertNotEqual(study.subject("sub-03").get("status"), "traced")
            self.assertTrue(study_ui._lnm("study").is_missing(study.subject("sub-03").get("lesion")))
            # The traced sub-01 opens on its scan with an empty lesion, and the missing lesion file is said
            del toasts[:]
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertEqual(session.current(), "sub-01")
            self.assertEqual(session.volumeNode().GetAttribute(study_ui.SUBJECT_ATTRIBUTE), "sub-01")
            said = missingToasts("sub-01")
            self.assertTrue(said, toasts)
            self.assertIn(os.path.basename(lesion01), said[-1][0])
            self.assertEqual(said[-1][1].get("level"), "consequence")
            # Leaving it unchanged does not repoint the row
            row01 = dict(study.subject("sub-01"))
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertEqual(study.subject("sub-01").get("lesion"), row01.get("lesion"))
            self.assertEqual(study.subject("sub-01").get("status"), "traced")
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_56_study_folder_renamed_while_open_is_said_and_not_recreated(self):
        """The study folder was renamed (or moved) in the file manager while the study is open: Done says that the study
        folder moved (not that the grid has no NIfTI file), and neither the journal, the heartbeat nor Done recreates a
        stray ``.studio`` folder at the old path (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Renamed study")
        renamed = os.path.join(self.directory, "Renamed study moved")
        os.makedirs(folder)
        scans = []
        for index in range(1, 3):
            scans.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertTrue(session.folderAvailable())
            os.rename(folder, renamed)
            self.assertFalse(session.folderAvailable())
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            # The journal and the heartbeat write nothing at the old path
            self.assertFalse(session._journal.writeNow())
            session._writeHeartbeat()
            self.assertFalse(os.path.exists(folder), "the journal or the heartbeat recreated the old study folder")
            said = [text for text in toasts if "moved" in text and "renamed" in text]
            self.assertEqual(len(said), 1, toasts)
            # Done says what happened, keeps the subject open and writes nothing at the old path
            del toasts[:]
            self.assertIsNone(session.done())
            self.assertEqual(session.current(), "sub-01")
            self.assertTrue([text for text in toasts if "moved" in text and "renamed" in text], toasts)
            self.assertFalse([text for text in toasts if "no NIfTI file" in text], toasts)
            self.assertFalse(os.path.exists(folder), "Done recreated the old study folder")
            self.assertFalse(os.path.exists(os.path.join(renamed, ".studio", "journal", "sub-01.nii.gz")))
            # The folder back at its place: everything writes again and the message can be said again
            os.rename(renamed, folder)
            self.assertTrue(session.folderAvailable())
            self.assertTrue(session._journal.writeNow())
            self.assertIsNotNone(session.done())
            self.assertEqual(session.study.subject("sub-01")["status"], "traced")
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_68_study_folder_moved_keeps_the_tracing_when_opened_from_its_new_place(self):
        """The study folder was moved while tracing and the user does what the message says (Open study… from its new
        place): every stroke still on screen is offered for recovery there, not only the journal made before the move,
        and closing the old session recreates nothing at the old path (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import autosave_io, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Moved study")
        moved = os.path.join(self.directory, "elsewhere", "Moved study new")
        os.makedirs(folder)
        os.makedirs(os.path.dirname(moved))
        scans = []
        for index in range(1, 3):
            scans.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        self.addCleanup(lambda: getattr(study_ui, "_kept", []).clear())
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def paint(session, box):
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            return int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode).sum())

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            before = paint(session, (slice(20, 23), slice(30, 33), slice(20, 23)))
            self.assertEqual(before, 27)
            self.assertTrue(session._journal.writeNow())
            autosave_io.backgroundWriter().drain(5.0)
            shutil.move(folder, moved)
            total = paint(session, (slice(40, 43), slice(30, 33), slice(20, 23)))
            self.assertEqual(total, 54)
            self.assertFalse(session._journal.writeNow())
            # Open study… from the new place: the old session closes while its folder is gone
            reopened = study_ui.openStudy(moved, studioShell, showLibrary=False)
            self.assertIsNot(reopened, session)
            self.assertFalse(os.path.exists(folder), "closing the old session recreated the old study folder")
            journalFile = os.path.join(moved, ".studio", "journal", "sub-01.nii.gz")
            self.assertTrue(os.path.isfile(journalFile))
            data, _affine, _header = nifti.read_nifti(journalFile, apply_scaling=False)
            self.assertEqual(int((data != 0).sum()), total, "the journal at the new place lacks the strokes drawn after the move")
            self.assertTrue([text for text in toasts if "offered for recovery" in text], toasts)
            self.assertEqual(getattr(study_ui, "_kept", []), [])
            # Opening the subject offers all of it; Recover puts every stroke back
            self.assertTrue(reopened.open("sub-01", switchToDraw=False))
            self.assertIsNotNone(reopened._journal.noticeWidget())
            self.assertTrue(reopened._journal.recover())
            segmentationNode, volumeNode = reopened.segmentationNode(), reopened.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode).sum()), total)
            # ...and it is written at the new place
            self.assertIsNotNone(reopened.done())
            self.assertEqual(reopened.study.subject("sub-01")["status"], "traced")
            self.assertTrue(os.listdir(os.path.join(moved, "lesions")))
            self.assertFalse(os.path.exists(folder))
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_69_undo_remove_of_the_open_subject_keeps_its_lesion(self):
        """Removing the subject being traced writes its lesion first; Undo puts back the row that names that lesion,
        not the row from before the write, and reopening shows the tracing (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Remove undo study")
        os.makedirs(folder)
        scans = []
        for index in range(1, 4):
            scans.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def voxels(session):
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            return int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode).sum())

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            for _ in range(4):
                slicer.app.processEvents()
            self.assertEqual(voxels(session), 27)
            # Delete in Library with the subject open (no Done), then Undo
            self.assertEqual(session.removeSubjects(["sub-02"]), 1)
            self.assertIsNone(session.current())
            self.assertNotIn("sub-02", session.study)
            lesions = os.listdir(os.path.join(folder, "lesions"))
            self.assertTrue([name for name in lesions if name.startswith("sub-02_") and name.endswith(".nii.gz")], lesions)
            self.assertEqual(session.undoRemove(), 1)
            self.assertEqual([row["participant_id"] for row in session.study.subjects], ["sub-01", "sub-02", "sub-03"])
            row = session.study.subject("sub-02")
            self.assertFalse(study_ui._missing(row.get("lesion")), "Undo put back the row from before the lesion was written")
            self.assertEqual(row.get("status"), "inprogress")
            self.assertTrue(os.path.isfile(os.path.join(folder, row["lesion"])))
            # Reopening shows the tracing
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertEqual(voxels(session), 27)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_29_second_tracing_survives_a_crash(self):
        """Slicer stopped during a second tracing: nothing of it was journaled, and the recovery card offered to reopen
        the session's earlier subject (an empty second tracing then). The second tracing now has its own journal, the
        autosave names it, and recovery reopens it with the journal offered (BB-1).
        """
        import types

        from LesionNetworkMappingLib import agreement, geometry, nifti
        from LesionNetworkMappingLib.study import Study

        from StudioLib import adjudicate, autosave_io, autosave_ui, journal, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Second study")
        os.makedirs(folder)
        scans = []
        for sid in ("sub-01", "sub-02"):
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: None
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump(seconds=0.0):
            deadline = time.time() + seconds
            while True:
                for _ in range(4):
                    slicer.app.processEvents()
                if time.time() >= deadline:
                    return
                time.sleep(0.02)

        def tracing(state):
            segmentationNode = state.segmentationNode()
            volumeNode = slicer.mrmlScene.GetNodeByID(state._nodeIds[0])
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            return segmentationNode, segmentId, volumeNode

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            review.setRaterName("Alice")
            session.open("sub-01", switchToDraw=False)
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()
            self.assertIsNotNone(session.done())
            pump()
            agreement.assign(session.study, sids=["sub-01"], rater="Bob", who="Alice")
            session.flush()
            review.setRaterName("Bob")
            state = adjudicate.startSecondTracing(sid="sub-01", shell=studioShell)
            self.assertIsNotNone(state)
            pump()

            # The autosave names the second tracing, not a subject of the session (which would show the first tracing)
            context = study_ui._autosaveContext()["study"]
            self.assertIsNone(context["subject"])
            self.assertEqual(context.get("agreement"), {"queue": adjudicate.QUEUE_SECOND, "subject": "sub-01"})
            self.assertEqual(autosave_ui._agreementText(context), "Reopen second tracing of sub-01")

            # Bob traces; 2 s later the journal is written
            segmentationNode, segmentId, volumeNode = tracing(state)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[10:20, 10:20, 10:20] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            painted = int(array.sum())
            pump(journal.JOURNAL_DELAY_MS / 1000.0 + 0.5)
            autosave_io.backgroundWriter().drain(5.0)
            pump()
            journalPath = os.path.join(folder, ".studio", "journal", "rater-Bob", "sub-01.nii.gz")
            self.assertTrue(os.path.isfile(journalPath), "the second tracing was not journaled")
            self.assertFalse(os.path.exists(os.path.join(folder, ".studio", "journal", "sub-01.nii.gz")),
                             "the second tracing must never be offered as the first tracing")

            # "Crash": the state ends without saving and the study closes (nothing of the second tracing is written)
            state.exit(quiet=True, keepScene=True)
            study_ui.closeStudy()
            pump()
            self.assertTrue(os.path.isfile(journalPath))
            self.assertTrue(adjudicate.secondTodo(Study.open(folder).subject("sub-01")))

            # Recovering the autosave reopens the second tracing, whose notice offers the journal
            study_ui._recover(types.SimpleNamespace(study=context))
            pump()
            state = adjudicate.state()
            self.assertIsNotNone(state, "recovery did not reopen the second tracing")
            self.assertEqual((type(state).__name__, state.current), ("SecondTracingState", "sub-01"))
            self.assertIsNone(study_ui.session().current(), "the first tracing was opened for the second rater")
            segmentationNode, segmentId, volumeNode = tracing(state)
            self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode).sum()), 0)
            offer = state.journal().noticeWidget()
            self.assertIsNotNone(offer, "the journal of the second tracing was not offered")
            self.assertIs(studioShell.inspectorNotice(), state.notice, "the second tracing's own notice must stay")
            self.assertTrue(state.notice.isAncestorOf(offer))
            recoverButtons = [button for button in offer.findChildren("QToolButton") if str(button.text) == "Recover"]
            self.assertEqual(len(recoverButtons), 1)
            recoverButtons[0].click()
            pump()
            self.assertEqual(int(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode).sum()), painted)
            self.assertIsNone(state.journal().noticeWidget())
            self.assertTrue(state.isDirty())

            # Saving the second tracing removes its journal
            self.assertTrue(state.saveNext())
            pump()
            self.assertFalse(os.path.exists(journalPath))
            row = study_ui.session().study.subject("sub-01")
            self.assertTrue(row.get("rater2_lesion"))
        finally:
            if adjudicate.state() is not None:
                adjudicate.state().exit(quiet=True, keepScene=True)
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_31_symptom_column_is_not_made_the_groups(self):
        """New study… and the participants table's "Create study" set case / control from any yes / no column (a symptom
        such as "aphasia") without showing the mapping; the folder card showed it for confirmation (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti
        from LesionNetworkMappingLib.study import read_tsv

        from StudioLib import shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid

        def makeFolder(name, column, values):
            folder = os.path.join(self.directory, name)
            os.makedirs(folder)
            lines = ["participant_id\tage\t" + column + "\tNIHSS"]
            for index, value in enumerate(values, start=1):
                nifti.write_nifti(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"), np.zeros(grid.shape, np.uint8), grid.affine)
                lines.append(f"sub-0{index}\t{50 + index}\t{value}\t{index * 3}")
            with open(os.path.join(folder, "participants.tsv"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return folder

        # The proposal: flag columns not named like a group are never the groups; group columns still are
        proposal = study_ui.groupMappingProposal
        for column, values in (("aphasia", ("yes", "no")), ("smoker", ("true", "false")), ("aphasia", ("Y", "N")),
                               ("neglect", ("1", "0")), ("include", ("1", "0"))):
            rows = [{"participant_id": f"sub-0{index}", column: value} for index, value in enumerate(values * 2)]
            self.assertIsNone(proposal(["participant_id", column], rows), f"{column} {values} was proposed as the groups")
        rows = [{"participant_id": "sub-01", "diagnosis": "yes"}, {"participant_id": "sub-02", "diagnosis": "no"}]
        self.assertEqual(proposal(["participant_id", "diagnosis"], rows), ("diagnosis", {"yes": "case", "no": "control"}))
        rows = [{"participant_id": "sub-01", "cohort": "patient"}, {"participant_id": "sub-02", "cohort": "HC"}]
        self.assertEqual(proposal(["participant_id", "cohort"], rows), ("cohort", {"patient": "case", "HC": "control"}))

        studioShell = shell.Shell(force=True)
        studioShell.apply()
        toasts = []
        studioShell.toast = lambda text, *args, **kwargs: toasts.append((str(text), kwargs))
        shown = []
        answers = []

        def fakeExec(dialog):
            # The group-mapping step: record what it shows, then answer as the test says
            combos = [combo for combo in dialog.findChildren("QComboBox") if combo.objectName == "StudioGroupMappingCombo"]
            values = [str(label.text).replace("→", "").strip() for label in dialog.findChildren("QLabel")
                      if label.objectName == "StudioGroupMappingValue"]
            mapping = {value: str(combo.currentText) for value, combo in zip(values, combos, strict=True)}
            title = [str(label.text) for label in dialog.findChildren("QLabel") if label.objectName == "StudioGroupMappingTitle"]
            shown.append((title, mapping))
            accept, choices = answers.pop(0)
            for value, text in choices.items():
                combo = combos[values.index(value)]
                combo.setCurrentIndex(combo.findText(text))
            return accept

        originalExec = getattr(study_ui, "_execDialog", None)
        study_ui._execDialog = fakeExec
        self.addCleanup(setattr, study_ui, "_execDialog", originalExec)
        realQt = study_ui.qt
        chosen = []

        class FileDialog:
            @staticmethod
            def getExistingDirectory(*args):
                return chosen[-1]

        class QtProxy:
            def __getattr__(self, name):
                return FileDialog if name == "QFileDialog" else getattr(realQt, name)

        def newStudy(folder):
            chosen.append(folder)
            study_ui.qt = QtProxy()
            try:
                return study_ui.newStudyDialog(studioShell)
            finally:
                study_ui.qt = realQt

        def groups(folder):
            columns, rows = read_tsv(os.path.join(folder, "participants.tsv"))
            return columns, [(row["participant_id"], row.get("group"), row.get("include")) for row in rows]

        try:
            # New study… on a folder with a yes / no symptom column: no groups are set, the column is kept as it was
            for name, column, values in (("Aphasia", "aphasia", ("yes", "yes", "no")), ("Smoker", "smoker", ("true", "false", "false")),
                                         ("AphasiaYN", "aphasia", ("Y", "N", "N"))):
                folder = makeFolder(name, column, values)
                session = newStudy(folder)
                self.assertIsNotNone(session)
                _columns, rows = groups(folder)
                self.assertEqual([group for _sid, group, _include in rows], ["n/a"] * 3, f"{column} became the groups")
                self.assertEqual([row[column] for row in read_tsv(os.path.join(folder, "participants.tsv"))[1]], list(values))
                study_ui.closeStudy()
            self.assertEqual(shown, [], "a step was shown without a group column")

            # The participants table's "Create study" (no study open) on a symptom column: no groups either
            folder = makeFolder("Table", "aphasia", ("yes", "no", "no"))
            del toasts[:]
            study_ui.openParticipantsTable(os.path.join(folder, "participants.tsv"), studioShell)
            offers = [kwargs for _text, kwargs in toasts if kwargs.get("actionText") == "Create study"]
            self.assertEqual(len(offers), 1, toasts)
            offers[0]["action"]()
            self.assertIsNotNone(study_ui.session())
            self.assertEqual([group for _sid, group, _include in groups(folder)[1]], ["n/a"] * 3)
            study_ui.closeStudy()

            # A group column: New study… shows the step first; Cancel makes no study
            folder = makeFolder("Diagnosis", "diagnosis", ("patient", "HC", "patient"))
            answers.append((False, {}))
            self.assertIsNone(newStudy(folder))
            self.assertEqual(shown[-1], (["participants.tsv has groups in column 'diagnosis'. Choose the study group of each value:"],
                                         {"patient": "Case", "HC": "Control"}))
            self.assertFalse(os.path.exists(os.path.join(folder, "study.json")), "Cancel made a study")
            self.assertFalse(os.path.exists(os.path.join(folder, ".studio")))
            # Create study with the mapping as chosen in the step (HC excluded)
            answers.append((True, {"HC": "Exclude"}))
            self.assertIsNotNone(newStudy(folder))
            self.assertEqual(groups(folder)[1], [("sub-01", "case", "1"), ("sub-02", "n/a", "0"), ("sub-03", "case", "1")])
            study_ui.closeStudy()

            # The participants table's "Create study" shows the same step
            folder = makeFolder("TableDiagnosis", "diagnosis", ("patient", "HC", "HC"))
            del toasts[:]
            study_ui.openParticipantsTable(os.path.join(folder, "participants.tsv"), studioShell)
            offers = [kwargs for _text, kwargs in toasts if kwargs.get("actionText") == "Create study"]
            answers.append((False, {}))
            offers[-1]["action"]()
            self.assertEqual(shown[-1][1], {"patient": "Case", "HC": "Control"})
            self.assertIsNone(study_ui.session())
            self.assertFalse(os.path.exists(os.path.join(folder, "study.json")))
            answers.append((True, {}))
            offers[-1]["action"]()
            self.assertIsNotNone(study_ui.session())
            self.assertEqual([group for _sid, group, _include in groups(folder)[1]], ["case", "control", "control"])
            self.assertEqual(answers, [])
        finally:
            study_ui.qt = realQt
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    # A QC key in a QC filter leaves no hidden selection (BB-1)

    def test_32_qc_key_in_a_qc_filter_moves_to_the_next_row(self):
        """P / X in a filter of the QC column (QC fail, Normalization to review) took the row out of the list but kept it in
        the shared selection: nothing was visibly selected, the next key did nothing, and the cards ("Redo N selected",
        BATCH) acted on the hidden row. The selection now goes on to the row that takes its place (BB-1).
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import library, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Queue study")
        os.makedirs(folder)
        paths = []
        for index in range(1, 6):
            paths.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(paths[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(6):
                slicer.app.processEvents()

        try:
            session = study_ui.createStudy(paths, studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.setQc(["sub-01", "sub-02", "sub-03", "sub-04"], "fail")
            # The subject list of the Library canvas (the only list in Studio: the selection is pruned to the rows it shows)
            studioShell.switchTo("library")
            pump()
            library.canvas().refreshData()
            studyList = library.canvas().studyPage.list
            studyList.setFilter("qcfail")
            pump()
            table, model = studyList.table, studyList.model
            self.assertEqual([model.sid(row) for row in range(model.rowCount())], ["sub-01", "sub-02", "sub-03", "sub-04"])
            table.selectionModel().setCurrentIndex(model.index(0, 0), qt.QItemSelectionModel.ClearAndSelect | qt.QItemSelectionModel.Rows)
            pump()
            self.assertEqual(library.studySelection(), ["sub-01"])
            # P passes sub-01, which leaves the list: sub-02 is selected (and the keyboard cursor is on it)
            studyList.setQc("pass")
            pump()
            self.assertEqual(session.study.subject("sub-01")["qc"], "pass")
            self.assertEqual(studyList.selectedIds(), ["sub-02"])
            self.assertEqual(library.studySelection(), ["sub-02"])
            self.assertEqual(model.sid(table.currentIndex().row()), "sub-02")
            # The next P marks sub-02 (it did nothing before)
            studyList.setQc("pass")
            pump()
            self.assertEqual(session.study.subject("sub-02")["qc"], "pass")
            self.assertEqual(library.studySelection(), ["sub-03"])
            # Two rows at once: the row after them is selected; the last row of the list leaves nothing hidden selected
            studyList.selectIds(["sub-03", "sub-04"])
            pump()
            studyList.setQc("pass")
            pump()
            self.assertEqual([session.study.subject(sid)["qc"] for sid in ("sub-03", "sub-04")], ["pass", "pass"])
            self.assertEqual(model.rowCount(), 0)
            self.assertEqual(library.studySelection(), [])
            self.assertEqual(session.study.subject("sub-05")["qc"], "unreviewed")
            # A chip that hides the selected rows takes them out of the selection too
            studyList.setFilter("all")
            studyList.selectIds(["sub-01", "sub-05"])
            pump()
            session.setQc(["sub-05"], "fail")
            studyList.setFilter("qcfail")
            pump()
            self.assertEqual(studyList.selectedIds(), ["sub-05"])
            self.assertEqual(library.studySelection(), ["sub-05"])
            # In All, a QC key keeps the selection where it is (the row stays)
            studyList.setFilter("all")
            studyList.selectIds(["sub-05"])
            pump()
            studyList.setQc("pass")
            pump()
            self.assertEqual(library.studySelection(), ["sub-05"])
            studyList.setFilter("all")
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_34_draw_edits_an_existing_segmentation(self):
        """A segmentation that Studio did not create (made by a script, loaded with slicer.util.loadSegmentation) was found by
        Draw but never given to the Segment Editor: Draw opened with no segmentation and no source volume ("No lesion",
        "Open a scan", tools disabled) and logged "SetSourceVolumeNode: need to set segment editor and segmentation nodes
        first". Draw edits it on its scan, as the classic Segment Editor does (BB-1).
        """
        from StudioLib import shell

        if slicer.util.mainWindow() is None or not hasattr(slicer.modules, "segmenteditor"):
            self.skipTest("needs the main window and the Segment Editor")
        slicer.mrmlScene.Clear(0)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(10):
                slicer.app.processEvents()

        def errors():
            model = slicer.app.errorLogModel()
            levelColumn, descriptionColumn = 2, 4  # ctkErrorLogAbstractModel::LogLevelColumn, DescriptionColumn
            return [str(model.index(row, descriptionColumn).data()) for row in range(model.rowCount())
                    if model.index(row, levelColumn).data() in ("Error", "Critical", "Fatal")]

        try:
            studioShell.switchTo("library")
            pump()
            volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "Scan")
            image = np.zeros((20, 24, 28), np.int16)
            image[5:15, 6:18, 7:21] = 200
            slicer.util.updateVolumeFromArray(volumeNode, image)
            volumeNode.CreateDefaultDisplayNodes()
            slicer.util.setSliceViewerLayers(background=volumeNode, fit=True)
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "MySeg")
            segmentationNode.CreateDefaultDisplayNodes()
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            segmentId = segmentationNode.GetSegmentation().AddEmptySegment("bright")
            slicer.util.updateSegmentBinaryLabelmapFromArray((image > 100).astype(np.uint8), segmentationNode, segmentId, volumeNode)
            pump()
            errorsBefore = len(errors())
            studioShell.switchTo("draw")
            pump()
            editor = slicer.modules.SegmentEditorWidget.editor
            self.assertIs(editor.segmentationNode(), segmentationNode)
            self.assertIs(editor.sourceVolumeNode(), volumeNode)
            self.assertEqual(editor.currentSegmentID(), segmentId)
            self.assertEqual([node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode")], ["MySeg"])
            self.assertEqual([text for text in errors()[errorsBefore:] if "SetSourceVolumeNode" in text], [])
        finally:
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_35_continue_in_draw_after_a_scene_load_logs_no_error_and_keeps_the_lesion(self):
        """A project opened from the Library detaches the Segment Editor node (a scene loads while Draw is not shown): Continue
        in Draw on a lesion selects it without the error "SetSegmentationNode: need to set segment editor node first" (the
        top-bar error badge), and Draw edits that lesion on its own scan, not the first one found.
        """
        import numpy as np
        from StudioLib import draw, importer, library, shell

        if slicer.util.mainWindow() is None or not hasattr(slicer.modules, "segmenteditor"):
            self.skipTest("needs the main window and the Segment Editor")
        slicer.mrmlScene.Clear(0)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(10):
                slicer.app.processEvents()

        def errors():
            model = slicer.app.errorLogModel()
            levelColumn, descriptionColumn = 2, 4  # ctkErrorLogAbstractModel::LogLevelColumn, DescriptionColumn
            return [str(model.index(row, descriptionColumn).data()) for row in range(model.rowCount())
                    if model.index(row, levelColumn).data() in ("Error", "Critical", "Fatal")]

        try:
            image = np.zeros((20, 24, 28), np.int16)
            image[5:15, 6:18, 7:21] = 200
            scans, lesions = [], []
            for name in ("A", "B"):
                volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "sub-0%s_T1w" % ("1" if name == "A" else "2"))
                slicer.util.updateVolumeFromArray(volumeNode, image)
                volumeNode.CreateDefaultDisplayNodes()
                segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion" + name)
                segmentationNode.CreateDefaultDisplayNodes()
                segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
                segmentId = segmentationNode.GetSegmentation().AddEmptySegment("bright")
                slicer.util.updateSegmentBinaryLabelmapFromArray((image > 100).astype(np.uint8), segmentationNode, segmentId, volumeNode)
                scans.append(volumeNode)
                lesions.append(segmentationNode)
            studioShell.switchTo("draw")
            pump()
            editor = slicer.modules.SegmentEditorWidget.editor
            self.assertIsNotNone(editor.mrmlSegmentEditorNode())
            studioShell.switchTo("library")
            pump()
            # What opening a project from the Library does to the editor
            draw._onSceneStartImport()
            self.assertIsNone(editor.mrmlSegmentEditorNode())
            errorsBefore = len(errors())
            library.continueInDraw(studioShell, segmentationNode=lesions[1], volumeNode=scans[1])
            pump()
            self.assertEqual([text for text in errors()[errorsBefore:] if "SetSegmentationNode" in text], [])
            self.assertIs(editor.segmentationNode(), lesions[1])
            self.assertIs(editor.sourceVolumeNode(), scans[1])
            self.assertEqual(editor.currentSegmentID(), lesions[1].GetSegmentation().GetNthSegmentID(0))
            self.assertEqual(importer.takePendingSelection(), None)
        finally:
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_36_deciding_the_last_lesion_of_the_review_queue_logs_no_error(self):
        """Approve (button or Ctrl+Enter) or Send of the last lesion of the review queue ends the review inside the decision:
        the footer is disposed and its widgets are deleted. The actions then must not hide the form of the deleted widgets
        (``ValueError: Trying to call 'hide' on a destroyed QFrame object`` in the log).
        """
        from StudioLib import review

        for mode in ("approve", "changes", "rejected"):
            decided = []
            holder = []

            def decide(to, comment, reason, holder=holder, decided=decided):
                decided.append((to, comment, reason))
                holder[0].dispose()  # the review ends with its last lesion: the footer is disposed ...
                holder[0].widget.delete()  # ... and its widgets are deleted
                return True

            actions = review.ReviewActions(decide, "StudioTest36")
            holder.append(actions)
            if mode == "approve":
                actions.primary()
            else:
                actions.showForm(mode)
                actions.commentEdit.setText("because")
                if mode == "rejected":
                    actions.reasonCombo.setCurrentIndex(1)
                actions.primary()
            self.assertEqual(len(decided), 1, mode)
            self.assertEqual(decided[0][0], "approved" if mode == "approve" else mode)
            actions.hideForm()  # a late call on the disposed actions does nothing either

    def test_37_paint_brush_not_shown_before_its_model_exists(self):
        """With the Paint effect active, the pointer entering a slice view (an Enter event, no mouse move) does not render the
        brush pipeline before the brush model exists: no VTK error "Input port 0 of algorithm vtkTransformPolyDataFilter ...
        has 0 connections but is not optional" is logged. The brush is shown by the first mouse move.
        """
        import vtk

        if slicer.util.mainWindow() is None or slicer.app.layoutManager() is None:
            self.skipTest("needs the main window")
        slicer.mrmlScene.Clear(0)
        editor = None
        try:
            volume = slicer.util.addVolumeFromArray(np.random.default_rng(1).integers(0, 100, (32, 32, 32)).astype(np.int16), name="Brush")
            segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
            segmentation.CreateDefaultDisplayNodes()
            segmentation.SetReferenceImageGeometryParameterFromVolumeNode(volume)
            segmentId = segmentation.GetSegmentation().AddEmptySegment("Lesion")
            editor = slicer.qMRMLSegmentEditorWidget()
            editor.setMRMLScene(slicer.mrmlScene)
            editor.setMRMLSegmentEditorNode(slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode"))
            editor.setSegmentationNode(segmentation)
            editor.setSourceVolumeNode(volume)
            editor.setCurrentSegmentID(segmentId)
            slicer.util.setSliceViewerLayers(background=volume, fit=True)
            slicer.app.processEvents()
            editor.setActiveEffectByName("Paint")
            slicer.app.processEvents()

            errorModel = slicer.app.errorLogModel()
            firstRow = errorModel.rowCount()
            sliceView = slicer.app.layoutManager().sliceWidget("Red").sliceView()
            interactor = sliceView.interactorStyle().GetInteractor()
            interactor.SetEventPosition(100, 100)
            interactor.InvokeEvent(vtk.vtkCommand.EnterEvent)
            slicer.app.processEvents()
            sliceView.forceRender()
            slicer.app.processEvents()

            errors = []
            for row in range(firstRow, errorModel.rowCount()):
                text = " | ".join(str(errorModel.data(errorModel.index(row, column))) for column in range(errorModel.columnCount()))
                if "| Error |" in text or "| Critical |" in text:
                    errors.append(text)
            self.assertEqual(errors, [])
        finally:
            if editor is not None:
                editor.setActiveEffectByName("")
                editor.setMRMLScene(None)
            slicer.app.processEvents()
            slicer.mrmlScene.Clear(0)

    def test_38_quit_after_fill_between_slices_exits_cleanly(self):
        """Quitting after the Fill between slices interpolator ran exits with code 0. Its thread_local ITK filters used to be
        destroyed in worker threads of the threader that end after ITK's globals at shutdown (SIGSEGV in
        ``itk::Object::Modified()``: "exit abnormally", exit status 1; about 2 runs in 3 here, every run in official 3D Slicer).
        """
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "quit_after_interpolation.py")
        with open(script, "w") as stream:
            stream.write(QUIT_AFTER_INTERPOLATION_SCRIPT)
        for attempt in range(4):
            home = os.path.join(self.directory, f"quit_interpolation_home{attempt}")
            os.makedirs(home)
            environment = dict(os.environ)
            environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config")})
            for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
                environment.pop(key, None)
            process = subprocess.run([launcher, "--no-splash", "--testing", "--python-script", script], env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
            output = process.stdout.decode("utf-8", "replace")
            self.assertIn("QUITTEST READY filled=", output, output[-3000:])
            self.assertNotIn("Traceback", output, output[-3000:])
            self.assertNotIn("exit abnormally", output, f"run {attempt + 1}: {output[-3000:]}")
            self.assertEqual(process.returncode, 0, f"run {attempt + 1}: {json.dumps(output[-3000:])}")

    def test_39_crash_check_studio_probe_runs_in_xvfb(self):
        """The A-01b probe of ``StudioAutosaveCrashCheck.py --studio`` is wrapped in xvfb-run (as every other Slicer run of the
        harness) unless ``--no-xvfb``, and only its exit code 3 says that ``StudioLib.autosave_ui`` is missing.
        """
        import argparse
        import importlib.util
        from unittest import mock

        path = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5), "Utilities", "Scripts",
                                             "StudioAutosaveCrashCheck.py"))
        if not os.path.isfile(path):
            self.skipTest("no source tree")
        spec = importlib.util.spec_from_file_location("StudioAutosaveCrashCheckProbeTest", path)
        crashCheck = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(crashCheck)

        calls = []

        def fake(returncode):
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, returncode, b"", b"")

            return run

        def probe(noXvfb, returncode):
            del calls[:]
            args = argparse.Namespace(slicer="/x/Slicer", out=self.directory, timeout=5, no_xvfb=noXvfb)
            with mock.patch.object(crashCheck.subprocess, "run", fake(returncode)):
                return crashCheck._probeStudio(args)

        self.assertIsNone(probe(False, 0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:3], ["xvfb-run", "-a", "-s"], calls[0])
        self.assertEqual(calls[0][4], "/x/Slicer", calls[0])
        self.assertIn("--no-main-window", calls[0])
        self.assertIn("StudioLib.autosave_ui", calls[0][-1])
        self.assertIsNone(probe(True, 0))
        self.assertEqual(calls[0][0], "/x/Slicer", calls[0])
        self.assertNotIn("xvfb-run", calls[0])
        self.assertIn("A-01b not present", probe(False, crashCheck.PROBE_ABSENT_EXIT))
        failed = probe(False, 1)
        self.assertIn("exited with code 1", failed)
        self.assertNotIn("not present", failed)

        # The real command line of a run goes through the same wrapper
        self.assertEqual(crashCheck._withDisplay(["a", "b"], argparse.Namespace(no_xvfb=False))[:3], ["xvfb-run", "-a", "-s"])
        self.assertEqual(crashCheck._withDisplay(["a", "b"], argparse.Namespace(no_xvfb=True)), ["a", "b"])

    def test_40_classic_mask_volume_does_not_widen_the_module_panel_for_good(self):
        """Choosing Mask volume in the Classic Segment Editor widens the module panel only as far as its options need and the
        panel is as narrow as before once the effect is left (it stayed 784 px wide, 57% of a 1366 px screen, for the rest of the
        session). The panel width is kept between modules, so this runs without ``--testing``, which turns that off.
        """
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "classic_panel_width.py")
        with open(script, "w") as stream:
            stream.write(CLASSIC_PANEL_WIDTH_SCRIPT)
        home = os.path.join(self.directory, "classic_panel_width_home")
        os.makedirs(os.path.join(home, ".config", "slicer.org"))
        with open(os.path.join(home, ".config", "slicer.org", "Slicer.ini"), "w") as stream:
            stream.write("[Studio]\nMode=classic\n")
        environment = dict(os.environ)
        environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"),
                            "SLICER_STUDIO_PROJECTS_ROOT": os.path.join(home, "Studio")})
        for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
            environment.pop(key, None)
        process = subprocess.run([launcher, "--no-splash", "--python-script", script], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
        output = process.stdout.decode("utf-8", "replace")
        lines = [line for line in output.splitlines() if line.startswith("PANELWIDTH ")]
        self.assertEqual(len(lines), 1, output[-3000:])
        result = json.loads(lines[0][len("PANELWIDTH "):])
        self.assertEqual(result["error"], "", result["error"])
        widths = [width for _, width in result["widths"]]
        self.assertEqual(len(widths), 6, result)
        start, paint, threshold, maskVolume, paintAgain, none = widths
        self.assertLessEqual(maskVolume, max(paint, threshold) + 100, f"Mask volume widened the panel: {result['widths']}")
        self.assertLessEqual(paintAgain, paint + 20, f"Paint after Mask volume: {result['widths']}")
        self.assertLessEqual(none, start + 20, f"No tool after Mask volume: {result['widths']}")

    def test_41_balance_shows_complete_separation_not_the_no_data_dash(self):
        """Cases all left and controls all right: the SMD is infinite, which the balance table says (not the "no data" dash)."""
        from LesionNetworkMappingLib import lesionset

        from StudioLib import libcard_cohort

        covariates = {f"c{k}": {"hemisphere_left": 1.0, "wm_frac": None} for k in range(3)}
        covariates.update({f"k{k}": {"hemisphere_left": 0.0, "wm_frac": None} for k in range(3)})
        table = lesionset.balance(covariates, ["c0", "c1", "c2"], ["k0", "k1", "k2"], names=("hemisphere_left", "wm_frac"))
        summary = {"balance": {name: lesionset._balance_row(row) for name, row in table.items()}}
        json.dumps(summary, allow_nan=False)  # stays strict JSON
        left, whiteMatter = libcard_cohort.balanceRows(summary)
        self.assertEqual((left[1], left[2], left[3], left[4]), ("100%", "0%", "+∞", True))
        self.assertEqual((whiteMatter[3], whiteMatter[4]), ("—", False))
        reverse = lesionset.balance(covariates, ["k0", "k1"], ["c0", "c1"], names=("hemisphere_left",))
        self.assertEqual(libcard_cohort.balanceRows({"balance": {"hemisphere_left": lesionset._balance_row(reverse["hemisphere_left"])}})[0][3], "-∞")

    def test_49_balance_table_shows_whole_covariate_names_in_the_inspector(self):
        """The balance table is only about 280 px wide in the 340 px inspector: "White matter share" and "Left hemisphere" wrap
        onto a second line (each row is tall enough for it) instead of ending in "..." (BB-1).
        """
        import qt
        from LesionNetworkMappingLib import lesionset

        from StudioLib import libcard_cohort, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        rows = [(f"sub-0{index}", {"group": "case" if index <= 3 else "control"}, False) for index in range(1, 6)]
        study = self._study(rows)
        cohortCard = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            cohortCard = libcard_cohort.CohortCard(session)
            cohortCard.widget.setFixedWidth(250)
            cohortCard.widget.show()
            covariates = {f"c{k}": {"volume_ml": 1.0 + k, "wm_frac": 0.3, "dim_2d": 0.0, "hemisphere_left": 1.0} for k in range(3)}
            covariates.update({f"k{k}": {"volume_ml": 2.0 + k, "wm_frac": 0.6, "dim_2d": 1.0, "hemisphere_left": 0.0} for k in range(3)})
            table = lesionset.balance(covariates, ["c0", "c1", "c2"], ["k0", "k1", "k2"], names=tuple(libcard_cohort.COVARIATE_LABELS))
            summary = {"balance": {name: lesionset._balance_row(row) for name, row in table.items()}}
            balanceRows = libcard_cohort.balanceRows(summary)
            self.assertEqual(len(balanceRows), 4)
            cohortCard.results.setVisible(True)
            cohortCard.balanceTable.setVisible(True)
            cohortCard._renderBalance(balanceRows)
            for _ in range(6):
                slicer.app.processEvents()
            balance = cohortCard.balanceTable
            metrics = balance.fontMetrics
            metrics = metrics() if callable(metrics) else metrics
            usable = balance.columnWidth(0) - 10
            self.assertLess(usable, metrics.horizontalAdvance("White matter share"), "the table is narrow enough for the check to mean something")
            for index, row in enumerate(balanceRows):
                label = row[0]
                self.assertEqual(balance.item(index, 0).toolTip(), label)
                for word in label.split():
                    self.assertLessEqual(metrics.horizontalAdvance(word), usable, f"the word {word!r} of {label!r} does not fit the column")
                needed = metrics.boundingRect(0, 0, usable, 10000, int(qt.Qt.TextWordWrap), label).height()
                self.assertGreaterEqual(balance.rowHeight(index), needed + 4, f"{label!r} needs {needed} px but its row is {balance.rowHeight(index)} px")
            self.assertGreaterEqual(balance.height, balance.horizontalHeader().height + sum(balance.rowHeight(i) for i in range(4)))
        finally:
            if cohortCard is not None:
                cohortCard.dispose()
                cohortCard.widget.deleteLater()

    def test_43_docs_describe_the_draw_inspector_as_the_app_builds_it(self):
        """studio.md describes the Draw inspector as it is (VISUAL 4.4): an Overlay row with an Outline switch, the display items
        in the panel menu and the footer link "Map this lesion's network →"; there is no Display card and no "Normalize to MNI…"
        footer button.
        """
        from StudioLib import draw

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "Docs", "user_guide", "modules", "studio.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        section = " ".join(text.split("### Draw", 1)[1].split("#### Draw keys", 1)[0].split())
        for stale in ("**Display** (collapsed)", "**Map network →**", "**Normalize to MNI…**"):
            self.assertNotIn(stale, section, f"studio.md still describes {stale} in the Draw inspector")
        for current in ("**Overlay**", "**Outline** switch", "**Voxel view**", "**Show lesion in 3D**", "**Map this lesion's network →**"):
            self.assertIn(current, section, f"studio.md does not describe {current} in the Draw inspector")
        # The names used by the docs are the ones the app shows
        self.assertIn('_("Map this lesion\'s network →")', inspect.getsource(draw._CtaFooter._secondaryAction))
        self.assertIn('_("Overlay")', inspect.getsource(draw._DisplayCard))

    def test_42_docs_name_the_classic_view_menu_item(self):
        """The docs call the Classic View menu item "Switch to Studio interface", as the app does (not Studio's "Studio interface")."""
        from StudioLib import shell

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("README.md", "Docs/user_guide/user_interface.md", "Docs/user_guide/modules/studio.md",
                 "Docs/user_guide/studio_coming_from_slicer.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        classicText = shell.Shell._classicSwitchText(types.SimpleNamespace(_active=False))
        self.assertEqual(classicText, "Switch to Studio interface")
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                text = " ".join(stream.read().replace("*", "").replace("`", "").split())
            self.assertNotIn("View > Studio interface", text, f"{name} names the Studio item for the way back from Classic")
        for name in names[:3]:
            with open(paths[name], encoding="utf-8") as stream:
                text = " ".join(stream.read().replace("*", "").replace("`", "").split())
            self.assertIn(f"View > {classicText}", text, f"{name} does not name the Classic View menu item")

    def test_44_docs_describe_saving_in_a_project(self):
        """studio.md and the README Quick start describe the Save lesion folder, the quit question and Ctrl+S as they are
        in a project (D-A16: the project's Lesions folder, "All changes are saved in Projects/<name>. Quit?", Ctrl+S saves
        the project now), and keep the old rules only for the cases without a project.
        """
        from StudioLib import draw, firstrun

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        paths = {name: os.path.join(root, *name.split("/")) for name in ("README.md", "Docs/user_guide/modules/studio.md")}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        studio = texts["Docs/user_guide/modules/studio.md"]
        readme = texts["README.md"]
        # The behaviour the docs describe
        self.assertIn("_projectLesionsFolder(create=True)", inspect.getsource(draw.defaultSaveDirectory))
        self.assertIn('"All changes are saved in {project}. Quit?"', inspect.getsource(firstrun.projectCloseSummary))
        # studio.md: the folder rule, the quit message and Ctrl+S name the project
        self.assertIn("In a project the folder is the project's `Lesions` folder", studio)
        self.assertIn('In a project the project is saved first and the message only asks "All changes are saved in Projects/<name>. Quit?"', studio)
        self.assertIn("In a project (every scan or template session), the project is saved at once", studio)
        self.assertNotIn("The folder is the last one you used, else the scan's folder", studio)
        self.assertNotIn("| save the scene, close the scene,", studio)
        # README Quick start: Ctrl+S saves the project; only without a project does it write a folder or a .mrb
        self.assertIn("proposes the project's `Lesions` folder", readme)
        self.assertIn("`Ctrl+S` saves it at once", readme)
        self.assertNotIn("An autosave is not a save: only these commands write your files", readme)

    def test_45_docs_describe_the_map_footer_as_the_app_builds_it(self):
        """studio.md and lesionnetworkmapping.md describe the Map footer as it is (VISUAL 4.5): one line, "Research use only.
        Lesion network mapping specificity is debated. Read more", the whole statement behind Read more (and in the Report
        card); studio.md lists every card of the Map inspector including Circuits, Compare and Report.
        """
        import LesionNetworkMappingUI.widgets as lnmwidgets

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("Docs/user_guide/modules/studio.md", "Docs/user_guide/modules/lesionnetworkmapping.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        studio, lnm = (texts[name] for name in names)
        # The behaviour the docs describe
        self.assertEqual(lnmwidgets.DISCLAIMER_LINE, "Research use only. Lesion network mapping specificity is debated.")
        self.assertIn("showDisclaimer(label)", inspect.getsource(lnmwidgets.disclaimerLine))
        line = lnmwidgets.DISCLAIMER_LINE + " Read more"
        for name, text in ((names[0], studio), (names[1], lnm)):
            self.assertIn(line, text, f"{name} does not describe the one-line Map footer")
        self.assertNotIn('footer that always reads "Research use only — not for clinical decisions"', studio)
        self.assertNotIn("the report and the footer of the module carry this statement", lnm)
        section = studio.split("### Map", 1)[1].split("### Other modules", 1)[0]
        for card in ("Circuits", "Compare", "Report"):
            self.assertIn(card, section, f"studio.md does not list the {card} card of the Map inspector")

    def test_46_docs_give_the_lnm_data_directory_default_as_the_app_uses_it(self):
        """settings.md and the README give the default of Studio/LNM/DataDirectory as the app uses it (A-09, D-A16): the
        LNM folder inside the Slicer Studio folder, and Documents/SlicerStudio only when that older folder already holds
        data (as studio_projects.md says), not Documents/SlicerStudio as the default.
        """
        import LesionNetworkMappingUI.normalize_controller as normalize

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("README.md", "Docs/user_guide/settings.md", "Docs/user_guide/studio_projects.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        # The behaviour the docs describe: the Studio folder's LNM folder, the legacy folder only when it holds data
        source = inspect.getsource(normalize.dataDirectory)
        self.assertIn("projects.lnmFolder()", source)
        self.assertIn("os.listdir(legacy)", source)
        readme, settings, projects = (texts[name] for name in names)
        self.assertIn("`Studio/LNM/DataDirectory` | the `LNM` folder inside your Slicer Studio folder", settings)
        self.assertIn("only if it already holds data", settings)
        self.assertNotIn("| the `SlicerStudio` folder in your documents |", settings)
        self.assertIn("`Studio/LNM/DataDirectory` | the `LNM` folder inside the Slicer Studio folder", readme)
        self.assertIn("`<Slicer Studio folder>/LNM/normalization/<name>-<hash>`", readme)
        self.assertNotIn("`Studio/LNM/DataDirectory` | `Documents/SlicerStudio`", readme)
        self.assertNotIn("Without one: `Documents/SlicerStudio/normalization/<name>-<hash>`", readme)
        self.assertIn("LNM/ normalization outputs, control sets and recipes", projects)

    def test_47_docs_name_the_lnm_buttons_the_app_shows(self):
        """lnm_intake.md, lnm_statistics.md and studio_templates.md name the single-slice extrusion choices, the rule
        banner buttons and the Library normalization button with the labels of the app, not with the spec's shorthand
        (Keep as drawn, Extrude to a total thickness, Extrude as a binary mask, Use anyway, Normalize all).
        """
        import LesionNetworkMapping as lnm
        from StudioLib import lesionqc, libcard_normalize

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("Docs/user_guide/modules/lnm_intake.md", "Docs/user_guide/modules/lnm_statistics.md",
                 "Docs/user_guide/studio_templates.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        intake, statistics, templates = (texts[name] for name in names)
        # The labels the app shows
        qc = inspect.getsource(lesionqc)
        for label in ("Keep single-slice", "Extrude to {mm:g} mm total", "Extrude ±{mm:g} mm, binary"):
            self.assertIn('_("%s")' % label, qc)
        banner = inspect.getsource(lnm)
        self.assertIn('(_("Switch to FWE"), self.onSwitchToFwe), (_("Keep"), self.onKeepRule)', banner)
        self.assertIn('_("Normalize native subjects")', inspect.getsource(libcard_normalize))
        # The docs use them
        for text in ("Keep single-slice", "Extrude to N mm total", "Extrude ±N/2 mm, binary"):
            self.assertIn(text, intake)
        for text in ("Keep as drawn", "Extrude to a total thickness", "Extrude as a binary mask"):
            self.assertNotIn(text, intake)
        self.assertIn("Switch to FWE", statistics)
        self.assertIn("Keep in the banner", statistics)
        self.assertNotIn("Use anyway", statistics)
        self.assertIn("Normalize native subjects in the Library", templates)
        self.assertNotIn("Normalize all in the Library", templates)

    def test_51_docs_quote_the_view_orientation_statement_as_the_views_show_it(self):
        """studio.md quotes the statement of the axial and coronal views as the views show it (VISUAL 4.3): "Radiological · L on
        right" or "Neurological · L on left" at the bottom right, not "Left is on screen right" at the bottom left.
        """
        from StudioLib import orientation

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "Docs", "user_guide", "modules", "studio.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            text = stream.read()
        section = " ".join(text.split("## Left and right", 1)[1].split("\n## ", 1)[0].split())
        self.assertNotIn("Left is on screen", section, "studio.md still quotes the old statement")
        # The statements quoted are the ones the views show (a radiological and a flipped view, and the short forms)
        shown = {orientation.displayStatement(matrix) for matrix in self._sliceToRasPair(orientation)}
        self.assertEqual(shown, {"Radiological · L on right", "Neurological · L on left"})
        for statement in shown:
            self.assertIn(f'"{statement}"', section)
        for short in ("L on right", "L on left"):
            self.assertIn(f'"{short}"', section)
        self.assertIn("bottom right", section)
        self.assertNotIn("bottom left, ", section)

    @staticmethod
    def _sliceToRasPair(orientation):
        """The SliceToRAS matrices of an axial view in the radiological display (x axis towards -R) and of a flipped one."""
        import vtk

        matrices = []
        for sign in (-1.0, 1.0):
            matrix = vtk.vtkMatrix4x4()
            matrix.SetElement(0, 0, sign)
            matrices.append(matrix)
        return matrices

    def test_50_wrap_check_box_row_is_one_line_high_when_the_text_fits(self):
        """The row of ui.wrapCheckBox() was laid out for two lines at every width (setSizePolicy() on the label dropped the
        height-for-width flag that setWordWrap() sets), and the label centred its text: in the wide Classic panel the text
        of "Blind tracing" sat 9 px below its check box (BB-1). The row is now as high as its text needs, and the text starts
        level with the first line of the check box.
        """
        from StudioLib import ui

        text = "Blind tracing: hide the other tracers' names while a case is traced"
        host = qt.QWidget()
        try:
            hostLayout = qt.QVBoxLayout(host)
            hostLayout.setContentsMargins(0, 0, 0, 0)
            checkBox = qt.QCheckBox(text)
            checkBox.setObjectName("StudioTestWrapCheckBox")
            hostLayout.addWidget(ui.wrapCheckBox(checkBox))
            hostLayout.addStretch(1)
            host.show()
            row = checkBox.parentWidget()
            label = next(w for w in row.findChildren("QLabel") if w.objectName == "StudioWrapCheckBoxText")
            metrics = label.fontMetrics
            metrics = metrics() if callable(metrics) else metrics
            lineHeight = metrics.height()
            results = {}
            for width in (900, 240):
                host.resize(width, 300)
                for _ in range(6):
                    slicer.app.processEvents()
                needed = label.heightForWidth(label.width)
                results[width] = needed
                self.assertEqual(row.height, needed, f"width {width}: the row is {row.height} px high, its text needs {needed} px")
                self.assertLessEqual(label.y, 1, "the text starts at the top of the row")
                alignment = _flagsValue(label.alignment)  # None: an opaque Qt 6 QFlags read; the y and height checks remain
                if alignment is not None:
                    self.assertTrue(alignment & int(qt.Qt.AlignTop), "the text is top-aligned")
                firstLineCentre = label.y + lineHeight / 2
                boxCentre = checkBox.y + checkBox.height / 2
                self.assertLessEqual(abs(firstLineCentre - boxCentre), 3, f"width {width}: text {firstLineCentre}, box {boxCentre}")
            self.assertLess(results[900], 1.5 * lineHeight, "one line in the wide row")
            self.assertGreaterEqual(results[240], 2 * lineHeight, "two or more lines in the narrow row")
            self.assertEqual(str(checkBox.accessibleName), text)
        finally:
            host.deleteLater()

    def test_48b_case_field_esc_gives_up_the_typed_value(self):
        """Esc in a Case field with unsent edits restored nothing and the next focus-out wrote the abandoned text to
        participants.tsv (BB-1). Esc now puts the stored value back (a plain field and the line edit of a lenient combo).
        """
        from StudioLib import libcard_columns, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        host = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            host = qt.QWidget()
            hostLayout = qt.QVBoxLayout(host)
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            hostLayout.addWidget(sectionWidget)
            host.resize(306, 700)
            host.show()
            section.refresh("sub-02")
            for key in ("imaging", "case"):
                button = section.groups[key][0]
                if not button.isChecked():
                    button.click()
            slicer.app.processEvents()
            self.assertTrue(section.setField("age", "70"))
            section.refresh("sub-02")

            def press(widget, eventType):
                event = qt.QKeyEvent(eventType, qt.Qt.Key_Escape, qt.Qt.NoModifier)
                qt.QApplication.sendEvent(widget, event)
                return event.isAccepted()

            edit = section.editors["age"]
            host.activateWindow()
            host.raise_()
            edit.setFocus(qt.Qt.OtherFocusReason)
            for _ in range(10):
                slicer.app.processEvents()
            if not edit.hasFocus():
                self.skipTest("the window cannot take the keyboard focus here")
            edit.setText("99")
            self.assertTrue(press(edit, qt.QEvent.ShortcutOverride), "Esc with unsent edits stays with the field")
            self.assertTrue(press(edit, qt.QEvent.KeyPress))
            self.assertEqual(str(edit.text), "70", "Esc puts the stored value back")
            self.assertFalse(press(edit, qt.QEvent.ShortcutOverride), "without unsent edits Esc goes on to the shell")
            edit.clearFocus()
            slicer.app.processEvents()
            self.assertEqual(str(session.study.subject("sub-02")["age"]), "70", "the abandoned value is not written")
        finally:
            if host is not None:
                host.deleteLater()

    def test_48c_case_combo_field_esc_gives_up_the_typed_value(self):
        """Esc in an editable choice field of the Case section (Sex, Image quality) kept the typed value, and the shell
        moved the focus out, which wrote it to participants.tsv (BB-1): the combo box has the focus and passes keys to
        its line edit directly, so the filter on the line edit never saw Esc. Esc now puts the stored level back.
        """
        from StudioLib import libcard_columns, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        host = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            host = qt.QWidget()
            hostLayout = qt.QVBoxLayout(host)
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            hostLayout.addWidget(sectionWidget)
            host.resize(306, 900)
            host.show()
            section.refresh("sub-02")
            for button, _container, _columns in section.groups.values():
                if not button.isChecked():
                    button.click()
            slicer.app.processEvents()
            self.assertTrue(section.setField("sex", "M"))
            self.assertTrue(section.setField("image_quality", "2"))
            section.refresh("sub-02")

            def press(widget, eventType):
                event = qt.QKeyEvent(eventType, qt.Qt.Key_Escape, qt.Qt.NoModifier)
                qt.QApplication.sendEvent(widget, event)
                return event.isAccepted()

            host.activateWindow()
            host.raise_()
            for column, stored, typed in (("sex", "M", "Female"), ("image_quality", "2", "3")):
                combo = section.editors[column]
                self.assertTrue(combo.isEditable(), column)
                shown = str(combo.currentText)
                combo.setFocus(qt.Qt.OtherFocusReason)
                for _ in range(10):
                    slicer.app.processEvents()
                if not combo.hasFocus():
                    self.skipTest("the window cannot take the keyboard focus here")
                self.assertFalse(press(combo, qt.QEvent.ShortcutOverride), f"{column}: without unsent edits Esc goes on to the shell")
                combo.setEditText(typed)
                combo.lineEdit().setModified(True)
                # The keys go to the combo box, which has the focus (its line edit has it as focus proxy)
                self.assertTrue(press(combo, qt.QEvent.ShortcutOverride), f"{column}: Esc with unsent edits stays with the field")
                self.assertTrue(press(combo, qt.QEvent.KeyPress))
                self.assertEqual(str(combo.currentText), shown, f"{column}: Esc puts the stored level back")
                self.assertEqual(combo.itemData(combo.currentIndex), stored)
                self.assertFalse(press(combo, qt.QEvent.ShortcutOverride), f"{column}: after Esc the next Esc goes on to the shell")
                combo.clearFocus()
                slicer.app.processEvents()
                self.assertEqual(str(session.study.subject("sub-02")[column]), stored, f"{column}: the abandoned value is not written")
        finally:
            if host is not None:
                host.deleteLater()

    def test_47b_case_line_edits_show_the_start_of_a_long_value(self):
        """A Case section line edit that is not being edited showed the end of a long value (cursor at the end after
        setText), so the first author or the start of the etiology was never visible (BB-1). It shows the start now.
        """
        from StudioLib import libcard_columns, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        host = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            host = qt.QWidget()
            hostLayout = qt.QVBoxLayout(host)
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            hostLayout.addWidget(sectionWidget)
            host.resize(306, 900)
            host.show()
            section.refresh("sub-02")
            for button, _container, _columns in section.groups.values():
                if not button.isChecked():
                    button.click()
            long = "Müller A, Schmidt B. Neglect after thalamic stroke. Brain 2020"
            for column in ("citation", "etiology"):
                self.assertTrue(section.setField(column, long), column)
            section.refresh("sub-01")
            section.refresh("sub-02")
            slicer.app.processEvents()
            for column in ("citation", "etiology"):
                editor = section.editors[column]
                self.assertEqual(str(editor.text), long, column)
                self.assertEqual(editor.cursorPosition, 0, f"{column}: a field not being edited shows the start of its value")
        finally:
            if host is not None:
                host.deleteLater()

    def test_48_case_doi_field_is_as_wide_as_the_other_source_fields(self):
        """The Case section's DOI field was 53 px wide in a 137 px field: the flat "Open" button next to it keeps a minimum
        width of about 80 px, so no DOI could be read (BB-1). The edit now has the width of the other fields and the link
        sits below it.
        """
        from StudioLib import libcard_columns, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([("sub-01", {}, False), ("sub-02", {}, False)])
        section = host = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            host = qt.QWidget()
            hostLayout = qt.QVBoxLayout(host)
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            hostLayout.addWidget(sectionWidget)
            host.resize(306, 600)
            host.show()
            section.refresh("sub-02")
            button = section.groups["source"][0]
            if not button.isChecked():
                button.click()
            doi = "10.1016/j.cortex.2020.01.001"
            section.setField("doi", doi)
            section.setField("pmid", "31234567")
            for _ in range(6):
                slicer.app.processEvents()
            edit, pmid, link = section.editors["doi"], section.editors["pmid"], section.doiButton
            self.assertTrue(edit.isVisible() and link.isVisible())
            self.assertGreaterEqual(edit.width, pmid.width - 2, f"DOI edit {edit.width} px, PMID edit {pmid.width} px")
            self.assertGreaterEqual(edit.width, 100)
            self.assertEqual(str(edit.text), doi)
            self.assertLess(link.width, 100, "the Open link is compact")
            self.assertTrue(link.enabled, "a DOI that resolves enables the Open link")
        finally:
            if host is not None:
                host.deleteLater()
            study_ui.closeStudy()

    def test_52_new_lesion_is_not_offered_in_a_study(self):
        """In a study the Lesion card's ⋯ menu (and Search) offered New lesion, although the card's own New lesion button is
        hidden there: it made a "Lesion 2" next to the subject's lesion and hid it. Done then found the subject's lesion empty
        ("nothing was saved"), leaving the subject wrote nothing, and "Lesion 2" stayed shown over the next subject's scan,
        saved nowhere (BB-1). New lesion is not offered in a study, and a new lesion on the subject's scan is refused.
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import draw, importer, palette, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "New lesion study")
        os.makedirs(folder)
        scans = []
        for sid in ("sub-01", "sub-02"):
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        toasts = []
        originalToast = studioShell.toast
        studioShell.toast = lambda text, *args, **kwargs: (toasts.append(str(text)), originalToast(text, *args, **kwargs))[1]

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def lesionNames():
            return sorted(node.GetName() for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode"))

        def labels():
            return [action[0] for action in draw.lesionMenuActions(studioShell) if action]

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.open("sub-01", switchToDraw=True)
            pump()
            self.assertEqual(session.current(), "sub-01")
            before = lesionNames()

            # Not offered: the ⋯ menu (Save lesion saves the subject's lesion instead) and Search
            self.assertNotIn("New lesion", labels())
            self.assertLess(labels().index("Save lesion"), labels().index("Save lesion as…"))
            drawEntry = [entry for entry in draw._paletteEntries(studioShell) if entry["id"] == "action.newLesion"]
            self.assertEqual(len(drawEntry), 1)
            self.assertFalse(drawEntry[0]["enabled"]())
            index = palette.Index(studioShell)
            commandEntry = [entry for entry in index._commandEntries() if entry.id == "action.newLesion"]
            self.assertTrue(all(not entry.isEnabled() for entry in commandEntry))
            index.refresh()
            if index.entry("action.newLesion") is not None:
                self.assertFalse(index.entry("action.newLesion").isEnabled())

            # Refused when called anyway: no second lesion on the subject's scan, and the user is told why
            self.assertIsNone(draw.newLesion(studioShell))
            self.assertIsNone(importer.newLesionSegmentation(shell=studioShell))
            pump()
            self.assertEqual(lesionNames(), before)
            self.assertTrue(any("not available in a study" in text for text in toasts), toasts)
            displayNode = session.segmentationNode().GetDisplayNode()
            self.assertTrue(displayNode.GetVisibility() and displayNode.GetVisibility2D(), "the subject's lesion stays shown")

            # The subject's lesion is the one that is painted and saved by Done
            segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()
            self.assertIsNotNone(session.done())
            pump()
            self.assertEqual(session.study.subject("sub-01")["status"], "traced")

            # Without a study the ⋯ menu offers New lesion as before
            study_ui.closeStudy()
            pump()
            self.assertIn("New lesion", labels())
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_53_a_scan_opened_from_outside_a_study_never_feeds_done_next(self):
        """With a study open, a scan opened from outside it (Open…, Library, a drop) was traced under the study's breadcrumb:
        Draw edited "Lesions (<scan>)", the footer still read "Done → next", and Done answered "The lesion of sub-01 is
        empty: nothing was saved" about the hidden subject. The tracing was saved nowhere (no project is saved while a
        study is open) (BB-1). A subject's own scan now opens that subject in the study; any other scan closes the study
        first (its subject's edits are written). A lesion of another scan in Draw is saved with Save lesion, never with
        Done → next of the hidden subject.
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import draw, importer, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Outside scan study")
        os.makedirs(folder)
        scans = []
        for sid in ("sub-01", "sub-02", "sub-03"):
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        os.makedirs(os.path.join(self.directory, "own"))
        outside = os.path.join(self.directory, "own", "patient08_T1w.nii.gz")
        nifti.write_nifti(outside, np.ones(grid.shape, np.uint8), grid.affine)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        toasts = []
        originalToast = studioShell.toast
        studioShell.toast = lambda text, *args, **kwargs: (toasts.append(str(text)), originalToast(text, *args, **kwargs))[1]
        originalSave = draw.saveLesionNow
        saved = []

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def paint(segmentationNode, volumeNode):
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()

        def primaryLabel():
            draw.session(studioShell).refreshPanel()
            pump()
            return str(studioShell.primaryActionLabel())

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            session.open("sub-01", switchToDraw=True)
            pump()
            self.assertEqual(session.current(), "sub-01")

            # A subject's own scan opens that subject in the study (not a copy traced outside it)
            importer.loadFiles([scans[1]], studioShell)
            pump()
            self.assertIs(study_ui.session(), session)
            self.assertEqual(session.current(), "sub-02")
            self.assertEqual(draw.lesionNodes()[0].GetID(), session.segmentationNode().GetID())
            paint(session.segmentationNode(), session.volumeNode())

            # Another patient's scan closes the study first: sub-02's tracing is written, and Draw saves the new scan's lesion
            importer.loadFiles([outside], studioShell)
            pump()
            self.assertIsNone(study_ui.session())
            self.assertIsNone(getattr(studioShell, "studySession", None))
            lesionsFolder = os.path.join(folder, "lesions")
            self.assertTrue(any(name.startswith("sub-02") for name in os.listdir(lesionsFolder)), os.listdir(lesionsFolder))
            self.assertTrue(any("is not one of its subjects" in text for text in toasts), toasts)
            segmentationNode, sourceNode = draw.lesionNodes()
            self.assertEqual(sourceNode.GetName(), "patient08_T1w")
            self.assertIn("patient08_T1w", segmentationNode.GetName())
            self.assertTrue(primaryLabel().startswith("Save lesion"), primaryLabel())

            # A lesion of another scan in Draw while a study is open (a route of 3D Slicer such as Add Data): Save lesion,
            # never Done → next of the hidden subject
            reopened = study_ui.openStudy(folder, studioShell, subject="sub-01")
            pump()
            self.assertEqual(reopened.current(), "sub-01")
            self.assertTrue(primaryLabel().startswith("Done"), primaryLabel())
            outsideLesion = importer.createLesionSegmentation(sourceNode, "Lesions (patient08_T1w)")
            importer.selectInSegmentEditor(outsideLesion, sourceNode)
            paint(outsideLesion, sourceNode)
            self.assertTrue(primaryLabel().startswith("Save lesion"), primaryLabel())
            del toasts[:]
            draw.saveLesionNow = lambda shell=None: saved.append(shell)
            draw.doneNext(studioShell)
            pump()
            self.assertEqual(len(saved), 1)
            self.assertEqual(reopened.current(), "sub-01")
            self.assertNotEqual(reopened.study.subject("sub-01")["status"], "traced")
            self.assertFalse(any("sub-01" in text for text in toasts), toasts)
        finally:
            draw.saveLesionNow = originalSave
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_55_a_scene_loaded_after_another_keeps_its_segments_visible(self):
        """Opening project X after project Y (``slicer.util.loadScene(X, clear=True)``, what Open project..., Recent and the crash
        card do) hid X's lesion segment: the segment selector of each slice controller restored the segment IDs it had checked
        for Y's segmentation, found none of them in X's and reported "nothing checked", so the slice controller hid every
        segment of X. The Draw eye button stayed on, and the next save wrote ``Visible:false`` for official 3D Slicer too (BB-1).
        """
        import numpy as np

        if slicer.util.mainWindow() is None or slicer.app.layoutManager() is None:
            self.skipTest("needs the main window and its slice views")
        slicer.mrmlScene.Clear(0)
        self.addCleanup(slicer.mrmlScene.Clear, 0)

        def pump():
            for _ in range(5):
                slicer.app.processEvents()

        def saveProject(name, segmentId):
            """A scene with a scan and a lesion whose segment ID is unique, as Studio's lesions are."""
            slicer.mrmlScene.Clear(0)
            image = np.zeros((10, 12, 14), np.int16)
            image[3:7, 4:8, 5:9] = 100
            volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan" + name)
            slicer.util.updateVolumeFromArray(volumeNode, image)
            volumeNode.CreateDefaultDisplayNodes()
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions")
            segmentationNode.CreateDefaultDisplayNodes()
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            segmentationNode.GetSegmentation().AddEmptySegment(segmentId, "lesion")
            slicer.util.updateSegmentBinaryLabelmapFromArray((image > 50).astype(np.uint8), segmentationNode, segmentId, volumeNode)
            folder = os.path.join(self.directory, name)
            os.makedirs(folder)
            self.assertTrue(slicer.app.applicationLogic().SaveSceneToSlicerDataBundleDirectory(folder))
            pump()
            return os.path.join(folder, name + ".mrml")

        sceneX = saveProject("X", "2.25.1111")
        sceneY = saveProject("Y", "2.25.2222")
        slicer.mrmlScene.Clear(0)
        pump()
        slicer.util.loadScene(sceneY)
        pump()
        slicer.util.loadScene(sceneX, {"clear": True})
        pump()
        segmentationNodes = slicer.util.getNodesByClass("vtkMRMLSegmentationNode")
        self.assertEqual(len(segmentationNodes), 1)
        segmentationNode = segmentationNodes[0]
        self.assertEqual(segmentationNode.GetSegmentation().GetNumberOfSegments(), 1)
        self.assertEqual(segmentationNode.GetSegmentation().GetNthSegmentID(0), "2.25.1111")
        self.assertTrue(segmentationNode.GetDisplayNode().GetSegmentVisibility("2.25.1111"),
                        "the lesion of the scene loaded second is shown as it was saved")
        # The slice view's segment checklist follows the new lesion and still shows and hides it
        selector = slicer.app.layoutManager().sliceWidget("Red").sliceController().findChild("qMRMLSegmentSelectorWidget", "SegmentSelectorWidget")
        self.assertIsNotNone(selector)
        self.assertIs(selector.currentNode(), segmentationNode)
        self.assertEqual(list(selector.selectedSegmentIDs()), ["2.25.1111"])
        selector.setSelectedSegmentIDs([])
        self.assertFalse(segmentationNode.GetDisplayNode().GetSegmentVisibility("2.25.1111"))
        selector.setSelectedSegmentIDs(["2.25.1111"])
        self.assertTrue(segmentationNode.GetDisplayNode().GetSegmentVisibility("2.25.1111"))

    def test_55b_segment_selector_keeps_the_stored_selection_for_ids_the_new_node_has(self):
        """qMRMLSegmentSelectorWidget is a public widget of official 3D Slicer. Its multi-selection is kept across a change of the
        segmentation node, as upstream does, for the segment IDs that exist in the new node: a scripted module that restores a saved
        selection, or keeps it across segmentations with matching IDs, relies on it. Only the IDs that the new node lacks are dropped,
        silently (BB-1: the fix of the hidden lesion on project open had cleared the whole selection on every node change).
        """
        slicer.mrmlScene.Clear(0)
        self.addCleanup(slicer.mrmlScene.Clear, 0)

        def pump():
            for _ in range(5):
                slicer.app.processEvents()

        def addSegmentation(name, segmentIds):
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
            node.CreateDefaultDisplayNodes()
            for segmentId in segmentIds:
                node.GetSegmentation().AddEmptySegment(segmentId, name + "-" + segmentId)
            return node

        nodeA = addSegmentation("segA", ["Segment_1", "Segment_2", "Segment_3"])
        nodeB = addSegmentation("segB", ["Segment_1", "Segment_2", "Segment_3"])
        nodeC = addSegmentation("segC", ["Segment_2", "Other"])
        widget = slicer.qMRMLSegmentSelectorWidget()
        self.addCleanup(widget.deleteLater)
        widget.multiSelection = True
        widget.setMRMLScene(slicer.mrmlScene)
        signals = []
        widget.connect("segmentSelectionChanged(QStringList)", lambda ids: signals.append(list(ids)))
        widget.setCurrentNode(nodeA)
        widget.setSelectedSegmentIDs(["Segment_1", "Segment_3"])
        pump()
        self.assertEqual(list(widget.selectedSegmentIDs()), ["Segment_1", "Segment_3"])
        signals.clear()

        widget.setCurrentNode(nodeB)
        pump()
        self.assertEqual(list(widget.selectedSegmentIDs()), ["Segment_1", "Segment_3"], "IDs that the new node has are kept")
        widget.setCurrentNode(nodeA)
        pump()
        self.assertEqual(list(widget.selectedSegmentIDs()), ["Segment_1", "Segment_3"], "and kept when switching back")
        self.assertEqual(signals, [], "a node change that keeps the selection reports no change")

        widget.setCurrentNode(nodeC)
        pump()
        self.assertEqual(list(widget.selectedSegmentIDs()), [], "IDs that the new node lacks are dropped")
        self.assertEqual(signals, [], "and silently, so listeners do not hide the new node's segments")
        widget.setSelectedSegmentIDs(["Segment_2", "Other"])
        pump()
        signals.clear()
        widget.setCurrentNode(nodeB)
        pump()
        self.assertEqual(list(widget.selectedSegmentIDs()), ["Segment_2"], "only the IDs that exist in the new node are kept")
        self.assertEqual(signals, [])

    def test_54_return_in_the_columns_dialog_never_cancels_the_edits(self):
        """Columns...: Return that a focused child leaves unhandled (a closed role combo, the table, the "Only columns without
        a role" check box) clicked Cancel, the dialog's default button, so every edit was dropped without a word (BB-1). The
        dialog has no default button now: Return does nothing, and the edits stay until Confirm or Cancel is clicked.
        """
        from StudioLib import libcard_columns as lc
        from StudioLib import study_ui

        study = self._study([("sub-01", {"my_score": "3"}, False), ("sub-02", {"my_score": "5"}, False)])
        folder = study.path
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        slicer.app.processEvents()
        self.assertEqual(session.study.column_spec("my_score").role, lc.NOT_SET)
        jsonPath = os.path.join(folder, "participants.json")

        def saved():
            if not os.path.exists(jsonPath):
                return None
            with open(jsonPath, encoding="utf-8") as f:
                return f.read()

        before = saved()
        dialog = lc.ColumnsDialog(session)
        self.addCleanup(dialog.widget.deleteLater)
        dialog.show()
        slicer.app.processEvents()
        self.assertEqual([button.objectName for button in dialog.widget.findChildren("QPushButton") if button.isDefault()], [],
                         "no button of the Columns dialog is clicked by Return")
        dialog.setRole("my_score", "score")
        dialog.onlyUnset.setChecked(False)
        for target in (dialog.roleCombo("my_score"), dialog.table, dialog.onlyUnset, dialog.cancelButton, dialog.confirmButton):
            for key in (qt.Qt.Key_Return, qt.Qt.Key_Enter):
                target.setFocus()
                for eventType in (qt.QEvent.KeyPress, qt.QEvent.KeyRelease):
                    slicer.app.sendEvent(target, qt.QKeyEvent(eventType, key, qt.Qt.NoModifier))
                slicer.app.processEvents()
                self.assertTrue(dialog.widget.isVisible(), f"Return on {target.objectName or target.className()} closed the dialog")
        self.assertEqual(dialog.widget.result(), 0)
        self.assertEqual(saved(), before, "Return wrote nothing")
        self.assertEqual(dialog.changes()[0], {"my_score": {"role": "score"}}, "the edit is still there after Return")
        self.assertEqual(session.study.column_spec("my_score").role, lc.NOT_SET)
        # Confirm still saves (the edits were kept for it)
        dialog.confirmButton.click()
        slicer.app.processEvents()
        self.assertEqual(session.study.column_spec("my_score").role, "score")
        self.assertFalse(dialog.widget.isVisible())

    def test_58_levels_of_registered_columns_in_columns_and_the_case_section(self):
        """Levels typed in Columns... for a registered column were saved, but the Case section kept offering, warning about and
        refusing against the registry's levels (sex "X" got "Unknown sex value 'X'", dimensionality "mixed" was refused) (BB-1).
        A lenient column (sex) now offers and accepts the levels the study declares; the Levels of a strict registered column
        (dimensionality) are read-only, like the Units of a registered column, because the application refuses any other value.
        """
        from StudioLib import libcard_columns as lc
        from StudioLib import study_ui

        study = self._study([("sub-01", {"sex": "F", "dimensionality": "3D"}, False), ("sub-02", {"sex": "M", "dimensionality": "3D"}, False)])
        session = study_ui.openStudy(study.path, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        slicer.app.processEvents()
        dialog = lc.ColumnsDialog(session)
        self.addCleanup(dialog.widget.deleteLater)
        dialog.show()
        dialog.onlyUnset.setChecked(False)
        slicer.app.processEvents()

        def editable(name):
            return bool(dialog.table.item(dialog.rowOf(name), lc.COL_LEVELS).flags() & qt.Qt.ItemIsEditable)

        self.assertFalse(editable("dimensionality"), "the levels of a strict registered column are fixed")
        self.assertTrue(editable("sex"), "the levels of a lenient registered column can be declared")
        dialog.setText("sex", lc.COL_LEVELS, "M = male; F = female; O = other; X = non-binary")
        self.assertEqual(list(dialog.changes()[0]), ["sex"], "the read-only Levels of dimensionality are not a change")
        self.assertEqual(sorted(dialog.confirm()["applied"]), ["sex"])
        slicer.app.processEvents()

        host = qt.QWidget()
        self.addCleanup(host.deleteLater)
        hostLayout = qt.QVBoxLayout(host)
        sectionWidget = lc._createSection(session)
        section = lc._sections[sectionWidget]
        hostLayout.addWidget(sectionWidget)
        host.resize(306, 700)
        host.show()
        section.refresh("sub-01")
        for key in ("imaging", "case"):
            button = section.groups[key][0]
            if not button.isChecked():
                button.click()
        slicer.app.processEvents()
        combo = section.editors["sex"]
        self.assertEqual([combo.itemData(i) for i in range(combo.count)], [None, "M", "F", "O", "X"], "the combo offers the declared levels")
        self.assertEqual(combo.itemText(4), "non-binary")
        self.assertTrue(section.setField("sex", "X"))
        self.assertEqual(session.study.subject("sub-01")["sex"], "X")
        section.refresh("sub-01")
        self.assertEqual(str(section.warningLabel.text), "", "a level the study declares is no warning")
        self.assertEqual(combo.currentData, "X")
        # Other studies keep the registry's levels, and an undeclared value still warns
        session.study.set("sub-02", {"sex": "Z"})
        section.refresh("sub-02")
        self.assertIn("Unknown Sex value 'Z'", str(section.warningLabel.text))
        # The warning names the field as the editor does, never the column code
        session.study.set("sub-02", {"onset_to_imaging_days": "2,5"})
        section.refresh("sub-02")
        text = str(section.warningLabel.text)
        self.assertIn("Onset to imaging: '2,5' is not a number", text)
        self.assertNotIn("onset_to_imaging_days", text)
        # A strict column keeps the registry's levels and refuses others
        dimensionality = section.editors["dimensionality"]
        self.assertEqual([dimensionality.itemData(i) for i in range(dimensionality.count)], [None, "2D", "3D", "multi2D"])
        self.assertFalse(section.setField("dimensionality", "mixed"))
        self.assertEqual(session.study.subject("sub-02")["dimensionality"], "3D")

    def test_57_cohort_card_names_left_out_lesions_in_plain_words(self):
        """Cohort diagnostics said "12 not used: review_status != approved (12)" (the engine's reason strings)."""
        from StudioLib import libcard_cohort

        study = libcard_cohort._lnm("study")
        text = libcard_cohort.excludedText(
            {
                study.EXCLUDED_REVIEW: ["a"] * 12,
                study.EXCLUDED_INCLUDE: ["b"],
                study.EXCLUDED_QC: ["c"],
                study.EXCLUDED_NORM_QC: ["d"],
                "lesion file not found": ["e", "f"],
            },
        )
        self.assertEqual(text, "17 not used: not approved in review (12), excluded (1), QC fail (1), normalization QC not passed (1), lesion file not found (2)")
        for code in ("include=0", "qc=fail", "review_status", "norm_qc", "!="):
            self.assertNotIn(code, text)
        self.assertEqual(libcard_cohort.excludedText({}), "")
        # The engine keeps the raw strings as its keys
        self.assertEqual((study.EXCLUDED_INCLUDE, study.EXCLUDED_QC), ("include=0", "qc=fail"))

    def test_59_stopping_the_command_line_check_keeps_the_previous_result(self):
        """Stop on a Cohort diagnostics check run by the command line said "The check failed" and dropped the result (BB-1)."""
        from StudioLib import libcard_cohort, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([(f"sub-0{index}", {"group": "case" if index <= 3 else "control"}, False) for index in range(1, 6)])
        card = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            card = libcard_cohort.CohortCard(session)
            card.refresh()
            previous = libcard_cohort.checkLesionSet(session.study.path)
            self.assertIsNotNone(previous.get("summary"))
            jobrunner = libcard_cohort._jobrunner()

            class FakeJob:
                def __init__(self, state):
                    self.state = state

                def succeeded(self):
                    return False

            for state, stopped in ((jobrunner.CANCELLED, True), (jobrunner.FAILED, False)):
                card.result = previous
                card._runPath = card.studyPath
                card._onCliDone(FakeJob(state))
                text = str(card.statusLabel.text)
                if stopped:
                    self.assertEqual(text, "Stopped.")
                    self.assertIs(card.result, previous, "a stopped check dropped the previous result")
                else:
                    self.assertIn("The check failed", text)
                    self.assertIsNone(card.result)
        finally:
            if card is not None:
                card.dispose()
                card.widget.deleteLater()
            study_ui.closeStudy()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_60_case_section_warns_about_a_doi_that_is_not_a_doi(self):
        """The Case section stored "see the paper" as the DOI without a word, while the case strip of Draw warned about it (BB-1)."""
        from StudioLib import libcard_columns as lc
        from StudioLib import study_ui

        study = self._study([("sub-01", {}, False)])
        session = study_ui.openStudy(study.path, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        slicer.app.processEvents()
        host = qt.QWidget()
        self.addCleanup(host.deleteLater)
        hostLayout = qt.QVBoxLayout(host)
        sectionWidget = lc._createSection(session)
        section = lc._sections[sectionWidget]
        hostLayout.addWidget(sectionWidget)
        host.resize(306, 700)
        host.show()
        section.refresh("sub-01")
        for key in ("imaging", "case"):
            button = section.groups[key][0]
            if not button.isChecked():
                button.click()
        slicer.app.processEvents()
        self.assertTrue(section.setField("doi", "see the paper"))
        self.assertEqual(session.study.subject("sub-01")["doi"], "see the paper", "the text is kept, as in the case strip after a second Enter")
        section.refresh("sub-01")
        self.assertIn("see the paper does not look like a DOI", str(section.warningLabel.text))
        self.assertFalse(section.warningLabel.isHidden())
        for good in ("10.1000/xyz", "https://doi.org/10.1000/abc", "doi:10.1000/def"):
            self.assertTrue(section.setField("doi", good))
            section.refresh("sub-01")
            self.assertEqual(str(section.warningLabel.text), "", f"{good!r} is a DOI")
            self.assertTrue(section.warningLabel.isHidden())

    def test_61_map_tab_order_mode_switch_before_cards_and_button_before_disclaimer(self):
        """Tab reaches the Lesion / Cohort / Coords / Stim switch right before the Seed card, and the primary button
        "Map network" before the research-use line under it (BB-1).

        The keyboard focus chain of Qt follows the creation order of the widgets: the switch is made after the cards and put
        above the Seed card, so Tab reached it after the last card (Export); the primary button is made in the Compute card
        and moved to the footer later, so Tab reached it after the research-use line.
        """
        from StudioLib import shell

        mainWindow = slicer.util.mainWindow()
        if mainWindow is None or not hasattr(slicer.modules, "lesionnetworkmapping"):
            self.skipTest("needs the main window and the Map module")
        savedGeometry = mainWindow.saveGeometry()
        studioShell = None
        tab = int(qt.Qt.TabFocus)

        def pump(count=10):
            for _ in range(count):
                slicer.app.processEvents()

        def stops(first):
            """The Tab stops of the window, from ``first`` round the focus chain."""
            result, widget = [], first
            for _ in range(2000):
                if int(widget.focusPolicy) & tab:
                    result.append(widget)
                widget = widget.nextInFocusChain()
                if widget == first:
                    break
            return result

        def index(chain, widget):
            return next(position for position, stop in enumerate(chain) if stop == widget)

        try:
            mainWindow.resize(1366, 768)
            studioShell = shell.Shell(force=True)
            studioShell.apply()
            studioShell.switchTo("map")
            pump()
            widget = slicer.modules.LesionNetworkMappingWidget
            seedCard = widget._cardWidgets["seed"][1]
            buttons = list(widget.seedModeGroup.buttons())
            self.assertGreater(len(buttons), 1)
            for _round in range(2):
                chain = stops(seedCard)
                positions = [index(chain, button) for button in buttons]
                self.assertEqual(positions, list(range(len(chain) - len(buttons), len(chain))),
                                 "the mode buttons are the stops right before the Seed card")
                # The Seed card is first in the chain here; the cards after it are the ones that the switch must come before
                for key in ("connectome", "compute", "export"):
                    card = widget._cardWidgets[key][1]
                    self.assertLess(index(chain, seedCard), index(chain, card), f"{key} card after the Seed card")
                chain = stops(buttons[0])
                for key in ("seed", "connectome", "compute", "export"):
                    card = widget._cardWidgets[key][1]
                    self.assertLess(0, index(chain, card), key)
                    self.assertLess(index(chain, buttons[-1]), index(chain, card), f"the mode switch comes before the {key} card")
                # Leaving and entering Map again (the primary button goes back to the Compute card and to the footer)
                studioShell.switchTo("draw")
                pump()
                studioShell.switchTo("map")
                pump()
            chain = stops(widget.computeButton)
            disclaimer = [child for child in widget.shellDisclaimer.findChildren("QWidget") if int(child.focusPolicy) & tab]
            self.assertTrue(disclaimer, "the research-use line has a Tab stop (its Read more link)")
            self.assertEqual(chain[0], widget.computeButton)
            self.assertEqual(chain[1], disclaimer[0], "the research-use line comes right after the primary button")
        finally:
            if studioShell is not None:
                studioShell.dispose()
            pump()
            mainWindow.restoreGeometry(savedGeometry)
            slicer.mrmlScene.Clear(0)

    def test_62_docs_name_the_connectome_buttons_as_the_card_shows_them(self):
        """With no connectome registered the Connectome card shows Get a connectome…, a ⋯ menu and Use the demo, and no
        Manage… button (that one appears with a registered connectome), so the documents never send a user without a
        connectome to Manage… > Get a connectome… (BB-1).
        """
        import re

        from LesionNetworkMappingUI import widgets as lnmwidgets  # noqa: F401  (the module is there)

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("Docs/user_guide/modules/lesionnetworkmapping.md", "Docs/user_guide/modules/lnm_data.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        # The behaviour the docs describe: the empty state has the buttons below and no Manage… button
        if hasattr(slicer.modules, "LesionNetworkMappingWidget"):
            widget = slicer.modules.LesionNetworkMappingWidget
            if getattr(widget, "connectomeCombo", None) is not None and widget.connectomeCombo.count == 0:
                self.assertTrue(widget.manageButton.isHidden(), "no Manage… button without a connectome")
                self.assertFalse(widget.connectomeEmpty.isHidden())
                self.assertEqual(widget.getConnectomeButton.text, "Get a connectome…")
                self.assertEqual(widget.demoButton.text, "Use the demo")
        for name, text in texts.items():
            # Every "Manage… > Get a connectome…" is said to be the way with a registered connectome
            for found in re.finditer(r"Manage… > Get a connectome…", text):
                around = text[max(0, found.start() - 120):found.end() + 120]
                self.assertTrue(re.search(r"already registered|once a connectome is registered|with a connectome", around, re.IGNORECASE),
                                f"{name} sends a user without a connectome to Manage… > Get a connectome…: {around}")
            self.assertNotIn("Map > Connectome > Manage… > Get a connectome…", text, name)
        lnm, data = (texts[name] for name in names)
        self.assertIn("the card shows Get a connectome…", lnm)
        self.assertIn("Use the demo", lnm)
        self.assertIn("Map > Connectome > Get a connectome…", data)

    def test_63_analysis_card_state_line_is_elided_not_cut_off(self):
        """The state line of the Analysis card is one line in a card of a fixed height: a long state ("Cohort analysis failed
        (open Map for the details)") is elided with "…" to the width of the card (200 px here), the whole text is the tooltip, and it is
        elided again after a resize and after a larger application font (it was cut off mid-word without an ellipsis) (BB-1).
        """
        from StudioLib import libcard_analysis, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        long = ("Cohort analysis failed (open Map for the details)", "Cohort analysis: up to date · finished 61 days ago")
        state = {"text": long[0]}
        original = libcard_analysis.stateLine
        self.addCleanup(setattr, libcard_analysis, "stateLine", original)
        libcard_analysis.stateLine = lambda *args, **kwargs: (state["text"], "text2")
        narrow = 200
        card = libcard_analysis.AnalysisCard(None)
        self.addCleanup(card.dispose)
        self.addCleanup(card.widget.delete)
        card.widget.resize(narrow, libcard_analysis.CARD_HEIGHT)
        card.widget.show()
        slicer.app.processEvents()
        line = card.line

        def check(text, expectElided):
            card.show()
            slicer.app.processEvents()
            shownText = str(line.text)
            width = line.width
            self.assertEqual(card.lineText(), text)
            self.assertLessEqual(qt.QFontMetrics(line.font).horizontalAdvance(shownText), width, (shownText, width))
            if expectElided:
                self.assertTrue(shownText.endswith("…"), shownText)
                self.assertIn(text, str(line.toolTip))
            else:
                self.assertEqual(shownText, text)

        for text in long:
            state["text"] = text
            card.widget.resize(narrow, libcard_analysis.CARD_HEIGHT)
            slicer.app.processEvents()
            check(text, qt.QFontMetrics(line.font).horizontalAdvance(text) > line.width)
            self.assertTrue(str(line.text).endswith("…"), "a state longer than the narrow card is elided")
            # A wider card shows more of it, a wide enough one shows all of it
            card.widget.resize(520, libcard_analysis.CARD_HEIGHT)
            slicer.app.processEvents()
            check(text, False)
            # A larger application font: the line is elided again to the same width
            card.widget.resize(narrow, libcard_analysis.CARD_HEIGHT)
            slicer.app.processEvents()
            font = qt.QFont(line.font)
            font.setPixelSize(int(font.pixelSize()) + 5 if font.pixelSize() > 0 else 17)
            line.setFont(font)
            slicer.app.processEvents()
            check(text, True)

    def test_64_docs_put_the_save_status_on_the_left_of_the_top_bar(self):
        """studio.md and user_interface.md say that the save status sits on the left of the top bar, after the breadcrumb, as
        the top bar builds it, and call it the save status, not "the autosave status" on the right (VISUAL 4.1).
        """
        from StudioLib import topbar

        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("Docs/user_guide/modules/studio.md", "Docs/user_guide/user_interface.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        source = inspect.getsource(topbar)
        self.assertIn("self._leftLayout.addWidget(self.saveStatusButton)", source)
        self.assertNotIn("rightLayout.addWidget(self.saveStatusButton)", source)
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
            self.assertNotIn("the autosave status", texts[name], f"{name} calls the top bar's save status the autosave status")
        studio = texts[names[0]]
        left, right = studio.split("On the left", 1)[1].split("In the centre", 1)[0], studio.split("On the right", 1)[1].split("an error badge", 1)[0]
        self.assertIn("save status", left, "studio.md does not put the save status on the left of the top bar")
        self.assertIn("Not saved · Retry", left)
        self.assertNotIn("save status", right, "studio.md still lists the save status on the right of the top bar")
        interface = texts[names[1]]
        self.assertIn("the breadcrumb with the save status after it", interface)

    def test_65_qc_chip_text_shows_every_line(self):
        """The text of a QC chip (Draw's Lesion card) is as high as its word-wrapped lines need at its width: the chip row
        had a Maximum vertical size policy, which capped it at its size hint (computed for another width than the one it
        gets), and the label lost its height-for-width flag to setSizePolicy(), so the last line of a long text ("... 16% (approx.: the atlas is in another template family)") was cut off (BB-1).
        """
        from StudioLib import lesionqc

        class Owner:
            def onAction(self, identifier):
                pass

        texts = (
            ("Traced: L cerebral cortex 78%, L planum temporale 18%, L superior temporal gyrus, posterior division 16% "
             "(approx.: the atlas is in another template family)"),
            "Traced: L cerebral cortex 78%, L planum temporale 18%, L superior temporal gyrus, posterior division 16%",
            "Traced: L cerebral cortex 78%",
        )
        host = qt.QWidget()
        panel = lesionqc._QcPanel(Owner())
        try:
            hostLayout = qt.QVBoxLayout(host)
            hostLayout.setContentsMargins(0, 0, 0, 0)
            hostLayout.addWidget(panel.widget)
            hostLayout.addStretch(1)
            host.show()
            for width in (300, 260, 420):
                host.resize(width, 400)
                for text in texts:
                    panel.setChips([{"key": "location", "level": "neutral", "badge": "Where", "text": text, "tooltip": "", "actions": []}])
                    for _ in range(6):
                        slicer.app.processEvents()
                    label = next(w for w in panel.chips["location"].findChildren("QLabel") if w.objectName == "StudioQcText")
                    self.assertTrue(label.hasHeightForWidth(), "the chip text keeps its height-for-width flag")
                    needed = label.heightForWidth(label.width)
                    self.assertGreaterEqual(label.height, needed, f"width {width}: {label.height} px for a text that needs {needed} px: {text[:30]}")
        finally:
            panel.clear()
            host.deleteLater()

    def test_66_cxx_extensions_of_the_official_build_are_said_incompatible(self):
        """The fork asks the extensions server for the extensions of official 3D Slicer, so an extension with compiled (C++)
        modules installed but never loaded: they need official Slicer's own libraries. It is said to be incompatible, in
        plain words, and kept disabled (its folders are not added to the module or launcher paths), at install and when
        an extension installed earlier is found at start; an extension written in Python stays compatible and enabled (BB-1). The Manage list shows no pending-restart status on the
        rows of the disabled ones.
        """
        import tarfile

        serverRevision = slicer.app.extensionsManagerModel().slicerRevision
        if serverRevision == slicer.app.revision:
            self.skipTest("this build asks the extensions server for its own extensions (an official-like build)")
        libDir = f"lib/Slicer-{slicer.app.majorVersion}.{slicer.app.minorVersion}"
        installPath = os.path.join(self.directory, "Extensions")
        os.makedirs(installPath)
        settingsPath = os.path.join(self.directory, "extensions.ini")
        settings = qt.QSettings(settingsPath, qt.QSettings.IniFormat)
        settings.setValue("Extensions/InstallPath", installPath)
        settings.sync()

        def newModel():
            """A model like the application's, on its own settings file and extensions folder."""
            model = slicer.qSlicerExtensionsManagerModel()
            model.interactive = False
            model.extensionsSettingsFilePath = settingsPath
            model.slicerOs, model.slicerArch, model.slicerRevision = "linux", "amd64", serverRevision
            return model

        def write(path, content, mode=0o644):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(content)
            os.chmod(path, mode)

        def package(name, files):
            """An extension package as the server has it: <top folder>/<files>, with share/.../<name>.s4ext."""
            top = f"{serverRevision}-linux-amd64-{name}-git0123456-2026-10-16"
            source = os.path.join(self.directory, "packages", top)
            files = dict(files, **{f"share/Slicer-{slicer.app.majorVersion}.{slicer.app.minorVersion}/{name}.s4ext": (b"scm git\n", 0o644)})
            for relative, (content, mode) in files.items():
                write(os.path.join(source, *relative.split("/")), content, mode)
            path = source + ".tar.gz"
            with tarfile.open(path, "w:gz") as tar:
                tar.add(source, arcname=top)
            return path

        def settingsText():
            with open(settingsPath, encoding="utf-8") as stream:
                return stream.read()

        def assertCxxIncompatible(model, name, fileName, when):
            self.assertTrue(model.isExtensionInstalled(name), f"{name} {when}")
            reasons = list(model.isExtensionCompatible(name))
            self.assertEqual(len(reasons), 1, f"{name} {when}: {reasons}")
            self.assertIn("compiled (C++) modules, such as " + fileName, reasons[0])
            self.assertIn("written in Python", reasons[0])
            self.assertFalse(model.isExtensionEnabled(name), f"{name} {when} is enabled: its modules would be loaded")

        def assertPythonCompatible(model, when):
            self.assertEqual(list(model.isExtensionCompatible("StudioPythonProbe")), [], when)
            self.assertTrue(model.isExtensionEnabled("StudioPythonProbe"), when)

        cxx = package("StudioCxxProbe", {f"{libDir}/qt-loadable-modules/libqSlicerStudioCxxProbeModule.so": (b"\x7fELF\x02\x01\x01\x00", 0o755)})
        python = package("StudioPythonProbe", {
            f"{libDir}/qt-scripted-modules/StudioPythonProbe.py": (b"# a scripted module\n", 0o644),
            f"{libDir}/cli-modules/StudioPythonCli.py": (b"#!/usr/bin/env python-real\n", 0o755),  # a scripted CLI
            f"{libDir}/cli-modules/StudioPythonCli.xml": (b"<executable/>\n", 0o644),
        })
        model = newModel()
        self.assertTrue(model.newExtensionEnabledByDefault)
        self.assertTrue(model.installExtension(cxx, False), "the C++ extension package was not installed")
        self.assertTrue(model.installExtension(python, False), "the Python extension package was not installed")
        assertCxxIncompatible(model, "StudioCxxProbe", "libqSlicerStudioCxxProbeModule.so", "after install")
        assertPythonCompatible(model, "after install")
        # Disabled at once: no folder of the C++ extension in the module paths or the launcher's library paths
        self.assertNotIn("StudioCxxProbe", settingsText())
        self.assertIn("StudioPythonProbe", settingsText())

        # At start, an extension installed earlier (enabled) with a compiled CLI module (an executable without suffix) is
        # found incompatible and disabled too; the description file keeps it disabled.
        write(os.path.join(installPath, "StudioCliProbe", *libDir.split("/"), "cli-modules", "StudioCliProbe"), b"\x7fELF\x02\x01\x01\x00", 0o755)
        with open(os.path.join(installPath, "StudioCliProbe.s4ext"), "w", encoding="utf-8") as stream:
            stream.write(f"installed true\nenabled true\nslicer_revision {serverRevision}\nos linux\narch amd64\n")
        errorLog = slicer.app.errorLogModel()

        def logRows(text):
            """The levels of the error log rows that contain text (the log groups consecutive messages of a level in a
            row and shortens long ones: the start of the first message of a row is what is looked for).
            """
            return [errorLog.index(row, 2).data() for row in range(errorLog.rowCount()) if text in (errorLog.index(row, 4).data() or "")]

        started = newModel()
        started.updateModel()
        assertCxxIncompatible(started, "StudioCliProbe", "StudioCliProbe", "at start")
        assertCxxIncompatible(started, "StudioCxxProbe", "libqSlicerStudioCxxProbeModule.so", "at start")
        # ... and said as a warning, not an error: it is kept disabled by design and was announced at install, so it must
        # not light the error badge of the top bar at every start (BB-1)
        for _ in range(100):  # the log reads the messages of the application from its own thread
            slicer.app.processEvents()
            if logRows("Extension StudioCliProbe is incompatible"):
                break
            time.sleep(0.05)
        self.assertEqual(logRows("Extension StudioCliProbe is incompatible"), ["Warning"])
        self.assertEqual(logRows("is incompatible"), ["Warning"], "an extension kept disabled for its compiled modules is not an error")
        assertPythonCompatible(started, "at start")
        with open(os.path.join(installPath, "StudioCliProbe.s4ext"), encoding="utf-8") as stream:
            self.assertIn("enabled false", stream.read())

        # The Manage list does not say a restart is pending for an extension that is kept disabled: restarting would not load
        # it. An extension installed and enabled but not loaded yet (written in Python) does say so (BB-1).
        listWidget = slicer.qSlicerExtensionsLocalWidget()
        self.addCleanup(listWidget.deleteLater)
        listWidget.setExtensionsManagerModel(started)
        listWidget.refresh()
        statuses = {}
        for row in range(listWidget.count):
            item = listWidget.item(row)
            rowWidget = listWidget.itemWidget(item)
            if rowWidget is None:
                continue
            label = rowWidget.findChild(qt.QLabel, "StatusLabel")
            statuses[item.data(qt.Qt.UserRole)] = (label.text if label is not None and not label.isHidden() else "")
        self.assertEqual(statuses.get("StudioPythonProbe"), "Install pending restart", str(statuses))
        for name in ("StudioCxxProbe", "StudioCliProbe"):
            self.assertIn(name, statuses, str(statuses))
            self.assertEqual(statuses[name], "", f"{name} is incompatible and disabled, yet its row says a restart is pending")

    def test_67_save_lesion_is_off_in_second_tracing_adjudication_and_review(self):
        """Save lesion (Ctrl+Shift+S, the Lesion card ⋯ menu, Search) in a second tracing, an adjudication or a review
        proposed the scan's folder and BIDS subject: in a native study laid out as sub-01_T1w + sub-01_lesion, the first
        tracer's file. Accepting it overwrote the primary tracing with the blind second rater's strokes, with the A/B label
        map of the adjudication, or with the reviewer's edit (and the review then kept nothing). These states write their
        own files with their footer (Ctrl+Enter), so Save lesion and Save lesion as… are off there (BB-1).
        """
        from LesionNetworkMappingLib import agreement, nifti
        from LesionNetworkMappingLib import review as reviewRules

        from StudioLib import adjudicate, draw, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        # A native grid (not an MNI one): the scans and the first tracings side by side, as a scanner export names them
        shape = (40, 48, 36)
        affine = np.array([[2.0, 0, 0, -40], [0, 2.0, 0, -48], [0, 0, 2.0, -36], [0, 0, 0, 1]])
        folder = os.path.join(self.directory, "Rater study")
        os.makedirs(folder)
        files = []
        for sid, offset in (("sub-01", 10), ("sub-02", 20)):
            files.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(files[-1], np.full(shape, 100, np.int16), affine, sform_code=1, qform_code=1)
            lesion = np.zeros(shape, np.uint8)
            lesion[offset:offset + 6, 20:26, 15:21] = 1
            files.append(os.path.join(folder, f"{sid}_lesion.nii.gz"))
            nifti.write_nifti(files[-1], lesion, affine, sform_code=1, qform_code=1)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: None
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        # The user accepts whatever the Save dialog proposes
        dialogs = []
        realGetSaveFileName = qt.QFileDialog.getSaveFileName

        def acceptProposal(parent, caption, proposal, *args):
            dialogs.append((str(caption), str(proposal)))
            return proposal

        qt.QFileDialog.getSaveFileName = acceptProposal
        self.addCleanup(setattr, qt.QFileDialog, "getSaveFileName", realGetSaveFileName)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        toasts = []
        originalShellToast = studioShell.toast
        studioShell.toast = lambda text, *args, **kwargs: (toasts.append(str(text)), originalShellToast(text, *args, **kwargs))[1]

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def contents(path):
            with open(path, "rb") as f:
                return f.read()

        def menuLabels():
            return [entry[0] for entry in draw.lesionMenuActions(studioShell) if entry]

        def paletteEnabled():
            entry = next(entry for entry in draw._paletteEntries(studioShell) if entry["id"] == "draw.saveLesionAs")
            return bool(entry["enabled"]())

        def assertSaveLesionOff(when, primaries):
            self.assertIsNotNone(draw.lesionNodes()[0], when)
            toasts.clear()
            self.assertIsNone(draw.saveLesionNow(studioShell), f"{when}: Ctrl+Shift+S saved the lesion")
            self.assertIsNone(draw.saveLesionDialog(studioShell), f"{when}: Save lesion as… saved the lesion")
            pump()
            for path, before in primaries.items():
                self.assertEqual(contents(path), before, f"{when}: {os.path.basename(path)} (a first tracing) was overwritten")
            self.assertEqual(dialogs, [], f"{when}: the Save dialog was offered")
            self.assertTrue(toasts and all("Save lesion is off" in text for text in toasts), f"{when}: {toasts}")
            labels = menuLabels()
            self.assertNotIn("Save lesion", labels, when)
            self.assertNotIn("Save lesion as…", labels, when)
            self.assertFalse(paletteEnabled(), f"{when}: Search offers Save lesion as…")

        try:
            review.setRaterName("Ana Bell")
            session = study_ui.createStudy(files, studioShell, folder=folder)
            self.assertIsNotNone(session)
            pump()
            studyObject = session.study
            for sid in ("sub-01", "sub-02"):
                session.setValues([sid], {"status": "traced", "tracer": "Ana Bell"})
            session.flush()
            primaries = {}
            for sid in ("sub-01", "sub-02"):
                path = studyObject.abspath(studyObject.subject(sid)["lesion"])
                self.assertEqual(path, os.path.join(folder, f"{sid}_lesion.nii.gz"))
                primaries[path] = contents(path)

            # Blind second tracing: a stroke, then Save lesion
            agreement.assign(studyObject, sids=["sub-01"], rater="Carl Diaz", who="Dana PI")
            session.flush()
            review.setRaterName("Carl Diaz")
            state = adjudicate.startSecondTracing(sid="sub-01", shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            segmentationNode = state.segmentationNode()
            volumeNode = slicer.mrmlScene.GetNodeByID(state._nodeIds[0])
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[5:8, 5:8, 5:8] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()
            assertSaveLesionOff("second tracing", primaries)
            adjudicate.exitState()
            pump()

            # Adjudication: the two tracings of sub-02 do not overlap
            review.setRaterName("Ana Bell")
            agreement.assign(studyObject, sids=["sub-02"], rater="Carl Diaz", who="Dana PI")
            primary, primaryAffine, _header = nifti.read_nifti(os.path.join(folder, "sub-02_lesion.nii.gz"))
            second = np.roll(primary, 8, axis=0)
            agreement.save_second_tracing(studyObject, "sub-02", np.ascontiguousarray(second.transpose(2, 1, 0)), primaryAffine,
                                          os.path.join(folder, "sub-02_lesion.nii.gz"), "Carl Diaz")
            session.setValues([], {})
            session.flush()
            review.setRaterName("Dana PI")
            state = adjudicate.startAdjudication(sid="sub-02", shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            self.assertIsNotNone(adjudicate.state(), "the adjudication did not start")
            assertSaveLesionOff("adjudication", primaries)
            adjudicate.exitState()
            pump()

            # Review: Save lesion would replace the tracer's file with the reviewer's edit
            for sid in ("sub-01", "sub-02"):
                reviewRules.apply_transition(studyObject, sid, "pending", "Ana Bell")
            session.setValues([], {})
            session.flush()
            review.setRaterName("Rev Iewer")
            review.startReview(sids=["sub-01"], shell=studioShell)
            pump()
            self.assertIsNotNone(review.state(), "the review did not start")
            assertSaveLesionOff("review", primaries)
            review.exitReview()
            pump()

            # Out of these states, Save lesion is back
            self.assertEqual(draw.saveLesionBlockedReason(), "")
            self.assertIn("Save lesion as…", menuLabels())
        finally:
            if adjudicate.state() is not None:
                adjudicate.exitState()
            if review.state() is not None:
                review.exitReview()
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_70_adjudication_tracings_are_read_only_until_edit(self):
        """The adjudication opened with the Brush armed on tracing A: a stroke changed A on screen (the Lesion card said
        "2 parts", "Not saved yet"), and choosing A or Union wrote the tracings on disk without it and without a word. The
        tracings are read-only until Edit…, whose Consensus segment is the one to edit (BB-1).
        """
        from LesionNetworkMappingLib import agreement, nifti

        from StudioLib import adjudicate, draw, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        shape = (40, 48, 36)
        affine = np.array([[2.0, 0, 0, -40], [0, 2.0, 0, -48], [0, 0, 2.0, -36], [0, 0, 0, 1]])
        folder = os.path.join(self.directory, "Adjudication study")
        os.makedirs(folder)
        files = []
        for sid in ("sub-01", "sub-02"):
            files.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(files[-1], np.full(shape, 100, np.int16), affine, sform_code=1, qform_code=1)
            lesion = np.zeros(shape, np.uint8)
            lesion[10:16, 20:26, 15:21] = 1
            files.append(os.path.join(folder, f"{sid}_lesion.nii.gz"))
            nifti.write_nifti(files[-1], lesion, affine, sform_code=1, qform_code=1)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: None
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        toasts = []
        originalShellToast = studioShell.toast
        studioShell.toast = lambda text, *args, **kwargs: (toasts.append(str(text)), originalShellToast(text, *args, **kwargs))[1]

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def voxels(path):
            return int(np.count_nonzero(nifti.read_nifti(path)[0]))

        def effectName(editor):
            effect = editor.activeEffect()
            return effect.name if effect is not None else None

        try:
            review.setRaterName("Ana Bell")
            session = study_ui.createStudy(files, studioShell, folder=folder)
            self.assertIsNotNone(session)
            pump()
            studyObject = session.study
            for sid in ("sub-01", "sub-02"):
                session.setValues([sid], {"status": "traced", "tracer": "Ana Bell"})
            session.flush()
            agreement.assign(studyObject, sids=["sub-01", "sub-02"], rater="Carl Diaz", who="Dana PI")
            for sid in ("sub-01", "sub-02"):
                primaryPath = os.path.join(folder, f"{sid}_lesion.nii.gz")
                primary, primaryAffine, _header = nifti.read_nifti(primaryPath)
                agreement.save_second_tracing(studyObject, sid, np.ascontiguousarray(np.roll(primary, 4, axis=0).transpose(2, 1, 0)),
                                              primaryAffine, primaryPath, "Carl Diaz")
            session.setValues([], {})
            session.flush()
            primaryPath = os.path.join(folder, "sub-01_lesion.nii.gz")
            primaryBefore = voxels(primaryPath)
            editor = draw.segmentEditor()
            widget = slicer.modules.SegmentEditorWidget
            review.setRaterName("Dana PI")
            state = adjudicate.startAdjudication(sids=["sub-01", "sub-02"], shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            self.assertIsNotNone(adjudicate.state(), "the adjudication did not start")
            node = state.segmentationNode()
            segmentA = state.segmentIds["A"]
            voxelsA = int(adjudicate._segmentMask(node, segmentA)[0].sum())

            # Before Edit…: no tool, and neither the tool keys nor Draw's arming nor the card fixes change A
            self.assertIs(editor.segmentationNode(), node)
            self.assertIsNone(effectName(editor), "a tool is armed on the tracings before Edit…")
            self.assertTrue(editor.readOnly, "the tracings can be edited before Edit…")
            widget._onMnemonic("Paint")
            draw.session(studioShell).requestArm()
            pump()
            self.assertIsNone(effectName(editor), "P armed the Brush on the tracings before Edit…")
            editor.setCurrentSegmentID(segmentA)
            toasts.clear()
            self.assertIsNone(draw.keepLargestPart(studioShell), "Keep largest edited tracing A")
            self.assertTrue(toasts and "read-only" in toasts[-1], toasts)
            self.assertEqual(int(adjudicate._segmentMask(node, segmentA)[0].sum()), voxelsA)
            self.assertIn("read-only", draw.extension()._hint(draw.session(studioShell), editor, None))

            # Edit…: the Consensus segment is edited with the Brush; Cancel edit: read-only again
            state.choose("edit")
            pump()
            self.assertFalse(editor.readOnly, "Edit… left the consensus read-only")
            self.assertEqual(editor.currentSegmentID(), state.segmentIds["consensus"])
            widget._onMnemonic("Paint")
            pump()
            self.assertEqual(effectName(editor), "Paint", "the Brush cannot be picked to edit the consensus")
            state.cancelEdit()
            pump()
            self.assertTrue(editor.readOnly, "Cancel edit left the tracings editable")
            self.assertIsNone(effectName(editor), "Cancel edit left a tool armed on the tracings")

            # A: the tracing on disk is the consensus, as shown; the next pair opens read-only too
            self.assertTrue(state.choose("A"))
            pump()
            self.assertEqual(voxels(primaryPath), primaryBefore)
            self.assertEqual(primaryBefore, voxelsA)
            self.assertEqual(state.current, "sub-02")
            self.assertTrue(editor.readOnly)
            self.assertIsNone(effectName(editor))

            # Leaving the adjudication leaves an editable editor
            adjudicate.exitState()
            pump()
            self.assertFalse(editor.readOnly, "the Segment Editor stayed read-only after the adjudication")
        finally:
            if adjudicate.state() is not None:
                adjudicate.exitState()
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_71_adjudication_edit_survives_escape_and_undo(self):
        """Esc during Edit… of an adjudication removed the edited Consensus segment without a word (it was registered as an
        Esc overlay); Ctrl+Z then brought back an untracked "Consensus", Edit… added a second one from the union, and Save
        consensus wrote the plain union: every erasure was lost (BB-1).
        """
        from LesionNetworkMappingLib import agreement, nifti

        from StudioLib import adjudicate, draw, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        shape = (40, 48, 36)
        affine = np.array([[2.0, 0, 0, -40], [0, 2.0, 0, -48], [0, 0, 2.0, -36], [0, 0, 0, 1]])
        folder = os.path.join(self.directory, "Adjudication edit study")
        os.makedirs(folder)
        files = []
        for sid in ("sub-01", "sub-02"):
            files.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(files[-1], np.full(shape, 100, np.int16), affine, sform_code=1, qform_code=1)
            lesion = np.zeros(shape, np.uint8)
            lesion[10:16, 20:26, 15:21] = 1
            files.append(os.path.join(folder, f"{sid}_lesion.nii.gz"))
            nifti.write_nifti(files[-1], lesion, affine, sform_code=1, qform_code=1)
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: None
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: None
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def consensusSegments(node):
            segmentation = node.GetSegmentation()
            return [segmentation.GetNthSegmentID(index) for index in range(segmentation.GetNumberOfSegments())
                    if segmentation.GetNthSegment(index).GetName() == "Consensus"]

        def count(node, segmentId):
            mask = adjudicate._segmentMask(node, segmentId)
            return 0 if mask is None else int(mask[0].sum())

        def erase(node, segmentId, editor):
            """One "stroke" of the Erase tool: the undo state first, as the effects save it, then fewer voxels."""
            editor.saveStateForUndo()
            reference = editor.sourceVolumeNode()
            array = slicer.util.arrayFromSegmentBinaryLabelmap(node, segmentId, reference).copy()
            kji = np.argwhere(array)
            array[tuple(kji[kji[:, 0] == kji[:, 0].min()].T)] = 0
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, node, segmentId, reference)

        try:
            review.setRaterName("Ana Bell")
            session = study_ui.createStudy(files, studioShell, folder=folder)
            self.assertIsNotNone(session)
            pump()
            studyObject = session.study
            for sid in ("sub-01", "sub-02"):
                session.setValues([sid], {"status": "traced", "tracer": "Ana Bell"})
            session.flush()
            agreement.assign(studyObject, sids=["sub-01", "sub-02"], rater="Carl Diaz", who="Dana PI")
            for sid in ("sub-01", "sub-02"):
                primaryPath = os.path.join(folder, f"{sid}_lesion.nii.gz")
                primary, primaryAffine, _header = nifti.read_nifti(primaryPath)
                agreement.save_second_tracing(studyObject, sid, np.ascontiguousarray(np.roll(primary, 4, axis=0).transpose(2, 1, 0)),
                                              primaryAffine, primaryPath, "Carl Diaz")
            session.setValues([], {})
            session.flush()
            editor = draw.segmentEditor()
            review.setRaterName("Dana PI")
            state = adjudicate.startAdjudication(sids=["sub-01", "sub-02"], shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            node = state.segmentationNode()

            # Edit…, two erasures, then Esc (twice: the tool, then nothing): the edit stays
            state.choose("edit")
            pump()
            consensus = state.segmentIds["consensus"]
            union = count(node, consensus)
            editor.setActiveEffectByName("Erase")
            erase(node, consensus, editor)
            erase(node, consensus, editor)
            edited = count(node, consensus)
            self.assertLess(edited, union)
            for _press in range(2):
                studioShell.handleEscape()
                pump()
                self.assertTrue(state.editing, "Esc left the consensus edit")
                self.assertEqual(consensusSegments(node), [consensus], "Esc removed the Consensus segment")
                self.assertEqual(count(node, consensus), edited, "Esc changed the edited consensus")

            # Cancel edit, then Ctrl+Z (undo the last erasure): Edit… resumes the restored Consensus, no second one
            state.cancelEdit()
            pump()
            self.assertEqual(consensusSegments(node), [])
            editor.undo()
            pump()
            restored = consensusSegments(node)
            self.assertEqual(len(restored), 1, "Ctrl+Z did not bring the Consensus back")
            restoredCount = count(node, restored[0])
            self.assertTrue(edited < restoredCount < union, (edited, restoredCount, union))
            state.choose("edit")
            pump()
            self.assertEqual(consensusSegments(node), restored, "Edit… added a second Consensus segment")
            self.assertEqual(state.segmentIds["consensus"], restored[0])
            self.assertEqual(count(node, restored[0]), restoredCount)

            # Save consensus writes the mask that is shown, not the union
            self.assertTrue(state.saveEdit())
            pump()
            saved = int(np.count_nonzero(nifti.read_nifti(os.path.join(folder, "sub-01_lesion.nii.gz"))[0]))
            self.assertEqual(saved, restoredCount, "the saved consensus is not the edited one")

            # The next subject's Edit… starts from its own union again
            self.assertEqual(state.current, "sub-02")
            state.choose("edit")
            pump()
            node = state.segmentationNode()
            self.assertEqual(len(consensusSegments(node)), 1)
            self.assertEqual(count(node, state.segmentIds["consensus"]), union)
        finally:
            if adjudicate.state() is not None:
                adjudicate.exitState()
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_74_blind_search_does_not_match_the_group(self):
        """With blind tracing on, typing "control" in "Search subjects" listed exactly the controls and "case" exactly the
        cases, although the Group column, the chips and the counts are hidden from the tracer (BB-1).
        """
        from LesionNetworkMappingLib import review as rules

        from StudioLib import library, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        cases = {"sub-01", "sub-03", "sub-05"}
        rows = [(f"sub-0{index}", {"group": "case" if f"sub-0{index}" in cases else "control", "notes": "left MCA"}, False)
                for index in range(1, 7)]
        study = self._study(rows)
        studyList = None

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def listed(text):
            studyList.search.setText(text)
            pump()
            model = studyList.model
            return {model.sid(row) for row in range(model.rowCount())}

        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            studyList = library.StudyList()
            studyList.widget.resize(900, 400)
            studyList.widget.show()
            studyList.refresh()
            pump()
            everyone = {f"sub-0{index}" for index in range(1, 7)}
            # Without blinding the search matches the group, the id and the notes
            self.assertEqual(listed("control"), everyone - cases)
            self.assertEqual(listed("case"), cases)
            self.assertIn("group", str(studyList.search.toolTip))

            # Blind tracing: the group is not searched, so neither word sorts the subjects into cases and controls
            rules.set_study_setting(session.study, "blind_tracing", True, "Tester")
            self.assertIn("group", library._hiddenColumns(session.study))
            studyList.refresh()
            pump()
            self.assertEqual(listed("control"), set(), "the search lists the controls while tracing blind")
            self.assertEqual(listed("contr"), set())
            self.assertEqual(listed("case"), set(), "the search lists the cases while tracing blind")
            self.assertEqual(listed("ase"), set())
            self.assertNotIn("group", str(studyList.search.toolTip), "the search tooltip offers the group while tracing blind")
            # The other columns are still searched
            self.assertEqual(listed("sub-03"), {"sub-03"})
            self.assertEqual(listed("mca"), everyone)

            # Blinding off: the search by group is back
            rules.set_study_setting(session.study, "blind_tracing", False, "Tester")
            studyList.refresh()
            self.assertEqual(listed("control"), everyone - cases)
            self.assertIn("group", str(studyList.search.toolTip))
        finally:
            if studyList is not None:
                studyList.dispose()
                studyList.widget.deleteLater()
            study_ui.closeStudy()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_75_keep_both_in_one_study_does_not_hide_the_offer_in_another(self):
        """Sources > Keep both remembered the pair of record ids for the whole session, and record ids (first author and year)
        repeat between studies: a second study whose near-duplicate records had the same ids was never offered for merging
        (BB-1). The decision now belongs to the study it was made in.
        """
        from LesionNetworkMappingLib import sources
        from LesionNetworkMappingLib.study import Study

        from StudioLib import libcard_sources, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")

        def make(name, title):
            path = os.path.join(self.directory, name)
            study = Study.create(path, name=name)
            records = sources.Records.open(study)
            records.add([{"title": title, "authors": "Smith J", "year": "2020"},
                         {"title": title + ".", "authors": "Smith J", "year": "2020"}])
            records.save()
            study.save()
            return path

        pathA = make("Keep A", "Epilepsy surgery outcome mapped to a hippocampal network")
        pathB = make("Keep B", "Tremor after pontine lesion localises to cerebellar circuit")
        card = None
        try:
            session = study_ui.openStudy(pathA, showLibrary=False)
            self.assertIsNotNone(session)
            card = libcard_sources.SourcesCard(session)
            card.widget.show()
            card.refresh()
            self.assertTrue(card._offers, "study A is offered its duplicate")
            card.keepOffer()
            self.assertEqual(card._offers, [])

            study_ui.closeStudy()
            self.assertIsNotNone(study_ui.openStudy(pathB, showLibrary=False))
            card.refresh()
            self.assertEqual(card.records.ids(), ["smith2020", "smith2020b"])
            self.assertTrue(card._offers, "study B (same record ids) is still offered its duplicate")

            # Back in study A the decision still holds
            study_ui.closeStudy()
            self.assertIsNotNone(study_ui.openStudy(pathA, showLibrary=False))
            card.refresh()
            self.assertEqual(card._offers, [])
        finally:
            if card is not None:
                card.widget.hide()
                card.widget.deleteLater()
            study_ui.closeStudy()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_76_tests_do_not_convert_a_qflags_property_read_with_int(self):
        """Bug qt6-tests-int-of-qflags-property-read: with Qt 6 a QFlags property read (``label.alignment``,
        ``readout.textInteractionFlags``, ``widget.property("textInteractionFlags")``) is an opaque object in PythonQt
        and int() raises TypeError (on Qt 5.15 the read is a plain int, and ``label.alignment()`` is not callable), so
        the tests read such a property through ``_flagsValue``.
        """
        import ast

        here = os.path.dirname(os.path.abspath(__file__))
        modules = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        paths = [
            os.path.join(here, "StudioAccessibilityTest.py"),
            os.path.join(here, "StudioRegressionTest.py"),
            os.path.join(modules, "LesionNetworkMapping", "Testing", "Python", "LesionNetworkMappingDisclaimerFocusTest.py"),
        ]
        flagProperties = {"alignment", "textInteractionFlags", "windowFlags", "inputMethodHints", "orientations"}
        offenders = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "int" and node.args):
                    continue
                for sub in ast.walk(node.args[0]):
                    attr = isinstance(sub, ast.Attribute) and sub.attr in flagProperties and isinstance(sub.value, ast.Name) and sub.value.id != "qt"
                    prop = (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "property"
                        and sub.args
                        and isinstance(sub.args[0], ast.Constant)
                        and sub.args[0].value in flagProperties
                    )
                    if attr or prop:
                        offenders.append(f"{os.path.basename(path)}:{node.lineno}")
        self.assertEqual(offenders, [], "int() of a QFlags property read raises TypeError on Qt 6")

    def test_77_flags_value_reads_plain_and_opaque_flags(self):
        class _Opaque:
            value = 132

        class _Unreadable:
            pass

        self.assertEqual(_flagsValue(132), 132)
        self.assertEqual(_flagsValue(_Opaque()), 132)
        self.assertIsNone(_flagsValue(_Unreadable()))
        self.assertIsNone(_flagsValue(None))

    def test_78_case_facts_line_uses_the_words_of_the_library(self):
        """The Case section's facts line showed the stored codes ("Review: changes", "Flags: side_mismatch;csf;multipart") while the
        list and the Review section show words (BB-1). It reads "Review: Changes requested" and "Flags: Side, CSF, Parts" now, with the
        sentences of the flags in the tooltip.
        """
        from StudioLib import libcard_columns, libcard_flags, lesionqc, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        stored = lesionqc.flagsValue(["side_mismatch", "csf", "multipart"])
        study = self._study([("sub-01", {"qc_flags": stored, "review_status": "changes"}, False), ("sub-02", {}, False)])
        host = None
        try:
            session = study_ui.openStudy(study.path, showLibrary=False)
            self.assertIsNotNone(session)
            host = qt.QWidget()
            hostLayout = qt.QVBoxLayout(host)
            sectionWidget = libcard_columns._createSection(session)
            section = libcard_columns._sections[sectionWidget]
            hostLayout.addWidget(sectionWidget)
            host.resize(306, 600)
            host.show()
            section.refresh("sub-01")
            text = str(section.factsLabel.text)
            self.assertIn("Review: Changes requested", text)
            self.assertIn("Flags: Side, CSF, Parts", text)
            for code in ("side_mismatch", "multipart", ";", "Review: changes"):
                self.assertNotIn(code, text)
            self.assertIn("Side:", str(section.factsLabel.toolTip), "the tooltip explains the flags")
            self.assertEqual(libcard_columns.caseFactWords("qc_flags", {"qc_flags": "xform;subvoxel"}, "xform;subvoxel")[0], "Header, Tiny")
            self.assertEqual(libcard_flags.flagsCell({"qc_flags": stored})[0], "Side, CSF +1")
            # a value without words is shown as it is
            self.assertEqual(libcard_columns.caseFactWords("review_status", {}, "odd")[0], "odd")
            self.assertEqual(libcard_columns.caseFactWords("hemisphere", {}, "left")[0], "left")
        finally:
            if host is not None:
                host.deleteLater()
            study_ui.closeStudy()

    def test_80_create_study_on_patient_folders(self):
        """Create study on patients/patient01/T1.nii.gz + lesion.nii.gz made an empty study ("0 subjects · 4 files skipped",
        BB-1): names made only of modality or lesion words take the subject id from their folder below the study folder,
        folders above it ("sub-outer") never give one, and files still without an id are skipped with how to name them.
        """
        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "sub-outer", "patients")
        paths = []
        for subject in ("patient01", "patient02"):
            for name in ("T1", "lesion"):
                paths.append(os.path.join(folder, subject, f"{name}.nii.gz"))
                os.makedirs(os.path.dirname(paths[-1]), exist_ok=True)
                nifti.write_nifti(paths[-1], np.full(grid.shape, int(name == "lesion"), np.uint8), grid.affine)
        study = study_ui.createStudy(paths, folder=folder, openIt=False)
        self.assertEqual(study.subject_ids, ["sub-patient01", "sub-patient02"])
        for subject in ("patient01", "patient02"):
            row = study.subject(f"sub-{subject}")
            self.assertEqual((row["scan"], row["lesion"], row["status"]), (f"{subject}/T1.nii.gz", f"{subject}/lesion.nii.gz", "traced"))
        # The toast of files without an id says why they were skipped and how to name them
        text = study_ui.skippedText({"skipped": [{"path": "T1.nii.gz", "reason": "no subject id"}, {"path": "b.nii.gz", "reason": "unreadable: x"}]})
        self.assertTrue(text.startswith("2 files skipped (1 without a subject id"), text)
        self.assertIn("patient01/T1.nii.gz", text)
        self.assertIn("ID rule", text)
        self.assertEqual(study_ui.skippedText({"skipped": [{"path": "b.nii.gz", "reason": "unreadable: x"}]}), "1 file skipped")

    def test_79_unreadable_participants_tsv_keeps_study_changes_at_close(self):
        """Study changes that participants.tsv cannot take (another program made it unreadable) are kept for the next session
        when the study closes, the close prompt says so, and the save failure logs no traceback (BB-1).
        """
        from LesionNetworkMappingLib import study as studyModule

        from StudioLib import firstrun, study_ui

        if not study_ui.isAvailable():
            self.skipTest("needs the lesion network mapping library")
        study = self._study([("sub-04", {"status": "untraced"}, False), ("sub-05", {}, False), ("sub-06", {}, False)])
        folder = study.path
        tsv = os.path.join(folder, "participants.tsv")
        inbox = os.path.join(folder, ".studio", "inbox")
        lesion = study.subject("sub-04")["lesion"]

        class Recorder(logging.Handler):
            def __init__(self):
                super().__init__(logging.DEBUG)
                self.records = []

            def emit(self, record):
                self.records.append(record)

        recorder = Recorder()
        logging.getLogger().addHandler(recorder)
        try:
            session = study_ui.openStudy(folder, showLibrary=False)
            self.assertIsNotNone(session)
            # Another program (a spreadsheet, a text editor) saves the table with a value that has no column name
            with open(tsv, encoding="utf-8") as f:
                text = f.read()
            broken = "\n".join(line + "\tcheck scan quality" if line.startswith("sub-06\t") else line for line in text.split("\n"))
            with open(tsv, "w", encoding="utf-8") as f:
                f.write(broken)
            # Done of sub-04 (the row of a traced subject)
            session.study.set("sub-04", {"status": "traced", "lesion": lesion, "trace_seconds": "42"})
            self.assertFalse(session.flush())
            self.assertEqual(session.saveStatus()[0], "Study not saved")
            errors = [record.getMessage()[:200] for record in recorder.records if record.levelno >= logging.ERROR]
            self.assertEqual(errors, [], "a table that another program made unreadable is the user's problem, not a program error")
            self.assertFalse(any("Traceback" in record.getMessage() for record in recorder.records))
            self.assertEqual(session.unsavedSubjects(), ["sub-04"])
            # The quit prompt says that the study changes are not saved, names the subject and says they are kept
            mode, summary = firstrun.studyCloseSummary()
            self.assertEqual(mode, firstrun.CLOSE_MODE_LESION_SAVED)
            self.assertIn("sub-04", summary)
            self.assertIn("could not be saved", summary)
            self.assertIn("line", summary, "the reason (the table's line) is said")
            self.assertIn("kept", summary)
            self.assertNotIn("All changes are saved", summary)
            # Closing (the application quits) keeps the changes in the study's inbox; the broken table is left as it was
            study_ui.closeStudy()
            with open(tsv, encoding="utf-8") as f:
                self.assertEqual(f.read(), broken)
            posted = [name for name in os.listdir(inbox) if name.endswith(".json")]
            self.assertEqual(len(posted), 1, posted)
            with open(os.path.join(inbox, posted[0]), encoding="utf-8") as f:
                rows = json.load(f)["rows"]
            self.assertEqual(set(rows), {"sub-04"}, "only the changed row is kept")
            self.assertEqual((rows["sub-04"]["status"], rows["sub-04"]["trace_seconds"]), ("traced", "42"))
            # The user repairs the table and opens the study again: sub-04's Done is in the study
            with open(tsv, "w", encoding="utf-8") as f:
                f.write(text)
            session = study_ui.openStudy(folder, showLibrary=False)
            self.assertIsNotNone(session)
            row = session.study.subject("sub-04")
            self.assertEqual((row["status"], row["lesion"], row["trace_seconds"]), ("traced", lesion, "42"))
            self.assertEqual([name for name in os.listdir(inbox) if name.endswith(".json")], [], "the kept changes were merged")
            study_ui.closeStudy()
            reopened = studyModule.Study.open(folder)
            self.assertEqual(reopened.subject("sub-04")["status"], "traced")
            self.assertEqual(reopened.subject("sub-06").get("status"), "untraced")
        finally:
            logging.getLogger().removeHandler(recorder)
            study_ui.closeStudy()

    def test_81_assist_counts_stay_in_their_study(self):
        """Smart fill counts are kept per study: another study's subject with the same id is not assisted (BB-1)."""
        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.study import is_missing

        from StudioLib import draw, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        shape = (40, 40, 30)
        affine = np.array([[1.5, 0, 0, -30], [0, 1.5, 0, -30], [0, 0, 1.5, -22], [0, 0, 0, 1]], float)
        rng = np.random.default_rng(81)

        def scanFolder(name):
            folder = os.path.join(self.directory, name)
            os.makedirs(folder)
            path = os.path.join(folder, "sub-01_T1w.nii.gz")
            nifti.write_nifti(path, rng.normal(300, 8, shape).astype(np.int16), affine, sform_code=1, qform_code=1)
            return folder, path

        def paint(session, box):
            segmentation, volume = session.segmentationNode(), session.volumeNode()
            segmentId = segmentation.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentation, segmentId, volume)
            array[box] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentation, segmentId, volume)
            slicer.app.processEvents()

        def pump():
            for _ in range(4):
                slicer.app.processEvents()

        def sidecarAssist(session):
            lesion = session.study.abspath(session.study.subject("sub-01")["lesion"])
            sidecar = lesion[:-len(".nii.gz")] + ".json"
            if not os.path.exists(sidecar):
                return {}
            with open(sidecar, encoding="utf-8") as f:
                return (json.load(f).get("SlicerStudio") or {}).get("assist") or {}

        saved = dict(draw._assistCounts)
        draw._assistCounts.clear()
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            # Study A: sub-01 is traced with Smart fill (what its apply reports to Draw), then Done
            folderA, scanA = scanFolder("Study A")
            session = study_ui.createStudy([scanA], studioShell, folder=folderA)
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            paint(session, np.s_[10:15, 10:15, 10:15])
            draw.notifyAssist("smartfill", 125, {"tolerance": 0.1})
            self.assertEqual(draw.assistCounts("sub-01")["smartfill"]["voxels"], 125)
            self.assertIsNotNone(session.done())
            pump()
            self.assertIn("smartfill", session.study.subject("sub-01")["trace_method"].split("+"))
            study_ui.closeStudy()
            slicer.mrmlScene.Clear(0)
            # Study B, in another folder, has its own sub-01: traced with the Brush only
            folderB, scanB = scanFolder("Study B")
            session = study_ui.createStudy([scanB], studioShell, folder=folderB)
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertEqual(draw.assistCounts("sub-01"), {}, "study A's Smart fill counts are not study B's")
            self.assertEqual(draw.assistCounts(), {})
            paint(session, np.s_[8:16, 8:16, 8:14])
            self.assertIsNotNone(session.done())
            pump()
            row = session.study.subject("sub-01")
            self.assertEqual(row["trace_method"], "manual")
            self.assertTrue(is_missing(row.get("assist_pct")), row.get("assist_pct"))
            assist = sidecarAssist(session)
            self.assertIsNone(assist.get("smartfill_voxels_pct"), assist)
            self.assertNotIn("smartfill", assist.get("tools") or {}, assist)
            study_ui.closeStudy()
            slicer.mrmlScene.Clear(0)
            # Study A opened again: its sub-01 keeps its own counts
            session = study_ui.openStudy(folderA, studioShell, showLibrary=False)
            self.assertIsNotNone(session)
            self.assertEqual(draw.assistCounts("sub-01")["smartfill"]["voxels"], 125)
            study_ui.closeStudy()
            # Outside a study the counts are kept per segment ID, which the next scene reuses: a closed scene ends them
            draw._installImportGuard()
            draw._assistCounts["vtkMRMLSegmentationNode1/Segment_1"] = {"smartfill": {"voxels": 3, "applies": 1, "params": []}}
            slicer.mrmlScene.Clear(0)
            self.assertEqual(draw.assistCounts("vtkMRMLSegmentationNode1/Segment_1"), {})
            self.assertEqual(draw.assistCounts("sub-01"), {}, "no study is open")
            self.assertEqual(len([key for key in draw._assistCounts if key.startswith("study:")]), 1, "the study counts are kept")
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)
            draw._assistCounts.clear()
            draw._assistCounts.update(saved)

    def test_82_damaged_lesion_or_scan_is_said(self):
        """A lesion or scan file cut short opened without a word: the readers show the missing part as zeros (BB-1)."""
        import hashlib

        from LesionNetworkMappingLib import geometry, nifti

        from StudioLib import draw, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Damaged study")
        os.makedirs(folder)
        rng = np.random.default_rng(82)
        scans = []
        for index in range(1, 4):
            scans.append(os.path.join(folder, f"sub-0{index}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], rng.integers(100, 400, grid.shape).astype(np.int16), grid.affine)

        def truncate(path):
            with open(path, "rb") as f:
                data = f.read()
            with open(path, "wb") as f:
                f.write(data[:len(data) // 2])

        def md5(path):
            with open(path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()

        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append((str(text), kwargs))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        studioShell = shell.Shell(force=True)
        studioShell.apply()
        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            study = session.study
            # sub-01 and sub-02 are traced, then sub-03 is open
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            for sid in ("sub-01", "sub-02"):
                self.assertEqual(session.current(), sid)
                segmentationNode, volumeNode = session.segmentationNode(), session.volumeNode()
                segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
                array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
                array[20:30, 30:40, 20:30] = 1
                slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
                for _ in range(4):
                    slicer.app.processEvents()
                self.assertIsNotNone(session.done())
            self.assertEqual(session.current(), "sub-03")
            lesions = {sid: study.abspath(study.subject(sid)["lesion"]) for sid in ("sub-01", "sub-02")}
            for path in lesions.values():
                self.assertIsNone(nifti.image_data_problem(path), "a complete lesion file is not damaged")
            self.assertIsNone(nifti.image_data_problem(scans[0]))

            def damagedToasts(sid):
                return [(text, kwargs) for text, kwargs in toasts if sid in text and "damaged" in text]

            # The lesion of sub-01 is cut short: an empty lesion, the damaged file is said, no unsaved changes are claimed
            truncate(lesions["sub-01"])
            self.assertIsNotNone(nifti.image_data_problem(lesions["sub-01"]))
            damagedMd5 = md5(lesions["sub-01"])
            del toasts[:]
            self.assertTrue(session.open("sub-01", switchToDraw=False))
            self.assertEqual(session.current(), "sub-01")
            said = damagedToasts("sub-01")
            self.assertTrue(said, toasts)
            self.assertIn(study.subject("sub-01")["lesion"], said[-1][0])
            self.assertIn("lesion", said[-1][0])
            self.assertNotIn(folder, said[-1][0], "the toast names the table path, not the local path")
            self.assertEqual(said[-1][1].get("level"), "consequence")
            self.assertEqual(said[-1][1].get("actionText"), "Show folder")
            self.assertIsNone(study_ui.lesionMask(session.segmentationNode()), "the damaged lesion is not shown in part")
            self.assertFalse(session.isDirty())
            self.assertFalse(draw.saveStateText(session.segmentationNode())[0].startswith("Unsaved"),
                             draw.saveStateText(session.segmentationNode()))
            # The scan and the lesion of sub-02 are cut short after its lesion was read ahead: one toast says both
            session.waitForPrefetch()
            truncate(lesions["sub-02"])
            truncate(scans[1])
            del toasts[:]
            self.assertTrue(session.open("sub-02", switchToDraw=False))
            self.assertEqual(session.current(), "sub-02")
            said = damagedToasts("sub-02")
            self.assertTrue(said, toasts)
            self.assertIn(study.subject("sub-02")["lesion"], said[-1][0])
            self.assertIn("sub-02_T1w.nii.gz", said[-1][0])
            self.assertIsNone(study_ui.lesionMask(session.segmentationNode()))
            # Leaving sub-01 unchanged kept its damaged file and its row
            self.assertEqual(md5(lesions["sub-01"]), damagedMd5)
            self.assertEqual(study.subject("sub-01").get("status"), "traced")
            # Only the scan of sub-03 is cut short: the subject opens, and the damaged scan is said
            truncate(scans[2])
            del toasts[:]
            self.assertTrue(session.open("sub-03", switchToDraw=False))
            said = damagedToasts("sub-03")
            self.assertTrue(said, toasts)
            self.assertIn("sub-03_T1w.nii.gz", said[-1][0])
            self.assertNotIn("lesion file", said[-1][0])
            # A subject whose files are complete opens without the toast
            del toasts[:]
            nifti.write_nifti(scans[2], rng.integers(100, 400, grid.shape).astype(np.int16), grid.affine)
            self.assertTrue(session.open("sub-03", switchToDraw=False, reload=True))
            self.assertFalse(damagedToasts("sub-03"), toasts)
        finally:
            study_ui.closeStudy()
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_83_columns_confirm_keeps_the_edits_of_rows_it_did_not_save(self):
        """Columns...: a Confirm that saved one row rebuilt the table from the study, so a row without a role lost its typed
        Description under the note "Choose a role for X to keep its other changes.", and a row whose score question was answered
        "Keep as is" lost its other edits without a word (BB-1). Those rows keep what was typed, and the next Confirm saves it.
        """
        from StudioLib import libcard_columns as lc
        from StudioLib import study_ui

        study = self._study([(f"sub-0{index}", {"nihss": str(index), "site": "A", "barthel": str(10 * index)}, False) for index in range(1, 4)])
        folder = study.path
        session = study_ui.openStudy(folder, None, showLibrary=False)
        self.addCleanup(study_ui.closeStudy)
        slicer.app.processEvents()
        self.assertEqual(session.study.column_spec("nihss").role, lc.NOT_SET)

        def saved(name):
            with open(os.path.join(folder, "participants.json"), encoding="utf-8") as f:
                return json.load(f).get(name) or {}

        dialog = lc.ColumnsDialog(session)
        self.addCleanup(dialog.widget.deleteLater)
        dialog.show()
        dialog.onlyUnset.setChecked(False)
        slicer.app.processEvents()

        def cell(name, column):
            return dialog.table.item(dialog.rowOf(name), column).text()

        # A row without a role keeps its edits while another row is saved
        dialog.setText("nihss", lc.COL_DESCRIPTION, "NIH Stroke Scale at admission")
        dialog.setText("nihss", lc.COL_UNITS, "points")
        dialog.setRole("site", "metadata")
        result = dialog.confirm()
        slicer.app.processEvents()
        self.assertEqual(result["applied"], ["site"], result)
        self.assertIn("Choose a role for nihss", str(dialog.noteLabel.text))
        self.assertTrue(dialog.widget.isVisible())
        self.assertEqual(cell("nihss", lc.COL_DESCRIPTION), "NIH Stroke Scale at admission", "the typed description was dropped")
        self.assertEqual(cell("nihss", lc.COL_UNITS), "points")
        self.assertEqual(dialog.original["site"]["role"], "metadata", "the saved row shows what the study holds")
        dialog.setRole("nihss", "score")
        self.assertEqual(dialog.confirm()["applied"], ["nihss"])
        self.assertEqual(saved("nihss").get("Description"), "NIH Stroke Scale at admission")
        self.assertEqual(saved("nihss").get("Units"), "points")

        # A row answered "Keep as is" keeps its role and its other edits, and the note says so
        dialog = lc.ColumnsDialog(session)
        self.addCleanup(dialog.widget.deleteLater)
        dialog.show()
        slicer.app.processEvents()
        dialog.setRole("site", "score")
        dialog.setText("site", lc.COL_DESCRIPTION, "Recruiting site")
        dialog.setRole("barthel", "score")
        asked = []
        original = lc.confirmScore
        lc.confirmScore = lambda names, reasons, parent=None: (asked.extend(names), [])[1]
        try:
            result = dialog.confirm()
        finally:
            lc.confirmScore = original
        slicer.app.processEvents()
        self.assertEqual((asked, result["applied"], result["declined"]), (["site"], ["barthel"], ["site"]), result)
        self.assertEqual(dialog.roleCombo("site").currentData, "metadata")
        self.assertEqual(cell("site", lc.COL_DESCRIPTION), "Recruiting site", "the declined row lost its other edits")
        self.assertIn("site keeps its role", str(dialog.noteLabel.text))
        self.assertEqual(dialog.confirm()["applied"], ["site"])
        self.assertEqual(saved("site").get("Description"), "Recruiting site")
        self.assertEqual(session.study.column_spec("site").role, "metadata")

    # Share outside the brain on the FSL 2 mm grid (BB-1)

    def test_84_downloaded_nlin6_brain_mask_gives_the_outside_check_and_clip(self):
        """The NLin6Asym brain mask the user downloaded (Normalize options, in slicer.app.cachePath) feeds the Mask chip."""
        import hashlib

        from LesionNetworkMappingLib import nifti, normalize
        from LesionNetworkMappingUI import normalize_controller

        from StudioLib import lesionqc, study_ui

        # A stand-in for tpl-MNI152NLin6Asym_res-02_desc-brain_mask.nii.gz (no download in tests): its record names this file
        info = normalize.TEMPLATES["MNI152NLin6Asym"]["files"]["mask"]
        affine = np.array([[2.0, 0, 0, -20.0], [0, 2.0, 0, -20.0], [0, 0, 2.0, -20.0], [0, 0, 0, 1.0]])
        brain = np.zeros((21, 21, 21), np.uint8)
        brain[4:17, 4:17, 4:17] = 1
        downloads = os.path.join(self.directory, "cache")
        os.makedirs(downloads)
        maskPath = os.path.join(downloads, info["name"])
        nifti.write_nifti(maskPath, brain, affine, dtype=np.uint8)
        with open(maskPath, "rb") as stream:
            digest = hashlib.sha256(stream.read()).hexdigest()
        saved = dict(info)
        hidden = maskPath + ".hidden"
        os.replace(maskPath, hidden)  # not downloaded yet
        originalCache = normalize_controller.cacheDirectory
        normalize_controller.cacheDirectory = lambda: downloads
        info.update(sha256=digest, bytes=os.path.getsize(hidden))
        c = lesionqc.core()

        def restore():
            info.clear()
            info.update(saved)
            normalize_controller.cacheDirectory = originalCache
            c.ensureResources(reload=True)
            c.waitIdle(60.0)

        self.addCleanup(restore)
        c.ensureResources(reload=True)
        self.assertTrue(c.waitIdle(60.0))
        fromFetchCache = "fsl6" in c.resources.brain

        # The user's explicit download: the next look at a lesion loads the mask, without a "Get atlases" download
        os.replace(hidden, maskPath)
        c.ensureResources()
        self.assertTrue(c.waitIdle(60.0))
        self.assertIn("fsl6", sorted(c.resources.brain), "the downloaded NLin6Asym brain mask was not loaded")
        if not fromFetchCache:
            self.assertEqual(c.resources.brain["fsl6"][0].shape, brain.shape)
            version = c.resources.version
            c.ensureResources()  # loaded once: no reload on each lesion
            self.assertTrue(c.waitIdle(10.0))
            self.assertEqual(c.resources.version, version)

            # An NLin6 lesion (2 mm grid) half outside the brain: the amber Mask chip and its Clip action
            lesion = np.zeros(brain.shape, bool)
            lesion[14:20, 8:12, 8:12] = True
            result = lesionqc.evaluate(lesion, affine, mni=True, space="MNI152NLin6Asym", resources=c.resources)
            self.assertEqual(result["family"], "fsl6")
            self.assertAlmostEqual(result["outsidePct"], 50.0, delta=1.0)
            chips = [chip for chip in lesionqc.buildChips(result, mni=True, resourcesState=c.resources.state) if chip["key"] == "hygiene"]
            self.assertEqual(len(chips), 1, chips)
            self.assertIn("outside the brain", chips[0]["text"])
            self.assertIn("clip", [action["id"] for action in chips[0]["actions"]])

            # The participants.tsv column of the same lesion
            loaded = study_ui._loadBrainMask("MNI152NLin6Asym")
            self.assertIsNotNone(loaded)
            values = study_ui.metricsValues(np.ascontiguousarray(lesion.transpose(2, 1, 0)), affine, "MNI152NLin6Asym", loaded)
            self.assertAlmostEqual(values["outside_brain_pct"], 50.0, delta=1.0)

        # A file of that name that is not the template (another checksum) is not used
        info.update(sha256="0" * 64)
        self.assertIsNone(study_ui.brainMaskFile("MNI152NLin6Asym"))

    def test_85_review_of_a_read_only_study_says_so_and_changes_nothing(self):
        """Approve or Request changes on a study whose folder cannot be written (a shared, read-only study): a warning toast says
        the review was not recorded, the in-memory row keeps its review state (Approve of an unreviewed lesion first submits
        it: that step is not half-applied either) and nothing is saved. The PermissionError of the audit log was uncaught.
        """
        from LesionNetworkMappingLib import review as rules
        from LesionNetworkMappingLib.study import Study

        from StudioLib import review

        studyObject = Study.create(os.path.join(self.directory, "Shared study"), space="MNI152NLin6Asym")
        for sid in ("sub-01", "sub-02"):
            studyObject.add_subject(sid, group="case", status="traced", trace_method="manual")
        studyObject.save()
        # The audit log cannot be opened for appending (a directory in its place: also for root, who ignores file modes)
        os.makedirs(rules.audit_path(studyObject))

        class FakeSession:
            study = studyObject
            flushes = 0

            def current(self):
                return None

            def isDirty(self, sid=None):
                return False

            def setValues(self, sids, values):
                pass

            def flush(self):
                FakeSession.flushes += 1

        toasts = []
        originalToast = review._toast
        review._toast = lambda text, ms=3000, **kwargs: toasts.append((str(text), kwargs.get("level")))
        self.addCleanup(setattr, review, "_toast", originalToast)
        before = {sid: studyObject.subject(sid) for sid in ("sub-01", "sub-02")}
        self.assertIsNone(review.transition("sub-01", "approved", session=FakeSession(), who="Rita Reviewer", via="library"))
        self.assertIsNone(review.transition("sub-02", "changes", comment="extend it", session=FakeSession(), who="Rita Reviewer", via="library"))
        self.assertEqual(len(toasts), 2, toasts)
        for text, level in toasts:
            self.assertIn("cannot be written", text)
            self.assertIn("not recorded", text)
            self.assertEqual(level, "warning")
        for sid, row in before.items():
            self.assertEqual(studyObject.subject(sid), row, f"{sid} was changed in memory")
            self.assertEqual(rules.status_of(studyObject.subject(sid)), "none")
        self.assertEqual(FakeSession.flushes, 0)

    def test_figure_crop_png_accepts_float_pixels(self):
        """A 32-bit float figure (ImageJ TIFF) cropped to PNG scales to 8 bit instead of raising 'cannot write mode F as PNG'."""
        import numpy as np
        from PIL import Image
        from StudioLib import figure

        floats = np.linspace(-2.0, 6.0, 60, dtype=np.float32).reshape(6, 10)
        pixels = figure._pngPixels(floats)
        self.assertEqual(pixels.dtype, np.uint8)
        self.assertEqual((int(pixels.min()), int(pixels.max())), (0, 255))
        path = os.path.join(tempfile.mkdtemp(), "f.png")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        Image.fromarray(pixels).crop((1, 1, 5, 4)).save(path, format="PNG")
        self.assertTrue(os.path.getsize(path) > 0)
        flat = figure._pngPixels(np.full((3, 3), 7.0, dtype=np.float32))
        self.assertEqual(int(flat.max()), 0)
        sixteen = np.arange(4, dtype=np.uint16).reshape(2, 2)
        self.assertIs(figure._pngPixels(sixteen).dtype, np.dtype(np.uint16))

    def test_86_classic_after_a_scene_load_outside_draw_logs_no_editor_error(self):
        """A scene loaded from the Library leaves the Segment Editor without its parameter node (draw._onSceneStartImport):
        Classic straight afterwards gives it back before the presentation changes, so no "Invalid segment editor
        parameter set node" is logged (the red badge) and the editor follows the scene's node.
        """
        import numpy as np
        from StudioLib import draw, shell

        if slicer.util.mainWindow() is None or not hasattr(slicer.modules, "segmenteditor"):
            self.skipTest("needs the main window and the Segment Editor")
        slicer.mrmlScene.Clear(0)
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(10):
                slicer.app.processEvents()

        try:
            image = np.zeros((20, 24, 28), np.int16)
            image[5:15, 6:18, 7:21] = 200
            volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "sub-01_T1w")
            slicer.util.updateVolumeFromArray(volumeNode, image)
            volumeNode.CreateDefaultDisplayNodes()
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion")
            segmentationNode.CreateDefaultDisplayNodes()
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            segmentationNode.GetSegmentation().AddEmptySegment("bright")
            studioShell.switchTo("draw")
            pump()
            editor = slicer.modules.SegmentEditorWidget.editor
            self.assertIsNotNone(editor.mrmlSegmentEditorNode())
            studioShell.switchTo("library")
            pump()
            draw._onSceneStartImport()
            self.assertIsNone(editor.mrmlSegmentEditorNode())
            model = slicer.app.errorLogModel()
            rowsBefore = model.rowCount()
            studioShell.classic(persist=False)
            pump()
            texts = [
                str(model.index(row, 4).data()) for row in range(rowsBefore, model.rowCount())
                if model.index(row, 2).data() in ("Error", "Critical", "Fatal")
            ]
            self.assertEqual([text for text in texts if "parameter set node" in text], [])
            self.assertIsNotNone(editor.mrmlSegmentEditorNode())
        finally:
            studioShell.dispose()
            for _ in range(4):
                slicer.app.processEvents()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_87_extrusion_binary_tooltip_voxel_count_follows_the_thickness(self):
        import re

        from StudioLib import lesionqc
        card = lesionqc.PolicyCard(None, None)
        for total, voxels in ((2.0, 3), (4.0, 5), (6.0, 7)):
            card.spin.setValue(total)
            tip = str(card.binaryButton.toolTip)
            self.assertIn(f"({voxels} voxels at 1 mm)", tip, tip)
            self.assertEqual(len(re.findall(r"\d+ voxels at 1 mm", tip)), 1)

    def test_88_docs_appearance_font_keys_and_four_choices(self):
        """settings.md does not claim that nothing is written for the default font (an empty Appearance/Font and the
        ForkDefaultFont keys are written on a first start), and studio.md counts the four Appearance choices.
        """
        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        names = ("Docs/user_guide/settings.md", "Docs/user_guide/modules/studio.md")
        paths = {name: os.path.join(root, *name.split("/")) for name in names}
        if not all(os.path.isfile(path) for path in paths.values()):
            self.skipTest("the documentation is not next to this build")
        texts = {}
        for name, path in paths.items():
            with open(path, encoding="utf-8") as stream:
                texts[name] = " ".join(stream.read().replace("*", "").split())
        settings, studio = (texts[name] for name in names)
        self.assertNotIn("nothing is written to the settings", settings)
        self.assertIn("Appearance/ForkDefaultFontFamily", settings)
        self.assertIn("Appearance/ForkDefaultFontPointSize", settings)
        self.assertIn("Appearance has four choices", studio)
        self.assertNotIn("Appearance has three choices", studio)
        self.assertIn("Font and size:", studio)

    def test_89_quit_with_the_file_open_filter_exits_cleanly(self):
        """A normal start (not ``--testing``, so Studio installs its QFileOpenEvent filter on the application) quits with
        exit code 0: the Python filter is removed on ``aboutToQuit``, before the interpreter is finalized (BB-1: every quit
        ended in a segmentation fault when the widgets owned by Python sent their destruction events through it).
        """
        launcher = slicer.app.launcherExecutableFilePath
        launcher = launcher() if callable(launcher) else launcher
        if not launcher or not os.path.isfile(launcher):
            self.skipTest("no Slicer launcher")
        script = os.path.join(self.directory, "quit_with_file_open_filter.py")
        with open(script, "w") as stream:
            stream.write(QUIT_FILE_OPEN_FILTER_SCRIPT)
        home = os.path.join(self.directory, "quit_fileopen_home")
        os.makedirs(os.path.join(home, "tmp"))
        environment = dict(os.environ)
        environment.update({"HOME": home, "XDG_CONFIG_HOME": os.path.join(home, ".config"), "TMPDIR": os.path.join(home, "tmp"),
                            "SLICER_STUDIO_PROJECTS_ROOT": os.path.join(self.directory, "quit_fileopen_projects")})
        for key in ("SLICERRC", "SLICER_STUDIO_WEB"):
            environment.pop(key, None)
        process = subprocess.run([launcher, "--no-splash", "--python-script", script], env=environment, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=QUIT_TIMEOUT_S, check=False)
        output = process.stdout.decode("utf-8", "replace")
        ready = [line for line in output.splitlines() if line.startswith("QUITTEST READY")]
        self.assertTrue(ready, output[-3000:])
        self.assertIn("filter=True", ready[0], ready[0])
        self.assertNotIn("Traceback", output, output[-3000:])
        self.assertNotIn("exit abnormally", output, output[-3000:])
        self.assertEqual(process.returncode, 0, json.dumps(output[-3000:]))

    def test_91_second_tracing_in_a_read_only_study_is_refused_plainly(self):
        """A blind second tracing in a study folder that cannot be written (BB-1): the journal timer slot returns False
        (the journal folder rater-<label>/ cannot be made) and marks the session read-only instead of raising an uncaught
        PermissionError, and Save second tracing says in plain words that the study folder is read-only (no errno, no
        absolute path), keeps the strokes and returns False. Root ignores chmod, so the permission errors are injected.
        """
        from LesionNetworkMappingLib import agreement, geometry, nifti

        from StudioLib import adjudicate, journal, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Read only second study")
        os.makedirs(folder)
        scan = os.path.join(folder, "sub-01_T1w.nii.gz")
        nifti.write_nifti(scan, np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: toasts.append(str(text))
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        originalMakedirs = journal.os.makedirs
        originalSave = agreement.save_second_tracing
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(6):
                slicer.app.processEvents()

        def paint(segmentationNode, volumeNode):
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()
            return segmentId

        def denied(path, *args, **kwargs):
            raise PermissionError(13, "Permission denied", str(path))

        try:
            session = study_ui.createStudy([scan], studioShell, folder=folder)
            self.assertIsNotNone(session)
            review.setRaterName("Alice")
            session.open("sub-01", switchToDraw=False)
            paint(session.segmentationNode(), session.volumeNode())
            self.assertIsNotNone(session.done())
            pump()
            agreement.assign(session.study, sids=["sub-01"], rater="Bob", who="Alice")
            session.flush()
            review.setRaterName("Bob")
            state = adjudicate.startSecondTracing(sid="sub-01", shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            segmentationNode = state.segmentationNode()
            volumeNode = slicer.mrmlScene.GetNodeByID(state._nodeIds[0])
            segmentId = paint(segmentationNode, volumeNode)
            secondJournal = state.journal()
            self.assertIsNotNone(secondJournal)
            self.assertFalse(session.readOnly)

            # The journal timer slot: the folder rater-Bob/ cannot be made
            journal.os.makedirs = denied
            toasts.clear()
            self.assertFalse(secondJournal.writeNow())  # raised PermissionError before the fix
            self.assertTrue(session.readOnly)
            self.assertTrue(any("read-only" in text for text in toasts), toasts)
            self.assertFalse(secondJournal.writeNow(), "the second attempt is quiet too")

            # Save second tracing: lesions/rater-Bob/ cannot be made
            agreement.save_second_tracing = lambda *args, **kwargs: denied(os.path.join(folder, "lesions", "rater-Bob"))
            toasts.clear()
            self.assertFalse(state.save())
            self.assertIs(adjudicate.state(), state)
            self.assertEqual(len(toasts), 1, toasts)
            self.assertIn("could not be saved: the study folder is read-only (Permission denied)", toasts[0])
            self.assertNotIn("Errno", toasts[0])
            self.assertNotIn(folder, toasts[0])
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            self.assertEqual(int(array.sum()), 27, "the strokes are kept")
        finally:
            journal.os.makedirs = originalMakedirs
            agreement.save_second_tracing = originalSave
            if adjudicate.state() is not None:
                adjudicate.state().exit(quiet=True, keepScene=True)
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)
            slicer.mrmlScene.Clear(0)

    def test_90_alt_arrows_follow_a_second_tracing_pass(self):
        """Alt+←/→ during a blind second tracing step through the pass (L-45: they follow the active queue). The study
        session has no subject during the pass, so they opened the first or last row of the table instead: the pass went
        backwards or ended, and the next Alt+→ showed the first rater's tracing of a subject still assigned to the second
        rater (BB-1).
        """
        from LesionNetworkMappingLib import agreement, geometry, nifti

        from StudioLib import adjudicate, draw, review, shell, study_ui

        if slicer.util.mainWindow() is None or not study_ui.isAvailable():
            self.skipTest("needs the main window and the lesion network mapping library")
        grid = geometry.GRID_BY_ID["TF2009-2mm"].grid
        folder = os.path.join(self.directory, "Second alt study")
        os.makedirs(folder)
        sids = ("sub-01", "sub-02", "sub-03")
        scans = []
        for sid in sids:
            scans.append(os.path.join(folder, f"{sid}_T1w.nii.gz"))
            nifti.write_nifti(scans[-1], np.zeros(grid.shape, np.uint8), grid.affine)
        toasts = []
        originalToast = study_ui._toast
        study_ui._toast = lambda shell, text, **kwargs: toasts.append(text)
        self.addCleanup(setattr, study_ui, "_toast", originalToast)
        originalAdjudicateToast = adjudicate._toast
        adjudicate._toast = lambda text, ms=3000, **kwargs: toasts.append(text)
        self.addCleanup(setattr, adjudicate, "_toast", originalAdjudicateToast)
        self.addCleanup(review.setRaterName, review.storedRaterName())
        studioShell = shell.Shell(force=True)
        studioShell.apply()

        def pump():
            for _ in range(6):
                slicer.app.processEvents()

        def paint(segmentationNode, volumeNode):
            segmentId = segmentationNode.GetSegmentation().GetNthSegmentID(0)
            array = slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId, volumeNode)
            array[20:23, 30:33, 20:23] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(array, segmentationNode, segmentId, volumeNode)
            pump()

        def firstTracingShown():
            node = session.segmentationNode()
            return node is not None and node.GetDisplayNode() is not None and node.GetDisplayNode().GetVisibility()

        try:
            session = study_ui.createStudy(scans, studioShell, folder=folder)
            self.assertIsNotNone(session)
            review.setRaterName("Alice")
            for sid in sids:
                session.open(sid, switchToDraw=False)
                paint(session.segmentationNode(), session.volumeNode())
                self.assertIsNotNone(session.done())
                pump()
            agreement.assign(session.study, sids=list(sids), rater="Bob", who="Alice")
            session.flush()
            review.setRaterName("Bob")
            state = adjudicate.startSecondTracing(shell=studioShell)
            self.assertIsNotNone(state)
            pump()
            self.assertEqual(state.current, "sub-01")

            # Bob saves sub-01; the pass opens sub-02 blind
            paint(state.segmentationNode(), slicer.mrmlScene.GetNodeByID(state._nodeIds[0]))
            self.assertTrue(state.saveNext())
            pump()
            self.assertEqual(state.current, "sub-02")

            # Alt+→ opens sub-03 in the same pass, never a subject of the table with Alice's tracing
            draw.stepSubject(1, studioShell)
            pump()
            self.assertIs(adjudicate.state(), state, f"Alt+→ ended the pass: {toasts[-3:]}")
            self.assertEqual(state.current, "sub-03")
            self.assertIsNone(session.current(), "the study session opened a subject (and its first tracing)")
            self.assertFalse(firstTracingShown())

            # At the end of the pass Alt+→ stays (a toast); Alt+← goes back to sub-02, still blind
            draw.stepSubject(1, studioShell)
            pump()
            self.assertIs(adjudicate.state(), state)
            self.assertEqual(state.current, "sub-03")
            self.assertIn("Last case in Second tracing", toasts)
            draw.stepSubject(-1, studioShell)
            pump()
            self.assertIs(adjudicate.state(), state)
            self.assertEqual(state.current, "sub-02")
            self.assertIsNone(session.current())
            self.assertFalse(firstTracingShown())
            # sub-01 is saved: Alt+← does not open it (it is no longer in the pass)
            draw.stepSubject(-1, studioShell)
            pump()
            self.assertIs(adjudicate.state(), state)
            self.assertEqual(state.current, "sub-02")
            self.assertTrue(adjudicate.secondTodo(session.study.subject("sub-02")))
        finally:
            if adjudicate.state() is not None:
                adjudicate.state().exit(quiet=True, keepScene=True)
            study_ui.closeStudy()
            studioShell.dispose()
            pump()
            slicer.app.sendPostedEvents(None, qt.QEvent.DeferredDelete)

    def test_92_readme_figure_letters_match_the_app(self):
        """The README says a figure shows no letters until its side is answered and then only R and L (as studio.md and
        orientation.py say), not that it gets no patient letters at all.
        """
        root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", ".."))
        path = os.path.join(root, "README.md")
        if not os.path.isfile(path):
            self.skipTest("the documentation is not next to this build")
        with open(path, encoding="utf-8") as stream:
            readme = " ".join(stream.read().replace("*", "").split())
        self.assertNotIn("gets no patient letters", readme)
        self.assertIn("until its side is answered it shows `Figure · side unknown` and no letters", readme)
        self.assertIn("once answered it shows only R and L", readme)

    def test_93_lesion_card_describes_a_new_lesion_that_reuses_the_closed_ones_ids(self):
        """After Ctrl+W the next lesion gets the same node and segment IDs again (vtkMRMLSegmentationNode1, Segment_1). The
        Draw card kept the closed lesion's Volume/Parts/Side because it only recomputed the stats when those IDs changed
        (BB-1 draw-card-stale-after-ctrlw-then-open-or-drop-same-node-id).
        """
        import numpy as np

        from StudioLib import draw

        slicer.mrmlScene.Clear(0)
        self.addCleanup(slicer.mrmlScene.Clear, 0)

        def addLesion(size):
            image = np.zeros((20, 20, 20), np.int16)
            volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "scan")
            slicer.util.updateVolumeFromArray(volumeNode, image)
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "lesion")
            segmentationNode.CreateDefaultDisplayNodes()
            segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
            segmentId = segmentationNode.GetSegmentation().AddEmptySegment("Segment_1", "lesion")
            mask = np.zeros((20, 20, 20), np.uint8)
            mask[2:2 + size, 2:2 + size, 2:2 + size] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentationNode, segmentId, volumeNode)
            return segmentationNode, volumeNode

        extension = draw.DrawExtension()
        extension._hint = lambda *args: None  # the hint needs the Draw editor; the stat cells do not
        first, firstVolume = addLesion(2)
        state = extension._cardState(None, None, first, firstVolume, None, False)
        self.assertIsNotNone(state["stats"])
        self.assertAlmostEqual(state["stats"]["ml"], 8 / 1000.0)
        firstId = first.GetID()
        slicer.mrmlScene.Clear(0)
        second, secondVolume = addLesion(3)
        self.assertEqual(second.GetID(), firstId, "the new lesion reuses the closed lesion's node ID (the case of the bug)")
        # No edit has happened yet (computeMetrics=False), as right after Open... or a drop
        state = extension._cardState(None, None, second, secondVolume, None, False)
        self.assertIsNotNone(state["stats"])
        self.assertAlmostEqual(state["stats"]["ml"], 27 / 1000.0, msg="the card describes the new lesion, not the closed one")

    def test_94_save_lesion_default_name_folds_accents_like_export(self):
        from StudioLib import draw

        for scanName, expected in (
            ("Müller_T1", "Muller_T1_lesion.nii.gz"),
            ("Lésion gauche", "Lesion_gauche_lesion.nii.gz"),
            ("Ödem rechts", "Odem_rechts_lesion.nii.gz"),
        ):
            volume = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", scanName)
            segmentation = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", "Lesions_1")
            self.assertEqual(draw.defaultLesionFileName(segmentation, volume), expected)
