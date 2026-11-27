import asyncio
import base64
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import closing, suppress
from datetime import date, datetime, timezone

import anthropic
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("medlab")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
DB = "medlab.db"
DAILY_LIMIT = 25
MB = 1024 * 1024
router = Router()
ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY, timeout=120)

# (green_lo, green_hi, yellow_lo, yellow_hi): outside yellow range = red
_W = (4.0, 9.0, 3.0, 11.0)
_P = (180, 320, 150, 400)
NORMS = {
    "HGB": ("г/л", "Гемоглобин", "Гемоглобин", {"m": (130, 160, 120, 170), "f": (120, 140, 110, 150)}),
    "RBC": ("×10¹²/л", "Эритроциттер", "Эритроциты", {"m": (4.3, 5.7, 4.0, 6.1), "f": (3.8, 5.1, 3.5, 5.5)}),
    "WBC": ("×10⁹/л", "Лейкоциттер", "Лейкоциты", {"m": _W, "f": _W}),
    "PLT": ("×10⁹/л", "Тромбоциттер", "Тромбоциты", {"m": _P, "f": _P}),
    "ESR": ("мм/ч", "ЭТЖ", "СОЭ", {"m": (0, 10, 0, 20), "f": (0, 15, 0, 25)}),
    "GLU": ("ммоль/л", "Глюкоза", "Глюкоза", {"m": (3.9, 5.5, 3.3, 6.95), "f": (3.9, 5.5, 3.3, 6.95)}),
    "ALT": ("Ед/л", "АЛТ", "АЛТ", {"m": (0, 41, 0, 80), "f": (0, 33, 0, 66)}),
    "AST": ("Ед/л", "АСТ", "АСТ", {"m": (0, 40, 0, 80), "f": (0, 32, 0, 64)}),
    "FERRITIN": ("мкг/л", "Ферритин", "Ферритин", {"m": (30, 300, 15, 400), "f": (30, 150, 15, 200)}),
    "VITD": ("нг/мл", "D дәрумені", "Витамин D", {"m": (30, 100, 20, 100), "f": (30, 100, 20, 100)}),
}
CODES = list(NORMS)

KNOW = (
    "Врачи: HGB/RBC — гематолог/терапевт (женщинам при обильных менструациях — гинеколог); "
    "WBC — терапевт (стойко — гематолог, аллергия — аллерголог-иммунолог); PLT — гематолог (кровотечения/синяки — срочно); "
    "ESR — терапевт/ревматолог/гематолог; GLU — эндокринолог/терапевт; ALT/AST — гастроэнтеролог/гепатолог (AST + боль в груди — кардиолог); "
    "FERRITIN — гематолог/терапевт (женщинам — гинеколог; высокий — гастроэнтеролог/гепатолог); VITD — эндокринолог/терапевт.\n"
    "Связи: 1) низкие HGB+FERRITIN — железодефицит (частые причины у студентов: питание, вегетарианство, у женщин — обильные менструации; железо назначает врач); "
    "2) высокие ALT+AST — нагрузка/повреждение печени (алкоголь, лекарства/БАД, гепатиты, стеатоз; желтуха/тёмная моча/сильная боль — срочно); "
    "3) высокие WBC+ESR — воспаление (при высокой температуре не откладывать визит).\n"
    "Суть: HGB — перенос O2; RBC — эритроциты; WBC — иммунитет; PLT — свёртывание; ESR — маркер воспаления; GLU — энергия; "
    "FERRITIN — запасы железа (растёт при воспалении); VITD — кости/иммунитет/мышцы (дозу подбирает врач); ALT — печень; AST — печень/сердце/мышцы. "
    "Референсы лабораторий могут отличаться. Опасные симптомы (обморок, сильная боль, одышка, кровотечение, желтуха, высокая температура) — 103/112.\n"
    "Источники: КП МЗ РК «Железодефицитная анемия» и «Сахарный диабет 2 типа»; WHO 2024 (haemoglobin); WHO 2020 (ferritin); "
    "Endocrine Society 2011 (vit D); WHO/IDF 2006 (glucose); ACG 2017 (ALT/AST); NIH MedlinePlus; Tietz / Henry's Clinical Diagnosis."
)
SRC_FALLBACK = "WHO 2024 (haemoglobin); WHO 2020 (ferritin); Endocrine Society 2011 (vitamin D); WHO/IDF 2006 (glucose); ACG 2017 (ALT/AST); NIH MedlinePlus: https://medlineplus.gov/"

KK_STRICT = (
    "БАРЛЫҚ МӘТІНДІК ӨРІСТЕРДІ ТЕК ҚАЗАҚ ТІЛІНДЕ ЖАЗ. "
    "Орыс тілін мүлде қолданба (тек талдау кодтары мен өлшем бірліктерінен басқа)."
)

