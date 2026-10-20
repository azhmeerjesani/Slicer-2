"""Display of lesion network maps in Slicer: the "LNM Diverging" colour table, thresholds and layers (SPEC 7.10).

A map is shown with a 256-entry diverging colour table node ("LNM Diverging", ``colormaps.lnm_diverging``)
over a symmetric window [-W, +W], W = the 99.5th percentile of |value|. A threshold only rewrites the
alpha of the 256 entries (0 for entries that do not pass), so moving a threshold never touches the
image data of the map. The colour table is a standard ``vtkMRMLColorTableNode`` that is saved with the
scene (a ``.ctbl`` file with alpha), so a saved map renders the same in official 3D Slicer.

Needs the Slicer application (MRML); the module imports it lazily. Every function must be called on
the main thread. ``applyTwoSidedThreshold(volumeNode, t)`` is the one implementation of the |value| >= t
display shared by the Map module and the statistical-map overlay of the Library importer.
"""

import math

import numpy as np
import slicer

from . import colormaps

#: Name of the colour table node of the module
LUT_NAME = "LNM Diverging"
#: Node attribute that marks the colour table node of the module
LUT_ATTRIBUTE = "LNM.ColorTable"
#: Number of entries of the colour table
LUT_ENTRIES = colormaps.DEFAULT_ENTRIES
#: Node attribute of result volumes, so that Draw can hide them (W2-05 (d))
RESULT_ATTRIBUTE = "LNM.Result"
#: Node attributes with the display state of a map (numbers only: no path, no counter; SPEC 9.7 rule 10)
WINDOW_ATTRIBUTE = "LNM.Window"
THRESHOLD_ATTRIBUTE = "LNM.Threshold"
SIGN_ATTRIBUTE = "LNM.Sign"
#: Percentile of |value| that sets the symmetric window (SPEC 4.12)
WINDOW_PERCENTILE = 99.5
#: Lesion outline colour on a map (SPEC 7.10)
OUTLINE_COLOR = (1.0, 1.0, 1.0)


def _scene(scene=None):
    return scene if scene is not None else slicer.mrmlScene


def findLUT(scene=None):
    """The module's colour table node in the scene, or None."""
    scene = _scene(scene)
    marked = None
    for node in _nodesByClass(scene, "vtkMRMLColorTableNode"):
        if node.GetName() == LUT_NAME:
            if node.GetAttribute(LUT_ATTRIBUTE) == "1":
                return node
            marked = marked or node  # a table of that name loaded from an older scene
    return marked


def _nodesByClass(scene, className):
    nodes = []
    collection = scene.GetNodesByClass(className)
    for index in range(collection.GetNumberOfItems()):
        nodes.append(collection.GetItemAsObject(index))
    return nodes


def ensureLUT(scene=None):
    """Create, or update the colours of, the "LNM Diverging" colour table node and return it.

    The colours are rewritten (alphas are kept), so a table loaded from a scene stays usable. The node
    is saved with the scene (SPEC 9.7 rule 6).
    """
    scene = _scene(scene)
    node = findLUT(scene)
    colors = _colors()
    if node is not None and node.GetAttribute(LUT_ATTRIBUTE) == "1" and node.GetNumberOfColors() == LUT_ENTRIES and _hasColors(node, colors):
        return node
    created = node is None
    if created:
        node = slicer.vtkMRMLColorTableNode()
        node.SetName(LUT_NAME)
        node.SetTypeToUser()
        node.SetNumberOfColors(LUT_ENTRIES)
        node.SetSaveWithScene(True)
    wasModifying = node.StartModify()
    if node.GetNumberOfColors() != LUT_ENTRIES:
        node.SetNumberOfColors(LUT_ENTRIES)
    node.SetAttribute(LUT_ATTRIBUTE, "1")
    node.SetAttribute("Category", "Lesion network mapping")
    lookupTable = node.GetLookupTable()
    for index, (r, g, b) in enumerate(colors):
        alpha = 1.0 if created else lookupTable.GetTableValue(index)[3]
        node.SetColor(index, f"lnm{index:03d}", r, g, b, alpha)
    node.EndModify(wasModifying)
    if created:
        scene.AddNode(node)
    return node


_COLORS = []


def _colors():
    if not _COLORS:
        _COLORS.extend(colormaps.lnm_diverging(LUT_ENTRIES))
    return _COLORS


