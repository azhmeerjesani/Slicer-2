# Your own MNI templates

Slicer Studio comes with one MNI brain: the 2 mm MNI152NLin2009cAsym brain, which
works offline. Many people prefer another one: FSL's MNI152 at 1 mm, an ICBM 2009a
or 2009b template, a 0.5 mm or 7T template, a head with the skull on, or an average
of their own study in MNI space. You can add any of them once and then use it like
the built-in brain: start drawing on it, read the sides and coordinates, map
networks from lesions drawn on it, normalize scans to it and share studies that use
it.

Slicer Studio never downloads or bundles a template for you: you bring the files.
The licence of a template you add is yours to check, for example before you share a
study that includes it.

## What counts as an MNI template

A template is a 3D image (NIfTI `.nii`/`.nii.gz`, or NRRD `.nrrd`) that is already in
MNI space. Slicer Studio checks this before it adds the file, from the file's header
only:

- **A known template grid** (for example the TemplateFlow 2009 grids or FSL's
  MNI152 grids, stored either way round) is accepted, and its space is proposed.
- **Otherwise** the image is accepted when its field of view looks like MNI space:
  the MNI brain (x -70 to 70, y -100 to 70, z -50 to 80 mm) lies inside the image
  and the centre of the image is within 20 mm of (0, -18, 18). When the header does
  not say "MNI", the dialog reminds you that a person's scan should be opened as a
  scan instead.
- **Anything else is refused**: "This image is not in MNI space. Open it as a scan
  instead, or normalize it to MNI first."
- **A NIfTI file whose sform and qform disagree** (by more than 0.1 mm) is refused
  too, whatever they say: Slicer would show it at the qform while the check reads the
  sform, so lesions drawn on it would not be at the MNI positions. Make the two agree
  (for example with `fslorient -copysform2qform`) and add the file again. A template
  of yours whose image disagrees this way is not opened either.

A file with several volumes (4D) is added with its first volume only. Voxels that
are not cubic are accepted with a warning: drawing and maps work best on
near-isotropic templates.

## Add a template

There are three ways to open **Add your own MNI template**:

- **Library › Start on template ▾ › Add your own template…**
- **Settings › Templates › + Add your own…**
- **Search** (Ctrl+K): type *Add MNI template*.

In the dialog:

1. Choose the image, or drop it on the dialog (a brain mask or head image dropped
   with it is used when its file name says so, for example `..._mask.nii.gz`).
   The file card shows what was
   detected: "Looks like MNI space" (or why it is refused), the size, the voxel
   size, the orientation and what the header says.
2. Check the **Name** (it is proposed from the file name), the **Space** (proposed
   from the grid; choose *MNI (other)* when it is not in the list, with your own
   label if you like) and the **Contrast** (T1w, T2w, PD, FLAIR, CT or Other).
3. Optional:
   - **Brain mask**: needed to normalize scans to this template. It must be on the
     grid of the image.
   - **Whole-head image**: helps normalization recover tilted scans. It must be on
     the grid of the image too.
   - **3D brain surfaces** (`.gii`, `.vtk`, `.vtp`, `.obj`, `.stl`, `.ply`): shown in
     the 3D view when you start on the template. They must lie in MNI space, whose
     coordinates are RAS: a file that does not state its coordinate system (most
     FreeSurfer, BrainNet or nibabel exports) is read as RAS, and a `SPACE=RAS` or
     `SPACE=LPS` header, as in every file Slicer saves, is followed. GIfTI files
     need a reader, for example the SlicerFreeSurfer extension.
   - **Source and licence**: where the template comes from and how to cite it.
4. Turn on **Use as my default template** if *Start on template* should open it.
5. Press **Add template**. The files are copied into your Slicer Studio folder with
   a progress bar; your original files are never changed.

## Where the files live

Templates are stored in your Slicer Studio folder, one folder per template:

```text
Slicer Studio/
└── Templates/
    └── ICBM 2009b 0.5 mm/
        ├── template.json          the name, space, resolution, contrast, notes and checksums
        ├── mni_icbm152_t1_tal_nlin_asym_09b_hires.nii   a copy of your image
        ├── <mask>, <head>         copies of the optional files
        └── surfaces/              copies of the optional 3D surfaces
```

