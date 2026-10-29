"""Where sides are known: template spaces and figure views of Slicer Studio (USABILITY 7.0).

This module is the single source of truth for two questions that several Studio pages ask:

- **Is this image on an MNI grid?** ``spaceOf(node)`` answers with ``(spaceName, resolutionMm)`` or None:
  the node attribute ``Studio.Space`` first (written by normalization), else (for a segmentation) the ``Studio.Space``
  of a volume on exactly its grid (a lesion drawn on a template is in the template's space), else an exact table of the known template
  grids (``KNOWN_GRIDS``, ±0.01 mm), else ``LesionNetworkMappingLib.geometry.detect_space`` with the same tolerance
  when that library is importable, which also knows the grids of the user's own templates (A-10: ``templates`` registers
  them; an anisotropic one is matched by its exact grid). A grid is compared through the RAS positions of its voxel
  centres, so a template stored LAS (FSL) or RAS (TemplateFlow) is recognized either way. ``hemisphereWord()``
  returns "Left", "Right" or "Both hemispheres" only on an MNI grid: in scanner space x = 0 is not the midline.
- **Is this slice view a figure?** ``isFigureView(view)`` is True when the background of the view is a figure
  (node attribute ``Studio.Role`` = "figure", set by the importer) or an image with a single slice along the view
  normal. Figure views show no patient directions (USABILITY 7.3).

Pure functions: no observer, no Qt widget, no settings. Every Studio plug-in may import this module.
"""

from StudioLib import lazyNumpy as np

import slicer
import vtk

#: Node attributes (USABILITY 17). Official Slicer ignores them.
SPACE_ATTRIBUTE = "Studio.Space"
ROLE_ATTRIBUTE = "Studio.Role"
FIGURE_CONVENTION_ATTRIBUTE = "Studio.FigureConvention"
#: Values of ``Studio.Role``
ROLE_FIGURE = "figure"
ROLE_STATMAP = "statmap"
ROLE_TEMPLATE = "template"
#: Values of ``Studio.FigureConvention``
CONVENTIONS = ("radiological", "neurological", "unknown")

#: Known template grids: (space, resolution in mm, number of voxels along R, A and S, voxel size, RAS position of
#: the voxel centre with the lowest x, y and z). The 2009c 2 mm grid is the bundled template; FSL stores its
#: MNI152NLin6Asym files LAS (the NIfTI origin 90, -126, -72 is the voxel centre with the highest x).
KNOWN_GRIDS = (
    ("MNI152NLin2009cAsym", 2, (97, 115, 97), 2.0, (-96.5, -132.5, -78.5)),
    ("MNI152NLin2009cAsym", 1, (193, 229, 193), 1.0, (-96.0, -132.0, -78.0)),
    ("MNI152NLin6Asym", 2, (91, 109, 91), 2.0, (-90.0, -126.0, -72.0)),
    ("MNI152NLin6Asym", 1, (182, 218, 182), 1.0, (-91.0, -126.0, -72.0)),
)
#: Tolerance of the built-in table (mm)
GRID_TOLERANCE_MM = 0.01


#
# Grids
#


def _matrixToArray(matrix):
    return np.array([[matrix.GetElement(row, column) for column in range(4)] for row in range(4)])


def _referenceGeometry(segmentationNode):
    """The serialized reference image geometry of a segmentation ("" if it has none)."""
    parameterName = slicer.vtkSegmentationConverter.GetReferenceImageGeometryParameterName()
    return segmentationNode.GetSegmentation().GetConversionParameter(parameterName) or ""


def _imageFromGeometry(geometry):
    image = slicer.vtkOrientedImageData()
    if not slicer.vtkSegmentationConverter.DeserializeImageGeometry(geometry, image, False):
        return None
    return image


