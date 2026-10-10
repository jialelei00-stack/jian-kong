"""微信视频号内容合规管控 —— 后端 API。

采集源通过环境变量切换：
    JIANKONG_USE_MOCK=1  使用模拟数据源（默认，可立即跑通演示）
    JIANKONG_USE_MOCK=0  使用 Appium 真机采集（需配置真机环境）
"""
import os
from datetime import datetime
# 加载 .env 文件中的环境变量（COS密钥等）
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    # dotenv 未安装时手动读取 .env
    _env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(_env_path):
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and "=" in _line and not _line.startswith("#"):
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
import json
import re
import sqlite3
import threading
import asyncio
import secrets
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

import database
import seed
import rules_engine
import ai_auditor
import auth
import queue_manager
import cos_service
import base64
from routers import auth_router, submissions_router, admin_router, notifications_router, usage_router, campaign_router
from models import ChannelIn, ChannelCategoryIn, RuleIn, ContentIn, ModelConfigIn, \
    BrandRefLearnIn

app = FastAPI(title="南孚在地营销", docs_url=None, redoc_url=None, openapi_url=None)

# ── 挂载模块化路由 ──
app.include_router(auth_router)
app.include_router(submissions_router)
app.include_router(admin_router)
app.include_router(notifications_router)
app.include_router(usage_router)
app.include_router(campaign_router)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
MEDIA_DIR = os.path.join(os.path.dirname(__file__), "media")
os.makedirs(MEDIA_DIR, exist_ok=True)
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# ── 简易访问保护：JIANKONG_PASSWORD 环境变量不为空时强制密码登录 ──
ACCESS_PASSWORD = os.getenv("JIANKONG_PASSWORD", "").strip()


@app.middleware("http")
async def password_middleware(request: Request, call_next):
    if not ACCESS_PASSWORD:
        return await call_next(request)
    path = request.url.path
    # 放行登录页和静态资源
    if path == "/login" or path.startswith("/static") or path.startswith("/media"):
        return await call_next(request)
    token = request.cookies.get("jiankong_token", "")
    if token and secrets.compare_digest(token, ACCESS_PASSWORD):
        return await call_next(request)
    if request.method == "POST" and path == "/login":
        try:
            body = await request.json()
            pwd = (body.get("pwd") or "").strip()
            if secrets.compare_digest(pwd, ACCESS_PASSWORD):
                resp = JSONResponse({"ok": True})
                resp.set_cookie("jiankong_token", ACCESS_PASSWORD, httponly=True, max_age=86400 * 7, samesite="lax")
                return resp
            return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
        except Exception:
            return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    # API 返回 401，页面重定向到登录页
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


# ── 安全响应头中间件（R-10） ──
# 统一注入 4 个 OWASP 推荐的响应头；不影响业务。
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # 最小化 CSP：仅允许同源资源（前端已全部内联/同源）
    if "Content-Security-Policy" not in response.headers:
        # img-src / media-src 放开腾讯云 COS 域名：视频/图片走 COS 预签名 URL，
        # 不放开则浏览器阻止跨域媒体加载 → 详情页视频出不来。
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob: https://*.myqcloud.com; "
            "media-src 'self' blob: https://*.myqcloud.com; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
    return response


# ── 限速中间件（滑窗，内存计数器） ──
# 设计原则：阈值按"未来增长"而非当前观察定，留足余量；
# 同 IP 维度优先，登录失败另带 username 维度；白名单接口不计。
from runtime_utils import client_ip, hit_rl, login_locked, normalize_region, REGIONS as _REGIONS


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method
    ip = client_ip(request)

    # 白名单：静态资源、/login 页面、/api/regions（前端频繁拉）
    if (path.startswith("/static") or path.startswith("/media")
            or path == "/" or path.endswith(".html") or path.endswith(".js")
            or path.endswith(".css") or path == "/api/regions"):
        return await call_next(request)

    # 登录/注册/游客：5 分钟 ≤10 次；登录额外做失败锁定
    if method == "POST" and path in (
            "/api/auth/login", "/api/auth/register", "/api/auth/guest"):
        if path == "/api/auth/login":
            try:
                body = await request.json()
                uname = (body.get("username") or "").strip()
            except Exception:
                uname = ""
            if uname and login_locked(ip, uname):
                return JSONResponse(
                    {"detail": "登录失败次数过多，请 5 分钟后再试"},
                    status_code=429,
                )
        if not hit_rl(f"auth:{ip}", limit=10, window_s=300):
            return JSONResponse({"detail": "操作过于频繁，请稍后再试"},
                                status_code=429)
        return await call_next(request)

    # 上传：1 分钟 ≤30 次（同 IP）
    if method == "POST" and path == "/api/submissions/upload":
        if not hit_rl(f"upload:{ip}", limit=30, window_s=60):
            return JSONResponse({"detail": "上传过于频繁，请稍后再试"},
                                status_code=429)
        return await call_next(request)

    # AI 推荐：1 分钟 ≤6 次；同一 submission_id 24h ≤5 次
    if method == "POST" and path.endswith("/recommend-track"):
        if not hit_rl(f"rec:{ip}", limit=6, window_s=60):
            return JSONResponse({"detail": "AI 推荐调用过于频繁，请稍后再试"},
                                status_code=429)
        m_id = re.search(r"/api/submissions/(\d+)/recommend-track", path)
        if m_id:
            sid = m_id.group(1)
            if not hit_rl(f"rec:sid:{sid}", limit=5, window_s=86400):
                return JSONResponse(
                    {"detail": "同一提交 AI 推荐已达上限（5 次/天）"},
                    status_code=429,
                )
        return await call_next(request)

    # 其他 API：1 秒 ≤20 次（防刷、扛展厅讲解连续点击）
    if path.startswith("/api/"):
        if not hit_rl(f"api:{ip}", limit=20, window_s=1):
            return JSONResponse({"detail": "请求过于频繁"},
                                status_code=429)
    return await call_next(request)


