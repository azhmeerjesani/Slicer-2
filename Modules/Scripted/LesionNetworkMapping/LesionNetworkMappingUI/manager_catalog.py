"""Get a connectome… action of the connectome manager (L-35, addendum 4.1, decision D-L1).

**Get a connectome…** opens the catalog dialog (``catalog_dialog.py``): the connectomes that can be obtained, with their
licence, size, N, space and ages, the provider's *live* terms text and a destination. Nothing is downloaded until the
user has read the live terms text, ticked "I have read and accept these terms", chosen a destination and clicked
Download. This module holds what outlives the dialog:

- :class:`FetchController` runs the fetch as a light job (``LesionNetworkMappingLib.fetchconnectome``; the archives are
  streamed into a bundle, none is stored unless asked) and shows it in the Studio status chip as
  "Fetching GSP1000 · 41% · 2 h 10 min". **Pause** stops the job (its state, with the member offset of the next
  unconverted subject, is kept next to the bundle); **Resume** starts it again with ``--resume``, and the Range of the
  download restarts at that offset. The finished bundle is registered like any other connectome.
- The job gets ``--parent-pid`` (this application): when the application ends without stopping it (a crash), the job
  stops by itself within a second, keeps its state and can be resumed; it never downloads on unseen.
- An unfinished job is offered for resuming the next time the Map module loads (:func:`offerResume`: a non-modal
  question, one click to resume; never under ``--testing`` and never without a main window; nothing resumes by itself).
  A job that finished without this application seeing it (it ended after the application did) is registered then, and
  the user is told; a job that another process is still running (its lock is live) is reported instead of offered.
- ``ACTIONS`` registers the action in the manager's Manage… menu.

Settings (fork-only, D-S1): ``Studio/LNM/EnableConnectomeFetch`` (read only; absent or true means on, "false" switches
fetching off and leaves link-outs; this code never writes it), ``Studio/LNM/AcceptedTermsText/<sha256>`` (written when the
user ticks the box in the dialog, as the Lead-DBS parity dialog does), ``Studio/LNM/FetchDestination`` (the last folder
chosen) and ``Studio/LNM/ActiveFetch`` (the request of an unfinished job: entry, folder, subset, options, accepted
terms sha256; removed when the job is done). Local paths stay in these settings; no shared file holds one.
Works in Classic and in Studio.
"""

import json
import logging
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import jobrunner

ENABLE_SETTING = "Studio/LNM/EnableConnectomeFetch"
ACCEPTED_TERMS_TEXT_SETTING = "Studio/LNM/AcceptedTermsText"
DESTINATION_SETTING = "Studio/LNM/FetchDestination"
ACTIVE_FETCH_SETTING = "Studio/LNM/ActiveFetch"
JOB_MODULE = "LesionNetworkMappingLib.fetchconnectome"
#: Time after the Map module loaded before an unfinished fetch is offered
OFFER_DELAY_MS = 4000
#: Lowered priority of the fetch job (it runs for hours next to the user's own work)
FETCH_NICE = 5
STATES = ("idle", "running", "paused", "failed", "done")


def _settingText(key):
    value = qt.QSettings().value(key)
    return "" if value is None else str(value)


def fetchEnabled():
    """False only when the user switched fetching off (``Studio/LNM/EnableConnectomeFetch`` is "false"); read only."""
    return _settingText(ENABLE_SETTING).strip().lower() not in ("false", "0", "no", "off")


def termsAccepted(sha):
    """Whether the user accepted the terms text with this sha256 (``Studio/LNM/AcceptedTermsText/<sha>``)."""
    return bool(sha) and _settingText(f"{ACCEPTED_TERMS_TEXT_SETTING}/{sha}").lower() in ("true", "1")


def setTermsAccepted(sha, accepted):
    """Store or forget the acceptance of one terms text (called only from the user's tick of the box)."""
    if not sha:
        return
    settings = qt.QSettings()
    key = f"{ACCEPTED_TERMS_TEXT_SETTING}/{sha}"
    if accepted:
        settings.setValue(key, "true")
    else:
        settings.remove(key)


