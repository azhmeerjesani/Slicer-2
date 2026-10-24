"""Reference-map library and comparison matrix (SR-17, DE-20; addendum §3.6, §3.7 and §4.4).

Researchers check whether the network found for one condition repeats across conditions (van den Heuvel et al.
2026b): if a clinically unrelated condition gives nearly the same map, the map follows the connectome rather than the
disease. This module keeps a local library of reference maps the user chose to fetch, puts them on the connectome grid
and compares any set of maps with honest statistics.

Library
-------

The registry lives in ``<Studio/LNM/DataDirectory>/ReferenceMaps`` (:func:`registry_root`)::

    ReferenceMaps/
        refmaps.json   {"format": "slicer-studio-refmaps", "version": 1, "entries": [entry, ...]}
        manifest.json  the fetch cache manifest (sha256 recorded at the first download)
        files/<key>/   the downloaded or copied files, and resampled/<grid key>.npy caches

An entry: ``{id, title, source: {type: neurovault | url | file | vdh, ...}, licence, licence_note, badge, citation,
sha256, space, family, condition, kind, file, added, redistribute: false}``. ``file`` is relative to the registry
folder ("/" separators), so the folder can be moved; no entry holds an absolute path.

- :func:`add_neurovault` reads the public NeuroVault API (image id, or every image of a collection) when the user
  clicks; NeuroVault shares public images under CC0, which the entry states.
- :func:`add_url` and :func:`add_file` add any NIfTI map with the licence the user states (default "unknown", grey).
- :func:`add_vdh` fetches a file of the van den Heuvel 2026 repository at the pinned commit ``ca410e6`` (the voxel
  degree map by default), verified against its git blob id. The repository has no LICENSE, so its files are kept on
  this computer, shown with their attribution and never redistributed (``redistribute: false``; nothing in this module
  copies them into an export). The degree map is GSP-derived: the caller shows the live GSP data use terms and passes
  the sha256 of the accepted text.

Nothing is downloaded unless one of the ``add_*`` functions is called (a user action); nothing is bundled.

Comparison
----------

- :func:`resample_to` puts an entry's map on the connectome voxels (``geometry.resample_to_grid``, fraction mode, via
  ``reference.external_on_grid``): NaN where the map covers less than half of a voxel.
- :func:`compare` gives the matrices of Pearson r, Spearman rho and partial r given [μC_r, PC1] (``similarity``;
  addendum §3.7) of any maps on the same voxels.
- :func:`permutation_p` gives a p-value for a pair of maps only when **both** are built from in-study raw inputs
  (per-lesion maps with scores or group labels, :class:`InStudyMap`): the scores or labels are reshuffled and both
  maps rebuilt. External maps (references, μC, PC1, maps of other studies) get r without p: their raw inputs are not
  available, a parametric p ignores spatial autocorrelation, and spin or BrainSMASH nulls are not used for volumetric
  maps (addendum §3.6).
- :func:`top_similar` ranks the maps most similar to the current one, with a note when a clinically unrelated condition
  exceeds |r| = 0.6 (:data:`NOTE_THRESHOLD`).
- :func:`write_outputs` writes the CSV matrices, a JSON record without local paths and the heat-map cross-correlogram
  (``figures.write_figure``: lower triangle Pearson r, upper triangle partial r).

Command line (downloads run as a light job of the Map module)::

    python -m LesionNetworkMappingLib.refmaps list [--root DIR]
    python -m LesionNetworkMappingLib.refmaps add-neurovault (--image ID | --collection ID) [--condition TEXT] [--root DIR]
    python -m LesionNetworkMappingLib.refmaps add-url --url URL [--title T] [--licence L] [--citation C] [--condition TEXT]
    python -m LesionNetworkMappingLib.refmaps add-vdh --accepted-terms-sha256 SHA [--entry degree] [--commit ca410e6]
    python -m LesionNetworkMappingLib.refmaps remove --id ID

numpy only (Pillow for the figure, requests for downloads); no Slicer, VTK or Qt.
"""

import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sys
from typing import NamedTuple

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])

import numpy as np

from . import jobs

FORMAT = "slicer-studio-refmaps"
VERSION = 1
REGISTRY_DIR = "ReferenceMaps"
REGISTRY_JSON = "refmaps.json"
FILES_DIR = "files"
RESAMPLED_DIR = "resampled"
#: Environment variable with the registry folder (<DataDirectory>/ReferenceMaps), read when no root is given
ENV_ROOT = "SLICER_STUDIO_REFMAPS"
SOURCE_TYPES = ("neurovault", "url", "file", "vdh")

COMPARE_FORMAT = "slicer-lnm-compare"
COMPARE_VERSION = 1
COMPARE_DIR = "compare"
FIGURE_NAME = "compare-matrix"

#: A clinically unrelated condition above this |r| gets the cross-condition note
NOTE_THRESHOLD = 0.6
TOP_K = 10
#: Covariates of the partial r (addendum §3.7)
PARTIAL_COVARIATES = ("mu_r", "pc1")
MIN_COVERAGE = 0.5
DEFAULT_PERMUTATIONS = 1000
#: Voxels of the permutation test: a fixed seeded sample (the observed and every null r use the same voxels)
PERMUTATION_MAX_VOXELS = 50_000
PERMUTATION_CHUNK = 64

NEUROVAULT_API = "https://neurovault.org/api"
NEUROVAULT_SITE = "https://neurovault.org"
NEUROVAULT_LICENCE = "CC0-1.0"
NEUROVAULT_LICENCE_NOTE = "NeuroVault shares public images under CC0 (neurovault.org terms); cite the collection's paper"
NEUROVAULT_MAX_COLLECTION = 100

VDH_REPOSITORY = "https://github.com/dutchconnectomelab/lesionnetworkmapping"
VDH_COMMIT = "ca410e6"
VDH_ATTRIBUTION = ("van den Heuvel MP et al. Lesion network mapping: a critical appraisal. Nat Neurosci (2026). doi:10.1038/s41593-025-02196-7; "
                   "data and code: https://github.com/dutchconnectomelab/lesionnetworkmapping (commit ca410e6)")
VDH_NOTE = ("The van den Heuvel repository states no licence: the file is downloaded for your own use on this computer, shown with its "
            "attribution and never redistributed (not copied into exports or shared folders).")
VDH_DEFAULT_ENTRY = "degree"

CROSS_CONDITION_NOTE = ("A map that also matches clinically unrelated conditions is more likely to reflect the connectome's own structure "
                        "(hubs, mean connectivity) than a condition-specific network (van den Heuvel et al. 2026b). Partial r given "
                        "μC_r and PC1 shows the similarity left after these connectome axes.")
NO_P_EXTERNAL = ("r only: a p-value needs the raw inputs of both maps (per-lesion maps with labels or scores from this study). "
                 "Spatial autocorrelation invalidates parametric p, and spin or BrainSMASH nulls are not used for volumetric maps.")
NO_P_MEAN = "r only: a group mean map has no labels or scores to reshuffle (test group differences in the Contrast tab)"

_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


class RefmapsError(ValueError):
    """A registry or comparison error with a user-facing message."""


class NotInStudyError(RefmapsError):
    """A permutation p was asked for a pair that does not come from in-study raw inputs."""


# Registry location and file


def default_data_directory():
    """The default ``Studio/LNM/DataDirectory`` without Qt (as the control-set registry)."""
    from .controlsets import default_data_directory as default

    return default()


def registry_root(root=None, data_directory=None):
    """The registry folder: ``root``, else <data_directory>/ReferenceMaps, else ``$SLICER_STUDIO_REFMAPS``, else the default."""
    if root:
        return os.path.abspath(os.fspath(root))
    if data_directory:
        return os.path.join(os.path.abspath(os.fspath(data_directory)), REGISTRY_DIR)
    value = os.environ.get(ENV_ROOT, "").strip()
    if value:
        return os.path.abspath(value)
    return os.path.join(default_data_directory(), REGISTRY_DIR)


