# -*- coding: utf-8 -*-
"""
 Бот доступа в клуб Creator Lab с оплатой ТОЛЬКО через ЮMoney.

Два тарифа:
  - month   : подписка на 1 месяц
  - forever : разовая покупка навсегда

Логика:
  1. /start -> приветствие + кнопки выбора тарифа.
  2. Бот просит почту (для чека/учёта) и сохраняет username.
  3. Бот создаёт ссылку на оплату ЮMoney.
  4. После оплаты ЮMoney отправляет POST на /yoomoney/notification.
  5. Бот опознаёт покупателя по label, выдаёт одноразовую ссылку в клуб и уведомляет админа.

Админ-панель:
  /admin — изменить цены кнопками
  /setprice month 699 — быстро изменить цену месяца
  /setprice forever 9990 — быстро изменить цену навсегда
"""

import os
import re
import json
import asyncio
import logging
import hashlib
import hmac
import html
import uuid
from datetime import datetime, timedelta
from typing import Optional

import pg8000.native
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart, StateFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CLUB_CHAT_ID = int(os.getenv("CLUB_CHAT_ID") or os.getenv("CHANNEL_ID") or "-1003973853516")
ADMIN_ID = int(os.getenv("ADMIN_ID") or os.getenv("OWNER_ID") or "1619432734")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

# ЮMoney кошелек
YOOMONEY_RECEIVER = os.getenv("YOOMONEY_RECEIVER", "").strip()
YOOMONEY_NOTIFICATION_SECRET = os.getenv("YOOMONEY_NOTIFICATION_SECRET", "").strip()
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("APP_URL")
    or "https://adelin-miller.onrender.com"
).rstrip("/")

