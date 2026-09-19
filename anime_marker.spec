# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单目录打包配置。

构建命令：
    pip install -r requirements-dev.txt
    pyinstaller anime_marker.spec --noconfirm --clean

产物：dist/AnimeMarker/AnimeMarker.exe + 依赖目录
"""

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('resources/icons', 'resources/icons'),
        # QML 界面文件：映射到 _MEIPASS/qml，与 paths.qml_dir() 的契约一致。
        # .qml 是运行时加载的，PyInstaller 静态分析发现不了，必须显式打包。
        ('app/qml', 'qml'),
        # 注：界面已全量迁移到 QML，不再需要样式表资源。
    ],
    hiddenimports=[
        'PySide6.QtSvg',
        'PySide6.QtSvgWidgets',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuickControls2',
        'sqlite3',
        'win32gui',
        'win32con',
    ],
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
    name='AnimeMarker',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # GUI 应用，不开控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='resources/icons/app.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AnimeMarker',
)
