#!/usr/bin/env python3
r"""Check README.md of Slicer Studio against the source tree and the state of the work packages (A-05).

The README makes many small claims (settings keys, shortcuts, command line options, file paths, what is
implemented). This script rejects the ones that are no longer true. It uses only the Python standard library
and never starts Slicer.

Usage::

  StudioReadmeCheck.py --readme README.md --source . --state wave_state.json [--run-examples] [--json]
  StudioReadmeCheck.py --self-test

``--state`` is the JSON file written by the orchestrator: ``{"<work package id>": "passed" | "failed" |
"deferred" | "pending"}``. The exit code is 1 if anything fails, and 0 otherwise.

Checks (the kind of each failure is in brackets)

- Every relative link and every ``#anchor`` resolves, with GitHub's rules for heading anchors: lower case,
  punctuation removed except ``-`` and ``_``, spaces to ``-``, repeated headings numbered ``-1``, ``-2``
  [``link-file``, ``link-anchor``]. Anchors that other files of the source tree use for the README
  (``README.md#...`` and ``Slicer-2#...``) still resolve [``anchor-external``].
- Code fences are balanced [``fence``]. No ``TODO`` [``todo``]. No banned wording: "localizes", "causal
  circuit", "disease-specific", "specificity confirmed", "Saved" next to autosave, and "validated" or
  "automatic segmentation" next to Smart fill [``banned``].
- Every backticked settings key (``Studio/...``, ``Appearance/...``, ``LNM/...``, ``SegmentEditor/...``,
  ``Views/...``) [``setting-key``], environment variable ``SLICER_...`` [``env-var``], object name
  ``Studio...`` or ``LNM...`` [``object-name``], file path [``path``], ``python -m`` module [``module``] and
  command line flag [``flag``] exists in the source tree. Flags are checked in code spans and in the
  commands of code fences that run a script of this repository, ``PythonSlicer`` or ``./Slicer``.
- Every keyboard shortcut in a code span exists in the source (Python, C++, ``.ui`` files, the user guide)
  [``shortcut``].
- The disclaimer quoted in the README equals ``DISCLAIMER`` in ``LesionNetworkMappingLib/export.py``, which
  is read by parsing the file, not by importing it [``disclaimer``].
- A line marked "Implemented" or "Available" names features (keywords of ``FEATURE_WPS`` below, or work
  package identifiers) whose work packages are all "passed" [``claim-state``, ``claim-unmapped``]. A line
  that names a work package that did not pass says that it is planned, pending or deferred
  [``planned-wording``].
- With ``--run-examples``, the commands of fences whose info string is ``console example`` are run with
  ``--help`` (scripts of this repository and ``python -m LesionNetworkMappingLib.<module>``), and every
  flag of the command must appear in the help text [``example``].
"""

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

#: Work packages of earlier waves that the README may describe as done (they are not in the wave state file)
EARLIER_DONE = {f"W1-0{n}" for n in range(1, 10)} | {"P-01", "P-02"}

#: README keywords (lower case, regular expressions) -> the work packages that implement the feature. A line
#: marked "Implemented" or "Available" that contains one of them is only accepted if all its work packages passed.
FEATURE_WPS = [
    (r"autosave|crash recovery|recover autosaved", ["A-01"]),
    (r"smart fill", ["A-02"]),
    (r"map workspace|map v1|\bmap network\b|\bmap\b workspace", ["W2-06"]),
    (r"command palette|shortcut overlay|in-view probe|in-view readout", ["W3-06"]),
    (r"flat view bar|slice index readout", ["W2-03"]),
    (r"lesion card|tool tiles|streamlined draw", ["W2-05"]),
    (r"pageup|page up|home/end|left-drag|focus follows", ["W3-02a"]),
    (r"inter font|bundled inter", ["W2-02"]),
    (r"guided tour|where did it go", ["T-01"]),
    (r"usability gate", ["U-05"]),
    (r"mode pill", ["U-04"]),
    (r"study schema|job protocol|threshold rules v2|design builder|permutation|reference maps", ["L-01"]),
]

#: Words that make a line about a work package that did not pass acceptable
PLANNED_WORDS = re.compile(r"\bplanned\b|\bplan\b|\bpending\b|\bdeferred\b|\bnot (?:yet )?(?:available|implemented|done)\b|\(wave \d\)|\bwave \d\b", re.IGNORECASE)

