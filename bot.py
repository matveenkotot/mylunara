import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from kerykeion.astrological_subject_factory import AstrologicalSubjectFactory

load_dotenv()
logging.basicConfig(level=logging.INFO)

GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("LUNARA_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("LUNARA_BOT_TOKEN") or
                  next((v for k, v in os.environ.items() if k.strip() == "TELEGRAM_TOKEN"), None))

ADMIN_ID = 353539738

client = None
users = {}

# ── Персистентность ─────────────────────────────────────────────────────────

def _find_data_dir() -> str:
    """Ищет первую доступную для записи директорию."""
    candidates = [
        os.getenv("DATA_DIR"),
        "/data",
        os.path.dirname(os.path.abspath(__file__)),
    ]
    for d in candidates:
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".write_probe")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            logging.info(f"Хранилище данных: {d}")
            return d
        except Exception as e:
            logging.warning(f"Директория {d} недоступна: {e}")
    logging.error("Не найдено ни одной доступной директории для данных!")
    return "/tmp"

DATA_DIR = _find_data_dir()
USERS_FILE = os.path.join(DATA_DIR, "users.json")

def load_users():
    global users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            users = {int(k): v for k, v in data.items()}
            logging.info(f"Загружено {len(users)} пользователей из {USERS_FILE}")
        else:
            logging.info(f"Файл {USERS_FILE} не найден — начинаем с пустой базы")
            users = {}
    except Exception as e:
        logging.error(f"Ошибка загрузки users.json: {e}")
        users = {}

def save_users():
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=2)
        logging.info(f"Сохранено {len(users)} пользователей → {USERS_FILE}")
    except Exception as e:
        logging.error(f"Ошибка сохранения users.json: {e}")

# ── Астрология ──────────────────────────────────────────────────────────────

SIGNS_RU = {
    "Ari": "Овен", "Tau": "Телец", "Gem": "Близнецы", "Can": "Рак",
    "Leo": "Лев", "Vir": "Дева", "Lib": "Весы", "Sco": "Скорпион",
    "Sag": "Стрелец", "Cap": "Козерог", "Aqu": "Водолей", "Pis": "Рыбы"
}

CITIES = {
    "москва": (55.7558, 37.6176, "Europe/Moscow"),
    "москве": (55.7558, 37.6176, "Europe/Moscow"),
    "санкт-петербург": (59.9343, 30.3351, "Europe/Moscow"),
    "петербург": (59.9343, 30.3351, "Europe/Moscow"),
    "питер": (59.9343, 30.3351, "Europe/Moscow"),
    "спб": (59.9343, 30.3351, "Europe/Moscow"),
    "екатеринбург": (56.8389, 60.6057, "Asia/Yekaterinburg"),
    "новосибирск": (54.9833, 82.8964, "Asia/Novosibirsk"),
    "казань": (55.7879, 49.1233, "Europe/Moscow"),
    "краснодар": (45.0355, 38.9753, "Europe/Moscow"),
    "нижний новгород": (56.3269, 44.0059, "Europe/Moscow"),
    "нижнем новгороде": (56.3269, 44.0059, "Europe/Moscow"),
    "самара": (53.2001, 50.1500, "Europe/Samara"),
    "уфа": (54.7388, 55.9721, "Asia/Yekaterinburg"),
    "ростов": (47.2357, 39.7015, "Europe/Moscow"),
    "омск": (54.9885, 73.3242, "Asia/Omsk"),
    "челябинск": (55.1644, 61.4368, "Asia/Yekaterinburg"),
    "пермь": (58.0105, 56.2502, "Asia/Yekaterinburg"),
    "волгоград": (48.7194, 44.5018, "Europe/Moscow"),
    "красноярск": (56.0153, 92.8932, "Asia/Krasnoyarsk"),
    "воронеж": (51.6720, 39.1843, "Europe/Moscow"),
    "минск": (53.9045, 27.5615, "Europe/Minsk"),
    "киев": (50.4501, 30.5234, "Europe/Kiev"),
    "харьков": (49.9935, 36.2304, "Europe/Kiev"),
    "алматы": (43.2567, 76.9286, "Asia/Almaty"),
    "астана": (51.1801, 71.4460, "Asia/Almaty"),
    "ташкент": (41.2995, 69.2401, "Asia/Tashkent"),
    "баку": (40.4093, 49.8671, "Asia/Baku"),
    "тбилиси": (41.6941, 44.8337, "Asia/Tbilisi"),
    "ереван": (40.1872, 44.5152, "Asia/Yerevan"),
}

