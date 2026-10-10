"""上传审核队列管理器。

使用 asyncio.Queue + Semaphore 控制并发 AI 审核数量。

工作流程：
  1. 用户上传 → 立即入库（status=pending），返回"已提交"
  2. 入队 → Worker 取出 → status=analyzing
  3. AI 审核 → 红黄绿灯判定 → 更新结果
  4. 绿灯→approved / 黄灯→manual_review / 红灯→rejected

并发限制：MAX_CONCURRENT_AI（默认 2，可按环境变量覆盖），超出则在队列中等待。
该上限由 ai_auditor._AI_GATE 全局生效，队列 worker 与请求线程共用。
"""

import asyncio
import json
import os
import time as _time
import traceback
from typing import Dict, List

import database
import rules_engine
import ai_auditor
import campaign_scoring

# AI 调用的全进程并发闸门在 ai_auditor._AI_GATE（队列 worker 与请求线程共用），
# 避免两处各写一个数字而漂移。
MAX_CONCURRENT_AI = ai_auditor.MAX_CONCURRENT_AI

# 队列 worker 数与 AI 并发闸门分开设置：
# worker 的活不只有"调模型"——还要下载视频、ffmpeg 抽帧（纯 IO/CPU），
# 大视频单个就要 1~2 分钟。若 worker 数 = AI 并发数，这些计算会把槽位占死，
# 后面的人只能干等。所以 worker 多给几个，真正并发的模型调用仍由 _AI_GATE 限制。
QUEUE_WORKERS = int(os.environ.get("QUEUE_WORKERS", "4"))

_queue: asyncio.Queue = asyncio.Queue()
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI)
_workers_started = False

# ── 智能队列状态追踪 ──
_processing: Dict[int, float] = {}  # submission_id → 开始时间
_queued: set = set()                 # 已在队列中等待的 submission_id（入队幂等）
_avg_time: float = 15.0              # 平均处理时间（秒），动态更新


def enqueue(submission_id: int, force: bool = False) -> None:
    """将提交加入审核队列（非阻塞）。force=True 用于管理员强制重审。"""
    if submission_id in _queued or submission_id in _processing:
        if not force:
            return
        return
    _queued.add(submission_id)
    _queue.put_nowait(submission_id)


def queue_size() -> int:
    """返回当前队列中等待的任务数。"""
    return _queue.qsize()


def processing_count() -> int:
    """当前正在处理的任务数。"""
    return len(_processing)


def queue_status() -> Dict:
    """返回队列实时状态，供前端轮询展示。"""
    qs = queue_size()
    pc = processing_count()
    total_waiting = qs + pc
    # 估计等待时间
    est_seconds = qs * _avg_time / MAX_CONCURRENT_AI if _avg_time > 0 else 0
    return {
        "queue_size": qs,
        "processing_count": pc,
        "processing_ids": list(_processing.keys())[:10],
        "max_concurrent": MAX_CONCURRENT_AI,
        "avg_time_seconds": round(_avg_time, 1),
        "estimated_wait_seconds": round(est_seconds, 0),
        "total_waiting": total_waiting,
    }


async def _process_one(submission_id: int) -> None:
    """处理单条提交：AI 审核 → 更新结果。"""
    async with _semaphore:
        _queued.discard(submission_id)
        _processing[submission_id] = _time.time()
        try:
            await asyncio.to_thread(_process_sync, submission_id)
        except Exception as e:
            print(f"[queue] 审核 #{submission_id} 异常: {e}", flush=True)
            traceback.print_exc()
            _save_result(submission_id, {
                "status": "manual_review",
                "risk_level": "medium",
                "ai_result": {"error": str(e)},
                "matched_rules": [],
            })
        finally:
            elapsed = _time.time() - _processing.pop(submission_id, _time.time())
            # 动态更新平均处理时间（指数移动平均）
            global _avg_time
            _avg_time = _avg_time * 0.8 + elapsed * 0.2


