"""提交审核路由：上传、列表、详情、人工审批。"""

import csv
import hashlib
import io
import json
import os
import secrets
import threading
import time
import subprocess
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, BackgroundTasks
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

import auth as auth_service
import queue_manager
import cos_service
import campaign_scoring
import ai_auditor
import database
from database import db_cursor
from runtime_utils import normalize_region

router = APIRouter(prefix="/api/submissions", tags=["submissions"])
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "..", "uploads")

# 内容审核硬门槛：视频时长不足 15 秒直接拒收（前端也会先拦一道，这里是兜底强限制）
MIN_VIDEO_SECONDS = 15
os.makedirs(UPLOAD_DIR, exist_ok=True)


def _video_content_fingerprint(filepath: str) -> str:
    """用固定关键帧的低分辨率感知摘要识别重新编码后的同一视频。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", filepath,
             "-vf", "fps=1/5,scale=16:16,format=gray", "-frames:v", "12",
             "-f", "rawvideo", "pipe:1"],
            capture_output=True, timeout=90,
        )
        raw = proc.stdout or b""
        frame_size = 16 * 16
        if len(raw) < frame_size:
            return ""
        bits = []
        for pos in range(0, len(raw) - frame_size + 1, frame_size):
            frame = raw[pos:pos + frame_size]
            avg = sum(frame) / len(frame)
            bits.append("".join("1" if px >= avg else "0" for px in frame))
        return "|".join(bits[:12])
    except Exception as e:
        print(f"[upload] 视频内容指纹生成失败: {e}", flush=True)
        return ""


def _fingerprint_similarity(a: str, b: str) -> float:
    aa, bb = a.split("|"), b.split("|")
    total = count = 0
    for x, y in zip(aa, bb):
        if len(x) != len(y):
            continue
        total += len(x)
        count += sum(c == d for c, d in zip(x, y))
    return count / total if total else 0.0


def _fill_media_urls(media_list):
    """把 media 里的 cos_key 实时转成预签名 URL（2小时有效），供前端/AI 访问。

    只有 cos_ready=True（后台已确认传到 COS）才生成预签名 URL；
    否则回退本地 /uploads 路径，避免给出一个还不存在的对象地址导致 404。
    注意用 get("cos_ready", True)：本次改动前的历史数据没有该字段，
    其本地副本早已删除，必须继续走 COS，否则视频图片会 404。
    """
    if not media_list:
        return media_list
    for m in media_list:
        if not isinstance(m, dict):
            continue
        key = m.get("cos_key")
        if key and cos_service.is_enabled() and m.get("cos_ready", True):
            m["cos_url"] = cos_service.presigned_url(key, expires=7200)
        else:
            m["cos_url"] = None
    return media_list


def _fill_rule_evidence(rules):
    """给命中的规则补齐违规截图的可访问地址。

    AI 审核时把违规画面截图存进 COS（key 记在 evidence_key），COS key 本身不可直接
    访问，必须在这里实时换成预签名 URL；未启用 COS 时回退审核时落的本地副本。
    """
    for r in (rules or []):
        if not isinstance(r, dict):
            continue
        key = r.get("evidence_key")
        url = None
        if key and cos_service.is_enabled():
            try:
                url = cos_service.presigned_url(key, expires=7200)
            except Exception:
                url = None
        r["evidence_url"] = url or r.get("evidence_local") or None
    return rules


def _mark_cos_status(submission_id: int, cos_key: str, status: str,
                     error: str = "", retry_count: int = 0) -> None:
    """更新本地缓存区素材的 COS 异步处理状态。"""
    for table, where in (("submissions", "id=?"), ("campaign_works", "submission_id=?")):
        try:
            with db_cursor() as cur:
                cur.execute(f"SELECT media FROM {table} WHERE {where}", (submission_id,))
                row = cur.fetchone()
                if not row:
                    continue
                media = _parse_json(row["media"], [])
                changed = False
                for m in media:
                    if isinstance(m, dict) and m.get("cos_key") == cos_key:
                        m["cos_status"] = status
                        m["cos_error"] = error or None
                        m["cos_retry_count"] = retry_count
                        changed = True
                if changed:
                    cur.execute(f"UPDATE {table} SET media=? WHERE {where}",
                                (json.dumps(media, ensure_ascii=False), submission_id))
        except Exception as e:
            print(f"[upload] 更新 COS 状态失败 #{submission_id}: {e}", flush=True)


def _mark_cos_ready(submission_id: int, cos_key: str) -> None:
    """把某个已上传到 COS 的对象标记为就绪。

    submissions 和 campaign_works 各存了一份 media 快照，**两张表都要更新**：
    只改 submissions 会让双百战役作品详情一直回退到已被清理的本地 /uploads
    地址，视频/图片变成 404。
    """
    for table, where in (("submissions", "id=?"), ("campaign_works", "submission_id=?")):
        try:
            with db_cursor() as cur:
                cur.execute(f"SELECT media FROM {table} WHERE {where}", (submission_id,))
                row = cur.fetchone()
                if not row:
                    continue
                media = _parse_json(row["media"], [])
                changed = False
                for m in media:
                    if (isinstance(m, dict) and m.get("cos_key") == cos_key
                            and not m.get("cos_ready")):
                        m["cos_ready"] = True
                        changed = True
                if changed:
                    cur.execute(
                        f"UPDATE {table} SET media=? WHERE {where}",
                        (json.dumps(media, ensure_ascii=False), submission_id),
                    )
        except Exception as e:
            print(f"[upload] 回填 {table}.media 失败 #{submission_id}: {e}", flush=True)


def _after_upload_bg(submission_id: int, items: list) -> None:
    """上传后的收尾（后台线程）：本地缓存区 → COS → 标记就绪 → 补算战役分。

    上传接口已经返回后，素材仍保留在 uploads 缓存区。COS 暂时失败只记录状态并保留本地文件，
    不影响用户先看到“已接收、排队处理中”。短暂网络失败自动重试，避免大视频丢失。
    """
    if cos_service.is_enabled():
        for filepath, cos_key, mime in items:
            last_error = ""
            for attempt in range(1, 4):
                _mark_cos_status(submission_id, cos_key, "uploading", last_error, attempt - 1)
                try:
                    ok = cos_service.upload(filepath, cos_key, mime)
                    if ok:
                        _mark_cos_ready(submission_id, cos_key)
                        _mark_cos_status(submission_id, cos_key, "ready", "", attempt)
                        break
                    last_error = "COS 未返回成功状态"
                except Exception as e:
                    last_error = str(e)[:500]
                    print(f"[upload] COS 上传失败 {cos_key}（第{attempt}次）: {e}", flush=True)
                if attempt < 3:
                    time.sleep(attempt * 5)
            else:
                _mark_cos_status(submission_id, cos_key, "failed", last_error, 3)
                print(f"[upload] COS 暂时无法处理，素材保留在缓存区: {filepath}", flush=True)

    try:
        from .campaign import score_pending_work_for_submission
        score_pending_work_for_submission(submission_id)
    except Exception as e:
        print(f"[upload] 战役打分触发失败 #{submission_id}: {e}", flush=True)


def _mark_fingerprint_duplicate(submission_id: int, first_video_path: str,
                                pending_cos: list) -> bool:
    """后台补算画面指纹，拦截「重新编码后的同一视频」。命中重复返回 True。

    指纹要走 ffmpeg 解码，100MB 级视频实测 6 秒以上；放在上传响应前会把请求拖到
    网关回源超时（前端 504）。这里在响应发出后补算，命中时按原有业务结论收尾：
    提交记为驳回、作废对应的双百战役作品、清掉缓存区素材、不进审核队列。
    """
    if not first_video_path or not os.path.exists(first_video_path):
        return False
    fingerprint = _video_content_fingerprint(first_video_path)
    if not fingerprint:
        return False

    with db_cursor() as cur:
        # 先把自己的指纹落库（后续提交才有对照），再排除自己去比对历史记录
        cur.execute("UPDATE submissions SET content_fingerprint=? WHERE id=?",
                    (fingerprint, submission_id))
        rows = cur.execute(
            "SELECT id, created_at, display_name, content_fingerprint FROM submissions "
            "WHERE content_fingerprint IS NOT NULL AND id != ? "
            "ORDER BY id DESC LIMIT 500", (submission_id,)).fetchall()
    dup = None
    for row in rows:
        if _fingerprint_similarity(fingerprint, row["content_fingerprint"]) >= 0.94:
            dup = row
            break
    if not dup:
        return False

    reason = (f"素材重复：与 #{dup['id']}（{dup['display_name'] or '他人'}，"
              f"{dup['created_at']}）画面一致，无需重复提交")
    for filepath, _k, _m in pending_cos:
        try:
            os.remove(filepath)
        except OSError:
            pass
    with db_cursor() as cur:
        cur.execute(
            "UPDATE submissions SET status='rejected', risk_level='none', review_comment=?, "
            "ai_result=? WHERE id=?",
            (reason,
             json.dumps({"model": "", "interpretation": "", "conclusion": reason,
                         "violations": [], "error": None}, ensure_ascii=False),
             submission_id),
        )
        cur.execute(
            "UPDATE campaign_works SET status='disqualified', qualified=0, reject_reason=?, "
            "updated_at=? WHERE submission_id=?",
            (reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), submission_id),
        )
    print(f"[upload] 画面指纹命中重复：#{submission_id} ↔ #{dup['id']}，已作废", flush=True)
    return True


def _post_upload_bg(submission_id: int, first_video_path: str, pending_cos: list,
                    auto_track: bool = False) -> None:
    """上传响应发出后的收尾：① 指纹判重 ② AI 赛道识别（选了「由 AI 识别」时）
    ③ 进 AI 审核队列 ④ 转 COS + 补战役分。

    顺序有讲究：先判重（重复就不该白烧一次多模态审核），再入队（审核要读本地素材，
    越早入队越早出结果），最后才是耗时的 COS 上传与战役打分——这些都在响应之后。
    """
    try:
        if _mark_fingerprint_duplicate(submission_id, first_video_path, pending_cos):
            return
    except Exception as e:
        print(f"[upload] 画面指纹判重失败 #{submission_id}: {e}", flush=True)
    # 选了「由 AI 识别后推荐」的，后端自己起识别任务，识别出赛道就自动建档。
    # 不等前端来触发：用户传完就关页面/息屏时，前端那次 POST 根本发不出去，
    # 提交就会永远挂在「未参战」。
    if auto_track:
        try:
            with db_cursor() as cur:
                cur.execute("SELECT caption, title, media FROM submissions WHERE id=?",
                            (submission_id,))
                row = cur.fetchone()
            if row:
                _start_track_task(
                    submission_id, row["caption"] or row["title"] or "",
                    _fill_media_urls(_parse_json(row["media"], [])),
                )
        except Exception as e:
            print(f"[upload] 启动赛道识别失败 #{submission_id}: {e}", flush=True)
    queue_manager.enqueue(submission_id)
    _after_upload_bg(submission_id, pending_cos)


@router.post("/upload")
async def upload_submission(req: Request, background_tasks: BackgroundTasks):
    """上传内容（视频/图片+文案）并加入审核队列，异步返回。
    支持大文件上传（最大 500MB/文件）。
    已登录用户直接使用身份；未登录用户须提供 display_name + region。

    COS 上传与双百战役 AI 打分走 background_tasks：大文件/大模型耗时很长，
    同步等待会让浏览器与 CDN 回源超时（504）。
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
    track = (form.get("track") or "").strip()  # 双百战役赛道（必选：见下方校验）
    # 赛道改成必选：所有投稿都必须参与双百战役。
    # "__ai__" 是前端「由 AI 识别后推荐」的占位值——它同样要求用户事后在
    # 确认弹窗里选定具体赛道（/confirm-track 会再校验一次），所以这里放行。
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if track != "__ai__" and track not in valid_tracks:
        raise HTTPException(
            400,
            f"请选择参与双百战役的赛道，可选：{' / '.join(valid_tracks)}",
        )
    work_url = (form.get("url") or "").strip()  # 作品链接：已改到 T+7 回传时填写，这里保留兼容旧客户端

    # ── 身份识别 ──
    user = auth_service.try_get_user(req)
    if user:
        try:
            auth_service.require_user(req)
        except HTTPException:
            user = None

    # 登录用户（在地营销等）上传时前端不显示「姓名 / 区域」输入框（#guestFields
    # 只对访客可见），表单里根本没有 region。必须用账号自带的区域兜底，
    # 否则作品会归到"未分区"：既不计入本大区的榜单与战绩，区域经理也看不到。
    if user and not region:
        region = (user.get("region") or "").strip()
    # 同理，登录用户表单里也没有姓名输入框，display_name 会是空的。
    # 不兜底的话：submissions.display_name 存空串 → 按姓名搜索搜不到自己的作品、
    # 操作日志里显示成空。用账号昵称（退而求其次用用户名）补上。
    if user and not display_name:
        display_name = (user.get("display_name") or user.get("username") or "").strip()
    # 区域统一成「大区 / 细分区域」：登录账号自带的区域可能是「西二区/云南省」
    # 这类没有空格的写法，直接落库会让榜单/驾驶舱多出一条重复区域。
    region = normalize_region(region)

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
    pending_cos = []  # (本地路径, cos_key, mime)：交给后台线程传 COS，不阻塞响应
    first_hash = ""   # 首个文件的内容哈希，用于拦截完全相同的重复提交
    first_video_path = ""   # 首个视频的本地路径：画面指纹改到响应后补算（见 _post_upload_bg）
    for idx, f in enumerate(files):
        ext = os.path.splitext(f.filename or "file")[1] or ".bin"
        safe_name = f"{int(time.time() * 1000)}_{secrets.token_hex(4)}{ext}"
        filepath = os.path.join(UPLOAD_DIR, safe_name)
        # 流式写入磁盘，避免大文件撑爆内存；顺带对首个文件算哈希
        hasher = hashlib.sha256()
        with open(filepath, "wb") as fd:
            while True:
                chunk = await f.read(8 * 1024 * 1024)  # 8MB 一块
                if not chunk:
                    break
                if idx == 0:
                    hasher.update(chunk)
                fd.write(chunk)
        if idx == 0:
            first_hash = hasher.hexdigest()[:32]
        fsize = os.path.getsize(filepath)
        is_video = ext.lower() in (".mp4", ".mov", ".avi", ".webm", ".mkv", ".flv")

        # 内容审核硬门槛：视频必须满 15 秒。前端选文件时已拦一次，
        # 这里再卡一道——绕过前端直接打接口同样传不进来。
        if is_video:
            dur = ai_auditor.probe_duration(filepath)
            if idx == 0:
                # 这里只记路径，不算指纹：抽帧指纹要走 ffmpeg 解码，100MB 级视频实测 6 秒以上，
                # 放在响应前会把请求拖到网关（CDN 回源）超时，用户看到 504。改到响应后补算。
                first_video_path = filepath
            if 0 < dur < MIN_VIDEO_SECONDS:
                for fp, _k, _m in pending_cos:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                raise HTTPException(
                    400,
                    f"视频时长 {dur:.1f} 秒，不足 {MIN_VIDEO_SECONDS} 秒，无法提交审核",
                )

        # COS 存放路径：大区/细分区域/上传者身份/内容类型/年月（对象由后台线程实际上传）
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

        media_list.append({
            "type": "video" if is_video else "image",
            "filename": f.filename,
            "path": f"uploads/{safe_name}",
            "cos_key": cos_key,
            "cos_ready": False,  # 后台传完 COS 后置 True，才对外给预签名 URL
            "size": fsize,
            "is_video": is_video,
            "thumb": None,
            "video_local": f"uploads/{safe_name}",
        })
        pending_cos.append((filepath, cos_key, mime))

    # 同一份素材重复提交会白烧一次多模态审核（单次成本最高），按文件哈希先拦一道。
    # 注意放在所有文件都落盘之后：命中重复时要把刚写的临时文件清干净。
    # 画面指纹（重新编码后的同一视频）判定在响应后补算，见 _post_upload_bg：
    # 哈希比对是纯查库，很快；指纹要跑 ffmpeg，放在这里会拖垮上传响应。
    dup = None
    with db_cursor() as cur:
        if first_hash:
            cur.execute(
                "SELECT id, created_at, display_name FROM submissions "
                "WHERE content_hash=? ORDER BY id DESC LIMIT 1", (first_hash,))
            dup = cur.fetchone()
    if dup:
        for fp, _k, _m in pending_cos:
            try:
                os.remove(fp)
            except OSError:
                pass
        who = dup["display_name"] or "他人"
        raise HTTPException(
            409,
            f"该内容已由「{who}」于 {dup['created_at']} 提交过（编号 #{dup['id']}），"
            f"无需重复提交。若确需重新上传，请先删除原记录或换一份素材。",
        )

    with db_cursor() as cur:
        client_ip = req.client.host if req.client else ""
        cur.execute(
            "INSERT INTO submissions (user_id, display_name, guest_session, uploader_ip, title, caption, media, region, url, content_hash, content_fingerprint, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (user["id"] if user else None, display_name, guest_session, client_ip, title, caption,
             json.dumps(media_list, ensure_ascii=False), region, work_url, first_hash or None,
             None, "pending"),
        )
        sub_id = cur.lastrowid
        # 写使用日志
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("upload", display_name, region, client_ip, sub_id,
             f"title={title[:60]}; caption={caption[:100]}; files={len(files)}"),
        )

    # ── 赛道必选：具体赛道在上传时已校验过，这里直接建档 ──
    campaign_code = None
    if track in valid_tracks:
        from .campaign import _create_campaign_work
        # 建档本身只写库（快）；内含的多模态打分会让响应卡住几十秒到几分钟，
        # 因此 defer_ai=True 先把 code 返回给用户，打分交给下面后置任务补。
        work = await run_in_threadpool(
            _create_campaign_work,
            creator_id=user["id"] if user else None,
            creator_name=display_name or (user.get("display_name") if user else "") or "",
            track=track,
            title=title or caption,
            url=work_url,
            media=media_list,
            region=region,
            submission_id=sub_id,
            defer_ai=True,
        )
        campaign_code = work["code"]

    # 收尾统一放后台：① 补算画面指纹并判重 ② AI 赛道识别（选了「AI 识别」时）
    # ③ 进审核队列 ④ 传 COS（大文件耗时长，同步等会被 CDN 回源超时切断）
    # ⑤ 标记 media 就绪 ⑥ 补算战役内容表现分。
    # 必须在本处注册（建档之后），否则打分时 campaign_works 里还没有这条作品。
    background_tasks.add_task(_post_upload_bg, sub_id, first_video_path, pending_cos,
                              track == "__ai__")

    resp_data = {"ok": True, "submission_id": sub_id, "status": "pending",
                 "queue_position": queue_manager.queue_size(),
                 "campaign_code": campaign_code}

    resp = JSONResponse(resp_data)
    # 下发 guest_session：未登录用户后续只能靠它认领自己的提交。
    # 之前只存库不下发 cookie，只能拿 IP 兜底，而经过 CDN 回源后
    # 前后两次请求的 client IP 可能不一致 → 查详情 403 → 前端把错误
    # 渲染成"审核不通过 · 内容违规"（用户反馈的核心问题）。
    if guest_session:
        resp.set_cookie(
            "guest_session", guest_session,
            max_age=86400 * 180, httponly=True, samesite="lax",
        )
    return resp


