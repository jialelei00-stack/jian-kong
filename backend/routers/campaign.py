"""双百战役路由：两段式上传、评分、战功、四大榜单、反作弊、导出。

业务闭环：
  1. 首次上传建档（合规预检，不结算不上榜）
  2. T+7 二次提交 → 门槛校验 + AI 情理色诚打分 + 留存/互动区间算分 → 总分/等级/战功
  3. 刷新四大榜单；总部反作弊抽检；支持人工改分
"""

import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape, quoteattr

import runtime_utils
from runtime_utils import normalize_region

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
    CampaignCredentialReviewIn, CampaignMeritIn,
)

_REGION_HEADCOUNTS = {
    "安徽AB区": 10, "安徽CD区": 10, "北京市": 8, "甘青宁藏": 10,
    "贵州省": 11, "桂北区": 7, "桂南区": 11, "海南省": 4,
    "河北AB区": 11, "河北CD区": 14, "河南AB区": 13, "河南CD区": 17,
    "黑龙江省": 8, "湖北AB区": 11, "湖北CD区": 8, "湖南AB区": 11,
    "湖南CD区": 9, "吉林省": 7, "江苏AB区": 15, "江西省": 14,
    "辽北区": 7, "辽南区": 7, "鲁东CD区": 8, "鲁西CD区": 7,
    "闽北区": 10, "闽南区": 11, "内蒙古": 6, "山东AB区": 16,
    "山西省": 9, "陕西AB区": 9, "陕西CD区": 5, "上海市": 11,
    "四川AB区": 12, "四川CD区": 9, "苏北CD区": 8, "苏南CD区": 10,
    "天津市": 3, "新疆省": 6, "粤东A区": 12, "粤东B区": 6,
    "粤西A区": 14, "粤西B区": 8, "云南省": 12, "浙东区": 6,
    "浙西区": 15, "重庆AB区": 6, "重庆CD区": 6,
}
_REGION_HEADCOUNT_ALIASES = {"苏南区": "苏南CD区", "豫北CD区": "河南CD区", "豫南CD区": "河南CD区"}
_REGIONS_BY_ZONE = {
    "华东区": ["安徽AB区", "安徽CD区", "江苏AB区", "上海市", "苏北CD区", "苏南CD区", "浙东区", "浙西区"],
    "华北区": ["北京市", "河北AB区", "河北CD区", "河南AB区", "山东AB区", "山西省", "天津市", "豫北CD区", "豫南CD区", "鲁东CD区", "鲁西CD区"],
    "华南区": ["海南省", "湖北AB区", "湖北CD区", "湖南AB区", "湖南CD区", "江西省", "闽北区", "闽南区", "粤东A区", "粤东B区", "粤西A区", "粤西B区"],
    "东北区": ["黑龙江省", "吉林省", "辽北区", "辽南区", "内蒙古"],
    "西一区": ["甘青宁藏", "陕西AB区", "陕西CD区", "四川AB区", "四川CD区", "新疆省", "重庆AB区", "重庆CD区"],
    "西二区": ["贵州省", "桂北区", "桂南区", "云南省"],
}

def _headcount_region(region):
    return _REGION_HEADCOUNT_ALIASES.get(region, region)


router = APIRouter(prefix="/api/campaign", tags=["双百战役"])

_DEFAULT_PLAY_THRESHOLD = 0
_DEFAULT_MIN_DAYS = 0


def _campaign_thresholds() -> tuple:
    """读取双百参评门槛；默认保持当前 0 播放、0 天。"""
    try:
        plays = max(0, int(database.get_setting("campaign_play_threshold", str(_DEFAULT_PLAY_THRESHOLD))))
    except (TypeError, ValueError):
        plays = _DEFAULT_PLAY_THRESHOLD
    try:
        days = max(0, int(database.get_setting("campaign_min_days", str(_DEFAULT_MIN_DAYS))))
    except (TypeError, ValueError):
        days = _DEFAULT_MIN_DAYS
    return plays, days


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
    """给作品素材补充可访问的展示 URL（COS 预签名 / 本地 HTTP 兜底），供详情弹窗预览。

    cos_ready=False 表示后台上传 COS 尚未完成，此时先回退本地 /uploads，
    免得给出一个还不存在的对象地址（默认 True 兼容历史数据与手动建档）。
    """
    if not media_list:
        return media_list
    base_url = os.environ.get("SERVER_BASE_URL", ai_auditor.DEFAULT_SERVER_BASE_URL)
    for m in media_list:
        if not isinstance(m, dict):
            continue
        key = m.get("cos_key")
        if key and cos_service.is_enabled() and m.get("cos_ready", True):
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


# 榜单聚合时给 region 为空的作品起的占位名（见 _big_region）。
# 数据库里并不存在这个值，查询时必须特判成「region 为空」，否则下钻永远查不到。
_UNASSIGNED = "未分区"


def _big_region(region: str) -> str:
    """取作品所属大区；region 为空的作品归入 _UNASSIGNED。"""
    return (region or "").split("/")[0].strip() or _UNASSIGNED


def _is_participating(region: str) -> bool:
    """是否属于参战的 6 个固定大区（runtime_utils.REGIONS）。

    参战大区就固定这 6 个。管理员建档时没填区域的作品会被归入 _UNASSIGNED 占位名，
    但它不该出现在任何按大区聚合的榜位/概览/导出里——否则榜上会凭空多出一个
    没有实际业务的"未分区大区"，还会挤占名次和折算分母。
    """
    return (region or "").split("/")[0].strip() in runtime_utils.REGIONS


def _region_filter(region: str, prefix: bool = False):
    """构造区域筛选条件，返回 (SQL 片段, 参数)。

    _UNASSIGNED 是展示层占位名而非真实区域值，直接拿去 LIKE 永远匹配不到，
    所以这里转成「region 为空」的判断，让榜单上的"未分区"卡片可以正常下钻。
    """
    r = (region or "").strip()
    if not r:
        # 只有空白字符：视为非法区域，返回空集而不是全量，
        # 否则 LIKE '%%' 会退化成"不过滤"，把全部区域的数据都吐出去。
        return "1=0", []
    if r == _UNASSIGNED:
        return "TRIM(COALESCE(w.region, '')) = ''", []
    if prefix:
        return "w.region LIKE ?", [f"{r}%"]
    return "w.region LIKE ?", [f"%{r}%"]


def _hist_files(val) -> list:
    """把历史快照里的凭证字段规整成 [{file, url}]（file 是 COS key，url 实时预签名）。"""
    items = val if isinstance(val, list) else ([val] if val else [])
    out = []
    for it in items:
        if not it:
            continue
        f = (it.get("file") or it.get("url") or "") if isinstance(it, dict) else str(it)
        if f:
            out.append({"file": f, "url": _cred_url(f)})
    return out


def _load_metrics_history(cur, work_id: int) -> list:
    """读取该作品的历次 T+7 回传快照（按第几次升序）。

    campaign_metrics 只留最新一次，历次明细全靠这张表——后台点进作品后
    能看到每次回传的指标、分数、凭证分别长什么样。
    """
    cur.execute(
        "SELECT * FROM campaign_metrics_history WHERE work_id=? ORDER BY seq, id", (work_id,)
    )
    out = []
    for h in cur.fetchall():
        hd = dict(h)
        hd["screenshot_files"] = _hist_files(_json_load(hd.get("screenshot"), []))
        hd["business_files"] = _hist_files(_json_load(hd.get("business_proof"), []))
        hd["ocr_raw"] = _json_load(hd.get("ocr_raw"), None)
        out.append(hd)
    return out


def _serialize_work(row: dict) -> dict:
    """把 works 行 + metrics + scores 组装成完整详情。"""
    work_id = row["id"]
    out = dict(row)
    out["media"] = _fill_media_urls(_json_load(out.get("media"), []))
    out["extra_flags"] = _json_load(out.get("extra_flags"), {})
    with db_cursor() as cur:
        cur.execute("SELECT * FROM campaign_metrics WHERE work_id=?", (work_id,))
        m = cur.fetchone()
        if m:
            md = dict(m)
            # AI 识别原值以 JSON 存库，详情页要直接比对，这里解出来
            md["ocr_raw"] = _json_load(md.get("ocr_raw"), None)
        out["metrics"] = md if m else None
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
        # 页面主体只展示最后一次（campaign_metrics），历次明细在这里给全
        out["metrics_history"] = _load_metrics_history(cur, work_id)
        out["submit_count"] = len(out["metrics_history"])
        out["remaining_submissions"] = max(0, 3 - out["submit_count"])
        out["can_submit"] = out["remaining_submissions"] > 0 and out['status'] in ('pending_t7', 'qualified', 'eliminated')
        cur.execute('SELECT * FROM campaign_metric_corrections WHERE work_id=? ORDER BY id', (work_id,))
        out['metric_corrections'] = [dict(r) for r in cur.fetchall()]
    return out


