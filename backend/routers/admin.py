"""管理员路由：用户管理、使用日志。"""

from fastapi import APIRouter, Request, HTTPException

import auth as auth_service
import database
import runtime_utils
from runtime_utils import REGIONS as REGION_HIERARCHY
from database import db_cursor

router = APIRouter(prefix="/api/users", tags=["管理员"])

# 使用日志路由 (admin only)
usage_router = APIRouter(prefix="/api/usage-log", tags=["使用日志"])

# 区域层级统一取自 runtime_utils.REGIONS（与前端、写入端规范化同一份）

# 管理动作类日志：账号增删改 + 自助注册 + 删提交 + 规则修订后的状态重算。
# 这些动作过去完全不留痕，导致「怎么平白多出来使用人 / 记录被谁删了」无法追查。
ADMIN_ACTIONS = ("新增账号", "修改账号", "删除账号", "注册账号", "删除提交", "批量补建档", "状态重算")


def _operator(user) -> str:
    """操作人展示名（退而用用户名），写日志用。"""
    if not user:
        return ""
    return user.get("display_name") or user.get("username") or ""


# 允许的角色：超管 / 在地营销经理（大区）/ 区域经理（大区+小区）
ALLOWED_ROLES = ("admin", "manager", "uploader")


def _pwd_ok(p: str) -> bool:
    """与管理端自助改密同一套规则：≥8 位且同时含字母和数字。"""
    return bool(p) and len(p) >= 8 and any(c.isalpha() for c in p) and any(c.isdigit() for c in p)


def _admin_count(cur) -> int:
    cur.execute("SELECT COUNT(*) AS c FROM users WHERE role='admin'")
    return cur.fetchone()["c"]


@router.get("")
def list_users(req: Request):
    """管理员查看所有用户。"""
    auth_service.require_admin(req)
    with db_cursor() as cur:
        cur.execute("SELECT id, username, display_name, role, region, created_at FROM users ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


@router.post("")
def create_user(payload: dict, req: Request):
    """管理员创建用户。"""
    admin = auth_service.require_admin(req)
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    role = (payload.get("role") or "uploader").strip()
    if role not in ALLOWED_ROLES:
        raise HTTPException(400, "角色只能是 admin / manager / uploader")
    if not _pwd_ok(password):
        raise HTTPException(400, "密码至少 8 位，且需同时包含字母和数字")
    region_norm = runtime_utils.normalize_region(payload.get("region"))
    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        if cur.fetchone():
            raise HTTPException(409, "用户名已存在")
        h = auth_service.hash_password(password)
        cur.execute(
            "INSERT INTO users (username, password_hash, display_name, role, region) VALUES (?,?,?,?,?)",
            (username, h, payload.get("display_name") or username, role, region_norm),
        )
        _new_id = cur.lastrowid
    database.log_usage(
        "新增账号", _operator(admin), region_norm,
        runtime_utils.client_ip(req), _new_id,
        f"username={username}; role={role}; "
        f"display_name={payload.get('display_name') or username}",
    )
    return {"id": _new_id}


@router.put("/{user_id}")
def update_user(user_id: int, payload: dict, req: Request):
    """管理员修改用户信息：显示名 / 角色 / 区域（授权范围）/ 重置密码。"""
    admin = auth_service.require_admin(req)
    new_role = payload.get("role")
    if new_role is not None and new_role not in ALLOWED_ROLES:
        raise HTTPException(400, "角色只能是 admin / manager / uploader")
    new_pwd = payload.get("password")
    if new_pwd and not _pwd_ok(new_pwd):
        raise HTTPException(400, "密码至少 8 位，且需同时包含字母和数字")
    with db_cursor() as cur:
        cur.execute("SELECT id, role FROM users WHERE id=?", (user_id,))
        target = cur.fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")
        # 不能让最后一个超管被降级，否则后台再没人能管理账号
        if (new_role is not None and new_role != "admin"
                and target["role"] == "admin" and _admin_count(cur) <= 1):
            raise HTTPException(400, "系统至少要保留一个超级管理员，不能降级最后一个超管")
        updates, params = [], []
        for field in ("display_name", "role", "region"):
            if field in payload and payload[field] is not None:
                updates.append(f"{field}=?")
                params.append(runtime_utils.normalize_region(payload[field])
                              if field == "region" else payload[field])
        if new_pwd:
            updates.append("password_hash=?")
            params.append(auth_service.hash_password(new_pwd))
        if updates:
            params.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
    database.log_usage(
        "修改账号", _operator(admin), "", runtime_utils.client_ip(req), user_id,
        f"user_id={user_id}; fields={','.join(sorted(set(payload) - {'password'}))}"
        + ("; password=已重置" if payload.get("password") else ""),
    )
    return {"ok": True}


@router.delete("/{user_id}")
def delete_user(user_id: int, req: Request):
    """管理员删除用户。"""
    user = auth_service.require_admin(req)
    if user["id"] == user_id:
        raise HTTPException(400, "不能删除自己")
    # 先取快照：删除后就没法知道被删的是谁了（账号增删此前完全没留痕，
    # 事后追查「这个使用人哪来的/谁删的」都查不到）。
    with db_cursor() as cur:
        cur.execute("SELECT username, display_name, role, region FROM users WHERE id=?",
                    (user_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    with db_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    database.log_usage(
        "删除账号", _operator(user), row["region"] or "", runtime_utils.client_ip(req),
        user_id,
        f"username={row['username']}; role={row['role']}; "
        f"display_name={row['display_name'] or ''}",
    )
    return {"ok": True}


@router.get("/../regions")
def list_regions():
    """公共：返回大区→细分区域列表。"""
    return REGION_HIERARCHY


# ── 使用日志 API ──
@usage_router.get("")
def list_usage_log(req: Request, page: int = 1, page_size: int = 30, action_type: str = ""):
    """管理员查看使用日志。

    action_type：upload=上传记录；review=审核记录；admin=管理操作记录
    （账号新增/修改/删除、自助注册、删除提交）。
    """
    auth_service.require_admin(req)
    offset = (page - 1) * page_size
    where, params = [], []
    if action_type == "upload":
        where.append("action = 'upload'")
    elif action_type == "review":
        where.append("action IN ('复核通过', '复核驳回')")
    elif action_type == "admin":
        where.append("action IN (" + ",".join("?" * len(ADMIN_ACTIONS)) + ")")
        params.extend(ADMIN_ACTIONS)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM usage_log {where_clause}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM usage_log {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        items = [dict(r) for r in cur.fetchall()]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