@router.get("")
def list_submissions(req: Request, page: int = 1, page_size: int = 20,
                     status: str = None, region: str = None,
                     risk_level: str = None, search: str = None,
                     campaign: str = None, track: str = None):
    """列出提交（按角色权限过滤）。支持按状态/区域/风险等级/关键词/是否参战/赛道筛选。
    guest 用户通过 guest_session cookie 查看自己的提交。
    """
    user = auth_service.try_get_user(req)
    guest_session = req.cookies.get("guest_session") or ""
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
    elif guest_session or client_ip:
        # guest：优先按 guest_session cookie 认领，IP 仅作旧数据兼容回退
        if guest_session and client_ip:
            where.append("(s.guest_session = ? OR s.uploader_ip = ?)")
            params.extend([guest_session, client_ip])
        elif guest_session:
            where.append("s.guest_session = ?")
            params.append(guest_session)
        else:
            where.append("s.uploader_ip = ?")
            params.append(client_ip)
    else:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}

    if status:
        where.append("s.status = ?")
        params.append(status)
    if region:
        # region 存储格式为 "大区 / 细分区域"：既支持按大区筛选，也支持下钻到细分区域
        where.append("(s.region LIKE ? OR s.region LIKE ? OR TRIM(s.region) = ? OR TRIM(s.region) LIKE ?)")
        params.extend([f"{region}%", f"{region}/%", region, f"%{region}"])
    if risk_level:
        where.append("s.risk_level = ?")
        params.append(risk_level)
    if search:
        # 同时搜账号昵称：登录用户早期上传的记录 submissions.display_name 是空的，
        # 只搜 s.display_name 会搜不到自己的作品
        where.append("(s.title LIKE ? OR s.caption LIKE ? OR s.display_name LIKE ? OR u.display_name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if campaign == "yes":
        where.append("w.id IS NOT NULL")
    elif campaign == "no":
        where.append("w.id IS NULL")
    if track:
        where.append("w.track = ?")
        params.append(track)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    # 始终用 LEFT JOIN：公开上传的提交 user_id 为 NULL，INNER JOIN 会丢弃
    # campaign_works 关联用于判断是否参与双百战役、以及所属赛道
    # 参战作品带出评分，供「数据导出」板块展示总分/等级/战功；未参战上传这三列为 NULL
    join_clause = ("LEFT JOIN users u ON u.id = s.user_id "
                   "LEFT JOIN campaign_works w ON w.submission_id = s.id "
                   "LEFT JOIN campaign_scores sc ON sc.work_id = w.id")
    count_from = "submissions s LEFT JOIN users u ON u.id = s.user_id LEFT JOIN campaign_works w ON w.submission_id = s.id"

    with db_cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM {count_from}{where_sql}", params)
        total = cur.fetchone()["c"]
        cur.execute(
            f"""SELECT s.*, u.username, u.display_name AS user_display_name, u.region AS user_region,
                       w.track AS campaign_track, w.code AS campaign_code, w.id AS campaign_work_id,
                       sc.ai_content_score, sc.total_score, sc.grade, sc.total_merit
                FROM submissions s {join_clause}
                {where_sql} ORDER BY s.id DESC LIMIT ? OFFSET ?""",
            params + [page_size, offset])
        data = [dict(r) for r in cur.fetchall()]

    for d in data:
        d["media"] = _fill_media_urls(_parse_json(d.get("media"), []))
        d["ai_result"] = _parse_json(d.get("ai_result"))
        d["matched_rules"] = _fill_rule_evidence(_parse_json(d.get("matched_rules"), []))
        # 对于 guest 提交，display_name 在 submission 表上；对于注册用户，用 user_display_name 覆盖
        if d.get("user_display_name") and not d.get("display_name"):
            d["display_name"] = d["user_display_name"]
        elif d.get("display_name"):
            d["region"] = d.get("region")  # submission 自带 region

    return {"items": data, "total": total, "page": page, "page_size": page_size}


@router.get("/export")
def export_submissions(req: Request, status: str = None, region: str = None,
                       search: str = None, campaign: str = None, track: str = None,
                       risk_level: str = None):
    """导出提交列表为 CSV（应用与列表一致的筛选 + 同口径的角色权限过滤）。"""
    user = auth_service.try_get_user(req)
    guest_session = req.cookies.get("guest_session") or ""
    client_ip = req.client.host if req.client else ""
    where, params = [], []

    if user:
        if user["role"] == "uploader":
            where.append("s.user_id = ?")
            params.append(user["id"])
        elif user["role"] == "manager":
            mgr_region = user.get("region", "")
            if mgr_region:
                big = mgr_region.split("/")[0].strip() if "/" in mgr_region else mgr_region.strip()
                where.append("(s.region LIKE ? OR s.region LIKE ? OR TRIM(s.region) = ?)")
                params.extend([f"{big}%", f"{big}/%", big])
    elif guest_session or client_ip:
        # guest：优先 cookie 认领，IP 仅作旧数据兼容回退
        if guest_session and client_ip:
            where.append("(s.guest_session = ? OR s.uploader_ip = ?)")
            params.extend([guest_session, client_ip])
        elif guest_session:
            where.append("s.guest_session = ?")
            params.append(guest_session)
        else:
            where.append("s.uploader_ip = ?")
            params.append(client_ip)
    else:
        return Response(content="", media_type="text/csv; charset=utf-8")

    if status:
        where.append("s.status = ?")
        params.append(status)
    if region:
        # 同列表接口：大区筛选用前缀，细分区域（存储为大区后缀）用后缀匹配
        where.append("(s.region LIKE ? OR s.region LIKE ? OR TRIM(s.region) = ? OR TRIM(s.region) LIKE ?)")
        params.extend([f"{region}%", f"{region}/%", region, f"%{region}"])
    if search:
        where.append("(s.title LIKE ? OR s.caption LIKE ? OR s.display_name LIKE ? OR u.display_name LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like, like, like])
    if campaign == "yes":
        where.append("w.id IS NOT NULL")
    elif campaign == "no":
        where.append("w.id IS NULL")
    if track:
        where.append("w.track = ?")
        params.append(track)
    if risk_level:
        where.append("s.risk_level = ?")
        params.append(risk_level)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with db_cursor() as cur:
        cur.execute(
            f"""SELECT s.id, s.title, s.caption, s.display_name, s.region, s.status,
                       s.risk_level, s.created_at, u.display_name AS user_display_name,
                       w.track AS campaign_track, w.code AS campaign_code,
                       sc.ai_content_score, sc.total_score, sc.grade, sc.total_merit
                FROM submissions s
                LEFT JOIN users u ON u.id = s.user_id
                LEFT JOIN campaign_works w ON w.submission_id = s.id
                LEFT JOIN campaign_scores sc ON sc.work_id = w.id
                {where_sql} ORDER BY s.id DESC""",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]

    status_cn = {"pending": "待审核", "analyzing": "分析中", "approved": "已通过",
                 "rejected": "已驳回", "manual_review": "待复核"}
    risk_cn = {"none": "无", "low": "低风险", "medium": "中风险",
               "high": "高风险", "forbidden": "禁止发布"}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ID", "标题", "提交者", "区域", "是否参战", "赛道", "内容分",
                     "总分", "等级", "战功", "风险等级", "状态", "提交时间"])
    for r in rows:
        name = r["display_name"] or r["user_display_name"] or "访客"
        writer.writerow([
            r["id"], (r["title"] or r["caption"] or "")[:100], name, r["region"],
            "是" if r["campaign_track"] else "否", r["campaign_track"] or "-",
            r["ai_content_score"] if r["ai_content_score"] is not None else "-",
            r["total_score"] if r["total_score"] is not None else "-",
            r["grade"] or "-",
            r["total_merit"] if r["total_merit"] is not None else "-",
            risk_cn.get(r["risk_level"], r["risk_level"] or "-"),
            status_cn.get(r["status"], r["status"]), (r["created_at"] or ""),
        ])

    csv_str = buf.getvalue()
    return Response(
        content="\ufeff" + csv_str,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=submissions_export.csv"},
    )


