"""双百战役路由：两段式上传、评分、战功、四大榜单、反作弊、导出。

业务闭环：
  1. 首次上传建档（合规预检，不结算不上榜）
  2. T+7 二次提交 → 门槛校验 + AI 情理色诚打分 + 留存/互动区间算分 → 总分/等级/战功
  3. 刷新四大榜单；总部反作弊抽检；支持人工改分
"""

import csv
import io
import json
import os
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response
from starlette.datastructures import UploadFile as StarletteUploadFile

import auth as auth_service
import rules_engine
import ai_auditor
import campaign_scoring
import cos_service
import database
from database import db_cursor
from models import (
    CampaignWorkIn, CampaignMetricsIn, CampaignScoreOverrideIn,
    CampaignCredentialIn, CampaignPropagationIn, CampaignAuditIn,
)

router = APIRouter(prefix="/api/campaign", tags=["双百战役"])

_PLAY_THRESHOLD = 0     # 自然播放门槛（临时放宽为 0 便于测试，正式上线恢复为 500）
_MIN_DAYS = 0           # 发布满 7 天（临时放宽为 0 便于测试，正式上线恢复为 7）


# ── 工具函数 ──

def _json_dump(obj):
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


def _json_load(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _gen_code() -> str:
    """生成一稿一码唯一 ID。"""
    return campaign_scoring.gen_code()


def _compliance_check(caption: str):
    """复用规则引擎做合规预检。返回 (ok, problems)。"""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM rules WHERE enabled=1")
        rules = [dict(r) for r in cur.fetchall()]
    content = {"caption": caption or "", "transcript": "", "ocr_text": ""}
    result = rules_engine.analyze_content(content, rules)
    matched = result.get("matched_rules") or []
    problems = [m for m in matched if m.get("severity") in ("forbidden", "high")]
    return (len(problems) == 0), result, problems


def _days_since(created_at: str) -> int:
    try:
        dt = datetime.fromisoformat(created_at)
    except Exception:
        try:
            dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0
    return (datetime.now() - dt).days


def _cred_url(file: str) -> str:
    """凭证 file 字段若是 COS key，实时转预签名 URL 供前端预览；否则原样返回。"""
    if not file:
        return ""
    if file.startswith("http"):
        return file
    if cos_service.is_enabled() and ("/" in file):
        try:
            u = cos_service.presigned_url(file, expires=7200)
            if u:
                return u
        except Exception:
            pass
    return file


def _fill_media_urls(media_list):
    """给作品素材补充可访问的展示 URL（COS 预签名 / 本地 HTTP 兜底），供详情弹窗预览。"""
    if not media_list:
        return media_list
    base_url = os.environ.get("SERVER_BASE_URL", "http://127.0.0.1:8000")
    for m in media_list:
        if not isinstance(m, dict):
            continue
        key = m.get("cos_key")
        if key and cos_service.is_enabled():
            try:
                m["cos_url"] = cos_service.presigned_url(key, expires=7200)
            except Exception:
                m["cos_url"] = None
        elif not m.get("cos_url"):
            m["cos_url"] = None
        if not m.get("cos_url"):
            local = m.get("video_local") or m.get("path") or ""
            if local:
                name = os.path.basename(local)
                m["local_url"] = f"{base_url}/uploads/{name}" if "uploads" in local else f"{base_url}/media/{name}"
    return media_list


def _region_scope(user):
    """根据用户角色返回区域过滤 SQL 片段与参数。
    admin 看全量；其他角色只看本战区（大区）。"""
    if user and user.get("role") == "admin":
        return "", []
    big = (user.get("region") or "").split("/")[0].strip()
    if big:
        return " AND w.region LIKE ?", [f"{big}%"]
    return " AND 1=0", []


def _serialize_work(row: dict) -> dict:
    """把 works 行 + metrics + scores 组装成完整详情。"""
    work_id = row["id"]
    out = dict(row)
    out["media"] = _fill_media_urls(_json_load(out.get("media"), []))
    out["extra_flags"] = _json_load(out.get("extra_flags"), {})
    with db_cursor() as cur:
        cur.execute("SELECT * FROM campaign_metrics WHERE work_id=?", (work_id,))
        m = cur.fetchone()
        out["metrics"] = dict(m) if m else None
        cur.execute("SELECT * FROM campaign_scores WHERE work_id=?", (work_id,))
        s = cur.fetchone()
        if s:
            sd = dict(s)
            sd["ai_dims"] = _json_load(sd.get("ai_dims"), {})
            sd["retention_detail"] = _json_load(sd.get("retention_detail"), {})
            sd["interaction_detail"] = _json_load(sd.get("interaction_detail"), {})
            sd["extra_merit"] = _json_load(sd.get("extra_merit"), {})
            out["scores"] = sd
        else:
            out["scores"] = None
        cur.execute("SELECT * FROM campaign_credentials WHERE work_id=? ORDER BY id", (work_id,))
        out["credentials"] = []
        for cr in cur.fetchall():
            cd = dict(cr)
            cd["url"] = _cred_url(cd.get("file"))
            out["credentials"].append(cd)
        cur.execute("SELECT * FROM campaign_propagation WHERE work_id=? ORDER BY id", (work_id,))
        out["propagation"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM campaign_audit_log WHERE work_id=? ORDER BY id DESC", (work_id,))
        out["audit_log"] = [dict(r) for r in cur.fetchall()]
    return out


def _get_work(cur, work_id: int):
    cur.execute("SELECT * FROM campaign_works WHERE id=?", (work_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "作品不存在")
    return dict(row)


def _create_campaign_work(creator_id, creator_name, track, title, url, media,
                          region, submission_id=None, extra_flags=None) -> dict:
    """统一的作品建档逻辑：合规预检 + 建档 + 首次 AI 内容表现分。

    供 /api/campaign/works（手动建档）与 /api/submissions/upload（上传建档）
    两个入口复用，避免重复写库与字段不一致。
    """
    caption = (title or "").strip()
    media_list = media if isinstance(media, list) else []
    compliance_ok, _, problems = _compliance_check(caption)
    status = "pending_t7" if compliance_ok else "pending_review"
    reject_reason = ("" if compliance_ok else ("合规预检命中：" + "、".join(p["rule_name"] for p in problems)))

    code = _gen_code()
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_works
               (code, creator_id, creator_name, track, title, url, media, region, submission_id,
                stage, status, qualified, reject_reason, publish_time, extra_flags)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                code, creator_id, creator_name, track, title, url, _json_dump(media_list),
                region, submission_id, "first", status, 0, reject_reason,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"), _json_dump(extra_flags or {}),
            ),
        )
        work_id = cur.lastrowid

    # 首次上传即出内容表现分（AI 情理色诚 0-40），不用等到 T+7 回填。
    # AI 未就绪时留空，T+7 回填时再补算，避免把 0 分固化。
    ai_quality = campaign_scoring.ai_score_content_quality(caption, media_list)
    if ai_quality.get("error") is None:
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO campaign_scores (work_id, ai_content_score, ai_dims, ai_reason)
                   VALUES (?,?,?,?)""",
                (
                    work_id,
                    ai_quality.get("score"),
                    _json_dump(ai_quality.get("dims") or {}),
                    ai_quality.get("reason") or "",
                ),
            )

    return {
        "id": work_id,
        "code": code,
        "compliance_ok": compliance_ok,
        "compliance_problems": problems,
        "status": status,
        "ai_content_score": ai_quality.get("score") if ai_quality.get("error") is None else None,
    }


# ── 首次上传建档 ──

@router.post("/works")
def create_work(payload: CampaignWorkIn, req: Request):
    auth_service.require_user(req)
    track = (payload.track or "").strip()
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if track not in valid_tracks:
        raise HTTPException(400, f"赛道不合法，可选：{' / '.join(valid_tracks)}")

    return _create_campaign_work(
        creator_id=payload.creator_id,
        creator_name=payload.creator_name,
        track=track,
        title=payload.title,
        url=payload.url,
        media=payload.media,
        region=payload.region,
        extra_flags=payload.extra_flags,
    )


# ── 作品列表（筛选：大区/赛道/等级/创作者）──

@router.get("/works")
def list_works(req: Request, region: str = "", track: str = "", grade: str = "",
               creator: str = "", status: str = ""):
    user = auth_service.require_user(req)
    where, params = ["1=1"], []
    # 投稿人只能看自己的作品；管理员看全量；区域负责人看本战区
    if user.get("role") == "uploader":
        where.append("w.creator_id=?")
        params.append(user["id"])
    else:
        scope_sql, scope_params = _region_scope(user)
        if scope_sql:
            where.append(scope_sql.lstrip(" AND ").strip())
            params.extend(scope_params)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    if track:
        where.append("w.track=?")
        params.append(track)
    if creator:
        where.append("(w.creator_name LIKE ? OR u.display_name LIKE ? OR u.username LIKE ?)")
        params += [f"%{creator}%", f"%{creator}%", f"%{creator}%"]
    if status:
        where.append("w.status=?")
        params.append(status)
    if grade:
        where.append("s.grade=?")
        params.append(grade)

    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.*, s.total_score, s.grade, s.total_merit, s.manual_total_score, s.ai_content_score, s.ai_reason
                FROM campaign_works w
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                LEFT JOIN users u ON u.id = w.creator_id
                WHERE {' AND '.join(where)}
                ORDER BY w.id DESC""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    return rows


# ── 按一稿一码定位作品（投稿人回传 T+7 用）──

@router.get("/works/by-code/{code}")
def get_work_by_code(code: str, req: Request):
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        cur.execute("SELECT * FROM campaign_works WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "未找到该一稿一码对应的作品")
        if user.get("role") == "uploader" and row["creator_id"] != user["id"]:
            raise HTTPException(403, "无权查看该作品（非本人作品）")
    return _serialize_work(row)


# ── 按姓名查询待回传作品（投稿人免登录回传 T+7 用）──

@router.get("/pending-by-name")
def pending_by_name(req: Request, name: str = "", region: str = ""):
    """按创作者姓名（可带区域）查询「待 T+7 回传」的作品，返回一稿一码列表。"""
    name = (name or "").strip()
    if not name:
        return []
    where, params = ["w.status='pending_t7'", "w.creator_name LIKE ?"], [f"%{name}%"]
    region = (region or "").strip()
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.id, w.code, w.title, w.track, w.creator_name, w.region, w.status
                FROM campaign_works w WHERE {' AND '.join(where)} ORDER BY w.id DESC LIMIT 50""",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


# ── 作品详情 ──

@router.get("/works/{work_id}")
def get_work(work_id: int, req: Request):
    auth_service.require_user(req)
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
    return _serialize_work(row)


# ── T+7 二次提交 → 自动打分 ──

def _do_submit_metrics(work_id: int, payload: CampaignMetricsIn) -> dict:
    """执行 T+7 提交打分（不含权限校验），返回打分结果。"""
    with db_cursor() as cur:
        row = _get_work(cur, work_id)

    # 门槛校验
    reject_reasons = []
    if not payload.original:
        reject_reasons.append("非原创/有效二创")
    plays = payload.plays or 0
    if plays < _PLAY_THRESHOLD:
        reject_reasons.append(f"自然播放 {plays} < {_PLAY_THRESHOLD}")
    if _days_since(row["publish_time"] or row["created_at"]) < _MIN_DAYS:
        reject_reasons.append(f"发布未满 {_MIN_DAYS} 天")
    if row["reject_reason"]:
        reject_reasons.append("合规预检未通过")
    qualified = (len(reject_reasons) == 0)

    # 保存指标
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_metrics
               (work_id, plays, retention_3s, completion_rate, deep_interaction_rate, like_rate,
                screenshot, business_proof, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET
                 plays=excluded.plays, retention_3s=excluded.retention_3s,
                 completion_rate=excluded.completion_rate,
                 deep_interaction_rate=excluded.deep_interaction_rate,
                 like_rate=excluded.like_rate, screenshot=excluded.screenshot,
                 business_proof=excluded.business_proof, submitted_at=excluded.submitted_at""",
            (
                work_id, payload.plays, payload.retention_3s, payload.completion_rate,
                payload.deep_interaction_rate, payload.like_rate,
                _json_dump(payload.screenshot), _json_dump(payload.business_proof),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        # 凭证联动：T+7 提交的截图/业务凭证自动写入凭证库（先清旧再插入，避免重复）
        cur.execute(
            "DELETE FROM campaign_credentials WHERE work_id=? AND type IN ('screenshot','business')",
            (work_id,),
        )
        for ctype, val in (("screenshot", payload.screenshot), ("business", payload.business_proof)):
            if not val:
                continue
            items = val if isinstance(val, list) else [val]
            for it in items:
                if not it:
                    continue
                if isinstance(it, dict):
                    f = it.get("file") or it.get("url") or ""
                    n = it.get("note") or "T+7 提交"
                else:
                    f = str(it)
                    n = "T+7 提交"
                if f:
                    cur.execute(
                        "INSERT INTO campaign_credentials (work_id, type, file, note) VALUES (?,?,?,?)",
                        (work_id, ctype, f, n),
                    )

    if not qualified:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE campaign_works SET stage='second', status='eliminated', qualified=0, reject_reason=?, updated_at=? WHERE id=?",
                ("；".join(reject_reasons), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
            )
        return {"qualified": False, "reject_reasons": reject_reasons, "status": "eliminated"}

    # AI 情理色诚打分（0-40）：优先复用第一次上传后已打的分，避免 T+7 重复请求
    with db_cursor() as cur:
        cur.execute(
            "SELECT ai_content_score, ai_dims, ai_reason FROM campaign_scores WHERE work_id=?",
            (work_id,),
        )
        existing = cur.fetchone()
    if existing and existing["ai_content_score"] is not None:
        ai_quality = {
            "score": existing["ai_content_score"],
            "dims": _json_load(existing["ai_dims"], {}),
            "reason": existing["ai_reason"] or "",
            "error": None,
        }
    else:
        media_list = _json_load(row.get("media"), [])
        ai_quality = campaign_scoring.ai_score_content_quality(
            (row.get("title") or "") or "", media_list
        )

    # 计算留存 / 互动 / 总分 / 等级 / 战功
    metrics = {
        "retention_3s": payload.retention_3s,
        "completion_rate": payload.completion_rate,
        "deep_interaction_rate": payload.deep_interaction_rate,
        "like_rate": payload.like_rate,
    }
    full = campaign_scoring.calc_full_score(metrics, ai_quality)
    extra_flags = _json_load(row.get("extra_flags"), {})
    total_merit, merit_detail = campaign_scoring.calc_total_merit(full["grade"], extra_flags)

    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_scores
               (work_id, ai_content_score, ai_dims, ai_reason, retention_score, retention_detail,
                interaction_score, interaction_detail, total_score, grade, base_merit, extra_merit, total_merit)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET
                 ai_content_score=excluded.ai_content_score, ai_dims=excluded.ai_dims,
                 ai_reason=excluded.ai_reason, retention_score=excluded.retention_score,
                 retention_detail=excluded.retention_detail, interaction_score=excluded.interaction_score,
                 interaction_detail=excluded.interaction_detail, total_score=excluded.total_score,
                 grade=excluded.grade, base_merit=excluded.base_merit,
                 extra_merit=excluded.extra_merit, total_merit=excluded.total_merit""",
            (
                work_id, full["ai_content_score"], _json_dump(full["ai_dims"]), full["ai_reason"],
                full["retention_score"], _json_dump(full["retention_detail"]),
                full["interaction_score"], _json_dump(full["interaction_detail"]),
                full["total_score"], full["grade"], merit_detail["base"],
                _json_dump(merit_detail), merit_detail["total"],
            ),
        )
        cur.execute(
            "UPDATE campaign_works SET stage='second', status='qualified', qualified=1, reject_reason='', updated_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
        )

    return {
        "qualified": True,
        "status": "qualified",
        "score": {
            "ai_content_score": full["ai_content_score"],
            "ai_dims": full["ai_dims"],
            "ai_reason": full["ai_reason"],
            "retention_score": full["retention_score"],
            "interaction_score": full["interaction_score"],
            "total_score": full["total_score"],
            "grade": full["grade"],
            "total_merit": merit_detail["total"],
        },
    }


@router.post("/works/{work_id}/submit")
def submit_metrics(work_id: int, payload: CampaignMetricsIn, req: Request):
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
    # 投稿人只能提交自己的作品
    if user.get("role") == "uploader" and row.get("creator_id") != user["id"]:
        raise HTTPException(403, "无权提交该作品（非本人作品）")
    return _do_submit_metrics(work_id, payload)


@router.post("/submit-by-code")
def submit_by_code(payload: dict, req: Request):
    """投稿人免登录回传 T+7：按一稿一码 + 姓名校验后提交打分。"""
    code = (payload.get("code") or "").strip()
    name = (payload.get("name") or "").strip()
    if not code or not name:
        raise HTTPException(400, "缺少一稿一码或姓名")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM campaign_works WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "未找到该一稿一码对应的作品")
        if (row["creator_name"] or "").strip() != name:
            raise HTTPException(403, "姓名与作品创作者不符，无法回传")
    metrics = CampaignMetricsIn(
        plays=payload.get("plays"),
        retention_3s=payload.get("retention_3s"),
        completion_rate=payload.get("completion_rate"),
        deep_interaction_rate=payload.get("deep_interaction_rate"),
        like_rate=payload.get("like_rate"),
        screenshot=payload.get("screenshot"),
        business_proof=payload.get("business_proof"),
        original=payload.get("original", True),
    )
    return _do_submit_metrics(row["id"], metrics)


