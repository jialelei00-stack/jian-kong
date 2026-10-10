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
import hashlib
import tempfile
import threading
from datetime import datetime
from typing import List, Dict, Any, Optional

import database
import cos_service

_HERE = os.path.dirname(__file__)
MEDIA_DIR = os.path.join(_HERE, "media")
UPLOAD_DIR = os.path.join(_HERE, "uploads")
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
# 媒体对外的可访问基地址：大模型（外部服务）要能直接拉取视频/图片，
# 本机回环地址（127.0.0.1）外部模型访问不到，必须用公网地址。
# 可用环境变量 SERVER_BASE_URL 覆盖。
DEFAULT_SERVER_BASE_URL = os.environ.get("SERVER_BASE_URL", "https://zhanyi.nanfu.com")
_MAX_VIDEOS = 4   # 视频作品单条最多送审视频数
_MAX_IMAGES = 8   # 图片（图文+视频封面帧）合计送审上限，控制 token 与耗时
_EVIDENCE_MAX = 3  # 违规证据截图：每条违规一张，单条内容最多截 3 张

# 全进程模型并发闸门：审核队列与请求线程（识别赛道 / 建档打分 / 连通性测试）共用，
# 避免多路含完整视频的多模态请求同时打到上游触发限流。
# 上游对并发突发敏感（会返回 Request rate increased too quickly），故统一在这里收口。
MAX_CONCURRENT_AI = int(os.environ.get("MAX_CONCURRENT_AI", "2"))
_AI_GATE = threading.Semaphore(MAX_CONCURRENT_AI)


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


def resolve_local_path(name: str) -> str:
    """把 /media/xxx、uploads/xxx 或纯文件名解析成磁盘上的真实路径；找不到返回 ""。

    统一兼容三种存储形态（绝对路径 / 含目录前缀的相对路径 / 纯文件名），
    供图片读取、视频体积判断、抽帧等场景共用。
    """
    if not name:
        return ""
    if os.path.isabs(name) and os.path.exists(name):
        return name
    candidates = [name]
    if "/media/" in name:
        candidates.append(name.split("/media/")[-1])
    if "uploads/" in name:
        candidates.append(name.split("uploads/")[-1])
    base = os.path.basename(name)
    if base:
        candidates.append(base)
    for base_dir in (MEDIA_DIR, UPLOAD_DIR):
        for cand in candidates:
            path = os.path.join(base_dir, cand)
            if os.path.exists(path):
                return path
    return ""


def media_remote_url(m: Dict[str, Any]) -> Optional[str]:
    """media 项的远端可读 URL：COS 实时预签名 → 已存的 cos_url。

    必须看 cos_ready：上传接口是后台线程异步传 COS 的，上传后立刻触发的
    审核/赛道识别如果直接拿预签名 URL，会指向一个**还没上传完的对象**，
    模型侧下载必然失败（400 Failed to download multimodal content），
    只能退化成封面图。此时应回退本地 HTTP 地址（本地副本还在）。
    老数据没有 cos_ready 字段，按已就绪处理。
    """
    if not isinstance(m, dict):
        return None
    cos_key = m.get("cos_key") or ""
    if cos_key and cos_service.is_enabled() and m.get("cos_ready", True):
        try:
            u = cos_service.presigned_url(cos_key, expires=7200)
            if u:
                return u
        except Exception:
            pass
    cu = m.get("cos_url") or ""
    if cu.startswith("http"):
        return cu
    return None


def media_public_url(m: Dict[str, Any]) -> Optional[str]:
    """media 项 → 大模型可直接读取的 URL，只返回 COS 预签名地址。

    不再回退本机 /uploads HTTP 地址：该域名走 CDN，CDN 配了防盗链，
    服务端（模型侧）拉取不带 Referer 会被 403，模型必然回
    「400 Failed to download multimodal content」。拿不到 COS 地址时
    宁可不给，让上层退化为本地抽关键帧，也不要白跑一次注定失败的请求。
    """
    return media_remote_url(m)


def _local_image_data_url(thumb: str) -> Optional[str]:
    """把 /media/xxx.jpg 或 uploads/xxx.jpg 形式的本地缩略图读成 data URL（base64）。"""
    path = resolve_local_path(thumb)
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        return _encode_data_url(raw, path)
    except Exception as e:
        print(f"[ai_auditor] 读取图片失败: {path}: {e}", flush=True)
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


# 模型对 video_url 的体积上限：超了会直接返回 400
# （Failed to download multimodal content / Invalid video file），
# 此时只能改用抽帧，否则整段视频内容都看不到。
VIDEO_URL_MAX_MB = 100


def video_exceeds_limit(m: Dict[str, Any]) -> bool:
    """该视频是否超出模型可直接读取的 video_url 体积上限。

    优先用上传时记录的 size（不依赖本地副本是否还在），
    老数据没有 size 时退回本地文件的实际大小。
    """
    if not isinstance(m, dict):
        return False
    try:
        size_mb = (m.get("size") or 0) / (1024 * 1024)
    except Exception:
        size_mb = 0
    if size_mb:
        return size_mb > VIDEO_URL_MAX_MB
    p = resolve_local_path(m.get("video_local") or m.get("path") or "")
    if not p:
        return False
    try:
        return os.path.getsize(p) / (1024 * 1024) > VIDEO_URL_MAX_MB
    except OSError:
        return False


def _download_video_to_temp(url: str, timeout: int = 600) -> str:
    """把远端视频整段下载到临时文件，失败返回 ""。

    逐个时间点对远程 URL 做 input seeking 会反复建连+拉取分片
    （实测 116MB 要 74 秒），先整段下载再本地抽帧快得多。
    """
    import requests
    import urllib3
    urllib3.disable_warnings()
    try:
        fd, path = tempfile.mkstemp(suffix=".mp4", prefix="nf_vid_", dir=_TMP_ROOT)
        os.close(fd)
    except OSError:
        return ""
    try:
        with requests.get(url, stream=True, timeout=timeout, verify=False) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 << 20):
                    if chunk:
                        f.write(chunk)
        if os.path.getsize(path) < 1024:
            raise IOError("下载内容过小")
        return path
    except Exception as e:
        print(f"[ai_auditor] 下载视频失败: {e}", flush=True)
        try:
            os.remove(path)
        except OSError:
            pass
        return ""


# 临时目录：优先用磁盘上的 /var/tmp。很多机器的 /tmp 是 tmpfs（内存盘），
# 抽帧要先落地整段视频（上百 MB），放 /tmp 会直接把内存吃掉。
_TMP_ROOT = "/var/tmp" if os.path.isdir("/var/tmp") else tempfile.gettempdir()


def _prune_frame_cache(max_age_days: int = 7) -> None:
    """清理过期的抽帧缓存目录，避免临时目录随视频量无限增长。"""
    import shutil
    import time as _t
    try:
        now = _t.time()
        for name in os.listdir(_TMP_ROOT):
            if not name.startswith("nf_frames_"):
                continue
            p = os.path.join(_TMP_ROOT, name)
            try:
                if now - os.path.getmtime(p) > max_age_days * 86400:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass
    except OSError:
        pass


# 抽帧的并发控制：
#  · 同一视频（按源地址）只允许一个线程在抽，其余线程等它完成后直接读缓存，
#    避免审核 worker 与赛道识别同时下载同一份大文件、白占一倍带宽；
#  · 全局最多同时下载 2 个视频，防止多人上传时把磁盘 IO / 带宽打满。
_FRAME_LOCKS: Dict[str, Any] = {}
_FRAME_LOCKS_GUARD = threading.Lock()
_DOWNLOAD_SEMA = threading.Semaphore(2)


def _frame_lock(key: str) -> Any:
    """取某个源地址对应的锁（没有则创建），保证同一视频串行抽帧。"""
    with _FRAME_LOCKS_GUARD:
        lk = _FRAME_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _FRAME_LOCKS[key] = lk
        return lk


# 帧文件名里带时间点（frame0_23.5s.jpg）：读缓存时能直接反推每帧对应的秒数，
# 不必再跑 ffprobe，也不依赖额外的时间表文件。
_FRAME_NAME_RE = re.compile(r"^frame(\d+)_([\d.]+)s\.jpg$")


def _list_cached_frames(frame_dir: str, n: int):
    """缓存目录里 >= n 帧则返回按时间排序的 [(路径, 秒数)]，否则 None。"""
    try:
        names = os.listdir(frame_dir)
    except OSError:
        return None
    items = []
    for name in names:
        m = _FRAME_NAME_RE.match(name)
        if m:
            items.append((os.path.join(frame_dir, name), float(m.group(2))))
    items.sort(key=lambda x: x[1])
    return items[:n] if len(items) >= n else None