@router.post("/{submission_id}/recheck-ai")
def recheck_ai_submission(submission_id: int, req: Request):
    """管理员强制让单条提交重新进入 AI 审核队列。"""
    auth_service.require_admin(req)
    with db_cursor() as cur:
        cur.execute("SELECT id FROM submissions WHERE id=?", (submission_id,))
        if not cur.fetchone():
            raise HTTPException(404, "提交不存在")
        cur.execute(
            "UPDATE submissions SET status='pending', ai_result=NULL, matched_rules='[]', risk_level='none', review_comment=NULL, reviewed_by=NULL, reviewed_at=NULL WHERE id=?",
            (submission_id,),
        )
    queue_manager.enqueue(submission_id, force=True)
    return {"ok": True, "submission_id": submission_id, "status": "pending", "message": "已重新加入AI审核队列"}


@router.get("/{submission_id}")
def get_submission(submission_id: int, req: Request):
    """查看单条提交详情。支持登录用户和 guest（guest_session cookie，回退 IP）。"""
    user = auth_service.try_get_user(req)
    guest_session = req.cookies.get("guest_session") or ""
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
        elif guest_session or client_ip:
            # guest：以 guest_session cookie 为主判据，IP 仅作旧数据兼容回退。
            # 只认 IP 会在 CDN 回源场景下误判（两次请求的 client IP 可能不同）。
            if not ((guest_session and sub.get("guest_session") == guest_session)
                    or (client_ip and sub.get("uploader_ip") == client_ip)):
                raise HTTPException(403, "无权查看")
        else:
            raise HTTPException(403, "无权查看")

    sub["media"] = _fill_media_urls(_parse_json(sub.get("media"), []))
    sub["ai_result"] = _parse_json(sub.get("ai_result"))
    sub["matched_rules"] = _fill_rule_evidence(_parse_json(sub.get("matched_rules"), []))
    # 填充 display_name
    if sub.get("user_display_name") and not sub.get("display_name"):
        sub["display_name"] = sub["user_display_name"]
    return sub


