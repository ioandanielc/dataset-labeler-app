# Melt Pool Frame Labeler — How to Use

This tool lets you label melt pool simulation frames as part of a two-pass
annotation workflow (Round 1 and Round 2), then resolve any disagreements.

---

## 1. Setup

### Requirements
- Python 3.10 or newer
- The `pip` package manager

### Install dependencies (first time only)
Open a terminal, navigate to this folder, and run:

```bash
pip install -r requirements.txt
```

> If you use a virtual environment (`venv`), activate it first.

---

## 2. Start the labeler

```bash
python run.py
```

The first time you run it with no path given, the browser will open to the
dashboard. You will see a **"Set Dataset Path"** banner — paste the full path
to your dataset folder there (see Section 3).

Alternatively, you can pass the path directly on the command line:

```bash
python run.py /path/to/your/dataset
```

The tool remembers the path after you set it once — you can just run
`python run.py` on subsequent launches.

---

## 3. Set / change the dataset path

If you need to set or change the dataset path after startup:

1. Click the **📁 Path** button in the top toolbar of the dashboard.
2. Paste the full path to the root folder containing the simulation
   sub-folders (e.g. `/data/raw` or `C:\Users\you\data\raw`).
3. Click **Apply & Scan** — the tool scans for experiments and
   saves the path for next time.

The path is saved to `labeler_config.json` in the tool's folder.

---

## 4. Dashboard overview

The dashboard lists all experiments (one row per simulation run).

| Column | Meaning |
|---|---|
| Name / # | Folder name and run hash |
| P, VX, LS, ST | Physics parameters |
| Bug Free | Is the simulation free of rendering bugs? |
| Finished | Did the simulation complete correctly? |
| Round 1 | Progress bar for your first labeling pass |
| Round 2 | Progress bar for the second labeling pass |
| Agreement | % match between Round 1 and Round 2 (κ = Cohen's Kappa) |

Action buttons per row:
- **R1** — open the experiment for Round 1 labeling
- **R2** — open for Round 2 (enabled once Round 1 is 100 % complete)
- **Voting** — start the agreement/conflict-resolution phase (enabled once both rounds are 100 % complete)
- **⊡ Cmp** — open the Comparison view to see averaged morphs for Conduction vs Keyhole
- **↺** — reset all labels for this experiment

---

## 5. Labeling a frame

When you open an experiment, you see:

- **Centre**: the current simulation frame image
- **Right sidebar**: list of all timesteps; coloured dots show each label
- **Bottom**: colour-coded timeline + label legend

### Keyboard shortcuts

| Key | Action |
|---|---|
| `←` / `→` | Previous / next frame |
| `1` | Unlabeled |
| `2` | Unsure |
| `3` | Screenshot Bug |
| `4` | Initial Emptiness |
| `5` | Forming Phase |
| `6` | Conduction |
| `7` | Keyhole |
| `Ctrl+Z` | Undo last label |
| `b` | Toggle Bug Free flag |
| `f` | Toggle Correctly Finished flag |
| `n` | Jump to next experiment |

### Filters (right sidebar top)

- **All** — show every frame
- **Unlabeled** — show only frames not yet labeled
- **Conflicts** — (correction phase only) show frames where R1 ≠ R2

### By-label panel

Click **By Label** in the bottom-right to open the left panel.
Each label group shows thumbnails and an averaged **morph image**.
Click **⊕ Compute Morph** to generate or refresh the averaged image.
Enable **Auto-Morph** to recompute automatically as you label.

---

## 6. Two-pass workflow

### Round 1
Label every frame from scratch. Progress is shown in the **Round 1** bar.
Use the **Reset R1** button in the header to restart if needed.

### Round 2
Only available once Round 1 is 100 % complete.
Click **R2** on the dashboard to start. Label every frame again
*independently* — do not look at your Round 1 labels.

### Check Agreement / Voting
Once both rounds are complete, click **Voting** on the dashboard.

- Frames where both rounds agree are resolved automatically.
- Frames where they differ go into **Correction** mode.
  - Each conflicting frame shows a badge: `R1: <label>  vs  R2: <label>`.
  - Press `1`–`7` to choose the final label.
  - Once all conflicts are resolved, a modal appears offering **Back to Dashboard**.

### Agreement statistics
- **Agreement %** — simple percentage of frames with matching labels.
- **κ (Cohen's Kappa)** — chance-corrected agreement; values above 0.8 are
  considered very good.

---

## 7. Comparison view

Click **⊡ Cmp** for any experiment to open a side-by-side comparison of:

- **Conduction** averaged morph (left)
- **Keyhole** averaged morph (right)

Use this to visually verify that the two classes look distinctly different.
Click **Compute Morph** in each panel if no image is available yet.

---

## 8. Export / Import

### Export
Click **Export CSV** on the dashboard toolbar. Choose:
- **All columns** — label_1, label_2, label_final
- **Round 1 only** — label_1
- **Round 2 only** — label_2
- **Voting (final)** — label_final (resolved labels)

### Import
Click **Import Labels** to bulk-load labels from a CSV file.
The CSV must have columns: `name`, `timestep`, `label`.
`name` = the experiment folder name as shown in the Name column.

---

## 9. Boolean flags (Bug Free / Finished)

For each experiment, use the **Bug Free** and **Finished** buttons in the header
while in the label view, or the keyboard shortcuts `b` / `f`.

- **Bug Free**: Is the simulation free of visual rendering bugs?
- **Finished**: Did the simulation run complete without errors?

These flags affect the global completion count but are **never** reset by
Total Reset — they persist until you change them manually.

---

## 10. Total Reset

**Total Reset** (red button on the dashboard) clears **all labels** in all
experiments and resets all stages back to Round 1.
It **does not** reset the Bug Free or Finished flags.
Type `yes, i want to reset` in the confirmation box to proceed.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Browser opens but shows "No dataset configured" | Click 📁 Path and paste the correct folder path |
| An experiment has 0 frames | Run **Re-scan** — the PNG files may not have been found |
| Morph image is "Stale" | Click Recompute to regenerate it with the current labels |
| R2 button is greyed out | Complete Round 1 first (100 % labeled) |
| Voting button is greyed out | Complete both Round 1 and Round 2 first |
| Labels are lost after restart | Labels are stored in `labeler.db` inside your dataset folder — do not delete that file |
