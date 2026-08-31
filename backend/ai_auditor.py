"""第三方多模态大模型审核器（OpenAI 兼容接口）。

将「文案 + 已下载的画面图片」连同启用的合规规则一起发送给用户配置的大模型
（如 qwen3-omni-flash 等任何 OpenAI 兼容模型），由模型对照规则判断画面/文案
是否违规，返回结构化命中结果，与本地关键词规则引擎的结果合并。

诚实边界：
  - 当前只发送文案 + 已下载的封面/图文缩略图；不含视频逐帧与口播音频
    （需另行下载视频/音轨，属后续工程）。
  - Qwen-Omni 系列官方要求必须流式调用，本模块统一 stream=True（对普通
    vision 模型同样兼容）。
  - 任何异常都不会中断本地关键词审核：审核失败时返回空命中列表。
"""
import os
import re
import json
import base64
from typing import List, Dict, Any, Optional

import database
import cos_service

_HERE = os.path.dirname(__file__)
MEDIA_DIR = os.path.join(_HERE, "media")
UPLOAD_DIR = os.path.join(_HERE, "uploads")
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
_MAX_VIDEOS = 4   # 视频作品单条最多送审视频数
_MAX_IMAGES = 8   # 图片（图文+视频封面帧）合计送审上限，控制 token 与耗时


def load_config() -> Dict[str, Any]:
    """从 settings 读取大模型配置。"""
    return {
        "enabled": database.get_setting("ai_enabled", "0") == "1",
        "base_url": database.get_setting("ai_base_url", "") or "",
        "api_key": database.get_setting("ai_api_key", "") or "",
        "model": database.get_setting("ai_model", "") or "",
    }


def is_ready() -> bool:
    c = load_config()
    return bool(c["enabled"] and c["base_url"] and c["api_key"] and c["model"])


def _local_image_data_url(thumb: str) -> Optional[str]:
    """把 /media/xxx.jpg 或 uploads/xxx.jpg 形式的本地缩略图读成 data URL（base64）。"""
    if not thumb:
        return None
    # 直接绝对路径检查
    if os.path.isabs(thumb) and os.path.exists(thumb):
        try:
            with open(thumb, "rb") as f:
                raw = f.read()
            return _encode_data_url(raw, thumb)
        except Exception as e:
            print(f"[ai_auditor] 读取图片失败: {thumb}: {e}", flush=True)
            return None
    # 支持三种路径格式: /media/xxx, uploads/xxx, 纯文件名
    for base_dir in (MEDIA_DIR, UPLOAD_DIR):
        candidates = [thumb]
        if "/media/" in thumb:
            candidates.append(thumb.split("/media/")[-1])
        if "uploads/" in thumb:
            candidates.append(thumb.split("uploads/")[-1])
        candidates.append(os.path.basename(thumb))
        for name in candidates:
            path = os.path.join(base_dir, name)
            if os.path.exists(path):
                break
        else:
            continue
        try:
            with open(path, "rb") as f:
                raw = f.read()
            return _encode_data_url(raw, path)
        except Exception as e:
            print(f"[ai_auditor] 读取图片失败: {path}: {e}", flush=True)
            return None
    return None


def _encode_data_url(raw: bytes, path: str) -> str:
    """将二进制数据编码为 data URL。"""
    mime = "image/jpeg"
    low = path.lower()
    if low.endswith(".png"):
        mime = "image/png"
    elif low.endswith(".gif"):
        mime = "image/gif"
    elif low.endswith(".webp"):
        mime = "image/webp"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _download_video_on_demand(url: str, play_len: int, content: dict) -> str:
    """AI 审核时按需下载视频到本地并缓存。返回 /media/xxx.mp4 或空串。

    使用该视频号所属频道的 session cookie 访问 CDN。
    """
    if not url:
        return ""
    try:
        import hashlib
        os.makedirs(MEDIA_DIR, exist_ok=True)
        name = hashlib.md5(url.encode("utf-8")).hexdigest() + ".mp4"
        path = os.path.join(MEDIA_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) >= 1024:
            return "/media/" + name

        # 从 content 的 channel_id 找到该频道的 session cookie
        cookies = None
        channel_id = content.get("_channel_id")
        if channel_id:
            try:
                from collector import channels_collector
                sp = channels_collector.session_path_for(channel_id)
                cookies = channels_collector._load_cookies(sp)
            except Exception:
                pass

        import urllib3
        urllib3.disable_warnings()
        sess = __import__("requests").Session()
        if cookies:
            for ck in cookies:
                sess.cookies.set(
                    ck.get("name", ""), ck.get("value", ""),
                    domain=ck.get("domain"), path=ck.get("path"),
                )
        UA = "Mozilla/5.0"
        resp = sess.get(url, headers={"User-Agent": UA}, timeout=90, stream=True, verify=False)
        if resp.status_code == 200:
            data = resp.content
            if data and (data[:3] == b"\x00\x00\x00" or data[:4] == b"\x1aE\xdf\xa3"
                         or data[:4] == b"ftyp" or data[:4] == b"\x00\x00\x00\x18ftyp"):
                with open(path, "wb") as f:
                    f.write(data)
                return "/media/" + name
        return ""
    except Exception as e:
        print(f"[ai_auditor] 按需下载视频失败: {e}", flush=True)
        return ""


