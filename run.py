"""
run.py — CLI entry point
========================
Usage:
    python run.py [/path/to/data] [--port PORT] [--db PATH] [--suggester NAME]
                  [--mode {two_pass,single_pass}]

If no path is given the last-used path is loaded from labeler_config.json.
If neither exists, open the browser and paste the path via the UI.

The labeling mode is likewise remembered between launches — it is normally
picked from the dashboard rather than on the command line.
"""

import argparse
import json
import webbrowser
from pathlib import Path

from labeler.api import LABELING_MODES, create_app

_CONFIG_FILE = Path(__file__).parent / "labeler_config.json"

DEFAULT_ROOT = "/Users/ioandanielcraciun/Downloads/output_data/"


def _load_saved_root() -> str | None:
    try:
        return json.loads(_CONFIG_FILE.read_text()).get("root")
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Melt Pool Frame Labeler")
    parser.add_argument("root", nargs="?", default=None,
                        help="Root directory containing simulation folders")
    parser.add_argument("--port", type=int, default=5050)
    parser.add_argument("--db", default=None, help="Path to SQLite DB file")
    parser.add_argument("--suggester", default="dummy",
                        help="Suggester to use (default: dummy)")
    parser.add_argument("--mode", choices=LABELING_MODES, default=None,
                        help="Labeling workflow (default: last one used)")
    args = parser.parse_args()

    root = args.root or _load_saved_root() or DEFAULT_ROOT

    app = create_app(root=root, db_path=args.db, suggester_name=args.suggester,
                     labeling_mode=args.mode)

    url = f"http://localhost:{args.port}"
    print(f"Labeler running at {url}")
    print(f"Data root: {root}")
    print(f"Mode:      {app.labeling_mode}")
    webbrowser.open(url)

    app.run(port=args.port, debug=False)


if __name__ == "__main__":
    main()