def _get_work(cur, work_id: int):
    cur.execute("SELECT * FROM campaign_works WHERE id=?", (work_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "作品不存在")
    return dict(row)


def _score_work_content_bg(work_id: int, caption: str, media_list: list,
                           upgrade: bool = False) -> None:
    """后台补算作品的内容表现分（情理色诚 0-40）。

    上传入口走这个：建档时先把 code 返回给用户，多模态打分放后台，
    否则大视频打分要几十秒到几分钟，请求会被 CDN 判为超时（504）。

    upgrade=True 表示这是 COS 传完之后的重算：首算时素材地址往往还没就绪，
    模型看不到完整视频、只能靠关键帧，口播与背景音全丢，分数是失真的，
    需要用完整视频覆盖一次。
    """
    try:
        ai_quality = campaign_scoring.ai_score_content_quality(caption, media_list)
        if ai_quality.get("error") is not None:
            print(f"[campaign] 后台打分失败 #{work_id}: {ai_quality.get('error')}", flush=True)
            return
        with db_cursor() as cur:
            # campaign_scores 的主键是 work_id，没有 id 列（写 SELECT id 会抛
            # no such column: id，导致这里每次打分都异常、分数根本写不进去）
            cur.execute(
                "SELECT manual_review_by, total_score FROM campaign_scores WHERE work_id=?",
                (work_id,),
            )
            exist = cur.fetchone()
            if exist:
                # 人工改过分，或已经出过总分（总分是在内容分基础上算的，改了会不一致）→ 不动
                if not upgrade or exist["manual_review_by"] or exist["total_score"] is not None:
                    return
                cur.execute(
                    """UPDATE campaign_scores
                          SET ai_content_score=?, ai_dims=?, ai_reason=?
                        WHERE work_id=?""",
                    (
                        ai_quality.get("score"),
                        _json_dump(ai_quality.get("dims") or {}),
                        ai_quality.get("reason") or "",
                        work_id,
                    ),
                )
                return
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
    except Exception as e:
        print(f"[campaign] 后台打分异常 #{work_id}: {e}", flush=True)


def score_pending_work_for_submission(submission_id: int) -> None:
    """媒体地址就绪后，补算/重算该提交对应作品的内容表现分。

    由上传后置任务在 COS 上传完成后调用。建档那一刻 media 里 cos_ready 还是
    False、本地副本也可能已被审核回收，直接打分取不到任何媒体，只能靠文案，
    分数是失真的。这里带着 upgrade=True 重算一次，用完整视频覆盖掉首算结果
    （人工改过分或已出总分的作品不会被覆盖，见 _score_work_content_bg）。
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                "SELECT w.id, w.title, s.media FROM campaign_works w "
                "LEFT JOIN submissions s ON s.id = w.submission_id "
                "WHERE w.submission_id=?",
                (submission_id,),
            )
            row = cur.fetchone()
            if not row:
                return
            work_id = row["id"]
            title = row["title"] or ""
            media_raw = row["media"]
        media = _json_load(media_raw, []) if media_raw else []
        _score_work_content_bg(work_id, title, media, upgrade=True)
    except Exception as e:
        print(f"[campaign] 补算内容表现分失败 #{submission_id}: {e}", flush=True)


def _create_campaign_work(creator_id, creator_name, track, title, url, media,
                          region, submission_id=None, extra_flags=None,
                          defer_ai=False) -> dict:
    """统一的作品建档逻辑：合规预检 + 建档 + 首次 AI 内容表现分。

    供 /api/campaign/works（手动建档）与 /api/submissions/upload（上传建档）
    两个入口复用，避免重复写库与字段不一致。

    defer_ai=True 时跳过同步打分（调用方自行安排后台补算），用于上传路径。
    """
    caption = (title or "").strip()
    # 区域统一成「大区 / 细分区域」：手动建档弹窗是自由文本，「西二区云南省」
    # 这种漏了斜杠的写法会让大区榜凭空多出一个幻影大区。
    region = normalize_region(region)
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
    ai_quality = {}
    if not defer_ai:
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
        "ai_content_score": ai_quality.get("score") if ai_quality and ai_quality.get("error") is None else None,
    }


# ── 首次上传建档 ──

@router.post("/works")
def create_work(payload: CampaignWorkIn, req: Request):
    auth_service.require_user(req)
    track = (payload.track or "").strip()
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if track not in valid_tracks:
        raise HTTPException(400, f"赛道不合法，可选：{' / '.join(valid_tracks)}")

    media = payload.media if isinstance(payload.media, list) else []
    # defer_ai=True：建档只写库、立即返回一稿一码；多模态打分放后台线程，
    # 否则这个大视频要一两分钟，请求会被 CDN 回源超时切断。
    work = _create_campaign_work(
        creator_id=payload.creator_id,
        creator_name=payload.creator_name,
        track=track,
        title=payload.title,
        url=payload.url,
        media=media,
        region=payload.region,
        extra_flags=payload.extra_flags,
        defer_ai=True,
    )
    threading.Thread(
        target=_score_work_content_bg,
        args=(work["id"], payload.title or "", media),
        daemon=True,
    ).start()
    return work


# ── 作品列表（筛选：大区/赛道/等级/创作者）──

@router.get("/works")
def list_works(req: Request, region: str = "", track: str = "", grade: str = "",
               creator: str = "", status: str = "", exclude_closed: int = 0):
    user = auth_service.require_user(req)
    where, params = ["1=1"], []
    # 区域下钻的作品明细需与「全国战况」口径一致：不展示已淘汰/已清零作品。
    # 内容库等场景不传该参数，仍可看到全部状态。
    if exclude_closed:
        where.append("w.status NOT IN ('eliminated','disqualified','excluded')")
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
        cond, p = _region_filter(region)
        where.append(cond)
        params.extend(p)
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
        # 列表只回前端真正要用的列：media（素材 JSON）单独就占整个响应的 40%，
        # 而列表页不渲染素材、只有详情接口 /works/{id} 才需要；ai_reason 同理（AI 结论全文）。
        # 驾驶舱每 20 秒轮询这个接口，字段少一半响应就小一半。
        cur.execute(
            f"""SELECT w.id, w.code, w.creator_id, w.creator_name, w.track, w.title, w.url,
                       w.region, w.status, w.publish_time, w.created_at,
                       s.total_score, s.grade, s.total_merit, s.manual_total_score, s.ai_content_score,
                       CASE WHEN m.work_id IS NULL THEN 0 ELSE 1 END AS has_t7_metrics
                FROM campaign_works w
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                LEFT JOIN campaign_metrics m ON m.work_id = w.id
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
    """按创作者姓名 + 所属区域查询可回传的作品，返回一稿一码列表。

    姓名走**精确匹配**：以前用 LIKE '%姓名%'，"李"会把"李俊 / 李娇 / 李圣杰"的作品
    一并列出来，而列表里又不展示一稿一码，极易把数据回传到别人的作品上。
    区域必填：同名的人靠它区分；小区可留空（部分作品只登记到大区）。

    T+7 允许同一作品多次回传：已回传（qualified）的作品同样列出，
    前端据此提示「已回传 N 次，本次是第 N+1 次」。
    """
    name = (name or "").strip()
    if not name:
        return []
    where = ["w.status IN ('pending_t7','qualified')", "TRIM(COALESCE(w.creator_name,'')) = ?"]
    params = [name]
    region = normalize_region(region)
    if not region:
        return []
    parts = [p.strip() for p in region.split("/") if p.strip()]
    if len(parts) >= 2:
        # 传进来的是「大区 / 小区」：精确匹配整串，或该作品只登记到大区
        where.append("(TRIM(COALESCE(w.region,'')) = ? OR TRIM(COALESCE(w.region,'')) = ?)")
        params.extend([f"{parts[0]} / {parts[1]}", parts[0]])
    else:
        where.append("TRIM(COALESCE(w.region,'')) LIKE ?")
        params.append(f"{parts[0]}%")
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.id, w.code, w.title, w.track, w.creator_name, w.region, w.url, w.status,
                       w.publish_time, w.created_at,
                       COALESCE((SELECT COUNT(*) FROM campaign_metrics_history h
                                 WHERE h.work_id=w.id), 0) AS submit_count,
                       (SELECT MAX(h.submitted_at) FROM campaign_metrics_history h
                        WHERE h.work_id=w.id) AS last_submitted_at
                FROM campaign_works w WHERE {' AND '.join(where)} ORDER BY w.id DESC LIMIT 50""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
        for item in rows:
            item['remaining_submissions'] = max(0, 3 - item['submit_count'])
            item['can_submit'] = item['remaining_submissions'] > 0 and item['status'] in ('pending_t7', 'qualified', 'eliminated')
        return rows


# ── 作品详情 ──

@router.get("/works/{work_id}")
def get_work(work_id: int, req: Request):
    auth_service.require_user(req)
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
    return _serialize_work(row)


# ── T+7 二次提交 → 自动打分 ──

def _has_cred(val) -> bool:
    """凭证字段是否至少有一项（兼容 list / JSON 字符串 / None）。"""
    if isinstance(val, str):
        val = _json_load(val, [])
    return bool(val)


def _save_metrics_history(work_id: int, seq: int, payload: CampaignMetricsIn,
                          submitter: str, qualified: bool, reject_reason: str = "",
                          score: dict = None) -> None:
    """把本次 T+7 回传的完整快照写入 campaign_metrics_history。

    无论本次是否达标计分都留档——"回传过但没达标"也是用户关心的一次记录。
    """
    sc = score or {}
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_metrics_history
               (work_id, seq, plays, retention_3s, completion_rate, comment_count,
                like_count, share_count, screenshot, business_proof, ocr_raw,
                ai_content_score, retention_score, interaction_score, total_score, grade,
                total_merit, qualified, reject_reason, submitter, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                work_id, seq, payload.plays, payload.retention_3s, payload.completion_rate,
                payload.comment_count, payload.like_count, payload.share_count,
                _json_dump(payload.screenshot), _json_dump(payload.business_proof),
                _json_dump(payload.ocr_raw),
                sc.get("ai_content_score"), sc.get("retention_score"), sc.get("interaction_score"),
                sc.get("total_score"), sc.get("grade"), sc.get("total_merit"),
                1 if qualified else 0, reject_reason or "", submitter,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def _do_submit_metrics_inner(work_id: int, payload: CampaignMetricsIn, submitter: str = "") -> dict:
    """执行 T+7 提交打分（不含权限校验），返回打分结果。

    同一作品允许多次回传：campaign_metrics / campaign_scores 覆盖为最新一次
    （列表、详情、榜单口径不变），同时往 campaign_metrics_history 追加一条
    带 seq 的快照，后台可查看历次回传的数据与分数变化。
    """
    # 「后台数据截图」是这六项数字的来源凭证，必传；「有效线索回传」选填——
    # 传了可以在有效线索库里人工追加战功，不传不影响本次计分。
    # 截图缺了要在这里直接拒收，不能落到下面的 reject_reasons 里——那样会存进库、
    # 只是不计分，等于没拦住漏传来源凭证的回传。
    if not _has_cred(payload.screenshot):
        raise HTTPException(400, "请上传「后台数据截图」后再提交")

    with db_cursor() as cur:
        row = _get_work(cur, work_id)
        cur.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM campaign_metrics_history WHERE work_id=?",
            (work_id,),
        )
        seq = cur.fetchone()["n"]

    # 门槛校验
    play_threshold, min_days = _campaign_thresholds()
    reject_reasons = []
    if not payload.original:
        reject_reasons.append("非原创/有效二创")
    plays = payload.plays or 0
    if plays < play_threshold:
        reject_reasons.append(f"自然播放 {plays} < {play_threshold}")
    if _days_since(row["publish_time"] or row["created_at"]) < min_days:
        reject_reasons.append(f"发布未满 {min_days} 天")
    if row["reject_reason"]:
        reject_reasons.append("合规预检未通过")
    qualified = (len(reject_reasons) == 0)

    # 保存指标
    with db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_metrics
               (work_id, plays, retention_3s, completion_rate, comment_count, like_count,
                share_count, screenshot, business_proof, ocr_raw, ocr_at, submitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET
                 plays=excluded.plays, retention_3s=excluded.retention_3s,
                 completion_rate=excluded.completion_rate,
                 comment_count=excluded.comment_count,
                 like_count=excluded.like_count, share_count=excluded.share_count,
                 screenshot=excluded.screenshot,
                 business_proof=excluded.business_proof,
                 ocr_raw=excluded.ocr_raw, ocr_at=excluded.ocr_at,
                 submitted_at=excluded.submitted_at""",
            (
                work_id, payload.plays, payload.retention_3s, payload.completion_rate,
                payload.comment_count, payload.like_count, payload.share_count,
                _json_dump(payload.screenshot), _json_dump(payload.business_proof),
                _json_dump(payload.ocr_raw),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S") if payload.ocr_raw else None,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        # 作品链接：回传时由投稿人填写，回写到作品上（首次上传不再要求填链接）
        new_url = (payload.url or "").strip()
        if new_url:
            cur.execute(
                "UPDATE campaign_works SET url=?, updated_at=? WHERE id=?",
                (new_url, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
            )
        # 凭证联动：只有「有效线索回传」进凭证库——有效线索库只放这一类。
        # 「后台数据截图」不入库：它已经完整落在 campaign_metrics.screenshot 和
        # history 快照里，视频数据库直接读那份，同一张图不存两份。
        # 不先删旧记录——同一作品多次回传时，历次有效线索都要留在库里，
        # 用 seq 标注它属于第几次回传。
        for ctype, val in (("business", payload.business_proof),):
            if not val:
                continue
            items = val if isinstance(val, list) else [val]
            for it in items:
                if not it:
                    continue
                if isinstance(it, dict):
                    f = it.get("file") or it.get("url") or ""
                    n = it.get("note") or f"T+7 第 {seq} 次提交"
                else:
                    f = str(it)
                    n = f"T+7 第 {seq} 次提交"
                if f:
                    cur.execute(
                        "INSERT INTO campaign_credentials (work_id, type, file, note, seq) VALUES (?,?,?,?,?)",
                        (work_id, ctype, f, n, seq),
                    )

    if not qualified:
        with db_cursor() as cur:
            cur.execute(
                "UPDATE campaign_works SET stage='second', status='eliminated', qualified=0, reject_reason=?, updated_at=? WHERE id=?",
                ("；".join(reject_reasons), datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
            )
        # 未达标也是一次回传，同样留档（不带分数）
        _save_metrics_history(work_id, seq, payload, submitter, False, "；".join(reject_reasons))
        return {"qualified": False, "reject_reasons": reject_reasons,
                "status": "eliminated", "seq": seq}

    # AI 情理色诚打分（0-40）：优先复用第一次上传后已打的分，避免 T+7 重复请求
    with db_cursor() as cur:
        cur.execute(
            "SELECT ai_content_score, ai_dims, ai_reason, manual_total_score, manual_review_by, COALESCE(manual_merit,0) AS manual_merit "
            "FROM campaign_scores WHERE work_id=?",
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
        "comment_count": payload.comment_count,
        "like_count": payload.like_count,
        "share_count": payload.share_count,
        "plays": payload.plays,
    }
    full = campaign_scoring.calc_full_score(metrics, ai_quality)
    if existing and existing['manual_total_score'] is not None:
        full['total_score'] = existing['manual_total_score']
        full['grade'] = campaign_scoring.grade_of(full['total_score'])
    extra_flags = _json_load(row.get("extra_flags"), {})
    total_merit, merit_detail = campaign_scoring.calc_total_merit(full["grade"], extra_flags)
    # 人工审核追加的战功要保留：重算基础+附加之后再加回人工那部分，否则一次回传就抹掉了
    manual_merit = int(existing["manual_merit"] or 0) if existing else 0
    total_merit += manual_merit

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
                _json_dump(merit_detail), total_merit,
            ),
        )
        cur.execute(
            "UPDATE campaign_works SET stage='second', status='qualified', qualified=1, reject_reason='', updated_at=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), work_id),
        )

    # 本次回传留档（含本次算出的分数，便于后台看到历次分数变化）
    _save_metrics_history(work_id, seq, payload, submitter, True, "", {
        "ai_content_score": full["ai_content_score"],
        "retention_score": full["retention_score"],
        "interaction_score": full["interaction_score"],
        "total_score": full["total_score"],
        "grade": full["grade"],
        "total_merit": total_merit,
    })

    return {
        "qualified": True,
        "status": "qualified",
        "seq": seq,
        "score": {
            "ai_content_score": full["ai_content_score"],
            "ai_dims": full["ai_dims"],
            "ai_reason": full["ai_reason"],
            "retention_score": full["retention_score"],
            "interaction_score": full["interaction_score"],
            "interaction_detail": full["interaction_detail"],
            "share_count": full["share_count"],
            "share_rate": full["share_rate"],
            "total_score": full["total_score"],
            "grade": full["grade"],
            "total_merit": merit_detail["total"],
        },
    }


def _do_submit_metrics(work_id: int, payload: CampaignMetricsIn, submitter: str = "",
                       request_key: str = "") -> dict:
    if not _has_cred(payload.screenshot):
        raise HTTPException(400, "请上传「后台数据截图」后再提交")
    # 写锁覆盖计数、评分、快照与去重写入；失败整体回滚。
    with database.atomic_cursor() as cur:
        row = _get_work(cur, work_id)
        if row['status'] not in ('pending_t7', 'qualified', 'eliminated'):
            raise HTTPException(409, '该作品当前不可回传')
        key = (request_key or '').strip()
        if key and len(key) > 128:
            raise HTTPException(400, '幂等键过长')
        fingerprint = hashlib.sha256(_json_dump(payload.model_dump(mode='json') if hasattr(payload, 'model_dump') else payload.dict()).encode()).hexdigest()
        if key:
            cur.execute('SELECT response FROM campaign_submit_dedup WHERE work_id=? AND request_key=?', (work_id, key))
            cached = cur.fetchone()
            if cached:
                return _json_load(cached['response'])
        cur.execute('SELECT COUNT(*) AS n FROM campaign_metrics_history WHERE work_id=?', (work_id,))
        count = cur.fetchone()['n']
        # 无幂等键的旧客户端以相同回传内容的短时窗口防止快连点。
        cur.execute('SELECT response, submitted_at FROM campaign_submit_dedup WHERE work_id=? AND request_key=?', (work_id, fingerprint))
        recent = cur.fetchone()
        now = int(time.time())
        if recent and now - recent['submitted_at'] < 30:
            return _json_load(recent['response'])
        if count >= 3:
            raise HTTPException(409, '该作品已达到 3 次回传上限')
        result = _do_submit_metrics_inner(work_id, payload, submitter)
        cur.execute('INSERT INTO campaign_submit_dedup (work_id, request_key, submitted_at, response) VALUES (?,?,?,?) '
                    'ON CONFLICT(work_id, request_key) DO UPDATE SET submitted_at=excluded.submitted_at, response=excluded.response',
                    (work_id, fingerprint, now, _json_dump(result)))
        if key and key != fingerprint:
            cur.execute('INSERT INTO campaign_submit_dedup (work_id, request_key, submitted_at, response) VALUES (?,?,?,?)',
                        (work_id, key, now, _json_dump(result)))
        return result


@router.post("/works/{work_id}/submit")
def submit_metrics(work_id: int, payload: CampaignMetricsIn, req: Request):
    user = auth_service.require_user(req)
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
    # 投稿人只能提交自己的作品
    if user.get("role") == "uploader" and row.get("creator_id") != user["id"]:
        raise HTTPException(403, "无权提交该作品（非本人作品）")
    return _do_submit_metrics(work_id, payload,
                              submitter=user.get("display_name") or user.get("username") or "",
                              request_key=req.headers.get('Idempotency-Key', ''))


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
    # 作品链接改为在回传环节必填（前端已拦，这里再兜一道，防止绕过前端直接打接口）
    work_url = (payload.get("url") or "").strip()
    if not work_url:
        raise HTTPException(400, "请填写作品链接后再提交")
    metrics = CampaignMetricsIn(
        plays=payload.get("plays"),
        retention_3s=payload.get("retention_3s"),
        completion_rate=payload.get("completion_rate"),
        comment_count=payload.get("comment_count"),
        like_count=payload.get("like_count"),
        share_count=payload.get("share_count"),
        screenshot=payload.get("screenshot"),
        business_proof=payload.get("business_proof"),
        original=payload.get("original", True),
        url=work_url,
        ocr_raw=payload.get("ocr_raw"),
    )
    return _do_submit_metrics(row["id"], metrics, submitter=name,
                              request_key=req.headers.get('Idempotency-Key', ''))


# ── 从「后台数据截图」AI 识别 T+7 六项指标（使用端回传页调用）──

_OCR_MAX_IMAGES = 3


@router.post("/ocr-metrics")
def ocr_metrics(payload: dict, req: Request):
    """识别投稿人上传的后台数据截图里的六项指标，用于回填回传表单。

    识别结果只是「预填 + 提示」，最终以用户确认或修改后的值为准；后端会同时
    记下 AI 原值与用户提交值，后台可比对抽查识别准确率。

    省钱关键：结果按「图片内容哈希」缓存。同一张截图只真正调一次模型——
    用户重复点、关掉表单重开再点、甚至删掉重传同一张图，都直接命中缓存。
    需要重试时由前端显式传 refresh=true 绕过缓存。
    """
    files = payload.get("files")
    if isinstance(files, str):
        files = [files]
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "请先上传「后台数据截图」再识别")
    keys = []
    for f in files[:_OCR_MAX_IMAGES]:
        k = ((f.get("file") if isinstance(f, dict) else str(f)) or "").strip()
        # 只允许读本系统的「后台数据截图」目录：这个接口免登录，
        # 不能变成任意对象的读取通道
        if _SCREENSHOT_KEY_RE.match(k):
            keys.append(k)
    if not keys:
        raise HTTPException(400, "截图还没上传完成，请稍候重试后再识别")

    pairs = []  # [(cos_key, bytes)]：下载失败的直接丢掉，避免 key 与内容错位
    for k in keys:
        try:
            raw = cos_service.download_bytes(k)
        except Exception as e:
            print(f"[campaign] 识别用截图下载失败 {k}: {e}", flush=True)
            raw = None
        if raw:
            pairs.append((k, raw))
    if not pairs:
        raise HTTPException(400, "截图读取失败，请重新上传后再试")

    # 缓存键：按图片内容算（多张按顺序拼接），同一张图无论换多少个 cos_key 都命中同一条
    h = hashlib.md5()
    for _k, b in pairs:
        h.update(b)
    img_hash = h.hexdigest()
    refresh = bool(payload.get("refresh"))

    if not refresh:
        with db_cursor() as cur:
            cur.execute("SELECT * FROM campaign_ocr_cache WHERE img_hash=?", (img_hash,))
            row = cur.fetchone()
        if row:
            return {"ok": True, "cached": True,
                    "metrics": _json_load(row["metrics"], {}) or {},
                    "model": row["model"] or "", "note": row["note"] or "", "error": None}

    data_urls = []
    for k, b in pairs:
        mime = _CRED_MIME.get(os.path.splitext(k)[1].lower(), "image/jpeg")
        data_urls.append(f"data:{mime};base64,{base64.b64encode(b).decode()}")

    r = campaign_scoring.extract_metrics_from_screenshots(data_urls)
    metrics = r.get("metrics") or {}
    # 只缓存真正识别出结果的：识别失败/全空的别写进缓存，否则用户重试也永远拿到空结果
    if r.get("error") is None and any(v is not None for v in metrics.values()):
        with db_cursor() as cur:
            cur.execute(
                """INSERT INTO campaign_ocr_cache (img_hash, metrics, model, note, created_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(img_hash) DO UPDATE SET
                     metrics=excluded.metrics, model=excluded.model,
                     note=excluded.note, created_at=excluded.created_at""",
                (img_hash, _json_dump(metrics), r.get("model") or "", r.get("note") or "",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
    return {
        "ok": r.get("error") is None,
        "cached": False,
        "metrics": metrics,
        "model": r.get("model") or "",
        "note": r.get("note") or "",
        "error": r.get("error"),
    }


# ── 管理员更正最新有效回传（历史快照不修改）──

@router.put('/works/{work_id}/metrics/correct')
def correct_metrics(work_id: int, payload: dict, req: Request):
    user = auth_service.require_admin(req)
    reason = str(payload.get('reason') or '').strip()
    changes = payload.get('metrics') or {}
    fields = dict(campaign_scoring.OCR_METRIC_DEFS)
    if not reason or not isinstance(changes, dict) or (not changes and 'screenshot' not in payload) or set(changes) - set(fields):
        raise HTTPException(400, '请填写更正理由及合法的六项指标')
    normalized = {}
    for key, value in changes.items():
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= (100 if key in ('retention_3s', 'completion_rate') else 1000000000):
            raise HTTPException(400, f'{key} 数值无效')
        if key in campaign_scoring.OCR_INT_KEYS and int(value) != value:
            raise HTTPException(400, f'{key} 必须是整数')
        normalized[key] = int(value) if key in campaign_scoring.OCR_INT_KEYS else float(value)
    screenshot = payload.get('screenshot')
    if screenshot is not None:
        items = screenshot if isinstance(screenshot, list) else [screenshot]
        if not items or any(not _SCREENSHOT_KEY_RE.fullmatch((it.get('file') if isinstance(it, dict) else it) or '') for it in items):
            raise HTTPException(400, '截图须为已上传的后台数据截图')
        try:
            if any(not cos_service.download_bytes((it.get('file') if isinstance(it, dict) else it)) for it in items):
                raise HTTPException(400, '截图不可读取')
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, '截图存储不可用')
    with database.atomic_cursor() as cur:
        work = _get_work(cur, work_id)
        cur.execute('SELECT * FROM campaign_metrics WHERE work_id=?', (work_id,))
        metric = cur.fetchone()
        cur.execute('SELECT * FROM campaign_metrics_history WHERE work_id=? ORDER BY seq DESC,id DESC LIMIT 1', (work_id,))
        hist = cur.fetchone()
        if not metric or not hist or not hist['qualified'] or not work['qualified'] or work['status'] != 'qualified':
            raise HTTPException(400, '仅可更正最新有效回传')
        cur.execute('SELECT * FROM campaign_scores WHERE work_id=?', (work_id,))
        score = cur.fetchone()
        if not score or score['total_score'] is None:
            raise HTTPException(400, '该作品尚未生成总分')
        before = {'metrics': dict(metric), 'score': dict(score)}
        if screenshot is not None:
            normalized['screenshot'] = _json_dump(screenshot)
        assignments = ', '.join(f'{k}=?' for k in normalized)
        cur.execute(f'UPDATE campaign_metrics SET {assignments} WHERE work_id=?', (*normalized.values(), work_id))
        cur.execute('SELECT * FROM campaign_metrics WHERE work_id=?', (work_id,))
        current = dict(cur.fetchone())
        ai = {'score': score['ai_content_score'], 'dims': _json_load(score['ai_dims'], {}), 'reason': score['ai_reason']}
        play_threshold, min_days = _campaign_thresholds()
        if (current['plays'] or 0) < play_threshold or _days_since(work['publish_time'] or work['created_at']) < min_days:
            raise HTTPException(400, '更正后的数据不满足参评门槛，请先核查作品资格')
        full = campaign_scoring.calc_full_score(current, ai)
        if score['manual_total_score'] is not None:
            full['total_score'] = score['manual_total_score']
        full['grade'] = campaign_scoring.grade_of(full['total_score'])
        total_merit, merit = campaign_scoring.calc_total_merit(full['grade'], _json_load(work['extra_flags'], {}))
        total_merit += int(score['manual_merit'] or 0)
        cur.execute('''UPDATE campaign_scores SET retention_score=?, retention_detail=?, interaction_score=?,
            interaction_detail=?, total_score=?, grade=?, base_merit=?, extra_merit=?, total_merit=? WHERE work_id=?''',
            (full['retention_score'], _json_dump(full['retention_detail']), full['interaction_score'],
             _json_dump(full['interaction_detail']), full['total_score'], full['grade'], merit['base'],
             _json_dump(merit), total_merit, work_id))
        cur.execute('SELECT * FROM campaign_scores WHERE work_id=?', (work_id,))
        after = {'metrics': current, 'score': dict(cur.fetchone())}
        cur.execute('''INSERT INTO campaign_metric_corrections
            (work_id, history_id, admin_id, reason, before_data, after_data) VALUES (?,?,?,?,?,?)''',
            (work_id, hist['id'], user['id'], reason, _json_dump(before), _json_dump(after)))
    return {'ok': True, 'metrics': current, 'score': after['score']}


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
        # 战功随等级刷新（附加战功与人工追加的战功保持不变）
        extra_flags = _json_load(row.get("extra_flags"), {})
        total_merit, merit_detail = campaign_scoring.calc_total_merit(new_grade, extra_flags)
        total_merit += int(s["manual_merit"] or 0)
        cur.execute(
            "UPDATE campaign_scores SET base_merit=?, total_merit=? WHERE work_id=?",
            (merit_detail["base"], total_merit, work_id),
        )

    return {"ok": True, "total_score": new_total, "grade": new_grade,
            "total_merit": total_merit, "ai_content_score": new_ai}


# ── 有效线索库：逐条凭证人工审核 ──

_CRED_REVIEW_STATUS = {"approve": "approved", "reject": "rejected", "reset": "pending"}


@router.post("/credentials/{cred_id}/review")
def review_credential(cred_id: int, payload: CampaignCredentialReviewIn, req: Request):
    """有效线索库：人工判定这条凭证「合格 / 不合格」。

    只改凭证自身的审核标记，不动作品的分数与战功——是否据此追加战功
    由后台单独操作（POST /works/{id}/merit），两件事分开留痕。
    """
    user = auth_service.require_manager(req)
    action = (payload.action or "").strip().lower()
    if action not in _CRED_REVIEW_STATUS:
        raise HTTPException(400, "action 需为 approve / reject / reset")
    status = _CRED_REVIEW_STATUS[action]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        cur.execute("SELECT * FROM campaign_credentials WHERE id=?", (cred_id,))
        c = cur.fetchone()
        if not c:
            raise HTTPException(404, "凭证不存在")
        cur.execute(
            "UPDATE campaign_credentials SET review_status=?, review_note=?, reviewed_by=?, reviewed_at=? "
            "WHERE id=?",
            (status, (payload.note or "").strip(), user["id"], now, cred_id),
        )
        row = _get_work(cur, c["work_id"])
        _log_work_op(cur, req, user, row, f"凭证 #{cred_id} 审核为「{status}」{(payload.note or '').strip()}")
    return {"ok": True, "id": cred_id, "review_status": status,
            "review_note": (payload.note or "").strip(), "reviewed_at": now}


# ── 有效线索库 / 作品：人工审核追加战功 ──

@router.post("/works/{work_id}/merit")
def adjust_work_merit(work_id: int, payload: CampaignMeritIn, req: Request):
    """人工审核追加战功：在不改系统评分的前提下调整该作品的战功总额。

    total_merit = 系统战功(基础+附加，上限 15) + 人工追加；人工部分不设上限，
    但会连同追加人、时间、理由一起落库，榜单口径不变（仍读 total_merit）。
    """
    user = auth_service.require_admin(req)
    if payload.delta is None:
        raise HTTPException(400, "请填写要追加的战功分值")
    try:
        delta = float(payload.delta)
    except (TypeError, ValueError):
        raise HTTPException(400, "战功分值必须是数字")
    if delta == 0:
        raise HTTPException(400, "追加分值不能为 0")
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(400, "请填写追加战功的理由")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        row = _get_work(cur, work_id)
        cur.execute("SELECT * FROM campaign_scores WHERE work_id=?", (work_id,))
        s = cur.fetchone()
        if not s:
            raise HTTPException(400, "该作品尚未打分，无法追加战功")
        # 系统战功 = min(基础 + 附加, 上限)，与 calc_total_merit 口径一致
        extra_obj = _json_load(s["extra_merit"], {}) or {}
        system_merit = min(int(s["base_merit"] or 0) + int(extra_obj.get("extra") or 0),
                           campaign_scoring.MERIT_CAP)
        manual = round(float(s["manual_merit"] or 0) + delta, 2)
        if manual < 0:
            manual = 0.0
        total = system_merit + manual
        cur.execute(
            "UPDATE campaign_scores SET manual_merit=?, manual_merit_reason=?, manual_merit_by=?, "
            "manual_merit_at=?, total_merit=? WHERE work_id=?",
            (manual, reason, user["id"], now, total, work_id),
        )
        _log_work_op(cur, req, user, row,
                     f"人工追加战功 {delta:+g}（{reason}），现总战功 {total:g}")
    return {"ok": True, "system_merit": system_merit, "manual_merit": manual, "total_merit": total}


# ── 凭证 / 传播记录 ──

@router.post("/works/{work_id}/credentials")
def add_credential(work_id: int, payload: CampaignCredentialIn, req: Request):
    """人工补录一条有效线索。凭证库只存这一类，其它类型一律拒收。"""
    auth_service.require_user(req)
    if (payload.type or "").strip() != "business":
        raise HTTPException(400, "凭证库只存「有效线索回传」（type=business）")
    with db_cursor() as cur:
        _get_work(cur, work_id)
        cur.execute(
            "INSERT INTO campaign_credentials (work_id, type, file, note) VALUES (?,?,?,?)",
            (work_id, "business", payload.file, payload.note),
        )
        return {"id": cur.lastrowid}


_CRED_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}

def _image_ext(data: bytes) -> str:
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in data[-16:]:
        return '.png'
    if data[:6] in (b'GIF87a', b'GIF89a') and data[-1:] == b';':
        return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP' and int.from_bytes(data[4:8], 'little') + 8 == len(data):
        return '.webp'
    return ''


# 一稿一码形态：BC + 12 位时间 + 6 位随机（如 BC250921103045A1B2C3）
_WORK_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 允许 OCR 读取的「后台数据截图」路径。新命名把作品码放在第二段，
# 旧命名是 campaign/credentials/screenshot/...，两种都放行。
_SCREENSHOT_KEY_RE = re.compile(
    r"^campaign/(?:"
    r"[A-Za-z0-9_-]{1,32}/screenshot/"   # 新：campaign/{作品码}/screenshot/...
    r"|credentials/screenshot/"          # 旧：campaign/credentials/screenshot/...
    r")"
)

# COS 目录：campaign/{作品码}/{用途}/{年月}/{毫秒时间戳}_{随机}.{ext}
#   screenshot = 后台数据截图（六项数字的来源凭证）
#   business   = 有效线索回传（选填，可用来申请额外战功）
# 按作品码归档，一个作品的所有材料都在同一目录下，方便核对与清理。
_CRED_PREFIX = "campaign"
_CRED_UNASSIGNED = "_unassigned"


def _cred_cos_key(code: str, ctype: str, filename_stub: str) -> str:
    """拼 COS 对象 key。作品码非法或缺失时落到 _unassigned/，不影响上传。"""
    code = (code or "").strip()
    if not _WORK_CODE_RE.match(code):
        code = _CRED_UNASSIGNED
    ym = datetime.now().strftime("%Y-%m")
    return f"{_CRED_PREFIX}/{code}/{ctype}/{ym}/{filename_stub}"


@router.post("/credentials/upload")
async def upload_credential_image(req: Request):
    """上传材料图片到 COS，返回 cos_key 与预签名 URL（前端回填到 T+7 提交）。

    命名规范：campaign/{作品码}/{screenshot|business}/{YYYY-MM}/{毫秒时间戳}_{随机}.{ext}
    公开接口：投稿人免登录回传 T+7 时也需要上传。
    """
    auth_service.try_get_user(req)
    try:
        form = await req.form(max_files=20, max_fields=10)
    except Exception as e:
        raise HTTPException(413, f"文件过大或不支持的格式: {e}")

    ctype = (form.get("type") or "").strip()
    if ctype not in ("screenshot", "business"):
        raise HTTPException(400, "type 只能是 screenshot 或 business")
    code = (form.get("code") or "").strip()

    files = [v for _, v in form.items() if isinstance(v, StarletteUploadFile) and v.filename]
    if not files:
        raise HTTPException(400, "请选择要上传的图片")

    if not cos_service.is_enabled():
        raise HTTPException(503, '图片存储暂不可用')
    results = []
    for f in files:
        data = await f.read(10 * 1024 * 1024 + 1)
        if not data or len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, '图片为空或超过 10MB')
        ext = _image_ext(data)
        if not ext:
            raise HTTPException(400, '图片格式不支持或文件已损坏')
        safe_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
        cos_key = _cred_cos_key(code, ctype, safe_name)
        try:
            uploaded = cos_service.upload_bytes(data, cos_key, _CRED_MIME[ext])
            readable = cos_service.download_bytes(cos_key) if uploaded == cos_key else None
            url = cos_service.presigned_url(cos_key, expires=7200) if readable == data else None
        except Exception:
            raise HTTPException(502, '图片存储失败，请重试')
        if not url:
            raise HTTPException(502, '图片存储校验失败，请重试')
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
    """有效线索库：只列「有效线索回传」（type=business），支持按区域筛选。

    后台数据截图不进这张表——它存在 campaign_metrics.screenshot 里，
    在「视频数据库」按作品查看。这里显式加 type 条件，避免历史遗留的
    screenshot 记录混进有效线索库。
    """
    user = auth_service.require_user(req)
    if user.get("role") == "uploader":
        scope_sql, scope_params = " AND w.creator_id=?", [user["id"]]
    else:
        scope_sql, scope_params = _region_scope(user)
    region_sql = ""
    if region:
        cond, p = _region_filter(region)
        region_sql = " AND " + cond
        scope_params.extend(p)
    with db_cursor() as cur:
        cur.execute(
            """SELECT c.*, w.code, w.title, w.creator_name, w.region,
                      COALESCE(c.seq, 1) AS seq,
                      COALESCE((SELECT MAX(h.seq) FROM campaign_metrics_history h
                                WHERE h.work_id=c.work_id), COALESCE(c.seq, 1)) AS total_seq,
                      w.status AS work_status,
                      (SELECT s.total_merit FROM campaign_scores s WHERE s.work_id=c.work_id) AS work_merit,
                      (SELECT s.manual_merit FROM campaign_scores s WHERE s.work_id=c.work_id) AS work_manual_merit,
                      (SELECT s.grade FROM campaign_scores s WHERE s.work_id=c.work_id) AS work_grade
               FROM campaign_credentials c JOIN campaign_works w ON w.id = c.work_id
               WHERE c.type='business'""" + scope_sql + region_sql + """
               ORDER BY c.id DESC LIMIT 500""",
            scope_params,
        )
        rows = [dict(r) for r in cur.fetchall()]
    for c in rows:
        c["url"] = _cred_url(c.get("file"))
    return rows


@router.get("/propagation")
def list_propagation(req: Request):
    """传播记录库：T+7 平台数据表现（播放/留存/完播/互动/点赞）。

    表格主体是每个作品「最新一次」回传的数据；submit_count / last_seq
    告诉后台这个作品一共回传过几次，点进详情可看历次明细。
    """
    user = auth_service.require_user(req)
    if user.get("role") == "uploader":
        scope_sql, scope_params = " AND w.creator_id=?", [user["id"]]
    else:
        scope_sql, scope_params = _region_scope(user)
    with db_cursor() as cur:
        cur.execute(
            """SELECT m.*, w.code, w.title, w.creator_name, w.track, w.url,
                      COALESCE((SELECT COUNT(*) FROM campaign_metrics_history h
                                WHERE h.work_id=m.work_id), 0) AS submit_count,
                      COALESCE((SELECT MAX(h.seq) FROM campaign_metrics_history h
                                WHERE h.work_id=m.work_id), 0) AS last_seq
               FROM campaign_metrics m JOIN campaign_works w ON w.id = m.work_id
               WHERE 1=1""" + scope_sql + """
               ORDER BY m.submitted_at DESC, m.work_id DESC LIMIT 500""",
            scope_params,
        )
        rows = []
        for r in cur.fetchall():
            p = dict(r)
            # 「数据截图」列要直接点开看大图：把 cos key 换成实时的预签名 URL
            p["screenshot_files"] = _hist_files(_json_load(p.get("screenshot"), []))
            rows.append(p)
        return rows


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


# ── 赛道人工调整 / 移出战役（管理后台）──

def _log_work_op(cur, req, user, work_row, detail: str) -> None:
    """把作品级人工操作写进 usage_log：谁、什么时候、对哪条作品做了什么。"""
    who = user.get("display_name") or user.get("username") or "unknown"
    cur.execute(
        "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) "
        "VALUES (?,?,?,?,?,?)",
        ("作品调整", who, "", req.client.host if req.client else "",
         work_row["submission_id"], f"作品 {work_row['code']}：{detail}"),
    )


@router.put("/works/{work_id}/track")
def update_work_track(work_id: int, payload: dict, req: Request):
    """人工调整作品赛道（AI 识别有误时纠正）。

    赛道只影响分组与赛道榜排名，不参与算分，所以改完无需重算分数，
    赛道榜/区域战绩下次查询即按新赛道统计。
    """
    user = auth_service.require_manager(req)
    new_track = (payload.get("track") or "").strip()
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if new_track not in valid_tracks:
        raise HTTPException(400, f"赛道不合法，可选：{' / '.join(valid_tracks)}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, code, track, submission_id FROM campaign_works WHERE id=?",
            (work_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "作品不存在")
        old_track = row["track"] or ""
        if old_track == new_track:
            return {"ok": True, "track": new_track, "unchanged": True}
        cur.execute(
            "UPDATE campaign_works SET track=?, updated_at=? WHERE id=?",
            (new_track, now, work_id),
        )
        _log_work_op(cur, req, user, row, f"调整赛道 {old_track or '(未设置)'} → {new_track}")
    return {"ok": True, "track": new_track, "old_track": old_track}


@router.put("/works/{work_id}/exclude")
def set_work_excluded(work_id: int, payload: dict, req: Request):
    """把作品移出/恢复战役。

    移出后不计分、不上榜（status=excluded，各榜单与区域战绩的过滤条件都排除它），
    原状态会被记下来，恢复时原样还原（否则 qualified / eliminated / pending_t7
    这几种状态无法区分）。
    """
    user = auth_service.require_manager(req)
    excluded = bool(payload.get("excluded"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db_cursor() as cur:
        cur.execute(
            "SELECT id, code, status, qualified, submission_id, extra_flags "
            "FROM campaign_works WHERE id=?",
            (work_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "作品不存在")
        cur_status = row["status"] or ""
        extra = _json_load(row["extra_flags"], {}) or {}
        if excluded:
            if cur_status == "excluded":
                return {"ok": True, "status": "excluded", "unchanged": True}
            extra["_pre_exclude_status"] = cur_status
            new_status = "excluded"
        else:
            if cur_status != "excluded":
                return {"ok": True, "status": cur_status, "unchanged": True}
            new_status = extra.pop("_pre_exclude_status", "") or (
                "qualified" if row["qualified"] else "pending_t7"
            )
        cur.execute(
            "UPDATE campaign_works SET status=?, extra_flags=?, updated_at=? WHERE id=?",
            (new_status, _json_dump(extra), now, work_id),
        )
        _log_work_op(cur, req, user, row,
                     ("移出战役" if excluded else "恢复参战") + f" {cur_status} → {new_status}")
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
            f"""SELECT w.track, w.region, s.total_merit, w.creator_id, w.creator_name, w.code, w.title
               FROM campaign_works w JOIN campaign_scores s ON s.work_id = w.id
               WHERE {' AND '.join(where)}""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    # 未分区（管理员没填区域）的作品不参与赛道榜的区域排名
    rows = [r for r in rows if _is_participating(r["region"])]

    result = {}
    for r in rows:
        track = r["track"]
        t = result.setdefault(track, {"track": track, "regions": {}, "total_merit": 0, "creator_ids": set()})
        t["total_merit"] += r["total_merit"] or 0
        t["creator_ids"].add(r["creator_id"] or r["creator_name"])
        region = r["region"] or _UNASSIGNED
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
        cond, p = _region_filter(region)
        where.append(cond)
        params.extend(p)
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.region, s.total_merit, s.grade, s.extra_merit, w.creator_id, w.creator_name
               FROM campaign_works w JOIN campaign_scores s ON s.work_id = w.id
               WHERE {' AND '.join(where)}""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    # 未分区（管理员没填区域）的作品不参与参战大区排名
    rows = [r for r in rows if _is_participating(r["region"])]

    zones = {}
    for r in rows:
        z = _big_region(r["region"])
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
        zone["valid_creators"].add(r["creator_id"] or r["creator_name"])
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
            f"""SELECT w.region, w.status, w.creator_id, w.creator_name, s.grade, s.total_merit, s.extra_merit
                FROM campaign_works w
                LEFT JOIN campaign_scores s ON s.work_id = w.id
                WHERE w.status NOT IN ('eliminated','disqualified','excluded'){scope_sql}""",
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

    # 未分区（管理员没填区域）的作品不计入 6 大区概览
    works = [w for w in works if _is_participating(w["region"])]

    zones = {}
    for r in works:
        z = _big_region(r["region"])
        zone = zones.setdefault(z, {
            "region": z, "total_merit": 0, "s_a_count": 0,
            "valid_creators": set(), "work_count": 0, "cross_region_count": 0,
        })
        zone["work_count"] += 1
        if r["total_merit"]:
            zone["total_merit"] += r["total_merit"]
        if r["grade"] in ("S", "A"):
            zone["s_a_count"] += 1
        zone["valid_creators"].add(r["creator_id"] or r["creator_name"])
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
        lead_count = sum(c for r, c in lead_map.items() if _big_region(r) == big)
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
    where, params = ["w.status NOT IN ('eliminated','disqualified','excluded')"], []
    if scope_sql:
        where.append(scope_sql.lstrip(" AND ").strip())
        params.extend(scope_params)
    if big:
        cond, p = _region_filter(big, prefix=True)
        where.append(cond)
        params.extend(p)
    where_sql = " AND ".join(where)
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT w.region, w.creator_id, w.creator_name, s.grade, s.total_merit
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

    # 未分区（管理员没填区域）的作品不出现在细分区域榜里
    works = [w for w in works if _is_participating(w["region"])]

    subs = {}
    for r in works:
        raw = (r["region"] or "").strip()
        # region 为空的作品统一显示为「未分区」；前端下钻时再传回这个名字，
        # 由 _region_filter 转成「region 为空」的查询条件。
        full = raw or _UNASSIGNED
        sub = full.split("/")[-1].strip() if "/" in full else full
        z = subs.setdefault(full, {
            "region": full, "sub_region": sub, "raw": raw,
            "total_merit": 0, "s_a_count": 0, "work_count": 0, "valid_creators": set(),
        })
        z["work_count"] += 1
        if r["total_merit"]:
            z["total_merit"] += r["total_merit"]
        if r["grade"] in ("S", "A"):
            z["s_a_count"] += 1
        z["valid_creators"].add(r["creator_id"] or r["creator_name"])

    out = []
    for full, z in subs.items():
        out.append({
            "region": full,
            "sub_region": z["sub_region"],
            "work_count": z["work_count"],
            "valid_creator_count": len(z["valid_creators"]),
            "s_a_count": z["s_a_count"],
            "total_merit": z["total_merit"],
            "lead_count": lead_map.get(z["raw"], 0),
        })
    out.sort(key=lambda x: x["total_merit"], reverse=True)
    return out


# ── 全国战况：区域战功榜三 Sheet 导出 ──

def _xlsx_col(n: int) -> str:
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _xlsx_value(value) -> str:
    if value is None:
        return '<c t="inlineStr"><is><t></t></is></c>'
    if isinstance(value, bool):
        return f'<c t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c t="inlineStr"><is><t>{text}</t></is></c>'


def _xlsx_sheet(rows: list) -> bytes:
    body = []
    for ridx, row in enumerate(rows, 1):
        cells = []
        for cidx, value in enumerate(row, 1):
            cell = _xlsx_value(value)
            cell = cell.replace('<c ', f'<c r="{_xlsx_col(cidx)}{ridx}"' + (' s="1"' if ridx == 1 else '') + ' ', 1)
            cells.append(cell)
        body.append(f'<row r="{ridx}">{"".join(cells)}</row>')
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>' + ''.join(body) + '</sheetData></worksheet>').encode()


def _build_three_sheet_xlsx(sheets: list) -> bytes:
    files = {}
    files['[Content_Types].xml'] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets)+1))
        + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
    files['_rels/.rels'] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    sheets_xml = ''.join(f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1))
    files['xl/workbook.xml'] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + sheets_xml + '</sheets></workbook>')
    files['xl/_rels/workbook.xml.rels'] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets)+1))
        + '<Relationship Id="rId99" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    files['xl/styles.xml'] = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="D9EAF7"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellXfs count="2"><xf/><xf fontId="1" fillId="1" applyFont="1" applyFill="1"/></cellXfs></styleSheet>')
    for i, (_, rows) in enumerate(sheets, 1):
        files[f'xl/worksheets/sheet{i}.xml'] = _xlsx_sheet(rows)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items(): z.writestr(name, content)
    return out.getvalue()


