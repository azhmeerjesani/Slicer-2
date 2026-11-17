"""A small, read-only git client for the commit updates of Slicer Studio (``updates_commit``): the git "smart HTTP"
protocol version 2 (pkt-lines, ``ls-refs``, ``fetch`` with side-band), a pack parser and git tree objects.

Everything is checked by its git object id (SHA-1 of ``"<type> <size>\\0" + data``): a commit or tree object is accepted
only under the id it hashes to, so a listing of files walked from a commit id is exactly the listing of that commit.

No Qt, no Slicer and no network code here: ``updates_commit`` sends the requests (HTTPS to github.com only, never the
GitHub API) and hands the answers to these functions. The tests use them with local data and with ``git`` itself.
"""

import hashlib
import re
import struct
import zlib

#: Largest payload of one pkt-line (the protocol allows 65520 bytes with the 4 length bytes)
PKT_MAX_PAYLOAD = 65516

FLUSH = "flush"
DELIM = "delim"
RESPONSE_END = "response-end"

OBJ_COMMIT, OBJ_TREE, OBJ_BLOB, OBJ_TAG, OBJ_OFS_DELTA, OBJ_REF_DELTA = 1, 2, 3, 4, 6, 7
TYPE_NAMES = {OBJ_COMMIT: "commit", OBJ_TREE: "tree", OBJ_BLOB: "blob", OBJ_TAG: "tag"}

#: Limits of a pack (a pack of the trees of one commit of this repository is about 220 KB)
MAX_PACK_BYTES = 32 * 1024 * 1024
MAX_PACK_OBJECTS = 200000
MAX_INFLATED_BYTES = 64 * 1024 * 1024
#: Deepest delta chain followed
MAX_DELTA_DEPTH = 10000
#: Deepest folder nesting of a tree
MAX_TREE_DEPTH = 64

MODE_FILE = "100644"
MODE_EXECUTABLE = "100755"
MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"
MODE_TREE = "40000"
REGULAR_MODES = (MODE_FILE, MODE_EXECUTABLE)

_OID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GitProtocolError(Exception):
    """An answer of the server that is not what the protocol allows, a pack that is damaged or too large, or an object
    whose content does not hash to its id."""


def isObjectId(text):
    return isinstance(text, str) and _OID_PATTERN.match(text) is not None


