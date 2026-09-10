"""SQLite 数据库连接与表结构初始化。"""
import sqlite3
import os
from contextlib import contextmanager

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
    conn = get_connection()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
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
                deep_interaction_rate REAL,         -- 深度互动率 %
                like_rate REAL,                     -- 点赞率 %
                screenshot TEXT,                    -- 后台截图 JSON
                business_proof TEXT,                -- 业务凭证 JSON
                submitted_at TEXT,
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
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
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE,
                FOREIGN KEY (manual_review_by) REFERENCES users(id) ON DELETE SET NULL
            )
            """
        )
        # ── 双百战役：凭证库 ──
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS campaign_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                work_id INTEGER NOT NULL,
                type TEXT NOT NULL,                 -- screenshot/business
                file TEXT,
                note TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (work_id) REFERENCES campaign_works(id) ON DELETE CASCADE
            )
            """
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


def get_setting(key: str, default=None):
    with db_cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
    return row["value"] if row else default


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
