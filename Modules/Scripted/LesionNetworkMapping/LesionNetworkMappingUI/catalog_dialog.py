"""The connectome catalog dialog: what can be obtained, under which terms, and the guided download (L-35, addendum 4.1/4.2).

Non-modal; works in Classic and in Studio (Esc closes it, a running download continues). One row per catalog entry
(``catalog/connectomes.json``) with its licence badge (green, amber, grey), size, N, space and ages. The details pane of
the selected entry shows the provenance (provider, DOI and version, population, preprocessing, citation, upstream notes)
and, for a connectome the app may fetch:

1. **Load terms** fetches the provider's *live* terms text when you click (a light job; the text is shown with its sha256);
2. the box **I have read and accept these terms (this is my agreement with the data provider)** stores
   ``Studio/LNM/AcceptedTermsText/<sha256>``: an acceptance belongs to this exact text, and a changed text must be
   accepted again;
3. a **destination** (the bundle becomes ``<folder>/<id>.lnmc``) with the disk plan from ``shutil.disk_usage``: download,
   stored size and peak, the subset (a few archives, or all) and whether to keep the archives;
4. **Download and convert** starts the job. It is disabled until 1 to 3 are done; the job itself refuses a terms text that
   is not the accepted one.

Entries whose terms are not stated (Lead-DBS PPMI, TorPD and Pediatric) are link-out only: **Open the source page**.
**Check catalog updates** compares the live dataset listings with the catalog, only when clicked. A progress card shows a
running, paused or finished download with Pause and Resume (``manager_catalog.FetchController``). In the web build
(``SLICER_STUDIO_WEB`` or a Docker container) the destination defaults to ``/data/SlicerStudio`` and a destination outside
``/data`` is warned about. When ``Studio/LNM/EnableConnectomeFetch`` is "false" the dialog lists links only.
"""

import logging
import os
import shutil

import qt
import slicer
from slicer.i18n import tr as _

from . import jobrunner, manager_catalog, widgets

JOB_MODULE = "LesionNetworkMappingLib.fetchconnectome"
BADGE_LEVELS = {"green": "ok", "amber": "warn", "grey": "neutral"}
COLUMNS = (_("Connectome"), _("Licence"), _("Size"), _("N"), _("Space"), _("Ages"))
#: Archive counts offered as subsets, besides all archives
SUBSET_ARCHIVE_COUNTS = (1, 2, 5, 10)
MAX_STREAMS = 4
DEFAULT_STREAMS = 2
#: Height of the entries table (the details below it scroll)
TABLE_MIN_HEIGHT = 90
TABLE_MAX_HEIGHT = 200


#
# Helpers without Qt state
#


def formatBytes(count):
    """"236 GB", "1.2 GB", "9 kB": decimal units, as the providers state sizes."""
    if count is None:
        return "—"
    for unit, size in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if count >= size:
            return f"{count / size:.3g} {unit}"
    return f"{int(count)} B"


def licenceText(entry):
    """The label of the licence badge: "GSP data use terms", "CC BY 4.0", "Terms not stated"."""
    licence = str(entry.get("licence") or "")
    if licence == "GSP-DUT-2014":
        return _("GSP data use terms")
    if licence in ("", "unknown", "not-stated"):
        return _("Terms not stated")
    return licence.replace("-", " ", 1) if licence.startswith("CC-") else licence


def licenceLevel(entry):
    """The chip level of an entry's badge (green: ok, amber: warn, grey: neutral)."""
    from LesionNetworkMappingLib import catalog

    return BADGE_LEVELS.get(catalog.licence_badge(entry), "neutral")


def agesText(entry):
    ages = (entry.get("population") or {}).get("age_range")
    return f"{ages[0]:g}–{ages[1]:g} y" if ages else "—"  # noqa: RUF001


def entryRow(entry):
    """The cells of an entry's row: title, licence, size, N, space and ages."""
    population = entry.get("population") or {}
    n = population.get("n")
    return [entry["title"], licenceText(entry), formatBytes(entry.get("size")) if entry.get("size") else "—", f"{n:,}" if n else "—", entry.get("space") or _("see source"), agesText(entry)]


def fetchable(entry):
    """True when the app may download the entry (not link-out, supported, live terms) and fetching is switched on."""
    from LesionNetworkMappingLib import fetchconnectome, jobs

    if not manager_catalog.fetchEnabled():
        return False
    try:
        fetchconnectome.check_fetchable(entry)
    except jobs.JobError:
        return False
    return True


def subsetChoices(entry):
    """``[(label, value)]``: all archives, the first 1, 2, 5 and 10 archives, and the entry's named subsets (Lead-DBS only)."""
    from LesionNetworkMappingLib import catalog

    files = catalog.archives(entry)
    count = len(files)
    choices = [(_("All {n} archives").format(n=count), "all")]
    for number in SUBSET_ARCHIVE_COUNTS:
        if number < count:
            value = f"archives:00-{number - 1:02d}"
            plan = catalog.disk_plan(entry, value)
            subjects = f" · ~{plan['subjects']:,} subjects" if plan.get("subjects") else ""
            label = _("The first archive") if number == 1 else _("The first {n} archives").format(n=number)
            choices.append((f"{label}{subjects}", value))
    for name in (entry.get("subsets") or {}):
        if name not in ("Full Set", "Full set"):
            choices.append((_("{name} (subjects)").format(name=name), name))
    return choices