# ── 统一认证中间件：注入用户 + API 拦截 ──
_API_PUBLIC = {"/api/auth/login", "/api/auth/register", "/api/auth/guest", "/api/regions", "/api/submissions/upload", "/api/campaign/tracks", "/api/campaign/pending-by-name", "/api/campaign/submit-by-code", "/api/campaign/credentials/upload", "/api/campaign/ocr-metrics"}
# 支持 guest 访问的 API（通过 guest_session cookie，不需要登录）
_GUEST_API_PREFIXES = ("/api/submissions", "/api/notifications", "/api/dashboard/stats", "/api/queue/status")


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    """禁止浏览器缓存 HTML 页面，防止旧版页面残留。

    JS/CSS 不在这里压 no-store：前端静态资源由 nginx 统一发缓存头
    （带内容哈希的 app.<hash>.js 走 immutable 长缓存，其余靠 ?v=N 更新），
    这里再叠一层 no-store 会把它盖掉，等于每次开页面都重下 1.2MB。
    """
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".html") or path in ("/", "/user", "/manager"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

@app.middleware("http")
async def api_auth_middleware(request: Request, call_next):
    """注入用户信息到 request.state，并对 API 路由拦截未登录请求。"""
    path = request.url.path

    # 注入用户
    token = request.cookies.get(auth.COOKIE_NAME, "") or ""
    request.state.user = auth.validate_token(token) if token else None

    # API 路由拦截（公开接口除外）
    if path.startswith("/api/"):
        if path not in _API_PUBLIC:
            # 兼容旧密码模式
            if ACCESS_PASSWORD and request.cookies.get("jiankong_token") == ACCESS_PASSWORD:
                return await call_next(request)
            # 支持 guest_session cookie 访问的接口
            is_guest_api = any(path.startswith(p) for p in _GUEST_API_PREFIXES)
            if is_guest_api:
                # guest API 无论有没有 guest_session 都放行，路由函数自行处理
                return await call_next(request)
            if not request.state.user:
                return JSONResponse({"error": "unauthorized"}, status_code=401)

    return await call_next(request)


# ---------------- 工具 ----------------
def rows(cur) -> List[dict]:
    return [dict(r) for r in cur.fetchall()]


def get_collector():
    """采集源由环境变量 JIANKONG_COLLECTOR 决定：
        channels（默认）—— 视频号助手后台（Playwright，需先扫码登录保存会话）
        appium          —— 手机微信 App 自动化
        mock            —— 不返回任何内容（占位）
    """
    src = os.getenv("JIANKONG_COLLECTOR", "channels")
    if src == "appium":
        from collector import appium_collector
        return appium_collector
    if src == "mock":
        from collector import mock_collector
        return mock_collector
    from collector import channels_collector
    return channels_collector


def _load_enabled_rules(cur) -> List[dict]:
    cur.execute("SELECT * FROM rules WHERE enabled = 1")
    return rows(cur)


from rules_engine import analyze_content as _rules_analyze


# 「无任何…违规元素」「未出现…违规情形」这类否定表述里的关键词不构成违规，
# 若不做处理，「合规，无违规」的结论会被误判为 red。先整段剔除否定从句再判级。
_NEGATED_RISK_RE = re.compile(
    r"(?:无|未|没有|不存在|不涉及|未出现|未命中|未发现|未触发|不含|非|勿)"
    r"[^，。；！？,;!?\n]{0,40}"
    r"(?:违规|违反|不合规|风险|问题|禁止|严重|错误|错别字|负面|虚假)"
)


def _ai_conclusion_to_risk(conclusion: str) -> str:
    """将 AI 结论文字映射为风险等级 red/yellow/green/none"""
    if not conclusion:
        return "none"
    c = conclusion.strip()
    c = _NEGATED_RISK_RE.sub("", c)
    # 违规类
    if any(kw in c for kw in ["违规", "不合规", "违反", "禁止", "严重"]):
        return "red"
    # 疑似类
    if any(kw in c for kw in ["疑似", "风险", "待确认", "可能", "存疑"]):
        return "yellow"
    # 合规类
    return "green"


def _analyze_and_store(cur, content_id: int, content: dict, rule_list: List[dict], skip_ai: bool = False):
    # 若已配置并启用第三方大模型，则把"文案 + 画面图片"交给模型解读并审核：
    # 模型的解读与结论存入 ai_review（供看板直观展示 AI 分析过程），
    # 模型命中的违规与本地关键词规则结果合并，计入风险等级。
    # skip_ai=True：采集时跳过 AI（耗时），留给后台线程异步补充。
    ai_review = None
    hook = None
    if not skip_ai and ai_auditor.is_ready():
        detail = ai_auditor.audit(content, rule_list, ai_auditor.load_config())
        ai_review = {
            "used": True,
            "model": detail.get("model"),
            "image_count": detail.get("image_count", 0),
            "video_count": detail.get("video_count", 0),
            "is_video": detail.get("is_video", False),
            "interpretation": detail.get("interpretation", ""),
            "conclusion": detail.get("conclusion", ""),
            "error": detail.get("error"),
            "violations": detail.get("violations") or [],
        }
        ai_violations = detail.get("violations") or []
        hook = lambda c, r, _av=ai_violations: _av
    result = rules_engine.analyze_content(content, rule_list, semantic_hook=hook)
    # 最终状态只看过滤后的真实违规列表；没有违规时清掉旧风险，保持状态为合规。
    if not result["matched_rules"]:
        result["risk_level"] = "none"
        result["status"] = "compliant"
        result["summary"] = "未命中任何合规规则，内容合规。"
    # 当关键词规则未命中（risk_level=none）但 AI 已有结论时，用 AI 结论映射风险等级
    if result["risk_level"] == "none" and result["matched_rules"] and ai_review and ai_review.get("conclusion"):
        result["risk_level"] = _ai_conclusion_to_risk(ai_review["conclusion"])
        if result["risk_level"] != "none":
            result["status"] = "violation" if result["risk_level"] == "red" else "compliant"
    cur.execute(
        """INSERT INTO analysis_results (content_id, status, risk_level, matched_rules, summary, ai_review)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(content_id) DO UPDATE SET
             status=excluded.status, risk_level=excluded.risk_level,
             matched_rules=excluded.matched_rules, summary=excluded.summary,
             ai_review=excluded.ai_review,
             analyzed_at=datetime('now','localtime')""",
        (
            content_id,
            result["status"],
            result["risk_level"],
            rules_engine.serialize_matched(result["matched_rules"]),
            result["summary"],
            json.dumps(ai_review, ensure_ascii=False) if ai_review else None,
        ),
    )


def _background_download_and_ai(channel_id: int, new_items: list):
    """后台线程：先下载所有视频到本地，再跑 AI 审核。确保 AI 拿到完整视频而非封面。"""
    try:
        database.init_db()

        # 1. 先下载所有视频（AI 需要本地文件）
        try:
            from collector import channels_collector
            sp = channels_collector.session_path_for(channel_id)
            cookies = channels_collector._load_cookies(sp) if sp else None
            for content_id, item in new_items:
                media_list = item.get("media") or []
                updated = False
                for m in media_list:
                    if m.get("is_video") and not m.get("video_local") and m.get("orig"):
                        try:
                            vlocal = channels_collector._download_video(m["orig"], m.get("play_len", 0), cookies)
                            if vlocal:
                                m["video_local"] = vlocal
                                updated = True
                        except Exception as e:
                            print(f"[bg-download] #{content_id} 视频下载失败: {e}", flush=True)
                if updated:
                    with database.db_cursor() as cur:
                        cur.execute("UPDATE contents SET media=? WHERE id=?",
                                    (json.dumps(media_list, ensure_ascii=False), content_id))
                    print(f"[bg-download] #{content_id} 视频下载完成", flush=True)
        except Exception as e:
            print(f"[bg-download] 视频下载阶段异常: {e}", flush=True)

        # 2. 下载完成后再跑 AI 分析（此时 video_local 已就绪）
        if ai_auditor.is_ready():
            new_ids = [nid for nid, _ in new_items]
            print(f"[bg-download] 视频下载完毕，开始 AI 分析 {len(new_ids)} 条内容", flush=True)
            _background_ai_analysis(channel_id, new_ids)
    except Exception as e:
        print(f"[bg-download] 后台异常: {e}", flush=True)


def _background_ai_analysis(channel_id: int, new_content_ids: list = None):
    """后台线程：对指定视频号的内容逐条跑 AI 分析并更新结果。

    如果传入 new_content_ids，只分析这些新内容；否则分析该频道所有待分析内容。
    """
    try:
        database.init_db()
        with database.db_cursor() as cur:
            cur.execute("SELECT * FROM rules WHERE enabled=1")
            rule_list = rows(cur)
            if new_content_ids:
                placeholders = ",".join("?" * len(new_content_ids))
                cur.execute(
                    f"SELECT * FROM contents WHERE id IN ({placeholders})",
                    new_content_ids,
                )
            else:
                cur.execute(
                    "SELECT * FROM contents WHERE channel_id=? ORDER BY id", (channel_id,)
                )
            items = rows(cur)
        if not items:
            return
        cfg = ai_auditor.load_config()
        for item in items:
            content_id = item["id"]
            # 跳过已有 AI 审核结果的内容（记忆机制：已审核的不重复跑）
            # 但如果之前审核失败（有 error），允许重试
            with database.db_cursor() as cur:
                cur.execute("SELECT ai_review FROM analysis_results WHERE content_id=?", (content_id,))
                row = cur.fetchone()
                if row and row["ai_review"]:
                    ai_data = json.loads(row["ai_review"]) if isinstance(row["ai_review"], str) else row["ai_review"]
                    if ai_data.get("used") and not ai_data.get("error"):
                        print(f"[bg-ai] #{content_id} 已有AI审核结果，跳过", flush=True)
                        continue
            content = {
                "caption": item.get("caption") or "",
                "transcript": item.get("transcript") or "",
                "ocr_text": item.get("ocr_text") or "",
                "media": item.get("media") or [],
                "_channel_id": channel_id,
            }
            detail = ai_auditor.audit(content, rule_list, cfg)
            ai_review = {
                "used": True,
                "model": detail.get("model"),
                "image_count": detail.get("image_count", 0),
                "video_count": detail.get("video_count", 0),
                "is_video": detail.get("is_video", False),
                "interpretation": detail.get("interpretation", ""),
                "conclusion": detail.get("conclusion", ""),
                "error": detail.get("error"),
                "violations": detail.get("violations") or [],
            }
            ai_violations = detail.get("violations") or []
            hook = lambda c, r, _av=ai_violations: _av
            result = rules_engine.analyze_content(content, rule_list, semantic_hook=hook)
            # 最终状态只看过滤后的真实违规列表；没有违规时清掉旧风险。
            if not result["matched_rules"]:
                result["risk_level"] = "none"
                result["status"] = "compliant"
                result["summary"] = "未命中任何合规规则，内容合规。"
            # 当关键词规则未命中但 AI 已有结论时，用 AI 结论映射风险等级
            if result["risk_level"] == "none" and result["matched_rules"] and ai_review.get("conclusion"):
                result["risk_level"] = _ai_conclusion_to_risk(ai_review["conclusion"])
                if result["risk_level"] != "none":
                    result["status"] = "violation" if result["risk_level"] == "red" else "compliant"
            with database.db_cursor() as cur:
                cur.execute(
                    """UPDATE analysis_results SET status=?, risk_level=?, matched_rules=?, summary=?, ai_review=?,
                       analyzed_at=datetime('now','localtime') WHERE content_id=?""",
                    (
                        result["status"], result["risk_level"],
                        rules_engine.serialize_matched(result["matched_rules"]),
                        result["summary"],
                        json.dumps(ai_review, ensure_ascii=False),
                        content_id,
                    ),
                )
            print(f"[bg-ai] #{content_id} 分析完成", flush=True)
        print(f"[bg-ai] 视频号 #{channel_id} 全部 AI 分析完成", flush=True)
    except Exception as e:
        print(f"[bg-ai] 后台分析异常: {e}", flush=True)


def run_collection(channel_id: int):
    """采集指定视频号 -> 存内容 -> 逐条合规分析 -> 存结果。"""
    with database.db_cursor() as cur:
        cur.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        ch = cur.fetchone()
        if not ch:
            raise HTTPException(404, "视频号不存在")
        channel_name = ch["name"]

        cur.execute("UPDATE channels SET status='collecting' WHERE id=?", (channel_id,))

    # 采集（可能耗时，放在事务外）
    collector = get_collector()
    try:
        items = collector.collect_channel(channel_name, max_videos=0)
        status = "done"
    except Exception as e:
        with database.db_cursor() as cur:
            cur.execute("UPDATE channels SET status='error' WHERE id=?", (channel_id,))
        raise HTTPException(500, f"采集失败：{e}")

    # 采集到 0 条：极可能是登录态（授权）已失效，而非该号真的没内容。
    # 此时绝不清空已有内容（避免误删数据），标记为需重新授权并提示用户。
    if not items:
        with database.db_cursor() as cur:
            cur.execute("UPDATE channels SET status='error' WHERE id=?", (channel_id,))
        raise HTTPException(
            409,
            "采集到 0 条内容，登录态可能已失效（已保留原有内容未清空）。"
            "请到看板顶部检查授权状态并重新扫码授权后再采集。",
        )

    with database.db_cursor() as cur:
        rule_list = _load_enabled_rules(cur)
        # 重新采集：保留已有审核结果（记忆机制）
        # 1. 读取旧内容的 object_id → 旧 content_id 映射
        cur.execute("SELECT id, object_id FROM contents WHERE channel_id=?", (channel_id,))
        old_map = {}  # object_id → old_content_id
        old_no_oid = []  # 没有 object_id 的旧内容 ID
        for r in cur.fetchall():
            oid = r["object_id"]
            if oid:
                old_map[oid] = r["id"]
            else:
                old_no_oid.append(r["id"])
        # 2. 对于没有 object_id 的旧内容，用 (title, publish_time) 作为备选匹配键
        old_title_map = {}  # (title, publish_time) → old_content_id
        if old_no_oid:
            placeholders = ",".join("?" * len(old_no_oid))
            cur.execute(
                f"SELECT id, title, publish_time FROM contents WHERE id IN ({placeholders})",
                old_no_oid,
            )
            for r in cur.fetchall():
                key = (r["title"] or "", r["publish_time"] or "")
                if key[0] or key[1]:
                    old_title_map[key] = r["id"]

        # 3. 清空旧内容（级联删除旧分析结果——但先保存需要保留的）
        # 保存已有人工覆盖 + AI 分析结果
        cur.execute("SELECT ar.* FROM analysis_results ar JOIN contents c ON ar.content_id=c.id WHERE c.channel_id=?", (channel_id,))
        saved_results = {}
        for r in cur.fetchall():
            cid = r["content_id"]
            saved_results[cid] = dict(r)

        cur.execute("DELETE FROM contents WHERE channel_id=?", (channel_id,))

        # 4. 插入新内容，匹配旧审核结果
        new_items = []
        for item in items:
            object_id = item.get("object_id", "")
            cur.execute(
                """INSERT INTO contents
                   (channel_id, object_id, title, video_url, caption, transcript, ocr_text, publish_time, media, stats)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    channel_id, object_id, item.get("title"), item.get("video_url"),
                    item.get("caption"), item.get("transcript"),
                    item.get("ocr_text"), item.get("publish_time"),
                    json.dumps(item.get("media") or [], ensure_ascii=False),
                    json.dumps(item.get("stats") or {}, ensure_ascii=False),
                ),
            )
            new_id = cur.lastrowid

            # 尝试匹配旧审核结果
            old_id = None
            if object_id and object_id in old_map:
                old_id = old_map[object_id]
            else:
                key = (item.get("title") or "", item.get("publish_time") or "")
                if key in old_title_map:
                    old_id = old_title_map[key]

            if old_id and old_id in saved_results:
                # 恢复旧审核结果（包括人工覆盖）
                sr = saved_results[old_id]
                cur.execute(
                    """UPDATE analysis_results SET status=?, risk_level=?, matched_rules=?,
                       summary=?, ai_review=?, manual_status=?,
                       analyzed_at=datetime('now','localtime') WHERE content_id=?""",
                    (
                        sr["status"], sr["risk_level"], sr["matched_rules"],
                        sr["summary"], sr.get("ai_review"), sr.get("manual_status"),
                        new_id,
                    ),
                )
            else:
                # 新内容：走关键词审核（AI 后台异步）
                _analyze_and_store(cur, new_id, item, rule_list, skip_ai=True)
                new_items.append((new_id, item))

        cur.execute(
            "UPDATE channels SET status=?, last_collected_at=datetime('now','localtime') WHERE id=?",
            (status, channel_id),
        )
    # 采集完成后，后台异步：1. 下载视频 2. 跑 AI 分析
    if new_items:
        threading.Thread(target=_background_download_and_ai, args=(channel_id, new_items), daemon=True).start()
    return len(items)


# ---------------- 视频代理 ----------------
@app.get("/api/video_proxy/{content_id}")
def video_proxy(content_id: int, request: Request):
    """代理视频播放：优先用本地已下载的文件，否则从微信 CDN 流式转发。

    前端用此接口替代直接访问 CDN（CDN 需要登录态，浏览器无法直接访问）。
    支持 Range 请求以实现视频拖动进度条。"""
    auth.require_user(request)
    with database.db_cursor() as cur:
        cur.execute("SELECT media, channel_id FROM contents WHERE id=?", (content_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "内容不存在")
    media = json.loads(row["media"] or "[]")
    channel_id = row["channel_id"]

    # 1. 优先用本地已下载的视频文件（支持 Range 请求，视频可拖动进度条）
    for m in media:
        if m.get("is_video") and m.get("video_local"):
            vpath = os.path.join(MEDIA_DIR, m["video_local"].split("/media/")[-1] if "/media/" in m["video_local"] else os.path.basename(m["video_local"]))
            if os.path.exists(vpath) and os.path.getsize(vpath) >= 1024:
                file_size = os.path.getsize(vpath)
                range_header = request.headers.get("range")
                if range_header:
                    # 解析 Range: bytes=start-end
                    import re
                    match = re.match(r'bytes=(\d+)-(\d*)', range_header)
                    if match:
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else file_size - 1
                        end = min(end, file_size - 1)
                        content_length = end - start + 1

                        def file_stream(path, s, e):
                            with open(path, "rb") as f:
                                f.seek(s)
                                remaining = e - s + 1
                                while remaining > 0:
                                    chunk = f.read(min(65536, remaining))
                                    if not chunk:
                                        break
                                    remaining -= len(chunk)
                                    yield chunk

                        from fastapi.responses import StreamingResponse
                        return StreamingResponse(
                            file_stream(vpath, start, end),
                            status_code=206,
                            headers={
                                "content-type": "video/mp4",
                                "content-length": str(content_length),
                                "content-range": f"bytes {start}-{end}/{file_size}",
                                "accept-ranges": "bytes",
                            },
                            media_type="video/mp4",
                        )
                # 无 Range 请求：返回完整文件
                return FileResponse(vpath, media_type="video/mp4", headers={"accept-ranges": "bytes", "content-length": str(file_size)})

    # 2. 本地无文件，从 CDN 代理
    video_url = None
    for m in media:
        if m.get("is_video"):
            video_url = m.get("orig") or m.get("video_url") or ""
            break
    if not video_url:
        raise HTTPException(404, "该内容无视频")

    # 加载该视频号专属的会话 cookie
    from collector import channels_collector
    sp = channels_collector.session_path_for(channel_id)
    cookies = channels_collector._load_cookies(sp) if sp else None

    import urllib3
    urllib3.disable_warnings()
    sess = __import__("requests").Session()
    if cookies:
        for ck in cookies:
            sess.cookies.set(
                ck.get("name", ""), ck.get("value", ""),
                domain=ck.get("domain"), path=ck.get("path"),
            )
    # 传递 Range 头以支持拖动进度条
    headers = {"User-Agent": channels_collector._HTTP_UA}
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header

    try:
        resp = sess.get(video_url, headers=headers, timeout=120, stream=True, verify=False)
    except Exception as e:
        raise HTTPException(502, f"CDN 请求失败: {e}")

    if resp.status_code not in (200, 206):
        raise HTTPException(502, f"CDN 返回 {resp.status_code}")

    ct = resp.headers.get("content-type", "video/mp4")
    cl = resp.headers.get("content-length")
    cr = resp.headers.get("content-range")
    resp_headers = {"content-type": ct, "accept-ranges": "bytes"}
    if cl:
        resp_headers["content-length"] = cl
    if cr:
        resp_headers["content-range"] = cr

    from fastapi.responses import StreamingResponse
    status = resp.status_code
    return StreamingResponse(
        resp.iter_content(chunk_size=65536),
        status_code=status,
        headers=resp_headers,
        media_type=ct,
    )


# ---------------- 启动 ----------------
@app.on_event("startup")
async def on_startup():
    # 关键依赖自检（R-18）：缺则在日志明确告警，但**不阻塞启动**，
    # 让缺依赖的功能自然降级（前端已有「AI 未就绪」提示）。
    import logging as _logging
    _logger = _logging.getLogger("startup")
    for _name in ("openai", "multipart", "starlette", "fastapi", "uvicorn"):
        try:
            __import__(_name)
        except Exception as _e:
            _logger.critical("[startup] 关键依赖缺失 %s: %s —— AI 审核/上传可能降级",
                             _name, _e)
    database.init_db()
    seed.seed_if_empty()
    queue_manager.start_workers()
    print("[startup] 数据库初始化完成，队列就绪。", flush=True)


# ---------------- 视频号 ----------------
@app.get("/api/channels")
def list_channels(req: Request):
    auth.require_manager(req)
    with database.db_cursor() as cur:
        cur.execute(
            """
            SELECT c.*,
                   (SELECT COUNT(*) FROM contents ct WHERE ct.channel_id=c.id) AS content_count,
                   (SELECT COUNT(*) FROM contents ct
                      JOIN analysis_results ar ON ar.content_id=ct.id
                      WHERE ct.channel_id=c.id AND ar.status='violation') AS violation_count
            FROM channels c ORDER BY c.id
            """
        )
        data = rows(cur)
    # 为每个视频号附带其专属登录态的授权状态（轻量：仅读会话文件，不开浏览器）
    from collector import channels_collector
    for c in data:
        try:
            st = channels_collector.auth_status(
                session_path=channels_collector.session_path_for(c["id"]))
            c["authorized"] = st["authorized"]
            c["auth_days_left"] = st.get("cookie_days_left")
        except Exception:
            c["authorized"] = False
            c["auth_days_left"] = None
        c["active"] = c["authorized"]  # 有授权 = 活跃监控中
    return data


@app.post("/api/channels")
def add_channel(req: Request, payload: ChannelIn):
    auth.require_manager(req)
    with database.db_cursor() as cur:
        try:
            cur.execute(
                "INSERT INTO channels (name, wechat_id, region, owner) VALUES (?, ?, ?, ?)",
                (payload.name, payload.wechat_id, payload.region, payload.owner),
            )
            return {"id": cur.lastrowid}
        except sqlite3.IntegrityError:
            raise HTTPException(400, "该视频号已存在")


@app.put("/api/channels/{channel_id}/category")
def update_channel_category(req: Request, channel_id: int, payload: ChannelCategoryIn):
    """更新视频号的自定义分类（区域 / 账号归属者）。空字符串按清空处理。"""
    auth.require_manager(req)
    region = (payload.region or "").strip() or None
    owner = (payload.owner or "").strip() or None
    with database.db_cursor() as cur:
        cur.execute("UPDATE channels SET region=?, owner=? WHERE id=?",
                    (region, owner, channel_id))
    return {"ok": True, "region": region, "owner": owner}


# ---- 网页内嵌扫码授权（云端/本地通用，无需终端命令） ----
_AUTH_SESSIONS: dict = {}          # channel_id -> {"state": {...}, "thread": Thread}
_AUTH_LOCK = threading.Lock()


@app.post("/api/channels/{channel_id}/auth/start")
def auth_start(req: Request, channel_id: int):
    """发起该视频号的扫码授权：后台起线程拉起登录会话并持续刷新二维码。
    前端随后轮询 /auth/status 取实时二维码与结果。"""
    auth.require_manager(req)
    from collector import channels_collector
    with database.db_cursor() as cur:
        cur.execute("SELECT name FROM channels WHERE id=?", (channel_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "视频号不存在")
    sp = channels_collector.session_path_for(channel_id)
    with _AUTH_LOCK:
        sess = _AUTH_SESSIONS.get(channel_id)
        if sess and sess["thread"].is_alive() and sess["state"].get("status") in ("pending", "scanned", "selecting"):
            return {"ok": True, "reused": True, "channel_name": row["name"]}
        state = {"status": "pending", "qrcode": None, "message": "", "cancel": False}
        t = threading.Thread(
            target=channels_collector.web_auth_worker,
            args=(sp, state, 240), kwargs={"channel_name": row["name"]}, daemon=True,
        )
        t.start()
        _AUTH_SESSIONS[channel_id] = {"state": state, "thread": t}
    return {"ok": True, "channel_name": row["name"]}


@app.get("/api/channels/{channel_id}/auth/status")
def auth_poll(req: Request, channel_id: int):
    """前端轮询：返回最新二维码(data:image base64)与授权状态。
    status: idle/pending/scanned/selecting/success/timeout/failed。
    当 status=selecting 时额外返回 channel_options 供前端展示频道选择列表。"""
    auth.require_manager(req)
    sess = _AUTH_SESSIONS.get(channel_id)
    if not sess:
        return {"status": "idle", "qrcode": None, "message": ""}
    st = sess["state"]
    return {"status": st.get("status"), "qrcode": st.get("qrcode"),
            "message": st.get("message", ""),
            "channel_options": st.get("channel_options"),
            "page_url": st.get("page_url"), "page_title": st.get("page_title"),
            "page_text": st.get("page_text"), "_need_channel_select": st.get("_need_channel_select")}


@app.post("/api/channels/{channel_id}/auth/select")
async def auth_select(req: Request, channel_id: int):
    """在授权过程中，当检测到多个可选视频号时，前端选择其中一个。
    将选中的频道名写入 state['_selected_channel']，auth worker 主循环会拾取并点击。"""
    auth.require_manager(req)
    channel_name = None
    try:
        body = await req.json()
        channel_name = body.get("channel_name")
    except Exception:
        pass
    if not channel_name:
        raise HTTPException(400, "缺少 channel_name")
    sess = _AUTH_SESSIONS.get(channel_id)
    if not sess:
        raise HTTPException(404, "授权会话不存在")
    sess["state"]["_selected_channel"] = channel_name
    return {"ok": True, "selected": channel_name}


@app.post("/api/channels/{channel_id}/switch_channel")
def switch_channel(req: Request, channel_id: int, channel_name: str = None):
    """授权完成后，切换到指定的视频号。用已保存的 session 打开平台，
    调用 _select_channel 切换，然后重新保存 session。"""
    auth.require_manager(req)
    if not channel_name:
        raise HTTPException(400, "缺少 channel_name")
    from collector import channels_collector
    sp = channels_collector.session_path_for(channel_id)
    if not os.path.exists(sp):
        raise HTTPException(404, "会话文件不存在，请先授权")
    try:
        success = channels_collector.switch_and_resave(sp, channel_name)
        if success:
            return {"ok": True, "channel": channel_name}
        else:
            return {"ok": False, "detail": f"未能切换到频道「{channel_name}」"}
    except Exception as e:
        raise HTTPException(500, f"切换失败: {e}")


@app.post("/api/channels/{channel_id}/auth/cancel")
def auth_cancel(req: Request, channel_id: int):
    """取消正在进行的授权会话并清理。"""
    auth.require_manager(req)
    with _AUTH_LOCK:
        sess = _AUTH_SESSIONS.pop(channel_id, None)
    if sess:
        sess["state"]["cancel"] = True
    return {"ok": True}


@app.delete("/api/channels/{channel_id}")
def delete_channel(req: Request, channel_id: int):
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute("DELETE FROM channels WHERE id=?", (channel_id,))
    return {"ok": True}


@app.post("/api/channels/{channel_id}/collect")
async def collect(req: Request, channel_id: int):
    auth.require_manager(req)
    count = await asyncio.to_thread(run_collection, channel_id)
    return {"ok": True, "collected": count}


@app.get("/api/channels/{channel_id}/contents")
def channel_contents(req: Request, channel_id: int, page: int = 1, page_size: int = 50):
    """分页返回某视频号的内容（含审核结果），默认每页50条。"""
    auth.require_manager(req)
    offset = (page - 1) * page_size
    with database.db_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM contents WHERE channel_id=?", (channel_id,))
        total = cur.fetchone()["c"]
        cur.execute(
            """SELECT ct.*, ar.status, ar.risk_level, ar.matched_rules, ar.summary, ar.ai_review
               FROM contents ct LEFT JOIN analysis_results ar ON ar.content_id=ct.id
               WHERE ct.channel_id=? ORDER BY ct.id DESC LIMIT ? OFFSET ?""",
            (channel_id, page_size, offset),
        )
        data = rows(cur)
    for d in data:
        d["matched_rules"] = json.loads(d["matched_rules"]) if d.get("matched_rules") else []
        d["media"] = json.loads(d["media"]) if d.get("media") else []
        d["stats"] = json.loads(d["stats"]) if d.get("stats") else {}
        d["ai_review"] = json.loads(d["ai_review"]) if d.get("ai_review") else None
        # 提取首张缩略图供前端卡片展示
        d["thumb_url"] = d["media"][0]["thumb"] if d.get("media") and d["media"][0].get("thumb") else ""
        d["is_video"] = d["media"][0].get("is_video", False) if d.get("media") else False
    return {"items": data, "total": total, "page": page, "page_size": page_size}


@app.get("/api/contents/all")
def all_contents(req: Request, page: int = 1, page_size: int = 50, channel_id: int = None,
                 status: str = None, risk_level: str = None, region: str = None,
                 sort_by: str = None, sort_order: str = "desc", search: str = None):
    """分页返回全部已采集内容（带视频号名与审核结果），支持筛选与排序。

    参数:
    page/page_size: 分页参数
    channel_id: 按视频号筛选
    status: 按审核状态筛选 (violation/compliant)
    risk_level: 按风险等级筛选
    region: 按区域筛选（支持大区或细分区域，LIKE 匹配）
    sort_by: read(观看量), like(点赞), publish_time(发布时间), id(默认)
    sort_order: asc/desc
    search: 搜索关键词（匹配 caption + 视频号名）
    """
    auth.require_user(req)
    offset = (page - 1) * page_size
    where_clauses = []
    params = []
    if channel_id:
        where_clauses.append("ct.channel_id=?")
        params.append(channel_id)
    if status:
        where_clauses.append("ar.status=?")
        params.append(status)
    if risk_level:
        if risk_level == "red":
            where_clauses.append("(ar.risk_level='red' OR ar.risk_level='forbidden')")
        else:
            where_clauses.append("ar.risk_level=?")
            params.append(risk_level)
    if region:
        where_clauses.append("ch.region LIKE ?")
        params.append(f"%{region}%")
    if search:
        where_clauses.append("(ct.caption LIKE ? OR ch.name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # 排序参数校验
    allowed_sorts = {
        "read": "json_extract(ct.stats, '$.read')",
        "like": "json_extract(ct.stats, '$.like')",
        "publish_time": "ct.publish_time",
        "id": "ct.id",
    }
    sort_col = allowed_sorts.get(sort_by, "ct.id") if sort_by else "ct.id"
    sort_dir = "ASC" if sort_order and sort_order.lower() == "asc" else "DESC"
    order_sql = f"ORDER BY {sort_col} {sort_dir} NULLS LAST"

    with database.db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM contents ct JOIN channels ch ON ch.id=ct.channel_id LEFT JOIN analysis_results ar ON ar.content_id=ct.id{where_sql}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"""SELECT ct.*, ch.name AS channel_name, ch.region, ch.owner,
                      ar.status, ar.risk_level, ar.matched_rules, ar.summary, ar.ai_review,
                      ar.manual_status
               FROM contents ct
               JOIN channels ch ON ch.id = ct.channel_id
               LEFT JOIN analysis_results ar ON ar.content_id = ct.id
               {where_sql}
               {order_sql} LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        )
        data = rows(cur)
    for d in data:
        d["matched_rules"] = json.loads(d["matched_rules"]) if d.get("matched_rules") else []
        d["media"] = json.loads(d["media"]) if d.get("media") else []
        d["stats"] = json.loads(d["stats"]) if d.get("stats") else {}
        d["ai_review"] = json.loads(d["ai_review"]) if d.get("ai_review") else None
        # 提取首张缩略图供前端卡片展示
        d["thumb_url"] = d["media"][0]["thumb"] if d.get("media") and d["media"][0].get("thumb") else ""
        d["is_video"] = d["media"][0].get("is_video", False) if d.get("media") else False
    return {"items": data, "total": total, "page": page, "page_size": page_size}


@app.get("/api/contents/export")
def export_contents(req: Request, channel_id: int = None, status: str = None, risk_level: str = None,
                      region: str = None):
    """导出全部已采集内容为 CSV 文件（应用相同筛选条件）。"""
    auth.require_user(req)
    import csv, io
    where_clauses = []
    params = []
    if channel_id:
        where_clauses.append("ct.channel_id=?")
        params.append(channel_id)
    if risk_level:
        where_clauses.append("ar.risk_level=?")
        params.append(risk_level)
    if region:
        where_clauses.append("ch.region LIKE ?")
        params.append(f"%{region}%")
    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    with database.db_cursor() as cur:
        cur.execute(
            f"""SELECT ct.caption, ct.publish_time, ct.stats, ch.name AS channel_name,
                      ch.region, ar.risk_level, ar.ai_review
               FROM contents ct
               JOIN channels ch ON ch.id = ct.channel_id
               LEFT JOIN analysis_results ar ON ar.content_id = ct.id
               {where_sql}
               ORDER BY ct.id DESC""",
            params,
        )
        data = rows(cur)

    # 生成 CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["视频号", "所属区域", "发布时间", "内容描述", "观看量", "点赞量",
                      "风险等级", "AI 结论"])
    risk_labels = {"red": "违规", "yellow": "疑似", "green": "合规", "none": "未标"}
    for d in data:
        stats = json.loads(d["stats"]) if d.get("stats") else {}
        ai = json.loads(d.get("ai_review") or "{}")
        conclusion = ai.get("conclusion", "") if isinstance(ai, dict) else ""
        writer.writerow([
            d.get("channel_name", ""),
            d.get("region", ""),
            d.get("publish_time", ""),
            (d.get("caption") or "")[:200],
            stats.get("read", 0),
            stats.get("like", 0),
            risk_labels.get(d.get("risk_level", "none"), "未标"),
            conclusion,
        ])

    csv_bytes = output.getvalue().encode("utf-8-sig")
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=contents_export.csv"},
    )


