"""QA card of the Map module (order 60): the specificity checks of every map (SPEC 7.7, 7.1 rule 4).

- Hubness (degree) similarity: Pearson r and Spearman rho of the map with the connectome's degree map, as a chip. Without a
  degree map, "Compute degree map" runs ``python -m LesionNetworkMappingLib.qa degree`` as a job.
- The relocated-lesion null: always stated, also when it is off ("Relocated null: off for this run
  (≈ +2 min) [Run]"), so a map is never shown without saying what was not checked.
- The reference QA (μC_r, calibrated percentile) when the run reports it.
- Coverage (voxels on the connectome grid, fractional sum, % outside the connectome mask) and the
  N / df / preview / partial badges.

The card expands itself when a check is amber or red. L-30 replaces its content (QA v2).
"""

import math

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_QA, widgets

#: Chip level of the qa.py levels
_LEVELS = {"neutral": "neutral", "amber": "warn", "red": "danger", "below": "ok", "not_calibrated": "neutral"}
#: Engine cost of 50 relocated lesions per subject for a GSP1000-size connectome (SPEC 7.5: ~146 ms for L = 51 vs ~10 ms)
_NULL_SECONDS_PER_SUBJECT = 0.136
_GSP_VOXELS = 292019


def nullCostText(bundle, nSubjects=None):
    """Estimated extra time of the relocated-lesion null for a bundle: "≈ +2 min" or "< 1 min"."""
    if bundle is None:
        return ""
    n = nSubjects or bundle.n_subjects
    seconds = n * _NULL_SECONDS_PER_SUBJECT * bundle.n_voxels / _GSP_VOXELS
    return _("< 1 min") if seconds < 60 else _("≈ +{minutes} min").format(minutes=round(seconds / 60))


