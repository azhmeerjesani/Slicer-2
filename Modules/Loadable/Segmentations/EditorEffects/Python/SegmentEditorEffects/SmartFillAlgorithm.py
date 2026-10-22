"""Smart fill: click-to-segment region growing with an automatic intensity tolerance (numpy and VTK only).

This module is the computation behind the "Smart fill" Segment Editor effect (SegmentEditorSmartFillEffect.py).
It imports only numpy, vtk and the Python standard library (not slicer or qt), so that it can be
tested without the application and so that importing it costs no more than importing numpy.

Conventions:

- volumes are numpy arrays indexed [k, j, i] (the memory order of vtkImageData scalars);
- spacing is (si, sj, sk), as returned by vtkImageData.GetSpacing();
- sliceAxis is None in 3D, or the numpy axis (0, 1 or 2) that is held constant in 2D ("This slice") mode.

Algorithm (one click):

1. RegionGrower crops a region of interest (ROI) around the clicked voxel: a sphere of the maximum radius,
   capped at MAX_ROI_VOXELS voxels so that every click is bounded on any image resolution.
2. The seed intensity (median of a 1.5 mm neighbourhood) and noise (1.4826 x MAD) are measured, and an
   intensity scale S is chosen: the smaller of the display window and the local 5-95 percentile range, so that
   a CT shown with a wide window still has a lesion-sized scale.
3. Tolerances of 1.5 % to 45 % of S are swept (flood fill with vtkImageThresholdConnectivity); the sweep stops
   at the first region that reaches the ROI boundary. The boundary contrast of each region (mean intensity
   step across its boundary) is measured and the largest tolerance within 95 % of the best contrast wins.
4. The region is finalized: a leak guard (an opening, the seed-connected component of it, and a dilation
   back inside the region) cuts thin bridges and tentacles, then a closing and hole filling smooth it.

The work is split into generator steps (iterCreate, iterAutoTolerance, iterFinalize) of a few tens of
milliseconds each, so that the effect can run them from timer callbacks on the GUI thread without blocking
it; the morphological filters are streamed in slabs of SLAB_PLANES planes for the same reason. They are
computed with numpy on the kernel of vtkImageDilateErode3D, with results identical to the VTK filters. The
synchronous functions (RegionGrower(...), autoTolerance(), finalize(), smartFill()) run the same steps to
completion.

templateGridOf() duplicates the template grid rules and constants of
LesionNetworkMappingLib/geometry.py (KNOWN_GRIDS, detect_grid), which stays the source of truth: the
Segmentations module must not depend on the Lesion Network Mapping module. Keep both in sync.
"""

import gzip
import struct

import numpy
import vtk
from vtk.util import numpy_support

COARSE_FRACTIONS = numpy.geomspace(0.015, 0.45, 8)  # tolerance sweep, as fractions of the intensity scale
CONTRAST_PREFERENCE = 0.95  # choose the largest tolerance whose boundary contrast is >= 95 % of the best
SEED_RADIUS_MM = 1.5
SCALE_RADIUS_MM = 10.0
MAX_ROI_VOXELS = 4_000_000
MIN_REGION_MM3 = 8.0
CLOSING_MM = 1.0
FIRST_PASS_RADIUS_MM = 50.0
MIN_EDGE_GAIN = 2.0  # a real lesion edge has >= 2x the boundary contrast of the noise-limited first candidate
NOISE_EDGE_SIGMAS = 4.0  # a boundary contrast below 4 sigma is noise, not a lesion edge
CONTINUE_REACH = 0.75  # extend the radius only when the chosen region already reaches 75 % of it
SLAB_PLANES = 16  # planes of k per streamed step of the morphological filters
SPHERE_SLAB_PLANES = 64  # planes per step of the ROI sphere mask (at most)
SPHERE_SLAB_VOXELS = 400_000  # voxels per step of the ROI sphere mask: 64 planes of a 4M-voxel ROI took 25-57 ms, this ~6-15 ms
PERCENTILE_DIRECT_VOXELS = 1_000_000  # larger arrays: percentiles in bounded steps (numpy.percentile takes 75 ms at 4M)
PERCENTILE_BINS = 4096
ADD, REMOVE = 1, 2


def runToCompletion(generator):
    """Run a generator to completion and return its return value (StopIteration.value)."""
    while True:
        try:
            next(generator)
        except StopIteration as stop:
            return stop.value


def wrapArray(array):
    """Wrap a contiguous [k, j, i] array as vtkImageData in index space (origin 0, spacing 1), without a copy."""
    array = numpy.ascontiguousarray(array)
    image = vtk.vtkImageData()
    image.SetDimensions(array.shape[2], array.shape[1], array.shape[0])
    image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(array.ravel(), deep=False))
    image.numpyReference = array  # keep the buffer alive while VTK uses it
    return image


def unwrapImage(image, shape):
    return numpy_support.vtk_to_numpy(image.GetPointData().GetScalars()).reshape(shape)


class Flood:
    """Connected-threshold flood fill (6-connected vtkImageThresholdConnectivity) on one array.

    The array is wrapped in index space, because the filter interprets seeds as data coordinates
    (origin + spacing * index) and ignores the image directions.
    """

    def __init__(self, array):
        self.shape = array.shape
        self.image = wrapArray(array)
        self.filter = vtk.vtkImageThresholdConnectivity()
        self.filter.SetInputData(self.image)
        self.filter.SetInValue(1)
        self.filter.SetOutValue(0)
        self.filter.ReplaceInOn()
        self.filter.ReplaceOutOn()
        self.points = vtk.vtkPoints()
        self.filter.SetSeedPoints(self.points)

    def run(self, seedsKji, lower, upper):
        """Return (bool array of the voxels connected to the seeds within [lower, upper], number of voxels)."""
        self.points.Reset()
        for k, j, i in seedsKji:
            self.points.InsertNextPoint(float(i), float(j), float(k))
        self.points.Modified()
        self.filter.ThresholdBetween(lower, upper)
        self.filter.Modified()
        self.filter.Update()
        return unwrapImage(self.filter.GetOutput(), self.shape) != 0, int(self.filter.GetNumberOfInVoxels())


