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
        # 海报圆角遮罩（PosterCard 用它把封面裁成"上圆下直"）。
        # **必须单列**：它不在 resources/icons 里，而是在 resources/ 根下，
        # 上面的目录条目覆盖不到。漏了它 → 打包后封面四角全变直角
        # （MultiEffect 拿不到遮罩 → 遮罩失效），开发态却看不出问题。
        # 由 paths.resource_path("poster_mask.png") 读取。
        ('resources/poster_mask.png', 'resources'),
        # 离线帮助页（状态栏「更多 → 帮助」用系统浏览器打开）。
        # 与 poster_mask 同理必须单列 —— 它在 resources/ 根下，不在
        # icons 目录里。由 paths.resource_path("help.html") 读取，
        # 缺了的话那一项会提示"帮助文档缺失"。
        ('resources/help.html', 'resources'),
        # QML 界面文件：映射到 _MEIPASS/qml，与 paths.qml_dir() 的契约一致。
        # .qml 是运行时加载的，PyInstaller 静态分析发现不了，必须显式打包。
        ('app/qml', 'qml'),
        # 许可证：PySide6 是 LGPL-3.0，分发二进制时必须随附许可信息与全文；
        # MIT / Apache / BSD 等也要求保留各自的版权声明。
        # THIRD_PARTY_LICENSES.md 是"哪个依赖用哪个许可证"的索引，
        # licenses/ 是各许可证原文 —— 两者缺一不可，都必须存在，
        # 否则 PyInstaller 会因 datas 源文件缺失直接报错终止。
        ('LICENSE', '.'),
        ('THIRD_PARTY_LICENSES.md', '.'),
        ('licenses', 'licenses'),
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
    # 应用图标：**暂缺，故不指定**（Windows 用默认 exe 图标）。
    #
    # 注意 `icon=` 指向的文件**必须存在**，否则 PyInstaller 直接报错终止。
    # 项目里目前没有任何 .ico（resources/icons/ 只放 SVG，见该目录 README）。
    # 以后补图标时：把 .ico 放进 resources/，再解开下面这行并改对路径。
    # icon='resources/app.ico',
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
