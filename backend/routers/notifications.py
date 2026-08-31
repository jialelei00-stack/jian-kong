"""消息通知路由 —— 推送 + 查询"""

import json as _json
from fastapi import APIRouter, Request
import auth as auth_service
from database import db_cursor

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_notifications(req: Request):
    """获取当前用户的未读通知。"""
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        conditions = ["user_id = ?"]
        params = [user["id"]]

        # 广播给该角色的（不限区域）
        if user.get("role"):
            conditions.append("(user_id IS NULL AND target_role = ? AND target_region IS NULL)")
            params.append(user["role"])

        # 广播给该区域特定角色的（需要角色+区域同时匹配）
        if user.get("role") and user.get("region"):
            region_big = user["region"].split("/")[0].strip()
            conditions.append("(user_id IS NULL AND target_role = ? AND TRIM(target_region) = ?)")
            params.extend([user["role"], region_big])

        where = " OR ".join(conditions)
        cur.execute(
            f"SELECT id, type, icon, text, read, created_at FROM notifications WHERE ({where}) AND read = 0 ORDER BY created_at DESC LIMIT 50",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"ok": True, "data": rows}


@router.post("/push")
async def push_notification(req: Request):
    """推送一条通知。"""
    user = auth_service.require_user(req)
    try:
        body = await req.json()
    except Exception:
        body = {}

    ntype = body.get("type", "info")
    icon_map = {"manual_review": "⚠️", "approved": "✅", "rejected": "❌", "info": "📌"}
    icon = body.get("icon", icon_map.get(ntype, "📌"))
    text = body.get("message", body.get("text", ""))
    target_role = body.get("target_role", "")
    target_region = body.get("target_region", "")
    target_user_id = body.get("user_id", None)

    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO notifications (user_id, target_role, target_region, type, icon, text) VALUES (?,?,?,?,?,?)",
            (target_user_id, target_role or None, target_region or None, ntype, icon, text),
        )
    return {"ok": True}


@router.put("/read-all")
def mark_all_read(req: Request):
    """全部已读。"""
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        # 标记个人通知
        cur.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (user["id"],))
        # 标记广播通知
        if user.get("role"):
            cur.execute(
                "UPDATE notifications SET read = 1 WHERE user_id IS NULL AND target_role = ? AND target_region IS NULL",
                (user["role"],),
            )
        if user.get("role") and user.get("region"):
            region_big = user["region"].split("/")[0].strip()
            cur.execute(
                "UPDATE notifications SET read = 1 WHERE user_id IS NULL AND target_role = ? AND TRIM(target_region) = ?",
                (user["role"], region_big),
            )
    return {"ok": True}