EMERG_RE = re.compile(
    r"созна\w*\s+(потер|теря)|(потер|теря)\w*\s+созна|обморок|(остр|сильн)\w*\s+боль\s+в\s+груди|"
    r"боль\s+в\s+груди.{0,30}(остр|сильн)|(сильн|обильн)\w*\s+кровотеч|кровотечени\w*.{0,20}(не\s+останавл|сильн)|"
    r"не\s+могу\s+дышать|есінен\s*тан|кеуде\w*\s+қатты\s+ауыр|қатты\s+ауыр\w*\s+кеуде|қатты\s+қан\s+кет"
)
LANGQ_RE = re.compile(
    r"(смен|поменя|измен|переключ)\w*\s+(\w+\s+)?язык|язык\w*\s+(смен|поменя|измен|переключ)|(как|где).{0,25}язык|"
    r"тіл\w*\s+(өзгерт|ауыстыр)|тілді\s+(қалай|өзгерт|ауыстыр)|қалай.{0,25}тіл|change\s+language"
)
EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
IMG_MIME = set(EXT_MIME.values())


def L(lang, kk, ru):
    return kk if lang == "kk" else ru if lang == "ru" else f"{kk}\n\n{ru}"


# ---------- DB ----------
def q(sql, args=()):
    try:
        with closing(sqlite3.connect(DB, timeout=30.0)) as c:
            c.row_factory = sqlite3.Row
            rows = c.execute(sql, args).fetchall()
            c.commit()
            return rows
    except sqlite3.Error:
        log.exception("SQLite error on query: %s", sql)
        raise


def init_db():
    q("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, lang TEXT, gender TEXT, age INTEGER,
         offtopic_count INTEGER DEFAULT 0, daily_count INTEGER DEFAULT 0, last_date TEXT)""")
    q("""CREATE TABLE IF NOT EXISTS analyses(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
         created_at TEXT, values_json TEXT)""")
    q("CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, vote TEXT)")


def get_user(uid):
    r = q("SELECT * FROM users WHERE user_id=?", (uid,))
    return dict(r[0]) if r else None


def ensure_user(uid):
    q("INSERT OR IGNORE INTO users(user_id, offtopic_count, daily_count, last_date) VALUES(?,0,0,'')", (uid,))


def ready(u):
    return bool(u and u["lang"] and u["gender"] and u["age"])


def has_quota(uid):
    u = get_user(uid)
    today = date.today().isoformat()
    n = u["daily_count"] if u and u["last_date"] == today else 0
    return n < DAILY_LIMIT


def add_quota(uid):
    u = get_user(uid)
    today = date.today().isoformat()
    n = u["daily_count"] if u and u["last_date"] == today else 0
    q("UPDATE users SET daily_count=?, last_date=? WHERE user_id=?", (n + 1, today, uid))


def last_analysis(uid):
    r = q("SELECT * FROM analyses WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,))
    return dict(r[0]) if r else None


def load_vals(row):
    try:
        return json.loads(row["values_json"]) if row else {}
    except Exception:
        return {}


# ---------- Norms / traffic light ----------
def fmt(v):
    return f"{v:g}"


def status(code, g, v):
    glo, ghi, ylo, yhi = NORMS[code][3][g]
    if glo <= v <= ghi:
        return "🟢"
    return "🟡" if ylo <= v <= yhi else "🔴"


def rng(code, g):
    glo, ghi = NORMS[code][3][g][:2]
    return f"≤ {fmt(ghi)}" if glo == 0 else f"{fmt(glo)}–{fmt(ghi)}"


def unit_of(code, lang):
    return "мм/сағ" if code == "ESR" and lang == "kk" else NORMS[code][0]


def norms_text(g):
    out = []
    for c, (unit, _, _, n) in NORMS.items():
        glo, ghi, ylo, yhi = n[g]
        yellow = []
        if ylo < glo:
            yellow.append(f"{fmt(ylo)}–{fmt(glo)}")
        if ghi < yhi:
            yellow.append(f"{fmt(ghi)}–{fmt(yhi)}")
        y_str = "/".join(yellow) if yellow else "нет"
        out.append(f"{c}({unit}):🟢{rng(c, g)},🟡{y_str},🔴вне {fmt(ylo)}–{fmt(yhi)}")
    return "; ".join(out)


def status_text(vals, g):
    return "; ".join(f"{c}={fmt(v)} {NORMS[c][0]} {status(c, g, v)} (норма {rng(c, g)})" for c, v in vals.items()) or "нет"


def clean_values(raw):
    out = {}
    if not isinstance(raw, dict):
        return out
    for c in CODES:
        v = raw.get(c)
        if isinstance(v, str):
            m = re.search(r"-?\d+(?:[.,]\d+)?", v)
            v = float(m.group().replace(",", ".")) if m else None
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            out[c] = float(v)
    return out


def clean_extras(raw):
    out = []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, dict) and e.get("name"):
                out.append((str(e["name"]).strip(), str(e.get("value", "")).strip()))
    return out[:30]


def txt(x):
    if isinstance(x, list):
        x = "; ".join(str(i) for i in x)
    return str(x or "").strip()


def build_report(u, data, vals, prev_vals, extras):
    lang, g = u["lang"], u["gender"]
    parts = ["🔬 MedLab AI"]
    if vals:
        line = " | ".join(f"{status(c, g, vals[c])} {c} {fmt(vals