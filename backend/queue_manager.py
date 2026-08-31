"""上传审核队列管理器。

使用 asyncio.Queue + Semaphore 控制并发 AI 审核数量。

工作流程：
  1. 用户上传 → 立即入库（status=pending），返回"已提交"
  2. 入队 → Worker 取出 → status=analyzing
  3. AI 审核 → 红黄绿灯判定 → 更新结果
  4. 绿灯→approved / 黄灯→manual_review / 红灯→rejected

并发限制：MAX_CONCURRENT_AI（默认 5），超出则在队列中等待。
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

MAX_CONCURRENT_AI = 5

_queue: asyncio.Queue = asyncio.Queue()
_semaphore: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENT_AI)
_workers_started = False

# ── 智能队列状态追踪 ──
_processing: Dict[int, float] = {}  # submission_id → 开始时间
_avg_time: float = 15.0              # 平均处理时间（秒），动态更新


def enqueue(submission_id: int) -> None:
    """将提交加入审核队列（非阻塞）。"""
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
        }
        ai_violations = detail.get("violations") or []
        hook = lambda c, r, _av=ai_violations: _av

    # 规则引擎判定
    result = rules_engine.analyze_content(content, rule_list, semantic_hook=hook)
    matched = result["matched_rules"]
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

    # ── 黄灯（待人工复核）：推送给对应大区的地在营销经理 ──
    if final_status == "manual_review":
        _push_review_notification(sub, submission_id)


def _save_result(submission_id: int, result: Dict) -> None:
    """将审核结果写入数据库。"""
    with database.db_cursor() as cur:
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


def start_workers() -> None:
    """启动后台 Worker 协程（在 FastAPI startup 中调用一次）。"""
    global _workers_started
    if _workers_started:
        return
    _workers_started = True
    loop = asyncio.get_event_loop()
    for _ in range(MAX_CONCURRENT_AI):
        loop.create_task(_worker())
    print(f"[queue] 审核队列启动，{MAX_CONCURRENT_AI} 个 Worker 就绪", flush=True)