BANNED = [
    (re.compile(r"\blocaliz(?:es|e)\b", re.IGNORECASE), "localizes/localize (use 'network associated with')"),
    (re.compile(r"causal circuit", re.IGNORECASE), "causal circuit"),
    (re.compile(r"disease-specific", re.IGNORECASE), "disease-specific"),
    (re.compile(r"specificity confirmed", re.IGNORECASE), "specificity confirmed"),
    (re.compile(r"autosav\w*[^.\n]{0,40}\bSaved\b|\bSaved\b[^.\n]{0,40}autosav", re.DOTALL), "'Saved' next to autosave (say 'Autosaved')"),
    (re.compile(r"smart fill[^.\n]{0,60}\b(?:validated|automatic segmentation)\b", re.IGNORECASE), "Smart fill is assisted tracing"),
    (re.compile(r"\b(?:validated|automatic segmentation)\b[^.\n]{0,60}smart fill", re.IGNORECASE), "Smart fill is assisted tracing"),
]

SETTING_FAMILIES = ("Studio", "Appearance", "LNM", "SegmentEditor", "Views")
#: Flags of other tools that are not defined in this repository
EXTERNAL_FLAGS = {"--help", "--as-needed", "--parallel", "--target", "--branch", "--agree-to-license", "--install-rosetta", "--ignore-words",
                  "--files", "--build", "--version"}
#: Modules run with ``-m`` that are not part of this repository
EXTERNAL_MODULES = {"unittest", "pre_commit", "pip", "venv", "http.server", "json.tool", "pytest"}
PATH_EXTENSIONS = (".py", ".md", ".sh", ".json", ".txt", ".ini", ".cxx", ".h", ".ui", ".yml", ".yaml", ".svg", ".png", ".ttf", ".cmake", ".toml", ".cff")
EXTERNAL_PATH_PREFIXES = ("Slicer-", "~", "/", "$", "%", "bin/", "python-install/", "Contents/", "http", "<", ".", "src/", "dist/")
TEXT_EXTENSIONS = (".py", ".cxx", ".h", ".txx", ".md", ".ui", ".txt", ".cmake", ".sh", ".json", ".ini", ".in", ".yml", ".yaml", ".qrc", ".toml", ".cff", "Dockerfile")
SKIP_DIRECTORIES = {".git", "__pycache__", "node_modules", ".mypy_cache", ".ruff_cache"}
MAX_FILE_BYTES = 2_000_000

