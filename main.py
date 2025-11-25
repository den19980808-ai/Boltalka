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
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")  # GNews API для новостей

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
# Координаты городов: Леуварден, Одесса, Варшава
CITY_COORDS = {
    "Леуварден": (53.2012, 5.7999),
    "Одесса": (46.4825, 30.7233),
    "Варшава": (52.2297, 21.0122),
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

# --- 2. НОВОСТИ из GNews API (science + technology) ---
GNEWS_API_KEY = os.getenv("GNEWS_API_KEY", "c3c1fde56c83d7d50a44c02722661372")
GNEWS_BASE_URL = "https://gnews.io/api/v4/top-headlines"

NEWS_EXCLUDE_KEYWORDS = [
    "war", "president", "election", "politic", "politics", "military", "soldier", "army",
    "attack", "russia", "ukraine", "conflict", "sanction", "strike", "bomb", "missile",
    "война", "политик", "политика", "армия", "удар", "санкци", "убийство", "теракт", 
    "насилие", "преступление"
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

def _collect_gnews_articles(limit: int = 3) -> list[str]:
    """
    Собирает 2-3 новости из GNews API на АНГЛИЙСКОМ (science + technology).
    Избегает новостей про Россию и русских источников.
    Возвращает список заголовков для обработки GPT.
    """
    articles = []
    
    if not GNEWS_API_KEY:
        logging.warning("⚠️  GNEWS_API_KEY не установлен, новости недоступны")
        return articles
    
    # Ищем в двух категориях: science и technology (на английском)
    categories = ["science", "technology"]
    seen_titles = set()
    
    # Ключевые слова, которые указывают на русские новости или Россию
    russia_keywords = [
        "russia", "russian", "moscow", "kremlin", "putin", "russia",
        "россия", "русс", "москв", "путин", "кремл",
        "ussr", "soviet", "baikonur"  # Байконур - исключаем космические новости из России
    ]
    
    for category in categories:
        if len(articles) >= limit:
            break
        
        try:
            params = {
                "category": category,
                "lang": "en",  # На АНГЛИЙСКОМ
                "max": limit * 3,  # Берем больше, чтобы после фильтрации было достаточно
                "apikey": GNEWS_API_KEY
            }
            
            r = requests.get(GNEWS_BASE_URL, params=params, timeout=10)
            if r.status_code != 200:
                logging.warning(f"⚠️  GNews вернул {r.status_code} для категории {category}")
                continue
            
            data = r.json()
            articles_data = data.get("articles", []) or []
            
            logging.info(f"📰 GNews вернул {len(articles_data)} статей для категории {category}")
            
            for article in articles_data:
                if len(articles) >= limit:
                    break
                
                title = (article.get("title") or "").strip()
                description = (article.get("description") or "").strip()
                
                if not title:
                    continue
                
                # ИСКЛЮЧАЕМ новости про Россию и русские источники
                combined = f"{title} {description}".lower()
                if any(kw in combined for kw in russia_keywords):
                    logging.debug(f"⏭️  Пропущена новость про Россию: {title[:50]}")
                    continue
                
                # Проверяем, не политическая ли (общая фильтрация)
                if not _is_news_ok(title):
                    logging.debug(f"⏭️  Пропущена новость (политика): {title[:50]}")
                    continue
                
                # Избегаем дубликатов
                if title in seen_titles:
                    continue
                
                seen_titles.add(title)
                
                # Сохраняем заголовок + описание для GPT
                full_text = f"{title}. {description}" if description else title
                articles.append(full_text)
                logging.info(f"✅ Новость добавлена: {title[:60]}")
        
        except Exception as e:
            logging.warning(f"❌ Ошибка при получении новостей из GNews ({category}): {e}")
            continue
    
    logging.info(f"📊 Собрано {len(articles)} новостей для обработки GPT")
    return articles

def _enhance_news_with_gpt(news_list: list[str]) -> list[str]:
    """
    Принимает список новостей на АНГЛИЙСКОМ и пропускает их через GPT.
    GPT переформатирует в 1-2 коротких предложения на РУССКОМ с юмором.
    Возвращает список кратких русских новостей.
    """
    if not news_list:
        return []
    
    if not OPENAI_MODEL or not _openai_client:
        logging.warning("⚠️  OpenAI не доступен, возвращаем новости без обработки")
        return news_list
    
    enhanced = []
    
    for news in news_list:
        try:
            prompt = (
                "You are a news editor for a family chat. Rewrite this science/tech news:\n\n"
                f'"{news}"\n\n'
                "REQUIREMENTS:\n"
                "- 1-2 short sentences MAXIMUM (keep it brief!)\n"
                "- Write in Russian\n"
                "- Make it interesting and easy to understand\n"
                "- You can add light humor or a funny metaphor\n"
                "- No emojis\n"
                "- Natural conversational style\n"
                "- Skip boring details - only the 'wow' factor\n\n"
                "Good examples:\n"
                "'Ученые создали новый материал, который прочнее стали, но легче пера — "
                "скоро может стать основой будущих самолетов и космических кораблей.'\n"
                "'Телескоп James Webb заметил самую далекую галактику — она была такой молодой, "
                "что вселенной было всего 300 миллионов лет.'\n\n"
                "Return ONLY the rewritten news in Russian, nothing else:"
            )
            
            resp = _intel_chat(prompt, max_tokens=120, temperature=0.8)
            
            if resp:
                # Санитизируем ответ
                resp = resp.strip().strip('"').strip("'")
                if resp and len(resp) > 15:
                    enhanced.append(resp)
                    logging.info(f"✅ Новость обработана GPT: {resp[:70]}")
                else:
                    logging.warning(f"⚠️  GPT вернул пустой или короткий ответ")
            else:
                logging.warning(f"⚠️  GPT не вернул ответ для новости")
        
        except Exception as e:
            logging.warning(f"❌ Ошибка обработки новости GPT: {e}")
    
    logging.info(f"📊 Обработано {len(enhanced)} новостей через GPT")
    return enhanced

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
    Формирует полный утренний дайджест строго по формату:
    
    🌅 Доброе утро!
    
    📍 Погода:
    • Город1: min° → max° icon
    • Город2: min° → max° icon
    
    🎉 Государственный праздник.
    🎂 Дни рождения.
    (если есть)
    
    🌍 Сегодня в мире:
    — Новость1.
    — Новость2.
    (если есть)
    
    ✨ РАСШИРЕННОЕ ПОЖЕЛАНИЕ НА ДЕНЬ (юмор, факты, примета)
    
    Возвращает (text, weather_map).
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
    parts.append("")
    
    # 2. ПОГОДА
    weather_map = build_weather_map()
    if weather_map:
        parts.append("📍 Погода:")
        for city_ru, _ in CITIES:
            wi = weather_map.get(city_ru)
            if not wi:
                continue
            parts.append(f"• {city_ru}: {wi['min']}° → {wi['max']}° {wi['icon']}")
        parts.append("")
    
    # 3. ПРАЗДНИКИ + ДНИ РОЖДЕНИЯ (вместе в одном блоке)
    bds = _birthdays_for_today(target_date)
    holidays_block = build_holidays_section(target_date, _intel_chat, bds)
    if holidays_block and holidays_block.strip():
        parts.append(holidays_block)
        parts.append("")
    
    # 4. НОВОСТИ (GNews + GPT обработка)
    raw_news = _collect_gnews_articles(limit=3)  # Берем 2-3 новости
    news = _enhance_news_with_gpt(raw_news)      # Пропускаем через GPT
    if news:
        parts.append("🌍 Сегодня в мире:")
        for n in news:
            parts.append(f"— {n}")
        parts.append("")
    
    # 5. РАЗДЕЛИТЕЛЬ И РАСШИРЕННОЕ ПОЖЕЛАНИЕ НА ДЕНЬ (конец!)
    parts.append("─" * 40)  # Разделитель линия
    parts.append("")
    
    # Обворачиваем пожелание в blockquote для Telegram
    wish_text = ai_generate_wish_extended_intel()
    blockquote_wish = "<blockquote>" + wish_text + "</blockquote>"
    parts.append("✨ " + blockquote_wish)
    parts.append("")
    
    # 6. ФИНАЛЬНОЕ ЗАКЛЮЧЕНИЕ
    parts.append("Отличного дня! 🌟")
    
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


# Пожелание: 100-200 символов с юмором и приметой/шуткой/фактом/советом/новостью
def ai_generate_wish_extended_intel() -> str:
    """
    Генерирует компактное пожелание на день (100-200 символов):
    - Основное пожелание с юмором
    - Случайная секция: примета, шутка, факт, совет или новость
    
    Каждый раз новое!
    """
    tz = ZoneInfo(os.getenv("TZ", "Europe/Amsterdam"))
    now_local = datetime.now(tz)
    date_human = human_ru_date(now_local)
    weekday = WEEKDAY_RU[now_local.weekday()]
    
    # Получаем случайную секцию
    section_label, section_text = get_random_wish_section()
    
    prompt = (
        "Ты пишешь короткое пожелание на день для чата.\n\n"
        "ТРЕБОВАНИЯ:\n"
        "- Одно предложение на русском (50-80 слов, 100-150 символов)\n"
        "- Теплое и позитивное, с лёгким юмором\n"
        "- Максимум 1 эмодзи\n"
        "- Потом пустая строка\n"
        f"- Потом строка: '{section_label}: [текст]'\n"
        "- Всего 100-200 символов\n\n"
        "ПРИМЕРЫ:\n"
        "'Пусть сегодня улыбка не сходит с лица, а кофе не остывает в кружке ☕\n\n"
        "Примета: если встретишь кота утром — день будет удачным.'\n\n"
        "'Замечай маленькие радости, смейся от души и помни: ты сильнее, чем думаешь 💫\n\n"
        "Шутка: мотивация — это как дезодорант, её нужно применять каждый день.'\n\n"
        f"КОНТЕКСТ: {weekday}, {date_human}\n"
        "Создай ОРИГИНАЛЬНОЕ пожелание (не копируй примеры):"
    )
    
    for attempt in range(2):
        raw = _intel_chat(prompt, max_tokens=250, temperature=0.8)
        
        if raw and len(raw) > 50:
            text = raw.strip()
            if 100 <= len(text) <= 300:  # Проверяем размер
                return text
    
    # Стабильный компактный фоллбэк без AI
    wishes = [
        f"Пусть день дарует маленькие победы и большие улыбки 💫\n\n{section_label}: {section_text}",
        f"Замечай красивое, смейся искренне, живи полнотой 🌟\n\n{section_label}: {section_text}",
        f"Пусть удача касается тебя на каждом шагу ☕\n\n{section_label}: {section_text}",
        f"Этот день — твой, наполни его радостью и добром 🙂\n\n{section_label}: {section_text}",
        f"Кофе горячий, улыбка теплая, день удачный 💛\n\n{section_label}: {section_text}",
    ]
    
    choice = hash(date_human) % len(wishes)
    return wishes[choice]


# Старая версия для совместимости
def ai_generate_wish_240_intel() -> str:
    """Для обратной совместимости — вызывает новую расширенную версию"""
    return ai_generate_wish_extended_intel()



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