TARIFFS = {
    "month": {
        "name": "1 месяц",
        "price": int(os.getenv("PRICE_MONTH", "899")),
    },
    "forever": {
        "name": "Навсегда",
        "price": int(os.getenv("PRICE_FOREVER", "9990")),
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("club-bot")

# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------

GREETING = (
    "Привет! Пока другие платят тысячи за курсы — ты получаешь всё и сразу 🔐\n\n"
    "Creator Lab — закрытый клуб для тех, кто делает контент с помощью нейросетей\n\n"
    "Внутри:\n"
    "🎬 Фото, видео, монтаж — от идеи до готового контента\n"
    "🤖 Собственные GPT-агенты — обученные, готовые к работе\n"
    "💸 Киношные видео в Seedance 2.0 — за такое платят от 500$\n"
    "🛠 Все актуальные сервисы — в одном месте, без лишнего поиска\n"
    "🏆 Сертификат по окончании\n"
    "💬 Уютный чат поддержки — живые люди, живые ответы\n"
    "🎁 Новые материалы каждую неделю\n\n"
    "Выбирай тариф 👇\n\n"
    "💎 Хочешь оплатить криптой? Напиши мне в личку @adelin_creator — подскажу реквизиты\n\n"
    "Если возникли проблемы с оплатой — напиши @adelin_creator"
)

ASK_EMAIL = (
    "Напиши, пожалуйста, свою почту 📧\n"
    "Она нужна для платежа и чека. Просто отправь её сообщением."
)

ASK_USERNAME = (
    "Чтобы я смог(ла) выдать тебе доступ, поставь, пожалуйста, "
    "<b>username</b> в настройках Telegram (Настройки → Имя пользователя), "
    "а потом нажми кнопку ещё раз 🙂"
)

BAD_EMAIL = "Хм, это не похоже на почту 🤔 Пришли в формате name@mail.ru, пожалуйста."

BLACKLIST_NOTICE = (
    "Администратор добавил вас в черный список клуба за нарушения оферты. "
    "Свяжитесь с администратором @adelin_creator"
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Buy(StatesGroup):
    waiting_email = State()
    waiting_method = State()


class AdminPrice(StatesGroup):
    waiting_for_price = State()


router = Router()

# ---------------------------------------------------------------------------
# База данных PostgreSQL
# ---------------------------------------------------------------------------

def parse_db_url(url: str):
    m = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):?(\d+)?/(.+)", url)
    if not m:
        raise ValueError("Не удалось разобрать DATABASE_URL")
    user, password, host, port, dbname = m.groups()
    return user, password, host, int(port or 5432), dbname


def get_db():
    user, password, host, port, dbname = parse_db_url(DATABASE_URL)
    return pg8000.native.Connection(
        user=user, password=password, host=host, port=port, database=dbname,
    )


def init_db():
    if not DATABASE_URL:
        log.warning("DATABASE_URL не задан — учёт сроков подписки работать НЕ будет.")
        return
    try:
        conn = get_db()
        conn.run('''CREATE TABLE IF NOT EXISTS members (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            tariff TEXT,
            start_dt TIMESTAMP,
            end_dt TIMESTAMP,
            status TEXT,
            reminded_stage INTEGER DEFAULT 0
        )''')
        conn.run('''CREATE TABLE IF NOT EXISTS consents (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            username TEXT,
            accepted_at TIMESTAMP
        )''')
        conn.run('''CREATE TABLE IF NOT EXISTS pending_members (
            username TEXT PRIMARY KEY,
            tariff TEXT,
            end_dt TIMESTAMP
        )''')
        conn.run('''CREATE TABLE IF NOT EXISTS blacklisted_users (
            id SERIAL PRIMARY KEY,
            user_id BIGINT UNIQUE,
            username TEXT,
            added_at TIMESTAMP
        )''')
        conn.run('''CREATE TABLE IF NOT EXISTS yoomoney_attempts (
            label TEXT PRIMARY KEY,
            email TEXT,
            user_id BIGINT,
            username TEXT,
            tariff TEXT,
            amount INTEGER,
            created_at TIMESTAMP,
            status TEXT
        )''')
        conn.close()
        log.info("База данных готова к работе.")
    except Exception as e:  # noqa: BLE001
        log.error("Ошибка инициализации базы: %s", e)


def save_consent(user_id: int, username: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            "INSERT INTO consents (user_id, username, accepted_at) VALUES (:uid, :un, :ts)",
            uid=user_id, un=username or "—", ts=datetime.now(),
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог записать согласие %s: %s", user_id, e)


def save_member(user_id: int, username: str, tariff_key: str,
                start_dt: datetime, end_dt: Optional[datetime]):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            '''INSERT INTO members (user_id, username, tariff, start_dt, end_dt, status, reminded_stage)
               VALUES (:uid, :un, :tf, :sd, :ed, 'active', 0)
               ON CONFLICT (user_id) DO UPDATE SET
               username=:un, tariff=:tf, start_dt=:sd, end_dt=:ed, status='active',
               reminded_stage=0 ''',
            uid=user_id, un=username, tf=tariff_key, sd=start_dt, ed=end_dt,
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог записать участника %s: %s", user_id, e)


def save_yoomoney_attempt(label: str, email: str, user_id: int, username: str,
                          tariff_key: str, amount: int):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run(
            '''INSERT INTO yoomoney_attempts
               (label, email, user_id, username, tariff, amount, created_at, status)
               VALUES (:label, :email, :uid, :un, :tf, :amount, :ts, 'pending')
               ON CONFLICT (label) DO UPDATE SET
               email=:email, user_id=:uid, username=:un, tariff=:tf,
               amount=:amount, created_at=:ts, status='pending' ''',
            label=label, email=email, uid=user_id, un=username or "—",
            tf=tariff_key, amount=amount, ts=datetime.now(),
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог сохранить оплату ЮMoney %s: %s", label, e)


def load_yoomoney_attempt(label: str) -> Optional[dict]:
    if not DATABASE_URL:
        return None
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT email, user_id, username, tariff, amount, status "
            "FROM yoomoney_attempts WHERE label=:label",
            label=label,
        )
        conn.close()
        if not rows:
            return None
        email, user_id, username, tariff, amount, status = rows[0]
        return {
            "email": email,
            "user_id": user_id,
            "username": username,
            "tariff": tariff,
            "amount": amount,
            "status": status,
        }
    except Exception as e:  # noqa: BLE001
        log.error("Не смог прочитать оплату ЮMoney %s: %s", label, e)
        return None


def mark_yoomoney_attempt_paid(label: str):
    if not DATABASE_URL:
        return
    try:
        conn = get_db()
        conn.run("UPDATE yoomoney_attempts SET status='paid' WHERE label=:label", label=label)
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отметить оплату ЮMoney %s: %s", label, e)


async def check_expired(bot: Bot):
    if not DATABASE_URL:
        return
    now = datetime.now()
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT user_id, end_dt, username FROM members WHERE end_dt IS NOT NULL AND status='active'"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("check_expired: не смог прочитать базу: %s", e)
        return

    for user_id, end_dt, username in rows:
        if (end_dt - now).total_seconds() <= 0:
            kicked_ok = False
            try:
                await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=user_id)
                kicked_ok = True
            except Exception as e:  # noqa: BLE001
                err = str(e).lower()
                ghost_markers = (
                    "participant_id_invalid", "user_not_participant",
                    "member not found", "user not found",
                    "can't initiate conversation", "chat not found",
                    "user_id_invalid",
                )
                if any(m in err for m in ghost_markers):
                    try:
                        c = get_db()
                        c.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Не удалось удалить участника</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Причина: {e}\n\n/remove {user_id}",
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue

            try:
                c = get_db()
                c.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=user_id)
                c.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                await bot.send_message(
                    user_id,
                    "Твоя подписка на месяц закончилась, доступ в клуб закрыт 🤍\n\n"
                    "Будем рады видеть снова — нажми /start, чтобы продлить.",
                )
            except Exception:  # noqa: BLE001
                pass
            if kicked_ok:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚪 <b>Участник удалён по окончании подписки</b>\n\n"
                        f"Пользователь: @{username or '—'} (id <code>{user_id}</code>)\n"
                        f"Подписка закончилась: {end_dt.strftime('%d.%m.%Y')}",
                    )
                except Exception:  # noqa: BLE001
                    pass


