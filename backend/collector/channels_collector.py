"""微信视频号助手后台采集器（Playwright 浏览器自动化）。

适用场景：采集**你自己运营、后台扫码能登录看到**的视频号内容。
合法合规、稳定、几乎无封号风险（访问的是自己后台的自己数据）。

后台：https://channels.weixin.qq.com/  （登录后进 /platform）
可采集到的文本：视频标题、描述文案、话题标签(#xxx)、发布时间等。
（口播语音后台无现成文本，如需审口播需另做视频转写。）

三种运行模式（在 backend 目录下执行）：
  1) 扫码登录并保存会话（仅首次，之后免扫）：
       ./.venv/bin/python -m collector.channels_collector login
  2) 导出后台真实页面结构（截图+HTML+捕获的接口JSON），用于精调选择器：
       ./.venv/bin/python -m collector.channels_collector dump
  3) 实际采集并打印结果：
       ./.venv/bin/python -m collector.channels_collector collect

设计说明：
  - 登录态保存在 channels_session.json，后端无头调用时复用，无需重复扫码。
  - 优先策略：监听后台加载视频列表时返回的 XHR JSON 接口，解析其中的
    标题/描述/话题字段（比解析混淆的 DOM class 更鲁棒）。
  - JSON 字段名因后台版本而异，dump 模式会把捕获到的接口原文保存下来，
    便于按真实返回精确映射字段（见 _extract_posts 中的解析逻辑）。
"""
import os
import re
import sys
import json
import time
import hashlib
import shutil
import subprocess
import urllib.request
import threading
from typing import List, Dict, Any, Optional

_HERE = os.path.dirname(__file__)
_BACKEND_DIR = os.path.dirname(_HERE)
SESSION_PATH = os.path.join(_BACKEND_DIR, "channels_session.json")  # 遗留单一会话（向后兼容）
SESSIONS_DIR = os.path.join(_BACKEND_DIR, "sessions")               # 多视频号：每号独立会话

# ── 浏览器实例池（预热 + 复用）──
_playwright_instance = None
_browser_instance = None
_browser_lock = threading.Lock()

def warmup_browser():
    """预热浏览器：服务启动时调用，提前初始化 Playwright 和浏览器实例。
    首次授权时可直接复用，省去 3-5 秒冷启动时间。"""
    global _playwright_instance, _browser_instance
    with _browser_lock:
        if _browser_instance is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
            _playwright_instance = sync_playwright().start()
            _browser_instance = _playwright_instance.chromium.launch(headless=True)
            print("[browser] 预热完成，浏览器实例已就绪")
        except Exception as e:
            print(f"[browser] 预热失败: {e}")
            _browser_instance = None
            _playwright_instance = None

def get_browser():
    """获取浏览器实例（预热过的或新启动的）。"""
    global _browser_instance
    with _browser_lock:
        if _browser_instance is not None:
            try:
                # 测试浏览器是否还活着
                _browser_instance.new_context().close()
                return _browser_instance
            except Exception:
                # 浏览器已死，重新启动
                _browser_instance = None
        # 重新启动
        from playwright.sync_api import sync_playwright
        global _playwright_instance
        if _playwright_instance is None:
            _playwright_instance = sync_playwright().start()
        _browser_instance = _playwright_instance.chromium.launch(headless=True)
        return _browser_instance


