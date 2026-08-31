"""提交审核路由：上传、列表、详情、人工审批。"""

import json
import os
import secrets
import time

from fastapi import APIRouter, Request, HTTPException
from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi.responses import JSONResponse

import auth as auth_service
import queue_manager
import cos_service
from database import db_cursor

router = APIRouter(prefix="/api/submissions", tags=["submissions"])
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _fill_media_urls(media_list):
    """把 media 里的 cos_key 实时转成预签名 URL（2小时有效），供前端/AI 访问。"""
    if not media_list:
        return media_list
    for m in media_list:
        if not isinstance(m, dict):
            continue
        key = m.get("cos_key")
        if key and cos_service.is_enabled():
            m["cos_url"] = cos_service.presigned_url(key, expires=7200)
        else:
            m["cos_url"] = None
    return media_list


@router.post("/upload")
async def upload_submission(req: Request):
    """上传内容（视频/图片+文案）并加入审核队列，异步返回。
    支持大文件上传（最大 500MB/文件）。
    已登录用户直接使用身份；未登录用户须提供 display_name + region。
    """
    # 最大 500MB / 文件，最多 20 个文件
    try:
        form = await req.form(max_files=20, max_fields=50)
    except Exception as e:
        raise HTTPException(413, f"文件过大或不支持的格式: {e}")

    title = (form.get("title") or "").strip()
    caption = (form.get("caption") or "").strip()
    region = (form.get("region") or "").strip()
    display_name = (form.get("display_name") or "").strip()

    # ── 身份识别 ──
    user = auth_service.try_get_user(req)
    if user:
        try:
            auth_service.require_user(req)
        except HTTPException:
            user = None

    guest_session = None
    if not user:
        if not display_name or not region:
            raise HTTPException(401, "请先登录或提供姓名和区域")
        # 读取已有 guest session（同一浏览器复用）
        guest_session = req.cookies.get("guest_session")
        if not guest_session:
            import uuid as _uuid
            guest_session = _uuid.uuid4().hex

    files = [v for _, v in form.items() if isinstance(v, StarletteUploadFile) and v.filename]
    if not files and not caption:
        raise HTTPException(400, "请上传至少一个文件或填写文案描述")

    media_list = []
    for f in files:
        ext = os.path.splitext(f.filename or "file")[1] or ".bin"
        safe_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
        filepath = os.path.join(UPLOAD_DIR, safe_name)
        # 流式写入磁盘，避免大文件撑爆内存
        with open(filepath, "wb") as fd:
            while True:
                chunk = await f.read(8 * 1024 * 1024)  # 8MB 一块
                if not chunk:
                    break
                fd.write(chunk)
        fsize = os.path.getsize(filepath)
        is_video = ext.lower() in (".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv")

        # 上传到腾讯云 COS，按 大区/细分区域/上传者身份/内容类型/年月 分类存放
        big_region = region.split("/")[0].strip() if "/" in region else region.strip()
        sub_region = region.split("/")[1].strip() if "/" in region else "全部"
        if user:
            uploader = user.get("role", "user")  # admin / manager / uploader
            uploader_name = (user.get("display_name") or user.get("username") or "unknown").replace("/", "_")
        else:
            uploader = "guest"
            uploader_name = (display_name or "unknown").replace("/", "_")
        content_type = "video" if is_video else "image"
        ym = time.strftime("%Y-%m", time.localtime())
        cos_key = f"{big_region}/{sub_region}/{uploader}/{uploader_name}/{content_type}/{ym}/{safe_name}"
        mime = "video/mp4" if is_video else ("image/jpeg" if ext.lower() in (".jpg",".jpeg") else "image/png")
        cos_url = cos_service.upload(filepath, cos_key, mime)

        media_list.append({
            "type": "video" if is_video else "image",
            "filename": f.filename,
            "path": f"uploads/{safe_name}",
            "cos_key": cos_url,  # 存的是 cos_key，不是 URL
            "size": fsize,
            "is_video": is_video,
            "thumb": None,
            "video_local": f"uploads/{safe_name}",
        })

    with db_cursor() as cur:
        client_ip = req.client.host if req.client else ""
        cur.execute(
            "INSERT INTO submissions (user_id, display_name, guest_session, uploader_ip, title, caption, media, region, status) VALUES (?,?,?,?,?,?,?,?, 'pending')",
            (user["id"] if user else None, display_name, guest_session, client_ip, title, caption,
             json.dumps(media_list, ensure_ascii=False), region),
        )
        sub_id = cur.lastrowid
        # 写使用日志
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("upload", display_name, region, client_ip, sub_id,
             f"title={title[:60]}; caption={caption[:100]}; files={len(files)}"),
        )

    queue_manager.enqueue(sub_id)

    resp_data = {"ok": True, "submission_id": sub_id, "status": "pending",
                 "queue_position": queue_manager.queue_size()}

    resp = JSONResponse(resp_data)
    return resp


