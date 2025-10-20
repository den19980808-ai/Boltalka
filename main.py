import os
import re
import json
import random
import requests
import logging
from datetime import datetime, time, date
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from holidays import build_holidays_section, load_birthdays, birthdays_for_date

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
    lines = [normalize(x) for x in (text or "").splitlines() if normalize(x)]
    for ln in lines:
        if not is_russian_strict(ln):
            continue
        if ln.startswith(("Привет", "Здравствуйте", "Доброе утро", "Добрый день")):
            continue
        ln = ln.replace("#", "")
        if len(ln) > 200:
            cut = ln[:200]
            for sep in [". ", " — ", "! ", "? "]:
                if sep in cut:
                    cut = cut[:cut.rfind(sep)+1]
                    break
            ln = cut
        if len(ln) < 140:
            if len(ln) < 130:
                ln += " 🙂"
        return ln
    return ""

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

def ai_generate_greeting_intel(now_local, first_city: str, wi: dict | None) -> str:
    # Приветствие для семьи — без упоминаний городов/стран
    tod_label, greet_phrase = time_of_day(now_local)
    weekday = WEEKDAY_RU[now_local.weekday()]
    style = random.choice(["энергичный", "спокойный", "игривый", "ободряющий"])
    prompt = (
        "Сгенерируй ОДНУ строку приветствия на русском (80–160 символов), "
        "разговорно и с лёгким юмором, 1–2 уместных эмодзи. "
        f"Можно начать с «{greet_phrase}», упомяни «{weekday}» и общий настрой дня. "
        "Не упоминай города и страны, без хэштегов и служебных пояснений. Верни только одну строку."
        f" Стиль: {style}."
    )
    for _ in range(3):
        raw = _intel_chat(prompt, max_tokens=160, temperature=0.85)
        g = sanitize_greeting(raw)
        if g:
            return g
    fb = f"{greet_phrase}! {weekday.capitalize()} пусть идёт легко: планы — по шагам, улыбка — по умолчанию 😉"
    return sanitize_greeting(fb) or f"{greet_phrase}! Хорошего дня 🙂"


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





