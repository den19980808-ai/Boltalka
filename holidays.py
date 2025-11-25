# holidays.py
import os, re, json, logging, requests, random
from datetime import datetime, date
from zoneinfo import ZoneInfo

# --- ENV
CALENDARIFIC_API_KEY = os.getenv("CALENDARIFIC_API_KEY")
API_NINJAS_KEY = os.getenv("API_NINJAS_KEY")
API_NINJAS_PREMIUM = os.getenv("API_NINJAS_PREMIUM", "false").lower() in ("1", "true", "yes")

# Базовые локальные страны (как у вас было в main)
COUNTRY_NAMES = {"NL": "Нидерланды", "UA": "Украина", "PL": "Польша"}

COUNTRY_TZ = {
    "NL": "Europe/Amsterdam", "UA": "Europe/Kyiv", "PL": "Europe/Warsaw",
    "US": "America/New_York", "GB": "Europe/London", "DE": "Europe/Berlin",
    "FR": "Europe/Paris", "ES": "Europe/Madrid", "IT": "Europe/Rome",
}

HOLIDAYS_COUNTRY_BLACKLIST = set(x.strip().upper() for x in os.getenv(
    "HOLIDAYS_COUNTRY_BLACKLIST", "RU"
).split(",") if x.strip())
def _local_date_for_iso(now_utc: datetime, iso: str) -> date:
    tz = COUNTRY_TZ.get(iso.upper(), "UTC")
    return now_utc.astimezone(ZoneInfo(tz)).date()

# «Широкий поиск» по странам при пустом дне локально
SCAN_COUNTRIES_ENV = os.getenv(
    "SCAN_COUNTRIES_ENV",
    "US:США,DE:Германия,FR:Франция,ES:Испания,IT:Италия,GB:Великобритания"
)
SCAN_COUNTRY_LIMIT = int(os.getenv("SCAN_LIMIT", "2"))

# --- Санитайзеры
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]")
HTML_BR_RE = re.compile(r"<\s*br\s*/?\s*>", flags=re.I)
ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}

BAD_MARKERS = [
    "we need", "the user asks", "let's craft", "let us", "explain", "instruction",
    "output json", "return json", "формат json", "без пояснений", "требуется", "нужно сделать"
]
HOLI_BAD_MARKERS = BAD_MARKERS + ["let's", "we need", "user asks", "explain", "format"]

def _local_date_for_iso(now_utc: datetime, iso: str) -> date:
    tz = COUNTRY_TZ.get(iso.upper(), "UTC")
    return now_utc.astimezone(ZoneInfo(tz)).date()

def strip_unsupported_html(s: str) -> str:
    s = HTML_BR_RE.sub("\n", s or "")
    def _repl(m):
        tag = m.group(1).lower().strip("/")
        return m.group(0) if tag in ALLOWED_TAGS else ""
    return re.sub(r"</?([A-Za-z0-9\-]+)(\s+[^>]*)?>", _repl, s)

def is_russian_strict(text: str) -> bool:
    if not text:
        return False
    return len(CYRILLIC_RE.findall(text)) > 0 and len(LATIN_RE.findall(text)) == 0

def _sanitize_lines_block(text: str) -> str:
    lines = (text or "").splitlines()
    cleaned = []
    for ln in lines:
        ln = (ln or "").strip()
        if not ln:
            continue
        low = ln.lower()
        if any(m in low for m in HOLI_BAD_MARKERS):
            continue
        core = re.sub(r"[^\u0400-\u04FF ]", "", ln)
        if not is_russian_strict(core):
            letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", ln)
            if LATIN_RE.search(letters):
                continue
        ln = re.sub(r"\s+—\s+", " — ", ln)
        cleaned.append(ln)
    cleaned = cleaned[:6]
    return strip_unsupported_html("\n".join(cleaned))

# --- ТОЛЬКО государственные праздники из API (без holidays.json)

# --- Утилиты
def _parse_pairs(env_str: str) -> list[tuple[str, str]]:
    out = []
    for token in (env_str or "").split(","):
        token = token.strip()
        if ":" in token:
            iso, name = token.split(":", 1)
            out.append((iso.strip().upper(), name.strip()))
    return out

SCAN_COUNTRIES = _parse_pairs(SCAN_COUNTRIES_ENV)

def _cat_from_types_calendarific(types: list[str]) -> str:
    t = [x.lower() for x in (types or [])]
    return "observance" if any(x in t for x in ["observance", "local holiday", "common local holiday"]) else "official"

def _cat_from_type_ninjas(t: str) -> str:
    t = (t or "").lower()
    official = ["public_holiday", "major_holiday", "national_holiday", "official_holiday", "federal_holiday"]
    return "official" if any(x in t for x in official) else "observance"