@app.get("/api/contents/{content_id}")
def get_content_detail(content_id: int):
    """单项内容详情（含所属频道及分析结果）。"""
    with database.db_cursor() as cur:
        cur.execute(
            """SELECT ct.*, ch.name AS channel_name, ch.region, ch.owner,
                      ar.status, ar.risk_level, ar.matched_rules, ar.summary, ar.ai_review,
                      ar.manual_status
               FROM contents ct
               JOIN channels ch ON ch.id = ct.channel_id
               LEFT JOIN analysis_results ar ON ar.content_id = ct.id
               WHERE ct.id = ?""",
            (content_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="内容不存在")
    d = dict(row)
    d["matched_rules"] = json.loads(d["matched_rules"]) if d.get("matched_rules") else []
    d["media"] = json.loads(d["media"]) if d.get("media") else []
    d["stats"] = json.loads(d["stats"]) if d.get("stats") else {}
    d["ai_review"] = json.loads(d["ai_review"]) if d.get("ai_review") else None
    d["thumb_url"] = d["media"][0]["thumb"] if d.get("media") and d["media"][0].get("thumb") else ""
    d["is_video"] = d["media"][0].get("is_video", False) if d.get("media") else False
    return d

@app.post("/api/contents/{content_id}/violations/{idx}/mark-false-positive")
def mark_content_violation_false_positive(content_id: int, idx: int, req: Request):
    """视频号采集内容：单条违规标误报，从 analysis_results.matched_rules 移除并重计状态。"""
    user = auth.require_manager(req)
    note = ""
    try:
        body = json.loads((req.scope.get("body") or b"").decode("utf-8") or "{}")
        note = (body.get("note") or "").strip()
    except Exception:
        pass
    with database.db_cursor() as cur:
        cur.execute("SELECT matched_rules, status, risk_level FROM analysis_results WHERE content_id=?", (content_id,))
        ar = cur.fetchone()
        if not ar:
            raise HTTPException(404, "分析结果不存在")
        ar = dict(ar)
        matched = json.loads(ar["matched_rules"]) if ar.get("matched_rules") else []
        if not (0 <= idx < len(matched)):
            raise HTTPException(400, f"违规下标 {idx} 超出范围（共 {len(matched)} 条）")
        target = dict(matched[idx])
        target["false_positive"] = True
        target["false_positive_by"] = user.get("display_name") or user.get("username", "")
        target["false_positive_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if note:
            target["false_positive_note"] = note[:500]
        matched.pop(idx)
        # 状态重算：与 recheck_typo 保持一致的口径
        if not matched:
            new_status, new_risk = "approved", "none"
        else:
            sev_rank = {"forbidden": 4, "high": 3, "medium": 2, "low": 1}
            max_sev = max((sev_rank.get(str(m.get("severity", "low")).lower(), 1) for m in matched), default=0)
            if max_sev <= 1:
                new_status, new_risk = "approved", "none"
            else:
                new_status, new_risk = "manual_review", "forbidden" if max_sev == 4 else ("high" if max_sev == 3 else "medium")
        cur.execute(
            "UPDATE analysis_results SET matched_rules=?, status=?, risk_level=? WHERE content_id=?",
            (json.dumps(matched, ensure_ascii=False), new_status, new_risk, content_id),
        )
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("违规标误报(content)", user.get("display_name") or user.get("username", ""),
             "", req.client.host if req.client else "", content_id,
             f"原违规：{target.get('rule_name')}/{target.get('matched_text','')[:60]}; 备注: {note or '无'}"),
        )
    return {"ok": True, "new_status": new_status, "new_risk": new_risk, "remaining": len(matched)}


@app.post("/api/contents/{content_id}/recheck-typo")
def recheck_content_typo(content_id: int, req: Request):
    """视频号采集内容的形近错字违规二次确认（同 /api/submissions/.../recheck-typo）。

    流程：按 needs_ocr_review 找出违规 → OCR 取证 + AI 复判 → verdict 写回 matched_rules 条目；
    丢弃的违规从列表里拿掉，状态同步下调；写 recheck_at / recheck_summary 到 analysis_results。
    """
    user = auth.require_manager(req)
    with database.db_cursor() as cur:
        cur.execute("SELECT * FROM contents WHERE id=?", (content_id,))
        c = cur.fetchone()
        if not c:
            raise HTTPException(404, "内容不存在")
        c = dict(c)
        cur.execute("SELECT matched_rules, status, risk_level FROM analysis_results WHERE content_id=?", (content_id,))
        ar = cur.fetchone()
        if not ar:
            return {"rechecks": [], "summary": {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}}
        ar = dict(ar)
        matched = json.loads(ar["matched_rules"]) if ar.get("matched_rules") else []

    media = c.get("media")
    if isinstance(media, str):
        try: media = json.loads(media)
        except: media = []
    image_urls: list = []
    for m in media or []:
        if not isinstance(m, dict):
            continue
        for k in ("frame_url", "frame_cos_url", "cos_url", "cover_url", "url", "preview_url", "thumb"):
            v = m.get(k)
            if isinstance(v, str) and v.startswith("http"):
                image_urls.append(v); break
    if not image_urls:
        return {"rechecks": [], "summary": {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}}

    full_text = " / ".join(
        t for t in (c.get("title"), c.get("caption"), c.get("ocr_text"), c.get("transcript"))
        if isinstance(t, str) and t.strip()
    )

    targets = [(i, v) for i, v in enumerate(matched)
               if v.get("needs_ocr_review") and v.get("recheck_verdict") not in ("uphold", "discard")]
    if not targets:
        return {"rechecks": [], "summary": {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}}

    rechecks, summary = [], {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}
    discard_indices: list = []
    new_status, new_risk = ar.get("status"), ar.get("risk_level")

    for idx, v in targets:
        try:
            res = ai_auditor.recheck_typo_violation(
                image_urls=image_urls,
                first_evidence=str(v.get("matched_text") or v.get("evidence") or ""),
                first_rule_name=str(v.get("rule_name") or ""),
                first_field=str(v.get("field") or "").rstrip("（AI）"),
                full_text=full_text,
            )
        except Exception as e:
            res = {"verdict": "uncertain", "reason": f"复判异常：{e}",
                   "ocr_seen": [], "standard_seen": [], "ocr_raw": [], "model": "", "error": str(e)}
        verdict = res.get("verdict", "uncertain")
        summary["total"] += 1
        summary[verdict] = summary.get(verdict, 0) + 1
        v["recheck_verdict"] = verdict
        v["recheck_reason"] = res.get("reason", "")
        v["recheck_ocr"] = res.get("ocr_text_all", "") or " / ".join(res.get("ocr_raw") or [])
        v["recheck_ocr_seen"] = res.get("ocr_seen") or []
        v["recheck_standard_seen"] = res.get("standard_seen") or []
        v["recheck_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        v["recheck_model"] = res.get("model", "")
        rechecks.append({
            "violation_index": idx, "rule_name": v.get("rule_name"),
            "verdict": verdict, "reason": v["recheck_reason"],
            "ocr_seen": v["recheck_ocr_seen"], "standard_seen": v["recheck_standard_seen"],
        })
        if verdict == "discard":
            discard_indices.append(idx)
        elif verdict == "uphold" and v.get("severity") in ("forbidden", "high"):
            if new_risk in (None, "", "none", "low", "medium"):
                new_risk = "forbidden" if v["severity"] == "forbidden" else "high"
                new_status = "violation"

    if discard_indices:
        discard_set = set(discard_indices)
        matched = [m for i, m in enumerate(matched) if i not in discard_set]
        if not matched:
            new_status, new_risk = "approved", "none"
        else:
            sev_rank = {"forbidden": 4, "high": 3, "medium": 2, "low": 1}
            max_sev = max((sev_rank.get(str(m.get("severity", "low")).lower(), 1) for m in matched), default=0)
            if max_sev <= 1:
                new_status, new_risk = "approved", "none"
            elif new_status == "violation":
                new_status = "manual_review"

    with database.db_cursor() as cur:
        cur.execute(
            """UPDATE analysis_results SET matched_rules=?, status=?, risk_level=?,
               recheck_at=datetime('now','localtime'), recheck_summary=?
               WHERE content_id=?""",
            (json.dumps(matched, ensure_ascii=False), new_status, new_risk,
             json.dumps(summary, ensure_ascii=False), content_id),
        )
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("形近错字复判(content)",
             user.get("display_name") or user.get("username", ""),
             "", req.client.host if req.client else "", content_id,
             f"summary={summary}; verdicts={[r['verdict'] for r in rechecks]}"),
        )
    return {"rechecks": rechecks, "summary": summary,
            "new_status": new_status, "new_risk": new_risk}


# ---------------- 内容审核管理（视频号采集内容）----------------
@app.get("/api/review/contents")
def review_contents(req: Request, page: int = 1, page_size: int = 20,
                    status: str = None, risk_level: str = None,
                    region: str = None, search: str = None):
    """返回视频号采集内容中需要人工复核的项目。
    数据来源: contents + analysis_results（视频号监控采集）。
    筛选维度: 审核状态(manual_status)、风险等级、区域、关键词搜索。"""
    auth.require_manager(req)
    offset = (page - 1) * page_size
    where_clauses = []
    params = []

    if status == "pending":
        # 待人工复核 = 风险等级为黄色且未被人工覆盖
        where_clauses.append("(ar.risk_level = 'yellow' OR ar.risk_level = 'medium')")
        where_clauses.append("ar.manual_status IS NULL")
    elif status == "manual_review":
        # 所有待复核（yellow + 手动设为待复核的）
        where_clauses.append("(ar.risk_level = 'yellow' OR ar.risk_level = 'medium' OR ar.manual_status = 'manual_review')")
    elif status == "reviewed":
        # 已复核
        where_clauses.append("ar.manual_status IS NOT NULL")
    elif status == "violation":
        where_clauses.append("ar.status = 'violation'")
    elif status == "compliant":
        where_clauses.append("ar.status = 'compliant'")
    elif status:
        where_clauses.append("ar.status = ?")
        params.append(status)

    if risk_level:
        if risk_level == "red":
            where_clauses.append("(ar.risk_level = 'red' OR ar.risk_level = 'forbidden')")
        else:
            where_clauses.append("ar.risk_level = ?")
            params.append(risk_level)

    if region:
        where_clauses.append("ch.region LIKE ?")
        params.append(f"%{region}%")

    if search:
        where_clauses.append("(ct.caption LIKE ? OR ch.name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    # 确保有关联数据
    where_clauses.append("ar.id IS NOT NULL")
    where_sql = " WHERE " + " AND ".join(where_clauses)

    with database.db_cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS c FROM contents ct "
            f"JOIN channels ch ON ch.id = ct.channel_id "
            f"JOIN analysis_results ar ON ar.content_id = ct.id{where_sql}", params)
        total = cur.fetchone()["c"]

        # 待审核总数（不受筛选影响，始终统计所有待复核的）
        cur.execute(
            "SELECT COUNT(*) AS c FROM contents ct "
            "JOIN channels ch ON ch.id = ct.channel_id "
            "JOIN analysis_results ar ON ar.content_id = ct.id "
            "WHERE (ar.risk_level = 'yellow' OR ar.risk_level = 'medium') "
            "AND ar.manual_status IS NULL")
        pending_count = cur.fetchone()["c"]
        cur.execute(
            f"""SELECT ct.*, ch.name AS channel_name, ch.region AS channel_region,
                       ar.status, ar.risk_level, ar.matched_rules, ar.summary, ar.manual_status
                FROM contents ct
                JOIN channels ch ON ch.id = ct.channel_id
                JOIN analysis_results ar ON ar.content_id = ct.id{where_sql}
                ORDER BY ct.id DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset],
        )
        data = rows(cur)

    for d in data:
        d["matched_rules"] = json.loads(d["matched_rules"]) if d.get("matched_rules") else []
        d["stats"] = json.loads(d["stats"]) if d.get("stats") else {}
        d["region"] = d.get("channel_region") or ""
    return {"items": data, "total": total, "page": page, "page_size": page_size, "pending_count": pending_count}


# ---------------- 规则 ----------------
@app.get("/api/rules")
def list_rules(req: Request):
    auth.require_user(req)
    with database.db_cursor() as cur:
        cur.execute("SELECT * FROM rules ORDER BY id")
        return rows(cur)


@app.post("/api/rules")
def add_rule(req: Request, payload: RuleIn):
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute(
            """INSERT INTO rules (name, category, rule_type, pattern, severity, description, enabled)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payload.name, payload.category, payload.rule_type, payload.pattern,
             payload.severity, payload.description, 1 if payload.enabled else 0),
        )
        return {"id": cur.lastrowid}