def extract_video_frames_with_times(src: str, n: int = 6):
    """按视频时长均匀抽 n 帧，返回 [(data URL, 秒数)]。

    比 extract_video_frames 多带时间点：审核时要告诉模型「每帧对应第几秒」，
    它才能报出违规发生在哪个时间点，进而据此定位违规截图。

    src 可以是本地路径或 http(s) URL；并发控制见 extract_video_frames。
    """
    import hashlib
    if not src:
        return []
    is_url = src.startswith("http://") or src.startswith("https://")
    if not is_url and not os.path.exists(src):
        return []
    # 缓存 key 用原始地址（去掉每次变化的签名参数），下载后的临时文件名是随机的，
    # 直接拿它做 key 会让缓存永不命中、每次重审都白抽一遍帧。
    key_src = src.split("?")[0]
    key = hashlib.md5(key_src.encode("utf-8")).hexdigest()[:12]
    frame_dir = os.path.join(_TMP_ROOT, f"nf_frames_{key}")

    items = _list_cached_frames(frame_dir, n)
    if not items:
        with _frame_lock(key):
            # 等锁期间可能已有别的线程抽完并写好缓存了
            items = _list_cached_frames(frame_dir, n)
            if not items:
                items = _extract_frames_uncached(src, frame_dir, n)
    out = []
    for p, t in items:
        du = _local_image_data_url(p)
        if du:
            out.append((du, t))
    return out


def extract_video_frames(src: str, n: int = 6) -> List[str]:
    """按视频时长均匀抽 n 帧，返回 data URL 列表。

    超大视频没法直接送 video_url，用多帧画面代替，保证模型仍能"看到"全片
    内容——比只塞一张封面图可靠得多（用户反馈"没解读我的视频内容"）。

    并发控制（审核 worker 与赛道识别会同时来要同一份视频）：
      · 同一源地址只允许一个线程在抽，其余等它完成后直接读缓存；
      · 全局最多同时下载 2 个视频，避免多人上传时打满磁盘 IO 与带宽。
    """
    return [du for du, _t in extract_video_frames_with_times(src, n)]


def extract_frame_at(src: str, seconds: float) -> bytes:
    """抽取指定时间点的单帧画面，返回 JPEG 字节；失败返回 b""。

    用于「违规证据截图」：模型报出违规时间点后，按该时间点精确定位画面，
    让用户能直接看到到底是哪一帧出了问题。
    """
    import subprocess as _sp
    if not src or seconds is None or seconds < 0:
        return b""
    is_url = src.startswith("http://") or src.startswith("https://")
    if not is_url and not os.path.exists(src):
        return b""
    tmp_video = ""
    if is_url:
        with _DOWNLOAD_SEMA:
            tmp_video = _download_video_to_temp(src)
        if not tmp_video:
            return b""
        src = tmp_video
    out_path = ""
    try:
        fd, out_path = tempfile.mkstemp(suffix=".jpg", prefix="nf_shot_", dir=_TMP_ROOT)
        os.close(fd)
        try:
            _sp.run(
                ["ffmpeg", "-y", "-ss", f"{max(0.0, float(seconds)):.2f}", "-i", src,
                 "-vframes", "1", "-q:v", "3", out_path],
                capture_output=True, timeout=90,
            )
        except Exception:
            return b""
        try:
            with open(out_path, "rb") as f:
                data = f.read()
        except OSError:
            return b""
        return data if len(data) > 1024 else b""
    finally:
        for p in (out_path, tmp_video):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