# --- Напоминания ---

REMIND_3_DAYS = (
    "Привет 🤍 Через 3 дня твой доступ в Creator Lab заканчивается.\n\n"
    "🎬 Новые материалы каждую неделю\n"
    "🤖 Готовые GPT-агенты и сервисы\n"
    "💬 Чат с поддержкой\n\n"
    "Продли заранее и оставайся с нами 👇"
)

REMIND_1_DAY = (
    "Привет 🤍 Завтра твой доступ в Creator Lab закрывается.\n\n"
    "Оставайся с нами — это одна кнопка 👇 Будем рады видеть тебя дальше 💛"
)


def renew_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💛 Продлить доступ", callback_data="renew_month")],
        ]
    )


async def check_reminders(bot: Bot):
    if not DATABASE_URL:
        return
    now = datetime.now()
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT user_id, end_dt, reminded_stage FROM members "
            "WHERE end_dt IS NOT NULL AND status='active' AND tariff='month'"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        log.error("check_reminders: ошибка чтения: %s", e)
        return

    for user_id, end_dt, reminded in rows:
        days_left = (end_dt - now).total_seconds() / 86400
        reminded = reminded or 0

        if 0 < days_left <= 1 and reminded != 1:
            try:
                await bot.send_message(user_id, REMIND_1_DAY, reply_markup=renew_keyboard())
                c = get_db()
                c.run("UPDATE members SET reminded_stage=1 WHERE user_id=:uid", uid=user_id)
                c.close()
            except Exception:  # noqa: BLE001
                pass

        elif 1 < days_left <= 3 and reminded == 0:
            try:
                await bot.send_message(user_id, REMIND_3_DAYS, reply_markup=renew_keyboard())
                c = get_db()
                c.run("UPDATE members SET reminded_stage=3 WHERE user_id=:uid", uid=user_id)
                c.close()
            except Exception:  # noqa: BLE001
                pass


async def build_status_text() -> str:
    if not DATABASE_URL:
        return "База не подключена, статус недоступен."
    now = datetime.now()
    try:
        conn = get_db()
        active = conn.run(
            "SELECT username, end_dt FROM members "
            "WHERE status='active' AND end_dt IS NOT NULL ORDER BY end_dt"
        )
        forever = conn.run("SELECT COUNT(*) FROM members WHERE status='active' AND end_dt IS NULL")
        expired = conn.run("SELECT COUNT(*) FROM members WHERE status='expired'")
        conn.close()
    except Exception as e:  # noqa: BLE001
        return f"Не смог прочитать базу: {e}"

    forever_n = forever[0][0] if forever else 0
    expired_n = expired[0][0] if expired else 0

    lines = [
        "✅ <b>Бот на посту и следит за клубом</b>\n",
        f"👥 Активных месячных: {len(active)}",
        f"💎 Навсегда: {forever_n}",
        f"🚪 Истёкших: {expired_n}\n",
    ]
    if active:
        lines.append("<b>Ближайшие окончания:</b>")
        for username, end_dt in active[:10]:
            days_left = (end_dt - now).total_seconds() / 86400
            mark = "⚠️" if days_left <= 3 else "🔹"
            lines.append(
                f"{mark} @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} "
                f"(осталось {int(days_left)} дн.)"
            )
    return "\n".join(lines)


