"""Clusters card of the Map module (order 110): the cluster and peak table of a map, as printed in a manuscript (L-32).

The table is the one of ``LesionNetworkMappingLib.clusters.cluster_table`` (nilearn ``get_clusters_table`` semantics): positive and
negative clusters that never merge, a minimum extent (shown in the header), sub-peaks "1a", "1b" more than 8 mm from a stronger
peak, the MNI coordinates of the peak voxel, its hemisphere, the value-weighted centre of gravity and the labels of the cached
atlases (Harvard-Oxford, Schaefer, Buckner, Tian: the percentage of the cluster in each region). Extents are descriptive: there
is no cluster-extent inference, and the card says so under the table, in the caption and in the exports.

- **Source.** The Result map (one lesion, or a map shown from a file or the scene), the group map shown in the Cohort card, or any
  NIfTI file. The threshold is the Result card's rule when it applies to the map, else a number typed here: the table always names
  the threshold it was made with. ``SourceModel`` is shared with the Networks card, so both read the same map and threshold.
- **Table.** A standard ``vtkMRMLTableNode`` ("LNM Clusters", saved as ``.tsv`` by Slicer's own storage) and a sortable table view
  (a click on a header sorts the clusters, each with its sub-peaks; the numbers sort as numbers). A click on a row (or the arrow
  keys and Enter) jumps the slice views to the peak: ``slicer.modules.markups.logic().JumpSlicesToLocation``.
- **Labels.** Atlases are never downloaded implicitly: a missing atlas is offered ("Get atlases", the TemplateFlow files of the
  catalog, a few hundred kB), and an atlas of another template family than the map is skipped with a notice, never mixed.
- **Export.** ``clusters.tsv`` and ``clusters.html`` (with the caption) into a folder of the user's choice.

Works in Classic and without a main window; nothing is computed while the card is collapsed (the table is marked "out of date"
and made when the card is opened).
"""

import json
import logging
import math
import os
import threading

import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import ORDER_CLUSTERS, widgets

#: Fork-only settings (SPEC Appendix B): the minimum extent, the label atlases and the last export folder (shared with Export)
MIN_EXTENT_SETTING = "Studio/LNM/Clusters/MinExtentMm3"
ATLASES_SETTING = "Studio/LNM/Clusters/Atlases"
LAST_FOLDER_SETTING = "Studio/LNM/LastExportFolder"
#: Minimum cluster extent when the user did not choose one (Siddiqi 2021 reports 100 mm³)
DEFAULT_MIN_EXTENT_MM3 = 100.0
#: A table with more clusters than this is not made: the threshold is too low to tell anything
MAX_CLUSTERS = 300
#: Atlases offered for the cluster labels (catalog ids; the first four are small, Tian is 18 MB)
LABEL_ATLASES = (
    "harvard-oxford-cortical-th25-2mm",
    "harvard-oxford-subcortical-th25-2mm",
    "schaefer2018-400-7n-2mm",
    "buckner2011-cerebellum-7n-1mm",
    "tian-s1-2mm",
)
#: Atlases used when the user has not chosen (those cached are used)
DEFAULT_LABEL_ATLASES = LABEL_ATLASES
SHORT_NAMES = {
    "harvard-oxford-cortical-th25-2mm": "HO cortical",
    "harvard-oxford-subcortical-th25-2mm": "HO subcortical",
    "schaefer2018-400-7n-2mm": "Schaefer-400 (7n)",
    "schaefer2018-400-17n-2mm": "Schaefer-400 (17n)",
    "buckner2011-cerebellum-7n-1mm": "Buckner cerebellum (7n)",
    "buckner2011-cerebellum-17n-1mm": "Buckner cerebellum (17n)",
    "tian-s1-2mm": "Tian S1",
}
#: Node attribute of the table node and of the group map volume of the Cohort card
TABLE_ATTRIBUTE = "LNM.ClusterTable"
GROUP_VOLUME_ATTRIBUTE = "LNM.GroupMapVolume"
COMPUTING_ATTRIBUTE = "LNM.Computing"
ROW_HEIGHT = 22
VISIBLE_ROWS = 12
#: Font size (px) of the dense tables of the Clusters and Networks cards
TABLE_FONT_PX = 11
SIGNS = ("both", "pos", "neg")
SIGN_TEXT = {"both": _("Both signs"), "pos": _("Positive"), "neg": _("Negative")}
UNLABELLED_FAMILIES = ("unknown", "native")


def mapWidget():
    """The Map module's widget (a controller of this package may be created before it is registered), or None."""
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _settings():
    return qt.QSettings()


def _readFloat(key, default):
    try:
        value = float(str(_settings().value(key)).strip())
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) and value >= 0 else default


def shortName(atlasId):
    """Short, readable name of a catalog atlas id."""
    return SHORT_NAMES.get(atlasId, atlasId)


def _setAlignment(item, alignment):
    try:
        item.setTextAlignment(alignment)
    except (TypeError, ValueError):
        item.setTextAlignment(int(alignment))  # Qt 5 takes an int, Qt 6 the flags


def _label(objectName, role=None, wrap=True):
    label = qt.QLabel("")
    label.setObjectName(objectName)
    label.setWordWrap(wrap)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    label.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
    if role:
        label.setProperty("slicerRole", role)
    return label


#
# The map a card works on
#


class SourceError(Exception):
    """The map cannot be used (none yet, still computing, not a statistic map); the message says what to do."""


class MapSource:
    """A map and the threshold it is read with.

    :ivar key: "result", "group" or "file:<path>"; ``name`` is shown to the user (never a path).
    :ivar volume: float64 array [i, j, k] on ``grid`` (zero or NaN outside the connectome mask).
    :ivar indices: flat C-order indices of the in-mask voxels (the connectome mask, else the finite non-zero voxels).
    :ivar statistic: "T", "Fz", "r" or a group map's desc; ``n`` the number of connectome subjects (None when unknown).
    :ivar threshold: absolute threshold: a ``thresholds.Resolved`` (the rule) or a number; ``value`` is its number.
    :ivar sign: "both", "pos" or "neg"; ``fromRule`` tells whether the threshold is the Result card's rule.
    :ivar runDir: folder of the run that made the map (its relocated null maps are looked for there), else None.
    :ivar desc: group map desc ("overlapPct", "meanT"...) for a group map; ``path`` the NIfTI file of a file or group map.
    :ivar space: template space the app knows for the map (``Studio.Space`` of its volume, a file's sidecar), else None:
      the space is then recognized from the grid (a map off the standard grids, such as a resampled or demo map, needs it).
    """

    def __init__(self, key, name, statistic, volume, grid, indices, threshold, sign="both", n=None, fromRule=False, runDir=None, desc=None, path=None, notes=(), token=None, space=None):
        self.key = key
        self.name = name
        self.statistic = statistic
        self.volume = volume
        self.grid = grid
        self.indices = indices
        self.threshold = threshold
        self.sign = sign
        self.n = n
        self.fromRule = fromRule
        self.runDir = runDir
        self.desc = desc
        self.path = path
        self.notes = list(notes)
        self.token = token  # identifies the version of the data (a volume's modification times, a file's)
        self.space = space

    @property
    def value(self):
        return float(getattr(self.threshold, "value", self.threshold))

    @property
    def vector(self):
        """The in-mask values (float64), in the order of ``indices``."""
        return self.volume.reshape(-1)[self.indices]

    def thresholdText(self):
        """"|T| ≥ 7.26", "T ≥ 4", "-Fz ≥ 0.2" for the sign of the map (the quantity the table is made of)."""
        quantity = {"both": f"|{self.statistic}|", "pos": self.statistic, "neg": f"-{self.statistic}"}[self.sign]
        return f"{quantity} ≥ {self.value:.4g}"

    def stamp(self):
        """What the table of this source depends on: the data, the statistic, the threshold and the sign."""
        return (self.key, self.token, self.statistic, self.value, self.sign, self.space)


def _templateSpace(space):
    """``space`` when it names a template family the atlases know ("MNI152NLin6Asym"...), else None (native, unknown)."""
    from LesionNetworkMappingLib.geometry import space_family

    return str(space) if space and space_family(space) not in UNLABELLED_FAMILIES else None


def volumeSpace(node):
    """Template space of a map volume as the app knows it (``StudioLib.space.spaceOf``: its ``Studio.Space`` first, else
    its grid), or None when that is no template family: the cards then recognize the space from the grid themselves.
    """
    try:
        from . import normalize_controller

        found = normalize_controller.spaceOf(node)
    except Exception:
        logging.exception("Lesion network mapping: the template space of a map could not be read")
        return None
    return _templateSpace(found[0]) if found else None