def s(sign_en: str) -> str:
    return SIGNS_RU.get(sign_en, sign_en)

def parse_birth_data(text: str) -> dict | None:
    """Парсит строку пользователя и возвращает данные рождения."""
    text_lower = text.lower().strip()

    date_match = re.search(r'(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})', text)
    if not date_match:
        return None

    day = int(date_match.group(1))
    month = int(date_match.group(2))
    year = int(date_match.group(3))

    time_match = re.search(r'(\d{1,2})[:\-](\d{2})', text)
    has_time = bool(time_match)
    hour = int(time_match.group(1)) if has_time else 12
    minute = int(time_match.group(2)) if has_time else 0

    # Ищем город (сначала длинные фразы, потом короткие)
    lat, lng, tz = 55.7558, 37.6176, "Europe/Moscow"
    city_name = "Москва (по умолчанию)"
    for city_key in sorted(CITIES.keys(), key=len, reverse=True):
        if city_key in text_lower:
            lat, lng, tz = CITIES[city_key]
            city_name = city_key.capitalize()
            break

    return {
        "day": day, "month": month, "year": year,
        "hour": hour, "minute": minute,
        "has_time": has_time,
        "lat": lat, "lng": lng, "tz": tz,
        "city": city_name
    }

def build_astro_context(bd: dict) -> str:
    """Строит контекст натальной карты + актуальных транзитов для system prompt."""
    try:
        natal = AstrologicalSubjectFactory.from_birth_data(
            name="User",
            year=bd["year"], month=bd["month"], day=bd["day"],
            hour=bd["hour"], minute=bd["minute"],
            city="City", nation="RU",
            lat=bd["lat"], lng=bd["lng"], tz_str=bd["tz"]
        )

        now = datetime.now(timezone.utc)
        transits = AstrologicalSubjectFactory.from_birth_data(
            name="Transits",
            year=now.year, month=now.month, day=now.day,
            hour=now.hour, minute=now.minute,
            city="London", nation="GB",
            lat=51.5074, lng=-0.1278, tz_str="UTC"
        )

        asc_line = (
            f"Асцендент в {s(natal.first_house.sign)} — как её воспринимают окружающие"
            if bd["has_time"]
            else "Асцендент: время рождения не указано, не рассчитывается"
        )

        return f"""=== НАТАЛЬНАЯ КАРТА ПОЛЬЗОВАТЕЛЯ ===
Дата рождения: {bd['day']:02d}.{bd['month']:02d}.{bd['year']}, город: {bd['city']}

Солнце в {s(natal.sun.sign)} ({round(natal.sun.position, 1)}°) — базовая личность и жизненный путь
Луна в {s(natal.moon.sign)} ({round(natal.moon.position, 1)}°) — эмоции и внутренний мир
{asc_line}
Меркурий в {s(natal.mercury.sign)} — мышление и коммуникация
Венера в {s(natal.venus.sign)} — стиль в любви и отношениях
Марс в {s(natal.mars.sign)} — энергия и действия
Юпитер в {s(natal.jupiter.sign)} — зона роста и удачи
Сатурн в {s(natal.saturn.sign)} — зона испытаний
Уран в {s(natal.uranus.sign)} — зона неожиданных перемен
Нептун в {s(natal.neptune.sign)} — интуиция и иллюзии
Плутон в {s(natal.pluto.sign)} — зона глубокой трансформации

=== ТЕКУЩИЕ ТРАНЗИТЫ ({now.strftime('%d.%m.%Y')}) ===
Солнце транзитирует {s(transits.sun.sign)} ({round(transits.sun.position, 1)}°)
Луна транзитирует {s(transits.moon.sign)} ({round(transits.moon.position, 1)}°) — меняется каждые 2.5 дня
Меркурий транзитирует {s(transits.mercury.sign)}
Венера транзитирует {s(transits.venus.sign)}
Марс транзитирует {s(transits.mars.sign)}
Юпитер транзитирует {s(transits.jupiter.sign)}
Сатурн транзитирует {s(transits.saturn.sign)}
Уран транзитирует {s(transits.uranus.sign)}
Нептун транзитирует {s(transits.neptune.sign)}
Плутон транзитирует {s(transits.pluto.sign)}

Используй эти данные для точного персонального совета. Анализируй аспекты между натальными планетами и транзитами."""

    except Exception as e:
        logging.error(f"Astro calculation error: {e}")
        if bd:
            return f"Данные рождения: {bd['day']:02d}.{bd['month']:02d}.{bd['year']} (расчёт карты временно недоступен)"
        return "Данные рождения недоступны"


