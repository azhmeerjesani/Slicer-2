#!/usr/bin/env python3
"""Check the Mach-O files of a macOS application bundle before it is shipped.

Two checks, both read the files directly (standard library only, so the script
also runs, and is tested, on Linux):

Architecture
    Every Mach-O file (thin or universal) must contain code for the target
    architecture. A file without it runs through Rosetta 2 on Apple Silicon
    (which macOS 28 no longer supports) or does not load at all.

Qt version
    Every bundled Qt framework, every reference to a Qt framework and every Qt
    plugin must belong to the Qt version of the bundled QtCore. Qt refuses a
    plugin built with a newer Qt ("Plugin uses incompatible Qt library"); a Qt
    platform plugin taken from another Qt installation (for example Homebrew's
    next to the official Qt the application is built with) stops the
    application at start-up ("no Qt platform plugin could be initialized").
    The Qt version is read from the "current version" of the framework install
    names and from the QTMETADATA header of each plugin (the same fields
    QFactoryLoader checks).

Usage: check_bundle_binaries.py --app "Slicer Studio.app" --arch arm64
Exit status 1 and one line per problem when a check fails.
"""

import argparse
import fnmatch
import mmap
import os
import re
import struct
import sys

CPU_TYPES = {
    0x01000007: "x86_64",
    0x0100000C: "arm64",
    0x00000007: "i386",
    0x0000000C: "arm",
    0x01000012: "ppc64",
    0x00000012: "ppc",
}
ARCH_CPU_TYPES = {name: cpu for cpu, name in CPU_TYPES.items()}

_THIN_MAGICS = {
    b"\xcf\xfa\xed\xfe": ("<", 64),
    b"\xce\xfa\xed\xfe": ("<", 32),
    b"\xfe\xed\xfa\xcf": (">", 64),
    b"\xfe\xed\xfa\xce": (">", 32),
}
_FAT_MAGIC = b"\xca\xfe\xba\xbe"
_FAT_MAGIC_64 = b"\xca\xfe\xba\xbf"

LC_ID_DYLIB = 0xD
_DYLIB_LOAD_COMMANDS = {0xC, 0x80000018, 0x8000001F, 0x80000023}  # LOAD, WEAK, REEXPORT, UPWARD

QT_PLUGIN_MAGIC = b"QTMETADATA !"
_QT_FRAMEWORK_RE = re.compile(r"(?:^|/)(Qt[A-Za-z0-9]+)\.framework/")


def _slices(data):
    """Return [(cputype, offset)] of the Mach-O slices of data, or None when
    data is not a Mach-O file (a Java class file also starts with 0xcafebabe)."""
    magic = data[:4]
    if magic in _THIN_MAGICS:
        endian, _bits = _THIN_MAGICS[magic]
        (cputype,) = struct.unpack_from(endian + "I", data, 4)
        return [(cputype, 0)]
    if magic in (_FAT_MAGIC, _FAT_MAGIC_64):
        if len(data) < 8:
            return None
        (count,) = struct.unpack_from(">I", data, 4)
        if count == 0 or count > 30:
            return None  # a Java class file (its version numbers), not a universal binary
        entry_size = 32 if magic == _FAT_MAGIC_64 else 20
        slices = []
        for index in range(count):
            position = 8 + index * entry_size
            if position + entry_size > len(data):
                return None
            if magic == _FAT_MAGIC_64:
                cputype, _sub, offset = struct.unpack_from(">IIQ", data, position)
            else:
                cputype, _sub, offset = struct.unpack_from(">III", data, position)
            slices.append((cputype, offset))
        return slices
    return None