# --- Источники
def _fetch_calendarific_today(dt: date, iso: str, cname_print: str) -> list[dict]:
    out = []
    if not CALENDARIFIC_API_KEY:
        return out
    y, m, d = dt.year, dt.month, dt.day
    try:
        r = requests.get(
            "https://calendarific.com/api/v2/holidays",
            params={"api_key": CALENDARIFIC_API_KEY, "country": iso, "year": y, "month": m, "day": d},
            timeout=12
        )
        data = r.json()
        hols = (data.get("response", {}) or {}).get("holidays", []) or []
        for h in hols:
            out.append({"name": h.get("name"), "country": cname_print, "iso": iso, "type": _cat_from_types_calendarific(h.get("type") or [])})
    except Exception:
        pass
    return out

def _fetch_nager_today(dt: date, iso: str, cname_print: str) -> list[dict]:
    out = []
    try:
        r = requests.get(f"https://date.nager.at/api/v3/PublicHolidays/{dt.year}/{iso}", timeout=12)
        hols = r.json() or []
        today_iso = dt.isoformat()
        for h in hols:
            if h.get("date") == today_iso:
                out.append({"name": h.get("localName") or h.get("name"), "country": cname_print, "iso": iso, "type": "official"})
    except Exception:
        pass
    return out

def _fetch_ninjas_today(dt: date, iso: str, cname_print: str) -> list[dict]:
    out = []
    if not API_NINJAS_KEY:
        return out
    try:
        headers = {"X-Api-Key": API_NINJAS_KEY}
        params = {"country": iso}
        r = requests.get("https://api.api-ninjas.com/v1/holidays", headers=headers, params=params, timeout=12)
        data = None
        if r.status_code == 200:
            data = r.json() or []
        elif API_NINJAS_PREMIUM:
            r2 = requests.get("https://api.api-ninjas.com/v1/holidays", headers=headers, params={"country": iso, "year": dt.year}, timeout=12)
            if r2.status_code == 200:
                data = r2.json() or []
        if not data:
            return out
        today = dt.isoformat()
        for h in data:
            if (h.get("date") or "") == today:
                out.append({"name": h.get("name"), "country": cname_print, "iso": iso, "type": _cat_from_type_ninjas(h.get("type"))})
    except Exception:
        pass
    return out

def _collect_country_today(dt: date, iso: str, cname_print: str) -> list[dict]:
    items = []
    items.extend(_fetch_calendarific_today(dt, iso, cname_print))
    if not items:
        items.extend(_fetch_nager_today(dt, iso, cname_print))
    if not items:
        items.extend(_fetch_ninjas_today(dt, iso, cname_print))
    return items