@app.put("/api/rules/{rule_id}")
def update_rule(req: Request, rule_id: int, payload: RuleIn):
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute(
            """UPDATE rules SET name=?, category=?, rule_type=?, pattern=?,
               severity=?, description=?, enabled=? WHERE id=?""",
            (payload.name, payload.category, payload.rule_type, payload.pattern,
             payload.severity, payload.description, 1 if payload.enabled else 0, rule_id),
        )
    return {"ok": True}


@app.delete("/api/rules/{rule_id}")
def delete_rule(req: Request, rule_id: int):
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    return {"ok": True}


@app.post("/api/contents")
def add_content(req: Request, payload: ContentIn):
    auth.require_admin(req)
    """手动录入一条真实视频内容，并立即用当前启用规则做合规审核。

    用于在没有真机采集时，由人工把某条视频的真实标题/文案/字幕/语音转写
    文本提交进来进行真实审核（系统不会自动生成任何内容）。
    """
    with database.db_cursor() as cur:
        cur.execute("SELECT id FROM channels WHERE id=?", (payload.channel_id,))
        if not cur.fetchone():
            raise HTTPException(404, "视频号不存在")
        cur.execute(
            """INSERT INTO contents
               (channel_id, title, video_url, caption, transcript, ocr_text, publish_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payload.channel_id, payload.title, payload.video_url, payload.caption,
             payload.transcript, payload.ocr_text, payload.publish_time),
        )
        content_id = cur.lastrowid
        rule_list = _load_enabled_rules(cur)
        _analyze_and_store(cur, content_id, {
            "caption": payload.caption,
            "transcript": payload.transcript,
            "ocr_text": payload.ocr_text,
        }, rule_list)
        cur.execute(
            "UPDATE channels SET status='done', last_collected_at=datetime('now','localtime') WHERE id=?",
            (payload.channel_id,),
        )
    return {"id": content_id}


@app.put("/api/contents/{content_id}/override-status")
def override_content_status(req: Request, content_id: int, payload: dict):
    """人工覆盖审核结果。
    传 {"manual_status": "compliant"} 强制合规，
    传 {"manual_status": "violation"} 强制违规，
    传 {"manual_status": null} 清除覆盖，恢复自动判定。"""
    auth.require_manager(req)
    ms = payload.get("manual_status")
    if ms not in ("compliant", "violation", None):
        raise HTTPException(400, "manual_status 只能是 compliant / violation / null")
    with database.db_cursor() as cur:
        cur.execute("SELECT matched_rules FROM analysis_results WHERE content_id=?", (content_id,))
        row = cur.fetchone()
        if row:
            if ms == "compliant":
                cur.execute(
                    "UPDATE analysis_results SET manual_status=?, status=?, risk_level='none' WHERE content_id=?",
                    (ms, ms, content_id),
                )
            elif ms == "violation":
                cur.execute(
                    "UPDATE analysis_results SET manual_status=?, status=?, risk_level='forbidden' WHERE content_id=?",
                    (ms, ms, content_id),
                )
            else:
                # 清除覆盖：恢复原始自动判定状态与风险等级
                cur.execute(
                    """UPDATE analysis_results SET manual_status=NULL,
                       status=(SELECT CASE WHEN matched_rules IS NOT NULL AND matched_rules != '[]'
                               THEN 'violation' ELSE 'compliant' END
                        FROM analysis_results WHERE content_id=?),
                       risk_level=(SELECT CASE WHEN matched_rules IS NOT NULL AND matched_rules != '[]'
                               THEN (
                                 SELECT CASE
                                   WHEN mr LIKE '%"severity":"forbidden"%' THEN 'forbidden'
                                   WHEN mr LIKE '%"severity":"high"%' THEN 'high'
                                   WHEN mr LIKE '%"severity":"medium"%' THEN 'medium'
                                   WHEN mr LIKE '%"severity":"low"%' THEN 'low'
                                   ELSE 'none'
                                 END
                                 FROM (SELECT matched_rules AS mr FROM analysis_results WHERE content_id=?)
                               )
                               ELSE 'none'
                               END
                        FROM analysis_results WHERE content_id=?)
                       WHERE content_id=?""",
                    (content_id, content_id, content_id),
                )
        else:
            cur.execute(
                """INSERT INTO analysis_results (content_id, status, risk_level, manual_status)
                   VALUES (?, ?, ?, ?)""",
                (content_id, ms or "compliant", "forbidden" if ms == "violation" else "none", ms),
            )
    return {"ok": True, "manual_status": ms}


@app.post("/api/reanalyze")
def reanalyze_all(req: Request):
    """规则变更后，对已采集的全部内容重新跑合规分析（不重新采集）。"""
    auth.require_admin(req)

    # 先下载所有缺失的视频文件（采集时可能未下载或下载失败）
    try:
        from collector import channels_collector
        with database.db_cursor() as cur:
            cur.execute("SELECT * FROM contents")
            contents = rows(cur)
        # 按 channel_id 分组缓存 session
        session_cache = {}
        for c in contents:
            media_raw = c.get("media")
            if isinstance(media_raw, str):
                try:
                    c["media"] = json.loads(media_raw)
                except Exception:
                    c["media"] = []
            else:
                c["media"] = c["media"] or []

            updated = False
            for m in c["media"]:
                if m.get("is_video") and not m.get("video_local") and m.get("orig"):
                    ch_id = c.get("channel_id")
                    if ch_id not in session_cache:
                        sp = channels_collector.session_path_for(ch_id)
                        cookies = channels_collector._load_cookies(sp) if sp else None
                        session_cache[ch_id] = cookies
                    cookies = session_cache[ch_id]
                    try:
                        vlocal = channels_collector._download_video(
                            m["orig"], m.get("play_len", 0), cookies
                        )
                        if vlocal:
                            m["video_local"] = vlocal
                            updated = True
                            print(f"[reanalyze] 下载视频成功: {vlocal}", flush=True)
                    except Exception as e:
                        print(f"[reanalyze] 视频下载失败: {e}", flush=True)
            if updated:
                with database.db_cursor() as cur2:
                    cur2.execute(
                        "UPDATE contents SET media=? WHERE id=?",
                        (json.dumps(c["media"], ensure_ascii=False), c["id"]),
                    )
    except Exception as e:
        print(f"[reanalyze] 下载阶段异常(继续分析): {e}", flush=True)

    with database.db_cursor() as cur:
        rule_list = _load_enabled_rules(cur)
        cur.execute("SELECT * FROM contents")
        contents = rows(cur)
        for c in contents:
            media_raw = c.get("media")
            if isinstance(media_raw, str):
                try:
                    c["media"] = json.loads(media_raw)
                except Exception:
                    c["media"] = []
            stats_raw = c.get("stats")
            if isinstance(stats_raw, str):
                try:
                    c["stats"] = json.loads(stats_raw)
                except Exception:
                    c["stats"] = {}
            _analyze_and_store(cur, c["id"], c, rule_list)
    return {"ok": True, "reanalyzed": len(contents)}


# ---------------- 看板汇总 ----------------
@app.get("/api/auth/status")
def auth_status_api(deep: bool = False):
    """检测视频号后台授权（登录态）状态，供看板顶部横幅展示。
    deep=true 时无头打开后台做真实服务端验证（较慢，数秒）。"""
    from collector import channels_collector
    return channels_collector.auth_status(deep=deep)


@app.post("/api/collect-all")
async def collect_all(req: Request):
    """一键采集所有视频号的最新内容（手动触发，非实时）。
    逐个采集并返回每个号的结果；某个号失效不影响其他号。"""
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute("SELECT id, name FROM channels ORDER BY id")
        chans = [(r["id"], r["name"]) for r in cur.fetchall()]
    results = []
    for cid, name in chans:
        try:
            n = await asyncio.to_thread(run_collection, cid)
            results.append({"channel": name, "ok": True, "count": n})
        except HTTPException as e:
            results.append({"channel": name, "ok": False, "count": 0, "message": e.detail})
        except Exception as e:
            results.append({"channel": name, "ok": False, "count": 0, "message": str(e)})
    return {"results": results}


@app.get("/api/model-config")
def get_model_config(req: Request):
    """返回大模型配置（API Key 脱敏，不回传明文）。"""
    auth.require_manager(req)
    cfg = ai_auditor.load_config()
    key = cfg.get("api_key") or ""
    return {
        "enabled": cfg["enabled"],
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_set": bool(key),
        "api_key_masked": (key[:3] + "****" + key[-4:]) if len(key) >= 8 else ("****" if key else ""),
    }


def _merge_key(incoming_key: str) -> str:
    """前端留空表示沿用已存 Key；非空则更新。"""
    incoming_key = (incoming_key or "").strip()
    return incoming_key if incoming_key else (database.get_setting("ai_api_key", "") or "")


@app.post("/api/model-config")
def save_model_config(req: Request, cfg: ModelConfigIn):
    """保存大模型配置。若 base_url + api_key + model 齐全且用户未显式关闭，
    自动启用。"""
    auth.require_admin(req)
    database.set_setting("ai_base_url", (cfg.base_url or "").strip())
    database.set_setting("ai_model", (cfg.model or "").strip())
    key = _merge_key(cfg.api_key)
    database.set_setting("ai_api_key", key)
    # 自动启用：三个关键字段都有值且用户未显式传 enabled=false
    auto = bool(key and (cfg.base_url or "").strip() and (cfg.model or "").strip())
    enabled = cfg.enabled if not auto else True
    database.set_setting("ai_enabled", "1" if enabled else "0")
    return {"ok": True}


@app.post("/api/model-config/test")
def test_model_config(req: Request, cfg: ModelConfigIn):
    """测试大模型连通性（API Key 留空则用已保存的）。"""
    auth.require_admin(req)
    return ai_auditor.test_connection({
        "base_url": (cfg.base_url or "").strip(),
        "model": (cfg.model or "").strip(),
        "api_key": _merge_key(cfg.api_key),
    })


@app.get("/api/dashboard")
def _dashboard_redirect():
    """重定向到完整路径"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/dashboard/stats")


