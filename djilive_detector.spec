# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules
import os
import imageio_ffmpeg as _iff

# 收集 imageio_ffmpeg 自带的 ffmpeg 二进制，打包到 exe 同级目录
try:
    FFMPEG_BIN = _iff.get_ffmpeg_exe()
except Exception:
    FFMPEG_BIN = None

hiddenimports = ['ultralytics', 'torch', 'torchvision', 'cv2', 'numpy', 'dxcam', 'mss', 'yaml', 'PIL', 'PIL.ImageTk', 'tkinter', 'requests', 'imageio_ffmpeg', 'music_engine', 'music_engine.LRCParser', 'music_page']
hiddenimports += collect_submodules('ultralytics')


datas = [('config.yaml', '.'), ('yolov8n.pt', '.'), ('models', 'models'), ('music_player.html', '.'), ('assets/backgrounds', 'assets/backgrounds')]
if FFMPEG_BIN and os.path.isfile(FFMPEG_BIN):
    datas.append((FFMPEG_BIN, '.'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='djilive_detector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='djilive_detector',
)
