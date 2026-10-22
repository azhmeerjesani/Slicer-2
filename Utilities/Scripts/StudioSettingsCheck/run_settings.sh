#!/bin/bash
# Settings written by each side (official 5.12.4 / 5.13 and the fork) and settings hygiene.
# S1 official first: official 5.13 user session (explicit choices) -> base. Control: official session again -> control.
#    Fork: base -> fork Studio session (switches workspaces, then Classic) -> fork Classic session (back to Studio)
#    -> fork Studio session -> fork tests (--testing, same HOME) -> fork.ini. Then official 5.13 and 5.12.4 sessions on fork.ini.
# S2 fork first: fresh HOME -> fork Studio session -> fork-first.ini; official 5.13 + 5.12.4 sessions on it;
#    control: fresh HOME official 5.13 session -> official-first.ini.
# usage: run_settings.sh <output dir>     (needs FORK_SLICER, OFFICIAL_SLICER_513 and OFFICIAL_SLICER_5124: the Slicer executables)
# Writes <output dir>/out/... and, at the end, runs analyse.py on <output dir> (foreign_keys.txt, s1_diff.json, s2_diff.txt).
# Official reference binaries must be unpacked official releases; the session scripts live next to this file.
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:?usage: run_settings.sh <output dir>}
mkdir -p "$OUT" || exit 1
D=$(cd "$OUT" && pwd) || exit 1
[ -n "$D" ] || exit 1
F=${FORK_SLICER:?set FORK_SLICER to the Slicer executable of the fork build}
O4=${OFFICIAL_SLICER_5124:?set OFFICIAL_SLICER_5124 to an official Slicer 5.12.4 executable}
O3=${OFFICIAL_SLICER_513:?set OFFICIAL_SLICER_513 to an official Slicer 5.13 executable}
REPO=$(cd "$HERE/../../.." && pwd)
TMPBASE=${TMPDIR:-/tmp}
INI=.config/slicer.org
sess() { # home bin session tag [args]
  local H=$1 bin=$2 s=$3 tag=$4 args=$5
  SESSION=$s SESSION_TAG=$tag G2_SET_OUT=$D/out HOME=$H LANG=C.UTF-8 LC_ALL=C.UTF-8 timeout 600 xvfb-run -a -s "-screen 0 1920x1080x24" $bin --no-splash $args --python-script $HERE/session.py > $D/out/$tag.log 2>&1
  echo "$tag exit=$?"
  mkdir -p $D/out/ini/$tag; cp $H/$INI/*.ini $D/out/ini/$tag/ 2>/dev/null
}
rm -rf "${D:?}/out"; mkdir -p "$D/out"
# ---- S1
H=$(mktemp -d $TMPBASE/slicer-settings-set1-XXXX)
sess $H $O3 official-user s1-1-official-user
HC=$(mktemp -d $TMPBASE/slicer-settings-setc-XXXX); cp -r $H/.config $HC/
sess $HC $O3 plain s1-control-official-plain
rm -rf $HC
sess $H $F studio s1-2-fork-studio
sess $H $F classic s1-3-fork-classic
sess $H $F plain s1-4-fork-studio-again
sess $H $F study s1-4b-fork-study
# Tests under --testing with the same HOME (they use temporary settings; Slicer.ini must not change)
md5sum $H/$INI/*.ini > $D/out/s1-before-tests.md5
for t in StudioTest StudioModesTest StudioAutosaveTest StudioAccessibilityTest StudioLibraryTest StudioNormalizeTest; do
  (cd "$REPO" && HOME=$H timeout 1800 xvfb-run -a -s "-screen 0 1920x1080x24" $F --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['$REPO/Modules/Scripted/Studio/Testing/Python'], '$t'); slicer.app.exit(0)" > $D/out/s1-test-$t.log 2>&1; echo "test $t exit=$?")
done
md5sum $H/$INI/*.ini > $D/out/s1-after-tests.md5
ls -la $H/$INI/ > $D/out/s1-after-tests.ls
mkdir -p $D/out/ini/s1-5-after-tests; cp $H/$INI/*.ini $D/out/ini/s1-5-after-tests/
sess $H $O3 plain s1-6-official513-after-fork
sess $H $O4 plain s1-7-official5124-after-fork
rm -rf $H
# ---- S2
H=$(mktemp -d $TMPBASE/slicer-settings-set2-XXXX)
sess $H $F plain s2-1-fork-first
sess $H $O3 plain s2-2-official513-after-fork
sess $H $O4 plain s2-3-official5124-after-fork
rm -rf $H
H=$(mktemp -d $TMPBASE/slicer-settings-set3-XXXX)
sess $H $O3 plain s2-control-official513-first
sess $H $O4 plain s2-control-official5124-second
rm -rf $H
python3 "$HERE/analyse.py" "$D" > "$D/out/analysis.txt" && echo "analysis: $D/out/analysis.txt"
echo DONE
