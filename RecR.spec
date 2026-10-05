# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ['src/main.py'],
    pathex=['.'],
    binaries=[],
    datas=collect_data_files('sv_ttk'),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 未使用の大きな依存を除外してexeサイズを抑える:
    # numpy(レベル計算は純Python化済み)、PillowのAVIF対応とフォント描画(どちらも未使用)
    excludes=['numpy', 'PIL._avif', 'PIL.AvifImagePlugin', 'PIL._imagingft'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RecR',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