def _dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for it in items:
        key = (it.get("name") or "").strip().lower() + "|" + (it.get("country") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def _score(it: dict) -> int:
    t = (it.get("type") or "").lower()
    if t == "official": return 100
    if "national" in t: return 90
    if "observance" in t or "local" in t or "common" in t: return 60
    return 50

def _select_top3(items: list[dict]) -> tuple[dict | None, list[dict]]:
    items = [x for x in items if x.get("name")]
    items = _dedupe(items)
    items.sort(key=lambda x: (-_score(x), (x.get("country") or ""), (x.get("name") or "")))
    if not items:
        return None, []
    return items[0], items[1:3]

def _parse_mmdd(s: str) -> tuple[int | None, int | None]:
    s = (s or "").strip()
    for fmt in ("%m-%d", "%Y-%m-%d", "%d-%m", "%d.%m", "%d/%m"):
        try:
            dt0 = datetime.strptime(s, fmt).date()
            return dt0.month, dt0.day
        except Exception:
            continue
    return None, None
def get_birthdays() -> list[dict]:
    """Универсальная функция для загрузки дней рождений"""
    return load_birthdays()  # или load_birthdays() в зависимости от того, какое имя вы выберете

# --- ДР (самостоятельно, чтобы модуль был автономным)
def load_birthdays() -> list[dict]:
    path = os.getenv("BIRTHDAYS_FILE")
    logging.info(f"Looking for birthdays file at: {path}")
    
    if path and os.path.exists(path):
        logging.info(f"Birthdays file found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                logging.info(f"Successfully loaded {len(data)} birthdays from file")
                return data
        except Exception as e:
            logging.error(f"Failed to load birthdays from file {path}: {e}")
    else:
        logging.warning(f"Birthdays file not found at: {path}")
    
    raw = os.getenv("BIRTHDAYS_JSON", "").strip()
    if raw:
        logging.info("Found birthdays in BIRTHDAYS_JSON environment variable")
        try:
            data = json.loads(raw)
            logging.info(f"Successfully loaded {len(data)} birthdays from env")
            return data
        except Exception as e:
            logging.error(f"Failed to parse BIRTHDAYS_JSON: {e}")
    
    logging.warning("No birthdays data available")
    return []

def birthdays_for_date(dt: date, people: list[dict]) -> list[dict]:
    out = []
    for p in people or []:
        name = (p.get("name") or "").strip()
        mmdd = (p.get("date") or "").strip()
        mm, dd = _parse_mmdd(mmdd)
        if not name or mm is None:
            continue
        if (dt.month, dt.day) == (mm, dd):
            year = p.get("year")
            age = dt.year - year if isinstance(year, int) and 1900 <= year <= dt.year else None
            out.append({"name": name, "age": age, "note": (p.get("note") or "").strip()})
    return out

# --- Рендер через ИИ + фоллбэк
# --- Рендер через ИИ + фоллбэк
# --- Рендер через ИИ + фоллбэк
def _render_block_ai(intel_chat, primary: dict | None, extras: list[dict], birthdays: list[dict]) -> str:
    data = {
        "birthdays": [
            {"name": b.get("name"), "age": b.get("age"), "note": (b.get("note") or "")}
            for b in (birthdays or [])
        ][:3],
        "holidays": {
            "primary": (
                {"name": primary.get("name"), "country": primary.get("country"), "type": primary.get("type")}
                if primary else None
            ),
            "extras": [
                {"name": it.get("name"), "country": it.get("country"), "type": it.get("type")}
                for it in (extras or [])[:2]
            ],
        },
    }

    prompt = (
        "Ты создаешь информационный блок для семейного чата Telegram. Следуй строго этим правилам:\n\n"
        
        "🎯 РОЛЬ: Дружелюбный помощник для семьи\n"
        "🎯 ЦЕЛЬ: Информировать о днях рождения и праздниках\n"
        "🎯 ТОН: Теплый, естественный, легкий юмор\n\n"
        
        "📝 СТРУКТУРА БЛОКА:\n"
        "1. Если есть дни рождения:\n"
        "   <b>Дни рождения</b>\n"
        "   • Для каждого: «🎂 Имя (возраст) — персонализированная подпись 8-16 слов»\n"
        "   • Креативно перефразируй note (не копируй дословно!)\n"
        "   • Добавь легкую метафору или образ\n"
        "   • Если возраста нет — не указывай скобки\n\n"
        
        "2. ОБЯЗАТЕЛЬНО: ОДНА ПУСТАЯ СТРОКА между блоками\n\n"
        
        "3. Праздники:\n"
        "   <b>Праздники сегодня</b>\n"
        "   • Для каждого: «[эмодзи] Название (страна) — остроумная подпись 8-14 слов»\n"
        "   • Эмодзи: 🎉 для официальных, ✨ для неофициальных\n"
        "   • Подпись должна быть разной для каждого праздника\n\n"
        
        "🚫 ЗАПРЕЩЕНО:\n"
        "- Клише: «достойный повод», «пусть день будет», «отметить малым»\n"
        "- Императивы: «возьми», «не забудь»\n" 
        "- Придумывать факты вне данных\n"
        "- Копировать более 3 слов подряд из note\n"
        "- Более 1 эмодзи в подписи\n\n"
        
        "✅ ТРЕБУЕМЫЙ СТИЛЬ:\n"
        "- Естественная разговорная речь\n"
        "- Легкий интеллигентный юмор\n"
        "- Теплота без фамильярности\n"
        "- Конкретика вместо общих фраз\n\n"
        
        "📋 ПРИМЕР ХОРОШЕГО ТОНА (НЕ КОПИРОВАТЬ!):\n"
        "<b>Дни рождения</b>\n"
        "🎂 Мария (35) — как хорошая книга: с каждым годом становится только интереснее и мудрее.\n"
        "🎂 Алексей — сегодня твой день сияет особенным светом, пусть он будет полон теплых моментов.\n"
        "\n"
        "<b>Праздники сегодня</b>\n"
        "🎉 День библиотек (Польша) — прекрасный повод перелистать страницы дня в поисках маленьких чудес.\n"
        "✨ День шоколада (Мир) — сладкий намек на то, что иногда жизнь нужно воспринимать не так серьезно.\n\n"
        
        "🎲 ТВОИ ДАННЫЕ:\n"
        f"{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
        
        "🔚 ФОРМАТ ВЫВОДА:\n"
        "Только готовый текст блока с HTML-заголовками. Без пояснений, без JSON, без мета-комментариев."
    )

    text = ""
    if callable(intel_chat):
        try:
            text = intel_chat(prompt, max_tokens=500, temperature=0.75)
        except Exception:
            text = ""

    if text:
        cleaned = _sanitize_lines_block(text)
        if cleaned:
            return cleaned

    # Улучшенный фоллбэк с гарантированным отступом
    def create_birthday_line(b: dict) -> str:
        name = b.get("name", "")
        age = f" ({b['age']})" if b.get("age") else ""
        note = (b.get("note") or "").strip()
        
        if note:
            # Легкое перефразирование заметки
            if "день рождения" in note.lower():
                note = note.replace("день рождения", "этот особенный день")
            if "поздравляем" in note.lower():
                note = note.replace("поздравляем", "радуемся")
            return f"🎂 {name}{age} — {note}"
        else:
            themes = [
                f"— сегодня твой день сияет особенным светом и теплом.",
                f"— пусть этот день принесет столько радости, сколько ты даришь другим.",
                f"— как хорошая музыка: с каждым годом звучит все богаче и глубже.",
            ]
            theme_index = hash(name) % len(themes)
            return f"🎂 {name}{age} {themes[theme_index]}"

    lines = []
    if birthdays:
        lines.append("<b>Дни рождения</b>")
        for b in birthdays[:3]:
            lines.append(create_birthday_line(b))
        lines.append("")  # ОБЯЗАТЕЛЬНАЯ пустая строка между блоками

    lines.append("<b>Праздники сегодня</b>")
    
    def create_holiday_line(item: dict) -> str:
        tag = "🎉" if (item.get("type") or "") == "official" else "✨"
        name = item.get("name") or "Праздник"
        country = item.get("country") or ""
        suffix = f" ({country})" if country else ""
        
        themes = [
            f"— добавляет особого настроения и поводов для улыбки.",
            f"— прекрасный повод заметить маленькие радости вокруг.",
            f"— напоминает, что каждый день может стать особенным.",
            f"— привносит в будни каплю праздничного волшебства.",
        ]
        theme_index = hash(name) % len(themes)
        return f"{tag} {name}{suffix} {themes[theme_index]}"
    
    if primary:
        lines.append(create_holiday_line(primary))
    for it in (extras or [])[:2]:
        lines.append(create_holiday_line(it))
        
    return "\n".join(lines)

    
    



# --- Публичная точка входа
def build_holidays_section(dt: date, intel_chat, birthdays: list[dict] | None = None) -> str:
    logging.info(f"📍 Вызов build_holidays_section: dt={dt}, type(dt)={type(dt).__name__}, birthdays={len(birthdays or [])} людей")
    
    # Если dt передана как дата, преобразуем в UTC datetime
    if isinstance(dt, date) and not isinstance(dt, datetime):
        # Это обычная дата, преобразуем в datetime в UTC
        now_utc = datetime.combine(dt, datetime.min.time()).replace(tzinfo=ZoneInfo("UTC"))
    else:
        # Это datetime, берем его
        now_utc = dt.replace(tzinfo=ZoneInfo("UTC")) if dt.tzinfo is None else dt
    
    items = []
    
    logging.info("🎯 Начинаем сбор информации о праздниках...")
    
    # 0) базовые страны по локальной дате
    for iso, name in COUNTRY_NAMES.items():
        d_loc = _local_date_for_iso(now_utc, iso)
        country_items = _collect_country_today(d_loc, iso, name)
        items.extend(country_items)
        if country_items:
            logging.info(f"✅ Найдено {len(country_items)} праздников в {name}")

    # 1) широкий поиск, если пусто
    if not items:
        logging.info("🔍 В основных странах праздников нет, начинаем расширенный поиск...")
        matched = 0
        for iso, name in SCAN_COUNTRIES:
            d_loc = _local_date_for_iso(now_utc, iso)
            chunk = _collect_country_today(d_loc, iso, name)
            if chunk:
                items.extend(chunk)
                matched += 1
                logging.info(f"🌍 Найдено {len(chunk)} праздников в {name} (расширенный поиск)")
                if matched >= SCAN_COUNTRY_LIMIT:
                    break

    # 2) выбор и рендер (БЕЗ пользовательских праздников из holidays.json)
    primary, extras = _select_top3(items)
    
    logging.info(f"🏆 Выбрано: Главный - {primary['name'] if primary else 'нет'}, Дополнительные - {len(extras)}")
    logging.info(f"🎂 Дней рождения сегодня: {len(birthdays or [])}")
    
    block = _render_block_ai(intel_chat, primary, extras, birthdays or [])
    
    # при полном отсутствии событий
    if not (birthdays or primary or extras):
        logging.warning("📭 Государственных праздников и дней рождения не найдено")
        block = ""  # Пустой блок - не показываем ничего
    
    return _sanitize_lines_block(block)

