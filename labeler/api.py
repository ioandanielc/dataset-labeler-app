"""
api.py — Flask application
==========================
Wires together LabelDB, Scanner, and Suggester into a REST API served by
Flask.  The frontend communicates exclusively through these endpoints.

Endpoints
---------
GET  /api/experiments                         — list all with progress stats
GET  /api/experiments/<id>/frames             — all frames for one experiment
PATCH /api/experiments/<id>                   — update bug_free / correctly_finished
GET  /api/frames/<experiment_id>/<timestep>   — single frame (with suggestion)
PATCH /api/frames/<experiment_id>/<timestep>  — set label
PATCH /api/frames/<experiment_id>/bulk        — set many labels in one request
POST /api/experiments/<id>/fill_from          — label a timestep and all later ones
POST /api/experiments/<id>/start_single       — switch to one-pass labeling
POST /api/undo                                — undo last label change
POST /api/scan                                — trigger diff re-scan
GET  /api/config                              — data root + labeling mode
POST /api/settings/labeling_mode              — switch single-pass / two-pass
GET  /api/progress                            — global progress summary
GET  /api/suggester                           — suggester state
POST /api/suggester/toggle                    — toggle suggester on/off
GET  /api/export/csv                          — download labelled data as CSV

GET  /images/<int:experiment_id>/<int:timestep>  — serve frame image file
GET  /                                            — serve frontend SPA
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from collections import deque
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from labeler.db import LABELS, SHORTCUT_TO_LABEL, LabelDB
from labeler.scanner import Scanner
from labeler.suggester import BaseSuggester, get_suggester


# ---------------------------------------------------------------------------
# Composite helpers (module-level — no Flask dependency)
# ---------------------------------------------------------------------------

def _label_slug(label: str) -> str:
    return label.lower().replace(" ", "-")


def _frames_hash(frames) -> str:
    """MD5 of sorted timesteps — used to detect stale composites."""
    ts = sorted(f["timestep"] for f in frames)
    return hashlib.md5(str(ts).encode()).hexdigest()[:12]


def _load_meta(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_meta(path: Path, hash_: str, count: int) -> None:
    path.write_text(json.dumps({"hash": hash_, "count": count}))


def _compute_composite_image(
    frames, folder: str, output_path: Path, is_cancelled=None
) -> bool:
    """Average all frame images for *frames* and save to *output_path*.

    *is_cancelled* is an optional callable; if it returns True the computation
    is aborted mid-way (returns False without writing any file).

    Returns True on success, False if PIL/numpy are missing, no valid images,
    or the computation was cancelled.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return False

    arrays = []
    for frame in frames:
        if is_cancelled and is_cancelled():
            return False
        img_path = Path(folder) / "png_files" / f"ss_{frame['timestep']}_side.png"
        if img_path.is_file():
            try:
                arrays.append(np.array(Image.open(img_path).convert("RGB"), dtype=np.float32))
            except Exception:
                pass

    if not arrays:
        return False

    h, w = arrays[0].shape[:2]
    resized = []
    for arr in arrays:
        if is_cancelled and is_cancelled():
            return False
        if arr.shape[:2] != (h, w):
            img = Image.fromarray(arr.astype(np.uint8)).resize((w, h), Image.LANCZOS)
            arr = np.array(img, dtype=np.float32)
        resized.append(arr)

    avg = np.mean(resized, axis=0).astype(np.uint8)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(avg).save(str(output_path))
    return True


# Path of the saved-settings config file — one level up from this package file.
_CONFIG_FILE = Path(__file__).parent.parent / "labeler_config.json"

#: Global labeling workflows.  ``two_pass`` is the round 1 → round 2 → voting
#: flow; ``single_pass`` is one final pass per experiment, no voting.
LABELING_MODES = ("two_pass", "single_pass")


