"""双百战役评分模块。

评分标准：好内容 = 内容表现 40% + 用户留存 30% + 互动传播 30%
  - 用户留存 30 分：3 秒留存(10) + 完播率(20)，按区间自动算分
  - 互动传播 30 分：评论个数(10) + 爱心个数(10) + 转发次数(10)，按区间自动算分
    （三项填的都是「个数」；转发率 = 转发次数÷播放量 只作展示，不参与打分）
  - 内容表现 40 分：调用多模态 AI，围绕「情理色诚」输出 0-40 分，支持人工复批改分

上面各指标的「区间 → 分值」不是写死的：实际取值来自 settings 表里的
campaign_score_config，可在管理后台直接改，下面 DEFAULT_SCORE_CONFIG 只作默认值。
（历史口径变更：2026-09 起「深度互动率%」改叫「评论个数」、「点赞率%」改叫「爱心个数」）

总分等级 & 基础战功（等级分数线与各级战功同样可在后台调整）：
  默认 S(≥90)=10 分战功 / A(≥70)=6 / B(≥50)=3 / 待优化(<50)=0
附加战功：矩阵放大 +2 / 跨区复用 +2 / 结果导向 +1，单条作品的系统战功上限 15 分；
人工审核追加的战功另计，不占这个上限。
"""

import json
import os
import re
import secrets
import subprocess
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import ai_auditor
import database

# ── 四大赛道枚举 ──
TRACKS = ["益圆/传应品宣", "转假宣传", "足球小将", "门锁场景"]


def gen_code() -> str:
    """生成一稿一码唯一 ID。"""
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    rand = secrets.token_hex(3).upper()
    return f"BC{stamp}{rand}"

# ── 评分区间默认值（可在管理后台改；按 [阈值, 得分] 从高到低匹配）──
# 阈值单位随指标而定：留存/完播是百分数，评论/爱心是「个数」，转发是百分数（转发率）
# 3秒留存 10 分：≥70→10 / 60-69→8 / 50-59→6 / <50→3
RETENTION_3S_BANDS: List[Tuple[float, int]] = [(70, 10), (60, 8), (50, 6), (0, 3)]
# 完播率 20 分：≥35→20 / 25-34→16 / 15-24→12 / <15→6
COMPLETION_BANDS: List[Tuple[float, int]] = [(35, 20), (25, 16), (15, 12), (0, 6)]
# 评论个数 10 分：≥30→10 / 20-29→7 / 10-19→5 / 5-9→3 / <5→0
COMMENT_BANDS: List[Tuple[float, int]] = [(30, 10), (20, 7), (10, 5), (5, 3), (0, 0)]
# 爱心个数 10 分：≥120→10 / 80-119→7 / 40-79→5 / 20-39→3 / <20→0
HEART_BANDS: List[Tuple[float, int]] = [(120, 10), (80, 7), (40, 5), (20, 3), (0, 0)]
# 转发次数 10 分：≥100→10 / 80-99→8 / 60-79→6 / 30-59→3 / <30→0
SHARE_BANDS: List[Tuple[float, int]] = [(100, 10), (80, 8), (60, 6), (30, 3), (0, 0)]

# 指标元数据：(配置键, 显示名, 单位, 所属分组)
# 评论 / 爱心 / 转发填的都是「个数」，直接套档位；转发率只作为展示信息，不参与打分。
SCORE_METRIC_DEFS: List[Tuple[str, str, str, str]] = [
    ("retention_3s", "3 秒留存率", "%", "用户留存"),
    ("completion", "完播率", "%", "用户留存"),
    ("comment", "评论个数", "个", "互动传播"),
    ("heart", "爱心个数", "个", "互动传播"),
    ("share", "转发次数", "次", "互动传播"),
]

