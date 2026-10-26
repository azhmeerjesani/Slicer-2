"""Plug-in framework of the Map user interface (module ``LesionNetworkMapping``).

Later features ship as their own files in this package instead of editing ``LesionNetworkMapping.py``:

- ``card_*.py`` exposes ``CARD`` (a collapsible inspector card) and/or ``HEADER`` (a header strip that
  cannot be collapsed, ``order`` below 10)::

      CARD = {
          "id": "qa",  # unique, used by MapContext.focusCard()
          "title": "QA",
          "order": ORDER_QA,  # position in the inspector (constants below, SPEC_LNM_ADDENDUM 7.1)
          "modes": ("lesion", "cohort"),  # seed modes that show the card (default: all)
          "classic": True,  # also shown in the Classic interface (default: True)
          "factory": lambda ctx: Controller(ctx),
      }

  The controller has ``.widget`` and optionally ``onRunEvent(event)``, ``onResultChanged()``,
  ``onStudyChanged()`` and ``cleanup()``.
- ``cohort_*.py`` exposes ``TAB = {id, title, order, factory(ctx)}`` (tabs of the Cohort card).
- ``manager_*.py`` exposes ``ACTIONS = [{id, label, order, factory(ctx, bundlePath)}, ...]``
  (actions of the Connectome card's Manage… menu).

Helpers: :mod:`.context` (``MapContext``), :mod:`.jobrunner` (``JobRunner``, ``JobSpec``) and
:mod:`.widgets` (card, chip, banner, progress row...). Importing this package imports only the
standard library; plug-in modules are imported by the discovery functions. A plug-in that cannot be
imported, or whose specification is invalid, is logged and skipped: it never breaks the module.
Discovery works in Classic and without a main window.
"""

import importlib
import logging
import numbers
import pkgutil

#: Seed modes of the Map module (``MapContext`` and ``CARD["modes"]``)
SEED_MODES = ("lesion", "cohort", "coordinates", "stimulation")

#: Header strips (``HEADER``) have an order below this value
HEADER_MAX_ORDER = 10

# Card order in the inspector (SPEC_LNM_ADDENDUM 7.1, SPEC 7.9). "main" rows are built by LesionNetworkMapping.py.
ORDER_BATTERY = 1  # card_report.py HEADER
ORDER_RECIPE = 5  # card_recipe.py HEADER
ORDER_SEED = 10  # main
ORDER_NORMALIZE = 15  # card_normalize.py (lesion mode)
ORDER_CONNECTOME = 20  # main
ORDER_PREFLIGHT = 30
ORDER_RUN_EVERYTHING = 35  # card_pipeline.py (cohort mode)
ORDER_COMPUTE = 40  # main
ORDER_RESULT = 50  # main
ORDER_QA = 60
ORDER_COHORT = 70
ORDER_NULLS = 80
ORDER_VALIDATE = 90
ORDER_ROBUST = 100
ORDER_CLUSTERS = 110
ORDER_NETWORKS = 115
ORDER_CIRCUITS = 120
ORDER_COMPARE = 130
ORDER_CALIBRATE = 135
ORDER_REPORT = 140
ORDER_EXPORT = 150

#: Order of every known card id (the Disclaimer footer is not a card)
CARD_ORDER = {
    "battery": ORDER_BATTERY,
    "recipe": ORDER_RECIPE,
    "seed": ORDER_SEED,
    "normalize": ORDER_NORMALIZE,
    "connectome": ORDER_CONNECTOME,
    "preflight": ORDER_PREFLIGHT,
    "pipeline": ORDER_RUN_EVERYTHING,
    "compute": ORDER_COMPUTE,
    "result": ORDER_RESULT,
    "qa": ORDER_QA,
    "cohort": ORDER_COHORT,
    "nulls": ORDER_NULLS,
    "validate": ORDER_VALIDATE,
    "robust": ORDER_ROBUST,
    "clusters": ORDER_CLUSTERS,
    "networks": ORDER_NETWORKS,
    "circuits": ORDER_CIRCUITS,
    "compare": ORDER_COMPARE,
    "calibrate": ORDER_CALIBRATE,
    "report": ORDER_REPORT,
    "export": ORDER_EXPORT,
}


def _pluginModules(prefix):
    """Import the modules of this package whose name starts with ``prefix``; yield (name, module).

    A module that fails to import is logged with its traceback and skipped.
    """
    names = sorted({info.name for info in pkgutil.iter_modules(__path__) if info.name.startswith(prefix)})
    for name in names:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except Exception:
            logging.exception(f"Lesion network mapping: the plug-in {name} could not be imported and is skipped")
            continue
        yield name, module