def nearestExisting(path):
    """The folder itself, else its nearest existing parent (where ``shutil.disk_usage`` can be asked)."""
    path = os.path.abspath(os.path.expanduser(path or "."))
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def freeBytes(path):
    try:
        return shutil.disk_usage(nearestExisting(path)).free
    except OSError:
        return None


def planSummary(entry, subset, keepArchives, destination):
    """``(text, problem)``: the disk plan of a download as a sentence, and why it cannot fit (or None)."""
    from LesionNetworkMappingLib import catalog

    plan = catalog.disk_plan(entry, None if subset == "all" else subset, keepArchives)
    free = freeBytes(destination) if destination else None
    parts = [_("Download {size}").format(size=formatBytes(plan["download"]))]
    if plan["stored_range"]:
        low, high = plan["stored_range"]
        parts.append(_("stored {low}–{high}").format(low=formatBytes(low), high=formatBytes(high)))  # noqa: RUF001
    elif plan["stored"]:
        parts.append(_("stored {size}").format(size=formatBytes(plan["stored"])))
    if plan["peak"]:
        parts.append(_("peak {size}").format(size=formatBytes(plan["peak"])))
    if free is not None:
        parts.append(_("free here {size}").format(size=formatBytes(free)))
    text = " · ".join(parts)
    if plan["notes"]:
        text += "\n" + " ".join(plan["notes"])
    problem = None
    if plan["peak"] and free is not None and free < plan["peak"] * 1.05:
        problem = _("Not enough free space here: the plan needs about {need} at the peak and {free} are free.").format(need=formatBytes(plan["peak"]), free=formatBytes(free))
    return text, problem


def _label(text="", objectName=None, secondary=False, selectable=False, wrap=True):
    label = qt.QLabel(text)
    if objectName:
        label.setObjectName(objectName)
    label.setWordWrap(wrap)
    if secondary:
        label.setProperty("slicerRole", "secondaryText")
    if selectable:
        label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    return label


def _button(text, objectName, primary=False):
    button = qt.QPushButton(text)
    button.setObjectName(objectName)
    button.setProperty("slicerRole", "primary" if primary else "secondary")
    return button


def _chipCell(text, level):
    cell = qt.QWidget()
    layout = qt.QHBoxLayout(cell)
    layout.setContentsMargins(4, 0, 4, 0)
    layout.addWidget(widgets.chip(text, level), 0, qt.Qt.AlignVCenter)
    layout.addStretch(1)
    return cell


def _availableArea(widget=None):
    """The available geometry (``QRect``, without panels) of the screen that shows ``widget``, else of the primary screen; None without a screen.

    ``QGuiApplication.primaryScreen`` is a property in PythonQt (Qt 5 and Qt 6) and cannot be called: ``StudioLib.ui``
    resolves the screen when it can be imported, else the first of ``QGuiApplication.screens()`` is the primary screen.
    """
    try:
        ui = widgets._studioUi()
        screen = ui.screenOf(widget) if ui is not None else None
        if screen is None:
            screens = qt.QGuiApplication.screens()
            screen = screens[0] if screens else None
        return screen.availableGeometry if screen is not None else None
    except Exception:  # noqa: BLE001 (the default size is fine without a screen)
        return None


