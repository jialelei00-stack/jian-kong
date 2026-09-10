"""双百战役评分模块。

评分标准：好内容 = 内容表现 40% + 用户留存 30% + 互动传播 30%
  - 用户留存 30 分：3 秒留存(10) + 完播率(20)，按区间自动算分
  - 互动传播 30 分：深度互动率(20) + 点赞率(10)，按区间自动算分
  - 内容表现 40 分：调用多模态 AI，围绕「情理色诚」输出 0-40 分，支持人工复批改分

总分等级 & 基础战功：
  S(90-100)=10 / A(70-89)=6 / B(50-69)=3 / 待优化(<50)=0
附加战功：矩阵放大 +2 / 跨区复用 +2 / 结果导向 +1，单条作品战功上限 15 分。
"""

import os
import secrets
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import ai_auditor

# ── 四大赛道枚举 ──
TRACKS = ["益圆/传应品宣", "转假宣传", "足球小将", "门锁场景"]


def gen_code() -> str:
    """生成一稿一码唯一 ID。"""
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    rand = secrets.token_hex(3).upper()
    return f"BC{stamp}{rand}"

# ── 评分区间（默认值；按 [阈值%, 得分] 从高到低匹配）──
# 3秒留存 10 分：≥70→10 / 60-69→8 / 50-59→6 / <50→3
RETENTION_3S_BANDS: List[Tuple[float, int]] = [(70, 10), (60, 8), (50, 6), (0, 3)]
# 完播率 20 分：≥35→20 / 25-34→16 / 15-24→12 / <15→6
COMPLETION_BANDS: List[Tuple[float, int]] = [(35, 20), (25, 16), (15, 12), (0, 6)]
# 深度互动率 20 分：≥3→20 / 2-2.9→16 / 1-1.9→12 / <1→6
DEEP_INTERACTION_BANDS: List[Tuple[float, int]] = [(3, 20), (2, 16), (1, 12), (0, 6)]
# 点赞率 10 分：≥5→10 / 3-4.9→8 / 1.5-2.9→6 / <1.5→3
LIKE_BANDS: List[Tuple[float, int]] = [(5, 10), (3, 8), (1.5, 6), (0, 3)]

# 附加战功定义：(key, 名称, 分值)
EXTRA_MERIT_DEFS: List[Tuple[str, str, int]] = [
    ("matrix_amplify", "矩阵放大", 2),
    ("cross_region", "跨区复用", 2),
    ("result_oriented", "结果导向", 1),
]
MERIT_CAP = 15

# 等级 → 基础战功
GRADE_BASE_MERIT = {"S": 10, "A": 6, "B": 3, "待优化": 0}

# 情理色诚四维度：(key, 简称, 说明)
QUALITY_DIMS = [
    ("情", "有共鸣", "能否引发观众情感共鸣"),
    ("理", "有获得", "是否有信息增量/实用价值，让观众有所收获"),
    ("色", "愿意看", "画面/节奏是否吸引人，让人愿意看下去"),
    ("诚", "真可信", "内容是否真实可信，不虚假夸大"),
]


def _score_by_bands(rate: Optional[float], bands: List[Tuple[float, int]]) -> int:
    """按区间给指标算分：命中第一个 rate >= threshold 的档位。"""
    if rate is None:
        return 0
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return 0
    for threshold, score in bands:
        if rate >= threshold:
            return score
    return 0


def calc_retention_score(retention_3s, completion_rate) -> Tuple[int, Dict[str, int]]:
    """用户留存 30 分：3 秒留存(10) + 完播率(20)。"""
    s1 = _score_by_bands(retention_3s, RETENTION_3S_BANDS)
    s2 = _score_by_bands(completion_rate, COMPLETION_BANDS)
    return s1 + s2, {"retention_3s": s1, "completion": s2}


def calc_interaction_score(deep_interaction_rate, like_rate) -> Tuple[int, Dict[str, int]]:
    """互动传播 30 分：深度互动率(20) + 点赞率(10)。"""
    s1 = _score_by_bands(deep_interaction_rate, DEEP_INTERACTION_BANDS)
    s2 = _score_by_bands(like_rate, LIKE_BANDS)
    return s1 + s2, {"deep_interaction": s1, "like": s2}


def grade_of(total_score: float) -> str:
    """根据总分返回等级 S/A/B/待优化。"""
    if total_score >= 90:
        return "S"
    if total_score >= 70:
        return "A"
    if total_score >= 50:
        return "B"
    return "待优化"