def _now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _sha256(path, block=1 << 20):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(text, limit=48):
    return (_ID_PATTERN.sub("-", str(text or "")).strip("-.") or "map")[:limit]


def _empty():
    return {"format": FORMAT, "version": VERSION, "entries": []}


def load_registry(root=None):
    """The registry dictionary (an empty one when the file does not exist)."""
    path = os.path.join(registry_root(root), REGISTRY_JSON)
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise RefmapsError(f"The reference-map registry cannot be read: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("entries"), list):
        raise RefmapsError(f"Not a {FORMAT} registry: {path}")
    if int(data.get("version", 0)) > VERSION:
        raise RefmapsError(f"The reference-map registry is version {data.get('version')}, newer than supported ({VERSION})")
    return data


def save_registry(data, root=None):
    folder = registry_root(root)
    os.makedirs(folder, exist_ok=True)
    jobs.write_json_atomic(os.path.join(folder, REGISTRY_JSON), data)
    return os.path.join(folder, REGISTRY_JSON)


def entries(root=None):
    """The registry entries (dictionaries, in the order they were added)."""
    return list(load_registry(root)["entries"])


def get_entry(entry_id, root=None):
    for entry in entries(root):
        if entry.get("id") == entry_id:
            return entry
    raise RefmapsError(f"No reference map {entry_id!r} in the library")


def entry_path(entry, root=None):
    """Absolute path of an entry's file (its ``file`` is relative to the registry folder)."""
    rel = str(entry.get("file") or "")
    if not rel or os.path.isabs(rel) or ".." in rel.split("/"):
        raise RefmapsError(f"The reference map {entry.get('id')!r} has no valid file")
    return os.path.join(registry_root(root), *rel.split("/"))


def _rel(root, path):
    return os.path.relpath(path, registry_root(root)).replace(os.sep, "/")


def _unique_id(base, existing):
    candidate, suffix = base, 2
    while candidate in existing:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _register(entry, root=None, replace=True):
    """Add (or replace, by id) an entry; return it."""
    from .fetch import badge_for

    data = load_registry(root)
    entry = dict(entry)
    entry.setdefault("badge", badge_for(entry.get("licence")))
    entry.setdefault("redistribute", entry.get("source", {}).get("type") != "vdh" and entry.get("badge") == "green")
    entry.setdefault("added", _now())
    kept = [item for item in data["entries"] if item.get("id") != entry["id"]]
    if not replace and len(kept) != len(data["entries"]):
        raise RefmapsError(f"A reference map {entry['id']!r} is already in the library")
    kept.append(entry)
    data["entries"] = kept
    save_registry(data, root)
    return entry


def remove_entry(entry_id, root=None, delete_files=True):
    """Remove an entry and its files: its resampled caches and its copied or downloaded file (with the download's
    record in the fetch manifest, so adding the same URL again downloads it again), unless another entry uses the
    same file folder. Return the removed entry.
    """
    data = load_registry(root)
    entry = next((item for item in data["entries"] if item.get("id") == entry_id), None)
    if entry is None:
        raise RefmapsError(f"No reference map {entry_id!r} in the library")
    data["entries"] = [item for item in data["entries"] if item.get("id") != entry_id]
    save_registry(data, root)
    if delete_files:
        shutil.rmtree(_cache_dir(entry, root), ignore_errors=True)
        _remove_entry_folder(entry, data["entries"], root)
    return entry


def _file_folder(entry):
    """The registry-relative folder of an entry's file ("files/<name>"), or None when the file is not in one."""
    parts = str(entry.get("file") or "").split("/")
    if len(parts) < 3 or parts[0] != FILES_DIR or parts[1] in ("", ".", "..", RESAMPLED_DIR):
        return None
    return "/".join(parts[:2])


def _remove_entry_folder(entry, remaining, root):
    from . import fetch

    folder = _file_folder(entry)
    if folder is None or any(_file_folder(item) == folder for item in remaining):
        return
    cache = registry_root(root)
    shutil.rmtree(os.path.join(cache, *folder.split("/")), ignore_errors=True)
    if entry.get("source", {}).get("type") == "file":
        return
    for relpath in [key for key in fetch.read_manifest(cache)["files"] if key.startswith(folder + "/")]:
        fetch._drop_record(cache, relpath)


def _image_info(path):
    """(space label or None, family) of a NIfTI file from its header (a standard template grid, else "unknown")."""
    from .geometry import detect_space, space_family

    try:
        found = detect_space(path)
    except Exception:
        found = None
    space = found[0] if found else None
    return space, space_family(space) if space else "unknown"


def _base_entry(path, root, **fields):
    space, family = _image_info(path)
    entry = {"sha256": _sha256(path), "space": fields.pop("space", None) or space, "family": family, "file": _rel(root, path)}
    entry.update(fields)
    if entry["space"] and entry["family"] == "unknown":
        from .geometry import space_family

        entry["family"] = space_family(entry["space"])
    return entry


# Adding maps (each is a user action; nothing runs on its own)


def add_file(path, title=None, licence="unknown", citation="", condition=None, space=None, kind="map", entry_id=None, root=None, licence_note=""):
    """Copy a NIfTI map into the library and register it; return the entry."""
    path = os.fspath(path)
    if not os.path.isfile(path):
        raise RefmapsError(f"No such file: {path}")
    name = os.path.basename(path)
    stem = re.sub(r"\.nii(\.gz)?$", "", name)
    existing = {item.get("id") for item in entries(root)}
    entry_id = entry_id or _unique_id(f"file-{_slug(stem, 32)}-{_sha256(path)[:8]}", existing)
    folder = os.path.join(registry_root(root), FILES_DIR, _slug(entry_id, 64))
    os.makedirs(folder, exist_ok=True)
    target = os.path.join(folder, name)
    if os.path.abspath(target) != os.path.abspath(path):
        shutil.copyfile(path, target + ".tmp")
        os.replace(target + ".tmp", target)
    entry = _base_entry(target, root, id=entry_id, title=title or stem, source={"type": "file", "name": name}, licence=licence or "unknown",
                        licence_note=licence_note, citation=citation or "", condition=condition or None, kind=kind, space=space)
    return _register(entry, root)


def _fetch_into_registry(resource, root, session=None, progress=None, cancel=None, offline=None):
    from . import fetch

    try:
        return fetch.fetch(resource, cache=registry_root(root), progress=progress, cancel=cancel, offline=offline, session=session)
    except fetch.FetchError as exc:
        raise RefmapsError(f"{resource.title}: {exc.message}") from None


def add_url(url, title=None, licence="unknown", citation="", condition=None, space=None, kind="map", entry_id=None, root=None, session=None,
            progress=None, cancel=None, offline=None, licence_note=""):
    """Download a NIfTI map from a URL (the user's click) and register it; return the entry."""
    from . import fetch

    url = str(url or "").strip()
    if not re.match(r"^https?://", url):
        raise RefmapsError("Give an http(s) URL of a NIfTI map")
    filename = os.path.basename(url.split("?")[0]) or "map.nii.gz"
    if not re.search(r"\.nii(\.gz)?$", filename):
        filename += ".nii.gz"
    resource = fetch.Resource({"id": f"refmap-url/{url}", "title": title or filename, "urls": [url], "filename": filename,
                               "checksum": fetch.RECORD_ON_FIRST_DOWNLOAD, "licence": licence or "unknown"})
    path = _fetch_into_registry(resource, root, session, progress, cancel, offline)
    existing = {item.get("id") for item in entries(root)}
    entry_id = entry_id or _unique_id(f"url-{_slug(re.sub(r'.nii(.gz)?$', '', filename), 32)}-{hashlib.sha256(url.encode()).hexdigest()[:8]}", existing)
    entry = _base_entry(path, root, id=entry_id, title=title or filename, source={"type": "url", "url": url}, licence=licence or "unknown",
                        licence_note=licence_note, citation=citation or "", condition=condition or None, kind=kind, space=space)
    return _register(entry, root)


def _https(url):
    return re.sub(r"^http://", "https://", str(url or ""))


def neurovault_image(image_id, session=None, timeout=30.0):
    """The public NeuroVault metadata of an image (a dict), read from the API."""
    from .fetch import make_session

    session = session or make_session()
    try:
        response = session.get(f"{NEUROVAULT_API}/images/{int(image_id)}/", params={"format": "json"}, timeout=(10.0, timeout))
        response.raise_for_status()
        return response.json()
    except (ValueError, TypeError) as exc:
        raise RefmapsError(f"NeuroVault image {image_id}: {exc}") from None
    except Exception as exc:
        raise RefmapsError(f"NeuroVault image {image_id} could not be read: {exc}") from None


def neurovault_collection(collection_id, session=None, timeout=30.0):
    """The public NeuroVault metadata of a collection (a dict, with ``images``: the image records, at most 100)."""
    from .fetch import make_session

    session = session or make_session()
    try:
        response = session.get(f"{NEUROVAULT_API}/collections/{int(collection_id)}/", params={"format": "json"}, timeout=(10.0, timeout))
        response.raise_for_status()
        collection = response.json()
        response = session.get(f"{NEUROVAULT_API}/collections/{int(collection_id)}/images/", params={"format": "json", "limit": NEUROVAULT_MAX_COLLECTION}, timeout=(10.0, timeout))
        response.raise_for_status()
        images = response.json()
    except (ValueError, TypeError) as exc:
        raise RefmapsError(f"NeuroVault collection {collection_id}: {exc}") from None
    except Exception as exc:
        raise RefmapsError(f"NeuroVault collection {collection_id} could not be read: {exc}") from None
    collection["images"] = images.get("results", images) if isinstance(images, dict) else images
    return collection


def _neurovault_citation(collection):
    parts = [str(collection.get(key) or "").strip() for key in ("authors", "name")]
    text = ". ".join(part for part in parts if part)
    if collection.get("DOI"):
        text += f". doi:{collection['DOI']}"
    url = collection.get("url") or (f"{NEUROVAULT_SITE}/collections/{collection.get('id')}/" if collection.get("id") else "")
    return (text + (f" ({_https(url)})" if url else "")).strip(". ")


def _known_space(label):
    from .geometry import space_family

    return space_family(label) not in ("unknown", "native")


def add_neurovault(image_id=None, collection_id=None, condition=None, root=None, session=None, progress=None, cancel=None, offline=None):
    """Download NeuroVault image(s) through the public API (on the user's click); return the new entries.

    Give ``image_id`` (one image) or ``collection_id`` (its images, at most 100). The licence is NeuroVault's CC0.
    """
    from . import fetch

    if (image_id is None) == (collection_id is None):
        raise RefmapsError("Give a NeuroVault image id or a collection id")
    session = session or fetch.make_session()
    if image_id is not None:
        images = [neurovault_image(image_id, session)]
        collection_id = images[0].get("collection_id")
        try:
            collection = neurovault_collection(collection_id, session) if collection_id else {}
        except RefmapsError:
            collection = {}
    else:
        collection = neurovault_collection(collection_id, session)
        images = list(collection.get("images") or [])
    citation = _neurovault_citation(collection) if collection else ""
    out = []
    for index, image in enumerate(images):
        fetch._raise_if_cancelled(cancel, index)
        url = _https(image.get("file"))
        if not url or not re.search(r"\.nii(\.gz)?$", url.split("?")[0]):
            continue
        nv_id = str(image.get("id"))
        resource = fetch.Resource({"id": f"neurovault/{nv_id}", "title": image.get("name") or f"NeuroVault image {nv_id}", "urls": [url],
                                   "filename": os.path.basename(url.split("?")[0]), "size": image.get("file_size"),
                                   "checksum": fetch.RECORD_ON_FIRST_DOWNLOAD, "licence": NEUROVAULT_LICENCE})
        path = _fetch_into_registry(resource, root, session, progress, cancel, offline)
        template = str(image.get("target_template_image") or "")
        entry = _base_entry(path, root, id=f"nv-{nv_id}", title=image.get("name") or f"NeuroVault image {nv_id}",
                            source={"type": "neurovault", "image_id": nv_id, "collection_id": str(image.get("collection_id") or collection_id or ""),
                                    "url": f"{NEUROVAULT_SITE}/images/{nv_id}/", "map_type": image.get("map_type"), "template": template or None},
                            licence=NEUROVAULT_LICENCE, licence_note=NEUROVAULT_LICENCE_NOTE, citation=citation, condition=condition or None,
                            kind="map", space=template if _known_space(template) else None)
        out.append(_register(entry, root))
    if not out:
        raise RefmapsError("No volumetric NIfTI image was found")
    return out


def vdh_catalog(path=None):
    """The van den Heuvel repository files known to the app (the Lead-DBS parity catalog, pinned to one commit)."""
    from .leaddbs import load_parity_catalog

    return load_parity_catalog(path)


def add_vdh(commit=VDH_COMMIT, entry=VDH_DEFAULT_ENTRY, accepted_terms_sha256=None, condition=None, root=None, catalog_path=None, session=None,
            progress=None, cancel=None, offline=None):
    """Fetch a file of the van den Heuvel 2026 repository at the pinned commit (the user's click); return the entry.

    The file is verified against its git blob id. The repository has no LICENSE: the entry says so, carries the
    attribution and is never redistributed. GSP-derived files (``terms`` in the catalog, e.g. the degree map) need
    ``accepted_terms_sha256``, the sha256 of the live GSP data use terms text the user accepted.
    """
    from .leaddbs import git_blob_sha1

    catalog = vdh_catalog(catalog_path)
    source = catalog.data.get("source") or {}
    pinned = str(source.get("commit") or "")
    if not commit or not pinned.startswith(str(commit)):
        raise RefmapsError(f"Only commit {pinned[:7] or VDH_COMMIT} of the van den Heuvel repository is pinned (asked: {commit})")
    resource = catalog.get(entry)
    terms = resource.get("terms")
    if terms and not accepted_terms_sha256:
        raise RefmapsError(f"{resource.title} is GSP-derived: accept the GSP data use terms ({terms}) first")
    path = _fetch_into_registry(resource, root, session, progress, cancel, offline)
    expected = resource.get("git_blob")
    if expected and git_blob_sha1(path) != expected:
        raise RefmapsError(f"{resource.title} does not match the pinned repository file (git blob {expected[:12]})")
    licence_note = " ".join(text for text in (resource.get("licence_note") or "", VDH_NOTE) if text)
    kind = "degree" if resource.kind == "degree" else "map"
    record = _base_entry(path, root, id=f"vdh-{_slug(entry.replace('/', '-'), 48)}", title=resource.title,
                         source={"type": "vdh", "repository": source.get("repository") or VDH_REPOSITORY, "commit": pinned[:7],
                                 "repo_path": resource.get("repo_path"), "catalog_id": resource.id, "git_blob": expected},
                         licence=resource.get("licence") or "not-stated", licence_note=licence_note,
                         citation=resource.get("citation") or VDH_ATTRIBUTION, attribution=VDH_ATTRIBUTION,
                         condition=condition or ("connectome degree" if kind == "degree" else None), kind=kind, space=None,
                         terms=terms, accepted_terms_sha256=accepted_terms_sha256 or None, redistribute=False)
    return _register(record, root)


# Resampling


def _grid_key(bundle, min_coverage):
    digest = hashlib.sha256()
    digest.update(json.dumps([list(bundle.grid.shape), np.asarray(bundle.grid.affine, dtype=np.float64).round(6).tolist(), float(min_coverage)]).encode())
    digest.update(np.ascontiguousarray(np.asarray(bundle.mask_indices, dtype=np.int64)).tobytes())
    return digest.hexdigest()[:16]


def _cache_dir(entry, root):
    return os.path.join(registry_root(root), FILES_DIR, RESAMPLED_DIR, _slug(entry.get("id"), 64))


def map_on_grid(path, bundle, min_coverage=MIN_COVERAGE):
    """A NIfTI map on the bundle voxels ((V,) float64): its values when it is on the bundle grid, else resampled with
    ``geometry.resample_to_grid`` in fraction mode over its finite non-zero voxels (as ``reference.external_on_grid``),
    NaN where less than ``min_coverage`` of a voxel is covered. ``bundle`` needs ``grid``, ``mask_indices`` and ``n_voxels``.
    """
    from .geometry import resample_to_grid, to_masked_vector
    from .nifti import read_nifti

    data, affine, _ = read_nifti(os.fspath(path))
    data = np.asarray(data, dtype=np.float64)
    if data.ndim > 3:
        if int(np.prod(data.shape[3:])) != 1:
            raise RefmapsError(f"{os.path.basename(os.fspath(path))} has {int(np.prod(data.shape[3:]))} volumes; give a single 3D map")
        data = data.reshape(data.shape[:3])
    grid = bundle.grid
    indices = np.asarray(bundle.mask_indices)
    if tuple(data.shape) == tuple(grid.shape) and np.allclose(affine, grid.affine, atol=1e-4, rtol=0):
        out = data.reshape(-1)[indices]
        return np.where(np.isfinite(out), out, np.nan)
    inside = np.isfinite(data) & (data != 0)
    total = to_masked_vector(resample_to_grid(np.where(inside, data, 0.0), affine, grid, mode="fraction"), indices).astype(np.float64)
    coverage = to_masked_vector(resample_to_grid(inside.astype(np.float64), affine, grid, mode="fraction"), indices).astype(np.float64)
    out = np.full(indices.size, np.nan)
    covered = coverage >= min_coverage
    out[covered] = total[covered] / coverage[covered]
    return out


def resample_to(entry, bundle, root=None, min_coverage=MIN_COVERAGE, cache=True):
    """An entry's map on the connectome voxels ((V,) float64, NaN where not covered), cached per grid and mask."""
    if isinstance(entry, str):
        entry = get_entry(entry, root)
    path = entry_path(entry, root)
    if not os.path.isfile(path):
        raise RefmapsError(f"The file of {entry.get('title') or entry.get('id')} is missing; add it again")
    cache_file = os.path.join(_cache_dir(entry, root), f"{_grid_key(bundle, min_coverage)}-{str(entry.get('sha256') or '')[:12]}.npy")
    if cache and os.path.isfile(cache_file):
        try:
            values = np.load(cache_file, allow_pickle=False)
            if values.shape == (int(bundle.n_voxels),):
                return values.astype(np.float64)
        except (OSError, ValueError):
            pass
    values = map_on_grid(path, bundle, min_coverage).astype(np.float32).astype(np.float64)  # as read back from the cache
    if cache:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            with open(cache_file + ".tmp", "wb") as f:
                np.save(f, values.astype(np.float32))
            os.replace(cache_file + ".tmp", cache_file)
        except OSError:
            pass
    return values


def family_warning(entry, bundle_space):
    """A sentence when the entry's template family differs from the connectome's (resampled without a family transform)."""
    from .geometry import space_family

    mine, theirs = entry.get("family") or "unknown", space_family(bundle_space) if bundle_space else "unknown"
    if mine == "unknown" or theirs == "unknown" or mine == theirs:
        return ""
    return f"{entry.get('title')}: template family {mine} differs from the connectome's ({theirs}); resampled without a template-family transform (offsets of a few mm)."


# In-study maps built from raw inputs


class InStudyMap(NamedTuple):
    """A map built from in-study raw inputs: the per-lesion maps (rows of a raw (n, V) matrix) and a score or group labels.

    ``kind``: "score" (Pearson r over lesions of each voxel's value with the score, as sLNM), "contrast" (mean of
    the lesions labelled 1 minus the mean of those labelled 0) or "mean" (the mean of the rows; nothing to reshuffle).
    ``rows``: indices into the raw matrix; ``values``: the scores or 0/1 labels of those rows (None for "mean").
    """

    name: str
    kind: str
    rows: np.ndarray
    values: np.ndarray = None

    @property
    def permutable(self):
        return self.kind in ("score", "contrast")


def in_study_map(name, kind, rows, values=None):
    """An :class:`InStudyMap` with its rows and values checked (finite values; two groups for a contrast)."""
    if kind not in ("score", "contrast", "mean"):
        raise RefmapsError(f"Unknown in-study map kind {kind!r}")
    rows = np.asarray(rows, dtype=np.int64).ravel()
    if kind == "mean":
        return InStudyMap(name, kind, rows, None)
    values = np.asarray(values, dtype=np.float64).ravel()
    if values.shape != rows.shape:
        raise RefmapsError(f"{name}: {values.size} values for {rows.size} lesions")
    keep = np.isfinite(values)
    rows, values = rows[keep], values[keep]
    if kind == "contrast":
        values = (values != 0).astype(np.float64)
        if values.sum() < 2 or (values.size - values.sum()) < 2:
            raise RefmapsError(f"{name}: each group needs at least 2 lesions")
    elif rows.size < 4 or np.ptp(values) == 0:
        raise RefmapsError(f"{name}: a score map needs at least 4 lesions with different scores")
    return InStudyMap(name, kind, rows, values)


def _weights(spec, values):
    """(P, n) weights whose product with the raw rows gives the maps of ``spec`` for P value vectors (rows of ``values``)."""
    values = np.atleast_2d(values)
    if spec.kind == "contrast":
        ones = values.sum(axis=1, keepdims=True)
        zeros = values.shape[1] - ones
        return values / ones - (1.0 - values) / zeros
    centred = values - values.mean(axis=1, keepdims=True)
    return centred / np.sqrt((centred * centred).sum(axis=1, keepdims=True))


def _standardized(raw, rows, voxels=None):
    """Rows of the raw matrix (selected voxels), each voxel centred and scaled to unit norm over the lesions (for score maps)."""
    x = np.asarray(raw[rows] if voxels is None else raw[np.ix_(rows, voxels)], dtype=np.float64)
    x = x - x.mean(axis=0)
    norms = np.sqrt((x * x).sum(axis=0))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(norms > 0, x / np.where(norms > 0, norms, 1.0), np.nan)


def _maps(raw, spec, values, voxels=None, standardized=None):
    """(P, V') maps of ``spec`` for P value vectors."""
    if spec.kind == "mean":
        x = raw[spec.rows] if voxels is None else raw[np.ix_(spec.rows, voxels)]
        return np.asarray(x, dtype=np.float64).mean(axis=0)[np.newaxis]
    weights = _weights(spec, values)
    if spec.kind == "score":
        x = standardized if standardized is not None else _standardized(raw, spec.rows, voxels)
        return weights @ x
    x = raw[spec.rows] if voxels is None else raw[np.ix_(spec.rows, voxels)]
    return weights @ np.asarray(x, dtype=np.float64)


def build_in_study(raw, spec):
    """The (V,) map of an :class:`InStudyMap` from the raw (n, V) per-lesion maps."""
    return _maps(np.asarray(raw), spec, spec.values)[0]


# Comparison


class Comparison(NamedTuple):
    """Matrices of a comparison (k x k; NaN where undefined): ``pearson``, ``spearman`` and ``partial`` (None without
    covariates), ``n_voxels`` (voxels used per pair), with the map ``names`` and the ``covariates`` names.
    """

    names: list
    pearson: np.ndarray
    spearman: np.ndarray
    partial: np.ndarray
    n_voxels: np.ndarray
    covariates: tuple


def _covariate_rows(reference, covariate_keys, n_voxels):
    if reference is None or not covariate_keys:
        return None
    if isinstance(reference, np.ndarray):
        rows = np.atleast_2d(np.asarray(reference, dtype=np.float64))
    elif isinstance(reference, dict):
        rows = np.vstack([np.asarray(reference[key], dtype=np.float64) for key in covariate_keys])
    else:
        rows = reference.covariates(tuple(covariate_keys))
    if rows.shape[1] != n_voxels:
        raise RefmapsError(f"The covariate maps have {rows.shape[1]} voxels, the maps {n_voxels}")
    return rows


def _restrict(values, mask):
    if mask is None:
        return values
    mask = np.asarray(mask)
    return values[..., mask] if mask.dtype == bool else values[..., mask.astype(np.int64)]


def compare(maps, reference=None, mask=None, covariate_keys=PARTIAL_COVARIATES):
    """Pearson r, Spearman rho and partial r [μC_r, PC1] between every pair of maps.

    :param maps: {name: (V,) vector} (insertion order kept) on the same voxels; NaN marks voxels a map does not cover.
    :param reference: a ``reference.Reference``, a {key: (V,)} dict, a (k, V) array of covariates, or None (no partial r).
    :param mask: optional boolean (V,) mask or indices restricting the voxels.
    :return: :class:`Comparison`. Each pair uses the voxels finite in both maps (and in the covariates for partial r).
    """
    from . import similarity

    names = list(maps)
    if not names:
        raise RefmapsError("No maps to compare")
    matrix = np.vstack([np.asarray(maps[name], dtype=np.float64).ravel() for name in names])
    covariates = _covariate_rows(reference, covariate_keys, matrix.shape[1])
    matrix = _restrict(matrix, mask)
    if covariates is not None:
        covariates = _restrict(covariates, mask)
    k = len(names)
    pearson = np.eye(k)
    spearman = np.eye(k)
    counts = np.zeros((k, k), dtype=np.int64)
    finite = np.isfinite(matrix)
    full = finite.all(axis=1)
    ranks = [similarity.ranks(row) if full[index] else None for index, row in enumerate(matrix)]
    for i in range(k):
        counts[i, i] = int(finite[i].sum())
        if counts[i, i] < similarity.MIN_VOXELS or np.ptp(matrix[i][finite[i]]) == 0:
            pearson[i, i] = spearman[i, i] = np.nan
        for j in range(i + 1, k):
            if full[i] and full[j]:
                n = matrix.shape[1]
                r = similarity._pearson(matrix[i], matrix[j]) if n >= similarity.MIN_VOXELS else math.nan
                rho = similarity._pearson(ranks[i], ranks[j]) if n >= similarity.MIN_VOXELS else math.nan
            else:
                n = int((finite[i] & finite[j]).sum())
                r, rho = similarity.pearson_spearman(matrix[i], matrix[j])
            pearson[i, j] = pearson[j, i] = r
            spearman[i, j] = spearman[j, i] = rho
            counts[i, j] = counts[j, i] = n
    partial = None
    if covariates is not None:
        partial = _partial_matrix(matrix, covariates, full)
    return Comparison(names, pearson, spearman, partial, counts, tuple(covariate_keys) if covariates is not None else ())


def _partial_matrix(matrix, covariates, full):
    """Partial r of every pair (similarity.partial_spatial_correlation semantics); maps finite everywhere share one fit."""
    from . import similarity

    k = matrix.shape[0]
    out = np.full((k, k), np.nan)
    together = np.flatnonzero(full)
    residuals = {}
    if together.size:
        fitted, valid, n_columns = similarity._fit(matrix[together], covariates)
        if np.count_nonzero(valid) >= max(similarity.MIN_VOXELS, n_columns + 2):
            for row, index in enumerate(together):
                resid = fitted[row]
                total = similarity._sum_squares(matrix[index][valid])
                if resid @ resid > 1e-12 * max(total, np.finfo(float).tiny):
                    residuals[index] = resid
    for i in range(k):
        if i in residuals:
            out[i, i] = 1.0
        for j in range(i + 1, k):
            if full[i] and full[j]:
                value = similarity._pearson(residuals[i], residuals[j]) if i in residuals and j in residuals else math.nan
            else:
                value = similarity.partial_spatial_correlation(matrix[i], matrix[j], covariates)
            out[i, j] = out[j, i] = value
    return out


class PermutationResult(NamedTuple):
    """A permutation test of the spatial similarity of two in-study maps.

    ``r``: observed r on the test voxels; ``p``: two-sided, (1 + #(|r_null| >= |r|)) / (1 + n_perm); ``statistic``:
    "pearson" or "partial"; ``n_voxels``: test voxels (a seeded sample when the maps have more); ``null``: the null r;
    ``scheme``: "joint" (one permutation of the participants applies to both maps), "independent" (the maps share no
    participant) or "mixed" (shared participants permuted jointly, the others within their own map).
    """

    r: float
    p: float
    n_perm: int
    statistic: str
    n_voxels: int
    null: np.ndarray
    seed: int
    scheme: str = "joint"

    @property
    def min_p(self):
        return 1.0 / (self.n_perm + 1)


def p_reason(a, b):
    """Why a pair gets no permutation p ("" when it can): both must be permutable :class:`InStudyMap` objects."""
    for spec in (a, b):
        if not isinstance(spec, InStudyMap):
            return NO_P_EXTERNAL
    for spec in (a, b):
        if not spec.permutable:
            return NO_P_MEAN
    return ""


def _test_voxels(raw, a, b, covariates, max_voxels, seed):
    rows = np.union1d(a.rows, b.rows)
    valid = np.all(np.isfinite(raw[rows]), axis=0)
    if covariates is not None:
        valid &= np.all(np.isfinite(covariates), axis=0)
    for spec in (a, b):
        if spec.kind == "score":  # a voxel constant over the lesions has no correlation with the score
            values = raw[spec.rows]
            valid &= np.nanmax(values, axis=0) > np.nanmin(values, axis=0)
    voxels = np.flatnonzero(valid)
    if max_voxels and voxels.size > max_voxels:
        voxels = np.sort(np.random.default_rng(seed).choice(voxels, int(max_voxels), replace=False))
    return voxels


def _residualizer(covariates, voxels):
    """A function removing [1, covariates] (on ``voxels``) from the rows of a (P, V') matrix by orthogonal projection."""
    design = np.ones((1, voxels.size))
    if covariates is not None:
        design = np.vstack([design, covariates[:, voxels]])
    q, r = np.linalg.qr(design.T)
    keep = np.abs(np.diag(r)) > 1e-10 * max(np.abs(np.diag(r)).max(), 1e-300)
    q = q[:, keep]

    def apply(rows):
        return rows - (rows @ q) @ q.T

    return apply


def _row_r(a, b):
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.einsum("ij,ij->i", a, b) / np.sqrt(np.einsum("ij,ij->i", a, a) * np.einsum("ij,ij->i", b, b))


class _Permuter:
    """Draws permuted value vectors of two maps: one permutation of the participants they share is applied to both
    maps (keeping the correlation between their scores or labels, so the null is "no lesion-behaviour association"),
    and the participants of only one map are permuted within it.
    """

    def __init__(self, a, b):
        shared = np.intersect1d(a.rows, b.rows) if _unique(a.rows) and _unique(b.rows) else np.empty(0, dtype=np.int64)
        self.a_shared = _positions(a.rows, shared)
        self.b_shared = _positions(b.rows, shared)
        self.a_own = np.setdiff1d(np.arange(a.rows.size), self.a_shared)
        self.b_own = np.setdiff1d(np.arange(b.rows.size), self.b_shared)
        self.a_values, self.b_values = np.asarray(a.values, dtype=np.float64), np.asarray(b.values, dtype=np.float64)
        if not shared.size:
            self.scheme = "independent"
        elif not self.a_own.size and not self.b_own.size:
            self.scheme = "joint"
        else:
            self.scheme = "mixed"

    def draw(self, rng, size):
        values_a = np.empty((size, self.a_values.size))
        values_b = np.empty((size, self.b_values.size))
        for row in range(size):
            perm = rng.permutation(self.a_shared.size)
            values_a[row, self.a_shared] = self.a_values[self.a_shared[perm]]
            values_b[row, self.b_shared] = self.b_values[self.b_shared[perm]]
            values_a[row, self.a_own] = self.a_values[self.a_own[rng.permutation(self.a_own.size)]]
            values_b[row, self.b_own] = self.b_values[self.b_own[rng.permutation(self.b_own.size)]]
        return values_a, values_b


def _unique(rows):
    return np.unique(rows).size == np.asarray(rows).size


def _positions(rows, selected):
    """Positions in ``rows`` of the ``selected`` row indices (in the order of ``selected``)."""
    order = np.argsort(rows, kind="stable")
    return order[np.searchsorted(rows, selected, sorter=order)].astype(np.int64)


def permutation_p(raw, a, b, n_perm=DEFAULT_PERMUTATIONS, seed=0, covariates=None, max_voxels=PERMUTATION_MAX_VOXELS, chunk=PERMUTATION_CHUNK,
                  cancel=None, progress=None):
    """Permutation p of the spatial r between two maps built from in-study raw inputs.

    The participants are permuted: one permutation of the lesions both maps use reassigns the scores or labels of
    both maps together (two columns of one participants.tsv stay as correlated as they are), lesions used by only
    one map are permuted within it; both maps are rebuilt from the raw per-lesion maps and r recomputed on the same
    voxels. Only valid when both maps are
    :class:`InStudyMap` objects with scores or labels; anything else raises :class:`NotInStudyError` (external maps
    get r without p).

    :param raw: (n, V) per-lesion maps (the rows the specs index).
    :param covariates: optional (k, V) covariate maps (μC_r, PC1): the statistic is then the partial r.
    :param max_voxels: test voxels, a seeded sample when there are more (the observed r uses the same sample).
    :return: :class:`PermutationResult`.
    """
    reason = p_reason(a, b)
    if reason:
        raise NotInStudyError(reason)
    raw = np.asarray(raw)
    if raw.ndim != 2:
        raise RefmapsError("The raw inputs must be an (n lesions, V voxels) matrix")
    n_perm = int(n_perm)
    if n_perm < 1:
        raise RefmapsError("n_perm must be at least 1")
    cov = None if covariates is None else np.atleast_2d(np.asarray(covariates, dtype=np.float64))
    voxels = _test_voxels(raw, a, b, cov, max_voxels, seed)
    if voxels.size < 3 + (0 if cov is None else cov.shape[0]):
        raise RefmapsError("Too few voxels are finite in both maps")
    residualize = _residualizer(cov, voxels)
    std = {id(spec): (_standardized(raw, spec.rows, voxels) if spec.kind == "score" else None) for spec in (a, b)}
    observed = _row_r(residualize(_maps(raw, a, a.values, voxels, std[id(a)])), residualize(_maps(raw, b, b.values, voxels, std[id(b)])))[0]
    if not math.isfinite(observed):
        raise RefmapsError("The observed r is undefined (a constant map on the test voxels)")
    from .fetch import _raise_if_cancelled

    rng = np.random.default_rng(seed)
    permuter = _Permuter(a, b)
    null = np.empty(n_perm)
    done = 0
    while done < n_perm:
        _raise_if_cancelled(cancel, done)
        size = min(int(chunk), n_perm - done)
        values_a, values_b = permuter.draw(rng, size)
        maps_a = residualize(_maps(raw, a, values_a, voxels, std[id(a)]))
        maps_b = residualize(_maps(raw, b, values_b, voxels, std[id(b)]))
        null[done:done + size] = _row_r(maps_a, maps_b)
        done += size
        if progress is not None:
            progress(done, n_perm)
    exceed = int(np.count_nonzero(np.abs(np.nan_to_num(null, nan=0.0)) >= abs(observed) - 1e-12))
    return PermutationResult(float(observed), (1 + exceed) / (1 + n_perm), n_perm, "partial" if cov is not None else "pearson", int(voxels.size), null, int(seed),
                             permuter.scheme)


# Most similar maps and the cross-condition note


def normalize_condition(text):
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def unrelated(condition, other, related=()):
    """True when both conditions are known and differ (after normalization), and ``other`` is not listed as related."""
    mine, theirs = normalize_condition(condition), normalize_condition(other)
    if not mine or not theirs or mine == theirs:
        return False
    return theirs not in {normalize_condition(item) for item in related or ()}


def _note(row, condition, threshold):
    if row["kind"] in ("connectome", "degree") and math.isfinite(row["r"]) and abs(row["r"]) > threshold:
        return (f"|r| = {abs(row['r']):.2f} with {row['name']}: the map follows the connectome's own structure; "
                f"see the partial r given μC_r and PC1.")
    if row["unrelated"] and math.isfinite(row["r"]) and abs(row["r"]) > threshold:
        partial = row.get("partial_r")
        tail = f" (partial r given μC_r and PC1: {partial:.2f})" if partial is not None and math.isfinite(partial) else ""
        return (f"{row['name']} ({row['condition']}) correlates at r = {row['r']:.2f} with this {condition or 'study'} map{tail}: a clinically "
                f"unrelated condition this similar suggests a shared connectome-driven pattern rather than a condition-specific network.")
    return ""


def top_similar(current, candidates, k=TOP_K, condition=None, related=(), threshold=NOTE_THRESHOLD, reference=None, covariate_keys=PARTIAL_COVARIATES):
    """The ``k`` maps most similar to ``current`` (by |Pearson r|), each with a note when needed.

    :param current: (V,) map.
    :param candidates: [{"name", "values" (V,), "condition" (optional), "kind" (optional: "map", "connectome", "degree", ...)}].
    :param condition: the current map's condition; candidates of another (not ``related``) condition are "unrelated".
    :return: rows {name, r, rho, partial_r, condition, kind, unrelated, note} sorted by |r| (largest first).
    """
    from . import similarity

    current = np.asarray(current, dtype=np.float64).ravel()
    covariates = _covariate_rows(reference, covariate_keys, current.size)
    rows = []
    for item in candidates:
        values = np.asarray(item["values"], dtype=np.float64).ravel()
        r, rho = similarity.pearson_spearman(current, values)
        partial = similarity.partial_spatial_correlation(current, values, covariates) if covariates is not None else None
        row = {"name": item["name"], "r": r, "rho": rho, "partial_r": partial, "condition": item.get("condition"), "kind": item.get("kind", "map"),
               "unrelated": unrelated(condition, item.get("condition"), related)}
        row["note"] = _note(row, condition, threshold)
        rows.append(row)
    rows.sort(key=lambda row: -abs(row["r"]) if math.isfinite(row["r"]) else 1.0)
    return rows[: int(k)]


# Outputs


def _fmt(value, digits=3):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{value:.{digits}f}" if math.isfinite(value) else ""


def _csv_cell(text):
    text = str(text)
    return f'"{text.replace(chr(34), chr(34) * 2)}"' if any(c in text for c in ',"\n') else text


def matrix_csv(names, matrix):
    lines = [",".join(["map", *(_csv_cell(name) for name in names)])]
    for name, row in zip(names, matrix, strict=True):
        lines.append(",".join([_csv_cell(name), *(_fmt(value) for value in row)]))
    return "\n".join(lines) + "\n"


_STATISTIC_NAMES = {"partial": "partial r", "pearson": "Pearson r"}


def pairs_rows(comparison, tests=None, infos=None):
    """One row per pair: map_a, map_b, n_voxels, pearson_r, spearman_rho, partial_r, p_perm, n_perm, p_statistic
    (the permuted statistic), p_voxels (its test voxels), p_scheme (joint, independent or mixed), p_note.
    """
    tests = tests or {}
    infos = infos or {}
    rows = []
    names = comparison.names
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            test = tests.get((i, j)) or tests.get((j, i))
            note = p_reason(infos.get(names[i]), infos.get(names[j])) if test is None else ""
            if test is None and not note:
                note = "not tested (press Permutation p)"
            rows.append({"map_a": names[i], "map_b": names[j], "n_voxels": int(comparison.n_voxels[i, j]),
                         "pearson_r": _fmt(comparison.pearson[i, j]), "spearman_rho": _fmt(comparison.spearman[i, j]),
                         "partial_r": _fmt(comparison.partial[i, j]) if comparison.partial is not None else "",
                         "p_perm": _fmt(test.p, 4) if test is not None else "", "n_perm": test.n_perm if test is not None else "",
                         "p_statistic": _STATISTIC_NAMES.get(test.statistic, test.statistic) if test is not None else "",
                         "p_voxels": test.n_voxels if test is not None else "", "p_scheme": test.scheme if test is not None else "",
                         "p_note": note})
    return rows


def _rows_csv(rows):
    if not rows:
        return ""
    columns = list(rows[0])
    return "\n".join([",".join(columns), *(",".join(_csv_cell(row.get(column, "")) for column in columns) for row in rows)]) + "\n"


#: Symbols the figure text replaces: Pillow's default font and the application's Inter subset lack these glyphs
_FIGURE_ASCII = {"\u03bc": "mu", "\u00b5": "u", "\u03c1": "rho", "\u03b1": "alpha", "\u2212": "-", "\u2265": ">=", "\u2264": "<=",
                 "\u25cf": "", "\u25d0": "", "\u25cb": ""}


def _figure_fonts(font):
    """``figures.Fonts`` with Pillow's basic text layout (raqm's HarfBuzz clashes with Qt's inside the application and
    draws only the first glyph) and the symbols of :data:`_FIGURE_ASCII` replaced.
    """
    from . import figures

    class Fonts(figures.Fonts):
        def get(self, size, bold=False):
            from PIL import ImageFont

            size = max(round(size), 6)
            key = (size, bool(bold))
            if key not in self._cache:
                path = self.bold_path if bold else self.path
                if path:
                    self._cache[key] = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)
                else:
                    return super().get(size, bold)
            return self._cache[key]

        def text(self, text):
            for symbol, replacement in _FIGURE_ASCII.items():
                text = text.replace(symbol, replacement)
            return super().text(text)

    return Fonts(font)


