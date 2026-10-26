# Where Slicer Studio saves your work

Slicer Studio saves your work for you, as you go. There is no Save button to
remember. The first time Slicer Studio starts, it asks one question:

> **Where should Slicer Studio keep your work?**

It proposes a folder called **Slicer Studio** in your Documents folder. Press
**Continue** to accept it, or **Choose folder…** to pick another one. If you
close the window, the proposed folder is used. You can change the folder later
in **Settings › Saving**.

If the folder you choose is synced to a cloud service (iCloud Drive, Dropbox,
OneDrive, Google Drive or Box), Slicer Studio says so. Scans can contain patient
data, so think twice before you keep them in a synced folder.

## What is in the folder

Everything is saved as ordinary files, sorted so that you can find them in
Finder, Explorer or your file manager:

```text
Slicer Studio/
├── About this folder.txt        a short map of this folder
├── Projects/
│   └── sub-012 T1w/             one folder per project
│       ├── sub-012 T1w.mrml     the scene: open it in 3D Slicer
│       ├── Images/              your scans and other images
│       ├── Segmentations/       your drawings (Slicer segmentations)
│       ├── Lesions/             lesion masks saved with "Save lesion" (NIfTI + a .json note)
│       ├── Maps/                network maps and statistic maps
│       ├── Markups/             point lists and other markups
│       ├── Models/  Tables/  Transforms/  Other/
│       ├── Exports/             screenshots, figures, reports, exported tables and nifti/ (NIfTI exports)
│       └── .studio/             Slicer Studio's own notes (thumbnail, lock, where copies came from): safe to ignore
├── Studies/
│   └── Stroke cohort/           lesion network mapping studies
├── Templates/
│   └── ICBM 2009b 0.5 mm/       your own MNI templates (see "Your own MNI templates")
└── LNM/                         normalization outputs, control sets and recipes
```

Templates/ appears when you add your own MNI template. How to add one and use it to draw, map and normalize is
explained in [Your own MNI templates](studio_templates.md).

A folder appears only when something is saved into it, so a new project with
one scan has just `Images/` and its `.mrml` file.

## Projects

A **project** is one piece of work: a scan, the lesion you draw on it, the maps
you compute. In Slicer Studio:

- As soon as you open a scan (or a DICOM series, a figure, or start drawing on
  the MNI template), a project is created. It is named after the scan, for
  example `sub-012 T1w`, or `Untitled project`. A message says
  "Saving to Projects/sub-012 T1w"; its **Rename** button renames it.
- The files you open are **copied** into the project. Your original files are
  never changed or moved. Opening one of them again (Open…, a drop, Recent, or a
  folder's **Load all**) does not load a second copy: its scan or lesion becomes
  current again ("<name> is already open"), also after a restart. Slicer Studio
  remembers where each copy came from in the project's `.studio/` folder, never
  in the scene file.
- Opening a Slicer scene (`.mrml` or `.mrb`) from somewhere else makes a new
  project that starts as a copy: "Opened a copy in Projects/<name>. The original
  is not changed." If some of its files cannot be read (missing or damaged), the
  copy holds what loaded and a warning names what is missing; if nothing loads
  (for example a damaged scene file), no project is made.
- Opening a project from **Library › Projects** continues it where you left it.
- While a lesion network mapping **study** is open, the study saves its own
  files (in `Studies/`): no project is made for it.

The **Library** lists your projects, the most recent first, with a thumbnail,
when you last worked on them and what they hold ("1 scan · 1 lesion"). Click a
project to open it. Right-click it to **Rename**, **Duplicate**, **Show in
Finder/Files** or **Move to Trash**.

## What is saved when

- **Continuously.** After you pause for a few seconds (10 by default), and at
  most every few minutes while you keep working, everything that changed is
  saved. Only what changed is written again. The top bar says **Saved**,
  **Saving…**, or **Not saved · Retry** if something went wrong (for example a
  full disk).
- **At once** when you press **Ctrl+S** (⌘S on macOS), go to **Library**, switch
  projects, start a **New project** (Ctrl+N), close the scene (Ctrl+W), or quit.
- **When you quit**, nothing is asked about unsaved changes: Slicer Studio saves
  first and only asks "All changes are saved in Projects/<name>. Quit?". If
  saving failed, the usual 3D Slicer question appears, and a message says why.
- **After a crash**, the next start says "Slicer Studio closed unexpectedly.
  Your project <name> was saved at <time>." with **Open project**. At most the
  last few seconds of work are lost.

If another program changes a file of a project while Slicer Studio has it open,
that file is never overwritten: Slicer Studio saves its own version next to it,
as `<name> (Slicer Studio).<ext>`, and tells you. This includes the scene file:
if official 3D Slicer saves `<name>.mrml` while the project is open, Slicer
Studio keeps it and saves its own scene as `<name> (Slicer Studio).mrml` from
then on. The next time, the project opens `<name>.mrml`; open the
`(Slicer Studio)` file to continue with Slicer Studio's version.

When you close something in a project (a scan, a point list), its file goes to
`.studio/removed/` in the project, where it stays for 14 days.

The pause and the interval are set in **Settings › Saving**. The same panel
shows the Slicer Studio folder, with **Change…** and **Show**. When you change
the folder, Slicer Studio offers to move your projects and studies there.

## Open a project in official 3D Slicer

Every project is a standard 3D Slicer scene with paths relative to its folder:

1. In 3D Slicer 5.12 or later, choose **File › Add Data** (or drag the file onto
   the window).
2. Pick `Projects/<name>/<name>.mrml`.

Everything opens: scans, segmentations, maps, markups and tables. You can work
on it and save it with 3D Slicer's **Save** as usual; when Slicer Studio opens
the project again it keeps those changes, and moves new files that 3D Slicer put
next to the `.mrml` file into their folders.

## Back up or share a project

- **Back up**: copy the whole `Slicer Studio` folder, or a project folder.
- **Share one project**: copy its folder (the `.studio` folder inside can be left
  out), or use **File › Save a copy as…** in Slicer Studio to make one `.mrb`
  file anywhere. The project itself stays where it is and keeps saving there.
- Scene files hold no absolute paths of your computer, so a copied project opens
  on another computer.
- **Use the images in FSL, SPM, AFNI or Python**: **Export project as NIfTI…**
  (the save status menu of the top bar, the project's card in Library, or
  Search) writes every image, lesion and map of the project as NIfTI files into
  `Exports/nifti/`, with a `README.txt` that lists them. See
  [Export as NIfTI](studio_nifti_export.md).

Scans and lesion masks can contain patient data. Keep the Slicer Studio folder
private, and share projects only as your data protection rules allow.

## In the web version

The web version saves in `Slicer Studio` inside the data folder you give the
launcher (`/data/Slicer Studio` in the container), without asking. See
[Install Slicer Studio](install_studio.md#in-a-web-browser).

## Without projects

In the Classic interface Slicer Studio behaves like official 3D Slicer: no
project is created, and you save with **File › Save Data**. A project you opened
in Studio keeps saving when you switch to Classic. Scenes that are not projects
(Classic, studies) are still copied to a private recovery folder, so unsaved
work can be recovered after a crash (**File › Recover Autosaved Work…**).