def fileSpace(path):
    """Template space declared for a map file (its ``_lnm.json`` sidecar: the connectome's space), or None."""
    try:
        from LesionNetworkMapping import LesionNetworkMappingLogic

        return _templateSpace(LesionNetworkMappingLogic.mapFileInfo(path).get("space"))
    except Exception:
        logging.exception("Lesion network mapping: the sidecar of a map file could not be read")
        return None


def _volumeValues(node):
    """(float64 array [i, j, k], Grid) of a scalar volume node."""
    import numpy as np

    from LesionNetworkMappingLib.geometry import Grid

    array = slicer.util.arrayFromVolume(node)
    volume = np.transpose(np.asarray(array, dtype=np.float64), (2, 1, 0))
    matrix = vtk.vtkMatrix4x4()
    node.GetIJKToRASMatrix(matrix)
    affine = np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])
    return volume, Grid(volume.shape, affine)


def _maskIndices(volume, grid, bundle):
    """The in-mask voxels: the connectome mask when the map lies on its grid, else the finite non-zero voxels."""
    import numpy as np

    if bundle is not None:
        try:
            if tuple(bundle.shape) == tuple(grid.shape) and np.allclose(np.asarray(bundle.affine), grid.affine, atol=1e-4):
                return np.asarray(bundle.mask_indices)
        except (AttributeError, TypeError, ValueError):
            pass
    flat = volume.reshape(-1)
    return np.flatnonzero(np.isfinite(flat) & (flat != 0))


def _kindDefault(desc, volume):
    """(default threshold, sign) of a group map from its desc."""
    import numpy as np

    from LesionNetworkMappingLib import display

    kind = display.groupMapKind(desc)
    finite = volume[np.isfinite(volume)]
    largest = float(np.abs(finite).max()) if finite.size else 0.0
    if kind == "count":
        return max(math.ceil(0.9 * largest), 1), "pos"
    if kind == "percent":
        return 90.0 if largest >= 90.0 else max(largest / 2.0, 0.5), "pos"
    if kind == "binary":
        return 0.5, "both"
    return None, "both"


class SourceModel:
    """Which map the Clusters and Networks cards read, and with which threshold (shared through :func:`sharedModel`).

    ``choice`` is "auto" (the group map in Cohort mode, else the Result map), "result", "group" or "file:<path>";
    ``custom`` is the threshold typed by the user (None: the default of the map), ``useRule`` whether the Result card's
    rule is used when it applies and ``signChoice`` "rule" (the rule's sign) or one of ``SIGNS`` for a typed threshold.
    Observers in ``listeners`` are called with no argument after any change.
    """

    def __init__(self, ctx):
        self.ctx = ctx
        self.choice = "auto"
        self.files = {}  # "file:<path>" -> path
        self.custom = None
        self.useRule = True
        self.signChoice = "rule"
        self.minExtent = _readFloat(MIN_EXTENT_SETTING, DEFAULT_MIN_EXTENT_MM3)
        self.listeners = []
        self._cache = None
        self._fileSpace = None  # (token, space) of the last map file read
        # The Result card's rule re-resolved to another threshold or sign: the "Result rule" sources changed
        thresholdChanged = getattr(ctx, "thresholdChanged", None)
        if thresholdChanged is not None:
            thresholdChanged.append(self.notify)

    #
    # State
    #

    def notify(self):
        for callback in list(self.listeners):
            try:
                callback()
            except Exception:
                logging.exception("Lesion network mapping: a map source observer failed")

    def setChoice(self, key):
        if key != self.choice:
            self.choice = key
            self.custom = None
            self.notify()

    def addFile(self, path):
        key = f"file:{os.path.abspath(path)}"
        self.files[key] = os.path.abspath(path)
        self.choice = key
        self.custom = None
        self.notify()
        return key

    def setCustom(self, value):
        value = None if value is None else float(value)
        if value != self.custom:
            self.custom = value
            self.notify()

    def setUseRule(self, flag):
        if bool(flag) != self.useRule:
            self.useRule = bool(flag)
            self.notify()

    def setSignChoice(self, sign):
        if sign != self.signChoice:
            self.signChoice = sign
            self.notify()

    def setMinExtent(self, value):
        value = max(float(value), 0.0)
        if value != self.minExtent:
            self.minExtent = value
            try:
                _settings().setValue(MIN_EXTENT_SETTING, f"{value:g}")
            except Exception:
                logging.exception("Lesion network mapping: the minimum cluster extent could not be remembered")
            self.notify()

    #
    # Sources
    #

    def seedMode(self):
        node = self.ctx.parameterNode
        try:
            return (node.seedMode or "lesion") if node is not None else "lesion"
        except AttributeError:
            return "lesion"

    def resultVolume(self):
        """The result volume of the Map module (a node with image data), or None."""
        node = self.ctx.resultVolume
        if node is None:
            parameters = self.ctx.parameterNode
            node = getattr(parameters, "resultVolume", None) if parameters is not None else None
        if node is None or node.GetScene() is None or node.GetImageData() is None:
            return None
        return node

    def groupVolume(self):
        """The volume that shows the group maps of the Cohort card, or None."""
        for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode"):
            if node.GetAttribute(GROUP_VOLUME_ATTRIBUTE) == "1" and node.GetImageData() is not None:
                return node
        return None

    def resolvedChoice(self):
        """The key the sources resolve to now ("result", "group" or a file key)."""
        if self.choice != "auto":
            if self.choice in self.files or self.choice in ("result", "group"):
                return self.choice
        if self.seedMode() == "cohort" and self.groupVolume() is not None:
            return "group"
        return "result"

    def entries(self):
        """[(key, label, available)] of the maps that can be chosen."""
        result = self.resultVolume()
        group = self.groupVolume()
        entries = [("result", _("Result map") + (f" · {result.GetName()}" if result is not None else ""), result is not None)]
        entries.append(("group", _("Group map") + (f" · {group.GetName()}" if group is not None else ""), group is not None))
        for key, path in self.files.items():
            entries.append((key, _("File · {name}").format(name=os.path.basename(path)), os.path.isfile(path)))
        return entries

    def source(self):
        """The current :class:`MapSource`; raises :class:`SourceError` with the reason when there is none."""
        key = self.resolvedChoice()
        try:
            if key == "result":
                source = self._volumeSource("result", self.resultVolume(), _("No map yet: Map network computes it, or open a map file."))
            elif key == "group":
                source = self._volumeSource("group", self.groupVolume(), _("No group map is shown: make the group maps in the Cohort card."))
            else:
                source = self._fileSource(key)
        except SourceError:
            raise
        except Exception as exc:  # whatever else goes wrong with a map is a message in the card, never a traceback from a slot
            logging.exception("Lesion network mapping: the map could not be read")
            raise SourceError(_("The map could not be read: {error}").format(error=exc)) from exc
        return source

    def _loaded(self, token, loader):
        """The (volume, grid, indices) of a map, read once per version of the data (``token``)."""
        if self._cache is not None and self._cache[0] == token:
            return self._cache[1]
        value = loader()
        self._cache = (token, value)
        return value

    def _volumeSource(self, key, node, emptyText):
        import numpy as np

        if node is None:
            raise SourceError(emptyText)
        if node.GetAttribute(COMPUTING_ATTRIBUTE) == "1":
            raise SourceError(_("The map is being computed: the table is made when it is complete."))
        token = (node.GetID(), node.GetImageData().GetMTime(), node.GetMTime(), id(self.ctx.bundle))

        def load():
            volume, grid = _volumeValues(node)
            return volume, grid, _maskIndices(volume, grid, self.ctx.bundle)

        volume, grid, indices = self._loaded(token, load)
        if indices.size == 0 or not np.isfinite(volume).any():
            raise SourceError(_("The map has no values."))
        widget = mapWidget()
        if key == "result":
            return self._resultSource(node, volume, grid, indices, widget, token)
        return self._groupSource(node, volume, grid, indices, widget, token)

    def _ruleThreshold(self, widget, statistic):
        """(Resolved, sign) of the Result card's rule when it applies to a map of ``statistic``, else (None, None)."""
        if widget is None:
            return None, None
        try:
            rule = widget.currentRule()
            resolved = getattr(widget, "resolved", None)
            if resolved is None or not math.isfinite(resolved.value) or resolved.value <= 0:
                return None, None
            if not (resolved.allowed or rule.get("override")) or widget._ruleStatistic(rule) != statistic:
                return None, None
            return resolved, widget._sign()
        except Exception:
            logging.exception("Lesion network mapping: the Result rule could not be read")
            return None, None

    def _resultSource(self, node, volume, grid, indices, widget, token):
        statistic = "T"
        n = None
        runDir = None
        if widget is not None:
            try:
                statistic = widget.currentStatistic()
                n = widget._resultN()
            except Exception:
                logging.exception("Lesion network mapping: the statistic of the result could not be read")
        run = self.ctx.currentRun
        if run is not None and widget is not None and getattr(widget, "loadedMap", None) is None and not getattr(run, "partial", False):
            runDir = run.runDir
        resolved, ruleSign = self._ruleThreshold(widget, statistic)
        notes = []
        if resolved is not None and self.useRule:
            threshold, sign, fromRule = resolved, ruleSign or "both", True
            if self.signChoice != "rule":
                sign = self.signChoice
        else:
            threshold = self.custom if self.custom is not None else (resolved.value if resolved is not None else self._fallbackThreshold(statistic))
            sign = self.signChoice if self.signChoice != "rule" else (ruleSign or "both")
            fromRule = False
            if resolved is None and widget is not None:
                notes.append(_("The Result rule does not apply to the {statistic} map: type a threshold.").format(statistic=statistic))
        if n is None and resolved is not None:
            n = resolved.n or None
        return MapSource("result", node.GetName(), statistic, volume, grid, indices, threshold, sign, n, fromRule, runDir, notes=notes, token=token, space=volumeSpace(node))

    @staticmethod
    def _fallbackThreshold(statistic):
        return {"T": 5.0, "Fz": 0.2, "r": 0.1}.get(statistic, 3.0)

    def _groupSource(self, node, volume, grid, indices, widget, token):
        from LesionNetworkMappingLib import display

        desc = node.GetAttribute(display.GROUP_MAP_ATTRIBUTE) or ""
        if display.groupMapKind(desc) == "p":
            raise SourceError(_("A p map is not clustered: choose a statistic, count or conjunction map."))
        default, sign = _kindDefault(desc, volume)
        if default is None:  # a statistic map (Welch t, mean T): the Result rule when it is a T rule, else a typical value
            resolved, _sign = self._ruleThreshold(widget, "T")
            default = resolved.value if resolved is not None else self._fallbackThreshold("T")
        threshold = self.custom if self.custom is not None else default
        if self.signChoice != "rule":
            sign = self.signChoice
        run = getattr(widget, "cohortMapsPath", None) if widget is not None else None
        folder = os.path.dirname(run) if run else None
        path = None
        if folder and desc:
            for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else ():
                if name.endswith(f"_desc-{desc}_map.nii.gz") and name.startswith("group_"):
                    path = os.path.join(folder, name)
                    break
        statistic = "T" if desc in ("meanT", "legacyWelchT") else ("Fz" if desc in ("meanFz",) else (desc or _("value")))
        record = getattr(widget, "cohortInfo", None) if widget is not None else None
        n = record.get("n") if isinstance(record, dict) else None
        return MapSource("group", node.GetName(), statistic, volume, grid, indices, threshold, sign, n, False, None, desc=desc, path=path, token=token, space=volumeSpace(node))

    def _fileSource(self, key):
        import numpy as np

        from LesionNetworkMappingLib import display
        from LesionNetworkMappingLib.geometry import Grid
        from LesionNetworkMappingLib.nifti import read_nifti

        path = self.files.get(key)
        if not path or not os.path.isfile(path):
            raise SourceError(_("The map file is not there any more."))
        token = (path, os.stat(path).st_mtime_ns, id(self.ctx.bundle))

        def load():
            try:
                data, affine, _header = read_nifti(path)
            except Exception as exc:
                raise SourceError(_("The file could not be read: {error}").format(error=exc)) from exc
            data = np.asarray(data)
            if data.ndim > 3 and all(extent == 1 for extent in data.shape[3:]):
                data = data.reshape(data.shape[:3])
            if data.ndim != 3:
                raise SourceError(_("The file is not a 3D map (shape {shape}): choose a statistic map.").format(shape="x".join(str(extent) for extent in data.shape)))
            volume = data.astype(np.float64)
            grid = Grid(volume.shape, affine)
            return volume, grid, _maskIndices(volume, grid, self.ctx.bundle)

        volume, grid, indices = self._loaded(token, load)
        if indices.size == 0 or not np.isfinite(volume).any():
            raise SourceError(_("The map has no values."))
        name = os.path.basename(path)
        statistic = "T" if "desc-T" in name else ("Fz" if "AvgRFz" in name else ("r" if "AvgR" in name else _("value")))
        desc = None
        marker = "_desc-"
        if marker in name:
            desc = name.split(marker, 1)[1].split("_", 1)[0]
        if desc and display.groupMapKind(desc) == "p":
            raise SourceError(_("A p map is not clustered: choose a statistic, count or conjunction map."))
        default, sign = (_kindDefault(desc, volume) if desc else (None, "both"))
        default = default if default is not None else self._fallbackThreshold(statistic)
        threshold = self.custom if self.custom is not None else default
        if self.signChoice != "rule":
            sign = self.signChoice
        if self._fileSpace is None or self._fileSpace[0] != token:
            self._fileSpace = (token, fileSpace(path))
        return MapSource(key, name, statistic, volume, grid, indices, threshold, sign, None, False, None, desc=desc, path=path, token=token, space=self._fileSpace[1])


