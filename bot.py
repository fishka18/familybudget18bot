"""
Телеграм-бот для учёта расходов, доходов и накоплений.
Все записи складываются в Google-таблицу.

Запуск:  python bot.py
Настройки берутся из файла .env (см. .env.example) или из переменных окружения.
"""

import os
import re
import json
import base64
import logging
import calendar
import datetime as dt
from zoneinfo import ZoneInfo

import gspread
import telebot
from telebot import types
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ---------------------------------------------------------------- настройки

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "").strip()
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json").strip()
# на хостинге ключ удобнее держать не файлом, а переменной окружения:
# сюда кладётся всё содержимое credentials.json (или оно же в base64)
CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Операции").strip()
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow").strip()
CURRENCY = os.getenv("CURRENCY", "₽").strip()

# кто может пользоваться ботом (id через запятую). Пусто = никто, бот подскажет id
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}

EXPENSE, INCOME, SAVING = "Расход", "Доход", "Накопление"
FACT, PLAN = "Факт", "План"
ONCE, REGULAR = "Однократно", "Регулярно"

KIND_BY_TAG = {"e": EXPENSE, "i": INCOME, "s": SAVING}
TAG_BY_KIND = {v: k for k, v in KIND_BY_TAG.items()}

CATEGORIES = {
    EXPENSE: [
        "Продукты", "Кафе", "Транспорт", "Дом и ЖКХ", "Дети",
        "Здоровье", "Одежда", "Развлечения", "Подарки", "Путешествия",
        "Образование", "Прочее",
    ],
    INCOME: [
        "Зарплата", "Подработка", "Инвестиции", "Подарок", "Возврат", "Прочее",
    ],
    SAVING: [
        "Подушка", "Отпуск", "Крупная покупка", "Образование детей",
        "Пенсия", "Инвестиции", "Прочее",
    ],
}

# короткие слова, которые бот понимает как категорию
ALIASES = {
    "еда": "Продукты", "магазин": "Продукты", "супермаркет": "Продукты",
    "такси": "Транспорт", "метро": "Транспорт", "бензин": "Транспорт",
    "жкх": "Дом и ЖКХ", "квартира": "Дом и ЖКХ", "аренда": "Дом и ЖКХ",
    "ресторан": "Кафе", "обед": "Кафе", "кофе": "Кафе",
    "аптека": "Здоровье", "врач": "Здоровье", "лекарства": "Здоровье",
    "садик": "Дети", "школа": "Дети", "игрушки": "Дети",
    "зп": "Зарплата", "аванс": "Зарплата",
    "дивиденды": "Инвестиции", "проценты": "Инвестиции", "вклад": "Инвестиции",
}

HEADERS = ["Дата", "Время", "Пользователь", "Тип", "Статус", "Сумма",
           "Категория", "Комментарий", "Повтор", "user_id"]

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
          "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

MAX_DATES = 40  # предохранитель: больше дат за один раз не запишем

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger("bot")

if not BOT_TOKEN:
    raise SystemExit("Не задан BOT_TOKEN — заполните файл .env")
if not SPREADSHEET_ID:
    raise SystemExit("Не задан SPREADSHEET_ID — заполните файл .env")

bot = telebot.TeleBot(BOT_TOKEN)


def today_date():
    return dt.datetime.now(ZoneInfo(TIMEZONE)).date()


# ------------------------------------------------------------ google sheets

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_worksheet = None
_spreadsheet = None
_columns = {}


def credentials_source():
    """Откуда берётся ключ Google: из переменной окружения или из файла."""
    return "переменная GOOGLE_CREDENTIALS_JSON" if CREDENTIALS_JSON else CREDENTIALS_FILE


def load_key_data():
    """Содержимое ключа сервисного аккаунта в виде словаря."""
    if CREDENTIALS_JSON:
        raw = CREDENTIALS_JSON
        if not raw.lstrip().startswith("{"):
            raw = base64.b64decode(raw).decode("utf-8")
        return json.loads(raw)
    with open(CREDENTIALS_FILE, encoding="utf-8-sig") as f:
        return json.load(f)


def load_credentials():
    return Credentials.from_service_account_info(load_key_data(), scopes=SCOPES)


