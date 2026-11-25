"""敏感配置项的本地加解密（Windows DPAPI）。

**能防什么 / 不能防什么**：

- 能防：config.ini 被误发、上传网盘、随日志/截图泄露；
- 不能防：同一 Windows 用户下运行的其它程序（它们同样能调 DPAPI 解密）、
  已经拿到你账户密码并能登录本机的人。想再加一层就开 BitLocker。

存储格式：`enc:v1:<base64(CryptProtectData 输出)>`。
不带前缀的值按明文处理，便于 `Config` 把旧配置就地迁移。

**非 Windows 环境**（开发/CI）退化为明文，只记一条警告 —— 本项目的
发布目标只有 Windows。
"""

from __future__ import annotations

import base64
import logging
import sys

log = logging.getLogger(__name__)

#: 加密值的标记前缀（带版本，方便以后换算法时区分）
PREFIX = "enc:v1:"

#: 附加熵：同一台机器上，别的程序即使拿到密文也不能直接解出本项目的值
_ENTROPY = b"AnimeMarker.v1.bangumi.token"

#: win32cryptcon.CRYPTPROTECT_UI_FORBIDDEN
#: 禁止 DPAPI 弹任何 UI（后台线程里更不能弹）
_UI_FORBIDDEN = 0x1

_IS_WINDOWS = sys.platform == "win32"

_warned_plain = False
_warned_decrypt = False


def is_encrypted(value: str) -> bool:
    """是否是本模块产出的密文格式。"""
    return str(value or "").startswith(PREFIX)


def protect(text: str) -> str:
    """明文 → `enc:v1:...`；空串原样返回，加密不可用时退化为明文。"""
    global _warned_plain

    text = str(text or "")
    if not text:
        return ""

    if not _IS_WINDOWS:
        if not _warned_plain:
            _warned_plain = True
            log.warning("当前不是 Windows：敏感配置将以明文保存")
        return text

    try:
        import win32crypt

        blob = win32crypt.CryptProtectData(
            text.encode("utf-8"),
            "Anime Marker",
            _ENTROPY,
            None,
            None,
            _UI_FORBIDDEN,
        )
        return PREFIX + base64.b64encode(blob).decode("ascii")
    except Exception as e:  # noqa: BLE001 - 加密失败不该阻止用户保存设置
        log.warning("加密失败（将明文保存）：%s", e)
        return text


def unprotect(value: str) -> tuple[str, bool]:
    """存储值 → `(明文, 是否成功)`。

    - 空串 / 明文（无前缀）：原样返回，视为成功；
    - 密文但解不开（换电脑、换 Windows 用户、文件被改）：返回 `("", False)`，
      调用方应把它当成「没配」处理，不要因此崩溃。
    """
    global _warned_decrypt

    raw = str(value or "")
    if not raw:
        return "", True
    if not is_encrypted(raw):
        return raw, True

    try:
        import win32crypt

        payload = base64.b64decode(raw[len(PREFIX):], validate=True)
        _, data = win32crypt.CryptUnprotectData(
            payload, _ENTROPY, None, None, _UI_FORBIDDEN
        )
        return data.decode("utf-8"), True
    except Exception as e:  # noqa: BLE001 - 解密失败按空值处理
        if not _warned_decrypt:
            _warned_decrypt = True
            log.warning(
                "敏感配置解密失败（配置可能来自其它电脑 / 其它 Windows 用户），"
                "将按未配置处理，请在设置里重新填写：%s", e,
            )
        return "", False
