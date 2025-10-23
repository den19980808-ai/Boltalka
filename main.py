import os
import re
import json
import random
import requests
import logging
import atexit
import base64
from telegram import Update
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from openai import OpenAI

from holidays import build_holidays_section, load_birthdays, birthdays_for_date
from chat_handler import init_chat_handler, get_chat_handler

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


# --- Список стран для «широкого поиска» (ENV)
SCAN_COUNTRIES_ENV = os.getenv(
    "SCAN_COUNTRIES_ENV",
    "US:США,DE:Германия,FR:Франция,ES:Испания,IT:Италия,GB:Великобритания"
)
SCAN_LIMIT = int(os.getenv("SCAN_LIMIT", "2"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

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
        f"- Начни с «{greet_phrase}!»\n"
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
    if any(x in d for x in ["облач", "cloud"]) or m == "clouds":
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

# Инициализация chat_handler
chat_handler_instance = init_chat_handler(_intel_chat)
atexit.register(chat_handler_instance._save_memory) 

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
    
     # Расширенный список тем для примет
    omen_themes = [
        # Кулинарные
        "если утром пересолен суп — день будет пикантным и насыщенным",
        "если кофе пролился на стол — жди неожиданных гостей",
        "если хлеб упал маслом вверх — к финансовой удаче",
        
        # Погодные
        "если дождь начался внезапно — это к неожиданным решениям",
        "если увидишь радугу после дождя — загадай желание, оно сбудется",
        "если утром туман — значит, судьба приготовила сюрпризы",
        
        # Бытовые
        "если часы остановились — пора сделать паузу и перевести дух",
        "если нашёл старую фотографию — вспомни о хорошем моменте",
        "если разбилась посуда — к обновлениям в жизни",
        
        # Природные
        "если птица села на подоконник — жди добрых вестей",
        "если увидишь падающую звезду — загадай самое сокровенное",
        "если кот потягивается — значит, удача уже на пороге",
        
        # Сезонные
        "если лист клёна упал прямо в руки — осень принесёт приятные перемены",
        "еждали первый снег — загадай желание на зиму",
        "если почувствовал запах весны в воздухе — начинай новые проекты",
        
        # Городские
        "если увидел красный кабриолет — это к покупке зелёного шарфика",
        "если все светофоры зелёные — день пройдет гладко",
        "если встретил улыбающегося незнакомца — удача сегодня с тобой",
        
        # Культурные/кулинарные
        "если за окном дождь — пора готовить чашушули",
        "если на улице мороз — самое время для глинтвейна",
        "если солнце светит ярко — приготовь окрошку",
        
        # Абсурдные/юмористические
        "если носки наделись с первого раза — весь день будет удачным",
        "если поймал такси без очереди — значит, вселенная тебя любит",
        "если Wi-Fi ловит лучше обычного — к продуктивному дню"
    ]


    selected_omen = get_fresh_omen(omen_themes, USED_OMENS)

    prompt = (
        "Ты создаёшь тёплое пожелание на день для семейного чата с выдуманной приметой.\n\n"
        "СТРУКТУРА:\n"
        "1. Основное пожелание (180-280 символов)\n"
        "2. Пустая строка\n"
        f"3. Цитата: <blockquote>Примета на {date_human}: [текст приметы]</blockquote>\n\n"
        "ТРЕБОВАНИЯ К ПОЖЕЛАНИЮ:\n"
        "- Тёплый, поддерживающий тон\n"
        "- Разговорный естественный стиль\n"
        "- Лёгкий ненавязчивый юмор\n"
        "- Про мелкие радости и простые моменты\n"
        "- 0-2 уместных эмодзи\n"
        "- Без клише и шаблонных фраз\n\n"
        "ТРЕБОВАНИЯ К ПРИМЕТЕ:\n"
        "- Выдуманная, бытовая, забавная\n"
        "- 6-16 слов\n"
        "- Про обычные вещи: кофе, ключи, погода, домашние дела\n"
        "- Не категоричная, с элементом игры\n"
        "- Лёгкая и запоминающаяся\n\n"
        "ПРИМЕРЫ ХОРОШИХ ПРИМЕТ:\n"
        f"«Если утренний кофе оказался особенно вкусным — весь день сложится удачно»\n"
        f"«Нашёл монету по дороге — жди приятного сюрприза после обеда»\n"
        f"«Услышал пение птиц из окна — день пройдёт под знаком лёгкости»\n\n"
        "Сгенерируй полный текст в требуемом формате:"
    )

    for _ in range(3):
        raw_wish = _intel_chat(prompt, max_tokens=400, temperature=0.85)
        if raw_wish:
            # Очищаем и форматируем пожелание
            wish_text = sanitize_wish(raw_wish)
            
            # Добавляем примету
            full_wish = f"{wish_text}\n\n<blockquote>Примета на {date_human}: {selected_omen}</blockquote>"
            
            # Проверяем длину
            if 220 <= len(full_wish) <= 450:
                return full_wish

    # Улучшенный фоллбэк с случайной приметой
    fallback_omens = [
        f"Если {weekday} начался с улыбки — весь день пройдет на позитивной волне",
        f"Нашёл монету по дороге — жди приятного сюрприза после обеда",
        f"Услышал пение птиц из окна — день пройдёт под знаком лёгкости",
        f"Если утренний кофе оказался особенно вкусным — все планы сложатся удачно"
    ]
    
    fallback_omen = random.choice(fallback_omens)
    fallback_wish = (
        f"Пусть {weekday} порадует маленькими неожиданностями — тёплой чашкой чая, "
        f"добрым словом и моментом тишины среди суеты.\n\n"
        f"<blockquote>Примета на {date_human}: {fallback_omen}</blockquote>"
    )
    return sanitize_wish(fallback_wish)




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
    tz = ZoneInfo(TZ_NAME)
    now_local = datetime.now(tz)
    today_local = now_local.date()

    # Погода
    weather_info = {}
    photo_desc_for_cover = "nature landscape"
    for city_ru, city_en in CITIES:
        wi = get_weather(city_en)
        weather_info[city_ru] = wi
        if wi and city_ru == "Леуварден":
            photo_desc_for_cover = wi["desc"]

    # ИИ‑приветствие
    first_city = CITIES[0][0] if CITIES else "ваш город"
    first_wi = weather_info.get(first_city) or {}
    greeting = ai_generate_greeting_intel(now_local, first_city, first_wi)
    header = f"🌞 <b>{greeting}</b>"

    # Комментарии по городам
    items = [{"city": c_ru, "temp": wi["temp"], "desc": wi["desc"], "hint": wi["hint"]}
             for c_ru, _ in CITIES if (wi := weather_info.get(c_ru))]
    comments_by_city = ai_generate_comments_batch_intel(items) if items else {}
    weather_block = build_weather_block(weather_info, comments_by_city)

    # Праздники (топ‑3) + ДР

    birthdays = birthdays_for_date(today_local, load_birthdays())
    
    holidays_block = build_holidays_section(today_local, _intel_chat, birthdays)   

    # Пожелание
    wish = ai_generate_wish_240_intel()

    caption = "\n\n".join([header, weather_block, holidays_block, wish])
    caption = strip_unsupported_html(caption)  
    caption = caption.replace("\n<blockquote>", "\n\n<blockquote>")
    photo_url = get_photo_for_weather(photo_desc_for_cover) or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"
    await context.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)
    pass

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
    
flask_app = Flask(__name__)


@flask_app.route("/")


def home():
    return "Bot is running fine ✅"

@flask_app.route("/health")
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 8000))
    flask_app.run(host="0.0.0.0", port=port)

      