class CatalogDialog(qt.QDialog):
    """See the module docstring. ``catalogPath`` (a catalog file) and the controller's ``extraArgs`` are for tests."""

    def __init__(self, ctx=None, parent=None, catalogPath=None):
        qt.QDialog.__init__(self, parent or slicer.util.mainWindow())
        self.ctx = ctx
        self.controller = manager_catalog.controller()
        if catalogPath is not None:
            self.controller.catalogPath = catalogPath
        self.entries = []
        self.entry = None
        self.terms = {}  # entry id -> {"text", "display", "sha"} of the terms loaded in this session
        self.termsJob = None
        self.checkJobs = []
        self.checkResults = []
        self.setObjectName("LNMCatalogDialog")
        self.setWindowTitle(_("Get a connectome"))
        self.setMinimumSize(860, 600)
        size = qt.QSize(1000, 760)
        available = _availableArea(self.parentWidget())
        if available is not None:  # a real window manager clamps a window that is as high as the screen
            size = qt.QSize(min(size.width(), max(860, available.width() - 80)), min(size.height(), max(600, available.height() - 120)))
        self.resize(size)
        layout = qt.QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(_label(_("Connectomes that can be obtained. Nothing is bundled and nothing is downloaded automatically: a download starts only when you click Download, "
                                  "after you have read and accepted the provider's current terms text. Sources that state no terms are links only."), "LNMCatalogIntro"))
        self.notice = widgets.banner("", level="warn")
        self.notice.setObjectName("LNMCatalogNotice")
        self.notice.hide()
        layout.addWidget(self.notice)
        self.resumeBanner = widgets.banner("", ((_("Resume"), self.onResume),), level="busy")
        self.resumeBanner.setObjectName("LNMCatalogResumeBanner")
        self.resumeBanner.hide()
        layout.addWidget(self.resumeBanner)

        self.table = widgets.tableView(list(COLUMNS))
        self.table.setObjectName("LNMCatalogTable")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        self.table.connect("itemSelectionChanged()", self.onSelectionChanged)
        layout.addWidget(self.table)
        scroll = qt.QScrollArea()
        scroll.setObjectName("LNMCatalogDetailsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(qt.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        scroll.setWidget(self._buildDetails())
        layout.addWidget(scroll, 1)

        self.actionBar = qt.QWidget()
        self.actionBar.setObjectName("LNMCatalogActionBar")
        actionLayout = qt.QVBoxLayout(self.actionBar)
        actionLayout.setContentsMargins(0, 0, 0, 0)
        actionLayout.setSpacing(4)
        self.planLabel = _label("", "LNMCatalogPlan", secondary=True, selectable=True)
        actionLayout.addWidget(self.planLabel)
        downloadRow = qt.QHBoxLayout()
        self.downloadButton = _button(_("Download and convert"), "LNMCatalogDownload", primary=True)
        self.downloadButton.connect("clicked()", self.onDownload)
        downloadRow.addWidget(self.downloadButton)
        self.hintLabel = _label("", "LNMCatalogDownloadHint", secondary=True)
        downloadRow.addWidget(self.hintLabel, 1)
        actionLayout.addLayout(downloadRow)
        layout.addWidget(self.actionBar)
        layout.addWidget(self._buildProgressCard())
        bottom = qt.QHBoxLayout()
        self.checkButton = _button(_("Check catalog updates"), "LNMCatalogCheckUpdates")
        self.checkButton.setToolTip(_("Compare the providers' live dataset listings with this catalog (nothing else is fetched)"))
        self.checkButton.connect("clicked()", self.checkUpdates)
        bottom.addWidget(self.checkButton)
        self.checkLabel = _label("", "LNMCatalogCheckResult", secondary=True, selectable=True)
        bottom.addWidget(self.checkLabel, 1)
        close = _button(_("Close"), "LNMCatalogClose")
        close.connect("clicked()", self.close)
        bottom.addWidget(close)
        layout.addLayout(bottom)

        self.controller.observers.append(self.onControllerChanged)
        self.refresh()

    # Building

    def _buildDetails(self):
        panel = qt.QWidget()
        panel.setObjectName("LNMCatalogDetails")
        layout = qt.QVBoxLayout(panel)
        layout.setContentsMargins(4, 0, 8, 0)
        layout.setSpacing(6)
        self.titleLabel = _label("", "LNMCatalogEntryTitle")
        font = self.titleLabel.font
        font.setBold(True)
        self.titleLabel.setFont(font)
        layout.addWidget(self.titleLabel)
        chips = qt.QHBoxLayout()
        self.licenceChip = widgets.chip("", "neutral")
        self.statusChip = widgets.chip("", "neutral")
        chips.addWidget(self.licenceChip)
        chips.addWidget(self.statusChip)
        chips.addStretch(1)
        layout.addLayout(chips)
        self.factsLabel = _label("", "LNMCatalogFacts", secondary=True, selectable=True)
        layout.addWidget(self.factsLabel)
        self.aboutToggle = qt.QToolButton()
        self.aboutToggle.setObjectName("LNMCatalogAboutToggle")
        self.aboutToggle.setText(_("Citation, acknowledgement and notes"))
        self.aboutToggle.setCheckable(True)
        self.aboutToggle.setChecked(False)
        self.aboutToggle.setArrowType(qt.Qt.RightArrow)
        self.aboutToggle.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.aboutToggle.setAutoRaise(True)
        self.aboutToggle.connect("toggled(bool)", self.onAboutToggled)
        layout.addWidget(self.aboutToggle, 0, qt.Qt.AlignLeft)
        self.aboutBox = qt.QWidget()
        self.aboutBox.setObjectName("LNMCatalogAbout")
        aboutLayout = qt.QVBoxLayout(self.aboutBox)
        aboutLayout.setContentsMargins(14, 0, 0, 0)
        aboutLayout.setSpacing(4)
        self.citationLabel = _label("", "LNMCatalogCitation", secondary=True, selectable=True)
        aboutLayout.addWidget(self.citationLabel)
        self.upstreamLabel = _label("", "LNMCatalogUpstream", secondary=True, selectable=True)
        aboutLayout.addWidget(self.upstreamLabel)
        self.aboutBox.hide()
        layout.addWidget(self.aboutBox)
        self.linkOutBox = qt.QWidget()
        linkLayout = qt.QVBoxLayout(self.linkOutBox)
        linkLayout.setContentsMargins(0, 0, 0, 0)
        self.linkOutLabel = _label("", "LNMCatalogLinkOutText")
        linkLayout.addWidget(self.linkOutLabel)
        self.openSourceButton = _button(_("Open the source page"), "LNMCatalogOpenSource")
        self.openSourceButton.connect("clicked()", self.openSource)
        row = qt.QHBoxLayout()
        row.addWidget(self.openSourceButton)
        row.addStretch(1)
        linkLayout.addLayout(row)
        layout.addWidget(self.linkOutBox)

        self.fetchBox = qt.QWidget()
        self.fetchBox.setObjectName("LNMCatalogFetchBox")
        fetchLayout = qt.QVBoxLayout(self.fetchBox)
        fetchLayout.setContentsMargins(0, 4, 0, 0)
        fetchLayout.setSpacing(6)
        fetchLayout.addWidget(_label(_("1. The provider's terms (the live text, fetched when you click)"), "LNMCatalogTermsTitle"))
        termsRow = qt.QHBoxLayout()
        self.loadTermsButton = _button(_("Load terms"), "LNMCatalogLoadTerms")
        self.loadTermsButton.connect("clicked()", self.loadTerms)
        termsRow.addWidget(self.loadTermsButton)
        self.shaLabel = _label("", "LNMCatalogTermsSha", secondary=True, selectable=True)
        termsRow.addWidget(self.shaLabel, 1)
        fetchLayout.addLayout(termsRow)
        self.termsView = qt.QTextBrowser()
        self.termsView.setObjectName("LNMCatalogTermsText")
        self.termsView.setMinimumHeight(90)
        self.termsView.setMaximumHeight(150)
        fetchLayout.addWidget(self.termsView)
        self.acceptCheck = qt.QCheckBox(_("I have read and accept these terms (this is my agreement with the data provider)"))
        self.acceptCheck.setObjectName("LNMCatalogAccept")
        self.acceptCheck.setEnabled(False)
        self.acceptCheck.connect("toggled(bool)", self.onAcceptToggled)
        fetchLayout.addWidget(self.acceptCheck)

        fetchLayout.addWidget(_label(_("2. Where to put it"), "LNMCatalogDestinationTitle"))
        destRow = qt.QHBoxLayout()
        self.destEdit = qt.QLineEdit()
        self.destEdit.setObjectName("LNMCatalogDestination")
        self.destEdit.setPlaceholderText(_("A folder with enough free space"))
        self.destEdit.connect("textChanged(QString)", lambda text: self.updateState())
        destRow.addWidget(self.destEdit, 1)
        browse = _button(_("Browse…"), "LNMCatalogBrowse")
        browse.connect("clicked()", self.browse)
        destRow.addWidget(browse)
        fetchLayout.addLayout(destRow)
        self.destNotice = _label("", "LNMCatalogDestinationNotice")
        self.destNotice.setProperty("slicerRole", "warningText")
        fetchLayout.addWidget(self.destNotice)
        options = qt.QHBoxLayout()
        options.addWidget(qt.QLabel(_("Subset:")))
        self.subsetCombo = qt.QComboBox()
        self.subsetCombo.setObjectName("LNMCatalogSubset")
        self.subsetCombo.connect("currentIndexChanged(int)", lambda index: self.updateState())
        options.addWidget(self.subsetCombo, 1)
        options.addWidget(qt.QLabel(_("Streams:")))
        self.streamsSpin = qt.QSpinBox()
        self.streamsSpin.setObjectName("LNMCatalogStreams")
        self.streamsSpin.setRange(1, MAX_STREAMS)
        self.streamsSpin.setValue(DEFAULT_STREAMS)
        self.streamsSpin.setToolTip(_("Archives downloaded at the same time; the bundle is written one archive after the other"))
        options.addWidget(self.streamsSpin)
        fetchLayout.addLayout(options)
        self.keepCheck = qt.QCheckBox(_("Also keep the downloaded archives (about the size of the download)"))
        self.keepCheck.setObjectName("LNMCatalogKeepArchives")
        self.keepCheck.connect("toggled(bool)", lambda checked: self.updateState())
        fetchLayout.addWidget(self.keepCheck)
        layout.addWidget(self.fetchBox)
        layout.addStretch(1)
        return panel

    def _buildProgressCard(self):
        card = qt.QFrame()
        card.setObjectName("LNMCatalogProgressCard")
        card.setFrameShape(qt.QFrame.StyledPanel)
        layout = qt.QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        top = qt.QHBoxLayout()
        self.progressTitle = _label("", "LNMCatalogProgressTitle", wrap=False)
        font = self.progressTitle.font
        font.setBold(True)
        self.progressTitle.setFont(font)
        top.addWidget(self.progressTitle, 1)
        self.progressChip = widgets.chip("", "neutral")
        top.addWidget(self.progressChip)
        layout.addLayout(top)
        self.progress = widgets.progressRow(stopText=_("Pause"), objectNamePrefix="LNMCatalog")
        self.progress.stopButton.setToolTip(_("Pause the download: what is converted is kept and Resume continues where it stopped"))
        layout.addWidget(self.progress.widget)
        buttons = qt.QHBoxLayout()
        self.resumeButton = _button(_("Resume"), "LNMCatalogResume")
        self.resumeButton.connect("clicked()", self.onResume)
        buttons.addWidget(self.resumeButton)
        self.acceptMismatchButton = _button(_("Keep this data anyway…"), "LNMCatalogAcceptMismatch")
        self.acceptMismatchButton.setToolTip(_("Accept the archive whose checksum does not match; the choice is recorded as checksum_override in the bundle"))
        self.acceptMismatchButton.connect("clicked()", self.onAcceptMismatch)
        buttons.addWidget(self.acceptMismatchButton)
        self.openFolderButton = _button(_("Open folder"), "LNMCatalogOpenFolder")
        self.openFolderButton.connect("clicked()", self.openFolder)
        buttons.addWidget(self.openFolderButton)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.progressNote = _label("", "LNMCatalogProgressNote", secondary=True, selectable=True)
        layout.addWidget(self.progressNote)
        self.progressCard = card
        card.hide()
        return card

    # Entries

    def refresh(self):
        """Read the catalog again (the shipped file; nothing is fetched) and fill the table."""
        self.entries = []
        try:
            self.entries = list(manager_catalog._loadCatalog(self.controller.catalogPath))
        except Exception as exc:
            self.showNotice(_("The connectome catalog cannot be read: {error}").format(error=exc))
        if not manager_catalog.fetchEnabled():
            self.showNotice(_("Downloading connectomes is switched off ({key} is false): the catalog lists links only.").format(key=manager_catalog.ENABLE_SETTING))
        elif not self.notice.isHidden() and self.entries:
            self.notice.hide()
        current = self.entry["id"] if self.entry else None
        table = self.table
        table.setRowCount(len(self.entries))
        selectRow = 0
        for row, entry in enumerate(self.entries):
            cells = entryRow(entry)
            for column, value in enumerate(cells):
                if column == 1:
                    table.setCellWidget(row, column, _chipCell(value, licenceLevel(entry)))
                    item = qt.QTableWidgetItem("")
                else:
                    item = qt.QTableWidgetItem(str(value))
                item.setToolTip(entry["title"] if column == 0 else str(value))
                table.setItem(row, column, item)
            if entry["id"] == current:
                selectRow = row
        rowsHeight = sum(table.rowHeight(row) for row in range(len(self.entries)))
        table.setFixedHeight(max(TABLE_MIN_HEIGHT, min(TABLE_MAX_HEIGHT, table.horizontalHeader().height + rowsHeight + 4)))
        if self.entries:
            table.selectRow(selectRow)
        self.onSelectionChanged()
        self.onControllerChanged()

    def showNotice(self, text):
        widgets.setBannerText(self.notice, text)
        self.notice.show()

    def selectedEntry(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() is not None else []
        index = rows[0].row() if rows else -1
        return self.entries[index] if 0 <= index < len(self.entries) else None

    def selectEntry(self, entryId):
        for row, entry in enumerate(self.entries):
            if entry["id"] == entryId:
                self.table.selectRow(row)
                return entry
        return None

    def onSelectionChanged(self):
        from LesionNetworkMappingLib import catalog

        entry = self.selectedEntry()
        self.entry = entry
        if entry is None:
            return
        self.titleLabel.setText(entry["title"])
        widgets.setChipLevel(self.licenceChip, licenceLevel(entry), licenceText(entry))
        status = entry.get("status")
        statusText = {"supported": _("Can be fetched"), "unsupported": _("Not supported yet"), "link_out": _("Link only"), "local": _("Made on this computer")}.get(status, status)
        widgets.setChipLevel(self.statusChip, "neutral" if status != "supported" else "busy", statusText)
        self.factsLabel.setText(self.factsText(entry))
        citation = entry.get("citation") or ""
        acknowledgement = entry.get("acknowledgement") or ""
        self.citationLabel.setText((_("Cite: {text}").format(text=citation) if citation else "") + (("\n" + _("Acknowledgement: {text}").format(text=acknowledgement)) if acknowledgement else ""))
        upstream = entry.get("upstream") or ""
        self.upstreamLabel.setText(upstream)
        self.upstreamLabel.setVisible(bool(upstream))
        canFetch = fetchable(entry)
        linkOnly = catalog.link_out(entry) or not canFetch
        self.fetchBox.setVisible(canFetch)
        self.actionBar.setVisible(canFetch)
        self.linkOutBox.setVisible(linkOnly)
        if canFetch:
            self.fillFetchBox(entry)
        else:
            self.linkOutLabel.setText(self.linkOutText(entry))
            url = entry.get("link_url") or (entry.get("source") or {}).get("url")
            self.openSourceButton.setVisible(bool(url))
        self.updateState()

    def onAboutToggled(self, checked):
        self.aboutBox.setVisible(bool(checked))
        self.aboutToggle.setArrowType(qt.Qt.DownArrow if checked else qt.Qt.RightArrow)

    @staticmethod
    def factsText(entry):
        population = entry.get("population") or {}
        pre = entry.get("preprocessing") or {}
        source = entry.get("source") or {}
        facts = []
        if source.get("doi"):
            facts.append(f"doi:{source['doi']}" + (f" v{source['version']}" if source.get("version") else ""))
        n = population.get("n")
        if n:
            facts.append(f"N={n:,}" + (f", {agesText(entry)}" if population.get("age_range") else ""))
        if entry.get("space"):
            facts.append(f"{entry['space']}" + (f" {entry['resolution_mm']:g} mm" if entry.get("resolution_mm") else ""))
        if entry.get("tr_s"):
            facts.append(f"TR {entry['tr_s']:g} s")
        gsr = pre.get("gsr")
        if gsr in (True, False):
            facts.append(_("global signal regression") if gsr else _("no global signal regression"))
        if pre.get("smoothing_mm") is not None:
            facts.append(_("{n} mm smoothing").format(n=pre["smoothing_mm"]))
        return " · ".join(facts)

    @staticmethod
    def linkOutText(entry):
        if not manager_catalog.fetchEnabled() and entry.get("status") == "supported":
            return _("Downloading is switched off in this installation. Get it from the provider's page.")
        if entry.get("status") == "link_out":
            return _("The provider states no terms for this connectome, so the app does not download it and cannot show terms to accept. Get it from the source and add the folder with Convert… or Add bundle…")
        if entry.get("status") == "unsupported":
            return entry.get("status_note") or _("Not supported in this version.")
        if entry.get("status") == "local":
            return _("Made on this computer, not downloaded: Manage… > Create synthetic demo. SYNTHETIC — not real data.")
        return ""

    def fillFetchBox(self, entry):
        terms = self.terms.get(entry["id"])
        self.setTermsView(terms)
        wasBlocked = self.subsetCombo.blockSignals(True)
        self.subsetCombo.clear()
        for label, value in subsetChoices(entry):
            self.subsetCombo.addItem(label, value)
        self.subsetCombo.blockSignals(wasBlocked)
        if not self.destEdit.text.strip():
            self.destEdit.setText(manager_catalog.lastDestination() or self.defaultDestination())

    @staticmethod
    def defaultDestination():
        from LesionNetworkMappingLib import fetchconnectome

        dataDirectory = None
        try:
            from .normalize_controller import dataDirectory as readDataDirectory

            dataDirectory = readDataDirectory()
        except Exception:
            logging.exception("Lesion network mapping: the data directory could not be read")
        return fetchconnectome.default_destination(dataDirectory)

    # Terms

    def loadTerms(self):
        """Fetch the live terms text in a light job (never on the main thread); nothing else is fetched."""
        entry = self.entry
        if entry is None or (self.termsJob is not None and not self.termsJob.isFinished()):
            return None
        self.termsView.setPlainText(_("Fetching the provider's terms…"))
        self.loadTermsButton.setEnabled(False)
        args = ["terms", "--entry", entry["id"]]
        if self.controller.catalogPath:
            args += ["--catalog", self.controller.catalogPath]
        args += list(self.controller.extraArgs)
        spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Terms"), heavy=False, kind="fetch-terms", name="fetch-terms", runRoot=jobrunner.defaultRunRoot(),
                                 onDone=lambda job, entryId=entry["id"]: self.onTermsDone(job, entryId))
        self.termsJob = jobrunner.instance().submit(spec)
        return self.termsJob

    def onTermsDone(self, job, entryId):
        self.loadTermsButton.setEnabled(True)
        event = next((e for e in job.results if e.get("kind") == "terms"), None)
        failure = None
        if job.succeeded() and event is not None:
            self.terms[entryId] = {"text": event.get("text") or "", "display": event.get("display") or event.get("text") or "", "sha": event.get("sha256"), "url": event.get("url")}
        else:
            self.terms.pop(entryId, None)
            failure = _("The terms could not be fetched ({reason}). They must be shown before anything is downloaded: check the connection and click Load terms again.").format(reason=job.failureReason)
        if self.entry is not None and self.entry["id"] == entryId:
            self.setTermsView(self.terms.get(entryId))
            if failure is not None:  # setTermsView shows the click-to-load prompt for missing terms: the failure replaces it
                self.termsView.setPlainText(failure)
        self.updateState()

    def setTermsView(self, terms):
        if terms is None:
            self.termsView.setPlainText(_("Click Load terms to read the provider's current terms."))
            self.shaLabel.setText("")
            wasBlocked = self.acceptCheck.blockSignals(True)
            self.acceptCheck.setChecked(False)
            self.acceptCheck.blockSignals(wasBlocked)
            self.acceptCheck.setEnabled(False)
            return
        url = terms.get("url")
        self.termsView.setPlainText(terms["display"] + (f"\n\n{url}" if url else ""))
        self.shaLabel.setText(f"sha256 {terms['sha']}")
        self.acceptCheck.setEnabled(True)
        wasBlocked = self.acceptCheck.blockSignals(True)
        self.acceptCheck.setChecked(manager_catalog.termsAccepted(terms["sha"]))  # an earlier acceptance of this exact text
        self.acceptCheck.blockSignals(wasBlocked)

    def currentTerms(self):
        return self.terms.get(self.entry["id"]) if self.entry is not None else None

    def onAcceptToggled(self, checked):
        terms = self.currentTerms()
        if terms is not None:
            manager_catalog.setTermsAccepted(terms["sha"], bool(checked))
        self.updateState()

    # Destination and download

    def browse(self):
        start = self.destEdit.text.strip() or self.defaultDestination()
        folder = qt.QFileDialog.getExistingDirectory(self, _("Where should the connectome be saved?"), nearestExisting(start))
        if folder:
            self.destEdit.setText(folder)

    def request(self):
        terms = self.currentTerms()
        subset = self.subsetCombo.currentData
        return {"entry": self.entry["id"], "dest": self.destEdit.text.strip(), "subset": None if subset in (None, "", "all") else str(subset), "keepArchives": bool(self.keepCheck.checked),
                "streams": int(self.streamsSpin.value), "termsSha": terms["sha"] if terms else None}

    def updateState(self):
        """Enable Download only when the terms were read and accepted, the destination is set and fits, and no download runs."""
        from LesionNetworkMappingLib import fetchconnectome

        entry = self.entry
        if entry is None or not fetchable(entry):
            return
        terms = self.currentTerms()
        destination = self.destEdit.text.strip()
        subset = self.subsetCombo.currentData or "all"
        problem = None
        if destination:
            text, problem = planSummary(entry, subset, self.keepCheck.checked, destination)
            self.planLabel.setText(text)
        else:
            self.planLabel.setText("")
        self.destNotice.setText(fetchconnectome.destination_notice(destination))
        self.destNotice.setVisible(bool(self.destNotice.text))
        running = self.controller.isRunning()
        hint = None
        if terms is None:
            hint = _("Load the terms first.")
        elif not (self.acceptCheck.checked and manager_catalog.termsAccepted(terms["sha"])):
            hint = _("Read the terms and tick the box to accept them.")
        elif not destination:
            hint = _("Choose a destination folder.")
        elif problem:
            hint = problem
        elif running:
            hint = _("A connectome is being fetched.")
        elif self.completeHere():
            hint = _("{name} is already in this folder: choose another folder.").format(name=os.path.basename(fetchconnectome.bundle_path(entry, destination)))
        self.hintLabel.setText(hint or "")
        self.downloadButton.setText(_("Resume this download") if self.unfinishedHere() else _("Download and convert"))
        self.downloadButton.setEnabled(hint is None)
        self.loadTermsButton.setEnabled(self.termsJob is None or self.termsJob.isFinished())

    def completeHere(self):
        """True when a finished bundle of the selected entry is in the chosen folder (nothing to resume: a new download would be refused)."""
        from LesionNetworkMappingLib import fetchconnectome

        destination = self.destEdit.text.strip()
        if self.entry is None or not destination or self.unfinishedHere():
            return False
        return os.path.exists(os.path.join(fetchconnectome.bundle_path(self.entry, destination), "connectome.json"))

    def unfinishedHere(self):
        """A summary of an interrupted download of the selected entry in the chosen folder (it is continued, not started again)."""
        from LesionNetworkMappingLib import fetchconnectome

        destination = self.destEdit.text.strip()
        if self.entry is None or not destination:
            return None
        try:
            return fetchconnectome.unfinished_fetch(self.entry, destination)
        except Exception:
            return None

    def onDownload(self):
        if self.entry is None:
            return None
        request = self.request()
        job = self.controller.start(request, resume=self.unfinishedHere() is not None)
        if job is None:
            self.showNotice(self.controller.lastRequestError or _("The download could not start."))
            return None
        manager_catalog.setLastDestination(request["dest"])
        self.notice.hide()
        self.updateState()
        return job

    # Progress

    def onControllerChanged(self):
        status = self.controller.status()
        state = status["state"]
        job = self.controller.job
        self.progressCard.setVisible(state != "idle")
        if state == "idle":
            self.updateResumeBanner()
            return
        name = status["name"]
        self.progressTitle.setText(_("Fetching {name}").format(name=name))
        level = {"running": "busy", "paused": "neutral", "failed": "danger", "done": "ok"}[state]
        chipText = {"running": _("Running"), "paused": _("Paused"), "failed": _("Failed"), "done": _("Done")}[state]
        widgets.setChipLevel(self.progressChip, level, chipText)
        self.progress.setJob(job if state == "running" else None)
        fraction = status["fraction"]
        if state == "running":
            archive = ""
            if status.get("archiveCount"):
                archive = _(" · archive {i} of {n}").format(i=status.get("archiveIndex") or 1, n=status["archiveCount"])
            subjects = _(" · {n} subjects").format(n=status["subjects"]) if status.get("subjects") else ""
            eta = f" · {status['eta']}" if status["eta"] else ""
            percent = f"{int(fraction * 100)}%" if fraction is not None else _("starting")
            data = ""
            if status.get("bytes") is not None and status.get("total"):
                data = f" · {formatBytes(status['bytes'])} of {formatBytes(status['total'])}"
            self.progress.setProgress(fraction, text=f"{percent}{data}{archive}{subjects}{eta}")
            self.progress.stopButton.setEnabled(not (job is not None and job.stopRequested))
        elif state == "paused":
            self.progress.setProgress(fraction, text=_("Paused. What is converted is kept; Resume continues where it stopped."))
            self.progress.stopButton.setEnabled(False)
        elif state == "failed":
            self.progress.setProgress(fraction, text=_("Failed: {reason}").format(reason=status["error"]))
            self.progress.stopButton.setEnabled(False)
        else:
            self.progress.setProgress(1.0, text=_("Done: the connectome is registered and ready in the Connectome card."))
            self.progress.stopButton.setEnabled(False)
        self.resumeButton.setVisible(state in ("paused", "failed") and not status["mismatch"])
        self.acceptMismatchButton.setVisible(state == "failed" and status["mismatch"])
        self.openFolderButton.setVisible(state == "done" and bool(status["path"]))
        notes = [w for w in status["warnings"] if w][-3:]
        self.progressNote.setText("\n".join(notes))
        self.progressNote.setVisible(bool(notes))
        self.updateResumeBanner()
        self.updateState()

    def updateResumeBanner(self):
        found = None if self.controller.isRunning() or self.controller.state in ("paused", "failed", "done") else self.controller.unfinished()
        if found is None:
            self.resumeBanner.hide()
            return
        request, summary = found
        percent = f" ({int(summary['fraction'] * 100)}%)" if summary.get("fraction") is not None else ""
        widgets.setBannerText(self.resumeBanner, _("An unfinished download of {name}{percent} is waiting. Resume continues where it stopped.").format(name=self.controller.shortName(request), percent=percent))
        self.resumeBanner.show()

    def onResume(self):
        if self.controller.request is None:
            self.controller.request = self.controller.savedRequest()
        if self.controller.resume() is None and self.controller.lastRequestError:
            self.showNotice(self.controller.lastRequestError)

    def onAcceptMismatch(self):
        question = _("The checksum of an archive does not match the one the provider lists, also after reading it twice. Every file in it passed its integrity check, so the data may be fine, "
                     "but it cannot be verified. Keep it? The bundle records that you accepted the mismatch (checksum_override).")
        if slicer.app.testingEnabled() or slicer.util.confirmYesNoDisplay(question, windowTitle=_("Checksum mismatch")):
            self.controller.resume(acceptChecksumMismatch=True)

    def openFolder(self):
        path = self.controller.status().get("path")
        if path:
            widgets.showInFileManager(os.path.dirname(path))

    def openSource(self):
        entry = self.entry
        url = (entry.get("link_url") or (entry.get("source") or {}).get("url")) if entry else None
        if url:
            widgets.openLink(url)

    # Catalog updates

    def checkUpdates(self):
        """Compare the live dataset listings of the fetchable entries with the catalog (one light job each; only on click)."""
        pending = [entry for entry in self.entries if fetchable(entry)]
        if not pending or self.checkJobs:
            return []
        self.checkLabel.setText(_("Checking the providers' dataset listings…"))
        self.checkButton.setEnabled(False)
        self.checkResults = []
        for entry in pending:
            args = ["check", "--entry", entry["id"]]
            if self.controller.catalogPath:
                args += ["--catalog", self.controller.catalogPath]
            args += list(self.controller.extraArgs)
            spec = jobrunner.JobSpec(JOB_MODULE, args, title=_("Catalog check"), heavy=False, kind="fetch-check", name="fetch-check", runRoot=jobrunner.defaultRunRoot(),
                                     onDone=lambda job, entry=entry: self.onCheckDone(job, entry))
            self.checkJobs.append(jobrunner.instance().submit(spec))
        return list(self.checkJobs)

    def onCheckDone(self, job, entry):
        event = next((e for e in job.results if e.get("kind") == "catalog_check"), None)
        if job.succeeded() and event is not None:
            if event.get("current"):
                line = _("{title}: up to date (version {version}).").format(title=entry["title"], version=event.get("live_version") or "?")
            else:
                line = _("{title}: differs from the catalog: {what}").format(title=entry["title"], what="; ".join(event.get("differences") or []))
        else:
            line = _("{title}: could not be checked ({reason}).").format(title=entry["title"], reason=job.failureReason)
        self.checkResults.append(line)
        self.checkJobs = [j for j in self.checkJobs if j is not job and not j.isFinished()]
        self.checkLabel.setText("\n".join(self.checkResults))
        if not self.checkJobs:
            self.checkButton.setEnabled(True)