# ── System prompt ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — Lunara, персональный астрологический компаньон для женщин 30+.
Говори тепло и по-дружески, как близкая подруга, которая глубоко разбирается в астрологии.
Не как справочник — а как живой человек, которому не всё равно.

СПЕЦИАЛИЗАЦИЯ: деньги, отношения, крупные решения (покупки, переезды, карьера). Если просят другое — мягко объясни специализацию.

НЕЛЬЗЯ:
- Предсказывать смерть, болезни, точные даты событий
- Давать советы по здоровью, юридическим или финансовым инвестициям (конкретные акции, криптовалюта)
- Обсуждать политику, религию, нумерологию, таро, прошлые жизни
- Говорить «ты точно встретишь любовь» — только вероятности и тенденции
- НИКОГДА не рекомендовать обратиться к другому астрологу или консультанту — ты сама и есть этот специалист
- НИКОГДА не говорить что у тебя "закончились бесплатные советы" или ограничения — это техническая часть, не твоя зона

КОММЕРЧЕСКОЕ ПОВЕДЕНИЕ:
- Ты — платный сервис, альтернатива дорогим астрологам (3-10к за сессию)
- Если тема выходит за рамки специализации — мягко перенаправь на то, в чём можешь помочь: «Это скорее для юриста, но если говорить об астрологическом аспекте твоего решения — давай посмотрим на карту»
- Никогда не заканчивай разговор словами "спасибо за общение" или "удачи" — всегда оставляй дверь открытой для следующего вопроса

МЕТОДОЛОГИЯ (западная астрология, система Плацидуса):
- Анализируй аспекты между натальными планетами пользователя и текущими транзитами
- 7-й дом = партнёрство и брак; 2-й и 8-й дом = деньги; 10-й дом = карьера
- Соединение, трин, секстиль — гармоничные аспекты; квадрат, оппозиция — напряжённые
- Транзит Юпитера = возможности и рост; Сатурна = испытания и уроки; Марса = действия и конфликты

СТРУКТУРА КАЖДОГО ОТВЕТА (строго следуй):
1. Конкретный вывод по запросу (1-2 предложения) — что карта говорит по данной теме
2. Ключевые аспекты (2-3 пункта через • ) — только самые значимые транзиты для этого вопроса
3. Практический совет или вывод (1-2 предложения)
4. Один уточняющий вопрос — только если он реально нужен для более точного ответа

СТИЛЬ:
- Пиши ТОЛЬКО на русском языке
- Используй • для списков, пустую строку между блоками
- Не пиши длинные абзацы — разбивай на короткие части
- НИКОГДА не используй обращения: «милая», «дорогая», «красавица» — только уважительно, без фамильярности
- Не начинай ответ с обращения — сразу по делу
- Если пользователь говорит «да», «готова», «конечно» — это СОГЛАСИЕ, не новый вопрос. Дай конкретный совет, не задавай новый вопрос вместо ответа
- Не повторяй один и тот же тип вопроса дважды подряд
- Если уже спрашивал про финансы — переходи к выводу, не спрашивай снова