def _orientedImageGrid(image):
    """``(dims, ijkToRas)`` of a vtkOrientedImageData; voxel (0, 0, 0) of ``dims`` is the first voxel of the extent."""
    matrix = vtk.vtkMatrix4x4()
    image.GetImageToWorldMatrix(matrix)
    extent = image.GetExtent()
    if extent[1] < extent[0] or extent[3] < extent[2] or extent[5] < extent[4]:
        return None
    shift = np.eye(4)
    shift[:3, 3] = extent[0::2]
    dims = (extent[1] - extent[0] + 1, extent[3] - extent[2] + 1, extent[5] - extent[4] + 1)
    return dims, _matrixToArray(matrix) @ shift


def gridOf(nodeOrGeometry):
    """``(dims, ijkToRas 4x4 numpy array)`` of a volume node, a segmentation (its reference image geometry, else its
    ``Studio.LesionGrid`` attribute), a vtkOrientedImageData, a serialized image geometry or a ``(dims, matrix)`` pair.
    None when there is no grid.
    """
    source = nodeOrGeometry
    if source is None:
        return None
    if isinstance(source, (tuple, list)) and len(source) == 2:
        dims, matrix = source
        if hasattr(matrix, "GetElement"):
            matrix = _matrixToArray(matrix)
        return tuple(int(n) for n in dims), np.asarray(matrix, dtype=float)
    if isinstance(source, str):
        image = _imageFromGeometry(source)
        return _orientedImageGrid(image) if image is not None else None
    if hasattr(source, "IsA") and source.IsA("vtkMRMLSegmentationNode"):
        geometry = _referenceGeometry(source) or source.GetAttribute("Studio.LesionGrid") or ""
        return gridOf(geometry) if geometry else None
    if hasattr(source, "IsA") and source.IsA("vtkMRMLVolumeNode"):
        image = source.GetImageData()
        if image is None:
            return None
        matrix = vtk.vtkMatrix4x4()
        source.GetIJKToRASMatrix(matrix)
        return tuple(image.GetDimensions()), _matrixToArray(matrix)
    if hasattr(source, "IsA") and source.IsA("vtkOrientedImageData"):
        return _orientedImageGrid(source)
    return None


def worldBox(dims, ijkToRas, tolerance=1e-3):
    """``(shape, spacing, lowest)`` of a grid along the R, A and S axes: number of voxels, voxel size and the RAS position of
    the voxel centre with the lowest coordinates. None when the voxel axes are not aligned with the RAS axes.
    """
    linear = np.asarray(ijkToRas, dtype=float)[:3, :3]
    axes = np.argmax(np.abs(linear), axis=0)
    if len(set(axes.tolist())) != 3:
        return None
    steps = linear[axes, np.arange(3)]
    offAxis = linear.copy()
    offAxis[axes, np.arange(3)] = 0.0
    if np.abs(offAxis).max() > tolerance * np.abs(steps).max():
        return None
    shape = [0, 0, 0]
    spacing = np.zeros(3)
    lowest = np.asarray(ijkToRas, dtype=float)[:3, 3].copy()
    for axis in range(3):
        worldAxis = int(axes[axis])
        shape[worldAxis] = int(dims[axis])
        spacing[worldAxis] = abs(steps[axis])
        if steps[axis] < 0:
            lowest[worldAxis] += steps[axis] * (int(dims[axis]) - 1)
    return tuple(shape), spacing, lowest


def tableSpace(dims, ijkToRas, tolerance=GRID_TOLERANCE_MM):
    """``(space, resolution)`` of a grid that is in ``KNOWN_GRIDS`` (exact match within ``tolerance`` mm), else None."""
    box = worldBox(dims, ijkToRas)
    if box is None:
        return None
    shape, spacing, lowest = box
    for space, resolution, gridShape, gridSpacing, gridLowest in KNOWN_GRIDS:
        if (shape == gridShape and np.all(np.abs(spacing - gridSpacing) <= tolerance)
                and np.all(np.abs(lowest - np.asarray(gridLowest)) <= tolerance)):
            return space, resolution
    return None


