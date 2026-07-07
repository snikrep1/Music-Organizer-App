#!/usr/bin/env python3
"""Build Music Organizer as a standalone executable for the current platform.

Usage:
    python3 build.py

Produces:
    dist/music-organizer        (Linux)
    dist/MusicOrganizer.exe     (Windows)
    dist/MusicOrganizer.app/    (macOS .app bundle)

Run the same command on each platform — this script only builds the native
binary for the machine it's running on. Cross-compiling to other OSes is not
supported by PyInstaller; use the GitHub Actions workflow for that.
"""

import subprocess
import sys
import shutil
from pathlib import Path
from typing import Optional

HERE = Path(__file__).parent.resolve()


def ensure(cmd: list, cwd: Optional[Path] = None):
    print(f"$ {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd)
    if res.returncode != 0:
        sys.exit(res.returncode)


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not installed. Install it first:")
        print("    python3 -m pip install pyinstaller PyQt6 mutagen")
        sys.exit(1)

    for d in ("build", "dist"):
        p = HERE / d
        if p.exists():
            print(f"Removing stale {d}/")
            shutil.rmtree(p)

    ensure(
        [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
         "music_organizer.spec"],
        cwd=HERE,
    )

    out = HERE / "dist"
    if sys.platform == "darwin":
        print(f"\n[OK] macOS app bundle built: {out / 'MusicOrganizer.app'}")
        print("     Drag into /Applications to install.")
    elif sys.platform == "win32":
        print(f"\n[OK] Windows executable built: {out / 'MusicOrganizer.exe'}")
    else:
        print(f"\n[OK] Linux executable built: {out / 'music-organizer'}")


if __name__ == "__main__":
    main()
