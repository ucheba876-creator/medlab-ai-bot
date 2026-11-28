# -*- coding: utf-8 -*-
"""
Telegram-бот «Светофор анализов крови» (aiogram 3.x + Claude Haiku Vision).

Переменные окружения:
    TELEGRAM_TOKEN     - токен Telegram-бота
    ANTHROPIC_API_KEY  - ключ Anthropic API
    PORT               - порт HTTP-сервера для Render (по умолчанию 10000)
"""

import asyncio
import base64
import html
import logging
import os
import re
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from anthropic import AsyncAnthropic

# ---------------------------------------------------------------------------
# Настройки (ID модели легко поменять здесь)
# ---------------------------------------------------------------------------
MODEL_NAME = "claude-haiku-5-5"

MAX_HISTORY = 10          # сколько последних сообщений помнит бот
MAX_TOKENS = 4000         # запас по токенам (казахский токенизируется «тяжелее»)
TG_LIMIT = 3500           # лимит «сырого» текста одного сообщения до экранирования
NORMS_FILE = Path(__file__).resolve().parent / "norms.txt"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bloodbot")

# ---------------------------------------------------------------------------
# Хранилища в памяти
# ---------------------------------------------------------------------------
histories: dict[int, list[dict]] = {}   # user_id -> [{"role":..., "content": str}, ...]
profiles: dict[int, dict] = {}          # user_id -> {"lang": "kk|ru", "sex": "m|f", "age": int}
locks: dict[int, asyncio.Lock] = {}     # защита от параллельных запросов одного пользователя

NORMS_TEXT = ""  # заполняется при старте


def get_lock(uid: int) -> asyncio.Lock:
    if uid not in locks:
        locks[uid] = asyncio.Lock()
    return locks[uid]


def trim_history(uid: int) -> None:
    """Оставляем последние MAX_HISTORY сообщений; история должна начинаться с user."""
    h = histories.get(uid, [])
    h = h[-MAX_HISTORY:]
    while h and h[0]["role"] != "user":
        h.pop(0)
    histories[uid] = h