def _detectSpace(source):
    """``LesionNetworkMappingLib.geometry.detect_space(source)`` when that library is importable (``False`` otherwise).

    Its origin tolerance is ``GRID_TOLERANCE_MM``, as for the built-in table: a scan grid 1 mm away from a template grid
    is not a template grid.
    """
    try:
        from LesionNetworkMappingLib import geometry
    except ImportError:
        return False
    templates = _templatesModule()
    if templates is not None:
        templates.ensureGridsRegistered()
    detect = getattr(geometry, "detect_space", None)
    if detect is None:
        return False
    try:
        result = detect(source, origin_tolerance=GRID_TOLERANCE_MM)
    except Exception:
        return False
    if result is None:
        return None
    if isinstance(result, (tuple, list)) and len(result) == 2 and result[0] and result[0] != "native":
        return str(result[0]), result[1]
    return None


def _templatesModule():
    """``StudioLib.templates`` (the user's own templates), or None."""
    try:
        from . import templates

        return templates
    except Exception:
        return None


def _sameWorldGrid(firstGrid, secondGrid, tolerance=GRID_TOLERANCE_MM):
    """True when two ``(dims, ijkToRas)`` grids put their voxel centres at the same RAS positions (any storage order)."""
    first, second = worldBox(*firstGrid), worldBox(*secondGrid)
    if first is None or second is None:
        return (tuple(firstGrid[0]) == tuple(secondGrid[0])
                and bool(np.all(np.abs(np.asarray(firstGrid[1], dtype=float) - np.asarray(secondGrid[1], dtype=float)) <= tolerance)))
    return (first[0] == second[0] and bool(np.all(np.abs(first[1] - second[1]) <= tolerance))
            and bool(np.all(np.abs(first[2] - second[2]) <= tolerance)))


def _sharedGridSpace(segmentationNode, grid):
    """The space named by the ``Studio.Space`` attribute of a volume that is on exactly the lesion's grid (its reference
    volume first, then any volume of the scene), else None.

    A lesion drawn on one of your templates keeps that template's space where the template's grid is not registered: a
    project opened on another computer (its template volume in Images/ still says ``Studio.Space``), or a template removed
    in Settings while it is open (A-10).
    """
    scene = segmentationNode.GetScene() if hasattr(segmentationNode, "GetScene") else None
    if scene is None:
        return None
    role = slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole()
    candidates = [segmentationNode.GetNodeReference(role)]
    volumes = scene.GetNodesByClass("vtkMRMLVolumeNode")
    volumes.UnRegister(None)  # a new collection: release it, or it keeps its nodes alive after removal
    candidates += [volumes.GetItemAsObject(index) for index in range(volumes.GetNumberOfItems())]
    seen = set()
    for volume in candidates:
        if volume is None or not volume.IsA("vtkMRMLVolumeNode") or volume.GetID() in seen:
            continue
        seen.add(volume.GetID())
        if not volume.GetAttribute(SPACE_ATTRIBUTE) or volume.GetParentTransformNode() is not None:
            continue
        volumeGrid = gridOf(volume)
        if volumeGrid is None or not _sameWorldGrid(grid, volumeGrid):
            continue
        found = spaceOf(volume)
        if found is not None:
            return found
    return None


def sharedGridSpace(segmentationNode):
    """The space of a volume tagged ``Studio.Space`` that is on exactly the grid of ``segmentationNode`` (its reference
    volume first), else None (also for a segmentation under a parent transform). A lesion drawn on one of your templates
    is in the space you chose for the template, also when the template's grid is a built-in grid (Map's seed space).
    """
    if segmentationNode is None or not segmentationNode.IsA("vtkMRMLSegmentationNode") or segmentationNode.GetParentTransformNode() is not None:
        return None
    grid = gridOf(segmentationNode)
    return _sharedGridSpace(segmentationNode, grid) if grid is not None else None


def _userTemplateGridSpace(dims, ijkToRas):
    templates = _templatesModule()
    if templates is None:
        return None
    try:
        return templates.gridSpace(dims, ijkToRas)
    except Exception:
        return None


#
# Spaces
#