def _process_sync(submission_id: int) -> None:
    """同步执行审核（由 asyncio.to_thread 调用，不被 asyncio 锁阻塞）。"""
    database.init_db()

    with database.db_cursor() as cur:
        cur.execute("SELECT * FROM submissions WHERE id=?", (submission_id,))
        row = cur.fetchone()
        if not row:
            return
        sub = dict(row)
        # 记忆机制：已出结论（终态）且结果有效的，不重复送审，避免白烧一次多模态调用。
        # 仅限终态——pending/analyzing 说明还没结论，必须继续审，否则会卡在「分析中」。
        if sub.get("status") not in ("pending", "analyzing") and _has_valid_result(sub):
            print(f"[queue] #{submission_id} 已有审核结果，跳过", flush=True)
            return
        # 标记为分析中
        cur.execute("UPDATE submissions SET status='analyzing' WHERE id=?", (submission_id,))

        # 加载规则
        cur.execute("SELECT * FROM rules WHERE enabled=1")
        rule_list = [dict(r) for r in cur.fetchall()]

    # 准备内容
    media_raw = sub["media"]
    media_list = json.loads(media_raw) if isinstance(media_raw, str) else (media_raw or [])

    # 为上传文件补全 video_local / thumb，以便 AI 审核器能找到文件
    import os as _os
    import subprocess as _sp
    _HERE = _os.path.dirname(__file__)
    for m in media_list:
        if not isinstance(m, dict):
            continue
        fp = m.get("path", "")
        if fp and not fp.startswith("/"):
            fp = _os.path.join(_HERE, fp)
        if not fp or not _os.path.exists(fp):
            continue
        if m.get("is_video"):
            if not m.get("video_local"):
                m["video_local"] = fp
            # 为视频自动提取缩略图（用于 AI 审核兜底）
            if not m.get("thumb"):
                thumb_path = fp + ".thumb.jpg"
                try:
                    _sp.run(
                        ["ffmpeg", "-y", "-i", fp, "-vframes", "1", "-ss", "1",
                         "-q:v", "5", thumb_path],
                        capture_output=True, timeout=15,
                    )
                    if _os.path.exists(thumb_path) and _os.path.getsize(thumb_path) > 100:
                        m["thumb"] = thumb_path  # 绝对路径，ai_auditor 会处理
                except Exception:
                    pass
        else:
            if not m.get("thumb"):
                m["thumb"] = fp
            if not m.get("path"):
                m["path"] = fp

    content = {
        "caption": sub["caption"] or sub["title"] or "",
        "transcript": "",
        "ocr_text": "",
        "media": media_list,
    }

    # AI 审核（如果配置了模型）
    ai_result = None
    hook = None
    if ai_auditor.is_ready():
        detail = ai_auditor.audit(content, rule_list, ai_auditor.load_config())
        ai_result = {
            "model": detail.get("model"),
            "interpretation": detail.get("interpretation", ""),
            "conclusion": detail.get("conclusion", ""),
            "error": detail.get("error"),
            "violations": detail.get("violations") or [],
            # 画面文字 / 语音：后台详情要用，之前没落库导致这两项永远是空的
            "ocr_text": detail.get("ocr_text", ""),
            "transcript": detail.get("transcript", ""),
        }
        ai_violations = detail.get("violations") or []
        hook = lambda c, r, _av=ai_violations: _av

    # 规则引擎判定
    result = rules_engine.analyze_content(content, rule_list, semantic_hook=hook)
    matched = result["matched_rules"]
    # 最终状态只看过滤后的真实违规列表，不能只看 AI conclusion 文本。
    # 没有真实违规时，必须清掉旧风险并落为合规。
    if not matched:
        risk = "none"
        status = "compliant"
        result["risk_level"] = risk
        result["status"] = status
        result["summary"] = "未命中任何合规规则，内容合规。"
    else:
        risk = result["risk_level"]
        status = result["status"]  # compliant / violation

    # 红黄绿灯转换
    if status == "violation":
        if risk in ("forbidden", "high"):
            final_status = "rejected"       # 红灯：直接违规
        else:
            final_status = "manual_review"  # 黄灯：需人工复核
    else:
        final_status = "approved"           # 绿灯：合规通过

    _save_result(submission_id, {
        "status": final_status,
        "risk_level": risk,
        "ai_result": ai_result,
        "matched_rules": matched,
        "summary": result.get("summary", ""),
    })

    # ── 若该提交参与双百战役，顺带做内容表现打分（情理色诚），写入 campaign_scores ──
    try:
        _score_campaign_content(submission_id, content)
    except Exception as e:
        print(f"[queue] 内容表现打分异常 #{submission_id}: {e}", flush=True)

    # ── 黄灯（待人工复核）：推送给对应大区的地在营销经理 ──
    if final_status == "manual_review":
        _push_review_notification(sub, submission_id)

    # 审核已完成，本地副本可以回收（COS 已确认有同一份对象）。
    # 上传接口不再同步删本地文件——那样会让大文件上传卡住导致 504，
    # 改成此刻清理，既省磁盘又不影响审核读取。
    _cleanup_local_media(submission_id, media_list)