def ensure_headers(ws):
    """
    Следит, чтобы в первой строке были все нужные заголовки.
    Недостающие дописывает справа — старые данные при этом не сдвигаются.
    Возвращает словарь «название столбца -> его номер».
    """
    header = ws.row_values(1)
    if not header:
        if ws.col_count < len(HEADERS):
            ws.add_cols(len(HEADERS) - ws.col_count)
        ws.append_row(HEADERS, value_input_option="USER_ENTERED")
        ws.format(f"A1:{chr(64 + len(HEADERS))}1", {"textFormat": {"bold": True}})
        return {name: index for index, name in enumerate(HEADERS)}

    missing = [name for name in HEADERS if name not in header]
    if missing:
        # в таблице может быть физически меньше столбцов, чем нам нужно —
        # сначала расширяем сетку, иначе Google ответит «exceeds grid limits»
        needed = len(header) + len(missing)
        if ws.col_count < needed:
            ws.add_cols(needed - ws.col_count)
        for name in missing:
            header.append(name)
            ws.update_cell(1, len(header), name)
            log.info("В таблицу добавлен столбец «%s»", name)

    return {name: index for index, name in enumerate(header)}


def get_worksheet():
    """Открывает лист таблицы (и создаёт его с заголовками, если нужно)."""
    global _worksheet, _spreadsheet, _columns
    if _worksheet is not None:
        return _worksheet

    spreadsheet = gspread.authorize(load_credentials()).open_by_key(SPREADSHEET_ID)
    _spreadsheet = spreadsheet

    try:
        ws = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME, rows=1000, cols=len(HEADERS)
        )

    _columns = ensure_headers(ws)
    _worksheet = ws
    return ws


def add_rows(entries, user):
    """
    Пишет операции в таблицу. entries — список словарей с ключами
    kind, status, amount, category, comment, date, repeat.
    """
    ws = get_worksheet()
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    width = max(_columns.values()) + 1

    rows = []
    for entry in entries:
        row = [""] * width
        values = {
            "Дата": entry["date"].strftime("%Y-%m-%d"),
            "Время": now.strftime("%H:%M"),
            "Пользователь": user_name(user),
            "Тип": entry["kind"],
            "Статус": entry["status"],
            "Сумма": entry["amount"],
            "Категория": entry["category"],
            "Комментарий": entry.get("comment", ""),
            "Повтор": entry.get("repeat", ""),
            "user_id": str(user.id),
        }
        for name, value in values.items():
            if name in _columns:
                row[_columns[name]] = value
        rows.append(row)

    response = ws.append_rows(rows, value_input_option="USER_ENTERED")
    written = (response or {}).get("updates", {}).get("updatedRange", "")
    log.info("Записано строк: %s -> %s", len(rows), written or "?")
    return written


def read_rows():
    """Все записи из таблицы в виде списка словарей."""
    ws = get_worksheet()
    values = ws.get_all_values()

    def cell(row, name):
        index = _columns.get(name)
        if index is None or index >= len(row):
            return ""
        return row[index]

    rows = []
    for row in values[1:]:
        try:
            amount = float(
                re.sub(r"[\s ]", "", cell(row, "Сумма")).replace(",", ".")
            )
        except ValueError:
            continue
        rows.append({
            "date": cell(row, "Дата"),
            "user": cell(row, "Пользователь"),
            "kind": cell(row, "Тип"),
            "status": cell(row, "Статус") or FACT,
            "amount": amount,
            "category": cell(row, "Категория"),
            "comment": cell(row, "Комментарий"),
            "repeat": cell(row, "Повтор"),
            "user_id": str(cell(row, "user_id")).strip(),
        })
    return rows


def delete_last_row_of(user_id):
    """Удаляет последнюю запись этого пользователя. Возвращает описание или None."""
    ws = get_worksheet()
    values = ws.get_all_values()
    id_col = _columns.get("user_id", len(HEADERS) - 1)

    for index in range(len(values) - 1, 0, -1):
        row = values[index]
        if len(row) > id_col and str(row[id_col]).strip() == str(user_id):
            def cell(name):
                i = _columns.get(name)
                return row[i] if i is not None and i < len(row) else ""
            ws.delete_rows(index + 1)  # в таблице строки нумеруются с 1
            return (f"{cell('Статус')} · {cell('Тип')} {cell('Сумма')} {CURRENCY} — "
                    f"{cell('Категория')} ({cell('Дата')})")
    return None


# -------------------------------------------------------------- разбор ввода

AMOUNT_RE = re.compile(r"^([+\-])?(\d+(?:[.,]\d+)?)\s*(к|k|тыс)?$", re.IGNORECASE)
DATE_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$")

