"""Export card of the Map module (order 150): the run's maps with sidecars and provenance (SPEC 7.11).

Export… writes, for the current run, the AvgR, AvgRFz and T maps (exact connectome header), one JSON
sidecar per lesion (N, df, subset, rule and resolved threshold, QA, disclaimer), ``provenance.json``,
``methods.md`` and ``reproduce.sh``. With an open study the destination is
``<study>/derivatives/lnm/conn-<label>[_n-<N>]``; otherwise the user chooses a folder. A run stopped
early is exported as a partial run (N = its last snapshot). L-30 and later work packages extend it.
"""

import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_EXPORT, widgets

#: Last export folder chosen without a study (fork-only setting, SPEC Appendix B)
LAST_FOLDER_SETTING = "Studio/LNM/LastExportFolder"


class ExportCard:
    """Controller of the Export card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.lastResult = None
        self.widget = widgets.card(_("Export"), objectName="LNMExportCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        self.destinationLabel = qt.QLabel("")
        self.destinationLabel.setObjectName("LNMExportDestination")
        self.destinationLabel.setWordWrap(True)
        self.destinationLabel.setProperty("slicerRole", "secondaryText")
        self.destinationLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout.addWidget(self.destinationLabel)
        row = qt.QHBoxLayout()
        self.exportButton = qt.QPushButton(_("Export…"))
        self.exportButton.setObjectName("LNMExportButton")
        self.exportButton.setProperty("slicerRole", "secondary")
        self.exportButton.setToolTip(_("Write the maps, their sidecars, provenance.json, methods.md and reproduce.sh"))
        self.exportButton.connect("clicked()", self.export)
        row.addWidget(self.exportButton)
        self.showFolderButton = qt.QPushButton(_("Show folder"))
        self.showFolderButton.setObjectName("LNMExportShowFolderButton")
        self.showFolderButton.setProperty("slicerRole", "ghost")
        self.showFolderButton.connect("clicked()", self.showFolder)
        self.showFolderButton.hide()
        row.addWidget(self.showFolderButton)
        row.addStretch(1)
        layout.addLayout(row)
        self.resultLabel = qt.QLabel("")
        self.resultLabel.setObjectName("LNMExportResult")
        self.resultLabel.setWordWrap(True)
        self.resultLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.resultLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.resultLabel.hide()
        layout.addWidget(self.resultLabel)
        for name in ("runChanged", "studyChanged", "bundleChanged"):
            getattr(ctx, name).append(self.refresh)
        self.refresh()

    #
    # Card protocol
    #

    def onRunEvent(self, event):
        if event.get("event") == "start":
            self.lastResult = None
            self.resultLabel.hide()
            self.showFolderButton.hide()
        self.refresh()

    def onStudyChanged(self):
        self.refresh()

    def cleanup(self):
        for name in ("runChanged", "studyChanged", "bundleChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)

    #
    # State
    #

    def _run(self):
        run = self.ctx.currentRun
        if run is None or not run.runDir or not os.path.isfile(os.path.join(run.runDir, "run.json")):
            return None
        job = getattr(run, "job", None)
        if job is not None and not job.isFinished():
            return None
        return run

    def _studyPath(self):
        study = self.ctx.study
        path = getattr(study, "path", None) if study is not None else None
        if path:
            return os.fspath(path)
        node = self.ctx.parameterNode
        try:
            return node.studyPath or None if node is not None else None
        except AttributeError:
            return None

    def studyDestination(self):
        """``<study>/derivatives/lnm/conn-<label>[_n-<N>]`` of the current run, or None without a study."""
        from LesionNetworkMappingLib import export

        studyPath, run, bundle = self._studyPath(), self._run(), self.ctx.bundle
        if not studyPath or run is None or bundle is None:
            return None
        n = run.n if run.n and run.n != bundle.n_subjects else None
        return export.derivatives_dir(studyPath, bundle.name, n)

    def refresh(self):
        run = self._run()
        self.exportButton.setEnabled(run is not None)
        if run is None:
            self.destinationLabel.setText(_("Export is available once a map is computed (or stopped with a partial map)."))
            return
        destination = self.studyDestination()
        if destination:
            self.destinationLabel.setText(_("Into the study: {path}").format(path=destination))
        else:
            self.destinationLabel.setText(_("No study is open: Export asks for a folder."))
        if run.partial:
            self.destinationLabel.setText(self.destinationLabel.text + " " + _("This run stopped early: it is exported as partial (N={n}).").format(n=run.n))

    #
    # Actions
    #

    def export(self):
        """Export the current run to the study derivatives, or to a folder chosen by the user; return the written files or None."""
        if self._run() is None:
            return None
        destination = self.studyDestination()
        if not destination:
            settings = qt.QSettings()
            start = str(settings.value(LAST_FOLDER_SETTING) or "")
            folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Export the network map into…"), start)
            if not folder:
                return None
            settings.setValue(LAST_FOLDER_SETTING, folder)
            bundle = self.ctx.bundle
            from LesionNetworkMappingLib import export

            run = self._run()
            n = run.n if run.n and bundle is not None and run.n != bundle.n_subjects else None
            destination = os.path.join(folder, export.run_dir_name(bundle.name, n)) if bundle is not None else folder
        try:
            return self.exportTo(destination)
        except Exception:
            return None  # shown in the card ("Export failed: …") and logged by exportTo

    def exportTo(self, destination):
        """Export the current run into ``destination`` (created when needed); return ``export.write_run``'s files."""
        run = self._run()
        if run is None:
            raise RuntimeError("There is no finished run to export")
        logic = self.ctx.logic
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        rule = self.ctx.rule
        resolved = getattr(widget, "resolved", None) if widget is not None else None
        if rule is None and widget is not None:
            rule = widget.currentRule()
        try:
            with slicer.util.WaitCursor():
                result = logic.exportRun(run.runDir, destination, rule=rule, resolved=resolved, partial=run.partial)
        except Exception as exc:
            import logging

            logging.exception("Lesion network mapping: the export failed")
            self.resultLabel.setText(_("Export failed: {error}").format(error=exc))
            self.resultLabel.show()
            raise
        self.lastResult = result
        self.lastDestination = destination
        files = len(result["maps"]) + len(result["sidecars"]) + 3
        self.resultLabel.setText((_("Exported 1 file (maps, sidecars, provenance.json, methods.md, reproduce.sh) to {path}") if files == 1
                                 else _("Exported {count} files (maps, sidecars, provenance.json, methods.md, reproduce.sh) to {path}")).format(count=files, path=destination))
        self.resultLabel.show()
        self.showFolderButton.show()
        shell = self.ctx.shell()
        if shell is not None:
            try:
                shell.toast(_("Exported · {name}").format(name=os.path.basename(destination)), 3000, action=self.showFolder, actionText=_("Show"))
            except Exception:
                pass
        return result

    def showFolder(self):
        path = getattr(self, "lastDestination", None)
        if path and os.path.isdir(path):
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(path))


CARD = {
    "id": "export",
    "title": "Export",
    "order": ORDER_EXPORT,
    "modes": ("lesion",),
    "factory": ExportCard,
}
