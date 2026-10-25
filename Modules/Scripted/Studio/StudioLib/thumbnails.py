"""Lesion thumbnails of the Library grid (W4-02; SPEC 3.1 and D19).

A thumbnail is an axial view of a subject: the maximum intensity projection (MIP) of the background (the study
template, or the subject's own scan for scan-space and normalized rows) in grey, and the lesion MIP in the lesion
colour (#F2545B) on top. Pixels are placed through the image affines (voxel → RAS world → thumbnail pixel), never
by array index, in the orientation of Slicer's Red axial view: patient right on screen left (patient left on screen
right) and anterior at the top.

Threading (D19):

- the worker (one ``threading.Thread`` per open study) only reads NIfTI files with ``LesionNetworkMappingLib.nifti``,
  computes numpy arrays with ``LesionNetworkMappingLib.geometry.resample_to_grid`` ("nearest" mode), encodes a PNG
  (zlib) and writes it to ``<study>/.studio/thumbs/<sid>-<lesion mtime>-<size>.png``. It never calls Qt, VTK, MRML
  or ``logging`` (whose Slicer handler reaches Qt): errors travel back to the main thread with the result;
- the main thread drains the results with a 30 ms ``QTimer`` and turns each PNG into a ``QPixmap`` (bounded by a
  per-tick budget). A cached PNG is read by the worker and decoded the same way, so reopening a study shows the
  thumbnails without computing them again.

``assertMainThread()`` / ``assertWorkerThread()`` guard both sides, and ``ThumbnailService(..., audit=True)``
records any call into Qt, VTK, MRML or Slicer code made on the worker thread (``workerViolations()``).

Requests hold only the participant id and file paths (never group, scores or symptoms), so a study with
``blind_tracing`` passes no hidden column to the worker.
"""

import collections
import hashlib
import math
import os
import re
import struct
import sys
import threading
import time
import traceback
import zlib

import numpy as np

#: Side of a computed thumbnail in pixels (the grid scales it to the card size)
THUMB_SIZE = 160
#: Lesion colour (SPEC 4.12)
LESION_RGB = (0xF2, 0x54, 0x5B)
#: Smallest field of view of a lesion shown without a background (mm across), so its size stays readable
LESION_ONLY_FOV_MM = 110.0
#: Thumbnail folder in a study (application state, ignored by analyses; study.py)
THUMBS_DIR = (".studio", "thumbs")
#: Interval of the main-thread drain timer and its work budget per tick
DRAIN_INTERVAL_MS = 30
DRAIN_BUDGET_S = 0.012
#: Decoded thumbnails kept in memory
MAX_PIXMAPS = 1200
#: Module prefixes that the worker thread must never call (the audit, D19)
FORBIDDEN_PREFIXES = ("qt", "vtk", "slicer", "mrml", "ctk", "PythonQt", "MRML", "logging")
#: Points per resampling slab on the worker: numpy holds the interpreter lock during an indexing pass, so small slabs
#: (a few ms each) keep the main thread's painting responsive while thumbnails are computed
WORKER_SLAB_POINTS = 200_000
#: Background backdrop grey levels: the darkest tissue and the range above it
GREY_FLOOR = 28
GREY_RANGE = 200
#: Fraction of the robust maximum below which a background pixel is transparent (air)
AIR_FRACTION = 0.04