def heatmap_figure(names, lower, upper=None, title="Map comparison", dpi=150, font=None, lower_label="Pearson r", upper_label="partial r | \u03bcC_r, PC1",
                   caption=None, sourcedata=None):
    """The heat-map cross-correlogram as a ``figures.Figure``: ``lower`` below the diagonal, ``upper`` above it (``lower``
    mirrored when None), on the LNM diverging colours from -1 to 1, with the values printed when there are at most 16 maps.
    Rows are labelled with the map names and numbered; the columns carry the numbers. Undefined cells (for example the
    partial r of a map that is itself a covariate) are left blank.
    """
    from PIL import Image

    from . import figures

    fonts = _figure_fonts(font)

    def text(value):
        return fonts.text(str(value)).strip()

    k = len(names)
    scale = dpi / 150.0
    cell = max(round((40 if k <= 8 else 30 if k <= 16 else 18) * scale), 8)
    text_px = 12 * scale
    small_px = 9.5 * scale
    labels = [f"{text(name)}  {index + 1}" for index, name in enumerate(names)]
    label_w = int(min(max((fonts.width(item, small_px) for item in labels), default=40) + 10 * scale, 260 * scale))
    margin = int(16 * scale)
    legend_y = margin + text_px + 14 * scale
    top = int(legend_y + 16 * scale + small_px)
    left = int(margin + label_w)
    bar_h = int(9 * scale)
    width = max(left + k * cell + margin, int(380 * scale))
    height = top + k * cell + int(58 * scale)
    lut = figures.diverging_lut()
    pixels = np.full((height, width, 3), 255, dtype=np.uint8)
    values = np.full((k, k), np.nan)
    for i in range(k):
        for j in range(k):
            if i > j:
                values[i, j] = lower[i, j]
            elif i < j:
                values[i, j] = (upper if upper is not None else lower)[i, j]
    gap = max(1, round(scale))
    for i in range(k):
        for j in range(k):
            y0, x0 = top + i * cell, left + j * cell
            if i == j:
                color = (236, 236, 236)
            elif not math.isfinite(values[i, j]):
                color = (248, 248, 248)
            else:
                index = int(np.clip(np.floor((values[i, j] + 1.0) / 2.0 * len(lut)), 0, len(lut) - 1))
                color = tuple(round(c * 255) for c in lut[index])
            pixels[y0 + gap:y0 + cell, x0 + gap:x0 + cell] = color
    base = Image.fromarray(pixels, "RGB")
    upper_text = upper_label if upper is not None else lower_label
    items = [figures.Annotation("text", margin, margin + text_px, text=text(title), size=text_px, bold=True, anchor="ls", role="title"),
             figures.Annotation("text", margin, legend_y, text=text(f"below the diagonal: {lower_label}  \u00b7  above: {upper_text}"),
                                size=small_px, anchor="ls", color="#555555", role="legend")]
    for index, item in enumerate(labels):
        y = top + index * cell + cell / 2
        items.append(figures.Annotation("text", left - 6 * scale, y, text=item, size=small_px, anchor="rm", role="row label"))
        items.append(figures.Annotation("text", left + index * cell + cell / 2, top - 5 * scale, text=str(index + 1), size=small_px, anchor="ms",
                                        color="#555555", role="column label"))
    if k <= 16:
        for i in range(k):
            for j in range(k):
                if i != j and math.isfinite(values[i, j]):
                    dark = abs(values[i, j]) > 0.55
                    items.append(figures.Annotation("text", left + j * cell + cell / 2 + gap / 2, top + i * cell + cell / 2 + gap / 2,
                                                    text=f"{values[i, j]:.2f}", size=small_px * (0.9 if cell < 36 * scale else 1.0),
                                                    anchor="mm", color="#FFFFFF" if dark else "#1A1A1A", role="value"))
    bar_w = min(max(k * cell, int(160 * scale)), int(240 * scale))
    items.extend(figures._colorbar({"kind": "diverging", "window": 1.0, "label": "r"}, left, top + k * cell + int(24 * scale), bar_w, bar_h, fonts, text_px, small_px))
    meta = {"format": figures.FORMAT, "version": figures.VERSION, "preset": "compare-matrix", "title": title, "maps": list(names),
            "lower": lower_label, "upper": upper_text, "dpi": int(dpi), "size_px": list(base.size),
            "font": "Inter" if not fonts.default else "Pillow default", "inputs": [], "tiles": []}
    caption_text = caption or (f"Spatial similarity of {k} maps over the connectome voxels: {lower_label} below the diagonal, {upper_text} above it "
                               "(blank: undefined, e.g. a map that is itself a covariate). " + "; ".join(f"{index + 1} = {name}" for index, name in enumerate(names)) + ".")
    meta["caption"] = caption_text
    return figures.Figure(base, items, dpi, fonts, meta, caption_text + "\n", sourcedata or [])


