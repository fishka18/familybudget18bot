"""
Телеграм-бот для учёта расходов, доходов и накоплений.
Все записи складываются в Google-таблицу.

Запуск:  python bot.py
Настройки берутся из файла .env (см. .env.example)
"""

import os
import re
import json
import base64
import logging
import calendar as cal
import datetime as dt
from html import escape
from zoneinfo import ZoneInfo

import gspread
import telebot
from telebot import types
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

# ---------------------------------------------------------------- настройки

load_dotenv()


def env(name, default=""):
    """
    Значение переменной окружения без мусора по краям.
    На хостингах в панель нередко попадают кавычки и пробелы — Telegram и Google
    из-за одного лишнего символа отвечают «Unauthorized», поэтому чистим сразу.
    """
    value = os.getenv(name, default).strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


BOT_TOKEN = env("BOT_TOKEN")
SPREADSHEET_ID = env("SPREADSHEET_ID")
CREDENTIALS_FILE = env("GOOGLE_CREDENTIALS_FILE", "credentials.json")
# на хостинге ключ удобнее держать не файлом, а переменной окружения:
# сюда кладётся всё содержимое credentials.json (или оно же в base64)
CREDENTIALS_JSON = env("GOOGLE_CREDENTIALS_JSON")
WORKSHEET_NAME = env("WORKSHEET_NAME", "Операции")
TIMEZONE = env("TIMEZONE", "Europe/Moscow")
CURRENCY = env("CURRENCY", "₽")

# токен иногда копируют вместе со словом bot или с адресом api.telegram.org
BOT_TOKEN = re.sub(r"^(https?://)?(api\.telegram\.org/)?bot", "", BOT_TOKEN)

# кто может пользоваться ботом (id через запятую). Пусто = никто, бот подскажет id
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", env("ALLOWED_USERS"))}

EXPENSE, INCOME, SAVING = "Расход", "Доход", "Накопления"

# Категории — ровно как на листе «Справочники» бюджетной таблицы.
# Формат: (название, тип, группа). Группа нужна только расходам.
CATEGORIES = [
    ("Продукты", EXPENSE, "Постоянные"),
    ("Еда вне дома", EXPENSE, "Постоянные"),
    ("Такси и транспорт", EXPENSE, "Постоянные"),
    ("Связь", EXPENSE, "Постоянные"),
    ("Подписки", EXPENSE, "Постоянные"),
    ("Коммунальные платежи и аренда", EXPENSE, "Постоянные"),
    ("Ипотека", EXPENSE, "Постоянные"),
    ("Рассрочки и кредиты", EXPENSE, "Постоянные"),
    ("Налоги, страховки", EXPENSE, "Постоянные"),
    ("Логопед", EXPENSE, "Постоянные"),
    ("Кружки и развлечения ребёнка", EXPENSE, "Постоянные"),
    ("Подарки", EXPENSE, "Постоянные"),
    ("Вредные привычки", EXPENSE, "Постоянные"),
    ("Одежда и обувь ребёнка", EXPENSE, "Переменные"),
    ("Одежда и обувь взрослых", EXPENSE, "Переменные"),
    ("Здоровье ребёнка", EXPENSE, "Переменные"),
    ("Здоровье взрослых", EXPENSE, "Переменные"),
    ("Игрушки и книжки", EXPENSE, "Переменные"),
    ("Бытовая химия и дом", EXPENSE, "Переменные"),
    ("Техника и электроника", EXPENSE, "Переменные"),
    ("Уходовая косметика", EXPENSE, "Переменные"),
    ("Декоративная косметика", EXPENSE, "Переменные"),
    ("Услуги по уходу", EXPENSE, "Переменные"),
    ("Развлечения", EXPENSE, "Переменные"),
    ("Аванс (Ира)", INCOME, ""),
    ("Зарплата (Ира)", INCOME, ""),
    ("Прочий доход", INCOME, ""),
    ("Накопительный счёт", SAVING, "Подушка"),
    ("ПДС", SAVING, "Инвестиции"),
    ("ИИС", SAVING, "Инвестиции"),
    ("БС", SAVING, "Инвестиции"),
]

CATEGORY_NAMES = [c[0] for c in CATEGORIES]
EXPENSE_GROUPS = ["Постоянные", "Переменные"]

# Счета — как в справочнике «Счёт / куда»
ACCOUNTS = [
    "Карта (Ира)", "Карта (Илья)", "Наличные",
    "Накопительный счёт", "ИИС", "БС", "ПДС",
]
DEFAULT_ACCOUNT = env("DEFAULT_ACCOUNT", "Карта (Ира)")

# у кого какой счёт по умолчанию: telegram id -> название счёта
# в .env пишется как ACCOUNT_BY_USER=311328289:Карта (Ира),12345:Карта (Илья)
ACCOUNT_BY_USER = {}
for pair in env("ACCOUNT_BY_USER").split(","):
    if ":" in pair:
        uid, acc = pair.split(":", 1)
        if uid.strip().isdigit():
            ACCOUNT_BY_USER[int(uid.strip())] = acc.strip()