def assertMainThread():
    """Raise if the caller is not the main (Qt) thread."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("Studio thumbnails: Qt work must run on the main thread")


def assertWorkerThread():
    """Raise if the caller is the main thread (the numpy work belongs to the worker)."""
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("Studio thumbnails: this runs on the worker thread only")


#
# Pure numpy and files (worker side)
#


def backgroundDigest(path):
    """12 hex digits naming a background file and its version (absolute path and mtime); ``"0"*12`` without one."""
    if not path:
        return "0" * 12
    text = f"{os.path.abspath(path)}\n{fileMtime(path)}"
    return hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def cacheName(sid, lesionMtime, size=THUMB_SIZE, background=None):
    """``<sid>-<lesion mtime in ms>-<background digest>-<size>.png`` (mtime 0 without a lesion).

    The background is part of the name, so attaching or replacing a scan, or switching the study template, makes a new
    thumbnail even when the lesion did not change.
    """
    return f"{sid}-{int(lesionMtime)}-{backgroundDigest(background)}-{int(size)}.png"


def backgroundCacheName(path, size=THUMB_SIZE):
    """Name of a background-only thumbnail (a subject without a lesion on the study template), shared by its rows."""
    return f"_bg-{backgroundDigest(path)}-{int(size)}.png"


def fileMtime(path):
    """Modification time in ms, or 0 when the file is missing."""
    try:
        return os.stat(path).st_mtime_ns // 1_000_000
    except (OSError, TypeError):
        return 0


def _volume(path):
    from LesionNetworkMappingLib import nifti

    data, affine, _header = nifti.read_nifti(path)
    data = np.asarray(data)
    while data.ndim > 3:
        data = data[..., 0]
    if data.ndim != 3:
        raise ValueError(f"Not a 3D image: {path}")
    return data, affine


def _worldBox(lo, hi, affine):
    """RAS bounding box (min xyz, max xyz) of the voxel block [lo, hi] (voxel edges included)."""
    from LesionNetworkMappingLib import geometry

    corners = np.array([[i, j, k] for i in (lo[0] - 0.5, hi[0] + 0.5) for j in (lo[1] - 0.5, hi[1] + 0.5) for k in (lo[2] - 0.5, hi[2] + 0.5)])
    world = geometry.voxel_to_world(corners, affine)
    return world.min(axis=0), world.max(axis=0)


class Frame:
    """The thumbnail's axial pixel grid in RAS, as in Slicer's Red axial view.

    Column c is x = right edge - c * s (patient right on screen left) and row r is y = top - r * s (anterior at the top).
    """

    def __init__(self, boxMin, boxMax, size=THUMB_SIZE, margin=0.02):
        boxMin = np.asarray(boxMin, dtype=np.float64)
        boxMax = np.asarray(boxMax, dtype=np.float64)
        self.size = int(size)
        extent = max(float(boxMax[0] - boxMin[0]), float(boxMax[1] - boxMin[1]), 1e-3)
        self.spacing = extent * (1.0 + 2 * margin) / self.size
        self.center = (boxMin[:2] + boxMax[:2]) / 2.0
        self.zRange = (float(boxMin[2]), float(boxMax[2]))

    def x(self, column):
        return self.center[0] + (self.size / 2.0 - 0.5 - column) * self.spacing

    def y(self, row):
        return self.center[1] + (self.size / 2.0 - 0.5 - row) * self.spacing

    def pixel(self, xyz):
        """Continuous (column, row) of (N, 3) RAS points."""
        xyz = np.asarray(xyz, dtype=np.float64).reshape(-1, 3)
        column = (self.center[0] - xyz[:, 0]) / self.spacing + self.size / 2.0 - 0.5
        row = (self.center[1] - xyz[:, 1]) / self.spacing + self.size / 2.0 - 0.5
        return column, row

    def grid(self, column0, row0, columns, rows, z0, zSpacing, slices):
        """A ``geometry.Grid`` whose i axis is the columns, j the rows and k the axial slices from z0 (centre)."""
        from LesionNetworkMappingLib import geometry

        affine = np.array([
            [-self.spacing, 0.0, 0.0, self.x(column0)],
            [0.0, -self.spacing, 0.0, self.y(row0)],
            [0.0, 0.0, zSpacing, z0],
            [0.0, 0.0, 0.0, 1.0]])
        return geometry.Grid((columns, rows, slices), affine)


def frameOf(shape, affine, size=THUMB_SIZE):
    """The frame that shows a whole volume."""
    boxMin, boxMax = _worldBox((0, 0, 0), np.asarray(shape[:3]) - 1, affine)
    return Frame(boxMin, boxMax, size)


def backgroundMip(data, affine, frame):
    """Axial MIP of a volume in the frame: float array [row, column]; 0 outside the volume."""
    from LesionNetworkMappingLib import geometry

    assertWorkerThread()
    zSpacing = frame.spacing
    slices = max(1, math.ceil((frame.zRange[1] - frame.zRange[0]) / zSpacing))
    grid = frame.grid(0, 0, frame.size, frame.size, frame.zRange[0] + zSpacing / 2.0, zSpacing, slices)
    volume = geometry.resample_to_grid(data, affine, grid, mode="nearest", max_points=WORKER_SLAB_POINTS)
    return volume.max(axis=2).T


def lesionMip(data, affine, frame):
    """Axial MIP of a lesion (voxels > 0) in the frame: bool array [row, column].

    The lesion's bounding box is resampled ("nearest", through the affines) at the frame's pixel spacing and at most
    the lesion's own slice spacing, and every lesion voxel centre is also marked, so a lesion smaller than a pixel
    never disappears.
    """
    from LesionNetworkMappingLib import geometry

    assertWorkerThread()
    size = frame.size
    out = np.zeros((size, size), dtype=bool)
    mask = np.nan_to_num(np.asarray(data, dtype=np.float32)) > 0
    voxels = np.argwhere(mask)
    if voxels.size == 0:
        return out
    lo, hi = voxels.min(axis=0), voxels.max(axis=0)
    crop = mask[lo[0]:hi[0] + 1, lo[1]:hi[1] + 1, lo[2]:hi[2] + 1].astype(np.uint8)
    shift = np.eye(4)
    shift[:3, 3] = lo
    cropAffine = np.asarray(affine, dtype=np.float64) @ shift
    boxMin, boxMax = _worldBox((0, 0, 0), np.asarray(crop.shape) - 1, cropAffine)
    columns, rows = frame.pixel(np.array([[boxMin[0], boxMin[1], 0.0], [boxMax[0], boxMax[1], 0.0]]))
    column0 = max(0, math.floor(columns.min()))
    column1 = min(size, math.ceil(columns.max()) + 1)
    row0 = max(0, math.floor(rows.min()))
    row1 = min(size, math.ceil(rows.max()) + 1)
    if column0 < column1 and row0 < row1:
        spacing = np.sqrt((np.asarray(affine, dtype=np.float64)[:3, :3] ** 2).sum(axis=0))
        zSpacing = max(1e-3, min(frame.spacing, float(spacing.min())))
        slices = max(1, math.ceil((boxMax[2] - boxMin[2]) / zSpacing))
        grid = frame.grid(column0, row0, column1 - column0, row1 - row0, float(boxMin[2]) + zSpacing / 2.0, zSpacing, slices)
        resampled = geometry.resample_to_grid(crop, cropAffine, grid, mode="nearest", max_points=WORKER_SLAB_POINTS)
        out[row0:row1, column0:column1] |= (resampled.max(axis=2) > 0.5).T
    # Voxel centres (sub-pixel lesions)
    world = geometry.voxel_to_world(voxels, affine)
    columns, rows = frame.pixel(world)
    columns = np.floor(columns + 0.5).astype(np.int64)
    rows = np.floor(rows + 0.5).astype(np.int64)
    inside = (columns >= 0) & (columns < size) & (rows >= 0) & (rows < size)
    out[rows[inside], columns[inside]] = True
    return out


def compose(grey, lesion, size=THUMB_SIZE):
    """RGBA uint8 image [row, column, 4]: the background in grey (transparent air) and the lesion in its colour."""
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    if grey is not None:
        positive = grey[grey > 0]
        top = float(np.percentile(positive, 99.5)) if positive.size else 0.0
        if top > 0:
            value = np.clip(grey / top, 0.0, 1.0)
            level = (GREY_FLOOR + GREY_RANGE * value).astype(np.uint8)
            rgba[..., 0] = level
            rgba[..., 1] = level
            rgba[..., 2] = level
            rgba[..., 3] = np.where(value > AIR_FRACTION, 255, 0).astype(np.uint8)
    if lesion is not None and lesion.any():
        rgba[lesion, 0] = LESION_RGB[0]
        rgba[lesion, 1] = LESION_RGB[1]
        rgba[lesion, 2] = LESION_RGB[2]
        rgba[lesion, 3] = 255
    return rgba


def encodePng(rgba):
    """PNG bytes of an RGBA uint8 image [row, column, 4] (zlib only)."""
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    height, width = rgba.shape[:2]
    raw = np.zeros((height, 1 + width * 4), dtype=np.uint8)  # filter byte 0 (None) per row
    raw[:, 1:] = rgba.reshape(height, width * 4)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b"")


def decodePngSize(data):
    """(width, height) of PNG bytes, or None (a quick check of cached files)."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _writeAtomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "wb") as f:
        f.write(data)
    os.replace(temporary, path)


