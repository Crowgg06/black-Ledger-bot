"""
Black Ledger — Telegram CRM bot.

Один бот для всей команды: каждый сотрудник пишет боту лично и добавляет
продажи, владелец видит общую статистику, и (по желанию) все новые продажи
дублируются в общий групповой чат.

Данные хранятся в одном SQLite-файле (crm.db) рядом со скриптом — ничего
дополнительно настраивать не нужно, файл просто должен сохраняться между
перезапусками (на большинстве хостингов это файл в volume/persistent disk).

Запуск: см. README.md
"""

import asyncio
import csv
import io
import logging
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or "0")
DB_PATH = os.getenv("DB_PATH", "crm.db")
RATE_DEFAULT = 45.6267

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("black-ledger")

router = Router()

# ---------------------------------------------------------------- storage --

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sales(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service TEXT NOT NULL,
                amount REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'UAH',
                sale_date TEXT,
                client TEXT,
                account TEXT,
                partner TEXT,
                commission REAL NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0,
                note TEXT,
                created_by INTEGER,
                created_by_name TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS accounts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS partners(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS employees(
                tg_id INTEGER PRIMARY KEY,
                name TEXT,
                joined_at TEXT
            )"""
        )
        defaults = {"rate": str(RATE_DEFAULT), "currency": "UAH", "commission": "0", "group_chat_id": ""}
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))


def get_setting(key, default=""):
    with closing(db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def rate():
    try:
        return float(get_setting("rate", str(RATE_DEFAULT)))
    except ValueError:
        return RATE_DEFAULT


def display_currency():
    return get_setting("currency", "UAH")


def default_commission():
    try:
        return float(get_setting("commission", "0"))
    except ValueError:
        return 0.0


def to_uah(amount, currency):
    return amount * rate() if currency == "USD" else amount


def fmt_money(uah_amount):
    cur = display_currency()
    value = uah_amount / rate() if cur == "USD" else uah_amount
    return f"{value:,.0f}".replace(",", " ") + f" {cur}"


def net_of(row):
    uah = to_uah(row["amount"], row["currency"])
    return uah - uah * (row["commission"] or 0) / 100 - (row["cost"] or 0)


def is_owner(tg_id):
    return OWNER_ID and tg_id == OWNER_ID


def ensure_employee(tg_id, name):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO employees(tg_id, name, joined_at) VALUES(?,?,?)",
            (tg_id, name, datetime.utcnow().isoformat()),
        )
        conn.execute("UPDATE employees SET name=? WHERE tg_id=?", (name, tg_id))


def list_names(table):
    with closing(db()) as conn:
        return [r["name"] for r in conn.execute(f"SELECT name FROM {table} ORDER BY name")]


# --------------------------------------------------------------- keyboards --

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def currency_kb():
    return kb([[InlineKeyboardButton(text="UAH", callback_data="cur:UAH"),
                InlineKeyboardButton(text="USD", callback_data="cur:USD")]])


def date_kb():
    return kb([[InlineKeyboardButton(text="Сегодня", callback_data="date:today")],
               [InlineKeyboardButton(text="Без даты", callback_data="date:none")],
               [InlineKeyboardButton(text="Ввести вручную (ГГГГ-ММ-ДД)", callback_data="date:manual")]])


def pick_kb(names, prefix, skip_label):
    rows = [[InlineKeyboardButton(text=n, callback_data=f"{prefix}:{n}")] for n in names]
    rows.append([InlineKeyboardButton(text=skip_label, callback_data=f"{prefix}:__skip__")])
    return kb(rows)


def main_menu_kb():
    return kb([
        [InlineKeyboardButton(text="➕ Добавить продажу", callback_data="menu:add")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats"),
         InlineKeyboardButton(text="🧾 Продажи", callback_data="menu:sales")],
        [InlineKeyboardButton(text="🧰 Команда", callback_data="menu:team"),
         InlineKeyboardButton(text="👥 Клиенты", callback_data="menu:clients")],
    ])


# ------------------------------------------------------------------ states --

class AddSale(StatesGroup):
    service = State()
    amount = State()
    currency = State()
    sale_date = State()
    sale_date_manual = State()
    client = State()
    account = State()
    partner = State()
    commission = State()
    cost = State()
    note = State()


HELP_TEXT = (
    "<b>Black Ledger — CRM в Telegram</b>\n\n"
    "/add — добавить продажу\n"
    "/stats — статистика (сегодня / неделя / месяц / всего)\n"
    "/sales — последние продажи\n"
    "/services — разбивка по услугам\n"
    "/clients — клиенты\n"
    "/accounts — список аккаунтов (+добавить/удалить)\n"
    "/partners — список партнёров (+добавить/удалить)\n"
    "/export — выгрузить CSV со всеми продажами\n"
    "/cancel — отменить текущий ввод\n"
)
OWNER_HELP = (
    "\n<b>Только для владельца:</b>\n"
    "/setrate 45.63 — курс USD→UAH\n"
    "/setcommission 10 — комиссия партнёра по умолчанию, %\n"
    "/setcurrency UAH|USD — валюта отображения сумм\n"
    "/setgroup — выполнить в групповом чате, чтобы бот дублировал туда новые продажи\n"
    "/resetdata — удалить все продажи, аккаунты и партнёров (нужно подтверждение)\n"
)


@router.message(CommandStart())
async def cmd_start(message: Message):
    ensure_employee(message.from_user.id, message.from_user.full_name)
    role = "владелец" if is_owner(message.from_user.id) else "сотрудник"
    await message.answer(
        f"Привет, {message.from_user.first_name}! Вы подключены как <b>{role}</b>.\n\n" + HELP_TEXT +
        (OWNER_HELP if is_owner(message.from_user.id) else ""),
        reply_markup=main_menu_kb(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT + (OWNER_HELP if is_owner(message.from_user.id) else ""), reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu:add")
async def menu_add(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await start_add_sale(call.message, state)


@router.callback_query(F.data == "menu:stats")
async def menu_stats(call: CallbackQuery):
    await call.answer()
    await send_stats(call.message)


@router.callback_query(F.data == "menu:sales")
async def menu_sales(call: CallbackQuery):
    await call.answer()
    await send_sales(call.message, 0)


@router.callback_query(F.data == "menu:team")
async def menu_team(call: CallbackQuery):
    await call.answer()
    await send_accounts(call.message)
    await send_partners(call.message)


@router.callback_query(F.data == "menu:clients")
async def menu_clients(call: CallbackQuery):
    await call.answer()
    await send_clients(call.message)


# ------------------------------------------------------------- add a sale --

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await start_add_sale(message, state)


async def start_add_sale(message: Message, state: FSMContext):
    await state.clear()
    recent_services = recent_service_names()
    hint = ("\nПоследние услуги: " + ", ".join(recent_services)) if recent_services else ""
    await message.answer("Новая продажа.\nВведите <b>название услуги</b>:" + hint)
    await state.set_state(AddSale.service)


def recent_service_names(limit=8):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT service, COUNT(*) c FROM sales GROUP BY service ORDER BY c DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["service"] for r in rows]


@router.message(AddSale.service)
async def add_service(message: Message, state: FSMContext):
    service = message.text.strip()
    if not service:
        await message.answer("Название не может быть пустым. Введите услугу:")
        return
    await state.update_data(service=service)
    await message.answer("Сумма продажи (число):")
    await state.set_state(AddSale.amount)


@router.message(AddSale.amount)
async def add_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", ".").strip())
        if amount < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите сумму числом, например 1500 или 1500.50:")
        return
    await state.update_data(amount=amount)
    await message.answer("Валюта:", reply_markup=currency_kb())
    await state.set_state(AddSale.currency)


@router.callback_query(AddSale.currency, F.data.startswith("cur:"))
async def add_currency(call: CallbackQuery, state: FSMContext):
    await call.answer()
    currency = call.data.split(":", 1)[1]
    await state.update_data(currency=currency)
    await call.message.answer("Дата продажи:", reply_markup=date_kb())
    await state.set_state(AddSale.sale_date)


@router.callback_query(AddSale.sale_date, F.data.startswith("date:"))
async def add_date(call: CallbackQuery, state: FSMContext):
    await call.answer()
    choice = call.data.split(":", 1)[1]
    if choice == "today":
        await state.update_data(sale_date=date.today().isoformat())
        await ask_client(call.message, state)
    elif choice == "none":
        await state.update_data(sale_date="")
        await ask_client(call.message, state)
    else:
        await call.message.answer("Введите дату в формате ГГГГ-ММ-ДД, например 2026-10-27:")
        await state.set_state(AddSale.sale_date_manual)


@router.message(AddSale.sale_date_manual)
async def add_date_manual(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        await message.answer("Неверный формат. Пример: 2026-10-27. Попробуйте ещё раз:")
        return
    await state.update_data(sale_date=text)
    await ask_client(message, state)


async def ask_client(message: Message, state: FSMContext):
    await message.answer("Имя клиента / username (или «-», если не нужно):")
    await state.set_state(AddSale.client)


@router.message(AddSale.client)
async def add_client(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.update_data(client="" if text == "-" else text)
    accounts = list_names("accounts")
    if accounts:
        await message.answer("Аккаунт:", reply_markup=pick_kb(accounts, "acc", "Не указан"))
        await state.set_state(AddSale.account)
    else:
        await state.update_data(account="")
        await ask_partner(message, state)


@router.callback_query(AddSale.account, F.data.startswith("acc:"))
async def add_account(call: CallbackQuery, state: FSMContext):
    await call.answer()
    value = call.data.split(":", 1)[1]
    await state.update_data(account="" if value == "__skip__" else value)
    await ask_partner(call.message, state)


async def ask_partner(message: Message, state: FSMContext):
    partners = list_names("partners")
    if partners:
        await message.answer("Партнёр:", reply_markup=pick_kb(partners, "ptn", "Не указан"))
        await state.set_state(AddSale.partner)
    else:
        await state.update_data(partner="")
        await ask_commission(message, state)


@router.callback_query(AddSale.partner, F.data.startswith("ptn:"))
async def add_partner(call: CallbackQuery, state: FSMContext):
    await call.answer()
    value = call.data.split(":", 1)[1]
    await state.update_data(partner="" if value == "__skip__" else value)
    await ask_commission(call.message, state)


async def ask_commission(message: Message, state: FSMContext):
    dflt = default_commission()
    await message.answer(f"Комиссия партнёра, % (или «-» для значения по умолчанию {dflt:g}%):")
    await state.set_state(AddSale.commission)


@router.message(AddSale.commission)
async def add_commission(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        commission = default_commission()
    else:
        try:
            commission = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Введите число (например 10) или «-»:")
            return
    await state.update_data(commission=commission)
    await message.answer("Расходы по продаже в грн (или «-» для 0):")
    await state.set_state(AddSale.cost)


@router.message(AddSale.cost)
async def add_cost(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "-":
        cost = 0.0
    else:
        try:
            cost = float(text.replace(",", "."))
        except ValueError:
            await message.answer("Введите число или «-»:")
            return
    await state.update_data(cost=cost)
    await message.answer("Заметка (или «-», если не нужна):")
    await state.set_state(AddSale.note)


@router.message(AddSale.note)
async def add_note(message: Message, state: FSMContext):
    text = message.text.strip()
    note = "" if text == "-" else text
    data = await state.update_data(note=note)
    await state.clear()
    await save_sale(message, data)


async def save_sale(message: Message, data: dict):
    created_at = datetime.utcnow().isoformat()
    with closing(db()) as conn, conn:
        conn.execute(
            """INSERT INTO sales(service, amount, currency, sale_date, client, account, partner,
                                  commission, cost, note, created_by, created_by_name, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["service"], data["amount"], data["currency"], data.get("sale_date") or None,
                data.get("client") or None, data.get("account") or None, data.get("partner") or None,
                data.get("commission", 0), data.get("cost", 0), data.get("note") or None,
                message.from_user.id, message.from_user.full_name, created_at,
            ),
        )
    uah = to_uah(data["amount"], data["currency"])
    await message.answer(
        f"✅ Продажа сохранена: <b>{data['service']}</b> — {fmt_money(uah)}"
        + (f"\nКлиент: {data['client']}" if data.get("client") else ""),
        reply_markup=main_menu_kb(),
    )
    await broadcast_to_group(message.bot, data, message.from_user.full_name, uah)


async def broadcast_to_group(bot: Bot, data: dict, who: str, uah_amount: float):
    chat_id = get_setting("group_chat_id", "")
    if not chat_id:
        return
    text = (
        f"💰 <b>Новая продажа</b>\n"
        f"Услуга: {data['service']}\n"
        f"Сумма: {fmt_money(uah_amount)}\n"
        + (f"Клиент: {data['client']}\n" if data.get("client") else "")
        + f"Добавил: {who}"
    )
    try:
        await bot.send_message(int(chat_id), text)
    except Exception as e:
        log.warning("Не удалось отправить в группу: %s", e)


# ---------------------------------------------------------------- stats --

def period_totals(days):
    since = (date.today() - timedelta(days=days)).isoformat()
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM sales WHERE sale_date >= ?", (since,)).fetchall()
    return rows


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await send_stats(message)


async def send_stats(message: Message):
    with closing(db()) as conn:
        all_rows = conn.execute("SELECT * FROM sales").fetchall()
    today_rows = [r for r in all_rows if r["sale_date"] == date.today().isoformat()]
    week_rows = period_totals(7)
    month_rows = period_totals(30)

    def total(rows):
        return sum(to_uah(r["amount"], r["currency"]) for r in rows)

    def net_total(rows):
        return sum(net_of(r) for r in rows)

    count = len(all_rows)
    total_rev = total(all_rows)
    avg_check = total_rev / count if count else 0
    text = (
        "<b>📊 Статистика</b>\n\n"
        f"Сегодня: {fmt_money(total(today_rows))} ({len(today_rows)} продаж)\n"
        f"За 7 дней: {fmt_money(total(week_rows))} ({len(week_rows)} продаж)\n"
        f"За 30 дней: {fmt_money(total(month_rows))} ({len(month_rows)} продаж)\n\n"
        f"Всего продаж: {count}\n"
        f"Общая выручка: {fmt_money(total_rev)}\n"
        f"Средний чек: {fmt_money(avg_check)}\n"
        f"Чистыми (после комиссий и расходов): {fmt_money(net_total(all_rows))}"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.message(Command("sales"))
async def cmd_sales(message: Message):
    await send_sales(message, 0)


def sales_page(offset, limit=10):
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM sales ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) c FROM sales").fetchone()["c"]
    return rows, total


async def send_sales(message: Message, offset: int):
    rows, total = sales_page(offset)
    if not rows:
        await message.answer("Продаж пока нет. Добавьте первую через /add.", reply_markup=main_menu_kb())
        return
    lines = []
    for r in rows:
        d = r["sale_date"] or "без даты"
        lines.append(
            f"• {d} — <b>{r['service']}</b> — {fmt_money(to_uah(r['amount'], r['currency']))}"
            + (f" — {r['client']}" if r["client"] else "")
            + f"\n   добавил {r['created_by_name']}"
        )
    text = f"<b>🧾 Продажи ({offset+1}–{offset+len(rows)} из {total})</b>\n\n" + "\n".join(lines)
    buttons = []
    if offset + len(rows) < total:
        buttons.append(InlineKeyboardButton(text="Ещё →", callback_data=f"sales:{offset+10}"))
    markup = kb([buttons]) if buttons else None
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("sales:"))
async def sales_more(call: CallbackQuery):
    await call.answer()
    offset = int(call.data.split(":", 1)[1])
    await send_sales(call.message, offset)