# короткие слова, которые бот понимает как категорию при быстром вводе строкой
ALIASES = {
    "еда": "Продукты", "магазин": "Продукты", "супермаркет": "Продукты",
    "кафе": "Еда вне дома", "ресторан": "Еда вне дома", "обед": "Еда вне дома",
    "кофе": "Еда вне дома",
    "такси": "Такси и транспорт", "метро": "Такси и транспорт",
    "бензин": "Такси и транспорт", "транспорт": "Такси и транспорт",
    "жкх": "Коммунальные платежи и аренда", "квартира": "Коммунальные платежи и аренда",
    "аренда": "Коммунальные платежи и аренда", "коммуналка": "Коммунальные платежи и аренда",
    "интернет": "Связь", "телефон": "Связь", "мобильный": "Связь",
    "аптека": "Здоровье взрослых", "врач": "Здоровье взрослых",
    "лекарства": "Здоровье взрослых",
    "садик": "Кружки и развлечения ребёнка", "школа": "Кружки и развлечения ребёнка",
    "кружок": "Кружки и развлечения ребёнка",
    "игрушки": "Игрушки и книжки", "книжки": "Игрушки и книжки",
    "химия": "Бытовая химия и дом", "дом": "Бытовая химия и дом",
    "техника": "Техника и электроника", "электроника": "Техника и электроника",
    "одежда": "Одежда и обувь взрослых", "обувь": "Одежда и обувь взрослых",
    "косметика": "Уходовая косметика",
    "кредит": "Рассрочки и кредиты", "рассрочка": "Рассрочки и кредиты",
    "налоги": "Налоги, страховки", "страховка": "Налоги, страховки",
    "зп": "Зарплата (Ира)", "зарплата": "Зарплата (Ира)", "аванс": "Аванс (Ира)",
    "накопления": "Накопительный счёт", "копилка": "Накопительный счёт",
}

# --- структура листа «Операции» в бюджетной таблице ---
# A Дата · B Категория · C Тип · D Группа · E Статус · F Счёт · G Сумма
# H Комментарий · I Месяц · J День нед. · K Год · L Месяц (выбор) · M День (выбор)
# Часть колонок — формулы. Бот их не заполняет: он копирует строку-образец,
# и формулы приходят вместе с ней, пересчитавшись на новую строку.
BUDGET_COLS = 13
HEADER_ROW = int(env("HEADER_ROW", "2"))      # строка с заголовками
FIRST_DATA_ROW = HEADER_ROW + 1

COL_DATE, COL_CATEGORY, COL_TYPE, COL_GROUP = 1, 2, 3, 4
COL_STATUS, COL_ACCOUNT, COL_AMOUNT, COL_COMMENT = 5, 6, 7, 8
COL_MONTH, COL_WEEKDAY, COL_YEAR, COL_MONTH_PICK, COL_DAY_PICK = 9, 10, 11, 12, 13

STATUS_FACT, STATUS_PLAN = "Факт", "План"

# сортировать журнал по дате после каждой записи, чтобы новые факты
# вставали между плановыми строками, а не копились внизу
SORT_AFTER_ADD = env("SORT_AFTER_ADD", "да").lower() not in ("нет", "no", "false", "0")

# скрытый лист в той же таблице: кто и когда что внёс через бота.
# В самом журнале операций автора нет, а для «Мои итоги» и /undo он нужен.
LOG_SHEET = env("LOG_SHEET", "Журнал бота")
LOG_HEADERS = ["Дата", "Время", "Пользователь", "user_id",
               "Тип", "Категория", "Счёт", "Сумма", "Комментарий"]

MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

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

# ------------------------------------------------------------ google sheets

_worksheet = None
_log_sheet = None

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


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


def get_worksheet():
    """Лист «Операции» бюджетной таблицы. Он должен существовать — не создаём."""
    global _worksheet
    if _worksheet is None:
        spreadsheet = gspread.authorize(load_credentials()).open_by_key(SPREADSHEET_ID)
        _worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    return _worksheet


def get_log_sheet():
    """
    Скрытый лист с историей записей бота.
    В самом журнале операций автора нет, а для «Мои итоги» и /undo он нужен.
    """
    global _log_sheet
    if _log_sheet is not None:
        return _log_sheet

    spreadsheet = get_worksheet().spreadsheet
    try:
        ws = spreadsheet.worksheet(LOG_SHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=LOG_SHEET, rows=1000,
                                       cols=len(LOG_HEADERS))
        ws.append_row(LOG_HEADERS, value_input_option="USER_ENTERED")
        try:
            spreadsheet.batch_update({"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": ws.id, "hidden": True},
                "fields": "hidden",
            }}]})
        except Exception:  # noqa: BLE001
            log.warning("не удалось скрыть лист «%s»", LOG_SHEET)

    _log_sheet = ws
    return ws


def parse_sheet_date(text):
    """'24.08.2026' или '2026-08-24' -> date, иначе None."""
    text = str(text).strip()
    match = re.match(r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})$", text)
    if match:
        day, month, year = (int(x) for x in match.groups())
    else:
        match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
        if not match:
            return None
        year, month, day = (int(x) for x in match.groups())
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None


