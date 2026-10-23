"""Library extension: the "Analysis" card of the STUDY section (A-04c, LNM addendum §6.2 and §7.2).

The card says where the cohort analysis of the open study stands, in one line, from cheap reads of
``derivatives/lnm/conn-*/pipeline.json`` and ``.pipeline/progress.json`` (on study open and every 5 s while it is
visible):

- "Cohort analysis: up to date · finished 2 h ago", "3 new or changed lesions" (the staleness plan of the last run's
  settings, ``pipeline.plan`` in a worker thread: file reads and hashing only), "Never run",
  "Running · Network maps 34%" (a run of this application or of another process), "Stopped", "Failed".
- [Open in Map] shows Map with Seed = Cohort and the Run everything card; [Open report] opens the last report.

A StudySession Done hook (order 900; it returns ``{}`` and only schedules a timer) shows the toast "All N lesions traced ·
run the cohort analysis in Map" once per session when every included subject is traced. In Classic the Studio module
panel hosts the same card. The Search entries of the cohort analysis (``card_pipeline.paletteEntries``) are also listed
while this module is imported, so that they exist before Map is first opened. Nothing is written: no setting, and never
participants.tsv. "Keep analysis up to date" is not part of this build (D-A22, backlog).
"""

import glob
import json
import logging
import os
import traceback

import qt
from slicer.i18n import tr as _

from . import library, ui

#: The line is refreshed this often while the card is visible (cheap file reads)
REFRESH_MS = 5000
#: Order of the Done hook among the study's hooks (after the lesion and metric hooks)
DONE_HOOK_ORDER = 900
#: The "all traced" check runs this long after Done (the row is updated by then)
ALL_TRACED_DELAY_MS = 300
#: The staleness plan runs this long after the last change (Done, study opened)
PLAN_DELAY_MS = 1500
#: Height of the card (SPEC 3.1 card rhythm)
CARD_HEIGHT = 52
TRACED = ("traced", "mapped")


def _cardPipeline():
    """``LesionNetworkMappingUI.card_pipeline`` (A-04c), or None when the LNM module is not part of the build."""
    import importlib

    try:
        return importlib.import_module("LesionNetworkMappingUI.card_pipeline")
    except Exception:
        logging.debug(f"Studio Library: the cohort analysis card is not available:\n{traceback.format_exc()}")
        return None


def _pipeline():
    import importlib

    return importlib.import_module("LesionNetworkMappingLib.pipeline")


def _session():
    studyUi = library._studyUi()
    return studyUi.session() if studyUi is not None else None


