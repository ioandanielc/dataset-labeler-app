"""
run.py — CLI entry point
========================
Usage:
    python run.py [/path/to/data] [--port PORT] [--db PATH] [--suggester NAME]

If no path is given the last-used path is loaded from labeler_config.json.
If neither exists, open the browser and paste the path via the UI.
"""

import argparse
import json
import webbrowser
from pathlib import Path

from labeler.api import create_app

_CONFIG_FILE = Path(__file__).parent / "labeler_config.json"

DEFAULT_ROOT = "/Users/ioandanielcraciun/Python-Projects/cVAE-sph2img/data/raw/"


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
    args = parser.parse_args()

    root = args.root or _load_saved_root() or DEFAULT_ROOT

    app = create_app(root=root, db_path=args.db, suggester_name=args.suggester)

    url = f"http://localhost:{args.port}"
    print(f"Labeler running at {url}")
    print(f"Data root: {root}")
    webbrowser.open(url)

    app.run(port=args.port, debug=False)


if __name__ == "__main__":
    main()