# ── 双百战役：AI 推荐赛道 + 上传人确认建档 ──

def _load_own_submission(req: Request, submission_id: int) -> dict:
    """读取提交并校验归属：本人 / 管理员 / 访客可操作。

    访客身份优先认 guest_session cookie，IP 只作旧数据兼容回退。
    """
    user = auth_service.try_get_user(req)
    guest_session = req.cookies.get("guest_session") or ""
    client_ip = req.client.host if req.client else ""
    with db_cursor() as cur:
        cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "提交不存在")
    sub = dict(row)
    allowed = False
    if user and user["role"] == "admin":
        allowed = True
    elif user and sub.get("user_id") == user["id"]:
        allowed = True
    elif not user:
        if guest_session and sub.get("guest_session") == guest_session:
            allowed = True
        elif sub.get("uploader_ip") and sub["uploader_ip"] == client_ip:
            allowed = True
    if not allowed:
        raise HTTPException(403, "无权操作该提交")
    return sub


def _load_for_campaign(req: Request, submission_id: int) -> dict:
    """赛道确认 / 补建档的入口鉴权。

    管理端「从提交补建档」是 manager 点的，而 _load_own_submission 只放行
    管理员 / 上传人本人 / 访客，manager 一律 403——这个入口其实一直点不动。
    这里对 manager / admin 放行任意提交，其余仍按归属校验。
    """
    user = auth_service.try_get_user(req)
    if user and user.get("role") in ("admin", "manager"):
        with db_cursor() as cur:
            cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))
            row = cur.fetchone()
        if not row:
            raise HTTPException(404, "提交不存在")
        return dict(row)
    return _load_own_submission(req, submission_id)