async def daily_report(bot: Bot):
    try:
        today = datetime.now().strftime("%d.%m.%Y")
        text = await build_status_text()
        await bot.send_message(ADMIN_ID, f"🌙 <b>Итог дня — {today}</b>\n\n" + text)
    except Exception as e:  # noqa: BLE001
        log.error("Не смог отправить отчёт: %s", e)


# ---------------------------------------------------------------------------
# Меню и кнопки
# ---------------------------------------------------------------------------

def tariff_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💛 1 месяц — {TARIFFS['month']['price']}₽",
                callback_data="tariff_month")],
            [InlineKeyboardButton(
                text=f"💎 Навсегда — {TARIFFS['forever']['price']}₽",
                callback_data="tariff_forever")],
            [InlineKeyboardButton(
                text="⭐ Отзывы участников",
                url="https://t.me/creator_lab_otzyvy")],
        ]
    )


OFERTA_PATH = os.getenv("OFERTA_PATH", "oferta.pdf")

OFERTA_TEXT = (
    "Привет! 👋\n\n"
    "Прежде чем продолжить, пожалуйста, ознакомься с договором-офертой "
    "(во вложении 📄).\n\n"
    "Все материалы защищены авторским правом. Их нельзя копировать или продавать.\n\n"
    "Нажимая «Принимаю условия», ты подтверждаешь согласие."
)


def accept_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_oferta")],
        ]
    )


WARNING_TEXT = (
    "⚠️ <b>ВАЖНО. Прочти перед оплатой.</b>\n\n"
    "Все материалы Creator Lab — видео, промпты, методики, шаблоны — это интеллектуальная собственность.\n\n"
    "🚫 Копирование, перепродажа или использование материалов в своих курсах строго запрещены.\n\n"
    "Нажимая «Я соглашаюсь», ты подтверждаешь соблюдение правил."
)


def warning_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Я соглашаюсь", callback_data="accept_warning")],
        ]
    )


# ===================== АДМИН-ПАНЕЛЬ =====================

def _is_admin(message: Message) -> bool:
    return message.from_user.id == ADMIN_ID


def _clean_username(raw: str) -> str:
    return raw.strip().lstrip("@").strip()


def _parse_target(raw: str):
    t = raw.strip().lstrip("@").strip()
    if t.lstrip("-").isdigit():
        return int(t), None
    return None, t


async def _lookup_in_db(user_id=None, username=None):
    if not DATABASE_URL:
        return None, None
    try:
        conn = get_db()
        if user_id is not None:
            rows = conn.run("SELECT user_id, username FROM members WHERE user_id=:uid", uid=user_id)
        else:
            rows = conn.run(
                "SELECT user_id, username FROM members WHERE LOWER(username)=LOWER(:un)", un=username)
        conn.close()
        if rows:
            return rows[0][0], rows[0][1]
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _is_blacklisted(user_id: int, username: Optional[str]) -> bool:
    if not DATABASE_URL:
        return False
    uname = _clean_username(username or "") if username else None
    conn = None
    try:
        conn = get_db()
        if user_id and uname:
            rows = conn.run(
                "SELECT 1 FROM blacklisted_users WHERE user_id=:uid OR LOWER(username)=LOWER(:un) LIMIT 1",
                uid=user_id, un=uname,
            )
        elif user_id:
            rows = conn.run("SELECT 1 FROM blacklisted_users WHERE user_id=:uid LIMIT 1", uid=user_id)
        elif uname:
            rows = conn.run(
                "SELECT 1 FROM blacklisted_users WHERE LOWER(username)=LOWER(:un) LIMIT 1", un=uname)
        else:
            rows = []
        return len(rows) > 0
    except Exception:  # noqa: BLE001
        return False
    finally:
        if conn:
            conn.close()


def _add_to_blacklist(user_id: Optional[int], username: Optional[str]):
    if not DATABASE_URL:
        return
    uname = _clean_username(username or "") if username else None
    conn = get_db()
    try:
        if user_id:
            conn.run(
                "INSERT INTO blacklisted_users (user_id, username, added_at) VALUES (:uid, :un, :ts) "
                "ON CONFLICT (user_id) DO NOTHING",
                uid=user_id, un=uname, ts=datetime.now(),
            )
        elif uname:
            conn.run(
                "INSERT INTO blacklisted_users (user_id, username, added_at) VALUES (NULL, :un, :ts)",
                un=uname, ts=datetime.now(),
            )
    finally:
        conn.close()


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Изменить «1 месяц» ({TARIFFS['month']['price']}₽)",
                    callback_data="admin_setprice:month",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"Изменить «Навсегда» ({TARIFFS['forever']['price']}₽)",
                    callback_data="admin_setprice:forever",
                ),
            ],
        ]
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "⚙️ <b>Панель управления ценами</b>\n\n"
        f"💛 Тариф «1 месяц»: <b>{TARIFFS['month']['price']}₽</b>\n"
        f"💎 Тариф «Навсегда»: <b>{TARIFFS['forever']['price']}₽</b>\n\n"
        "Выбери кнопку, чтобы задать любую цену:",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data.startswith("admin_setprice:"))
