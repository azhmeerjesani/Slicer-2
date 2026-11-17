"""Code updates from main (StudioLib/updates_git.py, updates_commit.py, Base/Python/studio_overlay.py,
Utilities/Scripts/SlicerStudioRelease/make_build_info.py).

Plain Python, no Qt and no Slicer object, so it runs without a Slicer build::

  python3 Modules/Scripted/Studio/Testing/Python/StudioCommitUpdatesTest.py

(it also runs in Slicer with the other Studio tests). Nothing contacts GitHub: a git smart-HTTP server (protocol version
2: capabilities, ls-refs, fetch with a side-band pack written here in Python) and the raw files run on 127.0.0.1 in this
process, through ``SLICER_STUDIO_COMMIT_UPDATES``. A fake installation (``lib/Slicer-5.9/qt-scripted-modules``,
``bin/Python``) and its build files are written to temporary folders. With ``git`` on PATH, the pack parser and the
listing are also checked against git itself, and make_build_info.py against a temporary repository.

- protocol: pkt-lines, capabilities, ls-refs, side-band and its error channel;
- pack parser: trailer, OFS and REF deltas, truncated and damaged packs, size, count and inflation caps;
- trees: unsafe names, the listing rebuilds the tree id;
- classifier: mapped, added next to mapped files, ignored, needs a new build (C++, CMake, pre-overlay files, modes);
- check: up to date, ready (staged), needs a new build without downloading anything, ignored changes only, a tampered
  file or commit stages nothing, a redirect to another host and a host that is not allowed are refused, a slow server,
  offline;
- staging: whole units, installed files verified against the build listing, deletes, compiled code in a unit refused, a
  syntax error refused, crash leftovers removed, two stagers at once;
- bootstrap: promotion, base mismatch, turned off, not installed, testing, sys.path order, lazy pre-seeded modules, an
  end-to-end import in a subprocess, mapPath;
- rollback: a start that died unconfirmed goes back to the previous version, then to the installed one; a live start
  does not; Go back; garbage collection; main back at the build's commit; a damaged staged file is downloaded again;
- releases: clearForRelease, restore, forgetBase, the build version used for release decisions;
- make_build_info.py: commit, tree, dirty, untracked recorded file, no absolute path in the outputs;
- no GitHub API address in the new files.
"""

