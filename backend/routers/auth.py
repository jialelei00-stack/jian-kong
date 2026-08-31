"""认证路由：登录、注册、登出、获取当前用户。"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

import auth as auth_service
from models import RegisterIn, LoginIn, GuestIn, ChangePasswordIn
from database import db_cursor

router = APIRouter(prefix="/api/auth", tags=["认证"])


@router.post("/register")
def register(payload: RegisterIn):
    """注册新用户（默认角色 uploader）。"""
    if not payload.username.strip() or not payload.password.strip():
        raise HTTPException(400, "用户名和密码不能为空")
    if len(payload.password) < 4:
        raise HTTPException(400, "密码至少4位")
    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=?", (payload.username.strip(),))
        if cur.fetchone():
            raise HTTPException(409, "用户名已存在")
        h = auth_service.hash_password(payload.password)
        cur.execute(
            "INSERT INTO users (username, password_hash, display_name, role, region) VALUES (?,?,?,?,?)",
            (payload.username.strip(), h, payload.display_name or payload.username, "uploader", payload.region),
        )
        uid = cur.lastrowid
    token = auth_service.create_token(uid)
    resp = JSONResponse({"ok": True, "user_id": uid, "token": token})
    resp.set_cookie(
        auth_service.COOKIE_NAME, token,
        httponly=True, max_age=86400 * auth_service.TOKEN_VALID_DAYS, samesite="lax"
    )
    return resp


@router.post("/login")
def login(payload: LoginIn):
    """登录：验证用户名密码，签发 Token。"""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=?", (payload.username.strip(),))
        row = cur.fetchone()
        if not row or not auth_service.verify_password(payload.password, row["password_hash"]):
            raise HTTPException(401, "用户名或密码错误")
        token = auth_service.create_token(row["id"])
    resp = JSONResponse({
        "ok": True,
        "user": {
            "id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"],
            "region": row["region"],
        },
    })
    resp.set_cookie(
        auth_service.COOKIE_NAME, token,
        httponly=True, max_age=86400 * auth_service.TOKEN_VALID_DAYS, samesite="lax"
    )
    return resp


@router.get("/me")
def get_current_user(req: Request):
    """获取当前登录用户信息。"""
    return auth_service.require_user(req)


@router.post("/logout")
def logout():
    """登出：清除 Token。"""
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_service.COOKIE_NAME)
    return resp


@router.post("/guest")
def guest_login(payload: GuestIn):
    """免登录入口：输入姓名+区域即可使用上传功能。"""
    display_name = payload.display_name.strip()
    region = payload.region.strip()
    if not display_name:
        raise HTTPException(400, "请输入您的姓名")
    if not region:
        raise HTTPException(400, "请选择所属区域")

    # 查找或创建 guest 用户（按 display_name + region 去重）
    with db_cursor() as cur:
        cur.execute(
            "SELECT id FROM users WHERE display_name=? AND region=? AND role='uploader'",
            (display_name, region),
        )
        row = cur.fetchone()
        if row:
            uid = row["id"]
        else:
            import secrets as _sec
            guest_user = f"guest_{_sec.token_hex(6)}"
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, role, region) VALUES (?,?,?,?,?)",
                (guest_user, "", display_name, "uploader", region),
            )
            uid = cur.lastrowid

    token = auth_service.create_token(uid)
    resp = JSONResponse({
        "ok": True,
        "user": {
            "id": uid, "username": "guest",
            "display_name": display_name, "role": "uploader",
            "region": region,
        },
    })
    resp.set_cookie(
        auth_service.COOKIE_NAME, token,
        httponly=True, max_age=86400 * auth_service.TOKEN_VALID_DAYS, samesite="lax",
    )
    return resp


@router.put("/change-password")
def change_password(payload: ChangePasswordIn, req: Request):
    """当前登录用户修改自己的密码。"""
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        cur.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "用户不存在")
        if not auth_service.verify_password(payload.old_password, row["password_hash"]):
            raise HTTPException(400, "旧密码错误")
        if len(payload.new_password) < 4:
            raise HTTPException(400, "新密码至少4位")
        new_hash = auth_service.hash_password(payload.new_password)
        cur.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, user["id"]))
    return {"ok": True}