@router.get("")
def list_submissions(req: Request, page: int = 1, page_size: int = 20,
                     status: str = None, region: str = None,
                     risk_level: str = None, search: str = None):
    """列出提交（按角色权限过滤）。支持按状态/区域/风险等级/关键词搜索。
    guest 用户通过 guest_session cookie 查看自己的提交。
    """
    user = auth_service.try_get_user(req)
    client_ip = req.client.host if req.client else ""
    offset = (page - 1) * page_size
    where, params = [], []

    if user:
        if user["role"] == "uploader":
            where.append("s.user_id = ?")
            params.append(user["id"])
        elif user["role"] == "manager":
            mgr_region = user.get("region", "")
            if mgr_region:
                big = mgr_region.split("/")[0].strip() if "/" in mgr_region else mgr_region.strip()
                # 提交的 region 格式为 "华东区 / 安徽AB区"（含空格），需要兼容
                where.append("(s.region LIKE ? OR s.region LIKE ? OR TRIM(s.region) = ?)")
                params.extend([f"{big}%", f"{big}/%", big])
        # admin: 不加任何过滤，看全部提交
    elif client_ip:
        # guest：通过 IP 查看同一 IP 的提交记录
        where.append("s.uploader_ip = ?")
        params.append(client_ip)
    else:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    if status:
        where.append("s.status = ?")
        params.append(status)
    if region:
        where.append("s.region = ?")
        params.append(region)
    if risk_level:
        where.append("s.risk_level = ?")
        params.append(risk_level)
    if search:
        where.append("(s.title LIKE ? OR s.caption LIKE ? OR s.display_name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    # 始终用 LEFT JOIN：公开上传的提交 user_id 为 NULL，INNER JOIN 会丢弃
    join_clause = "LEFT JOIN users u ON u.id = s.user_id"
    count_from = "submissions s LEFT JOIN users u ON u.id = s.user_id"

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM {count_from}{where_sql}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"""SELECT s.*, u.username, u.display_name AS user_display_name, u.region AS user_region
                FROM submissions s {join_clause}
                {where_sql} ORDER BY s.id DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset])
        data = [dict(r) for r in cur.fetchall()]

    for d in data:
        d["media"] = _fill_media_urls(_parse_json(d.get("media"), []))
        d["ai_result"] = _parse_json(d.get("ai_result"))
        d["matched_rules"] = _parse_json(d.get("matched_rules"), [])
        # 对于 guest 提交，display_name 在 submission 表上；对于注册用户，用 user_display_name 覆盖
        if d.get("user_display_name") and not d.get("display_name"):
            d["display_name"] = d["user_display_name"]
        elif d.get("display_name"):
            d["region"] = d.get("region")  # submission 自带 region

    return {"items": data, "total": total, "page": page, "page_size": page_size}


@router.get("/{submission_id}")
def get_submission(submission_id: int, req: Request):
    """查看单条提交详情。支持登录用户和 guest（IP 匹配）。"""
    user = auth_service.try_get_user(req)
    client_ip = req.client.host if req.client else ""
    with db_cursor() as cur:
        cur.execute(
            """SELECT s.*, u.username, u.display_name AS user_display_name, u.region AS user_region
               FROM submissions s LEFT JOIN users u ON u.id = s.user_id
               WHERE s.id = ?""", (submission_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(403, "提交不存在或无权查看")
        sub = dict(row)
        # 权限检查
        if user:
            if user["role"] == "uploader" and sub["user_id"] and sub["user_id"] != user["id"]:
                raise HTTPException(403, "无权查看")
            # manager 和 admin 可以查看
        elif client_ip:
            # guest：通过 IP 匹配
            if sub.get("uploader_ip") != client_ip:
                raise HTTPException(403, "无权查看")
        else:
            raise HTTPException(403, "无权查看")

    sub["media"] = _fill_media_urls(_parse_json(sub.get("media"), []))
    sub["ai_result"] = _parse_json(sub.get("ai_result"))
    sub["matched_rules"] = _parse_json(sub.get("matched_rules"), [])
    # 填充 display_name
    if sub.get("user_display_name") and not sub.get("display_name"):
        sub["display_name"] = sub["user_display_name"]
    return sub


@router.put("/{submission_id}/review")
def review_submission(submission_id: int, payload: dict, req: Request):
    """人工审核：manager/admin 审批黄灯内容。"""
    user = auth_service.require_manager(req)
    action = (payload.get("action") or "").strip()
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action 必须是 approve 或 reject")

    with db_cursor() as cur:
        cur.execute("SELECT id, title, display_name, region FROM submissions WHERE id=?", (submission_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "提交不存在")
        sub_info = dict(row)
        new_status = "approved" if action == "approve" else "rejected"
        new_risk = "none" if action == "approve" else "forbidden"
        action_label = "复核通过" if action == "approve" else "复核驳回"
        cur.execute(
            """UPDATE submissions SET status=?, risk_level=?,
               review_comment=?, reviewed_by=?, reviewed_at=datetime('now','localtime')
               WHERE id=?""",
            (new_status, new_risk, payload.get("comment"), user["id"], submission_id),
        )
        # 写审核日志
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            (action_label, user.get("display_name") or user.get("username", ""),
             sub_info.get("region", ""), req.client.host if req.client else "",
             submission_id,
             f"审核人: {user.get('display_name') or user.get('username','')}; 标题: {sub_info.get('title','')}; 备注: {payload.get('comment') or '无'}"),
        )
    return {"ok": True, "status": new_status}


@router.delete("/{submission_id}")
def delete_submission(submission_id: int, req: Request):
    """删除提交（上传者本人或 admin 可删除），同时清理上传文件。"""
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))
        sub = cur.fetchone()
        if not sub:
            raise HTTPException(404, "提交不存在")
        sub = dict(sub)
        # 权限：上传者本人 或 admin
        if user["role"] != "admin" and sub["user_id"] != user["id"]:
            raise HTTPException(403, "无权删除")
        # 删除关联的上传文件
        try:
            media = json.loads(sub["media"]) if sub.get("media") else []
        except Exception:
            media = []
        for m in media:
            fp = m.get("path", "") if isinstance(m, dict) else ""
            if fp:
                abs_path = os.path.join(UPLOAD_DIR, os.path.basename(fp))
                try:
                    if os.path.exists(abs_path):
                        os.remove(abs_path)
                except Exception:
                    pass
        cur.execute("DELETE FROM submissions WHERE id=?", (submission_id,))
    return {"ok": True}


def _parse_json(val, default=None):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val if val is not None else default
