# slicer-lnm: lesion network mapping without the user interface

`slicer-lnm` runs the lesion network mapping library of Slicer Studio (`LesionNetworkMappingLib`, pure
numpy and scipy) on a server or an HPC cluster. It is the same library the application runs, so a run
started from Map or Library ▸ Analysis and a `slicer-lnm` run of the same lesions, connectome and settings
write the same files, with maps equal within numerical tolerance (they are not byte-for-byte identical
across numpy builds and CPUs). Research use only: not for clinical decisions.

Three ways to get it, all built locally (nothing here is published to PyPI or pushed to a registry):

| Way | Command | Notes |
|---|---|---|
| Slicer Studio on macOS | `"/Applications/Slicer Studio.app/Contents/bin/slicer-lnm"` | uses the bundled PythonSlicer; link it into your PATH: `ln -s "/Applications/Slicer Studio.app/Contents/bin/slicer-lnm" /usr/local/bin/` |
| Python wheel | `pip install "slicer_lnm-<version>-py3-none-any.whl[pillow]"` | Python 3.11 or newer; numpy and scipy are installed as dependencies, Pillow with the extra |
| Container | `docker run … slicer-lnm:local …` or `apptainer run slicer-lnm.sif …` | Dockerfile and lnm.def in this folder |

## Build

The wheel (from a checkout; the version is `1.0+fork.<git revision>`):

```sh
cd Modules/Scripted/LesionNetworkMapping
SLICER_FORK_REV=$(git rev-parse --short=12 HEAD) PythonSlicer -m pip wheel . --no-deps -w dist
rm -rf build slicer_lnm.egg-info   # setuptools leaves these in the module folder
```

Without a network connection add `--no-build-isolation`: PythonSlicer bundles setuptools. Any Python 3.11+
with setuptools 77 or newer builds the same wheel. Extras: `slicer-lnm[pillow]` (figures and report
images), `slicer-lnm[simpleitk]` (registration helpers), `slicer-lnm[all]`.

The container images (the build context is the module folder, so only the library is copied):

```sh
cd Modules/Scripted/LesionNetworkMapping
docker build -f ../../../Utilities/SlicerStudioLNM/Dockerfile \
  --build-arg SLICER_FORK_REV=$(git rev-parse --short=12 HEAD) -t slicer-lnm:local .
# amd64 and arm64 into a local OCI archive (no registry):
docker buildx build --platform linux/amd64,linux/arm64 -f ../../../Utilities/SlicerStudioLNM/Dockerfile \
  --output type=oci,dest=slicer-lnm.oci.tar .
# Apptainer / Singularity:
apptainer build --build-arg SLICER_FORK_REV=$(git rev-parse --short=12 HEAD) \
  slicer-lnm.sif ../../../Utilities/SlicerStudioLNM/lnm.def
```

The images use `python:3.12-slim` with numpy 2.4.6, scipy 1.17.1 and Pillow 12.2.0, the versions Slicer
Studio bundles. `--build-arg WITH_PILLOW=0` leaves Pillow out: the report stage (report.html) and
`--figures` then fail with "No module named 'PIL'", the data outputs are complete and the exit code is 0
(`pipeline.json` lists the failed stage). The same holds for a wheel installed without the `pillow` extra.

The macOS disk image gets `Contents/bin/slicer-lnm` from `Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh`:
a shell wrapper that runs the bundled `PythonSlicer -m LesionNetworkMappingLib.app` with the library folder
on `PYTHONPATH`. It is written before the application is signed, works through symbolic links and in
folders with spaces, and the start-up test runs `slicer-lnm --version` from the mounted image. No pip
package is ever installed into the application.

## Usage

```
slicer-lnm <input_dir> <output_dir> {participant,group} --connectome PATH --accept-terms [options]
slicer-lnm <command> [arguments]       library jobs: engine, cohortmaps, pipeline, glm, slnm, nulls, qa, figures, ...
slicer-lnm --version
```

**Input**: a BIDS dataset whose lesion masks are named `*_label-lesion_mask`, `*_desc-lesion_mask` or
`*_label-lesion_roi` (`.nii` or `.nii.gz`, in the raw data or in `derivatives/`), or a Slicer Studio study
folder. For a BIDS dataset, `slicer-lnm` writes a study to `<output_dir>/sourcedata/slicer-lnm-study`
that references the masks in place (the input can be read-only) and imports `participants.tsv`:

- `--group-map 'patient=case,HC=control'` (and `--group-column C` when the column is not `group`) maps the
  groups to case and control for the specificity GLM;
- `--score-columns aphasia_score,...` confirms the score columns used by sLNM;
- the `space-` entity of a mask (for example `space-MNI152NLin2009cAsym`) is its template space; a
  native-space mask needs a scan and the Slicer application (`--slicer`) to be normalized, otherwise it is
  excluded and reported.

When a subject has several masks, the template-space one is used and the others are reported. The study
folder opens in the Library of Slicer Studio.

**Levels** (both run the cohort pipeline of the application, `LesionNetworkMappingLib.pipeline`):