# T+7 六项指标：(字段名, 中文名)。既用于「后台截图 AI 识别」的提示词与回显，
# 也用于后台比对「AI 识别成多少 / 用户最终提交了多少」。
OCR_METRIC_DEFS: List[Tuple[str, str]] = [
    ("plays", "自然播放量"),
    ("retention_3s", "3 秒留存率"),
    ("completion_rate", "完播率"),
    ("comment_count", "评论个数"),
    ("like_count", "爱心个数"),
    ("share_count", "转发次数"),
]

# 六项里属于「个数」的字段，识别结果要取整；其余是百分数，保留两位小数
OCR_INT_KEYS = ("plays", "comment_count", "like_count", "share_count")

DEFAULT_SCORE_CONFIG: Dict[str, List[Tuple[float, int]]] = {
    "retention_3s": RETENTION_3S_BANDS,
    "completion": COMPLETION_BANDS,
    "comment": COMMENT_BANDS,
    "heart": HEART_BANDS,
    "share": SHARE_BANDS,
}

SCORE_CONFIG_KEY = "campaign_score_config"


def _normalize_bands(bands) -> Optional[List[Tuple[float, int]]]:
    """把外部传入的档位规整成按阈值降序的 [(阈值, 分值)]；无有效项返回 None。"""
    if not isinstance(bands, list):
        return None
    out: List[Tuple[float, int]] = []
    for item in bands:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        try:
            threshold = float(item[0])
            score = int(float(item[1]))
        except (TypeError, ValueError):
            continue
        if score < 0:
            continue
        out.append((threshold, score))
    if not out:
        return None
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def load_score_config() -> Dict[str, List[Tuple[float, int]]]:
    """读取评分标准；未配置或内容损坏时逐项回退到默认档位。"""
    raw = database.get_setting(SCORE_CONFIG_KEY, "") or ""
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
    cfg: Dict[str, List[Tuple[float, int]]] = {}
    for key, *_rest in SCORE_METRIC_DEFS:
        bands = _normalize_bands(saved.get(key)) if isinstance(saved, dict) else None
        cfg[key] = bands or list(DEFAULT_SCORE_CONFIG[key])
    return cfg


def _normalize_grade_bands(bands) -> Optional[List[Tuple[str, float]]]:
    """规整「等级 → 分数线」为按阈值降序的 [(等级, 阈值)]；等级只认 S/A/B。"""
    if not isinstance(bands, list):
        return None
    out: List[Tuple[str, float]] = []
    for item in bands:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        name = str(item[0]).strip().upper()
        if name not in ("S", "A", "B"):
            continue
        try:
            threshold = float(item[1])
        except (TypeError, ValueError):
            continue
        out.append((name, threshold))
    if not out:
        return None
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _normalize_grade_merit(merit) -> Optional[Dict[str, int]]:
    """规整「等级 → 基础战功」；「待优化」固定 0 分，缺项回退默认值。"""
    if not isinstance(merit, dict):
        return None
    out: Dict[str, int] = {}
    for name, fallback in DEFAULT_GRADE_MERIT.items():
        raw = merit.get(name, fallback)
        try:
            val = int(float(raw))
        except (TypeError, ValueError):
            val = fallback
        out[name] = max(0, val)
    out["待优化"] = 0
    return out


def load_grade_config() -> Tuple[List[Tuple[str, float]], Dict[str, int]]:
    """读取等级配置：返回 (等级阈值(降序), 各级基础战功)。"""
    raw = database.get_setting(SCORE_CONFIG_KEY, "") or ""
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
    if not isinstance(saved, dict):
        saved = {}
    bands = _normalize_grade_bands(saved.get("grades")) or list(DEFAULT_GRADE_BANDS)
    merit = _normalize_grade_merit(saved.get("grade_merit")) or dict(DEFAULT_GRADE_MERIT)
    return bands, merit