def _normalize_risk_distribution(rows) -> dict:
    """将 risk_level 合并到 red/yellow/green 三色分布中。
    forbidden/high → red（严重/高风险即违规）
    medium → yellow（中风险即疑似）
    low/none/green → green（低风险或无风险即合规）"""
    dist = {"red": 0, "yellow": 0, "green": 0}
    for r in rows:
        level = (r["risk_level"] or "").lower()
        cnt = r["c"]
        if level in ("red", "forbidden", "high"):
            dist["red"] += cnt
        elif level in ("yellow", "medium"):
            dist["yellow"] += cnt
        elif level in ("green", "low", "none"):
            dist["green"] += cnt
    return dist


@app.get("/api/dashboard/stats")
def dashboard_stats(req: Request):
    user = auth.require_user(req)
    role = user["role"]
    region = user.get("region", "")

    with database.db_cursor() as cur:
        if role == "uploader":
            # 只看自己的提交
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=?", (user["id"],))
            sc = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='approved'", (user["id"],))
            com = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='rejected'", (user["id"],))
            vic = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='manual_review'", (user["id"],))
            mr = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='pending'", (user["id"],))
            pen = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE user_id=? AND status='analyzing'", (user["id"],))
            anz = cur.fetchone()["c"]
            cur.execute("SELECT risk_level, COUNT(*) AS c FROM submissions WHERE user_id=? AND risk_level IS NOT NULL GROUP BY risk_level", (user["id"],))
            rd = _normalize_risk_distribution(cur.fetchall())
            return {
                "channel_count": 0, "content_count": sc, "violation_count": vic,
                "compliant_count": com, "manual_review_count": mr,
                "risk_distribution": rd, "submission_risk_distribution": rd,
                "submission_total": sc, "submission_approved": com,
                "submission_rejected": vic, "submission_manual_review": mr,
                "pending_count": pen, "analyzing_count": anz,
            }

        if role == "manager":
            big = region.split("/")[0].strip() if "/" in region else region.strip()
            mgr_filter = "(s.region LIKE ? OR s.region LIKE ? OR TRIM(s.region) = ?)"
            mgr_params = [f"{big}%", f"{big}/%", big]

            def _mgr_count(extra_where="", extra_params=[]):
                sql = f"SELECT COUNT(*) AS c FROM submissions s WHERE {mgr_filter}"
                if extra_where:
                    sql += f" AND {extra_where}"
                cur.execute(sql, mgr_params + extra_params)
                return cur.fetchone()["c"]

            # 提交侧统计
            sc = _mgr_count()
            com = _mgr_count("s.status='approved'")
            vic = _mgr_count("s.status='rejected'")
            mr = _mgr_count("s.status='manual_review'")
            pen = _mgr_count("s.status='pending'")
            anz = _mgr_count("s.status='analyzing'")
            cur.execute(f"SELECT risk_level, COUNT(*) AS c FROM submissions s WHERE {mgr_filter} AND risk_level IS NOT NULL GROUP BY risk_level", mgr_params)
            srd = _normalize_risk_distribution(cur.fetchall())

            # 采集侧统计（manager 只看本区域的采集内容）
            cur.execute("SELECT COUNT(*) AS c FROM channels WHERE region LIKE ? OR region LIKE ? OR TRIM(region) = ?", [f"{big}%", f"{big}/%", big])
            cc = cur.fetchone()["c"]
            cur.execute("""SELECT COUNT(*) AS c FROM contents c JOIN channels ch ON c.channel_id = ch.id
                           WHERE ch.region LIKE ? OR ch.region LIKE ? OR TRIM(ch.region) = ?""", [f"{big}%", f"{big}/%", big])
            cnt = cur.fetchone()["c"]
            cur.execute("""SELECT COUNT(*) AS c FROM analysis_results ar JOIN contents c ON ar.content_id = c.id
                           JOIN channels ch ON c.channel_id = ch.id
                           WHERE ar.status='violation' AND (ch.region LIKE ? OR ch.region LIKE ? OR TRIM(ch.region) = ?)""", [f"{big}%", f"{big}/%", big])
            cvic = cur.fetchone()["c"]
            cur.execute("""SELECT COUNT(*) AS c FROM analysis_results ar JOIN contents c ON ar.content_id = c.id
                           JOIN channels ch ON c.channel_id = ch.id
                           WHERE ar.manual_status IS NOT NULL AND (ch.region LIKE ? OR ch.region LIKE ? OR TRIM(ch.region) = ?)""", [f"{big}%", f"{big}/%", big])
            cmr = cur.fetchone()["c"]
            cur.execute("""SELECT ar.risk_level, COUNT(*) AS c FROM analysis_results ar JOIN contents c ON ar.content_id = c.id
                           JOIN channels ch ON c.channel_id = ch.id
                           WHERE ch.region LIKE ? OR ch.region LIKE ? OR TRIM(ch.region) = ?
                           GROUP BY ar.risk_level""", [f"{big}%", f"{big}/%", big])
            crd = _normalize_risk_distribution(cur.fetchall())

            return {
                # 采集侧
                "channel_count": cc, "content_count": cnt, "violation_count": cvic,
                "compliant_count": cnt - cvic, "manual_review_count": cmr,
                "risk_distribution": crd,
                # 提交侧
                "submission_risk_distribution": srd,
                "submission_total": sc, "submission_approved": com,
                "submission_rejected": vic, "submission_manual_review": mr,
                "pending_count": pen, "analyzing_count": anz,
            }

        # admin: 全量数据
        cur.execute("SELECT COUNT(*) AS c FROM channels")
        cc = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM contents")
        cnt = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM analysis_results WHERE status='violation'")
        vic = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM analysis_results WHERE manual_status IS NOT NULL")
        mr = cur.fetchone()["c"]
        cur.execute("SELECT risk_level, COUNT(*) AS c FROM analysis_results GROUP BY risk_level")
        rd = _normalize_risk_distribution(cur.fetchall())

        # 提交侧风险分布（数据来源：内容上传）
        cur.execute("SELECT risk_level, COUNT(*) AS c FROM submissions WHERE risk_level IS NOT NULL GROUP BY risk_level")
        srd = _normalize_risk_distribution(cur.fetchall())

        # 提交统计（上传侧）
        cur.execute("SELECT COUNT(*) AS c FROM submissions")
        sc = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE status='approved'")
        com = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE status='rejected'")
        rej = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE status='manual_review'")
        mr2 = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE status='pending'")
        pen = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM submissions WHERE status='analyzing'")
        anz = cur.fetchone()["c"]

        # 按区域统计提交
        cur.execute("SELECT s.region, COUNT(*) AS c FROM submissions s WHERE s.region IS NOT NULL GROUP BY s.region ORDER BY c DESC")
        by_region = [{"region": r["region"], "count": r["c"]} for r in cur.fetchall()]

        return {
            "channel_count": cc, "content_count": cnt, "violation_count": vic,
            "compliant_count": cnt - vic, "manual_review_count": mr,
            "risk_distribution": rd,
            "submission_risk_distribution": srd,
            # 提交侧数据
            "submission_total": sc, "submission_approved": com,
            "submission_rejected": rej, "submission_manual_review": mr2,
            "pending_count": pen, "analyzing_count": anz,
            "by_region": by_region,
        }