def parse_money(text):
    """'5 501', '1 234,50', 450 -> число. Пусто или мусор -> None."""
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^\d,.\-]", "", str(text)).replace(",", ".")
    if not re.search(r"\d", cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def category_info(name):
    """(тип, группа) по названию категории."""
    for cat_name, kind, group in CATEGORIES:
        if cat_name == name:
            return kind, group
    return "", ""


def find_template_row(values):
    """
    Номер строки-образца: последняя нормальная строка журнала.
    С неё копируются формулы, форматы и выпадающие списки.
    """
    for index in range(len(values) - 1, FIRST_DATA_ROW - 2, -1):
        row = list(values[index]) + [""] * BUDGET_COLS
        if (row[COL_CATEGORY - 1].strip()
                and row[COL_STATUS - 1].strip() in (STATUS_FACT, STATUS_PLAN)):
            return index + 1
    return None


def sort_by_date(ws, last_row):
    """Сортирует журнал по дате, чтобы новые факты встали на своё место."""
    if last_row <= FIRST_DATA_ROW:
        return
    ws.spreadsheet.batch_update({"requests": [{"sortRange": {
        "range": {
            "sheetId": ws.id,
            "startRowIndex": FIRST_DATA_ROW - 1, "endRowIndex": last_row,
            "startColumnIndex": 0, "endColumnIndex": BUDGET_COLS,
        },
        "sortSpecs": [{"dimensionIndex": 0, "sortOrder": "ASCENDING"}],
    }}]})


def add_row(kind, amount, category, comment, user, when, account):
    """
    Дописывает операцию в журнал бюджета со статусом «Факт».
    Строка сначала копируется с образца — так приезжают формулы «авто»-колонок,
    форматирование и выпадающие списки, — а потом заполняются только те ячейки,
    где в образце стояло обычное значение, а не формула.
    """
    ws = get_worksheet()
    values = ws.get_all_values()
    new_row = max(len(values), HEADER_ROW) + 1

    if ws.row_count < new_row:
        ws.add_rows(new_row - ws.row_count + 50)

    template = find_template_row(values)
    formulas = [""] * BUDGET_COLS
    if template:
        ws.spreadsheet.batch_update({"requests": [{"copyPaste": {
            "source": {
                "sheetId": ws.id,
                "startRowIndex": template - 1, "endRowIndex": template,
                "startColumnIndex": 0, "endColumnIndex": BUDGET_COLS,
            },
            "destination": {
                "sheetId": ws.id,
                "startRowIndex": new_row - 1, "endRowIndex": new_row,
                "startColumnIndex": 0, "endColumnIndex": BUDGET_COLS,
            },
            "pasteType": "PASTE_NORMAL",
        }}]})
        got = ws.get(f"A{template}:M{template}", value_render_option="FORMULA")
        if got:
            formulas = (list(got[0]) + [""] * BUDGET_COLS)[:BUDGET_COLS]

    kind_name, group = category_info(category)
    computed = {
        COL_DATE: when.strftime("%d.%m.%Y"),
        COL_CATEGORY: category,
        COL_TYPE: kind_name or kind,
        COL_GROUP: group,
        COL_STATUS: STATUS_FACT,
        COL_ACCOUNT: account,
        COL_AMOUNT: amount,
        COL_COMMENT: comment,
        COL_MONTH: f"{MONTHS_RU[when.month - 1]} {when.year}",
        COL_WEEKDAY: WEEKDAYS_RU[when.weekday()],
        COL_YEAR: when.year,
        COL_MONTH_PICK: MONTHS_RU[when.month - 1],
        COL_DAY_PICK: when.day,
    }

    updates = []
    for col, value in computed.items():
        if str(formulas[col - 1]).startswith("="):
            continue  # в образце формула — она уже скопирована и посчитает сама
        updates.append({"range": rowcol_to_a1(new_row, col), "values": [[value]]})
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    if SORT_AFTER_ADD:
        sort_by_date(ws, new_row)

    write_log(user, kind, amount, category, account, comment, when)


def write_log(user, kind, amount, category, account, comment, when):
    """История записей бота. Запись в бюджет из-за неё ломаться не должна."""
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    try:
        get_log_sheet().append_row(
            [
                when.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
                user_name(user), str(user.id),
                kind, category, account, amount, comment,
            ],
            value_input_option="USER_ENTERED",
        )
    except Exception:  # noqa: BLE001
        log.exception("не удалось записать в «%s»", LOG_SHEET)


def read_rows():
    """Строки журнала операций в виде словарей."""
    values = get_worksheet().get_all_values()
    rows = []
    for raw in values[FIRST_DATA_ROW - 1:]:
        row = list(raw) + [""] * BUDGET_COLS
        date = parse_sheet_date(row[COL_DATE - 1])
        amount = parse_money(row[COL_AMOUNT - 1])
        if date is None or amount is None:
            continue
        rows.append({
            "date": date,
            "category": row[COL_CATEGORY - 1].strip(),
            "kind": row[COL_TYPE - 1].strip(),
            "status": row[COL_STATUS - 1].strip(),
            "account": row[COL_ACCOUNT - 1].strip(),
            "amount": amount,
            "comment": row[COL_COMMENT - 1].strip(),
        })
    return rows


def read_my_rows(user_id):
    """Записи, сделанные этим человеком через бота."""
    try:
        values = get_log_sheet().get_all_values()
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for raw in values[1:]:
        row = list(raw) + [""] * len(LOG_HEADERS)
        if row[3].strip() != str(user_id):
            continue
        date = parse_sheet_date(row[0])
        amount = parse_money(row[7])
        if date is None or amount is None:
            continue
        rows.append({
            "date": date, "kind": row[4].strip(), "category": row[5].strip(),
            "account": row[6].strip(), "amount": amount,
            "comment": row[8].strip(), "status": STATUS_FACT,
        })
    return rows


def delete_last_row_of(user_id):
    """
    Убирает последнюю запись этого человека и возвращает текст для ответа.
    Строку в «Операциях» ищем по значениям, а не по номеру: журнал сортируется,
    и номера строк со временем разъезжаются.
    """
    log_ws = get_log_sheet()
    log_values = log_ws.get_all_values()

    for index in range(len(log_values) - 1, 0, -1):
        entry = list(log_values[index]) + [""] * len(LOG_HEADERS)
        if entry[3].strip() != str(user_id):
            continue

        date = parse_sheet_date(entry[0])
        category = entry[5].strip()
        account = entry[6].strip()
        comment = entry[8].strip()
        amount = parse_money(entry[7])

        ws = get_worksheet()
        values = ws.get_all_values()
        for j in range(len(values) - 1, FIRST_DATA_ROW - 2, -1):
            row = list(values[j]) + [""] * BUDGET_COLS
            if (row[COL_STATUS - 1].strip() == STATUS_FACT
                    and row[COL_CATEGORY - 1].strip() == category
                    and row[COL_ACCOUNT - 1].strip() == account
                    and row[COL_COMMENT - 1].strip() == comment
                    and parse_money(row[COL_AMOUNT - 1]) == amount
                    and parse_sheet_date(row[COL_DATE - 1]) == date):
                ws.delete_rows(j + 1)
                log_ws.delete_rows(index + 1)
                return (f"Удалено: {money(amount)} — {category} "
                        f"({date.strftime('%d.%m.%Y')})")

        log_ws.delete_rows(index + 1)
        return ("Эту запись в «Операциях» уже удалили или поправили вручную — "
                "убрал её только из истории бота.")

    return "У вас нет записей, сделанных через бота."


# -------------------------------------------------------------- разбор ввода

AMOUNT_RE = re.compile(r"^([+\-])?(\d+(?:[.,]\d+)?)\s*(к|k|тыс)?$", re.IGNORECASE)


def parse_amount(token):
    """'1500' -> 1500.0, '1,5к' -> 1500.0, '+300' -> 300.0. Иначе None."""
    token = re.sub(r"[\s ]", "", token)
    match = AMOUNT_RE.match(token)
    if not match:
        return None
    sign, number, thousands = match.groups()
    value = float(number.replace(",", "."))
    if thousands:
        value *= 1000
    if value <= 0:
        return None
    return round(value, 2)


def normalize(text):
    return re.sub(r"[^a-zа-яё0-9]", "", text.lower())


def match_category(word, kind):
    """Ищет категорию нужного типа по началу слова. Не нашёл — None."""
    key = normalize(word)
    if not key:
        return None
    if key in ALIASES:
        found = ALIASES[key]
        if category_kind(found) == kind:
            return found
    for name, cat_kind, _ in CATEGORIES:
        if cat_kind != kind:
            continue
        cat_key = normalize(name)
        if cat_key.startswith(key) or key.startswith(cat_key):
            return name
    return None


def category_kind(name):
    for cat_name, kind, _ in CATEGORIES:
        if cat_name == name:
            return kind
    return EXPENSE


DATE_WORDS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
DATE_RE = re.compile(r"^(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?$")


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

    # год не указан, а дата получилась в будущем — значит, это прошлый год
    if year is None and result > today + dt.timedelta(days=1):
        try:
            result = dt.date(candidate_year - 1, month, day)
        except ValueError:
            return None
    return result


def parse_entry(text, today=None):
    """
    Быстрый ввод строкой: '450 продукты обед', '+70000 зарплата',
    'вчера 450 продукты'. Возвращает (kind, amount, category|None, comment, date).
    """
    if today is None:
        today = today_date()

    parts = text.strip().split()
    if not parts:
        return None

    when = parse_date(parts[0], today)
    if when is not None:
        rest = parts[1:]
        if rest and parse_amount(rest[0]) is not None:
            parts = rest
        else:
            # первое слово похоже на дату, но суммы за ним нет —
            # значит это была сумма вроде 12.08 (12 рублей 8 копеек)
            when = None
    if when is None:
        when = today

    amount = parse_amount(parts[0])
    if amount is None:
        return None

    kind = INCOME if parts[0].startswith("+") else EXPENSE
    category = match_category(parts[1], kind) if len(parts) > 1 else None
    comment = " ".join(parts[2:]) if len(parts) > 2 else ""
    return kind, amount, category, comment, when


def money(value):
    text = f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} {CURRENCY}"


