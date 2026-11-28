import json
import logging
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

log = logging.getLogger("medlab.db")
DB_PATH = "medlab.db"


def q(sql, args=()):
    try:
        with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows
    except sqlite3.Error:
        log.exception("SQLite error on query: %s", sql)
        raise


def _has_column(table, col):
    cols = q(f"PRAGMA table_info({table})")
    return any(r["name"] == col for r in cols)


def init_db():
    # Включаем WAL для безопасной конкурентной работы при фоновых задачах на следующих этапах
    with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as c:
        c.execute("PRAGMA journal_mode=WAL;")
        c.commit()

    # 1. Пользователи (добавляем pending_fb_id для приёма текстового комментария после 👎)
    q("""CREATE TABLE IF NOT EXISTS users(
         user_id INTEGER PRIMARY KEY,
         lang TEXT,
         gender TEXT,
         age INTEGER,
         offtopic_count INTEGER DEFAULT 0,
         daily_count INTEGER DEFAULT 0,
         last_date TEXT,
         pending_fb_id INTEGER DEFAULT NULL
    )""")
    if not _has_column("users", "pending_fb_id"):
        q("ALTER TABLE users ADD COLUMN pending_fb_id INTEGER DEFAULT NULL")

    # 2. Анализы (закладываем source_type и raw_summary для будущей единой ленты /history)
    q("""CREATE TABLE IF NOT EXISTS analyses(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER,
         created_at TEXT,
         values_json TEXT,
         source_type TEXT DEFAULT 'lab',
         raw_summary TEXT DEFAULT ''
    )""")
    if not _has_column("analyses", "source_type"):
        q("ALTER TABLE analyses ADD COLUMN source_type TEXT DEFAULT 'lab'")
    if not _has_column("analyses", "raw_summary"):
        q("ALTER TABLE analyses ADD COLUMN raw_summary TEXT DEFAULT ''")

    # 3. Обратная связь (оценка + опциональный текстовый комментарий + привязка к разбору)
    q("""CREATE TABLE IF NOT EXISTS feedback(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER,
         vote TEXT,
         comment TEXT DEFAULT '',
         analysis_id INTEGER DEFAULT NULL,
         created_at TEXT DEFAULT ''
    )""")
    if not _has_column("feedback", "comment"):
        q("ALTER TABLE feedback ADD COLUMN comment TEXT DEFAULT ''")
    if not _has_column("feedback", "analysis_id"):
        q("ALTER TABLE feedback ADD COLUMN analysis_id INTEGER DEFAULT NULL")
    if not _has_column("feedback", "created_at"):
        q("ALTER TABLE feedback ADD COLUMN created_at TEXT DEFAULT ''")

    # 4. Заготовка под Этап 2: Дневник симптомов (/symptoms)
    q("""CREATE TABLE IF NOT EXISTS symptoms(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER,
         created_at TEXT,
         raw_text TEXT,
         symptom_name TEXT,
         duration TEXT,
         severity TEXT,
         ai_note TEXT
    )""")
    q("CREATE INDEX IF NOT EXISTS idx_symptoms_user ON symptoms(user_id, created_at)")

    # 5. Заготовка под Этап 2: Напоминания о пересдаче анализа
    q("""CREATE TABLE IF NOT EXISTS reminders(
         id INTEGER PRIMARY KEY AUTOINCREMENT,
         user_id INTEGER,
         created_at TEXT,
         remind_at TEXT,
         markers_text TEXT,
         weeks INTEGER,
         sent INTEGER DEFAULT 0
    )""")
    q("CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(sent, remind_at)")
    q("CREATE INDEX IF NOT EXISTS idx_analyses_user ON analyses(user_id, created_at)")


def get_user(uid):
    r = q("SELECT * FROM users WHERE user_id=?", (uid,))
    return dict(r[0]) if r else None


