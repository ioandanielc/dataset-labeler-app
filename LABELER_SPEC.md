# Melt Pool Frame Labeler — Specification

## Overview

Semi-automated labeling tool for SPH melt pool simulation frames. The user labels side-view screenshots of laser powder bed fusion simulations by melt pool behavior phase. The core output is a mapping of `(P, VX, LS, ST, timestep) → label`.

---

## Data Structure

### Filesystem Layout

```
{root}/
├── P-74p5..._VX-0p201..._LS-4p5e-05_ST-300_M-TI64_XI-..._XF-..._XL-..._TE-..._DT-..._H-fb250.../
│   └── png_files/
│       ├── ss_512_side.png
│       ├── ss_1024_side.png
│       ├── ss_1536_side.png
│       └── ...
├── {another folder matching regex}/
│   └── ...
└── ...
```

Default root: `/Users/ioandanielcraciun/Python-Projects/cVAE-sph2img/data/raw/`

Only `png_files/ss_{ts}_side.png` files are used. Other views (front, top) are ignored.

### Folder Name Regex

```python
DEFAULT_FOLDER_REGEX = (
    r"^P-(?P<P>[^_]+)"
    r"_VX-(?P<VX>[^_]+)"
    r"_LS-(?P<LS>[^_]+)"
    r"_ST-(?P<ST>[^_]+)"
    r"_M-(?P<M>[^_]+)"
    r"_XI-(?P<XI>[^_]+)"
    r"_XF-(?P<XF>[^_]+)"
    r"_XL-(?P<XL>[^_]+)"
    r"_TE-(?P<TE>[^_]+)"
    r"_DT-(?P<DT>[^_]+)"
    r"_H-(?P<H>[^_]+).*$"
)
```

The four primary physics parameters: **P** (Laser Power), **VX** (X Velocity), **LS** (Laser Spot Size), **ST** (Substrate Temperature).

### Image Path Pattern

```
{folder}/png_files/ss_{timestep}_side.png
```

Timestep is an integer. Sort ascending for temporal order.

---

## Label Classes

| # | Label              | Shortcut | Notes                              |
|---|--------------------|---------|------------------------------------|
| 1 | Unlabeled          | `1`     | Default. Counts as NOT labeled.    |
| 2 | Unsure             | `2`     | Conscious decision. Counts as labeled. |
| 3 | Screenshot Bug     | `3`     | Anomaly / rendering artifact.      |
| 4 | Initial Emptiness  | `4`     | Before melt pool forms.            |
| 5 | Forming Phase      | `5`     | Melt pool beginning to form.       |
| 6 | Convection         | `6`     | Stable convective melt pool.       |
| 7 | Keyhole            | `7`     | Keyhole regime.                    |

Rough expected temporal order: Initial Emptiness → Forming Phase → Convection → Keyhole, but this is not enforced. Screenshot Bug and Unsure can appear anywhere.

---

## Database Schema (SQLite)

```sql
CREATE TABLE experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    P TEXT NOT NULL,
    VX TEXT NOT NULL,
    LS TEXT NOT NULL,
    ST TEXT NOT NULL,
    folder TEXT NOT NULL,
    bug_free BOOLEAN DEFAULT NULL,
    correctly_finished BOOLEAN DEFAULT NULL,
    UNIQUE (P, VX, LS, ST)
);

CREATE TABLE frames (
    experiment_id INTEGER NOT NULL REFERENCES experiments(id),
    timestep INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT 'Unlabeled',
    missing BOOLEAN NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (experiment_id, timestep)
);
```

An **experiment** is identified by `(P, VX, LS, ST)`. The folder path is stored for file resolution only. Frames are children of experiments.

---

## Crawl Behavior

1. **First launch**: full scan of `{root}`, parse all matching folders, index all `png_files/ss_{ts}_side.png` files into SQLite.
2. **Every reopen**: diff scan — insert new files as Unlabeled, mark previously indexed files no longer on disk as `missing=1`, clear `missing` flag for files that reappear. Never delete rows.
3. **Manual re-scan**: button/menu entry to trigger a diff scan on demand.

---

## Architecture

### Four Layers

```
┌─────────────────────────────────┐
│  App Layer (Flask + HTML/JS)    │
│  Dashboard / Label / Review     │
├─────────────────────────────────┤
│  Suggester Layer (pluggable)    │
│  BaseSuggester → DummySuggester │
│                → future: ...    │
├─────────────────────────────────┤
│  Crawl Layer (Scanner)          │
│  Filesystem → parsed tuples    │
├─────────────────────────────────┤
│  Data Layer (LabelDB)           │
│  SQLite + undo stack            │
└─────────────────────────────────┘
```

