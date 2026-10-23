# Lesion Network Mapping

Lesion network mapping (LNM) asks which brain networks a lesion belongs to. The lesion is used as a seed. The
module looks up how that seed is correlated with every other brain voxel in a large set of healthy people (a
**normative connectome**), and the result is a map of the brain regions that are functionally connected to the
lesion. Maps of many lesions that cause the same symptom can then be combined and compared.

This module is part of Slicer Studio (it is the **Map** workspace), and it also works in the classic interface. It
is not part of official 3D Slicer. The maps it writes are ordinary NIfTI files that official 3D Slicer, FSLeyes,
nilearn and Lead-DBS open. See [Slicer Studio](studio.md) for the workspaces and keys.

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

## Statistics

### Thresholds are rules

A threshold is not a number but a **rule** that is resolved again every time it is displayed, from the number of
subjects N and the number of voxels V of the map. The derived line under the threshold always shows the result, for
example "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7". No threshold is carried over from one map
to another by accident.

| Rule | Meaning | Notes |
| ---- | ------- | ----- |
| **FWE (Bonferroni)**, the default | \|T\| at which p < α/V, two-sided (α = 0.05) | 5.22 at V = 228,483 and 5.27 at V = 292,019, for N = 1000 |
| **\|T\| ≥ 7** (the convention) | T = 7 | Defined for about N = 1000 and **disabled for N below 900** unless you choose "Use anyway". A banner appears when N differs by more than 10% from the reference N |
| **Fz ≥ x** | an effect size on the mean Fz | Does not depend on N; the statistic switches to Fz |
| **Custom T** | a value you type | A banner appears when the map's N differs from the N it was chosen for |
| **Top x %** | the value above which the top x % of in-mask voxels lie (default 10) | Does not depend on N; there are no p values. This is the only kind of rule used with the fast (parcel) mode |

The Fz and top-percent rules are alternatives to a T rule, never "equivalents": no Fz value corresponds to a given T,
because T depends on the spread of the values at each voxel.

Every map also shows the **percentage of in-mask voxels above the rule**. For reference, the |T| ≥ 7 convention
passes about 64% of the voxels of a map of a random lesion at N = 1000, so a large share of voxels
can pass a threshold that looks strict.