def user_name(user):
    name = " ".join(filter(None, [user.first_name, user.last_name])).strip()
    return name or (user.username or str(user.id))


def today_date():
    return dt.datetime.now(ZoneInfo(TIMEZONE)).date()


def human_date(when):
    today = today_date()
    if when == today:
        return f"{when.strftime('%d.%m.%Y')} · сегодня"
    if when == today - dt.timedelta(days=1):
        return f"{when.strftime('%d.%m.%Y')} · вчера"
    return f"{when.strftime('%d.%m.%Y')} · {WEEKDAYS_RU[when.weekday()]}"


# ------------------------------------------------------------------ отчёты

def month_report(rows, title="Итоги за"):
    """Свод по месяцу. Считаются только строки со статусом «Факт»."""
    today = dt.datetime.now(ZoneInfo(TIMEZONE)).date()
    fact = [r for r in rows
            if r["status"] == STATUS_FACT
            and r["date"].year == today.year and r["date"].month == today.month]
    plan = [r for r in rows
            if r["status"] == STATUS_PLAN
            and r["date"].year == today.year and r["date"].month == today.month]

    if not fact:
        return f"{title} {MONTHS_RU[today.month - 1]}: фактических записей пока нет."

    income = sum(r["amount"] for r in fact if r["kind"] == INCOME)
    expense = sum(r["amount"] for r in fact if r["kind"] == EXPENSE)
    saving = sum(r["amount"] for r in fact if r["kind"] == SAVING)

    by_category = {}
    for r in fact:
        if r["kind"] == EXPENSE:
            by_category[r["category"]] = by_category.get(r["category"], 0) + r["amount"]

    lines = [
        f"<b>{title} {MONTHS_RU[today.month - 1]} {today.year}</b>",
        f"Доходы: {money(income)}",
        f"Расходы: {money(expense)}",
        f"Отложено: {money(saving)}",
        f"Остаток: {money(income - expense - saving)}",
    ]
    if plan:
        planned = sum(r["amount"] for r in plan if r["kind"] == EXPENSE)
        lines.append(f"\nВ планах на остаток месяца: {money(planned)}")
    if by_category:
        lines.append("\n<b>Расходы по категориям</b>")
        for category, total in sorted(by_category.items(), key=lambda x: -x[1]):
            share = f" ({total / expense * 100:.0f}%)" if expense else ""
            lines.append(f"• {escape(category)}: {money(total)}{share}")
    return "\n".join(lines)


