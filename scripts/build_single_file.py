#!/usr/bin/env python3
"""Build the self-contained single-file harness release asset."""

from __future__ import annotations

import shutil
import stat
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "single-file"
DIST = ROOT / "dist"
OUT = DIST / "harness"


def main() -> int:
    """Package the pure-Python harness modules into one executable zipapp."""

    if BUILD.exists():
        shutil.rmtree(BUILD)
    DIST.mkdir(exist_ok=True)
    shutil.copytree(
        ROOT / "llm_harness",
        BUILD / "llm_harness",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (BUILD / "__main__.py").write_text(
        "from llm_harness.cli import main\n"
        "raise SystemExit(main())\n"
    )
    if OUT.exists():
        OUT.unlink()
    zipapp.create_archive(BUILD, OUT, interpreter="/usr/bin/env python3")
    OUT.chmod(OUT.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