# ── 赛道识别任务表（内存）──
# 识别要先下载视频、抽帧、再调多模态模型，耗时一两分钟，同步返回会被
# CDN 回源超时切断，前端拿到 null 就显示「AI 未能完成识别」。
# 改为提交任务 + GET 轮询结果。
_TRACK_TASKS: dict = {}
_TRACK_TASK_BY_SUB: dict = {}   # submission_id → 最近一次识别任务 id（用于复用处理中的任务）
_TRACK_TASK_LOCK = threading.Lock()
_TRACK_TASK_TTL = 1800  # 结果保留 30 分钟


def _track_task_set(task_id: str, **kw) -> None:
    with _TRACK_TASK_LOCK:
        t = _TRACK_TASKS.get(task_id)
        if t is not None:
            t.update(kw)


def _track_task_prune() -> None:
    now = time.time()
    with _TRACK_TASK_LOCK:
        stale = [k for k, v in _TRACK_TASKS.items() if now - v.get("ts", 0) > _TRACK_TASK_TTL]
        for k in stale:
            _TRACK_TASKS.pop(k, None)
        for sid, tid in list(_TRACK_TASK_BY_SUB.items()):
            if tid not in _TRACK_TASKS:
                _TRACK_TASK_BY_SUB.pop(sid, None)


def _archive_work_for_submission(submission_id: int, track: str) -> str:
    """按赛道给提交建档，返回一稿一码（已建档则返回原有码）。"""
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if track not in valid_tracks:
        return ""
    with db_cursor() as cur:
        cur.execute("SELECT code FROM campaign_works WHERE submission_id=?", (submission_id,))
        row = cur.fetchone()
        if row:
            return row["code"]
        cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))
        sub = cur.fetchone()
    if not sub:
        return ""
    sub = dict(sub)
    from .campaign import _create_campaign_work, _score_work_content_bg
    media = _parse_json(sub.get("media"), [])
    work = _create_campaign_work(
        creator_id=sub.get("user_id"),
        creator_name=sub.get("display_name") or "",
        track=track,
        title=sub.get("title") or sub.get("caption") or "",
        url=sub.get("url") or "",
        media=media,
        region=sub.get("region") or "",
        submission_id=submission_id,
        defer_ai=True,
    )
    threading.Thread(
        target=_score_work_content_bg,
        args=(work["id"], sub.get("title") or sub.get("caption") or "", media),
        daemon=True,
    ).start()
    return work["code"]