#: Space of the volumes tagged ``Studio.Role`` = "template" (the templates of "Start on MNI template")
TEMPLATE_SPACE = "MNI152NLin2009cAsym"


def spaceOf(nodeOrGeometry):
    """``(spaceName, resolutionMm)`` of an image on an MNI grid, None otherwise (scanner space, figures, unknown grids).

    The node attribute ``Studio.Space`` ("MNI152NLin6Asym", optionally with the resolution: "MNI152NLin6Asym 2") wins;
    else, for a segmentation, the ``Studio.Space`` of a volume on exactly its grid (``_sharedGridSpace``: a lesion drawn
    on one of your templates is in the space you chose for it, even on a built-in grid); else the built-in
    ``KNOWN_GRIDS``; else ``LesionNetworkMappingLib.geometry.detect_space`` when it is importable (more spaces and
    resolutions, with the same ±0.01 mm origin tolerance); else your templates' unregistered grids.
    A node under a parent transform is only recognized by its attribute (its grid is not where the file put it).
    """
    node = nodeOrGeometry if hasattr(nodeOrGeometry, "GetAttribute") else None
    if node is not None:
        attribute = node.GetAttribute(SPACE_ATTRIBUTE)
        if attribute:
            name, _separator, resolution = attribute.partition(" ")
            try:
                resolutionValue = float(resolution.replace("mm", "").strip()) if resolution else None
            except ValueError:
                resolutionValue = None
            if resolutionValue is not None and resolutionValue.is_integer():
                resolutionValue = int(resolutionValue)
            if name.lower() in ("native", "scanner", "orig", "individual"):
                return None
            return name, resolutionValue
        if node.GetAttribute(ROLE_ATTRIBUTE) == ROLE_FIGURE:
            return None
        if hasattr(node, "GetParentTransformNode") and node.GetParentTransformNode() is not None:
            return None
    grid = gridOf(nodeOrGeometry)
    if grid is None:
        return None
    dims, ijkToRas = grid
    detected = None
    if node is not None and node.IsA("vtkMRMLSegmentationNode"):
        # A lesion on the grid of a volume whose space is known is in that volume's space: a lesion drawn on one of your
        # templates whose grid is also a built-in grid (the 91x109x91 FSL grid added as MNI152Lin) is in the space you
        # chose for the template, not the grid's built-in space; and a template not registered on this computer
        detected = _sharedGridSpace(node, grid)
    if detected is None:
        detected = tableSpace(dims, ijkToRas)
    if detected is None:
        detected = _detectSpace((dims, ijkToRas)) or None
    if detected is None:
        detected = _userTemplateGridSpace(dims, ijkToRas)
    if detected is None and node is not None and node.GetAttribute(ROLE_ATTRIBUTE) == ROLE_TEMPLATE:
        # A template volume is in its template's space, whatever grid it was resampled on
        spacing = worldBox(dims, ijkToRas)
        resolution = float(spacing[1].min()) if spacing is not None else None
        return TEMPLATE_SPACE, int(resolution) if resolution is not None and resolution.is_integer() else resolution
    return detected


def isMni(node):
    """True when ``spaceOf(node)`` names a space: left and right are known there (x < 0 is the left hemisphere)."""
    return spaceOf(node) is not None


def spaceLabel(space):
    """Display text of a ``spaceOf()`` result: "MNI152NLin6Asym 2 mm" ("" for None); the space "MNI" of the user's own
    templates is "MNI (other)".
    """
    if not space:
        return ""
    name, resolution = space
    if name == "MNI":
        name = "MNI (other)"
    if resolution is None:
        return str(name)
    try:
        return f"{name} {float(resolution):g} mm"
    except (TypeError, ValueError):
        return f"{name} {resolution} mm"


