#!/usr/bin/env python3
"""Privacy check for files meant to be shared: absolute local paths in scenes, .mrb, sidecars, tables, study files, reports and logs.

usage: StudioPrivacyCheck.py [--strict] <root> [<root> ...]    exit status 0 when no shared file holds an absolute local path

Each file with hits is printed with its class and the first hits. Classes (CLAUDE.md: shared files hold paths relative to
the file; provenance and reproduce files are the exception, by design):

  scene          .mrml (also inside .mrb)                        shared: a hit fails
  table          .tsv / .csv                                     shared: a hit fails
  sidecar/study  other .json next to data, study files           shared: a hit fails
  log            .log in a study folder (for example derivatives/normalization/<sid>/logs; the child output of a run,
                 written with paths relative to the run folder)  shared: a hit fails
  other          anything else that is text                      shared: a hit fails
  report         .html / .md / .txt                              reported, not a failure
  run-record     argv / inputs of a run: runs.jsonl, normalization.json, provenance.json, reproduce*.sh (for example the
                 pipeline's reproduce_pipeline.sh), job and design records
                                                                 provenance by design: reported, not a failure
  app-state      .studio, cache, work folders                    never shared: reported, not a failure

--strict also counts cohortmaps.json and glm.json as shared outputs (their paths are relative since W3-I1).
"""

import os
import re
import sys
import zipfile

# Not preceded by a word character or ".": "../../opt/x" is a relative path, not an absolute one
PAT = re.compile(r"(?<![\w.])(/tmp/[^\"'\t\n<>]*|/home/[^\"'\t\n<>]*|/root/[^\"'\t\n<>]*|/Users/[^\"'\t\n<>]*|/opt/[^\"'\t\n<>]*|[A-Za-z]:\\Users\\[^\"'\t\n<>]*)")
RUN_RECORDS = {"runs.jsonl", "normalization.json", "provenance.json", "reproduce.sh", "slnm.json", "design.json", "entry.json", "job.json", "cohort.json"}
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
    if name in RUN_RECORDS or (name in RUN_RECORDS_LOOSE and not strict) or (name.startswith("reproduce") and name.endswith(".sh")):
        return "run-record"
    if name.endswith(".log"):
        return "log"
    if name.endswith((".html", ".md", ".txt")):
        return "report"
    if name.endswith((".json", ".jsonl")):
        return "sidecar/study"
    return "other"


def scan(roots, strict=False, out=print):
    """Return the number of shared files with absolute local paths."""
    failures = 0
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                texts = []
                if name.endswith(".mrb"):
                    with zipfile.ZipFile(path) as z:
                        texts = [(f"{rel}!{n}", "scene", z.read(n).decode("utf-8", "replace")) for n in z.namelist() if n.endswith((".mrml", ".json", ".tsv"))]
                elif name.endswith(TEXT_SUFFIXES):
                    with open(path, encoding="utf-8", errors="replace") as f:
                        texts = [(rel, classify(rel, name, strict), f.read())]
                for label, cls, text in texts:
                    hits = sorted({m.group(1)[:110] for m in PAT.finditer(text)})
                    if not hits:
                        continue
                    bad = cls in SHARED
                    failures += bad
                    out(f"{'FAIL' if bad else 'ok  '} [{cls}] {os.path.basename(os.path.abspath(root))}/{label}: {len(hits)} distinct, e.g. {hits[:2]}")
    out(f"PRIVACY {'FAIL' if failures else 'PASS'} ({failures} shared files with absolute local paths)")
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
    return 1 if scan(roots, strict) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
