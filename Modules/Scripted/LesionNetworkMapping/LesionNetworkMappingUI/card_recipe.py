"""Recipe header of the Map module (order 5; L-39, LNM addendum §7.1 and F23).

A header strip above the Seed card (every seed mode, Studio and Classic):

- **Recipe combo** (``LNMRecipeCombo``): the built-in recipes, the study's ``recipes/`` and the folders of the fork-only
  setting ``Studio/LNM/RecipeFolders``, each with its status tag ("adapted from Ferguson et al. 2019", "2026 preprint",
  "lab recipe") and a "partial" tag when a step is not available in this build. Nothing is chosen at start: the Map
  settings stay as they are until a recipe is picked. Picking one sets the primary threshold rule (Result card) and the
  options of Run everything (``Studio/LNM/Pipeline/Options`` through card_pipeline): steps that are not in the recipe
  are turned off there, so the Run everything pipeline (A-04) runs the recipe's steps. The study remembers the choice
  (``derivatives/lnm/recipe.json``). The rule and the options are not kept per study, so opening a study with a
  remembered recipe and an open plan sets them to that recipe's again (not to another study's), and a study without a
  remembered recipe shows none (a recipe picked before any study was open carries over to the first one). Opening a
  study whose plan is locked sets the rule, the Run everything options and the Cohort analyses' settings to the plan's
  (with its recorded deviations), as **Use the plan's settings** does; nothing is recorded.
- **Status line** (``LNMRecipeStatus``): the status tag, "Custom (based on X) · 2 changes" when the Map settings differ
  from the recipe, and the partial-recipe badge "1 step not available in this build: Coordinate seeds".
- **Plan chip** (``LNMPlanChip``): "Plan: open" or "Plan: locked 2026-10-18 · 3f2a1b9c" ("modified after lock" in red
  when the plan file no longer matches its sha256). **Lock plan…** (``LNMLockPlanButton``) asks for the primary
  hypothesis (contrast or score), a-priori ROI masks (hashed), alpha, permutations and seed, and writes
  ``derivatives/lnm/analysis_plan.json``.
- After the lock, a change of the rule or of the Run everything options shows an **inline reason row**
  (``LNMDeviationPrompt``: "Changed after the plan was locked: n_perm 1000 instead of 5000", a reason field and
  **Record**). Until the reason is recorded in ``analysis_plan_deviations.jsonl``, Compute and Run everything are
  blocked (``MapContext.setComputeEnabled(False, …, key="recipe")``); setting the value back clears it. The Cohort
  card's Contrast and Symptom analyses hold their own permutations, covariates and inference options
  (``recipes.analysis_pending``): their changes are shown in the same row ("Contrast GLM: n_perm 1000 instead of
  5000"), hold only that analysis's Run until the reason is recorded, and are recorded as "contrast.n_perm".
- **Deviations (n)** (``LNMDeviationsButton``) opens a popover with the log; the ⋯ menu has Recipe details…,
  Save as lab recipe…, Add recipe folder… and Reload recipes.

Search entries (``StudioLib.palette.registerProvider``): "Choose an analysis recipe (lesion network mapping)…",
"Lock analysis plan…" and "Show deviations from the analysis plan".

Settings: reads and extends ``Studio/LNM/RecipeFolders`` (a list of folders; only when the user adds one); writes
``Studio/LNM/Pipeline/Options`` through ``card_pipeline.saveOptions`` only when a recipe is picked, a plan is locked or
restored, or a study with a locked plan or a remembered recipe is opened. No prompt, setting or file is written at start
or under ``--testing``.
"""

import html
import logging
import os

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_RECIPE, widgets

#: Pipeline options are re-read this often while a plan is locked and the header is shown (no change signal exists)
POLL_MS = 1500
#: Compute block key of this header (MapContext.setComputeEnabled)
BLOCK_KEY = "recipe"
#: The header must fit the inspector (SPEC 7.9)
HEADER_WIDTH = 340
POPOVER_WIDTH = 380


def _mapWidget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _recipes():
    from LesionNetworkMappingLib import recipes

    return recipes


def _pipelineCard():
    """``card_pipeline`` (the Run everything options), or None when it is not part of this build."""
    try:
        from . import card_pipeline
    except Exception:
        return None
    return card_pipeline


def recipeFolders():
    """The folders of the fork-only setting ``Studio/LNM/RecipeFolders`` (read only)."""
    value = qt.QSettings().value(_recipes().RECIPE_FOLDERS_SETTING)
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    return [str(folder) for folder in value if str(folder).strip()]


def addRecipeFolder(folder):
    """Add a folder to ``Studio/LNM/RecipeFolders`` (fork-only key; the user chose the folder)."""
    folders = recipeFolders()
    if folder and folder not in folders:
        folders.append(folder)
        qt.QSettings().setValue(_recipes().RECIPE_FOLDERS_SETTING, folders)
    return folders


def _exec(dialog):
    run = getattr(dialog, "exec_", None) or dialog.exec  # Qt 5 PythonQt: exec_, Qt 6: exec
    return run()


def _label(text="", objectName=None, role=None, wrap=True):
    label = qt.QLabel(text)
    if objectName:
        label.setObjectName(objectName)
    if role:
        label.setProperty("slicerRole", role)
    label.setWordWrap(wrap)
    label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
    return label


