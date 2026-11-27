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
import logging
import os
import re
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F
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
MAX_TOKENS = 3000         # лимит ответа Claude
TG_LIMIT = 4000           # безопасный лимит длины одного сообщения Telegram
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
# Тексты интерфейса
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
            "Требования: компактная сводка ТОЛЬКО по отклонениям (🟡 и 🔴) с цифрами и единицами; "
            "укажи, к какому врачу идти; дай 4–5 вопросов для приёма у врача. "
            "Не ставь диагнозов и не назначай лечение. "
            "В начале и в конце — короткий дисклеймер, что ответ носит информационный характер."
        ),
        "photo_default": "Вот фото бланка анализа крови. Распознай показатели и разбери их.",
        "photo_note": "[Пользователь отправил фото бланка анализа]",
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
            "Талаптар: тек ауытқулар (🟡 және 🔴) бойынша шағын жиынтық, сандары мен өлшем бірліктерімен; "
            "қай дәрігерге бару керегін көрсет; дәрігер қабылдауына 4–5 сұрақ бер. "
            "Диагноз қойма, емдеу тағайындама. "
            "Басында және соңында ақпараттық сипаттағы қысқа ескерту жаз."
        ),
        "photo_default": "Мынау қан анализі бланкісінің фотосы. Көрсеткіштерді танып, талдап бер.",
        "photo_note": "[Пайдаланушы анализ бланкісінің фотосын жіберді]",
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
# Системный промпт
# ---------------------------------------------------------------------------


def build_system_prompt(profile: dict) -> str:
    lang_name = "русском" if profile["lang"] == "ru" else "казахском"
    sex_name = "мужской" if profile["sex"] == "m" else "женский"

    norms_block = NORMS_TEXT.strip() or (
        "[ФАЙЛ norms.txt ПУСТ ИЛИ НЕ НАЙДЕН. Нормативов нет: прямо сообщи пользователю, "
        "что не можешь оценить показатели по «Светофору» без справочника норм, "
        "и не придумывай нормы самостоятельно.]"
    )

    return f"""Ты — информационный помощник, который помогает молодым людям (18–25 лет) понять результаты общего и биохимического анализа крови простым языком.

ЯЗЫК ОБЩЕНИЯ: строго на {lang_name} языке. Не переключайся на другой язык, даже если пользователь напишет на другом.

ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ: пол — {sex_name}; возраст — {profile['age']} лет. Учитывай это при оценке показателей по нормам.

СТРОГИЕ ПРАВИЛА:
1. Ты НЕ ставишь диагнозы и НЕ назначаешь лечение, лекарства, добавки или дозировки. Не пиши «у вас такое-то заболевание». Можно говорить лишь о том, с чем МОГУТ быть связаны отклонения, и всегда рекомендовать обсудить это с врачом.
2. В НАЧАЛЕ и в КОНЦЕ каждого разбора анализа пиши короткий дисклеймер: ответ носит информационный характер и не заменяет консультацию врача.
3. Оценивай показатели ТОЛЬКО по цифрам из справочника норм ниже (с учётом пола и возраста). Не используй нормы из собственной памяти. Если показателя нет в справочнике, так и скажи: «в справочнике нет нормы для этого показателя», и не присваивай ему цвет.
4. Не выдумывай значения. Если что-то на фото нечитаемо или неясно — переспроси пользователя, а не угадывай.

СТРУКТУРА РАЗБОРА АНАЛИЗА (когда пользователь прислал показатели текстом или фото):
1) Дисклеймер (коротко).
2) «Распознанные показатели»: список всех распознанных показателей с их значениями и единицами измерения.
3) «Светофор»: оценка каждого показателя —
   🟢 Норма
   🟡 Пограничное
   🔴 Требует внимания
   Для каждого показателя укажи значение, референсный диапазон из справочника и цвет.
4) «Что означают показатели»: расшифруй аббревиатуры (например, HGB, RBC, WBC, PLT, ALT, AST) простым языком, без сложных терминов.
5) «Связки показателей»: отметь, какие показатели стоит рассматривать вместе (например, гемоглобин + эритроциты + ферритин + MCV), и что такие сочетания могут означать, без постановки диагноза.
6) «К какому врачу обратиться»: при отклонениях (🟡/🔴) подскажи профильного врача (терапевт, гематолог, эндокринолог, гастроэнтеролог и т. д.). Если всё в норме — скажи, что срочных причин для визита нет, но плановое наблюдение у терапевта полезно.
7) «Источники»: перечисли ссылки на источники, указанные в справочнике норм. Не придумывай ссылки; если в справочнике их нет — не добавляй этот раздел.
8) Дисклеймер (коротко) — ПОСЛЕДНЯЯ строка ответа.

ПОВТОРНЫЙ АНАЛИЗ (если в истории диалога уже есть предыдущий разбор и пользователь прислал новые показатели):
- Сделай разбор по той же структуре, а затем добавь раздел «Было / Стало»: сравни новые цифры с предыдущими.
- Сравнивай в ЕДИНИЦАХ ИЗМЕРЕНИЯ (например, «было 105 г/л → стало 118 г/л, +13 г/л»). Проценты НЕ считай и не упоминай.
- По каждому показателю напиши, что улучшилось (приблизилось к норме), что ухудшилось (отдалилось от нормы), а что осталось без изменений.

ШПАРГАЛКА ВРАЧУ (если пользователь просит её):
- Компактная сводка ТОЛЬКО по отклонениям (🟡 и 🔴): показатель, значение, единицы, референс.
- Укажи, к какому врачу идти.
- Дай 4–5 конкретных вопросов, которые можно задать врачу на приёме.
- Короткий дисклеймер в начале и в конце.

ФОРМАТ: пиши обычным текстом с эмодзи, БЕЗ Markdown-разметки (не используй **, __, #, ``` и таблицы). Для списков используй дефисы или цифры. Пиши дружелюбно, понятно, не пугай пользователя.

Если пользователь пишет что-то, не связанное с анализом крови, вежливо верни разговор к теме: попроси прислать показатели анализа текстом или фото.

===== СПРАВОЧНИК НОРМ (norms.txt) — ЕДИНСТВЕННЫЙ ИСТОЧНИК НОРМ И ССЫЛОК =====
{norms_block}
===== КОНЕЦ СПРАВОЧНИКА ====="""


# ---------------------------------------------------------------------------
# Клиенты
# ---------------------------------------------------------------------------
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
router_dp = Dispatcher()


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


async def send_long(message: Message, text: str, reply_markup=None) -> None:
    parts = split_text(text)
    for i, part in enumerate(parts):
        markup = reply_markup if (i == len(parts) - 1) else None
        await message.answer(part, reply_markup=markup)


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
            await send_long(call.message, answer)
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

            await send_long(message, answer)
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
            await send_long(message, answer)
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
