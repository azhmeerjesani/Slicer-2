# Lesion Network Mapping: data

This page describes where the data of the [Lesion Network Mapping](lesionnetworkmapping.md) module comes from, what is
downloaded and when, and how the module protects you and the people whose data you use. It also lists the licence of
each resource that the module can fetch.

## The connectome policy

The policy has two rules. Both are enforced by the code, and the tests check them.

1. **Nothing is bundled and nothing is downloaded by itself.** Connectomes, and every file derived from the GSP1000 data
   (bundles, the fast index, reference maps, degree maps), are never part of Slicer Studio. The only data that ships with
   the application is the 2 mm MNI152NLin2009cAsym brain and mask, and the JSON catalogs and recipes. A network request to
   fetch data is made only when you click for it.
2. **A connectome is downloaded only when you ask for it and have accepted the provider's own terms.** The app shows the
   *live* terms text of the provider (fetched when you click "Load terms"), with its sha256, and the download stays
   disabled until you tick "I accept these data use terms". The acceptance is stored for that exact text
   (`Studio/LNM/AcceptedTermsText/<sha256>`): if the provider changes the text, you are asked again. **A source that does
   not state its terms is only linked to**, never downloaded (a grey badge).

The module has no account, credential or telemetry of its own. The fetching action can be switched off with the fork-only
setting `Studio/LNM/EnableConnectomeFetch` (on by default).

## Getting a connectome

**Map > Connectome > Manage… > Get a connectome…** opens a catalog of the connectomes that can be obtained (a non-modal
dialog; you can keep working). The shipped catalog (`catalog/connectomes.json`) lists, for each entry, its source and
DOI, version, files with sizes and checksums, licence, terms, population, preprocessing, the template space and the
estimated disk space. It is refreshed only when you click **Check catalog updates**.

| Entry | Status | Licence | Notes |
| ----- | ------ | ------- | ----- |
| `gsp1000-ilxiks` | supported | GSP Data Use Terms (amber) | GSP1000 preprocessed NIfTI, v3.0 (doi:10.7910/DVN/ILXIKS): 1000 subjects aged 18 to 36, 292,019 voxels, 1 to 2 runs of 120 frames, global signal regression; about 126 to 140 GB when converted |
| `gsp1000-kktjqc` | supported | GSP Data Use Terms (amber) | GSP1000 preprocessed for Lead-DBS (v4.0, doi:10.7910/DVN/KKTJQC), with the named subsets Full Set (1000), First 100 Subjects, First 5 Subjects and First Subject |
| `ukb-migp-grouppca` | unsupported | CC BY 4.0 (green) | a group-PCA connectome; listed, not usable in this version |
| `leaddbs-ppmi-fgroup2017`, `leaddbs-torpd`, `leaddbs-pediatric` | link only | not stated (grey) | the app only opens the source page |
| `synthetic-demo` | local | CC0 | random numbers with planted networks, made on your computer; always labelled "SYNTHETIC — not real data" |

**Licence badges.** Every resource has a badge: **green** for CC0, CC BY, MIT, Apache and the permissive Melbourne and
McGill texts; **amber** for data use terms, registration, non-commercial or share-alike licences, FSL terms, derived data
and custom terms; **grey** when the terms are not stated. A grey entry is never accepted automatically and is a link only.

**The download.**

- You choose a **destination folder** (the dialog shows the free space and the disk plan, including the estimated final
  size), a **subset** (all archives, a range of archives, or a number of subjects, or one of the named subsets) and
  how many archives are downloaded at once (1 to 4).
- Each archive is **streamed into the converter**: no archive is stored unless you choose "keep archives". The download
  runs as a light, low-priority job with a status chip ("Fetching GSP1000 · 41% · 2 h 10 min"); **Pause** stops it and
  **Resume** continues from where it stopped. If the application was closed during a download, Map asks, a few seconds after it is opened again, whether to resume
  ("Resume" or "Not now"); nothing resumes by itself.