MODIFIERS = {
    "план": ("status", PLAN),
    "плановый": ("status", PLAN),
    "факт": ("status", FACT),
    "доход": ("kind", INCOME),
    "расход": ("kind", EXPENSE),
    "накопление": ("kind", SAVING),
    "накоп": ("kind", SAVING),
    "копилка": ("kind", SAVING),
    "отложить": ("kind", SAVING),
}


def parse_amount(token):
    """'1500' -> 1500.0, '1,5к' -> 1500.0, '+300' -> 300.0. Иначе None."""
    token = re.sub(r"[\s ]", "", token)
    match = AMOUNT_RE.match(token)
    if not match:
        return None
    _sign, number, thousands = match.groups()
    value = float(number.replace(",", "."))
    if thousands:
        value *= 1000
    if value <= 0:
        return None
    return round(value, 2)


def normalize(text):
    return re.sub(r"[^a-zа-яё0-9]", "", text.lower())


def parse_date(token, today):
    """'вчера' или '12.08' / '12.08.2026' -> дата. Не дата — None."""
    key = token.strip().lower()
    if key in DATE_WORDS:
        return today - dt.timedelta(days=DATE_WORDS[key])

    match = DATE_RE.match(key)
    if not match:
        return None

    day, month, year = match.groups()
    day, month = int(day), int(month)
    if year is None:
        candidate_year = today.year
    else:
        candidate_year = int(year)
        if candidate_year < 100:
            candidate_year += 2000

    try:
        result = dt.date(candidate_year, month, day)
    except ValueError:
        return None

    # год не указан, а дата больше чем на полгода вперёд — значит, прошлый год
    if year is None and result > today + dt.timedelta(days=180):
        try:
            result = dt.date(candidate_year - 1, month, day)
        except ValueError:
            return None
    return result


def match_category(word, kind):
    """Ищет категорию из списка по началу слова; иначе возвращает слово как есть."""
    key = normalize(word)
    if not key:
        return None
    if key in ALIASES:
        return ALIASES[key]
    for category in CATEGORIES[kind]:
        cat_key = normalize(category)
        if cat_key.startswith(key) or key.startswith(cat_key):
            return category
    return word.strip().capitalize()


def _try_parse(parts, today, allow_date, kind_hint=None):
    when = None
    kind = None
    status = None
    index = 0

    while index < len(parts):
        # дату проверяем раньше суммы: «12.09» — это дата, а не 12 рублей 9 копеек.
        # если после такой «даты» суммы не окажется, второй заход разберёт её как сумму
        if allow_date and when is None:
            maybe = parse_date(parts[index], today)
            if maybe is not None:
                when = maybe
                index += 1
                continue
        if parse_amount(parts[index]) is not None:
            break
        modifier = MODIFIERS.get(normalize(parts[index]))
        if modifier:
            field, value = modifier
            if field == "kind":
                kind = value
            else:
                status = value
            index += 1
            continue
        break

    if index >= len(parts):
        return None

    amount = parse_amount(parts[index])
    if amount is None:
        return None

    if kind is None:
        # тип, выбранный кнопками, важнее знака «+» в тексте
        kind = kind_hint or (INCOME if parts[index].startswith("+") else EXPENSE)

    rest = parts[index + 1:]
    category = match_category(rest[0], kind) if rest else None
    comment = " ".join(rest[1:]) if len(rest) > 1 else ""

    return {
        "kind": kind,
        "status": status or FACT,
        "amount": amount,
        "category": category,
        "comment": comment,
        "date": when or today,
    }


def parse_entry(text, today=None, kind_hint=None):
    """
    Разбирает быстрый ввод: '450 продукты', '+70000 зарплата',
    'вчера 450 продукты', 'план 12.09 5000 отпуск', 'накопление 10000 подушка'.
    kind_hint — тип, уже выбранный кнопками: от него зависит список категорий.
    Возвращает словарь операции либо None.
    """
    if today is None:
        today = today_date()
    parts = text.strip().split()
    if not parts:
        return None
    # первый заход — с распознаванием даты, второй — без него
    # (на случай суммы вида 12.08, которую можно принять за дату)
    return (_try_parse(parts, today, True, kind_hint)
            or _try_parse(parts, today, False, kind_hint))


# ------------------------------------------------------ контекст в сообщении

def money(value):
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} {CURRENCY}"


def user_name(user):
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return name or (user.username or str(user.id))


