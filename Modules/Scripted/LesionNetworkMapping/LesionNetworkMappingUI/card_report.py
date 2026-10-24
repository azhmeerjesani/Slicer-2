"""Battery strip and Report card of the Map module (order 1 and 140; L-40, LNM addendum §3.11 and §7.1).

Two plug-ins in one file, both built from the run registry and the files of the open study through
``LesionNetworkMappingLib.checklist`` and ``.methods`` (nothing here computes statistics):

- **Battery strip** (``HEADER``, id "battery", ``LNMBatteryHeader``): six dots (grey not run, green run, amber run and flagged
  or only partly run) for Degree/PC1 QA, Null reference, Specificity, Validation vs baselines, Robustness and Provenance &
  sharing. Each dot has a glyph as well as a colour (check mark, exclamation mark, nothing). Clicking a dot scrolls to
  and expands the card the check belongs to (QA, Nulls, Cohort, Validate, Robust, Report). The strip refreshes on
  ``ctx.runChanged``, ``ctx.studyChanged`` and when a run finishes. It never blocks anything.
- **Report card** (``CARD``, id "report", ``LNMReportCard``):

  - the 15-item checklist table (done / not run / N/A / flagged, each with a glyph, the value and the evidence run links
    shown under the table for the selected row; a link opens the run folder or the file);
  - a methods preview (the generated methods rendered in a ``QTextBrowser``) with **Copy as Markdown** and **Copy plain text**;
  - **Open report** (``report.html`` of the newest pipeline run, A-04b: the card links to it, no second HTML report);
  - **Export publication bundle…**: copies the files of ``export.publication_manifest`` (unthresholded maps, p maps,
    design files, the PALM export, reproduce scripts) and writes ``methods.*``, ``checklist.md``, ``neurovault/images.json``
    and ``manifest.json`` into ``derivatives/lnm/publication/<UTC>/``. Checklist items that were not run, or flagged,
    are listed under the button as warnings; they never block the export. Only a folder that cannot be written does;
  - the research-only disclaimer is always shown.

The root of the checklist is the open study (``MapContext.study``); ``ctx.reportRoot`` (a folder holding
``derivatives/lnm/runs.jsonl``) may be set to use another one (tests). Without either, the strip is grey and the card
says what to do. Works in Classic and without a main window. Writes nothing at start; the bundle is written only when
the button is pressed, under the study. Shared files hold relative paths only (reproduce scripts keep theirs by design).
"""

import datetime
import html
import logging
import os
import shutil

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_BATTERY, ORDER_REPORT, widgets

#: The strip and the card must fit the inspector (SPEC 7.9)
DOT_SIZE = 18
BUNDLE_PARENT = ("derivatives", "lnm", "publication")
#: Battery dot id -> card that holds the check (``MapContext.focusCard``); the Report card is the fallback
DOT_CARDS = {
    "degree_qa": "qa",
    "null_reference": "nulls",
    "specificity": "cohort",
    "validation": "validate",
    "robustness": "robust",
    "provenance": "report",
}
DOT_COLOURS = {"grey": "#8a8f98", "green": "#2e9d5b", "amber": "#d9962b"}
DOT_GLYPHS = {"grey": "", "green": "✓", "amber": "!"}
DOT_NAMES = {"grey": "not run", "green": "run", "amber": "run, needs attention"}
STATUS_GLYPHS = {"done": "✓", "not_run": "○", "na": "\u2013", "flagged": "⚠"}
STATUS_COLOURS = {"done": "#2e9d5b", "not_run": "#8a8f98", "na": "#8a8f98", "flagged": "#d9962b"}


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _lib():
    from LesionNetworkMappingLib import checklist, export, methods

    return checklist, export, methods


def reportRoot(ctx):
    """Folder the checklist and methods are read from: ``ctx.reportRoot``, else the open study; None when there is none."""
    path = getattr(ctx, "reportRoot", None)
    if not path:
        study = getattr(ctx, "study", None)
        path = getattr(study, "path", None) if study is not None else None
    return os.path.abspath(os.fspath(path)) if path else None