async def on_admin_setprice_btn(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Только для админа", show_alert=True)
        return
    tariff_key = cb.data.split(":")[1]
    await state.update_data(tariff=tariff_key)
    await state.set_state(AdminPrice.waiting_for_price)
    await cb.message.answer(
        f"Напиши новую цену в рублях для тарифа <b>«{TARIFFS[tariff_key]['name']}»</b> (только цифры):"
    )
    await cb.answer()


@router.message(StateFilter(AdminPrice.waiting_for_price))
async def on_new_price_input(message: Message, state: FSMContext):
    if not _is_admin(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("Введи положительное число. Например: 890")
        return

    new_price = int(raw)
    data = await state.get_data()
    tariff_key = data.get("tariff")
    if tariff_key in TARIFFS:
        TARIFFS[tariff_key]["price"] = new_price
        await state.clear()
        await message.answer(
            f"✅ Цена тарифа «{TARIFFS[tariff_key]['name']}» обновлена: <b>{new_price}₽</b>!",
            reply_markup=admin_keyboard(),
        )
    else:
        await state.clear()
        await message.answer("Ошибка с тарифом. Нажми /admin заново.")


@router.message(Command("setprice"))
async def cmd_setprice(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or parts[1] not in ("month", "forever") or not parts[2].isdigit():
        await message.answer("Пример: <code>/setprice month 699</code> или <code>/setprice forever 9990</code>")
        return
    tariff_key, price = parts[1], int(parts[2])
    TARIFFS[tariff_key]["price"] = price
    await message.answer(f"✅ Цена тарифа «{TARIFFS[tariff_key]['name']}» теперь <b>{price}₽</b>.")


@router.message(Command("status"))
async def cmd_status(message: Message):
    if not _is_admin(message):
        return
    text = await build_status_text()
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not _is_admin(message):
        return
    await message.answer(
        "🛠 <b>Админ-команды</b>\n\n"
        "/admin — меню изменения цен\n"
        "/setprice month 699 — изменить цену месяца\n"
        "/setprice forever 9990 — изменить цену навсегда\n"
        "/status — краткая сводка\n"
        "/members — список участников\n"
        "/find @username — найти человека\n"
        "/add @username 30 — добавить на 30 дней\n"
        "/add @username forever — добавить навсегда\n"
        "/extend @username 30 — продлить на 30 дней\n"
        "/remove @username — кикнуть из клуба\n"
        "/ban @username — черный список\n"
    )


@router.message(Command("members"))
async def cmd_members(message: Message):
    if not _is_admin(message):
        return
    if not DATABASE_URL:
        await message.answer("База не подключена.")
        return
    now = datetime.now()
    try:
        conn = get_db()
        rows = conn.run(
            "SELECT username, tariff, start_dt, end_dt, status FROM members "
            "WHERE status='active' ORDER BY end_dt NULLS LAST"
        )
        conn.close()
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Ошибка базы: {e}")
        return

    if not rows:
        await message.answer("В базе пока нет активных участников.")
        return

    lines = [f"👥 <b>Активных участников: {len(rows)}</b>\n"]
    for i, (username, tariff, _start, end_dt, _status) in enumerate(rows, 1):
        if end_dt:
            days = int((end_dt - now).total_seconds() / 86400)
            lines.append(f"{i}. @{username or '—'} — до {end_dt.strftime('%d.%m.%Y')} (ост. {days} дн.)")
        else:
            lines.append(f"{i}. 💎 @{username or '—'} (навсегда)")

    await message.answer("\n".join(lines[:60]))


@router.message(Command("add"))
async def cmd_add(message: Message):
    if not _is_admin(message):
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer("Пример: /add @username 30 или /add @username forever")
        return
    target, term = parts[1], parts[2].lower()
    uid, uname = _parse_target(target)

    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer(f"Не знаю id для @{uname}. Укажи числовой id или перешли сообщение.")
        return

    start_dt = datetime.now()
    if term == "forever":
        end_dt = None
        tariff = "forever"
    else:
        try:
            days = int(term)
        except ValueError:
            await message.answer("Укажи число дней или forever.")
            return
        end_dt = start_dt + timedelta(days=days)
        tariff = "month"

    save_member(uid, uname or "—", tariff, start_dt, end_dt)
    await message.answer(f"✅ Добавлен @{uname or uid} ({term}).")


@router.message(Command("remove"))
async def cmd_remove(message: Message, bot: Bot):
    if not _is_admin(message):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Пример: /remove @username или /remove 123456789")
        return
    uid, uname = _parse_target(parts[1])
    if uid is None:
        uid, found = await _lookup_in_db(username=uname)
        if found:
            uname = found
    if uid is None:
        await message.answer("Пользователь не найден.")
        return

    if DATABASE_URL:
        try:
            conn = get_db()
            conn.run("UPDATE members SET status='expired' WHERE user_id=:uid", uid=uid)
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    try:
        await bot.ban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await bot.unban_chat_member(chat_id=CLUB_CHAT_ID, user_id=uid)
        await message.answer(f"✅ @{uname or uid} удалён из клуба.")
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Из базы убран, но кикнуть в Telegram не удалось: {e}")


# ===================== ПОЛЬЗОВАТЕЛЬСКИЙ ПУТЬ =====================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    try:
        await message.answer_document(
            FSInputFile(OFERTA_PATH, filename="Договор-оферта Creator Lab.pdf"),
            caption=OFERTA_TEXT,
            reply_markup=accept_keyboard(),
        )
    except Exception:  # noqa: BLE001
        await message.answer(OFERTA_TEXT, reply_markup=accept_keyboard())


@router.callback_query(F.data == "accept_oferta")
async def on_accept(cb: CallbackQuery):
    user = cb.from_user
    save_consent(user.id, user.username)
    await cb.message.answer(WARNING_TEXT, reply_markup=warning_keyboard())
    await cb.answer("Условия приняты ✅")


@router.callback_query(F.data == "accept_warning")
async def on_accept_warning(cb: CallbackQuery):
    user = cb.from_user
    save_consent(user.id, user.username)
    await cb.message.answer(GREETING, reply_markup=tariff_keyboard())
    await cb.answer("Принято ✅")


@router.callback_query(F.data.in_({"tariff_month", "tariff_forever"}))
async def on_tariff(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    if not user.username:
        await cb.message.answer(ASK_USERNAME)
        await cb.answer()
        return
    if _is_blacklisted(user.id, user.username):
        await cb.message.answer(BLACKLIST_NOTICE)
        await cb.answer()
        return

    tariff_key = "month" if cb.data == "tariff_month" else "forever"
    await state.update_data(tariff=tariff_key)
    await state.set_state(Buy.waiting_email)
    await cb.message.answer(
        f"Тариф «{TARIFFS[tariff_key]['name']}» — отличный выбор 🙂\n\n" + ASK_EMAIL
    )
    await cb.answer()


@router.callback_query(F.data == "renew_month")
async def on_renew(cb: CallbackQuery, state: FSMContext):
    user = cb.from_user
    if not user.username:
        await cb.message.answer(ASK_USERNAME)
        await cb.answer()
        return
    if _is_blacklisted(user.id, user.username):
        await cb.message.answer(BLACKLIST_NOTICE)
        await cb.answer()
        return
    await state.update_data(tariff="month")
    await state.set_state(Buy.waiting_email)
    await cb.message.answer("Продлеваем доступ на 1 месяц 💛\n\n" + ASK_EMAIL)
    await cb.answer()


@router.message(StateFilter(Buy.waiting_email))
async def on_email(message: Message, state: FSMContext):
    email = (message.text or "").strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer(BAD_EMAIL)
        return

    user = message.from_user
    if not user.username:
        await message.answer(ASK_USERNAME)
        await state.clear()
        return

    data = await state.get_data()
    tariff_key = data.get("tariff")
    if not tariff_key or tariff_key not in TARIFFS:
        await message.answer("Сессия сброшена. Нажми /start заново.")
        await state.clear()
        return

    if not YOOMONEY_RECEIVER or not YOOMONEY_NOTIFICATION_SECRET:
        await message.answer("Оплата настраивается. Напиши @adelin_creator.")
        await state.clear()
        return

    price = TARIFFS[tariff_key]["price"]
    label = f"ym_{user.id}_{uuid.uuid4().hex[:12]}"
    save_yoomoney_attempt(label, email, user.id, user.username, tariff_key, price)
    await state.clear()

    url = f"{PUBLIC_BASE_URL}/yoomoney/pay/{label}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Перейти к оплате 💳", url=url)]]
    )
    await message.answer(
        f"Готово! Нажми кнопку ниже и оплати доступ ({price}₽) через ЮMoney.\n\n"
        "Как только оплата пройдёт, я пришлю ссылку в клуб прямо сюда.",
        reply_markup=kb,
    )