import gzip
import hashlib
import http.server
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.parse
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))
for _path in (os.path.join(SOURCE_DIR, "Modules", "Scripted", "Studio"), os.path.join(SOURCE_DIR, "Base", "Python"),
              os.path.join(SOURCE_DIR, "Utilities", "Scripts", "SlicerStudioRelease")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import studio_overlay  # noqa: E402
from StudioLib import updates  # noqa: E402
from StudioLib import updates_commit  # noqa: E402
from StudioLib import updates_git as git  # noqa: E402

import make_build_info  # noqa: E402

ROOT = "lib/Slicer-5.9/qt-scripted-modules"
PYROOT = "bin/Python"

#: source path -> (installed path, module)
MAP = {
    "Modules/Scripted/Studio/Studio.py": (f"{ROOT}/Studio.py", "Studio"),
    "Modules/Scripted/Studio/StudioLib/__init__.py": (f"{ROOT}/StudioLib/__init__.py", None),
    "Modules/Scripted/Studio/StudioLib/thing.py": (f"{ROOT}/StudioLib/thing.py", None),
    "Modules/Scripted/Studio/StudioLib/gone.py": (f"{ROOT}/StudioLib/gone.py", None),
    "Modules/Scripted/Studio/Resources/Icons/a.svg": (f"{ROOT}/Resources/Icons/a.svg", None),
    "Modules/Scripted/Other/Other.py": (f"{ROOT}/Other.py", "Other"),
    "Base/Python/slicer/__init__.py": (f"{PYROOT}/slicer/__init__.py", None),
    "Base/Python/slicer/util.py": (f"{PYROOT}/slicer/util.py", None),
    "Base/Python/slicerqt.py": (f"{PYROOT}/slicerqt.py", None),
    "Base/Python/studio_overlay.py": (f"{PYROOT}/studio_overlay.py", None),
}

STUDIO_PY = '''\
import os
from StudioLib import thing

VERSION = "{version}"


class Studio:
    pass


def icon():
    return os.path.join(os.path.dirname(__file__), "Resources", "Icons", "a.svg")
'''

BASE_FILES = {
    "Modules/Scripted/Studio/Studio.py": STUDIO_PY.format(version="B"),
    "Modules/Scripted/Studio/StudioLib/__init__.py": "",
    "Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'B'\n",
    "Modules/Scripted/Studio/StudioLib/gone.py": "GONE = True\n",
    "Modules/Scripted/Studio/Resources/Icons/a.svg": "<svg>B</svg>\n",
    "Modules/Scripted/Studio/CMakeLists.txt": "slicerMacroBuildScriptedModule(NAME Studio)\n",
    "Modules/Scripted/Studio/Testing/Python/StudioXTest.py": "pass\n",
    "Modules/Scripted/Other/Other.py": "class Other:\n    pass\n",
    "Base/Python/slicer/__init__.py": "",
    "Base/Python/slicer/util.py": "def modulePath():\n    return 'B'\n",
    "Base/Python/slicerqt.py": "pass\n",
    "Base/Python/studio_overlay.py": "pass\n",
    "Base/QTCore/qSlicerThing.cxx": "int main() { return 0; }\n",
    "CMakeLists.txt": "project(Slicer)\n",
    "README.md": "readme\n",
    "Docs/user_guide/a.md": "doc\n",
}


#
# A git repository in memory, and a pack writer
#


def _treeObjects(listing):
    """(root tree id, {id: ("tree", data)}) of a listing path -> (mode, id)."""
    root = {}
    for path, entry in listing.items():
        node = root
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = entry
    objects = {}

    def build(node):
        items = sorted(node.items(), key=lambda item: item[0].encode() + (b"/" if isinstance(item[1], dict) else b""))
        data = bytearray()
        for name, value in items:
            if isinstance(value, dict):
                mode, oid = "40000", build(value)
            else:
                mode, oid = value
            data += mode.encode() + b" " + name.encode() + b"\0" + bytes.fromhex(oid)
        oid = git.objectId("tree", bytes(data))
        objects[oid] = ("tree", bytes(data))
        return oid

    return build(root), objects


class Repo:
    """Commits of files (path -> text or (mode, bytes)) with their objects."""

    def __init__(self):
        self.commits = {}  # id -> {"files": {path: (mode, bytes)}, "objects": {...}, "listing": {...}}
        self.head = None

    def commit(self, files, message="Change", when=1763337600):
        normalized = {}
        for path, value in files.items():
            mode, data = value if isinstance(value, tuple) else ("100644", value)
            normalized[path] = (mode, data.encode() if isinstance(data, str) else data)
        listing = {path: (mode, git.blobId(data)) for path, (mode, data) in normalized.items()}
        tree, objects = _treeObjects(listing)
        parent = f"parent {self.head}\n" if self.head else ""
        data = (f"tree {tree}\n{parent}author A <a@example.org> {when} +0000\ncommitter A <a@example.org> {when} +0000\n\n{message}\n").encode()
        oid = git.objectId("commit", data)
        objects[oid] = ("commit", data)
        self.commits[oid] = {"files": normalized, "objects": objects, "listing": listing, "tree": tree}
        self.head = oid
        return oid


_TYPES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def _encodeSize(typeNumber, size):
    first = (typeNumber << 4) | (size & 0x0F)
    size >>= 4
    out = bytearray()
    while size:
        out.append(first | 0x80)
        first = size & 0x7F
        size >>= 7
    out.append(first)
    return bytes(out)


def _encodeOffset(distance):
    out = [distance & 0x7F]
    distance >>= 7
    while distance:
        distance -= 1
        out.insert(0, 0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(out)


def _varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def makeDelta(base, target):
    """A git delta: copy the common prefix of ``base``, insert the rest."""
    prefix = 0
    while prefix < min(len(base), len(target), 0xFFFF) and base[prefix] == target[prefix]:
        prefix += 1
    out = bytearray(_varint(len(base)) + _varint(len(target)))
    if prefix:
        opcode = 0x80
        sizeBytes = []
        for bit in range(3):
            byte = (prefix >> (8 * bit)) & 0xFF
            if byte:
                opcode |= 1 << (4 + bit)
                sizeBytes.append(byte)
        out.append(opcode)  # offset 0: no offset byte
        out += bytes(sizeBytes)
    rest = target[prefix:]
    for start in range(0, len(rest), 127):
        chunk = rest[start:start + 127]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def writePack(objects, deltas=None):
    """A pack of ``objects`` (a list of (type, data)); ``deltas`` maps an index to ("ofs" | "ref", base index)."""
    deltas = deltas or {}
    out = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
    offsets = []
    for index, (kind, data) in enumerate(objects):
        offsets.append(len(out))
        if index in deltas:
            how, baseIndex = deltas[index]
            payload = makeDelta(objects[baseIndex][1], data)
            out += _encodeSize(6 if how == "ofs" else 7, len(payload))
            if how == "ofs":
                out += _encodeOffset(offsets[index] - offsets[baseIndex])
            else:
                out += bytes.fromhex(git.objectId(*objects[baseIndex]))
        else:
            payload = data
            out += _encodeSize(_TYPES[kind], len(payload))
        out += zlib.compress(payload)
    out += hashlib.sha1(out).digest()
    return bytes(out)


def sideBand(pack, extra=b""):
    parts = [git.pktLine("shallow-info\n"), git.delimPkt(), git.pktLine("packfile\n")]
    parts.append(git.pktLine(b"\x02Enumerating objects\n"))
    for start in range(0, len(pack), 60000):
        parts.append(git.pktLine(b"\x01" + pack[start:start + 60000]))
    parts.append(extra)
    parts.append(git.flushPkt())
    return b"".join(parts)


#
# The fake server
#


class FakeGitServer:
    def __init__(self, repo):
        self.repo = repo
        self.requests = []
        self.tamperBlobs = {}  # path -> bytes served instead
        self.tamperCommit = False
        self.redirectRaw = None
        self.trickle = False
        self.useDeltas = True
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, code, body, contentType="application/octet-stream", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", contentType)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    if server.trickle:
                        for start in range(0, len(body), 8):
                            self.wfile.write(body[start:start + 8])
                            self.wfile.flush()
                            time.sleep(0.05)
                    else:
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True  # the client gave up (a slow answer)

            def do_GET(self):
                path = urllib.parse.urlsplit(self.path).path
                server.requests.append(("GET", path))
                if path == "/repo/info/refs":
                    body = (git.pktLine("# service=git-upload-pack\n") + git.flushPkt() + git.pktLine("version 2\n")
                            + git.pktLine("agent=git/fake\n") + git.pktLine("ls-refs=unborn\n")
                            + git.pktLine("fetch=shallow wait-for-done filter\n") + git.pktLine("object-format=sha1\n") + git.flushPkt())
                    return self._send(200, body, "application/x-git-upload-pack-advertisement")
                if path.startswith("/repo/raw/"):
                    if server.redirectRaw:
                        return self._send(302, b"", headers={"Location": server.redirectRaw})
                    commit, _sep, filePath = path[len("/repo/raw/"):].partition("/")
                    filePath = urllib.parse.unquote(filePath)
                    entry = server.repo.commits.get(commit, {}).get("files", {}).get(filePath)
                    if entry is None:
                        return self._send(404, b"not found")
                    return self._send(200, server.tamperBlobs.get(filePath, entry[1]))
                self._send(404, b"")

            def do_POST(self):
                path = urllib.parse.urlsplit(self.path).path
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                lines = [item.decode().strip() for item in git.readPktLines(body) if isinstance(item, bytes)]
                command = lines[0] if lines else ""
                server.requests.append(("POST", command))
                if path != "/repo/git-upload-pack" or self.headers.get("Git-Protocol") != "version=2":
                    return self._send(400, b"")
                if command == "command=ls-refs":
                    answer = git.pktLine(f"{server.repo.head} refs/heads/main\n") + git.flushPkt()
                    return self._send(200, answer, "application/x-git-upload-pack-result")
                if command == "command=fetch":
                    want = next(line[5:] for line in lines if line.startswith("want "))
                    commit = server.repo.commits[want]
                    objects = [(kind, data) for oid, (kind, data) in commit["objects"].items() if kind == "commit"]
                    trees = [(kind, data) for oid, (kind, data) in commit["objects"].items() if kind == "tree"]
                    if server.tamperCommit:
                        objects = [("commit", objects[0][1].replace(b"Change", b"Evil!!"))]
                    allObjects = objects + trees
                    deltas = {}
                    if server.useDeltas and len(trees) >= 3:
                        deltas = {2: ("ofs", 1), len(allObjects) - 1: ("ref", 1)}
                    pack = writePack(allObjects, deltas)
                    return self._send(200, sideBand(pack), "application/x-git-upload-pack-result")
                self._send(400, b"")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/repo"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def blobRequests(self):
        return [path for method, path in self.requests if method == "GET" and "/raw/" in path]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


#
# The test
#


class StudioCommitUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="StudioCommitUpdates-")
        self.home = os.path.join(self.tmp, "home")
        self.stateRoot = os.path.join(self.tmp, "state", "commit")
        self.repo = Repo()
        self.base = self.repo.commit(BASE_FILES, "Base build")
        self.server = FakeGitServer(self.repo)
        self.env = {studio_overlay.ENVIRONMENT_VARIABLE: self.server.url}
        self._overlayGlobals = {name: getattr(studio_overlay, name) for name in ("_status", "_buildInfo", "_roots", "_preseeded", "_boot")}
        self._constants = {name: getattr(updates_commit, name) for name in ("CHECK_DEADLINE_S", "MAX_FILES")}
        self.writeInstall()

    def tearDown(self):
        self.server.close()
        for name, value in self._overlayGlobals.items():
            setattr(studio_overlay, name, value)
        for name, value in self._constants.items():
            setattr(updates_commit, name, value)
        for name in ("Studio", "Other", "StudioLib", "StudioLib.thing"):
            module = sys.modules.get(name)
            if module is not None and self.tmp in str(getattr(module, "__spec__", None) and module.__spec__.origin or ""):
                del sys.modules[name]
        shutil.rmtree(self.tmp, ignore_errors=True)

    #
    # Fixtures
    #

    def writeInstall(self, base=None):
        """The installation of the base commit, and its build files (as make_build_info.py writes them)."""
        base = base or self.base
        commit = self.repo.commits[base]
        for src, (dst, _module) in MAP.items():
            path = os.path.join(self.home, dst)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(commit["files"][src][1])
        # Unmapped installed files: generated (kits.py), native packages (vtkmodules), files inside a mapped package
        extra = {f"{PYROOT}/slicer/kits.py": "KITS = []\n", f"{PYROOT}/vtkmodules/__init__.py": "", f"{PYROOT}/vtk.py": "",
                 f"{ROOT}/StudioLib/generated.txt": "generated\n", f"{ROOT}/Unmapped/__init__.py": "", "Slicer": "#!/bin/sh\n"}
        for dst, text in extra.items():
            path = os.path.join(self.home, dst)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        info = {"format": 1, "repository": "azhmeerjesani/Slicer-2", "branch": "main", "commit": base, "tree": commit["tree"],
                "committed": "2025-11-17T00:00:00Z", "dirty": False, "studioVersion": "0.9.0", "files": "studio_build_files.json.gz"}
        files = {"format": 1, "commit": base, "tree": commit["tree"],
                 "entries": [[path, mode, oid] for path, (mode, oid) in sorted(commit["listing"].items())],
                 "map": [dict({"src": src, "dst": dst, "root": ROOT if dst.startswith(ROOT) else PYROOT}, **({"module": module} if module else {}))
                         for src, (dst, module) in sorted(MAP.items())],
                 "scriptedRoot": ROOT, "preOverlay": list(make_build_info.PRE_OVERLAY)}
        make_build_info.write(os.path.join(self.home, PYROOT), info, files)
        return info

    def change(self, changes, message="Change"):
        files = {path: (mode, data) for path, (mode, data) in self.repo.commits[self.repo.head]["files"].items()}
        for path, value in changes.items():
            if value is None:
                files.pop(path, None)
            else:
                files[path] = value if isinstance(value, tuple) else ("100644", value.encode())
        return self.repo.commit(files, message)

    def check(self, **kwargs):
        return updates_commit.check(self.home, self.stateRoot, env=self.env, **kwargs)

    def baseDir(self):
        return os.path.join(self.stateRoot, self.base)

    def state(self):
        return studio_overlay.readState(self.baseDir())

    def runInfo(self, **values):
        info = {"installed": True, "testing": False, "interactive": True, "pid": os.getpid(), "token": None}
        info.update(values)
        return info

    def activate(self, sysPath=None, sysModules=None, env=None, **runValues):
        sysPath = [os.path.join(self.home, "bin", "..", PYROOT), os.path.join(self.home, ROOT), "/usr/lib/python3"] if sysPath is None else sysPath
        sysModules = {} if sysModules is None else sysModules
        result = studio_overlay.activate(self.home, self.stateRoot, self.runInfo(**runValues), env=env if env is not None else {},
                                         sysPath=sysPath, sysModules=sysModules)
        return result, sysPath, sysModules

    def stageHead(self, changes=None):
        head = self.change(changes or {"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n",
                                       "Modules/Scripted/Studio/Studio.py": STUDIO_PY.format(version="H")})
        result = self.check()
        self.assertEqual(result["result"], "ready", result)
        return head

    #
    # Protocol
    #

    def test_PktLines(self):
        data = git.pktLine("version 2\n") + git.delimPkt() + git.pktLine(b"abc") + git.flushPkt()
        self.assertEqual(git.readPktLines(data), [b"version 2\n", git.DELIM, b"abc", git.FLUSH])
        with self.assertRaises(git.GitProtocolError):
            git.readPktLines(b"00ffabc")
        with self.assertRaises(git.GitProtocolError):
            git.readPktLines(b"zzzz")
        capabilities = git.parseCapabilities(git.pktLine("# service=git-upload-pack\n") + git.flushPkt() + git.pktLine("version 2\n")
                                             + git.pktLine("ls-refs\n") + git.pktLine("fetch=shallow filter\n") + git.flushPkt())
        git.checkCapabilities(capabilities)
        with self.assertRaises(git.GitProtocolError):
            git.parseCapabilities(git.pktLine("version 1\n") + git.flushPkt())
        with self.assertRaises(git.GitProtocolError):
            git.checkCapabilities({"ls-refs": None, "fetch": "shallow"})  # no filter
        refs = git.parseLsRefs(git.pktLine("a" * 40 + " refs/heads/main\n") + git.pktLine("unborn HEAD\n") + git.flushPkt())
        self.assertEqual(refs, {"refs/heads/main": "a" * 40})
        request = git.fetchRequest("b" * 40, agent="SlicerStudio/1")
        texts = [item.decode() for item in git.readPktLines(request) if isinstance(item, bytes)]
        self.assertIn("deepen 1\n", texts)
        self.assertIn("filter blob:none\n", texts)
        self.assertIn("want " + "b" * 40 + "\n", texts)
        self.assertEqual(texts[-1], "done\n")
        # Side-band: 1 is the pack, 2 is ignored, 3 is an error
        pack = writePack([("blob", b"x")])
        shallow, parsed = git.parseFetchResponse(sideBand(pack))
        self.assertEqual(parsed, pack)
        with self.assertRaises(git.GitProtocolError):
            git.parseFetchResponse(sideBand(pack, extra=git.pktLine(b"\x03fatal: no\n")))
        with self.assertRaises(git.GitProtocolError):
            git.parseFetchResponse(git.pktLine("packfile\n") + git.flushPkt())
        with self.assertRaises(git.GitProtocolError):
            git.parseFetchResponse(sideBand(pack), maxPackBytes=10)

    def test_PackParser(self):
        base = b"line\n" * 200
        target = base + b"more\n"
        objects = [("blob", base), ("blob", target), ("blob", target + b"x"), ("tree", b"")]
        pack = writePack(objects, {1: ("ofs", 0), 2: ("ref", 1)})
        parsed = git.parsePack(pack)
        for kind, data in objects:
            self.assertEqual(parsed[git.objectId(kind, data)], (kind, data))
        # Trailer, truncation, damage
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(pack[:-1] + bytes([pack[-1] ^ 1]))
        truncated = pack[:40]
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(truncated + hashlib.sha1(truncated).digest())
        damaged = bytearray(pack[:-20])
        damaged[20] ^= 0xFF
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(bytes(damaged) + hashlib.sha1(damaged).digest())
        # Caps
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(pack, maxObjects=3)
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(pack, maxInflated=500)
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(pack, maxPackBytes=50)
        # A REF_DELTA whose base is not in the pack (a thin pack is never asked for)
        withBase = writePack([("blob", base), ("blob", target)], {1: ("ref", 0)})
        firstEnd = 12 + len(_encodeSize(3, len(base))) + len(zlib.compress(base))
        body = bytearray(withBase[:12] + withBase[firstEnd:-20])
        body[8:12] = struct.pack(">I", 1)
        with self.assertRaises(git.GitProtocolError):
            git.parsePack(bytes(body) + hashlib.sha1(body).digest())
        # An object larger than announced
        with self.assertRaises(git.GitProtocolError):
            git._inflate(zlib.compress(b"abcdef"), 0, 3, 100)

    def test_PackAgainstGit(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        repo = os.path.join(self.tmp, "gitrepo")
        os.makedirs(os.path.join(repo, "a", "b"))
        run = lambda *args: subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                                           env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                                    GIT_COMMITTER_EMAIL="t@t")).stdout
        run("init", "-q")
        for index in range(20):
            with open(os.path.join(repo, "a", "b", f"f{index}.txt"), "w") as f:
                f.write("x\n" * (index * 40 + 5))
        run("add", "-A")
        run("commit", "-qm", "one")
        for index in range(20):
            with open(os.path.join(repo, "a", "b", f"f{index}.txt"), "a") as f:
                f.write("y\n")
        run("add", "-A")
        run("commit", "-qm", "two")
        objects = run("rev-list", "--objects", "--all").decode()
        pack = subprocess.run(["git", "-C", repo, "pack-objects", "--stdout", "--delta-base-offset", "--window=50"],
                              input=objects.encode(), capture_output=True, check=True).stdout
        parsed = git.parsePack(pack)
        head = run("rev-parse", "HEAD").decode().strip()
        fields = git.parseCommit(parsed[head][1])
        listing = git.walkTree(parsed, fields["tree"])
        expected = {}
        for line in run("ls-tree", "-r", "HEAD").decode().splitlines():
            meta, path = line.split("\t", 1)
            mode, _kind, oid = meta.split()
            expected[path] = (mode, oid)
        self.assertEqual(listing, expected)
        self.assertEqual(git.treeIdFromListing(listing), fields["tree"])
        self.assertEqual(len(parsed), len([line for line in objects.splitlines() if line.strip()]))

    def test_Trees(self):
        listing = self.repo.commits[self.base]["listing"]
        self.assertEqual(git.treeIdFromListing(listing), self.repo.commits[self.base]["tree"])
        objects = self.repo.commits[self.base]["objects"]
        self.assertEqual(git.walkTree(objects, self.repo.commits[self.base]["tree"]), listing)
        for name in ("..", ".git", ".GIT", "a/b"):
            data = b"100644 " + name.encode() + b"\0" + b"\1" * 20
            with self.assertRaises(git.GitProtocolError):
                git.parseTree(data)
        with self.assertRaises(git.GitProtocolError):
            git.parseTree(b"100644 \xff\xfe\0" + b"\1" * 20)
        with self.assertRaises(git.GitProtocolError):
            git.walkTree({}, "a" * 40)

    #
    # Classifier
    #

    def buildFiles(self):
        return updates_commit.loadBuildFiles(self.home, studio_overlay.readBuildInfo(self.home))

    def test_Classifier(self):
        files = self.buildFiles()
        blob = "c" * 40

        def one(path, before, after):
            return updates_commit.classify([(path, before, after)], files)

        result = one("Modules/Scripted/Studio/StudioLib/thing.py", ("100644", "a" * 40), ("100644", blob))
        self.assertEqual(result.kind, "ready")
        self.assertEqual(result.apply[0]["dst"], f"{ROOT}/StudioLib/thing.py")
        # Added next to mapped files with the same extension: applied to the same installed folder
        result = one("Modules/Scripted/Studio/StudioLib/new.py", None, ("100644", blob))
        self.assertEqual((result.kind, result.apply[0]["dst"]), ("ready", f"{ROOT}/StudioLib/new.py"))
        # ... but not with another extension, nor a new folder
        self.assertEqual(one("Modules/Scripted/Studio/StudioLib/new.ui", None, ("100644", blob)).kind, "needsBuild")
        self.assertEqual(one("Modules/Scripted/Studio/NewLib/x.py", None, ("100644", blob)).kind, "needsBuild")
        # Deleted mapped file
        result = one("Modules/Scripted/Studio/StudioLib/gone.py", ("100644", "a" * 40), None)
        self.assertEqual((result.kind, result.apply[0]["action"]), ("ready", "delete"))
        # Ignored
        for path in ("Docs/x.md", "README.md", "CLAUDE.md", ".gitignore", ".github/workflows/a.yml", "Modules/Scripted/Studio/Testing/Python/T.py",
                     "Utilities/Scripts/SlicerStudioRelease/make_release.py", "Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh"):
            self.assertEqual(one(path, ("100644", "a" * 40), ("100644", blob)).kind, "upToDate", path)
        # Needs a new build
        for path in ("Base/QTCore/qSlicerThing.cxx", "CMakeLists.txt", "Modules/Scripted/Studio/CMakeLists.txt", "Base/QTGUI/Resources/UI/x.ui",
                     "Base/QTGUI/Resources/qSlicerBaseQTGUI.qrc", "Modules/CLI/X/X.xml", "SuperBuild/External_X.cmake",
                     "Base/Python/slicerqt.py", "Base/Python/studio_overlay.py", "Base/Python/vtk.py.in"):
            self.assertEqual(one(path, ("100644", "a" * 40), ("100644", blob)).kind, "needsBuild", path)
        # Mode changes, symlinks and submodules
        self.assertEqual(one("Modules/Scripted/Studio/StudioLib/thing.py", ("100644", "a" * 40), ("100755", "a" * 40)).kind, "needsBuild")
        self.assertEqual(one("Modules/Scripted/Studio/StudioLib/link.py", None, ("120000", blob)).kind, "needsBuild")
        self.assertEqual(one("Modules/Scripted/Studio/StudioLib/thing.py", ("100644", "a" * 40), ("120000", blob)).kind, "needsBuild")
        self.assertEqual(one("Libs/sub", None, ("160000", blob)).kind, "needsBuild")
        # Mixed: anything that needs a build wins
        mixed = updates_commit.classify([("Modules/Scripted/Studio/StudioLib/thing.py", ("100644", "a" * 40), ("100644", blob)),
                                         ("Base/QTCore/qSlicerThing.cxx", ("100644", "a" * 40), ("100644", blob))], files)
        self.assertEqual(mixed.kind, "needsBuild")

    #
    # Check
    #

    def test_CheckUpToDate(self):
        result = self.check()
        self.assertEqual(result["result"], "upToDate")
        self.assertEqual(self.server.blobRequests(), [])
        # Only ignored changes
        self.change({"Docs/user_guide/a.md": "new doc\n", "README.md": "new\n"})
        result = self.check()
        self.assertEqual(result["result"], "upToDate", result)
        self.assertEqual(self.server.blobRequests(), [])
        self.assertFalse(os.path.isdir(os.path.join(self.baseDir(), "versions")))

    def test_CheckReadyStagesWholeUnits(self):
        head = self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n",
                            "Modules/Scripted/Studio/StudioLib/new.py": "NEW = 1\n",
                            "Modules/Scripted/Studio/StudioLib/gone.py": None,
                            "Modules/Scripted/Studio/Resources/Icons/a.svg": "<svg>H</svg>\n",
                            "Docs/user_guide/a.md": "doc 2\n"}, message="Make thing H\n\nbody")
        result = self.check()
        self.assertEqual(result["result"], "ready", result)
        self.assertEqual(result["head"], head)
        self.assertEqual(result["subject"], "Make thing H")
        self.assertEqual(result["files"], 4)
        version = os.path.join(self.baseDir(), "versions", head)
        files = os.path.join(version, "files")
        read = lambda path: _read(os.path.join(files, path))
        self.assertEqual(read(f"{ROOT}/StudioLib/thing.py"), "VALUE = 'H'\n")
        self.assertEqual(read(f"{ROOT}/StudioLib/new.py"), "NEW = 1\n")
        self.assertEqual(read(f"{ROOT}/Resources/Icons/a.svg"), "<svg>H</svg>\n")
        self.assertFalse(os.path.exists(os.path.join(files, ROOT, "StudioLib", "gone.py")))
        # Whole units: unchanged mapped files and unmapped files of a mapped package come along; units without a
        # mapped file (Unmapped) and unchanged roots (bin/Python) do not
        self.assertEqual(read(f"{ROOT}/Studio.py"), STUDIO_PY.format(version="B"))
        self.assertEqual(read(f"{ROOT}/StudioLib/generated.txt"), "generated\n")
        self.assertTrue(os.path.isfile(os.path.join(files, ROOT, "Other.py")))
        self.assertFalse(os.path.exists(os.path.join(files, ROOT, "Unmapped")))
        self.assertFalse(os.path.exists(os.path.join(files, PYROOT)))
        # Compiled
        self.assertTrue(any(name.startswith("thing.") for name in os.listdir(os.path.join(files, ROOT, "StudioLib", "__pycache__"))))
        manifest = studio_overlay.readManifest(self.baseDir(), head)
        self.assertEqual(manifest["base"], self.base)
        self.assertEqual(manifest["roots"], [ROOT])
        self.assertEqual(manifest["modules"], {"Studio": f"{ROOT}/Studio.py", "Other": f"{ROOT}/Other.py"})
        self.assertEqual(manifest["files"][f"{ROOT}/StudioLib/thing.py"], git.blobId(b"VALUE = 'H'\n"))
        self.assertEqual(self.state()["pending"], head)
        # Only the changed files were downloaded (the docs change was not)
        self.assertEqual(len(self.server.blobRequests()), 3)
        # No absolute path of this computer in the manifest
        self.assertNotIn(self.tmp, json.dumps(manifest))
        # The same head again: nothing downloaded or staged again
        before = len(self.server.requests)
        again = self.check()
        self.assertEqual((again["result"], again.get("already")), ("ready", "pending"))
        self.assertEqual(len(self.server.requests) - before, 2)  # the capabilities and ls-refs only
        self.assertEqual(len(self.server.blobRequests()), 3)

    def test_CheckNeedsBuildDownloadsNothing(self):
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n", "Base/QTCore/qSlicerThing.cxx": "int x;\n"})
        result = self.check()
        self.assertEqual(result["result"], "needsBuild", result)
        self.assertEqual(result["needsBuild"], 1)
        self.assertEqual(result["paths"], ["Base/QTCore/qSlicerThing.cxx"])
        self.assertEqual(self.server.blobRequests(), [])
        self.assertIsNone(self.state())
        # The same head again is answered from the cached result without fetching
        before = [command for method, command in self.server.requests if command == "command=fetch"]
        cached = self.check(cached=result)
        self.assertEqual(cached["result"], "needsBuild")
        self.assertEqual(before, [command for method, command in self.server.requests if command == "command=fetch"])

    def test_TamperedFileStagesNothing(self):
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        self.server.tamperBlobs["Modules/Scripted/Studio/StudioLib/thing.py"] = b"import os; os.system('evil')\n"
        result = self.check()
        self.assertEqual(result["result"], "failed", result)
        self.assertIn("does not match", result["error"])
        self.assertFalse(os.listdir(os.path.join(self.baseDir(), "versions")) if os.path.isdir(os.path.join(self.baseDir(), "versions")) else [])
        self.assertIsNone((self.state() or {}).get("pending"))
        self.assertEqual(os.listdir(os.path.join(self.baseDir(), "staging")), [])

    def test_TamperedCommitStagesNothing(self):
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        self.server.tamperCommit = True
        result = self.check()
        self.assertEqual(result["result"], "failed", result)
        self.assertEqual(self.server.blobRequests(), [])
        self.assertIsNone(self.state())

    def test_HostsAndRedirects(self):
        # Default addresses: GitHub only, never the API
        endpoint = updates_commit.endpoints({})
        self.assertEqual(endpoint["git"], "https://github.com/azhmeerjesani/Slicer-2.git")
        self.assertTrue(endpoint["raw"].startswith("https://raw.githubusercontent.com/azhmeerjesani/Slicer-2/"))
        policy = endpoint["policy"]
        updates.checkUrl("https://github.com/azhmeerjesani/Slicer-2.git/info/refs", **policy)
        updates.checkUrl("https://raw.githubusercontent.com/azhmeerjesani/Slicer-2/x/y", **policy)
        for url in ("https://api.github.com/repos/x", "http://github.com/x", "https://example.org/x", "http://127.0.0.1:1/x", "file:///etc/passwd"):
            with self.assertRaises(updates.UpdateError, msg=url):
                updates.checkUrl(url, **policy)
        self.assertIsNone(updates_commit.endpoints({studio_overlay.ENVIRONMENT_VARIABLE: "off"}))
        self.assertIsNone(updates_commit.endpoints({"SLICER_STUDIO_UPDATE_FEED": "off"}))
        # A redirect of a file to another host is refused: nothing staged
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        self.server.redirectRaw = "http://example.org/evil.py"
        result = self.check()
        self.assertNotEqual(result["result"], "ready")
        self.assertIn("Refused", result["error"])
        self.assertIsNone((self.state() or {}).get("pending"))

    def test_OfflineAndSlow(self):
        closed = FakeGitServer(self.repo)
        url = closed.url
        closed.close()
        result = updates_commit.check(self.home, self.stateRoot, env={studio_overlay.ENVIRONMENT_VARIABLE: url})
        self.assertEqual(result["result"], "offline")
        updates_commit.CHECK_DEADLINE_S = 0.2
        self.server.trickle = True
        result = self.check()
        self.assertEqual(result["result"], "offline", result)
        self.assertIn("slowly", result["error"])

    def test_UnavailableBuilds(self):
        info = studio_overlay.readBuildInfo(self.home)
        for change, expected in (({"dirty": True}, "changed files"), ({"commit": None}, "commit")):
            data = dict(info, **change)
            make_build_info._writeIfChanged(os.path.join(self.home, PYROOT, "studio_build_info.json"), json.dumps(data).encode())
            result = self.check()
            self.assertEqual(result["result"], "unavailable", result)
            self.assertIn(expected, result["error"])
        self.writeInstall()
        # A build listing that does not match the build commit
        os.remove(os.path.join(self.home, PYROOT, "studio_build_files.json.gz"))
        files = {"format": 1, "commit": self.base, "tree": self.repo.commits[self.base]["tree"], "entries": [["x", "100644", "a" * 40]], "map": []}
        with gzip.open(os.path.join(self.home, PYROOT, "studio_build_files.json.gz"), "wt") as f:
            json.dump(files, f)
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        result = self.check()
        self.assertEqual(result["result"], "unavailable", result)
        self.assertIn("does not match", result["error"])

    def test_TooLarge(self):
        updates_commit.MAX_FILES = 1
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n", "Modules/Scripted/Studio/StudioLib/new.py": "X = 1\n"})
        self.assertEqual(self.check()["result"], "tooLarge")
        self.assertEqual(self.server.blobRequests(), [])

    #
    # Staging
    #

    def test_StagingVerifiesInstalledFiles(self):
        # An installed file changed by hand is replaced by the file of the commit
        with open(os.path.join(self.home, ROOT, "Studio.py"), "w") as f:
            f.write("HACKED = True\n")
        head = self.stageHead({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        staged = os.path.join(self.baseDir(), "versions", head, "files", ROOT, "Studio.py")
        self.assertEqual(_read(staged), STUDIO_PY.format(version="B"))

    def test_NativeCodeInUnitRefused(self):
        with open(os.path.join(self.home, ROOT, "StudioLib", "fast.so"), "wb") as f:
            f.write(b"\x7fELF" + b"\0" * 60)
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        result = self.check()
        self.assertEqual(result["result"], "unavailable", result)
        self.assertIn("compiled code", result["error"])
        self.assertIsNone((self.state() or {}).get("pending"))

    def test_SyntaxErrorRefused(self):
        self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "def broken(:\n"})
        result = self.check()
        self.assertEqual(result["result"], "failed", result)
        self.assertIn("thing.py", result["error"])
        self.assertIsNone((self.state() or {}).get("pending"))
        self.assertEqual(os.listdir(os.path.join(self.baseDir(), "staging")), [])

    def test_CrashLeftoversRemoved(self):
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        leftover = os.path.join(self.baseDir(), "staging", f"{'d' * 40}-{dead.pid}-abcd")
        os.makedirs(os.path.join(leftover, "files"))
        live = os.path.join(self.baseDir(), "staging", f"{'e' * 40}-{os.getpid()}-abcd")
        os.makedirs(live)
        self.stageHead()
        self.assertFalse(os.path.exists(leftover))
        self.assertTrue(os.path.exists(live))  # another stager of a running process

    def test_ConcurrentStagers(self):
        head = self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H'\n"})
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.check())) for _index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual([result["result"] for result in results], ["ready", "ready"], results)
        self.assertEqual(os.listdir(os.path.join(self.baseDir(), "versions")), [head])
        self.assertEqual(os.listdir(os.path.join(self.baseDir(), "staging")), [])
        self.assertEqual(self.state()["pending"], head)

    def test_ReuseOfStagedFiles(self):
        first = self.stageHead()
        before = len(self.server.blobRequests())
        second = self.change({"Modules/Scripted/Studio/StudioLib/new.py": "X = 2\n"})
        result = self.check()
        self.assertEqual(result["result"], "ready", result)
        # thing.py and Studio.py (same blobs as in the first version) are taken from it; only new.py is downloaded
        self.assertEqual(len(self.server.blobRequests()) - before, 1)
        self.assertNotEqual(first, second)

    #
    # Bootstrap
    #

    def test_BootstrapPromotesAndActivates(self):
        head = self.stageHead()
        result, sysPath, sysModules = self.activate()
        self.assertEqual(result["state"], "active", result)
        self.assertEqual(result["promoted"], head)
        overlayRoot = os.path.join(self.baseDir(), "versions", head, "files", ROOT)
        # Inserted just before the installed folder (bin/Python is not overlaid)
        self.assertEqual(sysPath.index(overlayRoot) + 1, sysPath.index(os.path.join(self.home, ROOT)))
        self.assertEqual(len(sysPath), 4)
        # Pre-seeded lazily: nothing ran yet
        module = sysModules["Studio"]
        self.assertEqual(type(module).__name__, "_LazyModule")
        self.assertEqual(sorted(result["modules"]), ["Other", "Studio"])
        state = self.state()
        self.assertEqual((state["active"], state["pending"], state["previous"]), (head, None, None))
        self.assertEqual(len(state["boots"]), 1)
        self.assertFalse(state["boots"][0]["confirmed"])
        # mapPath follows the overlay
        installed = os.path.join(self.home, ROOT, "Studio.py")
        self.assertEqual(studio_overlay.mapPath(installed), os.path.join(overlayRoot, "Studio.py"))
        self.assertEqual(studio_overlay.mapPath(os.path.join(self.home, ROOT, "Unmapped", "__init__.py")),
                         os.path.join(self.home, ROOT, "Unmapped", "__init__.py"))
        self.assertEqual(studio_overlay.mapPath("/elsewhere/x.py"), "/elsewhere/x.py")
        # Confirmation
        self.assertTrue(studio_overlay.confirmStart(True))
        self.assertTrue(self.state()["boots"][0]["confirmed"])

    def test_BootstrapRefusals(self):
        self.stageHead()
        for values, env, reason in (({"installed": False}, {}, "development"), ({"testing": True}, {}, "testing"),
                                    ({}, {studio_overlay.ENVIRONMENT_VARIABLE: "off"}, "environment"),
                                    ({}, {"SLICER_STUDIO_UPDATE_FEED": "off"}, "environment"),
                                    ({}, {"SLICER_STUDIO_WEB": "1"}, "web")):
            result, sysPath, sysModules = self.activate(env=env, **values)
            self.assertEqual((result["state"], result["reason"]), ("inactive", reason))
            self.assertEqual(sysModules, {})
        self.assertEqual(self.state()["active"], None)  # nothing was promoted by those runs
        # Turned off by the user
        updates_commit.setDisabled(self.stateRoot, self.base, True)
        result, _path, _modules = self.activate()
        self.assertEqual(result["reason"], "disabled")
        updates_commit.setDisabled(self.stateRoot, self.base, False)
        # A module already imported: the installed code is used
        result, sysPath, sysModules = self.activate(sysModules={"StudioLib": object()})
        self.assertEqual(result["state"], "inactive")
        self.assertIn("StudioLib", result["error"])
        self.assertEqual(self.state()["activationError"]["version"], self.repo.head)
        self.assertEqual(len(sysPath), 3)

    def test_BootstrapBaseMismatch(self):
        head = self.stageHead()
        manifestPath = os.path.join(self.baseDir(), "versions", head, "manifest.json")
        manifest = json.loads(_read(manifestPath))
        manifest["base"] = "f" * 40
        studio_overlay.writeJsonAtomic(manifestPath, manifest)
        result, sysPath, _modules = self.activate()
        self.assertEqual(result["state"], "inactive")
        self.assertIsNone(self.state()["active"])
        self.assertIsNone(self.state()["pending"])
        # Another build (another commit) never sees this state
        info = dict(studio_overlay.readBuildInfo(self.home), commit="a" * 40)
        make_build_info._writeIfChanged(os.path.join(self.home, PYROOT, "studio_build_info.json"), json.dumps(info).encode())
        result, _path, _modules = self.activate()
        self.assertEqual(result["reason"], "none")

    def test_BootstrapEndToEndImport(self):
        head = self.stageHead()
        self.activate(sysPath=[], sysModules={})  # promote (the subprocess activates the active version)
        script = textwrap.dedent(f"""
            import json, os, sys
            sys.path[:0] = [{os.path.join(SOURCE_DIR, 'Base', 'Python')!r}]
            import studio_overlay
            home = {self.home!r}
            sys.path.append(os.path.join(home, "bin", "..", {ROOT!r}))
            result = studio_overlay.activate(home, {self.stateRoot!r}, {{"installed": True, "interactive": False, "pid": os.getpid()}}, env={{}})
            # What the module factory of Slicer does: the module in sys.modules, else the installed file
            module = sys.modules.get("Studio")
            assert hasattr(module, "Studio"), "the factory would load the installed file"
            import StudioLib.thing
            print(json.dumps({{"state": result["state"], "studio": module.__file__, "version": module.VERSION,
                               "thing": StudioLib.thing.__file__, "value": StudioLib.thing.VALUE, "icon": module.icon(),
                               "mapped": studio_overlay.mapPath(os.path.join(home, {ROOT!r}, "Studio.py"))}}))
            """)
        output = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(output.returncode, 0, output.stderr)
        values = json.loads(output.stdout)
        overlayRoot = os.path.join(self.baseDir(), "versions", head, "files", ROOT)
        self.assertEqual(values["state"], "active")
        self.assertEqual((values["version"], values["value"]), ("H", "H"))
        self.assertEqual(values["studio"], os.path.join(overlayRoot, "Studio.py"))
        self.assertEqual(values["thing"], os.path.join(overlayRoot, "StudioLib", "thing.py"))
        self.assertEqual(values["icon"], os.path.join(overlayRoot, "Resources", "Icons", "a.svg"))
        self.assertTrue(os.path.isfile(values["icon"]))
        self.assertEqual(values["mapped"], os.path.join(overlayRoot, "Studio.py"))

    #
    # Rollback
    #

    def deadPid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def test_RollbackOfStartThatDied(self):
        first = self.stageHead()
        # A start of the first version that died before it was confirmed
        self.activate(pid=self.deadPid())
        self.assertEqual(self.state()["active"], first)
        second = self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H2'\n"})
        # (the dead start above is found now and goes back before the second version is promoted)
        result, _path, _modules = self.activate(pid=self.deadPid())
        self.assertEqual(result["rolledBack"], {"from": first, "to": "installed", "at": result["rolledBack"]["at"]})
        self.assertIn(first, self.state()["bad"])
        self.assertIsNone(result["active"])
        # The bad version is not staged again
        self.assertEqual(self.check()["result"], "ready")  # the second one
        self.assertEqual(self.state()["pending"], second)
        # The second start of the second version confirms; then a third version dies: back to the second
        result, _path, _modules = self.activate()
        self.assertEqual(result["active"], second)
        self.assertTrue(studio_overlay.confirmStart(True))
        third = self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H3'\n"})
        self.assertEqual(self.check()["result"], "ready")
        result, _path, _modules = self.activate(pid=self.deadPid())
        self.assertEqual((result["active"], result["promoted"]), (third, third))
        self.assertEqual(self.state()["previous"], second)
        result, _path, _modules = self.activate()
        self.assertEqual(result["rolledBack"]["from"], third)
        self.assertEqual(result["rolledBack"]["to"], second)
        self.assertEqual(result["active"], second)

    def test_LiveStartIsNotRolledBack(self):
        head = self.stageHead()
        self.activate()  # this process: alive and not confirmed yet
        result, _path, _modules = self.activate()
        self.assertIsNone(result["rolledBack"])
        self.assertEqual(result["active"], head)
        self.assertEqual(len(self.state()["boots"]), 2)
        # A non-interactive run is not recorded
        self.activate(interactive=False)
        self.assertEqual(len(self.state()["boots"]), 2)

    def test_GoBackAndGarbageCollection(self):
        first = self.stageHead()
        self.activate()
        studio_overlay.confirmStart(True)
        second = self.change({"Modules/Scripted/Studio/StudioLib/thing.py": "VALUE = 'H2'\n"})
        self.assertEqual(self.check()["result"], "ready")
        self.activate()
        studio_overlay.confirmStart(True)
        self.assertEqual((self.state()["active"], self.state()["previous"]), (second, first))
        # Go back to the previous version
        updates_commit.requestRollback(self.stateRoot, self.base, first)
        result, _path, _modules = self.activate()
        self.assertEqual(result["active"], first)
        self.assertIn(second, self.state()["skipped"])
        self.assertIsNone(self.state()["previous"])
        self.assertEqual(self.check()["result"], "skipped")
        # Go back to the installed version
        updates_commit.requestRollback(self.stateRoot, self.base, None)
        result, _path, _modules = self.activate()
        self.assertEqual(result["state"], "inactive")
        self.assertIsNone(self.state()["active"])
        # Garbage collection keeps versions in use by a running start only
        versions = os.path.join(self.baseDir(), "versions")
        self.assertEqual(sorted(os.listdir(versions)), sorted([first, second]))
        state = self.state()
        state["boots"] = [{"version": first, "pid": os.getpid(), "token": None, "startedAt": time.time(), "confirmed": True},
                          {"version": second, "pid": self.deadPid(), "token": None, "startedAt": time.time(), "confirmed": True}]
        studio_overlay.writeState(self.baseDir(), state)
        updates_commit.collectGarbage(self.stateRoot, self.base)
        self.assertEqual(os.listdir(versions), [first])
        # Other builds unused for long are removed, recent ones kept
        old = os.path.join(self.stateRoot, "1" * 40)
        recent = os.path.join(self.stateRoot, "2" * 40)
        for folder in (old, recent):
            os.makedirs(folder)
            studio_overlay.writeState(folder, studio_overlay.newState(os.path.basename(folder)))
        past = time.time() - updates_commit.UNUSED_BASE_S - 3600
        os.utime(os.path.join(old, "state.json"), (past, past))
        os.utime(old, (past, past))
        updates_commit.collectGarbage(self.stateRoot, self.base)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))

    def test_DamagedStagedFileRepaired(self):
        head = self.stageHead()
        self.activate()
        staged = os.path.join(self.baseDir(), "versions", head, "files", ROOT, "StudioLib", "thing.py")
        with open(staged, "w") as f:
            f.write("damaged\n")
        before = len(self.server.blobRequests())
        result = self.check()
        self.assertEqual((result["result"], result.get("already"), result.get("repaired")), ("ready", "active", 1), result)
        self.assertEqual(_read(staged), "VALUE = 'H'\n")
        self.assertEqual(len(self.server.blobRequests()) - before, 1)
        # Nothing to repair: nothing downloaded
        self.assertEqual(self.check().get("repaired"), 0)
        self.assertEqual(len(self.server.blobRequests()) - before, 1)

    def test_MainBackToBuildRetiresOverlay(self):
        head = self.stageHead()
        self.activate()
        self.assertEqual(self.state()["active"], head)
        # main is reverted to the build's commit content: only ignored changes remain
        self.repo.commit(dict(BASE_FILES, **{"Docs/user_guide/a.md": "doc 3\n"}), "Revert")
        self.assertEqual(self.check()["result"], "upToDate")
        self.assertEqual(self.state()["pending"], "installed")
        result, _path, _modules = self.activate()
        self.assertEqual(result["state"], "inactive")
        self.assertIsNone(self.state()["active"])

    #
    # Releases
    #

    def test_ReleaseInterplay(self):
        head = self.stageHead()
        self.activate()
        self.assertFalse(updates_commit.clearForRelease(self.stateRoot, self.base))
        state = self.state()
        self.assertEqual((state["disabled"], state["disabledReason"]), (True, "release"))
        result, _path, _modules = self.activate()
        self.assertEqual(result["reason"], "disabled")
        # The install did not happen: restored
        self.assertTrue(updates_commit.restoreAfterRelease(self.stateRoot, self.base))
        self.assertFalse(self.state()["disabled"])
        result, _path, _modules = self.activate()
        self.assertEqual(result["active"], head)
        # Turned off by the user before the release: stays off after a failed install
        updates_commit.setDisabled(self.stateRoot, self.base, True)
        self.assertTrue(updates_commit.clearForRelease(self.stateRoot, self.base))
        updates_commit.restoreAfterRelease(self.stateRoot, self.base)
        self.assertEqual((self.state()["disabled"], self.state()["disabledReason"]), (True, "setting"))
        # The release was installed: everything of the old build is removed
        self.assertTrue(updates_commit.forgetBase(self.stateRoot, self.base))
        self.assertFalse(os.path.exists(self.baseDir()))

    def test_BuildVersionUsedForReleases(self):
        try:
            updates.buildInfoOverride = {"commit": self.base, "studioVersion": "0.9.0"}
            original = updates.versionModule.STUDIO_VERSION
            updates.versionModule.STUDIO_VERSION = "9.9.9"  # an overlaid version.py with a raised version
            self.assertEqual(updates.currentVersion(), "0.9.0")
            manifest = updates.parseManifest(json.dumps({"version": "1.0.0", "assets": {}}))
            self.assertTrue(updates.evaluate(manifest, updates.currentVersion()).available)
            updates.buildInfoOverride = {"commit": None, "studioVersion": "not a version"}
            self.assertEqual(updates.currentVersion(), "9.9.9")
        finally:
            updates.buildInfoOverride = None
            updates.versionModule.STUDIO_VERSION = original

    def test_RelaunchCommand(self):
        self.assertEqual(updates.relaunchCommand(self.home, "linux-x86_64"), [os.path.join(self.home, "Slicer")])
        bundle = os.path.join(self.tmp, "Slicer Studio.app")
        os.makedirs(os.path.join(bundle, "Contents"))
        self.assertEqual(updates.relaunchCommand(os.path.join(bundle, "Contents"), "macos-arm64"), ["open", "-n", os.path.realpath(bundle)])
        self.assertIsNone(updates.relaunchCommand("", "linux-x86_64"))

    def test_Texts(self):
        text = updates.commitReadyText({"head": "a" * 40, "committed": "2026-11-16T10:00:00Z"})
        self.assertTrue(text.startswith("Update ready · main @ aaaaaaa ("), text)
        text = updates.commitNeedsBuildText({"needsBuild": 3, "paths": ["Base/QTCore/x.cxx"]})
        self.assertIn("3 files, for example Base/QTCore/x.cxx", text)
        self.assertIn("next release", text)

    #
    # make_build_info.py
    #

    def test_MakeBuildInfo(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        source = os.path.join(self.tmp, "source")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        run = lambda *args: subprocess.run(["git", "-C", source, *args], check=True, capture_output=True, env=env).stdout.decode().strip()
        os.makedirs(source)
        run("init", "-q")
        for path, text in BASE_FILES.items():
            target = os.path.join(source, path)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as f:
                f.write(text)
        os.makedirs(os.path.join(source, "Modules", "Scripted", "Studio", "StudioLib"), exist_ok=True)
        with open(os.path.join(source, "Modules", "Scripted", "Studio", "StudioLib", "version.py"), "w") as f:
            f.write('STUDIO_VERSION = "1.2.3"\n')
        run("add", "-A")
        run("commit", "-qm", "base")
        records = os.path.join(self.tmp, "records.tsv")
        with open(records, "w") as f:
            for src, (dst, module) in MAP.items():
                root = ROOT if dst.startswith(ROOT) else PYROOT
                f.write(f"{src}\t{root}\t{dst[len(root) + 1:]}\t{module or ''}\n")
            f.write(f"Modules/Scripted/Studio/StudioLib/thing.py\t{ROOT}\tStudioLib/thing.py\tStudio\n")  # duplicate, not a module
        out = os.path.join(self.tmp, "out")
        self.assertEqual(make_build_info.main(["--source", source, "--records", records, "--out", out, "--scripted-root", ROOT]), 0)
        info = json.loads(_read(os.path.join(out, "studio_build_info.json")))
        self.assertEqual(info["commit"], run("rev-parse", "HEAD"))
        self.assertEqual(info["tree"], run("rev-parse", "HEAD^{tree}"))
        self.assertFalse(info["dirty"])
        self.assertEqual(info["studioVersion"], "1.2.3")
        with gzip.open(os.path.join(out, "studio_build_files.json.gz")) as f:
            files = json.load(f)
        self.assertEqual(len(files["map"]), len(MAP))
        self.assertEqual({entry["src"]: entry.get("module") for entry in files["map"] if entry.get("module")},
                         {"Modules/Scripted/Studio/Studio.py": "Studio", "Modules/Scripted/Other/Other.py": "Other"})
        for name in ("studio_build_info.json", "studio_build_files.json.gz"):
            with open(os.path.join(out, name), "rb") as f:
                raw = f.read()
            text = gzip.decompress(raw).decode() if name.endswith(".gz") else raw.decode()
            self.assertNotIn(self.tmp, text)
            self.assertNotIn(source, text)
        # The listing rebuilds the tree of the commit: the application accepts it
        home = os.path.join(self.tmp, "home2")
        shutil.copytree(out, os.path.join(home, PYROOT))
        self.assertEqual(len(updates_commit.loadBuildFiles(home, studio_overlay.readBuildInfo(home)).listing), len(BASE_FILES) + 1)
        # Unchanged content: the files are not rewritten
        mtime = os.stat(os.path.join(out, "studio_build_files.json.gz")).st_mtime_ns
        time.sleep(0.01)
        make_build_info.main(["--source", source, "--records", records, "--out", out, "--scripted-root", ROOT])
        self.assertEqual(os.stat(os.path.join(out, "studio_build_files.json.gz")).st_mtime_ns, mtime)
        # A changed tracked file, or a recorded file that is not tracked: dirty
        with open(os.path.join(source, "Base", "Python", "slicer", "util.py"), "a") as f:
            f.write("# changed\n")
        make_build_info.main(["--source", source, "--records", records, "--out", out])
        self.assertTrue(json.loads(_read(os.path.join(out, "studio_build_info.json")))["dirty"])
        run("checkout", "--", ".")
        with open(records, "a") as f:
            f.write(f"Modules/Scripted/Studio/StudioLib/untracked.py\t{ROOT}\tStudioLib/untracked.py\t\n")
        with open(os.path.join(source, "Modules", "Scripted", "Studio", "StudioLib", "untracked.py"), "w") as f:
            f.write("")
        make_build_info.main(["--source", source, "--records", records, "--out", out])
        self.assertTrue(json.loads(_read(os.path.join(out, "studio_build_info.json")))["dirty"])
        # Without git: no commit
        noGit = os.path.join(self.tmp, "nogit")
        os.makedirs(noGit)
        make_build_info.main(["--source", noGit, "--out", out])
        self.assertIsNone(json.loads(_read(os.path.join(out, "studio_build_info.json")))["commit"])

    #
    # Hygiene
    #

    def test_NoGitHubApi(self):
        for path in ("Modules/Scripted/Studio/StudioLib/updates_git.py", "Modules/Scripted/Studio/StudioLib/updates_commit.py",
                     "Base/Python/studio_overlay.py", "Utilities/Scripts/SlicerStudioRelease/make_build_info.py",
                     "CMake/SlicerStudioBuildInfo.cmake"):
            with open(os.path.join(SOURCE_DIR, path), encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("api.github.com", text, path)
            self.assertNotIn("codeload", text, path)


def runTest():
    suite = unittest.TestLoader().loadTestsFromTestCase(StudioCommitUpdatesTest)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    unittest.main(verbosity=2)