#### Data Layer — `LabelDB`
- Thin Python class wrapping SQLite.
- All reads/writes go through this class exclusively.
- Handles: inserts, updates, undo stack (in-memory list of `(experiment_id, timestep, old_label, new_label)`), progress queries.

#### Crawl Layer — `Scanner`
- Takes root path, walks filesystem.
- Applies folder regex, finds `png_files/ss_{ts}_side.png`.
- Returns list of `(P, VX, LS, ST, folder, timestep)` tuples.
- `LabelDB` diffs against existing data — inserts new as Unlabeled, marks disappeared files as `missing=1`, clears `missing` on reappearance. Never deletes rows.

#### Suggester Layer — pluggable label suggestions
- Abstract base class `BaseSuggester` with method `suggest(experiment_id, timestep, context) -> str | None`.
- `DummySuggester` is the default — always returns `None`.
- Future suggesters slot in by subclassing (e.g. ML-based, rule-based, etc.).
- Selectable at launch via CLI flag (e.g. `--suggester dummy`).
- Toggle on/off from the UI menu at runtime (for performance).
- When active, suggestion shown in Label Mode as a subtle chip (e.g. "Suggestion: Convection"). User can accept with a shortcut key or ignore.
- When off or when suggester returns `None`, nothing shown.

#### App Layer — Flask + frontend
- Lightweight web server serving a single-page HTML/JS frontend.
- REST API for communication:
  - `GET /api/experiments` — list with progress stats
  - `GET /api/experiments/{id}/frames` — all frames for an experiment
  - `PATCH /api/frames/{experiment_id}/{timestep}` — update label
  - `PATCH /api/experiments/{id}` — update booleans
  - `POST /api/scan` — trigger re-scan
  - `POST /api/undo` — undo last label change
- Serves images from the filesystem (static file route pointing at root).

---

## UI Specification

### Dashboard View

- Lists all experiments showing: P, VX, LS, ST (human-readable), Bug Free status, Correctly Finished status.
- Per experiment: progress bar / percentage of frames labeled (non-Unlabeled).
- Global stats: `X/Y experiments fully complete` with percentage.
- "Fully complete" = zero Unlabeled frames + both booleans set.
- Entry points: click to enter Label Mode or Review Mode per experiment.
- Re-scan button.

### Label Mode

- **Main area**: current frame image, displayed large.
- **Keyboard shortcuts**: number keys 1–7 assign labels (legend always visible on screen).
- **Navigation**:
  - Arrow keys (left/right) for prev/next frame — does NOT change the label (skip).
  - Navigation is NOT locked to consecutive order. User can jump freely via:
    - **Timeline bar** at bottom — clickable, shows position in sequence.
    - **Timestep list panel** — scrollable/slideable sidebar showing all timesteps with their current label.
- **Auto-save**: every label assignment writes to SQLite immediately.
- **Undo**: Ctrl+Z pops from undo stack, reverts last label change.
- **Per-experiment progress**: visible while labeling (percentage + breakdown by class).
- **Experiment booleans**: Bug Free and Correctly Finished toggleable from within label mode.

### Review Mode

- **Separate menu entry** — distinct from Label Mode.
- **Read-only**: no label changes possible.
- **Same frame viewer** and navigation as Label Mode.
- **Colored timeline bar**: each segment colored by label class.
- **Browse freely**.

---

## Progress Stats (Always Visible)

### Global (dashboard / top bar)
- % of experiments fully labeled
- Count: e.g. `12/16 experiments complete`

### Per experiment (dashboard list)
- % of frames labeled (non-Unlabeled)
- Progress bar or fraction

### Inside experiment (Label or Review)
- Per-experiment % labeled
- Class breakdown: e.g. `14 Forming, 30 Convection, 12 Keyhole, 3 Unsure, 8 Unlabeled`

---

## Build Order

1. **`LabelDB`** — schema creation, CRUD operations, undo stack, progress queries
2. **`Scanner`** — crawl filesystem, parse regex, parse timesteps, return tuples
3. **`Suggester`** — base class + DummySuggester
4. **Flask API** — wire LabelDB + Scanner + Suggester to REST endpoints
5. **Frontend — Dashboard** — experiment list, stats, navigation
6. **Frontend — Label View** — frame viewer, shortcuts, timeline, suggestion display, auto-save
7. **Frontend — Review View** — read-only, colored timeline

---

## Launch

```bash
python labeler.py /path/to/root
# Opens browser at http://localhost:PORT
# SQLite db stored at {root}/labeler.db (or configurable)
```
