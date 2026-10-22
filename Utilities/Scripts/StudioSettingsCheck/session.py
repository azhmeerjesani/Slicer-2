"""G2 settings session (runs in any Slicer). SESSION env selects what the session does, then the app quits normally
(so that settings are written as on a real quit). Writes $G2_SET_OUT/<SESSION_TAG>.json with what the app showed.
"""

import json
import os
import time
import traceback

import qt
import slicer

OUT = os.environ["G2_SET_OUT"]
SESSION = os.environ.get("SESSION", "plain")
TAG = os.environ.get("SESSION_TAG", SESSION)
R = {"version": slicer.app.applicationVersion, "session": SESSION}


def pump(n=10):
    for _ in range(n):
        slicer.app.processEvents()
        time.sleep(0.02)


def _value(obj, name):
    v = getattr(obj, name)
    return v() if callable(v) else v


def observed():
    s = slicer.app.userSettings()
    font = _value(slicer.app, "font")
    return {
        "styleName": str(_value(slicer.app.style(), "objectName")),
        "styleClass": slicer.app.style().metaObject().className(),
        "font": font.toString() if hasattr(font, "toString") else str(font),
        "settingsFile": s.fileName(),
        "Styles/Style": s.value("Styles/Style"),
        "font key": str(s.value("font")),
    }


try:
    pump(20)
    try:
        R["before"] = observed()
    except Exception:
        R["before"] = traceback.format_exc()
    if SESSION == "official-user":
        # An official user's explicit choices (as the Settings dialog writes them)
        s = slicer.app.userSettings()
        s.setValue("Styles/Style", "Dark Slicer")
        f = qt.QFont("DejaVu Sans", 11)
        s.setValue("font", f)
        s.setValue("Modules/FavoriteModules", ["Data", "Volumes", "SegmentEditor", "Markups"])
        s.setValue("Views/Orientation/Default", "Axial")
        s.sync()
    elif SESSION == "studio":
        from StudioLib import shell

        sh = shell.instance()
        R["studioActive"] = bool(sh is not None and sh._active)
        if sh is not None and sh._active:
            for key in ("draw", "map", "library", "draw"):
                sh.workspaces.switchTo(key)
                pump(10)
            R["workspace"] = sh.workspaces.currentKey() if hasattr(sh.workspaces, "currentKey") else None
            sh.classic()  # the user switches to Classic: persists Studio/Mode=classic
            pump(10)
    elif SESSION == "classic":
        from StudioLib import shell

        # G3: shell.instance() is None in Classic; the (inactive) shell is the one the "Switch to Studio interface" action uses
        sh = shell.instance() or shell._shell
        R["studioActive"] = bool(sh is not None and sh._active)
        slicer.util.selectModule("Volumes")
        pump(5)
        slicer.util.selectModule("SegmentEditor")
        pump(5)
        if sh is not None and not sh._active:
            sh.studio()  # back to Studio (persists Studio/Mode=studio)
            pump(20)
            R["studioActiveAfterSwitch"] = bool(sh._active)
    elif SESSION == "study":
        # G3: the study loop (W3-07): create a study from two synthetic scans, paint on sub-01, Done -> next
        import numpy as np
        from LesionNetworkMappingLib import nifti
        from StudioLib import draw, shell, study_ui

        sh = shell.instance()
        if sh is None and shell._shell is not None:
            shell._shell.studio()  # the user switches to Studio first (the study loop is a Studio feature)
            pump(20)
            sh = shell.instance()
        R["studioActive"] = bool(sh is not None and sh._active)
        folder = os.path.join(OUT, "study-" + TAG)
        os.makedirs(folder, exist_ok=True)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = (-40, -40, -30)
        for sid in ("sub-01", "sub-02"):
            nifti.write_nifti(os.path.join(folder, f"{sid}_T1w.nii.gz"), (np.random.default_rng(1).random((40, 40, 30)) * 100).astype(np.float32), affine)
        session = study_ui.createStudy(sorted(os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".nii.gz")), sh)
        pump(20)
        session.open("sub-01", switchToDraw=True)
        pump(20)
        seg = session.segmentationNode()
        segmentId = seg.GetSegmentation().GetNthSegmentID(0)
        array = slicer.util.arrayFromSegmentBinaryLabelmap(seg, segmentId, session.volumeNode())
        array[10:14, 10:14, 10:14] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(array, seg, segmentId, session.volumeNode())
        pump(5)
        draw.doneNext(sh)
        pump(20)
        R["studyCurrent"] = session.current()
        R["studyTraced"] = session.study.subject("sub-01")["status"]
    elif SESSION == "plain":
        slicer.util.selectModule("Volumes")
        pump(5)
    try:
        R["after"] = observed()
    except Exception:
        R["after"] = traceback.format_exc()
except Exception:
    R["error"] = traceback.format_exc()
json.dump(R, open(os.path.join(OUT, f"{TAG}.json"), "w"), indent=1, default=str)
# A normal quit (writes the settings as a user quit would)
qt.QTimer.singleShot(200, slicer.util.mainWindow().close if slicer.util.mainWindow() else slicer.app.quit)
qt.QTimer.singleShot(8000, lambda: slicer.app.exit(0))