# ---------------------------------------------------------------------------
# Тексты интерфейса (обычный текст, без HTML)
# ---------------------------------------------------------------------------
TEXTS = {
    "ru": {
        "ask_sex": "Отлично! Укажите, пожалуйста, ваш пол:",
        "sex_m": "Мужской",
        "sex_f": "Женский",
        "ask_age": "Спасибо! Теперь напишите ваш возраст (от 18 до 25 лет).",
        "bad_age": "Бот рассчитан на возраст 18–25 лет. Напишите возраст числом в этом диапазоне.",
        "pick_sex": "Пожалуйста, выберите пол кнопками выше.",
        "ask_analysis": (
            "Принято! Теперь отправьте показатели анализа крови текстом "
            "ИЛИ фотографией/скриншотом бланка."
        ),
        "wait": "⏳ Анализирую, это займёт несколько секунд...",
        "error": "⚠️ Произошла ошибка при обработке. Попробуйте ещё раз чуть позже.",
        "photo_error": "⚠️ Не удалось скачать фото. Попробуйте отправить его ещё раз.",
        "need_start": "Нажмите /start, чтобы начать заново.",
        "cheat_btn": "🩺 Шпаргалка врачу",
        "after": (
            "Хотите подготовиться к визиту к врачу? Нажмите кнопку ниже.\n\n"
            "Также вы можете отправить повторный анализ в этом диалоге — "
            "я сравню динамику «Было / Стало»."
        ),
        "no_analysis": "Сначала отправьте анализ.",
        "cheat_request": (
            "Сформируй «Шпаргалку врачу» по результатам анализа выше. "
            "Компактная сводка ТОЛЬКО по отклонениям (🟡 и 🔴) с цифрами и единицами; "
            "укажи, к какому врачу идти; дай 4–5 вопросов для приёма. "
            "Не ставь диагнозов и не назначай лечение. Уложись в 2000 символов."
        ),
        "photo_default": "Вот фото бланка анализа крови. Распознай показатели и разбери их.",
        "photo_note": "[Пользователь отправил фото бланка анализа]",
        "disclaimer": "ℹ️ Ответ носит информационный характер, не является диагнозом и не заменяет консультацию врача.",
    },
    "kk": {
        "ask_sex": "Тамаша! Жынысыңызды көрсетіңіз:",
        "sex_m": "Ер",
        "sex_f": "Әйел",
        "ask_age": "Рақмет! Енді жасыңызды жазыңыз (18-ден 25-ке дейін).",
        "bad_age": "Бот 18–25 жас аралығына арналған. Жасыңызды осы аралықтағы сан ретінде жазыңыз.",
        "pick_sex": "Жоғарыдағы батырмалар арқылы жынысыңызды таңдаңыз.",
        "ask_analysis": (
            "Қабылданды! Енді қан анализінің көрсеткіштерін мәтінмен НЕМЕСЕ "
            "бланктің фотосын/скриншотын жіберіңіз."
        ),
        "wait": "⏳ Талдап жатырмын, бірнеше секунд күтіңіз...",
        "error": "⚠️ Өңдеу кезінде қате шықты. Сәл кейінірек қайталап көріңіз.",
        "photo_error": "⚠️ Фотоны жүктеу мүмкін болмады. Қайта жіберіп көріңіз.",
        "need_start": "Қайта бастау үшін /start басыңыз.",
        "cheat_btn": "🩺 Дәрігерге арналған шпаргалка",
        "after": (
            "Дәрігерге барар алдында дайындалғыңыз келе ме? Төмендегі батырманы басыңыз.\n\n"
            "Сондай-ақ осы диалогта қайта анализ жібере аласыз — "
            "мен «Болды / Қазір» динамикасын салыстырамын."
        ),
        "no_analysis": "Алдымен анализді жіберіңіз.",
        "cheat_request": (
            "Жоғарыдағы анализ нәтижесі бойынша «Дәрігерге арналған шпаргалка» жаса. "
            "Тек ауытқулар (🟡 және 🔴) бойынша шағын жиынтық, сандары мен өлшем бірліктерімен; "
            "қай дәрігерге бару керегін көрсет; қабылдауға 4–5 сұрақ бер. "
            "Диагноз қойма, емдеу тағайындама. 2000 таңбадан аспасын."
        ),
        "photo_default": "Мынау қан анализі бланкісінің фотосы. Көрсеткіштерді танып, талдап бер.",
        "photo_note": "[Пайдаланушы анализ бланкісінің фотосын жіберді]",
        "disclaimer": "ℹ️ Бұл жауап ақпараттық сипатта, диагноз емес және дәрігер кеңесін алмастырмайды.",
    },
}

GREETING = (
    "👋 Сәлем! / Привет!\n\n"
    "Я помогу разобраться в показателях анализа крови простым языком "
    "(информационный разбор, не диагноз).\n"
    "Мен қан анализінің көрсеткіштерін қарапайым тілмен түсіндіріп беремін "
    "(ақпараттық талдау, диагноз емес).\n\n"
    "Выберите язык / Тілді таңдаңыз:"
)

