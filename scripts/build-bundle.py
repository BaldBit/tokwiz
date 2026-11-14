#!/usr/bin/env python3
"""Release Bundle Builder for TokWiz (`scripts/build-bundle.py`).

Generates standalone, zero-clone installation artifacts in `dist/`:
1. `dist/tokwiz-bundle.tar.gz` — Portable bundle containing `SKILL.md`, `scripts/`,
   `hooks/`, `references/`, `adapters/`, `dashboard/`, and `evaluations/`.
2. `dist/tokwiz.pyz` — Single-file executable Python zipapp (`#!/usr/bin/env python3`).
3. `dist/install.sh` — One-liner curl installer fetching the latest GitHub Release bundle.
"""
from __future__ import annotations

import shutil
import stat
import tarfile
import tempfile
import zipapp
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_DIR = REPO_ROOT / "dist"

BUNDLE_ITEMS = [
    "SKILL.md",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "references",
    "scripts",
    "hooks",
    "adapters",
    "dashboard",
    "evaluations",
]


def build_bundles() -> None:
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        stage_dir = Path(tmp) / "tokwiz"
        stage_dir.mkdir(parents=True, exist_ok=True)

        for item in BUNDLE_ITEMS:
            src = REPO_ROOT / item
            dst = stage_dir / item
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(
                    src,
                    dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
                )
            else:
                shutil.copy2(src, dst)

        # 1. Create dist/tokwiz-bundle.tar.gz
        tar_path = DIST_DIR / "tokwiz-bundle.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            tar.add(stage_dir, arcname="tokwiz")

        # 2. Create dist/tokwiz.pyz (executable zipapp)
        pyz_path = DIST_DIR / "tokwiz.pyz"
        zipapp.create_archive(
            stage_dir,
            target=pyz_path,
            interpreter="/usr/bin/env python3",
            main="scripts.cli:main",
        )
        pyz_path.chmod(pyz_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # 3. Copy install.sh into dist/install.sh
        install_src = REPO_ROOT / "scripts" / "install.sh"
        if install_src.exists():
            install_dst = DIST_DIR / "install.sh"
            shutil.copy2(install_src, install_dst)
            install_dst.chmod(install_dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"[TokWiz Build] Created {tar_path.name} and {pyz_path.name} in {DIST_DIR}")


if __name__ == "__main__":
    build_bundles()