def _parse_timestamp(v: Any) -> Optional[float]:
    """把模型返回的 timestamp 转成秒数；无法解析返回 None。

    模型可能给数字（12.5），也可能给「第12秒」「00:15」「12.5s」这类字符串，
    这里统一兜住，解析不出来就当没给（不生成截图）。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v >= 0 else None
    s = str(v).strip()
    if not s:
        return None
    m = re.search(r"(\d{1,2}):(\d{1,2})(?:[.:](\d{1,2}))?", s)     # 01:23 / 01:23.5
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)) + (int(m.group(3)) / 100 if m.group(3) else 0)
    m = re.search(r"(\d+(?:\.\d+)?)", s)                            # 12 / 12.5 / 第12秒
    return float(m.group(1)) if m else None


def _attach_evidence_images(violations: List[Dict[str, Any]], video_src: str) -> None:
    """给违规条目补「违规画面截图」——用户需求：AI 判违规时把那一帧截下来。

    按模型报出的时间点精确定位画面 → 存 COS（未启用 COS 时落本地 uploads），
    结果写回该条违规的 evidence_key / evidence_time，前端据此展示缩略图。
    每条违规一张、最多 _EVIDENCE_MAX 张，避免同一视频反复抽帧拖慢审核。
    """
    targets = [v for v in (violations or []) if v.get("evidence_time")][:_EVIDENCE_MAX]
    if not video_src or not targets:
        return
    # 远端源只下载一次，多张截图都从同一份本地副本上抽，避免同一大视频重复下载
    local_src = video_src
    tmp_video = ""
    if video_src.startswith("http://") or video_src.startswith("https://"):
        with _DOWNLOAD_SEMA:
            tmp_video = _download_video_to_temp(video_src)
        if not tmp_video:
            return
        local_src = tmp_video
    try:
        for v in targets:
            data = extract_frame_at(local_src, float(v["evidence_time"]))
            if not data:
                continue
            key = f"evidence/{hashlib.md5(data).hexdigest()}.jpg"
            saved = ""
            if cos_service.is_enabled():
                try:
                    saved = cos_service.upload_bytes(data, key, "image/jpeg") or ""
                except Exception as e:
                    print(f"[ai_auditor] 违规截图上传 COS 失败: {e}", flush=True)
            if saved:
                v["evidence_key"] = saved
            else:
                # 本地兜底（开发环境 / COS 未配置）：/uploads 已由 main.py 挂载
                try:
                    local_dir = os.path.join(UPLOAD_DIR, "evidence")
                    os.makedirs(local_dir, exist_ok=True)
                    with open(os.path.join(local_dir, os.path.basename(key)), "wb") as f:
                        f.write(data)
                    v["evidence_local"] = "/uploads/evidence/" + os.path.basename(key)
                except OSError:
                    pass
    finally:
        if tmp_video:
            try:
                os.remove(tmp_video)
            except OSError:
                pass


def evidence_image_inputs(v: Dict[str, Any], video_src: str = "",
                          media: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    """形近错字 OCR 复核要用的「图片」输入（必须真的是图片，不能是视频）。

    之前这个复核总是失败在「image format is illegal / file size exceeds 64MB」——
    因为它把整段视频的 URL 当图片发给了模型（视频素材的 cos_url 就是视频）。
    取图优先级：① 违规截图（已按 evidence_time 精确抽帧）② 本地视频按时间点抽帧
    ③ 封面/缩略图等图片类素材（跳过视频）。
    """
    out: List[str] = []

    # ① 违规截图：COS 优先，其次本地 /uploads/evidence
    key = v.get("evidence_key") or ""
    if key and cos_service.is_enabled():
        try:
            u = cos_service.presigned_url(key, expires=7200)
            if u:
                out.append(u)
        except Exception:
            pass
    local_ev = v.get("evidence_local") or ""
    if not out and local_ev:
        # 本机 /uploads 地址会被 CDN 防盗链 403，这里改用 data URL 直接喂模型
        du = _local_image_data_url(os.path.join(UPLOAD_DIR, os.path.basename(local_ev)))
        if du:
            out.append(du)
    if out:
        return out

    # ② 本地/远端视频按违规时间点抽一帧，转 data URL
    ts = v.get("evidence_time")
    if video_src and ts:
        src, tmp = video_src, ""
        if src.startswith("http://") or src.startswith("https://"):
            tmp = _download_video_to_temp(src) or ""
            src = tmp
        data = extract_frame_at(src, float(ts)) if src else None
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
        if data:
            u = _encode_data_url(data, "frame.jpg")
            if u:
                out.append(u)
        if out:
            return out

    # ③ 图片类素材（跳过视频）
    for m in (media or []):
        if not isinstance(m, dict) or m.get("is_video"):
            continue
        for k in ("thumb_url", "cover_url", "frame_url", "preview_url", "cos_url", "url"):
            u = m.get(k)
            if isinstance(u, str) and u.startswith("http") and not _looks_like_video(u):
                out.append(u)
                break
        if out:
            break
    return out


_VIDEO_EXT_RE = re.compile(r"\.(mp4|mov|m4v|avi|mkv|webm|flv|wmv)(\?|$)", re.I)


def _looks_like_video(url: str) -> bool:
    return bool(_VIDEO_EXT_RE.search(url or ""))


def probe_duration(src: str) -> float:
    """用 ffprobe 读媒体时长（秒）。

    读不到（格式异常 / ffprobe 不可用）时返回 0.0 —— 调用方据此放行而不是误拦，
    宁可漏过也不能把正常视频挡在门外。
    """
    import subprocess as _sp
    try:
        r = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src],
            capture_output=True, text=True, timeout=60,
        )
        return float((r.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _extract_frames_uncached(src: str, frame_dir: str, n: int):
    """真正干活的部分：下载（如需）→ 取时长 → 均匀抽帧。返回 [(路径, 秒数)]。"""
    import subprocess as _sp
    try:
        os.makedirs(frame_dir, exist_ok=True)
    except OSError:
        return []
    _prune_frame_cache()

    tmp_video = ""
    if src.startswith("http://") or src.startswith("https://"):
        with _DOWNLOAD_SEMA:
            tmp_video = _download_video_to_temp(src)
        if not tmp_video:
            return []
        src = tmp_video
    try:
        dur = 0.0
        try:
            r = _sp.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", src],
                capture_output=True, text=True, timeout=60,
            )
            dur = float((r.stdout or "").strip() or 0)
        except Exception:
            dur = 0.0
        # 取不到时长就按固定时间点；取得到就均匀取样并避开首尾
        if dur > 0:
            times = [dur * (i + 0.5) / n for i in range(n)]
        else:
            times = [1.0, 3.0, 6.0, 10.0, 15.0, 20.0][:n]
        out = []
        for i, t in enumerate(times):
            frame = os.path.join(frame_dir, f"frame{i}_{t:.1f}s.jpg")
            if not os.path.exists(frame):
                try:
                    _sp.run(
                        ["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", src,
                         "-vframes", "1", "-q:v", "4", frame],
                        capture_output=True, timeout=60,
                    )
                except Exception:
                    continue
            if os.path.exists(frame) and os.path.getsize(frame) > 512:
                out.append((frame, t))
        return out
    finally:
        # 临时视频用完即删；帧图保留作缓存，由 _prune_frame_cache 定期清理
        if tmp_video:
            try:
                os.remove(tmp_video)
            except OSError:
                pass


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
    """调用 OpenAI 兼容模型，流式拼接文本输出（带限流退避重试）。

    整个调用（含退避重试）都在全进程并发闸门 _AI_GATE 内执行：
    审核队列 worker 与请求线程（识别赛道 / 建档打分 / 连通性测试）共用同一上限，
    避免并发突发把上游打到限流。
    """
    import time as _time
    from openai import OpenAI

    client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
    last_err: Optional[Exception] = None
    with _AI_GATE:
        # 并发突发会触发「Request rate increased too quickly」限流，指数退避重试
        for attempt in range(4):
            try:
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
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if any(k in msg for k in ("rate", "limit", "429", "too many", "throttl", "overloaded")):
                    _time.sleep(min(20, 1.5 * (2 ** attempt)))
                    continue
                raise
    raise last_err


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

# 模型自称"不确定/需人工复核"的措辞：这类判定不能直接驳回，降为 medium 进人工复核。
# （否则会出现"结论写建议人工复核、状态却是已驳回"的自相矛盾结果）
_AI_UNCERTAIN_RE = re.compile(
    r"(需|建议|请|应)[^，。；,；]{0,4}人工(复核|确认|核实|判断|裁定)"
    r"|需(要)?进一步(确认|核实|判断|比对)"
    r"|无法(确认|判定|确定|判断|辨认)"
    r"|难以(判断|确认|辨别|认定)"
    r"|(疑似|不确定|暂不确定|仅供参考|存疑|待核实|需核实)"
)
# 真正的硬红线：措辞再含糊也不能降级为人工复核
_HARD_REDLINE_RE = re.compile(
    r"(涉政|政治敏感|色情|低俗|暴力|恐怖|赌博|毒品|违法违规|一票否决|红线)"
)
# 品牌产品视觉一致性规则里"不是实物产品"的说法：拟人化/IP 形象不适用这条规则
_BRAND_VISUAL_NON_REAL = (
    "吉祥物", "卡通", "拟人化", "拟人", "动画角色", "动画形象", "IP形象", "IP 形象",
    "插画", "手绘", "虚拟形象", "并非实物", "不是实物", "非实物", "无实物产品",
)

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


_BRAND_TYPO_WORDS = [
    "南福", "南符", "南浮", "南扶", "南乎", "南弗",
    "丰篮", "益园", "园圆", "传印", "益元", "一元",
    "丰蓝1好", "丰蓝一号",
    # 画面里的形近错字（"益圆"笔画被写歪/被艺术字处理成这些字形）
    "米圆", "米园", "盆圆", "盆园", "盗圆", "益囲", "溢圆", "易圆",
]


# 复判 prompt：给 AI 一份对照表，让它拿着「画面」+「OCR 文字」做最终判断
# 输出强制 JSON，给后续流程（决定保留/丢弃）用
_TYPO_RECHECK_SYS = (
    "你是品牌名形近错字的复判员，需要拿【第一次 AI 的判定】对照【OCR 文字】"
    "和【画面图片】做最终结论。\n"
    "只针对画面里品牌名/文字是否写错做复判，其它违规一律不复判。\n"
    "\n"
    "判定规则：\n"
    "1) 如果 OCR 文字里有 X、且画面里也确认识别为 X（与 X 字形一致），同时 X 是"
    "一个形近字（米圆/米园/益园/益元/盆圆/盗圆/益囲/溢圆/易圆/南福/南符/南浮/南扶/"
    "南乎/南弗/丰篮/传印/园圆/一元/丰蓝1好/丰蓝一号）→ 画面里【真的写错】了，"
    "保留违规 verdict=uphold。\n"
    "2) 如果 OCR 文字里出现的是 Y（标准品牌名：南孚/益圆/丰蓝/传应），且画面里也"
    "是 Y（OCR 反映的字符与画面一致）→ 第一次 AI 把 Y 看成 X 是误判，丢弃违规"
    " verdict=discard。\n"
    "3) 如果 OCR 文字完全识别不到 X 或 Y（画面模糊、字体艺术化），"
    "verdict=uncertain，由人工复核。\n"
    "4) OCR 与第一次 AI 判定结论一致 → verdict=uphold（无需再怀疑）。\n"
    "5) 第一次 AI 判定的 evidence 里出现的具体错字形（米圆/南福/丰篮/传印 等）"
    "是 OCR 对照的关键词，必须在 OCR 文字里逐字查一遍。\n"
    "\n"
    "必须仅输出 JSON，不要任何额外文字，格式：\n"
    '{"verdict":"uphold|discard|uncertain",'
    '"reason":"<一句话说明依据>",'
    '"ocr_seen":["<OCR 里实际读到的疑似错字>"],'
    '"standard_seen":["<OCR 里读到的标准写法>"]}'
)


# 同步 OCR 提示词：和复判共用 OCR 部分，单独抽出来便于第一次审核和复判复用
_OCR_SYS = (
    "你是 OCR 助手。逐张图把画面上【所有能看清的文字】原样读出来，"
    "保留原始字形（看到「米圆」就写「米圆」，不要自动改成「益圆」）。\n"
    "【强约束】只读你【直接看到】的字；看不清、笔画缺失、艺术字变形的标注 [模糊]。\n"
    "读图时对品牌名要特别仔细：「益圆 = 氵 + 益(皿字底) + 圆」「南孚 = 南 + 孚」"
    "「丰蓝 = 丰 + 蓝」「传应 = 传 + 应」，要逐字核对氵/皿/口/竹/示/监 等偏旁是否存在。\n"
    "必须仅输出 JSON：{\"ocr\":[\"<图1的文字>\",\"<图2的文字>\"]}\n"
    "一张图没字就空字符串。不要其它文字。"
)


def ocr_brand_text(image_urls: List[str]) -> Dict[str, Any]:
    """同步 OCR：让多模态模型逐张读画面里的文字，用于 AI 审核前的"取证"。

    返回 {"ocr_raw": [<图1 OCR>, ...], "ocr_text_all": "拼接后的整段", "model": str, "error": str|None}
    与 recheck_typo_violation 共享同一套提示词，确保第一次审核与复判拿到一致对照。
    """
    out = {"ocr_raw": [], "ocr_text_all": "", "model": "", "error": None}
    if not image_urls or not is_ready():
        if not image_urls:
            out["error"] = "无画面"
        else:
            out["error"] = "AI 模型未配置"
        return out
    cfg = load_config()
    user_parts: list = [{"type": "text", "text": "请逐张图读出画面里的文字。"}]
    for u in image_urls:
        user_parts.append({"type": "image_url", "image_url": {"url": u}})
    messages = [
        {"role": "system", "content": _OCR_SYS},
        {"role": "user", "content": user_parts},
    ]
    try:
        raw = _call_model(cfg, messages)
    except Exception as e:
        out["error"] = f"OCR 调用失败：{e}"
        return out
    data = _extract_json(raw)
    if not isinstance(data, dict):
        out["error"] = "OCR 返回内容无法解析"
        return out
    ocr_raw = data.get("ocr") or []
    if not isinstance(ocr_raw, list):
        ocr_raw = [str(ocr_raw)]
    out["ocr_raw"] = [str(t or "") for t in ocr_raw]
    out["ocr_text_all"] = " / ".join(out["ocr_raw"])
    out["model"] = cfg.get("model", "")
    return out


# ── 品牌参考学习：把多模态模型对参考图的"字形指纹 + 包装布局"描述写进 DB ──
# audit() 时只把"描述文本"塞进 system prompt，0 额外图片 token。
_BRAND_REF_LEARN_SYS = (
    "你是品牌视觉分析员。用户会给你一张标准品牌产品图，请按下面 4 段生成 JSON 描述，"
    "用于后续审核员（多模态模型）逐字核对画面里的品牌名是否正确。\n"
    "\n"
    "【必须描述的 4 段内容】\n"
    "1) brand_logo: 这个品牌的标准 logo 长什么样（文字字形、字色、是否有图形）\n"
    "2) brand_chars: 品牌名每个字的【字形构成】——画清楚每个字由什么偏旁部首组成，"
    "   这是防误识别的关键。例如：\n"
    "     \"益\" = 氵(三点水) + 益(下面皿字底)；\n"
    "     \"圆\" = 冂 + 员；\n"
    "     \"孚\" = 子 + 爪头；\n"
    "     \"监/蓝\" = 左边的绞丝旁 + 监 / 监 + 艹/竹\n"
    "     \"应\" = 广 + 口 + 一\n"
    "3) packaging: 产品包装上的主色 / 配色 / Logo 出现位置（顶部/中部/全图）\n"
    "4) typos_to_avoid: 这张图最容易被错认的【形近字】列表（基于字形相似度），"
    "   写 5-10 个最可能误识的近似字。例：\n"
    "     \"益圆\"易被看成: 米圆/米园/益园/益元/盆圆/盗圆/易圆/益囲/溢圆/园圆/一元\n"
    "     \"南孚\"易被看成: 南福/南符/南浮/南扶/南乎/南弗\n"
    "     \"丰蓝\"易被看成: 丰篮/丰蓝1好/丰蓝一号\n"
    "     \"传应\"易被看成: 传印/传映/传映\n"
    "\n"
    "必须仅输出 JSON：{\"brand_logo\":\"\",\"brand_chars\":\"\",\"packaging\":\"\",\"typos_to_avoid\":[\"\",\"\"]}"
)


def learn_brand_description(brand: str, image: str) -> Dict[str, Any]:
    """让多模态模型对一张品牌产品图生成"字形指纹 + 包装布局"描述。

    image 可以是：
    - data URL: data:image/png;base64,xxxx
    - HTTP URL: https://...

    返回 {"brand": str, "description": dict, "model": str, "error": str|None}
    """
    out = {"brand": brand, "description": {}, "model": "", "error": None}
    if not is_ready():
        out["error"] = "AI 模型未配置"
        return out
    cfg = load_config()
    try:
        raw = _call_model(cfg, [
            {"role": "system", "content": _BRAND_REF_LEARN_SYS},
            {"role": "user", "content": [
                {"type": "text", "text": f"请描述「{brand}」的标准品牌产品图。"},
                {"type": "image_url", "image_url": {"url": image}},
            ]},
        ])
    except Exception as e:
        out["error"] = f"AI 调用失败：{e}"
        return out
    data = _extract_json(raw)
    if not isinstance(data, dict):
        out["error"] = "返回内容无法解析"
        return out
    out["description"] = data
    out["model"] = cfg.get("model", "")
    return out


def _get_brand_reference_images() -> List[Dict[str, str]]:
    """取已学习品牌的标准产品图 URL，供审核模型直接逐字比对。"""
    refs = []
    try:
        with database.db_cursor() as cur:
            cur.execute("""
                SELECT d.brand, i.cos_key
                FROM brand_ref_descriptions d
                JOIN brand_ref_images i ON i.brand = d.brand
                WHERE i.cos_key IS NOT NULL AND i.cos_key != ''
                ORDER BY d.id
            """)
            for row in cur.fetchall():
                url = cos_service.presigned_url(row["cos_key"], expires=7200)
                if url:
                    refs.append({"brand": row["brand"], "url": url})
    except Exception as e:
        print(f"[ai_auditor] 读取品牌参考图失败: {e}", flush=True)
    return refs


def _format_brand_refs_for_prompt() -> str:
    """从 DB 读所有 brand_ref_descriptions，组装成"品牌参考清单"文本块。

    读 DB 的代价 = 1 次 SELECT（每条审核都做），用 cache 兜底。
    """
    import database
    rows = []
    try:
        with database.db_cursor() as cur:
            cur.execute("SELECT brand, description, generated_at FROM brand_ref_descriptions ORDER BY id")
            for r in cur.fetchall():
                rows.append({"brand": r["brand"], "description": r["description"],
                             "generated_at": r["generated_at"]})
    except Exception:
        return ""
    if not rows:
        return ""
    lines = ["【公司 4 个标准品牌产品图 —— 逐字核对参考清单（多模态模型已经看过原图，"
             "请把这份字形指纹当作「绝对基准」与画面/OCR 文字逐字对照）】"]
    for r in rows:
        d = r["description"]
        if not isinstance(d, dict):
            # 兜底：description 存的是 JSON 字符串
            try:
                d = json.loads(d) if isinstance(d, str) and d.strip().startswith("{") else {}
            except Exception:
                d = {}
        lines.append(f"\n**{r['brand']}**：")
        if d.get("brand_logo"):
            lines.append(f"  - Logo: {d['brand_logo']}")
        if d.get("brand_chars"):
            lines.append(f"  - 字形构成: {d['brand_chars']}")
        if d.get("packaging"):
            lines.append(f"  - 包装: {d['packaging']}")
        if d.get("typos_to_avoid"):
            typos = d["typos_to_avoid"]
            if isinstance(typos, list):
                lines.append(f"  - 易误识的形近字（{len(typos)}）: {' / '.join(typos)}")
    return "\n".join(lines) + "\n"


def recheck_typo_violation(
    image_urls: List[str],
    first_evidence: str,
    first_rule_name: str,
    first_field: str,
    full_text: str,
) -> Dict[str, Any]:
    """形近错字类违规的二次确认：先 OCR 取证，再让 AI 复判，给出最终 verdict。

    - verdict=uphold：画面真的写错，保留违规
    - verdict=discard：画面其实写的是标准写法，第一次 AI 误判，丢弃违规
    - verdict=uncertain：OCR/AI 都拿不准，转人工

    返回 {"verdict": str, "reason": str, "ocr_seen": [..], "standard_seen": [..],
          "ocr_raw": [..每张图的 OCR 文本..], "model": str, "error": str|None}
    """
    if not image_urls:
        return {"verdict": "uncertain", "reason": "无画面可取证", "ocr_seen": [],
                "standard_seen": [], "ocr_raw": [], "model": "", "error": None}
    if not is_ready():
        return {"verdict": "uncertain", "reason": "AI 模型未配置",
                "ocr_seen": [], "standard_seen": [], "ocr_raw": [],
                "model": "", "error": "AI 模型未配置"}

    cfg = load_config()

    # ── 第一步：先调多模态模型读图里的文字（OCR 取证） ──
    ocr = ocr_brand_text(image_urls)
    ocr_raw = ocr.get("ocr_raw") or []
    ocr_text_all = ocr.get("ocr_text_all") or ""
    if ocr.get("error") and not ocr_text_all:
        return {"verdict": "uncertain", "reason": ocr["error"],
                "ocr_seen": [], "standard_seen": [], "ocr_raw": ocr_raw,
                "model": ocr.get("model", ""), "error": ocr["error"]}

    # ── 第二步：拿 OCR 文字 + 第一次 AI 判定 + 画面，让 AI 复判 ──
    recheck_user = (
        f"【第一次 AI 判定】\n"
        f"  规则：{first_rule_name}\n"
        f"  来源：{first_field}\n"
        f"  依据：{first_evidence}\n\n"
        f"【OCR 取证结果】（来自你刚刚识别每张画面图得到的文字）\n"
        f"  {ocr_text_all}\n\n"
        f"【文案/标题/口播】\n"
        f"  {full_text}\n\n"
        f"请对比【第一次 AI 判定】与【OCR 文字】是否一致，"
        f"结合【画面图片】做出最终结论。\n"
        f"只输出 JSON，不要任何额外文字。"
    )
    recheck_msgs = [
        {"role": "system", "content": _TYPO_RECHECK_SYS},
        {"role": "user", "content": [
            {"type": "text", "text": recheck_user},
            *[{"type": "image_url", "image_url": {"url": u}} for u in image_urls],
        ]},
    ]
    try:
        recheck_raw = _call_model(cfg, recheck_msgs)
    except Exception as e:
        return {"verdict": "uncertain", "reason": f"复判调用失败：{e}",
                "ocr_seen": [], "standard_seen": [], "ocr_raw": ocr_raw,
                "model": ocr.get("model", ""), "error": str(e)}
    recheck_data = _extract_json(recheck_raw)
    if not isinstance(recheck_data, dict):
        return {"verdict": "uncertain", "reason": "复判结果无法解析",
                "ocr_seen": [], "standard_seen": [], "ocr_raw": ocr_raw,
                "model": ocr.get("model", ""), "error": "复判解析失败"}

    verdict = str(recheck_data.get("verdict") or "").strip().lower()
    if verdict not in ("uphold", "discard", "uncertain"):
        verdict = "uncertain"
    return {
        "verdict": verdict,
        "reason": str(recheck_data.get("reason") or "").strip(),
        "ocr_seen": recheck_data.get("ocr_seen") or [],
        "standard_seen": recheck_data.get("standard_seen") or [],
        "ocr_raw": ocr_raw,
        "ocr_text_all": ocr_text_all,
        "model": cfg.get("model", ""),
        "error": None,
    }



# 「X 误写为 Y」中 X/Y 两端常包着引号、括号或空白（如 ‘南孚’、“南孚”、「南孚」），
# 这些噪声字符必须先吃掉，否则「‘南孚’误写为‘南孚’」这类幻觉会漏过滤。
_TYPO_NOISE = r"[\s\"'“”‘’「」『』《》〈〉()（）\[\]【】〔〕,，.。:：;；、]"
_TYPO_WRONG_RE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{1,8})" + _TYPO_NOISE + r"*"
    r"[误错]写(?:成|为|成了|作)" + _TYPO_NOISE + r"*"
    r"([\u4e00-\u9fa5A-Za-z0-9]{1,8})"
)


def _is_typo_self_contradiction(evidence: str) -> bool:
    """错别字自相矛盾过滤。

    AI 有时会输出「将 X 误写为 X」这类前后相同的依据（例如把正确的「南孚」说成误写为「南孚」），
    说明它根本没有指出任何实际错字，属于幻觉，应剔除。

    允许左侧带上「字幕中把」这类上下文，因此除完全相等外，
    只要左侧以右侧结尾（如「字幕中把南孚→南孚」）也判为自相矛盾。
    """
    e = (evidence or "").strip()
    if not e:
        return False
    for m in _TYPO_WRONG_RE.finditer(e):
        left = m.group(1).strip()
        right = m.group(2).strip()
        if not left or not right:
            continue
        if left == right:
            return True
        # 上下文差异：左侧比右侧多出「字幕中把」这类前缀，短的至少 2 字才算同一词
        if min(len(left), len(right)) >= 2 and (left.endswith(right) or right.endswith(left)):
            return True
    return False


# AI 在错别字规则里自称「虽非严格错别字」「并非错别字」时，说明它没找到真实错字，
# 只是措辞建议，属于误报。
_NOT_TYPO_RE = re.compile(r"(?:并非|不是|不属于|不算|非)[^，。；！？,;!?\n]{0,8}错别字")


def _is_not_a_typo_judgement(rule_name: str, evidence: str) -> bool:
    """错别字规则的「其实不是错别字」过滤。

    例如依据写着「文案中'居家用电'表述略显生硬，虽非严格错别字，但建议优化为…」，
    模型自己都承认没有错字，这种措辞建议不该算错别字违规。
    """
    if "错别字" not in (rule_name or "").replace(" ", ""):
        return False
    return bool(_NOT_TYPO_RE.search(evidence or ""))


# 「正确写法」与「实际显示」两种表述，用于识别"正确写法=实际写法"的矛盾判定
_TYPO_CORRECT_RE = re.compile(
    r"(?:应为|应写作|正确应为|正确为|正确写法(?:应)?为)" + _TYPO_NOISE + r"*([\u4e00-\u9fa5A-Za-z0-9]{1,8})"
)
_TYPO_SHOWN_RE = re.compile(
    r"(?:显示为|显示是|写作|写成|写为|误写为|错写为)" + _TYPO_NOISE + r"*([\u4e00-\u9fa5A-Za-z0-9]{1,8})"
)


def _is_typo_same_word_claim(rule_name: str, evidence: str) -> bool:
    """错别字规则的「正确写法 = 实际显示」矛盾过滤。

    AI 会写出「'南孚'应为'南孚'（正确写法），但画面显示为'南孚'」
    或「显示为'南孚'（正确写法应为'南孚'）」——正确写法与实际显示是同一个词，
    等于没有指出任何错字，属误报，应剔除。
    """
    if "错别字" not in (rule_name or "").replace(" ", ""):
        return False
    e = evidence or ""
    correct = {m.group(1) for m in _TYPO_CORRECT_RE.finditer(e)}
    shown = {m.group(1) for m in _TYPO_SHOWN_RE.finditer(e)}
    return bool(correct & shown)


def _is_brand_typo_hallucination(rule_name: str, full_text: str, field: str = "",
                                  evidence: str = "") -> bool:
    """品牌名错误规则的幻觉过滤。

    按你选的「文案里出现也保留画面证据」策略：画面/视频来源一律不因文案而丢弃。
    因为画面里写错字（文案写「益圆」、画面画成「米圆」）正是这条规则要抓的最常见场景，
    不能用文案正确去否定画面判定。

    保留的兜底过滤：evidence 来自「文案/标题/字幕/口播」且全文搜不到任何形近字时，
    视为 AI 套话丢弃——AI 有时会输出「文案里把 X 误写为 X」这种没意义的话。
    """
    if not rule_name or "品牌名" not in rule_name:
        return False
    if not field.startswith(("文案", "标题", "字幕", "口播")):
        return False  # 画面/视频：文案正确不能否定画面错误
    if not full_text or full_text == "（无文案）":
        return False
    # 文本型 evidence：若全文里连一个形近字都没出现，整条依据就是 AI 套话
    return not any(w in full_text for w in _BRAND_TYPO_WORDS)


def _is_brand_typo_overridden_by_ocr(rule_name: str, evidence: str,
                                     ocr_text: str) -> bool:
    """预 OCR 文字与 AI 判定"画面形近字违规"矛盾 → 视为 AI 误判丢弃。

    触发条件（任一即可）：
    1) AI 报「品牌名称错误」违规，evidence 里出现形近字 X（米圆/南福/丰篮/传印 等）
       且 OCR 文字里**没有**X，但**有**对应的标准写法 Y（益圆/南孚/丰蓝/传应）
    2) 「错别字」类规则，evidence 里"显示为 X"的 X 是形近字，且 OCR 里只有 Y 没有 X

    这是从"形近字幻觉过滤"独立出来的二次过滤，因为预 OCR 文字是
    独立于"看图印象"的字面结果，更可靠。
    """
    e = (evidence or "").strip()
    if not e or not ocr_text:
        return False
    rn = (rule_name or "").replace(" ", "")
    if "品牌名" not in rn and "错别字" not in rn:
        return False
    # 提取 evidence 里 AI 自称看到的形近字
    xs = set()
    for w in _BRAND_TYPO_WORDS:
        if w in e:
            xs.add(w)
    # 也从"显示为 X""误写为 X"句式里取
    for m in _TYPO_SHOWN_RE.finditer(e):
        if m.group(1) in _BRAND_TYPO_WORDS:
            xs.add(m.group(1))
    if not xs:
        return False
    # X 标准写法映射（O(1) 查表）
    _STANDARD_OF = {
        "米圆": "益圆", "米园": "益圆", "益园": "益圆", "益元": "益圆",
        "盆圆": "益圆", "盗圆": "益圆", "益囲": "益圆", "溢圆": "益圆", "易圆": "益圆",
        "园圆": "益圆", "一元": "益圆",
        "南福": "南孚", "南符": "南孚", "南浮": "南孚", "南扶": "南孚",
        "南乎": "南孚", "南弗": "南孚",
        "丰篮": "丰蓝", "丰蓝1好": "丰蓝", "丰蓝一号": "丰蓝",
        "传印": "传应",
    }
    for x in xs:
        y = _STANDARD_OF.get(x, "")
        if not y:
            continue
        if y in ocr_text and x not in ocr_text:
            return True
    return False


def _is_typo_like_violation(rule_name: str, full_text: str, field: str,
                             evidence: str) -> bool:
    """形近错字类违规：判断这条违规是否应该走「OCR 取证 + AI 复判」流程。

    触发条件（任一即可）：
    1) 「品牌名称错误」规则，evidence 里有形近字 X（米圆/南福/丰篮/传印 等）
    2) 「错别字」类规则，evidence 里"显示为/写成/误写为"的 X 是个形近字
    3) 来源是画面/视频——画面 OCR 本来就是关键证据

    注意：与 `_is_brand_typo_hallucination` 互补——后者判断"是否虚报"，前者判断
    "是否值得复判"。两个都用同一个形近字白名单 _BRAND_TYPO_WORDS。
    """
    e = (evidence or "").strip()
    if not e:
        return False
    rn = (rule_name or "").replace(" ", "")
    if "品牌名" in rn:
        # 任一形近字出现就算——不论来自画面还是文案，都需要 OCR 取证
        return any(w in e for w in _BRAND_TYPO_WORDS)
    if "错别字" in rn:
        # 错别字规则里 evidence 提到"显示为 X"且 X 是形近字
        shown = {m.group(1) for m in _TYPO_SHOWN_RE.finditer(e)}
        return any(x in _BRAND_TYPO_WORDS for x in shown)
    return False


def _auto_recheck_typos(violations: List[Dict[str, Any]], image_urls: List[str],
                        full_text: str, video_src: str = "",
                        media: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """对「会直接驳回」的形近错字类违规自动做一次 OCR 取证复判。

    模型会把实物包装上的「益圆」误读成「米圆」、把「南孚」误读成「南福」，
    这类误报一旦按 forbidden 直接驳回，代价太大。所以在定状态之前自动复核：

      - verdict=discard（画面里确实是正确写法）→ 剔除该条违规
      - verdict=uphold（画面里确实是错字）→ 保留，并带上 OCR 证据
      - 其它（看不清 / 无法确认）→ 降为 medium，交人工复核，不自动驳回
    """
    todo = [v for v in violations
            if v.get("needs_ocr_review") and str(v.get("severity")) in ("high", "forbidden")]
    if not todo:
        return violations
    kept: List[Dict[str, Any]] = []
    for v in violations:
        if v not in todo:
            kept.append(v)
            continue
        # 复核必须用「图片」：优先违规截图，其次按时间点抽帧，最后才是图片类素材。
        # 直接把视频 URL 当图片传会报 image format is illegal / 超过 64MB。
        imgs = evidence_image_inputs(v, video_src, media) or [
            u for u in image_urls if not _looks_like_video(u)
        ]
        if not imgs:
            v = dict(v)
            v["recheck_verdict"] = "uncertain"
            v["recheck_reason"] = "没有可用的画面截图，转人工复核"
            v["severity"] = "medium"
            kept.append(v)
            continue
        try:
            res = recheck_typo_violation(
                image_urls=imgs,
                first_evidence=str(v.get("matched_text") or ""),
                first_rule_name=str(v.get("rule_name") or ""),
                first_field=str(v.get("field") or "").rstrip("（AI）"),
                full_text=full_text,
            )
        except Exception as e:
            res = {"verdict": "uncertain", "reason": f"复判异常：{e}"}
        verdict = res.get("verdict") or "uncertain"
        v = dict(v)
        v["recheck_verdict"] = verdict
        v["recheck_reason"] = res.get("reason", "")
        v["recheck_ocr"] = res.get("ocr_text_all", "") or " / ".join(res.get("ocr_raw") or [])
        v["recheck_ocr_seen"] = res.get("ocr_seen") or []
        v["recheck_standard_seen"] = res.get("standard_seen") or []
        v["recheck_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        v["recheck_auto"] = True
        if verdict == "discard":
            print(f"[ai_auditor] 形近错字自动复核剔除：{v.get('rule_name')}", flush=True)
            continue
        if verdict != "uphold":
            v["severity"] = "medium"
        kept.append(v)
    return kept


def _should_pre_ocr(full_text: str, caption: str = "", transcript: str = "",
                     ocr_text: str = "") -> bool:
    """是否在第一次审核前就同步跑 OCR 取证。

    触发条件（任一即可）：
    1) 全文/标题/口播里出现任一标准品牌名（南孚/益圆/丰蓝/传应）→ 画面里大概率也有
       这些品牌的露出，先 OCR 取证能避免形近字误判
    2) 全文/标题/口播里出现任一形近字（米圆/南福/丰篮/传印 等）→ 画面里可能就是
       这种错字形态，OCR 可以给"原样字形"作为对照
    """
    blob = " ".join([full_text or "", caption or "", transcript or "", ocr_text or ""])
    if not blob.strip():
        return False
    if any(b in blob for b in ["南孚", "益圆", "丰蓝", "传应"]):
        return True
    if any(w in blob for w in _BRAND_TYPO_WORDS):
        return True
    return False


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
    image_times: List[Optional[float]] = []   # 与 image_urls 对齐：关键帧对应的秒数，非帧为 None
    video_data_urls = []
    # 违规截图用的视频源：模型报出秒数后要按该时间点回抽画面
    shot_src = ""
    is_video_content = False
    for m in (media or []):
        if not isinstance(m, dict):
            continue
        if m.get("is_video"):
            is_video_content = True
            size_mb = (m.get("size") or 0) / (1024 * 1024)

            # ── 未超限：远端 URL 优先（COS 预签名，模型原生支持 HTTP video_url）──
            # 必须先判体积：超限时远端 URL 一样会被模型拒（400 Failed to
            # download），之前漏了这道判断，大视频就会整段丢失内容。
            if not video_exceeds_limit(m):
                remote = media_remote_url(m)
                if remote:
                    video_data_urls.append({"url": remote, "size_mb": size_mb})
                    shot_src = shot_src or remote
                    continue
                # 只有本机副本、还没传完 COS 时，不再把 /uploads 的 HTTP 地址交给模型：
                # 该域名走 CDN 防盗链，服务端拉取无 Referer 必然 403，模型报
                # 「Failed to download multimodal content」。直接落到下面的抽帧分支，
                # 用本地文件抽关键画面，至少拿得到真实画面而不是空跑一次。
                print(f"[ai_auditor] 视频 {size_mb:.0f}MB 暂无 COS 地址，改用本地抽帧", flush=True)

            # ── 超限（或没有可用的视频 URL）：抽多帧关键画面代替 ──
            vpath = resolve_local_path(m.get("video_local") or "")
            # 本地副本可能已被回收，退回 COS 预签名 URL 让 ffmpeg 按需抽帧
            frame_src = vpath or media_remote_url(m) or ""
            if frame_src:
                print(f"[ai_auditor] 视频 {size_mb:.0f}MB 超模型上限，抽取关键帧代替", flush=True)
                frames = extract_video_frames_with_times(frame_src)
                if frames:
                    image_urls.extend(du for du, _t in frames)
                    image_times.extend(t for _du, t in frames)
                    shot_src = shot_src or frame_src
                    # 打标记：下面选 prompt 时要知道"这里给的是静音关键帧"，
                    # 否则仍按完整视频要求模型描述口播/背景音乐，会凭空编造。
                    m["_frame_extracted"] = True

            # 兜底：连抽帧都拿不到时，至少送封面图
            if not image_urls:
                du = _local_image_data_url(m.get("thumb") or "")
                if not du and m.get("thumb_remote"):
                    du = m["thumb_remote"]
                if du:
                    image_urls.append(du)
                    image_times.append(None)
        else:
            # ── 图片：远端优先，其次本地缩略图 ──
            remote = media_remote_url(m)
            if remote:
                image_urls.append(remote)
                image_times.append(None)
                continue
            du = _local_image_data_url(m.get("thumb") or m.get("path") or "")
            if not du and m.get("thumb_remote"):
                du = m["thumb_remote"]
            if du:
                image_urls.append(du)
                image_times.append(None)
    image_urls = image_urls[:_MAX_IMAGES_STRATEGY]
    image_times = image_times[:_MAX_IMAGES_STRATEGY]
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

    # ── 同步 OCR 取证：先把画面上的字"原样读出来"，再交给主审核
    # 形近字/易误判类违规（品牌名错字、同音错字）按"AI 看错字形"的高发场景处理：
    # AI 自己看图时容易把"益圆"看成"米圆"、把"南孚"看成"南福"，但只要 OCR
    # 已经把画面里的字原样读出来，AI 拿到对照后能极大降低误报率。
    # 所有视频关键帧和图片都强制做一次逐帧 OCR，不能因为标题没有品牌词就跳过。
    # 产品包装、字幕中的错别字可能只出现在画面里，不能依赖文案触发。
    pre_ocr_text = ""
    pre_ocr_raw: list = []
    if image_urls:
        pre = ocr_brand_text(image_urls)
        if not pre.get("error"):
            pre_ocr_raw = pre.get("ocr_raw") or []
            pre_ocr_text = pre.get("ocr_text_all") or ""

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
        f"{_format_brand_refs_for_prompt()}\n"
        "【审核核心原则 —— 必须区分「品牌创意内容」与「违规营销广告」】\n"
        "品牌常会制作动画/短剧/故事/IP系列（如武侠剧情、二次元角色、品牌吉祥物动画等）"
        "来传播品牌文化，这类内容属于品牌创意表达，不属于违规营销。审核时务必区分：\n"
        "- 创意/IP内容中的品牌元素（Logo/名称/徽章等）属于正常品牌露出，不能仅因「出现品牌元素」就判违规；"
        "但画面里的品牌名若【字形本身写错了】（如把「益圆」画成「米圆」），仍属「品牌名错误」，必须命中\n"
        "- 创意剧情中的冲突（打假/反派等）是剧情需要，不能判为「品牌负面」「品质下降暗示」「安全负面联想」等\n"
        "- 创意内容中的视觉特效（闪电/光影等）是画面风格，不能判为「危险场景」「电池爆炸联想」等\n"
        "- 创意内容不是产品广告：画面中的品牌元素不代表产品功能宣称，「聚能环」徽章不等于夸大产品\n"
        "- 品牌IP动画中角色与品牌同名是刻意设定（如「南孚宗少主雷雨砚」），不能判为「品牌名错误」或「混淆定位」\n"
        "- 【品牌产品视觉一致性】有标准品牌参考原图时，必须把视频/图片中的产品与对应参考图逐项比较：产品颜色、形状、包装文字、Logo位置、型号/品类、包装布局、关键标签和产品用途。\n"
        "  品牌名字写对不等于产品视觉正确；如果产品包装、外观、型号或标签与标准图明显不同，必须命中「品牌产品视觉一致性」，severity 用 high，evidence 写明具体差异并建议人工复核。\n"
        "  只看到品牌角色、抽象图形或无法确认是否为实物产品时，不得武断判定不一致；画面模糊或参考角度不同但无法确认时，写入 evidence「视觉差异无法确认，建议人工复核」，不要直接判 forbidden。\n"
        "- 同一条内容「联合推广」南孚集团旗下多个品牌（如南孚+益圆、或四大品牌全家福）是正常的集团多品牌营销，"
        "不能判为「品牌间-内部对比」「品牌间-混淆定位」或「竞品」违规；只要没有互相比较高低优劣、没有用南孚泛指所有产品即可\n"
"- 唯一例外：如果创意内容是AI生成的且确实未标注「AI生成」，「AI-未标注标识」规则可以命中（这是格式要求，不否定创意本身）\n"
"- 【AI标注判断】画面中任何位置出现的「AI生成」标识（左上角、右下角、画面中央、视频开头或结尾）都算「已标注」，不判定违规。\n"
"- 只有画面中完全找不到「AI生成」文字标识时才可命中「AI-未标注标识」。\n"
"\n"
"【品牌名识别 —— 逐字核对，禁止用常识自动纠正，禁止看错字形】\n"
"标准品牌名只有这几种唯一正确写法：南孚、益圆、丰蓝、传应。\n"
"对画面/字幕/包装/旗帜上出现的每个品牌名，必须按下面三步核对：\n"
"1) 先把你【实际看到的字形】原样写出来——看到「米圆」就写「米圆」，"
"绝对不要因为「应该写益圆」就自动改写成「益圆」；\n"
"2) 再和该品牌的标准写法逐字对照；\n"
"3) 只要字形不一致（形近字、同音字、缺字多字、镜像或变形），就命中「品牌名称错误」，"
"evidence 写成「画面中实际显示为『米圆』，标准写法应为『益圆』」。\n"
"常见错误示例：益圆→米圆/米园/益园/益元/盆圆；南孚→南福/南符/南扶/南浮；丰蓝→丰篮；传应→传印。\n"
"只有在画面模糊到根本认不出字形时才跳过；只要能看清字形，就必须逐字核对并如实报告。\n"
"特别注意：AI 生成的动画/艺术字很容易把品牌名写成形近的错字，这类内容必须重点核对品牌名。\n"
"\n"
"【画面看错字形的高发陷阱（务必避免）】\n"
"AI 容易把画面里的「益圆」错看成形近字「米圆」「米园」「益园」「益元」「盆圆」「盗圆」「易圆」。\n"
"它们的字形差异：\n"
"  - 益圆 = 氵(三点水) + 益(下面皿字底) + 圆\n"
"  - 米圆 = 米 + 圆（氵和皿字底一旦看错/看反/看成米字，就成了米圆）\n"
"  - 益园 = 氵 + 益(下面口字) + 园\n"
"  - 益元 = 氵 + 益(下面口字) + 元\n"
"看图时必须先确认氵(三点水)和益字下面的皿字底/口字底确实存在；\n"
"如看不清或笔画模糊，【不能武断判错字】，应在 conclusion 说明「画面模糊未能确认」，由人工复核。\n"
"同样的陷阱：南孚(孚字上面是爫/子)+南福(示字旁)+南符(竹字头)+南浮(氵)；"
"丰蓝(蓝字上面是监字)+丰篮(竹字头)；传应(应字)+传印(印字)。\n"
"【强约束】当文案/标题/口播里已经出现正确的标准品牌名（如「益圆」「南孚」），\n"
"而画面被你看成是它的形近字（如「米圆」「南福」），【优先相信文案】，不要轻易判品牌名错字。\n"
"\n"
"【预 OCR 取证 —— 形近字错字类违规的铁律】\n"
"系统已经在主审核前先调了一次多模态模型做同步 OCR，\n"
"结果以「【⚠ 预 OCR 取证结果】」段加在内容的最上方给你看。\n"
"【对形近字错字（米圆/南福/丰篮/传印 等）类违规】：\n"
"1) 你必须**逐字对比**预 OCR 结果与你【直接看图】的结果；\n"
"2) 若预 OCR 结果与画面一致，且预 OCR 里出现的是 Y（标准品牌名：南孚/益圆/丰蓝/传应），\n"
"   而你凭印象觉得是 X（形近字）→ 以预 OCR 为准，【不判定违规】；\n"
"3) 若预 OCR 结果里**同时**出现 Y 和 X，且你看到画面里 X 确实存在 → 仍判定违规；\n"
"4) 若预 OCR 完全识别不到（画面模糊、字体艺术化）→ 在 evidence 注明「画面/OCR 模糊，\n"
"   建议人工复核」，由人在复批页处理。\n"
"这一改动的目的是大幅降低「AI 看错字形」导致的误报——之前几乎每张有\n"
"「益圆」「南孚」的画面都会触发一次错误的「品牌名错误」违规，让后台看不完。\n"
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
"【违规时间点 timestamp —— 用于自动截取违规画面】\n"
"每条违规都要给 timestamp，写明【违规画面出现在视频的第几秒】（纯数字秒数，如 12.5）。\n"
"- 如果上方给的是关键帧截图，每帧标注了「本帧取自视频第 X 秒处」，请据此填写该帧的秒数；\n"
"- 如果给的是完整视频，按你看到的画面时间轴填写；\n"
"- 违规只出现在文案/标题里、画面中看不到，或确实无法判断时间时，timestamp 填 0。\n"
"系统会用这个秒数回原视频截取该帧画面，作为人工复核的违规证据，请尽量填准确。\n"
"\n"
        "必须仅输出 JSON，不要任何额外文字，格式：\n"
        '{"interpretation":"' + interp_req + '",'
        '"violations":[{"rule_name":"","category":"","severity":"forbidden|high|medium|low",'
        '"field":"画面|文案|口播|字幕","timestamp":12.5,'
        '"evidence":"该内容【确实违反】此规则的具体依据(哪句文案/哪段口播/画面里看到什么)"}],'
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
    # ── 把预 OCR 文字插入到 user_text 顶部，让审核员（AI）第一时间拿到对照 ──
    if pre_ocr_text:
        user_text = (
            f"【⚠ 预 OCR 取证结果】（多模态模型刚刚逐张读出的画面文字——不要自动纠错，"
            f"看到「米圆」就照写「米圆」）\n{pre_ocr_text}\n\n"
            + user_text
        )
    user_parts: list = [
        {"type": "text", "text": user_text}
    ]
    # 品牌参考原图：文字描述只能辅助，必须把已学习的标准产品图一并给模型逐字比对。
    brand_ref_images = _get_brand_reference_images()
    if brand_ref_images:
        # 构建强制品牌视觉比对指令：模型必须对视频中出现的每个已学习品牌逐项输出比对结果
        brand_names = [r["brand"] for r in brand_ref_images]
        brand_check_instruction = (
            "【⚠ 强制品牌产品视觉一致性比对 —— 必须执行，不可跳过】\n"
            "你已收到以下品牌的标准参考原图：{}。\n"
            "请仔细观看视频/图片中出现的所有产品画面。对于视频中出现了上述任意品牌的产品（电池实物、包装、产品外观），\n"
            "你【必须】在 violations 数组中为每个出现的品牌输出一条「品牌产品视觉一致性」比对结果，格式如下：\n"
            '  rule_name: "品牌产品视觉一致性", category: "红线-品牌形象", severity: "medium",\n'
            '  field: "画面", timestamp: <产品出现时的秒数>,\n'
            '  evidence: 逐项列出比对结果（颜色/形状/包装文字/Logo位置/型号/包装布局），写明"一致"或"差异：<具体差异>"\n\n'
            "判定标准：\n"
            "- 这条规则只做提示：severity 一律用 medium（由人工复核决定是否算违规），不要用 high/forbidden\n"
            '- 产品包装/外观/型号与标准图【明显不同】→ severity 用 medium，evidence 写明具体差异\n'
            '- 产品与标准图【基本一致】→ 不输出此条\n'
            '- 画面模糊或角度不同【无法确认】→ 不输出此条\n'
            "- 【重要】只有【实物产品本身】（电池实物、实物包装盒、实物标签）才做这个比对。\n"
            "  吉祥物、拟人化角色、卡通/动画形象、IP 形象、插画、手绘、抽象图形都【不是实物产品】，\n"
            "  即使它们以电池为原型做了夸张变形，也【绝对不要】输出这条规则。\n"
            "- 如果视频中某个品牌只出现了动画/IP 形象、没有出现实物产品，就不为该品牌输出此条\n"
            "- 如果视频中出现了一个品牌但不在上述已学习品牌列表中，跳过该品牌的视觉一致性比对\n"
            "【注意】对于出现了实物产品的品牌，必须逐项比对并输出结果，不能因为你觉得『看起来差不多』就跳过。\n"
        ).format("、".join(brand_names))
        user_parts.append({"type": "text", "text": brand_check_instruction})
        user_parts.append({"type": "text", "text": "【标准品牌参考原图】以下图片来自后台品牌学习库，只用于核对标准字形和产品外观，不属于待审核内容。请先仔细记住每张参考图对应品牌的产品外观（颜色、形状、包装文字、Logo位置、型号），再分析下面的视频画面。"})
        for ref in brand_ref_images:
            user_parts.append({"type": "text", "text": f"[标准品牌参考图：{ref['brand']}] 请记住此品牌的标准产品外观"})
            user_parts.append({"type": "image_url", "image_url": {"url": ref["url"]}})
    # 视频：直接发送完整视频文件
    for idx, v in enumerate(video_data_urls):
        user_parts.append({"type": "text",
                           "text": f"[完整视频 {idx+1}/{len(video_data_urls)}] 请从头到尾分析此视频的所有画面、声音和字幕"})
        user_parts.append({"type": "video_url", "video_url": {"url": v["url"]}})
    # 图片：编号标注（关键帧额外带上秒数，模型才能报出违规时间点）
    for idx, du in enumerate(image_urls):
        t = image_times[idx] if idx < len(image_times) else None
        if t is not None:
            label = f"[{media_label} 图{idx+1}/{len(image_urls)}]（本帧取自视频第 {t:.1f} 秒处）"
        else:
            label = f"[{media_label} 图{idx+1}/{len(image_urls)}]"
        if len(image_urls) == 1 and t is None:
            label = f"[{media_label}]"
        user_parts.append({"type": "text", "text": label})
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
                                 or "video" in err_msg.lower() or "invalid" in err_msg.lower()
                                 or "download" in err_msg.lower()):
            print(f"[ai_auditor] 视频发送失败({err_msg[:80]})，改用抽取关键帧重试", flush=True)
            # 重建消息：去掉视频 URL，改用多帧关键画面。
            # 只用一张封面图信息量太少（实测会漏掉画面里的品牌名错字等），
            # 此时本地副本通常还在，抽帧又快又可靠；抽不到再退回封面图。
            fallback_parts = [{"type": "text", "text": user_text}]
            if brand_ref_images:
                fallback_parts.append({"type": "text", "text": brand_check_instruction})
                fallback_parts.append({"type": "text", "text": "【标准品牌参考原图】以下是后台品牌学习库中的标准图，只用于核对品牌字形和产品外观，不属于待审核内容。请先仔细记住每张参考图对应品牌的产品外观。"})
                for ref in brand_ref_images:
                    fallback_parts.append({"type": "text", "text": f"[标准品牌参考图：{ref['brand']}] 请记住此品牌的标准产品外观"})
                    fallback_parts.append({"type": "image_url", "image_url": {"url": ref["url"]}})
            for m in (media or []):
                if not isinstance(m, dict):
                    continue
                if m.get("is_video"):
                    frame_src = (resolve_local_path(m.get("video_local") or m.get("path") or "")
                                 or media_remote_url(m) or "")
                    frames = extract_video_frames_with_times(frame_src, n=6) if frame_src else []
                    if frames:
                        shot_src = shot_src or frame_src
                        for _i, (f, _t) in enumerate(frames):
                            fallback_parts.append(
                                {"type": "text",
                                 "text": f"[视频关键帧 图{_i+1}/{len(frames)}]（本帧取自视频第 {_t:.1f} 秒处）"})
                            fallback_parts.append(
                                {"type": "image_url", "image_url": {"url": f}})
                        m["_frame_extracted"] = True
                        continue
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
                {"role": "system", "content": sys_prompt.replace(
                    "一条完整视频（你可以逐帧/逐秒分析视频画面+声音+字幕）",
                    "视频的关键帧截图（从原视频中均匀抽取的代表性画面，无法提供声音）")},
                {"role": "user", "content": fallback_parts},
            ]
            try:
                raw = _call_model(cfg, fallback_messages)
            except Exception as e2:
                return _empty_review(cfg["model"], error=f"模型调用失败（关键帧重试也失败）：{e2}")
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
        # 第4道：预 OCR 已确认是标准品牌名时，对画面写错形近字的违规再卡一道
        # （用户反馈：每次都识别错误。需要把 OCR 的字面结果作为对照）
        if pre_ocr_text and _is_brand_typo_overridden_by_ocr(
            str(v.get("rule_name") or ""), evidence, pre_ocr_text,
        ):
            continue
        # 第4道：品牌名错误幻觉过滤——AI 把正确品牌名误判为错字时剔除
        # （证据来自画面时不校验文案，错字可能只画在画面里）
        rule_name = str(v.get("rule_name") or "")
        # 品牌产品视觉一致性规则做保护：它的 evidence 可能含"一致/不命中"等词
        # 但不应该被下面的否定句过滤误杀
        is_brand_visual = "视觉一致性" in rule_name
        # 第2道：依据本身明确说“不违规”（不涉及/无XX/结尾不违规等否定句），剔除
        if not is_brand_visual and _is_noviolation_evidence(evidence):
            continue
        # 第3道：模型把“已标注/不命中/不判定违规”写进 evidence 时，不能当成违规。
        # 这类自相矛盾文本曾导致结论写“合规”，但状态仍被规则引擎算成中风险。
        no_hit_phrases = ("不违规", "不判定违规", "不命中", "不应命中", "不构成违规", "已标注")
        if not is_brand_visual and any(phrase in evidence for phrase in no_hit_phrases):
            continue
        if not is_brand_visual and "合规" in evidence and ("未" in evidence or "无" in evidence or "没有" in evidence):
            continue
        if _is_brand_typo_hallucination(rule_name, full_text, str(v.get("field") or ""), evidence):
            continue
        # 第5道：错别字自相矛盾过滤——"X误写为X"等于没写错，属幻觉
        if _is_typo_self_contradiction(evidence):
            continue
        # 第6道：错别字规则里 AI 自称"并非错别字"的措辞建议，剔除
        if _is_not_a_typo_judgement(rule_name, evidence):
            continue
        # 第7道：错别字规则里"正确写法=实际显示"的矛盾判定，剔除
        if _is_typo_same_word_claim(rule_name, evidence):
            continue
        sev = str(v.get("severity") or "medium").lower()
        if sev not in valid_sev:
            sev = "medium"
        # 品牌产品视觉一致性：AI 无法可靠判断"明显不同"，一律只做提示（medium），
        # 由人工复核决定。之前按模型给的 high 直接驳回，实测 77 条命中里 66 条
        # 模型自己写着"需人工复核"或是吉祥物/动画形象（根本不是实物产品）。
        if is_brand_visual:
            if any(w in evidence for w in _BRAND_VISUAL_NON_REAL):
                continue        # 吉祥物/拟人化角色/动画形象不是实物产品，本规则不适用
            sev = "medium"
        # 模型自己说「需人工复核/无法确认/需进一步确认」时不能直接驳回——
        # 结论写"建议人工复核"而状态是红灯，逻辑上自相矛盾。降为 medium 进人工复核。
        # 例外：涉政/色情/暴恐/违法这类硬红线，措辞再含糊也不降级。
        elif sev in ("high", "forbidden"):
            if _AI_UNCERTAIN_RE.search(evidence) and not _HARD_REDLINE_RE.search(evidence):
                sev = "medium"
        ts = _parse_timestamp(v.get("timestamp"))
        item = {
            "rule_id": None,
            "rule_name": str(v.get("rule_name") or "AI判定"),
            "category": str(v.get("category") or "AI审核"),
            "severity": sev,
            "matched_text": evidence or "（模型未提供依据）",
            "field": f"{v.get('field') or '画面'}（AI）",
            "source": "AI模型",
        }
        # 形近错字类违规打标：复批端/二次确认会用到
        item["needs_ocr_review"] = _is_typo_like_violation(
            item["rule_name"], full_text, str(v.get("field") or ""), evidence,
        )
        # timestamp <= 0 表示违规只出现在文案里（画面里看不到），不截图
        if ts and ts > 0:
            item["evidence_time"] = ts
        # 品牌产品视觉一致性：模型判定"基本一致"时不计入违规
        # 注意：必须排除"不一致"的情况——"不一致"包含"一致"子串但不能被匹配
        if is_brand_visual:
            # 先排除"不一致"，再检查"一致"
            if "不一致" in evidence:
                pass  # 有差异，保留违规
            else:
                consistent_phrases = ("基本一致", "完全一致", "产品外观与标准参考图基本一致")
                if any(p in evidence for p in consistent_phrases):
                    continue
        out.append(item)

    # 程序化兜底：OCR 已读到明确的 AI 生成标识时，不能再保留“AI-未标注标识”。
    # 模型可能会自行解释“位置不够显著”，但当前业务规则规定画面任意位置出现即算已标注。
    ai_markers = ("AI生成", "AI 生成", "AI创作", "AI 创作", "豆包AI", "豆包 AI")
    has_ai_marker = any(marker in pre_ocr_text for marker in ai_markers)
    if has_ai_marker:
        out = [v for v in out if "未标注" not in str(v.get("rule_name") or "")]

    # 违规证据截图：把模型报出的违规画面截下来，存 COS 供人工复核
    try:
        _attach_evidence_images(out, shot_src)
    except Exception as e:
        print(f"[ai_auditor] 违规截图生成失败: {e}", flush=True)

    # 形近错字类违规（品牌名写错等）在自动驳回前，先自动做一次 OCR 取证复判：
    # 必须放在截图之后——复核用的正是刚截下来的那一帧；之前把视频 URL 当图片
    # 传给模型，报 image format is illegal / 超过 64MB，复核从未成功过。
    try:
        out = _auto_recheck_typos(out, image_urls, full_text, shot_src,
                                  content.get("media") or [])
    except Exception as e:
        print(f"[ai_auditor] 形近错字自动复核失败: {e}", flush=True)

    conclusion = str(data.get("conclusion") or "").strip()
    if has_ai_marker and not out:
        conclusion = "合规：画面中已识别到 AI 生成标识，不命中 AI-未标注标识。"

    return {
        "model": cfg["model"],
        "interpretation": str(data.get("interpretation") or "").strip(),
        "conclusion": conclusion,
        "violations": out,
        "image_count": len(image_urls),
        "video_count": len(video_data_urls),
        "text_length": len(full_text),
        "has_caption": bool(caption.strip()),
        "has_transcript": bool(transcript.strip()),
        "has_ocr": bool(ocr_text.strip()),
        "is_video": is_video_content,
        "strategy": content_type,
        # 同步 OCR 取证结果：有画面 + 触发条件命中时才会有值
        "pre_ocr_text": pre_ocr_text,
        "pre_ocr_raw": pre_ocr_raw,
        "pre_ocr_done": bool(pre_ocr_text or pre_ocr_raw),
        # 画面文字 / 语音：审核链路早就拿到了，之前算完就丢，后台一直看不到这两项
        "ocr_text": (pre_ocr_text or ocr_text or "").strip(),
        "transcript": (transcript or "").strip(),
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