# ---------------------------------------------------------------------------
# Языковые спецификации для системного промпта
# ---------------------------------------------------------------------------
LANG_SPEC = {
    "ru": {
        "lang_name": "РУССКОМ",
        "sex": {"m": "мужской", "f": "женский"},
        "h_traffic": "🚦 Светофор",
        "h_decode": "📖 Расшифровка",
        "h_links": "🔗 Связки показателей",
        "h_doctor": "👨‍⚕️ К какому врачу обратиться",
        "h_sources": "📚 Источники",
        "h_compare": "📈 Было / Стало",
        "h_cheat": "🩺 Шпаргалка врачу",
        "h_questions": "Вопросы врачу",
        "st_green": "🟢 Норма",
        "st_yellow": "🟡 Пограничное",
        "st_red": "🔴 Требует внимания",
        "norm_word": "норма",
        "improved": "улучшилось",
        "worse": "ухудшилось",
        "same": "без изменений",
        "example": "• <b>Гемоглобин (HGB)</b>: 105 г/л (норма 120–140) — 🟡 Пограничное",
        "language_rule": (
            "Весь ответ — ТОЛЬКО на русском языке: заголовки блоков, названия показателей, статусы, "
            "объяснения, названия врачей, дисклеймеры, вопросы врачу. НИ ОДНОГО казахского слова. "
            "Если бланк или справочник на другом языке — переводи названия и смысл на русский. "
            "Если пользователь пишет на другом языке — всё равно отвечай по-русски."
        ),
    },
    "kk": {
        "lang_name": "КАЗАХСКОМ",
        "sex": {"m": "ер", "f": "әйел"},
        "h_traffic": "🚦 Бағдаршам",
        "h_decode": "📖 Түсіндірме",
        "h_links": "🔗 Көрсеткіштердің өзара байланысы",
        "h_doctor": "👨‍⚕️ Қай дәрігерге қаралу керек",
        "h_sources": "📚 Дереккөздер",
        "h_compare": "📈 Болды / Қазір",
        "h_cheat": "🩺 Дәрігерге арналған шпаргалка",
        "h_questions": "Дәрігерге сұрақтар",
        "st_green": "🟢 Қалыпты",
        "st_yellow": "🟡 Шекаралық",
        "st_red": "🔴 Назар аудару қажет",
        "norm_word": "қалыпты мәні",
        "improved": "жақсарды",
        "worse": "нашарлады",
        "same": "өзгермеді",
        "example": "• <b>Гемоглобин (HGB)</b>: 105 г/л (қалыпты мәні 120–140) — 🟡 Шекаралық",
        "language_rule": (
            "Весь ответ — ТОЛЬКО на казахском языке, включая: заголовки блоков, названия показателей "
            "с бланка (ПЕРЕВОДИ их на казахский: Гемоглобин, Эритроциттер, Лейкоциттер, Тромбоциттер, "
            "ЭТЖ (вместо СОЭ), Глюкоза, Холестерин, Ферритин, Темір, Билирубин и т. д.), статусы светофора, "
            "слова «норма», «было», «стало», «улучшилось», «ухудшилось», названия врачей (терапевт, "
            "гематолог, эндокринолог, гастроэнтеролог — в казахском написании), дисклеймеры, вопросы врачу. "
            "ЗАПРЕЩЕНО использовать русские слова, даже в скобках и даже одно слово. "
            "Допустимы ТОЛЬКО: цифры, единицы измерения (г/л, ммоль/л, ×10⁹/л и т. п.), международные "
            "латинские аббревиатуры (HGB, RBC, WBC, PLT, ALT, AST) и ссылки (URL). "
            "Справочник norms.txt может быть на русском — переводи его содержание на казахский. "
            "Бланк может быть на русском — названия всё равно переводи на казахский. "
            "Если пользователь пишет по-русски — всё равно отвечай ТОЛЬКО по-казахски."
        ),
    },
}


