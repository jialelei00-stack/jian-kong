"""管理员路由：用户管理、使用日志。"""

from fastapi import APIRouter, Request, HTTPException

import auth as auth_service
from database import db_cursor

router = APIRouter(prefix="/api/users", tags=["管理员"])

# 使用日志路由 (admin only)
usage_router = APIRouter(prefix="/api/usage-log", tags=["使用日志"])

# 与前端一致的区域层级
REGION_HIERARCHY = {
    "华东区": ["安徽AB区", "安徽CD区", "江苏AB区", "上海市", "苏北CD区", "苏南CD区", "浙东区", "浙西区"],
    "华北区": ["北京市", "河北AB区", "河北CD区", "河南AB区", "山东AB区", "山西省", "天津市", "豫北CD区", "豫南CD区", "鲁东CD区", "鲁西CD区"],
    "华南区": ["海南省", "湖北AB区", "湖北CD区", "湖南AB区", "湖南CD区", "江西省", "闽北区", "闽南区", "粤东A区", "粤东B区", "粤西A区", "粤西B区"],
    "东北区": ["黑龙江省", "吉林省", "辽北区", "辽南区", "内蒙古"],
    "西一区": ["甘青宁藏", "陕西AB区", "陕西CD区", "四川AB区", "四川CD区", "新疆省", "重庆AB区", "重庆CD区"],
    "西二区": ["贵州省", "桂北区", "桂南区", "桂中区", "云南省"],
}


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
    auth_service.require_admin(req)
    username = (payload.get("username") or "").strip()
    password = (payload.get("password") or "").strip()
    if not username or not password:
        raise HTTPException(400, "用户名和密码不能为空")
    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE username=?", (username,))
        if cur.fetchone():
            raise HTTPException(409, "用户名已存在")
        h = auth_service.hash_password(password)
        cur.execute(
            "INSERT INTO users (username, password_hash, display_name, role, region) VALUES (?,?,?,?,?)",
            (username, h, payload.get("display_name") or username,
             payload.get("role") or "uploader", payload.get("region")),
        )
        return {"id": cur.lastrowid}


@router.put("/{user_id}")
def update_user(user_id: int, payload: dict, req: Request):
    """管理员修改用户信息。"""
    auth_service.require_admin(req)
    with db_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE id=?", (user_id,))
        if not cur.fetchone():
            raise HTTPException(404, "用户不存在")
        updates, params = [], []
        for field in ("display_name", "role", "region"):
            if field in payload:
                updates.append(f"{field}=?")
                params.append(payload[field])
        if "password" in payload and payload["password"]:
            updates.append("password_hash=?")
            params.append(auth_service.hash_password(payload["password"]))
        if updates:
            params.append(user_id)
            cur.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
    return {"ok": True}


@router.delete("/{user_id}")
def delete_user(user_id: int, req: Request):
    """管理员删除用户。"""
    user = auth_service.require_admin(req)
    if user["id"] == user_id:
        raise HTTPException(400, "不能删除自己")
    with db_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


@router.get("/../regions")
def list_regions():
    """公共：返回大区→细分区域列表。"""
    return REGION_HIERARCHY


# ── 使用日志 API ──
@usage_router.get("")
def list_usage_log(req: Request, page: int = 1, page_size: int = 30, action_type: str = ""):
    """管理员查看使用日志。action_type=upload 只看上传记录，review 只看审核记录。"""
    auth_service.require_admin(req)
    offset = (page - 1) * page_size
    where, params = [], []
    if action_type == "upload":
        where.append("action = 'upload'")
    elif action_type == "review":
        where.append("action IN ('复核通过', '复核驳回')")
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM usage_log {where_clause}")
        total = cur.fetchone()["c"]
        cur.execute(
            f"SELECT * FROM usage_log {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [page_size, offset],
        )
        items = [dict(r) for r in cur.fetchall()]
    return {"items": items, "total": total, "page": page, "page_size": page_size}