def read_config() -> dict:
    """Return the saved settings, or ``{}`` if the file is absent or broken."""
    try:
        data = json.loads(_CONFIG_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(**updates) -> None:
    """Merge *updates* into the config file, leaving the other settings alone."""
    cfg = read_config()
    cfg.update(updates)
    try:
        _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


def create_app(
    root: str | Path | None = None,
    db_path: str | Path | None = None,
    suggester_name: str = "dummy",
    labeling_mode: str | None = None,
) -> Flask:
    """Create and configure the Flask application.

    Args:
        root: Root directory containing simulation folders.  When ``None`` —
            or when the path no longer exists — the app starts in
            *unconfigured* mode; the user must paste a path via the browser UI
            before any data is shown.
        db_path: Path for the SQLite DB file.  Defaults to
            ``{root}/labeler.db``.
        suggester_name: Name of the suggester to use (e.g. ``"dummy"``).
        labeling_mode: ``"two_pass"`` or ``"single_pass"``.  When ``None`` the
            last mode chosen in the UI is restored from the config file.

    Returns:
        A configured :class:`flask.Flask` instance.
    """
    if root is not None:
        root = Path(root).expanduser().resolve()
        # A saved root can disappear between launches — an external drive, a
        # moved or renamed folder.  Starting unconfigured lets the UI ask for a
        # new path; opening the DB under a missing directory would only raise
        # "unable to open database file" before the app is even up.
        if not root.is_dir():
            print(f"Data root not found: {root}")
            root = None

    if root is not None:
        _configured = True
    else:
        # Use a throw-away temp dir so LabelDB can still initialise
        root = Path(tempfile.mkdtemp(prefix="labeler_unconfigured_"))
        _configured = False

    db_path = Path(db_path) if db_path else root / "labeler.db"

    app = Flask(
        __name__,
        static_folder=str(Path(__file__).parent / "static"),
        template_folder=str(Path(__file__).parent / "templates"),
    )

    # ------------------------------------------------------------------
    # Application state (attached to app for testability)
    # ------------------------------------------------------------------
    app.db: LabelDB = LabelDB(db_path)
    app.scanner: Scanner = Scanner(root, app.db)
    app.suggester: BaseSuggester = get_suggester(suggester_name)
    app.suggester_enabled: bool = True
    app.data_root: Path = root
    app.is_configured: bool = _configured

    # Global workflow choice — picked once in the dashboard rather than per
    # experiment, and remembered across restarts.
    _mode = labeling_mode or read_config().get("labeling_mode") or "two_pass"
    app.labeling_mode: str = _mode if _mode in LABELING_MODES else "two_pass"

    # Composite cache: {root}/.labeler_cache/composites/{exp_id}/{label_slug}.{png,json}
    app.composite_dir: Path = root / ".labeler_cache" / "composites"
    app.composite_dir.mkdir(parents=True, exist_ok=True)
    app.composites_computing: set = set()   # (exp_id, label) currently being computed
    app._composite_lock = threading.Lock()

    # Background auto-morph worker
    app.bg_compute_enabled: bool = False
    app._bg_queue: deque = deque()          # (exp_id, label) pending
    app._bg_queue_set: set = set()          # O(1) dedup
    app._bg_cond = threading.Condition()

    def _bg_worker() -> None:
        while True:
            with app._bg_cond:
                while not app._bg_queue:
                    app._bg_cond.wait()
                exp_id, lbl = app._bg_queue.popleft()
                app._bg_queue_set.discard((exp_id, lbl))

            if not app.bg_compute_enabled:
                continue

            key = (exp_id, lbl)
            exp = app.db.get_experiment(exp_id)
            if exp is None:
                continue

            slug = _label_slug(lbl)
            with app._composite_lock:
                if key in app.composites_computing:
                    continue
                app.composites_computing.add(key)

            try:
                frames = [f for f in app.db.get_frames(exp_id)
                          if f["label"] == lbl and not f["missing"]]
                current_hash = _frames_hash(frames)
                png_path  = app.composite_dir / str(exp_id) / f"{slug}.png"
                meta_path = app.composite_dir / str(exp_id) / f"{slug}.json"
                # Skip if already up to date
                if png_path.is_file() and _load_meta(meta_path).get("hash") == current_hash:
                    continue
                success = _compute_composite_image(
                    frames, exp["folder"], png_path,
                    is_cancelled=lambda: not app.bg_compute_enabled,
                )
                if success:
                    _save_meta(meta_path, current_hash, len(frames))
            finally:
                with app._composite_lock:
                    app.composites_computing.discard(key)

    threading.Thread(target=_bg_worker, daemon=True).start()

    # Run initial scan on startup (skip if unconfigured)
    if app.is_configured:
        app.scanner.scan()

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _to_bool(v):
        return bool(v) if v is not None else None

    def _experiment_dict(exp, include_progress: bool = True) -> dict:
        d = {
            "id": exp["id"],
            "P": exp["P"],
            "VX": exp["VX"],
            "LS": exp["LS"],
            "ST": exp["ST"],
            "folder": exp["folder"],
            "bug_free": _to_bool(exp["bug_free"]),
            "correctly_finished": _to_bool(exp["correctly_finished"]),
            "name": exp["name"],
            "hash": exp["hash"],
            "labeling_stage": exp["labeling_stage"],
        }
        if include_progress:
            d["progress"] = app.db.get_experiment_progress(exp["id"])
        return d

    def _frame_dict(frame) -> dict:
        return {
            "experiment_id": frame["experiment_id"],
            "timestep": frame["timestep"],
            "label": frame["label"],
            "missing": bool(frame["missing"]),
            "updated_at": frame["updated_at"],
            "label_1": frame["label_1"],
            "label_2": frame["label_2"],
            "label_final": frame["label_final"],
        }

    # ------------------------------------------------------------------
    # Frontend SPA
    # ------------------------------------------------------------------

    @app.route("/")
    def index():
        return send_from_directory(app.template_folder, "index.html")

    # ------------------------------------------------------------------
    # Configuration (data root path)
    # ------------------------------------------------------------------

    @app.route("/api/config")
    def get_config():
        return jsonify({
            "root": str(app.data_root) if app.is_configured else None,
            "configured": app.is_configured,
            "labeling_mode": app.labeling_mode,
        })

    @app.route("/api/config", methods=["POST"])
    def set_config():
        """Change the data root directory at runtime and re-scan."""
        body = request.get_json(silent=True) or {}
        new_root_str = (body.get("root") or "").strip()
        if not new_root_str:
            return jsonify({"error": "root path is required"}), 400
        new_root = Path(new_root_str).expanduser().resolve()
        if not new_root.is_dir():
            return jsonify({"error": f"Not a valid directory: {new_root}"}), 400

        # Close old DB and reinitialise with the new root
        old_db = app.db
        new_db_path = new_root / "labeler.db"
        app.db      = LabelDB(new_db_path)
        app.scanner = Scanner(new_root, app.db)
        app.data_root = new_root
        app.is_configured = True
        app.composite_dir = new_root / ".labeler_cache" / "composites"
        app.composite_dir.mkdir(parents=True, exist_ok=True)
        try:
            old_db.close()
        except Exception:
            pass

        # Persist so the next `python run.py` remembers the path
        _write_config(root=str(new_root))

        summary = app.scanner.scan()
        return jsonify({"root": str(new_root), "configured": True, "scan": summary})

    # ------------------------------------------------------------------
    # Labeling mode (single-pass / two-pass)
    # ------------------------------------------------------------------

    @app.route("/api/settings/labeling_mode", methods=["POST"])
    def set_labeling_mode():
        """Switch the global workflow and remember it for the next launch."""
        body = request.get_json(silent=True) or {}
        mode = body.get("mode")
        if mode not in LABELING_MODES:
            abort(400, description=f"mode must be one of: {list(LABELING_MODES)}")
        app.labeling_mode = mode
        _write_config(labeling_mode=mode)
        return jsonify({"labeling_mode": mode})

    # ------------------------------------------------------------------
    # Image serving
    # ------------------------------------------------------------------

    @app.route("/images/<int:experiment_id>/<int:timestep>")
    def serve_image(experiment_id: int, timestep: int):
        """Serve the PNG frame file for a given experiment and timestep."""
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        img_path = Path(exp["folder"]) / "png_files" / f"ss_{timestep}_side.png"
        if not img_path.is_file():
            abort(404)
        # Cached client-side so scrubbing back and forth (or holding an arrow
        # key) does not re-request the same PNG over and over.
        return send_file(str(img_path), mimetype="image/png", max_age=3600)

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    @app.route("/api/experiments")
    def list_experiments():
        experiments = app.db.get_all_experiments()
        progress_batch = app.db.get_all_progress_batch()
        empty = {"total": 0, "labeled": 0, "unlabeled": 0, "pct": 0.0, "counts": {}}
        return jsonify([
            {**_experiment_dict(e, include_progress=False),
             "progress": progress_batch.get(e["id"], empty)}
            for e in experiments
        ])

    @app.route("/api/experiments/<int:experiment_id>")
    def get_experiment(experiment_id: int):
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        return jsonify(_experiment_dict(exp))

    @app.route("/api/experiments/<int:experiment_id>", methods=["PATCH"])
    def update_experiment(experiment_id: int):
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        body = request.get_json(silent=True) or {}
        bug_free = body.get("bug_free", _to_bool(exp["bug_free"]))
        correctly_finished = body.get("correctly_finished", _to_bool(exp["correctly_finished"]))
        app.db.update_experiment_booleans(experiment_id, bug_free, correctly_finished)
        updated = app.db.get_experiment(experiment_id)
        return jsonify(_experiment_dict(updated))

    @app.route("/api/experiments/<int:experiment_id>/reset", methods=["POST"])
    def reset_experiment(experiment_id: int):
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        count = app.db.reset_experiment(experiment_id)
        return jsonify({"reset": count})

    @app.route("/api/experiments/<int:experiment_id>/start_single", methods=["POST"])
    def start_single(experiment_id: int):
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        result = app.db.start_single(experiment_id)
        updated = app.db.get_experiment(experiment_id)
        return jsonify({**_experiment_dict(updated, include_progress=False), **result})

    @app.route("/api/experiments/<int:experiment_id>/start_round_2", methods=["POST"])
    def start_round_2(experiment_id: int):
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        result = app.db.start_round_2(experiment_id)
        updated = app.db.get_experiment(experiment_id)
        return jsonify({**_experiment_dict(updated, include_progress=False), **result})

    @app.route("/api/experiments/<int:experiment_id>/switch_to_round_1", methods=["POST"])
    def switch_to_round_1(experiment_id: int):
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        result = app.db.switch_to_round_1(experiment_id)
        updated = app.db.get_experiment(experiment_id)
        return jsonify({**_experiment_dict(updated, include_progress=False), **result})

    @app.route("/api/experiments/<int:experiment_id>/reset/<int:pass_num>", methods=["POST"])
    def reset_experiment_pass(experiment_id: int, pass_num: int):
        if pass_num not in (1, 2):
            abort(400, description="pass_num must be 1 or 2")
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        count = app.db.reset_experiment_pass(experiment_id, pass_num)
        return jsonify({"reset": count, "pass": pass_num})

    @app.route("/api/experiments/<int:experiment_id>/check_agreement", methods=["POST"])
    def check_agreement(experiment_id: int):
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        result = app.db.check_agreement(experiment_id)
        updated = app.db.get_experiment(experiment_id)
        return jsonify({**_experiment_dict(updated, include_progress=False), **result})

    @app.route("/api/experiments/<int:experiment_id>/conflicts")
    def get_conflicts(experiment_id: int):
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        conflicts = app.db.get_conflicts(experiment_id)
        return jsonify([_frame_dict(f) for f in conflicts])

    # ------------------------------------------------------------------
    # Frames
    # ------------------------------------------------------------------

    @app.route("/api/experiments/<int:experiment_id>/frames")
    def list_frames(experiment_id: int):
        if app.db.get_experiment(experiment_id) is None:
            abort(404)
        frames = app.db.get_frames(experiment_id)
        return jsonify([_frame_dict(f) for f in frames])

    @app.route("/api/frames/<int:experiment_id>/<int:timestep>")
    def get_frame(experiment_id: int, timestep: int):
        frames = app.db.get_frames(experiment_id)
        frame = next((f for f in frames if f["timestep"] == timestep), None)
        if frame is None:
            abort(404)
        d = _frame_dict(frame)
        d["suggestion"] = None
        if app.suggester_enabled:
            exp = app.db.get_experiment(experiment_id)
            context = {"P": exp["P"], "VX": exp["VX"], "LS": exp["LS"], "ST": exp["ST"]} if exp else {}
            d["suggestion"] = app.suggester.suggest(experiment_id, timestep, context)
        return jsonify(d)

    def _queue_composites(experiment_id: int, labels) -> None:
        """Queue affected labels for background morph recompute (if enabled)."""
        if not app.bg_compute_enabled:
            return
        with app._bg_cond:
            for lbl in labels:
                if lbl and lbl != "Unlabeled":
                    key = (experiment_id, lbl)
                    if key not in app._bg_queue_set:
                        app._bg_queue.append(key)
                        app._bg_queue_set.add(key)
            app._bg_cond.notify()

    @app.route("/api/frames/<int:experiment_id>/<int:timestep>", methods=["PATCH"])
    def set_frame_label(experiment_id: int, timestep: int):
        body = request.get_json(silent=True) or {}
        label = body.get("label")
        if label not in LABELS:
            abort(400, description=f"Invalid label. Must be one of: {LABELS}")

        result = app.db.set_labels(experiment_id, [(timestep, label)])
        if result is None or not result["matched"]:
            abort(404)

        affected = {label} | {old for _ts, old, _new in result["changed"]}
        _queue_composites(experiment_id, affected)

        frame = app.db.get_frame(experiment_id, timestep)
        return jsonify(_frame_dict(frame))

    @app.route("/api/frames/<int:experiment_id>/bulk", methods=["PATCH"])
    def set_frame_labels_bulk(experiment_id: int):
        """Apply several label changes at once.

        The frontend batches key presses here so that holding a label key down
        costs one request per batch instead of one per frame.  Each frame is
        still its own undo step.

        Body: ``{"changes": [{"timestep": int, "label": str}, ...]}``
        """
        body    = request.get_json(silent=True) or {}
        changes = body.get("changes") or []
        if not isinstance(changes, list):
            abort(400, description="changes must be a list")

        pairs = []
        for c in changes:
            label = (c or {}).get("label")
            if label not in LABELS:
                abort(400, description=f"Invalid label. Must be one of: {LABELS}")
            try:
                pairs.append((int(c["timestep"]), label))
            except (KeyError, TypeError, ValueError):
                abort(400, description="each change needs an integer timestep")

        result = app.db.set_labels(experiment_id, pairs)
        if result is None:
            abort(404)

        affected = {lbl for _ts, lbl in pairs} | {old for _ts, old, _new in result["changed"]}
        _queue_composites(experiment_id, affected)

        frames = {f["timestep"]: f for f in app.db.get_frames(experiment_id)}
        return jsonify({
            "updated": len(result["changed"]),
            "frames":  [_frame_dict(frames[ts]) for ts in result["matched"] if ts in frames],
        })

    @app.route("/api/experiments/<int:experiment_id>/fill_from", methods=["POST"])
    def fill_from_timestep(experiment_id: int):
        """Label *timestep* and every later frame of the experiment with *label*.

        Body: ``{"timestep": int, "label": str}``.  Frames missing on disk are
        skipped.  The whole fill is one undo step.
        """
        exp = app.db.get_experiment(experiment_id)
        if exp is None:
            abort(404)
        body  = request.get_json(silent=True) or {}
        label = body.get("label")
        if label not in LABELS:
            abort(400, description=f"Invalid label. Must be one of: {LABELS}")
        try:
            start = int(body["timestep"])
        except (KeyError, TypeError, ValueError):
            abort(400, description="timestep is required and must be an integer")

        result = app.db.fill_label_from(experiment_id, start, label)
        if result is None:
            abort(404)

        affected = {label} | {old for _ts, old, _new in result["changed"]}
        _queue_composites(experiment_id, affected)

        updated_exp = app.db.get_experiment(experiment_id)
        return jsonify({
            "updated":       len(result["changed"]),
            "matched":       len(result["matched"]),
            "stage_changed": result["stage_changed"],
            "experiment":    _experiment_dict(updated_exp, include_progress=False),
            "frames":        [_frame_dict(f) for f in app.db.get_frames(experiment_id)],
        })

    # ------------------------------------------------------------------
    # Composites
    # ------------------------------------------------------------------

    @app.route("/api/composites/<int:exp_id>/status")
    def composites_status(exp_id: int):
        """Return per-label composite status: ready | computing | stale | none."""
        all_frames = list(app.db.get_frames(exp_id))
        with app._composite_lock:
            computing = set(app.composites_computing)
        statuses = {}
        for lbl in LABELS:
            if lbl == "Unlabeled":
                continue
            key = (exp_id, lbl)
            if key in computing:
                statuses[lbl] = "computing"
                continue
            slug      = _label_slug(lbl)
            png_path  = app.composite_dir / str(exp_id) / f"{slug}.png"
            meta_path = app.composite_dir / str(exp_id) / f"{slug}.json"
            if not png_path.is_file():
                statuses[lbl] = "none"
                continue
            label_frames  = [f for f in all_frames if f["label"] == lbl and not f["missing"]]
            current_hash  = _frames_hash(label_frames)
            stored_hash   = _load_meta(meta_path).get("hash")
            statuses[lbl] = "ready" if stored_hash == current_hash else "stale"
        return jsonify(statuses)

    @app.route("/api/composites/<int:exp_id>/<slug>")
    def get_composite(exp_id: int, slug: str):
        """Serve the composite PNG for a label slug."""
        path = app.composite_dir / str(exp_id) / f"{slug}.png"
        if not path.is_file():
            abort(404)
        return send_file(str(path), mimetype="image/png", max_age=0)

    @app.route("/api/composites/<int:exp_id>/<slug>", methods=["POST"])
    def compute_composite(exp_id: int, slug: str):
        """Trigger async composite computation for one label."""
        lbl = next((l for l in LABELS if _label_slug(l) == slug), None)
        if lbl is None:
            abort(404)
        exp = app.db.get_experiment(exp_id)
        if exp is None:
            abort(404)

        key = (exp_id, lbl)
        with app._composite_lock:
            if key in app.composites_computing:
                return jsonify({"status": "computing"}), 202
            app.composites_computing.add(key)

        def do_compute():
            try:
                frames = [f for f in app.db.get_frames(exp_id)
                          if f["label"] == lbl and not f["missing"]]
                current_hash = _frames_hash(frames)
                png_path  = app.composite_dir / str(exp_id) / f"{slug}.png"
                meta_path = app.composite_dir / str(exp_id) / f"{slug}.json"
                success = _compute_composite_image(frames, exp["folder"], png_path)
                if success:
                    _save_meta(meta_path, current_hash, len(frames))
            finally:
                with app._composite_lock:
                    app.composites_computing.discard(key)

        threading.Thread(target=do_compute, daemon=True).start()
        return jsonify({"status": "computing"}), 202

    # ------------------------------------------------------------------
    # Background auto-morph toggle
    # ------------------------------------------------------------------

    @app.route("/api/settings/bg_compute")
    def get_bg_compute():
        return jsonify({"enabled": app.bg_compute_enabled})

    @app.route("/api/settings/bg_compute", methods=["POST"])
    def toggle_bg_compute():
        app.bg_compute_enabled = not app.bg_compute_enabled
        if app.bg_compute_enabled:
            # Wake worker in case items are already queued
            with app._bg_cond:
                app._bg_cond.notify()
        return jsonify({"enabled": app.bg_compute_enabled})

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    @app.route("/api/undo", methods=["POST"])
    def undo():
        result = app.db.undo()
        if result is None:
            return jsonify({"message": "Nothing to undo"}), 200
        return jsonify(result)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    @app.route("/api/scan", methods=["POST"])
    def rescan():
        summary = app.scanner.scan()
        return jsonify(summary)

    # ------------------------------------------------------------------
    # Progress
    # ------------------------------------------------------------------

    @app.route("/api/progress")
    def global_progress():
        return jsonify(app.db.get_global_progress())

    # ------------------------------------------------------------------
    # Suggester
    # ------------------------------------------------------------------

    @app.route("/api/suggester")
    def suggester_state():
        return jsonify({
            "enabled": app.suggester_enabled,
            "name": type(app.suggester).__name__,
        })

    @app.route("/api/suggester/toggle", methods=["POST"])
    def toggle_suggester():
        app.suggester_enabled = not app.suggester_enabled
        return jsonify({"enabled": app.suggester_enabled})

    # ------------------------------------------------------------------
    # Total reset
    # ------------------------------------------------------------------

    @app.route("/api/reset_all", methods=["POST"])
    def reset_all():
        """Reset every frame in every experiment back to Unlabeled."""
        count = app.db.total_reset()
        return jsonify({"reset": count})

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    @app.route("/api/import", methods=["POST"])
    def import_labels():
        """Import labels from an uploaded CSV file.

        Expects multipart/form-data with:
          - file: CSV file (with header row)
          - pass: "1", "2", "voting" or "single"
        """
        pass_num_str = request.form.get("pass", "1")
        if pass_num_str not in ("1", "2", "voting", "single"):
            abort(400, description="pass must be '1', '2', 'voting' or 'single'")
        # Convert to int for r1/r2, keep the named targets as strings
        pass_num = pass_num_str if pass_num_str in ("voting", "single") else int(pass_num_str)

        uploaded = request.files.get("file")
        if uploaded is None:
            abort(400, description="No file uploaded")

        import csv as _csv
        import io as _io

        text = uploaded.read().decode("utf-8", errors="replace")
        reader = _csv.DictReader(_io.StringIO(text))
        rows = list(reader)
        result = app.db.import_labels(rows, pass_num)
        return jsonify(result)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    @app.route("/api/export/csv")
    def export_csv():
        which = request.args.get("which", "all")
        if which not in ("all", "r1", "r2", "voting"):
            which = "all"
        filename_map = {"all": "labels_all.csv", "r1": "labels_round1.csv",
                        "r2": "labels_round2.csv", "voting": "labels_voting.csv"}
        csv_data = app.db.export_csv(which)
        from flask import Response
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename_map[which]}"},
        )

    # ------------------------------------------------------------------
    # Labels metadata (useful for frontend to know valid labels/shortcuts)
    # ------------------------------------------------------------------

    @app.route("/api/labels")
    def labels_meta():
        return jsonify({
            "labels": LABELS,
            "shortcuts": SHORTCUT_TO_LABEL,
        })

    return app