def render_context(status, kind, dates=None, amount=None, comment="", tail=""):
    """
    Собирает текст сообщения-мастера. Из этого же текста контекст потом
    читается обратно — поэтому бот ничего не забывает даже после перезапуска.
    """
    lines = [f"📝 {status} · {kind}"]
    if dates:
        lines.append("Даты: " + ", ".join(d.strftime("%d.%m.%Y") for d in dates))
    if amount is not None:
        lines.append(f"Сумма: {amount:g}")
    if comment:
        lines.append(f"Комментарий: {comment}")
    if tail:
        lines.append("")
        lines.append(tail)
    return "\n".join(lines)


def parse_context(text):
    """Читает контекст обратно из текста сообщения-мастера."""
    if not text or not text.startswith("📝"):
        return None

    head = text.split("\n", 1)[0]
    status = PLAN if PLAN in head else FACT
    kind = next((k for k in (SAVING, INCOME, EXPENSE) if k in head), None)
    if kind is None:
        return None

    dates = []
    match = re.search(r"^Даты:\s*(.+)$", text, re.MULTILINE)
    if match:
        for chunk in match.group(1).split(","):
            try:
                dates.append(dt.datetime.strptime(chunk.strip(), "%d.%m.%Y").date())
            except ValueError:
                continue

    amount = None
    match = re.search(r"^Сумма:\s*([\d.,]+)", text, re.MULTILINE)
    if match:
        amount = parse_amount(match.group(1))

    comment = ""
    match = re.search(r"^Комментарий:\s*(.+)$", text, re.MULTILINE)
    if match:
        comment = match.group(1).strip()

    return {"status": status, "kind": kind, "dates": dates,
            "amount": amount, "comment": comment}


# --------------------------------------------------------------- клавиатуры

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Новая операция")
    kb.row("📊 Итоги за месяц", "👤 Мои итоги")
    kb.row("🧾 Последние записи", "↩️ Удалить последнюю")
    kb.row("❓ Как записывать")
    return kb


def kinds_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        types.InlineKeyboardButton(kind, callback_data=f"w|k|{TAG_BY_KIND[kind]}")
        for kind in (EXPENSE, INCOME, SAVING)
    ])
    return kb


def status_keyboard(tag):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(FACT, callback_data=f"w|s|{tag}|F"),
        types.InlineKeyboardButton(PLAN, callback_data=f"w|s|{tag}|P"),
    )
    return kb


def repeat_keyboard(tag):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(ONCE, callback_data=f"w|r|{tag}|1"),
        types.InlineKeyboardButton(REGULAR, callback_data=f"w|r|{tag}|R"),
    )
    return kb


def categories_keyboard(kind):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(*[
        types.InlineKeyboardButton(category, callback_data=f"cat|{index}")
        for index, category in enumerate(CATEGORIES[kind])
    ])
    return kb


def calendar_keyboard(year, month, selected):
    """Календарь месяца: отмеченные даты помечены галочкой."""
    kb = types.InlineKeyboardMarkup(row_width=7)
    stamp = f"{year}{month:02d}"

    previous = dt.date(year, month, 1) - dt.timedelta(days=1)
    following = dt.date(year, month, 28) + dt.timedelta(days=7)
    kb.row(
        types.InlineKeyboardButton(
            "‹", callback_data=f"c|m|{previous.year}{previous.month:02d}"),
        types.InlineKeyboardButton(
            f"{MONTHS[month - 1].capitalize()} {year}", callback_data="c|nop"),
        types.InlineKeyboardButton(
            "›", callback_data=f"c|m|{following.year}{following.month:02d}"),
    )
    kb.row(*[types.InlineKeyboardButton(day, callback_data="c|nop")
             for day in WEEKDAYS])

    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month):
        buttons = []
        for day in week:
            if day == 0:
                buttons.append(types.InlineKeyboardButton(" ", callback_data="c|nop"))
                continue
            current = dt.date(year, month, day)
            mark = "✅" if current in selected else str(day)
            buttons.append(types.InlineKeyboardButton(
                mark, callback_data=f"c|d|{stamp}{day:02d}"))
        kb.row(*buttons)

    kb.row(
        types.InlineKeyboardButton("🔁 Ежемесячно", callback_data="c|q|m"),
        types.InlineKeyboardButton("🔁 Еженедельно", callback_data="c|q|w"),
    )
    kb.row(
        types.InlineKeyboardButton("Очистить", callback_data="c|x"),
        types.InlineKeyboardButton("Готово ✓", callback_data="c|ok"),
    )
    return kb


CALENDAR_HINT = ("Отметьте даты, когда операция повторяется, и нажмите «Готово».\n"
                 "«Ежемесячно» повторит отмеченные числа до конца года, "
                 "«Еженедельно» — восемь раз подряд.")