class Popover:
    """A popup frame with one rich text label (Esc or a click outside closes it)."""

    def __init__(self, parent, objectName):
        self.frame = qt.QFrame(parent, qt.Qt.Popup)
        self.frame.setObjectName(objectName)
        self.frame.setFrameShape(qt.QFrame.StyledPanel)
        self.frame.setFixedWidth(POPOVER_WIDTH)
        layout = qt.QVBoxLayout(self.frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = qt.QScrollArea()
        self.scroll.setObjectName(f"{objectName}Scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(qt.QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        layout.addWidget(self.scroll)
        self.label = qt.QLabel()
        self.label.setObjectName(f"{objectName}Text")
        self.label.setWordWrap(True)
        self.label.setTextFormat(qt.Qt.RichText)
        self.label.setOpenExternalLinks(True)
        self.label.setTextInteractionFlags(qt.Qt.TextBrowserInteraction)
        self.label.setContentsMargins(12, 10, 12, 10)
        self.label.setAlignment(qt.Qt.AlignTop | qt.Qt.AlignLeft)
        self.scroll.setWidget(self.label)

    def show(self, anchor, text):
        """Show the text below ``anchor``; the popover is as tall as the wrapped text, up to 70% of the screen (then it scrolls)."""
        self.label.setText(text)
        width = POPOVER_WIDTH - 2 * self.frame.frameWidth
        height = self.label.heightForWidth(width - self.scroll.verticalScrollBar().sizeHint.width()) + 2 * self.frame.frameWidth
        screen = anchor.screen() if hasattr(anchor, "screen") else None
        geometry = getattr(screen, "availableGeometry", None) if screen is not None else None
        geometry = geometry() if callable(geometry) else geometry  # a property in PythonQt, a method elsewhere
        available = geometry.height() if geometry is not None else 768
        self.frame.setFixedHeight(int(max(60, min(height, 0.7 * available))))
        self.frame.move(anchor.mapToGlobal(qt.QPoint(0, anchor.height)))
        self.frame.show()


class LockPlanDialog:
    """Lock plan…: the primary hypothesis, a-priori ROI masks, alpha, permutations and seed."""

    def __init__(self, parent, label, params, scores, rule):
        self.dialog = qt.QDialog(parent)
        self.dialog.setObjectName("LNMLockPlanDialog")
        self.dialog.setWindowTitle(_("Lock the analysis plan"))
        self.dialog.setMinimumWidth(420)
        layout = qt.QVBoxLayout(self.dialog)
        form = qt.QFormLayout()
        layout.addLayout(form)
        form.addRow(_("Recipe:"), _label(label, "LNMLockPlanRecipe"))
        self.kindCombo = qt.QComboBox()
        self.kindCombo.setObjectName("LNMLockPlanKind")
        self.kindCombo.addItem(_("Group contrast"), "contrast")
        if scores:
            self.kindCombo.addItem(_("Score"), "score")
        form.addRow(_("Primary hypothesis:"), self.kindCombo)
        self.targetCombo = qt.QComboBox()
        self.targetCombo.setObjectName("LNMLockPlanTarget")
        form.addRow("", self.targetCombo)
        self.scores = list(scores)
        self.contrast = params["design"]["contrast"]
        self.kindCombo.connect("currentIndexChanged(int)", self._fillTargets)
        if params["design"]["test"] == "continuous" and scores:
            self.kindCombo.setCurrentIndex(1)
        self._fillTargets()
        form.addRow(_("Primary threshold:"), _label(_recipes().rule_text(rule) + _(" (change it in Result before locking)"), "LNMLockPlanRule"))
        self.alphaSpin = qt.QDoubleSpinBox()
        self.alphaSpin.setObjectName("LNMLockPlanAlpha")
        self.alphaSpin.setDecimals(3)
        self.alphaSpin.setRange(0.001, 0.2)
        self.alphaSpin.setSingleStep(0.01)
        self.alphaSpin.setValue(params["alpha"])
        form.addRow(_("Alpha (FWE):"), self.alphaSpin)
        self.permSpin = qt.QSpinBox()
        self.permSpin.setObjectName("LNMLockPlanPerm")
        self.permSpin.setRange(_recipes().MIN_N_PERM, 1000000)
        self.permSpin.setSingleStep(1000)
        self.permSpin.setValue(params["n_perm"])
        form.addRow(_("Permutations:"), self.permSpin)
        self.seedSpin = qt.QSpinBox()
        self.seedSpin.setObjectName("LNMLockPlanSeed")
        self.seedSpin.setRange(0, 2147483647)
        self.seedSpin.setValue(params["seeds"]["permutation"])
        form.addRow(_("Permutation seed:"), self.seedSpin)
        self.roiList = qt.QListWidget()
        self.roiList.setObjectName("LNMLockPlanRois")
        self.roiList.setMaximumHeight(90)
        form.addRow(_("A-priori ROIs:"), self.roiList)
        buttons = qt.QHBoxLayout()
        add = qt.QPushButton(_("Add ROI mask…"))
        add.setObjectName("LNMLockPlanAddRoi")
        add.connect("clicked()", self._addRoi)
        remove = qt.QPushButton(_("Remove"))
        remove.setObjectName("LNMLockPlanRemoveRoi")
        remove.connect("clicked()", lambda: self.roiList.takeItem(self.roiList.currentRow) if self.roiList.currentRow >= 0 else None)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        form.addRow("", buttons)
        note = _label(_("Locking writes derivatives/lnm/analysis_plan.json with its own sha256. Every later change of these settings in Map asks for a reason, "
                        "and the methods text lists it as a deviation from the plan."), "LNMLockPlanNote", "caption")
        layout.addWidget(note)
        # The flags constructor yields an empty box under PythonQt: set the buttons afterwards
        box = qt.QDialogButtonBox()
        box.setStandardButtons(qt.QDialogButtonBox.Ok | qt.QDialogButtonBox.Cancel)
        box.button(qt.QDialogButtonBox.Ok).setText(_("Lock plan"))
        box.connect("accepted()", self.dialog.accept)
        box.connect("rejected()", self.dialog.reject)
        layout.addWidget(box)

    def _fillTargets(self, *args):
        self.targetCombo.clear()
        if self.kindCombo.currentData == "score":
            for name in self.scores:
                self.targetCombo.addItem(name, name)
        else:
            for value, text in (("A>B", _("case > control")), ("B>A", _("control > case")), ("two_sided", _("case ≠ control (two-sided)"))):
                self.targetCombo.addItem(text, value)
            index = self.targetCombo.findData(self.contrast)
            self.targetCombo.setCurrentIndex(max(0, index))

    def _addRoi(self):
        paths = qt.QFileDialog.getOpenFileNames(self.dialog, _("Add a-priori ROI masks"), "", "NIfTI (*.nii *.nii.gz);;All files (*)")
        for path in paths or []:
            item = qt.QListWidgetItem(os.path.basename(path))
            item.setData(qt.Qt.UserRole, path)
            item.setToolTip(path)
            self.roiList.addItem(item)

    def hypotheses(self):
        kind = self.kindCombo.currentData
        primary = {"kind": "score", "score": self.targetCombo.currentData} if kind == "score" else {"kind": "contrast", "contrast": self.targetCombo.currentData}
        rois = [self.roiList.item(row).data(qt.Qt.UserRole) for row in range(self.roiList.count)]
        return {"primary": primary, "rois": rois, "alpha": self.alphaSpin.value, "n_perm": self.permSpin.value, "seeds": {"permutation": self.seedSpin.value}}

    def exec(self):
        return _exec(self.dialog) == qt.QDialog.Accepted


class _ShowFilter(qt.QObject):
    """Calls ``callback`` soon after the watched widget is shown (returning to Map restarts the plan check)."""

    def __init__(self, widget, callback):
        qt.QObject.__init__(self, widget)
        self.callback = callback
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            qt.QTimer.singleShot(0, self.callback)
        return False


class RecipeHeader:
    """The Recipe header strip (``HEADER`` factory)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.recipes = []
        self.loadErrors = []
        self.recipe = None  # the chosen recipes.Recipe
        self.pending = []
        self._blocked = False
        self._cleanedUp = False
        self._lastStudyPath = None
        self._buildWidget()
        self.pollTimer = qt.QTimer()
        self.pollTimer.setInterval(POLL_MS)
        self.pollTimer.connect("timeout()", self._poll)
        self._showFilter = _ShowFilter(self.widget, self._onShown)
        for name in ("studyChanged", "ruleChanged"):
            getattr(ctx, name).append(self.refresh)
        ctx.beforeCompute.append(self.checkBeforeCompute)
        self.reload()

    #
    # Widgets
    #

    def _buildWidget(self):
        self.widget = qt.QFrame()
        self.widget.setObjectName("LNMRecipeHeader")
        self.widget.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(6, 4, 6, 4)  # aligned with the content of the cards below
        layout.setSpacing(4)

        self.comboRow = qt.QWidget()
        self.comboRow.setObjectName("LNMRecipeComboRow")
        row = qt.QHBoxLayout(self.comboRow)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self._layout = layout
        #: Studio presentation (``onPresentationChanged``): no row while nothing is chosen and no study is open
        self._studio = False
        self.combo = qt.QComboBox()
        self.combo.setObjectName("LNMRecipeCombo")
        self.combo.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Fixed)
        self._comboToolTip = _("Recipe · a named, cited analysis pipeline; picking one sets the threshold rule and the Run everything steps")
        self.combo.setToolTip(self._comboToolTip)
        self.combo.connect("activated(int)", self.onRecipeActivated)
        row.addWidget(self.combo, 1)
        self.menuButton = qt.QToolButton()
        self.menuButton.setObjectName("LNMRecipeMenuButton")
        self.menuButton.setText("⋯")
        self.menuButton.setToolTip(_("Recipe actions · details, save as lab recipe, recipe folders"))
        self.menuButton.setAccessibleName(_("Recipe actions"))
        self.menuButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.menuButton.setAutoRaise(True)
        self.menu = qt.QMenu(self.menuButton)
        self.menu.setObjectName("LNMRecipeMenu")
        self.detailsAction = self.menu.addAction(_("Recipe details…"))
        self.detailsAction.connect("triggered()", self.showDetails)
        self.saveLabAction = self.menu.addAction(_("Save as lab recipe…"))
        self.saveLabAction.connect("triggered()", self.saveLabRecipe)
        self.addFolderAction = self.menu.addAction(_("Add recipe folder…"))
        self.addFolderAction.connect("triggered()", self.chooseRecipeFolder)
        self.reloadAction = self.menu.addAction(_("Reload recipes"))
        self.reloadAction.connect("triggered()", self.reload)
        self.menuButton.setMenu(self.menu)
        row.addWidget(self.menuButton)
        layout.addWidget(self.comboRow)

        self.statusLabel = _label("", "LNMRecipeStatus", "secondaryText")
        layout.addWidget(self.statusLabel)

        self.planRow = qt.QWidget()
        self.planRow.setObjectName("LNMPlanRow")
        row = qt.QHBoxLayout(self.planRow)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.planChip = widgets.chip(_("Plan: open"), "neutral")
        self.planChip.setObjectName("LNMPlanChip")
        self.planChip.setToolTip(_("Analysis plan · locked plans record every later change with its reason"))
        row.addWidget(self.planChip)
        self.lockButton = qt.QPushButton(_("Lock plan…"))
        self.lockButton.setObjectName("LNMLockPlanButton")
        self.lockButton.setToolTip(_("Lock plan · state the hypotheses and settings before the analysis (derivatives/lnm/analysis_plan.json)"))
        self.lockButton.connect("clicked()", self.onLockClicked)
        row.addWidget(self.lockButton)
        self.deviationsButton = qt.QToolButton()
        self.deviationsButton.setObjectName("LNMDeviationsButton")
        self.deviationsButton.setAutoRaise(True)
        self.deviationsButton.setToolTip(_("Deviations from the locked plan, with their reasons"))
        self.deviationsButton.connect("clicked()", self.showDeviations)
        row.addWidget(self.deviationsButton)
        row.addStretch(1)
        layout.addWidget(self.planRow)

        self.prompt = qt.QFrame()
        self.prompt.setObjectName("LNMDeviationPrompt")
        promptLayout = qt.QVBoxLayout(self.prompt)
        promptLayout.setContentsMargins(0, 2, 0, 2)
        promptLayout.setSpacing(4)
        self.promptLabel = _label("", "LNMDeviationPromptText")
        promptLayout.addWidget(self.promptLabel)
        row = qt.QHBoxLayout()
        self.reasonEdit = qt.QLineEdit()
        self.reasonEdit.setObjectName("LNMDeviationReason")
        self.reasonEdit.setPlaceholderText(_("Reason for the change (recorded in the methods)"))
        self.reasonEdit.connect("returnPressed()", self.recordPending)
        self.reasonEdit.connect("textChanged(QString)", lambda text: self.recordButton.setEnabled(bool(text.strip())))
        row.addWidget(self.reasonEdit, 1)
        self.recordButton = qt.QPushButton(_("Record"))
        self.recordButton.setObjectName("LNMRecordDeviationButton")
        self.recordButton.setEnabled(False)
        self.recordButton.connect("clicked()", self.recordPending)
        row.addWidget(self.recordButton)
        promptLayout.addLayout(row)
        self.restoreButton = qt.QPushButton(_("Use the plan's settings"))
        self.restoreButton.setObjectName("LNMRestorePlanButton")
        self.restoreButton.setToolTip(_("Set the threshold rule, the Run everything options and the Cohort analyses' settings back to the locked plan (nothing is recorded)"))
        self.restoreButton.setProperty("slicerRole", "ghost")
        self.restoreButton.connect("clicked()", self.restorePlanSettings)
        promptLayout.addWidget(self.restoreButton, 0, qt.Qt.AlignLeft)
        self.prompt.setVisible(False)
        layout.addWidget(self.prompt)

        self.deviationsPopover = Popover(self.widget, "LNMDeviationsPopover")
        self.detailsPopover = Popover(self.widget, "LNMRecipeDetails")

    #
    # State
    #

    def study(self):
        return getattr(self.ctx, "study", None)

    def studyPath(self):
        study = self.study()
        return os.fspath(study.path) if study is not None and getattr(study, "path", None) else None

    def plan(self):
        path = self.studyPath()
        return _recipes().read_plan(path) if path else None

    def rule(self):
        rule = getattr(self.ctx, "rule", None)
        widget = _mapWidget()
        if rule is None and widget is not None and hasattr(widget, "currentRule"):
            try:
                rule = widget.currentRule()
            except Exception:
                rule = None
        return rule

    def options(self):
        module = _pipelineCard()
        if module is None:
            return None
        try:
            return module.loadOptions()
        except Exception:
            logging.exception("Lesion network mapping: the Run everything options could not be read")
            return None

    def baseParameters(self):
        """The locked parameters (with recorded deviations), else the chosen recipe's, else the defaults."""
        recipes = _recipes()
        path = self.studyPath()
        if path:
            effective = recipes.effective_parameters(path)
            if effective is not None:
                return effective
        return recipes.apply(self.recipe) if self.recipe is not None else recipes.normalize_parameters({})

    def currentParameters(self):
        """The parameters as Map holds them now: the base with the current rule and Run everything options."""
        recipes = _recipes()
        try:
            return recipes.parameters_from_map(self.baseParameters(), rule=self.rule(), options=self.options())
        except (ValueError, TypeError):
            logging.exception("Lesion network mapping: the current Map settings could not be read as recipe parameters")
        try:
            return self.baseParameters()
        except (ValueError, TypeError):  # a hand-edited plan with invalid parameters (the chip says "modified")
            logging.exception("Lesion network mapping: the locked plan's parameters are not valid")
            return recipes.apply(self.recipe) if self.recipe is not None else recipes.normalize_parameters({})

    def analysisTabs(self):
        """The Cohort-card analyses that hold their own copy of plan parameters (``PLAN_SCOPE``: Contrast, Symptom)."""
        card = self.ctx.card("cohort")
        return [controller for _key, controller in getattr(card, "tabControllers", None) or [] if getattr(controller, "PLAN_SCOPE", None)]

    def analysisPending(self):
        """Changes of the Cohort-card analyses from the locked plan that wait for a reason (each with its ``scope``)."""
        pending = []
        for tab in self.analysisTabs():
            try:
                pending.extend(tab.planPending())
            except Exception:
                logging.exception("Lesion network mapping: a Cohort analysis could not be compared with the locked plan")
        return pending

    def currentSteps(self):
        """The steps Run everything would run now (the plan's or the recipe's steps, following the options)."""
        recipes = _recipes()
        path = self.studyPath()
        planned = recipes.effective_steps(path) if path else None
        steps = planned if planned is not None else (self.recipe.steps if self.recipe is not None else None)
        options = self.options()
        if steps is None or options is None:
            return steps
        return recipes.steps_from_options(steps, options)

    #
    # Recipes
    #

    def reload(self):
        """Read the recipes again (built-ins, the study's recipes/ folder, Studio/LNM/RecipeFolders) and refill the combo."""
        recipes = _recipes()
        self.loadErrors = []
        try:
            self.recipes = recipes.load_recipes(study=self.studyPath(), folders=recipeFolders(), errors=self.loadErrors)
        except Exception:
            logging.exception("Lesion network mapping: the recipes could not be read")
            self.recipes = []
        path = self.studyPath()
        previous = self._lastStudyPath
        opened = bool(path) and path != previous
        self._lastStudyPath = path
        chosen = self.recipe.id if self.recipe is not None else None
        selected = None
        if path:
            selected = (recipes.selected_recipe(path) or {}).get("id")  # the study's choice (its locked plan's recipe first)
            # Another study's recipe is not this study's choice; one picked before any study was open carries over
            if selected or (opened and previous):
                chosen = selected
        self.recipe = next((recipe for recipe in self.recipes if recipe.id == chosen), None)
        self._fillCombo()
        # A study with a locked plan was opened: Map starts from the plan's settings (with its recorded deviations), not
        # from the defaults of a new session or another study's options, so only the user's later changes ask for a reason
        if opened and self._planLocked(path):
            if self.restorePlanSettings():
                return
        elif opened and selected and self.recipe is not None:
            # An open plan with a remembered recipe: the rule and the Run everything options are not kept per study, so
            # they are set to this recipe's again (Run everything runs its steps, not those another study left)
            try:
                params = recipes.apply(self.recipe)
            except (ValueError, TypeError):
                logging.exception("Lesion network mapping: the study's recipe parameters are not valid")
            else:
                self._setMapSettings(params, self.recipe.steps, "recipe")
        self.refresh()

    def _planLocked(self, path):
        try:
            return _recipes().read_plan(path) is not None
        except Exception:
            logging.exception("Lesion network mapping: the analysis plan could not be read")
            return False

    def _fillCombo(self):
        recipes = _recipes()
        self.combo.blockSignals(True)
        try:
            self.combo.clear()
            self.combo.addItem(_("Choose a recipe…"), "")
            for recipe in self.recipes:
                tag = {"adapted": _("adapted"), "established": _("established"), "2026-preprint": _("2026 preprint"), "lab": _("lab")}.get(recipe.status, recipe.status)
                text = f"{recipe.title} · {tag}"
                badge = recipes.partial_badge(recipe)
                if badge:
                    text += " · " + _("partial")
                self.combo.addItem(text, recipe.id)
                tip = recipe.status_text() + (f"\n{badge}" if badge else "")
                self.combo.setItemData(self.combo.count - 1, tip, qt.Qt.ToolTipRole)
            index = self.combo.findData(self.recipe.id) if self.recipe is not None else 0
            self.combo.setCurrentIndex(max(0, index))
        finally:
            self.combo.blockSignals(False)

    def recipeById(self, recipeId):
        return next((recipe for recipe in self.recipes if recipe.id == recipeId), None)

    def onRecipeActivated(self, index):
        recipeId = self.combo.itemData(index)
        recipe = self.recipeById(recipeId) if recipeId else None
        if recipe is None:
            self.combo.setCurrentIndex(max(0, self.combo.findData(self.recipe.id)) if self.recipe is not None else 0)
            return
        self.applyRecipe(recipe)

    def applyRecipe(self, recipe):
        """Choose a recipe: set the threshold rule and the Run everything options, and remember it in the study."""
        recipes = _recipes()
        if self.plan() is not None:
            self.refresh()  # the combo is disabled while a plan is locked; nothing changes
            return False
        self.recipe = recipe
        self._setMapSettings(recipes.apply(recipe), recipe.steps, "recipe")
        path = self.studyPath()
        if path:
            try:
                recipes.select_recipe(path, recipe)
            except OSError:
                logging.exception("Lesion network mapping: the recipe choice could not be saved in the study")
        index = self.combo.findData(recipe.id)
        if index >= 0 and self.combo.currentIndex != index:
            self.combo.setCurrentIndex(index)
        self.refresh()
        return True

    def _setMapSettings(self, params, steps, source):
        """Set the threshold rule and the Run everything options to ``params`` and ``steps`` (a recipe's or the plan's)."""
        recipes = _recipes()
        widget = _mapWidget()
        try:
            if widget is not None and hasattr(widget, "setRule"):
                widget.setRule(params["rule"])
            else:
                self.ctx.setRule(params["rule"])
        except Exception:
            logging.exception(f"Lesion network mapping: the {source}'s threshold rule could not be set")
        module = _pipelineCard()
        if module is not None:
            try:
                module.saveOptions(recipes.pipeline_options(params, steps, module.loadOptions()))
                card = self.ctx.card("pipeline")
                if card is not None and hasattr(card, "onRunSettingsChanged"):
                    card.onRunSettingsChanged()
            except Exception:
                logging.exception(f"Lesion network mapping: the Run everything options could not follow the {source}")

    #
    # Refresh
    #

    def refresh(self):
        """Update the status line, the plan chip, the buttons and the deviation prompt."""
        if self._cleanedUp:
            return
        if self.studyPath() != self._lastStudyPath:
            self.reload()
            return
        recipes = _recipes()
        path = self.studyPath()
        status = recipes.plan_status(path) if path else {"state": "open", "chip": _("Plan: open"), "deviations": 0}
        locked = status["state"] != "open"
        params = self.currentParameters()
        parts = []
        if self.recipe is not None:
            changes = recipes.diff(self.recipe, params)
            if changes:
                label = recipes.custom_label(self.recipe, params)
                parts.append(f"{label} · " + (_("1 change") if len(changes) == 1 else _("{n} changes").format(n=len(changes))))
            parts.append(self.recipe.status_text())
            badge = recipes.partial_badge(self.recipe)
            if badge:
                parts.append(badge)
        if self.recipe is not None and not path:
            parts.append(_("Open a study to lock an analysis plan."))
        elif self.recipe is None and path and locked:
            title = (status.get("recipe") or {}).get("title") or (status.get("recipe") or {}).get("id") or _("its recipe")
            parts.append(_("The plan was locked with {recipe}, which is not in the loaded recipe folders (⋯ ▸ Add recipe folder…).").format(recipe=title))
        elif self.recipe is None and path:
            parts.append(_("No recipe chosen: Map uses its own settings. Choose one to lock an analysis plan."))
        # Compact while there is nothing to say: no recipe and no study is one row (the combo says "Choose a recipe…")
        self.statusLabel.setText(" · ".join(parts))
        self.statusLabel.setVisible(bool(parts))
        self.planRow.setVisible(bool(path))
        self.statusLabel.setToolTip("\n".join(f"{change['description']}" for change in recipes.diff(self.recipe, params)) if self.recipe is not None else "")
        level = {"open": "neutral", "locked": "ok", "modified": "danger"}[status["state"]]
        # The Cohort analyses' own changes (scoped) only hold their own Run; the others block every computation
        self.pending = recipes.pending_deviations(path, params, steps=self.currentSteps()) + self.analysisPending() if locked and path else []
        if self.pending:
            level = "warn"
        widgets.setChipLevel(self.planChip, level, status["chip"])
        self.lockButton.setVisible(not locked)
        self.lockButton.setEnabled(bool(path) and self.recipe is not None)
        self.lockButton.setToolTip(_("Lock plan · state the hypotheses and settings before the analysis (derivatives/lnm/analysis_plan.json)") if path and self.recipe is not None
                                   else (_("Open a study to lock an analysis plan") if not path else _("Choose a recipe to lock an analysis plan")))
        self.deviationsButton.setText(_("Deviations ({n})").format(n=status.get("deviations", 0)))
        self.deviationsButton.setVisible(locked)
        self.combo.setEnabled(not locked)
        if locked:
            self.combo.setToolTip(_("The plan is locked with {recipe}; change settings in Map (each change asks for a reason)").format(recipe=(status.get("recipe") or {}).get("title") or _("its recipe")))
        else:
            self.combo.setToolTip(self._comboToolTip)
        self.saveLabAction.setEnabled(self.recipe is not None)
        self.detailsAction.setEnabled(self.recipe is not None)
        self._showPrompt()
        # Studio: nothing to say without a study, a recipe or a plan (VISUAL 4.5); the recipes are in the Map ⋯ menu
        idle = self._studio and not path and self.recipe is None and not locked and not self.pending
        self.comboRow.setVisible(not idle)
        if idle:
            self.statusLabel.hide()
        elif self._studio and path and self.recipe is None and not locked and not self.pending:
            # A study with no recipe and an open plan: one row, the combo; what the line said is its tooltip (there is no
            # plan to lock before a recipe is chosen)
            self.statusLabel.hide()
            self.planRow.hide()
            self.combo.setToolTip(" · ".join([*parts, self._comboToolTip]))
        self._layout.setContentsMargins(*((0, 0, 0, 0) if idle else (6, 4, 6, 4)))
        if locked and self.widget.isVisible():
            self.pollTimer.start()
        else:
            self.pollTimer.stop()

    def onPresentationChanged(self, studio):
        """Map entered in Studio (True) or in Classic (False)."""
        self._studio = bool(studio)
        if not self._cleanedUp:
            self.refresh()

    def menuItems(self):
        """The recipes for the Map ⋯ menu (Studio): ``[(title, callback, checked)]``; empty while a plan is locked."""
        recipes = _recipes()
        path = self.studyPath()
        status = recipes.plan_status(path) if path else {"state": "open"}
        if status["state"] != "open":
            return []
        items = []
        for recipe in self.recipes:
            items.append((recipe.title, lambda recipe=recipe: self.applyRecipe(recipe), self.recipe is not None and recipe.id == self.recipe.id))
        return items

    def _onShown(self):
        if not self._cleanedUp:
            self.refresh()  # options may have changed while Map was hidden (the poll timer stops then)

    def _poll(self):
        if self._cleanedUp or not self.widget.isVisible():
            self.pollTimer.stop()
            return
        self.refresh()

    def _showPrompt(self):
        pending = self.pending
        if pending:
            text = _("Changed after the plan was locked: {changes}.").format(changes="; ".join(change["description"] for change in pending))
            self.promptLabel.setText(text)
        self.prompt.setVisible(bool(pending))
        blocked = any(not change.get("scope") for change in pending)
        if blocked != self._blocked or blocked:
            self.ctx.setComputeEnabled(not blocked, _("Give a reason for the change from the locked analysis plan (Recipe, top of Map)") if blocked else "", key=BLOCK_KEY)
            self._blocked = blocked

    def checkBeforeCompute(self):
        """``ctx.beforeCompute``: a change from the locked plan needs its reason first."""
        self.refresh()
        pending = [change for change in self.pending if not change.get("scope")]  # a Cohort analysis's change holds only its Run
        if not pending:
            return None
        return False, _("Changed after the plan was locked: {changes}. Give the reason in the Recipe header.").format(changes="; ".join(change["description"] for change in pending)), (_("Give reason"), self.focusReason)

    def focusReason(self):
        self.ctx.focusCard("recipe")
        self.reasonEdit.setFocus()

    def recordPending(self, reason=None):
        """Record every pending change with ``reason`` (default: the reason field); return the records."""
        recipes = _recipes()
        reason = (reason if isinstance(reason, str) else self.reasonEdit.text).strip()
        path = self.studyPath()
        if not reason or not path or not self.pending:
            return []
        records = []
        for change in list(self.pending):
            try:
                records.append(recipes.record_deviation(path, change["field"], change["old"], change["new"], reason, description=change["description"]))
            except (recipes.RecipeError, OSError) as exc:
                logging.error(f"Lesion network mapping: the deviation could not be recorded: {exc}")
        self.reasonEdit.clear()
        self.refresh()
        for tab in self.analysisTabs():
            tab.showState()  # Run of a Cohort analysis waited for this reason
        return records

    #
    # Plan lock
    #

    def onLockClicked(self):
        recipes = _recipes()
        path = self.studyPath()
        if not path or self.recipe is None:
            return None
        params = self.currentParameters()
        scores = []
        try:
            scores = [name for name, _confirmed in self.study().score_columns()]
        except Exception:
            scores = []
        dialog = LockPlanDialog(slicer.util.mainWindow(), recipes.custom_label(self.recipe, params), params, scores, params["rule"])
        if not dialog.exec():
            return None
        try:
            return self.lockPlan(dialog.hypotheses())
        except recipes.RecipeError as exc:
            slicer.util.errorDisplay(str(exc), windowTitle=_("Lock the analysis plan"))
            return None

    def lockPlan(self, hypotheses, now=None):
        """Lock the plan of the open study with the current parameters; return the plan dict (raises RecipeError)."""
        recipes = _recipes()
        path = self.studyPath()
        if not path or self.recipe is None:
            raise recipes.RecipeError("Open a study and choose a recipe first")
        name = None
        try:
            name = str(qt.QSettings().value("Studio/RaterName") or "") or None
        except Exception:
            name = None
        plan = recipes.lock_plan(path, self.recipe, self.currentParameters(), hypotheses, now=now, locked_by=name)
        self.restorePlanSettings()  # Run everything follows the locked numbers and steps from now on
        return plan

    def restorePlanSettings(self):
        """Set the threshold rule, the Run everything options and the Cohort analyses' settings back to the locked plan
        (with its recorded deviations).
        """
        recipes = _recipes()
        path = self.studyPath()
        try:
            params = recipes.effective_parameters(path) if path else None
            steps = recipes.effective_steps(path) if params is not None else None
        except (recipes.RecipeError, ValueError, TypeError, OSError):  # a hand-edited plan (the chip says "modified")
            logging.exception("Lesion network mapping: the locked plan's settings could not be read")
            return False
        if params is None:
            return False
        self._setMapSettings(params, steps, "locked plan")
        for tab in self.analysisTabs():
            try:
                values = recipes.analysis_parameters(path, tab.PLAN_SCOPE)
                if values is not None:
                    tab.applyPlanValues(values)
                    tab.refresh()
            except Exception:
                logging.exception("Lesion network mapping: a Cohort analysis could not follow the locked plan")
        self.refresh()
        return True

    #
    # Popovers and lab recipes
    #

    def deviationsHtml(self):
        recipes = _recipes()
        path = self.studyPath()
        if not path:
            return ""
        status = recipes.plan_status(path)
        rows = [f"<b>{html.escape(_('Deviations from the locked plan'))}</b>", f"<div>{html.escape(status['chip'])}</div>"]
        items = recipes.read_deviations(path)
        if not items:
            rows.append(f"<div>{html.escape(_('None recorded.'))}</div>")
        for item in items:
            when = str(item.get("time") or "")[:16].replace("T", " ")
            rows.append(f"<div>· {html.escape(str(item.get('description') or item.get('field')))} <i>({html.escape(_('reason'))}: {html.escape(str(item.get('reason') or ''))}; {html.escape(when)})</i></div>")
        rows.append(f"<div style='margin-top:6px'>{html.escape(_('The methods text lists these under “Deviations from the analysis plan”.'))}</div>")
        return "".join(rows)

    def showDeviations(self):
        self.deviationsPopover.show(self.deviationsButton, self.deviationsHtml())

    def detailsHtml(self, recipe=None):
        recipes = _recipes()
        recipe = recipe or self.recipe
        if recipe is None:
            return ""
        rows = [f"<b>{html.escape(recipe.title)}</b>", f"<div>{html.escape(recipe.status_text())}</div>"]
        if recipe.get("description"):
            rows.append(f"<div>{html.escape(recipe['description'])}</div>")
        for doi in recipe.get("citations") or []:
            rows.append(f"<div><a href='https://doi.org/{html.escape(doi)}'>doi:{html.escape(doi)}</a></div>")
        differences = recipe.get("differences_from_source") or []
        if differences:
            rows.append(f"<div style='margin-top:6px'><b>{html.escape(_('Differences from the source'))}</b></div>")
            rows.extend(f"<div>· {html.escape(text)}</div>" for text in differences)
        rows.append(f"<div style='margin-top:6px'><b>{html.escape(_('Steps'))}</b></div>")
        for step in recipes.available_steps(recipe):
            mark = "" if step["available"] else f" — <i>{html.escape(_(recipes.NOT_AVAILABLE))}</i>"
            where = recipes.MANUAL_STEP_PLACES.get(step["id"])
            extra = f" ({html.escape(where)})" if where and step["available"] else ""
            rows.append(f"<div>· {html.escape(step['label'])}{extra}{mark}</div>")
        return "".join(rows)

    def showDetails(self):
        if self.recipe is not None:
            self.detailsPopover.show(self.combo, self.detailsHtml())

    def labRecipeFolder(self):
        """Where Save as lab recipe… writes: the study's recipes/ folder, else the first recipe folder (None: ask)."""
        path = self.studyPath()
        if path:
            return os.path.join(path, _recipes().STUDY_RECIPES_DIR)
        folders = recipeFolders()
        return folders[0] if folders else None

    def saveLabRecipe(self, title=None, folder=None):
        """Save the current parameters as a lab recipe (asks for the title, and the folder when there is no study)."""
        recipes = _recipes()
        if self.recipe is None:
            return None
        params = self.currentParameters()
        if title is None:
            title = qt.QInputDialog.getText(slicer.util.mainWindow(), _("Save as lab recipe"), _("Name of the lab recipe:"), qt.QLineEdit.Normal, recipes.custom_label(self.recipe, params))
            if not title:
                return None
        folder = folder or self.labRecipeFolder()
        if folder is None:
            folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Folder for lab recipes"))
            if not folder:
                return None
            addRecipeFolder(folder)
        base = recipes._slug(title)
        target = os.path.join(folder, f"{base}.json")
        counter = 2
        while os.path.exists(target):
            target = os.path.join(folder, f"{base}_{counter}.json")
            counter += 1
        recipeId = os.path.splitext(os.path.basename(target))[0]
        if self.recipeById(recipeId) is not None:
            recipeId = f"{recipeId}_lab"
        try:
            saved = recipes.save_lab_recipe(target, params, self.recipe, title=title, recipe_id=recipeId, steps=self.currentSteps())
        except (recipes.RecipeError, OSError) as exc:
            slicer.util.errorDisplay(str(exc), windowTitle=_("Save as lab recipe"))
            return None
        self.reload()
        if self.plan() is None:
            recipe = self.recipeById(saved.id)
            if recipe is not None:
                self.recipe = recipe
                path = self.studyPath()
                if path:
                    recipes.select_recipe(path, recipe)
                self._fillCombo()
                self.refresh()
        return saved

    def chooseRecipeFolder(self):
        folder = qt.QFileDialog.getExistingDirectory(slicer.util.mainWindow(), _("Add a folder of lab recipes"))
        if folder:
            addRecipeFolder(folder)
            self.reload()

    def cleanup(self):
        if self._cleanedUp:
            return
        self._cleanedUp = True
        self.pollTimer.stop()
        self.widget.removeEventFilter(self._showFilter)
        for name in ("studyChanged", "ruleChanged"):
            callbacks = getattr(self.ctx, name, [])
            while self.refresh in callbacks:
                callbacks.remove(self.refresh)
        while self.checkBeforeCompute in self.ctx.beforeCompute:
            self.ctx.beforeCompute.remove(self.checkBeforeCompute)
        if self._blocked:
            self.ctx.setComputeEnabled(True, key=BLOCK_KEY)
            self._blocked = False


#
# Search entries
#


def showInMap(then=None):
    """Show Map (the Studio workspace, else the module) scrolled to the Recipe header; then ``then(header)``."""
    from . import jobrunner

    shell = jobrunner._studioShell()
    if shell is not None:
        shell.switchTo("map")
    else:
        slicer.util.selectModule("LesionNetworkMapping")
    module = getattr(slicer.modules, "lesionnetworkmapping", None)
    widget = module.widgetRepresentation().self() if module is not None else _mapWidget()
    ctx = getattr(widget, "ctx", None) if widget is not None else None
    if ctx is None:
        return None
    ctx.focusCard("recipe")
    header = ctx.card("recipe")
    if header is not None and then is not None:
        return then(header)
    return header


def _headerOrNone():
    widget = _mapWidget()
    ctx = getattr(widget, "ctx", None) if widget is not None else None
    return ctx.card("recipe") if ctx is not None else None


def paletteEntries(shell=None):
    """Search entries of the recipes and the analysis plan (dict form of the provider contract, USABILITY 4)."""

    def canLock():
        header = _headerOrNone()
        return header is None or (header.recipe is not None and bool(header.studyPath()) and header.plan() is None)

    def locked():
        header = _headerOrNone()
        return header is None or header.plan() is not None

    def chooseRecipe(header):
        header.combo.setFocus()
        header.combo.showPopup()

    return [
        {"id": "lnm.recipe", "title": _("Choose an analysis recipe (lesion network mapping)…"), "category": _("Map"),
         "synonyms": ["recipe", "pipeline preset", "cookbook", "ferguson", "boes", "sLNM"], "callback": lambda: showInMap(chooseRecipe),
         "description": _("Named, cited analysis pipelines with their differences from the source")},
        {"id": "lnm.lock-plan", "title": _("Lock analysis plan…"), "category": _("Map"), "synonyms": ["pre-registration", "preregister", "analysis plan", "hypotheses", "roi"],
         "callback": lambda: showInMap(lambda header: header.onLockClicked()), "enabled": canLock,
         "description": _("State the hypotheses and settings before the analysis; later changes are recorded with a reason")},
        {"id": "lnm.plan-deviations", "title": _("Show deviations from the analysis plan"), "category": _("Map"), "synonyms": ["deviations", "plan changes"],
         "callback": lambda: showInMap(lambda header: header.showDeviations()), "enabled": locked,
         "description": _("The changes made after the plan was locked, with their reasons")},
    ]


def registerPaletteProvider():
    """Register the Search entries with the Studio palette (guarded: nothing happens without Studio)."""
    try:
        from StudioLib import palette
    except Exception:
        return False
    try:
        for existing in palette.providers():
            if getattr(existing, "__name__", None) == "paletteEntries" and getattr(existing, "__module__", None) == __name__ and existing is not paletteEntries:
                palette.unregisterProvider(existing)
        palette.registerProvider(paletteEntries)
    except Exception:
        logging.exception("Lesion network mapping: the recipe Search entries could not be registered")
        return False
    return True


registerPaletteProvider()


HEADER = {
    "id": "recipe",
    "title": "Recipe",
    "order": ORDER_RECIPE,
    "classic": True,
    "factory": RecipeHeader,
}