def last_entries(rows, count=10):
    """Последние фактические операции журнала."""
    fact = [r for r in rows if r["status"] == STATUS_FACT]
    if not fact:
        return "Фактических записей пока нет."
    signs = {INCOME: "+", EXPENSE: "−", SAVING: "→"}
    lines = ["<b>Последние записи</b>"]
    for r in fact[-count:][::-1]:
        sign = signs.get(r["kind"], "−")
        comment = f" — {escape(r['comment'])}" if r["comment"] else ""
        account = f" · {escape(r['account'])}" if r["account"] else ""
        lines.append(f"{r['date'].strftime('%d.%m.%Y')} {sign}{money(r['amount'])} · "
                     f"{escape(r['category'])}{account}{comment}")
    return "\n".join(lines)


# --------------------------------------------------------------- черновики

# что пользователь сейчас заполняет: user_id -> черновик записи
DRAFTS = {}

KIND_ICONS = {EXPENSE: "💸", INCOME: "💰", SAVING: "🏦"}


def new_draft(user, chat_id):
    draft = {
        "kind": None,
        "amount": None,
        "category": None,
        "account": ACCOUNT_BY_USER.get(user.id, DEFAULT_ACCOUNT),
        "date": today_date(),
        "comment": "",
        "chat_id": chat_id,
        "message_id": None,
        "awaiting": None,     # "amount" | "comment" | None
    }
    DRAFTS[user.id] = draft
    return draft


def draft_of(user_id):
    return DRAFTS.get(user_id)


def drop_draft(user_id):
    DRAFTS.pop(user_id, None)


# --------------------------------------------------------------- клавиатуры

def btn(text, data):
    return types.InlineKeyboardButton(text, callback_data=data)


def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Новая запись")
    kb.row("📊 Итоги за месяц", "👤 Мои итоги")
    kb.row("🧾 Последние записи", "↩️ Удалить последнюю")
    kb.row("❓ Как записывать")
    return kb


def kind_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(btn("💸 Расход", "kind|0"),
           btn("💰 Доход", "kind|1"),
           btn("🏦 Накопление", "kind|2"))
    kb.add(btn("✖️ Отмена", "cancel"))
    return kb


def group_keyboard(back="kind"):
    """Для расходов сначала спрашиваем группу — иначе 24 кнопки в одном экране."""
    kb = types.InlineKeyboardMarkup(row_width=2)
    for i, group in enumerate(EXPENSE_GROUPS):
        count = sum(1 for _, k, g in CATEGORIES if k == EXPENSE and g == group)
        kb.add(btn(f"{group} · {count}", f"group|{i}"))
    kb.add(btn("‹ Назад", f"back|{back}"), btn("✖️ Отмена", "cancel"))
    return kb


def category_keyboard(kind, group=None, back="kind"):
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        btn(name, f"cat|{i}")
        for i, (name, cat_kind, cat_group) in enumerate(CATEGORIES)
        if cat_kind == kind and (group is None or cat_group == group)
    ]
    kb.add(*buttons)
    kb.add(btn("‹ Назад", f"back|{back}"), btn("✖️ Отмена", "cancel"))
    return kb


def account_keyboard(current):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(*[
        btn(("● " if name == current else "") + name, f"acc|{i}")
        for i, name in enumerate(ACCOUNTS)
    ])
    kb.add(btn("‹ Назад к записи", "back|card"))
    return kb


