"""Version of Slicer Studio, and how versions are parsed and compared (A-08, decision D-U1).

``STUDIO_VERSION`` is the one place where the version of Slicer Studio is written. The macOS disk image script
(``Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh``), the release script
(``Utilities/Scripts/SlicerStudioRelease/make_release.py``) and the web launcher read it from this file with a
regular expression, so keep it a plain string assignment on one line.

Versions are semantic versions ``MAJOR.MINOR.PATCH`` with an optional pre-release (``1.2.0-rc.1``) and build
metadata (``+abc``, ignored when comparing), optionally written with a leading ``v`` (the tag ``v1.2.0``). A
pre-release sorts before its release, as in https://semver.org.

This module has no Qt or Slicer import: the release script and the tests use it as plain Python.
"""

import re

#: Version of this Slicer Studio (semantic version). Raise it for every release (make_release.py checks it).
STUDIO_VERSION = "0.9.0"

_VERSION_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


class Version:
    """A parsed semantic version; instances compare by precedence (build metadata is ignored)."""

    __slots__ = ("build", "major", "minor", "patch", "prerelease")

    def __init__(self, major, minor, patch, prerelease=(), build=""):
        self.major = int(major)
        self.minor = int(minor)
        self.patch = int(patch)
        self.prerelease = tuple(prerelease)
        self.build = build or ""

    def _key(self):
        # A release sorts after its pre-releases; numeric identifiers sort before alphanumeric ones
        pre = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in self.prerelease)
        return (self.major, self.minor, self.patch, 1 if not self.prerelease else 0, pre)

    def __eq__(self, other):
        return isinstance(other, Version) and self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __le__(self, other):
        return self._key() <= other._key()

    def __gt__(self, other):
        return self._key() > other._key()

    def __ge__(self, other):
        return self._key() >= other._key()

    def __hash__(self):
        return hash(self._key())

    def __str__(self):
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            text += "-" + ".".join(self.prerelease)
        if self.build:
            text += "+" + self.build
        return text

    def __repr__(self):
        return f"Version('{self}')"


def parse(text):
    """Return the ``Version`` of ``text`` ("1.2.0", "v1.2.0-rc.1"), or raise ``ValueError``."""
    if isinstance(text, Version):
        return text
    match = _VERSION_PATTERN.match(str(text).strip()) if text is not None else None
    if match is None:
        raise ValueError(f"not a version: {text!r}")
    pre = match.group("pre")
    return Version(match.group("major"), match.group("minor"), match.group("patch"), pre.split(".") if pre else (), match.group("build") or "")


def tryParse(text):
    """``parse(text)``, or None if ``text`` is not a version."""
    try:
        return parse(text)
    except (TypeError, ValueError):
        return None


def isValid(text):
    return tryParse(text) is not None


def compare(first, second):
    """-1, 0 or 1 as ``first`` is older than, the same as or newer than ``second`` (both versions or strings)."""
    first, second = parse(first), parse(second)
    return (first > second) - (first < second)


def isNewer(candidate, current):
    """True if ``candidate`` is a newer version than ``current``."""
    return compare(candidate, current) > 0


def studioVersion():
    """The version of this Slicer Studio, as a string."""
    return STUDIO_VERSION


def readVersionFile(path):
    """Read ``STUDIO_VERSION`` from a ``version.py`` file without importing it (for scripts). None if not found."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    match = re.search(r'^STUDIO_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None