@router.message(Command("services"))
async def cmd_services(message: Message):
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM sales").fetchall()
    if not rows:
        await message.answer("Данных пока нет.")
        return
    agg = {}
    for r in rows:
        x = agg.setdefault(r["service"], {"count": 0, "revenue": 0.0})
        x["count"] += 1
        x["revenue"] += to_uah(r["amount"], r["currency"])
    ordered = sorted(agg.items(), key=lambda kv: -kv[1]["revenue"])
    lines = [f"• <b>{n}</b> — {fmt_money(x['revenue'])} ({x['count']} продаж, ср. чек {fmt_money(x['revenue']/x['count'])})"
             for n, x in ordered]
    await message.answer("<b>Разбивка по услугам</b>\n\n" + "\n".join(lines))


@router.message(Command("clients"))
async def cmd_clients(message: Message):
    await send_clients(message)


async def send_clients(message: Message):
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM sales WHERE client IS NOT NULL AND client != ''").fetchall()
    if not rows:
        await message.answer("Пока нет продаж с указанным клиентом.")
        return
    agg = {}
    for r in rows:
        x = agg.setdefault(r["client"], {"count": 0, "total": 0.0, "last": ""})
        x["count"] += 1
        x["total"] += to_uah(r["amount"], r["currency"])
        if (r["sale_date"] or "") > x["last"]:
            x["last"] = r["sale_date"] or ""
    ordered = sorted(agg.items(), key=lambda kv: -kv[1]["total"])[:20]
    lines = [f"• <b>{n}</b> — {x['count']} покупок, {fmt_money(x['total'])}, последняя {x['last'] or '—'}"
             for n, x in ordered]
    await message.answer("<b>👥 Клиенты</b>\n\n" + "\n".join(lines))