ASK_AMOUNT = "Напишите сумму и категорию, например: <code>5000 продукты</code>"

HELP_TEXT = (
    "<b>Как записывать</b>\n\n"
    "Расход — просто сумма и категория:\n"
    "<code>450 продукты</code>\n"
    "<code>1200 кафе обед с Аней</code>\n"
    "<code>1,5к транспорт</code>\n\n"
    "Доход — со знаком «+»:\n"
    "<code>+70000 зарплата</code>\n\n"
    "Накопление и план — словом в начале:\n"
    "<code>накопление 10000 подушка</code>\n"
    "<code>план 12.09 5000 отпуск</code>\n\n"
    "Дата покупки — первым словом:\n"
    "<code>вчера 450 продукты</code>\n"
    "<code>12.08 3200 одежда куртка Мише</code>\n\n"
    "Кнопка «➕ Новая операция» открывает пошаговый ввод: "
    "тип → статус → для плана повтор с календарём.\n\n"
    "Команды: /start, /report, /last, /undo, /id, /check"
)


# ---------------------------------------------------------------- доступ

def allowed(message_or_call):
    user = message_or_call.from_user
    if user.id in ALLOWED_USERS:
        return True
    bot.send_message(
        user.id,
        "Этот бот приватный.\n"
        f"Ваш ID: <code>{user.id}</code>\n"
        "Передайте его владельцу бота, чтобы он добавил вас в список ALLOWED_USERS.",
        parse_mode="HTML",
    )
    return False


# ------------------------------------------------------------------ отчёты

def month_report(rows, user_id=None):
    today = today_date()
    prefix = today.strftime("%Y-%m")
    selected = [r for r in rows if r["date"].startswith(prefix)]
    if user_id is not None:
        selected = [r for r in selected if r["user_id"] == str(user_id)]
    if not selected:
        return "За этот месяц записей пока нет."

    def total(status, kind):
        return sum(r["amount"] for r in selected
                   if r["status"] == status and r["kind"] == kind)

    fact_in, fact_out = total(FACT, INCOME), total(FACT, EXPENSE)
    fact_save = total(FACT, SAVING)
    plan_in, plan_out = total(PLAN, INCOME), total(PLAN, EXPENSE)
    plan_save = total(PLAN, SAVING)

    lines = [f"<b>Итоги за {today.strftime('%m.%Y')}</b>", "", "<b>Факт</b>",
             f"Доходы: {money(fact_in)}",
             f"Расходы: {money(fact_out)}",
             f"Накопления: {money(fact_save)}",
             f"Остаток: {money(fact_in - fact_out - fact_save)}"]

    if plan_in or plan_out or plan_save:
        lines += ["", "<b>План</b>",
                  f"Доходы: {money(plan_in)}",
                  f"Расходы: {money(plan_out)}",
                  f"Накопления: {money(plan_save)}",
                  f"Расходы к исполнению: {money(max(plan_out - fact_out, 0))}"]

    by_category = {}
    for r in selected:
        if r["status"] == FACT and r["kind"] == EXPENSE:
            by_category[r["category"]] = by_category.get(r["category"], 0) + r["amount"]
    if by_category:
        lines.append("\n<b>Расходы по категориям (факт)</b>")
        for category, amount in sorted(by_category.items(), key=lambda x: -x[1]):
            share = f" ({amount / fact_out * 100:.0f}%)" if fact_out else ""
            lines.append(f"• {category}: {money(amount)}{share}")

    return "\n".join(lines)


def last_entries(rows, count=10):
    if not rows:
        return "Записей пока нет."
    lines = ["<b>Последние записи</b>"]
    for r in rows[-count:][::-1]:
        sign = "+" if r["kind"] == INCOME else "−"
        mark = "🔹" if r["status"] == PLAN else ""
        comment = f" — {r['comment']}" if r["comment"] else ""
        lines.append(f"{mark}{r['date']} {sign}{money(r['amount'])} · {r['category']}"
                     f"{comment} <i>({r['user']})</i>")
    lines.append("\n🔹 — плановая операция")
    return "\n".join(lines)


# --------------------------------------------------------------- обработчики

@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Ваш Telegram ID: <code>{message.from_user.id}</code>",
                 parse_mode="HTML")


