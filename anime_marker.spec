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
        # 应用图标（EXE 图标 + 运行时窗口/任务栏图标）。
        # 由 resources/icons/logo.svg 渲染的 16/24/32/48/64/128/256 多尺寸 ICO：
        #   - 打包时给 EXE(icon=) 用；
        #   - 运行时由 QmlApp 调 resource_path("app.ico") 设置窗口图标。
        # 它在 resources/ 根下、不在 icons/ 里，必须单列。
        ('resources/app.ico', 'resources'),
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
        # 内置字体（设置页「字体」下拉里的「霞鹜文楷 GB 轻便版」）。
        # 由 paths.resource_path("fonts/...") 读取；漏打包时程序仍可用，
        # 只是下拉菜单里少了内置字体（FontBridge 会记一条警告）。
        ('resources/fonts', 'resources/fonts'),
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
        # Token 的 DPAPI 加解密。
        'win32crypt',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 排除其它 Qt 绑定：Conda 环境里常同时装着 PyQt5，
    # PyInstaller 会为它跑 hook，然后因为"多个 Qt 绑定"直接中止打包。
    # 本项目只用 PySide6，显式排除即可。
    #
    # 后面几项是**体积裁剪**（模块层面）：conda 环境里 numpy/Tcl-Tk 会被
    # 其它包的 hook 顺带拉进来，本项目一行都没用过（见下方 _PRUNE_PREFIXES）。
    excludes=[
        'PyQt5',
        'PyQt6',
        'PySide2',
        'tkinter',
        'numpy',
        'scipy',
        'pandas',
        'matplotlib',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineQuick',
        'PySide6.QtWebChannel',
        'PySide6.QtWebSockets',
        'PySide6.QtPdf',
        'PySide6.QtPdfQuick',
        'PySide6.QtTest',
        'PySide6.QtQuickTest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# ---------------------------------------------------------------------------
# 体积裁剪（二进制 / 数据层面）
#
# 这里按**打包目标路径前缀**过滤 Analysis 的TOC —— 打包时就不再复制，源文件一行不动。
#
# 只按前缀匹配，Qt 升级后多出来的同名变体（如 Qt6WebEngineQuick*）也能
# 一并覆盖。
_PRUNE_PREFIXES = (
    # conda 数学库 / OpenMP / MPI
    'mkl', 'libiomp', 'omptarget', 'msmpi', 'tbb',
    # 未使用的 Qt 模块
    'qt6webengine', 'qt6webchannel', 'qt6websockets',
    'qt6pdf', 'qt6quicktest', 'qt6test',
    # Tcl/Tk
    'tcl', 'tk86', '_tcl_data', '_tk_data', '_tkinter',
    # numpy 残留（excludes 已挡模块，这里清 DLL / dist-info）
    'numpy',
    # 对应的 QML 模块目录
    'pyside6/qml/qtwebengine', 'pyside6/qml/qtwebchannel',
    'pyside6/qml/qtwebsockets', 'pyside6/qml/qttest',
    'pyside6/qml/qmltime',
)


def _prune_toc(toc):
    """丢掉目标路径以 _PRUNE_PREFIXES 开头的条目。"""
    kept = []
    for name, src, kind in toc:
        if str(name).replace('\\', '/').lower().startswith(_PRUNE_PREFIXES):
            continue
        kept.append((name, src, kind))
    return kept


_toc_before = len(a.binaries) + len(a.datas)
a.binaries = _prune_toc(a.binaries)
a.datas = _prune_toc(a.datas)
print(f'[spec] 体积裁剪：TOC {_toc_before} -> '
      f'{len(a.binaries) + len(a.datas)} 个条目')

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
    # 应用图标：resources/app.ico（由 resources/icons/logo.svg 渲染，
    # 含 16/24/32/48/64/128/256 七种尺寸）。
    icon='resources/app.ico',
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