@app.get("/api/dashboard/overview")
def dashboard_overview(req: Request):
    """视频号合规概览（仅 admin）。"""
    auth.require_admin(req)
    with database.db_cursor() as cur:
        cur.execute("""SELECT c.name, c.region,
            COUNT(ct.id) AS content_count,
            SUM(CASE WHEN ar.status='violation' THEN 1 ELSE 0 END) AS violation_count
            FROM channels c LEFT JOIN contents ct ON ct.channel_id=c.id
            LEFT JOIN analysis_results ar ON ar.content_id=ct.id
            GROUP BY c.id ORDER BY c.id""")
        data = []
        for r in cur.fetchall():
            cnt = r["content_count"] or 0
            vic = r["violation_count"] or 0
            data.append({
                "name": r["name"],
                "active": True,
                "region": r["region"],
                "content_count": cnt,
                "violation_count": vic,
                "compliant_rate": round((cnt - vic) / cnt * 100, 1) if cnt > 0 else 0,
            })
    return data


# ── 公开区域列表（登录页注册需要，无需认证） ──
# 标准表已收敛到 runtime_utils.REGIONS（写入端规范化也用同一份），这里只做转发。

# 人均战功统计使用的区域人数。河南CD区是豫北CD区与豫南CD区的合计，
# 作品区域仍保留原有两个名称，统计时统一归入河南CD区；流通渠道部不计入。
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