_STALE_MIDDLE = re.compile(r"\d+(-[0-9a-f]{12})?\Z")


def _removeStale(folder, name, sid, size):
    """Remove the other thumbnails of a subject at this size (an older lesion)."""
    prefix, suffix = f"{sid}-", f"-{int(size)}.png"
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for other in names:
        # ``<mtime>`` (older names) or ``<mtime>-<background digest>`` between the prefix and the suffix
        if other != name and other.startswith(prefix) and other.endswith(suffix) and _STALE_MIDDLE.match(other[len(prefix):-len(suffix)]):
            try:
                os.remove(os.path.join(folder, other))
            except OSError:
                pass


class Request(collections.namedtuple("Request", "key sid lesion background cachePath size")):
    """A thumbnail to produce.

    ``key`` (the PNG name), participant id, lesion and background files (absolute or None), the PNG cache file (None: keep
    in memory only) and the side in pixels. Nothing else (blind tracing).
    """


class Renderer:
    """Computes thumbnails (worker thread): keeps the last background MIPs (the study template is computed once)."""

    def __init__(self, keep=2):
        self._backgrounds = collections.OrderedDict()
        self.keep = keep

    def background(self, path, size):
        key = (os.path.abspath(path), fileMtime(path), size)
        entry = self._backgrounds.get(key)
        if entry is None:
            data, affine = _volume(path)
            frame = frameOf(data.shape, affine, size)
            entry = (frame, backgroundMip(data, affine, frame))
            self._backgrounds[key] = entry
            while len(self._backgrounds) > self.keep:
                self._backgrounds.popitem(last=False)
        else:
            self._backgrounds.move_to_end(key)
        return entry

    def render(self, request):
        """RGBA array of a request."""
        assertWorkerThread()
        frame, grey = (None, None)
        if request.background and os.path.isfile(request.background):
            frame, grey = self.background(request.background, request.size)
        mask = None
        if request.lesion and os.path.isfile(request.lesion):
            data, affine = _volume(request.lesion)
            if frame is None:
                voxels = np.argwhere(np.nan_to_num(np.asarray(data, dtype=np.float32)) > 0)
                if voxels.size:
                    boxMin, boxMax = _worldBox(voxels.min(axis=0), voxels.max(axis=0), affine)
                    # Framed on the lesion with 25% padding and at least LESION_ONLY_FOV_MM across, so a small lesion
                    # keeps a readable size instead of filling the thumbnail
                    extent = max(float(boxMax[0] - boxMin[0]), float(boxMax[1] - boxMin[1]))
                    half = max(0.75 * extent, LESION_ONLY_FOV_MM / 2.0)
                    center = (np.asarray(boxMin, dtype=np.float64) + np.asarray(boxMax, dtype=np.float64)) / 2.0
                    lo, hi = center.copy(), center.copy()
                    lo[:2] -= half
                    hi[:2] += half
                    lo[2], hi[2] = boxMin[2], boxMax[2]
                    frame = Frame(lo, hi, request.size)
                else:
                    frame = frameOf(data.shape, affine, request.size)
            mask = lesionMip(data, affine, frame)
        if frame is None:
            raise FileNotFoundError("Neither the lesion nor a background image exists")
        return compose(grey, mask, request.size)

    def produce(self, request):
        """PNG bytes of a request: the cached file when it exists, else computed and cached. Returns (bytes, cached)."""
        assertWorkerThread()
        if request.cachePath and os.path.isfile(request.cachePath):
            with open(request.cachePath, "rb") as f:
                data = f.read()
            if decodePngSize(data) == (request.size, request.size):
                return data, True
        data = encodePng(self.render(request))
        if request.cachePath:
            try:
                _writeAtomic(request.cachePath, data)
                if request.sid and not request.key.startswith("_bg-"):
                    _removeStale(os.path.dirname(request.cachePath), os.path.basename(request.cachePath), request.sid, request.size)
            except OSError:
                pass  # a read-only study keeps its thumbnails in memory
        return data, False


