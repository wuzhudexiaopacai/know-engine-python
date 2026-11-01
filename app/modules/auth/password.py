"""口令哈希（pbkdf2_hmac-SHA256，纯标准库实现）。

工程选型说明：
- 放弃 bcrypt 原生扩展（Windows 源码编译易失败），改用 hashlib.pbkdf2_hmac，
  260000 次迭代满足 OWASP 2023 推荐强度
- 存储格式：pbkdf2_sha256$迭代数$盐(b64)$哈希(b64)，自描述、可调参
- 恒定时间比较（hmac.compare_digest）防时序侧信道
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 260_000


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(plain: str) -> str:
    """生成自描述哈希串（含随机盐）。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(plain: str, stored: str) -> bool:
    """校验口令；格式不合法一律 False（不抛异常，防枚举探测）。"""
    try:
        algo, iters, salt_b64, digest_b64 = stored.split("$")
        if algo != _ALGORITHM:
            return False
        calc = hashlib.pbkdf2_hmac("sha256", plain.encode(),
                                   base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(calc, base64.b64decode(digest_b64))
    except (ValueError, TypeError):
        return False