# ------------------------------------------------------------ accounts/partners --

@router.message(Command("accounts"))
async def cmd_accounts(message: Message):
    await send_accounts(message)


async def send_accounts(message: Message):
    names = list_names("accounts")
    text = "<b>Аккаунты</b>\n" + ("\n".join(f"• {n}" for n in names) if names else "Пока не добавлено.")
    text += "\n\nДобавить: <code>/addaccount Название</code>\nУдалить: <code>/delaccount Название</code>"
    await message.answer(text)


@router.message(Command("addaccount"))
async def cmd_addaccount(message: Message):
    name = message.text.partition(" ")[2].strip()
    if not name:
        await message.answer("Укажите название: /addaccount Название")
        return
    try:
        with closing(db()) as conn, conn:
            conn.execute("INSERT INTO accounts(name) VALUES(?)", (name,))
        await message.answer(f"Аккаунт «{name}» добавлен.")
    except sqlite3.IntegrityError:
        await message.answer("Такой аккаунт уже есть.")


@router.message(Command("delaccount"))
async def cmd_delaccount(message: Message):
    name = message.text.partition(" ")[2].strip()
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM accounts WHERE name=?", (name,))
    await message.answer(f"Удалено (если существовало): «{name}». История продаж сохранена.")


@router.message(Command("partners"))
async def cmd_partners(message: Message):
    await send_partners(message)