MODIFIERS = r"(?:Ctrl|Cmd|Alt|Option|Shift|Meta)"
SHORTCUT = re.compile(rf"^(?:{MODIFIERS}\+)+(?:F\d{{1,2}}|Esc|Enter|Return|Tab|Backspace|Delete|Space|PageUp|PageDown|Home|End|Left|Right|Up|Down|\S)$")
FUNCTION_KEY = re.compile(r"^F\d{1,2}$")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)((?:(?!\n[ \t]*\n).)+?)(?<!`)\1(?!`)", re.DOTALL)
LINK = re.compile(r"(!?)\[((?:[^\]\\]|\\.)*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_DEFINITION = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
FENCE = re.compile(r"^( {0,3}|\s*)(`{3,}|~{3,})\s*(.*)$")
WP_ID = re.compile(r"\b(?:W\d-(?:\d{2}[a-z]?|I\d)|[ALUPT]-\d{2}[a-z]?)\b")


class Failure:
    def __init__(self, kind, line, message):
        self.kind = kind
        self.line = line
        self.message = message

    def __str__(self):
        return f"README.md:{self.line}: [{self.kind}] {self.message}"

    def asDict(self):
        return {"kind": self.kind, "line": self.line, "message": self.message}


#
# Reading the README
#


def maskFences(text):
    """Return ``(masked, fences, problems)``.

    ``masked`` is the text with the lines of the fences blanked (same line numbers). ``fences`` is a list of
    ``(first line, info string, [lines])``. ``problems`` is a list of ``(line, message)`` for unbalanced fences.
    """
    lines = text.split("\n")
    masked = []
    fences = []
    problems = []
    openFence = None
    for number, line in enumerate(lines, 1):
        match = FENCE.match(line)
        if openFence is None:
            if match and not (match.group(2).startswith("`") and "`" in match.group(3)):
                openFence = {"char": match.group(2)[0], "length": len(match.group(2)), "line": number, "info": match.group(3).strip(), "lines": []}
            masked.append(line if openFence is None else "")
            continue
        if match and match.group(2)[0] == openFence["char"] and len(match.group(2)) >= openFence["length"] and not match.group(3).strip():
            fences.append((openFence["line"], openFence["info"], openFence["lines"]))
            openFence = None
        else:
            openFence["lines"].append(line)
        masked.append("")
    if openFence is not None:
        problems.append((openFence["line"], "code fence opened here is never closed"))
        fences.append((openFence["line"], openFence["info"], openFence["lines"]))
    return "\n".join(masked), fences, problems


def stripInline(text):
    """Heading text without Markdown: links to their text, no images, code marks, emphasis marks or HTML tags."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("`", "")
    return text


def slugify(heading):
    """GitHub's anchor of a heading (without the -1, -2 numbering of repeated headings)."""
    text = stripInline(heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchorsOf(text):
    """The set of anchors of a Markdown text (headings with GitHub's numbering, and explicit HTML anchors)."""
    masked, _fences, _problems = maskFences(text)
    anchors = set()
    counts = {}
    for line in masked.split("\n"):
        match = HEADING.match(line)
        if not match:
            continue
        slug = slugify(match.group(2))
        number = counts.get(slug, 0)
        counts[slug] = number + 1
        anchors.add(slug if number == 0 else f"{slug}-{number}")
    for match in re.finditer(r"<a\s+(?:name|id)=[\"']([^\"']+)[\"']", masked):
        anchors.add(match.group(1))
    return anchors


#
# The source tree
#


class Source:
    """Lazy views of the source tree: its files, its text, its C++ text and its key-related strings."""

    def __init__(self, root, exclude=None):
        self.root = os.path.abspath(root)
        #: Relative path of the README under test (and this script, whose self-test holds made-up keys): not evidence
        self.exclude = exclude
        self._files = None
        self._text = None
        self._docs = None
        self._cxx = None
        self._perFile = None
        self._lower = None
        self._own = None

    def files(self):
        if self._files is None:
            self._files = []
            for directory, directories, names in os.walk(self.root):
                directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
                relative = os.path.relpath(directory, self.root)
                for name in names:
                    self._files.append(os.path.normpath(os.path.join(relative, name)).replace(os.sep, "/"))
        return self._files

    def _load(self):
        if self._text is not None:
            return
        parts = []
        docs = []
        cxx = []
        perFile = {}
        for relative in self.files():
            if not relative.endswith(TEXT_EXTENSIONS) or relative == self.exclude or os.path.basename(relative) == os.path.basename(__file__):
                continue
            path = os.path.join(self.root, relative)
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
                with open(path, encoding="utf-8", errors="replace") as stream:
                    content = stream.read()
            except OSError:
                continue
            (docs if relative.endswith(".md") else parts).append(content)
            perFile[relative] = content
            if relative.endswith((".cxx", ".h", ".txx")):
                cxx.append(content)
        self._text = "\n".join(parts)
        self._docs = "\n".join(docs)
        self._cxx = "\n".join(cxx)
        self._perFile = perFile

    def text(self):
        self._load()
        return self._text

    def docsText(self):
        self._load()
        return self._docs

    def cxxText(self):
        self._load()
        return self._cxx

    def perFile(self):
        self._load()
        return self._perFile

    def hasPath(self, relative):
        relative = relative.rstrip("/")
        if os.path.exists(os.path.join(self.root, relative)):
            return True
        suffix = "/" + relative
        return any(name == relative or name.endswith(suffix) for name in self.files()) or any(("/" + name).find(suffix + "/") >= 0 for name in self.files())

    def hasModule(self, module):
        relative = module.replace(".", "/")
        candidates = (relative + ".py", relative + "/__init__.py", relative + "/__main__.py")
        names = self.files()
        for candidate in candidates:
            suffix = "/" + candidate
            if any(name == candidate or name.endswith(suffix) for name in names):
                return True
        return False

    def hasSettingKey(self, key):
        """True if the settings key (or key prefix ending in ``/``) is in the source, also when it is built from a group and a name."""
        text = self.text()
        if key in text:
            return True
        group, _separator, name = key.rpartition("/")
        if not group:
            return False
        if not name:
            return group in text
        # A group constant and a name: ("Studio/Autosave", "IntervalMinutes") or f"{GROUP}/{name}"
        quotedName = re.compile(rf"[\"']{re.escape(name)}[\"']")
        for relative, content in self.perFile().items():
            if not relative.endswith(".md") and group in content and quotedName.search(content):
                return True
        return False

    def ownFlags(self):
        """The text of this script without the made-up keys of its self-test: the options it defines are real."""
        if self._own is None:
            with open(__file__, encoding="utf-8") as stream:
                content = stream.read()
            start = content.index("\n" + "_SELF_TEST_SOURCE = {")
            end = content.index("\n" + "def selfTest():")
            self._own = content[:start] + content[end:]
        return self._own

    def hasFlag(self, flag):
        if flag in EXTERNAL_FLAGS or flag in self.text() or flag in self.ownFlags():
            return True
        name = flag[2:]
        return re.search(rf"[\"']{re.escape(name)}[\"']", self.cxxText()) is not None

    def hasIdentifier(self, name):
        text = self.text()
        position = text.find(name)
        while position >= 0:
            before = text[position - 1] if position > 0 else " "
            after = text[position + len(name)] if position + len(name) < len(text) else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return True
            position = text.find(name, position + 1)
        stems = {os.path.splitext(os.path.basename(path))[0] for path in self.files()}
        return name in stems

    def hasShortcut(self, combo):
        """True if the key combination is in the source: as written, with Cmd for Ctrl and Option for Alt, or as a family such as Ctrl+Alt+{n}."""
        if self._lower is None:
            self._lower = (self.text() + "\n" + self.docsText()).lower()
        text = self._lower
        variants = {combo, combo.replace("Cmd", "Ctrl"), combo.replace("Option", "Alt")}
        for variant in variants:
            lowered = variant.lower()
            for form in (lowered, lowered.replace("\\", "\\\\"), lowered.replace("+", " + ")):
                if form in text:
                    return True
            stem = lowered[: lowered.rfind("+") + 1]
            last = lowered[len(stem):]
            if stem and re.fullmatch(r"\d", last) and re.search(re.escape(stem) + r"(?:\d|\{|%|\"\s*\+|' *\+)", text):
                return True
        return False


#
# Checks
#


def checkLinks(text, masked, readmeDirectory, anchors, failures):
    """Relative links and anchors of the README (outside code)."""
    codeFree = CODE_SPAN.sub(lambda match: " " * len(match.group(0)), masked)
    targets = []
    for match in LINK.finditer(codeFree):
        targets.append((match.start(), match.group(3)))
    for match in REFERENCE_DEFINITION.finditer(codeFree):
        targets.append((match.start(1), match.group(1)))
    for position, target in targets:
        line = text.count("\n", 0, position) + 1
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):
            continue
        path, _separator, fragment = target.partition("#")
        if not path:
            if fragment and fragment not in anchors:
                failures.append(Failure("link-anchor", line, f"anchor #{fragment} does not match any heading"))
            continue
        absolute = os.path.normpath(os.path.join(readmeDirectory, path))
        if not os.path.exists(absolute):
            failures.append(Failure("link-file", line, f"{path} does not exist"))
        elif fragment and absolute.endswith(".md"):
            try:
                with open(absolute, encoding="utf-8") as stream:
                    other = anchorsOf(stream.read())
            except OSError:
                other = set()
            if fragment not in other:
                failures.append(Failure("link-anchor", line, f"{path}#{fragment}: no such heading"))


def checkExternalAnchors(source, anchors, failures):
    """Anchors of the README that other files of the source tree use."""
    pattern = re.compile(r"(?:README\.md|Slicer-2/?)#([A-Za-z0-9_\-]+)")
    for relative, content in source.perFile().items():
        if relative == "README.md" or not relative.endswith((".md", ".py")) or os.path.basename(relative) == os.path.basename(__file__):
            continue
        for match in pattern.finditer(content):
            if match.group(1) not in anchors:
                failures.append(Failure("anchor-external", 0, f"{relative} links to README anchor #{match.group(1)}, which does not exist"))


def codeSpans(text, masked):
    """``[(line, content)]`` of the inline code spans outside fences."""
    spans = []
    for match in CODE_SPAN.finditer(masked):
        content = match.group(2)
        if content.startswith(" ") and content.endswith(" ") and content.strip():
            content = content[1:-1]
        spans.append((text.count("\n", 0, match.start()) + 1, content))
    return spans


def fenceCommands(fences):
    """``[(line, command text)]``: the commands of the fences, with the lines joined at backslashes."""
    commands = []
    for firstLine, _info, lines in fences:
        current = ""
        start = 0
        for offset, line in enumerate(lines):
            if not current:
                start = firstLine + 1 + offset
            stripped = line.rstrip()
            if stripped.endswith("\\"):
                current += stripped[:-1] + " "
                continue
            current += stripped
            if current.strip():
                commands.append((start, current.strip()))
            current = ""
        if current.strip():
            commands.append((start, current.strip()))
    return commands


def runsRepositoryCode(command):
    return bool(re.search(r"\.py\b|PythonSlicer|build_dmg\.sh|(?:^|\s)\./Slicer\b|-m\s+\w", command))


def checkCode(text, masked, fences, source, failures):
    spans = codeSpans(text, masked)
    commands = fenceCommands(fences)
    seen = set()

    def report(kind, line, message):
        if (kind, line, message) not in seen:
            seen.add((kind, line, message))
            failures.append(Failure(kind, line, message))

    for line, content in spans:
        token = content.strip()
        # Settings keys
        for match in re.finditer(rf"(?<![\w/.\-])(?:{'|'.join(SETTING_FAMILIES)})/[^\s,;()]+", token):
            key = match.group(0).rstrip(".:")
            wildcard = re.search(r"[*<{]", key)
            if wildcard:
                key = key[: wildcard.start()]
                key = key[: key.rfind("/") + 1]
                if key.count("/") < 2 and key.split("/")[0] in ("Studio",) and key.count("/") < 1:
                    continue
            if not key or key.endswith(PATH_EXTENSIONS):
                continue
            if not source.hasSettingKey(key):
                report("setting-key", line, f"settings key {key} is not in the source")
        # Environment variables
        for match in re.finditer(r"\bSLICER_[A-Z0-9_]+\b", token):
            if match.group(0) not in source.text():
                report("env-var", line, f"environment variable {match.group(0)} is not in the source")
        # Object names and test names
        if re.fullmatch(r"(?:Studio|LNM)[A-Z][A-Za-z0-9]*", token) and not source.hasIdentifier(token):
            report("object-name", line, f"{token} is not in the source")
        # Paths
        if (re.fullmatch(r"[A-Za-z0-9_.\-/]+", token) and "/" in token and (token.endswith(PATH_EXTENSIONS) or token.endswith("/"))
                and not token.startswith(EXTERNAL_PATH_PREFIXES) and token.split("/")[0] not in SETTING_FAMILIES):
            if not source.hasPath(token):
                report("path", line, f"path {token} does not exist")
        # Shortcuts
        if SHORTCUT.match(token) and not source.hasShortcut(token):
            report("shortcut", line, f"shortcut {token} is not in the source")
        elif FUNCTION_KEY.match(token) and not source.hasShortcut(token):
            report("shortcut", line, f"shortcut {token} is not in the source")

    codeItems = [(line, content) for line, content in spans] + [(line, command) for line, command in commands if runsRepositoryCode(command)]
    for line, content in codeItems:
        for match in re.finditer(r"python3?\s+-m\s+([A-Za-z_][\w.]*)|PythonSlicer\s+-m\s+([A-Za-z_][\w.]*)|(?<![\w-])-m\s+([A-Za-z_][\w.]*)", content):
            module = next(group for group in match.groups() if group)
            if module not in EXTERNAL_MODULES and not source.hasModule(module):
                report("module", line, f"python -m {module}: no such module in the source")
        for match in re.finditer(r"(?<![\w-])(--[a-z][a-z0-9-]*)", content):
            if not source.hasFlag(match.group(1)):
                report("flag", line, f"command line flag {match.group(1)} is not in the source")


def checkBanned(text, failures):
    for pattern, description in BANNED:
        for match in pattern.finditer(text):
            failures.append(Failure("banned", text.count("\n", 0, match.start()) + 1, description))
    for match in re.finditer(r"\bTODO\b", text):
        failures.append(Failure("todo", text.count("\n", 0, match.start()) + 1, "TODO in the README"))


def parseDisclaimer(source):
    """The text of ``DISCLAIMER`` in LesionNetworkMappingLib/export.py (parsed, not imported), or None."""
    for relative in source.files():
        if relative.endswith("LesionNetworkMappingLib/export.py"):
            try:
                with open(os.path.join(source.root, relative), encoding="utf-8") as stream:
                    tree = ast.parse(stream.read())
            except (OSError, SyntaxError):
                return None
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "DISCLAIMER" for target in node.targets):
                    try:
                        return str(ast.literal_eval(node.value))
                    except ValueError:
                        return None
    return None