def base_merit_of(grade: str) -> int:
    return GRADE_BASE_MERIT.get(grade, 0)


def calc_total_merit(grade: str, extra_flags: Dict[str, bool]) -> Tuple[int, Dict]:
    """计算总战功（基础 + 附加，上限 MERIT_CAP）。"""
    base = base_merit_of(grade)
    extra_detail = {}
    extra = 0
    for key, label, pts in EXTRA_MERIT_DEFS:
        on = bool(extra_flags.get(key))
        extra_detail[key] = {"label": label, "points": pts, "on": on}
        if on:
            extra += pts
    total = min(base + extra, MERIT_CAP)
    return total, {"base": base, "extra": extra, "total": total, "detail": extra_detail}


def _video_frame_data_url(m: Dict) -> Optional[str]:
    """从视频 media 项抽取封面帧并转 data URL（不把 .mp4 当图片发送）。"""
    vlocal = m.get("video_local") or m.get("path") or ""
    if not vlocal:
        return None
    vpath = ""
    if os.path.isabs(vlocal) and os.path.exists(vlocal):
        vpath = vlocal
    else:
        for base in (ai_auditor.UPLOAD_DIR, ai_auditor.MEDIA_DIR):
            name = vlocal
            if "/media/" in name:
                name = name.split("/media/")[-1]
            elif "uploads/" in name:
                name = name.split("uploads/")[-1]
            else:
                name = os.path.basename(name)
            cand = os.path.join(base, name) if name else ""
            if cand and os.path.exists(cand):
                vpath = cand
                break
    if not vpath or not os.path.exists(vpath):
        return None
    frame = vpath + ".cover.jpg"
    if not os.path.exists(frame):
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-i", vpath, "-vframes", "1", "-ss", "1", "-q:v", "5", frame],
                capture_output=True, timeout=15,
            )
        except Exception:
            return None
    if os.path.exists(frame):
        return ai_auditor._local_image_data_url(frame)
    return None


def _video_url(m: Dict) -> Optional[str]:
    """生成视频的可访问 URL（COS 预签名 / 本地 HTTP），供 AI 直接读取完整视频。"""
    cos_key = m.get("cos_key") or ""
    if cos_key and ai_auditor.cos_service.is_enabled():
        try:
            u = ai_auditor.cos_service.presigned_url(cos_key, expires=7200)
            if u:
                return u
        except Exception:
            pass
    cu = m.get("cos_url") or ""
    if cu and cu.startswith("http"):
        return cu
    vlocal = m.get("video_local") or m.get("path") or ""
    if vlocal:
        vpath = ""
        if os.path.isabs(vlocal) and os.path.exists(vlocal):
            vpath = vlocal
        else:
            for base in (ai_auditor.UPLOAD_DIR, ai_auditor.MEDIA_DIR):
                name = vlocal
                if "/media/" in name:
                    name = name.split("/media/")[-1]
                elif "uploads/" in name:
                    name = name.split("uploads/")[-1]
                else:
                    name = os.path.basename(name)
                cand = os.path.join(base, name) if name else ""
                if cand and os.path.exists(cand):
                    vpath = cand
                    break
        if vpath and os.path.exists(vpath):
            vname = os.path.basename(vpath)
            base_url = os.environ.get("SERVER_BASE_URL", "http://127.0.0.1:8000")
            if "uploads" in vpath:
                return f"{base_url}/uploads/{vname}"
            return f"{base_url}/media/{vname}"
    return None


