import os
import re
import json
import random
import requests
import logging
import atexit
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
from chat_history_manager import ChatHistoryManager

# --- окружение
load_dotenv(dotenv_path="token.env", override=True)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY")
NEWSAPI_API_KEY = os.getenv("NEWSAPI_API_KEY", "cd6e0f14d879486a9dbb6ec85d970178")  # NewsAPI для новостей

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")
_openai_client = None

# Хранилище приветствий за день (чтобы не повторять)
_greetings_today = {}  # {chat_id: True/False}

# Хранилище времени последнего сообщения в диалоге (без явного упоминания имени)
_last_dialog_time = {}  # {chat_id: timestamp of last non-triggered message}

# Хранилище показанных новостей дня (чтобы не повторять)
_shown_news_today = set()  # {article_title}

TZ_NAME = os.getenv("TZ", "Europe/Amsterdam")
CITIES_ENV = os.getenv("CITIES", "Леуварден:Leeuwarden,Одесса:Odesa,Варшава:Warsaw,Малага:Malaga")

# Человечные названия стран для вывода
COUNTRY_NAMES = {"NL": "Нидерланды", "UA": "Украина", "PL": "Польша"}

# Разнообразные секции для пожеланий: приметы, шутки, факты, советы, новости
wish_sections = {
    "приметы": [
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
    ],
    "шутки": [
        "знаешь, кофе — это не просто напиток, это жизненная позиция",
        "понедельник: мой злейший враг. Но я всё равно его побью",
        "я бы сказал, что вчера спал как младенец, но младенцы не просыпаются каждый час",
        "мотивация — это как дезодорант: её нужно применять каждый день",
        "если ты не ошибаешься, то ты просто недостаточно пытался",
        "сегодня хороший день для того, чтобы не делать ничего хорошего",
        "я не прокрастинирую, я просто выбираю между множеством приоритетов",
        "если жизнь дарует тебе лимоны, то почему бы не попросить их с сахаром",
        "мой план на день: выглядеть, как будто я что-то знаю",
        "если ты думаешь, что мал, попробуй снести комара"
    ],
    "факты": [
        "улитка может спать 3 года подряд, и я завидую",
        "медведь может бежать со скоростью до 60 км/ч, а я не могу быстро ходить",
        "кактус может не пить воду несколько лет и выжить (я бы не смог)",
        "пчёлы танцуют, чтобы рассказать друг другу о цветах (это же круто!)",
        "дельфины спят с одним открытым глазом и на 50% мозга, защищая себя",
        "осьминог может менять цвет, отражая своё настроение (я это делаю только иногда)",
        "звёзды, которые мы видим, могут быть уже мёртвы, но свет ещё идёт",
        "твой мозг потребляет 20% энергии тела, хотя весит всего 2% (усердный работник!)",
        "сова может вращать голову на 270°, что удобнее, чем мне смотреть в стороны",
        "размер вселенной настолько велик, что она вероятно содержит копию этого чата"
    ],
    "советы": [
        "выпей воды, твой мозг на 75% состоит из воды",
        "сделай один шаг, потом ещё один — это уже прогресс",
        "если не знаешь, что делать, можно просто улыбнуться (работает)",
        "помни: ошибки — это не провалы, это уроки жизни",
        "иногда лучше помолчать, чем сказать что-то невоздержанное",
        "не сравнивай свой путь с путём других, у тебя свой темп",
        "если чувствуешь себя потерянным, вернись к основам и дыши",
        "один день в неделю найди время просто для себя",
        "будь добр к людям, никогда не знаешь, что они переживают",
        "лучше несовершенное действие, чем идеальные планы"
    ],
    "новости": [
        "интересный факт: вчера был день, и он был",
        "развитие ситуации: ты встал и готов к дню",
        "последние новости: ты читаешь это сообщение",
        "срочно: сегодня — хороший день для хороших дел",
        "в развитие темы: каждый момент — это новый шанс",
        "обновление: ты стал на день мудрее, чем вчера",
        "свежая информация: жизнь продолжается, и это здорово",
        "горячие новости: ты уже справился с большей частью дня",
        "сообщение: позитив заразителен, поделись улыбкой",
        "трендовое: быть собой — это в моде"
    ]
}

# Для совместимости, сохраняем старое имя
omen_themes = wish_sections["приметы"]

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

def get_random_wish_section() -> tuple[str, str]:
    """
    Возвращает случайную секцию с её меткой.
    Вернёт: (метка, текст) где метка это "Примета", "Шутка", "Факт" и т.д.
    """
    section_labels = {
        "приметы": "Примета",
        "шутки": "Шутка",
        "факты": "Интересный факт",
        "советы": "Мини-совет",
        "новости": "Новость дня"
    }
    
    # Выбираем случайную категорию
    category = random.choice(list(wish_sections.keys()))
    items = wish_sections[category]
    
    # Выбираем случайный элемент из категории
    selected = random.choice(items)
    label = section_labels[category]
    
    return label, selected

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