def _run_track_task(task_id: str, submission_id: int, caption: str,
                    media: list, tracks: list) -> None:
    """后台线程：跑赛道识别，把结果写回任务表；识别出赛道就直接建档。

    以前识别完只把结果放着等用户点「确认并建档」，用户中途关页面/息屏就再也
    不会回来确认 → 这条提交永远没有赛道、拿不到一稿一码（实测 424 条提交里漏了
    90 条，其中 56 条内容还是审核通过的）。现在识别一有结果就自动建档，
    用户/经理事后想改赛道，走「调整赛道」即可。
    """
    try:
        res = campaign_scoring.recommend_track(caption, media, tracks)
        track = (res.get("track") or "").strip()
        _track_task_set(task_id, status="done", track=track,
                        reason=res.get("reason") or "", error=res.get("error"))
        if track and submission_id:
            code = _archive_work_for_submission(submission_id, track)
            if code:
                _track_task_set(task_id, campaign_code=code, auto_archived=True)
                print(f"[track] 赛道识别完成 #{submission_id} 赛道={track} 码={code}", flush=True)
    except Exception as e:
        print(f"[track] 赛道识别异常: {e}", flush=True)
        _track_task_set(task_id, status="done", error=f"AI 调用失败：{e}")


def _start_track_task(submission_id: int, caption: str, media: list) -> str:
    """创建并启动一次赛道识别任务，返回 task_id。

    同一提交若已有「处理中」的任务，直接复用它的 task_id：上传后由后端自动起过
    一次识别，前端随后 POST 时应接上同一个任务，而不是重复跑一遍模型。
    """
    _track_task_prune()
    with _TRACK_TASK_LOCK:
        old_id = _TRACK_TASK_BY_SUB.get(submission_id)
        old = _TRACK_TASKS.get(old_id) if old_id else None
        if old is not None and old.get("status") == "processing":
            return old_id
    tracks = [t["name"] for t in database.get_tracks()]
    task_id = secrets.token_hex(8)
    with _TRACK_TASK_LOCK:
        _TRACK_TASKS[task_id] = {
            "task_id": task_id, "status": "processing", "tracks": tracks,
            "track": "", "reason": "", "error": None, "ts": time.time(),
        }
        _TRACK_TASK_BY_SUB[submission_id] = task_id
    threading.Thread(
        target=_run_track_task,
        args=(task_id, submission_id, caption, media, tracks), daemon=True,
    ).start()
    return task_id


@router.post("/{submission_id}/recommend-track")
def recommend_track_for_submission(submission_id: int, req: Request):
    """提交 AI 赛道识别任务，立即返回 task_id（结果用 GET 轮询）。

    不同步返回结果：识别要先下载视频、抽帧、再调多模态模型，动辄一两分钟，
    超过 CDN 回源超时会被直接切断，前端只能拿到 null（用户看到的
    「AI 未能完成识别」就是这么来的）。
    """
    sub = _load_own_submission(req, submission_id)
    media = _fill_media_urls(_parse_json(sub.get("media"), []))
    caption = sub.get("caption") or sub.get("title") or ""
    task_id = _start_track_task(submission_id, caption, media)
    with _TRACK_TASK_LOCK:
        t = dict(_TRACK_TASKS.get(task_id) or {})
    return {"task_id": task_id, "status": t.get("status", "processing"),
            "tracks": t.get("tracks", [])}


@router.get("/{submission_id}/recommend-track/{task_id}")
def get_track_task_result(submission_id: int, task_id: str):
    """查询赛道识别任务结果（前端轮询用）。"""
    with _TRACK_TASK_LOCK:
        t = _TRACK_TASKS.get(task_id)
        if t is None:
            raise HTTPException(404, "任务不存在或已过期")
        return dict(t)


@router.post("/{submission_id}/confirm-track")
def confirm_track_for_submission(submission_id: int, payload: dict, req: Request):
    """确认/修正赛道并建档（生成一稿一码）。赛道必选，不能跳过。

    已建档时不再直接返回旧结果：还没 T+7 计分的作品允许改赛道（AI 识别错了、
    上传人自己选错了都能纠）；已计分的作品赛道只影响分组排名，改动请走后台
    「调整赛道」（管理员），这里直接回原值，避免改完对不上已结算的榜单。
    """
    sub = _load_for_campaign(req, submission_id)
    track = (payload.get("track") or "").strip()
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if not track:
        raise HTTPException(400, f"请先选择参与双百战役的赛道，可选：{' / '.join(valid_tracks)}")
    if track not in valid_tracks:
        raise HTTPException(400, f"赛道不合法，可选：{' / '.join(valid_tracks)}")

    # 已建档：同赛道直接返回；不同赛道且在待回传阶段则允许修正
    with db_cursor() as cur:
        cur.execute("SELECT id, code, track, status FROM campaign_works WHERE submission_id=?",
                    (submission_id,))
        exist = cur.fetchone()
    if exist:
        old_track = exist["track"] or ""
        if old_track == track:
            return {"ok": True, "campaign_code": exist["code"], "track": old_track, "already": True}
        if exist["status"] != "pending_t7":
            return {"ok": True, "campaign_code": exist["code"], "track": old_track,
                    "already": True, "note": "该作品已计分，如需改赛道请走后台「调整赛道」"}
        with db_cursor() as cur:
            cur.execute("UPDATE campaign_works SET track=?, updated_at=? WHERE id=?",
                        (track, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), exist["id"]))
        return {"ok": True, "campaign_code": exist["code"], "track": track,
                "updated": True, "old_track": old_track}

    user = auth_service.try_get_user(req)
    from .campaign import _create_campaign_work, _score_work_content_bg
    media = _parse_json(sub.get("media"), [])
    # defer_ai=True：建档只写库，立刻把一稿一码返回。多模态打分同样要一两分钟，
    # 同步等会被 CDN 回源超时切断（点「确认并建档」时表现为失败）。
    work = _create_campaign_work(
        creator_id=sub.get("user_id"),
        creator_name=sub.get("display_name") or (user.get("display_name") if user else "") or "",
        track=track,
        title=sub.get("title") or sub.get("caption") or "",
        url=sub.get("url") or "",
        media=media,
        region=sub.get("region") or "",
        submission_id=submission_id,
        defer_ai=True,
    )
    threading.Thread(
        target=_score_work_content_bg,
        args=(work["id"], sub.get("title") or sub.get("caption") or "", media),
        daemon=True,
    ).start()
    return {"ok": True, "campaign_code": work["code"], "track": track, "work_id": work["id"]}


