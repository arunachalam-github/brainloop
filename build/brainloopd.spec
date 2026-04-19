# PyInstaller spec for the brainloopd binary.
#
# Produces a single-file executable that embeds the daemon + all deps.
# The output lands in build/dist/brainloopd and is consumed by the
# Tauri bundle step (via tauri.conf.json `resources`).
#
# Run through `pyinstaller build/brainloopd.spec` — not directly.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# pyobjc + its frameworks each ship compiled extensions that PyInstaller's
# default analyzer misses (the framework modules lazy-load their Objective-C
# bridges at import time, which looks like dead code). Collect them all
# explicitly so the frozen binary can `import Quartz`, etc.
block_cipher = None

hiddenimports = []
datas = []
binaries = []

for pkg in (
    "objc",
    "Foundation",
    "AppKit",
    "Quartz",
    "CoreAudio",
    "ApplicationServices",
    "CoreFoundation",
):
    collected_datas, collected_binaries, collected_hidden = collect_all(pkg)
    datas += collected_datas
    binaries += collected_binaries
    hiddenimports += collected_hidden

# CLAUDE.md teaches the chat model the activity_log schema + example queries.
# daemon.prompts._load_claude_md reads it from sys._MEIPASS at runtime.
datas += [("../CLAUDE.md", ".")]


a = Analysis(
    ["entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # These are not needed at runtime and bloat the binary ~60 MB.
        "tkinter",
        "PIL",
        "numpy",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="brainloopd",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # daemon logs to stdout/stderr (redirected via plist)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,      # host arch; set to "arm64" / "x86_64" / "universal2" for cross-builds
    codesign_identity=None,
    entitlements_file=None,
)