@app.get("/api/regions")
def list_regions():
    return _REGIONS


@app.get("/api/region-headcounts")
def list_region_headcounts():
    return {
        "headcounts": _REGION_HEADCOUNTS,
        "aliases": {"苏南区": "苏南CD区", "豫北CD区": "河南CD区", "豫南CD区": "河南CD区"},
        "excluded": ["流通渠道部"],
    }


@app.get("/api/queue/status")
def get_queue_status(req: Request):
    """返回审核队列实时状态（排队数、处理中、预估等待时间）。"""
    auth.require_user(req)
    return queue_manager.queue_status()


# ---------------- 品牌参考学习（防形近字误报） ----------------
_BRAND_REF_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _brand_ref_cos_key(brand: str) -> str:
    ym = datetime.now().strftime("%Y%m")
    return f"campaign/brand_ref/{brand}/{ym}/{int(time.time() * 1000)}_{secrets.token_hex(4)}.jpg"


def _decode_data_url(data_url: str) -> tuple:
    """把 data:image/...;base64,xxx 解码成 (bytes, mime)。失败返回 (None, None)。"""
    try:
        if "," not in data_url:
            return None, None
        head, b64 = data_url.split(",", 1)
        m = re.match(r"data:image/([a-zA-Z0-9.+-]+);base64", head.strip())
        mime = f"image/{m.group(1)}" if m else "image/jpeg"
        return base64.b64decode(b64), mime
    except Exception:
        return None, None