#
# Service (main thread side + its worker)
#


def _preimport():
    """Import the worker's modules on the main thread first.

    An import on the worker holds the interpreter lock for long stretches of Python code, which would stall the main
    thread's painting (each Qt call waits for the lock).
    """
    import importlib

    for name in ("LesionNetworkMappingLib.nifti", "LesionNetworkMappingLib.geometry", "scipy.ndimage"):
        try:
            importlib.import_module(name)
        except ImportError:
            pass


class _Audit:
    """``sys.setprofile`` hook of the worker thread: records calls into the forbidden modules."""

    def __init__(self):
        self.violations = []

    @staticmethod
    def _moduleOf(frame, event, arg):
        if event == "call":
            return frame.f_globals.get("__name__", "") or ""
        if event == "c_call":
            module = getattr(arg, "__module__", None)
            if not module:
                owner = getattr(arg, "__self__", None)
                module = type(owner).__module__ if owner is not None else ""
            return module or ""
        return None

    def __call__(self, frame, event, arg):
        module = self._moduleOf(frame, event, arg)
        if module and module.split(".", 1)[0] in FORBIDDEN_PREFIXES and len(self.violations) < 100:
            self.violations.append(f"{module}.{getattr(arg, '__name__', None) or frame.f_code.co_name}")


