"""配置版本迁移（config.ini 的「结构升级」）。

这里按「版本号 + 一串有序的迁移函数」处理升级，与数据库的
`schema_version`（见 core/database.py）同一套思路。

约定（**每条都重要**）：

1. **版本号复用软件版本号**（`app.__version__`，形如 `1.0.0`），
   不单独维护一个"配置版本常量"：配置结构本身就是随软件走的，
   再来一个常量只会多一个忘记同步的地方。
   但它记录的是「**这份配置的结构最后迁移到哪个版本**」，而不是
   「最后打开它的软件版本」：只涨版本号、没有结构变化时 `run()`
   会直接返回，`config_version` 保持原样（见 `run()` 的 `not STEPS`
   短路）。
2. **迁移函数必须幂等**：每次都自己判断"旧键在不在 / 新键有没有值"，
   不依赖"版本号到了就跳过"。理由见 database._migrate 的注释 ——
   版本号一旦写错或落后于真实结构，靠版本号做 guard 会让后续迁移
   永远不执行。幂等之后重复执行也无害。
3. **只做加法与搬迁，不整份重建**：config.ini 里是用户手填的
   Token、路径、快捷键，重建等于全丢。
4. **迁移前留备份，放在单独的文件夹里**：
   `<用户数据目录>/config_backups/config.ini.<旧版本号>`，只在真的
   执行了迁移时写，并且只保留最近 `BACKUP_KEEP` 份。
   **不能放在 config.ini 旁边**：备份里有用户的 Bangumi Token，
   而开发态 config.ini 在项目根，`.gitignore` 只忽略了精确的
   `config.ini` —— 紧挨着放的备份（如 `config.ini.1.0.0`）不会被
   忽略，一次 `git add .` 就把 Token 带进仓库。
   `data/` 整个目录都是忽略的，放里面天然安全。打包态同理：
   `%APPDATA%\\AnimeMarker\\config_backups\\` 与库文件、封面缓存分开，
   用户一眼知道能删。

新增一次迁移的做法（以「从 1.1.0 升上来时删掉废弃键」为例）：

    def _migrate_1_1_0(cfg):
        cfg.remove_option("scanner", "sp_numbering")

    STEPS = {**STEPS, "1.1.0": _migrate_1_1_0}

即：**键是"从这个版本升级上来"的版本号**，函数名照抄这个版本号。
"""

from __future__ import annotations

import logging
import shutil
from typing import Callable

from app import __version__
from app.utils.paths import app_data_dir

log = logging.getLogger(__name__)

#: 版本号 / 备份标记所在的节
META_SECTION = "meta"
#: 盘上这份 config.ini 的结构**最后迁移到**的版本
#: （不等于"最后打开它的软件版本"：只发版不改结构时不会更新）
KEY_VERSION = "config_version"

#: 备份目录名（在用户数据目录下，与 config.ini 分开）
BACKUP_DIR_NAME = "config_backups"
#: 最多保留几份备份（按版本号从新到旧）
BACKUP_KEEP = 3


# ---------- 迁移函数 ----------
#
# 每个函数都接收 Config 实例（只调它的 get/set/remove_option，
# 不要碰 self._parser —— 那是实现细节），并且**必须幂等**。


# ---------- 步骤表 ----------
#
# 键 = 升级前的版本号（"盘上那份配置的结构停在哪个版本"）。
# 值 = 从该版本升到下一版的迁移函数。
#
# 目前为空：还没有发生过需要迁移的结构变化。
STEPS: dict[str, Callable] = {}


def _parse_version(text: str) -> tuple[int, ...]:
    """"1.0.0" → (1, 0, 0)；解析不了的段按 0 处理（不抛异常）。

    为什么不用 `packaging.version`：它不在 requirements 里，
    为了一个比较再引入一个第三方依赖不值得；这里只需要"谁更新"。
    """
    parts: list[int] = []
    for seg in (text or "").strip().split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def is_newer(a: str, b: str) -> bool:
    """版本 a 是否比 b 新（逐段比较，长度不同时补 0）。"""
    va, vb = _parse_version(a), _parse_version(b)
    n = max(len(va), len(vb))
    va += (0,) * (n - len(va))
    vb += (0,) * (n - len(vb))
    return va > vb