def _hasColors(node, colors, tolerance=1e-6):
    """True when the node's RGB values are ``colors`` (checked at a few entries, enough to detect another table)."""
    lookupTable = node.GetLookupTable()
    for index in (0, len(colors) // 2 - 1, len(colors) // 2, len(colors) - 1):
        rgba = lookupTable.GetTableValue(index)
        if any(abs(rgba[component] - colors[index][component]) > tolerance for component in range(3)):
            return False
    return True


def setThreshold(lut, window, threshold, sign="both"):
    """Rewrite only the alpha values of the colour table: 0 for entries whose value does not pass.

    The table spans [-window, +window]; ``sign`` is "both" (|value| >= threshold), "pos" or "neg".
    Returns the number of visible entries.
    """
    alphas = colormaps.entry_alphas(lut.GetNumberOfColors(), window, threshold, sign)
    lookupTable = lut.GetLookupTable()
    wasModifying = lut.StartModify()
    for index, alpha in enumerate(alphas):
        r, g, b, a = lookupTable.GetTableValue(index)
        if a != alpha:
            lut.SetColor(index, r, g, b, alpha)
    lut.EndModify(wasModifying)
    return int(sum(alphas))


def symmetricWindow(values, percentile=WINDOW_PERCENTILE):
    """W of the symmetric window: the ``percentile`` of |value| over the finite non-zero values (1.0 when there are none).

    ``values`` is an array, or a volume node (its voxels are read, never modified).
    """
    if hasattr(values, "GetImageData"):
        values = slicer.util.arrayFromVolume(values)
    values = np.abs(np.asarray(values, dtype=np.float64).ravel())
    values = values[np.isfinite(values) & (values > 0)]
    if values.size == 0:
        return 1.0
    window = float(np.percentile(values, percentile))
    return window if window > 0 and math.isfinite(window) else float(values.max())


def countPassing(values, threshold, sign="both"):
    """Number of finite voxels that pass ``threshold`` (|value| >= t for "both"); reads, never modifies, a volume node."""
    if hasattr(values, "GetImageData"):
        values = slicer.util.arrayFromVolume(values)
    values = np.asarray(values, dtype=np.float64).ravel()
    values = values[np.isfinite(values)]
    if sign == "pos":
        passing = values >= threshold
    elif sign == "neg":
        passing = values <= -threshold
    else:
        passing = np.abs(values) >= threshold
    return int(np.count_nonzero(passing & (values != 0)))


def shownWindow(values, window, threshold, passing=None, entries=LUT_ENTRIES):
    """Half-width of the displayed colour table window so that the voxels passing ``threshold`` stay visible.

    The table spans [-W, +W]; values beyond it clamp to the edge entries, whose alpha follows their
    centre value W (1 - 1/n). When the threshold reaches the edge entries while voxels pass (a custom
    threshold above W, a small Top x%, FWE on a peaked map), W is widened to
    max(max |value|, threshold (1 + 2/n)): the passing voxels then fall in visible entries. Otherwise
    ``window`` is returned. Only the display window changes, never the image data.
    """
    window = float(window)
    threshold = float(threshold)
    if not math.isfinite(threshold) or threshold < window * (1.0 - 2.0 / entries):
        return window
    if passing is None:
        passing = countPassing(values, threshold)
    if not passing:
        return window
    if hasattr(values, "GetImageData"):
        values = slicer.util.arrayFromVolume(values)
    values = np.asarray(values)
    finite = values[np.isfinite(values)] if values.dtype.kind == "f" else values
    largest = float(np.abs(finite).max()) if finite.size else 0.0
    return max(window, largest, threshold * (1.0 + 2.0 / entries))


def applyResultDisplay(volume, lut=None, window=None, threshold=None, sign=None, recordedWindow=None):
    """Show a map with the module colour table: ColorNodeID, automatic window/level off, window 2W at level 0.

    Accepts T/Fz/r maps and group maps (float percent or T, uint8 binary): a map without negative
    values gets the positive half only. ``window`` defaults to :func:`symmetricWindow`; ``threshold``
    (optional) applies :func:`setThreshold`. ``recordedWindow`` (default ``window``) is the W recorded
    in ``LNM.Window`` (the map's own window, when the display is widened by :func:`shownWindow`).
    Returns the window W.
    """
    lut = lut if lut is not None else ensureLUT(volume.GetScene())
    if volume.GetDisplayNode() is None:
        volume.CreateDefaultDisplayNodes()
    displayNode = volume.GetDisplayNode()
    array = slicer.util.arrayFromVolume(volume)
    if window is None:
        window = symmetricWindow(array)
    if sign is None:
        finite = array[np.isfinite(array)] if array.dtype.kind == "f" else array
        sign = "pos" if finite.size and float(finite.min()) >= 0 else "both"
    wasModifying = displayNode.StartModify()
    displayNode.SetAndObserveColorNodeID(lut.GetID())
    displayNode.AutoWindowLevelOff()
    displayNode.SetWindowLevel(2.0 * window, 0.0)
    displayNode.SetApplyThreshold(False)
    displayNode.EndModify(wasModifying)
    volume.SetAttribute(WINDOW_ATTRIBUTE, f"{(window if recordedWindow is None else recordedWindow):.6g}")
    volume.SetAttribute(SIGN_ATTRIBUTE, sign)
    if threshold is not None:
        setThreshold(lut, window, threshold, sign)
        volume.SetAttribute(THRESHOLD_ATTRIBUTE, f"{threshold:.6g}")
    return window


def applyTwoSidedThreshold(volumeNode, threshold, window=None, sign="both"):
    """Show ``volumeNode`` with |value| >= ``threshold`` (both tails), through the colour table alphas only.

    The volume gets the module colour table and a symmetric window: ``window``, else its recorded
    ``LNM.Window``, else computed once; the displayed window is widened when the threshold reaches its
    edge (:func:`shownWindow`), so that voxels that pass are always visible. Returns ``(window, passing)``:
    the half-width of the displayed window and the number of voxels that pass (0 gives the explicit
    empty-result state).
    """
    lut = ensureLUT(volumeNode.GetScene())
    displayNode = volumeNode.GetDisplayNode()
    array = slicer.util.arrayFromVolume(volumeNode)
    if window is None:
        recorded = volumeNode.GetAttribute(WINDOW_ATTRIBUTE)
        try:
            window = float(recorded) if recorded else None
        except ValueError:
            window = None
    if window is None:
        window = symmetricWindow(array)
    passing = countPassing(array, threshold, sign)
    shown = shownWindow(array, window, threshold, passing)
    usesLut = displayNode is not None and displayNode.GetColorNodeID() == lut.GetID()
    sameWindow = usesLut and not displayNode.GetAutoWindowLevel() and abs(displayNode.GetWindow() - 2.0 * shown) <= 1e-6 * max(1.0, shown) and abs(displayNode.GetLevel()) <= 1e-9
    if not sameWindow:
        applyResultDisplay(volumeNode, lut, window=shown, sign=sign, recordedWindow=window)
    else:
        if volumeNode.GetAttribute(SIGN_ATTRIBUTE) != sign:
            volumeNode.SetAttribute(SIGN_ATTRIBUTE, sign)
        if volumeNode.GetAttribute(WINDOW_ATTRIBUTE) != f"{window:.6g}":
            volumeNode.SetAttribute(WINDOW_ATTRIBUTE, f"{window:.6g}")
    setThreshold(lut, shown, threshold, sign)
    volumeNode.SetAttribute(THRESHOLD_ATTRIBUTE, f"{threshold:.6g}")
    return shown, passing


def showLayers(template, result, opacity=0.8):
    """Background = template, foreground = result at ``opacity``, no label layer (any of them may be None)."""
    slicer.util.setSliceViewerLayers(
        background=template if template is not None else "keep-current",
        foreground=result,
        foregroundOpacity=float(opacity),
        label=None,
    )


def setForegroundOpacity(opacity):
    """Opacity of the foreground layer (the map) in every slice view."""
    slicer.util.setSliceViewerLayers(foregroundOpacity=float(opacity))


def lesionOutline(segNode, segmentId=None):
    """Show the lesion as a white outline (``Visibility2DFill`` off) over a map; return the previous state.

    Pass the returned state to :func:`restoreLesionDisplay` to give the lesion its own look back.
    """
    if segNode is None:
        return None
    if segNode.GetDisplayNode() is None:
        segNode.CreateDefaultDisplayNodes()
    displayNode = segNode.GetDisplayNode()
    segmentIds = [segmentId] if segmentId else list(segNode.GetSegmentation().GetSegmentIDs())
    state = {"node": segNode, "fill": displayNode.GetVisibility2DFill(), "outline": displayNode.GetVisibility2DOutline(), "override": {}}
    wasModifying = displayNode.StartModify()
    displayNode.SetVisibility2DFill(False)
    displayNode.SetVisibility2DOutline(True)
    for identifier in segmentIds:
        color = displayNode.GetSegmentOverrideColor(identifier)
        state["override"][identifier] = (color.GetX(), color.GetY(), color.GetZ())
        displayNode.SetSegmentOverrideColor(identifier, *OUTLINE_COLOR)
    displayNode.EndModify(wasModifying)
    return state


def restoreLesionDisplay(state):
    """Undo :func:`lesionOutline` (fill, outline and per-segment colours)."""
    if not state or state.get("node") is None:
        return
    segNode = state["node"]
    displayNode = segNode.GetDisplayNode()
    if displayNode is None or segNode.GetScene() is None:
        return
    wasModifying = displayNode.StartModify()
    displayNode.SetVisibility2DFill(state["fill"])
    displayNode.SetVisibility2DOutline(state["outline"])
    segmentation = segNode.GetSegmentation()
    for identifier, color in state["override"].items():
        if segmentation.GetSegment(identifier) is None:
            continue
        if any(component < 0 for component in color):  # SEGMENT_COLOR_NO_OVERRIDE is -1
            displayNode.UnsetSegmentOverrideColor(identifier)
        else:
            displayNode.SetSegmentOverrideColor(identifier, *color)
    displayNode.EndModify(wasModifying)