class ThumbnailService:
    """Thumbnails of one study: a worker thread that produces PNGs, and a 30 ms main-thread drain into ``QPixmap``s.

    ``request(request, front)`` queues a thumbnail (``front`` for the cards on screen); ``pixmap(key)`` returns it once
    ready, else None; listeners ``fn(keys)`` are called on the main thread when thumbnails arrive.
    """

    def __init__(self, root, size=THUMB_SIZE, audit=False):
        assertMainThread()
        import qt

        self.root = root
        self.size = int(size)
        self.folder = os.path.join(root, *THUMBS_DIR) if root else None
        self.audit = _Audit() if audit else None
        self._condition = threading.Condition()
        self._pending = collections.deque()
        self._queued = set()
        self._promoted = set()
        self._results = collections.deque()
        self._busy = False
        self._stopped = False
        self._thread = None
        self._renderer = Renderer()
        self._pixmaps = collections.OrderedDict()
        self._failed = {}
        self.listeners = []
        self.stats = {"computed": 0, "cached": 0, "failed": 0, "tickMs": collections.deque(maxlen=2000), "workerMs": collections.deque(maxlen=2000)}
        self._timer = qt.QTimer()
        self._timer.setInterval(DRAIN_INTERVAL_MS)
        self._timer.connect("timeout()", self._drain)

    # Requests (main thread)

    def cachePath(self, key):
        return os.path.join(self.folder, key) if self.folder else None

    def requestFor(self, sid, lesion, background):
        """The request of a subject: its lesion over the background, or the background alone (shared). None if nothing to show."""
        hasLesion = bool(lesion) and os.path.isfile(lesion)
        hasBackground = bool(background) and os.path.isfile(background)
        if hasLesion:
            key = cacheName(sid, fileMtime(lesion), self.size, background if hasBackground else None)
        elif hasBackground:
            key = backgroundCacheName(background, self.size) if sid is None else cacheName(sid, 0, self.size, background)
        else:
            return None
        return Request(key, sid, lesion if hasLesion else None, background if hasBackground else None, self.cachePath(key), self.size)

    def pixmap(self, key):
        pixmap = self._pixmaps.get(key)
        if pixmap is not None:
            self._pixmaps.move_to_end(key)
        return pixmap

    def failed(self, key):
        """The error text of a thumbnail that could not be made, else None."""
        return self._failed.get(key)

    def request(self, request, front=False):
        """Queue a thumbnail unless it is ready, failed or queued (``front`` moves a queued one ahead, once)."""
        assertMainThread()
        if request is None or self._stopped or request.key in self._pixmaps or request.key in self._failed:
            return False
        with self._condition:
            if request.key in self._queued:
                if front and request.key not in self._promoted:
                    self._promoted.add(request.key)
                    for index, queued in enumerate(self._pending):
                        if queued.key == request.key:
                            del self._pending[index]
                            self._pending.appendleft(queued)
                            break
                return False
            self._queued.add(request.key)
            if front:
                self._promoted.add(request.key)
                self._pending.appendleft(request)
            else:
                self._pending.append(request)
            self._condition.notify()
        self._ensureThread()
        if not self._timer.isActive():
            self._timer.start()
        return True

    def cancelPending(self):
        """Forget the queued requests (the study's rows changed); the one being computed still arrives."""
        with self._condition:
            self._pending.clear()
            self._queued.clear()
            self._promoted.clear()

    def isIdle(self):
        with self._condition:
            return not self._pending and not self._busy and not self._results

    def _ensureThread(self):
        if self._thread is None or not self._thread.is_alive():
            _preimport()
            self._thread = threading.Thread(target=self._run, name="StudioThumbnails", daemon=True)
            self._thread.start()

    def stop(self, wait=0.0):
        """Stop the worker (it finishes the thumbnail being computed) and the drain timer."""
        with self._condition:
            self._stopped = True
            self._pending.clear()
            self._queued.clear()
            self._condition.notify_all()
        try:
            self._timer.stop()
        except Exception:
            pass
        if wait and self._thread is not None:
            self._thread.join(wait)

    def workerViolations(self):
        """Calls into Qt / VTK / MRML / Slicer / logging seen on the worker thread (audit mode only)."""
        return list(self.audit.violations) if self.audit is not None else []

    # Worker thread: numpy and files only

    def _run(self):
        if self.audit is not None:
            sys.setprofile(self.audit)
        try:
            while True:
                with self._condition:
                    while not self._pending and not self._stopped:
                        self._condition.wait()
                    if self._stopped:
                        return
                    request = self._pending.popleft()
                    self._busy = True
                start = time.perf_counter()
                try:
                    data, cached = self._renderer.produce(request)
                    result = (request.key, "cached" if cached else "computed", data)
                except Exception:
                    result = (request.key, "error", traceback.format_exc())
                elapsed = (time.perf_counter() - start) * 1000.0
                with self._condition:
                    # The key stays queued until the main thread has its pixmap (no second request in between)
                    self._results.append(result + (elapsed,))
                    self._busy = False
        finally:
            if self.audit is not None:
                sys.setprofile(None)

    # Main thread: PNG bytes → QPixmap within the tick budget

    def _drain(self):
        assertMainThread()
        import qt

        start = time.perf_counter()
        ready = []
        while self._results:
            key, kind, payload, workerMs = self._results.popleft()
            with self._condition:
                self._queued.discard(key)
                self._promoted.discard(key)
            if kind == "error":
                self.stats["failed"] += 1
                self._failed[key] = payload.strip().splitlines()[-1] if payload else "error"
                import logging

                logging.warning(f"Studio Library: no thumbnail for {key}:\n{payload}")
            else:
                image = qt.QImage()
                if image.loadFromData(qt.QByteArray(payload), "PNG"):
                    self._pixmaps[key] = qt.QPixmap.fromImage(image)
                    while len(self._pixmaps) > MAX_PIXMAPS:
                        self._pixmaps.popitem(last=False)
                    self.stats[kind] += 1
                    self.stats["workerMs"].append(workerMs)
                else:
                    self._failed[key] = "unreadable PNG"
            ready.append(key)
            if time.perf_counter() - start > DRAIN_BUDGET_S:
                break
        if ready:
            for listener in list(self.listeners):
                try:
                    listener(ready)
                except Exception:
                    import logging

                    logging.error(f"Studio Library: thumbnail listener failed:\n{traceback.format_exc()}")
        self.stats["tickMs"].append((time.perf_counter() - start) * 1000.0)
        if self._stopped or self.isIdle():
            self._timer.stop()


#
# Project thumbnails (V-02): the axial, coronal and sagittal slices through the lesion of a project
#