def macho_slices(path):
    """[(arch name, slice offset)] of a Mach-O file, or None for other files."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return None
    slices = _slices(head)
    if slices is None:
        return None
    return [(CPU_TYPES.get(cpu, hex(cpu)), offset) for cpu, offset in slices]


def format_version(value):
    return "%d.%d.%d" % (value >> 16, (value >> 8) & 0xFF, value & 0xFF)


def dylib_commands(path, offset):
    """[(command, install name, current version)] of the slice at offset: the
    LC_ID_DYLIB and the dylib load commands."""
    result = []
    with open(path, "rb") as handle:
        handle.seek(offset)
        header = handle.read(32)
        if header[:4] not in _THIN_MAGICS:
            return result
        endian, bits = _THIN_MAGICS[header[:4]]
        ncmds, sizeofcmds = struct.unpack_from(endian + "II", header, 16)
        header_size = 32 if bits == 64 else 28
        handle.seek(offset + header_size)
        commands = handle.read(sizeofcmds)
    position = 0
    for _index in range(ncmds):
        if position + 8 > len(commands):
            break
        cmd, cmdsize = struct.unpack_from(endian + "II", commands, position)
        if cmdsize < 8:
            break
        if cmd == LC_ID_DYLIB or cmd in _DYLIB_LOAD_COMMANDS:
            name_offset, _timestamp, current, _compat = struct.unpack_from(endian + "IIII", commands, position + 8)
            raw = commands[position + name_offset:position + cmdsize]
            name = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
            result.append(("id" if cmd == LC_ID_DYLIB else "load", name, current))
        position += cmdsize
    return result


def qt_plugin_versions(path):
    """[(qt major, qt minor)] of each QTMETADATA header in the file (one per
    architecture slice of a plugin)."""
    versions = []
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                return versions
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                start = 0
                while True:
                    found = data.find(QT_PLUGIN_MAGIC, start)
                    if found < 0:
                        break
                    header = data[found + len(QT_PLUGIN_MAGIC):found + len(QT_PLUGIN_MAGIC) + 4]
                    start = found + 1
                    if len(header) < 4:
                        continue
                    version, major, minor = header[0], header[1], header[2]
                    # QtCore itself holds the magic string (the loader compares
                    # with it), followed by unrelated bytes: keep real headers.
                    if version in (0, 1) and 5 <= major <= 9:
                        versions.append((major, minor))
    except (OSError, ValueError):
        pass
    return versions


def walk_files(app):
    """Every regular file of the bundle; symlinks (for example a framework's
    Versions/Current) are not followed, so no file is seen twice."""
    for directory, dirs, files in os.walk(app):
        dirs.sort()
        for name in sorted(files):
            yield os.path.join(directory, name)


def check(app, arch, allow=(), check_qt=True):
    """Return (problems, warnings, summary)."""
    problems, warnings = [], []
    if arch not in ARCH_CPU_TYPES:
        return ["unknown architecture %r" % arch], warnings, {}
    machos = {}
    for path in walk_files(app):
        slices = macho_slices(path)
        if slices is not None:
            machos[path] = slices

    def rel(path):
        return os.path.relpath(path, app)

    def allowed(path):
        return any(fnmatch.fnmatch(rel(path), pattern) for pattern in allow)

    for path, slices in sorted(machos.items()):
        archs = [name for name, _offset in slices]
        if arch not in archs:
            message = "%s: built for %s, not %s" % (rel(path), " ".join(archs) or "?", arch)
            (warnings if allowed(path) else problems).append(message)

    summary = {"macho_files": len(machos)}
    if not check_qt:
        return problems, warnings, summary

    def host_offset(slices):
        for name, offset in slices:
            if name == arch:
                return offset
        return slices[0][1]

    contents = os.path.join(app, "Contents")
    qtcore = os.path.join(contents, "Frameworks", "QtCore.framework", "Versions", "A", "QtCore")
    qtcore_version = None
    if qtcore in machos:
        for command, _name, current in dylib_commands(qtcore, host_offset(machos[qtcore])):
            if command == "id":
                qtcore_version = current
    if qtcore_version is None:
        warnings.append("Contents/Frameworks/QtCore.framework not found: the Qt version check was skipped")
        return problems, warnings, summary
    qt_major, qt_minor = qtcore_version >> 16, (qtcore_version >> 8) & 0xFF
    summary["qt_version"] = format_version(qtcore_version)

    for path, slices in sorted(machos.items()):
        for command, name, current in dylib_commands(path, host_offset(slices)):
            match = _QT_FRAMEWORK_RE.search(name)
            if match is None:
                continue
            if current == 0:
                continue
            if (current >> 16, (current >> 8) & 0xFF) != (qt_major, qt_minor):
                what = "is" if command == "id" else "references"
                problems.append("%s %s %s %s, but the bundled QtCore is %s" % (
                    rel(path), what, match.group(1), format_version(current), format_version(qtcore_version)))

    plugins = 0
    for path in sorted(machos):
        if os.sep + "Frameworks" + os.sep in path:
            continue
        for major, minor in qt_plugin_versions(path):
            plugins += 1
            if (major, minor) != (qt_major, qt_minor):
                problems.append("%s is a Qt plugin built with Qt %d.%d, but the bundled QtCore is %s "
                                "(Qt refuses it: \"Plugin uses incompatible Qt library\")" % (
                                    rel(path), major, minor, format_version(qtcore_version)))
    summary["qt_plugins"] = plugins
    return problems, warnings, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", required=True, help="the .app bundle")
    parser.add_argument("--arch", required=True, help="target architecture (arm64 or x86_64)")
    parser.add_argument("--allow", action="append", default=[], metavar="GLOB",
                        help="bundle-relative paths whose missing architecture is only a warning")
    parser.add_argument("--no-qt", action="store_true", help="skip the Qt version check")
    options = parser.parse_args(argv)
    if not os.path.isdir(options.app):
        print("check_bundle_binaries: no such bundle: %s" % options.app, file=sys.stderr)
        return 2
    problems, warnings, summary = check(options.app, options.arch, options.allow, not options.no_qt)
    for message in warnings:
        print("warning: " + message)
    for message in problems:
        print("error: " + message)
    print("check_bundle_binaries: %d Mach-O files, Qt %s, %d Qt plugin headers, %d problems" % (
        summary.get("macho_files", 0), summary.get("qt_version", "?"), summary.get("qt_plugins", 0), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