# ========== НОВАЯ РЕАЛИЗАЦИЯ: build_morning_digest() ==========
# Модульные функции для сбора погоды, новостей, праздников и ДР
# с надежным обращением к API и graceful fallback'ами

# --- 1. ПОГОДА (Open-Meteo - бесплатный API без ключей) ---
# Координаты городов: Леуварден, Одесса, Варшава, Малага
CITY_COORDS = {
    "Леуварден": (53.2012, 5.7999),
    "Одесса": (46.4825, 30.7233),
    "Варшава": (52.2297, 21.0122),
    "Малага": (36.59, -4.535),
}

def _get_openmeteo_daily(lat: float, lon: float, tz: str = "Europe/Amsterdam") -> dict | None:
    """
    Open-Meteo API: получает прогноз на день (min/max температура).
    Формат возврата: {"min": int, "max": int}
    Никаких ключей API не требуется!
    """
    try:
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min,weather_code",
            "timezone": tz,
            "forecast_days": 1
        }
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        daily = data.get("daily", {})
        
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])
        weather_codes = daily.get("weather_code", [])
        
        if not (temps_max and temps_min):
            return None
        
        min_t = temps_min[0]
        max_t = temps_max[0]
        code = weather_codes[0] if weather_codes else None
        
        if min_t is None or max_t is None:
            return None
        
        return {
            "min": int(round(min_t)),
            "max": int(round(max_t)),
            "code": code
        }
    except Exception:
        return None

def _weather_code_to_emoji(code: int | None) -> str:
    """Преобразует WMO weather code в эмодзи"""
    if code is None:
        return "🌤️"
    
    # WMO Weather codes: https://open-meteo.com/en/docs
    if code == 0:
        return "☀️"  # Clear sky
    elif code == 1 or code == 2:
        return "🌤️"  # Mainly clear, partly cloudy
    elif code == 3:
        return "☁️"  # Overcast
    elif code == 45 or code == 48:
        return "🌫️"  # Foggy
    elif code in (51, 53, 55, 61, 63, 65, 80, 81, 82):
        return "🌧️"  # Drizzle or rain
    elif code in (71, 73, 75, 77, 80, 81, 82, 85, 86):
        return "❄️"  # Snow
    elif code in (80, 81, 82):
        return "🌧️"  # Rain showers
    elif code in (85, 86):
        return "❄️"  # Snow showers
    elif code in (80, 81, 82):
        return "🌧️"  # Rain showers
    elif code >= 80:
        return "⛈️"  # Thunderstorm
    else:
        return "🌤️"

def build_weather_map() -> dict:
    """
    Возвращает структуру погоды для всех городов (Open-Meteo API):
    {
      "Леуварден": {"min": 3, "max": 6, "icon": "🌥"},
      "Одесса": {"min": 7, "max": 13, "icon": "☀️"},
      ...
    }
    Полностью бесплатный, без API ключей!
    """
    out = {}
    
    for city_ru, (lat, lon) in CITY_COORDS.items():
        try:
            daily = _get_openmeteo_daily(lat, lon, TZ_NAME)
            if not daily:
                logging.debug(f"⚠️  Не получены данные Open-Meteo для {city_ru}")
                continue
            
            icon = _weather_code_to_emoji(daily.get("code"))
            out[city_ru] = {
                "min": daily["min"],
                "max": daily["max"],
                "icon": icon
            }
            logging.info(f"✅ Погода для {city_ru}: {daily['min']}°-{daily['max']}° {icon}")
        except Exception as e:
            logging.warning(f"❌ Ошибка получения погоды для {city_ru}: {e}")
            continue
    
    return out

# --- 2. НОВОСТИ из NewsAPI (science + technology + general) ---
NEWSAPI_API_KEY = os.getenv("NEWSAPI_API_KEY", "cd6e0f14d879486a9dbb6ec85d970178")
NEWSAPI_BASE_URL = "https://newsapi.org/v2/top-headlines"

# Крупные авторитетные источники новостей (для фильтра популярности)
TRUSTED_SOURCES = {
    "bbc", "cnn", "reuters", "associated press", "ap", "reuters", "bloomberg", 
    "the guardian", "new york times", "nyt", "washington post", "the times",
    "the telegraph", "financial times", "ft", "the economist", "nature", "science",
    "sciencedaily", "phys.org", "techcrunch", "wired", "theverge", "axios",
    "cnbc", "bbc news", "aljazeera", "dw", "france24", "rt.com"
}