def save_score_config(payload: Dict) -> Dict:
    """校验并保存评分标准（含等级阈值与基础战功），返回规整后的配置。"""
    clean: Dict = {}
    for key, label, *_rest in SCORE_METRIC_DEFS:
        bands = _normalize_bands((payload or {}).get(key))
        if not bands:
            raise ValueError(f"「{label}」的评分档位不能为空，至少要保留一行")
        clean[key] = [[t, s] for t, s in bands]

    grade_bands = _normalize_grade_bands((payload or {}).get("grades"))
    if not grade_bands:
        raise ValueError("「等级分数线」不能为空，至少要保留 S / A / B 三行")
    if len(grade_bands) != 3:
        raise ValueError("「等级分数线」必须正好是 S / A / B 三行")
    clean["grades"] = [[name, thr] for name, thr in grade_bands]

    grade_merit = _normalize_grade_merit((payload or {}).get("grade_merit"))
    if not grade_merit:
        raise ValueError("「各级基础战功」不能为空")
    clean["grade_merit"] = grade_merit

    database.set_setting(SCORE_CONFIG_KEY, json.dumps(clean, ensure_ascii=False))
    return clean


def score_config_for_api() -> Dict:
    """给管理后台用的完整配置视图：含指标中文名、单位、分组、当前档位与等级配置。"""
    cfg = load_score_config()
    grade_bands, grade_merit = load_grade_config()
    return {
        "metrics": [
            {
                "key": key,
                "label": label,
                "unit": unit,
                "group": group,
                "bands": [[t, s] for t, s in cfg[key]],
            }
            for key, label, unit, group in SCORE_METRIC_DEFS
        ],
        "grades": [[name, thr] for name, thr in grade_bands],
        "grade_merit": {name: grade_merit.get(name, 0) for name in GRADE_NAMES},
    }

# 附加战功定义：(key, 名称, 分值)
EXTRA_MERIT_DEFS: List[Tuple[str, str, int]] = [
    ("matrix_amplify", "矩阵放大", 2),
    ("cross_region", "跨区复用", 2),
    ("result_oriented", "结果导向", 1),
]
MERIT_CAP = 15

# 等级 → 基础战功（默认值，可在管理后台「评分标准」里改）
GRADE_BASE_MERIT = {"S": 10, "A": 6, "B": 3, "待优化": 0}
DEFAULT_GRADE_MERIT: Dict[str, int] = dict(GRADE_BASE_MERIT)

# 等级名称（固定四种）
GRADE_NAMES = ["S", "A", "B", "待优化"]

# 等级分数线默认值：总分 ≥ 阈值即得该等级（按阈值从高到低匹配）
DEFAULT_GRADE_BANDS: List[Tuple[str, float]] = [("S", 90), ("A", 70), ("B", 50)]

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


def calc_share_rate(share_count, plays) -> Optional[float]:
    """转发率 % = 转发次数 / 自然播放量 × 100。

    只用于详情页/后台展示，**不参与打分**（2026-09 起转发改为直接按次数套档位）。
    未填转发次数（None）时返回 None；播放量为 0 无法换算，同样返回 None。
    """
    if share_count is None:
        return None
    try:
        p = int(plays or 0)
        s = int(share_count)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return round(s / p * 100, 3)


def calc_retention_score(retention_3s, completion_rate, cfg=None) -> Tuple[int, Dict[str, int]]:
    """用户留存 30 分：3 秒留存 + 完播率（档位可在后台调整）。"""
    cfg = cfg or load_score_config()
    s1 = _score_by_bands(retention_3s, cfg["retention_3s"])
    s2 = _score_by_bands(completion_rate, cfg["completion"])
    return s1 + s2, {"retention_3s": s1, "completion": s2}


def calc_interaction_score(comment_count, like_count, share_count=None,
                           cfg=None) -> Tuple[int, Dict[str, int]]:
    """互动传播 30 分：评论个数 + 爱心个数 + 转发次数（档位可在后台调整）。

    这三项填的都是「个数」，直接套档位。转发率（次数÷播放量）只作展示，不参与打分。
    """
    cfg = cfg or load_score_config()
    s1 = _score_by_bands(comment_count, cfg["comment"])
    s2 = _score_by_bands(like_count, cfg["heart"])
    s3 = _score_by_bands(share_count, cfg["share"])
    return s1 + s2 + s3, {"comment": s1, "heart": s2, "share": s3}