def lastDestination():
    return _settingText(DESTINATION_SETTING)


def setLastDestination(path):
    qt.QSettings().setValue(DESTINATION_SETTING, path)


def _loadCatalog(catalogPath=None):
    from LesionNetworkMappingLib import catalog

    return catalog.load_catalog(catalogPath or catalog.CONNECTOMES)


def _formatEta(seconds):
    text = jobrunner.formatEta(seconds)
    return text[: -len(" left")] if text.endswith(" left") else text


class FetchController:
    """The connectome fetch job of this application: start, pause, resume, status (one at a time).

    ``observers`` are callables without arguments, called on the main thread whenever the status changes (progress
    events included). ``catalogPath`` and ``extraArgs`` exist for tests (a catalog file; ``--server URL`` of a fake
    Dataverse); the interface never sets them.
    """

    def __init__(self):
        self.job = None
        self.request = None
        self.state = "idle"
        self.error = None
        self.errorEvent = None
        self.resultPath = None
        self.observers = []
        self.catalogPath = None
        self.extraArgs = []
        self.lastRequestError = None

    # Requests

    def isRunning(self):
        return self.job is not None and not self.job.isFinished()

    def entry(self, request=None):
        request = request or self.request
        if not request:
            return None
        try:
            return _loadCatalog(self.catalogPath).get(request["entry"])
        except Exception:
            logging.exception("Lesion network mapping: the connectome catalog could not be read")
            return None

    def shortName(self, request=None):
        from LesionNetworkMappingLib import fetchconnectome

        entry = self.entry(request)
        return fetchconnectome.bundle_name(entry) if entry is not None else _("connectome")

    def checkRequest(self, request):
        """The reason a request may not start (a text), or None when it may."""
        if not fetchEnabled():
            return _("Downloading connectomes is switched off ({key} is false); the catalog only links to the sources.").format(key=ENABLE_SETTING)
        entry = self.entry(request)
        if entry is None:
            return _("This connectome is not in the catalog.")
        from LesionNetworkMappingLib import fetchconnectome, jobs

        try:
            fetchconnectome.check_fetchable(entry)
        except jobs.JobError as exc:
            return str(exc.message)
        if not termsAccepted(request.get("termsSha")):
            return _("Read and accept the provider's terms first: nothing is downloaded before.")
        if not request.get("dest"):
            return _("Choose a destination folder.")
        return None

    def arguments(self, request, resume=False, acceptChecksumMismatch=False):
        args = ["--entry", request["entry"], "--dest", request["dest"], "--terms-sha", request["termsSha"], "--streams", str(int(request.get("streams") or 2))]
        if request.get("subset"):
            args += ["--subset", str(request["subset"])]
        if request.get("keepArchives"):
            args.append("--keep-archives")
        if resume:
            args.append("--resume")
        if acceptChecksumMismatch:
            args.append("--accept-checksum-mismatch")
        if self.catalogPath:
            args += ["--catalog", self.catalogPath]
        # Stop with this application: the PythonSlicer launcher outlives a crashed application, so the job's own orphan watchdog does not notice
        args += ["--parent-pid", str(os.getpid())]
        return args + list(self.extraArgs)

    def start(self, request, resume=False, acceptChecksumMismatch=False):
        """Start the job for ``request`` (``entry``, ``dest``, ``subset``, ``keepArchives``, ``streams``, ``termsSha``).

        Returns the job, or None with ``lastRequestError`` set when the request may not start: fetching is switched off, the
        entry is not fetchable, or the user has not accepted the terms text with this sha256.
        """
        self.lastRequestError = self.checkRequest(request)
        if self.lastRequestError or self.isRunning():
            if not self.lastRequestError:
                self.lastRequestError = _("A connectome is being fetched already.")
            return None
        self.request = dict(request)
        self.error = self.errorEvent = self.resultPath = None
        spec = jobrunner.JobSpec(JOB_MODULE, self.arguments(request, resume, acceptChecksumMismatch), title=_("Fetching {name}").format(name=self.shortName()), heavy=False, kind="fetch",
                                 name="fetch", nice=FETCH_NICE, runRoot=jobrunner.defaultRunRoot(), onEvent=self._onEvent, onDone=self._onDone, statusText=self._statusText)
        qt.QSettings().setValue(ACTIVE_FETCH_SETTING, json.dumps(self.request))
        self.state = "running"
        self.job = jobrunner.instance().submit(spec)
        self._notify()
        return self.job

    def pause(self):
        """Stop the job; what was converted is kept and Resume continues at the next unconverted subject."""
        if self.isRunning():
            self.job.stop()
            self._notify()

    def resume(self, acceptChecksumMismatch=False):
        request = self.request or self.savedRequest()
        if not request:
            return None
        return self.start(request, resume=True, acceptChecksumMismatch=acceptChecksumMismatch)

    def discard(self):
        """Forget an unfinished job's request (its files stay in the destination folder)."""
        qt.QSettings().remove(ACTIVE_FETCH_SETTING)
        self.state = "idle" if not self.isRunning() else self.state
        self._notify()

    # Saved job

    def savedRequest(self):
        text = _settingText(ACTIVE_FETCH_SETTING)
        if not text:
            return None
        try:
            request = json.loads(text)
        except ValueError:
            return None
        return request if isinstance(request, dict) and request.get("entry") and request.get("dest") else None

    def unfinished(self):
        """``(request, summary)`` of an interrupted job that can be resumed, or None (a finished or missing one is forgotten).

        A saved job whose bundle is finished (the job ended after the application that started it) is registered first
        (:meth:`adoptFinished`); a saved job that another live process is running (:meth:`runningElsewhere`) is not offered.
        """
        from LesionNetworkMappingLib import fetchconnectome

        request = self.request if self.state in ("paused", "failed") else self.savedRequest()
        if not request or self.isRunning():
            return None
        entry = self.entry(request)
        if entry is None:
            return None
        if self.runningElsewhere(request) is not None:
            return None  # Resume would fail on its lock; the saved request stays until that job ends
        summary = fetchconnectome.unfinished_fetch(entry, request["dest"])
        if summary is None:
            self.adoptFinished(request)
            # Forget the saved request only when the folder is reachable and holds no unfinished state (an unmounted drive does not forget it)
            if _settingText(ACTIVE_FETCH_SETTING) and not self.isRunning() and os.path.isdir(request["dest"]):
                qt.QSettings().remove(ACTIVE_FETCH_SETTING)
            return None
        return request, summary

    def runningElsewhere(self, request=None):
        """The lock holder (``{pid, host, started}``) of the saved job when another live process is running it, else None."""
        from LesionNetworkMappingLib import fetchconnectome

        request = request or self.savedRequest()
        if not request or self.isRunning():
            return None
        entry = self.entry(request)
        if entry is None:
            return None
        try:
            return fetchconnectome.running_fetch(entry, request["dest"])
        except OSError:
            return None

    def adoptFinished(self, request=None):
        """Register the bundle of the saved job when the job finished without this application seeing it; return its path or None.

        That happens when the job ended after the application that started it (for example while it was closing); the bundle
        is registered like the one of a job this application ran, and the saved request is forgotten.
        """
        from LesionNetworkMappingLib import fetchconnectome

        request = request or self.savedRequest()
        if not request or self.isRunning() or request != self.savedRequest():
            return None
        entry = self.entry(request)
        if entry is None:
            return None
        bundle = fetchconnectome.finished_fetch(entry, request["dest"])
        if bundle is None or not self._register(bundle):
            return None
        qt.QSettings().remove(ACTIVE_FETCH_SETTING)
        self.request = dict(request)
        self.state = "done"
        self.error = self.errorEvent = None
        self.resultPath = bundle
        self._notify()
        return bundle

    # Events

    def _statusText(self, job):
        text = _("Fetching {name}").format(name=self.shortName())
        if job.progress is not None:
            text += f" · {int(job.progress * 100)}%"
        eta = _formatEta(job.eta)
        if eta:
            text += f" · {eta}"
        return text

    def _onEvent(self, event):
        if event.get("event") == "result" and event.get("kind") == "connectome":
            self.resultPath = event.get("path")
        self._notify()

    def _onDone(self, job):
        if job.succeeded():
            self.state = "done"
            qt.QSettings().remove(ACTIVE_FETCH_SETTING)
            if self.resultPath:
                self._register(self.resultPath)
        elif job.state == "cancelled":
            self.state = "paused"
        else:
            self.state = "failed"
            self.errorEvent = job.errorEvent or {}
            code = self.errorEvent.get("code")
            if code == "terms_mismatch":
                self.error = _("The provider's terms text is not the text you accepted (it has changed). Open Get a connectome…, read the live text and accept it to continue. Nothing more was downloaded.")
            elif code == "checksum":
                # The job's own text names its command-line flag; the interface offers the same choice as a button
                self.error = _("{name}: the checksum does not match the one the provider lists, also after reading it twice. "
                               "Every file that was converted from it passed its integrity check. Choose Keep this data anyway… to keep it "
                               "(recorded as checksum_override), or delete the folder and start again.").format(name=self.errorEvent.get("file") or self.shortName())
            else:
                self.error = job.failureReason or _("The download failed.")
        self._notify()

    def _register(self, path):
        """Register the finished bundle like any other connectome, and show it in the Map module's list."""
        try:
            from LesionNetworkMapping import LesionNetworkMappingLogic

            LesionNetworkMappingLogic.registerConnectome(path)
        except Exception:
            logging.exception("Lesion network mapping: the fetched connectome could not be registered")
            return False
        try:
            module = getattr(slicer.modules, "lesionnetworkmapping", None)
            widget = module.widgetRepresentation().self() if module is not None else None
            if widget is not None and hasattr(widget, "refreshConnectomes"):
                widget.refreshConnectomes(select=path)
        except Exception:
            logging.exception("Lesion network mapping: the connectome list could not be refreshed")
        return True

    def status(self):
        """A dict for the interface: ``state``, ``fraction`` (0..1 or None), ``eta`` (text), ``archive``, ``subjects``, ``name``,
        ``error``, ``mismatch`` (the failure is a checksum mismatch the user may accept), ``termsChanged`` (the provider's terms text changed: only accepting the live text helps) and ``path`` of a finished bundle.
        """
        job = self.job
        event = (job.lastEvent or {}) if job is not None else {}
        fraction = job.progress if job is not None and job.progress is not None else None
        done = event.get("done") if event.get("event") == "progress" else None
        return {
            "state": self.state,
            "fraction": fraction,
            "eta": _formatEta(job.eta) if job is not None and self.state == "running" else "",
            "archive": event.get("archive"),
            "archiveIndex": event.get("archive_index"),
            "archiveCount": event.get("archive_count"),
            "subjects": event.get("subjects"),
            "bytes": done,
            "total": event.get("total"),
            "name": self.shortName() if self.request else "",
            "error": self.error,
            "mismatch": bool(self.errorEvent and self.errorEvent.get("code") == "checksum"),
            "termsChanged": bool(self.errorEvent and self.errorEvent.get("code") == "terms_mismatch"),
            "path": self.resultPath,
            "warnings": [w.get("message") for w in (job.warnings if job is not None else [])],
        }

    def _notify(self):
        for observer in list(self.observers):
            try:
                observer()
            except Exception:
                logging.exception("Lesion network mapping: a fetch observer failed")