NEWS_EXCLUDE_KEYWORDS = [
    # === ПОЛИТИКА И КОНФЛИКТЫ ===
    "war", "president", "election", "politic", "politics", "military", "soldier", "army",
    "attack", "russia", "ukraine", "conflict", "sanction", "strike", "bomb", "missile",
    "война", "политик", "политика", "армия", "удар", "санкци", "убийство", "теракт", 
    "насилие", "преступление", "российск", "москв", "кремл", "путин", "территори", "требует",
    "fired", "shot", "killed", "died", "death", "shooting", "shooting", "gunshot",
    "застрелили", "убили", "убита", "погибли", "скончался", "скончалась", "ушел в отставку",
    "палестинцы", "израильск", "газа", "хамас", "хезболла", "израил", "ближний восток",
    
    # === СМЕРТИ И ТРАУР ===
    "actor died", "актер умер", "умер", "скончался", "скончалась", "похороны", "похорон",
    "funeral", "died at", "death of", "passed away", "in memoriam", "tribute to",
    "ушедший из жизни", "вечная память",
    
    # === СКИДКИ И ПОКУПКИ ===
    "discount", "sale", "скидк", "распродаж", "deal", "offer", "coupon", "price drop",
    "black friday", "cyber monday", "скидка", "акция", "предложение", "дешевле",
    "walmart", "amazon prime", "ebay", "aliexpress", "промоакция", "предложени",
    
    # === ЭКОНОМИКА И ФИНАНСЫ (СКУЧНО) ===
    "stock market", "crypto", "bitcoin", "ethereum", "trading", "forex", "investment",
    "акци", "биржа", "валют", "котировк", "растет цена", "падает цена", "доллар",
    "рубль", "евро", "инвестици", "трейд", "трейдер",
    
    # === ИНТЕРНЕТ И REDDIT (ЛОКАЛЬНОЕ) ===
    "reddit", "twitter trend", "tiktok", "nsfw", "meme",
    "реддит", "твиттер тренд", "тикток",
    
    # ===  ОСТАЛЬНОЕ (ПОЛИТИКА, ЛОКАЛЬНОЕ, РАЗВЛЕЧЕНИЯ) ===
    "sex", "porn", "сексу", "секс", "порно", "эротик", "nut november", "nut",
    "сво", "неизвестный", "локальный", "провинциальный",
    "horoscope", "astrology", "гороскоп", "астролог", "zodiac", "знак зодиака",
    "indie film", "indie", "documentary", "низкобюджетный",
    "game", "gaming", "destiny", "fortnite", "call of duty", "esports", "twitch",
    "gameplay", "режим вторжения", "ковбойск", "игра", "игрок", "видеоигр",
    "nfl", "nba", "nhl", "soccer", "football league", "спорт", "матч", "турнир",
]

def _is_news_ok(title: str) -> bool:
    """Проверяет, не политическая ли новость"""
    if not title:
        return False
    low = title.lower()
    for kw in NEWS_EXCLUDE_KEYWORDS:
        if kw in low:
            return False
    return True

def _is_source_trusted(source_name: str) -> bool:
    """Проверяет, является ли источник авторитетным"""
    if not source_name:
        return False
    source_lower = source_name.lower()
    # Пройти по списку доверенных источников
    for trusted in TRUSTED_SOURCES:
        if trusted in source_lower:
            return True
    return False

