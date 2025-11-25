import os
import re
import json
import random
import requests
import logging
import atexit
import base64
import xml.etree.ElementTree as ET
from typing import Optional, Dict, List, Any
from telegram import Update
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from threading import Thread
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from openai import OpenAI

from holidays import build_holidays_section, load_birthdays, birthdays_for_date
from chat_handler import init_chat_handler, get_chat_handler
from chat_history_manager import ChatHistoryManager

# --- окружение
load_dotenv(dotenv_path="token.env", override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
CALENDARIFIC_API_KEY = os.getenv("CALENDARIFIC_API_KEY")
API_NINJAS_KEY = os.getenv("API_NINJAS_KEY")
API_NINJAS_PREMIUM = os.getenv("API_NINJAS_PREMIUM", "false").lower() in ("1", "true", "yes")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")

# Intelligence.io
INTELLIGENCE_API_KEY = os.getenv("INTELLIGENCE_API_KEY")
INTELLIGENCE_MODEL = os.getenv("INTELLIGENCE_MODEL", "openai/gpt-oss-120b")
INTELLIGENCE_URL = "https://api.intelligence.io.solutions/api/v1/chat/completions"

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
_openai_client = None

TZ_NAME = os.getenv("TZ", "Europe/Amsterdam")
CITIES_ENV = os.getenv("CITIES", "Леуварден:Leeuwarden,Одесса:Odesa,Варшава:Warsaw")

# Человечные названия стран для вывода
COUNTRY_NAMES = {"NL": "Нидерланды", "UA": "Украина", "PL": "Польша"}

# Список примет для ротации
omen_themes = [
    "если кошка умывается лапкой — к хорошей погоде",
    "если утром нашёл парные носки с первой попытки — день будет удачным",
    "если кофе получился особенно вкусным — жди хороших новостей",
    "если птицы поют с утра — к солнечному дню",
    "если зеркало блестит без протирания — к приятным встречам",
    "если телефон заряжается быстрее обычного — к продуктивному дню",
    "если успел на автобус, не торопясь — день пройдет легко",
    "если книга сама открылась на нужной странице — к удаче в делах",
    "если нашел мелочь на улице — к неожиданному подарку",
    "если чай заварился ярче обычного — к хорошим вестям",
    "если подушка с утра особенно мягкая — день будет приятным",
    "если ключи нашлись сразу — все дела пойдут гладко",
    "если успел перейти дорогу на зеленый — удача на твоей стороне",
    "если первый блин не комом — к успеху во всех начинаниях",
    "если зонтик взял, а дождя нет — значит, день будет солнечным"
]

# --- Список стран для «широкого поиска» (ENV)
SCAN_COUNTRIES_ENV = os.getenv(
    "SCAN_COUNTRIES_ENV",
    "US:США,DE:Германия,FR:Франция,ES:Испания,IT:Италия,GB:Великобритания"
)
SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
chat_history_manager = ChatHistoryManager("chat_history.json")

# --- города RU:EN
CITIES = []
for pair in CITIES_ENV.split(","):
    ru, en = pair.split(":")
    CITIES.append((ru.strip(), en.strip()))

# --- валидация/очистка
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]")
EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002600-\U000026FF]", flags=re.UNICODE)

BAD_MARKERS = [
    "we need", "the user asks", "let's craft", "let us", "explain", "instruction",
    "output json", "return json", "формат json", "без пояснений", "требуется", "нужно сделать"
]

# Добавьте после других глобальных переменных
USED_OMENS = set()
MAX_OMEN_HISTORY = 10

def _get_openai():
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _openai_client

# Удалил локальную дефиницию init_chat_handler (она перекрывала импорт из chat_handler)
def is_russian_strict(text: str) -> bool:
    if not text:
        return False
    return len(CYRILLIC_RE.findall(text)) > 0 and len(LATIN_RE.findall(text)) == 0

def normalize(s: str) -> str:
    s = EMOJI_RE.sub("", s or "")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" «»\"'")

def clamp_words(text: str, min_w=8, max_w=16) -> str:
    words = normalize(text).split(" ")
    words = [w for w in words if w]
    if len(words) > max_w:
        words = words[:max_w]
    return " ".join(words)

# 2) Время суток и ИИ‑приветствие
def time_of_day(now_local) -> tuple[str, str]:
    h = now_local.hour
    if 5 <= h < 12:
        return "утро", "Доброе утро"
    if 12 <= h < 18:
        return "день", "Добрый день"
    if 18 <= h < 23:
        return "вечер", "Добрый вечер"
    return "ночь", "Доброй ночи"

def sanitize_comment(text: str) -> str:
    lines = [normalize(x) for x in (text or "").splitlines() if normalize(x)]
    for ln in lines:
        low = ln.lower()
        if any(m in low for m in BAD_MARKERS):
            continue
        if not is_russian_strict(ln):
            continue
        ln = ln.replace("#", "")
        ln = clamp_words(ln, 8, 16)
        ln = EMOJI_RE.sub("", ln)  # никаких эмодзи в комментарии
        return ln
    return ""

def sanitize_wish(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n")
    # Не теряем пустые строки — они нужны перед цитатой
    lines = []
    for ln in raw.split("\n"):
        base = normalize(ln)
        # пустую строку сохраняем
        if base == "":
            lines.append("")
            continue
        # валидация кириллицы (сохраняем blockquote даже если есть теги)
        core = re.sub(r"[^\u0400-\u04FF ]", "", base)
        if core and not is_russian_strict(core):
            letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", base)
            if LATIN_RE.search(letters):
                continue
        lines.append(base)
    out = "\n".join(lines).strip()

    # Если слишком длинно — сократим часть до цитаты, но не цитату
    if len(out) > 360 and "<blockquote>" in out:
        head, _, tail_after = out.partition("<blockquote>")
        tail = "<blockquote>" + tail_after
        head = head.strip()
        if len(head) > 300:
            head = head[:300]
            # обрезаем по последнему пробелу, чтобы не резать слово
            if " " in head:
                head = head[:head.rfind(" ")] + "…"
        out = (head + "\n\n" + tail).strip()
    return out

def get_fresh_omen(themes: list, used_omens: set) -> str:
    """Возвращает свежую примету, которая не использовалась недавно"""
    available_themes = [t for t in themes if t not in used_omens]
    
    if not available_themes:
        # Если все темы использовались, очищаем историю и начинаем заново
        used_omens.clear()
        available_themes = themes
    
    selected = random.choice(available_themes)
    used_omens.add(selected)
    
    # Ограничиваем размер истории
    if len(used_omens) > MAX_OMEN_HISTORY:
        # Удаляем самую старую запись (но set не упорядочен, поэтому просто очищаем часть)
        used_omens.clear()
        used_omens.add(selected)
    
    return selected

HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.I)
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}