# ---------------------------------------------------------------------------
# Выдача доступа
# ---------------------------------------------------------------------------

async def grant_access(bot: Bot, email: str, amount, currency, contract_id: str,
                       tariff_name: str, tariff_key: str, user_id: int, username: str):
    username = username or "—"

    if _is_blacklisted(user_id, username):
        try:
            await bot.send_message(user_id, BLACKLIST_NOTICE)
        except Exception:  # noqa: BLE001
            pass
        return

    try:
        link = await bot.create_chat_invite_link(
            chat_id=CLUB_CHAT_ID,
            member_limit=1,
            name=f"buyer {user_id}",
        )
        invite_url = link.invite_link
    except Exception as e:  # noqa: BLE001
        log.exception("Не смог создать ссылку: %s", e)
        await bot.send_message(
            ADMIN_ID,
            f"❗️ Оплата прошла (@{username}, {email}), но я не смог создать ссылку: {e}",
        )
        return

    start_dt = datetime.now()
    end_dt = start_dt + timedelta(days=30) if tariff_key == "month" else None

    if end_dt:
        period_line = (
            f"📅 Доступ активен с {start_dt.strftime('%d.%m.%Y')} "
            f"по {end_dt.strftime('%d.%m.%Y')}.\n"
            "Я напомню тебе об окончании заранее 🤍\n\n"
        )
    else:
        period_line = "📅 Доступ — навсегда 💎\n\n"

    try:
        await bot.send_message(
            user_id,
            "Оплата получена, спасибо! 💛\n\n"
            "Добро пожаловать в Creator Lab 🔐\n\n"
            f"{period_line}"
            f"Вот твоя ссылка в клуб:\n{invite_url}\n\nЗаходи и пользуйся 🚀",
        )
    except Exception as e:  # noqa: BLE001
        log.error("Не смог доставить ссылку клиенту %s: %s", user_id, e)

    save_member(user_id, username, tariff_key, start_dt, end_dt)

    access_str = f"с {start_dt.strftime('%d.%m.%Y')} по {end_dt.strftime('%d.%m.%Y')}" if end_dt else "навсегда"
    await bot.send_message(
        ADMIN_ID,
        f"💰 <b>Новая продажа!</b>\n\n"
        f"Тариф: {tariff_name}\n"
        f"Покупатель: @{username} (id <code>{user_id}</code>)\n"
        f"Почта: {email}\n"
        f"Сумма: {amount} {currency}\n"
        f"Метка: {contract_id}\n"
        f"Срок: {access_str}",
    )