@bot.message_handler(commands=["start", "help"])
def cmd_start(message):
    if not allowed(message):
        return
    bot.send_message(
        message.chat.id,
        f"Привет, {user_name(message.from_user)}! Я веду учёт денег "
        "и складываю всё в Google-таблицу.\n\n" + HELP_TEXT,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["report"])
def cmd_report(message):
    if not allowed(message):
        return
    bot.send_message(message.chat.id, month_report(read_rows()), parse_mode="HTML")


@bot.message_handler(commands=["last"])
def cmd_last(message):
    if not allowed(message):
        return
    bot.send_message(message.chat.id, last_entries(read_rows()), parse_mode="HTML")


@bot.message_handler(commands=["undo"])
def cmd_undo(message):
    if not allowed(message):
        return
    removed = delete_last_row_of(message.from_user.id)
    bot.send_message(
        message.chat.id,
        f"Удалено: {removed}" if removed else "У вас нет записей для удаления.",
    )


@bot.message_handler(commands=["new"])
def cmd_new(message):
    if not allowed(message):
        return
    bot.send_message(message.chat.id, "Что записываем?",
                     reply_markup=kinds_keyboard())


@bot.message_handler(commands=["check"])
def cmd_check(message):
    """Проверка связи с Google-таблицей — что с ключом и открывается ли таблица."""
    if not allowed(message):
        return

    global _worksheet
    lines = []

    try:
        data = load_key_data()
        key = data.get("private_key", "")
        whole = (key.startswith("-----BEGIN PRIVATE KEY-----")
                 and key.rstrip().endswith("-----END PRIVATE KEY-----"))
        lines.append(f"Ключ взят из: {credentials_source()}")
        lines.append(f"Сервисный аккаунт: {data.get('client_email', 'не указан')}")
        lines.append(f"Проект: {data.get('project_id', 'не указан')}")
        lines.append(f"Номер ключа: {data.get('private_key_id', 'не указан')}")
        lines.append(f"Приватный ключ: {len(key)} символов, "
                     + ("целый" if whole else "ПОВРЕЖДЁН — начало или конец обрезаны"))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Ключ не прочитан ({credentials_source()}): "
                     f"{type(exc).__name__}: {exc}")

    _worksheet = None  # заставляем переподключиться
    try:
        ws = get_worksheet()
        lines.append(f"Таблица: «{_spreadsheet.title}»")
        lines.append(f"ID: {SPREADSHEET_ID}")
        lines.append(f"Лист: «{ws.title}», строк с данными: "
                     f"{max(len(ws.col_values(1)) - 1, 0)}")
        lines.append("Столбцы: " + ", ".join(_columns))
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Таблица: НЕ открывается — {type(exc).__name__}: {exc}")

    bot.send_message(message.chat.id, "\n".join(lines))


def safe_edit(text, chat_id, message_id, markup=None, parse_mode=None):
    """Правит сообщение, не падая, если текст и кнопки не изменились."""
    try:
        bot.edit_message_text(text, chat_id, message_id,
                              reply_markup=markup, parse_mode=parse_mode)
    except telebot.apihelper.ApiTelegramException as exc:
        if "not modified" not in str(exc):
            raise


# подстраховка: последний заданный вопрос мастера по каждому чату.
# основной способ — контекст в тексте сообщения, но если ответить не «ответом»,
# а обычным сообщением, бот всё равно вспомнит, о чём спрашивал
LAST_PROMPT = {}
PROMPT_TTL = dt.timedelta(minutes=30)


def ask_amount(chat_id, status, kind, dates, tail=None):
    """
    Просит сумму и категорию. Весь контекст операции лежит в тексте этого
    сообщения — пользователь отвечает на него, и бот читает контекст обратно.
    """
    bot.send_message(
        chat_id,
        render_context(status, kind, dates, tail=tail or ASK_AMOUNT),
        parse_mode="HTML",
        reply_markup=types.ForceReply(selective=False),
    )
    LAST_PROMPT[chat_id] = (
        {"status": status, "kind": kind, "dates": list(dates or [])},
        dt.datetime.now(),
    )


def recent_prompt(chat_id):
    """Контекст последнего вопроса мастера, если он ещё не протух."""
    saved = LAST_PROMPT.get(chat_id)
    if not saved:
        return None
    context, moment = saved
    if dt.datetime.now() - moment > PROMPT_TTL:
        LAST_PROMPT.pop(chat_id, None)
        return None
    return context