@router.post("/backfill-campaign")
def backfill_campaign(payload: dict, req: Request):
    """批量给「未参战」的提交补建档（生成一稿一码）。

    ids：要补的提交 id 列表；track 留空则逐条交给 AI 识别赛道后自动建档
    （识别一条要一两分钟，放后台排队跑，作品会陆续出现，刷新即可看到）。
    """
    user = auth_service.require_manager(req)
    ids = payload.get("ids") or []
    if not isinstance(ids, list) or not ids:
        raise HTTPException(400, "请选择要补建档的提交")
    track = (payload.get("track") or "").strip()
    valid_tracks = [t["name"] for t in database.get_tracks()]
    if track and track not in valid_tracks:
        raise HTTPException(400, f"赛道不合法，可选：{' / '.join(valid_tracks)}")

    archived, skipped, queued = 0, 0, []
    for raw in ids[:500]:
        try:
            sid = int(raw)
        except (TypeError, ValueError):
            continue
        with db_cursor() as cur:
            cur.execute("SELECT code FROM campaign_works WHERE submission_id=?", (sid,))
            if cur.fetchone():
                skipped += 1
                continue
            cur.execute("SELECT * FROM submissions WHERE id=?", (sid,))
            sub = cur.fetchone()
        if not sub:
            skipped += 1
            continue
        sub = dict(sub)
        if track:
            if _archive_work_for_submission(sid, track):
                archived += 1
            else:
                skipped += 1
        else:
            media = _fill_media_urls(_parse_json(sub.get("media"), []))
            _start_track_task(sid, sub.get("caption") or sub.get("title") or "", media)
            queued.append(sid)
    database.log_usage(
        "批量补建档", (user.get("display_name") or user.get("username") or ""), "",
        req.client.host if req.client else "", None,
        f"指定赛道={track or '(由 AI 逐条识别)'}; 已建档={archived}; 跳过={skipped}; 排队识别={len(queued)}",
    )
    return {"ok": True, "archived": archived, "skipped": skipped,
            "queued": len(queued), "queued_ids": queued}


@router.put("/{submission_id}/review")
def review_submission(submission_id: int, payload: dict, req: Request):
    """人工审核：manager/admin 直接判定通过或驳回。

    不限当前状态——AI 判驳回但人工确认没问题、或已通过的被人工驳回，都要能改判
    （payload.manual=true 时在日志里标注「人工直接改判」，方便事后追查是谁把
    状态从什么改成了什么）。
    """
    user = auth_service.require_manager(req)
    action = (payload.get("action") or "").strip()
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action 必须是 approve 或 reject")

    with db_cursor() as cur:
        cur.execute("SELECT id, title, display_name, region, status FROM submissions WHERE id=?",
                    (submission_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "提交不存在")
        sub_info = dict(row)
        old_status = sub_info.get("status") or ""
        new_status = "approved" if action == "approve" else "rejected"
        new_risk = "none" if action == "approve" else "forbidden"
        action_label = "复核通过" if action == "approve" else "复核驳回"
        who = user.get("display_name") or user.get("username", "")
        cur.execute(
            """UPDATE submissions SET status=?, risk_level=?,
               review_comment=?, reviewed_by=?, reviewed_at=datetime('now','localtime')
               WHERE id=?""",
            (new_status, new_risk, payload.get("comment"), user["id"], submission_id),
        )
        # 写审核日志（格式与后台「审核记录」的解析保持一致，便于直接展示）
        detail = (f"审核人: {who}; 标题: {sub_info.get('title','')}; "
                  f"状态: {old_status} → {new_status}"
                  + ("; 来源: 人工直接改判" if payload.get("manual") else "")
                  + f"; 备注: {payload.get('comment') or '无'}")
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            (action_label, who, sub_info.get("region", ""),
             req.client.host if req.client else "", submission_id, detail),
        )
    return {"ok": True, "status": new_status, "old_status": old_status}


@router.post("/{submission_id}/violations/{idx}/mark-false-positive")
def mark_violation_false_positive(submission_id: int, idx: int, req: Request):
    """单条违规「这是误报」：人工认定某条 AI 违规是误判，从 matched_rules 移除。

    与"整条通过/驳回"不同——只动这一条违规、其它违规保留、状态按剩余违规重算。
    与"形近错字复判"不同——不调 OCR/AI，纯人工决定，适合"AI 套话、过度判定、字形误读"等场景。
    """
    user = auth_service.require_manager(req)
    # 记下审计人 + 时间 + 可选原因，避免后续 AI 复判或重新分析抹掉人工决定
    note = ""
    try:
        body = json.loads((req.scope.get("body") or b"").decode("utf-8") or "{}")
        note = (body.get("note") or "").strip()
    except Exception:
        pass

    with db_cursor() as cur:
        cur.execute("SELECT id, matched_rules, status, risk_level FROM submissions WHERE id=?", (submission_id,))
        sub = cur.fetchone()
        if not sub:
            raise HTTPException(404, "提交不存在")
        sub = dict(sub)
        matched = _parse_json(sub.get("matched_rules"), [])
        if not (0 <= idx < len(matched)):
            raise HTTPException(400, f"违规下标 {idx} 超出范围（共 {len(matched)} 条）")
        target = dict(matched[idx])  # 复制避免污染
        target["false_positive"] = True
        target["false_positive_by"] = user.get("display_name") or user.get("username", "")
        target["false_positive_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if note:
            target["false_positive_note"] = note[:500]
        matched.pop(idx)  # 直接移除（不是标记后留着），简化后续状态计算

        # 状态重算：和 recheck_typo_violation 同一套规则——剩余违规的最高 severity 决定 status
        new_status, new_risk = _recompute_status_from_violations(matched, sub.get("status"))
        cur.execute(
            "UPDATE submissions SET matched_rules=?, status=?, risk_level=? WHERE id=?",
            (json.dumps(matched, ensure_ascii=False), new_status, new_risk, submission_id),
        )
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("违规标误报", user.get("display_name") or user.get("username", ""),
             "", req.client.host if req.client else "", submission_id,
             f"原违规：{target.get('rule_name')}/{target.get('matched_text','')[:60]}; 备注: {note or '无'}"),
        )
    return {"ok": True, "new_status": new_status, "new_risk": new_risk, "remaining": len(matched)}


def _recompute_status_from_violations(matched, prev_status):
    """根据剩余违规重新计算 submissions.status / risk_level。

    必须与主流程（queue_manager 的红黄绿灯）同一口径：
    空 / 仅 low → approved/none；存在 high / forbidden → rejected。
    之前这里把 high/forbidden 落成 manual_review，导致出现「risk_level=forbidden
    但状态是待复核」这种自相矛盾的记录（人工复判、标记误报后重算时会踩到）。
    """
    if not matched:
        return "approved", "none"
    sev_rank = {"forbidden": 4, "high": 3, "medium": 2, "low": 1}
    max_sev = max((sev_rank.get(str(m.get("severity", "low")).lower(), 1) for m in matched), default=0)
    if max_sev <= 1:
        return "approved", "none"
    if max_sev >= 3:
        return "rejected", "forbidden" if max_sev == 4 else "high"
    return "manual_review", "medium"