def _is_news_popular(title: str, description: str = "") -> bool:
    """Проверяет, интересна ли новость широкой аудитории (не нишевая)
    Смотрит на упоминание известных брендов, компаний, явлений
    И проверяет, что это про развитые страны или глобальные события"""
    
    combined = (title + " " + description).lower()
    
    # Сначала проверим популярные ключевые слова - если есть, это скорее всего интересная новость
    popular_keywords = {
        # Tech гиганты
        "apple", "iphone", "ipad", "imac", "macos", "airpods",
        "google", "android", "chrome", "youtube", "gmail",
        "microsoft", "windows", "xbox", "copilot", "surface",
        "meta", "facebook", "instagram", "whatsapp", "threads",
        "amazon", "aws", "alexa",
        "nvidia", "tesla", "openai", "chatgpt", "claude",
        "netflix", "disney", "sony",
        # Развитие технологий
        "ai", "artificial intelligence", "machine learning", "deep learning",
        "quantum", "quantum computing",
        "software", "app", "device", "innovation", "startup",
        "tech", "technology", "digital", "internet",
        # Известные люди мирового уровня (но не политики)
        "elon musk", "mark zuckerberg", "jeff bezos", "bill gates", "steve jobs",
        # События и явления
        "nasa", "spacex", "james webb", "olympic", "world cup", "earthquake", "volcano",
        "hurricane", "flood", "discovery", "breakthrough",
        # Science (работает везде)
        "fusion", "climate", "renewable", "solar", "wind",
        "dark matter", "dark stars", "black hole", "gravity", "physics", "scientist",
        "space", "mars", "telescope", "mission", "astronomer", "астроном",
        "темная материя", "черная дыра", "гравитация", "физик", "ученые", "исследование",
        "квантов", "частиц", "атом", "молекул", "биология", "генетик",
        # Развитые страны и известные места
        "usa", "united states", "america", "europe", "germany", "france", "uk", "netherlands",
        "japan", "south korea",
        "америк", "европ", "германи", "франц", "англи", "япони",
        "un", "eu", "nato",
        "washington", "london", "paris", "berlin", "tokyo", "silicon valley", "amsterdam",
        # Health
        "cancer", "disease", "virus", "pandemic", "medicine", "health", "doctor",
        "hospital", "treatment", "vaccine", "research", "scientists", "study shows",
        "исследован", "учены", "открыт", "найден", "обнаружен"
    }
    
    # Если есть популярное ключевое слово - это интересная новость!
    has_popular = any(kw in combined for kw in popular_keywords)
    if has_popular:
        return True
    
    # Если нет популярных ключевых слов, проверяем на исключения
    exclude_keywords = {
        # === СМЕРТИ, ТРАУР ===
        "died", "death", "died at", "passed away", "умер", "скончался", "ушедший",
        "похоронен", "похороны", "ушел в отставку", "скончалась",
        
        # === ПОЛИТИКА И КОНФЛИКТЫ ===
        "putin", "путин", "territory", "территори", "war", "война", "army", "армия",
        "conflict", "конфликт", "military", "военн", "sanction", "санкци",
        "attack", "атак", "strike", "удар", "требует", "requires", "demands",
        "shot", "fired", "shooting", "застрелил", "палестин", "израил", "газа",
        
        # === СКИДКИ, АКЦИИ, ПОКУПКИ ===
        "discount", "sale", "скидк", "распродаж", "deal", "offer", "coupon",
        "black friday", "cyber monday", "скидка", "акция", "на распродажи",
        "цены упали", "цена упала", "дешевле чем", "скидка на",
        
        # === КАК НАЙТИ / КАК СДЕЛАТЬ (АБСТРАКТНОЕ) ===
        "how to find", "how to get", "how to make", "как найти", "как получить", 
        "как сделать", "способ", "способы", "инструкция", "советы как",
        "потерянн",
        
        # === ЛОКАЛЬНЫЕ СТРАНЫ И СОБЫТИЯ ===
        "ghana", "nigeria", "cameroon", "senegal", "mali", "uganda", "kenya",
        "india", "pakistan", "bangladesh", "australia", "new zealand",
        "africa", "африк", "ганы", "нигери", "индии", "австрали",
        # Локальные спорты и события
        "nrl", "afl", "rugby league", "cricket", "nsw", "sydney", "perth",
        "thunderstorm", "погода в", "вчера в",
        # Игры и развлечения нишевые
        "game", "gaming", "destiny", "fortnite", "esports", "twitch",
        "режим вторжения", "ковбойск", "игра", "видеоигр", "reddit", "nsfw"
    }
    
    # Если попадает под исключение - отфильтровать
    for keyword in exclude_keywords:
        if keyword in combined:
            return False
    
    # Если нет популярных ключевых слов и нет явных исключений - скорее всего нишевая новость
    return False

def _first_sentence(text: str) -> str:
    """Обрезает текст до первого предложения (макс 150 символов)"""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    # Ищем конец первого предложения
    for sep in [". ", "! ", "? ", "…"]:
        if sep in text:
            sent = (text.split(sep, 1)[0] + sep.strip()).strip()
            if len(sent) <= 150:
                return sent
            # Если слишком длинное — ищем последний пробел
            if " " in sent[:150]:
                return sent[:150].rsplit(" ", 1)[0] + "…"
            return sent[:150] + "…"
    
    # Если нет точек — ограничиваем длину
    if len(text) > 150:
        cut = text[:150]
        if " " in cut:
            cut = cut[:cut.rfind(" ")] + "…"
        return cut
    return text