def hemisphereWord(rasX, node, partsSpanBoth=False):
    """"Left", "Right" or "Both hemispheres" for a position ``rasX`` (mm) of ``node``, None when ``node`` is not on an MNI
    grid (never a hemisphere word in scanner space: x = 0 is not the midline there).

    ``partsSpanBoth``: the lesion has parts on both sides of the midline.
    """
    if not isMni(node):
        return None
    if partsSpanBoth or rasX == 0:
        return "Both hemispheres"
    return "Left" if rasX < 0 else "Right"


#
# Figure views
#


def isFigure(node):
    """True if ``node`` is a volume tagged as a figure by the importer (``Studio.Role`` = "figure")."""
    return node is not None and hasattr(node, "GetAttribute") and node.GetAttribute(ROLE_ATTRIBUTE) == ROLE_FIGURE


def _compositeNode(viewName):
    for compositeNode in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode"):
        if compositeNode.GetLayoutName() == viewName:
            return compositeNode
    return None


def _sliceNode(viewName):
    for sliceNode in slicer.util.getNodesByClass("vtkMRMLSliceNode"):
        if sliceNode.GetLayoutName() == viewName:
            return sliceNode
    return None


def _viewName(view):
    """Layout name of a slice view given by name, slice node, composite node or slice widget."""
    if view is None:
        return None
    if isinstance(view, str):
        return view
    if hasattr(view, "IsA") and (view.IsA("vtkMRMLSliceNode") or view.IsA("vtkMRMLSliceCompositeNode")):
        return view.GetLayoutName()
    if hasattr(view, "mrmlSliceNode"):
        sliceNode = view.mrmlSliceNode()
        return sliceNode.GetLayoutName() if sliceNode is not None else None
    return None


def viewBackground(view):
    """The background volume of a slice view (name, slice node, composite node or slice widget), or None."""
    compositeNode = _compositeNode(_viewName(view))
    if compositeNode is None or not compositeNode.GetBackgroundVolumeID():
        return None
    return slicer.mrmlScene.GetNodeByID(compositeNode.GetBackgroundVolumeID())


def _singleSliceAlongNormal(volumeNode, sliceNode):
    image = volumeNode.GetImageData() if volumeNode is not None else None
    if image is None or sliceNode is None:
        return False
    dims = image.GetDimensions()
    if 1 not in dims:
        return False
    rasToIjk = vtk.vtkMatrix4x4()
    volumeNode.GetRASToIJKMatrix(rasToIjk)
    sliceToRas = sliceNode.GetSliceToRAS()
    normal = [sliceToRas.GetElement(row, 2) for row in range(3)]
    ijkNormal = [sum(rasToIjk.GetElement(row, column) * normal[column] for column in range(3)) for row in range(3)]
    axis = max(range(3), key=lambda index: abs(ijkNormal[index]))
    return dims[axis] == 1


def isFigureView(view):
    """True if the slice view shows a figure: its background is tagged ``Studio.Role`` = "figure", or is an image with a
    single slice along the view normal (a 2D picture). ``view`` is a view name ("Yellow"), a slice node, a composite node or
    a slice widget.
    """
    name = _viewName(view)
    if not name:
        return False
    background = viewBackground(name)
    if background is None:
        return False
    if isFigure(background):
        return True
    return _singleSliceAlongNormal(background, _sliceNode(name))


def figureViewNames():
    """Names of the slice views of the current layout that show a figure."""
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None:
        return []
    return [name for name in layoutManager.sliceViewNames() if isFigureView(name)]


def figureConvention(nodeOrView):
    """``Studio.FigureConvention`` of a figure ("radiological", "neurological" or "unknown"); None if it is not a figure.

    ``nodeOrView`` is the figure volume, or a slice view (name, slice node, widget) that shows it.
    """
    node = nodeOrView if hasattr(nodeOrView, "GetAttribute") and hasattr(nodeOrView, "IsA") and nodeOrView.IsA("vtkMRMLVolumeNode") else viewBackground(nodeOrView)
    if node is None:
        return None
    if not isFigure(node):
        return None
    convention = node.GetAttribute(FIGURE_CONVENTION_ATTRIBUTE)
    return convention if convention in CONVENTIONS else "unknown"