def ai_score_content_quality(caption: str, media_list) -> Dict:
    """调用多模态 AI，围绕「情理色诚」输出 0-40 分。

    返回：{"score": int, "dims": {...}, "reason": str, "error": str|None}
    """
    if not ai_auditor.is_ready():
        return {"score": 0, "dims": {}, "reason": "", "error": "AI 模型未配置"}

    cfg = ai_auditor.load_config()

    # 准备媒体：视频发完整 video_url，图片转 data URL；绝不把 .mp4 视频文件当图片发送
    image_urls: List[str] = []
    video_urls: List[str] = []
    for m in (media_list or []):
        if not isinstance(m, dict):
            continue
        if m.get("is_video"):
            vu = _video_url(m)
            if vu:
                video_urls.append(vu)
                continue
            # 无可用视频 URL 时抽封面帧兜底
            du = ai_auditor._local_image_data_url(m.get("thumb") or "")
            if not du:
                du = _video_frame_data_url(m)
            if not du and m.get("thumb_remote"):
                du = m["thumb_remote"]
            if du:
                image_urls.append(du)
        else:
            thumb = m.get("thumb") or m.get("path") or ""
            du = ai_auditor._local_image_data_url(thumb) if thumb else None
            if not du and m.get("thumb_remote"):
                du = m["thumb_remote"]
            if du:
                image_urls.append(du)
    image_urls = image_urls[:8]
    video_urls = video_urls[:4]

    sys_prompt = (
        "你是短视频营销内容质量评审专家，围绕「情理色诚」四个维度给作品打分。\n"
        "四个维度（每维 0-10 分，合计 0-40 分）：\n"
        "- 情·动之以情（有共鸣）：情绪牵引，让用户「有感觉」而不只是「知道」。自检：用户看到后心里有没有被触动？关键词：有共鸣。\n"
        "- 理·晓之以理（有获得）：用事实和数据给用户「必须选我」的理由。自检：用户有没有「不得不选」的理由？关键词：有获得。\n"
        "- 色·诱之以色（愿意看）：第一眼抓不住就难赢得关注。自检：用户愿不愿意停下来多看一秒？关键词：愿意看。\n"
        "- 诚·示之以诚（真可信）：不夸大、不隐瞒、不玩文字游戏，让用户感受到在替他考虑。自检：用户有没有感受到「你在替他考虑」？关键词：真可信。\n"
        "评分要求（务必遵守）：\n"
        "1. 结合文案与画面真实质量逐条独立打分，四个维度要有区分度，不要都打接近的分数。\n"
        "2. 单维拉开档次：优秀 8-10、良好 6-7、一般 4-5、差 0-3。\n"
        "3. total 必须等于四个维度分数之和，禁止套用固定分数或任何示例数值。\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"dims":{"情":<0-10整数>,"理":<0-10整数>,"色":<0-10整数>,"诚":<0-10整数>},"total":<四维之和>,"reason":"<一句话理由>"}'
    )

    user_parts: list = [
        {"type": "text", "text": f"【作品文案】\n{caption or '（无文案）'}\n\n请结合文案与视频/画面，客观打分。"}
    ]
    for idx, vu in enumerate(video_urls):
        user_parts.append({"type": "text", "text": f"[完整视频 {idx+1}/{len(video_urls)}] 请完整观看此视频的画面、声音与字幕后打分"})
        user_parts.append({"type": "video_url", "video_url": {"url": vu}})
    for du in image_urls:
        user_parts.append({"type": "image_url", "image_url": {"url": du}})

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_parts},
    ]

    try:
        raw = ai_auditor._call_model(cfg, messages)
    except Exception as e:
        return {"score": 0, "dims": {}, "reason": "", "error": f"AI 调用失败：{e}"}

    data = ai_auditor._extract_json(raw) or {}
    dims = data.get("dims") if isinstance(data.get("dims"), dict) else {}
    total = data.get("total")
    if total is None:
        try:
            total = sum(int(v) for v in dims.values())
        except Exception:
            total = 0
    try:
        total = max(0, min(40, int(total)))
    except (TypeError, ValueError):
        total = 0

    return {
        "score": total,
        "dims": dims,
        "reason": str(data.get("reason") or "").strip(),
        "error": None,
    }


def calc_full_score(metrics: Dict, ai_quality: Dict) -> Dict:
    """组合全部评分，返回完整打分明细。

    metrics 需含：retention_3s / completion_rate / deep_interaction_rate / like_rate
    """
    retention_total, retention_detail = calc_retention_score(
        metrics.get("retention_3s"), metrics.get("completion_rate")
    )
    interaction_total, interaction_detail = calc_interaction_score(
        metrics.get("deep_interaction_rate"), metrics.get("like_rate")
    )
    ai_score = int(ai_quality.get("score") or 0)
    total = ai_score + retention_total + interaction_total
    grade = grade_of(total)

    return {
        "ai_content_score": ai_score,
        "ai_dims": ai_quality.get("dims") or {},
        "ai_reason": ai_quality.get("reason") or "",
        "retention_score": retention_total,
        "retention_detail": retention_detail,
        "interaction_score": interaction_total,
        "interaction_detail": interaction_detail,
        "total_score": total,
        "grade": grade,
    }