def parse_scan_countries(env_str: str) -> list[tuple[str, str]]:
    pairs = []
    for token in (env_str or "").split(","):
        token = token.strip()
        if not token or ":" not in token:
            continue
        iso, name = token.split(":", 1)
        pairs.append((iso.strip().upper(), name.strip()))
    return pairs

SCAN_COUNTRIES = parse_scan_countries(SCAN_COUNTRIES_ENV)



def strip_unsupported_html(s: str) -> str:
    s = HTML_BR_RE.sub("\n", s or "")
    # Удалим любые теги, кроме разрешённых
    def _repl(m):
        tag = m.group(1).lower().strip("/")
        return m.group(0) if tag in ALLOWED_TAGS else ""
    s = re.sub(r"</?([A-Za-z0-9\-]+)(\s+[^>]*)?>", _repl, s)
    return s

def should_respond(update: Update) -> bool:
    """Проверяет, стоит ли отвечать на сообщение"""
    if not update.message or not update.message.text:
        return False

    text = update.message.text.lower()
    chat_id = str(update.effective_chat.id)

    logging.info(f"📨 Получено сообщение: {text}")

    # Список триггеров для реакции
    triggers = [
        r'\b[Бб]олтун\w*',
        r'\b[Пп]оболта\w+',
        r'\b[Пп]оговори\w+', 
        r'\b[Ээ]й\s*,\s*бот',
        r'\b[Пп]ривет\s*,\s*бот',
        r'\b[Бб]от\s*,\s*[Пп]ривет',
        r'\b[Пп]риветствую',
        r'\b[Пп]оздороваться',
        r'\b[Вв]ася\w*',
        r'\b[Вв]асилий\w*',
        r'\b[Бб]олт\w*',
    ]

    # Проверяем каждый триггер
    for trigger in triggers:
        if re.search(trigger, text, re.IGNORECASE):
            logging.info(f"⚡ Обнаружен триггер: {trigger}")
            return True

 # Проверяем, является ли сообщение продолжением диалога
    handler = get_chat_handler()
    if handler and handler.should_continue_conversation(chat_id, text):
        chat_id = str(update.effective_chat.id)
        logging.info("🔄 Обнаружено продолжение диалога по контексту")
        return True
        
    logging.info("🚫 Триггер не найден")
    return False

# ====== ИИ-приветствие «Доброе утро» ======

WEEKDAY_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]