def _collect_digest_news() -> list[dict]:
    """Собирает 3 уникальные популярные новости из разных категорий: 
    science → technology → general
    Берёт самые популярные мировые новости, а не локальные"""
    
    # Категории в нужном порядке (согласно рекомендации: science, technology, general)
    categories = ["science", "technology", "general"]
    news_list = []
    
    for category in categories:
        if len(news_list) >= 3:  # Нужны 3 новости из разных категорий
            break
            
        try:
            params = {
                "category": category,
                "language": "en",
                "sortBy": "popularity",  # Popularity для популярных новостей
                "pageSize": 25,  # Берём много, чтобы отфильтровать и найти популярные
                "apiKey": NEWSAPI_API_KEY
            }
            response = requests.get(NEWSAPI_BASE_URL, params=params, timeout=5)
            if response.status_code != 200:
                logging.debug(f"⚠️ NewsAPI вернул статус {response.status_code} для категории {category}")
                continue
            
            data = response.json()
            articles = data.get("articles", [])
            logging.info(f"📰 Категория '{category}': получено {len(articles)} статей")
            
            # Ищем первую подходящую ПОПУЛЯРНУЮ новость из этой категории
            # Сначала ищем в авторитетных источниках и популярные
            found_in_category = False
            candidates = []  # (article, is_trusted, is_popular, source)
            
            for article in articles:
                title = article.get("title") or ""
                description = article.get("description") or ""
                source = article.get("source", {}).get("name") or "Unknown"
                
                # Пропускаем статьи без заголовка
                if not title or not isinstance(title, str):
                    continue
                
                # Пропускаем, если уже показывали эту новость сегодня
                if title in _shown_news_today:
                    logging.debug(f"🔄 Уже показана сегодня: {title[:50]}")
                    continue
                
                # Проверяем, не табу-новость ли это (война, политика, военное и т.д.)
                if not _is_news_ok(title):
                    logging.debug(f"🚫 Пропущена табу-новость: {title[:50]}")
                    continue
                
                # Пропускаем локальные источники
                source_lower = (source or "").lower()
                if any(x in source_lower for x in ["ghana", "ghanaian", "africa", "africana", "nrl", "afl"]):
                    logging.debug(f"⚠️ Локальный источник: {source}")
                    continue
                
                is_trusted = _is_source_trusted(source)
                is_popular = _is_news_popular(title, description or "")
                
                candidates.append((article, is_trusted, is_popular, source))
            
            # Сортируем: авторитетные + популярные вперёд
            # Для всех категорий требуем популярность
            candidates = [(a, t, p, s) for a, t, p, s in candidates if p]
            candidates.sort(key=lambda x: (x[1], x[2]), reverse=True)
            
            for article, is_trusted, is_popular, source in candidates:
                title = (article.get("title") or "").strip()
                description = (article.get("description") or "").strip()
                
                # Полный текст для анализа GPT (защита от None)
                if title:
                    full_text = f"{title}. {description}".strip() if description else title
                else:
                    continue
                if full_text:
                    news_list.append({
                        "title": title,
                        "full_text": full_text,
                        "category": category,
                        "source": source
                    })
                    _shown_news_today.add(title)
                    found_in_category = True
                    trust_mark = "✓ авторитетный" if is_trusted else "⚠️ локальный"
                    pop_mark = "★ популярная" if is_popular else "○ специализированная"
                    logging.info(f"✅ Нашли в категории '{category}' ({trust_mark} {pop_mark}, {source}): {title[:60]}...")
                    break
            
            if not found_in_category:
                logging.info(f"⚠️ Подходящих новостей не найдено в категории '{category}'")
                
        except Exception as e:
            logging.error(f"❌ Ошибка при загрузке новостей из {category}: {e}")
            continue
    
    logging.info(f"✅ Отобрано {len(news_list)} новостей из разных категорий (всего сегодня: {len(_shown_news_today)})")
    return news_list

def _analyze_news_with_gpt(news_items: list[dict]) -> list[dict]:
    """Анализирует новости с помощью GPT, делает выжимку 80-120 символов на русском с юмором"""
    if not news_items:
        logging.warning("⚠️ Нет новостей для анализа")
        return []
    
    client = _get_openai()
    if not client:
        logging.error("❌ Не удалось инициализировать OpenAI клиент")
        return []
    
    analyzed = []
    for item in news_items:
        try:
            prompt = f"""Проанализируй эту новость и сделай краткую выжимку на РУССКОМ языке (80-120 символов).
Если уместно, добавь мягкий юмор. БЕЗ эмодзи!

Новость: {item['full_text'][:500]}

Ответь ТОЛЬКО выжимкой, без предисловий и объяснений."""

            logging.info(f"🤖 Анализирую новость с GPT: {item['title'][:50]}...")
            
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                max_tokens=150,
                temperature=0.7
            )
            
            summary = response.choices[0].message.content.strip()
            analyzed.append({
                "title": item["title"],
                "summary": summary
            })
            logging.info(f"✅ Выжимка готова: {summary[:80]}...")
            
        except Exception as e:
            logging.error(f"❌ Ошибка при анализе новости GPT: {e}")
            # Fallback: используем первое предложение оригинального текста
            fallback = _first_sentence(item["full_text"])
            if fallback:
                analyzed.append({
                    "title": item["title"],
                    "summary": fallback
                })
                logging.info(f"⚠️ Использован fallback для новости: {fallback[:80]}...")
    
    return analyzed

# --- 3. ДНИ РОЖДЕНИЯ (из локального файла) ---
def _birthdays_for_today(target_date: date | None = None) -> list[dict]:
    """Загружает дни рождения на указанную дату или на сегодня"""
    if target_date is None:
        try:
            tz = ZoneInfo(TZ_NAME)
            target_date = datetime.now(tz).date()
        except Exception:
            target_date = datetime.now().date()
    
    people = load_birthdays()
    return birthdays_for_date(target_date, people)

