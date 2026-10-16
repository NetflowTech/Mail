# -*- mode: python ; coding: utf-8 -*-
import sys
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# Locate the certifi CA bundle inside whatever venv is active at build time.
import certifi as _certifi
CERTIFI_PEM = _certifi.where()

# Platform-specific hidden imports
PLATFORM_IMPORTS = []
if IS_MAC:
    PLATFORM_IMPORTS += ['keyring.backends.macOS']
if IS_WIN:
    PLATFORM_IMPORTS += ['keyring.backends.Windows', 'winotify']

a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('assets', 'assets'),
        (CERTIFI_PEM, 'certifi'),
    ],
    hiddenimports=(
        collect_submodules('emailclient') +
        collect_submodules('PySide6') +
        ['pyspellchecker', 'imapclient', 'msal', 'cryptography', 'certifi',
         'PySide6.QtSvg', 'PySide6.QtSvgWidgets'] +
        PLATFORM_IMPORTS
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AMail',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/AMail-icon-Exo2.ico' if IS_WIN else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AMail',
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name='AMail.app',
        icon='assets/AMail-icon-Exo2.icns',
        bundle_identifier='com.amail.app',
        info_plist={
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,
            'CFBundleShortVersionString': '2.0.3',
            'CFBundleVersion': '2.0.3',
            'LSMinimumSystemVersion': '12.0',
        },
    )