def _cleanup_local_media(submission_id: int, media_list: list) -> None:
    """删除已确认上传到 COS 的本地副本，节省磁盘空间。

    cos_ready 由上传接口的后台线程异步回填，可能晚于审核完成，
    因此这里重新读一次库拿最新 media，否则会漏掉清理、白占磁盘。
    """
    import os as _os
    _HERE = _os.path.dirname(__file__)
    try:
        with database.db_cursor() as cur:
            cur.execute("SELECT media FROM submissions WHERE id=?", (submission_id,))
            row = cur.fetchone()
            if row:
                fresh = json.loads(row["media"] or "[]")
                if fresh:
                    media_list = fresh
    except Exception:
        pass
    for m in media_list:
        if not isinstance(m, dict) or not m.get("cos_ready"):
            continue
        p = m.get("video_local") or m.get("path") or ""
        if not p:
            continue
        fp = p if p.startswith("/") else _os.path.join(_HERE, p)
        try:
            if _os.path.exists(fp):
                _os.remove(fp)
        except OSError:
            pass


def _has_valid_result(sub: dict) -> bool:
    """该提交是否已有有效审核结果 —— 有则不再重复送审（记忆机制）。

    与采集路径（main.py 的 _background_ai_analysis）同一判据：
      - 已人工复核（reviewed_by 非空）：人工结论优先，AI 不覆盖、不重审；
      - 已有 AI 结果且无 error：审过一次且成功，不重审。
    注意「有 error」时视为无效，允许重试（正是 openai 缺失那类故障的恢复路径）。
    """
    if sub.get("reviewed_by"):
        return True
    raw = sub.get("ai_result")
    if not raw:
        return False
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return False
    return bool(data) and not data.get("error")


def _save_result(submission_id: int, result: Dict) -> None:
    """将审核结果写入数据库。已被人工复核的提交不覆盖（人工结论优先）。"""
    with database.db_cursor() as cur:
        cur.execute("SELECT reviewed_by FROM submissions WHERE id=?", (submission_id,))
        row = cur.fetchone()
        if row and row["reviewed_by"]:
            print(f"[queue] #{submission_id} 已被人工复核，AI 结果不覆盖", flush=True)
            return
        cur.execute(
            """UPDATE submissions SET
               status=?, risk_level=?,
               ai_result=?, matched_rules=?
               WHERE id=?""",
            (
                result["status"],
                result["risk_level"],
                json.dumps(result.get("ai_result"), ensure_ascii=False) if result.get("ai_result") else None,
                json.dumps(result.get("matched_rules") or [], ensure_ascii=False),
                submission_id,
            ),
        )