# --- 4. ОСНОВНАЯ ФУНКЦИЯ ФОРМИРОВАНИЯ ДАЙДЖЕСТА ---
def build_morning_digest(target_date: date | None = None) -> tuple[str, dict]:
    """
    Формирует полный утренний дайджест в новом компактном формате.
    """
    try:
        if target_date is None:
            tz = ZoneInfo(TZ_NAME)
            target_date = datetime.now(tz).date()
    except Exception:
        target_date = datetime.now().date()

    parts = []
    
    # 1. ПРИВЕТСТВИЕ
    parts.append("🌅 Доброе утро!")
    
    # Добавить информацию о Новом годе
    today = target_date
    # Если уже 1 января - это Новый год! Считаем дни до СЛЕДУЮЩЕГО НГ для остальных
    if today.month == 1 and today.day == 1:
        days_left = 0
        parts.append("🎉 С Новым годом вас!")
    else:
        new_year = date(today.year + 1, 1, 1)
        days_left = (new_year - today).days
        
        if days_left == 1:
            # 31 декабря
            parts.append("🎊 Скоро Новый год! Осталось 1 день!")
        elif days_left > 1 and days_left <= 31:
            # С 1 по 30 декабря
            parts.append(f"✨ До Нового года осталось {days_left} дней!")
    
    parts.append("")
    
    # 2. ПОГОДА с сокращенными пробелами
    weather_map = build_weather_map()
    if weather_map:
        parts.append("📌 Погода сегодня:")
        # Берём города из CITY_COORDS (там точно все города, включая Малагу)
        for city_ru in CITY_COORDS.keys():
            wi = weather_map.get(city_ru)
            if not city_ru or not wi:
                continue
            # Компактный формат без лишних пробелов
            city_display = f"• {city_ru:<8}"  # 8 символов для выравнивания (сокращено)
            temp_display = f" — {wi['min']:3}° → {wi['max']:3}° {wi['icon']}"
            parts.append(city_display + temp_display)
        parts.append("")
    
    # 3. РАЗДЕЛИТЕЛЬ
    parts.append("∙∙∙")
    parts.append("")
    
    # 4. НОВОСТИ ДЛЯ ДАЙДЖЕСТА (3 новости с проверкой дублей)
    news_items = _collect_digest_news()
    if news_items:
        # Анализируем новости с GPT: выжимка 80-120 символов на русском с юмором
        analyzed_news = _analyze_news_with_gpt(news_items)
        if analyzed_news:
            parts.append("📰 Новости дня:")
            for item in analyzed_news:
                parts.append(f"• {item['summary']}")
            parts.append("")
    
    # 5. РАЗДЕЛИТЕЛЬ
    parts.append("∙∙∙")
    parts.append("")
    
    # 6. ПОЖЕЛАНИЕ НА ДЕНЬ (основное + blockquote с цитатой)
    wish_text = ai_generate_wish_extended_intel()
    
    parts.append("✨ Хорошего дня!")
    parts.append("")
    
    # Разделяем основное пожелание от цитаты по пустой строке
    if "\n\n" in wish_text:
        main_wish, citation = wish_text.split("\n\n", 1)
        parts.append(main_wish)
        parts.append("")
        # Обернуть цитату в blockquote
        parts.append(f"<blockquote>{citation}</blockquote>")
    else:
        # Fallback если нет разделения
        parts.append(wish_text)
    
    parts.append("")
    
    # 7. ФИНАЛЬНОЕ ЗАКЛЮЧЕНИЕ
    parts.append("Увидимся завтра")
    
    text = "\n".join(p for p in parts if p is not None)
    return text, weather_map


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

    # Список триггеров для реакции (на какие слова реагирует бот)
    triggers = [
        r'\b[Бб]олтун\w*',      # болтун
        r'\b[Вв]ася\w*',        # вася (новое - имя бота)
        r'\b[Вв]асилий\w*',     # василий
        r'\b[Пп]оболта\w+',     # поболтай
        r'\b[Пп]оговори\w+',    # поговори
        r'\b[Ээ]й\s*,?\s*бот',  # эй, бот
        r'\b[Пп]ривет\s*,?\s*бот', # привет, бот
        r'\b[Бб]от\s*,?\s*[Пп]ривет', # бот, привет
        r'\b[Пп]риветствую',    # приветствую
        r'\b[Пп]оздороваться',  # поздороваться
        r'\b[Бб]олт\w*',        # болтовня
    ]

    # Проверяем каждый триггер
    for trigger in triggers:
        if re.search(trigger, text, re.IGNORECASE):
            logging.info(f"⚡ Обнаружен триггер: {trigger}")
            return True

    # *** НОВОЕ: Проверяем окно диалога (10 минут без явного упоминания имени) ***
    if chat_id in _last_dialog_time:
        time_diff = datetime.now(ZoneInfo(TZ_NAME)) - _last_dialog_time[chat_id]
        if time_diff.total_seconds() > 600:  # > 10 минут
            # Окно диалога истекло - требуется явное упоминание имени
            logging.info(f"⏰ Окно диалога истекло (прошло {int(time_diff.total_seconds())} сек > 10 минут)")
            del _last_dialog_time[chat_id]
            # Не отвечаем без явного триггера
            logging.info("🚫 Требуется явное упоминание имени")
            return False
        else:
            # Еще в пределах 10 минут - обновляем временную метку
            logging.info(f"⏳ В пределах окна диалога ({int(time_diff.total_seconds())} сек из 600)")
            _last_dialog_time[chat_id] = datetime.now(ZoneInfo(TZ_NAME))

    # Проверяем, является ли сообщение продолжением диалога
    handler = get_chat_handler()
    if handler and handler.should_continue_conversation(chat_id, text):
        chat_id = str(update.effective_chat.id)
        logging.info("🔄 Обнаружено продолжение диалога по контексту")
        # Устанавливаем время начала/продления диалога
        _last_dialog_time[chat_id] = datetime.now(ZoneInfo(TZ_NAME))
        return True
        
    logging.info("🚫 Триггер не найден")
    return False