def normalizeSpace(text):
    return re.sub(r"\s+", " ", text).strip()


def checkDisclaimer(text, source, failures):
    expected = parseDisclaimer(source)
    if expected is None:
        failures.append(Failure("disclaimer", 0, "DISCLAIMER was not found in LesionNetworkMappingLib/export.py"))
        return
    lines = text.split("\n")
    quote = []
    heading = 0
    for number, line in enumerate(lines, 1):
        match = HEADING.match(line)
        if match and slugify(match.group(2)) == "disclaimer":
            heading = number
            continue
        if heading and not quote and line.startswith(">"):
            quote.append(line)
            continue
        if heading and quote:
            if line.startswith(">"):
                quote.append(line)
            else:
                break
    if not heading:
        failures.append(Failure("disclaimer", 0, "the README has no 'Disclaimer' heading"))
        return
    quoted = normalizeSpace(" ".join(line.lstrip(">").strip() for line in quote))
    if quoted != normalizeSpace(expected):
        failures.append(Failure("disclaimer", heading, "the quoted disclaimer differs from DISCLAIMER in export.py"))


def loadState(path):
    with open(path, encoding="utf-8") as stream:
        state = json.load(stream)
    return {str(key): str(value) for key, value in state.items()}


def wpState(identifier, state):
    if identifier in state:
        return state[identifier]
    if identifier in EARLIER_DONE:
        return "passed"
    return "not in the state file"