# ---------------------------------------------------------------------------
# Системный промпт
# ---------------------------------------------------------------------------
def build_system_prompt(profile: dict) -> str:
    sp = LANG_SPEC[profile["lang"]]
    disclaimer = TEXTS[profile["lang"]]["disclaimer"]
    sex_name = sp["sex"][profile["sex"]]

    norms_block = NORMS_TEXT.strip() or (
        "[ФАЙЛ norms.txt ПУСТ ИЛИ НЕ НАЙДЕН. Нормативов нет: прямо сообщи пользователю "
        "(на выбранном языке), что не можешь оценить показатели по «Светофору» без справочника норм, "
        "и не придумывай нормы самостоятельно.]"
    )

    return f"""Ты — информационный помощник, который помогает молодым людям (18–25 лет) понять результаты общего и биохимического анализа крови простым языком.

=== ПРАВИЛО №1: ЯЗЫК (ЖЁСТКИЙ КОНТРОЛЬ) ===
Язык общения выбран пользователем: {sp['lang_name']}.
{sp['language_rule']}
СМЕШИВАТЬ ЯЗЫКИ КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО. Перед отправкой мысленно проверь каждую строку ответа на соответствие языку.
Статусы светофора пиши ТОЛЬКО так: «{sp['st_green']}», «{sp['st_yellow']}», «{sp['st_red']}».

=== ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ ===
Пол — {sex_name}; возраст — {profile['age']} лет. Учитывай это при оценке по нормам.

=== СТРОГИЕ ПРАВИЛА СОДЕРЖАНИЯ ===
1. Ты НЕ ставишь диагнозы и НЕ назначаешь лечение, лекарства, добавки или дозировки. Можно говорить лишь о том, с чем МОГУТ быть связаны отклонения, и всегда рекомендовать обсудить это с врачом.
2. В НАЧАЛЕ и в КОНЦЕ каждого разбора — дисклеймер ровно такой одной строкой: {disclaimer}
3. Оценивай показатели ТОЛЬКО по цифрам из справочника норм ниже (с учётом пола и возраста). Не используй нормы из памяти. Если показателя нет в справочнике — напиши об этом коротко и не присваивай ему цвет.
4. Не выдумывай значения. Если что-то на фото нечитаемо — переспроси, а не угадывай.
5. Не ставь диагнозов, не используй слова «у вас болезнь/заболевание».

=== ФОРМАТ ОТВЕТА: HTML-РАЗМЕТКА TELEGRAM ===
- Разрешён ТОЛЬКО тег <b>...</b>. Никаких других тегов, никакого Markdown (**, __, #, ```, таблицы).
- Символы < > & пиши как обычные символы (бот сам их экранирует), но по возможности заменяй словами.
- <b> используй для: заголовков блоков, названий показателей, ключевых выводов.
- Между блоками — ПУСТАЯ строка. Заголовок блока и его содержимое — на соседних строках без пустой строки.
- Списки — строками, начинающимися с «• ».

=== СТРУКТУРА РАЗБОРА (когда пользователь прислал показатели текстом или фото) ===
Строка 1: дисклеймер.

Блок «<b>{sp['h_traffic']}</b>» — здесь же выводятся распознанные показатели с цифрами. ОДНА строка на показатель, строго в формате:
{sp['example']}
Общий вид: • <b>Название</b>: значение единица ({sp['norm_word']} X–Y) — статус
Статус — один из: {sp['st_green']} / {sp['st_yellow']} / {sp['st_red']}. Показатели выводи в порядке бланка.

Блок «<b>{sp['h_decode']}</b>»:
- Все показатели со статусом {sp['st_green']} объедини в 1–2 общие короткие строки: что они в целом означают и что все они в пределах нормы. Не расписывай их по одному.
- ТОЛЬКО по показателям со статусом {sp['st_yellow']} и {sp['st_red']} дай подробность: <b>название</b> — расшифровка аббревиатуры простым языком и 1–2 предложения о том, что может означать отклонение (без диагноза).
- Если отклонений нет — одна строка об этом.

Блок «<b>{sp['h_links']}</b>» (1–3 коротких строки, только если есть реально значимые сочетания показателей; иначе блок пропусти).

Блок «<b>{sp['h_doctor']}</b>» (1–2 строки): при отклонениях — профильный врач (терапевт, гематолог, эндокринолог, гастроэнтеролог и т. д.); если всё в норме — коротко скажи, что срочных причин нет, а плановое наблюдение у терапевта полезно.

Блок «<b>{sp['h_sources']}</b>» (1–3 ссылки): только ссылки, которые есть в справочнике norms.txt, каждая с новой строки. Не придумывай ссылки; если в справочнике их нет — блок пропусти.

Последняя строка: дисклеймер (тот же).

=== ПОВТОРНЫЙ АНАЛИЗ ===
Если в истории диалога уже есть предыдущий разбор, а пользователь прислал новые показатели: сделай разбор по той же структуре и добавь перед блоком врача компактный блок «<b>{sp['h_compare']}</b>»: по одной строке на показатель в формате
• <b>Название</b>: X → Y единица (+/− Z единица) — {sp['improved']} / {sp['worse']} / {sp['same']}
Сравнивай ТОЛЬКО в единицах измерения. Проценты НЕ считай и не упоминай. «{sp['improved']}» = приблизилось к норме, «{sp['worse']}» = отдалилось от нормы.

=== ШПАРГАЛКА ВРАЧУ (если пользователь просит её) ===
Формат: дисклеймер; заголовок «<b>{sp['h_cheat']}</b>»; строки ТОЛЬКО по отклонениям ({sp['st_yellow']} и {sp['st_red']}) в формате «• <b>Название</b>: значение единица ({sp['norm_word']} X–Y) — статус»; пустая строка; «<b>{sp['h_doctor']}</b>» с названием врача; пустая строка; «<b>{sp['h_questions']}</b>» — 4–5 нумерованных вопросов для приёма; дисклеймер. Если отклонений нет — скажи об этом и дай 4–5 общих вопросов о профилактике. Объём — до 2000 символов.

=== ОБЪЁМ ===
Разбор анализа должен укладываться в 2500–3000 символов (с учётом тегов и эмодзи) — не длиннее. Пиши сжато, без повторов и воды, но ВСЕГДА доводи ответ до заключительного дисклеймера: ответ не должен обрываться. Если показателей очень много — сокращай пояснения к отклонениям, но не пропускай строки светофора.

Если пользователь пишет что-то, не связанное с анализом крови, вежливо верни разговор к теме (на выбранном языке): попроси прислать показатели текстом или фото.

=== СПРАВОЧНИК НОРМ (norms.txt) — ЕДИНСТВЕННЫЙ ИСТОЧНИК НОРМ И ССЫЛОК ===
{norms_block}
=== КОНЕЦ СПРАВОЧНИКА ===

НАПОМИНАНИЕ: отвечай строго на {sp['lang_name']} языке, не смешивай языки, используй только тег <b>."""