def date_keyboard(when):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(btn("Сегодня", "day|0"), btn("Вчера", "day|1"), btn("Позавчера", "day|2"))
    kb.add(btn(f"📅 Календарь · {MONTHS_RU[when.month - 1]}", f"cal|{when.year}|{when.month}"))
    kb.add(btn("‹ Назад к записи", "back|card"))
    return kb


def calendar_keyboard(year, month, chosen):
    kb = types.InlineKeyboardMarkup(row_width=7)
    today = today_date()

    prev_month = dt.date(year, month, 1) - dt.timedelta(days=1)
    next_month = dt.date(year, month, cal.monthrange(year, month)[1]) + dt.timedelta(days=1)

    kb.row(
        btn("‹", f"cal|{prev_month.year}|{prev_month.month}"),
        btn(f"{MONTHS_RU[month - 1]} {year}", "noop"),
        btn("›", f"cal|{next_month.year}|{next_month.month}"),
    )
    kb.row(*[btn(d, "noop") for d in WEEKDAYS_RU])

    for week in cal.monthcalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(btn(" ", "noop"))
                continue
            date = dt.date(year, month, day)
            label = str(day)
            if date == chosen:
                label = f"[{day}]"
            elif date == today:
                label = f"·{day}·"
            row.append(btn(label, f"pick|{year}|{month}|{day}"))
        kb.row(*row)

    kb.row(btn("‹ Назад", "back|date"))
    return kb


def comment_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(btn("Без комментария", "nocomment"))
    kb.add(btn("‹ Назад к записи", "back|card"))
    return kb


def card_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(btn("✅ Сохранить", "save"))
    kb.add(btn("💰 Сумма", "edit|amount"), btn("🏷 Категория", "edit|category"))
    kb.add(btn("💳 Счёт", "edit|account"), btn("📅 Дата", "edit|date"))
    kb.add(btn("💬 Комментарий", "edit|comment"), btn("✖️ Отмена", "cancel"))
    return kb


# ------------------------------------------------------------ экраны мастера

def card_text(draft):
    icon = KIND_ICONS.get(draft["kind"], "🧾")
    lines = [
        f"{icon} <b>Черновик записи</b>",
        "",
        f"Тип: <b>{draft['kind']}</b>",
        f"Сумма: <b>{money(draft['amount'])}</b>",
        f"Категория: <b>{escape(draft['category'])}</b>",
        f"Счёт: {escape(draft['account'])}",
        f"Дата: {human_date(draft['date'])}",
        f"Комментарий: {escape(draft['comment']) if draft['comment'] else '—'}",
    ]
    return "\n".join(lines)


def show(draft, text, keyboard):
    """Перерисовывает сообщение мастера (или создаёт его в первый раз)."""
    if draft["message_id"] is None:
        sent = bot.send_message(draft["chat_id"], text, parse_mode="HTML",
                                reply_markup=keyboard)
        draft["message_id"] = sent.message_id
        return
    try:
        bot.edit_message_text(text, draft["chat_id"], draft["message_id"],
                              parse_mode="HTML", reply_markup=keyboard)
    except telebot.apihelper.ApiTelegramException:
        sent = bot.send_message(draft["chat_id"], text, parse_mode="HTML",
                                reply_markup=keyboard)
        draft["message_id"] = sent.message_id


def show_kind(draft):
    draft["awaiting"] = None
    show(draft, "Что записываем?", kind_keyboard())


def show_amount(draft):
    draft["awaiting"] = "amount"
    icon = KIND_ICONS.get(draft["kind"], "🧾")
    hint = "Например: <code>450</code>, <code>1,5к</code>, <code>2350,40</code>"
    show(draft, f"{icon} <b>{draft['kind']}</b>\n\nВведите сумму сообщением.\n{hint}",
         types.InlineKeyboardMarkup().add(btn("‹ Назад", "back|kind"),
                                          btn("✖️ Отмена", "cancel")))


def show_category(draft):
    draft["awaiting"] = None
    # если запись уже собрана, «Назад» должно возвращать в карточку, а не в начало
    back = "card" if draft["category"] else ("amount" if draft["amount"] else "kind")
    if draft["kind"] == EXPENSE:
        show(draft, f"Расход {money(draft['amount'])}. Какая группа?",
             group_keyboard(back))
    else:
        title = "Откуда доход?" if draft["kind"] == INCOME else "Куда откладываем?"
        show(draft, f"{money(draft['amount'])}. {title}",
             category_keyboard(draft["kind"], back=back))


def show_card(draft):
    draft["awaiting"] = None
    show(draft, card_text(draft), card_keyboard())


# ---------------------------------------------------------------- справка