#
# Model: checklist, battery and methods of a root (shared by the strip and the card)
#


class ReportState:
    """Checklist items, battery dots, methods text and warnings of a root, evaluated by :func:`evaluate`."""

    def __init__(self, root, items, dots, sections, records):
        self.root = root
        self.items = items
        self.dots = dots
        self.sections = sections
        self.records = records
        self._text = {}

    def methodsText(self, fmt="md"):
        """The methods rendered as ``fmt`` ("md", "txt" or "html"); "" when there is no root."""
        if self.sections is None:
            return ""
        if fmt not in self._text:
            _checklist, _export, methods = _lib()
            self._text[fmt] = methods.render(self.sections, fmt)
        return self._text[fmt]

    def warnings(self):
        """Items that were not run or are flagged: [(id, title, status, value)]; they never block anything."""
        return [(item["id"], item["title"], item["status"], item["value"]) for item in self.items if item["status"] in ("not_run", "flagged")]


def evaluate(root, withMethods=True):
    """Evaluate the checklist, the battery and (``withMethods``) the methods of ``root`` (None: all items not run)."""
    checklist, _export, methods = _lib()
    if not root:
        items = [{"id": item.id, "number": item.number, "title": item.title, "status": checklist.NOT_RUN, "value": "no study open", "evidence": []} for item in checklist.ITEMS]
        return ReportState(None, items, checklist.battery(None, items=items), None, None)
    records = methods.Records(root)
    items = checklist.evaluate(root, records=records)
    dots = checklist.battery(root, items=items)
    sections = None
    if withMethods:
        try:
            sections = methods.methods_sections(root, records=records)
        except Exception:
            logging.exception("Lesion network mapping: the methods could not be generated")
    return ReportState(root, items, dots, sections, records)


def latestReport(root):
    """Path of ``report.html`` of the newest pipeline run of ``root`` that wrote one, or None."""
    if not root:
        return None
    try:
        from LesionNetworkMappingLib import runs

        records = list(runs.load(root).values())
    except Exception:
        return None
    for record in reversed(records):
        if record.get("kind") != "pipeline":
            continue
        folder = runs.run_dir(root, record)
        path = os.path.join(folder, "report.html") if folder else None
        if path and os.path.isfile(path):
            return path
    return None


def _evidenceTarget(root, token):
    """Absolute path an evidence token points to ("file:<relative>" or a run id), or None."""
    if not root:
        return None
    if token.startswith("file:"):
        return os.path.join(root, *token[5:].split("/"))
    try:
        from LesionNetworkMappingLib import runs

        record = runs.load(root).get(token)
        return runs.run_dir(root, record) if record else None
    except Exception:
        return None


#
# Publication bundle (no Qt)
#


def _bundleFolder(root, now=None):
    stamp = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y%m%dT%H%M%SZ")
    parent = os.path.join(root, *BUNDLE_PARENT)
    out, number = os.path.join(parent, stamp), 1
    while os.path.exists(out):
        number += 1
        out = os.path.join(parent, f"{stamp}-{number}")
    return out


def _scrub(text, root):
    """``text`` without the absolute path of the study (warnings are stored in the shared manifest, SPEC 9.7)."""
    text = str(text)
    variants = {root, os.path.realpath(root), root.replace(os.sep, "/")}
    for variant in sorted(variants, key=len, reverse=True):
        if variant and variant != os.sep:
            text = text.replace(variant, "<study>")
    return text


