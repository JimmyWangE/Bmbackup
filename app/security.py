import hashlib
import hmac
import secrets
import time

import jwt
from fastapi import HTTPException

from app.config import get_settings

ALGORITHM = "HS256"


class ApiError(HTTPException):
    """统一的错误格式：{"detail": {"code": "...", "message": "..."}}，App 按 code 判断。"""

    def __init__(self, status: int, code: str, message: str, **extra: object) -> None:
        super().__init__(status_code=status, detail={"code": code, "message": message, **extra})


def hash_secret(value: str) -> str:
    """设备凭证是 App 生成的高强度随机串，直接 SHA-256 即可。"""
    return hashlib.sha256(value.encode()).hexdigest()


def hash_code(email: str, code: str) -> str:
    """验证码只有 6 位，用服务端密钥做 HMAC，数据库泄露也无法离线穷举。"""
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def issue_tokens(account_id: str, device_id: str) -> dict:
    s = get_settings()
    now = int(time.time())
    access = jwt.encode(
        {"sub": account_id, "did": device_id, "typ": "access", "iat": now, "exp": now + s.access_token_minutes * 60},
        s.secret_key,
        ALGORITHM,
    )
    refresh = jwt.encode(
        {"sub": account_id, "did": device_id, "typ": "refresh", "iat": now, "exp": now + s.refresh_token_days * 86400,
         "jti": secrets.token_hex(8)},
        s.secret_key,
        ALGORITHM,
    )
    return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": s.access_token_minutes * 60}


def decode_token(token: str, expected_type: str) -> dict:
    try:
        claims = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "token_expired", "登录已过期，请刷新令牌")
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "令牌无效")
    if claims.get("typ") != expected_type:
        raise ApiError(401, "invalid_token", "令牌类型不对")
    return claims


def sign_blob(object_key: str, method: str, expires_at: int) -> str:
    """本地存储模式下，照片/语音上传下载链接的签名。"""
    key = get_settings().secret_key.encode()
    return hmac.new(key, f"{method}:{object_key}:{expires_at}".encode(), hashlib.sha256).hexdigest()
