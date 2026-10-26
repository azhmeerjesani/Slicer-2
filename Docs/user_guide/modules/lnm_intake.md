# Lesion Network Mapping: intake

This page describes how lesions get into a study of the [Lesion Network Mapping](lesionnetworkmapping.md) module, how they
are checked on the way in, and how a study is documented and shared. The tools are in the **Library** (the study list and
its cards), in **Draw** (tracing) and, for normalization, in both. You do not need to know 3D Slicer to use them: the
Library is the home screen of Slicer Studio, and a study is just a folder of files.

## The study and its table

A **study** is a folder with a `study.json` and a BIDS-style `participants.tsv` that has one row per subject (see
[Slicer Studio](studio.md#the-study-folder)). **Library** builds one from a folder of lesion masks (and scans), from a
drop, or from a [tutorial cohort](lnm_data.md#templates-atlases-and-tutorials). The lesion files stay where they are or
under `lesions/`; the table points at them with paths relative to the study.

### Columns and roles

Every column of the table has a **type** (numeric, categorical, ordinal, text, path or date) and a **role** in analyses
(`id`, `group`, `score`, `covariate`, `block`, `metadata`, `bookkeeping` or `path`). Designs, score pickers and reports are
built from declared roles, never from "all numeric columns" (which would offer bookkeeping columns such as the fit
residual of a figure as symptom scores).

- **Library > Study > Columns…** shows each column with the role it has, from the study's own `participants.json` (a
  BIDS data dictionary with the fork key `SlicerStudio`: `type`, `role`, `confirmed`), else from the registry of the
  standard columns, else inferred from the values with the role "unknown". A role is only used for an analysis after you
  confirm it. BIDS tools and official 3D Slicer ignore the fork key.
- **Optional columns** (the source record, imaging details, case fields such as the reported side and location, workflow
  fields such as the review status, and analysis columns such as damage scores) are not added to a study until
  something writes them. A column that has no value is shown as missing and is never imputed.
- **Cohort diagnostics.** The Cohort card of the Library summarises the lesion set before any analysis: counts, volumes,
  hemisphere split, dimensionality, source types, tissue fractions, balance between cases and controls (standardised
  mean differences, flagged above 0.25), and the literature context.
- **Control sets.** A set of control lesions (a folder, registered once; 500 to 1000 lesions are typical) can be linked to
  any number of studies without copying a file (**Add control set…**). Linked rows get `group = control`,
  `source_type = control_db`. A balance report compares the cases with the set, and a seeded matched subsample can be
  chosen. The set's own licence governs its lesions, and they are never exported.

## Bringing lesions in

- **Space.** Each lesion is identified as being in MNI152NLin2009cAsym or MNI152NLin6Asym space (from its grid, 1 mm or
  2 mm), or in the scan's own **native** space. Studio never guesses: a lesion on an unknown grid is treated as native, and
  a grid that does not name its space (the TemplateFlow 2009 grids hold 2009c and 2009a images) is asked once.
  The Seed card says "Native space: normalize to MNI first" and links to the Normalize card.
- **Draw** traces lesions on the MNI template, or on the patient's own scan. **Done → next** saves the
  lesion, writes the study row and opens the next case of the queue; the queues (untraced, needs review, changes
  requested, flagged, this paper, second tracing) are in the Library.
- **Lesions from papers.** Case reports are first-class: the Sources card holds the papers, each case is a row
  `sub-<record><nn>` with its DOI, PMID and citation, and a case can be traced from the paper's figure.

### Tracing from a published figure

About a third of lesion network mapping papers show the lesion only as a figure. **Draw > Trace from figure…** (also
a dropped picture, `Ctrl+V` of a picture, or the Search entry of that name) places the figure over the template and fits
it with landmarks. Only a crop of the figure is kept, and no lesion is extracted automatically.

1. **Crop** the part of the figure that shows the slice. Only the crop is saved.
2. **Plane**: axial, coronal or sagittal, with the cut height and, if the slice is oblique, a pitch and a roll.
3. **Convention.** "Patient's right is on the Left | Right of this figure". Radiological (patient's right on the left of the
   picture) is assumed until you confirm it, once per source record, and an unconfirmed convention blocks **Done**.
   Studio shows R/L letters and a side tag on the figure views so that a left-right flip is never silent.
4. **Landmarks.** Place pairs of points on the figure and on the template (names are suggested). A similarity fit is the
   default; an affine fit needs at least 4 pairs.
5. **Fit.** A residual chip reads "RMS 1.6 mm · LOO 2.3 mm (5 pairs)" in green, amber or red. It measures the consistency
   of the landmarks, not the accuracy of the lesion. A warning appears for a landmark that is off the cut plane or when a
   mirror image would fit better.

Then paint the lesion in the Red view (which shows the template on the cut plane with the figure over it); every panel
of a figure paints into the same lesion. **Show source** rebuilds the overlay of a saved case within 300 ms. The crop and
the fit are saved as `sources/<record>/fig-<k>.png` and `fig-<k>_fit.json` (paths relative; the original figure, which is
often copyrighted, is never kept or exported), and `trace_method` records "figure".

### Single-slice tracings and the extrusion policy

When a lesion was traced on one slice (2D), the Lesion card asks how to turn it into a volume, once per study:

- **Keep as drawn** (a single slice).
- **Extrude to a total thickness** (default 2 mm) with fractional edge slices, written as `desc-lesionProb` and mapped with
  the engine's probabilistic option; or
- **Extrude as a binary mask** by a whole number of voxels on each side.

The study stores the choice and its semantics ("total, centred on the drawn plane"), and the methods text reports it.
Several single-slice drawings on different planes are "multi2D". The traced mask is never modified: the extruded file is
derived and the original is kept.

### Checks while you trace (QC chips)

Chips on the Lesion card update within about 15 ms of an edit (a first stage on the lesion crop) and then add the atlas
labels:

| Chip | What it checks |
| ---- | -------------- |
| Side | the traced hemisphere (at least 80 % of the lesion on one side) against the side the report names, in MNI space only; in scan space it says "side unknown" |
| Location | where the lesion lies against an atlas and, if you enter it, the location named in the paper |
| CSF | the share of the lesion in cerebrospinal fluid or the ventricles (refused across template families) |
| Mask hygiene | values outside {0, 1}, an empty mask, a lesion smaller than a connectome voxel, more than 5 % outside the brain, a header whose sform and qform disagree, a grid that is not the study's template, several parts |
| Duplicate | another lesion of the study with a high Dice (a duplicate index on a 2 mm grid, built in the background) |
| Dimensionality | 2D, multi2D or 3D, and the number of slices per plane |

**One-click fixes** (mirror left to right, binarize, clip to the brain, mark as duplicate, "not a duplicate") are undoable,
keep the original file, and are recorded in the study (an audit line each). The mirror is a mirror of the world x axis and
is labelled **approximate**. The Library shows a **Flags** column and the filters "Has flags" and "2D".

A lesion file with fractional values (a probabilistic mask such as `desc-lesionProb`) opens with every voxel above 0 as the
lesion; the file itself is not changed. In a study, the Mask hygiene chip then offers **Binarize at 0.5**.

## Review, blinding and agreement

Papers say that lesions "were checked by an expert" and rarely show it. The module records the check.

- **Review states.** After **Done** a lesion is *pending*. In **Library > Review > Review N →** the lesions waiting for a
  decision open one by one in Draw (the mask as an outline, on the figure or scan it was traced on, with no network map
  shown), and a footer replaces "Done → next": **Approve → next** (`Ctrl+Enter`), **Request changes…** (a comment is
  needed) or **Reject…** (a reason is needed; the lesion is excluded). A reviewer may edit the mask; the tracer's file is
  kept. Tracing a lesion again reopens a review (an approval is for the voxels that were reviewed), and a rejected lesion
  stays excluded until a reviewer approves it. Every transition is one line of `.studio/audit.jsonl` with the person, the
  time and the comment. (The keys **P**, **X** and **U** of the subject list set the *QC* column, pass, fail and not
  reviewed; they are not review decisions.)
- **Gate.** With **Require review before analyses** in the Review card, only approved lesions enter the analyses. An
  automatic segmentation (a tool name in `trace_method`) always needs a human approval, whatever the setting. The
  preflight and the methods text say what was excluded and any override.
- **Assisted tracing** (Smart fill) is recorded as the share of voxels that the tool added and never approves anything.
- **Blind tracing.** **Blind tracing: hide group, scores and symptoms** hides those columns from tracers and reviewers,
  and network maps are never shown while tracing or reviewing. Changing a setting is itself audited.
- **Second tracing and agreement.** **Second tracing (blind)** assigns a seeded random fraction of the lesions (or chosen
  ones) to another rater (or to the same rater again after a delay, for intra-rater agreement); the second rater never
  sees the first tracing. **Compute tracing agreement** measures the **masks**, never the network maps: Dice, centroid
  distance, volume ratio and HD95, against the distance between *different* subjects' lesions as the baseline, written to
  `derivatives/qc/interrater.tsv` (rater labels only, no paths) and `.json`. Two tracings give network maps that correlate
  highly even when the masks barely overlap, so network similarity overstates tracing agreement.
- **Adjudicate tracings** resolves a disagreement: keep A, keep B, the union, the intersection or an edit. The consensus
  becomes the lesion, the first tracing is kept as `desc-preadjudication`, and each decision can be undone.

## Normalize native-space lesions to MNI

**Why.** Network mapping needs the lesion in the connectome's space. A lesion drawn on a patient's own scan is carried to
MNI space by registering the scan to a template.

**Method.** The scan is registered to the template with the lesion excluded from the similarity measure ("cost-function
masking", Brett et al., 2001; Andersen et al., 2010; the lesion is dilated by 3 mm for the mask), in an affine stage
followed by a B-spline stage (BRAINSFit, which ships with Slicer; the affine stage tries more starting positions when a
first attempt does not fit). Without masking, the registration tends to shrink the lesion or pull tissue into it. The
lesion is warped with linear interpolation onto the 1 mm grid of the template family, saved as a fraction
(`desc-lesionProb`) and as a binary mask at 0.5 (`desc-lesion`). The scan is warped as well, for review. For a FLAIR,
T2-weighted, DWI or CT scan that has a T1-weighted scan of the same person, the T1-weighted scan is used as an
intermediate step (`--anat`); without one the registration is labelled experimental and always asks for review.
**Quality** is Standard (default), Fine (a finer B-spline grid) or Affine (no B-spline). ANTs (SyN) is not available in
this build.

**Outputs** are written to `derivatives/normalization/<subject>/` of the study:

| File | Content |
| ---- | ------- |
| `<sub>_space-<tpl>_res-1_desc-lesion_mask.nii.gz` (+ `.json`) | the binary lesion in template space |
| `<sub>_space-<tpl>_res-1_desc-lesionProb_mask.nii.gz` | the fractional lesion |
| `<sub>_space-<tpl>_res-1_desc-preproc_<modality>.nii.gz` | the warped scan |
| `<sub>_from-<modality>_to-<tpl>_mode-image_xfm.h5` | the transform |
| `<sub>_desc-normqc.png`, `<sub>_desc-normqc.json` | the QC image and metrics |
| `normalization.json` | the settings and software versions |

The study row is pointed at the normalized lesion, so `lesions/` keeps the lesions as you drew them. **Library > Study > Normalization >
Normalize N native subjects** runs the whole study one subject after the other (tutorial cohorts with native masks use
this path).

**Normalization QC.** The result is a **suggestion** ("review" or "looks ok"), never a verdict. The QC mosaic shows axial,
coronal and sagittal slices through the lesion (the patient's left on the left of the image, with L/R stamps). The
metrics are the similarity inside the brain outside the lesion; the lesion's volume before and after, relative to the
change in brain volume (so that a small head is not flagged because the template is larger); the local volume change
(log Jacobian) in the lesion against the brain; the share of folded voxels; the share of the warped lesion outside the
brain and in the ventricles; and the overlap of the warped and template brain masks. The thresholds were calibrated on
one synthetic data set (the MRHead scan with simulated chronic stroke lesions), are not validated on patient data, and
two of them (the brain-mask overlap and the ventricle share) have not been calibrated at all. Always look at the mosaic.
With review required in the study, a normalized lesion is used only after a reviewer passes it (`norm_qc`).

**Limits.** The registration works best for T1-weighted scans and lesions that are small compared with the hemisphere.
Large lesions, strong atrophy, a very different field of view and scans without a T1-weighted partner are less reliable,
and the suggestion says so.

**Command line** (a job; quote paths with spaces):

```
PythonSlicer -m LesionNetworkMappingLib.normalize check
PythonSlicer -m LesionNetworkMappingLib.normalize run --scan "scan T1.nii.gz" --lesion "lesion.nii.gz" --out "out folder"
PythonSlicer -m LesionNetworkMappingLib.normalize batch --study "my study" --all-native
PythonSlicer -m LesionNetworkMappingLib.normalize warp --run "out folder" --image "other image.nii.gz" --kind image
```

`check` tells which templates and tools are available, `batch` normalizes every included native-space subject, and `warp`
applies a finished run's transform to another image.

## Literature sources and PRISMA

The **Sources** card (Library > Study) keeps the records that a study of published case reports screened, so that the
counts of the flow diagram and the paper behind each case are never retyped.

- **Import** a file (PubMed `.nbib`/MEDLINE, RIS, BibTeX, or a CSV with DOI or PMID columns) or paste identifiers; nothing
  touches the network. Each import records the database, the query and the date in the search log (`study.json`).
- **Duplicates.** An identical DOI or PMID merges into the record already there (empty fields are filled). A title and
  year that match closely are only **offered** for merging (Merge or Keep both).
- **Screening.** Select a record and press **P** to pass it to the next stage (identified, screened, full text, included),
  **X** to exclude it with a reason, **U** to undo. **Fetch metadata** (Crossref or NCBI, sending only the identifier) runs
  only when you click it, and offline it changes nothing.
- **Add cases…** creates the rows `sub-<record><nn>` of a record, with its DOI, PMID and citation.
- **Export PRISMA flow diagram** writes `derivatives/prisma/prisma_counts.json` and `prisma_flow.svg` (a fixed template:
  no scripts, no paths). Records hold citation fields only; the attachment folder `sources/<record>/` (figures, PDFs) is
  never exported. There is no dual-screening reconciliation and no risk-of-bias assessment.

## Share export

**Export study for sharing…** writes a copy of a study that leaves out by construction what should not be shared: it
chooses each file by name and never walks the study folder.

- **Never exported:** the figures and PDFs of `sources/`, scans, native-space data, lesions linked from control sets, the
  private `.studio/` folder, the private autosave folder, and absolute local paths. NIfTI headers are scrubbed (`descrip`,
  `aux_file`, `db_name`, header extensions); voxels and geometry are unchanged.
- **Chosen by you:** the columns of `participants.tsv` (free text, tracer names and dates are off by default); the lesion
  masks (template space only; optional fMRIPrep and lacuna aliases); unthresholded group maps; a NeuroVault-ready folder
  (nothing is uploaded); a zip.
- **Pseudonyms.** Ids can be replaced by `sub-P001`, … in a random order; the key stays in the study's `.studio/share/` and
  is never in the export.
- **Licence is always an explicit choice.** CC0 is suggested for your own masks. GSP-derived maps carry
  `terms: GSP-DUT-2014`; the export needs your acknowledgement of the data use terms and the app never claims that the
  licensing is cleared. Lesions of your own patients need an explicit decision.
- **Checklist.** The dialog lists what is ok, what warns and what blocks (ids without names or dates, sensitive columns,
  scrubbed headers, no absolute paths in JSON or TSV, licence chosen); the export refuses while a blocker is open and
  verifies the written files again before it moves them into place.

The result is a BIDS-derivatives dataset (`dataset_description.json`, `participants.tsv/.json`, `sub-<id>/anat/…`,
`group/…`, `sources/records.tsv`, a README, `CITATION.cff` and a `LICENSE`) that passes a local validator.

## Command lines of this page

The normalization, pipeline and report jobs have command-line twins that run without the application (see
[Outputs](lesionnetworkmapping.md#outputs) and [the headless twin](lnm_data.md#the-headless-twin)):

```
PythonSlicer -m LesionNetworkMappingLib.normalize batch --study STUDY --all-native
PythonSlicer -m LesionNetworkMappingLib.pipeline run --study STUDY --bundle NAME.lnmc --accept-terms
PythonSlicer -m LesionNetworkMappingLib.pipeline report --out OUTPUT_FOLDER
```
