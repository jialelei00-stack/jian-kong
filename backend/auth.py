"""认证模块：密码哈希、Token 签发与验证、角色权限中间件。

使用 PBKDF2-SHA256 哈希密码，随机 Token 做会话管理（无需 JWT 依赖）。
Token 同时存数据库 + HttpOnly Cookie，支持服务端主动废止。
"""

import hashlib
import os
import secrets
import time
from typing import Optional, Tuple
from datetime import datetime, timedelta

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse

import database

TOKEN_VALID_DAYS = 7
COOKIE_NAME = "jk_auth_token"


# ── 密码哈希 ──

def hash_password(password: str) -> str:
    """返回 "salt_hex:hash_hex"。"""
    salt = os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
    return salt.hex() + ":" + h.hex()


def verify_password(password: str, stored: str) -> bool:
    """验证密码与存储的哈希是否匹配。"""
    try:
        salt_hex, hash_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100000)
        return secrets.compare_digest(h.hex(), hash_hex)
    except Exception:
        return False


# ── Token 管理 ──

def create_token(user_id: int) -> str:
    """生成随机 Token 并存入数据库，返回 token 字符串。"""
    token = secrets.token_hex(32)
    expires = (datetime.utcnow() + timedelta(days=TOKEN_VALID_DAYS)).isoformat()
    with database.db_cursor() as cur:
        cur.execute(
            "INSERT OR REPLACE INTO auth_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires),
        )
    return token


def validate_token(token: str) -> Optional[dict]:
    """验证 Token，返回用户信息字典（含 id/username/role/region/display_name）或 None。"""
    if not token:
        return None
    with database.db_cursor() as cur:
        cur.execute(
            """SELECT u.id, u.username, u.role, u.region, u.display_name, t.expires_at
               FROM auth_tokens t JOIN users u ON u.id = t.user_id
               WHERE t.token = ?""",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["expires_at"] < datetime.utcnow().isoformat():
            cur.execute("DELETE FROM auth_tokens WHERE token=?", (token,))
            return None
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "region": row["region"],
        "display_name": row["display_name"],
    }


def delete_token(token: str) -> None:
    """登出：删除 Token。"""
    with database.db_cursor() as cur:
        cur.execute("DELETE FROM auth_tokens WHERE token=?", (token,))


# ── FastAPI 中间件：从 Cookie 读取 Token 并注入 request.state.user ──

async def auth_middleware(request: Request, call_next):
    """将认证用户信息注入 request.state.user；未登录时为 None。
    不在此处拦截——由各路由的依赖函数按需检查权限。"""
    token = request.cookies.get(COOKIE_NAME, "")
    user = validate_token(token) if token else None
    request.state.user = user
    return await call_next(request)


# ── 依赖：要求已登录 ──

def require_user(request: Request) -> dict:
    """FastAPI 依赖：返回当前用户，未登录抛 401。"""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "请先登录")
    return user


def try_get_user(request: Request):
    """尝试获取当前用户，未登录返回 None（不抛异常）。"""
    return getattr(request.state, "user", None)


def require_role(*roles: str):
    """FastAPI 依赖工厂：要求用户属于指定角色之一。"""

    def checker(request: Request) -> dict:
        user = require_user(request)
        if user["role"] not in roles:
            raise HTTPException(403, "权限不足")
        return user

    return checker


def require_admin(request: Request) -> dict:
    return require_role("admin")(request)


def require_manager(request: Request) -> dict:
    return require_role("admin", "manager")(request)