def save_and_report(chat_id, context, user, edit_message_id=None):
    """Пишет операцию (одну или несколько дат) и отвечает подтверждением."""
    dates = context["dates"] or [today_date()]
    repeat = ""
    if context["status"] == PLAN:
        repeat = REGULAR if len(dates) > 1 else ONCE

    entries = [{
        "kind": context["kind"],
        "status": context["status"],
        "amount": context["amount"],
        "category": context["category"],
        "comment": context.get("comment", ""),
        "date": day,
        "repeat": repeat,
    } for day in dates]

    written = add_rows(entries, user)

    sign = "+" if context["kind"] == INCOME else "−"
    text = (f"Записал: {context['status']} · {context['kind']}\n"
            f"{sign}{money(context['amount'])} · {context['category']}")
    if context.get("comment"):
        text += f" — {context['comment']}"
    if len(dates) == 1:
        text += f"\nДата: {dates[0].strftime('%d.%m.%Y')}"
    else:
        text += (f"\nДат: {len(dates)} — "
                 + ", ".join(d.strftime("%d.%m") for d in dates[:6])
                 + (" …" if len(dates) > 6 else ""))
        text += f"\nИтого по плану: {money(context['amount'] * len(dates))}"
    if written:
        text += f"\nВ таблице: {written}"

    if edit_message_id:
        bot.edit_message_text(text, chat_id, edit_message_id)
    else:
        bot.send_message(chat_id, text)


@bot.message_handler(content_types=["text"])
def on_text(message):
    if not allowed(message):
        return

    text = message.text.strip()

    if text.startswith("➕"):
        return cmd_new(message)
    if text.startswith("📊"):
        return cmd_report(message)
    if text.startswith("👤"):
        bot.send_message(
            message.chat.id,
            month_report(read_rows(), user_id=message.from_user.id),
            parse_mode="HTML",
        )
        return
    if text.startswith("🧾"):
        return cmd_last(message)
    if text.startswith("↩️"):
        return cmd_undo(message)
    if text.startswith("❓"):
        bot.send_message(message.chat.id, HELP_TEXT, parse_mode="HTML")
        return

    # ответ на сообщение мастера — тип, статус и даты берём оттуда
    wizard = None
    if message.reply_to_message:
        wizard = parse_context(message.reply_to_message.text or "")
    if wizard is None:
        wizard = recent_prompt(message.chat.id)

    parsed = parse_entry(text, kind_hint=wizard["kind"] if wizard else None)
    if parsed is None:
        hint = ("Не понял 🤔 Начните сообщение с суммы, например "
                "<code>450 продукты</code>.")
        bot.send_message(message.chat.id, hint, parse_mode="HTML")
        return

    if wizard:
        parsed["kind"] = wizard["kind"]
        parsed["status"] = wizard["status"]
        dates = wizard["dates"] or [parsed["date"]]
        LAST_PROMPT.pop(message.chat.id, None)  # вопрос отработан
    else:
        dates = [parsed["date"]]

    context = {
        "kind": parsed["kind"],
        "status": parsed["status"],
        "amount": parsed["amount"],
        "category": parsed["category"],
        "comment": parsed["comment"],
        "dates": dates,
    }

    if context["category"] is None:
        bot.send_message(
            message.chat.id,
            render_context(context["status"], context["kind"], dates,
                           context["amount"], context["comment"],
                           "Выберите категорию:"),
            reply_markup=categories_keyboard(context["kind"]),
        )
        return

    save_and_report(message.chat.id, context, message.from_user)


# ----- шаги мастера

@bot.callback_query_handler(func=lambda call: call.data.startswith("w|"))
def on_wizard(call):
    if not allowed(call):
        return

    parts = call.data.split("|")
    step, tag = parts[1], parts[2]
    kind = KIND_BY_TAG[tag]

    if step == "k":
        bot.edit_message_text(f"{kind}. Это план или уже свершившийся факт?",
                              call.message.chat.id, call.message.message_id,
                              reply_markup=status_keyboard(tag))

    elif step == "s":
        status = PLAN if parts[3] == "P" else FACT
        if status == FACT:
            bot.edit_message_text(f"{FACT} · {kind}", call.message.chat.id,
                                  call.message.message_id)
            # дату не фиксируем: её можно указать первым словом в ответе
            ask_amount(call.message.chat.id, FACT, kind, None,
                       "Напишите сумму и категорию. Если операция не сегодняшняя — "
                       "дату первым словом: <code>вчера 450 продукты</code>")
        else:
            bot.edit_message_text(f"{PLAN} · {kind}. Операция разовая или повторяется?",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=repeat_keyboard(tag))

    elif step == "r":
        if parts[3] == "1":
            bot.edit_message_text(f"{PLAN} · {kind} · {ONCE}", call.message.chat.id,
                                  call.message.message_id)
            ask_amount(call.message.chat.id, PLAN, kind, None,
                       "Напишите сумму и категорию. Дату планируемой операции — "
                       "первым словом: <code>12.09 5000 отпуск</code>")
        else:
            today = today_date()
            safe_edit(render_context(PLAN, kind, [], tail=CALENDAR_HINT),
                      call.message.chat.id, call.message.message_id,
                      calendar_keyboard(today.year, today.month, set()))

    bot.answer_callback_query(call.id)