#: Files of a project's thumbnails in its hidden ``.studio`` folder: the axial view (Library cards, the inspector) and
#: the coronal and sagittal views (the 3-view strip of the Continue card)
PROJECT_VIEW_FILES = (("axial", "thumbnail.png"), ("coronal", "thumbnail-coronal.png"), ("sagittal", "thumbnail-sagittal.png"))
#: Pixel size of the axial thumbnail and of the two side views
PROJECT_THUMB_SIZE = (480, 360)
PROJECT_SIDE_SIZE = (240, 180)
#: The lesion overlay: the lesion colour at 45 % with a full-colour outline (VISUAL 3)
PROJECT_LESION_RGB = (0xFF, 0x5A, 0x5F)
PROJECT_LESION_ALPHA = 0.45
#: Margin around the image in a thumbnail (fraction of the field of view)
PROJECT_MARGIN = 0.06
#: Grey level (0..1 of the window) above which a pixel is content when a view is fitted to what its slice shows
PROJECT_CONTENT_LEVEL = 0.08
#: Slices of the lesion label map read per step of the centroid pass (keeps each numpy call short, see WORKER_SLAB_POINTS)
_CENTROID_SLAB = 8
#: Screen axes of the views in RAS, as in Slicer's slice views (radiological: patient right on screen left, anterior or
#: superior at the top, anterior on the left of the sagittal view): (rightward, downward, normal)
_VIEW_AXES = {
    "axial": ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), 2),
    "coronal": ((-1.0, 0.0, 0.0), (0.0, 0.0, -1.0), 1),
    "sagittal": ((0.0, -1.0, 0.0), (0.0, 0.0, -1.0), 0),
}


class ProjectThumbnailSource:
    """What a project thumbnail is made of, taken on the main thread (``projectThumbnailSource()``) and rendered by the
    background writer (``renderProjectViews()``): numpy views of the background volume and of the lesion label maps, their
    voxel-to-RAS matrices and the window/level of the background. The VTK objects that own the arrays are shallow copies
    kept alive in ``_keep`` until ``release()`` (on the main thread): the worker only reads numpy memory.
    """

    def __init__(self):
        self.key = None
        #: numpy [k, j, i] and the 4 x 4 matrix of array indices (i, j, k, 1) -> RAS
        self.background = None
        self.backgroundMatrix = None
        self.window = None
        self.level = None
        #: [(numpy [k, j, i], 4 x 4 matrix, label value)]
        self.lesions = []
        self._keep = []

    def release(self):
        """Drop the arrays and the VTK objects (main thread)."""
        self.background = None
        self.lesions = []
        self._keep = []


def _matrixArray(vtkMatrix):
    return np.array([[vtkMatrix.GetElement(row, column) for column in range(4)] for row in range(4)], dtype=float)


def _imageArray(imageData, keep):
    """numpy [k, j, i] view of the first component of a shallow copy of ``imageData`` (main thread), or None."""
    import vtk
    from vtk.util import numpy_support

    if imageData is None:
        return None, None
    dimensions = imageData.GetDimensions()
    if min(dimensions) < 1 or imageData.GetPointData() is None or imageData.GetPointData().GetScalars() is None:
        return None, None
    copy = imageData.NewInstance()
    copy.ShallowCopy(imageData)
    scalars = copy.GetPointData().GetScalars()
    keep.extend((copy, scalars))
    array = numpy_support.vtk_to_numpy(scalars)
    if array.ndim > 1:
        array = array[:, 0]
    extent = copy.GetExtent()
    offset = vtk.vtkMatrix4x4()
    for axis in range(3):
        offset.SetElement(axis, 3, float(extent[2 * axis]))
    return array.reshape(dimensions[2], dimensions[1], dimensions[0]), offset


def _isBackgroundCandidate(node):
    if node is None or not node.IsA("vtkMRMLScalarVolumeNode") or node.IsA("vtkMRMLLabelMapVolumeNode") or node.GetImageData() is None:
        return False
    try:
        from . import space

        if node.GetAttribute(space.ROLE_ATTRIBUTE) in (space.ROLE_FIGURE, space.ROLE_STATMAP):
            return False
    except ImportError:
        pass
    # Never a network map (LNM blinding, as the autosave thumbnails: ``autosave_io.isNetworkMapNode``)
    if node.GetAttribute("LNM.Result") == "1":
        return False
    dimensions = node.GetImageData().GetDimensions()
    return min(dimensions) > 1


def _segmentationLayers(segmentationNode):
    """[(vtkOrientedImageData, label value)] of the segments of ``segmentationNode`` that have a binary label map."""
    import slicer

    segmentation = segmentationNode.GetSegmentation() if segmentationNode is not None else None
    if segmentation is None:
        return []
    name = slicer.vtkSegmentationConverter.GetBinaryLabelmapRepresentationName()
    layers = []
    for index in range(min(segmentation.GetNumberOfSegments(), 8)):
        segment = segmentation.GetNthSegment(index)
        representation = segment.GetRepresentation(name) if segment is not None else None
        if representation is not None and representation.GetPointData() is not None and representation.GetPointData().GetScalars() is not None:
            layers.append((representation, segment.GetLabelValue()))
    return layers