def session_path_for(channel_id) -> str:
    """返回某视频号专属的登录态文件路径（按 channel_id 隔离不同微信账号）。"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    return os.path.join(SESSIONS_DIR, f"channel_{channel_id}.json")


def _load_cookies(session_path: str):
    """从 Playwright storage_state JSON 中提取 cookies 列表。供视频代理接口使用。"""
    if not session_path or not os.path.exists(session_path):
        return None
    try:
        with open(session_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get("cookies", [])
    except Exception:
        return None


def _resolve_channel_id(channel_name: str):
    """按视频号名查数据库取 id（用于定位该号的会话文件）。找不到返回 None。"""
    try:
        import database
        with database.db_cursor() as cur:
            cur.execute("SELECT id FROM channels WHERE name=?", (channel_name,))
            row = cur.fetchone()
            return row["id"] if row else None
    except Exception:
        return None
DUMP_DIR = os.path.join(_BACKEND_DIR, "channels_dump")
MEDIA_DIR = os.path.join(_BACKEND_DIR, "media")  # 下载的作品缩略图（合规留证，永久保存）
QRCODE_PATH = os.environ.get(
    "JIANKONG_QR", os.path.join(_BACKEND_DIR, "login_qrcode.png")
)


def _download_thumb(url: str) -> str:
    """下载作品缩略图到本地 media 目录，返回可静态访问的相对路径 /media/<md5>.jpg。

    采集时即下载落地，避免视频号媒体链接 token 过期后无法展示（合规留证）。
    不带 Referer（实测带 127.0.0.1 referer 可能被防盗链拒绝）。失败返回空串。
    """
    if not url:
        return ""
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        name = hashlib.md5(url.encode("utf-8")).hexdigest() + ".jpg"
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.exists(path):
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            # 校验确为图片（JPEG/PNG 文件头），防止把错误页存成图
            if not (data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n"):
                return ""
            with open(path, "wb") as f:
                f.write(data)
        return "/media/" + name
    except Exception:
        return ""


_FFMPEG = shutil.which("ffmpeg")
_HTTP_UA = "Mozilla/5.0"


def _download_video(url: str, play_len: int = 0, cookies: Optional[List[Dict[str, Any]]] = None) -> str:
    """下载作品的完整视频到本地 media 目录，返回 /media/<md5>.mp4。

    优先用 Python requests + 会话 cookie 下载（腾讯 finder CDN 需要登录态）；
    无 cookie 时回退到 ffmpeg 拉流。失败返回空串。
    """
    if not url:
        return ""
    try:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        name = hashlib.md5(url.encode("utf-8")).hexdigest() + ".mp4"
        path = os.path.join(MEDIA_DIR, name)
        if os.path.exists(path) and os.path.getsize(path) >= 1024:
            return "/media/" + name

        # 方案 A：requests + cookie（有登录态，最可靠）
        if cookies:
            import urllib3
            urllib3.disable_warnings()
            try:
                sess = __import__("requests").Session()
                for ck in cookies:
                    sess.cookies.set(
                        ck.get("name", ""), ck.get("value", ""),
                        domain=ck.get("domain"), path=ck.get("path"),
                    )
                resp = sess.get(url, headers={"User-Agent": _HTTP_UA}, timeout=90, stream=True, verify=False)
                if resp.status_code == 200:
                    data = resp.content
                    if data and (data[:3] == b"\x00\x00\x00" or data[:4] == b"\x1aE\xdf\xa3" or data[:4] == b"ftyp" or data[:4] == b"\x00\x00\x00\x18ftyp"):
                        # 确认为 MP4/WebM 视频
                        with open(path, "wb") as f:
                            f.write(data)
                        # 压缩大视频（>2MB），减少带宽占用
                        _compress_video(path)
                        return "/media/" + name
            except Exception:
                pass

        # 方案 B：ffmpeg 回退
        if _FFMPEG:
            cmd = [_FFMPEG, "-y", "-user_agent", _HTTP_UA, "-i", url,
                   "-c", "copy", "-movflags", "+faststart", path]
            subprocess.run(cmd, timeout=150, capture_output=True)
            _compress_video(path)
        if os.path.exists(path) and os.path.getsize(path) >= 1024:
            return "/media/" + name
        return ""
    except Exception:
        return ""


def _compress_video(path: str) -> None:
    """压缩大视频文件（>1MB），转为 480p 低码率 mp4，减少带宽占用。

    服务器带宽有限，原始视频动辄 3-10MB，压缩后通常 100KB-500KB，播放加载快 10-20 倍。
    """
    if not _FFMPEG or not os.path.exists(path):
        return
    try:
        fsize_mb = os.path.getsize(path) / (1024 * 1024)
        if fsize_mb <= 1:
            return  # 1MB 以下不压缩
        tmp = path + ".compressed.mp4"
        cmd = [_FFMPEG, "-y", "-i", path,
               "-vf", "scale=-2:480",
               "-c:v", "libx264", "-preset", "fast", "-crf", "35",
               "-c:a", "aac", "-b:a", "32k", "-ar", "22050",
               "-movflags", "+faststart",
               tmp]
        result = subprocess.run(cmd, timeout=120, capture_output=True)
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) >= 1024:
            new_mb = os.path.getsize(tmp) / (1024 * 1024)
            if new_mb < fsize_mb * 0.9:  # 压缩后至少小 10% 才替换
                os.replace(tmp, path)
                print(f"[compress] {os.path.basename(path)}: {fsize_mb:.1f}MB → {new_mb:.1f}MB", flush=True)
            else:
                os.remove(tmp)
        elif os.path.exists(tmp):
            os.remove(tmp)
    except Exception as e:
        print(f"[compress] 压缩失败: {e}", flush=True)


def _extract_frames(video_rel: str, play_len: int = 0, n: int = 6) -> List[str]:
    """从已下载到本地的视频里均匀抽取 n 帧（按时间顺序），返回 /media/xxx_fK.jpg 列表。

    供大模型「看」整个视频的演进来解读，而不只是首帧封面。
    """
    if not _FFMPEG or not video_rel:
        return []
    name = os.path.basename(video_rel)
    vpath = os.path.join(MEDIA_DIR, name)
    if not os.path.exists(vpath):
        return []
    base = os.path.splitext(name)[0]
    try:
        dur = float(play_len)
    except Exception:
        dur = 0.0
    if dur <= 0:
        dur = 10.0
    frames: List[str] = []
    for i in range(n):
        t = dur * (i + 0.5) / n  # 均匀采样：每段中点取一帧
        fname = f"{base}_f{i}.jpg"
        fpath = os.path.join(MEDIA_DIR, fname)
        if not os.path.exists(fpath):
            try:
                subprocess.run(
                    [_FFMPEG, "-y", "-ss", f"{t:.2f}", "-i", vpath,
                     "-frames:v", "1", "-q:v", "3", fpath],
                    timeout=30, capture_output=True,
                )
            except Exception:
                continue
        if os.path.exists(fpath):
            frames.append("/media/" + fname)
    return frames

# 规避 IDE 沙箱对系统目录的限制 + 无头稳定运行所需参数
_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-crash-reporter",
    "--disable-component-update",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

HOME_URL = "https://channels.weixin.qq.com/"
PLATFORM_URL = "https://channels.weixin.qq.com/platform"
# 内容管理（作品列表）页，不同版本路径可能不同，登录后会自动尝试导航
POST_LIST_URLS = [
    # 真实图文列表页路由（用户从已登录后台确认）——首选
    "https://channels.weixin.qq.com/platform/post/finderNewLifePostList",
    "https://channels.weixin.qq.com/platform/post/list",
    "https://channels.weixin.qq.com/platform/post/postList",
]

# 后台真实接口前缀（命中即尝试解析为 JSON，不依赖 content-type）
API_PREFIX = "mmfinderassistant-bin"


# ---------------- 登录 ----------------
def _fetch_qr_via_page(page) -> Optional[str]:
    """当 QR 码是相对 URL（而非 data:image base64）时，通过页面 fetch 下载并转 base64。
    典型场景：微信开放平台 iframe 中的 /connect/qrcode/... 图片。"""
    import base64 as _b64
    try:
        # 在所有 frame 中找 class 含 qrcode 的 img
        for fr in page.frames:
            try:
                el = fr.query_selector("img.qrcode, img.web_qrcode_img, img[class*='qr']:not([class*='avatar']):not([class*='quick'])")
                if not el:
                    continue
                src = (el.get_attribute("src") or "").strip()
                if not src or src.startswith("data:"):
                    continue
                # 相对 URL → 用 Playwright 的 fetch API 下载
                data = fr.evaluate("""async (url) => {
                    const r = await fetch(url);
                    if (!r.ok) return null;
                    const blob = await r.blob();
                    return new Promise((resolve) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                }""", src)
                if isinstance(data, str) and data.startswith("data:image") and len(data) > 500:
                    return data
            except Exception:
                pass
    except Exception:
        pass
    return None


def _find_qr_src(page) -> Optional[str]:
    """从登录页各 frame 中提取二维码图片的 data:image base64 源。
    微信视频号助手的登录二维码可能在 iframe 中，尝试多种选择器。"""
    selectors = [
        "img.qrcode",
        "img[src^='data:image']",
        ".qrcode img",
        ".login_qrcode img",
        ".wr_code_img",
        "img[class*='qr']",
        "img[class*='code']",
        "img[class*='Qr']",
        "img[class*='Code']",
        ".qrcode-img",
        ".mp_qrcode",
        "img",
    ]
    for fr in page.frames:
        try:
            for sel in selectors:
                els = fr.query_selector_all(sel)
                for el in els:
                    s = (el.get_attribute("src") or "").strip()
                    if s.startswith("data:image") and len(s) > 500:
                        return s
        except Exception:
            pass
    return None


def _login_cookies_present(context) -> bool:
    """登录成功后 context 会出现会话票据 cookie；未扫码时不会有，绝不误判。"""
    try:
        for c in context.cookies():
            name = (c.get("name") or "").lower()
            if c.get("value") and any(
                k in name for k in ("sessionid", "wxuin", "data_ticket", "media_ticket", "uin")
            ):
                return True
    except Exception:
        pass
    return False


def _logged_in(page) -> bool:
    """检测是否已登录：URL 必须明确进入工作台（/platform 或 /home）。
    绝不把首页（channels.weixin.qq.com/）当作已登录。"""
    url = page.url
    if "/platform" in url or "/home" in url:
        return True
    return False


def _is_authenticated(page, context) -> bool:
    """双重检测：URL 已进入工作台 + 有足够 cookie，或已有微信会话 cookie。
    旧 cookie 重放时，goto 后 URL 可能在 JS 跳转完成前仍是首页；此时若有真实登录
    cookie（sessionid/wxuin 等），也认为已登录，避免进入"扫码等 cookie 变化"的死循环。"""
    # 满足登录态 cookie 即可认定已登录（覆盖 JS 跳转未完成的过渡态）
    if _login_cookies_present(context):
        return True
    # URL 已到工作台 + 有一定量 cookie
    if _logged_in(page):
        try:
            if len(context.cookies()) >= 2:
                return True
        except Exception:
            pass
    return False


def _dump_cookies(context, label: str = ""):
    """调试用：打印当前所有 cookies 的名称与值长度。"""
    try:
        cookies = context.cookies()
        names = [(c.get("name", "?"), len(c.get("value") or "")) for c in cookies]
        print(f"[auth] {label} cookies ({len(names)}): {names}", flush=True)
    except Exception:
        pass


def _detect_auth_channel_list(page) -> list:
    """检测登录后的频道选择页面，返回可选视频号名称列表。

    微信视频号助手在扫码登录后，如果该微信账号管理多个视频号，
    会显示一个「选择要管理的视频号」页面。
    先用多种 CSS 选择器尝试，失败后通过 JS 遍历所有可见元素查找疑似频道名。
    """
    import sys as _sys
    names = []

    def _log(msg: str):
        print(f"[detect_channels] {msg}", flush=True, file=_sys.stderr)

    _log(f"当前 URL: {page.url}")

    # === 策略1：已知的卡片/列表 CSS 选择器 ===
    card_selectors = [
        # 微信视频号助手 precise selectors (from actual HTML)
        ".choose-finder-area .finder-item .name span",
        ".choose-finder-area .finder-item .name",
        ".finder-item .name span",
        ".finder-item .name",
        ".finder-list .finder-item .name",
        # 通用账号卡片选择器
        ".finder-account-card .name",
        ".account-card .account-name",
        ".channel-card .channel-name",
        ".account-item .nickname",
        ".finder-item .finder-name",
        "[class*='account-card'] [class*='name']",
        "[class*='channel-card'] [class*='name']",
        "[class*='finder-card'] [class*='name']",
        "[class*='finder-item'] [class*='name']",
    ]
    for sel in card_selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                if el.is_visible():
                    txt = (el.inner_text() or "").strip()
                    # 频道名：2-20字符，至少含一个中文
                    if txt and 2 <= len(txt) <= 20 and txt not in names:
                        has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in txt)
                        if has_cn:
                            names.append(txt)
            if len(names) >= 2:
                _log(f"CSS 选择器命中 ({sel}): {names}")
                return names
        except Exception:
            continue
    # 只有一个也返回
    if names:
        _log(f"CSS 选择器命中: {names}")
        return names

    # 模式3：radio/checkbox 式选择（label 中包含频道名）
    try:
        labels = page.query_selector_all("label")
        for label in labels:
            if label.is_visible():
                inp = label.query_selector("input[type='radio'], input[type='checkbox']")
                if inp:
                    txt = (label.inner_text() or "").strip()
                    if txt and 2 <= len(txt) <= 20 and txt not in names:
                        has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in txt)
                        if has_cn:
                            names.append(txt)
        if names:
            _log(f"radio/checkbox 命中: {names}")
            return names
    except Exception:
        pass

    # 模式4：弹窗/对话框内的列表（微信 UI 常用 weui-desktop-dialog）
    modal_selectors = [
        ".weui-desktop-dialog .finder-item .name",
        ".weui-desktop-dialog [class*='name']",
        ".dialog [class*='name']",
        ".modal [class*='name']",
        "[role='dialog'] [class*='name']",
    ]
    for sel in modal_selectors:
        try:
            els = page.query_selector_all(sel)
            for el in els:
                if el.is_visible():
                    txt = (el.inner_text() or "").strip()
                    if txt and 2 <= len(txt) <= 20 and txt not in names:
                        has_cn = any('\u4e00' <= ch <= '\u9fff' for ch in txt)
                        if has_cn:
                            names.append(txt)
            if len(names) >= 2:
                _log(f"弹窗选择器命中 ({sel}): {names}")
                return names
        except Exception:
            continue
    if names:
        _log(f"弹窗选择器命中: {names}")
        return names

    # === 策略4：JS 兜底 — 遍历所有可见可点击元素 ===
    _log("CSS 选择器均未命中，启动 JS 兜底扫描...")
    try:
        # 保存页面截图和 HTML 到文件，方便调试
        import tempfile, os as _os
        dump_dir = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "debug_dumps")
        _os.makedirs(dump_dir, exist_ok=True)
        ts = str(int(time.time()))
        try:
            page.screenshot(path=_os.path.join(dump_dir, f"auth_page_{ts}.png"), full_page=False)
            _log(f"截图已保存: debug_dumps/auth_page_{ts}.png")
        except Exception:
            pass
        try:
            html = page.content()
            with open(_os.path.join(dump_dir, f"auth_page_{ts}.html"), "w") as f:
                f.write(html)
            _log(f"HTML 已保存 ({len(html)} 字节)")
        except Exception:
            pass

        # JS 遍历：查找所有包含短文本（疑似频道名）的可点击元素
        channel_names = page.evaluate("""() => {
            const names = [];
            const seen = new Set();

            // 遍历所有 a, button, li, div, span 及互动元素
            const els = document.querySelectorAll('a, button, li, div, span, [role="button"], [role="option"], [role="listitem"], [class*="card"], [class*="item"], [class*="account"], [class*="channel"], [class*="finder"]');
            for (const el of els) {
                if (el.offsetParent === null) continue;
                const cls = (el.className || '').toLowerCase();
                // 跳过导航、按钮、图标等非频道元素
                if (cls.includes('nav') && !cls.includes('name')) continue;
                if (cls.includes('menu') || cls.includes('tooltip')) continue;
                if (cls.includes('dialog') || cls.includes('modal') || cls.includes('overlay')) continue;
                if (cls.includes('btn') || cls.includes('button') || cls.includes('icon')) continue;
                if (cls.includes('header') || cls.includes('footer') || cls.includes('sidebar')) continue;
                if (cls.includes('tab') && !cls.includes('table')) continue;
                if (cls.includes('banner') || cls.includes('notice') || cls.includes('popup')) continue;

                const txt = (el.innerText || '').trim();
                if (txt.length < 2 || txt.length > 20) continue;
                if (txt.includes('\\n')) continue;

                const skipWords = ['登录','扫码','注册','退出','设置','帮助','反馈','关于',
                    '首页','内容管理','数据中心','直播管理','互动管理','粉丝管理',
                    '平台','微信','视频号','确定','取消','保存','提交','删除',
                    '下一页','上一页','刷新','加载','无数据','暂无','更多',
                    '我知道了','关闭','已选择','全部','搜索','筛选','通知','消息',
                    '原创','转载','视频','图文','直播','音频',
                    '工作日','休息日','自定义','今天','昨天','最近',
                    '选择视频号登录','使用其他账号登录','运营者'];
                const lower = txt.toLowerCase();
                let isSkip = false;
                for (const w of skipWords) {
                    if (lower === w || lower.startsWith(w) || lower.endsWith(w)) { isSkip = true; break; }
                }
                if (isSkip) continue;
                if (/^[0-9.,:;!?@#$%^&*()_+=\\-\\[\\]{}|/\\\\<>~`"']+$/.test(txt)) continue;

                if (!seen.has(txt)) {
                    seen.add(txt);
                    names.push(txt);
                }
            }
            return names;
        }""")
        _log(f"JS 兜底扫描结果 ({len(channel_names)} 个): {channel_names[:20]}")

        # 过滤：只保留长度合理（2-20字符）且包含中文的文本（频道名通常是中文）
        for name in channel_names:
            if 2 <= len(name) <= 20 and name not in names:
                # 至少包含一个中文字符
                has_chinese = any('\u4e00' <= ch <= '\u9fff' for ch in name)
                if has_chinese:
                    names.append(name)
        if names:
            _log(f"过滤后频道列表: {names}")
    except Exception as e:
        _log(f"JS 兜底扫描出错: {e}")

    return names


def _handle_channel_selection(page, state: dict, channel_list: list, _log, browser):
    """统一的频道选择等待逻辑：将列表推给前端，等待用户选择后点击。"""
    wait_start = time.time()
    while time.time() - wait_start < 120:
        if state.get("cancel"):
            state["status"] = "failed"
            state["message"] = "已取消"
            return
        picked = state.get("_selected_channel")
        if picked:
            _log(f"前端选择了频道: {picked}")
            _try_auto_select_channel(page, channel_list, picked)
            state["_channel_selected"] = True
            state["channel_options"] = None
            state["status"] = "scanned"
            state["message"] = f"已选择频道「{picked}」，正在完成…"
            page.wait_for_timeout(2000)
            return
        time.sleep(1)
    state["status"] = "timeout"
    state["message"] = "选择频道超时"


def switch_and_resave(session_path: str, channel_name: str) -> bool:
    """授权完成后，用已保存的 session 打开平台，切换到指定频道并重新保存。

    由 POST /api/channels/{id}/switch_channel 调用。
    返回 True 表示切换成功，False 表示未找到目标频道。
    """
    import sys as _sys
    from playwright.sync_api import sync_playwright

    def _log(msg: str):
        print(f"[switch_channel] {msg}", flush=True, file=_sys.stderr)

    _log(f"开始切换频道: {channel_name}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            context = browser.new_context(
                storage_state=session_path,
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            _log(f"打开平台: {PLATFORM_URL}")
            try:
                page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(5000)

            # 尝试切换
            _select_channel(page, channel_name)
            page.wait_for_timeout(3000)

            # 验证是否切换成功（检查当前频道名）
            current = _get_current_channel_name(page)
            _log(f"切换后当前频道: {current}")
            if current and (channel_name in current or current in channel_name):
                # 切换成功，重新保存 session
                context.storage_state(path=session_path)
                _log(f"切换成功，session 已更新: {session_path}")
                browser.close()
                return True
            else:
                # 可能切换失败但也保存一下
                context.storage_state(path=session_path)
                _log(f"切换结果不确定，已保存 session")
                browser.close()
                return current is not None and len(current) > 0
    except Exception as e:
        _log(f"切换异常: {e}")
        return False


def _get_current_channel_name(page) -> str:
    """获取当前页面上显示的频道名称。"""
    selectors = [
        ".finder-account__name",
        ".account-info__name",
        ".channel-name",
        ".nickname",
        "[class*='account'] [class*='name']",
        "[class*='finder'] [class*='name']",
    ]
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return (el.inner_text() or "").strip()
        except Exception:
            continue
    return ""


def _try_auto_select_channel(page, channel_list: list, target_name: str) -> bool:
    """在频道选择页面中点击匹配 target_name 的频道。

    返回 True 表示成功点击，False 表示未找到匹配项。
    """
    # 1. 精确匹配
    for name in channel_list:
        if name == target_name:
            return _click_channel_by_name(page, name)

    # 2. 包含匹配
    for name in channel_list:
        if target_name in name or name in target_name:
            return _click_channel_by_name(page, name)

    # 3. 模糊匹配（去除空格和特殊符号）
    clean_target = re.sub(r'\s+', '', target_name)
    for name in channel_list:
        clean_name = re.sub(r'\s+', '', name)
        if clean_target == clean_name or clean_target in clean_name or clean_name in clean_target:
            return _click_channel_by_name(page, name)

    return False


def _click_channel_by_name(page, name: str) -> bool:
    """在页面上通过文本匹配点击频道。"""
    click_selectors = [
        f"text={name}",
        f"[title='{name}']",
        f"[aria-label='{name}']",
        f"a:has-text('{name}')",
        f"li:has-text('{name}')",
        f"div:has-text('{name}')",
        f"span:has-text('{name}')",
        f".finder-account-card:has-text('{name}')",
        f".account-card:has-text('{name}')",
        f".channel-card:has-text('{name}')",
        f"[class*='account']:has-text('{name}')",
        f"[class*='channel']:has-text('{name}')",
    ]
    for sel in click_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return True
        except Exception:
            continue

    # 兜底：遍历所有可见元素找匹配文本
    try:
        all_els = page.query_selector_all("a, button, li, div, span")
        for el in all_els:
            try:
                if el.is_visible():
                    txt = (el.inner_text() or "").strip()
                    if txt == name:
                        el.click()
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def web_auth_worker(session_path: str, state: dict, timeout: int = 240,
                     channel_name: str = None) -> None:
    """供「网页内嵌扫码授权」使用：在后台线程内跑完整登录会话。

    持续把页面里实时刷新的二维码（data:image base64）写入 state['qrcode']，
    供前端轮询展示；检测到 cookie 变化（扫码成功）即保存会话并完成。
    不做页面导航验证（避免跳转打乱微信登录态）。
    state 由调用方创建并共享，可设 state['cancel']=True 主动中止。
    state['status'] 取值：pending / scanned / selecting / success / timeout / failed。
    若 channel_name 不为空，登录后会自动检测并选择对应视频号；
    若页面有多个视频号可选，会将列表推送到 state['channel_options'] 供前端选择。

    使用独立 Playwright 实例（greenlet 线程绑定，不可跨线程共享）。
    """
    import sys as _sys
    from playwright.sync_api import sync_playwright

    def _log(msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"[auth:{ts}] {msg}"
        print(line, flush=True, file=_sys.stderr)
        prev = state.get("_log", "")
        state["_log"] = (prev + "\n" + line).strip()

    _log("worker start")
    try:
        with sync_playwright() as p:
            _log("启动无头浏览器")
            state["status"] = "starting"
            state["message"] = "正在启动浏览器..."
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            _log(f"goto {HOME_URL}")
            state["message"] = "正在打开登录页面..."
            try:
                page.goto(HOME_URL, wait_until="load", timeout=20000)
            except Exception:
                _log("goto timed out, continuing...")
            _log(f"initial URL: {page.url}")
            
            # 动态等待二维码出现（最多等10秒）
            state["message"] = "正在等待二维码加载..."
            qr_data = None

            def _extract_qr(page, context) -> Optional[str]:
                """从页面 iframe 中提取二维码 base64。
                支持两种格式：data:image（macOS 本地有头）和相对路径（服务器无头）。
                服务器无头模式：打开二维码图片 URL 截图转 base64。"""
                import base64 as _b64
                for fr in page.frames:
                    try:
                        el = fr.query_selector("img.qrcode")
                        if not el:
                            continue
                        src = el.get_attribute("src") or ""
                        if not src:
                            continue
                        # macOS 本地有头模式：直接是 data:image
                        if src.startswith("data:image"):
                            return src
                        # 服务器无头模式：相对路径如 /connect/qrcode/xxxx
                        if src.startswith("/connect/qrcode/") or "qrcode" in src:
                            from urllib.parse import urlparse
                            parsed = urlparse(fr.url)
                            img_url = f"{parsed.scheme}://{parsed.netloc}{src}"
                            # 用同 context 打开新页面（带 cookie）截图二维码
                            qr_page = None
                            try:
                                qr_page = context.new_page()
                                qr_page.goto(img_url, wait_until="load", timeout=10000)
                                time.sleep(1)
                                ss = qr_page.screenshot()
                                return "data:image/png;base64," + _b64.b64encode(ss).decode()
                            except Exception:
                                pass
                            finally:
                                if qr_page:
                                    try:
                                        qr_page.close()
                                    except Exception:
                                        pass
                    except Exception:
                        pass
                return None

            for _ in range(20):
                qr_data = _extract_qr(page, context)
                if qr_data:
                    state["qrcode"] = qr_data
                    state["status"] = "pending"
                    state["message"] = "请使用微信扫码授权"
                    break
                page.wait_for_timeout(500)
            
            if not qr_data:
                # 降级：固定等待后再试
                page.wait_for_timeout(2000)
                qr_data = _extract_qr(page, context)
                if qr_data:
                    state["qrcode"] = qr_data
                    state["status"] = "pending"
                    state["message"] = "请使用微信扫码授权"

            if not qr_data:
                state["status"] = "failed"
                state["message"] = "无法获取二维码，请重试"
                _log("QR code not found")
                return

            _log("QR code extracted successfully")

            deadline = time.time() + timeout
            initial_cookie_count = len(context.cookies())
            page.wait_for_timeout(1500)
            initial_cookie_count = len(context.cookies())
            initial_url = page.url
            _log(f"等待扫码，初始 cookies: {initial_cookie_count}, 初始 url: {initial_url}")
            scan_confirmed = False
            scan_ts = 0.0
            browser_closed = False

            while time.time() < deadline:
                if state.get("cancel"):
                    state["status"] = "failed"
                    state["message"] = "已取消"
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return

                # 安全获取页面信息（页面可能因跳转暂时不可用）
                try:
                    url_now = page.url
                except Exception:
                    if not browser_closed:
                        _log("页面不可访问，浏览器可能已关闭")
                        browser_closed = True
                    break

                # === 扫码前：抓取二维码 ===
                if not scan_confirmed:
                    src = _find_qr_src(page)
                    if not src:
                        src = _fetch_qr_via_page(page)
                    if src:
                        state["qrcode"] = src

                # 检测扫码完成
                try:
                    current_cookie_count = len(context.cookies())
                except Exception:
                    current_cookie_count = 0

                cookie_signal = (current_cookie_count > initial_cookie_count) or _login_cookies_present(context)
                url_changed = ("/platform" in url_now or "/home" in url_now or "/nfa" in url_now)
                has_logged_in_cookies = _login_cookies_present(context)

                if not scan_confirmed and (cookie_signal or url_changed or has_logged_in_cookies):
                    _log(f"检测到扫码: cookies={initial_cookie_count}→{current_cookie_count} url_changed={url_changed}")
                    scan_confirmed = True
                    scan_ts = time.time()
                    state["status"] = "scanned"
                    state["message"] = "已扫码，请在浏览器中完成后续操作（选择视频号等）"
                    state["qrcode"] = None  # 清除二维码，前端不再显示

                if scan_confirmed:
                    elapsed = time.time() - scan_ts
                    try:
                        url_now = page.url
                    except Exception:
                        url_now = ""

                    try:
                        cookies_now = len(context.cookies())
                    except Exception:
                        cookies_now = 0

                    on_platform = ("/platform" in url_now or "/home" in url_now)
                    can_save = (cookies_now >= 3) or has_logged_in_cookies or _login_cookies_present(context)

                    # 扫码后3秒检测是否有多个视频号可选
                    if elapsed >= 3 and not state.get("_channel_checked"):
                        state["_channel_checked"] = True
                        _log("检测视频号列表...")
                        channel_list = _detect_auth_channel_list(page)
                        _log(f"检测到视频号列表: {channel_list}")
                        if len(channel_list) > 1:
                            state["channel_options"] = channel_list
                            state["status"] = "selecting"
                            state["message"] = "请选择要管理的视频号"
                            _log(f"多视频号，等待前端选择...")
                            # 等前端选择（最长 120 秒）
                            _handle_channel_selection(page, state, channel_list, _log, browser)
                            # 选择完成后继续
                            state["status"] = "scanned"
                            state["message"] = "已选择视频号，正在完成登录..."
                        elif len(channel_list) == 1:
                            _log(f"单视频号: {channel_list[0]}，自动选择")
                            _try_auto_select_channel(page, channel_list, channel_list[0])
                            page.wait_for_timeout(3000)
                        else:
                            _log("未检测到视频号列表，可能已直接进入平台")

                    # 已到平台页且有足够 cookie → 保存 session
                    if on_platform and can_save:
                        _log(f"检测到已进入平台，保存 session... url={url_now} cookies={cookies_now}")
                        _dump_cookies(context, "final")
                        try:
                            context.storage_state(path=session_path)
                            _log(f"session 已保存: {session_path}")
                        except Exception as e:
                            _log(f"保存 session 失败: {e}")

                        state["status"] = "success"
                        state["message"] = "授权成功"
                        page.wait_for_timeout(3000)
                        try:
                            browser.close()
                        except Exception:
                            pass
                        return

                    # 更新状态提示（给前端显示进度）
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)
                    if mins > 0:
                        state["message"] = f"已扫码（{mins}分{secs}秒），请在浏览器中选择视频号完成登录…"
                    else:
                        state["message"] = f"已扫码（{secs}秒），请在浏览器中选择视频号完成登录…"

                    # 超时兜底（3分钟），只要有登录态就保存
                    if elapsed >= 180 and can_save:
                        _log(f"超时兜底保存 session... url={url_now} cookies={cookies_now}")
                        _dump_cookies(context, "final")
                        try:
                            context.storage_state(path=session_path)
                            _log(f"session 已保存: {session_path}")
                        except Exception as e:
                            _log(f"保存 session 失败: {e}")

                        state["status"] = "success"
                        state["message"] = "授权成功（超时兜底）"
                        try:
                            browser.close()
                        except Exception:
                            pass
                        return

                    # 扫码后超时但还没登录 → 提示用户
                    if elapsed >= 180 and not can_save:
                        state["status"] = "timeout"
                        state["message"] = "扫码后未完成登录，请重新发起"
                        try:
                            browser.close()
                        except Exception:
                            pass
                        return

                time.sleep(0.8)

            # 总超时
            if browser_closed:
                state["status"] = "failed"
                state["message"] = "浏览器已关闭，授权中断"
            elif scan_confirmed:
                state["status"] = "timeout"
                state["message"] = "扫码后未完成登录，请重新发起"
            elif not scan_confirmed:
                state["status"] = "timeout"
                state["message"] = "二维码超时未扫"
            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:
        state["status"] = "failed"
        state["message"] = f"授权出错：{e}"
        _log(f"exception: {e}")


def login(timeout: int = 240, on_success=None, session_path=None, headed=False):
    """无头打开后台，提取登录 iframe 内 img.qrcode 的 base64，解码保存为清晰二维码图片，
    用户打开该图片用手机扫码；后台轮询登录状态。

    headed=True 时改为弹出真实浏览器窗口，显示实时自动刷新的二维码，用户直接对着
    屏幕扫（彻底规避"图片文件过期/预览不刷新扫到旧码"的问题）。

    登录成功后：
      - 若提供 on_success(context, page) 回调，则在同一活会话上调用它并返回其结果
        （用于"扫码后立即采集"，绕开会话持久化失效问题）；
      - 否则保存会话到 session_path（默认 SESSION_PATH）并返回 True。
    """
    out_path = session_path or SESSION_PATH
    import base64
    from playwright.sync_api import sync_playwright

    def find_qr_src(page) -> Optional[str]:
        for fr in page.frames:
            try:
                el = fr.query_selector("img.qrcode")
                if el:
                    s = el.get_attribute("src") or ""
                    if s.startswith("data:image"):
                        return s
            except Exception:
                pass
        return None

    def login_cookies_present(context) -> bool:
        # 登录成功后 context 会出现会话票据 cookie；未扫码时不会有，绝不误判。
        try:
            for c in context.cookies():
                name = (c.get("name") or "").lower()
                if c.get("value") and any(
                    k in name for k in ("sessionid", "wxuin", "data_ticket", "media_ticket", "uin")
                ):
                    return True
        except Exception:
            pass
        return False

    def logged_in(page) -> bool:
        url = page.url
        return ("/platform" in url) and ("login" not in url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, args=_LAUNCH_ARGS)
        context = browser.new_context()
        page = context.new_page()
        page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
        if headed:
            print("已弹出登录窗口，请在弹出的浏览器里直接用手机微信扫二维码（无需打开图片）。")
        print("正在获取登录二维码……")

        deadline = time.time() + timeout
        last_src = None
        ok = False
        last_probe = time.time()
        while time.time() < deadline:
            # 信号A：已自动跳转到工作台
            if logged_in(page):
                ok = True
                break
            # 信号B：检测到登录态 cookie；或每 45 秒兜底主动探测一次工作台。
            cookie_signal = login_cookies_present(context)
            due_probe = (time.time() - last_probe > 45)
            if cookie_signal or due_probe:
                last_probe = time.time()
                if cookie_signal:
                    print("COOKIE_SIGNAL 检测到登录态，正在完成登录……")
                try:
                    page.goto(PLATFORM_URL, wait_until="networkidle", timeout=20000)
                except Exception:
                    pass
                if logged_in(page):
                    ok = True
                    break
                # 未登录：回登录页继续拿二维码
                try:
                    page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
                except Exception:
                    pass
            # 仍在登录页：刷新二维码图片
            src = find_qr_src(page)
            if src and src != last_src:
                try:
                    b64 = src.split(",", 1)[1]
                    with open(QRCODE_PATH, "wb") as f:
                        f.write(base64.b64decode(b64))
                    last_src = src
                    print(f"QRCODE_SAVED {QRCODE_PATH}")
                    print("请打开该图片用手机微信扫码（二维码会自动刷新，过期请重新打开图片）。")
                except Exception as e:
                    print(f"保存二维码失败: {e}")
            time.sleep(2)

        if not ok:
            print("LOGIN_TIMEOUT 超时未检测到登录成功，请重试。")
            browser.close()
            return [] if on_success else False

        # 登录成功
        time.sleep(3)
        try:
            context.storage_state(path=out_path)
        except Exception:
            pass
        print(f"LOGIN_OK 登录成功，会话已保存：{out_path}")

        if on_success is not None:
            try:
                result = on_success(context, page)
            finally:
                browser.close()
            return result

        browser.close()
        return True


# ---------------- 解析视频列表 JSON ----------------
def _text_of(v: Any) -> str:
    """从字段值中取出文本：兼容字符串与嵌套 dict（如 {"description": "..."}）。"""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        for kk in ("description", "content", "text", "title", "desc"):
            inner = v.get(kk)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def _fmt_time(ts: Any) -> str:
    try:
        n = int(ts)
        if n > 0:
            return time.strftime("%Y-%m-%d %H:%M", time.localtime(n))
    except Exception:
        pass
    return ""


def _extract_media(desc_field: Any) -> List[Dict[str, Any]]:
    """从作品的 desc.media[] 提取媒体资源并下载缩略图。

    视频号图文/视频的媒体在 desc.media[] 中：thumbUrl=缩略图、url=原图/原视频、
    videoPlayLen>0 表示视频。下载缩略图到本地，原始地址保留为“查看原始”链接。
    """
    medias: List[Dict[str, Any]] = []
    raw = desc_field.get("media") if isinstance(desc_field, dict) else None
    if not isinstance(raw, list):
        return medias
    for m in raw:
        if not isinstance(m, dict):
            continue
        thumb_remote = m.get("thumbUrl") or m.get("coverUrl") or m.get("url") or ""
        orig = m.get("url") or m.get("fullUrl") or thumb_remote
        play_len = m.get("videoPlayLen") or 0
        is_video = bool(play_len and play_len > 0)
        entry = {
            "thumb": _download_thumb(thumb_remote),   # 本地永久缩略图/封面 /media/xxx.jpg
            "thumb_remote": thumb_remote,             # 远程缩略图（备用）
            "orig": orig,                              # 原图/原视频地址（可能有时效）
            "is_video": is_video,
            "play_len": play_len,
            "w": m.get("width"), "h": m.get("height"),
        }
        if is_video:
            entry["video_local"] = ""  # 稍后在 _collect_on_page 中用会话 cookie 统一下载
        medias.append(entry)
    return medias


def _extract_stats(it: Dict[str, Any]) -> Dict[str, Any]:
    """提取作品的真实互动数据（观看/点赞/评论/转发/收藏等）。

    字段来自视频号助手后台 post/post_list 接口，均为平台真实统计值：
    readCount=观看/阅读、likeCount=点赞、commentCount=评论、
    forwardCount=转发、favCount=收藏、avgPlayTimeSec=平均播放时长(视频)、
    fullPlayRate=完播率(视频)。
    """
    def i(v):
        try:
            return int(v or 0)
        except Exception:
            return 0
    return {
        "read": i(it.get("readCount")),
        "like": i(it.get("likeCount")),
        "comment": i(it.get("commentCount")),
        "forward": i(it.get("forwardCount")),
        "fav": i(it.get("favCount")),
        "follow": i(it.get("followCount")),
        "avg_play_sec": round(float(it.get("avgPlayTimeSec") or 0), 1),
        "full_play_rate": round(float(it.get("fullPlayRate") or 0) * 100, 1),
    }


def _extract_posts(data: Any) -> List[Dict[str, Any]]:
    """精确解析 post/post_list / get_collection_list 等结构中的作品条目。

    支持两种格式：
    1. data.list[] 中含 objectId —— post/post_list
    2. data.data.list[] 含 objectId —— 嵌套 data 包装（部分接口）
    3. data.collectionList[].missEpisodesInfo.missEpisodeItems[] —— 合集缺失剧集
    """
    out: List[Dict[str, Any]] = []
    seen_oids = set()

    def add_item(it: dict):
        oid = str(it.get("objectId") or "")
        if not oid or oid in seen_oids:
            return
        seen_oids.add(oid)
        desc_field = it.get("desc")
        text = (_text_of(desc_field)
                or _text_of(it.get("feedDesc"))
                or _text_of(it.get("title")))
        topics = re.findall(r"#[^#\s]+", text)
        out.append({
            "object_id": oid,
            "title": (text or f"(无文案) {oid}")[:60],
            "caption": text,
            "ocr_text": " ".join(topics),
            "transcript": "",
            "video_url": oid,
            "publish_time": _fmt_time(it.get("createTime")),
            "media": _extract_media(desc_field),
            "stats": _extract_stats(it),
        })

    def walk(node: Any):
        if isinstance(node, dict):
            # 格式1/2：post_list 的 data.list[] / 嵌套 data.data.list[]
            lst = node.get("list")
            if isinstance(lst, list):
                for it in lst:
                    if isinstance(it, dict) and ("objectId" in it or "objectNonce" in it):
                        add_item(it)
            # 格式3：合集列表 → 提取每个集合的 missEpisodeItems
            clist = node.get("collectionList")
            if isinstance(clist, list):
                for col in clist:
                    if isinstance(col, dict):
                        miss_info = col.get("missEpisodesInfo", {})
                        if isinstance(miss_info, dict):
                            for me in miss_info.get("missEpisodeItems") or []:
                                if isinstance(me, dict) and me.get("objectId"):
                                    oid = str(me["objectId"])
                                    if oid not in seen_oids:
                                        seen_oids.add(oid)
                                        out.append({
                                            "object_id": oid,
                                            "title": f"(合集缺失剧集) {col.get('name','')} #{me.get('episodeNumber','')}",
                                            "caption": f"合集「{col.get('name','')}」第{me.get('episodeNumber','')}集（objectId: {oid}）",
                                            "ocr_text": "",
                                            "transcript": "",
                                            "video_url": oid,
                                            "publish_time": "",
                                            "media": [],
                                            "stats": {},
                                        })
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


# ---------------- 采集 ----------------
def _open_authed_page(p, headless: bool, session_path=None):
    sp = session_path or SESSION_PATH
    if not os.path.exists(sp):
        raise RuntimeError(
            "该视频号尚未扫码授权（未找到其登录会话）。请先为该号扫码授权后再采集。"
        )
    browser = p.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
    context = browser.new_context(storage_state=sp)
    page = context.new_page()
    return browser, context, page


def _continue_scroll_if_needed(page, captured_json: List[Any], content_count: list) -> None:
    """检查已捕获的 post_list 响应中是否有 continueFlag=True，
    如果有，在页面上继续滚动触发框架自动发翻页请求。

    微信后台 post_list 接口使用 lastBuff 游标分页，每次返回 ~20 条。
    滚动到页面底部会自动触发下一页请求（框架自动处理 lastBuff 参数）。
    """
    # 检查是否还有未加载的内容
    has_more = False
    total = 0
    for data in captured_json:
        if not isinstance(data, dict):
            continue
        d = data.get("data", {})
        if not isinstance(d, dict) or "list" not in d:
            continue
        if d.get("continueFlag"):
            has_more = True
            total = max(total, d.get("totalCount", 0))

    if not has_more:
        return

    print(f"[collect] 检测到还有更多内容(总计{total}条)，继续滚动翻页...", flush=True)
    # 继续滚动触发翻页（框架会自动发 post_list 请求）
    _scroll_until_done(page, max_rounds=200, wait=0.4, content_count=content_count, min_rounds=5)


def _select_channel(page, channel_name: str) -> None:
    """多视频号场景：在微信视频号助手后台切换到指定频道。

    登录后，平台顶部/左侧会显示当前选中的视频号名称或头像。
    如果已匹配目标号则跳过；否则打开频道列表并点击目标号。
    """
    # 1. 检查当前已选中的频道是否已匹配（顶部频道名称/头像区域）
    current_selectors = [
        ".finder-account__name",
        ".account-info__name",
        ".channel-name",
        ".nickname",
        "[class*='account'] [class*='name']",
        "[class*='finder'] [class*='name']",
        ".profile-info .name",
    ]
    for sel in current_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                txt = (el.inner_text() or "").strip()
                if channel_name in txt or txt in channel_name:
                    print(f"[_select_channel] 已选中目标频道: {txt}")
                    return
                break  # 找到了频道名称元素但不匹配 → 需要切换
        except Exception:
            continue

    print(f"[_select_channel] 需要切换到: {channel_name}")

    # 2. 尝试打开频道切换面板（多种 UI 模式）
    switcher_selectors = [
        ".finder-account__name",         # 点击当前频道名称打开列表
        ".account-info__name",
        "[class*='account']",             # 账号区域点击
        "[class*='switch']",              # 切换按钮
        "text=切换账号",
        "text=切换",
        ".avatar",                        # 头像区域
        "[class*='finder'] [class*='avatar']",
    ]
    opened = False
    for sel in switcher_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                time.sleep(1.5)
                opened = True
                break
        except Exception:
            continue

    if not opened:
        print(f"[_select_channel] 未找到频道切换入口，跳过选择")
        return

    # 3. 在弹出的频道列表中查找并点击目标频道
    # 频道列表可能的 DOM 结构：
    #   - 弹出下拉菜单 .finder-account__list / .account-list / .dropdown-menu
    #   - 每个条目包含频道名 + 头像
    #   - 也可能是独立的页面路由
    item_selectors = [
        f"text={channel_name}",
        f"[title='{channel_name}']",
        f"li:has-text('{channel_name}')",
        f"div:has-text('{channel_name}'):not(:has(div:has-text('{channel_name}')))",
        f".account-item:has-text('{channel_name}')",
        f"[class*='finder']:has-text('{channel_name}')",
    ]
    for sel in item_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                # 确保不是整个页面级别的匹配（太宽泛的 has-text）
                tag = el.evaluate("el => el.tagName")
                text_content = el.inner_text()
                if channel_name in text_content:
                    el.click()
                    time.sleep(2)  # 等待页面刷新/路由切换
                    print(f"[_select_channel] 已点击切换到: {channel_name}")
                    return
        except Exception:
            continue

    # 4. 最后兜底：逐个检查可见文本元素
    try:
        elements = page.query_selector_all("span, div, a, li, button")
        for el in elements:
            try:
                if not el.is_visible():
                    continue
                txt = (el.inner_text() or "").strip()
                if txt == channel_name or txt.startswith(channel_name):
                    el.click()
                    time.sleep(4)
                    print(f"[_select_channel] 兜底匹配点击: {channel_name}")
                    return
            except Exception:
                continue
    except Exception:
        pass

    print(f"[_select_channel] 未能匹配频道: {channel_name}，将继续采集当前选中号的内容")
    return


def _navigate_to_post_list(page, channel_name: str = None) -> None:
    """进入内容管理页。

    必须先进工作台首页，再点击左侧菜单做应用内路由切换。
    多视频号场景：登录后若平台显示频道选择界面，自动切换到目标号。
    子标签页（视频/图文）由 _click_subtab_and_scroll 单独处理。
    """
    try:
        page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    time.sleep(2)
    # 关掉可能出现的弹窗
    for dismiss_text in ["我知道了", "确定", "关闭", "取消"]:
        try:
            el = page.query_selector(f"text={dismiss_text}")
            if el and el.is_visible():
                el.click()
                time.sleep(0.5)
        except Exception:
            pass

    # —— 多视频号场景：选择目标频道 ——
    if channel_name:
        _select_channel(page, channel_name)

    # 展开"内容管理"菜单
    time.sleep(1)
    for attempt in range(4):
        try:
            page.click("text=内容管理", timeout=5000)
            time.sleep(1)
            break
        except Exception:
            time.sleep(0.8)


def _scroll_until_done(page, max_rounds: int = 80, wait: float = 0.4,
                       stable_threshold: int = 3,
                       content_count: list = None,
                       min_rounds: int = 5) -> None:
    """持续滚动页面，直到连续 stable_threshold 轮后内容条数不再增长或达到最大轮数。

    content_count 为 [整数] 列表（闭包可变引用），on_response 回调实时更新。
    每轮滚动后检查 content_count[0] 是否变化——只要还在返回新内容就继续滚。
    min_rounds: 最少滚动轮数，即使提前稳定也必须滚满这么多轮（防止 API 响应延迟导致误判）。
    """
    prev_count = content_count[0] if content_count else 0
    prev_height = 0
    stable_count = 0
    for round_num in range(max_rounds):
        try:
            # 优先点击分页按钮（微信后台列表底部的 1 2 3 4 5 分页）
            # 每页 20 条，点击下一页按钮触发 post_list API 翻页
            next_btn = page.query_selector(
                ".weui-desktop-pagination__nav--next, "
                ".weui-desktop-pagination__btn--next, "
                "li.weui-desktop-pagination__nav--next, "
                "button:has-text('下一页'), "
                "a:has-text('下一页'), "
                ".next, "
                "[aria-label='Next'], "
                ".pagination li:last-child a, "
                ".pagination li:last-child button"
            )
            if next_btn and next_btn.is_visible():
                next_btn.click()
                time.sleep(wait)
                continue
        except Exception:
            pass
        try:
            # 没有分页按钮时，模拟键盘滚动
            page.keyboard.press("End")
        except Exception:
            pass
        try:
            page.keyboard.press("PageDown")
        except Exception:
            pass
        time.sleep(wait)
        if content_count is not None:
            cur_count = content_count[0]
            if cur_count == prev_count:
                stable_count += 1
                if stable_count >= stable_threshold and round_num >= min_rounds:
                    print(f"[scroll] 第{round_num}轮: content_count={cur_count} 连续{stable_count}轮无增长，停止滚动", flush=True)
                    break
            else:
                print(f"[scroll] 第{round_num}轮: content_count {prev_count}→{cur_count}", flush=True)
                stable_count = 0
                prev_count = cur_count
        else:
            try:
                cur_height = page.evaluate("document.documentElement.scrollHeight")
            except Exception:
                cur_height = prev_height
            if cur_height == prev_height:
                stable_count += 1
                if stable_count >= stable_threshold and round_num >= min_rounds:
                    break
            else:
                stable_count = 0
                prev_height = cur_height


def _click_subtab_and_scroll(page, tab_keywords, content_count: list = None) -> bool:
    """点击内容管理下的子标签页（视频/图文），滚动加载内容，返回是否成功进入。

    用户描述的路径：左侧栏"内容管理" → 子菜单"视频" → 列表页可能显示部分内容，
    下滑后出现"全部视频"按钮，点击后才显示完整列表。
    content_count 为 [整数] 列表，用于判断滚动是否还有新内容加载。
    """
    for kw in tab_keywords:
        try:
            # 优先匹配左侧栏菜单项（精确文本匹配，避免误点页面上其他含"视频"的元素）
            el = page.query_selector(f".weui-desktop-sidebar__link:has-text(\"{kw}\"), .menu-item:has-text(\"{kw}\"), a:has-text(\"{kw}\")")
            if not el or not el.is_visible():
                # 退而求其次：用 text= 但限制在侧边栏区域
                el = page.query_selector(f"text={kw}")
            if el and el.is_visible():
                print(f"[collect] 点击标签页 '{kw}'", flush=True)
                el.click()
                time.sleep(3)
                # 先滚动触发懒加载
                _scroll_until_done(page, max_rounds=30, wait=0.4, content_count=content_count, min_rounds=5)
                # 点击"全部视频"/"全部图文"按钮（主页只显示5条，点这个才显示全部）
                for btn_kw in ["全部视频", "全部图文", "全部作品", "查看全部"]:
                    try:
                        btn = page.query_selector(f"text={btn_kw}")
                        if btn and btn.is_visible():
                            print(f"[collect] 点击按钮 '{btn_kw}'", flush=True)
                            btn.click()
                            time.sleep(3)
                            # 点击后持续滚动直到全部加载
                            _scroll_until_done(page, max_rounds=200, wait=0.4, content_count=content_count, min_rounds=5)
                            break
                    except Exception:
                        continue
                return True
        except Exception:
            continue
    return False


def _collect_on_page(context, page, max_videos: int, dump: bool, channel_name: str = None) -> List[Dict[str, Any]]:
    """在已登录页面上采集内容：监听后台 JSON 接口 + 导航视频/图文标签页 + 解析。

    依次点击"视频"和"图文"标签页，各自触发不同的 post_list 接口，
    由 _extract_posts 精确解析含 objectId 的作品条目。
    channel_name 用于多视频号场景下在后台切换目标号。
    """
    captured_json: List[Any] = []
    captured_log: List[Dict[str, Any]] = []  # {url, keys/summary} 便于定位内容接口
    captured_post_list_urls: List[str] = []  # 记录 post_list 的完整 URL（含查询参数）
    # 累计从 API 响应中提取到的内容条数——用于滚动终止判断
    # 当连续多轮滚动后此值不再增长，说明已加载全部内容
    content_count = [0]  # 用 list 以便闭包修改

    def on_response(resp):
        try:
            url = resp.url
            ct = (resp.headers or {}).get("content-type", "")
            # 后台真实接口（mmfinderassistant-bin）无条件尝试解析；其余仅当声明 json 时解析
            if API_PREFIX in url or "json" in ct:
                data = resp.json()
                captured_json.append(data)
                # 记录 post_list 的完整 URL（含查询参数）
                if "post_list" in url or "postList" in url:
                    captured_post_list_urls.append(url)
                    print(f"[collect] post_list URL: {url[:200]}", flush=True)
                # 记录接口路径与顶层结构，便于诊断哪个接口含内容
                short = url.split("?", 1)[0].split("mmfinderassistant-bin/")[-1]
                top = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                captured_log.append({"api": short, "top": top})
                # 实时统计从本条响应中提取的内容数
                posts = _extract_posts(data)
                if posts:
                    content_count[0] += len(posts)
                    print(f"[collect] on_response: +{len(posts)} 条, 累计 content_count={content_count[0]}", flush=True)
        except Exception:
            pass

    page.on("response", on_response)

    _navigate_to_post_list(page, channel_name)

    # 依次点击"视频"和"图文"标签页，各自触发 post_list 接口
    # "视频"标签页含视频类内容（如"真假南孚辨别小指南"）
    # "图文"标签页含图文类内容
    _click_subtab_and_scroll(page, ["视频", "动态视频", "视频作品"], content_count)
    _click_subtab_and_scroll(page, ["图文", "最近图文", "图文作品"], content_count)

    # 点击合集卡片以触发合集内部内容 API（get_collection_feed 等）
    collection_keywords = ["合集", "系列", "扫假王", "绿野联盟"]
    for kw in collection_keywords:
        try:
            el = page.query_selector(f"text={kw}")
            if el and el.is_visible():
                el.click()
                time.sleep(2)
                # 合集详情页滚动懒加载
                _scroll_until_done(page, max_rounds=20, wait=0.4, content_count=content_count, min_rounds=5)
                time.sleep(1)
                break
        except Exception:
            continue

    if dump:
        os.makedirs(DUMP_DIR, exist_ok=True)
        try:
            page.screenshot(path=os.path.join(DUMP_DIR, "page.png"), full_page=True)
            with open(os.path.join(DUMP_DIR, "page.html"), "w", encoding="utf-8") as f:
                f.write(page.content())
        except Exception:
            pass
        with open(os.path.join(DUMP_DIR, "captured_api.json"), "w", encoding="utf-8") as f:
            json.dump(captured_json, f, ensure_ascii=False, indent=2)
        with open(os.path.join(DUMP_DIR, "captured_log.json"), "w", encoding="utf-8") as f:
            json.dump(captured_log, f, ensure_ascii=False, indent=2)
        print(f"已导出后台页面结构到 {DUMP_DIR}/ （page.png / page.html / captured_api.json / captured_log.json）")
        print(f"共捕获 {len(captured_json)} 个接口响应，落点URL：{page.url}")

    # 额外等 1 秒，确保最后一批异步 API 响应完全落盘
    time.sleep(1)

    # 页面滚动 + API 翻页：通过持续滚动触发更多 post_list 请求
    # 如果 continueFlag=True（还有更多内容），继续滚动触发翻页
    _continue_scroll_if_needed(page, captured_json, content_count)

    # 只从 post/post_list 和 collection 接口提取内容，不用兜底解析
    # 避免把 notification_list（通知）里的条目误当成视频/图文内容
    items: List[Dict[str, Any]] = []
    for idx, data in enumerate(captured_json):
        posts = _extract_posts(data)
        if posts:
            print(f"[collect] API[{idx}] → {len(posts)} 条内容", flush=True)
        items.extend(posts)

    print(f"[collect] 提取总计 {len(items)} 条，去重中…", flush=True)

    # 去重（按 objectId 优先，其次 caption）+ 截断
    # 注意：空 key 不参与去重（两个空 key 不应被视为"相同"）
    seen, uniq = set(), []
    for it in items:
        vid = (it.get("video_url") or "").strip()
        cap = (it.get("caption") or "").strip()
        key = vid or cap or None
        if key is None:
            uniq.append(it)  # 无标识可去重，保留
        elif key not in seen:
            seen.add(key)
            uniq.append(it)

    # 不在采集阶段下载视频——视频通过 /api/video_proxy 按需代理 CDN 流式传输，
    # 大幅加快采集速度（56条视频原本需要逐个下载，现在秒级完成）。
    # AI 审核需要视频文件时，按需下载并缓存到本地。

    return uniq if max_videos == 0 else uniq[:max_videos]


def collect(max_videos: int = 30, headless: bool = True, dump: bool = False,
            session_path=None, channel_name: str = None) -> List[Dict[str, Any]]:
    """复用已保存会话采集（免扫码路径；若会话失效会拿不到数据）。
    session_path 指定该视频号专属的登录态文件。
    channel_name 用于多视频号场景下在后台切换目标号。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, context, page = _open_authed_page(p, headless=headless, session_path=session_path)
        try:
            return _collect_on_page(context, page, max_videos, dump, channel_name=channel_name)
        finally:
            browser.close()


def login_and_collect(max_videos: int = 30, dump: bool = False) -> List[Dict[str, Any]]:
    """扫码登录后在同一活会话里立即采集（绕开会话持久化失效），返回内容列表。"""
    return login(on_success=lambda ctx, pg: _collect_on_page(ctx, pg, max_videos, dump))


def collect_channel(channel_name: str, max_videos: int = 0) -> List[Dict[str, Any]]:
    """供后端调用：用该视频号专属的登录态无头采集（不同微信账号互相隔离）。
    该号未授权（无专属会话）时，_open_authed_page 会抛出明确错误。
    channel_name 传递给采集流程用于多视频号切换。"""
    cid = _resolve_channel_id(channel_name)
    sp = session_path_for(cid) if cid is not None else SESSION_PATH
    return collect(max_videos=max_videos, headless=True, dump=False, session_path=sp, channel_name=channel_name)


def _live_session_valid(session_path=None) -> bool:
    """无头验证服务端登录态是否仍有效：打开后台首页，未被重定向到登录页即有效。"""
    sp = session_path or SESSION_PATH
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
            ctx = b.new_context(storage_state=sp)
            pg = ctx.new_page()
            try:
                pg.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=25000)
                time.sleep(4)
                return "login" not in pg.url
            finally:
                b.close()
    except Exception:
        return False


def _find_any_valid_session() -> Optional[str]:
    """扫描所有会话文件（旧版单会话 + 多视频号独立会话），返回第一个存在且非空的路径。"""
    # 旧版单会话（命令行 login）
    if os.path.exists(SESSION_PATH):
        try:
            data = json.load(open(SESSION_PATH, encoding="utf-8"))
            if data.get("cookies"):
                return SESSION_PATH
        except Exception:
            pass
    # 多视频号隔离会话（网页扫码）
    if os.path.isdir(SESSIONS_DIR):
        for fname in sorted(os.listdir(SESSIONS_DIR)):
            if not fname.endswith(".json"):
                continue
            fp = os.path.join(SESSIONS_DIR, fname)
            try:
                data = json.load(open(fp, encoding="utf-8"))
                if data.get("cookies"):
                    return fp
            except Exception:
                pass
    return None


def auth_status(deep: bool = False, session_path=None) -> Dict[str, Any]:
    """检测某视频号后台授权（登录态）状态。

    session_path 指定该号专属会话文件；未指定时自动扫描所有会话（旧版单会话 + 多视频号隔离）。
    轻量模式(deep=False)：仅读本地会话 cookie 的存在性与标称有效期，快。
    深度模式(deep=True)：额外无头打开后台首页，真实验证服务端登录态是否仍有效
        （可发现"在别处重新登录/被风控导致服务端提前失效"），较慢(数秒)。
    """
    sp = session_path or _find_any_valid_session()
    result: Dict[str, Any] = {
        "authorized": False, "reason": "", "cookie_expires_at": None,
        "cookie_days_left": None, "deep_checked": False, "live_valid": None,
    }
    if not sp:
        result["reason"] = "尚未授权：未找到登录会话，请扫码授权。"
        return result
    try:
        data = json.load(open(sp, encoding="utf-8"))
        cookies = data.get("cookies", [])
    except Exception:
        result["reason"] = "授权文件损坏，请重新扫码授权。"
        return result
    if not cookies:
        result["reason"] = "授权已失效：会话为空，请重新扫码授权。"
        return result
    now = time.time()
    exps = [c.get("expires") for c in cookies
            if isinstance(c.get("expires"), (int, float)) and c.get("expires") > 0]
    min_exp = min(exps) if exps else None
    if min_exp is not None:
        result["cookie_expires_at"] = time.strftime("%Y-%m-%d", time.localtime(min_exp))
        result["cookie_days_left"] = int((min_exp - now) / 86400)
        if min_exp <= now:
            result["reason"] = "授权已过期，请重新扫码授权。"
            return result
    result["authorized"] = True
    result["reason"] = "授权正常。"
    if deep:
        result["deep_checked"] = True
        result["live_valid"] = _live_session_valid(sp)
        if not result["live_valid"]:
            result["authorized"] = False
            result["reason"] = ("服务端登录态已失效（可能在其他设备重新登录或触发安全验证），"
                                "请重新扫码授权。")
    return result


def probe() -> None:
    """探测登录页结构：列出各 frame 内的 img / canvas，写入 channels_dump/probe.json，
    用于精确定位二维码元素（避免整页截图截不到异步加载的二维码）。"""
    from playwright.sync_api import sync_playwright

    os.makedirs(DUMP_DIR, exist_ok=True)
    report: Dict[str, Any] = {"frames": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        context = browser.new_context()
        page = context.new_page()
        page.goto(HOME_URL, wait_until="networkidle", timeout=30000)
        time.sleep(8)  # 等二维码异步渲染
        for fr in page.frames:
            finfo: Dict[str, Any] = {"url": fr.url, "imgs": [], "canvas": 0}
            try:
                finfo["imgs"] = fr.eval_on_selector_all(
                    "img",
                    """els => els.map(e => ({
                        src: (e.src||'').slice(0,80),
                        w: e.naturalWidth||e.width||0,
                        h: e.naturalHeight||e.height||0,
                        cls: e.className||'', id: e.id||''
                    }))""",
                )
                finfo["canvas"] = fr.eval_on_selector_all("canvas", "els => els.length")
            except Exception as e:
                finfo["error"] = str(e)[:150]
            report["frames"].append(finfo)
        page.screenshot(path=os.path.join(DUMP_DIR, "login_fullpage.png"), full_page=True)
        with open(os.path.join(DUMP_DIR, "probe.json"), "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        browser.close()
    print("PROBE_DONE")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "login"
    channel_name = sys.argv[2] if len(sys.argv) > 2 else None
    if mode == "login":
        # 为指定视频号扫码授权（不同微信账号互相隔离，存到该号专属会话文件）。
        # 用法：login "视频号名称"   不带名字则存到遗留默认会话（向后兼容）。
        if channel_name:
            cid = _resolve_channel_id(channel_name)
            if cid is None:
                print(f"数据库未找到视频号「{channel_name}」。请先在网页『视频号管理』添加该号，再来授权。")
            else:
                sp = session_path_for(cid)
                headed = (len(sys.argv) > 3 and sys.argv[3] == "headed")
                print(f"开始为视频号「{channel_name}」(id={cid}) 扫码授权，请用【该号绑定的微信】扫码。")
                ok = login(session_path=sp, headed=headed)
                if ok:
                    print(f"OK 「{channel_name}」授权成功！现在可在网页点该号『采集最新』采集。")
                else:
                    print(f"FAIL 「{channel_name}」授权未完成（超时或未扫码），请重试。")
        else:
            login()
    elif mode == "probe":
        probe()
    elif mode in ("lc", "cc"):
        # lc = 扫码即采集；cc = 复用已保存会话采集（不扫码）。两者均入库+审核+上看板。
        if mode == "lc":
            items = login_and_collect(max_videos=30, dump=True)
        else:
            items = collect(max_videos=30, headless=True, dump=True)
        print(f"采集到 {len(items)} 条内容。")
        for r in items[:10]:
            print("·", r["title"], "|", (r["caption"] or "")[:50])
        if not items:
            print("未采集到内容（可能：会话失效/未扫码，或该号在内容管理里没有可见作品）。"
                  "已导出 channels_dump/ 供诊断。")
        elif not channel_name:
            print(f"未指定视频号名称，未入库。用法：{mode} \"视频号名称\"")
        else:
            n, hit = _store_and_audit(channel_name, items)
            if n:
                print(f"已入库 {n} 条并完成审核，其中 {hit} 条命中规则。请刷新看板查看。")
    elif mode == "dump":
        # 无头导出页面结构与接口原文（规避沙箱），用于精调选择器
        results = collect(max_videos=50, headless=True, dump=True)
        print(f"解析到 {len(results)} 条内容（若为0，请把 channels_dump/ 里的文件发给开发者精调）。")
        for r in results[:5]:
            print("·", r["title"])
    elif mode == "collect":
        results = collect(max_videos=50, headless=True, dump=False)
        print(f"采集到 {len(results)} 条内容：")
        for r in results:
            print("·", r["title"], "|", (r["caption"] or "")[:40])