# ---------------------------------------------------------------------------
# HTML-утилиты
# ---------------------------------------------------------------------------
_B_TAG = re.compile(r"</?b>", re.IGNORECASE)


def to_telegram_html(text: str) -> str:
    """
    Экранирует <, >, & во всём тексте и оставляет только корректно
    сбалансированные теги <b>...</b>. Остальные теги превращаются в обычный текст.
    """
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)  # страховка от Markdown
    out: list[str] = []
    pos = 0
    open_b = False
    for m in _B_TAG.finditer(text):
        out.append(html.escape(text[pos:m.start()], quote=False))
        if m.group().lower() == "<b>":
            if not open_b:
                out.append("<b>")
                open_b = True
        else:
            if open_b:
                out.append("</b>")
                open_b = False
        pos = m.end()
    out.append(html.escape(text[pos:], quote=False))
    if open_b:
        out.append("</b>")
    return "".join(out)


def strip_tags(text: str) -> str:
    return _B_TAG.sub("", text)


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Делит длинный ответ на части по границам абзацев/строк."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


async def send_html(message: Message, text: str, reply_markup=None) -> None:
    """Отправляет ответ Claude с parse_mode=HTML; при ошибке разметки — обычным текстом."""
    parts = split_text(text)
    for i, part in enumerate(parts):
        markup = reply_markup if (i == len(parts) - 1) else None
        try:
            await message.answer(
                to_telegram_html(part),
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramBadRequest:
            log.warning("HTML не принят Telegram, отправляю без разметки", exc_info=True)
            await message.answer(strip_tags(part), reply_markup=markup)


# ---------------------------------------------------------------------------
# Клиенты и вызов Claude
# ---------------------------------------------------------------------------
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
router_dp = Dispatcher()


async def ask_claude(uid: int, user_content) -> str:
    """
    Отправляет запрос в Claude. user_content — строка или список блоков (текст + изображение).
    История берётся из памяти; новое сообщение добавляется временно (с картинкой),
    а в историю сохраняется только текстовая версия.
    """
    if claude is None:
        raise RuntimeError("ANTHROPIC_API_KEY не задан")

    profile = profiles[uid]
    messages = list(histories.get(uid, [])) + [{"role": "user", "content": user_content}]

    response = await claude.messages.create(
        model=MODEL_NAME,
        max_tokens=MAX_TOKENS,
        system=build_system_prompt(profile),
        messages=messages,
    )
    answer = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    if not answer:
        raise RuntimeError("Пустой ответ от модели")

    # Защита от обрыва: режем по последнему целому абзацу и дописываем дисклеймер
    if response.stop_reason == "max_tokens":
        log.warning("Ответ упёрся в max_tokens, обрезаю по последнему абзацу")
        cut = answer.rfind("\n\n")
        if cut > len(answer) // 2:
            answer = answer[:cut].rstrip()
        answer += "\n\n" + TEXTS[profile["lang"]]["disclaimer"]
    return answer


def save_exchange(uid: int, user_text: str, answer: str) -> None:
    h = histories.setdefault(uid, [])
    h.append({"role": "user", "content": user_text})
    h.append({"role": "assistant", "content": answer})
    trim_history(uid)


def cheat_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=TEXTS[lang]["cheat_btn"], callback_data="cheat")]]
    )