def sharedModel(ctx):
    """The :class:`SourceModel` of a Map context (one for both cards)."""
    model = getattr(ctx, "lnmSourceModel", None)
    if model is None:
        model = SourceModel(ctx)
        ctx.lnmSourceModel = model
    return model


class SourceBar:
    """Map chooser and threshold controls bound to a :class:`SourceModel` (``widget`` is the row block)."""

    def __init__(self, model, prefix, withExtent=False):
        self.model = model
        self._syncing = False
        self.widget = qt.QWidget()
        self.widget.setObjectName(f"{prefix}SourceBar")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = qt.QHBoxLayout()
        row.addWidget(qt.QLabel(_("Map")))
        self.combo = qt.QComboBox()
        self.combo.setObjectName(f"{prefix}SourceCombo")
        self.combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self.combo.setToolTip(_("The map this card describes: the Result map, the group map of the Cohort card, or a NIfTI file"))
        self.combo.connect("activated(int)", self._onCombo)
        row.addWidget(self.combo, 1)
        self.openButton = qt.QToolButton()
        self.openButton.setObjectName(f"{prefix}OpenFileButton")
        self.openButton.setText(_("Open…"))
        self.openButton.setToolTip(_("Describe a NIfTI map file (it is not added to the scene)"))
        self.openButton.setProperty("slicerRole", "ghost")
        self.openButton.connect("clicked()", self.openFile)
        row.addWidget(self.openButton)
        layout.addLayout(row)
        self.ruleCheck = qt.QCheckBox("")
        self.ruleCheck.setObjectName(f"{prefix}UseRuleCheckBox")
        self.ruleCheck.connect("toggled(bool)", self._onUseRule)
        layout.addWidget(self.ruleCheck)
        thresholdRow = qt.QHBoxLayout()
        self.thresholdLabel = qt.QLabel(_("Threshold"))
        thresholdRow.addWidget(self.thresholdLabel)
        self.thresholdSpin = qt.QDoubleSpinBox()
        self.thresholdSpin.setObjectName(f"{prefix}ThresholdSpinBox")
        self.thresholdSpin.setPrefix("≥ ")
        self.thresholdSpin.setDecimals(3)
        self.thresholdSpin.setRange(0.001, 1e6)
        self.thresholdSpin.setSingleStep(0.5)
        self.thresholdSpin.setToolTip(_("Absolute cluster-forming threshold of the map (the table states it)"))
        self.thresholdSpin.connect("valueChanged(double)", self._onThreshold)
        thresholdRow.addWidget(self.thresholdSpin)
        self.signCombo = qt.QComboBox()
        self.signCombo.setObjectName(f"{prefix}SignCombo")
        for sign in SIGNS:
            self.signCombo.addItem(SIGN_TEXT[sign], sign)
        self.signCombo.connect("activated(int)", self._onSign)
        thresholdRow.addWidget(self.signCombo)
        thresholdRow.addStretch(1)
        layout.addLayout(thresholdRow)
        self.extentSpin = None
        if withExtent:
            extentRow = qt.QHBoxLayout()
            extentRow.addWidget(qt.QLabel(_("Minimum extent")))
            self.extentSpin = qt.QDoubleSpinBox()
            self.extentSpin.setObjectName(f"{prefix}MinExtentSpinBox")
            self.extentSpin.setRange(0, 1e6)
            self.extentSpin.setDecimals(0)
            self.extentSpin.setSingleStep(50)
            self.extentSpin.setSuffix(" mm³")
            self.extentSpin.setToolTip(_("Clusters smaller than this are left out of the table. A descriptive limit: it is not a cluster-extent test"))
            self.extentSpin.connect("valueChanged(double)", self._onExtent)
            extentRow.addWidget(self.extentSpin)
            extentRow.addStretch(1)
            layout.addLayout(extentRow)
        model.listeners.append(self.sync)
        self.sync()

    def cleanup(self):
        if self.sync in self.model.listeners:
            self.model.listeners.remove(self.sync)

    def _onCombo(self, index):
        if not self._syncing and index >= 0:
            key = self.combo.itemData(index)
            self.model.setChoice(str(key))

    def _onUseRule(self, flag):
        if not self._syncing:
            self.model.setUseRule(flag)

    def _onThreshold(self, value):
        if not self._syncing:
            self.model.setCustom(value)

    def _onSign(self, index):
        if not self._syncing and index >= 0:
            self.model.setSignChoice(str(self.signCombo.itemData(index)))

    def _onExtent(self, value):
        if not self._syncing:
            self.model.setMinExtent(value)

    def openFile(self, path=None):
        """Choose a NIfTI map file (``path`` skips the dialog) and describe it."""
        if not path:
            start = str(_settings().value(LAST_FOLDER_SETTING) or "")
            path = qt.QFileDialog.getOpenFileName(slicer.util.mainWindow(), _("Open a map"), start, "NIfTI (*.nii *.nii.gz);;All files (*)")
        if path:
            self.model.addFile(path)

    def refreshEntries(self):
        """Fill the combo with the maps that exist now (and select the one in use)."""
        self._syncing = True
        try:
            current = self.model.resolvedChoice()
            self.combo.clear()
            for key, text, available in self.model.entries():
                self.combo.addItem(text, key)
                if not available:
                    item = self.combo.model().item(self.combo.count - 1)  # the combo box's own QStandardItemModel
                    if item is not None:
                        item.setEnabled(False)  # not selectable
            self.combo.setCurrentIndex(max(0, self.combo.findData(current)))
        finally:
            self._syncing = False

    def sync(self):
        """Show the model: the entries, the rule check box, the typed threshold, the sign and the extent."""
        self.refreshEntries()
        self._syncing = True
        try:
            model = self.model
            try:
                source = model.source()
            except SourceError:
                source = None
            fromRule = source is not None and source.fromRule
            ruleApplies = False
            if model.resolvedChoice() == "result" and source is not None:
                resolved, _sign = model._ruleThreshold(mapWidget(), source.statistic)
                ruleApplies = resolved is not None
                if ruleApplies:
                    self.ruleCheck.setText(_("Result rule: {text}").format(text=MapSource.thresholdText(MapSource("r", "", source.statistic, None, None, None, resolved, _sign or "both"))))
            self.ruleCheck.setVisible(ruleApplies)
            self.ruleCheck.setChecked(bool(model.useRule and ruleApplies))
            typed = source is not None and not fromRule
            self.thresholdLabel.setEnabled(typed)
            self.thresholdSpin.setEnabled(typed)
            if source is not None:
                self.thresholdSpin.setValue(source.value)
                self.thresholdSpin.setPrefix("≥ ")
                self.signCombo.setCurrentIndex(max(0, self.signCombo.findData(source.sign)))
            self.signCombo.setEnabled(typed or (source is not None and fromRule))
            if self.extentSpin is not None:
                self.extentSpin.setValue(model.minExtent)
        finally:
            self._syncing = False