class QACard:
    """Controller of the QA card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.degreeJob = None
        self.qa = None
        self.qc = None
        self.widget = widgets.card(_("QA"), objectName="LNMQACard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        self.badgesLayout = qt.QHBoxLayout()
        self.nChip = widgets.chip("", "neutral")
        self.dfChip = widgets.chip("", "neutral")
        self.subsetChip = widgets.chip("", "neutral")
        for chip in (self.nChip, self.dfChip, self.subsetChip):
            chip.hide()
            self.badgesLayout.addWidget(chip)
        self.badgesLayout.addStretch(1)
        layout.addLayout(self.badgesLayout)

        degreeRow = qt.QHBoxLayout()
        self.degreeChip = widgets.chip(_("Hubness"), "neutral")
        self.degreeChip.setObjectName("LNMQADegreeChip")
        degreeRow.addWidget(self.degreeChip, 0, qt.Qt.AlignTop)
        self.degreeLabel = self._label("LNMQADegreeText")
        degreeRow.addWidget(self.degreeLabel, 1)
        layout.addLayout(degreeRow)
        self.degreeButton = qt.QPushButton(_("Compute degree map (~2-3 min for GSP1000)"))
        self.degreeButton.setObjectName("LNMQADegreeButton")
        self.degreeButton.setProperty("slicerRole", "secondary")
        self.degreeButton.setToolTip(_("Compute the connectome's hubness (degree) map once; it is stored in the bundle (derived/degree.npy)"))
        self.degreeButton.connect("clicked()", self.computeDegree)
        self.degreeButton.hide()
        layout.addWidget(self.degreeButton)
        self.degreeProgress = widgets.progressRow(objectNamePrefix="LNMQADegree")
        self.degreeProgress.widget.hide()
        layout.addWidget(self.degreeProgress.widget)

        nullRow = qt.QHBoxLayout()
        self.nullChip = widgets.chip(_("Null"), "neutral")
        self.nullChip.setObjectName("LNMQANullChip")
        nullRow.addWidget(self.nullChip, 0, qt.Qt.AlignTop)
        self.nullLabel = self._label("LNMQANullText")
        nullRow.addWidget(self.nullLabel, 1)
        self.nullRunButton = qt.QPushButton(_("Run"))
        self.nullRunButton.setObjectName("LNMQANullRunButton")
        self.nullRunButton.setProperty("slicerRole", "secondary")
        self.nullRunButton.setToolTip(_("Turn the relocated-lesion null on and map again"))
        self.nullRunButton.connect("clicked()", self.runWithNull)
        self.nullRunButton.hide()
        nullRow.addWidget(self.nullRunButton, 0, qt.Qt.AlignTop)
        layout.addLayout(nullRow)

        self.referenceLabel = self._label("LNMQAReferenceText")
        self.referenceLabel.hide()
        layout.addWidget(self.referenceLabel)
        self.coverageLabel = self._label("LNMQACoverageText", role="secondaryText")
        layout.addWidget(self.coverageLabel)

        for name in ("runChanged", "bundleChanged", "seedChanged"):
            getattr(ctx, name).append(self.refresh)
        self.refresh()

    @staticmethod
    def _label(objectName, role=None):
        label = qt.QLabel("")
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        if role:
            label.setProperty("slicerRole", role)
        return label

    #
    # Card protocol
    #

    def onRunEvent(self, event):
        kind = event.get("event")
        if kind == "start":
            self.qa = None
            qc = event.get("qc") or []
            self.qc = qc[0] if qc else None
            self.refresh(running=True)
        elif kind == "snapshot":
            self.refresh(running=True, n=event.get("n"), partial=True)
        elif kind == "done":
            qa = event.get("qa") or {}
            self.qa = next(iter(qa.values()), None) if qa else None
            qc = event.get("qc") or []
            self.qc = qc[0] if qc else self.qc

    def onRunSettingsChanged(self):
        self.refresh()

    def onResultChanged(self):
        pass

    def cleanup(self):
        for name in ("runChanged", "bundleChanged", "seedChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.refresh in callbacks:
                callbacks.remove(self.refresh)

    #
    # Display
    #

    def _nullRelocations(self):
        node = self.ctx.parameterNode
        try:
            return int(node.nullRelocations) if node is not None else 50
        except (AttributeError, TypeError, ValueError):
            return 50

    def refresh(self, running=False, n=None, partial=None):
        run = self.ctx.currentRun
        bundle = self.ctx.bundle
        job = getattr(run, "job", None) if run is not None else None
        if run is not None and not running:
            done = getattr(job, "doneEvent", None) or {}
            qa = done.get("qa") or {}
            if qa:
                self.qa = next(iter(qa.values()), None)
            start = getattr(job, "startEvent", None) or {}
            if start.get("qc"):
                self.qc = start["qc"][0]
        n = n if n is not None else (run.n if run is not None else None)
        partial = partial if partial is not None else bool(run is not None and run.partial)
        preview = bool(run is not None and (run.runJson.get("desc_suffix") or "").startswith("PreviewN"))
        levels = []

        # Badges: N, df, preview / partial
        if n:
            widgets.setChipLevel(self.nChip, "neutral", f"N={int(n):,}")
            widgets.setChipLevel(self.dfChip, "neutral", f"df={max(int(n) - 1, 0):,}")
        self.nChip.setVisible(bool(n))
        self.dfChip.setVisible(bool(n))
        subset = _("partial") if partial else (_("Preview") if preview else "")
        if subset:
            widgets.setChipLevel(self.subsetChip, "warn" if partial else "neutral", subset)
        self.subsetChip.setVisible(bool(subset))

        # Hubness (degree) similarity
        hasDegree = bundle is not None and self._bundleHasDegree(bundle)
        qa = self.qa or {}
        degreeR = qa.get("degree_r")
        if degreeR is None and hasDegree and run is not None and not running and not partial:
            degreeR, degreeRho, level = self._degreeFromMap(bundle)
            if degreeR is not None:
                qa = dict(qa, degree_r=degreeR, degree_rho=degreeRho, degree_level=level)
        if degreeR is not None:
            from LesionNetworkMappingLib import qa as qaLib

            level, text = qaLib.chip_level(degreeR)
            rho = qa.get("degree_rho")
            rhoText = f", ρ = {rho:.2f}" if isinstance(rho, (int, float)) and math.isfinite(rho) else ""  # noqa: RUF001
            self.degreeLabel.setText(_("Hubness (degree) similarity: r = {r:.2f}{rho} · {text}").format(r=degreeR, rho=rhoText, text=text))
            widgets.setChipLevel(self.degreeChip, _LEVELS.get(level, "neutral"), _("Hubness"))
            levels.append(level)
        elif bundle is None:
            self.degreeLabel.setText(_("Hubness (degree) similarity: choose a connectome."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        elif not hasDegree:
            self.degreeLabel.setText(_("Hubness (degree) similarity: not available, this connectome has no degree map yet."))
            widgets.setChipLevel(self.degreeChip, "warn", _("Hubness"))
        elif running or partial:
            self.degreeLabel.setText(_("Hubness (degree) similarity: computed when the map is complete."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        else:
            self.degreeLabel.setText(_("Hubness (degree) similarity: computed with every map."))
            widgets.setChipLevel(self.degreeChip, "neutral", _("Hubness"))
        degreeRunning = self.degreeJob is not None and not self.degreeJob.isFinished()
        self.degreeButton.setVisible(bundle is not None and not hasDegree and not degreeRunning)
        self.degreeProgress.widget.setVisible(degreeRunning)

        # Relocated-lesion null: always stated
        relocations = self._nullRelocations()
        null = qa.get("null") if qa else None
        self.nullRunButton.hide()
        if null and null.get("r_median") is not None:
            from LesionNetworkMappingLib import qa as qaLib

            level = qa.get("null_level") or "neutral"
            self.nullLabel.setText(qaLib.describe_null(null) + ".")
            widgets.setChipLevel(self.nullChip, _LEVELS.get(level, "neutral"), _("Null"))
            levels.append(level)
        elif run is not None and not running and not partial and not null:
            ran = (run.runJson.get("null") or {}).get("k") if isinstance(run.runJson.get("null"), dict) else None
            if ran:
                self.nullLabel.setText(_("Relocated null: not available for this lesion (no relocated lesion fits in the brain mask)."))
                widgets.setChipLevel(self.nullChip, "warn", _("Null"))
            else:
                self.nullLabel.setText(_("Relocated null: off for this run ({cost}). Run it to compare the map with relocated lesions of the same shape.").format(cost=nullCostText(bundle, n)))
                widgets.setChipLevel(self.nullChip, "warn", _("Null"))
                self.nullRunButton.show()
        elif partial:
            self.nullLabel.setText(_("Relocated null: not computed, the run stopped at N={n} (partial).").format(n=n))
            widgets.setChipLevel(self.nullChip, "warn", _("Null"))
        elif relocations > 0:
            self.nullLabel.setText(_("Relocated null: on, {k} relocated lesions in the same pass (computed with the map).").format(k=relocations))
            widgets.setChipLevel(self.nullChip, "neutral", _("Null"))
        else:
            self.nullLabel.setText(_("Relocated null: off ({cost}). Turn on Specificity QA in Compute to compare the map with relocated lesions.").format(cost=nullCostText(bundle)))
            widgets.setChipLevel(self.nullChip, "warn", _("Null"))

        # Reference QA (μC_r and PC1-3), when the run reports it (L-09/L-10)
        reference = qa.get("reference") if qa else None
        if reference and reference.get("text"):
            muR = reference.get("mu_r")
            muText = f"r(μC_r) = {muR:.2f} · " if isinstance(muR, (int, float)) and math.isfinite(muR) else ""
            self.referenceLabel.setText(_("Degree-likeness: {mu}{text}").format(mu=muText, text=reference["text"]))
            self.referenceLabel.show()
            levels.append(reference.get("level") or "neutral")
        else:
            self.referenceLabel.hide()

        # Coverage
        self.coverageLabel.setText(self._coverageText(bundle))

        if any(level in ("amber", "red") for level in levels):
            self.widget.collapsed = False

    def _coverageText(self, bundle):
        qc = self.qc
        if not qc or bundle is None:
            return _("Coverage: shown once the lesion is mapped.")
        spacing = abs(float(bundle.grid.spacing[0]))
        text = _("Coverage: {voxels:,} voxels on the connectome grid ({spacing:g} mm) · fractional sum {total:.1f} · {outside:.0%} outside the connectome mask").format(
            voxels=int(qc.get("voxels_in_mask", 0)), spacing=spacing, total=float(qc.get("weight_total", 0.0)), outside=float(qc.get("weight_outside_fraction", 0.0)))
        segNode, _segmentId = self.ctx.seed()
        normalization = segNode.GetAttribute("LNM.Normalization") if segNode is not None else None
        if normalization:
            text += " · " + _("normalized ({details})").format(details=normalization)
        return text

    @staticmethod
    def _bundleHasDegree(bundle):
        import os

        return os.path.isfile(os.path.join(bundle.path, "derived", "degree.npy"))

    def _degreeFromMap(self, bundle):
        """Degree similarity of the current T map with a degree map computed after the run."""
        run = self.ctx.currentRun
        paths = [path for path in (run.outputs if run is not None else []) if "_desc-T" in path]
        if not paths:
            return None, None, "neutral"
        try:
            from LesionNetworkMappingLib import nifti
            from LesionNetworkMappingLib import qa as qaLib

            data, _affine, _header = nifti.read_nifti(paths[0])
            values = data.reshape(data.shape[:3]).ravel()[bundle.mask_indices]
            r, rho = qaLib.degree_similarity(values, bundle.degree)
        except Exception:
            import logging

            logging.exception("Lesion network mapping: the degree similarity could not be computed")
            return None, None, "neutral"
        r = r if math.isfinite(r) else None
        rho = rho if math.isfinite(rho) else None
        return r, rho, qaLib.chip_level(r)[0]

    #
    # Actions
    #

    def computeDegree(self):
        """Run ``qa degree`` on the current connectome as a job (it writes derived/degree.npy and degree.json)."""
        from . import jobrunner

        bundle = self.ctx.bundle
        if bundle is None:
            return None
        spec = jobrunner.JobSpec(
            "LesionNetworkMapping", ["job", "LesionNetworkMappingLib.qa", "degree", "--bundle", bundle.path], title=_("Degree map"), heavy=True,
            kind="degree", name="degree", onEvent=lambda event: self.degreeProgress.update(), onDone=self._onDegreeDone)
        self.degreeJob = self.ctx.submit(spec)
        self.degreeProgress.setJob(self.degreeJob)
        self.refresh()
        return self.degreeJob

    def _onDegreeDone(self, job):
        self.degreeProgress.update()
        if job.state == "failed":
            slicer.util.warningDisplay(_("The degree map could not be computed: {reason}").format(reason=job.failureReason), windowTitle=_("Degree map"))
        self.refresh()

    def runWithNull(self):
        """Turn the relocated-lesion null on and map again."""
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is None:
            return
        widget.nullCheckBox.setChecked(True)
        widget.onPrimary()


CARD = {
    "id": "qa",
    "title": "QA",
    "order": ORDER_QA,
    "modes": ("lesion",),
    "factory": QACard,
}
