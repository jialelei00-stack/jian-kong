"""SQLite 数据库连接与表结构初始化。"""
import json
import sqlite3
import os
from contextlib import contextmanager
from contextvars import ContextVar

_atomic_connection = ContextVar('atomic_connection', default=None)

from runtime_utils import normalize_region

DB_PATH = os.path.join(os.path.dirname(__file__), "jiankong.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)  # 并发安全
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # WAL模式：读写不互斥，避免 database is locked
    conn.execute("PRAGMA busy_timeout = 10000")  # 繁忙等待10秒
    return conn


@contextmanager
def db_cursor():
    active = _atomic_connection.get()
    if active is not None:
        yield active.cursor()
        return
    conn = get_connection()
    try:
        yield conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def atomic_cursor():
    conn = get_connection()
    token = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        token = _atomic_connection.set(conn)
        yield conn.cursor()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if token is not None:
            _atomic_connection.reset(token)
        conn.close()


def init_db() -> None:
    """创建所有数据表（若不存在）。"""
    with db_cursor() as cur:
        # 视频号
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                wechat_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',  -- pending/collecting/done/error
                last_collected_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        # 合规规则
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '通用',
                rule_type TEXT NOT NULL DEFAULT 'keyword',  -- keyword/regex
                pattern TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'medium',     -- high/medium/low
                description TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        # 采集到的视频内容
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                title TEXT,
                video_url TEXT,
                caption TEXT,          -- 文案/标题文字
                transcript TEXT,       -- 语音转写文本
                ocr_text TEXT,         -- 画面 OCR 文字
                publish_time TEXT,
                collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            )
            """
        )
        # 合规分析结果
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS analysis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending',  -- compliant/violation/pending
                risk_level TEXT NOT NULL DEFAULT 'none', -- high/medium/low/none
                matched_rules TEXT,                       -- JSON: 命中的规则明细
                summary TEXT,
                analyzed_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (content_id) REFERENCES contents(id) ON DELETE CASCADE
            )
            """
        )
        # 迁移：contents 增加 media 字段，存作品媒体资源 JSON
        # （封面/图片/视频缩略图本地路径 + 原始地址），用于看板直观展示画面内容。
        cols = [r["name"] for r in cur.execute("PRAGMA table_info(contents)").fetchall()]
        if "media" not in cols:
            cur.execute("ALTER TABLE contents ADD COLUMN media TEXT")
        if "stats" not in cols:
            # 作品互动数据 JSON：观看/点赞/评论/转发/收藏等平台真实统计值
            cur.execute("ALTER TABLE contents ADD COLUMN stats TEXT")

        # 迁移：analysis_results 增加 ai_review 字段，存大模型对内容的
        # 解读、审核结论与（若失败）错误信息 JSON，用于看板直观呈现 AI 分析过程。
        ar_cols = [r["name"] for r in cur.execute("PRAGMA table_info(analysis_results)").fetchall()]
        if "ai_review" not in ar_cols:
            cur.execute("ALTER TABLE analysis_results ADD COLUMN ai_review TEXT")
        # 形近错字二次确认（OCR 取证 + AI 复判）结果，存全表最后一次
        if "recheck_at" not in ar_cols:
            cur.execute("ALTER TABLE analysis_results ADD COLUMN recheck_at TEXT")
        if "recheck_summary" not in ar_cols:
            # JSON：{"total":N,"uphold":M,"discard":K,"uncertain":U}，给看板用
            cur.execute("ALTER TABLE analysis_results ADD COLUMN recheck_summary TEXT")

        # 品牌参考图：4 个标准品牌的产品包装图 + 多模态模型描述的字形指纹
        # audit() 调模型时把描述写进 system prompt —— 比每条都附图省 token
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_ref_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL UNIQUE,   -- 南孚 / 益圆 / 丰蓝 / 传应
                cos_key TEXT NOT NULL,        -- 原图，存 COS 供人核验
                learned_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                note TEXT                     -- 哪个用户上传的备注
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_ref_descriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL UNIQUE,    -- 南孚 / 益圆 / 丰蓝 / 传应
                description TEXT NOT NULL,     -- 多模态模型生成的字形指纹 + 包装布局描述
                model TEXT,                    -- 用的什么模型
                generated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )

        # 迁移：analysis_results 增加 manual_status 字段，
        # 允许人工覆盖审核结果（null=无覆盖，"compliant"=强制合规，"violation"=强制违规）
        if "manual_status" not in ar_cols:
            cur.execute("ALTER TABLE analysis_results ADD COLUMN manual_status TEXT")

        # 迁移：channels 增加自定义分类字段（区域 + 账号归属者），用于看板筛选。
        ch_cols = [r["name"] for r in cur.execute("PRAGMA table_info(channels)").fetchall()]
        if "region" not in ch_cols:
            cur.execute("ALTER TABLE channels ADD COLUMN region TEXT")
        if "owner" not in ch_cols:
            cur.execute("ALTER TABLE channels ADD COLUMN owner TEXT")

        # 迁移：contents 增加 object_id 字段，存微信视频号作品 objectId，
        # 用于重新采集时匹配已有内容、保留审核结果。
        c_cols = [r["name"] for r in cur.execute("PRAGMA table_info(contents)").fetchall()]
        if "object_id" not in c_cols:
            cur.execute("ALTER TABLE contents ADD COLUMN object_id TEXT")

        # 通用键值配置表（存第三方大模型配置等）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        # ── 用户系统 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'uploader',
                region TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        cur.execute("SELECT COUNT(*) AS c FROM users")
        if cur.fetchone()["c"] == 0:
            import hashlib as _hl, os as _os
            salt = _os.urandom(16)
            h = _hl.pbkdf2_hmac("sha256", b"admin123", salt, 100000).hex()
            cur.execute(
                "INSERT INTO users (username, password_hash, display_name, role) VALUES (?,?,?,?)",
                ("admin", salt.hex() + ":" + h, "超级管理员", "admin"),
            )

        # ── 用户提交表 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                display_name TEXT,
                guest_session TEXT,
                title TEXT,
                caption TEXT,
                media TEXT,
                region TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                risk_level TEXT NOT NULL DEFAULT 'none',
                ai_result TEXT,
                matched_rules TEXT,
                review_comment TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        # 迁移：submissions 增加 nullable user_id（允许 guest 提交无 user）
        sub_cols = [r["name"] for r in cur.execute("PRAGMA table_info(submissions)").fetchall()]
        if "display_name" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN display_name TEXT")
        if "guest_session" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN guest_session TEXT")
        if "uploader_ip" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN uploader_ip TEXT")
        # 迁移：user_id 改为 nullable（旧表可能是 NOT NULL 且有 FK CASCADE）
        col_info = {r["name"]: dict(r) for r in cur.execute("PRAGMA table_info(submissions)").fetchall()}
        if col_info.get("user_id", {}).get("notnull", 0) == 1:
            # 重建表以便修改 user_id 约束
            cur.execute("PRAGMA foreign_keys = OFF")
            cur.execute("""
                CREATE TABLE submissions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    display_name TEXT,
                    guest_session TEXT,
                    title TEXT,
                    caption TEXT,
                    media TEXT,
                    region TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    risk_level TEXT NOT NULL DEFAULT 'none',
                    ai_result TEXT,
                    matched_rules TEXT,
                    review_comment TEXT,
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY (reviewed_by) REFERENCES users(id) ON DELETE SET NULL
                )
            """)
            cur.execute("INSERT INTO submissions_new SELECT id, user_id, display_name, guest_session, title, caption, media, region, status, risk_level, ai_result, matched_rules, review_comment, reviewed_by, reviewed_at, created_at FROM submissions")
            cur.execute("DROP TABLE submissions")
            cur.execute("ALTER TABLE submissions_new RENAME TO submissions")
            cur.execute("PRAGMA foreign_keys = ON")
        # 迁移：submissions 增加作品链接
        sub_cols = [r["name"] for r in cur.execute("PRAGMA table_info(submissions)").fetchall()]
        if "url" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN url TEXT")
        # 迁移：上传文件内容哈希（sha256 前 32 位），用于拦截同一视频重复提交
        if "content_hash" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN content_hash TEXT")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_content_hash ON submissions(content_hash)")
        # 迁移：视频画面指纹，用于识别被微信/剪辑软件重新编码后的同一视频
        if "content_fingerprint" not in sub_cols:
            cur.execute("ALTER TABLE submissions ADD COLUMN content_fingerprint TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sub_content_fingerprint ON submissions(content_fingerprint)")
        # 迁移：T+7 指标增加「转发次数」。此处表可能尚未创建（全新库会按上面的
        # 建表语句直接带上该列），故先判表存在再判列。
        metric_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_metrics)").fetchall()]
        if metric_cols and "share_count" not in metric_cols:
            cur.execute("ALTER TABLE campaign_metrics ADD COLUMN share_count INTEGER")
        # 迁移：凭证记录归属「第几次回传」。历史功能上线前的老凭证统一记为第 1 次
        cred_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_credentials)").fetchall()]
        if cred_cols and "seq" not in cred_cols:
            cur.execute("ALTER TABLE campaign_credentials ADD COLUMN seq INTEGER")
            cur.execute("UPDATE campaign_credentials SET seq=1 WHERE seq IS NULL")


        # ── 登录令牌表 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_tokens (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )


        # ── 通知表（手动推送通知）──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target_role TEXT,
                target_region TEXT,
                type TEXT NOT NULL,
                icon TEXT DEFAULT '📌',
                text TEXT NOT NULL,
                read INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )

        # ── 使用日志表（记录公开上传页的每次提交，不创建用户）──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL DEFAULT 'upload',
                display_name TEXT NOT NULL,
                region TEXT,
                ip TEXT,
                submission_id INTEGER,
                detail TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )

        # ── 双百战役：作品表（两段式上传）──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_works (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,          -- 一稿一码 ID
                creator_id INTEGER,                 -- 创作者（关联 users）
                creator_name TEXT,                  -- 冗余显示名
                track TEXT NOT NULL,                -- 赛道
                title TEXT,
                url TEXT,                           -- 作品链接
                media TEXT,                         -- 素材 JSON
                region TEXT,                        -- 区域
                submission_id INTEGER,              -- 关联原有上传提交（submissions.id）
                stage TEXT NOT NULL DEFAULT 'first',    -- first=首次建档 / second=T+7已回填
                status TEXT NOT NULL DEFAULT 'pending_t7', -- pending_t7/qualified/eliminated/disqualified
                qualified INTEGER NOT NULL DEFAULT 0,   -- 是否满足参评门槛
                reject_reason TEXT,                 -- 不满足门槛原因
                publish_time TEXT,                  -- 首次上传时间（用于 T+7 判断）
                extra_flags TEXT,                   -- 附加战功开关 JSON
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT,
                FOREIGN KEY (creator_id) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        # 迁移：campaign_works 增加 submission_id 字段（关联原有上传提交）
        cw_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_works)").fetchall()]
        if "submission_id" not in cw_cols:
            cur.execute("ALTER TABLE campaign_works ADD COLUMN submission_id INTEGER")
        # ── 双百战役：T+7 二次提交指标 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_metrics (
                work_id INTEGER PRIMARY KEY,
                plays INTEGER,                      -- 自然播放
                retention_3s REAL,                  -- 3秒留存率 %
                completion_rate REAL,               -- 完播率 %
                comment_count INTEGER,              -- 评论个数（2026-09 起由「深度互动率%」改成个数）
                like_count INTEGER,                 -- 爱心个数（2026-09 起由「点赞率%」改成个数）
                share_count INTEGER,                -- 转发次数（直接按次数套档位；转发率只作展示）
                screenshot TEXT,                    -- 后台截图 JSON
                business_proof TEXT,                -- 业务凭证 JSON
                ocr_raw TEXT,                       -- AI 从后台截图识别出的原始数值 JSON（供后台比对用户是否改过）
                ocr_at TEXT,                        -- 识别时间
                submitted_at TEXT,
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
        )
        # 迁移：指标表补上「AI 识别原值」（老数据为空）
        cm_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_metrics)").fetchall()]
        for _col, _ddl in (
            ("ocr_raw", "ALTER TABLE campaign_metrics ADD COLUMN ocr_raw TEXT"),
            ("ocr_at", "ALTER TABLE campaign_metrics ADD COLUMN ocr_at TEXT"),
        ):
            if _col not in cm_cols:
                cur.execute(_ddl)
        # 迁移：深度互动率% → 评论个数、点赞率% → 爱心个数（量纲从「率」改成「个数」）
        # 旧列还在才执行，重命名后旧列消失 → 天然幂等，不会重复跑
        if "deep_interaction_rate" in cm_cols or "like_rate" in cm_cols:
            if "deep_interaction_rate" in cm_cols:
                cur.execute("ALTER TABLE campaign_metrics RENAME COLUMN deep_interaction_rate TO comment_count")
            if "like_rate" in cm_cols:
                cur.execute("ALTER TABLE campaign_metrics RENAME COLUMN like_rate TO like_count")
            # 旧列存的是百分数（如 2.0 表示 2%），跟「个数」不是一个量纲；
            # 留着会被当成「2 条评论」展示，直接清空比留着错值安全。
            cur.execute("UPDATE campaign_metrics SET comment_count=NULL, like_count=NULL")
        # ── 双百战役：T+7 回传历史（同一作品可多次回传，每次留档）──
        # campaign_metrics 仍只存"最新一次"（列表/详情/榜单口径不变），
        # 这张表存每一次的完整快照，供后台查看历次回传的数据与分数变化。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_metrics_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                seq INTEGER NOT NULL,               -- 第几次回传，从 1 开始
                plays INTEGER,
                retention_3s REAL,
                completion_rate REAL,
                comment_count INTEGER,              -- 评论个数
                like_count INTEGER,                 -- 爱心个数
                share_count INTEGER,
                screenshot TEXT,                    -- 本次提交的后台数据截图 JSON
                business_proof TEXT,                -- 本次提交的有效线索凭证 JSON
                ocr_raw TEXT,                       -- 本次 AI 识别出的原始数值 JSON
                ai_content_score REAL,
                retention_score REAL,
                interaction_score REAL,
                total_score REAL,
                grade TEXT,
                total_merit INTEGER,
                qualified INTEGER DEFAULT 1,        -- 本次是否达标计分
                reject_reason TEXT,
                submitter TEXT,                     -- 本次回传人姓名
                submitted_at TEXT,
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cmh_work ON campaign_metrics_history(work_id, seq)"
        )
        cur.execute("""CREATE TABLE IF NOT EXISTS campaign_submit_dedup (
            work_id INTEGER NOT NULL, request_key TEXT NOT NULL, submitted_at INTEGER NOT NULL,
            response TEXT NOT NULL, PRIMARY KEY(work_id, request_key),
            FOREIGN KEY(work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS campaign_metric_corrections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, work_id INTEGER NOT NULL,
            history_id INTEGER NOT NULL, admin_id INTEGER NOT NULL,
            reason TEXT NOT NULL, before_data TEXT NOT NULL, after_data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            FOREIGN KEY(work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
        )""")
        # 迁移：历史快照表补上「本次 AI 识别原值」
        cmh_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_metrics_history)").fetchall()]
        if "ocr_raw" not in cmh_cols:
            cur.execute("ALTER TABLE campaign_metrics_history ADD COLUMN ocr_raw TEXT")
        # 迁移：历史快照表同口径改名，旧百分数值同样清空
        if "deep_interaction_rate" in cmh_cols or "like_rate" in cmh_cols:
            if "deep_interaction_rate" in cmh_cols:
                cur.execute("ALTER TABLE campaign_metrics_history RENAME COLUMN deep_interaction_rate TO comment_count")
            if "like_rate" in cmh_cols:
                cur.execute("ALTER TABLE campaign_metrics_history RENAME COLUMN like_rate TO like_count")
            cur.execute("UPDATE campaign_metrics_history SET comment_count=NULL, like_count=NULL")
        # 迁移：评分标准里的指标键名跟着改名（deep_interaction→comment、like→heart）。
        # 只改键名、档位数值原样保留 —— 用户可能已经按「个数」把阈值调好了
        # （比如评论 ≥30 得 10 分），不能因为改名就把人家调好的区间丢掉。
        _cfg_row = cur.execute(
            "SELECT value FROM settings WHERE key='campaign_score_config'"
        ).fetchone()
        if _cfg_row and _cfg_row["value"]:
            try:
                _cfg = json.loads(_cfg_row["value"])
            except ValueError:
                # 只吞「内容不是合法 JSON」；其它异常（如写错变量名）必须炸出来，
                # 否则迁移被静默跳过，用户的档位会悄悄回落到默认值。
                _cfg = None
            if isinstance(_cfg, dict) and ("deep_interaction" in _cfg or "like" in _cfg):
                if "deep_interaction" in _cfg and "comment" not in _cfg:
                    _cfg["comment"] = _cfg["deep_interaction"]
                _cfg.pop("deep_interaction", None)
                if "like" in _cfg and "heart" not in _cfg:
                    _cfg["heart"] = _cfg["like"]
                _cfg.pop("like", None)
                cur.execute(
                    "UPDATE settings SET value=? WHERE key='campaign_score_config'",
                    (json.dumps(_cfg, ensure_ascii=False),),
                )
        # ── 双百战役：打分明细 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_scores (
                work_id INTEGER PRIMARY KEY,
                ai_content_score REAL,              -- 情理色诚 0-40
                ai_dims TEXT,                       -- 四维分 JSON
                ai_reason TEXT,                     -- AI 打分理由
                retention_score REAL,               -- 留存 0-30
                retention_detail TEXT,              -- JSON
                interaction_score REAL,             -- 互动 0-30
                interaction_detail TEXT,            -- JSON
                total_score REAL,                   -- 总分 0-100
                grade TEXT,                         -- S/A/B/待优化
                base_merit INTEGER,                 -- 基础战功
                extra_merit TEXT,                   -- 附加战功明细 JSON
                total_merit INTEGER,                -- 总战功（上限15）
                manual_total_score REAL,            -- 人工改分后的总分（null=未改）
                manual_review_by INTEGER,
                manual_review_at TEXT,
                manual_merit INTEGER NOT NULL DEFAULT 0,  -- 人工审核追加的战功
                manual_merit_reason TEXT,
                manual_merit_by INTEGER,
                manual_merit_at TEXT,
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE,
                FOREIGN KEY (manual_review_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        # 迁移：评分表补齐人工字段（人工改分 / 人工追加战功）
        sc_cols = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_scores)").fetchall()]
        for _col, _ddl in (
            ("manual_total_score", "ALTER TABLE campaign_scores ADD COLUMN manual_total_score REAL"),
            ("manual_review_by", "ALTER TABLE campaign_scores ADD COLUMN manual_review_by INTEGER"),
            ("manual_review_at", "ALTER TABLE campaign_scores ADD COLUMN manual_review_at TEXT"),
            ("manual_merit", "ALTER TABLE campaign_scores ADD COLUMN manual_merit INTEGER NOT NULL DEFAULT 0"),
            ("manual_merit_reason", "ALTER TABLE campaign_scores ADD COLUMN manual_merit_reason TEXT"),
            ("manual_merit_by", "ALTER TABLE campaign_scores ADD COLUMN manual_merit_by INTEGER"),
            ("manual_merit_at", "ALTER TABLE campaign_scores ADD COLUMN manual_merit_at TEXT"),
        ):
            if _col not in sc_cols:
                cur.execute(_ddl)
        # ── 双百战役：凭证库 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                type TEXT NOT NULL,                 -- screenshot/business
                file TEXT,
                note TEXT,
                seq INTEGER,                        -- 属于第几次 T+7 回传（1 起）
                review_status TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected 人工审核
                review_note TEXT,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
        )
        # 迁移：凭证库加上「人工审核」字段（老数据一律视为待审核）
        cred_cols2 = [r["name"] for r in cur.execute("PRAGMA table_info(campaign_credentials)").fetchall()]
        for _col, _ddl in (
            ("review_status", "ALTER TABLE campaign_credentials ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"),
            ("review_note", "ALTER TABLE campaign_credentials ADD COLUMN review_note TEXT"),
            ("reviewed_by", "ALTER TABLE campaign_credentials ADD COLUMN reviewed_by INTEGER"),
            ("reviewed_at", "ALTER TABLE campaign_credentials ADD COLUMN reviewed_at TEXT"),
        ):
            if _col not in cred_cols2:
                cur.execute(_ddl)
        cur.execute(
            "UPDATE campaign_credentials SET review_status='pending' "
            "WHERE review_status IS NULL OR TRIM(review_status)=''"
        )
        # ── 双百战役：传播记录库 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_propagation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                account TEXT,                       -- 转发账号
                url TEXT,
                type TEXT,                          -- 转发类型
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
        )
        # ── 双百战役：后台截图 AI 识别结果缓存 ──
        # 按「图片内容哈希」缓存：同一张截图只真正调一次模型，
        # 用户重复点识别、关掉表单重开再点，都直接命中缓存、不再消耗 AI 调用。
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_ocr_cache (
                img_hash TEXT PRIMARY KEY,          -- 截图内容的 md5（多张图按顺序拼接后取）
                metrics TEXT NOT NULL,              -- 识别结果 JSON
                model TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        # ── 双百战役：反作弊抽检记录 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                action TEXT NOT NULL,               -- sample/clear/disqualify
                result TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
        )
        # ── 双百战役：赛道标签（可维护）──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        cur.execute("SELECT COUNT(*) AS c FROM campaign_tracks")
        if cur.fetchone()["c"] == 0:
            for _t in ("益圆/传应品宣", "转假宣传", "足球小将", "门锁场景"):
                cur.execute("INSERT INTO campaign_tracks (name) VALUES (?)", (_t,))

        # ── 一次性回填：多次回传功能上线前，每个作品只回传过一次。
        # 把现有 campaign_metrics / campaign_scores 补成 seq=1 的历史快照，
        # 否则后台的「历次回传」对老作品会是空白。NOT EXISTS 保证幂等。
        cur.execute(
            """
            INSERT INTO campaign_metrics_history
              (work_id, seq, plays, retention_3s, completion_rate, comment_count,
               like_count, share_count, screenshot, business_proof,
               ai_content_score, retention_score, interaction_score, total_score, grade,
               total_merit, qualified, submitter, submitted_at)
            SELECT m.work_id, 1, m.plays, m.retention_3s, m.completion_rate,
                   m.comment_count, m.like_count, m.share_count,
                   m.screenshot, m.business_proof,
                   s.ai_content_score, s.retention_score, s.interaction_score,
                   s.total_score, s.grade, s.total_merit,
                   COALESCE(w.qualified, 0), w.creator_name, m.submitted_at
              FROM campaign_metrics m
              LEFT JOIN campaign_scores s ON s.work_id = m.work_id
              LEFT JOIN campaign_works w ON w.id = m.work_id
             WHERE NOT EXISTS (
                   SELECT 1 FROM campaign_metrics_history h WHERE h.work_id = m.work_id
             )
            """
        )

        # ── 一次性回填：区域串统一成「大区 / 细分区域」──
        # 历史上有三种写法（西二区 / 云南省、西二区/云南省、西二区云南省），
        # 榜单/驾驶舱按原串分组会出现重复区域，甚至凭空多出一个「西二区云南省」
        # 幻影大区。这里把存量数据一次收敛，写入端由 normalize_region 兜住。
        for _table in ("users", "submissions", "campaign_works"):
            try:
                _rows = cur.execute(
                    f"SELECT DISTINCT region FROM {_table} "
                    f"WHERE region IS NOT NULL AND TRIM(region) != ''").fetchall()
            except sqlite3.OperationalError:
                continue  # 表还没建（全新库的早期阶段）
            for _r in _rows:
                _old = _r["region"]
                _new = normalize_region(_old)
                if _new != _old:
                    cur.execute(f"UPDATE {_table} SET region=? WHERE region=?", (_new, _old))