#
# Atlases
#


class AtlasManager:
    """The label atlases of the cache: which are there, loading them, and fetching the missing ones on an explicit click.

    Never downloads by itself. ``override`` ({catalog id: Atlas}) replaces the cache (tests and callers that hold atlases).
    ``listeners`` are called with no argument when the cache state or a download changes.
    """

    def __init__(self):
        self.override = {}
        self.listeners = []
        self.allowCrossFamily = False
        self.fetching = None  # {"thread", "timer", "state"} while a download runs
        self.message = ""
        self._status = None
        self._memo = {}

    def notify(self):
        for callback in list(self.listeners):
            try:
                callback()
            except Exception:
                logging.exception("Lesion network mapping: an atlas observer failed")

    def resource(self, atlasId):
        from LesionNetworkMappingLib import fetch

        return fetch.find_resource(atlasId, ("atlases",))

    def title(self, atlasId):
        if atlasId in self.override:
            return self.override[atlasId].title
        try:
            return self.resource(atlasId).title
        except Exception:
            return shortName(atlasId)

    def isCached(self, atlasId, refresh=False):
        if atlasId in self.override:
            return True
        if self._status is None or refresh:
            self._status = {}
        if atlasId not in self._status:
            try:
                from LesionNetworkMappingLib import fetch

                resource = self.resource(atlasId)
                labels = resource.label_resource()
                self._status[atlasId] = bool(fetch.is_cached(resource) and (labels is None or fetch.is_cached(labels)))
            except Exception:
                self._status[atlasId] = False
        return self._status[atlasId]

    def missing(self, ids):
        return [atlasId for atlasId in ids if not self.isCached(atlasId)]

    def load(self, ids):
        """The cached atlases of ``ids`` as ``atlases.Atlas`` objects (in the order of ``ids``); a damaged one is skipped."""
        from LesionNetworkMappingLib import atlases

        result = []
        for atlasId in ids:
            if atlasId in self.override:
                result.append(self.override[atlasId])
                continue
            if not self.isCached(atlasId):
                continue
            if atlasId not in self._memo:
                try:
                    self._memo[atlasId] = atlases.Atlas.load(atlasId)
                except Exception:
                    logging.exception(f"Lesion network mapping: the atlas {atlasId} could not be loaded")
                    continue
            result.append(self._memo[atlasId])
        return result

    def sizeText(self, ids):
        """Download size of the missing atlases ("about 0.3 MB")."""
        total, counted = 0, set()
        for atlasId in ids:
            try:
                resource = self.resource(atlasId)
                labels = resource.label_resource()
                for item in (resource, labels):
                    key = tuple(item.urls) if item is not None else None
                    if item is not None and key not in counted:  # the Tian archive holds the atlas and its labels: counted once
                        counted.add(key)
                        total += int(item.size or 0)
            except Exception:
                continue
        if total <= 0:
            return _("a few MB")
        return _("about {mb:.1f} MB").format(mb=max(total / 1e6, 0.1))

    def describe(self, ids):
        """One line per atlas: title, licence and source, for the confirmation of a download."""
        lines = []
        for atlasId in ids:
            try:
                resource = self.resource(atlasId)
                lines.append(f"{resource.title} ({resource.licence})")
            except Exception:
                lines.append(atlasId)
        return lines

    def busy(self):
        return self.fetching is not None and self.fetching["thread"].is_alive()

    def fetch(self, ids):
        """Download the missing atlases of ``ids`` in a background thread (an explicit user action; one at a time)."""
        if self.busy():
            return False
        ids = [atlasId for atlasId in ids if not self.isCached(atlasId)]
        if not ids:
            return False
        state = {"text": _("Downloading…"), "errors": [], "done": False}

        def work():
            from LesionNetworkMappingLib import fetch

            for atlasId in ids:
                try:
                    resource = fetch.find_resource(atlasId, ("atlases",))

                    def progress(done, total, title=resource.title):
                        state["text"] = _("Downloading {title}… {pct}%").format(title=title, pct=int(100 * done / total)) if total else _("Downloading {title}…").format(title=title)

                    fetch.fetch_set(resource, progress=progress)
                except Exception as exc:
                    state["errors"].append(f"{atlasId}: {type(exc).__name__}: {exc}")
            state["done"] = True

        timer = qt.QTimer()
        timer.setInterval(150)

        def poll():
            self.message = state["text"]
            if state["done"]:
                timer.stop()
                self._status = None
                self.fetching = None
                self.message = _("Some atlases could not be downloaded: {errors}").format(errors="; ".join(state["errors"])) if state["errors"] else ""
            self.notify()

        timer.connect("timeout()", poll)
        thread = threading.Thread(target=work, name="lnm-atlas-fetch", daemon=True)
        self.fetching = {"thread": thread, "timer": timer, "state": state}
        thread.start()
        timer.start()
        self.message = state["text"]
        self.notify()
        return True


def sharedAtlases(ctx):
    """The :class:`AtlasManager` of a Map context (one for both cards)."""
    manager = getattr(ctx, "lnmAtlasManager", None)
    if manager is None:
        manager = AtlasManager()
        ctx.lnmAtlasManager = manager
    return manager


