"""
db.py — LabelDB
===============
The single source of truth for all persistence in the labeler.
All reads and writes to SQLite go through :class:`LabelDB` exclusively.

Role in the system:
    - Written to by ``scanner.py`` — upserts experiments and frames during crawls.
    - Read/written by ``api.py`` — serves data to the frontend and applies label changes.
    - Independent of Flask, Scanner, and Suggester (pure stdlib: ``sqlite3``, ``csv``, ``io``).

Two tables are managed here:
    - ``experiments`` — one row per unique (P, VX, LS, ST) physics parameter set.
    - ``frames`` — one row per PNG image file; child of an experiment.
"""

import csv
import io
import sqlite3
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Label constants
# ---------------------------------------------------------------------------

LABELS = [
    "Unlabeled",         # 1 — default, not counted as labeled
    "Unsure",            # 2 — conscious decision, counts as labeled
    "Screenshot Bug",    # 3 — rendering artifact
    "Initial Emptiness", # 4 — before melt pool forms
    "Forming Phase",     # 5 — melt pool beginning to form
    "Convection",        # 6 — stable convective melt pool
    "Keyhole",           # 7 — keyhole regime
]

#: Maps keyboard shortcut digit → label name, e.g. ``{"1": "Unlabeled", ...}``.
SHORTCUT_TO_LABEL: dict[str, str] = {str(i + 1): label for i, label in enumerate(LABELS)}

# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    P                   TEXT NOT NULL,
    VX                  TEXT NOT NULL,
    LS                  TEXT NOT NULL,
    ST                  TEXT NOT NULL,
    folder              TEXT NOT NULL,
    bug_free            BOOLEAN DEFAULT NULL,
    correctly_finished  BOOLEAN DEFAULT NULL,
    name                TEXT NOT NULL DEFAULT '',
    hash                TEXT NOT NULL DEFAULT '',
    labeling_stage      TEXT NOT NULL DEFAULT 'round_1',
    UNIQUE (P, VX, LS, ST)
);