def _fallbackManifest(root, export, warnings):
    """A file list like ``export.publication_manifest`` that skips unreadable or dangling files (each becomes a warning)."""
    bases = [folder for folder in (os.path.join(root, "derivatives", "lnm"), os.path.join(root, "neurovault")) if os.path.isdir(folder)] or [root]
    skipped = getattr(export, "_SKIPPED_FOLDERS", ())
    files, seen, group = [], set(), False
    for base in bases:
        for folder, folders, names in os.walk(base):
            folders[:] = sorted(name for name in folders if name not in skipped and not name.startswith("."))
            for name in sorted(names):
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, root).replace(os.sep, "/")
                try:
                    kind = "neurovault" if name == "images.json" and os.path.basename(folder) == "neurovault" else export._publication_kind(name, names)
                    if kind is None:
                        continue
                    real = os.path.realpath(path)
                    if real in seen:
                        continue
                    size = os.path.getsize(path)
                except OSError as exc:
                    warnings.append(f"{rel} was skipped: {_scrub(exc, root)}")
                    continue
                if name in ("glm.json", "slnm.json"):
                    group = True
                seen.add(real)
                files.append({"path": rel, "kind": kind, "bytes": size})
    required = ["unthresholded_map", "reproduce", "checklist", "neurovault"]
    if group:
        required[1:1] = ["p_map", "design", "palm_export"]
    present = {entry["kind"] for entry in files}
    return {"files": files, "missing": [kind for kind in required if kind not in present]}


def exportBundle(root, now=None):
    """Write a publication bundle of ``root`` into ``derivatives/lnm/publication/<UTC>/`` and return a dict.

    Copies the files of ``export.publication_manifest`` (relative paths below ``derivatives/lnm`` kept; earlier bundles are
    not copied), writes ``methods.md/.html/.txt`` and ``checklist.md`` from the current registry, regenerates
    ``neurovault/images.json`` for the copied maps (nothing is uploaded) and writes ``manifest.json``.

    Checklist items that were not run or are flagged, and kinds of files that are absent, are returned as ``warnings``
    (a list of strings); they never stop the export. Failures of single steps are warnings too.

    :return: {"dir", "files": [relative paths], "warnings": [str], "missing": [kinds], "items", "dots"}.
    :raises OSError: when the bundle folder cannot be created.
    """
    checklist, export, _methods = _lib()
    root = os.path.abspath(os.fspath(root))
    if not os.path.isdir(root):
        raise OSError("The study folder does not exist.")
    out = _bundleFolder(root, now)
    os.makedirs(out)
    warnings = []
    prefix = "/".join(BUNDLE_PARENT) + "/"

    items, dots = [], []
    try:
        written = export.write_methods(root, out, formats=("md", "html", "txt"), checklist=True)
        items, dots = written.get("items") or [], written.get("battery") or []
    except Exception as exc:
        logging.exception("Lesion network mapping: the methods of the bundle could not be written")
        warnings.append(f"The methods and the checklist could not be written: {_scrub(exc, root)}")
        try:
            state = evaluate(root, withMethods=False)
            items, dots = state.items, state.dots
        except Exception:
            pass
    for item in items:
        if item["status"] in ("not_run", "flagged"):
            warnings.append(f"{item['id']} {item['title']}: {checklist.STATUS_LABELS[item['status']]} ({item['value']})")

    manifest = {"files": [], "missing": []}
    try:
        manifest = export.publication_manifest(root, hashes=False)
    except Exception as exc:
        logging.exception("Lesion network mapping: the publication manifest could not be built")
        warnings.append(f"The file list was built file by file: {_scrub(exc, root)}")
        try:
            manifest = _fallbackManifest(root, export, warnings)
        except Exception as exc2:
            logging.exception("Lesion network mapping: the fallback file list failed")
            warnings.append(f"The file list could not be built: {_scrub(exc2, root)}")
    fresh = {"methods.md", "methods.html", "methods.txt", "checklist.md"}
    copied, maps = [], []
    for entry in manifest.get("files", []):
        rel = entry["path"]
        if rel.startswith(prefix) or entry["kind"] == "neurovault":
            continue
        dest = rel[len("derivatives/lnm/") :] if rel.startswith("derivatives/lnm/") else rel
        if dest in fresh and entry["kind"] in ("methods", "checklist"):
            continue  # replaced by the files written from the current registry
        if os.path.exists(os.path.join(out, *dest.split("/"))):
            dest = rel
        try:
            target = os.path.join(out, *dest.split("/"))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, *rel.split("/")), target)
        except OSError as exc:
            warnings.append(f"{rel} was not copied: {_scrub(exc, root)}")
            continue
        copied.append((dest, entry["kind"]))
        if entry["kind"] in ("unthresholded_map", "p_map"):
            maps.append({"path": os.path.join(root, *rel.split("/")), "file": dest})
    if not any(os.path.basename(dest) == "reproduce.sh" and "/" not in dest for dest, kind in copied):
        scripts = [dest for dest, kind in copied if kind == "reproduce"]
        if scripts:
            try:
                shutil.copy2(os.path.join(out, *scripts[-1].split("/")), os.path.join(out, "reproduce.sh"))
                copied.append(("reproduce.sh", "reproduce"))
            except OSError as exc:
                warnings.append(f"reproduce.sh was not copied: {_scrub(exc, root)}")

    try:
        export.neurovault_metadata(maps, out=os.path.join(out, "neurovault", "images.json"), root=root)
    except Exception as exc:
        logging.exception("Lesion network mapping: the NeuroVault metadata could not be written")
        warnings.append(f"The NeuroVault metadata could not be written: {_scrub(exc, root)}")

    files = []
    for folder, folders, names in os.walk(out):
        folders.sort()
        for name in sorted(names):
            path = os.path.join(folder, name)
            rel = os.path.relpath(path, out).replace(os.sep, "/")
            if rel == "manifest.json":
                continue
            kind = dict(copied).get(rel) or ("neurovault" if rel == "neurovault/images.json" else export._publication_kind(name, names))
            files.append({"path": rel, "kind": kind or "other", "bytes": os.path.getsize(path), "sha256": export.file_sha256(path)})
    present = {entry["kind"] for entry in files}
    missing = [kind for kind in manifest.get("missing", []) if kind not in present]
    if missing:
        warnings.append("Not found in the study: " + ", ".join(kind.replace("_", " ") for kind in missing))
    content = {"format": export.PUBLICATION_FORMAT, "version": 1, "created": export.now_iso(), "files": files, "missing": missing, "warnings": warnings, "disclaimer": export.DISCLAIMER}
    export.write_json_atomic(os.path.join(out, "manifest.json"), content)
    return {"dir": out, "files": [entry["path"] for entry in files] + ["manifest.json"], "warnings": warnings, "missing": missing, "items": items, "dots": dots}