def confirmFetch(manager, ids, parent=None):
    """Ask the user to confirm the download of atlases (their licences and size are named); return whether they agreed."""
    ids = manager.missing(ids)
    if not ids:
        return False
    text = _("Download {count} atlas files ({size}) from the open sources of the catalog (TemplateFlow, the Tian atlas site)?").format(count=len(ids), size=manager.sizeText(ids))
    text += "\n\n" + "\n".join(manager.describe(ids))
    box = qt.QMessageBox(qt.QMessageBox.Question, _("Get atlases"), text, qt.QMessageBox.Ok | qt.QMessageBox.Cancel, parent or slicer.util.mainWindow())
    run = getattr(box, "exec_", None) or box.exec  # Qt 5 PythonQt: exec_, Qt 6: exec
    return run() == qt.QMessageBox.Ok


#
# The card
#


def _columnSpecs(extras):
    """[(header text, tooltip, key)] of the columns of the view."""
    return [
        (_("ID"), _("Cluster number; the letters are sub-peaks"), "id"),
        (_("Sign"), _("Sign of the cluster: positive and negative clusters never merge"), "sign"),
        (_("Voxels"), _("Number of voxels of the cluster"), "voxels"),
        (_("mm³"), _("Volume of the cluster (descriptive extent)"), "mm3"),
        (_("Peak"), _("Value of the map at the peak voxel"), "peak"),
        *[(name, _("Value of the {name} map at the peak voxel").format(name=name), f"extra:{name}") for name in extras],
        ("x", _("x, y, z: MNI coordinates (mm) of the peak voxel"), "x"),
        ("y", "", "y"),
        ("z", "", "z"),
        (_("Side"), _("Hemisphere of the peak (shown when the template space is known)"), "side"),
        (_("Labels"), _("Atlas labels: the share of the cluster in each region"), "labels"),
    ]


