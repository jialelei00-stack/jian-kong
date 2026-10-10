"""认证路由：登录、注册、登出、获取当前用户。"""

import re
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse

import auth as auth_service
import runtime_utils
from models import RegisterIn, LoginIn, GuestIn, ChangePasswordIn
from database import db_cursor

router = APIRouter(prefix="/api/auth", tags=["认证"])


# ── 密码强度校验（R-09） ──
# 仅约束"新"密码：注册、改密时校验。已有用户不受影响。
def _pwd_strength_ok(p: str) -> bool:
    """≥8 位，且同时含字母与数字。"""
    if not p or len(p) < 8:
        return False
    if not re.search(r"[A-Za-z]", p):
        return False
    if not re.search(r"[0-9]", p):
        return False
    return True


@router.post("/register")
def register(payload: RegisterIn, req: Request):
    """注册通道已关闭：账号统一由管理员在「后台 → 账号管理 → 新增账号」创建。

    以前注册页是公开的，任何人填用户名/密码/区域就能拿到一个「区域经理」账号，
    后台会不断冒出说不清来源的使用人，也没法追查。现在只保留管理员创建这一条
    路径（那条路径会写「新增账号」审计日志）。
    需要重新开放自助注册时，把下面这行 403 去掉即可。
    """
    raise HTTPException(403, "注册通道已关闭，请联系管理员开通账号")


@router.post("/login")
def login(payload: LoginIn, req: Request):
    """登录：验证用户名密码，签发 Token。
    登录失败会在 usage_log 留痕（R-12），并由限速中间件按 5 次/5 分钟锁定。"""
    ip = runtime_utils.client_ip(req)
    username = (payload.username or "").strip()
    with db_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if not row or not auth_service.verify_password(payload.password, row["password_hash"]):
            # 失败留痕（R-12）+ 累计到限速器
            with db_cursor() as cur2:
                cur2.execute(
                    "INSERT INTO usage_log (action, display_name, region, ip, detail) "
                    "VALUES (?, ?, ?, ?, ?)",
                    ("login_failed", username, "",
                     ip, "用户名或密码错误"),
                )
            runtime_utils.record_login(ip, username, success=False)
            raise HTTPException(401, "用户名或密码错误")
        runtime_utils.record_login(ip, username, success=True)
        token = auth_service.create_token(row["id"])
        # 成功也留痕（便于审计）
        with db_cursor() as cur3:
            cur3.execute(
                "INSERT INTO usage_log (action, display_name, region, ip, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                ("login_ok", row["display_name"] or username, row["region"] or "",
                 ip, f"role={row['role']}"),
            )
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
def logout(req: Request):
    """登出：服务端 Token 立即失效（R-08），同时清 Cookie。"""
    token = req.cookies.get(auth_service.COOKIE_NAME, "")
    if token:
        auth_service.delete_token(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth_service.COOKIE_NAME)
    return resp


@router.post("/guest")
def guest_login(payload: GuestIn):
    """免登录入口：输入姓名+区域即可使用上传功能。"""
    display_name = payload.display_name.strip()
    region = runtime_utils.normalize_region(payload.region)
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
        if not _pwd_strength_ok(payload.new_password):
            raise HTTPException(400, "新密码至少 8 位，且需同时包含字母和数字")
        new_hash = auth_service.hash_password(payload.new_password)
        cur.execute("UPDATE users SET password_hash=? WHERE id=?", (new_hash, user["id"]))
    return {"ok": True}