# === Точка входа ===
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
    
    # отладка — последней
    application.add_handler(
        MessageHandler(
            filters.ALL,
            lambda u, c: logging.info(f"DEBUG: {getattr(u, 'message', None) and u.message.text}")
        )
    )



    application.post_init = on_startup
    application.run_polling(close_loop=False)

async def reset_conversation_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает контекст диалога"""
    chat_id = str(update.effective_chat.id)
    handler = get_chat_handler()
    if handler:
        handler.end_conversation(chat_id)
        await update.message.reply_text("✅ Начинаем новый диалог! Что хочешь обсудить?")

if __name__ == "__main__":
    # Запускаем Flask сервер для health checks в отдельном потоке
    try:
        flask_app = Flask(__name__)

        @flask_app.route("/")
        def home():
            return "Bot is running fine ✅"

        @flask_app.route("/health")
        def health():
            return "OK", 200

        def run_flask():
            port = int(os.environ.get("PORT", 8000))
            flask_app.run(host="0.0.0.0", port=port)

        Thread(target=run_flask, daemon=True).start()
        logging.info("✅ Flask сервер запущен на порту 8000")
    except Exception as e:
        logging.warning(f"⚠️ Не удалось запустить Flask сервер: {e}")

    # Запускаем Telegram бота
    main()










