ВАЖНО: Если пользователь уточняет детали по своей просьбе — это НЕ новый запрос. Продолжай начатый разговор."""


# ── AI-классификатор тем ─────────────────────────────────────────────────

def classify_is_new_topic(text: str, history: list) -> bool:
    """Возвращает True если сообщение — новая тема, False если уточнение текущей."""
    if not history:
        return True
    try:
        recent = history[-8:]
        context = "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'Бот'}: {m['content'][:400]}"
            for m in recent
        )
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты классификатор диалогов. Определи: сообщение пользователя — НОВАЯ ТЕМА или ПРОДОЛЖЕНИЕ текущего разговора?\n\n"
                        "НОВАЯ ТЕМА (new) — если в сообщении появляется НОВЫЙ объект, событие или жизненная ситуация, отличная от той, что обсуждалась:\n"
                        "  • Обсуждали собаку → пишет про дачу = НОВАЯ ТЕМА\n"
                        "  • Обсуждали дачу → пишет про парня/брак = НОВАЯ ТЕМА\n"
                        "  • Обсуждали финансы → пишет про отношения = НОВАЯ ТЕМА\n"
                        "  • Слова «а ещё», «ещё хочу», «кстати» перед новым объектом = НОВАЯ ТЕМА\n\n"
                        "ПРОДОЛЖЕНИЕ (continue) — если пользователь уточняет ТОТ ЖЕ объект или отвечает на вопрос бота:\n"
                        "  • Бот спросил про породу → пишет «хочу лабрадора» = ПРОДОЛЖЕНИЕ\n"
                        "  • Бот спросил готова ли → пишет «да», «готова», «конечно» = ПРОДОЛЖЕНИЕ\n"
                        "  • Бот спросил про район → пишет «север Москвы» = ПРОДОЛЖЕНИЕ\n\n"
                        "Отвечай строго одним словом: 'new' или 'continue'."
                    )
                },
                {
                    "role": "user",
                    "content": (
                        f"История диалога:\n{context}\n\n"
                        f"Новое сообщение пользователя: «{text}»\n\n"
                        "Это новая тема или продолжение?"
                    )
                }
            ],
            max_tokens=5,
            temperature=0,
        )
        result = response.choices[0].message.content.strip().lower()
        return "new" in result
    except Exception as e:
        logging.error(f"Topic classification error: {e}")
        return True  # при ошибке считаем новой темой (в пользу монетизации)


# ── Хранилище пользователей ──────────────────────────────────────────────

def get_user(user_id):
    if user_id not in users:
        users[user_id] = {
            "free_advice_count": 0,
            "birth_data": None,
            "birth_parsed": None,
            "history": [],
            "step": "start",
            "subscription": None,
        }
    return users[user_id]


# ── Handlers ─────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)

    # Если данные рождения уже есть — не сбрасываем, просто приветствуем
    if user.get("birth_parsed") and user.get("step") == "ready":
        bd = user["birth_parsed"]
        sub = user.get("subscription")
        if sub == "premium":
            sub_text = "✨ Подписка Премиум активна"
        elif sub == "base":
            sub_text = "⭐ Подписка Базовая активна"
        else:
            remaining = max(0, 3 - user["free_advice_count"])
            sub_text = f"🌙 Осталось бесплатных советов: {remaining} из 3"

        await update.message.reply_text(
            f"С возвращением 🌙\n\n"
            f"Твоя карта на {bd['day']:02d}.{bd['month']:02d}.{bd['year']} ({bd['city']}) уже сохранена.\n"
            f"{sub_text}\n\n"
            "Задавай вопрос — с чем разбираемся сегодня?",
        )
        return

    # Новый пользователь или нет данных рождения
    user["step"] = "collecting_birth"
    user["history"] = []
    save_users()

    await update.message.reply_text(
        "Привет! Я Lunara — твой персональный астрологический компаньон 🌙\n\n"
        "Я помогаю с конкретными ситуациями в деньгах и отношениях — не общими гороскопами, "
        "а советом именно для тебя, на основе твоей натальной карты.\n\n"
        "Чтобы начать, напиши дату рождения в формате:\n"
        "*ДД.ММ.ГГГГ ЧЧ:ММ Город*\n\n"
        "Например: `15.03.1990 14:30 Москва`\n\n"
        "Время и город желательны для точного расчёта, но если не знаешь — напиши только дату.",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    text = update.message.text

    # Шаг 0: пользователь не запустил /start
    if user["step"] == "start":
        user["step"] = "collecting_birth"
        await update.message.reply_text(
            "Привет! Я Lunara — твой персональный астрологический компаньон 🌙\n\n"
            "Чтобы начать, напиши дату рождения в формате:\n"
            "*ДД.ММ.ГГГГ ЧЧ:ММ Город*\n\n"
            "Например: `15.03.1990 14:30 Москва`",
            parse_mode="Markdown"
        )
        return

    # Шаг 1: сбор данных рождения
    if user["step"] == "collecting_birth":
        parsed = parse_birth_data(text)
        if not parsed:
            await update.message.reply_text(
                "Не смогла распознать дату рождения 🌙\n\n"
                "Напиши, пожалуйста, в формате: *ДД.ММ.ГГГГ ЧЧ:ММ Город*\n"
                "Например: `15.03.1990 14:30 Москва`",
                parse_mode="Markdown"
            )
            return

        user["birth_data"] = text
        user["birth_parsed"] = parsed
        user["step"] = "ready"
        user["history"] = []
        save_users()

        time_note = "" if parsed["has_time"] else "\n_(Время не указано — асцендент не рассчитывается, но всё остальное будет точным)_"
        await update.message.reply_text(
            f"Принято ✨ Рассчитываю твою карту...\n\n"
            f"Дата: {parsed['day']:02d}.{parsed['month']:02d}.{parsed['year']}, "
            f"город: {parsed['city']}{time_note}\n\n"
            "Теперь расскажи — с чем хочешь разобраться? "
            "Есть какая-то конкретная ситуация прямо сейчас?\n\n"
            "Например: получила оффер о работе, познакомилась с мужчиной, "
            "думаю открыть бизнес — что угодно, что тебя беспокоит.",
            parse_mode="Markdown"
        )
        return

    has_subscription = user["subscription"] in ("base", "premium")

    # AI-классификатор: новая тема или уточнение текущей?
    is_new_topic = classify_is_new_topic(text, user["history"]) if user["history"] else True
    is_clarification = not is_new_topic

    # Пейволл
    if not has_subscription and is_new_topic and user["free_advice_count"] >= 3:
        await send_paywall(update)
        return

    # Предупреждение об остатке (только для новых тем)
    if not has_subscription and is_new_topic:
        if user["free_advice_count"] == 1:
            await update.message.reply_text("💫 У тебя осталось 2 бесплатных совета в этом месяце.")
        elif user["free_advice_count"] == 2:
            await update.message.reply_text("💫 У тебя остался 1 бесплатный совет в этом месяце.")

    await update.message.reply_text("🔮 Смотрю карту...")

    try:
        astro_context = build_astro_context(user["birth_parsed"])

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + astro_context}
        ] + user["history"] + [
            {"role": "user", "content": text}
        ]

        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            max_tokens=600,
            temperature=0.7,
        )

        reply = response.choices[0].message.content

        user["history"].append({"role": "user", "content": text})
        user["history"].append({"role": "assistant", "content": reply})

        # Не сжигаем слот если бот отказал (вне специализации)
        refused = any(phrase in reply for phrase in [
            "вне моей специализации", "выходит за пределы", "не могу дать совет",
            "обратитесь к", "рекомендую обратиться"
        ])
        if not has_subscription and is_new_topic and not refused:
            user["free_advice_count"] += 1

        save_users()
        await update.message.reply_text(reply)

        if not has_subscription and is_new_topic and not refused and user["free_advice_count"] >= 3:
            await send_paywall(update)

    except Exception as e:
        await update.message.reply_text(
            "Что-то пошло не так со связью со звёздами 🌌 Попробуй ещё раз."
        )
        logging.error(f"Error: {e}")

async def send_paywall(update: Update):
    await update.message.reply_text(
        "🌙 Ты использовала все 3 бесплатных совета этого месяца.\n\n"
        "Чтобы продолжить — выбери тариф:\n\n"
        "⭐ *Базовая — 299 ₽/месяц*\n"
        "Безлимитные персональные советы по деньгам и отношениям + ежедневный транзитный прогноз\n\n"
        "✨ *Премиум — 490 ₽/месяц*\n"
        "Всё из базовой + совместимость с конкретным человеком, финансовое окно на месяц, "
        "благоприятные даты для сделок\n\n"
        "💳 *Разовые покупки:*\n"
        "• Проверка совместимости — 199 ₽\n"
        "• Финансовое окно на месяц — 149 ₽\n\n"
        "_Для оплаты напиши: @matveenkotot_\n\n"
        "_Живой астролог берёт 3–10к за сессию. Здесь — персонально и без ожидания 💫_",
        parse_mode="Markdown"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    users[user_id] = {
        "free_advice_count": 0,
        "birth_data": None,
        "birth_parsed": None,
        "history": [],
        "step": "collecting_birth",
        "subscription": None,
        "bot_asked_question": False,
    }
    save_users()
    await update.message.reply_text("Данные сброшены. Напиши дату рождения заново:")


# ── Команды администратора ───────────────────────────────────────────────

async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grant base 123456789 или /grant premium 123456789"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Формат: /grant base 123456789 или /grant premium 123456789")
        return
    plan = args[0]
    try:
        target_id = int(args[1])
    except ValueError:
        await update.message.reply_text("Неверный ID")
        return
    if plan not in ("base", "premium"):
        await update.message.reply_text("Тариф: base или premium")
        return
    get_user(target_id)["subscription"] = plan
    save_users()
    await update.message.reply_text(f"✅ Подписка {plan} выдана пользователю {target_id}")
    try:
        plan_name = "Базовая" if plan == "base" else "Премиум"
        await context.bot.send_message(
            chat_id=target_id,
            text=f"🌟 Твоя подписка *{plan_name}* активирована! Теперь задавай вопросы без ограничений ✨",
            parse_mode="Markdown"
        )
    except Exception:
        pass

async def revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/revoke 123456789"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Формат: /revoke 123456789")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID")
        return
    get_user(target_id)["subscription"] = None
    save_users()
    await update.message.reply_text(f"✅ Подписка отозвана у пользователя {target_id}")

async def resetcount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetcount 123456789 — обнуляет счётчик бесплатных советов"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Формат: /resetcount 123456789")
        return
    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID")
        return
    if target_id not in users:
        await update.message.reply_text(f"❌ Пользователь {target_id} не найден в базе")
        return
    old_count = users[target_id].get("free_advice_count", 0)
    users[target_id]["free_advice_count"] = 0
    save_users()
    await update.message.reply_text(
        f"✅ Счётчик сброшен для {target_id}\n"
        f"Было: {old_count} → Стало: 0"
    )
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text="🌙 Твои бесплатные советы обновлены — можешь задавать новые вопросы!"
        )
    except Exception:
        pass


# ── Команды для пользователей ────────────────────────────────────────────

async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    file_exists = os.path.exists(USERS_FILE)
    file_size = os.path.getsize(USERS_FILE) if file_exists else 0
    lines = [
        f"DATA_DIR: {DATA_DIR}",
        f"USERS_FILE: {USERS_FILE}",
        f"Файл существует: {file_exists}",
        f"Размер файла: {file_size} байт",
        f"Пользователей в памяти: {len(users)}",
    ]
    for uid, u in list(users.items())[:5]:
        lines.append(f"  uid={uid} step={u.get('step')} count={u.get('free_advice_count')} sub={u.get('subscription')}")
    await update.message.reply_text("\n".join(lines))

async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Твой Telegram ID: `{update.effective_user.id}`\n\n"
        "Отправь его @matveenkotot после оплаты — подписка активируется в течение нескольких минут.",
        parse_mode="Markdown"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    sub = user.get("subscription")
    if sub == "premium":
        text = "✨ У тебя активна подписка *Премиум*"
    elif sub == "base":
        text = "⭐ У тебя активна подписка *Базовая*"
    else:
        remaining = max(0, 3 - user["free_advice_count"])
        text = f"🌙 Бесплатный план. Осталось советов: *{remaining}* из 3"
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    global client
    load_users()
    logging.info(f"GROQ_API_KEY: {GROQ_API_KEY[:15] if GROQ_API_KEY else 'NOT SET'}")
    logging.info(f"TELEGRAM_TOKEN: {TELEGRAM_TOKEN[:15] if TELEGRAM_TOKEN else 'NOT SET'}")

    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set!")

    client = OpenAI(
        api_key=GROQ_API_KEY,
        base_url="https://api.groq.com/openai/v1"
    )

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("grant", grant))
    app.add_handler(CommandHandler("revoke", revoke))
    app.add_handler(CommandHandler("resetcount", resetcount))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logging.info("Lunara запущена 🌙")
    time.sleep(15)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