def ensure_user(uid):
    q("INSERT OR IGNORE INTO users(user_id, offtopic_count, daily_count, last_date) VALUES(?,0,0,'')", (uid,))


def ready(u):
    return bool(u and u["lang"] and u["gender"] and u["age"])


def has_quota(uid, daily_limit):
    u = get_user(uid)
    today = date.today().isoformat()
    n = u["daily_count"] if u and u["last_date"] == today else 0
    return n < daily_limit


def add_quota(uid):
    u = get_user(uid)
    today = date.today().isoformat()
    n = u["daily_count"] if u and u["last_date"] == today else 0
    q("UPDATE users SET daily_count=?, last_date=? WHERE user_id=?", (n + 1, today, uid))


def last_analysis(uid):
    r = q("SELECT * FROM analyses WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
    return dict(r[0]) if r else None


def save_analysis(uid, vals, source_type="lab", raw_summary=""):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as c:
        cur = c.execute(
            "INSERT INTO analyses(user_id, created_at, values_json, source_type, raw_summary) VALUES(?,?,?,?,?)",
            (uid, now, json.dumps(vals, ensure_ascii=False), source_type, raw_summary[:1000]),
        )
        c.commit()
        return cur.lastrowid


def load_vals(row):
    try:
        return json.loads(row["values_json"]) if row else {}
    except Exception:
        return {}


def save_feedback(uid, vote, analysis_id=None):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(sqlite3.connect(DB_PATH, timeout=30.0)) as c:
        cur = c.execute(
            "INSERT INTO feedback(user_id, vote, comment, analysis_id, created_at) VALUES(?,?,?,?,?)",
            (uid, vote, "", analysis_id, now),
        )
        c.commit()
        return cur.lastrowid


def set_pending_feedback(uid, fb_id):
    q("UPDATE users SET pending_fb_id=? WHERE user_id=?", (fb_id, uid))


def save_feedback_comment(uid, fb_id, comment_text):
    q("UPDATE feedback SET comment=? WHERE id=? AND user_id=?", (comment_text[:1000], fb_id, uid))
    q("UPDATE users SET pending_fb_id=NULL WHERE user_id=?", (uid,))


def delete_user_data(uid):
    for table in ("users", "analyses", "feedback", "symptoms", "reminders"):
        q(f"DELETE FROM {table} WHERE user_id=?", (uid,))


def get_quality_stats():
    users_total = q("SELECT COUNT(*) c FROM users")[0]["c"]
    users_ready = q("SELECT COUNT(*) c FROM users WHERE age IS NOT NULL")[0]["c"]
    analyses_total = q("SELECT COUNT(*) c FROM analyses")[0]["c"]
    blocked_total = q("SELECT COUNT(*) c FROM users WHERE offtopic_count>=3")[0]["c"]

    fb_up = q("SELECT COUNT(*) c FROM feedback WHERE vote='up'")[0]["c"]
    fb_tot = q("SELECT COUNT(*) c FROM feedback")[0]["c"]
    like_pct = f"{round(fb_up * 100 / fb_tot)}%" if fb_tot else "—"

    users_with_an = q("SELECT COUNT(DISTINCT user_id) c FROM analyses")[0]["c"]
    repeat_users = q("SELECT COUNT(*) c FROM (SELECT user_id FROM analyses GROUP BY user_id HAVING COUNT(*) >= 2)")[0]["c"]
    repeat_pct = f"{round(repeat_users * 100 / users_with_an)}%" if users_with_an else "—"
    avg_per_user = f"{(analyses_total / users_with_an):.1f}" if users_with_an else "0"

    return {
        "users_total": users_total,
        "users_ready": users_ready,
        "analyses_total": analyses_total,
        "blocked_total": blocked_total,
        "fb_up": fb_up,
        "fb_tot": fb_tot,
        "like_pct": like_pct,
        "repeat_pct": repeat_pct,
        "avg_per_user": avg_per_user,
    }