def _specs(value):
    """A spec attribute as a list: a dict gives [dict], a list or tuple gives its items."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _validated(spec, moduleName, attribute, required, factoryName="factory"):
    """A copy of ``spec`` with the defaults filled in, or None (logged) when it is not valid."""
    if not isinstance(spec, dict):
        logging.error(f"Lesion network mapping: {moduleName}.{attribute} is not a dict; the plug-in is skipped")
        return None
    missing = [key for key in required if key not in spec]
    if missing:
        logging.error(f"Lesion network mapping: {moduleName}.{attribute} lacks {', '.join(missing)}; the plug-in is skipped")
        return None
    if not callable(spec[factoryName]):
        logging.error(f"Lesion network mapping: {moduleName}.{attribute}['{factoryName}'] is not callable; the plug-in is skipped")
        return None
    order = spec.get("order", CARD_ORDER.get(str(spec["id"])))
    if not isinstance(order, numbers.Real) or isinstance(order, bool):
        logging.error(f"Lesion network mapping: {moduleName}.{attribute} has no numeric 'order'; the plug-in is skipped")
        return None
    result = dict(spec)
    result["id"] = str(spec["id"])
    result["order"] = order
    result["module"] = moduleName
    return result


def discoverCards():
    """Return the card and header specifications of the ``card_*.py`` modules, sorted by (order, id).

    Each specification is a copy of the module's ``CARD`` or ``HEADER`` dict with these keys filled in:
    ``kind`` ("card" or "header"), ``title`` (default: the id), ``modes`` (a tuple, default
    ``SEED_MODES``), ``classic`` (default True) and ``module`` (the plug-in module name). A module may
    expose both (for example the Report card and the battery header strip). Two specifications with the
    same id keep the first one in module-name order; the other is logged and skipped.
    """
    specs = []
    seen = {}
    for name, module in _pluginModules("card_"):
        for attribute, kind in (("HEADER", "header"), ("CARD", "card")):
            for raw in _specs(getattr(module, attribute, None)):
                spec = _validated(raw, name, attribute, ("id", "factory"))
                if spec is None:
                    continue
                if spec["id"] in seen:
                    logging.error(f"Lesion network mapping: the card id '{spec['id']}' of {name} is already used by {seen[spec['id']]}; it is skipped")
                    continue
                modes = spec.get("modes") or SEED_MODES
                modes = (modes,) if isinstance(modes, str) else tuple(modes)
                unknown = [mode for mode in modes if mode not in SEED_MODES]
                if unknown:
                    logging.warning(f"Lesion network mapping: {name}.{attribute} names unknown seed modes {unknown}")
                spec["kind"] = kind
                spec["modes"] = modes
                spec.setdefault("title", spec["id"])
                spec["classic"] = bool(spec.get("classic", True))
                if kind == "header" and spec["order"] >= HEADER_MAX_ORDER:
                    logging.warning(f"Lesion network mapping: the header '{spec['id']}' of {name} has order {spec['order']} (headers use orders below {HEADER_MAX_ORDER})")
                seen[spec["id"]] = name
                specs.append(spec)
    specs.sort(key=lambda spec: (spec["order"], spec["id"]))
    return specs


def discoverCohortTabs():
    """Return the ``TAB`` specifications of the ``cohort_*.py`` modules (tabs of the Cohort card), sorted by (order, id)."""
    tabs = []
    for name, module in _pluginModules("cohort_"):
        for raw in _specs(getattr(module, "TAB", None)):
            spec = _validated(raw, name, "TAB", ("id", "order", "factory"))
            if spec is not None:
                spec.setdefault("title", spec["id"])
                tabs.append(spec)
    tabs.sort(key=lambda spec: (spec["order"], spec["id"]))
    return tabs


def discoverManagerActions():
    """Return the ``ACTIONS`` of the ``manager_*.py`` modules (the connectome Manage… menu), sorted by (order, id).

    Each action is ``{id, label, order, factory(ctx, bundlePath)}``; the factory runs the action.
    """
    actions = []
    for name, module in _pluginModules("manager_"):
        for raw in _specs(getattr(module, "ACTIONS", None)):
            spec = _validated(raw, name, "ACTIONS", ("id", "label", "order", "factory"))
            if spec is not None:
                actions.append(spec)
    actions.sort(key=lambda spec: (spec["order"], spec["id"]))
    return actions


def visibleSpecs(specs, mode=None, classic=False):
    """The specifications of ``specs`` that are shown for seed ``mode`` (None: any) in Studio, or in Classic when ``classic``."""
    return [spec for spec in specs if (mode is None or mode in spec.get("modes", SEED_MODES)) and (not classic or spec.get("classic", True))]