# ---------------------------------------------------------------------------
# Сервер ЮMoney
# ---------------------------------------------------------------------------

def verify_yoomoney_sign(params: dict[str, str]) -> bool:
    if not YOOMONEY_NOTIFICATION_SECRET:
        return False
    given_hash = (params.get("sha1_hash") or params.get("sign") or "").strip().lower()
    if not given_hash:
        return False

    fields = [
        params.get("notification_type", ""),
        params.get("operation_id", ""),
        params.get("amount", ""),
        params.get("currency", ""),
        params.get("datetime", ""),
        params.get("sender", ""),
        params.get("codepro", ""),
        YOOMONEY_NOTIFICATION_SECRET,
        params.get("label", ""),
    ]
    raw = "&".join(str(f) for f in fields)
    calculated_hash = hashlib.sha1(raw.encode("utf-8")).hexdigest().lower()
    return hmac.compare_digest(calculated_hash, given_hash)


async def handle_yoomoney_pay(request: web.Request) -> web.Response:
    label = request.match_info.get("label", "")
    info = load_yoomoney_attempt(label)
    if not info or info.get("status") == "paid":
        return web.Response(status=404, text="payment not found")
    if not YOOMONEY_RECEIVER:
        return web.Response(status=500, text="YOOMONEY_RECEIVER is not configured")

    tariff_key = info.get("tariff")
    tariff_name = TARIFFS.get(tariff_key, {}).get("name", "доступ")
    amount = int(info.get("amount") or TARIFFS.get(tariff_key, {}).get("price", 0))
    email = info.get("email") or ""
    targets = f"Creator Lab: {tariff_name}"

    fields = {
        "receiver": YOOMONEY_RECEIVER,
        "quickpay-form": "shop",
        "paymentType": "AC",
        "sum": str(amount),
        "label": label,
        "targets": targets,
        "formcomment": targets,
        "short-dest": targets,
        "successURL": f"{PUBLIC_BASE_URL}/",
        "comment": f"Telegram @{info.get('username') or 'user'}",
        "need-email": "true",
        "email": email,
    }
    inputs = "\n".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in fields.items()
    )
    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Оплата Creator Lab</title>