class ClustersCard:
    """Controller of the Clusters card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.model = sharedModel(ctx)
        self.atlases = sharedAtlases(ctx)
        self.table = None  # the ClusterTable on show
        self.source = None
        self.knownSpace = False
        self.displayRows = []  # table rows in the order of the view
        self.sortColumn = None
        self.sortDescending = False
        self.tableNode = None
        self.lastFolder = None
        self.exported = []
        self._shown = None
        self._columns = _columnSpecs(())
        self._observed = {}  # node id -> (node, tag)
        self._sceneTag = None
        self._loading = False
        self.widget = widgets.card(_("Clusters"), objectName="LNMClustersCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        self.sourceBar = SourceBar(self.model, "LNMClusters", withExtent=True)
        layout.addWidget(self.sourceBar.widget)

        # Atlases
        atlasRow = qt.QHBoxLayout()
        self.atlasLabel = _label("LNMClustersAtlasText", "secondaryText")
        atlasRow.addWidget(self.atlasLabel, 1)
        self.atlasButton = qt.QToolButton()
        self.atlasButton.setObjectName("LNMClustersAtlasButton")
        self.atlasButton.setText(_("Atlases…"))
        self.atlasButton.setProperty("slicerRole", "ghost")
        self.atlasButton.setToolTip(_("Choose the atlases that label the clusters; missing ones can be downloaded"))
        self.atlasButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.atlasMenu = qt.QMenu(self.atlasButton)
        self.atlasMenu.setObjectName("LNMClustersAtlasMenu")
        self.atlasMenu.connect("aboutToShow()", self._fillAtlasMenu)
        self.atlasButton.setMenu(self.atlasMenu)
        atlasRow.addWidget(self.atlasButton)
        layout.addLayout(atlasRow)
        self.atlasBanner = widgets.banner("", [(_("Get atlases"), self.getAtlases)], level="neutral")
        self.atlasBanner.setObjectName("LNMClustersAtlasBanner")
        self.atlasBanner.hide()
        layout.addWidget(self.atlasBanner)

        # Table
        actionRow = qt.QHBoxLayout()
        self.updateButton = qt.QPushButton(_("Make table"))
        self.updateButton.setObjectName("LNMClustersUpdateButton")
        self.updateButton.setProperty("slicerRole", "secondary")
        self.updateButton.setToolTip(_("Make the cluster table of the map with the threshold above"))
        self.updateButton.connect("clicked()", self.update)
        actionRow.addWidget(self.updateButton)
        self.staleChip = widgets.chip(_("Out of date"), "warn")
        self.staleChip.setObjectName("LNMClustersStaleChip")
        self.staleChip.hide()
        actionRow.addWidget(self.staleChip)
        actionRow.addStretch(1)
        layout.addLayout(actionRow)
        self.headerLabel = _label("LNMClustersHeader")
        layout.addWidget(self.headerLabel)
        self.messageLabel = _label("LNMClustersMessage", "secondaryText")
        layout.addWidget(self.messageLabel)
        self.view = qt.QTableView()
        self.view.setObjectName("LNMClustersTable")
        self.itemModel = qt.QStandardItemModel(self.view)
        self.view.setModel(self.itemModel)
        self.view.setEditTriggers(qt.QAbstractItemView.NoEditTriggers)
        self.view.setSelectionBehavior(qt.QAbstractItemView.SelectRows)
        self.view.setSelectionMode(qt.QAbstractItemView.SingleSelection)
        self.view.setAlternatingRowColors(True)
        self.view.setWordWrap(False)
        self.view.verticalHeader().setVisible(False)
        self.view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.view.setHorizontalScrollMode(qt.QAbstractItemView.ScrollPerPixel)
        self.view.setToolTip(_("Click a row to jump the slice views to its peak; click a header to sort"))
        header = self.view.horizontalHeader()
        header.setSectionsClickable(True)
        header.setStretchLastSection(True)
        header.connect("sectionClicked(int)", self._onHeaderClicked)
        self.view.connect("clicked(QModelIndex)", self._onRowClicked)
        self.view.connect("activated(QModelIndex)", self._onRowClicked)
        self.view.selectionModel().connect("currentRowChanged(QModelIndex,QModelIndex)", self._onCurrentRowChanged)
        widgets._font(self.view, TABLE_FONT_PX)
        self.view.hide()
        layout.addWidget(self.view)
        self.detailLabel = _label("LNMClustersDetail")
        self.detailLabel.hide()
        layout.addWidget(self.detailLabel)
        self.noticeLabel = _label("LNMClustersNotices", "secondaryText")
        self.noticeLabel.hide()
        layout.addWidget(self.noticeLabel)
        self.noteLabel = _label("LNMClustersNote", "secondaryText")
        self.noteLabel.setText(_("Extents are descriptive (no cluster-extent inference)."))
        layout.addWidget(self.noteLabel)

        # Export
        exportRow = qt.QHBoxLayout()
        self.exportButton = qt.QPushButton(_("Export…"))
        self.exportButton.setObjectName("LNMClustersExportButton")
        self.exportButton.setProperty("slicerRole", "secondary")
        self.exportButton.setToolTip(_("Write clusters.tsv and clusters.html (with the caption) into a folder"))
        self.exportButton.connect("clicked()", self.export)
        self.exportButton.setEnabled(False)
        exportRow.addWidget(self.exportButton)
        self.showFolderButton = qt.QPushButton(_("Show folder"))
        self.showFolderButton.setObjectName("LNMClustersShowFolderButton")
        self.showFolderButton.setProperty("slicerRole", "ghost")
        self.showFolderButton.connect("clicked()", self.showFolder)
        self.showFolderButton.hide()
        exportRow.addWidget(self.showFolderButton)
        exportRow.addStretch(1)
        layout.addLayout(exportRow)
        self.exportLabel = _label("LNMClustersExportResult", "secondaryText")
        self.exportLabel.hide()
        layout.addWidget(self.exportLabel)

        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.connect("timeout()", self._autoUpdate)
        self.sceneTimer = qt.QTimer()
        self.sceneTimer.setSingleShot(True)
        self.sceneTimer.setInterval(200)
        self.sceneTimer.connect("timeout()", self.model.notify)
        try:
            self.widget.connect("contentsCollapsed(bool)", self._onCollapsed)
        except Exception:
            logging.debug("Lesion network mapping: the Clusters card cannot follow its expansion", exc_info=True)
        for name in ("runChanged", "resultChanged", "ruleChanged", "bundleChanged", "seedChanged"):
            getattr(ctx, name).append(self.schedule)
        self.model.listeners.append(self.schedule)
        self.atlases.listeners.append(self._onAtlasesChanged)
        self._sceneTag = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.NodeAddedEvent, self._onSceneChanged)
        self._sceneTagRemoved = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.NodeRemovedEvent, self._onSceneChanged)
        self._refreshAtlases()
        self._observeVolumes()
        self._refreshStatus()

    #
    # Card protocol
    #

    def onRunEvent(self, event):
        if event.get("event") == "start":
            self._clear()
            self.schedule()

    def onRunDone(self, job=None):
        self.schedule()

    def onCohortMapsChanged(self, path=None):
        self.schedule()

    def onStudyChanged(self):
        self.schedule()

    def onRunSettingsChanged(self):
        self.schedule()

    def cleanup(self):
        for name in ("runChanged", "resultChanged", "ruleChanged", "bundleChanged", "seedChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.schedule in callbacks:
                callbacks.remove(self.schedule)
        if self.schedule in self.model.listeners:
            self.model.listeners.remove(self.schedule)
        if self._onAtlasesChanged in self.atlases.listeners:
            self.atlases.listeners.remove(self._onAtlasesChanged)
        self.sourceBar.cleanup()
        self.timer.stop()
        self.sceneTimer.stop()
        for tag in (self._sceneTag, getattr(self, "_sceneTagRemoved", None)):
            if tag is not None:
                slicer.mrmlScene.RemoveObserver(tag)
        self._sceneTag = None
        self._dropObservers()

    #
    # Update scheduling
    #

    def expanded(self):
        try:
            return not bool(self.widget.collapsed)
        except (ValueError, RuntimeError):
            return False

    def signature(self, source):
        """What the table on show depends on: the map (and its threshold), the minimum extent and the atlases."""
        return (source.stamp(), self.model.minExtent, tuple(self.usableAtlasIds()), self.atlases.allowCrossFamily)

    def _currentSignature(self):
        try:
            return self.signature(self.model.source())
        except SourceError:
            return None
        except Exception:
            logging.exception("Lesion network mapping: the map of the Clusters card could not be read")
            return None

    def schedule(self, *args):
        """The inputs may have changed: update the out-of-date chip, and make the table now when the card is open."""
        self._observeVolumes()
        self._refreshStatus()
        if self.expanded() and self._isStale():
            self.timer.start()

    def _isStale(self):
        """True when no table is on show for the current inputs (nothing on show, or the inputs differ)."""
        return self._shown is None or self._currentSignature() != self._shown

    def _autoUpdate(self):
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return  # the card was destroyed
        if self._isStale():
            self.update()

    def _onCollapsed(self, collapsed):
        if not collapsed and self._isStale():
            self.timer.start()

    def _onSceneChanged(self, caller=None, event=None, *args):
        """A node was added or removed: the choice of maps may have changed (coalesced)."""
        self.sceneTimer.start()

    def _observeVolumes(self):
        """Watch the volumes the card reads: a change of their values marks the table out of date."""
        wanted = {}
        for node in (self.model.resultVolume(), self.model.groupVolume()):
            if node is not None:
                wanted[node.GetID()] = node
        for nodeId in list(self._observed):
            node, tag = self._observed[nodeId]
            if nodeId not in wanted or node.GetScene() is None:
                try:
                    node.RemoveObserver(tag)
                except Exception:
                    pass
                del self._observed[nodeId]
        for nodeId, node in wanted.items():
            if nodeId not in self._observed:
                tag = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self._onVolumeModified())
                self._observed[nodeId] = (node, tag)

    def _dropObservers(self):
        for node, tag in self._observed.values():
            try:
                node.RemoveObserver(tag)
            except Exception:
                pass
        self._observed.clear()

    def _onVolumeModified(self):
        self.schedule()

    def _refreshStatus(self):
        """The out-of-date chip: shown while a table is on show that no longer matches its inputs."""
        self.staleChip.setVisible(bool(self.table is not None and self._isStale()))
        self.updateButton.setText(_("Update table") if self.table is not None else _("Make table"))

    #
    # Atlases
    #

    def selectedAtlasIds(self):
        """Atlas ids chosen for the labels: the setting, else the defaults."""
        try:
            stored = json.loads(str(_settings().value(ATLASES_SETTING) or "null"))
        except ValueError:
            stored = None
        if isinstance(stored, list) and all(isinstance(item, str) for item in stored):
            return [atlasId for atlasId in stored if atlasId in LABEL_ATLASES]
        return list(DEFAULT_LABEL_ATLASES)

    def setSelectedAtlasIds(self, ids):
        _settings().setValue(ATLASES_SETTING, json.dumps(list(ids)))
        self._refreshAtlases()
        self.schedule()

    def usableAtlasIds(self):
        return [atlasId for atlasId in self.selectedAtlasIds() if self.atlases.isCached(atlasId)]

    def _fillAtlasMenu(self):
        menu = self.atlasMenu
        menu.clear()
        selected = set(self.selectedAtlasIds())
        for atlasId in LABEL_ATLASES:
            cached = self.atlases.isCached(atlasId, refresh=True)
            action = menu.addAction(shortName(atlasId) + ("" if cached else _(" (not downloaded)")))
            action.setCheckable(True)
            action.setChecked(atlasId in selected)
            action.setEnabled(cached)
            action.connect("toggled(bool)", lambda checked, key=atlasId: self._toggleAtlas(key, checked))
        menu.addSeparator()
        missing = self.atlases.missing(LABEL_ATLASES)
        fetchAction = menu.addAction(_("Get missing atlases…"))
        fetchAction.setEnabled(bool(missing) and not self.atlases.busy())
        fetchAction.connect("triggered()", self.getAtlases)
        crossAction = menu.addAction(_("Label across template families"))
        crossAction.setCheckable(True)
        crossAction.setChecked(self.atlases.allowCrossFamily)
        crossAction.setToolTip(_("Templates of different families differ by a few millimetres: the label is approximate"))
        crossAction.connect("toggled(bool)", self._toggleCrossFamily)

    def _toggleAtlas(self, atlasId, checked):
        ids = [item for item in self.selectedAtlasIds() if item != atlasId]
        if checked:
            ids.append(atlasId)
        self.setSelectedAtlasIds([item for item in LABEL_ATLASES if item in ids])

    def _toggleCrossFamily(self, checked):
        self.atlases.allowCrossFamily = bool(checked)
        self.schedule()

    def getAtlases(self):
        """Download the missing label atlases after the user confirmed (an explicit action; never implicit)."""
        ids = self.atlases.missing(LABEL_ATLASES)
        if ids and confirmFetch(self.atlases, ids):
            self.atlases.fetch(ids)

    def _onAtlasesChanged(self):
        self._refreshAtlases()
        if not self.atlases.busy():
            self.schedule()

    def _refreshAtlases(self):
        usable = self.usableAtlasIds()
        missing = self.atlases.missing(LABEL_ATLASES)
        busy = self.atlases.busy()
        if usable:
            self.atlasLabel.setText(_("Labels: {names}").format(names=", ".join(shortName(atlasId) for atlasId in usable)))
        else:
            self.atlasLabel.setText(_("Labels: none (no atlas on this computer)"))
        self.atlasLabel.setToolTip("\n".join(self.atlases.title(atlasId) for atlasId in usable))
        self.atlasBanner.setVisible((bool(missing) and not usable) or busy)
        if busy:
            widgets.setBannerText(self.atlasBanner, self.atlases.message or _("Downloading…"))
        elif missing and not usable:
            widgets.setBannerText(self.atlasBanner, self.atlases.message or _("The clusters are listed without anatomical labels: no atlas is on this computer ({size}).").format(size=self.atlases.sizeText(missing)))
        for button in self.atlasBanner.findChildren(qt.QToolButton):
            button.setEnabled(not busy)

    #
    # The table
    #

    def _clear(self):
        self.table = None
        self.source = None
        self.displayRows = []
        self._shown = None
        self._loading = True
        try:
            self.itemModel.clear()
        finally:
            self._loading = False
        self.view.hide()
        self.detailLabel.hide()
        self.headerLabel.setText("")
        self.noticeLabel.hide()
        self.exportButton.setEnabled(False)
        self._refreshStatus()

    def _showMessage(self, text):
        self._clear()
        self.messageLabel.setText(text)
        self.messageLabel.show()

    def countClusters(self, source):
        """Number of clusters that would be listed (before the labels are made); used to refuse an absurd table."""
        import numpy as np
        from scipy import ndimage

        volume = np.where(np.isfinite(source.volume), source.volume, 0.0)
        structure = ndimage.generate_binary_structure(3, 3)
        minVoxels = max(math.ceil(self.model.minExtent / max(source.grid.voxel_volume, 1e-9)), 1)
        total = 0
        for sign in (("pos", "neg") if source.sign == "both" else (source.sign,)):
            score = volume if sign == "pos" else -volume
            labels, count = ndimage.label(score >= source.value, structure=structure)
            if count:
                total += int((np.bincount(labels.ravel())[1:] >= minVoxels).sum())
        return total

    def siblingMaps(self, source):
        """{statistic: volume} of the other statistics of the run (their values at the peaks go into the table)."""
        import numpy as np

        widget = mapWidget()
        out = {}
        if source.key != "result" or widget is None or getattr(widget, "loadedMap", None) is not None or self.ctx.bundle is None:
            return out
        bundle = self.ctx.bundle
        for statistic in ("T", "Fz"):
            if statistic == source.statistic:
                continue
            try:
                values = widget._mapValues(statistic)
            except Exception:
                values = None
            if values is None or len(values) != len(source.indices) or not np.array_equal(np.asarray(bundle.mask_indices), source.indices):
                continue
            volume = np.zeros(source.grid.n_voxels)
            volume[source.indices] = values
            out[statistic] = volume.reshape(source.grid.shape)
        return out

    def update(self):
        """Make the cluster table of the current map and threshold; return the ``ClusterTable`` (None with a message when there is none)."""
        from LesionNetworkMappingLib import atlases as atlasLib
        from LesionNetworkMappingLib import clusters

        self.timer.stop()
        self.messageLabel.hide()
        try:
            source = self.model.source()
        except SourceError as exc:
            self._showMessage(str(exc))
            return None
        try:
            with slicer.util.WaitCursor():
                count = self.countClusters(source)
                if count > MAX_CLUSTERS:
                    self._showMessage(_("{count} clusters pass {threshold}: too many for a table. Raise the threshold or the minimum extent.").format(count=count, threshold=source.thresholdText()))
                    return None
                atlasList = self.atlases.load(self.usableAtlasIds())
                # The space the app knows for the map (Studio.Space, a sidecar) wins over recognizing its grid
                labeler = clusters.atlas_labeler(atlasList, top=3, min_pct=5.0, space=source.space, allow_cross_family=self.atlases.allowCrossFamily) if atlasList else None
                table = clusters.cluster_table(
                    source.volume, source.grid, None, source.threshold, sign=source.sign, min_extent_mm3=self.model.minExtent, labeler=labeler, extra_maps=self.siblingMaps(source), statistic=source.statistic,
                )
                family, _space = atlasLib.mask_family(source.grid.shape, source.grid.affine, space=source.space)
        except Exception as exc:
            logging.exception("Lesion network mapping: the cluster table could not be made")
            self._showMessage(_("The cluster table could not be made: {error}").format(error=exc))
            return None
        self.table = table
        self.source = source
        self.knownSpace = family not in UNLABELLED_FAMILIES
        self._shown = self.signature(source)
        self._columns = _columnSpecs(self._extraNames(table))
        self.sortColumn, self.sortDescending = None, False
        self._fillView()
        self._updateTableNode()
        self._showHeader(source, table, atlasList)
        self.exportButton.setEnabled(True)
        self._refreshStatus()
        return table

    @staticmethod
    def _extraNames(table):
        names = []
        for row in table:
            for name in row["extra"]:
                if name not in names:
                    names.append(name)
        return names

    def caption(self):
        """The caption of the table on show (threshold, rule, N, connectivity, extent, atlases, "descriptive")."""
        from LesionNetworkMappingLib import clusters

        if self.table is None:
            return ""
        titles = [shortName(atlasId) for atlasId in self.table.atlases]
        n = self.source.n if self.source is not None else None
        df = (n - 1) if n else None
        caption = clusters.cluster_caption(self.table, n=n, df=df, atlases=titles, space="MNI" if self.knownSpace else _("map-grid"))
        if self.source is not None:
            caption = _("Map: {name}. ").format(name=self.source.name) + caption
        return caption

    def _showHeader(self, source, table, atlasList):
        clustersCount = len(table.clusters)
        parts = [source.thresholdText(), _("minimum extent {extent:g} mm³").format(extent=table.min_extent_mm3), _("{connectivity}-connected").format(connectivity=table.connectivity)]
        parts.append(_("{count} clusters").format(count=clustersCount) if clustersCount != 1 else _("1 cluster"))
        if table.dropped:
            parts.append(_("{count} smaller left out").format(count=table.dropped))
        self.headerLabel.setText(" · ".join(parts))
        self.headerLabel.setToolTip(self.caption())
        notices = [notice["message"] if isinstance(notice, dict) else str(notice) for notice in table.notices]
        notices.extend(source.notes)
        if not self.knownSpace:
            notices.append(_("The template space of this map is not recognized: the side and the atlas labels are not shown."))
        elif not atlasList:
            notices.append(_("No atlas labels: choose or download atlases (Atlases…)."))
        if clustersCount == 0:
            notices.insert(0, _("No cluster passes {threshold} with a minimum extent of {extent:g} mm³.").format(threshold=source.thresholdText(), extent=table.min_extent_mm3))
        self.noticeLabel.setText(" ".join(notices))
        self.noticeLabel.setVisible(bool(notices))

    def _cell(self, row, key):
        """(display text, alignment) of a row's cell for a column key."""
        if key == "id":
            return row["id"], "l"
        if key == "sign":
            return "+" if row["sign"] == "pos" else "−", "c"  # noqa: RUF001
        if key == "voxels":
            return ("" if row["voxels"] is None else f"{row['voxels']:,}"), "r"
        if key == "mm3":
            return ("" if row["mm3"] is None else f"{row['mm3']:,.0f}"), "r"
        if key == "peak":
            return f"{row['peak']:.2f}", "r"
        if key.startswith("extra:"):
            value = row["extra"].get(key[6:])
            return ("" if value is None or not math.isfinite(value) else f"{value:.2f}"), "r"
        if key in ("x", "y", "z"):
            return f"{row['xyz']['xyz'.index(key)]:.0f}", "r"
        if key == "side":
            if not self.knownSpace:
                return "–", "c"  # noqa: RUF001
            return {"L": _("Left"), "R": _("Right"), "midline": _("Midline")}.get(row["hemisphere"], row["hemisphere"]), "l"
        if key == "labels":
            if not self.knownSpace:
                return "", "l"
            return "; ".join(f"{shortName(atlas)}: {label} ({pct:.0f}%)" for atlas, label, pct in row["labels"]), "l"
        return "", "l"

    def _sortValue(self, row, key):
        if key == "id":
            return (row["cluster"], 0)
        if key == "sign":
            return 0 if row["sign"] == "pos" else 1
        value = {"voxels": row["voxels"], "mm3": row["mm3"], "peak": row["peak"]}.get(key)
        if key in ("x", "y", "z"):
            value = row["xyz"]["xyz".index(key)]
        if key.startswith("extra:"):
            value = row["extra"].get(key[6:])
        if key in ("side", "labels"):
            return self._cell(row, key)[0].lower()
        return -math.inf if value is None else value

    def _blocks(self):
        """The cluster rows, each with its sub-peaks, in the order of the table."""
        blocks = {}
        for row in self.table:
            blocks.setdefault(row["cluster"], []).append(row)
        return [blocks[number] for number in sorted(blocks)]

    def _fillView(self):
        """Fill the view from the table, in the sort order (``sortColumn`` None: the order of the table)."""
        blocks = self._blocks()
        if self.sortColumn is not None:
            key = self._columns[self.sortColumn][2]
            blocks.sort(key=lambda block: self._sortValue(block[0], key), reverse=self.sortDescending)
        self.displayRows = [row for block in blocks for row in block]
        self._loading = True
        try:
            self.itemModel.clear()
            self.itemModel.setColumnCount(len(self._columns))
            self.itemModel.setHorizontalHeaderLabels([name for name, _tip, _key in self._columns])
            for column, (_name, tip, _key) in enumerate(self._columns):
                if tip:
                    self.itemModel.setHeaderData(column, qt.Qt.Horizontal, tip, qt.Qt.ToolTipRole)
            for rowIndex, row in enumerate(self.displayRows):
                for column, (_name, _tip, key) in enumerate(self._columns):
                    text, align = self._cell(row, key)
                    item = qt.QStandardItem(text)
                    alignment = {"l": qt.Qt.AlignLeft, "r": qt.Qt.AlignRight, "c": qt.Qt.AlignHCenter}[align] | qt.Qt.AlignVCenter
                    _setAlignment(item, alignment)
                    if row["subpeak"]:
                        item.setForeground(qt.QBrush(slicer.app.palette().color(qt.QPalette.Disabled, qt.QPalette.WindowText)))
                    else:
                        font = item.font()
                        font.setBold(key == "id")
                        item.setFont(font)
                    if key == "labels":
                        item.setToolTip(text.replace("; ", "\n"))
                    elif key == "id":
                        item.setToolTip(_("Peak {x:.0f}, {y:.0f}, {z:.0f}: click to jump there").format(x=row["xyz"][0], y=row["xyz"][1], z=row["xyz"][2]))
                    self.itemModel.setItem(rowIndex, column, item)
        finally:
            self._loading = False
        header = self.view.horizontalHeader()
        for column in range(len(self._columns)):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        labelsColumn = len(self._columns) - 1
        header.setSectionResizeMode(labelsColumn, qt.QHeaderView.Interactive)
        self.view.setColumnWidth(labelsColumn, 240)
        self.detailLabel.hide()
        header.setSortIndicatorShown(self.sortColumn is not None)
        if self.sortColumn is not None:
            header.setSortIndicator(self.sortColumn, qt.Qt.DescendingOrder if self.sortDescending else qt.Qt.AscendingOrder)
        rows = len(self.displayRows)
        self.view.setFixedHeight(ROW_HEIGHT * (min(rows, VISIBLE_ROWS) + 1) + 24)
        self.view.setVisible(rows > 0)

    def _onHeaderClicked(self, column):
        if self.table is None or not 0 <= column < len(self._columns):
            return
        if self.sortColumn == column:
            self.sortDescending = not self.sortDescending
        else:
            self.sortColumn, self.sortDescending = column, False
        self._fillView()

    def sortBy(self, key, descending=False):
        """Sort the clusters by a column key ("id", "mm3", "peak"...); each cluster keeps its sub-peaks below it."""
        for column, (_name, _tip, columnKey) in enumerate(self._columns):
            if columnKey == key:
                self.sortColumn, self.sortDescending = column, bool(descending)
                self._fillView()
                return True
        return False

    def _onRowClicked(self, index):
        self.jumpToRow(index.row())

    def _onCurrentRowChanged(self, current, previous):
        if not self._loading and current.isValid():
            self.jumpToRow(current.row())

    def detailText(self, row):
        """What a row says in full: the cluster or sub-peak, its peak and side, and every atlas label with its share."""
        x, y, z = row["xyz"]
        side = {"L": _("left"), "R": _("right"), "midline": _("midline")}.get(row["hemisphere"], "") if self.knownSpace else ""
        place = _("peak at {x:.0f}, {y:.0f}, {z:.0f}").format(x=x, y=y, z=z) + (f" ({side})" if side else "")
        if row["subpeak"]:
            return _("Sub-peak {id}: {place}.").format(id=row["id"], place=place)
        text = _("Cluster {id}: {voxels:,} voxels ({mm3:,.0f} mm³), {place}.").format(id=row["id"], voxels=row["voxels"], mm3=row["mm3"], place=place)
        if self.knownSpace and row["labels"]:
            text += " " + "; ".join(f"{shortName(atlas)}: {label} ({pct:.0f}%)" for atlas, label, pct in row["labels"]) + "."
        if row["network"]:
            text += " " + _("Network: {name}.").format(name=row["network"])
        return text

    def _showDetail(self, rowIndex):
        if 0 <= rowIndex < len(self.displayRows):
            self.detailLabel.setText(self.detailText(self.displayRows[rowIndex]))
            self.detailLabel.show()

    def jumpToRow(self, rowIndex):
        """Jump the slice views to the peak of a row of the view; return the (x, y, z) or None."""
        if not 0 <= rowIndex < len(self.displayRows):
            return None
        x, y, z = self.displayRows[rowIndex]["xyz"]
        slicer.modules.markups.logic().JumpSlicesToLocation(x, y, z, True)
        self._showDetail(rowIndex)
        return (x, y, z)

    def selectRow(self, rowIndex):
        """Select a row of the view (this jumps to its peak, like a click)."""
        self.view.selectRow(rowIndex)
        self.view.setFocus()
        return self.jumpToRow(rowIndex)

    #
    # The table node
    #

    def _updateTableNode(self):
        """The cluster table as a standard vtkMRMLTableNode ("LNM Clusters"): saved by Slicer as .tsv, readable by official Slicer."""
        node = self.tableNode
        if node is None or node.GetScene() is None:
            node = next((candidate for candidate in slicer.util.getNodesByClass("vtkMRMLTableNode") if candidate.GetAttribute(TABLE_ATTRIBUTE) == "1"), None)
        if node is None:
            node = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLTableNode", "LNM Clusters")
            node.SetAttribute(TABLE_ATTRIBUTE, "1")
        self.tableNode = node
        self.fillTableNode(node, self.table, self.source, self.caption())
        return node

    @staticmethod
    def fillTableNode(node, table, source, caption=""):
        """Put ``table`` (a ``ClusterTable``) into a vtkMRMLTableNode: typed columns, the header, the threshold as attributes."""
        extras = ClustersCard._extraNames(table)
        vtkTable = node.GetTable()
        wasModifying = node.StartModify()
        node.RemoveAllColumns()

        def addColumn(name, kind, values):
            array = {"s": vtk.vtkStringArray, "d": vtk.vtkDoubleArray}[kind]()
            array.SetName(name)
            for value in values:
                array.InsertNextValue(value)
            vtkTable.AddColumn(array)

        nan = math.nan
        addColumn("cluster_id", "s", [row["id"] for row in table])
        addColumn("sign", "s", ["+" if row["sign"] == "pos" else "-" for row in table])
        addColumn("voxels", "d", [nan if row["voxels"] is None else float(row["voxels"]) for row in table])
        addColumn("volume_mm3", "d", [nan if row["mm3"] is None else float(row["mm3"]) for row in table])
        addColumn("peak", "d", [row["peak"] for row in table])
        for name in extras:
            addColumn(name, "d", [row["extra"].get(name, nan) for row in table])
        for axis, name in enumerate("xyz"):
            addColumn(name, "d", [row["xyz"][axis] for row in table])
        addColumn("hemisphere", "s", [row["hemisphere"] for row in table])
        for axis, name in enumerate(("cog_x", "cog_y", "cog_z")):
            addColumn(name, "d", [nan if row["cog"] is None else row["cog"][axis] for row in table])
        addColumn("labels", "s", ["; ".join(f"{atlas}: {label} ({pct:.1f}%)" for atlas, label, pct in row["labels"]) for row in table])
        addColumn("network", "s", [row["network"] or "" for row in table])
        titles = {"cluster_id": _("Cluster"), "sign": _("Sign"), "voxels": _("Voxels"), "volume_mm3": _("Volume (mm³)"), "peak": _("Peak"), "hemisphere": _("Hemisphere"), "labels": _("Labels"), "network": _("Network"),
                  "cog_x": _("CoG x"), "cog_y": _("CoG y"), "cog_z": _("CoG z")}
        if hasattr(node, "SetColumnTitle"):  # Slicer 5.13: titles are shown by the Tables module, the files keep the column names
            for name, title in titles.items():
                node.SetColumnTitle(name, title)
        if hasattr(node, "SetUseColumnTitleAsColumnHeader"):
            node.SetUseColumnTitleAsColumnHeader(True)
        else:
            node.SetUseColumnNameAsColumnHeader(True)
        node.SetAttribute("LNM.Statistic", str(table.statistic))
        node.SetAttribute("LNM.Threshold", f"{table.threshold:.6g}")
        node.SetAttribute("LNM.MinExtentMm3", f"{table.min_extent_mm3:g}")
        node.SetAttribute("LNM.Caption", caption)
        node.EndModify(wasModifying)
        node.Modified()

    #
    # Export
    #

    def export(self, folder=None):
        """Write clusters.tsv and clusters.html into ``folder`` (asked when None); return the written paths or None."""
        if self.table is None:
            return None
        if not folder:
            start = str(_settings().value(LAST_FOLDER_SETTING) or "")
            folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Export the cluster table into…"), start)
            if not folder:
                return None
            _settings().setValue(LAST_FOLDER_SETTING, folder)
        try:
            return self.exportTo(folder)
        except Exception:  # exportTo has logged it and shown "Export failed" in the card; the button slot must not print a traceback
            return None

    def exportTo(self, folder):
        """Write ``clusters.tsv`` and ``clusters.html`` into ``folder`` (created when needed); return the two paths."""
        from LesionNetworkMappingLib import clusters

        tsv, page = os.path.join(folder, "clusters.tsv"), os.path.join(folder, "clusters.html")
        try:
            os.makedirs(folder, exist_ok=True)
            clusters.write_clusters(self.table, tsv_path=tsv, html_path=page, caption=self.caption())
        except Exception as exc:
            logging.exception("Lesion network mapping: the cluster table could not be exported")
            self.exportLabel.setText(_("Export failed: {error}").format(error=exc))
            self.exportLabel.show()
            raise
        self.lastFolder = folder
        self.exported = [tsv, page]
        self.exportLabel.setText(_("Exported clusters.tsv and clusters.html (the caption says the extents are descriptive) to {path}").format(path=folder))
        self.exportLabel.show()
        self.showFolderButton.show()
        return tsv, page

    def showFolder(self):
        if self.lastFolder and os.path.isdir(self.lastFolder):
            widgets.showInFileManager(self.lastFolder)


CARD = {
    "id": "clusters",
    "title": "Clusters",
    "order": ORDER_CLUSTERS,
    "modes": ("lesion", "cohort"),
    "factory": ClustersCard,
}