def backup_dir():
    """备份目录（`<用户数据目录>/config_backups/`），不存在则创建。"""
    d = app_data_dir() / BACKUP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_path(config_path, from_version: str):
    """`config.ini` → `config_backups/config.ini.<from_version>`。

    注意**不是**放在 config.ini 旁边：见模块 docstring 第 4 条
    （备份含 Token，不能落到未被 .gitignore 覆盖的目录里）。
    """
    return backup_dir() / f"{config_path.name}.{from_version}"


def _prune_backups(config_name: str) -> None:
    """只保留最近 `BACKUP_KEEP` 份备份，多出来的删掉。

    排序依据是文件名里的版本号（`config.ini.<version>`），而不是
    修改时间：`shutil.copy2` 会保留源文件的 mtime，同一份配置在不同
    时间备份出来的 mtime 可能相同，按版本号排是确定的。
    """
    d = backup_dir()
    try:
        files = sorted(
            (p for p in d.glob(f"{config_name}.*") if p.is_file()),
            key=lambda p: _parse_version(p.name[len(config_name) + 1:]),
        )
    except OSError as e:              # 目录读不了就跳过清理，不影响启动
        log.warning("读取配置备份目录失败（跳过清理）：%s", e)
        return

    for old in files[:-BACKUP_KEEP]:
        try:
            old.unlink()
            log.info("清理旧配置备份：%s", old.name)
        except OSError as e:
            log.warning("清理旧配置备份失败：%s", e)


def mark_current(config) -> None:
    """把结构版本标成当前软件版本（全新配置 / 迁移完成时用）。不存盘。"""
    config.set(META_SECTION, KEY_VERSION, __version__)


def run(config) -> bool:
    """执行升级。返回是否**真的**做了迁移（调用方据此决定 save）。

    `config` 需提供：`path`（Path）、`get / set / remove_option`、
    `has_section`。见 Config 的同名方法。

    判定用「版本号更旧」而不是「版本号不同」：
      - 用户从新版本降级回旧版本：盘上版本比程序新 → 什么都不做，
        免得旧代码把新结构误删。
      - `STEPS` 为空（当前就是）：没有结构变化，直接返回 ——
        既不写备份，也不改版本号。结构没变，版本号就该保持原样。
      - 没有 `config_version`（更早的版本不写这个键）：按"最旧"处理，
        让所有迁移跑一遍（它们都幂等，无操作也无害）。
    """
    old = (config.get(META_SECTION, KEY_VERSION, "") or "").strip()
    if old and not is_newer(__version__, old):
        # 同版本或盘上更新：无需迁移（降级时尤其不能动）
        return False
    if not STEPS:
        # 没有结构迁移可做：不动盘上的文件（不备份、不盖版本号）
        return False

    old_label = old or "legacy"

    # 先备份再动：迁移中途出错时，用户至少还有原始文件
    dst = backup_path(config.path, old_label)
    try:
        if config.path.exists():
            shutil.copy2(config.path, dst)
            log.info("配置迁移前备份：%s", dst)
            _prune_backups(config.path.name)
    except OSError as e:              # 备份失败不该阻止程序启动
        log.warning("配置备份失败（继续迁移）：%s", e)

    # 依次执行所有步骤（按版本从旧到新）。
    #
    # **为什么不做「只跑还没跑过的那些」的筛选**：步骤本身幂等（自己判断
    # 旧键在不在、新键有没有值），全跑一遍是安全的；反而筛选要处理
    # "没记录版本号"这个边界，容易筛错、把该跑的那步漏掉。
    for step_version in sorted(STEPS, key=_parse_version):
        try:
            STEPS[step_version](config)
        except Exception as e:        # pragma: no cover - 防御性
            log.exception("配置迁移步骤 %s 失败（继续后续步骤）：%s",
                          step_version, e)

    mark_current(config)
    log.info("配置版本 %s → %s", old or "（未记录）", __version__)
    return True