def get_setting(key: str, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
    return row["value"] if row else default


def log_usage(action: str, display_name: str = "", region: str = "",
              ip: str = "", submission_id=None, detail: str = "") -> None:
    """写一条使用日志。

    账号新增/删除、提交删除这类管理动作过去完全没有留痕，事后问「这个使用人
    哪来的」「记录被谁删了」都查不到，所以统一从这里落一条日志。
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                "INSERT INTO usage_log (action, display_name, region, ip, submission_id, detail) "
                "VALUES (?,?,?,?,?,?)",
                (action, display_name, region, ip, submission_id, detail),
            )
    except Exception as e:
        print(f"[usage_log] 写日志失败 action={action}: {e}", flush=True)


def set_setting(key: str, value: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def get_tracks() -> list:
    """返回全部赛道（id + name）。"""
    with db_cursor() as cur:
        cur.execute("SELECT id, name FROM campaign_tracks ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def add_track(name: str) -> int:
    with db_cursor() as cur:
        cur.execute("INSERT INTO campaign_tracks (name) VALUES (?)", (name,))
        return cur.lastrowid


def update_track(track_id: int, name: str) -> None:
    with db_cursor() as cur:
        cur.execute("UPDATE campaign_tracks SET name=? WHERE id=?", (name, track_id))


def delete_track(track_id: int) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM campaign_tracks WHERE id=?", (track_id,))