HELP_TEXT = (
    "<b>Как записывать</b>\n\n"
    "Самый простой путь — кнопка <b>➕ Новая запись</b> или команда /add: "
    "бот проведёт по шагам (тип → сумма → категория) и покажет карточку, "
    "где можно поменять счёт, дату в календаре и комментарий.\n\n"
    "<b>Быстрый ввод строкой</b> тоже работает:\n"
    "<code>450 продукты</code>\n"
    "<code>1200 кафе обед с Аней</code>\n"
    "<code>+70000 зарплата</code>\n"
    "<code>вчера 450 продукты</code>\n"
    "<code>12.08 3200 одежда куртка</code>\n\n"
    "После быстрого ввода бот всё равно покажет карточку — сохранение "
    "в один тап, но счёт и дату видно до записи.\n\n"
    "Команды: /add, /report, /last, /undo, /id, /check"
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
        f"Привет, {escape(user_name(message.from_user))}! Я веду учёт денег "
        "и складываю всё в Google-таблицу.\n\n" + HELP_TEXT,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(commands=["add"])
def cmd_add(message):
    if not allowed(message):
        return
    draft = new_draft(message.from_user, message.chat.id)
    show_kind(draft)


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
    bot.send_message(message.chat.id, delete_last_row_of(message.from_user.id))


@bot.message_handler(commands=["check"])
def cmd_check(message):
    """Проверка связи с Google-таблицей — что с ключом и открывается ли таблица."""
    if not allowed(message):
        return

    global _worksheet
    lines = []
    service_account = "не определён"

    try:
        data = load_key_data()
        service_account = data.get("client_email", "не определён")
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

    lines.append(f"ID таблицы: {SPREADSHEET_ID} ({len(SPREADSHEET_ID)} символов)")
    lines.append(f"Лист: {WORKSHEET_NAME}")

    global _log_sheet
    _worksheet = None  # заставляем переподключиться
    _log_sheet = None
    try:
        ws = get_worksheet()
        values = ws.get_all_values()
        fact = sum(1 for r in read_rows() if r["status"] == STATUS_FACT)
        template = find_template_row(values)
        lines.append(f"Таблица: открыта, лист «{ws.title}», строк: {len(values)}, "
                     f"из них фактических операций: {fact}")
        lines.append(f"Строка-образец для новых записей: "
                     f"{template if template else 'не найдена — журнал пуст'}")
        can_write = "да"
        try:
            get_log_sheet()
        except Exception as exc:  # noqa: BLE001
            can_write = f"нет — {type(exc).__name__}: {exc}"
        lines.append(f"Лист «{LOG_SHEET}» доступен: {can_write}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"Таблица: НЕ открывается — {type(exc).__name__}: {exc}")
        if "404" in str(exc) or "NotFound" in type(exc).__name__:
            lines.append(
                "\n404 значит одно из двух: неверный ID таблицы "
                "или у сервисного аккаунта нет к ней доступа.\n"
                "Откройте таблицу → Настройки доступа → добавьте как Редактора:\n"
                f"{service_account}"
            )

    bot.send_message(message.chat.id, "\n".join(lines))


def forget_message(message):
    """Убирает из чата то, что пользователь ввёл текстом — чтобы остался мастер."""
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except telebot.apihelper.ApiTelegramException:
        pass


@bot.message_handler(content_types=["text"])
def on_text(message):
    if not allowed(message):
        return

    text = message.text.strip()

    if text.startswith("➕"):
        return cmd_add(message)
    if text.startswith("📊"):
        return cmd_report(message)
    if text.startswith("👤"):
        bot.send_message(
            message.chat.id,
            month_report(read_my_rows(message.from_user.id), "Ваши записи за"),
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

    draft = draft_of(message.from_user.id)

    # ждём сумму
    if draft and draft["awaiting"] == "amount":
        amount = parse_amount(text)
        if amount is None:
            bot.send_message(message.chat.id,
                             "Это не похоже на сумму. Например: <code>450</code>",
                             parse_mode="HTML")
            return
        forget_message(message)
        draft["amount"] = amount
        if draft["category"]:
            show_card(draft)
        else:
            show_category(draft)
        return

    # ждём комментарий
    if draft and draft["awaiting"] == "comment":
        forget_message(message)
        draft["comment"] = text[:200]
        show_card(draft)
        return

    # быстрый ввод строкой
    parsed = parse_entry(text)
    if parsed is None:
        bot.send_message(
            message.chat.id,
            "Не понял 🤔 Нажмите <b>➕ Новая запись</b> или начните сообщение "
            "с суммы: <code>450 продукты</code>.",
            parse_mode="HTML",
        )
        return

    kind, amount, category, comment, when = parsed
    draft = new_draft(message.from_user, message.chat.id)
    draft["kind"] = kind
    draft["amount"] = amount
    draft["category"] = category
    draft["comment"] = comment
    draft["date"] = when
    if category:
        show_card(draft)
    else:
        show_category(draft)


# ------------------------------------------------------------ кнопки мастера

def need_draft(call):
    draft = draft_of(call.from_user.id)
    if draft is None:
        bot.answer_callback_query(call.id, "Черновик уже закрыт — начните заново")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                          reply_markup=None)
        except telebot.apihelper.ApiTelegramException:
            pass
        return None
    draft["chat_id"] = call.message.chat.id
    draft["message_id"] = call.message.message_id
    return draft


@bot.callback_query_handler(func=lambda c: c.data == "noop")
def on_noop(call):
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "cancel")
def on_cancel(call):
    if not allowed(call):
        return
    drop_draft(call.from_user.id)
    bot.edit_message_text("Запись отменена.", call.message.chat.id,
                          call.message.message_id)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("kind|"))