CREATE TABLE IF NOT EXISTS frames (
    experiment_id   INTEGER NOT NULL REFERENCES experiments(id),
    timestep        INTEGER NOT NULL,
    label           TEXT NOT NULL DEFAULT 'Unlabeled',
    missing         BOOLEAN NOT NULL DEFAULT 0,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    label_1         TEXT DEFAULT NULL,
    label_2         TEXT DEFAULT NULL,
    label_final     TEXT DEFAULT NULL,
    PRIMARY KEY (experiment_id, timestep)
);
"""


# ---------------------------------------------------------------------------
# LabelDB
# ---------------------------------------------------------------------------

class LabelDB:
    """Thin wrapper around a SQLite connection for the labeler database.

    Opens (or creates) the database at *db_path* and runs the schema
    migrations on every instantiation — safe to call repeatedly because all
    ``CREATE`` statements use ``IF NOT EXISTS``.

    Args:
        db_path: Path to the ``.db`` file. Created if it does not exist.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

        # check_same_thread=False is safe here — Flask dev server is single-threaded.
        # WAL mode lets reads and writes coexist without blocking each other.
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row   # row["P"] instead of row[0]
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._migrate()

        # In-memory only — resets on restart. Each entry: (exp_id, timestep, old, new).
        self._undo_stack: list[tuple[int, int, str, str]] = []

    # ------------------------------------------------------------------
    # Migration helpers
    # ------------------------------------------------------------------

    def _migrate(self) -> None:
        """Add any missing columns to existing DBs using PRAGMA table_info + ALTER TABLE."""
        # experiments table migrations
        exp_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(experiments)")}
        if "name" not in exp_cols:
            self._conn.execute("ALTER TABLE experiments ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        if "hash" not in exp_cols:
            self._conn.execute("ALTER TABLE experiments ADD COLUMN hash TEXT NOT NULL DEFAULT ''")
        if "labeling_stage" not in exp_cols:
            self._conn.execute("ALTER TABLE experiments ADD COLUMN labeling_stage TEXT NOT NULL DEFAULT 'round_1'")

        # frames table migrations
        frame_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(frames)")}
        if "label_1" not in frame_cols:
            self._conn.execute("ALTER TABLE frames ADD COLUMN label_1 TEXT DEFAULT NULL")
        if "label_2" not in frame_cols:
            self._conn.execute("ALTER TABLE frames ADD COLUMN label_2 TEXT DEFAULT NULL")
        if "label_final" not in frame_cols:
            self._conn.execute("ALTER TABLE frames ADD COLUMN label_final TEXT DEFAULT NULL")

        self._conn.commit()

    # ------------------------------------------------------------------
    # Experiment methods
    # ------------------------------------------------------------------

    def upsert_experiment(self, P: str, VX: str, LS: str, ST: str, folder: str, name: str = '', hash: str = '') -> int:
        """Insert a new experiment or update its folder path and name if it already exists.

        Uses ``INSERT ... ON CONFLICT DO UPDATE`` so one call handles both cases
        without raising an error on duplicate physics parameters.

        Args:
            P: Laser power string as parsed from the folder name.
            VX: X velocity string.
            LS: Laser spot size string.
            ST: Substrate temperature string.
            folder: Absolute path to the simulation folder (for file resolution).
            name: Full folder name including hash suffix.

        Returns:
            The integer ``id`` of the inserted or existing experiment row.
        """
        cur = self._conn.execute(
            """
            INSERT INTO experiments (P, VX, LS, ST, folder, name, hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(P, VX, LS, ST) DO UPDATE SET
                folder = excluded.folder,
                name   = excluded.name,
                hash   = excluded.hash
            RETURNING id
            """,
            (P, VX, LS, ST, folder, name, hash),
        )
        row = cur.fetchone()
        self._conn.commit()
        return row[0]

    def update_experiment_booleans(
        self,
        experiment_id: int,
        bug_free: Optional[bool],
        correctly_finished: Optional[bool],
    ) -> None:
        """Set the Bug Free and Correctly Finished flags for an experiment.

        Args:
            experiment_id: The experiment to update.
            bug_free: True/False/None (None means not yet assessed).
            correctly_finished: True/False/None.
        """
        self._conn.execute(
            "UPDATE experiments SET bug_free = ?, correctly_finished = ? WHERE id = ?",
            (bug_free, correctly_finished, experiment_id),
        )
        self._conn.commit()

    def get_experiment(self, experiment_id: int) -> Optional[sqlite3.Row]:
        """Fetch one experiment row by its id.

        Args:
            experiment_id: Primary key of the experiment.

        Returns:
            A :class:`sqlite3.Row` (dict-like) or ``None`` if not found.
        """
        cur = self._conn.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,))
        return cur.fetchone()

    def get_all_experiments(self) -> list[sqlite3.Row]:
        """Fetch all experiments ordered by physics parameters (P, VX, LS, ST).

        Returns:
            List of :class:`sqlite3.Row` objects.
        """
        cur = self._conn.execute("SELECT * FROM experiments ORDER BY P, VX, LS, ST")
        return cur.fetchall()

    # ------------------------------------------------------------------
    # Frame methods
    # ------------------------------------------------------------------

    def upsert_frame(self, experiment_id: int, timestep: int, missing: bool = False) -> None:
        """Insert a frame if it does not exist; otherwise update only its missing flag.

        Crucially, this never overwrites the ``label`` column — that would
        silently discard user work during a re-scan.

        Args:
            experiment_id: Parent experiment id.
            timestep: Integer timestep parsed from the filename.
            missing: Whether the file is absent from disk right now.
        """
        self._conn.execute(
            """
            INSERT INTO frames (experiment_id, timestep, missing)
            VALUES (?, ?, ?)
            ON CONFLICT(experiment_id, timestep) DO UPDATE SET missing = excluded.missing
            """,
            (experiment_id, timestep, int(missing)),
        )
        self._conn.commit()

    def set_label(self, experiment_id: int, timestep: int, new_label: str) -> bool:
        """Update a frame's label and push the old value onto the undo stack.

        The column updated depends on the experiment's current labeling_stage:
          - round_1: writes label and label_1
          - round_2: writes label and label_2
          - correction: writes label and label_final; checks for completion
          - done: no-op, returns False

        Args:
            experiment_id: Parent experiment id.
            timestep: Frame timestep.
            new_label: One of the :data:`LABELS` constants.

        Returns:
            ``True`` on success, ``False`` if the frame row does not exist or stage is done.
        """
        # Look up stage
        exp_row = self._conn.execute(
            "SELECT labeling_stage FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        if exp_row is None:
            return False
        stage = exp_row["labeling_stage"]

        cur = self._conn.execute(
            "SELECT label FROM frames WHERE experiment_id = ? AND timestep = ?",
            (experiment_id, timestep),
        )
        row = cur.fetchone()
        if row is None:
            return False

        old_label = row["label"]

        if stage == "done":
            # Voting/done mode: allow re-labelling the final label in place.
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_final = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (new_label, new_label, experiment_id, timestep),
            )
            self._conn.commit()
            self._undo_stack.append((experiment_id, timestep, old_label, new_label))
            return True

        if stage == "round_1":
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_1 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (new_label, new_label, experiment_id, timestep),
            )
        elif stage == "round_2":
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_2 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (new_label, new_label, experiment_id, timestep),
            )
        elif stage == "correction":
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_final = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (new_label, new_label, experiment_id, timestep),
            )
            self._conn.commit()
            # Check if all conflicts are now resolved
            remaining = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM frames
                WHERE experiment_id = ? AND label_1 != label_2 AND label_final IS NULL AND missing = 0
                """,
                (experiment_id,),
            ).fetchone()["cnt"]
            if remaining == 0:
                self._conn.execute(
                    "UPDATE experiments SET labeling_stage = 'done' WHERE id = ?",
                    (experiment_id,),
                )
            self._conn.commit()
            self._undo_stack.append((experiment_id, timestep, old_label, new_label))
            return True

        self._conn.commit()
        self._undo_stack.append((experiment_id, timestep, old_label, new_label))
        return True

    def undo(self) -> Optional[dict]:
        """Revert the last label change by popping the undo stack.

        Also reverts the appropriate stage-specific column (label_1, label_2, or label_final).

        Returns:
            A dict ``{experiment_id, timestep, reverted_to, was}`` describing
            what changed, or ``None`` if the stack is empty.
        """
        if not self._undo_stack:
            return None

        experiment_id, timestep, old_label, new_label = self._undo_stack.pop()

        # Look up current stage to know which extra column to revert
        exp_row = self._conn.execute(
            "SELECT labeling_stage FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        stage = exp_row["labeling_stage"] if exp_row else "round_1"

        if stage == "round_1":
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_1 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (old_label, old_label if old_label != "Unlabeled" else None, experiment_id, timestep),
            )
        elif stage == "round_2":
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_2 = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (old_label, old_label if old_label != "Unlabeled" else None, experiment_id, timestep),
            )
        elif stage in ("correction", "done"):
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, label_final = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (old_label, old_label if old_label != "Unlabeled" else None, experiment_id, timestep),
            )
        else:
            self._conn.execute(
                """
                UPDATE frames
                SET label = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ?
                """,
                (old_label, experiment_id, timestep),
            )

        self._conn.commit()
        return {
            "experiment_id": experiment_id,
            "timestep": timestep,
            "reverted_to": old_label,
            "was": new_label,
        }

    def get_frames(self, experiment_id: int) -> list[sqlite3.Row]:
        """Return all frames for an experiment, sorted by timestep ascending.

        Args:
            experiment_id: Parent experiment id.

        Returns:
            List of :class:`sqlite3.Row` objects.
        """
        cur = self._conn.execute(
            "SELECT * FROM frames WHERE experiment_id = ? ORDER BY timestep",
            (experiment_id,),
        )
        return cur.fetchall()

    def get_existing_timesteps(self, experiment_id: int) -> set[int]:
        """Return the set of timesteps already stored in the DB for an experiment.

        Used by the Scanner during diff scans to determine which files are new
        and which have disappeared.

        Args:
            experiment_id: Parent experiment id.

        Returns:
            Set of integer timesteps.
        """
        cur = self._conn.execute(
            "SELECT timestep FROM frames WHERE experiment_id = ?",
            (experiment_id,),
        )
        return {row[0] for row in cur.fetchall()}

    # ------------------------------------------------------------------
    # Two-pass workflow methods
    # ------------------------------------------------------------------

    def start_round_2(self, experiment_id: int) -> dict:
        """Transition an experiment to round_2 (can be called at any time during round_1).

        Saves any remaining round_1 labels to label_1 that aren't already stored,
        then restores any existing label_2 progress so round_2 can be continued
        independently.  label_1 is always preserved.

        Args:
            experiment_id: The experiment to advance.

        Returns:
            ``{"stage": "round_2"}``
        """
        # Persist any labeled frames to label_1 that set_label didn't catch
        self._conn.execute(
            """
            UPDATE frames SET label_1 = label
            WHERE experiment_id = ? AND missing = 0 AND label_1 IS NULL AND label != 'Unlabeled'
            """,
            (experiment_id,),
        )
        # Restore label_2 progress (or Unlabeled if none yet)
        self._conn.execute(
            """
            UPDATE frames SET label = COALESCE(label_2, 'Unlabeled'), updated_at = CURRENT_TIMESTAMP
            WHERE experiment_id = ? AND missing = 0
            """,
            (experiment_id,),
        )
        self._conn.execute(
            "UPDATE experiments SET labeling_stage = 'round_2' WHERE id = ?",
            (experiment_id,),
        )
        self._conn.commit()
        self._undo_stack.clear()
        return {"stage": "round_2"}

    def switch_to_round_1(self, experiment_id: int) -> dict:
        """Switch back from round_2 (or correction) to round_1 for further editing.

        Restores label_1 values to the ``label`` column so the UI shows
        round_1 progress.  Any round_2 work already written to label_2 is
        preserved.

        Args:
            experiment_id: The experiment to switch.

        Returns:
            ``{"stage": "round_1"}``
        """
        self._conn.execute(
            """
            UPDATE frames SET label = COALESCE(label_1, 'Unlabeled'), updated_at = CURRENT_TIMESTAMP
            WHERE experiment_id = ? AND missing = 0
            """,
            (experiment_id,),
        )
        self._conn.execute(
            "UPDATE experiments SET labeling_stage = 'round_1' WHERE id = ?",
            (experiment_id,),
        )
        self._conn.commit()
        self._undo_stack.clear()
        return {"stage": "round_1"}

    def check_agreement(self, experiment_id: int) -> dict:
        """Compare round_1 and round_2 labels and advance the stage.

        - Auto-sets label_final = label_1 where they agree.
        - Counts remaining conflicts (label_1 != label_2).
        - If no conflicts: stage → 'done', label = resolved value.
        - If conflicts: stage → 'correction', label = label_1 for display.

        Args:
            experiment_id: The experiment to check.

        Returns:
            ``{"conflict_count": n, "stage": "done"|"correction"}``
        """
        # Fill in label_1 for frames that were never explicitly labeled in round_1
        # (e.g. user started round_2 without finishing round_1, or used lbNextExperiment).
        # Using the current label column as the best available round_1 proxy prevents
        # NULL label_1 values from corrupting the COALESCE(label_final, label_1) result.
        self._conn.execute(
            """
            UPDATE frames
            SET label_1 = label
            WHERE experiment_id = ? AND label_1 IS NULL AND missing = 0
            """,
            (experiment_id,),
        )

        # First: copy label→label_2 for any frames not yet written in round_2
        self._conn.execute(
            """
            UPDATE frames
            SET label_2 = label
            WHERE experiment_id = ? AND label_2 IS NULL AND missing = 0
            """,
            (experiment_id,),
        )

        # Auto-set label_final where labels agree
        self._conn.execute(
            """
            UPDATE frames
            SET label_final = label_1
            WHERE experiment_id = ? AND label_1 = label_2 AND label_final IS NULL AND missing = 0
            """,
            (experiment_id,),
        )

        # Count remaining conflicts
        conflict_count = self._conn.execute(
            """
            SELECT COUNT(*) AS cnt FROM frames
            WHERE experiment_id = ? AND label_1 != label_2 AND label_final IS NULL AND missing = 0
            """,
            (experiment_id,),
        ).fetchone()["cnt"]

        if conflict_count == 0:
            self._conn.execute(
                "UPDATE experiments SET labeling_stage = 'done' WHERE id = ?",
                (experiment_id,),
            )
            # Set label = resolved value for all frames
            self._conn.execute(
                """
                UPDATE frames
                SET label = COALESCE(label_final, label_1)
                WHERE experiment_id = ? AND missing = 0
                """,
                (experiment_id,),
            )
            stage = "done"
        else:
            self._conn.execute(
                "UPDATE experiments SET labeling_stage = 'correction' WHERE id = ?",
                (experiment_id,),
            )
            # Set label = label_1 for display during correction
            self._conn.execute(
                """
                UPDATE frames
                SET label = label_1
                WHERE experiment_id = ? AND missing = 0
                """,
                (experiment_id,),
            )
            stage = "correction"

        self._conn.commit()
        self._undo_stack.clear()
        return {"conflict_count": conflict_count, "stage": stage}

    def get_conflicts(self, experiment_id: int) -> list[sqlite3.Row]:
        """Return frames that have conflicting round_1 / round_2 labels.

        Args:
            experiment_id: The experiment to query.

        Returns:
            List of frame rows ordered by timestep.
        """
        cur = self._conn.execute(
            """
            SELECT * FROM frames
            WHERE experiment_id = ? AND label_1 != label_2 AND label_final IS NULL AND missing = 0
            ORDER BY timestep
            """,
            (experiment_id,),
        )
        return cur.fetchall()

    # ------------------------------------------------------------------
    # Progress queries
    # ------------------------------------------------------------------

    def get_experiment_progress(self, experiment_id: int) -> dict:
        """Return label counts and completion percentage for one experiment.

        In correction mode, progress = conflicts resolved / total conflicts.
        Otherwise uses standard label counting.

        Args:
            experiment_id: The experiment to query.

        Returns:
            A dict with keys ``total``, ``labeled``, ``unlabeled``, ``pct``,
            ``counts``, and ``stage``.
        """
        exp_row = self._conn.execute(
            "SELECT labeling_stage FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        stage = exp_row["labeling_stage"] if exp_row else "round_1"

        if stage == "correction":
            total = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM frames
                WHERE experiment_id = ? AND label_1 != label_2 AND missing = 0
                """,
                (experiment_id,),
            ).fetchone()["cnt"]
            labeled = self._conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM frames
                WHERE experiment_id = ? AND label_final IS NOT NULL AND label_1 != label_2 AND missing = 0
                """,
                (experiment_id,),
            ).fetchone()["cnt"]
            unlabeled = total - labeled
            pct = round(labeled / total * 100, 1) if total > 0 else 0.0
            return {
                "total": total,
                "labeled": labeled,
                "unlabeled": unlabeled,
                "pct": pct,
                "counts": {},
                "stage": stage,
            }

        cur = self._conn.execute(
            """
            SELECT label, COUNT(*) AS count
            FROM frames
            WHERE experiment_id = ? AND missing = 0
            GROUP BY label
            """,
            (experiment_id,),
        )
        counts = {row["label"]: row["count"] for row in cur.fetchall()}
        total = sum(counts.values())
        unlabeled = counts.get("Unlabeled", 0)
        labeled = total - unlabeled
        pct = round(labeled / total * 100, 1) if total > 0 else 0.0
        return {
            "total": total,
            "labeled": labeled,
            "unlabeled": unlabeled,
            "pct": pct,
            "counts": counts,
            "stage": stage,
        }

    def get_all_progress_batch(self) -> dict:
        """Return progress stats for ALL experiments in two queries instead of N+1.

        Returns:
            A dict mapping ``experiment_id`` → progress dict (same shape as
            :meth:`get_experiment_progress`).
        """
        from collections import defaultdict

        # Get stages for all experiments
        stages = {
            row["id"]: row["labeling_stage"]
            for row in self._conn.execute("SELECT id, labeling_stage FROM experiments")
        }

        # Standard label counts
        cur = self._conn.execute(
            """
            SELECT experiment_id, label, COUNT(*) AS cnt
            FROM frames
            WHERE missing = 0
            GROUP BY experiment_id, label
            """
        )
        raw: dict[int, dict[str, int]] = defaultdict(dict)
        for row in cur.fetchall():
            raw[row["experiment_id"]][row["label"]] = row["cnt"]

        # Correction-mode conflict counts
        corr_cur = self._conn.execute(
            """
            SELECT experiment_id,
                   COUNT(*) AS total_conflicts,
                   SUM(CASE WHEN label_final IS NOT NULL THEN 1 ELSE 0 END) AS resolved
            FROM frames
            WHERE missing = 0 AND label_1 != label_2
            GROUP BY experiment_id
            """
        )
        corr_data: dict[int, tuple[int, int]] = {}
        for row in corr_cur.fetchall():
            corr_data[row["experiment_id"]] = (row["total_conflicts"], row["resolved"])

        # Inter-rater agreement: label_1 vs label_2 confusion matrix per experiment
        agree_cur = self._conn.execute(
            """
            SELECT experiment_id, label_1, label_2, COUNT(*) AS cnt
            FROM frames
            WHERE missing = 0 AND label_1 IS NOT NULL AND label_2 IS NOT NULL
            GROUP BY experiment_id, label_1, label_2
            """
        )
        # confusion_data: exp_id → {(l1, l2): count}
        from collections import defaultdict as _dd
        confusion_data: dict[int, dict] = _dd(dict)
        for row in agree_cur.fetchall():
            confusion_data[row["experiment_id"]][(row["label_1"], row["label_2"])] = row["cnt"]

        # Per-pass labeled counts (independent of current stage)
        pass1_cur = self._conn.execute(
            """
            SELECT experiment_id, COUNT(*) AS cnt
            FROM frames
            WHERE missing = 0 AND label_1 IS NOT NULL AND label_1 != 'Unlabeled'
            GROUP BY experiment_id
            """
        )
        pass1_counts: dict[int, int] = {row["experiment_id"]: row["cnt"] for row in pass1_cur.fetchall()}

        pass2_cur = self._conn.execute(
            """
            SELECT experiment_id, COUNT(*) AS cnt
            FROM frames
            WHERE missing = 0 AND label_2 IS NOT NULL AND label_2 != 'Unlabeled'
            GROUP BY experiment_id
            """
        )
        pass2_counts: dict[int, int] = {row["experiment_id"]: row["cnt"] for row in pass2_cur.fetchall()}

        # Total non-missing frames per experiment (for pass pct calculation)
        total_cur = self._conn.execute(
            "SELECT experiment_id, COUNT(*) AS cnt FROM frames WHERE missing = 0 GROUP BY experiment_id"
        )
        total_counts: dict[int, int] = {row["experiment_id"]: row["cnt"] for row in total_cur.fetchall()}

        result = {}
        all_exp_ids = set(stages.keys()) | set(raw.keys())
        for exp_id in all_exp_ids:
            stage = stages.get(exp_id, "round_1")
            frame_total = total_counts.get(exp_id, 0)
            p1 = pass1_counts.get(exp_id, 0)
            p2 = pass2_counts.get(exp_id, 0)
            p1_pct = round(p1 / frame_total * 100, 1) if frame_total > 0 else 0.0
            p2_pct = round(p2 / frame_total * 100, 1) if frame_total > 0 else 0.0

            # Agreement stats
            confusion = confusion_data.get(exp_id, {})
            n_comparable = sum(confusion.values())
            if n_comparable > 0:
                agreed = sum(v for (l1, l2), v in confusion.items() if l1 == l2)
                agree_pct = round(agreed / n_comparable * 100, 1)
                kappa = LabelDB._cohen_kappa(confusion)
            else:
                agree_pct = None
                kappa = None

            base = {
                "pass1_labeled": p1, "pass2_labeled": p2,
                "pass1_pct": p1_pct, "pass2_pct": p2_pct,
                "agree_pct": agree_pct, "kappa": kappa,
            }

            if stage == "correction" and exp_id in corr_data:
                total, labeled = corr_data[exp_id]
                unlabeled = total - labeled
                pct = round(labeled / total * 100, 1) if total > 0 else 0.0
                result[exp_id] = {
                    "total": total, "labeled": labeled, "unlabeled": unlabeled,
                    "pct": pct, "counts": {}, "stage": stage, **base,
                }
            else:
                counts = dict(raw.get(exp_id, {}))
                total = sum(counts.values())
                unlabeled = counts.get("Unlabeled", 0)
                labeled = total - unlabeled
                pct = round(labeled / total * 100, 1) if total > 0 else 0.0
                result[exp_id] = {
                    "total": total, "labeled": labeled, "unlabeled": unlabeled,
                    "pct": pct, "counts": counts, "stage": stage, **base,
                }
        return result

    @staticmethod
    def _cohen_kappa(confusion: dict) -> float:
        """Compute Cohen's Kappa from a confusion dict {(label_1, label_2): count}."""
        n = sum(confusion.values())
        if n == 0:
            return 0.0
        all_labels = set(l for pair in confusion for l in pair)
        po = sum(v for (l1, l2), v in confusion.items() if l1 == l2) / n
        pe = sum(
            (sum(v for (l1, _), v in confusion.items() if l1 == lbl) / n) *
            (sum(v for (_, l2), v in confusion.items() if l2 == lbl) / n)
            for lbl in all_labels
        )
        return round((po - pe) / (1 - pe), 3) if pe < 1.0 else 1.0

    def get_global_progress(self) -> dict:
        """Return how many experiments are fully complete across the whole dataset.

        An experiment is *fully complete* when it has zero Unlabeled frames,
        at least one frame, and both boolean flags are set (not NULL).

        Returns:
            A dict with keys ``total``, ``complete``, and ``pct`` (float 0–100).
        """
        experiments = self.get_all_experiments()
        progress_batch = self.get_all_progress_batch()
        total = len(experiments)
        complete = 0
        for exp in experiments:
            p = progress_batch.get(exp["id"], {"unlabeled": 0, "total": 0})
            if (
                p["unlabeled"] == 0
                and p["total"] > 0
                and exp["bug_free"] is not None
                and exp["correctly_finished"] is not None
            ):
                complete += 1
        pct = round(complete / total * 100, 1) if total > 0 else 0.0
        return {"total": total, "complete": complete, "pct": pct}

    def reset_experiment(self, experiment_id: int) -> int:
        """Set all non-missing frames back to Unlabeled, clearing two-pass columns.

        Also resets the labeling_stage to round_1.
        Clears the undo stack since this bulk operation cannot be meaningfully
        undone frame-by-frame.

        Args:
            experiment_id: The experiment to reset.

        Returns:
            The number of frames that were reset.
        """
        cur = self._conn.execute(
            """
            UPDATE frames
            SET label = 'Unlabeled', label_1 = NULL, label_2 = NULL, label_final = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE experiment_id = ? AND missing = 0
            """,
            (experiment_id,),
        )
        self._conn.execute(
            "UPDATE experiments SET labeling_stage = 'round_1' WHERE id = ?",
            (experiment_id,),
        )
        self._conn.commit()
        self._undo_stack.clear()
        return cur.rowcount

    def total_reset(self) -> int:
        """Reset ALL frames across ALL experiments to Unlabeled.

        Clears label_1, label_2, label_final, resets every experiment's
        labeling_stage back to round_1.  Intended as a full restart.

        Returns:
            Total number of frames reset.
        """
        cur = self._conn.execute(
            """
            UPDATE frames
            SET label = 'Unlabeled', label_1 = NULL, label_2 = NULL, label_final = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE missing = 0
            """
        )
        self._conn.execute(
            "UPDATE experiments SET labeling_stage = 'round_1', bug_free = NULL, correctly_finished = NULL"
        )
        self._conn.commit()
        self._undo_stack.clear()
        return cur.rowcount

    def reset_experiment_pass(self, experiment_id: int, pass_num: int) -> int:
        """Reset only one labeling pass for an experiment.

        Pass 1 reset clears label_1 (and ``label`` if currently in round_1).
        Pass 2 reset clears label_2/label_final (and ``label`` if in round_2 or later).
        Stage is also reverted appropriately.

        Args:
            experiment_id: The experiment to partially reset.
            pass_num: 1 or 2.

        Returns:
            Number of frames affected.
        """
        exp_row = self._conn.execute(
            "SELECT labeling_stage FROM experiments WHERE id = ?", (experiment_id,)
        ).fetchone()
        stage = exp_row["labeling_stage"] if exp_row else "round_1"

        if pass_num == 1:
            # Clear label_1; if currently in round_1, also reset active label
            if stage in ("round_1",):
                cur = self._conn.execute(
                    """
                    UPDATE frames SET label = 'Unlabeled', label_1 = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE experiment_id = ? AND missing = 0
                    """,
                    (experiment_id,),
                )
            else:
                # In round_2/correction/done: only clear label_1, restore active display from label_2
                cur = self._conn.execute(
                    """
                    UPDATE frames SET label_1 = NULL, label_final = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE experiment_id = ? AND missing = 0
                    """,
                    (experiment_id,),
                )
                # If in correction/done, fall back to round_2
                if stage in ("correction", "done"):
                    self._conn.execute(
                        """
                        UPDATE frames SET label = COALESCE(label_2, 'Unlabeled')
                        WHERE experiment_id = ? AND missing = 0
                        """,
                        (experiment_id,),
                    )
                    self._conn.execute(
                        "UPDATE experiments SET labeling_stage = 'round_2' WHERE id = ?",
                        (experiment_id,),
                    )
        else:  # pass_num == 2
            # Clear label_2 and label_final
            cur = self._conn.execute(
                """
                UPDATE frames SET label_2 = NULL, label_final = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND missing = 0
                """,
                (experiment_id,),
            )
            # If active display is round_2/correction/done, reset label to Unlabeled
            if stage in ("round_2", "correction", "done"):
                self._conn.execute(
                    """
                    UPDATE frames SET label = 'Unlabeled'
                    WHERE experiment_id = ? AND missing = 0
                    """,
                    (experiment_id,),
                )
                self._conn.execute(
                    "UPDATE experiments SET labeling_stage = 'round_2' WHERE id = ?",
                    (experiment_id,),
                )

        self._conn.commit()
        self._undo_stack.clear()
        return cur.rowcount

    def import_labels(self, rows: list[dict], pass_num) -> dict:
        """Bulk-import labels from a list of dicts into one labeling pass.

        Each dict must have ``name`` (experiment folder name), ``timestep``
        (integer), and ``label`` (string).  Rows that don't match a known
        experiment/frame or carry an invalid label are skipped.

        If the target pass is currently the active stage, the ``label``
        column is also updated so the UI reflects the import immediately.

        Args:
            rows: List of dicts parsed from an uploaded CSV.
            pass_num: 1, 2, or "voting" (imports into label_final).

        Returns:
            ``{"updated": n, "skipped": n}``
        """
        if pass_num == "voting":
            col = "label_final"
            active_stage = "correction"   # mirror to label when in correction
        elif pass_num == 2:
            col = "label_2"
            active_stage = "round_2"
        else:
            col = "label_1"
            active_stage = "round_1"

        updated = 0
        skipped = 0

        # Build a name→(id, stage) lookup to avoid per-row queries
        exp_lookup: dict[str, tuple[int, str]] = {
            row["name"]: (row["id"], row["labeling_stage"])
            for row in self._conn.execute("SELECT id, name, labeling_stage FROM experiments")
        }

        for row in rows:
            name = row.get("name") or row.get("experiment_name", "")
            try:
                timestep = int(row.get("timestep", ""))
            except (ValueError, TypeError):
                skipped += 1
                continue
            label = row.get("label") or row.get(col, "")
            if label not in LABELS:
                skipped += 1
                continue
            if name not in exp_lookup:
                skipped += 1
                continue

            exp_id, stage = exp_lookup[name]
            cur = self._conn.execute(
                f"""
                UPDATE frames SET {col} = ?, updated_at = CURRENT_TIMESTAMP
                WHERE experiment_id = ? AND timestep = ? AND missing = 0
                """,
                (label, exp_id, timestep),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue

            # Mirror to active label column if this pass is currently active
            if stage == active_stage:
                self._conn.execute(
                    "UPDATE frames SET label = ? WHERE experiment_id = ? AND timestep = ?",
                    (label, exp_id, timestep),
                )
            updated += 1

        self._conn.commit()
        return {"updated": updated, "skipped": skipped}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_csv(self, which: str = "all") -> str:
        """Export all non-missing frames as a CSV string.

        Args:
            which: One of ``"all"`` (all three label columns),
                ``"r1"`` (label_1 only), ``"r2"`` (label_2 only),
                or ``"voting"`` (label_final only).

        Returns:
            A CSV-formatted string including the header row.
        """
        base_cols = "e.name, e.hash, e.P, e.VX, e.LS, e.ST, e.bug_free, e.correctly_finished, f.timestep"
        base_headers = ["name", "hash", "P", "VX", "LS", "ST", "bug_free", "correctly_finished", "timestep"]

        if which == "r1":
            select = f"{base_cols}, f.label_1"
            headers = base_headers + ["label_1"]
        elif which == "r2":
            select = f"{base_cols}, f.label_2"
            headers = base_headers + ["label_2"]
        elif which == "voting":
            select = f"{base_cols}, f.label_final"
            headers = base_headers + ["label_final"]
        else:  # "all"
            select = f"{base_cols}, f.label_1, f.label_2, f.label_final"
            headers = base_headers + ["label_1", "label_2", "label_final"]

        cur = self._conn.execute(
            f"""
            SELECT {select}
            FROM frames f
            JOIN experiments e ON e.id = f.experiment_id
            WHERE f.missing = 0
            ORDER BY e.P, e.VX, e.LS, e.ST, f.timestep
            """
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        writer.writerows(cur.fetchall())
        return buf.getvalue()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()