</head>
<body>
  <p>Переходим к оплате через ЮMoney...</p>
  <form id="pay" method="POST" action="https://yoomoney.ru/quickpay/confirm">
    {inputs}
    <button type="submit">Перейти к оплате</button>
  </form>
  <script>document.getElementById('pay').submit();</script>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


async def handle_yoomoney_notification(request: web.Request) -> web.Response:
    log.info("🔔 [ЮMoney Webhook] Запрос: путь=%s", request.path)
    try:
        form = await request.post()
        params = {k: str(v) for k, v in form.items()}
    except Exception as e:
        log.error("ЮMoney: ошибка чтения формы: %s", e)
        return web.Response(status=200, text="OK")

    log.info("🔔 [ЮMoney Webhook] Данные формы: %s", json.dumps(params, ensure_ascii=False)[:2000])

    if not params:
        return web.Response(status=200, text="OK")

    if not verify_yoomoney_sign(params):
        log.warning("ЮMoney: неверный sha1_hash для операции %s", params.get("operation_id"))
        return web.Response(status=200, text="OK")

    label = params.get("label") or ""
    info = load_yoomoney_attempt(label)
    if not info or info.get("status") == "paid":
        return web.Response(status=200, text="OK")

    expected = int(info.get("amount") or 0)
    paid_raw = params.get("withdraw_amount") or params.get("amount") or "0"
    try:
        paid = float(str(paid_raw).replace(",", "."))
    except ValueError:
        paid = 0

    if paid + 0.01 < expected:
        log.warning("ЮMoney: сумма меньше ожидаемой %s < %s", paid, expected)
        return web.Response(status=200, text="OK")

    tariff_key = info.get("tariff")
    tariff_name = TARIFFS.get(tariff_key, {}).get("name", "—")

    await grant_access(
        request.app["bot"],
        info.get("email"),
        paid_raw,
        "RUB",
        params.get("operation_id") or label,
        tariff_name=tariff_name,
        tariff_key=tariff_key,
        user_id=info.get("user_id"),
        username=info.get("username"),
    )
    mark_yoomoney_attempt_paid(label)
    return web.Response(status=200, text="OK")


async def handle_ping(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def handle_check(_request: web.Request) -> web.Response:
    return web.Response(text="YOOMONEY NOTIFICATION ENDPOINT OK")


# ---------------------------------------------------------------------------
# Запуск
# ---------------------------------------------------------------------------

async def on_startup(bot: Bot):
    try:
        await bot.set_my_commands(
            [BotCommand(command="start", description="Перезапустить бота")],
            scope=BotCommandScopeDefault(),
        )
        await bot.set_my_commands(
            [
                BotCommand(command="admin", description="⚙️ Изменить цены"),
                BotCommand(command="status", description="📊 Статус клуба"),
                BotCommand(command="members", description="👥 Список участников"),
                BotCommand(command="help", description="🛠 Все команды"),
            ],
            scope=BotCommandScopeChat(chat_id=ADMIN_ID),
        )
        log.info("Меню настроено.")
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось настроить меню: %s", e)


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN в переменных окружения")

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    init_db()
    await on_startup(bot)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_expired, "interval", hours=1, args=[bot])
    scheduler.add_job(check_reminders, "interval", hours=12, args=[bot])
    scheduler.add_job(daily_report, CronTrigger(hour=20, minute=0), args=[bot])
    scheduler.start()

    app = web.Application()
    app["bot"] = bot

    # Маршруты
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_ping)
    app.router.add_get("/yoomoney/pay/{label}", handle_yoomoney_pay)

    # Приём уведомлений ЮMoney по любым адресам
    for p in (
        "/yoomoney/notification",
        "/yoomoney/notification/",
        "/yoomoney/notification/{secret}",
        "/yoomoney/notification/{secret}/",
    ):
        app.router.add_get(p, handle_check)
        app.router.add_post(p, handle_yoomoney_notification)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Сервер слушает порт %s.", PORT)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