_controller = None
_dialogs = {}
_offered = {"done": False}


def controller():
    """The application's one :class:`FetchController`."""
    global _controller
    if _controller is None:
        _controller = FetchController()
    return _controller


def openCatalog(ctx=None, bundlePath=None):
    """Show the catalog dialog (non-modal, one per application) and return it."""
    from .catalog_dialog import CatalogDialog

    dialog = _dialogs.get("catalog")
    if dialog is None:
        dialog = CatalogDialog(ctx)
        _dialogs["catalog"] = dialog
    else:
        dialog.ctx = ctx
        dialog.refresh()
    dialog.show()
    dialog.raise_()
    return dialog


def _showInformation(objectName, title, text):
    """A non-modal information box over the main window (kept in ``_dialogs``)."""
    box = qt.QMessageBox(qt.QMessageBox.Information, title, text, qt.QMessageBox.Ok, slicer.util.mainWindow())
    box.setObjectName(objectName)
    box.setModal(False)
    _dialogs[objectName] = box
    box.show()
    return box


def offerResume():
    """Offer to resume an unfinished fetch (a non-modal question; one click). Never automatic, never under ``--testing``.

    A saved fetch that finished after the application ended is registered and reported instead; one that another live
    process is still running is reported (where it writes, and which process) instead of offered.
    """
    if _offered["done"]:
        return False
    app = getattr(slicer, "app", None)
    if app is None or app.testingEnabled() or slicer.util.mainWindow() is None or not fetchEnabled():
        return False
    _offered["done"] = True
    current = controller()
    saved = current.savedRequest()
    holder = current.runningElsewhere(saved) if saved else None
    if holder is not None:
        _showInformation("LNMFetchRunningElsewhere", _("Download still running"),
                         _("Fetching {name} into {dest} is still running in another process (process {pid} on {host}, since {started}). It continues there; "
                           "this application does not show its progress.").format(name=current.shortName(saved), dest=saved["dest"], pid=holder.get("pid"), host=holder.get("host"),
                                                                                  started=holder.get("started")))
        return True
    found = current.unfinished()
    if found is None:
        if saved and current.state == "done" and current.resultPath:
            _showInformation("LNMFetchFinishedNotice", _("Download finished"),
                             _("Fetching {name} finished after Slicer was closed. The connectome is registered and ready in the Connectome card.").format(name=current.shortName(saved)))
            return True
        return False
    request, summary = found
    percent = f" ({int(summary['fraction'] * 100)}%)" if summary.get("fraction") is not None else ""
    box = qt.QMessageBox(qt.QMessageBox.Question, _("Unfinished download"), _("Fetching {name} stopped before it was finished{percent}. Resume it? It continues where it stopped; nothing is downloaded until you click Resume.").format(name=current.shortName(request), percent=percent),
                         qt.QMessageBox.NoButton, slicer.util.mainWindow())
    box.setObjectName("LNMFetchResumeOffer")
    resume = box.addButton(_("Resume"), qt.QMessageBox.AcceptRole)
    box.addButton(_("Not now"), qt.QMessageBox.RejectRole)
    box.setModal(False)

    def answered(result):
        if box.clickedButton() == resume:
            dialog = openCatalog(None)
            current.request = request
            if current.resume() is None and current.lastRequestError:
                dialog.showNotice(current.lastRequestError)

    box.connect("finished(int)", answered)
    _dialogs["offer"] = box
    box.show()
    return True


def _scheduleOffer():
    try:
        if slicer.app.testingEnabled() or slicer.util.mainWindow() is None:
            return
        qt.QTimer.singleShot(OFFER_DELAY_MS, offerResume)
    except Exception:
        logging.exception("Lesion network mapping: the resume offer could not be scheduled")


ACTIONS = [
    # "always": the action needs no selected connectome (it is how a first one is obtained)
    {"id": "getConnectome", "label": _("Get a connectome…"), "order": 5, "factory": openCatalog, "always": True},
]

_scheduleOffer()