- `participant`: preflight, normalization, the network maps of every lesion (batched, with the per-lesion
  relocation nulls), the per-lesion quality checks and the per-lesion export;
- `group`: everything else, reusing what the participant level computed: overlap and sensitivity, null
  models, lesion-set diagnostics, the GLM, sLNM, conjunction, clusters, methods, checklist and report.

The results are in `<output_dir>/conn-<connectome>[_n-<N>]/`, with the names, sidecars, `provenance.json`,
`pipeline.json`, `methods.md`, `reproduce.sh` and `reproduce_pipeline.sh` of the application's
`<study>/derivatives/lnm/conn-<connectome>[_n-<N>]/`, and `<output_dir>/dataset_description.json`
(DatasetType "derivative"). The group level can run alone; it computes the maps when the participant level
did not.

Common options: `--participant-label 01 02` (either level), `--rule '{"kind": "fwe_bonferroni", "alpha": 0.05}'`
(or a JSON file), `--recipe NAME` (an analysis recipe; options given on the command line win; pipeline.json and the methods
name it, and the recipe's steps outside the pipeline, such as validation, are listed as info events),
`--design design.json` (a design of Map ▸ Contrast: two-group case/control or continuous score),
`--null-relocations K`, `--perm N`, `--preview N`, `--figures glass,montage,gallery` (extra figures of the
mean T map), `--events json` (JSON lines for scripts), `--no-report`. `--subject-list` is not available in
this build: the pipeline maps every connectome subject, or `--preview N` random ones. Exit codes: 0 done,
1 error, 2 usage, 3 cancelled, 4 data use terms not accepted, 5 blocked by preflight, 6 no eligible lesion,
75 output folder locked.

### Threads and HPC

`--threads K` sets the worker and BLAS threads (default: CPUs − 1, capped by `$SLICER_LNM_MAX_THREADS`).
Ask the scheduler for K cores and pass the same K. The maps are computed in one streaming pass over the
connectome per batch of up to 100 lesions (`--memory-budget-gb` sets the batch size), so one
participant-level job for the whole cohort is faster than one job per subject: run one participant job,
then one group job. Jobs that write to the same output folder wait for each other (`--lock-wait`, seconds).

```sh
#!/bin/sh
#SBATCH --cpus-per-task=8 --mem=32G --time=12:00:00
apptainer run --containall -B "$PWD/bids:/bids:ro" -B "$PWD/out:/out" -B /data/GSP1000.lnmc:/connectome:ro \
  slicer-lnm.sif /bids /out participant --connectome /connectome --accept-terms --threads 8
apptainer run --containall -B "$PWD/bids:/bids:ro" -B "$PWD/out:/out" -B /data/GSP1000.lnmc:/connectome:ro \
  slicer-lnm.sif /bids /out group --connectome /connectome --accept-terms --threads 8 \
  --group-map 'patient=case,HC=control' --score-columns aphasia_score
```

### Bind mounts

- the BIDS dataset (or study folder) — read-only is fine for a BIDS dataset; a study folder needs write
  access (the pipeline posts its results to the study's inbox and keeps its map cache there, as the
  application does);
- the output folder — writable;
- the connectome bundle (`.lnmc` folder) — read-only (`:ro`). Reference maps are then not built; build
  them once in Connectome ▸ Manage… of the application, or on a writable copy with `--build-references`.

Paths inside the outputs are relative, so the output folder can be moved together with the input it
refers to; `provenance.json` and the reproduce scripts keep the paths of the run on purpose.

## Reproducing a run

`reproduce.sh` (next to the maps) recomputes the maps with `slicer-lnm engine` when `slicer-lnm` is on
PATH, is `SLICER_LNM=/path/to/slicer-lnm` or is the command that wrote the script, otherwise with
`PythonSlicer -m LesionNetworkMappingLib.engine` (the `PythonSlicer` on PATH, else the one that wrote the
script); `PYTHONSLICER=/path/to/PythonSlicer` forces the second. `OUT=folder sh reproduce.sh` writes the maps to
`folder`. `reproduce_pipeline.sh` re-runs the whole pipeline from `pipeline.json` (`ACCEPT_TERMS=1 sh
reproduce_pipeline.sh`) with `slicer-lnm pipeline run` when `slicer-lnm` is on PATH, is `SLICER_LNM` or is the command that
wrote the script, otherwise with `PythonSlicer`; `PYTHONSLICER` forces the second.

## Licences and data

- The images and the wheel contain the library code only: no connectome, template, atlas or lesion. Mount
  the data at run time. A connectome is used only after `--accept-terms` (its data use terms are recorded
  in the provenance; a GSP1000-derived result carries the GSP acknowledgement in methods.md).
- `slicer-lnm` never downloads anything (`SLICER_LNM_OFFLINE=1` in the images); templates and atlases are
  read from `--template` and `--atlas`.
- Code: the 3D Slicer licence (BSD-style, see `License.txt` at the root of the sources). The images also
  contain Python, numpy, scipy (BSD) and Pillow (MIT-CMU) on Debian (`python:3.12-slim`),
  under their own licences.