def _json_ready(value):
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, np.ndarray):
        return _json_ready(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        value = float(value)
        return value if math.isfinite(value) else None
    return value


def _strip_paths(value):
    """A copy of map metadata without local paths (keys named path/file/folder, or absolute-looking strings)."""
    if isinstance(value, dict):
        return {key: _strip_paths(item) for key, item in value.items() if key not in ("path", "file", "folder", "dir")}
    if isinstance(value, (list, tuple)):
        return [_strip_paths(item) for item in value]
    if isinstance(value, str) and (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
        return os.path.basename(value)
    return value


def write_outputs(comparison, out_dir, infos=None, tests=None, top=None, condition=None, title="Map comparison", font=None, formats=("png", "svg"),
                  reference_sha256=None):
    """Write the comparison to ``out_dir``: compare_r.csv, compare_spearman.csv, compare_partial.csv (with covariates),
    compare_pairs.csv, compare.json (no local paths; references by id, licence and citation; files are never copied)
    and the heat-map figure in ``figures/compare-matrix/``. Returns {"json": path, "files": [names], "figure": dir}.

    :param infos: {name: metadata} (kind, source, licence, citation, condition, sha256...; paths are dropped).
    :param tests: {(i, j): PermutationResult} of the tested pairs.
    """
    from . import figures

    os.makedirs(out_dir, exist_ok=True)
    infos = infos or {}
    tests = tests or {}
    files = {}
    files["compare_r.csv"] = matrix_csv(comparison.names, comparison.pearson)
    files["compare_spearman.csv"] = matrix_csv(comparison.names, comparison.spearman)
    if comparison.partial is not None:
        files["compare_partial.csv"] = matrix_csv(comparison.names, comparison.partial)
    pairs = pairs_rows(comparison, tests, {name: info.get("in_study") for name, info in infos.items() if isinstance(info, dict)})
    files["compare_pairs.csv"] = _rows_csv(pairs)
    for name, text in files.items():
        path = os.path.join(out_dir, name)
        with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(path + ".tmp", path)
    upper = comparison.partial if comparison.partial is not None else None
    figure = heatmap_figure(comparison.names, comparison.pearson, upper, title=title, font=font, sourcedata=pairs)
    written = figures.write_figure(figure, out_dir, FIGURE_NAME, formats=formats)
    maps = []
    for name in comparison.names:
        info = dict(infos.get(name) or {})
        info.pop("in_study", None)
        maps.append({"name": name, **_strip_paths(info)})
    record = {
        "format": COMPARE_FORMAT,
        "version": COMPARE_VERSION,
        "created": _now(),
        "condition": condition or None,
        "maps": maps,
        "covariates": list(comparison.covariates),
        "reference_sha256": reference_sha256,
        "statistics": {"pearson": "spatial Pearson r over the voxels finite in both maps", "spearman": "spatial Spearman rho (ties averaged)",
                       "partial": "Pearson r of both maps after OLS residualisation on [1, μC_r, PC1] (lnm-reference-v1)" if comparison.covariates else None,
                       "p": ("two-sided permutation p: the participants are permuted (one permutation of the lesions both maps use reassigns both maps' "
                             "scores or labels together); none for external maps; no spin or BrainSMASH p")},
        "pairs": pairs,
        "tests": [{"map_a": comparison.names[i], "map_b": comparison.names[j], "r": test.r, "p": test.p, "n_perm": test.n_perm, "statistic": test.statistic,
                   "n_voxels": test.n_voxels, "min_p": test.min_p, "seed": test.seed,
                   "scheme": test.scheme} for (i, j), test in sorted(tests.items())],
        "top_similar": top or [],
        "note": CROSS_CONDITION_NOTE,
        "redistribution": "Reference map files stay in the local library and are never copied into this folder.",
        "figure": _rel_to(out_dir, written["dir"]),
        "software": {"library": jobs.library_version(), "numpy": np.__version__},
    }
    path = os.path.join(out_dir, "compare.json")
    jobs.write_json_atomic(path, _json_ready(record))
    return {"json": path, "files": [*files, "compare.json"], "figure": written["dir"]}


def _rel_to(base, path):
    return os.path.relpath(path, base).replace(os.sep, "/")


# Command line


def _entry_summary(entry):
    return {key: entry.get(key) for key in ("id", "title", "source", "licence", "badge", "licence_note", "citation", "sha256", "space", "family", "condition", "kind", "redistribute")}


def _root_argument(parser):
    parser.add_argument("--root", default=None, help=f"registry folder (default: ${ENV_ROOT}, else <data directory>/{REGISTRY_DIR})")
    parser.add_argument("--condition", default=None, help="condition of the map(s), for the cross-condition note")


def _emit_entries(added):
    for entry in added:
        jobs.emit("result", kind="refmap", entry=_entry_summary(entry))


def _cli_progress():
    def progress(done, total):
        jobs.emit("progress", done=done, total=total, elapsed=round(jobs.elapsed(), 3), eta=None)

    return progress


def _list_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.refmaps list")
    _root_argument(parser)
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    jobs.emit("start", job="refmaps-list", argv=argv)
    _emit_entries(entries(args.root))
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return 0


def _wrap(callable_, *args, **kwargs):
    try:
        return callable_(*args, **kwargs)
    except RefmapsError as exc:
        raise jobs.JobError(str(exc), code="refmaps") from None


def _add_main(command, argv):
    parser = jobs.JsonArgumentParser(prog=f"python -m LesionNetworkMappingLib.refmaps {command}")
    _root_argument(parser)
    if command == "add-neurovault":
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--image", type=int, default=None, help="NeuroVault image id")
        group.add_argument("--collection", type=int, default=None, help="NeuroVault collection id (its images, at most 100)")
    elif command == "add-url":
        parser.add_argument("--url", required=True)
    elif command == "add-file":
        parser.add_argument("--file", required=True)
    else:
        parser.add_argument("--commit", default=VDH_COMMIT)
        parser.add_argument("--entry", default=VDH_DEFAULT_ENTRY, help="id in the pinned repository catalog (default: the degree map)")
        parser.add_argument("--accepted-terms-sha256", default=None, help="sha256 of the GSP data use terms text the user accepted (GSP-derived files)")
    if command in ("add-url", "add-file"):
        parser.add_argument("--title", default=None)
        parser.add_argument("--licence", default="unknown")
        parser.add_argument("--citation", default="")
        parser.add_argument("--space", default=None)
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    cancel = jobs.CancelToken(args.cancel_file)
    jobs.emit("start", job=f"refmaps-{command}", argv=argv)
    progress = _cli_progress()
    if command == "add-neurovault":
        added = _wrap(add_neurovault, image_id=args.image, collection_id=args.collection, condition=args.condition, root=args.root, progress=progress, cancel=cancel)
    elif command == "add-url":
        added = [_wrap(add_url, args.url, title=args.title, licence=args.licence, citation=args.citation, condition=args.condition, space=args.space, root=args.root,
                       progress=progress, cancel=cancel)]
    elif command == "add-file":
        added = [_wrap(add_file, args.file, title=args.title, licence=args.licence, citation=args.citation, condition=args.condition, space=args.space, root=args.root)]
    else:
        added = [_wrap(add_vdh, commit=args.commit, entry=args.entry, accepted_terms_sha256=args.accepted_terms_sha256, condition=args.condition, root=args.root,
                       progress=progress, cancel=cancel)]
    _emit_entries(added)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return 0


def _remove_main(argv):
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.refmaps remove")
    _root_argument(parser)
    parser.add_argument("--id", required=True)
    jobs.add_common_arguments(parser)
    args = parser.parse_args(argv)
    jobs.emit("start", job="refmaps-remove", argv=argv)
    _wrap(remove_entry, args.id, root=args.root)
    jobs.emit("done", outputs=[], elapsed=round(jobs.elapsed(), 3), run_id=None)
    return 0


_COMMANDS = {
    "list": _list_main,
    "add-neurovault": lambda argv: _add_main("add-neurovault", argv),
    "add-url": lambda argv: _add_main("add-url", argv),
    "add-file": lambda argv: _add_main("add-file", argv),
    "add-vdh": lambda argv: _add_main("add-vdh", argv),
    "remove": _remove_main,
}


def main(argv=None):
    """Command-line entry point (see the module docstring)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in _COMMANDS:
        parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.refmaps", description="Reference-map library (user-initiated downloads).")
        parser.add_argument("command", choices=list(_COMMANDS))
        parser.parse_args(argv[:1])
        return 2
    return _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    jobs.run_job(main, job="refmaps")