def grade_of(total_score: float, bands: Optional[List[Tuple[str, float]]] = None) -> str:
    """根据总分返回等级 S/A/B/待优化；分数线可在管理后台调整。"""
    bands = bands if bands is not None else load_grade_config()[0]
    try:
        total_score = float(total_score)
    except (TypeError, ValueError):
        return "待优化"
    for name, threshold in bands:
        if total_score >= threshold:
            return name
    return "待优化"


def base_merit_of(grade: str, merit: Optional[Dict[str, int]] = None) -> int:
    merit = merit if merit is not None else load_grade_config()[1]
    return int(merit.get(grade, 0))


def calc_total_merit(grade: str, extra_flags: Dict[str, bool],
                     merit: Optional[Dict[str, int]] = None) -> Tuple[int, Dict]:
    """计算总战功（基础 + 附加，上限 MERIT_CAP）。人工追加的战功由调用方另加。"""
    base = base_merit_of(grade, merit)
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
    vpath = ai_auditor.resolve_local_path(m.get("video_local") or m.get("path") or "")
    if not vpath:
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


def _collect_ai_media(media_list):
    """收集供多模态 AI 读取的媒体。

    视频发完整 video_url，图片转 data URL / 远程图；绝不把 .mp4 视频文件当图片发送。
    媒体地址解析统一复用 ai_auditor 的公共实现，避免与审核链路各写一套。
    返回 (video_urls, image_urls)。
    """
    image_urls: List[str] = []
    video_urls: List[str] = []
    for m in (media_list or []):
        if not isinstance(m, dict):
            continue
        if m.get("is_video"):
            # 超模型体积上限时，video_url 一定被拒（400 Failed to download），
            # 必须先判体积再决定送视频还是送多帧画面，否则整段内容都看不到。
            if not ai_auditor.video_exceeds_limit(m):
                vu = ai_auditor.media_public_url(m)
                if vu:
                    video_urls.append(vu)
                    continue
            # 超限（或没有可用视频 URL）：抽多帧关键画面兜底
            vpath = ai_auditor.resolve_local_path(m.get("video_local") or m.get("path") or "")
            # 本地副本可能已被回收，退回 COS 预签名 URL 让 ffmpeg 按需抽帧
            frame_src = vpath or ai_auditor.media_remote_url(m) or ""
            frames = ai_auditor.extract_video_frames(frame_src) if frame_src else []
            if frames:
                image_urls.extend(frames)
                continue
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
    return video_urls[:4], image_urls[:8]