# ── 人工复批改分 ──

@router.put("/works/{work_id}/score")
def override_score(work_id: int, payload: CampaignScoreOverrideIn, req: Request):
    user = auth_service.require_admin(req)
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
        cur.execute("SELECT * FROM campaign_scores WHERE work_id=?", (work_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(400, "该作品尚未打分，无法改分")

        # 人工改分：支持直接覆盖总分，或单独改内容表现分（自动重算总分）
        new_ai = s["ai_content_score"]
        if payload.total_score is not None:
            new_total = payload.total_score
        elif payload.ai_content_score is not None:
            new_ai = payload.ai_content_score
            new_total = new_ai + (s["retention_score"] or 0) + (s["interaction_score"] or 0)
        else:
            raise HTTPException(400, "请提供 total_score 或 ai_content_score")

        new_grade = campaign_scoring.grade_of(new_total)
        cur.execute(
            """UPDATE campaign_scores
               SET ai_content_score=?, total_score=?, grade=?, manual_total_score=?, manual_review_by=?, manual_review_at=?
               WHERE work_id=?""",
            (new_ai, new_total, new_grade, new_total, user["id"],
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
        )
        # 战功随等级刷新（附加战功保持不变）
        extra_flags = _json_load(row.get("extra_flags"), {})
        total_merit, merit_detail = campaign_scoring.calc_total_merit(new_grade, extra_flags)
        cur.execute(
            "UPDATE campaign_scores SET base_merit=?, total_merit=? WHERE work_id=?",
            (merit_detail["base"], merit_detail["total"], work_id),
        )

    return {"ok": True, "total_score": new_total, "grade": new_grade,
            "total_merit": merit_detail["total"], "ai_content_score": new_ai}


# ── 凭证 / 传播记录 ──

@router.post("/works/{work_id}/credentials")
def add_credential(work_id: int, payload: CampaignCredentialIn, req: Request):
    auth_service.require_user(req)
    with db_cursor() as cur:
        _get_work(cur, work_id)
        cur.execute(
            "INSERT INTO campaign_credentials (work_id, type, file, note) VALUES (?,?,?,?)",
            (work_id, payload.type, payload.file, payload.note),
        )
        return {"id": cur.lastrowid}


_CRED_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


@router.post("/credentials/upload")
async def upload_credential_image(req: Request):
    """上传凭证图片到 COS，返回 cos_key 与预签名 URL（前端回填到 T+7 提交）。

    命名规范：campaign/credentials/{screenshot|business}/{YYYY-MM}/{毫秒时间戳}_{随机}.{ext}
    公开接口：投稿人免登录回传 T+7 时也需要上传凭证图片。
    """
    auth_service.try_get_user(req)
    try:
        form = await req.form(max_files=20, max_fields=10)
    except Exception as e:
        raise HTTPException(413, f"文件过大或不支持的格式: {e}")

    ctype = (form.get("type") or "").strip()
    if ctype not in ("screenshot", "business"):
        raise HTTPException(400, "type 只能是 screenshot 或 business")

    files = [v for _, v in form.items() if isinstance(v, StarletteUploadFile) and v.filename]
    if not files:
        raise HTTPException(400, "请选择要上传的图片")

    ym = datetime.now().strftime("%Y-%m")
    results = []
    for f in files:
        ext = os.path.splitext(f.filename or "image.jpg")[1].lower() or ".jpg"
        if ext not in _CRED_MIME:
            ext = ".jpg"
        data = await f.read()
        safe_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
        cos_key = f"campaign/credentials/{ctype}/{ym}/{safe_name}"
        cos_service.upload_bytes(data, cos_key, _CRED_MIME[ext])
        url = cos_service.presigned_url(cos_key, expires=7200) if cos_service.is_enabled() else None
        results.append({
            "cos_key": cos_key,
            "url": url,
            "filename": f.filename,
            "type": ctype,
        })
    return {"files": results}


@router.post("/works/{work_id}/propagation")
def add_propagation(work_id: int, payload: CampaignPropagationIn, req: Request):
    auth_service.require_user(req)
    with db_cursor() as cur:
        _get_work(cur, work_id)
        cur.execute(
            "INSERT INTO campaign_propagation (work_id, account, url, type, note) VALUES (?,?,?,?,?)",
            (work_id, payload.account, payload.url, payload.type, payload.note),
        )
        return {"id": cur.lastrowid}


@router.get("/credentials")
def list_credentials(req: Request, region: str = ""):
    """凭证库：全部凭证列表（支持按区域筛选）。"""
    user = auth_service.require_user(req)
    if user.get("role") == "uploader":
        scope_sql, scope_params = " AND w.creator_id=?", [user["id"]]
    else:
        scope_sql, scope_params = _region_scope(user)
    region_sql = ""
    if region:
        region_sql = " AND w.region LIKE ?"
        scope_params.append(f"%{region}%")
    with db_cursor() as cur:
        cur.execute(
            """SELECT c.*, w.code, w.title, w.creator_name, w.region
               FROM campaign_credentials c JOIN campaign_works w ON w.id = c.work_id
               WHERE 1=1""" + scope_sql + region_sql + """
               ORDER BY c.id DESC LIMIT 500""",
            scope_params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    for c in rows:
        c["url"] = _cred_url(c.get("file"))
    return rows


@router.get("/propagation")
def list_propagation(req: Request):
    """传播记录库：T+7 平台数据表现（播放/留存/完播/互动/点赞）。"""
    user = auth_service.require_user(req)
    if user.get("role") == "uploader":
        scope_sql, scope_params = " AND w.creator_id=?", [user["id"]]
    else:
        scope_sql, scope_params = _region_scope(user)
    with db_cursor() as cur:
        cur.execute(
            """SELECT m.*, w.code, w.title, w.creator_name, w.track
               FROM campaign_metrics m JOIN campaign_works w ON w.id = m.work_id
               WHERE 1=1""" + scope_sql + """
               ORDER BY m.submitted_at DESC, m.work_id DESC LIMIT 500""",
            scope_params,
        )
        return [dict(r) for r in cur.fetchall()]


# ── 区域初审 ──

@router.post("/works/{work_id}/review")
def review_work(work_id: int, payload: dict, req: Request):
    """区域初审：approve=初审通过（待T+7）/ reject=打回（淘汰）。"""
    auth_service.require_manager(req)
    action = (payload.get("action") or "").strip()
    with db_cursor() as cur:
        _get_work(cur, work_id)
    if action == "approve":
        new_status, new_reject = "pending_t7", ""
    elif action == "reject":
        new_status, new_reject = "eliminated", (payload.get("comment") or "初审打回")
    else:
        raise HTTPException(400, "action 需为 approve 或 reject")
    with db_cursor() as cur:
        cur.execute(
            "UPDATE campaign_works SET status=?, reject_reason=?, updated_at=? WHERE id=?",
            (new_status, new_reject, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
        )
    return {"ok": True, "status": new_status}


# ── 反作弊抽检 ──

@router.post("/works/{work_id}/audit")
def audit_work(work_id: int, payload: CampaignAuditIn, req: Request):
    """总部抽检：sample=抽检 / clear=通过 / disqualify=清零取消资格。"""
    auth_service.require_admin(req)
    action = payload.action
    with db_cursor() as cur:
        _get_work(cur, work_id)
        cur.execute(
            "INSERT INTO campaign_audit_log (work_id, action, result, note) VALUES (?,?,?,?)",
            (work_id, action, payload.result, payload.note),
        )
        if action == "disqualify":
            cur.execute(
                "UPDATE campaign_works SET status='disqualified', qualified=0, updated_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
            )
    return {"ok": True}


# ── 四大榜单 ──

@router.get("/leaderboards/content")
def leaderboard_content(req: Request, track: str = "", region: str = "", grade: str = "",
                        creator: str = "", limit: int = 100):
    """内容榜：单条作品按总分排序（同分优先战功、留存）。"""
    user = auth_service.require_user(req)
    where, params = ["w.status='qualified'"], []
    scope_sql, scope_params = _region_scope(user)
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if track:
        where.append("w.track=?")
        params.append(track)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    if grade:
        where.append("s.grade=?")
        params.append(grade)
    if creator:
        where.append("(w.creator_name LIKE ? OR u.display_name LIKE ? OR u.username LIKE ?)")
        params += [f"%{creator}%", f"%{creator}%", f"%{creator}%"]
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.id, w.code, w.title, w.track, w.region, w.creator_name,
                       s.total_score, s.grade, s.total_merit, s.retention_score, s.manual_total_score
                FROM campaign_works w
                JOIN campaign_scores s ON s.work_id = w.id
                LEFT JOIN users u ON u.id = w.creator_id
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(s.manual_total_score, s.total_score) DESC, s.total_merit DESC, s.retention_score DESC
                LIMIT ?""",
            params + [limit],
        )
        return [dict(r) for r in cur.fetchall()]


@router.get("/leaderboards/creator")
def leaderboard_creator(req: Request, region: str = "", limit: int = 20, all_status: int = 0):
    """创作者库：聚合所有作品；战功仍只统计已计分（qualified）作品。

    all_status=1 时（创作者库 Tab）包含所有状态的创作者，战功按已计分作品计算；
    默认 all_status=0（四大榜单·创作者榜）只统计已计分作品。
    """
    user = auth_service.require_user(req)
    where, params = [], []
    if not all_status:
        where.append("w.status='qualified'")
    scope_sql, scope_params = _region_scope(user)
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.creator_id, COALESCE(w.creator_name, u.display_name, u.username, '匿名') AS name,
                      w.region, s.total_merit, w.created_at, w.status
               FROM campaign_works w
               LEFT JOIN users u ON u.id = w.creator_id
               LEFT JOIN campaign_scores s ON s.work_id = w.id
               {where_sql}""",
            params,
        )
        data = [dict(r) for r in cur.fetchall()]

    creators = {}
    for r in data:
        key = r["creator_id"] or r["name"]
        c = creators.setdefault(key, {"name": r["name"], "region": r["region"], "merits": [], "weeks": set(), "work_count": 0})
        c["work_count"] += 1
        if r["status"] == "qualified":
            c["merits"].append(r["total_merit"] or 0)
            if r["created_at"]:
                try:
                    c["weeks"].add(datetime.fromisoformat(r["created_at"]).strftime("%Y%W"))
                except Exception:
                    pass

    out = []
    for key, c in creators.items():
        top3 = sorted(c["merits"], reverse=True)[:3]
        total = sum(top3)
        extra = 5 if len(c["weeks"]) >= 4 else 0
        out.append({
            "name": c["name"],
            "region": c["region"],
            "top3_merit": total,
            "continuous_weeks_bonus": extra,
            "total": total + extra,
            "work_count": c["work_count"],
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out[:limit]


@router.get("/leaderboards/track")
def leaderboard_track(req: Request, region: str = ""):
    """赛道榜：分赛道，区域 Top5 作品战功 + 有效创作者统计。"""
    user = auth_service.require_user(req)
    where, params = ["w.status='qualified'"], []
    scope_sql, scope_params = _region_scope(user)
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.track, w.region, s.total_merit, w.creator_id, w.code, w.title
               FROM campaign_works w JOIN campaign_scores s ON s.work_id = w.id
               WHERE {' AND '.join(where)}""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    result = {}
    for r in rows:
        track = r["track"]
        t = result.setdefault(track, {"track": track, "regions": {}, "total_merit": 0, "creator_ids": set()})
        t["total_merit"] += r["total_merit"] or 0
        t["creator_ids"].add(r["creator_id"])
        region = r["region"] or "未分区"
        rg = t["regions"].setdefault(region, {"region": region, "works": [], "total_merit": 0})
        rg["total_merit"] += r["total_merit"] or 0
        rg["works"].append({"code": r["code"], "title": r["title"], "merit": r["total_merit"]})

    out = []
    for track, t in result.items():
        regions = []
        for region, rg in t["regions"].items():
            rg["works"].sort(key=lambda x: x["merit"], reverse=True)
            rg["top5"] = rg["works"][:5]
            rg.pop("works", None)
            regions.append(rg)
        regions.sort(key=lambda x: x["total_merit"], reverse=True)
        out.append({
            "track": track,
            "total_merit": t["total_merit"],
            "valid_creator_count": len(t["creator_ids"]),
            "regions": regions,
        })
    out.sort(key=lambda x: x["total_merit"], reverse=True)
    return out


@router.get("/leaderboards/warzone")
def leaderboard_warzone(req: Request, region: str = ""):
    """战区总榜（月度 100 分）：
    优秀创作者培养 35 + 高质量内容产出 35 + 渠道业务价值 15 + 方法沉淀复制 15。
    """
    user = auth_service.require_user(req)
    where, params = ["w.status='qualified'"], []
    scope_sql, scope_params = _region_scope(user)
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.region, s.total_merit, s.grade, s.extra_merit, w.creator_id
               FROM campaign_works w JOIN campaign_scores s ON s.work_id = w.id
               WHERE {' AND '.join(where)}""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    def big_region(region):
        return (region or "").split("/")[0].strip() or "未分区"

    zones = {}
    for r in rows:
        z = big_region(r["region"])
        zone = zones.setdefault(z, {
            "region": z,
            "total_merit": 0,
            "s_a_count": 0,
            "valid_creators": set(),
            "cross_region_count": 0,
        })
        zone["total_merit"] += r["total_merit"] or 0
        if r["grade"] in ("S", "A"):
            zone["s_a_count"] += 1
        zone["valid_creators"].add(r["creator_id"])
        extra = _json_load(r.get("extra_merit"), {})
        if extra.get("detail", {}).get("cross_region", {}).get("on"):
            zone["cross_region_count"] += 1

    # 归一化到 100 分制（分母至少为 1，避免除零）
    max_creator = max((len(z["valid_creators"]) for z in zones.values()), default=0) or 1
    max_sa = max((z["s_a_count"] for z in zones.values()), default=0) or 1
    max_merit = max((z["total_merit"] for z in zones.values()), default=0) or 1
    max_cross = max((z["cross_region_count"] for z in zones.values()), default=0) or 1

    out = []
    for z in zones.values():
        score_creator = round(len(z["valid_creators"]) / max_creator * 35, 1)
        score_content = round(z["s_a_count"] / max_sa * 35, 1)
        score_business = round(z["total_merit"] / max_merit * 15, 1)
        score_method = round(z["cross_region_count"] / max_cross * 15, 1)
        out.append({
            "region": z["region"],
            "valid_creator_count": len(z["valid_creators"]),
            "s_a_count": z["s_a_count"],
            "total_merit": z["total_merit"],
            "cross_region_count": z["cross_region_count"],
            "score_creator": score_creator,
            "score_content": score_content,
            "score_business": score_business,
            "score_method": score_method,
            "total": round(score_creator + score_content + score_business + score_method, 1),
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


# ── 区域业绩（三级下钻：大区 → 细分区域 → 作品/线索）──

@router.get("/region/overview")
def region_overview(req: Request):
    """一级：6 大区业绩概览（战功/作品/S·A/创作者/有效线索 + 100 分制）。"""
    user = auth_service.require_user(req)
    scope_sql, scope_params = _region_scope(user)
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.region, w.status, w.creator_id, s.grade, s.total_merit, s.extra_merit
                FROM campaign_works w
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                WHERE 1=1{scope_sql}""",
            scope_params,
        )
        works = [dict(r) for r in cur.fetchall()]
        cur.execute(
            f"""SELECT w.region, COUNT(*) AS c
                FROM campaign_credentials cr
                JOIN campaign_works w ON w.id = cr.work_id
                WHERE cr.type='business'{scope_sql}
                GROUP BY w.region""",
            scope_params,
        )
        lead_map = {r["region"]: r["c"] for r in cur.fetchall()}

    def big_region(region):
        return (region or "").split("/")[0].strip() or "未分区"

    zones = {}
    for r in works:
        z = big_region(r["region"])
        zone = zones.setdefault(z, {
            "region": z, "total_merit": 0, "s_a_count": 0,
            "valid_creators": set(), "work_count": 0, "cross_region_count": 0,
        })
        zone["work_count"] += 1
        if r["total_merit"]:
            zone["total_merit"] += r["total_merit"]
        if r["grade"] in ("S", "A"):
            zone["s_a_count"] += 1
        zone["valid_creators"].add(r["creator_id"])
        extra = _json_load(r.get("extra_merit"), {})
        if extra.get("detail", {}).get("cross_region", {}).get("on"):
            zone["cross_region_count"] += 1

    max_creator = max((len(z["valid_creators"]) for z in zones.values()), default=0) or 1
    max_sa = max((z["s_a_count"] for z in zones.values()), default=0) or 1
    max_merit = max((z["total_merit"] for z in zones.values()), default=0) or 1
    max_cross = max((z["cross_region_count"] for z in zones.values()), default=0) or 1

    out = []
    for z in zones.values():
        big = z["region"]
        lead_count = sum(c for r, c in lead_map.items() if big_region(r) == big)
        score_creator = round(len(z["valid_creators"]) / max_creator * 35, 1)
        score_content = round(z["s_a_count"] / max_sa * 35, 1)
        score_business = round(z["total_merit"] / max_merit * 15, 1)
        score_method = round(z["cross_region_count"] / max_cross * 15, 1)
        out.append({
            "region": big,
            "work_count": z["work_count"],
            "valid_creator_count": len(z["valid_creators"]),
            "s_a_count": z["s_a_count"],
            "total_merit": z["total_merit"],
            "lead_count": lead_count,
            "cross_region_count": z["cross_region_count"],
            "score_creator": score_creator,
            "score_content": score_content,
            "score_business": score_business,
            "score_method": score_method,
            "total": round(score_creator + score_content + score_business + score_method, 1),
        })
    out.sort(key=lambda x: x["total"], reverse=True)
    return out


@router.get("/region/subregions")
def region_subregions(req: Request, big: str = ""):
    """二级：某大区下的细分区域业绩。"""
    user = auth_service.require_user(req)
    scope_sql, scope_params = _region_scope(user)
    where, params = ["1=1"], []
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if big:
        where.append("w.region LIKE ?")
        params.append(f"{big}%")
    where_sql = " AND ".join(where)
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.region, w.creator_id, s.grade, s.total_merit
                FROM campaign_works w
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                WHERE {where_sql}""",
            params,
        )
        works = [dict(r) for r in cur.fetchall()]
        cur.execute(
            f"""SELECT w.region, COUNT(*) AS c
                FROM campaign_credentials cr
                JOIN campaign_works w ON w.id = cr.work_id
                WHERE cr.type='business' AND {where_sql}
                GROUP BY w.region""",
            params,
        )
        lead_map = {r["region"]: r["c"] for r in cur.fetchall()}

    subs = {}
    for r in works:
        full = (r["region"] or "").strip()
        sub = full.split("/")[-1].strip() if "/" in full else full
        z = subs.setdefault(full, {
            "region": full, "sub_region": sub,
            "total_merit": 0, "s_a_count": 0, "work_count": 0, "valid_creators": set(),
        })
        z["work_count"] += 1
        if r["total_merit"]:
            z["total_merit"] += r["total_merit"]
        if r["grade"] in ("S", "A"):
            z["s_a_count"] += 1
        z["valid_creators"].add(r["creator_id"])

    out = []
    for full, z in subs.items():
        out.append({
            "region": full,
            "sub_region": z["sub_region"],
            "work_count": z["work_count"],
            "valid_creator_count": len(z["valid_creators"]),
            "s_a_count": z["s_a_count"],
            "total_merit": z["total_merit"],
            "lead_count": lead_map.get(full, 0),
        })
    out.sort(key=lambda x: x["total_merit"], reverse=True)
    return out


# ── 报表导出 ──

@router.get("/export")
def export_works(req: Request, track: str = "", region: str = ""):
    user = auth_service.require_user(req)
    where, params = ["1=1"], []
    scope_sql, scope_params = _region_scope(user)
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if track:
        where.append("w.track=?")
        params.append(track)
    if region:
        where.append("w.region LIKE ?")
        params.append(f"%{region}%")

    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.code, w.track, w.title, w.region, COALESCE(w.creator_name, u.display_name, u.username, '') AS creator,
                       m.plays, m.retention_3s, m.completion_rate, m.deep_interaction_rate, m.like_rate,
                       s.ai_content_score, s.retention_score, s.interaction_score, s.total_score, s.grade, s.total_merit, w.status
                FROM campaign_works w
                LEFT JOIN users u ON u.id = w.creator_id
                LEFT JOIN campaign_metrics m ON m.work_id = w.id
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                WHERE {' AND '.join(where)}
                ORDER BY w.id DESC""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["一稿一码", "赛道", "标题", "区域", "创作者", "播放", "3秒留存%", "完播率%",
                     "深度互动率%", "点赞率%", "AI内容分", "留存分", "互动分", "总分", "等级", "战功", "状态"])
    for r in rows:
        writer.writerow([
            r["code"], r["track"], r["title"], r["region"], r["creator"],
            r["plays"], r["retention_3s"], r["completion_rate"], r["deep_interaction_rate"],
            r["like_rate"], r["ai_content_score"], r["retention_score"], r["interaction_score"],
            r["total_score"], r["grade"], r["total_merit"], r["status"],
        ])
    csv_str = buf.getvalue()
    return Response(
        content="\ufeff" + csv_str,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=campaign_export.csv"},
    )


# ── 赛道枚举 ──

@router.get("/tracks")
def list_tracks():
    return {"tracks": database.get_tracks()}


@router.post("/tracks")
def create_track(payload: dict, req: Request):
    auth_service.require_manager(req)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "赛道名不能为空")
    try:
        tid = database.add_track(name)
    except Exception:
        raise HTTPException(409, "赛道已存在")
    return {"id": tid, "name": name}


@router.put("/tracks/{track_id}")
def update_track(track_id: int, payload: dict, req: Request):
    auth_service.require_manager(req)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "赛道名不能为空")
    database.update_track(track_id, name)
    return {"ok": True}


@router.delete("/tracks/{track_id}")
def delete_track(track_id: int, req: Request):
    auth_service.require_manager(req)
    database.delete_track(track_id)
    return {"ok": True}