def claimUnits(masked):
    """``[(line, text)]``: table rows, and the sentences of paragraphs and list items (a sentence may span lines)."""
    units = []
    paragraph = []
    paragraphLine = 0

    def flush():
        if paragraph:
            text = " ".join(paragraph)
            for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z*`(\[\"])", text):
                units.append((paragraphLine, sentence))
            paragraph.clear()

    for number, line in enumerate(masked.split("\n"), 1):
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("|"):
            flush()
            units.append((number, stripped))
        elif re.match(r"^(?:[-*+]|\d+[.)])\s", stripped) or HEADING.match(line) or stripped.startswith(">"):
            flush()
            paragraphLine = number
            paragraph.append(re.sub(r"^(?:[-*+]|\d+[.)])\s+|^>\s*", "", stripped))
        else:
            if not paragraph:
                paragraphLine = number
            paragraph.append(stripped)
    flush()
    return units


def checkClaims(text, masked, state, failures):
    """Sentences and table rows marked Implemented or Available, and those about work packages that did not pass."""
    for number, unit in claimUnits(masked):
        lowered = unit.lower()
        identifiers = sorted(set(WP_ID.findall(unit)))
        features = []
        for pattern, workPackages in FEATURE_WPS:
            if re.search(pattern, lowered):
                features.extend(workPackages)
        marked = re.search(r"\b(?:Implemented|Available)\b", unit) is not None
        if marked:
            named = sorted(set(identifiers) | set(features))
            if not named:
                failures.append(Failure("claim-unmapped", number, "a sentence marked Implemented/Available names no work package or known feature"))
            for identifier in named:
                status = wpState(identifier, state)
                if status != "passed":
                    failures.append(Failure("claim-state", number, f"marked Implemented/Available, but {identifier} is '{status}'"))
        notPassed = [identifier for identifier in identifiers if wpState(identifier, state) != "passed"]
        if notPassed and not PLANNED_WORDS.search(unit):
            failures.append(Failure("planned-wording", number, f"mentions {', '.join(notPassed)}, which did not pass, without saying it is planned or pending"))


def runExamples(fences, source, pythonSlicer, failures):
    """Run the commands of ``console example`` fences with --help and compare their flags with the help text."""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([os.path.join(source.root, "Modules", "Scripted", "LesionNetworkMapping")] + ([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else []))
    for firstLine, info, lines in fences:
        if info.split()[:2] != ["console", "example"]:
            continue
        for line, command in fenceCommands([(firstLine, info, lines)]):
            try:
                arguments = shlex.split(command)
            except ValueError as error:
                failures.append(Failure("example", line, f"cannot parse the command: {error}"))
                continue
            # Skip assignments and shell built-ins
            while arguments and re.fullmatch(r"[A-Za-z_]\w*=\S*", arguments[0]):
                arguments = arguments[1:]
            if not arguments:
                continue
            program = arguments[0]
            flags = [argument.split("=")[0] for argument in arguments if argument.startswith("--")]
            if program == "PythonSlicer" and "-m" in arguments:
                module = arguments[arguments.index("-m") + 1]
                if not module.startswith("LesionNetworkMappingLib."):
                    continue
                run = [pythonSlicer, "-m", module, "--help"]
            elif program in ("python3", "python") and len(arguments) > 1 and arguments[1].endswith(".py"):
                script = os.path.join(source.root, arguments[1])
                if not os.path.exists(script):
                    failures.append(Failure("example", line, f"{arguments[1]} does not exist"))
                    continue
                run = [sys.executable, script, "--help"]
            else:
                continue
            try:
                result = subprocess.run(run, capture_output=True, text=True, timeout=120, env=environment, cwd=source.root, check=False)
            except (OSError, subprocess.TimeoutExpired) as error:
                failures.append(Failure("example", line, f"could not run {' '.join(run[:3])}: {error}"))
                continue
            helpText = result.stdout + result.stderr
            if result.returncode != 0:
                failures.append(Failure("example", line, f"{' '.join(run[-3:])} exited with {result.returncode}: {helpText.strip()[-200:]}"))
                continue
            for flag in flags:
                if flag != "--help" and flag not in helpText:
                    failures.append(Failure("example", line, f"{flag} is not in the help text of {' '.join(run[-3:-1])}"))


def checkReadme(readmePath, sourceRoot, statePath, examples=False, pythonSlicer="PythonSlicer"):
    """Run every check. Return the list of ``Failure``."""
    with open(readmePath, encoding="utf-8") as stream:
        text = stream.read()
    relativeReadme = os.path.relpath(os.path.abspath(readmePath), os.path.abspath(sourceRoot)).replace(os.sep, "/")
    source = Source(sourceRoot, exclude=relativeReadme)
    state = loadState(statePath) if statePath else {}
    masked, fences, fenceProblems = maskFences(text)
    anchors = anchorsOf(text)
    failures = [Failure("fence", line, message) for line, message in fenceProblems]
    checkLinks(text, masked, os.path.dirname(os.path.abspath(readmePath)), anchors, failures)
    checkExternalAnchors(source, anchors, failures)
    checkCode(text, masked, fences, source, failures)
    checkBanned(text, failures)
    checkDisclaimer(text, source, failures)
    if statePath:
        checkClaims(text, masked, state, failures)
    if examples:
        runExamples(fences, source, pythonSlicer, failures)
    failures.sort(key=lambda failure: (failure.line, failure.kind))
    return failures


#
# Self test
#

_SELF_TEST_SOURCE = {
    "Modules/Scripted/Studio/StudioLib/shell.py": (
        'KEY = "Studio/Autosave/Enabled"\nENV = "SLICER_STUDIO_AUTOSAVE"\nname = "StudioTopBar"\nopen = "Ctrl+K"\n'
        'F = "--real-flag"\nGROUP = "Studio/Group"\nitem = "Item"\nkeys = "Ctrl+Alt+" + str(n)\n'),
    "Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib/export.py": 'DISCLAIMER = (\n    "Research use only. "\n    "Specificity is debated."\n)\n',
    "Modules/Scripted/LesionNetworkMapping/LesionNetworkMappingLib/engine.py": "# engine\n",
    "Docs/other.md": "See [the README](../README.md#missing-anchor) and [fine](../README.md#a-heading).\n",
    "Docs/target.md": "# Target\n\n## Section Two\n",
}

_SELF_TEST_STATE = {"A-01": "passed", "A-02": "failed", "W2-I4": "pending"}

_SELF_TEST_GOOD = """# A heading

Contents: [heading](#a-heading) and [target](Docs/target.md#section-two).

- Key `Studio/Autosave/Enabled`, group `Studio/Group/Item`, variable `SLICER_STUDIO_AUTOSAVE`, widget `StudioTopBar`.
- Path `Docs/target.md`, shortcuts `Ctrl+K` and `Ctrl+Alt+2`, flag `--real-flag`, module `LesionNetworkMappingLib.engine`.
- Autosave is Implemented (A-01). Smart fill is planned (A-02).

```console example
python3 Utilities/Scripts/Tool.py --out x
```

### Disclaimer

> Research use only. Specificity
> is debated.
"""

_SELF_TEST_ERRORS = {
    "link-file": "[missing](Docs/missing.md)",
    "link-anchor": "[bad](#no-such-heading)",
    "anchor-external": None,  # comes from Docs/other.md
    "setting-key": "`Studio/Autosave/Nope`",
    "env-var": "`SLICER_STUDIO_NOPE`",
    "object-name": "`StudioMissingWidget`",
    "path": "`Docs/nope.md`",
    "module": "`python -m LesionNetworkMappingLib.nomodule`",
    "flag": "`--fake-flag`",
    "shortcut": "`Ctrl+Alt+Z`",
    "todo": "TO" + "DO later",
    "banned": "this localizes the lesion",
    "claim-state": "Smart fill is Implemented.",
    "claim-unmapped": "An Available thing.",
    "planned-wording": "The W2-I4 work is done.",
    "disclaimer": None,  # comes from a different quote
    "fence": None,  # comes from an unbalanced fence
}


def selfTest():
    """Run the checker on a small embedded README: it must pass, and one error of each kind must be reported."""
    problems = []
    with tempfile.TemporaryDirectory() as directory:
        for relative, content in _SELF_TEST_SOURCE.items():
            path = os.path.join(directory, relative)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as stream:
                stream.write(content)
        statePath = os.path.join(directory, "state.json")
        with open(statePath, "w", encoding="utf-8") as stream:
            json.dump(_SELF_TEST_STATE, stream)
        readmePath = os.path.join(directory, "README.md")
        # A tool for the example block
        os.makedirs(os.path.join(directory, "Utilities", "Scripts"), exist_ok=True)
        with open(os.path.join(directory, "Utilities", "Scripts", "Tool.py"), "w", encoding="utf-8") as stream:
            stream.write('import argparse\nparser = argparse.ArgumentParser()\nparser.add_argument("--out")\nparser.parse_args()\n')

        # The good README passes (the link to the other file of the tree needs its anchor: Docs/other.md links a-heading)
        with open(readmePath, "w", encoding="utf-8") as stream:
            stream.write(_SELF_TEST_GOOD)
        failures = [failure for failure in checkReadme(readmePath, directory, statePath, examples=True) if failure.kind != "anchor-external"]
        # anchor-external is expected once for #missing-anchor
        external = [failure for failure in checkReadme(readmePath, directory, statePath) if failure.kind == "anchor-external"]
        if failures:
            problems.append("the good README was rejected: " + "; ".join(str(failure) for failure in failures))
        if len(external) != 1 or "missing-anchor" not in external[0].message:
            problems.append(f"expected one anchor-external failure for #missing-anchor, got {[str(failure) for failure in external]}")

        # One error of each kind
        errors = "\n".join(f"- {text}" for text in _SELF_TEST_ERRORS.values() if text)
        bad = _SELF_TEST_GOOD.replace("### Disclaimer\n\n> Research use only. Specificity\n> is debated.\n", "### Disclaimer\n\n> Research use only. Something else.\n")
        bad = bad.replace("```console example", f"{errors}\n\n```console example", 1)
        bad += "\n```text\nnever closed\n"
        with open(readmePath, "w", encoding="utf-8") as stream:
            stream.write(bad)
        kinds = {failure.kind for failure in checkReadme(readmePath, directory, statePath)}
        for kind in _SELF_TEST_ERRORS:
            if kind not in kinds:
                problems.append(f"the error of kind '{kind}' was not reported")
        # The example of a missing flag is reported with --run-examples
        with open(readmePath, "w", encoding="utf-8") as stream:
            stream.write(_SELF_TEST_GOOD.replace("--out x", "--bogus x"))
        kinds = {failure.kind for failure in checkReadme(readmePath, directory, statePath, examples=True)}
        if "example" not in kinds:
            problems.append("a flag that the help text does not list was not reported by --run-examples")
    # Anchor rules
    expectedSlugs = {"A heading: with `code` & more!": "a-heading-with-code--more", "Under_score-and-dash": "under_score-and-dash"}
    for heading, slug in expectedSlugs.items():
        if slugify(heading) != slug:
            problems.append(f"slugify({heading!r}) is {slugify(heading)!r}, expected {slug!r}")
    if anchorsOf("# Same\n\n# Same\n\n# Same\n") != {"same", "same-1", "same-2"}:
        problems.append("repeated headings are not numbered -1, -2")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check README.md against the source tree and the work package state.")
    parser.add_argument("--readme", help="path of README.md")
    parser.add_argument("--source", help="root of the source tree")
    parser.add_argument("--state", help="wave_state.json: work package id -> passed|failed|deferred|pending")
    parser.add_argument("--run-examples", action="store_true", help="run the 'console example' fences with --help")
    parser.add_argument("--pythonslicer", default=None, help="PythonSlicer for the LNM examples (default: PythonSlicer in PATH, or the build tree default)")
    parser.add_argument("--json", action="store_true", help="print the failures as JSON")
    parser.add_argument("--self-test", action="store_true", help="check the checker on an embedded README")
    arguments = parser.parse_args(argv)

    if arguments.self_test:
        problems = selfTest()
        for problem in problems:
            print("SELF-TEST FAILED:", problem)
        if not problems:
            print("self-test passed")
        return 1 if problems else 0

    if not arguments.readme or not arguments.source:
        parser.error("--readme and --source are required (or use --self-test)")
    pythonSlicer = arguments.pythonslicer
    if pythonSlicer is None:
        default = "/opt/S-r/python-install/bin/PythonSlicer"
        pythonSlicer = default if os.path.exists(default) else "PythonSlicer"
    failures = checkReadme(arguments.readme, arguments.source, arguments.state, arguments.run_examples, pythonSlicer)
    if arguments.json:
        print(json.dumps([failure.asDict() for failure in failures], indent=1))
    else:
        for failure in failures:
            print(failure)
        print(f"{len(failures)} failure(s)" if failures else "README check passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