def iterPercentiles(values, percentiles):
    """Generator: numpy.percentile(values, percentiles) (linear interpolation), in steps of bounded duration.

    Small arrays use numpy.percentile directly. For large arrays the order statistics are found exactly with a
    histogram (the bin index is monotonic in the value, so a bin holds a contiguous range of ranks) and a sort of the
    few bins that hold the needed ranks; the interpolation follows numpy.percentile.
    """
    values = numpy.asarray(values).ravel()
    count = values.size
    if count <= PERCENTILE_DIRECT_VOXELS or not numpy.issubdtype(values.dtype, numpy.floating):
        return numpy.percentile(values, percentiles)
    low, high = values.min(), values.max()
    if not (numpy.isfinite(low) and numpy.isfinite(high)) or low == high:
        return numpy.percentile(values, percentiles)
    yield
    binIndex = ((values - low) * (PERCENTILE_BINS / (float(high) - float(low)))).astype(numpy.int32)
    numpy.minimum(binIndex, PERCENTILE_BINS - 1, out=binIndex)
    counts = numpy.bincount(binIndex, minlength=PERCENTILE_BINS)
    yield
    quantiles = numpy.true_divide(numpy.asarray(percentiles, dtype=numpy.float64), 100)
    virtualIndexes = (count - 1) * quantiles  # as numpy.percentile (method "linear")
    previousIndexes = numpy.floor(virtualIndexes).astype(numpy.int64)
    nextIndexes = numpy.minimum(previousIndexes + 1, count - 1)
    ranks = sorted(set(previousIndexes.tolist()) | set(nextIndexes.tolist()))
    binEnds = numpy.cumsum(counts)
    rankValues = {}
    for binNumber in sorted({int(numpy.searchsorted(binEnds, rank, side="right")) for rank in ranks}):
        binValues = values[binIndex == binNumber]
        binStart = int(binEnds[binNumber] - counts[binNumber])
        localRanks = [rank - binStart for rank in ranks if binStart <= rank < binEnds[binNumber]]
        if binValues.min() == binValues.max():
            # for example the zero background of a skull-stripped image: no ordering needed
            for localRank in localRanks:
                rankValues[binStart + localRank] = binValues[0]
        else:
            binValues.partition(localRanks)
            for localRank in localRanks:
                rankValues[binStart + localRank] = binValues[localRank]
        yield
    results = []
    for virtualIndex, previousIndex, nextIndex in zip(virtualIndexes, previousIndexes, nextIndexes, strict=True):
        a, b = rankValues[int(previousIndex)], rankValues[int(nextIndex)]
        gamma = virtualIndex - previousIndex
        difference = b - a
        results.append(b - difference * (1 - gamma) if gamma >= 0.5 else a + difference * gamma)
    return numpy.array(results)


def ellipsoidOffsets(radiusKji):
    """Index offsets (k, j, i arrays) of the voxels of an ellipsoid; a zero radius keeps that axis fixed."""
    rk, rj, ri = (int(r) for r in radiusKji)
    kk, jj, ii = numpy.mgrid[-rk:rk + 1, -rj:rj + 1, -ri:ri + 1]
    distance = numpy.zeros(kk.shape)
    for coordinate, radius in ((kk, rk), (jj, rj), (ii, ri)):
        distance = distance + ((coordinate / radius) ** 2 if radius > 0 else (coordinate != 0) * 2.0)
    inside = distance <= 1.0
    return kk[inside], jj[inside], ii[inside]


def boundingBoxSlices(mask, pad=1):
    """Slices of the bounding box of the True voxels of a non-empty mask, padded and clamped to the array."""
    # two passes over the mask (projections along k and onto the j-i plane) instead of one per axis
    projectionJi = mask.any(axis=0)
    projections = (mask.any(axis=(1, 2)), projectionJi.any(axis=1), projectionJi.any(axis=0))
    slices = []
    for axis, projection in enumerate(projections):
        nonZero = numpy.flatnonzero(projection)
        slices.append(slice(max(0, int(nonZero[0]) - pad), min(mask.shape[axis], int(nonZero[-1]) + 1 + pad)))
    return tuple(slices)


def boundaryContrast(sub, region):
    """Mean |I_a - I_b| over the 6-neighbour voxel pairs that cross the region boundary (finite values only)."""
    total, count = 0.0, 0
    for axis in range(3):
        if sub.shape[axis] < 2:
            continue
        regionAlongAxis = numpy.moveaxis(region, axis, 0)
        subAlongAxis = numpy.moveaxis(sub, axis, 0)
        crossing = regionAlongAxis[1:] != regionAlongAxis[:-1]
        if crossing.any():
            difference = numpy.abs(subAlongAxis[1:] - subAlongAxis[:-1])[crossing]
            finite = numpy.isfinite(difference)
            total += float(difference[finite].sum())
            count += int(finite.sum())
    return total / count if count else 0.0


