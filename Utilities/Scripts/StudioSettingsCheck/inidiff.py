"""Parse QSettings ini files and report fork-only keys and rewritten official keys.

usage: inidiff.py base.ini control.ini fork.ini   (control = base after an official session, for churn)
       inidiff.py --keys some.ini                 (list keys outside Studio/* and Appearance/*)
"""

import json
import sys

FORK_PREFIXES = ("Studio/", "Appearance/")


def parse(path):
    out = {}
    section = "General"
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return {}
    for line in lines:
        if not line.strip() or line.startswith(";") or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().replace("%20", " ")
            key = k if section == "General" else f"{section}/{k}"
            out[key.replace("\\", "/")] = v.strip()
    return out


def usageError(message):
    """One-line error plus the usage text on stderr, exit status 2 (like argparse)."""
    sys.stderr.write(f"inidiff.py: error: {message}\n{__doc__}")
    sys.exit(2)


def isFork(key):
    return key.startswith(FORK_PREFIXES)


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)
        sys.exit(0)
    if sys.argv[1:2] == ["--keys"]:
        if len(sys.argv) != 3:
            usageError("--keys needs exactly one ini file")
        d = parse(sys.argv[2])
        print(json.dumps({"official_keys": sorted(k for k in d if not isFork(k)), "fork_keys": sorted(k for k in d if isFork(k))}, indent=1))
        sys.exit(0)
    if len(sys.argv) != 4:
        usageError(f"expected three ini files (base, control, fork), got {len(sys.argv) - 1}")
    base, control, fork = (parse(p) for p in sys.argv[1:4])
    churn = {k for k in set(base) | set(control) if base.get(k) != control.get(k)}
    report = {"churn_in_official_control": sorted(churn), "rewritten_official_keys": [], "new_official_keys_not_written_by_official": [], "removed_official_keys": [],
              "fork_keys_added": sorted(k for k in fork if isFork(k) and k not in base)}
    for k in sorted(set(base) | set(fork)):
        if isFork(k):
            continue
        if k in base and k in fork and base[k] != fork[k]:
            report["rewritten_official_keys"].append({"key": k, "base": base[k][:120], "fork": fork[k][:120], "also_changes_in_official_control": k in churn})
        elif k in fork and k not in base:
            report["new_official_keys_not_written_by_official"].append({"key": k, "value": fork[k][:120], "official_control_writes_it": k in control})
        elif k in base and k not in fork:
            report["removed_official_keys"].append(k)
    print(json.dumps(report, indent=1))