def _build_rules_text(rules: List[Dict[str, Any]]) -> str:
    lines = []
    for r in rules:
        lines.append(
            f"- 规则名:{r.get('name')} | 分类:{r.get('category')} | "
            f"风险等级:{r.get('severity')} | 说明:{r.get('description') or '无'}"
        )
    return "\n".join(lines) if lines else "（暂无规则）"


def _extract_json(text: str) -> Optional[dict]:
    """从模型输出里提取 JSON 对象（容错：截取首个 { 到末个 }）。"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            return None
    return None


# ── P2: 结构化输出正则兜底解析 ──
# 当模型返回非标准 JSON 时，用正则从文本中提取违规规则名和依据
# 借鉴 zhuanjia-agent 的"通过/不通过"解析模式

_VIOLATION_LINE_RE = re.compile(
    r"(?:违规|命中|不符合|违反)\s*[：:]\s*"
    r"【?(?P<rule>.+?)】?"
    r"(?:\s*[。，,;]*\s*"
    r"(?:依据|原因|证据|说明|匹配|内容)\s*[：:]\s*"
    r"(?P<evidence>.+?))?"
    r"(?=\s*(?:违规|命中|不符合|违反|$|AI[-\s]?未标注|错别字|绝对化|虚假))",
    re.DOTALL,
)


def _simple_extract_violations(raw_text: str, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """简化的违规提取：逐行扫描，不依赖复杂正则。

    借鉴 zhuanjia-agent 的"通过/不通过"模式。
    """
    if not raw_text or not raw_text.strip():
        return []

    # 整体合规判断 —— 但如果同时有明确的违规标记，不丢弃
    if re.search(r"(全部|均|所有).{0,15}(合规|通过|未命中|未发现|无违规|不违规)", raw_text):
        if not re.search(r"(违规|命中|不符合|违反)\s*[：:]", raw_text):
            return []

    # 构建规则名 → 规则对象的快速查找
    rule_map = {}
    for r in rules:
        name = (r.get("name") or "").strip()
        if name:
            rule_map[name.lower()] = r

    out = []
    seen_rules = set()
    lines = raw_text.split("\n")

    for line in lines:
        line = line.strip()
        if not line or len(line) < 8:
            continue

        # 方法1：匹配 "违规：XXX" 或 "命中：【XXX】依据：YYY"
        m = re.match(r"(?:违规|命中|不符合|违反)\s*[：:]\s*(.+)$", line)
        if m:
            rest = m.group(1).strip()
            # 尝试拆分为规则名 + 依据
            evidence = ""
            rule_name = rest

            ev_match = re.search(r"(?:依据|原因|证据|说明|匹配|内容)\s*[：:]\s*(.+)$", rest)
            if ev_match:
                rule_name = rest[:ev_match.start()].strip().rstrip("。，,;")
                evidence = ev_match.group(1).strip()

            # 清理规则名（去掉方括号、句号等）
            rule_name = re.sub(r"^【|】$|^\[|\]$", "", rule_name).strip()
            if not rule_name or len(rule_name) < 2:
                continue

            # 过滤"不违规"的描述
            if _is_noviolation_evidence(evidence or rule_name):
                continue

            # 尝试匹配已知规则
            matched = None
            for rname, robj in rule_map.items():
                if rname in rule_name or rule_name in rname:
                    matched = robj
                    break

            dedup_key = (matched["name"] if matched else rule_name)[:30]
            if dedup_key in seen_rules:
                continue
            seen_rules.add(dedup_key)

            out.append({
                "rule_id": matched["id"] if matched else None,
                "rule_name": (matched["name"] if matched else rule_name)[:40],
                "category": matched.get("category", "AI审核（兜底）") if matched else "AI审核（兜底）",
                "severity": matched.get("severity", "medium") if matched else "medium",
                "matched_text": (evidence or rule_name)[:200],
                "field": "内容（AI）",
                "source": "AI模型（兜底解析）",
            })

    return out


_SEVERITY_MAP = {
    "forbidden": "forbidden", "严重": "forbidden", "红线": "forbidden",
    "high": "high", "高": "high", "风险": "high",
    "medium": "medium", "中": "medium", "一般": "medium",
    "low": "low", "低": "low", "建议": "low",
}


def _regex_parse_violations(raw_text: str, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """当 JSON 解析失败时，用简化的逐行扫描从模型原文中提取违规条目。

    借鉴 zhuanjia-agent: 逐行匹配 → 结构化 → 兜底空列表。
    """
    if not raw_text or not raw_text.strip():
        return []

    # 优先使用新的简化抽取
    result = _simple_extract_violations(raw_text, rules)
    if result:
        return result

    # 兜底：使用旧正则（用于非逐行格式的文本）
    out = []
    for m in _VIOLATION_LINE_RE.finditer(raw_text):
        rule_name = (m.group("rule") or "").strip()
        evidence = (m.group("evidence") or m.group("rule") or "").strip()
        rule_name = re.sub(r"^【|】$|^\[|\]$", "", rule_name).strip()
        if not rule_name or len(rule_name) < 2:
            continue
        if _is_noviolation_evidence(evidence or rule_name):
            continue
        out.append({
            "rule_id": None, "rule_name": rule_name[:40],
            "category": "AI审核（正则兜底）", "severity": "medium",
            "matched_text": evidence[:200], "field": "内容（AI）",
            "source": "AI模型（兜底解析）",
        })

    return out


# ── P1: 审核策略分流 ──
# 借鉴 zhuanjia-agent 的多模型分流：不同内容类型用不同策略
# - 纯文本：只发文本，速度快、成本低
# - 含图片：发送文本+图片
# - 含视频：发送全部（多模态）
# 这样文本-only 内容（无图的图文帖）不会浪费图片 token


def _classify_content(media: list, caption: str, transcript: str, ocr: str) -> str:
    """分类内容类型，返回策略标识：
    - 'text':     纯文本（无媒体）
    - 'image':    含图片（有缩略图/图文）
    - 'video':    含视频
    """
    if not media:
        return "text"
    for m in media:
        if isinstance(m, dict) and m.get("is_video"):
            return "video"
    # 有 media 但没有视频 → 可能是图片
    for m in media:
        if isinstance(m, dict) and (m.get("thumb") or m.get("thumb_remote")):
            return "image"
    has_text = bool((caption or "").strip() or (transcript or "").strip() or (ocr or "").strip())
    return "text" if has_text else "image"  # 兜底


def _call_model(cfg: Dict[str, Any], messages: list) -> str:
    """调用 OpenAI 兼容模型，流式拼接文本输出。"""
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    # Qwen-Omni 强制流式；普通模型流式同样兼容。
    stream = client.chat.completions.create(
        model=cfg["model"],
        messages=messages,
        stream=True,
        temperature=0,
    )
    buf = []
    for chunk in stream:
        try:
            delta = chunk.choices[0].delta
            if getattr(delta, "content", None):
                buf.append(delta.content)
        except Exception:
            continue
    return "".join(buf)


def _empty_review(model: str = "", error: Optional[str] = None) -> Dict[str, Any]:
    return {"model": model, "interpretation": "", "conclusion": "",
            "violations": [], "image_count": 0, "video_count": 0, "text_length": 0,
            "has_caption": False, "has_transcript": False, "has_ocr": False,
            "is_video": False, "error": error}


# 模型有时会把"判定为合规/未命中"的规则也写进 violations，依据是"未涉及/无XX"这类
# 否定说明（即在解释"为什么不违规"）。这类条目必须剔除，否则合规内容会被误判为违规。
_NOVIOLATION_RE = re.compile(
    r"(未(涉及|提及|提到|使用|出现|包含|宣称|承诺|引导|构成|存在|发现|采用|呈现|显示|展示|见到|看到|关联|暗示|违反|命中|触发|夸大|描述为|说明|标明|标注))"
    r"|(无[^，。；,；]{0,30}(描述|表述|内容|词汇|话术|数据|行为|用语|链接|承诺|提及|涉及|信息|画面|元素|风险|违规|危险|场景|错误|问题|误用|夸大))"
    r"|(没有[^，。；,；]{0,12}(涉及|提及|使用|出现|包含|违规|风险|描述|表述|内容|承诺|引导|夸大|贬低|暗示|违反|问题))"
    r"|(不(涉及|包含|构成|属于|存在|违规|违反|需要|适用)[^，。；,；]{0,6}(违规|风险|此类|该类)?)"
    r"|(符合[^，。；,；]{0,30}(规范|要求|规定|标准|限制|规则|法律|法规|定位))"
    r"|未命中|未见|不存在该|无此类|不构成违规|属合规|不违规|未违规|未违反|不涉及违规|不违反|可合规"
)
# "应标注而未标注"等"应做未做"本身就是违规，不能被上面的否定规则误删。
# 但注意：如果 evidence 是"本视频已标注"、"已标注"说明内容实际合规，不应保留。
_SHOULD_BUT_NOT_RE = re.compile(r"未(标注|标明|标识|披露|注明|告知|公示|提示风险)")
_SHOULD_BUT_SAFE = re.compile(r"(已(标注|标明|标识|披露|注明|告知|公示))|(不构成违规|不违规|属合规|合规$)")

# 推测性/联想性判定：模型自己都不确定，不应算违规
_SPECULATIVE_EVIDENCE_RE = re.compile(
    r"(可能引发|可能被解读|可能被误认为|可能存在|可能涉及|可能构成|可能隐含|可能暗示|"
    r"可能被(误解|误判|混淆|理解|解读|误认为|联想)|"
    r"容易与|容易被|易引发|易与|易被|或被解读|被解读为|被误认为|或用词泛化|或可被|不排除|有.*风险|有.*嫌疑|存在.*可能性|"
    r"隐含替代|场景重叠|消费.{0,5}联想|"
    r"替换.{0,10}(风险|隐患)|联想.{0,5}(风险|隐患))"
)
# 跨品牌联想（画面中是A品牌，却判B品牌违规）— 这种推测不应成立
_CROSS_BRAND_GUESS = re.compile(
    r"(虽未(直接)?(提及|出现|显示|涉及|标注|说明|明确|明示|写明).*但|可能引发.*联想|市场.*替代关系|"
    r"消费者.*安全.{0,5}联想|无关联|易引发.{0,8}负面|"
    r".{0,5}(或可|可能会|可能).*(被解读|被误认为|被误解|隐含))"
)


def _is_noviolation_evidence(evidence: str) -> bool:
    """判断某条 violation 的依据其实是在"说明不违规"（应剔除）。"""
    e = (evidence or "").strip()
    if not e:
        return False
    # 如果 evidence 明确表明内容已是合规状态（已标注、不构成违规等），
    # 且包含"未标注"等词只是在描述规则条件而非内容缺失，则跳过 SHOULD_BUT 检查
    if _SHOULD_BUT_NOT_RE.search(e) and not _SHOULD_BUT_SAFE.search(e):
        return False  # 真实违规：应做而未做
    # 结尾明确否认违规：不违规 / 合规 / 属合规 / 未违规 / 不涉及违规
    if re.search(r"(不违规|未违规|属合规|不构成违规|不涉及违规|合规\s*$)", e):
        return True
    # 推测性/联想性判定 → 模型自己都不确定，丢弃
    # 但如果证据中明确写"违反/命中/触发"了某规则，说明是明确判定而非推测
    if _SPECULATIVE_EVIDENCE_RE.search(e):
        if not re.search(r"(违反|命中|触犯|触发)\s*.{0,30}(规则|红线|规定|要求|标准|法律|法规)", e):
            return True
    # 跨品牌联想（虽未提及A...但可能...） → 丢弃
    if _CROSS_BRAND_GUESS.search(e):
        return True
    return bool(_NOVIOLATION_RE.search(e))


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "是", "违规", "命中")
    return True  # 未提供则默认按真违规处理，再由依据否定检测兜底


def audit(content: Dict[str, Any], rules: List[Dict[str, Any]],
          cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """用大模型解读并审核单条内容。

    返回 dict：
      - interpretation: 模型对画面+文案的客观解读（证明模型确实"看了"内容）
      - conclusion:     模型给出的合规审核结论
      - violations:     与规则引擎兼容的命中列表（带 source='AI模型'）
      - image_count:    实际送审的画面图片数
      - error:          失败原因（成功为 None）
    """
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("base_url") and cfg.get("api_key") and cfg.get("model")):
        return _empty_review(cfg.get("model", ""), error="未启用或配置不完整")

    # 组装图片（本地缩略图优先转 base64）。
    # media 可能是 list（采集路径）或 DB 取出的 JSON 字符串（重分析路径），做类型容错。
    media = content.get("media") or []
    if isinstance(media, str):
        try:
            media = json.loads(media)
        except Exception:
            media = []

    caption = content.get("caption") or ""
    transcript = content.get("transcript") or ""
    ocr_text = content.get("ocr_text") or ""

    # ── P1: 策略分流 ──
    content_type = _classify_content(media, caption, transcript, ocr_text)
    # 纯文本内容可以用更精简的 prompt（省 token）
    if content_type == "text":
        _MAX_IMAGES_STRATEGY = 0  # 文本-only 不发图片
    else:
        _MAX_IMAGES_STRATEGY = _MAX_IMAGES

    image_urls = []
    video_data_urls = []
    is_video_content = False
    for m in (media or []):
        if not isinstance(m, dict):
            continue
        if m.get("is_video"):
            is_video_content = True

            # ── 优先使用 COS（实时生成预签名 URL，qwen3-omni-flash 原生支持 HTTP video_url）──
            cos_key = m.get("cos_key", "")
            if cos_key and cos_service.is_enabled():
                cos_url = cos_service.presigned_url(cos_key, expires=7200)
                if cos_url:
                    video_data_urls.append({
                        "url": cos_url,
                        "size_mb": 0,
                    })
                    continue

            # ── 兼容旧数据：如果有 cos_url 直接用 ──
            cos_url = m.get("cos_url", "")
            if cos_url and cos_url.startswith("http"):
                video_data_urls.append({
                    "url": cos_url,
                    "size_mb": 0,
                })
                continue

            # ── 本地文件：生成 HTTP URL 让模型直接读取完整视频 ──
            vlocal = m.get("video_local", "")
            vpath = ""
            for _base in (UPLOAD_DIR, MEDIA_DIR):
                _name = vlocal.split("/media/")[-1] if "/media/" in vlocal else (vlocal.split("uploads/")[-1] if "uploads/" in vlocal else os.path.basename(vlocal))
                _candidate = os.path.join(_base, _name) if _name else ""
                if _candidate and os.path.exists(_candidate):
                    vpath = _candidate
                    break
            if not vpath and vlocal:
                vpath = vlocal if os.path.exists(vlocal) else ""

            if vpath and os.path.exists(vpath):
                file_size_mb = os.path.getsize(vpath) / (1024 * 1024)
                # 生成可通过服务器访问的 HTTP URL
                # 优先使用环境变量 SERVER_BASE_URL，否则回退 127.0.0.1
                vname = os.path.basename(vpath)
                base_url = os.environ.get("SERVER_BASE_URL", "http://127.0.0.1:8000")
                if UPLOAD_DIR in vpath:
                    http_url = f"{base_url}/uploads/{vname}"
                else:
                    http_url = f"{base_url}/media/{vname}"

                # qwen3-omni-flash 支持 video_url（HTTP），限制 100MB 以内
                if file_size_mb <= 100:
                    video_data_urls.append({
                        "url": http_url,
                        "size_mb": file_size_mb,
                    })
                    continue
                else:
                    # 超过 100MB 的视频，用 ffmpeg 抽取多帧关键画面
                    print(f"[ai_auditor] 视频 {file_size_mb:.0f}MB 超限，抽取关键帧代替", flush=True)

                # 抽取缩略图/关键帧作为兜底
                du = _local_image_data_url(m.get("thumb") or "")
                if not du:
                    # ffmpeg 抽取多帧（开头、中间、结尾）
                    for ss in ["1", "00:00:05", "00:00:10"]:
                        thumb_path = vpath + f".thumb_{ss}.jpg"
                        if not os.path.exists(thumb_path):
                            try:
                                import subprocess as _sp
                                _sp.run(
                                    ["ffmpeg", "-y", "-i", vpath, "-vframes", "1", "-ss", ss,
                                     "-q:v", "5", thumb_path],
                                    capture_output=True, timeout=15,
                                )
                            except Exception:
                                pass
                        frame_du = _local_image_data_url(thumb_path)
                        if frame_du:
                            image_urls.append(frame_du)
                if du:
                    image_urls.append(du)
            if not image_urls:
                du = _local_image_data_url(m.get("thumb") or "")
                if not du and m.get("thumb_remote"):
                    du = m["thumb_remote"]
                if du:
                    image_urls.append(du)
        else:
            # ── 图片路径：优先 COS（实时生成预签名 URL）──
            cos_key = m.get("cos_key", "")
            if cos_key and cos_service.is_enabled():
                cos_url = cos_service.presigned_url(cos_key, expires=7200)
                if cos_url:
                    image_urls.append(cos_url)
            elif m.get("cos_url", "").startswith("http"):
                image_urls.append(m["cos_url"])
            else:
                du = _local_image_data_url(m.get("thumb") or m.get("path") or "")
                if not du and m.get("thumb_remote"):
                    du = m["thumb_remote"]
                if du:
                    image_urls.append(du)
    image_urls = image_urls[:_MAX_IMAGES_STRATEGY]
    video_data_urls = video_data_urls[:_MAX_VIDEOS]

    # 组装完整文案：正文 + 语音转写 + 画面话题文字
    text_parts = []
    if caption:
        text_parts.append(f"【文案/正文】\n{caption}")
    if transcript:
        text_parts.append(f"【语音转写/口播内容】\n{transcript}")
    if ocr_text:
        text_parts.append(f"【话题标签/字幕关键词】\n{ocr_text}")
    full_text = "\n\n".join(text_parts) if text_parts else "（无文案）"

    if is_video_content and image_urls and any(m.get("_frame_extracted") for m in (media or []) if isinstance(m, dict)):
        media_desc = "一条视频的关键帧截图（从视频中抽取的代表性画面）"
        interp_req = ("对视频内容的客观解读，100-300字。"
                      "需根据截图帧+文案综合推断：画面的视觉风格、人物/产品/场景/品牌元素是什么，"
                      "文案在表达什么卖点或故事，整体传达了什么主题，"
                      "以证明你确实通读了文案并仔细观看了截图帧")
    elif video_data_urls:
        media_desc = "一条完整视频（你可以逐帧/逐秒分析视频画面+声音+字幕）"
        interp_req = ("对视频完整内容的客观解读，100-300字。"
                      "需完整描述：视频的开头、中间、结尾分别发生了什么，"
                      "人物/产品/品牌Logo/场景的变化与演进，"
                      "口播说了什么、字幕写了什么、背景音乐风格，"
                      "整体传达了什么主题或故事。以证明你确实从头到尾看了完整视频")
    elif is_video_content and image_urls:
        # 从视频中取了帧/screenshot 或封面图
        media_desc = "一条视频的预览画面（关键帧/封面截图）"
        interp_req = ("对视频内容的客观解读，100-250字。"
                      "需结合预览画面与文案综合描述：画面的主题/风格/品牌元素是什么，"
                      "文案在表达什么卖点或故事，以证明你确实通读了文案并观看了预览画面")
    elif is_video_content and not image_urls:
        media_desc = "一条视频（本文案为视频的完整标题与描述）"
        interp_req = ("对视频内容的客观解读，60-180字。"
                      "需根据文案推断视频的主题、场景和大致内容，"
                      "以证明你确实通读了全部文案")
    elif image_urls:
        media_desc = "若干画面图片"
        interp_req = ("对图文完整内容的客观解读，60-150字。"
                      "需结合画面与文案综合描述：画面里有什么、文案在表达什么，"
                      "以证明你确实通读了全部文案并分析了画面")
    else:
        media_desc = "纯文本内容（无画面图片可看）"
        interp_req = ("对文本内容的客观解读，60-150字。"
                      "需分析文案的整体主题、关键表述、语气风格，"
                      "以证明你确实通读了全部文本内容")

    sys_prompt = (
        f"你是严谨的短视频内容合规审核员。下面给你一条视频号内容（完整文案 + {media_desc}）"
        "和一份【合规规则清单】。请你：\n"
        "1) 先客观解读这条内容——通读所有文案（正文+口播+字幕话题）、结合画面，整体在表达什么；\n"
        "2) 再对照规则逐条判断是否违规。\n"
        "\n"
        "【审核核心原则 —— 必须区分「品牌创意内容」与「违规营销广告」】\n"
        "品牌常会制作动画/短剧/故事/IP系列（如武侠剧情、二次元角色、品牌吉祥物动画等）"
        "来传播品牌文化，这类内容属于品牌创意表达，不属于违规营销。审核时务必区分：\n"
        "- 创意/IP内容中的品牌元素（Logo/名称/徽章等）是正常品牌露出，不能判为「Logo变形」「品牌名错误」「Logo幻觉」「仿官方」等\n"
        "- 创意剧情中的冲突（打假/反派等）是剧情需要，不能判为「品牌负面」「品质下降暗示」「安全负面联想」等\n"
        "- 创意内容中的视觉特效（闪电/光影等）是画面风格，不能判为「危险场景」「电池爆炸联想」等\n"
        "- 创意内容不是产品广告：画面中的品牌元素不代表产品功能宣称，「聚能环」徽章不等于夸大产品\n"
        "- 品牌IP动画中角色与品牌同名是刻意设定（如「南孚宗少主雷雨砚」），不能判为「品牌名错误」或「混淆定位」\n"
        "- 同一条内容「联合推广」南孚集团旗下多个品牌（如南孚+益圆、或四大品牌全家福）是正常的集团多品牌营销，"
        "不能判为「品牌间-内部对比」「品牌间-混淆定位」或「竞品」违规；只要没有互相比较高低优劣、没有用南孚泛指所有产品即可\n"
"- 唯一例外：如果创意内容是AI生成的且确实未标注「AI生成」，「AI-未标注标识」规则可以命中（这是格式要求，不否定创意本身）\n"
"- 【AI标注判断】画面中任何位置出现的「AI生成」标识（左上角、右下角、画面中央、视频开头或结尾）都算「已标注」，不判定违规。\n"
"- 只有画面中完全找不到「AI生成」文字标识时才可命中「AI-未标注标识」。\n"
"\n"
"【品牌名识别 —— 严格禁止幻觉】\n"
"判断「品牌名称错误」时，你必须100%确认画面或文案中出现的文字确实错误。如果你不确定、看不清、文字模糊，就需要跳过。\n"
"禁止凭猜测或脑补去判「品牌名错误」——你没有看清就不要说。电池外壳上的Logo通常是品牌正品标签，不要仅凭OCR就断言其文字错误。\n"
"如果你在画面里【确实清晰地看到】品牌名有错字（如'南福'写成'南孚'之外的字形），才可命中。\n"
"重点是品牌名常用同音错字：南孚→南福/南符/南扶、益圆→益园/益元/一元、以及画面字幕中任何将品牌名写错的情况都必须检查。\n"
"\n"
"【字幕与文案错别字检查 —— 这是高频违规类型，必须逐字扫描】\n"
"错别字检测是审核的核心任务之一，不能遗漏！请按以下方法仔细检查：\n"
"1. OCR文字（视频画面上的硬字幕）：逐行逐字读，确认每个字是否正确。写错的品牌名（南孚→南福/南符）、"
"在再混淆（在来→再来）、既即混淆（既使→即使）、的得地误用都要标出来。\n"
"2. 文案正文（caption/标题）：逐句检查，特别注意同音错字。\n"
"3. 常见电池行业错字重点扫：漏液≠漏夜、聚能环≠聚能杯/聚能坏、电池≠电弛/电迟。\n"
"4. 每发现一个错别字，就作为一条独立的违规放进 violations，写上规则名「错别字-语义检查」或匹配的错别字规则，"
"evidence 里写出具体位置和错误→正确的对应（如：画面上方字幕第3秒「在来一次」应为「再来一次」）。\n"
"5. 如果一条内容同时有品牌违规和错别字，两者都要放入 violations，不能因为品牌违规漏掉错别字。\n"
"\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"interpretation":"' + interp_req + '",'
        '"violations":[{"rule_name":"","category":"","severity":"forbidden|high|medium|low",'
        '"field":"画面|文案|口播|字幕","evidence":"该内容【确实违反】此规则的具体依据(哪句文案/哪段口播/画面里看到什么)"}],'
        '"conclusion":"一句话合规结论：合规，或 命中X条规则及原因"}\n'
        "【极重要】violations 数组里【只能放你判定为确实违规的规则】。\n"
"判定为合规、未命中的规则【绝对不要】放进 violations；\n"
"【绝对不要】输出诸如「未涉及…」「无…描述」「未使用…」「未提及…」这类用于说明"
"「为什么不违规」的条目——它们不是违规，写进去就是错误。\n"
"【绝对不要】做'推测性/联想性'违规判定。如果evidence包含「可能引发」「可能被解读」「可能被误认为」「易被误认为」「容易与」「或用词泛化可能」\n"
"这类词，说明你本身也不确定是否真的违规——这种条目不准放进violations，直接丢掉。\n"
"只有画面中【确实出现】或文案中【明确写出】的违规事实才能命中；仅靠'可能/或许/隐含'的联想不能判违规。\n"
"- 视频中出现某品牌电池漏液，只能判该品牌，不能因画面中有'电池+漏液'就跨品牌联想到南孚（除非画面/口播/字幕中明确出现了南孚品牌元素）\n"
"- 某品牌电池出现在家用遥控器中，只能按实际出现的场景判断，不能因遥控器'市场中可能与南孚存在替代关系'就判「替代南孚」——市场关系不是画面里出现的\n"
"若全部合规，violations 必须是空数组 []，并在 conclusion 说明为何合规。\n"
"severity 必须取自对应规则的风险等级。"
    )
    media_label = ("视频" if video_data_urls
                   else ("视频（封面）" if is_video_content
                         else ("图文" if image_urls else "文本")))

    if video_data_urls:
        total_mb = sum(v.get("size_mb", 0) for v in video_data_urls)
        vid_note = f"（完整视频文件，{total_mb:.0f}MB，请从头到尾逐段分析视频内容）"
        img_note = ""
    elif is_video_content and image_urls:
        vid_note = "（视频关键帧截图，请仔细观察画面中的品牌元素、文字、场景、人物等）"
        img_note = ""
    elif image_urls:
        vid_note = ""
        img_note = f"（共 {len(image_urls)} 张图，请逐一分析每张图，不要只看第一张）" if len(image_urls) > 1 else ""
    else:
        vid_note = ""
        img_note = ""

    user_text = (
        f"【合规规则清单】\n{_build_rules_text(rules)}\n\n"
        f"=== 以下为内容的完整信息，请通读全部文案后结合画面综合分析 ===\n\n"
        f"{full_text}\n\n"
        f"【{media_label}】{vid_note if video_data_urls else img_note} 见下方媒体文件。"
        + (" 请将每张图的内容都纳入解读。" if (len(image_urls) > 1 and not video_data_urls) else "")
    )
    user_parts: list = [
        {"type": "text", "text": user_text}
    ]
    # 视频：直接发送完整视频文件
    for idx, v in enumerate(video_data_urls):
        user_parts.append({"type": "text",
                           "text": f"[完整视频 {idx+1}/{len(video_data_urls)}] 请从头到尾分析此视频的所有画面、声音和字幕"})
        user_parts.append({"type": "video_url", "video_url": {"url": v["url"]}})
    # 图片：编号标注
    for idx, du in enumerate(image_urls):
        label = f"（第{idx+1}张图）" if len(image_urls) > 1 else ""
        user_parts.append({"type": "text", "text": f"[{media_label} 图{idx+1}/{len(image_urls)}]{label}"})
        user_parts.append({"type": "image_url", "image_url": {"url": du}})

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user_parts},
    ]

    try:
        raw = _call_model(cfg, messages)
    except Exception as e:
        err_msg = str(e)
        # 如果视频 base64 发送失败，自动退化为只发封面图重试
        if video_data_urls and ("base64" in err_msg.lower() or "empty" in err_msg.lower()
                                 or "video" in err_msg.lower() or "invalid" in err_msg.lower()):
            print(f"[ai_auditor] 视频发送失败({err_msg[:80]})，退化为封面图重试", flush=True)
            # 重建消息：去掉视频，只用封面图
            fallback_parts = [{"type": "text", "text": user_text}]
            for m in (media or []):
                if not isinstance(m, dict):
                    continue
                if m.get("is_video"):
                    du = _local_image_data_url(m.get("thumb") or "")
                    if not du and m.get("thumb_remote"):
                        du = m["thumb_remote"]
                    if du:
                        fallback_parts.append({"type": "image_url", "image_url": {"url": du}})
                elif m.get("thumb") or m.get("thumb_remote"):
                    du = _local_image_data_url(m.get("thumb") or "")
                    if not du and m.get("thumb_remote"):
                        du = m["thumb_remote"]
                    if du:
                        fallback_parts.append({"type": "image_url", "image_url": {"url": du}})
            fallback_messages = [
                {"role": "system", "content": sys_prompt.replace("一条完整视频（你可以逐帧/逐秒分析视频画面+声音+字幕）", "视频封面图")},
                {"role": "user", "content": fallback_parts},
            ]
            try:
                raw = _call_model(cfg, fallback_messages)
            except Exception as e2:
                return _empty_review(cfg["model"], error=f"模型调用失败（封面图重试也失败）：{e2}")
        else:
            return _empty_review(cfg["model"], error=f"模型调用失败：{e}")

    data = _extract_json(raw)
    if not data:
        # ── P2: JSON 失败时用正则兜底解析 ──
        violations = _regex_parse_violations(raw, [r for r in rules if r.get("enabled", 1)])
        rv = _empty_review(cfg["model"])
        rv["interpretation"] = (raw or "").strip()[:500]
        rv["conclusion"] = "AI 返回非 JSON 格式，已通过正则兜底解析出 {} 条疑似违规".format(len(violations)) if violations else "AI 返回非 JSON 格式，正则兜底未检出违规"
        rv["violations"] = violations
        rv["image_count"] = len(image_urls)
        rv["video_count"] = len(video_data_urls)
        rv["text_length"] = len(full_text)
        rv["has_caption"] = bool(caption.strip())
        rv["has_transcript"] = bool(transcript.strip())
        rv["has_ocr"] = bool(ocr_text.strip())
        rv["is_video"] = is_video_content
        rv["error"] = "模型返回非标准JSON（已用正则兜底解析）"
        rv["strategy"] = content_type
        return rv

    out = []
    valid_sev = {"forbidden", "high", "medium", "low"}
    for v in (data.get("violations") or []):
        if not isinstance(v, dict):
            continue
        # 第1道：模型若显式标注了 hit/violated/是否违规 为假，跳过
        if "hit" in v and not _truthy(v.get("hit")):
            continue
        if "violated" in v and not _truthy(v.get("violated")):
            continue
        evidence = str(v.get("evidence") or "").strip()
        # 第2道：依据本身明确说"不违规"（不涉及/无XX/结尾不违规等否定句），剔除
        if _is_noviolation_evidence(evidence):
            continue
        # 第3道：如果 evidence 明确包含"不违规"三个字（无论前后），跳过
        if "不违规" in evidence:
            continue
        if "合规" in evidence and ("未" in evidence or "无" in evidence or "没有" in evidence):
            continue
        sev = str(v.get("severity") or "medium").lower()
        if sev not in valid_sev:
            sev = "medium"
        out.append({
            "rule_id": None,
            "rule_name": str(v.get("rule_name") or "AI判定"),
            "category": str(v.get("category") or "AI审核"),
            "severity": sev,
            "matched_text": evidence or "（模型未提供依据）",
            "field": f"{v.get('field') or '画面'}（AI）",
            "source": "AI模型",
        })

    return {
        "model": cfg["model"],
        "interpretation": str(data.get("interpretation") or "").strip(),
        "conclusion": str(data.get("conclusion") or "").strip(),
        "violations": out,
        "image_count": len(image_urls),
        "video_count": len(video_data_urls),
        "text_length": len(full_text),
        "has_caption": bool(caption.strip()),
        "has_transcript": bool(transcript.strip()),
        "has_ocr": bool(ocr_text.strip()),
        "is_video": is_video_content,
        "strategy": content_type,
        "error": None,
    }


def test_connection(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """测试模型连通性：发一个最简请求，成功返回模型回的内容。"""
    if not (cfg.get("base_url") and cfg.get("api_key") and cfg.get("model")):
        return {"ok": False, "message": "请先填写 Base URL、API Key 与模型名称。"}
    try:
        reply = _call_model(cfg, [
            {"role": "user", "content": "请只回复两个字：正常"}
        ])
        return {"ok": True, "message": f"连接成功，模型回复：{reply.strip()[:60] or '(空)'}"}
    except Exception as e:
        return {"ok": False, "message": f"连接失败：{e}"}