def _readJson(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


#
# The state of a study's analysis (cheap reads)
#


def analysisFolders(studyPath):
    """Output folders of the cohort pipeline in a study: ``derivatives/lnm/conn-*`` with a pipeline.json or a lock."""
    pipeline = _pipeline()
    root = os.path.join(studyPath, "derivatives", "lnm")
    folders = []
    for folder in sorted(glob.glob(os.path.join(glob.escape(root), "conn-*"))):
        if os.path.isfile(os.path.join(folder, pipeline.PIPELINE_JSON)) or os.path.isfile(os.path.join(folder, pipeline.PRIVATE_DIR, pipeline.LOCK_NAME)):
            folders.append(folder)
    return folders


def latestAnalysis(studyPath):
    """``{out, record, status}`` of the analysis to show: a running one first, else the most recently updated; None without any."""
    pipeline = _pipeline()
    best, bestKey = None, None
    for folder in analysisFolders(studyPath):
        try:
            status = pipeline.status(folder)
        except Exception:
            continue
        record = _readJson(os.path.join(folder, pipeline.PIPELINE_JSON))
        if record is not None and record.get("format") != pipeline.FORMAT_NAME:
            record = None
        try:
            mtime = os.path.getmtime(os.path.join(folder, pipeline.PIPELINE_JSON))
        except OSError:
            mtime = 0.0
        key = (status.get("status") == "running", mtime)
        if bestKey is None or key > bestKey:
            best, bestKey = {"out": folder, "record": record, "status": status}, key
    return best


def stateLine(analysis, plan=None, liveModel=None):
    """``(text, level)`` of the card's line. ``plan`` is the staleness summary (``card_pipeline.summarizePlan``)."""
    cardPipeline = _cardPipeline()
    if liveModel is not None and liveModel.isActive():
        return _("Running · {stage} {percent}%").format(stage=liveModel.currentLabel(), percent=int(liveModel.percent())), "accent"
    if analysis is None:
        return _("Cohort analysis: never run"), "text2"
    status = analysis.get("status") or {}
    record = analysis.get("record") or {}
    if status.get("status") == "running":
        stage = cardPipeline.rowLabelOf(status.get("stage")) if cardPipeline is not None and status.get("stage") else (status.get("stage") or "")
        percent = status.get("percent")
        text = _("Running · {stage}").format(stage=stage)
        return (f"{text} {int(float(percent))}%" if percent is not None else text), "accent"
    if record.get("status") == "error" or status.get("status") == "error":
        return _("Cohort analysis failed (open Map for the details)"), "danger"
    stale = 0
    if plan is not None:
        stale = int(plan["counts"].get("compute", 0)) + int(plan["counts"].get("normalize", 0))
    if record.get("status") == "cancelled":
        return _("Cohort analysis: stopped"), "text2"
    if stale:
        return (_("{n} new or changed lesion") if stale == 1 else _("{n} new or changed lesions")).format(n=stale), "warning"
    if record.get("status") == "done_with_failures":
        failed = len(record.get("failed_stages") or [])
        return (_("Cohort analysis: finished with {n} failed stage") if failed == 1 else _("Cohort analysis: finished with {n} failed stages")).format(n=failed), "warning"
    if record.get("status") == "done":
        age = cardPipeline.formatAge(record.get("updated")) if cardPipeline is not None else ""
        return (_("Cohort analysis: up to date · finished {age}").format(age=age) if age else _("Cohort analysis: up to date")), "success"
    return _("Cohort analysis: never run"), "text2"


def planConfig(analysis):
    """The analysis settings of a finished run (its pipeline.json, paths made absolute) for the staleness plan, or None."""
    if analysis is None or analysis.get("record") is None:
        return None
    pipeline = _pipeline()
    try:
        config = pipeline.PipelineConfig.from_json(analysis["record"], base=analysis["out"])
    except Exception:
        return None
    if not config.study or not config.bundle or not os.path.isdir(config.bundle):
        return None  # the connectome of that run is not on this computer
    return config.to_json()


#
# The card
#


class AnalysisCard:
    """The 'Analysis' card of the STUDY section (CARD, see the module docstring)."""

    def __init__(self, session):
        self.session = session
        self.analysis = None
        self.plan = None
        self.planKey = None
        self.studyPath = None
        self.widget = qt.QWidget()
        self.widget.setObjectName("StudioAnalysisLibraryCard")
        self.widget.setFixedHeight(CARD_HEIGHT)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(2)
        top = qt.QHBoxLayout()
        top.setSpacing(8)
        title = qt.QLabel(_("Analysis").upper())
        title.setObjectName("StudioAnalysisLibraryTitle")
        title.setProperty("slicerRole", "sectionHeader")
        library._setFont(title, 11, qt.QFont.DemiBold, 0.6)
        top.addWidget(title)
        top.addStretch(1)
        self.openInMapButton = library._linkButton(_("Open in Map"), "StudioAnalysisOpenInMapButton", self.openInMap)
        self.openInMapButton.setToolTip(_("Show Map with Seed = Cohort and the Run everything card"))
        top.addWidget(self.openInMapButton)
        self.openReportButton = library._linkButton(_("Open report"), "StudioAnalysisOpenReportButton", self.openReport)
        self.openReportButton.setToolTip(_("Open report.html of the last cohort analysis"))
        top.addWidget(self.openReportButton)
        layout.addLayout(top)
        self.line = qt.QLabel()
        self.line.setObjectName("StudioAnalysisLine")
        self.line.setTextFormat(qt.Qt.PlainText)
        self.line.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        library._setFont(self.line, 12)
        self._level = "text2"
        ui.bindStyleSheet(self.line, lambda: f"QLabel#StudioAnalysisLine {{ color: {ui.token(self._level).name()}; }}")
        layout.addWidget(self.line)
        self.timer = qt.QTimer()
        self.timer.setInterval(REFRESH_MS)
        self.timer.connect("timeout()", self._onTimer)
        self.planTimer = qt.QTimer()
        self.planTimer.setSingleShot(True)
        self.planTimer.setInterval(PLAN_DELAY_MS)
        self.planTimer.connect("timeout()", self.requestPlan)
        cardPipeline = _cardPipeline()
        self.worker = cardPipeline.PlanWorker(self._onPlan) if cardPipeline is not None else None
        self.timer.start()
        _cards.append(self)

    def dispose(self):
        self.timer.stop()
        self.planTimer.stop()
        if self.worker is not None:
            self.worker.stop()
        if self in _cards:
            _cards.remove(self)

    # State

    def _liveModel(self):
        cardPipeline = _cardPipeline()
        if cardPipeline is None or self.analysis is None:
            return None
        jobs = cardPipeline.pipelineJobs(self.analysis.get("out"))
        return cardPipeline.modelFor(jobs[-1]) if jobs else None

    def refresh(self, selectedIds=None):
        """Re-read the state files of the open study (cheap) and show the line."""
        session = _session()
        if session is None:
            return
        self.session = session
        studyPath = session.study.path
        if studyPath != self.studyPath:
            self.studyPath, self.plan, self.planKey = studyPath, None, None
            self.schedulePlan()
        try:
            self.analysis = latestAnalysis(studyPath)
        except Exception:
            logging.error(f"Studio Library: the cohort analysis state could not be read:\n{traceback.format_exc()}")
            self.analysis = None
        self.show()

    def show(self):
        text, level = stateLine(self.analysis, self.plan if self.planKey == self._planKey() else None, self._liveModel())
        if str(self.line.text) != text:
            self.line.setText(text)
            self.line.setToolTip(text)
        if level != self._level:
            self._level = level
            ui.bindStyleSheet(self.line, lambda: f"QLabel#StudioAnalysisLine {{ color: {ui.token(self._level).name()}; }}")
        self.openReportButton.setVisible(self.reportPath() is not None)

    def _onTimer(self):
        if library._isAlive(self.widget) and self.widget.isVisible():
            self.refresh()

    def _planKey(self):
        config = planConfig(self.analysis)
        return json.dumps(config, sort_keys=True, default=str) if config is not None else None

    def schedulePlan(self):
        self.planTimer.start()

    def requestPlan(self):
        """The staleness plan of the last run's settings, in the worker thread (only for a finished run)."""
        if self.worker is None:
            return
        if self.analysis is None and self.studyPath:
            try:
                self.analysis = latestAnalysis(self.studyPath)
            except Exception:
                self.analysis = None
        config = planConfig(self.analysis)
        if config is None or (self.analysis.get("status") or {}).get("status") == "running":
            return
        self.worker.request(json.dumps(config, sort_keys=True, default=str), config)

    def _onPlan(self, key, summary, error):
        if error or key != self._planKey():
            return
        self.plan, self.planKey = summary, key
        if library._isAlive(self.widget):
            self.show()

    # Actions

    def reportPath(self):
        out = (self.analysis or {}).get("out")
        path = os.path.join(out, "report.html") if out else None
        return path if path and os.path.isfile(path) else None

    def openInMap(self):
        cardPipeline = _cardPipeline()
        if cardPipeline is not None:
            cardPipeline.showInMap()

    def openReport(self):
        path = self.reportPath()
        cardPipeline = _cardPipeline()
        if path is None or cardPipeline is None:
            return None
        return cardPipeline.openPath(path, _("Cohort analysis report"), _viewers)


_cards = []  # live cards
_viewers = []  # in-app viewers and path dialogs opened by the cards (kept alive)
_widgets = {}  # widget: AnalysisCard


def _createCard(session):
    card = AnalysisCard(session)
    _widgets[card.widget] = card
    card.widget.connect("destroyed()", lambda: (card.dispose(), _widgets.pop(card.widget, None)))
    card.refresh()
    return card.widget


def _refreshCard(widget, session, selectedIds):
    card = _widgets.get(widget)
    if card is not None and library._isAlive(widget):
        card.refresh(selectedIds)


#
# Done hook: "All N lesions traced"
#

_toastedSessions = set()  # id() of the sessions that showed the toast
_hookedSessions = set()


def allTraced(study):
    """``(n, traced)``: the included subjects and whether every one of them is traced (or mapped)."""
    rows = [row for row in study.subjects if str(row.get("include", "1")).strip() == "1"]
    return len(rows), bool(rows) and all(row.get("status") in TRACED for row in rows)


def _checkAllTraced(session):
    studyUi = library._studyUi()
    if studyUi is None or session is not studyUi.session():
        return
    for card in list(_cards):
        card.schedulePlan()
    n, traced = allTraced(session.study)
    if not traced or n < 2 or id(session) in _toastedSessions:
        return
    _toastedSessions.add(id(session))
    shell = library._shell()
    text = _("All {n} lesions traced · run the cohort analysis in Map").format(n=n)
    if shell is None:
        logging.info(f"Studio: {text}")
        return
    cardPipeline = _cardPipeline()
    if cardPipeline is not None:
        shell.toast(text, 6000, action=cardPipeline.showInMap, actionText=_("Open in Map"))
    else:
        shell.toast(text, 6000)


def _doneHook(sid, labelmapArray, ijkToRAS, ctx):
    """StudySession Done hook (order 900): schedules the "all traced" check; no other work, no columns."""
    session = _session()
    if session is not None:
        qt.QTimer.singleShot(ALL_TRACED_DELAY_MS, lambda: _checkAllTraced(session))
    return {}


def _onSession(session):
    """Study listener: install the Done hook once per session and refresh the cards."""
    if session is not None and id(session) not in _hookedSessions and hasattr(session, "addDoneHook"):
        session.addDoneHook(_doneHook, order=DONE_HOOK_ORDER)
        _hookedSessions.add(id(session))


def _install():
    studyUi = library._studyUi()
    if studyUi is None:
        return
    studyUi.addListener(_onSession)
    _onSession(studyUi.session())


def _paletteActions(shell=None):
    """The Search entries of the cohort analysis, also before Map is first opened (``card_pipeline.paletteEntries``)."""
    cardPipeline = _cardPipeline()
    return cardPipeline.paletteEntries(shell) if cardPipeline is not None else []


CARD = {"section": "study", "order": 60, "create": _createCard, "refresh": _refreshCard}
PALETTE_ACTIONS = _paletteActions

try:
    _install()
except Exception:
    logging.error(f"Studio Library: the cohort analysis hooks could not be installed:\n{traceback.format_exc()}")