- The computer is kept awake during the download on macOS and Windows.
- The result is `<destination>/<id>.lnmc`, a normal connectome bundle (see
  [The connectome bundle](lesionnetworkmapping.md#data)), registered and selected in Map.
- Command line (a job; the application never passes `--server`, which exists for mirrors and tests):
  `python -m LesionNetworkMappingLib.fetchconnectome --entry ID --dest DIR --terms-sha SHA256 [--subset S] [--streams N]
  [--keep-archives] [--resume]`, with the subcommands `terms` (show the live terms text and its sha256) and `check`
  (compare the live dataset listing with the catalog).

**Integrity.** The MD5 of each archive is checked as the data is read (and across reconnections). A resumed archive
records "not verified (resumed); members verified by gzip CRC". On a mismatch the job stops with a clear message and
offers **Keep this data anyway…**, which records `checksumOverride` in `connectome.json`. A `terms-accepted.txt`
file with the accepted text, and the sha256 of that text, are kept with the bundle.

**Provenance and terms travel with the data.** `connectome.json` records the DOI, version, files with sizes and checksums,
licence, `termsSha256`, the acknowledgement that the terms require, the population (the actual N), the preprocessing and
the TR. Every map computed from a GSP-derived bundle carries `"terms": "GSP-DUT-2014"` and the acknowledgement in its
sidecar, provenance, methods text and exports.

### Subsets

A connectome can be used as a whole or as a **named subject list** (for example the first 100 subjects). The subject list
is part of every map's identity: its hash is in the sidecar, the output folder is named `conn-<label>_n-<N>`, and maps
of different subject lists are never pooled by accident. A 100-subject map is always labelled **Preview**.

## Converting a connectome you already have

**Manage… > Convert** (and `python -m LesionNetworkMappingLib.manager convert`) turns a folder of preprocessed 4D NIfTI
runs on one brain mask, or a Lead-DBS fMRI connectome folder, into a bundle, with the disk space it needs shown before it
starts. The same page lists **Build reference maps**, **Build fast index…** and **Create synthetic demo**. See
[Lesion Network Mapping](lesionnetworkmapping.md#data) for the bundle layout.

## Reference maps

The **Compare** card keeps a library of reference maps in `<LNM data folder>/ReferenceMaps/` (a registry with the licence,
the citation, the sha256 and the space of each map; paths are relative, so the folder can be moved). Maps are added on a
click, from NeuroVault (a public image or collection; the licence is recorded as CC0), from a URL or a file (the licence
you state, default "unknown", grey), or the van den Heuvel degree map from its pinned commit. The last one is derived from
GSP data, so it needs the GSP terms to have been shown and accepted, is never redistributed and is recorded as such.

## Templates, atlases and tutorials

Templates and atlases are fetched **only when you ask**: for the 1 mm template, when a normalization needs a template of
the family of your connectome, or when you pick an atlas for cluster labels. Each download is checked against its hash
(recorded the first time it is downloaded when the source does not publish one), shown with its licence, and cached.
The catalogs that list them (`templates.json`, `atlases.json`) ship with the application and are never refreshed from the
network by themselves.

| Resource | Licence | Badge | Notes |
| -------- | ------- | ----- | ----- |
| MNI152NLin2009cAsym 2 mm T1w and brain mask | MNI/McGill permissive | green | **Included** with Studio (the notice is in [Slicer Studio](studio.md#notices)) |
| MNI152NLin2009cAsym 1 mm T1w and brain mask; 2 mm tissue probability maps | MNI/McGill permissive | green | On request, from TemplateFlow |
| MNI152NLin6Asym 1 mm and 2 mm T1w and brain mask | **FSL terms** (non-commercial) | amber | On request, after the terms are shown |
| MNI152NLin6Asym tissue probability maps | derived (warped from 2009c) | amber | On request; the warp is recorded |
| Transforms between the two families (about 205 MB for the from-2009c file) | derived | amber | On request; used by normalization |
| Schaefer2018 400 and 1000 parcels, 7 and 17 networks | MIT | green | The default atlas of reports is Schaefer-400 7-network |
| Yeo2011 7 and 17 networks | MIT | green | |
| Tian subcortex, scales I to IV | Melbourne subcortex atlas licence | green | One 18 MB archive serves all scales |
| Harvard–Oxford cortical and subcortical (th25), and the HO ventricle labels | CC BY-SA 4.0 | amber | Also used for the lesion location chips and the CSF share |
| MIAL67 thalamic nuclei | CC BY 4.0 | green | 2009c family only |
| HCP-MMP1 (volumetric projection) | CC BY 4.0 | green | Use with care: the projection is approximate |
| Harvard Ascending Arousal Network (AAN v2) | CC0 | green | Link only: Dryad serves files only to signed-in users, so download it in a browser and use Adopt |
| Buckner2011 cerebellum, MASSP20, AAL3 | terms not stated | grey | On request, with the grey badge and a notice to check the source; AAL3's readme mentions the GNU GPL |
| Brainnetome | custom (registration, non-commercial, share-alike) | amber | Link only: download it in a browser and use Adopt |
| GSP1000 and Lead-DBS connectomes | GSP Data Use Terms, or not stated | amber or grey | See the connectome policy above |

**Tutorial cohorts** (Library > Import > **Tutorial cohorts…**) build a ready Study from a public data set, only after
you click, from CC0 sources:

| Tutorial | Source | Licence | What it teaches |
| -------- | ------ | ------- | --------------- |
| `joutsa2022_addiction` | Harvard Dataverse doi:10.7910/DVN/8BHHRS | CC0 | 129 lesion masks already in MNI space (116 MB) from the addiction study, with no scores or labels: the overlap map and the lesion network map |
| `soop_nihss` | OpenNeuro ds004889 | CC0 | 1455 stroke lesion masks on diffusion images (native space; 4 MB) with the NIH Stroke Scale at admission: normalize to MNI first, then symptom mapping |
| `arc_aphasia` | OpenNeuro ds004884 | CC0 | 228 chronic-stroke masks on native T2-weighted images (5.7 MB) with the Western Aphasia Battery quotient: the tutorial for the volume covariate (aphasia severity is largely a volume effect) |
| `lesymap_groundtruth` | LESYMAP package | Apache-2.0 | link only: lesion masks with simulated scores on the Colin27 grid, where the region behind the score is known; you download the package and choose its folder |

A tutorial study is flagged (`tutorial: true`), so its figures carry the watermark "TUTORIAL DATA". The scans are an
option, and the native cohorts follow the normal path (normalize batch, then review).

The van den Heuvel repository has no licence file: its masks and maps are downloaded only when you start the Lead-DBS
parity check (below), with attribution, and are never redistributed.

## Lead-DBS interoperability and parity

- **Names.** Maps use the Lead-DBS naming, `<lesion>_conn-<tag>x<Subset>_desc-{AvgR,AvgRFz,T}_funcmap.nii.gz`, so that files
  can be exchanged with Lead-DBS (`leaddbs_label`, `map_paths`).
- **Import.** **Manage… > Import Lead-DBS maps…** (or `python -m LesionNetworkMappingLib.leaddbs import --folder DIR
  --study DIR`) copies existing Lead-DBS maps, with the header bytes unchanged, into `derivatives/lnm/conn-<label>/`
  with a sidecar per lesion. The subject list of an imported map is unknown, so imported maps are pooled only with
  imported maps of the same label, never with maps computed here.
- **Parity.** **Manage… > Verify against Lead-DBS…** (or `python -m LesionNetworkMappingLib.leaddbs parity --bundle B
  --out DIR --accept-terms GSP-DUT-2014`) maps the 24 reference lesions of the van den Heuvel (2026) repository on *your*
  bundle and compares them with the published Lead-DBS GSP1000v2 maps: r of T and of AvgR, the partial r given μC_r and
  PC1, the largest |dT|, the voxels whose status at |T| ≥ 7 changes, and the Dice of the top 10 %. Nothing is promised in
  advance: `parity.json` and `methods.md` report the numbers that were measured. A bundle that is not derived from the
  Lead-DBS release is flagged "release differences included". The files are fetched on your click from a pinned commit
  and checked by their git blob id; the GSP terms must have been shown and accepted.

## The headless twin

The same library runs without the application, on a server or a cluster, as a BIDS App:

```
slicer-lnm INPUT_DIR OUTPUT_DIR {participant,group} --connectome PATH --accept-terms \
    [--participant-label L ...] [--rule JSON|FILE] [--recipe NAME] [--design design.json] \
    [--null-relocations K] [--perm N] [--figures PRESET,...] [--threads K]
slicer-lnm <command> [arguments]      a library job, for example: slicer-lnm pipeline run ...
```

(`python -m LesionNetworkMappingLib.app` is the same command; the `slicer-lnm` wheel, the container image and the
launcher inside `Slicer Studio.app` start it.) The input is a BIDS dataset with lesion masks named `*_label-lesion_mask`,
`*_desc-lesion_mask` or `*_label-lesion_roi`, or a Slicer Studio study folder. For a BIDS dataset a study is written under
`OUTPUT_DIR/sourcedata/slicer-lnm-study`, which references the lesion files in place (the input folder is never written)
and opens in the Library. A GUI run and a batch run of the same study, connectome and settings give the same files
within numerical tolerance. The `participant` level runs the stages up to the network maps and the per-lesion export;
the `group` level runs every stage and reuses what the participant level completed.

**Exit codes** of the pipeline and the twin: 0 done, 1 error, 2 usage, 3 stopped, 4 data use terms not accepted
(`--accept-terms` is required: it states that you accepted the terms of the connectome), 5 blocked by the preflight,
6 no eligible lesion, 75 another run holds the output folder.

## Where the files go

- Connectome bundles: where you choose in the dialog. The destination is remembered (`Studio/LNM/FetchDestination`), and
  in the web build the default is `/data/SlicerStudio` (the persistent folder), with a notice for any other place.
- Downloaded templates, atlases and tutorial downloads: the cache folder (`$SLICER_LNM_CACHE`, else the application's
  cache path); reference maps and control sets: the LNM data folder (`Studio/LNM/DataDirectory`).
- Files that are meant to be shared (sidecars, tables, `pipeline.json`, share exports) hold paths relative to their own
  folder; the local paths live in fork-only settings and in `provenance.json` and `reproduce.sh`, by design.