def _ocr_number(v) -> Optional[float]:
    """把模型读到的「12.3万 / 62.5% / 1,234」这类文本转成数字；读不出返回 None。

    模型经常把「万」这种单位原样吐回来，也可能带上 % 号或千分位逗号，
    统一在这里洗净，避免脏值直接进库。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s:
        return None
    mult = 1.0
    if s.endswith(("万", "w", "W")):
        mult, s = 10000.0, s[:-1]
    elif s.endswith("%"):
        s = s[:-1]
    # 取最后一个数字：「3秒留存率 62.5」这类带标签的文本，数值在后面
    nums = re.findall(r"-?\d+(?:\.\d+)?", s)
    if not nums:
        return None
    return float(nums[-1]) * mult


def extract_metrics_from_screenshots(image_urls: List[str]) -> Dict:
    """让多模态模型从「后台数据截图」里识别 T+7 六项指标。

    只做识别、不做判断：读不到的项一律返回 None，由投稿人手工补，
    绝不瞎猜一个数填进去（错数比空着更危险）。
    返回 {"metrics": {key: 数值|None}, "model": str, "error": str|None}
    """
    if not image_urls:
        return {"metrics": {}, "model": "", "error": "没有可识别的截图"}
    if not ai_auditor.is_ready():
        return {"metrics": {}, "model": "", "error": "AI 模型未配置"}

    cfg = ai_auditor.load_config()
    sys_prompt = (
        "你是数据录入助手。用户会给你一张短视频平台「创作者后台/数据中心」的数据截图，"
        "请把下面六项指标读出来。\n"
        "字段与常见叫法：\n"
        "- plays：自然播放量 / 播放量 / 播放次数（整数，遇到「12.3万」要换算成 123000）\n"
        "- retention_3s：3秒留存率 / 3s完播率 / 3秒播放完成率（百分数，只填数字，如 62.5）\n"
        "- completion_rate：完播率 / 播放完成率（百分数，只填数字）\n"
        "- comment_count：评论数 / 评论个数 / 评论量（整数，是「条数」不是比率）\n"
        "- like_count：爱心数 / 点赞数 / 点赞量 / 喜欢数（整数，是「个数」不是比率）\n"
        "- share_count：转发次数 / 分享次数 / 转发数（整数）\n"
        "铁律：\n"
        "1. 只在截图上确实找到该项时才填，找不到就填 null，严禁估算、换算或编造。\n"
        "2. comment_count / like_count 要的是「个数」：若图上只有「评论率」「点赞率」这类百分比，"
        "填 null，不要拿百分比当个数。\n"
        "3. 不要把「收藏数」当爱心、不要把「转发」当评论。\n"
        "4. 百分数只填数字本身，不要带 % 号。\n"
        "5. 截图模糊看不清的项，填 null。\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"plays":<整数或null>,"retention_3s":<数字或null>,"completion_rate":<数字或null>,'
        '"comment_count":<整数或null>,"like_count":<整数或null>,"share_count":<整数或null>,'
        '"note":"<一句话说明哪几项没读出来，读全了写 ok>"}'
    )
    user_parts: list = [{"type": "text", "text": "请识别这张后台数据截图里的六项指标。"}]
    for u in image_urls:
        user_parts.append({"type": "image_url", "image_url": {"url": u}})

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_parts},
    ]
    try:
        raw = ai_auditor._call_model(cfg, messages)
    except Exception as e:
        return {"metrics": {}, "model": cfg.get("model", ""), "error": f"AI 调用失败：{e}"}

    data = ai_auditor._extract_json(raw)
    if not isinstance(data, dict):
        return {"metrics": {}, "model": cfg.get("model", ""), "error": "AI 返回内容无法解析"}

    metrics: Dict[str, Optional[float]] = {}
    for key, _label in OCR_METRIC_DEFS:
        n = _ocr_number(data.get(key))
        if n is None:
            metrics[key] = None
        elif key in OCR_INT_KEYS:      # 播放量/评论/爱心/转发都是「个数」，必须是整数
            metrics[key] = int(round(n))
        else:
            metrics[key] = round(n, 2)
    return {"metrics": metrics, "model": cfg.get("model", ""),
            "note": str(data.get("note") or "").strip(), "error": None}


_QUALITY_DIM_KEYS = [d[0] for d in QUALITY_DIMS]


def _salvage_dim_scores(raw: str) -> Dict[str, int]:
    """模型 JSON 残缺时，按维度逐个兜底抽取分数。

    实测模型偶尔会漏掉键名，例如输出 `"诚": {"5, "reason": "..."}`（少了
    `"score":`），整份 JSON 直接不合法 → 四个维度全被判为解析失败 →
    之前还会把作品静默记成 0 分。这里按维度单独用正则再捞一次。
    """
    out: Dict[str, int] = {}
    for k in _QUALITY_DIM_KEYS:
        m = (
            re.search(r'"%s"\s*:\s*\{[^{}]{0,20}?"score"\s*:\s*(\d{1,2})' % re.escape(k), raw)
            or re.search(r'"%s"\s*:\s*\{[^0-9]{0,12}?(\d{1,2})' % re.escape(k), raw)
        )
        if m:
            out[k] = max(0, min(10, int(m.group(1))))
    return out


def ai_score_content_quality(caption: str, media_list) -> Dict:
    """调用多模态 AI，围绕「情理色诚」输出 0-40 分。

    返回：{"score": int, "dims": {...}, "reason": str, "error": str|None}
    """
    if not ai_auditor.is_ready():
        return {"score": 0, "dims": {}, "reason": "", "error": "AI 模型未配置"}

    cfg = ai_auditor.load_config()

    # 准备媒体：视频发完整 video_url，图片转 data URL；绝不把 .mp4 视频文件当图片发送
    video_urls, image_urls = _collect_ai_media(media_list)

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
        "4. 每个维度的 reason 要针对该作品的具体文案/画面，说明为什么打这个分，不要空泛。\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"dims":{"情":{"score":<0-10整数>,"reason":"<该维度一句话理由>"},"理":{"score":<0-10整数>,"reason":"<该维度一句话理由>"},"色":{"score":<0-10整数>,"reason":"<该维度一句话理由>"},"诚":{"score":<0-10整数>,"reason":"<该维度一句话理由>"}},"total":<四维之和>,"reason":"<整体一句话理由>"}'
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
    # 归一化：兼容旧格式（数字）与新格式（{score, reason}），统一为 {dim: {score, reason}}
    norm_dims = {}
    for _k, _v in dims.items():
        if isinstance(_v, dict):
            try:
                _score = max(0, min(10, int(_v.get("score") or 0)))
            except (TypeError, ValueError):
                _score = 0
            norm_dims[_k] = {"score": _score, "reason": str(_v.get("reason") or "").strip()}
        else:
            try:
                _score = max(0, min(10, int(_v)))
            except (TypeError, ValueError):
                _score = 0
            norm_dims[_k] = {"score": _score, "reason": ""}

    # 一份都没解析出来：先按维度兜底再捞一次（模型 JSON 偶发残缺），
    # 捞到至少 3 维就采用；否则才算失败。
    # 关键：绝不能返回「score 0 + error None」——那等于把好作品静默记成 0 分，
    # 调用方看不出异常，也就永远不会重算（库里那批 0 分就是这么来的）。
    salvaged = False
    if not norm_dims:
        salv = _salvage_dim_scores(str(raw or ""))
        if len(salv) >= 3:
            norm_dims = {k: {"score": v, "reason": ""} for k, v in salv.items()}
            salvaged = True

    if not norm_dims:
        _raw = str(raw or "")
        return {
            "score": 0, "dims": {}, "reason": "",
            "error": f"AI 返回无法解析（未拿到情理色诚四维分数，原文 {len(_raw)} 字符）：{_raw[-200:]}",
        }

    total = data.get("total")
    if salvaged or total is None:
        total = sum(d["score"] for d in norm_dims.values())
    try:
        total = max(0, min(40, int(total)))
    except (TypeError, ValueError):
        total = 0

    reason = str(data.get("reason") or "").strip()
    if salvaged:
        reason = (reason + "（模型返回的 JSON 有残缺，已按维度兜底抽取）").strip()

    return {
        "score": total,
        "dims": norm_dims,
        "reason": reason,
        "error": None,
    }


def recommend_track(caption: str, media_list, tracks: List[str]) -> Dict:
    """根据内容（文案 + 视频/画面）推荐最匹配的双百战役赛道，供上传人确认。

    返回：{"track": str, "reason": str, "error": str|None}
    track 为空字符串表示未能判定。
    """
    tracks = [t for t in (tracks or []) if t]
    if not tracks:
        return {"track": "", "reason": "", "error": "未配置赛道"}
    if not ai_auditor.is_ready():
        return {"track": "", "reason": "", "error": "AI 模型未配置"}

    cfg = ai_auditor.load_config()
    video_urls, image_urls = _collect_ai_media(media_list)

    track_list = "\n".join(f"- {t}" for t in tracks)
    sys_prompt = (
        "你是短视频营销内容的赛道归类专家。请阅读作品文案与视频/画面，"
        "判断这条内容最契合下面哪一个赛道。\n"
        f"可选赛道（必须从中选且只能选一个，名称必须与列表完全一致，禁止自创）：\n{track_list}\n"
        "判断依据：内容主题、核心卖点、使用场景、目标人群与赛道的匹配度。\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"track":"<从上面列表中原样选择的一个赛道>","reason":"<一句话理由，不超过40字>"}'
    )

    user_parts: list = [
        {"type": "text", "text": f"【作品文案】\n{caption or '（无文案）'}\n\n请判断最匹配的赛道。"}
    ]
    for idx, vu in enumerate(video_urls):
        user_parts.append({"type": "text", "text": f"[完整视频 {idx+1}/{len(video_urls)}] 请完整观看此视频的画面、声音与字幕后判断"})
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
        err = str(e).lower()
        # 视频 URL 被模型侧拒绝（下载失败/格式不支持）时，改用抽帧重试。
        # 之前这里直接返回错误，用户看到的就是"AI 未能完成识别"。
        if video_urls and any(k in err for k in
                              ("video", "download", "invalid", "base64", "empty")):
            print(f"[track] 视频发送失败({str(e)[:70]})，改用抽取关键帧重试", flush=True)
            fallback_parts: list = [{"type": "text", "text": user_parts[0]["text"]}]
            got_frames = False
            for m in (media_list or []):
                if not isinstance(m, dict) or not m.get("is_video"):
                    continue
                frame_src = (ai_auditor.resolve_local_path(m.get("video_local") or m.get("path") or "")
                             or ai_auditor.media_remote_url(m) or "")
                frames = ai_auditor.extract_video_frames(frame_src) if frame_src else []
                if frames:
                    fallback_parts.extend(
                        {"type": "image_url", "image_url": {"url": f}} for f in frames
                    )
                    got_frames = True
            for du in image_urls:
                fallback_parts.append({"type": "image_url", "image_url": {"url": du}})
            if not got_frames:
                return {"track": "", "reason": "", "error": f"AI 调用失败：{e}"}
            try:
                raw = ai_auditor._call_model(cfg, [
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": fallback_parts},
                ])
            except Exception as e2:
                return {"track": "", "reason": "", "error": f"AI 调用失败：{e2}"}
        else:
            return {"track": "", "reason": "", "error": f"AI 调用失败：{e}"}

    data = ai_auditor._extract_json(raw) or {}
    picked = str(data.get("track") or "").strip()
    # 归一化：必须命中候选赛道，否则尝试模糊匹配，仍不匹配则留空由用户自选
    if picked not in tracks:
        exact = next((t for t in tracks if t == picked), "")
        fuzzy = next((t for t in tracks if t and (t in picked or picked in t)), "")
        picked = exact or fuzzy
    return {
        "track": picked,
        "reason": str(data.get("reason") or "").strip(),
        "error": None,
    }


def calc_full_score(metrics: Dict, ai_quality: Dict, cfg: Optional[Dict] = None) -> Dict:
    """组合全部评分，返回完整打分明细。

    metrics 需含：retention_3s / completion_rate / comment_count /
    like_count / share_count / plays
    （share_rate = 转发次数÷播放量，只作为展示信息，不参与打分）
    """
    cfg = cfg or load_score_config()
    retention_total, retention_detail = calc_retention_score(
        metrics.get("retention_3s"), metrics.get("completion_rate"), cfg
    )
    share_rate = calc_share_rate(metrics.get("share_count"), metrics.get("plays"))
    interaction_total, interaction_detail = calc_interaction_score(
        metrics.get("comment_count"), metrics.get("like_count"),
        metrics.get("share_count"), cfg
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
        "share_count": metrics.get("share_count"),
        "share_rate": share_rate,
        "total_score": total,
        "grade": grade,
    }