@router.get("/region-leaderboard-export")
def export_region_leaderboards(req: Request):
    """全国战况区域战功榜：大区、区域、全部明细三个 Sheet。"""
    user = auth_service.require_user(req)
    scope_sql, scope_params = _region_scope(user)
    scope = f" AND {scope_sql.lstrip(' AND ').strip()}" if scope_sql else ""
    with db_cursor() as cur:
        cur.execute(f"""SELECT w.id, w.region, w.code, w.title, w.track, w.creator_name, w.creator_id,
                              w.status, w.created_at, w.publish_time,
                              s.total_score, s.grade, s.total_merit, s.manual_merit,
                              s.base_merit, s.extra_merit
                         FROM campaign_works w
                         LEFT JOIN campaign_scores s ON s.work_id=w.id
                        WHERE 1=1{scope}
                        ORDER BY w.id DESC""", scope_params)
        works = [dict(r) for r in cur.fetchall()]
        cur.execute(f"""SELECT cr.work_id, COUNT(*) AS n
                         FROM campaign_credentials cr JOIN campaign_works w ON w.id=cr.work_id
                        WHERE cr.type='business'{scope}
                        GROUP BY cr.work_id""", scope_params)
        lead_map = {r['work_id']: r['n'] for r in cur.fetchall()}

    # 与全国战况 /api/campaign/region/overview、region/subregions 保持一致：
    # 待初审、待T+7、已计分都纳入区域统计；只有淘汰、清零、排除不纳入；
    # 未分区（管理员没填区域）的作品也不计入 6 大区统计。
    valid = [w for w in works
             if w['status'] not in ('eliminated', 'disqualified', 'excluded')
             and _is_participating(w['region'])]
    def big(region): return _big_region(region)
    def sub(region):
        raw = (region or '').strip() or _UNASSIGNED
        name = raw.split('/')[-1].strip() if '/' in raw else raw
        return _headcount_region(name)
    zone_map, sub_map = {}, {}
    for w in valid:
        bz, sr = big(w['region']), sub(w['region'])
        for key, target in [((bz,), zone_map), ((bz, sr), sub_map)]:
            x = target.setdefault(key, {'merit': 0, 'score': 0, 'works': 0, 'creators': set(), 'sa': 0, 'leads': 0})
            x['merit'] += w['total_merit'] or 0; x['score'] += w['total_score'] or 0; x['works'] += 1
            x['creators'].add(w['creator_id'] or w['creator_name'] or '')
            x['sa'] += 1 if w['grade'] in ('S','A') else 0; x['leads'] += lead_map.get(w['id'], 0)
    zone_rows = [["排名","大区","人数","总战功","人均战功","总分","人均得分","统计作品数","统计创作者数","S/A作品数","有效线索数"]]
    for i, (key, x) in enumerate(sorted(zone_map.items(), key=lambda p: (-p[1]['merit'], p[0])), 1):
        people = sum(_REGION_HEADCOUNTS.get(r, 0) for r in {_headcount_region(name) for name in _REGIONS_BY_ZONE.get(key[0], [])}) if '_REGIONS_BY_ZONE' in globals() else 0
        zone_rows.append([i, key[0], people, x['merit'], round(x['merit'] / people, 2) if people else 0, x['score'], round(x['score'] / people, 2) if people else 0, x['works'], len(x['creators']), x['sa'], x['leads']])
    sub_rows = [["大区","区域","人数","排名","总战功","人均战功","总分","人均得分","统计作品数","统计创作者数","S/A作品数","有效线索数"]]
    by_zone = {}
    for (bz, sr), x in sub_map.items(): by_zone.setdefault(bz, []).append((sr, x))
    for bz in sorted(by_zone):
        for i, (sr, x) in enumerate(sorted(by_zone[bz], key=lambda p: (-p[1]['merit'], p[0])), 1):
            people = _REGION_HEADCOUNTS.get(_headcount_region(sr), 0)
            sub_rows.append([bz, sr, people, i, x['merit'], round(x['merit'] / people, 2) if people else 0, x['score'], round(x['score'] / people, 2) if people else 0, x['works'], len(x['creators']), x['sa'], x['leads']])
    detail_rows = [["大区","区域","区域人数","作品ID","一稿一码","作品名称","创作者","赛道","作品状态","总分","等级","系统战功","人工追加战功","总战功","有效线索数","创建时间","发布时间"]]
    for w in sorted(works, key=lambda x: (big(x['region']), sub(x['region']), -(x['total_merit'] or 0), x['id'])):
        detail_rows.append([big(w['region']), sub(w['region']), _REGION_HEADCOUNTS.get(_headcount_region(sub(w['region'])), 0), w['id'], w['code'], w['title'], w['creator_name'], w['track'], w['status'], w['total_score'], w['grade'], (w['base_merit'] or 0) + (w['extra_merit'] or 0) if isinstance(w['extra_merit'], (int,float)) else w['base_merit'], w['manual_merit'], w['total_merit'], lead_map.get(w['id'], 0), w['created_at'], w['publish_time']])
    content = _build_three_sheet_xlsx([('大区战功榜', zone_rows), ('区域战功榜', sub_rows), ('全部明细', detail_rows)])
    return Response(content=content, media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename=region_leaderboards.xlsx'})


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
                       m.plays, m.retention_3s, m.completion_rate, m.comment_count, m.like_count,
                       m.share_count,
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
                     "评论个数", "爱心个数", "转发次数", "AI内容分", "留存分", "互动分", "总分", "等级", "战功", "状态"])
    for r in rows:
        writer.writerow([
            r["code"], r["track"], r["title"], r["region"], r["creator"],
            r["plays"], r["retention_3s"], r["completion_rate"], r["comment_count"],
            r["like_count"], r["share_count"], r["ai_content_score"], r["retention_score"],
            r["interaction_score"], r["total_score"], r["grade"], r["total_merit"], r["status"],
        ])
    csv_str = buf.getvalue()
    return Response(
        content="\ufeff" + csv_str,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=campaign_export.csv"},
    )


