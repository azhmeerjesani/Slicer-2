"""Settings hygiene analysis (foreign_keys.txt, s1_diff.json, s2_diff.txt).

usage: analyse.py <output dir of run_settings.sh>   (reads out/ini/<stage>/Slicer.ini there, writes foreign_keys.txt, s1_diff.json, s2_diff.txt to out/)
"""

import json
import os
import subprocess
import sys

if sys.argv[1:2] in (["-h"], ["--help"]):
    print(__doc__)
    sys.exit(0)

D = sys.argv[1]
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import inidiff  # noqa: E402

INI = os.path.join(D, "out", "ini")


def ini(stage):
    return inidiff.parse(os.path.join(INI, stage, "Slicer.ini"))


# Official sessions of the same scenario (S1: official user + control; S2: fresh official 5.13 then 5.12.4)
officialStages = {"s1": ["s1-1-official-user", "s1-control-official-plain"], "s2": ["s2-control-official513-first", "s2-control-official5124-second"]}
officialKeys = {sc: set().union(*(set(ini(s)) for s in stages)) for sc, stages in officialStages.items()}

lines = []
for s in ["s1-2-fork-studio", "s1-3-fork-classic", "s1-4-fork-studio-again", "s1-4b-fork-study", "s1-5-after-tests", "s2-1-fork-first"]:
    foreign = sorted(k for k in ini(s) if not inidiff.isFork(k) and not k.startswith("LogFiles/") and k not in officialKeys[s[:2]])
    lines.append(f"{s} non-Studio/Appearance keys never written by the official sessions (LogFiles/* rotation excluded): {foreign}")

# --testing writes to Slicer-tmp.ini (temporary settings); the user Slicer.ini must not change (md5 before/after tests)
def md5s(name):
    return {os.path.basename(f): h for h, f in (ln.split() for ln in open(os.path.join(D, "out", name)) if ln.strip())}


before, after = md5s("s1-before-tests.md5"), md5s("s1-after-tests.md5")
lines.append(f"Slicer.ini md5 unchanged by the --testing suites: {before.get('Slicer.ini') == after.get('Slicer.ini')} (files after tests: {sorted(after)})")
open(os.path.join(D, "out", "foreign_keys.txt"), "w").write("\n".join(lines) + "\n")
print("\n".join(lines))

# S1: official base -> fork sessions + tests: rewritten official keys (churn = what an official session changes by itself)
s1 = subprocess.run([sys.executable, os.path.join(HERE, "inidiff.py"), os.path.join(INI, "s1-1-official-user", "Slicer.ini"),
                     os.path.join(INI, "s1-control-official-plain", "Slicer.ini"), os.path.join(INI, "s1-5-after-tests", "Slicer.ini")],
                    capture_output=True, text=True, check=False).stdout
open(os.path.join(D, "out", "s1_diff.json"), "w").write(s1)
r = json.loads(s1)
real = [x for x in r["rewritten_official_keys"] if not x["also_changes_in_official_control"]]
newNotOfficial = [x for x in r["new_official_keys_not_written_by_official"] if not x["official_control_writes_it"] and not x["key"].startswith("LogFiles/")]
print(f"S1 rewritten official keys (not churn): {real}")
print(f"S1 new official keys the official control does not write (LogFiles/* rotation excluded): {newNotOfficial}")
print(f"S1 removed official keys: {r['removed_official_keys']}")

# S2: fork first vs official first (official namespace), then official sessions on the fork-written ini
out = []
ff, of = ini("s2-1-fork-first"), ini("s2-control-official513-first")
out.append("S2 fork-first official-namespace keys vs a fresh official 5.13 session:")
for k in sorted(set(ff) | set(of)):
    if inidiff.isFork(k) or k.startswith(("LogFiles/", "ApplicationUpdate/")):
        continue
    a, b = ff.get(k), of.get(k)
    if a != b:
        out.append(f"  {k}: fork-first={a[:100] if a else None!r} official-first={b[:100] if b else None!r}")
out.append("fork keys: " + json.dumps({k: v[:60] for k, v in sorted(ff.items()) if inidiff.isFork(k)}))
out.append("Official 5.13 then 5.12.4 on the fork-written ini: official keys changed by them:")
for s, label in (("s2-2-official513-after-fork", "5.13"), ("s2-3-official5124-after-fork", "5.12.4")):
    cur = ini(s)
    changed = sorted(k for k in set(ff) | set(cur) if not inidiff.isFork(k) and not k.startswith(("LogFiles/", "ApplicationUpdate/")) and ff.get(k) != cur.get(k))
    kept = all(cur.get(k) == v for k, v in ff.items() if inidiff.isFork(k))
    out.append(f"  {label} {changed}")
    out.append(f"   fork keys kept: {kept}")
open(os.path.join(D, "out", "s2_diff.txt"), "w").write("\n".join(out) + "\n")
print("\n".join(out))