def _check_whats_today_request(text: str) -> bool:
    """Проверяет, это ли запрос 'что сегодня'"""
    return bool(re.search(r'\bчто\s+сегодня\??\b', text, re.IGNORECASE))

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


# Пожелание: 100-200 символов с юмором и приметой/шуткой/фактом/советом/новостью
def ai_generate_wish_extended_intel() -> str:
    """
    Генерирует пожелание на день в формате:
    Основное пожелание (30-50 символов)
    
    ПУСТАЯ СТРОКА
    
    Цитата (примета/шутка/факт/совет/новость - без метки)
    """
    tz = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
    now_local = datetime.now(tz)
    weekday = WEEKDAY_RU[now_local.weekday()]
    
    # Получаем случайную секцию
    section_label, section_text = get_random_wish_section()
    
    prompt = (
        "Ты пишешь короткое пожелание на день для семейного чата.\n\n"
        "ФОРМАТ (без кавычек):\n"
        "Основное пожелание (30-50 символов, мотивирующее)\n"
        "ПУСТАЯ СТРОКА\n"
        "Только текст цитаты БЕЗ метки (80-100 символов)\n\n"
        "ТРЕБОВАНИЯ:\n"
        "- Русский язык\n"
        "- Тёплое и позитивное\n"
        "- Максимум 1 эмодзи на основное пожелание\n"
        "- Легкий юмор\n"
        "- Естественный разговорный стиль\n"
        "- БЕЗ КАВЫЧЕК в ответе\n"
        "- БЕЗ слова 'Метка:' перед цитатой\n"
        "- Всего 150-200 символов\n\n"
        f"КОНТЕКСТ: {weekday}\n"
        f"МЕТКА И ТЕКСТ: {section_label}: {section_text}\n\n"
        "ПРИМЕРЫ ПРАВИЛЬНОГО ФОРМАТА:\n"
        "Замечай красивое, смейся искренне ☕\n"
        "\n"
        "если встретишь кота — день будет удачным.\n\n"
        "Пусть удача касается тебя на каждом шагу 💫\n"
        "\n"
        "мотивация — это дезодорант, её нужно применять каждый день.\n\n"
        "Создай ОРИГИНАЛЬНОЕ пожелание в этом формате (БЕЗ кавычек, БЕЗ 'Метка:'):"
    )
    
    for attempt in range(2):
        raw = _intel_chat(prompt, max_tokens=250, temperature=0.8)
        
        if raw and len(raw) > 50:
            text = raw.strip().strip('"').strip("'")  # Удаляем кавычки если есть
            # Проверяем, что есть пустая строка и две части
            if "\n\n" in text and len(text) <= 300:
                return text
    
    # Стабильный компактный фоллбэк без AI
    # Важно: метка НЕ включается в цитату, только текст!
    wishes_templates = [
        "Замечай красивое, смейся искренне 💫\n\n{text}",
        "Пусть удача касается тебя на каждом шагу ☕\n\n{text}",
        "Этот день — твой, наполни его радостью 🌟\n\n{text}",
        "Кофе горячий, улыбка теплая, день удачный 💛\n\n{text}",
        "Живи как будто каждый день — особенный день 🙂\n\n{text}",
    ]
    
    choice = hash(weekday) % len(wishes_templates)
    return wishes_templates[choice].format(text=section_text)