# ── 评分标准与参评门槛配置（管理后台可手动调整）──

@router.get("/threshold-config")
def get_threshold_config(req: Request):
    """读取双百参评门槛；默认保持当前 0 播放、0 天。"""
    auth_service.require_user(req)
    plays, days = _campaign_thresholds()
    return {"play_threshold": plays, "min_days": days}


@router.put("/threshold-config")
def update_threshold_config(payload: dict, req: Request):
    """保存双百参评门槛，仅管理员可修改。"""
    auth_service.require_admin(req)
    try:
        plays = max(0, int(payload.get("play_threshold", 0)))
        days = max(0, int(payload.get("min_days", 0)))
    except (TypeError, ValueError):
        raise HTTPException(400, "播放量门槛和发布天数必须是非负整数")
    database.set_setting("campaign_play_threshold", str(plays))
    database.set_setting("campaign_min_days", str(days))
    return {"ok": True, "play_threshold": plays, "min_days": days}


@router.get("/score-config")
def get_score_config(req: Request):
    """读取当前评分标准：每个指标的档位（阈值 → 分值）。"""
    auth_service.require_user(req)
    return campaign_scoring.score_config_for_api()


@router.put("/score-config")
def update_score_config(payload: dict, req: Request):
    """保存评分标准。

    只影响此后新提交的 T+7 打分；已计分作品不会自动回算，
    需要重算的作品得重新回传一次。
    """
    auth_service.require_manager(req)
    try:
        cfg = campaign_scoring.save_score_config(payload or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "config": cfg}


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