#
# Battery strip
#


class _Debounce:
    """Run ``callback`` once after a burst of triggers (observers fire several times per run)."""

    def __init__(self, callback, ms=60):
        self.callback = callback
        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(ms)
        self.timer.connect("timeout()", self._fire)

    def trigger(self):
        self.timer.start()

    def _fire(self):
        try:
            self.callback()
        except Exception:
            logging.exception("Lesion network mapping: the report refresh failed")

    def stop(self):
        self.timer.stop()


def _cardAvailable(ctx, cardId):
    """Whether card ``cardId`` exists and is offered in the current seed mode and interface."""
    widget = _mapWidget()
    entries = getattr(widget, "_cardWidgets", None) if widget is not None else None
    if entries is not None:
        entry = entries.get(cardId)
        if entry is None:
            return False
        try:
            from LesionNetworkMappingUI import visibleSpecs

            return bool(visibleSpecs([entry[0]], widget.seedMode(), widget._classic()))
        except Exception:
            return True
    return ctx.card(cardId) is not None


class BatteryStrip:
    """The battery strip (``HEADER`` factory)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.state = evaluate(None, withMethods=False)
        self._cleanedUp = False
        self.widget = qt.QFrame()
        self.widget.setObjectName("LNMBatteryHeader")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(6, 4, 6, 4)  # aligned with the content of the cards below
        layout.setSpacing(2)
        row = qt.QHBoxLayout()
        row.setSpacing(6)
        title = qt.QLabel(_("Reporting"))
        title.setObjectName("LNMBatteryTitle")
        title.setProperty("slicerRole", "secondaryText")
        row.addWidget(title)
        self.dotButtons = {}
        checklist, _export, _methods = _lib()
        for dotId, label, _items in checklist.BATTERY:
            button = qt.QToolButton()
            button.setObjectName(f"LNMBatteryDot_{dotId}")
            button.setFixedSize(DOT_SIZE, DOT_SIZE)
            button.setCursor(qt.Qt.PointingHandCursor)
            button.setFocusPolicy(qt.Qt.StrongFocus)
            button.connect("clicked()", lambda dotId=dotId: self.onDotClicked(dotId))
            row.addWidget(button)
            self.dotButtons[dotId] = button
        row.addStretch(1)
        layout.addLayout(row)
        self.summaryLabel = qt.QLabel("")
        self.summaryLabel.setObjectName("LNMBatterySummary")
        self.summaryLabel.setWordWrap(True)
        self.summaryLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.summaryLabel.setProperty("slicerRole", "secondaryText")
        layout.addWidget(self.summaryLabel)
        self._debounce = _Debounce(self.refresh)
        for name in ("runChanged", "studyChanged"):
            getattr(ctx, name).append(self._debounce.trigger)
        self._paint()
        qt.QTimer.singleShot(0, self.refresh)

    def refresh(self):
        """Re-read the registry and repaint the dots."""
        if self._cleanedUp:
            return
        try:
            self.state = evaluate(reportRoot(self.ctx), withMethods=False)
        except Exception:
            logging.exception("Lesion network mapping: the battery could not be evaluated")
            self.state = evaluate(None, withMethods=False)
        self._paint()

    def _paint(self):
        counts = {"grey": 0, "green": 0, "amber": 0}
        for dot in self.state.dots:
            counts[dot["colour"]] += 1
            button = self.dotButtons.get(dot["id"])
            if button is None:
                continue
            colour = dot["colour"]
            button.setText(DOT_GLYPHS[colour])
            button.setStyleSheet(
                f"QToolButton#LNMBatteryDot_{dot['id']} {{ background-color: {DOT_COLOURS[colour]}; color: white; border: none; border-radius: {DOT_SIZE // 2}px; font-size: 11px; font-weight: bold; }}"
                f"QToolButton#LNMBatteryDot_{dot['id']}:focus {{ border: 2px solid palette(highlight); }}",
            )
            button.setProperty("batteryColour", colour)
            tip = f"{dot['label']} · {DOT_NAMES[colour]}"
            if dot["text"]:
                tip += "\n" + dot["text"]
            tip += "\n" + _("Click to go to the card")
            button.setToolTip(tip)
            button.setAccessibleName(f"{dot['label']}: {DOT_NAMES[colour]}")
        run = counts["green"] + counts["amber"]
        text = _("{n} of 6 checks run").format(n=run)
        if counts["amber"]:
            text += " · " + _("{n} need attention").format(n=counts["amber"])
        if self.state.root is None:
            text = _("Open a study to track the reporting checks")
        self.summaryLabel.setText(text)

    def onDotClicked(self, dotId):
        """Scroll to and expand the card of a dot (the Report card when that card is not offered here)."""
        target = DOT_CARDS.get(dotId, "report")
        if not _cardAvailable(self.ctx, target) or not self.ctx.focusCard(target):
            self.ctx.focusCard("report")

    # Card protocol

    def onRunEvent(self, event):
        if event.get("event") in ("done", "error", "cancelled", "failed"):
            self._debounce.trigger()

    def onStudyChanged(self):
        self._debounce.trigger()

    def onResultChanged(self):
        pass

    def cleanup(self):
        self._cleanedUp = True
        self._debounce.stop()
        for name in ("runChanged", "studyChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self._debounce.trigger in callbacks:
                callbacks.remove(self._debounce.trigger)


#
# Report card
#


class ReportCard:
    """The Report card (``CARD`` factory)."""

    COLUMNS = (_("Item"), _("Status"), _("Value"))

    def __init__(self, ctx):
        self.ctx = ctx
        self.state = evaluate(None)
        self.lastBundle = None
        self._cleanedUp = False
        self.widget = qt.QFrame()
        self.widget.setObjectName("LNMReportCard")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.summaryLabel = self._label("LNMReportSummary")
        layout.addWidget(self.summaryLabel)

        self.table = widgets.tableView(self.COLUMNS)
        self.table.setObjectName("LNMReportChecklist")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        header.setSectionResizeMode(1, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, qt.QHeaderView.Stretch)
        self.table.setVerticalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        self.table.setTextElideMode(qt.Qt.ElideRight)
        self.table.setToolTip(_("Reporting checklist · select a row to see its value and evidence"))
        self.table.connect("itemSelectionChanged()", self.onSelectionChanged)
        layout.addWidget(self.table)

        self.detailLabel = self._label("LNMReportDetail", role="secondaryText")
        self.detailLabel.setTextFormat(qt.Qt.RichText)
        self.detailLabel.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
        self.detailLabel.connect("linkActivated(QString)", self.onLinkActivated)
        layout.addWidget(self.detailLabel)

        layout.addWidget(self._label("LNMReportMethodsTitle", _("Methods preview"), role="secondaryText"))
        self.methodsView = qt.QTextBrowser()
        self.methodsView.setObjectName("LNMReportMethods")
        self.methodsView.setOpenLinks(False)
        self.methodsView.setMinimumHeight(150)
        self.methodsView.setMaximumHeight(260)
        self.methodsView.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        layout.addWidget(self.methodsView)

        row = qt.QHBoxLayout()
        row.setSpacing(4)
        self.copyMdButton = qt.QPushButton(_("Copy as Markdown"))
        self.copyMdButton.setObjectName("LNMReportCopyMarkdown")
        self.copyMdButton.setToolTip(_("Copy the methods as Markdown to the clipboard"))
        self.copyMdButton.connect("clicked()", lambda: self.copyMethods("md"))
        row.addWidget(self.copyMdButton)
        self.copyTxtButton = qt.QPushButton(_("Copy plain text"))
        self.copyTxtButton.setObjectName("LNMReportCopyText")
        self.copyTxtButton.setToolTip(_("Copy the methods as plain text to the clipboard"))
        self.copyTxtButton.connect("clicked()", lambda: self.copyMethods("txt"))
        row.addWidget(self.copyTxtButton)
        layout.addLayout(row)
        self.copyStatus = self._label("LNMReportCopyStatus", role="secondaryText")
        self.copyStatus.hide()
        layout.addWidget(self.copyStatus)

        row = qt.QHBoxLayout()
        row.setSpacing(4)
        self.reportButton = qt.QPushButton(_("Open report"))
        self.reportButton.setObjectName("LNMReportOpenHtml")
        self.reportButton.setProperty("slicerRole", "secondary")
        self.reportButton.connect("clicked()", self.openReport)
        row.addWidget(self.reportButton)
        self.exportButton = qt.QPushButton(_("Export publication bundle…"))
        self.exportButton.setObjectName("LNMReportExportButton")
        self.exportButton.connect("clicked()", self.onExportClicked)
        row.addWidget(self.exportButton)
        layout.addLayout(row)
        self.exportReason = self._label("LNMReportExportReason", role="secondaryText")
        layout.addWidget(self.exportReason)

        self.warningsLabel = self._label("LNMReportWarnings")
        self.warningsLabel.setTextFormat(qt.Qt.RichText)
        layout.addWidget(self.warningsLabel)
        self.resultLabel = self._label("LNMReportExportResult", role="secondaryText")
        self.resultLabel.setTextFormat(qt.Qt.RichText)
        self.resultLabel.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
        self.resultLabel.connect("linkActivated(QString)", self.onLinkActivated)
        self.resultLabel.hide()
        layout.addWidget(self.resultLabel)

        self.disclaimer = widgets.disclaimerLabel()
        layout.addWidget(self.disclaimer)

        self._debounce = _Debounce(self.refresh)
        for name in ("runChanged", "studyChanged"):
            getattr(ctx, name).append(self._debounce.trigger)
        self._fillTable()
        self._renderState()
        qt.QTimer.singleShot(0, self.refresh)

    @staticmethod
    def _label(objectName, text="", role=None):
        label = qt.QLabel(text)
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        if role:
            label.setProperty("slicerRole", role)
        return label

    #
    # State
    #

    def root(self):
        return reportRoot(self.ctx)

    def refresh(self):
        """Re-read the registry and the study; redraw the checklist, the methods preview and the warnings."""
        if self._cleanedUp:
            return
        try:
            self.state = evaluate(self.root())
        except Exception:
            logging.exception("Lesion network mapping: the report could not be evaluated")
            self.state = evaluate(None)
        self._fillTable()
        self._renderState()

    def _fillTable(self):
        items = self.state.items
        selected = self.table.currentRow()
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            colour = qt.QColor(STATUS_COLOURS[item["status"]])
            checklist, _export, _methods = _lib()
            cells = (f"{item['id']}  {item['title']}", f"{STATUS_GLYPHS[item['status']]} {checklist.STATUS_LABELS[item['status']]}", item["value"])
            for column, text in enumerate(cells):
                cell = qt.QTableWidgetItem(text)
                cell.setToolTip(f"{item['id']} {item['title']}\n{checklist.STATUS_LABELS[item['status']]}: {item['value']}")
                if column == 1:
                    cell.setForeground(qt.QBrush(colour))
                self.table.setItem(row, column, cell)
        self.table.resizeRowsToContents()
        height = self.table.horizontalHeader().height + 4
        for row in range(self.table.rowCount):
            height += self.table.rowHeight(row)
        self.table.setFixedHeight(height)  # all 15 rows: the card scrolls, the table does not
        if 0 <= selected < len(items):
            self.table.selectRow(selected)
        else:
            self.detailLabel.setText(_("Select a row to see its value and evidence."))

    def _renderState(self):
        state = self.state
        counts = {}
        for item in state.items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        self.summaryLabel.setText(_("{done} done · {flagged} flagged · {notrun} not run · {na} N/A").format(done=counts.get("done", 0), flagged=counts.get("flagged", 0), notrun=counts.get("not_run", 0), na=counts.get("na", 0)))
        if state.root is None:
            self.methodsView.setPlainText(_("Open a study (Library) to generate the methods text from its runs."))
        elif state.sections is None:
            self.methodsView.setPlainText(_("The methods could not be generated; see the Python console."))
        elif hasattr(self.methodsView, "setMarkdown"):
            self.methodsView.setMarkdown(state.methodsText("md"))
        else:
            self.methodsView.setPlainText(state.methodsText("txt"))
        hasMethods = state.sections is not None
        self.copyMdButton.setEnabled(hasMethods)
        self.copyTxtButton.setEnabled(hasMethods)
        report = latestReport(state.root)
        self.reportButton.setEnabled(report is not None)
        self.reportButton.setToolTip(_("Open the report.html of the newest Run everything run") if report else _("No report yet: Run everything writes report.html"))
        self.exportButton.setEnabled(state.root is not None and os.path.isdir(state.root))
        self.exportButton.setToolTip(_("Copy the maps, design files, reproduce script, checklist and methods into derivatives/lnm/publication/<date>/"))
        self.exportReason.setText("" if state.root is not None else _("Open a study to export a publication bundle."))
        self.exportReason.setVisible(state.root is None)
        warnings = state.warnings()
        if warnings and state.root is not None:
            lines = "".join(f"<li>{html.escape(w[0])} {html.escape(w[1])}: {html.escape('flagged' if w[2] == 'flagged' else 'not run')}</li>" for w in warnings)
            self.warningsLabel.setText(f"<p>{html.escape(_('Warnings (the export is not blocked):'))}</p><ul style='margin-left:-24px'>{lines}</ul>")
        else:
            self.warningsLabel.setText("")
        self.warningsLabel.setVisible(bool(self.warningsLabel.text))

    #
    # Actions
    #

    def onSelectionChanged(self):
        row = self.table.currentRow()
        if not 0 <= row < len(self.state.items):
            return
        item = self.state.items[row]
        evidence = []
        for token in item["evidence"]:
            label = token[5:] if token.startswith("file:") else token
            evidence.append(f'<a href="{html.escape(token, quote=True)}">{html.escape(label)}</a>')
        text = f"<b>{html.escape(item['id'])} {html.escape(item['title'])}</b><br>{html.escape(item['value'])}"
        text += "<br>" + (_("Evidence: ") + ", ".join(evidence) if evidence else _("Evidence: none"))
        self.detailLabel.setText(text)

    def onLinkActivated(self, link):
        """Open the run folder or file of an evidence link, or the bundle folder ("bundle:")."""
        if link.startswith("bundle:"):
            path = self.lastBundle["dir"] if self.lastBundle else None
        else:
            path = _evidenceTarget(self.state.root, link)
        if path and os.path.exists(path):
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))
        else:
            self.detailLabel.setText(_("That file or run folder is not on disk."))

    def methodsText(self, fmt):
        return self.state.methodsText(fmt)

    def copyMethods(self, fmt="md"):
        """Copy the methods (Markdown or plain text) to the clipboard; return the text."""
        text = self.methodsText(fmt)
        if not text:
            return ""
        qt.QApplication.clipboard().setText(text)
        kind = _("Markdown") if fmt == "md" else _("plain text")
        self.copyStatus.setText(_("Copied {n} characters as {kind}.").format(n=len(text), kind=kind))
        self.copyStatus.show()
        qt.QTimer.singleShot(4000, self._hideCopyStatus)
        return text

    def _hideCopyStatus(self):
        try:
            self.copyStatus.hide()
        except (ValueError, RuntimeError):
            pass  # destroyed

    def openReport(self):
        path = latestReport(self.state.root)
        if path:
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))

    def onExportClicked(self):
        self.exportBundle()

    def exportBundle(self):
        """Export the publication bundle of the open study; warnings never block. Return the result dict or None."""
        root = self.root()
        if root is None:
            return None
        qt.QApplication.setOverrideCursor(qt.Qt.WaitCursor)
        try:
            result = exportBundle(root)
        except Exception as exc:
            logging.exception("Lesion network mapping: the publication bundle could not be exported")
            self.resultLabel.setText(html.escape(_("The bundle could not be written: {error}").format(error=exc)))
            self.resultLabel.show()
            return None
        finally:
            qt.QApplication.restoreOverrideCursor()
        self.lastBundle = result
        folder = os.path.relpath(result["dir"], root).replace(os.sep, "/")
        text = _("Exported {n} files to {folder}").format(n=len(result["files"]), folder=html.escape(folder))
        if result["warnings"]:
            text += " · " + _("{n} warnings (see manifest.json)").format(n=len(result["warnings"]))
        self.resultLabel.setText(f'{text} · <a href="bundle:">{html.escape(_("Show folder"))}</a>')
        self.resultLabel.show()
        self.refresh()
        return result

    # Card protocol

    def onRunEvent(self, event):
        if event.get("event") in ("done", "error", "cancelled", "failed"):
            self._debounce.trigger()

    def onStudyChanged(self):
        self._debounce.trigger()

    def onResultChanged(self):
        pass

    def cleanup(self):
        self._cleanedUp = True
        self._debounce.stop()
        for name in ("runChanged", "studyChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self._debounce.trigger in callbacks:
                callbacks.remove(self._debounce.trigger)


HEADER = {
    "id": "battery",
    "title": "Reporting",
    "order": ORDER_BATTERY,
    "classic": True,
    "factory": BatteryStrip,
}

CARD = {
    "id": "report",
    "title": "Report",
    "order": ORDER_REPORT,
    "classic": True,
    "factory": ReportCard,
}