def get_ready_profile(uid: int) -> tuple[dict | None, str]:
    """
    Возвращает (profile, stage). stage: 'start' | 'sex' | 'age' | 'ready'.
    """
    p = profiles.get(uid)
    if not p or "lang" not in p:
        return None, "start"
    if "sex" not in p:
        return p, "sex"
    if "age" not in p:
        return p, "age"
    return p, "ready"


# ---------------------------------------------------------------------------
# Хэндлеры
# ---------------------------------------------------------------------------
@router_dp.message(CommandStart())
async def cmd_start(message: Message):
    uid = message.from_user.id
    histories[uid] = []
    profiles[uid] = {}
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🇰🇿 Қазақша", callback_data="lang:kk"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
        ]]
    )
    await message.answer(GREETING, reply_markup=kb)


@router_dp.callback_query(F.data.startswith("lang:"))
async def cb_lang(call: CallbackQuery):
    uid = call.from_user.id
    lang = call.data.split(":", 1)[1]
    if lang not in TEXTS:
        await call.answer()
        return
    profiles[uid] = {"lang": lang}
    histories[uid] = []
    t = TEXTS[lang]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=t["sex_m"], callback_data="sex:m"),
            InlineKeyboardButton(text=t["sex_f"], callback_data="sex:f"),
        ]]
    )
    await call.message.answer(t["ask_sex"], reply_markup=kb)
    await call.answer()


@router_dp.callback_query(F.data.startswith("sex:"))
async def cb_sex(call: CallbackQuery):
    uid = call.from_user.id
    p = profiles.get(uid)
    if not p or "lang" not in p:
        await call.message.answer(TEXTS["ru"]["need_start"] + " / " + TEXTS["kk"]["need_start"])
        await call.answer()
        return
    p["sex"] = call.data.split(":", 1)[1]
    p.pop("age", None)
    await call.message.answer(TEXTS[p["lang"]]["ask_age"])
    await call.answer()


@router_dp.callback_query(F.data == "cheat")
async def cb_cheat(call: CallbackQuery):
    uid = call.from_user.id
    p, stage = get_ready_profile(uid)
    if stage != "ready":
        await call.message.answer(TEXTS["ru"]["need_start"] + " / " + TEXTS["kk"]["need_start"])
        await call.answer()
        return
    t = TEXTS[p["lang"]]
    if not histories.get(uid):
        await call.answer(t["no_analysis"], show_alert=True)
        return

    await call.answer()
    lock = get_lock(uid)
    if lock.locked():
        return
    async with lock:
        wait_msg = await call.message.answer(t["wait"])
        try:
            await call.bot.send_chat_action(call.message.chat.id, "typing")
            answer = await ask_claude(uid, t["cheat_request"])
            save_exchange(uid, t["cheat_request"], answer)
            await send_html(call.message, answer)
        except Exception:
            log.exception("Ошибка при формировании шпаргалки")
            await call.message.answer(t["error"])
        finally:
            try:
                await wait_msg.delete()
            except Exception:
                pass