def sanitize_greeting(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    BAD = BAD_MARKERS + ["we need", "user asks", "let's", "format", "instruction", "json"]
    for ln in lines:
        low = ln.lower()
        if any(m in low for m in BAD):
            continue
        core_ru = re.sub(r"[^\u0400-\u04FF ]", "", ln)
        if not is_russian_strict(core_ru):
            letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", ln)
            if LATIN_RE.search(letters):
                continue
        ln = re.sub(r"\s+", " ", ln).strip(" «»\"'")
        if len(ln) < 70:
            ln += " 🙂"
        if len(ln) > 160:
            cut = ln[:160]
            for sep in [". ", " — ", "! ", "? "]:
                if sep in cut:
                    cut = cut[:cut.rfind(sep)+1]
                    break
            ln = cut
        return strip_unsupported_html(ln)
    return ""

RU_MONTHS = [
    "января","февраля","марта","апреля","мая","июня",
    "июля","августа","сентября","октября","ноября","декабря"
]

def human_ru_date(d: datetime) -> str:
    # 21 октября
    return f"{d.day} {RU_MONTHS[d.month-1]}"


def ai_generate_greeting_intel(now_local, first_city: str, wi: dict | None) -> str:
    weekday = WEEKDAY_RU[now_local.weekday()]
    greet_phrase = time_of_day(now_local)[1]
    date_human = human_ru_date(now_local)

    prompt = (
        "Ты — дружелюбный помощник для семейного чата. Создай ОДНО приветствие на русском (120-200 символов).\n\n"
        "ТРЕБОВАНИЯ:\n"
        f"- Начни с «{greet_phrase}»\n"
        f"- Упомяни, что сегодня {weekday}\n"
        "- Тон: тёплый, естественный, с лёгким позитивным юмором\n"
        "- 1-2 уместных эмодзи для оживления текста\n"
        "- Без упоминания городов, стран, погоды\n"
        "- Избегай клише вроде 'пусть день будет хорошим'\n"
        "- Сделай акцент на маленьких радостях и простых моментах\n\n"
        "ПРИМЕРЫ ХОРОШЕГО ТОНА:\n"
        "«Доброе утро! Вторник — отличный повод для маленьких побед и тёплого кофе ☕️»\n"
        "«Добрый день! Среда на полпути — самое время для глубокого вдоха и улыбки 😊»\n\n"
        "Сгенерируй только одну строку приветствия:"
    )

    for _ in range(3):
        raw = _intel_chat(prompt, max_tokens=180, temperature=0.8)
        g = sanitize_greeting(raw)
        if g:
            g = g.replace("\n", " ").replace("  ", " ").strip()
            # Защита от слишком длинных приветствий
            if len(g) > 240:
                g = g[:240]
                if " " in g:
                    g = g[:g.rfind(" ")] + "…"
            return g

    fb = f"{greet_phrase}! {weekday.capitalize()} — время для маленьких радостей и спокойных свершений 😊"
    return sanitize_greeting(fb)




# --- погода (OWM)
def weather_emoji(desc: str, main: str) -> str:
    d = (desc or "").lower()
    m = (main or "").lower()
    if any(x in d for x in ["гроза", "thunder"]) or "thunder" in m:
        return "⛈️"
    if any(x in d for x in ["дожд", "дождь", "rain", "drizzle"]) or m in ["rain", "drizzle"]:
        return "🌧️"
    if any(x in d for x in ["снег", "snow"]) or m == "snow":
        return "❄️"
    if any(x in d for x in ["туман", "дымка", "mist", "fog", "haze"]):
        return "🌫️"
    if any(x in d for x in ["облач", "cloud"]) or m == "cloud":
        return "☁️"
    if any(x in d for x in ["ясно", "clear"]) or m == "clear":
        return "☀️"
    return "🌤️"

def condition_hint(desc: str, main: str, wind_mps: float | None) -> str:
    d = (desc or "").lower()
    m = (main or "").lower()
    hints = []
    if "rain" in d or "дожд" in d or m in ["rain", "drizzle"]:
        hints.append("возьми зонт")
    elif "snow" in d or "снег" in d or m == "snow":
        hints.append("нужны шапка и перчатки")
    elif "clear" in d or "ясн" in d or m == "clear":
        hints.append("SPF и очки кстати")
    elif "cloud" in d or "облач" in d or m == "clouds":
        hints.append("SPF можно не наносить")
    elif "mist" in d or "fog" in d or "туман" in d:
        hints.append("будь осторожнее на дороге")
    if wind_mps and wind_mps >= 7:
        hints.append(f"ветер {int(round(wind_mps))} м/с — шарф пригодится")
    return ", ".join(hints) if hints else "оденься по погоде"

def get_weather(city_en: str):
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"q": city_en, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru"}
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        if r.status_code != 200 or "main" not in data:
            return None
        temp = round(data["main"]["temp"])
        desc = data["weather"][0]["description"]
        main = data["weather"][0].get("main", "")
        wind = (data.get("wind") or {}).get("speed")
        emj = weather_emoji(desc, main)
        hint = condition_hint(desc, main, wind)
        return {"temp": temp, "desc": desc, "main": main, "emoji": emj, "hint": hint}
    except Exception:
        return None

# --- Unsplash random
def get_photo_for_weather(desc: str) -> str:
    d = (desc or "").lower()
    if "дожд" in d or "rain" in d:
        query = "rain landscape"
    elif "снег" in d or "snow" in d:
        query = "snow landscape"
    elif "облач" in d or "cloud" in d:
        query = "cloudy landscape"
    elif "ясн" in d or "sun" in d or "clear" in d:
        query = "sunny landscape"
    else:
        query = "nature landscape"
    url = "https://api.unsplash.com/photos/random"
    params = {"query": query, "orientation": "landscape", "client_id": UNSPLASH_ACCESS_KEY}
    try:
        r = requests.get(url, params=params, timeout=12)
        data = r.json()
        return data.get("urls", {}).get("regular", "")
    except Exception:
        return ""



# --- Инициализация chat_handler
def _intel_chat(prompt: str, max_tokens: int = 400, temperature: float = 0.8) -> str:
    try:
        global _openai_client
        if _openai_client is None:
            _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        resp = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logging.warning("OpenAI error: %s", e)
        return ""

# Инициализация обработчика — передаём также manager истории
chat_handler_instance = init_chat_handler(_intel_chat, chat_history_manager)
# Регистрируем сохранение памяти при завершении (убедитесь, что метод публичный)
atexit.register(getattr(chat_handler_instance, "_save_memory", lambda: None))

# --- вспомогательное: нормализация для дедупликации (убираем город и ключевые слова погоды)
KEY_WEATHER_WORDS = ["пасмур", "ясн", "облач", "дожд", "снег", "туман", "ветер"]

def phrase_skeleton(text: str, city_ru: str, desc: str) -> str:
    t = (text or "").lower()
    t = t.replace(city_ru.lower(), "")
    for kw in KEY_WEATHER_WORDS:
        t = re.sub(kw, "", t)
    t = re.sub(r"[^а-яё0-9 ]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

DEMONYMS = {
    "Леуварден": "нидерландцам",
    "Одесса": "одесситам",
    "Варшава": "живущим в Польше",
}
def demonym(city_ru: str) -> str:
    return DEMONYMS.get(city_ru, "у вас")

# Усиленный промпт: не повторять название города в самой фразе
def ai_generate_comment_intel(city_ru: str, temp_c: int, desc: str, hint: str, avoid_skeletons: set[str] | None = None) -> str:
    locals_term = demonym(city_ru)
    avoid_note = " Избегай повторения конструкций из предыдущих фраз." if avoid_skeletons else ""

    base_prompt = (
        "Ты создаёшь краткие комментарии о погоде для семейного чата.\n\n"
        f"ДАННЫЕ: {temp_c}°C, {desc}. Аудитория: {locals_term}.\n\n"
        "ТРЕБОВАНИЯ:\n"
        "- ОДНА фраза (8-16 слов) на русском\n"
        "- Дружелюбный тон с лёгким юмором\n"
        "- Упомяни погодные условия (перефразируй описание)\n"
        "- Добавь практический совет из подсказки\n"
        "- Не упоминай название города\n"
        "- Без эмодзи, без приветствий, без хэштегов\n"
        "- Естественный разговорный стиль\n\n"
        f"ПОДСКАЗКА ДЛЯ СОВЕТА: {hint}\n\n"
        "ПРИМЕРЫ:\n"
        "«На улице прохладно и ветрено — самое время для тёплого шарфа и бодрой прогулки»\n"
        "«Солнечно и ясно — отличный день для солнечных очков и хорошего настроения»\n"
        "«Лёгкий дождь намекает, что зонт сегодня будет как нельзя кстати»\n\n"
        f"{avoid_note}\n"
        "Сгенерируй только одну фразу:"
    )

    for _ in range(3):
        raw = _intel_chat(base_prompt, max_tokens=120, temperature=0.85)
        sent = sanitize_comment(raw)
        if sent and is_russian_strict(sent):
            if not avoid_skeletons:
                return sent
            sk = phrase_skeleton(sent, city_ru, desc)
            if sk and sk not in avoid_skeletons:
                return sent
            
    templates = [
        f"{locals_term.capitalize()} сегодня {desc.lower()} — {hint}.",
        f"На улице {desc.lower()}, так что {hint}.",
        f"Погода шепчет: {desc.lower()}, а это значит — {hint}.",
    ]
    return random.choice(templates)

# Батч-генерация — передаём демоним и требование не упоминать город
def ai_generate_comments_batch_intel(city_items):
    enriched = []
    for it in city_items:
        it2 = dict(it)
        it2["audience"] = demonym(it["city"])
        enriched.append(it2)

    prompt = (
        "Ты создаёшь набор комментариев о погоде для разных городов.\n\n"
        "ДАННЫЕ В JSON:\n"
        f"{json.dumps(enriched, ensure_ascii=False, indent=2)}\n\n"
        "ТРЕБОВАНИЯ ДЛЯ КАЖДОГО ГОРОДА:\n"
        "- ОДНА фраза 8-16 слов на русском\n"
        "- Дружелюбно, с лёгким юмором\n"
        "- Упомяни погодные условия (перефразируй)\n"
        "- Включи практический совет из hint\n"
        "- Ориентируйся на audience (не упоминай названия городов)\n"
        "- Без эмодзи, без приветствий\n"
        "- Каждая фраза должна быть уникальной по структуре\n\n"
        "ФОРМАТ ОТВЕТА - строго JSON:\n"
        '{"items": [{"city": "Название города", "comment": "сгенерированная фраза"}]}\n\n'
        "ПРИМЕР:\n"
        '{"items": [\n  {"city": "Леуварден", "comment": "Сегодня прохладно и облачно — идеальная погода для тёплого свитера и неторопливой прогулки"},\n  {"city": "Одесса", "comment": "Солнечно и тепло — отличный повод надеть что-то лёгкое и насладиться днём"}\n]}'
    )
    
    raw = _intel_chat(prompt, max_tokens=600, temperature=0.9)
    data = None
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = None
    if data is None:
        try:
            data = json.loads(raw)
        except Exception:
            data = {"items": []}

    out = {}
    used_skel = set()
    for item in data.get("items", []):
        city = normalize((item.get("city") or ""))
        cm = sanitize_comment(item.get("comment") or "")
        if city and cm and is_russian_strict(cm):
            out[city] = cm

    # Дедупликация и перегенерация
    for it in city_items:
        city = it["city"]
        desc = it["desc"]
        hint = it["hint"]
        cm = out.get(city, "")
        sk = phrase_skeleton(cm, city, desc) if cm else ""
        if not cm or not sk or sk in used_skel:
            cm = ai_generate_comment_intel(city, it["temp"], desc, hint, avoid_skeletons=used_skel)
            sk = phrase_skeleton(cm, city, desc)
        out[city] = cm
        if sk:
            used_skel.add(sk)
    return out


# Пожелание: 180–240 символов
def ai_generate_wish_240_intel() -> str:
    tz = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
    now_local = datetime.now(tz)
    date_human = human_ru_date(now_local)
    weekday = WEEKDAY_RU[now_local.weekday()]
    
    # Создаем более короткий и четкий промпт
    prompt = (
        "Создай одно короткое пожелание на день для семейного чата.\n\n"
        "ТРЕБОВАНИЯ:\n"
        "1. Одно основное пожелание (120-180 символов)\n"
        "2. Одна забавная примета\n"
        "3. Структура: пожелание + пустая строка + <blockquote>Примета на [дата]: [текст]</blockquote>\n\n"
        "СТИЛЬ:\n"
        "- Тёплый, дружеский тон\n"
        "- Простой разговорный язык\n"
        "- Без пафоса и штампов\n"
        "- Максимум 1 эмодзи\n"
        "- Акцент на простых радостях\n\n"
        "ТЕКУЩИЙ КОНТЕКСТ:\n- День недели: {weekday}\n- Дата: {date_human}\n\n"
        "ПРИМЕР ХОРОШЕГО ФОРМАТА:\n"
        "Пусть этот день будет наполнен маленькими приятностями — ароматным кофе, "
        "тёплыми улыбками и душевными разговорами ☕\n\n"
        "<blockquote>Примета на 21 октября: если утром нашёл парные носки с первой "
        "попытки — день будет особенно удачным</blockquote>"
    )

    for _ in range(2):  # Уменьшаем количество попыток
        raw = _intel_chat(prompt, max_tokens=300, temperature=0.7)  # Уменьшаем temperature
        wish = sanitize_wish(raw)
        if wish and 180 <= len(wish) <= 400 and wish.count("<blockquote>") == 1:
            return wish

    # Улучшенный фоллбэк с гарантированной структурой
    return (
        f"Пусть {weekday} подарит моменты радости и тепла — в мелочах прячется "
        f"самое важное ✨\n\n"
        f"<blockquote>Примета на {date_human}: {get_fresh_omen(omen_themes, USED_OMENS)}"
        f"</blockquote>"
    )



# Отрисовка блока погоды: эмодзи после температуры, пустая строка между пунктами, без двоеточия
def build_weather_block(weather_info, comments_by_city):
    lines = ["<b>Погода сегодня:</b>", ""]
    used_skel = set()
    for city_ru, _ in CITIES:
        wi = weather_info.get(city_ru)
        if not wi:
            lines.append(f"• {city_ru} — данные недоступны")
            lines.append("")  # визуальный отступ
            continue
        comment = comments_by_city.get(city_ru) or ai_generate_comment_intel(
            city_ru, wi["temp"], wi["desc"], wi["hint"], avoid_skeletons=used_skel
        )
        tries = 0
        while (not is_russian_strict(comment) or phrase_skeleton(comment, city_ru, wi["desc"]) in used_skel) and tries < 2:
            comment = ai_generate_comment_intel(city_ru, wi["temp"], wi["desc"], wi["hint"], avoid_skeletons=used_skel)
            tries += 1
        sk = phrase_skeleton(comment, city_ru, wi["desc"])
        if sk:
            used_skel.add(sk)
        lines.append(f"• {city_ru} {wi['temp']}°C {wi['emoji']} — {comment}")
        lines.append("")  # визуальный отступ между пунктами
    return "\n".join(lines)


# === Хэндлер триггера "Болтун" ===
async def on_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Логируем входящее сообщение
        user = update.effective_user
        chat = update.effective_chat
        message_text = update.message.text if update.message else None
        chat_id = str(update.effective_chat.id)
        
        logging.info(f"📨 Получено сообщение от {user.first_name} в чате {chat.title if chat.type == 'group' else 'private'}: {message_text}")
        
        if not message_text:
            return
            
        # Используем функцию should_respond для проверки всех триггеров и контекста
        if should_respond(update):
            logging.info(f"⚡ Обнаружен триггер или продолжение диалога: {message_text}")
            
            # Отправляем действие "печатает"
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action="typing"
            )
            
            # Получаем ответ от chat_handler
            handler = get_chat_handler()
            if handler:
                response = await handler.generate_contextual_response(update, context)
                
                if response:
                    # Отправляем ответ
                    sent_message = await update.message.reply_text(
                        response,
                        reply_to_message_id=update.message.message_id
                    )

                    # Добавляем ответ бота в историю
                    chat_history_manager.add_message(
                        from_user="Болтун",  # Имя бота
                        from_id="bot",
                        text=response
                    )
                    
                    # Обновляем контекст: если бот задал вопрос, отмечаем это
                    if any(marker in response for marker in ['?', 'расскажи', 'скажи', 'как', 'что', 'почему']):
                        await handler.update_conversation_context(chat_id, response)
                    else:
                        # Если это не вопрос, завершаем диалог
                        handler.end_conversation(chat_id)
                    
                    logging.info(f"✅ Ответ отправлен: {response[:50]}...")
                else:
                    logging.warning("❌ Chat handler вернул пустой ответ")
                    handler.end_conversation(chat_id)
            else:
                logging.error("❌ Chat handler не инициализирован")
                await update.message.reply_text("Извините, я временно недоступен 🛠️")
        else:
            logging.info("🚫 Триггер не найден и диалог не продолжается")
            
    except Exception as e:
        logging.error(f"❌ Ошибка в обработчике on_trigger: {e}")
        try:
            handler = get_chat_handler()
            if handler:
                handler.end_conversation(str(update.effective_chat.id))
            await update.message.reply_text("Произошла ошибка при обработке сообщения 🛠️")
        except:
            pass

# === Хэндлер "что сегодня?" ===
async def on_whats_today(update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logging.info("🔄 Обработка запроса 'что сегодня?'")
        await send_digest(context, update.effective_chat.id)
    except Exception as e:
        logging.error(f"❌ Ошибка в обработчике on_whats_today: {e}")

# === Отладочный хэндлер ===
async def debug_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Логирует все входящие сообщения для отладки"""
    try:
        message_text = getattr(update, 'message', None) and update.message.text
        user = update.effective_user
        chat = update.effective_chat
        
        if message_text:
            logging.info(f"🔍 DEBUG: Сообщение от {user.first_name if user else 'N/A'} в {chat.title if chat.type == 'group' else 'private'}: {message_text}")
        else:
            logging.info(f"🔍 DEBUG: Сообщение без текста от {user.first_name if user else 'N/A'}")
    except Exception as e:
        logging.error(f"❌ Ошибка в debug_log: {e}")

# === Планировщик ===
async def on_startup(app: Application):
    try:
        if app.job_queue is None:
            logging.error('❌ JobQueue не доступен')
            return
            
        tz = ZoneInfo(TZ_NAME)
        logging.info("⏰ Инициализация планировщика...")

        # Ежедневное расписание
        app.job_queue.run_daily(
            lambda context: send_digest(context, CHAT_ID),
            time=time(hour=8, minute=1, tzinfo=tz),
            name="morning_digest"
        )
        logging.info(f"✅ Ежедневная задача запланирована на 08:01 {TZ_NAME}")
        
    except Exception as e:
        logging.error(f"❌ Ошибка в on_startup: {e}")

# 3) Универсальный рендер дайджеста (вынесено из send_morning)
async def send_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int):
    """
    Переписанный send_digest использует build_morning_digest() для формирования текста.
    Отправляет текст (и фото по погоде, если доступно).
    """
    try:
        msg_text, weather_map = build_morning_digest()  
        
        import math
        from datetime import timedelta
        
        # ...existing code...
        
        # ========== Новая реализация формирования утреннего дайджеста ==========
        # Функция build_morning_digest() собирает:
        # - прогноз min→max + emoji для каждого города из CITIES (использует OpenWeather One Call 3.0)
        # - государственные праздники NL/PL/UA (Calendarific если есть ключ)
        # - дни рождения из локального файла Birthdays.json
        # - «Сегодня в мире» — короткие одно‑предложные научно/технологические новости из RSS
        # Возвращает (text: str, weather_map: dict)
        #
        # Примечание: функция работает устойчиво при ошибках API и пропускает блоки при отсутствии данных.
        
        def _get_coords_openweather(city_name_en: str) -> tuple[float, float] | None:
            """Geocoding через OpenWeather (direct geocoding). Возвращает (lat, lon) или None."""
            try:
                if not OPENWEATHER_API_KEY:
                    return None
                url = "http://api.openweathermap.org/geo/1.0/direct"
                r = requests.get(url, params={"q": city_name_en, "limit": 1, "appid": OPENWEATHER_API_KEY}, timeout=8)
                if r.status_code != 200:
                    return None
                data = r.json()
                if not data:
                    return None
                return float(data[0]["lat"]), float(data[0]["lon"])
            except Exception:
                return None
        
        def _get_onecall_daily(lat: float, lon: float) -> dict | None:
            """
            One Call 3.0: возвращает daily[0] с min/max и weather[0].
            Формат возврата: {"min": int, "max": int, "desc": str, "main": str}
            """
            try:
                if not OPENWEATHER_API_KEY:
                    return None
                url = "https://api.openweathermap.org/data/3.0/onecall"
                params = {
                    "lat": lat,
                    "lon": lon,
                    "exclude": "minutely,hourly,alerts",
                    "units": "metric",
                    "lang": "ru",
                    "appid": OPENWEATHER_API_KEY,
                }
                r = requests.get(url, params=params, timeout=10)
                if r.status_code != 200:
                    return None
                data = r.json()
                daily = data.get("daily")
                if not daily or not isinstance(daily, list):
                    return None
                today = daily[0]
                t = today.get("temp", {})
                weather = (today.get("weather") or [{}])[0]
                min_t = t.get("min")
                max_t = t.get("max")
                if min_t is None or max_t is None:
                    return None
                return {
                    "min": int(round(min_t)),
                    "max": int(round(max_t)),
                    "desc": weather.get("description", ""),
                    "main": weather.get("main", "")
                }
            except Exception:
                return None
        
        def build_weather_map() -> dict:
            """
            Возвращает структуру:
            {
              "Leeuwarden": {"min": 3, "max": 6, "icon": "🌥"},
              "Odesa": {"min": 7, "max": 13, "icon": "☀️"},
              ...
            }
            Использует CITIES (список (ru, en)).
            Пропускает города с ошибкой получения.
            """
            out = {}
            for city_ru, city_en in CITIES:
                try:
                    coords = _get_coords_openweather(city_en)
                    if not coords:
                        continue
                    lat, lon = coords
                    daily = _get_onecall_daily(lat, lon)
                    if not daily:
                        continue
                    icon = weather_emoji(daily.get("desc", ""), daily.get("main", ""))
                    # Ключ в карте — английское название как в примере (но в выводе используем русское имя)
                    out[city_ru] = {"min": daily["min"], "max": daily["max"], "icon": icon, "desc": daily.get("desc", "")}
                except Exception:
                    continue
            return out
        
        # --- Holidays (Calendarific) ---
        def _get_state_holidays_calendarific(target_date: date) -> list[dict]:
            """
            Возвращает список государственных праздников для NL, PL, UA на target_date.
            Каждый элемент: {"country": "NL", "name": "Название праздника"}
            Если CALENDARIFIC_API_KEY отсутствует или ошибка — возвращает [].
            """
            results = []
            if not CALENDARIFIC_API_KEY:
                return results
            countries = {"NL": "Netherlands", "PL": "Poland", "UA": "Ukraine"}
            for iso in countries.keys():
                try:
                    url = "https://calendarific.com/api/v2/holidays"
                    params = {
                        "api_key": CALENDARIFIC_API_KEY,
                        "country": iso,
                        "year": target_date.year,
                        "month": target_date.month,
                        "day": target_date.day,
                    }
                    r = requests.get(url, params=params, timeout=8)
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    holidays = data.get("response", {}).get("holidays", []) or []
                    for h in holidays:
                        types = h.get("type", []) or []
                        # типы содержат строки типа "National holiday"
                        if any("National" in t or "Public" in t or "national" in t.lower() for t in types):
                            results.append({"country": iso, "name": h.get("name", "")})
                except Exception:
                    continue
            return results
        
        # --- Birthdays (из локального Birthdays.json) ---
        def _load_birthdays_from_file(path: str = "Birthdays.json") -> list[dict]:
            """
            Ожидается список объектов с полями:
            - name
            - date (формат yyyy-mm-dd, dd.mm.yyyy, dd-mm или mm-dd etc.)
            Возвращает список словарей.
            """
            try:
                p = os.path.join(os.path.dirname(__file__), path)
                with open(p, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    if isinstance(data, dict):
                        # если файл содержит ключ "birthdays"
                        data = data.get("birthdays") or data.get("list") or list(data.values())
                    if not isinstance(data, list):
                        return []
                    return data
            except Exception:
                return []
        
        def _birthdays_for_date(target_date: date) -> list[dict]:
            out = []
            items = _load_birthdays_from_file()
            for it in items:
                name = it.get("name") or it.get("fullname") or it.get("Имя")
                dt_raw = it.get("date") or it.get("dob") or it.get("birthday")
                if not name or not dt_raw:
                    continue
                dt_raw = str(dt_raw).strip()
                parsed = None
                for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m", "%d-%m-%Y", "%d-%m", "%m-%d-%Y", "%m-%d"):
                    try:
                        tmp = datetime.strptime(dt_raw, fmt)
                        # Если год отсутствует, parsed.year будет 1900 — заменим на текущ год
                        year = tmp.year if fmt.count("%Y") else target_date.year
                        parsed = date(year, tmp.month, tmp.day)
                        break
                    except Exception:
                        continue
                if not parsed:
                    # Попробуем найти число и месяц через regex
                    m = re.search(r"(\d{1,2})\D+(\d{1,2})", dt_raw)
                    if m:
                        try:
                            d = int(m.group(1)); mo = int(m.group(2))
                            parsed = date(target_date.year, mo, d)
                        except Exception:
                            parsed = None
                if not parsed:
                    continue
                if parsed.month == target_date.month and parsed.day == target_date.day:
                    out.append({"name": name})
            return out
        
        # --- Новости из RSS (NASA, ESA, Nature и др.) ---
        RSS_SOURCES = [
            "https://www.nasa.gov/rss/dyn/breaking_news.rss",
            "https://www.nature.com/nature/articles?type=research&format=rss",
            "https://www.sciencedaily.com/rss/top/science.xml",
            "https://www.esa.int/rssfeed/en/news",
            "https://www.technologyreview.com/feed/"  # MIT Technology Review
        ]
        
        NEWS_EXCLUDE_KEYWORDS = [
            "war", "president", "election", "politic", "politics", "military", "soldier", "army",
            "attack", "russia", "ukraine", "conflict", "sanction", "strike", "bomb", "missile"
        ]
        
        def _is_news_ok(title: str) -> bool:
            low = title.lower()
            for kw in NEWS_EXCLUDE_KEYWORDS:
                if kw in low:
                    return False
            # также отвергаем общие политические слова на русском/англ
            for kw in ["власть", "правительство", "война", "армия", "удар", "санкции", "политика", "политический"]:
                if kw in low:
                    return False
            return True
        
        def _first_sentence(text: str) -> str:
            # Обрезаем до первой точки/воскл/вопроса, чтобы получить короткое предложение.
            if not text:
                return ""
            text = re.sub(r"\s+", " ", text).strip()
            for sep in [". ", "! ", "? ", "…"]:
                if sep in text:
                    return (text.split(sep, 1)[0] + sep.strip()).strip()
            # если нет разделителей — возвращаем весь текст, но укоротим до 140 символов
            if len(text) > 140:
                cut = text[:140]
                if " " in cut:
                    cut = cut[:cut.rfind(" ")] + "…"
                return cut
            return text
        
        def _collect_world_news(limit: int = 5) -> list[str]:
            collected = []
            seen = set()
            for src in RSS_SOURCES:
                if len(collected) >= limit:
                    break
                try:
                    r = requests.get(src, timeout=8)
                    if r.status_code != 200:
                        continue
                    root = ET.fromstring(r.content)
                    # RSS может иметь channel->item or feed->entry
                    items = root.findall(".//item")
                    if not items:
                        # Atom feed
                        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
                    for it in items:
                        if len(collected) >= limit:
                            break
                        title_el = it.find("title") or it.find("{http://www.w3.org/2005/Atom}title")
                        if title_el is None or not (title_el.text):
                            continue
                        title = title_el.text.strip()
                        if not _is_news_ok(title):
                            continue
                        sent = _first_sentence(title)
                        # избегаем дубликатов по тексту
                        if sent in seen:
                            continue
                        seen.add(sent)
                        collected.append(sent if sent.endswith((".", "!", "?")) else sent)
                        if len(collected) >= limit:
                            break
                except Exception:
                    continue
            return collected
        
        # --- Основная функция сборки текста ---
        def build_morning_digest(target_date: date | None = None) -> tuple[str, dict]:
            """
            Формирует финальный текст строго в формате, описанном в задаче.
            Возвращает (text, weather_map).
            """
            try:
                if target_date is None:
                    tz = ZoneInfo(TZ_NAME)
                    target_date = datetime.now(tz).date()
            except Exception:
                target_date = datetime.now().date()
        
            parts = []
            # 1. Greeting
            parts.append("🌅 Доброе утро!")
            parts.append("")  # пустая строка
        
            # 2. Погода
            weather_map = build_weather_map()
            if weather_map:
                parts.append("📍 Погода:")
                for city_ru, _ in CITIES:
                    wi = weather_map.get(city_ru)
                    if not wi:
                        # Пропускаем город, если нет данных
                        continue
                    parts.append(f"• {city_ru}: {wi['min']}° → {wi['max']}° {wi['icon']}")
                parts.append("")  # пустая строка после погоды
        
            # 3. Праздники (только государственные NL/PL/UA)
            holidays = _get_state_holidays_calendarific(target_date)
            # оставляем только уникальные страны NL/PL/UA и только если есть хотя бы один праздник
            if holidays:
                # Оставляем только события для заданных стран и форматируем как "🎉 Государственный праздник." или несколько строк?
                # По требованию: показывать только государственные праздники Нидерландов, Польши или Украины.
                # Выведем заголовок и перечислим имена праздников (если есть) в одной строке.
                # Если есть хотя бы один, показываем блок.
                # Пример в задаче: "🎉 Государственный праздник."
                # Сделаем: если есть >=1 — покажем простой заголовок + перечисление названий (коротко).
                # Берём только первые 3 уникальных названий.
                names = []
                for h in holidays:
                    n = (h.get("name") or "").strip()
                    if n and n not in names:
                        names.append(n)
                if names:
                    # Если больше одного — добавим краткое перечисление в следующей строке
                    parts.append("🎉 Государственный праздник.")
                    if len(names) <= 3:
                        parts.append("• " + "; ".join(names))
                    else:
                        parts.append("• " + "; ".join(names[:3]) + "…")
                    parts.append("")
        
            # 4. Дни рождения
            bds = _birthdays_for_date(target_date)
            if bds:
                parts.append("🎂 Дни рождения:")
                for bd in bds:
                    name = bd.get("name")
                    # короткое дружелюбное поздравление
                    parts.append(f"• {name} — С днём рождения, {name}! Пусть будет замечательный день.")
                parts.append("")
        
            # 5. Сегодня в мире (RSS-сборка)
            news = _collect_world_news(limit=5)
            if news:
                parts.append("🌍 Сегодня в мире:")
                for n in news:
                    parts.append(f"— {n}")
                parts.append("")
        
            # 6. Заключение
            parts.append("✨ Отличного дня! Пусть он начнётся легко.")
        
            text = "\n".join(p for p in parts if p is not None)
            return text, weather_map
        
        # ...existing code...
        
        # Заменяем использование старой send_digest на вызов build_morning_digest внутри send_digest
        async def send_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int):
            """
            Переписанный send_digest использует build_morning_digest() для формирования текста.
            Отправляет текст (и фото по погоде, если доступно).
            """
            try:
                msg_text, weather_map = build_morning_digest()
                # Подбор фото: используем первую доступную погоду для поиска фото
                photo_desc = None
                for city_ru, info in weather_map.items():
                    if info and info.get("desc"):
                        photo_desc = info["desc"]
                        break
                photo_url = get_photo_for_weather(photo_desc or "") or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"
        
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_url,
                    caption=msg_text,
                    parse_mode=ParseMode.HTML
                )
                logging.info("✅ Утренний дайджест отправлен (новая схема).")
            except Exception as e:
                logging.error(f"❌ Ошибка при отправке нового дайджеста: {e}")
        
        # ...existing code...()
        # Подбор фото: используем первую доступную погоду для поиска фото
        photo_desc = None
        for city_ru, info in weather_map.items():
            if info and info.get("desc"):
                photo_desc = info["desc"]
                break
        photo_url = get_photo_for_weather(photo_desc or "") or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"

        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=msg_text,
            parse_mode=ParseMode.HTML
        )
        logging.info("✅ Утренний дайджест отправлен (новая схема).")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке нового дайджеста: {e}")

# --- основная отправка
async def send_morning(context: ContextTypes.DEFAULT_TYPE, custom_holidays_for_date, load_custom_holidays):
    tz = ZoneInfo(TZ_NAME)
    now_local = datetime.now(tz)            # было: today_local = datetime.now(tz).date()
    today_local = now_local.date()

    logging.info("=== DEBUG: Testing custom holidays ===")
    test_date = datetime.now().date()
    custom_hols = load_custom_holidays()
    logging.info(f"Loaded {len(custom_hols)} custom holidays total")
    today_custom = custom_holidays_for_date(test_date, custom_hols)
    logging.info(f"Custom holidays for today: {len(today_custom)}")
    for h in today_custom:
        logging.info(f"  - {h['name']} ({h['country']})")

    weather_info = {}
    photo_desc_for_cover = "nature landscape"
    for city_ru, city_en in CITIES:
        wi = get_weather(city_en)
        weather_info[city_ru] = wi
        if wi and city_ru == "Леуварден":
            photo_desc_for_cover = wi["desc"]

    # Приветствие — передаём now_local с часами
    first_city = CITIES[0][0] if CITIES else "ваш город"
    first_wi = weather_info.get(first_city)
    morning_greeting = ai_generate_greeting_intel(now_local, first_city, first_wi or {})
    header = f"🌞 <b>{morning_greeting}</b>"

    # Заголовок теперь динамический
    header = f"🌞 <b>{morning_greeting}</b>"
    
    # подготавливаем батч для ИИ (с подсказками)
    items = []
    for c_ru, _ in CITIES:
        wi = weather_info.get(c_ru)
        if wi:
            items.append({"city": c_ru, "temp": wi["temp"], "desc": wi["desc"], "hint": wi["hint"]})

    comments_by_city = ai_generate_comments_batch_intel(items) if items else {}
    weather_block = build_weather_block(weather_info, comments_by_city)

    # Праздники top-3 и дни рождения
    birthdays = birthdays_for_date(today_local, load_birthdays())
    
    holidays_block = build_holidays_section(today_local, _intel_chat, birthdays)

    

    wish = ai_generate_wish_240_intel()

    logging.info("Loading birthdays...")
    birthdays_data = load_birthdays()
    logging.info(f"Total birthdays in database: {len(birthdays_data)}")
    
    birthdays = birthdays_for_date(today_local, birthdays_data)
    logging.info(f"Birthdays today: {len(birthdays)}")
    
    for bd in birthdays:
        logging.info(f"Today's birthday: {bd['name']} (age: {bd.get('age', 'N/A')})")
    
    holidays_block = build_holidays_section(today_local, _intel_chat, birthdays)

    caption = "\n\n".join([header, weather_block, holidays_block, wish])
    caption = strip_unsupported_html(caption)  
    photo_url = get_photo_for_weather(photo_desc_for_cover) or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"
    await context.bot.send_photo(chat_id=CHAT_ID, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)
    pass   
    



async def reset_conversation_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает контекст диалога"""
    chat_id = str(update.effective_chat.id)
    handler = get_chat_handler()
    if handler:
        handler.end_conversation(chat_id)
        await update.message.reply_text("✅ Начинаем новый диалог! Что хочешь обсудить?")

# --- Точка входа ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()

 # Получаем обработчик чата
    chat_handler = get_chat_handler()  # Используем эту переменную
    
    # "что сегодня?"
    application.add_handler(MessageHandler(filters.Regex(r"(?i)\bчто\s+сегодня\??\b"), on_whats_today))


    # Обработчик фото
    application.add_handler(MessageHandler(filters.PHOTO, chat_handler_instance.handle_photo_message))

    # Команда для просмотра памяти
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(память|что помнишь|memory)"), 
        chat_handler_instance.show_memory_command
    ))

    # Команда для просмотра истории - ДОБАВЬТЕ ЭТО
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(история|history|историю)"), 
        chat_handler_instance.show_history_command
    ))

    # Команда для экспорта истории - ДОБАВЬТЕ ЭТО
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(экспорт|export|скачать историю)"), 
        chat_handler_instance.export_history_command
    ))

    # В main() добавьте этот обработчик:
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(новый диалог|сброс|забудь|start over)"), 
        reset_conversation_context
    ))



    # триггер "Болтун" - исправленная строка
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_trigger))
    
    # отладка — последней (async handler чтобы не возвращать None)
    async def _debug_log_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            text = getattr(update, "message", None) and update.message.text
            logging.info(f"DEBUG: {text}")
        except Exception as e:
            logging.error(f"Ошибка в _debug_log_cb: {e}")

    application.add_handler(MessageHandler(filters.ALL, _debug_log_cb))



    application.post_init = on_startup
    application.run_polling(close_loop=False)



if __name__ == "__main__":
    # Запускаем Flask сервер для health checks в отдельном потоке
    

    # Запускаем Telegram бота
    main()

