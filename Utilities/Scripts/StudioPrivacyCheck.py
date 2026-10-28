#!/usr/bin/env python3
r"""Privacy check for files meant to be shared: absolute local paths in scenes, .mrb, sidecars, tables, study files, reports and logs.

usage: StudioPrivacyCheck.py [--strict] <root> [<root> ...]    exit status 0 when no shared file holds an absolute local path,
       1 when one does (or a file cannot be read: "PRIVACY ERROR <file>"), 2 for a missing root or a root without any file

Each file with hits is printed with its class and the first hits. Classes (CLAUDE.md: shared files hold paths relative to
the file; provenance and reproduce files are the exception, by design):

  scene          .mrml, and every text member of a .mrb (.mrml, .json, .tsv, .csv, .txt, ...: the .mrb is shared as a whole,
                 so a table saved as .csv or a text node saved as .txt inside it counts like the scene)  shared: a hit fails
  table          .tsv / .csv                                     shared: a hit fails
  sidecar/study  other .json next to data, study files           shared: a hit fails
  log            .log in a study folder (for example derivatives/normalization/<sid>/logs; the child output of a run,
                 written with paths relative to the run folder)  shared: a hit fails
  other          anything else that is text                      shared: a hit fails
  report         .html / .md / .txt                              reported, not a failure
  run-record     argv / inputs of a run: runs.jsonl, normalization.json, provenance.json, reproduce.sh and reproduce_<name>.sh
                 (for example the pipeline's reproduce_pipeline.sh), job and design records
                                                                 provenance by design: reported, not a failure
  app-state      .studio, cache, work folders                    never shared: reported, not a failure

--strict also counts cohortmaps.json and glm.json as shared outputs (their paths are relative since W3-I1).

Absolute local paths are the folders of Linux and macOS homes, mounts and volumes (/home, /root, /Users, /tmp, /opt, /Volumes,
/mnt, /media, /data, /private, the macOS temporary folders /var/folders, and the usual cluster folders /scratch, /work, /srv,
/gpfs, /lustre), Windows drive paths (C:/..., also with backslashes)
and Windows UNC paths (\\server\share). The number of files checked (the .mrb and text files above) is printed per root.
"""

import os
import re
import sys
import zipfile

# Not preceded by a word character or ".": "../../opt/x" is a relative path, not an absolute one
# Windows: a drive letter followed by a back or forward slash ("C:\\data", "D:/study"), not preceded by a word character
# ("http://" is not a drive). Windows UNC paths: two backslashes, a server, a backslash and a share ("\\\\server\\share"; in
# JSON each backslash is doubled)
PAT = re.compile(
    r"(?<![\w.])((?:/tmp|/home|/root|/Users|/opt|/Volumes|/mnt|/media|/data|/private|/var/folders|/scratch|/work|/srv|/gpfs|/lustre)/[^\"'\t\n<>]*"
    r"|[A-Za-z]:[\\/][^\"'\t\n<>]*"
    r"|(?<![\\\w])\\{2,}[\w.$-]+\\+[\w.$-][^\"'\t\n<>]*)",
)
RUN_RECORDS = {"runs.jsonl", "normalization.json", "provenance.json", "slnm.json", "design.json", "entry.json", "job.json", "cohort.json"}
#: Reproduce scripts are run records: reproduce.sh and reproduce_<name>.sh (the pipeline's reproduce_pipeline.sh), nothing else
REPRODUCE_SCRIPT = re.compile(r"^reproduce(_\w+)?\.sh$")
RUN_RECORDS_LOOSE = {"cohortmaps.json", "glm.json"}
TEXT_SUFFIXES = (".mrml", ".json", ".jsonl", ".tsv", ".csv", ".html", ".md", ".txt", ".sh", ".log")
SHARED = ("scene", "table", "sidecar/study", "log", "other")


def classify(rel, name, strict=False):
    parts = rel.split(os.sep)
    if any(p in (".studio", "cache", "work") or p.startswith(".work") for p in parts):
        return "app-state"
    if name.endswith(".mrml"):
        return "scene"
    if name.endswith((".tsv", ".csv")):
        return "table"
    if name in RUN_RECORDS or (name in RUN_RECORDS_LOOSE and not strict) or REPRODUCE_SCRIPT.match(name):
        return "run-record"
    if name.endswith(".log"):
        return "log"
    if name.endswith((".html", ".md", ".txt")):
        return "report"
    if name.endswith((".json", ".jsonl")):
        return "sidecar/study"
    return "other"


def _texts(path, rel, name, strict):
    """``[(label, class, text)]`` of a file: every text member inside a .mrb (scene, sidecar, table .tsv/.csv, text node .txt, ...:
    the .mrb is shared as a whole), the file itself if it is text.
    """
    if name.endswith(".mrb"):
        with zipfile.ZipFile(path) as z:
            return [(f"{rel}!{n}", "scene", z.read(n).decode("utf-8", "replace")) for n in z.namelist() if n.endswith(TEXT_SUFFIXES)]
    if name.endswith(TEXT_SUFFIXES):
        with open(path, encoding="utf-8", errors="replace") as f:
            return [(rel, classify(rel, name, strict), f.read())]
    return []


def scan(roots, strict=False, out=print, counts=None):
    """Return the number of shared files with absolute local paths, plus the files that could not be read.

    ``counts`` (a dict), if given, receives the number of files checked per root (.mrb and text files).
    """
    failures = 0
    for root in roots:
        scanned = 0
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                if not name.endswith((".mrb", *TEXT_SUFFIXES)):
                    continue
                scanned += 1
                try:
                    texts = _texts(path, rel, name, strict)
                except Exception as error:
                    # A corrupt or unreadable file (a bad zip directory, damaged deflate data: zlib.error, an unsupported
                    # compression method: NotImplementedError, ...) cannot be shown to be free of local paths
                    failures += 1
                    out(f"PRIVACY ERROR {os.path.basename(os.path.abspath(root))}/{rel}: {type(error).__name__}: {error}")
                    continue
                for label, cls, text in texts:
                    hits = sorted({m.group(1)[:110] for m in PAT.finditer(text)})
                    if not hits:
                        continue
                    bad = cls in SHARED
                    failures += bad
                    out(f"{'FAIL' if bad else 'ok  '} [{cls}] {os.path.basename(os.path.abspath(root))}/{label}: {len(hits)} distinct, e.g. {hits[:2]}")
        out(f"PRIVACY checked {scanned} files in {root}")
        if counts is not None:
            counts[root] = scanned
    out(f"PRIVACY {'FAIL' if failures else 'PASS'} ({failures} shared files with absolute local paths or unreadable)")
    return failures


def main(argv):
    strict = "--strict" in argv
    roots = [a for a in argv if a != "--strict"]
    if not roots:
        print(__doc__)
        return 2
    # A missing root would scan nothing and pass: refuse it
    missing = [root for root in roots if not os.path.isdir(root)]
    if missing:
        print(f"PRIVACY ERROR: not a folder: {', '.join(missing)}")
        return 2
    counts = {}
    failures = scan(roots, strict, counts=counts)
    # A root without any file to check proves nothing (a wrong folder): refuse it rather than pass
    empty = [root for root in roots if not counts.get(root)]
    if empty:
        print(f"PRIVACY ERROR: no file to check in: {', '.join(empty)}")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
