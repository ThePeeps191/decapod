"""Build a single-file `decapod` executable with PyInstaller.

    pip install -e ".[build]"
    python scripts/build_binary.py

The binary is written to dist/ (dist/decapod or dist/decapod.exe).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    entry = ROOT / "scripts" / "_entry.py"
    entry.write_text("from decapod.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(entry),
        "--name",
        "decapod",
        "--onefile",
        "--noconfirm",
        "--clean",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
        "--collect-data",
        "decapod",  # prompt templates, example charters, config template
        "--collect-data",
        "textual",  # Textual's CSS and widgets
        "--collect-submodules",
        "decapod",
        "--copy-metadata",
        "decapod",
    ]
    try:
        return subprocess.call(command, cwd=ROOT)
    finally:
        entry.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
