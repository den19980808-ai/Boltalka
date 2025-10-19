# holidays.py
import os, re, json, logging, requests
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

# --- Пользовательские праздники (фоллбэк)
def load_custom_holidays() -> list[dict]:
    """
    Загружает список праздников из файла (HOLIDAYS_FILE) или ENV (HOLIDAYS_JSON).
    Поддерживает оба формата: {"date": "...", "name": "..."} и {"date": "...", "holiday": "..."}
    """
    path = os.getenv("HOLIDAYS_FILE") or os.getenv("HOLIDAYS_PATH")
    items = []
    
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f) or []
                logging.info(f"Loaded {len(data)} items from custom holidays file: {path}")
                
                # Преобразуем формат если нужно
                for item in data:
                    # Поддерживаем оба формата: "name" и "holiday"
                    if "holiday" in item and "name" not in item:
                        item["name"] = item["holiday"]
                    items.append(item)
                        
        except Exception as e:
            logging.warning(f"Custom Holidays file load failed: {e}")
    
    raw = (os.getenv("HOLIDAYS_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw) or []
            for item in data:
                if "holiday" in item and "name" not in item:
                    item["name"] = item["holiday"]
                items.append(item)
            logging.info(f"Loaded {len(items)} items from HOLIDAYS_JSON")
        except Exception as e:
            logging.warning(f"Custom Holidays JSON parse failed: {e}")
    
    logging.info(f"Total custom holidays loaded: {len(items)}")
    return items

def _parse_mmdd_any(s: str) -> tuple[int|None, int|None]:
    s = (s or "").strip()
    for fmt in ("%m-%d","%Y-%m-%d","%d-%m","%d.%m","%d/%m"):
        try:
            d0 = datetime.strptime(s, fmt).date()
            return d0.month, d0.day
        except Exception:
            continue
    return None, None

def custom_holidays_for_date(dt: date, items: list[dict]) -> list[dict]:
    """
    Фильтрует пользовательские праздники на указанную дату dt (по месяц-день),
    нормализует структуру под общий пайплайн и применяет чёрный список стран.
    """
    out = []
    target_mmdd = f"{dt.month:02d}-{dt.day:02d}"
    logging.info(f"Looking for custom holidays for date: {target_mmdd}")
    
    for h in items or []:
        mm, dd = _parse_mmdd_any(h.get("date"))
        if mm is None or not h.get("name"):
            logging.debug(f"Skipping invalid holiday item: {h}")
            continue
        
        item_mmdd = f"{mm:02d}-{dd:02d}"
        if item_mmdd != target_mmdd:
            continue
            
        iso = (h.get("iso") or "").upper()
        if iso in HOLIDAYS_COUNTRY_BLACKLIST:
            logging.debug(f"Skipping blacklisted country: {iso} for holiday {h['name']}")
            continue
            
        cname = h.get("country") or (COUNTRY_NAMES.get(iso) if iso in COUNTRY_NAMES else "Мир")
        tcat = (h.get("type") or "observance").lower()
        if tcat not in ("official","observance"):
            tcat = "observance"
            
        out.append({
            "name": h["name"], 
            "country": cname, 
            "iso": iso or "XX", 
            "type": tcat
        })
        logging.info(f"Found custom holiday: {h['name']} for {cname}")
    
    logging.info(f"Found {len(out)} custom holidays for {target_mmdd}")
    return out

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
def _render_block_ai(intel_chat, primary: dict | None, extras: list[dict], birthdays: list[dict]) -> str:
    data = {
        "birthdays": [{"name": b["name"], "age": b.get("age")} for b in (birthdays or [])][:5],
        "holidays": {"primary": primary or None, "extras": (extras or [])[:2]}
    }
    prompt = (
        "Собери компактный русскоязычный блок дней рождения и праздников для семейного чата.\n"
        "1) Если есть дни рождения: заголовок <b>Дни рождения</b>, затем 1–3 строки «🎂 Имя (возраст) — тёплая подпись 6–14 слов».\n"
        "2) Затем <b>Праздники сегодня</b>: одна строка для главного и до двух для дополнительных — эмодзи + название (со страной) + шутливая подпись 6–14 слов.\n"
        "Только русский, без хэштегов/приветствий/пояснений; верни готовый многострочный текст (HTML заголовки оставь).\n"
        f"Данные: {json.dumps(data, ensure_ascii=False)}"
    )
    text = ""
    if callable(intel_chat):
        try:
            text = intel_chat(prompt, max_tokens=380, temperature=0.85)
        except Exception:
            text = ""
    if text:
        cleaned = _sanitize_lines_block(text)
        if cleaned:
            return cleaned

    # Фоллбэк
    lines = []
    if birthdays:
        lines.append("<b>Дни рождения</b>")
        for b in birthdays[:3]:
            age = f" ({b['age']})" if b.get("age") else ""
            lines.append(f"🎂 {b['name']}{age} — пусть день будет тёплым и очень добрым.")
        lines.append("")
    lines.append("<b>Праздники сегодня</b>")
    if primary:
        tag = "🎉" if (primary.get("type") or "") == "official" else "✨"
        nm = primary.get("name") or "Праздник"
        cn = primary.get("country") or ""
        suffix = f" ({cn})" if cn else ""
        lines.append(f"{tag} {nm}{suffix} — достойный повод отметить малым, но искренним.")
    for it in (extras or [])[:2]:
        tag = "🎉" if (it.get("type") or "") == "official" else "✨"
        nm = it.get("name") or "Праздник"
        cn = it.get("country") or ""
        suffix = f" ({cn})" if cn else ""
        lines.append(f"{tag} {nm}{suffix} — маленькая искра радости в расписании.")
    return "\n".join(lines)

# --- Публичная точка входа
def build_holidays_section(dt: date, intel_chat, birthdays: list[dict] | None = None) -> str:
    now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
    items = []
    
    logging.info("=== Starting holiday collection ===")
    
    # 0) базовые страны по локальной дате
    for iso, name in COUNTRY_NAMES.items():
        d_loc = _local_date_for_iso(now_utc, iso)
        country_items = _collect_country_today(d_loc, iso, name)
        items.extend(country_items)
        if country_items:
            logging.info(f"Found {len(country_items)} holidays from {name}")

    # 1) широкий поиск, если пусто
    if not items:
        logging.info("No holidays from base countries, starting wide search...")
        matched = 0
        for iso, name in SCAN_COUNTRIES:
            d_loc = _local_date_for_iso(now_utc, iso)
            chunk = _collect_country_today(d_loc, iso, name)
            if chunk:
                items.extend(chunk)
                matched += 1
                logging.info(f"Found {len(chunk)} holidays from {name} (wide search)")
                if matched >= SCAN_COUNTRY_LIMIT:
                    break

    # 2) пользовательский фоллбэк Holidays.json, если по API всё пусто
    if not items:
        logging.info("No holidays from APIs, trying custom holidays...")
        # Берём «домашнюю» дату dt (параметр функции) — она уже соответствует TZ проекта (Amsterdam)
        user_list = load_custom_holidays()
        custom_items = custom_holidays_for_date(dt, user_list)
        items.extend(custom_items)
        if custom_items:
            logging.info(f"Found {len(custom_items)} custom holidays")

    # 3) выбор и рендер
    primary, extras = _select_top3(items)
    
    logging.info(f"Final selection - Primary: {primary['name'] if primary else 'None'}, Extras: {len(extras)}")
    logging.info(f"Birthdays count: {len(birthdays or [])}")
    
    block = _render_block_ai(intel_chat, primary, extras, birthdays or [])
    
    # при полном отсутствии событий добавим безопасную строку
    if not (birthdays or primary or extras):
        logging.warning("No holidays or birthdays found at all!")
        block = "<b>Праздники сегодня</b>\n✨ Сегодня больших праздников нет — придумайте свой маленький повод для улыбки."
    
    return _sanitize_lines_block(block)

