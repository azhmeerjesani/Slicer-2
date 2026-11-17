#!/usr/bin/env python3
"""Write the build information of Slicer Studio for the commit updates from main (``StudioLib/updates_commit.py``).

Run by the ``SlicerStudioBuildInfo`` target of every build (``CMake/SlicerStudioBuildInfo.cmake``)::

    make_build_info.py --source <Slicer source> --records <build>/SlicerStudioVerbatimFiles.tsv --out <build>/bin/Python
                       [--scripted-root lib/Slicer-X.Y/qt-scripted-modules]

``--records`` is written by CMake at configure time: one line per file that the install rules copy verbatim, with
tab-separated fields ``source (relative to --source)``, ``installed folder on sys.path (relative to the Slicer home)``,
``path below that folder`` and ``scripted module name`` (empty for other files).

Two files are written (only when their content changed, so that nothing is reinstalled needlessly):

- ``studio_build_info.json``: the commit B the build was made from, its tree, its date, ``branch`` (the branch checked
  out when it was built, for information: the application takes code updates only when main contains B, which it
  checks against the repository), ``dirty`` (the working tree
  had changes to tracked files, or a recorded file is not tracked: such a build never takes commit updates) and
  ``studioVersion`` (``STUDIO_VERSION`` of ``StudioLib/version.py``). It is read at every start, so it stays small.
- ``studio_build_files.json.gz``: the listing of B (path, mode and blob id of every tracked file, from
  ``git ls-tree -r``) and the map of the recorded files from their source path to their installed path.

Both hold paths relative to the repository or to the Slicer home only. Without git (a source archive) ``commit`` is
null and the build never takes commit updates. Only the Python standard library is used.
"""

import argparse
import gzip
import io
import json
import os
import re
import subprocess
import sys
import time

FORMAT = 1
REPOSITORY = "azhmeerjesani/Slicer-2"
INFO_NAME = "studio_build_info.json"
FILES_NAME = "studio_build_files.json.gz"
VERSION_FILE = "Modules/Scripted/Studio/StudioLib/version.py"
#: Files that run before a commit update can apply: a change to them always needs a new build
PRE_OVERLAY = ("Base/Python/slicerqt.py", "Base/Python/studio_overlay.py")


def _git(source, *arguments):
    """The output of ``git -C source arguments`` (bytes), or None when git or the repository is not available."""
    try:
        result = subprocess.run(["git", "-C", source] + list(arguments), stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def readRecords(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 3 or not fields[0] or not fields[1] or not fields[2]:
                continue
            src, root, relative = (field.strip().strip("/") for field in fields[:3])
            module = fields[3].strip() if len(fields) > 3 else ""
            if any(part in ("", ".", "..") for part in (src + "/" + root + "/" + relative).split("/")) or os.path.isabs(src):
                continue
            records.append((src, root, relative, module))
    return records


def readVersion(source):
    """``STUDIO_VERSION`` of ``StudioLib/version.py`` (read, not imported), or None."""
    try:
        with open(os.path.join(source, VERSION_FILE), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r'^STUDIO_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def collect(source, records, scriptedRoot=None):
    """The build information and the build files of the source tree ``source`` (a dict each)."""
    source = os.path.abspath(source)
    head = _git(source, "rev-parse", "--verify", "HEAD")
    info = {"format": FORMAT, "repository": REPOSITORY, "branch": None, "commit": None, "tree": None, "committed": None,
            "dirty": True, "studioVersion": readVersion(source), "files": FILES_NAME}
    files = {"format": FORMAT, "commit": None, "tree": None, "entries": [], "map": [], "scriptedRoot": scriptedRoot,
             "preOverlay": list(PRE_OVERLAY)}
    if head is None:
        return info, files
    commit = head.decode().strip()
    tree = (_git(source, "rev-parse", "HEAD^{tree}") or b"").decode().strip()
    committed = (_git(source, "log", "-1", "--format=%ct", "HEAD") or b"").decode().strip()
    prefix = (_git(source, "rev-parse", "--show-prefix") or b"").decode().strip()
    branch = (_git(source, "rev-parse", "--abbrev-ref", "HEAD") or b"").decode().strip()
    listing = _git(source, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    status = _git(source, "status", "--porcelain", "--untracked-files=no")
    if listing is None or status is None or len(commit) != 40 or len(tree) != 40:
        return info, files
    entries = []
    tracked = set()
    for item in listing.split(b"\0"):
        if not item:
            continue
        meta, _tab, path = item.partition(b"\t")
        mode, _kind, oid = meta.decode().split(" ")
        name = path.decode("utf-8", "surrogateescape")
        entries.append([name, mode, oid])
        tracked.add(name)
    dirty = bool(status.strip())
    mapping = []
    seen = set()
    for src, root, relative, module in records:
        repoPath = prefix + src
        if repoPath not in tracked:
            dirty = True  # a recorded file that is not in the commit
            continue
        dst = f"{root}/{relative}"
        key = (repoPath, dst)
        if key in seen:
            continue
        seen.add(key)
        entry = {"src": repoPath, "dst": dst, "root": root}
        if module and relative == f"{module}.py":
            entry["module"] = module
        mapping.append(entry)
    mapping.sort(key=lambda entry: (entry["src"], entry["dst"]))
    info.update(commit=commit, tree=tree, dirty=dirty, branch=branch if branch and branch != "HEAD" else None,
                committed=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(committed))) if committed.isdigit() else None)
    files.update(commit=commit, tree=tree, entries=entries, map=mapping, preOverlay=[prefix + path for path in PRE_OVERLAY])
    return info, files


def _writeIfChanged(path, data):
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    temporary = path + ".tmp"
    with open(temporary, "wb") as f:
        f.write(data)
    os.replace(temporary, path)
    return True


def write(out, info, files):
    os.makedirs(out, exist_ok=True)
    infoData = (json.dumps(info, indent=1, sort_keys=True) + "\n").encode("utf-8")
    payload = json.dumps(files, separators=(",", ":"), sort_keys=True).encode("utf-8")
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0, compresslevel=9) as f:
        f.write(payload)
    changed = _writeIfChanged(os.path.join(out, FILES_NAME), buffer.getvalue())
    changed = _writeIfChanged(os.path.join(out, INFO_NAME), infoData) or changed
    return changed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source", required=True, help="Slicer Studio source tree (a git checkout)")
    parser.add_argument("--records", help="the verbatim files recorded by CMake (tab-separated)")
    parser.add_argument("--out", required=True, help="folder to write to (<build>/bin/Python)")
    parser.add_argument("--scripted-root", default=None, help="installed folder of the scripted modules, relative to the Slicer home")
    arguments = parser.parse_args(argv)
    records = readRecords(arguments.records) if arguments.records and os.path.exists(arguments.records) else []
    info, files = collect(arguments.source, records, arguments.scripted_root)
    write(arguments.out, info, files)
    if info["commit"] is None:
        print("make_build_info: no git commit: this build will not take code updates from main", file=sys.stderr)
    elif info["dirty"]:
        print("make_build_info: the source tree has changes: this build will not take code updates from main", file=sys.stderr)
    elif info["branch"] != "main":
        print(f"make_build_info: built from {info['branch'] or 'a detached HEAD'}, not main: this build takes code updates "
              "from main only once main contains its commit", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
