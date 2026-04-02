"""
scanner.py — Scanner
====================
Crawls the filesystem for simulation folders and PNG frames, then syncs
the results into :class:`~labeler.db.LabelDB`.

Role in the system:
    - Called on first launch (full scan) and on every subsequent open or
      manual re-scan (diff scan).
    - Parses folder names with :data:`DEFAULT_FOLDER_REGEX` to extract the
      four physics parameters: **P**, **VX**, **LS**, **ST**.
    - Finds ``png_files/ss_{timestep}_side.png`` files inside each matching
      folder.
    - Delegates all DB writes to :class:`~labeler.db.LabelDB` — Scanner
      itself never touches SQLite directly.
"""

import re
from pathlib import Path

from labeler.db import LabelDB

# ---------------------------------------------------------------------------
# Folder name regex
# ---------------------------------------------------------------------------

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
    r"_H-(?P<H>[^_]+)"
    r"(?:_(?P<hash>.+))?$"   # optional trailing hash / run-id suffix
)

# Regex for frame filenames: ss_{timestep}_side.png
_FRAME_RE = re.compile(r"^ss_(\d+)_side\.png$")


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class Scanner:
    """Crawls *root* for simulation folders and syncs frames into *db*.

    Args:
        root: Root directory that contains simulation folders.
        db: :class:`~labeler.db.LabelDB` instance to sync into.
        folder_regex: Compiled (or string) regex with named groups
            ``P``, ``VX``, ``LS``, ``ST``.  Defaults to
            :data:`DEFAULT_FOLDER_REGEX`.
    """

    def __init__(
        self,
        root: str | Path,
        db: LabelDB,
        folder_regex: str | re.Pattern = DEFAULT_FOLDER_REGEX,
    ) -> None:
        self.root = Path(root)
        self.db = db
        self._folder_re = (
            re.compile(folder_regex)
            if isinstance(folder_regex, str)
            else folder_regex
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> dict:
        """Run a full diff scan and sync results into the DB.

        For each matching simulation folder:
        - New experiments are inserted.
        - New frames are inserted as ``Unlabeled``.
        - Frames no longer on disk are marked ``missing=1``.
        - Frames that have reappeared have their ``missing`` flag cleared.
        - Rows are never deleted.

        Returns:
            A summary dict with keys ``experiments_seen``, ``frames_added``,
            ``frames_missing``, ``frames_restored``.
        """
        disk_tuples = self._crawl()

        experiments_seen = 0
        frames_added = 0
        frames_missing = 0
        frames_restored = 0

        # Group tuples by (P, VX, LS, ST, folder, name) so we make one DB round-trip
        # per experiment.
        from collections import defaultdict
        by_experiment: dict[tuple, list[int]] = defaultdict(list)
        for P, VX, LS, ST, folder, name, hash_val, timestep in disk_tuples:
            by_experiment[(P, VX, LS, ST, folder, name, hash_val)].append(timestep)

        for (P, VX, LS, ST, folder, name, hash_val), disk_timesteps in by_experiment.items():
            experiments_seen += 1
            exp_id = self.db.upsert_experiment(P, VX, LS, ST, folder, name, hash_val)

            existing = self.db.get_existing_timesteps(exp_id)
            disk_set = set(disk_timesteps)

            # New frames → insert as Unlabeled (missing=False)
            for ts in disk_set - existing:
                self.db.upsert_frame(exp_id, ts, missing=False)
                frames_added += 1

            # Frames that disappeared → mark missing
            for ts in existing - disk_set:
                self.db.upsert_frame(exp_id, ts, missing=True)
                frames_missing += 1

            # Frames that reappeared → clear missing flag
            # (upsert_frame sets missing=False, which handles restoration)
            # We need to check which existing frames are currently missing
            # and are now back on disk.
            existing_frames = self.db.get_frames(exp_id)
            for frame in existing_frames:
                if frame["missing"] and frame["timestep"] in disk_set:
                    self.db.upsert_frame(exp_id, frame["timestep"], missing=False)
                    frames_restored += 1

        return {
            "experiments_seen": experiments_seen,
            "frames_added": frames_added,
            "frames_missing": frames_missing,
            "frames_restored": frames_restored,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _crawl(self) -> list[tuple[str, str, str, str, str, str, str, int]]:
        """Walk *root* and return one tuple per valid frame file.

        Returns:
            List of ``(P, VX, LS, ST, folder, name, timestep)`` tuples.
            ``folder`` is the absolute path string of the simulation folder.
            ``name`` is the full folder name (including any hash suffix).
        """
        results: list[tuple[str, str, str, str, str, str, str, int]] = []

        if not self.root.is_dir():
            return results

        for entry in self.root.iterdir():
            if not entry.is_dir():
                continue
            m = self._folder_re.match(entry.name)
            if m is None:
                continue

            P = m.group("P")
            VX = m.group("VX")
            LS = m.group("LS")
            ST = m.group("ST")
            folder = str(entry.resolve())
            name = entry.name
            hash_val = m.group("hash") or ""

            png_dir = entry / "png_files"
            if not png_dir.is_dir():
                continue

            for png in png_dir.iterdir():
                fm = _FRAME_RE.match(png.name)
                if fm is None:
                    continue
                timestep = int(fm.group(1))
                results.append((P, VX, LS, ST, folder, name, hash_val, timestep))

        return results