@router_dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    uid = message.from_user.id
    p, stage = get_ready_profile(uid)
    if stage == "start":
        await message.answer(TEXTS["ru"]["need_start"] + " / " + TEXTS["kk"]["need_start"])
        return
    t = TEXTS[p["lang"]]
    if stage == "sex":
        await message.answer(t["pick_sex"])
        return
    if stage == "age":
        await message.answer(t["ask_age"])
        return

    lock = get_lock(uid)
    if lock.locked():
        return
    async with lock:
        wait_msg = await message.answer(t["wait"])
        try:
            await bot.send_chat_action(message.chat.id, "typing")

            # Фото максимального качества — последний элемент списка
            photo = message.photo[-1]
            buf = BytesIO()
            try:
                await bot.download(photo, destination=buf)
            except Exception:
                log.exception("Не удалось скачать фото")
                await message.answer(t["photo_error"])
                return
            image_b64 = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

            caption = (message.caption or "").strip()
            prompt_text = caption if caption else t["photo_default"]

            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",  # Telegram отдаёт фото как JPEG
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt_text},
            ]
            answer = await ask_claude(uid, content)

            # В историю сохраняем только текстовую заметку (без тяжёлых base64)
            history_text = t["photo_note"] + (f"\n{caption}" if caption else "")
            save_exchange(uid, history_text, answer)

            await send_html(message, answer)
            await message.answer(t["after"], reply_markup=cheat_keyboard(p["lang"]))
        except Exception:
            log.exception("Ошибка при обработке фото")
            await message.answer(t["error"])
        finally:
            try:
                await wait_msg.delete()
            except Exception:
                pass


@router_dp.message(F.text)
async def on_text(message: Message):
    uid = message.from_user.id
    text = (message.text or "").strip()
    if not text:
        return

    p, stage = get_ready_profile(uid)
    if stage == "start":
        await message.answer(TEXTS["ru"]["need_start"] + " / " + TEXTS["kk"]["need_start"])
        return
    t = TEXTS[p["lang"]]

    if stage == "sex":
        await message.answer(t["pick_sex"])
        return

    if stage == "age":
        m = re.search(r"\d{1,3}", text)
        age = int(m.group()) if m else None
        if age is None or not (18 <= age <= 25):
            await message.answer(t["bad_age"])
            return
        p["age"] = age
        await message.answer(t["ask_analysis"])
        return

    # stage == "ready": пользователь прислал показатели текстом
    lock = get_lock(uid)
    if lock.locked():
        return
    async with lock:
        wait_msg = await message.answer(t["wait"])
        try:
            await message.bot.send_chat_action(message.chat.id, "typing")
            answer = await ask_claude(uid, text)
            save_exchange(uid, text, answer)
            await send_html(message, answer)
            await message.answer(t["after"], reply_markup=cheat_keyboard(p["lang"]))
        except Exception:
            log.exception("Ошибка при обработке текста")
            await message.answer(t["error"])
        finally:
            try:
                await wait_msg.delete()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Фоновый HTTP-сервер для Render (Web Service)
# ---------------------------------------------------------------------------
async def handle_ok(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_route("GET", "/{tail:.*}", handle_ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info("HTTP-сервер запущен на порту %s", PORT)
    return runner


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------
def load_norms() -> str:
    try:
        text = NORMS_FILE.read_text(encoding="utf-8")
        log.info("norms.txt загружен (%d символов)", len(text))
        return text
    except FileNotFoundError:
        log.warning("Файл norms.txt не найден рядом с bot.py: %s", NORMS_FILE)
    except Exception:
        log.exception("Не удалось прочитать norms.txt")
    return ""


async def main() -> None:
    global NORMS_TEXT

    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задана переменная окружения TELEGRAM_TOKEN")
    if not ANTHROPIC_API_KEY:
        raise SystemExit("Не задана переменная окружения ANTHROPIC_API_KEY")

    NORMS_TEXT = load_norms()

    bot = Bot(token=TELEGRAM_TOKEN)
    runner = await start_web_server()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Бот запущен, модель: %s", MODEL_NAME)
        await router_dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