`template.json` holds file names relative to its folder and a sha256 checksum of
each file, never a path on your computer. **Settings › Templates** lists every
template with a thumbnail, its space, resolution and contrast, and a *Built-in* or
*Yours* chip. There you can set the default, start on a template, show its folder
or remove it (its folder goes to the Trash). Slicer Studio remembers the default
template (`Studio/Templates/Default`) and the folder the add dialog last opened
(`Studio/Templates/LastDirectory`, on this computer only); neither is written into
shared files.

## Use it

- **Draw**: *Start on template* opens your default template; the ▾ next to it
  lists all of them. The lesion is drawn on the template's own grid. Sides
  ("Left", "Right"), coordinates and laterality cues know the template is in MNI
  space, and the Lesion card says which template you are on. A lesion saved on your
  template has a `.json` note that names the template (id, name, space and
  checksum).
- **Map**: a lesion drawn on your template seeds the network like one on the
  built-in brain. When the template is of another MNI family than the connectome
  (for example ICBM 2009b and FSL's 6Asym), Map shows a note: the families are a
  few millimetres apart, so the map is approximate near subcortical structures.
  *MNI (other)* templates get the same note.
- **Normalize to MNI**: a template with a brain mask can be the target: choose it
  under **Template** in the options of *Normalize to MNI* ("… (yours)"). The
  results are written on the grid of your template, and `normalization.json`
  records the template's id, name, space and checksums. Registration quality
  depends on the template; normalization stays experimental.
- **Studies**: a study made while your template is the default records it in
  `study.json` by id, name, space and checksum, never by path. When you open the
  study on another computer that does not have the template, Slicer Studio says
  "This study uses the template … Add it…"; *Add it…* opens the dialog and accepts
  only the file with the same checksum. Until then the study opens on the built-in
  template of the same family; its lesions stay in the study's MNI space (sides,
  Map), and the Lesion card notes that the lesion grid and the template differ by a
  few millimetres. A project reopened without the template, or a template removed
  while it is open, keeps its lesion in the template's space in the same way.
  *Normalize all* in the Library (and `normalize
  batch --template auto`) normalizes the study's lesions to the study's template
  when it is on this computer with a brain mask; otherwise it uses the built-in
  2009c brain and says so. With a study open, *Start on template* (or choosing the
  default or the study's template in its ▾ menu) adds a case to the study; another
  template starts a case outside it.
- **Sharing a study**: *Export study for sharing…* names your template (id, name,
  space and image checksum) in the export's `dataset_description.json`
  (`SlicerStudio.template`) and README, so the recipient can tell which template
  the masks are on and add the same file. The template file itself is included only
  when you tick **Include the template file** (off by default). It is then copied
  unchanged into `template/` so that its checksum still matches, and checking its
  licence is up to you.

## Projects and official 3D Slicer

A project drawn on your template saves a copy of the template image in its
`Images/` folder, like any other image. The project's `.mrml` opens in official 3D
Slicer 5.12 and 5.13 with the template and the lesion aligned; official 3D Slicer
simply ignores Slicer Studio's notes on the nodes (`Studio.Space`, `Studio.Role`,
`Studio.TemplateId`).

## On the command line

The headless normalization accepts your templates too:

```bash
python -m LesionNetworkMappingLib.normalize run --scan scan.nii.gz --lesion lesion.nii.gz --out out \
    --template user:icbm-2009b-0-5-mm --template-dir "$HOME/Documents/Slicer Studio/Templates"
python -m LesionNetworkMappingLib.normalize check --template user:icbm-2009b-0-5-mm \
    --template-dir "$HOME/Documents/Slicer Studio/Templates"
```

The id after `user:` is the `id` of the template's `template.json`. Lesions drawn on
your template can be mapped and analysed headlessly as they are: their files carry
their own grid. For `vlsm`, pass the template image as the grid
(`--grid "<Templates>/<name>/<image>" --space <space>`).