def projectThumbnailSource(scene=None):
    """The images of a project thumbnail (main thread, well under a millisecond), or None when the scene has no image.

    The background is the reference image of the lesion, else the image shown in the Red view, else the first image;
    never a figure, a statistic map or a network map. The lesion is the first segmentation with a label map (its
    segments, at most 8). ``source.key`` changes when the images, the lesion or the window/level change, so that a save
    that changed nothing visible makes no new thumbnail.
    """
    import vtk

    import slicer

    scene = scene or slicer.mrmlScene
    segmentations = [node for node in slicer.util.getNodesByClass("vtkMRMLSegmentationNode", scene) if _segmentationLayers(node)]
    segmentationNode = segmentations[0] if segmentations else None
    background = None
    if segmentationNode is not None:
        reference = segmentationNode.GetNodeReference(slicer.vtkMRMLSegmentationNode.GetReferenceImageGeometryReferenceRole())
        background = reference if _isBackgroundCandidate(reference) else None
    if background is None:
        composite = next((node for node in slicer.util.getNodesByClass("vtkMRMLSliceCompositeNode", scene) if node.GetLayoutName() == "Red"), None)
        volumeId = composite.GetBackgroundVolumeID() if composite is not None else None
        candidate = scene.GetNodeByID(volumeId) if volumeId else None
        background = candidate if _isBackgroundCandidate(candidate) else None
    if background is None:
        background = next((node for node in slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode", scene) if _isBackgroundCandidate(node)), None)
    if background is None:
        return None
    source = ProjectThumbnailSource()
    array, offset = _imageArray(background.GetImageData(), source._keep)
    if array is None:
        return None
    ijkToRas = vtk.vtkMatrix4x4()
    background.GetIJKToRASMatrix(ijkToRas)
    source.background = array
    source.backgroundMatrix = _matrixArray(ijkToRas) @ _matrixArray(offset)
    displayNode = background.GetDisplayNode()
    if displayNode is not None and hasattr(displayNode, "GetWindow") and displayNode.GetWindow() > 0:
        source.window, source.level = float(displayNode.GetWindow()), float(displayNode.GetLevel())
    imageData = background.GetImageData()
    scalars = imageData.GetPointData().GetScalars()
    keyParts = [background.GetID(), imageData.GetMTime(), scalars.GetMTime() if scalars is not None else 0, source.window, source.level]
    for representation, labelValue in _segmentationLayers(segmentationNode):
        layerArray, layerOffset = _imageArray(representation, source._keep)
        if layerArray is None:
            continue
        imageToWorld = vtk.vtkMatrix4x4()
        representation.GetImageToWorldMatrix(imageToWorld)
        source.lesions.append((layerArray, _matrixArray(imageToWorld) @ _matrixArray(layerOffset), int(labelValue)))
        layerScalars = representation.GetPointData().GetScalars()
        keyParts += [segmentationNode.GetID(), representation.GetMTime(), layerScalars.GetMTime(), int(labelValue)]
    source.key = tuple(keyParts)
    return source


def _lesionCentroid(lesions):
    """RAS centroid of the lesion voxels (pure numpy, in slabs), or None when the lesion is empty."""
    total = 0
    weighted = np.zeros(3)
    for array, matrix, labelValue in lesions:
        slices = array.shape[0]
        sums = np.zeros(3)
        count = 0
        for k0 in range(0, slices, _CENTROID_SLAB):
            mask = array[k0:k0 + _CENTROID_SLAB] == labelValue
            n = int(np.count_nonzero(mask))
            if not n:
                continue
            kk, jj, ii = np.nonzero(mask)
            sums += (ii.sum(), jj.sum(), (kk + k0).sum())
            count += n
        if count:
            ras = matrix @ np.append(sums / count, 1.0)
            weighted += ras[:3] * count
            total += count
    return weighted / total if total else None


def _volumeBox(shape, matrix):
    """RAS bounding box (min, max) of an array [k, j, i] with its index-to-RAS ``matrix`` (voxel edges included)."""
    nk, nj, ni = shape
    corners = np.array([[i, j, k, 1.0] for i in (-0.5, ni - 0.5) for j in (-0.5, nj - 0.5) for k in (-0.5, nk - 0.5)])
    world = (matrix @ corners.T).T[:, :3]
    return world.min(axis=0), world.max(axis=0)


def _sample(array, matrix, points):
    """Nearest-neighbour values of ``array`` at RAS ``points`` [n, 3], and the mask of the points inside it."""
    rasToIndex = np.linalg.inv(matrix)
    index = points @ rasToIndex[:3, :3].T + rasToIndex[:3, 3]
    index = np.rint(index).astype(np.int64)
    nk, nj, ni = array.shape
    inside = (index[:, 0] >= 0) & (index[:, 0] < ni) & (index[:, 1] >= 0) & (index[:, 1] < nj) & (index[:, 2] >= 0) & (index[:, 2] < nk)
    values = np.zeros(len(points), dtype=float)
    if inside.any():
        values[inside] = array[index[inside, 2], index[inside, 1], index[inside, 0]]
    return values, inside


def _greyPlane(source, origin, right, down, spacing, width, height):
    """Grey levels 0..1 [height * width] of the background on a plane, the RAS points and the mask of points in the image."""
    columns = (np.arange(width) - (width - 1) / 2.0) * spacing
    rows = (np.arange(height) - (height - 1) / 2.0) * spacing
    points = (origin + rows[:, None, None] * down + columns[None, :, None] * right).reshape(-1, 3)
    values, inside = _sample(source.background, source.backgroundMatrix, points)
    if source.window:
        grey = np.clip((values - (source.level - source.window / 2.0)) / source.window, 0.0, 1.0)
    else:
        positive = values[inside]
        top = float(np.percentile(positive, 99.5)) if positive.size else 0.0
        bottom = float(np.percentile(positive, 1.0)) if positive.size else 0.0
        grey = np.clip((values - bottom) / (top - bottom), 0.0, 1.0) if top > bottom else np.zeros_like(values)
    return np.where(inside, grey, 0.0), points, inside


def renderProjectView(source, view, size, centre):
    """RGB uint8 image [row, column, 3] of one view (``"axial"``, ``"coronal"`` or ``"sagittal"``) through ``centre`` (RAS),
    zoomed to what the slice shows (the head, not the air around it). Worker-safe: numpy only.
    """
    width, height = size
    right, down, normal = _VIEW_AXES[view]
    right, down = np.array(right), np.array(down)
    low, high = _volumeBox(source.background.shape, source.backgroundMatrix)
    origin = (low + high) / 2.0
    origin[normal] = centre[normal]
    spacing = max(float(np.abs(right) @ (high - low)) / width, float(np.abs(down) @ (high - low)) / height)
    # A coarse pass finds the content of the slice (grey above CONTENT_LEVEL), then the view is fitted to it
    coarse = 4
    grey, _points, _inside = _greyPlane(source, origin, right, down, spacing * coarse, width // coarse, height // coarse)
    content = grey.reshape(height // coarse, width // coarse) > PROJECT_CONTENT_LEVEL
    if content.any():
        rows, columns = np.nonzero(content)
        rowLow, rowHigh = rows.min() - 0.5, rows.max() + 0.5
        columnLow, columnHigh = columns.min() - 0.5, columns.max() + 0.5
        step = spacing * coarse
        shiftRight = ((columnLow + columnHigh) / 2.0 - (width // coarse - 1) / 2.0) * step
        shiftDown = ((rowLow + rowHigh) / 2.0 - (height // coarse - 1) / 2.0) * step
        origin = origin + shiftRight * right + shiftDown * down
        spacing = max((columnHigh - columnLow) * step / width, (rowHigh - rowLow) * step / height)
    spacing *= 1.0 + 2 * PROJECT_MARGIN
    grey, points, inside = _greyPlane(source, origin, right, down, spacing, width, height)
    grey = (grey * 255.0).reshape(height, width)
    rgb = np.repeat(grey[:, :, None], 3, axis=2)
    mask = np.zeros(height * width, dtype=bool)
    for array, matrix, labelValue in source.lesions:
        labels, labelInside = _sample(array, matrix, points)
        mask |= labelInside & (labels == labelValue)
    if mask.any():
        mask = mask.reshape(height, width)
        colour = np.array(PROJECT_LESION_RGB, dtype=float)
        rgb[mask] = rgb[mask] * (1.0 - PROJECT_LESION_ALPHA) + colour * PROJECT_LESION_ALPHA
        # The outline: lesion pixels with a 4-neighbour outside the lesion
        padded = np.pad(mask, 1)
        interior = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
        rgb[mask & ~interior] = colour
    return np.clip(rgb, 0, 255).astype(np.uint8)


def renderProjectViews(source, privateFolder):
    """Render the three views of a project into ``privateFolder`` (``<project>/.studio``), atomically. Worker-safe: numpy,
    zlib and files only. Nothing is written when the project folder is gone (renamed or moved meanwhile).
    Return the paths written.
    """
    projectFolder = os.path.dirname(os.path.abspath(privateFolder))
    if not os.path.isdir(projectFolder):
        return []
    centre = _lesionCentroid(source.lesions)
    if centre is None:
        low, high = _volumeBox(source.background.shape, source.backgroundMatrix)
        centre = (low + high) / 2.0
    written = []
    for view, fileName in PROJECT_VIEW_FILES:
        size = PROJECT_THUMB_SIZE if view == "axial" else PROJECT_SIDE_SIZE
        rgb = renderProjectView(source, view, size, centre)
        rgba = np.concatenate([rgb, np.full(rgb.shape[:2] + (1,), 255, dtype=np.uint8)], axis=2)
        path = os.path.join(privateFolder, fileName)
        _writeAtomic(path, encodePng(rgba))
        written.append(path)
    return written