@app.post("/api/admin/brand-ref/learn")
async def brand_ref_learn(payload: BrandRefLearnIn, req: Request):
    """上传一张品牌产品图，后端：
    1) 解码图片 → 2) 上传 COS（路径 campaign/brand_ref/{brand}/{yyyymm}/）→
    3) 调多模态模型生成「字形指纹 + 包装布局 + 易误识形近字」描述 →
    4) 写到 brand_ref_descriptions；之后每次 audit() 都把这份描述读进 system prompt。

    入参：
      brand: 品牌名（南孚/益圆/丰蓝/传应）
      image: base64 data URL 或 HTTP URL（建议用 data URL，避免 CDN 防盗链）
      note:  可选备注
    """
    auth.require_admin(req)
    brand = (payload.brand or "").strip()
    if not brand:
        raise HTTPException(400, "品牌名必填")
    if not payload.image:
        raise HTTPException(400, "图片必填（base64 data URL 或 HTTP URL）")

    cos_key = None
    img_bytes = None
    img_mime = "image/jpeg"

    if payload.image.startswith("data:"):
        img_bytes, img_mime = _decode_data_url(payload.image)
        if not img_bytes:
            raise HTTPException(400, "data URL 解析失败")
    elif payload.image.startswith("http://") or payload.image.startswith("https://"):
        # HTTP URL：直接传 URL 给多模态模型，cos_key 留空
        img_bytes = None
    else:
        raise HTTPException(400, "image 必须是 data URL 或 http(s) URL")

    if img_bytes is not None and cos_service.is_enabled():
        try:
            cos_key = _brand_ref_cos_key(brand)
            cos_service.upload_bytes(img_bytes, cos_key, img_mime or "image/jpeg")
        except Exception as e:
            print(f"[brand-ref] COS 上传失败: {e}")
            cos_key = None

    # 调多模态模型生成描述
    res = ai_auditor.learn_brand_description(brand, payload.image)
    if res.get("error"):
        # 描述生成失败：回滚本次学习（避免留下空描述）
        raise HTTPException(500, f"AI 生成描述失败: {res['error']}")
    desc = res.get("description") or {}

    # 写 DB：先 upsert brand_ref_descriptions，再 upsert brand_ref_images
    import json as _json
    with database.db_cursor() as cur:
        cur.execute("""
            INSERT INTO brand_ref_descriptions (brand, description, model, generated_at)
            VALUES (?, ?, ?, datetime('now','localtime'))
            ON CONFLICT(brand) DO UPDATE SET
                description=excluded.description,
                model=excluded.model,
                generated_at=excluded.generated_at
        """, (brand, _json.dumps(desc, ensure_ascii=False), res.get("model", "")))
        if cos_key:
            cur.execute("""
                INSERT INTO brand_ref_images (brand, cos_key, updated_at, note)
                VALUES (?, ?, datetime('now','localtime'), ?)
                ON CONFLICT(brand) DO UPDATE SET
                    cos_key=excluded.cos_key,
                    updated_at=excluded.updated_at,
                    note=COALESCE(excluded.note, brand_ref_images.note)
            """, (brand, cos_key, payload.note))

    return {
        "ok": True,
        "brand": brand,
        "cos_key": cos_key,
        "cos_url": cos_service.presigned_url(cos_key) if cos_key and cos_service.is_enabled() else None,
        "description": desc,
        "model": res.get("model", ""),
    }


@app.get("/api/admin/brand-ref")
def brand_ref_list(req: Request):
    """列出已学习的品牌 + 描述 + COS 预览链接，给管理后台展示用。"""
    auth.require_admin(req)
    import json as _json
    with database.db_cursor() as cur:
        cur.execute("""
            SELECT b.brand, b.description, b.model, b.generated_at,
                   i.cos_key, i.note
            FROM brand_ref_descriptions b
            LEFT JOIN brand_ref_images i ON i.brand = b.brand
            ORDER BY b.id
        """)
        rows = []
        for r in cur.fetchall():
            desc = r["description"]
            try:
                if isinstance(desc, str):
                    desc = _json.loads(desc) if desc.strip().startswith("{") else {}
            except Exception:
                desc = {}
            rows.append({
                "brand": r["brand"],
                "description": desc,
                "model": r["model"],
                "generated_at": r["generated_at"],
                "cos_key": r["cos_key"],
                "cos_url": cos_service.presigned_url(r["cos_key"]) if r["cos_key"] and cos_service.is_enabled() else None,
                "note": r["note"],
            })
    return {"items": rows, "count": len(rows)}


# ---------------- 前端静态托管 ----------------
# 页面路由（必须在 StaticFiles mount 之前定义）
@app.get("/")
def index():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/user")

@app.get("/user")
def user_page():
    """公开上传页面 —— 无需登录即可上传内容送审（复用 index.html 的免登录模式）"""
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/manager")
def manager_page():
    """管理后台 —— 需要登录后使用（看板、审核、采集等）"""
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/brand_ref")
def brand_ref_page():
    """品牌参考学习 —— 让多模态模型学习 4 个品牌的标准产品图，作为「形近字错字」判定的绝对基准。"""
    return FileResponse(os.path.join(FRONTEND_DIR, "brand_ref.html"))


app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="static")