# --- Intelligence.io универсальный вызов
def _intel_chat(prompt: str, max_tokens: int = 220, temperature: float = 0.8) -> str:
    if not INTELLIGENCE_API_KEY:
        logging.warning("INTELLIGENCE_API_KEY отсутствует")
        return ""
    headers = {"Authorization": f"Bearer {INTELLIGENCE_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "messages": [{"role": "user", "content": prompt}],
        "model": INTELLIGENCE_MODEL,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        resp = requests.post(INTELLIGENCE_URL, headers=headers, json=payload, timeout=25)
        resp.raise_for_status()
        data = resp.json()
        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
        logging.info("INTEL RAW: %s", content[:300])
        return content
    except Exception as e:
        logging.warning("INTEL error: %s", e)
        return ""

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
    avoid_note = " Избегай повторения конструкции ранее." if avoid_skeletons else ""
    base_prompt = (
        f"Погода: {temp_c}°C, {desc}. Аудитория: {locals_term}. "
        "Сгенерируй ОДНУ фразу на русском (8–16 слов), дружелюбно и с лёгким юмором. "
        "Упомяни погодные условия словами (перефразируй), добавь практический совет (зонт/SPF/слои/туман/ветер). "
        "Не упоминай название города, не используй эмодзи, без приветствий и хэштегов. Только фраза."
        + avoid_note
    )
    for _ in range(3):
        raw = _intel_chat(base_prompt, max_tokens=100, temperature=0.9)
        sent = sanitize_comment(raw)
        if sent and is_russian_strict(sent):
            if not avoid_skeletons:
                return sent
            sk = phrase_skeleton(sent, city_ru, desc)
            if sk and sk not in avoid_skeletons:
                return sent
    # тематический фоллбэк без названия города и без эмодзи
    templates = [
        f"{locals_term.capitalize()} достанется {desc.lower()}; совет — {hint}.",
        f"Сегодня {desc.lower()}; логичный выбор — {hint}.",
        f"На улице {desc.lower()}; комфорт спасают {hint}.",
    ]
    return random.choice(templates)

# Батч-генерация — передаём демоним и требование не упоминать город
def ai_generate_comments_batch_intel(city_items):
    # city_items: [{"city": "Леуварден", "temp": 8, "desc": "...", "hint": "..."}]
    enriched = []
    for it in city_items:
        it2 = dict(it)
        it2["audience"] = demonym(it["city"])
        enriched.append(it2)

    prompt = (
        "Дан JSON со списком городов и погодой.\n"
        "Для каждого запиши ОДНУ фразу (8–16 слов) на русском: дружелюбно, лёгкий юмор,\n"
        "упомяни условия (перефразируй) и дай практический совет (зонт/SPF/слои/туман/ветер).\n"
        "Не упоминай названия городов, ориентируйся на audience (одесситам/нидерландцам/жителям польши/«у вас»).\n"
        "Без эмодзи, без приветствий, без хэштегов. Верни строго JSON: "
        "{\"items\":[{\"city\":\"...\",\"comment\":\"...\"}]}."
    )
    raw = _intel_chat(prompt + "\n\n" + json.dumps(enriched, ensure_ascii=False), max_tokens=420, temperature=0.9)
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

    # точечная перегенерация и дедупликация по «скелету»
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
    prompt = (
        "Напиши короткое пожелание на день (180-240 символов) на русском языке. "
        "Стиль: разговорный, естественный, с лёгким юмором. "
        "Можно использовать 1-2 уместных эмодзи. "
        "Без обращений по имени, без приветствий. "
        "Примеры хорошего тона: 'Пусть день сложится как пазл — все детали на своих местах 😊' "
        "или 'Если встретишь непогоду — улыбнись, она точно растеряется ☔️'"
    )
    for _ in range(3):
        raw = _intel_chat(prompt, max_tokens=180, temperature=0.85)
        wish = sanitize_wish(raw)
        if wish and is_russian_strict(wish) and 160 <= len(wish) <= 240:
            return wish
    # фоллбэк (чуть длиннее прежних)
    fb = (
        "План на сегодня простой и добрый: важное — по шагам, приятное — между делом; "
        "если подует встречный ветер — добавь улыбку и маленькую паузу, она творит чудеса 😉✨"
    )
    return sanitize_wish(fb)

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


# 3) Универсальный рендер дайджеста (вынесено из send_morning)
async def send_digest(context: ContextTypes.DEFAULT_TYPE):
    tz = ZoneInfo(TZ_NAME)
    now_local = datetime.now(tz)
    today_local = now_local.date()

    job_data = getattr(context, "job", None)
    chat_id = (job_data.data if job_data else {}).get("chat_id")
    if not chat_id:
        # на случай прямого вызова без JobQueue — используем дефолт из .env
        chat_id = CHAT_ID

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
    photo_url = get_photo_for_weather(photo_desc_for_cover) or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"
    await context.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=caption, parse_mode=ParseMode.HTML)

# --- основная отправка
async def send_morning(context: ContextTypes.DEFAULT_TYPE, custom_holidays_for_date, load_custom_holidays):
    tz = ZoneInfo(TZ_NAME)
    now_local = datetime.now(tz)            # было: today_local = datetime.now(tz).date()
    today_local = now_local.date()
    chat_id = context.job.data["chat_id"]

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

# 4) Обработчик «что сегодня?»
async def on_whats_today(update, context: ContextTypes.DEFAULT_TYPE):
    # Отвечаем в тот же чат, где спросили
    await send_digest(context, update.effective_chat.id)

# 5) Планировщик и регистрация хендлера
async def on_startup(app: Application):
    if app.job_queue is None:
        raise RuntimeError('Установите поддержку JobQueue: pip install "python-telegram-bot[job-queue]"')
    tz = ZoneInfo(TZ_NAME)
    logging.info("Scheduler init...")


    # ежедневное расписание
    app.job_queue.run_daily(
    send_digest,
    time=time(hour=8, minute=7, tzinfo=tz),
    name="morning_digest",
    data={"chat_id": CHAT_ID}
)
    logging.info("Daily job scheduled at 08:00 %s", TZ_NAME)

   
    

def main():
    application = Application.builder().token(BOT_TOKEN).build()
    # Регистрация обработчика текста «что сегодня?» (регистронезависимо, в любом месте фразы)
    application.add_handler(MessageHandler(filters.Regex(r"(?i)\bчто\s+сегодня\??\b"), on_whats_today))
    application.post_init = on_startup
    application.run_polling(close_loop=False)

from flask import Flask
from threading import Thread

# === Flask "заглушка" для Koyeb ===
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running fine ✅"

def run_flask():
    flask_app.run(host="0.0.0.0", port=8000)


if __name__ == "__main__":
    import asyncio
    from threading import Thread

    # Запускаем Flask в отдельном потоке
    Thread(target=run_flask).start()

    # Запускаем Telegram-бота
    main()