# ----- календарь

def expand_monthly(selected):
    result = set(selected)
    for day in selected:
        month, year = day.month, day.year
        while month < 12:
            month += 1
            last = calendar.monthrange(year, month)[1]
            result.add(dt.date(year, month, min(day.day, last)))
    return result


def expand_weekly(selected):
    result = set(selected)
    for day in selected:
        for step in range(1, 8):
            result.add(day + dt.timedelta(days=7 * step))
    return result


@bot.callback_query_handler(func=lambda call: call.data.startswith("c|"))
def on_calendar(call):
    if not allowed(call):
        return

    parts = call.data.split("|")
    action = parts[1]

    if action == "nop":
        bot.answer_callback_query(call.id)
        return

    context = parse_context(call.message.text or "")
    if context is None:
        bot.answer_callback_query(call.id, "Начните заново: «Новая операция»")
        return

    selected = set(context["dates"])
    today = today_date()
    shown = min(selected) if selected else today
    year, month = shown.year, shown.month
    note = None

    if action == "d":
        stamp = parts[2]
        day = dt.date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
        year, month = day.year, day.month
        if day in selected:
            selected.discard(day)
        elif len(selected) >= MAX_DATES:
            note = f"Больше {MAX_DATES} дат за раз — многовато"
        else:
            selected.add(day)

    elif action == "m":
        year, month = int(parts[2][:4]), int(parts[2][4:6])

    elif action == "q":
        if not selected:
            note = "Сначала отметьте хотя бы одну дату"
        else:
            grown = expand_monthly(selected) if parts[2] == "m" else expand_weekly(selected)
            if len(grown) > MAX_DATES:
                note = f"Получилось больше {MAX_DATES} дат — не стал добавлять"
            else:
                selected = grown

    elif action == "x":
        selected = set()

    elif action == "ok":
        if not selected:
            bot.answer_callback_query(call.id, "Не отмечено ни одной даты")
            return
        ordered = sorted(selected)
        bot.edit_message_text(
            f"{PLAN} · {context['kind']} · {REGULAR}, дат: {len(ordered)}",
            call.message.chat.id, call.message.message_id)
        ask_amount(call.message.chat.id, PLAN, context["kind"], ordered)
        bot.answer_callback_query(call.id)
        return

    ordered = sorted(selected)
    safe_edit(render_context(PLAN, context["kind"], ordered, tail=CALENDAR_HINT),
              call.message.chat.id, call.message.message_id,
              calendar_keyboard(year, month, selected))
    bot.answer_callback_query(call.id, note or "")


# ----- выбор категории

@bot.callback_query_handler(func=lambda call: call.data.startswith("cat|"))
def on_category(call):
    if not allowed(call):
        return

    context = parse_context(call.message.text or "")
    if context is None or context["amount"] is None:
        bot.answer_callback_query(call.id, "Начните заново")
        return

    context["category"] = CATEGORIES[context["kind"]][int(call.data.split("|")[1])]
    save_and_report(call.message.chat.id, context, call.from_user,
                    edit_message_id=call.message.message_id)
    bot.answer_callback_query(call.id, "Готово")


def error_guard(handler):
    """Оборачивает обработчики, чтобы бот не падал из-за ошибок Google API."""
    def wrapper(message_or_call, *args, **kwargs):
        try:
            return handler(message_or_call, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка в обработчике")
            chat = getattr(getattr(message_or_call, "message", None), "chat", None)
            chat_id = chat.id if chat else message_or_call.chat.id
            bot.send_message(chat_id, f"Что-то пошло не так: {exc}")
    return wrapper


for h in bot.message_handlers + bot.callback_query_handlers:
    h["function"] = error_guard(h["function"])


if __name__ == "__main__":
    log.info("Бот запущен. Разрешённые пользователи: %s", ALLOWED_USERS or "никого")
    bot.infinity_polling(skip_pending=True)
