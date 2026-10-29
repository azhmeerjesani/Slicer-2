# Lesion Network Mapping

Lesion network mapping (LNM) asks which brain networks a lesion belongs to. The lesion is used as a seed. The
module looks up how that seed is correlated with every other brain voxel in a large set of healthy people (a
**normative connectome**), and the result is a map of the brain regions that are functionally connected to the
lesion. Maps of many lesions that cause the same symptom can then be combined and compared.

This module is part of Slicer Studio (it is the **Map** workspace), and it also works in the classic interface. It
is not part of official 3D Slicer. The maps it writes are ordinary NIfTI files that official 3D Slicer, FSLeyes,
nilearn and Lead-DBS open. See [Slicer Studio](studio.md) for the workspaces and keys.

This page is the overview. Three pages go deeper:

- [Statistics](lnm_statistics.md): thresholds per N, the general linear model, symptom mapping, VLSM, null models, the
  reference maps, validation, robustness, calibration, and what is deliberately not offered.
- [Data](lnm_data.md): the connectome policy, the connectome catalog and the download, licences, integrity, subsets,
  templates, atlases and tutorial cohorts, Lead-DBS interoperability and the headless twin.
- [Intake](lnm_intake.md): the study table and its roles, tracing (also from figures), the single-slice policy, QC chips,
  review, blinding and agreement, normalization and its QC, literature sources and PRISMA, and the share export.

## Research use only

Every sidecar, the methods text, the report and the footer of the module carry this statement:

> Research use only — not for clinical decisions. Lesion network mapping specificity is debated (van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7; Siddiqi et al., 2026, bioRxiv, doi:10.64898/2026.02.24.707529; Zalesky & Cash, 2026, Nat Neurosci, doi:10.1038/s41593-026-02259-3).