def _score_campaign_content(submission_id: int, content: dict) -> None:
    """双百战役内容表现打分（情理色诚 0-40）的兜底补算。

    建档时已由 campaign._create_campaign_work 打过一次分，这里只在「缺失」时补算，
    避免同一条内容被重复送审（一次多模态调用要传完整视频，成本最高）。
    判分文本统一取 campaign_works.title，与建档、T+7 三处口径保持一致。
    """
    with database.db_cursor() as cur:
        cur.execute("SELECT id, title, media FROM campaign_works WHERE submission_id=?", (submission_id,))
        row = cur.fetchone()
        if not row:
            return
        work_id = row["id"]
        # 已有内容表现分 → 建档时已打过，直接跳过，不重复调用模型
        cur.execute("SELECT ai_content_score FROM campaign_scores WHERE work_id=?", (work_id,))
        scored = cur.fetchone()
        if scored and scored["ai_content_score"] is not None:
            return

    title = (row["title"] or "").strip() or (content.get("caption") or "")
    try:
        media = json.loads(row["media"]) if row["media"] else []
    except Exception:
        media = []
    if not media:
        media = content.get("media") or []

    res = campaign_scoring.ai_score_content_quality(title, media)
    if res.get("error"):
        print(f"[queue] 内容表现打分失败 #{submission_id}: {res['error']}", flush=True)
        return

    with database.db_cursor() as cur:
        cur.execute(
            """INSERT INTO campaign_scores (work_id, ai_content_score, ai_dims, ai_reason)
               VALUES (?,?,?,?)
               ON CONFLICT(work_id) DO UPDATE SET
                 ai_content_score=excluded.ai_content_score,
                 ai_dims=excluded.ai_dims,
                 ai_reason=excluded.ai_reason""",
            (work_id, res["score"], json.dumps(res["dims"], ensure_ascii=False), res["reason"]),
        )
    print(f"[queue] 内容表现打分完成 #{submission_id} work={work_id} score={res['score']}", flush=True)


def _push_review_notification(sub: dict, submission_id: int) -> None:
    """推送黄灯复核通知给对应大区的在地营销经理。"""
    title = (sub.get("caption") or sub.get("title") or f"提交#{submission_id}")[:30]
    region_full = sub.get("region", "")
    big_region = region_full.split("/")[0].strip() if region_full else ""
    sub_region = region_full.split("/")[-1].strip() if "/" in region_full else region_full

    text = f"来自 {big_region}/{sub_region} 的新内容需要复核：{title}"

    with database.db_cursor() as cur:
        cur.execute(
            "INSERT INTO notifications (user_id, target_role, target_region, type, icon, text) VALUES (?,?,?,?,?,?)",
            (None, "manager", big_region or None, "manual_review", "⚠️", text),
        )
    print(f"[queue] 已推送复核通知 → manager / {big_region}: {text}", flush=True)


async def _worker() -> None:
    """后台 Worker：从队列取任务并处理。"""
    while True:
        submission_id = await _queue.get()
        try:
            await _process_one(submission_id)
        except Exception:
            pass
        finally:
            _queue.task_done()


def _recover_unfinished() -> None:
    """把上次进程退出时未处理完的提交重新入队。

    内存队列不持久化：服务重启/崩溃会让排队中的提交永久停在 pending，
    这里以 DB 为唯一事实来源做一次恢复（analyzing 是上次处理到一半的，同样重跑）。
    已有有效结果的会被 _process_sync 的记忆机制跳过，不会重复送审。
    """
    try:
        with database.db_cursor() as cur:
            cur.execute(
                "SELECT id FROM submissions WHERE status IN ('pending','analyzing') ORDER BY id"
            )
            ids = [r["id"] for r in cur.fetchall()]
        for sid in ids:
            enqueue(sid)
        if ids:
            print(f"[queue] 恢复未处理提交 {len(ids)} 条: {ids}", flush=True)
    except Exception as e:
        print(f"[queue] 恢复未处理提交失败: {e}", flush=True)


async def _periodic_recover(interval: int = 300) -> None:
    """定期把仍停留在 pending/analyzing 的提交重新入队。

    上传响应发出后才跑的后台任务（画面指纹判重 → 入队）在客户端提前断开等极端情况下
    可能没跑到，只靠启动时恢复会让这类提交一直卡在 pending。enqueue 对已在队列/正在
    处理的 id 有幂等保护，重复入队不会重复送审。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            _recover_unfinished()
        except Exception as e:
            print(f"[queue] 定期恢复失败: {e}", flush=True)


def start_workers() -> None:
    """启动后台 Worker 协程（在 FastAPI startup 中调用一次）。"""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    loop = asyncio.get_event_loop()
    for _ in range(QUEUE_WORKERS):
        loop.create_task(_worker())
    _recover_unfinished()
    loop.create_task(_periodic_recover())
    print(f"[queue] 审核队列启动，{QUEUE_WORKERS} 个 Worker 就绪"
          f"（模型并发上限 {MAX_CONCURRENT_AI}）", flush=True)
