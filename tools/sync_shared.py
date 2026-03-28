#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_SRC = ROOT / "shared"
FUNCTIONS_DIR = ROOT / "functions"


def main() -> None:
    if not SHARED_SRC.exists():
        raise SystemExit(f"Missing shared folder: {SHARED_SRC}")

    # Make shared a package
    init_file = SHARED_SRC / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")

    if not FUNCTIONS_DIR.exists():
        raise SystemExit(f"Missing functions folder: {FUNCTIONS_DIR}")

    for func_dir in FUNCTIONS_DIR.iterdir():
        if not func_dir.is_dir() or func_dir.name.startswith("."):
            continue

        dest = func_dir / "shared"
        if dest.exists():
            shutil.rmtree(dest)

        shutil.copytree(SHARED_SRC, dest)
        print(f"Copied shared -> {dest}")


if __name__ == "__main__":
    main()