@router.post("/{submission_id}/recheck-typo")
def recheck_typo_violation(submission_id: int, req: Request):
    """形近错字违规的「OCR 取证 + AI 复判」二次确认。

    按启发式找出命中 `needs_ocr_review` 的违规，逐条调多模态模型做 OCR 取证
    + 复判，按 verdict 决定是丢弃还是保留。结果直接写进 submissions.matched_rules
    的对应条目（recheck_verdict / recheck_evidence / recheck_ocr / recheck_at），
    并在违规被丢弃时同步把 submissions.status / risk_level 改写。

    返回 {"rechecks": [{violation_index, verdict, reason, ...}, ...], "summary": {...}}
    """
    user = auth_service.require_manager(req)

    with db_cursor() as cur:
        cur.execute(
            "SELECT id, title, caption, media, matched_rules, status, risk_level "
            "FROM submissions WHERE id=?",
            (submission_id,),
        )
        sub = cur.fetchone()
        if not sub:
            raise HTTPException(404, "提交不存在")
        sub = dict(sub)
        matched = _parse_json(sub.get("matched_rules"), [])
        if not matched:
            return {"rechecks": [], "summary": {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}}

    # 收集所有可作为「画面」的图（视频关键帧、封面图、原始图片）
    # 注意：视频素材不能当图片发给模型（会报 image format is illegal / 超过 64MB），
    # 所以这里跳过 is_video 的素材；视频优先用下面的「违规截图」。
    media = _fill_media_urls(_parse_json(sub.get("media"), []))
    image_urls: list = []
    for m in media or []:
        if not isinstance(m, dict) or m.get("is_video"):
            continue
        for k in ("frame_url", "frame_cos_url", "cos_url", "cover_url", "url", "preview_url"):
            v = m.get(k)
            if isinstance(v, str) and v.startswith("http"):
                image_urls.append(v)
                break

    full_text = " / ".join(
        t for t in (sub.get("title"), sub.get("caption"))
        if isinstance(t, str) and t.strip()
    )

    # 仅对 needs_ocr_review=true 的违规做二次确认
    targets = [(i, v) for i, v in enumerate(matched) if v.get("needs_ocr_review") and v.get("recheck_verdict") not in ("uphold", "discard")]
    if not targets:
        return {"rechecks": [], "summary": {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}}

    rechecks: list = []
    summary = {"total": 0, "uphold": 0, "discard": 0, "uncertain": 0}
    discard_indices: list = []  # verdict=discard 的违规下标，事务里统一从 matched 拿掉
    new_status, new_risk = sub.get("status"), sub.get("risk_level")

    for idx, v in targets:
        # 取图：优先该条违规自己的画面截图，其次图片类素材（视频不参与）
        imgs = ai_auditor.evidence_image_inputs(v, media=media) or image_urls
        if not imgs:
            res = {"verdict": "uncertain", "reason": "没有可用的画面截图，请人工核对原片",
                   "ocr_seen": [], "standard_seen": [], "ocr_raw": []}
            verdict = res["verdict"]
            summary["total"] += 1
            summary[verdict] = summary.get(verdict, 0) + 1
            v["recheck_verdict"] = verdict
            v["recheck_reason"] = res["reason"]
            continue
        try:
            res = ai_auditor.recheck_typo_violation(
                image_urls=imgs,
                first_evidence=str(v.get("matched_text") or v.get("evidence") or ""),
                first_rule_name=str(v.get("rule_name") or ""),
                first_field=str(v.get("field") or "").rstrip("（AI）"),
                full_text=full_text,
            )
        except Exception as e:
            res = {"verdict": "uncertain", "reason": f"复判异常：{e}",
                   "ocr_seen": [], "standard_seen": [], "ocr_raw": [],
                   "model": "", "error": str(e)}

        verdict = res.get("verdict", "uncertain")
        summary["total"] += 1
        summary[verdict] = summary.get(verdict, 0) + 1

        # 把复判结果写回 matched_rules 条目
        v["recheck_verdict"] = verdict
        v["recheck_reason"] = res.get("reason", "")
        v["recheck_ocr"] = res.get("ocr_text_all", "") or " / ".join(res.get("ocr_raw") or [])
        v["recheck_ocr_seen"] = res.get("ocr_seen") or []
        v["recheck_standard_seen"] = res.get("standard_seen") or []
        v["recheck_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        v["recheck_model"] = res.get("model", "")

        rechecks.append({
            "violation_index": idx,
            "rule_name": v.get("rule_name"),
            "verdict": verdict,
            "reason": v["recheck_reason"],
            "ocr_seen": v["recheck_ocr_seen"],
            "standard_seen": v["recheck_standard_seen"],
        })

        if verdict == "discard":
            discard_indices.append(idx)
        elif verdict == "uncertain":
            # OCR 看不清/无法确认：不自动驳回，降为 medium 交人工
            if v.get("severity") in ("forbidden", "high"):
                v["severity"] = "medium"
        elif verdict == "uphold" and v.get("severity") in ("forbidden", "high"):
            # 复判确认是真违规：按红黄绿灯规则落为已驳回
            new_risk = "forbidden" if v["severity"] == "forbidden" else "high"
            new_status = "rejected"

    # 把丢弃的违规从 matched 列表里去掉；被降级（uncertain→medium）的也要重算状态，
    # 否则会出现「违规已降级、状态还是已驳回」的不一致。
    if discard_indices:
        discard_set = set(discard_indices)
        matched = [m for i, m in enumerate(matched) if i not in discard_set]
    if discard_indices or any(r["verdict"] == "uncertain" for r in rechecks):
        new_status, new_risk = _recompute_status_from_violations(matched, sub.get("status"))

    with db_cursor() as cur:
        cur.execute(
            "UPDATE submissions SET matched_rules=?, status=?, risk_level=? WHERE id=?",
            (json.dumps(matched, ensure_ascii=False), new_status, new_risk, submission_id),
        )
        # 写日志
        cur.execute(
            "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) VALUES (?,?,?,?,?,?)",
            ("形近错字复判",
             user.get("display_name") or user.get("username", ""),
             "", req.client.host if req.client else "", submission_id,
             f"summary={summary}; verdicts={[r['verdict'] for r in rechecks]}"),
        )

    return {"rechecks": rechecks, "summary": summary,
            "new_status": new_status, "new_risk": new_risk}


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
        # 级联清理：该提交对应的双百战役作品及其子表（campaign_scores/metrics/
        # credentials/propagation/audit_log 均声明了 ON DELETE CASCADE），
        # 否则删除提交后作品仍会留在「全国战况」里。
        cur.execute("DELETE FROM campaign_works WHERE submission_id=?", (submission_id,))
        cur.execute("DELETE FROM submissions WHERE id=?", (submission_id,))
    # 留痕：删除提交会连带删掉双百作品，过去没日志，事后查不到是谁删的
    database.log_usage(
        "删除提交",
        (user.get("display_name") or user.get("username") or ""),
        sub.get("region") or "",
        req.client.host if req.client else "",
        submission_id,
        f"标题={((sub.get('title') or sub.get('caption') or ''))[:60]}; "
        f"上传人={sub.get('display_name') or '(登录用户)'}; 删除人角色={user.get('role')}",
    )
    return {"ok": True}


def _parse_json(val, default=None):
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val if val is not None else default
