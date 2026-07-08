# PyInstaller spec for Music Organizer.
# Build with:  pyinstaller music_organizer.spec
# Produces a single-file executable in dist/ for the current platform.

import sys
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH)

a = Analysis(
    ['music_organizer.py'],
    pathex=[str(HERE)],
    binaries=[],
    datas=[],
    # mutagen loads format modules lazily; pull them all in so no format is
    # dropped from the frozen build.
    hiddenimports=[
        'mutagen', 'mutagen.mp3', 'mutagen.easyid3', 'mutagen.id3',
        'mutagen.flac', 'mutagen.mp4', 'mutagen.easymp4',
        'mutagen.oggvorbis', 'mutagen.oggopus', 'mutagen.asf',
        'mutagen.aac', 'mutagen.aiff',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'pydoc', 'doctest',
        'test', 'distutils', 'setuptools', 'PIL',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if sys.platform == 'darwin':
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='MusicOrganizer',
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        console=False, disable_windowed_traceback=False,
        argv_emulation=True, target_arch=None,
        codesign_identity=None, entitlements_file=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        strip=False, upx=False, name='MusicOrganizer',
    )
    app = BUNDLE(
        coll,
        name='MusicOrganizer.app',
        icon=None,
        bundle_identifier='com.guy.musicorganizer',
        info_plist={
            'CFBundleShortVersionString': '0.2.1',
            'CFBundleVersion': '0.2.1',
            'NSHighResolutionCapable': 'True',
            'NSPrincipalClass': 'NSApplication',
        },
    )
else:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
        name='MusicOrganizer' if sys.platform == 'win32' else 'music-organizer',
        debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
        upx_exclude=[], runtime_tmpdir=None,
        console=False, disable_windowed_traceback=False,
        argv_emulation=False, target_arch=None,
        codesign_identity=None, entitlements_file=None,
    )