# Старая версия для совместимости


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
        
        logging.info(f"📨 Получено сообщение от {user.first_name if user else 'unknown'}: {message_text}")
        
        if not message_text:
            logging.warning("⚠️  Пустое сообщение")
            return
        
        # *** ПЕРВАЯ ПРОВЕРКА: Специфичные запросы ***
        
        # Запрос "что сегодня?"
        if _check_whats_today_request(message_text):
            logging.info(f"📅 ✅ ТРИГГЕР 'ЧТО СЕГОДНЯ': {message_text}")
            await on_whats_today(update, context)
            return
        
        # *** ВТОРАЯ ПРОВЕРКА: Обычные триггеры и контекст ***
        if should_respond(update):
            logging.info(f"⚡ ✅ ТРИГГЕР НАЙДЕН: {message_text}")
            
            # *** НОВОЕ: Устанавливаем время явного упоминания имени ***
            _last_dialog_time[chat_id] = datetime.now(ZoneInfo(TZ_NAME))
            logging.info(f"🕐 Установлено время явного упоминания имени: {_last_dialog_time[chat_id]}")
            
            # Отправляем действие "печатает"
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action="typing"
            )
            
            # Получаем ответ от chat_handler
            handler = get_chat_handler()
            if handler:
                # *** НОВОЕ: Проверяем, не было ли приветствия сегодня ***
                today = datetime.now(ZoneInfo(TZ_NAME)).date()
                greeting_key = f"{chat_id}_{today.isoformat()}"
                
                suppress_greeting = ""
                if greeting_key in _greetings_today:
                    # Если уже было приветствие сегодня, подавляем его
                    suppress_greeting = "\n[НЕ ДОБАВЛЯЙ ПРИВЕТСТВИЕ! Уже было приветствие сегодня - просто отвечай на вопрос.]"
                    logging.info(f"💬 Приветствие уже было сегодня для {chat_id}")
                else:
                    # Первое сообщение в день - помечаем, что приветствие было
                    _greetings_today[greeting_key] = True
                    logging.info(f"💬 Первое сообщение дня для {chat_id} - разрешаем приветствие")
                
                # Добавляем инструкцию о приветствиях если нужно
                if suppress_greeting:
                    chat_history_manager.add_message(
                        from_user="СИСТЕМА",
                        from_id="system",
                        text=suppress_greeting
                    )
                
                response = await handler.generate_contextual_response(update, context)
                
                if response:
                    # Отправляем ответ
                    await update.message.reply_text(
                        response,
                        reply_to_message_id=update.message.message_id
                    )

                    # Добавляем ответ бота в историю
                    chat_history_manager.add_message(
                        from_user="Болтун",
                        from_id="bot",
                        text=response
                    )
                    
                    # Обновляем контекст: если бот задал вопрос, отмечаем это
                    if any(marker in response for marker in ['?', 'расскажи', 'скажи']):
                        await handler.update_conversation_context(chat_id, response)
                    else:
                        handler.end_conversation(chat_id)
                    
                    logging.info(f"✅ Ответ отправлен: {len(response)} символов")
                else:
                    logging.warning("❌ Chat handler вернул пустой ответ")
                    handler.end_conversation(chat_id)
            else:
                logging.error("❌ Chat handler не инициализирован")
                await update.message.reply_text("Извините, я временно недоступен 🛠️")
        else:
            logging.info(f"🚫 ТРИГГЕР НЕ НАЙДЕН: {message_text}")
            
    except Exception as e:
        logging.error(f"❌ Ошибка в on_trigger: {e}", exc_info=True)
        try:
            handler = get_chat_handler()
            if handler:
                handler.end_conversation(str(update.effective_chat.id))
            await update.message.reply_text("Произошла ошибка 🛠️")
        except Exception as e2:
            logging.error(f"❌ Ошибка в обработке исключения: {e2}")

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

# === НОВАЯ отправка дайджеста (модульная и надежная) ===
async def send_digest(context: ContextTypes.DEFAULT_TYPE, chat_id: str | int):
    """
    Формирует и отправляет утренний дайджест с фото.
    Использует новую модульную функцию build_morning_digest().
    """
    try:
        msg_text, weather_map = build_morning_digest()
        
        # Подбираем фото по первой доступной погоде
        photo_desc = ""
        for info in weather_map.values():
            if info and info.get("desc"):
                photo_desc = info.get("desc")
                break
        
        photo_url = get_photo_for_weather(photo_desc or "") or "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee"
        
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=photo_url,
            caption=msg_text,
            parse_mode=ParseMode.HTML
        )
        logging.info("✅ Утренний дайджест успешно отправлен")
    except Exception as e:
        logging.error(f"❌ Ошибка при отправке дайджеста: {e}")

# === Старая отправка (для совместимости — удалить позже) ===


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


async def reset_conversation_context(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сбрасывает контекст диалога"""
    chat_id = str(update.effective_chat.id)
    handler = get_chat_handler()
    if handler:
        handler.end_conversation(chat_id)
        await update.message.reply_text("✅ Начинаем новый диалог! Что хочешь обсудить?")

# === Точка входа ===
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Получаем обработчик чата
    chat_handler = get_chat_handler()  # Используем эту переменную
    
    # Обработчик фото
    application.add_handler(MessageHandler(filters.PHOTO, chat_handler_instance.handle_photo_message))

    # Команда для просмотра памяти
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(память|что помнишь|memory)"), 
        chat_handler_instance.show_memory_command
    ))

    # Команда для просмотра истории
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(история|history|историю)"), 
        chat_handler_instance.show_history_command
    ))

    # Команда для экспорта истории
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(экспорт|export|скачать историю)"), 
        chat_handler_instance.export_history_command
    ))

    # Команда для сброса контекста диалога
    application.add_handler(MessageHandler(
        filters.Regex(r"(?i)(новый диалог|сброс|забудь|start over)"), 
        reset_conversation_context
    ))

    # *** ГЛАВНЫЙ ОБРАБОТЧИК для всех текстовых сообщений ***
    # Проверяет: новости → "что сегодня" → болтун + контекст
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