class RegionGrower:
    """One click: ROI crop, seed statistics, intensity scale and the automatic tolerance sweep.

    :param image: source volume, [k, j, i] (a view; it is not copied, only the ROI is).
    :param seedKji: clicked voxel index.
    :param spacingIjk: voxel spacing (si, sj, sk).
    :param displayWindow: display window width of the source volume (0 if unknown).
    :param maxRadiusMm: radius of the ROI sphere (scaled down if the ROI would exceed MAX_ROI_VOXELS).
    :param sliceAxis: None (3D) or the numpy axis held constant (2D).
    :param allowed: optional bool array with the shape of image: voxels outside are never filled.
    :param intensityRange: optional (min, max): voxels outside the range are never filled.
    """

    def __init__(self, image, seedKji, spacingIjk, displayWindow, maxRadiusMm=FIRST_PASS_RADIUS_MM, sliceAxis=None,
                 allowed=None, intensityRange=None):
        runToCompletion(self._iterInit(image, seedKji, spacingIjk, displayWindow, maxRadiusMm, sliceAxis, allowed, intensityRange))

    @classmethod
    def iterCreate(cls, image, seedKji, spacingIjk, displayWindow, maxRadiusMm=FIRST_PASS_RADIUS_MM, sliceAxis=None,
                   allowed=None, intensityRange=None):
        """Generator form of the constructor: yields between steps, returns the RegionGrower."""
        grower = cls.__new__(cls)
        yield from grower._iterInit(image, seedKji, spacingIjk, displayWindow, maxRadiusMm, sliceAxis, allowed, intensityRange)
        return grower

    def _iterInit(self, image, seedKji, spacingIjk, displayWindow, maxRadiusMm, sliceAxis, allowed, intensityRange):
        spacing = numpy.array(spacingIjk[::-1], dtype=float)
        self.spacingKji = spacing
        self.sliceAxis = sliceAxis
        seed = numpy.array(seedKji, dtype=int)
        radius = float(maxRadiusMm)
        radiusVoxels = self._radiusVoxels(radius)
        numberOfVoxels = numpy.prod(2 * radiusVoxels + 1)
        if numberOfVoxels > MAX_ROI_VOXELS:
            # keep every click bounded on any image resolution
            radius *= (MAX_ROI_VOXELS / numberOfVoxels) ** (1.0 / (3 if sliceAxis is None else 2))
            radiusVoxels = self._radiusVoxels(radius)
        self.effectiveRadiusMm = radius
        lower = numpy.maximum(seed - radiusVoxels, 0)
        upper = numpy.minimum(seed + radiusVoxels + 1, image.shape[:3])
        self.roi = tuple(slice(int(a), int(b)) for a, b in zip(lower, upper, strict=True))
        self.offsetKji = lower
        sub = numpy.array(image[self.roi], dtype=numpy.float32)
        if sub.ndim == 4:
            sub = numpy.ascontiguousarray(sub[..., 0])
        self.sub = sub
        localSeed = seed - lower
        self.seedLocal = localSeed
        yield

        # Sphere mask, allowed voxels and intensity range, in slabs of planes
        work = numpy.empty(sub.shape, numpy.float32)
        shellParts = []
        _, gridJ, gridI = numpy.ogrid[0:1, 0:sub.shape[1], 0:sub.shape[2]]
        distanceJI = ((gridJ - localSeed[1]) * spacing[1]) ** 2 + ((gridI - localSeed[2]) * spacing[2]) ** 2
        shellInner = max(radius - 1.5 * spacing.max(), 0.0) ** 2 if radius > 1.5 * spacing.max() else -1.0
        planeSize = sub.shape[1] * sub.shape[2]
        allowedRoi = allowed[self.roi] if allowed is not None else None
        slabPlanes = int(min(SPHERE_SLAB_PLANES, max(1, SPHERE_SLAB_VOXELS // max(planeSize, 1))))
        for k0 in range(0, sub.shape[0], slabPlanes):
            k1 = min(sub.shape[0], k0 + slabPlanes)
            gridK = numpy.arange(k0, k1).reshape(-1, 1, 1)
            distance2 = ((gridK - localSeed[0]) * spacing[0]) ** 2 + distanceJI
            ok = distance2 <= radius * radius
            if allowedRoi is not None:
                ok &= numpy.asarray(allowedRoi[k0:k1], dtype=bool)
            if intensityRange is not None:
                ok &= (sub[k0:k1] >= intensityRange[0]) & (sub[k0:k1] <= intensityRange[1])
            shellParts.append(numpy.flatnonzero((ok & (distance2 >= shellInner)).ravel()) + k0 * planeSize)
            if k0 <= localSeed[0] < k1:
                self.outsideAllowed = not bool(ok[localSeed[0] - k0, localSeed[1], localSeed[2]])
            numpy.copyto(work[k0:k1], sub[k0:k1])
            work[k0:k1][~ok] = numpy.inf
            yield
        self.shellIndex = numpy.concatenate(shellParts) if shellParts else numpy.zeros(0, numpy.int64)
        self.work = work
        self.flood = Flood(work)

        # Seed statistics and intensity scale
        seedRadius = numpy.maximum(1, numpy.round(SEED_RADIUS_MM / spacing)).astype(int)
        if sliceAxis is not None:
            seedRadius[sliceAxis] = 0
        kk, jj, ii = ellipsoidOffsets(seedRadius)
        kk, jj, ii = kk + localSeed[0], jj + localSeed[1], ii + localSeed[2]
        keep = (kk >= 0) & (kk < sub.shape[0]) & (jj >= 0) & (jj < sub.shape[1]) & (ii >= 0) & (ii < sub.shape[2])
        self.neighbourhood = (kk[keep], jj[keep], ii[keep])
        values = sub[self.neighbourhood]
        self.mu = float(numpy.median(values))
        self.sigma = float(1.4826 * numpy.median(numpy.abs(values - self.mu)))
        self.scale = yield from self._iterIntensityScale(displayWindow)
        self.minVoxels = max(4, int(numpy.ceil(MIN_REGION_MM3 / numpy.prod(spacing))))

    def _radiusVoxels(self, radiusMm):
        radiusVoxels = numpy.ceil(radiusMm / self.spacingKji).astype(int)
        if self.sliceAxis is not None:
            radiusVoxels[self.sliceAxis] = 0
        return radiusVoxels

    def _iterIntensityScale(self, displayWindow):
        """min(display window, local p95-p5): the display window alone fails for a CT shown with a wide window.

        The local box starts at +-10 mm and doubles while it holds no edge (p95-p5 < 6 sigma), up to the ROI,
        so that a click deep inside a large homogeneous lesion still sees the lesion boundary.
        """
        seed = self.seedLocal
        radiusMm = SCALE_RADIUS_MM
        while True:
            radiusVoxels = numpy.maximum(1, numpy.round(radiusMm / self.spacingKji)).astype(int)
            if self.sliceAxis is not None:
                radiusVoxels[self.sliceAxis] = 0
            box = tuple(slice(max(0, int(seed[a] - radiusVoxels[a])), int(seed[a] + radiusVoxels[a] + 1)) for a in range(3))
            p5, p95 = yield from iterPercentiles(self.sub[box], (5.0, 95.0))
            local = float(p95 - p5)
            if local >= 6.0 * self.sigma or radiusMm >= self.effectiveRadiusMm:
                break
            radiusMm = min(2.0 * radiusMm, self.effectiveRadiusMm)
            yield
        scale = min(float(displayWindow), local) if displayWindow and displayWindow > 0 else local
        return max(scale, 4.0 * self.sigma, 1e-6 * (abs(self.mu) + 1.0))

    def reachMm(self, region):
        """Upper bound of the distance from the seed to the farthest region voxel, from the region bounding box."""
        slices = boundingBoxSlices(region, pad=0)
        seed = self.seedLocal
        distances = [max(abs(slices[a].start - seed[a]), abs(slices[a].stop - 1 - seed[a])) * self.spacingKji[a] for a in range(3)]
        return float(numpy.sqrt(numpy.sum(numpy.square(distances))))

    def seedsFor(self, lower, upper):
        values = self.sub[self.neighbourhood]
        inside = (values >= lower) & (values <= upper)
        points = list(zip(self.neighbourhood[0][inside].tolist(), self.neighbourhood[1][inside].tolist(),
                          self.neighbourhood[2][inside].tolist(), strict=True))
        return points or [tuple(int(x) for x in self.seedLocal)]

    def seedPoints(self):
        return list(zip(*[x.tolist() for x in self.neighbourhood], strict=True))

    def region(self, tolerance):
        """Return (bool region in ROI coordinates, number of voxels) for an intensity tolerance mu +- tolerance."""
        lower, upper = self.mu - tolerance, self.mu + tolerance
        return self.flood.run(self.seedsFor(lower, upper), lower, upper)

    def limitReached(self, region):
        """True if the region touches the ROI sphere (or the ROI box)."""
        return bool(region.ravel()[self.shellIndex].any()) if len(self.shellIndex) else False

    def _iterCandidate(self, fraction):
        """Generator: flood at one tolerance, then (after a yield) its boundary contrast; returns the candidate."""
        tolerance = fraction * self.scale
        region, count = self.region(tolerance)
        hit = self.limitReached(region)
        candidate = {"fraction": fraction, "tolerance": tolerance, "region": region, "count": count, "hit": hit, "contrast": -1.0}
        if not hit and count >= self.minVoxels:
            yield
            slices = boundingBoxSlices(region)
            candidate["contrast"] = boundaryContrast(self.sub[slices], region[slices])
        return candidate

    def autoTolerance(self, minFraction=None):
        """Return (tolerance, region, info); see iterAutoTolerance."""
        return runToCompletion(self.iterAutoTolerance(minFraction))

    def iterAutoTolerance(self, minFraction=None):
        """Generator: 8-step geometric sweep of the tolerance, refined around the best boundary contrast.

        The sweep stops at the first candidate that reaches the radius limit (regions are nested, so every larger
        tolerance reaches it too), then refines with the two geometric midpoints next to the best candidate.
        Yields after each candidate flood and contrast; returns (tolerance, region, info). Only the best-so-far region is kept.

        info: scale, mu, sigma, candidates [(tolerance, count, contrast)], limitReached (no valid candidate),
        fraction (of the chosen tolerance), contrastGain, noEdge (only noise-level edges), mayContinue (the
        contrast was still rising, or no real edge was found, when the sweep reached the radius limit: the lesion may
        continue).
        """
        candidates = []  # dicts without regions, sorted by fraction at the end
        best = None  # the candidate with the best contrast so far, with its region
        firstHit = None
        if minFraction is None:
            fractions = list(COARSE_FRACTIONS)
        else:
            fractions = [f for f in COARSE_FRACTIONS if f >= minFraction * 0.999]
            if not fractions or fractions[0] > minFraction * 1.001:
                fractions = [minFraction] + fractions
        for fraction in fractions:
            candidate = yield from self._iterCandidate(fraction)
            yield
            if candidate["hit"]:
                firstHit = candidate
                break
            candidates.append(candidate)
            if candidate["contrast"] >= 0 and (best is None or candidate["contrast"] > best["contrast"]):
                best = candidate
            # keep the regions of the best candidate and of the latest one (the refine step may need it)
            for c in candidates[:-1]:
                if c is not best:
                    c["region"] = None
        valid = [index for index, c in enumerate(candidates) if c["contrast"] >= 0]
        if valid:
            bestIndex = max(valid, key=lambda index: candidates[index]["contrast"])
            extraFractions = []
            if bestIndex > 0:
                extraFractions.append(numpy.sqrt(candidates[bestIndex - 1]["fraction"] * candidates[bestIndex]["fraction"]))
            if bestIndex + 1 < len(candidates):
                extraFractions.append(numpy.sqrt(candidates[bestIndex]["fraction"] * candidates[bestIndex + 1]["fraction"]))
            elif len(candidates) < len(COARSE_FRACTIONS):
                extraFractions.append(numpy.sqrt(candidates[bestIndex]["fraction"] * COARSE_FRACTIONS[len(candidates)]))
            for fraction in extraFractions:
                candidate = yield from self._iterCandidate(fraction)
                yield
                if not candidate["hit"]:
                    candidates.append(candidate)
            candidates.sort(key=lambda c: c["fraction"])
        good = [c for c in candidates if c["contrast"] >= 0]
        info = {"scale": self.scale, "mu": self.mu, "sigma": self.sigma,
                "candidates": [(c["tolerance"], c["count"], c["contrast"]) for c in candidates]}
        if not good:
            tolerance = COARSE_FRACTIONS[0] * self.scale
            region, _ = self.region(tolerance)
            info.update(limitReached=True, mayContinue=False, noEdge=False, fraction=float(COARSE_FRACTIONS[0]))
            return tolerance, region, info
        bestContrast = max(c["contrast"] for c in good)
        chosen = [c for c in good if c["contrast"] >= CONTRAST_PREFERENCE * bestContrast][-1]
        region = chosen["region"]
        if region is None:
            region, _ = self.region(chosen["tolerance"])
        gain = bestContrast / max(good[0]["contrast"], 1e-12)
        info["limitReached"] = False
        info["fraction"] = float(chosen["fraction"])
        info["contrastGain"] = gain
        info["noEdge"] = bool(gain < MIN_EDGE_GAIN and bestContrast < NOISE_EDGE_SIGMAS * max(self.sigma, 1e-12))
        # The lesion may continue beyond the radius limit if the contrast was still rising when the sweep reached it,
        # or if no real edge was found before it (in a lesion larger than the radius limit, a tolerance below the
        # noise can percolate through the lesion to the limit: then only noise-level candidates remain).
        info["mayContinue"] = bool(firstHit is not None and (info["noEdge"] or (
            good[-1]["contrast"] >= bestContrast
            and (gain < MIN_EDGE_GAIN or self.reachMm(region) >= CONTINUE_REACH * self.effectiveRadiusMm))))
        return chosen["tolerance"], region, info


_kernelOffsetsCache = {}


def kernelOffsets(radiusKji):
    """Index offsets (N x 3, k j i) of the ellipsoidal kernel that vtkImageDilateErode3D uses for this radius.

    The kernel is taken from VTK itself (the dilation of a single voxel), so that the numpy morphology below gives
    voxel-identical results to vtkImageDilateErode3D and vtkImageOpenClose3D.
    """
    key = tuple(int(r) for r in radiusKji)
    if key not in _kernelOffsetsCache:
        size = [2 * r + 1 for r in key]
        point = numpy.zeros(size, numpy.uint8)
        point[key] = 1
        dilateFilter = vtk.vtkImageDilateErode3D()
        dilateFilter.SetInputData(wrapArray(point))
        dilateFilter.SetDilateValue(1)
        dilateFilter.SetErodeValue(0)
        dilateFilter.SetKernelSize(size[2], size[1], size[0])
        dilateFilter.Update()
        kernel = unwrapImage(dilateFilter.GetOutput(), tuple(size)) != 0
        _kernelOffsetsCache[key] = numpy.argwhere(kernel) - numpy.array(key)
    return _kernelOffsetsCache[key]


_kernelRowsCache = {}


def kernelRows(radiusKji):
    """The kernel of kernelOffsets() as rows along i: a list of (dk, dj, halfWidth), each row being di = -w..w.

    Returns None if a row of the kernel is not such a centred run (then the morphology uses the offsets directly).
    """
    key = tuple(int(r) for r in radiusKji)
    if key not in _kernelRowsCache:
        rows = {}
        for dk, dj, di in kernelOffsets(key).tolist():
            rows.setdefault((dk, dj), set()).add(di)
        runs = []
        for (dk, dj), values in sorted(rows.items()):
            halfWidth = max(values)
            if values != set(range(-halfWidth, halfWidth + 1)):
                runs = None
                break
            runs.append((dk, dj, halfWidth))
        _kernelRowsCache[key] = runs
    return _kernelRowsCache[key]


def _iterSpread(mask, radiusKji, foreground):
    """Generator: dilation (foreground=True) or erosion (False) of a bool mask, SLAB_PLANES planes of k per step.

    Same result as vtkImageDilateErode3D with the same kernel size: voxels outside the array are ignored.
    Implemented with numpy (OR / AND of the shifted mask over the kernel): the kernel is split into rows along i,
    the mask is first spread along i once per row half-width, then the rows are combined (one operation per row
    instead of one per kernel voxel: 179 -> 37 + 6 for a 7x7x7 kernel). Results are identical to the VTK filter.
    """
    radius = tuple(int(r) for r in radiusKji)
    rows = kernelRows(radius)
    if rows is None:
        rows = [(dk, dj, None, di) for dk, dj, di in kernelOffsets(radius).tolist()]
    else:
        rows = [(dk, dj, halfWidth, 0) for dk, dj, halfWidth in rows]
    combine = numpy.bitwise_or if foreground else numpy.bitwise_and
    # outside voxels are ignored: they never erode (padded with True) and never dilate (padded with False)
    padded = numpy.pad(mask, [(r, r) for r in radius], constant_values=not foreground)
    output = numpy.empty_like(mask)
    nk, nj, ni = mask.shape
    ri = radius[2]
    halfWidths = sorted({row[2] for row in rows if row[2] is not None})
    for k0 in range(0, nk, SLAB_PLANES):
        k1 = min(nk, k0 + SLAB_PLANES)
        window = padded[k0:k1 + 2 * radius[0]]  # the planes that the slab's kernel reaches
        # spread along i by each half-width needed (incrementally: w from w - 1 with the two new columns)
        spreadByWidth = {}
        spread = window[:, :, ri:ri + ni]
        for width in range(1, (halfWidths[-1] if halfWidths else 0) + 1):
            spread = combine(spread, window[:, :, ri - width:ri - width + ni])
            combine(spread, window[:, :, ri + width:ri + width + ni], out=spread)
            spreadByWidth[width] = spread
        spreadByWidth[0] = window[:, :, ri:ri + ni]
        slab = output[k0:k1]
        slab[...] = not foreground
        for dk, dj, halfWidth, di in rows:
            if halfWidth is None:
                source = window[:, :, ri + di:ri + di + ni]
            else:
                source = spreadByWidth[halfWidth]
            combine(slab, source[radius[0] + dk:radius[0] + dk + k1 - k0, radius[1] + dj:radius[1] + dj + nj], out=slab)
        yield
    return output


def iterMorph(mask, radiusKji, openValue, closeValue):
    """Generator: morphological opening or closing of a bool mask, streamed in slabs; returns a bool array.

    Same result as vtkImageOpenClose3D, where OpenValue is the value that is ERODED first: (openValue, closeValue) =
    (1, 0) opens the foreground and (0, 1) closes it.
    """
    mask = numpy.asarray(mask, dtype=bool)
    if (openValue, closeValue) == (1, 0):
        eroded = yield from _iterSpread(mask, radiusKji, False)
        return (yield from _iterSpread(eroded, radiusKji, True))
    if (openValue, closeValue) == (0, 1):
        dilated = yield from _iterSpread(mask, radiusKji, True)
        return (yield from _iterSpread(dilated, radiusKji, False))
    raise ValueError("Only binary opening (1, 0) and closing (0, 1) are supported")


def iterDilate(mask, radiusKji):
    """Generator: binary dilation of a bool mask (vtkImageDilateErode3D kernel), streamed in slabs."""
    return (yield from _iterSpread(numpy.asarray(mask, dtype=bool), radiusKji, True))


def morph(mask, radiusKji, openValue, closeValue):
    return runToCompletion(iterMorph(mask, radiusKji, openValue, closeValue))


def dilate(mask, radiusKji):
    return runToCompletion(iterDilate(mask, radiusKji))


def iterLeakGuard(region, radiusKji, seedPoints):
    """Generator: cut leaks through thin bridges and tentacles thinner than the opening radius.

    The region is opened, the seed-connected component of the opening is kept, dilated by radius + 1 and
    intersected with the region (so that the lesion boundary is preserved), and its seed component returned.
    A region that the opening would mostly destroy (a small or thin lesion) is returned unchanged.
    """
    if max(radiusKji) <= 0 or not region.any():
        return region
    slices = boundingBoxSlices(region, pad=int(max(radiusKji)) + 2)
    sub = numpy.ascontiguousarray(region[slices])
    opened = yield from iterMorph(sub, radiusKji, 1, 0)
    if int(opened.sum()) < 0.5 * int(sub.sum()):
        return region
    offset = [s.start for s in slices]
    points = []
    for k, j, i in seedPoints:
        point = (k - offset[0], j - offset[1], i - offset[2])
        if all(0 <= point[a] < sub.shape[a] for a in range(3)) and opened[point]:
            points.append(point)
    if not points:
        return region
    component, _ = Flood(opened.astype(numpy.uint8)).run(points, 1, 1)
    yield
    keep = yield from iterDilate(component, [int(r) + 1 for r in radiusKji])
    keep &= sub
    keepPoints = [p for p in points if keep[p]]
    if keepPoints:
        keep, _ = Flood(keep.astype(numpy.uint8)).run(keepPoints, 1, 1)
        yield
    output = numpy.zeros_like(region)
    output[slices] = keep
    return output


def leakGuard(region, radiusKji, seedPoints):
    return runToCompletion(iterLeakGuard(region, radiusKji, seedPoints))


def iterCloseRegion(region, radiusKji):
    """Generator: morphological closing of the region (box kernel), OR'ed with the region."""
    if max(radiusKji) <= 0 or not region.any():
        return region
    slices = boundingBoxSlices(region, pad=int(max(radiusKji)) + 1)
    closed = yield from iterMorph(numpy.ascontiguousarray(region[slices]), radiusKji, 0, 1)
    output = region.copy()
    output[slices] = closed | region[slices]
    return output


def closeRegion(region, radiusKji):
    return runToCompletion(iterCloseRegion(region, radiusKji))


def fillHoles(region, sliceAxis=None):
    """Fill cavities not connected to the outside (3D), or in-plane holes (2D: no padding along the slice axis)."""
    if not region.any():
        return region
    slices = boundingBoxSlices(region, pad=0)
    sub = region[slices]
    pad = [(1, 1)] * 3
    if sliceAxis is not None:
        pad[sliceAxis] = (0, 0)
    padded = numpy.pad(sub, pad, constant_values=False)
    background, _ = Flood((~padded).astype(numpy.uint8)).run([(0, 0, 0)], 1, 1)  # a padded corner is background
    filled = ~background
    core = tuple(slice(p[0], filled.shape[a] - p[1]) for a, p in enumerate(pad))
    output = region.copy()
    output[slices] = filled[core]
    return output


def iterFinalize(grower, region, leakGuardMm=1.5, fillAndSmooth=True):
    """Generator: leak guard (skipped when leakGuardMm is 0), closing and hole filling; returns the region."""
    spacing = grower.spacingKji
    if leakGuardMm > 0:
        radius = numpy.maximum(1, numpy.round(leakGuardMm / spacing)).astype(int)
        if grower.sliceAxis is not None:
            radius[grower.sliceAxis] = 0
        region = yield from iterLeakGuard(region, radius, grower.seedPoints())
    if fillAndSmooth:
        radius = numpy.maximum(0, numpy.round(CLOSING_MM / spacing)).astype(int)
        if grower.sliceAxis is not None:
            radius[grower.sliceAxis] = 0
        region = yield from iterCloseRegion(region, radius)
        yield
        region = fillHoles(region, grower.sliceAxis)
    return region


def finalize(grower, region, leakGuardMm=1.5, fillAndSmooth=True):
    return runToCompletion(iterFinalize(grower, region, leakGuardMm, fillAndSmooth))


def composeParts(parts):
    """parts: [(sign, offsetKji, bool region)] -> (offsetKji, uint8 labels) over the union box.

    Later parts overwrite earlier ones. Returns (None, None) when there are no parts.
    """
    if not parts:
        return None, None
    lower = numpy.min([numpy.array(offset) for _, offset, _ in parts], axis=0)
    upper = numpy.max([numpy.array(offset) + numpy.array(region.shape) for _, offset, region in parts], axis=0)
    labels = numpy.zeros(tuple(int(x) for x in upper - lower), numpy.uint8)
    for sign, offset, region in parts:
        start = numpy.array(offset) - lower
        view = labels[start[0]:start[0] + region.shape[0], start[1]:start[1] + region.shape[1], start[2]:start[2] + region.shape[2]]
        view[region] = sign
    return lower, labels


def iterSmartFill(image, seedKji, spacingIjk, displayWindow, mode="3d", sliceAxis=0, tolerance=None,
                  maxRadiusMm=100.0, leakGuardMm=1.5, fillAndSmooth=True, allowed=None, intensityRange=None):
    """Generator form of smartFill (the effect runs its own progressive version of these steps)."""
    axis = sliceAxis if mode == "2d" else None
    firstRadius = min(FIRST_PASS_RADIUS_MM, maxRadiusMm) if tolerance is None else maxRadiusMm
    grower = yield from RegionGrower.iterCreate(image, seedKji, spacingIjk, displayWindow, maxRadiusMm=firstRadius,
                                                sliceAxis=axis, allowed=allowed, intensityRange=intensityRange)
    if grower.outsideAllowed:
        return grower.offsetKji, numpy.zeros(grower.sub.shape, bool), {"outsideAllowed": True, "grower": grower}
    if tolerance is None:
        chosenTolerance, region, info = yield from grower.iterAutoTolerance()
        if info.get("mayContinue") and firstRadius < maxRadiusMm:
            firstFraction = info.get("fraction")
            grower = yield from RegionGrower.iterCreate(image, seedKji, spacingIjk, displayWindow, maxRadiusMm=maxRadiusMm,
                                                        sliceAxis=axis, allowed=allowed, intensityRange=intensityRange)
            chosenTolerance, region, info = yield from grower.iterAutoTolerance(minFraction=firstFraction)
            info["escalated"] = True
        # still rising at the final radius: the lesion may continue beyond the maximum radius
        info["limitReached"] = bool(info.get("limitReached") or info.get("mayContinue"))
    else:
        region, _ = grower.region(tolerance)
        yield
        chosenTolerance, info = tolerance, {"limitReached": grower.limitReached(region)}
    region = yield from iterFinalize(grower, region, leakGuardMm, fillAndSmooth)
    info.update(tolerance=chosenTolerance, voxels=int(region.sum()), grower=grower)
    return grower.offsetKji, region, info


def smartFill(image, seedKji, spacingIjk, displayWindow, mode="3d", sliceAxis=0, tolerance=None,
              maxRadiusMm=100.0, leakGuardMm=1.5, fillAndSmooth=True, allowed=None, intensityRange=None):
    """One click -> (offsetKji, bool region, info), with the two-pass radius extension.

    The first pass uses a radius of min(FIRST_PASS_RADIUS_MM, maxRadiusMm). When its boundary contrast was still
    rising at that radius (info mayContinue), a second pass with maxRadiusMm sweeps from the first pass's
    tolerance up. A fixed tolerance skips the sweep and uses maxRadiusMm directly.
    """
    return runToCompletion(iterSmartFill(image, seedKji, spacingIjk, displayWindow, mode, sliceAxis, tolerance,
                                         maxRadiusMm, leakGuardMm, fillAndSmooth, allowed, intensityRange))


#
# Template grids and brain masks
#

# Copied from LesionNetworkMappingLib/geometry.py (KNOWN_GRIDS): template id, resolution, shape, voxel size and the
# world (RAS) coordinates of the voxel centre with the smallest x, y and z.
KNOWN_TEMPLATE_GRIDS = (
    ("MNI152NLin6Asym", 1, (182, 218, 182), 1.0, (-91.0, -126.0, -72.0)),
    ("MNI152NLin6Asym", 2, (91, 109, 91), 2.0, (-90.0, -126.0, -72.0)),
    ("MNI152NLin2009cAsym", 1, (193, 229, 193), 1.0, (-96.0, -132.0, -78.0)),
    ("MNI152NLin2009cAsym", 2, (97, 115, 97), 2.0, (-96.5, -132.5, -78.5)),
)

# Brain mask file names of the template grids (SampleData "MNI152 (neuro)" category)
BRAIN_MASK_FILE_NAMES = {
    ("MNI152NLin2009cAsym", 2): "tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz",
    ("MNI152NLin2009cAsym", 1): "tpl-MNI152NLin2009cAsym_res-01_desc-brain_mask.nii.gz",
    ("MNI152NLin6Asym", 2): "tpl-MNI152NLin6Asym_res-02_desc-brain_mask.nii.gz",
}


def _matrixToNumpy(matrix):
    if isinstance(matrix, vtk.vtkMatrix4x4):
        return numpy.array([[matrix.GetElement(r, c) for c in range(4)] for r in range(4)])
    return numpy.asarray(matrix, dtype=numpy.float64).reshape(4, 4)


def templateGridOf(dimsIjk, ijkToRas, originTolerance=1.0, spacingTolerance=1e-3):
    """Identify a standard template grid: returns (template, resolution), for example ("MNI152NLin2009cAsym", 2), or None.

    Same rules as LesionNetworkMappingLib.geometry.detect_grid: the voxel axes must be aligned with the RAS axes
    (any order and directions), the voxel size and the number of voxels along each world axis must match, and the
    lowest voxel centre must be within originTolerance mm of the template's.

    :param dimsIjk: number of voxels along i, j, k.
    :param ijkToRas: 4x4 matrix (vtkMatrix4x4 or array) mapping the voxel index (0, 0, 0) of the grid to RAS.
    """
    shape = tuple(int(n) for n in tuple(dimsIjk)[:3])
    affine = _matrixToNumpy(ijkToRas)
    if len(shape) != 3:
        return None
    linear = affine[:3, :3]
    worldAxes = numpy.argmax(numpy.abs(linear), axis=0)  # world axis of each voxel axis
    if len(set(worldAxes.tolist())) != 3:
        return None
    steps = linear[worldAxes, numpy.arange(3)]
    offAxis = linear.copy()
    offAxis[worldAxes, numpy.arange(3)] = 0.0
    if numpy.abs(offAxis).max() > spacingTolerance * numpy.abs(steps).max():
        return None
    worldShape = numpy.zeros(3, dtype=numpy.int64)
    worldSpacing = numpy.zeros(3)
    lowest = affine[:3, 3].copy()
    for axis in range(3):
        worldAxis = worldAxes[axis]
        worldShape[worldAxis] = shape[axis]
        worldSpacing[worldAxis] = abs(steps[axis])
        if steps[axis] < 0:
            lowest[worldAxis] += steps[axis] * (shape[axis] - 1)
    for template, resolution, gridShape, spacing, origin in KNOWN_TEMPLATE_GRIDS:
        if (tuple(worldShape) == gridShape
                and numpy.all(numpy.abs(worldSpacing - spacing) <= spacingTolerance * spacing)
                and numpy.all(numpy.abs(lowest - origin) <= originTolerance)):
            return template, resolution
    return None


NIFTI_DATA_TYPES = {
    2: numpy.uint8, 4: numpy.int16, 8: numpy.int32, 16: numpy.float32, 64: numpy.float64,
    256: numpy.int8, 512: numpy.uint16, 768: numpy.uint32, 1024: numpy.int64, 1280: numpy.uint64,
}


def readNifti1(path):
    """Minimal NIfTI-1 reader (.nii or .nii.gz, scalar volumes with an sform).

    :return: (array indexed [k, j, i], 4x4 voxel-to-RAS affine as a numpy array).
    :raises ValueError: for files that are not NIfTI-1, have an unsupported data type or no sform.
    """
    with open(path, "rb") as file:
        data = file.read()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    if len(data) < 348:
        raise ValueError(f"Not a NIfTI-1 file: {path}")
    if struct.unpack("<i", data[0:4])[0] == 348:
        byteOrder = "<"
    elif struct.unpack(">i", data[0:4])[0] == 348:
        byteOrder = ">"
    else:
        raise ValueError(f"Not a NIfTI-1 file: {path}")
    dim = struct.unpack(byteOrder + "8h", data[40:56])
    dataType = struct.unpack(byteOrder + "h", data[70:72])[0]
    voxOffset = int(struct.unpack(byteOrder + "f", data[108:112])[0])
    slope, intercept = struct.unpack(byteOrder + "2f", data[112:120])
    sformCode = struct.unpack(byteOrder + "h", data[254:256])[0]
    if sformCode == 0:
        raise ValueError(f"NIfTI file without sform: {path}")
    if dataType not in NIFTI_DATA_TYPES:
        raise ValueError(f"Unsupported NIfTI data type {dataType}: {path}")
    if dim[0] < 3 or any(int(d) > 1 for d in dim[4:1 + dim[0]]):
        raise ValueError(f"Only 3D scalar NIfTI volumes are supported: {path}")
    rows = [struct.unpack(byteOrder + "4f", data[offset:offset + 16]) for offset in (280, 296, 312)]
    affine = numpy.eye(4)
    affine[:3, :] = numpy.array(rows, dtype=numpy.float64)
    nx, ny, nz = int(dim[1]), int(dim[2]), int(dim[3])
    dtype = numpy.dtype(NIFTI_DATA_TYPES[dataType]).newbyteorder(byteOrder)
    array = numpy.frombuffer(data, dtype=dtype, count=nx * ny * nz, offset=max(voxOffset, 348)).reshape(nz, ny, nx)
    if slope not in (0.0, 1.0) or intercept != 0.0:
        array = array * (slope if slope != 0.0 else 1.0) + intercept
    return array, affine


def maskToReference(mask, maskAffine, referenceDimsIjk, referenceIjkToRas, tolerance=1e-3):
    """Express a mask ([k, j, i], voxel-to-RAS affine) on a reference grid that shares its voxel centres.

    Only signed axis permutations (same voxel size, axes swapped or flipped) with identical voxel centres within
    tolerance mm are supported (numpy transpose and flip, no resampling).

    :return: bool array [k, j, i] with the reference shape, or None if the grids do not share voxel centres.
    """
    referenceAffine = _matrixToNumpy(referenceIjkToRas)
    matrix = numpy.linalg.inv(referenceAffine) @ numpy.asarray(maskAffine, dtype=numpy.float64)  # mask ijk -> reference ijk
    rounded = numpy.round(matrix)
    if numpy.abs(matrix - rounded).max() > tolerance or numpy.any(rounded[3] != [0, 0, 0, 1]):
        return None
    linear = rounded[:3, :3]
    if not (numpy.array_equal(numpy.abs(linear).sum(axis=0), [1, 1, 1]) and numpy.array_equal(numpy.abs(linear).sum(axis=1), [1, 1, 1])):
        return None
    referenceShapeIjk = tuple(int(n) for n in referenceDimsIjk[:3])
    maskIjk = numpy.asarray(mask).transpose(2, 1, 0)  # [i, j, k]
    if maskIjk.ndim != 3:
        return None
    # destination axis of each mask axis, and its sign
    destinationAxes = numpy.argmax(numpy.abs(linear), axis=0)
    signs = linear[destinationAxes, numpy.arange(3)]
    offsets = rounded[:3, 3]
    order = numpy.argsort(destinationAxes)  # mask axis for each destination axis
    permuted = maskIjk.transpose(order)
    for destinationAxis in range(3):
        sourceAxis = order[destinationAxis]
        if signs[sourceAxis] < 0:
            permuted = numpy.flip(permuted, axis=destinationAxis)
    # after the flips, destination index = permuted index + start along each axis
    starts = []
    for destinationAxis in range(3):
        sourceAxis = order[destinationAxis]
        size = maskIjk.shape[sourceAxis]
        start = offsets[destinationAxis] if signs[sourceAxis] > 0 else offsets[destinationAxis] - (size - 1)
        starts.append(int(start))
    if tuple(permuted.shape) != referenceShapeIjk or any(starts):
        return None
    return numpy.ascontiguousarray(permuted.transpose(2, 1, 0) != 0)