async def send_partners(message: Message):
    names = list_names("partners")
    text = "<b>Партнёры</b>\n" + ("\n".join(f"• {n}" for n in names) if names else "Пока не добавлено.")
    text += "\n\nДобавить: <code>/addpartner Имя</code>\nУдалить: <code>/delpartner Имя</code>"
    await message.answer(text)


@router.message(Command("addpartner"))
async def cmd_addpartner(message: Message):
    name = message.text.partition(" ")[2].strip()
    if not name:
        await message.answer("Укажите имя: /addpartner Имя")
        return
    try:
        with closing(db()) as conn, conn:
            conn.execute("INSERT INTO partners(name) VALUES(?)", (name,))
        await message.answer(f"Партнёр «{name}» добавлен.")
    except sqlite3.IntegrityError:
        await message.answer("Такой партнёр уже есть.")


@router.message(Command("delpartner"))
async def cmd_delpartner(message: Message):
    name = message.text.partition(" ")[2].strip()
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM partners WHERE name=?", (name,))
    await message.answer(f"Удалено (если существовало): «{name}». История продаж сохранена.")


# ------------------------------------------------------------------- export --

@router.message(Command("export"))
async def cmd_export(message: Message):
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM sales ORDER BY created_at").fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(["date", "service", "client", "account", "partner", "amount", "currency",
                      "commission_percent", "cost_uah", "net_uah", "added_by"])
    for r in rows:
        writer.writerow([r["sale_date"], r["service"], r["client"], r["account"], r["partner"],
                          r["amount"], r["currency"], r["commission"], r["cost"], round(net_of(r), 2),
                          r["created_by_name"]])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    await message.answer_document(BufferedInputFile(data, filename="black-ledger-sales.csv"))