def objectId(kind, data):
    """The git object id (hexadecimal SHA-1) of an object of type ``kind`` ("blob", "tree", "commit") with ``data``."""
    digest = hashlib.sha1()
    digest.update(f"{kind} {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def blobId(data):
    return objectId("blob", data)


#
# pkt-line
#


def pktLine(payload):
    """One pkt-line: four hexadecimal length digits (including themselves), then ``payload`` (bytes or str)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if len(payload) > PKT_MAX_PAYLOAD:
        raise ValueError("pkt-line payload too long")
    return b"%04x" % (len(payload) + 4) + payload


def flushPkt():
    return b"0000"


def delimPkt():
    return b"0001"


def readPktLines(data):
    """The pkt-lines of ``data``: a list of bytes payloads, and ``FLUSH``, ``DELIM`` or ``RESPONSE_END`` markers."""
    items = []
    position = 0
    size = len(data)
    while position < size:
        if size - position < 4:
            raise GitProtocolError("truncated pkt-line")
        header = data[position:position + 4]
        if not all(c in b"0123456789abcdefABCDEF" for c in header):
            raise GitProtocolError("invalid pkt-line length")
        length = int(header, 16)
        position += 4
        if length == 0:
            items.append(FLUSH)
            continue
        if length == 1:
            items.append(DELIM)
            continue
        if length == 2:
            items.append(RESPONSE_END)
            continue
        if length < 4 or position + length - 4 > size:
            raise GitProtocolError("truncated pkt-line")
        items.append(data[position:position + length - 4])
        position += length - 4
    return items


def _text(payload):
    try:
        return payload.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError:
        raise GitProtocolError("invalid text in the answer")


#
# Capabilities, ls-refs, fetch
#


def parseCapabilities(data):
    """The capability advertisement of ``GET <repository>/info/refs?service=git-upload-pack`` with ``Git-Protocol:
    version=2``: a dict of capability name -> value (None without a value). Raise ``GitProtocolError`` unless the server
    speaks protocol version 2.
    """
    items = readPktLines(data)
    # Smart HTTP puts "# service=git-upload-pack" and a flush first
    if items and isinstance(items[0], bytes) and items[0].startswith(b"# service="):
        items = items[1:]
        if items and items[0] == FLUSH:
            items = items[1:]
    lines = [_text(item) for item in items if isinstance(item, bytes)]
    if not lines or lines[0] != "version 2":
        raise GitProtocolError("the server does not speak git protocol version 2")
    capabilities = {}
    for line in lines[1:]:
        name, _sep, value = line.partition("=")
        capabilities[name] = value if _sep else None
    return capabilities


def checkCapabilities(capabilities):
    """Raise ``GitProtocolError`` unless the server offers ``ls-refs`` and ``fetch`` with ``shallow`` and ``filter`` on
    SHA-1 object ids."""
    if "ls-refs" not in capabilities:
        raise GitProtocolError("the server does not offer ls-refs")
    features = (capabilities.get("fetch") or "").split()
    if "fetch" not in capabilities or "shallow" not in features or "filter" not in features:
        raise GitProtocolError("the server does not offer a shallow, filtered fetch")
    objectFormat = capabilities.get("object-format")
    if objectFormat not in (None, "sha1"):
        raise GitProtocolError("the repository does not use SHA-1 object ids")


def _commandRequest(command, agent, arguments):
    parts = [pktLine(f"command={command}\n")]
    if agent:
        parts.append(pktLine(f"agent={agent}\n"))
    parts.append(pktLine("object-format=sha1\n"))
    parts.append(delimPkt())
    parts.extend(pktLine(argument + "\n") for argument in arguments)
    parts.append(flushPkt())
    return b"".join(parts)


def lsRefsRequest(refPrefix, agent=None):
    """The body of ``POST <repository>/git-upload-pack`` that lists the refs starting with ``refPrefix``."""
    return _commandRequest("ls-refs", agent, [f"ref-prefix {refPrefix}"])


def parseLsRefs(data):
    """The answer of ``ls-refs``: a dict of ref name -> object id."""
    refs = {}
    for item in readPktLines(data):
        if item in (FLUSH, RESPONSE_END):
            break
        if not isinstance(item, bytes):
            raise GitProtocolError("unexpected section in the ls-refs answer")
        line = _text(item)
        if line.startswith("ERR "):
            raise GitProtocolError("the server answered: " + line[4:200])
        fields = line.split(" ")
        if len(fields) < 2 or not isObjectId(fields[0]):
            # "unborn HEAD" and other entries without an object id
            continue
        refs[fields[1]] = fields[0]
    return refs


def fetchRequest(want, agent=None, depth=1, blobless=True):
    """The body of a ``fetch`` of the commit ``want``: only that commit (``deepen``), without blobs (``filter
    blob:none``), no progress messages."""
    if not isObjectId(want):
        raise ValueError("not an object id")
    arguments = ["no-progress", "ofs-delta"]
    if depth:
        arguments.append(f"deepen {int(depth)}")
    if blobless:
        arguments.append("filter blob:none")
    arguments += [f"want {want}", "done"]
    return _commandRequest("fetch", agent, arguments)


def parseFetchResponse(data, maxPackBytes=MAX_PACK_BYTES):
    """The answer of ``fetch``: (the shallow commit ids, the pack bytes). Side-band 1 is pack data, 2 progress (ignored)
    and 3 an error."""
    items = readPktLines(data)
    shallow = []
    pack = []
    packSize = 0
    section = None
    index = 0
    while index < len(items):
        item = items[index]
        index += 1
        if item == FLUSH or item == RESPONSE_END:
            if section == "packfile":
                break
            section = None
            continue
        if item == DELIM:
            section = None
            continue
        if section is None:
            name = _text(item)
            if name.startswith("ERR "):
                raise GitProtocolError("the server answered: " + name[4:200])
            if name not in ("acknowledgments", "shallow-info", "wanted-refs", "packfile-uris", "packfile"):
                raise GitProtocolError("unknown section in the fetch answer")
            if name == "packfile-uris":
                raise GitProtocolError("the server sent pack file addresses, which are not supported")
            section = name
            continue
        if section == "shallow-info":
            line = _text(item)
            if line.startswith("shallow "):
                shallow.append(line[len("shallow "):].strip())
            continue
        if section == "packfile":
            if not item:
                continue
            band = item[0]
            if band == 1:
                packSize += len(item) - 1
                if packSize > maxPackBytes:
                    raise GitProtocolError("the pack is too large")
                pack.append(item[1:])
            elif band == 3:
                raise GitProtocolError("the server reported an error: " + item[1:200].decode("utf-8", "replace").strip())
            elif band != 2:
                raise GitProtocolError("unknown side-band channel")
            continue
        # acknowledgments, wanted-refs: nothing to do (no "have" was sent)
    if not pack:
        raise GitProtocolError("the fetch answer has no pack")
    return shallow, b"".join(pack)


#
# Pack
#


def _inflate(data, position, expectedSize, budget):
    """Inflate the zlib stream at ``position``; return (bytes, position after the stream). At most ``expectedSize``
    bytes are produced (more is an error), and never more than ``budget``."""
    if expectedSize > budget:
        raise GitProtocolError("the pack inflates to too many bytes")
    inflater = zlib.decompressobj()
    output = []
    produced = 0
    consumed = position
    pending = b""
    while not inflater.eof:
        if pending:
            chunk = pending
        else:
            chunk = data[consumed:consumed + 65536]
            if not chunk:
                raise GitProtocolError("truncated pack")
            consumed += len(chunk)
        try:
            out = inflater.decompress(chunk, expectedSize + 1 - produced)
        except zlib.error:
            raise GitProtocolError("damaged pack data")
        pending = inflater.unconsumed_tail
        produced += len(out)
        if produced > expectedSize:
            raise GitProtocolError("a pack object is larger than announced")
        output.append(out)
    end = consumed - len(inflater.unused_data)
    result = b"".join(output)
    if len(result) != expectedSize:
        raise GitProtocolError("a pack object is smaller than announced")
    return result, end


def _deltaSize(delta, position):
    size = 0
    shift = 0
    while True:
        if position >= len(delta):
            raise GitProtocolError("damaged delta")
        byte = delta[position]
        position += 1
        size |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return size, position
        if shift > 63:
            raise GitProtocolError("damaged delta")


def applyDelta(base, delta, maxSize):
    """The object made by applying the git ``delta`` to ``base``."""
    sourceSize, position = _deltaSize(delta, 0)
    targetSize, position = _deltaSize(delta, position)
    if sourceSize != len(base):
        raise GitProtocolError("a delta does not match its base")
    if targetSize > maxSize:
        raise GitProtocolError("the pack inflates to too many bytes")
    output = bytearray()
    length = len(delta)
    while position < length:
        opcode = delta[position]
        position += 1
        if opcode & 0x80:
            offset = size = 0
            for bit in range(4):
                if opcode & (1 << bit):
                    if position >= length:
                        raise GitProtocolError("damaged delta")
                    offset |= delta[position] << (8 * bit)
                    position += 1
            for bit in range(3):
                if opcode & (1 << (4 + bit)):
                    if position >= length:
                        raise GitProtocolError("damaged delta")
                    size |= delta[position] << (8 * bit)
                    position += 1
            if size == 0:
                size = 0x10000
            if offset + size > len(base):
                raise GitProtocolError("damaged delta")
            output += base[offset:offset + size]
        elif opcode:
            if position + opcode > length:
                raise GitProtocolError("damaged delta")
            output += delta[position:position + opcode]
            position += opcode
        else:
            raise GitProtocolError("damaged delta")
        if len(output) > targetSize:
            raise GitProtocolError("damaged delta")
    if len(output) != targetSize:
        raise GitProtocolError("damaged delta")
    return bytes(output)


def parsePack(data, maxObjects=MAX_PACK_OBJECTS, maxInflated=MAX_INFLATED_BYTES, maxPackBytes=MAX_PACK_BYTES):
    """The objects of the pack ``data``: a dict of object id -> (type name, bytes). Every object is hashed, so its id is
    the id of its content. Deltas (by offset or by id) are resolved within the pack. Raise ``GitProtocolError``."""
    if len(data) > maxPackBytes:
        raise GitProtocolError("the pack is too large")
    if len(data) < 32 or data[:4] != b"PACK":
        raise GitProtocolError("not a pack")
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        raise GitProtocolError("unsupported pack version")
    if count > maxObjects:
        raise GitProtocolError("the pack has too many objects")
    if hashlib.sha1(data[:-20]).digest() != data[-20:]:
        raise GitProtocolError("the pack checksum is wrong")
    body = data[:-20]
    entries = {}  # offset -> (type, payload, base): base is an offset (OFS) or an object id (REF)
    order = []
    position = 12
    budget = maxInflated
    for _index in range(count):
        offset = position
        if position >= len(body):
            raise GitProtocolError("truncated pack")
        byte = body[position]
        position += 1
        kind = (byte >> 4) & 7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            if position >= len(body) or shift > 60:
                raise GitProtocolError("damaged pack")
            byte = body[position]
            position += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        base = None
        if kind == OBJ_OFS_DELTA:
            if position >= len(body):
                raise GitProtocolError("truncated pack")
            byte = body[position]
            position += 1
            distance = byte & 0x7F
            while byte & 0x80:
                if position >= len(body):
                    raise GitProtocolError("truncated pack")
                byte = body[position]
                position += 1
                distance = ((distance + 1) << 7) | (byte & 0x7F)
            base = offset - distance
            if base < 12 or base >= offset:
                raise GitProtocolError("damaged delta reference")
        elif kind == OBJ_REF_DELTA:
            if position + 20 > len(body):
                raise GitProtocolError("truncated pack")
            base = body[position:position + 20].hex()
            position += 20
        elif kind not in TYPE_NAMES:
            raise GitProtocolError("unknown pack object type")
        payload, position = _inflate(body, position, size, budget)
        budget -= len(payload)
        entries[offset] = (kind, payload, base)
        order.append(offset)
    if position != len(body):
        raise GitProtocolError("unexpected data after the pack objects")

    objects = {}
    resolved = {}  # offset -> (type name, data)

    def resolve(offset):
        # Iterative: follow the chain of bases down to an object that is not a delta (or one already resolved), then
        # apply the deltas back up
        chain = []
        current = offset
        while current not in resolved:
            kind, payload, base = entries[current]
            if kind in TYPE_NAMES:
                resolved[current] = (TYPE_NAMES[kind], payload)
                break
            chain.append(current)
            if len(chain) > MAX_DELTA_DEPTH:
                raise GitProtocolError("delta chain too long")
            if kind == OBJ_OFS_DELTA:
                if base not in entries:
                    raise GitProtocolError("damaged delta reference")
                current = base
            else:
                current = byId.get(base)
                if current is None:
                    raise GitProtocolError("a delta base is missing from the pack")
            if current in chain:
                raise GitProtocolError("damaged delta reference")
        for deltaOffset in reversed(chain):
            baseType, baseData = resolved[current]
            resolved[deltaOffset] = (baseType, applyDelta(baseData, entries[deltaOffset][1], maxInflated))
            current = deltaOffset
        return resolved[offset]

    # Ids of the objects that are not deltas first: REF_DELTA bases are looked up by id
    byId = {}
    for offset in order:
        kind, payload, _base = entries[offset]
        if kind in TYPE_NAMES:
            oid = objectId(TYPE_NAMES[kind], payload)
            byId[oid] = offset
    pendingRefs = [offset for offset in order if entries[offset][0] in (OBJ_OFS_DELTA, OBJ_REF_DELTA)]
    # A REF_DELTA may name a base that is itself a delta: resolve in rounds until nothing changes
    progress = True
    total = 0
    while pendingRefs and progress:
        progress = False
        remaining = []
        for offset in pendingRefs:
            kind, _payload, base = entries[offset]
            if kind == OBJ_REF_DELTA and base not in byId:
                remaining.append(offset)
                continue
            typeName, content = resolve(offset)
            total += len(content)
            if total > maxInflated:
                raise GitProtocolError("the pack inflates to too many bytes")
            byId[objectId(typeName, content)] = offset
            progress = True
        pendingRefs = remaining
    if pendingRefs:
        raise GitProtocolError("a delta base is missing from the pack")
    for oid, offset in byId.items():
        objects[oid] = resolve(offset)
    return objects


#
# Commits and trees
#


def parseCommit(data):
    """The fields of a commit object: ``tree``, ``parents``, ``committer`` (the raw line), ``committed`` (seconds since
    the epoch, or None) and ``message``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    header, _sep, message = text.partition("\n\n")
    result = {"tree": None, "parents": [], "committer": "", "committed": None, "message": message}
    for line in header.split("\n"):
        if line.startswith("tree ") and result["tree"] is None:
            result["tree"] = line[5:].strip()
        elif line.startswith("parent "):
            result["parents"].append(line[7:].strip())
        elif line.startswith("committer "):
            result["committer"] = line[10:]
            match = re.search(r"> (\d+) [+-]\d{4}$", line)
            if match:
                result["committed"] = int(match.group(1))
    if not isObjectId(result["tree"]):
        raise GitProtocolError("the commit has no tree")
    return result


def _safeName(name):
    return name not in ("", ".", "..", ".git") and "/" not in name and "\0" not in name and name.lower() != ".git"


def parseTree(data):
    """The entries of a tree object: a list of (mode, name, object id). Unsafe names (``..``, ``.git``, a slash or a
    NUL, not UTF-8) raise ``GitProtocolError``."""
    entries = []
    position = 0
    size = len(data)
    while position < size:
        space = data.find(b" ", position)
        nul = data.find(b"\0", space + 1) if space >= 0 else -1
        if space < 0 or nul < 0 or nul + 21 > size:
            raise GitProtocolError("damaged tree")
        mode = data[position:space].decode("ascii", "replace")
        try:
            name = data[space + 1:nul].decode("utf-8")
        except UnicodeDecodeError:
            raise GitProtocolError("a file name is not UTF-8")
        if not _safeName(name):
            raise GitProtocolError(f"unsafe file name in the tree: {name!r}")
        if mode not in (MODE_FILE, MODE_EXECUTABLE, MODE_SYMLINK, MODE_GITLINK, MODE_TREE):
            raise GitProtocolError("unknown file mode in the tree")
        entries.append((mode, name, data[nul + 1:nul + 21].hex()))
        position = nul + 21
    return entries


def walkTree(objects, treeId, maxEntries=200000):
    """The files of the tree ``treeId`` (recursively): a dict of path -> (mode, object id), as ``git ls-tree -r``.
    Every tree must be in ``objects`` (a dict of id -> (type, data) whose ids are hashes of the content)."""
    listing = {}
    stack = [("", treeId, 0)]
    while stack:
        prefix, oid, depth = stack.pop()
        if depth > MAX_TREE_DEPTH:
            raise GitProtocolError("folders nested too deeply")
        entry = objects.get(oid)
        if entry is None or entry[0] != "tree":
            raise GitProtocolError("a tree is missing from the pack")
        for mode, name, child in parseTree(entry[1]):
            path = prefix + name
            if mode == MODE_TREE:
                stack.append((path + "/", child, depth + 1))
            else:
                listing[path] = (mode, child)
                if len(listing) > maxEntries:
                    raise GitProtocolError("the tree has too many files")
    return listing


def _treeSortKey(item):
    name, isTree = item
    return name.encode("utf-8") + (b"/" if isTree else b"")


def treeIdFromListing(listing):
    """The id of the root tree of a flat listing (path -> (mode, object id)), built the way git builds tree objects. It
    equals the commit's tree id exactly when the listing is the commit's listing."""
    root = {}
    for path, (mode, oid) in listing.items():
        parts = path.split("/")
        node = root
        for part in parts[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(f"a file and a folder have the same path: {path}")
            node = child
        if parts[-1] in node:
            raise ValueError(f"duplicate path: {path}")
        node[parts[-1]] = (mode, oid)

    def build(node):
        items = []
        for name, value in node.items():
            items.append(((name, isinstance(value, dict)), value))
        items.sort(key=lambda item: _treeSortKey(item[0]))
        data = bytearray()
        for (name, isTree), value in items:
            if isTree:
                mode, oid = MODE_TREE, build(value)
            else:
                mode, oid = value
            data += mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(oid)
        return objectId("tree", bytes(data))

    return build(root)


def diffListings(old, new):
    """The changes from listing ``old`` to listing ``new``: a sorted list of (path, old entry or None, new entry or
    None), each entry a (mode, object id)."""
    changes = []
    for path in sorted(set(old) | set(new)):
        before, after = old.get(path), new.get(path)
        if before != after:
            changes.append((path, before, after))
    return changes