def on_kind(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    draft["kind"] = [EXPENSE, INCOME, SAVING][int(call.data.split("|")[1])]
    draft["category"] = None
    show_amount(draft)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("group|"))
def on_group(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    group = EXPENSE_GROUPS[int(call.data.split("|")[1])]
    show(draft, f"Расход {money(draft['amount'])} · {group}. Категория:",
         category_keyboard(EXPENSE, group, back="category"))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cat|"))
def on_category(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    draft["category"] = CATEGORY_NAMES[int(call.data.split("|")[1])]
    if draft["amount"] is None:
        show_amount(draft)
    else:
        show_card(draft)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("acc|"))
def on_account(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    draft["account"] = ACCOUNTS[int(call.data.split("|")[1])]
    show_card(draft)
    bot.answer_callback_query(call.id, draft["account"])


@bot.callback_query_handler(func=lambda c: c.data.startswith("day|"))
def on_day(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    draft["date"] = today_date() - dt.timedelta(days=int(call.data.split("|")[1]))
    show_card(draft)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("cal|"))
def on_calendar(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    _, year, month = call.data.split("|")
    year, month = int(year), int(month)
    show(draft, "Выберите дату операции:",
         calendar_keyboard(year, month, draft["date"]))
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("pick|"))
def on_pick(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    _, year, month, day = call.data.split("|")
    draft["date"] = dt.date(int(year), int(month), int(day))
    show_card(draft)
    bot.answer_callback_query(call.id, draft["date"].strftime("%d.%m.%Y"))


@bot.callback_query_handler(func=lambda c: c.data.startswith("edit|"))
def on_edit(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    field = call.data.split("|")[1]
    if field == "amount":
        show_amount(draft)
    elif field == "category":
        show_category(draft)
    elif field == "account":
        show(draft, "С какого счёта?", account_keyboard(draft["account"]))
    elif field == "date":
        show(draft, f"Дата операции: {human_date(draft['date'])}",
             date_keyboard(draft["date"]))
    elif field == "comment":
        draft["awaiting"] = "comment"
        show(draft, "Напишите комментарий сообщением — или оставьте пустым.",
             comment_keyboard())
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "nocomment")
def on_nocomment(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    draft["comment"] = ""
    show_card(draft)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("back|"))
def on_back(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    where = call.data.split("|")[1]
    if where == "kind":
        show_kind(draft)
    elif where == "amount":
        show_amount(draft)
    elif where == "category":
        show_category(draft)
    elif where == "date":
        show(draft, f"Дата операции: {human_date(draft['date'])}",
             date_keyboard(draft["date"]))
    else:
        show_card(draft)
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda c: c.data == "save")
def on_save(call):
    if not allowed(call):
        return
    draft = need_draft(call)
    if not draft:
        return
    if not (draft["kind"] and draft["amount"] and draft["category"]):
        bot.answer_callback_query(call.id, "Не хватает данных")
        return

    add_row(draft["kind"], draft["amount"], draft["category"], draft["comment"],
            call.from_user, draft["date"], draft["account"])

    icon = KIND_ICONS.get(draft["kind"], "🧾")
    lines = [
        f"{icon} <b>Записано</b>",
        f"{money(draft['amount'])} · {escape(draft['category'])}",
        f"{escape(draft['account'])} · {human_date(draft['date'])}",
    ]
    if draft["comment"]:
        lines.append(escape(draft["comment"]))
    bot.edit_message_text("\n".join(lines), draft["chat_id"], draft["message_id"],
                          parse_mode="HTML")
    drop_draft(call.from_user.id)
    bot.answer_callback_query(call.id, "Готово")


def error_guard(handler):
    """Оборачивает обработчики, чтобы бот не падал из-за ошибок Google API."""
    def wrapper(message_or_call, *args, **kwargs):
        try:
            return handler(message_or_call, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("ошибка в обработчике")
            chat = getattr(getattr(message_or_call, "message", None), "chat", None)
            chat = chat or getattr(message_or_call, "chat", None)
            if chat is not None:
                bot.send_message(chat.id, f"Что-то пошло не так: {exc}")
    return wrapper


for h in bot.message_handlers + bot.callback_query_handlers:
    h["function"] = error_guard(h["function"])


def masked_token():
    """Как токен выглядит изнутри контейнера — без раскрытия самого токена."""
    head = BOT_TOKEN[:4] if len(BOT_TOKEN) > 8 else "?"
    tail = BOT_TOKEN[-4:] if len(BOT_TOKEN) > 8 else "?"
    return f"{len(BOT_TOKEN)} символов, {head}…{tail}"


if __name__ == "__main__":
    try:
        me = bot.get_me()
    except telebot.apihelper.ApiTelegramException as exc:
        log.error("Telegram не принял токен: %s", exc)
        log.error("Переменная BOT_TOKEN внутри контейнера: %s", masked_token())
        log.error("Правильный токен — 46 символов вида 1234567890:AA... "
                  "Проверьте, что в переменной нет кавычек и пробелов "
                  "и что она не задана второй раз где-то ещё.")
        raise SystemExit(1)

    log.info("Бот @%s запущен. Токен: %s", me.username, masked_token())
    log.info("Разрешённые пользователи: %s", ALLOWED_USERS or "никого")
    bot.infinity_polling(skip_pending=True)