**Global signal regression.** The connectome's metadata says whether its preprocessing included global signal
regression (GSR). Without GSR, a symmetric |T| rule shows a banner ("Non-GSR connectomes centre uninformative
connections above 0; consider asymmetric or effect-size thresholds (Cohen & Fox 2021)"). With GSR, negative peaks are
annotated that the size of negative correlations is influenced by GSR (Murphy & Fox 2017).

The threshold you choose first is the primary result. Other thresholds can be shown as sensitivity checks and report
"conclusion flips at …"; there is no selection of the threshold that looks best.

Moving the threshold changes only the transparency of the colour table, so it is instant.

### Cohort statistics

When the seed is a **Cohort** (the lesions of a study), the module can combine the per-lesion maps. The group
statistics use the **mean Fz** by default; T can be chosen, and the choice is recorded in the output.

- **Overlap and sensitivity.** Each lesion's map is thresholded with the rule, and the overlap count (or percent) of
  lesions that reach it at each voxel is shown, as is the sensitivity map (voxels reached by at least k% of the
  lesions, default 90%). These are the classic maps; they are descriptive and are labelled so. An overlap map by
  itself does not show that a network is specific to a symptom.
- **Specificity: the general linear model.** Cases are compared with controls, or a continuous score is related to the
  maps, with a voxelwise linear model and **permutation inference** (Freedman–Lane, 5000 permutations by default,
  exhaustive when the number of distinct permutations is smaller). The family-wise error corrected p value
  (maximum statistic over voxels) is the primary result and the false discovery rate (Benjamini–Hochberg) secondary.
  The smallest p value that the number of permutations allows is stated. There is no cluster-extent inference;
  threshold-free cluster enhancement is an option. A Hedges g, a partial correlation or the mean Fz accompanies every
  p map.
  - **Design types:** `within_cohort` (every block of the data holds both groups), `cases_vs_control_database` (the
    controls are another dataset, which makes permutation free but leaves confounds that the model cannot adjust
    for; they are listed under Limitations), `continuous` and `one_sample`.
  - **Lesion volume is a covariate and is on by default.** Turning it off asks for a short reason, which is recorded.
  - The design is checked before it runs: a rank-deficient design, no intercept, maps from different connectomes,
    non-finite values, fewer than 100 distinct permutations, a variance inflation factor above 5, fewer than 10 per
    group and a variance ratio above 4 are blocked or flagged.
  - Rows without the covariates or the score are left out (listwise) and the count is shown. Missing values are never
    filled in.
- **Symptom LNM (sLNM).** The partial correlation of each voxel with a continuous score (the lesion volume as a
  covariate), with permutation tests that exchange scores only within blocks, and a leave-one-dataset-out check when
  there are at least two datasets of at least 10 subjects each.
- **Legacy two-group maps** (Welch t of the mean Fz maps, and the Liebermeister statistic of the binarised maps) are
  available, labelled "legacy two-group, no covariates".

The statistics run as background jobs that can be stopped and resumed.

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

**Where to get a connectome.** The module is made for the **Brain Genomics Superstruct Project (GSP1000)**
connectome, the one used for most lesion network mapping papers. GSP1000 is **not** included with Slicer Studio and
**is not downloaded by it**. Obtain it yourself from the Harvard Dataverse (doi:[10.7910/DVN/ILXIKS](https://doi.org/10.7910/DVN/ILXIKS))
under its **Data Use Terms**, and convert it once. Time series that are already preprocessed for Lead-DBS
(doi:[10.7910/DVN/KKTJQC](https://doi.org/10.7910/DVN/KKTJQC)) can be converted too. For trying the module, **Create
synthetic demo** in the connectome manager builds a small connectome of random numbers with planted networks on your
computer. It is labelled "SYNTHETIC — not real data" in the module, in every sidecar and on figures; never use it
for a conclusion.

**Data use terms.** The terms text of a connectome is shown in the Connectome card. **Compute stays disabled until you
tick "I accept these data use terms"** for that connectome. The acceptance is stored for that connectome (as a hash of
its manifest), and every map made from GSP data carries the terms in its sidecar (`"terms": "GSP-DUT-2014"`) and the
acknowledgement that the terms require in publications. The module never uses an account or a credential of its own.

:::{note}

**Connectome policy.** Connectomes, and any file derived from the GSP1000 data (bundles, the fast index, reference
maps, degree maps), are never bundled with Slicer Studio, and in this version are never downloaded by it. If a later
version offers a download, it will happen only when you ask for it, and only after the provider's own terms have been
shown to you and you have accepted them. Connectomes whose terms are not stated are only linked to.

:::

### Data downloaded on request

Templates and atlases are not bundled (apart from the 2 mm MNI152NLin2009cAsym template, which Studio includes) and are
**downloaded only when you ask for one**, for example when you ask for the 1 mm template, when a normalization needs a
template of the family of your connectome, or when you choose an atlas for cluster labels in a report. Each download
is checked against its hash, recorded the first time it is downloaded, and shown with its licence. The catalogs that
list them ship with the application and are never refreshed from the network.

| Resource | Licence | Notes |
| -------- | ------- | ----- |
| MNI152NLin2009cAsym 2 mm T1w and brain mask | MNI/McGill permissive licence | **Included** with Studio. The notice is in [Slicer Studio](studio.md#notices) |
| MNI152NLin2009cAsym 1 mm T1w and brain mask; 2 mm tissue probability maps | MNI/McGill permissive licence | On request, from TemplateFlow |
| MNI152NLin6Asym 1 mm and 2 mm T1w and brain mask | **FSL licence terms** (non-commercial use) | On request only, after the terms are shown; never included |
| Transforms between MNI152NLin2009cAsym and MNI152NLin6Asym (about 205 MB each) | follows the templates | On request; used by normalization |
| Schaefer2018 400 and 1000 parcels, 7 and 17 networks | MIT | On request. The default atlas of reports is Schaefer-400 7-network |
| Yeo2011 7 and 17 networks | MIT | On request |
| Tian subcortex, scales I to IV | Melbourne MSA terms | On request |
| Harvard–Oxford cortical and subcortical (th25) | CC BY-SA 4.0 | On request. Also used for the lesion location chips |
| MIAL67 thalamic nuclei | CC BY 4.0 | On request |
| HCP-MMP1 (volumetric projection) | CC BY 4.0 | On request. Use with care: the projection is approximate |
| Harvard Ascending Arousal Network atlas | CC0 | Link only: download it yourself and adopt the file |
| Buckner2011 cerebellum, MASSP20, AAL3 | licence not stated | On request with a grey badge: terms are not stated |
| Brainnetome | custom | Link only |
| Connectomes (GSP1000, Lead-DBS connectomes) | GSP Data Use Terms, or not stated | **Not downloaded**: see the connectome policy above |

The licence shown here is the one recorded in the catalog; the catalog is the authority. A resource marked with an
amber or grey badge asks you to read its terms first.

### Templates and their notices

The templates are in the MNI space of two families. **MNI152NLin2009cAsym** (the "ICBM 2009c nonlinear asymmetric"
template; Fonov et al., NeuroImage 2011, doi:[10.1016/j.neuroimage.2010.07.033](https://doi.org/10.1016/j.neuroimage.2010.07.033);
TemplateFlow, Ciric et al., 2022) is the template of Slicer Studio's Draw workspace. **MNI152NLin6Asym** is the
template of FSL and of the GSP1000 connectome, and its use follows the FSL licence terms. Lesions traced on one
and mapped on a connectome of the other are in spaces that differ by a few millimetres; the module shows a notice,
and the normalization step can warp a lesion into the family of the connectome.

## Intake

### Bringing lesions in

**Library** loads lesion masks and builds a **study** from a folder (see [Slicer Studio](studio.md#the-study-folder)).
Draw traces lesions on the MNI template, or on the patient's own scan.

- **Space.** Each lesion is identified as being in MNI152NLin2009cAsym or MNI152NLin6Asym space (from its grid, 1 mm or 2 mm), or
  in the scan's own **native** space. Studio never guesses: a lesion on an unknown grid is treated as native.
  The Seed card says "Native space: normalize to MNI first" and links to the Normalize card.
- **Checks while you trace.** Chips on the Lesion card in Draw show the side against the side that a report names
  (only in MNI space; in scan space they say "side unknown"), where the lesion lies against an atlas and, if you
  enter it, the location named in the paper, the share of cerebrospinal fluid, the cleanliness of the mask, a
  possible duplicate of another lesion of the study, and the dimensionality of the tracing. One-click fixes (mirror
  left to right, binarize, clip to the brain) are undoable, keep the original file and are recorded in the study.
- **Single-slice tracings.** When a lesion was traced on one slice (2D), Studio asks how to turn it into a volume:
  keep it single-slice, extrude it to a total thickness with fractional edges, or extrude it symmetrically as a
  binary mask. The choice is stored in the study.

### Normalize native-space lesions to MNI

**Why.** Network mapping needs the lesion in the connectome's space. A lesion drawn on a patient's own scan is
carried to MNI space by registering the scan to a template.

**Method.** The scan is registered to the template with the lesion excluded from the similarity measure
("cost-function masking", Brett et al., 2001; Andersen et al., 2010), in an affine stage followed by a B-spline
stage (BRAINSFit, which ships with Slicer). Without masking, the registration tends to shrink the lesion or to pull
tissue into it, so that the lesion's size and place change. The lesion is warped with linear interpolation onto the 1 mm
grid of the template family, saved as a fraction (`desc-lesionProb`) and as a binary mask (`desc-lesion`). The scan
is warped as well, for review. For a FLAIR, T2-weighted, DWI or CT scan that has a T1-weighted scan of the same person,
use the T1-weighted scan as an intermediate step. Without one, the registration uses the scan alone, is labelled
experimental, and always asks for review. A head with a strong tilt gets extra starting positions (a rotation search)
when the first one does not fit.

**Quality** is **Standard** (default), **Fine** (a finer B-spline grid) or **Affine** (no B-spline). Standard takes
about 45 seconds and Fine about 85 seconds for a 1 mm test head on the development computer; real scans take longer.

**Outputs** are written to `derivatives/normalization/<subject>/` of the study (or, without a study, to `normalization`
in the LNM data folder), with these names:

| File | Content |
| ---- | ------- |
| `<sub>_space-<tpl>_res-1_desc-lesion_mask.nii.gz` (+ `.json`) | the binary lesion in template space |
| `<sub>_space-<tpl>_res-1_desc-lesionProb_mask.nii.gz` | the fractional lesion |
| `<sub>_space-<tpl>_res-1_desc-preproc_<modality>.nii.gz` | the warped scan |
| `<sub>_from-<modality>_to-<tpl>_mode-image_xfm.h5` | the transform |
| `<sub>_desc-normqc.png`, `<sub>_desc-normqc.json` | the QC image and metrics |
| `normalization.json` | the settings and software versions |

The study row is pointed at the normalized lesion, so that `lesions/` keeps the lesions as you drew them.

**Quality control.** The result is a **suggestion** ("review" or "looks ok"), never a verdict. The QC mosaic shows
axial, coronal and sagittal slices through the lesion (the patient's left on the left of the image, with L/R stamps).
The metrics: the similarity inside the brain outside the lesion; the lesion's volume before and after, relative to
the change in brain volume (so that a small head is not flagged only because the template is larger); the local
volume change (the logarithm of the Jacobian) in the lesion compared with the brain; the share of folded voxels; the
share of the warped lesion outside the brain and in the ventricles; and the overlap of the warped and the template
brain mask. The thresholds were calibrated on one synthetic data set (the MRHead scan with simulated chronic stroke
lesions) and are not validated on patient data. Two of them (the brain-mask overlap and the ventricle share) have not
been calibrated at all. Always look at the mosaic. With **review required** in the study, a normalized lesion is
used in analyses only after a reviewer passes it (`norm_qc`).

**Limits.** The registration works best for T1-weighted scans with a lesion that is small compared to the
hemisphere. Large lesions, strong atrophy, a very different field of view and scans without a T1-weighted partner are
less reliable, and the suggestion says so. ANTs (SyN) is not available in this build.

**Command line** (in a terminal, with the `PythonSlicer` of the installation; quote paths with spaces):

```
PythonSlicer -m LesionNetworkMappingLib.normalize check
PythonSlicer -m LesionNetworkMappingLib.normalize run --scan "scan T1.nii.gz" --lesion "lesion.nii.gz" --out "out folder"
PythonSlicer -m LesionNetworkMappingLib.normalize batch --study "my study" --all-native
PythonSlicer -m LesionNetworkMappingLib.normalize warp --run "out folder" --image "other image.nii.gz" --kind image
```

`check` tells which templates and tools are available, `batch` normalizes every included native-space subject of a
study one after the other, and `warp` applies a finished run's transform to another image.

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
- `derivatives/lnm/runs.jsonl`: a log of the runs of a study.

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