# -------------------------------------------------------------- owner-only --

def owner_only(func):
    async def wrapper(message: Message, *a, **kw):
        if not is_owner(message.from_user.id):
            await message.answer("Эта команда доступна только владельцу.")
            return
        return await func(message, *a, **kw)
    return wrapper


@router.message(Command("setrate"))
@owner_only
async def cmd_setrate(message: Message):
    try:
        value = float(message.text.partition(" ")[2].strip().replace(",", "."))
        assert value > 0
    except (ValueError, AssertionError):
        await message.answer("Использование: /setrate 45.63")
        return
    set_setting("rate", value)
    await message.answer(f"Курс USD→UAH установлен: {value:g}")


@router.message(Command("setcommission"))
@owner_only
async def cmd_setcommission(message: Message):
    try:
        value = float(message.text.partition(" ")[2].strip().replace(",", "."))
        assert 0 <= value <= 100
    except (ValueError, AssertionError):
        await message.answer("Использование: /setcommission 10")
        return
    set_setting("commission", value)
    await message.answer(f"Комиссия по умолчанию: {value:g}%")


@router.message(Command("setcurrency"))
@owner_only
async def cmd_setcurrency(message: Message):
    value = message.text.partition(" ")[2].strip().upper()
    if value not in ("UAH", "USD"):
        await message.answer("Использование: /setcurrency UAH  или  /setcurrency USD")
        return
    set_setting("currency", value)
    await message.answer(f"Валюта отображения: {value}")


@router.message(Command("setgroup"))
@owner_only
async def cmd_setgroup(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        await message.answer("Выполните эту команду внутри группового чата, куда нужно дублировать продажи.")
        return
    set_setting("group_chat_id", message.chat.id)
    await message.answer("Готово — новые продажи теперь будут дублироваться в этот чат.")


@router.message(Command("resetdata"))
@owner_only
async def cmd_resetdata(message: Message):
    await message.answer(
        "Это удалит ВСЕ продажи, аккаунты и партнёров без возможности восстановления.\n"
        "Чтобы подтвердить, отправьте: <code>/confirmreset</code>"
    )


@router.message(Command("confirmreset"))
@owner_only
async def cmd_confirmreset(message: Message):
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM sales")
        conn.execute("DELETE FROM accounts")
        conn.execute("DELETE FROM partners")
    await message.answer("Все данные удалены.")


# ------------------------------------------------------------------- main --

async def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан. Смотрите README.md / .env.example")
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    log.info("Бот запущен")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