The module avoids clinical wording: a map shows a "network associated with" a lesion, never the place that
"localizes" a symptom. A single-lesion map is a hypothesis about a network, and a connectome of healthy people is not
the connectome of the patient. Read the [QA section](#qa) before you interpret a map.

## Methods

**The method** is lesion network mapping as introduced by Boes et al. (2015) and reviewed by Fox (2018): a lesion mask
in standard (MNI) space is the seed; its time course in each subject of the normative connectome is correlated with
every voxel; the correlations of all subjects are combined.

**The engine** (`LesionNetworkMappingLib/engine.py`, plain numpy) does the following for each lesion and each
subject of the connectome:

1. The lesion is resampled onto the connectome's grid through the voxel-to-world matrices (never by voxel index),
   as the fraction of each voxel that is lesioned. Voxels outside the connectome's brain mask are dropped, and the
   share of the lesion that falls outside is reported.
2. Each run of the subject is demeaned and scaled to unit length once, when the connectome is built. The seed
   time course of a run is the lesion-weighted sum of the voxel time courses, demeaned and scaled to unit length
   too, so that a matrix product with the stored voxel time courses gives the Pearson correlation r of the seed with
   every voxel.
3. **r is averaged over the runs of a subject** (as Lead-DBS does), converted to **Fisher z** (the inverse hyperbolic
   tangent of r), and accumulated over subjects.
4. After the last subject the engine writes the **mean r**, the **mean Fz** and the **one-sample T statistic** of the
   subject-wise Fz values against zero (N−1 degrees of freedom). The sums use float64 (Welford's method), so a valid
   T map exists after any number of subjects.

The connectome time series are stored as **float16** to halve the disk space, and are converted to float32 one chunk
at a time for the products. All lesions of a run are columns of the same product, so mapping 100 lesions reads the
connectome once, not 100 times.

**Preview and all subjects.** *All* uses every subject of the connectome. *Preview* uses a random subset (100 subjects by
default, with a fixed random seed) and is always labelled as a preview. While a computation runs, a sharper map is
shown every 50 subjects. **Stop** keeps the last valid map, labelled with its N ("N=312 (partial)").

**Run in the background.** The engine runs as a separate process with the application's own Python. Closing the
module does not stop it, a crash of the computation does not take the application down, and the number of threads is
capped so that the computer stays usable.

## The Map workspace

The inspector of the Map workspace is a list of **cards** in a fixed order; the same cards appear in the Classic
interface. Cards that do not apply to the seed (Lesion, Cohort, Coordinates or Stimulation) are hidden, and the research-only
statement is always visible at the bottom.

| Card | What it is for |
| ---- | -------------- |
| Battery strip | six dots that show which of the checks below have been run |
| Recipe | the named analysis recipe and the state of the analysis plan ("Plan: open", "Plan: locked 2026-10-18 · 3f2a…") |
| Seed | Lesion, Cohort (n lesions), Coordinates or Stimulation |
| Normalize to MNI | native-space lesions (see [Intake](lnm_intake.md#normalize-native-space-lesions-to-mni)) |
| Connectome | the connectome in use: name, DOI, version, N, ages, GSR, voxels, the data use terms, and **Manage…** |
| Preflight | the checks before a run: study, connectome and its terms, design |
| Run everything | the automated cohort analysis (cohort seed) |
| Compute | **Map network** (or **Maps only** for a cohort) |
| Result | the map, its threshold rule and the percentage of voxels above it, the GSR banner |
| QA | the degree similarity and the relocated-lesion null chip |
| Cohort | tabs **Overlap**, **Contrast**, **Symptom** and **VLSM** |
| Nulls | null-referenced sensitivity |
| Validate | out-of-sample validation against baselines |
| Robust | threshold sweep, other connectome, split-half, influence |
| Clusters | cluster tables with atlas labels |
| Networks | the networks that a map overlaps |
| Circuits | a result re-seeded as a circuit; damage scores and targets |
| Compare | comparison with other maps and a library of reference maps |
| Calibrate | empirical false-positive rate and planted-network recovery |
| Report | the reporting checklist, the methods text and the publication bundle |
| Export | the maps with their sidecars |

**Ctrl+Enter** runs the preflight and then the computation. The status chip follows the running job ("Mapping 34%",
"GLM 1200/5000 · 3 min left").

## A first analysis, step by step

1. **Open or create a study** in the Library (drop a folder of lesion masks, or use a
   [tutorial cohort](lnm_data.md#templates-atlases-and-tutorials)). Check that the lesions are in MNI space or normalize
   them first ([Intake](lnm_intake.md)). Confirm the role of any column you want to use (a group or a score).
2. **Choose a connectome** in the Connectome card. If you have none, **Manage… > Get a connectome…** offers the catalog,
   after you have read and accepted the provider's terms ([Data](lnm_data.md#getting-a-connectome)), and **Create
   synthetic demo** makes a toy one for learning.
3. **Open Map**, set the seed to **Cohort**, and read the **Preflight** card. It says what is missing before anything runs.
4. **Run everything** (or **Maps only** and the single steps). Look at the overlap and the QA chips first.
5. **Compare cases with controls or relate a score** in the Cohort card ([Statistics](lnm_statistics.md)), look at the
   effect size next to the p map, and run **Nulls**, **Validate** and **Robust** before you believe a result.
6. **Report and export.** The Report card shows the checklist and writes the methods text; **Export** copies the maps with
   their sidecars. The result is research-only and is described as a "network associated with" the lesions.

## Statistics

The statistics have their own page: [Lesion Network Mapping: statistics](lnm_statistics.md). In short, a threshold is a
rule that is resolved again from N and V each time it is shown (FWE by default; the |T| ≥ 7 convention only from N = 900),
the cohort tests are Freedman–Lane permutation tests with lesion volume as a covariate, and nothing in the module issues
a "specific" verdict.

## QA

Every map is checked against the main known confound of lesion network mapping: networks that look "specific" may
mostly reflect that some brain regions are connected to almost everywhere (they have a high **degree**), and any
lesion near them will produce a similar map (van den Heuvel et al., 2026).

- **Degree similarity.** Each map is compared with reference maps of the connectome: the mean correlation profile
  (μC_r, the mean over subjects of each voxel's mean r to all other voxels), the first three principal components of the
  voxel-by-voxel correlation matrix (PC1–PC3) and a sampled "hubness" map. The QA card shows r and Spearman ρ with each
  one, and R² on the four together. The references are built once per connectome and subject list, as a job
  that takes about 15 to 25 minutes on a full-size connectome and starts only after you confirm it ("Build reference
  maps (~20 min)?").
- **Relocated-lesion null.** The lesion is moved to other places in the brain (rigid shifts, with at least 95% of the
  moved lesion inside the brain mask), and the same degree similarity is computed for each. By default there are 50
  relocations per lesion for single lesions; a cohort uses fewer as it grows (50 for up to 10 lesions, 10 up to 100,
  none above), and the relocations run in the same pass as the real maps. The result is a calibrated **chip**:
  - **below the 5th percentile:** "less degree-like than relocated lesions";
  - **5th to 95th percentile (amber):** "indistinguishable from relocated lesions on this axis";
  - **above the 95th (red):** "more degree-like than relocated lesions".

  The chip also states the resolution of the null ("2% at K=50"). A cohort is calibrated against null cohorts.
- **Coverage.** The share of the lesion that lies outside the connectome's brain mask, the lesion's size in
  voxels at the connectome's resolution, the fractional sum and a notice if the lesion and the connectome are in
  different MNI spaces. The N and degrees of freedom and a "preview"/"partial" badge are always shown.
- **Debiased view.** A toggle in the result shows the map with the effect of μC_r and PC1 regressed out. It is
  descriptive, "not an inferential correction".
- **Connectome sampling stability.** Optionally, the map computed from the odd-numbered and the even-numbered
  subjects separately are compared.

All QA values are written into the sidecar and `qa.json`. The QA card opens by itself when a chip is amber or red.
A chip does not say that a map is right or wrong; it says how much of the map might be explained by degree alone.

## Data

The data have their own page: [Lesion Network Mapping: data](lnm_data.md). This section keeps the format of the connectome
bundle.

### The connectome (`.lnmc` bundle)

A normative connectome is stored as a folder called `<name>.lnmc`:

```
<name>.lnmc/
  connectome.json            name, description, citation, dataUseTerms, shape, affine, nVoxels, subjects, dtype
  mask.nii.gz                the brain mask on the connectome's grid
  subjects/<id>.f16.npy      (voxels × time points), float16, runs demeaned and scaled to unit length
  subjects/<id>.norms.npy    the original length of each run (float32)
  derived/                   optional: degree and reference maps
```

`connectome.json` has `"format": "lnmc"` and `"version": 1`. Optional keys hold the provenance: source DOI and
version, licence, hash of the terms text, preprocessing (including GSR), population, named subsets of subjects. The
voxels are the non-zero voxels of the mask, in C order; the voxel-to-world matrix of the mask is used for every
output.

**Converting a connectome.** **Map > Connectome > Manage…** opens the connectome manager, which registers a bundle,
converts one (a folder of preprocessed 4D NIfTI runs on one brain mask, or a Lead-DBS fMRI connectome folder),
builds the degree map and the reference maps, and removes a registration (never the files). A conversion shows the
disk space it needs before it starts and can be resumed. On the command line:

```
PythonSlicer -m LesionNetworkMappingLib.manager convert --source nifti --input DIR_OR_GLOB --mask MASK.nii.gz --out NAME.lnmc
PythonSlicer -m LesionNetworkMappingLib.manager convert --source leaddbs --input DATASET_DIR --out NAME.lnmc
PythonSlicer -m LesionNetworkMappingLib.manager estimate --source nifti --input DIR_OR_GLOB --mask MASK.nii.gz
```

**Where to get a connectome.** The module is made for the **Brain Genomics Superstruct Project (GSP1000)** connectome, the
one used for most lesion network mapping papers. GSP1000 is **not** included with Slicer Studio and is **never downloaded
by itself**. You can fetch it from the Harvard Dataverse (doi:[10.7910/DVN/ILXIKS](https://doi.org/10.7910/DVN/ILXIKS)) with
**Manage… > Get a connectome…**, only after you have clicked "Load terms", read the provider's current Data Use Terms and
ticked "I accept". Or obtain it yourself and convert it once. Time series that are already preprocessed for Lead-DBS
(doi:[10.7910/DVN/KKTJQC](https://doi.org/10.7910/DVN/KKTJQC)) can be converted or fetched too. For trying the module,
**Create synthetic demo** in the connectome manager builds a small connectome of random numbers with planted networks on
your computer. It is labelled "SYNTHETIC — not real data" in the module, in every sidecar and on figures; never use it for a
conclusion.

**Data use terms.** The terms text of a connectome is shown in the Connectome card. **Compute stays disabled until you tick
"I accept these data use terms"** for that connectome. The acceptance is stored for that connectome (as a hash of its
manifest), and every map made from GSP data carries the terms in its sidecar (`"terms": "GSP-DUT-2014"`) and the
acknowledgement that the terms require in publications. The module never uses an account or a credential of its own.

:::{note}

**Connectome policy.** Connectomes, and any file derived from the GSP1000 data (bundles, the fast index, reference maps,
degree maps), are never bundled with Slicer Studio. A connectome is downloaded only when you ask for it and have accepted
the provider's live terms text, and sources whose terms are not stated are only linked to.

:::

### Data downloaded on request

Templates, atlases, tutorial cohorts and reference maps are not bundled (apart from the 2 mm MNI152NLin2009cAsym template)
and are **downloaded only when you ask for one**. Each download is checked against its hash, shown with its licence, and
cached. The licence of every resource is in the table of [Data](lnm_data.md#templates-atlases-and-tutorials); a resource
with an amber or grey badge asks you to read its terms first. The 2 mm template's notice is in
[Slicer Studio](studio.md#notices).

### Templates and their notices

The templates are in the MNI space of two families. **MNI152NLin2009cAsym** (the "ICBM 2009c nonlinear asymmetric"
template; Fonov et al., NeuroImage 2011, doi:[10.1016/j.neuroimage.2010.07.033](https://doi.org/10.1016/j.neuroimage.2010.07.033);
TemplateFlow, Ciric et al., 2022) is the template of Slicer Studio's Draw workspace. **MNI152NLin6Asym** is the template of
FSL and of the GSP1000 connectome, and its use follows the FSL licence terms. Lesions traced on one and mapped on a
connectome of the other are in spaces that differ by a few millimetres; the module shows a notice, and the normalization
step can warp a lesion into the family of the connectome.

## Intake

Intake has its own page: [Lesion Network Mapping: intake](lnm_intake.md). It covers the study table and its roles, how
lesions are brought in and traced (also from published figures), the QC chips, review, blinding and agreement,
normalization of native-space lesions and its quality control (`PythonSlicer -m LesionNetworkMappingLib.normalize ...`),
literature sources with PRISMA counts, and the share export.

## Automated cohort analysis (Run everything)

With the seed set to **Cohort**, the **Run everything** card runs the whole analysis of a study with one click. The
main button of the inspector (and **Ctrl+Enter**) reads "Run everything · N lesions", and the **Compute** card's
**Map network** becomes **Maps only**.

**What it does,** in order, skipping what does not apply:

1. **Prepare:** checks (preflight) that the study, the connectome and its terms are ready, and plans the run.
2. **Normalize** native-space lesions, when there are any and the option is on.
3. **Reference maps,** only if you chose to build them.
4. **Network maps** of all lesions, in batches, with a cache per lesion.
5. **Null models** (relocated lesions) as described under [QA](#qa).
6. **Statistics:** overlap, the lesion set summary, specificity (the general linear model) and, for each confirmed score
   column, symptom LNM, then the conjunction of the maps.
7. **QA and clusters:** the QA of the group maps and a table of clusters with atlas labels.
8. **Export** of the maps with sidecars, and the methods text.
9. **Report:** an HTML report.

**Defaults.** FWE as the primary rule, 5000 permutations for the general linear model, lesion volume as a covariate, a
sensitivity of 90%, the number of relocations by the size of the cohort, native-space lesions normalized, and the
threads chosen from the number of cores. The **⋯** button opens the options (extra threshold rules, permutations, TFCE, which
scores to use, nulls, normalization, an atlas for the cluster labels, the memory budget and threads). The options are
stored in `Studio/LNM/Pipeline/Options`. **Keep the computer awake during long runs** prevents the computer from sleeping during a run.

**Running.** The card lists the stages with a symbol for each (waiting ○, running ●, done ✓, reused ↺, skipped –, failed !) and
a progress bar with an estimate. The estimate is computed from the number of lesions and the connectome and is shown before you
start ("≈ 12 min (estimate; 1 pass over …)"); read it as an order of magnitude. The run is a separate process:
**Stop** (never the Esc key) ends it cleanly, the application can be closed, and you can **re-attach** to a run
that is still going when you come back. **Resume.** Work that is complete (the stage, the per-lesion maps, the batches) is
reused when you run again, and only what is missing or has changed is computed. A **lock** prevents two runs from
writing the same output folder.

**The result row** has **Open report**, **Show folder**, **Load maps** (loads the main maps into the views) and
**Warnings**. When a run is done, Studio merges the results into the study (the lesions' status becomes "mapped").

**Output folder.** `derivatives/lnm/conn-<connectome>[_n-<N>]/` in the study:

| Item | Content |
| ---- | ------- |
| `pipeline.json` | the plan, the settings and the state of every stage (paths relative to the folder) |
| `report.html` | the report: methods, figures, QA, clusters, a checklist |
| `<sub>_conn-<label>_desc-{AvgR,AvgRFz,T}[PreviewN<k>]_funcmap.nii.gz` | the per-lesion maps, copied from the cache |
| `<sub>_conn-<label>_lnm.json` | the sidecar of each lesion (N, threshold rule, QA, terms, disclaimer) |
| `group_conn-<label>_desc-<name>_map.nii.gz` + `.json` | group maps: overlap, sensitivity, T and p maps, conjunction, sLNM |
| `peaks.tsv`, `clusters_<name>.tsv` | peak and cluster tables |
| `inputs/lesions/` | copies of the lesion masks that were analysed |
| `qa.json`, `lesionset.json`, `checklist.md`, `methods.md` | QA values, lesion set summary, the checklist and the methods text |
| `provenance.json`, `reproduce.sh`, `reproduce_pipeline.sh` | what was run, and the command that repeats it |
| `stages/`, `.pipeline/` | the folders of the stages, the lock, the progress and the log |

The per-lesion maps are **copies**, not links into the cache, so editing an exported file never changes the cache.
Files meant for sharing (the sidecars, the tables, `pipeline.json`) contain paths relative to this folder. `provenance.json`, `reproduce.sh`
and `reproduce_pipeline.sh` contain local paths on purpose.

**Time.** The time of a run is mostly the time to read the connectome once per batch of lesions (a GSP1000-size connectome
is about 126 GB), so it depends mainly on the speed of the disk and little on the number of lesions. The
design estimate for GSP1000 on a fast solid-state disk is a few minutes per pass; the figure has not yet been
measured on a full-size connectome by this project, and a connectome on a network drive can take much longer.
Normalization adds about a minute per native-space lesion, and the permutation tests about 10 seconds per 1000
permutations on a 292,019-voxel map.

**Command line.** The same pipeline can be run without the application, for example on a server. All commands use
`PythonSlicer -m LesionNetworkMappingLib.pipeline`:

```
... pipeline init --from FOLDER_OF_LESIONS --participants participants.csv --study NEW_STUDY
... pipeline plan --study STUDY --bundle NAME.lnmc
... pipeline run  --study STUDY --bundle NAME.lnmc --accept-terms
... pipeline status --out OUTPUT_FOLDER --json
... pipeline report --out OUTPUT_FOLDER
```

`init` creates a study from a folder (a `participants.csv` or `.tsv` supplies groups and scores); `plan` shows what
would be computed and an estimate without running; `run` runs; `status` reads the progress of a run; `report` writes
the report again. **`--accept-terms` states that you have accepted the data use terms of the connectome**; the command
refuses to run without it (exit code 4). Other exit codes: 0 done, 1 error, 3 stopped, 5 blocked by the preflight, 6 no
eligible lesion, 75 another run holds the lock. Paths with spaces need quotes.

## Outputs

**Per lesion.** In `derivatives/lnm/conn-<label>[_n-<N>]/` (a preview adds `PreviewN<k>`):

- `<sub>_conn-<label>_desc-AvgR_funcmap.nii.gz`: the mean r; `…_desc-AvgRFz_…`: the mean Fisher z; `…_desc-T_…`: the T
  statistic (float32; the T file carries the degrees of freedom in its header).
- `<sub>_conn-<label>_lnm.json`: the **sidecar**: the software and its version, the connectome (name, version, hash of
  its manifest, preprocessing, terms), N and the hash of the subject list, the random seed of a preview, the threshold rule and
  the resolved value, the lesion file and its hash, the number of voxels at the connectome's resolution and the fractional sum,
  the QA values, the time, and the research-only statement. A map from the GSP1000 data also carries the terms and the
  acknowledgement.

**Per run.**

- `provenance.json`: all of the above plus the command line, timings, computer and numerical library versions.
- `methods.md`: a generated paragraph for a methods section: the method, the parameters, and the citations (Boes et al.
  2015; Fox 2018; the connectome's citation; the three papers of the specificity debate) and the research-only statement. Edit it before you use it.
- `reproduce.sh`: the exact command that repeats the run.
- `derivatives/lnm/runs.jsonl`: a log of the runs of a study (one line per run, with its kind: maps, glm, slnm, vlsm, nulls,
  validate, robustness, calibrate, fetch and others).

**Format.** All maps are NIfTI-1 files written with the **exact header of the connectome** (the same voxel-to-world
matrix in the sform and the qform), so they overlay without resampling in FSLeyes, nilearn, Lead-DBS and 3D Slicer. Slicer's own
writer would flip the file for radiological images, so the module writes them itself.

**Names** follow Lead-DBS, so that files can be exchanged with it: `_conn-<label>_desc-{AvgR,AvgRFz,T}_funcmap`.
Group maps use `group_conn-<label>_desc-<contrast><Stat>_map.nii.gz`, with `<Stat>` one of Tstat, Fstat, PUnc, PFwe,
PFdr, TfcePFwe, HedgesG, PartialR and R.

**Display.** A result opens as a volume on the MNI template with the shared colour table "LNM Diverging" (hot and cold,
transparent below the threshold) and the lesion as a white outline, opacity 80%. The colour table node is saved with
the scene, so a scene with a map looks the same in official 3D Slicer. **Export…** (Ctrl+E) copies the outputs of a
result to a folder you choose.

## Command-line tools

Every analysis step is a library job that also runs without the application, with the `PythonSlicer` of the installation
(or the `slicer-lnm` command of the [headless twin](lnm_data.md#the-headless-twin)). Each one reads and writes the same
files as the module, prints one JSON event per line, and uses the exit codes 0 done, 1 error, 2 usage or blocked design,
3 stopped, 75 another job holds the output folder (the pipeline adds 4, 5 and 6, see above). Run any of them with `--help`.

| Command (`PythonSlicer -m LesionNetworkMappingLib.…`) | What it does |
| ----------------------------------------------------- | ------------ |
| `engine` | the network map of one or more lesions on a connectome bundle |
| `cohortmaps` | the maps of a whole study, in batches, with a per-lesion cache |
| `cohort` | group maps, overlap and conjunction of a cohort |
| `glm`, `slnm`, `vlsm`, `roi` | permutation GLM, symptom mapping, lesion-symptom mapping, ROI tests |
| `nulls`, `calibrate`, `validate`, `robustness` | null models, calibration, validation, robustness |
| `reference`, `fastmap`, `qa` | reference maps, the fast parcel index, degree similarity |
| `circuits` | seed a circuit, score damage, score targets |
| `normalize` | `check`, `run`, `batch` and `warp` for native-space lesions |
| `pipeline` | `init`, `plan`, `run`, `status` and `report` (Run everything and its HTML report) |
| `manager`, `fetchconnectome`, `fetch`, `leaddbs`, `refmaps`, `tutorials` | connectome conversion and download, resources, Lead-DBS, reference maps, tutorial cohorts |
| `recipes`, `transforms`, `coords`, `lesionset`, `preflight`, `app` | recipes, transform tools, coordinate seeds, lesion-set summary, preflight, the BIDS App |

### Atrophy seeds (command line)

Atrophy network mapping (Tetreault 2020) is a command-line feature: the Seed card of the Map workspace offers Lesion,
Cohort, Coordinates and Stimulation, but not atrophy maps. The `coords atrophy` command thresholds one per-patient
w-map (or z-map; negative means atrophy) for each participant, writes the seeds and `lesions.tsv` to a run folder,
and `cohortmaps` then maps them like lesions:

```
PythonSlicer -m LesionNetworkMappingLib.coords atrophy --bundle CONNECTOME.lnmc --out RUNFOLDER \
    --wmap sub-01=sub-01_wmap.nii.gz --wmap sub-02=sub-02_wmap.nii.gz [--rule w<-2] [--weighted]
```

`--rule` is `w<-2` (the default), `w<-1.5`, `w<-2.5` or `top5%`; `--weighted` weights the seed voxels by -w instead of
using a binary seed. The command does not check the space of a w-map: each w-map must already be in the MNI space of
the connectome (it is placed where its own header says), and a w-map in another space gives a wrong map without a
notice.

## Citations

- Boes AD, Prasad S, Liu H, et al. Network localization of neurological symptoms from focal brain lesions. *Brain*.
  2015;138(10):3061-3075. doi:[10.1093/brain/awv228](https://doi.org/10.1093/brain/awv228)
- Fox MD. Mapping symptoms to brain networks with the human connectome. *N Engl J Med*. 2018;379(23):2237-2245.
  doi:[10.1056/NEJMra1706158](https://doi.org/10.1056/NEJMra1706158)
- van den Heuvel et al. *Nat Neurosci*. 2026. doi:[10.1038/s41593-025-02196-7](https://doi.org/10.1038/s41593-025-02196-7)
- Siddiqi SH, Horn A, Schaper FLWVJ, et al. The methodological foundations of lesion network mapping remain sound.
  *bioRxiv*. 2026. doi:[10.64898/2026.02.24.707529](https://doi.org/10.64898/2026.02.24.707529)
- Zalesky A, Cash RFH. Null models for lesion network mapping. *Nat Neurosci*. 2026;29:1025-1028.
  doi:[10.1038/s41593-026-02259-3](https://doi.org/10.1038/s41593-026-02259-3)
- Brain Genomics Superstruct Project (GSP1000): doi:[10.7910/DVN/ILXIKS](https://doi.org/10.7910/DVN/ILXIKS); see its Data Use
  Terms for the acknowledgement to include in publications.
- Fonov V, Evans AC, Botteron K, et al. Unbiased average age-appropriate atlases for pediatric studies. *NeuroImage*.
  2011;54(1):313-327. doi:[10.1016/j.neuroimage.2010.07.033](https://doi.org/10.1016/j.neuroimage.2010.07.033)
- Brett M, Leff AP, Rorden C, Ashburner J. Spatial normalization of brain images with focal lesions using cost function
  masking. *NeuroImage*. 2001;14(2):486-500.
- Andersen SM, Rapcsak SZ, Beeson PM. Cost function masking during normalization of brains with focal lesions: still a
  necessity? *NeuroImage*. 2010;53(1):78-84.
- Winkler AM, Ridgway GR, Webster MA, Smith SM, Nichols TE. Permutation inference for the general linear model.
  *NeuroImage*. 2014;92:381-397.
- Murphy K, Fox MD. Towards a consensus regarding global signal regression for resting state functional connectivity
  MRI. *NeuroImage*. 2017;154:169-173. (The threshold banner for connectomes without global signal regression also
  names Cohen & Fox, 2021.)
